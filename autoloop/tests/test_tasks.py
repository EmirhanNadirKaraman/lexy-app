"""Task registry / graph: ids, dependencies, derived ready/blocked states,
lifecycle transitions, cycle detection, batch atomicity, persistence."""

import json

import pytest

from autoloop.errors import StateCorruptError, StateError, TaskGraphError
from autoloop.tasks import TASKS_SCHEMA_VERSION, Task, TaskRegistry, TaskState, TaskStore


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


# ---- description mutation ---------------------------------------------------


#: Every value the shared description validator refuses. `None` is in here
#: rather than in a test of its own on purpose: it exercises the validator's
#: OTHER branch (non-string, which creation used to reach as an
#: `AttributeError` from `.strip()`), so parity is proven on both branches
#: instead of only on blanks.
BAD_DESCRIPTIONS = ["", "   ", "\n\t", None]


def test_set_description_replaces_the_text():
    reg = registry(task("t1"))
    returned = reg.set_description("t1", "  a new description  ")
    assert returned is reg.get("t1")
    # Byte-identical: creation stores the string it was given, padding and all,
    # so mutation must not normalise what creation would have kept.
    assert reg.get("t1").description == "  a new description  "


def test_set_description_touches_nothing_else():
    reg = registry(task("a"), task("b", deps=["a"]))
    before = reg.to_dict()
    reg.set_description("b", "rewritten")
    after = reg.to_dict()
    before_task, after_task = before["tasks"][1], after["tasks"][1]
    assert after_task["description"] == "rewritten"
    # Every OTHER field, compared as a whole rather than enumerated: an
    # enumeration silently stops covering whatever is added to `Task` next, and
    # the field that must not move here is `approved_paths`.
    assert {k: v for k, v in after_task.items() if k != "description"} == {
        k: v for k, v in before_task.items() if k != "description"
    }
    assert after["tasks"][0] == before["tasks"][0]


def test_set_description_survives_persistence(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    reg = registry(task("a"))
    reg.set_description("a", "the rewritten description")
    store.save(reg)
    assert store.load().get("a").description == "the rewritten description"


@pytest.mark.parametrize("bad", BAD_DESCRIPTIONS)
def test_creation_and_mutation_reject_a_bad_description_identically(bad):
    """The point of extracting `_validate_description`: the two paths must be
    the SAME check, not two checks that happen to agree today. Comparing the
    message as well as the code is what makes that load-bearing — a
    re-implemented check would almost certainly word its refusal differently,
    and this fails the moment mutation stops calling what creation calls."""
    with pytest.raises(TaskGraphError) as created:
        registry(Task(id="t1", title="Title t1", description=bad))
    reg = registry(task("t1"))
    with pytest.raises(TaskGraphError) as mutated:
        reg.set_description("t1", bad)
    assert created.value.code == mutated.value.code == "empty_task_field"
    assert str(created.value) == str(mutated.value)


def test_set_description_unknown_task_rejected():
    """`task_unknown`, the code every mutator routing through `get` raises.
    `set_priority`'s `unknown_task` is the outlier and stays one — that string
    reaches the operator through the inbox's refusal text."""
    expect_code(lambda: registry(task("t1")).set_description("ghost", "x"), "task_unknown")


@pytest.mark.parametrize("bad", BAD_DESCRIPTIONS)
def test_a_rejected_description_leaves_the_registry_byte_identical(bad):
    """Atomicity, asserted over the whole serialised graph rather than the one
    field: this also kills a 'assign first, validate second' ordering and any
    mutation that leaves a partially-written task behind."""
    reg = registry(task("a"), task("b", deps=["a"]))
    before = json.dumps(reg.to_dict(), sort_keys=True)
    with pytest.raises(TaskGraphError):
        reg.set_description("b", bad)
    assert json.dumps(reg.to_dict(), sort_keys=True) == before


def test_a_rejected_unknown_id_creates_nothing():
    reg = registry(task("a"))
    before = json.dumps(reg.to_dict(), sort_keys=True)
    with pytest.raises(TaskGraphError):
        reg.set_description("ghost", "a perfectly good description")
    assert not reg.has("ghost")
    assert json.dumps(reg.to_dict(), sort_keys=True) == before


def test_a_blank_description_already_on_disk_still_loads(tmp_path):
    """`from_dict` deliberately does not re-validate a stored graph, and this
    change must not quietly start. A registry that refuses to LOAD is
    unrecoverable without hand-editing JSON; a blank description written before
    this validator existed is a task to fix, not a file to reject."""
    path = tmp_path / "tasks.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": TASKS_SCHEMA_VERSION,
                "tasks": [{"id": "a", "title": "Title a", "description": ""}],
            }
        ),
        encoding="utf-8",
    )
    assert TaskStore(path).load().get("a").description == ""


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


def test_tracker_paths_are_exactly_the_claude_md_mandates():
    """Pinned as a set. CLAUDE.md makes updating these a CONDITION of the work
    (§12 SUMMARY/TESTS/COMMON_ERRORS, §14 SECURITY), which is why they are
    implicitly approved. Anything ADDED here widens the scope of every task in
    the repository, so it must be a deliberate diff, not an accident — this
    test failing is the intended way to notice.

    Widened 2026-08-04 to six. `CLAUDE.md` and `docs/SCHEMA.md` were each
    earned by a real refusal (rt-06's stale test count, rt-02's migration-table
    row), not guessed at: both writes documented the task's own change, which
    is the same shape as the four already here."""
    from autoloop.tasks import TRACKER_PATHS

    assert set(TRACKER_PATHS) == {
        "CLAUDE.md",
        "docs/COMMON_ERRORS.md",
        "docs/SCHEMA.md",
        "docs/SECURITY.md",
        "docs/SUMMARY.md",
        "docs/TESTS.md",
    }
    # Markdown only: nothing executable, nothing that changes runtime behaviour.
    # The old `startswith("docs/")` half of this assertion was retired when
    # CLAUDE.md (repo root) joined — the property that matters is "a document,
    # not code", and the directory was only ever a proxy for it.
    assert all(p.endswith(".md") for p in TRACKER_PATHS)
    assert not any(p.endswith((".py", ".toml", ".json", ".sh", ".yml")) for p in TRACKER_PATHS)


def test_CLAUDE_md_is_implicitly_approved_with_its_risk_understood():
    """The sharpest entry, pinned separately so it cannot be added or dropped
    without someone reading why.

    Unlike the five docs, CLAUDE.md is not only a record — it is the
    INSTRUCTIONS future agents read, so an executor may now edit the rules it
    will later operate under without that being named in its task. What bounds
    that is NOT trust: `approved_paths` is still enforced from the Task and
    never from anything an agent writes, so a task cannot use a CLAUDE.md edit
    to widen its own scope. This test pins that specific non-circularity —
    if it ever fails, implicit approval has become self-granting."""
    from autoloop.tasks import TRACKER_PATHS, effective_approved_paths

    assert "CLAUDE.md" in TRACKER_PATHS
    # An unscoped task gains nothing, CLAUDE.md included: implicit approval
    # rides along with a real scope, it never creates one.
    assert effective_approved_paths(()) == ()
    # And a scoped task's OWN paths are still whatever the Task declared.
    effective = effective_approved_paths(("only/this.py",))
    assert "only/this.py" in effective
    assert "CLAUDE.md" in effective
    assert "some/other.py" not in effective


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
        "docs/AUTOLOOP.md",   # a doc, but NOT one of the trackers
        "docs/ROADMAP.md",    # ditto — being markdown is not being a tracker
        # `CLAUDE.md` used to be listed here as an out-of-scope example. It
        # became a tracker on 2026-08-04 (rt-06's stale test count), so it is
        # deliberately NOT in this list any more — the property under test is
        # "a path the task did not name and the trackers do not cover stays
        # outside", which is unchanged.
    ):
        assert outside not in effective
    # The widening itself, asserted rather than implied by omission:
    assert "CLAUDE.md" in effective
    assert "docs/SCHEMA.md" in effective
