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
