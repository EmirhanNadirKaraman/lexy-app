"""Two dead ends an operator could only escape by hand-editing state.

`release`: a `loop_fatal` park mid-round leaves its task marked in-progress
with nothing to finish it — `next_ready` skips it forever and no command could
move it. `archive-blocker`: an escape detection refuses every answer by design
and tells you to archive the session, but archiving left the RECORD open and
`start` refuses to run with an open blocker.

Both were hit for real on 2026-08-02 and both were escaped with a Python
one-liner, which is the signal that the CLI was missing something.
"""

import argparse
import json

import pytest

from autoloop import cli
from autoloop.blockers import NO_TASK, BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.errors import GitCommandError, TaskGraphError
from autoloop.state import LoopState, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore
from autoloop.worker_env import WorkerRepoManager
from autoloop.worktask import TaskExecution, TaskExecutionStore

URL = "https://chatgpt.com/c/recovery-commands"


@pytest.fixture
def config(tmp_path):
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path.parent / f"{tmp_path.name}-workers",
    )


@pytest.fixture
def wired(config, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


def _seed(config, *tasks) -> TaskStore:
    store = TaskStore(config.tasks_file)
    store.save(TaskRegistry(list(tasks)))
    return store


def _task(tid="t-1", **kw):
    return Task(id=tid, title="t", description="d", approved_paths=["docs/A.md"], **kw)


# --- registry: release --------------------------------------------------------


def test_release_returns_an_interrupted_round_to_pending():
    registry = TaskRegistry([_task()])
    registry.mark_in_progress("t-1")
    assert registry.state_of("t-1") is TaskState.IN_PROGRESS

    registry.release("t-1")

    assert registry.state_of("t-1") is TaskState.READY
    assert registry.next_ready().id == "t-1"


def test_release_refuses_anything_that_is_not_in_progress():
    """Narrow on purpose: it must not un-complete finished work, and a
    quarantine still goes through `unblock`, which the blocker record is
    tied to."""
    registry = TaskRegistry([_task(), _task("t-2"), _task("t-3")])
    registry.block("t-2", "quarantined")
    registry.mark_completed("t-3")

    for tid in ("t-1", "t-2", "t-3"):
        with pytest.raises(TaskGraphError) as excinfo:
            registry.release(tid)
        assert excinfo.value.code == "task_not_in_progress"

    # completed work stays completed
    assert registry.state_of("t-3") is TaskState.COMPLETED

    # the quarantined one is untouched and still needs `unblock`
    assert registry.state_of("t-2") is TaskState.BLOCKED_BY_OPERATOR
    registry.unblock("t-2")
    assert registry.state_of("t-2") is TaskState.READY


# --- CLI: release -------------------------------------------------------------


def test_release_command_clears_the_status_and_the_worker(wired, capsys):
    """Both halves matter. The status keeps it out of `next_ready`, and a
    stale worker repo makes the next dispatch refuse — fixing only one swaps
    a dead end for another."""
    store = _seed(wired, _task("dash-02"))
    registry = store.load()
    registry.mark_in_progress("dash-02")
    store.save(registry)

    workers = WorkerRepoManager(wired.workers_root, wired.worker_hooks_dir)
    worker_path = workers.path_for("dash-02")
    worker_path.mkdir(parents=True)
    (worker_path / "half-done.txt").write_text("work in progress", encoding="utf-8")

    code = cli._cmd_release(argparse.Namespace(config=None, task_id="dash-02"))

    assert code == 0
    assert store.load().state_of("dash-02") is TaskState.READY
    assert not worker_path.exists(), "the stale worker would block re-dispatch"
    out = capsys.readouterr().out
    assert "in_progress -> pending" in out
    # MOVED, never deleted — an interrupted round usually holds real work.
    assert "kept, not deleted" in out
    quarantined = list(wired.workers_root.parent.glob("quarantine/dash-02-*"))
    assert quarantined, "the worker must survive somewhere"
    assert (quarantined[0] / "half-done.txt").read_text(encoding="utf-8") == "work in progress"


def test_release_command_refuses_a_task_that_is_not_in_progress(wired, capsys):
    _seed(wired, _task("t-1"))

    code = cli._cmd_release(argparse.Namespace(config=None, task_id="t-1"))

    assert code == 1
    assert "not in progress" in capsys.readouterr().out


def test_release_command_works_when_there_is_no_worker_repo(wired, capsys):
    store = _seed(wired, _task("t-1"))
    registry = store.load()
    registry.mark_in_progress("t-1")
    store.save(registry)

    assert cli._cmd_release(argparse.Namespace(config=None, task_id="t-1")) == 0
    out = capsys.readouterr().out
    assert "no worker repo to clear" in out
    # Absence is a no-op, not an error: a task parked before it ever committed
    # has neither half to retire, and `release` must still return it to pending.
    assert "no execution record to retire" in out


# --- CLI: release retires the EXECUTION RECORD too ----------------------------
#
# The third half. `release` fixed the task STATUS and quarantined the WORKER
# REPO, and left the `TaskExecution` record exactly where it was — still
# carrying `candidate_sha`, still claiming live unpublished work for a task
# that had been returned to pending and would be redone from scratch.
#
# `cli._merge_window_blockers` reads those records and held the window shut on
# them. Observed 2026-08-15: releasing 25 stranded tasks the day before left 14
# such records, every one bound to the pre-merge HEAD, and the window could not
# reopen by itself — each of those tasks would have had to be re-dispatched AND
# re-published first. With `auto_merge_enabled` on, pkt-02 completed, published,
# then logged `auto_merge_deferred "merge window closed"`, and the
# published-but-unmerged backlog began rebuilding silently. An operator
# archived the 14 records by hand.


def _stranded(config, task_id="auto-01", candidate="c" * 40):
    """A task interrupted mid-round after a review: in-progress, a worker repo
    on disk with real work in it, and an execution record holding a candidate."""
    store = _seed(config, _task(task_id))
    registry = store.load()
    registry.mark_in_progress(task_id)
    store.save(registry)

    workers = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
    worker_path = workers.path_for(task_id)
    worker_path.mkdir(parents=True)
    (worker_path / "half-done.txt").write_text("work in progress", encoding="utf-8")

    executions = TaskExecutionStore(config.executions_dir)
    executions.save(
        TaskExecution(
            task_id=task_id,
            task_branch=f"autoloop/{task_id}",
            worktree_path=str(worker_path),
            task_base_sha="d2d4d6b8" + "0" * 32,
            candidate_sha=candidate,
            review_round=1,
        )
    )
    return store, executions


def test_release_retires_the_execution_record_alongside_the_worker(wired, capsys):
    """The record must go where the worker goes: archived, never deleted, so
    the candidate stays recoverable — and out of the merge-window gate, which
    is the whole reason the leftover records mattered."""
    store, executions = _stranded(wired)

    assert cli._cmd_release(argparse.Namespace(config=None, task_id="auto-01")) == 0

    assert store.load().state_of("auto-01") is TaskState.READY
    # The LIVE record is gone: nothing reads a candidate for work that will be
    # redone from scratch.
    assert executions.load("auto-01") is None
    assert not (wired.executions_dir / "auto-01.json").exists()
    # ...but it is ARCHIVED, not deleted. The commit it names is inside the
    # quarantined worker repo, so throwing the pointer away would be the one
    # irreversible half of this command.
    archived = sorted((wired.executions_dir / "archive").glob("auto-01-*.json"))
    assert len(archived) == 1, f"expected exactly one archived record, got {archived}"
    record = json.loads(archived[0].read_text(encoding="utf-8"))
    assert record["candidate_sha"] == "c" * 40
    assert record["task_base_sha"] == "d2d4d6b8" + "0" * 32
    assert "kept, not deleted" in capsys.readouterr().out

    # THE POINT: the window is no longer held shut by a record for a task that
    # is back in the queue. Before this, it could not reopen without that task
    # being re-dispatched and re-published first.
    reasons, _notes = cli._merge_window_blockers(wired)
    assert reasons == [], f"the released task must not hold the window shut: {reasons}"


def test_both_halves_are_filed_under_the_same_label(wired):
    """One operation, not two that happen to run next to each other. The label
    is what proves it: the quarantined worker and the archived record name the
    same attempt, so a human reading either finds the other half.

    A CONSTANT label would make this vacuous, which is why the assertion also
    demands a per-call suffix."""
    _stranded(wired, task_id="inbox-05")

    assert cli._cmd_release(argparse.Namespace(config=None, task_id="inbox-05")) == 0

    quarantined = sorted(wired.workers_root.parent.glob("quarantine/inbox-05-*"))
    archived = sorted((wired.executions_dir / "archive").glob("inbox-05-*.json"))
    assert len(quarantined) == 1 and len(archived) == 1

    worker_label = quarantined[0].name[len("inbox-05-"):]
    record_label = archived[0].stem[len("inbox-05-"):]
    assert worker_label == record_label, (
        f"the two halves drifted apart: worker {worker_label!r} vs "
        f"record {record_label!r}"
    )
    prefix = "released-by-operator-"
    assert worker_label.startswith(prefix)
    assert worker_label[len(prefix):], "the label must be unique per call, not a constant"
    # And the evidence really is there, under that shared label.
    assert (quarantined[0] / "half-done.txt").read_text(encoding="utf-8") == "work in progress"


def test_the_record_is_retired_before_the_worker(wired, monkeypatch):
    """Either half can fail, and the two residues are not equally safe.

    A left-behind RECORD is silent: it holds the merge window shut and nothing
    announces it — the exact failure this change ends. A left-behind WORKER is
    loud: the next dispatch's `create()` refuses to write into the existing
    directory and parks naming it. So the record goes first, and whichever half
    fails, the survivor is the one that reports itself."""
    _stranded(wired, task_id="loop-01")

    def refuse(self, task_id, label):
        raise GitCommandError("mv", "quarantine destination is not writable")

    monkeypatch.setattr(WorkerRepoManager, "quarantine", refuse)

    with pytest.raises(GitCommandError):
        cli._cmd_release(argparse.Namespace(config=None, task_id="loop-01"))

    assert not (wired.executions_dir / "loop-01.json").exists(), (
        "the record must be retired first — a surviving one is the silent failure"
    )
    assert sorted((wired.executions_dir / "archive").glob("loop-01-*.json"))
    # The worker survives, and its next dispatch will say so out loud.
    assert WorkerRepoManager(
        wired.workers_root, wired.worker_hooks_dir
    ).path_for("loop-01").exists()


# --- CLI: retire --------------------------------------------------------------
#
# The third dead end, and the one that produced a wrong ANSWER rather than a
# stuck task. Six of the seven `blocked` rows on 2026-08-14 were retirements
# saying so only in free text, so the dashboard's blocked count meant "needs
# you" and "needs nobody" at the same time. `tasks._RETIREMENTS` migrates those
# six on load; this command is the route for every retirement after them, and
# the fallback when a reason no longer matches that table.


def _retire_args(task_id, superseded_by=None, reason="", rewrite_dependents=False):
    return argparse.Namespace(
        config=None, task_id=task_id, superseded_by=superseded_by, reason=reason,
        rewrite_dependents=rewrite_dependents,
    )


def test_retire_command_records_the_successor_and_keeps_everything_else(wired, capsys):
    """Deliberately NOT one of the six ids in `tasks._RETIREMENTS` — those are
    re-filed by the load-time migration, so a test using one would pass
    without the command doing anything. This is a retirement decided after
    that table, which is what the command exists for."""
    store = _seed(wired, _task("old-01"))
    registry = store.load()
    registry.block("old-01", "superseded by new-01")
    store.save(registry)

    code = cli._cmd_retire_task(_retire_args("old-01", superseded_by=["new-01"]))

    assert code == 0
    reloaded = store.load()
    assert reloaded.state_of("old-01") is TaskState.RETIRED
    assert reloaded.get("old-01").superseded_by == ("new-01",)
    # Nothing is deleted: the task, its reason and its scope all survive.
    assert reloaded.get("old-01").blocked_reason == "superseded by new-01"
    assert reloaded.get("old-01").approved_paths == ("docs/A.md",)
    out = capsys.readouterr().out
    assert "blocked -> retired" in out and "new-01" in out


def test_retire_command_takes_a_task_stranded_in_progress(wired, capsys):
    """`dash-01`, the task this command was written for: in_progress at
    dispatch with no candidate and no execution record, so nothing will ever
    finish it. No successor — it went stale rather than being replaced."""
    store = _seed(wired, _task("dash-01"))
    registry = store.load()
    registry.mark_in_progress("dash-01")
    store.save(registry)

    assert cli._cmd_retire_task(_retire_args("dash-01", reason="stale since 2026-08-03")) == 0

    reloaded = store.load()
    assert reloaded.state_of("dash-01") is TaskState.RETIRED
    assert reloaded.get("dash-01").superseded_by == ()
    assert "stale, not replaced" in capsys.readouterr().out


def test_retire_command_refuses_completed_work_and_a_bad_successor(wired, capsys):
    store = _seed(wired, _task("t-1"), _task("t-2"))
    registry = store.load()
    registry.mark_completed("t-1")
    store.save(registry)

    assert cli._cmd_retire_task(_retire_args("t-1", superseded_by=["t-2"])) == 1
    assert "already completed" in capsys.readouterr().out

    # Rejected before anything is written — `retire` validates the successor
    # shape ahead of the single assignment, like every other mutator here.
    assert cli._cmd_retire_task(_retire_args("t-2", superseded_by=["not an id"])) == 1
    assert "not a valid task id" in capsys.readouterr().out

    reloaded = store.load()
    assert reloaded.state_of("t-1") is TaskState.COMPLETED
    assert reloaded.state_of("t-2") is TaskState.READY, "a refused retirement writes nothing"


def test_retiring_a_quarantined_task_closes_its_blocker(wired, capsys):
    """The split brain: a retirement lives in `tasks.json`, the quarantine that
    stopped the task lives in its own record. Moving only the first leaves
    `start`/`health`/`heartbeat` stopped on a question about work nobody is
    going to do — a row that says "waits on nobody" beside a loop that waits."""
    store = _seed(wired, _task("old-01"))
    registry = store.load()
    registry.block("old-01", "attempt count ceiling")
    store.save(registry)
    blockers = BlockerStore(wired.blockers_dir)
    blocker = blockers.record(
        task_id="old-01", kind="task_fatal", code="attempt_count_ceiling",
        question="old-01 hit the attempt ceiling; what now?", detail="3 attempts",
        phase="executing", now="2026-08-14T00:00:00+00:00",
    )

    assert cli._cmd_retire_task(_retire_args("old-01", superseded_by=["new-01"])) == 0

    assert not blockers.open_blockers(), "a retired task cannot still be asking"
    closed = blockers.load(blocker.id)
    # Closed, never deleted, and never answered: nobody responded to this
    # question — the work it belongs to was superseded.
    assert closed.answer is None
    assert "retired" in closed.archived_reason and "new-01" in closed.archived_reason
    assert closed.question == "old-01 hit the attempt ceiling; what now?"
    assert "closed" in capsys.readouterr().out


def test_retiring_one_task_leaves_every_other_blocker_open(wired):
    """The sweep is per task, and `(loop)` blockers are never in it: a login
    expiry is a loop-level condition no task retirement answers."""
    store = _seed(wired, _task("old-01"), _task("audit-0003"))
    registry = store.load()
    registry.block("old-01", "superseded")
    registry.block("audit-0003", "failed its own validation")
    store.save(registry)
    blockers = BlockerStore(wired.blockers_dir)
    for task_id, code in (("old-01", "attempt_count_ceiling"),
                          ("audit-0003", "validation_failed"),
                          (NO_TASK, "login_expired")):
        blockers.record(
            task_id=task_id, kind="task_fatal", code=code, question="q", detail="",
            phase="executing", now="2026-08-14T00:00:00+00:00",
        )

    assert cli._cmd_retire_task(_retire_args("old-01", superseded_by=["new-01"])) == 0

    still_open = {b.task_id for b in blockers.open_blockers()}
    assert still_open == {"audit-0003", NO_TASK}


def test_retiring_a_task_with_no_blocker_is_fine(wired):
    """Most retirements are of pending work that never parked."""
    _seed(wired, _task("old-01"))
    assert cli._cmd_retire_task(_retire_args("old-01", superseded_by=["new-01"])) == 0
    assert not BlockerStore(wired.blockers_dir).open_blockers()


def test_a_second_retire_command_cannot_erase_the_chain(wired, capsys):
    """`python -m autoloop retire brw-02` with no `--superseded-by`, run twice.
    The second used to assign `()` over the recorded successor."""
    store = _seed(wired, _task("old-01"))

    assert cli._cmd_retire_task(_retire_args("old-01", superseded_by=["new-01"])) == 0
    capsys.readouterr()
    assert cli._cmd_retire_task(_retire_args("old-01")) == 0

    assert store.load().get("old-01").superseded_by == ("new-01",)
    out = capsys.readouterr().out
    assert "already retired" in out and "nothing changed" in out


def test_a_retire_command_that_would_rewrite_the_record_is_refused(wired, capsys):
    store = _seed(wired, _task("old-01"))
    assert cli._cmd_retire_task(_retire_args("old-01", superseded_by=["new-01"],
                                             reason="superseded by new-01")) == 0
    capsys.readouterr()

    assert cli._cmd_retire_task(_retire_args("old-01", superseded_by=["other-01"])) == 1
    assert "already retired" in capsys.readouterr().out
    assert cli._cmd_retire_task(_retire_args("old-01", reason="actually it just failed")) == 1

    task = store.load().get("old-01")
    assert task.superseded_by == ("new-01",)
    assert task.blocked_reason == "superseded by new-01"


# --- CLI: retire must not strand the tasks that depend on it ------------------
#
# The operator-facing half of retire-01. The registry refuses; this command has
# to make the refusal ACTIONABLE — every direct dependent by name, the
# transitive count beside it, and the two ways out — because the decision is
# the operator's and 4 direct dependents read very differently from 21 total.


def test_the_retire_command_refuses_a_strand_and_names_the_dependents(wired, capsys):
    store = _seed(
        wired,
        _task("roadmap-01"),
        _task("ingest-01", depends_on=("roadmap-01",)),
        _task("ingest-02", depends_on=("roadmap-01",)),
        _task("ingest-03", depends_on=("ingest-01",)),
    )

    assert cli._cmd_retire_task(_retire_args("roadmap-01")) == 1

    out = capsys.readouterr().out
    assert "ingest-01" in out and "ingest-02" in out
    assert "3 tasks blocked in total" in out, "the transitive count, not just the direct one"
    assert "--rewrite-dependents" in out and "--superseded-by" in out
    reloaded = store.load()
    assert reloaded.state_of("roadmap-01") is TaskState.READY, "nothing was written"
    assert reloaded.get("ingest-01").depends_on == ("roadmap-01",)


def test_the_retire_command_re_points_dependents_at_a_live_successor(wired, capsys):
    store = _seed(
        wired,
        _task("roadmap-01"),
        _task("roadmap-02"),
        _task("ingest-01", depends_on=("roadmap-01",)),
    )

    assert cli._cmd_retire_task(_retire_args("roadmap-01", superseded_by=["roadmap-02"])) == 0

    reloaded = store.load()
    assert reloaded.state_of("roadmap-01") is TaskState.RETIRED
    assert reloaded.get("ingest-01").depends_on == ("roadmap-02",)
    out = capsys.readouterr().out
    assert "dependents re-pointed" in out
    assert "ingest-01 now depends on roadmap-02" in out
    # Both counts survive onto the SUCCESS path — the transitive one is what
    # the operator's decision turned on — but in the past tense. `describe()`
    # is the refusal's sentence and says "blocked in total", which would be a
    # false present-tense claim about tasks this command just unblocked.
    assert "1 that named roadmap-01 directly" in out
    assert "1 task was waiting on it in total" in out
    assert "blocked in total" not in out


def test_the_retire_command_rewrite_flag_drops_the_dependency(wired, capsys):
    """The stale case — nothing continues the work, so the edge goes and the
    dependent is dispatchable again. One command, one save: the retirement and
    the rewrite are in the same file after it."""
    store = _seed(wired, _task("dash-01"), _task("dep-01", depends_on=("dash-01",)))

    assert cli._cmd_retire_task(
        _retire_args("dash-01", reason="stale", rewrite_dependents=True)
    ) == 0

    reloaded = store.load()
    assert reloaded.state_of("dash-01") is TaskState.RETIRED
    assert reloaded.get("dep-01").depends_on == ()
    assert reloaded.state_of("dep-01") is TaskState.READY
    assert "dep-01 now depends on nothing" in capsys.readouterr().out


def test_the_retire_command_says_so_when_nothing_depended_on_the_task(wired, capsys):
    """The ordinary retirement still reports the question it answered — silence
    would leave an operator unsure whether the check ran at all."""
    _seed(wired, _task("old-01"))

    assert cli._cmd_retire_task(_retire_args("old-01", superseded_by=["new-01"])) == 0

    assert "dependents: none" in capsys.readouterr().out


# --- CLI: archive-blocker -----------------------------------------------------


def _record(config, *, task_id="dash-02", code="checkout_escape_detected", session_id=""):
    return BlockerStore(config.blockers_dir).record(
        task_id=task_id,
        kind="loop_fatal",
        code=code,
        question="the write-capable agent changed the PRIMARY checkout",
        detail="",
        phase="executing",
        now="2026-08-02T00:00:00+00:00",
        session_id=session_id,
    )


def _archive_args(blocker_id, reason="session retired; every path inspected"):
    return argparse.Namespace(config=None, blocker_id=blocker_id, reason=reason)


def test_archive_closes_a_blocker_whose_session_is_gone(wired, capsys):
    blocker = _record(wired, session_id="retired-session")
    store = BlockerStore(wired.blockers_dir)
    assert store.open_blockers()

    assert cli._cmd_archive_blocker(_archive_args(blocker.id)) == 0

    assert not store.open_blockers()
    closed = store.load(blocker.id)
    assert closed.archived_reason
    # The distinction the whole command rests on.
    assert closed.answer is None
    assert "NOT as an operator answer" in capsys.readouterr().out


def test_archive_refuses_a_blocker_from_the_live_session(wired, capsys):
    """Otherwise this becomes the 'clear the escape detection' button that
    `_RESOLUTION_PRECONDITIONS` deliberately withholds."""
    StateStore(wired.state_file).save(
        LoopState(session_id="live-session", conversation_url=URL)
    )
    blocker = _record(wired, session_id="live-session")

    assert cli._cmd_archive_blocker(_archive_args(blocker.id)) == 1

    out = capsys.readouterr().out
    assert "still live" in out
    assert BlockerStore(wired.blockers_dir).open_blockers(), "must stay open"


def test_archive_requires_a_reason(wired, capsys):
    blocker = _record(wired, session_id="retired")

    assert cli._cmd_archive_blocker(_archive_args(blocker.id, reason="   ")) == 1
    assert BlockerStore(wired.blockers_dir).open_blockers()


def test_archive_reports_an_unknown_id(wired, capsys):
    assert cli._cmd_archive_blocker(_archive_args("blk-nope-001")) == 1
    assert "no blocker with id" in capsys.readouterr().out


def test_archive_refuses_an_already_closed_blocker(wired, capsys):
    blocker = _record(wired, session_id="retired")
    assert cli._cmd_archive_blocker(_archive_args(blocker.id)) == 0

    assert cli._cmd_archive_blocker(_archive_args(blocker.id)) == 1
    assert "already closed" in capsys.readouterr().out
