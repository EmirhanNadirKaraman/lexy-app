"""Task registry / graph: ids, dependencies, derived ready/blocked states,
lifecycle transitions, cycle detection, batch atomicity, persistence."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import autoloop
from autoloop.errors import LockHeldError, StateCorruptError, StateError, TaskGraphError
from autoloop.tasks import (
    LEDGER_PHASE_COMPLETE,
    LEDGER_PHASE_INTENT,
    StrandReport,
    Task,
    TaskRegistry,
    TaskState,
    TaskStore,
)

#: The directory the `autoloop` package lives in, handed to child processes as
#: `PYTHONPATH` so they import the SAME source this test does regardless of
#: where pytest was invoked from.
PACKAGE_ROOT = Path(autoloop.__file__).resolve().parent.parent


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


# ---- approved_paths + depends_on mutation -----------------------------------


def test_set_approved_paths_replaces_rather_than_merges():
    """REPLACES, on purpose. A merging mutator can only ever widen the field
    that decides what an agent may write, which makes correcting a mistaken
    scope impossible — a one-way ratchet on authorization."""
    reg = registry(task("a"))
    reg.set_approved_paths("a", ["autoloop/inbox.py", "autoloop/tasks.py"])
    reg.set_approved_paths("a", ["autoloop/tasks.py"])
    assert reg.get("a").approved_paths == ("autoloop/tasks.py",)


def test_set_approved_paths_can_revoke_a_scope_entirely():
    """Clearing to () means "no scope authorized", which
    `effective_approved_paths` keeps empty and dispatch refuses. Revoking must
    park the task, not silently hand it the always-allowed trackers."""
    from autoloop.tasks import effective_approved_paths

    reg = registry(task("a"))
    reg.set_approved_paths("a", ["autoloop/inbox.py"])
    reg.set_approved_paths("a", [])
    assert reg.get("a").approved_paths == ()
    assert effective_approved_paths(reg.get("a").approved_paths) == ()


#: Scopes the shared validator refuses, one per branch of
#: `_validate_approved_paths`: a glob, a traversal, an absolute path, a
#: duplicate entry, and the bare string that used to be iterated per character.
BAD_SCOPES = [
    ["autoloop/*.py"],
    ["../secrets.txt"],
    ["/etc/passwd"],
    ["autoloop/inbox.py", "autoloop/inbox.py"],
    "autoloop/inbox.py",
]


@pytest.mark.parametrize("bad", BAD_SCOPES)
def test_creation_and_mutation_reject_a_bad_scope_identically(bad):
    """The reason `_validate_approved_paths` was extracted rather than reused
    by eye: the duplicate rule used to live inline in `add_many` and nowhere
    else, so a mutation calling only the singular `_validate_approved_path`
    could write a scope creation refuses. Comparing the MESSAGE is what makes
    this load-bearing — a re-implemented check would word its refusal
    differently."""
    with pytest.raises(TaskGraphError) as created:
        registry(Task(id="t1", title="T", description="d", approved_paths=bad))
    reg = registry(task("t1"))
    with pytest.raises(TaskGraphError) as mutated:
        reg.set_approved_paths("t1", bad)
    assert created.value.code == mutated.value.code
    assert str(created.value) == str(mutated.value)


def test_a_rejected_scope_leaves_the_registry_byte_identical():
    """The duplicate is the second entry, so a validator that checked as it
    assigned would already have written the first one."""
    reg = registry(task("a"))
    reg.set_approved_paths("a", ["autoloop/inbox.py"])
    before = json.dumps(reg.to_dict(), sort_keys=True)
    expect_code(
        lambda: reg.set_approved_paths("a", ["docs/TESTS.md", "docs/TESTS.md"]),
        "duplicate_approved_path",
    )
    assert json.dumps(reg.to_dict(), sort_keys=True) == before


def test_set_depends_on_replaces_and_redrives_the_derived_state():
    """Ready/blocked is DERIVED, never stored, so a dependency change must move
    the state with no second write anywhere."""
    reg = registry(task("a"), task("b"))
    assert reg.state_of("b") is TaskState.READY
    reg.set_depends_on("b", ["a"])
    assert reg.state_of("b") is TaskState.BLOCKED
    reg.set_depends_on("b", [])
    assert reg.state_of("b") is TaskState.READY


def test_set_depends_on_refuses_an_unknown_task_a_cycle_and_a_self_edge():
    reg = registry(task("a"), task("b", deps=["a"]))
    expect_code(lambda: reg.set_depends_on("a", ["ghost"]), "unknown_dependency")
    expect_code(lambda: reg.set_depends_on("a", ["a"]), "dependency_cycle")
    # a -> b -> a. Only `_check_acyclic` catches this one: every id is known
    # and no edge is a self-edge, so the per-entry checks all pass.
    expect_code(lambda: reg.set_depends_on("a", ["b"]), "dependency_cycle")


def test_a_rejected_dependency_change_leaves_the_registry_byte_identical():
    """The cycle check needs the whole graph, so the tempting implementation is
    assign-then-check-then-revert. This fails that one: `to_dict` is compared
    over the entire registry, and a revert that misses `depends_on`'s tuple
    identity or leaves the edge in place shows up here."""
    reg = registry(task("a"), task("b", deps=["a"]), task("c"))
    before = json.dumps(reg.to_dict(), sort_keys=True)
    expect_code(lambda: reg.set_depends_on("a", ["b"]), "dependency_cycle")
    assert json.dumps(reg.to_dict(), sort_keys=True) == before
    assert reg.get("a").depends_on == ()


@pytest.mark.parametrize("bad", ["ab", None, 7, ["has space"], [None]])
def test_creation_and_mutation_reject_bad_dependencies_identically(bad):
    """Same parity rule as descriptions and scopes. `"ab"` is the interesting
    entry: creation used to iterate it per character and refuse it as
    `unknown task 'a'`, naming a task nobody wrote."""
    with pytest.raises(TaskGraphError) as created:
        registry(task("x"), Task(id="t1", title="T", description="d", depends_on=bad))
    reg = registry(task("x"), task("t1"))
    with pytest.raises(TaskGraphError) as mutated:
        reg.set_depends_on("t1", bad)
    assert created.value.code == mutated.value.code
    assert str(created.value) == str(mutated.value)


# ---- the strand guard -------------------------------------------------------


#: Every mutator that rewrites a field a live dispatch is judged against.
#: `set_priority` is deliberately absent — it only orders `next_ready()`.
CONTENT_MUTATIONS = [
    ("set_description", "a new description"),
    ("set_approved_paths", ["autoloop/tasks.py"]),
    ("set_depends_on", []),
]


@pytest.mark.parametrize("method,value", CONTENT_MUTATIONS)
def test_an_in_progress_task_cannot_be_edited(method, value):
    """The strand: a dispatch has already started and is being judged against
    all three fields. `depends_on` is the worst — a new incomplete dependency
    makes `state_of` report BLOCKED, and from there `mark_completed` refuses
    the finished round AND `release` refuses to return it to pending, so the
    task is stuck with no command able to move it."""
    reg = registry(task("a"))
    reg.mark_in_progress("a")
    expect_code(lambda: getattr(reg, method)("a", value), "task_in_progress")


def test_an_in_progress_scope_cannot_be_emptied():
    """The sharpest shape of the refusal above, and the one `CONTENT_MUTATIONS`
    cannot reach: it passes a NON-empty scope, so nothing pinned the case where
    the mutation UN-authorizes a dispatch that has already started. Emptying is
    worse than rewriting — the round is being judged against the old scope, and
    an empty one is also what `_dispatch_task_postcommit` refuses outright, so
    the running task would be left with no scope to complete against.

    The guard runs BEFORE `_validate_approved_paths`, which is why the refusal
    is `task_in_progress` rather than anything about the empty list — clearing a
    scope is legal (`test_set_approved_paths_can_revoke_a_scope_entirely`), just
    not on a task the loop is running. The scope is re-read afterwards because
    "refused" and "refused without writing" are different claims."""
    reg = registry(task("a"))
    reg.set_approved_paths("a", ["autoloop/inbox.py"])
    reg.mark_in_progress("a")
    expect_code(lambda: reg.set_approved_paths("a", []), "task_in_progress")
    assert reg.get("a").approved_paths == ("autoloop/inbox.py",)


def test_the_strand_guard_reads_stored_status_not_the_derived_state():
    """The mutation check that matters most. `state_of` tests dependencies
    BEFORE the in_progress branch, so an in-progress task that already has an
    incomplete dependency reports BLOCKED — and a guard written as
    `state_of(...) is IN_PROGRESS` would fall silent on exactly the task that
    is already in the stranded shape.

    Built through `from_dict`, which is where this shape really comes from:
    that path deliberately bypasses `add_many` (re-validating a stored graph
    would reject states only the running loop can produce), so a `tasks.json`
    carrying an in-progress task whose dependency is not complete loads
    exactly as written."""
    reg = TaskRegistry.from_dict({
        "schema_version": 1,
        "tasks": [
            {"id": "dep", "title": "D", "description": "d", "status": "pending"},
            {"id": "a", "title": "A", "description": "d", "status": "in_progress",
             "depends_on": ["dep"]},
        ],
    })
    assert reg.state_of("a") is TaskState.BLOCKED, "the trap this guard walks into"
    assert reg.get("a").status == "in_progress", "but it IS still running"
    expect_code(lambda: reg.set_description("a", "rewritten"), "task_in_progress")


@pytest.mark.parametrize("method,value", CONTENT_MUTATIONS)
def test_terminal_records_are_not_editable(method, value):
    """Completed and retired tasks are records, not queue. Rewriting the scope
    a finished commit was checked against, or the description of work that
    shipped under a successor's id, edits history."""
    reg = registry(task("done"), task("gone"))
    reg.mark_completed("done")
    reg.retire("gone", superseded_by=["done"])
    expect_code(lambda: getattr(reg, method)("done", value), "task_completed")
    expect_code(lambda: getattr(reg, method)("gone", value), "task_retired")


@pytest.mark.parametrize("method,value", CONTENT_MUTATIONS)
def test_a_quarantined_task_stays_editable(method, value):
    """`blocked` is mutable and that is the point: correcting a description or
    widening a scope is what a quarantined task usually needs BEFORE its
    blocker can be answered. A guard that refused every non-pending status
    would make the fix unreachable."""
    reg = registry(task("a"))
    reg.block("a", "the executor could not find the file")
    getattr(reg, method)("a", value)
    assert reg.state_of("a") is TaskState.BLOCKED_BY_OPERATOR, "still quarantined"


# ---- the approved decomposition ---------------------------------------------
#
# Every task is decomposed and the decomposition approved before any code is
# written (operator decision, 2026-08-17). The registry is where the approval
# becomes durable: policy reads it to authorize `implement`, and the
# implementing agent is shown it.


DECOMP_TEXT = "Approach: one commit\nFiles expected to change:\n  - a.py\nThis is one step:\n  1. go"


def test_an_approved_decomposition_is_durable_across_a_save_and_load(tmp_path):
    """THE durability claim. The plan is approved on one directive and read by
    a later round — possibly in a different process — so it has to survive the
    task file, not just the registry that is in memory right now."""
    store = TaskStore(tmp_path / "tasks.json")
    reg = registry(task("a"))
    reg.set_decomposition("a", DECOMP_TEXT)
    store.save(reg)

    assert store.load().get("a").decomposition == DECOMP_TEXT


def test_a_task_file_written_before_the_field_existed_still_loads():
    """Backward compatibility, same pattern as `approved_paths`/`hold_origin`:
    a missing key loads as "no plan approved yet" rather than corrupting, and a
    hand-edited `null` is coerced instead of becoming `None`."""
    reg = TaskRegistry.from_dict({
        "schema_version": 1,
        "tasks": [
            {"id": "old", "title": "T", "description": "d"},
            {"id": "edited", "title": "T", "description": "d", "decomposition": None},
        ],
    })
    assert reg.get("old").decomposition == ""
    assert reg.get("edited").decomposition == ""


def test_a_reshape_replaces_the_stored_plan_rather_than_appending():
    """REPLACES, like `set_approved_paths`: a reviewer reshaping a plan has to
    be able to drop a step, and a merging setter could only ever add."""
    reg = registry(task("a"))
    reg.set_decomposition("a", DECOMP_TEXT)
    reg.set_decomposition("a", "Approach: two commits\nSteps, in order:\n  1. x\n  2. y")
    assert "two commits" in reg.get("a").decomposition
    assert "one commit" not in reg.get("a").decomposition


def test_a_running_task_can_still_have_its_plan_reshaped():
    """Deliberately unlike `set_description`, which `_refuse_immutable` blocks
    on an in-progress task. The difference is the author and the timing: a
    description is rewritten by an operator at an arbitrary moment, while this
    is written by the dispatch itself, before that round's executor starts, and
    a `revise` reshaping the plan is exactly when a reviewer is entitled to.
    Refusing here would leave a reshaped plan unreachable, since a task under
    review is in progress by definition."""
    reg = registry(task("a"))
    reg.set_decomposition("a", DECOMP_TEXT)
    reg.mark_in_progress("a")
    reg.set_decomposition("a", "Approach: reshaped\nSteps, in order:\n  1. x\n  2. y")
    assert "reshaped" in reg.get("a").decomposition


def test_a_blank_plan_is_refused_rather_than_clearing_the_approval():
    """Empty means "nothing approved yet", the state policy refuses to dispatch
    from. Reaching it by writing would let a reshape silently un-approve a task
    that a reviewer had already approved."""
    reg = registry(task("a"))
    reg.set_decomposition("a", DECOMP_TEXT)
    for bad in ("", "   ", None):
        expect_code(lambda: reg.set_decomposition("a", bad), "empty_task_field")
    assert reg.get("a").decomposition == DECOMP_TEXT


def test_a_terminal_records_plan_is_not_rewritten():
    """Same reasoning as the other terminal-record refusals: the decomposition
    of completed or superseded work is the record of what was approved."""
    reg = registry(task("done"), task("gone"))
    reg.mark_completed("done")
    reg.retire("gone", superseded_by=["done"])
    expect_code(lambda: reg.set_decomposition("done", DECOMP_TEXT), "task_completed")
    expect_code(lambda: reg.set_decomposition("gone", DECOMP_TEXT), "task_retired")


def test_setting_a_plan_on_an_unknown_task_is_refused_and_creates_nothing():
    reg = registry(task("a"))
    expect_code(lambda: reg.set_decomposition("ghost", DECOMP_TEXT), "task_unknown")
    assert not reg.has("ghost")


def test_priority_is_still_editable_on_a_running_task():
    """Not an oversight. Priority only orders `next_ready()`, so it cannot
    strand anything — and it is the one mutation the dashboard already
    queues, so narrowing it here would break a shipped route."""
    reg = registry(task("a"))
    reg.mark_in_progress("a")
    assert reg.set_priority("a", 1).priority == 1


# ---- operator holds ---------------------------------------------------------


def test_an_operator_hold_is_reversible_through_its_own_pair():
    """The whole reason `operator_block`/`operator_unblock` exist. A hold
    placed through the inbox writes no `blockers.Blocker` record, and
    `python -m autoloop answer` — the only route out of `blocked` — takes a
    blocker id. Without the reverse, holding a task would be a one-way door."""
    from autoloop.tasks import OPERATOR_HOLD_PREFIX

    reg = registry(task("a"))
    held = reg.operator_block("a", "waiting on the API key")
    assert reg.state_of("a") is TaskState.BLOCKED_BY_OPERATOR
    assert held.blocked_reason == OPERATOR_HOLD_PREFIX + "waiting on the API key"

    reg.operator_unblock("a")
    assert reg.state_of("a") is TaskState.READY
    assert reg.get("a").blocked_reason == ""


def test_the_operator_reverse_will_not_release_a_loop_raised_quarantine():
    """Narrowed by provenance, not by trust. A `task_fatal` park is resolved by
    `answer`, which resolves the blocker record and unblocks the task together.
    If this reverse released it too, anything able to write to the inbox could
    put a quarantined task straight back into `ready_tasks()` with its blocker
    still open and unanswered."""
    reg = registry(task("a"))
    reg.block("a", "validation failed three times")
    expect_code(lambda: reg.operator_unblock("a"), "task_blocked_by_operator")
    assert reg.state_of("a") is TaskState.BLOCKED_BY_OPERATOR
    assert reg.get("a").blocked_reason == "validation failed three times"
    # `answer`'s route is unaffected — it calls `unblock` directly.
    reg.unblock("a")
    assert reg.state_of("a") is TaskState.READY


def test_a_loop_quarantine_whose_reason_reads_like_a_hold_is_still_refused():
    """The hole the previous test could not see. Provenance used to be
    `blocked_reason.startswith(OPERATOR_HOLD_PREFIX)`, and `blocked_reason` is
    unconstrained free text that ordinary quarantines write too — a park detail
    quoting an operator's note, or one crafted to, made a REAL quarantine
    releasable from the inbox with its `blockers.Blocker` record still open and
    unanswered. That is exactly the laundering this pair claims to prevent.

    The middle assertion is the one that pins the fix rather than re-testing
    the refusal: `block` must not set the origin no matter what the reason
    says, so the marker is written by exactly one method and never inferred."""
    from autoloop.tasks import HOLD_ORIGIN_OPERATOR, OPERATOR_HOLD_PREFIX

    reg = registry(task("a"))
    reason = OPERATOR_HOLD_PREFIX + "the agent said it was pausing for review"
    reg.block("a", reason)

    assert reg.get("a").hold_origin == "", "block never records an operator hold"
    expect_code(lambda: reg.operator_unblock("a"), "task_blocked_by_operator")
    assert reg.state_of("a") is TaskState.BLOCKED_BY_OPERATOR
    assert reg.get("a").blocked_reason == reason, "the record is untouched"
    # And the real thing still works, so the guard is not simply refusing all.
    reg.unblock("a")
    reg.operator_block("a", "waiting on the API key")
    assert reg.get("a").hold_origin == HOLD_ORIGIN_OPERATOR
    reg.operator_unblock("a")
    assert reg.state_of("a") is TaskState.READY


def test_a_released_task_keeps_no_hold_origin_for_the_next_quarantine():
    """The marker has to be cleared on the way out, not just written on the way
    in. Left behind, it would sit on the row until the loop quarantines that
    task for real — and then say an operator held it."""
    from autoloop.tasks import HOLD_ORIGIN_OPERATOR

    reg = registry(task("a"))
    reg.operator_block("a", "waiting on the API key")
    reg.operator_unblock("a")
    assert reg.get("a").hold_origin == ""

    # ... and a re-block by the loop cannot inherit one either.
    reg.operator_block("a", "waiting again")
    assert reg.get("a").hold_origin == HOLD_ORIGIN_OPERATOR
    reg.block("a", "post-commit validation failed")
    assert reg.get("a").hold_origin == ""
    expect_code(lambda: reg.operator_unblock("a"), "task_blocked_by_operator")


def test_a_refused_hold_leaves_no_marker_behind():
    """`operator_block` records the origin AFTER the delegate returns, so a
    hold `block` refuses never stamps a task that was not held."""
    reg = registry(task("done"), task("gone"), task("running"))
    reg.mark_completed("done")
    reg.retire("gone")
    reg.mark_in_progress("running")
    for tid in ("done", "gone", "running"):
        with pytest.raises(TaskGraphError):
            reg.operator_block(tid, "why")
        assert reg.get(tid).hold_origin == ""


def test_an_operator_hold_never_overwrites_a_recorded_quarantine():
    """`block` is idempotent and REFRESHES the reason, which is right for a
    park that re-fires and wrong here: it would replace the account of a real
    failure with an operator's note AND stamp it as inbox-releasable, which is
    the previous test's guard laundered away."""
    reg = registry(task("a"))
    reg.block("a", "validation failed three times")
    expect_code(
        lambda: reg.operator_block("a", "actually let us pause this"),
        "task_blocked_by_operator",
    )
    assert reg.get("a").blocked_reason == "validation failed three times"


def test_an_operator_hold_is_refused_on_a_running_task():
    """Holding a running round strands it: the round finishes and pushes, and
    then `mark_completed` refuses it as quarantined (the B10 failure, which is
    deliberate there and must not be reachable on purpose from here)."""
    reg = registry(task("a"))
    reg.mark_in_progress("a")
    expect_code(lambda: reg.operator_block("a", "pause this"), "task_in_progress")
    assert reg.get("a").status == "in_progress"


@pytest.mark.parametrize("bad", ["", "   ", None])
def test_an_operator_hold_needs_a_reason(bad):
    """A hold with no account of why is the free-text blocker problem, not a
    fix for it — and the refusal must come before anything is written."""
    reg = registry(task("a"))
    expect_code(lambda: reg.operator_block("a", bad), "empty_task_field")
    assert reg.get("a").status == "pending"


def test_the_operator_pair_delegates_the_terminal_refusals():
    """`operator_block` adds guards, it does not re-implement `block`'s — and
    `block` itself is left untouched because its own caller
    (`cli._handle_parked_task`) blocks a task that is by definition
    in_progress, and fail-closes to loop_fatal on any refusal."""
    reg = registry(task("done"), task("gone"))
    reg.mark_completed("done")
    reg.retire("gone")
    expect_code(lambda: reg.operator_block("done", "why"), "task_completed")
    expect_code(lambda: reg.operator_block("gone", "why"), "task_retired")
    expect_code(lambda: reg.operator_unblock("gone"), "task_retired")
    expect_code(lambda: reg.operator_unblock("done"), "task_not_blocked")


def test_an_operator_hold_survives_persistence(tmp_path):
    """`hold_origin` round-trips like any other field, so a hold placed before
    a restart is still releasable after one — and the prose prefix survives
    alongside it for whoever reads the file."""
    from autoloop.tasks import HOLD_ORIGIN_OPERATOR, OPERATOR_HOLD_PREFIX

    store = TaskStore(tmp_path / "tasks.json")
    reg = registry(task("a"))
    reg.operator_block("a", "waiting on review")
    store.save(reg)

    loaded = store.load()
    assert loaded.get("a").hold_origin == HOLD_ORIGIN_OPERATOR
    assert loaded.get("a").blocked_reason.startswith(OPERATOR_HOLD_PREFIX)
    loaded.operator_unblock("a")
    assert loaded.state_of("a") is TaskState.READY


@pytest.mark.parametrize("stored_origin", [{}, {"hold_origin": None},
                                           {"hold_origin": "Operator "},
                                           {"hold_origin": "loop"}])
def test_a_stored_row_without_the_operator_marker_is_a_loop_quarantine(
    tmp_path, stored_origin
):
    """Backward compatibility, in the SAFE direction. A `tasks.json` written
    before `hold_origin` existed has no marker, and an unmarked row must read
    as a loop quarantine — releasable by `answer`, not by the inbox — rather
    than as a hold anything with write access to the inbox can clear. The
    reason text is deliberately the one that used to be trusted.

    A hand-edited `null` must load as `""` and not `None` (the next `str`
    operation on which would raise), and a near-miss must NOT be normalised
    into the marker: this field decides whether a quarantine can be released,
    so widening the match is the wrong direction to be lenient in."""
    from autoloop.tasks import HOLD_ORIGIN_OPERATOR, OPERATOR_HOLD_PREFIX

    path = tmp_path / "tasks.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "tasks": [{"id": "old", "title": "O", "description": "d",
                   "status": "blocked",
                   "blocked_reason": OPERATOR_HOLD_PREFIX + "from before the field",
                   **stored_origin}],
    }), encoding="utf-8")

    loaded = TaskStore(path).load()
    origin = loaded.get("old").hold_origin
    assert isinstance(origin, str), "a stored null must not load as None"
    assert origin != HOLD_ORIGIN_OPERATOR
    expect_code(lambda: loaded.operator_unblock("old"), "task_blocked_by_operator")
    assert loaded.state_of("old") is TaskState.BLOCKED_BY_OPERATOR


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


# ---- immediate priority edits, and the mutex that makes them safe ------------
#
# `os.replace` has always made a SAVE atomic. It does nothing for the
# read-modify-write around it, and that is where updates actually went missing:
# two writers each doing load -> mutate -> save interleave, and whichever save
# lands second discards the other's change. These pin the fine-grained mutex
# (`tasks.task_file_mutex`), the immediate write the dashboard uses, and the
# reconciliation that keeps a loop holding a stale registry in memory from
# writing an operator's edit back out.


def store_with_ledger(tmp_path, *tasks_):
    """A `TaskStore` wired to a ledger outside the "checkout", pre-populated.

    The ledger is what `apply_priority` attests into; a store without one
    refuses to write at all (an unattested change would park the loop as an
    escape if it landed mid-round), so every test of the immediate path needs
    it.
    """
    store = TaskStore(tmp_path / "state" / "tasks.json",
                      ledger=tmp_path / "outside" / "task-mutations.jsonl")
    store.save(registry(*tasks_))
    return store


def test_a_priority_write_persists_and_survives_a_reload(tmp_path):
    """The defect this replaces: the POST succeeded, the request sat in the
    inbox, and the page re-rendered the OLD number from tasks.json — so a save
    that worked looked exactly like one that did not."""
    store = store_with_ledger(tmp_path, task("t1", priority=3), task("t2"))

    written = store.apply_priority("t1", 2)

    assert written.priority == 2
    # A FRESH store, so nothing in memory can be answering: the value has to
    # have reached the file.
    assert TaskStore(store.path).load().get("t1").priority == 2


def test_the_returned_priority_is_read_back_from_disk_not_echoed(tmp_path):
    """Read-back is what proves persistence. Echoing the caller's number would
    report success identically whether or not anything landed."""
    store = store_with_ledger(tmp_path, task("t1", priority=3))
    reloaded = TaskStore(store.path).load()
    assert reloaded.get("t1").priority == 3  # baseline, before the write

    assert store.apply_priority("t1", 7).priority == 7
    assert json.loads(store.path.read_text(encoding="utf-8"))["tasks"][0]["priority"] == 7


def test_a_priority_write_leaves_every_other_field_alone(tmp_path):
    """One field, on an existing task. This path may not become a task editor:
    `approved_paths` is authorization, `status` is what the loop dispatches on,
    and `depends_on` reorders the graph."""
    original = Task(
        id="t1", title="T", description="d", priority=100,
        approved_paths=("autoloop/dashboard.py",), status="pending",
    )
    other = task("t0")
    store = TaskStore(tmp_path / "state" / "tasks.json",
                      ledger=tmp_path / "outside" / "ledger.jsonl")
    reg = TaskRegistry([other, original])
    reg.set_depends_on("t1", ["t0"])
    store.save(reg)
    before = json.loads(store.path.read_text(encoding="utf-8"))

    store.apply_priority("t1", 1)

    after = json.loads(store.path.read_text(encoding="utf-8"))
    assert after["tasks"][1]["priority"] == 1
    for row_before, row_after in zip(before["tasks"], after["tasks"]):
        assert {k: v for k, v in row_before.items() if k != "priority"} == \
               {k: v for k, v in row_after.items() if k != "priority"}
    assert after["tasks"][1]["status"] == "pending"
    assert after["tasks"][1]["approved_paths"] == ["autoloop/dashboard.py"]
    assert after["tasks"][1]["depends_on"] == ["t0"]


def test_a_priority_write_refuses_rather_than_creating_the_registry(tmp_path):
    """`tasks.json` is written by the loop on its first task-graph change
    (seeded from the tracked `seed_tasks.json`). Materialising it from a
    priority form would be a brand-new write path, not a priority edit."""
    store = TaskStore(tmp_path / "state" / "tasks.json",
                      ledger=tmp_path / "outside" / "ledger.jsonl")
    with pytest.raises(StateError):
        store.apply_priority("t1", 1)
    assert not store.path.exists(), "a refused edit must not create the registry"


def test_a_priority_write_without_a_ledger_refuses(tmp_path):
    """An unattested write into `.autoloop/` would be reported as an agent
    escape if it landed inside a detection window — i.e. a routine priority
    edit could stop the loop. Refuse instead."""
    store = TaskStore(tmp_path / "tasks.json")
    store.save(registry(task("t1", priority=3)))
    with pytest.raises(StateError):
        store.apply_priority("t1", 1)
    assert TaskStore(store.path).load().get("t1").priority == 3


def test_a_failed_priority_write_reports_rather_than_reverting_silently(tmp_path):
    """Two failures, and neither may look like "nothing happened": an unknown
    id raises in the registry's own words, and a ledger that cannot be written
    leaves the file untouched instead of producing a change nothing attests."""
    store = store_with_ledger(tmp_path, task("t1", priority=3))
    expect_code(lambda: store.apply_priority("nope", 1), "unknown_task")
    expect_code(lambda: store.apply_priority("t1", "high"), "bad_priority")
    assert TaskStore(store.path).load().get("t1").priority == 3

    # The ledger's own directory replaced by a file: the append fails, and the
    # task file must be untouched because the record is written FIRST.
    ledger = store.ledger.path
    ledger.parent.mkdir(parents=True, exist_ok=True)
    for stray in list(ledger.parent.iterdir()):
        stray.unlink()
    ledger.parent.rmdir()
    ledger.parent.write_text("not a directory", encoding="utf-8")
    with pytest.raises(OSError):
        store.apply_priority("t1", 1)
    assert TaskStore(store.path).load().get("t1").priority == 3


def test_a_save_adopts_an_operator_priority_written_under_it(tmp_path):
    """Stale memory is the other half of the lost update. A running loop holds
    its registry for a whole round, so an edit that lands mid-round is on disk
    and not in that object — and the next ordinary save (a completion, a park)
    would write the old number straight back over it."""
    store = store_with_ledger(tmp_path, task("t1", priority=100), task("t2"))
    loop_registry = store.load()  # what the loop holds for the rest of the round

    store.apply_priority("t1", 1)  # the operator steers, mid-round

    loop_registry.mark_completed("t2")
    store.save(loop_registry)

    assert TaskStore(store.path).load().get("t1").priority == 1, "the edit was overwritten"
    assert TaskStore(store.path).load().state_of("t2") is TaskState.COMPLETED
    # Adopted into the live object too, so THIS round's `next_ready()` already
    # follows the operator's ordering rather than waiting for a restart.
    assert loop_registry.get("t1").priority == 1


def test_a_deliberate_priority_change_beats_the_stored_value(tmp_path):
    """Reconciliation must not undo a change the loop itself just made — a
    drained inbox `priority` request is a deliberate write, so it takes
    precedence over whatever the file says (`inbox.apply_requests` is
    last-write-wins, and this is the same rule)."""
    from autoloop.inbox import apply_requests

    store = store_with_ledger(tmp_path, task("t1", priority=100))
    loop_registry = store.load()
    store.apply_priority("t1", 5)  # on disk, not in the loop's memory

    added, applied, refused = apply_requests(
        loop_registry, [{"kind": "priority", "id": "t1", "priority": 9}]
    )
    store.save(loop_registry)

    assert (added, refused) == ([], [])
    assert applied == ["t1 -> 9"]
    assert TaskStore(store.path).load().get("t1").priority == 9
    # And the override is spent: the NEXT save reconciles normally again.
    assert loop_registry.priority_overrides() == frozenset()


def test_reconciliation_fails_open_on_an_unreadable_task_file(tmp_path):
    """`save` records completions and quarantines. A save that started refusing
    because the file it is about to overwrite will not parse would be a far
    worse bug than a late priority."""
    store = TaskStore(tmp_path / "tasks.json")
    store.path.write_text("{ this is not json", encoding="utf-8")
    reg = registry(task("t1"))
    store.save(reg)  # must not raise
    assert TaskStore(store.path).load().get("t1").id == "t1"


# ---- what the attestation actually proves ------------------------------------
#
# The escape detector silences ONE change to `.autoloop/tasks.json`: an operator
# priority edit this store applied. The first version of that proof asked
# whether the observed after-digest was REACHABLE from the before-digest through
# ledger records, and reachability is a strictly weaker claim than "this is what
# happened". These pin the two gaps that opened — an intent from a failed write
# standing in for an outcome, and an intermediate state from a round trip
# staying attested forever — plus the path and window bindings that keep the
# chain about this file and this round.


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FailingWriteStore(TaskStore):
    """Ledger works, task-file write does not — the shape of a disk that fills
    between the two. Leaves a real intent on record for a write that never
    landed."""

    def _write_bytes(self, data):
        raise OSError("simulated task-file write failure")


def priority_payload(path, task_id, priority) -> bytes:
    """The exact bytes the store would have written for this priority — what an
    agent reproducing a state has to produce for any of these tests to be about
    the attestation rather than about a formatting difference."""
    registry_ = TaskStore(path).load()
    registry_.set_priority(task_id, priority)
    return TaskStore._serialize(registry_)


def test_a_successful_priority_write_attests_itself(tmp_path):
    """The positive control every negative below is measured against."""
    store = store_with_ledger(tmp_path, task("t1", priority=3))
    before_bytes = store.path.read_bytes()
    watermark = store.priority_edit_watermark()

    store.apply_priority("t1", 1)

    after = store.path.read_bytes()
    assert store.attested_priority_edit(
        before_bytes, sha(before_bytes), sha(after), watermark
    )


def test_a_priority_write_records_an_intent_and_then_a_completion(tmp_path):
    """Both phases, in that order. The intent first is what makes an unwritable
    ledger leave the task file alone; the completion after is what keeps that
    intent from becoming a licence."""
    store = store_with_ledger(tmp_path, task("t1", priority=3))
    before_bytes = store.path.read_bytes()
    watermark = store.priority_edit_watermark()

    store.apply_priority("t1", 1)

    written = store.ledger.read()[watermark:]
    assert [r["phase"] for r in written] == [LEDGER_PHASE_INTENT, LEDGER_PHASE_COMPLETE]
    # Same hop, announced and then confirmed: from the state the file was in to
    # the state it is in now.
    assert all(r["before"] == sha(before_bytes) for r in written)
    assert all(r["after"] == sha(store.path.read_bytes()) for r in written)
    assert all(r["ids"] == ["t1"] and r["kind"] == "priority" for r in written)


def test_an_intent_from_a_failed_write_is_not_an_edge(tmp_path):
    """The write was announced and then failed, so the state it named never
    existed on disk. An agent that writes exactly that state must not inherit
    the announcement."""
    store = store_with_ledger(tmp_path, task("t1", priority=3))
    before_bytes = store.path.read_bytes()
    watermark = store.priority_edit_watermark()

    failing = FailingWriteStore(store.path, ledger=store.ledger.path)
    with pytest.raises(OSError):
        failing.apply_priority("t1", 1)

    announced = store.ledger.read()[watermark:]
    assert [r["phase"] for r in announced] == [LEDGER_PHASE_INTENT]
    payload = priority_payload(store.path, "t1", 1)
    # Not vacuous: the agent's bytes are exactly the state the intent named.
    assert sha(payload) == announced[0]["after"]
    store.path.write_bytes(payload)

    assert not store.attested_priority_edit(
        before_bytes, sha(before_bytes), sha(payload), watermark
    )


def test_a_round_trip_leaves_no_intermediate_state_attested(tmp_path):
    """Two real edits that return the file to its baseline. The state in the
    middle is on record as a completed hop, and under a reachability test it
    stays authorized forever — so an agent writing it later is silently
    exempted. The chain says the window ended where it began."""
    store = store_with_ledger(tmp_path, task("t1", priority=3))
    baseline = store.path.read_bytes()
    watermark = store.priority_edit_watermark()

    store.apply_priority("t1", 1)
    intermediate = store.path.read_bytes()
    store.apply_priority("t1", 3)
    assert store.path.read_bytes() == baseline, "the round trip did not return the file"

    store.path.write_bytes(intermediate)  # the agent reproduces it

    assert not store.attested_priority_edit(
        baseline, sha(baseline), sha(intermediate), watermark
    )
    # And the chain itself says why: it is A -> B -> A, so B is a state passed
    # THROUGH rather than the state the window ended at.
    chain = store.ledger.completed_chain(
        sha(baseline), since=watermark, tasks_path=store.path
    )
    assert chain == [sha(baseline), sha(intermediate), sha(baseline)]


def test_a_completed_record_for_another_task_file_does_not_attest_this_one(tmp_path):
    """One ledger can serve more than one checkout (it lives beside
    `workers_root`). A record naming a different task file must not authorize a
    change to this one — and the second half of this test is what proves the
    refusal is the path binding rather than something else."""
    store = store_with_ledger(tmp_path, task("t1", priority=3))
    before_bytes = store.path.read_bytes()
    watermark = store.priority_edit_watermark()
    payload = priority_payload(store.path, "t1", 1)
    store.path.write_bytes(payload)

    store.ledger.record_complete(
        tasks_path=tmp_path / "elsewhere" / "tasks.json",
        before=sha(before_bytes), after=sha(payload), ids=("t1",),
    )
    assert not store.attested_priority_edit(
        before_bytes, sha(before_bytes), sha(payload), watermark
    )

    store.ledger.record_complete(
        tasks_path=store.path, before=sha(before_bytes), after=sha(payload), ids=("t1",),
    )
    assert store.attested_priority_edit(
        before_bytes, sha(before_bytes), sha(payload), watermark
    )


def test_the_same_task_file_spelled_differently_is_the_same_file(tmp_path):
    """Path binding must not become an accidental refusal. The dashboard builds
    its own store per request and may reach the file through a symlinked path —
    macOS resolves `/var` to `/private/var` for exactly this shape — and a
    record filed under one spelling but looked up under another proves nothing.
    `canonical_task_path` is what makes both sides agree."""
    store = store_with_ledger(tmp_path, task("t1", priority=3))
    before_bytes = store.path.read_bytes()
    watermark = store.priority_edit_watermark()

    link = tmp_path / "state-by-another-name"
    link.symlink_to(store.path.parent, target_is_directory=True)
    TaskStore(link / store.path.name, ledger=store.ledger.path).apply_priority("t1", 1)

    after = store.path.read_bytes()
    assert store.attested_priority_edit(
        before_bytes, sha(before_bytes), sha(after), watermark
    )


def test_the_watermark_keeps_an_earlier_edit_out_of_this_window(tmp_path):
    """The ledger is append-only and never pruned, so by the second round it
    already holds hops between states the file has since left. Those must not be
    part of this round's chain — walking from record 0 breaks on the first one
    and reports an ordinary operator edit as an escape."""
    store = store_with_ledger(tmp_path, task("t1", priority=3))
    store.apply_priority("t1", 5)  # an earlier window

    before_bytes = store.path.read_bytes()
    watermark = store.priority_edit_watermark()
    store.apply_priority("t1", 2)
    after = store.path.read_bytes()

    assert store.attested_priority_edit(
        before_bytes, sha(before_bytes), sha(after), watermark
    )
    assert store.ledger.completed_chain(
        sha(before_bytes), since=0, tasks_path=store.path
    ) is None


def test_a_state_later_than_the_observed_one_is_not_attested(tmp_path):
    """The residual of requiring the chain's TERMINAL state, pinned rather than
    left to be discovered. A second legitimate edit landing between the
    detector's after-snapshot and this check makes the round park. That is a
    spurious loop-fatal park an operator can read and recover from, traded for
    closing a laundering path that is silent by construction."""
    store = store_with_ledger(tmp_path, task("t1", priority=3))
    before_bytes = store.path.read_bytes()
    watermark = store.priority_edit_watermark()

    store.apply_priority("t1", 1)
    observed = sha(store.path.read_bytes())
    store.apply_priority("t1", 2)  # lands after the snapshot was taken

    assert not store.attested_priority_edit(
        before_bytes, sha(before_bytes), observed, watermark
    )


def test_the_window_is_captured_as_one_observation(tmp_path):
    """`capture_priority_window` reads the bytes and the watermark under a
    single mutex hold. Read separately they can straddle an in-flight edit —
    baseline from before it, watermark from after it — and the chain then finds
    an empty window and parks a benign operator write."""
    store = store_with_ledger(tmp_path, task("t1", priority=3))

    baseline, watermark = store.capture_priority_window()

    assert baseline == store.path.read_bytes()
    assert watermark == store.priority_edit_watermark()
    store.apply_priority("t1", 1)
    assert store.attested_priority_edit(
        baseline, sha(baseline), sha(store.path.read_bytes()), watermark
    )


# ---- two real processes, one task file --------------------------------------
#
# REAL subprocesses, not monkeypatched sleeps in one interpreter: the mutex is
# a `flock` on a lock file, and an in-process test can only exercise the
# `threading.RLock` half of it. The child is given the package root as
# PYTHONPATH so it imports this source tree whatever pytest's cwd is.

#: The OPERATOR side of the race, spelled out: exactly the sequence
#: `TaskStore.apply_priority` runs internally (take the mutex, load, mutate,
#: write) with a deliberate pause inserted between the load and the write, so
#: the window the mutex has to cover is deterministic instead of a coin flip.
#: The pause lives HERE and never in production code.
_PRIORITY_WRITER = """
import sys, time
from pathlib import Path
from autoloop.tasks import TaskStore

tasks_path, loaded_flag, hold, task_id, value = sys.argv[1:6]
store = TaskStore(tasks_path)
with store.lock():
    registry = store.load()
    Path(loaded_flag).write_text("loaded")
    time.sleep(float(hold))
    registry.set_priority(task_id, int(value))
    store.save(registry)
"""

#: A real process holding the RUN-level lock, the way a live `run --continuous`
#: holds it for its whole run. It never lets go until told to, so a test can
#: prove an immediate write does not wait for it.
_LOOP_LOCK_HOLDER = """
import sys, time
from pathlib import Path
from autoloop.lock import LoopLock

state_dir, held_flag, release_flag = sys.argv[1:4]
lock = LoopLock(Path(state_dir)).acquire()
Path(held_flag).write_text("held")
while not Path(release_flag).exists():
    time.sleep(0.02)
lock.release()
"""


def spawn(script, *args):
    return subprocess.Popen(
        [sys.executable, "-c", script, *[str(a) for a in args]],
        env={**os.environ, "PYTHONPATH": str(PACKAGE_ROOT)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def reap(child, timeout=30.0):
    """Wait for `child` and return its exit code, never raising.

    Called from a `finally`, where an assertion would replace the body's real
    failure with a confusing subprocess one — so the code is returned and
    asserted after the block instead.
    """
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:  # pragma: no cover - a wedged child
        child.kill()
        return child.wait(timeout=timeout)


def wait_for(path, child, timeout=20.0):
    """Block until `path` appears, failing with the child's own stderr if it
    dies first — a child that crashed on an import would otherwise show up as
    an unexplained timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if Path(path).exists():
            return
        if child.poll() is not None:
            raise AssertionError(f"child exited early: {child.communicate()}")
        time.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


def test_two_concurrent_writers_do_not_lose_an_update(tmp_path):
    """The whole reason a fine-grained mutex exists, with a REAL competing
    writer rather than a mocked one.

    The child models the operator's immediate priority edit: it holds the mutex
    across load -> mutate -> save. This process models the loop: it reads, marks
    a task completed, and saves. Without the mutex the loop's save lands in the
    middle of the child's read-modify-write and the child then writes its
    pre-completion snapshot back — the completion is gone, which is exactly the
    loss the task brief calls far worse than a late priority.

    Delete the `with self.lock()` from `TaskStore.save` / `apply_priority` and
    this test fails on the completion assertion.
    """
    store = store_with_ledger(tmp_path, task("t1", priority=100), task("t2"))
    loaded_flag = tmp_path / "child-loaded"

    child = spawn(_PRIORITY_WRITER, store.path, loaded_flag, 0.6, "t1", 2)
    try:
        wait_for(loaded_flag, child)
        # The child has read the file and is holding the mutex. Everything this
        # process does from here has to queue behind it.
        loop_registry = store.load()
        loop_registry.mark_completed("t2")
        started = time.monotonic()
        store.save(loop_registry)
        waited = time.monotonic() - started
    finally:
        # Reaped, never ASSERTED here: an assertion inside `finally` replaces a
        # real failure from the body with a confusing subprocess one.
        code = reap(child)

    assert code == 0, "the competing writer failed"
    assert waited >= 0.2, "the save did not wait for the other writer's mutex"
    persisted = TaskStore(store.path).load()
    assert persisted.get("t1").priority == 2, "the operator's edit was lost"
    assert persisted.state_of("t2") is TaskState.COMPLETED, "the completion was lost"


def test_a_priority_write_succeeds_while_the_loop_lock_is_held(tmp_path):
    """The case that motivates the whole design. `LoopLock` is held for the
    ENTIRE run — that is why `answer` and `release` refuse while the loop is up
    — so an immediate write that waited for it would be waiting for the loop to
    stop, which is the opposite of immediate. It must not block and must not
    fail."""
    store = store_with_ledger(tmp_path, task("t1", priority=100))
    state_dir = store.path.parent
    held_flag, release_flag = tmp_path / "lock-held", tmp_path / "lock-release"

    child = spawn(_LOOP_LOCK_HOLDER, state_dir, held_flag, release_flag)
    try:
        wait_for(held_flag, child)
        from autoloop.lock import LoopLock

        # Not merely a file on disk: a LIVE foreign process owns it, so the
        # loop's own lock acquisition genuinely refuses.
        with pytest.raises(LockHeldError):
            LoopLock(state_dir).acquire()

        started = time.monotonic()
        written = store.apply_priority("t1", 1)
        elapsed = time.monotonic() - started
    finally:
        release_flag.write_text("go")
        code = reap(child)

    assert code == 0, "the lock holder failed"
    assert written.priority == 1
    assert elapsed < 5.0, f"the write waited {elapsed:.1f}s — it must not block on LoopLock"
    assert TaskStore(store.path).load().get("t1").priority == 1


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


def test_a_task_depending_on_a_retired_one_is_blocked_with_no_way_out():
    """WHY the strand refusal exists, pinned on a graph that ALREADY holds the
    shape. Retirement does not satisfy a dependency — `state_of` counts only
    `completed` — and there is no command that clears it: `unblock` wants a
    quarantine, `release` wants an in-progress task, and `retire` has no
    reverse.

    Built through `from_dict` rather than through `retire`, because since
    retire-01 `retire` refuses to create it. That is exactly how a `tasks.json`
    written before retire-01 holds it: the load path deliberately bypasses
    `add_many` and does not re-validate a stored graph."""
    reg = TaskRegistry.from_dict({
        "schema_version": 1,
        "tasks": [
            {"id": "a", "title": "A", "description": "d", "status": "retired",
             "superseded_by": ["a2"]},
            {"id": "b", "title": "B", "description": "d", "status": "pending",
             "depends_on": ["a"]},
        ],
    })
    assert reg.state_of("b") is TaskState.BLOCKED
    expect_code(lambda: reg.unblock("b"), "task_not_blocked")
    expect_code(lambda: reg.release("b"), "task_not_in_progress")
    # The one supported way back: re-point it, which is what `retire` now does
    # for the operator in the same operation rather than leaving it to them.
    reg.set_depends_on("b", [])
    assert reg.state_of("b") is TaskState.READY


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


# ---- retiring must not permanently strand a dependent ------------------------
#
# Measured 2026-08-20: an operator asked for `roadmap-01` to be retired. Four
# tasks named it directly and 21 waited on it transitively — the entire ingest
# line. `state_of` counts a dependency satisfied ONLY when it is `completed`, a
# retirement is written once with no reverse, and no supported command returns
# a task blocked on one (`answer` needs an open blocker, `release` needs an
# in-progress task, and there is no `unblock`). All 21 would have become
# permanently unreachable. A human caught it before the command ran; nothing in
# the code would have stopped it.


def test_retiring_a_task_with_a_pending_dependent_is_refused_and_names_it():
    reg = registry(task("roadmap-01"), task("ingest-01", deps=["roadmap-01"]))

    with pytest.raises(TaskGraphError) as excinfo:
        reg.retire("roadmap-01")

    assert excinfo.value.code == "task_would_strand_dependents"
    assert "ingest-01" in str(excinfo.value)
    assert reg.get("roadmap-01").status == "pending", "a refused retirement writes nothing"
    assert reg.get("ingest-01").depends_on == ("roadmap-01",)


def test_the_refusal_names_every_dependent_that_would_be_stranded():
    """"Names it" is not "names one of them". An operator who has to re-run the
    command to discover the next id cannot judge the decision at all — and the
    decision is the whole point of refusing instead of repairing."""
    reg = registry(
        task("roadmap-01"),
        *[task(f"ingest-0{n}", deps=["roadmap-01"]) for n in (1, 2, 3, 8)],
    )

    with pytest.raises(TaskGraphError) as excinfo:
        reg.retire("roadmap-01")

    message = str(excinfo.value)
    for dependent in ("ingest-01", "ingest-02", "ingest-03", "ingest-08"):
        assert dependent in message


def test_the_refusal_reports_the_transitive_count_beside_the_direct_one():
    """4 direct reads very differently from 21 in total, and the operator's
    decision turns on the second number."""
    reg = registry(
        task("roadmap-01"),
        task("ingest-01", deps=["roadmap-01"]),
        task("ingest-02", deps=["ingest-01"]),
        task("ingest-03", deps=["ingest-02"]),
    )

    report = reg.stranded_dependents("roadmap-01")
    assert report.direct == ("ingest-01",)
    assert report.transitive == ("ingest-01", "ingest-02", "ingest-03")

    with pytest.raises(TaskGraphError) as excinfo:
        reg.retire("roadmap-01")
    message = str(excinfo.value)
    assert "1 dependent (ingest-01)" in message
    assert "3 tasks blocked in total" in message


def test_the_total_is_reported_even_when_nothing_is_behind_the_direct_ones():
    """Printed ALWAYS, not only when it is larger. A count a reader sees
    sometimes and not others cannot be told apart from a message that does not
    report it — and "the same as the direct count" is a fact, not an omission."""
    reg = registry(task("old-01"), task("dep-01", deps=["old-01"]))

    with pytest.raises(TaskGraphError) as excinfo:
        reg.retire("old-01")

    assert "1 dependent (dep-01); 1 task blocked in total" in str(excinfo.value)


def test_a_self_edge_on_a_stored_graph_is_refused_at_load_not_at_retirement():
    """First, the reachability, because the guard below is easy to mis-document
    as covering a hand-edited file. It does not: `from_dict` runs
    `_check_acyclic` over the WHOLE stored graph, and a self-edge is a one-node
    cycle, so such a `tasks.json` never loads at all."""
    with pytest.raises(TaskGraphError) as excinfo:
        TaskRegistry.from_dict({
            "schema_version": 1,
            "tasks": [
                {"id": "old-01", "title": "O", "description": "d", "status": "pending",
                 "depends_on": ["old-01"]},
            ],
        })

    assert excinfo.value.code == "dependency_cycle"
    # The other two routes in, for completeness: neither can produce one either.
    expect_code(lambda: registry(task("old-01", deps=["old-01"])), "dependency_cycle")
    reg = registry(task("old-01"))
    expect_code(lambda: reg.set_depends_on("old-01", ["old-01"]), "dependency_cycle")


def corrupt_self_edge(reg, task_id):
    """Give `task_id` a dependency on itself, by writing the dataclass field.

    The ONLY way to build this shape: every route into a registry refuses it
    (test above), so a test that tried to load one would raise before it could
    assert anything — which is exactly what an earlier version of the tests
    below did. What is being pinned is defence in depth against an in-memory
    corruption, and against the day `from_dict` is relaxed to tolerate a stored
    cycle the way it already tolerates a dangling `depends_on`. Naming that
    honestly here is the point: a guard whose test cannot reach it is not a
    guard, and a comment claiming a route that does not exist is worse.
    """
    live = reg.get(task_id)
    live.depends_on = (*live.depends_on, task_id)
    return reg


def test_the_task_being_retired_is_never_its_own_stranded_dependent():
    """Without the guard the refusal would name the task as a dependent of
    itself — and the rewrite would then edit the row it is retiring."""
    reg = corrupt_self_edge(registry(task("old-01")), "old-01")

    assert reg.stranded_dependents("old-01") == StrandReport()
    reg.retire("old-01", reason="stale")

    assert reg.state_of("old-01") is TaskState.RETIRED
    assert reg.get("old-01").depends_on == ("old-01",), "its own record is untouched"


def test_a_completed_or_retired_dependent_cannot_be_stranded():
    """Count only REAL stranding. A dependent that already finished, or that is
    itself a retirement record, is not waiting on anything and will never be
    dispatched again — refusing on its behalf would block retirements that harm
    nobody.

    Built through `from_dict`, because `mark_completed` cannot produce it: the
    shape comes from a stored graph, which the load path deliberately does not
    re-validate."""
    reg = TaskRegistry.from_dict({
        "schema_version": 1,
        "tasks": [
            {"id": "roadmap-01", "title": "R", "description": "d", "status": "pending"},
            {"id": "done-01", "title": "D", "description": "d", "status": "completed",
             "depends_on": ["roadmap-01"]},
            {"id": "gone-01", "title": "G", "description": "d", "status": "retired",
             "depends_on": ["roadmap-01"]},
        ],
    })

    assert reg.stranded_dependents("roadmap-01") == StrandReport()
    reg.retire("roadmap-01", reason="stale")

    assert reg.state_of("roadmap-01") is TaskState.RETIRED


def test_a_terminal_dependent_is_not_descended_through_either():
    """`done-01` is satisfied, so `behind-01` is NOT waiting on the retirement —
    counting it would inflate the number the operator decides on."""
    reg = TaskRegistry.from_dict({
        "schema_version": 1,
        "tasks": [
            {"id": "roadmap-01", "title": "R", "description": "d", "status": "pending"},
            {"id": "done-01", "title": "D", "description": "d", "status": "completed",
             "depends_on": ["roadmap-01"]},
            {"id": "behind-01", "title": "B", "description": "d", "status": "pending",
             "depends_on": ["done-01"]},
        ],
    })

    assert reg.stranded_dependents("roadmap-01").transitive == ()


def test_a_quarantined_dependent_still_counts_as_stranded():
    """`blocked` is a question waiting for an operator, not a terminal record.
    Answering it puts the task back in the queue — where it would then wait on
    a retired id forever."""
    reg = registry(task("old-01"), task("dep-01", deps=["old-01"]))
    reg.block("dep-01", "attempt ceiling")

    expect_code(lambda: reg.retire("old-01"), "task_would_strand_dependents")


def test_a_task_with_no_dependents_retires_exactly_as_it_did_before():
    """The ordinary case, and the one this must not change: `dash-01` went
    stale with nothing waiting on it and no successor to name."""
    reg = registry(task("dash-01"), task("unrelated-01"))
    reg.mark_in_progress("dash-01")

    retired = reg.retire("dash-01", reason="stale since 2026-08-03")

    assert reg.state_of("dash-01") is TaskState.RETIRED
    assert retired.superseded_by == ()
    assert retired.blocked_reason == "stale since 2026-08-03"
    assert reg.get("unrelated-01").depends_on == ()


def test_asking_about_an_unknown_id_is_refused_rather_than_answered_empty():
    """The fail-open shape this whole check exists to avoid: a typo that
    answers "nothing depends on it" reads as a safe retirement."""
    expect_code(lambda: registry(task("a-01")).stranded_dependents("ghost"), "task_unknown")


# ---- lifting the refusal: a live successor, or an explicit rewrite ------------
#
# Supersession satisfaction is DIRECT — the successor id replaces the retired
# one in each affected dependent, in the same operation. Lifting the refusal
# without that rewrite would be a lie: the dependents would still name a retired
# id and still never dispatch, which is the defect, not the fix.


def test_a_live_successor_lifts_the_refusal_and_re_points_every_dependent():
    reg = registry(
        task("roadmap-01"),
        task("roadmap-02"),
        task("ingest-01", deps=["roadmap-01"]),
        task("ingest-02", deps=["roadmap-01", "roadmap-02"]),
    )

    reg.retire("roadmap-01", superseded_by=["roadmap-02"])

    assert reg.state_of("roadmap-01") is TaskState.RETIRED
    assert reg.get("roadmap-01").superseded_by == ("roadmap-02",), "the record survives"
    assert reg.get("ingest-01").depends_on == ("roadmap-02",)
    assert reg.get("ingest-02").depends_on == ("roadmap-02",), "not ('roadmap-02',) twice"
    # The point of the whole exercise: the dependency is SATISFIABLE now.
    assert reg.state_of("ingest-01") is TaskState.BLOCKED
    reg.mark_completed("roadmap-02")
    assert reg.state_of("ingest-01") is TaskState.READY


def test_a_completed_successor_satisfies_the_dependency_immediately():
    reg = registry(task("old-01"), task("new-01"), task("dep-01", deps=["old-01"]))
    reg.mark_completed("new-01")

    reg.retire("old-01", superseded_by=["new-01"])

    assert reg.get("dep-01").depends_on == ("new-01",)
    assert reg.state_of("dep-01") is TaskState.READY


def test_a_successor_that_is_not_a_task_yet_cannot_satisfy_a_dependency():
    """A supersession is a record, not a schedule — brw-06 was retired into
    brw-07/brw-08 before either was planned, and that stays legal for a task
    nothing depends on. But nothing can WAIT on an id that is not in the graph,
    so it does not lift the refusal, and the message says which id and why."""
    reg = registry(task("brw-02"), task("brw-09", deps=["brw-02"]))

    with pytest.raises(TaskGraphError) as excinfo:
        reg.retire("brw-02", superseded_by=["brw-06"])

    assert excinfo.value.code == "task_would_strand_dependents"
    assert "brw-06 is not a task in this graph" in str(excinfo.value)
    assert reg.get("brw-02").status == "pending"


def test_a_successor_that_is_itself_retired_cannot_satisfy_a_dependency():
    reg = registry(task("a-01"), task("b-01"), task("c-01", deps=["a-01"]))
    reg.retire("b-01", reason="also stale")

    with pytest.raises(TaskGraphError) as excinfo:
        reg.retire("a-01", superseded_by=["b-01"])

    assert excinfo.value.code == "task_would_strand_dependents"
    assert "b-01 is itself retired" in str(excinfo.value)


def test_a_partly_planned_successor_list_refuses_without_the_flag():
    """`--superseded-by brw-07 brw-08` with only brw-07 planned. Replacing with
    brw-07 alone would silently re-point the dependents at half a continuation,
    so the AUTOMATIC lift demands that every named successor be live; the flag
    is how an operator says they meant it. The record keeps both ids either
    way — only what a dependent can wait on is narrowed."""
    reg = registry(task("brw-06"), task("brw-07"), task("dep-01", deps=["brw-06"]))

    expect_code(
        lambda: reg.retire("brw-06", superseded_by=["brw-07", "brw-08"]),
        "task_would_strand_dependents",
    )
    assert reg.get("brw-06").status == "pending"

    reg.retire("brw-06", superseded_by=["brw-07", "brw-08"], rewrite_dependents=True)

    assert reg.get("brw-06").superseded_by == ("brw-07", "brw-08")
    assert reg.get("dep-01").depends_on == ("brw-07",)


def test_the_rewrite_flag_drops_the_dependency_when_nothing_replaces_the_task():
    """The stale case: no successor exists to wait on, so the edge goes. This
    is the branch that needs an EXPLICIT opt-in — it is the one where the loop
    has no evidence at all about what the dependents should wait for instead."""
    reg = registry(
        task("dash-01"),
        task("dep-01", deps=["dash-01"]),
        task("dep-02", deps=["dash-01"]),
    )

    reg.retire("dash-01", reason="stale", rewrite_dependents=True)

    assert reg.state_of("dash-01") is TaskState.RETIRED
    assert reg.get("dep-01").depends_on == ()
    assert reg.get("dep-02").depends_on == ()
    assert reg.state_of("dep-01") is TaskState.READY
    assert reg.state_of("dep-02") is TaskState.READY


def test_only_the_direct_dependents_are_rewritten():
    """The transitive ones never named the retired task. They unblock by
    themselves once the direct ones can run, and rewriting them would edit
    dependencies the retirement says nothing about."""
    reg = registry(
        task("roadmap-01"),
        task("roadmap-02"),
        task("ingest-01", deps=["roadmap-01"]),
        task("ingest-02", deps=["ingest-01"]),
    )

    reg.retire("roadmap-01", superseded_by=["roadmap-02"])

    assert reg.get("ingest-01").depends_on == ("roadmap-02",)
    assert reg.get("ingest-02").depends_on == ("ingest-01",), "untouched"


def test_a_successor_that_is_itself_a_dependent_loses_the_edge():
    """Retire A into B where B already waits on A. B CONTINUES the work; it does
    not wait on itself, and `_validate_depends_on` refuses a self-edge."""
    reg = registry(task("brw-02"), task("brw-06", deps=["brw-02"]))

    reg.retire("brw-02", superseded_by=["brw-06"])

    assert reg.get("brw-06").depends_on == ()
    assert reg.state_of("brw-06") is TaskState.READY


def test_a_rewrite_that_would_build_a_cycle_leaves_nothing_applied():
    """The cycle is only visible in the WHOLE candidate graph, and it is found
    after every dependent has already been planned — so this is the case that
    proves the rewrites are validated before ANY of them is written. Half of
    this applied would be a graph no command could repair."""
    reg = registry(
        task("old-01"),
        task("dep-01", deps=["old-01"]),
        task("new-01", deps=["dep-01"]),
        task("dep-02", deps=["old-01"]),
    )

    expect_code(lambda: reg.retire("old-01", superseded_by=["new-01"]), "dependency_cycle")

    assert reg.get("old-01").status == "pending", "the retirement did not land either"
    assert reg.get("old-01").superseded_by == ()
    assert reg.get("dep-01").depends_on == ("old-01",)
    assert reg.get("dep-02").depends_on == ("old-01",)


# ---- the subject's own self-edge is not a cycle this retirement may be vetoed by
#
# `stranded_dependents` refuses to count the subject as its own dependent (test
# above), which means the rewrite pass never re-points that row — so the edge
# rides into the whole-candidate cycle check untouched and refuses an otherwise
# valid retirement as `dependency_cycle: old-01 -> old-01`, naming the
# retirement itself as the loop. Half a guard is worse than none here: it turns
# "reported as its own dependent" into "refused for a cycle no command removes".
#
# Every case below builds the corruption IN MEMORY, because no route into a
# registry produces it (see `corrupt_self_edge` and the load test above). The
# four split the carve-out (first two) from what it must NOT weaken: a
# self-edge on any other task still refuses, and a cycle that runs THROUGH the
# subject is dissolved by the rewrite rather than exempted from the check.


def test_a_self_edge_on_the_subject_does_not_veto_its_retirement():
    """The path the guard's first half does not reach. `test_the_task_being_
    retired_is_never_its_own_stranded_dependent` retires a task with NO
    dependents, so `_retirement_rewrites` returns before the cycle check runs;
    this one has a real dependent, so the check runs on a candidate graph that
    still holds the subject's row."""
    reg = corrupt_self_edge(
        registry(task("old-01"), task("new-01"), task("dep-01", deps=["old-01"])),
        "old-01",
    )

    reg.retire("old-01", superseded_by=["new-01"])

    assert reg.state_of("old-01") is TaskState.RETIRED
    assert reg.get("old-01").depends_on == ("old-01",), "its own record is untouched"
    assert reg.get("dep-01").depends_on == ("new-01",)
    assert reg.state_of("dep-01") is TaskState.BLOCKED
    reg.mark_completed("new-01")
    assert reg.state_of("dep-01") is TaskState.READY


def test_the_self_edge_carve_out_covers_the_rewrite_flag_too():
    """Same row, the other route out of the refusal. A carve-out that only held
    for `--superseded-by` would leave `--rewrite-dependents` refusing with a
    cycle the operator cannot act on."""
    reg = corrupt_self_edge(
        registry(task("old-01"), task("dep-01", deps=["old-01"])), "old-01"
    )

    reg.retire("old-01", reason="stale", rewrite_dependents=True)

    assert reg.state_of("old-01") is TaskState.RETIRED
    assert reg.get("old-01").depends_on == ("old-01",), "its own record is untouched"
    assert reg.get("dep-01").depends_on == ()
    assert reg.state_of("dep-01") is TaskState.READY


def test_a_self_edge_on_a_task_the_retirement_does_not_touch_still_refuses():
    """The fail-closed half, and the bound on the carve-out: the exemption is
    the SUBJECT's self-edge and nothing else. `loop-01` is a corruption this
    retirement was not asked about and cannot repair, so the whole operation
    refuses and applies nothing — including the retirement."""
    reg = corrupt_self_edge(
        registry(
            task("old-01"),
            task("new-01"),
            task("dep-01", deps=["old-01"]),
            task("loop-01"),
        ),
        "loop-01",
    )

    expect_code(lambda: reg.retire("old-01", superseded_by=["new-01"]), "dependency_cycle")

    assert reg.get("old-01").status == "pending"
    assert reg.get("old-01").superseded_by == ()
    assert reg.get("dep-01").depends_on == ("old-01",)


def test_a_two_task_cycle_through_the_subject_is_dissolved_by_the_rewrite():
    """Not the carve-out — the rewrite. `old-01 -> dep-01 -> old-01` is a real
    cycle, but `dep-01` is a stranded dependent, so the substitution takes
    `old-01` out of its `depends_on` and the loop is gone from the candidate
    graph before it is checked. Proves the exemption above is not what lets a
    cycle involving the subject through."""
    reg = registry(task("old-01"), task("dep-01", deps=["old-01"]), task("new-01"))
    # The other half of a two-node cycle, written the same way and for the same
    # reason as `corrupt_self_edge`: `set_depends_on` would refuse it.
    reg.get("old-01").depends_on = ("dep-01",)

    assert reg.stranded_dependents("old-01").direct == ("dep-01",)
    reg.retire("old-01", superseded_by=["new-01"])

    assert reg.get("dep-01").depends_on == ("new-01",)
    assert reg.get("old-01").depends_on == ("dep-01",), "its own record is untouched"


def test_an_in_progress_dependent_refuses_the_whole_operation():
    """Its `depends_on` is what the running dispatch is being judged against —
    rewriting it mid-round is exactly the strand `_refuse_immutable` exists to
    prevent (`state_of` reads dependencies before the in-progress branch, so
    the round would finish and then be refused both completion and release).
    Neither route may force it."""
    reg = TaskRegistry.from_dict({
        "schema_version": 1,
        "tasks": [
            {"id": "old-01", "title": "O", "description": "d", "status": "pending"},
            {"id": "new-01", "title": "N", "description": "d", "status": "pending"},
            {"id": "dep-01", "title": "D", "description": "d", "status": "in_progress",
             "depends_on": ["old-01"]},
        ],
    })

    assert reg.stranded_dependents("old-01").in_progress == ("dep-01",)
    expect_code(lambda: reg.retire("old-01", superseded_by=["new-01"]), "task_in_progress")
    expect_code(
        lambda: reg.retire("old-01", superseded_by=["new-01"], rewrite_dependents=True),
        "task_in_progress",
    )
    assert reg.get("old-01").status == "pending"
    assert reg.get("dep-01").depends_on == ("old-01",)


def test_the_strand_check_survives_a_dependency_naming_a_task_that_is_gone():
    """`state_of` raises `KeyError` on this shape and `from_dict` tolerates it,
    so the check reads STORED statuses and walks the edges backwards. A guard
    that crashed here — or that was wrapped in a `try: … except: continue` —
    would fail OPEN on precisely the graph it should refuse.

    The rewrite path fails CLOSED on the same graph: it cannot write a
    `depends_on` that still names a task nobody has, so it refuses and applies
    nothing rather than persisting a half-repaired row."""
    reg = TaskRegistry.from_dict({
        "schema_version": 1,
        "tasks": [
            {"id": "old-01", "title": "O", "description": "d", "status": "pending"},
            {"id": "dep-01", "title": "D", "description": "d", "status": "pending",
             "depends_on": ["old-01", "vanished-01"]},
        ],
    })

    with pytest.raises(TaskGraphError) as excinfo:
        reg.retire("old-01")
    assert excinfo.value.code == "task_would_strand_dependents"
    assert "dep-01" in str(excinfo.value)

    expect_code(lambda: reg.retire("old-01", rewrite_dependents=True), "unknown_dependency")
    assert reg.get("old-01").status == "pending"
    assert reg.get("dep-01").depends_on == ("old-01", "vanished-01")


def test_the_rewrite_flag_on_an_already_retired_task_is_refused_not_ignored():
    """A flag that quietly does nothing is the same failure class as a check
    that silently passes: the operator reads "nothing changed" and believes the
    strand they came to clear was cleared. Retirement stays written-once, so
    the route for a strand an EARLIER retirement left is `set_depends_on`."""
    reg = TaskRegistry.from_dict({
        "schema_version": 1,
        "tasks": [
            {"id": "old-01", "title": "O", "description": "d", "status": "retired",
             "superseded_by": ["new-01"]},
            {"id": "new-01", "title": "N", "description": "d", "status": "pending"},
            {"id": "dep-01", "title": "D", "description": "d", "status": "pending",
             "depends_on": ["old-01"]},
        ],
    })

    expect_code(lambda: reg.retire("old-01", rewrite_dependents=True), "task_already_retired")
    assert reg.get("dep-01").depends_on == ("old-01",)

    # The bare repeat is still the no-op it has always been.
    reg.retire("old-01")
    assert reg.get("old-01").superseded_by == ("new-01",)


def test_the_rewrite_and_the_retirement_persist_as_one_save(tmp_path):
    """One registry mutation, one `save`. Both halves land together or the
    refusal happened before either was written — there is no window in which
    the file holds a retirement whose dependents were not re-pointed."""
    store = TaskStore(tmp_path / "tasks.json")
    reg = registry(task("old-01"), task("new-01"), task("dep-01", deps=["old-01"]))
    reg.retire("old-01", superseded_by=["new-01"])

    store.save(reg)

    loaded = store.load()
    assert loaded.state_of("old-01") is TaskState.RETIRED
    assert loaded.get("old-01").superseded_by == ("new-01",)
    assert loaded.get("dep-01").depends_on == ("new-01",)


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
