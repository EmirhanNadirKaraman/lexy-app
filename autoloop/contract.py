"""Machine-readable response contract between the orchestrator and ChatGPT.

Protocol v2. Two structural rules distinguish it from v1:

* **Work is authorized by task id, never by free-form instruction.** ChatGPT
  first emits `plan` (a batch of task definitions for the registry), then
  `implement`/`revise` referencing a task id. The executor looks the task up;
  the response carries no engineering prose to execute.
* **Git approvals carry a review-integrity stamp.** `commit`/`push`/
  `commit_and_push` must include `reviewed: {request_id, head_sha,
  report_sha256}` copied from the CONTEXT block of the request being answered.
  `verify_review` checks the stamp against what was actually sent, so an
  approval can never be applied to a state ChatGPT did not review.

Every prompt embeds CONTRACT_INSTRUCTIONS. `parse_response` validates
strictly; failures raise ContractError with a stable code that is echoed back
for correction. Nothing is guessed or defaulted.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import Enum

from .errors import ContractError

# v3 (Phase 3): commit.paths is REQUIRED and non-empty — blanket "stage
# everything" commits no longer exist at the contract level.
PROTOCOL_VERSION = 3

# The audit pseudo-task id: `revise` may target it to re-run the audit with
# feedback. It never exists in the task registry.
AUDIT_TASK_ID = "audit"


class Decision(str, Enum):
    AUDIT = "audit"
    PLAN = "plan"
    IMPLEMENT = "implement"
    REVISE = "revise"
    COMMIT = "commit"
    PUSH = "push"
    COMMIT_AND_PUSH = "commit_and_push"
    STOP = "stop"
    ASK_USER = "ask_user"


# Decisions that authorize executor work on a referenced task.
TASK_DECISIONS = frozenset({Decision.IMPLEMENT, Decision.REVISE})
# Decisions that authorize a git commit.
COMMIT_DECISIONS = frozenset({Decision.COMMIT, Decision.COMMIT_AND_PUSH})
# Decisions that authorize a git push.
PUSH_DECISIONS = frozenset({Decision.PUSH, Decision.COMMIT_AND_PUSH})
# Decisions that require the review-integrity stamp.
REVIEWED_DECISIONS = COMMIT_DECISIONS | PUSH_DECISIONS

_TOP_LEVEL_KEYS = {
    "version",
    "decision",
    "reason",
    "scope",
    "tasks",
    "task_id",
    "feedback",
    "commit",
    "reviewed",
    "question",
    "notes",
}
_COMMIT_KEYS = {"message", "paths"}
_REVIEWED_KEYS = {"request_id", "head_sha", "report_sha256"}
_TASK_SPEC_KEYS = {"id", "title", "description", "depends_on"}

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class TaskSpec:
    """A task definition inside a `plan` decision."""

    id: str
    title: str
    description: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewRef:
    """The stamp inside a git approval — must match the request it answers."""

    request_id: str
    head_sha: str
    report_sha256: str


@dataclass(frozen=True)
class Directive:
    """A validated ChatGPT response, ready for policy check + dispatch."""

    decision: Decision
    reason: str
    scope: str | None = None
    tasks: tuple[TaskSpec, ...] | None = None
    task_id: str | None = None
    feedback: str | None = None
    commit_message: str | None = None
    commit_paths: tuple[str, ...] | None = None
    reviewed: ReviewRef | None = None
    question: str | None = None
    notes: str | None = None


CONTRACT_INSTRUCTIONS = """\
RESPONSE FORMAT — mandatory.
Reply with exactly one fenced JSON code block (```json ... ```). Prose outside
the block is ignored; only the LAST json block is read. The block must be a
single object with these keys and no others:

  version    (required) always 3
  decision   (required) one of: audit | plan | implement | revise | commit |
             push | commit_and_push | stop | ask_user
  reason     (required) one short sentence explaining the decision
  scope      (audit only, optional) what the audit should focus on
  tasks      (required for plan) list of task definitions:
               {id, title, description, depends_on?}
             id: a stable slug ([A-Za-z0-9._-], max 64). depends_on lists ids
             of existing tasks or tasks in this same batch.
  task_id    (required for implement/revise; optional for commit /
             commit_and_push, where it marks that task completed)
  feedback   (required for revise) what is wrong and must change
  commit     (required for commit/commit_and_push) an object:
               message (required) the full commit message
               paths   (required) NON-EMPTY list of repo-relative paths to
                       stage. There is no "stage everything" — every approved
                       path must be a file the task actually changed.
  reviewed   (required for commit/push/commit_and_push) an object:
               {request_id, head_sha, report_sha256}
             Copy the three values EXACTLY from the CONTEXT block of the
             request you are answering. An approval whose stamp does not match
             the reviewed state is rejected — never approve from memory.
  question   (required for ask_user) the question for the human operator
  notes      (optional) anything else worth recording

Decision semantics:
  audit — the executor audits the repository and reports back.
  plan — add tasks to the roadmap. Work is authorized ONLY by task id, so
    plan before you implement. Nothing is executed by plan itself.
  implement — the executor performs the referenced READY task and reports
    back with validation results. No free-form instructions — use plan first
    if the roadmap lacks the task.
  revise — send the referenced task back to the executor with feedback. Use
    task_id "audit" to send the AUDIT itself back for another pass.
  commit — commit the reviewed work. No push. Add task_id to mark the task
    completed.
  push — push the current branch (the reviewed commit). No new commit.
  commit_and_push — commit, then push.
  stop — end the loop.
  ask_user — pause the loop and surface `question` to the human operator.

Malformed replies are rejected with an error code and echoed back for
correction — they are never guessed at."""


def _require_str(field: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"missing_field:{field}", f"'{field}' must be a non-empty string")
    return value.strip()


def _forbid(field: str, value: object, decision: Decision) -> None:
    if value is not None:
        raise ContractError(
            "unexpected_field",
            f"'{field}' is not allowed for decision '{decision.value}'",
        )


def _parse_task_specs(raw: object) -> tuple[TaskSpec, ...]:
    if not isinstance(raw, list) or not raw:
        raise ContractError(
            "missing_field:tasks", "'tasks' must be a non-empty list for decision 'plan'"
        )
    specs: list[TaskSpec] = []
    for i, item in enumerate(raw):
        where = f"tasks[{i}]"
        if not isinstance(item, dict):
            raise ContractError("bad_type:tasks", f"{where} must be an object")
        unknown = set(item) - _TASK_SPEC_KEYS
        if unknown:
            raise ContractError("unknown_keys", f"unknown keys in {where}: {sorted(unknown)}")
        task_id = _require_str(f"{where}.id", item.get("id"))
        title = _require_str(f"{where}.title", item.get("title"))
        description = _require_str(f"{where}.description", item.get("description"))
        deps_raw = item.get("depends_on")
        deps: tuple[str, ...] = ()
        if deps_raw is not None:
            if not isinstance(deps_raw, list) or not all(
                isinstance(d, str) and d.strip() for d in deps_raw
            ):
                raise ContractError(
                    f"bad_type:{where}.depends_on",
                    f"'{where}.depends_on' must be a list of non-empty strings",
                )
            deps = tuple(d.strip() for d in deps_raw)
        specs.append(TaskSpec(id=task_id, title=title, description=description, depends_on=deps))
    return tuple(specs)


def _parse_reviewed(raw: object, decision: Decision) -> ReviewRef:
    if not isinstance(raw, dict):
        raise ContractError(
            "missing_field:reviewed",
            f"'reviewed' must be an object for decision '{decision.value}' — copy "
            "request_id, head_sha and report_sha256 from the request's CONTEXT block",
        )
    unknown = set(raw) - _REVIEWED_KEYS
    if unknown:
        raise ContractError("unknown_keys", f"unknown keys in 'reviewed': {sorted(unknown)}")
    return ReviewRef(
        request_id=_require_str("reviewed.request_id", raw.get("request_id")),
        head_sha=_require_str("reviewed.head_sha", raw.get("head_sha")),
        report_sha256=_require_str("reviewed.report_sha256", raw.get("report_sha256")),
    )


def parse_response(text: str) -> Directive:
    """Parse ChatGPT's raw reply into a Directive, or raise ContractError."""
    if not isinstance(text, str) or not text.strip():
        raise ContractError("empty_response", "response text is empty")

    blocks = _JSON_BLOCK.findall(text)
    if blocks:
        candidate = blocks[-1]
    elif text.strip().startswith("{"):
        candidate = text.strip()
    else:
        raise ContractError("no_json_block", "no fenced ```json block found in response")

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ContractError("invalid_json", f"json block does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError("not_an_object", "top-level JSON value must be an object")

    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise ContractError("unknown_keys", f"unknown top-level keys: {sorted(unknown)}")

    version = data.get("version")
    if version != PROTOCOL_VERSION:
        raise ContractError(
            "bad_version", f"version must be {PROTOCOL_VERSION}, got {version!r}"
        )

    raw_decision = data.get("decision")
    if not isinstance(raw_decision, str):
        raise ContractError("missing_field:decision", "'decision' must be a string")
    try:
        decision = Decision(raw_decision)
    except ValueError:
        raise ContractError(
            "unknown_decision",
            f"'{raw_decision}' is not one of {sorted(d.value for d in Decision)}",
        ) from None

    reason = _require_str("reason", data.get("reason"))

    notes = data.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise ContractError("bad_type:notes", "'notes' must be a string when present")

    scope_raw = data.get("scope")
    tasks_raw = data.get("tasks")
    task_id_raw = data.get("task_id")
    feedback_raw = data.get("feedback")
    commit_raw = data.get("commit")
    reviewed_raw = data.get("reviewed")
    question_raw = data.get("question")

    scope = None
    if decision is Decision.AUDIT:
        if scope_raw is not None:
            scope = _require_str("scope", scope_raw)
    else:
        _forbid("scope", scope_raw, decision)

    tasks = None
    if decision is Decision.PLAN:
        tasks = _parse_task_specs(tasks_raw)
    else:
        _forbid("tasks", tasks_raw, decision)

    task_id = None
    if decision in TASK_DECISIONS:
        task_id = _require_str("task_id", task_id_raw)
    elif decision in COMMIT_DECISIONS:
        if task_id_raw is not None:
            task_id = _require_str("task_id", task_id_raw)
    else:
        _forbid("task_id", task_id_raw, decision)

    feedback = None
    if decision is Decision.REVISE:
        feedback = _require_str("feedback", feedback_raw)
    else:
        _forbid("feedback", feedback_raw, decision)

    commit_message = None
    commit_paths: tuple[str, ...] | None = None
    if decision in COMMIT_DECISIONS:
        if not isinstance(commit_raw, dict):
            raise ContractError(
                "missing_field:commit",
                f"'commit' must be an object for decision '{decision.value}'",
            )
        unknown_commit = set(commit_raw) - _COMMIT_KEYS
        if unknown_commit:
            raise ContractError(
                "unknown_keys", f"unknown keys in 'commit': {sorted(unknown_commit)}"
            )
        commit_message = _require_str("commit.message", commit_raw.get("message"))
        paths_raw = commit_raw.get("paths")
        if paths_raw is None:
            raise ContractError(
                "missing_field:commit.paths",
                "'commit.paths' is required — name the exact files to commit; "
                "there is no stage-everything commit",
            )
        if (
            not isinstance(paths_raw, list)
            or not paths_raw
            or not all(isinstance(p, str) and p.strip() for p in paths_raw)
        ):
            raise ContractError(
                "bad_type:commit.paths",
                "'commit.paths' must be a non-empty list of non-empty strings",
            )
        commit_paths = tuple(p.strip() for p in paths_raw)
    else:
        _forbid("commit", commit_raw, decision)

    reviewed = None
    if decision in REVIEWED_DECISIONS:
        reviewed = _parse_reviewed(reviewed_raw, decision)
    else:
        _forbid("reviewed", reviewed_raw, decision)

    question = None
    if decision is Decision.ASK_USER:
        question = _require_str("question", question_raw)
    else:
        _forbid("question", question_raw, decision)

    return Directive(
        decision=decision,
        reason=reason,
        scope=scope,
        tasks=tasks,
        task_id=task_id,
        feedback=feedback,
        commit_message=commit_message,
        commit_paths=commit_paths,
        reviewed=reviewed,
        question=question,
        notes=notes,
    )


def verify_review(
    directive: Directive,
    expected_request_id: str,
    expected_head_sha: str,
    expected_report_sha256: str,
) -> None:
    """Reject a git approval whose stamp does not match the reviewed request.

    Called by the orchestrator before any commit/push dispatch, with the
    values that were actually stamped into the request ChatGPT answered.
    """
    ref = directive.reviewed
    if ref is None:  # pragma: no cover - parse_response enforces presence
        raise ContractError("review_mismatch:missing", "approval carries no reviewed stamp")
    if ref.request_id != expected_request_id:
        raise ContractError(
            "review_mismatch:request_id",
            f"approval references request '{ref.request_id}' but answers "
            f"'{expected_request_id}'",
        )
    if ref.head_sha != expected_head_sha:
        raise ContractError(
            "review_mismatch:head_sha",
            f"approval references head {ref.head_sha!r} but the reviewed head "
            f"was {expected_head_sha!r}",
        )
    if ref.report_sha256 != expected_report_sha256:
        raise ContractError(
            "review_mismatch:report_sha256",
            "approval references a different report than the one that was reviewed",
        )
