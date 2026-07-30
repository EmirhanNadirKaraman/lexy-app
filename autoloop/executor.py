"""Task-executor abstraction.

The executor is the "Fable does the engineering work" half of the loop —
repository audits and task implementation. That half is deliberately NOT built
yet; this module defines the seam it will plug into. The orchestrator only
ever sees the TaskExecutor protocol, so wiring in a real executor later (or a
fake one in tests) changes nothing else.

Since contract v2 the directive carries only a task id — the full Task (title,
description, dependencies) is resolved from the registry and passed alongside.
For `audit` there is no task; `task` is None and `directive.scope` may narrow
the audit. Executors should be idempotent per directive where possible: after
a crash mid-execution the orchestrator re-enters the executing phase and
dispatches the same directive again.

`ExecutionOutcome.validation` is the validation summary (lint/tests) that the
orchestrator stores and injects into every subsequent review request.

The produce-then-review commit path (`orchestrator.py`, `worktree.py`,
`worktask.py`) adds five more fields, all defaulted so every existing caller
and test is unaffected: `task_branch` / `task_base_sha` / `candidate_sha`
identify where and onto what the executor's changes were (or will be)
committed, `changed_paths` is what the orchestrator uses as the `CommitIntent`
planned-paths set (and therefore what `commit_and_capture` is allowed to
stage), and `post_commit_validation` records the RE-RUN validation summary
computed after the commit exists — separate from `validation` (computed
before/without a commit) because a commit hook can change committed content
in ways pre-commit validation never saw. `ExecutionOutcome` is frozen, so a
caller that only learns `candidate_sha` after committing (the orchestrator)
never mutates an outcome in place — it tracks that on `TaskExecution`
(`worktask.py`) instead, or via `dataclasses.replace`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .contract import Directive
from .tasks import Task


@dataclass(frozen=True)
class ExecutionOutcome:
    status: str  # "ok" | "error" | "not_implemented"
    summary: str
    details: str = ""
    validation: str = ""
    # ---- produce-then-review commit path (see module docstring) ----
    task_branch: str = ""
    task_base_sha: str = ""
    candidate_sha: str = ""
    changed_paths: tuple[str, ...] = ()
    post_commit_validation: str = ""


class TaskExecutor(Protocol):
    def execute(self, directive: Directive, task: Task | None) -> ExecutionOutcome: ...


class NullExecutor:
    """Placeholder until the audit/implement executor lands.

    Records what was asked and reports honestly that nothing was performed,
    so a live loop run stays truthful end to end.
    """

    def execute(self, directive: Directive, task: Task | None) -> ExecutionOutcome:
        subject = f"task '{task.id}' ({task.title})" if task is not None else "the audit"
        return ExecutionOutcome(
            status="not_implemented",
            summary=(
                f"No executor is wired up yet; {subject} was recorded but NOT performed."
            ),
            details=task.description if task is not None else (directive.scope or ""),
            validation="not run (executor not implemented)",
        )
