"""Orchestrator state machine with fake conversation / git / executor:
audit flow, phase gating, review-integrity verification, terminal decisions,
corrective re-prompts, budgets, crash recovery, duplicate-submission
protection, pause.

Two Orchestrator-construction styles:

* `build()` — a pure in-memory `FakeGit` "main checkout". Used by every test
  that never reaches `_dispatch_executor` (parsing, terminal decisions,
  browser transport/submission machinery, budgets, most policy denials —
  `FakeGit`'s directory is not a real git repository, so it cannot back the
  real `WorkerRepoManager.create` the produce-then-review path needs).
* `build_postcommit()` — a REAL throwaway git repo plus a real
  `WorkerRepoManager`/`TaskExecutionStore`/`IntentStore`, for the smaller set
  of tests that dispatch `audit`/`implement`/`revise` end to end (since
  2026-07-30 the ONLY dispatch path — see docs/SECURITY.md S21)."""

import hashlib
import json
import re
import subprocess
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
from autoloop.git_gateway import GitGateway
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
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import IntentStore, TaskExecutionStore

URL = "https://chatgpt.com/c/test-conversation"


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
    return f"Reasoning...\n```json\n{json.dumps(obj)}\n```"


def plan_block(*ids, approved_paths=("docs/AUDIT_2026-07-29.md",)):
    # `approved_paths` defaults to match `build_postcommit`'s default
    # `executor_files` — every task in the batch gets the same declared
    # scope, which is harmless for ids that are never actually implemented
    # in a given test and correct for the one (usually "t1") that is.
    tasks = [
        {
            "id": tid,
            "title": f"Title {tid}",
            "description": "desc",
            "approved_paths": list(approved_paths),
        }
        for tid in ids
    ]
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
        self.push_exact_calls: list[tuple[str, str, str]] = []
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

    def push_exact(self, remote, sha, dest_ref, protected_refs, expected_url=None, env_snapshot=None):
        if self.push_error:
            raise self.push_error
        branch = dest_ref[len("refs/heads/"):] if dest_ref.startswith("refs/heads/") else dest_ref
        if branch in set(protected_refs) or dest_ref in set(protected_refs):
            raise GitCommandError(f"push_exact refuses protected ref {dest_ref!r}")
        self.pushes += 1
        self.push_exact_calls.append((remote, sha, dest_ref))
        return sha

    def remote_ref_sha(self, remote, dest_ref):
        # No remote state is modelled beyond `push_exact_calls`; treat
        # nothing as ever already landed so callers that pre-check idempotency
        # always fall through to `push_exact` in these tests.
        return ""


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


def ready_task(tid="t1", approved_paths=("docs/AUDIT_2026-07-29.md",)):
    # Default matches `build_postcommit`'s own default `executor_files` below
    # — most callers here never override either, so the pair stays in sync.
    return Task(id=tid, title=f"Title {tid}", description="desc", approved_paths=tuple(approved_paths))


IMPLEMENT_ON = PolicyConfig(implement_enabled=True)


class PostcommitExecutor:
    """Test double standing in for the real TaskExecutor (`AuditExecutor` in
    production) on the produce-then-review path: writes `files` into the
    dispatched task's own worker repo — `workers_root / task.id`, the same
    layout `WorkerRepoManager` uses — and reports success with those paths as
    `changed_paths`. Mirrors `test_postcommit_flow.py`'s `WritingExecutor`.
    `task` is never `None` here: the orchestrator resolves the audit to its
    own synthetic `Task` before ever reaching an executor (`_resolve_audit_task`).

    Each call's content carries a round marker so a `revise` round (writing
    the SAME logical file again) produces a real diff rather than an empty,
    refused commit.
    """

    def __init__(self, workers_root, files, status="ok"):
        self.workers_root = Path(workers_root)
        self.files = dict(files)
        self.status = status
        self.calls: list[tuple] = []

    def execute(self, directive, task):
        self.calls.append((directive, task))
        wt = self.workers_root / task.id
        for rel, content in self.files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"{content}\n<!-- call {len(self.calls)} -->\n", encoding="utf-8")
        return ExecutionOutcome(
            status=self.status,
            summary="did it",
            details="details here",
            validation="ruff clean; 5 tests passed",
            changed_paths=tuple(self.files.keys()),
        )


def build_postcommit(
    tmp_path,
    responses=(),
    policy=None,
    clients=None,
    state=None,
    tasks=(),
    executor_files=None,
    executor_status="ok",
):
    """Real-git-backed Orchestrator construction for the smaller set of tests
    that dispatch `audit`/`implement`/`revise` end to end. `build()`'s
    `FakeGit` cannot back this: `WorkerRepoManager.create` runs a real `git
    fetch <repo_root> <sha>`, which needs `repo_root` to be an actual
    repository with that sha present."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")

    policy_config = policy or PolicyConfig()
    git = GitGateway(repo_root, PolicyEngine(policy_config))
    workers_root = tmp_path / "workers"
    worker_repos = WorkerRepoManager(workers_root, tmp_path / "worker-hooks")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    intent_store = IntentStore(tmp_path / "intents")

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=policy_config,
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

    executor = PostcommitExecutor(
        workers_root,
        executor_files
        if executor_files is not None
        else {"docs/AUDIT_2026-07-29.md": "# audit report"},
        status=executor_status,
    )
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
        worker_repos=worker_repos,
        execution_store=execution_store,
        intent_store=intent_store,
        validation_runner=ok_validation,
    )
    return orch, store, git, executor, made, registry, execution_store


# ---- audit flow -------------------------------------------------------------
#
# Since 2026-07-30 (docs/SECURITY.md S21: legacy authorize-then-produce/
# manifest path retired) the audit is dispatched as a task-shaped unit of
# work through the SAME produce-then-review commit path as `implement`/
# `revise` — its own isolated worker repo, committed automatically, reviewed
# from the immutable commit via a `postcommit_review` packet. These tests use
# `build_postcommit` (a real throwaway repo), not `build` (in-memory `FakeGit`
# cannot back the real `WorkerRepoManager.create` this path needs).


def test_audit_decision_executes_and_reports(tmp_path):
    orch, _, git, executor, clients, _, execution_store = build_postcommit(
        tmp_path, responses=[audit_block(scope="focus on SRS")]
    )
    orch.run(max_steps=4)
    directive, task = executor.calls[0]
    assert directive.decision is Decision.AUDIT
    # a synthetic per-run unit id, not the literal "audit" pseudo-task id —
    # see `Orchestrator._resolve_audit_task`.
    assert task is not None and task.id.startswith("audit-")
    assert "committed task" in orch.state.outbox  # postcommit_review template

    execution = execution_store.load(task.id)
    assert execution is not None
    assert execution.candidate_sha != ""
    assert execution.review_round == 1


def test_audit_revision_loop(tmp_path):
    orch, _, git, executor, _, _, execution_store = build_postcommit(
        tmp_path,
        responses=[audit_block(), revise_audit_block("check migrations"), stop_block()],
    )
    assert orch.run() == Phase.STOPPED.value
    decisions = [d.decision for d, _ in executor.calls]
    assert decisions == [Decision.AUDIT, Decision.REVISE]
    _, first_task = executor.calls[0]
    revise_directive, revise_task = executor.calls[1]
    # round 2 resumes round 1's own unit id — same worker repo, same branch,
    # same TaskExecution — never forks a second one.
    assert revise_task.id == first_task.id
    assert revise_directive.feedback == "check migrations"
    execution = execution_store.load(first_task.id)
    assert execution.review_round == 2


def test_two_audits_in_one_session_get_distinct_worker_units(tmp_path):
    """Continuous mode (scope item 4) drives audit -> push -> audit
    repeatedly within one long-lived process. `_resolve_audit_task` mints a
    fresh `audit-{iteration:04d}` unit id per AUDIT decision specifically to
    avoid two audits colliding on the literal `"audit"` pseudo-task id — the
    one case `test_audit_revision_loop` (REVISE, same unit id) does not
    exercise. This proves the second audit gets its own unit id, its own
    worker repo, and its own `TaskExecution` record rather than reusing or
    clobbering the first."""
    orch, _, git, executor, _, _, execution_store = build_postcommit(
        tmp_path,
        responses=[
            audit_block(),
            approval(decision="push"),
            audit_block(),
            stop_block(),
        ],
    )
    assert orch.run() == Phase.STOPPED.value
    decisions = [d.decision for d, _ in executor.calls]
    assert decisions == [Decision.AUDIT, Decision.AUDIT]
    _, first_task = executor.calls[0]
    _, second_task = executor.calls[1]
    assert first_task.id != second_task.id
    assert first_task.id.startswith("audit-") and second_task.id.startswith("audit-")

    first_execution = execution_store.load(first_task.id)
    second_execution = execution_store.load(second_task.id)
    assert first_execution is not None and second_execution is not None
    assert first_execution.candidate_sha != second_execution.candidate_sha
    # each unit got its own isolated worker repo on disk
    workers_root = git.repo_root.parent / "workers"
    assert (workers_root / first_task.id).is_dir()
    assert (workers_root / second_task.id).is_dir()


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
    orch, _, git, executor, _, _, execution_store = build_postcommit(
        tmp_path, clients=[FakeClient()], state=state
    )
    orch.run(max_steps=1)
    assert len(executor.calls) == 1
    assert orch.state.phase == Phase.READY.value
    # iteration=1 at dispatch time -> minted unit id "audit-0001"
    execution = execution_store.load("audit-0001")
    assert execution is not None
    assert execution.review_round == 1


def test_review_requests_are_serialised_through_one_client(tmp_path):
    """Item 5 of the v1 brief ("verify, do not expand"): review requests are
    serialised through the single conversation. `_get_client` memoizes on
    `self._client`, so a multi-round session (audit -> revise -> stop) never
    constructs a second client — there is exactly one conversation, and
    requests go through it strictly one at a time (the phase machine has no
    concurrent in-flight-request state to even represent a second one)."""
    client = FakeClient(responses=[audit_block(), revise_audit_block("x"), stop_block()])
    orch, _, _, _, _, _, _ = build_postcommit(tmp_path, clients=[client])

    calls = {"count": 0}
    real_factory = orch._client_factory

    def counting_factory():
        calls["count"] += 1
        return real_factory()

    orch._client_factory = counting_factory
    assert orch.run() == Phase.STOPPED.value
    assert calls["count"] == 1
    assert len(client.submitted) == 3  # three sequential rounds, one client


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
    orch, _, git, executor, _, registry, _ = build_postcommit(
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
    orch, _, git, executor, clients, registry, execution_store = build_postcommit(
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
    assert "committed task" in orch.state.outbox  # postcommit_review template
    # the re-run POST-commit validation summary (`_verify_committed`), not the
    # executor's own outcome.validation — see `orchestrator.py`.
    assert orch.state.last_validation == "ruff check .: PASS"
    assert execution_store.load("t1").review_round == 1


def test_plan_rejected_keeps_registry_and_reports(tmp_path):
    orch, _, _, _, clients, registry, _ = build(
        tmp_path,
        responses=[plan_block("t1"), stop_block()],
        tasks=[ready_task("t1")],  # duplicate id -> rejected
    )
    assert orch.run() == Phase.STOPPED.value
    assert len(registry.all_tasks()) == 1
    assert "plan_rejected" in clients[0].submitted[1][1]


# ---- change manifest + commit gate -------------------------------------
#
# Retired 2026-07-30 (docs/SECURITY.md S21: closed by retirement, not a fix).
# The authorize-then-produce/manifest commit gate this section tested
# (`self._git.commit(...)` behind `ChangeManifest`/`verify_commit`, dispatched
# by `_dispatch_git`'s COMMIT_DECISIONS branch) has no code left to test —
# `_dispatch_git` was removed along with its only caller. A `commit` /
# `commit_and_push` directive is now refused outright (see
# `test_denied_push_is_reported_not_executed`-style coverage and
# `_dispatch`'s `legacy_git_path_retired` denial); the produce-then-review
# equivalent of "does the executor's own commit land and get reported" is
# `test_audit_decision_executes_and_reports` / `test_plan_then_implement_flow`
# above.


# ---- review integrity -------------------------------------------------------
#
# The stamp/head checks below run in `_step_executing`, entirely BEFORE
# `_dispatch()` — they fire (or don't) independently of whether anything
# downstream would have handled the decision, so they still hold after S21's
# retirement. `test_push_approval_stamps_new_head_after_commit` and
# `test_recovery_reverifies_stamp_from_saved_response` (removed here) tested
# the retired legacy commit->push_approval->bare-push cycle specifically and
# have no equivalent left to adapt to.


def test_stale_stamp_rejected_and_nothing_committed(tmp_path):
    stale = {"request_id": "alr-old-0001", "head_sha": "f" * 40, "report_sha256": "0" * 64}
    orch, _, git, executor, clients, _, execution_store = build_postcommit(
        tmp_path,
        responses=[
            audit_block(),
            approval("commit", paths=("out.md",), stamp=stale),
            stop_block(),
        ],
    )
    assert orch.run() == Phase.STOPPED.value
    assert "review_mismatch:request_id" in clients[0].submitted[2][1]
    # the stale-stamped commit approval never reached dispatch — the audit's
    # own candidate is unaffected (still exactly one review round)
    task_id = executor.calls[0][1].id
    assert execution_store.load(task_id).review_round == 1


def test_head_moved_since_review_rejected(tmp_path):
    orch, _, git, executor, made, _, execution_store = build_postcommit(
        tmp_path,
        responses=[audit_block(), "PLACEHOLDER", stop_block()],
    )

    def responder(client):
        response = approval("commit", paths=("out.md",))(client)
        # Tree changes on the MAIN checkout after review, before execution —
        # `ctx.head_sha` (what the stamp echoes) is `self._git.head_sha()`,
        # the main checkout's, never the worker branch's.
        run_git(git.repo_root, "commit", "--allow-empty", "-q", "-m", "moved")
        return response

    made[0].responses[1] = responder
    assert orch.run() == Phase.STOPPED.value
    assert "review_mismatch:head_moved" in made[0].submitted[2][1]
    task_id = executor.calls[0][1].id
    assert execution_store.load(task_id).review_round == 1


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
    orch, _, _, executor, clients, _, _ = build_postcommit(
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
    orch, _, _, _, _, _, _ = build_postcommit(
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
    orch, store, _, _, _, _, _ = build_postcommit(tmp_path, responses=[audit_block()])
    orch.run(max_steps=4)
    reloaded = store.load()
    assert reloaded.phase == Phase.READY.value
    assert reloaded.iteration == 1
    assert reloaded.last_decision == "audit"
    # `last_manifest_id` belongs to the retired manifest path and is never
    # set anymore; `task_execution` (the produce-then-review record) is the
    # thing that must survive a reload now. (`allowed_paths` round-trips as a
    # JSON list rather than a tuple — compare the fields that matter instead
    # of the raw dicts.)
    assert reloaded.task_execution["task_id"] == orch.state.task_execution["task_id"]
    assert reloaded.task_execution["candidate_sha"] == orch.state.task_execution["candidate_sha"]
    assert reloaded.task_execution["candidate_sha"] != ""


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


# ---- adopted manifests -------------------------------------------------
#
# Removed 2026-07-30 (docs/SECURITY.md S21/S22). This section's 9 tests
# dispatched a stamped "commit" approval through `_dispatch_git`'s adopted
# branch (`ChangeManifest.adopt` + `GitGateway.commit_adopted`) end to end via
# the orchestrator — that branch, and `_dispatch_git` itself, were removed
# along with the legacy manifest commit gate. `commit_adopted` is sound (it
# is what closed the hook-rewrite hole S21 documents) and is kept, but it now
# has no production caller — see its own docstring in `git_gateway.py`.
#
# Coverage after removal: the unit-level content-binding behaviour these
# tests exercised through the orchestrator is still directly covered in
# `test_manifest.py` / `test_git_gateway.py`, which call `ChangeManifest.
# adopt` / `verify_commit` / `verify_tree_content` / `commit_adopted`
# themselves rather than through a live dispatch:
#   test_adopted_manifest_commit_succeeds_when_content_matches
#     -> test_manifest.py::test_exact_approved_content_and_mode_is_accepted
#   test_payload_without_the_adoption_block_binds_nothing_and_cannot_commit
#     -> no direct replacement; this was the ONLY test of the `_step_ready`
#        adoption-stamping wiring, which was removed with `_dispatch_git`
#        (nothing produces an adopted manifest to stamp anymore)
#   test_one_byte_change_after_approval_refuses_the_adopted_commit
#     -> test_manifest.py::test_tree_content_change_is_rejected
#   test_unapproved_path_cannot_be_committed_from_an_adopted_manifest
#     -> test_manifest.py::test_unapproved_path_cannot_be_added_to_an_adopted_commit
#   test_stale_approval_cannot_authorize_an_adoption
#     -> test_manifest.py::test_unpresented_adopted_manifest_is_refused
#        (same "presented_report_sha256 must match" gate, unit-level)
#   test_tree_verification_aborts_before_the_commit_is_created
#     -> test_git_gateway.py::test_index_mutation_after_write_tree_cannot_alter_the_commit
#   test_executor_manifest_flow_is_unaffected_by_adoption
#     -> retired outright: it asserted the legacy executor-manifest commit
#        (`self._git.commit(...)`) still worked, which is exactly what S21
#        retired
#   test_adoption_grants_no_push_authorization
#     -> policy.allow_push / protected-branch denial is still covered by
#        test_denied_push_is_reported_not_executed above; the "adoption
#        authorizes content, never publication" framing has no path left to
#        test now that adoption never reaches dispatch at all
#   test_wired_verification_reads_the_committed_tree_not_the_working_tree
#     -> test_git_gateway.py::test_index_mutation_after_write_tree_cannot_alter_the_commit
#        (same swap-and-restore-through-a-hook attack, pinned at the
#        GitGateway level instead of through the orchestrator)
