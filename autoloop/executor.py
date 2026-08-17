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

`assumptions` is the sixth such field and the one that is not about git: it
carries the readings the executor had to CHOOSE because the task did not say.
See its own comment below — it exists because `ask_user` is retired, so an
ambiguity that used to stop the loop and ask now has to reach the reviewer as
a disclosure attached to the work instead.
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
    #: Readings the executor CHOSE where the task did not say — the disclosure
    #: half of the rule that replaced asking a human.
    #:
    #: `ask_user` is retired (`contract.RETIRED_DECISIONS`), so an ambiguous
    #: task can no longer be escalated mid-run: the executor takes the
    #: smallest reversible reading and carries on. That is only safe if the
    #: choice is visible to the reviewer who authorizes the result — an
    #: assumption nobody is told about is indistinguishable from a
    #: misunderstanding, and the review is the last point at which either can
    #: be caught.
    #:
    #: CLAIMS, exactly like `summary`/`details`: the executor authors this text
    #: and nothing downstream may treat it as authorization. It is accumulated
    #: onto `worktask.TaskExecution.assumptions` and rendered inside the
    #: packet's clearly-labelled executor-report section.
    assumptions: tuple[str, ...] = ()
    #: Why `status="error"` happened, when — and ONLY when — the cause was
    #: environmental rather than the task's own work: a provider 429 or API
    #: error that stopped the agent before it produced anything, or the stall
    #: supervisor killing it. A short machine slug; empty means "not a fault".
    #:
    #: `status="error"` covers four different things in
    #: `implement_executor._run_implementation` (the agent did not complete, a
    #: git read failed, the agent changed no files, validation failed) and only
    #: the first can be a fault. This field is how the orchestrator tells them
    #: apart WITHOUT reading `summary` prose, which is exactly the kind of
    #: inference the attempt ledger exists to replace.
    #:
    #: FAIL CLOSED: anything an executor cannot positively name is left empty
    #: and charged to the task's own attempt budget. A wrongly-blank fault
    #: costs one attempt; a wrongly-set one would excuse a genuine failure.
    fault_kind: str = ""


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
