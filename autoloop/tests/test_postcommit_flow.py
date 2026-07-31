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
    tmp_path, executor, task_id="t1", validation_runner=ok_validation, approved_paths=None
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


# ---- 6. hook adds an unexpected path -> refused --------------------------------


def test_hook_adding_unexpected_path_is_refused(tmp_path):
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

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert "REFUSED" in orch.state.question
    assert "sneaked.txt" in orch.state.question
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
