"""Task registry / graph: ids, dependencies, derived ready/blocked states,
lifecycle transitions, cycle detection, batch atomicity, persistence."""

import json

import pytest

from autoloop.errors import StateCorruptError, StateError, TaskGraphError
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore


def task(tid, deps=(), **kw):
    return Task(
        id=tid, title=kw.get("title", f"Title {tid}"), description=kw.get("desc", "d"),
        depends_on=tuple(deps),
    )


def registry(*tasks_):
    return TaskRegistry(list(tasks_))


def expect_code(fn, code):
    with pytest.raises(TaskGraphError) as excinfo:
        fn()
    assert excinfo.value.code == code


# ---- adding + lookup --------------------------------------------------------


def test_add_and_get():
    reg = registry(task("t1"))
    assert reg.get("t1").title == "Title t1"
    assert reg.has("t1")
    assert not reg.has("t2")


def test_get_unknown_raises():
    expect_code(lambda: registry().get("nope"), "task_unknown")


@pytest.mark.parametrize("bad_id", ["", "has space", "-leading", "x" * 65, "a/b"])
def test_invalid_ids_rejected(bad_id):
    expect_code(lambda: registry(task(bad_id)), "bad_task_id")


def test_duplicate_id_rejected():
    reg = registry(task("t1"))
    expect_code(lambda: reg.add(task("t1")), "duplicate_task")


def test_duplicate_within_batch_rejected():
    expect_code(lambda: registry(task("t1"), task("t1")), "duplicate_task")


def test_empty_title_rejected():
    expect_code(lambda: registry(Task(id="t1", title="  ", description="d")), "empty_task_field")


def test_unknown_dependency_rejected():
    expect_code(lambda: registry(task("t1", deps=["ghost"])), "unknown_dependency")


def test_self_dependency_rejected():
    expect_code(lambda: registry(task("t1", deps=["t1"])), "dependency_cycle")


def test_cycle_rejected():
    expect_code(
        lambda: registry(task("a", deps=["b"]), task("b", deps=["a"])), "dependency_cycle"
    )


def test_dependency_on_same_batch_ok():
    reg = registry(task("a"), task("b", deps=["a"]))
    assert reg.get("b").depends_on == ("a",)


def test_failed_batch_adds_nothing():
    reg = registry(task("t1"))
    with pytest.raises(TaskGraphError):
        reg.add_many([task("t2"), task("t3", deps=["ghost"])])
    assert not reg.has("t2")
    assert not reg.has("t3")


# ---- derived states ---------------------------------------------------------


def test_task_without_deps_is_ready():
    assert registry(task("t1")).state_of("t1") is TaskState.READY


def test_task_with_pending_dep_is_blocked():
    reg = registry(task("a"), task("b", deps=["a"]))
    assert reg.state_of("b") is TaskState.BLOCKED


def test_completing_dep_unblocks():
    reg = registry(task("a"), task("b", deps=["a"]))
    reg.mark_completed("a")
    assert reg.state_of("b") is TaskState.READY


def test_in_progress_state():
    reg = registry(task("t1"))
    reg.mark_in_progress("t1")
    assert reg.state_of("t1") is TaskState.IN_PROGRESS


def test_completed_state():
    reg = registry(task("t1"))
    reg.mark_completed("t1")
    assert reg.state_of("t1") is TaskState.COMPLETED
    assert reg.get("t1").completed_at is not None


# ---- next_ready -------------------------------------------------------------


def test_next_ready_insertion_order():
    reg = registry(task("a"), task("b"))
    assert reg.next_ready().id == "a"


def test_next_ready_skips_blocked_in_progress_completed():
    reg = registry(task("a"), task("b", deps=["a"]), task("c"), task("d"))
    reg.mark_completed("a")  # a done; b becomes ready
    reg.mark_in_progress("b")
    reg.mark_completed("c")
    assert reg.next_ready().id == "d"


def test_next_ready_none_when_nothing_ready():
    reg = registry(task("a"), task("b", deps=["a"]))
    reg.mark_in_progress("a")
    assert reg.next_ready() is None


# ---- lifecycle guards -------------------------------------------------------


def test_mark_in_progress_blocked_denied():
    reg = registry(task("a"), task("b", deps=["a"]))
    expect_code(lambda: reg.mark_in_progress("b"), "task_blocked")


def test_mark_in_progress_completed_denied():
    reg = registry(task("a"))
    reg.mark_completed("a")
    expect_code(lambda: reg.mark_in_progress("a"), "task_completed")


def test_mark_completed_blocked_denied():
    reg = registry(task("a"), task("b", deps=["a"]))
    expect_code(lambda: reg.mark_completed("b"), "task_blocked")


def test_mark_completed_twice_denied():
    reg = registry(task("a"))
    reg.mark_completed("a")
    expect_code(lambda: reg.mark_completed("a"), "task_completed")


# ---- summary ----------------------------------------------------------------


def test_summary_empty():
    assert registry().summary() == "no tasks planned yet"


def test_summary_counts_and_next():
    reg = registry(task("a"), task("b", deps=["a"]), task("c"))
    reg.mark_completed("a")
    reg.mark_in_progress("c")
    text = reg.summary()
    assert "3 tasks" in text
    assert "1 completed" in text
    assert "1 in progress" in text
    assert "1 ready" in text
    assert "0 blocked" in text
    assert "next ready: b" in text


# ---- persistence ------------------------------------------------------------


def test_store_roundtrip(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    reg = registry(task("a"), task("b", deps=["a"]))
    reg.mark_completed("a")
    store.save(reg)
    loaded = store.load()
    assert [t.id for t in loaded.all_tasks()] == ["a", "b"]
    assert loaded.state_of("a") is TaskState.COMPLETED
    assert loaded.state_of("b") is TaskState.READY
    assert loaded.get("b").depends_on == ("a",)


def test_store_load_missing_returns_none(tmp_path):
    assert TaskStore(tmp_path / "tasks.json").load() is None


def test_store_corrupt_raises(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(StateCorruptError):
        TaskStore(path).load()


def test_store_wrong_schema_raises(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({"schema_version": 99, "tasks": []}), encoding="utf-8")
    with pytest.raises(StateError):
        TaskStore(path).load()


def test_store_archive(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    store.save(registry(task("a")))
    backup = store.archive()
    assert backup is not None and backup.exists()
    assert store.load() is None


# ---- always-approved repository trackers ------------------------------------


def test_tracker_paths_are_exactly_the_four_claude_md_mandates():
    """Pinned as a set. CLAUDE.md makes updating these a CONDITION of the work
    (§12 SUMMARY/TESTS/COMMON_ERRORS, §14 SECURITY), which is why they are
    implicitly approved. Anything ADDED here widens the scope of every task in
    the repository, so it must be a deliberate diff, not an accident."""
    from autoloop.tasks import TRACKER_PATHS

    assert set(TRACKER_PATHS) == {
        "docs/COMMON_ERRORS.md",
        "docs/SECURITY.md",
        "docs/SUMMARY.md",
        "docs/TESTS.md",
    }
    # Markdown trackers only: nothing executable, nothing that changes behaviour.
    assert all(p.startswith("docs/") and p.endswith(".md") for p in TRACKER_PATHS)


def test_a_scoped_task_gains_the_trackers():
    from autoloop.tasks import TRACKER_PATHS, effective_approved_paths

    effective = effective_approved_paths(("lexy-app/backend/routers/books.py",))
    assert "lexy-app/backend/routers/books.py" in effective
    assert set(TRACKER_PATHS) <= set(effective)
    assert list(effective) == sorted(effective), "must be sorted for a stable record"


def test_an_UNSCOPED_task_stays_unscoped():
    """The property that must not regress: empty `approved_paths` means "no
    scope authorized yet" and must keep refusing dispatch (docs/SECURITY.md
    finding #2, circular ownership). Returning just the trackers would quietly
    turn an unscoped task into a dispatchable one."""
    from autoloop.tasks import effective_approved_paths

    assert effective_approved_paths(()) == ()


def test_trackers_do_not_authorize_code_outside_the_task_scope():
    """The widening is documentation-only. A source file the task did not name
    is still outside its authorization."""
    from autoloop.tasks import effective_approved_paths

    effective = set(effective_approved_paths(("docs/SECURITY.md",)))
    for outside in (
        "lexy-app/backend/routers/books.py",
        "autoloop/policy.py",
        "docs/AUTOLOOP.md",   # a doc, but NOT one of the four trackers
        "CLAUDE.md",
    ):
        assert outside not in effective
