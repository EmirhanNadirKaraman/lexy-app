"""Task registry / graph: ids, dependencies, derived ready/blocked states,
lifecycle transitions, cycle detection, batch atomicity, persistence."""

import json

import pytest

from autoloop.errors import StateCorruptError, StateError, TaskGraphError
from autoloop.tasks import Task, TaskRegistry, TaskState, TaskStore


def task(tid, deps=(), **kw):
    # `priority` is forwarded only when asked for, so an unprioritised task
    # keeps whatever the dataclass default is rather than pinning it here.
    optional = {"priority": kw["priority"]} if "priority" in kw else {}
    return Task(
        id=tid, title=kw.get("title", f"Title {tid}"), description=kw.get("desc", "d"),
        depends_on=tuple(deps), **optional,
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


# The READY count carries a priority-1 breakdown because
# `contract.AUDIT_VS_READY_PREFERENCE` asks the reviewer to prefer ready work
# over a fresh audit, and to weigh how urgent that queue is. `test_context.py`
# owns the other half — that these numbers survive into the rendered CONTEXT
# block the reviewer actually reads.


def test_summary_breaks_out_how_many_ready_tasks_are_priority_one():
    reg = registry(
        task("urgent", priority=1),
        task("also-urgent", priority=1),
        task("someday"),
        task("blocked-p1", deps=["urgent"], priority=1),
    )
    text = reg.summary()
    assert "3 ready (2 at priority 1)" in text
    # A priority-1 task that is BLOCKED is not work the reviewer can pick, so
    # it counts as neither ready nor urgent.
    assert "1 blocked" in text


def test_summary_reports_zero_urgent_when_nothing_is_prioritised():
    """The default priority is 100. A roadmap nobody has triaged must report 0
    at priority 1, not treat every task as urgent."""
    assert "2 ready (0 at priority 1)" in registry(task("a"), task("b")).summary()


def test_summary_reports_an_empty_ready_queue_as_zero():
    """0 ready is the signal that an audit is the right call, so it has to be
    stated rather than left to be inferred from the other counts."""
    reg = registry(task("a"), task("b", deps=["a"]))
    reg.mark_in_progress("a")
    assert "0 ready (0 at priority 1)" in reg.summary()


# ---- description mutation ---------------------------------------------------


#: What the shared validator refuses. `None` is in here rather than in a test
#: of its own on purpose: it exercises the validator's OTHER branch
#: (non-string, which creation used to reach as an `AttributeError` from
#: `.strip()`), so parity is proven on both branches instead of only on blanks.
BAD_DESCRIPTIONS = ["", "   ", None]


def test_set_description_replaces_only_that_tasks_text():
    reg = registry(task("a"), task("b", deps=["a"]))
    before_other = reg.to_dict()["tasks"][0]
    returned = reg.set_description("b", "  a new description  ")
    assert returned is reg.get("b")
    # Byte-identical: creation stores the string it was given, padding and all,
    # so mutation must not normalise what creation would have kept.
    assert reg.get("b").description == "  a new description  "
    # Nothing else moves — not the task's own lifecycle/scope fields, and not
    # its neighbour.
    assert (reg.get("b").status, reg.get("b").approved_paths) == ("pending", ())
    assert reg.to_dict()["tasks"][0] == before_other


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


def test_set_description_unknown_task_is_refused_and_creates_nothing():
    """`task_unknown`, the code every mutator routing through `get` raises
    (`set_priority`'s `unknown_task` is the outlier and stays one — that string
    reaches the operator through the inbox's refusal text). A miss must not
    quietly conjure the task it failed to find."""
    reg = registry(task("a"))
    before = json.dumps(reg.to_dict(), sort_keys=True)
    expect_code(lambda: reg.set_description("ghost", "a good description"), "task_unknown")
    assert not reg.has("ghost")
    assert json.dumps(reg.to_dict(), sort_keys=True) == before


def test_a_rejected_description_leaves_the_registry_byte_identical():
    """Atomicity, asserted over the whole serialised graph rather than the one
    field: this kills an 'assign first, validate second' ordering and any
    mutation that leaves a partially-written task behind."""
    reg = registry(task("a"), task("b", deps=["a"]))
    before = json.dumps(reg.to_dict(), sort_keys=True)
    with pytest.raises(TaskGraphError):
        reg.set_description("b", "   ")
    assert json.dumps(reg.to_dict(), sort_keys=True) == before


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


# ---- retirement: superseded work is not blocked work -------------------------
#
# The distinction these pin: BLOCKED resolves itself, BLOCKED_BY_OPERATOR
# resolves when a human answers, RETIRED resolves for nobody. Six of the seven
# `blocked` tasks on 2026-08-14 were the third kind, saying so only in free
# text, so "7 blocked" meant two opposite things at once.


def test_retire_records_the_successor_and_leaves_the_reason_alone():
    """`superseded_by` is the machine-readable half; `blocked_reason` is the
    prose half and is NOT overwritten by a retirement that brings no new
    reason. Deleting either loses the record."""
    reg = registry(task("brw-02"))
    reg.block("brw-02", "superseded by brw-06")

    retired = reg.retire("brw-02", superseded_by=["brw-06"])

    assert reg.state_of("brw-02") is TaskState.RETIRED
    assert retired.superseded_by == ("brw-06",)
    assert retired.blocked_reason == "superseded by brw-06"


def test_retire_accepts_a_new_reason_and_several_successors():
    reg = registry(task("brw-06"))
    reg.retire("brw-06", superseded_by=("brw-07", "brw-08"), reason="split by the reviewer")
    assert reg.get("brw-06").superseded_by == ("brw-07", "brw-08")
    assert reg.get("brw-06").blocked_reason == "split by the reviewer"


def test_retire_takes_an_in_progress_task():
    """`dash-01` is the reason this is not `release`'s pending-only guard: it
    was in_progress at dispatch with no candidate and no execution record, so
    nothing would ever finish it — which is exactly what needs retiring."""
    reg = registry(task("dash-01"))
    reg.mark_in_progress("dash-01")
    reg.retire("dash-01", reason="stale since 2026-08-03")
    assert reg.state_of("dash-01") is TaskState.RETIRED
    # No successor is legal: it was abandoned, not replaced.
    assert reg.get("dash-01").superseded_by == ()


def test_retire_takes_a_quarantined_task_without_a_trip_through_ready():
    """Deciding a `task_fatal` park will never be worked IS a retirement.
    Forcing it through `unblock` first would put the task back in the READY
    queue in between, where the loop could pick it up."""
    reg = registry(task("t1"))
    reg.block("t1", "attempt ceiling")
    reg.retire("t1", superseded_by=["t2"])
    assert reg.state_of("t1") is TaskState.RETIRED


def test_retire_refuses_a_completed_task():
    reg = registry(task("a"))
    reg.mark_completed("a")
    expect_code(lambda: reg.retire("a"), "task_completed")


def test_a_retired_task_is_never_ready_even_with_every_dependency_done():
    """The state is checked BEFORE the dependency scan, like `blocked` — a
    retired task that still declares its old dependencies must not reappear as
    READY, nor be re-described as BLOCKED."""
    reg = registry(task("a"), task("b", deps=["a"]))
    reg.mark_completed("a")
    reg.retire("b", superseded_by=["c"])
    assert reg.state_of("b") is TaskState.RETIRED
    assert reg.ready_tasks() == []
    assert reg.next_ready() is None


def test_a_retired_task_is_retired_rather_than_blocked_by_its_dependencies():
    reg = registry(task("a"), task("b", deps=["a"]))
    reg.retire("b")
    assert reg.state_of("b") is TaskState.RETIRED


def test_a_task_depending_on_a_retired_one_stays_blocked():
    """Stated because it is a real consequence, not an oversight: retirement
    does not satisfy a dependency — the prerequisite genuinely never happened
    under that id. The dependent must be re-planned against the successor."""
    reg = registry(task("a"), task("b", deps=["a"]))
    reg.retire("a", superseded_by=["a2"])
    assert reg.state_of("b") is TaskState.BLOCKED


def test_a_retired_task_cannot_be_dispatched_or_completed():
    """Defense in depth behind `policy._check_task_reference`, and the message
    names the successor so the refusal is actionable."""
    reg = registry(task("brw-02"))
    reg.retire("brw-02", superseded_by=["brw-06"])
    expect_code(lambda: reg.mark_in_progress("brw-02"), "task_retired")
    expect_code(lambda: reg.mark_completed("brw-02"), "task_retired")
    with pytest.raises(TaskGraphError) as excinfo:
        reg.mark_in_progress("brw-02")
    assert "brw-06" in str(excinfo.value)


def test_unblock_refuses_a_retired_task_and_says_which_state_it_is_in():
    """`answer` calls `unblock`, so a retired task reached from a stale blocker
    must not read as "not blocked" — that is the generic message an operator
    would take for a bug."""
    reg = registry(task("t1"))
    reg.retire("t1", superseded_by=["t2"])
    with pytest.raises(TaskGraphError) as excinfo:
        reg.unblock("t1")
    assert excinfo.value.code == "task_retired"
    assert "t2" in str(excinfo.value)
    assert reg.state_of("t1") is TaskState.RETIRED  # unchanged


def test_release_still_refuses_a_retired_task():
    """`release` is for an interrupted round. A retirement is not that, and it
    must not be launderable back into the queue by the recovery command."""
    reg = registry(task("t1"))
    reg.mark_in_progress("t1")
    reg.retire("t1")
    expect_code(lambda: reg.release("t1"), "task_not_in_progress")


def test_summary_counts_retired_separately_from_quarantined():
    """Folded together, the roadmap line tells the reviewer that superseded
    work is waiting on someone."""
    reg = registry(task("a"), task("b"), task("c"))
    reg.block("b", "answer me")
    reg.retire("c", superseded_by=["d"])
    text = reg.summary()
    assert "1 quarantined" in text
    assert "1 retired" in text


@pytest.mark.parametrize(
    "successors",
    [["has space"], ["t1"], ["t2", "t2"], "t2", [None], [""], 7],
)
def test_superseded_by_shape_is_validated(successors):
    """Shape only: a bad id, a self-reference, a repeat, and a bare string
    (which would otherwise iterate character by character)."""
    reg = registry(task("t1"))
    expect_code(lambda: reg.retire("t1", superseded_by=successors), "bad_superseded_by")


# ---- a retirement is written once -------------------------------------------
#
# The record is the point of the whole state, so the second `retire` is the
# dangerous one: it used to reach an unconditional
# `task.superseded_by = successors`, which made a bare
# `python -m autoloop retire brw-02` the command that DELETES brw-02's chain to
# brw-06 — the one thing this change says is never deleted.


def test_a_bare_second_retirement_cannot_erase_the_recorded_successors():
    """The exact reported call. `retire brw-02` with no `--superseded-by` after
    a real retirement must leave the chain alone, not assign `()` over it."""
    reg = registry(task("brw-02"))
    reg.retire("brw-02", superseded_by=["brw-06"], reason="superseded by brw-06")

    reg.retire("brw-02")  # no successors, no reason — says nothing, changes nothing

    assert reg.get("brw-02").superseded_by == ("brw-06",)
    assert reg.get("brw-02").blocked_reason == "superseded by brw-06"
    assert reg.state_of("brw-02") is TaskState.RETIRED


def test_an_exact_repeat_of_a_retirement_is_a_no_op():
    reg = registry(task("brw-06"))
    reg.retire("brw-06", superseded_by=["brw-07", "brw-08"], reason="split by the reviewer")

    again = reg.retire("brw-06", superseded_by=("brw-07", "brw-08"), reason="split by the reviewer")

    assert again.superseded_by == ("brw-07", "brw-08")
    assert again.blocked_reason == "split by the reviewer"


@pytest.mark.parametrize("successors", [["brw-09"], ["brw-06", "brw-09"], ["brw-09", "brw-06"]])
def test_a_second_retirement_refuses_to_change_the_successors(successors):
    """Adding, replacing or reordering are all rewrites of a historical record.
    Correcting one means planning a task, not overwriting the last decision."""
    reg = registry(task("brw-02"))
    reg.retire("brw-02", superseded_by=["brw-06"])

    expect_code(lambda: reg.retire("brw-02", superseded_by=successors), "task_already_retired")
    assert reg.get("brw-02").superseded_by == ("brw-06",)


def test_a_second_retirement_cannot_reword_the_reason():
    """`blocked_reason` is the prose half of the same record. `block` refreshes
    it because a quarantine can genuinely re-fire; a supersession cannot."""
    reg = registry(task("sub-01"))
    reg.retire("sub-01", superseded_by=["sub-02", "sub-03"], reason="superseded by sub-02/sub-03")

    expect_code(
        lambda: reg.retire("sub-01", reason="never mind, it just failed"),
        "task_already_retired",
    )
    assert reg.get("sub-01").blocked_reason == "superseded by sub-02/sub-03"
    assert reg.get("sub-01").superseded_by == ("sub-02", "sub-03")


def test_a_refusal_to_re_retire_names_the_recorded_successor():
    reg = registry(task("brw-02"))
    reg.retire("brw-02", superseded_by=["brw-06"])
    with pytest.raises(TaskGraphError) as excinfo:
        reg.retire("brw-02", superseded_by=["brw-09"])
    assert "brw-06" in str(excinfo.value)


def test_a_stale_retirement_can_still_be_repeated_without_gaining_a_successor():
    """`dash-01` was retired with no successor because it went stale. Repeating
    that must stay a no-op — it must not become the moment an invented
    successor gets written, and it must not raise either."""
    reg = registry(task("dash-01"))
    reg.retire("dash-01", reason="stale since 2026-08-03")

    reg.retire("dash-01")

    assert reg.get("dash-01").superseded_by == ()
    assert reg.get("dash-01").blocked_reason == "stale since 2026-08-03"


def test_block_refuses_a_retired_task_rather_than_un_retiring_it():
    """The last write path that could silently undo a retirement: `block` ends
    in a bare `status = "blocked"`, which would put a superseded row back under
    "needs a human" with its supersession chain still attached. It should be
    unreachable — a retired task cannot be dispatched, so it cannot park — and
    `_handle_parked_task` fail-closing on the refusal is the right answer if it
    somehow is."""
    reg = registry(task("brw-02"))
    reg.retire("brw-02", superseded_by=["brw-06"], reason="superseded by brw-06")

    expect_code(lambda: reg.block("brw-02", "parked again"), "task_retired")

    assert reg.state_of("brw-02") is TaskState.RETIRED
    assert reg.get("brw-02").superseded_by == ("brw-06",)
    assert reg.get("brw-02").blocked_reason == "superseded by brw-06"


def test_a_successor_does_not_have_to_exist_yet():
    """brw-06 was split into brw-07 + brw-08 before either was planned. A
    supersession is a record, not a schedule — nothing dispatches off it, so it
    is neither a dependency nor part of the cycle check."""
    reg = registry(task("brw-06"))
    reg.retire("brw-06", superseded_by=["brw-07", "brw-08"])
    assert not reg.has("brw-07")
    assert reg.get("brw-06").superseded_by == ("brw-07", "brw-08")


def test_creation_validates_superseded_by_too():
    """One validator, two callers — a shape the registry refuses to create
    must not be writable onto an existing task, and vice versa."""
    expect_code(
        lambda: registry(Task(id="t1", title="t", description="d",
                              superseded_by=("bad id",))),
        "bad_superseded_by",
    )


def test_retirement_survives_a_store_round_trip_as_a_tuple(tmp_path):
    """JSON has no tuples. Without the conversion in `from_dict` the field
    reloads as a list and compares unequal to everything else here."""
    store = TaskStore(tmp_path / "tasks.json")
    reg = registry(task("brw-02"))
    reg.retire("brw-02", superseded_by=["brw-06"], reason="superseded by brw-06")
    store.save(reg)

    loaded = store.load()
    assert loaded.state_of("brw-02") is TaskState.RETIRED
    assert loaded.get("brw-02").superseded_by == ("brw-06",)
    assert loaded.get("brw-02").blocked_reason == "superseded by brw-06"


def test_a_task_file_without_superseded_by_still_loads(tmp_path):
    """Backward compatibility, same rule as `blocked_reason`/`approved_paths`:
    every `tasks.json` on disk today predates this field."""
    path = tmp_path / "tasks.json"
    path.write_text(
        json.dumps({"schema_version": 1, "tasks": [
            {"id": "t1", "title": "t", "description": "d", "depends_on": [],
             "priority": 100, "status": "pending", "created_at": "2026-08-14T00:00:00+00:00",
             "completed_at": None, "blocked_reason": "", "validation": [],
             "validation_cwd": "", "approved_paths": []},
        ]}),
        encoding="utf-8",
    )
    assert TaskStore(path).load().get("t1").superseded_by == ()


# ---- a stored row is validated too -------------------------------------------
#
# `from_dict` deliberately bypasses `add_many`, so it is the ONLY gate a stored
# or hand-edited row passes. It used to run bare `tuple()` over this field,
# which quietly accepted shapes `add_many` refuses.


@pytest.mark.parametrize(
    "value",
    [
        "brw-06",          # a bare string: tuple() made it six one-letter ids
        None,              # explicit null: used to be an uncontrolled TypeError
        ["not an id"],
        ["t1"],            # naming itself
        ["brw-06", "brw-06"],
        7,
        [None],
        {"brw-06": True},
    ],
)
def test_a_malformed_persisted_superseded_by_is_corruption(value):
    """Fails CLOSED, as `StateCorruptError`, like every other unreadable state
    file in this package. Reading a malformed chain as "no successor" would
    delete the record — the exact thing retirement exists to preserve."""
    with pytest.raises(StateCorruptError):
        TaskRegistry.from_dict({"tasks": [
            stored("t1", status="retired", superseded_by=value),
        ]})


def test_a_bare_string_never_becomes_a_tuple_of_characters():
    """Stated separately because this one is silent rather than loud: six valid
    single-character ids read as six successors everywhere downstream, and the
    next save writes them back."""
    with pytest.raises(StateCorruptError) as excinfo:
        TaskRegistry.from_dict({"tasks": [
            stored("brw-02", status="retired", superseded_by="brw-06"),
        ]})
    assert "superseded_by" in str(excinfo.value)


def test_a_store_load_reports_a_malformed_chain_rather_than_dropping_it(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(
        json.dumps({"schema_version": 1, "tasks": [
            {"id": "t1", "title": "t", "description": "d", "depends_on": [],
             "priority": 100, "status": "retired", "superseded_by": "t2"},
        ]}),
        encoding="utf-8",
    )
    with pytest.raises(StateCorruptError):
        TaskStore(path).load()


def test_a_well_formed_persisted_chain_still_loads():
    reg = TaskRegistry.from_dict({"tasks": [
        stored("brw-02", status="retired", superseded_by=["brw-06"]),
    ]})
    assert reg.get("brw-02").superseded_by == ("brw-06",)


# ---- migrating the retirements that predate the state ------------------------
#
# The live `tasks.json` is loop state under `.autoloop/`, outside this
# repository, so the migration is code that runs on load rather than a data
# edit. These pin the two guards that keep it from retiring anything nobody
# retired.


def stored(task_id, **over):
    row = {"id": task_id, "title": task_id.upper(), "description": "d",
           "depends_on": [], "priority": 100, "status": "pending"}
    row.update(over)
    return row


def test_the_six_pre_state_retirements_are_migrated_on_load():
    """Read from each task's own `blocked_reason`, as of 2026-08-14. brw-05
    records brw-02/brw-04 rather than brw-06 because that is what ITS reason
    says — the chain stays traversable one hop at a time."""
    reg = TaskRegistry.from_dict({"tasks": [
        stored("brw-02", status="blocked", blocked_reason="superseded by brw-06"),
        stored("brw-04", status="blocked", blocked_reason="superseded by brw-06"),
        stored("brw-05", status="blocked", blocked_reason="retired with brw-02/brw-04"),
        stored("brw-06", status="blocked",
               blocked_reason="split at the reviewer's request into brw-07 + brw-08"),
        stored("sub-01", status="blocked", blocked_reason="superseded by sub-02 and sub-03"),
        stored("dash-01", status="blocked",
               blocked_reason="stale since 2026-08-03: in_progress at dispatch with "
                              "no candidate and no execution record"),
    ]})

    assert {t.id for t in reg.all_tasks() if reg.state_of(t.id) is TaskState.RETIRED} == {
        "brw-02", "brw-04", "brw-05", "brw-06", "sub-01", "dash-01"
    }
    assert reg.get("brw-02").superseded_by == ("brw-06",)
    assert reg.get("brw-05").superseded_by == ("brw-02", "brw-04")
    assert reg.get("brw-06").superseded_by == ("brw-07", "brw-08")
    assert reg.get("sub-01").superseded_by == ("sub-02", "sub-03")
    # Stale, not replaced. An invented successor would be a false record.
    assert reg.get("dash-01").superseded_by == ()
    # Nothing is deleted: every reason survives the migration verbatim.
    assert reg.get("brw-02").blocked_reason == "superseded by brw-06"
    assert "no candidate" in reg.get("dash-01").blocked_reason


def test_the_migration_leaves_a_genuine_failure_quarantined():
    """audit-0003 is the one real failure among the seven blocked rows. It
    must keep asking for an operator — a migration that swept it up would
    delete the only task on that list anybody has to act on."""
    reg = TaskRegistry.from_dict({"tasks": [
        stored("audit-0003", status="blocked", blocked_reason="failed its own validation"),
    ]})
    assert reg.state_of("audit-0003") is TaskState.BLOCKED_BY_OPERATOR


def test_the_migration_needs_the_reason_to_still_match():
    """The self-limiting guard: a listed id whose reason no longer names its
    successor is left alone. If brw-02 is ever revived and quarantined again
    for a real reason, this must not silently re-retire it — the manual route
    (`autoloop retire`) is then the only way, on purpose."""
    reg = TaskRegistry.from_dict({"tasks": [
        stored("brw-02", status="blocked", blocked_reason="post-commit validation failed"),
    ]})
    assert reg.state_of("brw-02") is TaskState.BLOCKED_BY_OPERATOR
    assert reg.get("brw-02").superseded_by == ()


@pytest.mark.parametrize("status", ["pending", "in_progress", "completed"])
def test_the_migration_only_touches_a_blocked_row(status):
    """The idempotence guard. After the first save the status is `retired`, so
    nothing here matches again — and a listed id that is legitimately pending
    or running is never yanked out from under the loop."""
    reg = TaskRegistry.from_dict({"tasks": [
        stored("brw-02", status=status, blocked_reason="superseded by brw-06"),
    ]})
    assert reg.state_of("brw-02") is not TaskState.RETIRED


def test_the_migration_is_idempotent_across_a_save_and_reload(tmp_path):
    store = TaskStore(tmp_path / "tasks.json")
    store.save(TaskRegistry.from_dict({"tasks": [
        stored("brw-02", status="blocked", blocked_reason="superseded by brw-06"),
    ]}))
    reloaded = store.load()
    assert reloaded.state_of("brw-02") is TaskState.RETIRED
    assert reloaded.get("brw-02").superseded_by == ("brw-06",)


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
