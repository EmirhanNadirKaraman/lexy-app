"""Pass 2a: worktree lifecycle + the produce-then-review commit path wired
into the orchestrator's executor dispatch.

Real git repos throughout, matching `test_git_gateway.py` /
`test_postcommit_primitives.py`'s self-contained style rather than importing
their fixtures. The orchestrator-level tests call `Orchestrator.
_dispatch_executor` directly with a hand-built `implement` Directive — the
browser/ChatGPT transport (submitting, awaiting, parsing) is already covered
exhaustively by `test_orchestrator.py` and is not this file's concern; this
file is about what happens once a directive reaches executor dispatch.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.errors import GitCommandError, StateError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import CommitIntent, IntentStore, TaskExecutionStore
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/test-conversation"


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.email", "test@example.com")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("hello\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root


def gateway(root) -> GitGateway:
    return GitGateway(root, PolicyEngine(PolicyConfig()))


def install_hook(repo, name, body="#!/bin/sh\nexit 0\n", executable=True):
    hook = repo / ".git" / "hooks" / name
    hook.write_text(body)
    if executable:
        hook.chmod(0o755)
    return hook


# =============================================================================
# 1-3. WorktreeManager lifecycle
# =============================================================================


def test_worktree_create_then_remove_round_trip_branch_survives(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    wm = WorktreeManager(gw, tmp_path / "worktrees")

    execution = wm.create("t1", base)
    wt_path = Path(execution.worktree_path)
    assert wt_path.exists()
    assert execution.task_id == "t1"
    assert execution.task_branch == "autoloop/t1"
    assert execution.task_base_sha == base
    assert execution.candidate_sha == ""
    assert run_git(wt_path, "rev-parse", "HEAD").strip() == base
    assert gw.branch_exists("autoloop/t1")

    wm.remove("t1")
    assert not wt_path.exists()
    # the branch and its history survive worktree removal
    assert gw.branch_exists("autoloop/t1")
    assert run_git(repo, "rev-parse", "refs/heads/autoloop/t1").strip() == base


def test_worktree_list_worktrees_reports_path_and_branch(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    wm = WorktreeManager(gw, tmp_path / "worktrees")
    execution = wm.create("t1", base)

    entries = wm.list_worktrees()
    paths = {e["path"] for e in entries}
    assert str(Path(execution.worktree_path).resolve()) in {str(Path(p).resolve()) for p in paths}
    linked = next(e for e in entries if Path(e["path"]).resolve() == Path(execution.worktree_path).resolve())
    assert linked["branch"] == "refs/heads/autoloop/t1"


# ---- refusals ----------------------------------------------------------------


def test_worktree_create_refuses_duplicate_task_id(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    wm = WorktreeManager(gw, tmp_path / "worktrees")
    wm.create("t1", base)
    with pytest.raises(GitCommandError, match="already exists"):
        wm.create("t1", base)


def test_worktree_create_refuses_when_branch_already_exists(repo, tmp_path):
    gw = gateway(repo)
    base = gw.head_sha()
    run_git(repo, "branch", "autoloop/t1")
    wm = WorktreeManager(gw, tmp_path / "worktrees")
    with pytest.raises(GitCommandError, match="branch"):
        wm.create("t1", base)


@pytest.mark.parametrize(
    "bad_id",
    ["", "a/b", "../escape", "..", "-rf", "a..b", "/abs/path", "a b", ".hidden"],
)
def test_worktree_create_refuses_unsafe_task_ids(repo, tmp_path, bad_id):
    gw = gateway(repo)
    base = gw.head_sha()
    worktrees_root = tmp_path / "worktrees"
    wm = WorktreeManager(gw, worktrees_root)
    before = wm.list_worktrees()  # just the main checkout itself
    with pytest.raises(ValueError):
        wm.create(bad_id, base)
    # validation raises BEFORE `root_dir.mkdir` or any git call — nothing on
    # disk and git's own worktree list is unchanged.
    assert not worktrees_root.exists()
    assert wm.list_worktrees() == before


def test_git_itself_refuses_two_worktrees_on_the_same_branch(repo, tmp_path):
    """The fail-closed property `WorktreeManager.create()` relies on:
    `create()` always creates a NEW branch (`-b`), so it can never reach
    git's "already checked out" refusal itself — this demonstrates the
    underlying git behaviour directly, at the plumbing level `create()`
    depends on."""
    run_git(repo, "branch", "shared-branch")
    wt1 = tmp_path / "wt1"
    run_git(repo, "worktree", "add", str(wt1), "shared-branch")

    wt2 = tmp_path / "wt2"
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        run_git(repo, "worktree", "add", str(wt2), "shared-branch")
    message = (excinfo.value.stderr or "") + (excinfo.value.stdout or "")
    assert "already" in message
    assert not wt2.exists()


# =============================================================================
# 4. Policy: worktree action-verb validation
# =============================================================================


def test_policy_accepts_worktree_add(tmp_path):
    verdict = PolicyEngine(PolicyConfig()).validate_git_command(
        ("worktree", "add", "-b", "autoloop/t1", str(tmp_path / "wt"), "a" * 40)
    )
    assert verdict.allowed


def test_policy_accepts_worktree_remove_list_prune():
    engine = PolicyEngine(PolicyConfig())
    assert engine.validate_git_command(("worktree", "remove", "--force", "/x")).allowed
    assert engine.validate_git_command(("worktree", "list", "--porcelain")).allowed
    assert engine.validate_git_command(("worktree", "prune")).allowed


def test_policy_rejects_worktree_lock_verb():
    verdict = PolicyEngine(PolicyConfig()).validate_git_command(("worktree", "lock", "/some/path"))
    assert not verdict.allowed
    assert verdict.code == "git_verb_forbidden"


def test_policy_rejects_worktree_unlock_and_move_verbs():
    engine = PolicyEngine(PolicyConfig())
    for verb in ("unlock", "move", "repair"):
        verdict = engine.validate_git_command(("worktree", verb, "/x"))
        assert not verdict.allowed
        assert verdict.code == "git_verb_forbidden"


def test_policy_rejects_bare_worktree_with_no_verb():
    verdict = PolicyEngine(PolicyConfig()).validate_git_command(("worktree",))
    assert not verdict.allowed
    assert verdict.code == "git_verb_required"


# =============================================================================
# 5-11. The commit path through the orchestrator
# =============================================================================


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def failing_validation(argv, **kwargs):
    class Proc:
        returncode = 1
        stdout = "ruff: E501 line too long\n"
        stderr = ""

    return Proc()


class WritingExecutor:
    """Test double standing in for the (not-yet-built) real task executor.
    Writes `files` directly into the worktree for `task.id` — derivable from
    `worktrees_root / task.id`, the same layout `WorktreeManager` uses — and
    reports success with those paths as `changed_paths`. `extra_hook`, when
    given, runs after the files are written (e.g. to leave an extra untracked
    file behind, modelling residual state the task did NOT intend to commit).
    """

    def __init__(self, worktrees_root, files, status="ok", extra_hook=None):
        self.worktrees_root = Path(worktrees_root)
        self.files = dict(files)
        self.status = status
        self.extra_hook = extra_hook
        self.calls = 0

    def execute(self, directive, task):
        self.calls += 1
        wt = self.worktrees_root / task.id
        for rel, content in self.files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if self.extra_hook is not None:
            self.extra_hook(wt)
        return ExecutionOutcome(
            status=self.status,
            summary="wrote the files",
            details="details",
            validation="executor-reported validation placeholder",
            changed_paths=tuple(self.files.keys()),
        )


def build_postcommit(
    tmp_path,
    executor,
    task_id="t1",
    validation_runner=ok_validation,
    approved_paths=None,
    task_validation=(),
    task_validation_cwd="",
    executor_factory=None,
    validation_env=None,
):
    """`executor_factory`, when given, is called with the `WorktreeManager`
    once it exists and its result replaces `executor`. Needed by the B4b test,
    which uses a REAL `ImplementExecutor` — that has to be rooted at the
    task's worktree, which does not exist until this function builds it."""
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
        policy=PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    # `approved_paths` defaults to every path the executor is ever going to
    # report as changed (across every round, for `per_round_files`-shaped
    # doubles) — computed HERE, before the task ever dispatches, mirroring
    # what a real `plan`/`seed_tasks.json` scope declaration looks like. A
    # test that specifically wants to exercise an out-of-scope path passes
    # `approved_paths` explicitly instead.
    if approved_paths is None:
        derived = set(getattr(executor, "files", {}) or {})
        for round_files in getattr(executor, "per_round_files", None) or ():
            derived |= set(round_files)
        approved_paths = tuple(sorted(derived))
    task = Task(
        id=task_id,
        title=f"Title {task_id}",
        description="desc",
        approved_paths=tuple(approved_paths),
        validation=tuple(task_validation),
        validation_cwd=task_validation_cwd,
    )
    if executor_factory is not None:
        executor = executor_factory(worktrees)
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
        validation_env=validation_env,
    )
    return orch, repo_root, worktrees, execution_store, intent_store, task


def implement(task_id="t1"):
    return Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task_id)


def worktree_git_for(worktrees: WorktreeManager, task_id: str) -> GitGateway:
    return GitGateway(worktrees.path_for(task_id), PolicyEngine(PolicyConfig()))


# ---- 5. full happy path -------------------------------------------------------


def test_happy_path_commits_and_passes_review(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution is not None
    assert execution.candidate_sha != ""
    assert execution.review_round == 1
    assert execution.candidate_commit_count == 1
    assert intent_store.load(task.id) is None  # cleared

    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.is_descendant(execution.candidate_sha, execution.task_base_sha)
    touched = wt_git.commit_range_paths(execution.task_base_sha, execution.candidate_sha)
    assert touched == {"feature.py"}
    assert wt_git.dirty_entries() == []

    # Pass 2b: a clean pass builds the review packet and re-enters `ready` to
    # send it, rather than parking with a placeholder message (pass 2a).
    assert orch.state.phase == Phase.READY.value
    assert "POST-COMMIT REVIEW PACKET" in orch.state.outbox
    assert execution.task_id in orch.state.outbox
    assert execution.task_branch in orch.state.outbox
    assert execution.task_base_sha in orch.state.outbox
    assert execution.candidate_sha in orch.state.outbox
    # state mirrors the execution record
    assert orch.state.task_execution["candidate_sha"] == execution.candidate_sha


# ---- 6. hook adds an unexpected path -> recorded, not refused ------------------


def test_hook_adding_unexpected_path_is_recorded_not_refused(tmp_path):
    """Isolates the POST-commit scope check. The hook adds `sneaked.txt`
    strictly AFTER the pre-commit check ran, so this path is one only the
    post-commit comparison can see — which makes it the sharpest test that
    site 2 became advisory too. Leaving site 2 blocking parks here for a path
    site 1 never had the chance to look at.

    Historical: refused as `post_commit_verification_failed` before
    2026-08-05. What the hook committed is unchanged and still fully visible
    to the reviewer in the range diff; only the park is gone."""
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    install_hook(
        repo_root,
        "pre-commit",
        "#!/bin/sh\nprintf 'sneaked\\n' > sneaked.txt\ngit add sneaked.txt\n",
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution.candidate_sha != ""  # the commit WAS created
    wt_git = worktree_git_for(worktrees, task.id)
    on_branch = run_git(
        Path(execution.worktree_path), "rev-parse", execution.task_branch
    ).strip()
    assert on_branch == execution.candidate_sha  # not rolled back

    assert orch.state.phase != Phase.NEEDS_USER.value, orch.state.question
    assert "POST-COMMIT REVIEW PACKET" in (orch.state.outbox or "")
    assert execution.out_of_scope_paths == ("sneaked.txt",)
    assert "sneaked.txt" not in execution.allowed_paths  # recorded, not granted
    assert wt_git.dirty_entries() == []  # the hook's own `git add` left nothing dirty


# ---- 7. hook modifies an approved file -> allowed, visible in range_diff -------


def test_hook_modifying_approved_file_is_allowed_and_visible_in_diff(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "APPROVED\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )
    install_hook(
        repo_root,
        "pre-commit",
        "#!/bin/sh\nprintf 'HOOK PAYLOAD\\n' > feature.py\ngit add feature.py\n",
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution.candidate_sha != ""
    wt_git = worktree_git_for(worktrees, task.id)
    diff = wt_git.range_diff(execution.task_base_sha, execution.candidate_sha)
    assert "HOOK PAYLOAD" in diff
    assert "APPROVED" not in diff  # the hook's bytes are what was actually committed

    # the path is still exactly what was planned — no path-ownership failure —
    # so this passes post-commit review (hook content changes are NOT caught
    # by the path check; that's what range_diff/post-commit validation are for)
    assert orch.state.phase == Phase.READY.value
    assert "POST-COMMIT REVIEW PACKET" in orch.state.outbox
    assert "HOOK PAYLOAD" in orch.state.outbox  # the packet's own diff, not just wt_git's


# ---- 8. residual dirty worktree after commit -> refused ------------------------


def test_residual_dirty_worktree_after_commit_is_refused(tmp_path):
    def leave_untracked_leftover(wt):
        (wt / "leftover.tmp").write_text("not part of the plan\n", encoding="utf-8")

    executor = WritingExecutor(
        tmp_path / "worktrees",
        {"feature.py": "print('hi')\n"},
        extra_hook=leave_untracked_leftover,
    )
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution.candidate_sha != ""  # commit still happened
    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.dirty_entries() == [("??", "leftover.tmp")]

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "REFUSED" in orch.state.question
    assert "not clean" in orch.state.question


# ---- 9. failing post-commit validation -> refused -------------------------------


def test_failing_post_commit_validation_is_refused(tmp_path):
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor, validation_runner=failing_validation
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution.candidate_sha != ""  # commit still happened, not rolled back

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "REFUSED" in orch.state.question
    assert "post-commit validation failed" in orch.state.question
    assert "E501" in orch.state.question
    # Bounds the 2026-08-05 relaxation: ONLY the path-ownership comparison
    # went advisory. Every other post-commit check still parks the candidate.
    assert execution.out_of_scope_paths == ()


def test_status_error_from_executor_never_reaches_commit(tmp_path):
    """A non-'ok' outcome must not be committed at all — distinct from every
    post-commit refusal above, which all commit first."""
    executor = WritingExecutor(
        tmp_path / "worktrees", {"feature.py": "broken\n"}, status="error"
    )
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution.candidate_sha == ""
    assert orch.state.phase == Phase.READY.value  # not parked — the old-style
    # implementation_review path continues the loop, matching the manifest
    # path's behaviour for a non-ok outcome.


def test_environment_snapshot_is_taken_before_the_executor_runs(tmp_path):
    """The snapshot must predate implementation, not just the commit: a hook
    that materializes DURING the executor's run (e.g. a dependency
    postinstall script) has to be caught by comparing against a snapshot from
    BEFORE it existed. If the snapshot were taken after `execute()` returned
    (right before committing), this hook would already be part of the
    baseline and `verify_unchanged` would report nothing — silently trusting
    it. `extra_hook` installs the hook from inside `execute()`, i.e. strictly
    between where the snapshot is taken and where the commit is attempted.

    The refusal parks in `needs_user`, the same as every other refusal in
    this path. A mid-task hook install used to escape as a raw
    `GitCommandError`; that inconsistency was closed deliberately, because
    "the environment moved under this task" is precisely the situation a
    human has to look at, not something to re-prompt ChatGPT about.
    """

    def install_hook_mid_task(wt):
        install_hook(tmp_path / "repo", "pre-commit", "#!/bin/sh\nexit 0\n")

    executor = WritingExecutor(
        tmp_path / "worktrees",
        {"feature.py": "print('hi')\n"},
        extra_hook=install_hook_mid_task,
    )
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor
    )

    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "environment changed" in (orch.state.question or "")
    execution = execution_store.load(task.id)
    assert execution.candidate_sha == ""  # nothing was committed
    assert intent_store.load(task.id) is None  # the drift check runs BEFORE
    # the intent is ever written (see `commit_and_capture`), so no intent was
    # left behind for a later crash-reconciliation pass to trip over
    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.head_sha() == execution.task_base_sha  # branch tip unmoved


# =============================================================================
# 10-11. crash reconciliation through the orchestrator
# =============================================================================


def precommit_a_crash(tmp_path, executor_files, install=None):
    """Set up a task execution, commit directly through `commit_and_capture`
    (bypassing the orchestrator), and leave the intent file on disk WITHOUT
    persisting `candidate_sha` — exactly the state a process crash between
    'commit exited 0' and 'the sha was saved' leaves behind."""
    executor = WritingExecutor(tmp_path / "worktrees", {})  # never actually called
    orch, repo_root, worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path, executor, approved_paths=tuple(executor_files.keys())
    )
    if install is not None:
        install(repo_root)

    base = GitGateway(repo_root, PolicyEngine(PolicyConfig())).head_sha()
    execution = worktrees.create(task.id, base)
    execution_store.save(execution)
    wt_git = worktree_git_for(worktrees, task.id)
    parent = wt_git.head_sha()
    for rel, content in executor_files.items():
        target = Path(execution.worktree_path) / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    intent = CommitIntent.create(
        task.id, execution.task_branch, parent, tuple(executor_files.keys()), "feat: precommitted"
    )
    real_sha, _ = wt_git.commit_and_capture(
        "feat: precommitted", tuple(executor_files.keys()), intent_store, intent
    )
    # Simulate the crash: candidate_sha was never persisted, intent is still there.
    assert execution_store.load(task.id).candidate_sha == ""
    assert intent_store.load(task.id) is not None
    return orch, repo_root, worktrees, execution_store, intent_store, task, real_sha


def test_crash_after_commit_before_persisting_is_recoverable_no_second_commit(tmp_path):
    orch, repo_root, worktrees, execution_store, intent_store, task, real_sha = precommit_a_crash(
        tmp_path, {"feature.py": "print('recovered')\n"}
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution.candidate_sha == real_sha  # adopted, not re-committed
    assert intent_store.load(task.id) is None  # cleared once resolved
    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.head_sha() == real_sha  # no second commit was created
    assert wt_git.commit_list(execution.task_base_sha, real_sha).__len__() == 1

    # the executor was never invoked — RECOVERABLE adopts, it does not re-run
    assert orch._executor.calls == 0

    assert orch.state.phase == Phase.READY.value
    assert "POST-COMMIT REVIEW PACKET" in orch.state.outbox


def test_crash_with_branch_head_outside_the_plan_is_ambiguous_and_parks(tmp_path):
    def add_unplanned_hook(repo_root):
        install_hook(
            repo_root,
            "pre-commit",
            "#!/bin/sh\nprintf 'sneaked\\n' > sneaked.txt\ngit add sneaked.txt\n",
        )

    orch, repo_root, worktrees, execution_store, intent_store, task, real_sha = precommit_a_crash(
        tmp_path, {"feature.py": "print('x')\n"}, install=add_unplanned_hook
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution.candidate_sha == ""  # left unresolved, not adopted
    assert intent_store.load(task.id) is not None  # preserved for inspection

    wt_git = worktree_git_for(worktrees, task.id)
    assert wt_git.head_sha() == real_sha  # nothing rolled back or re-committed

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "AMBIGUOUS" in orch.state.question
    assert orch._executor.calls == 0


# =============================================================================
# 12. state schema v2 refuses to load
# =============================================================================


def test_state_schema_v2_refuses_to_load(tmp_path):
    path = tmp_path / "state.json"
    state = LoopState.new(URL)
    data = state.to_dict()
    assert data["schema_version"] == 3
    data["schema_version"] = 2
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(StateError):
        StateStore(path).load()


# ---- B4b: post-commit re-runs the TASK's validation, not the audit's ---------


class _WritingAgent:
    """Minimal write-capable agent double: drops one file into the worker repo
    so `dirty_paths_all()` has something real to report."""

    def __init__(self, root_for, task_id, rel_path):
        self._root_for, self._task_id, self._rel = root_for, task_id, rel_path

    def run(self, spec):
        from autoloop.audit.agents import AgentResult

        target = Path(self._root_for(self._task_id)) / self._rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("changed by the agent\n", encoding="utf-8")
        return AgentResult(domain=spec.domain, raw_text="wrote it", returncode=0,
                           duration_seconds=0.0, command=("claude",))


def test_post_commit_reruns_the_tasks_own_validation_not_the_audit_set(tmp_path):
    """B4b. The declared commands must run BEFORE the commit (executor) and
    AGAIN AFTER it (orchestrator) — otherwise the reviewed commit is graded by
    the generic audit set, and produce-then-review's whole premise (a hook can
    change committed content after the executor looked) is not actually
    checked for any task that declares its own validation.

    Recorded through ONE runner shared by both ends, so "the same commands"
    is observed rather than asserted twice against two separate doubles.

    Compared against the EFFECTIVE form (`effective_validation_command`, which
    adds `-n auto` / `-p no:cacheprovider` to a pytest run — val-01,
    2026-08-06) rather than the declared literal. Both ends normalize at the
    same single point, `run_validation_commands`, so they still agree by
    construction; this test is what proves the parallel flags reach the live
    post-commit gate and not just the config template.
    """
    from autoloop.implement_executor import ImplementExecutor
    from autoloop.validation import effective_validation_command

    DECLARED = (("pytest", "-q", "tests/test_thing.py"),)
    EFFECTIVE = effective_validation_command(DECLARED[0])
    AUDIT_DEFAULT = (("ruff", "check", "."),)
    calls: list[tuple[tuple[str, ...], str]] = []

    def recorder(argv, **kwargs):
        calls.append((tuple(argv), str(kwargs.get("cwd", ""))))

        class Proc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        return Proc()

    def make_executor(worktrees):
        return ImplementExecutor(
            git=GitGateway(tmp_path / "repo", PolicyEngine(PolicyConfig())),
            agent_runner=_WritingAgent(worktrees.path_for, "t1", "src/thing.py"),
            # The executor's OWN default is the audit set; the task's declared
            # validation must win over it at both ends.
            validation_commands=AUDIT_DEFAULT,
            command_runner=recorder,
            worker_repo_root_for=worktrees.path_for,
            policy=PolicyEngine(PolicyConfig()),
            agent_runner_factory=lambda root: _WritingAgent(
                lambda _t: root, "t1", "src/thing.py"
            ),
            # Since advis-01 (2026-08-26) a round whose agent never uses the
            # advisory channel is handed back once and then WITHHELD from review.
            # `_WritingAgent` cannot ask, and this test is about which validation
            # commands reach the pre- and post-commit gates — a withheld round
            # runs neither. Pinned off; the contract is graded in
            # `test_agent_self_validation.py` §10a.
            advisory_zero_call_returns=0,
        )

    orch, repo_root, worktrees, execution_store, _intents, _task = build_postcommit(
        tmp_path,
        executor=None,
        validation_runner=recorder,
        approved_paths=("src/thing.py",),
        task_validation=DECLARED,
        executor_factory=make_executor,
    )

    orch._dispatch(implement())

    ran = [argv for argv, _cwd in calls]
    assert ran.count(EFFECTIVE) == 2, (
        f"expected the declared command before AND after the commit, saw {ran}"
    )
    for argv in ran:
        assert ("-n", "auto") in set(zip(argv, argv[1:])), (
            "a validation run went serial — whatever a task declares, both ends "
            f"run it in parallel: {' '.join(argv)}"
        )
    assert AUDIT_DEFAULT[0] not in ran, (
        f"the audit default was substituted for the task's declared validation: {ran}"
    )
    # ...and the persisted record is what carried it across, so a crash-resumed
    # task re-runs the same thing rather than falling back.
    execution = execution_store.load("t1")
    assert execution.validation_commands == DECLARED


def test_a_round_that_never_ran_the_suite_produces_no_candidate_to_review(tmp_path):
    """advis-01 (revision, 2026-08-27), END TO END: a round whose agent never
    used the advisory validation channel does not reach the reviewer.

    The executor-level half — the bounded hand-back, and the `status="error"`
    that follows a record still showing zero — is graded in
    `test_agent_self_validation.py` §10a. What only this file can show is the
    CONSEQUENCE, through the real orchestrator: `_dispatch_task_postcommit`
    returns at its non-ok test, so no commit is made, no `candidate_sha` is
    recorded, no `CommitIntent` survives and no review round is opened. There is
    nothing for an approval to publish.

    And the existing machinery is what bounds a repeat: the round is charged to
    the TASK's attempt budget (`executor_reported_failure`), not to the fault
    budget, so a task whose agent keeps skipping the suite walks into the
    `attempt_count_ceiling` park that already exists rather than looping
    forever. No new park kind, no orchestrator change.

    `_WritingAgent` writes with no advisory request, exactly like the 18
    never-asked rounds, and the DEFAULT allowance is used deliberately — this
    test is about what production does.
    """
    from autoloop.implement_executor import ImplementExecutor

    calls: list[tuple[str, ...]] = []

    def recorder(argv, **kwargs):
        calls.append(tuple(argv))

        class Proc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        return Proc()

    def make_executor(worktrees):
        return ImplementExecutor(
            git=GitGateway(tmp_path / "repo", PolicyEngine(PolicyConfig())),
            agent_runner=_WritingAgent(worktrees.path_for, "t1", "src/thing.py"),
            validation_commands=(("ruff", "check", "."),),
            command_runner=recorder,
            worker_repo_root_for=worktrees.path_for,
            policy=PolicyEngine(PolicyConfig()),
            agent_runner_factory=lambda root: _WritingAgent(
                lambda _t: root, "t1", "src/thing.py"
            ),
        )

    orch, _repo, _worktrees, execution_store, intent_store, task = build_postcommit(
        tmp_path,
        executor=None,
        validation_runner=recorder,
        approved_paths=("src/thing.py",),
        executor_factory=make_executor,
    )

    orch._dispatch_executor(implement(task.id))

    execution = execution_store.load(task.id)
    assert execution is not None
    assert execution.candidate_sha == "", "a zero-request round produced a candidate"
    assert execution.review_round == 0, "a zero-request round opened a review"
    assert intent_store.load(task.id) is None
    # Neither validation gate ran: the executor short-circuits before its own
    # run, and the post-commit re-run needs a commit that was never made.
    assert calls == []
    # Charged to the task, by the existing route, with the existing reason.
    assert execution.attempt_count == 1
    assert execution.fault_attempt_count == 0
    assert "executor_reported_failure" in execution.attempt_ledger[-1]
    # The reviewer is TOLD — they are simply not shown a candidate.
    assert "WITHHELD from review" in orch.state.outbox


def test_post_commit_validation_honours_the_declared_cwd(tmp_path):
    """A declared `validation_cwd` must apply to the post-commit re-run too —
    the right commands from the wrong directory check nothing."""
    calls: list[str] = []

    def recorder(argv, **kwargs):
        calls.append(str(kwargs.get("cwd", "")))

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return Proc()

    executor = WritingExecutor(tmp_path / "worktrees", {"sub/app/f.py": "x\n"})
    orch, _repo, worktrees, _store, _intents, _task = build_postcommit(
        tmp_path,
        executor,
        validation_runner=recorder,
        approved_paths=("sub/app/f.py",),
        task_validation=(("pytest", "-q"),),
        task_validation_cwd="sub/app",
    )
    orch._dispatch(implement())

    expected = str(Path(worktrees.path_for("t1")) / "sub" / "app")
    assert calls, "post-commit validation never ran"
    assert calls[-1] == expected, f"ran from {calls[-1]}, expected {expected}"


# ---- validation-binding integrity (frozen at dispatch) ----------------------


def _dispatch_once(tmp_path, declared, files=None, task_id="t1"):
    """One full dispatch, returning the harness so a second round can run
    against the SAME on-disk execution record."""
    executor = WritingExecutor(tmp_path / "worktrees", files or {"src/a.py": "x\n"})
    return build_postcommit(
        tmp_path,
        executor,
        task_id=task_id,
        approved_paths=tuple(files or {"src/a.py": "x\n"}),
        task_validation=declared,
    )


def test_recovery_uses_the_persisted_binding_not_the_current_task_file(tmp_path):
    """A later edit to the task file must NOT change what a resumed round
    validates. The binding is authorization-adjacent: if `tasks.json` could
    widen it mid-task, an approved review would be re-graded against commands
    nobody reviewed."""
    DISPATCHED = (("pytest", "-q", "declared/at/dispatch.py"),)
    WIDENED = (("pytest", "-q", "--co"),)

    orch, _repo, _wt, execution_store, _intents, _task = _dispatch_once(tmp_path, DISPATCHED)
    orch._dispatch(implement())
    assert execution_store.load("t1").validation_commands == DISPATCHED

    # The operator (or an agent, or a ChatGPT `plan`) rewrites the task's
    # validation between rounds, then the task is dispatched again.
    registry = orch._registry
    registry.get("t1").validation = WIDENED
    orch._registry.get("t1").validation_cwd = "somewhere/else"
    orch._dispatch(implement())

    persisted = execution_store.load("t1")
    assert persisted.validation_commands == DISPATCHED, (
        "a task-file edit replaced the binding the round was dispatched under"
    )
    assert persisted.validation_cwd == ""


def test_an_unbound_legacy_record_adopts_the_task_binding_once(tmp_path):
    """The one permitted write: a record from before the field existed has no
    binding at all, so adopting the Task's value cannot WIDEN anything — and
    is strictly better than silently falling back to the audit default."""
    DECLARED = (("pytest", "-q", "x.py"),)
    orch, _repo, _wt, execution_store, _intents, _task = _dispatch_once(tmp_path, DECLARED)
    orch._dispatch(implement())

    # Simulate the legacy on-disk shape: binding stripped, everything else kept.
    execution = execution_store.load("t1")
    execution.validation_commands = ()
    execution.validation_cwd = ""
    execution_store.save(execution)

    orch._dispatch(implement())
    assert execution_store.load("t1").validation_commands == DECLARED


def test_binding_survives_a_restart_with_a_fresh_orchestrator(tmp_path):
    """Restart/recovery: a NEW Orchestrator reading the same state dir must
    validate against the persisted binding, not re-derive it."""
    DISPATCHED = (("pytest", "-q", "round/one.py"),)
    orch, _repo, _wt, execution_store, _intents, _task = _dispatch_once(tmp_path, DISPATCHED)
    orch._dispatch(implement())

    reloaded = TaskExecutionStore(tmp_path / "executions").load("t1")
    assert reloaded.validation_commands == DISPATCHED
    assert reloaded.task_base_sha and reloaded.candidate_sha, "round did not commit"


# ---- validation mutation guard ----------------------------------------------
#
# Accepted v1 posture: validation code RECEIVES real (test-only) DB
# credentials. That is not a secrecy boundary, so the thing worth enforcing is
# that validation cannot write — including into a path the task was approved
# to touch, because approval authorises the AGENT to edit a path, never
# validation to mutate one.

MUTATION_SECRET = "sup3r-secret-db-password"


def _validation_env_with_secret(tmp_path):
    from autoloop.validation_env import ValidationEnv

    return ValidationEnv(
        tmp_path / "validation.env",
        {
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "5433",
            "DB_NAME": "zzz_validation_test",
            "DB_USER": "zzz_validation_user",
            "DB_PASSWORD": MUTATION_SECRET,
            "SECRET_KEY": "zzz-signing-key-for-tests",
        },
    )


def _mutating_validation(mutate):
    """A validation runner that succeeds but performs `mutate(cwd)` — the
    real shape of the risk: the commands PASS, so nothing else in the gate
    would notice."""

    def runner(argv, **kwargs):
        mutate(Path(kwargs["cwd"]))

        class Proc:
            returncode = 0
            stdout = "All checks passed!\n"
            stderr = ""

        return Proc()

    return runner


def _run_with_mutation(tmp_path, mutate, files=None, approved=None):
    files = files or {"feature.py": "print('hi')\n"}
    executor = WritingExecutor(tmp_path / "worktrees", files)
    orch, _repo, worktrees, execution_store, _intents, task = build_postcommit(
        tmp_path,
        executor,
        validation_runner=_mutating_validation(mutate),
        approved_paths=approved if approved is not None else tuple(files),
        validation_env=_validation_env_with_secret(tmp_path),
    )
    orch._dispatch_executor(implement(task.id))
    return orch, worktrees, execution_store, task


def test_validation_writing_the_password_into_an_APPROVED_path_is_refused(tmp_path):
    """The case the brief singles out: the file is one the task was allowed to
    change, so path-ownership passes and only the mutation guard catches it."""
    orch, worktrees, execution_store, task = _run_with_mutation(
        tmp_path,
        lambda cwd: (cwd / "feature.py").write_text(f"LEAKED = {MUTATION_SECRET!r}\n"),
    )
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "MUTATED" in orch.state.question
    assert "feature.py" in orch.state.question
    assert MUTATION_SECRET not in orch.state.question, "the secret reached the park message"
    # Evidence preserved, uncommitted: the file on disk holds the write, the
    # committed tree does not.
    wt = Path(worktrees.path_for(task.id))
    assert MUTATION_SECRET in (wt / "feature.py").read_text()
    committed = run_git(wt, "show", f"{execution_store.load(task.id).candidate_sha}:feature.py")
    assert MUTATION_SECRET not in committed


def test_validation_writing_the_password_into_an_UNAPPROVED_path_is_refused(tmp_path):
    orch, worktrees, _store, task = _run_with_mutation(
        tmp_path,
        lambda cwd: (cwd / "not_approved.txt").write_text(f"{MUTATION_SECRET}\n"),
    )
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "MUTATED" in orch.state.question
    assert "not_approved.txt" in orch.state.question
    assert MUTATION_SECRET not in orch.state.question


def test_validation_writing_into_a_GITIGNORED_path_is_refused(tmp_path):
    """The gap the pre-existing residual-dirty check cannot close: it is a
    `git status` check, so an ignored path is invisible to it entirely."""
    executor = WritingExecutor(tmp_path / "worktrees", {"feature.py": "print('hi')\n"})
    orch, repo_root, _wt, _store, _intents, task = build_postcommit(
        tmp_path,
        executor,
        validation_runner=_mutating_validation(
            lambda cwd: (cwd / "secrets.log").write_text(f"{MUTATION_SECRET}\n")
        ),
        approved_paths=("feature.py",),
        validation_env=_validation_env_with_secret(tmp_path),
    )
    # `.gitignore` goes in the BASE commit, not the approved set: a leading
    # dot is not a legal approved-path segment, and the point here is that the
    # ignored file is invisible to `git status` — which is what the pre-existing
    # residual-dirty check relies on.
    (repo_root / ".gitignore").write_text("secrets.log\n")
    run_git(repo_root, "add", ".gitignore")
    run_git(repo_root, "commit", "-q", "-m", "ignore secrets.log")

    orch._dispatch_executor(implement(task.id))
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "MUTATED" in orch.state.question
    assert "secrets.log" in orch.state.question
    assert MUTATION_SECRET not in orch.state.question


def test_validation_changing_an_executable_mode_is_refused(tmp_path):
    orch, _wt, _store, _task = _run_with_mutation(
        tmp_path, lambda cwd: (cwd / "feature.py").chmod(0o755)
    )
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "MUTATED" in orch.state.question
    assert "executable" in orch.state.question.lower()


def test_validation_replacing_a_file_with_a_symlink_is_refused(tmp_path):
    def mutate(cwd):
        target = cwd / "feature.py"
        target.unlink()
        target.symlink_to("/etc/passwd")

    orch, _wt, _store, _task = _run_with_mutation(tmp_path, mutate)
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "MUTATED" in orch.state.question
    assert "feature.py" in orch.state.question


def test_validation_staging_a_change_is_refused_even_with_a_clean_worktree(tmp_path):
    """Index-only mutation: `git add` of already-tracked content leaves every
    working-tree file byte-identical, so only the index hash catches it."""
    def mutate(cwd):
        (cwd / "feature.py").write_text("print('hi')\nstaged = 1\n")
        run_git(cwd, "add", "feature.py")
        (cwd / "feature.py").write_text("print('hi')\n")  # restore the worktree

    orch, _wt, _store, _task = _run_with_mutation(tmp_path, mutate)
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "MUTATED" in orch.state.question
    assert "index" in orch.state.question.lower()


def test_validation_that_leaves_the_tree_unchanged_is_accepted(tmp_path):
    """The negative control — without it the guard could be refusing
    everything and every test above would still pass."""
    orch, _wt, execution_store, task = _run_with_mutation(tmp_path, lambda cwd: None)
    assert orch.state.phase != Phase.NEEDS_USER.value, orch.state.question
    assert execution_store.load(task.id).candidate_sha != ""
    assert "POST-COMMIT REVIEW PACKET" in (orch.state.outbox or "")


# ---- always-approved trackers, end to end -----------------------------------


def test_a_tracker_edit_outside_approved_paths_no_longer_refuses(tmp_path):
    """rt-01's actual failure, twice: the agent updated `docs/SUMMARY.md`
    because CLAUDE.md §12 requires it when a file is added, and the commit was
    refused for a path the repo's own rules obliged it to touch."""
    executor = WritingExecutor(
        tmp_path / "worktrees",
        {"src/thing.py": "x\n", "docs/SUMMARY.md": "index\n"},
    )
    orch, _repo, _wt, execution_store, _intents, task = build_postcommit(
        tmp_path,
        executor,
        approved_paths=("src/thing.py",),   # SUMMARY.md deliberately NOT named
    )
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase != Phase.NEEDS_USER.value, orch.state.question
    assert execution_store.load(task.id).candidate_sha != "", "should have committed"


def test_a_NON_tracker_path_outside_approved_paths_commits_and_is_recorded(tmp_path):
    """The scope check is ADVISORY (2026-08-05). A path the task did not name
    and the trackers do not cover is still OUT OF SCOPE — it is just no longer
    a reason to throw the round away. It commits, it reaches review, and the
    path is on the execution record for the packet to render.

    Historical: this parked as `changed_paths_outside_approved` before that
    date. Six such parks in three days were all legitimate work, at least
    three of them caused by a scope guessed wrong when the task was written."""
    executor = WritingExecutor(
        tmp_path / "worktrees",
        {"src/thing.py": "x\n", "src/sneaky.py": "y\n"},
    )
    orch, _repo, _wt, execution_store, _intents, task = build_postcommit(
        tmp_path,
        executor,
        approved_paths=("src/thing.py",),
    )
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase != Phase.NEEDS_USER.value, orch.state.question
    # The mutation-killing assertion, and the reason both gates are one task:
    # `candidate_sha != ""` alone would still pass with the POST-commit check
    # left blocking (the commit exists either way — only the park differs).
    # Reaching the packet is what proves the round was not parked downstream.
    assert "POST-COMMIT REVIEW PACKET" in (orch.state.outbox or "")

    execution = execution_store.load(task.id)
    assert execution.candidate_sha != "", "should have committed"
    assert execution.out_of_scope_paths == ("src/sneaky.py",)
    # Recorded, never granted: the out-of-scope path must not have widened the
    # task's own authorization (M1 finding #2/#3 is unchanged by this).
    assert "src/sneaky.py" not in execution.allowed_paths


def test_out_of_scope_paths_survive_a_store_round_trip_as_a_tuple(tmp_path):
    """It is read back by the packet renderer and by crash-recovery adoption,
    both of which load the record from disk. JSON has no tuples, so without
    the coercion in `TaskExecutionStore.load` this comes back a list."""
    executor = WritingExecutor(
        tmp_path / "worktrees",
        {"src/thing.py": "x\n", "src/sneaky.py": "y\n"},
    )
    orch, _repo, _wt, execution_store, _intents, task = build_postcommit(
        tmp_path, executor, approved_paths=("src/thing.py",)
    )
    orch._dispatch_executor(implement(task.id))

    reloaded = execution_store.load(task.id)
    assert reloaded.out_of_scope_paths == ("src/sneaky.py",)
    assert isinstance(reloaded.out_of_scope_paths, tuple)
    # An older record — written before the field existed — has no key at all
    # and must load as "nothing recorded", not raise.
    path = execution_store.directory / f"{task.id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["out_of_scope_paths"]
    path.write_text(json.dumps(data), encoding="utf-8")
    assert execution_store.load(task.id).out_of_scope_paths == ()


def test_a_clean_round_records_nothing_out_of_scope(tmp_path):
    """The negative control — without it the recording could be firing on
    every round and every assertion above would still pass."""
    executor = WritingExecutor(tmp_path / "worktrees", {"src/thing.py": "x\n"})
    orch, _repo, _wt, execution_store, _intents, task = build_postcommit(
        tmp_path, executor, approved_paths=("src/thing.py",)
    )
    orch._dispatch_executor(implement(task.id))

    assert execution_store.load(task.id).out_of_scope_paths == ()
