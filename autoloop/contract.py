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
* **`implement` carries the decomposition it authorizes.** `Decomposition`
  below — approach, expected files, and the steps to work in order (the key is
  literally `steps`; the ordering is a rule about them, not part of the key
  name, and the documented spelling is pinned to the parsed one by test) —
  rides on the directive that already starts the work, so planning costs no
  extra round. It is
  OPTIONAL at this layer and required by `policy.authorize_directive`, exactly
  like `TaskSpec.approved_paths`: see `Decomposition` for why the enforcement
  lives there and not here.

Every prompt embeds CONTRACT_INSTRUCTIONS, which is the response format plus
two advisory clauses: NEXT_WORK_PREFERENCE (which decision to prefer when work
is already in flight) and AUDIT_VS_READY_PREFERENCE (ready roadmap work before
a fresh audit). Neither is enforced by the parser or by policy — see the
comments on each. `parse_response` validates strictly; failures raise
ContractError with a stable code that is echoed back for correction. Nothing is
guessed or defaulted.
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
    #: Discard an unsalvageable candidate and start the named task again from
    #: the current base. THE reviewer's own destructive verb — every other
    #: discarding action in this system passes through an operator — so it is
    #: bounded by `orchestrator._dispatch_recut` rather than by this enum: at
    #: most `orchestrator.MAX_TASK_RECUTS` per task, never a published
    #: candidate, never one whose verdict is still outstanding, and nothing is
    #: deleted (`worktask.retire_execution` moves both halves aside).
    #:
    #: Deliberately NOT `stop`. `stop` parks because a HUMAN must decide; this
    #: is the reviewer deciding. When it is unsure, `stop` is still correct, and
    #: `_RESPONSE_FORMAT` below says which is which in those words.
    #:
    #: The CAP appears in `_RESPONSE_FORMAT` as a literal `2` rather than as a
    #: rendered constant, because `orchestrator` imports this module and not the
    #: other way round. That is a second copy of a number, so it is pinned by
    #: test (`test_recut.test_the_instructions_state_the_cap_the_loop_actually_
    #: enforces`) instead of by construction: moving `MAX_TASK_RECUTS` without
    #: moving the sentence fails the suite rather than quietly telling the
    #: reviewer it has a cut it does not have.
    RECUT = "recut"
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
# Decisions that must NAME a task, whatever they then do with it. `recut` is
# not a TASK_DECISION — it authorizes no executor work and carries no
# decomposition — but a recut with no task id names nothing to discard, so
# `task_id` is required for it too. Derived rather than restated so a third
# task-naming decision cannot be added to one list and forgotten in the other.
NAMES_A_TASK = TASK_DECISIONS | {Decision.RECUT}
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
    "decomposition",
    "commit",
    "reviewed",
    # Legacy: no longer documented in CONTRACT_INSTRUCTIONS, but still an
    # accepted key — dropping it here would fail a legacy `ask_user` at
    # `unknown_keys` before its own (retired) branch is ever reached.
    "question",
    "notes",
    # The vocabulary gap the reviewer NAMES but never gets to use — see
    # `Directive.wanted_decision`. Listed here or every reply carrying it would
    # die at `unknown_keys` before the field is read.
    "wanted_decision",
}
_COMMIT_KEYS = {"message", "paths"}
_REVIEWED_KEYS = {"request_id", "head_sha", "report_sha256"}
_DECOMPOSITION_KEYS = {"approach", "files", "steps"}
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
class Decomposition:
    """The plan a task is implemented from, approved BEFORE any code is written.

    **Why it rides on `implement` rather than costing its own round.** The loop
    already asks the reviewer what to work on and already receives `implement`
    before any agent runs, so a decomposition carried by that directive is
    approved at the moment the work is authorized — no extra round trip, and
    nothing else in the protocol moves. A separate plan round would add one
    round to EVERY task; tasks that land today take one to three, so that is a
    30-100% tax on the common case, paid on every task to catch the occasional
    oversized one. This field is that decision, written down.

    **One step is a first-class answer.** `steps` of length one is accepted
    everywhere, deliberately: most work that lands here is 500-2,000 lines in a
    single commit, and forcing it apart is what turned one capability into ten
    tasks of which four were already implemented. The REASON a plan is one step
    belongs in that step's own text — a separate field for it would invite two
    answers to one question.

    **Prose, never a schedule.** Nothing dispatches per step, and nothing here
    splits a task into several: that is `split-01`'s mechanism, which applies a
    split atomically across the registry, the execution record and the worker
    repo. These steps are instructions for the implementing agent, in the same
    category as `tasks.Task.description`, and keeping them prose is what stops
    this becoming the second split mechanism the design forbids.

    **Optional here, required by `policy.authorize_directive` — for `revise`
    too.** The same layering `TaskSpec.approved_paths` already uses: requiring
    it in the parser
    would be a breaking wire change to every existing directive (and
    PROTOCOL_VERSION stays 3), and a missing decomposition is a well-formed
    directive that is not authorized rather than a malformed one — so it draws
    a policy denial, which explains itself and is bounded by the denial budget,
    instead of spending the much smaller parse-retry budget on a correction
    that says "field missing". The refusal happens before dispatch, so it costs
    no `TaskExecution.attempt_count`. The gate covers `revise` as well as
    `implement` — a revise names a task the same way, and one on a task never
    implemented would otherwise start it with no plan — but the ordinary
    revise-after-implement finds the plan already on the task and carries
    nothing, so reuse stays free.
    """

    approach: str
    files: tuple[str, ...]
    steps: tuple[str, ...]

    def render(self) -> str:
        """The durable text stored on the task and shown to the implementing
        agent (`tasks.Task.decomposition`, `implement_executor._agent_prompt`).

        ONE rendering, in one place, so the plan the reviewer approved and the
        instructions the agent is given cannot drift apart. A one-step plan
        says so in its heading rather than being silently numbered "1." on its
        own — the reviewer is allowed to answer "this is one step", and the
        record should read back as that answer.
        """
        lines = [f"Approach: {self.approach}", "Files expected to change:"]
        lines += [f"  - {path}" for path in self.files]
        lines.append(
            "This is one step:" if len(self.steps) == 1 else "Steps, in order:"
        )
        lines += [f"  {i}. {step}" for i, step in enumerate(self.steps, 1)]
        return "\n".join(lines)


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
    #: The plan this directive authorizes, when it carries one. Accepted on
    #: `implement` and `revise` only — see `Decomposition` for why `implement`
    #: without one is refused by policy rather than by the parser, and why
    #: `revise` may omit it (the stored plan stands unless the reviewer
    #: reshapes it).
    decomposition: Decomposition | None = None
    commit_message: str | None = None
    commit_paths: tuple[str, ...] | None = None
    reviewed: ReviewRef | None = None
    #: Only ever set by the retired `ask_user` shape, and optional even there —
    #: the contract no longer asks for it, so a legacy reply may omit it.
    question: str | None = None
    notes: str | None = None
    #: The decision the reviewer WOULD have used, when none of the available
    #: ones fit. A plain string, parsed, recorded, counted and rendered — and
    #: STRUCTURALLY UNABLE TO BE ACTED ON.
    #:
    #: **Why it is a string and not a `Decision`.** If this value could ever
    #: become the verb that is dispatched, the reviewer would hold an unbounded
    #: vocabulary and could name actions the policy engine never authorized —
    #: the circular-ownership hazard `docs/SECURITY.md` finding #2 exists to
    #: close. So it is never passed to `Decision(...)`, never compared against a
    #: `Decision`, and `orchestrator._dispatch` branches on `decision` alone.
    #: `parse_response` deliberately does NOT validate it against `Decision`
    #: either: a value naming a decision that already exists is itself the
    #: signal — the reviewer believed the fitting verb was unavailable when it
    #: was not, i.e. these instructions are unclear.
    #:
    #: **Why ONE narrow question and not `notes`.** `notes` is documented as
    #: "anything else worth recording"; measured over 578 directives it was used
    #: zero times and has no consumer anywhere in `orchestrator.py`,
    #: `dashboard.py`, `transcript.py` or `worktask.py`. An open-ended optional
    #: field earns exactly that. A specific question has an answer shape, and
    #: the tally of the answers (`wanted: recut x7, split x3`) is how the NEXT
    #: missing verb gets found by counting instead of by someone happening to
    #: read a `reason` field. A named verb becomes real only the slow way: a
    #: person reads the tally and files a task.
    wanted_decision: str | None = None


#: The response schema itself. Two clauses in it are worth explaining HERE
#: rather than in the prompt, which is re-sent every turn:
#:
#: **The `notes` bound and the line-break rule (2026-08-24).** `parse_response`
#: is unchanged by them and enforces neither — they are guidance the model
#: reads, exactly like the two preference clauses below. Measured 2026-08-20/21:
#: `parse_error` fired 25 times in three weeks, eight of them in one thirty-hour
#: window, and the recent ones were all the same defect —
#: `invalid_json: Invalid control character at: line 6 column ~2073` — a literal
#: newline inside the long `notes` value. The reviewer's CONTENT was right every
#: time; only its encoding was not. Two of those parked the loop
#: `parse_budget_exhausted` (loop_fatal, `policy.max_parse_retries` is 2), once
#: for six unattended hours. Both recoveries were a conversational `run
#: --answer` saying "escape newlines, keep notes short", which works and then
#: decays, because it lives in the thread and does not survive a rotation or a
#: fresh session. This text does, which is the whole point of putting it here.
#:
#: Why "write \\n" and NOT "keep every string on one line": `commit.message` is
#: documented above as the FULL commit message and this repo's messages have
#: bodies, so a one-line-only rule would quietly cost every commit body. The
#: escape is legal JSON and keeps multi-line values expressible.
#:
#: Why 200 is a literal here and not a named constant: nothing reads or
#: enforces it, so there is no second copy for it to drift from — unlike
#: `note_merge.MAX_NOTE_LINE_CHARS`, which a validator checks and a prompt
#: states. A constant would advertise an enforcement that does not exist.
_RESPONSE_FORMAT = """\
RESPONSE FORMAT — mandatory.
Your ENTIRE reply is exactly one fenced JSON block (```json ... ```) and
nothing else: no sentence before or after it. Two blocks, a second object, or
trailing text is REJECTED, never guessed at. One object, these keys only:

  version    (required) always 3
  decision   (required) one of: audit | plan | implement | revise | commit |
             push | commit_and_push | recut | stop
  reason     (required) one short sentence explaining the decision
  scope      (audit only, optional) what the audit should focus on
  tasks      (required for plan) list of {id, title, description, depends_on?,
             approved_paths?}. id: a slug ([A-Za-z0-9._-], max 64).
             depends_on: ids of existing tasks or tasks in this same batch.
             approved_paths: the EXACT repo-relative files this task may touch
             — no globs, no "..", no absolute paths; name new files. A task
             with no approved path cannot be implemented.
  task_id    (required for implement/revise/recut; optional for commit /
             commit_and_push, marking that task completed)
  decomposition (required for implement) {approach, files, steps}; steps are
             worked in order; one step with a reason is valid.
  feedback   (required for revise) what is wrong and must change
  commit     (required for commit/commit_and_push) an object:
               message (required) the full commit message
               paths   (required) NON-EMPTY list of repo-relative paths to
                       stage — no "stage everything"; every path must be one
                       the task actually changed.
  reviewed   (required for commit/push/commit_and_push) an object:
               {request_id, head_sha, report_sha256}
             Copy all three EXACTLY from the CONTEXT block of the request
             you are answering; never approve from memory. A mismatched stamp
             is rejected.
  notes      (optional) at most 200 characters, on ONE line.
  wanted_decision (optional) ONE word: the decision you WOULD have used when
             none above fits. Counted for the operator, and NEVER acted on —
             the loop still executes `decision`.
NEVER put a literal line break inside a JSON string value — write \\n.
A raw newline in a string is invalid JSON, and the reply is REJECTED.

Decisions:
  audit — the executor audits the repository and reports back.
  plan — add tasks to the roadmap; plan executes nothing. Work is authorized
    ONLY by task id, so plan before you implement.
  implement — the executor performs the referenced READY task, one approved
    step at a time, and reports back with validation results.
  revise — send the referenced task back to the executor with feedback;
    task_id "audit" re-runs the AUDIT.
  commit — commit the reviewed work; no push. task_id marks the task completed.
  push — push the current branch (the reviewed commit); no new commit.
  commit_and_push — commit, then push.
  recut — DISCARD task_id's candidate; the task is re-cut from the CURRENT
    base. ONLY for an unsalvageable branch (contaminated history, a dead end
    another `revise` would repeat); work that needs changing is `revise`.
    Nothing is deleted — the record is archived, the worker quarantined.
    REFUSED for a published candidate, one whose verdict is outstanding, and
    after 2 recuts of a task (the third parks for a human: two clean cuts
    failing means the SPEC is wrong, not the branch).
  stop — end the loop.
`recut` vs `stop`: `recut` is YOU deciding a branch is beyond saving; `stop`
asks a HUMAN to decide. Unsure? Use `stop`."""

#: A scheduling PREFERENCE, appended to the response format above.
#:
#: Advisory by construction. `parse_response` never reads this text and
#: `policy.py` gains no refusal from it, because a scheduling preference
#: expressed as a policy denial is the wrong layer twice over: policy
#: authorizes actions, and a denial would park the loop instead of redirecting
#: it. It is also deliberately not absolute — a task can be legitimately stuck
#: on an external condition, and forbidding new work would stall everything
#: queued behind it — so it names the three cases where starting fresh work is
#: still the right call.
#:
#: It orders `implement`/`revise`/approval among themselves and says nothing
#: about `audit`: which of a fresh audit or ready roadmap work comes first is a
#: separate rule with its own home — `AUDIT_VS_READY_PREFERENCE` below — and
#: restating it here would give the same preference two texts to drift between.
#:
#: The numbers it depends on are rendered by `context.render_context` under
#: `context.IN_FLIGHT_LABEL`. A rule the reviewer cannot evaluate is not a
#: rule, so the two are pinned to each other by test.
NEXT_WORK_PREFERENCE = """\
CHOOSING WHAT TO DO NEXT — a preference, not a parser rule.
CONTEXT's `in_flight` line gives tasks in progress and how many hold an
unpublished candidate. While any holds one, prefer `revise` or an approval on
it over `implement` on a fresh task: finish before you start. Start new work
when nothing is in flight, when everything in flight is
blocked on something external, or when the operator asks."""

#: The second scheduling PREFERENCE: ready roadmap work before a fresh audit.
#:
#: Advisory for the same reasons `NEXT_WORK_PREFERENCE` is, and deliberately
#: NOT a policy denial. `policy.py` authorizes actions; a scheduling preference
#: refused there would park the loop instead of redirecting it, and the loop
#: would have no way to answer "then what should I have done?".
#:
#: Why it exists: an audit ADDS findings. Choosing one while the roadmap
#: already holds ready tasks moves the backlog in the wrong direction —
#: observed 2026-08-05, a synthetic audit unit was running with 15 tasks
#: ready, six of them priority 1. The reviewer makes this call with the
#: roadmap summary in front of it, so the fix is to state the preference,
#: not to hope.
#:
#: Why it never forbids: an empty or fully blocked roadmap is exactly when an
#: audit is right, and continuous mode depends on that to find new work at
#: all. A hard denial would stall the loop the moment the queue drains, so the
#: clause names the three cases where `audit` is still the correct answer.
#:
#: It lives here rather than inside `NEXT_WORK_PREFERENCE` — which orders
#: `implement`/`revise`/approval among themselves and says nothing about
#: `audit` — so one preference never has two texts to drift between. This is
#: the "separate rule with its own home" that comment refers to.
#:
#: Why it names `implement` and ONLY `implement`: the count it ranks against
#: `audit` is the READY count, and `implement` is the one directive this
#: protocol defines for a READY task. `revise` sends an already-started task
#: back to its executor and is phase-gated on top of that, so recommending it
#: for a ready task would name a directive that is invalid for exactly the
#: tasks being counted. Ordering `revise` and the approvals against fresh work
#: is `NEXT_WORK_PREFERENCE`'s job, and it stays there.
#:
#: Why the second escape hatch says "outside the roadmap" rather than "waiting
#: on a dependency": `TaskRegistry.state_of` calls a task READY only once its
#: declared `depends_on` are completed, so a ready task with an unmet declared
#: dependency does not exist and a rule phrased that way describes nothing.
#: The real case is a task the registry can schedule but a human cannot start
#: — waiting on an upstream release, an operator decision, a service that is
#: down — a blocker the graph does not model, which is why the text names it
#: as being outside the roadmap rather than inside its dependency edges. The
#: near-parallel with `NEXT_WORK_PREFERENCE`'s "blocked on something external"
#: is deliberate and the two are NOT to be unified: that one qualifies tasks
#: already in flight, this one qualifies tasks the registry calls READY, and
#: collapsing them would put one preference back into two texts.
#:
#: The numbers it depends on are rendered by `context.render_context` under
#: `context.ROADMAP_LABEL`, from `tasks.TaskRegistry.summary`. A rule the
#: reviewer cannot evaluate is not a rule, so the two are pinned by test.
AUDIT_VS_READY_PREFERENCE = """\
CHOOSING AUDIT VS READY WORK — a preference, not a parser rule.
CONTEXT's `roadmap` line gives how many tasks are ready and how many of those
are priority 1. While any task is ready, prefer `implement` on one of them
over `audit`: an audit adds findings, so auditing ahead of queued work grows
the backlog. Choose `audit` when no task is ready, when every ready task is
blocked on something outside the roadmap, or when the operator asks."""

CONTRACT_INSTRUCTIONS = (
    _RESPONSE_FORMAT + "\n\n" + NEXT_WORK_PREFERENCE + "\n\n" + AUDIT_VS_READY_PREFERENCE
)


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


def _require_str_list(field: str, value: object) -> tuple[str, ...]:
    """A non-empty list of non-empty strings, stripped, or `ContractError`.

    Entries ARE stripped here, unlike `TaskSpec.approved_paths` — nothing
    compares these strings against a git pathspec or a stored scope, they are
    read by a human and by the implementing agent, so padding is noise rather
    than meaning.
    """
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise ContractError(
            f"bad_type:{field}",
            f"'{field}' must be a non-empty list of non-empty strings",
        )
    return tuple(item.strip() for item in value)


def _parse_decomposition(raw: object) -> Decomposition:
    """The `decomposition` object, validated. See `Decomposition`.

    SHAPE only, and every part is required once the key is present at all: a
    decomposition with no steps, or with no files, is not a smaller plan — it
    is a plan that answers none of the question it was asked. Whether a
    directive needed one in the first place is `policy.authorize_directive`'s
    call, not this function's.
    """
    if not isinstance(raw, dict):
        raise ContractError(
            "bad_type:decomposition",
            "'decomposition' must be an object with 'approach', 'files' and 'steps'",
        )
    unknown = set(raw) - _DECOMPOSITION_KEYS
    if unknown:
        raise ContractError(
            "unknown_keys", f"unknown keys in 'decomposition': {sorted(unknown)}"
        )
    return Decomposition(
        approach=_require_str("decomposition.approach", raw.get("approach")),
        files=_require_str_list("decomposition.files", raw.get("files")),
        steps=_require_str_list("decomposition.steps", raw.get("steps")),
    )


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
        # Enumerates ACTIVE_DECISIONS, not `Decision`: a correction that
        # listed `ask_user` would be handing the reviewer a decision the
        # policy engine refuses unconditionally. A literal `ask_user` reply
        # never lands here — it parses, then gets the retirement denial.
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

    # Accepted on EVERY decision, deliberately: the field says "none of these
    # fitted", and the reviewer still had to send one of them, so forbidding it
    # per-decision would forbid it exactly where it is used. Validated as a
    # non-empty string and NOTHING else — never against `Decision` — see
    # `Directive.wanted_decision` for why a value naming a real decision is a
    # signal to keep rather than an error to raise. An omitted key stays None,
    # which is byte-for-byte today's behaviour for every existing reply.
    wanted_decision = None
    if data.get("wanted_decision") is not None:
        wanted_decision = _require_str("wanted_decision", data.get("wanted_decision"))

    scope_raw = data.get("scope")
    tasks_raw = data.get("tasks")
    task_id_raw = data.get("task_id")
    feedback_raw = data.get("feedback")
    commit_raw = data.get("commit")
    reviewed_raw = data.get("reviewed")
    question_raw = data.get("question")
    decomposition_raw = data.get("decomposition")

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
    if decision in NAMES_A_TASK:
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

    # Accepted on the two decisions that authorize executor work on a real
    # task, and forbidden everywhere else — a plan attached to `stop` or to an
    # approval would be a plan nothing could ever apply. Never REQUIRED here,
    # including for `implement`: see `Decomposition` for why that gate is
    # policy's.
    decomposition = None
    if decision in TASK_DECISIONS and task_id != AUDIT_TASK_ID:
        if decomposition_raw is not None:
            decomposition = _parse_decomposition(decomposition_raw)
    elif decision in TASK_DECISIONS and decomposition_raw is not None:
        # A revise of the audit pseudo-task. Refused with its own reason
        # rather than silently accepted: the audit is not a roadmap task, so
        # nothing stores or applies a plan for it (`_resolve_audit_task` mints
        # a synthetic Task the registry never sees), and its write surface is
        # bounded by `scope` + MarkdownPolicy instead. A field that parses and
        # is then dropped reads as configured while behaving as if it were not.
        raise ContractError(
            "unexpected_field",
            "'decomposition' is not allowed for the audit pseudo-task — the "
            "audit is not a roadmap task, so nothing would store or apply a "
            "plan for it; use 'scope' on `audit` to narrow it",
        )
    else:
        _forbid("decomposition", decomposition_raw, decision)

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
        # Optional, exactly like `scope` above, and for the retirement's sake:
        # the instructions no longer ask for a question, so requiring one
        # would answer a stale `ask_user` with `missing_field:question` — a
        # correction naming a field the contract no longer documents, in
        # place of the denial that says the decision itself is gone. Still
        # validated when present: an omitted question is legacy, a
        # present-but-empty one is malformed, and the two are not the same.
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
        decomposition=decomposition,
        commit_message=commit_message,
        commit_paths=commit_paths,
        reviewed=reviewed,
        question=question,
        notes=notes,
        wanted_decision=wanted_decision,
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
