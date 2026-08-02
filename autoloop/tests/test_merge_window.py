"""Two dead ends that came from the operator and the loop sharing one branch.

`merge-window`: every merge into the branch the loop builds against, while a
task holds a candidate, strands that task — the loop refuses to rebase
(correctly: a reviewer has already seen the candidate) and parks. It happened
four times on 2026-08-02, each time because "no agent is running right now"
was mistaken for "safe to merge".

Stale completed-task park: resolving a park BY COMPLETING its task then left a
session that could only be archived, because `block` refuses a completed task
and the fail-closed branch escalated that refusal to loop_fatal.
"""

import argparse
import json

import pytest

from autoloop import cli
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore

URL = "https://chatgpt.com/c/merge-window"


@pytest.fixture
def config(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=checkout / ".autoloop",
        workers_root=tmp_path / "workers",
    )


@pytest.fixture
def wired(config, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return config


def _state(config, phase=Phase.AWAITING.value, **kw):
    StateStore(config.state_file).save(
        LoopState(session_id="mw", conversation_url=URL, phase=phase, **kw)
    )


def _execution(config, task_id="t-1", candidate="abc123def456", base="000111222333"):
    d = config.state_dir / "executions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "candidate_sha": candidate, "task_base_sha": base}),
        encoding="utf-8",
    )


def _args(**kw):
    return argparse.Namespace(
        config=None, wait=kw.get("wait", False),
        timeout=kw.get("timeout", 0.1), poll=kw.get("poll", 0.01),
    )


# --- merge-window -------------------------------------------------------------


def test_an_in_flight_candidate_closes_the_window(wired, capsys):
    """THE case a phase check misses. No agent is running and the phase is
    quiet, but a candidate is bound to an older base — merging strands it."""
    _state(wired, phase=Phase.AWAITING.value)
    _execution(wired)

    assert cli._cmd_merge_window(_args()) == 1

    out = capsys.readouterr().out
    assert "CLOSED" in out
    assert "would strand it" in out
    assert "abc123def456"[:12] in out


def test_a_quiet_loop_with_no_candidate_is_safe(wired, capsys):
    _state(wired, phase=Phase.AWAITING.value)

    assert cli._cmd_merge_window(_args()) == 0
    assert "OPEN" in capsys.readouterr().out


def test_an_executing_phase_closes_the_window(wired, capsys):
    _state(wired, phase=Phase.EXECUTING.value)

    assert cli._cmd_merge_window(_args()) == 1
    assert "executing" in capsys.readouterr().out


def test_an_execution_record_without_a_candidate_does_not_close_it(wired):
    """A dispatched task that has not committed yet holds no reviewed work,
    so there is nothing a moved head could discard."""
    _state(wired, phase=Phase.AWAITING.value)
    d = wired.state_dir / "executions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "t-1.json").write_text(json.dumps({"task_id": "t-1"}), encoding="utf-8")

    assert cli._cmd_merge_window(_args()) == 0


def test_no_session_at_all_is_safe(wired):
    assert cli._cmd_merge_window(_args()) == 0


def test_an_unreadable_execution_record_is_skipped_not_fatal(wired):
    """A torn write must not make the tool unusable — it reports on the
    records it can read."""
    _state(wired, phase=Phase.AWAITING.value)
    d = wired.state_dir / "executions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "t-1.json").write_text("{not json", encoding="utf-8")

    assert cli._cmd_merge_window(_args()) == 0


def test_wait_gives_up_and_reports_rather_than_hanging(wired, capsys):
    _state(wired, phase=Phase.EXECUTING.value)

    assert cli._cmd_merge_window(_args(wait=True, timeout=0.05)) == 1
    assert "gave up" in capsys.readouterr().out


# --- a stale park whose task has since completed ------------------------------


def _parked(config, task_id="t-1"):
    store = StateStore(config.state_file)
    store.save(
        LoopState(
            session_id="mw", conversation_url=URL, phase=Phase.NEEDS_USER.value,
            park_kind="task_fatal", park_task_id=task_id,
            question=f"task {task_id}: its recorded base is behind the branch head",
        )
    )
    return store


def test_a_park_whose_task_is_now_completed_does_not_stop_the_loop(config, capsys):
    """Resolving a park by publishing its candidate and completing the task
    used to produce a session that could only be archived: `block` refuses a
    completed task, and the fail-closed branch escalated that to loop_fatal."""
    store = _parked(config)
    task_store = TaskStore(config.tasks_file)
    registry = TaskRegistry([Task(id="t-1", title="t", description="d")])
    registry.mark_completed("t-1")
    task_store.save(registry)

    outcome = cli._handle_parked_task(
        config, store, task_store, registry, store.load()
    )

    assert outcome == "task_fatal", "continuous mode must carry on"
    out = capsys.readouterr().out
    assert "already completed" in out
    assert "stale" in out
    # The session is cleared, so the next pass starts fresh rather than
    # re-reading the same park.
    assert not config.state_file.exists()
    # And completion is untouched — nothing tried to un-complete it.
    assert task_store.load().state_of("t-1") is TaskState.COMPLETED


def test_a_park_whose_task_is_unknown_still_escalates(config, capsys):
    """The fail-closed branch must survive: only the completed case is
    reinterpreted, because only that one is provably not a fault."""
    store = _parked(config, task_id="ghost-1")
    task_store = TaskStore(config.tasks_file)
    registry = TaskRegistry([Task(id="t-1", title="t", description="d")])
    task_store.save(registry)

    outcome = cli._handle_parked_task(
        config, store, task_store, registry, store.load()
    )

    assert outcome != "task_fatal"
    assert "loop_fatal" in capsys.readouterr().out


def test_an_ordinary_task_fatal_park_still_quarantines(config, capsys):
    store = _parked(config)
    task_store = TaskStore(config.tasks_file)
    registry = TaskRegistry([Task(id="t-1", title="t", description="d")])
    task_store.save(registry)

    outcome = cli._handle_parked_task(
        config, store, task_store, registry, store.load()
    )

    assert outcome == "task_fatal"
    assert task_store.load().state_of("t-1") is TaskState.BLOCKED_BY_OPERATOR
