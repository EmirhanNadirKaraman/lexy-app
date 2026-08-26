"""esc-02 — the loop observes a checkout nothing else writes to.

THE CLAIM THESE TESTS EXIST TO PIN, in the three parts it was stated in:

  1. a concurrent write in the OPERATOR's checkout cannot raise
     `checkout_escape_detected`;
  2. a genuine write outside its worker repo by a write-capable agent still
     does — IGNORED paths included, which is the half that must not be
     narrowed and the half a fresh clone makes easy to prove vacuously (a
     brand-new clone has no ignored files at all, so a test that merely
     asserts "ignored paths are enumerated" proves nothing; the tests below
     CREATE one inside the window and require the park);
  3. a synchronisation failure between the two checkouts fails SAFE — it
     parks or refuses rather than silently observing a stale tree.

Self-contained per this codebase's convention (see `test_postcommit_flow.py`)
— real git repositories throughout, no fixtures imported from other test
modules.

WHAT IS DELIBERATELY NOT ASSERTED HERE: the exact text git writes into
`.git/FETCH_HEAD`. That "the worker was seeded from the clone" is checked by
recording the argument `WorkerRepoManager.create` actually receives, which is
the fact the design depends on; the FETCH_HEAD spelling is a consequence of it
and not a contract this repository controls.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoloop.config import (
    AutoloopConfig,
    BrowserConfig,
    DEFAULT_OBSERVED_CHECKOUT_NAME,
    default_observed_checkout,
    load_config,
    resolve_observed_checkout,
)
from autoloop.contract import Decision, Directive
from autoloop.errors import ConfigError
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore, mutation_ledger_for
from autoloop.transcript import TranscriptLogger
from autoloop.worker_env import (
    OBSERVED_BRANCH,
    OBSERVED_PIN_PREFIX,
    ObservedCheckout,
    WorkerRepoManager,
    validate_observed_checkout,
)
from autoloop.worktask import IntentStore, TaskExecution, TaskExecutionStore

URL = "https://chatgpt.com/c/esc-02-observed-checkout"

#: Everything both trees ignore. `.ruff_cache/` and `__pycache__/` are the two
#: artefacts that actually parked this loop, and `.al/` mirrors the state dir
#: the harness puts inside the checkout (as production's `.gitignore` does).
GITIGNORE = ".al/\n__pycache__/\n*.py[cod]\n.ruff_cache/\n"


#: Every raw git call in this module is a local-filesystem operation that
#: should take milliseconds. BOUNDED anyway: one of them is a `fetch`, the only
#: network-shaped verb here, and an unbounded call that blocks does not fail a
#: test — it hangs the whole suite, which reports as a timeout naming nothing.
_GIT_TIMEOUT_SECONDS = 120


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout


def real_repo(tmp_path, name="repo") -> Path:
    repo_root = tmp_path / name
    repo_root.mkdir()
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n", encoding="utf-8")
    (repo_root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")
    return repo_root


def head_of(repo_root: Path) -> str:
    return run_git(repo_root, "rev-parse", "HEAD").strip()


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def implement(task_id="t1") -> Directive:
    return Directive(decision=Decision.IMPLEMENT, reason="go", task_id=task_id)


# =============================================================================
# 1. ObservedCheckout lifecycle — created, reused, and every failure fails safe
# =============================================================================


def test_a_missing_clone_is_created_at_the_named_commit_and_is_clean(tmp_path):
    repo_root = real_repo(tmp_path)
    clone = ObservedCheckout(tmp_path / "observed")
    assert not clone.path.exists()

    assert clone.synchronize(repo_root, [head_of(repo_root)]) == []

    assert clone.is_repo()
    assert clone.head_sha() == head_of(repo_root)
    assert clone.residue() == []
    assert (clone.path / "README.md").read_text(encoding="utf-8") == "hello\n"
    # The tree it observes is the COMMITTED content and nothing else: the
    # operator's ignored and untracked files are not in it, which is the whole
    # mechanism by which their writes stop being reported.
    assert not (clone.path / ".ruff_cache").exists()


def test_an_existing_clone_is_reused_and_moved_forward_to_the_new_head(tmp_path):
    """Behind is the ordinary case — the loop merges into the primary checkout
    between rounds, so the clone is one commit back every time."""
    repo_root = real_repo(tmp_path)
    clone = ObservedCheckout(tmp_path / "observed")
    assert clone.synchronize(repo_root, [head_of(repo_root)]) == []
    first = clone.head_sha()

    (repo_root / "second.txt").write_text("more\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "second")
    second = head_of(repo_root)
    assert second != first

    assert clone.synchronize(repo_root, [second]) == []
    assert clone.head_sha() == second
    assert (clone.path / "second.txt").exists()
    assert clone.residue() == []


def test_a_dirty_clone_is_refused_and_nothing_in_it_is_reset(tmp_path):
    """The residue IS the evidence. A sync that quietly checked out over it
    would destroy the only record that something wrote to a tree only the loop
    is supposed to write to — including, in the worst case, what an escape in
    the previous round left behind."""
    repo_root = real_repo(tmp_path)
    clone = ObservedCheckout(tmp_path / "observed")
    assert clone.synchronize(repo_root, [head_of(repo_root)]) == []

    (clone.path / "README.md").write_text("SOMETHING ELSE WROTE THIS\n", encoding="utf-8")

    violations = clone.synchronize(repo_root, [head_of(repo_root)])
    assert violations, "a dirty observed checkout must refuse"
    assert "not clean" in violations[0]
    assert "README.md" in violations[0]
    # Not reset, not repaired, not deleted.
    assert (clone.path / "README.md").read_text(encoding="utf-8") == (
        "SOMETHING ELSE WROTE THIS\n"
    )


def test_an_ignored_file_in_the_clone_is_residue_even_though_git_status_is_clean(tmp_path):
    """The `.ruff_cache/` case, one level down from the detector.

    `git status` reports nothing for an ignored path — that is exactly why the
    escape detector enumerates them — so a cleanliness check built on `status`
    alone would call this tree clean. Nothing ever runs inside the clone, so a
    cache directory there is not noise; it is evidence."""
    repo_root = real_repo(tmp_path)
    clone = ObservedCheckout(tmp_path / "observed")
    assert clone.synchronize(repo_root, [head_of(repo_root)]) == []

    cache = clone.path / ".ruff_cache" / "0.14.1"
    cache.mkdir(parents=True)
    (cache / "0123456789abcdef").write_bytes(b"cache")

    assert run_git(clone.path, "status", "--porcelain").strip() == ""
    residue = clone.residue()
    assert any(".ruff_cache" in entry for entry in residue), residue
    violations = clone.synchronize(repo_root, [head_of(repo_root)])
    assert violations and "not clean" in violations[0]


def test_a_clone_holding_a_commit_the_primary_checkout_does_not_is_refused(tmp_path):
    """Ahead / diverged. Resetting would be the tempting answer and is the
    wrong one: a commit here came from something that is not the loop."""
    repo_root = real_repo(tmp_path)
    clone = ObservedCheckout(tmp_path / "observed")
    assert clone.synchronize(repo_root, [head_of(repo_root)]) == []

    run_git(clone.path, "config", "user.email", "intruder@example.com")
    run_git(clone.path, "config", "user.name", "Intruder")
    run_git(clone.path, "config", "commit.gpgsign", "false")
    (clone.path / "planted.py").write_text("evil\n", encoding="utf-8")
    run_git(clone.path, "add", "-A")
    run_git(clone.path, "commit", "-q", "-m", "not the loop")
    ahead = clone.head_sha()

    violations = clone.synchronize(repo_root, [head_of(repo_root)])
    assert violations, "a clone ahead of the primary checkout must refuse"
    assert "not an ancestor" in violations[0]
    assert clone.head_sha() == ahead, "refusal must not move the branch"
    assert (clone.path / "planted.py").exists()


def test_a_path_that_exists_and_is_not_a_repository_is_refused_not_deleted(tmp_path):
    repo_root = real_repo(tmp_path)
    target = tmp_path / "observed"
    target.mkdir()
    (target / "someone-elses-data.txt").write_text("keep me\n", encoding="utf-8")
    clone = ObservedCheckout(target)

    violations = clone.synchronize(repo_root, [head_of(repo_root)])
    assert violations and "not the top level of a git repository" in violations[0]
    assert (target / "someone-elses-data.txt").exists()


def test_a_file_where_the_clone_should_be_is_refused(tmp_path):
    repo_root = real_repo(tmp_path)
    target = tmp_path / "observed"
    target.write_text("not a directory\n", encoding="utf-8")
    clone = ObservedCheckout(target)

    violations = clone.synchronize(repo_root, [head_of(repo_root)])
    assert violations and "not a directory" in violations[0]
    assert target.is_file()


@pytest.mark.parametrize(
    "shas",
    [
        [],
        [""],
        ["HEAD"],
        ["main"],
        ["deadbeef"],
        ["Z" * 40],
        ["0123456789abcdef0123456789abcdef01234567".upper()],
    ],
)
def test_a_target_that_is_not_a_literal_40_hex_commit_is_refused(tmp_path, shas):
    """Nothing is created for a refused target, either — the refusal happens
    before any filesystem work, so a bad call cannot leave half a clone."""
    repo_root = real_repo(tmp_path)
    clone = ObservedCheckout(tmp_path / "observed")
    assert clone.synchronize(repo_root, shas)
    assert not clone.path.exists()


def test_a_commit_the_primary_checkout_does_not_have_is_refused(tmp_path):
    repo_root = real_repo(tmp_path)
    clone = ObservedCheckout(tmp_path / "observed")
    missing = "0" * 40
    violations = clone.synchronize(repo_root, [missing])
    assert violations and "could not obtain" in violations[0]


def test_extra_shas_are_pinned_as_refs_so_a_worker_can_still_be_seeded_from_them(tmp_path):
    """A resumed round recreates its worker from a recorded `task_base_sha`
    that may be older than the head. Git's `upload-pack` refuses a request for
    an object no ref advertises, so "present in the object database" is not
    enough — the pin is what makes the fetch possible at all."""
    repo_root = real_repo(tmp_path)
    older = head_of(repo_root)
    (repo_root / "second.txt").write_text("more\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "second")
    newer = head_of(repo_root)

    clone = ObservedCheckout(tmp_path / "observed")
    assert clone.synchronize(repo_root, [newer, older]) == []
    assert clone.head_sha() == newer

    refs = run_git(clone.path, "show-ref")
    assert f"{OBSERVED_PIN_PREFIX}{older}" in refs
    assert OBSERVED_BRANCH in refs

    # And the fetch a worker repo would make really does work for the older
    # commit — the property the pin exists for, exercised rather than asserted.
    worker = tmp_path / "worker"
    worker.mkdir()
    run_git(worker, "init", "-q")
    run_git(worker, "fetch", "-q", str(clone.path), older)


def test_a_git_that_cannot_run_is_a_violation_rather_than_a_pass(tmp_path):
    """The fail-open this class must not have. Every branch below reports "I
    could not look", never "there is nothing to see" — a synchronisation that
    returned `[]` because git was unavailable would hand the loop an
    unverified tree with a clean bill of health."""
    repo_root = real_repo(tmp_path)

    def explode(argv, **kwargs):
        raise OSError("git is not installed")

    clone = ObservedCheckout(tmp_path / "observed", runner=explode)
    assert clone.synchronize(repo_root, [head_of(repo_root)])
    # And the OTHER way a git call can fail to answer: it never returns.
    # `TimeoutExpired` is a `SubprocessError`, not an `OSError`, so a handler
    # catching only the latter would let it escape into the dispatch.
    def stall(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))

    stalled = ObservedCheckout(tmp_path / "stalled", runner=stall)
    assert stalled.synchronize(repo_root, [head_of(repo_root)])
    assert stalled.residue() == [
        "the observed checkout's status could not be read: git could not be executed"
    ]
    assert clone.residue() == [
        "the observed checkout's status could not be read: git could not be executed"
    ]
    assert clone.head_sha() == ""
    assert clone.is_repo() is False


def test_an_unreadable_status_after_a_successful_sync_is_a_violation(tmp_path):
    """The post-sync re-read is a check, so it has to fail closed too."""
    repo_root = real_repo(tmp_path)
    real = ObservedCheckout(tmp_path / "observed")
    assert real.synchronize(repo_root, [head_of(repo_root)]) == []

    class Proc:
        returncode = 1
        stdout = ""
        stderr = "fatal: cannot read the index"

    seen = {"status": 0}

    def flaky(argv, **kwargs):
        # Let the FIRST status read (the pre-sync cleanliness check) through and
        # break the SECOND (the post-sync verification), so the failure is
        # isolated to the check being tested rather than to setup.
        if "status" in argv:
            seen["status"] += 1
            if seen["status"] > 1:
                return Proc()
        return subprocess.run(argv, **kwargs)

    clone = ObservedCheckout(tmp_path / "observed", runner=flaky)
    violations = clone.synchronize(repo_root, [head_of(repo_root)])
    assert seen["status"] >= 2, "not vacuous: the post-sync re-read really ran"
    assert violations, "an unreadable status must never read as clean"


# =============================================================================
# 2. Where the clone may live
# =============================================================================


def test_an_unconfigured_observed_checkout_is_not_a_violation(tmp_path):
    """Absent means "watch the primary checkout, as before" — safe but noisy,
    not unsafe. That is the asymmetry with `validate_workers_root`, which
    refuses an absent value outright."""
    assert validate_observed_checkout(None, tmp_path / "repo", tmp_path / "state", None) == []


def test_a_clone_inside_the_primary_checkout_or_the_state_dir_is_refused(tmp_path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    workers_root = tmp_path / "workers"
    workers_root.mkdir()

    for bad in (repo_root / "observed", repo_root / ".git" / "observed",
                state_dir / "observed", workers_root / "observed"):
        violations = validate_observed_checkout(bad, repo_root, state_dir, workers_root)
        assert violations, bad
        assert "nested beneath" in violations[0]


def test_a_clone_that_CONTAINS_the_state_dir_or_workers_root_is_refused(tmp_path):
    """The other direction, and it matters as much: a clone with the state dir
    or the worker repositories inside it puts every loop write back into the
    snapshot, which is port-01's bug rebuilt on the new tree."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    observed = tmp_path / "observed"
    violations = validate_observed_checkout(
        observed, repo_root, observed / "state", observed / "workers"
    )
    assert len(violations) >= 2
    assert all("nested beneath observed_checkout" in v for v in violations)


def test_the_primary_checkout_itself_is_refused(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    violations = validate_observed_checkout(
        repo_root, repo_root, tmp_path / "state", tmp_path / "workers"
    )
    assert violations and "IS the primary checkout" in violations[0]


def test_a_relative_observed_checkout_is_refused_at_both_layers(tmp_path):
    assert validate_observed_checkout(
        Path("relative/observed"), tmp_path / "repo", tmp_path / "state", None
    )
    with pytest.raises(ConfigError, match="absolute"):
        resolve_observed_checkout("relative/observed", tmp_path / "workers")


def test_the_default_sits_beside_workers_root_and_load_config_resolves_it(tmp_path):
    workers = tmp_path / "nest" / "workers"
    assert default_observed_checkout(workers) == tmp_path / "nest" / DEFAULT_OBSERVED_CHECKOUT_NAME
    assert resolve_observed_checkout(None, workers) == default_observed_checkout(workers)

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nworkers_root = "{workers}"\n',
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert config.observed_checkout == default_observed_checkout(workers)

    elsewhere = tmp_path / "elsewhere"
    cfg.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nworkers_root = "{workers}"\n'
        f'observed_checkout = "{elsewhere}"\n',
        encoding="utf-8",
    )
    assert load_config(cfg).observed_checkout == elsewhere


# =============================================================================
# 3. End to end — which tree a write has to land in to stop the loop
# =============================================================================


class _RecordingWorkerRepos(WorkerRepoManager):
    """Records the fetch source every worker repository is seeded from."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sources: list[Path] = []

    def create(self, task_id, source_repo_path, base_sha, branch=None):
        self.sources.append(Path(source_repo_path).resolve())
        return super().create(task_id, source_repo_path, base_sha, branch=branch)


class _TamperingExecutor:
    """Writes into its own worker repo (legitimate) and, once, reaches outside
    to write somewhere else — modelling either an agent that is not confined to
    its worker repo, or the operator's own tooling running concurrently,
    depending on which tree `tamper` is pointed at."""

    def __init__(self, workers_root, tamper):
        self.workers_root = Path(workers_root)
        self.tamper = tamper
        self.calls = 0

    def execute(self, directive, task):
        self.calls += 1
        (self.workers_root / task.id / "feature.py").write_text(
            "print('hi')\n", encoding="utf-8"
        )
        self.tamper()
        return ExecutionOutcome(
            status="ok", summary="did it", validation="ok", changed_paths=("feature.py",)
        )


def _build(tmp_path, tamper, *, observed=True, task_id="t1"):
    repo_root = real_repo(tmp_path)
    workers_root = tmp_path / "workers_root"
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig(implement_enabled=True)))
    worker_repos = _RecordingWorkerRepos(workers_root, tmp_path / "worker-hooks")

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=repo_root / ".al",     # realistic: inside the checkout, ignored
        workers_root=workers_root,
        observed_checkout=(tmp_path / "observed") if observed else None,
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)
    task = Task(id=task_id, title="T", description="d", approved_paths=("feature.py",))
    registry = TaskRegistry([task])
    task_store = TaskStore(
        config.tasks_file,
        ledger=mutation_ledger_for(config.workers_root, config.state_dir),
    )
    task_store.save(registry)
    executor = _TamperingExecutor(workers_root, tamper)

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
        execution_store=TaskExecutionStore(tmp_path / "executions"),
        intent_store=IntentStore(tmp_path / "intents"),
        validation_runner=ok_validation,
        observed_checkout=(
            ObservedCheckout(config.observed_checkout) if observed else None
        ),
    )
    return orch, repo_root, tmp_path / "observed", executor, worker_repos


def _both_incidents(root: Path) -> None:
    """The two writes that actually parked this loop on 2026-08-26, in one
    call: a `ruff` run's cache (untracked AND ignored) and a Claude Code
    project-rules file (untracked, not ignored)."""
    cache = root / ".ruff_cache" / "0.14.1"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "0123456789abcdef").write_bytes(b"cache")
    rules = root / ".claude" / "rules"
    rules.mkdir(parents=True, exist_ok=True)
    (rules / "evidence-first.md").write_text("# rule\n", encoding="utf-8")


def test_a_concurrent_write_in_the_operator_checkout_no_longer_parks_the_loop(tmp_path):
    """Part 1 of the claim, in the exact shape of both real incidents."""
    holder = {}

    def tamper():
        _both_incidents(holder["repo_root"])

    orch, repo_root, observed, executor, _repos = _build(tmp_path, tamper)
    holder["repo_root"] = repo_root

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 1, "not vacuous: the round really ran"
    question = orch.state.question or ""
    assert "outside its worker repository" not in question, question
    assert orch.state.phase != Phase.NEEDS_USER.value, question
    # DETECTION, not deletion: the operator's files are still theirs.
    assert (repo_root / ".claude" / "rules" / "evidence-first.md").exists()
    assert (repo_root / ".ruff_cache" / "0.14.1" / "0123456789abcdef").exists()


def test_the_very_same_writes_inside_the_observed_clone_are_still_loop_fatal(tmp_path):
    """Part 2, and the pair that makes part 1 mean something. Byte for byte
    the same two writes; the only difference is which tree they land in."""
    holder = {}

    def tamper():
        _both_incidents(holder["observed"])

    orch, _repo_root, observed, executor, _repos = _build(tmp_path, tamper)
    holder["observed"] = observed

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 1
    assert orch.state.park_kind == "loop_fatal"
    question = orch.state.question or ""
    assert "outside its worker repository" in question
    # BOTH are reported, the ignored one included. Matched on the leaf name
    # rather than the full repo-relative path: what is under test is that
    # neither write goes unreported, not how git spells a path it enumerates.
    assert "evidence-first.md" in question, question
    assert "0123456789abcdef" in question, question


def test_an_ignored_write_in_the_clone_is_reported_while_git_status_stays_blind(tmp_path):
    """The half that a fresh clone makes easy to prove vacuously, proved
    instead. A brand-new clone holds no ignored files at all, so "ignored paths
    are enumerated" asserts nothing; this CREATES one inside the window, requires
    the park, and then shows `git status --porcelain` in that same tree is empty
    — so the report cannot be coming from anywhere but the ignored-path
    enumeration."""
    holder = {}

    def tamper():
        cache = holder["observed"] / ".ruff_cache" / "0.14.1"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "0123456789abcdef").write_bytes(b"cache")

    orch, _repo_root, observed, executor, _repos = _build(tmp_path, tamper)
    holder["observed"] = observed

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 1
    assert orch.state.park_kind == "loop_fatal"
    question = orch.state.question or ""
    assert ".ruff_cache/0.14.1/0123456789abcdef" in question, question
    assert run_git(observed, "status", "--porcelain").strip() == "", (
        "not vacuous only while `git status` really is blind to this write"
    )


def test_an_ordinary_tracked_file_edited_in_the_clone_is_still_loop_fatal(tmp_path):
    """The plainest escape there is, so that the ignored-path cases above are
    not the only thing keeping the detector honest."""
    holder = {}

    def tamper():
        (holder["observed"] / "README.md").write_text("TAMPERED\n", encoding="utf-8")

    orch, _repo_root, observed, executor, _repos = _build(tmp_path, tamper)
    holder["observed"] = observed

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 1
    assert orch.state.park_kind == "loop_fatal"
    assert "content changed outside the worker repo" in (orch.state.question or "")
    assert "README.md" in (orch.state.question or "")


def test_the_worker_repository_is_seeded_from_the_clone_not_the_operator_checkout(tmp_path):
    """Why the second half of the claim is a property rather than a hope: the
    fetch source is the one absolute path to a non-worker tree that a worker
    repo carries on disk, so it must name a watched tree."""
    orch, repo_root, observed, _executor, repos = _build(tmp_path, lambda: None)

    orch._dispatch_executor(implement("t1"))

    assert repos.sources, "no worker repository was created at all"
    assert repos.sources == [observed.resolve()]
    assert repo_root.resolve() not in repos.sources


def test_the_round_parks_before_running_anything_when_the_clone_cannot_be_established(
    tmp_path,
):
    """Part 3. The clone is occupied by something that is not a repository, so
    synchronisation refuses — and the write-capable agent never starts, because
    starting it would mean running with no escape detection at all."""
    orch, _repo_root, observed, executor, repos = _build(tmp_path, lambda: None)
    observed.mkdir(parents=True)
    (observed / "not-a-repo.txt").write_text("hello\n", encoding="utf-8")

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 0, "nothing may run against an unestablished clone"
    assert repos.sources == []
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    question = orch.state.question or ""
    assert "observed checkout" in question
    assert "could not be established" in question
    assert (observed / "not-a-repo.txt").exists(), "nothing here deletes anything"


def test_residue_left_by_a_detected_escape_still_parks_the_NEXT_round(tmp_path):
    """The failure mode a self-healing sync would have introduced: round N's
    escape is detected, and round N+1 would quietly check out over the
    evidence. It does not — it refuses, and says what it found."""
    holder = {}

    def tamper():
        (holder["observed"] / "planted.py").write_text("evil\n", encoding="utf-8")

    orch, _repo_root, observed, _executor, _repos = _build(tmp_path, tamper)
    holder["observed"] = observed

    orch._dispatch_executor(implement("t1"))
    assert orch.state.park_kind == "loop_fatal"
    assert (observed / "planted.py").exists()

    # The next round's controlled boundary, asked directly rather than through
    # a second full dispatch: what is being pinned is the SYNC's answer, and a
    # re-dispatch would have several other reasons to refuse a task that has
    # just parked, any of which would make this pass for the wrong reason.
    orch.state.question = None
    orch.state.park_kind = None
    task = Task(id="t1", title="T", description="d", approved_paths=("feature.py",))
    assert orch._synchronise_observed_checkout(task) is False
    assert "could not be established" in (orch.state.question or "")
    assert "not clean" in (orch.state.question or "")
    assert (observed / "planted.py").exists(), "the evidence survives the refusal"


def test_without_a_configured_clone_the_primary_checkout_is_still_watched(tmp_path):
    """The opt-in half of the reversibility story, asserted rather than
    assumed: a deployment that wires no observed checkout behaves exactly as it
    did before esc-02, including parking for a write into the primary
    checkout. There is no deployment that watches nothing."""
    holder = {}

    def tamper():
        (holder["repo_root"] / "sneaked_outside.py").write_text("evil\n", encoding="utf-8")

    orch, repo_root, _observed, executor, repos = _build(tmp_path, tamper, observed=False)
    holder["repo_root"] = repo_root

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 1
    assert orch.state.park_kind == "loop_fatal"
    assert "sneaked_outside.py" in (orch.state.question or "")
    assert repos.sources == [repo_root.resolve()]


# =============================================================================
# 4. The operator-facing recheck follows the tree the park was about
# =============================================================================


def test_the_dirty_checkout_precondition_asks_about_the_observed_tree(tmp_path):
    """A recheck aimed at a different tree than the park would clear the
    blocker the moment the OPERATOR's checkout happened to be clean, while the
    tree the loop refuses to use as a baseline stayed dirty."""
    from autoloop.cli import _RESOLUTION_PRECONDITIONS

    repo_root = real_repo(tmp_path)
    observed = ObservedCheckout(tmp_path / "observed")
    assert observed.synchronize(repo_root, [head_of(repo_root)]) == []
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / "state",
        workers_root=tmp_path / "workers_root",
        observed_checkout=observed.path,
    )

    check = _RESOLUTION_PRECONDITIONS["primary_checkout_dirty"]
    assert check(config) == ""

    # Dirty the OPERATOR's checkout: not the tree this park is about.
    (repo_root / "README.md").write_text("operator editing\n", encoding="utf-8")
    assert check(config) == ""

    # Dirty the OBSERVED tree, in the way `git status` cannot see.
    cache = observed.path / ".ruff_cache"
    cache.mkdir()
    (cache / "entry").write_bytes(b"x")
    assert ".ruff_cache" in check(config)


def test_the_observed_checkout_precondition_refuses_while_the_tree_is_wrong(tmp_path):
    from autoloop.cli import _RESOLUTION_PRECONDITIONS

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / "state",
        workers_root=tmp_path / "workers_root",
        observed_checkout=tmp_path / "observed",
    )
    check = _RESOLUTION_PRECONDITIONS["observed_checkout_unusable"]

    # Absent: the loop rebuilds it, so there is nothing to be wrong about.
    assert check(config) == ""

    (tmp_path / "observed").mkdir()
    (tmp_path / "observed" / "junk.txt").write_text("x\n", encoding="utf-8")
    assert "not a git repository" in check(config)


# =============================================================================
# 5. The two paths that touch a worker repository WITHOUT going through a
#    fresh `WorkerRepoManager.create` — where "the fetch source names a watched
#    tree" and "the boundary always runs" are easiest to lose by omission
# =============================================================================


class _RefusingWorkerRepos:
    """A worker-repo manager that fails the test if anything asks it to build
    or quarantine. The carry-forward path must do NEITHER — that is what makes
    it a carry-forward rather than a re-base."""

    def path_for(self, task_id):
        raise AssertionError("nothing may ask for a worker path here")

    def quarantine(self, task_id, label):
        raise AssertionError("a reviewed candidate's worker is never quarantined")

    def create(self, task_id, source, base_sha, branch=None):
        raise AssertionError("a reviewed candidate's worker is never rebuilt")


def _reviewed_worker(repo_root, tmp_path, base, *, task_id="t1"):
    """A real worker repository (git init + one local fetch, as production
    does) carrying one committed candidate. Returns `(worker, candidate_sha)`."""
    manager = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
    worker = manager.create(task_id, repo_root, base)
    run_git(worker.path, "config", "user.email", "worker@example.com")
    run_git(worker.path, "config", "user.name", "Worker")
    run_git(worker.path, "config", "commit.gpgsign", "false")
    (worker.path / "w.txt").write_text("the task's work\n", encoding="utf-8")
    run_git(worker.path, "add", "-A")
    run_git(worker.path, "commit", "-q", "-m", "the reviewed candidate")
    return worker, run_git(worker.path, "rev-parse", "HEAD").strip()


def _carry_forward_orch(repo_root, tmp_path, execution, observed):
    """An Orchestrator with only what `_rebase_execution_if_stale`'s
    carry-forward branch touches, wired to a REAL observed clone.

    `test_rebase_stale_base.py` and `test_base_refresh_notes.py` own the same
    path with `_observed = None` (the pre-esc-02 primary-checkout behaviour);
    this owns the half where a clone is wired.
    """
    orch = Orchestrator.__new__(Orchestrator)
    orch._policy = PolicyEngine(PolicyConfig())
    orch._git = GitGateway(repo_root, orch._policy)
    orch._worker_repos = _RefusingWorkerRepos()
    orch._observed = observed
    orch._observed_git = None
    orch._observed_synced_sha = ""
    orch._execution_store = TaskExecutionStore(tmp_path / "executions")
    orch._logged: list = []
    orch._log = lambda event, **kw: orch._logged.append((event, kw))
    orch._parked: list = []
    orch._to_needs_user = lambda msg, **kw: orch._parked.append((msg, kw))
    orch._execution_store.save(execution)
    return orch


def _reviewed_record(worker, candidate, base):
    """A record a reviewer has already seen: `review_round` above zero, a
    candidate, and NO publication intent — so `_reconcile_published_execution`
    declines immediately and the carry-forward branch is the one under test."""
    return TaskExecution(
        task_id="t1",
        task_branch=worker.branch,
        worktree_path=str(worker.path),
        task_base_sha=base,
        candidate_sha=candidate,
        review_round=1,
    )


def _move_primary_head(repo_root):
    (repo_root / "mainline.txt").write_text("somebody else shipped\n", encoding="utf-8")
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "mainline shipped something")
    return head_of(repo_root)


def test_a_carried_forward_candidate_fetches_the_head_from_the_clone(tmp_path, monkeypatch):
    """The OTHER path that writes an absolute non-worker path into a worker
    repository, and the one a fix aimed only at `WorkerRepoManager.create`
    misses. Carrying a reviewed candidate past a moved head fetches that head
    INTO the worker, so the source it is spelled must be the watched tree too —
    otherwise `escape_detector`'s claim that the only such path names the clone
    is false for every task that has ever been carried forward.

    The recorded ARGUMENT is the fact under test, for the reason the module
    docstring gives about FETCH_HEAD: the spelling git puts in that file is a
    consequence of this argument and not a contract this repository controls.
    The merge is a real one against the clone, so this also proves a clone can
    actually serve the commit (git's `upload-pack` refuses an unadvertised sha
    — the sync's ref pin is what makes it reachable).
    """
    repo_root = real_repo(tmp_path)
    old_base = head_of(repo_root)
    worker, candidate = _reviewed_worker(repo_root, tmp_path, old_base)
    head = _move_primary_head(repo_root)

    observed = ObservedCheckout(tmp_path / "observed")
    execution = _reviewed_record(worker, candidate, old_base)
    orch = _carry_forward_orch(repo_root, tmp_path, execution, observed)

    sources: list[Path] = []
    real_merge = GitGateway.merge_foreign_commit

    def recording_merge(self, source_path, sha, message, resolve_conflicts=None):
        sources.append(Path(source_path))
        return real_merge(self, source_path, sha, message, resolve_conflicts=resolve_conflicts)

    monkeypatch.setattr(GitGateway, "merge_foreign_commit", recording_merge)

    result = orch._rebase_execution_if_stale(
        execution, Task(id="t1", title="T", description="d")
    )

    assert result is not None and orch._parked == [], "the carry-forward must not park"
    assert sources == [observed.path], "the head was fetched from the operator's checkout"
    assert repo_root.resolve() not in sources
    # And it really was carried forward: the reviewed object still exists and is
    # still reachable, with mainline's commit now on the branch.
    tip = run_git(worker.path, "rev-parse", "HEAD").strip()
    assert tip != candidate, "a merge commit was added"
    assert run_git(worker.path, "cat-file", "-t", candidate).strip() == "commit"
    assert (worker.path / "mainline.txt").read_text() == "somebody else shipped\n"
    assert (worker.path / "w.txt").read_text() == "the task's work\n"
    assert execution.task_base_sha == head
    # The clone holds exactly the commit that was merged, pinned so it could be
    # served at all.
    assert head_of(observed.path) == head
    assert OBSERVED_PIN_PREFIX + head in run_git(observed.path, "show-ref")


def test_a_carry_forward_parks_loop_fatal_when_the_clone_cannot_be_synchronised(tmp_path):
    """Part 3 of the claim on the carry-forward path. The clone is now this
    path's fetch source, so a clone that cannot be established must stop it —
    and must stop it as the LOOP-FATAL it is, not as the task-fatal
    `task_base_behind_head` this branch parks with for its own refusals. The
    worker is left exactly as it was found."""
    repo_root = real_repo(tmp_path)
    old_base = head_of(repo_root)
    worker, candidate = _reviewed_worker(repo_root, tmp_path, old_base)
    _move_primary_head(repo_root)

    observed = ObservedCheckout(tmp_path / "observed")
    observed.path.mkdir(parents=True)
    (observed.path / "not-a-repo.txt").write_text("hello\n", encoding="utf-8")

    execution = _reviewed_record(worker, candidate, old_base)
    orch = _carry_forward_orch(repo_root, tmp_path, execution, observed)

    result = orch._rebase_execution_if_stale(
        execution, Task(id="t1", title="T", description="d")
    )

    assert result is None, "this dispatch stops"
    assert len(orch._parked) == 1, "one park, not two"
    message, kw = orch._parked[0]
    assert kw["code"] == "observed_checkout_unusable", (
        "a clone that cannot be established is an environment failure, not this "
        "task's stale base"
    )
    assert kw["kind"] == "loop_fatal"
    assert "could not be established" in message
    # Nothing was merged, nothing re-pointed, nothing deleted.
    assert run_git(worker.path, "rev-parse", "HEAD").strip() == candidate
    assert run_git(worker.path, "status", "--porcelain").strip() == ""
    assert execution.task_base_sha == old_base
    assert TaskExecutionStore(tmp_path / "executions").load("t1").task_base_sha == old_base
    assert (observed.path / "not-a-repo.txt").exists()


class _RefusingWorktrees:
    """The legacy linked-worktree mechanism, present but never reached: the
    synchronisation boundary must park before anything creates a worktree."""

    def create(self, task_id, base_sha):
        raise AssertionError("nothing may be created past an unusable clone")


def test_the_synchronisation_boundary_is_not_gated_on_the_worker_repo_mechanism(tmp_path):
    """The fail-open a guard keyed on the wrong field would have.

    The snapshot and the clean-baseline gate follow `self._observed`; if the
    SYNC followed `self._worker_repos` instead, a deployment that wires a clone
    while still using the legacy `worktrees` mechanism would never synchronise
    it — and a clone that is CLEAN BUT STALE snapshots identically before and
    after, so the detector would report nothing and nothing would say the tree
    was never brought to this round's commit. The alarm not firing, silently.
    Here the clone is unusable, so the boundary must park before the executor
    is ever called.
    """
    orch, _repo_root, observed, executor, _repos = _build(tmp_path, lambda: None)
    orch._worker_repos = None
    orch._worktrees = _RefusingWorktrees()
    observed.mkdir(parents=True)
    (observed / "not-a-repo.txt").write_text("hello\n", encoding="utf-8")

    orch._dispatch_executor(implement("t1"))

    assert executor.calls == 0, "nothing may run against an unsynchronised clone"
    assert orch.state.park_kind == "loop_fatal"
    assert "could not be established" in (orch.state.question or "")
