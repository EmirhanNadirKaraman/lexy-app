"""Two attempt budgets, and the rule that decides which one a round spends.

`attempt_count` bounds a task's own unproductive churn. Until 2026-08-17 it
also paid for every round destroyed by something the task could not have
avoided — a provider 429, an agent the stall supervisor killed, a process that
died mid-round — and six tasks (brw-09, exec-01, port-01, brw-11, dash-04,
hlth-01) reached the ceiling on rounds a reviewer had never even seen. An
operator repaired each one by editing the execution record by hand, and an
external watcher script had to GUESS which attempts were faults by comparing
`attempt_count` against `review_round`, because nothing recorded the answer.

What this file pins:

  * a fault does not spend `attempt_count` — it spends `fault_attempt_count`;
  * a completed failure of the task's own work still does, including a
    structural refusal, which is the judgement call this task had to make and
    defend (see `MAX_TASK_ATTEMPTS`' comment);
  * reconciliation after a restart cannot turn a finished task attempt into a
    fault — only a round nothing ever stamped;
  * CONSECUTIVE session-ending faults never alternate back into the task
    budget — the defect the first cut of this shipped with, where a redo was
    written into the ledger as already-settled and so stopped being
    recognisable as a round with a review in flight;
  * a recovery chain the environment interrupts REPEATEDLY stays on the fault
    budget for as long as it is still recovering the same lost review, however
    the interruption arrives — and ends the moment a round reaches a reviewer
    or fails on the task's own merits;
  * BOTH budgets terminate, so nothing here removes a bound;
  * the record says, per attempt, which budget was charged and why.

Self-contained per this codebase's convention (see `test_m1_hardening.py`'s
docstring) — real git repos, no fixtures imported from other test modules.
"""

from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

import pytest

from autoloop import cli
from autoloop.audit.agents import (
    AGENT_FAULT_PROVIDER,
    AGENT_FAULT_STALL,
    AgentResult,
    classify_agent_fault,
)
from autoloop.blockers import Blocker, BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig, load_config
from autoloop.contract import Decision, Directive
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    MAX_TASK_ATTEMPTS,
    MAX_TASK_FAULT_ATTEMPTS,
    Orchestrator,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore, utcnow_iso
from autoloop.stall import StallReport
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore, mutation_ledger_for
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import (
    ATTEMPT_FAULT,
    ATTEMPT_PENDING,
    ATTEMPT_PENDING_FAULT,
    ATTEMPT_TASK,
    IntentStore,
    TaskExecution,
    TaskExecutionStore,
    attempt_outcome,
    compose_reason,
    format_attempt,
    split_attempt,
)

URL = "https://chatgpt.com/c/attempt-budget-test"


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


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def implement(task_id="t1") -> Directive:
    return Directive(decision=Decision.IMPLEMENT, reason="go", task_id=task_id)


def revise(task_id="t1", feedback="please fix it") -> Directive:
    return Directive(
        decision=Decision.REVISE, reason="not quite", task_id=task_id, feedback=feedback
    )


def build(tmp_path, executor_factory, approved_paths=("A.py",), task_id="t1"):
    """Real-git, `WorkerRepoManager`-backed Orchestrator — the production
    dispatch path, because the attempt accounting lives inside it. Returns the
    pieces every test here reaches for."""
    repo_root = real_repo(tmp_path)
    # Mirrors the real repo's `.gitignore`: without it `.al/state.json` reads as
    # an untracked dirty path and trips `primary_checkout_dirty` for a reason
    # unrelated to anything under test (see docs/COMMON_ERRORS.md).
    (repo_root / ".gitignore").write_text(".al/\n__pycache__/\n*.py[cod]\n", encoding="utf-8")
    run_git(repo_root, "add", ".gitignore")
    run_git(repo_root, "commit", "-q", "-m", "gitignore state dir")

    workers_root = tmp_path / "workers_root"
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig(implement_enabled=True)))
    worker_repos = WorkerRepoManager(workers_root, tmp_path / "worker-hooks")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    intent_store = IntentStore(tmp_path / "intents")
    blocker_store = BlockerStore(tmp_path / "blockers")

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=repo_root / ".al",
        workers_root=workers_root,
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)
    task = Task(id=task_id, title="T", description="d", approved_paths=tuple(approved_paths))
    registry = TaskRegistry([task])
    task_store = TaskStore(
        config.tasks_file,
        ledger=mutation_ledger_for(config.workers_root, config.state_dir),
    )
    task_store.save(registry)

    def no_client():
        raise AssertionError("no browser client expected in this test")

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
        worker_repos=worker_repos,
        execution_store=execution_store,
        intent_store=intent_store,
        blocker_store=blocker_store,
        validation_runner=ok_validation,
    )
    return orch, execution_store, blocker_store, task, config


class ScriptedExecutor:
    """One `ExecutionOutcome` (or exception) per round, in order.

    A callable entry is invoked with the worker repo path so a round can write
    real files before returning; anything else is returned as-is, and a
    `BaseException` instance is raised (modelling a round the process does not
    survive)."""

    def __init__(self, workers_root, rounds):
        self.workers_root = Path(workers_root)
        self.rounds = list(rounds)
        self.calls = 0

    def execute(self, directive, task):
        step = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        if callable(step):
            return step(self.workers_root / task.id)
        if isinstance(step, BaseException):
            raise step
        return step


def writes(rel="A.py", body="content\n", status="ok", **kwargs):
    """A round that really writes into its worker repo, so a commit has
    something to stage."""

    def _round(worktree: Path):
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return ExecutionOutcome(
            status=status,
            summary=f"wrote {rel}",
            validation="ok",
            changed_paths=(rel,),
            **kwargs,
        )

    return _round


def ledger(execution) -> list[tuple[str, str]]:
    """`(budget, reason)` per attempt, ordinals checked to be 1..N in order —
    an out-of-order or duplicated ordinal would mean the ledger no longer
    describes rounds one-to-one, which every assertion here relies on."""
    parsed = [split_attempt(entry) for entry in execution.attempt_ledger]
    assert [ordinal for ordinal, _, _ in parsed] == list(range(1, len(parsed) + 1))
    return [(budget, reason) for _, budget, reason in parsed]


def assert_books_balance(execution) -> None:
    """The invariant that makes the combined bound exact: every ledger entry is
    charged to exactly one counter, so the two counters sum to its length."""
    assert (
        execution.attempt_count + execution.fault_attempt_count
        == len(execution.attempt_ledger)
    ), (
        f"attempt_count={execution.attempt_count} "
        f"fault_attempt_count={execution.fault_attempt_count} "
        f"ledger={execution.attempt_ledger}"
    )


# =============================================================================
# 1. the executor reports a fault: the task's budget is not touched
# =============================================================================


def test_a_rate_limited_round_that_produced_no_work_does_not_spend_an_attempt(tmp_path):
    """exec-01's measured shape: two rounds died to the agent provider's
    session-limit 429 and "produced no work", yet each consumed an attempt
    exactly like a failing implementation would have."""
    rate_limited = ExecutionOutcome(
        status="error",
        summary="task 't1': implementation agent failed — API error 429 rate limit",
        validation="not run",
        fault_kind=AGENT_FAULT_PROVIDER,
    )
    orch, execution_store, _blockers, task, _config = build(
        tmp_path, lambda wr, rr: ScriptedExecutor(wr, [rate_limited])
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution.attempt_count == 0, "the task's own budget must be untouched"
    assert execution.fault_attempt_count == 1
    assert ledger(execution) == [(ATTEMPT_FAULT, AGENT_FAULT_PROVIDER)]
    assert_books_balance(execution)
    # ...and the round still ends the ordinary way: back to ready with a report,
    # not parked. Exempting the budget is not the same as hiding the failure.
    assert orch.state.phase == Phase.READY.value


def test_an_agent_killed_by_the_stall_supervisor_does_not_spend_an_attempt(tmp_path):
    killed = ExecutionOutcome(
        status="error",
        summary="task 't1': implementation agent failed — no progress for 900s",
        validation="not run",
        fault_kind=AGENT_FAULT_STALL,
    )
    orch, execution_store, _blockers, task, _config = build(
        tmp_path, lambda wr, rr: ScriptedExecutor(wr, [killed])
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution.attempt_count == 0
    assert execution.fault_attempt_count == 1
    assert ledger(execution) == [(ATTEMPT_FAULT, AGENT_FAULT_STALL)]


def test_a_completed_validation_failure_still_spends_the_task_attempt_budget(tmp_path):
    """The control, and the reason the exemption is safe. `status="error"`
    covers four different things; only an agent stopped by the provider or the
    supervisor is a fault. A round that ran to completion and failed its own
    validation is the task's work being wrong, and it must keep costing the
    task's budget — otherwise nothing bounds a task that can never pass."""
    orch, execution_store, _blockers, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr,
            [
                ExecutionOutcome(
                    status="error",
                    summary="task 't1': validation failed after implementation",
                    validation="ruff: E501",
                    # fault_kind deliberately absent — the executor did not, and
                    # must not, name this environmental.
                )
            ],
        ),
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution.attempt_count == 1
    assert execution.fault_attempt_count == 0
    assert ledger(execution) == [(ATTEMPT_TASK, "executor_reported_failure")]
    assert_books_balance(execution)


# =============================================================================
# 2. the chosen rule for structural refusals — CHARGED, and pinned to it
# =============================================================================


def test_a_structural_refusal_spends_the_task_attempt_budget(tmp_path):
    """THE decision this task had to make and defend.

    A structural refusal (post-commit verification failing) is a round the
    reviewer never judged, which is the argument for exempting it. It is
    charged anyway, and deliberately: it is a genuine defect in a candidate
    this task's own work produced, and `MAX_TASK_ATTEMPTS` exists precisely to
    stop a task refusing structurally over and over without bound. Exempting it
    would have removed the only ceiling on that case — the correction to this
    task's brief says so in as many words.

    Pinned here rather than left implicit so a future change that quietly moves
    refusals onto the fault budget fails a test instead of passing review.
    """
    def failing_validation(argv, **kwargs):
        class Proc:
            returncode = 1
            stdout = "1 failed"
            stderr = ""

        return Proc()

    orch, execution_store, blocker_store, task, _config = build(
        tmp_path, lambda wr, rr: ScriptedExecutor(wr, [writes()])
    )
    orch._validation_runner = failing_validation

    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert blocker_store.load(orch.state.park_blocker_id).code == (
        "post_commit_verification_failed"
    )
    execution = execution_store.load(task.id)
    assert execution.attempt_count == 1, "a structural refusal is the task's own cost"
    assert execution.fault_attempt_count == 0
    assert ledger(execution) == [(ATTEMPT_TASK, "post_commit_verification_failed")]


# =============================================================================
# 3. a round that reached the reviewer — the case the budget exists for
# =============================================================================


def test_a_round_that_reaches_the_reviewer_and_comes_back_revise_spends_attempts(tmp_path):
    orch, execution_store, _blockers, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr, [writes("A.py", "round 1\n"), writes("A.py", "round 2\n")]
        ),
    )

    orch._dispatch_executor(implement(task.id))
    after_one = execution_store.load(task.id)
    assert after_one.review_round == 1
    assert after_one.attempt_count == 1
    assert ledger(after_one) == [(ATTEMPT_TASK, "sent_for_review")]

    # The reviewer came back `revise`: the redo is exactly what this budget is
    # meant to bound, so it is charged.
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(revise(task.id, "tighten the naming"))

    after_two = execution_store.load(task.id)
    assert after_two.review_round == 2
    assert after_two.attempt_count == 2
    assert after_two.fault_attempt_count == 0
    assert ledger(after_two) == [
        (ATTEMPT_TASK, "sent_for_review"),
        (ATTEMPT_TASK, "sent_for_review"),
    ]
    assert_books_balance(after_two)


# =============================================================================
# 4. a round the process did not survive — reconciled, and only that
# =============================================================================


def test_a_round_the_process_did_not_survive_is_reclassified_as_a_fault(tmp_path):
    """An operator pause or restart mid-round, a kill, a crash inside the agent.

    The attempt was charged before the executor ran (that is deliberate and
    unchanged — see `_open_attempt`), and nothing stamped it, because the
    process never reached any of the round's exits. The next dispatch settles
    it: that is a round which produced no reviewable outcome, so the fault
    budget pays for it, not the task's.
    """
    orch, execution_store, _blockers, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr, [KeyboardInterrupt("operator stopped the loop"), writes()]
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        orch._dispatch_executor(implement(task.id))

    interrupted = execution_store.load(task.id)
    assert interrupted.attempt_count == 1, "charged up front, as before"
    assert ledger(interrupted) == [(ATTEMPT_PENDING, "dispatched")], (
        "an unfinished round is recorded as OPEN — that is what makes it "
        "distinguishable from one that finished"
    )

    # A later process re-dispatches the same task.
    orch.state.phase = Phase.READY.value
    orch.state.last_response = None
    orch._dispatch_executor(implement(task.id))

    settled = execution_store.load(task.id)
    assert settled.attempt_count == 1, "only the NEW round is the task's own"
    assert settled.fault_attempt_count == 1
    assert ledger(settled) == [
        (ATTEMPT_FAULT, "interrupted_mid_round"),
        (ATTEMPT_TASK, "sent_for_review"),
    ]
    assert_books_balance(settled)


def test_reconciliation_never_reclassifies_a_finished_task_attempt(tmp_path):
    """The guard the accounting stands on.

    If reconciliation could reach a round that already finished, a task failing
    validation five times over could have every one of them refunded on the
    next restart and churn forever. It cannot: the only entries it touches are
    ones that positively read `pending`, and every exit of a dispatched round
    stamps its entry. Here three finished rounds — a failure, a fault and a
    review — survive a restart with their classifications and both counters
    exactly as they were.
    """
    orch, execution_store, _blockers, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr,
            [
                ExecutionOutcome(status="error", summary="validation failed", validation="fail"),
                ExecutionOutcome(
                    status="error", summary="429", validation="not run",
                    fault_kind=AGENT_FAULT_PROVIDER,
                ),
                writes(),
            ],
        ),
    )
    for _ in range(3):
        orch.state.phase = Phase.READY.value
        orch._dispatch_executor(implement(task.id))

    before = execution_store.load(task.id)
    assert ledger(before) == [
        (ATTEMPT_TASK, "executor_reported_failure"),
        (ATTEMPT_FAULT, AGENT_FAULT_PROVIDER),
        (ATTEMPT_TASK, "sent_for_review"),
    ]
    assert (before.attempt_count, before.fault_attempt_count) == (2, 1)

    # Exactly what a restart runs before reading either ceiling, against the
    # record as it stands on disk.
    orch._reconcile_unfinished_attempts(execution_store.load(task.id))

    after = execution_store.load(task.id)
    assert ledger(after) == ledger(before)
    assert (after.attempt_count, after.fault_attempt_count) == (2, 1)


def test_finalising_an_attempt_is_one_way_and_cannot_be_re_stamped(tmp_path):
    """The same guard at the unit level: nothing downstream of a round can
    relabel what that round already cost, in either direction."""
    orch, execution_store, _blockers, task, _config = build(
        tmp_path, lambda wr, rr: ScriptedExecutor(wr, [writes()])
    )
    execution = TaskExecution(
        task_id=task.id,
        task_branch="autoloop/t1",
        worktree_path="/tmp/wt",
        task_base_sha="a" * 40,
        attempt_count=1,
        attempt_ledger=(format_attempt(1, ATTEMPT_TASK, "executor_reported_failure"),),
    )
    execution_store.save(execution)

    orch._finalise_attempt(execution, ATTEMPT_FAULT, "provider_rate_limited")

    assert execution.attempt_count == 1
    assert execution.fault_attempt_count == 0
    assert ledger(execution) == [(ATTEMPT_TASK, "executor_reported_failure")]


# =============================================================================
# 5. a session-ending fault destroys a review the task had already earned
# =============================================================================


def test_a_session_ending_browser_fault_charges_the_redo_to_the_fault_budget(tmp_path):
    """brw-11's shape: the lint fix was already committed and passing, and three
    rounds were lost to faults anyway. The round itself is a real task attempt —
    it produced work — but the review it earned died with the session, so the
    round has to be performed again, and that redo is the fault's cost."""
    orch, execution_store, _blockers, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr, [writes("A.py", "round 1\n"), writes("A.py", "round 2\n")]
        ),
    )
    orch._dispatch_executor(implement(task.id))
    assert ledger(execution_store.load(task.id)) == [(ATTEMPT_TASK, "sent_for_review")]

    orch._note_round_fault("browser_session_lost")
    marked = execution_store.load(task.id)
    assert marked.pending_fault_code == "browser_session_lost"

    # The next session redoes the round.
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement(task.id))

    redone = execution_store.load(task.id)
    assert redone.attempt_count == 1, "the redo did not spend the task's budget"
    assert redone.fault_attempt_count == 1
    assert ledger(redone) == [
        (ATTEMPT_TASK, "sent_for_review"),
        # Both halves are recorded: WHY the round had to be redone, and WHAT the
        # redo achieved. The second half is what a later fault reads.
        (ATTEMPT_FAULT, "browser_session_lost>sent_for_review"),
    ]
    assert redone.pending_fault_code == "", "consumed exactly once"
    assert_books_balance(redone)


def test_consecutive_session_ending_faults_never_fall_back_onto_the_task_budget(tmp_path):
    """THE regression this section exists for, and the defect the first cut of
    budget-01 shipped with.

    A redo used to be written straight into the ledger as a SETTLED
    `fault|<code>` entry. When that redo went on to reach the reviewer, the
    round's own exit had nothing left to stamp, so the entry never said a review
    had been in flight — and the NEXT session-ending fault, which recognised
    only the literal pair `(task, "sent_for_review")`, silently declined to mark
    it. Its redo was then charged to `attempt_count`. Two faults in a row
    alternated straight back into the budget this whole task exists to protect.

    Three dispatches, two session-ending faults between them, and `attempt_count`
    must be 1 throughout: only the first round was ever the task's own work.
    """
    orch, execution_store, _blockers, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr,
            [
                writes("A.py", "round 1\n"),
                writes("A.py", "round 2\n"),
                writes("A.py", "round 3\n"),
            ],
        ),
    )

    orch._dispatch_executor(implement(task.id))
    first = execution_store.load(task.id)
    assert (first.attempt_count, first.fault_attempt_count) == (1, 0)
    assert ledger(first) == [(ATTEMPT_TASK, "sent_for_review")]

    # Fault #1 kills the session with that review in flight.
    orch._note_round_fault("browser_session_lost")
    assert execution_store.load(task.id).pending_fault_code == "browser_session_lost"

    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement(task.id))
    second = execution_store.load(task.id)
    assert second.attempt_count == 1, "the first redo did not spend the task's budget"
    assert second.fault_attempt_count == 1
    assert ledger(second)[-1] == (ATTEMPT_FAULT, "browser_session_lost>sent_for_review")
    assert_books_balance(second)

    # Fault #2, with the redo's own review in flight. The last ledger entry is
    # on the FAULT budget this time — recognising it anyway is the fix.
    orch._note_round_fault("provider_rate_limited")
    marked = execution_store.load(task.id)
    assert marked.pending_fault_code == "provider_rate_limited", (
        "a review reached by a fault-charged redo is still a review in flight"
    )

    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement(task.id))

    third = execution_store.load(task.id)
    assert third.attempt_count == 1, (
        "neither fault touched the task's budget — the round that reached "
        "review on its own merits is still the only thing charged to it"
    )
    assert third.fault_attempt_count == 2
    assert ledger(third) == [
        (ATTEMPT_TASK, "sent_for_review"),
        (ATTEMPT_FAULT, "browser_session_lost>sent_for_review"),
        (ATTEMPT_FAULT, "provider_rate_limited>sent_for_review"),
    ], "the ledger still names every round's origin and outcome"
    assert third.pending_fault_code == ""
    assert third.review_round == 3
    assert_books_balance(third)


def test_a_redo_that_fails_on_its_own_merits_goes_back_onto_the_task_budget(tmp_path):
    """The narrowing that keeps the redo exemption from laundering failures.

    A round opened on the fault budget stays there only when it achieves what
    the fault destroyed — it reaches the reviewer. If it instead comes back
    structurally refused, that is a fresh defect no reviewer has seen, and it is
    charged to the task exactly as the same refusal would be on any other round.
    Without this, a task could keep failing post-commit verification for free as
    long as a fault had preceded it.
    """
    def failing_validation(argv, **kwargs):
        class Proc:
            returncode = 1
            stdout = "1 failed"
            stderr = ""

        return Proc()

    orch, execution_store, blocker_store, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr, [writes("A.py", "round 1\n"), writes("A.py", "round 2\n")]
        ),
    )
    orch._dispatch_executor(implement(task.id))
    orch._note_round_fault("browser_session_lost")

    orch._validation_runner = failing_validation
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement(task.id))

    refused = execution_store.load(task.id)
    assert blocker_store.load(orch.state.park_blocker_id).code == (
        "post_commit_verification_failed"
    )
    assert refused.attempt_count == 2, "the refusal is the task's own cost"
    assert refused.fault_attempt_count == 0, "the provisional fault charge moved back"
    assert ledger(refused) == [
        (ATTEMPT_TASK, "sent_for_review"),
        # The origin is still recorded — the round happened because of a fault —
        # but the budget it spent is the task's, because of how it ended.
        (ATTEMPT_TASK, "browser_session_lost>post_commit_verification_failed"),
    ]
    assert refused.pending_fault_code == "", (
        "and the recovery chain ENDS here. A redo that produced a real defect "
        "hands the task something to fix, so the next round is the task's own "
        "work — carrying the marker past it would excuse rounds nothing "
        "environmental ever touched"
    )
    assert_books_balance(refused)


def test_a_redo_the_process_does_not_survive_keeps_its_replacement_on_the_fault_budget(
    tmp_path,
):
    """Reconciliation settles a redo without moving its charge, AND carries the
    recovery forward.

    The dispatch already put the redo on the fault budget; dying mid-round is
    another fault, so there is nothing to move — only a stamp to add, so the
    entry stops reading as in-flight. What used to be missing is the other half:
    the review that redo existed to recover is STILL lost, so the dispatch after
    it is still recovery work. This test asserted the opposite in budget-01's
    first cut ("only the fresh round is the task's own", `attempt_count == 2`) —
    the gap written down rather than closed, so one environmental interruption
    was absorbed and the next one billed the task. `_settle_attempt` rule 4
    re-arms `pending_fault_code` from the redo's own origin, so the replacement
    dispatch stays on the fault budget."""
    orch, execution_store, _blockers, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr,
            [
                writes("A.py", "round 1\n"),
                RuntimeError("died mid-redo"),
                writes("A.py", "round 3\n"),
            ],
        ),
    )
    orch._dispatch_executor(implement(task.id))
    orch._note_round_fault("provider_quota_exhausted")

    orch.state.phase = Phase.READY.value
    with pytest.raises(RuntimeError):
        orch._dispatch_executor(implement(task.id))

    unfinished = execution_store.load(task.id)
    assert ledger(unfinished)[-1] == (
        ATTEMPT_PENDING_FAULT,
        "provider_quota_exhausted",
    ), "a redo is OPEN while it runs, exactly like any other round"
    assert (unfinished.attempt_count, unfinished.fault_attempt_count) == (1, 1)

    orch.state.phase = Phase.READY.value
    orch.state.last_response = None
    orch._dispatch_executor(implement(task.id))

    settled = execution_store.load(task.id)
    assert settled.attempt_count == 1, (
        "the task's own budget still holds only the one round it really spent — "
        "the replacement for an interrupted redo is still the fault's cost"
    )
    assert settled.fault_attempt_count == 2, (
        "the redo's charge did not move, and its replacement added one"
    )
    assert ledger(settled) == [
        (ATTEMPT_TASK, "sent_for_review"),
        (ATTEMPT_FAULT, "provider_quota_exhausted>interrupted_mid_round"),
        # Same origin, so the two entries read as one recovery chain rather than
        # as two unrelated incidents; the outcomes are what differ.
        (ATTEMPT_FAULT, "provider_quota_exhausted>sent_for_review"),
    ]
    assert settled.pending_fault_code == "", (
        "the chain ends when a round finally reaches the reviewer — no marker "
        "survives to excuse the round after it"
    )
    assert_books_balance(settled)


def test_a_recovery_chain_interrupted_twice_never_reaches_the_task_budget(tmp_path):
    """THE end-to-end shape, with BOTH ways an environment can take a redo.

    One review earned, then nothing but interruptions until a round finally
    gets through:

      1. round 1 commits and reaches the reviewer            → `task`
      2. the session dies with that review in flight         → redo armed
      3. the redo's process dies mid-round                   → `fault`, still armed
      4. the next redo's agent hits a provider outage        → `fault`, still armed
      5. the redo after that reaches the reviewer            → `fault`, disarmed

    Steps 3 and 4 arrive at `_settle_attempt` from DIFFERENT callers —
    `_reconcile_unfinished_attempts` after a restart, `_finalise_attempt` on the
    round's own exit — which is why the rule lives in the one method they share
    rather than at either call site. Both are the same event to this accounting:
    the environment took the round, the review is still lost, the next dispatch
    is still recovering it.

    `attempt_count` must be 1 at every step. Round 1 is the only work this task
    has ever been asked to account for.
    """
    orch, execution_store, _blockers, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr,
            [
                writes("A.py", "round 1\n"),
                RuntimeError("the loop was restarted mid-redo"),
                ExecutionOutcome(
                    status="error",
                    summary="task 't1': implementation agent failed — API error 429",
                    validation="not run",
                    fault_kind=AGENT_FAULT_PROVIDER,
                ),
                writes("A.py", "round 4\n"),
            ],
        ),
    )

    # 1 — a real round, reviewed.
    orch._dispatch_executor(implement(task.id))
    assert execution_store.load(task.id).review_round == 1

    # 2 — the session dies before the verdict comes back.
    orch._note_round_fault("browser_session_lost")

    # 3 — the redo's own process does not survive.
    orch.state.phase = Phase.READY.value
    with pytest.raises(RuntimeError):
        orch._dispatch_executor(implement(task.id))
    crashed = execution_store.load(task.id)
    assert (crashed.attempt_count, crashed.fault_attempt_count) == (1, 1)

    # 4 — a restart reconciles that round, and the replacement is dispatched on
    # the fault budget (the gap this closes: it used to be dispatched on the
    # task's). Its agent then hits the provider.
    orch.state.phase = Phase.READY.value
    orch.state.last_response = None
    orch._dispatch_executor(implement(task.id))
    throttled = execution_store.load(task.id)
    assert throttled.attempt_count == 1, (
        "a redo the process did not survive must not bill the task for its "
        "replacement — the review it was recovering is still lost"
    )
    assert throttled.fault_attempt_count == 2
    assert throttled.pending_fault_code == "browser_session_lost", (
        "and the chain is STILL armed: the second interruption did not produce "
        "a review either"
    )

    # 5 — the next dispatch finally gets through to the reviewer.
    orch.state.phase = Phase.READY.value
    orch.state.last_response = None
    orch._dispatch_executor(implement(task.id))

    final = execution_store.load(task.id)
    assert final.attempt_count == 1, (
        "unchanged from step 1 through four dispatches — the task budget paid "
        "for exactly the one round the task itself ran"
    )
    assert final.fault_attempt_count == 3
    assert final.fault_attempt_count < MAX_TASK_FAULT_ATTEMPTS, (
        "bounded, and visibly so: every excused dispatch was charged somewhere"
    )
    assert final.review_round == 2, "one lost review, recovered once"
    assert final.pending_fault_code == "", (
        "no stale recovery marker survives a successful review — the next "
        "round after this one is the task's own again"
    )
    assert_books_balance(final)

    # Auditable, from disk, without inferring anything from a counter gap: one
    # origin, four outcomes, in dispatch order.
    on_disk = json.loads(execution_store._path(task.id).read_text(encoding="utf-8"))
    assert on_disk["attempt_ledger"] == [
        f"1|{ATTEMPT_TASK}|sent_for_review",
        f"2|{ATTEMPT_FAULT}|browser_session_lost>interrupted_mid_round",
        f"3|{ATTEMPT_FAULT}|browser_session_lost>{AGENT_FAULT_PROVIDER}",
        f"4|{ATTEMPT_FAULT}|browser_session_lost>sent_for_review",
    ]


def test_a_recovery_chain_interrupted_forever_still_hits_the_fault_ceiling(tmp_path):
    """The bound on the rule above, and the direct answer to "do not solve this
    with an unbounded sticky exemption".

    Carrying `pending_fault_code` forward spares the TASK budget; it spares no
    budget at all. Each replacement dispatch consumes a `fault_attempt_count`
    charge exactly like the fault that started the chain, so a recovery
    interrupted every single time walks into `fault_attempt_ceiling` and parks
    for an operator instead of redoing itself forever.
    """
    orch, execution_store, blocker_store, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr, [writes("A.py", "round 1\n"), RuntimeError("died mid-redo, again")]
        ),
    )
    orch._dispatch_executor(implement(task.id))
    orch._note_round_fault("browser_session_lost")

    # Every one of these is a redo of the SAME lost review, and every one dies.
    for expected in range(1, MAX_TASK_FAULT_ATTEMPTS + 1):
        orch.state.phase = Phase.READY.value
        orch.state.last_response = None
        with pytest.raises(RuntimeError):
            orch._dispatch_executor(implement(task.id))
        assert execution_store.load(task.id).fault_attempt_count == expected

    orch.state.phase = Phase.READY.value
    orch.state.last_response = None
    calls_before = orch._executor.calls
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"
    assert blocker_store.load(orch.state.park_blocker_id).code == "fault_attempt_ceiling"
    assert orch._executor.calls == calls_before, (
        "the ceiling fired before dispatch — the chain terminates rather than "
        "granting itself one more round"
    )

    parked = execution_store.load(task.id)
    assert parked.attempt_count == 1, "still only round 1"
    assert parked.fault_attempt_count == MAX_TASK_FAULT_ATTEMPTS
    assert len(parked.attempt_ledger) <= MAX_TASK_ATTEMPTS + MAX_TASK_FAULT_ATTEMPTS - 1
    assert parked.pending_fault_code == "browser_session_lost", (
        "the marker outlives the park on purpose: that review is still lost, so "
        "if an operator grants a fresh fault allowance the recovery resumes on "
        "the fault budget rather than starting to bill the task"
    )
    assert_books_balance(parked)


def test_a_session_ending_fault_marks_nothing_when_no_review_was_in_flight(tmp_path):
    """The narrowing that keeps this from handing out free attempts. A fault
    while the task had no earned review interrupted nothing it can be credited
    for — an unfinished round is already covered by reconciliation, and a task
    that was nowhere near a dispatch has no round to redo."""
    orch, execution_store, _blockers, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr, [ExecutionOutcome(status="error", summary="validation failed", validation="x")]
        ),
    )
    orch._dispatch_executor(implement(task.id))

    orch._note_round_fault("browser_session_lost")

    execution = execution_store.load(task.id)
    assert execution.pending_fault_code == ""
    assert ledger(execution) == [(ATTEMPT_TASK, "executor_reported_failure")]


def test_note_round_fault_is_a_no_op_with_nothing_in_flight(tmp_path):
    """It runs inside failure handlers, so it must never be the thing that
    raises: no execution record, no task in flight, nothing to do."""
    orch, execution_store, _blockers, task, _config = build(
        tmp_path, lambda wr, rr: ScriptedExecutor(wr, [writes()])
    )
    orch.state.task_execution = None

    orch._note_round_fault("provider_rate_limited")  # must not raise

    assert execution_store.load(task.id) is None


# =============================================================================
# 6. both budgets terminate — nothing here removed a bound
# =============================================================================


def test_the_fault_budget_terminates_a_task_that_faults_every_round(tmp_path):
    """The answer to "an exemption would remove the only bound on a task that
    crashes every round". It does not: faults are bounded too, on their own
    ceiling, with a blocker that names which wall was hit."""
    fault = ExecutionOutcome(
        status="error", summary="429", validation="not run",
        fault_kind=AGENT_FAULT_PROVIDER,
    )
    orch, execution_store, blocker_store, task, _config = build(
        tmp_path, lambda wr, rr: ScriptedExecutor(wr, [fault])
    )

    for expected in range(1, MAX_TASK_FAULT_ATTEMPTS + 1):
        orch.state.phase = Phase.READY.value
        orch._dispatch_executor(implement(task.id))
        execution = execution_store.load(task.id)
        assert execution.fault_attempt_count == expected
        assert execution.attempt_count == 0
        assert orch.state.phase == Phase.READY.value

    orch.state.phase = Phase.READY.value
    calls_before = orch._executor.calls
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"
    assert orch._executor.calls == calls_before, "the ceiling fired before dispatch"
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert blocker.code == "fault_attempt_ceiling"
    # The blocker carries the per-attempt record, so an operator reads why each
    # round was charged instead of inferring it from a counter gap.
    assert AGENT_FAULT_PROVIDER in blocker.detail
    execution = execution_store.load(task.id)
    assert execution.fault_attempt_count == MAX_TASK_FAULT_ATTEMPTS
    assert_books_balance(execution)


def test_a_task_whose_process_dies_every_round_still_terminates(tmp_path):
    """The specific case the pre-executor increment was protecting, re-checked
    against the new accounting: nothing ever stamps these rounds, so every one
    reconciles onto the fault budget — and that budget ends."""
    orch, execution_store, blocker_store, task, _config = build(
        tmp_path, lambda wr, rr: ScriptedExecutor(wr, [RuntimeError("died mid-round")])
    )

    # Each round: dispatched, charged, then the process dies before any exit
    # stamps it. The NEXT dispatch settles the previous one onto the fault
    # budget, which is why the last of these still runs rather than parking —
    # its own entry has not been settled yet.
    for _ in range(MAX_TASK_FAULT_ATTEMPTS):
        orch.state.phase = Phase.READY.value
        with pytest.raises(RuntimeError):
            orch._dispatch_executor(implement(task.id))

    orch.state.phase = Phase.READY.value
    calls_before = orch._executor.calls
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert blocker_store.load(orch.state.park_blocker_id).code == "fault_attempt_ceiling"
    assert orch._executor.calls == calls_before, "refused before the executor ran"

    execution = execution_store.load(task.id)
    assert execution.attempt_count == 0, "none of it was the task's own work"
    assert execution.fault_attempt_count == MAX_TASK_FAULT_ATTEMPTS
    assert len(execution.attempt_ledger) <= MAX_TASK_ATTEMPTS + MAX_TASK_FAULT_ATTEMPTS - 1


def test_the_task_budget_still_terminates_a_task_that_fails_its_own_work(tmp_path):
    """Unchanged behaviour, restated against the ledger: five completed
    failures still reach `attempt_count_ceiling`, and the record now says all
    five were the task's own."""
    orch, execution_store, blocker_store, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr, [ExecutionOutcome(status="error", summary="nope", validation="fail")]
        ),
    )

    for expected in range(1, MAX_TASK_ATTEMPTS + 1):
        orch.state.phase = Phase.READY.value
        orch._dispatch_executor(implement(task.id))
        assert execution_store.load(task.id).attempt_count == expected

    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert blocker_store.load(orch.state.park_blocker_id).code == "attempt_count_ceiling"
    execution = execution_store.load(task.id)
    assert execution.fault_attempt_count == 0
    assert ledger(execution) == [(ATTEMPT_TASK, "executor_reported_failure")] * 5


# =============================================================================
# 7. the record says why — per attempt, on disk
# =============================================================================


def test_the_record_states_per_attempt_which_budget_was_charged_and_why(tmp_path):
    """The whole point of the ledger. `~/.autoloop/afk-worker.sh` had to guess
    with `attempt_count - review_round >= 2` because the record did not say;
    a mixed history now reads back exactly, from disk, in dispatch order."""
    orch, execution_store, _blockers, task, _config = build(
        tmp_path,
        lambda wr, rr: ScriptedExecutor(
            wr,
            [
                ExecutionOutcome(
                    status="error", summary="429", validation="not run",
                    fault_kind=AGENT_FAULT_PROVIDER,
                ),
                ExecutionOutcome(status="error", summary="tests failed", validation="fail"),
                writes(),
            ],
        ),
    )
    for _ in range(3):
        orch.state.phase = Phase.READY.value
        orch._dispatch_executor(implement(task.id))

    on_disk = json.loads((execution_store._path(task.id)).read_text(encoding="utf-8"))
    assert on_disk["attempt_ledger"] == [
        f"1|{ATTEMPT_FAULT}|{AGENT_FAULT_PROVIDER}",
        f"2|{ATTEMPT_TASK}|executor_reported_failure",
        f"3|{ATTEMPT_TASK}|sent_for_review",
    ]
    assert on_disk["attempt_count"] == 2
    assert on_disk["fault_attempt_count"] == 1


def test_the_ledger_round_trips_and_an_older_record_still_loads(tmp_path):
    store = TaskExecutionStore(tmp_path / "executions")
    execution = TaskExecution(
        task_id="t1",
        task_branch="autoloop/t1",
        worktree_path="/tmp/wt",
        task_base_sha="a" * 40,
        attempt_count=1,
        fault_attempt_count=1,
        attempt_ledger=(
            format_attempt(1, ATTEMPT_FAULT, "provider_rate_limited"),
            format_attempt(2, ATTEMPT_TASK, "sent_for_review"),
        ),
        pending_fault_code="browser_session_lost",
    )
    store.save(execution)
    loaded = store.load("t1")
    assert loaded == execution
    assert isinstance(loaded.attempt_ledger, tuple)

    # A record written before either field existed. Its `attempt_count` is
    # honoured as-is: a missing ledger is NOT read as "those were all faults",
    # which would retroactively refund a budget nobody can audit.
    path = store._path("old")
    path.write_text(
        json.dumps(
            {
                "task_id": "old",
                "task_branch": "autoloop/old",
                "worktree_path": "/tmp/old",
                "task_base_sha": "b" * 40,
                "attempt_count": 4,
            }
        ),
        encoding="utf-8",
    )
    legacy = store.load("old")
    assert legacy.attempt_count == 4
    assert legacy.attempt_ledger == ()
    assert legacy.fault_attempt_count == 0
    assert legacy.pending_fault_code == ""


def test_split_attempt_is_tolerant_of_a_hand_edited_entry():
    """Fail closed on the way in: an entry nobody can parse must read as
    neither `pending` nor a charge, so reconciliation leaves it alone rather
    than refunding it."""
    assert split_attempt("3|task|sent_for_review") == (3, "task", "sent_for_review")
    assert split_attempt("garbage") == (0, "", "")
    assert split_attempt("") == (0, "", "")
    ordinal, budget, reason = split_attempt("x|fault|why")
    assert (ordinal, budget, reason) == (0, ATTEMPT_FAULT, "why")
    # Reasons keep any interior text after the second separator intact.
    assert split_attempt("1|task|a|b")[2] == "a|b"


def test_a_reason_carries_a_redos_origin_without_hiding_its_outcome():
    """The accounting keys on the OUTCOME, so a reason that also names why the
    round had to happen must still read back to the same outcome. Getting this
    wrong is what let a second consecutive fault go unnoticed."""
    assert compose_reason("browser_session_lost", "sent_for_review") == (
        "browser_session_lost>sent_for_review"
    )
    # No origin — an ordinary round's entry is unchanged, byte for byte.
    assert compose_reason("", "sent_for_review") == "sent_for_review"

    assert attempt_outcome("sent_for_review") == "sent_for_review"
    assert attempt_outcome("browser_session_lost>sent_for_review") == "sent_for_review"
    # A composed reason survives a round-trip through the entry format.
    entry = format_attempt(2, ATTEMPT_FAULT, compose_reason("x", "sent_for_review"))
    assert attempt_outcome(split_attempt(entry)[2]) == "sent_for_review"
    # Nothing to split reads as itself, which is the fail-closed direction: it
    # matches no outcome the accounting acts on.
    assert attempt_outcome("") == ""
    assert attempt_outcome("garbage") == "garbage"


# =============================================================================
# 8. classifying an agent failure — narrow, structured, fail-closed
# =============================================================================


def _agent_result(error="", stall=None):
    return AgentResult(
        domain="t1",
        raw_text="",
        returncode=1,
        duration_seconds=1.0,
        command=("claude",),
        error=error,
        stall=stall,
    )


def test_classify_agent_fault_names_the_supervisor_kill_and_provider_refusals():
    report = StallReport(
        verdict="stalled",
        elapsed_seconds=930.0,
        silent_seconds=900.0,
        stall_seconds=900.0,
        ceiling_seconds=14400.0,
    )
    assert classify_agent_fault(_agent_result(error="killed", stall=report)) == (
        AGENT_FAULT_STALL
    )
    for error in (
        "API Error: 429 Too Many Requests",
        "you have hit your usage limit for this session",
        "Overloaded",
        "quota exceeded for this organization",
        "service unavailable, please retry",
    ):
        assert classify_agent_fault(_agent_result(error=error)) == AGENT_FAULT_PROVIDER, error


def test_classify_agent_fault_defaults_to_the_task_owning_its_own_failure():
    """The load-bearing default. A wrongly-blank fault costs one attempt; a
    wrongly-set one would excuse a genuine failure forever."""
    for error in (
        "non-zero exit (1) with no output on stderr or stdout",
        "AssertionError at line 429 of test_thing.py",   # a bare code is NOT enough
        "502 tests collected, 3 failed",
        "ruff: 12 errors",
        "",
    ):
        assert classify_agent_fault(_agent_result(error=error)) == "", error


# =============================================================================
# 9. answering the fault ceiling grants a fresh allowance — and only that
# =============================================================================


def test_answering_a_fault_ceiling_blocker_clears_only_the_fault_budget(tmp_path, capsys):
    """Without this, answering the park achieves nothing: the counter is still
    at the cap, so the next dispatch parks on the identical wall — the exact
    mechanical hand-repair this task exists to remove. It must NOT reach
    `attempt_count`, which bounds the task's own churn."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nstate_dir = "{tmp_path / ".al"}"\n'
        f'workers_root = "{tmp_path / "workers_root"}"\n',
        encoding="utf-8",
    )
    config = load_config(config_path)

    registry = TaskRegistry([Task(id="t1", title="T", description="d", approved_paths=("A.py",))])
    registry.block("t1", "fault attempt ceiling reached")
    TaskStore(config.tasks_file).save(registry)

    executions = TaskExecutionStore(config.executions_dir)
    executions.save(
        TaskExecution(
            task_id="t1",
            task_branch="autoloop/t1",
            worktree_path="/tmp/wt",
            task_base_sha="a" * 40,
            candidate_sha="c" * 40,
            review_round=2,
            last_revise_feedback="tighten the naming",
            attempt_count=2,
            fault_attempt_count=MAX_TASK_FAULT_ATTEMPTS,
            attempt_ledger=tuple(
                format_attempt(n, ATTEMPT_FAULT, "provider_rate_limited")
                for n in range(1, MAX_TASK_FAULT_ATTEMPTS + 1)
            ),
        )
    )
    blockers = BlockerStore(config.blockers_dir)
    blocker = Blocker(
        id=blockers.next_id("t1"),
        task_id="t1",
        kind="task_fatal",
        code="fault_attempt_ceiling",
        question="t1 lost five rounds to faults",
        detail="",
        phase="executing",
        created_at=utcnow_iso(),
    )
    blockers.save(blocker)

    rc = cli._cmd_answer(
        Namespace(config=config_path, blocker_id=blocker.id, text="the network is fine now")
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "fault attempt budget for t1 reset" in out

    repaired = executions.load("t1")
    assert repaired.fault_attempt_count == 0
    assert repaired.attempt_count == 2, "the task's own budget is NOT refilled"
    # Everything an operator used to preserve by hand is preserved by the tool.
    assert repaired.candidate_sha == "c" * 40
    assert repaired.review_round == 2
    assert repaired.last_revise_feedback == "tighten the naming"
    assert len(repaired.attempt_ledger) == MAX_TASK_FAULT_ATTEMPTS, (
        "history preserved — the reset grants a new allowance, it does not "
        "erase what was spent"
    )
    assert TaskStore(config.tasks_file).load().state_of("t1") is TaskState.READY


def test_the_fault_budget_is_reset_even_when_the_task_was_never_quarantined(tmp_path, capsys):
    """Plain `run` (unlike `--continuous`) parks task_fatal without going
    through `cli._handle_parked_task`, so the task is never
    BLOCKED_BY_OPERATOR and `registry.unblock` raises. The reset must still
    happen — running it after the unblock left the counter at the cap in
    exactly the case it exists for."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nstate_dir = "{tmp_path / ".al"}"\n'
        f'workers_root = "{tmp_path / "workers_root"}"\n',
        encoding="utf-8",
    )
    config = load_config(config_path)
    registry = TaskRegistry([Task(id="t1", title="T", description="d", approved_paths=("A.py",))])
    TaskStore(config.tasks_file).save(registry)  # READY, never blocked

    executions = TaskExecutionStore(config.executions_dir)
    executions.save(
        TaskExecution(
            task_id="t1",
            task_branch="autoloop/t1",
            worktree_path="/tmp/wt",
            task_base_sha="a" * 40,
            attempt_count=1,
            fault_attempt_count=MAX_TASK_FAULT_ATTEMPTS,
        )
    )
    blockers = BlockerStore(config.blockers_dir)
    blocker = Blocker(
        id=blockers.next_id("t1"),
        task_id="t1",
        kind="task_fatal",
        code="fault_attempt_ceiling",
        question="t1 lost five rounds to faults",
        detail="",
        phase="executing",
        created_at=utcnow_iso(),
    )
    blockers.save(blocker)

    assert cli._cmd_answer(
        Namespace(config=config_path, blocker_id=blocker.id, text="network is fine")
    ) == 0
    out = capsys.readouterr().out
    assert "could not be unblocked" in out  # the raising path really was taken

    repaired = executions.load("t1")
    assert repaired.fault_attempt_count == 0
    assert repaired.attempt_count == 1


def test_answering_an_attempt_count_ceiling_does_not_refill_the_task_budget(tmp_path):
    """The other half of the narrowing. `attempt_count` is the bound on a task
    failing its own work; refilling it on a keystroke would hand out five more
    rounds of the same failure."""
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nstate_dir = "{tmp_path / ".al"}"\n'
        f'workers_root = "{tmp_path / "workers_root"}"\n',
        encoding="utf-8",
    )
    config = load_config(config_path)
    registry = TaskRegistry([Task(id="t1", title="T", description="d", approved_paths=("A.py",))])
    registry.block("t1", "attempt ceiling")
    TaskStore(config.tasks_file).save(registry)

    executions = TaskExecutionStore(config.executions_dir)
    executions.save(
        TaskExecution(
            task_id="t1",
            task_branch="autoloop/t1",
            worktree_path="/tmp/wt",
            task_base_sha="a" * 40,
            attempt_count=MAX_TASK_ATTEMPTS,
            fault_attempt_count=1,
        )
    )
    blockers = BlockerStore(config.blockers_dir)
    blocker = Blocker(
        id=blockers.next_id("t1"),
        task_id="t1",
        kind="task_fatal",
        code="attempt_count_ceiling",
        question="t1 hit the attempt ceiling",
        detail="",
        phase="executing",
        created_at=utcnow_iso(),
    )
    blockers.save(blocker)

    assert cli._cmd_answer(
        Namespace(config=config_path, blocker_id=blocker.id, text="try again")
    ) == 0

    untouched = executions.load("t1")
    assert untouched.attempt_count == MAX_TASK_ATTEMPTS
    assert untouched.fault_attempt_count == 1
