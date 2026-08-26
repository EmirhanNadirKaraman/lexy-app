"""A task at its attempt ceiling asks the REVIEWER what to do, not a human.

ceil-01, 2026-08-25. Measured over 131 resolved blocker records on 2026-08-24,
`attempt_count_ceiling` was the single largest cause of operator-blocked time
(12 parks, 32.0h, median 1.34h — the highest median of the top five) precisely
because, unlike the mechanical parks, it needs a JUDGEMENT: is the spec wrong,
is the task too big, is it nearly done. The reviewer already holds the candidate
and its own verdict history, so the judgement is asked of it.

THE ONE CLAIM these tests grade: a task that reaches its attempt ceiling
requests a fresh PLAN from the reviewer against its CURRENT candidate, and that
reply decides what happens — a named remaining fix on an extended budget, or a
decomposition. It does not park for a human on the ceiling alone.

The four things that break it if left untested, one section each below:

  * the classify half must work ALONE (a converging task is extended and is not
    split — that is where the hours are);
  * a split must not REFUND the parent's spent attempts, or the ceiling stops
    meaning anything and unbounded churn returns through the back door;
  * splitting must be BOUNDED, or one looping task becomes an unbounded family
    of them;
  * the new plan must be REQUIRED to differ from the stored one, because the
    planner is the same reviewer that just refused the work five times — and the
    request itself shows it the stored plan, so handing it straight back is an
    echo rather than a classification;
  * accepting a split must be CRASH-CONSISTENT (split-04) — it moves three
    stores that share no commit point, so the last section grades what is left
    behind when the process does not survive doing it.

Real git and real worker repos throughout, with the `run_git` / executor / build
helpers duplicated per this suite's self-contained convention (the same shape
`test_recut.py` uses, and for the same reason: what these mechanisms claim is
about what ends up on disk).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Decomposition, parse_response
from autoloop.errors import (
    GitCommandError,
    StateCorruptError,
    StateError,
    TaskGraphError,
)
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    CEILING_EXTENSION_ATTEMPTS,
    CEILING_SPLIT_RETIREMENT_REASON,
    MAX_CEILING_EXTENSIONS,
    MAX_SPLIT_DEPTH,
    MAX_TASK_ATTEMPTS,
    MIN_CEILING_SPLIT_TASKS,
    MIN_CHILD_ATTEMPTS,
    SPLIT_ACCEPTANCE_UNRECONCILED,
    Orchestrator,
    release_task_to_pending,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import (
    IntentStore,
    SplitAcceptance,
    SplitIntent,
    TaskExecution,
    TaskExecutionStore,
    preserve_execution,
    reconcile_split_acceptance,
    split_intents_dir,
)

URL = "https://chatgpt.com/c/test-conversation"

#: Every task here may write both files, so a round before a classification and
#: a round after it can touch different paths without a scope refusal.
PATHS = ("docs/A.md", "docs/B.md")


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


#: The plan the first `implement` approves, and therefore the plan stored on the
#: task. A classification reply that renders to THIS text is the echo case.
FIRST_PLAN = {
    "approach": "one commit",
    "files": ["docs/A.md"],
    "steps": ["write the file"],
}

#: A materially different plan — the "one named remaining fix" answer.
NAMED_FIX_PLAN = {
    "approach": "one remaining fix: the concurrency hole in the reconciler",
    "files": ["docs/B.md"],
    "steps": ["close the reconciler hole", "pin it with a test"],
}


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


def revise_block(task_id="t1", feedback="fix it", decomposition=None):
    data = {
        "version": 3,
        "decision": "revise",
        "reason": "needs work",
        "task_id": task_id,
        "feedback": feedback,
    }
    if decomposition is not None:
        data["decomposition"] = decomposition
    return block(data)


def child_spec(tid, paths=("docs/A.md",)):
    return {
        "id": tid,
        "title": f"Subtask {tid}",
        "description": "one independently reviewable piece",
        "approved_paths": list(paths),
    }


def plan_block(specs, reason="the objections keep relocating"):
    return block(
        {"version": 3, "decision": "plan", "reason": reason, "tasks": list(specs)}
    )


def stop_block(reason="all done"):
    return block({"version": 3, "decision": "stop", "reason": reason})


def recut_block(task_id="t1", reason="the branch is contaminated"):
    return block(
        {
            "version": 3,
            "decision": "recut",
            "reason": reason,
            "task_id": task_id,
        }
    )


class FakeClient:
    """Minimal conversation double: scripted replies in order, remembers what
    was submitted."""

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
        from autoloop.browser.chatgpt import SubmitResult

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


def spend_to_ceiling(wiring, task_id="t1", attempts=None):
    """Put `task_id`'s execution record exactly at its attempt ceiling.

    The counter is set rather than earned: five real dispatches would prove
    nothing this file is about and would take five agent rounds to do it. What
    matters here is the STATE at the ceiling, which is what the record holds.
    """
    execution = wiring.execution_store.load(task_id)
    cap = attempts
    if cap is None:
        cap = wiring.orch._attempt_cap_for(wiring.registry.get(task_id))
    execution.attempt_count = cap
    wiring.execution_store.save(execution)
    return execution


def first_round(tmp_path, tasks=None, responses=None):
    """A wiring whose task `t1` has been implemented once: an execution record,
    a candidate, and the stored plan `FIRST_PLAN`."""
    wiring = build(
        tmp_path,
        responses=responses or [implement_block("t1")],
        tasks=tasks or [ready_task("t1")],
    )
    wiring.orch.run(max_steps=4)
    return wiring


def ask_at_ceiling(wiring, task_id="t1"):
    """Drive `task_id` to its ceiling and take the classification request."""
    spend_to_ceiling(wiring, task_id)
    dispatch(wiring, revise_block(task_id, feedback="another go"))
    return wiring.orch.state.outbox or ""


# ---------------------------------------------------------------------------
# the ceiling asks instead of parking
# ---------------------------------------------------------------------------


def test_a_task_at_the_ceiling_asks_the_reviewer_instead_of_parking(tmp_path):
    """THE claim, in its narrowest form. Before ceil-01 this dispatch parked
    `attempt_count_ceiling` and waited a median 1.34 hours for a person."""
    wiring = first_round(tmp_path)
    request = ask_at_ceiling(wiring)

    state = wiring.orch.state
    assert state.phase == Phase.READY.value, state.phase
    assert state.question is None
    assert "ATTEMPT CEILING REACHED" in request
    assert wiring.registry.get("t1").ceiling_plan_requested_at


def test_the_request_costs_no_attempt_and_does_not_raise_the_ceiling(tmp_path):
    """`MAX_TASK_ATTEMPTS` bounds unbounded local churn and the ask must not be
    a way around it: nothing is spent, and nothing is granted, until the
    reviewer answers."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    execution = wiring.execution_store.load("t1")
    assert execution.attempt_count == MAX_TASK_ATTEMPTS
    task = wiring.registry.get("t1")
    assert task.attempt_extensions == 0
    assert wiring.orch._attempt_cap_for(task) == MAX_TASK_ATTEMPTS


def test_the_request_carries_the_classification_evidence_and_no_diff(tmp_path):
    """The packet cap already bites on exactly the tasks that reach a ceiling —
    port-01's range diff was refused at 414KB — and the diff is not what
    classification needs anyway. What it needs is whether the objections are
    shrinking or relocating, which the verdict history answers."""
    wiring = first_round(tmp_path)
    request = ask_at_ceiling(wiring)

    assert "diff --git" not in request
    assert "@@" not in request
    # ...and it still carries what the judgement is made from.
    assert "attempt ledger" in request
    assert "the plan currently on record" in request
    assert "one commit" in request  # FIRST_PLAN, rendered
    assert "docs/A.md" in request  # the file the candidate touches
    assert "SHRINKING" in request and "RELOCATING" in request
    assert len(request) < 40_000, len(request)


def test_the_request_is_recorded_before_it_is_sent(tmp_path):
    """RECORD FIRST. The marker is the only thing that stops the next dispatch
    asking again, so a request that went out without one would be re-asked
    forever — and the ask spends neither the attempt budget nor the denial
    budget, so nothing else would ever stop it."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.get("t1").ceiling_plan_requested_at


# ---------------------------------------------------------------------------
# converging feedback: an extension, and NOT a split
# ---------------------------------------------------------------------------


def test_converging_feedback_extends_the_budget_and_does_not_split(tmp_path):
    """blk-01's shape: attempt 5, round 3, zero faults, a verdict endorsing
    eight things by name with ONE fix left and the remedy spelled out. Splitting
    that would shatter a task one attempt from done."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    dispatch(
        wiring,
        revise_block("t1", feedback="close the reconciler hole", decomposition=NAMED_FIX_PLAN),
    )

    task = wiring.registry.get("t1")
    assert task.attempt_extensions == 1
    assert task.ceiling_plan_requested_at == ""       # the request is answered
    assert (
        wiring.orch._attempt_cap_for(task)
        == MAX_TASK_ATTEMPTS + CEILING_EXTENSION_ATTEMPTS
    )
    # NOT split: no subtask exists, and the task is still the one being worked.
    assert sorted(t.id for t in wiring.registry.all_tasks()) == ["t1"]
    assert task.status == "in_progress"
    assert task.superseded_by == ()


def test_the_extended_round_actually_runs_rather_than_parking(tmp_path):
    """An extension that granted a number but never dispatched a round would
    pass every counter assertion and deliver nothing. The proof is the executor
    call and the attempt charged to it."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    calls_before = len(wiring.executor.calls)

    dispatch(
        wiring,
        revise_block("t1", feedback="close the reconciler hole", decomposition=NAMED_FIX_PLAN),
    )

    assert len(wiring.executor.calls) == calls_before + 1
    execution = wiring.execution_store.load("t1")
    assert execution.attempt_count == MAX_TASK_ATTEMPTS + 1
    assert wiring.orch.state.phase != Phase.NEEDS_USER.value


def test_the_new_plan_replaces_the_stored_one(tmp_path):
    """The extension is granted FOR a named remaining fix, so the fix has to
    become the plan the implementing agent is given — otherwise the round is
    dispatched against the plan that already failed five times."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    dispatch(
        wiring,
        revise_block("t1", feedback="close the reconciler hole", decomposition=NAMED_FIX_PLAN),
    )

    stored = wiring.registry.get("t1").decomposition
    assert "concurrency hole in the reconciler" in stored
    assert "write the file" not in stored


def test_an_ordinary_reshape_does_not_buy_an_extension(tmp_path):
    """The gate that keeps the classify half honest. `set_decomposition` stores
    any plan an implement/revise carries, so a grant sitting beside it ungated
    would mean every mid-task reshape silently widened the ceiling."""
    wiring = first_round(tmp_path)
    # No ceiling, no request — just a reviewer reshaping the plan mid-task.
    dispatch(
        wiring,
        revise_block("t1", feedback="do it differently", decomposition=NAMED_FIX_PLAN),
    )

    task = wiring.registry.get("t1")
    assert task.attempt_extensions == 0
    assert wiring.orch._attempt_cap_for(task) == MAX_TASK_ATTEMPTS


# ---------------------------------------------------------------------------
# relocating feedback: a decomposition
# ---------------------------------------------------------------------------


def test_relocating_feedback_decomposes_the_task(tmp_path):
    """exec-01's shape: three review rounds and seven consecutive revise
    verdicts, validation passing every round, each review finding a deeper hole
    in the same property. That task had become the too-big task this exists to
    split."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    parent = wiring.registry.get("t1")
    assert parent.status == "retired"
    assert parent.superseded_by == ("t1-a", "t1-b")
    assert parent.ceiling_plan_requested_at == ""
    assert {"t1-a", "t1-b"} <= {t.id for t in wiring.registry.all_tasks()}
    assert wiring.orch.state.phase == Phase.READY.value
    assert "DECOMPOSITION APPLIED" in (wiring.orch.state.outbox or "")


def test_the_parents_record_is_archived_and_its_worker_quarantined(tmp_path):
    """NOTHING IS DELETED, and both halves name each other on disk — the same
    guarantee a recut gives, through the same one call."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    assert wiring.execution_store.load("t1") is None
    assert not (wiring.tmp_path / "workers" / "t1").exists()
    archived = sorted((wiring.tmp_path / "executions" / "archive").glob("t1-*.json"))
    quarantined = sorted((wiring.tmp_path / "quarantine").glob("t1-*"))
    assert len(archived) == 1 and len(quarantined) == 1
    label = archived[0].name[len("t1-"): -len(".json")]
    assert quarantined[0].name == f"t1-{label}"
    assert label.startswith(CEILING_SPLIT_RETIREMENT_REASON)
    assert (quarantined[0] / ".git").exists()


def test_a_dependent_of_the_split_parent_is_repointed_at_the_children(tmp_path):
    """A retirement satisfies no dependency, so a dependent left naming the
    parent would wait forever. `retire(superseded_by=...)` re-points it, which
    is precisely why the children are added BEFORE the parent is retired."""
    wiring = first_round(
        tmp_path, tasks=[ready_task("t1"), ready_task("t2", depends_on=("t1",))]
    )
    ask_at_ceiling(wiring)
    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    assert set(wiring.registry.get("t2").depends_on) == {"t1-a", "t1-b"}
    assert "t1" not in wiring.registry.get("t2").depends_on


def test_an_in_progress_dependent_refuses_the_split_before_anything_moves(tmp_path):
    """`retire` refuses to rewrite the dependencies a running dispatch is being
    judged against — and by the time it raised, the children would already
    exist. So it is checked first, and the registry is untouched."""
    wiring = first_round(
        tmp_path, tasks=[ready_task("t1"), ready_task("t2", depends_on=("t1",))]
    )
    # Set directly: `mark_in_progress` refuses a task whose dependency is not
    # complete, and the state under test is one an operator's release or a
    # crash-recovered round can still produce.
    wiring.registry.get("t2").status = "in_progress"
    wiring.task_store.save(wiring.registry)
    ask_at_ceiling(wiring)

    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    assert "ceiling_split_dependent_in_progress" in denial_codes(wiring)
    assert sorted(t.id for t in wiring.registry.all_tasks()) == ["t1", "t2"]
    assert wiring.registry.get("t1").status == "in_progress"


def test_an_ordinary_plan_is_untouched_when_nothing_is_waiting(tmp_path):
    """Nearly every `plan` is roadmap work. The split path must be reachable
    only from a task that actually asked."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    dispatch(wiring, plan_block([child_spec("n1"), child_spec("n2")], reason="roadmap"))

    added = {t.id for t in wiring.registry.all_tasks()}
    assert {"t1", "n1", "n2"} == added
    assert wiring.registry.get("t1").status == "pending"
    assert wiring.registry.get("n1").split_depth == 0
    assert wiring.registry.get("n1").inherited_attempts == 0


# ---------------------------------------------------------------------------
# the split does NOT refund the parent's spent attempts
# ---------------------------------------------------------------------------


def test_a_split_does_not_refund_the_parents_spent_attempts(tmp_path):
    """If a split handed each subtask a fresh `MAX_TASK_ATTEMPTS`, the ceiling
    would stop meaning anything and unbounded churn would return through the
    back door — a looping task could buy budget by being split.

    BOTH properties are pinned, because satisfying only the first is easy and
    useless: the family collectively gets strictly less than a fresh budget
    each, AND every child still has usable attempts."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    children = [wiring.registry.get("t1-a"), wiring.registry.get("t1-b")]
    caps = [wiring.orch._attempt_cap_for(child) for child in children]

    # NOT REFUNDED: the parent's spend is carried, and no child holds a fresh
    # budget — collectively or individually.
    assert all(child.inherited_attempts == MAX_TASK_ATTEMPTS for child in children)
    assert sum(caps) < len(children) * MAX_TASK_ATTEMPTS
    assert all(cap < MAX_TASK_ATTEMPTS for cap in caps)
    # AND IT WORKS: every child can actually produce a candidate and answer a
    # review of it. A child born at its own ceiling would rebuild the park this
    # feature removes while passing the assertion above.
    assert all(cap >= MIN_CHILD_ATTEMPTS >= 1 for cap in caps)


def test_even_an_extended_child_never_reaches_a_full_fresh_budget(tmp_path):
    """The strongest form of the anti-refund claim, and the one that has to
    survive a later constant change: a subtask at its most generous — inherited
    debt, floored, plus its own single extension — still gets strictly less than
    a task that was planned from scratch."""
    assert MIN_CHILD_ATTEMPTS + CEILING_EXTENSION_ATTEMPTS < MAX_TASK_ATTEMPTS
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    child = Task(
        id="c1",
        title="child",
        description="d",
        approved_paths=PATHS,
        inherited_attempts=MAX_TASK_ATTEMPTS,
        split_depth=1,
        attempt_extensions=MAX_CEILING_EXTENSIONS,
    )
    assert wiring.orch._attempt_cap_for(child) < MAX_TASK_ATTEMPTS


def test_a_childs_extension_is_worth_real_attempts(tmp_path):
    """The floor is applied BEFORE the grant, deliberately. Adding first would
    have made the extension worth nothing for exactly the tasks with the least
    budget — a grant that reads as granted while behaving as if it were not."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    plain = Task(
        id="c1", title="c", description="d", inherited_attempts=MAX_TASK_ATTEMPTS
    )
    extended = Task(
        id="c2",
        title="c",
        description="d",
        inherited_attempts=MAX_TASK_ATTEMPTS,
        attempt_extensions=1,
    )
    assert wiring.orch._attempt_cap_for(plain) == MIN_CHILD_ATTEMPTS
    assert (
        wiring.orch._attempt_cap_for(extended)
        == MIN_CHILD_ATTEMPTS + CEILING_EXTENSION_ATTEMPTS
    )


def test_the_inherited_spend_survives_on_disk(tmp_path):
    """The debt has to outlive the process that recorded it: the parent's
    execution record is archived by the very operation that sets it."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    raw = json.loads(wiring.config.tasks_file.read_text(encoding="utf-8"))
    row = next(t for t in raw["tasks"] if t["id"] == "t1-a")
    assert row["inherited_attempts"] == MAX_TASK_ATTEMPTS
    assert row["split_depth"] == 1
    reloaded = TaskStore(wiring.config.tasks_file).load()
    assert reloaded.get("t1-a").inherited_attempts == MAX_TASK_ATTEMPTS


def test_the_split_report_states_the_budget_it_did_not_refund(tmp_path):
    """A report that showed only the new ids would read as "you got a fresh
    start", which is the one thing this must not imply."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    report = wiring.orch.state.outbox or ""
    assert "NOT REFUNDED" in report
    assert f"inherits the {MAX_TASK_ATTEMPTS} attempt" in report
    assert f"{MIN_CHILD_ATTEMPTS} of the usual {MAX_TASK_ATTEMPTS}" in report


# ---------------------------------------------------------------------------
# recursion is bounded
# ---------------------------------------------------------------------------


def child_at_ceiling(tmp_path):
    """A wiring holding a depth-1 subtask that has reached its own ceiling and
    asked for a classification."""
    wiring = first_round(
        tmp_path,
        tasks=[
            ready_task(
                "c1", inherited_attempts=MAX_TASK_ATTEMPTS, split_depth=MAX_SPLIT_DEPTH
            )
        ],
        responses=[implement_block("c1")],
    )
    request = ask_at_ceiling(wiring, "c1")
    return wiring, request


def test_a_subtask_at_the_depth_bound_is_refused_a_further_split(tmp_path):
    """Splitting without a bound is how one looping task becomes an unbounded
    family of them."""
    wiring, _ = child_at_ceiling(tmp_path)

    dispatch(wiring, plan_block([child_spec("c1-a"), child_spec("c1-b")]))

    assert "ceiling_split_depth" in denial_codes(wiring)
    assert sorted(t.id for t in wiring.registry.all_tasks()) == ["c1"]
    assert wiring.registry.get("c1").status == "in_progress"


def test_the_request_tells_a_subtask_which_answers_are_unavailable(tmp_path):
    """A remedy the reviewer cannot use must not be offered: it would spend a
    round producing a directive that is then refused."""
    _, request = child_at_ceiling(tmp_path)

    assert "Not available for this task" in request
    assert "split depth" in request
    assert "A DECOMPOSITION" not in request
    assert "A NAMED REMAINING FIX" in request  # the child may still be extended


def test_a_subtask_can_still_be_extended_at_its_own_ceiling(tmp_path):
    """The depth bound must not strand the children it creates. A child that
    could never be extended either would simply reintroduce the park."""
    wiring, _ = child_at_ceiling(tmp_path)

    dispatch(
        wiring, revise_block("c1", feedback="one fix left", decomposition=NAMED_FIX_PLAN)
    )

    task = wiring.registry.get("c1")
    assert task.attempt_extensions == 1
    assert (
        wiring.orch._attempt_cap_for(task)
        == MIN_CHILD_ATTEMPTS + CEILING_EXTENSION_ATTEMPTS
    )
    assert wiring.orch.state.phase != Phase.NEEDS_USER.value


# ---------------------------------------------------------------------------
# an identical plan is refused, not accepted
# ---------------------------------------------------------------------------


def test_a_plan_identical_to_the_stored_one_is_refused(tmp_path):
    """THE ECHO CASE. The planner is the same reviewer that just refused this
    work five times, and the request SHOWS it the stored plan so that the new
    one can differ from it — which is exactly why handing it straight back must
    not be read as a classification."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    dispatch(wiring, revise_block("t1", feedback="same again", decomposition=FIRST_PLAN))

    state = wiring.orch.state
    assert state.phase == Phase.NEEDS_USER.value
    assert state.park_kind == "task_fatal"
    assert wiring.registry.get("t1").attempt_extensions == 0


def test_an_identical_plan_differing_only_in_whitespace_and_case_is_refused(tmp_path):
    """Normalised exactly as the repeated-feedback bound normalises: whitespace
    and case, and nothing fuzzier. Two genuinely different plans must always be
    allowed through."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    echoed = {
        "approach": "  ONE   COMMIT ",
        "files": ["docs/A.md"],
        "steps": ["Write   The File"],
    }

    dispatch(wiring, revise_block("t1", feedback="same again", decomposition=echoed))

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert wiring.registry.get("t1").attempt_extensions == 0


def test_the_stored_plan_is_shown_so_the_new_one_can_differ_from_it(tmp_path):
    """The requirement only makes sense if the reviewer can see what it must
    differ FROM. Pinned because the two halves are easy to drift apart."""
    wiring = first_round(tmp_path)
    request = ask_at_ceiling(wiring)

    stored = wiring.registry.get("t1").decomposition
    assert stored and stored.strip() in request
    assert "MUST DIFFER" in request


# ---------------------------------------------------------------------------
# nothing here removes the bound on genuine churn
# ---------------------------------------------------------------------------


def test_the_extension_is_granted_at_most_once(tmp_path):
    """A second grant would be the reviewer arguing with its own evidence: a
    task that spends a second budget without landing has falsified the claim an
    extension makes."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    dispatch(
        wiring, revise_block("t1", feedback="one fix left", decomposition=NAMED_FIX_PLAN)
    )
    assert wiring.registry.get("t1").attempt_extensions == MAX_CEILING_EXTENSIONS

    # Spend the extended budget too, and ask again.
    ask_at_ceiling(wiring)
    second_plan = {
        "approach": "yet another angle",
        "files": ["docs/A.md"],
        "steps": ["try again"],
    }
    dispatch(wiring, revise_block("t1", feedback="more", decomposition=second_plan))

    assert "ceiling_extension_spent" in denial_codes(wiring)
    assert wiring.registry.get("t1").attempt_extensions == MAX_CEILING_EXTENSIONS
    execution = wiring.execution_store.load("t1")
    assert execution.attempt_count == MAX_TASK_ATTEMPTS + CEILING_EXTENSION_ATTEMPTS


def test_a_task_with_no_remedy_left_parks_on_the_original_code(tmp_path):
    """The park is not removed — it is moved to the END of the sequence, under
    the same `attempt_count_ceiling` code an operator's tooling already keys
    off. A subtask that has already been extended has neither remedy left."""
    wiring = first_round(
        tmp_path,
        tasks=[
            ready_task(
                "c1",
                inherited_attempts=MAX_TASK_ATTEMPTS,
                split_depth=MAX_SPLIT_DEPTH,
                attempt_extensions=MAX_CEILING_EXTENSIONS,
            )
        ],
        responses=[implement_block("c1")],
    )
    spend_to_ceiling(wiring, "c1")
    dispatch(wiring, revise_block("c1", feedback="again"))

    state = wiring.orch.state
    assert state.phase == Phase.NEEDS_USER.value
    assert state.park_kind == "task_fatal"
    assert "specification problem" in (state.question or "")
    assert [r["code"] for r in records(wiring, "needs_user")] == ["attempt_count_ceiling"]
    # It never asked, so nothing is left waiting for an answer either.
    assert wiring.registry.get("c1").ceiling_plan_requested_at == ""


def test_the_loop_asks_once_and_then_parks(tmp_path):
    """THE PING-PONG BOUND. The ceiling check spends neither the attempt budget
    nor the denial budget, so a loop that merely re-asked would re-ask forever
    with every automated signal green — the exact shape the repeated-stop
    livelock had to be bounded against."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    assert wiring.orch.state.phase == Phase.READY.value

    # A reply that does not classify: the same revise, with no plan.
    dispatch(wiring, revise_block("t1", feedback="just try again"))

    state = wiring.orch.state
    assert state.phase == Phase.NEEDS_USER.value
    assert state.park_kind == "task_fatal"
    assert "does not ask twice" in (state.question or "")
    # And no round was dispatched against the exhausted budget.
    assert wiring.execution_store.load("t1").attempt_count == MAX_TASK_ATTEMPTS


def test_the_total_dispatches_for_one_task_stay_bounded(tmp_path):
    """`MAX_TASK_ATTEMPTS` still bounds churn: every route out of a ceiling is
    bounded by a constant, so a task that is genuinely looping reaches a hard
    wall rather than never."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    task = wiring.registry.get("t1")
    task.attempt_extensions = MAX_CEILING_EXTENSIONS
    assert (
        wiring.orch._attempt_cap_for(task)
        == MAX_TASK_ATTEMPTS + MAX_CEILING_EXTENSIONS * CEILING_EXTENSION_ATTEMPTS
    )
    # ...and that total is what a task can reach, not a step toward more.
    task.attempt_extensions = 99
    assert (
        wiring.orch._attempt_cap_for(task)
        == MAX_TASK_ATTEMPTS + MAX_CEILING_EXTENSIONS * CEILING_EXTENSION_ATTEMPTS
    )


# ---------------------------------------------------------------------------
# the answers that are not answers
# ---------------------------------------------------------------------------


def test_policy_refuses_a_planless_revise_on_a_waiting_task(tmp_path):
    """The ordinary flow never reaches the unanswered park: a `revise` with no
    decomposition re-uses the stored plan, which is the one reply that cannot
    classify anything, so policy corrects it with the rule and the denial budget
    bounds the exchange."""
    registry = TaskRegistry(
        [Task(id="t1", title="T", description="d", approved_paths=PATHS)]
    )
    registry.set_decomposition("t1", "the stored plan")
    registry.request_ceiling_plan("t1", "2026-08-25T00:00:00+00:00")
    engine = PolicyEngine(PolicyConfig(implement_enabled=True))

    verdict = engine.authorize_directive(
        parse_response(revise_block("t1")), "main", registry
    )

    assert not verdict.allowed
    assert verdict.code == "ceiling_plan_required"
    assert "no attempt was spent" in verdict.reason


def test_policy_admits_the_same_revise_once_it_carries_a_plan(tmp_path):
    """The correction has to be answerable on the same round, or it is a wall
    rather than a redirect."""
    registry = TaskRegistry(
        [Task(id="t1", title="T", description="d", approved_paths=PATHS)]
    )
    registry.set_decomposition("t1", "the stored plan")
    registry.request_ceiling_plan("t1", "2026-08-25T00:00:00+00:00")
    engine = PolicyEngine(PolicyConfig(implement_enabled=True))

    verdict = engine.authorize_directive(
        parse_response(revise_block("t1", decomposition=NAMED_FIX_PLAN)),
        "main",
        registry,
    )

    assert verdict.allowed


def test_a_task_not_at_its_ceiling_still_reuses_its_stored_plan(tmp_path):
    """The reuse rule is unchanged for every task that is not waiting, which is
    all of them nearly all of the time."""
    registry = TaskRegistry(
        [Task(id="t1", title="T", description="d", approved_paths=PATHS)]
    )
    registry.set_decomposition("t1", "the stored plan")
    engine = PolicyEngine(PolicyConfig(implement_enabled=True))

    verdict = engine.authorize_directive(
        parse_response(revise_block("t1")), "main", registry
    )

    assert verdict.allowed


def test_a_one_subtask_plan_is_refused(tmp_path):
    """One child inherits the parent's spend and then hands the SAME unit of
    work a fresh floor of attempts under a new id — a rename that buys budget."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    dispatch(wiring, plan_block([child_spec("t1-a")]))

    assert "ceiling_split_too_small" in denial_codes(wiring)
    assert wiring.registry.get("t1").status == "in_progress"
    assert "t1-a" not in {t.id for t in wiring.registry.all_tasks()}
    assert MIN_CEILING_SPLIT_TASKS == 2


def test_a_plan_naming_the_parent_as_its_own_subtask_is_refused(tmp_path):
    """The parent is retired into its children, so a child under the same id
    cannot be created and the split would be refused halfway through."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    dispatch(wiring, plan_block([child_spec("t1"), child_spec("t1-b")]))

    assert "ceiling_split_names_parent" in denial_codes(wiring)
    assert wiring.registry.get("t1").status == "in_progress"
    assert "t1-b" not in {t.id for t in wiring.registry.all_tasks()}


def test_a_published_parent_is_never_split(tmp_path):
    """Published work is never retired by this loop. If it is wrong that is a
    new task, and a decomposition of it would archive the record of work that is
    already on a remote."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    execution = wiring.execution_store.load("t1")
    execution.published_sha = "b" * 40
    wiring.execution_store.save(execution)

    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    assert "ceiling_split_candidate_published" in denial_codes(wiring)
    assert wiring.registry.get("t1").status == "in_progress"
    assert "t1-a" not in {t.id for t in wiring.registry.all_tasks()}
    assert wiring.execution_store.load("t1") is not None


def test_a_parent_whose_record_is_gone_is_refused_rather_than_split(tmp_path):
    """The spend the children must inherit comes from that record. With no
    record there is no spend to carry, and a split that proceeded anyway would
    hand every child a fresh budget — the refund this must never make."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    wiring.orch._execution_store.load = lambda task_id: None

    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    assert "ceiling_split_no_execution" in denial_codes(wiring)
    assert "t1-a" not in {t.id for t in wiring.registry.all_tasks()}
    assert wiring.registry.get("t1").status == "in_progress"


def test_an_unreadable_parent_record_is_refused_rather_than_guessed_at(tmp_path):
    """Unreadable is NOT absent, and the two must not collapse into one answer:
    a record this cannot parse may name a published candidate, and reading it as
    "nothing spent" is the fail-open that both retires published work and
    refunds a budget in the same step."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    def unreadable(task_id):
        raise StateError("the execution record is not JSON")

    wiring.orch._execution_store.load = unreadable
    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    assert "ceiling_split_record_unreadable" in denial_codes(wiring)
    assert "t1-a" not in {t.id for t in wiring.registry.all_tasks()}
    assert wiring.registry.get("t1").status == "in_progress"


def test_a_rejected_plan_leaves_the_registry_exactly_as_it_was(tmp_path):
    """`add_many` is atomic, and the refusal has to be too: a duplicate id in
    the batch must not leave half a decomposition applied."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-a")]))

    assert wiring.registry.get("t1").status == "in_progress"
    assert wiring.registry.get("t1").ceiling_plan_requested_at
    assert "t1-a" not in {t.id for t in wiring.registry.all_tasks()}


# ---------------------------------------------------------------------------
# the states that must NOT reach the reviewer at all
# ---------------------------------------------------------------------------


def synthetic_execution(wiring, task_id):
    return TaskExecution(
        task_id=task_id,
        task_branch=f"autoloop/{task_id}",
        worktree_path=str(wiring.tmp_path / "workers" / task_id),
        task_base_sha=wiring.git.head_sha(),
        attempt_count=MAX_TASK_ATTEMPTS,
        attempt_ledger=("1|task|validation_failed",),
    )


def test_an_audit_unit_at_the_ceiling_still_parks(tmp_path):
    """An audit is synthetic, minted per iteration and never planned: there is
    no registry row to record a classification against and nothing to decompose
    it into. It parks exactly as it always has."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    unit = Task(id="audit-0001", title="audit", description="d")
    execution = synthetic_execution(wiring, "audit-0001")

    wiring.orch._handle_attempt_ceiling(
        parse_response(revise_block("t1")),
        unit,
        execution,
        wiring.git,
        wiring.orch.state,
        MAX_TASK_ATTEMPTS,
        is_audit=True,
    )

    state = wiring.orch.state
    assert state.phase == Phase.NEEDS_USER.value
    assert "not a roadmap task" in (state.question or "")
    assert [r["code"] for r in records(wiring, "needs_user")] == ["attempt_count_ceiling"]


def test_an_id_the_registry_does_not_hold_parks_rather_than_asking(tmp_path):
    """Fail-closed: a classification cannot be recorded against a task that is
    not there, and asking a question the loop cannot remember asking is the
    ping-pong this parks instead of starting."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    ghost = Task(id="gone-01", title="gone", description="d")

    wiring.orch._handle_attempt_ceiling(
        parse_response(revise_block("t1")),
        ghost,
        synthetic_execution(wiring, "gone-01"),
        wiring.git,
        wiring.orch.state,
        MAX_TASK_ATTEMPTS,
        is_audit=False,
    )

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert "not in the task registry" in (wiring.orch.state.question or "")


def test_a_registry_write_that_fails_parks_instead_of_asking(tmp_path):
    """RECORD FIRST, and if the record cannot be written, do not ask: an
    unrecorded request would be re-asked on every dispatch, forever."""
    wiring = first_round(tmp_path)
    spend_to_ceiling(wiring, "t1")

    def refuse(*_args, **_kwargs):
        raise TaskGraphError("write_refused", "the registry could not be written")

    # Patched on the registry rather than the store: `_dispatch_executor` saves
    # the registry earlier in this same dispatch, so failing the store would
    # fail a write that has nothing to do with the ceiling.
    wiring.orch._registry.request_ceiling_plan = refuse
    dispatch(wiring, revise_block("t1", feedback="another go"))

    state = wiring.orch.state
    assert state.phase == Phase.NEEDS_USER.value
    assert "could not record" in (state.question or "")
    assert [r["code"] for r in records(wiring, "needs_user")] == ["attempt_count_ceiling"]


def test_a_candidate_that_was_never_committed_still_produces_a_request(tmp_path):
    """A task whose every round failed validation has no candidate at all. The
    request must still go out, saying so, rather than failing on an empty sha —
    that task is exactly the one whose ceiling needs classifying."""
    wiring = first_round(tmp_path)
    execution = wiring.execution_store.load("t1")
    execution.attempt_count = MAX_TASK_ATTEMPTS
    execution.candidate_sha = ""
    wiring.execution_store.save(execution)

    dispatch(wiring, revise_block("t1", feedback="another go"))

    request = wiring.orch.state.outbox or ""
    assert "ATTEMPT CEILING REACHED" in request
    assert "no candidate has been committed" in request
    assert wiring.orch.state.phase == Phase.READY.value


# ---------------------------------------------------------------------------
# the registry's own three writes
# ---------------------------------------------------------------------------


def test_a_second_request_keeps_the_original_timestamp():
    """The field answers "has this task already asked?", so refreshing it would
    erase the fact that bounds the whole mechanism."""
    registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    registry.request_ceiling_plan("t1", "2026-08-25T01:00:00+00:00")
    registry.request_ceiling_plan("t1", "2026-08-25T09:00:00+00:00")
    assert registry.get("t1").ceiling_plan_requested_at == "2026-08-25T01:00:00+00:00"


def test_an_extension_cannot_be_granted_to_a_task_that_never_asked():
    """The registry-level half of the gate `_ceiling_reply_ok` enforces: a
    reshape is not a request, and a grant with no request behind it is the
    silent widening this must not allow."""
    registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    with pytest.raises(TaskGraphError) as exc:
        registry.grant_attempt_extension("t1")
    assert exc.value.code == "ceiling_plan_not_requested"
    assert registry.get("t1").attempt_extensions == 0


def test_a_grant_clears_the_request_in_the_same_call():
    """Both halves or neither: a grant that left the request standing would park
    the task as unanswered on its very next dispatch."""
    registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    registry.request_ceiling_plan("t1", "2026-08-25T01:00:00+00:00")
    registry.grant_attempt_extension("t1")
    assert registry.get("t1").attempt_extensions == 1
    assert registry.get("t1").ceiling_plan_requested_at == ""


def test_terminal_tasks_take_none_of_the_three_writes():
    """Rewriting the budget of finished work edits history rather than steering
    the queue — the same rule `set_decomposition` applies."""
    registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    registry.mark_in_progress("t1")
    registry.mark_completed("t1")
    for call in (
        lambda: registry.request_ceiling_plan("t1", "2026-08-25T01:00:00+00:00"),
        lambda: registry.grant_attempt_extension("t1"),
        lambda: registry.clear_ceiling_plan_request("t1"),
    ):
        with pytest.raises(TaskGraphError) as exc:
            call()
        assert exc.value.code == "task_completed"


def test_a_retired_row_is_never_offered_as_a_pending_parent():
    """A retired parent whose marker survived would be picked up as the parent
    of an unrelated later plan — a split applied to the wrong task."""
    registry = TaskRegistry(
        [Task(id="t1", title="T", description="d"), Task(id="t2", title="T", description="d")]
    )
    registry.request_ceiling_plan("t1", "2026-08-25T01:00:00+00:00")
    assert [t.id for t in registry.ceiling_plan_pending()] == ["t1"]
    registry.retire("t1", superseded_by=("t2",))
    assert registry.ceiling_plan_pending() == []


def test_the_budget_counters_are_validated_at_load(tmp_path):
    """These fields are inputs to BOUNDS. Reading a value the loader cannot
    trust as 0 hands back allowance the loop never granted, which is the guard
    switching itself off rather than reporting."""
    good = {
        "schema_version": 1,
        "tasks": [
            {
                "id": "t1",
                "title": "T",
                "description": "d",
                "attempt_extensions": 1,
                "inherited_attempts": 5,
                "split_depth": 1,
            }
        ],
    }
    registry = TaskRegistry.from_dict(good)
    assert registry.get("t1").attempt_extensions == 1
    assert registry.get("t1").inherited_attempts == 5

    for field, value in (
        ("attempt_extensions", "1"),
        ("attempt_extensions", True),
        ("inherited_attempts", -1),
        ("split_depth", 1.5),
    ):
        bad = {
            "schema_version": 1,
            "tasks": [{"id": "t1", "title": "T", "description": "d", field: value}],
        }
        with pytest.raises(StateCorruptError):
            TaskRegistry.from_dict(bad)


def test_a_row_written_before_these_fields_existed_loads_unchanged():
    """Every `tasks.json` on disk today. A missing key genuinely means no
    extension, no inherited spend and no split ancestry."""
    registry = TaskRegistry.from_dict(
        {"schema_version": 1, "tasks": [{"id": "t1", "title": "T", "description": "d"}]}
    )
    task = registry.get("t1")
    assert task.attempt_extensions == 0
    assert task.inherited_attempts == 0
    assert task.split_depth == 0
    assert task.ceiling_plan_requested_at == ""


# ---------------------------------------------------------------------------
# nothing about an ordinary task changes
# ---------------------------------------------------------------------------


def test_an_ordinary_task_has_exactly_the_budget_it_always_had(tmp_path):
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    assert wiring.orch._attempt_cap_for(wiring.registry.get("t1")) == MAX_TASK_ATTEMPTS


def test_a_hand_edited_counter_cannot_widen_the_ceiling_in_memory(tmp_path):
    """`_persisted_nonneg_int` refuses an unreadable stored value at LOAD, but a
    `Task` handed in by an embedder passes no such gate — and a `bool` is an
    `int` in Python, so `True` would compute a budget rather than raise."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])
    task = wiring.registry.get("t1")
    task.attempt_extensions = True
    assert wiring.orch._attempt_cap_for(task) == MAX_TASK_ATTEMPTS
    task.attempt_extensions = -5
    assert wiring.orch._attempt_cap_for(task) == MAX_TASK_ATTEMPTS
    task.inherited_attempts = "lots"
    assert wiring.orch._attempt_cap_for(task) == MAX_TASK_ATTEMPTS


def test_the_decomposition_the_contract_parses_is_what_is_compared(tmp_path):
    """The echo check compares the RENDERED plan against the stored text, which
    is the same rendering `set_decomposition` writes — so the two cannot drift
    into comparing different things."""
    plan = Decomposition(
        approach=FIRST_PLAN["approach"],
        files=tuple(FIRST_PLAN["files"]),
        steps=tuple(FIRST_PLAN["steps"]),
    )
    wiring = first_round(tmp_path)
    assert plan.render() == wiring.registry.get("t1").decomposition


def test_the_split_and_the_extension_are_the_only_two_answers(tmp_path):
    """A `stop` at the ceiling is still a `stop`: the reviewer saying a human
    must decide is a legitimate verdict and is not intercepted."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    dispatch(wiring, stop_block("this needs a person"))

    assert wiring.orch.state.phase == Phase.STOPPED.value
    assert wiring.orch.state.stop_kind == "contract"
    # The request stays on record: the task has asked, so a later dispatch
    # parks rather than asking a second time.
    assert wiring.registry.get("t1").ceiling_plan_requested_at


def test_a_recut_clears_a_pending_ceiling_request(tmp_path):
    """A recut ARCHIVES the execution record, so the next dispatch starts from
    attempt 0 and the ceiling this task asked about no longer exists. A stale
    marker would meet the fresh cut's first `implement`: an identical plan would
    park it, and a differing one would spend its single extension on a budget
    nothing had spent."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    assert wiring.registry.get("t1").ceiling_plan_requested_at

    dispatch(wiring, recut_block("t1"))

    task = wiring.registry.get("t1")
    assert task.status == "pending"
    assert task.ceiling_plan_requested_at == ""
    reloaded = TaskStore(wiring.config.tasks_file).load().get("t1")
    assert reloaded.ceiling_plan_requested_at == ""
    assert wiring.orch._attempt_cap_for(task) == MAX_TASK_ATTEMPTS


# ---------------------------------------------------------------------------
# an operator ending the round ends the pending request with it
# ---------------------------------------------------------------------------
#
# The ask leaves the task `in_progress` with the loop parked on the reply, which
# is exactly the shape `release` and `shelve` exist for. Both return the task to
# the queue, so both end the round the classification request was asked ABOUT —
# and a marker that outlived the round would meet the next ordinary directive:
# `policy._check_decomposition` would demand a classification nobody asked for,
# and the plan that answered it would spend the task's single extension on a
# budget that was never at its ceiling. Cleared in `tasks._return_to_pending`,
# once, for all three verbs that share it.


def operator_release(wiring, task_id="t1"):
    """`python -m autoloop release`, through the one function `cli._cmd_release`
    calls: the status move, the archived record and the quarantined worker."""
    return release_task_to_pending(
        task_id,
        wiring.registry,
        wiring.execution_store,
        wiring.worker_repos,
        persist=lambda: wiring.task_store.save(wiring.registry),
    )


def operator_shelve(wiring, task_id="t1"):
    """`python -m autoloop shelve`: the same status move, with the round and
    both of its budgets deliberately left exactly where they are."""
    task = wiring.registry.shelve(task_id)
    preserved = preserve_execution(task_id, wiring.execution_store, wiring.worker_repos)
    wiring.task_store.save(wiring.registry)
    return task, preserved


def authorize(wiring, response_text):
    """The policy verdict `_step_executing` reaches BEFORE anything is spent —
    the gate that refuses a plan-less directive on a task still waiting to be
    classified."""
    return wiring.orch._policy.authorize_directive(
        parse_response(response_text), "main", wiring.registry
    )


def test_an_operator_release_clears_a_pending_ceiling_request(tmp_path):
    """`release` ARCHIVES the execution record, so the next dispatch starts from
    attempt 0 and the ceiling this task asked about no longer exists — the same
    reasoning `recut` already carried, reached by the operator's own verb."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    assert wiring.registry.get("t1").ceiling_plan_requested_at

    operator_release(wiring)

    task = wiring.registry.get("t1")
    assert task.status == "pending"
    assert task.ceiling_plan_requested_at == ""
    reloaded = TaskStore(wiring.config.tasks_file).load().get("t1")
    assert reloaded.ceiling_plan_requested_at == ""
    assert wiring.execution_store.load("t1") is None


def test_after_a_release_the_next_ordinary_directive_needs_no_classification(tmp_path):
    """The denial is real while the request stands and gone once the round it
    was asked about has been released. Without the clear, every subsequent
    directive for this task would be refused `ceiling_plan_required` until a
    human edited `tasks.json`."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    # Plan-LESS, which is the only shape the gate reads: a directive carrying a
    # plan is authorized by the branch above it, answered or not.
    waiting = authorize(wiring, revise_block("t1"))
    assert not waiting.allowed and waiting.code == "ceiling_plan_required"

    operator_release(wiring)

    assert authorize(wiring, revise_block("t1")).allowed
    assert authorize(wiring, implement_block("t1")).allowed


def test_after_a_release_a_differing_plan_does_not_buy_an_extension(tmp_path):
    """The half a plan-less directive cannot prove: `_ceiling_reply_ok` returns
    early on a directive carrying no plan, so only one that DOES carry a plan
    shows the grant is gone. This is the exact directive that would have bought
    an extension one dispatch earlier."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    operator_release(wiring)
    calls_before = len(wiring.executor.calls)

    dispatch(wiring, implement_block("t1", decomposition=NAMED_FIX_PLAN))

    task = wiring.registry.get("t1")
    assert task.attempt_extensions == 0
    assert task.ceiling_plan_requested_at == ""
    assert wiring.orch._attempt_cap_for(task) == MAX_TASK_ATTEMPTS
    # The round really ran on the ordinary budget rather than parking: a fresh
    # record, charged its first attempt, with no extension behind it.
    assert len(wiring.executor.calls) == calls_before + 1
    assert wiring.execution_store.load("t1").attempt_count == 1
    assert wiring.orch.state.phase != Phase.NEEDS_USER.value
    assert not [code for code in denial_codes(wiring) if code.startswith("ceiling")]


def test_an_operator_shelve_clears_the_request_without_refunding_anything(tmp_path):
    """`shelve` KEEPS the round, so the task is still at its ceiling afterwards.
    Clearing the marker there buys no budget — it only means the next dispatch
    asks afresh against the same candidate instead of parking
    `ceiling_plan_unanswered` on a question the operator interrupted."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    task, preserved = operator_shelve(wiring)

    assert task.status == "pending"
    assert task.ceiling_plan_requested_at == ""
    reloaded = TaskStore(wiring.config.tasks_file).load().get("t1")
    assert reloaded.ceiling_plan_requested_at == ""
    # Nothing was refunded: the record, the candidate and the spent attempts
    # are all exactly where the ceiling found them.
    assert preserved.attempt_count == MAX_TASK_ATTEMPTS
    assert wiring.execution_store.load("t1").attempt_count == MAX_TASK_ATTEMPTS
    assert task.attempt_extensions == 0
    assert wiring.orch._attempt_cap_for(task) == MAX_TASK_ATTEMPTS


def test_after_a_shelve_the_ceiling_asks_again_rather_than_granting(tmp_path):
    """The shelved round resumes AT its ceiling, so the next directive — even
    one carrying a materially different plan — buys nothing: the dispatch runs
    the ceiling check again and asks. One ask per round is the bound; the
    operator's interruption is what starts a new one."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    operator_shelve(wiring)
    calls_before = len(wiring.executor.calls)

    dispatch(wiring, revise_block("t1", feedback="one fix left", decomposition=NAMED_FIX_PLAN))

    task = wiring.registry.get("t1")
    assert task.attempt_extensions == 0
    assert wiring.orch._attempt_cap_for(task) == MAX_TASK_ATTEMPTS
    assert wiring.execution_store.load("t1").attempt_count == MAX_TASK_ATTEMPTS
    assert len(wiring.executor.calls) == calls_before  # no round was dispatched
    # Asked afresh rather than parked for a human on the ceiling alone.
    assert "ATTEMPT CEILING REACHED" in (wiring.orch.state.outbox or "")
    assert wiring.orch.state.phase == Phase.READY.value
    assert task.ceiling_plan_requested_at


def test_unblocking_a_ceiling_park_deliberately_leaves_the_request_standing(tmp_path):
    """The counter-pin, and the reason the clear lives in `_return_to_pending`
    rather than on every route to `pending`. `unblock` ends no round — it
    answers a quarantine — and `_park_ceiling_plan_unanswered` tells the
    operator in as many words that answering it does not re-ask the reviewer.
    Clearing here would make that sentence a lie."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    wiring.registry.block("t1", "attempt_count_ceiling")

    wiring.registry.unblock("t1")

    task = wiring.registry.get("t1")
    assert task.status == "pending"
    assert task.ceiling_plan_requested_at


def test_a_refused_return_to_pending_leaves_the_request_where_it_found_it():
    """The clear sits AFTER the status move, and that move RAISES rather than
    returning: a verb that refused ended no round, so the question the reviewer
    has not answered is still on record. Clearing before the refusal would
    un-ask it on a task nothing moved."""
    registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    registry.request_ceiling_plan("t1", "2026-08-25T01:00:00+00:00")
    for verb in (registry.release, registry.shelve, registry.recut):
        with pytest.raises(TaskGraphError) as exc:
            verb("t1")
        assert exc.value.code == "task_not_in_progress"
    assert registry.get("t1").ceiling_plan_requested_at == "2026-08-25T01:00:00+00:00"


def test_the_transition_is_in_the_transcript(tmp_path):
    """A budget that changed with nothing saying so is the "task that silently
    restarted" problem in its other form."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    dispatch(
        wiring, revise_block("t1", feedback="one fix left", decomposition=NAMED_FIX_PLAN)
    )

    asked = records(wiring, "attempt_ceiling_plan_requested")
    granted = records(wiring, "attempt_ceiling_extended")
    assert len(asked) == 1 and asked[0]["task_id"] == "t1"
    assert asked[0]["cap"] == MAX_TASK_ATTEMPTS and asked[0]["may_extend"] is True
    assert len(granted) == 1
    assert granted[0]["granted_attempts"] == CEILING_EXTENSION_ATTEMPTS
    assert granted[0]["attempt_cap"] == MAX_TASK_ATTEMPTS + CEILING_EXTENSION_ATTEMPTS


def test_the_split_transition_is_in_the_transcript(tmp_path):
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    events = records(wiring, "task_ceiling_split")
    assert len(events) == 1
    event = events[0]
    assert event["task_id"] == "t1"
    assert event["children"] == ["t1-a", "t1-b"]
    assert event["inherited_attempts"] == MAX_TASK_ATTEMPTS
    assert event["child_split_depth"] == 1
    assert event["child_attempt_cap"] == MIN_CHILD_ATTEMPTS
    assert event["artifacts_retired"] is True


def test_the_decision_vocabulary_is_pinned(tmp_path):
    """ceil-01 added NO verb — its trigger and its budget rule were what was
    missing, and both of its answers ride on decisions the protocol already had.
    That is still true of the ceiling flow, and this test is what keeps it true:
    the pin is here so a verb can only join the vocabulary deliberately.

    split-03 joined it deliberately, and `split` is the ONE addition since. It
    is a different claim from the ceiling's — a task can be correct and still
    undeliverable in one piece — and it is exercised in
    `test_split_decision.py`. Renamed from `..._is_unchanged`, because a test
    asserting a changed set under that name is a lie about what it pins."""
    assert {d.value for d in Decision} == {
        "audit",
        "plan",
        "implement",
        "revise",
        "commit",
        "push",
        "commit_and_push",
        "recut",
        "split",
        "stop",
        "ask_user",
    }


# ---------------------------------------------------------------------------
# accepting a split is crash-consistent across all three stores (split-04)
# ---------------------------------------------------------------------------
#
# Everything above grades WHAT a split does. This section grades what is left
# behind when the process does not survive doing it.
#
# Acceptance moves three stores that share no commit point: the task registry
# (`tasks.json`), the parent's execution record (`executions/<id>.json`) and the
# parent's worker repository (`workers/<id>`). A process that dies after the
# registry save and before `retire_execution` leaves the registry saying the
# parent is retired while both artefacts are still live — a state that is not
# merely stale but CONTRADICTORY, and silent: the surviving record holds the
# repository-wide merge window shut (`cli._merge_window_blockers`) and the
# retired parent is never dispatched again for anything to notice.
#
# The claim these tests grade: a crash at ANY write boundary leaves a state the
# next start reconciles, and the three stores then agree. Every one of them
# reloads from DISK through `reopen` before asserting anything — an assertion
# against the crashed process's own in-memory registry would pass without
# checking that a single byte was durable.


class Boom(RuntimeError):
    """A crash, not a failure.

    Deliberately NOT one of the types `release_task_to_pending` or
    `_dispatch_ceiling_split` catch. A caught exception is the SYNCHRONOUS error
    path — those are already handled, and are tested separately below. What is
    being simulated here is the process going away mid-sequence, and the closest
    a test can get to that is an exception nothing on the path is prepared for.
    """


def marker(wiring, task_id="t1") -> Path:
    """The split-acceptance marker's path, through the SAME helper the loop
    resolves it with. A second speller here is how a test starts passing against
    a directory production stopped using."""
    return split_intents_dir(wiring.config.state_dir) / f"{task_id}.json"


def live_record(wiring, task_id="t1") -> bool:
    return (wiring.tmp_path / "executions" / f"{task_id}.json").exists()


def live_worker(wiring, task_id="t1") -> bool:
    return (wiring.tmp_path / "workers" / task_id).exists()


def archived_records(wiring, task_id="t1"):
    return sorted((wiring.tmp_path / "executions" / "archive").glob(f"{task_id}-*.json"))


def quarantined_workers(wiring, task_id="t1"):
    return sorted((wiring.tmp_path / "quarantine").glob(f"{task_id}-*"))


def open_codes(wiring):
    return [b.code for b in BlockerStore(wiring.config.blockers_dir).open_blockers()]


def reopen(wiring, responses=()) -> Wiring:
    """A SECOND process over the same state directory.

    Every store is rebuilt and the registry is RE-READ from `tasks.json`. That
    is the whole point of the helper: reusing the crashed process's in-memory
    registry would assert nothing about what is durable, and is the likeliest
    way a crash-recovery test passes while checking nothing. It also puts the
    round trip through `_persisted_superseded_by` on every assertion about which
    children the parent was retired into.

    Unlike `build`, this wires a real `BlockerStore`, because the failure
    endings below report through one.
    """
    config = wiring.config
    git = GitGateway(wiring.tmp_path / "repo", PolicyEngine(config.policy))
    worker_repos = WorkerRepoManager(
        wiring.tmp_path / "workers", wiring.tmp_path / "worker-hooks"
    )
    execution_store = TaskExecutionStore(wiring.tmp_path / "executions")
    task_store = TaskStore(config.tasks_file)
    registry = task_store.load()
    assert registry is not None, "tasks.json was never written"
    store = StateStore(config.state_file)
    state = store.load() or LoopState.new(URL)
    executor = WritingExecutor(worker_repos.root_dir)
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
        intent_store=IntentStore(wiring.tmp_path / "intents"),
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
        tmp_path=wiring.tmp_path,
    )


def _die(message):
    def boom(*_args, **_kwargs):
        raise Boom(message)

    return boom


def break_add_many(wiring):
    wiring.registry.add_many = _die("died before the children were added")


def break_retire(wiring):
    wiring.registry.retire = _die("died before the parent was retired")


def break_archive(wiring):
    wiring.execution_store.archive = _die("died before the record was archived")


def break_quarantine(wiring):
    wiring.worker_repos.quarantine = _die("died before the worker was quarantined")


def break_marker_clear(wiring):
    wiring.orch._split_intents.clear = _die("died before the marker was dropped")


#: Every durable write boundary in `_dispatch_ceiling_split`, in order, with the
#: state each crash point provably produces: `retired` says whether the REGISTRY
#: write had landed by then, which is the only thing that decides whether the
#: parent's artefacts may be moved afterwards.
CRASH_BOUNDARIES = [
    pytest.param(break_add_many, False, id="before-the-children-are-added"),
    pytest.param(break_retire, False, id="before-the-parent-is-retired"),
    pytest.param(break_archive, True, id="after-the-registry-save"),
    pytest.param(break_quarantine, True, id="after-the-record-is-archived"),
    pytest.param(break_marker_clear, True, id="after-both-artefacts-moved"),
]


def crash_at(tmp_path, injector) -> Wiring:
    """Drive a real split acceptance and kill it at one write boundary."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)
    injector(wiring)
    with pytest.raises(Boom):
        dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))
    assert marker(wiring).exists(), "the crash left no durable record of the split"
    return wiring


def assert_stores_agree(wiring, retired: bool):
    """THE predicate, identical at every boundary.

    Not "the parent is retired and its artefacts are gone" — that is false at
    the two boundaries before the registry write lands, and a predicate that
    only describes the late crashes would grade half the sequence. What has to
    hold everywhere is that the three stores tell ONE story, and that no
    unfinished split is still recorded.
    """
    parent = wiring.registry.get("t1")
    assert not marker(wiring).exists(), "an unfinished split is still recorded"
    if retired:
        assert parent.status == "retired"
        assert set(parent.superseded_by) == {"t1-a", "t1-b"}
        assert not live_record(wiring), "the retired parent still holds the window"
        assert not live_worker(wiring), "the retired parent still owns a worker"
        # Exactly one of each: the recovery must FINISH the retirement, never
        # file a second copy of a half it already moved.
        assert len(archived_records(wiring)) == 1
        assert len(quarantined_workers(wiring)) == 1
    else:
        assert parent.status != "retired"
        assert live_record(wiring), "a live task's execution record was retired"
        assert live_worker(wiring), "a live task's worker repository was retired"
        assert archived_records(wiring) == []
        assert quarantined_workers(wiring) == []


@pytest.mark.parametrize("injector,retired", CRASH_BOUNDARIES)
def test_a_crash_at_any_write_boundary_is_reconcilable(tmp_path, injector, retired):
    """THE claim. One predicate, five boundaries, every assertion made against
    stores re-read from disk by a second process."""
    crashed = crash_at(tmp_path, injector)
    fresh = reopen(crashed)

    fresh.orch._reconcile_split_acceptance()

    assert_stores_agree(fresh, retired)


def test_without_the_reconciliation_the_crash_really_does_leave_them_disagreeing(
    tmp_path,
):
    """The counter-pin, without which the test above grades nothing: the state
    a crash after the registry save actually leaves IS contradictory, and the
    surviving record is the half that costs every other task its merge window."""
    crashed = crash_at(tmp_path, break_archive)
    fresh = reopen(crashed)

    assert fresh.registry.get("t1").status == "retired"
    assert live_record(fresh)
    assert live_worker(fresh)


def test_the_reconciliation_runs_at_startup_and_not_only_on_demand(tmp_path):
    """A crash mid-split leaves the phase wherever the dying round had it, so a
    per-round sweep would settle this behind an arbitrary amount of other work
    while `merge-window` reads the contradictory record in the meantime.
    `run()` is the entry point, and this asserts it rather than the method."""
    crashed = crash_at(tmp_path, break_archive)
    fresh = reopen(crashed)
    fresh.orch.state.phase = Phase.STOPPED.value

    assert fresh.orch.run() == Phase.STOPPED.value

    assert_stores_agree(fresh, True)


def test_reconciling_a_second_time_changes_nothing(tmp_path):
    """Idempotence is what makes a crash DURING a recovery merely another crash
    rather than a new failure mode."""
    crashed = crash_at(tmp_path, break_archive)
    fresh = reopen(crashed)
    fresh.orch._reconcile_split_acceptance()
    settled = (
        [p.name for p in archived_records(fresh)],
        [p.name for p in quarantined_workers(fresh)],
    )

    fresh.orch._reconcile_split_acceptance()

    assert (
        [p.name for p in archived_records(fresh)],
        [p.name for p in quarantined_workers(fresh)],
    ) == settled
    assert_stores_agree(fresh, True)


def test_a_split_that_completes_leaves_no_marker_behind(tmp_path):
    """The ordinary path. A marker that outlived its own split would have every
    later start reconcile a decomposition that finished cleanly."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    assert not marker(wiring).exists()
    assert split_intents_dir(wiring.config.state_dir).is_dir()
    fresh = reopen(wiring)
    fresh.orch._reconcile_split_acceptance()
    assert records(fresh, "split_acceptance_reconciled") == []


def test_a_start_with_nothing_in_flight_reconciles_nothing(tmp_path):
    """The bound. This runs at every start, including the overwhelming majority
    that have never accepted a split at all."""
    wiring = build(tmp_path, tasks=[ready_task("t1")])

    wiring.orch._reconcile_split_acceptance()

    assert records(wiring, "split_acceptance_reconciled") == []
    assert records(wiring, "split_acceptance_unreconciled") == []


def test_a_refused_retirement_parks_and_records_no_unfinished_split(tmp_path):
    """The SYNCHRONOUS half-applied ending, which is consistent rather than
    contradictory: the children exist and the parent is deliberately still live,
    so its record and worker are still its own. The marker must be dropped here
    — a recovery that "finished" this state would retire the artefacts of a task
    the registry says is running."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    def refuse(*_args, **_kwargs):
        raise TaskGraphError("task_in_progress", "a dependent is running")

    wiring.registry.retire = refuse
    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert [r["code"] for r in records(wiring, "needs_user")] == [
        "ceiling_split_parent_not_retired"
    ]
    assert not marker(wiring).exists()
    fresh = reopen(wiring)
    fresh.orch._reconcile_split_acceptance()
    assert {"t1-a", "t1-b"} <= {t.id for t in fresh.registry.all_tasks()}
    assert_stores_agree(fresh, False)


def test_a_marker_that_cannot_be_written_refuses_the_split(tmp_path):
    """Fail CLOSED. Continuing without the marker would leave the split exactly
    as crash-unsafe as it was before this existed, and nothing would say so."""
    wiring = first_round(tmp_path)
    ask_at_ceiling(wiring)

    def refuse(*_args, **_kwargs):
        raise OSError("read-only state directory")

    wiring.orch._split_intents.save = refuse
    dispatch(wiring, plan_block([child_spec("t1-a"), child_spec("t1-b")]))

    assert "ceiling_split_intent_unwritable" in denial_codes(wiring)
    assert sorted(t.id for t in wiring.registry.all_tasks()) == ["t1"]
    assert wiring.registry.get("t1").status == "in_progress"
    assert live_record(wiring) and live_worker(wiring)
    assert not marker(wiring).exists()


def test_a_corrupt_marker_is_reported_rather_than_read_as_absent(tmp_path):
    """The fail-open this must not have. A marker that reads as absent leaves
    the contradictory state in place AND discards the last durable thing that
    knew about it — the alarm switching itself off."""
    crashed = crash_at(tmp_path, break_archive)
    marker(crashed).write_text("{not json", encoding="utf-8")
    fresh = reopen(crashed)

    fresh.orch._reconcile_split_acceptance()

    assert marker(fresh).exists(), "a marker it could not read was thrown away"
    assert live_record(fresh) and live_worker(fresh)
    assert SPLIT_ACCEPTANCE_UNRECONCILED in open_codes(fresh)


def test_a_marker_whose_children_are_a_bare_string_is_refused(tmp_path):
    """`tuple("t1-a")` is five single-character "children", which is the silent
    per-character split `tasks._persisted_superseded_by` exists to refuse. Read
    that way the set comparison could never match, so the marker would sit there
    forever reporting AMBIGUOUS — a permanent non-recovery with a green log."""
    crashed = crash_at(tmp_path, break_archive)
    marker(crashed).write_text(
        json.dumps({"parent_id": "t1", "child_ids": "t1-a", "reason": "x"}),
        encoding="utf-8",
    )
    fresh = reopen(crashed)

    fresh.orch._reconcile_split_acceptance()

    assert marker(fresh).exists()
    assert live_record(fresh) and live_worker(fresh)
    assert SPLIT_ACCEPTANCE_UNRECONCILED in open_codes(fresh)


def test_a_marker_that_names_other_children_moves_nothing(tmp_path):
    """The fail-closed rule: the marker only says a split was ATTEMPTED, and
    what the REGISTRY recorded decides. One that cannot be shown to describe
    that retirement must never move a task's artefacts."""
    crashed = crash_at(tmp_path, break_archive)
    marker(crashed).write_text(
        json.dumps(
            {"parent_id": "t1", "child_ids": ["someone-else"], "reason": "x"}
        ),
        encoding="utf-8",
    )
    fresh = reopen(crashed)

    fresh.orch._reconcile_split_acceptance()

    assert marker(fresh).exists()
    assert live_record(fresh) and live_worker(fresh)
    assert SPLIT_ACCEPTANCE_UNRECONCILED in open_codes(fresh)


def test_every_start_reports_an_unreconciled_split_even_with_the_blocker_open(
    tmp_path,
):
    """The blocker is upserted ONCE; the transcript entry is written every start.

    `_report_strand_blocker` returns early once its blocker is open, and copying
    that here would be a fail-open: the parent in every shape that reaches this
    method is RETIRED, and `cli._reconcile_retired_blockers` resolves open
    blockers naming a retired task. A report living only in the blocker store
    could therefore be closed out from under the condition it describes,
    leaving the marker in place with nothing at all saying so.
    """
    crashed = crash_at(tmp_path, break_archive)
    marker(crashed).write_text(
        json.dumps({"parent_id": "t1", "child_ids": ["someone-else"], "reason": "x"}),
        encoding="utf-8",
    )
    first = reopen(crashed)
    first.orch._reconcile_split_acceptance()
    second = reopen(crashed)
    second.orch._reconcile_split_acceptance()

    reports = records(second, "split_acceptance_unreconciled")
    assert [r["task_id"] for r in reports] == ["t1", "t1"]
    assert len(BlockerStore(second.config.blockers_dir).open_blockers()) == 1


def test_the_recorded_children_are_matched_as_a_set_not_in_order(tmp_path):
    """`superseded_by` round-trips through JSON in the order it was written, so
    exact-tuple equality would hold today and would start failing silently — as
    a permanent AMBIGUOUS, i.e. a recovery that never runs — the first time
    anything normalised that order. Order carries no meaning for a successor
    list, so nothing is given up by not requiring it."""
    crashed = crash_at(tmp_path, break_archive)
    marker(crashed).write_text(
        json.dumps(
            {
                "parent_id": "t1",
                "child_ids": ["t1-b", "t1-a"],
                "reason": CEILING_SPLIT_RETIREMENT_REASON,
            }
        ),
        encoding="utf-8",
    )
    fresh = reopen(crashed)

    fresh.orch._reconcile_split_acceptance()

    assert fresh.registry.get("t1").superseded_by == ("t1-a", "t1-b")
    assert_stores_agree(fresh, True)


def test_a_retirement_that_still_fails_keeps_the_marker_and_names_the_task(tmp_path):
    """A recovery that cannot finish must not clear the marker: doing so would
    take the last durable record of an unfinished split with it. The partial
    progress it DID make is kept — the record is out of the merge window even
    though the worker could not move — and the next start finishes the rest."""
    crashed = crash_at(tmp_path, break_archive)
    stuck = reopen(crashed)

    def refuse(*_args, **_kwargs):
        raise GitCommandError("quarantine destination already exists")

    stuck.worker_repos.quarantine = refuse
    stuck.orch._reconcile_split_acceptance()

    assert marker(stuck).exists()
    assert SPLIT_ACCEPTANCE_UNRECONCILED in open_codes(stuck)
    assert not live_record(stuck)  # the half that DID move stayed moved
    assert live_worker(stuck)

    recovered = reopen(crashed)
    recovered.orch._reconcile_split_acceptance()
    assert_stores_agree(recovered, True)


def test_a_run_with_no_stores_keeps_the_marker_instead_of_clearing_it(tmp_path):
    """An Orchestrator wired without an execution store or worker repositories
    cannot retire anything. Answering "nothing to do" and dropping the marker
    would silently discard the split for the properly-wired run that follows."""

    class Row:
        status = "retired"
        superseded_by = ("t1-a", "t1-b")

    class Registry:
        def has(self, _task_id):
            return True

        def get(self, _task_id):
            return Row()

    result = reconcile_split_acceptance(
        SplitIntent(parent_id="t1", child_ids=("t1-a", "t1-b")),
        Registry(),
        None,
        None,
    )

    assert result.outcome is SplitAcceptance.AMBIGUOUS
    assert result.intent_is_spent is False
