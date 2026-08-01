"""Operator task inbox + priority ordering.

The point of the inbox is that it is safe to write at ANY moment, including
while a write-capable agent is running. That safety rests on one property —
it lives outside the checkout, so the escape detector never sees it — and the
first test below pins exactly that.
"""

from __future__ import annotations

import json

import pytest

from autoloop.inbox import InboxError, TaskInbox, inbox_dir_for
from autoloop.tasks import Task, TaskRegistry


def test_the_inbox_lives_outside_the_checkout(tmp_path):
    """The property the whole design depends on. `escape_detector` snapshots
    tracked + untracked + IGNORED paths, so anything under the repo — including
    the gitignored `.autoloop/` — is inside the before/after comparison taken
    around every agent call. An operator write landing there mid-execute would
    park the loop LOOP-FATAL. Placing the inbox beside `workers_root`, which is
    already required to be external, is what makes mid-run submission safe."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    workers_root = tmp_path / "outside" / "workers"

    inbox = inbox_dir_for(workers_root, repo / ".autoloop")

    with pytest.raises(ValueError):
        inbox.resolve().relative_to(repo.resolve())


def test_submit_then_drain_round_trip(tmp_path):
    inbox = TaskInbox(tmp_path / "inbox")
    inbox.submit({"id": "a-1", "title": "T", "description": "D", "priority": 2})
    inbox.submit({"id": "a-2", "title": "T2", "description": "D2"})

    specs, problems = inbox.drain()
    assert problems == []
    assert [s["id"] for s in specs] == ["a-1", "a-2"], "submission order"
    assert specs[0]["priority"] == 2
    # Drained means gone — a second drain must not replay the same requests.
    assert inbox.drain() == ([], [])


def test_submit_refuses_a_malformed_request(tmp_path):
    inbox = TaskInbox(tmp_path / "inbox")
    with pytest.raises(InboxError, match="missing required"):
        inbox.submit({"id": "a-1", "title": "T"})
    with pytest.raises(InboxError, match="unknown field"):
        inbox.submit({"id": "a", "title": "T", "description": "D", "urgency": "high"})
    with pytest.raises(InboxError, match="priority must be an integer"):
        inbox.submit({"id": "a", "title": "T", "description": "D", "priority": "high"})
    assert inbox.pending() == [], "nothing malformed should reach the queue"


def test_an_unparseable_request_is_quarantined_not_replayed_forever(tmp_path):
    """One typo must never stop a running loop, and must not re-fail on every
    drain. Moved aside rather than deleted — it is what the operator wrote."""
    inbox = TaskInbox(tmp_path / "inbox")
    inbox.directory.mkdir(parents=True)
    (inbox.directory / "20260801T000000Z-1-1.json").write_text("{not json", encoding="utf-8")

    specs, problems = inbox.drain()
    assert specs == []
    assert len(problems) == 1
    assert inbox.drain() == ([], []), "must not re-fail forever"
    assert list((inbox.directory / "rejected").glob("*.json")), "evidence preserved"


def test_submit_is_atomic(tmp_path):
    """A drain racing a submit must never see a half-written file."""
    inbox = TaskInbox(tmp_path / "inbox")
    path = inbox.submit({"id": "a-1", "title": "T", "description": "D"})
    assert json.loads(path.read_text())["id"] == "a-1"
    assert not list(inbox.directory.glob("*.tmp")), "no temp file left behind"


# ---- priority ordering -------------------------------------------------------


def test_next_ready_prefers_the_lower_priority_number():
    registry = TaskRegistry()
    registry.add_many([
        Task(id="later", title="L", description="d", priority=5),
        Task(id="urgent", title="U", description="d", priority=1),
        Task(id="default", title="D", description="d"),  # 100
    ])
    assert registry.next_ready().id == "urgent"


def test_a_task_added_later_can_overtake_one_already_queued():
    """The reason ordering changed from insertion order: otherwise an operator
    cannot steer a running loop — a task added mid-run could never be picked
    before the ones already queued, however urgent."""
    registry = TaskRegistry()
    registry.add_many([Task(id="first", title="F", description="d", priority=50)])
    assert registry.next_ready().id == "first"

    registry.add_many([Task(id="second", title="S", description="d", priority=1)])
    assert registry.next_ready().id == "second"


def test_equal_priorities_break_on_id_not_dict_order():
    registry = TaskRegistry()
    registry.add_many([
        Task(id="zzz", title="Z", description="d", priority=7),
        Task(id="aaa", title="A", description="d", priority=7),
    ])
    assert registry.next_ready().id == "aaa"


def test_priority_survives_a_persistence_round_trip(tmp_path):
    from autoloop.tasks import TaskStore

    store = TaskStore(tmp_path / "tasks.json")
    registry = TaskRegistry()
    registry.add_many([Task(id="p", title="P", description="d", priority=3)])
    store.save(registry)
    assert store.load().get("p").priority == 3


def test_an_old_tasks_file_without_priority_still_loads(tmp_path):
    """Backward compatibility: a roadmap written before the field existed must
    keep working, defaulting to last place rather than jumping the queue."""
    from autoloop.tasks import TaskStore

    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "tasks": [{"id": "old", "title": "O", "description": "d", "status": "pending"}],
    }), encoding="utf-8")
    assert TaskStore(path).load().get("old").priority == 100
