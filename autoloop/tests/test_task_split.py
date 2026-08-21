"""Accepting a split atomically across the registry, the execution record and
the worker repository.

THE PROBLEM THIS FILE IS ABOUT. Retiring a task into successors touches three
stores that cannot be written in one operation: `tasks.json`, the task's
`executions/<id>.json` record, and its worker repository (a directory that has
to be MOVED). A crash between any two of them leaves the three disagreeing —
the registry saying the parent is retired while its record and worker still
describe live, unpublished work — and no later run can tell "half-applied" from
"someone else's state" by looking.

THE ANSWER UNDER TEST is option (b) from the task: a durable
`state.SplitIntent`, written before anything else is touched, plus an idempotent
reconciliation that drives every store to match it and clears it only once each
one has been INSPECTED and found to agree. A crash is then never a
contradiction; it is an unfinished intent.

So the centre of this file is not the happy path. It is the CRASH MATRIX below:
one test per durable write boundary, each of which kills the process at that
boundary and then restarts the loop over the same on-disk state, asserting that
recovery converges on exactly the same four facts the uninterrupted run
produces. A test that only proved the happy path would prove nothing about the
window this task exists to close.

  B0  intent durable, registry not yet written
  B1  registry written, execution record not yet archived
  B2  record archived, worker not yet quarantined
  B3  worker quarantined, intent not yet cleared / round not yet finished
  B4  no crash at all (the control)
  B5  reconciliation re-run over an already-complete intent

and the negative half, which is the other thing a reviewer asked for: durable
state that CONTRADICTS the intent is never overwritten — it fails closed, parks,
and leaves the intent on disk for a human.

That half includes the case a `parent_id`-only intent cannot see. Both stores
are addressed by task id, so the thing living at that address can be REPLACED
between the crash and the recovery — the task re-dispatched, a record repaired
by hand, some other repository moved into the quarantine path — and retiring by
id alone would destroy the replacement while discharging the intent as though
the accepted work had been retired. So the intent binds WHICH record and WHICH
repository it accepted, and the tests here replace each of them, and each of
their already-filed-away destinations, and assert that nothing moves.

A `Crash` here is deliberately not an `OSError` or a `StateError`. Those are
caught and parked, and a park is the GRACEFUL outcome; what these tests have to
exercise is the ungraceful one, where nothing runs afterwards — no cleanup, no
park, no save.

Self-contained per this codebase's convention (see `test_m1_hardening.py`'s
docstring): real git repos, real worker repositories, real state files, no
fixtures imported from other test modules.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autoloop.audit.agents import AGENT_FAULT_STALL
from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive, TaskSpec
from autoloop.errors import ContractError, StateError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    MAX_DERIVATION_DEPTH,
    SPLIT_CUT_SHORT_ROUNDS,
    Orchestrator,
)
from autoloop.policy import PolicyConfig, PolicyEngine, Verdict
from autoloop.state import (
    SPLIT_DEFINITION_KEYS,
    SPLIT_RECORD_PROVENANCE_KEYS,
    SPLIT_WORKER_PROVENANCE_KEYS,
    LoopState,
    Phase,
    SplitIntent,
    StateStore,
)
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore, mutation_ledger_for
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import (
    IntentStore,
    TaskExecution,
    TaskExecutionStore,
    archived_record_is_for,
)

URL = "https://chatgpt.com/c/task-split-test"
PARENT = "big-01"
#: The retirement label a hand-built intent files both halves under. Fixed, so a
#: test can name the exact archive and quarantine destinations that intent
#: implies — which is what the wrong-identity tests below drop an impostor at.
LABEL = "split-20260821T000000Z"


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
    # Mirrors the real repo's `.gitignore`: without it `.al/state.json` reads as
    # an untracked dirty path and trips `primary_checkout_dirty` for a reason
    # unrelated to anything under test (see docs/COMMON_ERRORS.md).
    (repo_root / ".gitignore").write_text(
        ".al/\n__pycache__/\n*.py[cod]\n", encoding="utf-8"
    )
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


class Crash(Exception):
    """A process death, modelled.

    Deliberately neither `OSError` nor `StateError`: both of those are CAUGHT by
    the split paths and turned into a park, which is the graceful outcome. The
    window this file is about is the ungraceful one — nothing runs after it, so
    whatever is on disk at that instant is the whole of what recovery gets.
    """


class CrashAt:
    """Kill the process the Nth time `owner.attr` is called.

    Installed on the INSTANCE, so it shadows the bound method for exactly the
    object under test and nothing else. `restore()` puts the real one back —
    every test does that before it asserts, because the same store objects are
    what the assertions and the restart read through.
    """

    def __init__(self, owner, attr, after: int = 0):
        self.owner = owner
        self.attr = attr
        self.original = getattr(owner, attr)
        self.remaining = after
        self.calls = 0
        setattr(owner, attr, self)

    def __call__(self, *args, **kwargs):
        self.calls += 1
        if self.remaining <= 0:
            raise Crash(f"process died at {self.attr} call {self.calls}")
        self.remaining -= 1
        return self.original(*args, **kwargs)

    def restore(self) -> None:
        setattr(self.owner, self.attr, self.original)


class NoExecutor:
    def execute(self, directive, task):  # pragma: no cover - never reached
        raise AssertionError("this test must not run an executor")


class StallingExecutor:
    """Every round writes real files into the worker repo and is then killed by
    the stall supervisor — exec-01's measured shape, and the only evidence the
    split trigger accepts.

    `changed=()` models the OTHER shape: an agent the supervisor killed before
    it produced anything, which must NOT be read as "too big".
    """

    def __init__(self, workers_root, changed=("A.py",)):
        self.workers_root = Path(workers_root)
        self.changed = tuple(changed)
        self.calls = 0

    def execute(self, directive, task):
        self.calls += 1
        worker = self.workers_root / task.id
        for rel in self.changed:
            target = worker / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"partial round {self.calls}\n", encoding="utf-8")
        return ExecutionOutcome(
            status="error",
            summary=f"task '{task.id}': implementation agent failed — no progress",
            details="",
            validation="not run",
            changed_paths=self.changed,
            fault_kind=AGENT_FAULT_STALL,
        )


class Env:
    """One split scenario: real repo, real stores, and the ability to RESTART
    the loop over the same durable state.

    `restart()` is what makes a crash test a crash test. It builds a brand-new
    `Orchestrator` from what is on disk — the state file, the task file, the
    execution records, the worker directories — exactly as a fresh process
    would, so nothing carried in memory by the dead run can make recovery look
    like it worked.
    """

    def __init__(self, tmp_path, tasks=None, executor=None):
        self.tmp_path = Path(tmp_path)
        self.repo_root = real_repo(tmp_path)
        self.base_sha = run_git(self.repo_root, "rev-parse", "HEAD").strip()
        self.workers_root = self.tmp_path / "workers_root"
        self.worker_repos = WorkerRepoManager(
            self.workers_root, self.tmp_path / "worker-hooks"
        )
        self.execution_store = TaskExecutionStore(self.tmp_path / "executions")
        self.intent_store = IntentStore(self.tmp_path / "intents")
        self.blocker_store = BlockerStore(self.tmp_path / "blockers")
        self.config = AutoloopConfig(
            browser=BrowserConfig(conversation_url=URL),
            policy=PolicyConfig(implement_enabled=True),
            state_dir=self.repo_root / ".al",
            workers_root=self.workers_root,
        )
        self.store = StateStore(self.config.state_file)
        self.task_store = TaskStore(
            self.config.tasks_file,
            ledger=mutation_ledger_for(self.config.workers_root, self.config.state_dir),
        )
        state = LoopState.new(URL)
        self.store.save(state)
        registry = TaskRegistry(
            list(tasks)
            if tasks is not None
            else [
                Task(
                    id=PARENT,
                    title="A task that will not fit",
                    description="too big to finish in one round",
                    approved_paths=("A.py",),
                )
            ]
        )
        self.task_store.save(registry)
        self.orch = self._build(state, registry, executor or NoExecutor())

    def _build(self, state, registry, executor) -> Orchestrator:
        def no_client():  # pragma: no cover - no test here reaches the browser
            raise AssertionError("no browser client expected in this test")

        return Orchestrator(
            config=self.config,
            store=self.store,
            state=state,
            policy=PolicyEngine(self.config.policy),
            git=GitGateway(
                self.repo_root, PolicyEngine(PolicyConfig(implement_enabled=True))
            ),
            executor=executor,
            transcript=TranscriptLogger(self.config.transcript_file),
            client_factory=no_client,
            registry=registry,
            task_store=self.task_store,
            manifest_store=ManifestStore(self.config.manifests_dir),
            worker_repos=self.worker_repos,
            execution_store=self.execution_store,
            intent_store=self.intent_store,
            blocker_store=self.blocker_store,
            validation_runner=ok_validation,
        )

    def restart(self, executor=None) -> Orchestrator:
        """A new process over the same disk. Everything is re-read."""
        self.orch = self._build(
            self.store.load(), self.task_store.load(), executor or NoExecutor()
        )
        return self.orch

    # ---- seeding ------------------------------------------------------------

    def seed_execution(self, task_id=PARENT) -> TaskExecution:
        """The durable halves a dispatched task owns: an execution record and a
        worker repository. Created for real, so retiring them is a real archive
        and a real directory move."""
        execution = TaskExecution(
            task_id=task_id,
            task_branch=f"autoloop/{task_id}",
            worktree_path=str(self.worker_repos.path_for(task_id)),
            task_base_sha=self.base_sha,
            cut_short_count=SPLIT_CUT_SHORT_ROUNDS,
            cut_short_with_work_count=SPLIT_CUT_SHORT_ROUNDS,
        )
        self.execution_store.save(execution)
        self.worker_repos.create(task_id, self.repo_root, self.base_sha)
        return execution

    def ask_for_split(self, task_id=PARENT) -> None:
        """Record the ask and put the loop where a reply is dispatched from.

        Recorded through the state store BEFORE any crash injector is installed
        — the orchestrator and this Env share ONE `StateStore`, so a patch meant
        for the acceptance would otherwise fire on the setup instead.
        """
        state = self.orch.state
        state.split_requested_for = task_id
        state.phase = Phase.EXECUTING.value
        self.store.save(state)

    # ---- reading back -------------------------------------------------------

    def disk_registry(self) -> TaskRegistry:
        return self.task_store.load()

    def disk_state(self) -> LoopState:
        return self.store.load()

    def archives(self, task_id=PARENT) -> list[Path]:
        return sorted((self.execution_store.directory / "archive").glob(f"{task_id}-*.json"))

    def quarantines(self, task_id=PARENT) -> list[Path]:
        """Every quarantined copy of this task's worker repo.

        Counted rather than merely probed, because "exactly one" is the
        anti-duplicate-label assertion. That is also why no test both runs REAL
        rounds and calls `assert_split_complete`: `_prepare_write_capable_worker`
        quarantines a worker with residual uncommitted state before recreating
        it, so a round-driven fixture can legitimately leave a quarantine
        directory behind that the split never made.
        """
        root = self.workers_root.parent / "quarantine"
        return sorted(root.glob(f"{task_id}-*")) if root.exists() else []


def spec(task_id, paths=("A.py",), depends_on=(), title=None, description=None) -> TaskSpec:
    return TaskSpec(
        id=task_id,
        title=title or f"Successor {task_id}",
        description=description or f"the {task_id} half of {PARENT}",
        depends_on=tuple(depends_on),
        approved_paths=tuple(paths),
    )


def split_plan(*specs, reason="the task does not fit in one round") -> Directive:
    return Directive(decision=Decision.PLAN, reason=reason, tasks=tuple(specs))


def implement(task_id=PARENT) -> Directive:
    return Directive(decision=Decision.IMPLEMENT, reason="go", task_id=task_id)


def assert_split_complete(env: Env, successors, *, parent=PARENT, others=()):
    """THE assertion: all three stores plus the intent agree that the split
    happened, read from DISK rather than from anything held in memory.

    `others` names tasks that legitimately exist besides the parent and its
    successors, so the roadmap check can stay EXACT — a bystander task is not a
    reason to weaken it into a subset test.
    """
    registry = env.disk_registry()

    # 1. the registry
    assert registry.state_of(parent) is TaskState.RETIRED
    assert registry.get(parent).superseded_by == tuple(s.id for s in successors)
    assert {t.id for t in registry.all_tasks()} == {
        parent,
        *(s.id for s in successors),
        *others,
    }
    for successor in successors:
        stored = registry.get(successor.id)
        assert stored.title == successor.title
        assert stored.description == successor.description
        assert stored.approved_paths == tuple(successor.approved_paths)
        assert stored.depends_on == tuple(successor.depends_on)
        assert registry.state_of(successor.id) is not TaskState.RETIRED

    # 2. the execution record — archived, exactly once, and really this task's
    assert env.execution_store.load(parent) is None
    archives = env.archives(parent)
    assert len(archives) == 1, f"expected one archived record, got {archives}"
    assert archived_record_is_for(archives[0], parent)

    # 3. the worker repository — quarantined, exactly once
    assert not env.worker_repos.path_for(parent).exists()
    quarantined = env.quarantines(parent)
    assert len(quarantined) == 1, f"expected one quarantined worker, got {quarantined}"
    assert quarantined[0].is_dir()

    # ...under ONE label, which is what pairs the two halves on disk and what a
    # second, freshly-minted label would break.
    label = archives[0].name[len(parent) + 1 : -len(".json")]
    assert quarantined[0].name == f"{parent}-{label}"
    assert label.startswith("split-")

    # 4. the intent is discharged, and so is the ask that produced it
    state = env.disk_state()
    assert state.split_intent is None
    assert state.split_requested_for == ""


def assert_nothing_applied(env: Env, successor_ids, *, parent=PARENT):
    """The mirror image: not one of the three stores has moved."""
    registry = env.disk_registry()
    assert registry.state_of(parent) is not TaskState.RETIRED
    assert registry.get(parent).superseded_by == ()
    for task_id in successor_ids:
        assert not registry.has(task_id)
    assert env.execution_store.load(parent) is not None
    assert env.archives(parent) == []
    assert env.worker_repos.path_for(parent).exists()
    assert env.quarantines(parent) == []


# =============================================================================
# 1. the trigger: what earns a split ask
# =============================================================================


def test_one_round_cut_short_with_work_does_not_ask_for_a_split(tmp_path):
    """One supervisor kill is an incident. Two is a shape — see
    `SPLIT_CUT_SHORT_ROUNDS`."""
    env = Env(tmp_path, executor=StallingExecutor(tmp_path / "workers_root"))

    env.orch._dispatch_executor(implement())

    execution = env.execution_store.load(PARENT)
    assert execution.cut_short_count == 1
    assert execution.cut_short_with_work_count == 1
    assert env.orch.state.split_requested_for == ""
    assert "SPLIT CANDIDATE" not in (env.orch.state.outbox or "")


def test_two_rounds_cut_short_with_work_ask_the_reviewer_to_split(tmp_path):
    """The whole trigger, end to end through the real dispatch path: the agent
    is killed twice having written real files into its worker repo, and the ask
    is appended to the SAME message that reports the second failure."""
    env = Env(tmp_path, executor=StallingExecutor(tmp_path / "workers_root"))

    env.orch._dispatch_executor(implement())
    env.orch._dispatch_executor(implement())

    execution = env.execution_store.load(PARENT)
    assert execution.cut_short_with_work_count == SPLIT_CUT_SHORT_ROUNDS
    state = env.disk_state()
    assert state.split_requested_for == PARENT
    # ...and the marker and the message it belongs to are durable TOGETHER: a
    # reply can only mean "replace that task" if the message that asked and the
    # marker that reinterprets the answer landed in the same save.
    assert "SPLIT CANDIDATE" in state.outbox
    assert PARENT in state.outbox
    # The ordinary round report is still there — the ask is an addition to it,
    # not a replacement for it.
    assert "implementation agent failed" in state.outbox


def test_rounds_cut_short_with_no_work_are_recorded_but_never_ask(tmp_path):
    """An agent the supervisor killed before it produced anything is not a task
    that is too big — it is an agent wedged before it starts, and splitting it
    would produce several tasks with the same problem. The rounds are still
    RECORDED, so the record says which of the two happened."""
    env = Env(tmp_path, executor=StallingExecutor(tmp_path / "workers_root", changed=()))

    env.orch._dispatch_executor(implement())
    env.orch._dispatch_executor(implement())

    execution = env.execution_store.load(PARENT)
    assert execution.cut_short_count == 2
    assert execution.cut_short_with_work_count == 0
    assert env.disk_state().split_requested_for == ""


def test_any_decision_other_than_plan_declines_the_split(tmp_path):
    """The marker reinterprets a `plan` as a retirement, so it must not outlive
    the question it answers: a reviewer who replies anything else has declined,
    and a marker left standing would silently retire a task later because an
    unrelated plan happened to arrive."""
    env = Env(tmp_path)
    env.ask_for_split()

    env.orch._dispatch(Directive(decision=Decision.STOP, reason="not now"))

    assert env.disk_state().split_requested_for == ""


def test_a_reply_that_never_reaches_dispatch_does_not_decline_the_split(tmp_path):
    """A malformed reply and a policy denial are both handled in
    `_step_executing`, BEFORE `_dispatch` — so neither spends the marker, and
    the ask survives to be answered by the corrected reply.

    Deliberate rather than incidental: the marker is spent by a DECISION the
    reviewer made, and a reply that could not be parsed or could not be
    authorized is not one. Both are separately bounded (the parse-retry and
    denial budgets), so this cannot hold the ask open indefinitely.
    """
    env = Env(tmp_path)
    env.ask_for_split()

    env.orch._handle_parse_error(ContractError("no_json_block", "no directive found"))
    # In memory AND on disk: the disk value alone would still read PARENT if the
    # handler had cleared it without saving, which is exactly the bug this is
    # about.
    assert env.orch.state.split_requested_for == PARENT
    assert env.disk_state().split_requested_for == PARENT

    env.orch._handle_policy_denial(
        implement(), Verdict.deny("implement_disabled", "not authorized right now")
    )
    assert env.orch.state.split_requested_for == PARENT
    assert env.disk_state().split_requested_for == PARENT


def test_a_plan_with_no_split_asked_for_is_still_an_ordinary_plan(tmp_path):
    """The regression guard on the other side: nothing about splitting may
    change what a plan does when no split was asked for."""
    env = Env(tmp_path)
    env.orch.state.phase = Phase.EXECUTING.value

    env.orch._dispatch(split_plan(spec("new-01")))

    registry = env.disk_registry()
    assert registry.has("new-01")
    assert registry.state_of(PARENT) is not TaskState.RETIRED
    assert env.disk_state().split_intent is None


# =============================================================================
# 2. the crash matrix — one test per durable write boundary
# =============================================================================


def test_boundary_0_intent_durable_registry_not_yet_written(tmp_path):
    """The first boundary, and the one that makes every later one recoverable:
    after the intent save the split WILL happen, even though not one store has
    been touched yet."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()
    successors = (spec("big-01a"), spec("big-01b", paths=("B.py",)))

    crash = CrashAt(env.task_store, "save")
    with pytest.raises(Crash):
        env.orch._dispatch(split_plan(*successors))
    crash.restore()

    # Nothing applied — but the DECISION is durable, which is the whole point.
    assert_nothing_applied(env, [s.id for s in successors])
    intent = SplitIntent.from_dict(env.disk_state().split_intent)
    assert intent.parent_id == PARENT
    assert intent.successor_ids() == tuple(s.id for s in successors)
    assert intent.retire_record is True and intent.retire_worker is True
    # The ask was spent in the same save that recorded the intent, so a second
    # plan cannot mint a second one.
    assert env.disk_state().split_requested_for == ""

    assert env.restart()._resume_split_intent() is True
    assert_split_complete(env, successors)


def test_boundary_1_registry_written_record_not_yet_archived(tmp_path):
    """The contradictory shape the reviewer named: `tasks.json` says the parent
    is retired while its execution record still describes live work. Recovery
    has to finish it, not pick one of the two to believe."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()
    successors = (spec("big-01a"),)

    crash = CrashAt(env.execution_store, "archive")
    with pytest.raises(Crash):
        env.orch._dispatch(split_plan(*successors))
    crash.restore()

    # Exactly the disagreement this task exists to make survivable.
    assert env.disk_registry().state_of(PARENT) is TaskState.RETIRED
    assert env.execution_store.load(PARENT) is not None
    assert env.worker_repos.path_for(PARENT).exists()
    assert env.disk_state().split_intent is not None

    assert env.restart()._resume_split_intent() is True
    assert_split_complete(env, successors)


def test_boundary_2_record_archived_worker_not_yet_quarantined(tmp_path):
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()
    successors = (spec("big-01a"),)

    crash = CrashAt(env.worker_repos, "quarantine")
    with pytest.raises(Crash):
        env.orch._dispatch(split_plan(*successors))
    crash.restore()

    assert env.disk_registry().state_of(PARENT) is TaskState.RETIRED
    assert env.execution_store.load(PARENT) is None
    assert len(env.archives()) == 1
    assert env.worker_repos.path_for(PARENT).exists(), "the worker is still live"
    assert env.disk_state().split_intent is not None

    assert env.restart()._resume_split_intent() is True
    assert_split_complete(env, successors)
    # ...and recovery reused the label the intent minted, rather than filing the
    # second half under a fresh timestamp that names nothing.
    assert env.quarantines()[0].name == env.archives()[0].name[: -len(".json")]


def test_boundary_3_all_three_stores_written_intent_not_yet_cleared(tmp_path):
    """The last boundary: everything is applied and the intent is still on disk.
    Recovery must be a no-op that clears it — and must NOT rewrite a registry
    that is already correct or file a second copy of anything."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()
    successors = (spec("big-01a"),)

    # The SECOND `store.save`: the first is the intent itself, so this kills the
    # process exactly at the save that would have cleared it.
    crash = CrashAt(env.store, "save", after=1)
    with pytest.raises(Crash):
        env.orch._dispatch(split_plan(*successors))
    crash.restore()

    assert env.disk_registry().state_of(PARENT) is TaskState.RETIRED
    assert env.execution_store.load(PARENT) is None
    assert len(env.archives()) == 1 and len(env.quarantines()) == 1
    assert env.disk_state().split_intent is not None, "the intent is still owed"

    orch = env.restart()
    assert orch._resume_split_intent() is True
    assert_split_complete(env, successors)
    assert len(env.archives()) == 1 and len(env.quarantines()) == 1


def test_boundary_4_no_crash_applies_everything_and_finishes_the_round(tmp_path):
    """The control. Also the one place the ROUND's own ending is asserted: the
    intent is cleared and the acknowledgement queued in ONE save, which is what
    stops a recovered round from re-dispatching the same plan into its own
    half-added successors."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()
    successors = (spec("big-01a"), spec("big-01b", paths=("B.py",), depends_on=("big-01a",)))

    env.orch._dispatch(split_plan(*successors))

    assert_split_complete(env, successors)
    state = env.disk_state()
    assert state.phase == Phase.READY.value
    assert state.last_response is None
    assert "Split applied" in state.outbox
    assert "big-01a" in state.outbox and "big-01b" in state.outbox


def test_boundary_5_reconciling_a_completed_intent_again_changes_nothing(tmp_path):
    """Repeated reconciliation, which is what a restart loop or a re-answered
    park produces. Nothing is filed twice, and the registry — already correct —
    is never REWRITTEN: `task_store.save` is booby-trapped on both passes and
    must not fire, which is the difference between a recovery that verifies and
    one that just re-does everything and hopes."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()
    successors = (spec("big-01a"),)

    # Reach the B3 shape: every store applied, the intent still owed.
    crash = CrashAt(env.store, "save", after=1)
    with pytest.raises(Crash):
        env.orch._dispatch(split_plan(*successors))
    crash.restore()
    outstanding = env.disk_state().split_intent
    assert outstanding is not None

    orch = env.restart()
    trap = CrashAt(env.task_store, "save")
    assert orch._resume_split_intent() is True
    assert trap.calls == 0, "a registry that already describes the split was rewritten"
    trap.restore()
    assert_split_complete(env, successors)

    # Now hand the SAME completed intent back, as a process that died between
    # the stores agreeing and the intent being cleared would leave it.
    second = env.restart()
    second.state.split_intent = outstanding
    env.store.save(second.state)
    trap = CrashAt(env.task_store, "save")
    assert second._resume_split_intent() is True
    assert trap.calls == 0
    trap.restore()

    assert_split_complete(env, successors)
    assert len(env.archives()) == 1 and len(env.quarantines()) == 1
    # Nothing outstanding now: reconciliation is a no-op and says so.
    assert second._reconcile_split_intent() is False


def test_reconciliation_runs_before_the_loop_takes_a_single_step(tmp_path):
    """Where the recovery is wired, not just that it works. `run()` discharges an
    outstanding intent BEFORE the pause check, the inbox drain and the phase
    read — every one of which reads a store the intent is about."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()
    successors = (spec("big-01a"),)

    crash = CrashAt(env.execution_store, "archive")
    with pytest.raises(Crash):
        env.orch._dispatch(split_plan(*successors))
    crash.restore()

    orch = env.restart()
    orch.run(max_steps=0)

    assert_split_complete(env, successors)


def test_a_crash_before_the_intent_save_leaves_no_trace_at_all(tmp_path):
    """The boundary BEFORE B0, stated so the matrix has no gap at its start: a
    process that dies while the plan is still being checked has changed
    nothing, and the ask is still outstanding — so the reviewer's answer is not
    silently discarded."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()
    successors = (spec("big-01a"),)

    crash = CrashAt(env.store, "save")
    with pytest.raises(Crash):
        env.orch._dispatch(split_plan(*successors))
    crash.restore()

    assert_nothing_applied(env, [s.id for s in successors])
    state = env.disk_state()
    assert state.split_intent is None
    assert state.split_requested_for == PARENT, "the ask is still unanswered"


# =============================================================================
# 3. the negative half — contradictory durable state is never overwritten
# =============================================================================


def park_with_intent(env: Env, intent: SplitIntent) -> Orchestrator:
    """Put `intent` on disk and hand back a freshly restarted loop, i.e. the
    exact position a crashed acceptance leaves behind."""
    state = env.store.load()
    state.split_intent = intent.to_dict()
    state.phase = Phase.READY.value
    env.store.save(state)
    return env.restart()


def intent_for(env: Env, successors, *, parent=PARENT, label=LABEL):
    """An intent exactly as ACCEPTANCE would have written it — including the
    provenance binding WHICH execution record and WHICH worker repository it
    undertakes to retire.

    Captured through the orchestrator's own capture methods rather than
    hand-built, so a test can never bind an identity the production path would
    not have bound, and the negative tests below are testing the real
    comparison rather than a fixture's idea of one.
    """
    record = env.orch._split_record_provenance(parent)
    worker = env.orch._split_worker_provenance(parent)
    return SplitIntent(
        parent_id=parent,
        successors=tuple(
            {
                "id": s.id,
                "title": s.title,
                "description": s.description,
                "depends_on": tuple(s.depends_on),
                "approved_paths": tuple(s.approved_paths),
            }
            for s in successors
        ),
        reason="split it",
        label=label,
        retire_record=record is not None,
        retire_worker=worker is not None,
        record_provenance=record,
        worker_provenance=worker,
    )


def assert_parked_with_intent_preserved(env: Env, orch: Orchestrator):
    assert orch._resume_split_intent() is False
    state = env.disk_state()
    assert state.phase == Phase.NEEDS_USER.value
    assert state.park_kind == "loop_fatal"
    assert state.split_intent is not None, (
        "the intent is the only record of what was supposed to happen — a park "
        "that discards it discards exactly the evidence this design keeps"
    )


def test_a_successor_id_that_belongs_to_a_different_task_fails_closed(tmp_path):
    """The id exists, with someone else's description and someone else's scope.
    Adopting it would silently reassign that task; refusing to look would let
    the split claim it."""
    env = Env(tmp_path)
    env.seed_execution()
    successors = (spec("big-01a"),)
    intent = intent_for(env, successors)
    registry = env.task_store.load()
    registry.add(
        Task(
            id="big-01a",
            title="Somebody else's task",
            description="planned independently",
            approved_paths=("Z.py",),
        )
    )
    env.task_store.save(registry)

    orch = park_with_intent(env, intent)
    assert_parked_with_intent_preserved(env, orch)

    # ...and nothing was rewritten on the way out.
    stored = env.disk_registry().get("big-01a")
    assert stored.title == "Somebody else's task"
    assert stored.approved_paths == ("Z.py",)
    assert env.disk_registry().state_of(PARENT) is not TaskState.RETIRED


def test_a_parent_already_retired_into_other_successors_fails_closed(tmp_path):
    env = Env(tmp_path)
    env.seed_execution()
    intent = intent_for(env, (spec("big-01a"),))
    registry = env.task_store.load()
    registry.add(Task(id="other-01", title="Other", description="d", approved_paths=("A.py",)))
    registry.retire(PARENT, superseded_by=("other-01",), reason="a different retirement")
    env.task_store.save(registry)

    orch = park_with_intent(env, intent)
    assert_parked_with_intent_preserved(env, orch)

    assert env.disk_registry().get(PARENT).superseded_by == ("other-01",)
    assert not env.disk_registry().has("big-01a")


def test_a_parent_that_completed_is_never_retired_by_a_split(tmp_path):
    """Finished work was not superseded. Retiring it would rewrite a real
    completion and take it off the merge panel."""
    env = Env(tmp_path)
    env.seed_execution()
    intent = intent_for(env, (spec("big-01a"),))
    registry = env.task_store.load()
    registry.mark_completed(PARENT)
    env.task_store.save(registry)

    orch = park_with_intent(env, intent)
    assert_parked_with_intent_preserved(env, orch)

    assert env.disk_registry().state_of(PARENT) is TaskState.COMPLETED


def test_a_record_that_vanished_without_being_archived_fails_closed(tmp_path):
    """Absence is not proof. A deleted record, a half-finished move and a record
    that never existed all look identical from the live side — so the intent
    says there WAS one, and the archive has to be there."""
    env = Env(tmp_path)
    env.seed_execution()
    intent = intent_for(env, (spec("big-01a"),))
    assert intent.retire_record is True
    env.execution_store.clear(PARENT)

    orch = park_with_intent(env, intent)
    assert_parked_with_intent_preserved(env, orch)

    blocker_question = env.disk_state().question
    assert "archived execution record" in blocker_question


def test_a_worker_that_vanished_without_being_quarantined_fails_closed(tmp_path):
    env = Env(tmp_path)
    env.seed_execution()
    intent = intent_for(env, (spec("big-01a"),))
    assert intent.retire_worker is True
    env.worker_repos.remove(PARENT)

    orch = park_with_intent(env, intent)
    assert_parked_with_intent_preserved(env, orch)
    assert "quarantined worker repository" in env.disk_state().question


def replacement_record(env: Env, *, parent=PARENT, base="0" * 40, candidate="1" * 40):
    """A DIFFERENT execution record living at the parent's own path — what a
    re-dispatch, or an operator rebuilding a lost record, leaves behind after an
    intent was written."""
    execution = TaskExecution(
        task_id=parent,
        task_branch=f"autoloop/{parent}",
        worktree_path=str(env.worker_repos.path_for(parent)),
        task_base_sha=base,
        candidate_sha=candidate,
    )
    env.execution_store.save(execution)
    return execution


def test_an_execution_record_replaced_after_acceptance_is_never_archived(tmp_path):
    """The hole a `parent_id`-only intent leaves open. The record at
    `executions/<parent>.json` is addressed by task id, so the thing living
    there can be REPLACED between the crash and the recovery — and retiring by
    id alone would archive that replacement, destroying a live attempt while
    discharging the intent as though the accepted record had been retired."""
    env = Env(tmp_path)
    env.seed_execution()
    intent = intent_for(env, (spec("big-01a"),))
    replacement = replacement_record(env)

    orch = park_with_intent(env, intent)
    assert_parked_with_intent_preserved(env, orch)
    assert "not the one this split accepted" in env.disk_state().question

    # The replacement is exactly where it was, unarchived and unrewritten.
    live = env.execution_store.load(PARENT)
    assert live is not None
    assert live.candidate_sha == replacement.candidate_sha
    assert live.task_base_sha == replacement.task_base_sha
    assert env.archives() == []
    # ...and the worker half never moved either: one half that cannot be
    # identified stops the retirement before ANY store is retired.
    assert env.worker_repos.path_for(PARENT).exists()
    assert env.quarantines() == []
    # The registry half IS applied — it is driven from the intent and matches
    # it — and that is safe precisely because the intent survives the park.
    assert env.disk_registry().state_of(PARENT) is TaskState.RETIRED


def test_a_worker_repository_replaced_after_acceptance_is_never_quarantined(tmp_path):
    """The same hole on the worker side, and the reason the binding cannot be
    the branch: `create()` derives `autoloop/<task_id>` from the id, so a rebuilt
    worker reproduces the branch exactly. Only the repository's own HEAD tells
    the accepted worker from the one that replaced it."""
    env = Env(tmp_path)
    env.seed_execution()
    intent = intent_for(env, (spec("big-01a"),))

    env.worker_repos.remove(PARENT)
    (env.repo_root / "later.txt").write_text("work done afterwards\n", encoding="utf-8")
    run_git(env.repo_root, "add", "-A")
    run_git(env.repo_root, "commit", "-q", "-m", "later")
    newer = run_git(env.repo_root, "rev-parse", "HEAD").strip()
    env.worker_repos.create(PARENT, env.repo_root, newer)
    replacement = env.worker_repos.path_for(PARENT)
    assert newer != intent.worker_provenance["head_sha"]
    assert run_git(replacement, "branch", "--show-current").strip() == (
        intent.worker_provenance["branch"]
    ), "the replacement wears the same branch — only HEAD distinguishes them"

    orch = park_with_intent(env, intent)
    assert_parked_with_intent_preserved(env, orch)
    assert "not the one this split accepted" in env.disk_state().question

    assert replacement.is_dir()
    assert run_git(replacement, "rev-parse", "HEAD").strip() == newer
    assert env.quarantines() == []
    # Nothing moved at all — the record was still live and stays live.
    assert env.execution_store.load(PARENT) is not None
    assert env.archives() == []


def test_an_archived_record_that_is_not_the_accepted_one_never_discharges(tmp_path):
    """The other side of the same move. Both retirement destinations are derived
    from `<parent>-<label>`, which anything can create — so a file sitting at the
    archive path, for this task id and under this very label, must still be
    identified before it is read as proof that the accepted record was
    retired."""
    env = Env(tmp_path)
    execution = TaskExecution(
        task_id=PARENT,
        task_branch=f"autoloop/{PARENT}",
        worktree_path=str(env.worker_repos.path_for(PARENT)),
        task_base_sha=env.base_sha,
    )
    env.execution_store.save(execution)
    intent = intent_for(env, (spec("big-01a"),))
    assert intent.retire_record is True
    assert intent.retire_worker is False, "this test isolates the record half"

    env.execution_store.clear(PARENT)
    archive_dir = env.execution_store.directory / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    impostor = archive_dir / f"{PARENT}-{LABEL}.json"
    impostor.write_text(
        json.dumps(
            {
                "task_id": PARENT,
                "task_branch": "autoloop/somebody-else",
                "worktree_path": "/elsewhere",
                "task_base_sha": "9" * 40,
                "candidate_sha": "",
            }
        ),
        encoding="utf-8",
    )
    # It passes the weaker name/id check, which is exactly why the identity
    # comparison has to be the thing that decides.
    assert archived_record_is_for(impostor, PARENT) is True

    orch = park_with_intent(env, intent)
    assert_parked_with_intent_preserved(env, orch)
    assert "is not the one this split accepted" in env.disk_state().question
    assert json.loads(impostor.read_text(encoding="utf-8"))["task_branch"] == (
        "autoloop/somebody-else"
    ), "a park preserves evidence; it never rewrites what it refused to accept"


def test_a_quarantined_repository_that_is_not_the_accepted_worker_never_discharges(
    tmp_path,
):
    """The worker half of the same rule: a directory at the quarantine path is
    not proof that THIS worker was quarantined."""
    env = Env(tmp_path)
    env.worker_repos.create(PARENT, env.repo_root, env.base_sha)
    intent = intent_for(env, (spec("big-01a"),))
    assert intent.retire_worker is True
    assert intent.retire_record is False, "this test isolates the worker half"

    env.worker_repos.remove(PARENT)
    impostor = env.workers_root.parent / "quarantine" / f"{PARENT}-{LABEL}"
    impostor.mkdir(parents=True)
    run_git(impostor, "init", "-q", "-b", f"autoloop/{PARENT}")
    run_git(impostor, "config", "user.email", "test@example.com")
    run_git(impostor, "config", "user.name", "Test")
    run_git(impostor, "config", "commit.gpgsign", "false")
    (impostor / "not-the-accepted-worker.txt").write_text("elsewhere\n", encoding="utf-8")
    run_git(impostor, "add", "-A")
    run_git(impostor, "commit", "-q", "-m", "impostor")
    impostor_head = run_git(impostor, "rev-parse", "HEAD").strip()

    orch = park_with_intent(env, intent)
    assert_parked_with_intent_preserved(env, orch)
    assert "is not the one this split accepted" in env.disk_state().question
    assert run_git(impostor, "rev-parse", "HEAD").strip() == impostor_head
    assert (impostor / "not-the-accepted-worker.txt").exists()


def test_bookkeeping_written_after_acceptance_still_reconciles(tmp_path):
    """The other direction, and the reason the binding is FIELDS rather than a
    digest of the record file.

    Exercised here: bookkeeping written to the still-LIVE record between
    acceptance and recovery — counters, the attempt ledger, the assumptions and
    the executor's report — none of which changes WHICH attempt the record
    describes, and none of which may therefore turn a recoverable split into a
    permanent park. `cli._clear_fault_budget_on_answer` writes one of exactly
    these fields when an operator answers a blocker, which is how a real loop
    reaches this shape; the archived side needs no equivalent test because a
    record already moved to `archive/` is not written again by anything."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()
    successors = (spec("big-01a"),)

    crash = CrashAt(env.execution_store, "archive")
    with pytest.raises(Crash):
        env.orch._dispatch(split_plan(*successors))
    crash.restore()

    execution = env.execution_store.load(PARENT)
    execution.fault_attempt_count = 0
    execution.attempt_count += 1
    execution.attempt_ledger = ("1|fault|agent_stall",)
    execution.assumptions = ("read the narrowest thing",)
    execution.report_summary = "what the last round claimed"
    env.execution_store.save(execution)

    assert env.restart()._resume_split_intent() is True
    assert_split_complete(env, successors)


def test_a_retirement_flag_that_is_not_a_boolean_is_corruption_not_a_coercion(tmp_path):
    """`bool("false")` is True and `bool(0)` is False, so coercion fails in both
    directions: one fabricates a retirement obligation that can never be
    discharged, the other silently discharges a real one."""
    env = Env(tmp_path)
    env.seed_execution()
    intent = intent_for(env, (spec("big-01a"),))

    fabricates = intent.to_dict()
    fabricates["retire_worker"] = "false"
    with pytest.raises(StateError):
        SplitIntent.from_dict(fabricates)

    discharges = intent.to_dict()
    discharges["retire_record"] = 0
    with pytest.raises(StateError):
        SplitIntent.from_dict(discharges)

    # ...and the loop treats it as corruption rather than running on it.
    state = env.store.load()
    state.split_intent = fabricates
    env.store.save(state)
    orch = env.restart()
    assert orch._resume_split_intent() is False
    assert env.disk_state().split_intent is not None
    assert env.execution_store.load(PARENT) is not None
    assert env.worker_repos.path_for(PARENT).exists()


def test_a_retirement_flag_without_the_identity_it_needs_is_corruption(tmp_path):
    """Both directions of the pairing. A flag with no provenance would let
    recovery retire whatever it finds; provenance with the flag off would
    describe a retirement nobody undertook."""
    env = Env(tmp_path)
    env.seed_execution()
    intent = intent_for(env, (spec("big-01a"),))

    unbound = intent.to_dict()
    unbound.pop("worker_provenance")
    with pytest.raises(StateError):
        SplitIntent.from_dict(unbound)

    unclaimed = intent.to_dict()
    unclaimed["retire_record"] = False
    with pytest.raises(StateError):
        SplitIntent.from_dict(unclaimed)

    someone_else = intent.to_dict()
    someone_else["record_provenance"] = {
        **someone_else["record_provenance"],
        "task_id": "another-task",
    }
    with pytest.raises(StateError):
        SplitIntent.from_dict(someone_else)

    empty_identity = intent.to_dict()
    empty_identity["worker_provenance"] = {
        **empty_identity["worker_provenance"],
        "head_sha": "",
    }
    with pytest.raises(StateError):
        SplitIntent.from_dict(empty_identity)


def test_an_intent_missing_a_retirement_flag_is_corruption_not_a_default(tmp_path):
    """The one place a tolerant `.get(...)` default would be fail-OPEN: reading
    an absent `retire_worker` as False silently discharges a quarantine that
    never happened."""
    env = Env(tmp_path)
    env.seed_execution()
    raw = intent_for(env, (spec("big-01a"),)).to_dict()
    raw.pop("retire_worker")
    state = env.store.load()
    state.split_intent = raw
    env.store.save(state)

    orch = env.restart()
    assert orch._resume_split_intent() is False
    assert env.disk_state().split_intent is not None
    assert env.worker_repos.path_for(PARENT).exists()


def test_two_unfinished_splits_are_never_reconciled_against_each_other(tmp_path):
    """A directive asking to split something else while an intent is still
    outstanding is a state nobody designed, and not one this path may resolve by
    picking a side."""
    env = Env(tmp_path)
    env.seed_execution()
    state = env.orch.state
    state.split_intent = intent_for(env, (spec("big-01a"),)).to_dict()
    state.split_requested_for = PARENT
    state.phase = Phase.EXECUTING.value
    env.store.save(state)

    env.orch._dispatch(split_plan(spec("totally-different")))

    assert env.disk_state().phase == Phase.NEEDS_USER.value
    assert env.disk_state().split_intent is not None
    assert not env.disk_registry().has("totally-different")


# =============================================================================
# 4. refusals — checked while nothing is written, so the ask survives them
# =============================================================================


def assert_refused(env: Env, successor_ids):
    """A refusal changes no store, leaves the ask standing, and goes back to the
    reviewer as an ordinary rejected plan."""
    assert_nothing_applied(env, successor_ids)
    state = env.disk_state()
    assert state.split_intent is None
    assert state.split_requested_for == PARENT, "the ask is still answerable"
    assert state.phase == Phase.READY.value
    assert "plan_rejected" in state.outbox


def test_a_successor_may_not_depend_on_the_task_being_retired(tmp_path):
    """`state_of` counts a dependency satisfied only when it is `completed`, and
    the parent is about to be `retired` — so such a successor is BLOCKED
    forever and the split would produce work nothing can run."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()

    env.orch._dispatch(split_plan(spec("big-01a", depends_on=(PARENT,))))

    assert_refused(env, ["big-01a"])
    assert "never resolves" in env.disk_state().outbox


def test_a_live_third_party_dependent_blocks_the_split(tmp_path):
    env = Env(
        tmp_path,
        tasks=[
            Task(id=PARENT, title="Big", description="too big", approved_paths=("A.py",)),
            Task(
                id="waiter-01",
                title="Waits on it",
                description="d",
                depends_on=(PARENT,),
                approved_paths=("C.py",),
            ),
        ],
    )
    env.seed_execution()
    env.ask_for_split()

    env.orch._dispatch(split_plan(spec("big-01a")))

    assert_refused(env, ["big-01a"])
    assert "waiter-01" in env.disk_state().outbox


def test_a_finished_dependent_does_not_block_a_split(tmp_path):
    """The scoping that matters: a `completed` dependent already got what it
    needed, so counting it would refuse a legitimate split on evidence of
    nothing. The roadmap check stays EXACT — the bystander is named."""
    # ORDER MATTERS in this list: `add_many` resolves dependencies against a
    # candidate graph built in its first pass, so `done-01` may name `PARENT`
    # only because `PARENT` is listed ahead of it. Reordering these two is a
    # silent `unknown_dependency`, not a cosmetic change.
    env = Env(
        tmp_path,
        tasks=[
            Task(
                id=PARENT,
                title="Big",
                description="too big",
                approved_paths=("A.py",),
                status="in_progress",
            ),
            Task(
                id="done-01",
                title="Already finished",
                description="d",
                depends_on=(PARENT,),
                approved_paths=("C.py",),
                status="completed",
            ),
        ],
    )
    env.seed_execution()
    env.ask_for_split()

    successors = (spec("big-01a"),)
    env.orch._dispatch(split_plan(*successors))

    assert_split_complete(env, successors, others=("done-01",))


def test_a_successor_with_no_approved_paths_is_refused(tmp_path):
    """Stricter than an ordinary plan, on purpose: an unscoped task is
    undispatchable, which is a harmless "fix it later" when the task is merely
    queued and a LOST piece of work when it is the only thing continuing a task
    being retired."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()

    env.orch._dispatch(split_plan(spec("big-01a", paths=())))

    assert_refused(env, ["big-01a"])


def test_a_plan_with_no_successors_at_all_is_refused(tmp_path):
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()

    env.orch._dispatch(Directive(decision=Decision.PLAN, reason="drop it", tasks=()))

    assert_refused(env, [])


def test_a_duplicate_successor_id_is_refused_by_the_registrys_own_rules(tmp_path):
    """The structural checks are a DRY RUN of the real mutation, not a second
    implementation of the same rules — so a plan the registry would refuse is
    refused BEFORE the intent is written rather than parking the loop after it."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()

    env.orch._dispatch(split_plan(spec("big-01a"), spec("big-01a", paths=("B.py",))))

    assert_refused(env, [])
    assert "duplicate_task" in env.disk_state().outbox


def test_splitting_stops_at_the_derivation_cap(tmp_path):
    """Work that has already been decomposed twice is not decomposed a third
    time: at some point the answer is to implement the smallest piece, or to say
    what is actually blocking it."""
    env = Env(
        tmp_path,
        tasks=[
            Task(id="root-01", title="Root", description="d", approved_paths=("A.py",)),
            Task(id="mid-01", title="Mid", description="d", approved_paths=("A.py",)),
            Task(id=PARENT, title="Big", description="d", approved_paths=("A.py",)),
        ],
    )
    registry = env.task_store.load()
    registry.retire("root-01", superseded_by=("mid-01",), reason="split")
    registry.retire("mid-01", superseded_by=(PARENT,), reason="split again")
    env.task_store.save(registry)
    env.orch = env.restart()
    env.seed_execution()
    env.ask_for_split()

    assert env.orch._derivation_depth(PARENT) == MAX_DERIVATION_DEPTH

    env.orch._dispatch(split_plan(spec("big-01a")))

    assert_refused(env, ["big-01a"])
    assert "deep" in env.disk_state().outbox


def test_derivation_depth_survives_a_cycle_in_a_hand_edited_chain(tmp_path):
    """It is called from a refusal path, so answering beats raising."""
    env = Env(
        tmp_path,
        tasks=[
            Task(id="a-01", title="A", description="d", approved_paths=("A.py",)),
            Task(id="b-01", title="B", description="d", approved_paths=("A.py",)),
        ],
    )
    registry = env.orch._registry
    registry.get("a-01").superseded_by = ("b-01",)
    registry.get("b-01").superseded_by = ("a-01",)

    assert env.orch._derivation_depth("a-01") == 1


# =============================================================================
# 5. the record itself
# =============================================================================


def test_the_intent_carries_every_field_recovery_needs(tmp_path):
    """Recovery never re-derives anything: the directive is gone with the
    process, so the successors' full definitions, the single retirement label
    and which halves there was anything to retire all live in the record."""
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()
    successors = (spec("big-01a", paths=("A.py", "B.py"), depends_on=()),)

    crash = CrashAt(env.task_store, "save")
    with pytest.raises(Crash):
        env.orch._dispatch(split_plan(*successors))
    crash.restore()

    intent = SplitIntent.from_dict(env.disk_state().split_intent)
    assert set(intent.successors[0]) == set(SPLIT_DEFINITION_KEYS)
    assert intent.successors[0]["approved_paths"] == ("A.py", "B.py")
    assert intent.reason
    assert intent.created_at

    # ...including WHICH record and WHICH repository the two retirement flags
    # are about, read from the stores themselves at acceptance.
    worker = env.worker_repos.path_for(PARENT)
    assert set(intent.record_provenance) == set(SPLIT_RECORD_PROVENANCE_KEYS)
    assert intent.record_provenance == env.execution_store.identity(PARENT)
    assert set(intent.worker_provenance) == set(SPLIT_WORKER_PROVENANCE_KEYS)
    assert intent.worker_provenance["path"] == str(worker)
    assert intent.worker_provenance["branch"] == f"autoloop/{PARENT}"
    assert intent.worker_provenance["head_sha"] == run_git(
        worker, "rev-parse", "HEAD"
    ).strip()


def test_an_intent_round_trips_through_the_state_file_unchanged(tmp_path):
    """JSON has no tuples, so the normalisation on the way in is what keeps the
    reconciliation a comparison rather than a coercion."""
    env = Env(tmp_path)
    original = intent_for(env, (spec("big-01a", paths=("A.py",), depends_on=()),))
    state = env.store.load()
    state.split_intent = original.to_dict()
    env.store.save(state)

    reloaded = SplitIntent.from_dict(env.store.load().split_intent)

    assert reloaded == original


def test_a_split_intent_without_successors_is_corruption():
    with pytest.raises(StateError):
        SplitIntent.from_dict(
            {
                "parent_id": PARENT,
                "successors": [],
                "reason": "",
                "label": "split-x",
                "retire_record": False,
                "retire_worker": False,
            }
        )


def test_a_split_with_nothing_to_retire_still_completes(tmp_path):
    """A task that never dispatched has no execution record and no worker repo.
    Both halves are then honestly recorded as "nothing to do" — which is a
    different claim from "it was retired", and only the intent can tell them
    apart afterwards."""
    env = Env(tmp_path)
    env.ask_for_split()
    successors = (spec("big-01a"),)

    env.orch._dispatch(split_plan(*successors))

    state = env.disk_state()
    assert state.split_intent is None
    registry = env.disk_registry()
    assert registry.state_of(PARENT) is TaskState.RETIRED
    assert registry.get(PARENT).superseded_by == ("big-01a",)
    assert env.archives() == [] and env.quarantines() == []


def test_the_retired_parent_keeps_the_reviewers_reason_on_record(tmp_path):
    env = Env(tmp_path)
    env.seed_execution()
    env.ask_for_split()

    env.orch._dispatch(
        split_plan(spec("big-01a"), reason="three subsystems in one task")
    )

    assert "three subsystems in one task" in env.disk_registry().get(PARENT).blocked_reason


def test_the_archived_record_is_identified_by_its_contents_not_its_name(tmp_path):
    """A name is what the mover chose; `task_id` inside the JSON is what the
    record says it is about. Reading the name would let a stray file dropped at
    the right path pass as proof that a retirement happened."""
    env = Env(tmp_path)
    env.seed_execution()
    archive_dir = env.execution_store.directory / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    impostor = archive_dir / f"{PARENT}-split-20260821T000000Z.json"
    impostor.write_text(json.dumps({"task_id": "someone-else"}), encoding="utf-8")

    assert archived_record_is_for(impostor, PARENT) is False
    assert archived_record_is_for(archive_dir / "missing.json", PARENT) is False
