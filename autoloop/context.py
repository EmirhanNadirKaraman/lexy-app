"""Automatic review context for every outgoing request.

One CONTEXT block per request, assembled from persisted state + git + the task
registry. It serves two purposes:

* **Review quality** — ChatGPT always sees the previous decision, the task it
  concerns, roadmap status, git summary, validation summary and changed files,
  without any component having to remember to include them.
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

from .git_gateway import GitGateway
from .state import LoopState, utcnow_iso
from .tasks import TaskRegistry


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


def build_context(
    state: LoopState,
    git: GitGateway,
    registry: TaskRegistry,
    request_id: str,
    payload: str,
) -> ReviewContext:
    task = state.current_task or {}
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
    )


def render_context(ctx: ReviewContext, max_files: int = 40) -> str:
    files = ", ".join(ctx.changed_files[:max_files]) or "(none)"
    if len(ctx.changed_files) > max_files:
        files += f", ... ({len(ctx.changed_files) - max_files} more)"
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
        ]
    )
