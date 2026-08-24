"""A round the environment destroys must not leave its task unscheduled AND
unreported.

THE CLAIM these tests grade: after a round ends in an environment fault, its
task is either back in the pool `next_ready()` draws from, or an OPEN BLOCKER
names it and says why it is not. There is no third state.

There was one until strand-01. On 2026-08-22, between 22:36:54Z and 22:46:27Z,
fourteen consecutive rounds died carrying `terminal_reason=api_error`,
`num_turns=1`, `duration_api_ms=0` — the implementation agent never started.
Six tasks were dispatched into that window and the recovery was inconsistent:
two finished, one returned to the queue, and three (scope-05 P1, contract-01,
recov-01) sat `in_progress` for twenty-one hours with `review_round: 0`,
`candidate_sha: ""`, `attempt_count: 0`, `fault_attempt_count: 1`.
`next_ready()` returns READY tasks and an `in_progress` task is not one, so the
loop never re-offered them; no blocker was filed; the dashboard showed work in
flight. They were found only because an unrelated analysis listed in-progress
tasks, and an operator released all three by hand.

What is pinned here, in the order the claim needs it:

  * the requeue arm — a stranded task is in `next_ready()` again, and the
    assertion is `next_ready()` itself, never `status == "pending"` (they are
    not the same question);
  * the SAFE SET is narrow and checkable — a candidate, a reviewed round, an
    unreadable record and a spent budget each produce a blocker instead, and
    none of them mutates anything;
  * neither attempt budget is refilled by a requeue, which is what keeps the
    bound below exact;
  * the BOUND — an outage that faults every round ends at
    `fault_attempt_ceiling`, it does not cycle the roadmap through a dead API;
  * the task whose round is genuinely in flight is never swept, so the reviewer
    keeps the redo that recovered two of the six tasks on its own — and that
    exemption is itself BOUNDED by the round ceiling, because
    `state.task_execution` is replaced only by the next dispatch, so an
    unconditional one would exclude a lone faulted task from this sweep forever
    while `next_ready()` also refused it: the same third state, one level up;
  * both arms are written to the transcript with the task id and the fault
    code, because the twenty-one hours were bought by silence.

Self-contained per this codebase's convention (see `test_m1_hardening.py`'s
docstring) — real git repos, no fixtures imported from other test modules.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autoloop import health
from autoloop.audit.agents import AGENT_FAULT_PROVIDER
from autoloop.blockers import STRANDED_AFTER_FAULT, BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.errors import TaskGraphError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.lock import LoopLock
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    MAX_TASK_ATTEMPTS,
    MAX_TASK_FAULT_ATTEMPTS,
    Orchestrator,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore, mutation_ledger_for
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import (
    ATTEMPT_FAULT,
    ATTEMPT_PENDING,
    ATTEMPT_TASK,
    IntentStore,
    TaskExecution,
    TaskExecutionStore,
    format_attempt,
)

URL = "https://chatgpt.com/c/strand-recovery-test"

# The incident's own fault code shape: the agent process came back having done
# nothing, and the executor named the cause.
API_ERROR = AGENT_FAULT_PROVIDER


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


def stamp_aged(age_seconds: float) -> str:
    """A dispatch stamp `age_seconds` old, in `state.utcnow_iso`'s own shape.

    Ages are built by moving the STAMP rather than by injecting a clock: the
    production readers (`health.current_round_age_seconds`, and
    `dashboard._elapsed_seconds` beside it) take their `now` from the wall
    clock in every real call, so a test that replaced it would stop exercising
    the parse that turns a recorded string back into an age.
    """
    return (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()


def config_with_ceiling(value):
    """The one field `health.round_ceiling_for` reads, set to `value`.

    A stand-in rather than a real `AutoloopConfig`, because the shapes under
    test are ones the dataclass refuses to hold — a missing section, a string
    where a float belongs — and those are exactly what a hand-edited config
    file delivers.
    """
    return type("Cfg", (), {"audit": type("Audit", (), {"agent_ceiling_seconds": value})()})()


class Wiring:
    """Everything a test here reaches for, built once around a real repo."""

    def __init__(self, orch, config, registry, task_store, executions, blockers):
        self.orch = orch
        self.config = config
        self.registry = registry
        self.task_store = task_store
        self.executions = executions
        self.blockers = blockers

    # -- convenience reads ----------------------------------------------------

    def status(self, task_id: str) -> str:
        return self.registry.get(task_id).status

    def open_blockers(self):
        return self.blockers.open_blockers()

    def transcript(self) -> list[dict]:
        path = self.config.transcript_file
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def entries(self, entry_type: str) -> list[dict]:
        return [e for e in self.transcript() if e.get("type") == entry_type]

    def moved_on_to(self, task_id: str | None) -> None:
        """The loop is now working something else — one of the two ways an
        in-progress task is shown to have been abandoned (the other is
        `working_on(..., age_seconds=)` past the round ceiling)."""
        self.orch.state.task_execution = (
            None if task_id is None else {"task_id": task_id}
        )

    def working_on(self, task_id: str, age_seconds: float = 0.0) -> None:
        """The loop's own claim to be running a round for `task_id`, dispatched
        `age_seconds` ago.

        BOTH halves of it, because the age is only readable when they agree:
        `_dispatch_executor` stamps `state.current_task` and
        `_dispatch_task_postcommit` writes `state.task_execution`, and
        `health.current_round_age_seconds` refuses a stamp that belongs to a
        different dispatch.
        """
        self.orch.state.current_task = {
            "task_id": task_id,
            "title": f"T {task_id}",
            "decision": "implement",
            "started_at": stamp_aged(age_seconds),
        }
        self.orch.state.task_execution = {"task_id": task_id}

    def persist_state(self) -> None:
        """What the loop does at dispatch (`_dispatch_task_postcommit`), and
        what `health.check` reads instead of the in-memory object."""
        StateStore(self.config.state_file).save(self.orch.state)

    def ceiling(self) -> float:
        """The bound under test, read from the config rather than restated: a
        test carrying its own copy of the number would keep passing after the
        one the loop uses moved."""
        return health.round_ceiling_for(self.config)


class ScriptedExecutor:
    """One `ExecutionOutcome` per round, the last one repeating."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0

    def execute(self, directive, task):
        step = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        return step


def fault_outcome(kind=API_ERROR) -> ExecutionOutcome:
    """The measured incident shape: the agent never ran, nothing was produced,
    and the executor POSITIVELY named the cause as environmental."""
    return ExecutionOutcome(
        status="error",
        summary="task: implementation agent failed — API error (no turns)",
        validation="not run",
        fault_kind=kind,
    )


def build(tmp_path, rounds=None, tasks=("t1",)) -> Wiring:
    """Real-git, `WorkerRepoManager`-backed Orchestrator — the production
    dispatch path, because both the attempt accounting and the strand sweep
    live inside it.

    Every store is rooted where `AutoloopConfig` says, not beside it: the
    health survey reads `config.executions_dir` / `config.tasks_file` while the
    orchestrator holds the objects, and a test where those two disagree would
    prove nothing about the pair.
    """
    repo_root = real_repo(tmp_path)
    (repo_root / ".gitignore").write_text(".al/\n__pycache__/\n*.py[cod]\n", encoding="utf-8")
    run_git(repo_root, "add", ".gitignore")
    run_git(repo_root, "commit", "-q", "-m", "gitignore state dir")

    workers_root = tmp_path / "workers_root"
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=repo_root / ".al",
        workers_root=workers_root,
    )
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig(implement_enabled=True)))
    execution_store = TaskExecutionStore(config.executions_dir)
    blocker_store = BlockerStore(config.blockers_dir)

    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)
    registry = TaskRegistry(
        [Task(id=t, title=f"T {t}", description="d", approved_paths=("A.py",)) for t in tasks]
    )
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
        executor=ScriptedExecutor(rounds if rounds is not None else [fault_outcome()]),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worker_repos=WorkerRepoManager(workers_root, config.worker_hooks_dir),
        execution_store=execution_store,
        intent_store=IntentStore(config.intents_dir),
        blocker_store=blocker_store,
        validation_runner=ok_validation,
    )
    return Wiring(orch, config, registry, task_store, execution_store, blocker_store)


def record_for(
    wiring: Wiring,
    task_id="t1",
    *,
    ledger=((ATTEMPT_FAULT, API_ERROR),),
    candidate_sha="",
    review_round=0,
    attempt_count=0,
    fault_attempt_count=1,
    published_sha="",
) -> TaskExecution:
    """A task marked in-progress with a hand-written execution record.

    Used where the SHAPE of the record is what is under test (a candidate, a
    reviewed round, a spent budget): driving a real commit to produce one would
    test the commit path, not the predicate reading it.
    """
    wiring.registry.mark_in_progress(task_id)
    wiring.task_store.save(wiring.registry)
    execution = TaskExecution(
        task_id=task_id,
        task_branch=f"autoloop/{task_id}",
        worktree_path=str(wiring.config.workers_root / task_id),
        task_base_sha="0" * 40,
        candidate_sha=candidate_sha,
        review_round=review_round,
        attempt_count=attempt_count,
        fault_attempt_count=fault_attempt_count,
        published_sha=published_sha,
        attempt_ledger=tuple(
            format_attempt(i + 1, budget, reason)
            for i, (budget, reason) in enumerate(ledger)
        ),
    )
    wiring.executions.save(execution)
    return execution


# =============================================================================
# 1. the requeue arm — the task is back in the pool `next_ready()` draws from
# =============================================================================


def test_a_task_stranded_by_an_environment_fault_is_back_in_the_pool(tmp_path):
    """The whole incident, end to end and through the production dispatch
    path: the round faults, the loop moves on to another task, and the sweep
    puts the first one back where `next_ready()` can see it."""
    wiring = build(tmp_path, tasks=("t1", "t2"))

    wiring.orch._dispatch_executor(implement("t1"))
    assert wiring.status("t1") == "in_progress"
    wiring.orch.state.phase = Phase.READY.value
    wiring.orch._dispatch_executor(implement("t2"))  # the loop moves on

    wiring.orch._reconcile_stranded_tasks()

    # THE claim's own words, and deliberately not `status == "pending"`:
    # `state_of` reports BLOCKED for a pending task whose dependency is
    # incomplete, so the two questions differ and only this one is the pool.
    assert wiring.registry.next_ready().id == "t1"
    assert wiring.registry.state_of("t1") is TaskState.READY
    # ...and the task the loop is actually working is untouched.
    assert wiring.status("t2") == "in_progress"
    assert not wiring.open_blockers()


def test_the_requeue_is_durable_not_only_in_memory(tmp_path):
    """The registry object the orchestrator holds is not the file `next_ready`
    is read from on the next process. If the save is skipped the strand comes
    straight back."""
    wiring = build(tmp_path)
    record_for(wiring)
    wiring.moved_on_to("something-else")

    wiring.orch._reconcile_stranded_tasks()

    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.next_ready().id == "t1"


def test_the_sweep_runs_before_the_packet_that_asks_what_is_next(tmp_path):
    """The wiring, and the reason it is at the TOP of `_step_ready`:
    `build_context` reads `next_ready()` while building the packet, so a task
    released one line earlier appears in the very message that asks the
    reviewer what to do next — not one round later."""
    wiring = build(tmp_path)
    record_for(wiring)
    wiring.moved_on_to("something-else")
    wiring.orch.state.outbox = "a report for the reviewer"

    wiring.orch._step_ready()

    assert wiring.registry.next_ready().id == "t1"
    assert "t1" in (wiring.orch.state.pending_request.prompt or "")


def test_an_interrupted_round_that_never_stamped_itself_is_a_strand(tmp_path):
    """The second environmental shape. An entry still reading OPEN means the
    round never reached one of its own exits — a process that did not survive,
    or a `GitError` that escaped the dispatch — which is what
    `_reconcile_unfinished_attempts` already defines as a fault. It is reported
    under the same slug that reconciliation will settle it with."""
    wiring = build(tmp_path)
    record_for(
        wiring,
        ledger=((ATTEMPT_PENDING, "dispatched"),),
        fault_attempt_count=0,
    )
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.registry.next_ready().id == "t1"
    assert wiring.entries("task_strand_requeued")[0]["data"]["fault_code"] == (
        health.FAULT_INTERRUPTED
    )


# =============================================================================
# 2. the safe set is narrow — everything else is REPORTED, never touched
# =============================================================================


def _assert_reported_not_touched(wiring: Wiring, task_id="t1") -> None:
    """The shape every unsafe arm must produce: an OPEN blocker naming the
    task, the status exactly where it was, and nothing rewritten."""
    assert wiring.status(task_id) == "in_progress", "an unsafe strand is never moved"
    open_blockers = wiring.open_blockers()
    assert [b.code for b in open_blockers] == [STRANDED_AFTER_FAULT]
    assert open_blockers[0].task_id == task_id
    assert open_blockers[0].kind == "task_fatal"
    blocked = wiring.entries("task_strand_blocked")
    assert [e["data"]["task_id"] for e in blocked] == [task_id]


def test_a_task_holding_a_candidate_is_reported_and_never_touched(tmp_path):
    """The constraint that bounds this whole feature. Archiving an execution
    record with a `candidate_sha` destroys an incoming verdict — that is why
    `release` is an operator command with a warning on it."""
    wiring = build(tmp_path)
    before = record_for(wiring, candidate_sha="a" * 40, review_round=1)
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    _assert_reported_not_touched(wiring)
    after = wiring.executions.load("t1")
    assert after == before, "the record must not be rewritten, archived or renumbered"
    assert "a" * 12 in wiring.open_blockers()[0].detail


def test_a_reviewed_round_alone_is_enough_to_refuse_the_requeue(tmp_path):
    """`candidate_sha` empty AND `review_round == 0` — both halves, not
    either. A record with a review round has been in front of a reviewer, so
    its next move belongs to them."""
    wiring = build(tmp_path)
    record_for(wiring, candidate_sha="", review_round=2)
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    _assert_reported_not_touched(wiring)
    assert "review round 2" in wiring.open_blockers()[0].question


def test_a_spent_fault_budget_is_reported_rather_than_requeued(tmp_path):
    """Requeuing here would buy one dispatch that refuses itself and then the
    strand would be invisible again until something chose the task. The
    blocker is the same answer the ceiling gives, one round earlier."""
    wiring = build(tmp_path)
    record_for(wiring, fault_attempt_count=MAX_TASK_FAULT_ATTEMPTS)
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    _assert_reported_not_touched(wiring)
    assert "budget is already spent" in wiring.open_blockers()[0].question


def test_a_spent_task_budget_is_reported_rather_than_requeued(tmp_path):
    wiring = build(tmp_path)
    record_for(wiring, attempt_count=MAX_TASK_ATTEMPTS, fault_attempt_count=1)
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    _assert_reported_not_touched(wiring)


def test_an_unreadable_execution_record_is_reported_never_read_as_no_strand(tmp_path):
    """The fail-open this must not have. A record that cannot be decoded is
    the one state in which we cannot tell what happened, and reading "cannot
    tell" as "nothing happened" is exactly how a task goes missing quietly."""
    wiring = build(tmp_path)
    record_for(wiring)
    (wiring.config.executions_dir / "t1.json").write_text("{ not json", encoding="utf-8")
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    _assert_reported_not_touched(wiring)
    assert wiring.open_blockers()[0].detail.startswith(
        f"fault_code={health.FAULT_UNREADABLE_RECORD}"
    )


def test_a_registry_refusal_is_reported_rather_than_skipped(tmp_path):
    """A `release` that raises must not become a silent `continue`: the task
    would stay in_progress, out of the pool, with nothing saying so — the very
    third state this exists to abolish."""
    wiring = build(tmp_path)
    record_for(wiring)
    wiring.moved_on_to(None)

    def refuse(task_id):
        raise TaskGraphError("task_not_in_progress", "nope")

    wiring.registry.release = refuse

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.status("t1") == "in_progress"
    assert [b.code for b in wiring.open_blockers()] == [STRANDED_AFTER_FAULT]
    assert "task_not_in_progress" in wiring.open_blockers()[0].question


# =============================================================================
# 3. what is NOT a strand — the quiet cases, which are most of the loop
# =============================================================================


def test_the_task_the_loop_is_working_is_never_swept(tmp_path):
    """The narrowing that keeps the reviewer's redo alive. A round that has
    just faulted is still the current task while its report travels to the
    reviewer, and that redo is how quota-01 and dash-18 recovered on their own
    in the incident. Sweeping here would take the task out from under it.

    Driven through a REAL dispatch, so the dispatch stamp the exemption is
    granted on is the one `_dispatch_executor` actually writes rather than one
    this test invented."""
    wiring = build(tmp_path)

    wiring.orch._dispatch_executor(implement("t1"))
    assert wiring.orch.state.task_execution["task_id"] == "t1"
    assert wiring.orch.state.current_task["task_id"] == "t1"

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.status("t1") == "in_progress"
    assert not wiring.open_blockers()
    assert not wiring.entries("task_strand_requeued")


# =============================================================================
# 3b. ...but that exemption is BOUNDED — the third state, one level up
# =============================================================================
#
# `state.task_execution` is replaced only by the NEXT dispatch. A faulted task
# that nothing else displaces therefore stays "the current task" for as long as
# the session lasts, which is forever when it is the only task there is. An
# unconditional exemption for the current task hands that case straight back
# the defect this whole file is about: nothing schedules it, this sweep skips
# it, and no blocker names it.


def test_a_lone_stranded_task_the_state_still_names_is_requeued_past_the_ceiling(
    tmp_path,
):
    """THE case the bound exists for, with nothing else in the roadmap to
    displace it: one task, faulted, still named by the loop's own state, its
    round older than the ceiling. It goes back in the pool and says so."""
    wiring = build(tmp_path, tasks=("t1",))
    record_for(wiring)
    wiring.working_on("t1", age_seconds=wiring.ceiling() + 60)

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.registry.next_ready().id == "t1"
    entry = wiring.entries("task_strand_requeued")[0]["data"]
    assert entry["task_id"] == "t1"
    assert entry["fault_code"] == API_ERROR
    # The one field that distinguishes this arm from "the loop moved on":
    # `task_execution` still names t1 after the sweep, so the transcript is the
    # only place the difference can be read.
    assert entry["stale_current"] is True
    assert not wiring.open_blockers()
    assert wiring.orch.state.task_execution["task_id"] == "t1", (
        "the sweep moves the STATUS; it does not rewrite the session's state"
    )


def test_a_round_still_inside_the_ceiling_is_left_alone(tmp_path):
    """The other side of the same boundary, and the false alarm that would
    matter: a round the loop is genuinely running has exactly the record shape
    a strand does — an open ledger entry, no candidate, no review round."""
    wiring = build(tmp_path, tasks=("t1",))
    record_for(wiring, ledger=((ATTEMPT_PENDING, "dispatched"),), fault_attempt_count=0)
    wiring.working_on("t1", age_seconds=wiring.ceiling() - 60)

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.status("t1") == "in_progress"
    assert wiring.registry.next_ready() is None
    assert not wiring.open_blockers()
    assert not wiring.entries("task_strand_requeued")


def test_the_ceiling_is_longer_than_the_longest_round_that_can_still_be_running(
    tmp_path,
):
    """WHY sweeping past the bound cannot sweep a live round, stated as a
    number rather than as a feeling: the ceiling is the agent's own hard kill
    (`config.audit.agent_ceiling_seconds`, which `cli._build_executor` passes to
    both implement-agent bindings) plus a grace for the validation/commit/packet
    tail that follows it. A round older than that has already been killed."""
    wiring = build(tmp_path)

    assert wiring.ceiling() > wiring.config.audit.agent_ceiling_seconds
    assert wiring.ceiling() == (
        wiring.config.audit.agent_ceiling_seconds + health.ROUND_CEILING_GRACE_SECONDS
    )


def test_a_misconfigured_ceiling_falls_back_to_the_shipped_one_not_to_zero():
    """The fail-open in the OTHER direction: a ceiling of zero would retire the
    exemption altogether and sweep every live round. `agent_ceiling_seconds` is
    operator-configurable, so an absent, non-numeric or non-positive value
    falls back to the shipped default instead."""
    shipped = health.DEFAULT_ROUND_CEILING_SECONDS

    assert health.round_ceiling_for(None) == shipped
    assert health.round_ceiling_for(config_with_ceiling(None)) == shipped
    assert health.round_ceiling_for(config_with_ceiling("not a number")) == shipped
    assert health.round_ceiling_for(config_with_ceiling(0)) == shipped
    assert health.round_ceiling_for(config_with_ceiling(-1)) == shipped
    assert health.round_ceiling_for(config_with_ceiling(60)) == (
        60 + health.ROUND_CEILING_GRACE_SECONDS
    ), "a configured ceiling IS honoured — the fallbacks above are not the only path"


def test_a_dispatch_stamp_for_a_different_task_is_not_an_exemption(tmp_path):
    """No stamp for THIS round is no evidence, and no evidence is not an
    exemption. The reachable way to get here is a dispatch that died between
    its two writes — `_dispatch_executor` stamps `current_task` before
    `_dispatch_task_postcommit` writes `task_execution` — which leaves the two
    naming different tasks. The task `task_execution` names is then genuinely
    abandoned, so reading "no stamp" as "still running" would strand exactly
    it."""
    wiring = build(tmp_path, tasks=("t1",))
    record_for(wiring)
    wiring.orch.state.current_task = {"task_id": "t2", "started_at": stamp_aged(5)}
    wiring.orch.state.task_execution = {"task_id": "t1"}

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.registry.next_ready().id == "t1"


def test_a_stale_current_strand_outside_the_safe_shape_is_blocked_not_requeued(
    tmp_path,
):
    """The bound widens WHICH tasks are judged, never what is safe to do with
    one. A candidate is still a candidate whoever the state says is current."""
    wiring = build(tmp_path, tasks=("t1",))
    before = record_for(wiring, candidate_sha="a" * 40)
    wiring.working_on("t1", age_seconds=wiring.ceiling() + 60)

    wiring.orch._reconcile_stranded_tasks()

    _assert_reported_not_touched(wiring)
    assert wiring.executions.load("t1") == before
    blocker = wiring.open_blockers()[0]
    assert "still names it as the round in flight" in blocker.question
    assert "stale_current=yes" in blocker.detail
    assert wiring.entries("task_strand_blocked")[0]["data"]["stale_current"] is True


def test_the_stale_current_requeue_happens_once_not_every_round(tmp_path):
    """Idempotence, and it is not an assumption: after the release the task is
    `pending`, so `in_progress_tasks()` — the candidate set the whole predicate
    starts from — no longer returns it at all, even though the state still
    names it."""
    wiring = build(tmp_path, tasks=("t1",))
    record_for(wiring)
    wiring.working_on("t1", age_seconds=wiring.ceiling() + 60)

    for _ in range(3):
        wiring.orch._reconcile_stranded_tasks()

    assert [t.id for t in wiring.registry.in_progress_tasks()] == []
    assert len(wiring.entries("task_strand_requeued")) == 1
    assert not wiring.open_blockers()


def test_the_bound_still_holds_when_the_loop_never_moves_on(tmp_path):
    """The dispatch loop, on the new arm. Same proof as the outage test below,
    with the loop never dispatching anything else: every requeued round is
    still charged to `fault_attempt_count`, which the requeue never resets, so
    the stale-current arm walks into the same ceiling and stops there instead
    of cycling one task through a dead API forever."""
    wiring = build(tmp_path, tasks=("t1",))

    for expected in range(1, MAX_TASK_FAULT_ATTEMPTS + 1):
        wiring.orch.state.phase = Phase.READY.value
        wiring.orch._dispatch_executor(implement("t1"))
        assert wiring.executions.load("t1").fault_attempt_count == expected
        # The loop moves on to NOTHING: its own state still names t1, and only
        # the round ceiling says that claim is stale.
        wiring.orch.state.current_task["started_at"] = stamp_aged(wiring.ceiling() + 60)
        wiring.orch._reconcile_stranded_tasks()
        if expected < MAX_TASK_FAULT_ATTEMPTS:
            assert wiring.registry.next_ready().id == "t1"
            assert not wiring.open_blockers(), "still inside the allowance"
        else:
            assert wiring.registry.next_ready() is None
            assert [b.code for b in wiring.open_blockers()] == [STRANDED_AFTER_FAULT]

    assert wiring.executions.load("t1").attempt_count == 0
    assert wiring.executions.load("t1").fault_attempt_count == MAX_TASK_FAULT_ATTEMPTS


def test_a_round_that_reached_the_reviewer_is_not_a_strand(tmp_path):
    """Its next move belongs to the reviewer, not to this sweep — including
    the redo shape `fault|<origin>>sent_for_review`, which is charged to the
    fault budget and would read as a fault to anything matching on the budget
    label alone."""
    wiring = build(tmp_path)
    record_for(
        wiring,
        ledger=((ATTEMPT_FAULT, "browser_session_lost>sent_for_review"),),
        candidate_sha="b" * 40,
        review_round=1,
    )
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.status("t1") == "in_progress"
    assert not wiring.open_blockers()


def test_a_task_failing_on_its_own_merits_is_not_swept(tmp_path):
    """The control. `status="error"` covers four different things and only an
    environment fault is one of them; a round that ran to completion and failed
    its own validation is charged to the task budget and is the task's problem,
    not the environment's. Adjacent strand class, deliberately not acted on."""
    wiring = build(tmp_path)
    record_for(
        wiring,
        ledger=((ATTEMPT_TASK, "executor_reported_failure"),),
        fault_attempt_count=0,
        attempt_count=1,
    )
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.status("t1") == "in_progress"
    assert not wiring.open_blockers()


def test_a_published_candidate_is_not_a_strand(tmp_path):
    """A published candidate is durable on its own branch and its task is
    finishing rather than stranded. Drawing a blocker for one would be a loud
    false alarm, which is the failure mode a monitor cannot afford."""
    wiring = build(tmp_path)
    record_for(wiring, candidate_sha="c" * 40, published_sha="c" * 40)
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    assert not wiring.open_blockers()


def test_a_task_with_no_execution_record_is_left_alone(tmp_path):
    """There is no evidence of a fault round, so this predicate has nothing to
    say. Named rather than silently covered: an in-progress task with no record
    at all is an adjacent strand class neither reader acts on."""
    wiring = build(tmp_path)
    wiring.registry.mark_in_progress("t1")
    wiring.task_store.save(wiring.registry)
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.status("t1") == "in_progress"
    assert not wiring.open_blockers()


def test_an_unparseable_ledger_entry_is_left_alone(tmp_path):
    """`worktask.split_attempt` is deliberately tolerant of a hand-edited
    record, so an entry naming no known budget reads as neither open nor
    settled. Inventing a fault from it would be the same guess the attempt
    ledger exists to replace."""
    wiring = build(tmp_path)
    record_for(wiring, ledger=(("nonsense", "who knows"),))
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.status("t1") == "in_progress"
    assert not wiring.open_blockers()


def test_an_empty_ledger_is_left_alone(tmp_path):
    wiring = build(tmp_path)
    record_for(wiring, ledger=())
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.status("t1") == "in_progress"
    assert not wiring.open_blockers()


def test_nothing_is_written_when_nothing_moved(tmp_path):
    """This runs at the top of every round. A `tasks.json` rewritten on each
    pass is noise in the escape detector's snapshot for no gain — the same rule
    `cli._reconcile_unblocked_tasks` follows.

    Driven with a strand PRESENT and unsafe, so the loop body really runs and
    the save is skipped because nothing was released. With no strand at all the
    method returns two lines in and the assertion would pass vacuously."""
    wiring = build(tmp_path)
    record_for(wiring, candidate_sha="9" * 40)
    wiring.moved_on_to(None)
    saves = []
    wiring.orch._task_store.save = lambda registry: saves.append(registry)

    wiring.orch._reconcile_stranded_tasks()

    assert wiring.open_blockers(), "the unsafe strand really was processed"
    assert saves == []


# =============================================================================
# 4. the accounting — no refilled budget, and the bound still holds
# =============================================================================


def test_the_requeue_refills_neither_attempt_budget(tmp_path):
    """An environment fault is not the task's own churn, and a return to the
    pool must not hand it an allowance it did not earn. Both counters and the
    ledger survive the requeue byte for byte — which is what makes the ceiling
    below arrive on schedule instead of never."""
    wiring = build(tmp_path)
    before = record_for(wiring, attempt_count=2, fault_attempt_count=3)
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    after = wiring.executions.load("t1")
    assert after.attempt_count == before.attempt_count == 2
    assert after.fault_attempt_count == before.fault_attempt_count == 3
    assert after.attempt_ledger == before.attempt_ledger
    assert after.pending_fault_code == ""
    assert after == before, "the requeue is a STATUS move; the record is evidence"


def test_the_execution_record_survives_so_the_next_round_resumes_it(tmp_path):
    """Deliberately NOT `release_task_to_pending`, which archives the record.
    A fresh record would read `attempt_count=0, fault_attempt_count=0`, and an
    outage would then requeue forever — fault, fresh record, fault."""
    wiring = build(tmp_path)
    record_for(wiring, fault_attempt_count=2)
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    execution = wiring.executions.load("t1")
    assert execution is not None, "the live record must still be where the next dispatch reads it"
    assert execution.fault_attempt_count == 2, "with the spent allowance still on it"
    archive = wiring.config.executions_dir / "archive"
    assert not archive.exists() or not list(archive.glob("*.json")), (
        "nothing was retired — this is a status move, not a release"
    )


def test_an_outage_that_faults_every_round_still_ends_at_the_ceiling(tmp_path):
    """THE bound, and the answer to "this builds a dispatch loop". It does
    not: every requeued dispatch is charged to `fault_attempt_count`, which the
    requeue never resets, so a task faulting into a dead API walks into
    `fault_attempt_ceiling` in `MAX_TASK_FAULT_ATTEMPTS` dispatches and parks —
    exactly as it would without any of this. The roadmap does not cycle.

    The last lap is where the two arms meet: the allowance is spent, so the
    sweep stops handing the task back and files the blocker instead. A dispatch
    after that parks at the ceiling, which is the same ending without the
    sweep."""
    wiring = build(tmp_path)

    for expected in range(1, MAX_TASK_FAULT_ATTEMPTS + 1):
        wiring.orch.state.phase = Phase.READY.value
        wiring.orch._dispatch_executor(implement("t1"))
        assert wiring.executions.load("t1").fault_attempt_count == expected
        wiring.moved_on_to("something-else")   # the loop moves on every time
        wiring.orch._reconcile_stranded_tasks()
        if expected < MAX_TASK_FAULT_ATTEMPTS:
            assert wiring.registry.next_ready().id == "t1"
            assert not wiring.open_blockers(), "still inside the allowance"
        else:
            # The allowance is spent, so the sweep hands the task to a human
            # instead of buying a dispatch that would refuse itself.
            assert wiring.registry.next_ready() is None
            assert wiring.status("t1") == "in_progress"
            assert [b.code for b in wiring.open_blockers()] == [STRANDED_AFTER_FAULT]

    execution = wiring.executions.load("t1")
    assert execution.attempt_count == 0, "none of it was the task's own work"
    assert execution.fault_attempt_count == MAX_TASK_FAULT_ATTEMPTS

    wiring.orch.state.phase = Phase.READY.value
    calls_before = wiring.orch._executor.calls
    wiring.orch._dispatch_executor(implement("t1"))

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch._executor.calls == calls_before, "refused before the executor ran"
    assert wiring.blockers.load(wiring.orch.state.park_blocker_id).code == (
        "fault_attempt_ceiling"
    )

    # And the sweep does not undo the ceiling: the task is still in_progress
    # with a spent budget, so the next pass REPORTS it instead of handing it
    # back to the queue.
    wiring.moved_on_to("something-else")
    wiring.orch._reconcile_stranded_tasks()
    assert wiring.status("t1") == "in_progress"
    assert STRANDED_AFTER_FAULT in {b.code for b in wiring.open_blockers()}


# =============================================================================
# 5. reporting — the part that cost twenty-one hours
# =============================================================================


def test_the_requeue_is_visible_in_the_transcript_with_the_task_and_the_fault(tmp_path):
    wiring = build(tmp_path)
    record_for(wiring, attempt_count=1, fault_attempt_count=2)
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    entry = wiring.entries("task_strand_requeued")[0]["data"]
    assert entry["task_id"] == "t1"
    assert entry["fault_code"] == API_ERROR
    # Both counters travel with it, so the bound is auditable from the
    # transcript alone rather than inferred from a counter gap.
    assert entry["attempt_count"] == 1
    assert entry["fault_attempt_count"] == 2


def test_a_blocked_strand_is_visible_in_the_transcript_too(tmp_path):
    wiring = build(tmp_path)
    record_for(wiring, candidate_sha="d" * 40)
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    entry = wiring.entries("task_strand_blocked")[0]["data"]
    assert entry["task_id"] == "t1"
    assert entry["fault_code"] == API_ERROR
    assert entry["blocker_id"] == wiring.open_blockers()[0].id
    assert "candidate" in entry["obstacle"]


def test_an_open_blocker_is_retained_rather_than_re_recorded_every_round(tmp_path):
    """`recurrences` means "this condition re-parked", not "a sweep looked at
    it again". Running every round, an upsert would inflate it into noise and
    rewrite `last_seen_at` forever."""
    wiring = build(tmp_path)
    record_for(wiring, candidate_sha="e" * 40)
    wiring.moved_on_to(None)

    for _ in range(4):
        wiring.orch._reconcile_stranded_tasks()

    open_blockers = wiring.open_blockers()
    assert len(open_blockers) == 1
    assert open_blockers[0].recurrences == 1
    assert len(wiring.entries("task_strand_blocked")) == 1


def test_the_blocker_says_what_to_do_and_that_answering_it_moves_nothing(tmp_path):
    """A blocker whose question does not name a next action is a puzzle. And
    the honest caveat has to be in the text: this code never changes a task's
    status, so `answer` reports `task_not_blocked` while resolving it."""
    wiring = build(tmp_path)
    record_for(wiring, candidate_sha="f" * 40)
    wiring.moved_on_to(None)

    wiring.orch._reconcile_stranded_tasks()

    question = wiring.open_blockers()[0].question
    assert "autoloop release t1" in question
    assert "NOT move the task" in question


# =============================================================================
# 6. the detection half — `health` names a strand on EVERY verdict
# =============================================================================


def test_health_reports_a_strand_while_the_loop_is_otherwise_running(tmp_path):
    """The 2026-08-22 signal that never fired. A loop working happily on other
    tasks reported `running` for twenty-one hours while a P1 task sat off the
    board."""
    wiring = build(tmp_path)
    record_for(wiring)
    wiring.orch.state.task_execution = {"task_id": "elsewhere"}
    StateStore(wiring.config.state_file).save(wiring.orch.state)
    wiring.config.transcript_file.parent.mkdir(parents=True, exist_ok=True)
    TranscriptLogger(wiring.config.transcript_file).append("directive", data={})

    with LoopLock(wiring.config.state_dir):
        verdict = health.check(wiring.config, agent_probe=lambda: False)

    assert verdict.code == health.STUCK_STRANDED
    assert verdict.needs_attention is True
    assert verdict.stranded_tasks == ("t1",)
    assert API_ERROR in verdict.detail


def test_health_carries_the_strand_on_a_verdict_that_already_needs_attention(tmp_path):
    """The placement that matters. `not_running`, `blocked` and a stale lock
    all return early, and those are the states a strand co-occurs with — a
    late check of its own would have stayed silent through the whole
    incident."""
    wiring = build(tmp_path)
    record_for(wiring)
    wiring.orch.state.task_execution = {"task_id": "elsewhere"}
    StateStore(wiring.config.state_file).save(wiring.orch.state)

    verdict = health.check(wiring.config, agent_probe=lambda: False)

    assert verdict.code == health.STUCK_NOT_RUNNING, "the loop verdict still wins"
    assert verdict.needs_attention is True
    assert verdict.stranded_tasks == ("t1",)
    assert "t1" in verdict.detail
    assert "start it with" in verdict.detail, "the original detail is kept, not replaced"


def test_health_stays_quiet_for_the_task_the_loop_is_working(tmp_path):
    """The false-alarm surface: mid-round, a healthy task has exactly the
    record shape a strand does — no candidate, no review round. The dispatch
    stamp is what tells the two apart, and the loop writes it (`current_task`)
    in the same save as `task_execution`."""
    wiring = build(tmp_path)
    record_for(wiring)
    wiring.working_on("t1", age_seconds=90)
    wiring.persist_state()
    TranscriptLogger(wiring.config.transcript_file).append("directive", data={})

    with LoopLock(wiring.config.state_dir):
        verdict = health.check(wiring.config, agent_probe=lambda: False)

    assert verdict.needs_attention is False
    assert verdict.stranded_tasks == ()


def test_health_names_the_task_whose_round_outlived_the_ceiling(tmp_path):
    """The detection half of the bound, and the state the 2026-08-22 incident
    would have sat in with only one task on the roadmap: the loop still claims
    to be working it, and nothing but the round ceiling can say otherwise."""
    wiring = build(tmp_path)
    record_for(wiring)
    wiring.working_on("t1", age_seconds=wiring.ceiling() + 60)
    wiring.persist_state()
    TranscriptLogger(wiring.config.transcript_file).append("directive", data={})

    with LoopLock(wiring.config.state_dir):
        verdict = health.check(wiring.config, agent_probe=lambda: False)

    assert verdict.code == health.STUCK_STRANDED
    assert verdict.stranded_tasks == ("t1",)


def test_a_dispatch_stamp_a_little_in_the_future_is_skew_not_an_age():
    """Wall clocks get adjusted. `dashboard._elapsed_seconds` reads the same
    stamp under the same rule, and the two must agree: a couple of minutes
    ahead reads as "just started" (so a live round keeps its exemption), and
    anything further ahead is not a measurable age at all, so it reads unknown
    and the caller falls back to the evidence it has."""
    state = LoopState.new(URL)
    state.task_execution = {"task_id": "t1"}

    state.current_task = {"task_id": "t1", "started_at": stamp_aged(-30)}
    assert health.current_round_age_seconds(state) == 0.0

    state.current_task = {"task_id": "t1", "started_at": stamp_aged(-86400)}
    assert health.current_round_age_seconds(state) is None


def test_an_unusable_dispatch_stamp_reads_as_unknown_never_as_young():
    """Every shape that is not a usable age, in one place: no state, no round,
    no `current_task`, a stamp for another task, an unparseable stamp, a
    missing one. `None` is the absence of evidence and the caller grants its
    exemption on evidence only."""
    assert health.current_round_age_seconds(None) is None

    state = LoopState.new(URL)
    assert health.current_round_age_seconds(state) is None, "no round in flight"

    state.task_execution = {"task_id": "t1"}
    assert health.current_round_age_seconds(state) is None, "no current_task at all"

    state.current_task = {"task_id": "t2", "started_at": stamp_aged(10)}
    assert health.current_round_age_seconds(state) is None, "another task's stamp"

    state.current_task = {"task_id": "t1", "started_at": "not a timestamp"}
    assert health.current_round_age_seconds(state) is None

    state.current_task = {"task_id": "t1"}
    assert health.current_round_age_seconds(state) is None

    # ...and the one shape that IS a usable age, so the assertions above are
    # not all passing for the same uninteresting reason.
    state.current_task = {"task_id": "t1", "started_at": stamp_aged(300)}
    age = health.current_round_age_seconds(state)
    assert age is not None and 290 < age < 400


def test_a_naive_dispatch_stamp_is_read_as_utc():
    """`utcnow_iso()` is tz-aware, but a record written by an older build (or
    by hand) may not be. Reading a naive stamp as local time would shift the
    age by the machine's offset — hours, either way, on the one number the
    exemption is granted from."""
    state = LoopState.new(URL)
    state.task_execution = {"task_id": "t1"}
    naive = (datetime.now(timezone.utc) - timedelta(seconds=600)).replace(tzinfo=None)
    state.current_task = {"task_id": "t1", "started_at": naive.isoformat()}

    age = health.current_round_age_seconds(state)

    assert age is not None and 590 < age < 700


def test_health_is_quiet_when_there_is_no_roadmap_at_all(tmp_path):
    """A missing task file means no tasks, which is the honest answer for a
    state directory the loop has never written to — not a failure."""
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / "state",
        workers_root=tmp_path / "workers",
    )
    config.state_dir.mkdir(parents=True)
    TranscriptLogger(config.transcript_file).append("directive", data={})

    with LoopLock(config.state_dir):
        verdict = health.check(config, agent_probe=lambda: False)

    assert verdict.needs_attention is False
    assert verdict.stranded_tasks == ()


def test_an_unreadable_task_file_is_reported_rather_than_read_as_no_strands(tmp_path):
    """The other fail-open. A survey that cannot run must not answer "nothing
    is stranded" — that is a check silently passing when what it needs is
    unreadable, and the alarm would never fire again."""
    wiring = build(tmp_path)
    wiring.config.tasks_file.write_text("{ not json", encoding="utf-8")
    TranscriptLogger(wiring.config.transcript_file).append("directive", data={})

    with LoopLock(wiring.config.state_dir):
        verdict = health.check(wiring.config, agent_probe=lambda: False)

    assert verdict.code == health.STUCK_STRANDED
    assert verdict.needs_attention is True
    assert "could not be read" in verdict.detail


def test_an_unreadable_state_file_refuses_to_guess_which_task_is_current(tmp_path):
    """Without the state file every in-progress task looks abandoned.
    Reporting them all would be the false alarm; reporting none would be the
    fail-open. It says it could not check.

    Asserted against the survey rather than `check`, and the reason is the
    ordering: `_judge` reads the same file first and raises on a corrupt one,
    so through `check` this arm is only reachable if the file rots between the
    two reads. It is defensive, it is still the right answer, and testing it
    where it lives is the honest way to say so."""
    wiring = build(tmp_path)
    record_for(wiring)
    wiring.config.state_file.write_text("{ not json", encoding="utf-8")

    strands, note = health._strand_survey(wiring.config)

    assert strands == ()
    assert "state could not be read" in note


def test_the_verdict_serialises_with_the_stranded_tasks(tmp_path):
    """`to_json` is what a monitor reads; a field the dataclass has and the
    payload does not would be a detector nobody downstream can see."""
    wiring = build(tmp_path)
    record_for(wiring)
    wiring.orch.state.task_execution = {"task_id": "elsewhere"}
    StateStore(wiring.config.state_file).save(wiring.orch.state)

    payload = json.loads(health.check(wiring.config, agent_probe=lambda: False).to_json())

    assert payload["stranded_tasks"] == ["t1"]


# =============================================================================
# 7. the predicate itself, against the registry directly
# =============================================================================


def test_the_predicate_reads_the_stored_status_not_state_of(tmp_path):
    """`state_of` reports BLOCKED for an in-progress task whose dependency is
    incomplete — the dependency test runs before the in-progress branch — so a
    sweep asking it would fall silent on exactly the row that is hardest to
    move."""
    registry = TaskRegistry(
        [
            Task(id="dep", title="D", description="d"),
            Task(id="t1", title="T", description="d", depends_on=("dep",)),
        ]
    )
    registry.get("t1").status = "in_progress"

    assert [t.id for t in registry.in_progress_tasks()] == ["t1"]
    assert registry.state_of("t1") is TaskState.BLOCKED


def test_the_predicate_never_raises_on_a_dangling_dependency():
    """`from_dict` tolerates a `depends_on` naming a task that no longer
    exists, and `state_of` raises `KeyError` on it. A reconciliation sweep must
    not crash the loop over a graph the loader accepted."""
    registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    registry.get("t1").status = "in_progress"
    registry.get("t1").depends_on = ("ghost",)

    assert [t.id for t in registry.in_progress_tasks()] == ["t1"]
    with pytest.raises(KeyError):
        registry.state_of("t1")


def test_the_safe_flag_and_the_obstacle_are_one_decision():
    """Two rules would eventually disagree about whether a record is safe, and
    the disagreement would show up as a blocker with nothing in it to quote."""
    safe = health.StrandedRound(task_id="t1", fault_code=API_ERROR)
    unsafe = health.StrandedRound(task_id="t1", fault_code=API_ERROR, obstacle="x")

    assert safe.safe_to_requeue is True
    assert unsafe.safe_to_requeue is False
