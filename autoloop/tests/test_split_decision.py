"""A reviewer that judges a task too big to review says so as a DECISION.

split-03, 2026-08-26. The evidence was already conclusive before this task and
none of it needed a tally to confirm:

  * brw-14 (2026-08-24) produced a 416,193-byte range diff against a
    400,000-byte packet cap and parked on `review_packet_build_failed`. It
    PASSED post-commit review — it was refused only because the reviewer could
    not be shown the diff in full. A task can be CORRECT and still
    undeliverable, and nothing in the decision vocabulary could say that.
  * Five task descriptions written that same day each carry a hand-written
    "this is ONE ROADMAP ITEM, NOT ONE COMMIT — produce a split plan if it is
    too large": an operator working around a missing verb five times in one day.
  * auto-02 is 48 park codes in one task, and nothing could propose it be four.

THE ONE CLAIM these tests grade: `contract.Decision` gains a verb the reviewer
can issue when a task cannot be delivered as one reviewable candidate; the
directive carries the proposed SUCCESSORS; and the acceptance that already
existed applies them atomically across the registry, the execution record and
the worker repository. ONE LEVEL ONLY — a successor is never itself split by the
same directive.

The five ways it can fail, one section each below:

  * the successors must be ORDINARY tasks — same registry, same validation, same
    `approved_paths` rules — or the split has moved the problem rather than
    solved it;
  * DEPENDENCIES MUST NOT STRAND: `state_of` counts a dependency satisfied only
    when it is completed, so a successor pointing at the retired parent (or at a
    retired sibling) waits forever with no command able to release it;
  * the parent must not silently VANISH — whatever becomes of it has to be a
    recorded decision a reader can follow from the parent to its successors;
  * splitting must be REFUSABLE, or the verb becomes a way to defer work
    indefinitely;
  * and there must be exactly ONE acceptance path, because a second one is what
    `contract.Decomposition` forbids by name.

Real git and real worker repos throughout, with the `run_git` / executor /
`build` helpers duplicated per this suite's self-contained convention (the same
shape `test_task_split.py` and `test_recut.py` use, and for the same reason:
what these mechanisms claim is about what ends up on disk).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import (
    ACTIVE_DECISIONS,
    CARRIES_TASK_SPECS,
    CONTRACT_INSTRUCTIONS,
    RETIRED_DECISIONS,
    Decision,
    parse_response,
)
from autoloop.errors import ContractError, StateCorruptError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    CEILING_SPLIT_ORIGIN,
    CEILING_SPLIT_RETIREMENT_REASON,
    MAX_SPLIT_DEPTH,
    MAX_TASK_ATTEMPTS,
    MIN_CEILING_SPLIT_TASKS,
    MIN_CHILD_ATTEMPTS,
    REVIEWER_SPLIT_ORIGIN,
    REVIEWER_SPLIT_RETIREMENT_REASON,
    Orchestrator,
    SplitOrigin,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LastResponse, LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import (
    IntentStore,
    TaskExecution,
    TaskExecutionStore,
    split_intents_dir,
)

URL = "https://chatgpt.com/c/test-conversation"

#: Every task here may write both files, so the parent's round and a successor's
#: can touch different paths without a scope refusal.
PATHS = ("docs/A.md", "docs/B.md")

FIRST_PLAN = {
    "approach": "one commit",
    "files": ["docs/A.md"],
    "steps": ["write the file"],
}


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def block(obj) -> str:
    return "Reasoning...\n```json\n" + json.dumps(obj) + "\n```"


def implement_block(task_id="t1", decomposition=None):
    return block(
        {
            "version": 3,
            "decision": "implement",
            "reason": "next",
            "task_id": task_id,
            "decomposition": decomposition or FIRST_PLAN,
        }
    )


def successor(tid, paths=("docs/A.md",), depends_on=None):
    spec = {
        "id": tid,
        "title": f"Successor {tid}",
        "description": "one independently reviewable piece",
        "approved_paths": list(paths),
    }
    if depends_on is not None:
        spec["depends_on"] = list(depends_on)
    return spec


def split_block(
    task_id="t1",
    specs=None,
    reason="the candidate cannot be shown in one piece",
):
    return block(
        {
            "version": 3,
            "decision": "split",
            "reason": reason,
            "task_id": task_id,
            "tasks": list(
                specs if specs is not None else [successor("t1-a"), successor("t1-b")]
            ),
        }
    )


class FakeClient:
    """Minimal conversation double: scripted replies in order."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.submitted: list[tuple[str, str]] = []
        self.persisted: set[str] = set()
        self.closed = False

    def attach(self):
        pass

    def has_request(self, request_id):
        return request_id in self.persisted

    def reconcile(self, request_id):
        return request_id in self.persisted

    def submit(self, request_id, prompt):
        from autoloop.conversation import SubmitResult

        self.submitted.append((request_id, prompt))
        self.persisted.add(request_id)
        return SubmitResult.CONFIRMED

    def await_response(self, request_id):
        if not self.responses:
            raise AssertionError("test script exhausted: no response left")
        entry = self.responses.pop(0)
        return entry(self) if callable(entry) else entry

    def close(self):
        self.closed = True


class WritingExecutor:
    """Writes into the dispatched task's own worker repo and reports success."""

    def __init__(self, workers_root, files=None):
        self.workers_root = Path(workers_root)
        self.files = dict(files or {"docs/A.md": "# first"})
        self.calls: list[tuple] = []

    def execute(self, directive, task):
        self.calls.append((directive, task))
        worker = self.workers_root / task.id
        for rel, content in self.files.items():
            target = worker / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                f"{content}\n<!-- call {len(self.calls)} -->\n", encoding="utf-8"
            )
        return ExecutionOutcome(
            status="ok",
            summary="did it",
            details="details",
            validation="ruff clean",
            changed_paths=tuple(self.files),
        )


@dataclass
class Wiring:
    orch: Orchestrator
    git: GitGateway
    registry: TaskRegistry
    task_store: TaskStore
    execution_store: TaskExecutionStore
    worker_repos: WorkerRepoManager
    executor: WritingExecutor
    config: AutoloopConfig
    store: StateStore
    client: FakeClient
    tmp_path: Path


def make_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


def build(tmp_path, responses=(), tasks=(), files=None, policy=None) -> Wiring:
    repo_root = make_repo(tmp_path)
    policy_config = policy or PolicyConfig(implement_enabled=True)
    git = GitGateway(repo_root, PolicyEngine(policy_config))
    worker_repos = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=policy_config,
        state_dir=tmp_path / ".al",
    )
    task_store = TaskStore(config.tasks_file)
    registry = TaskRegistry(list(tasks))
    task_store.save(registry)

    state = LoopState.new(URL)
    state.outbox = "kickoff report"
    store = StateStore(config.state_file)
    store.save(state)

    executor = WritingExecutor(worker_repos.root_dir, files=files)
    client = FakeClient(responses)
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worker_repos=worker_repos,
        execution_store=execution_store,
        intent_store=IntentStore(tmp_path / "intents"),
        blocker_store=BlockerStore(config.blockers_dir),
        validation_runner=ok_validation,
    )
    return Wiring(
        orch=orch,
        git=git,
        registry=registry,
        task_store=task_store,
        execution_store=execution_store,
        worker_repos=worker_repos,
        executor=executor,
        config=config,
        store=store,
        client=client,
        tmp_path=tmp_path,
    )


def ready_task(tid="t1", **kwargs):
    return Task(
        id=tid, title=f"Title {tid}", description="desc", approved_paths=PATHS, **kwargs
    )


def records(wiring, kind):
    path = wiring.config.transcript_file
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("type") == kind:
            out.append(entry.get("data") or {})
    return out


def denial_codes(wiring):
    return [record.get("code") for record in records(wiring, "policy_denied")]


def dispatch(wiring, response_text):
    """Dispatch one directive the way `_step_executing` would, without the
    request/response round trip."""
    wiring.orch.state.last_response = None
    wiring.orch._dispatch(parse_response(response_text))


def first_round(tmp_path, tasks=None, responses=None):
    """A wiring whose task `t1` has been implemented once: an execution record,
    a committed candidate, and the stored plan."""
    wiring = build(
        tmp_path,
        responses=responses or [implement_block("t1")],
        tasks=tasks or [ready_task("t1")],
    )
    wiring.orch.run(max_steps=4)
    return wiring


def marker_path(wiring, task_id="t1") -> Path:
    """The split-acceptance marker's path, through the SAME helper the loop
    resolves it with."""
    return split_intents_dir(wiring.config.state_dir) / f"{task_id}.json"


def registry_on_disk(wiring) -> TaskRegistry:
    """`tasks.json` re-read from disk. Asserting against the live in-memory
    registry would prove nothing about what is durable."""
    loaded = wiring.task_store.load()
    assert loaded is not None, "tasks.json was never written"
    return loaded


def snapshot(wiring) -> str:
    """The registry as bytes, for the "nothing was changed" assertions."""
    return json.dumps(wiring.registry.to_dict(), sort_keys=True)


# ---------------------------------------------------------------------------
# the contract: the verb exists, is advertised, and its payload is FLAT
# ---------------------------------------------------------------------------


def test_split_parses_with_its_parent_and_its_successors():
    directive = parse_response(split_block("brw-14", reason="416KB in one diff"))
    assert directive.decision is Decision.SPLIT
    assert directive.task_id == "brw-14"
    assert directive.reason == "416KB in one diff"
    assert [spec.id for spec in directive.tasks] == ["t1-a", "t1-b"]
    assert directive.tasks[0].approved_paths == ("docs/A.md",)


def test_a_split_that_names_no_task_is_rejected():
    """A split that names nothing names nothing to decompose. Refused by the
    parser rather than defaulted to "the current task": a verb that retires a
    task must never guess which work it is about."""
    with pytest.raises(ContractError) as exc:
        parse_response(
            block(
                {
                    "version": 3,
                    "decision": "split",
                    "reason": "too big",
                    "tasks": [successor("t1-a")],
                }
            )
        )
    assert exc.value.code == "missing_field:task_id"


def test_a_split_that_names_no_successors_is_rejected():
    with pytest.raises(ContractError) as exc:
        parse_response(
            block(
                {
                    "version": 3,
                    "decision": "split",
                    "reason": "too big",
                    "task_id": "t1",
                }
            )
        )
    assert exc.value.code == "missing_field:tasks"
    # ...and the correction names the decision that was actually sent, not
    # `plan`, which is the other decision that carries this key.
    assert "'split'" in str(exc.value)


def test_a_successor_cannot_carry_a_split_of_its_own():
    """ONE LEVEL, ENFORCED BY THE PAYLOAD'S SHAPE. `TaskSpec` has no key through
    which a successor could nest another proposal, so a recursive directive dies
    at `unknown_keys` rather than being applied — recursion is not refused by a
    check that could be forgotten, it is unrepresentable."""
    nested = successor("t1-a")
    nested["tasks"] = [successor("t1-a-i")]
    with pytest.raises(ContractError) as exc:
        parse_response(split_block("t1", specs=[nested, successor("t1-b")]))
    assert exc.value.code == "unknown_keys"


def test_a_split_may_not_carry_a_decomposition():
    """`Decomposition` STAYS PROSE. A plan attached here would be the second
    split mechanism its own docstring forbids by name."""
    with pytest.raises(ContractError) as exc:
        parse_response(
            block(
                {
                    "version": 3,
                    "decision": "split",
                    "reason": "too big",
                    "task_id": "t1",
                    "tasks": [successor("t1-a"), successor("t1-b")],
                    "decomposition": FIRST_PLAN,
                }
            )
        )
    assert exc.value.code == "unexpected_field"


def test_split_is_an_active_decision_and_is_advertised():
    assert Decision.SPLIT in ACTIVE_DECISIONS
    assert Decision.SPLIT not in RETIRED_DECISIONS
    assert "split" in CONTRACT_INSTRUCTIONS


def test_exactly_two_decisions_carry_a_task_batch():
    """`plan` ADDS tasks; `split` proposes SUCCESSORS. Both use `TaskSpec`, and
    that identity is the machine-checkable half of "the successors must be
    ordinary tasks" — a bespoke successor type here is how they would stop
    being."""
    assert CARRIES_TASK_SPECS == {Decision.PLAN, Decision.SPLIT}


def test_the_instructions_distinguish_split_from_revise_and_recut():
    """A verb offered without its boundary is one the reviewer has to guess at.
    `revise` is the same task at the same SIZE, `recut` the same task from a
    clean BASE, `split` the task being too big to review at all."""
    text = CONTRACT_INSTRUCTIONS
    assert "`split` vs `revise` vs `recut`" in text
    assert "too big" in text
    # ...and the one-level rule reaches the reviewer, not just the code.
    assert "ONE LEVEL" in text


def test_the_unknown_decision_correction_now_offers_split():
    with pytest.raises(ContractError) as exc:
        parse_response(block({"version": 3, "decision": "nope", "reason": "r"}))
    assert exc.value.code == "unknown_decision"
    assert "split" in str(exc.value)
    assert "ask_user" not in str(exc.value)


# ---------------------------------------------------------------------------
# the successors are ORDINARY tasks, and the parent does not vanish
# ---------------------------------------------------------------------------


def test_a_split_retires_the_parent_into_the_successors_it_names(tmp_path):
    """THE claim, in its narrowest form."""
    wiring = first_round(tmp_path)
    dispatch(wiring, split_block("t1"))

    loaded = registry_on_disk(wiring)
    parent = loaded.get("t1")
    assert parent.status == "retired"
    assert parent.superseded_by == ("t1-a", "t1-b")
    assert loaded.has("t1-a") and loaded.has("t1-b")


def test_the_successors_are_tasks_the_registry_can_schedule(tmp_path):
    """"Same registry, same validation, same approved_paths rules." A successor
    only the splitter understands would have moved the problem."""
    wiring = first_round(tmp_path)
    dispatch(wiring, split_block("t1"))

    loaded = registry_on_disk(wiring)
    for tid in ("t1-a", "t1-b"):
        child = loaded.get(tid)
        assert loaded.state_of(tid) is TaskState.READY
        assert child.approved_paths == ("docs/A.md",)
        assert child.decomposition == ""  # each needs its own approved plan
        assert child.split_depth == 1
    # The registry can hand one straight to the next dispatch.
    assert loaded.next_ready() is not None


def test_the_successors_inherit_the_parents_spend(tmp_path):
    """A split is a re-scoping, not a fresh allowance — the same anti-refund
    rule a ceiling decomposition carries, reached through the same code."""
    wiring = first_round(tmp_path)
    spent = wiring.execution_store.load("t1").attempt_count
    assert spent >= 1
    dispatch(wiring, split_block("t1"))

    loaded = registry_on_disk(wiring)
    child = loaded.get("t1-a")
    assert child.inherited_attempts == spent
    assert wiring.orch._attempt_cap_for(child) < MAX_TASK_ATTEMPTS


def test_the_parent_is_followable_from_its_row_to_its_successors(tmp_path):
    """"The parent must not silently vanish." Three records say where it went:
    the registry row's `superseded_by`, the retirement reason, and a transcript
    event carrying the reviewer's own words."""
    wiring = first_round(tmp_path)
    dispatch(wiring, split_block("t1", reason="416KB in one diff"))

    parent = registry_on_disk(wiring).get("t1")
    assert parent.superseded_by == ("t1-a", "t1-b")
    assert "t1-a" in parent.blocked_reason and "t1-b" in parent.blocked_reason
    assert "reviewer" in parent.blocked_reason

    events = records(wiring, "task_reviewer_split")
    assert len(events) == 1
    assert events[0]["task_id"] == "t1"
    assert events[0]["children"] == ["t1-a", "t1-b"]
    assert events[0]["reason"] == "416KB in one diff"


def test_the_parents_record_and_worker_are_filed_under_the_reviewers_label(tmp_path):
    """Nothing is deleted, and the label says which DECISION moved it. An
    operator reading `quarantine/` has to be able to tell a task that ran out of
    attempts from one the reviewer judged undeliverable in one piece."""
    wiring = first_round(tmp_path)
    dispatch(wiring, split_block("t1"))

    archived = sorted((wiring.tmp_path / "executions" / "archive").glob("t1-*.json"))
    quarantined = sorted((wiring.tmp_path / "quarantine").glob("t1-*"))
    assert len(archived) == 1 and len(quarantined) == 1
    assert REVIEWER_SPLIT_RETIREMENT_REASON in archived[0].name
    assert REVIEWER_SPLIT_RETIREMENT_REASON in quarantined[0].name
    assert CEILING_SPLIT_RETIREMENT_REASON not in archived[0].name
    # ...and the live halves are gone, so the merge window is not held shut.
    assert not (wiring.tmp_path / "executions" / "t1.json").exists()
    assert not (wiring.tmp_path / "workers" / "t1").exists()


def test_a_dependent_of_the_split_parent_is_repointed_at_the_successors(tmp_path):
    """The retirement's own strand precondition, reached through this verb: a
    task waiting on the parent is re-pointed at the successors rather than left
    waiting on a retired id forever."""
    wiring = first_round(
        tmp_path, tasks=[ready_task("t1"), ready_task("t2", depends_on=("t1",))]
    )
    dispatch(wiring, split_block("t1"))

    loaded = registry_on_disk(wiring)
    assert loaded.get("t2").depends_on == ("t1-a", "t1-b")
    assert "t1" not in loaded.get("t2").depends_on


def test_the_report_states_what_landed_and_what_is_now_spent(tmp_path):
    wiring = first_round(tmp_path)
    dispatch(wiring, split_block("t1", reason="416KB in one diff"))

    report = wiring.orch.state.outbox or ""
    assert "SPLIT APPLIED" in report
    assert "t1-a" in report and "t1-b" in report
    assert "416KB in one diff" in report
    assert "NOT REFUNDED" in report
    # The consequence a reviewer will otherwise discover by being refused: the
    # one level of subdivision is now spent for these successors.
    assert "ONE LEVEL IS NOW SPENT" in report
    assert str(MAX_SPLIT_DEPTH) in report


def test_a_split_never_reaches_the_executor(tmp_path):
    """`_dispatch`'s terminal `else` routes to `_dispatch_executor`. A `split`
    that fell through it would be run as if it were an `implement` — against the
    very task the reviewer just said is too big to work as one."""
    wiring = first_round(tmp_path)
    before = len(wiring.executor.calls)
    dispatch(wiring, split_block("t1"))
    assert len(wiring.executor.calls) == before
    assert wiring.orch.state.phase == Phase.READY.value


def test_a_split_clears_a_pending_ceiling_request(tmp_path):
    """A task can be split by the reviewer while a ceiling classification is
    still standing. A retired row that kept the marker would be picked up as the
    pending parent of the next unrelated `plan` — a split applied to the wrong
    task."""
    wiring = first_round(tmp_path)
    wiring.registry.request_ceiling_plan("t1", "2026-08-26T00:00:00Z")
    wiring.task_store.save(wiring.registry)
    dispatch(wiring, split_block("t1"))

    loaded = registry_on_disk(wiring)
    assert loaded.get("t1").ceiling_plan_requested_at == ""
    assert loaded.ceiling_plan_pending() == []


# ---------------------------------------------------------------------------
# dependencies must not strand
# ---------------------------------------------------------------------------


def test_a_successor_depending_on_the_parent_is_refused(tmp_path):
    """The parent is being RETIRED, and `state_of` satisfies a dependency only
    on a completed (or shipped-elsewhere) task — so such a successor waits
    forever and no supported command releases it.

    Refused BEFORE anything moves, which is the point: `add_many` accepts the
    edge (the parent is still live at that moment) and `retire` then re-points
    it at every live sibling, which for an all-successors-name-the-parent plan
    is a cycle `_check_acyclic` raises on from INSIDE `retire`, after the
    children are already in the registry — the half-applied park."""
    wiring = first_round(tmp_path)
    before = snapshot(wiring)
    dispatch(
        wiring,
        split_block(
            "t1",
            specs=[
                successor("t1-a", depends_on=["t1"]),
                successor("t1-b", depends_on=["t1"]),
            ],
        ),
    )
    assert denial_codes(wiring)[-1] == "reviewer_split_dependency_stranded"
    assert snapshot(wiring) == before
    assert not marker_path(wiring).exists()
    assert not wiring.registry.has("t1-a")


def test_a_successor_depending_on_a_retired_task_is_refused(tmp_path):
    """The same rule for a sibling that is already terminal. `--rewrite-dependents`
    had to be used by hand twice on 2026-08-24 to clear exactly this."""
    wiring = first_round(tmp_path, tasks=[ready_task("t1"), ready_task("old")])
    wiring.registry.retire("old", reason="stale")
    wiring.task_store.save(wiring.registry)
    dispatch(
        wiring,
        split_block(
            "t1",
            specs=[successor("t1-a", depends_on=["old"]), successor("t1-b")],
        ),
    )
    assert denial_codes(wiring)[-1] == "reviewer_split_dependency_stranded"
    assert not wiring.registry.has("t1-a")


def test_a_successor_may_still_depend_on_a_sibling(tmp_path):
    """The refusal is about UNREACHABLE dependencies, not about ordering. Two
    successors that must land in order is an ordinary roadmap shape and stays
    legal — refusing it would push the reviewer back to prose."""
    wiring = first_round(tmp_path)
    dispatch(
        wiring,
        split_block(
            "t1", specs=[successor("t1-a"), successor("t1-b", depends_on=["t1-a"])]
        ),
    )
    loaded = registry_on_disk(wiring)
    assert loaded.get("t1-b").depends_on == ("t1-a",)
    assert loaded.state_of("t1-b") is TaskState.BLOCKED  # waits, but is reachable
    assert loaded.state_of("t1-a") is TaskState.READY


def test_every_successor_stays_reachable_once_the_parent_is_retired(tmp_path):
    """The claim the constraint actually states: PROVE that a split whose parent
    is retired leaves every successor reachable. Walked from the persisted
    graph, not from the accepted directive."""
    wiring = first_round(tmp_path, tasks=[ready_task("t1"), ready_task("done")])
    wiring.registry.mark_completed("done")
    wiring.task_store.save(wiring.registry)
    dispatch(
        wiring,
        split_block(
            "t1",
            specs=[
                successor("t1-a", depends_on=["done"]),
                successor("t1-b", depends_on=["t1-a"]),
            ],
        ),
    )
    loaded = registry_on_disk(wiring)
    assert loaded.get("t1").status == "retired"
    for tid in ("t1-a", "t1-b"):
        for dep in loaded.get(tid).depends_on:
            # Reachable == the dependency can still reach `completed`. A retired
            # dependency never can, and the retired parent is the one this
            # operation just created.
            assert loaded.get(dep).status != "retired"


def test_an_in_progress_dependent_refuses_the_split_before_anything_moves(tmp_path):
    wiring = first_round(
        tmp_path, tasks=[ready_task("t1"), ready_task("t2", depends_on=("t1",))]
    )
    wiring.registry.get("t2").status = "in_progress"
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1"))
    assert denial_codes(wiring)[-1] == "reviewer_split_dependent_in_progress"
    assert snapshot(wiring) == before


# ---------------------------------------------------------------------------
# splitting is REFUSABLE — or the verb defers work indefinitely
# ---------------------------------------------------------------------------


def test_a_one_successor_split_is_refused(tmp_path):
    """A task that is already ONE claim is not too big to review. One successor
    inherits the parent's spend and hands the SAME unit of work a fresh floor of
    attempts under a new id — a rename that buys budget."""
    wiring = first_round(tmp_path)
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1", specs=[successor("t1-a")]))
    assert denial_codes(wiring)[-1] == "reviewer_split_too_small"
    assert snapshot(wiring) == before
    assert str(MIN_CEILING_SPLIT_TASKS) in records(wiring, "policy_denied")[-1]["reason"]


def test_a_successor_of_an_earlier_split_cannot_be_split_again(tmp_path):
    """ONE LEVEL, at the other end: the successors of a split are ordinary tasks
    in every respect except this one. Without the bound the verb is a way to
    defer work forever — "one testable claim" is a judgement, and it can always
    be applied again.

    The successor here has no execution record at all, which is the point of
    checking it this way: the depth refusal runs BEFORE the record is read, so a
    subtask cannot be split again whatever state its own round is in."""
    wiring = first_round(tmp_path)
    dispatch(wiring, split_block("t1"))
    child = wiring.registry.get("t1-a")
    assert child.split_depth == MAX_SPLIT_DEPTH

    before = snapshot(wiring)
    dispatch(
        wiring,
        split_block("t1-a", specs=[successor("t1-a-i"), successor("t1-a-ii")]),
    )
    assert denial_codes(wiring)[-1] == "reviewer_split_depth"
    assert snapshot(wiring) == before
    assert not wiring.registry.has("t1-a-i")


def test_a_task_shipped_under_another_id_is_refused(tmp_path):
    """The one terminal status `policy._check_task_reference` has NO arm for, so
    it falls through as allowed. Reaching the acceptance with such a parent adds
    the successors and only THEN meets `retire`'s own `task_shipped_elsewhere`
    refusal — from inside `release_task_to_pending`, after the children exist,
    which is the half-applied park. Refused on this path instead."""
    wiring = first_round(tmp_path)
    # Set directly rather than through `record_shipped_elsewhere`, which refuses
    # an `in_progress` task — and `in_progress` is exactly the state a split's
    # parent is in. What is being graded is the guard's read of `state_of`, and
    # that answers on the stored status alone.
    wiring.registry.get("t1").status = "shipped_elsewhere"
    wiring.task_store.save(wiring.registry)
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1"))
    assert denial_codes(wiring)[-1] == "reviewer_split_task_terminal"
    assert snapshot(wiring) == before
    assert not wiring.registry.has("t1-a")
    assert registry_on_disk(wiring).get("t1").status == "shipped_elsewhere"


@pytest.mark.parametrize("bad", ["1", True, -1, None, 1.0])
def test_a_split_depth_that_cannot_be_read_refuses_rather_than_reading_as_zero(
    tmp_path, bad
):
    """THE FAIL-OPEN this bound is written around. `_nonneg_int` answers 0 for a
    value it cannot read, which is right where a number is SPENT and wrong where
    it is a BOUND — depth 0 means "may be split", so an unreadable counter would
    switch the one-level rule off at the moment it is doing work.

    `tasks._persisted_nonneg_int` refuses such a value at LOAD, so this is
    unreachable from a `tasks.json` on disk; it covers a `Task` handed in by an
    embedder or a test, and `True`, which is an `int` in Python."""
    wiring = first_round(tmp_path)
    wiring.registry.get("t1").split_depth = bad
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1"))
    assert denial_codes(wiring)[-1] == "reviewer_split_depth"
    assert snapshot(wiring) == before
    assert not wiring.registry.has("t1-a")


def test_a_split_naming_the_parent_as_its_own_successor_is_refused(tmp_path):
    wiring = first_round(tmp_path)
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1", specs=[successor("t1"), successor("t1-b")]))
    assert denial_codes(wiring)[-1] == "reviewer_split_names_parent"
    assert snapshot(wiring) == before


def test_a_task_that_has_produced_no_candidate_is_refused(tmp_path):
    """THE EVIDENCE REQUIREMENT. "Too large to review" is a judgement about
    something the reviewer has SEEN. A split proposed before any work exists is
    a re-scoping that defers a task nobody has attempted, which is precisely the
    indefinite deferral this verb must not become.

    Deliberately NOT applied to the ceiling trigger, which reads an attempt
    ledger instead and is tested to tolerate an uncommitted candidate
    (`test_task_split.py::test_a_candidate_that_was_never_committed_still_
    produces_a_request`)."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    wiring.execution_store.save(
        TaskExecution(
            task_id="t1",
            task_branch="autoloop/t1",
            worktree_path=str(wiring.tmp_path / "workers" / "t1"),
            task_base_sha=wiring.git.head_sha(),
            attempt_count=1,
        )
    )
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1"))
    assert denial_codes(wiring)[-1] == "reviewer_split_no_candidate"
    assert snapshot(wiring) == before


def test_a_task_that_was_never_dispatched_is_refused(tmp_path):
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    dispatch(wiring, split_block("t1"))
    assert denial_codes(wiring)[-1] == "reviewer_split_no_execution"


def test_a_published_parent_is_never_split(tmp_path):
    """Published work is never retired by this loop — the same bound `recut`
    carries, and for the same reason."""
    wiring = first_round(tmp_path)
    execution = wiring.execution_store.load("t1")
    execution.published_sha = "c" * 40
    wiring.execution_store.save(execution)
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1"))
    assert denial_codes(wiring)[-1] == "reviewer_split_candidate_published"
    assert snapshot(wiring) == before


def test_an_unreadable_record_is_refused_rather_than_guessed_at(tmp_path):
    """Unreadable, NOT absent. A record this cannot parse may name a published
    candidate, and it carries the spend every successor's budget derives from —
    so it fails CLOSED."""
    wiring = first_round(tmp_path)

    def boom(_task_id):
        raise StateCorruptError("record is not JSON")

    wiring.execution_store.load = boom  # type: ignore[method-assign]
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1"))
    assert denial_codes(wiring)[-1] == "reviewer_split_record_unreadable"
    assert snapshot(wiring) == before


def test_an_audit_unit_is_refused_by_name(tmp_path):
    """Refused by NAME rather than by registry lookup: most audit units are not
    in the registry at all, so a lookup would answer `task_unknown` and send the
    reviewer looking for a planning mistake that never happened."""
    wiring = first_round(tmp_path)
    dispatch(wiring, split_block("audit-2026-08-26"))
    assert denial_codes(wiring)[-1] == "reviewer_split_audit_unit"
    dispatch(wiring, split_block("audit"))
    assert denial_codes(wiring)[-1] == "reviewer_split_audit_unit"


def test_an_unknown_task_is_refused(tmp_path):
    wiring = first_round(tmp_path)
    dispatch(wiring, split_block("nope"))
    assert denial_codes(wiring)[-1] == "task_unknown"


def test_a_split_of_a_completed_task_is_refused_by_policy(tmp_path):
    """The graph check every task-naming decision passes, applied to this one.
    Refused at the policy gate, before dispatch, so nothing is spent."""
    wiring = first_round(tmp_path)
    wiring.registry.mark_completed("t1")
    verdict = wiring.orch._policy.authorize_directive(
        parse_response(split_block("t1")), "feature/x", wiring.registry
    )
    assert not verdict.allowed
    assert verdict.code == "task_completed"


def test_a_split_of_a_retired_task_names_its_successor(tmp_path):
    """A reviewer asking to split an already-split task wants its continuation,
    not a bare refusal — and `task_retired`'s denial is the one place that
    lookup is written."""
    wiring = first_round(tmp_path)
    dispatch(wiring, split_block("t1"))
    verdict = wiring.orch._policy.authorize_directive(
        parse_response(split_block("t1", specs=[successor("x"), successor("y")])),
        "feature/x",
        wiring.registry,
    )
    assert not verdict.allowed
    assert verdict.code == "task_retired"
    assert "t1-a" in verdict.reason


# ---------------------------------------------------------------------------
# never discard work that may already be approved
# ---------------------------------------------------------------------------


def _present_candidate(wiring, request_id, task_id="t1"):
    """Record that this loop PRESENTED `task_id`'s candidate under
    `request_id`, exactly as `_record_sent_postcommit` does."""
    execution = wiring.execution_store.load(task_id)
    wiring.orch.state.sent_postcommits = [
        {
            "request_id": request_id,
            "head_sha": execution.candidate_sha,
            "report_sha256": "b" * 64,
            "postcommit": {
                "task_id": task_id,
                "task_branch": execution.task_branch,
                "base_sha": execution.task_base_sha,
                "candidate_sha": execution.candidate_sha,
                "candidate_tree_sha": "d" * 40,
                "packet_sha256": "b" * 64,
            },
        }
    ]
    return execution


def test_a_split_is_refused_while_a_verdict_for_the_candidate_is_in_flight(tmp_path):
    """budget-01's shape with the reviewer in the operator's seat: work
    destroyed while an approval for it could still arrive. A packet this reply
    does not answer stays approvable, so the candidate is not discarded."""
    wiring = first_round(tmp_path)
    _present_candidate(wiring, "alr-x-0007")
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1"))  # `dispatch` answers no request at all
    assert denial_codes(wiring)[-1] == "reviewer_split_verdict_outstanding"
    assert "alr-x-0007" in records(wiring, "policy_denied")[-1]["reason"]
    assert snapshot(wiring) == before


def test_a_split_answering_the_packet_that_presented_it_is_allowed(tmp_path):
    """The positive control, and the verb's PRIMARY use: brw-14's reviewer is
    reading the packet for that very candidate. Refusing this case would refuse
    every real split."""
    wiring = first_round(tmp_path)
    _present_candidate(wiring, "alr-x-0007")
    wiring.orch.state.last_response = LastResponse(
        request_id="alr-x-0007", raw="", received_at="2026-08-26T00:00:00Z"
    )
    wiring.orch._dispatch(parse_response(split_block("t1")))
    assert "reviewer_split_verdict_outstanding" not in denial_codes(wiring)
    assert registry_on_disk(wiring).get("t1").status == "retired"


def test_a_split_is_refused_while_this_loop_awaits_a_reply(tmp_path):
    """Defence in depth against a state the ordinary single-request flow does
    not reach: never retire a candidate while the loop is waiting to hear about
    one."""
    from autoloop.state import PendingRequest

    wiring = first_round(tmp_path)
    wiring.orch.state.pending_request = PendingRequest(
        request_id="alr-x-0009", payload="..."
    )
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1"))
    assert denial_codes(wiring)[-1] == "reviewer_split_verdict_outstanding"
    assert snapshot(wiring) == before


# ---------------------------------------------------------------------------
# ONE acceptance path, two triggers
# ---------------------------------------------------------------------------


def test_the_two_origins_differ_only_in_labels(tmp_path):
    """`SplitOrigin` is the whole of the difference between the two triggers.
    Every field differs (a shared code or label would make one trigger
    unreadable in the other's logs), and there is no field on it through which a
    second acceptance could be supplied."""
    ceiling = CEILING_SPLIT_ORIGIN
    reviewer = REVIEWER_SPLIT_ORIGIN
    fields = [f for f in SplitOrigin.__dataclass_fields__]
    assert fields  # sanity: the walk below is not vacuous
    for name in fields:
        assert getattr(ceiling, name) != getattr(reviewer, name), name
        assert isinstance(getattr(reviewer, name), str)
    # Every code is a literal on the table rather than an f-string at the emit
    # site, so `rg <code>` still lands somewhere useful.
    codes = [getattr(reviewer, n) for n in fields if n.startswith("code_")]
    assert len(codes) == len(set(codes))
    assert all(code.startswith("reviewer_split_") for code in codes)


def test_the_ceiling_trigger_keeps_the_codes_and_label_it_shipped_with():
    """The refactor that gave the acceptance two callers MOVED these strings
    into a table; it did not rename them. Operator tooling, docs and blocker
    records all know them."""
    assert CEILING_SPLIT_ORIGIN.label == CEILING_SPLIT_RETIREMENT_REASON
    assert CEILING_SPLIT_ORIGIN.log_event == "task_ceiling_split"
    for code in (
        "ceiling_split_unavailable",
        "ceiling_split_depth",
        "ceiling_split_too_small",
        "ceiling_split_names_parent",
        "ceiling_split_dependent_in_progress",
        "ceiling_split_record_unreadable",
        "ceiling_split_no_execution",
        "ceiling_split_candidate_published",
        "ceiling_split_intent_unwritable",
        "ceiling_split_parent_not_retired",
        "ceiling_split_retirement_failed",
    ):
        assert code in {
            getattr(CEILING_SPLIT_ORIGIN, name)
            for name in SplitOrigin.__dataclass_fields__
        }


def test_the_dependency_guard_protects_the_ceiling_trigger_too(tmp_path):
    """It lives in the shared acceptance, so the plan that answers a ceiling
    classification is refused for the same reason and by the same code family.
    Asserted directly on the helper — driving a ceiling request here would be
    re-testing `test_task_split.py`."""
    wiring = first_round(tmp_path)
    from autoloop.contract import TaskSpec

    parent = wiring.registry.get("t1")
    specs = (
        TaskSpec(id="t1-a", title="A", description="d", depends_on=("t1",)),
        TaskSpec(id="t1-b", title="B", description="d"),
    )
    problems = wiring.orch._successors_that_would_strand(parent, specs)
    assert len(problems) == 1
    assert "t1-a" in problems[0] and "t1" in problems[0]
    assert CEILING_SPLIT_ORIGIN.code_dependency_stranded.startswith("ceiling_split_")


def test_a_completed_split_leaves_no_marker_behind(tmp_path):
    """All three stores agree, so the crash marker has nothing left to say."""
    wiring = first_round(tmp_path)
    dispatch(wiring, split_block("t1"))
    assert not marker_path(wiring).exists()


def test_the_marker_records_the_reviewers_own_label(tmp_path):
    """The crash boundary, at the one point this verb touches it: a recovery
    re-files the surviving halves under the label the ATTEMPT used, so a
    reviewer split interrupted mid-way is still findable as one afterwards."""
    wiring = first_round(tmp_path)
    seen: list[str] = []
    real_save = wiring.orch._split_intents.save

    def capture(intent):
        seen.append(intent.reason)
        return real_save(intent)

    wiring.orch._split_intents.save = capture  # type: ignore[method-assign]
    dispatch(wiring, split_block("t1"))
    assert seen == [REVIEWER_SPLIT_RETIREMENT_REASON]


def test_a_marker_that_cannot_be_written_refuses_the_split(tmp_path):
    """Continuing without it is the fail-open reading: the split would be
    exactly as crash-unsafe as it was before the marker existed, with nothing
    saying so."""
    wiring = first_round(tmp_path)

    def boom(_intent):
        raise OSError("read-only state directory")

    wiring.orch._split_intents.save = boom  # type: ignore[method-assign]
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1"))
    assert denial_codes(wiring)[-1] == "reviewer_split_intent_unwritable"
    assert snapshot(wiring) == before
    assert not wiring.registry.has("t1-a")


def test_a_registry_refusal_leaves_nothing_behind(tmp_path):
    """`add_many` is atomic, so a plan the graph refuses is a denial and not a
    park — and the marker written a line earlier is dropped again."""
    wiring = first_round(tmp_path, tasks=[ready_task("t1"), ready_task("taken")])
    before = snapshot(wiring)
    dispatch(wiring, split_block("t1", specs=[successor("taken"), successor("t1-b")]))
    assert denial_codes(wiring)[-1] == "duplicate_task"
    assert snapshot(wiring) == before
    assert not marker_path(wiring).exists()


@pytest.mark.parametrize(
    "specs,code",
    [
        ([successor("t1-a")], "reviewer_split_too_small"),
        ([successor("t1"), successor("t1-b")], "reviewer_split_names_parent"),
        (
            [successor("t1-a", depends_on=["t1"]), successor("t1-b")],
            "reviewer_split_dependency_stranded",
        ),
    ],
)
def test_no_refusal_leaves_a_half_applied_split(tmp_path, specs, code):
    """Every refusal says "Nothing was changed", and this is what makes that
    true: no successor in the registry, no marker on disk, the parent's record
    and worker still live, and the loop still working."""
    wiring = first_round(tmp_path)
    dispatch(wiring, split_block("t1", specs=specs))
    assert denial_codes(wiring)[-1] == code
    assert registry_on_disk(wiring).get("t1").status != "retired"
    assert not marker_path(wiring).exists()
    assert (wiring.tmp_path / "executions" / "t1.json").exists()
    assert (wiring.tmp_path / "workers" / "t1").exists()
    assert wiring.orch.state.phase != Phase.NEEDS_USER.value


def test_a_split_with_no_stores_configured_is_refused(tmp_path):
    """Retiring one half of an execution is worse than retiring neither: the
    subtasks would exist while the parent's candidate stayed live."""
    wiring = first_round(tmp_path)
    wiring.orch._worker_repos = None
    dispatch(wiring, split_block("t1"))
    assert denial_codes(wiring)[-1] == "reviewer_split_unavailable"
    assert not wiring.registry.has("t1-a")


def test_the_measured_length_brackets_the_ceiling():
    """The OBSERVED reading behind split-03's move of the contract's per-turn
    length ceiling — 5,200, against a ceiling of 5,300.

    Every earlier move of that number was a HAND SUM, because those executors
    had no shell; each of their docstrings says so and asks to be replaced by a
    real reading. This round had impl-02's advisory channel, which reports WHICH
    test ids failed but no numbers — so the length was bracketed by a temporary
    parametrized probe asserting `len(...) > floor` for six floors. Floors 4,900,
    5,000 and 5,100 passed and 5,200, 5,300 and 5,400 failed, which puts the
    length in (5,100, 5,200]. The hand sum in
    `test_contract.test_contract_stays_within_its_budget` said 5,112, inside
    that bracket. The probe was deleted; this is what it left behind.

    Asserted as the UPPER bracket only, not as the two-sided band the probe
    measured. A lower bound would fail on a legitimate compression of the
    instructions, which is the one direction this whole budget exists to
    encourage. The anchor below is what stops it passing vacuously instead —
    it fails if the text this reading measured is not the text being measured.
    """
    assert len(CONTRACT_INSTRUCTIONS) <= 5200
    assert "  split — task_id cannot be delivered" in CONTRACT_INSTRUCTIONS


def test_the_minimum_successor_count_is_shared_with_the_ceiling_trigger():
    """Two names for one number is the drift this codebase writes docstrings
    against. Both triggers refuse below the same constant."""
    assert MIN_CEILING_SPLIT_TASKS >= 2
    assert MIN_CHILD_ATTEMPTS < MAX_TASK_ATTEMPTS
