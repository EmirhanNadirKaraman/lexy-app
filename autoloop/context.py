"""Automatic review context for every outgoing request.

One CONTEXT block per request, assembled from persisted state + git + the task
registry. It serves two purposes:

* **Review quality** — ChatGPT always sees the previous decision, the task it
  concerns, roadmap status, in-flight counts, git summary, validation summary
  and changed files, without any component having to remember to include them.
  The in-flight line exists because CONTRACT_INSTRUCTIONS states a scheduling
  preference (finish before start) that a reviewer cannot evaluate without
  knowing how much is already in progress and how much of it is committed but
  unpublished. The roadmap line carries the ready and priority-1 counts for
  the same reason: the audit-vs-ready preference is applied against them.

  Since 2026-08-21 (ctx-01) the in-flight summary is followed by ONE line per
  in-progress task naming it — id, candidate sha, review round, whether revise
  feedback is on record — and by a `merge_window` line carrying why merging is
  unsafe right now. Two integers were not enough to apply the preference they
  exist for: on 2026-08-21 a fresh-session reviewer read
  "2 in progress, 2 holding an unpublished candidate" and answered `implement`
  on a third task, reasoning that "the held candidates are externally blocked".
  Neither was. Nothing in the block could have told it otherwise — per-task
  state appeared nowhere, and the roadmap line's "29 blocked" two rows above is
  a count of DEPENDENCY-blocked roadmap tasks with nothing to do with the
  in-flight ones. Six reviewed, published branches sat unmerged for over a day
  behind that reading. The rows are FACTS ONLY: what to do about them stays in
  CONTRACT_INSTRUCTIONS, which is the single instruction channel.
* **Self-contained requests** — since 2026-08-18 the block also carries the
  TASK BRIEFS the two task decisions are authored from: the READY task this
  request is offering (`next_ready`) with its full description and the exact
  paths it may write, and the task currently under review (`in_review`).
  Both carry the task's stored decomposition verbatim when it has one. Every
  `implement` must carry a decomposition and every `revise` either reuses the
  stored one or replaces it (`policy._check_decomposition`), and neither is a
  decision a reviewer can make from a task id and a title — so the request
  that asks for it carries what it is asked from. See `TaskBrief`.
* **Review integrity** — the block carries the stamp (request_id, timestamp,
  head_sha, base_sha, report_sha256) that a commit/push approval must copy
  into `reviewed`; `contract.verify_review` checks the echo against these
  exact values.

`build_context` is pure given its inputs (git access aside) and returns a
dataclass; `render_context` turns it into the text block. Field labels in the
rendered block are part of the protocol — ChatGPT is told to copy them — so
change them only together with CONTRACT_INSTRUCTIONS.

`NEXT_READY_LABEL` / `IN_REVIEW_LABEL` are the exception, and deliberately: the
two scheduling preferences name the labels they read because a rule about
NUMBERS is unusable without them, whereas these sections carry their own
heading text and are read where they are written. CONTRACT_INSTRUCTIONS is
re-sent every turn and carries a hard size ceiling
(`test_contract.test_contract_stays_within_its_budget`), so a pointer that buys
nothing is not worth its per-turn cost. Renaming them is still a protocol
change; it is just not one the instructions have to be edited for.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .errors import StateCorruptError
from .git_gateway import GitGateway
from .state import LoopState, utcnow_iso
from .tasks import TRACKER_PATHS, Task, TaskRegistry, TaskState, effective_approved_paths
from .worktask import TaskExecutionStore

#: Label of the in-flight line in the rendered block. The scheduling
#: preference in `contract.CONTRACT_INSTRUCTIONS` points at this label by
#: name, so the two are coupled the way this module's docstring describes:
#: field labels are part of the protocol. `test_context.py` asserts the same
#: literal appears on both sides — a rename or a deletion here leaves the
#: contract pointing at a line that no longer exists, and the rule it states
#: cannot be followed without the numbers.
IN_FLIGHT_LABEL = "in_flight"

#: Label of the per-task in-flight rows, one per in-progress task, indented
#: under `IN_FLIGHT_LABEL`. Deliberately NOT a prefix collision hazard with it:
#: every reader (and every test) matches `"in_flight: "` or
#: `"in_flight_task: "` with the separator attached, so neither label can be
#: found inside the other.
#:
#: CONTRACT_INSTRUCTIONS does NOT name this label, and that is the point. The
#: preference it states is about the COUNTS — "while any holds one, prefer
#: revise" — and those stay on the `in_flight` line it already names. These
#: rows are the evidence a reviewer needs to tell WHICH task that is, not a
#: second rule.
IN_FLIGHT_TASK_LABEL = "in_flight_task"

#: Label of the merge-window line: whether the branch head may move right now,
#: and if not, why. Sourced by CALLING `cli._merge_window_blockers` — the same
#: predicate `auto_merge` and `merge_sweep` call, never a second copy of it.
#: A block that disagreed with the loop about whether a merge is safe would be
#: worse than no line at all, so there is exactly one implementation and this
#: is a reader of it.
MERGE_WINDOW_LABEL = "merge_window"

#: Label of the roadmap line, coupled to `contract.AUDIT_VS_READY_PREFERENCE`
#: exactly as `IN_FLIGHT_LABEL` is coupled to `contract.NEXT_WORK_PREFERENCE`.
#: That rule tells the reviewer to prefer a READY task over a fresh audit and
#: names this line as where the ready and priority-1 counts are read from, so
#: a rename or a deletion here leaves it pointing at a line that does not
#: exist. `test_context.py` asserts the same literal appears on both sides.
ROADMAP_LABEL = "roadmap"

#: Label of the section describing the task `TaskRegistry.next_ready()` would
#: pick — i.e. the READY work this request is offering. `roadmap` names that
#: task by id and title; this section is the rest of what an `implement` for it
#: has to be authored from.
NEXT_READY_LABEL = "next_ready"

#: Label of the section describing the task currently under review — the one a
#: `revise` would send back. `previous_task` names it; this section carries the
#: plan already approved for it, so a revise can reuse or replace it knowingly.
IN_REVIEW_LABEL = "in_review"


@dataclass(frozen=True)
class TaskBrief:
    """One task, as much of it as the reviewer needs to answer about it.

    Read from the `TaskRegistry` at request-build time, never from what the
    conversation happens to remember: a reviewer that was restarted, rotated
    onto a fresh conversation or switched to the fallback provider mid-run has
    no memory of a task it never saw planned, and `implement` REQUIRES a
    decomposition — approach, files, steps — which cannot be authored from an
    id and a title.

    **`description` is carried for the READY brief only, and that asymmetry is
    deliberate.** A description is unbounded (the one that motivated this rule
    was 12,020 characters) and the CONTEXT block is NOT chunked — only the
    payload's diff has a parts fallback (`packet.split_diff_into_parts`), so
    everything here rides in one message alongside a diff that may already be
    `packet.DIFF_INCLUDE_MAX_CHARS` long. Rendering the description in both
    briefs would double that cost on exactly the requests that are already the
    largest, to restate text the round under review was dispatched from. The
    review side needs the PLAN — that is what a revise reuses or reshapes — so
    that is what it carries. Do not "symmetrise" this.

    It is also NOT truncated. The reviewer cannot open the repository, so a
    shortened description is one it would silently plan around; a long request
    is the better failure.
    """

    task_id: str
    title: str
    #: `tasks.effective_approved_paths` — the task's own scope UNION the
    #: always-allowed trackers, i.e. exactly what the dispatch will authorize.
    #: Empty means no scope is authorized at all, which is not "writes
    #: nothing" but "cannot be dispatched" (see `_render_brief`).
    approved_paths: tuple[str, ...]
    #: `tasks.Task.decomposition` verbatim — the text
    #: `contract.Decomposition.render()` produced and the registry stored.
    #: Empty means no plan is on record yet.
    decomposition: str
    description: str = ""


#: `InFlightTask.record_state` values. Three outcomes, kept distinct because
#: they mean different things to a reviewer deciding what to finish: the record
#: loaded; the task is dispatched but has written no record yet; the record
#: exists and could not be read. The last one is rendered as UNKNOWN and is
#: never silently folded into either of the others.
RECORD_LOADED = "loaded"
RECORD_MISSING = "missing"
RECORD_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class InFlightTask:
    """One in-progress task, as one rendered line.

    Read from the SAME `TaskExecution` records `_in_flight_counts` already
    loads to produce its two integers — this is a rendering of data the loop
    holds, not a new source. In particular it is NOT fetched from the
    dashboard: the dashboard reads the loop's state files, so asking it would
    invert the dependency, add a second process to the hot path of every
    request, and substitute re-read files for the authoritative registry.

    **`has_revise_feedback` is a boolean and must stay one.** The underlying
    `TaskExecution.last_revise_feedback` is reviewer-authored prose of
    unbounded length; embedding it here would both blow the one-line-per-task
    budget and open a second channel of foreign text into a block whose line
    ordering is load-bearing (see `render_context` and docs/SECURITY.md S33).
    Its name says what the field IS — feedback is on record for this task —
    rather than asserting the feedback is unactioned, because the field is
    written when a `revise` is DISPATCHED: on the round answering that revise
    it names the feedback the round addressed. Stating more than that would
    re-create the false inference this whole line exists to kill.
    """

    task_id: str
    #: One of `RECORD_LOADED` / `RECORD_MISSING` / `RECORD_UNREADABLE`. The
    #: three fields below are meaningful only for `RECORD_LOADED`.
    record_state: str = RECORD_LOADED
    #: Empty when the task is dispatched but has committed nothing yet.
    candidate_sha: str = ""
    review_round: int = 0
    has_revise_feedback: bool = False


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
    #: One row per in-progress task, in registry order — the detail behind the
    #: two counts above. EMPTY when no execution store was supplied, which is
    #: the "block unchanged from today" case: with no store there is nothing to
    #: read per task, and inventing rows would be worse than the counts alone.
    in_flight_tasks: tuple[InFlightTask, ...] = ()
    #: Why the branch head may not move right now, from
    #: `cli._merge_window_blockers`. `None` means the question was not asked
    #: (no config supplied) OR could not be answered — see
    #: `merge_window_error` for which — and renders no line / an unknown line
    #: respectively. `()` means the window is OPEN. Non-empty carries the
    #: reasons, each normalised to a single line.
    merge_window_reasons: tuple[str, ...] | None = None
    #: Why the merge-window question could not be answered, when it could not.
    #: Non-empty implies `merge_window_reasons is None`. Rendering "open" on a
    #: failed check would be the one wrong answer here, so a failure says so.
    merge_window_error: str = ""
    #: The READY task this request offers, or `None` when the queue is empty.
    #: The same task `roadmap`'s "next ready:" names.
    next_ready: TaskBrief | None = None
    #: The task under review, or `None` when there is none the registry knows.
    #: An AUDIT round is the ordinary `None` case: audit units are synthetic
    #: (`orchestrator._resolve_audit_task`) and never enter the registry.
    in_review: TaskBrief | None = None


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
) -> tuple[int, int | None, tuple[InFlightTask, ...]]:
    """(tasks in progress, how many of those hold an unpublished candidate,
    one row per in-progress task).

    The two counts are what CONTRACT_INSTRUCTIONS' scheduling preference is
    applied against and are UNCHANGED by the rows: `holding` is still derived
    from exactly the records this loop already read, so adding detail cannot
    move a number an existing consumer depends on.

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

    **A record that will not load produces a ROW, not a gap.** The loop used to
    return at the first `StateCorruptError`, which was fine when the answer was
    one integer; a task silently missing from a per-task listing reads as "not
    in flight", which is the opposite of true. So the scan runs to completion,
    the unreadable task is listed as unknown, and the aggregate still collapses
    to `None` exactly as before — unknown stays unknown at BOTH levels.
    """
    in_progress = [
        t for t in registry.all_tasks() if registry.state_of(t.id) is TaskState.IN_PROGRESS
    ]
    if executions is None:
        return len(in_progress), None, ()
    rows: list[InFlightTask] = []
    for task in in_progress:
        try:
            execution = executions.load(task.id)
        except StateCorruptError:
            rows.append(InFlightTask(task_id=task.id, record_state=RECORD_UNREADABLE))
            continue
        if execution is None:
            # Dispatched, nothing written yet. Knowable, and not the same
            # thing as unreadable: it holds no candidate, so it counts as 0.
            rows.append(InFlightTask(task_id=task.id, record_state=RECORD_MISSING))
            continue
        rows.append(
            InFlightTask(
                task_id=task.id,
                record_state=RECORD_LOADED,
                candidate_sha=execution.candidate_sha,
                review_round=execution.review_round,
                has_revise_feedback=bool(execution.last_revise_feedback),
            )
        )
    holding: int | None
    if any(row.record_state == RECORD_UNREADABLE for row in rows):
        holding = None
    else:
        holding = sum(1 for row in rows if row.candidate_sha)
    return len(in_progress), holding, tuple(rows)


def _one_line(text: str) -> str:
    """Collapse whitespace so one reason occupies exactly one line.

    The reasons interpolate `GitError` messages, which carry git's stderr and
    can be multi-line. A line-oriented block whose lines are not lines is how a
    reader — or a first-match parse of the stamp above — gets confused.
    """
    return " ".join(str(text).split())


def _merge_window(config, git) -> tuple[tuple[str, ...] | None, str]:
    """(reasons the merge window is shut, why the question went unanswered).

    `((), "")` — rendered "open" — is a real answer; `(None, why)` is not one
    and says so. Never the other way round: claiming the window is open when
    the check failed is the single wrong answer this line can give.

    CALLS `cli._merge_window_blockers`, the same predicate `auto_merge` and
    `merge_sweep` call, with the SAME deferred-import idiom they use (`cli`
    imports `orchestrator`, which imports this module, so the import cannot be
    top-level). `git` is forwarded for the reason `_candidate_publication`'s
    docstring gives: the CLI's own gateway is built against `Path.cwd()`, which
    is the operator's shell for the `merge-window` command and something else
    entirely inside a loop process.

    The gate's NOTES are deliberately dropped. They are unbounded advisory
    prose about records being written off rather than respected, the CONTEXT
    block is not chunked, and the question this line answers is what HOLDS the
    merge — which is the reasons. `merge-window` and `auto_merge` both still
    report the notes to their own audiences.

    Broad `except` on purpose, and it is the same judgement `_in_flight_counts`
    already makes about corrupt records: this runs while every outgoing request
    is assembled, and `_merge_window_blockers` reaches the task store, the
    state store and git. A roadmap file that will not parse, a gateway that
    does not implement `remote_ref_sha`, a git binary having a bad day — none
    of those are reasons to destroy a review round, and all of them are
    reasons to say the window's state is unknown.
    """
    if config is None:
        return None, ""
    from . import cli  # deferred: cli -> orchestrator -> context would cycle

    try:
        reasons, _notes = cli._merge_window_blockers(config, None, git)
    except Exception as exc:  # deliberately broad — see the docstring
        return None, _one_line(f"{type(exc).__name__}: {exc}")
    return tuple(_one_line(reason) for reason in reasons), ""


def _brief(task: Task, trackers: tuple[str, ...], with_description: bool) -> TaskBrief:
    """One `TaskBrief` off a registry task. See `TaskBrief` for why the
    description is carried on one side only."""
    return TaskBrief(
        task_id=task.id,
        title=task.title,
        approved_paths=effective_approved_paths(task.approved_paths, trackers),
        decomposition=task.decomposition,
        description=task.description if with_description else "",
    )


def _in_review_brief(
    state: LoopState, registry: TaskRegistry, trackers: tuple[str, ...]
) -> TaskBrief | None:
    """The task under review, when the registry knows one.

    `None` in three cases, all ordinary rather than exceptional: nothing has
    been dispatched yet, the round was an AUDIT (its unit id is synthetic and
    normally absent from the registry — see `orchestrator._resolve_audit_task`),
    or the task under review IS the next ready task. The last one is dropped
    because `next_ready` already renders it with strictly more detail, and one
    task rendered twice in one block invites the reviewer to read the shorter
    copy as a second, differently-scoped task.
    """
    task_id = (state.current_task or {}).get("task_id")
    if not task_id or not registry.has(task_id):
        return None
    task = registry.get(task_id)
    ready = registry.next_ready()
    if ready is not None and ready.id == task.id:
        return None
    return _brief(task, trackers, with_description=False)


def build_context(
    state: LoopState,
    git: GitGateway,
    registry: TaskRegistry,
    request_id: str,
    payload: str,
    executions: TaskExecutionStore | None = None,
    trackers: tuple[str, ...] = TRACKER_PATHS,
    config=None,
) -> ReviewContext:
    """Assemble the block. `trackers` defaults to `tasks.TRACKER_PATHS`, the
    same reviewed constant `effective_approved_paths` defaults to and the only
    value `orchestrator._tracker_paths` ever returns, so the scope shown to the
    reviewer is the scope the dispatch will actually authorize. It is a
    parameter for the same reason it is one there: so a test can state a list
    explicitly instead of asserting against whatever the repository ships.

    `config` is the `AutoloopConfig` the merge-window check reads its state
    directory and task store from, and is OPTIONAL for the same reason
    `executions` is: not every caller holds one, and a block without the line
    is honest where a block asserting "open" on an unasked question is not.
    Omitting it renders exactly the block this function rendered before the
    line existed.
    """
    task = state.current_task or {}
    in_flight_count, candidate_count, in_flight_tasks = _in_flight_counts(registry, executions)
    merge_reasons, merge_error = _merge_window(config, git)
    ready_task = registry.next_ready()
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
        in_flight_tasks=in_flight_tasks,
        merge_window_reasons=merge_reasons,
        merge_window_error=merge_error,
        next_ready=(
            None
            if ready_task is None
            else _brief(ready_task, trackers, with_description=True)
        ),
        in_review=_in_review_brief(state, registry, trackers),
    )


def _fenced(kind: str, task_id: str, text: str) -> list[str]:
    """`text` between two named markers, on its own lines and UNINDENTED.

    The markers name both the kind and the task, so two fenced sections in one
    block cannot be misread as one; the text is not re-indented because it is a
    verbatim record (a description an operator wrote, or the exact
    `contract.Decomposition.render()` output the registry stored) and
    re-indenting it would make the reviewer's copy differ from the loop's.
    """
    return [
        f"  --- begin {kind} of {task_id} ---",
        text,
        f"  --- end {kind} of {task_id} ---",
    ]


def _render_brief(label: str, brief: TaskBrief, note: str) -> list[str]:
    """One task brief as block lines. See `TaskBrief` for the field choices."""
    paths = ", ".join(brief.approved_paths) or (
        "(none authorized — this task cannot be dispatched until it is scoped, "
        "so do not plan work for it yet)"
    )
    lines = [
        f"{label}: {brief.task_id} — {brief.title} ({note})",
        f"  approved_paths (its own scope plus the always-allowed trackers): {paths}",
    ]
    if brief.decomposition:
        lines.append("  approved decomposition, exactly as recorded:")
        lines += _fenced("decomposition", brief.task_id, brief.decomposition)
    else:
        lines.append(
            "  approved decomposition: (none on record — the directive that "
            "starts this task must carry one)"
        )
    if brief.description:
        lines.append("  full task description:")
        lines += _fenced("description", brief.task_id, brief.description)
    return lines


def _render_in_flight_task(row: InFlightTask) -> str:
    """One in-progress task as exactly ONE line, indented under `in_flight`.

    One line per task is the budget (see the module docstring): the block is
    re-sent every round and is not chunked, so a section that grows with the
    size of a record would be paid for on every request forever.
    """
    if row.record_state == RECORD_UNREADABLE:
        detail = "unknown — its execution record could not be read"
    elif row.record_state == RECORD_MISSING:
        detail = "no execution record yet — no candidate, no review round, no revise feedback"
    else:
        candidate = row.candidate_sha[:12] if row.candidate_sha else "none committed yet"
        feedback = (
            "revise feedback on record"
            if row.has_revise_feedback
            else "no revise feedback on record"
        )
        detail = f"candidate {candidate}, review round {row.review_round}, {feedback}"
    return f"  {IN_FLIGHT_TASK_LABEL}: {row.task_id} — {detail}"


def _render_merge_window(ctx: ReviewContext) -> list[str]:
    """The merge-window line, or no line at all when it was never asked.

    Three renderings, and the difference between them is the whole point:
    OPEN (the check ran and found nothing), SHUT plus the reasons it found, and
    UNKNOWN plus why it could not answer. A failed check never renders as open.
    """
    if ctx.merge_window_error:
        return [f"{MERGE_WINDOW_LABEL}: unknown — {ctx.merge_window_error}"]
    if ctx.merge_window_reasons is None:
        return []
    if not ctx.merge_window_reasons:
        return [f"{MERGE_WINDOW_LABEL}: open"]
    return [f"{MERGE_WINDOW_LABEL}: shut — " + "; ".join(ctx.merge_window_reasons)]


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
    lines = [
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
        f"{ROADMAP_LABEL}: {ctx.roadmap_status}",
        f"{IN_FLIGHT_LABEL}: {ctx.in_flight_count} in progress, {candidates}",
    ]
    # The rows and the merge-window line, STRICTLY BELOW the summary they
    # detail and strictly ABOVE the briefs. They state facts and give no
    # instruction — what to do about a held candidate is
    # `contract.NEXT_WORK_PREFERENCE`'s job, and a second instruction channel
    # inside CONTEXT would compete with it (same rule as the note below).
    lines += [_render_in_flight_task(row) for row in ctx.in_flight_tasks]
    lines += _render_merge_window(ctx)
    # STRICTLY AFTER every stamp line, and that ordering is load-bearing rather
    # than cosmetic. The briefs below embed text nobody here wrote — an
    # operator's task description, a stored plan — so a description containing
    # a line like `report_sha256: 0000...` renders a second stamp-shaped line.
    # Keeping the real stamp first means a first-match read of the block still
    # finds the real value (`test_orchestrator.extract_stamp` does exactly
    # that), and `contract.verify_review` refuses any echo that does not match
    # what was recorded for the request — so the residue is a denied approval,
    # never a laundered one. See docs/SECURITY.md S33.
    # The notes below DESCRIBE what each section is, and deliberately give no
    # instruction: what to do with a ready task is CONTRACT_INSTRUCTIONS' job,
    # and a second instruction channel inside the context block would compete
    # with it — including with the `smoke_test` payload, which tells the
    # reviewer that no repository work of any kind will be accepted.
    if ctx.next_ready is not None:
        lines += _render_brief(
            NEXT_READY_LABEL,
            ctx.next_ready,
            "the READY task the roadmap line names",
        )
    if ctx.in_review is not None:
        lines += _render_brief(
            IN_REVIEW_LABEL,
            ctx.in_review,
            "the task under review — the plan below is what stands unless it is replaced",
        )
    return "\n".join(lines)
