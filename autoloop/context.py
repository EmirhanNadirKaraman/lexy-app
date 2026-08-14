"""Automatic review context for every outgoing request.

One CONTEXT block per request, assembled from persisted state + git + the task
registry. It serves two purposes:

* **Review quality** — ChatGPT always sees the previous decision, the task it
  concerns, roadmap status, in-flight counts, git summary, validation summary
  and changed files, without any component having to remember to include them.
  The in-flight line exists because CONTRACT_INSTRUCTIONS states a scheduling
  preference (finish before start) that a reviewer cannot evaluate without
  knowing how much is already in progress and how much of it is committed but
  unpublished.
* **Review integrity** — the block carries the stamp (request_id, timestamp,
  head_sha, base_sha, report_sha256) that a commit/push approval must copy
  into `reviewed`; `contract.verify_review` checks the echo against these
  exact values.

`build_context` is pure given its inputs (git access aside) and returns a
dataclass; `render_context` turns it into the text block. Field labels in the
rendered block are part of the protocol — ChatGPT is told to copy them — so
change them only together with CONTRACT_INSTRUCTIONS.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .errors import StateCorruptError
from .git_gateway import GitGateway
from .state import LoopState, utcnow_iso
from .tasks import TaskRegistry, TaskState
from .worktask import TaskExecutionStore

#: Label of the in-flight line in the rendered block. The scheduling
#: preference in `contract.CONTRACT_INSTRUCTIONS` points at this label by
#: name, so the two are coupled the way this module's docstring describes:
#: field labels are part of the protocol. `test_context.py` asserts the same
#: literal appears on both sides — a rename or a deletion here leaves the
#: contract pointing at a line that no longer exists, and the rule it states
#: cannot be followed without the numbers.
IN_FLIGHT_LABEL = "in_flight"


@dataclass(frozen=True)
class ReviewContext:
    request_id: str
    timestamp: str
    head_sha: str
    base_sha: str
    report_sha256: str
    branch: str
    dirty_count: int
    changed_files: tuple[str, ...]
    previous_decision: str
    previous_task: str
    validation_summary: str
    roadmap_status: str
    #: Tasks currently in progress.
    in_flight_count: int = 0
    #: How many of those hold a committed candidate that has not been
    #: published yet. `None` means "not established" — see
    #: `_in_flight_counts`; it is rendered as unknown rather than as 0.
    unpublished_candidate_count: int | None = None


def report_sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _changed_files(git: GitGateway) -> tuple[str, ...]:
    # Porcelain lines are "XY path" (or "XY orig -> dest" for renames);
    # keep the path part.
    files = []
    for line in git.dirty_files():
        path = line[3:] if len(line) > 3 else line
        files.append(path.split(" -> ")[-1].strip())
    return tuple(files)


def _in_flight_counts(
    registry: TaskRegistry, executions: TaskExecutionStore | None
) -> tuple[int, int | None]:
    """(tasks in progress, how many of those hold an unpublished candidate).

    The candidate count is scoped to IN-PROGRESS tasks deliberately. A
    completed task keeps its `candidate_sha` in its execution record forever,
    so counting every non-empty sha would fold finished-and-published work
    into a number that is meant to say "committed, waiting on your approval".

    The candidate count is `None` — not 0 — when it cannot be established:
    no execution store was supplied, or a record refuses to load. Rendering 0
    there would put a number the loop does not actually know into the block
    the reviewer is asked to schedule from. Corruption still surfaces where it
    matters (dispatch and crash recovery both load the same records and let
    `StateCorruptError` through); one line of a report is not the place to
    park the loop over it.
    """
    in_progress = [
        t for t in registry.all_tasks() if registry.state_of(t.id) is TaskState.IN_PROGRESS
    ]
    if executions is None:
        return len(in_progress), None
    holding = 0
    for task in in_progress:
        try:
            execution = executions.load(task.id)
        except StateCorruptError:
            return len(in_progress), None
        if execution is not None and execution.candidate_sha:
            holding += 1
    return len(in_progress), holding


def build_context(
    state: LoopState,
    git: GitGateway,
    registry: TaskRegistry,
    request_id: str,
    payload: str,
    executions: TaskExecutionStore | None = None,
) -> ReviewContext:
    task = state.current_task or {}
    in_flight_count, candidate_count = _in_flight_counts(registry, executions)
    previous_task = (
        f"{task.get('task_id')} ({task.get('title', '?')}, via {task.get('decision', '?')})"
        if task.get("task_id")
        else "(none)"
    )
    return ReviewContext(
        request_id=request_id,
        timestamp=utcnow_iso(),
        head_sha=git.head_sha(),
        base_sha=state.reviewed_commit or "(none)",
        report_sha256=report_sha256(payload),
        branch=git.current_branch(),
        dirty_count=len(git.dirty_files()),
        changed_files=_changed_files(git),
        previous_decision=state.last_decision or "(none)",
        previous_task=previous_task,
        validation_summary=state.last_validation or "(none)",
        roadmap_status=registry.summary(),
        in_flight_count=in_flight_count,
        unpublished_candidate_count=candidate_count,
    )


def render_context(ctx: ReviewContext, max_files: int = 40) -> str:
    files = ", ".join(ctx.changed_files[:max_files]) or "(none)"
    if len(ctx.changed_files) > max_files:
        files += f", ... ({len(ctx.changed_files) - max_files} more)"
    # The in-progress count also appears inside `roadmap_status`. Repeating it
    # here is deliberate: the scheduling preference in CONTRACT_INSTRUCTIONS
    # needs ONE stable label carrying both numbers, and the candidate count is
    # meaningless without the total it is a subset of.
    candidates = (
        f"{ctx.unpublished_candidate_count} holding an unpublished candidate"
        if ctx.unpublished_candidate_count is not None
        else "unpublished candidates unknown (no execution records available)"
    )
    return "\n".join(
        [
            "CONTEXT — copy the review-integrity values from here when approving:",
            f"request_id: {ctx.request_id}",
            f"timestamp: {ctx.timestamp}",
            f"head_sha: {ctx.head_sha}",
            f"base_sha: {ctx.base_sha}",
            f"report_sha256: {ctx.report_sha256}",
            f"branch: {ctx.branch} | uncommitted files: {ctx.dirty_count}",
            f"changed_files: {files}",
            f"previous_decision: {ctx.previous_decision}",
            f"previous_task: {ctx.previous_task}",
            f"validation: {ctx.validation_summary}",
            f"roadmap: {ctx.roadmap_status}",
            f"{IN_FLIGHT_LABEL}: {ctx.in_flight_count} in progress, {candidates}",
        ]
    )
