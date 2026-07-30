"""Orchestrator state machine with fake conversation / git / executor:
audit flow, task-owned change manifests around commits, phase gating,
review-integrity verification, terminal decisions, corrective re-prompts,
budgets, crash recovery, duplicate-submission protection, pause."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.browser.chatgpt import SubmitResult
from autoloop.contract import Decision
from autoloop.errors import (
    GitCommandError,
    LoginExpiredError,
    ResponseTimeoutError,
    SessionLostError,
    StateError,
    SubmissionError,
)
from autoloop.executor import ExecutionOutcome
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import (
    LastResponse,
    LoopState,
    PendingRequest,
    Phase,
    StateStore,
)
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.transcript import TranscriptLogger

URL = "https://chatgpt.com/c/test-conversation"


def block(obj) -> str:
    return f"Reasoning...\n```json\n{json.dumps(obj)}\n```"


def plan_block(*ids):
    tasks = [{"id": tid, "title": f"Title {tid}", "description": "desc"} for tid in ids]
    return block({"version": 3, "decision": "plan", "reason": "roadmap", "tasks": tasks})


def implement_block(task_id="t1"):
    return block(
        {"version": 3, "decision": "implement", "reason": "next", "task_id": task_id}
    )


def audit_block(scope=None):
    data = {"version": 3, "decision": "audit", "reason": "orient"}
    if scope:
        data["scope"] = scope
    return block(data)


def revise_audit_block(feedback="dig deeper into migrations"):
    return block(
        {
            "version": 3,
            "decision": "revise",
            "reason": "insufficient",
            "task_id": "audit",
            "feedback": feedback,
        }
    )


def stop_block():
    return block({"version": 3, "decision": "stop", "reason": "all done"})


def ask_user_block(question="which DB?"):
    return block(
        {"version": 3, "decision": "ask_user", "reason": "unsure", "question": question}
    )


def extract_stamp(prompt: str) -> dict:
    return {
        "request_id": re.search(r"request_id: (\S+)", prompt).group(1),
        "head_sha": re.search(r"head_sha: (\S+)", prompt).group(1),
        "report_sha256": re.search(r"report_sha256: (\S+)", prompt).group(1),
    }


def approval(decision="commit", paths=("out.md",), task_id=None, message="docs: audit", stamp=None):
    def responder(client):
        data = {
            "version": 3,
            "decision": decision,
            "reason": "approved",
            "reviewed": stamp or extract_stamp(client.submitted[-1][1]),
        }
        if decision in ("commit", "commit_and_push"):
            data["commit"] = {"message": message, "paths": list(paths)}
            if task_id:
                data["task_id"] = task_id
        return block(data)

    return responder


class FakeClient:
    """Conversation double for the post-repair interface.

    `persisted` is server truth. `submit_result` decides what the transport
    reports; UNCONFIRMED deliberately does NOT add to `persisted`, modelling a
    send whose acceptance is unknown.
    """

    def __init__(self, responses=(), submit_result=SubmitResult.CONFIRMED):
        self.responses = list(responses)
        self.submitted: list[tuple[str, str]] = []
        self.persisted: set[str] = set()
        self.attach_calls = 0
        self.reconcile_calls: list[str] = []
        self.closed = False
        self.attach_errors: list[Exception] = []
        self.submit_errors: list[Exception] = []
        self.submit_result = submit_result
        #: Mirrors BrowserChatGPT.send_attempted — False until Send is clicked.
        #: The orchestrator reads it to decide whether a resend is provably safe.
        self.send_attempted = False

    def attach(self):
        self.attach_calls += 1
        if self.attach_errors:
            raise self.attach_errors.pop(0)

    def has_request(self, request_id):
        return request_id in self.persisted

    def reconcile(self, request_id):
        self.reconcile_calls.append(request_id)
        return request_id in self.persisted

    def submit(self, request_id, prompt):
        if self.submit_errors:
            raise self.submit_errors.pop(0)  # fails BEFORE clicking Send
        self.send_attempted = True
        self.submitted.append((request_id, prompt))
        result = self.submit_result
        if result is SubmitResult.CONFIRMED:
            self.persisted.add(request_id)
        return result

    def await_response(self, request_id):
        if not self.responses:
            raise AssertionError("test script exhausted: no response left")
        entry = self.responses.pop(0)
        return entry(self) if callable(entry) else entry

    def close(self):
        self.closed = True


class FakeGit:
    """Filesystem-backed fake: dirty_files reflects porcelain lines the test
    (or the fake executor) appends; commit validates exact paths."""

    def __init__(self, repo_root: Path, branch="feature/x"):
        self.repo_root = Path(repo_root)
        self.branch = branch
        self.head = "a" * 40
        self.dirty: list[str] = []
        self.commits: list[tuple[str, tuple[str, ...]]] = []
        self.pushes = 0
        self.commit_error: Exception | None = None
        self.push_error: Exception | None = None
        self.index: dict[str, bytes] = {}
        self.stage_hook = None
        self.restore_hook = None
        self.trees: dict[str, dict] = {"tree-parent": {}}
        self.blobs: dict[str, bytes] = {}
        self.head_tree = "tree-parent"
        self.active_hooks: list[str] = []
        self.detached = False

    def staged_blob(self, path):
        if path not in self.index:
            raise GitCommandError(f"no staged content for {path}")
        return self.index[path]

    def staged_mode(self, path):
        return "100644" if path in self.index else ""

    # -- immutable-tree commit path -------------------------------------------
    def tree_entries(self, tree):
        return self.trees[tree]

    def blob_bytes(self, oid):
        return self.blobs[oid]

    def changed_paths(self, tree_a, tree_b):
        a, b = self.trees.get(tree_a, {}), self.trees.get(tree_b, {})
        return {p for p in set(a) | set(b) if a.get(p) != b.get(p)}

    def commit_adopted(self, message, paths, verify_tree):
        """Model the real sequence: stage -> tree -> verify -> commit -> CAS."""
        if self.active_hooks:
            raise GitCommandError(
                f"adopted commit refused: active commit hook(s) {self.active_hooks}"
            )
        if self.detached:
            raise GitCommandError("adopted commit requires a symbolic branch HEAD")
        if self.commit_error:
            raise self.commit_error
        if self.stage_hook is not None:
            self.stage_hook()
        for rel in paths:                      # `git add` snapshots the worktree
            target = self.repo_root / rel
            if target.exists():
                self.index[rel] = target.read_bytes()
        if self.restore_hook is not None:
            self.restore_hook()
        # write-tree: an immutable snapshot of the index
        tree_id = f"tree-{len(self.trees)}"
        entries = {}
        for rel, data in self.index.items():
            oid = hashlib.sha256(data).hexdigest()
            self.blobs[oid] = data
            entries[rel] = ("100644", "blob", oid)
        self.trees[tree_id] = entries
        violations = verify_tree(tree_id, self.head_tree)
        if violations:
            raise GitCommandError("adopted commit refused: " + "; ".join(violations))
        self.commits.append((message, tuple(paths)))
        self.head = "c" * 40
        self.head_tree = tree_id
        approved = set(paths)
        self.dirty = [
            line for line in self.dirty if line[3:].split(" -> ")[-1] not in approved
        ]
        return self.head, "1 file changed", sorted(
            line[3:].split(" -> ")[-1] for line in self.dirty
        )

    def current_branch(self):
        return self.branch

    def head_sha(self):
        return self.head

    def dirty_files(self):
        return list(self.dirty)

    def dirty_entries(self):
        """NUL-safe equivalent of the real gateway's parser."""
        out = []
        for line in self.dirty:
            if len(line) > 3:
                out.append((line[:2], line[3:].split(" -> ")[-1]))
        return out

    def commit(self, message, paths, post_stage_check=None):
        if self.commit_error:
            raise self.commit_error
        if not paths:
            raise GitCommandError("commit requires an explicit non-empty path list")
        # Mirror the real gateway: `git add` snapshots the working tree into the
        # index, THEN the hook runs, THEN the commit is created. Modelling the
        # index is what lets a test prove the hook reads staged bytes rather
        # than re-reading the file.
        if self.stage_hook is not None:
            self.stage_hook()                      # simulate a swap before `add`
        for rel in paths:
            target = self.repo_root / rel
            if target.exists():
                self.index[rel] = target.read_bytes()
        if self.restore_hook is not None:
            self.restore_hook()                    # worktree put back afterwards
        if post_stage_check is not None:
            post_stage_check()
        self.commits.append((message, tuple(paths)))
        self.head = "c" * 40
        approved = set(paths)
        self.dirty = [
            line for line in self.dirty if line[3:].split(" -> ")[-1] not in approved
        ]
        return self.head, False, "1 file changed, 1 insertion(+)"

    def push(self, remote="origin"):
        if self.push_error:
            raise self.push_error
        self.pushes += 1
        return "to origin"


class FakeExecutor:
    """Optionally creates files (like a real task would) so the manifest has
    task-owned changes to approve."""

    def __init__(self, status="ok", creates=None):
        self.status = status
        self.creates = dict(creates or {})
        self.calls: list[tuple] = []
        self.git: FakeGit | None = None

    def execute(self, directive, task):
        self.calls.append((directive, task))
        for rel_path, content in self.creates.items():
            target = self.git.repo_root / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            line = f"?? {rel_path}"
            if line not in self.git.dirty:
                self.git.dirty.append(line)
        return ExecutionOutcome(
            status=self.status,
            summary="did it",
            details="details here",
            validation="ruff clean; 5 tests passed",
        )


def build(
    tmp_path,
    responses=(),
    policy=None,
    clients=None,
    state=None,
    tasks=(),
    executor=None,
    branch="feature/x",
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    (repo_root / "x.py").write_text("pre-existing dirty content\n", encoding="utf-8")
    git = FakeGit(repo_root, branch=branch)
    git.dirty.append(" M x.py")

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=policy or PolicyConfig(),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    if state is None:
        state = LoopState.new(URL)
        state.outbox = "kickoff report"
    store.save(state)
    registry = TaskRegistry(list(tasks))
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    remaining = list(clients) if clients is not None else [FakeClient(responses)]
    made = list(remaining)

    def factory():
        if not remaining:
            raise AssertionError("client factory exhausted")
        return remaining.pop(0)

    executor = executor or FakeExecutor()
    executor.git = git
    manifest_store = ManifestStore(config.manifests_dir)
    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=factory,
        registry=registry,
        task_store=task_store,
        manifest_store=manifest_store,
    )
    return orch, store, git, executor, made, registry, manifest_store


def ready_task(tid="t1"):
    return Task(id=tid, title=f"Title {tid}", description="desc")


IMPLEMENT_ON = PolicyConfig(implement_enabled=True)


# ---- audit flow -------------------------------------------------------------


def test_audit_decision_executes_and_reports(tmp_path):
    executor = FakeExecutor(creates={"docs/AUDIT_2026-07-29.md": "# audit report"})
    orch, _, _, executor, clients, _, manifest_store = build(
        tmp_path, responses=[audit_block(scope="focus on SRS")], executor=executor
    )
    orch.run(max_steps=4)
    directive, task = executor.calls[0]
    assert directive.decision is Decision.AUDIT
    assert task is None
    assert "counterpart" in orch.state.outbox  # audit template wraps the report
    # manifest recorded the report as task-created
    manifest = manifest_store.load(orch.state.last_manifest_id)
    assert manifest.created == ["docs/AUDIT_2026-07-29.md"]
    assert manifest.task_id == "audit"


def test_audit_revision_loop(tmp_path):
    executor = FakeExecutor(creates={"docs/AUDIT_2026-07-29.md": "# v2 report"})
    orch, _, _, executor, _, _, _ = build(
        tmp_path,
        responses=[audit_block(), revise_audit_block("check migrations"), stop_block()],
        executor=executor,
    )
    assert orch.run() == Phase.STOPPED.value
    decisions = [d.decision for d, _ in executor.calls]
    assert decisions == [Decision.AUDIT, Decision.REVISE]
    revise_directive, revise_task = executor.calls[1]
    assert revise_task is None
    assert revise_directive.feedback == "check migrations"


def test_crash_recovery_during_audit_redispatches(tmp_path):
    state = LoopState.new(URL)
    state.iteration = 1
    state.phase = Phase.EXECUTING.value
    state.last_response = LastResponse(
        request_id="alr-crash-0001",
        raw=audit_block(),
        received_at="t",
        head_sha="a" * 40,
        base_sha="(none)",
        report_sha256="b" * 64,
    )
    orch, _, _, executor, _, _, _ = build(tmp_path, clients=[FakeClient()], state=state)
    orch.run(max_steps=1)
    assert len(executor.calls) == 1
    assert orch.state.phase == Phase.READY.value


# ---- phase gate -------------------------------------------------------------


def test_implement_rejected_in_audit_phase(tmp_path):
    orch, _, _, executor, clients, _, _ = build(
        tmp_path,
        responses=[implement_block("t1"), stop_block()],
        tasks=[ready_task("t1")],
    )
    assert orch.run() == Phase.STOPPED.value
    assert executor.calls == []
    reprompt = clients[0].submitted[1][1]
    assert "policy_denied" in reprompt
    assert "audit review only" in reprompt


def test_revise_of_registry_task_rejected_in_audit_phase(tmp_path):
    raw = block(
        {
            "version": 3,
            "decision": "revise",
            "reason": "r",
            "task_id": "t1",
            "feedback": "f",
        }
    )
    orch, _, _, executor, _, _, _ = build(
        tmp_path, responses=[raw, stop_block()], tasks=[ready_task("t1")]
    )
    assert orch.run() == Phase.STOPPED.value
    assert executor.calls == []


def test_implement_works_when_phase_gate_lifted(tmp_path):
    orch, _, _, executor, _, registry, _ = build(
        tmp_path,
        responses=[implement_block("t1")],
        tasks=[ready_task("t1")],
        policy=IMPLEMENT_ON,
    )
    orch.run(max_steps=4)
    directive, task = executor.calls[0]
    assert task.id == "t1"
    assert registry.state_of("t1") is TaskState.IN_PROGRESS


# ---- task flow (gate lifted) ------------------------------------------------


def test_plan_then_implement_flow(tmp_path):
    orch, _, _, executor, clients, registry, _ = build(
        tmp_path,
        responses=[plan_block("t1", "t2"), implement_block("t1")],
        policy=IMPLEMENT_ON,
    )
    outcome = orch.run(max_steps=8)
    assert outcome == Phase.READY.value
    assert registry.state_of("t2") is TaskState.READY
    directive, task = executor.calls[0]
    assert directive.decision is Decision.IMPLEMENT
    assert task.id == "t1"
    assert "commit" in orch.state.outbox  # ok outcome -> commit_approval template
    assert orch.state.last_validation == "ruff clean; 5 tests passed"


def test_plan_rejected_keeps_registry_and_reports(tmp_path):
    orch, _, _, _, clients, registry, _ = build(
        tmp_path,
        responses=[plan_block("t1"), stop_block()],
        tasks=[ready_task("t1")],  # duplicate id -> rejected
    )
    assert orch.run() == Phase.STOPPED.value
    assert len(registry.all_tasks()) == 1
    assert "plan_rejected" in clients[0].submitted[1][1]


# ---- change manifest + commit gate ------------------------------------------


def audit_then_commit(tmp_path, commit_responder, executor=None, extra_responses=()):
    executor = executor or FakeExecutor(creates={"out.md": "# report"})
    return build(
        tmp_path,
        responses=[audit_block(), commit_responder, *extra_responses],
        executor=executor,
    )


def test_commit_of_task_created_file_succeeds(tmp_path):
    orch, _, git, _, clients, _, _ = audit_then_commit(
        tmp_path, approval("commit", paths=("out.md",))
    )
    orch.run(max_steps=8)
    assert git.commits == [("docs: audit", ("out.md",))]
    assert orch.state.reviewed_commit == "c" * 40
    assert "NOT yet pushed" in orch.state.outbox


def test_commit_without_manifest_refused(tmp_path):
    # First directive is a commit — no task ever executed, no manifest.
    orch, _, git, _, clients, _, _ = build(
        tmp_path,
        responses=[approval("commit", paths=("x.py",)), stop_block()],
    )
    assert orch.run() == Phase.STOPPED.value
    assert git.commits == []
    assert "no change manifest recorded" in clients[0].submitted[1][1]


def test_commit_of_preexisting_dirty_file_refused(tmp_path):
    # x.py was dirty BEFORE the audit ran and the audit did not touch it.
    orch, _, git, _, clients, _, _ = audit_then_commit(
        tmp_path, approval("commit", paths=("x.py",)), extra_responses=[stop_block()]
    )
    assert orch.run() == Phase.STOPPED.value
    assert git.commits == []
    reprompt = clients[0].submitted[2][1]
    assert "commit refused" in reprompt
    assert "already modified before the task started" in reprompt


def test_commit_of_untouched_file_refused(tmp_path):
    orch, _, git, _, clients, _, _ = audit_then_commit(
        tmp_path,
        approval("commit", paths=("out.md", "never_touched.md")),
        extra_responses=[stop_block()],
    )
    assert orch.run() == Phase.STOPPED.value
    assert git.commits == []
    assert "was not changed by task" in clients[0].submitted[2][1]


def test_file_changed_during_task_on_top_of_baseline_is_committable(tmp_path):
    # x.py is dirty at baseline AND the task edits it further -> task-modified.
    executor = FakeExecutor(creates={"x.py": "content rewritten by the task\n"})
    orch, _, git, _, _, _, manifest_store = audit_then_commit(
        tmp_path, approval("commit", paths=("x.py",)), executor=executor
    )
    orch.run(max_steps=8)
    manifest = manifest_store.load(orch.state.last_manifest_id)
    assert manifest.modified == ["x.py"]
    assert git.commits == [("docs: audit", ("x.py",))]


def test_commit_and_push_flow(tmp_path):
    orch, _, git, _, _, _, _ = audit_then_commit(
        tmp_path, approval("commit_and_push", paths=("out.md",))
    )
    orch.run(max_steps=8)
    assert len(git.commits) == 1
    assert git.pushes == 1
    assert "Git action completed" in orch.state.outbox


def test_commit_completes_referenced_task(tmp_path):
    executor = FakeExecutor(creates={"out.md": "# change"})
    orch, _, git, _, _, registry, _ = build(
        tmp_path,
        responses=[implement_block("t1"), approval("commit", paths=("out.md",), task_id="t1")],
        tasks=[ready_task("t1")],
        policy=IMPLEMENT_ON,
        executor=executor,
    )
    orch.run(max_steps=8)
    assert registry.state_of("t1") is TaskState.COMPLETED
    assert git.commits == [("docs: audit", ("out.md",))]


# ---- review integrity -------------------------------------------------------


def test_stale_stamp_rejected_and_nothing_committed(tmp_path):
    stale = {"request_id": "alr-old-0001", "head_sha": "f" * 40, "report_sha256": "0" * 64}
    orch, _, git, _, clients, _, _ = audit_then_commit(
        tmp_path,
        approval("commit", paths=("out.md",), stamp=stale),
        extra_responses=[stop_block()],
    )
    assert orch.run() == Phase.STOPPED.value
    assert git.commits == []
    assert "review_mismatch:request_id" in clients[0].submitted[2][1]


def test_head_moved_since_review_rejected(tmp_path):
    executor = FakeExecutor(creates={"out.md": "# r"})
    orch, _, git, _, made, _, _ = build(
        tmp_path,
        responses=[audit_block(), "PLACEHOLDER", stop_block()],
        executor=executor,
    )

    def responder(client):
        response = approval("commit", paths=("out.md",))(client)
        git.head = "f" * 40  # tree changes after review, before execution
        return response

    made[0].responses[1] = responder
    assert orch.run() == Phase.STOPPED.value
    assert git.commits == []
    assert "review_mismatch:head_moved" in made[0].submitted[2][1]


def test_push_approval_stamps_new_head_after_commit(tmp_path):
    orch, _, git, _, clients, _, _ = audit_then_commit(
        tmp_path,
        approval("commit", paths=("out.md",)),
        extra_responses=[approval("push"), stop_block()],
    )
    assert orch.run() == Phase.STOPPED.value
    assert git.pushes == 1
    assert extract_stamp(clients[0].submitted[2][1])["head_sha"] == "c" * 40


def test_recovery_reverifies_stamp_from_saved_response(tmp_path):
    state = LoopState.new(URL)
    state.iteration = 3
    state.phase = Phase.EXECUTING.value
    raw = block(
        {
            "version": 3,
            "decision": "commit",
            "reason": "approved",
            "commit": {"message": "docs: audit", "paths": ["out.md"]},
            "reviewed": {
                "request_id": "alr-crash-0003",
                "head_sha": "a" * 40,
                "report_sha256": "b" * 64,
            },
        }
    )
    state.last_response = LastResponse(
        request_id="alr-crash-0003",
        raw=raw,
        received_at="t",
        head_sha="a" * 40,
        base_sha="(none)",
        report_sha256="b" * 64,
    )
    orch, _, git, _, _, _, manifest_store = build(
        tmp_path, clients=[FakeClient()], state=state
    )
    # Recreate the pre-crash manifest: the audit had created out.md.
    from autoloop.manifest import ChangeManifest

    (git.repo_root / "out.md").write_text("# r", encoding="utf-8")
    git.dirty.append("?? out.md")
    manifest = ChangeManifest.begin("audit-i0003", "audit", git)
    manifest.baseline = {"x.py": manifest.baseline["x.py"]}  # out.md is task-created
    manifest.finish(
        {
            "x.py": manifest.baseline["x.py"],
            "out.md": hashlib.sha256(b"# r").hexdigest(),
        }
    )
    manifest_store.save(manifest)
    orch.state.last_manifest_id = "audit-i0003"
    orch.run(max_steps=1)
    assert git.commits == [("docs: audit", ("out.md",))]


# ---- context ----------------------------------------------------------------


def test_prompt_carries_full_context_block(tmp_path):
    orch, _, _, _, clients, _, _ = build(
        tmp_path,
        responses=[plan_block("t1"), stop_block()],
        tasks=[ready_task("t0")],
    )
    orch.run()
    first, second = clients[0].submitted[0][1], clients[0].submitted[1][1]
    for label in (
        "CONTEXT",
        "request_id:",
        "timestamp:",
        "head_sha:",
        "base_sha:",
        "report_sha256:",
        "branch: feature/x",
        "changed_files: x.py",
        "previous_decision: (none)",
        "validation: (none)",
        "roadmap:",
    ):
        assert label in first, label
    assert "previous_decision: plan" in second
    assert "next ready: t0" in second


# ---- contract violations ----------------------------------------------------


def test_malformed_response_triggers_corrective_reprompt(tmp_path):
    orch, _, _, executor, clients, _, _ = build(
        tmp_path, responses=["Sounds good.", audit_block()]
    )
    outcome = orch.run(max_steps=8)
    assert outcome == Phase.READY.value
    prompts = [p for _, p in clients[0].submitted]
    assert "contract_violation" in prompts[1]
    assert len(executor.calls) == 1
    assert orch.state.parse_retries == 0


def test_parse_budget_exhaustion_parks_loop(tmp_path):
    orch, _, _, _, _, _, _ = build(
        tmp_path,
        responses=["not json", "still not json"],
        policy=PolicyConfig(max_parse_retries=1),
    )
    assert orch.run() == Phase.NEEDS_USER.value
    assert "malformed" in orch.state.question


# ---- terminal decisions -----------------------------------------------------


def test_stop_decision_ends_loop(tmp_path):
    orch, _, _, executor, _, _, _ = build(tmp_path, responses=[stop_block()])
    assert orch.run() == Phase.STOPPED.value
    assert orch.state.stop_reason == "all done"
    assert executor.calls == []


def test_ask_user_parks_loop(tmp_path):
    orch, _, _, _, _, _, _ = build(tmp_path, responses=[ask_user_block("which DB?")])
    assert orch.run() == Phase.NEEDS_USER.value
    assert orch.state.question == "which DB?"
    assert orch.state.resume_phase is None


# ---- policy + git failures --------------------------------------------------


def test_denied_push_is_reported_not_executed(tmp_path):
    orch, _, git, _, clients, _, _ = build(
        tmp_path, responses=[approval("push"), stop_block()], branch="main"
    )
    assert orch.run() == Phase.STOPPED.value
    assert git.pushes == 0
    assert "policy_denied" in clients[0].submitted[1][1]


def test_git_failure_reported_to_chatgpt(tmp_path):
    orch, _, git, _, clients, _, _ = audit_then_commit(
        tmp_path, approval("commit", paths=("out.md",)), extra_responses=[stop_block()]
    )
    git.commit_error = GitCommandError("index locked")
    assert orch.run() == Phase.STOPPED.value
    reprompt = clients[0].submitted[2][1]
    assert "git_failure" in reprompt
    assert "index locked" in reprompt


def test_git_failure_in_ready_preserves_outbox(tmp_path):
    orch, _, git, _, _, _, _ = build(tmp_path, clients=[FakeClient()])
    git_error = GitCommandError("git binary missing")

    def broken_head():
        raise git_error

    git.head_sha = broken_head
    assert orch.run() == Phase.NEEDS_USER.value
    assert orch.state.resume_phase == Phase.READY.value
    assert orch.state.outbox == "kickoff report"


# ---- browser failures -------------------------------------------------------


def test_browser_failure_reconnects_and_retries(tmp_path):
    first = FakeClient()
    first.submit_errors = [SessionLostError("browser restarted")]
    second = FakeClient(responses=[stop_block()])
    orch, _, _, _, _, _, _ = build(tmp_path, clients=[first, second])
    assert orch.run() == Phase.STOPPED.value
    assert first.closed
    assert len(second.submitted) == 1


def test_browser_failure_budget_leads_to_failed(tmp_path):
    clients = []
    for _ in range(3):
        c = FakeClient()
        c.submit_errors = [SessionLostError("still down")]
        clients.append(c)
    orch, _, _, _, _, _, _ = build(
        tmp_path, clients=clients, policy=PolicyConfig(max_consecutive_failures=1)
    )
    assert orch.run() == Phase.FAILED.value
    assert orch.state.resume_phase == Phase.SUBMITTING.value


def test_login_expiry_parks_with_resume_phase(tmp_path):
    client = FakeClient()
    client.attach_errors = [LoginExpiredError("ChatGPT session is logged out")]
    orch, _, _, _, _, _, _ = build(tmp_path, clients=[client])
    assert orch.run() == Phase.NEEDS_USER.value
    assert orch.state.resume_phase == Phase.SUBMITTING.value


# ---- crash recovery ---------------------------------------------------------


def test_recovery_never_resubmits_a_submitted_request(tmp_path):
    state = LoopState.new(URL)
    state.iteration = 1
    state.phase = Phase.SUBMITTING.value
    state.pending_request = PendingRequest(
        request_id="alr-crash-0001", payload="payload", prompt="THE PROMPT", submitted=False
    )
    client = FakeClient(responses=[stop_block()])
    client.persisted.add("alr-crash-0001")
    orch, _, _, _, _, _, _ = build(tmp_path, clients=[client], state=state)
    assert orch.run() == Phase.STOPPED.value
    assert client.submitted == []


def test_recovery_resubmits_stored_prompt_byte_identical(tmp_path):
    state = LoopState.new(URL)
    state.iteration = 1
    state.phase = Phase.SUBMITTING.value
    state.pending_request = PendingRequest(
        request_id="alr-crash-0001",
        payload="payload",
        prompt="EXACT STORED PROMPT alr-crash-0001",
        submitted=False,
    )
    client = FakeClient(responses=[stop_block()])
    orch, _, _, _, _, _, _ = build(tmp_path, clients=[client], state=state)
    orch.run()
    assert client.submitted == [("alr-crash-0001", "EXACT STORED PROMPT alr-crash-0001")]


# ---- budgets & control ------------------------------------------------------


def test_iteration_budget_parks_loop_with_outbox_intact(tmp_path):
    orch, _, _, _, _, _, _ = build(
        tmp_path,
        responses=[audit_block()],
        policy=PolicyConfig(max_iterations=1),
    )
    assert orch.run() == Phase.NEEDS_USER.value
    assert orch.state.resume_phase == Phase.READY.value
    assert orch.state.outbox is not None
    assert "iteration budget" in orch.state.question


def test_pause_file_stops_before_next_step(tmp_path):
    orch, _, _, _, _, _, _ = build(tmp_path, responses=[stop_block()])
    orch._config.pause_file.parent.mkdir(parents=True, exist_ok=True)
    orch._config.pause_file.touch()
    assert orch.run() == "paused"
    assert orch.state.phase == Phase.READY.value


def test_ready_without_outbox_is_a_state_error(tmp_path):
    state = LoopState.new(URL)
    orch, _, _, _, _, _, _ = build(tmp_path, clients=[FakeClient()], state=state)
    with pytest.raises(StateError):
        orch.run(max_steps=1)


def test_state_persists_across_reload(tmp_path):
    orch, store, _, _, _, _, _ = build(tmp_path, responses=[audit_block()])
    orch.run(max_steps=4)
    reloaded = store.load()
    assert reloaded.phase == Phase.READY.value
    assert reloaded.iteration == 1
    assert reloaded.last_decision == "audit"
    assert reloaded.last_manifest_id == orch.state.last_manifest_id


# ---- submission confirmation & ambiguity (Phase 3.1 transport repair) -------


def test_submitting_reconciles_before_sending(tmp_path):
    """Persisted history — not the live DOM — decides whether to send."""
    orch, _, _, _, clients, _, _ = build(tmp_path, responses=[stop_block()])
    orch.run()
    client = clients[0]
    # exactly one reconciliation before the single send
    assert client.reconcile_calls == [client.submitted[0][0]]
    assert len(client.submitted) == 1


def test_awaiting_never_reconciles(tmp_path):
    """A reload during awaiting would destroy a streaming answer."""
    orch, _, _, _, clients, _, _ = build(tmp_path, responses=[stop_block()])
    orch.run()
    # one reconcile total (from submitting), none from awaiting
    assert len(clients[0].reconcile_calls) == 1


def test_unconfirmed_submission_parks_and_never_resends(tmp_path):
    client = FakeClient(submit_result=SubmitResult.UNCONFIRMED)
    orch, store, _, _, _, _, _ = build(tmp_path, clients=[client])
    assert orch.run() == Phase.NEEDS_USER.value
    # one send attempt only — the ambiguity must not trigger another
    assert len(client.submitted) == 1
    assert orch.state.resume_phase == Phase.SUBMISSION_UNCONFIRMED.value
    assert "AMBIGUOUS" in orch.state.question
    assert "will not resend on its own" in orch.state.question
    req = orch.state.pending_request
    assert req.send_attempted is True
    assert req.submitted is False
    # the request id and prompt survive for the operator / a later resubmit
    reloaded = store.load()
    assert reloaded.pending_request.request_id == req.request_id
    assert reloaded.pending_request.prompt == req.prompt


def test_unconfirmed_then_late_persistence_resolves_to_awaiting(tmp_path):
    """The backend accepted it after all: reconciliation finds it and the loop
    continues without resending."""
    client = FakeClient(responses=[stop_block()], submit_result=SubmitResult.UNCONFIRMED)
    orch, _, _, _, _, _, _ = build(tmp_path, clients=[client])
    orch.run(max_steps=2)  # ready -> submitting (unconfirmed)
    assert orch.state.phase == Phase.SUBMISSION_UNCONFIRMED.value
    # message shows up in persisted history before the reconciliation step
    client.persisted.add(orch.state.pending_request.request_id)
    assert orch.run() == Phase.STOPPED.value
    assert len(client.submitted) == 1  # never sent twice


def test_resumed_submission_unconfirmed_reconciles_before_acting(tmp_path):
    """Crash recovery straight into the ambiguous phase: reconcile first."""
    state = LoopState.new(URL)
    state.iteration = 1
    state.phase = Phase.SUBMISSION_UNCONFIRMED.value
    state.pending_request = PendingRequest(
        request_id="alr-crash-0001",
        payload="p",
        prompt="THE PROMPT",
        send_attempted=True,
    )
    client = FakeClient(responses=[stop_block()])
    client.persisted.add("alr-crash-0001")  # it did land
    orch, _, _, _, _, _, _ = build(tmp_path, clients=[client], state=state)
    assert orch.run() == Phase.STOPPED.value
    assert client.reconcile_calls == ["alr-crash-0001"]
    assert client.submitted == []  # reconciliation replaced the resend


def test_attempted_send_is_never_auto_resent_after_reentering_submitting(tmp_path):
    """Even if the machine re-enters `submitting`, a prior send attempt blocks
    an automatic resend — only an operator `--resubmit` may clear it."""
    state = LoopState.new(URL)
    state.iteration = 1
    state.phase = Phase.SUBMITTING.value
    state.pending_request = PendingRequest(
        request_id="alr-crash-0002",
        payload="p",
        prompt="THE PROMPT",
        send_attempted=True,
    )
    client = FakeClient()  # not persisted -> reconcile fails
    orch, _, _, _, _, _, _ = build(tmp_path, clients=[client], state=state)
    assert orch.run() == Phase.NEEDS_USER.value
    assert client.submitted == []
    assert client.reconcile_calls == ["alr-crash-0002"]
    assert "AMBIGUOUS" in orch.state.question


# ---- exactly-once: the durable send marker ---------------------------------


def test_send_marker_is_persisted_before_the_click(tmp_path):
    """A crash between clicking Send and observing acceptance must not lose the
    fact that a send happened — otherwise recovery double-posts."""
    seen = {}

    class RecordingClient(FakeClient):
        def __init__(self, store, **kw):
            super().__init__(**kw)
            self._store = store

        def submit(self, request_id, prompt):
            # What is ALREADY on disk at the moment the transport takes over?
            seen["persisted_before_submit"] = (
                self._store.load().pending_request.send_attempted
            )
            return super().submit(request_id, prompt)

    config_store = StateStore(tmp_path / ".al" / "state.json")
    client = RecordingClient(config_store, responses=[stop_block()])
    orch, _, _, _, _, _, _ = build(tmp_path, clients=[client])
    orch.run()
    assert seen["persisted_before_submit"] is True


def test_login_expiry_after_the_click_never_resends(tmp_path):
    """The live-realistic hole: submit() raises AFTER Send was clicked. The
    message may have been accepted, so recovery must reconcile, not resend."""

    class ClickThenExplode(FakeClient):
        def submit(self, request_id, prompt):
            self.submitted.append((request_id, prompt))
            self.send_attempted = True  # Send WAS clicked...
            raise LoginExpiredError("logged out right after clicking send")

    first = ClickThenExplode()
    orch, store, _, _, _, _, _ = build(tmp_path, clients=[first])
    assert orch.run() == Phase.NEEDS_USER.value
    saved = store.load()
    assert saved.pending_request.send_attempted is True  # durably remembered
    assert saved.resume_phase == Phase.SUBMITTING.value

    # Operator logs back in and retries. The message did NOT persist, so the
    # loop must PARK, not post a second copy.
    saved.phase = saved.resume_phase
    saved.resume_phase = None
    store.save(saved)
    second = FakeClient()  # reconcile finds nothing
    orch2, _, _, _, _, _, _ = build(tmp_path, clients=[second], state=saved)
    assert orch2.run() == Phase.NEEDS_USER.value
    assert second.submitted == []  # <- the duplicate that must never happen
    assert second.reconcile_calls == [saved.pending_request.request_id]
    assert "AMBIGUOUS" in orch2.state.question


def test_provable_no_send_clears_the_marker_so_retry_may_submit(tmp_path):
    """If the composer/Send never accepted the input, nothing was sent — that
    is unambiguous, so a retry must be allowed to submit normally."""

    class NeverSent(FakeClient):
        def submit(self, request_id, prompt):
            # send_attempted stays False: the click provably never happened.
            raise SubmissionError("Send control never became enabled")

    first = NeverSent()
    orch, store, _, _, _, _, _ = build(
        tmp_path, clients=[first], policy=PolicyConfig(max_consecutive_failures=0)
    )
    assert orch.run() == Phase.FAILED.value
    saved = store.load()
    assert saved.pending_request.send_attempted is False  # marker cleared

    saved.phase = saved.resume_phase
    saved.resume_phase = None
    store.save(saved)
    second = FakeClient(responses=[stop_block()])
    orch2, _, _, _, _, _, _ = build(tmp_path, clients=[second], state=saved)
    assert orch2.run() == Phase.STOPPED.value
    assert len(second.submitted) == 1  # allowed to send, exactly once


def test_exactly_once_across_restart_when_the_message_did_land(tmp_path):
    """Same crash, but the message DID persist: recovery adopts it and awaits —
    still exactly one submission in the conversation."""

    class ClickThenExplode(FakeClient):
        def submit(self, request_id, prompt):
            self.submitted.append((request_id, prompt))
            self.send_attempted = True
            raise SessionLostError("browser died after clicking send")

    first = ClickThenExplode()
    orch, store, _, _, _, _, _ = build(
        tmp_path, clients=[first], policy=PolicyConfig(max_consecutive_failures=0)
    )
    assert orch.run() == Phase.FAILED.value
    saved = store.load()
    request_id = saved.pending_request.request_id
    saved.phase = saved.resume_phase
    saved.resume_phase = None
    store.save(saved)

    # Fresh process, fresh client: the request is in persisted history.
    second = FakeClient(responses=[stop_block()])
    second.persisted.add(request_id)
    orch2, _, _, _, _, _, _ = build(tmp_path, clients=[second], state=saved)
    assert orch2.run() == Phase.STOPPED.value
    assert second.submitted == []  # adopted, never re-sent
    assert second.reconcile_calls == [request_id]


def test_await_timeout_exits_with_recoverable_state(tmp_path):
    """A response timeout must fail cleanly and leave a resumable phase."""

    class TimesOut(FakeClient):
        def await_response(self, request_id):
            raise ResponseTimeoutError("no assistant response began within 90.0s")

    orch, store, _, _, _, _, _ = build(
        tmp_path,
        clients=[TimesOut(), TimesOut()],
        policy=PolicyConfig(max_consecutive_failures=1),
    )
    assert orch.run() == Phase.FAILED.value
    saved = store.load()
    assert saved.resume_phase == Phase.AWAITING.value  # recoverable via --retry
    assert "no assistant response" in saved.stop_reason
    assert saved.pending_request is not None  # request id + prompt preserved


# ---- adopted manifests end to end ------------------------------------------


def adopted_approval(paths=("out.md",), message="docs: adopted"):
    """A stamped commit approval for an adopted manifest."""
    def responder(client):
        return block({
            "version": 3,
            "decision": "commit",
            "reason": "approved",
            "commit": {"message": message, "paths": list(paths)},
            "reviewed": extract_stamp(client.submitted[-1][1]),
        })
    return responder


def build_with_adoption(tmp_path, paths, responses, payload_includes_block=True,
                        manifest_id="adopt-1"):
    """Wire a session whose current manifest is an ADOPTED one, as the future
    precommit-review workflow will."""
    from autoloop.manifest import ChangeManifest, render_adoption_block

    orch, store, git, executor, clients, registry, manifest_store = build(
        tmp_path, responses=responses
    )
    for rel in paths:                      # make the paths genuinely dirty
        target = git.repo_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"content of {rel}\n", encoding="utf-8")
        line = f"?? {rel}"
        if line not in git.dirty:
            git.dirty.append(line)
    manifest = ChangeManifest.adopt(manifest_id, {p: "100644" for p in paths}, git)
    manifest_store.save(manifest)
    orch.state.last_manifest_id = manifest_id
    block_text = render_adoption_block(manifest)
    orch.state.outbox = (
        "PRE-COMMIT REVIEW PACKET\n\n" + (block_text if payload_includes_block else "(table omitted)")
    )
    store.save(orch.state)
    return orch, store, git, clients, manifest_store, manifest


def test_adopted_manifest_commit_succeeds_when_content_matches(tmp_path):
    orch, _, git, clients, store, manifest = build_with_adoption(
        tmp_path, ["out.md"], responses=[adopted_approval()]
    )
    orch.run(max_steps=4)
    assert git.commits == [("docs: adopted", ("out.md",))]
    # the hash table was bound to the reviewed report
    reloaded = store.load("adopt-1")
    assert reloaded.presented_report_sha256 is not None
    assert reloaded.presented_report_sha256 in clients[0].submitted[0][1]


def test_payload_without_the_adoption_block_binds_nothing_and_cannot_commit(tmp_path):
    """If the packet omits the hash table, report_sha256 does not cover it, so
    the manifest is never bound and no approval answering that report can
    commit. The request is still sent — refusing to send would deadlock error
    reporting — but the commit gate refuses."""
    orch, _, git, clients, store, _ = build_with_adoption(
        tmp_path, ["out.md"],
        responses=[adopted_approval(), stop_block()],
        payload_includes_block=False,
    )
    assert orch.run() == Phase.STOPPED.value
    assert len(clients[0].submitted) >= 1              # it WAS asked
    assert store.load("adopt-1").presented_report_sha256 is None   # nothing bound
    assert git.commits == []                           # and nothing committed
    assert "never presented for review" in clients[0].submitted[1][1]


def test_one_byte_change_after_approval_refuses_the_adopted_commit(tmp_path):
    def approve_then_mutate(client):
        response = adopted_approval()(client)
        target = git_ref["git"].repo_root / "out.md"
        target.write_text(target.read_text() + " ", encoding="utf-8")   # one byte
        return response

    git_ref = {}
    orch, _, git, clients, _, _ = build_with_adoption(
        tmp_path, ["out.md"], responses=[approve_then_mutate, stop_block()]
    )
    git_ref["git"] = git
    assert orch.run() == Phase.STOPPED.value
    assert git.commits == []
    reprompt = clients[0].submitted[1][1]
    assert "changed after approval" in reprompt
    assert "content of out.md" not in reprompt      # never echoes file content


def test_unapproved_path_cannot_be_committed_from_an_adopted_manifest(tmp_path):
    orch, _, git, clients, _, _ = build_with_adoption(
        tmp_path, ["out.md"],
        responses=[adopted_approval(paths=("out.md", "secret.md")), stop_block()],
    )
    assert orch.run() == Phase.STOPPED.value
    assert git.commits == []
    assert "not in adopted manifest" in clients[0].submitted[1][1]


def test_stale_approval_cannot_authorize_an_adoption(tmp_path):
    """An approval bound to a different report must not commit this table."""
    orch, _, git, clients, store, _ = build_with_adoption(
        tmp_path, ["out.md"], responses=[adopted_approval(), stop_block()]
    )
    # after the request is sent, rebind the manifest to some other report
    orch.run(max_steps=2)
    manifest = store.load("adopt-1")
    manifest.presented_report_sha256 = "a" * 64
    store.save(manifest)
    assert orch.run() == Phase.STOPPED.value
    assert git.commits == []
    assert "stale approval" in clients[0].submitted[1][1]


def test_tree_verification_aborts_before_the_commit_is_created(tmp_path):
    """TOCTOU: content changing before staging must not be committed — the
    candidate tree is verified before any commit object exists."""
    orch, _, git, clients, _, _ = build_with_adoption(
        tmp_path, ["out.md"], responses=[adopted_approval(), stop_block()]
    )

    real_commit = git.commit_adopted

    def commit_with_mutation(message, paths, verify_tree):
        target = git.repo_root / "out.md"
        target.write_text("mutated between check and stage\n", encoding="utf-8")
        return real_commit(message, paths, verify_tree)

    git.commit_adopted = commit_with_mutation
    assert orch.run() == Phase.STOPPED.value
    assert git.commits == []                       # no commit was created
    assert "tree content does not match" in clients[0].submitted[1][1]


def test_executor_manifest_flow_is_unaffected_by_adoption(tmp_path):
    """The original provenance path still works, and never sees adoption."""
    executor = FakeExecutor(creates={"out.md": "# report"})
    orch, _, git, _, clients, _, store = build(
        tmp_path, responses=[audit_block(), approval("commit", paths=("out.md",))],
        executor=executor,
    )
    orch.run(max_steps=8)
    assert git.commits == [("docs: audit", ("out.md",))]
    manifest = store.load(orch.state.last_manifest_id)
    assert not manifest.is_adopted()
    assert manifest.presented_report_sha256 is None   # binding is adoption-only


def test_adoption_grants_no_push_authorization(tmp_path):
    """Adoption authorizes content, never publication."""
    orch, _, git, clients, _, _ = build_with_adoption(
        tmp_path, ["out.md"],
        responses=[adopted_approval(), approval("push"), stop_block()],
    )
    orch._policy = PolicyEngine(PolicyConfig(allow_push=False))
    assert orch.run() == Phase.STOPPED.value
    assert len(git.commits) == 1
    assert git.pushes == 0
    assert "push_disabled" in clients[0].submitted[2][1] or "policy_denied" in clients[0].submitted[2][1]


def test_wired_verification_reads_the_committed_tree_not_the_working_tree(tmp_path):
    """End-to-end swap-and-restore through the orchestrator: the index receives
    altered bytes, the working tree is restored before the hook runs, and the
    commit must still be refused."""
    orch, _, git, clients, _, _ = build_with_adoption(
        tmp_path, ["out.md"], responses=[adopted_approval(), stop_block()]
    )
    target = git.repo_root / "out.md"
    approved = target.read_text()
    git.stage_hook = lambda: target.write_text("MALICIOUS PAYLOAD\n")
    git.restore_hook = lambda: target.write_text(approved)

    assert orch.run() == Phase.STOPPED.value
    assert git.commits == []                       # nothing committed
    assert target.read_text() == approved          # worktree looks innocent
    reprompt = clients[0].submitted[1][1]
    assert "tree content does not match" in reprompt
    assert "MALICIOUS" not in reprompt             # digests only, never content
