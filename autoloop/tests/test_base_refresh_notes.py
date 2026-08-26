"""notes-04: the change-note resolver runs in BOTH merge directions.

The loop merges these documentation trackers two ways, and until now only one
of them consulted `note_merge`:

  * a task branch INTO the base branch — `auto_merge.AutoMerger._merge`, wired
    to the resolver since docs-01 and pinned by `test_docs_merge.py`;
  * the base branch's head INTO a task branch —
    `orchestrator._carry_reviewed_candidate_past`, which refreshes a REVIEWED
    candidate's recorded base after the head moved under it. This had no note
    handling at all, so any change-note collision refused the whole operation.

Measured 2026-08-23, hours after notes-03 widened WHICH files may be combined:
`blk-quota-01-002` parked `task_base_behind_head` because the head could not be
merged into quota-01's branch — it conflicted at `docs/SUMMARY.md` and
`docs/TESTS.md`. Both are in `NOTE_TRACKERS`. Both are exactly the append-at-
the-end shape the resolver exists to combine. It was never consulted, and an
11-file reviewed candidate that had passed validation was abandoned.

Every task appends a change note by construction, so two tasks in flight across
one merge collide in the trackers by DEFAULT rather than by accident — which is
why `task_base_behind_head` is this repository's most common blocker code.

WHAT IS PINNED HERE, in the order the claim states it:
  * a head conflicting only in change-note sections merges into the task
    branch, and BOTH sides' notes survive;
  * the same merge conflicting in a tracker's PROSE still parks;
  * a merge conflicting in any source file still parks, with the existing
    message and the existing blocker code;
  * a reviewed candidate is never rebased, rebuilt or quarantined past a
    conflict — the operator still decides;
  * and the ORDERING, which is the half that is easy to get wrong: the
    incoming head's note lines must lead so the refreshed branch can still be
    merged back OUT afterwards (`test_a_refreshed_task_branch_still_merges_
    back_out_through_auto_merge`). Getting that backwards trades one blocker
    for a branch that can never merge again — the ctx-01 shape, repaired by
    hand on 2026-08-21.

Real git throughout, real worker repositories built the way production builds
them (`WorkerRepoManager.create`), self-contained helpers — this package's
convention, see `test_postcommit_primitives.py` for why they are duplicated
rather than imported. The base branch is `work`, matching `test_docs_merge.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoloop import note_merge
from autoloop.auto_merge import AutoMerger, MergeDeferralStore
from autoloop.git_gateway import GitGateway
from autoloop.note_merge import NOTES_MARKER
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import Task
from autoloop.worker_env import WorkerRepoManager, worker_env
from autoloop.worktask import TaskExecution, TaskExecutionStore

TASK = Task(id="t1", title="T", description="d")

#: This file's own literal of the resolver's scope, deliberately a second copy
#: (same reasoning as `test_docs_merge.py`'s): every test here is a statement
#: about THESE paths, and `test_the_refresh_covers_exactly_the_declared_
#: trackers` is where the two are required to agree.
TRACKERS = (
    "docs/COMMON_ERRORS.md",
    "docs/SECURITY.md",
    "docs/SUMMARY.md",
    "docs/TESTS.md",
)

MARKER_LINE = f"{NOTES_MARKER} append below, one line per note, at the END. -->"
PROSE_ROW = "| `main.py` | FastAPI app. |"
SEED_NOTE = "| 2026-08-17 | seed-00 | the note that was already there |"


def git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


def try_git(cwd, *args) -> subprocess.CompletedProcess:
    """Unchecked — used where a NON-zero exit is the thing being staged."""
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def contains(cwd, tip, sha) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", sha, tip], cwd=str(cwd), capture_output=True
    ).returncode == 0


def seed(title: str) -> str:
    """The shape every real tracker has: prose, then ONE marker, then a ledger
    whose last line is a note row."""
    return (
        f"# {title}\n\n| Path | Purpose |\n|---|---|\n{PROSE_ROW}\n\n"
        f"## Change notes\n\n{MARKER_LINE}\n\n| Date | Task | Note |\n|---|---|---|\n"
        f"{SEED_NOTE}\n"
    )


SEEDS = {rel: seed(rel.rsplit("/", 1)[-1]) for rel in TRACKERS}


def note_line(who: str) -> str:
    return f"| 2026-08-23 | {who} | what {who} changed |\n"


def write(root, rel: str, text: str) -> None:
    target = Path(root) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def read(root, rel: str) -> str:
    return (Path(root) / rel).read_text(encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    """The primary checkout: four seeded trackers, one source file, one doc
    that is deliberately NOT a tracker."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "work")
    git(root, "config", "user.email", "t@e.com")
    git(root, "config", "user.name", "T")
    git(root, "config", "commit.gpgsign", "false")
    for rel, text in SEEDS.items():
        write(root, rel, text)
    write(root, "autoloop/thing.py", "TIMEOUT = 30\n")
    write(root, "docs/TODO.md", "# TODO\n\n- one\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "first")
    return root


class _FakeWorkerRepos:
    """Records what a re-base would have asked of it. Every test below asserts
    both lists stay EMPTY: a reviewed candidate is never rebuilt."""

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


def _orch(repo, tmp_path, execution, review_round=1):
    """An Orchestrator with only what `_rebase_execution_if_stale` touches —
    same construction as `test_rebase_stale_base.py`'s."""
    from autoloop.orchestrator import Orchestrator

    orch = Orchestrator.__new__(Orchestrator)
    orch._policy = PolicyEngine(PolicyConfig())
    orch._git = GitGateway(repo, orch._policy)
    orch._worker_repos = _FakeWorkerRepos(tmp_path / "workers")
    # Same reason as `test_rebase_stale_base.py`'s fixture: no loop-owned
    # observed checkout, so the carry-forward below fetches the head from the
    # primary checkout exactly as it did before esc-02.
    orch._observed = None
    orch._observed_git = None
    orch._observed_synced_sha = ""
    orch._merge_deferrals = MergeDeferralStore(tmp_path / "deferrals")
    orch._execution_store = TaskExecutionStore(tmp_path / "executions")
    orch._logged: list = []
    orch._log = lambda event, **kw: orch._logged.append((event, kw))
    orch._parked: list = []
    orch._to_needs_user = lambda msg, **kw: orch._parked.append((msg, kw))
    execution.review_round = review_round
    orch._execution_store.save(execution)
    return orch


def _worker(repo, tmp_path, base, files, *, task_id="t1"):
    """A real worker repo carrying one committed candidate. Returns
    `(WorkerRepo, candidate_sha)`.

    `git init` + a one-time local fetch, exactly as production does — that
    separateness is what makes the fetch half of the merge load-bearing.
    """
    manager = WorkerRepoManager(tmp_path / "workers", tmp_path / "worker-hooks")
    worker = manager.create(task_id, repo, base)
    git(worker.path, "config", "user.email", "worker@example.com")
    git(worker.path, "config", "user.name", "Worker")
    git(worker.path, "config", "commit.gpgsign", "false")
    for rel, text in files.items():
        write(worker.path, rel, text)
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


def _move_head(repo, files):
    for rel, text in files.items():
        write(repo, rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "mainline shipped something")
    return git(repo, "rev-parse", "HEAD")


def _recording(who, *, extra=None):
    """The edit EVERY task makes: one new line at the end of EVERY tracker."""
    files = {rel: SEEDS[rel] + note_line(who) for rel in TRACKERS}
    files.update(extra or {})
    return files


def _refresh(repo, tmp_path, worker, candidate, old_base, head, **kw):
    """Run the production dispatch step and return `(orch, execution, result)`."""
    execution = _reviewed(worker, candidate, old_base, **kw)
    orch = _orch(repo, tmp_path, execution, review_round=kw.get("review_round", 1))
    result = orch._rebase_execution_if_stale(execution, TASK)
    return orch, execution, result


def assert_nothing_was_rebased(orch, worker, execution, old_base, candidate):
    """The guard this task must not remove: a reviewed candidate is never
    rebased, rebuilt or quarantined, and the record is not re-pointed."""
    assert orch._worker_repos.created == [], "no worker may be rebuilt"
    assert orch._worker_repos.quarantined == [], "and none quarantined"
    assert execution.task_base_sha == old_base, "nothing was re-pointed"
    assert TaskExecutionStore(orch._execution_store.directory).load("t1").task_base_sha == old_base
    assert git(worker.path, "rev-parse", "HEAD") == candidate, "the branch tip is unmoved"
    assert git(worker.path, "status", "--porcelain") == "", "and the merge was aborted"


def assert_parked_the_same_way(orch, old_base, head):
    """The park is unchanged: same code, same three operator choices."""
    assert orch._parked, "it must park"
    message, kw = orch._parked[0]
    assert kw["code"] == "task_base_behind_head", "the same code, so the same recovery"
    assert kw["kind"] == "task_fatal"
    assert head[:12] in kw["detail"] and old_base[:12] in kw["detail"]
    assert "Either publish or abandon that candidate" in message
    assert "archive" in message
    return message


# --- the claim ----------------------------------------------------------------


def test_a_head_conflicting_only_in_change_notes_is_merged_into_the_task_branch(
    repo, tmp_path
):
    """THE provable claim. Two tasks in flight across one merge: each appended
    its own note to every tracker, so the head and the task branch collide in
    all four. The refresh succeeds and BOTH sides' notes survive."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))

    orch, _execution, result = _refresh(
        repo, tmp_path, worker, candidate, old_base, head,
        review_round=2, attempt_count=3, fault_attempt_count=1,
    )

    assert result is not None, "the dispatch must CONTINUE, not park"
    assert orch._parked == []
    assert result.task_base_sha == head
    # Nothing a moving base may touch was touched.
    assert result.candidate_sha == candidate
    assert result.review_round == 2 and result.attempt_count == 3
    assert result.fault_attempt_count == 1
    reloaded = TaskExecutionStore(tmp_path / "executions").load("t1")
    assert reloaded.task_base_sha == head and reloaded.candidate_sha == candidate

    tip = git(worker.path, "rev-parse", "HEAD")
    assert contains(worker.path, tip, candidate), "the reviewed object is still reachable"
    assert git(worker.path, "cat-file", "-t", candidate) == "commit"
    assert contains(worker.path, tip, head), "with the new head integrated"
    assert git(worker.path, "status", "--porcelain") == "", "and the worker is clean"
    assert orch._worker_repos.created == [] and orch._worker_repos.quarantined == []

    for rel in TRACKERS:
        text = read(worker.path, rel)
        assert "<<<<<<<" not in text, rel
        assert text.count(note_line("task-b")) == 1, rel
        assert text.count(note_line("mainline")) == 1, rel
        assert text.count(SEED_NOTE) == 1, rel
        assert text.count(MARKER_LINE) == 1, rel
        assert text.count(PROSE_ROW) == 1, rel


def test_a_refreshed_task_branch_still_merges_back_out_through_auto_merge(repo, tmp_path):
    """The half that is easy to get wrong, and the reason `lead` exists.

    `resolve_note_append` requires each side's section to hold the merge base's
    section as a literal PREFIX. After a refresh the incoming head IS the task's
    new base, so the head's note lines must come FIRST and the task's own must
    stay last — otherwise the branch's section no longer starts with its base's
    and EVERY later merge of that tracker refuses. That is the ctx-01 shape,
    which had to be repaired by hand on 2026-08-21.

    So this goes the whole way round: refresh the base, let mainline record one
    more note, then merge the task branch back out through the REAL
    `AutoMerger._resolve_note_conflicts` and require all three notes to survive.
    """
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))

    orch, _execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)
    assert result is not None and orch._parked == []
    tip = git(worker.path, "rev-parse", "HEAD")

    # Mainline records one more note, so the merge back out really conflicts.
    later = {rel: SEEDS[rel] + note_line("mainline") + note_line("mainline-2")
             for rel in TRACKERS}
    _move_head(repo, later)

    # ... and now the ORIGINAL direction, through the code that already ships.
    git(repo, "fetch", "-q", str(worker.path), tip)
    merged = try_git(repo, "merge", "--no-ff", "--no-edit", "-m", "merge task t1", tip)
    assert merged.returncode != 0, "the fixture must actually conflict, or this proves nothing"

    primary = GitGateway(repo, PolicyEngine(PolicyConfig()))
    merger = AutoMerger.__new__(AutoMerger)
    merger._git = primary
    merger._log = lambda event, **kw: None
    conflicts = primary.conflicted_paths()
    assert set(conflicts) == set(TRACKERS), conflicts

    assert merger._resolve_note_conflicts("t1", tip, conflicts, "merge task t1") is True, (
        "the refreshed branch must still be mergeable — if this fails, the "
        "refresh ordered the notes so that the branch can never merge again"
    )
    for rel in TRACKERS:
        text = read(repo, rel)
        assert "<<<<<<<" not in text, rel
        for who in ("task-b", "mainline", "mainline-2"):
            assert text.count(note_line(who)) == 1, f"{who} in {rel}"
        assert text.count(SEED_NOTE) == 1, rel


def test_the_incoming_notes_lead_and_the_tasks_own_notes_stay_last(repo, tmp_path):
    """The ordering stated directly, so a regression names itself rather than
    surfacing as a mysterious refusal one merge later."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))

    _refresh(repo, tmp_path, worker, candidate, old_base, head)

    for rel in TRACKERS:
        text = read(worker.path, rel)
        assert text.index(note_line("mainline")) < text.index(note_line("task-b")), rel
        assert text.endswith(note_line("task-b")), rel
        # And stated as the invariant the next merge actually checks: the
        # branch's section is the incoming base's section plus its own lines.
        assert text == read(repo, rel) + note_line("task-b"), rel


def test_the_wrong_ordering_really_would_break_the_next_merge():
    """The counter-case, at the unit level. Without this the test above could
    pass for reasons unrelated to `lead`, and the bound would be unproven.

    A branch whose section is `base + own + incoming` does NOT start with the
    incoming base's section, so the resolver refuses — forever."""
    base = seed("Tracker")
    incoming = base + note_line("mainline")

    wrong_way = base + note_line("task-b") + note_line("mainline")
    right_way = base + note_line("mainline") + note_line("task-b")

    later = incoming + note_line("mainline-2")
    assert note_merge.resolve_note_append(incoming, later, wrong_way, incoming) is None
    assert note_merge.resolve_note_append(incoming, later, right_way, incoming) is not None


# --- what must still park -----------------------------------------------------


@pytest.mark.parametrize("rel", TRACKERS)
def test_a_conflict_in_tracker_prose_still_parks(repo, tmp_path, rel):
    """A conflict ABOVE the marker is ordinary documentation disagreeing and
    needs a human. Run per tracker: a passing result on one says nothing about
    the file added yesterday."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(
        repo, tmp_path, old_base,
        {rel: SEEDS[rel].replace(PROSE_ROW, "| `main.py` | the task's words. |")},
    )
    head = _move_head(
        repo, {rel: SEEDS[rel].replace(PROSE_ROW, "| `main.py` | mainline's words. |")}
    )

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    message = assert_parked_the_same_way(orch, old_base, head)
    assert f"conflicts at {rel}" in message
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    assert not [e for e, _ in orch._logged if e == "execution_base_notes_resolved"]
    refused = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_refused"]
    assert refused and refused[0]["reason"], "the refusal must explain itself"


def test_a_conflict_in_a_source_file_still_parks_with_the_existing_message(repo, tmp_path):
    """The bound on the whole change: only append-only change notes are in
    scope. A genuine source conflict parks with the message it always had."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, {"autoloop/thing.py": "TIMEOUT = 60\n"})
    head = _move_head(repo, {"autoloop/thing.py": "TIMEOUT = 90\n"})

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    message = assert_parked_the_same_way(orch, old_base, head)
    assert "conflicts at autoloop/thing.py" in message, "it names what actually clashed"
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    assert read(worker.path, "autoloop/thing.py") == "TIMEOUT = 60\n", "no marker was left"
    assert [e for e, _ in orch._logged if e == "execution_base_carry_forward_refused"]


def test_a_source_conflict_alongside_resolvable_trackers_resolves_nothing(repo, tmp_path):
    """The case the trackers cannot buy their way out of — and the shape that
    actually occurred. All four trackers are clean pairs of appends that WOULD
    combine; one source file genuinely conflicts. One conflicted path outside
    the list refuses the WHOLE merge, and no tracker is written."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(
        repo, tmp_path, old_base, _recording("task-b", extra={"autoloop/thing.py": "TIMEOUT = 60\n"})
    )
    head = _move_head(
        repo, _recording("mainline", extra={"autoloop/thing.py": "TIMEOUT = 90\n"})
    )

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    assert_parked_the_same_way(orch, old_base, head)
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    for rel in TRACKERS:
        text = read(worker.path, rel)
        assert note_line("mainline") not in text, (
            f"a resolvable {rel} must never reach disk when a source file refused"
        )
        assert text.count(note_line("task-b")) == 1, rel
    refused = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_refused"]
    assert refused, "the resolver must say why it declined"
    assert "outside" in refused[0]["reason"]
    assert "autoloop/thing.py" in refused[0]["reason"]
    assert refused[0]["conflicted_files"] == sorted(("autoloop/thing.py", *TRACKERS))


def test_a_documentation_file_outside_the_trackers_still_parks(repo, tmp_path):
    """What "narrow" buys: the same append-at-EOF edit that combines in the
    trackers still parks in a doc nobody granted the resolver. `docs/` is not a
    prefix and never becomes one."""
    old_base = git(repo, "rev-parse", "HEAD")
    todo = "# TODO\n\n- one\n"
    worker, candidate = _worker(repo, tmp_path, old_base, {"docs/TODO.md": todo + "- from the task\n"})
    head = _move_head(repo, {"docs/TODO.md": todo + "- from mainline\n"})

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    assert_parked_the_same_way(orch, old_base, head)
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    refused = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_refused"]
    assert refused and refused[0]["conflicted_files"] == ["docs/TODO.md"]
    assert "outside" in refused[0]["reason"]


def test_an_edited_existing_note_line_still_parks(repo, tmp_path):
    """A note already in the ledger is not append-only content — it is a claim
    somebody made. Rewriting it is a real content conflict, in this direction
    exactly as in the other."""
    old_base = git(repo, "rev-parse", "HEAD")
    rel = "docs/SUMMARY.md"
    worker, candidate = _worker(
        repo, tmp_path, old_base,
        {rel: SEEDS[rel].replace(SEED_NOTE, "| 2026-08-17 | seed-00 | the task's rewording |")},
    )
    head = _move_head(
        repo,
        {rel: SEEDS[rel].replace(SEED_NOTE, "| 2026-08-17 | seed-00 | mainline's rewording |")},
    )

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    assert_parked_the_same_way(orch, old_base, head)
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    refused = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_refused"]
    assert refused and "not two branches appending change notes" in refused[0]["reason"]


@pytest.mark.parametrize("rel", TRACKERS)
def test_a_tracker_without_the_marker_is_refused_rather_than_combined(repo, tmp_path, rel):
    """The precondition behind the list, proven in this direction too. A
    granted path whose file has no append-only section gives the resolver no
    boundary between prose and ledger — so it refuses, and the failure mode is
    a park, never a combined paragraph."""
    unmarked = "# Tracker\n\nJust prose, no append-only section at all.\n"
    write(repo, rel, unmarked)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a tracker with no section")
    old_base = git(repo, "rev-parse", "HEAD")

    worker, candidate = _worker(repo, tmp_path, old_base, {rel: unmarked + "a line from the task\n"})
    head = _move_head(repo, {rel: unmarked + "a line from mainline\n"})

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    assert_parked_the_same_way(orch, old_base, head)
    assert_nothing_was_rebased(orch, worker, execution, old_base, candidate)
    assert read(worker.path, rel) == unmarked + "a line from the task\n"


# --- the reviewed-candidate guard, unchanged ----------------------------------


def test_a_dirty_worker_is_not_merged_over_even_when_only_notes_conflict(repo, tmp_path):
    """Residue in a worker is an interrupted round's work or a failed round's
    evidence. The precondition runs BEFORE any merge is attempted, so making
    note conflicts resolvable must not let a resolvable one slip past it."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))
    (worker.path / "half-written.txt").write_text("mid-round\n", encoding="utf-8")

    orch, execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    message = assert_parked_the_same_way(orch, old_base, head)
    assert "uncommitted changes" in message
    assert execution.task_base_sha == old_base
    assert git(worker.path, "rev-parse", "HEAD") == candidate
    assert (worker.path / "half-written.txt").read_text() == "mid-round\n"
    assert orch._worker_repos.created == [] and orch._worker_repos.quarantined == []
    assert not [e for e, _ in orch._logged if e.startswith("execution_base_notes_")]


def test_a_branch_tip_that_lost_the_candidate_is_not_merged_into(repo, tmp_path):
    """The approval binding stays a CHECKED fact. A resolvable note conflict is
    no reason to skip it."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))
    git(worker.path, "reset", "-q", "--hard", old_base)

    orch, _execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is None
    message = assert_parked_the_same_way(orch, old_base, head)
    assert "does not contain the reviewed candidate" in message
    assert git(worker.path, "rev-parse", "HEAD") == old_base, "and nothing was merged"
    assert not [e for e, _ in orch._logged if e.startswith("execution_base_notes_")]


def test_the_resolution_is_named_in_the_transcript(repo, tmp_path):
    """A merge the loop resolved without a human is exactly the thing an
    operator must be able to find afterwards."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))

    orch, _execution, result = _refresh(repo, tmp_path, worker, candidate, old_base, head)

    assert result is not None
    resolved = [kw["data"] for e, kw in orch._logged if e == "execution_base_notes_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["paths"] == list(TRACKERS)
    assert resolved[0]["task_id"] == "t1"
    assert resolved[0]["head"] == head and resolved[0]["candidate_sha"] == candidate
    assert not [e for e, _ in orch._logged if e == "execution_base_notes_refused"]
    # And the automatic resolution is visible in git, not only in the log.
    assert "combined automatically" in git(worker.path, "log", "-1", "--format=%B")


def test_the_refresh_covers_exactly_the_declared_trackers():
    """This file's literal and the resolver's list must agree, or every test
    above is quietly scoped to something else."""
    assert note_merge.NOTE_TRACKERS == frozenset(TRACKERS)
    for excluded in ("CLAUDE.md", "docs/SCHEMA.md", "docs/", "docs/TODO.md"):
        assert excluded not in note_merge.NOTE_TRACKERS


# --- the direction that already worked, and the hook's own guards -------------


def test_the_task_to_mainline_direction_is_unchanged():
    """`lead` defaults to the order that shipped, so every existing caller —
    `auto_merge`, and `test_docs_merge.py`'s positional calls — behaves exactly
    as before."""
    base = seed("Tracker")
    ours = base + note_line("mainline")
    theirs = base + note_line("task-b")

    assert note_merge.resolve_note_append(base, ours, theirs, base) == (
        base + note_line("mainline") + note_line("task-b")
    )
    assert note_merge.resolve_note_append(base, ours, theirs, base, lead=note_merge.OURS_FIRST) == (
        note_merge.resolve_note_append(base, ours, theirs, base)
    )


def test_an_unknown_lead_is_refused_rather_than_defaulted():
    """Silently falling back to `OURS_FIRST` in the refresh direction is the
    unmergeable-branch bug arriving without a word, so a typo raises."""
    base = seed("Tracker")
    with pytest.raises(ValueError):
        note_merge.resolve_note_append(base, base, base, base, lead="whatever")
    with pytest.raises(ValueError):
        note_merge.combine_conflicted_notes(None, ["docs/TESTS.md"], "m", lead="whatever")


def test_no_conflicted_path_is_refused_rather_than_read_as_resolved(tmp_path):
    """An unreadable `git status` reports NO conflicted path. Treating that as
    "everything resolved" is the fail-open shape: the resolver would conclude a
    merge it never looked at."""
    outcome = note_merge.combine_conflicted_notes(None, [], "m")
    assert outcome.resolved is False
    assert outcome.refusal and outcome.paths == ()


def _plain_gateway(path):
    """A gateway rooted at `path` under the SCRUBBED worker environment, the
    way production builds one. Not decoration: without it these subprocesses
    resolve the developer's ambient git config, and an `insteadOf` rule or a
    signing requirement in it would decide the result instead of the code."""
    return GitGateway(path, PolicyEngine(PolicyConfig()), env=worker_env())


def _plain_pair(tmp_path, name):
    """Two repos where merging the second's commit into the first conflicts in
    a source file — the gateway-level fixture, no orchestrator involved."""
    src = tmp_path / f"{name}-src"
    src.mkdir()
    git(src, "init", "-q", "-b", "work")
    git(src, "config", "user.email", "t@e.com")
    git(src, "config", "user.name", "T")
    git(src, "config", "commit.gpgsign", "false")
    write(src, "f.txt", "one\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "first")
    base = git(src, "rev-parse", "HEAD")

    dst = tmp_path / f"{name}-dst"
    subprocess.run(["git", "clone", "-q", str(src), str(dst)], check=True)
    git(dst, "config", "user.email", "t@e.com")
    git(dst, "config", "user.name", "T")
    git(dst, "config", "commit.gpgsign", "false")
    write(dst, "f.txt", "the local line\n")
    git(dst, "add", "-A")
    git(dst, "commit", "-qm", "local")
    local_tip = git(dst, "rev-parse", "HEAD")

    write(src, "f.txt", "the incoming line\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "incoming")
    return src, dst, base, local_tip, git(src, "rev-parse", "HEAD")


def test_a_resolver_that_claims_success_without_committing_is_not_believed(tmp_path):
    """A hook returning True is a CLAIM. The gateway applies the same
    discipline to it that it applies to `git merge` returning 0 — and a hook
    that concluded nothing leaves the merge exactly where git did, so the
    ordinary abort still runs and the tree comes back clean."""
    src, dst, _base, local_tip, incoming = _plain_pair(tmp_path, "liar")
    gw = _plain_gateway(dst)

    attempt = gw.merge_foreign_commit(
        str(src), incoming, "merge", resolve_conflicts=lambda g, c: True
    )

    assert attempt.merged is False, "an unearned True must not become a merge"
    assert attempt.conflicted_paths == ("f.txt",)
    assert attempt.restored is True
    assert git(dst, "rev-parse", "HEAD") == local_tip, "the branch is unmoved"
    assert git(dst, "status", "--porcelain") == ""
    assert read(dst, "f.txt") == "the local line\n"


def test_a_resolution_that_committed_the_wrong_thing_is_reported_as_a_failure(tmp_path):
    """The other half of "a True is a CLAIM": a hook that DID commit, but
    committed something that does not contain the commit being merged in.

    Only a resolver bug can reach this, which is exactly why it is driven here
    rather than left as an unexercised verification branch. Two things are
    pinned: it is not accepted, and it reports NO conflicted paths — the paths
    conflicted, but their conflict is not what went wrong, and naming them would
    route the caller's park at the path list and hide `error` entirely.
    """
    src, dst, _base, local_tip, incoming = _plain_pair(tmp_path, "wrongcommit")
    gw = _plain_gateway(dst)

    def abort_and_commit_something_else(g, c):
        git(dst, "merge", "--abort")
        write(dst, "unrelated.txt", "not the merge at all\n")
        git(dst, "add", "-A")
        git(dst, "commit", "-qm", "an unrelated commit")
        return True

    attempt = gw.merge_foreign_commit(
        str(src), incoming, "merge", resolve_conflicts=abort_and_commit_something_else
    )

    assert attempt.merged is False, "an unverifiable resolution is not a merge"
    assert attempt.conflicted_paths == (), "this failure was not a content conflict"
    assert "does not contain the merged commit" in attempt.error
    assert attempt.restored is True
    assert contains(dst, git(dst, "rev-parse", "HEAD"), local_tip), "nothing was discarded"
    assert not contains(dst, git(dst, "rev-parse", "HEAD"), incoming)


def test_an_unverifiable_resolution_parks_naming_the_real_condition(repo, tmp_path):
    """The same failure through the production dispatch, because the routing is
    the point. `_carry_reviewed_candidate_past` prefers the conflicted-path list
    when there is one, so an unverifiable resolution reported WITH paths would
    park as "it conflicts at docs/SUMMARY.md" and never show the operator that
    the loop wrote a merge commit it could not vouch for."""
    old_base = git(repo, "rev-parse", "HEAD")
    worker, candidate = _worker(repo, tmp_path, old_base, _recording("task-b"))
    head = _move_head(repo, _recording("mainline"))

    def lying_hook(g, conflicts):
        git(worker.path, "merge", "--abort")
        write(worker.path, "unrelated.txt", "not the merge at all\n")
        git(worker.path, "add", "-A")
        git(worker.path, "commit", "-qm", "an unrelated commit")
        return True

    execution = _reviewed(worker, candidate, old_base, review_round=1)
    orch = _orch(repo, tmp_path, execution, review_round=1)
    orch._note_conflict_resolver = lambda *a, **k: lying_hook

    result = orch._rebase_execution_if_stale(execution, TASK)

    assert result is None
    message = assert_parked_the_same_way(orch, old_base, head)
    assert "git refused:" in message, "the park must not claim the trackers conflicted"
    assert "does not contain the merged commit" in message
    assert "conflicts at" not in message
    assert execution.task_base_sha == old_base, "nothing was re-pointed"
    assert orch._worker_repos.created == [] and orch._worker_repos.quarantined == []
    assert contains(worker.path, git(worker.path, "rev-parse", "HEAD"), candidate), (
        "the reviewed object is still reachable — this path discards nothing, "
        "it stops and asks"
    )


def test_a_resolver_that_raises_falls_through_to_the_abort(tmp_path):
    """A resolver that blew up has resolved nothing. It must not crash the
    dispatch and must not become a success."""
    from autoloop.errors import GitCommandError

    src, dst, _base, local_tip, incoming = _plain_pair(tmp_path, "raiser")
    gw = _plain_gateway(dst)

    def boom(g, c):
        raise GitCommandError("the index could not be read")

    attempt = gw.merge_foreign_commit(str(src), incoming, "merge", resolve_conflicts=boom)

    assert attempt.merged is False and attempt.restored is True
    assert attempt.conflicted_paths == ("f.txt",)
    assert git(dst, "rev-parse", "HEAD") == local_tip
    assert git(dst, "status", "--porcelain") == ""


def test_a_failure_that_is_not_a_conflict_never_reaches_the_hook(tmp_path):
    """The fail-open case from the other side. A failure with no unmerged path
    — here an unfetchable object — must not reach a resolver at all: handed an
    empty list it could only no-op its way to declaring the merge concluded.
    `test_no_conflicted_path_is_refused_rather_than_read_as_resolved` pins the
    same refusal one layer down, in the resolver itself."""
    src, dst, _base, local_tip, _incoming = _plain_pair(tmp_path, "unfetchable")
    gw = _plain_gateway(dst)
    calls: list = []

    attempt = gw.merge_foreign_commit(
        str(src), "0" * 40, "merge", resolve_conflicts=lambda g, c: calls.append(c) or True
    )

    assert attempt.merged is False
    assert calls == [], "a fetch failure never reaches the resolver"
    assert git(dst, "rev-parse", "HEAD") == local_tip


def test_omitting_the_hook_behaves_exactly_as_before(tmp_path):
    """Every caller that does not pass one — and there is one such caller in
    the tree — gets the pre-existing behaviour, conflict for conflict."""
    src, dst, _base, local_tip, incoming = _plain_pair(tmp_path, "default")
    gw = _plain_gateway(dst)

    attempt = gw.merge_foreign_commit(str(src), incoming, "merge")

    assert attempt.merged is False
    assert attempt.conflicted_paths == ("f.txt",)
    assert attempt.restored is True
    assert git(dst, "rev-parse", "HEAD") == local_tip
