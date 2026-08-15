"""Review-context builder: integrity stamp values, git summary, changed-file
parsing, previous decision/task, roadmap and validation summaries, and the
counts the two scheduling preferences in CONTRACT_INSTRUCTIONS read — in-flight
work, and how much of the roadmap is ready."""

import hashlib

import pytest

from autoloop.context import (
    IN_FLIGHT_LABEL,
    ROADMAP_LABEL,
    build_context,
    render_context,
)
from autoloop.contract import AUDIT_VS_READY_PREFERENCE, NEXT_WORK_PREFERENCE
from autoloop.errors import StateCorruptError
from autoloop.state import LoopState
from autoloop.tasks import Task, TaskRegistry
from autoloop.worktask import TaskExecution, TaskExecutionStore

URL = "https://chatgpt.com/c/test"


class FakeGit:
    def __init__(self):
        self.head = "a" * 40
        self.branch = "feature/x"
        self.porcelain = [" M lexy-app/a.py", "?? new_file.py", "R  old.py -> new.py"]

    def head_sha(self):
        return self.head

    def current_branch(self):
        return self.branch

    def dirty_files(self):
        return list(self.porcelain)


def make_state(**kw):
    state = LoopState.new(URL)
    for key, value in kw.items():
        setattr(state, key, value)
    return state


def make_registry():
    return TaskRegistry([Task(id="t1", title="First task", description="d")])


def test_integrity_stamp_values():
    payload = "the report body"
    ctx = build_context(make_state(), FakeGit(), make_registry(), "alr-x-0001", payload)
    assert ctx.request_id == "alr-x-0001"
    assert ctx.head_sha == "a" * 40
    assert ctx.base_sha == "(none)"  # nothing reviewed yet
    assert ctx.report_sha256 == hashlib.sha256(payload.encode()).hexdigest()
    assert ctx.timestamp  # stamped


def test_base_sha_uses_reviewed_commit():
    state = make_state(reviewed_commit="b" * 40)
    ctx = build_context(state, FakeGit(), make_registry(), "r", "p")
    assert ctx.base_sha == "b" * 40


def test_changed_files_parsed_from_porcelain():
    ctx = build_context(make_state(), FakeGit(), make_registry(), "r", "p")
    assert ctx.changed_files == ("lexy-app/a.py", "new_file.py", "new.py")
    assert ctx.dirty_count == 3


def test_previous_decision_and_task():
    state = make_state(
        last_decision="implement",
        current_task={"task_id": "t1", "title": "First task", "decision": "implement"},
        last_validation="ruff clean; 12 tests passed",
    )
    ctx = build_context(state, FakeGit(), make_registry(), "r", "p")
    assert ctx.previous_decision == "implement"
    assert "t1" in ctx.previous_task
    assert "First task" in ctx.previous_task
    assert ctx.validation_summary == "ruff clean; 12 tests passed"


def test_defaults_when_nothing_happened_yet():
    ctx = build_context(make_state(), FakeGit(), make_registry(), "r", "p")
    assert ctx.previous_decision == "(none)"
    assert ctx.previous_task == "(none)"
    assert ctx.validation_summary == "(none)"


def test_roadmap_status_from_registry():
    ctx = build_context(make_state(), FakeGit(), make_registry(), "r", "p")
    assert "next ready: t1" in ctx.roadmap_status


def test_render_contains_all_labels():
    ctx = build_context(make_state(), FakeGit(), make_registry(), "alr-x-0001", "p")
    block = render_context(ctx)
    for label in (
        "request_id: alr-x-0001",
        "timestamp:",
        f"head_sha: {'a' * 40}",
        "base_sha:",
        "report_sha256:",
        "branch: feature/x",
        "changed_files:",
        "previous_decision:",
        "previous_task:",
        "validation:",
        "roadmap:",
        f"{IN_FLIGHT_LABEL}:",
    ):
        assert label in block


def test_render_truncates_long_file_lists():
    git = FakeGit()
    git.porcelain = [f" M f{i}.py" for i in range(50)]
    ctx = build_context(make_state(), git, make_registry(), "r", "p")
    block = render_context(ctx, max_files=40)
    assert "10 more" in block


# ---- in-flight counts -------------------------------------------------------
#
# CONTRACT_INSTRUCTIONS tells the reviewer to prefer finishing work already in
# flight over starting fresh work. That preference is only checkable if the
# CONTEXT block says how much IS in flight, so these tests own the numbers.


def busy_registry():
    """Three tasks: one completed, one in progress, one ready."""
    reg = TaskRegistry(
        [
            Task(id=tid, title=f"Task {tid}", description="d")
            for tid in ("done", "wip", "next")
        ]
    )
    reg.mark_completed("done")
    reg.mark_in_progress("wip")
    return reg


def store_with(tmp_path, **candidate_shas):
    store = TaskExecutionStore(tmp_path / "executions")
    for task_id, sha in candidate_shas.items():
        store.save(
            TaskExecution(
                task_id=task_id,
                task_branch=f"autoloop/{task_id}",
                worktree_path=str(tmp_path / task_id),
                task_base_sha="0" * 40,
                candidate_sha=sha,
            )
        )
    return store


def context_for(registry, executions=None):
    return build_context(make_state(), FakeGit(), registry, "r", "p", executions=executions)


def test_counts_tasks_in_progress_and_the_candidates_they_hold(tmp_path):
    store = store_with(tmp_path, wip="c" * 40)
    ctx = context_for(busy_registry(), store)
    assert ctx.in_flight_count == 1
    assert ctx.unpublished_candidate_count == 1
    assert f"{IN_FLIGHT_LABEL}: 1 in progress, 1 holding an unpublished candidate" in (
        render_context(ctx)
    )


def test_in_progress_without_a_candidate_is_counted_only_as_in_progress(tmp_path):
    # Dispatched but nothing committed yet: in flight, nothing to approve.
    store = store_with(tmp_path, wip="")
    ctx = context_for(busy_registry(), store)
    assert (ctx.in_flight_count, ctx.unpublished_candidate_count) == (1, 0)


def test_completed_tasks_keep_their_candidate_sha_but_are_not_in_flight(tmp_path):
    """A finished task's record keeps its candidate sha forever. Counting every
    non-empty sha would report published work as awaiting approval."""
    store = store_with(tmp_path, done="d" * 40, wip="c" * 40)
    ctx = context_for(busy_registry(), store)
    assert (ctx.in_flight_count, ctx.unpublished_candidate_count) == (1, 1)


def test_nothing_in_flight_renders_zeroes(tmp_path):
    reg = TaskRegistry([Task(id="t1", title="First task", description="d")])
    ctx = context_for(reg, store_with(tmp_path))
    assert (ctx.in_flight_count, ctx.unpublished_candidate_count) == (0, 0)
    block = render_context(ctx)
    assert f"{IN_FLIGHT_LABEL}: 0 in progress, 0 holding an unpublished candidate" in block
    assert "next ready: t1" in block  # the roadmap line is unaffected


def test_empty_roadmap_renders_zeroes(tmp_path):
    ctx = context_for(TaskRegistry([]), store_with(tmp_path))
    assert (ctx.in_flight_count, ctx.unpublished_candidate_count) == (0, 0)
    assert "roadmap: no tasks planned yet" in render_context(ctx)


def test_candidate_count_is_unknown_not_zero_without_an_execution_store():
    """`Orchestrator` accepts `execution_store=None`. Rendering 0 there would
    put a number the loop does not know into the block the reviewer schedules
    from — and 0 is exactly the value that says 'go start something new'."""
    ctx = context_for(busy_registry())
    assert ctx.in_flight_count == 1
    assert ctx.unpublished_candidate_count is None
    assert f"{IN_FLIGHT_LABEL}: 1 in progress, unpublished candidates unknown" in (
        render_context(ctx)
    )


def test_unreadable_execution_record_reports_unknown_rather_than_parking(tmp_path):
    store = store_with(tmp_path, wip="c" * 40)
    (tmp_path / "executions" / "wip.json").write_text("{ not json", encoding="utf-8")
    with pytest.raises(StateCorruptError):
        store.load("wip")  # still loud where it matters
    ctx = context_for(busy_registry(), store)
    assert (ctx.in_flight_count, ctx.unpublished_candidate_count) == (1, None)


def test_the_label_the_contract_points_at_is_the_label_that_is_rendered():
    """The mutation guard, from the other side: `context.py`'s docstring says
    field labels are part of the protocol, and the scheduling preference names
    this one. Rename or drop it here and the rule points at nothing."""
    assert f"`{IN_FLIGHT_LABEL}`" in NEXT_WORK_PREFERENCE
    ctx = context_for(busy_registry())
    assert f"{IN_FLIGHT_LABEL}:" in render_context(ctx)


# ---- ready counts -----------------------------------------------------------
#
# The second scheduling preference (`contract.AUDIT_VS_READY_PREFERENCE`) tells
# the reviewer to implement a READY task rather than order a fresh audit while
# the queue has work in it. That is only checkable if the CONTEXT
# block says how many tasks ARE ready and how many of those are priority 1, so
# these tests own those numbers on the rendered side — `test_tasks.py` owns the
# summary string itself.


def queued_registry():
    """Five tasks: two ready at priority 1, one ready at the default priority,
    one in progress, one blocked on it."""
    reg = TaskRegistry(
        [
            Task(id="p1a", title="Urgent A", description="d", priority=1),
            Task(id="p1b", title="Urgent B", description="d", priority=1),
            Task(id="later", title="Later", description="d"),
            Task(id="wip", title="In flight", description="d"),
            Task(id="waiting", title="Waiting", description="d", depends_on=("wip",)),
        ]
    )
    reg.mark_in_progress("wip")
    return reg


def test_render_carries_the_ready_count_and_the_priority_one_count():
    """The mutation this guards: drop either number from `summary()` and the
    rule in CONTRACT_INSTRUCTIONS is asking the reviewer to weigh a count that
    never reaches it."""
    block = render_context(context_for(queued_registry()))
    assert f"{ROADMAP_LABEL}: " in block
    assert "3 ready (2 at priority 1)" in block


def test_render_carries_zeroes_when_the_ready_queue_is_empty():
    """The case where an audit IS the right call — the counts must still be
    rendered, because 0 ready is what tells the reviewer so."""
    reg = TaskRegistry(
        [
            Task(id="wip", title="In flight", description="d"),
            Task(id="waiting", title="Waiting", description="d", depends_on=("wip",)),
        ]
    )
    reg.mark_in_progress("wip")
    block = render_context(context_for(reg))
    assert "0 ready (0 at priority 1)" in block
    assert "next ready:" not in block  # nothing to point at


def test_ready_tasks_at_the_default_priority_are_not_counted_as_urgent():
    """100 is the default, so a roadmap nobody has prioritised must report 0 at
    priority 1 rather than reporting every task as urgent."""
    reg = TaskRegistry([Task(id="t1", title="First task", description="d")])
    assert "1 ready (0 at priority 1)" in render_context(context_for(reg))


def test_the_roadmap_label_the_contract_points_at_is_the_label_that_is_rendered():
    """Same pin as the in-flight one above, for the other preference: the audit
    rule names this line as where the counts are read from."""
    assert f"`{ROADMAP_LABEL}`" in AUDIT_VS_READY_PREFERENCE
    assert f"{ROADMAP_LABEL}:" in render_context(context_for(queued_registry()))
