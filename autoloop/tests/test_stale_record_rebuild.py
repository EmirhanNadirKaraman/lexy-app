"""Stale state is rebuilt at the current head, not handed to an operator
(halt-03, 2026-08-25).

The one claim under test, stated as the loop has to satisfy it:

    With `autonomy.enabled` on, each of `task_base_behind_head`,
    `push_candidate_stale`, `push_candidate_unresolvable`, `state_inconsistent`,
    `audit_revise_no_record` and `changeset_binding_missing` ARCHIVES the stale
    record it is holding, REBUILDS at the current head and RE-DISPATCHES, with
    no operator step. With the flag off every one of them parks exactly as it
    did before, and the five hard halts are unreachable from any of it.

Three things about the shape of this file, because they are the three ways a
test suite for this could look convincing and prove nothing:

* **The task-free half is tested with no task in flight.** Three of the six
  codes carry no task and never could — `state_inconsistent` is the loop's own
  bookkeeping, and `changeset_binding_missing` plus the changeset arm of
  `push_candidate_unresolvable` belong to an operator's changeset, which has no
  roadmap task by construction. halt-02's gate refused to automate anything
  without a task to set aside, so a test that seeded `state.task_execution`
  (as every halt-02 test does) would pass against code that leaves exactly
  those three codes halting the loop. `build(..., in_flight=None)` is the
  fixture that makes that impossible to miss.
* **The archival half runs against real git and a real `WorkerRepoManager`,**
  because the claim is about what is on disk afterwards: an execution record in
  `executions/archive/`, a worker in `quarantine/`, and a task back in the
  queue. Duplicated per this suite's self-contained convention.
* **The refusals are tested as refusals**, one per recut-01 bound. A rebuild
  that archived a published candidate, or one whose verdict was still in
  flight, would satisfy "no operator step" while destroying approvable work —
  that is the failure this feature is one mistake away from.

Self-contained per this codebase's convention (see `test_blockers.py`) — the
config/orchestrator helpers are duplicated here rather than imported from
`test_autonomous_recovery.py` or `test_recut.py`.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
from pathlib import Path

import pytest

from autoloop import cli
from autoloop.blockers import (
    AUTONOMOUS_RECOVERIES,
    HARD_HALT_CODES,
    NO_TASK,
    RECOVER_BY_REBUILDING_AT_HEAD,
    STALE_AUDIT_POINTER,
    STALE_EXECUTION_RECORD,
    STALE_PUSH_BINDING,
    STALE_QUEUED_REVIEW,
    STALE_SESSION_ROUND,
    STALE_RECORD_RECOVERIES,
    TRANSPORT_RECOVERIES,
    AutonomousRecovery,
    BlockerStore,
    autonomous_recovery,
)
from autoloop.config import AutoloopConfig, AutonomyConfig, BrowserConfig
from autoloop.errors import StateCorruptError, TaskGraphError
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    AUTONOMOUS_REBUILD_RETIREMENT_REASON,
    MAX_TASK_RECUTS,
    Orchestrator,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, PendingRequest, Phase, StateStore
from autoloop.tasks import (
    HOLD_ORIGIN_OPERATOR,
    Task,
    TaskRegistry,
    TaskState,
    TaskStore,
)
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import TaskExecution, TaskExecutionStore

URL = "https://chatgpt.com/c/stale-record-test"

#: The six codes halt-03 names, verbatim from the task. Listed here rather than
#: read off the table so the assertions below compare the table against the
#: SPEC, not against itself.
SPECIFIED_CODES = (
    "task_base_behind_head",
    "push_candidate_stale",
    "push_candidate_unresolvable",
    "state_inconsistent",
    "audit_revise_no_record",
    "changeset_binding_missing",
)

#: The three that can be raised with no task behind them at all. Two of the park
#: sites pass no `task_id` and never could, and the third —
#: `push_candidate_unresolvable` — has two sites, one of each kind.
TASK_FREE_CODES = (
    "state_inconsistent",
    "audit_revise_no_record",
    "changeset_binding_missing",
    "push_candidate_unresolvable",
)


# =============================================================================
# helpers — collaborator-free
# =============================================================================


def make_config(tmp_path, *, enabled=False, max_recovery_attempts=2) -> AutoloopConfig:
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers_root",
        autonomy=AutonomyConfig(
            enabled=enabled, max_recovery_attempts=max_recovery_attempts
        ),
    )


def build(tmp_path, *, enabled=True, max_recovery_attempts=2, with_store=True,
          tasks=("t1",), in_flight="t1"):
    """A collaborator-free Orchestrator, enough for every rebuild whose stale
    record is a SESSION POINTER or an approval binding — none of those touch
    git, the execution store or the worker manager.

    `in_flight=None` is the fixture that matters: it is the state the three
    task-free codes are actually raised in, and the one halt-02's gate refused
    to act in.
    """
    config = make_config(
        tmp_path, enabled=enabled, max_recovery_attempts=max_recovery_attempts
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    if in_flight is not None:
        state.task_execution = {"task_id": in_flight}
    store.save(state)
    registry = TaskRegistry([
        Task(id=tid, title=f"Title {tid}", description="d", approved_paths=("a.py",))
        for tid in tasks
    ])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    blocker_store = BlockerStore(config.blockers_dir) if with_store else None
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=None,
        executor=None,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=None,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        blocker_store=blocker_store,
    )
    return orch, config, blocker_store, task_store, registry


def transcript_entries(config, kind) -> list[dict]:
    if not config.transcript_file.exists():
        return []
    out = []
    for line in config.transcript_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("type") == kind:
            out.append(entry.get("data") or {})
    return out


def transcript_types(config) -> list[str]:
    if not config.transcript_file.exists():
        return []
    return [
        json.loads(line)["type"]
        for line in config.transcript_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def emitted_blocker_codes() -> set[str]:
    """Every string literal reachable as the `code=` argument of a
    blocker-emitting call in `orchestrator.py`.

    A local copy of the same walk `test_autonomous_recovery.py` keeps, and local
    for the same reason: this module asserts REACHABILITY, and a reachability
    check that depends on another test module staying importable is a check an
    unrelated edit can switch off.
    """
    from autoloop import orchestrator as orchestrator_module

    tree = ast.parse(inspect.getsource(orchestrator_module))
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name not in ("_to_needs_user", "_to_fault_stop"):
            continue
        for kw in node.keywords:
            if kw.arg != "code":
                continue
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    codes.add(sub.value)
    return codes


# =============================================================================
# helpers — real git, for the one rebuild that archives a record on disk
# =============================================================================


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def make_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True, exist_ok=True)
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


class RealWiring:
    """Everything `task_base_behind_head`'s rebuild touches, for real: a git
    repository whose head has moved past the recorded base, a live
    `TaskExecution` on disk, a `WorkerRepoManager` with a real quarantine
    directory, and a task the registry holds `in_progress`.

    Built as a class rather than a dataclass so the attribute list reads beside
    the construction that fills it.
    """

    def __init__(self, tmp_path, *, enabled=True, review_round=1, recut_count=0,
                 published_sha="", with_worker=False, status="in_progress"):
        self.repo_root = make_repo(tmp_path)
        self.config = make_config(tmp_path, enabled=enabled)
        self.git = GitGateway(self.repo_root, PolicyEngine(self.config.policy))
        self.base_sha = self.git.head_sha()
        # The head walks forward, which is the whole condition: every other
        # task's completion auto-merges while this one waits.
        (self.repo_root / "OTHER.md").write_text("moved on\n", encoding="utf-8")
        run_git(self.repo_root, "add", "-A")
        run_git(self.repo_root, "commit", "-q", "-m", "another task landed")
        self.head_sha = self.git.head_sha()

        self.worker_repos = WorkerRepoManager(
            tmp_path / "workers", tmp_path / "worker-hooks"
        )
        self.execution_store = TaskExecutionStore(tmp_path / "executions")
        self.task_store = TaskStore(self.config.tasks_file)
        self.registry = TaskRegistry([
            Task(id="t1", title="Title t1", description="d",
                 approved_paths=("a.py",), recut_count=recut_count),
            Task(id="t2", title="Title t2", description="d",
                 approved_paths=("b.py",)),
        ])
        if status == "in_progress":
            self.registry.mark_in_progress("t1")
        self.task_store.save(self.registry)

        worker_path = ""
        if with_worker:
            repo = self.worker_repos.create("t1", self.repo_root, self.base_sha)
            worker_path = str(repo.path)
        self.execution = TaskExecution(
            task_id="t1",
            task_branch="autoloop/t1",
            # Empty by default, which is one of the real refusals
            # `_carry_reviewed_candidate_past` returns ("the record names no
            # worker repository") and therefore a genuine way to reach this park.
            worktree_path=worker_path,
            task_base_sha=self.base_sha,
            candidate_sha=self.base_sha,
            review_round=review_round,
            recut_count=recut_count,
            published_sha=published_sha,
        )
        self.execution_store.save(self.execution)

        self.state = LoopState.new(URL)
        self.state.task_execution = {"task_id": "t1", "candidate_sha": self.base_sha}
        self.state.phase = Phase.EXECUTING.value
        self.store = StateStore(self.config.state_file)
        self.store.save(self.state)
        self.blocker_store = BlockerStore(self.config.blockers_dir)
        self.orch = Orchestrator(
            config=self.config,
            store=self.store,
            state=self.state,
            policy=PolicyEngine(self.config.policy),
            git=self.git,
            executor=None,
            transcript=TranscriptLogger(self.config.transcript_file),
            client_factory=None,
            registry=self.registry,
            task_store=self.task_store,
            manifest_store=ManifestStore(self.config.manifests_dir),
            worker_repos=self.worker_repos,
            execution_store=self.execution_store,
            blocker_store=self.blocker_store,
        )

    def park_as_the_site_does(self):
        """Call `_to_needs_user` with exactly the arguments the
        `task_base_behind_head` site passes — see `_rebase_execution_if_stale`.
        `test_the_park_site_still_passes_the_arguments_this_fixture_replays`
        below is what keeps this honest."""
        self.orch._to_needs_user(
            f"task t1: its recorded base {self.base_sha[:12]} is behind the "
            f"branch head {self.head_sha[:12]}...",
            kind="task_fatal",
            code="task_base_behind_head",
            task_id="t1",
            detail=f"base={self.base_sha} head={self.head_sha} review_round=1",
        )


# =============================================================================
# 1. the table
# =============================================================================


def test_the_table_names_all_six_codes_and_one_action():
    """The claim's list, checked against the spec rather than against itself.
    ONE action for all six — they differ only in WHICH record is stale, which is
    what `stale_record` names."""
    assert set(STALE_RECORD_RECOVERIES) == set(SPECIFIED_CODES)
    assert len(STALE_RECORD_RECOVERIES) == 6
    for code, entry in STALE_RECORD_RECOVERIES.items():
        assert entry.code == code, "the table is keyed by its own code"
        assert entry.action == RECOVER_BY_REBUILDING_AT_HEAD
        assert entry.stale_record, f"{code} rebuilds without naming a record"
        assert entry.max_attempts == 1
        assert entry.why.strip(), f"{code} automates without saying why"


def test_every_code_names_a_record_the_orchestrator_has_a_handler_for():
    """A record kind with no handler falls into `_autonomous_rebuild`'s
    fail-closed tail and rebuilds nothing — which would read as coverage while
    the loop still parked."""
    handled = {
        STALE_EXECUTION_RECORD,
        STALE_PUSH_BINDING,
        STALE_QUEUED_REVIEW,
        STALE_AUDIT_POINTER,
        STALE_SESSION_ROUND,
    }
    for code, entry in STALE_RECORD_RECOVERIES.items():
        assert entry.stale_record in handled, f"{code} names an unhandled record"


def test_the_two_halves_of_the_table_are_disjoint_and_merge_into_the_one_lookup():
    """`autonomous_recovery` is the ONE lookup, and it is where the hard-halt
    refusal lives. A caller that consulted either half directly would bypass
    it, so the halves must not be reachable as a substitute for the merge."""
    assert set(TRANSPORT_RECOVERIES).isdisjoint(set(STALE_RECORD_RECOVERIES))
    assert set(AUTONOMOUS_RECOVERIES) == (
        set(TRANSPORT_RECOVERIES) | set(STALE_RECORD_RECOVERIES)
    )
    for code, entry in STALE_RECORD_RECOVERIES.items():
        assert autonomous_recovery(code) is entry


def test_every_stale_record_code_is_one_the_orchestrator_can_actually_raise():
    """The rule halt-02 stated and this inherits: a code no live site can raise
    should be REMOVED, not automated. Automating a dead code reads as coverage
    while covering nothing."""
    unreachable = set(STALE_RECORD_RECOVERIES) - emitted_blocker_codes()
    assert not unreachable, f"automated codes nothing emits: {sorted(unreachable)}"


def test_the_five_hard_halts_stay_disjoint_and_refused_after_the_table_grew():
    """halt-02's guarantee, re-asserted against the MERGED table: adding six
    entries must not have brought a hard halt in with them."""
    assert HARD_HALT_CODES.isdisjoint(set(AUTONOMOUS_RECOVERIES))
    assert HARD_HALT_CODES == {
        "checkout_escape_detected",
        "worker_isolation_violation",
        "primary_checkout_dirty",
        "approved_path_symlink_traversal",
        "prompt_integrity_mismatch",
    }
    assert HARD_HALT_CODES <= emitted_blocker_codes()
    for code in HARD_HALT_CODES:
        assert autonomous_recovery(code) is None


@pytest.mark.parametrize("code", sorted(HARD_HALT_CODES))
@pytest.mark.parametrize("site_kind", ["loop_fatal", "task_fatal"])
def test_a_hard_halt_is_never_rebuilt_and_keeps_its_own_classification(
    tmp_path, code, site_kind
):
    """The property is "autonomous mode does not touch it", not "it stops the
    loop": the five do not share one classification today, so the park has to be
    the one the site asked for, whichever that is."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user(f"task t1: {code}", resume_phase=Phase.EXECUTING.value,
                        kind=site_kind, code=code, task_id="t1")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == site_kind
    assert blocker_store.load(orch.state.park_blocker_id).kind == site_kind
    assert "autonomous_rebuild" not in transcript_types(config)


# =============================================================================
# 2. DEFAULT OFF — the reversibility half
# =============================================================================


@pytest.mark.parametrize("code", SPECIFIED_CODES)
def test_with_the_flag_off_every_named_code_parks_exactly_as_before(tmp_path, code):
    """`task_base_behind_head` parks `task_fatal` at its own site and the other
    five park `loop_fatal`, so the off position is asserted against the SITE's
    classification rather than against one blanket value — a copy-pasted
    `loop_fatal` here would have been a claim about the sites, and a false one.
    """
    site_kind = "task_fatal" if code == "task_base_behind_head" else "loop_fatal"
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=False)
    orch.state.phase = Phase.EXECUTING.value
    orch.state.changeset = {"candidate_sha": "abc", "base_sha": "def"}
    orch.state.outbox = "the operator's packet"

    orch._to_needs_user("something is stale", kind=site_kind, code=code)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == site_kind
    assert orch.state.park_task_id is None
    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert (blocker.kind, blocker.task_id, blocker.code) == (site_kind, NO_TASK, code)
    # And nothing was rebuilt: the stale records are exactly where they were.
    assert orch.state.changeset == {"candidate_sha": "abc", "base_sha": "def"}
    assert orch.state.outbox == "the operator's packet"
    assert "autonomous_rebuild" not in transcript_types(config)


def test_with_the_flag_off_the_execution_record_is_left_on_disk(tmp_path):
    """The half of the off position that matters most: an archival that fired
    with the flag off would be irreversible in the one way a park is not."""
    wiring = RealWiring(tmp_path, enabled=False)

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    assert TaskStore(wiring.config.tasks_file).load().state_of("t1") is (
        TaskState.IN_PROGRESS
    )


# =============================================================================
# 3. the six rebuilds
# =============================================================================


def test_task_base_behind_head_archives_the_record_and_requeues_at_head(tmp_path):
    """The measured case: 15 parks, 18.5h, median 0.54h. The park's own text
    names the remedy ("archive .autoloop/executions/<task>.json to start fresh
    at the current head"); this asserts the loop performs it."""
    wiring = RealWiring(tmp_path)

    wiring.park_as_the_site_does()

    # It did NOT park.
    assert wiring.orch.state.phase == Phase.READY.value
    assert wiring.orch.state.question is None
    assert wiring.orch.state.park_blocker_id is None
    assert "needs_user" not in transcript_types(wiring.config)

    # ARCHIVED, not deleted: the live record is gone and an archived copy exists
    # under this feature's own label.
    assert wiring.execution_store.load("t1") is None
    archived = list((wiring.execution_store.directory / "archive").glob("t1-*.json"))
    assert len(archived) == 1
    assert AUTONOMOUS_REBUILD_RETIREMENT_REASON in archived[0].name

    # REBUILT AT HEAD: the task is back in the queue, so its next dispatch cuts
    # from the current head with an empty tree.
    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.get("t1").recut_count == 1

    # RE-DISPATCHED: an outbox the next `ready` step can actually send.
    assert wiring.orch.state.outbox and "REBUILT AT HEAD" in wiring.orch.state.outbox
    assert wiring.orch.state.task_execution is None
    assert wiring.orch.state.last_response is None

    entries = transcript_entries(wiring.config, "autonomous_rebuild")
    assert len(entries) == 1
    assert entries[0]["stale_record"] == STALE_EXECUTION_RECORD
    assert entries[0]["discarded_base"] == wiring.base_sha
    assert entries[0]["archived_record"] == str(archived[0])

    # The state file agrees with the object: a crash here resumes the rebuild.
    assert StateStore(wiring.config.state_file).load().phase == Phase.READY.value


def test_the_quarantined_worker_and_the_archived_record_name_each_other(tmp_path):
    """`retire_execution`'s one-label rule, inherited rather than re-implemented:
    a human reading either half can find the other."""
    wiring = RealWiring(tmp_path, with_worker=True)

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.READY.value
    entries = transcript_entries(wiring.config, "autonomous_rebuild")
    label = entries[0]["label"]
    assert label.startswith(AUTONOMOUS_REBUILD_RETIREMENT_REASON)
    assert label in entries[0]["archived_record"]
    assert label in entries[0]["quarantined_worker"]
    assert Path(entries[0]["quarantined_worker"]).exists()
    assert not wiring.worker_repos.path_for("t1").exists()


def test_push_candidate_stale_drops_the_binding_and_keeps_the_record(tmp_path):
    """The stale thing here is the APPROVAL BINDING — the park says so in its
    own words ("a later round advanced it"). Archiving the execution record
    beneath it would discard a live candidate over a stale pointer."""
    orch, config, _, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value
    orch.state.sent_postcommits = [
        {"request_id": "alr-1", "postcommit": {"task_id": "t1", "candidate_sha": "old"}},
        {"request_id": "alr-2", "postcommit": {"task_id": "t2", "candidate_sha": "keep"}},
    ]
    orch.state.carry_postcommit = {"task_id": "t1", "candidate_sha": "old"}

    orch._to_needs_user("push refused — the reviewed candidate is stale",
                        kind="loop_fatal", code="push_candidate_stale", task_id="t1")

    assert orch.state.phase == Phase.READY.value
    # Only THIS task's ledger entries went; another task's approval still binds.
    assert [r["request_id"] for r in orch.state.sent_postcommits] == ["alr-2"]
    assert orch.state.carry_postcommit is None
    assert orch.state.last_response is None
    assert "STALE APPROVAL BINDING DISCARDED" in orch.state.outbox
    entry = transcript_entries(config, "autonomous_rebuild")[0]
    assert entry["stale_record"] == STALE_PUSH_BINDING
    assert entry["forgotten_packets"] == ["alr-1"]


def test_push_candidate_unresolvable_on_the_task_arm_drops_the_binding(tmp_path):
    orch, config, _, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("push refused — the candidate no longer resolves",
                        kind="loop_fatal", code="push_candidate_unresolvable",
                        task_id="t1")

    assert orch.state.phase == Phase.READY.value
    assert transcript_entries(config, "autonomous_rebuild")[0]["task_id"] == "t1"


def test_push_candidate_unresolvable_on_the_changeset_arm_drops_the_queued_review(
    tmp_path
):
    """The second producer, which names NO task — `_dispatch_changeset_push`.
    A test that only covered the task arm would pass while the changeset arm
    still halted the loop."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.EXECUTING.value
    orch.state.changeset = {
        "candidate_sha": "cafe", "base_sha": "beef",
        "branch": "main", "dest_ref": "refs/heads/main",
    }

    orch._to_needs_user("changeset push refused — the candidate no longer resolves",
                        kind="loop_fatal", code="push_candidate_unresolvable")

    assert orch.state.phase == Phase.READY.value
    assert orch.state.changeset is None
    entry = transcript_entries(config, "autonomous_rebuild")[0]
    # The whole queued record survives in the transcript — an operator's
    # changeset must never evaporate with nothing saying so.
    assert entry["discarded_changeset"]["candidate_sha"] == "cafe"
    assert entry["discarded_changeset"]["dest_ref"] == "refs/heads/main"


def test_changeset_binding_missing_drops_the_queue_entry_and_keeps_the_outbox(tmp_path):
    """The one code that halts the loop INDEFINITELY: it is raised inside
    `_step_ready` before anything is sent, so every future round refuses at the
    same line for as long as the queue entry stands.

    The outbox is the operator's packet and is left exactly as it stands —
    replacing it with a report would discard the packet in order to explain
    that the packet could not be sent."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.READY.value
    orch.state.outbox = "the operator's own changeset packet"
    orch.state.changeset = {
        "candidate_sha": "c0ffee", "base_sha": "ba5e",
        "branch": "main", "dest_ref": "refs/heads/main",
    }

    orch._to_needs_user("a changeset review is queued but its packet does not "
                        "contain base_sha as literal text",
                        kind="loop_fatal", code="changeset_binding_missing",
                        detail="candidate=c0ffee missing=base_sha")

    assert orch.state.phase == Phase.READY.value
    assert orch.state.changeset is None
    assert orch.state.outbox == "the operator's own changeset packet"
    entry = transcript_entries(config, "autonomous_rebuild")[0]
    assert entry["stale_record"] == STALE_QUEUED_REVIEW
    assert entry["discarded_changeset"]["candidate_sha"] == "c0ffee"


def test_audit_revise_no_record_drops_the_pointer_and_asks_for_a_fresh_audit(tmp_path):
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.EXECUTING.value
    orch.state.current_task = {"task_id": "audit", "title": "repository audit"}

    orch._to_needs_user("revise of the audit pseudo-task has no audit currently "
                        "on record for this session",
                        kind="loop_fatal", code="audit_revise_no_record")

    assert orch.state.phase == Phase.READY.value
    assert orch.state.current_task is None
    assert orch.state.last_response is None
    assert "AUDIT POINTER DISCARDED" in orch.state.outbox
    assert "audit" in orch.state.outbox
    entry = transcript_entries(config, "autonomous_rebuild")[0]
    assert entry["stale_record"] == STALE_AUDIT_POINTER
    assert entry["discarded_pointer"]["task_id"] == "audit"


def test_state_inconsistent_discards_the_half_finished_round(tmp_path):
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.SUBMITTING.value
    orch.state.pending_request = PendingRequest(request_id="alr-9", payload="p")
    orch.state.outbox = "a packet from the inconsistent round"

    orch._to_needs_user("phase=submitting but no pending request",
                        resume_phase=Phase.SUBMITTING.value,
                        kind="loop_fatal", code="state_inconsistent")

    assert orch.state.phase == Phase.READY.value
    assert orch.state.pending_request is None
    assert orch.state.last_response is None
    assert "ROUND REBUILT" in orch.state.outbox
    entry = transcript_entries(config, "autonomous_rebuild")[0]
    assert entry["stale_record"] == STALE_SESSION_ROUND
    assert entry["discarded_request"] == "alr-9"


# =============================================================================
# 4. the task-free half — the gate halt-02 left closed
# =============================================================================


@pytest.mark.parametrize("code", TASK_FREE_CODES)
def test_a_task_free_code_is_rebuilt_with_no_task_in_flight(tmp_path, code):
    """THE load-bearing test of this change. halt-02's gate turned every plan
    off when `_autonomous_set_aside_task` found nothing to quarantine, and three
    of the six codes here carry no task and never could — so a suite that seeded
    `state.task_execution` (as every halt-02 test does) would pass against code
    that leaves exactly the loop-halting codes halting the loop."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.EXECUTING.value
    orch.state.current_task = {"task_id": "audit"}
    orch.state.changeset = {"candidate_sha": "cafe", "base_sha": "beef",
                            "branch": "main", "dest_ref": "refs/heads/main"}
    orch.state.outbox = "packet"

    orch._to_needs_user(f"stale: {code}", kind="loop_fatal", code=code)

    assert orch.state.phase == Phase.READY.value, f"{code} was not rebuilt"
    assert orch.state.park_kind is None
    # The fault is still ON THE RECORD — nothing became invisible.
    assert [b.code for b in blocker_store.all_blockers()] == [code]
    assert transcript_entries(config, "autonomous_rebuild")[0]["code"] == code


def test_the_changeset_arm_is_not_captured_by_an_unrelated_task_in_flight(tmp_path):
    """THE bug an earlier cut of this change shipped, and the reason the gate
    moved rather than being widened.

    `_dispatch_changeset_push` raises `push_candidate_unresolvable` naming NO
    task, while `state.task_execution` routinely names an unrelated task whose
    candidate is still unpublished — an operator can queue a `review-changeset`
    at any moment. With the in-flight fallback consulted, the rebuild took the
    TASK branch: it forgot an innocent task's approval binding and left the
    queued changeset, the record that had actually gone stale, exactly where it
    was. Wrong twice — it damaged approvable work AND did not fix the fault."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight="t1")
    orch.state.phase = Phase.EXECUTING.value
    orch.state.sent_postcommits = [
        {"request_id": "alr-live",
         "postcommit": {"task_id": "t1", "candidate_sha": "live"}},
    ]
    orch.state.carry_postcommit = {"task_id": "t1", "candidate_sha": "live"}
    orch.state.changeset = {"candidate_sha": "cafe", "base_sha": "beef",
                            "branch": "main", "dest_ref": "refs/heads/main"}

    orch._to_needs_user("changeset push refused — the reviewed candidate no "
                        "longer resolves", kind="loop_fatal",
                        code="push_candidate_unresolvable")

    assert orch.state.phase == Phase.READY.value
    # The record that was stale is gone...
    assert orch.state.changeset is None
    # ...and the innocent task's approvable binding is untouched.
    assert [r["request_id"] for r in orch.state.sent_postcommits] == ["alr-live"]
    assert orch.state.carry_postcommit == {"task_id": "t1", "candidate_sha": "live"}
    entry = transcript_entries(config, "autonomous_rebuild")[0]
    assert entry["task_id"] == NO_TASK
    assert entry["discarded_changeset"]["candidate_sha"] == "cafe"


def test_a_refused_task_free_rebuild_quarantines_nobody(tmp_path):
    """The other half of the same fix. A refused rebuild of a session-scoped
    record must park exactly as the loop parks today — `loop_fatal`, naming no
    task — rather than setting aside whichever task happened to be in flight for
    a fault belonging to an operator's changeset."""
    orch, config, blocker_store, task_store, _ = build(
        tmp_path, enabled=True, in_flight="t1", tasks=("t1", "t2")
    )
    orch.state.phase = Phase.READY.value
    orch.state.outbox = "packet"
    orch.state.changeset = None          # nothing queued -> the rebuild refuses

    orch._to_needs_user("unbindable", kind="loop_fatal",
                        code="changeset_binding_missing")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.park_task_id is None
    assert blocker_store.load(orch.state.park_blocker_id).task_id == NO_TASK
    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.state_of("t2") is TaskState.READY


def test_a_task_free_rebuild_never_invents_a_task_to_quarantine(tmp_path):
    """The other half of the same rule: acting without a task must not mean
    acting ON a task. Nothing is set aside, so `run --continuous` keeps every
    task it had."""
    orch, config, _, task_store, _ = build(tmp_path, enabled=True, in_flight=None,
                                           tasks=("t1", "t2"))
    orch.state.phase = Phase.READY.value
    orch.state.outbox = "packet"
    orch.state.changeset = {"candidate_sha": "c", "base_sha": "b",
                            "branch": "main", "dest_ref": "refs/heads/main"}

    orch._to_needs_user("unbindable", kind="loop_fatal",
                        code="changeset_binding_missing")

    reloaded = TaskStore(config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.READY
    assert reloaded.state_of("t2") is TaskState.READY
    assert orch.state.park_task_id is None


def test_a_transport_code_still_needs_a_task_with_nothing_in_flight(tmp_path):
    """halt-02's gate is NARROWED, not removed. A retry-then-set-aside plan with
    no task to set aside still falls through to the ordinary park: the second
    stage cannot happen, so the first must not either."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.AWAITING.value

    orch._to_needs_user("logged out", resume_phase=Phase.AWAITING.value,
                        kind="loop_fatal", code="login_expired")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert blocker_store.load(orch.state.park_blocker_id).task_id == NO_TASK
    assert "autonomous_recovery" not in transcript_types(config)


def test_the_execution_record_rebuild_still_requires_its_task(tmp_path):
    """The one stale record that is genuinely task-scoped. With no task named
    and none in flight there is no record to archive, so it parks."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("base behind head", kind="task_fatal",
                        code="task_base_behind_head")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "autonomous_rebuild" not in transcript_types(config)


# =============================================================================
# 5. the refusals — recut-01's bounds, inherited rather than re-earned
# =============================================================================


def test_a_published_candidate_is_never_archived(tmp_path):
    """budget-01's shape: a record was archived by hand 54 seconds before the
    reviewer returned PUSH for that exact candidate. A loop that archives on its
    own must refuse this, or it rebuilds that incident on a timer."""
    wiring = RealWiring(tmp_path, published_sha="deadbeefcafe")

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "already published" in reasons[0]["reason"]


def test_a_candidate_with_a_verdict_still_in_flight_is_never_archived(tmp_path):
    """The same shape with the packet still outstanding: an approval for it can
    still arrive, so the work may already be approved."""
    wiring = RealWiring(tmp_path)
    wiring.orch.state.sent_postcommits = [
        {"request_id": "alr-live",
         "postcommit": {"task_id": "t1", "candidate_sha": wiring.base_sha}}
    ]

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "outstanding" in reasons[0]["reason"]


def test_an_unreadable_execution_record_is_never_archived(tmp_path):
    """UNREADABLE, not absent. A record this cannot parse may name a published
    candidate or one under review, and archiving it unread destroys the only
    evidence of which — the fail-open this whole design refuses."""
    wiring = RealWiring(tmp_path)
    (wiring.execution_store.directory / "t1.json").write_text("{ not json",
                                                              encoding="utf-8")

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert (wiring.execution_store.directory / "t1.json").exists()
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "cannot be read" in reasons[0]["reason"]


def test_the_recut_cap_bounds_the_rebuild_across_episodes(tmp_path):
    """The bound that makes an automatic archival acceptable. `registry.recut`
    charges `recut_count`, which SURVIVES the archival a count on the execution
    record would not — so a task whose base keeps landing behind the head is
    rebuilt at most `MAX_TASK_RECUTS` times and then parks for a human."""
    wiring = RealWiring(tmp_path, recut_count=MAX_TASK_RECUTS)

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.orch.state.park_kind == "task_fatal"
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and f"cap {MAX_TASK_RECUTS}" in reasons[0]["reason"]


def test_an_operator_hold_is_never_laundered_into_a_rebuild(tmp_path):
    """`recut_obstacle`'s own rule, reached through the same call: a task a
    human said "not now" about is not one the loop may cut again by itself."""
    wiring = RealWiring(tmp_path, status="pending")
    # The documented operator sequence, not a hand-poked field: `operator_block`
    # is what the inbox calls, and it is the only thing that sets
    # `HOLD_ORIGIN_OPERATOR`.
    wiring.registry.operator_block("t1", "parked by hand")
    wiring.task_store.save(wiring.registry)
    assert wiring.registry.get("t1").hold_origin == HOLD_ORIGIN_OPERATOR

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "task_operator_hold" in reasons[0]["reason"]


def test_a_task_the_registry_will_not_move_is_never_archived(tmp_path):
    """A pending task has no round in flight to discard. `recut_obstacle`
    refuses it and the rebuild parks rather than archiving anyway."""
    wiring = RealWiring(tmp_path, status="pending")

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "task_not_in_progress" in reasons[0]["reason"]


def test_without_an_execution_store_and_worker_manager_nothing_is_archived(tmp_path):
    """BOTH halves, exactly as `_dispatch_recut` demands them: retiring the
    record without a worker manager leaves the contaminated worktree where the
    next dispatch looks for it — a rebuild that says it archived and did not."""
    orch, config, _, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value

    orch._to_needs_user("base behind head", kind="task_fatal",
                        code="task_base_behind_head", task_id="t1")

    assert orch.state.phase == Phase.NEEDS_USER.value
    reasons = transcript_entries(config, "autonomous_rebuild_refused")
    assert reasons and "execution store" in reasons[0]["reason"]


def test_a_task_missing_from_the_registry_is_never_archived(tmp_path):
    """Driven through the REAL wiring, not the collaborator-free build: without
    an execution store the earlier refusal fires first and the assertion below
    would be checking a different branch than it names."""
    wiring = RealWiring(tmp_path)

    wiring.orch._to_needs_user("base behind head", kind="task_fatal",
                               code="task_base_behind_head", task_id="ghost-99")

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
    reasons = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert reasons and "not in the registry" in reasons[0]["reason"]


def test_a_binding_rebuild_with_nothing_to_drop_parks_instead(tmp_path):
    """Re-dispatching without having changed anything is theatre that costs a
    round and arrives at the same park."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.EXECUTING.value
    orch.state.changeset = None

    orch._to_needs_user("changeset push refused", kind="loop_fatal",
                        code="push_candidate_unresolvable")

    assert orch.state.phase == Phase.NEEDS_USER.value
    reasons = transcript_entries(config, "autonomous_rebuild_refused")
    assert reasons and "no queued changeset" in reasons[0]["reason"]


def test_an_unbindable_changeset_rebuild_with_no_queue_entry_parks_instead(tmp_path):
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.READY.value
    orch.state.outbox = "packet"
    orch.state.changeset = None

    orch._to_needs_user("unbindable", kind="loop_fatal",
                        code="changeset_binding_missing")

    assert orch.state.phase == Phase.NEEDS_USER.value
    reasons = transcript_entries(config, "autonomous_rebuild_refused")
    assert reasons and "no changeset is queued" in reasons[0]["reason"]


def test_an_unknown_record_kind_rebuilds_nothing(tmp_path):
    """`_autonomous_rebuild` has no fallback branch: an entry that names a
    record nobody wrote a handler for parks, rather than falling into the
    nearest existing handler. This is the guard that keeps the table safe to
    grow."""
    orch, config, _, _, _ = build(tmp_path, enabled=True)
    orch.state.phase = Phase.EXECUTING.value
    plan = AutonomousRecovery(
        code="login_expired", action=RECOVER_BY_REBUILDING_AT_HEAD,
        max_attempts=1, why="test", stale_record="a_kind_nobody_handles",
    )
    orch._autonomy_plan = lambda code: plan

    orch._to_needs_user("whatever", kind="loop_fatal", code="login_expired",
                        task_id="t1")

    assert orch.state.phase == Phase.NEEDS_USER.value
    reasons = transcript_entries(config, "autonomous_rebuild_refused")
    assert reasons and reasons[0]["reason"] == "unknown_record_kind"


def test_without_a_blocker_store_nothing_is_rebuilt(tmp_path):
    """No durable record means no budget to count and no durable question. The
    loop parks exactly as it always did."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, with_store=False,
                                  in_flight=None)
    orch.state.phase = Phase.READY.value
    orch.state.outbox = "packet"
    orch.state.changeset = {"candidate_sha": "c", "base_sha": "b",
                            "branch": "main", "dest_ref": "refs/heads/main"}

    orch._to_needs_user("unbindable", kind="loop_fatal",
                        code="changeset_binding_missing")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.changeset is not None
    assert "autonomous_rebuild" not in transcript_types(config)


def test_a_corrupt_blocker_record_raises_rather_than_licensing_a_rebuild(tmp_path):
    """The fail-open this must not have: a store that cannot be read must never
    answer "nothing open", which would read as a full, unspent budget."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    blocker_store.directory.mkdir(parents=True, exist_ok=True)
    (blocker_store.directory / "blk-t1-001.json").write_text("{ nope",
                                                             encoding="utf-8")
    orch.state.phase = Phase.READY.value
    orch.state.changeset = {"candidate_sha": "c", "base_sha": "b",
                            "branch": "main", "dest_ref": "refs/heads/main"}

    with pytest.raises(StateCorruptError):
        orch._to_needs_user("unbindable", kind="loop_fatal",
                            code="changeset_binding_missing")
    assert "autonomous_rebuild" not in transcript_types(config)
    assert orch.state.changeset is not None


def test_a_non_boolean_enabled_is_not_read_as_consent_to_rebuild(tmp_path):
    """A hand-built config is validated by nothing, so the gate is `is not True`
    rather than a truthiness test — `enabled = "no"` is a truthy string."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    object.__setattr__(config, "autonomy", AutonomyConfig(enabled="yes"))  # type: ignore[arg-type]
    orch.state.phase = Phase.READY.value
    orch.state.outbox = "packet"
    orch.state.changeset = {"candidate_sha": "c", "base_sha": "b",
                            "branch": "main", "dest_ref": "refs/heads/main"}

    orch._to_needs_user("unbindable", kind="loop_fatal",
                        code="changeset_binding_missing")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.changeset is not None


def test_a_config_ceiling_of_zero_performs_no_rebuild_at_all(tmp_path):
    """`max_recovery_attempts` is a CEILING on the table, never a floor: at 0 no
    rebuild happens and the loop parks, which is how an operator switches the
    archival off without switching autonomy off."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, max_recovery_attempts=0,
                                  in_flight=None)
    orch.state.phase = Phase.READY.value
    orch.state.outbox = "packet"
    orch.state.changeset = {"candidate_sha": "c", "base_sha": "b",
                            "branch": "main", "dest_ref": "refs/heads/main"}

    orch._to_needs_user("unbindable", kind="loop_fatal",
                        code="changeset_binding_missing")

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.changeset is not None
    assert "autonomous_rebuild" not in transcript_types(config)


# =============================================================================
# 6. `state_inconsistent` — the one that has to be bounded by a budget
# =============================================================================


def test_a_corrupt_state_error_is_not_rebuilt_at_all(tmp_path):
    """`StateCorruptError` subclasses `StateError`, so it reaches the SAME
    handler in `run` and would be automated as `state_inconsistent`. Rebuilding
    a round on top of a store that cannot be READ is precisely the fail-open
    this design exists to refuse, so the park site vetoes that occurrence.

    Driven through `run()`, so the veto is tested where it is wired rather than
    as a parameter nobody passes."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.READY.value

    def fake_step(phase):
        raise StateCorruptError("tasks.json is unreadable")

    orch._step = fake_step
    assert orch.run(max_steps=3) == Phase.NEEDS_USER.value

    assert orch.state.park_kind == "loop_fatal"
    assert blocker_store.load(orch.state.park_blocker_id).code == "state_inconsistent"
    assert "autonomous_rebuild" not in transcript_types(config)
    assert transcript_entries(config, "state_error")[0]["corrupt"] is True


def test_an_ordinary_state_error_is_rebuilt_through_run(tmp_path):
    """The contrast that makes the test above mean something, and the wiring
    check: the same handler, the same code, `recoverable=True`."""
    orch, config, _, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.READY.value
    calls = []

    def fake_step(phase):
        from autoloop.errors import StateError

        calls.append(phase)
        if len(calls) == 1:
            raise StateError("phase=ready but outbox is empty")
        orch.state.phase = Phase.STOPPED.value

    orch._step = fake_step
    assert orch.run(max_steps=5) == Phase.STOPPED.value

    assert [p.value for p in calls] == [Phase.READY.value, Phase.READY.value]
    assert transcript_entries(config, "state_error")[0]["corrupt"] is False
    assert transcript_entries(config, "autonomous_rebuild")[0]["stale_record"] == (
        STALE_SESSION_ROUND
    )


def test_a_second_inconsistency_with_no_completed_step_between_parks(tmp_path):
    """THE bound for the one rebuild whose stale record is not durable. Every
    other handler removes a record, so the identical fault cannot recur off it;
    this one removes the loop's own round, and the inconsistency underneath can
    outlive it. Its blocker is therefore left OPEN, and the second occurrence
    finds the allowance spent."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.READY.value

    orch._to_needs_user("inconsistent", resume_phase=Phase.READY.value,
                        kind="loop_fatal", code="state_inconsistent")
    assert orch.state.phase == Phase.READY.value  # the one rebuild

    orch.state.phase = Phase.READY.value
    orch._to_needs_user("inconsistent", resume_phase=Phase.READY.value,
                        kind="loop_fatal", code="state_inconsistent")

    assert orch.state.phase == Phase.NEEDS_USER.value, "a second rebuild was granted"
    assert transcript_types(config).count("autonomous_rebuild") == 1
    assert blocker_store.open_recurrences(NO_TASK, "state_inconsistent") == 2


def test_the_state_inconsistent_record_stays_open_until_a_step_completes(tmp_path):
    """The other half of that bound: the record is closed by
    `_close_recovered_blocker` on a step that afterwards COMPLETES, which is the
    only free, honest evidence the inconsistency is behind the loop."""
    orch, config, blocker_store, _, _ = build(tmp_path, enabled=True, in_flight=None)
    orch.state.phase = Phase.READY.value

    orch._to_needs_user("inconsistent", resume_phase=Phase.READY.value,
                        kind="loop_fatal", code="state_inconsistent")

    assert len(blocker_store.open_blockers()) == 1
    orch._close_recovered_blocker()
    closed = blocker_store.all_blockers()
    assert closed[0].resolved_at is not None
    assert closed[0].answer is None, "a machine close must never forge an answer"


# =============================================================================
# 7. the loop actually keeps going
# =============================================================================


def test_the_rebuilt_task_is_offered_to_the_next_round(tmp_path):
    """"Re-dispatch" means the queue can hand it out again, not merely that the
    status string changed. `next_ready` is the function the packet builder
    consults, so this is the observable form of the claim."""
    wiring = RealWiring(tmp_path)

    wiring.park_as_the_site_does()

    reloaded = TaskStore(wiring.config.tasks_file).load()
    offered = reloaded.next_ready()
    assert offered is not None and offered.id == "t1"


def test_a_park_that_still_happens_is_still_a_park_continuous_mode_works_past(
    tmp_path, capsys
):
    """When a rebuild refuses, the loop must land exactly where it landed
    before — including the existing quarantine that lets `run --continuous`
    keep working on the rest of the roadmap."""
    wiring = RealWiring(tmp_path, recut_count=MAX_TASK_RECUTS)

    wiring.park_as_the_site_does()

    verdict = cli._handle_parked_task(
        wiring.config, StateStore(wiring.config.state_file), wiring.task_store,
        wiring.registry, wiring.orch.state,
    )
    assert verdict == "task_fatal"
    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.state_of("t1") is TaskState.BLOCKED_BY_OPERATOR
    assert reloaded.state_of("t2") is TaskState.READY
    capsys.readouterr()


def test_the_park_site_still_passes_the_arguments_this_fixture_replays(tmp_path):
    """`RealWiring.park_as_the_site_does` replays the `task_base_behind_head`
    call rather than driving a failed carry-forward, which is faster and far
    more legible — but a replay that drifts from the site is a test of nothing.

    So the site itself is read: it must still pass this code with a `task_id`
    and `kind="task_fatal"`. Without the `task_id`, the rebuild would have no
    record to archive and every test above would be exercising a call the loop
    never makes."""
    from autoloop import orchestrator as orchestrator_module

    tree = ast.parse(inspect.getsource(orchestrator_module))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_to_needs_user":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        code = kwargs.get("code")
        if not (isinstance(code, ast.Constant) and code.value == "task_base_behind_head"):
            continue
        found.append(kwargs)
    assert len(found) == 1, "the park site moved or was duplicated"
    site = found[0]
    assert isinstance(site.get("kind"), ast.Constant)
    assert site["kind"].value == "task_fatal"
    assert "task_id" in site, "the site no longer names a task to archive"


def test_the_state_inconsistent_site_still_passes_the_recoverable_veto(tmp_path):
    """The veto is the whole reason a corrupt store is not rebuilt on. A site
    that stopped passing it would switch the guard off silently, which is the
    exact failure shape this feature is built to refuse."""
    from autoloop import orchestrator as orchestrator_module

    tree = ast.parse(inspect.getsource(orchestrator_module))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_to_needs_user":
            continue
        kwargs = {kw.arg: kw.value for kw in node.keywords}
        code = kwargs.get("code")
        if isinstance(code, ast.Constant) and code.value == "state_inconsistent":
            found.append(kwargs)
    assert len(found) == 1, "the park site moved or was duplicated"
    assert "recoverable" in found[0], "the corrupt-state veto is no longer passed"


def test_nothing_here_can_publish(tmp_path):
    """A rebuild is an archive-and-requeue and nothing else. The orchestrator is
    built with no publisher at all in every test above, and the one that
    archives on disk asserts the record moved rather than a ref."""
    wiring = RealWiring(tmp_path)
    assert wiring.orch._publisher is None

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.READY.value
    assert "push" not in transcript_types(wiring.config)
    assert wiring.git.head_sha() == wiring.head_sha


def test_a_rebuild_never_touches_another_tasks_record(tmp_path):
    """Scope: the archival names one task, and `t2`'s row must be exactly where
    it was afterwards."""
    wiring = RealWiring(tmp_path)

    wiring.park_as_the_site_does()

    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.get("t2").recut_count == 0
    assert reloaded.state_of("t2") is TaskState.READY


def test_a_registry_refusal_is_reported_rather_than_skipped(tmp_path):
    """strand-01's rule, inherited: a silent `continue` is the fail-open. Every
    refusal here writes `autonomous_rebuild_refused`, because the park an
    operator then sees carries the ORIGINAL question and cannot say the loop
    tried."""
    wiring = RealWiring(tmp_path, status="pending")

    wiring.park_as_the_site_does()

    refused = transcript_entries(wiring.config, "autonomous_rebuild_refused")
    assert len(refused) == 1
    assert refused[0]["code"] == "task_base_behind_head"
    assert refused[0]["reason"]


def test_recut_obstacle_raising_is_caught_rather_than_escaping_the_park(tmp_path):
    """A park handler is the one place a second failure has nowhere to go: an
    exception out of here replaces a recoverable park with a crashed process."""
    wiring = RealWiring(tmp_path)

    def boom(task_id):
        raise TaskGraphError("task_unknown", "gone")

    wiring.registry.recut_obstacle = boom

    wiring.park_as_the_site_does()

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.execution_store.load("t1") is not None
