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
    neighbours) the KILL IS REFUSED and says so — `ABORT_REFUSED`, not
    `ABORTED`. Nothing is killed, the loop stops between steps exactly as
    `pause` already could, and the pending request and its phase survive. The
    distinct return value is the point: returning `ABORTED` from there was a
    silent degrade to pause semantics, and a refusal nobody is told about is a
    guard that has switched itself off;
  * an abort already ACTED ON stays acted on: the flag is a file and `resume`
    deletes it, so the round's classification is carried by a positive
    per-round record (`AbortLedger`) of what this process killed, never by a
    second read of a file a third party can remove;
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
    AbortLedger,
    ImplementExecutor,
    abort_aware_command_runner,
    abort_aware_spawn,
    abort_flag_set,
    abort_in_effect,
    implement_agent_runner,
    killable_run,
)
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    ABORT_REFUSED,
    ABORT_STOP_KIND,
    ABORTED,
    Orchestrator,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.stall import COMPLETED, StallPolicy, spawn_supervised, supervise
from autoloop.state import (
    EXECUTION_ABORTED,
    PACKET_OUTSTANDING_PHASES,
    LoopState,
    PendingRequest,
    Phase,
    StateStore,
    abort_flag_file,
    abort_requested,
    packet_outstanding_reason,
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
                  command_runner=None, ledger=None):
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
        # The object `cli._build_executor` shares between the executor and the
        # runner factory. Passed explicitly here so a test can write into it
        # exactly where a real `AbortableProcessHandle` would.
        abort_ledger=ledger,
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
    # TWO, not one, and not because of anything abort-related: `executor_with`
    # configures validation, so the advisory channel is offered, and this agent
    # never uses it — since advis-01 (2026-08-26) a report with zero advisory
    # requests is handed back to the agent once before the round is forwarded.
    # What this test is about is unchanged: the round ran to completion.
    assert agent.runs == 2
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
# 3b. THE ABORT-THEN-RESUME RACE: a flag is a file, and `resume` deletes it
# =============================================================================
#
# THE SEQUENCE THAT COSTS A TASK AN ATTEMPT, and the one the executor's report
# named: flag appears -> the agent's process group is killed -> the flag is
# cleared (`resume`, a second operator command, a wrapper script) -> the round
# re-reads the flag, sees nothing, and reports the killed agent's own `not ok`
# as an ordinary failure. That charges the attempt, names a `fault_kind`, and
# builds an `attempt_count_ceiling` out of the operator's own button — which is
# the one thing the task's constraints forbid outright.
#
# The fix is a POSITIVE per-round record of what this process DID, never a
# second read of a file a third party can remove.


def test_the_ledger_records_the_kill_rather_than_inferring_it_from_the_flag(tmp_path):
    abort_file = tmp_path / "ABORT"
    abort_file.touch()
    ledger = AbortLedger()
    inner = FakeProcess()
    handle = AbortableProcessHandle(
        inner, abort_file, ledger=ledger, grace_seconds=1.0, sleep=lambda _: None
    )

    assert ledger.killed is False, "nothing has been killed yet"
    handle.poll()

    assert ledger.killed is True
    assert "implementation agent" in ledger.reason
    # AND IT SURVIVES THE FILE. This is the whole property: the record is about
    # what this process did, so deleting the operator's flag cannot un-say it.
    abort_file.unlink()
    assert ledger.killed is True
    assert abort_in_effect(abort_file, ledger) is True
    assert abort_in_effect(abort_file, None) is False, "the flag alone has forgotten"


def test_a_flag_cleared_before_the_round_re_reads_it_is_still_an_abort(tmp_path):
    """THE REGRESSION. The agent is killed, the operator's `resume` removes the
    flag, and the round is classified AFTER that. Without the ledger this is a
    `status="error"` round carrying `fault_kind` — a charged failure — and the
    reviewer is told a healthy agent wedged."""
    worker = real_repo(tmp_path, "worker")
    abort_file = tmp_path / "ABORT"
    ledger = AbortLedger()

    def aborted_then_resumed():
        # The whole sequence, in order, WHILE THE AGENT RUNS — which is the only
        # time it can happen. A flag armed before the round starts is caught by
        # the pre-spawn check instead and no agent is paid for at all.
        abort_file.touch()  # the operator arms it
        # what `AbortableProcessHandle.poll` does on the next 5s tick:
        ledger.record("the implementation agent and every process it spawned were killed")
        abort_file.unlink()  # `resume`, before the round classifies itself

    agent = WritingAgent(
        worker, {"A.py": "x\n" * 12}, before_returning=aborted_then_resumed, ok=False
    )
    executor = executor_with(worker, agent, abort_file, ledger=ledger)

    outcome = executor.execute(implement(), a_task())

    assert outcome.status == EXECUTION_ABORTED, (
        "a cleared flag turned the operator's abort back into a charged failure"
    )
    assert outcome.fault_kind == "", "an abort spends neither budget"
    assert "implementation agent failed" not in outcome.summary
    assert "1 file(s) changed" in outcome.summary, "the partial work is still measured"
    assert abort_file.exists() is False, "the test's own premise"


#: The clause `killable_run` writes into the ledger when an abort kills a live
#: validation process group.
#:
#: Held as ONE string because two places need it and they must not drift: the
#: helper below, which REPLAYS the kill rather than performing one, and
#: `test_the_replayed_kill_clause_is_the_one_production_actually_writes`, which
#: runs the real thing and requires equality. Without that pin, a helper that
#: writes the sentence by hand and a test that then asserts on it are an ECHO —
#: the test reading back its own input — and production could change the wording
#: with every assertion still green.
VALIDATION_KILL_CLAUSE = (
    "the validation subprocess and every process it spawned were killed"
)


def test_the_replayed_kill_clause_is_the_one_production_actually_writes(tmp_path):
    """The echo guard for `VALIDATION_KILL_CLAUSE`, by equality rather than by
    substring: `"validation subprocess" in reason` would still hold if
    `killable_run` started writing a different sentence around those two words,
    and the helper's replay would go on asserting about a clause nothing
    produces. This kills a real process group and compares the whole string."""
    abort_file = tmp_path / "ABORT"
    abort_file.touch()
    produced = AbortLedger()

    killable_run(
        [sys.executable, "-c", f"import time; time.sleep({LINGER_SECONDS})"],
        cwd=tmp_path,
        text=True,
        abort_file=abort_file,
        ledger=produced,
        poll_seconds=0.05,
        grace_seconds=2.0,
    )

    assert produced.reason == VALIDATION_KILL_CLAUSE, (
        "the replayed clause drifted from the one production writes: "
        f"{produced.reason!r}"
    )


def a_round_whose_suite_was_killed(tmp_path):
    """Run the REAL executor through the race's SECOND window and return what it
    reported: `(outcome, launched, abort_file)`.

    THE WINDOW the first abort-01 round left open. The check at the top of
    `_run_implementation` covers an abort landing while the AGENT runs; this one
    lands while the AUTHORITATIVE SUITE runs, which since impl-02 is a window of
    minutes and the longest one left in a round.

    The runner reproduces `killable_run`'s own kill branch IN ORDER, and the
    order is the whole point. Arming the flag before `execute()` would exercise
    `abort_aware_command_runner`'s outer refusal instead — a different branch
    writing a different clause — so the flag is armed INSIDE the runner, after
    that outer check has already let the command through.
    """
    worker = real_repo(tmp_path, "killed-suite-worker")
    abort_file = tmp_path / "ABORT"
    ledger = AbortLedger()
    launched = []

    def killed_mid_suite(argv, **kwargs):
        launched.append(tuple(argv))
        abort_file.touch()  # the operator arms it, mid-suite
        # what `killable_run` does on its next 0.25s tick — the clause is
        # production's, pinned by equality in the guard above rather than
        # retyped here, so this replay cannot drift away from what it replays.
        ledger.record(VALIDATION_KILL_CLAUSE)
        abort_file.unlink()  # `resume`, before the round classifies itself

        class Killed:
            returncode = ABORT_RETURNCODE
            stdout = ""
            stderr = ""

        return Killed()

    executor = executor_with(
        worker,
        WritingAgent(worker, {"A.py": "x\n" * 30}),
        abort_file,
        command_runner=killed_mid_suite,
        ledger=ledger,
    )
    return executor.execute(implement(), a_task()), launched, abort_file


def test_a_suite_the_operator_killed_is_not_the_tasks_own_red_validation(tmp_path):
    """THE REGRESSION, executor side.

    Sequence: the flag appears while the suite is live -> the validation process
    group is killed and the ledger records it -> `resume` removes the flag ->
    the round classifies itself. Without a ledger read at that point the killed
    suite's own `rc=-99` falls through to "validation failed after
    implementation" — a `status="error"` round — and the orchestrator's own flag
    read finds nothing either, so the task is charged an attempt for a suite the
    operator stopped.
    """
    outcome, launched, abort_file = a_round_whose_suite_was_killed(tmp_path)

    assert launched, "the round never reached the authoritative run"
    assert abort_file.exists() is False, "the test's own premise"
    assert outcome.status == EXECUTION_ABORTED, (
        "a suite the operator killed was reported as the task's own failing "
        f"tests: {outcome.summary}"
    )
    assert "validation failed after implementation" not in outcome.summary
    assert outcome.fault_kind == "", "an abort spends neither budget"
    # It took the KILLED branch and says so. "refused before launching" is the
    # other clause a validation abort can write, and reporting one for the other
    # would tell the operator a suite ran that did not, or the reverse.
    assert "validation subprocess" in outcome.summary, outcome.summary
    # The work is still MEASURED — the constraint the task states — and measured
    # from the tree as it stood before the suite launched.
    assert "1 file(s) changed" in outcome.summary, outcome.summary
    assert outcome.changed_paths == ("A.py",)


def test_a_killed_suite_is_never_reported_as_a_suite_that_did_not_run(tmp_path):
    """The abort report's fixed sentence stops being true at this site.

    Every pre-validation abort site returns before any command launches, so
    "Validation did not run" is exactly right there. Here the suite LAUNCHED and
    was killed, and `run_validation_commands` has already written the per-command
    `PASS`/`FAIL`/`NOT RUN` account of how far it got. That account is real
    evidence about a repository an operator is about to resume into, and
    replacing it with a blanket denial would discard it.
    """
    outcome, _launched, _abort_file = a_round_whose_suite_was_killed(tmp_path)

    assert "Validation did not run" not in outcome.summary, outcome.summary
    assert "cut short by the abort" in outcome.summary, outcome.summary
    assert outcome.validation != "not run (aborted)", (
        "the round threw away the suite's own account of what it managed to run"
    )
    # `state.last_validation` is written from this field, so an operator reading
    # the loop's status sees which command was interrupted rather than a phrase.
    assert "ruff" in outcome.validation, outcome.validation


def test_a_finished_suite_is_not_reported_as_one_the_abort_cut_short(tmp_path):
    """The flag can also land AFTER the suite finishes, and the report must not
    claim a kill that never happened.

    Nothing refused or killed a command on this path — the ledger is EMPTY and
    the flag alone ends the round — so "cut short by the abort" would be the
    report overstating what the operator's button did, and "the agent and every
    process it spawned were killed" would invent a kill outright.

    It also pins the new check as UNCONDITIONAL rather than an extra clause on
    the failure branch: this suite passed, and the round still ends as an abort
    — which is what the orchestrator's own flag read would do with it anyway.
    """
    worker = real_repo(tmp_path, "worker")
    abort_file = tmp_path / "ABORT"

    def finished_then_aborted(argv, **kwargs):
        # The command completes normally and the flag lands in the window
        # between it returning and the round classifying itself, so
        # `killable_run` never sees it and nothing is recorded.
        abort_file.touch()
        return _green()

    outcome = executor_with(
        worker,
        WritingAgent(worker, {"A.py": "x\n"}),
        abort_file,
        command_runner=finished_then_aborted,
    ).execute(implement(), a_task())

    assert outcome.status == EXECUTION_ABORTED, outcome.summary
    assert "cut short" not in outcome.summary, outcome.summary
    assert "had already finished" in outcome.summary, outcome.summary
    assert "were killed" not in outcome.summary, (
        "the report claimed a kill on a round where nothing was killed: "
        + outcome.summary
    )


def test_a_healthy_round_after_an_aborted_one_is_not_classified_as_aborted(tmp_path):
    """THE DANGEROUS DIRECTION, and the reason the reset is unconditional.

    `cli._build_executor` builds ONE ledger for the process and shares it with
    the runner factory, so a ledger that remembered a kill forever would refund
    an attempt nobody spent and shelve a task that was working — silently, and
    for every round after the first abort. `_run_implementation` resets it at
    the top, before it reads anything.
    """
    worker = real_repo(tmp_path, "worker")
    abort_file = tmp_path / "ABORT"
    ledger = AbortLedger()
    ran = []

    def aborted_on_the_first_round_only():
        # `runs` is incremented before this fires, so this is round 1 alone —
        # round 2 is an ordinary healthy round sharing the same ledger, which is
        # exactly the production shape. The flag is armed and cleared INSIDE the
        # round, because that is the only window in which the race exists.
        if agent.runs == 1:
            abort_file.touch()
            ledger.record("the implementation agent and every process it spawned were killed")
            abort_file.unlink()

    agent = WritingAgent(
        worker, {"A.py": "x\n"}, before_returning=aborted_on_the_first_round_only
    )
    executor = executor_with(
        worker,
        agent,
        abort_file,
        ledger=ledger,
        command_runner=lambda argv, **kw: ran.append(tuple(argv)) or _green(),
    )
    assert executor.execute(implement(), a_task()).status == EXECUTION_ABORTED

    second = executor.execute(implement(), a_task())

    assert second.status == "ok", (
        "the second round inherited the first round's kill: " + second.summary
    )
    assert ran, "the second round must really have validated"
    assert ledger.killed is False, "the ledger was not reset for the new round"


def test_the_report_says_what_was_actually_killed_rather_than_one_fixed_guess(tmp_path):
    """An overstatement is a fail-open of its own: the reviewer reads the summary
    before the diff, and "the agent was killed" about a round whose agent had
    already returned is the executor claiming an action it never took. Three
    things can end a round here and the ledger carries whichever happened."""
    worker = real_repo(tmp_path, "worker")
    abort_file = tmp_path / "ABORT"

    # (a) nothing was killed — the flag landed after the agent returned.
    flag_only = executor_with(
        worker, WritingAgent(worker, {}, before_returning=abort_file.touch), abort_file
    ).execute(implement(), a_task())
    assert "stopped before it could finish" in flag_only.summary
    assert "killed" not in flag_only.summary, flag_only.summary

    # (b) the validation group was killed, and the sentence names THAT.
    abort_file.unlink()
    ledger = AbortLedger()
    validation_killed = executor_with(
        worker,
        WritingAgent(
            worker,
            {},
            before_returning=lambda: ledger.record(
                "the validation subprocess and every process it spawned were killed"
            ),
        ),
        abort_file,
        ledger=ledger,
    ).execute(implement(), a_task())
    assert "validation subprocess" in validation_killed.summary


def test_the_ledger_keeps_refusing_commands_after_the_flag_is_cleared(tmp_path):
    """The sticky half, and it is not decoration: `AdvisoryRendezvous.stop()`
    joins the validation thread for up to `ADVISORY_STOP_JOIN_SECONDS` (11 min).
    If a flag cleared mid-abort let the remaining commands launch again, the
    round's own `finally` would wait out a suite — the 39-minute wait rebuilt
    under a new name, in the exact code path this verb exists to remove."""
    abort_file = tmp_path / "ABORT"
    ledger = AbortLedger()
    launched = []
    runner = abort_aware_command_runner(
        lambda argv, **kw: launched.append(tuple(argv)) or _green(), abort_file, ledger
    )

    abort_file.touch()
    first = runner(["pytest"], cwd=tmp_path)
    abort_file.unlink()
    second = runner(["ruff", "check", "."], cwd=tmp_path)

    assert first.returncode == ABORT_RETURNCODE
    assert second.returncode == ABORT_RETURNCODE, "a cleared flag re-armed the suite"
    assert launched == [], "no command may launch once the round is being aborted"
    assert "refused before launching" in ledger.reason, (
        "a refusal is not a kill and the record must not claim one"
    )


def test_the_killable_runner_records_its_own_kill_but_not_a_timeout(tmp_path):
    """Both real subprocesses, because the distinction is the point: a suite the
    OPERATOR stopped must classify the round as aborted, and a suite that simply
    ran too long is the round's own failure and must keep costing it an
    attempt."""
    abort_file = tmp_path / "ABORT"
    aborted, timed_out = AbortLedger(), AbortLedger()

    abort_file.touch()
    proc = killable_run(
        [sys.executable, "-c", f"import time; time.sleep({LINGER_SECONDS})"],
        cwd=tmp_path,
        text=True,
        abort_file=abort_file,
        ledger=aborted,
        poll_seconds=0.05,
        grace_seconds=2.0,
    )
    assert proc.returncode != 0
    assert "validation subprocess" in aborted.reason

    with pytest.raises(subprocess.TimeoutExpired):
        killable_run(
            [sys.executable, "-c", f"import time; time.sleep({LINGER_SECONDS})"],
            cwd=tmp_path,
            text=True,
            ledger=timed_out,
            timeout=0.5,
            poll_seconds=0.05,
            grace_seconds=2.0,
        )
    assert timed_out.killed is False, (
        "a timeout recorded itself as an operator abort, so the round it failed "
        "would have been refunded instead of charged"
    )


def test_an_unwired_executor_owns_a_ledger_nothing_ever_writes_to(tmp_path):
    """The fail-closed default said precisely: an executor with no flag path has
    no abort capability, so its ledger stays empty forever and every read of it
    is the behaviour that predates this feature."""
    worker = real_repo(tmp_path, "worker")
    executor = executor_with(
        worker,
        WritingAgent(worker, {"A.py": "x\n"}),
        None,
        command_runner=lambda argv, **kw: _green(),
    )
    assert executor.execute(implement(), a_task()).status == "ok"
    assert executor._abort_ledger.killed is False


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


def aborting_round(
    abort_file, *, rel="A.py", body="x\n" * 30, status=EXECUTION_ABORTED, arm=True
):
    """A round that writes real work into its worker repo and is then aborted —
    the shape `implement_executor` produces when the operator presses the
    button mid-agent.

    `arm=False` is the abort-then-resume race at the dispatch level: the round
    was killed and classified, and by the time the orchestrator looks the flag
    has been cleared. The executor's `EXECUTION_ABORTED` is then the ONLY signal
    left, and it has to be enough.
    """

    def _round(worktree: Path):
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / rel).write_text(body, encoding="utf-8")
        if arm:
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


def test_the_executors_signal_alone_aborts_a_round_whose_flag_is_already_gone(tmp_path):
    """The same two-signal rule read the other way, and the dispatch-level half
    of the abort-then-resume race: the flag was never armed by the time this
    round was classified — `resume` cleared it while the kill was landing — so
    `EXECUTION_ABORTED` is the only thing left saying what happened. Charging
    the attempt here is what would eventually park the task on
    `attempt_count_ceiling` for the operator's own button."""
    abort_file = tmp_path / "ABORT"
    orch, execution_store, repo_root, config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(wr, [aborting_round(abort_file, arm=False)]),
    )
    head_before = run_git(repo_root, "rev-parse", "HEAD").strip()

    orch._dispatch_executor(implement())

    assert abort_requested(config) is False, "the test's own premise"
    execution = execution_store.load("t1")
    assert execution.attempt_count == 0, "an operator's abort spent an attempt"
    assert execution.fault_attempt_count == 0
    assert execution.attempt_ledger == ()
    assert execution.candidate_sha == "", "an aborted round commits nothing"
    assert orch.state.stop_kind == ABORT_STOP_KIND
    assert orch._registry.get("t1").status == "pending", "resumable, not consumed"
    assert run_git(repo_root, "rev-parse", "HEAD").strip() == head_before


def test_a_killed_suite_costs_the_task_no_attempt_once_dispatch_has_seen_it(tmp_path):
    """The BUDGET half of the killed-suite regression, driven end to end.

    `fault_kind == ""` on its own does not prove the budgets are untouched: that
    field is only consulted on the `outcome.status != "ok"` branch, which is the
    branch this fix exists to avoid reaching at all. What actually leaves the
    counters alone is the routing to `_abort_round` and its refund, so this
    asserts the counters themselves — with the flag ABSENT, so `EXECUTION_ABORTED`
    is the only signal left and the executor's own report has to carry the round.

    The outcome is PRODUCED by the real executor rather than written out here.
    An `ExecutionOutcome` typed into this test would prove only that the
    orchestrator handles a shape a test author invented — the echo this file
    grades everywhere else.
    """
    outcome, _launched, _abort_file = a_round_whose_suite_was_killed(tmp_path)
    assert outcome.status == EXECUTION_ABORTED, "the premise this test replays"

    def replay(worktree: Path):
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / "A.py").write_text("x\n" * 30, encoding="utf-8")
        return outcome

    orch, execution_store, repo_root, config = build(
        tmp_path, lambda wr, rr: ScriptedExecutor(wr, [replay])
    )
    head_before = run_git(repo_root, "rev-parse", "HEAD").strip()

    orch._dispatch_executor(implement())

    assert abort_requested(config) is False, "the test's own premise"
    execution = execution_store.load("t1")
    assert execution.attempt_count == 0, "the operator's stop spent the task's budget"
    assert execution.fault_attempt_count == 0, "an abort is not an environment fault"
    assert execution.attempt_ledger == (), "the open entry is removed, not settled"
    assert_books_balance(execution)
    assert execution.candidate_sha == "", "an aborted round commits nothing"
    assert orch.state.stop_kind == ABORT_STOP_KIND
    assert orch._registry.get("t1").status == "pending", "resumable, not consumed"
    assert run_git(repo_root, "rev-parse", "HEAD").strip() == head_before


#: A validation command that ARMS THE ABORT ITSELF, mid-run, and then lingers.
#:
#: The flag has to land WHILE a real validation process group is live, because
#: that is the only moment `killable_run`'s kill branch can be reached — and
#: nothing outside the suite knows when the suite actually started. So the
#: command announces itself (the marker) and presses the button (the flag) from
#: inside its own process. That makes the timing deterministic rather than a
#: sleep this test hopes is long enough, which is the difference between a
#: regression and a flake. It spawns a child FIRST, so what the kill has to
#: reach is a GROUP with a grandchild in it, observable per pid.
ARMS_THE_ABORT_MID_SUITE = f"""
import os, subprocess, sys, time
marker, flag = sys.argv[1], sys.argv[2]
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep({LINGER_SECONDS})"]
)
tmp = marker + ".tmp"
with open(tmp, "w") as handle:
    handle.write("%d %d\\n" % (os.getpid(), child.pid))
os.replace(tmp, marker)
open(flag, "w").close()
time.sleep({LINGER_SECONDS})
"""


def abort_arming_command(tmp_path, marker: Path, abort_file: Path) -> tuple[str, ...]:
    """A real validation command `run_validation_commands` will actually launch.

    TWO things about it are load-bearing rather than incidental.

    `validation.SAFE_VALIDATION_BINARIES` matches on BASENAME, and
    `sys.executable` is commonly `python3.13` — which that allowlist REFUSES
    unrun. A test built on it would assert about a command validation never
    launched: a green-looking run that proves nothing, which is exactly the
    fail-open this file exists to catch. The symlink named `python3` is what
    makes this a command the runner really executes.

    The script goes in a FILE rather than after `-c` so that the argv, and
    therefore the per-command line in `state.last_validation`, stays readable.
    `effective_validation_command` leaves it alone either way (it rewrites only
    a `pytest` invocation), so what runs is what is written here.
    """
    interpreter = tmp_path / "python3"
    if not interpreter.exists():
        interpreter.symlink_to(sys.executable)
    script = tmp_path / "arms_the_abort.py"
    script.write_text(ARMS_THE_ABORT_MID_SUITE, encoding="utf-8")
    return (str(interpreter), str(script), str(marker), str(abort_file))


class NeverRuns:
    """The STANDALONE agent binding, which `worker_repo_root_for` +
    `agent_runner_factory` always beat. It raises rather than writing anything:
    an agent rooted at the main checkout is what `escape_detector` exists to
    catch, and a test that quietly reached one would be building the escape it
    is meant to prove cannot happen."""

    def run(self, spec):  # pragma: no cover - the factory wins in this wiring
        raise AssertionError("the standalone agent binding was reached")


def killed_suite_executor(
    workers_root, repo_root, *, abort_file, ledger, marker, tmp_path
):
    """The REAL `ImplementExecutor`, wired as `cli._build_executor` wires it,
    whose authoritative validation run is a real subprocess group an operator
    kills mid-flight.

    `command_runner` is a WRAPPER AROUND PRODUCTION'S `killable_run`, not a
    stand-in for it, and that is the whole point of this construction: the
    clause the round ends up reporting ("the validation subprocess and every
    process it spawned were killed") is written into the ledger by
    `killable_run` itself. A test that recorded that sentence by hand and then
    asserted on it would be reading back its own input — an echo, and the
    reviewer would be right to refuse it.

    What the wrapper adds is the OPERATOR'S `resume`, at the one deterministic
    moment it can land: after the kill, before the round classifies itself.
    That is the race the ledger exists for, and doing it here rather than from a
    second thread is what keeps this test a regression instead of a coin flip.
    """
    policy = PolicyEngine(PolicyConfig(implement_enabled=True))

    def resume_after_the_kill(argv, **kwargs):
        proc = killable_run(
            argv,
            abort_file=abort_file,
            ledger=ledger,
            poll_seconds=0.05,
            grace_seconds=2.0,
            **kwargs,
        )
        if abort_file.exists():
            abort_file.unlink()
        return proc

    return ImplementExecutor(
        git=GitGateway(repo_root, policy),
        agent_runner=NeverRuns(),
        validation_commands=(abort_arming_command(tmp_path, marker, abort_file),),
        command_runner=resume_after_the_kill,
        worker_repo_root_for=lambda task_id: Path(workers_root) / task_id,
        policy=policy,
        agent_runner_factory=lambda root: WritingAgent(root, {"A.py": "x\n" * 30}),
        abort_file=abort_file,
        abort_ledger=ledger,
    )


def test_a_killed_suite_is_carried_end_to_end_without_charging_the_task(tmp_path):
    """THE REGRESSION the revision asked for, run END TO END rather than stitched.

    `test_a_killed_suite_costs_the_task_no_attempt_once_dispatch_has_seen_it`
    above produces a real outcome and then REPLAYS it through a scripted
    executor. That proves both halves, but it proves them separately, and the
    seam is precisely where the claim could still fail: nothing in it shows the
    real executor's outcome reaching the real dispatch path in one run.

    So this is one continuous run of the production pieces:

        the agent writes real work into its worker repo
        -> the authoritative suite launches a real process group
        -> that group arms the abort flag from inside itself, mid-suite
        -> `killable_run` kills the group and records the ledger
        -> `resume` removes the flag BEFORE the round classifies itself
        -> `_run_implementation` classifies from the LEDGER
        -> `_dispatch_task_postcommit` routes to `_abort_round`

    With the flag gone by classification time, `abort_requested(config)` is
    False and `EXECUTION_ABORTED` is the only signal left carrying the round —
    which is the direction that costs a task an attempt when it is wrong, and
    enough of those are the `attempt_count_ceiling` this task exists to prevent.

    Every constraint the task names is asserted here against that one run: the
    process group dies (grandchild included), no attempt and no fault is
    charged, mainline is untouched, the task is back in the queue, and what was
    discarded is reported and measured.
    """
    abort_file = tmp_path / "ABORT"
    marker = tmp_path / "suite-pids.txt"
    ledger = AbortLedger()
    orch, execution_store, repo_root, config = build(
        tmp_path,
        lambda workers_root, repo: killed_suite_executor(
            workers_root,
            repo,
            abort_file=abort_file,
            ledger=ledger,
            marker=marker,
            tmp_path=tmp_path,
        ),
    )
    head_before = run_git(repo_root, "rev-parse", "HEAD").strip()
    pids: tuple[int, ...] = ()

    try:
        orch._dispatch_executor(implement())

        # KILL THE WHOLE PROCESS GROUP, proved per pid rather than inferred from
        # the one handle the runner happened to hold.
        assert marker.exists(), "the authoritative suite never launched"
        parent_pid, child_pid = pids = read_pids(marker)
        assert wait_until(lambda: not alive(parent_pid)), (
            "the validation command outlived the abort"
        )
        assert wait_until(lambda: not alive(child_pid)), (
            "a worker the validation command spawned outlived the abort"
        )
    finally:
        reap(*pids)

    # The premise: by the time the round was classified there was no flag left
    # to read, so nothing below can be passing because of one.
    assert abort_requested(config) is False, "`resume` never ran — premise broken"
    assert ledger.killed, "the round classified itself from something else"

    execution = execution_store.load("t1")
    assert execution.attempt_count == 0, "the operator's stop spent the task's budget"
    assert execution.fault_attempt_count == 0, "an abort is not an environment fault"
    assert execution.attempt_ledger == (), "the open entry is removed, not settled"
    assert_books_balance(execution)
    assert execution.candidate_sha == "", "an aborted round commits nothing"
    assert orch.state.stop_kind == ABORT_STOP_KIND
    assert orch._registry.get("t1").status == "pending", "resumable, not consumed"
    assert run_git(repo_root, "rev-parse", "HEAD").strip() == head_before

    # REPORT WHAT WAS DISCARDED — and the clause naming what was killed is
    # PRODUCTION's. Nothing in this test writes it into the ledger; the only
    # writer on this path is `killable_run`'s own kill branch, which is what
    # makes this an assertion about the code rather than about the fixture.
    record = orch.state.aborted_round
    assert record["partial_work_measured"] is True
    assert "validation subprocess" in record["discarded_work"], record["discarded_work"]
    assert "1 file(s) changed" in record["discarded_work"], record["discarded_work"]
    assert "validation failed after implementation" not in record["discarded_work"]
    # The suite LAUNCHED and was cut short — a different fact from the
    # "Validation did not run" every pre-validation abort site reports.
    assert "cut short by the abort" in record["discarded_work"]


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


def arm(config) -> Path:
    flag = abort_flag_file(config)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    return flag


def test_an_abort_is_refused_in_so_many_words_while_a_packet_is_outstanding(tmp_path):
    """NEVER ABORT IN `submitting` OR `awaiting`. A review packet is outstanding
    there and killing the step strands an approved push.

    THE REVISION (abort-01, 2026-08-26): this used to return `ABORTED`, which is
    what an abort that KILLED something returns — so the operator asked for a
    kill, got `pause`'s boundary stop, and nothing anywhere said the two had
    differed. A refusal nobody is told about is a guard that has switched itself
    off. `ABORT_REFUSED` is its own value precisely so no caller can confuse
    them, and the transcript records the refusal with its reason.

    What is NOT refused is the stop itself: the loop returns between steps,
    having killed nothing, with the phase and the pending request intact for the
    resume. `_step_awaiting` is never entered — the stub client would raise if it
    were, which is what makes "nothing ran" evidence rather than assertion.
    """
    pending = PendingRequest(request_id="alr-1", payload="the packet", submitted=True)
    orch, config = loop_at(tmp_path, Phase.AWAITING, pending=pending)
    arm(config)

    assert orch.run() == ABORT_REFUSED
    assert ABORT_REFUSED != ABORTED, "a refusal that returns the abort value says nothing"

    assert orch.state.phase == Phase.AWAITING.value, "the phase must survive"
    assert orch.state.pending_request.request_id == "alr-1", "the packet must survive"
    assert orch.state.stop_kind != ABORT_STOP_KIND, "no round was killed, so none is recorded"
    assert orch.state.aborted_round is None
    records = [
        line for line in config.transcript_file.read_text(encoding="utf-8").splitlines()
        if "abort_refused" in line
    ]
    assert records, "the refusal must be recorded, not merely performed"
    assert "awaiting" in records[0]


@pytest.mark.parametrize("phase", sorted(PACKET_OUTSTANDING_PHASES, key=lambda p: p.value))
def test_every_packet_phase_refuses_the_kill_rather_than_reporting_an_abort(
    tmp_path, phase
):
    """The whole set, not the two the constraint happened to name. `delivering`
    is mid-deposit of a chunked payload and the two `submission_*` phases are
    sends whose acceptance is unknown or disproved — each strands something a
    later reconciliation cannot classify, which is why they are one set shared
    with `cli._shelve_session_refusal` rather than a list per verb."""
    orch, config = loop_at(tmp_path, phase)
    arm(config)

    assert orch.run() == ABORT_REFUSED
    assert orch.state.phase == phase.value
    assert orch.state.aborted_round is None


def test_a_request_that_outlived_its_phase_refuses_the_kill_too(tmp_path):
    """Guard 1's second half, and not a hypothetical: a request OUTLIVES its own
    phase, which is why `cli._shelve_session_refusal` checks it separately. A
    session sitting in `ready` while a reviewer still holds a request is a
    session that can strand one, whatever its phase says."""
    pending = PendingRequest(request_id="alr-9", payload="the packet", submitted=True)
    orch, config = loop_at(tmp_path, Phase.READY, pending=pending)
    arm(config)

    assert orch.run() == ABORT_REFUSED
    assert orch.state.pending_request.request_id == "alr-9"


@pytest.mark.parametrize("phase", [Phase.READY, Phase.EXECUTING])
def test_the_loop_stops_between_steps_from_any_live_phase(tmp_path, phase):
    """The other side of the refusal: where NO packet is outstanding the abort is
    an abort. `executing` is the phase the whole verb exists for, and it can
    never be refused by the pending-request half of the guard —
    `_step_awaiting` clears `pending_request` in the same save that moves the
    phase to `executing`."""
    orch, config = loop_at(tmp_path, phase)
    arm(config)

    assert orch.run() == ABORTED
    assert orch.state.phase == phase.value


@pytest.mark.parametrize(
    "state,expected",
    [
        (None, "no readable session"),
        (object(), "unrecognised phase"),
    ],
)
def test_the_packet_predicate_refuses_what_it_cannot_read(state, expected):
    """FAIL-CLOSED, and into the harmless direction: an unreadable session and a
    phase this build does not know both refuse the KILL rather than answering
    "no packet outstanding" and performing one. Answering the other way is the
    fail-open this file exists to grade — the guard silently passing when what it
    needs is absent."""
    assert expected in packet_outstanding_reason(state)


def test_the_packet_predicate_is_silent_where_a_kill_is_safe():
    state = LoopState.new(URL)
    state.phase = Phase.EXECUTING.value
    assert packet_outstanding_reason(state) == ""


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


def save_session(config, phase, *, pending=None) -> None:
    state = LoopState.new(URL)
    state.phase = phase.value
    state.pending_request = pending
    StateStore(config.state_file).save(state)


def test_abort_warns_when_the_phase_it_lands_in_will_refuse_the_kill(cli_config, capsys):
    """The operator is told BEFORE they go looking for a dead agent that will
    not be there. Still armed, though — see the next test for why refusing to
    arm on a snapshot would be the worse failure."""
    config = cli.load_config(cli_config)
    save_session(config, Phase.AWAITING)

    assert cli._cmd_abort(argparse.Namespace(config=cli_config)) == 0

    printed = capsys.readouterr().out
    assert "will be REFUSED" in printed
    assert "review packet is outstanding" in printed
    assert "`pause` is the verb" in printed
    assert abort_requested(config) is True, "the warning is not a refusal to arm"


def test_abort_arms_the_flag_anyway_because_the_phase_it_read_is_a_snapshot(
    cli_config, capsys
):
    """WHY the warning is not a refusal. The operator who most needs this verb is
    the one whose loop entered `executing` and started a four-hour agent call one
    second after `state.json` was written. Deciding from that file would drop
    exactly that request — silently — which is the failure the verb exists to
    remove."""
    config = cli.load_config(cli_config)
    save_session(config, Phase.EXECUTING)

    cli._cmd_abort(argparse.Namespace(config=cli_config))

    printed = capsys.readouterr().out
    assert abort_requested(config) is True
    assert "REFUSED" not in printed, "a kill is performed here; nothing is refused"


def test_abort_never_reports_a_refusal_it_cannot_actually_read(cli_config, capsys):
    """FAIL-CLOSED IN THE PREDICATE, honest in the report, and the two are not the
    same thing. `packet_outstanding_reason(None)` refuses — correctly, for the
    loop's own guard — but this process may simply be unable to open a state file
    that a perfectly healthy loop is holding in memory and killing an agent from.
    Printing "the kill will be refused" there would be this command inventing a
    fact about a loop it cannot see."""
    config = cli.load_config(cli_config)
    assert not config.state_file.exists(), "the fixture ships no session"

    cli._cmd_abort(argparse.Namespace(config=cli_config))

    printed = capsys.readouterr().out
    assert "REFUSED" not in printed
    assert "could not be read" in printed and "unknown" in printed
    assert abort_requested(config) is True


def test_the_operator_report_for_a_refusal_says_nothing_was_killed(capsys):
    """The counterpart to `_report_abort`, and the reason the loop has a return
    value of its own: an operator who asked for a kill and got a boundary stop
    has to be told which one they got."""
    state = LoopState.new(URL)
    state.phase = Phase.AWAITING.value

    cli._report_abort_refused(state)

    printed = capsys.readouterr().out
    assert "ABORT REFUSED" in printed
    assert "killed       nothing" in printed
    assert "packet" in printed
    assert cli._is_abort_stop(state) is False, (
        "a refusal writes no stop_kind, so the abort report must stay silent"
    )


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
