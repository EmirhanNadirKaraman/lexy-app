"""Auto-merge: a completed task's branch reaches the base, or says why not.

Publication is not integration. B10 retires a task the moment its candidate is
confirmed on its own side branch, and until `auto_merge.py` existed that was
the end of it — on 2026-08-06 seven completed tasks were unmerged at once,
including the tab reaper and the Python restart module, fixes for failures the
loop was still hitting while their code sat on branches nobody had pulled.

Real git throughout, self-contained helpers, matching this package's test
convention (see `test_postcommit_primitives.py`'s docstring for why the small
`run_git`/`WritingExecutor` helpers are duplicated rather than imported).

The base branch here is `work`, not `main`: `protected_branches` defaults to
`("main", "master")`, so a `main` base would exercise `push_exact`'s protected
refusal rather than the integration path. That refusal is real and deliberate
— enabling auto-merge is not by itself permission to push `main` — and it has
its own test at the bottom.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autoloop import auto_merge
from autoloop.auto_merge import MergeDeferralStore
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.executor import ExecutionOutcome
from autoloop.git_gateway import GitGateway
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LastResponse, LoopState, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import IntentStore, TaskExecution, TaskExecutionStore
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/auto-merge"
BASE = "work"
BASE_REF = f"refs/heads/{BASE}"


# --- helpers ------------------------------------------------------------------


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def head(repo) -> str:
    return run_git(repo, "rev-parse", "HEAD").strip()


def ref_sha(repo, ref) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", ref], cwd=str(repo), capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def contains(repo, descendant, ancestor) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=str(repo), capture_output=True, text=True,
    ).returncode == 0


def is_clean(repo) -> bool:
    return not run_git(repo, "status", "--porcelain").strip()


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


class WritingExecutor:
    """Writes `per_task[task.id]` into that task's worktree and reports
    success. Duplicated per this package's self-contained test convention."""

    def __init__(self, worktrees_root, per_task):
        self.worktrees_root = Path(worktrees_root)
        self.per_task = {k: dict(v) for k, v in per_task.items()}

    def execute(self, directive, task):
        files = self.per_task[task.id]
        wt = self.worktrees_root / task.id
        for rel, content in files.items():
            target = wt / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return ExecutionOutcome(
            status="ok",
            summary=f"wrote {sorted(files)}",
            details="details",
            validation="ok",
            changed_paths=tuple(files.keys()),
        )


class Harness:
    """Everything a test needs to drive one loop through a real push."""

    def __init__(self, orch, repo, origin, config, execution_store, tasks):
        self.orch = orch
        self.repo = repo
        self.origin = origin
        self.config = config
        self.execution_store = execution_store
        self.tasks = tasks
        self.deferrals = MergeDeferralStore(config.merge_deferrals_dir)

    def stage(self, task_id):
        """Implement + review-packet, stopping just short of the approval.

        Split from `push` so a test can change the world in between — commit
        onto the base, dirty the checkout, move the remote — which is the only
        way to reach the paths that exist for exactly those situations."""
        self.orch._dispatch_executor(
            Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task_id)
        )
        self.orch._step_ready()
        req = self.orch.state.pending_request
        return LastResponse(
            request_id=req.request_id, raw="{}", received_at="now",
            head_sha=req.head_sha, base_sha=req.base_sha,
            report_sha256=req.report_sha256, postcommit=req.postcommit,
        )

    def approve(self, resp):
        """The approval. Auto-merge fires at the end of `_dispatch_task_push`,
        exactly as it does in production."""
        self.orch._dispatch_task_push(
            Directive(decision=Decision.PUSH, reason="approved"), resp
        )

    def push(self, task_id):
        """One full implement -> review -> approve -> push -> integrate."""
        self.approve(self.stage(task_id))
        return self.execution_store.load(task_id)

    def entries(self, entry_type=None):
        path = self.config.transcript_file
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if entry_type is None or r["type"] == entry_type]

    def head(self):
        return head(self.repo)

    def origin_base(self):
        return ref_sha(self.origin, BASE_REF)


def build(tmp_path, *, per_task, auto_merge_enabled=True, base_branch=BASE, seed_files=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", base_branch)
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("hello\n")
    for rel, content in (seed_files or {}).items():
        (repo / rel).write_text(content, encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    run_git(repo, "remote", "add", "origin", str(origin))
    run_git(repo, "push", "-q", "-u", "origin", base_branch)

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(
            implement_enabled=True, auto_merge_enabled=auto_merge_enabled
        ),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    git = GitGateway(repo, PolicyEngine(config.policy))
    worktrees = WorktreeManager(git, tmp_path / "worktrees")
    executor = WritingExecutor(tmp_path / "worktrees", per_task)
    # Under `state_dir`, like production (`cli._build_orchestrator`) — the
    # merge-window gate globs `state_dir/executions/*.json`, so a store
    # anywhere else makes every in-flight candidate invisible to it.
    execution_store = TaskExecutionStore(config.executions_dir)

    tasks = [
        Task(id=tid, title=f"Title {tid}", description="desc",
             approved_paths=tuple(sorted(files)))
        for tid, files in per_task.items()
    ]
    registry = TaskRegistry(tasks)
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

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
        worktrees=worktrees,
        execution_store=execution_store,
        intent_store=IntentStore(config.intents_dir),
        validation_runner=ok_validation,
    )
    return Harness(orch, repo, origin, config, execution_store, {t.id: t for t in tasks})


def in_flight(config, task_id, base_sha, *, review_round=1):
    """An UNPUBLISHED candidate bound to `base_sha` — the thing that must keep
    the merge window shut. `review_round=1` is what makes it unrecoverable:
    `_rebase_execution_if_stale` re-bases a round-0 record but PARKS this one."""
    store = TaskExecutionStore(config.executions_dir)
    execution = TaskExecution(
        task_id=task_id,
        task_branch=f"autoloop/{task_id}",
        worktree_path="",
        task_base_sha=base_sha,
        candidate_sha="c" * 40,
        review_round=review_round,
    )
    store.save(execution)
    return execution


def published_in_flight(config, task_id, base_sha, *, candidate):
    """An in-flight record whose candidate IS on its own side branch. Not
    terminal in the registry, so only the publication exemption can clear
    it — see `cli._merge_window_blockers`."""
    store = TaskExecutionStore(config.executions_dir)
    execution = TaskExecution(
        task_id=task_id,
        task_branch=f"autoloop/{task_id}",
        worktree_path="",
        task_base_sha=base_sha,
        candidate_sha=candidate,
        review_round=1,
        intended_remote="origin",
        intended_remote_ref=f"refs/heads/autoloop/{task_id}",
    )
    store.save(execution)
    return execution


# --- the flag -----------------------------------------------------------------


def test_auto_merge_is_off_by_default():
    """It is the only setting that moves the shared branch head with no
    operator in the loop. Opt-in, always."""
    assert PolicyConfig().auto_merge_enabled is False


def test_with_the_flag_off_a_completed_push_changes_the_base_not_at_all(tmp_path):
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, auto_merge_enabled=False)
    before = h.head()

    h.push("t1")

    assert h.head() == before, "the base must not move while the flag is off"
    assert h.origin_base() == before
    assert h.entries("auto_merge_pushed") == []
    assert h.entries("auto_merge_deferred") == []
    assert h.deferrals.all_deferrals() == []


# --- the happy path -----------------------------------------------------------


def test_a_completed_tasks_branch_is_merged_and_the_base_is_pushed(tmp_path):
    """The whole feature. An unpushed merge would be the same invisibility one
    level down, so the remote base is what this asserts on."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}})
    before = h.head()

    execution = h.push("t1")

    after = h.head()
    assert after != before, "HEAD must actually move"
    assert contains(h.repo, after, execution.candidate_sha), "and must contain the candidate"
    assert contains(h.repo, after, before), "without discarding what was there"
    assert is_clean(h.repo)
    assert h.origin_base() == after, "the base must be PUSHED, not merely merged"
    assert (h.repo / "a.py").read_text() == "one\n"

    logged = h.entries("auto_merge_pushed")
    assert len(logged) == 1
    assert logged[0]["data"]["task_id"] == "t1"
    assert logged[0]["data"]["candidate_sha"] == execution.candidate_sha
    assert h.deferrals.all_deferrals() == [], "a merged task leaves no retry behind"


def test_a_second_run_over_an_already_merged_task_is_a_no_op(tmp_path):
    """Crash-recovery re-enters the push path. Integration must be idempotent
    for the same reason `mark_completed` is."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}})
    execution = h.push("t1")
    merged = h.head()

    h.orch._auto_merge_after_completion("t1")

    assert h.head() == merged
    assert h.origin_base() == merged
    assert [e["data"]["task_id"] for e in h.entries("auto_merge_already_integrated")] == ["t1"]
    assert execution.candidate_sha  # the record is untouched


# --- the gate -----------------------------------------------------------------


def test_an_unpublished_in_flight_candidate_defers_the_merge(tmp_path):
    """The thirteen-stranded case. On 2026-08-06 thirteen tasks held
    unpublished candidates; an eager merge would have parked every one of
    them, because `_rebase_execution_if_stale` refuses to re-base a record a
    reviewer has already seen."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}})
    before = h.head()
    h.orch._registry.add_many([Task(id="t9", title="in flight", description="d")])
    h.orch._task_store.save(h.orch._registry)
    in_flight(h.config, "t9", before)

    h.push("t1")

    assert h.head() == before, "the base must not move while a candidate is bound to it"
    assert h.origin_base() == before
    deferrals = h.deferrals.all_deferrals()
    assert [d.task_id for d in deferrals] == ["t1"]
    assert "merge window closed" in deferrals[0].reason
    assert "t9" in deferrals[0].reason
    assert h.entries("auto_merge_merged") == []
    assert h.orch.state.park_kind in (None, ""), "deferring is not parking"


def test_the_deferral_is_what_keeps_the_in_flight_task_recoverable(tmp_path):
    """The mutation: merge anyway and this fails. `_rebase_execution_if_stale`
    returns the record untouched only while its pinned base is still the branch
    head; once the head moves past it, a reviewed record parks
    (`task_base_behind_head`) and its work is stranded."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}})
    before = h.head()
    h.orch._registry.add_many([Task(id="t9", title="in flight", description="d")])
    h.orch._task_store.save(h.orch._registry)
    record = in_flight(h.config, "t9", before, review_round=1)

    h.push("t1")

    survivor = h.orch._rebase_execution_if_stale(record, h.orch._registry.get("t9"))
    assert survivor is not None, "an eager merge would have parked this task"
    assert survivor.task_base_sha == before
    assert survivor.candidate_sha == "c" * 40


def test_a_deferred_merge_retries_after_the_next_completion(tmp_path):
    """A deferral is durable and drains on the next completion — otherwise
    'defer' would just be a politer word for 'never'."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}, "t2": {"b.py": "two\n"}})
    before = h.head()
    h.orch._registry.add_many([Task(id="t9", title="in flight", description="d")])
    h.orch._task_store.save(h.orch._registry)
    in_flight(h.config, "t9", before)

    first = h.push("t1")
    assert h.head() == before
    assert [d.task_id for d in h.deferrals.all_deferrals()] == ["t1"]
    # The record is durable on disk, not just in this process.
    assert MergeDeferralStore(h.config.merge_deferrals_dir).load("t1") is not None

    # t9 publishes / is cleaned up, and the next task completes.
    (h.config.executions_dir / "t9.json").unlink()
    second = h.push("t2")

    after = h.head()
    assert contains(h.repo, after, first.candidate_sha), "the DEFERRED task merged"
    assert contains(h.repo, after, second.candidate_sha), "and so did the new one"
    assert h.origin_base() == after
    assert h.deferrals.all_deferrals() == []
    assert {e["data"]["task_id"] for e in h.entries("auto_merge_pushed")} == {"t1", "t2"}


def test_a_published_candidate_does_not_hold_the_window_shut(tmp_path):
    """The exemption that keeps this usable at all: a candidate already on its
    own side branch is durable there, so moving the base cannot discard it.

    The record here is deliberately NOT terminal — `in_progress` with a
    published candidate is the real production shape, since a task that
    publishes outside `_dispatch_task_push` never reaches `mark_completed`.
    Gating on the registry alone would close the window on it for good."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}})
    before = h.head()
    h.orch._registry.add_many([Task(id="t8", title="published", description="d")])
    h.orch._task_store.save(h.orch._registry)
    # A real ref on the real remote carrying exactly this candidate.
    run_git(h.repo, "push", "-q", "origin", f"{before}:refs/heads/autoloop/t8")
    published_in_flight(h.config, "t8", before, candidate=before)

    execution = h.push("t1")

    assert h.head() != before, "a published candidate must not block the merge"
    assert contains(h.repo, h.head(), execution.candidate_sha)
    assert h.origin_base() == h.head()
    assert h.deferrals.all_deferrals() == []


# --- a base that moved --------------------------------------------------------


def test_a_moved_remote_base_is_caught_before_anything_is_merged(tmp_path):
    """Detected BEFORE the mutation, not left to the push failing afterwards:
    a merge onto a base the remote is already ahead of produces a head that
    cannot be fast-forwarded, and unwinding it needs the history rewrites this
    gateway deliberately cannot perform."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}})
    before = h.head()

    other = tmp_path / "other"
    run_git(tmp_path, "clone", "-q", "-b", BASE, str(h.origin), str(other))
    run_git(other, "config", "user.email", "other@example.com")
    run_git(other, "config", "user.name", "Other")
    run_git(other, "config", "commit.gpgsign", "false")
    (other / "elsewhere.txt").write_text("someone else was here\n")
    run_git(other, "add", "-A")
    run_git(other, "commit", "-q", "-m", "someone else")
    run_git(other, "push", "-q", "origin", f"HEAD:{BASE_REF}")
    moved = ref_sha(h.origin, BASE_REF)
    assert moved != before

    h.push("t1")

    assert h.head() == before, "nothing may be merged onto a base that moved"
    assert is_clean(h.repo)
    assert h.origin_base() == moved, "and the remote is left exactly as it was"
    assert h.entries("auto_merge_merged") == []
    deferrals = h.deferrals.all_deferrals()
    assert [d.task_id for d in deferrals] == ["t1"]
    assert "the base moved" in deferrals[0].reason
    assert moved[:12] in deferrals[0].reason


def test_a_dirty_checkout_defers_rather_than_merging_into_it(tmp_path):
    """A conflict abort restores the checkout to its pre-merge state — which
    is only a meaningful promise if that state was clean to begin with."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}})
    before = h.head()
    resp = h.stage("t1")
    (h.repo / "README.md").write_text("edited by hand\n")

    h.approve(resp)

    assert h.head() == before
    assert (h.repo / "README.md").read_text() == "edited by hand\n", "left untouched"
    assert "uncommitted changes" in h.deferrals.all_deferrals()[0].reason


# --- conflicts ----------------------------------------------------------------


def test_a_conflict_aborts_leaving_the_base_exactly_as_it_was(tmp_path):
    """Never force, never resolve automatically. The base is restored, the
    branch is left unmerged, and the operator is told which files collided."""
    h = build(
        tmp_path,
        per_task={"t1": {"shared.py": "the task's version\n"}},
        seed_files={"shared.py": "the original\n"},
    )
    origin_at_start = h.origin_base()
    resp = h.stage("t1")

    # Commit a conflicting change onto the base AFTER the candidate exists —
    # the ordinary "someone edited the same lines meanwhile" case.
    (h.repo / "shared.py").write_text("a different version\n")
    run_git(h.repo, "add", "-A")
    run_git(h.repo, "commit", "-q", "-m", "base moves too")
    before = h.head()

    h.approve(resp)

    assert h.head() == before, "the base is exactly as it was"
    assert is_clean(h.repo), "no half-merged checkout is left behind"
    assert (h.repo / "shared.py").read_text() == "a different version\n"
    assert h.origin_base() == origin_at_start, "nothing was pushed"

    conflict = h.entries("auto_merge_conflict")
    assert len(conflict) == 1
    assert conflict[0]["data"]["conflicted_files"] == ["shared.py"], "name the files"
    assert conflict[0]["data"]["restored"] is True
    assert conflict[0]["data"]["task_id"] == "t1"
    assert h.entries("auto_merge_pushed") == []
    # The branch is left unmerged and the candidate is still reachable, so an
    # operator can resolve it by hand.
    execution = h.execution_store.load("t1")
    assert not contains(h.repo, before, execution.candidate_sha)


# --- verification -------------------------------------------------------------


def test_a_merge_that_does_not_move_head_is_a_failure(tmp_path, monkeypatch):
    """A merge command returning 0 is not evidence. Nothing may be pushed off
    the back of a merge that integrated nothing."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}})
    before = h.head()
    monkeypatch.setattr(h.orch._git, "merge_commit", lambda sha, message: None)

    h.push("t1")

    assert h.head() == before
    assert h.origin_base() == before, "a failed merge must never reach the remote"
    failures = h.entries("auto_merge_failed")
    assert len(failures) == 1
    assert "HEAD did not move" in failures[0]["data"]["reason"]
    assert h.entries("auto_merge_pushed") == []


def test_a_merge_that_loses_the_previous_base_is_a_failure(tmp_path, monkeypatch):
    """HEAD moving is not enough either — it must still contain what was
    there. A head that dropped the old base is a rewrite, not a merge, and
    the base's own commits would be silently gone."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}})
    origin_at_start = h.origin_base()
    resp = h.stage("t1")

    # A base commit the candidate does not have, so "moved to the candidate"
    # is demonstrably a LOSS rather than a fast-forward.
    (h.repo / "base_only.txt").write_text("only on the base\n")
    run_git(h.repo, "add", "-A")
    run_git(h.repo, "commit", "-q", "-m", "base moves on")
    before = h.head()

    def rewrite(sha, message):
        run_git(h.repo, "checkout", "-q", "--detach", sha)

    monkeypatch.setattr(h.orch._git, "merge_commit", rewrite)

    h.approve(resp)

    assert not contains(h.repo, head(h.repo), before), "the setup really did lose it"
    assert h.origin_base() == origin_at_start, "nothing may be pushed"
    failures = h.entries("auto_merge_failed")
    assert len(failures) == 1
    assert "does not contain the previous base" in failures[0]["data"]["reason"]
    assert h.entries("auto_merge_pushed") == []


def test_a_protected_base_is_merged_locally_but_never_pushed(tmp_path):
    """`allow_protected_push` still governs the remote. Enabling auto-merge is
    not by itself permission to push `main` — and the residual (a merge that
    exists only locally) is reported, not swallowed."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}}, base_branch="main")
    before = head(h.repo)

    h.push("t1")

    assert head(h.repo) != before, "the merge itself is allowed"
    assert ref_sha(h.origin, "refs/heads/main") == before, "the push is not"
    refused = h.entries("auto_merge_push_refused")
    assert len(refused) == 1
    assert "protected" in refused[0]["data"]["reason"]
    assert [d.task_id for d in h.deferrals.all_deferrals()] == ["t1"], (
        "kept for retry: the merge stands, only the publication is missing"
    )


# --- quarantine ---------------------------------------------------------------


def test_a_task_quarantined_before_integration_is_not_merged(tmp_path):
    """An operator quarantine records a decision they have not finished
    making. Merging over it acts on work explicitly set aside."""
    h = build(tmp_path, per_task={"t1": {"a.py": "one\n"}})
    before = h.head()
    h.orch._dispatch_executor(
        Directive(decision=Decision.IMPLEMENT, reason="do it", task_id="t1")
    )
    h.orch._step_ready()
    req = h.orch.state.pending_request
    resp = LastResponse(
        request_id=req.request_id, raw="{}", received_at="now",
        head_sha=req.head_sha, base_sha=req.base_sha,
        report_sha256=req.report_sha256, postcommit=req.postcommit,
    )
    h.orch._registry.block("t1", "operator set this aside")

    h.orch._dispatch_task_push(Directive(decision=Decision.PUSH, reason="approved"), resp)

    assert h.head() == before
    skipped = h.entries("auto_merge_skipped")
    assert len(skipped) == 1
    assert "not completed" in skipped[0]["data"]["reason"]


# --- the git whitelist --------------------------------------------------------
#
# `merge` is the first MUTATING checkout subcommand on the whitelist, so its
# shape check earns its own tests: everything else there either reads, or
# writes through an already-resolved sha.


def _verdict(*args):
    return PolicyEngine(PolicyConfig()).validate_git_command(args)


def test_the_whitelist_admits_a_merge_of_a_literal_sha():
    assert _verdict("merge", "--no-ff", "--no-edit", "-m", "Merge t1", "a" * 40).allowed


def test_the_whitelist_refuses_merging_a_branch_name():
    """A branch can move between the merge-window check and the merge."""
    verdict = _verdict("merge", "--no-ff", "autoloop/t1")
    assert not verdict.allowed
    assert verdict.code == "git_merge_commit"


def test_the_whitelist_refuses_merging_head():
    assert not _verdict("merge", "--no-ff", "HEAD").allowed


def test_the_whitelist_refuses_two_commits_at_once():
    assert not _verdict("merge", "--no-ff", "a" * 40, "b" * 40).allowed


def test_the_whitelist_admits_a_bare_abort():
    assert _verdict("merge", "--abort").allowed


def test_the_whitelist_refuses_an_abort_carrying_anything_else():
    """`--abort` must restore and nothing more."""
    verdict = _verdict("merge", "--abort", "a" * 40)
    assert not verdict.allowed
    assert verdict.code == "git_merge_abort_shape"


def test_the_whitelist_still_refuses_a_merge_flag_it_does_not_know():
    assert not _verdict("merge", "--strategy=ours", "a" * 40).allowed


# --- the deferral store -------------------------------------------------------


def test_a_repeated_deferral_bumps_the_attempt_count_rather_than_duplicating(tmp_path):
    """One task, one record — the same condition seen twice is not two
    problems (same reasoning as `BlockerStore.record`)."""
    store = MergeDeferralStore(tmp_path / "deferrals")
    store.record(task_id="t1", candidate_sha="a" * 40, dest_ref=BASE_REF,
                 base_sha="b" * 40, reason="window shut", now="2026-08-14T00:00:00Z")
    store.record(task_id="t1", candidate_sha="a" * 40, dest_ref=BASE_REF,
                 base_sha="b" * 40, reason="window still shut", now="2026-08-14T01:00:00Z")

    records = store.all_deferrals()
    assert len(records) == 1
    assert records[0].attempts == 2
    assert records[0].reason == "window still shut"
    assert records[0].created_at == "2026-08-14T00:00:00Z"


def test_deferrals_drain_oldest_first(tmp_path):
    store = MergeDeferralStore(tmp_path / "deferrals")
    for task_id, stamp in (("zz", "2026-08-14T00:00:00Z"), ("aa", "2026-08-14T02:00:00Z")):
        store.record(task_id=task_id, candidate_sha="a" * 40, dest_ref=BASE_REF,
                     base_sha="b" * 40, reason="r", now=stamp)

    assert [d.task_id for d in store.all_deferrals()] == ["zz", "aa"]


def test_a_corrupt_deferral_raises_rather_than_reading_as_absent(tmp_path):
    """A dropped retry is indistinguishable from the unmerged-forever state
    this module exists to end — same rule as every other store here."""
    from autoloop.errors import StateCorruptError

    directory = tmp_path / "deferrals"
    directory.mkdir()
    (directory / "t1.json").write_text("{not json", encoding="utf-8")

    try:
        MergeDeferralStore(directory).load("t1")
    except StateCorruptError:
        return
    raise AssertionError("a corrupt deferral must raise")


def test_the_outcome_slugs_match_the_transcript_types():
    """The log grep and the test assertion must name the same thing."""
    assert auto_merge.MERGED == "merged"
    assert auto_merge.DEFERRED == "deferred"
    assert auto_merge.CONFLICT == "conflict"
