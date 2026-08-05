"""`chrome_restart`: stop every Chrome on the loop's profile, and only those.

The test that carries the most weight is
`test_a_browser_on_a_different_profile_is_never_signalled`. Its decoys run the
SAME binary as the loop's Chrome — that is the point. An implementation that
matched on the binary name would pass every other test here and, in real use,
end the operator's session mid-work. One decoy also sits on a profile whose
path merely EXTENDS the loop's, so a substring match dies too.

Nothing here touches the machine: `FakeChrome` supplies the process list, the
signals, the port probe, the endpoint probe and the launch, and `restart()`
requires an ops object, so there is no live default a test could inherit by
forgetting to pass one.
"""

import json

import pytest

from autoloop.browser import chrome_restart
from autoloop.browser.chrome_restart import (
    ChromeOps,
    RestartResult,
    endpoint_body_is_ready,
    main,
    matches_profile,
    profile_values,
    restart,
)

LOOP_PROFILE = "/Users/op/.autoloop-chrome"
PORT = 9222
#: The real macOS path, spaces and all — the parser has to survive actual `ps`
#: output, not a tidied-up version of it.
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
#: Where the operator's everyday Chrome keeps its profile. Also has spaces.
OPERATOR_PROFILE = "/Users/op/Library/Application Support/Google/Chrome"


def _chrome(pid: int, profile: str, *, port: int | None = None) -> tuple[int, str]:
    command = f"{CHROME} --user-data-dir={profile}"
    if port is not None:
        command += f" --remote-debugging-port={port}"
    return (pid, command)


class FakeChrome:
    """A machine that records what was done to it and did none of it.

    `calls` is an ordered log, which is what lets a test assert SEQUENCE — that
    the launch happened after the port read free, not merely that it happened.
    """

    def __init__(
        self,
        processes=(),
        *,
        port_held_probes: int | None = 0,
        ready_after_probes: int | None = 0,
        deaf_pids=(),
        ghost_pids=(),
    ):
        #: `port_held_probes=None` means the port is never released;
        #: `ready_after_probes=None` means the endpoint never answers.
        self.processes = list(processes)
        self.port_held_probes = port_held_probes
        self.ready_after_probes = ready_after_probes
        #: pids that ignore SIGTERM and stay in the process list.
        self.deaf_pids = set(deaf_pids)
        #: pids that appear in the listing but exit before the signal lands.
        self.ghost_pids = set(ghost_pids)
        self.calls: list[str] = []
        self.terminated: list[int] = []
        self.launches: list[tuple[str, int]] = []
        self.sleeps: list[float] = []
        self._port_probes = 0
        self._ready_probes = 0

    # --- the injected boundary ---------------------------------------------

    def list_processes(self):
        self.calls.append("list")
        return list(self.processes)

    def terminate(self, pid):
        self.calls.append(f"term:{pid}")
        self.terminated.append(pid)
        alive = [p for p, _ in self.processes]
        if pid in self.ghost_pids or pid not in alive:
            self.processes = [(p, c) for p, c in self.processes if p != pid]
            raise ProcessLookupError(pid)
        if pid not in self.deaf_pids:
            self.processes = [(p, c) for p, c in self.processes if p != pid]

    def port_in_use(self, port):
        self._port_probes += 1
        held = self.port_held_probes is None or self._port_probes <= self.port_held_probes
        self.calls.append(f"port:{'held' if held else 'free'}:{port}")
        return held

    def endpoint_ready(self, port):
        self._ready_probes += 1
        ready = (
            self.ready_after_probes is not None
            and self._ready_probes > self.ready_after_probes
        )
        self.calls.append(f"ready:{ready}")
        return ready

    def launch(self, profile, port):
        self.calls.append(f"launch:{profile}:{port}")
        self.launches.append((profile, port))

    def sleep(self, seconds):
        self.calls.append("sleep")
        self.sleeps.append(seconds)

    def ops(self) -> ChromeOps:
        return ChromeOps(
            list_processes=self.list_processes,
            terminate=self.terminate,
            port_in_use=self.port_in_use,
            endpoint_ready=self.endpoint_ready,
            launch=self.launch,
            sleep=self.sleep,
        )


def _restart(fake: FakeChrome, profile: str = LOOP_PROFILE, **kw) -> RestartResult:
    kw.setdefault("poll_interval", 0.0)
    return restart(profile, PORT, fake.ops(), **kw)


# --- 1. every instance on the profile, not the first pid ----------------------


def test_two_instances_on_the_profile_are_both_stopped():
    """The bug this module exists for: stopping one of two leaves the survivor
    owning the debug port, and the replacement can never bind it."""
    fake = FakeChrome([_chrome(90888, LOOP_PROFILE, port=PORT), _chrome(94120, LOOP_PROFILE)])

    result = _restart(fake)

    assert fake.terminated == [90888, 94120]
    assert result.ok, result.detail
    assert result.matched_pids == (90888, 94120)


# --- 2. the safety bound ------------------------------------------------------


def test_a_browser_on_a_different_profile_is_never_signalled():
    """THE test. The decoys run the same binary as the loop's Chrome, so an
    implementation matching on the binary name fails here and nowhere else —
    and the difference is between restarting the loop's browser and ending the
    operator's session.

    The third entry extends the loop's profile path, which kills a substring
    match too.
    """
    fake = FakeChrome(
        [
            _chrome(101, OPERATOR_PROFILE),
            _chrome(202, LOOP_PROFILE, port=PORT),
            _chrome(303, f"{LOOP_PROFILE}-old"),
        ]
    )

    result = _restart(fake)

    assert fake.terminated == [202]
    assert 101 not in fake.terminated, "the operator's own browser was signalled"
    assert 303 not in fake.terminated, "a profile that merely shares a prefix was signalled"
    assert result.matched_pids == (202,)
    assert result.ok, result.detail


def test_the_same_profile_written_differently_still_matches():
    """A trailing slash or a `.` segment is the same directory. Missing it
    would leave a live instance holding the port while we report success."""
    fake = FakeChrome([_chrome(11, f"{LOOP_PROFILE}/"), _chrome(12, "/Users/op/./.autoloop-chrome")])

    assert _restart(fake).ok
    assert fake.terminated == [11, 12]


def test_an_unusable_profile_is_refused_before_anything_is_signalled():
    """A relative or empty `--user-data-dir` matches either nothing or far too
    much, so it never gets as far as a signal."""
    for bad in ("", "   ", "relative/profile", "/"):
        fake = FakeChrome([_chrome(1, LOOP_PROFILE, port=PORT)])
        result = _restart(fake, profile=bad)
        assert not result.ok
        assert "not a usable profile" in result.detail
        assert fake.terminated == []
        assert fake.launches == []


def test_the_matcher_reads_values_not_binaries():
    assert matches_profile(_chrome(1, LOOP_PROFILE)[1], LOOP_PROFILE)
    assert not matches_profile(_chrome(1, OPERATOR_PROFILE)[1], LOOP_PROFILE)
    assert not matches_profile(_chrome(1, f"{LOOP_PROFILE}-old")[1], LOOP_PROFILE)
    # A profile with spaces is read whole, up to the next flag.
    assert profile_values(_chrome(1, OPERATOR_PROFILE, port=PORT)[1]) == [OPERATOR_PROFILE]
    # A command line naming no profile at all can never match.
    assert profile_values(f"{CHROME} --remote-debugging-port=9222") == []


# --- 3. the port must be free BEFORE the launch -------------------------------


def test_the_launch_waits_until_the_port_reads_free():
    """A kill is not proof the port was released — poll for it. Asserted as an
    ORDERING, because "a launch happened" also passes for an implementation
    that launches first and checks afterwards."""
    fake = FakeChrome([_chrome(202, LOOP_PROFILE, port=PORT)], port_held_probes=2)

    result = _restart(fake)

    assert result.ok, result.detail
    launch_at = fake.calls.index(f"launch:{LOOP_PROFILE}:{PORT}")
    held_before = [c for c in fake.calls[:launch_at] if c.startswith("port:held")]
    assert len(held_before) == 2, "did not wait out the probes that read held"
    assert fake.calls[:launch_at].count(f"port:free:{PORT}") == 1
    assert fake.calls[launch_at - 1] == f"port:free:{PORT}", "launched before the port read free"
    assert fake.launches == [(LOOP_PROFILE, PORT)]


def test_a_port_that_never_frees_refuses_to_launch():
    """Launching into a held port is the original fault reproduced: a second
    Chrome that cannot bind, beside one the loop can no longer drive."""
    fake = FakeChrome([_chrome(202, LOOP_PROFILE, port=PORT)], port_held_probes=None)

    result = _restart(fake, port_attempts=4)

    assert not result.ok
    assert f"port {PORT} is still held" in result.detail
    assert fake.launches == [], "launched a second instance into a held port"
    assert result.launched is False
    assert len([c for c in fake.calls if c.startswith("port:")]) == 4, "unbounded polling"
    assert len(fake.sleeps) == 3, "slept after the last probe"


def test_a_process_that_will_not_exit_reports_failure_and_does_not_launch():
    """A stop that failed reports failure rather than starting a second
    instance next to the survivor."""
    fake = FakeChrome([_chrome(94120, LOOP_PROFILE, port=PORT)], deaf_pids={94120})

    result = _restart(fake, stop_attempts=3)

    assert not result.ok
    assert "94120" in result.detail and "did not exit" in result.detail
    assert fake.launches == []
    assert result.matched_pids == (94120,), "a survivor is reported as matched, never as stopped"


def test_no_matching_process_still_launches_when_the_port_is_free():
    """Chrome died outright: there is nothing to stop and everything to start.
    Refusing here would leave the loop with no browser at all."""
    fake = FakeChrome([_chrome(101, OPERATOR_PROFILE)])

    result = _restart(fake)

    assert result.ok, result.detail
    assert fake.terminated == []
    assert fake.launches == [(LOOP_PROFILE, PORT)]
    assert result.matched_pids == ()


def test_a_pid_that_exits_between_the_listing_and_the_signal_is_not_a_failure():
    """It exited on its own. That is the outcome we asked for, not an error."""
    fake = FakeChrome([_chrome(202, LOOP_PROFILE, port=PORT)], ghost_pids={202})

    result = _restart(fake)

    assert result.ok, result.detail
    assert fake.terminated == [202]
    assert fake.launches == [(LOOP_PROFILE, PORT)]


# --- 4. success means the endpoint answered -----------------------------------


def test_success_requires_a_positive_endpoint_response():
    """Owning the port is the success condition, not a launcher's exit code —
    a half-started browser looks exactly like the fault being recovered from."""
    fake = FakeChrome([_chrome(202, LOOP_PROFILE, port=PORT)], ready_after_probes=None)

    result = _restart(fake, ready_attempts=3)

    assert not result.ok
    assert "did not answer" in result.detail
    assert result.launched is True, "the launch did happen; only the confirmation failed"
    assert len([c for c in fake.calls if c.startswith("ready:")]) == 3, "unbounded polling"


def test_a_slow_start_is_waited_out_not_failed():
    fake = FakeChrome([_chrome(202, LOOP_PROFILE, port=PORT)], ready_after_probes=2)

    result = _restart(fake, ready_attempts=5)

    assert result.ok, result.detail
    assert result.detail.endswith(f"on {LOOP_PROFILE})")
    assert fake.calls[-1] == "ready:True"


@pytest.mark.parametrize(
    "body, ready",
    [
        (json.dumps({"Browser": "Chrome/1", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/x"}), True),
        (json.dumps({"Browser": "Chrome/1"}), False),
        (json.dumps({"webSocketDebuggerUrl": ""}), False),
        (json.dumps(["not", "a", "dict"]), False),
        ("<html>proxy says hello</html>", False),
        ("", False),
    ],
)
def test_only_a_websocket_url_counts_as_ready(body, ready):
    """A 200 alone is too weak: the fault being recovered from is a browser
    that answers and cannot be driven."""
    assert endpoint_body_is_ready(body) is ready


# --- 5. the loop's consumers see a usable report ------------------------------


def test_the_ops_object_is_required():
    """No live default to forget: a test cannot accidentally run `ps`, signal a
    real pid, or start a real browser."""
    with pytest.raises(TypeError):
        restart(LOOP_PROFILE, PORT)


def test_a_failure_is_reported_never_raised():
    """The caller is already handling a browser fault; a second exception on
    top of the first turns the recovery path into a crash."""

    def boom(_pid):
        raise PermissionError("not owned by this user")

    fake = FakeChrome([_chrome(202, LOOP_PROFILE, port=PORT)])
    ops = fake.ops()
    result = restart(
        LOOP_PROFILE,
        PORT,
        ChromeOps(
            list_processes=ops.list_processes,
            terminate=boom,
            port_in_use=ops.port_in_use,
            endpoint_ready=ops.endpoint_ready,
            launch=ops.launch,
            sleep=ops.sleep,
        ),
        poll_interval=0.0,
    )

    assert not result.ok
    assert "cannot signal pid 202" in result.detail
    assert fake.launches == []


# --- 6. the entry point brw-08 will point `restart_command` at -----------------


@pytest.fixture
def captured_restart(monkeypatch):
    """`main()` with the real work stubbed out — the CLI wiring under test.

    Without this the entry point is executed by nothing, and a typo in it
    passes ruff and pytest both while breaking the only thing brw-08 wires up.
    """
    seen: dict = {}

    def fake_restart(profile, port, ops, **kw):
        seen.update(profile=profile, port=port, ops=ops, kw=kw)
        return seen.get("result", RestartResult(True, "autoloop chrome up on port 9222"))

    monkeypatch.setattr(chrome_restart, "restart", fake_restart)
    return seen


def test_main_exits_zero_and_reports_on_stdout_when_it_worked(captured_restart, capsys):
    assert main([]) == 0
    out = capsys.readouterr()
    assert "autoloop chrome up" in out.out
    assert out.err == ""


def test_main_exits_one_and_puts_the_diagnosis_on_stderr(captured_restart, capsys):
    """`cli._repair_browser` and `orchestrator._attempt_browser_restart` both
    report `result.stderr` on a non-zero exit. A detail printed to stdout
    reaches the operator as "restart FAILED:" followed by nothing."""
    captured_restart["result"] = RestartResult(False, "port 9222 is still held")

    assert main([]) == 1
    out = capsys.readouterr()
    assert "port 9222 is still held" in out.err
    assert "port 9222 is still held" not in out.out


def test_main_passes_its_flags_through(captured_restart):
    assert main(["--profile", "/tmp/profile", "--port", "9333"]) == 0
    assert captured_restart["profile"] == "/tmp/profile"
    assert captured_restart["port"] == 9333


def test_main_reads_the_environment_the_shell_helper_honoured(captured_restart, monkeypatch):
    """The operator's existing AUTOLOOP_CHROME_* overrides keep working — and
    the port arrives as an int, not the string the environment holds."""
    monkeypatch.setenv("AUTOLOOP_CHROME_PROFILE", "/tmp/from-env")
    monkeypatch.setenv("AUTOLOOP_CHROME_PORT", "9444")

    assert main([]) == 0
    assert captured_restart["profile"] == "/tmp/from-env"
    assert captured_restart["port"] == 9444


def test_the_live_ops_set_can_be_built():
    """A renamed or missing field in `ChromeOps.real()` is a TypeError that no
    linter catches and that fires only when the loop is already broken.

    Building it runs nothing: the fields hold function references.
    """
    ops = ChromeOps.real("/nonexistent/chrome")

    assert isinstance(ops, ChromeOps)
    assert all(
        callable(getattr(ops, field))
        for field in ("list_processes", "terminate", "port_in_use", "endpoint_ready", "launch", "sleep")
    )
