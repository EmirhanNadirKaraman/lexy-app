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

from autoloop import auto_merge, cli, merge_sweep
from autoloop.auto_merge import MergeDeferralStore
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.errors import GitCommandError
from autoloop.git_gateway import GitGateway
from autoloop.merge_sweep import BacklogSweeper
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.transcript import TranscriptLogger
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.worktask import TaskExecution, TaskExecutionStore

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
                base=None, complete=True):
        """One completed task whose reviewed candidate is on its own branch on
        origin and is NOT in the base — the exact state the 2026-08-06 seven
        were found in."""
        base_sha = base or self.head()
        branch = f"autoloop/{task_id}"
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
                    complete=complete)
        return sha

    def record(self, task_id, candidate, base_sha, *, dest_ref, published_at="",
               remote="origin", complete=True):
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
        self.registry.add_many([Task(id=task_id, title=f"Title {task_id}", description="d")])
        if complete:
            self.registry.mark_completed(task_id)
        self.task_store.save(self.registry)

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


def build(tmp_path, *, auto_merge_enabled=True, seed_files=None):
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

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(auto_merge_enabled=auto_merge_enabled),
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
    assert b.sweep_entries() == [], "silently means silently"
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

    assert [t for t, _why in result.skipped] == ["gone-01"]
    assert result.is_clear is False, "a branch it could not judge is not a clear backlog"
    assert b.head() == before
    assert not contains(b.repo, b.head(), candidate)
    assert b.entries("merge_sweep_skipped")[0]["data"]["task_id"] == "gone-01"


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
    assert [t for t, _why in result.skipped] == ["auto-08"]
    assert "could not verify" in result.skipped[0][1]
    assert result.merged == []
    assert b.head() == before, "nothing may be merged on evidence nobody could read"
    assert not contains(b.repo, b.head(), candidate)


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


def test_run_sweeps_the_backlog_before_starting_the_loop(tmp_path, monkeypatch):
    """The startup half. Nothing reports a stranded branch, so the only moment
    left to look for one is before the loop does anything else."""
    b = build(tmp_path)
    candidate = b.publish("auto-08", {"a.py": "one\n"})
    monkeypatch.setattr(cli, "load_config", lambda _p: b.config)
    monkeypatch.chdir(b.repo)
    monkeypatch.setattr(cli, "_run_locked", lambda args, config: 0)
    monkeypatch.setattr(cli.heartbeat, "publish", lambda *a, **k: None)

    assert cli._cmd_run(argparse.Namespace(
        config=None, continuous=False, max_steps=None
    )) == 0

    assert contains(b.repo, b.head(), candidate), "swept before the loop ran"
    assert b.origin_base() == b.head()


def test_a_sweep_that_blows_up_never_stops_the_run(tmp_path, monkeypatch):
    """It runs before the loop has done anything. An integration problem that
    prevented the loop from starting would be a strictly worse failure than the
    unmerged branch it was trying to fix."""
    b = build(tmp_path)
    monkeypatch.setattr(
        merge_sweep, "sweep_backlog",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = merge_sweep.sweep_on_startup(b.config)

    assert result.outcome == merge_sweep.FAILED
    assert "boom" in b.entries("merge_sweep_error")[0]["data"]["error"]


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
    assert merge_sweep.STOPPED == "stopped"
