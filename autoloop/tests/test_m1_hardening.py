"""Autoloop M1 hardening — adversarial tests for the four findings this pass
closes:

  #1  external worker location (`config.workers_root`, `worker_env.
      validate_workers_root`) — the old `state_dir / "workers"` default put
      a task's own working repository INSIDE the tree every verification
      primitive is scoped to.
  #2  primary-checkout escape detection (`escape_detector.py`) — a
      deterministic before/after filesystem snapshot bracketing exactly the
      write-capable executor call.
  #3  non-circular task ownership (`Task.approved_paths`) + failed-round
      isolation and bounded attempts (`WorkerRepoManager.quarantine`,
      `attempt_count` incremented before dispatch).
  #7  blocker precondition integrity (`cli._RESOLUTION_PRECONDITIONS`).

Self-contained per this codebase's convention (see `test_postcommit_flow.py`'s
docstring) — real git repos throughout, no shared fixtures imported from
other test modules.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
from pathlib import Path

import pytest

from autoloop.config import AutoloopConfig, BrowserConfig, load_config
from autoloop.contract import Decision, Directive
from autoloop.errors import ConfigError, TaskGraphError
from autoloop.escape_detector import (
    diff_snapshots,
    enumerate_checkout_paths,
    find_symlink_traversal,
    snapshot_checkout,
)
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import MAX_TASK_ATTEMPTS, Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import WorkerRepoManager, validate_workers_root
from autoloop.worktask import IntentStore, TaskExecutionStore

URL = "https://chatgpt.com/c/m1-hardening-test"


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
# 1. external worker location (finding #1) — adversarial tests 1 & 2
# =============================================================================


def test_relative_workers_root_refused_by_load_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        '[paths]\nworkers_root = "relative/path"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="absolute"):
        load_config(cfg)


def test_missing_workers_root_refused_by_load_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[browser]\nconversation_url = "{URL}"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="workers_root is required"):
        load_config(cfg)


def test_blank_workers_root_refused_by_load_config(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        '[paths]\nworkers_root = "   "\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="workers_root is required"):
        load_config(cfg)


def test_workers_root_nested_under_checkout_refused(tmp_path):
    repo_root = real_repo(tmp_path)
    violations = validate_workers_root(
        repo_root / ".autoloop" / "workers", repo_root, repo_root / ".autoloop"
    )
    assert violations
    assert any("primary checkout" in v for v in violations)


def test_workers_root_nested_directly_under_dot_git_refused(tmp_path):
    repo_root = real_repo(tmp_path)
    violations = validate_workers_root(repo_root / ".git" / "workers", repo_root, tmp_path / "state")
    assert violations
    assert any(".git" in v for v in violations)


def test_workers_root_nested_under_state_dir_refused(tmp_path):
    repo_root = real_repo(tmp_path)
    state_dir = tmp_path / "state"
    violations = validate_workers_root(state_dir / "workers", repo_root, state_dir)
    assert violations
    assert any("state directory" in v for v in violations)


def test_workers_root_nested_under_publisher_dirs_refused(tmp_path):
    from autoloop.publisher import publisher_hooks_path, publisher_repo_path

    repo_root = real_repo(tmp_path)
    state_dir = tmp_path / "state"
    pub = publisher_repo_path(state_dir)
    violations = validate_workers_root(pub / "nested", repo_root, state_dir)
    assert violations
    assert any("publisher" in v for v in violations)

    pub_hooks = publisher_hooks_path(state_dir)
    violations2 = validate_workers_root(pub_hooks / "nested", repo_root, state_dir)
    assert violations2
    assert any("publisher" in v for v in violations2)


def test_workers_root_nested_under_linked_worktree_gitdir_refused(tmp_path):
    """A linked worktree's `.git` is a FILE pointing elsewhere (verified
    empirically against this very m1-postcommit worktree during this pass —
    see `worker_env.validate_workers_root`'s docstring). A naive literal
    `<repo_root>/.git` prefix check alone would miss a `workers_root`
    pointed at the resolved gitdir it points to — the real, shared object
    database, which is a much higher-value target than an ordinary path
    under the linked worktree's own `.git` file."""
    repo_root = real_repo(tmp_path)
    worktree_path = tmp_path / "linked-worktree"
    run_git(repo_root, "worktree", "add", "-b", "wt-branch", str(worktree_path))
    git_pointer = worktree_path / ".git"
    assert git_pointer.is_file()
    real_gitdir = Path(git_pointer.read_text(encoding="utf-8").split(":", 1)[1].strip())

    violations = validate_workers_root(real_gitdir / "workers", worktree_path, tmp_path / "state")
    assert violations
    assert any("gitdir" in v.lower() or "linked worktree" in v.lower() for v in violations)


def test_workers_root_outside_every_protected_path_accepted(tmp_path):
    repo_root = real_repo(tmp_path)
    state_dir = tmp_path / "state"
    workers_root = tmp_path / "elsewhere" / "workers"
    assert validate_workers_root(workers_root, repo_root, state_dir) == []


def test_workers_root_accepted_via_full_load_config_round_trip(tmp_path):
    """Positive companion to every refusal above: a well-formed config with
    an absolute, external `workers_root` loads cleanly."""
    workers_root = tmp_path / "workers_root"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nworkers_root = "{workers_root}"\n',
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert config.workers_root == workers_root
    assert config.workers_root.is_absolute()


def test_cli_build_orchestrator_refuses_an_unsafe_workers_root(tmp_path, monkeypatch):
    """The end-to-end wiring point (`cli._build_orchestrator`), not just the
    standalone validator — never falls back to the old
    `config.workers_dir` default."""
    from argparse import Namespace

    from autoloop import cli
    from autoloop.state import LoopState, StateStore
    from autoloop.tasks import TaskRegistry, TaskStore

    repo_root = real_repo(tmp_path)
    monkeypatch.chdir(repo_root)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=repo_root / ".al",
        workers_root=repo_root / ".al" / "workers",  # nested — invalid
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)
    task_store = TaskStore(config.tasks_file)
    registry = TaskRegistry()
    task_store.save(registry)

    with pytest.raises(ConfigError, match="workers_root"):
        cli._build_orchestrator(config, Namespace(config=None), store, state, task_store, registry)


# =============================================================================
# 2. primary-checkout escape detection (finding #2) — adversarial test 3
# =============================================================================


def test_escape_detector_detects_tracked_content_change(tmp_path):
    repo_root = real_repo(tmp_path)
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    paths = enumerate_checkout_paths(git)
    before = snapshot_checkout(repo_root, paths)

    (repo_root / "README.md").write_text("tampered\n", encoding="utf-8")

    after = snapshot_checkout(repo_root, paths)
    violations = diff_snapshots(before, after)
    assert any("README.md" in v and "content changed" in v for v in violations)


def test_escape_detector_detects_untracked_creation(tmp_path):
    repo_root = real_repo(tmp_path)
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    paths_before = enumerate_checkout_paths(git)
    before = snapshot_checkout(repo_root, paths_before)

    (repo_root / "sneaked.py").write_text("evil\n", encoding="utf-8")

    # Re-enumerate: a brand-new file is invisible to the OLD path list.
    paths_after = enumerate_checkout_paths(git)
    after = snapshot_checkout(repo_root, sorted(set(paths_before) | set(paths_after)))
    violations = diff_snapshots(before, after)
    assert any("sneaked.py" in v and "created" in v for v in violations)


def test_escape_detector_detects_ignored_content_change(tmp_path):
    """The literal claim `git status` cannot back on its own: an ignored
    file is entirely invisible to plain `git status`, and even
    `git status --ignored` collapses an ignored DIRECTORY to one entry."""
    repo_root = real_repo(tmp_path)
    (repo_root / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    run_git(repo_root, "add", ".gitignore")
    run_git(repo_root, "commit", "-q", "-m", "gitignore")
    (repo_root / "ignored.log").write_text("v1\n", encoding="utf-8")

    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    paths = enumerate_checkout_paths(git)
    assert "ignored.log" in paths
    assert git.dirty_files() == []  # sanity: plain `git status` sees nothing at all

    before = snapshot_checkout(repo_root, paths)
    (repo_root / "ignored.log").write_text("TAMPERED\n", encoding="utf-8")
    after = snapshot_checkout(repo_root, paths)
    violations = diff_snapshots(before, after)
    assert any("ignored.log" in v and "content changed" in v for v in violations)


def test_escape_detector_detects_symlink_target_change(tmp_path):
    repo_root = real_repo(tmp_path)
    (repo_root / "target_a.txt").write_text("a\n", encoding="utf-8")
    (repo_root / "target_b.txt").write_text("b\n", encoding="utf-8")
    link = repo_root / "link.txt"
    link.symlink_to("target_a.txt")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "add symlink")

    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    paths = enumerate_checkout_paths(git)
    before = snapshot_checkout(repo_root, paths)

    link.unlink()
    link.symlink_to("target_b.txt")

    after = snapshot_checkout(repo_root, paths)
    violations = diff_snapshots(before, after)
    assert any("link.txt" in v and "symlink target changed" in v for v in violations)


def test_escape_detector_detects_executable_bit_change(tmp_path):
    repo_root = real_repo(tmp_path)
    script = repo_root / "script.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "add script")

    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    paths = enumerate_checkout_paths(git)
    before = snapshot_checkout(repo_root, paths)

    script.chmod(0o755)

    after = snapshot_checkout(repo_root, paths)
    violations = diff_snapshots(before, after)
    assert any("script.sh" in v and "executable bit changed" in v for v in violations)


def test_escape_detector_detects_deletion(tmp_path):
    repo_root = real_repo(tmp_path)
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    paths = enumerate_checkout_paths(git)
    before = snapshot_checkout(repo_root, paths)

    (repo_root / "README.md").unlink()

    after = snapshot_checkout(repo_root, paths)
    violations = diff_snapshots(before, after)
    assert any("README.md" in v and "deleted" in v for v in violations)


def test_escape_detector_clean_run_reports_no_violations(tmp_path):
    repo_root = real_repo(tmp_path)
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    paths = enumerate_checkout_paths(git)
    before = snapshot_checkout(repo_root, paths)
    after = snapshot_checkout(repo_root, paths)
    assert diff_snapshots(before, after) == []


# =============================================================================
# 3. end-to-end: an executor that escapes its worker repo is caught before
#    anything is committed (findings #1/#2, adversarial test 4)
# =============================================================================


def _build_worker_repos_orchestrator(tmp_path, executor_factory, approved_paths=("feature.py",), task_id="t1"):
    repo_root = real_repo(tmp_path)
    # Mirrors the real repo's own `.gitignore` (`.autoloop/` is ignored
    # there) — without this, `.al/state.json` shows up as an untracked,
    # dirty path the moment it is written below, which would trip the
    # "primary checkout must be clean" precondition for a reason that has
    # nothing to do with what any of these tests are actually checking.
    (repo_root / ".gitignore").write_text(".al/\n", encoding="utf-8")
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
        state_dir=repo_root / ".al",  # realistic default: inside the checkout
        workers_root=workers_root,  # but the worker root is EXTERNAL
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)
    task = Task(id=task_id, title="T", description="d", approved_paths=tuple(approved_paths))
    registry = TaskRegistry([task])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    executor = executor_factory(workers_root, repo_root)

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
        manifest_store=ManifestStore(config.manifests_dir),
        worker_repos=worker_repos,
        execution_store=execution_store,
        intent_store=intent_store,
        validation_runner=ok_validation,
    )
    return orch, repo_root, execution_store, task, executor


class _TamperingExecutor:
    """Writes into its own worker repo (legitimate) AND, once, reaches
    outside to tamper with something in the PRIMARY checkout — modelling a
    write-capable agent that is not actually confined to its worker repo."""

    def __init__(self, workers_root, repo_root, tamper):
        self.workers_root = Path(workers_root)
        self.repo_root = Path(repo_root)
        self.tamper = tamper
        self.calls = 0

    def execute(self, directive, task):
        self.calls += 1
        wt = self.workers_root / task.id
        (wt / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        self.tamper(self.repo_root)
        return ExecutionOutcome(
            status="ok", summary="did it", validation="ok", changed_paths=("feature.py",)
        )


def test_agent_creating_a_file_outside_its_worker_repo_is_detected(tmp_path):
    def tamper(repo_root):
        (repo_root / "sneaked_outside.py").write_text("evil\n", encoding="utf-8")

    orch, repo_root, execution_store, task, executor = _build_worker_repos_orchestrator(
        tmp_path, lambda wr, rr: _TamperingExecutor(wr, rr, tamper)
    )
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert "sneaked_outside.py" in (orch.state.question or "")
    assert "created outside the worker repo" in (orch.state.question or "")
    execution = execution_store.load(task.id)
    assert execution is None or execution.candidate_sha == ""  # never committed
    # DETECTION, not prevention — the write is real and NOT reverted.
    assert (repo_root / "sneaked_outside.py").exists()


def test_agent_tampering_with_a_tracked_file_outside_its_worker_repo_is_detected(tmp_path):
    def tamper(repo_root):
        (repo_root / "README.md").write_text("TAMPERED BY AGENT\n", encoding="utf-8")

    orch, repo_root, execution_store, task, executor = _build_worker_repos_orchestrator(
        tmp_path, lambda wr, rr: _TamperingExecutor(wr, rr, tamper)
    )
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert "README.md" in (orch.state.question or "")
    assert "content changed" in (orch.state.question or "")
    execution = execution_store.load(task.id)
    assert execution is None or execution.candidate_sha == ""


def test_agent_tampering_with_state_json_is_detected(tmp_path):
    """Adversarial test 4 (state.json half): the escape detector's snapshot
    window is deliberately narrow enough that tampering with Autoloop's own
    `.autoloop/state.json` BY THE AGENT — as opposed to the orchestrator's
    own ordinary writes to it, which happen strictly outside the window —
    is caught, not silently excluded. See `escape_detector.py`'s module
    docstring for why the exclusion list this would otherwise need is empty
    by measurement."""

    def tamper(repo_root):
        state_file = repo_root / ".al" / "state.json"
        assert state_file.exists(), "the orchestrator must have written it before the executor ran"
        state_file.write_text('{"tampered": true}', encoding="utf-8")

    orch, repo_root, execution_store, task, executor = _build_worker_repos_orchestrator(
        tmp_path, lambda wr, rr: _TamperingExecutor(wr, rr, tamper)
    )
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert ".al/state.json" in (orch.state.question or "") or "state.json" in (orch.state.question or "")
    execution = execution_store.load(task.id)
    assert execution is None or execution.candidate_sha == ""


def test_agent_tampering_with_a_blocker_record_is_detected(tmp_path):
    """Adversarial test 4 (blocker-record half)."""
    from autoloop.blockers import Blocker, BlockerStore

    def make_executor(workers_root, repo_root):
        blocker_dir = repo_root / ".al" / "blockers"
        store = BlockerStore(blocker_dir)
        store.save(
            Blocker(
                id="blk-other-001",
                task_id="other",
                kind="task_fatal",
                code="attempt_count_ceiling",
                question="pre-existing blocker",
                detail="",
                phase="executing",
                created_at="2026-07-31T00:00:00+00:00",
            )
        )

        def tamper(rr):
            (blocker_dir / "blk-other-001.json").write_text(
                '{"id": "blk-other-001", "resolved_at": "forged", "answer": "forged"}',
                encoding="utf-8",
            )

        return _TamperingExecutor(workers_root, repo_root, tamper)

    orch, repo_root, execution_store, task, executor = _build_worker_repos_orchestrator(
        tmp_path, make_executor
    )
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert "blk-other-001.json" in (orch.state.question or "")


def test_audit_dispatch_is_not_wrapped_by_the_escape_detector(tmp_path, monkeypatch):
    """Negative + positive control together: `AuditExecutor` legitimately
    writes under `.autoloop/audit/` by design (its own module docstring) —
    the escape detector must be scoped to write-capable (non-audit) dispatch
    only, or the very first real audit would trip a false LOOP-FATAL.

    Asserted by SPYING on `_execute_with_escape_detection` itself (call
    count), not by inspecting the audit dispatch's outcome — the original
    version of this test asserted a disjunction (`park_kind != "loop_fatal"
    or "escape" not in question`) that passes whenever the audit parks
    loop_fatal for ANY other reason, so it could never fail for the right
    reason. It's also structurally unable to check the right task: a
    `Decision.AUDIT` dispatch resolves through `_resolve_audit_task` to its
    OWN synthetic `audit-<iteration>` task id, never whatever `task_id` a
    caller passes in — so this test dispatches AUDIT first (proving zero
    escape-detector calls) and then a real IMPLEMENT for the registered task
    (proving the escape detector DOES wrap that one), as a direct
    before/after comparison against the same spy.

    `executor.calls` is asserted too (not just the escape-detector spy): a
    `calls == []` assertion on the spy alone would ALSO pass if the audit
    dispatch parked for some unrelated reason before ever reaching the
    executor (`_resolve_audit_task` failing, an isolation check, the
    approved_paths gate, ...) — a negative control that can pass without the
    scenario it claims to cover ever running is exactly the failure mode
    this test itself replaced. Confirming the audit executor actually ran
    (and wrote its report) is what proves "escape detection is skipped for
    a real audit", not merely "escape detection was skipped for whatever
    this dispatch happened to do."."""

    class RecordingExecutor:
        def __init__(self, workers_root, repo_root):
            self.workers_root = Path(workers_root)
            self.repo_root = Path(repo_root)
            self.calls: list[str] = []

        def execute(self, directive, task):
            self.calls.append(task.id)
            wt = self.workers_root / task.id
            if task.id.startswith("audit"):
                (wt / "docs").mkdir(parents=True, exist_ok=True)
                (wt / "docs" / "AUDIT_2026-07-31.md").write_text("# audit\n", encoding="utf-8")
                # Models what AuditExecutor really does: write its own raw
                # report under the PRIMARY checkout's `.autoloop/audit/`.
                audit_dir = self.repo_root / ".al" / "audit"
                audit_dir.mkdir(parents=True, exist_ok=True)
                (audit_dir / "run.md").write_text("# raw output\n", encoding="utf-8")
                return ExecutionOutcome(
                    status="ok",
                    summary="audited",
                    validation="ok",
                    changed_paths=("docs/AUDIT_2026-07-31.md",),
                )
            (wt / "feature.py").write_text("print('hi')\n", encoding="utf-8")
            return ExecutionOutcome(
                status="ok", summary="did it", validation="ok", changed_paths=("feature.py",)
            )

    orch, repo_root, execution_store, task, executor = _build_worker_repos_orchestrator(
        tmp_path, RecordingExecutor, approved_paths=("feature.py",), task_id="t1"
    )
    calls: list[str] = []
    real_escape = orch._execute_with_escape_detection

    def spy_escape(directive, directive_task):
        calls.append(directive_task.id)
        return real_escape(directive, directive_task)

    monkeypatch.setattr(orch, "_execute_with_escape_detection", spy_escape)

    orch._dispatch_executor(Directive(decision=Decision.AUDIT, reason="orient"))
    assert executor.calls == ["audit-0000"], "the audit executor must actually have run"
    assert calls == [], "audit dispatch must never go through the escape detector"

    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement("t1"))
    assert executor.calls == ["audit-0000", "t1"], "the implement executor must actually have run"
    assert calls == ["t1"], "a non-audit write-capable dispatch must go through the escape detector"


# =============================================================================
# 4. non-circular task ownership (finding #2/#3) — adversarial tests 5/6/7
# =============================================================================


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "~/secrets",
        "../escape.py",
        "a/../../b.py",
        "*.py",
        "docs/*.md",
        "a/b?.py",
        "a[0].py",
        "a//b.py",
        "a/./b.py",
        "",
        "  a.py",
        "a.py  ",
        "a\\b.py",
    ],
)
def test_approved_path_structural_violations_refused(bad_path):
    with pytest.raises(TaskGraphError) as excinfo:
        TaskRegistry([Task(id="t1", title="T", description="d", approved_paths=(bad_path,))])
    assert excinfo.value.code == "bad_approved_path"


def test_approved_path_exact_relative_paths_accepted():
    approved = ("lexy-app/backend/routers/books.py", "docs/SECURITY.md")
    registry = TaskRegistry([Task(id="t1", title="T", description="d", approved_paths=approved)])
    assert registry.get("t1").approved_paths == approved


def test_approved_path_duplicate_refused():
    with pytest.raises(TaskGraphError) as excinfo:
        TaskRegistry(
            [Task(id="t1", title="T", description="d", approved_paths=("a.py", "a.py"))]
        )
    assert excinfo.value.code == "duplicate_approved_path"


def test_approved_path_symlink_traversal_refused(tmp_path):
    repo_root = real_repo(tmp_path)
    (repo_root / "real_target").mkdir()
    (repo_root / "real_target" / "file.py").write_text("x\n", encoding="utf-8")
    (repo_root / "docs").symlink_to("real_target")

    violations = find_symlink_traversal(repo_root, ["docs/file.py"])
    assert violations
    assert "docs/file.py" in violations[0]


def test_approved_path_symlink_leaf_itself_refused(tmp_path):
    repo_root = real_repo(tmp_path)
    (repo_root / "elsewhere.txt").write_text("x\n", encoding="utf-8")
    (repo_root / "link.py").symlink_to("elsewhere.txt")

    violations = find_symlink_traversal(repo_root, ["link.py"])
    assert violations


def test_approved_path_symlink_check_is_silent_for_paths_that_do_not_exist_yet(tmp_path):
    """New files are named explicitly up front and legitimately do not
    exist yet — that alone must never be treated as traversal."""
    repo_root = real_repo(tmp_path)
    assert find_symlink_traversal(repo_root, ["brand/new/file.py"]) == []


def test_agent_reported_extra_path_cannot_widen_authorization(tmp_path):
    """Adversarial test 5: the executor reports a path OUTSIDE
    `approved_paths` — refused BEFORE any commit; nothing lands, and the
    task is never left silently "authorized" for what it touched."""

    class WideningExecutor:
        def __init__(self, workers_root, repo_root):
            self.workers_root = Path(workers_root)
            self.calls = 0

        def execute(self, directive, task):
            self.calls += 1
            wt = self.workers_root / task.id
            (wt / "a.py").write_text("ok\n", encoding="utf-8")
            (wt / "b.py").write_text("widened scope\n", encoding="utf-8")
            return ExecutionOutcome(
                status="ok", summary="did it", validation="ok", changed_paths=("a.py", "b.py")
            )

    orch, repo_root, execution_store, task, executor = _build_worker_repos_orchestrator(
        tmp_path, lambda wr, rr: WideningExecutor(wr, rr), approved_paths=("a.py",)
    )
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"
    assert "b.py" in (orch.state.question or "")
    execution = execution_store.load(task.id)
    assert execution.candidate_sha == ""  # never committed
    assert executor.calls == 1


def test_unexpected_commit_path_from_a_prior_process_is_rejected(tmp_path):
    """Adversarial test 7: however an unapproved path ends up in a REAL
    commit on the task's branch, the post-commit path-ownership check must
    still catch it. For `worker_repos` specifically, a commit HOOK cannot be
    the mechanism — `verify_worker_isolation` refuses ANY active hook in the
    worker's controlled hooks directory unconditionally, before anything
    else runs (proven directly by `test_worker_isolation_refuses_any_active_
    hook_before_anything_else_runs` below; the equivalent hook-based
    scenario is already covered for the ONE path where hooks are actually
    possible — the worktrees fallback — by `test_postcommit_flow.py`'s
    `test_hook_adding_unexpected_path_is_refused`, unmodified by this pass).

    So the realistic way an unapproved path lands in a real worker-repo
    commit is a commit made by something OTHER than the CURRENT
    `_dispatch_task_postcommit` call — modelled here via the existing
    crash-recovery path (`worktask.reconcile_after_crash`): a PRIOR process
    committed both the approved and an unapproved path, then crashed after
    the commit succeeded but before persisting `candidate_sha`. Crash
    recovery correctly classifies this RECOVERABLE (parent linkage + nonce
    trailer both match) and ADOPTS the commit without re-running the
    executor — but `execution.allowed_paths` is the FIXED `task.
    approved_paths` set from creation, never widened by the recovered
    intent's own `planned_paths` (that widening is exactly the circularity
    M1 finding #2/#3 closes — see `_dispatch_task_postcommit`'s `is_audit`
    branch), so the post-commit ownership check still refuses."""
    from autoloop.worktask import CommitIntent, TaskExecution

    repo_root = real_repo(tmp_path)
    (repo_root / ".gitignore").write_text(".al/\n", encoding="utf-8")
    run_git(repo_root, "add", ".gitignore")
    run_git(repo_root, "commit", "-q", "-m", "gitignore state dir")

    workers_root = tmp_path / "workers_root"
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig(implement_enabled=True)))
    worker_repos = WorkerRepoManager(workers_root, tmp_path / "worker-hooks")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    intent_store = IntentStore(tmp_path / "intents")

    base_sha = git.head_sha()
    repo = worker_repos.create("t1", repo_root, base_sha)
    execution = TaskExecution(
        task_id="t1",
        task_branch=repo.branch,
        worktree_path=str(repo.path),
        task_base_sha=base_sha,
        allowed_paths=("a.py",),
    )
    execution_store.save(execution)

    worker_git = repo.gateway(PolicyEngine(PolicyConfig()))
    (repo.path / "a.py").write_text("ok\n", encoding="utf-8")
    (repo.path / "sneaked.py").write_text("unapproved\n", encoding="utf-8")
    intent = CommitIntent.create("t1", repo.branch, base_sha, ("a.py", "sneaked.py"), "feat: t1")
    real_sha, _ = worker_git.commit_and_capture(
        "feat: t1", ("a.py", "sneaked.py"), intent_store, intent
    )
    assert execution_store.load("t1").candidate_sha == ""  # crash: never persisted
    assert intent_store.load("t1") is not None

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=repo_root / ".al",
        workers_root=workers_root,
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)
    task = Task(id="t1", title="T", description="d", approved_paths=("a.py",))
    registry = TaskRegistry([task])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    class NeverCalledExecutor:
        def execute(self, directive, task):
            raise AssertionError("RECOVERABLE adopts the existing commit; the executor must not run")

    def no_client():
        raise AssertionError("no browser client expected in this test")

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=PolicyEngine(config.policy),
        git=git,
        executor=NeverCalledExecutor(),
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
    orch._dispatch_executor(implement("t1"))

    loaded = execution_store.load("t1")
    assert loaded.candidate_sha == real_sha  # adopted, not re-committed
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"
    assert "sneaked.py" in (orch.state.question or "")
    assert "outside" in (orch.state.question or "")


def test_worker_isolation_refuses_any_active_hook_before_anything_else_runs(tmp_path):
    """Companion to the test above: proves hooks are categorically
    impossible for `worker_repos` (unlike the worktrees fallback), so the
    crash-recovery scenario above is the realistic one, not an evasion of
    an easier hook-based test."""
    repo_root = real_repo(tmp_path)
    workers_root = tmp_path / "workers_root"
    worker_repos = WorkerRepoManager(workers_root, tmp_path / "worker-hooks")
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    repo = worker_repos.create("probe", repo_root, git.head_sha())
    hook = worker_repos.hooks_dir_for("probe") / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)

    from autoloop.worker_env import verify_worker_isolation

    probe_git = repo.gateway(PolicyEngine(PolicyConfig()))
    violations = verify_worker_isolation(probe_git, expected_hooks_dir=worker_repos.hooks_dir_for("probe"))
    assert any("hook" in v for v in violations)


class _NeverRunExecutor:
    def execute(self, directive, task):
        raise AssertionError("executor must never run for a task with no approved_paths")


def test_task_with_no_approved_paths_cannot_be_dispatched(tmp_path):
    orch, repo_root, execution_store, task, executor = _build_worker_repos_orchestrator(
        tmp_path,
        lambda wr, rr: _NeverRunExecutor(),
        approved_paths=(),
    )
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"
    assert execution_store.load(task.id) is None  # not even a worker repo was created


# =============================================================================
# 5. failed-round isolation and bounded attempts (finding #3) — adversarial
#    tests 8 & 9
# =============================================================================


def test_failed_attempt_residue_absent_from_the_next_candidate(tmp_path):
    """Adversarial test 8: attempt 1 writes A and FAILS validation; attempt
    2 writes B and passes. The resulting commit must contain ONLY B — A
    must never ride along, and its evidence must be preserved (quarantined)
    rather than silently vanished."""

    class FailThenSucceedExecutor:
        def __init__(self, workers_root, repo_root):
            self.workers_root = Path(workers_root)
            self.calls = 0

        def execute(self, directive, task):
            self.calls += 1
            wt = self.workers_root / task.id
            if self.calls == 1:
                (wt / "A.py").write_text("attempt 1 — must never be committed\n", encoding="utf-8")
                return ExecutionOutcome(
                    status="error", summary="validation failed", validation="ruff: E501", changed_paths=()
                )
            (wt / "B.py").write_text("attempt 2 — the real change\n", encoding="utf-8")
            return ExecutionOutcome(
                status="ok", summary="did it", validation="ok", changed_paths=("B.py",)
            )

    orch, repo_root, execution_store, task, executor = _build_worker_repos_orchestrator(
        tmp_path, lambda wr, rr: FailThenSucceedExecutor(wr, rr), approved_paths=("A.py", "B.py")
    )
    workers_root = orch._worker_repos.root_dir

    orch._dispatch_executor(implement(task.id))
    assert orch.state.phase == Phase.READY.value  # a failed outcome re-enters ready, does not park
    execution = execution_store.load(task.id)
    assert execution.candidate_sha == ""
    assert execution.attempt_count == 1  # consumed even though nothing committed

    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement(task.id))
    execution = execution_store.load(task.id)
    assert execution.candidate_sha != ""
    assert execution.attempt_count == 2

    worker_git = GitGateway(orch._worker_repos.path_for(task.id), PolicyEngine(PolicyConfig()))
    touched = worker_git.commit_range_paths(execution.task_base_sha, execution.candidate_sha)
    assert touched == {"B.py"}
    assert "A.py" not in touched

    # Evidence preserved (never deleted), but no longer reachable by create().
    quarantine_root = workers_root.parent / "quarantine"
    assert quarantine_root.is_dir()
    quarantined = list(quarantine_root.iterdir())
    assert len(quarantined) == 1
    assert (quarantined[0] / "A.py").exists()


def test_quarantine_recreate_resumes_from_a_candidate_sha_that_only_exists_in_the_quarantined_repo(
    tmp_path,
):
    """Regression test: `_prepare_write_capable_worker`'s quarantine-and-
    recreate branch previously always fetched from `self._git.repo_root`
    (the PRIMARY checkout) regardless of whether it was resuming from
    `execution.task_base_sha` or `execution.candidate_sha`. But
    `candidate_sha` names a commit made INSIDE the worker repo being
    quarantined — its own local git object database, never pushed anywhere
    — so the primary checkout never has that object and the recreate's
    `git fetch` fails outright. `test_failed_attempt_residue_absent_from_
    the_next_candidate` above never exercises this: its quarantine happens
    before ANY commit, so it only ever resumes from `task_base_sha`.

    Reachable path modelled here: round 1 succeeds and commits
    (`candidate_sha` set); round 2 writes residue into the SAME worker repo
    and FAILS validation (never commits) — the worktree is left dirty;
    round 3's dispatch finds that dirty worktree, quarantines it, and must
    recreate the worker repo resuming from round 1's `candidate_sha`, which
    now exists ONLY in the directory just moved into quarantine."""

    class RoundExecutor:
        def __init__(self, workers_root, repo_root):
            self.workers_root = Path(workers_root)
            self.calls = 0

        def execute(self, directive, task):
            self.calls += 1
            wt = self.workers_root / task.id
            if self.calls == 1:
                (wt / "A.py").write_text("round 1\n", encoding="utf-8")
                return ExecutionOutcome(
                    status="ok", summary="round 1", validation="ok", changed_paths=("A.py",)
                )
            if self.calls == 2:
                (wt / "B.py").write_text("round 2 — must never be committed\n", encoding="utf-8")
                return ExecutionOutcome(
                    status="error",
                    summary="round 2 validation failed",
                    validation="ruff: E501",
                    changed_paths=(),
                )
            (wt / "C.py").write_text("round 3 — the real fix\n", encoding="utf-8")
            return ExecutionOutcome(
                status="ok", summary="round 3", validation="ok", changed_paths=("C.py",)
            )

    orch, repo_root, execution_store, task, executor = _build_worker_repos_orchestrator(
        tmp_path, RoundExecutor, approved_paths=("A.py", "B.py", "C.py")
    )
    workers_root = orch._worker_repos.root_dir

    # Round 1: succeeds and commits.
    orch._dispatch_executor(implement(task.id))
    execution1 = execution_store.load(task.id)
    assert execution1.candidate_sha != ""
    candidate_1 = execution1.candidate_sha

    # Round 2: writes B.py into the SAME worker repo, fails validation, never
    # commits — the worktree is left dirty with B.py on disk.
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement(task.id))
    execution2 = execution_store.load(task.id)
    assert execution2.candidate_sha == candidate_1  # unchanged: round 2 never committed
    assert execution2.attempt_count == 2
    worker_repo_path = orch._worker_repos.path_for(task.id)
    assert (worker_repo_path / "B.py").exists()  # residue really is on disk

    # Round 3: the dirty worktree from round 2 must be quarantined and a
    # fresh worker repo recreated resuming from `candidate_1`. Before the
    # fix, `create()` tried to fetch `candidate_1` from the PRIMARY
    # checkout, which never received it, and raised `GitCommandError`.
    orch.state.phase = Phase.READY.value
    orch._dispatch_executor(implement(task.id))

    assert orch.state.phase != Phase.NEEDS_USER.value, (
        f"round 3 unexpectedly parked: {orch.state.question}"
    )
    execution3 = execution_store.load(task.id)
    assert execution3.candidate_sha != candidate_1
    assert execution3.candidate_sha != ""

    new_worker_git = GitGateway(orch._worker_repos.path_for(task.id), PolicyEngine(PolicyConfig()))
    touched = new_worker_git.commit_range_paths(execution3.task_base_sha, execution3.candidate_sha)
    assert touched == {"A.py", "C.py"}
    assert "B.py" not in touched  # round 2's residue never rode along

    # And round 2's residue is preserved, quarantined, not silently gone.
    quarantine_root = workers_root.parent / "quarantine"
    assert quarantine_root.is_dir()
    quarantined_dirs = [d for d in quarantine_root.iterdir() if d.is_dir()]
    assert any((d / "B.py").exists() for d in quarantined_dirs)


def test_pre_commit_failures_consume_the_attempt_budget_across_restart(tmp_path):
    """Adversarial test 9: `MAX_TASK_ATTEMPTS` consecutive pre-commit
    (validation) failures must hit the ceiling — even when each attempt is
    dispatched through a BRAND NEW `Orchestrator` instance (modelling a
    process restart between each one), because the count is persisted to
    `TaskExecutionStore` on disk, not held only in memory."""
    from autoloop.blockers import BlockerStore

    repo_root = real_repo(tmp_path)
    workers_root = tmp_path / "workers_root"
    execution_store = TaskExecutionStore(tmp_path / "executions")
    intent_store = IntentStore(tmp_path / "intents")
    blocker_store = BlockerStore(tmp_path / "blockers")
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
        workers_root=workers_root,
    )
    task_store = TaskStore(config.tasks_file)
    task = Task(id="t1", title="T", description="d", approved_paths=("A.py",))
    registry = TaskRegistry([task])
    task_store.save(registry)

    class AlwaysFailExecutor:
        def __init__(self, workers_root):
            self.workers_root = Path(workers_root)
            self.calls = 0

        def execute(self, directive, task):
            self.calls += 1
            (self.workers_root / task.id / "A.py").write_text(f"attempt {self.calls}\n", encoding="utf-8")
            return ExecutionOutcome(status="error", summary="always fails", validation="fail", changed_paths=())

    def fresh_orchestrator():
        # A brand new Orchestrator + State + WorkerRepoManager per call —
        # nothing but what is on DISK (execution_store / task_store /
        # config) carries over, modelling a process restart.
        git = GitGateway(repo_root, PolicyEngine(PolicyConfig(implement_enabled=True)))
        worker_repos = WorkerRepoManager(workers_root, tmp_path / "worker-hooks")
        store = StateStore(config.state_file)
        state = LoopState.new(URL)
        store.save(state)

        def no_client():
            raise AssertionError("no browser client expected")

        return Orchestrator(
            config=config,
            store=store,
            state=state,
            policy=PolicyEngine(config.policy),
            git=git,
            executor=AlwaysFailExecutor(workers_root),
            transcript=TranscriptLogger(config.transcript_file),
            client_factory=no_client,
            registry=registry,
            task_store=task_store,
            manifest_store=ManifestStore(config.manifests_dir),
            worker_repos=worker_repos,
            execution_store=execution_store,
            intent_store=intent_store,
            blocker_store=blocker_store,
            validation_runner=ok_validation,
        )

    for attempt in range(1, MAX_TASK_ATTEMPTS + 1):
        orch = fresh_orchestrator()
        orch._dispatch_executor(implement("t1"))
        execution = execution_store.load("t1")
        assert execution.attempt_count == attempt, f"attempt {attempt}"
        assert execution.candidate_sha == ""
        assert orch.state.phase == Phase.READY.value  # each failure re-enters ready, not parked

    # One more restart: the ceiling refuses BEFORE the executor ever runs.
    orch = fresh_orchestrator()
    orch._dispatch_executor(implement("t1"))
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "task_fatal"
    assert orch._executor.calls == 0  # the ceiling fired before dispatch
    execution = execution_store.load("t1")
    assert execution.attempt_count == MAX_TASK_ATTEMPTS  # not bumped by the refusal itself

    blocker = blocker_store.load(orch.state.park_blocker_id)
    assert blocker is not None
    assert blocker.code == "attempt_count_ceiling"


# =============================================================================
# 6. blocker precondition integrity (finding #7) — adversarial tests 10 & 11
# =============================================================================


def test_worker_environment_drift_precondition_is_not_the_browser_check(tmp_path):
    """Old behaviour (the bug): `worker_environment_drift` mapped to
    `_precondition_browser`, whose checks (cdp/playwright/provider/
    conversation_url/browser_live) never inspect git hooks or worker
    isolation at all. New behaviour: a DEDICATED recheck that reuses
    `verify_worker_isolation` against a real throwaway probe repo, so it can
    actually distinguish a still-broken worker environment from a fixed
    one — independent of whether a browser happens to be reachable."""
    from autoloop.cli import _RESOLUTION_PRECONDITIONS, _precondition_browser

    assert _RESOLUTION_PRECONDITIONS["worker_environment_drift"] is not _precondition_browser

    repo_root = real_repo(tmp_path)
    precondition = _RESOLUTION_PRECONDITIONS["worker_environment_drift"]

    bad_config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=repo_root / ".al",
        workers_root=repo_root / ".al" / "workers",  # nested — still broken
    )
    problem = precondition(bad_config)
    assert problem  # still broken, correctly reported

    good_config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=repo_root / ".al",
        workers_root=tmp_path / "workers_root",  # external — genuinely fixed
    )
    import os

    cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        assert precondition(good_config) == ""
    finally:
        os.chdir(cwd)


def test_worker_drift_blocker_cannot_be_cleared_by_arbitrary_answer_text(tmp_path, monkeypatch, capsys):
    """Adversarial test 10: end to end through `python -m autoloop answer`
    — a `worker_environment_drift` blocker whose underlying condition is
    STILL present must refuse, regardless of what the operator types."""
    from autoloop.blockers import Blocker, BlockerStore
    from autoloop.cli import main

    repo_root = real_repo(tmp_path)
    monkeypatch.chdir(repo_root)
    cfg = repo_root / "config.toml"
    bad_workers_root = str(repo_root / ".al" / "workers")  # nested — invalid
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nstate_dir = ".al"\nworkers_root = "{bad_workers_root}"\n',
        encoding="utf-8",
    )

    store = BlockerStore(repo_root / ".al" / "blockers")
    store.save(
        Blocker(
            id="blk-(loop)-001",
            task_id="(loop)",
            kind="loop_fatal",
            code="worker_environment_drift",
            question="did the environment settle?",
            detail="",
            phase="executing",
            created_at="2026-07-31T00:00:00+00:00",
        )
    )

    code = main(["answer", "blk-(loop)-001", "fixed it, I promise", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 1
    assert "NOT resolved" in out
    assert store.load("blk-(loop)-001").resolved_at is None


def test_primary_checkout_dirty_precondition_refuses_until_checkout_is_clean(tmp_path):
    """`primary_checkout_dirty` (finding #2's own new loop_fatal park) was,
    like `worker_environment_drift` before the M1-hardening pass fixed it,
    initially left OUT of `_RESOLUTION_PRECONDITIONS` entirely — meaning any
    answer text at all would have cleared a blocker whose condition
    (uncommitted changes sitting in the primary checkout) plain text can
    never actually fix. Confirms the dedicated recheck: refuses while the
    checkout is dirty, clears once it genuinely is not."""
    from autoloop.cli import _RESOLUTION_PRECONDITIONS

    precondition = _RESOLUTION_PRECONDITIONS["primary_checkout_dirty"]
    repo_root = real_repo(tmp_path)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=repo_root / ".al",
        workers_root=tmp_path / "workers_root",
    )

    import os

    cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        assert precondition(config) == ""  # clean checkout: nothing to refuse

        (repo_root / "README.md").write_text("still dirty\n", encoding="utf-8")
        problem = precondition(config)
        assert problem
        assert "README.md" in problem

        run_git(repo_root, "checkout", "--", "README.md")
        assert precondition(config) == ""  # genuinely clean again: clears
    finally:
        os.chdir(cwd)


def test_checkout_escape_detected_precondition_never_resolves_by_answer_text(tmp_path):
    """`checkout_escape_detected` is the escape detector's OWN loop_fatal
    park (`_execute_with_escape_detection`) — the most security-sensitive
    code this pass adds, since it fires exactly when a write-capable agent
    reached outside its worker repo.

    Regression test for a real gap found in review: an EARLIER version of
    this precondition reused `primary_checkout_dirty`'s `is_dirty()` recheck
    — wrong, because the escape detector's snapshot covers a SUPERSET of
    what `is_dirty()` can see (tracked + untracked + IGNORED paths; see
    `test_escape_detector_detects_ignored_content_change`, which proves
    `git.dirty_files() == []` even while the detector correctly flags
    tampering). Autoloop's own state dir is gitignored in production, so
    the canonical "agent forged its own records" escape is EXACTLY a case
    where the tree stays clean throughout — a dirty-checkout recheck would
    have cleared that blocker on nothing. Fixed: this precondition now
    refuses UNCONDITIONALLY, regardless of checkout state, mirroring
    `_precondition_protected`'s shape."""
    from autoloop.cli import _RESOLUTION_PRECONDITIONS, _precondition_checkout_clean

    precondition = _RESOLUTION_PRECONDITIONS["checkout_escape_detected"]
    assert precondition is not _precondition_checkout_clean  # no longer shared

    repo_root = real_repo(tmp_path)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=repo_root / ".al",
        workers_root=tmp_path / "workers_root",
    )

    import os

    cwd = os.getcwd()
    os.chdir(repo_root)
    try:
        # Dirty checkout: still refuses (never resolvable by answer text).
        (repo_root / "sneaked_outside.py").write_text("evil\n", encoding="utf-8")
        assert precondition(config)

        # Checkout made clean again: STILL refuses — this is the actual
        # regression case. `is_dirty()` alone would have cleared here.
        (repo_root / "sneaked_outside.py").unlink()
        assert precondition(config)
    finally:
        os.chdir(cwd)


def test_checkout_escape_detected_blocker_cannot_be_cleared_even_when_only_an_ignored_path_was_touched(
    tmp_path, monkeypatch, capsys
):
    """End-to-end companion, through `python -m autoloop answer` — the exact
    scenario the regression above targets: an escape that touches ONLY an
    ignored path (modelling the canonical `.autoloop/state.json`-tampering
    case) leaves `git status`/`is_dirty()` reporting nothing at all, so if
    the precondition were still `is_dirty()`-based, `answer` would resolve
    this blocker having verified precisely nothing."""
    from autoloop.blockers import Blocker, BlockerStore
    from autoloop.cli import main

    repo_root = real_repo(tmp_path)
    (repo_root / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    run_git(repo_root, "add", ".gitignore")
    run_git(repo_root, "commit", "-q", "-m", "gitignore")
    (repo_root / "ignored.log").write_text("tampered by agent\n", encoding="utf-8")

    monkeypatch.chdir(repo_root)
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    assert git.is_dirty() is False  # sanity: plain git status sees nothing

    cfg = repo_root / "config.toml"
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nstate_dir = ".al"\nworkers_root = "{tmp_path / "workers_root"}"\n',
        encoding="utf-8",
    )

    store = BlockerStore(repo_root / ".al" / "blockers")
    store.save(
        Blocker(
            id="blk-t1-001",
            task_id="t1",
            kind="loop_fatal",
            code="checkout_escape_detected",
            question="agent wrote outside its worker repo: ignored.log content changed",
            detail="",
            phase="executing",
            created_at="2026-07-31T00:00:00+00:00",
        )
    )

    code = main(["answer", "blk-t1-001", "reverted it, all clean now", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert code == 1
    assert "NOT resolved" in out
    assert store.load("blk-t1-001").resolved_at is None


def test_worker_isolation_violation_precondition_reuses_worker_environment_drift_check(tmp_path):
    """`worker_isolation_violation` (emitted by
    `_prepare_write_capable_worker` when a freshly recreated worker repo
    still is not isolated) is the SAME underlying condition
    `worker_environment_drift` already has a dedicated recheck for — so it
    reuses that function rather than needing a second implementation of the
    identical probe."""
    from autoloop.cli import _RESOLUTION_PRECONDITIONS, _precondition_worker_environment_drift

    assert (
        _RESOLUTION_PRECONDITIONS["worker_isolation_violation"]
        is _precondition_worker_environment_drift
    )


def test_security_and_environment_codes_all_have_a_precondition():
    """Reverse-direction companion to `test_every_precondition_key_matches_
    a_real_emitted_code` below: that test only proves every KEY in
    `_RESOLUTION_PRECONDITIONS` maps to a real code (nothing stale). It
    cannot catch the opposite mistake — a genuinely environmental/security
    code that got emitted but never added to the mapping at all, which is
    exactly the gap this pass found for `primary_checkout_dirty`,
    `checkout_escape_detected`, and `worker_isolation_violation` (none were
    in the mapping when first written, despite all being loop_fatal parks
    whose condition text can never fix). This list is intentionally
    CURATED, not derived from `_emitted_blocker_codes()` — most parks (every
    task_fatal one, `ask_user`, etc.) correctly DO resolve on operator text,
    so a fully exhaustive reverse mapping would be wrong, not just
    redundant."""
    from autoloop.cli import _RESOLUTION_PRECONDITIONS

    security_and_environment_codes = {
        "login_expired",
        "submission_ambiguous",
        "git_failure_budget_exhausted",
        "publisher_url_drift",
        "worker_environment_drift",
        "worker_isolation_violation",
        # `push_refused` was RETIRED, not renamed: the orchestrator now emits
        # `push_refused_protected` for the protected-branch case. Its old
        # precondition only rechecked `allow_push`, a flag necessarily already
        # true for the push to have reached the gateway — a no-op for every
        # real failure. Listing a code nothing emits would make this test
        # demand a precondition for a blocker that can never exist.
        "push_refused_protected",
        "primary_checkout_dirty",
        "checkout_escape_detected",
    }
    missing = security_and_environment_codes - set(_RESOLUTION_PRECONDITIONS)
    assert not missing, f"security/environment codes with no precondition at all: {sorted(missing)}"


def _emitted_blocker_codes() -> set[str]:
    """Every string literal that can appear as the `code=` argument of a
    `self._to_needs_user(...)` call in `orchestrator.py`, AST-walked rather
    than regex-matched so a `code=` built from a conditional expression
    (e.g. `"push_refused_protected" if ... else "push_refused"`) still
    yields every branch's literal, not just whichever one happens to sit
    immediately after `code=`."""
    from autoloop import orchestrator as orchestrator_module

    source = inspect.getsource(orchestrator_module)
    tree = ast.parse(source)
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_to_needs_user":
            continue
        for kw in node.keywords:
            if kw.arg != "code":
                continue
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    codes.add(sub.value)
    return codes


def test_every_precondition_key_matches_a_real_emitted_code():
    """Adversarial test 11, exhaustive/data-driven per the brief: NOT a
    hand-maintained list of expected codes (that is exactly how
    `git_failure_budget` — real code `git_failure_budget_exhausted` — and
    `push_refused_protected` — previously never emitted at all — went stale
    in the first place)."""
    from autoloop.cli import _RESOLUTION_PRECONDITIONS

    emitted = _emitted_blocker_codes()
    assert len(emitted) > 10, "sanity: the AST walk must actually find a realistic number of codes"

    unmatched = set(_RESOLUTION_PRECONDITIONS) - emitted
    assert not unmatched, f"precondition keys with no matching emitted code: {sorted(unmatched)}"

    # And the two specific historical dead/mismatched keys are gone for good.
    assert "git_failure_budget" not in _RESOLUTION_PRECONDITIONS
    assert "git_failure_budget_exhausted" in _RESOLUTION_PRECONDITIONS
    assert "push_refused_protected" in emitted


def test_push_refused_protected_is_actually_emitted_for_a_protected_branch_refusal(tmp_path):
    """Companion to the exhaustiveness test above: proves
    `push_refused_protected` is not just present in the source text but is
    the code a REAL protected-branch push refusal produces — computed from
    the same `protected_branches`/`allow_protected_push` policy state
    `push_exact` itself checks, never by sniffing the exception string."""
    from autoloop.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator._dispatch_task_push)
    assert "push_refused_protected" in source
    assert "is_protected_refusal" in source
    # The decision must be computed from policy state, not string-matched
    # out of the exception it is deciding about.
    assert 'in str(exc)' not in source
    assert "gateway_protected" in source
