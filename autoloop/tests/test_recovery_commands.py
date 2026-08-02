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

import pytest

from autoloop import cli
from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.errors import TaskGraphError
from autoloop.state import LoopState, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore

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

    from autoloop.worker_env import WorkerRepoManager

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
    assert "no worker repo to clear" in capsys.readouterr().out


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
