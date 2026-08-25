"""The backlog sweep: branches nobody is going to report are found and merged.

`auto_merge.py` reacts to a completion it sees. A branch published before that
existed — or by a process that died before integrating anything — has no event
left to react to, and until `merge_sweep.py` nothing ever looked for it. On
2026-08-06 seven completed tasks were published and unmerged at once (auto-08,
auto-12, brw-01, brw-07, inbox-09, rt-10, rt-11) with the base still at
d2d4d6b; it took a hand-written `git ls-remote` loop to notice, and two of the
seven were fixes for failures the loop was still hitting.

Real git throughout, self-contained helpers, matching this package's test
convention (see `test_postcommit_primitives.py`'s docstring for why the small
`run_git` helpers are duplicated rather than imported).

The fixture builds the backlog DIRECTLY — a real commit on a side branch, a
real push to a real (local, bare) origin, an execution record and a completed
task — rather than driving the loop through `Orchestrator`, because that is
exactly the shape the sweep has to cope with: work published by a process that
is long gone. `test_auto_merge.py` covers the merge machinery this reuses.

The base branch is `work`, not `main`: `protected_branches` defaults to
`("main", "master")`, so a `main` base would exercise `push_exact`'s protected
refusal rather than integration (that refusal has its own test next door).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from autoloop import auto_merge, cli, merge_sweep
from autoloop.auto_merge import AutoMerger, MergeDeferralStore
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.errors import GitCommandError
from autoloop.git_gateway import GitGateway
from autoloop.merge_sweep import BacklogSweeper
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.transcript import TranscriptLogger
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.worktask import TaskExecution, TaskExecutionStore, retire_execution

URL = "https://chatgpt.com/c/merge-sweep"
BASE = "work"
BASE_REF = f"refs/heads/{BASE}"


# --- helpers ------------------------------------------------------------------


def run_git(cwd, *args, when=None):
    """`when` (an ISO stamp) fixes BOTH git dates, so a test can control the
    committer timestamp the sweep falls back to for ordering."""
    env = None
    if when is not None:
        env = {**os.environ, "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env
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


class Backlog:
    """A checkout, its origin, and whatever published branches a test asks for."""

    def __init__(self, repo, origin, config):
        self.repo = repo
        self.origin = origin
        self.config = config
        self.execution_store = TaskExecutionStore(config.executions_dir)
        self.task_store = TaskStore(config.tasks_file)
        self.registry = TaskRegistry()
        self.task_store.save(self.registry)
        self.deferrals = MergeDeferralStore(config.merge_deferrals_dir)

    # -- building the backlog --

    def commit_on_base(self, files, message="the base moves on"):
        for rel, content in files.items():
            (self.repo / rel).write_text(content, encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-q", "-m", message)
        return self.head()

    def publish(self, task_id, files, *, when=None, published_at="", dest_ref=None,
                base=None, complete=True, branch=None, register=True):
        """One completed task whose reviewed candidate is on its own branch on
        origin and is NOT in the base — the exact state the 2026-08-06 seven
        were found in.

        `branch` / `register=False` publish a SECOND generation for a task the
        registry already knows: a retry after a release, which is what leaves
        several archived records for one task id (`TaskExecutionStore.archive`
        keeps every retirement). The registry refuses a duplicate id and a
        second `mark_completed`, correctly — the task is completed once, by the
        publication that finished it.
        """
        base_sha = base or self.head()
        branch = branch or f"autoloop/{task_id}"
        ref = dest_ref or f"refs/heads/{branch}"
        run_git(self.repo, "checkout", "-q", "-b", branch, base_sha)
        for rel, content in files.items():
            target = self.repo / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-q", "-m", f"work for {task_id}", when=when)
        sha = self.head()
        run_git(self.repo, "push", "-q", "origin", f"{sha}:{ref}")
        run_git(self.repo, "checkout", "-q", BASE)
        self.record(task_id, sha, base_sha, dest_ref=ref, published_at=published_at,
                    complete=complete, register=register)
        return sha

    def record(self, task_id, candidate, base_sha, *, dest_ref, published_at="",
               remote="origin", complete=True, register=True):
        self.execution_store.save(
            TaskExecution(
                task_id=task_id,
                task_branch=f"autoloop/{task_id}",
                worktree_path="",
                task_base_sha=base_sha,
                candidate_sha=candidate,
                review_round=1,
                intended_remote=remote,
                intended_remote_ref=dest_ref,
                published_sha=candidate if remote else "",
                published_at=published_at,
            )
        )
        if not register:
            return
        self.registry.add_many([Task(id=task_id, title=f"Title {task_id}", description="d")])
        if complete:
            self.registry.mark_completed(task_id)
        self.task_store.save(self.registry)

    def complete_without_record(self, task_id):
        """A COMPLETED task whose live execution record is gone — what
        `retire_execution` leaves behind (`_reconcile_published_execution`
        completes the task and archives the record in the same call)."""
        self.registry.add_many([Task(id=task_id, title=f"Title {task_id}", description="d")])
        self.registry.mark_completed(task_id)
        self.task_store.save(self.registry)

    def retire_record(self, task_id, *, label="published"):
        """MOVE the live record into `executions/archive/`, exactly as
        `TaskExecutionStore.archive` does."""
        source = self.config.executions_dir / f"{task_id}.json"
        destination = self.config.executions_dir / "archive" / f"{task_id}-{label}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
        return destination

    def in_flight(self, task_id, base_sha):
        """An UNPUBLISHED candidate bound to the base — what must keep the
        merge window shut (`cli._merge_window_blockers`)."""
        self.execution_store.save(
            TaskExecution(
                task_id=task_id,
                task_branch=f"autoloop/{task_id}",
                worktree_path="",
                task_base_sha=base_sha,
                candidate_sha="c" * 40,
                review_round=1,
            )
        )
        self.registry.add_many([Task(id=task_id, title="in flight", description="d")])
        self.task_store.save(self.registry)

    # -- running it --

    def git(self):
        return GitGateway(self.repo, PolicyEngine(self.config.policy))

    def sweep(self):
        return merge_sweep.sweep_backlog(self.config, git=self.git())

    def sweep_with(self, merger):
        """The sweep over a merger that only records what it was handed —
        enumeration and ORDER, observed without a real merge."""
        return BacklogSweeper(
            config=self.config,
            git=self.git(),
            policy=PolicyEngine(self.config.policy),
            execution_store=self.execution_store,
            registry=self.task_store.load(),
            log=TranscriptLogger(self.config.transcript_file).append,
            merger=merger,
        ).sweep()

    def real_merger(self):
        """A REAL `AutoMerger`, wired exactly as `sweep_backlog` wires one, for
        the tests that need real merges under an observed attempt order."""
        return AutoMerger(
            config=self.config,
            git=self.git(),
            policy=PolicyEngine(self.config.policy),
            execution_store=self.execution_store,
            registry=self.task_store.load(),
            log=TranscriptLogger(self.config.transcript_file).append,
        )

    # -- observing it --

    def entries(self, entry_type=None):
        path = self.config.transcript_file
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if entry_type is None or r["type"] == entry_type]

    def sweep_entries(self):
        return [r for r in self.entries() if r["type"].startswith("merge_sweep")]

    def head(self):
        return head(self.repo)

    def origin_base(self):
        return ref_sha(self.origin, BASE_REF)


class RecordingMerger:
    """Stands in for `AutoMerger`. Records the order it was asked to integrate
    and reports whatever the test wants back."""

    def __init__(self, outcomes=None):
        self.attempted = []
        self.outcomes = dict(outcomes or {})

    def attempt(self, task_id, seen=None):
        self.attempted.append(task_id)
        return self.outcomes.get(task_id, auto_merge.MERGED)


class MeddlingMerger:
    """A REAL `AutoMerger` with a hand on the remote between branches.

    `between(n)` runs after the n-th branch has been integrated and before the
    sweep looks at the next one — the window in which a second operator, a
    `release` or a CI job can move a ref the sweep confirmed at enumeration
    time and has not asked about since.
    """

    def __init__(self, inner, between):
        self._inner = inner
        self._between = between
        self.attempted = []

    def attempt(self, task_id, seen=None):
        self.attempted.append(task_id)
        outcome = self._inner.attempt(task_id, seen)
        self._between(len(self.attempted))
        return outcome


def build(tmp_path, *, auto_merge_enabled=True, seed_files=None, protected=None):
    """`protected` overrides `protected_branches`. Passing `(BASE,)` is how a
    test gets a REAL refused push: the merge runs and verifies, `push_exact`
    then refuses the base ref, and the checkout is left with a head the remote
    has never seen. No mock reproduces that shape, and it is the one that comes
    back as `auto_merge.DEFERRED` — the slug that otherwise means "nothing was
    touched"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", BASE)
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
    run_git(repo, "push", "-q", "-u", "origin", BASE)

    policy_kwargs = {"auto_merge_enabled": auto_merge_enabled}
    if protected is not None:
        policy_kwargs["protected_branches"] = tuple(protected)
    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(**policy_kwargs),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers",
    )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return Backlog(repo, origin, config)


# --- the flag -----------------------------------------------------------------


def test_the_sweep_is_off_unless_auto_merge_is_on(tmp_path):
    """It moves the shared branch head with no operator in the loop, exactly
    as `auto_merge.py` does, so it lives behind the same opt-in flag."""
    b = build(tmp_path, auto_merge_enabled=False)
    before = b.head()
    b.publish("t1", {"a.py": "one\n"})

    result = b.sweep()

    assert result.outcome == merge_sweep.DISABLED
    assert b.head() == before
    assert b.origin_base() == before
    assert b.sweep_entries() == []


# --- the whole point ----------------------------------------------------------


def test_a_published_branch_that_is_not_an_ancestor_of_head_is_merged(tmp_path):
    """The 2026-08-06 case, one branch of it. Nothing reported this work — the
    sweep has to go and find it."""
    b = build(tmp_path)
    before = b.head()
    candidate = b.publish("auto-08", {"reaper.py": "close the tabs\n"})
    assert not contains(b.repo, before, candidate), "the setup really is unmerged"

    result = b.sweep()

    after = b.head()
    assert result.outcome == merge_sweep.SWEPT
    assert result.merged == ["auto-08"]
    assert after != before, "HEAD must actually move"
    assert contains(b.repo, after, candidate), "and must contain the candidate"
    assert contains(b.repo, after, before), "without discarding what was there"
    assert is_clean(b.repo)
    assert b.origin_base() == after, "the base must be PUSHED, not merely merged"
    assert (b.repo / "reaper.py").read_text() == "close the tabs\n"
    assert [e["data"]["task_id"] for e in b.entries("auto_merge_pushed")] == ["auto-08"]
    assert b.entries("merge_sweep_completed")[0]["data"]["merged"] == ["auto-08"]


def test_the_whole_backlog_is_swept_not_just_the_first_branch(tmp_path):
    """Seven at once was the real number. One sweep, one pass, all of them."""
    b = build(tmp_path)
    shas = {
        task_id: b.publish(task_id, {f"{task_id}.py": f"work for {task_id}\n"},
                           published_at=stamp)
        for task_id, stamp in (
            ("auto-08", "2026-08-06T01:00:00+00:00"),
            ("brw-01", "2026-08-06T02:00:00+00:00"),
            ("rt-10", "2026-08-06T03:00:00+00:00"),
        )
    }

    result = b.sweep()

    assert result.outcome == merge_sweep.SWEPT
    assert result.merged == ["auto-08", "brw-01", "rt-10"]
    after = b.head()
    for task_id, sha in shas.items():
        assert contains(b.repo, after, sha), f"{task_id} never reached the base"
    assert b.origin_base() == after
    assert result.pending == []


def test_merged_ness_is_decided_by_ancestry_not_by_the_branch_name(tmp_path):
    """A name match is what made the backlog invisible in the first place. The
    branch here follows no convention at all and is still integrated."""
    b = build(tmp_path)
    candidate = b.publish(
        "odd-01", {"odd.py": "one\n"}, dest_ref="refs/heads/some/other/naming"
    )

    result = b.sweep()

    assert result.merged == ["odd-01"]
    assert contains(b.repo, b.head(), candidate)


# --- what must NOT be touched -------------------------------------------------


def test_a_branch_already_an_ancestor_of_head_is_skipped_silently(tmp_path):
    """The ordinary case for every task the loop has ever completed. One log
    line each would bury the handful that actually need integrating — and a
    second merge of an already-merged branch is a head-move for nothing."""
    b = build(tmp_path)
    candidate = b.publish("done-01", {"a.py": "one\n"})
    run_git(b.repo, "merge", "--no-ff", "--no-edit", "-m", "operator merged it", candidate)
    merged_head = b.head()
    assert contains(b.repo, merged_head, candidate)

    result = b.sweep()

    assert result.outcome == merge_sweep.NOTHING_TO_DO
    assert result.is_clear is True, "this IS the provably-clear case"
    assert result.merged == [] and result.pending == []
    assert b.head() == merged_head, "nothing may move for a branch already in the base"
    assert [e["type"] for e in b.sweep_entries()] == [merge_sweep.SWEEP_IDLE_EVENT], (
        "per-BRANCH silence is unchanged; one terminal entry per invocation is "
        "what lets a reader tell a clear sweep from a sweep that never ran "
        "(sweep-01)"
    )
    assert b.entries("auto_merge_merged") == [], "the merger was never called"


def test_a_task_that_is_not_completed_is_left_alone(tmp_path):
    """Same rule `AutoMerger.attempt` applies task by task: an in-flight or
    quarantined task's candidate is not the sweep's to integrate."""
    b = build(tmp_path)
    before = b.head()
    b.publish("live-01", {"a.py": "one\n"}, complete=False)

    result = b.sweep()

    assert result.outcome == merge_sweep.NOTHING_TO_DO
    assert b.head() == before
    assert b.origin_base() == before


def test_a_completed_task_whose_branch_is_gone_is_named_not_merged(tmp_path):
    """`_mark_task_completed` only fires on a confirmed publication, so a
    completed candidate with no branch carrying it means the ref was deleted or
    force-moved afterwards. There is nothing here to merge FROM, and inventing
    it from the record's own claim is the fail-open reading
    `_candidate_publication` exists to refuse."""
    b = build(tmp_path)
    before = b.head()
    candidate = b.publish("gone-01", {"a.py": "one\n"})
    run_git(b.repo, "push", "-q", "origin", "--delete", "refs/heads/autoloop/gone-01")

    result = b.sweep()

    assert [t for t, _why in result.unresolved] == ["gone-01"]
    assert result.is_clear is False, "a branch it could not judge is not a clear backlog"
    assert b.head() == before
    assert not contains(b.repo, b.head(), candidate)
    assert b.entries("merge_sweep_unresolved")[0]["data"]["task_id"] == "gone-01"


def test_a_remote_that_cannot_be_REACHED_never_reports_a_clear_backlog(tmp_path,
                                                                      monkeypatch):
    """The costly half of the same rule. `_candidate_publication` cannot tell
    "the ref is gone" from "the remote did not answer", and must not: an
    unverifiable answer is not an answer. Reporting an offline run as
    `nothing_to_do` would say "I looked, the backlog is clear" for a run in
    which nothing could be looked at — the 2026-08-06 invisibility rebuilt
    inside the tool written to end it."""
    b = build(tmp_path)
    before = b.head()
    candidate = b.publish("auto-08", {"a.py": "one\n"})

    def offline(self, remote, dest_ref):
        raise GitCommandError("ls-remote", "Could not read from remote repository")

    monkeypatch.setattr(GitGateway, "remote_ref_sha", offline)

    result = b.sweep()

    assert result.is_clear is False, "an unreachable remote is not a clear backlog"
    assert [t for t, _why in result.unresolved] == ["auto-08"]
    assert "could not verify" in result.unresolved[0][1]
    assert result.merged == []
    assert b.head() == before, "nothing may be merged on evidence nobody could read"
    assert not contains(b.repo, b.head(), candidate)


# --- metadata the sweep cannot read -------------------------------------------


def test_an_execution_record_it_cannot_READ_is_never_a_clear_backlog(tmp_path):
    """The corrupt-record hole, and the reason this task exists in a second
    round. A completed task's record is the ONE thing naming the branch its
    completion says was published; a record that will not load therefore means
    "I could not look", never "there is nothing there". It used to log
    `merge_sweep_error` and simply continue, so a sweep whose only completed
    task had a torn record returned `nothing_to_do` with nothing skipped —
    `is_clear` true, exit 0, having inspected precisely nothing.

    The task is the ONLY completed one on purpose: that is the exact shape that
    exited 0.
    """
    b = build(tmp_path)
    before = b.head()
    b.publish("torn-01", {"a.py": "one\n"})
    (b.config.executions_dir / "torn-01.json").write_text("{not json", encoding="utf-8")

    result = b.sweep()

    assert result.is_clear is False, (
        "a record the sweep could not read is not a provably clear backlog"
    )
    assert [t for t, _why in result.unresolved] == ["torn-01"]
    assert "could not be read" in result.unresolved[0][1]
    assert result.merged == []
    assert b.head() == before, "nothing may be merged on metadata nobody could read"
    assert b.entries("merge_sweep_unresolved")[0]["data"]["task_id"] == "torn-01"


def test_a_completed_task_with_no_record_at_all_is_unresolved(tmp_path):
    """Completion implies a confirmed publication, so a completed task with no
    record — live or archived — has a branch somewhere that nothing here can
    name. Silence would be the 2026-08-06 invisibility exactly."""
    b = build(tmp_path)
    b.complete_without_record("ghost-01")

    result = b.sweep()

    assert result.is_clear is False
    assert [t for t, _why in result.unresolved] == ["ghost-01"]
    assert "no execution record, live or archived" in result.unresolved[0][1]


def test_a_retired_record_whose_work_IS_in_the_base_stays_silent(tmp_path):
    """The other side of the same rule, and what stops it crying wolf.
    `retire_execution` archives the record of a task that published, so every
    such task would otherwise be permanently unresolved. The archived copy
    answers the question authoritatively — `published_sha == candidate_sha` is
    the remote's own confirmation, and the candidate is an ancestor of HEAD —
    so this one is provably clear and says nothing about the task at all (only
    the invocation's own terminal entry, naming nothing unresolved)."""
    b = build(tmp_path)
    candidate = b.publish("retired-01", {"a.py": "one\n"})
    run_git(b.repo, "merge", "--no-ff", "--no-edit", "-m", "merged earlier", candidate)
    merged_head = b.head()
    b.retire_record("retired-01")

    result = b.sweep()

    assert result.outcome == merge_sweep.NOTHING_TO_DO
    assert result.is_clear is True, "provably integrated is provably clear"
    assert result.unresolved == []
    assert b.head() == merged_head
    assert [e["type"] for e in b.sweep_entries()] == [merge_sweep.SWEEP_IDLE_EVENT], (
        "per-BRANCH silence is unchanged; one terminal entry per invocation is "
        "what lets a reader tell a clear sweep from a sweep that never ran "
        "(sweep-01)"
    )


def test_a_LEGACY_archive_with_no_published_sha_is_judged_on_ancestry_alone(tmp_path):
    """`published_sha` only exists from 2026-08-15, so every archive written
    before it has no such key. Requiring one would be a stricter rule than the
    LIVE path applies — that one asks ancestry and nothing else — and a rule
    with a date on it: every legacy archive would report unresolved forever,
    with its candidate demonstrably merged and no action an operator could
    take. Git is authoritative about what is in the base and needs no second
    opinion; the remote's confirmation answers a different question."""
    b = build(tmp_path)
    candidate = b.publish("legacy-01", {"a.py": "one\n"})
    run_git(b.repo, "merge", "--no-ff", "--no-edit", "-m", "merged earlier", candidate)
    archived = b.retire_record("legacy-01")
    record = json.loads(archived.read_text(encoding="utf-8"))
    del record["published_sha"]
    del record["published_at"]
    archived.write_text(json.dumps(record), encoding="utf-8")

    result = b.sweep()

    assert result.is_clear is True, (
        "an archived candidate that IS an ancestor is provably integrated, "
        "whether or not the record predates the field that says the remote agreed"
    )
    assert result.unresolved == []
    assert [e["type"] for e in b.sweep_entries()] == [merge_sweep.SWEEP_IDLE_EVENT]


def test_a_retired_record_whose_work_is_NOT_in_the_base_is_unresolved(tmp_path):
    """Retirement means the publication was confirmed, never that it was
    merged: `_reconcile_published_execution` archives the record and completes
    the task in one call, and with `auto_merge_enabled` off nothing integrates
    it. Reading the archive is what keeps that branch visible — and it is only
    ever READ, never merged from, since `AutoMerger.attempt` loads the live
    record and would skip the task anyway."""
    b = build(tmp_path)
    before = b.head()
    candidate = b.publish("stranded-01", {"a.py": "one\n"})
    b.retire_record("stranded-01")

    result = b.sweep()

    assert result.is_clear is False
    assert [t for t, _why in result.unresolved] == ["stranded-01"]
    assert "merge it by hand" in result.unresolved[0][1]
    assert result.merged == []
    assert b.head() == before
    assert not contains(b.repo, b.head(), candidate)
    assert b.entries("auto_merge_skipped") == [], "it was never handed to the merger"


def test_an_OLDER_archived_retirement_cannot_stand_in_for_the_newest(tmp_path):
    """`TaskExecutionStore.archive` keeps EVERY retirement — a release, a later
    retry, the `published-<sha>` retirement that completed the task — and those
    generations describe different commits. Judging the task on whichever
    archived copy happens to name an ancestor therefore clears it on the
    strength of a superseded attempt: the first one landed and was released, the
    retry that actually completed the task is still sitting on its branch, and
    the sweep reports the backlog clear. That is the 2026-08-06 invisibility
    granted by the reconciler written to end it.

    The older copy here really is integrated, so this fails against the
    first-integrated-archive-wins rule rather than passing vacuously.
    """
    b = build(tmp_path)
    released = b.publish("twice-01", {"a.py": "one\n"})
    run_git(b.repo, "merge", "--no-ff", "--no-edit", "-m", "attempt one landed", released)
    merged_head = b.head()
    b.retire_record("twice-01", label="released-by-operator-20260801T000000Z")
    retry = b.publish(
        "twice-01", {"b.py": "two\n"},
        branch="autoloop/twice-01-retry", register=False,
    )
    b.retire_record("twice-01", label=f"published-{retry[:12]}-20260810T000000Z")
    assert contains(b.repo, merged_head, released), "the OLDER attempt really did land"

    result = b.sweep()

    assert result.is_clear is False, (
        "an older ancestral retirement must not answer for a newer publication"
    )
    assert [t for t, _why in result.unresolved] == ["twice-01"]
    assert "merge it by hand" in result.unresolved[0][1]
    assert "20260810T000000Z" in result.unresolved[0][1], (
        "the operator is told WHICH generation is outstanding"
    )
    assert b.head() == merged_head
    assert not contains(b.repo, b.head(), retry), "the newest publication is unmerged"


def test_the_NEWEST_retirement_being_in_the_base_clears_a_task_with_older_ones(tmp_path):
    """The control that stops the rule above from being merely strict. A
    superseded generation is NOT required to have landed — a released attempt's
    candidate is usually abandoned and never merged — so demanding that every
    archived copy be an ancestor would report every retried task unresolved
    forever, the same wolf-crying from the other direction. Only the newest is
    asked, and when it answers, the task is silent."""
    b = build(tmp_path)
    abandoned = b.publish("retried-01", {"a.py": "one\n"})
    b.retire_record("retried-01", label="released-by-operator-20260801T000000Z")
    shipped = b.publish(
        "retried-01", {"b.py": "two\n"},
        branch="autoloop/retried-01-retry", register=False,
    )
    run_git(b.repo, "merge", "--no-ff", "--no-edit", "-m", "the retry landed", shipped)
    merged_head = b.head()
    b.retire_record("retried-01", label=f"published-{shipped[:12]}-20260810T000000Z")
    assert not contains(b.repo, merged_head, abandoned), "the older attempt never landed"

    result = b.sweep()

    assert result.is_clear is True, "the newest publication is provably integrated"
    assert result.unresolved == []
    assert b.head() == merged_head
    assert [e["type"] for e in b.sweep_entries()] == [merge_sweep.SWEEP_IDLE_EVENT], (
        "per-BRANCH silence is unchanged; one terminal entry per invocation is "
        "what lets a reader tell a clear sweep from a sweep that never ran "
        "(sweep-01)"
    )


def test_archived_copies_that_cannot_be_PUT_IN_ORDER_are_unresolved(tmp_path):
    """Generation comes from the archive filename — `retire_execution` appends a
    fixed-width UTC instant to every label — so a label without one cannot be
    placed against the others, and the unstamped copy could be the newest. An
    unorderable archive is unresolved rather than guessed at, matching every
    other unanswerable question here. (A task with exactly ONE archived copy has
    nothing to order and is judged directly; that is what keeps pre-stamp
    records answerable, and the two tests above cover it.)"""
    b = build(tmp_path)
    landed = b.publish("murky-01", {"a.py": "one\n"})
    run_git(b.repo, "merge", "--no-ff", "--no-edit", "-m", "attempt one landed", landed)
    b.retire_record("murky-01", label="released-by-operator")
    b.publish("murky-01", {"b.py": "two\n"},
              branch="autoloop/murky-01-retry", register=False)
    b.retire_record("murky-01", label="published-20260810T000000Z")

    result = b.sweep()

    assert result.is_clear is False
    assert [t for t, _why in result.unresolved] == ["murky-01"]
    assert "cannot be put in order" in result.unresolved[0][1]
    assert "murky-01-released-by-operator.json" in result.unresolved[0][1], (
        "the operator is told which file has no stamp"
    )


def test_another_TASKS_archive_cannot_answer_through_a_shared_id_PREFIX(tmp_path):
    """The archive is globbed by filename prefix, and task ids may contain `-`
    (`TaskRegistry`'s rule admits it), so `rt-1-*.json` matches
    `rt-1-b-published-<stamp>.json`. Judging the newest generation makes that
    sharper rather than safer: the sibling's copy here carries the newer stamp,
    so without an owner check it BECOMES `rt-1`'s newest generation and answers
    with a commit that has nothing to do with `rt-1` — the sibling's work landed,
    so `rt-1` would be reported clear while its own branch sits outstanding.

    `rt-1-b` is genuinely integrated, so this fails against the unguarded
    version rather than passing vacuously.
    """
    b = build(tmp_path)
    stranded = b.publish("rt-1", {"a.py": "one\n"})
    b.retire_record("rt-1", label="published-20260801T000000Z")
    sibling = b.publish("rt-1-b", {"b.py": "two\n"})
    run_git(b.repo, "merge", "--no-ff", "--no-edit", "-m", "the sibling landed", sibling)
    b.retire_record("rt-1-b", label="published-20260810T000000Z")

    result = b.sweep()

    assert [t for t, _why in result.unresolved] == ["rt-1"], (
        "rt-1's own branch is outstanding; rt-1-b's really did land"
    )
    assert "merge it by hand" in result.unresolved[0][1]
    assert "rt-1-b" not in result.unresolved[0][1], (
        "and the sibling's copy is not what it was judged on"
    )
    assert result.is_clear is False
    assert not contains(b.repo, b.head(), stranded)


def test_a_newest_archived_copy_that_cannot_be_READ_is_unresolved(tmp_path):
    """Same fail-closed rule as the live record's. The newest generation is the
    one that answers, so a newest copy that will not load leaves the question
    unanswered — an older readable one does not get to answer in its place."""
    b = build(tmp_path)
    landed = b.publish("torn-arch-01", {"a.py": "one\n"})
    run_git(b.repo, "merge", "--no-ff", "--no-edit", "-m", "attempt one landed", landed)
    b.retire_record("torn-arch-01", label="released-by-operator-20260801T000000Z")
    b.publish("torn-arch-01", {"b.py": "two\n"},
              branch="autoloop/torn-arch-01-retry", register=False)
    newest = b.retire_record("torn-arch-01", label="published-20260810T000000Z")
    newest.write_text("{not json", encoding="utf-8")

    result = b.sweep()

    assert result.is_clear is False
    assert [t for t, _why in result.unresolved] == ["torn-arch-01"]
    assert "newest archived copy could not be read" in result.unresolved[0][1]


def test_a_completed_record_that_names_no_candidate_is_unresolved(tmp_path):
    """Completion means a candidate was published, so a completed task's record
    that names none cannot be describing that publication. Skipping it silently
    (as an in-flight record with no candidate is skipped, correctly, by
    `_merge_window_blockers`) would write off the branch it fails to name."""
    b = build(tmp_path)
    b.execution_store.save(
        TaskExecution(
            task_id="blank-01",
            task_branch="autoloop/blank-01",
            worktree_path="",
            task_base_sha=b.head(),
            candidate_sha="",
            review_round=1,
        )
    )
    b.registry.add_many([Task(id="blank-01", title="blank", description="d")])
    b.registry.mark_completed("blank-01")
    b.task_store.save(b.registry)

    result = b.sweep()

    assert result.is_clear is False
    assert [t for t, _why in result.unresolved] == ["blank-01"]
    assert "names no candidate" in result.unresolved[0][1]


# --- an unjudgeable task holds the WHOLE sweep --------------------------------


def test_a_branch_DESCENDED_from_an_unjudgeable_task_is_not_merged_either(tmp_path):
    """Naming an unjudgeable task and sweeping on is not safe, because this
    module deliberately supports a later branch being cut from an earlier one.

    Publish A, cut B from A, then lose A's ref before the enumeration while B's
    is still confirmed. A is excluded — correctly, nothing here may merge a
    publication the remote will not confirm. But B is a DESCENDANT of A, so
    merging B makes A an ancestor of HEAD anyway, and A's publication was never
    confirmed by anything. The refusal to merge A directly would be undone
    transitively by the very next branch in the list.
    """
    b = build(tmp_path)
    before = b.head()
    a_sha = b.publish(
        "a-first", {"shared.py": "line one\n"}, published_at="2026-08-06T01:00:00+00:00"
    )
    b_sha = b.publish(
        "b-on-a", {"shared.py": "line one\nline two\n"},
        base=a_sha, published_at="2026-08-06T02:00:00+00:00",
    )
    assert contains(b.repo, b_sha, a_sha), "B really is built on A"
    run_git(b.repo, "push", "-q", "origin", "--delete", "refs/heads/autoloop/a-first")

    result = b.sweep()

    assert result.outcome == merge_sweep.HELD
    assert result.merged == [], "nothing may be merged while a task is unjudged"
    assert [t for t, _why in result.unresolved] == ["a-first"]
    assert result.pending == ["b-on-a"], "the withheld branch is named, not swept"
    assert result.is_clear is False
    assert b.head() == before, "the base is exactly as it was"
    assert not contains(b.repo, b.head(), b_sha)
    assert not contains(b.repo, b.head(), a_sha), (
        "and the unconfirmed publication must not arrive as B's ancestor"
    )
    assert b.origin_base() == before, "nothing was pushed"
    assert b.entries("auto_merge_merged") == [], "the merger was never called"
    held = b.entries("merge_sweep_held")[0]["data"]
    assert held["unresolved"] == ["a-first"] and held["pending"] == ["b-on-a"]


def test_a_task_whose_RECORD_will_not_load_holds_the_sweep_the_same_way(tmp_path):
    """The less provable half of the same shape. A publication the remote denies
    at least leaves a candidate sha to reason about; a record nobody can read
    names no commit at all, so whether the branches below it descend from its
    work is not merely unknown but unaskable. Fail closed identically."""
    b = build(tmp_path)
    before = b.head()
    b.publish("torn-01", {"a.py": "one\n"}, published_at="2026-08-06T01:00:00+00:00")
    later = b.publish("later-01", {"b.py": "two\n"},
                      published_at="2026-08-06T02:00:00+00:00")
    (b.config.executions_dir / "torn-01.json").write_text("{not json", encoding="utf-8")

    result = b.sweep()

    assert result.outcome == merge_sweep.HELD
    assert result.merged == []
    assert [t for t, _why in result.unresolved] == ["torn-01"]
    assert result.pending == ["later-01"]
    assert b.head() == before
    assert not contains(b.repo, b.head(), later)


def test_the_hold_is_per_INVOCATION_not_per_lineage(tmp_path):
    """Deliberately an over-approximation: the withheld branch here shares no
    history with the unjudgeable task beyond the base they were both cut from.

    Excluding only the candidates that DESCEND from an unresolved one would need
    the ancestry of a commit the sweep may be unable to name or resolve at all —
    the same unanswerable question one step along — so the invariant is the
    coarse one. It costs a delay and nothing else: nothing has been mutated at
    the point the sweep holds, and the next run re-derives the work-list from
    git ancestry.
    """
    b = build(tmp_path)
    before = b.head()
    unrelated = b.publish("unrelated-01", {"elsewhere.py": "untouched\n"})
    b.publish("gone-01", {"a.py": "one\n"})
    run_git(b.repo, "push", "-q", "origin", "--delete", "refs/heads/autoloop/gone-01")

    result = b.sweep()

    assert result.outcome == merge_sweep.HELD
    assert result.merged == []
    assert result.pending == ["unrelated-01"]
    assert not contains(b.repo, b.head(), unrelated)
    assert b.head() == before


def test_an_unjudgeable_task_with_NOTHING_to_sweep_is_not_reported_as_held(tmp_path):
    """`held` is a claim about branches that were withheld, so it must not fire
    when there were none to withhold — `pending == []` under a line saying
    "N branch(es) left untouched" reads as a lie. The task is still unresolved
    and the run still is not clear; that is `is_clear`'s job, not the outcome's."""
    b = build(tmp_path)
    b.publish("gone-01", {"a.py": "one\n"})
    run_git(b.repo, "push", "-q", "origin", "--delete", "refs/heads/autoloop/gone-01")

    result = b.sweep()

    assert result.outcome == merge_sweep.NOTHING_TO_DO
    assert result.pending == []
    assert result.is_clear is False, "unjudged is still not clear"
    assert b.entries("merge_sweep_held") == []


# --- order --------------------------------------------------------------------


def test_branches_are_attempted_oldest_publication_first(tmp_path):
    """Arbitrary order manufactures conflicts that do not really exist: a
    branch cut from another branch only applies cleanly after the one it builds
    on. Records with no `published_at` predate the field, so they are older
    than every record that has one; the candidate's committer date orders that
    older group among itself."""
    b = build(tmp_path)
    # Deliberately published in an order that is neither the expected one nor
    # the registry's insertion order.
    b.publish("new-late", {"d.py": "d\n"}, published_at="2026-08-14T00:00:00+00:00")
    b.publish("old-second", {"b.py": "b\n"}, when="2026-08-05T00:00:00+0000")
    b.publish("new-early", {"c.py": "c\n"}, published_at="2026-08-10T00:00:00+00:00")
    b.publish("old-first", {"a.py": "a\n"}, when="2026-08-01T00:00:00+0000")
    merger = RecordingMerger()

    result = b.sweep_with(merger)

    assert merger.attempted == ["old-first", "old-second", "new-early", "new-late"]
    assert result.outcome == merge_sweep.SWEPT
    assert b.entries("merge_sweep_backlog")[0]["data"]["pending"] == merger.attempted


def test_a_branch_built_on_another_applies_cleanly_in_publication_order(tmp_path):
    """The reason order matters at all, with real git: `second` edits the file
    `first` created, so merging it first would collide with a base that has
    never seen `first`."""
    b = build(tmp_path)
    first = b.publish(
        "first", {"shared.py": "line one\n"}, published_at="2026-08-06T01:00:00+00:00"
    )
    second = b.publish(
        "second", {"shared.py": "line one\nline two\n"},
        base=first, published_at="2026-08-06T02:00:00+00:00",
    )

    result = b.sweep()

    assert result.merged == ["first", "second"]
    after = b.head()
    assert contains(b.repo, after, first) and contains(b.repo, after, second)
    assert (b.repo / "shared.py").read_text() == "line one\nline two\n"
    assert b.entries("auto_merge_conflict") == [], "publication order avoids the collision"


# --- stopping -----------------------------------------------------------------


def test_the_sweep_stops_at_the_first_conflict_with_the_base_unchanged(tmp_path):
    """A half-swept backlog with one branch aborted mid-way is harder to reason
    about than a clean stop — and the operator has to resolve that conflict
    before the rest mean anything."""
    b = build(tmp_path, seed_files={"shared.py": "the original\n"})
    b.publish(
        "conflicting", {"shared.py": "the task's version\n"},
        published_at="2026-08-06T01:00:00+00:00",
    )
    later = b.publish(
        "later", {"elsewhere.py": "untouched\n"},
        published_at="2026-08-06T02:00:00+00:00",
    )
    # The base moves onto the same lines AFTER the candidate was cut — the
    # ordinary "someone edited this meanwhile" case.
    before = b.commit_on_base({"shared.py": "a different version\n"})
    origin_at_start = b.origin_base()

    result = b.sweep()

    assert result.outcome == merge_sweep.STOPPED
    assert result.stopped_on == "conflicting"
    assert result.stopped_outcome == auto_merge.CONFLICT
    assert result.merged == []
    assert result.is_reconciled is True, (
        "an abort that restored the head and the tree IS the restored case — the "
        "claim is checked against the checkout, not assumed from the outcome"
    )
    assert b.head() == before, "the base is exactly as it was"
    assert is_clean(b.repo), "no half-merged checkout is left behind"
    assert (b.repo / "shared.py").read_text() == "a different version\n"
    assert b.origin_base() == origin_at_start, "nothing was pushed"

    conflict = b.entries("auto_merge_conflict")
    assert len(conflict) == 1
    assert conflict[0]["data"]["conflicted_files"] == ["shared.py"]
    assert conflict[0]["data"]["restored"] is True

    stopped = b.entries("merge_sweep_stopped")[0]["data"]
    assert stopped["task_id"] == "conflicting"
    assert stopped["remaining"] == ["conflicting", "later"]
    assert not contains(b.repo, b.head(), later), "the rest are untouched"


def test_a_conflict_halfway_leaves_every_later_branch_untouched(tmp_path):
    """Not "skip it and carry on". The branches after the conflict may well
    depend on the one that did not land, so attempting them would be guessing."""
    b = build(tmp_path, seed_files={"shared.py": "the original\n"})
    clean = b.publish(
        "clean", {"a.py": "one\n"}, published_at="2026-08-06T01:00:00+00:00"
    )
    b.publish(
        "collides", {"shared.py": "the task's version\n"},
        published_at="2026-08-06T02:00:00+00:00",
    )
    never = b.publish(
        "never-tried", {"b.py": "two\n"}, published_at="2026-08-06T03:00:00+00:00"
    )
    b.commit_on_base({"shared.py": "a different version\n"})

    result = b.sweep()

    assert result.outcome == merge_sweep.STOPPED
    assert result.merged == ["clean"], "everything before the conflict still landed"
    assert result.pending == ["collides", "never-tried"]
    after = b.head()
    assert contains(b.repo, after, clean)
    assert not contains(b.repo, after, never), "nothing past the conflict was attempted"
    assert b.origin_base() == after, "what did merge was pushed"
    assert [e["data"]["task_id"] for e in b.entries("auto_merge_pushed")] == ["clean"]
    assert is_clean(b.repo)


def test_a_merge_that_cannot_be_verified_stops_the_sweep_too(tmp_path, monkeypatch):
    """`AutoMerger._merge` deliberately does NOT undo a merge that failed
    verification (`reset` is off the git whitelist), so stacking the next
    branch onto a head nobody understands is worse than a conflict."""
    b = build(tmp_path)
    b.publish("first", {"a.py": "one\n"}, published_at="2026-08-06T01:00:00+00:00")
    later = b.publish("later", {"b.py": "two\n"}, published_at="2026-08-06T02:00:00+00:00")
    before = b.head()
    monkeypatch.setattr(GitGateway, "merge_commit", lambda self, sha, message: None)

    result = b.sweep()

    assert result.outcome == merge_sweep.STOPPED
    assert result.stopped_outcome == auto_merge.FAILED
    assert result.pending == ["first", "later"]
    assert b.head() == before
    assert b.origin_base() == before, "a failed merge must never reach the remote"
    assert not contains(b.repo, b.head(), later)
    assert "HEAD did not move" in b.entries("auto_merge_failed")[0]["data"]["reason"]
    assert result.is_reconciled is True, (
        "this particular failure mutated nothing — the merge command was a no-op"
    )


# --- a stop is not automatically a restoration --------------------------------
#
# Stopping keeps the sweep from stacking a second merge onto a head nobody
# understands. It does NOT by itself mean the checkout came back. Two outcomes
# leave the base MOVED, and one of them wears the slug that otherwise means
# "nothing was touched", so the claim has to be an observation of the checkout
# rather than a reading of the outcome.


def _merge_for_real_then_dirty_the_tree(b, monkeypatch):
    """A REAL merge — real commit, real head move, all three of
    `_verify_merge`'s ancestry checks satisfied — that then leaves a file behind,
    so verification fails on its last check (`the checkout is dirty after the
    merge`) with the merge already in the checkout.

    A stray write after a merge is not exotic: a `post-merge` hook, an editor, a
    watcher. What matters is that this is the shape `AutoMerger._merge`
    deliberately does not undo, because `reset` is off the git whitelist.
    """
    real_merge = GitGateway.merge_commit

    def merge_then_dirty(self, sha, message):
        real_merge(self, sha, message)
        (b.repo / "left-behind.txt").write_text("a hook wrote this\n", encoding="utf-8")

    monkeypatch.setattr(GitGateway, "merge_commit", merge_then_dirty)


def test_a_merge_that_moved_HEAD_then_failed_verification_is_NOT_called_restored(
    tmp_path, monkeypatch
):
    """The base really did move, and nothing here will move it back. Reporting
    "the base is exactly as it was before this branch" for that — which is what
    every STOPPED outcome used to print — tells the operator the one thing that
    would stop them looking."""
    b = build(tmp_path)
    before = b.head()
    candidate = b.publish("auto-08", {"a.py": "one\n"},
                          published_at="2026-08-06T01:00:00+00:00")
    later = b.publish("later", {"b.py": "two\n"},
                      published_at="2026-08-06T02:00:00+00:00")
    _merge_for_real_then_dirty_the_tree(b, monkeypatch)

    result = b.sweep()

    assert result.outcome == merge_sweep.STOPPED
    assert result.stopped_outcome == auto_merge.FAILED
    assert result.is_reconciled is False
    assert "HEAD moved from" in result.unreconciled
    assert before[:12] in result.unreconciled and b.head()[:12] in result.unreconciled, (
        "both ends are named — the operator has to find the merge to judge it"
    )
    assert b.head() != before, "the merge really is in the checkout"
    assert contains(b.repo, b.head(), candidate)
    assert b.origin_base() == before, "and never reached the remote"
    assert not contains(b.repo, b.head(), later), "the rest were left alone"
    assert result.base_before == before and result.base_after == b.head()

    stopped = b.entries("merge_sweep_stopped")[0]["data"]
    assert stopped["base_sha_before_attempt"] == before
    assert stopped["base_sha_after_attempt"] == b.head()
    assert stopped["unreconciled"], "the transcript carries it too, not just stdout"


def test_a_REFUSED_push_leaves_the_base_moved_under_the_deferred_slug(tmp_path):
    """The case that cannot be classified by outcome. `_push` catches the
    refusal and calls `_defer`, so this comes back as `auto_merge.DEFERRED` —
    the same slug a shut gate or a dirty checkout produces, both of which touch
    nothing. Here the merge ran, verified, and moved the base to a commit the
    remote has never seen.

    Reproduced with no mock at all: `protected_branches` containing the base is
    exactly the pairing `PolicyConfig` documents (auto-merge on is not by itself
    permission to push `main`), and it is a configuration an operator can
    plausibly be running.
    """
    b = build(tmp_path, protected=(BASE,))
    before = b.head()
    candidate = b.publish("auto-08", {"a.py": "one\n"})

    result = b.sweep()

    assert result.outcome == merge_sweep.STOPPED
    assert result.stopped_outcome == auto_merge.DEFERRED, (
        "the slug that otherwise means 'a precondition failed, nothing moved'"
    )
    assert result.is_reconciled is False, (
        "and the checkout says otherwise — which is why the slug is not asked"
    )
    assert b.head() != before and contains(b.repo, b.head(), candidate)
    assert b.origin_base() == before, "the merge is local only"
    assert is_clean(b.repo), "a clean tree is not evidence the base is where it was"
    assert b.entries("auto_merge_push_refused"), "the refusal is the real one"


def test_a_conflict_whose_ABORT_did_not_restore_is_not_called_restored(tmp_path,
                                                                       monkeypatch):
    """`_abort` already distrusts a zero exit from `git merge --abort` and logs
    `restored: false` when the head or the tree disagrees. The sweep has to
    reach the same conclusion independently, because it is the layer deciding
    whether anything else may run in this checkout — and the outcome it gets
    back is `CONFLICT` either way."""
    b = build(tmp_path, seed_files={"shared.py": "the original\n"})
    b.publish("collides", {"shared.py": "the task's version\n"})
    before = b.commit_on_base({"shared.py": "a different version\n"})
    monkeypatch.setattr(GitGateway, "merge_abort", lambda self: None)

    result = b.sweep()

    assert result.stopped_outcome == auto_merge.CONFLICT
    assert result.is_reconciled is False
    assert "the working tree is not what the attempt found" in result.unreconciled
    assert b.head() == before, "a conflicted merge does not move HEAD"
    assert not is_clean(b.repo), "it leaves the conflict in the tree"
    assert b.entries("auto_merge_conflict")[0]["data"]["restored"] is False


def test_a_probe_that_cannot_read_the_checkout_is_not_read_as_unchanged(tmp_path):
    """"Could not look" is not "nothing moved" — the same rule the enumeration
    applies to a branch, applied to the base. Asserted on the helper directly:
    getting git into a state where `rev-parse` fails midway through a merge is
    not reproducible, and the decision is one comparison."""
    clean = merge_sweep._Checkout(head="a" * 40, dirty=())
    assert merge_sweep._unreconciled(clean, clean) == ""
    unreadable = merge_sweep._Checkout(error="GitCommandError: no such ref")
    assert "unknown" in merge_sweep._unreconciled(clean, unreadable)
    assert "unknown" in merge_sweep._unreconciled(unreadable, clean)
    assert "no such ref" in merge_sweep._unreconciled(unreadable, unreadable), (
        "two unreadable observations are not a match either, and the operator is "
        "told WHAT would not answer rather than only that something did not"
    )


# --- publication is re-confirmed per branch, at the moment it is merged -------


def test_a_branch_DELETED_after_enumeration_is_not_merged_on_stale_evidence(tmp_path):
    """`seen` memoizes CONFIRMED publications, and the enumeration fills it for
    every candidate before the first merge runs. Share that positive cache with
    the merges and branch N is integrated on an `ls-remote` taken before
    branches 1..N-1 were even attempted — so a delete or force-move during the
    sweep (a second operator, a `release`, a CI job) is HIDDEN by the cache
    rather than caught by it. A seven-branch sweep is minutes of merging and
    pushing; that window is real.

    Here `second`'s ref is deleted after `first` lands. The candidate must not
    reach `AutoMerger.attempt` at all — with the stale cache it does, and it
    merges, because `attempt`'s own publication check hits the same cached
    positive.
    """
    b = build(tmp_path)
    first = b.publish("first", {"a.py": "one\n"}, published_at="2026-08-06T01:00:00+00:00")
    second = b.publish("second", {"b.py": "two\n"}, published_at="2026-08-06T02:00:00+00:00")

    def meddle(count):
        if count == 1:
            run_git(b.repo, "push", "-q", "origin", "--delete", "refs/heads/autoloop/second")

    merger = MeddlingMerger(b.real_merger(), meddle)
    result = b.sweep_with(merger)

    assert merger.attempted == ["first"], (
        "the second candidate must never reach the merger on enumeration-time evidence"
    )
    assert result.outcome == merge_sweep.STOPPED
    assert result.stopped_on == "second"
    assert result.stopped_outcome == merge_sweep.UNCONFIRMED
    assert result.merged == ["first"]
    assert result.pending == ["second"], "the remainder is reported, not swept"
    assert result.is_clear is False

    after = b.head()
    assert contains(b.repo, after, first), "what landed before the change stands"
    assert not contains(b.repo, after, second)
    assert b.origin_base() == after, "and was pushed"
    changed = b.entries("merge_sweep_publication_changed")[0]["data"]
    assert changed["task_id"] == "second"
    assert "does not exist" in changed["reason"]


def test_a_branch_MOVED_after_enumeration_is_not_merged_either(tmp_path):
    """The sneakier half: the ref still exists, it just no longer carries the
    reviewed candidate. Only a fresh `ls-remote` compared against the recorded
    candidate can tell — the ref being present is exactly what a cached
    positive would keep reporting."""
    b = build(tmp_path)
    first = b.publish("first", {"a.py": "one\n"}, published_at="2026-08-06T01:00:00+00:00")
    second = b.publish("second", {"b.py": "two\n"}, published_at="2026-08-06T02:00:00+00:00")

    def meddle(count):
        if count == 1:
            run_git(
                b.repo, "push", "-q", "-f", "origin",
                f"{first}:refs/heads/autoloop/second",
            )

    merger = MeddlingMerger(b.real_merger(), meddle)
    result = b.sweep_with(merger)

    assert merger.attempted == ["first"]
    assert result.stopped_on == "second"
    assert result.stopped_outcome == merge_sweep.UNCONFIRMED
    assert not contains(b.repo, b.head(), second)
    assert "not the candidate" in (
        b.entries("merge_sweep_publication_changed")[0]["data"]["reason"]
    )


def test_the_re_confirmation_costs_one_lookup_per_branch_not_two(tmp_path, monkeypatch):
    """Fresh evidence, without paying twice for it. `_reconfirm` evicts only
    the candidate's own key and `_candidate_publication` re-adds it on success,
    so `AutoMerger.attempt`'s step 3 reads the answer this just obtained rather
    than making a second round-trip for the same ref."""
    b = build(tmp_path)
    b.publish("solo-01", {"a.py": "one\n"})
    asked = []
    real = GitGateway.remote_ref_sha

    def counting(self, remote, dest_ref):
        asked.append(dest_ref)
        return real(self, remote, dest_ref)

    monkeypatch.setattr(GitGateway, "remote_ref_sha", counting)

    result = b.sweep()

    assert result.merged == ["solo-01"]
    side = [ref for ref in asked if ref == "refs/heads/autoloop/solo-01"]
    assert len(side) == 2, (
        "one lookup for the enumeration, one immediately before the merge — a "
        f"third would mean `attempt` re-asked what `_reconfirm` just cached: {asked}"
    )


# --- the gate -----------------------------------------------------------------


def test_a_shut_gate_defers_the_whole_sweep_rather_than_merging_part_of_it(tmp_path):
    """The thirteen-stranded case, applied to a sweep. An unpublished candidate
    is bound to the current base; moving it strands that task
    (`_rebase_execution_if_stale` parks a record a reviewer has already seen).
    The gate is checked ONCE, before the first merge — letting each branch
    discover it would write one deferral per branch for one condition."""
    b = build(tmp_path)
    before = b.head()
    first = b.publish("first", {"a.py": "one\n"}, published_at="2026-08-06T01:00:00+00:00")
    second = b.publish("second", {"b.py": "two\n"}, published_at="2026-08-06T02:00:00+00:00")
    b.in_flight("live-09", before)

    result = b.sweep()

    assert result.outcome == merge_sweep.DEFERRED
    assert result.merged == []
    assert result.pending == ["first", "second"], "the whole list waits, not part of it"
    assert b.head() == before, "the base must not move while a candidate is bound to it"
    assert b.origin_base() == before
    assert not contains(b.repo, b.head(), first)
    assert not contains(b.repo, b.head(), second)
    assert b.entries("auto_merge_merged") == []
    assert b.entries("merge_sweep_stopped") == []

    deferred = b.entries("merge_sweep_deferred")[0]["data"]
    assert deferred["pending"] == ["first", "second"]
    assert any("live-09" in reason for reason in deferred["reasons"])
    assert b.deferrals.all_deferrals() == [], (
        "the sweep keeps no queue of its own — it re-derives the work-list from "
        "git ancestry every time, so a per-branch deferral would be duplicate state"
    )


def test_a_candidate_ALREADY_behind_the_head_lets_the_whole_backlog_through(tmp_path):
    """The measured 2026-08-21 failure, end to end through real git.

    blk-01's candidate was bound to a base already 10 commits behind the head,
    split-01's to one 12 behind. Both held the window shut with "merging would
    strand it" — a harm inflicted ten and twelve commits ago — while four
    finished, reviewed and published branches waited a day for a merge, two of
    them loop fixes that stay inert until merged. Moving the head cannot strand
    a candidate that is already behind it, so the sweep proceeds, and it
    proceeds ALL of the way: the all-or-nothing rule is unchanged, so what this
    proves is that every outstanding branch lands in ONE pass.
    """
    b = build(tmp_path)
    stale = b.head()
    b.in_flight("blk-01", stale)
    # The head walks past that recorded base — an operator merge, another
    # task's auto-merge — which is exactly the state the record was found in.
    moved = b.commit_on_base({"moved.txt": "the head moved on\n"})
    assert contains(b.repo, moved, stale) and moved != stale

    first = b.publish("first", {"a.py": "one\n"}, published_at="2026-08-20T12:11:00+00:00")
    second = b.publish("second", {"b.py": "two\n"}, published_at="2026-08-20T13:41:00+00:00")
    third = b.publish("third", {"c.py": "three\n"}, published_at="2026-08-20T22:26:00+00:00")

    result = b.sweep()

    assert result.outcome == merge_sweep.SWEPT
    assert result.merged == ["first", "second", "third"], "one pass, not one branch"
    assert result.pending == []
    for candidate in (first, second, third):
        assert contains(b.repo, b.head(), candidate)
    assert b.origin_base() == b.head(), "and the base reached the remote"

    # Visible, not merely unblocked: the record still needs a merge-forward or
    # a recut before it can be reviewed again, so the gate says so.
    notes = [e["data"]["note"] for e in b.entries("merge_sweep_window_note")]
    assert any("blk-01" in note and "ALREADY behind" in note for note in notes), notes
    assert b.entries("merge_sweep_deferred") == []


def test_a_candidate_at_the_head_STILL_shuts_the_gate_after_the_head_moves(tmp_path):
    """The narrowing is not a one-time amnesty, and it is not a self-granting
    one either. Once the head moves, whatever is bound to the NEW head is
    in-flight work again and holds the window exactly as before — otherwise this
    would trade a permanently shut window for a permanently open one.

    TWO outstanding branches rather than one, deliberately. The gate is
    evaluated ONCE against the head as it stands before anything is attempted,
    so the all-or-nothing rule has to keep BOTH branches off the base. Letting
    the first land would move the head past live-09's recorded base, and the
    second would then qualify for the new already-behind note — the exemption
    granting itself, one merge at a time, on a record that was genuinely
    in-flight when the sweep began. The proof is the base itself: local AND
    remote byte-for-byte at the shas the sweep found them at, which is a claim
    an outcome slug cannot make on its own (a refused push returns `DEFERRED`
    over a base that has already moved locally).

    The moved head is pushed as well, so the two ends start out agreeing and
    "unchanged" means the same thing at both of them.
    """
    b = build(tmp_path)
    b.commit_on_base({"moved.txt": "the head moved on\n"})
    run_git(b.repo, "push", "-q", "origin", BASE)   # the operator's merge reached origin too
    b.in_flight("live-09", b.head())            # bound to the head as it is NOW
    first = b.publish("first", {"a.py": "one\n"}, published_at="2026-08-06T01:00:00+00:00")
    second = b.publish("second", {"b.py": "two\n"}, published_at="2026-08-06T02:00:00+00:00")
    before, origin_before = b.head(), b.origin_base()
    assert before == origin_before, "the two ends start out at the same sha"

    result = b.sweep()

    assert result.outcome == merge_sweep.DEFERRED
    assert result.merged == []
    assert result.pending == ["first", "second"], "the whole list waits, not part of it"
    assert b.head() == before, (
        "not one branch may move the base while a candidate is bound to the head "
        "the gate was evaluated against"
    )
    assert b.origin_base() == origin_before, "and the remote base is untouched too"
    assert not contains(b.repo, b.head(), first)
    assert not contains(b.repo, b.head(), second)
    assert b.entries("auto_merge_merged") == [], "no merge ran, so none was logged"
    assert b.entries("merge_sweep_stopped") == [], "deferred before the first attempt"

    reasons = b.entries("merge_sweep_deferred")[0]["data"]["reasons"]
    assert any("live-09" in reason and "IS the current head" in reason
               for reason in reasons), reasons
    notes = [e["data"]["note"] for e in b.entries("merge_sweep_window_note")]
    assert not any("live-09" in note for note in notes), (
        f"the at-head candidate is a blocker, never an already-behind note: {notes}"
    )


def test_an_executing_phase_defers_the_sweep(tmp_path):
    """The other half of the same predicate: an agent may be mid-write in the
    checkout. Reached through `cli._merge_window_blockers`, called not copied."""
    from autoloop.state import LoopState, Phase, StateStore

    b = build(tmp_path)
    before = b.head()
    b.publish("first", {"a.py": "one\n"})
    StateStore(b.config.state_file).save(
        LoopState(session_id="s", conversation_url=URL, phase=Phase.EXECUTING.value)
    )

    result = b.sweep()

    assert result.outcome == merge_sweep.DEFERRED
    assert b.head() == before
    assert any("executing" in reason for reason in result.reasons)


def test_a_dirty_checkout_defers_rather_than_merging_into_it(tmp_path):
    """A conflict abort restores the checkout to its pre-merge state, which is
    only a meaningful promise if that state was clean. The refusal comes from
    `AutoMerger.attempt`, so the sweep stops on it like any other non-landing."""
    b = build(tmp_path)
    before = b.head()
    b.publish("first", {"a.py": "one\n"})
    (b.repo / "README.md").write_text("edited by hand\n", encoding="utf-8")

    result = b.sweep()

    assert result.outcome == merge_sweep.STOPPED
    assert result.stopped_outcome == auto_merge.DEFERRED
    assert b.head() == before
    assert (b.repo / "README.md").read_text() == "edited by hand\n", "left untouched"


# --- the CLI ------------------------------------------------------------------


def _args():
    return argparse.Namespace(config=None)


def test_the_command_sweeps_and_exits_zero(tmp_path, monkeypatch, capsys):
    b = build(tmp_path)
    candidate = b.publish("auto-08", {"a.py": "one\n"})
    monkeypatch.setattr(cli, "load_config", lambda _p: b.config)
    monkeypatch.chdir(b.repo)

    assert cli._cmd_merge_backlog(_args()) == 0

    assert contains(b.repo, b.head(), candidate)
    out = capsys.readouterr().out
    assert "swept" in out and "auto-08" in out


def test_the_command_exits_one_when_it_stops(tmp_path, monkeypatch, capsys):
    """Exit code is the contract: 0 = the backlog is clear, 1 = it is not."""
    b = build(tmp_path, seed_files={"shared.py": "the original\n"})
    b.publish("collides", {"shared.py": "the task's version\n"})
    b.publish("never-tried", {"b.py": "two\n"}, published_at="2026-08-06T03:00:00+00:00")
    b.commit_on_base({"shared.py": "a different version\n"})
    before = b.head()
    monkeypatch.setattr(cli, "load_config", lambda _p: b.config)
    monkeypatch.chdir(b.repo)

    assert cli._cmd_merge_backlog(_args()) == 1

    assert b.head() == before
    out = capsys.readouterr().out
    assert "STOPPED at" in out and "collides" in out
    assert "never-tried" in out, "the untouched remainder must be named"


def test_the_command_does_not_claim_a_base_it_left_MOVED_is_where_it_was(
    tmp_path, monkeypatch, capsys
):
    """`_format_sweep` is shared with the startup hook, and it printed "the base
    is exactly as it was before this branch" for every STOPPED outcome — while
    the same module documents that a merge which failed verification is
    deliberately left in place. The operator most likely to act on that line is
    the one it is wrong for."""
    b = build(tmp_path)
    before = b.head()
    b.publish("auto-08", {"a.py": "one\n"})
    _merge_for_real_then_dirty_the_tree(b, monkeypatch)
    monkeypatch.setattr(cli, "load_config", lambda _p: b.config)
    monkeypatch.chdir(b.repo)

    assert cli._cmd_merge_backlog(_args()) == 1

    out = capsys.readouterr().out
    assert "the base is exactly as it was" not in out
    assert "UNRECONCILED" in out
    assert "the base is NOT as it was" in out
    assert before[:12] in out and b.head()[:12] in out, "both ends are on screen"
    assert "reset" in out, "and why nothing here will undo it for them"


def test_the_command_exits_one_when_a_branch_could_not_be_judged(tmp_path, monkeypatch,
                                                                capsys):
    """Exit 0 must mean "provably clear", never "I could not look". The two
    are the same failure this whole module exists to end, one layer up."""
    b = build(tmp_path)
    b.publish("auto-08", {"a.py": "one\n"})
    run_git(b.repo, "push", "-q", "origin", "--delete", "refs/heads/autoloop/auto-08")
    monkeypatch.setattr(cli, "load_config", lambda _p: b.config)
    monkeypatch.chdir(b.repo)

    assert cli._cmd_merge_backlog(_args()) == 1

    out = capsys.readouterr().out
    assert "UNJUDGED" in out and "auto-08" in out
    assert "already an ancestor" not in out, (
        "it must not claim the backlog is clear on a branch it could not judge"
    )


def test_the_command_exits_one_when_a_record_could_not_be_READ(tmp_path, monkeypatch,
                                                               capsys):
    """The exit-code half of the corrupt-record hole. One completed task, its
    record torn, nothing else in the backlog: the sweep inspected nothing and
    must not answer 0. It did, until 2026-08-15."""
    b = build(tmp_path)
    b.publish("torn-01", {"a.py": "one\n"})
    (b.config.executions_dir / "torn-01.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(cli, "load_config", lambda _p: b.config)
    monkeypatch.chdir(b.repo)

    assert cli._cmd_merge_backlog(_args()) == 1

    out = capsys.readouterr().out
    assert "UNJUDGED" in out and "torn-01" in out
    assert "could not be read" in out, "the operator is told WHICH thing would not answer"
    assert "already an ancestor" not in out, (
        "it must not claim the backlog is clear on metadata it could not read"
    )


def test_the_command_names_the_branches_it_WITHHELD(tmp_path, monkeypatch, capsys):
    """Holding the sweep is only defensible if the operator can see what it is
    holding up. Both halves have to be on screen: the task that could not be
    judged, and the branch that is waiting on it."""
    b = build(tmp_path)
    before = b.head()
    b.publish("gone-01", {"a.py": "one\n"}, published_at="2026-08-06T01:00:00+00:00")
    b.publish("waiting-01", {"b.py": "two\n"}, published_at="2026-08-06T02:00:00+00:00")
    run_git(b.repo, "push", "-q", "origin", "--delete", "refs/heads/autoloop/gone-01")
    monkeypatch.setattr(cli, "load_config", lambda _p: b.config)
    monkeypatch.chdir(b.repo)

    assert cli._cmd_merge_backlog(_args()) == 1

    assert b.head() == before
    out = capsys.readouterr().out
    assert "held" in out
    assert "UNJUDGED" in out and "gone-01" in out
    assert "waiting-01" in out, "the withheld branch must be named"
    assert "DESCENDED" in out, "and WHY a judgeable branch was not merged anyway"


def test_the_command_refuses_when_the_flag_is_off(tmp_path, monkeypatch, capsys):
    b = build(tmp_path, auto_merge_enabled=False)
    b.publish("auto-08", {"a.py": "one\n"})
    monkeypatch.setattr(cli, "load_config", lambda _p: b.config)
    monkeypatch.chdir(b.repo)

    assert cli._cmd_merge_backlog(_args()) == 1
    assert "auto_merge_enabled" in capsys.readouterr().out


def test_the_command_is_wired_into_the_parser():
    args = cli.build_parser().parse_args(["merge-backlog"])
    assert args.func is cli._cmd_merge_backlog


# --- startup ------------------------------------------------------------------


def _run(b, monkeypatch, started, beats=None):
    """`run` with the loop body stubbed and the heartbeat captured. Every
    startup test needs the same wiring, and what each one is really asserting is
    whether `_run_locked` was reached at all — so the stub records that.

    `beats` collects `(status, detail)` for the tests that care what a monitor
    would see afterwards."""
    monkeypatch.setattr(cli, "load_config", lambda _p: b.config)
    monkeypatch.chdir(b.repo)
    monkeypatch.setattr(cli, "_run_locked",
                        lambda args, config: (started.append(True), 0)[1])
    monkeypatch.setattr(
        cli.heartbeat, "publish",
        lambda config, state=None, status="running", detail="": (
            beats.append((status, detail)) if beats is not None else None
        ),
    )
    return cli._cmd_run(argparse.Namespace(config=None, continuous=False, max_steps=None))


def test_run_sweeps_the_backlog_before_starting_the_loop(tmp_path, monkeypatch):
    """The startup half. Nothing reports a stranded branch, so the only moment
    left to look for one is before the loop does anything else."""
    b = build(tmp_path)
    candidate = b.publish("auto-08", {"a.py": "one\n"})
    started = []

    assert _run(b, monkeypatch, started) == 0

    assert started == [True]
    assert contains(b.repo, b.head(), candidate), "swept before the loop ran"
    assert b.origin_base() == b.head()


def test_startup_REPORTS_a_completed_task_it_could_not_judge(tmp_path, monkeypatch,
                                                             capsys):
    """The startup hook is silent only when the backlog is PROVABLY clear, and
    a torn record is the case most likely to slip through that test: the
    outcome is still `nothing_to_do`, so an outcome comparison would return
    early and print nothing. `is_clear` is what stops it — and the operator
    starting a loop is the last person who will ever be told."""
    b = build(tmp_path)
    b.publish("torn-01", {"a.py": "one\n"})
    (b.config.executions_dir / "torn-01.json").write_text("{not json", encoding="utf-8")
    monkeypatch.chdir(b.repo)

    cli._sweep_backlog_on_startup(b.config)

    out = capsys.readouterr().out
    assert "UNJUDGED" in out and "torn-01" in out
    assert "could not be read" in out


def test_a_HELD_sweep_REPORTS_and_still_lets_the_loop_start(tmp_path, monkeypatch,
                                                            capsys):
    """The second half of the hold invariant, and the one that stops it being a
    denial of service: withholding the merges must not withhold the RUN.

    The branches this module integrates have already sat unmerged for days, so a
    startup sweep that refused to let the loop start over one unjudgeable task
    would be a strictly worse failure than the one it is reporting. It reports
    and returns; `_run_locked` is reached exactly as it would be otherwise.
    """
    b = build(tmp_path)
    before = b.head()
    a_sha = b.publish(
        "a-first", {"shared.py": "line one\n"}, published_at="2026-08-06T01:00:00+00:00"
    )
    b_sha = b.publish(
        "b-on-a", {"shared.py": "line one\nline two\n"},
        base=a_sha, published_at="2026-08-06T02:00:00+00:00",
    )
    run_git(b.repo, "push", "-q", "origin", "--delete", "refs/heads/autoloop/a-first")
    started = []

    assert _run(b, monkeypatch, started) == 0

    assert started == [True], "a held sweep must not stop the loop from starting"
    assert b.head() == before, "and must not have merged anything on the way"
    assert not contains(b.repo, b.head(), b_sha)
    out = capsys.readouterr().out
    assert "held" in out
    assert "UNJUDGED" in out and "a-first" in out
    assert "b-on-a" in out, "the operator is told which branch is waiting on it"


def test_a_merge_that_moved_HEAD_and_failed_verification_stops_the_LOOP_starting(
    tmp_path, monkeypatch, capsys
):
    """The post-mutation startup hole. The sweep already stops at this branch,
    and stops for the right reason — `AutoMerger._merge` does not undo a merge
    that failed verification, so nothing further may be stacked on that head.
    But stopping only the SWEEP and then dispatching ordinary roadmap work onto
    the same checkout defeats the entire point of stopping: the next task would
    be cut from a head nobody verified, and its own push would carry the
    unverified merge along with it.

    There is no policy-legal way to undo it (`reset` is off the git whitelist),
    so the only honest response is to refuse to start and say so.
    """
    b = build(tmp_path)
    before = b.head()
    candidate = b.publish("auto-08", {"a.py": "one\n"})
    _merge_for_real_then_dirty_the_tree(b, monkeypatch)
    started, beats = [], []

    rc = _run(b, monkeypatch, started, beats)

    assert started == [], "the loop must not run in a checkout nobody can explain"
    assert rc == 1, "and the refusal has to be visible in the exit code"
    assert [status for status, _detail in beats] == ["parked"], (
        "a monitor must not keep reading a dead run's `running` beat — and "
        "`stopped` is deliberately not an attention status, while nobody chose "
        "this"
    )
    assert "unreconciled" in beats[0][1]
    assert b.head() != before and contains(b.repo, b.head(), candidate), (
        "the merge really is in the checkout — this is not the untouched case"
    )
    assert b.origin_base() == before, "and never reached the remote"
    out = capsys.readouterr().out
    assert "UNRECONCILED" in out
    assert "NOT starting the loop" in out
    assert "the base is exactly as it was" not in out, (
        "the line that would send the operator away is the whole regression"
    )


def test_a_REFUSED_push_at_startup_stops_the_loop_too(tmp_path, monkeypatch, capsys):
    """Same refusal, reached through the outcome that looks safest. `deferred`
    is what a shut gate and a dirty checkout return, and both of those start the
    loop normally two tests below — so a startup guard written against the slug
    would let exactly this one through, with the base sitting on a merge commit
    the remote has never seen."""
    b = build(tmp_path, protected=(BASE,))
    before = b.head()
    b.publish("auto-08", {"a.py": "one\n"})
    started = []

    rc = _run(b, monkeypatch, started)

    assert started == [] and rc == 1
    assert b.head() != before, "merged locally"
    assert b.origin_base() == before, "and unpushed — pushing later work stacks on it"
    out = capsys.readouterr().out
    assert "UNRECONCILED" in out and "the base is exactly as it was" not in out


def test_a_conflict_that_restored_the_base_still_lets_the_loop_start(tmp_path,
                                                                     monkeypatch,
                                                                     capsys):
    """The control, and the reason the guard is a probe rather than "any stop
    blocks". A conflict aborts back to the exact pre-merge head with a clean
    tree; the branch is outstanding, but the checkout is untouched and the loop
    has every business running in it. Blocking here would turn one unmergeable
    branch into a loop that will not start — strictly worse than what it is
    reporting, and the same denial of service the HELD outcome avoids."""
    b = build(tmp_path, seed_files={"shared.py": "the original\n"})
    b.publish("collides", {"shared.py": "the task's version\n"})
    b.publish("never-tried", {"b.py": "two\n"}, published_at="2026-08-06T03:00:00+00:00")
    before = b.commit_on_base({"shared.py": "a different version\n"})
    started = []

    rc = _run(b, monkeypatch, started)

    assert started == [True], "a clean stop must not stop the loop"
    assert rc == 0
    assert b.head() == before and is_clean(b.repo)
    out = capsys.readouterr().out
    assert "STOPPED at" in out and "collides" in out
    assert "the base is exactly as it was" in out, (
        "and here the claim is true, so it is made"
    )
    assert "UNRECONCILED" not in out and "NOT starting the loop" not in out


def test_a_dirty_checkout_defers_and_still_lets_the_loop_start(tmp_path, monkeypatch,
                                                               capsys):
    """The other `deferred` the guard must not confuse with a refused push. The
    checkout was ALREADY dirty when the attempt found it and is dirty in exactly
    the same way afterwards — the sweep declined to merge into it and changed
    nothing, which is why the comparison is against the pre-ATTEMPT observation
    rather than against "is the tree clean"."""
    b = build(tmp_path)
    before = b.head()
    b.publish("first", {"a.py": "one\n"})
    (b.repo / "README.md").write_text("edited by hand\n", encoding="utf-8")
    started = []

    rc = _run(b, monkeypatch, started)

    assert started == [True] and rc == 0
    assert b.head() == before
    assert (b.repo / "README.md").read_text() == "edited by hand\n"
    assert "UNRECONCILED" not in capsys.readouterr().out


def test_startup_stays_quiet_when_the_backlog_really_is_clear(tmp_path, monkeypatch,
                                                              capsys):
    """The control that keeps the test above from passing vacuously — and the
    reason `is_clear` has to be provable rather than merely non-alarming. An
    operator who sees a report every single startup stops reading them."""
    b = build(tmp_path)
    candidate = b.publish("done-01", {"a.py": "one\n"})
    run_git(b.repo, "merge", "--no-ff", "--no-edit", "-m", "merged earlier", candidate)
    monkeypatch.chdir(b.repo)

    cli._sweep_backlog_on_startup(b.config)

    assert capsys.readouterr().out == ""


def test_a_sweep_that_blows_up_never_stops_the_run(tmp_path, monkeypatch):
    """It runs before the loop has done anything. An integration problem that
    prevented the loop from starting would be a strictly worse failure than the
    unmerged branch it was trying to fix — so a crash that touched nothing is
    still reported and still lets the run go ahead."""
    b = build(tmp_path)
    monkeypatch.chdir(b.repo)
    before = b.head()
    monkeypatch.setattr(
        merge_sweep, "sweep_backlog",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = merge_sweep.sweep_on_startup(b.config)

    assert result.outcome == merge_sweep.FAILED
    assert "boom" in b.entries("merge_sweep_error")[0]["data"]["error"]
    assert result.is_reconciled is True, "nothing was mutated, so nothing is withheld"
    assert result.base_before == before == result.base_after


def test_a_crash_that_left_the_base_MOVED_is_not_waved_through(tmp_path, monkeypatch):
    """The other side of the guard above, and why the crash path is answered by
    a comparison rather than by the outcome slug.

    `sweep()` returns a result for every path it decides, so an exception
    reaching `sweep_on_startup` came either from construction — before anything
    could move — or from the transcript write that follows a stop, which happens
    after one. `FAILED` cannot tell those apart, so it is not asked to: HEAD and
    the tree are observed on both sides of the call.
    """
    b = build(tmp_path)
    monkeypatch.chdir(b.repo)
    before = b.head()

    def merge_then_die(*_a, **_k):
        run_git(b.repo, "commit", "-q", "--allow-empty", "-m", "half-done integration")
        raise RuntimeError("boom, after the head moved")

    monkeypatch.setattr(merge_sweep, "sweep_backlog", merge_then_die)

    result = merge_sweep.sweep_on_startup(b.config)

    assert result.outcome == merge_sweep.FAILED
    assert result.is_reconciled is False
    assert "HEAD moved from" in result.unreconciled
    assert result.base_before == before
    assert result.base_after == b.head()
    assert result.base_after != before, "the crash really did leave the head moved"


# --- ordering helpers ---------------------------------------------------------


def test_a_stamp_reads_the_same_with_or_without_a_trailing_z():
    """`state.utcnow_iso` writes `+00:00`, but labels elsewhere in this package
    rewrite it to `Z`. Both are the same instant, and an absent stamp is None —
    which is what puts a record in the older group at all."""
    assert merge_sweep._parse_iso("") is None
    assert merge_sweep._parse_iso("2026-08-14T00:00:00Z") == (
        merge_sweep._parse_iso("2026-08-14T00:00:00+00:00")
    )


def test_an_unparseable_stamp_does_not_crash_the_sweep():
    assert merge_sweep._parse_iso("not a date") is None
    assert merge_sweep._ident_timestamp("Name <e@x> 1754500000 +0000") == 1754500000.0
    assert merge_sweep._ident_timestamp("nonsense") == 0.0
    assert merge_sweep._ident_timestamp("") == 0.0


def test_the_outcome_slugs_match_the_transcript_types():
    """The log grep and the test assertion must name the same thing."""
    assert merge_sweep.SWEPT == "swept"
    assert merge_sweep.DEFERRED == "deferred"
    assert merge_sweep.HELD == "held"
    assert merge_sweep.STOPPED == "stopped"


def test_the_retirement_stamp_is_read_off_a_label_retire_execution_really_wrote(tmp_path):
    """Generation ordering reads the archive FILENAME, so it is coupled to how
    `retire_execution` builds a label. Pinned against the real function rather
    than a hand-written string, so a change to that format fails here instead of
    quietly making every multi-generation archive unorderable.

    `reason` is the awkward real one — it contains `-`, exactly like the task id
    on the other side of it — which is why the stamp is read off the END of the
    stem and not by stripping a prefix."""
    store = TaskExecutionStore(tmp_path / "executions")
    store.directory.mkdir(parents=True, exist_ok=True)
    store.save(
        TaskExecution(
            task_id="stamp-01",
            task_branch="autoloop/stamp-01",
            worktree_path="",
            task_base_sha="a" * 40,
            candidate_sha="b" * 40,
        )
    )
    retired = retire_execution("stamp-01", store, None, reason="published-abc123def456")

    stamp = merge_sweep._retirement_stamp(retired.record_path)
    assert stamp and stamp == retired.label.rsplit("-", 1)[-1]
    assert len(stamp) == 16 and stamp[8] == "T" and stamp.endswith("Z")


def test_a_label_with_no_retirement_stamp_reads_as_unorderable():
    """Both halves of the shape check, since the answer decides between "judge
    the newest" and "refuse to guess"."""
    assert merge_sweep._retirement_stamp(Path("t-published-20260810T000000Z.json"))
    assert merge_sweep._retirement_stamp(Path("t-released-by-operator.json")) == ""
    assert merge_sweep._retirement_stamp(Path("t-published.json")) == ""
    assert merge_sweep._retirement_stamp(Path("t-20260810T00000Z.json")) == "", "too short"
    assert merge_sweep._retirement_stamp(Path("t-2026081OT000000Z.json")) == "", "not digits"


def test_a_publication_that_stopped_being_confirmed_is_not_an_auto_merge_slug():
    """`UNCONFIRMED` is minted here, not returned by `auto_merge`: nothing
    reached `AutoMerger.attempt`, so borrowing one of its outcomes would report
    a decision the merge machinery never made. It must also stay OUT of the
    continue-list, which is what makes it stop the sweep."""
    assert merge_sweep.UNCONFIRMED == "publication_unconfirmed"
    assert merge_sweep.UNCONFIRMED not in vars(auto_merge).values()
    assert merge_sweep.UNCONFIRMED not in merge_sweep._CONTINUE_ON
