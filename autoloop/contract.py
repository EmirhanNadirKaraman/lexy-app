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
    #: RETIRED — no longer offered by CONTRACT_INSTRUCTIONS, still parsed. A
    #: live conversation that already saw the old instructions can answer
    #: `ask_user` at any time; keeping the member means such a reply parks the
    #: loop through `orchestrator._dispatch`'s ASK_USER branch instead of being
    #: rejected as `unknown_decision` and re-prompted into a parse-retry budget.
    ASK_USER = "ask_user"


# Decisions the contract still advertises, and the ones kept only so a reply
# written against older instructions parses. Derived by subtraction, not
# listed twice: a new enum member is ACTIVE unless it is explicitly retired,
# so it can never end up in neither set.
RETIRED_DECISIONS = frozenset({Decision.ASK_USER})
ACTIVE_DECISIONS = frozenset(Decision) - RETIRED_DECISIONS
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
    # Legacy: no longer documented in CONTRACT_INSTRUCTIONS, but still an
    # accepted key — dropping it here would fail a legacy `ask_user` at
    # `unknown_keys` before its own (retired) branch is ever reached.
    "question",
    "notes",
}
_COMMIT_KEYS = {"message", "paths"}
_REVIEWED_KEYS = {"request_id", "head_sha", "report_sha256"}
_TASK_SPEC_KEYS = {"id", "title", "description", "depends_on", "approved_paths"}

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

#: A rendered fenced block loses its backticks in `innerText` and leaves the
#: language label as a standalone first line ("JSON"). Exactly one such leading
#: label line is stripped, case-insensitively — nothing else.
_LANG_LABEL = re.compile(r"\A(?:json)[ \t]*\r?\n", re.IGNORECASE)


def _extract_envelope(text: str) -> str:
    """Return the single JSON envelope in `text`, or raise ContractError.

    Two accepted representations, both requiring the reply to contain exactly
    one directive:

    * **Canonical fenced** — one ```json block (raw markdown, other providers).
      Prose outside the fence is ignored because the fence delimits the
      directive unambiguously. Two or more blocks are rejected.
    * **Rendered / plain** — the whole reply is the JSON value, optionally
      preceded by one language-label line (what the browser DOM yields).

    Deliberately NOT supported: picking one object out of several by position.
    A reply that mixes prose with a bare object, or carries a second object or
    trailing instructions, is rejected — with a directive that can authorize a
    commit or push, "guess which one they meant" is not an acceptable rule.
    """
    fenced = _JSON_BLOCK.findall(text)
    if len(fenced) > 1:
        raise ContractError(
            "multiple_json_blocks",
            f"{len(fenced)} fenced json blocks found — send exactly one directive; "
            "position is never used to choose between them",
        )
    if fenced:
        return fenced[0].strip()
    return _LANG_LABEL.sub("", text.strip(), count=1).strip()


@dataclass(frozen=True)
class TaskSpec:
    """A task definition inside a `plan` decision."""

    id: str
    title: str
    description: str
    depends_on: tuple[str, ...] = ()
    #: The task's write-scope authorization — see `tasks.Task.approved_paths`.
    #: Accepted here (type/shape only: a list of non-empty strings) but NOT
    #: required at the protocol level — making it required would be a
    #: breaking wire change to every existing `plan` (PROTOCOL_VERSION stays
    #: 3). The real enforcement is downstream and fail-closed instead:
    #: `tasks.TaskRegistry.add_many` validates each path is an exact,
    #: repo-relative, non-glob, non-'..' pathspec, and
    #: `orchestrator._dispatch_task_postcommit` refuses to dispatch a
    #: write-capable implement/revise for a task whose `approved_paths` is
    #: still empty — so an omitted scope makes a task permanently
    #: undispatchable rather than silently unscoped.
    approved_paths: tuple[str, ...] = ()


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
    #: Only ever set by the retired `ask_user` shape, and optional even there —
    #: the contract no longer asks for it, so a legacy reply may omit it.
    question: str | None = None
    notes: str | None = None


CONTRACT_INSTRUCTIONS = """\
RESPONSE FORMAT — mandatory.
Your ENTIRE reply must be exactly one fenced JSON code block (```json ... ```)
and nothing else: no sentence before it, no sentence after it. Two blocks, a
second object, or trailing text is REJECTED, never guessed at. The block is one
object with these keys and no others:

  version    (required) always 3
  decision   (required) one of: audit | plan | implement | revise | commit |
             push | commit_and_push | stop
  reason     (required) one short sentence explaining the decision
  scope      (audit only, optional) what the audit should focus on
  tasks      (required for plan) list of {id, title, description, depends_on?,
             approved_paths?}. id: a stable slug ([A-Za-z0-9._-], max 64).
             depends_on: ids of existing tasks or tasks in this same batch.
             approved_paths: the EXACT repo-relative files this task may touch
             — no globs, no "..", no absolute paths; name new files
             explicitly. A task with no approved path cannot be implemented.
  task_id    (required for implement/revise; optional for commit /
             commit_and_push, where it marks that task completed)
  feedback   (required for revise) what is wrong and must change
  commit     (required for commit/commit_and_push) an object:
               message (required) the full commit message
               paths   (required) NON-EMPTY list of repo-relative paths to
                       stage. There is no "stage everything"; every path must
                       be one the task actually changed.
  reviewed   (required for commit/push/commit_and_push) an object:
               {request_id, head_sha, report_sha256}
             Copy all three EXACTLY from the CONTEXT block of the request you
             are answering; never approve from memory. A stamp that does not
             match the reviewed state is rejected.
  notes      (optional) anything else worth recording

Decisions:
  audit — the executor audits the repository and reports back.
  plan — add tasks to the roadmap; nothing is executed by plan itself. Work is
    authorized ONLY by task id, so plan before you implement.
  implement — the executor performs the referenced READY task and reports back
    with validation results. No free-form instructions.
  revise — send the referenced task back to the executor with feedback.
    task_id "audit" sends the AUDIT itself back for another pass.
  commit — commit the reviewed work; no push. task_id marks the task completed.
  push — push the current branch (the reviewed commit); no new commit.
  commit_and_push — commit, then push.
  stop — end the loop."""


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
        paths_raw = item.get("approved_paths")
        approved_paths: tuple[str, ...] = ()
        if paths_raw is not None:
            if not isinstance(paths_raw, list) or not all(
                isinstance(p, str) and p.strip() for p in paths_raw
            ):
                raise ContractError(
                    f"bad_type:{where}.approved_paths",
                    f"'{where}.approved_paths' must be a list of non-empty strings",
                )
            # Deliberately NOT stripped/normalized here: the exact string is
            # what `tasks._validate_approved_path` and the later dispatch-time
            # comparison both see, so silently trimming whitespace here would
            # let a path differ from what actually gets validated/compared.
            approved_paths = tuple(paths_raw)
        specs.append(
            TaskSpec(
                id=task_id,
                title=title,
                description=description,
                depends_on=deps,
                approved_paths=approved_paths,
            )
        )
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

    envelope = _extract_envelope(text)
    try:
        data = json.loads(envelope)
    except json.JSONDecodeError as exc:
        if exc.msg.startswith("Extra data"):
            raise ContractError(
                "trailing_content",
                "content follows the JSON directive (a second object, another "
                "decision, or trailing text). The reply must be exactly one "
                "directive and nothing else — trailing content is never ignored "
                f"and never resolved by position: {exc}",
            ) from exc
        if "{" not in envelope:
            # Nothing JSON-shaped at all (usually a conversational reply). Say
            # so plainly rather than complaining about JSON syntax.
            raise ContractError(
                "no_json_block",
                "no JSON directive found — reply with exactly one fenced ```json "
                "block and nothing else",
            ) from exc
        raise ContractError(
            "invalid_json",
            "the reply is not exactly one JSON value — send only the fenced "
            f"```json block, with no prose around it: {exc}",
        ) from exc
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
            # Active decisions only: the correction is what the model is told
            # to choose from next, and naming a retired decision there would
            # invite it back into use.
            f"'{raw_decision}' is not one of "
            f"{sorted(d.value for d in ACTIVE_DECISIONS)}",
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
        # Optional, not required: `question` is no longer documented, so a
        # legacy reply may omit it (the orchestrator parks with a placeholder).
        # Present-but-blank is still rejected — a park with an empty question is
        # worse than one that admits it has none.
        if question_raw is not None:
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
