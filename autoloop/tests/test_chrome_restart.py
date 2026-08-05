"""`chrome_restart`: stop every Chrome on the loop's profile, and only those.

Nothing here touches the machine — `FakeChrome` supplies the listing, the
signals, both probes and the launch, and `restart()` requires an ops object, so
no test can inherit a live default.
"""

import json
from dataclasses import replace

import pytest

from autoloop.browser import chrome_restart
from autoloop.browser.chrome_restart import (
    ChromeOps,
    RestartResult,
    endpoint_body_is_ready,
    main,
    profile_values,
    restart,
)

LOOP_PROFILE = "/Users/op/.autoloop-chrome"
PORT = 9222
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
#: The operator's everyday profile — the real macOS path, spaces and all.
OPERATOR_PROFILE = "/Users/op/Library/Application Support/Google/Chrome"


def _chrome(pid: int, profile: str, *, port: int | None = None) -> tuple[int, str]:
    command = f"{CHROME} --user-data-dir={profile}"
    if port is not None:
        command += f" --remote-debugging-port={port}"
    return (pid, command)


class FakeChrome:
    """A machine that records what was done to it and did none of it.

    `calls` is an ordered log, which is what lets a test assert SEQUENCE — that
    the launch came after the port read free. `port_held_probes=None` means the
    port is never released and `ready_after_probes=None` that the endpoint never
    answers; `deaf_pids` ignore SIGTERM, `ghost_pids` are listed but gone before
    the signal lands.
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
        self.processes = list(processes)
        self.port_held_probes, self.ready_after_probes = port_held_probes, ready_after_probes
        self.deaf_pids, self.ghost_pids = set(deaf_pids), set(ghost_pids)
        self.calls, self.terminated, self.launches, self.sleeps = [], [], [], []
        self._port_probes = self._ready_probes = 0

    def list_processes(self):
        self.calls.append("list")
        return list(self.processes)

    def terminate(self, pid):
        self.calls.append(f"term:{pid}")
        self.terminated.append(pid)
        gone = pid in self.ghost_pids
        if gone or pid not in self.deaf_pids:
            self.processes = [(p, c) for p, c in self.processes if p != pid]
        if gone:
            raise ProcessLookupError(pid)

    def port_in_use(self, port):
        self._port_probes += 1
        held = self.port_held_probes is None or self._port_probes <= self.port_held_probes
        self.calls.append(f"port:{'held' if held else 'free'}:{port}")
        return held

    def endpoint_ready(self, port):
        self._ready_probes += 1
        ready = self.ready_after_probes is not None and self._ready_probes > self.ready_after_probes
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


def test_every_instance_on_the_profile_is_stopped():
    """Stopping one of two leaves the survivor owning the debug port. The second
    spells the profile with a trailing slash — the same directory. The port reads
    held once first, so the launch has to wait it out: a kill is not proof the
    port was freed, and the ordering is asserted because "a launch happened" also
    passes for an implementation that launches first and checks afterwards."""
    fake = FakeChrome(
        [_chrome(90888, LOOP_PROFILE, port=PORT), _chrome(94120, f"{LOOP_PROFILE}/")],
        port_held_probes=1,
    )
    result = _restart(fake)
    assert fake.terminated == [90888, 94120]
    assert result.matched_pids == (90888, 94120)
    assert result.ok, result.detail
    assert fake.launches == [(LOOP_PROFILE, PORT)]
    launch_at = fake.calls.index(f"launch:{LOOP_PROFILE}:{PORT}")
    assert fake.calls[launch_at - 1] == f"port:free:{PORT}", "launched before the port read free"


def test_only_the_loop_profile_is_signalled():
    """THE test. Both decoys run the same binary as the loop's Chrome, so an
    implementation matching on the binary name fails here and nowhere else — the
    difference between restarting the loop's browser and ending the operator's
    session. The third entry extends the loop's path, so a substring match dies
    too."""
    fake = FakeChrome(
        [
            _chrome(101, OPERATOR_PROFILE),
            _chrome(202, LOOP_PROFILE, port=PORT),
            _chrome(303, f"{LOOP_PROFILE}-old"),
        ]
    )
    result = _restart(fake)
    assert fake.terminated == [202], "signalled a browser on another profile"
    assert result.matched_pids == (202,)
    assert result.ok, result.detail
    # A profile with spaces is read whole, up to the next flag.
    assert profile_values(_chrome(1, OPERATOR_PROFILE, port=9333)[1]) == [OPERATOR_PROFILE]


def test_an_unusable_profile_is_refused_before_anything_is_signalled():
    """Empty, relative or root matches either nothing or far too much."""
    for bad in ("", "relative/profile", "/"):
        fake = FakeChrome([_chrome(202, LOOP_PROFILE, port=PORT)])
        result = _restart(fake, profile=bad)
        assert not result.ok
        assert "not a usable profile" in result.detail
        assert fake.terminated == [] and fake.launches == []


def test_a_process_that_will_not_exit_refuses_to_launch():
    """A failed stop reports failure rather than starting a second instance."""
    fake = FakeChrome([_chrome(94120, LOOP_PROFILE, port=PORT)], deaf_pids={94120})
    result = _restart(fake, stop_attempts=3)
    assert not result.ok
    assert "94120" in result.detail and "did not exit" in result.detail
    assert fake.launches == []
    assert result.matched_pids == (94120,), "a survivor is matched, never stopped"


def test_a_pid_that_exits_before_the_signal_is_not_a_failure():
    """`ProcessLookupError` is an `OSError`, so losing the narrower handler turns
    a benign race into a reported failure and the loop never relaunches."""
    fake = FakeChrome([_chrome(202, LOOP_PROFILE, port=PORT)], ghost_pids={202})
    result = _restart(fake)
    assert result.ok, result.detail
    assert fake.terminated == [202] and fake.launches == [(LOOP_PROFILE, PORT)]


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


def test_nothing_matching_still_launches_when_the_port_is_free():
    """Chrome died outright: refusing here leaves the loop with no browser."""
    fake = FakeChrome([_chrome(101, OPERATOR_PROFILE)])
    result = _restart(fake)
    assert result.ok, result.detail
    assert result.matched_pids == () and fake.terminated == []
    assert fake.launches == [(LOOP_PROFILE, PORT)]


def test_success_requires_a_positive_endpoint_response():
    """Owning the port is the success condition, not a launcher's exit code."""
    fake = FakeChrome([_chrome(202, LOOP_PROFILE, port=PORT)], ready_after_probes=None)
    result = _restart(fake, ready_attempts=3)
    assert not result.ok
    assert "did not answer" in result.detail
    assert result.launched is True, "the launch happened; only the confirmation failed"
    assert len([c for c in fake.calls if c.startswith("ready:")]) == 3, "unbounded polling"


@pytest.mark.parametrize(
    "body, ready",
    [
        (json.dumps({"Browser": "Chrome/1", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/x"}), True),
        (json.dumps({"Browser": "Chrome/1"}), False),
        (json.dumps({"webSocketDebuggerUrl": ""}), False),
        ("<html>proxy says hello</html>", False),
    ],
)
def test_only_a_websocket_url_counts_as_ready(body, ready):
    """A 200 alone would also describe a browser that cannot be driven."""
    assert endpoint_body_is_ready(body) is ready


def test_the_ops_object_is_required():
    """No live default: no test can run `ps`, signal a pid, or start a browser."""
    with pytest.raises(TypeError):
        restart(LOOP_PROFILE, PORT)


def test_a_failure_is_reported_never_raised():
    """A second exception on top of a browser fault would crash the recovery."""

    def boom(_pid):
        raise PermissionError("not owned by this user")

    fake = FakeChrome([_chrome(202, LOOP_PROFILE, port=PORT)])
    result = restart(LOOP_PROFILE, PORT, replace(fake.ops(), terminate=boom), poll_interval=0.0)
    assert not result.ok
    assert "cannot signal pid 202" in result.detail
    assert fake.launches == []


@pytest.fixture
def captured(monkeypatch):
    """`main()` with only `restart` stubbed — the entry point brw-08 will point
    `restart_command` at is otherwise executed by nothing. `ChromeOps.real()` is
    still built for real, so a renamed field there is a TypeError this catches."""
    seen: dict = {}

    def fake_restart(profile, port, ops, **kw):
        seen.update(profile=profile, port=port, ops=ops, kw=kw)
        return seen.get("result", RestartResult(True, "autoloop chrome up on port 9222"))

    monkeypatch.setattr(chrome_restart, "restart", fake_restart)
    return seen


def test_main_reports_success_on_stdout_and_passes_its_flags_through(captured, capsys):
    assert main(["--profile", "/tmp/profile", "--port", "9333"]) == 0
    assert (captured["profile"], captured["port"]) == ("/tmp/profile", 9333)
    out = capsys.readouterr()
    assert "autoloop chrome up" in out.out and out.err == ""


def test_main_exits_one_with_the_diagnosis_on_stderr(captured, capsys, monkeypatch):
    """Both callers surface `result.stderr` on a non-zero exit, so a detail on
    stdout reaches the operator as "restart FAILED:" and nothing else. The env
    vars are the shell helper's, still honoured, and the port arrives as an int."""
    monkeypatch.setenv("AUTOLOOP_CHROME_PROFILE", "/tmp/from-env")
    monkeypatch.setenv("AUTOLOOP_CHROME_PORT", "9444")
    captured["result"] = RestartResult(False, "port 9444 is still held")
    assert main([]) == 1
    out = capsys.readouterr()
    assert "port 9444 is still held" in out.err
    assert "port 9444 is still held" not in out.out
    assert (captured["profile"], captured["port"]) == ("/tmp/from-env", 9444)
