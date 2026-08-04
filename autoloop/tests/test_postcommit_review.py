"""Autoloop M1 pass 2b: the post-commit review packet, request binding,
push_exact routing, revise/round-cap handling, and terminal parking.

Real git throughout, matching `test_postcommit_flow.py`'s self-contained
style rather than importing its fixtures (this file duplicates the small
`run_git`/`gateway`/`install_hook`/`WritingExecutor`/`build_postcommit`
helpers on purpose — see `test_postcommit_primitives.py`'s docstring for why
that duplication, not a shared import, is this codebase's convention).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.packet import build_review_packet
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LastResponse, LoopState, Phase, StateStore
from autoloop.tasks import TRACKER_PATHS, Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import IntentStore, TaskExecutionStore
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/test-conversation"


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def gateway(root) -> GitGateway:
    return GitGateway(root, PolicyEngine(PolicyConfig()))


def make_bare(tmp_path, name="bare.git"):
    bare = tmp_path / name
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    return bare


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


class WritingExecutor:
    """Writes `files` into the worktree for `task.id` and reports success —
    same shape as `test_postcommit_flow.py`'s double, duplicated per this
    file's self-contained convention. `per_round_files`, when given, is a
    list consumed one entry per call (round 1 gets element 0, round 2 gets
    element 1, ...) so a test can make each review round touch a DIFFERENT
    path — the round>0 path-ownership regression needs exactly that.
    """

    def __init__(self, worktrees_root, files=None, per_round_files=None, status="ok"):
        self.worktrees_root = Path(worktrees_root)
        self.files = dict(files or {})
        self.per_round_files = list(per_round_files) if per_round_files else None
        self.status = status
        self.calls = 0

    def execute(self, directive, task):
        wt = self.worktrees_root / task.id
        if self.per_round_files is not None:
            files = self.per_round_files[self.calls]
        else:
            files = self.files
        self.calls += 1
        for rel, content in files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return ExecutionOutcome(
            status=self.status,
            summary=f"round {self.calls}",
            details="details",
            validation="ok",
            changed_paths=tuple(files.keys()),
        )


def worktree_git_for(worktrees: WorktreeManager, task_id: str) -> GitGateway:
    return GitGateway(worktrees.path_for(task_id), PolicyEngine(PolicyConfig()))


def build_postcommit(
    tmp_path,
    executor,
    task_id="t1",
    validation_runner=ok_validation,
    policy=None,
    approved_paths=None,
):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")

    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    worktrees = WorktreeManager(git, tmp_path / "worktrees")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    intent_store = IntentStore(tmp_path / "intents")

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=policy or PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    # See `test_postcommit_flow.py`'s `build_postcommit` for why this is
    # derived from the executor's own known files rather than left empty.
    if approved_paths is None:
        derived = set(getattr(executor, "files", {}) or {})
        for round_files in getattr(executor, "per_round_files", None) or ():
            derived |= set(round_files)
        approved_paths = tuple(sorted(derived))
    task = Task(id=task_id, title=f"Title {task_id}", description="desc", approved_paths=tuple(approved_paths))
    registry = TaskRegistry([task])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)
    manifest_store = ManifestStore(config.manifests_dir)

    def no_client():
        raise AssertionError("no browser client expected in this test")

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=manifest_store,
        worktrees=worktrees,
        execution_store=execution_store,
        intent_store=intent_store,
        validation_runner=validation_runner,
    )
    return orch, repo_root, worktrees, execution_store, intent_store, task


def implement(task_id="t1"):
    return Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task_id)


def revise(task_id="t1", feedback="please fix it"):
    return Directive(decision=Decision.REVISE, reason="not quite", task_id=task_id, feedback=feedback)


# =============================================================================
# FakeClient — only the tests driving a full orch.run() round trip need it.
# =============================================================================


def block(obj) -> str:
    return f"Reasoning...\n```json\n{json.dumps(obj)}\n```"


def stop_block():
    return block({"version": 3, "decision": "stop", "reason": "done"})


def extract_stamp(prompt: str) -> dict:
    return {
        "request_id": re.search(r"request_id: (\S+)", prompt).group(1),
        "head_sha": re.search(r"head_sha: (\S+)", prompt).group(1),
        "report_sha256": re.search(r"report_sha256: (\S+)", prompt).group(1),
    }


def push_approval_from_last_submitted(client):
    stamp = extract_stamp(client.submitted[-1][1])
    return block({"version": 3, "decision": "push", "reason": "approved", "reviewed": stamp})


class FakeClient:
    """Minimal browser-transport double — server truth (`persisted`) kept
    separate from what was sent, matching `test_orchestrator.py`'s fake."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.submitted: list[tuple[str, str]] = []
        self.persisted: set[str] = set()
        self.send_attempted = False

    def attach(self):
        pass

    def reconcile(self, request_id):
        return request_id in self.persisted

    def submit(self, request_id, prompt):
        self.send_attempted = True
        self.submitted.append((request_id, prompt))
        self.persisted.add(request_id)
        from autoloop.browser.chatgpt import SubmitResult

        return SubmitResult.CONFIRMED

    def await_response(self, request_id):
        entry = self.responses.pop(0)
        return entry(self) if callable(entry) else entry

    def close(self):
        pass


class RaisesIfCalled:
    def submit(self, *a, **kw):
        raise AssertionError("client.submit must not be called in this test")

    def attach(self):
        pass

    def reconcile(self, request_id):
        return False

    def await_response(self, request_id):  # pragma: no cover - never reached
        raise AssertionError("await_response must not be called in this test")

    def close(self):
        pass


def build_postcommit_with_client(tmp_path, executor, responses, task_id="t1", policy=None):
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor, task_id=task_id, policy=policy
    )
    client = FakeClient(responses=list(responses))
    remaining = [client]

    def factory():
        return remaining.pop(0) if remaining else client

    orch._client_factory = factory
    return orch, repo_root, worktrees, execution_store, intent_store, task, client


# =============================================================================
# 1. base/candidate sha are inside the HASHED body
# =============================================================================


def test_base_and_candidate_sha_are_inside_the_hashed_body(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    execution = execution_store.load(task.id)
    assert orch.state.phase == Phase.READY.value

    orch._step_ready()
    req = orch.state.pending_request
    assert req is not None
    assert req.postcommit is not None

    # Recompute the hash exactly the way `context.report_sha256` does, over
    # exactly the bytes it was computed over (`req.payload` == what was
    # `state.outbox` at send time) — not over `build_review_packet`'s return
    # value directly, and not over the rendered prompt (which also carries
    # the CONTEXT block and CONTRACT_INSTRUCTIONS).
    assert hashlib.sha256(req.payload.encode("utf-8")).hexdigest() == req.report_sha256
    assert execution.task_base_sha in req.payload
    assert execution.candidate_sha in req.payload
    assert task.id in req.payload
    assert execution.task_branch in req.payload

    assert req.postcommit.task_id == task.id
    assert req.postcommit.task_branch == execution.task_branch
    assert req.postcommit.base_sha == execution.task_base_sha
    assert req.postcommit.candidate_sha == execution.candidate_sha
    assert req.postcommit.packet_sha256 == req.report_sha256


# =============================================================================
# 2. packet bytes are unchanged by a later, unrelated local commit
# =============================================================================


def test_packet_bytes_unchanged_when_working_tree_changes_afterward(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    execution = execution_store.load(task.id)
    wt_git = worktree_git_for(worktrees, task.id)
    before = build_review_packet(execution, wt_git, task)

    (Path(execution.worktree_path) / "unrelated.txt").write_text("later human work\n")
    run_git(Path(execution.worktree_path), "add", "unrelated.txt")
    run_git(Path(execution.worktree_path), "commit", "-q", "-m", "later, unrelated")

    after = build_review_packet(execution, wt_git, task)
    assert after == before
    assert "unrelated.txt" not in after


# =============================================================================
# 3. approving candidate A cannot push candidate B
# =============================================================================


def test_approving_candidate_a_cannot_push_candidate_b(tmp_path):
    executor = WritingExecutor(
        tmp_path / "worktrees", per_round_files=[{"a.py": "one\n"}, {"a.py": "two\n"}]
    )
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    orch._step_ready()
    req_a = orch.state.pending_request
    resp_a = LastResponse(
        request_id=req_a.request_id,
        raw="{}",
        received_at="now",
        head_sha=req_a.head_sha,
        base_sha=req_a.base_sha,
        report_sha256=req_a.report_sha256,
        postcommit=req_a.postcommit,
    )
    candidate_a = req_a.postcommit.candidate_sha

    # Advance to round 2 — the task's CURRENT candidate is now B, not A.
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(revise(task.id))
    execution = execution_store.load(task.id)
    candidate_b = execution.candidate_sha
    assert candidate_b != candidate_a

    bare = make_bare(tmp_path)
    run_git(repo_root, "remote", "add", "origin", str(bare))

    directive = Directive(decision=Decision.PUSH, reason="approved")
    orch._dispatch_task_push(directive, resp_a)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "no longer this task's current candidate" in orch.state.question
    worktree_git = worktree_git_for(worktrees, task.id)
    assert worktree_git.remote_ref_sha("origin", f"refs/heads/{execution.task_branch}") == ""


# =============================================================================
# 4. revise keeps the original base and advances the candidate
# =============================================================================


def test_revise_keeps_original_base_and_advances_candidate(tmp_path):
    executor = WritingExecutor(
        tmp_path / "worktrees", per_round_files=[{"a.py": "one\n"}, {"a.py": "two\n"}]
    )
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    execution1 = execution_store.load(task.id)
    base0 = execution1.task_base_sha
    candidate_a = execution1.candidate_sha
    assert execution1.review_round == 1

    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(revise(task.id))
    execution2 = execution_store.load(task.id)

    assert execution2.task_base_sha == base0  # unchanged
    assert execution2.candidate_sha != candidate_a
    assert execution2.review_round == 2
    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.is_descendant(execution2.candidate_sha, base0)
    assert wt_git.commit_list(base0, execution2.candidate_sha)[0]["sha"] == candidate_a


# =============================================================================
# 5. a legitimate round-2 revision is NOT wrongly refused (path-ownership union)
# =============================================================================


def test_round_two_touching_a_different_path_is_not_wrongly_refused(tmp_path):
    executor = WritingExecutor(
        tmp_path / "worktrees",
        per_round_files=[{"a.py": "round one\n"}, {"b.py": "round two\n"}],
    )
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    assert orch.state.phase == Phase.READY.value  # round 1 passed review

    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(revise(task.id))

    # The regression: comparing the WHOLE base..candidate range against only
    # round 2's own changed_paths would flag "a.py" as outside the plan.
    assert orch.state.phase == Phase.READY.value
    assert "REFUSED" not in (orch.state.outbox or "")
    assert "POST-COMMIT REVIEW PACKET" in orch.state.outbox

    execution = execution_store.load(task.id)
    # The task's own scope PLUS the always-approved trackers: updating
    # docs/SUMMARY.md, TESTS.md, SECURITY.md and COMMON_ERRORS.md is a
    # CONDITION of doing the work (CLAUDE.md 12/14), so they are implicit
    # for every scoped task — see tasks.TRACKER_PATHS and AUTOLOOP.md 4f-bis.
    # Asserted against the constant rather than six literals, so widening
    # that list cannot silently drift from what this test expects.
    assert set(execution.allowed_paths) == {"a.py", "b.py"} | set(TRACKER_PATHS)
    wt_git = worktree_git_for(worktrees, task.id)
    touched = wt_git.commit_range_paths(execution.task_base_sha, execution.candidate_sha)
    assert touched == {"a.py", "b.py"}


# =============================================================================
# 6. a third review round parks and never reaches ChatGPT or the executor
# =============================================================================


def test_third_review_round_parks_and_never_submits(tmp_path):
    executor = WritingExecutor(
        tmp_path / "worktrees",
        per_round_files=[{"a.py": "one\n"}, {"a.py": "two\n"}, {"a.py": "three\n"}],
    )
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        # The cap is configurable now and defaults to UNLIMITED, so this
        # test pins the value it is actually about. What it verifies is
        # unchanged: with a cap of 2, a third round never reaches the
        # executor.
        tmp_path, executor,
        policy=PolicyConfig(implement_enabled=True, max_review_rounds=2),
    )
    orch._dispatch_executor(implement(task.id))
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(revise(task.id, feedback="round 2 feedback"))
    assert orch.state.phase == Phase.READY.value
    execution_after_2 = execution_store.load(task.id)
    assert execution_after_2.review_round == 2

    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(revise(task.id, feedback="one more time please"))

    assert executor.calls == 2  # the third round never reached the executor
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "round cap" in orch.state.question
    assert "one more time please" in orch.state.question
    assert "full diff" in orch.state.question
    assert "latest round diff" in orch.state.question
    execution = execution_store.load(task.id)
    assert execution.review_round == 2  # unchanged — no third round was attempted
    assert execution.candidate_sha == execution_after_2.candidate_sha


# =============================================================================
# 7. explicit-SHA push excludes a later local commit, through the orchestrator
# =============================================================================


def test_push_excludes_a_later_local_commit(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    orch._step_ready()
    req = orch.state.pending_request
    resp = LastResponse(
        request_id=req.request_id, raw="{}", received_at="now",
        head_sha=req.head_sha, base_sha=req.base_sha, report_sha256=req.report_sha256,
        postcommit=req.postcommit,
    )
    execution = execution_store.load(task.id)
    bare = make_bare(tmp_path)
    run_git(repo_root, "remote", "add", "origin", str(bare))

    orch._dispatch_task_push(Directive(decision=Decision.PUSH, reason="approved"), resp)
    assert orch.state.phase == Phase.READY.value

    wt_git = worktree_git_for(worktrees, task.id)
    dest_ref = f"refs/heads/{execution.task_branch}"
    assert wt_git.remote_ref_sha("origin", dest_ref) == execution.candidate_sha

    # A LATER local commit on the same branch must not have reached the remote.
    (Path(execution.worktree_path) / "a.py").write_text("later, unpushed\n")
    run_git(Path(execution.worktree_path), "commit", "-a", "-q", "-m", "later, unpushed")
    assert wt_git.remote_ref_sha("origin", dest_ref) == execution.candidate_sha


# =============================================================================
# 8. same-SHA push retry is idempotent
# =============================================================================


def test_same_sha_push_retry_is_idempotent(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    orch._step_ready()
    req = orch.state.pending_request
    resp = LastResponse(
        request_id=req.request_id, raw="{}", received_at="now",
        head_sha=req.head_sha, base_sha=req.base_sha, report_sha256=req.report_sha256,
        postcommit=req.postcommit,
    )
    execution = execution_store.load(task.id)
    bare = make_bare(tmp_path)
    run_git(repo_root, "remote", "add", "origin", str(bare))

    directive = Directive(decision=Decision.PUSH, reason="approved")
    orch._dispatch_task_push(directive, resp)
    assert orch.state.phase == Phase.READY.value

    orch.state.phase = Phase.EXECUTING.value  # simulate re-entry
    orch._dispatch_task_push(directive, resp)
    assert orch.state.phase == Phase.READY.value

    wt_git = worktree_git_for(worktrees, task.id)
    dest_ref = f"refs/heads/{execution.task_branch}"
    assert wt_git.remote_ref_sha("origin", dest_ref) == execution.candidate_sha


# =============================================================================
# 9. non-fast-forward remote movement refuses
# =============================================================================


def test_push_refuses_non_fast_forward_remote_movement(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    orch._step_ready()
    req = orch.state.pending_request
    resp = LastResponse(
        request_id=req.request_id, raw="{}", received_at="now",
        head_sha=req.head_sha, base_sha=req.base_sha, report_sha256=req.report_sha256,
        postcommit=req.postcommit,
    )
    execution = execution_store.load(task.id)
    wt_git = worktree_git_for(worktrees, task.id)
    dest_ref = f"refs/heads/{execution.task_branch}"

    bare = make_bare(tmp_path)
    run_git(repo_root, "remote", "add", "origin", str(bare))

    # A commit that is a SIBLING of the real candidate (same parent, same
    # tree as the candidate but a different commit object) — pushed to the
    # destination BEFORE the real candidate, so publishing the real candidate
    # afterwards is not a fast-forward.
    candidate_tree = wt_git.tree_of(execution.candidate_sha)
    divergent = wt_git.commit_tree(candidate_tree, execution.task_base_sha, "divergent")
    wt_git.push_exact("origin", divergent, dest_ref, ())
    assert wt_git.remote_ref_sha("origin", dest_ref) == divergent

    orch._dispatch_task_push(Directive(decision=Decision.PUSH, reason="approved"), resp)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "REFUSED" in orch.state.question
    assert wt_git.remote_ref_sha("origin", dest_ref) == divergent  # unchanged


# =============================================================================
# 10. protected destination refuses (policy layer, using the FIXED branch name)
# =============================================================================


def test_push_to_protected_task_branch_refused_by_policy(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    protected_policy = PolicyConfig(implement_enabled=True, protected_branches=("autoloop/t1",))
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor, policy=protected_policy
    )
    orch._dispatch_executor(implement(task.id))
    execution = execution_store.load(task.id)
    assert execution.task_branch == "autoloop/t1"

    orch._step_ready()
    req = orch.state.pending_request
    orch.state.phase = Phase.AWAITING.value
    orch.state.last_response = LastResponse(
        request_id=req.request_id, raw="{}", received_at="now",
        head_sha=req.head_sha, base_sha=req.base_sha, report_sha256=req.report_sha256,
        postcommit=req.postcommit,
    )
    orch.state.pending_request = None
    orch.state.phase = Phase.EXECUTING.value
    orch.state.last_response.raw = block(
        {
            "version": 3,
            "decision": "push",
            "reason": "approved",
            "reviewed": {
                "request_id": req.request_id,
                "head_sha": req.head_sha,
                "report_sha256": req.report_sha256,
            },
        }
    )

    orch._step_executing()

    assert orch.state.phase == Phase.READY.value  # re-prompted, not pushed
    # `authorize_directive` judged `resp.postcommit.task_branch`
    # ("autoloop/t1"), not the main checkout's branch ("main") — proving the
    # branch-name fix, since the main checkout is never on "autoloop/t1".
    assert "protected branch" in orch.state.outbox
    assert "autoloop/t1" in orch.state.outbox
    # No remote is even configured in this test — `_dispatch_task_push` (the
    # only path that could publish anything) was never reached, which is the
    # point; a `remote_ref_sha` probe here would just fail on the missing
    # remote rather than proving anything further.


def test_push_exact_own_protected_ref_check_is_independent_defense(tmp_path):
    """Even if policy somehow let a protected destination through,
    `push_exact`'s own protected-ref check (inside `_dispatch_task_push`,
    via `worktree_git.push_exact`) refuses independently."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    protected_policy = PolicyConfig(implement_enabled=True, protected_branches=("autoloop/t1",))
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor, policy=protected_policy
    )
    orch._dispatch_executor(implement(task.id))
    orch._step_ready()
    req = orch.state.pending_request
    resp = LastResponse(
        request_id=req.request_id, raw="{}", received_at="now",
        head_sha=req.head_sha, base_sha=req.base_sha, report_sha256=req.report_sha256,
        postcommit=req.postcommit,
    )
    execution = execution_store.load(task.id)
    bare = make_bare(tmp_path)
    run_git(repo_root, "remote", "add", "origin", str(bare))

    orch._dispatch_task_push(Directive(decision=Decision.PUSH, reason="approved"), resp)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "REFUSED" in orch.state.question
    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.remote_ref_sha("origin", f"refs/heads/{execution.task_branch}") == ""


# =============================================================================
# 11. allow_push=false refuses and parks without pushing
# =============================================================================


def test_allow_push_false_refuses_without_pushing(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    no_push_policy = PolicyConfig(implement_enabled=True, allow_push=False)
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor, policy=no_push_policy
    )
    orch._dispatch_executor(implement(task.id))
    orch._step_ready()
    req = orch.state.pending_request

    orch.state.pending_request = None
    orch.state.last_response = LastResponse(
        request_id=req.request_id, raw="{}", received_at="now",
        head_sha=req.head_sha, base_sha=req.base_sha, report_sha256=req.report_sha256,
        postcommit=req.postcommit,
    )
    orch.state.last_response.raw = block(
        {
            "version": 3,
            "decision": "push",
            "reason": "approved",
            "reviewed": {
                "request_id": req.request_id,
                "head_sha": req.head_sha,
                "report_sha256": req.report_sha256,
            },
        }
    )
    orch.state.phase = Phase.EXECUTING.value

    orch._step_executing()

    assert orch.state.phase == Phase.READY.value
    assert "pushes are disabled" in orch.state.outbox
    # No remote is configured in this test at all — `_dispatch_task_push`
    # was never reached, which is the point.


# =============================================================================
# 12. crash after push reconciles from the remote ref, never re-pushes
# =============================================================================


def test_crash_after_push_reconciles_without_repushing(tmp_path, monkeypatch):
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    orch._step_ready()
    req = orch.state.pending_request
    resp = LastResponse(
        request_id=req.request_id, raw="{}", received_at="now",
        head_sha=req.head_sha, base_sha=req.base_sha, report_sha256=req.report_sha256,
        postcommit=req.postcommit,
    )
    execution = execution_store.load(task.id)
    bare = make_bare(tmp_path)
    run_git(repo_root, "remote", "add", "origin", str(bare))

    # Push for real once — this is "before the crash".
    orch._dispatch_task_push(Directive(decision=Decision.PUSH, reason="approved"), resp)
    assert orch.state.phase == Phase.READY.value

    # "Crash": a brand-new orchestrator-side call re-dispatches the SAME
    # directive/response (state.task_execution reloaded fresh from disk, as
    # a real restart would do). `push_exact` must never be called again —
    # the pre-check against `remote_ref_sha` must short-circuit first.
    def fail_if_called(*a, **kw):
        raise AssertionError("push_exact must not be called on a same-sha retry")

    monkeypatch.setattr(GitGateway, "push_exact", fail_if_called)
    orch.state.phase = Phase.EXECUTING.value
    orch._dispatch_task_push(Directive(decision=Decision.PUSH, reason="approved"), resp)

    assert orch.state.phase == Phase.READY.value
    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.remote_ref_sha("origin", f"refs/heads/{execution.task_branch}") == (
        execution.candidate_sha
    )


# =============================================================================
# 13. a retry verifies prompt_sha256 before resending
# =============================================================================


def test_retry_verifies_prompt_sha256_before_resending(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    orch._step_ready()
    req = orch.state.pending_request
    assert req.prompt_sha256 == hashlib.sha256(req.prompt.encode("utf-8")).hexdigest()

    # Simulate on-disk corruption / manual tampering between a crash and a
    # retry: the prompt text changed but the stamped hash did not.
    req.prompt = req.prompt + "\nTAMPERED BY SOMETHING"

    orch._client_factory = lambda: RaisesIfCalled()
    orch._step_submitting()

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "prompt_sha256" in orch.state.question
    assert orch.state.resume_phase == Phase.SUBMITTING.value


# =============================================================================
# 14. no caller of the removed GitGateway.push() remains
# =============================================================================


def test_no_caller_of_removed_push_method_remains():
    assert not hasattr(GitGateway, "push")
    repo_root = Path(__file__).resolve().parents[2]
    pattern = re.compile(r"(?<![\w.])\.push\(")
    offenders = []
    for path in (repo_root / "autoloop").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if pattern.search(line) and ".push_exact(" not in line:
                offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert offenders == [], "\n".join(offenders)


# =============================================================================
# 15. an oversized review packet parks instead of propagating as a git error
# =============================================================================


def test_oversized_review_packet_parks_instead_of_raising(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    original_cap = GitGateway.RANGE_DIFF_MAX_BYTES
    GitGateway.RANGE_DIFF_MAX_BYTES = 10
    try:
        orch._dispatch_executor(implement(task.id))
    finally:
        GitGateway.RANGE_DIFF_MAX_BYTES = original_cap

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "review packet could not be built" in orch.state.question
    execution = execution_store.load(task.id)
    assert execution.candidate_sha != ""  # the commit exists — not rolled back
    # No review round was consumed: `review_round` counts REVIEWS, and no
    # packet ever reached ChatGPT. Charging this against the two-round budget
    # would burn it without a single review having happened.
    assert execution.review_round == 0


# =============================================================================
# 16/17/18. legacy push fall-through — RETIRED 2026-07-30 (docs/SECURITY.md
# S21: `_dispatch_git` removed along with the authorize-then-produce/manifest
# commit path it gated). The three tests that lived here
# (`test_parse_error_reprompt_does_not_leak_postcommit_binding_to_legacy_push`,
# `test_stale_audit_manifest_does_not_leak_postcommit_push_to_legacy_path`,
# `test_successful_postcommit_push_does_not_block_a_later_unrelated_audit_push`)
# each pinned a specific fail-closed guard INSIDE `_dispatch_git`'s
# PUSH_DECISIONS branch (checking `state.task_execution` against the
# response's postcommit binding before falling through to a bare
# `push_exact`). None of that code exists anymore: `commit` /
# `commit_and_push` / any `push` not bound to a produce-then-review
# candidate are now refused UNCONDITIONALLY, by `_dispatch`'s own
# `legacy_git_path_retired` policy denial, before any git write is even
# attempted — a strictly stronger guarantee than "refused in this one
# leak-shaped scenario". The audit-specific two of the three additionally
# relied on the now-gone `ChangeManifest`/`last_manifest_id` bookkeeping for
# `task is None` dispatch, which no longer exists at all (audit is
# task-shaped now — see `orchestrator.py`'s `_resolve_audit_task`).
#
# The replacement below pins the STRONGER guarantee: a live, unpublished
# postcommit candidate on record does not change the outcome — the legacy
# decision is refused either way.
# =============================================================================


def test_legacy_commit_and_push_is_always_refused_even_with_a_live_candidate(tmp_path):
    """Supersedes the three retired leak-guard tests above (see the block
    comment). A malformed reply to the review packet triggers a corrective
    re-prompt (whose payload carries none of the postcommit identifiers); a
    `push` approval answering THAT request therefore carries no postcommit
    binding. Previously this fell through to `_dispatch_git`'s fail-closed
    leak guard specifically because a live candidate was on record; now there
    is no legacy git path left to fall through to at all — it is refused the
    same way regardless of whether a candidate exists."""
    executor = WritingExecutor(tmp_path / "worktrees", {"a.py": "one\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task, client = (
        build_postcommit_with_client(
            tmp_path,
            executor,
            responses=[
                "not a json directive at all",
                push_approval_from_last_submitted,
                stop_block(),
            ],
            policy=PolicyConfig(implement_enabled=True, allow_protected_push=True),
        )
    )
    orch._dispatch_executor(implement(task.id))
    assert orch.state.phase == Phase.READY.value
    assert orch.state.task_execution is not None  # a live, unpublished candidate IS on record

    def fail_if_called(*a, **kw):
        raise AssertionError("push_exact must not be called")

    original = GitGateway.push_exact
    GitGateway.push_exact = fail_if_called
    try:
        outcome = orch.run()
    finally:
        GitGateway.push_exact = original

    assert outcome == Phase.STOPPED.value
    prompts = [p for _, p in client.submitted]
    assert any("policy_denied" in p and "no longer supported" in p for p in prompts)
    # the live candidate is unaffected — nothing was published or lost
    assert execution_store.load(task.id).candidate_sha != ""


# =============================================================================
# Report-first packets (2026-08-04)
#
# The packet carried the entire patch, and on rt-09 that was 38 KB of new test
# code. ChatGPT could not process the message: the composer accepted it and
# rendered optimistically (so the loop logged `request_submitted: confirmed`),
# generation failed server-side, and the turn was never persisted — reloading
# the conversation showed no user message at all. The loop then waited 486s for
# a reply that could not come, restarted Chrome between attempts, and tried to
# rotate to a fresh chat, which failed for the same reason. Three blockers, one
# cause: a message too big to send.
#
# The fix carries the executor's own report (so the reviewer can judge intent)
# and omits an oversized diff LOUDLY rather than truncating it silently.


def test_the_packet_carries_what_the_executor_said_it_did(tmp_path):
    """The point of the change: a reviewer gets the executor's account, which
    a raw patch answers badly."""
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, _root, _wt, execution_store, _intent, task = build_postcommit(tmp_path, executor)

    orch._dispatch_executor(implement(task.id))
    orch._step_ready()

    payload = orch.state.pending_request.payload
    assert "round 1" in payload, "the executor's summary must reach the reviewer"
    assert "details" in payload
    execution = execution_store.load(task.id)
    assert execution.report_summary == "round 1"
    assert execution.report_details == "details"


def test_the_report_is_labelled_as_claimed_not_read(tmp_path):
    """Load-bearing. Every other section is read from git; folding the
    executor's voice back in without saying so is what SECURITY.md finding #2
    removed from authorization. The label is the whole safety margin."""
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, _root, _wt, _store, _intent, task = build_postcommit(tmp_path, executor)

    orch._dispatch_executor(implement(task.id))
    orch._step_ready()

    payload = orch.state.pending_request.payload
    assert "CLAIMED by the executor, not read from git" in payload


def test_an_oversized_diff_is_OMITTED_not_truncated(tmp_path):
    """Silent truncation is worse than omission: a reviewer who cannot see
    that the patch was cut reads a partial diff as the whole change."""
    from autoloop import packet as packet_mod

    big = "\n".join(f"line {i} of a large generated file" for i in range(4000)) + "\n"
    executor = WritingExecutor(tmp_path / "worktrees", {"big.py": big})
    orch, _root, worktrees, execution_store, _intent, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    orch._step_ready()

    payload = orch.state.pending_request.payload
    assert "Full diff: OMITTED" in payload
    assert "Nothing was truncated" in payload
    assert "line 3999 of a large generated file" not in payload, "no patch body"
    # The git-read facts are still complete — that is what makes the omission
    # honest rather than a hole.
    assert "big.py" in payload
    assert "Diff stat:" in payload
    assert len(payload) < packet_mod.DIFF_INCLUDE_MAX_CHARS + 4_000


def test_a_small_diff_is_still_sent_in_full(tmp_path):
    """The cap must not cost fidelity on the changes where fidelity is cheap."""
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, _root, _wt, _store, _intent, task = build_postcommit(tmp_path, executor)

    orch._dispatch_executor(implement(task.id))
    orch._step_ready()

    payload = orch.state.pending_request.payload
    assert "Full diff:" in payload
    assert "OMITTED" not in payload
    assert "print('hi')" in payload


def test_a_record_with_no_report_says_so_rather_than_going_blank(tmp_path):
    """Candidates committed by an older build, and crash-recovery adoptions,
    have no report. An absent report must not read as a silent one."""
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, _root, worktrees, execution_store, _intent, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))
    execution = execution_store.load(task.id)
    execution.report_summary = ""
    execution.report_details = ""

    text = build_review_packet(execution, worktree_git_for(worktrees, task.id), task)

    assert "(none recorded" in text
    assert "predates report capture" in text


def test_the_report_never_widens_scope(tmp_path):
    """The guarantee that makes including it safe at all: path ownership is
    checked against git's own range, never against what the executor wrote.
    A report naming a file it did not touch changes nothing."""
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, _root, worktrees, execution_store, _intent, task = build_postcommit(
        tmp_path, executor
    )
    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    execution.report_summary = "also rewrote /etc/passwd and docs/SECURITY.md"
    execution_store.save(execution)
    text = build_review_packet(execution, worktree_git_for(worktrees, task.id), task)

    assert "also rewrote" in text                      # it is shown, as a claim
    assert "docs/SECURITY.md" not in text.split("Executor report")[0], (
        "the claim must not appear among the git-read changed paths"
    )
    assert execution.allowed_paths == ("feature.py",) or "feature.py" in text
