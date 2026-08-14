"""The progress-based stall detector that replaced `audit.agent_timeout_seconds`.

The bound being replaced killed six agents mid-write over two days
(`autoloop/stall.py` lists them) and never once caught a hang, so these tests
are written around that failure rather than around the mechanism:

* `test_an_agent_writing_steadily_past_the_old_timeouts_is_not_killed` IS the
  six-losses case. It is the mutation guard for the whole change: reintroduce
  any elapsed-time bound at 900s or 1800s and it fails.
* the silence, partial-work and ceiling tests cover the other direction — the
  bound really was replaced, not removed.
* the config tests pin that an existing config naming the retired key is
  handled EXPLICITLY — migrated onto the one replacement that kept its meaning,
  announced, and never left to quietly govern the write-capable path it used
  to.

No real process is ever spawned by the supervisor tests and none of them ever
waits: `supervise` is pure over an injected handle, probe, clock and sleep. Two
places do use the real world, both because the thing under test is an honest
observation of it: the probe tests drive a real git repository, and the
once-per-process notice test runs a real subprocess (see its docstring — the
contract is about process-global state, so it cannot be observed from inside a
process other tests have already touched).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from autoloop import cli
from autoloop.audit.agents import AgentResult, AgentSpec, ClaudeCliRunner
from autoloop.config import load_config
from autoloop.contract import Decision, Directive
from autoloop.errors import ConfigError, GitCommandError
from autoloop.git_gateway import GitGateway
from autoloop.implement_executor import ImplementExecutor, implement_agent_runner
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.stall import (
    CEILING,
    COMPLETED,
    STALLED,
    PartialWork,
    ProgressSample,
    StallPolicy,
    StallReport,
    WorkerTreeProbe,
    supervise,
)
from autoloop.tasks import Task

#: The two values `agent_timeout_seconds` actually held in production. Raising
#: 900 -> 1800 changed only the size of each loss.
RETIRED_TIMEOUTS = (900.0, 1800.0)

SPEC = AgentSpec(domain="t1", title="Add widget", prompt="do the thing")


# ---- fakes -------------------------------------------------------------------


class FakeClock:
    """A monotonic clock that only moves when something sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeHandle:
    """A process that exits at a chosen clock time, or never."""

    def __init__(self, clock=None, exit_at=None, exits_on_signal=True, returncode=0):
        self._clock = clock
        self._exit_at = exit_at
        self._exits_on_signal = exits_on_signal
        self._returncode = returncode
        self._rc = None
        self.terminated = 0
        self.killed = 0

    def poll(self):
        if self._rc is None and self._exit_at is not None and self._clock() >= self._exit_at:
            self._rc = self._returncode
        return self._rc

    def terminate(self):
        self.terminated += 1
        if self._exits_on_signal:
            self._rc = -15

    def kill(self):
        self.killed += 1
        self._rc = -9


class WritingProbe:
    """An agent that keeps changing the tree — every sample differs."""

    def __init__(self):
        self.samples = 0

    def sample(self):
        self.samples += 1
        return ProgressSample(
            files=1, marks=(("feature.py", self.samples, self.samples * 10),)
        )

    def partial_work(self):
        return PartialWork(files_changed=1, lines_written=self.samples)


class SilentProbe:
    """A wedged agent: the tree never changes again, but real work is sitting
    in it — the shape every one of the six losses had."""

    def __init__(self, files=16, lines=591):
        self.files = files
        self.lines = lines

    def sample(self):
        return ProgressSample(files=self.files, marks=(("feature.py", 1, 1),))

    def partial_work(self):
        return PartialWork(files_changed=self.files, lines_written=self.lines)


class BlindProbe:
    """A probe that cannot observe the tree at all."""

    def sample(self):
        raise RuntimeError("git status failed in the worker repo")

    def partial_work(self):
        return PartialWork(measured=False, note="the worker repo could not be read")


def make_policy(stall=1800.0, ceiling=14400.0, poll=60.0):
    return StallPolicy(stall_seconds=stall, ceiling_seconds=ceiling, poll_seconds=poll)


# ---- 1. the six losses: steady progress is never killed ----------------------


@pytest.mark.parametrize("retired_timeout", RETIRED_TIMEOUTS)
def test_an_agent_writing_steadily_past_the_old_timeouts_is_not_killed(retired_timeout):
    """THE regression, and the mutation guard for this whole change.

    An agent that keeps writing for 90 minutes — past both values
    `agent_timeout_seconds` ever held — runs to completion untouched. Bound
    this on elapsed time again, at either value, and this fails: the agent
    would be signalled instead of exiting on its own, which is precisely what
    happened six times on 2026-08-05/06.
    """
    clock = FakeClock()
    handle = FakeHandle(clock=clock, exit_at=5400.0)
    probe = WritingProbe()

    result = supervise(handle, probe, make_policy(), clock=clock, sleep=clock.sleep)

    assert result.verdict == COMPLETED
    assert result.returncode == 0
    assert result.report is None
    assert handle.terminated == 0
    assert handle.killed == 0
    # It really did run past the retired bound — the test would pass vacuously
    # if the fake process had finished early.
    assert clock.now > retired_timeout


def test_a_pause_shorter_than_the_stall_window_is_not_a_stall():
    """A long compile or test run leaves the tree untouched for minutes. That
    is not a hang, and the generous default window is what says so."""
    clock = FakeClock()
    handle = FakeHandle(clock=clock, exit_at=2400.0)

    class PausesThenResumes:
        """Silent from 600s to 2000s — 1400s, comfortably inside a 1800s
        window — then writing again."""

        def sample(self):
            phase = 0 if clock.now < 600 else (1 if clock.now < 2000 else 2)
            return ProgressSample(files=1, marks=(("feature.py", phase, phase),))

        def partial_work(self):
            return PartialWork(files_changed=1, lines_written=10)

    result = supervise(
        handle, PausesThenResumes(), make_policy(), clock=clock, sleep=clock.sleep
    )

    assert result.verdict == COMPLETED
    assert handle.terminated == 0


# ---- 2 & 3. silence is a hang, and the report says what was lost -------------


def test_an_agent_silent_past_the_stall_window_is_killed_and_reports_a_stall():
    clock = FakeClock()
    handle = FakeHandle()  # never exits on its own — genuinely wedged

    result = supervise(handle, SilentProbe(), make_policy(), clock=clock, sleep=clock.sleep)

    assert result.verdict == STALLED
    assert handle.terminated == 1
    assert result.report is not None
    assert result.report.silent_seconds >= 1800.0
    # The stall window is what fired, not the backstop.
    assert result.report.elapsed_seconds < 14400.0
    text = result.report.describe()
    assert text.startswith("STALLED:")
    assert "1800s" in text
    assert "absence of filesystem change" in text


def test_the_stall_report_carries_the_partial_work_numbers():
    """A stall with substantial partial work and a stall with none call for
    opposite responses, so the report must let a reviewer tell them apart."""
    clock = FakeClock()
    result = supervise(
        FakeHandle(), SilentProbe(files=16, lines=591), make_policy(),
        clock=clock, sleep=clock.sleep,
    )

    text = result.report.describe()
    assert "16 file(s) changed" in text
    assert "591 line(s) written" in text
    assert "Validation had NOT started" in text


def test_a_stall_that_produced_nothing_reads_differently_from_one_that_did():
    clock = FakeClock()
    result = supervise(
        FakeHandle(), SilentProbe(files=0, lines=0), make_policy(),
        clock=clock, sleep=clock.sleep,
    )

    text = result.report.describe()
    assert "NONE" in text
    assert "no work was lost" in text
    # The two cases must not be reported with the same words.
    assert "file(s) changed, ~" not in text


def test_a_process_that_ignores_sigterm_is_killed():
    clock = FakeClock()
    handle = FakeHandle(exits_on_signal=False)

    result = supervise(handle, SilentProbe(), make_policy(), clock=clock, sleep=clock.sleep)

    assert result.verdict == STALLED
    assert handle.terminated == 1
    assert handle.killed == 1


def test_a_process_that_finishes_while_the_kill_is_being_decided_is_not_a_stall():
    """The window between deciding to kill and signalling is exactly where a
    slow agent finishes — so the handle is polled once more first."""
    clock = FakeClock()

    class FinishesAtTheLastMoment(FakeHandle):
        def __init__(self):
            super().__init__()
            self._polls = 0

        def poll(self):
            self._polls += 1
            # Exits on the extra poll the stall branch makes at t=1800.
            if clock.now >= 1800 and self._polls > 31:
                self._rc = 0
            return self._rc

    handle = FinishesAtTheLastMoment()
    result = supervise(handle, SilentProbe(), make_policy(), clock=clock, sleep=clock.sleep)

    assert result.verdict == COMPLETED
    assert handle.terminated == 0


# ---- 4. the backstop still terminates a pathological run ---------------------


def test_the_absolute_ceiling_still_terminates_a_pathological_run():
    """Progress forever, exit never. The stall detector correctly never fires;
    the ceiling is what stops the loop from being wedged with it."""
    clock = FakeClock()
    handle = FakeHandle()

    result = supervise(handle, WritingProbe(), make_policy(), clock=clock, sleep=clock.sleep)

    assert result.verdict == CEILING
    assert handle.terminated == 1
    assert result.report.elapsed_seconds >= 14400.0
    text = result.report.describe()
    assert "ABSOLUTE CEILING FIRED" in text
    # It must not read as a routine timeout — it is a finding.
    assert "finding" in text
    assert "NOT" in text


def test_a_probe_that_cannot_observe_never_triggers_a_stall_kill():
    """"I cannot see the tree" is not "the tree is not changing". Treating the
    first as the second is the mistake this module exists to correct, so only
    the ceiling may end a run nobody can observe — and the report says the
    silence was unobserved."""
    clock = FakeClock()
    handle = FakeHandle()

    result = supervise(handle, BlindProbe(), make_policy(), clock=clock, sleep=clock.sleep)

    assert result.verdict == CEILING
    assert result.report.probe_blind
    assert "may be unobserved" in result.report.describe()
    assert "UNKNOWN" in result.report.partial.describe()


# ---- 5. the retired config key is handled explicitly, not ignored ------------


def _write_config(tmp_path: Path, audit_lines: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        "[browser]\n"
        'conversation_url = "https://chatgpt.com/c/abc"\n'
        "\n"
        "[paths]\n"
        'state_dir = ".autoloop"\n'
        f'workers_root = "{tmp_path / "workers"}"\n'
        "\n"
        "[audit]\n"
        'agent_command = ["claude"]\n'
        f"{audit_lines}\n",
        encoding="utf-8",
    )
    return path


def test_an_old_config_naming_agent_timeout_seconds_is_migrated_explicitly(tmp_path):
    """Handled explicitly, which is the requirement — not ignored, and not
    silently left to mean what it used to mean.

    The old key meant "elapsed bound for a subagent". That meaning survives on
    exactly one path (read-only audit agents, which have no progress to watch),
    so the value lands there and governs it exactly as before. It does NOT
    reach the write-capable agent, because bounding that by elapsed time is the
    failure being corrected — and the notice says so rather than leaving the
    operator to discover it.
    """
    config = load_config(_write_config(tmp_path, "agent_timeout_seconds = 1200.0"))

    # Its meaning is preserved where the meaning still applies...
    assert config.audit.audit_agent_timeout_seconds == 1200.0
    # ...and it does NOT become an elapsed bound on the write-capable path,
    # which now gets the stall defaults.
    assert config.audit.agent_stall_seconds == 1800.0
    assert config.audit.agent_ceiling_seconds == 14400.0
    # The retired name is consumed, so nothing can read it back.
    assert not hasattr(config.audit, "agent_timeout_seconds")

    (notice,) = config.migration_notices
    assert "agent_timeout_seconds" in notice
    assert "RETIRED" in notice
    assert "1200.0" in notice
    # It names all three replacements and what each one means.
    assert "audit_agent_timeout_seconds" in notice
    assert "agent_stall_seconds" in notice
    assert "agent_ceiling_seconds" in notice
    # A migration message, not the generic unknown-key one.
    assert "unknown keys" not in notice


def test_the_new_key_wins_when_a_config_names_both(tmp_path):
    """Someone who wrote both has already chosen, and the current name is the
    choice. Letting the retired key override the key that replaced it would be
    the one outcome nobody could have intended."""
    config = load_config(
        _write_config(
            tmp_path,
            "agent_timeout_seconds = 1200.0\naudit_agent_timeout_seconds = 300.0",
        )
    )

    assert config.audit.audit_agent_timeout_seconds == 300.0
    (notice,) = config.migration_notices
    assert "IGNORED" in notice
    assert "300.0" in notice  # the winner is named
    assert "1200.0" in notice  # so is the value that lost


def test_a_config_without_the_retired_key_produces_no_notice(tmp_path):
    """The notice must be evidence the key was seen, not decoration printed on
    every start — otherwise it says nothing when it appears."""
    assert load_config(_write_config(tmp_path, "")).migration_notices == ()


def test_a_junk_value_for_the_retired_key_is_still_refused(tmp_path):
    """Migrating a value onto a live setting means validating it like one. A
    notice announcing that `0` now bounds the audit agents would be worse than
    the error it replaced."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write_config(tmp_path, "agent_timeout_seconds = 0"))

    assert "agent_timeout_seconds" in str(excinfo.value)
    assert "positive number" in str(excinfo.value)


def test_loading_a_config_never_writes_to_stderr_itself(tmp_path, capsys):
    """`load_config` is PURE — notices come back as data and no global records
    that they were produced.

    This is what makes the notice content testable in any order: were the
    loader to print (and to remember printing), the first test in the file to
    load a legacy config would consume the one emission and every later
    assertion about it would depend on test ordering.
    """
    first = load_config(_write_config(tmp_path, "agent_timeout_seconds = 1200.0"))
    second = load_config(_write_config(tmp_path, "agent_timeout_seconds = 1200.0"))

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""
    # Same input, same output — no "already warned" state anywhere.
    assert first.migration_notices == second.migration_notices != ()


def test_the_cli_prints_the_migration_notice_on_stderr(tmp_path, capsys, monkeypatch):
    """Routing: stderr, never stdout. `status` / `tasks` / `next-task` have
    parseable stdout, and a notice mixed into it would corrupt a pipe."""
    # Only the process-global emission ledger is reset, and monkeypatch puts it
    # back afterwards. Production semantics are untouched.
    monkeypatch.setattr(cli, "_EMITTED_MIGRATION_NOTICES", set())

    config = cli.load_config(_write_config(tmp_path, "agent_timeout_seconds = 1200.0"))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RETIRED" in captured.err
    assert config.audit.audit_agent_timeout_seconds == 1200.0


def test_the_cli_prints_the_migration_notice_on_stderr_once(tmp_path):
    """The once-PER-PROCESS contract, tested in a process of its own.

    It cannot be tested in-process: the ledger is module-global by design (the
    loop reads its config on every command, and `run --continuous` is one
    long-lived process, so a notice repeated each round is one an operator
    learns to scroll past). Any earlier test that loads a legacy config through
    the CLI would consume the single emission and leave this one asserting
    against an empty stream — which is exactly how it failed before. A
    subprocess makes "this process has not printed it yet" true by
    construction instead of by test ordering.
    """
    path = _write_config(tmp_path, "agent_timeout_seconds = 1200.0")
    repo_root = Path(__file__).resolve().parents[2]
    program = (
        "import sys\n"
        "from autoloop.cli import load_config\n"
        "for _ in range(3):\n"
        "    load_config(sys.argv[1])\n"
        "print('loaded 3 times', file=sys.stdout)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(repo_root)}

    proc = subprocess.run(
        [sys.executable, "-c", program, str(path)],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "loaded 3 times" in proc.stdout
    # Three loads, one notice.
    assert proc.stderr.count("was RETIRED on 2026-08-14") == 1


def test_the_new_settings_load_and_default_generously(tmp_path):
    config = load_config(_write_config(tmp_path, ""))

    assert config.audit.agent_stall_seconds == 1800.0
    assert config.audit.agent_ceiling_seconds == 14400.0
    # The read-only audit path keeps the old bound's meaning AND its value.
    assert config.audit.audit_agent_timeout_seconds == 900.0
    assert not hasattr(config.audit, "agent_timeout_seconds")


def test_a_stall_window_at_or_above_the_ceiling_is_refused(tmp_path):
    """It would read as configured while being unreachable — the ceiling would
    always fire first and the progress detector would never run."""
    path = _write_config(
        tmp_path, "agent_stall_seconds = 3600.0\nagent_ceiling_seconds = 3600.0"
    )

    with pytest.raises(ConfigError) as excinfo:
        load_config(path)

    assert "strictly below" in str(excinfo.value)


def test_a_non_positive_bound_is_refused(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write_config(tmp_path, "agent_stall_seconds = 0.0"))

    assert "positive number" in str(excinfo.value)


# ---- the probe observes the worker repo, not the agent ----------------------


def run_git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def worker_repo(tmp_path):
    root = tmp_path / "worker"
    root.mkdir(parents=True, exist_ok=True)
    run_git(root, "init", "-q", "-b", "autoloop/t1")
    run_git(root, "config", "user.email", "t@e.c")
    run_git(root, "config", "user.name", "T")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("hi\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def main_repo(tmp_path):
    root = tmp_path / "main"
    root.mkdir(parents=True, exist_ok=True)
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.email", "t@e.c")
    run_git(root, "config", "user.name", "T")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("hi\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root


def make_probe(repo: Path) -> WorkerTreeProbe:
    return WorkerTreeProbe(GitGateway(repo, PolicyEngine(PolicyConfig())), repo)


def test_the_probe_sees_one_file_growing_even_though_the_path_set_is_unchanged(worker_repo):
    """Repeated Edits to the SAME file leave `git status`'s path set identical
    — which is why the per-path stat is load-bearing rather than decoration.
    Without it, an agent rewriting one large file would look stalled."""
    probe = make_probe(worker_repo)
    (worker_repo / "feature.py").write_text("x = 1\n")
    first = probe.sample()
    (worker_repo / "feature.py").write_text("x = 1\ny = 2\n")
    second = probe.sample()

    assert first != second
    assert [path for path, _m, _s in first.marks] == [path for path, _m, _s in second.marks]


def test_an_untouched_tree_samples_identically(worker_repo):
    probe = make_probe(worker_repo)
    (worker_repo / "feature.py").write_text("x = 1\n")
    assert probe.sample() == probe.sample()


def test_partial_work_counts_new_files_and_tracked_insertions(worker_repo):
    (worker_repo / "README.md").write_text("hi\nthere\nagain\n")  # +2 insertions
    (worker_repo / "pkg").mkdir()
    (worker_repo / "pkg" / "new.py").write_text("a\nb\nc\n")  # a whole new 3-line file

    partial = make_probe(worker_repo).partial_work()

    assert partial.measured
    assert partial.files_changed == 2
    assert partial.lines_written == 5
    assert "2 file(s) changed" in partial.describe()


def test_a_new_binary_file_is_counted_as_a_file_but_not_as_lines(worker_repo):
    (worker_repo / "blob.bin").write_bytes(b"\x00\x01\x02\x00")

    partial = make_probe(worker_repo).partial_work()

    assert partial.files_changed == 1
    assert partial.lines_written == 0
    assert "not line-counted" in partial.note


def test_a_clean_worker_repo_measures_zero_rather_than_unknown(worker_repo):
    partial = make_probe(worker_repo).partial_work()

    assert partial.measured
    assert partial.files_changed == 0
    assert "NONE" in partial.describe()


def test_a_failed_tracked_diff_never_reads_as_a_measured_zero(worker_repo):
    """`HEAD` always resolves in a real worker repo (`WorkerRepoManager.create`
    ends with `git checkout -B <branch> FETCH_HEAD`), but if the diff ever
    fails the count silently loses every tracked edit — and "16 files changed,
    ~0 lines written" for a run that wrote 591 would be a wrong number
    presented as a measured one, which is worse than the timeout this
    replaced. The shortfall has to travel with the number."""
    (worker_repo / "new.py").write_text("a\nb\n")

    class DiffFails:
        repo_root = worker_repo

        def dirty_entries_all(self):
            return [(" M", "README.md"), ("??", "new.py")]

        def worktree_diff_stat(self):
            raise GitCommandError("git diff HEAD --stat failed (rc=128)")

    partial = WorkerTreeProbe(DiffFails(), worker_repo).partial_work()

    assert partial.measured  # the file COUNT is still real
    assert partial.files_changed == 2
    assert partial.lines_written == 2  # the new file only
    text = partial.describe()
    assert "INCOMPLETE" in text
    assert "1 already-tracked file(s)" in text


def test_an_unreadable_repo_is_unknown_not_zero(tmp_path):
    """A measured zero means "the hang produced nothing"; an unreadable repo
    means "we do not know". Collapsing the two would let a probe failure read
    as proof that no work was lost."""
    not_a_repo = tmp_path / "nope"
    not_a_repo.mkdir()

    partial = make_probe(not_a_repo).partial_work()

    assert not partial.measured
    assert "UNKNOWN" in partial.describe()


# ---- the runner: supervised end to end, no real process ---------------------


def test_the_runner_reports_a_stall_instead_of_a_timeout(tmp_path):
    clock = FakeClock()
    handle = FakeHandle()
    spawned = []

    def fake_spawn(argv, *, cwd, env, stdout, stderr):
        spawned.append((argv, cwd))
        stdout.write(b'{"result": "I was half way through"}')
        return handle

    runner = ClaudeCliRunner(
        tmp_path,
        progress_probe=SilentProbe(files=15, lines=532),
        stall_policy=make_policy(stall=60.0, ceiling=600.0, poll=10.0),
        spawn=fake_spawn,
        clock=clock,
        sleep=clock.sleep,
    )

    result = runner.run(SPEC)

    assert not result.ok
    assert result.stall is not None
    assert result.stall.verdict == STALLED
    assert "15 file(s) changed" in result.error
    assert "532 line(s) written" in result.error
    # Partial output from a killed run is kept — often the only account of
    # what the agent was doing when it wedged.
    assert result.raw_text == "I was half way through"
    assert spawned and spawned[0][1] == str(tmp_path)


def test_the_supervised_runner_lets_a_long_healthy_run_finish(tmp_path):
    clock = FakeClock()
    handle = FakeHandle(clock=clock, exit_at=5400.0)

    def fake_spawn(argv, *, cwd, env, stdout, stderr):
        stdout.write(b'{"result": "done"}')
        return handle

    runner = ClaudeCliRunner(
        tmp_path,
        # The retired bound, passed deliberately: on the supervised path it is
        # unused, and a run that outlives it by an hour proves that.
        timeout_seconds=900.0,
        progress_probe=WritingProbe(),
        stall_policy=make_policy(),
        spawn=fake_spawn,
        clock=clock,
        sleep=clock.sleep,
    )

    result = runner.run(SPEC)

    assert result.ok
    assert result.stall is None
    assert result.raw_text == "done"
    assert handle.terminated == 0
    assert clock.now > 900.0


def test_the_supervised_spawn_still_strips_the_validation_credentials(tmp_path, monkeypatch):
    """The supervised path is a SECOND way a write-capable subagent gets
    launched, and the environment strip is a security control
    (`docs/SECURITY.md` S27) that `test_validation_env.py` pins on the
    `subprocess.run` path. A control that only one of two spawn paths applies
    is not a control, so it is pinned here too."""
    monkeypatch.setenv("DB_PASSWORD", "secret-value-for-DB_PASSWORD")
    monkeypatch.setenv("AUTOLOOP_VALIDATION_ENV_FILE", "/home/me/validation.env")
    seen = {}

    def fake_spawn(argv, *, cwd, env, stdout, stderr):
        seen["env"] = env
        stdout.write(b'{"result": "done"}')
        return FakeHandle(clock=clock, exit_at=0.0)

    clock = FakeClock()
    ClaudeCliRunner(
        tmp_path,
        progress_probe=WritingProbe(),
        stall_policy=make_policy(),
        spawn=fake_spawn,
        clock=clock,
        sleep=clock.sleep,
    ).run(SPEC)

    assert "DB_PASSWORD" not in seen["env"]
    assert "AUTOLOOP_VALIDATION_ENV_FILE" not in seen["env"]
    assert seen["env"].get("PATH")


def test_a_runner_without_a_probe_keeps_the_old_elapsed_bound(tmp_path):
    """The read-only audit path. No progress signal exists there, so elapsed
    time stays the bound — and a timeout costs a re-run, never lost work."""
    passed_timeouts = []

    def stub(argv, **kwargs):
        passed_timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(cmd=argv, timeout=900.0)

    result = ClaudeCliRunner(tmp_path, timeout_seconds=900.0, runner=stub).run(SPEC)

    assert passed_timeouts == [900.0]
    assert not result.ok
    assert "timed out" in result.error
    assert result.stall is None


def test_the_write_capable_runner_gets_a_probe_only_when_given_a_policy(tmp_path):
    """`policy=` is the single switch that turns the stall detector on, and
    omitting it is what keeps every stubbed-runner test on the old path."""
    unsupervised = implement_agent_runner(tmp_path, runner=lambda *a, **k: None)
    supervised = implement_agent_runner(tmp_path, policy=PolicyEngine(PolicyConfig()))

    assert unsupervised._progress_probe is None
    assert isinstance(supervised._progress_probe, WorkerTreeProbe)


# ---- the executor: the reviewer sees the numbers ----------------------------


def build_executor(main_repo, worker_repo, factory):
    policy = PolicyEngine(PolicyConfig())
    return ImplementExecutor(
        git=GitGateway(main_repo, policy),
        agent_runner=None,
        validation_commands=(),
        worker_repo_root_for=lambda task_id: worker_repo,
        policy=policy,
        agent_runner_factory=factory,
    )


def stalling_agent_factory(report):
    class StallingAgent:
        def __init__(self, root):
            self.root = root

        def run(self, spec):
            # Real partial work, left in the worker repo exactly as a killed
            # agent would leave it.
            (self.root / "half_written.py").write_text("a\nb\nc\n")
            return AgentResult(
                domain=spec.domain,
                raw_text="",
                returncode=-15,
                duration_seconds=report.elapsed_seconds,
                command=("claude",),
                error=report.describe(),
                stall=report,
            )

    return lambda root: StallingAgent(root)


def test_the_executor_surfaces_the_stall_and_what_was_left_behind(main_repo, worker_repo):
    report = StallReport(
        verdict=STALLED,
        elapsed_seconds=4210.0,
        silent_seconds=1801.0,
        stall_seconds=1800.0,
        ceiling_seconds=14400.0,
        partial=PartialWork(files_changed=1, lines_written=3),
    )
    executor = build_executor(main_repo, worker_repo, stalling_agent_factory(report))

    outcome = executor.execute(
        Directive(decision=Decision.IMPLEMENT, reason="r", task_id="t1"),
        Task(id="t1", title="Add widget", description="Implement the widget."),
    )

    assert outcome.status == "error"
    assert "STALLED" in outcome.summary
    assert "1 file(s) changed" in outcome.summary
    assert "Validation had NOT started" in outcome.summary
    assert outcome.validation == "not run"
    # Read from the worker repo's own git state, never from the agent.
    assert outcome.changed_paths == ("half_written.py",)
    # The stall report already states its own numbers; the executor must not
    # print a second, separately-measured set alongside them.
    assert outcome.summary.count("file(s) changed") == 1


def test_an_ordinary_agent_failure_now_reports_what_it_left_behind(main_repo, worker_repo):
    """Not only stalls. "The agent failed" alone is unactionable: a crash that
    produced 600 lines and one that produced nothing need opposite responses."""

    class FailingAgent:
        def __init__(self, root):
            self.root = root

        def run(self, spec):
            (self.root / "half_written.py").write_text("a\nb\n")
            return AgentResult(
                domain=spec.domain, raw_text="", returncode=1, duration_seconds=1.0,
                command=("claude",), error="agent exploded",
            )

    executor = build_executor(main_repo, worker_repo, lambda root: FailingAgent(root))

    outcome = executor.execute(
        Directive(decision=Decision.IMPLEMENT, reason="r", task_id="t1"),
        Task(id="t1", title="Add widget", description="Implement the widget."),
    )

    assert outcome.status == "error"
    assert "agent exploded" in outcome.summary
    assert "Partial work left in the worker repository" in outcome.summary
    assert "1 file(s) changed" in outcome.summary
    assert "~2 line(s) written" in outcome.summary
    assert outcome.changed_paths == ("half_written.py",)
