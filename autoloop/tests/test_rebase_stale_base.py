"""B9: a retry must not rebuild on a base the branch has moved past.

`TaskExecution.task_base_sha` is recorded once, at first dispatch. Observed
2026-08-02: audit-0001 was refused for two failing tests, those tests were
fixed one commit later, and the retry failed with the SAME two because its
base predated the fix — it would have repeated forever.

The other half, added 2026-08-20 (base-02): a task that has ALREADY been
reviewed must survive the head moving too. Re-basing it is still refused —
that rewrites the reviewed commits' shas and an approval binds to one by exact
sha — so the head is MERGED INTO the task branch instead. Nothing is rewritten,
the reviewed object stays reachable, and only `task_base_sha` moves. What made
this urgent is that human response time was fatal to a candidate: a task parks
for any reason, the head walks forward while its blocker waits for an answer
(every other completion auto-merges), and answering one park caused the next —
23 of 108 parks before that day carried this one code.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from autoloop.auto_merge import MergeDeferralStore
from autoloop.git_gateway import GitGateway
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import Phase
from autoloop.tasks import Task, TaskState
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import TaskExecution, TaskExecutionStore


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def contains(cwd, tip, sha) -> bool:
    """Is `sha` reachable from `tip`? The question "did the reviewed object
    survive" reduces to, and is only ever asked as, this."""
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, tip],
        cwd=str(cwd), capture_output=True,
    ).returncode == 0


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "work")
    git(root, "config", "user.email", "t@e.com")
    git(root, "config", "user.name", "T")
    (root / "f.txt").write_text("one\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    return root


class _FakeWorkerRepos:
    """Records what the re-base asks of it. `create` returns an object with the
    two attributes the caller reads."""

    def __init__(self, root):
        self.root = Path(root)
        self.quarantined: list[str] = []
        self.created: list[str] = []

    def path_for(self, task_id):
        return self.root / task_id

    def quarantine(self, task_id, label):
        self.quarantined.append(label)
        return self.root / f"quarantine/{task_id}-{label}"

    def create(self, task_id, source, base_sha):
        self.created.append(base_sha)

        class _Repo:
            branch = f"autoloop/{task_id}"
            path = self.root / task_id

        return _Repo()


def _orch(repo, tmp_path, execution, review_round=0):
    """An Orchestrator with only what `_rebase_execution_if_stale` touches."""
    from autoloop.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._policy = PolicyEngine(PolicyConfig())
    orch._git = GitGateway(repo, orch._policy)
    orch._worker_repos = _FakeWorkerRepos(tmp_path / "workers")
    # No loop-owned observed checkout (esc-02): this hand-built orchestrator
    # observes the primary checkout, which is the pre-esc-02 behaviour and what
    # every re-base assertion below is about. Set EXPLICITLY rather than left to
    # `__new__`'s missing attribute, because `_rebase_execution_if_stale` now
    # re-synchronises the clone before rebuilding a worker from it — a fixture
    # that leaves the field absent tests an object production never builds.
    # `test_observed_checkout.py` owns the with-a-clone half of this path.
    orch._observed = None
    orch._observed_git = None
    orch._observed_synced_sha = ""
    # Reconciliation asks this before retiring anything — a record an
    # undrained auto-merge retry still reads must not be archived out from
    # under it. Empty here; the pin tests below put a deferral in it.
    orch._merge_deferrals = MergeDeferralStore(tmp_path / "deferrals")
    store = TaskExecutionStore(tmp_path / "executions")
    orch._execution_store = store
    orch._logged: list = []
    orch._log = lambda event, **kw: orch._logged.append((event, kw))
    orch._parked: list = []
    orch._to_needs_user = lambda msg, **kw: orch._parked.append((msg, kw))
    execution.review_round = review_round
    store.save(execution)
    return orch


def _execution(repo, base, **kw):
    return TaskExecution(
        task_id="t1", task_branch="autoloop/t1",
        worktree_path=str(repo / "worker"), task_base_sha=base, **kw
    )


def test_a_base_left_behind_is_rebased_onto_the_current_head(repo, tmp_path):
    old_base = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("two\n")          # the fix that lands later
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "the fix")
    head = git(repo, "rev-parse", "HEAD")

    execution = _execution(repo, old_base, candidate_sha="c" * 40, attempt_count=2)
    orch = _orch(repo, tmp_path, execution)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is not None, "must not park when nothing has been reviewed"
    assert result.task_base_sha == head, "re-pointed at the branch head"
    assert orch._worker_repos.created == [head], "worker rebuilt at the new base"
    assert orch._worker_repos.quarantined, "old worker preserved as evidence, not deleted"
    assert result.candidate_sha == "", "the old candidate belonged to the old base"
    assert result.attempt_count == 2, (
        "attempt_count must survive: a moving base must not refill the retry "
        "budget, or a task could churn forever by re-basing"
    )
    # Persisted, not just mutated in memory — a crash must not lose the re-base.
    assert TaskExecutionStore(tmp_path / "executions").load("t1").task_base_sha == head


def test_an_already_reviewed_candidate_is_never_rebased(repo, tmp_path):
    """The invariant the whole design hangs on, asserted where it is easiest to
    break: whatever else happens to a reviewed record, its candidate is not
    rewritten and its worker is not thrown away. Re-basing changes the reviewed
    commits' shas, and an approval binds to one by exact sha."""
    old_base = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("two\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later")

    execution = _execution(repo, old_base, candidate_sha="c" * 40)
    orch = _orch(repo, tmp_path, execution, review_round=1)

    orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert orch._worker_repos.created == [], "nothing rebuilt"
    assert orch._worker_repos.quarantined == [], "nothing moved"
    assert execution.candidate_sha == "c" * 40, "the reviewed sha is never rewritten"


def test_an_up_to_date_base_is_left_alone(repo, tmp_path):
    head = git(repo, "rev-parse", "HEAD")
    execution = _execution(repo, head)
    orch = _orch(repo, tmp_path, execution)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is execution
    assert orch._worker_repos.created == [] and orch._worker_repos.quarantined == []


def test_a_base_that_is_not_an_ancestor_is_left_alone(repo, tmp_path):
    """A base off the branch entirely (rewritten history) is unusual — stopping
    is safer than silently re-pointing it."""
    unrelated = git(repo, "commit-tree", git(repo, "rev-parse", "HEAD^{tree}"), "-m", "orphan")
    execution = _execution(repo, unrelated)
    orch = _orch(repo, tmp_path, execution)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))
    assert result is execution
    assert orch._worker_repos.created == []


# --- the same drift, the other way: PUBLISHED but still recorded in flight ----
#
# The task lifecycle and the execution record are written in separate places, so
# a task can be published while its record still describes a candidate in
# flight. `merge-window` reported exactly that on 2026-08-15:
#
#   note: task audit-0002: candidate 8d96c52aeca4 is published at
#   origin/refs/heads/autoloop/audit-0002 — safe to merge past, but its record
#   still reads in_progress, so a later revise would park it
#
# Benign for merging, latent for revising: the park below refuses to re-base
# "work a reviewer has already seen", which is right for work that lives only in
# a worker repo and wrong for work that is durable on its own remote branch.


def _published(repo, base, candidate, *, remote="origin"):
    return _execution(
        repo, base,
        candidate_sha=candidate,
        intended_remote=remote,
        intended_remote_ref="refs/heads/autoloop/t1",
        published_sha=candidate,
        published_at="2026-08-15T00:00:00+00:00",
    )


def _with_remote(orch, refs):
    """Point the checkout's `ls-remote` at a fixed answer."""
    orch._git.remote_ref_sha = lambda remote, dest_ref: refs.get((remote, dest_ref), "")
    return orch


def _wire_registry(orch, tmp_path):
    from autoloop.tasks import TaskRegistry, TaskStore

    orch._registry = TaskRegistry([Task(id="t1", title="T", description="d")])
    orch._registry.mark_in_progress("t1")
    orch._task_store = TaskStore(tmp_path / "tasks.json")
    return orch


def test_a_published_candidate_is_reconciled_instead_of_parking(repo, tmp_path):
    """Nothing is discarded by moving on: the reviewed object is on its own
    branch. So the record is retired and the task completed, rather than the
    operator being asked to resolve a conflict that no longer exists."""
    old_base = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("two\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later")

    candidate = "c" * 40
    execution = _published(repo, old_base, candidate)
    orch = _wire_registry(_orch(repo, tmp_path, execution, review_round=1), tmp_path)
    _with_remote(orch, {("origin", "refs/heads/autoloop/t1"): candidate})

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is None, "this dispatch stops either way"
    assert orch._parked == [], "a published candidate is not an operator problem"
    # The record is RETIRED — archived with its worker under one label, so the
    # next dispatch starts clean instead of meeting a record for shipped work.
    store = TaskExecutionStore(tmp_path / "executions")
    assert store.load("t1") is None
    archived = sorted((tmp_path / "executions" / "archive").glob("t1-*.json"))
    assert len(archived) == 1
    assert candidate[:12] in archived[0].name, "the label names what shipped"
    # And the registry is reconciled to what git says.
    assert orch._registry.state_of("t1") is TaskState.COMPLETED
    assert [e for e, _ in orch._logged if e == "execution_retired_published"]


def test_a_record_the_remote_does_not_confirm_still_parks(repo, tmp_path):
    """GIT is the authority, never the record. `published_sha` only says where
    to go and ask — a ref that is missing, or at someone else's commit, means
    the work is not durable anywhere but the worker repo."""
    old_base = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("two\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later")

    execution = _published(repo, old_base, "c" * 40)
    orch = _wire_registry(_orch(repo, tmp_path, execution, review_round=1), tmp_path)
    _with_remote(orch, {("origin", "refs/heads/autoloop/t1"): "9" * 40})

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is None
    assert orch._parked[0][1]["code"] == "task_base_behind_head"
    assert TaskExecutionStore(tmp_path / "executions").load("t1") is not None


def test_a_QUARANTINED_task_is_parked_not_reconciled(repo, tmp_path):
    """Retiring is only safe where completing is. `mark_completed` refuses a
    quarantined task — an operator's quarantine records a decision they have
    not made yet — and `_mark_task_completed` swallows that refusal to a log,
    which is right after a push and wrong here: the record would already be
    retired, leaving no record, no completion, no park, and nothing behind the
    blocker the operator was about to answer."""
    old_base = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("two\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later")

    candidate = "c" * 40
    execution = _published(repo, old_base, candidate)
    orch = _wire_registry(_orch(repo, tmp_path, execution, review_round=1), tmp_path)
    orch._registry.block("t1", "an operator set this aside")
    _with_remote(orch, {("origin", "refs/heads/autoloop/t1"): candidate})

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is None
    assert orch._parked[0][1]["code"] == "task_base_behind_head", (
        "a quarantined task must still reach the operator-facing park"
    )
    assert TaskExecutionStore(tmp_path / "executions").load("t1") is not None
    assert orch._registry.state_of("t1") is TaskState.BLOCKED_BY_OPERATOR


def _defer(orch, task_id="t1", candidate="c" * 40, reason="merge window closed"):
    """An auto-merge retry that has not drained yet — the state
    `AutoMerger.after_completion` reads the live record back for."""
    return orch._merge_deferrals.record(
        task_id=task_id, candidate_sha=candidate,
        dest_ref="refs/heads/work", base_sha="b" * 40,
        reason=reason, now="2026-08-15T00:00:00+00:00",
    )


def test_a_record_an_UNDRAINED_merge_retry_still_needs_is_pinned(repo, tmp_path):
    """Retiring here would break the integration `_dispatch_task_push`
    deliberately kept alive. `AutoMerger.attempt` reads the candidate and the
    worktree path back off the LIVE record; with the record archived it finds
    none, SKIPS the task — which also clears the deferral — and the published
    branch stops being retried, which is the unmerged-forever backlog
    auto-merge exists to end.

    So: completed (the same fresh `ls-remote` justifies it), record kept, no
    park — the park's own advice is to archive the record, which is exactly
    what must not happen while a retry depends on it."""
    old_base = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("two\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later")

    candidate = "c" * 40
    execution = _published(repo, old_base, candidate)
    orch = _wire_registry(_orch(repo, tmp_path, execution, review_round=1), tmp_path)
    _with_remote(orch, {("origin", "refs/heads/autoloop/t1"): candidate})
    _defer(orch)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is None, "this dispatch still stops"
    assert orch._parked == [], "a published candidate is not an operator problem"
    store = TaskExecutionStore(tmp_path / "executions")
    assert store.load("t1") is not None, "the retry has nothing to read without it"
    assert store.load("t1").candidate_sha == candidate
    assert not sorted((tmp_path / "executions" / "archive").glob("t1-*.json"))
    assert orch._worker_repos.quarantined == [], "and its worker is still there"
    assert orch._merge_deferrals.load("t1") is not None, "the retry survives too"
    # Completed, so the merger will still touch it: `attempt` skips (and clears
    # the deferral for) anything that is not COMPLETED.
    assert orch._registry.state_of("t1") is TaskState.COMPLETED
    assert [e for e, _ in orch._logged if e == "execution_retire_pinned_by_deferral"]
    assert [e for e, _ in orch._logged if e == "execution_retired_published"] == []


def test_a_DRAINED_retry_leaves_the_record_free_to_retire(repo, tmp_path):
    """The pin is the deferral, not the history of one. Once the merge landed
    and cleared it, the next dispatch retires the record exactly as before —
    otherwise 'pinned' would quietly mean 'never retired again'."""
    old_base = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("two\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later")

    candidate = "c" * 40
    execution = _published(repo, old_base, candidate)
    orch = _wire_registry(_orch(repo, tmp_path, execution, review_round=1), tmp_path)
    _with_remote(orch, {("origin", "refs/heads/autoloop/t1"): candidate})
    _defer(orch)
    orch._merge_deferrals.clear("t1")        # what a pushed merge does

    orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert TaskExecutionStore(tmp_path / "executions").load("t1") is None
    assert orch._parked == [], "retiring is not a park either"
    assert [e for e, _ in orch._logged if e == "execution_retired_published"]


def test_a_deferral_store_that_cannot_be_read_pins_the_record_too(repo, tmp_path):
    """Fail-closed on the same rule `MergeDeferralStore` states for itself: a
    dropped retry is indistinguishable from work that was never merged, so an
    unanswerable "is one outstanding?" must read as "assume so"."""
    old_base = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("two\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later")

    candidate = "c" * 40
    execution = _published(repo, old_base, candidate)
    orch = _wire_registry(_orch(repo, tmp_path, execution, review_round=1), tmp_path)
    _with_remote(orch, {("origin", "refs/heads/autoloop/t1"): candidate})
    directory = tmp_path / "deferrals"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "t1.json").write_text("{not json", encoding="utf-8")

    orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert TaskExecutionStore(tmp_path / "executions").load("t1") is not None
    assert [e for e, _ in orch._logged if e == "merge_deferral_unreadable"]
    assert [e for e, _ in orch._logged if e == "execution_retire_pinned_by_deferral"]


def test_a_reviewed_candidate_with_no_worker_left_still_parks(repo, tmp_path):
    """The residual park, and the reason it must stay. Carrying a candidate
    forward means merging INSIDE its worker repository; with no such repository
    on disk there is nothing to merge into and nothing to preserve, so this
    falls back to the operator-facing park exactly as it always did."""
    old_base = git(repo, "rev-parse", "HEAD")
    (repo / "f.txt").write_text("two\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later")

    # `_execution` points `worktree_path` at a directory the fixture never
    # creates — which is the whole point here.
    execution = _execution(repo, old_base, candidate_sha="c" * 40)
    orch = _orch(repo, tmp_path, execution, review_round=1)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is None
    message, kw = orch._parked[0]
    assert kw["code"] == "task_base_behind_head"
    assert old_base[:12] in message and "review round has already run" in message
    assert "is not a git repository" in message, "the park says WHY it could not merge"
    assert orch._worker_repos.quarantined == [], "nothing retired without confirmation"
    assert TaskExecutionStore(tmp_path / "executions").load("t1").task_base_sha == old_base


# --- carrying a reviewed candidate past a moved head (base-02) ----------------
#
# The dominant park in the whole log. `task_base_sha` is pinned once, at first
# dispatch; the head walks forward whenever another task completes and
# auto-merges. Consecutive review rounds keep the loop's attention and stay
# fresh (roadmap-01 held one base across EIGHT rounds), so what actually bites
# is an INTERRUPTION: a task parks, sits waiting for a human, and other tasks
# land meanwhile. Measured 2026-08-20: split-01 needed exactly ONE other task
# to complete while it sat; roadmap-01 was passed by 34 commits, was unstuck,
# and re-parked four minutes later.
#
# Every test below builds a REAL worker repository the way production does
# (`WorkerRepoManager.create` — `git init` plus a one-time local fetch, NOT a
# linked worktree), because that separateness is what makes the fetch half of
# the merge load-bearing: the primary checkout's new head is not in the
# worker's object database until it is fetched in.


def _worker(repo, tmp_path, base, *, task_id="t1", path="w.txt", text="worker\n"):
    """A real worker repo carrying one committed candidate on the task branch.
    Returns `(WorkerRepo, candidate_sha)`.

    The local `user.email`/`user.name` are set for the same reason production
    depends on an identity being resolvable at all: `worker_env()` forces
    `GIT_CONFIG_NOSYSTEM` and `GIT_CONFIG_GLOBAL=/dev/null`, so a worker's own
    LOCAL config is the only layer left. The merge commit uses exactly the same
    identity resolution as `commit_and_capture` does in this same repository.
    """
    manager = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
    worker = manager.create(task_id, repo, base)
    git(worker.path, "config", "user.email", "worker@example.com")
    git(worker.path, "config", "user.name", "Worker")
    (worker.path / path).write_text(text)
    git(worker.path, "add", "-A")
    git(worker.path, "commit", "-qm", "the reviewed candidate")
    return worker, git(worker.path, "rev-parse", "HEAD")


def _reviewed(worker, candidate, base, **kw):
    return TaskExecution(
        task_id="t1",
        task_branch=worker.branch,
        worktree_path=str(worker.path),
        task_base_sha=base,
        candidate_sha=candidate,
        **kw,
    )


def _move_head(repo, path="mainline.txt", text="somebody else shipped\n"):
    (repo / path).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", f"mainline: {path}")
    return git(repo, "rev-parse", "HEAD")


def test_a_reviewed_candidate_survives_the_head_moving(repo, tmp_path):
    """THE provable claim. A task that has completed at least one review round
    and is re-dispatched after the head moved must NOT park — and must keep its
    candidate_sha, review_round and attempt_count."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base)
    head = _move_head(repo)

    execution = _reviewed(
        worker, candidate, old_base,
        review_round=2, attempt_count=3, fault_attempt_count=1,
        attempt_ledger=("1|ATTEMPT_TASK|refused",),
    )
    orch = _orch(repo, tmp_path, execution, review_round=2)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is not None, "the dispatch must CONTINUE, not park"
    assert orch._parked == []
    assert result.task_base_sha == head, "and continue against the current head"
    assert result.candidate_sha == candidate, "the reviewed object is still the candidate"
    assert result.review_round == 2, "no round is forgotten"
    assert result.attempt_count == 3, "and neither budget is refilled"
    assert result.fault_attempt_count == 1
    assert result.attempt_ledger == ("1|ATTEMPT_TASK|refused",)
    # Persisted, not merely mutated in memory.
    reloaded = TaskExecutionStore(tmp_path / "executions").load("t1")
    assert reloaded.task_base_sha == head and reloaded.candidate_sha == candidate

    # The worker was merged, never rebased or rebuilt.
    assert orch._worker_repos.created == [] and orch._worker_repos.quarantined == []
    tip = git(worker.path, "rev-parse", "HEAD")
    assert tip != candidate, "a merge commit was added"
    assert git(worker.path, "cat-file", "-t", candidate) == "commit", (
        "the exact object a reviewer approved still exists"
    )
    assert contains(worker.path, tip, candidate), "and is still reachable"
    assert contains(worker.path, tip, head), "with the new head integrated"
    assert (worker.path / "mainline.txt").read_text() == "somebody else shipped\n"
    assert (worker.path / "w.txt").read_text() == "worker\n", "the task's work is untouched"

    logged = [kw["data"] for e, kw in orch._logged if e == "execution_base_carried_forward"]
    assert len(logged) == 1
    assert logged[0]["old_base"] == old_base and logged[0]["new_base"] == head
    assert logged[0]["merge_sha"] == tip


def test_the_next_rounds_diff_shows_only_this_tasks_work(repo, tmp_path):
    """Why `task_base_sha` moves to the new head rather than staying put. Every
    review artifact is a DIRECT tree diff of `task_base_sha..candidate` — so a
    base left behind would attribute everything mainline shipped meanwhile to
    this task, in the reviewer's diff AND in the out-of-scope path check."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base)
    head = _move_head(repo)

    execution = _reviewed(worker, candidate, old_base, review_round=1)
    orch = _orch(repo, tmp_path, execution, review_round=1)
    orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    worker_git = GitGateway(worker.path, PolicyEngine(PolicyConfig()))
    tip = worker_git.head_sha()
    assert execution.task_base_sha == head
    assert worker_git.commit_range_paths(head, tip) == {"w.txt"}, (
        "mainline.txt is mainline's work and must not be attributed to this task"
    )
    # The counter-case, which is what a base left behind would have produced.
    assert worker_git.commit_range_paths(old_base, tip) == {"w.txt", "mainline.txt"}


def test_a_record_written_before_this_change_is_rescued_the_same_way(repo, tmp_path):
    """Four tasks were parked on this code the day it was fixed, with reviewed
    unpublished candidates and stale bases already on disk. A fix that only
    helps records created afterwards leaves all four exactly where they are —
    so the rescue must need no new field and no migration."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base)
    head = _move_head(repo)

    orch = _orch(repo, tmp_path, _reviewed(worker, candidate, old_base), review_round=1)

    # Overwrite what the fixture just wrote with a record in the OLD shape:
    # only the keys that existed before `published_sha` / `attempt_ledger` /
    # `out_of_scope_paths` / the validation binding did. `TaskExecutionStore.
    # load` does `TaskExecution(**data)`, so every absent key takes its
    # default — and the record reaching the rescue is one no version of this
    # code wrote.
    executions = tmp_path / "executions"
    (executions / "t1.json").write_text(json.dumps({
        "task_id": "t1",
        "task_branch": worker.branch,
        "worktree_path": str(worker.path),
        "task_base_sha": old_base,
        "candidate_sha": candidate,
        "candidate_commit_count": 1,
        "review_round": 1,
        "attempt_count": 2,
    }), encoding="utf-8")

    execution = TaskExecutionStore(executions).load("t1")
    assert execution.published_sha == "", "the old shape knows nothing about publication"

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is not None and orch._parked == []
    assert result.task_base_sha == head
    assert result.candidate_sha == candidate and result.review_round == 1
    assert result.attempt_count == 2
    assert contains(worker.path, git(worker.path, "rev-parse", "HEAD"), candidate)


def test_a_real_conflict_parks_instead_of_being_resolved_quietly(repo, tmp_path):
    """Resolving a conflict on a reviewed candidate's behalf is the same quiet
    discard the refusal exists to prevent, one level down. So it parks — and
    leaves the worker exactly as it found it."""
    old_base = git(repo, "rev-parse", "HEAD")
    # Both sides edit f.txt, differently.
    worker, candidate = _worker(repo, tmp_path, old_base, path="f.txt", text="the task's line\n")
    head = _move_head(repo, path="f.txt", text="mainline's line\n")

    execution = _reviewed(worker, candidate, old_base, review_round=1, attempt_count=2)
    orch = _orch(repo, tmp_path, execution, review_round=1)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is None, "a conflict is an operator's decision"
    message, kw = orch._parked[0]
    assert kw["code"] == "task_base_behind_head", "the same code, so the same recovery"
    assert "conflicts at f.txt" in message, "and it names what actually clashed"
    assert head[:12] in kw["detail"] and old_base[:12] in kw["detail"]

    assert execution.task_base_sha == old_base, "nothing was re-pointed"
    assert TaskExecutionStore(tmp_path / "executions").load("t1").task_base_sha == old_base
    assert git(worker.path, "rev-parse", "HEAD") == candidate, "the branch tip is unmoved"
    assert git(worker.path, "status", "--porcelain") == "", "and the merge was aborted"
    assert (worker.path / "f.txt").read_text() == "the task's line\n", "no marker was left"
    assert [e for e, _ in orch._logged if e == "execution_base_carry_forward_refused"]


def test_a_worker_with_uncommitted_work_is_not_merged_over(repo, tmp_path):
    """Residue in a worker is either an interrupted round's work or evidence
    from a failed one. Merging over it could destroy something no reviewer has
    seen, which is precisely what this whole path refuses to do."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base)
    _move_head(repo)
    (worker.path / "half-written.txt").write_text("mid-round\n")

    execution = _reviewed(worker, candidate, old_base, review_round=1)
    orch = _orch(repo, tmp_path, execution, review_round=1)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is None
    assert "uncommitted changes" in orch._parked[0][0]
    assert git(worker.path, "rev-parse", "HEAD") == candidate
    assert (worker.path / "half-written.txt").read_text() == "mid-round\n"


def test_a_branch_tip_that_lost_the_candidate_is_not_merged_into(repo, tmp_path):
    """The claim that the approval binding survives has to be a CHECKED fact,
    not an assumption about which commit the branch happens to be sitting on.
    A tip that no longer contains the reviewed candidate cannot make it."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base)
    _move_head(repo)
    # The branch is moved off the reviewed candidate behind the loop's back.
    git(worker.path, "reset", "-q", "--hard", old_base)

    execution = _reviewed(worker, candidate, old_base, review_round=1)
    orch = _orch(repo, tmp_path, execution, review_round=1)

    result = orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    assert result is None
    assert "does not contain the reviewed candidate" in orch._parked[0][0]
    assert git(worker.path, "rev-parse", "HEAD") == old_base, "and nothing was merged"


def test_a_carried_forward_candidate_is_still_pushable(repo, tmp_path):
    """The consequence of moving the base, closed in the same change. The
    pre-push check asks "is the candidate a descendant of the recorded base" —
    and after a carry-forward the candidate is the merge's PARENT, so the
    direct question answers no for work that is perfectly sound. The branch tip
    carries the same guarantee from the other side."""
    from autoloop.orchestrator import Orchestrator

    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base)
    head = _move_head(repo)

    execution = _reviewed(worker, candidate, old_base, review_round=1)
    orch = _orch(repo, tmp_path, execution, review_round=1)
    orch._rebase_execution_if_stale(execution, Task(id="t1", title="T", description="d"))

    worker_git = GitGateway(worker.path, PolicyEngine(PolicyConfig()))
    assert not worker_git.is_descendant(candidate, head), (
        "the direct question really does answer no — otherwise this proves nothing"
    )
    assert Orchestrator._candidate_is_on_task_line(worker_git, candidate, execution)
    # And it is not a blanket yes: a commit that is not on this branch at all
    # still fails, which is what the check exists for.
    orphan = git(worker.path, "commit-tree", git(worker.path, "rev-parse", "HEAD^{tree}"),
                 "-m", "built somewhere else entirely")
    assert not Orchestrator._candidate_is_on_task_line(worker_git, orphan, execution)


def test_phase_enum_is_untouched_by_this_module():
    assert Phase.NEEDS_USER.value == "needs_user"
