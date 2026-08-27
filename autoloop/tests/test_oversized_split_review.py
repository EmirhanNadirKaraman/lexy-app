"""A candidate too large to REVIEW reaches the split verb, not a park nobody
can act on.

split-05, 2026-08-27. `Decision.SPLIT` (split-03) exists BECAUSE of this
failure — the comment above `_dispatch_split` names brw-14's 416,193-byte range
diff, which PASSED post-commit review and was refused only because the reviewer
could not be shown the diff in full. But the verb could only ever be issued by a
reviewer who had SEEN a candidate, and this park happens strictly BEFORE any
packet reaches one: the mechanism built for the failure could not be triggered
by it. Three occurrences in under a week (port-01 414,596 bytes; brw-14 416,193;
brw-18 683,693, split by hand from outside the loop), each costing a full
executor round and ending in `needs_user` on `review_packet_build_failed`.

THE ONE CLAIM these tests grade: when — and ONLY when — the review packet fails
because of the SIZE CAP, an eligible candidate is presented to the reviewer as a
STAT-ONLY packet, and the answer either splits through the existing
`_apply_split` or parks on the existing `review_packet_build_failed` code.

The four paths the task requires, one section each:

  * an oversized candidate is asked about and CAN be split;
  * a reviewer that judges it ONE claim parks it, on the same code as before;
  * every NON-SIZE `GitCommandError` parks exactly as it always did, with no ask;
  * a SUCCESSOR of an earlier split parks — `MAX_SPLIT_DEPTH` is 1, so there is
    no second split to ask for, and asking anyway would build a loop with no
    park in it.

Plus the two properties that make the ask safe rather than merely useful: no
approval can bind to a stat (a stat is a complete artifact, but it is not the
diff), and the ask costs the same one attempt the park it replaces costs.

Real git and real worker repos throughout, with the `run_git`/executor/`build`
helpers duplicated per this suite's self-contained convention (the same shape
`test_split_decision.py` uses, and for the same reason).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.errors import GitCommandError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import (
    MAX_SPLIT_DEPTH,
    MIN_CEILING_SPLIT_TASKS,
    REASON_SENT_FOR_SPLIT_REVIEW,
    REVIEWER_SPLIT_ORIGIN,
    Orchestrator,
)
from autoloop.packet import STAT_ONLY_PACKET_BANNER
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import IntentStore, TaskExecutionStore, attempt_outcome, split_attempt

URL = "https://chatgpt.com/c/test-conversation"

PATHS = ("docs/A.md", "docs/B.md")

FIRST_PLAN = {
    "approach": "one commit",
    "files": ["docs/A.md"],
    "steps": ["write the file"],
}

#: Big enough that its PATCH dwarfs `SMALL_CAP` while its `--stat` (one line for
#: one file) stays far under it. That relationship is the whole mechanism: the
#: stat renders precisely where the patch does not.
BIG_FILE = "\n".join(f"line {i:04d} of a change nobody can be shown" for i in range(400))

#: A render cap between the two sizes above. Not `10` (the value
#: `test_postcommit_review.py`'s older park test uses): at 10 the STAT busts the
#: cap too, which is a different path — the one where there is nothing to show
#: at all, and the loop parks.
SMALL_CAP = 2_000


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


def implement_block(task_id="t1"):
    return block(
        {
            "version": 3,
            "decision": "implement",
            "reason": "next",
            "task_id": task_id,
            "decomposition": FIRST_PLAN,
        }
    )


def successor(tid, paths=("docs/A.md",)):
    return {
        "id": tid,
        "title": f"Successor {tid}",
        "description": "one independently reviewable piece",
        "approved_paths": list(paths),
    }


def split_block(task_id="t1", specs=None, reason="the patch cannot be shown in one piece"):
    return block(
        {
            "version": 3,
            "decision": "split",
            "reason": reason,
            "task_id": task_id,
            "tasks": list(specs if specs is not None else [successor("t1-a"), successor("t1-b")]),
        }
    )


def stop_block(reason="this is ONE claim; splitting it would be wrong"):
    return block({"version": 3, "decision": "stop", "reason": reason})


def extract_stamp(prompt: str) -> dict:
    return {
        "request_id": re.search(r"request_id: (\S+)", prompt).group(1),
        "head_sha": re.search(r"head_sha: (\S+)", prompt).group(1),
        "report_sha256": re.search(r"report_sha256: (\S+)", prompt).group(1),
    }


def push_block(client):
    return block(
        {
            "version": 3,
            "decision": "push",
            "reason": "looks fine to me",
            "reviewed": extract_stamp(client.submitted[-1][1]),
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
        self.files = dict(files or {"docs/A.md": BIG_FILE})
        self.calls: list[tuple] = []

    def execute(self, directive, task):
        self.calls.append((directive, task))
        worker = self.workers_root / task.id
        for rel, content in self.files.items():
            target = worker / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{content}\n<!-- call {len(self.calls)} -->\n", encoding="utf-8")
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


def make_repo(tmp_path: Path, branch: str = "main") -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    run_git(repo_root, "init", "-q", "-b", branch)
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


def build(tmp_path, responses=(), tasks=(), files=None, branch="main") -> Wiring:
    repo_root = make_repo(tmp_path, branch)
    policy_config = PolicyConfig(implement_enabled=True)
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
    return Task(id=tid, title=f"Title {tid}", description="desc", approved_paths=PATHS, **kwargs)


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


def blockers_written(wiring):
    return [b.code for b in BlockerStore(wiring.config.blockers_dir).open_blockers()]


def last_attempt_outcome(wiring, task_id="t1") -> str:
    execution = wiring.execution_store.load(task_id)
    assert execution is not None, "the execution record is gone"
    _ordinal, _budget, reason = split_attempt(execution.attempt_ledger[-1])
    return attempt_outcome(reason)


def oversized_round(tmp_path, monkeypatch, responses, tasks=None, branch="main", steps=6):
    """One executor round whose committed candidate's PATCH busts the render cap
    while its STAT does not — the exact shape of brw-14 — driven through the real
    `run` loop, plus however many further steps the scripted replies need.

    The step counts are exact and matter. Four steps per round trip (ready,
    submitting, awaiting, executing), so `steps=6` stops with the ask SENT and
    `steps=8` stops immediately after the reply is dispatched. Going further
    re-enters `ready`, which moves `state.outbox` into the request and clears
    it — so a test asserting on the report a dispatch produced must stop at 8.
    """
    monkeypatch.setattr(GitGateway, "RANGE_DIFF_MAX_BYTES", SMALL_CAP)
    wiring = build(
        tmp_path,
        responses=responses,
        tasks=tasks or [ready_task("t1")],
        branch=branch,
    )
    wiring.orch.run(max_steps=steps)
    return wiring


# ---------------------------------------------------------------------------
# 1. the ask itself: a stat-only packet, and it says what it is
# ---------------------------------------------------------------------------


def test_an_oversized_candidate_is_shown_as_a_stat_and_not_parked(tmp_path, monkeypatch):
    wiring = oversized_round(tmp_path, monkeypatch, [implement_block("t1")])

    assert wiring.orch.state.phase != Phase.NEEDS_USER.value, "the old park still fired"
    prompt = wiring.client.submitted[-1][1]
    assert STAT_ONLY_PACKET_BANNER in prompt
    assert "NOTHING HERE CAN BE APPROVED" in prompt
    # The stat and the file list ARE there — that is what makes the question
    # answerable — and the patch is not.
    assert "Diff stat:" in prompt
    assert "docs/A.md" in prompt
    assert "line 0399 of a change nobody can be shown" not in prompt, "the patch leaked in"
    assert "Full diff:" not in prompt
    execution = wiring.execution_store.load("t1")
    assert execution.candidate_sha != "", "the commit exists and was not rolled back"
    assert execution.published_sha == ""
    # `review_round` counts REVIEWS OF A DIFF. No diff was shown, so charging
    # one here would spend the revision budget of a candidate nobody reviewed —
    # the same rule the park it replaces follows.
    assert execution.review_round == 0
    assert last_attempt_outcome(wiring) == REASON_SENT_FOR_SPLIT_REVIEW


def test_the_ask_costs_exactly_one_attempt_like_the_park_it_replaces(tmp_path, monkeypatch):
    """The attempt-budget check the task asks for, from the other end: a round
    that asks must not be cheaper than a round that parked, or a task could
    produce unshowable candidates forever without approaching its ceiling."""
    wiring = oversized_round(tmp_path, monkeypatch, [implement_block("t1")])

    execution = wiring.execution_store.load("t1")
    assert execution.attempt_count == 1
    assert execution.fault_attempt_count == 0
    assert len(execution.attempt_ledger) == 1, "one ledger entry per dispatch, still"


# ---------------------------------------------------------------------------
# 2. the reviewer splits it — through the acceptance that already existed
# ---------------------------------------------------------------------------


def test_the_reviewer_can_split_a_candidate_it_was_never_shown(tmp_path, monkeypatch):
    wiring = oversized_round(
        tmp_path,
        monkeypatch,
        [implement_block("t1"), split_block("t1")],
        steps=8,
    )

    on_disk = wiring.task_store.load()
    assert {t.id for t in on_disk.all_tasks()} >= {"t1", "t1-a", "t1-b"}
    assert on_disk.get("t1").status == "retired"
    assert on_disk.get("t1").superseded_by == ("t1-a", "t1-b")
    assert on_disk.state_of("t1-a") is TaskState.READY
    # The acceptance was the SHARED one, reached through the reviewer origin.
    applied = records(wiring, REVIEWER_SPLIT_ORIGIN.log_event)
    assert applied, "no split was applied"
    assert applied[-1]["children"] == ["t1-a", "t1-b"]
    # Requirement 5: the transcript SAYS the validated commit is discarded,
    # rather than leaving it to be inferred from a sha in a field named
    # `discarded_candidate`.
    assert "DISCARDED" in applied[-1]["discarded_candidate_note"]
    assert applied[-1]["discarded_candidate"], "the sha is still recorded too"
    # And so does the report the reviewer reads.
    assert "THE COMMITTED WORK IS DISCARDED" in (wiring.orch.state.outbox or "")
    assert "passed validation and post-commit review" in (wiring.orch.state.outbox or "")
    # The parent's record and worker were archived/quarantined, not deleted.
    assert wiring.execution_store.load("t1") is None
    assert not (wiring.worker_repos.root_dir / "t1").exists()


def test_a_split_of_an_unshown_candidate_still_meets_every_existing_bound(
    tmp_path, monkeypatch
):
    """The ask must not become a second acceptance path with its own rules. A
    one-successor plan is refused by `MIN_CEILING_SPLIT_TASKS` exactly as it is
    for a reviewer-initiated split — this dispatch routes through
    `_dispatch_split`, it does not reimplement it."""
    wiring = oversized_round(
        tmp_path,
        monkeypatch,
        [implement_block("t1"), split_block("t1", specs=[successor("t1-only")])],
        steps=8,
    )

    codes = [record.get("code") for record in records(wiring, "policy_denied")]
    assert REVIEWER_SPLIT_ORIGIN.code_too_small in codes
    on_disk = wiring.task_store.load()
    assert on_disk.get("t1").status != "retired", "nothing was changed"
    assert "t1-only" not in {t.id for t in on_disk.all_tasks()}


# ---------------------------------------------------------------------------
# 3. "this is ONE claim" — the park stays available, on the same code
# ---------------------------------------------------------------------------


def test_a_reviewer_that_declines_to_split_parks_on_the_same_code(tmp_path, monkeypatch):
    wiring = oversized_round(
        tmp_path,
        monkeypatch,
        [implement_block("t1"), stop_block()],
        steps=10,
    )

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert "review_packet_build_failed" in blockers_written(wiring)
    question = wiring.orch.state.question or ""
    assert "STAT-ONLY" in question
    assert "this is ONE claim" in question, "the reviewer's own reason is recorded"
    assert "NOT rolled back and NOT pushed" in question
    execution = wiring.execution_store.load("t1")
    assert execution.candidate_sha != "" and execution.published_sha == ""


def test_the_declined_park_cannot_re_fire_after_an_operator_resumes(tmp_path, monkeypatch):
    """The gate is armed by `state.task_execution`, and the park CONSUMES it.
    Without that, the first directive after an operator answered the blocker
    would re-park — a park with no way out, which is worse than the park this
    whole mechanism replaced."""
    wiring = oversized_round(
        tmp_path,
        monkeypatch,
        [implement_block("t1"), stop_block()],
        steps=10,
    )

    assert wiring.orch.state.task_execution is None
    assert wiring.orch._stat_only_split_review_task() == ""


# ---------------------------------------------------------------------------
# 4. every OTHER GitCommandError parks exactly as it always did
# ---------------------------------------------------------------------------


def test_a_git_failure_that_is_not_about_size_parks_unchanged(tmp_path, monkeypatch):
    """A torn repository is not a task that needs cutting up. The broad
    `except GitCommandError` still parks on the same code, with the same
    message, and NOTHING is asked of the reviewer."""

    def torn(self, base_sha, candidate_sha):
        raise GitCommandError("diff-tree died: object file is empty")

    monkeypatch.setattr(GitGateway, "range_diff", torn)
    wiring = build(tmp_path, responses=[implement_block("t1")], tasks=[ready_task("t1")])
    wiring.orch.run(max_steps=6)

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert "review_packet_build_failed" in blockers_written(wiring)
    question = wiring.orch.state.question or ""
    assert "review packet could not be built" in question
    assert "object file is empty" in question
    assert STAT_ONLY_PACKET_BANNER not in question
    assert "No split was offered" not in question, "this is not a size refusal"
    assert last_attempt_outcome(wiring) == "review_packet_build_failed"
    # The kickoff went out; nothing else did.
    assert len(wiring.client.submitted) == 1


# ---------------------------------------------------------------------------
# 5. a SUCCESSOR that busts the cap parks — there is no second split
# ---------------------------------------------------------------------------


def test_a_successor_of_an_earlier_split_parks_instead_of_asking(tmp_path, monkeypatch):
    wiring = oversized_round(
        tmp_path,
        monkeypatch,
        [implement_block("t1")],
        tasks=[ready_task("t1", split_depth=MAX_SPLIT_DEPTH, inherited_attempts=1)],
    )

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert "review_packet_build_failed" in blockers_written(wiring)
    question = wiring.orch.state.question or ""
    assert "review packet could not be built" in question
    assert "ALREADY a successor" in question
    assert f"cap {MAX_SPLIT_DEPTH}" in question
    assert last_attempt_outcome(wiring) == "review_packet_build_failed"
    assert len(wiring.client.submitted) == 1, "nothing was asked of the reviewer"


def test_a_stat_that_busts_the_cap_too_parks_rather_than_showing_less(tmp_path, monkeypatch):
    """The stat is normally ~2 KB. When it is not — tens of thousands of paths,
    or a repository that fails the stat as readily as the patch — there is
    nothing to show, and nothing is shortened to manufacture something."""
    monkeypatch.setattr(GitGateway, "RANGE_DIFF_MAX_BYTES", 10)
    wiring = build(tmp_path, responses=[implement_block("t1")], tasks=[ready_task("t1")])
    wiring.orch.run(max_steps=6)

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert "review_packet_build_failed" in blockers_written(wiring)
    assert "the stat-only packet could not be built" in (wiring.orch.state.question or "")
    assert len(wiring.client.submitted) == 1


# ---------------------------------------------------------------------------
# 6. NO REVIEW BYPASS: a stat can never authorize a publish
# ---------------------------------------------------------------------------


def test_a_stat_only_packet_binds_no_approval(tmp_path, monkeypatch):
    """The structural half, and the one that does not depend on the dispatch
    guard firing: the request carrying a stat-only packet gets NO
    `PostcommitBinding`, so there is nothing an approval could ever resolve to
    — even though the packet carries the same four identifiers every packet
    does."""
    wiring = oversized_round(tmp_path, monkeypatch, [implement_block("t1")])

    request = wiring.orch.state.pending_request
    assert request is not None and STAT_ONLY_PACKET_BANNER in request.payload
    execution = wiring.execution_store.load("t1")
    assert execution.candidate_sha in request.payload, "the identifiers ARE present"
    assert request.postcommit is None, "a stat must never bind an approval"
    assert wiring.orch.state.sent_postcommits == []


def test_an_approval_answering_a_stat_only_packet_publishes_nothing(tmp_path, monkeypatch):
    """And the dispatch half. The checkout sits on a non-protected branch here
    on purpose, so the `push` is not stopped early by the protected-branch
    denial and genuinely reaches the dispatch this guard lives in."""
    wiring = oversized_round(
        tmp_path,
        monkeypatch,
        [implement_block("t1"), push_block],
        branch="work",
        steps=10,
    )

    assert wiring.orch.state.phase == Phase.NEEDS_USER.value
    assert "review_packet_build_failed" in blockers_written(wiring)
    assert "The reply was `push`" in (wiring.orch.state.question or "")
    execution = wiring.execution_store.load("t1")
    assert execution.published_sha == "", "nothing was published"


# ---------------------------------------------------------------------------
# 7. the packet says the two things a reviewer must not have to infer
# ---------------------------------------------------------------------------


def test_the_ask_states_the_bounds_the_answer_has_to_meet(tmp_path, monkeypatch):
    wiring = oversized_round(tmp_path, monkeypatch, [implement_block("t1")])

    prompt = wiring.client.submitted[-1][1]
    assert f"at least {MIN_CEILING_SPLIT_TASKS} successors" in prompt
    assert "approved_paths" in prompt
    assert "THE COMMITTED WORK IS DISCARDED EITHER WAY BY A SPLIT" in prompt
    # The park is offered as a legitimate answer, not hidden.
    assert "ONE CLAIM" in prompt
    assert "parks for a human operator" in prompt


@pytest.mark.parametrize(
    "clause",
    [
        "`revise` (it orders the same size again)",
        "`recut` (the same task from a clean base is the same size)",
        "an approval (you have not read this change)",
    ],
)
def test_the_ask_says_which_other_verbs_park(tmp_path, monkeypatch, clause):
    """A reviewer that has to spend a round discovering that `revise` orders the
    same size again has been told nothing the request could not have told it.

    Asserted as whole CLAUSES, not as the bare verb names: `revise` and `recut`
    both appear in `CONTRACT_INSTRUCTIONS`, which ships in the same prompt, so a
    bare-name assertion would pass with this paragraph deleted."""
    wiring = oversized_round(tmp_path, monkeypatch, [implement_block("t1")])

    assert clause in wiring.client.submitted[-1][1]
