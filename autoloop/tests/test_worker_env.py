"""Worker reuse at dispatch (`worker_env.worker_repo_is_reusable` + the
resumed-record guard in `Orchestrator._dispatch_task_postcommit`).

ONE claim, pinned from both sides: when an execution record names a
`worktree_path` that exists, is a git repository, and is already checked out
on the recorded `task_branch`, dispatching that task again REUSES it — same
path, same branch, no new clone — AS IT STANDS: uncommitted partial work
left by an interrupted round is neither quarantined nor recreated away, it
is what the resumed executor picks back up (the reuse decision is carried
into `_prepare_write_capable_worker`, whose dirty-residue quarantine is
skipped for exactly this gate and nothing else). Anything else (missing
directory, plain non-git directory, wrong branch) is NOT reuse and falls back to the SAME
`WorkerRepoManager.create` call a first dispatch makes: a missing directory
is recreated at the recorded base/branch, while a path that exists but
fails the probe makes `create()` refuse with its usual "already exists"
error — no repair, no deletion, no branch switching, and the execution
record is never rewritten by either outcome.

Self-contained per this codebase's convention (see `test_postcommit_flow.py`'s
docstring) — real git repos throughout, no shared fixtures imported from
other test modules.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.errors import GitCommandError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore, mutation_ledger_for
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager, worker_repo_is_reusable
from autoloop.worktask import IntentStore, TaskExecutionStore

URL = "https://chatgpt.com/c/worker-reuse-test"


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
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def implement(task_id="t1") -> Directive:
    return Directive(decision=Decision.IMPLEMENT, reason="go", task_id=task_id)


# =============================================================================
# 1. the probe itself — `worker_repo_is_reusable`
# =============================================================================


def test_a_repo_on_exactly_the_recorded_branch_is_reusable(tmp_path):
    repo = real_repo(tmp_path)
    run_git(repo, "checkout", "-q", "-B", "autoloop/t1")
    assert worker_repo_is_reusable(repo, "autoloop/t1")


def test_a_missing_directory_is_not_reusable(tmp_path):
    assert not worker_repo_is_reusable(tmp_path / "never-created", "autoloop/t1")


def test_a_plain_directory_is_not_reusable(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "leftover.txt").write_text("not a repo\n", encoding="utf-8")
    assert not worker_repo_is_reusable(plain, "autoloop/t1")


def test_a_directory_inside_someone_elses_repo_is_not_reusable(tmp_path):
    """`--show-toplevel` is compared back against the probed path: a plain
    subdirectory that merely sits INSIDE a repository must not pass as
    being one."""
    repo = real_repo(tmp_path)
    inner = repo / "docs"
    inner.mkdir()
    assert not worker_repo_is_reusable(inner, "main")


def test_the_wrong_branch_is_not_reusable(tmp_path):
    repo = real_repo(tmp_path)
    run_git(repo, "checkout", "-q", "-B", "autoloop/other")
    assert not worker_repo_is_reusable(repo, "autoloop/t1")


def test_a_detached_head_is_not_reusable(tmp_path):
    repo = real_repo(tmp_path)
    run_git(repo, "checkout", "-q", "--detach")
    assert not worker_repo_is_reusable(repo, "main")


def test_an_empty_recorded_branch_never_matches(tmp_path):
    """A record with no branch must not "match" a detached HEAD's empty
    `--show-current` output."""
    repo = real_repo(tmp_path)
    run_git(repo, "checkout", "-q", "--detach")
    assert not worker_repo_is_reusable(repo, "")


# =============================================================================
# 2. dispatch — reuse vs the existing creation path
# =============================================================================


class ScriptedWriter:
    """Writes `round<N>.py` into the task's worker repo on call N and
    reports success. Duplicated per this package's self-contained test
    convention."""

    def __init__(self, workers_root):
        self.workers_root = Path(workers_root)
        self.calls = 0

    def execute(self, directive, task):
        self.calls += 1
        name = f"round{self.calls}.py"
        (self.workers_root / task.id / name).write_text(
            f"round {self.calls}\n", encoding="utf-8"
        )
        return ExecutionOutcome(
            status="ok",
            summary=f"round {self.calls}",
            validation="ok",
            changed_paths=(name,),
        )


def build_orchestrator(tmp_path, task_id="t1", executor=None):
    repo_root = real_repo(tmp_path)
    # Mirrors the real repo's own `.gitignore` — without it, `.al/state.json`
    # dirties the primary checkout and trips a precondition unrelated to
    # what these tests check (see `test_m1_hardening.py`'s builder).
    (repo_root / ".gitignore").write_text(
        ".al/\n__pycache__/\n*.py[cod]\n", encoding="utf-8"
    )
    run_git(repo_root, "add", ".gitignore")
    run_git(repo_root, "commit", "-q", "-m", "gitignore state dir")
    workers_root = tmp_path / "workers_root"
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig(implement_enabled=True)))
    worker_repos = WorkerRepoManager(workers_root, tmp_path / "worker-hooks")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    intent_store = IntentStore(tmp_path / "intents")

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=repo_root / ".al",
        workers_root=workers_root,
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)
    task = Task(
        id=task_id, title="T", description="d",
        approved_paths=("round1.py", "round2.py"),
    )
    registry = TaskRegistry([task])
    task_store = TaskStore(
        config.tasks_file,
        ledger=mutation_ledger_for(config.workers_root, config.state_dir),
    )
    task_store.save(registry)

    def no_client():
        raise AssertionError("no browser client expected in this test")

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=executor if executor is not None else ScriptedWriter(workers_root),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worker_repos=worker_repos,
        execution_store=execution_store,
        intent_store=intent_store,
        validation_runner=ok_validation,
    )
    return orch, config, execution_store, task


def count_creates(orch):
    """Shadow `WorkerRepoManager.create` on this instance with a recording
    wrapper — the direct proof of "no new clone"."""
    calls = []
    real_create = orch._worker_repos.create

    def counting(*args, **kwargs):
        calls.append((args, kwargs))
        return real_create(*args, **kwargs)

    orch._worker_repos.create = counting
    return calls


def entries(config, entry_type):
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if r["type"] == entry_type]


def test_a_second_dispatch_reuses_the_existing_worker_with_no_new_clone(tmp_path):
    """THE claim: valid existing worker -> reused as it stands. One create
    across two dispatches, same path, same branch, nothing quarantined, and
    round 2's candidate is built ON round 1's commit — content that only
    the reused worker held."""
    orch, config, execution_store, task = build_orchestrator(tmp_path)
    creates = count_creates(orch)

    orch._dispatch_executor(implement(task.id))
    first = execution_store.load(task.id)
    assert len(creates) == 1
    assert first.candidate_sha != ""
    worker_path = Path(first.worktree_path)
    assert worker_path.is_dir()

    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement(task.id))

    assert len(creates) == 1, "the second dispatch cloned a new worker"
    second = execution_store.load(task.id)
    assert second.worktree_path == first.worktree_path
    assert second.task_branch == first.task_branch
    assert second.task_base_sha == first.task_base_sha
    assert run_git(worker_path, "branch", "--show-current").strip() == first.task_branch
    assert not (orch._worker_repos.root_dir.parent / "quarantine").exists()
    assert entries(config, "worker_recreated") == []

    # Round 2 really did build on the reused worker: its candidate contains
    # BOTH rounds' files, and round 1's commit is its ancestor.
    worker_git = GitGateway(worker_path, PolicyEngine(PolicyConfig()))
    touched = worker_git.commit_range_paths(second.task_base_sha, second.candidate_sha)
    assert touched == {"round1.py", "round2.py"}


def test_partial_uncommitted_work_in_the_reused_worker_survives_a_resumed_dispatch(tmp_path):
    """THE incident wrk-01 exists for: an interrupted round leaves partial
    uncommitted work — a tracked edit and an untracked file — in a worker
    that still passes every reuse predicate (recorded path, a git repo, on
    the recorded branch). Dispatching again must resume THAT worker as it
    stands: one create across both dispatches, same path, same branch,
    nothing quarantined, and the partial edits still on disk, byte for
    byte, when the resumed executor starts. The executor here mirrors the
    real `ImplementExecutor` by reporting every dirty path (it derives
    `changed_paths` from `dirty_entries_all()`), so the resumed round's
    commit carries the partial work forward and the round completes."""

    class ResumingWriter:
        """Call 1 commits round1.py. Call 2 records exactly what it finds
        on entry (the interrupted round's partial work), finishes it, and
        reports every dirty path."""

        def __init__(self, workers_root):
            self.workers_root = Path(workers_root)
            self.calls = 0
            self.seen_on_entry = {}

        def execute(self, directive, task):
            self.calls += 1
            wt = self.workers_root / task.id
            if self.calls == 1:
                (wt / "round1.py").write_text("round 1\n", encoding="utf-8")
                return ExecutionOutcome(
                    status="ok", summary="round 1", validation="ok",
                    changed_paths=("round1.py",),
                )
            for name in ("round1.py", "round2.py"):
                p = wt / name
                self.seen_on_entry[name] = (
                    p.read_text(encoding="utf-8") if p.exists() else None
                )
            (wt / "round2.py").write_text(
                self.seen_on_entry["round2.py"] + "resumed and finished\n",
                encoding="utf-8",
            )
            return ExecutionOutcome(
                status="ok", summary="round 2 resumed", validation="ok",
                changed_paths=("round1.py", "round2.py"),
            )

    executor = ResumingWriter(tmp_path / "workers_root")
    orch, config, execution_store, task = build_orchestrator(tmp_path, executor=executor)
    creates = count_creates(orch)

    orch._dispatch_executor(implement(task.id))
    first = execution_store.load(task.id)
    assert len(creates) == 1
    assert first.candidate_sha != ""
    worker_path = Path(first.worktree_path)

    # The interrupted round's residue: a TRACKED partial edit and an
    # UNTRACKED partial file, left uncommitted between dispatches.
    (worker_path / "round1.py").write_text(
        "round 1\npartial tracked edit\n", encoding="utf-8"
    )
    (worker_path / "round2.py").write_text(
        "partial untracked work\n", encoding="utf-8"
    )

    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement(task.id))

    assert len(creates) == 1, "the resumed dispatch cloned a new worker"
    second = execution_store.load(task.id)
    assert second.worktree_path == first.worktree_path
    assert second.task_branch == first.task_branch
    assert run_git(worker_path, "branch", "--show-current").strip() == first.task_branch
    assert not (orch._worker_repos.root_dir.parent / "quarantine").exists()
    assert entries(config, "worker_quarantined") == []
    assert entries(config, "worker_recreated") == []

    # The resumed executor really saw the partial work, byte for byte.
    assert executor.seen_on_entry == {
        "round1.py": "round 1\npartial tracked edit\n",
        "round2.py": "partial untracked work\n",
    }

    # And the round completed ON TOP of it: the partial edits are in the
    # new candidate — not reverted, not quarantined, not parked.
    assert orch.state.phase == Phase.READY.value
    assert second.candidate_sha not in ("", first.candidate_sha)
    assert run_git(worker_path, "show", f"{second.candidate_sha}:round1.py") == (
        "round 1\npartial tracked edit\n"
    )
    assert run_git(worker_path, "show", f"{second.candidate_sha}:round2.py") == (
        "partial untracked work\nresumed and finished\n"
    )


def test_a_missing_worker_directory_is_recreated_by_the_existing_creation_path(tmp_path):
    """Not a reuse case: the recorded path is gone. Dispatch falls back to
    the same `create()` a first dispatch uses — recorded base, recorded
    branch, same path — and the record itself is not rewritten."""
    orch, config, execution_store, task = build_orchestrator(tmp_path)
    creates = count_creates(orch)

    orch._dispatch_executor(implement(task.id))
    first = execution_store.load(task.id)
    worker_path = Path(first.worktree_path)
    shutil.rmtree(worker_path)

    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement(task.id))

    assert len(creates) == 2, "a missing worker must be recreated"
    args, kwargs = creates[1]
    assert args[0] == task.id
    assert args[2] == first.task_base_sha, "recreated at the RECORDED base"
    assert kwargs.get("branch") == first.task_branch, "onto the RECORDED branch"

    second = execution_store.load(task.id)
    assert second.worktree_path == first.worktree_path
    assert second.task_branch == first.task_branch
    assert second.task_base_sha == first.task_base_sha
    assert worker_path.is_dir()
    assert run_git(worker_path, "branch", "--show-current").strip() == first.task_branch
    assert [e["data"]["task_id"] for e in entries(config, "worker_recreated")] == [task.id]
    # The recreated worker starts from the recorded base, so round 2's
    # candidate is a fresh commit there — the dispatch completed.
    assert second.candidate_sha not in ("", first.candidate_sha)


def test_a_worker_on_the_wrong_branch_is_not_reused_and_is_not_repaired(tmp_path):
    """Bounds: wrong branch is not a reuse case, and repair is out of scope.
    The fallback is the existing creation path, whose own precondition
    refuses because something already sits at the path — fail closed, with
    the worker left byte-for-byte where it was: same branch, not deleted,
    not quarantined, record untouched."""
    orch, config, execution_store, task = build_orchestrator(tmp_path)
    creates = count_creates(orch)

    orch._dispatch_executor(implement(task.id))
    first = execution_store.load(task.id)
    worker_path = Path(first.worktree_path)
    run_git(worker_path, "checkout", "-q", "-B", "elsewhere")

    orch.state.phase = Phase.READY.value
    with pytest.raises(GitCommandError, match="already exists"):
        orch._dispatch_executor(implement(task.id))

    assert len(creates) == 2, "the fallback went through create(), which refused"
    assert run_git(worker_path, "branch", "--show-current").strip() == "elsewhere"
    assert not (orch._worker_repos.root_dir.parent / "quarantine").exists()
    after = execution_store.load(task.id)
    assert after.worktree_path == first.worktree_path
    assert after.task_branch == first.task_branch
    assert after.attempt_count == first.attempt_count, "the refusal spent nothing"


def test_a_non_git_directory_at_the_recorded_path_is_not_reused_or_deleted(tmp_path):
    """Bounds: a plain directory at the recorded path is not a reuse case
    either — same fallback, same `create()` refusal, and the directory's
    contents survive untouched."""
    orch, config, execution_store, task = build_orchestrator(tmp_path)
    creates = count_creates(orch)

    orch._dispatch_executor(implement(task.id))
    first = execution_store.load(task.id)
    worker_path = Path(first.worktree_path)
    shutil.rmtree(worker_path)
    worker_path.mkdir()
    (worker_path / "evidence.txt").write_text("not a repo\n", encoding="utf-8")

    orch.state.phase = Phase.READY.value
    with pytest.raises(GitCommandError, match="already exists"):
        orch._dispatch_executor(implement(task.id))

    assert len(creates) == 2, "the fallback went through create(), which refused"
    assert (worker_path / "evidence.txt").read_text(encoding="utf-8") == "not a repo\n"
    after = execution_store.load(task.id)
    assert after.worktree_path == first.worktree_path
    assert after.task_branch == first.task_branch
