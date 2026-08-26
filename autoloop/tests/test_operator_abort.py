"""`abort`: stopping a round within seconds, discarding only the step in flight.

THE ONE CLAIM these tests exist to grade, stated as the task stated it: an
operator can stop the loop within SECONDS at any point in a round; the agent
call in flight is killed along with anything it spawned; the worker repo's
uncommitted work is preserved and reported; mainline is untouched; and the task
is resumable — it returns to the queue rather than being consumed.

WHY IT WAS NEEDED. `pause` writes a flag that `run()` reads at the TOP OF EACH
STEP, and a step is an agent call bounded by SILENCE rather than by elapsed time
(4-hour absolute ceiling). Measured across every pause-and-edit job run in one
night, 2026-08-25: mean 39 minutes to land a pause, worst 60 — and that one was
abandoned, so the work never ran at all. The loop was easiest to interrupt when
idle and hardest when busy, exactly inverted from when an operator needs it.

WHAT IS DELIBERATELY NOT CLAIMED, and has a test saying so rather than a
sentence hoping so:

  * `pause` is unchanged — it still means "stop at the next boundary", which is
    the right default for an unattended job;
  * in the phases that owe a review packet (`submitting`, `awaiting` and their
    neighbours) nothing is killed: the loop stops BETWEEN steps exactly as
    `pause` already could, and the pending request and its phase survive;
  * a round that already committed a candidate finishes and is honoured at the
    next boundary — the kill is bounded by the commit, which is what makes
    "mainline is untouched" structural rather than argued.

Self-contained per this codebase's convention (see `test_m1_hardening.py`) —
real git repos, real subprocesses where the claim is about real subprocesses,
and no fixtures imported from other test modules.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from autoloop import cli
from autoloop.audit.agents import AgentResult
from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.implement_executor import (
    ABORT_RETURNCODE,
    AbortableProcessHandle,
    ImplementExecutor,
    abort_aware_command_runner,
    abort_aware_spawn,
    abort_flag_set,
    implement_agent_runner,
    killable_run,
)
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import ABORT_STOP_KIND, ABORTED, Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.stall import COMPLETED, StallPolicy, spawn_supervised, supervise
from autoloop.state import (
    EXECUTION_ABORTED,
    LoopState,
    PendingRequest,
    Phase,
    StateStore,
    abort_flag_file,
    abort_requested,
)
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore, mutation_ledger_for
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import (
    ATTEMPT_PENDING,
    ATTEMPT_PENDING_FAULT,
    ATTEMPT_TASK,
    IntentStore,
    TaskExecution,
    TaskExecutionStore,
    format_attempt,
    refund_attempt,
    split_attempt,
)

URL = "https://chatgpt.com/c/operator-abort-test"

#: How long a test waits for a real process to die. Generous relative to the
#: thing being measured (a SIGTERM to a python process, which is milliseconds)
#: and short enough that a broken kill fails the suite rather than hanging it —
#: bounded deliberately BELOW `LINGER_SECONDS`, so a stand-in that nothing
#: killed is still running when the assertion about it fails, and the test says
#: "it survived" rather than "it had already exited by itself".
KILL_DEADLINE_SECONDS = 8.0


# =============================================================================
# shared helpers
# =============================================================================


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def real_repo(tmp_path, name="repo") -> Path:
    repo_root = tmp_path / name
    repo_root.mkdir()
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


def wait_until(predicate, *, deadline=KILL_DEADLINE_SECONDS, step=0.05):
    """Poll `predicate` until it is true or the deadline passes. Returns it."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def alive(pid: int) -> bool:
    """Is `pid` still a process we can signal?

    `os.kill(pid, 0)` is the standard existence probe: it sends nothing and
    raises `ProcessLookupError` for a pid that is gone. `PermissionError` means
    the pid exists and belongs to somebody else — impossible for a process this
    test started, and counted as alive rather than silently as dead, because
    reading a probe failure as "the kill worked" is exactly the fail-open this
    file is grading.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not reachable for our own child
        return True
    return True


#: How long a spawned stand-in lives if NOTHING kills it. Long enough that it is
#: still running while the test aborts it, short enough that a kill which fails
#: costs the suite seconds rather than minutes — a test asserting that processes
#: die must not be the thing that hangs the run when they do not.
LINGER_SECONDS = 10

#: A process that spawns a child of its own and then sleeps. The `claude` CLI's
#: shape, and the reason `stall.ProcessGroupHandle` exists at all: signalling
#: only the parent leaves the child writing into a worker repo nobody owns.
#: Writes both pids to a marker file so the test can prove what happened to
#: EACH, rather than asserting about the one it happens to hold a handle on.
SPAWNS_A_CHILD = f"""
import os, subprocess, sys, time
marker = sys.argv[1]
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep({LINGER_SECONDS})"]
)
tmp = marker + ".tmp"
with open(tmp, "w") as handle:
    handle.write("%d %d\\n" % (os.getpid(), child.pid))
os.replace(tmp, marker)
time.sleep({LINGER_SECONDS})
"""


def read_pids(marker: Path) -> tuple[int, int]:
    parent, child = marker.read_text(encoding="utf-8").split()
    return int(parent), int(child)


def reap(*pids: int) -> None:
    """Best-effort cleanup so a FAILING test never leaves a sleeper behind.

    Every real-process test here calls this in a `finally`. It is not part of
    any claim — the claims are the assertions that these pids are already gone —
    it exists so a regression costs one failed test rather than a suite that
    drags every worker behind it.
    """
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


# =============================================================================
# 1. THE KILL: the agent in flight, and everything it spawned
# =============================================================================


def test_an_abort_kills_the_agent_and_every_process_it_spawned(tmp_path):
    """THE constraint the task named first, proved against real processes.

    "Killing only the agent leaves four workers running against a worker repo
    nobody owns. Prove the subprocesses die with it." So this spawns a process
    that spawns a process, aborts, and asserts BOTH are gone — the grandchild by
    its own pid, not by inference from the parent's exit.
    """
    marker = tmp_path / "pids.txt"
    abort_file = tmp_path / "ABORT"
    pids: tuple[int, ...] = ()
    out = open(tmp_path / "out.log", "wb")
    err = open(tmp_path / "err.log", "wb")
    try:
        handle = abort_aware_spawn(abort_file)(
            [sys.executable, "-c", SPAWNS_A_CHILD, str(marker)],
            cwd=str(tmp_path),
            env=os.environ.copy(),
            stdout=out,
            stderr=err,
        )
        assert wait_until(marker.exists), "the child never reported its pids"
        agent_pid, spawned_pid = pids = read_pids(marker)
        assert alive(agent_pid) and alive(spawned_pid), "both should be up before the abort"

        assert handle.poll() is None, "nothing is killed before the flag exists"

        abort_file.touch()
        returncode = handle.poll()

        assert returncode is not None, "the flag must end the run, not merely be noticed"
        assert handle.aborted is True
        assert wait_until(lambda: not alive(agent_pid)), "the agent survived the abort"
        assert wait_until(
            lambda: not alive(spawned_pid)
        ), "the process the agent spawned survived the abort — the whole point"
    finally:
        reap(*pids)
        out.close()
        err.close()


def test_a_process_that_finishes_on_its_own_is_never_reported_as_aborted(tmp_path):
    """The ordering `stall.supervise` documents, applied one level down: a
    process that has already exited is COMPLETE, never posthumously killed —
    even once the flag is set. `poll()` asks the process FIRST and only then
    asks the flag, so an abort arriving after a healthy exit reports the exit."""
    abort_file = tmp_path / "ABORT"
    out = open(tmp_path / "out.log", "wb")
    err = open(tmp_path / "err.log", "wb")
    try:
        handle = abort_aware_spawn(abort_file)(
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            cwd=str(tmp_path),
            env=os.environ.copy(),
            stdout=out,
            stderr=err,
        )
        assert wait_until(lambda: handle.poll() is not None), "the process never exited"

        abort_file.touch()

        assert handle.poll() == 7, "its own exit code, not the abort's"
        assert handle.aborted is False
    finally:
        out.close()
        err.close()


class FakeProcess:
    """A `stall.ProcessHandle` with no process behind it, so the supervisor's
    behaviour can be exercised with no real waiting."""

    def __init__(self, exit_after: int | None = None):
        self.polls = 0
        self.terminated = 0
        self.killed = 0
        self._exit_after = exit_after
        self._returncode = None

    def poll(self):
        self.polls += 1
        if self._returncode is not None:
            return self._returncode
        if self._exit_after is not None and self.polls >= self._exit_after:
            self._returncode = 0
        return self._returncode

    def terminate(self):
        self.terminated += 1
        self._returncode = -15

    def kill(self):  # pragma: no cover - SIGTERM always lands on the fake
        self.killed += 1
        self._returncode = -9


def test_the_supervisor_sees_an_abort_as_an_ordinary_exit_not_a_stall(tmp_path):
    """The abort reports through the ORDINARY channel, and that is deliberate.

    A `stall.StallReport` says "nothing changed for N seconds, so this agent was
    wedged" — a false statement about a healthy agent an operator stopped. So
    `supervise` sees a returncode and answers COMPLETED, learning nothing about
    the kill; what the round was ABORTED is established by the flag itself,
    which the executor and the orchestrator each read for themselves.
    """
    abort_file = tmp_path / "ABORT"
    abort_file.touch()
    inner = FakeProcess()
    handle = AbortableProcessHandle(inner, abort_file, grace_seconds=1.0, sleep=lambda _: None)

    class NoProgress:
        """Always-changing samples, so the STALL detector can never be what ends
        this run — only the abort or the ceiling can, which is what makes the
        verdict below mean something."""

        def sample(self):
            return object()

        def partial_work(self):  # pragma: no cover - only a kill path reads this
            raise AssertionError("no stall report is produced for an abort")

    # An ADVANCING clock, deliberately: a frozen one would make the absolute
    # ceiling unreachable too, so a regression in which the abort never fires
    # would spin here forever instead of failing.
    ticks = iter(range(0, 1000))
    supervision = supervise(
        handle,
        NoProgress(),
        StallPolicy(stall_seconds=10.0, ceiling_seconds=20.0, poll_seconds=1.0),
        clock=lambda: float(next(ticks)),
        sleep=lambda _: None,
    )

    assert supervision.verdict == COMPLETED
    assert supervision.report is None, "an abort is not a stall and must not report as one"
    assert inner.terminated == 1, "the group was signalled"


def test_the_kill_escalates_to_sigkill_and_always_answers(tmp_path):
    """A process that ignores SIGTERM is SIGKILLed, and the answer is never
    `None`: a `None` here would send `supervise` round its loop to re-signal a
    process that is already dead, forever."""

    class IgnoresSigterm:
        def __init__(self):
            self.terminated = 0
            self.killed = 0

        def poll(self):
            return None

        def terminate(self):
            self.terminated += 1

        def kill(self):
            self.killed += 1

    abort_file = tmp_path / "ABORT"
    abort_file.touch()
    inner = IgnoresSigterm()
    handle = AbortableProcessHandle(inner, abort_file, grace_seconds=0.5, sleep=lambda _: None)

    returncode = handle.poll()

    assert returncode == ABORT_RETURNCODE
    assert inner.terminated == 1 and inner.killed == 1
    # Once. A second poll must not re-signal a process this handle already
    # killed — `aborted` is a positive record of having done it.
    handle.poll()
    assert inner.terminated == 1 and inner.killed == 1


def test_without_an_abort_file_the_spawn_is_the_untouched_original():
    """No abort capability wired means no wrapper at all — the honest
    representation of "this construction cannot be aborted", and what keeps
    every existing test's spawn seam behaving exactly as it did."""
    sentinel = object()
    assert abort_aware_spawn(None, lambda *a, **k: sentinel)(
        [], cwd=None, env=None, stdout=None, stderr=None
    ) is sentinel
    assert abort_aware_spawn(None) is spawn_supervised


# =============================================================================
# 2. THE KILL, second process group: the validation subprocess
# =============================================================================


def test_the_validation_runner_kills_its_whole_process_group(tmp_path):
    """Since impl-02 the agent runs the suite MID-ROUND, so an abort can land
    while `pytest -n 4` is live — in the LOOP's process group, not the agent's.
    Killing only the agent would leave the workers running; this proves the
    runner takes its own group down with it, grandchild included."""
    marker = tmp_path / "pids.txt"
    abort_file = tmp_path / "ABORT"
    result = {}
    pids: tuple[int, ...] = ()

    def run():
        result["proc"] = killable_run(
            [sys.executable, "-c", SPAWNS_A_CHILD, str(marker)],
            cwd=tmp_path,
            text=True,
            env=os.environ.copy(),
            abort_file=abort_file,
            poll_seconds=0.05,
            grace_seconds=2.0,
        )

    # Daemon, so a regression that stopped the runner returning cannot hold the
    # interpreter open behind it — the assertions below still fail either way.
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert wait_until(marker.exists), "the validation command never started"
        parent_pid, child_pid = pids = read_pids(marker)

        abort_file.touch()

        thread.join(timeout=KILL_DEADLINE_SECONDS)
        assert not thread.is_alive(), "the runner did not return after the abort"
        assert result["proc"].returncode != 0
        assert wait_until(lambda: not alive(parent_pid))
        assert wait_until(
            lambda: not alive(child_pid)
        ), "a worker the validation command spawned outlived the abort"
    finally:
        reap(*pids)


def test_commands_after_an_abort_never_launch_and_never_read_as_a_pass(tmp_path):
    """A list of commands, aborted at command one. The rest must not run — and
    what they report must be a FAILURE, because a validation summary that said
    green about commands nobody ran is the fail-open this whole file grades."""
    abort_file = tmp_path / "ABORT"
    launched = []

    def base(argv, **kwargs):
        launched.append(tuple(argv))

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return Proc()

    runner = abort_aware_command_runner(base, abort_file)
    first = runner(["ruff", "check", "."], cwd=tmp_path)
    abort_file.touch()
    second = runner(["pytest"], cwd=tmp_path)

    assert first.returncode == 0 and launched == [("ruff", "check", ".")]
    assert second.returncode == ABORT_RETURNCODE, "not a pass"
    assert launched == [("ruff", "check", ".")], "the second command must not launch"
    assert "aborted" in second.stderr.lower()


def test_an_unwired_command_runner_is_returned_exactly_as_given():
    base = object()
    assert abort_aware_command_runner(base, None) is base
    assert abort_aware_command_runner(None, None) is subprocess.run


@pytest.mark.parametrize(
    "code,expected",
    [("import sys; sys.exit(0)", 0), ("import sys; sys.exit(3)", 3)],
)
def test_killable_run_reports_exit_codes_like_subprocess_run(tmp_path, code, expected):
    proc = killable_run([sys.executable, "-c", code], cwd=tmp_path, text=True)
    assert proc.returncode == expected


def test_killable_run_captures_both_streams_as_text(tmp_path):
    proc = killable_run(
        [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr)"],
        cwd=tmp_path,
        text=True,
    )
    assert "out" in proc.stdout and "err" in proc.stderr


def test_killable_run_raises_the_same_two_exceptions_run_validation_commands_catches(
    tmp_path,
):
    """`validation._run_one_command` catches exactly `TimeoutExpired` and
    `FileNotFoundError` and reports each as a failure rather than a raise. A
    drop-in that raised something else would turn a reportable failure into an
    unhandled exception out of the round."""
    with pytest.raises(subprocess.TimeoutExpired):
        killable_run(
            [sys.executable, "-c", f"import time; time.sleep({LINGER_SECONDS})"],
            cwd=tmp_path,
            text=True,
            timeout=0.5,
            poll_seconds=0.05,
            grace_seconds=2.0,
        )
    with pytest.raises(FileNotFoundError):
        killable_run(["autoloop-no-such-binary-xyz"], cwd=tmp_path, text=True)


def test_a_timed_out_command_is_dead_before_the_exception_is_raised(tmp_path):
    """`subprocess.run`'s own timeout path kills the direct child only, leaving
    xdist workers orphaned. Here the group goes first and the raise comes
    after, so a caller's `except TimeoutExpired` never runs while workers are
    still writing into the worker repo."""
    marker = tmp_path / "pids.txt"
    pids: tuple[int, ...] = ()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            killable_run(
                [sys.executable, "-c", SPAWNS_A_CHILD, str(marker)],
                cwd=tmp_path,
                text=True,
                env=os.environ.copy(),
                timeout=1.5,
                poll_seconds=0.05,
                grace_seconds=2.0,
            )
        parent_pid, child_pid = pids = read_pids(marker)
        assert wait_until(lambda: not alive(parent_pid))
        assert wait_until(lambda: not alive(child_pid))
    finally:
        reap(*pids)


# =============================================================================
# 3. THE EXECUTOR: what an aborted round reports
# =============================================================================


class WritingAgent:
    """An agent that writes real files into its worker repo, then reports
    however the test asked. Real writes, because the whole point of the report
    under test is that it is MEASURED from the worker repo rather than taken
    from the agent's word."""

    def __init__(self, worker_repo: Path, files, *, before_returning=None, ok=True):
        self.worker_repo = Path(worker_repo)
        self.files = dict(files)
        self.before_returning = before_returning
        self.ok = ok
        self.runs = 0

    def run(self, spec):
        self.runs += 1
        for rel, body in self.files.items():
            target = self.worker_repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        if self.before_returning is not None:
            self.before_returning()
        return AgentResult(
            domain=spec.domain,
            raw_text="I wrote some things.",
            returncode=0 if self.ok else -99,
            duration_seconds=0.1,
            command=("claude",),
            error="" if self.ok else "killed",
        )


def _green():
    """A passing `CompletedProcess`-shaped result, for a round that is meant to
    reach validation. Real binaries are deliberately not invoked here — what is
    under test is the executor's control flow, not ruff's."""

    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def executor_with(worker_repo, agent, abort_file, *, validation=(("ruff", "check", "."),),
                  command_runner=None):
    policy = PolicyEngine(PolicyConfig())
    return ImplementExecutor(
        git=GitGateway(worker_repo, policy),
        agent_runner=agent,
        validation_commands=validation,
        command_runner=command_runner,
        worker_repo_root_for=lambda task_id: worker_repo,
        policy=policy,
        agent_runner_factory=lambda root: agent,
        abort_file=abort_file,
    )


def implement(task_id="t1") -> Directive:
    return Directive(decision=Decision.IMPLEMENT, reason="go", task_id=task_id)


def a_task(task_id="t1") -> Task:
    return Task(
        id=task_id, title="T", description="d", approved_paths=("A.py", "B.py", "docs/")
    )


def test_an_aborted_round_reports_the_partial_work_it_measured(tmp_path):
    """REPORT WHAT WAS DISCARDED, and reuse the sentence that already says it.

    `_partial_work_note` is what every other uncommitted round reports through,
    and it is EVIDENCE: files, lines and paths read from the worker repo's own
    `git status` / `git diff HEAD`, never from `result.raw_text`. An abort that
    said only "stopped" would be the exact failure that note was written for.
    """
    worker = real_repo(tmp_path, "worker")
    abort_file = tmp_path / "ABORT"
    agent = WritingAgent(
        worker,
        {"A.py": "line\n" * 40, "docs/N.md": "note\n"},
        before_returning=abort_file.touch,
    )
    ran_validation = []
    executor = executor_with(
        worker, agent, abort_file,
        command_runner=lambda *a, **k: ran_validation.append(a) or None,
    )

    outcome = executor.execute(implement(), a_task())

    assert outcome.status == EXECUTION_ABORTED
    assert "ABORTED by the operator" in outcome.summary
    assert "A.py" in outcome.summary and "docs/N.md" in outcome.summary
    assert "2 file(s) changed" in outcome.summary
    assert "41 line(s) written" in outcome.summary, outcome.summary
    assert outcome.changed_paths == ("A.py", "docs/N.md")
    assert not ran_validation, "no suite may start after the operator said stop"
    assert outcome.validation.startswith("not run")


def test_an_aborted_round_that_produced_nothing_says_so_rather_than_printing_zero(
    tmp_path,
):
    """The other half of the same sentence: "nothing changed, so nothing was
    lost" and "600 lines are sitting in the worker" call for opposite responses,
    and a bare 0 leaves the reader to guess which one they have."""
    worker = real_repo(tmp_path, "worker")
    abort_file = tmp_path / "ABORT"
    agent = WritingAgent(worker, {}, before_returning=abort_file.touch)

    outcome = executor_with(worker, agent, abort_file).execute(implement(), a_task())

    assert outcome.status == EXECUTION_ABORTED
    assert "no work was lost" in outcome.summary


def test_an_abort_already_set_when_the_round_starts_runs_no_agent_at_all(tmp_path):
    worker = real_repo(tmp_path, "worker")
    abort_file = tmp_path / "ABORT"
    abort_file.touch()
    agent = WritingAgent(worker, {"A.py": "x\n"})

    outcome = executor_with(worker, agent, abort_file).execute(implement(), a_task())

    assert outcome.status == EXECUTION_ABORTED
    assert agent.runs == 0, "an abort must not pay for an agent it is about to kill"


def test_the_abort_is_reported_as_the_cause_not_the_killed_agents_own_failure(tmp_path):
    """A killed agent comes back `not ok`, and reporting THAT as the cause would
    tell a reviewer a healthy agent had wedged. The abort check therefore sits
    ahead of the `result.ok` branch."""
    worker = real_repo(tmp_path, "worker")
    abort_file = tmp_path / "ABORT"
    agent = WritingAgent(
        worker, {"A.py": "x\n"}, before_returning=abort_file.touch, ok=False
    )

    outcome = executor_with(worker, agent, abort_file).execute(implement(), a_task())

    assert outcome.status == EXECUTION_ABORTED
    assert "implementation agent failed" not in outcome.summary


def test_an_executor_with_no_abort_file_behaves_exactly_as_before(tmp_path):
    """The fail-closed default: no wiring, no abort capability, and an existing
    flag elsewhere on disk changes nothing about this executor.

    The injected runner is what makes this a POSITIVE assertion rather than a
    negative one: validation really ran, so "no abort capability" means the
    round completed, not that it stopped somewhere quieter.
    """
    worker = real_repo(tmp_path, "worker")
    (tmp_path / "ABORT").touch()
    agent = WritingAgent(worker, {"A.py": "x\n"})
    ran = []

    outcome = executor_with(
        worker, agent, None, command_runner=lambda argv, **kw: ran.append(tuple(argv)) or _green()
    ).execute(implement(), a_task())

    assert outcome.status == "ok", outcome.summary
    assert agent.runs == 1
    assert ran, "the round must have validated, not stopped early"


def test_the_agents_own_mid_round_validation_run_is_abortable_too(tmp_path):
    """The constraint the task named FIRST, PINNED rather than commented.

    Since impl-02 (2026-08-24) the agent runs the validation suite MID-ROUND
    through the advisory channel, so an abort can land while `pytest -n 4` is
    live. §2 above proves the RUNNER kills its whole process group; what nothing
    proved until here is that the advisory channel is actually HOLDING that
    runner. It is one attribute — `_command_runner`, wrapped once in
    `__init__` — handed to both the round's authoritative run and to
    `_advisory_for`, and a comment saying so is not evidence that it is so.

    The assertion that matters is the second `run()`: an advisory answer reading
    "PASSED" about commands nobody launched is exactly the fail-open this file
    exists to grade, and it would be believed — the agent asked for it.
    """
    worker = real_repo(tmp_path, "worker")
    abort_file = tmp_path / "ABORT"
    launched = []
    executor = executor_with(
        worker,
        WritingAgent(worker, {}),
        abort_file,
        command_runner=lambda argv, **kwargs: launched.append(tuple(argv)) or _green(),
    )
    advisory = executor._advisory_for(
        a_task(), GitGateway(worker, PolicyEngine(PolicyConfig()))
    )

    before = advisory.run()

    assert "PASSED" in before, before
    assert launched == [("ruff", "check", ".")], "the advisory run never executed"

    abort_file.touch()
    after = advisory.run()

    assert launched == [("ruff", "check", ".")], "a command launched after the abort"
    assert "PASSED" not in after, (
        "the advisory channel reported green about a command nobody ran: " + after
    )
    assert advisory.last_run_ok is False, (
        "the round's own record of the last advisory run must be red too — "
        "`note()` reads this, and a True here would tell the reviewer the agent "
        "checked its work against commands that never ran"
    )


def test_the_production_runner_wiring_actually_carries_the_flag(tmp_path):
    """`implement_agent_runner` is the ONE place a write-capable runner is
    built, and the abort only reaches the supervised path. This asserts the
    wiring rather than describing it: the spawn the runner holds is the wrapper,
    and it wraps whatever spawn it was given."""
    worker = real_repo(tmp_path, "worker")
    policy = PolicyEngine(PolicyConfig())
    inner_calls = []

    def fake_spawn(argv, *, cwd, env, stdout, stderr):
        inner_calls.append(argv)
        return FakeProcess(exit_after=1)

    runner = implement_agent_runner(
        worker, policy=policy, spawn=fake_spawn, abort_file=tmp_path / "ABORT"
    )
    handle = runner._spawn(["claude"], cwd=str(worker), env={}, stdout=None, stderr=None)

    assert isinstance(handle, AbortableProcessHandle)
    assert inner_calls == [["claude"]], "the injected spawn is wrapped, not replaced"


# =============================================================================
# 4. THE ROUND: budgets, the queue, the worker, and mainline
# =============================================================================


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


class ScriptedExecutor:
    """One outcome per round. A callable entry is invoked with the worker repo
    path so a round can write real files (and set the abort flag) first."""

    def __init__(self, workers_root, rounds):
        self.workers_root = Path(workers_root)
        self.rounds = list(rounds)
        self.calls = 0

    def execute(self, directive, task):
        step = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        if callable(step):
            return step(self.workers_root / task.id)
        return step


def build(tmp_path, executor_factory, task_id="t1"):
    """The production dispatch path — real git, `WorkerRepoManager`-backed —
    because the abort accounting lives inside `_dispatch_task_postcommit`."""
    repo_root = real_repo(tmp_path)
    (repo_root / ".gitignore").write_text(
        ".al/\n__pycache__/\n*.py[cod]\n", encoding="utf-8"
    )
    run_git(repo_root, "add", ".gitignore")
    run_git(repo_root, "commit", "-q", "-m", "gitignore state dir")

    workers_root = tmp_path / "workers_root"
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig(implement_enabled=True)))
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=repo_root / ".al",
        workers_root=workers_root,
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)
    registry = TaskRegistry([a_task(task_id)])
    task_store = TaskStore(
        config.tasks_file,
        ledger=mutation_ledger_for(config.workers_root, config.state_dir),
    )
    task_store.save(registry)
    execution_store = TaskExecutionStore(tmp_path / "executions")

    def no_client():  # pragma: no cover - no transport is reached by an abort
        raise AssertionError("no conversation is opened during an abort")

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor_factory(workers_root, repo_root),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worker_repos=WorkerRepoManager(workers_root, tmp_path / "worker-hooks"),
        execution_store=execution_store,
        intent_store=IntentStore(tmp_path / "intents"),
        blocker_store=BlockerStore(tmp_path / "blockers"),
        validation_runner=ok_validation,
    )
    return orch, execution_store, repo_root, config


def aborting_round(abort_file, *, rel="A.py", body="x\n" * 30, status=EXECUTION_ABORTED):
    """A round that writes real work into its worker repo and is then aborted —
    the shape `implement_executor` produces when the operator presses the
    button mid-agent."""

    def _round(worktree: Path):
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / rel).write_text(body, encoding="utf-8")
        abort_file.touch()
        return ExecutionOutcome(
            status=status,
            summary=(
                "task 't1': the round was ABORTED by the operator — the "
                "implementation agent and every process it spawned were killed. "
                "Partial work left in the worker repository: 1 file(s) changed, "
                "~30 line(s) written; paths: A.py"
            ),
            validation="not run (aborted)",
            changed_paths=(rel,),
        )

    return _round


def assert_books_balance(execution) -> None:
    assert (
        execution.attempt_count + execution.fault_attempt_count
        == len(execution.attempt_ledger)
    ), (
        f"attempt_count={execution.attempt_count} "
        f"fault_attempt_count={execution.fault_attempt_count} "
        f"ledger={execution.attempt_ledger}"
    )


def test_an_aborted_round_spends_no_attempt_and_counts_no_fault(tmp_path):
    """"No attempt spent, no fault counted, no ceiling advanced." Charging
    either would make the abort itself a cause of `attempt_count_ceiling`."""
    abort_file = tmp_path / "ABORT"
    orch, execution_store, _repo, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(wr, [aborting_round(abort_file)]),
    )

    orch._dispatch_executor(implement())

    execution = execution_store.load("t1")
    assert execution.attempt_count == 0, "the task's own budget must be untouched"
    assert execution.fault_attempt_count == 0, "an abort is not an environment fault"
    assert execution.attempt_ledger == (), "the open entry is removed, not settled"
    assert_books_balance(execution)
    assert orch.state.consecutive_failures == 0


def test_leaving_the_entry_open_would_have_become_a_fault_one_round_later(tmp_path):
    """WHY the refund removes the entry rather than walking away from it.

    `_reconcile_unfinished_attempts` settles any OPEN entry as
    `ATTEMPT_FAULT, "interrupted_mid_round"` at the START of the next dispatch.
    An abort that merely stopped would therefore be charged a fault one round
    later — silently, and eventually as `fault_attempt_ceiling` blaming the
    environment for the operator's own button. This runs the second dispatch and
    asserts the charge never appears.
    """
    abort_file = tmp_path / "ABORT"
    orch, execution_store, _repo, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr,
            [
                aborting_round(abort_file),
                ExecutionOutcome(status="error", summary="second round failed"),
            ],
        ),
    )
    orch._dispatch_executor(implement())
    abort_file.unlink()
    orch.state.phase = Phase.EXECUTING.value

    orch._dispatch_executor(implement())

    execution = execution_store.load("t1")
    assert execution.fault_attempt_count == 0, (
        "the aborted round came back as a fault charge — the refund did not "
        f"remove its ledger entry: {execution.attempt_ledger}"
    )
    assert execution.attempt_count == 1, "only the SECOND round is charged"
    assert [split_attempt(e)[1] for e in execution.attempt_ledger] == [ATTEMPT_TASK]
    assert_books_balance(execution)


def test_an_aborted_round_returns_its_task_to_the_queue(tmp_path):
    """Resumable, not consumed: back to PENDING, so `next_ready` can see it
    again — an in-progress task is invisible to it and would sit there."""
    abort_file = tmp_path / "ABORT"
    orch, _executions, _repo, config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(wr, [aborting_round(abort_file)]),
    )

    orch._dispatch_executor(implement())

    assert orch._registry.get("t1").status == "pending"
    assert orch._registry.state_of("t1") is TaskState.READY
    # ...and DURABLY: continuous mode re-reads tasks.json at the top of its next
    # iteration, so an unsaved status move would simply not exist there.
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.get("t1").status == "pending"


def test_the_worker_repo_keeps_its_uncommitted_work_and_the_round_is_resumable(tmp_path):
    """`shelve`'s semantics, not `release`'s: the execution record and the
    worker repository are LEFT WHERE THEY ARE, so the next dispatch resumes this
    round rather than starting over."""
    abort_file = tmp_path / "ABORT"
    orch, execution_store, _repo, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(wr, [aborting_round(abort_file)]),
    )

    orch._dispatch_executor(implement())

    worker = orch._worker_repos.path_for("t1")
    assert (worker / "A.py").read_text(encoding="utf-8") == "x\n" * 30
    assert execution_store.load("t1") is not None, "the record must not be archived"
    record = orch.state.aborted_round
    assert record["worker_path"] == str(worker)
    assert record["resumable"] is True


def test_an_aborted_round_leaves_mainline_untouched(tmp_path):
    """ASSERT IT, DO NOT ARGUE IT. The whole value of the verb is that an
    operator can stop without thinking about consequences, so the primary
    checkout's HEAD and its working tree are captured before and after and
    compared — the argument ("the executor commits only after the agent returns")
    is why it holds; this is the evidence that it does.
    """
    abort_file = tmp_path / "ABORT"
    orch, _executions, repo_root, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(wr, [aborting_round(abort_file)]),
    )
    head_before = run_git(repo_root, "rev-parse", "HEAD").strip()
    status_before = run_git(repo_root, "status", "--porcelain")
    branches_before = run_git(repo_root, "branch", "--format=%(refname)")

    orch._dispatch_executor(implement())

    assert run_git(repo_root, "rev-parse", "HEAD").strip() == head_before
    assert run_git(repo_root, "status", "--porcelain") == status_before == ""
    assert run_git(repo_root, "branch", "--format=%(refname)") == branches_before


def test_nothing_is_committed_in_the_worker_either(tmp_path):
    """The candidate side of the same guarantee: an aborted round returns before
    the commit, so the work stays uncommitted (which is what makes it survive
    into the resumed round) and no candidate sha is recorded for a reviewer to
    be shown."""
    abort_file = tmp_path / "ABORT"
    orch, execution_store, _repo, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(wr, [aborting_round(abort_file)]),
    )

    orch._dispatch_executor(implement())

    execution = execution_store.load("t1")
    assert execution.candidate_sha == ""
    assert execution.review_round == 0
    worker = orch._worker_repos.path_for("t1")
    assert "A.py" in run_git(worker, "status", "--porcelain")


def test_the_session_records_what_the_killed_step_had_produced(tmp_path):
    abort_file = tmp_path / "ABORT"
    orch, _executions, _repo, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(wr, [aborting_round(abort_file)]),
    )

    orch._dispatch_executor(implement())

    assert orch.state.phase == Phase.STOPPED.value
    assert orch.state.stop_kind == ABORT_STOP_KIND
    record = orch.state.aborted_round
    assert record["task_id"] == "t1"
    assert record["returned_to_pending"] is True
    assert record["attempt_refunded"] == ATTEMPT_PENDING
    assert record["partial_work_measured"] is True
    assert "1 file(s) changed" in record["discarded_work"]
    assert "1 file(s) changed" in orch.state.stop_reason
    # The queued packet goes with the round it was about.
    assert orch.state.outbox is None and orch.state.task_execution is None


def test_the_flag_alone_aborts_a_round_whose_outcome_never_said_so(tmp_path):
    """Two independent signals, either sufficient. The flag is the operator's
    own artefact; `EXECUTION_ABORTED` is the executor's report of having read
    it. A round whose executor never learned about the flag — an embedder with
    no `abort_file` wired — must still not be committed or charged."""
    abort_file = tmp_path / "ABORT"
    orch, execution_store, repo_root, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr, [aborting_round(abort_file, status="ok")]
        ),
    )
    head_before = run_git(repo_root, "rev-parse", "HEAD").strip()

    orch._dispatch_executor(implement())

    execution = execution_store.load("t1")
    assert execution.candidate_sha == "", "an aborted round commits nothing"
    assert execution.attempt_count == 0
    assert orch.state.stop_kind == ABORT_STOP_KIND
    assert orch.state.aborted_round["partial_work_measured"] is False, (
        "the record must say the line it carries is NOT a partial-work "
        "measurement, rather than presenting an ordinary summary as one"
    )
    assert run_git(repo_root, "rev-parse", "HEAD").strip() == head_before


def test_the_next_dispatch_resumes_the_aborted_round(tmp_path):
    """The claim's last word: resumable. The second dispatch reuses the SAME
    worker repository — the partial file is still there and is committed with
    the round that follows — rather than cutting a fresh one."""
    abort_file = tmp_path / "ABORT"

    def second_round(worktree: Path):
        (worktree / "B.py").write_text("more\n", encoding="utf-8")
        return ExecutionOutcome(
            status="ok",
            summary="finished it",
            validation="ok",
            changed_paths=("A.py", "B.py"),
        )

    orch, execution_store, _repo, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(wr, [aborting_round(abort_file), second_round]),
    )
    orch._dispatch_executor(implement())
    first_worker = orch._worker_repos.path_for("t1")
    abort_file.unlink()
    orch.state.phase = Phase.EXECUTING.value

    orch._dispatch_executor(implement())

    execution = execution_store.load("t1")
    assert execution.worktree_path == str(first_worker), "a fresh worker was cut"
    assert execution.candidate_sha != "", "the resumed round produced a candidate"
    committed = run_git(first_worker, "show", "--name-only", "--format=", "HEAD")
    assert "A.py" in committed, "the aborted round's work was lost"
    assert "B.py" in committed


# =============================================================================
# 5. THE REFUND, in isolation
# =============================================================================


def test_a_refunded_redo_re_arms_the_recovery_it_was_still_owed(tmp_path):
    """A round opened on the FAULT budget is a redo of a review a fault
    destroyed. Aborting it must give back the fault charge AND the marker, or
    the next dispatch quietly becomes the task's own attempt."""
    execution = TaskExecution(
        task_id="t1",
        task_branch="autoloop/t1",
        worktree_path=str(tmp_path),
        task_base_sha="b" * 40,
        fault_attempt_count=1,
        attempt_ledger=(format_attempt(1, ATTEMPT_PENDING_FAULT, "browser_session_lost"),),
    )

    assert refund_attempt(execution) == ATTEMPT_PENDING_FAULT
    assert execution.fault_attempt_count == 0
    assert execution.attempt_ledger == ()
    assert execution.pending_fault_code == "browser_session_lost"


def test_a_refund_never_touches_a_round_that_already_settled(tmp_path):
    """One-way, like `_finalise_attempt`: a genuine failure a round recorded for
    itself can never be un-charged by anything downstream of it."""
    execution = TaskExecution(
        task_id="t1",
        task_branch="autoloop/t1",
        worktree_path=str(tmp_path),
        task_base_sha="b" * 40,
        attempt_count=1,
        attempt_ledger=(format_attempt(1, ATTEMPT_TASK, "validation_failed"),),
    )

    assert refund_attempt(execution) == ""
    assert execution.attempt_count == 1
    assert len(execution.attempt_ledger) == 1


def test_a_refund_refuses_rather_than_breaking_the_books(tmp_path):
    """A hand-edited record whose counter does not match its ledger. Refusing
    leaves the pre-abort behaviour (the next dispatch settles the open entry as
    a fault, which an operator can see); decrementing below zero would break the
    invariant every ceiling is computed from, permanently and silently."""
    execution = TaskExecution(
        task_id="t1",
        task_branch="autoloop/t1",
        worktree_path=str(tmp_path),
        task_base_sha="b" * 40,
        attempt_count=0,
        attempt_ledger=(format_attempt(1, ATTEMPT_PENDING, "dispatched"),),
    )

    assert refund_attempt(execution) == ""
    assert execution.attempt_count == 0
    assert execution.attempt_ledger != ()


def test_a_refund_on_an_empty_ledger_is_a_no_op(tmp_path):
    execution = TaskExecution(
        task_id="t1",
        task_branch="autoloop/t1",
        worktree_path=str(tmp_path),
        task_base_sha="b" * 40,
    )
    assert refund_attempt(execution) == ""
    assert execution.attempt_ledger == ()


# =============================================================================
# 6. THE PHASES: what an abort may and may not interrupt
# =============================================================================


def loop_at(tmp_path, phase, *, pending=None):
    """A minimal orchestrator parked at `phase`, for the between-steps rule."""
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "outside" / "workers",
    )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    state.phase = phase.value
    state.outbox = "a packet"
    state.pending_request = pending
    store.save(state)
    registry = TaskRegistry([a_task()])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    class NoStep:
        pass

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=NoStep(),
        executor=NoStep(),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: (_ for _ in ()).throw(
            AssertionError("no conversation is opened")
        ),
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        execution_store=TaskExecutionStore(config.executions_dir),
        worker_repos=WorkerRepoManager(config.workers_root, config.worker_hooks_dir),
    )
    return orch, config


def test_an_abort_never_kills_a_step_while_a_review_packet_is_outstanding(tmp_path):
    """NEVER ABORT IN `submitting` OR `awaiting`. A review packet is outstanding
    there and killing the step strands an approved push.

    What happens instead is exactly what `pause` does: the loop returns BETWEEN
    steps, having killed nothing, with the phase and the pending request intact
    for the resume. `_step_awaiting` is never entered — the stub client would
    raise if it were.
    """
    pending = PendingRequest(request_id="alr-1", payload="the packet", submitted=True)
    orch, config = loop_at(tmp_path, Phase.AWAITING, pending=pending)
    abort_flag_file(config).parent.mkdir(parents=True, exist_ok=True)
    abort_flag_file(config).touch()

    assert orch.run() == ABORTED

    assert orch.state.phase == Phase.AWAITING.value, "the phase must survive"
    assert orch.state.pending_request.request_id == "alr-1", "the packet must survive"
    assert orch.state.stop_kind != ABORT_STOP_KIND, "no round was killed, so none is recorded"
    assert orch.state.aborted_round is None


@pytest.mark.parametrize("phase", [Phase.READY, Phase.SUBMITTING, Phase.EXECUTING])
def test_the_loop_stops_between_steps_from_any_live_phase(tmp_path, phase):
    orch, config = loop_at(tmp_path, phase)
    abort_flag_file(config).parent.mkdir(parents=True, exist_ok=True)
    abort_flag_file(config).touch()

    assert orch.run() == ABORTED
    assert orch.state.phase == phase.value


def test_a_parked_loop_reports_its_park_rather_than_the_abort(tmp_path):
    """AFTER the terminal check, deliberately: a loop parked on a question an
    operator has to answer must keep saying so, or the abort hides the very
    thing that needs a decision."""
    orch, config = loop_at(tmp_path, Phase.NEEDS_USER)
    abort_flag_file(config).parent.mkdir(parents=True, exist_ok=True)
    abort_flag_file(config).touch()

    assert orch.run() == Phase.NEEDS_USER.value


def test_the_abort_flag_lives_outside_the_checkout_beside_pause(tmp_path):
    """The escape-detector trap, which the pause flag hit first: `state_dir` is
    inside the tree snapshotted around every write-capable agent call, so a flag
    written there mid-round reads as an escape and parks the loop loop_fatal. An
    abort flag is written at exactly that moment BY DEFINITION, so it would hit
    that trap every single time."""
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / "checkout" / ".al",
        workers_root=tmp_path / "outside" / "workers",
    )
    flag = abort_flag_file(config)

    assert flag.parent == config.pause_file.parent
    assert config.state_dir not in flag.parents
    assert (tmp_path / "checkout") not in flag.parents


def test_abort_requested_reads_the_flag_and_nothing_else(tmp_path):
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "outside" / "workers",
    )
    assert abort_requested(config) is False
    flag = abort_flag_file(config)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    assert abort_requested(config) is True
    assert abort_flag_set(flag) is True
    assert abort_flag_set(None) is False


# =============================================================================
# 7. THE CLI: the verb, and the two flags an operator holds
# =============================================================================


@pytest.fixture
def cli_config(tmp_path, monkeypatch):
    repo_root = real_repo(tmp_path, "checkout")
    config_dir = repo_root / ".al"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "\n".join(
            [
                "[browser]",
                f'conversation_url = "{URL}"',
                "[paths]",
                f'state_dir = "{config_dir}"',
                f'workers_root = "{tmp_path / "outside" / "workers"}"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(repo_root)
    return config_dir / "config.toml"


def test_abort_writes_the_flag_and_says_what_it_costs(cli_config, capsys):
    assert cli._cmd_abort(argparse.Namespace(config=cli_config)) == 0
    config = cli.load_config(cli_config)

    assert abort_requested(config) is True
    printed = capsys.readouterr().out
    assert "killed within seconds" in printed
    assert "no attempt or fault is charged" in printed


def test_abort_does_not_set_the_pause_flag_and_pause_does_not_set_this_one(cli_config):
    """`pause` KEEPS ITS CURRENT MEANING — this is a second verb, not a change
    to the first."""
    config = cli.load_config(cli_config)
    cli._cmd_abort(argparse.Namespace(config=cli_config))
    assert cli.pause_requested(config) is False

    cli.clear_abort(config)
    cli._cmd_pause(argparse.Namespace(config=cli_config))
    assert abort_requested(config) is False
    assert cli.pause_requested(config) is True


def test_resume_clears_both_flags(cli_config, monkeypatch, capsys):
    """A resume that cleared only half would print success and leave a loop that
    stops at the top of its first step with nothing saying why."""
    config = cli.load_config(cli_config)
    cli._cmd_pause(argparse.Namespace(config=cli_config))
    cli._cmd_abort(argparse.Namespace(config=cli_config))
    monkeypatch.setattr(cli, "_cmd_run", lambda args: 0)

    assert cli._cmd_resume(argparse.Namespace(config=cli_config)) == 0

    assert cli.pause_requested(config) is False
    assert abort_requested(config) is False
    printed = capsys.readouterr().out
    assert "pause flag cleared" in printed and "abort flag cleared" in printed


def test_status_shows_the_abort_flag(cli_config):
    config = cli.load_config(cli_config)
    registry = TaskRegistry([a_task()])
    state = LoopState.new(URL)

    assert "abort flag   no" in cli._summary(config, state, registry)
    cli._cmd_abort(argparse.Namespace(config=cli_config))
    assert "abort flag   yes" in cli._summary(config, state, registry)


def test_continuous_mode_stops_instead_of_picking_the_task_straight_back_up(
    cli_config, capsys
):
    """The failure this check exists for: an abort ends its session `stopped`,
    and a `stopped` session is exactly what continuous mode treats as a clean
    boundary and selects afresh from — so without a flag check of its own the
    abort would stop one round and start another on the same task."""
    config = cli.load_config(cli_config)
    cli._cmd_abort(argparse.Namespace(config=cli_config))

    exit_code = cli._run_continuous(
        argparse.Namespace(config=cli_config, continuous=True), config
    )

    assert exit_code == 0
    assert "aborted" in capsys.readouterr().out


def test_start_clears_a_stale_abort_flag(cli_config, monkeypatch, capsys):
    """`start` is an explicit request to run, so a flag left over from an
    operator who stopped the loop yesterday must not stop it again."""
    config = cli.load_config(cli_config)
    cli._cmd_abort(argparse.Namespace(config=cli_config))
    capsys.readouterr()
    # The browser repair really probes CDP on 127.0.0.1:9222; since brw-16 no
    # fixture stops it, and dialling a port is not what this test is about.
    monkeypatch.setattr(cli, "_repair_browser", lambda cfg: ("browser      n/a", True))

    # `--check-only`, so the preflight runs and stops rather than starting a
    # loop that would want a conversation.
    cli._cmd_start(argparse.Namespace(config=cli_config, check_only=True))

    assert abort_requested(config) is False
    assert "abort        flag cleared" in capsys.readouterr().out


def test_the_operator_report_names_the_work_that_was_discarded(tmp_path, capsys):
    """What the terminal shows. An abort that reported only "stopped" would be
    the failure the partial-work note was written for, one level up."""
    state = LoopState.new(URL)
    state.phase = Phase.STOPPED.value
    state.stop_kind = ABORT_STOP_KIND
    state.aborted_round = {
        "task_id": "t1",
        "is_audit": False,
        "aborted_at_phase": "executing",
        "returned_to_pending": True,
        "attempt_refunded": ATTEMPT_PENDING,
        "attempt_count": 0,
        "fault_attempt_count": 0,
        "obstacle": "",
        "discarded_work": "9 file(s) changed, ~612 line(s) written; paths: A.py",
        "partial_work_measured": True,
        "worker_path": str(tmp_path / "workers" / "t1"),
        "resumable": True,
        "preservation_obstacle": "",
    }

    cli._report_abort(state)

    printed = capsys.readouterr().out
    assert "ABORTED by the operator" in printed
    assert "612 line(s) written" in printed
    assert "in_progress -> pending" in printed
    assert "CONTINUES this round" in printed
    assert "an abort charges neither" in printed


def test_the_operator_report_is_silent_for_every_other_stop():
    """Callers print it unconditionally on a clean stop, so a session that was
    not aborted must produce nothing at all."""
    state = LoopState.new(URL)
    state.phase = Phase.STOPPED.value
    state.stop_kind = "contract"
    assert cli._is_abort_stop(state) is False
