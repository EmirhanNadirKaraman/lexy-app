"""Persistent operator-facing blocker records.

Continuous mode (`cli.py`'s `run --continuous`) is multi-track: when a park
site in `orchestrator.py` is classified `task_fatal` (see `_to_needs_user`'s
`kind` parameter and the classification table in its module docstring),
only the ONE task at fault is set aside — the loop keeps working whatever
else is READY (`tasks.TaskRegistry.block`/`unblock`). A `loop_fatal` park
still stops the loop outright, exactly as every park did before this module
existed.

Either way — task_fatal or loop_fatal — the operator-facing question that
would have been shown at the park site is durably persisted here, one JSON
file per blocker, so it survives whatever the loop does next (clearing the
session state for a task_fatal park, or the loop simply exiting for a
loop_fatal one) and can be listed/answered later:

    python -m autoloop blockers            # list open ones (--all for resolved too)
    python -m autoloop answer <id> "..."   # resolve + unblock the task if task_fatal

When more than one is open at once, WHICH of them is "the" blocker is a
decision this module makes for everyone: `primary_sort_key` (severity, then
recency, then id) and the `BlockerStore.primary_blocker` /
`open_blockers_by_severity` pair beside `open_blockers`. Every caller that
needs a single blocker reads it from here; a second sort anywhere else is
the bug that rule exists to prevent.

Same crash-safety rule as every other store in this package
(`worktask.TaskExecutionStore`, `tasks.TaskStore`): a corrupt record RAISES
(`StateCorruptError`) rather than silently reading as absent. Reading a
blocker as "not there" when it actually failed to decode would let the
operator believe a problem was resolved (or never existed) when it did not —
exactly backwards for a record whose entire purpose is not losing track of
open questions.

Autonomous recovery (halt-02, 2026-08-25) lives here too — `HARD_HALT_CODES`,
`AUTONOMOUS_RECOVERIES` and `autonomous_recovery`. It is a TABLE, in this
module rather than in `orchestrator.py`, for two reasons. The table is data
about blocker codes and this is the blocker-code module; and the orchestrator's
source is read as evidence by `test_transport_recovery.py`'s retired-code walk,
so a table of code strings there would be indistinguishable from an emitter.
`orchestrator._to_needs_user` is the only caller.

halt-03 (2026-08-25) adds the SECOND half of that table,
`STALE_RECORD_RECOVERIES`: six codes whose fault is not a transport fault at
all but a RECORD the loop is still holding that no longer describes anything —
a base pinned behind the branch head, an approval binding naming a candidate
that moved, a packet that can never bind the review it is standing in front of,
a session pointer at an audit nobody minted. Their remedy is one action,
`RECOVER_BY_REBUILDING_AT_HEAD`: archive the stale record, rebuild at the
current head, re-dispatch. That is
exactly what an operator does by hand today, which is why the median park on
`task_base_behind_head` was 0.54h across 15 parks — seeing it IS deciding it.
The two dicts are merged into `AUTONOMOUS_RECOVERIES`, which stays the ONE
table `autonomous_recovery` reads, so the hard-halt refusal cannot be bypassed
by looking a code up in the newer half directly.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .errors import StateCorruptError, StateError
from .state import utcnow_iso

#: Sentinel `task_id` for a blocker not tied to any specific registry task
#: (e.g. a login expiry, or an iteration-budget exhaustion mid corrective
#: re-prompt — the loop-level failures every park used to be before
#: task_fatal existed). Never a real task id: `tasks._ID_RE` forbids
#: parentheses, so this can never collide with one.
NO_TASK = "(loop)"

#: `code` for a task the loop could NOT return to scheduling after a round the
#: environment destroyed (strand-01, 2026-08-24). Written by
#: `orchestrator.Orchestrator._reconcile_stranded_tasks` when the record is
#: outside the narrow safe shape — it holds a candidate, a reviewer has already
#: seen a round, its attempt budget is spent, or it cannot be read at all.
#:
#: **Recorded WITHOUT parking, which no other code here is.** Every other
#: blocker is minted by `orchestrator._to_needs_user` or `_to_fault_stop`, and
#: both of those stop something: a park holds the session open for an answer, a
#: fault stop ends the run. Neither is right here — the loop is working, on
#: other tasks, and the failure being reported is one task's ABSENCE from the
#: queue. So the record is filed on its own and the loop carries on. Two
#: consequences worth knowing before adding a second code like this:
#:
#:   * it is invisible to `test_m1_hardening._emitted_blocker_codes`, which
#:     AST-walks those two emitters only, so it is deliberately absent from
#:     `cli._RESOLUTION_PRECONDITIONS` (that dict's keys must all be emitted
#:     codes) and an operator can answer it with no precondition recheck;
#:   * `answer` will report `task_not_blocked` while resolving it, because the
#:     task is `in_progress` rather than `blocked` — this code never changes a
#:     task's status. The question text says so and names `release` as the
#:     command that moves the task.
STRANDED_AFTER_FAULT = "stranded_after_environment_fault"


# ---- autonomous recovery (halt-02, 2026-08-25) ------------------------------
#
# Measured 2026-08-24 over 131 resolved blocker records: 411.8h of the loop's
# life was spent parked, and 30.6% of the window where every round is timed was
# the loop waiting for an operator. A transport or environment fault is the
# worst of those waits, because there is nothing to DECIDE — the loop already
# knows the remedy, it simply refuses to move without a human saying so.
#
# Autonomous mode (`config.AutonomyConfig`, default OFF) changes that for the
# codes named below and for NOTHING else. Two stages, in order:
#
#   1. RETRY the recovery path that already exists, bounded, durably counted;
#   2. when that path is exhausted, SET THE TASK ASIDE — park `task_fatal`
#      naming the task in flight, which is the existing quarantine
#      `cli._handle_parked_task` already knows how to work past — so the loop
#      keeps going on the rest of the roadmap instead of stopping.
#
# Stage 2 is the point. Stage 1 is only worth doing where a recovery path
# genuinely exists, which is why `max_attempts` is 0 for three of the SIX
# entries and the reason is written into each entry rather than left implied.
#
# Six entries, not seven: halt-02 named seven codes, and one of them — the
# conversation-rotation failure brw-15 retired — has no producer left in
# `orchestrator.py`. A code no live provider can raise is REMOVED rather than
# automated, so it is absent here by decision, and
# `test_autonomous_recovery.py` asserts both that absence and that every code
# that IS here is one the orchestrator can actually emit.


#: The parks autonomous mode must NEVER touch, whatever else is configured.
#:
#: Each one means an agent wrote outside its worker repository, or the checkout
#: is not what the loop believes it to be. Continuing past any of them corrupts
#: the tree every later task builds on, so "retry and step aside" is exactly the
#: wrong answer: stepping aside from a compromised checkout leaves the
#: compromise in place and starts the next task on top of it.
#:
#: An ALLOWLIST is what actually enforces this (`AUTONOMOUS_RECOVERIES` below);
#: this frozenset is the second, redundant lock — `autonomous_recovery` refuses
#: a member outright, and a test asserts the two sets are disjoint, so adding a
#: hard halt to the table is a failing test rather than a silent automation.
HARD_HALT_CODES = frozenset({
    "checkout_escape_detected",
    "worker_isolation_violation",
    "primary_checkout_dirty",
    "approved_path_symlink_traversal",
    "prompt_integrity_mismatch",
})

#: Re-enter the phase the park itself declares resumable (`resume_phase`) —
#: precisely what `run --retry` does, and the loop's own statement that this
#: session can continue by stepping that phase again.
RECOVER_BY_RESUMING = "resume_phase"

#: Re-issue the SAME request id (`run --resubmit`): clear the request's
#: `send_attempted` mark and re-enter `submitting`. The one action that
#: knowingly risks a duplicate, and it is authorized for exactly one code.
RECOVER_BY_RESUBMITTING = "resubmit"

#: No in-process recovery exists, so the recovery path is exhausted the moment
#: the fault is raised and stage 2 fires immediately. NOT a synonym for "do
#: nothing": setting the task aside and continuing is the whole remedy for
#: these, and it is the half that was missing.
RECOVER_UNAVAILABLE = "none"

#: ARCHIVE the stale record this park is holding, rebuild at the CURRENT head,
#: and re-dispatch (halt-03, 2026-08-25). The one action every entry in
#: `STALE_RECORD_RECOVERIES` takes, and the reason that dict needs no second
#: verb: the six codes differ only in WHICH record is stale, which is what
#: `AutonomousRecovery.stale_record` names.
#:
#: Distinct from `RECOVER_BY_RESUMING` in the thing that makes it safe to
#: automate. A resume is a HOPE that a transient fault has passed, so it is
#: bounded by a retry budget and nothing else; a rebuild REMOVES the record
#: that caused the fault, so the identical fault cannot recur off the same
#: record. Every rebuild archives rather than deletes — the execution record
#: goes to `executions/archive/` and its worker to `quarantine/` through
#: `worktask.retire_execution`, and a session pointer is written into the
#: transcript before it is cleared — so "no operator step" never means "no
#: record of what was discarded".
RECOVER_BY_REBUILDING_AT_HEAD = "rebuild_at_head"


# ---- WHICH record a rebuild archives ----------------------------------------
#
# One value per shape, never a catch-all, because the shapes are not
# interchangeable and treating them as one is how a rebuild destroys work.
# `task_base_behind_head`'s record holds a reviewed candidate and has to go
# through recut-01's five refusals before anything moves;
# `push_candidate_stale`'s record is an APPROVAL BINDING and archiving the
# execution beneath it would discard a live candidate over a stale pointer.
# `orchestrator._autonomous_rebuild` dispatches on these and REFUSES an
# unrecognised one, so a future entry that forgets to name its record parks
# exactly as the loop parks today rather than falling into someone else's
# handler.

#: `TaskExecution` on disk: its `task_base_sha` is behind the branch head and
#: the reviewed candidate could not be carried past it. Archived through
#: `release_task_to_pending(move=registry.recut)` — recut-01's path, with
#: recut-01's refusals and recut-01's durable `MAX_TASK_RECUTS` bound.
STALE_EXECUTION_RECORD = "execution_record"

#: A postcommit / changeset APPROVAL BINDING naming a candidate that is no
#: longer this task's current one, or that no longer resolves at all. Dropped;
#: the execution record underneath is not touched.
STALE_PUSH_BINDING = "push_binding"

#: The PACKET standing between an operator-queued changeset review
#: (`LoopState.changeset`) and the reviewer: `LoopState.outbox` does not carry
#: the four identifiers an approval binds by, so no round spent on it could ever
#: publish. The queue entry is KEPT and the packet is rebuilt around it
#: (`orchestrator._rebuild_changeset_packet_at_head`) — dropping the entry
#: instead would send the unbindable payload as an ordinary unbound request and
#: leave the operator's candidate unpublishable for the rest of the session,
#: which is discarding the review rather than rebuilding it.
STALE_QUEUED_REVIEW = "queued_review"

#: `LoopState.current_task` pointing at an audit pseudo-task with no audit unit
#: on record, so the `revise` answering it names nothing. Dropped; the next
#: `audit` mints a fresh unit at the current head.
STALE_AUDIT_POINTER = "audit_pointer"

#: The loop's own half-finished ROUND (`last_response` / `pending_request`)
#: after a `StateError` proved the session's bookkeeping inconsistent. Dropped
#: so the next `ready` step rebuilds the round from the current head. The only
#: rebuild that names no durable record, and therefore the only one whose
#: blocker is NOT closed by the rebuild itself — see `orchestrator.
#: _discard_inconsistent_round`.
STALE_SESSION_ROUND = "session_round"


@dataclass(frozen=True)
class AutonomousRecovery:
    """What autonomous mode does about one blocker code, and why.

    `max_attempts` counts RETRIES, not occurrences: at 2, the first and second
    occurrence of the condition each re-enter the recovery path and the third
    sets the task aside. At 0 the first occurrence sets it aside. A REBUILD is
    counted the same way, and at 1: rebuilding the same record twice inside one
    open episode means the first rebuild did not take, and a second identical
    attempt buys no new evidence.

    `stale_record` is required for `RECOVER_BY_REBUILDING_AT_HEAD` and empty for
    every other action. Empty is the fail-closed value: `orchestrator.
    _autonomous_rebuild` refuses a record kind it does not recognise, so an
    entry that names none rebuilds nothing and the loop parks as it does today.
    """

    code: str
    action: str
    max_attempts: int
    why: str
    stale_record: str = ""


#: halt-02's half: a TRANSPORT or ENVIRONMENT fault, answered by re-entering the
#: recovery path that already exists and then stepping the task aside.
TRANSPORT_RECOVERIES: dict[str, AutonomousRecovery] = {
    entry.code: entry
    for entry in (
        AutonomousRecovery(
            code="login_expired",
            action=RECOVER_BY_RESUMING,
            max_attempts=2,
            why=(
                "The park already carries the phase it was raised in as "
                "`resume_phase`, and `run()` drops the client before parking — "
                "so re-entering that phase builds a fresh client and re-reads "
                "the session, which is the entire manual remedy minus the wait "
                "for a human. Nothing is sent by the re-entry that was not "
                "about to be sent anyway."
            ),
        ),
        AutonomousRecovery(
            code="git_unavailable_in_ready",
            action=RECOVER_BY_RESUMING,
            max_attempts=2,
            why=(
                "Raised while BUILDING a request, before one exists: the outbox "
                "is intact and nothing has been sent, which is why the park is "
                "retryable in the first place. Re-entering `ready` re-runs the "
                "context build against a git that may simply have been busy "
                "(an index lock, a concurrent fetch)."
            ),
        ),
        AutonomousRecovery(
            code="submission_ambiguous",
            action=RECOVER_BY_RESUBMITTING,
            max_attempts=1,
            why=(
                "The one code whose automation TRADES a risk rather than "
                "removing one: acceptance is unknown, so re-issuing may post a "
                "duplicate. Autonomous mode takes that trade deliberately — a "
                "possible duplicate request beats a stopped loop — and takes it "
                "ONCE, because the second identical resend buys no new evidence "
                "and doubles the duplicate. Still reachable: "
                "`codex.app_server_conversation` declines `idempotent_submit` "
                "and reconciles an appended-but-unanswered turn to False."
            ),
        ),
        AutonomousRecovery(
            code="worker_environment_drift",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "The park declares no `resume_phase` and the round's own "
                "`last_response` is cleared before it, so there is no phase to "
                "step again. The existing remedy is an operator repairing the "
                "shared git environment (a hook, `core.hooksPath`, a url "
                "rewrite) — the loop must not undo any of those on its own. So "
                "the recovery path is empty and the task is set aside at once. "
                "NOTE the classification this overrides: the park site argues "
                "loop_fatal because the drift affects every task, and under "
                "autonomous mode each task is instead set aside in turn."
            ),
        ),
        AutonomousRecovery(
            code="publisher_url_drift",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "SECURITY-SHAPED, and the reason `max_attempts` is 0 rather "
                "than small. The only recovery is `reprovision-publisher "
                "--confirm`, an operator's confirmation that a NEW push "
                "destination is correct; a loop that could take it would be a "
                "loop that can redirect its own publication. Re-running the "
                "snapshot-vs-live comparison in the same process is not a "
                "recovery either, it is the identical read seconds later. "
                "Nothing is published, ever, on this path."
            ),
        ),
        AutonomousRecovery(
            code="crash_reconciliation_ambiguous",
            action=RECOVER_UNAVAILABLE,
            max_attempts=0,
            why=(
                "`reconcile_after_crash` is deterministic over an intent the "
                "park deliberately does NOT clear, so a re-run returns "
                "AMBIGUOUS again by construction — a retry here is theatre that "
                "costs a reviewer round. Already `task_fatal` at its park site, "
                "so autonomous mode changes nothing about it except to state "
                "that plainly and to hold it to the same table as the rest."
            ),
        ),
    )
}


# ---- halt-03's half: a STALE RECORD, rebuilt at the current head -------------
#
# MEASURED 2026-08-24 over 131 resolved blocker records: `task_base_behind_head`
# is the second largest cause of parked time — 15 parks, 18.5h, median 0.54h.
# The median is the tell. A park that is cleared as fast as it is seen is not a
# decision anybody makes; the operator reads the blocker, archives the execution
# record the question NAMES, and the loop cuts the task again at the current
# head. Every code below has that shape: the loop already knows which record is
# stale, already names the remedy in its own park text, and simply refuses to
# perform it.
#
# All six are `max_attempts=1`. The budget is not the interesting bound here —
# a rebuild removes the record that caused the fault, so the identical fault
# cannot recur off the same record — and where a durable bound IS needed it is
# the one that already exists: `STALE_EXECUTION_RECORD` goes through
# `registry.recut`, so it is capped per task by `orchestrator.MAX_TASK_RECUTS`
# across episodes, not merely within one.
STALE_RECORD_RECOVERIES: dict[str, AutonomousRecovery] = {
    entry.code: entry
    for entry in (
        AutonomousRecovery(
            code="task_base_behind_head",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_EXECUTION_RECORD,
            why=(
                "The park's OWN text already names the remedy — 'archive "
                ".autoloop/executions/<task>.json to start fresh at the current "
                "head' — and recut-01 already implements exactly that, "
                "refusals included. So this rebuilds through "
                "`release_task_to_pending(move=registry.recut)` rather than "
                "archiving anything itself: a published candidate is refused, a "
                "candidate with a verdict still in flight is refused, an "
                "unreadable record is refused, and the cut is charged to "
                "`recut_count` so `MAX_TASK_RECUTS` caps it per task. A second "
                "archival mechanism would have had to re-earn all four."
            ),
        ),
        AutonomousRecovery(
            code="push_candidate_stale",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_PUSH_BINDING,
            why=(
                "The park says it in its own words: 'a later round advanced it, "
                "or the execution record is gone'. The stale thing is the "
                "APPROVAL BINDING, and the execution record underneath it may "
                "hold a live candidate a reviewer is about to approve — so this "
                "drops the binding and the ledger entries that could re-resolve "
                "it, and touches no record on disk. The remedy the park asks "
                "for, 're-review the current state before approving again', is "
                "then simply the next round."
            ),
        ),
        AutonomousRecovery(
            code="push_candidate_unresolvable",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_PUSH_BINDING,
            why=(
                "The same stale record as `push_candidate_stale`, reached by the "
                "other road: the approved sha does not resolve at all. TWO park "
                "sites emit it — the task push, which names a task, and the "
                "changeset push, which names none — so the rebuild has to handle "
                "a binding with no task behind it, and does: it drops the queued "
                "changeset instead, after writing its identifiers to the "
                "transcript. Nothing is pushed on this path, ever."
            ),
        ),
        AutonomousRecovery(
            code="state_inconsistent",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_SESSION_ROUND,
            why=(
                "The one entry whose stale record is the loop's OWN round rather "
                "than anything on disk, and the one that has to be bounded by a "
                "budget rather than by removal — the underlying inconsistency "
                "can outlive the round that tripped over it, so its blocker is "
                "deliberately left OPEN by the rebuild and closed only by a step "
                "that afterwards COMPLETES. A second occurrence with no "
                "completed step in between therefore finds the allowance spent "
                "and parks. It also refuses to fire at all for the corrupt "
                "subclass: `StateCorruptError` reaches this same handler, and "
                "rebuilding a round on top of a store that cannot be READ is the "
                "fail-open this whole design is built to avoid, so the park site "
                "passes `recoverable=False` for it."
            ),
        ),
        AutonomousRecovery(
            code="audit_revise_no_record",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_AUDIT_POINTER,
            why=(
                "A `revise` of the audit pseudo-task while `state.current_task` "
                "holds no audit unit id: the directive names a record that was "
                "never minted, and the park's own remedy is 'Run `audit` first'. "
                "An audit unit is synthetic and per-iteration, so there is "
                "nothing to salvage and nothing to return to a queue — dropping "
                "the pointer and telling the reviewer to run `audit` IS the "
                "rebuild, and the fresh unit is cut at the current head by "
                "construction."
            ),
        ),
        AutonomousRecovery(
            code="changeset_binding_missing",
            action=RECOVER_BY_REBUILDING_AT_HEAD,
            max_attempts=1,
            stale_record=STALE_QUEUED_REVIEW,
            why=(
                "A queued changeset review whose PACKET cannot carry the four "
                "identifiers an approval binds by. It is raised BEFORE anything "
                "is sent and refuses every round for as long as the payload "
                "stands, so this is the one code here that halts the loop "
                "indefinitely rather than costing it a round. The stale record "
                "is the packet, never the queue entry: `build_changeset_packet` "
                "always stamps the four identifiers and `review-changeset` sets "
                "no `outbox_diff`, so the queued packet always binds and this "
                "fault can only be raised once something ELSE has taken the "
                "outbox (a corrective re-prompt, a plan request, a later task "
                "review packet). So the entry is kept and the packet is rebuilt "
                "around it, which is the park's own remedy — 're-queue with "
                "`review-changeset`' — performed rather than requested. Dropping "
                "the entry, as the first cut of this did, sent the unbindable "
                "payload unbound and left the operator's candidate "
                "unpublishable: a review discarded, not rebuilt."
            ),
        ),
    )
}


#: THE table `autonomous_recovery` reads — halt-02's transport half and halt-03's
#: stale-record half, merged. One lookup, one hard-halt refusal: a caller that
#: consulted `STALE_RECORD_RECOVERIES` directly would bypass the refusal in
#: `autonomous_recovery`, which is why nothing does.
AUTONOMOUS_RECOVERIES: dict[str, AutonomousRecovery] = {
    **TRANSPORT_RECOVERIES,
    **STALE_RECORD_RECOVERIES,
}


def autonomous_recovery(code: str) -> AutonomousRecovery | None:
    """What autonomous mode does about `code`, or `None` for "park exactly as
    the loop parks today".

    `None` is the answer for every code that is not in the table, for a hard
    halt, and for an empty/unknown one — the fail-closed direction, and the
    reason this is a function rather than a bare dict lookup at the call site:
    the hard-halt refusal cannot be forgotten by a caller that only remembers
    the dict.
    """
    if code in HARD_HALT_CODES:
        return None
    return AUTONOMOUS_RECOVERIES.get(code)


@dataclass
class Blocker:
    id: str                 # stable: f"blk-{task_id}-{n:03d}"
    task_id: str
    kind: str                # "task_fatal" | "loop_fatal"
    code: str                # short machine slug, e.g. "attempt_count_ceiling"
    question: str            # the operator-facing text (what _to_needs_user was given)
    detail: str              # extra context (paths, shas, reasons)
    phase: str               # loop phase when it happened
    created_at: str
    resolved_at: str | None = None
    answer: str | None = None
    #: How many times this exact condition has re-parked. A restart or retry
    #: that hits the same wall must UPDATE this record, never mint a second
    #: one — otherwise a crash-retry loop silently fills the queue with
    #: duplicates of one problem and the operator cannot see how many
    #: distinct things are actually wrong.
    recurrences: int = 1
    last_seen_at: str = ""
    #: The loop session that parked this. Recorded so "does this blocker
    #: belong to a session that has since been retired?" is a machine question
    #: rather than a timestamp inference — the only way to answer it before
    #: this field existed was to match `created_at` against a transcript line,
    #: by hand. Empty on records written before this field.
    session_id: str = ""
    #: Set ONLY by `archive_stale`. Distinct from `answer` on purpose: `answer`
    #: means "an operator responded to this question", and writing text there
    #: to clear a dead blocker would forge exactly the operator confirmation
    #: `_RESOLUTION_PRECONDITIONS` exists to require.
    archived_reason: str = ""


#: Severity rank for primary selection — LOWER is more urgent. `loop_fatal`
#: means the loop cannot continue at all; `task_fatal` means one task is
#: parked and the loop works past it, so no number of task_fatal records is
#: as urgent as a single loop_fatal one, whatever their timestamps.
#:
#: An UNRECOGNISED kind ranks with `loop_fatal`, not after `task_fatal` —
#: same fail-closed direction as `orchestrator._to_needs_user`'s
#: `kind="loop_fatal"` default (`docs/AUTOLOOP.md` §9c): a record whose
#: severity we cannot read is treated as the loop-stopping one rather than
#: quietly sorted below every classified record, where it would be the last
#: thing an operator saw named as primary.
_KIND_RANK = {"loop_fatal": 0, "task_fatal": 1}
_UNCLASSIFIED_KIND_RANK = _KIND_RANK["loop_fatal"]


def _created_epoch(blocker: Blocker) -> float:
    """`created_at` as a comparable instant. An unparseable or empty stamp
    reads as `-inf`, i.e. the OLDEST possible — so a record we cannot date
    never wins the "most recent" tiebreak on the strength of being unreadable."""
    try:
        parsed = datetime.fromisoformat(blocker.created_at or "")
    except (TypeError, ValueError):
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def primary_sort_key(blocker: Blocker) -> tuple[int, float, str]:
    """THE ordering for "which open blocker is *the* one" — severity first,
    then most recent, then blocker id.

    One implementation on purpose. `health`, `heartbeat`, the CLI's status
    and exhaustion reports and any future autonomous routing all read the
    same open set for different reasons, and if each sorted it its own way
    the loop could describe one situation while acting on another. Ordering
    by whatever `Path.glob` returned is not a decision at all: `(` sorts
    before `b`, so `blk-(loop)-038.json` happening to be listed first is a
    coin flip that has nothing to do with which problem is worse. Observed
    2026-08-21, when a loop_fatal `parse_budget_exhausted` and a task_fatal
    `task_base_behind_head` were open together and a recovery script picked
    the second because the glob did.

    The three keys, in order:

    1. `_KIND_RANK` — loop_fatal (and anything unclassified) before task_fatal.
    2. Most recent `created_at` first: within one severity, the newest record
       describes the state the loop actually reached.
    3. Blocker id ascending, so two records written in the same second still
       order stably. Ids are monotonic per task (`blk-<task>-<NNN>`, zero
       padded), and ascending matches `all_blockers`' documented "oldest id
       first" rather than inventing a second convention.

    This chooses WHICH blocker is primary and nothing else. What action a
    given `code` deserves is a separate question, deliberately not answered
    here."""
    return (
        _KIND_RANK.get(blocker.kind, _UNCLASSIFIED_KIND_RANK),
        -_created_epoch(blocker),
        blocker.id,
    )


def by_severity(blockers: list[Blocker]) -> list[Blocker]:
    """`blockers` in `primary_sort_key` order — most urgent first, nothing
    dropped. For callers that already hold a list (e.g. a printer handed the
    open set); everything else should go through `BlockerStore`."""
    return sorted(blockers, key=primary_sort_key)


class BlockerStore:
    """One JSON file per blocker under `directory`. Atomic temp-file +
    `os.replace` writes — same pattern as `tasks.TaskStore`.

    Filenames double as ids (`<id>.json`), so `next_id` can derive the next
    free sequence number for a task purely by listing the directory — there
    is no separate counter file that could drift out of sync with what is
    actually on disk.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, blocker_id: str) -> Path:
        return self.directory / f"{blocker_id}.json"

    def save(self, blocker: Blocker) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(blocker.id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(asdict(blocker), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def load(self, blocker_id: str) -> Blocker | None:
        path = self._path(blocker_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Blocker(**data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StateCorruptError(f"blocker record {path} is unreadable: {exc}") from exc

    def next_id(self, task_id: str) -> str:
        """`f"blk-{task_id}-{n:03d}"` for the next free `n` — scans existing
        files for `task_id` rather than keeping a separate counter."""
        prefix = f"blk-{task_id}-"
        n = 0
        if self.directory.exists():
            for path in self.directory.glob(f"{prefix}*.json"):
                suffix = path.stem[len(prefix):]
                if suffix.isdigit():
                    n = max(n, int(suffix))
        return f"{prefix}{n + 1:03d}"

    def find_open(self, task_id: str, code: str, phase: str) -> "Blocker | None":
        """An OPEN blocker for the same (task, code, phase), or None.

        Identity is the CONDITION, not the occurrence: the same task hitting
        the same failure in the same phase is one blocker seen repeatedly,
        which is what the operator needs to answer once. A resolved blocker
        is deliberately not matched — if the condition returns after being
        answered, that is genuinely new and deserves its own record."""
        for existing in self.open_blockers():
            if (existing.task_id, existing.code, existing.phase) == (task_id, code, phase):
                return existing
        return None

    def record(self, *, task_id, kind, code, question, detail, phase, now,
               session_id: str = "") -> "Blocker":
        """Idempotent upsert. Returns the existing open blocker for this
        condition with its recurrence count bumped, or a new one."""
        existing = self.find_open(task_id, code, phase)
        if existing is not None:
            from dataclasses import replace
            bumped = replace(
                existing,
                recurrences=existing.recurrences + 1,
                last_seen_at=now,
                question=question,
                detail=detail,
            )
            self.save(bumped)
            return bumped
        fresh = Blocker(
            id=self.next_id(task_id), task_id=task_id, kind=kind, code=code,
            question=question, detail=detail, phase=phase, created_at=now,
            last_seen_at=now, session_id=session_id,
        )
        self.save(fresh)
        return fresh

    def archive_stale(self, blocker_id: str, reason: str) -> "Blocker":
        """Close a blocker that belongs to a RETIRED session, recording a
        machine reason rather than an operator answer.

        Deliberately not `resolve()`: that writes `answer`, which means "the
        operator responded to this question", and using it to clear a dead
        blocker would fabricate exactly the confirmation
        `cli._RESOLUTION_PRECONDITIONS` exists to demand. A blocker whose
        session no longer exists was never answered — it was abandoned, and
        the record should say so. `reason` is required and non-empty so an
        archival can never be a silent delete.
        """
        return self._close_without_answer(blocker_id, reason, "archive_stale")

    def close_recovered(self, blocker_id: str, reason: str) -> "Blocker":
        """Close a blocker whose fault AUTONOMOUS MODE ACTUALLY RECOVERED FROM
        — the loop retried, a step then completed, and the condition is gone.

        A sibling of `archive_stale` rather than a call to it, because the two
        record different facts and an operator reading `archived_reason` has to
        be able to tell them apart: one says the question was abandoned with a
        session, this one says the loop answered it by succeeding. Both refuse
        to write `answer`, for the same reason — that field means an operator
        responded, and forging it would fake exactly the confirmation
        `cli._RESOLUTION_PRECONDITIONS` exists to demand.

        Closing is what makes the retry budget PER EPISODE. The budget is read
        off `Blocker.recurrences` for the open record of this
        (task, code, phase), so a record left open after a recovered fault
        would hand the next, unrelated occurrence a budget that was already
        spent — a login expiry today silently costing next week's its retries.
        A close that never happens (the process dies mid-retry, the store
        cannot be written) leaves the budget spent, which is the safe
        direction: the loop retries less, never more.
        """
        return self._close_without_answer(blocker_id, reason, "close_recovered")

    def _close_without_answer(self, blocker_id: str, reason: str, caller: str) -> "Blocker":
        """Shared body of `archive_stale` / `close_recovered`: resolve a
        blocker with a machine reason and no operator answer. `reason` is
        required and non-empty so neither can ever be a silent delete."""
        if not reason.strip():
            raise StateError(f"{caller} requires a non-empty machine reason")
        blocker = self.load(blocker_id)
        if blocker is None:
            raise StateError(f"no blocker with id '{blocker_id}'")
        if blocker.resolved_at is not None:
            raise StateError(
                f"blocker '{blocker_id}' is already closed (at {blocker.resolved_at})"
            )
        blocker.resolved_at = utcnow_iso()
        blocker.archived_reason = reason
        self.save(blocker)
        return blocker

    def all_blockers(self) -> list[Blocker]:
        """Every blocker on disk, open or resolved, oldest id first per
        task (lexicographic filename order — ids are zero-padded, so this is
        also chronological within a task)."""
        if not self.directory.exists():
            return []
        loaded = [self.load(path.stem) for path in sorted(self.directory.glob("blk-*.json"))]
        return [b for b in loaded if b is not None]

    def open_blockers(self) -> list[Blocker]:
        return [b for b in self.all_blockers() if b.resolved_at is None]

    def open_recurrences(self, task_id: str, code: str) -> int:
        """How many times this (task, code) has been recorded across every OPEN
        record, whatever phase each was raised in. 0 when none is open.

        THE retry budget's meter for `orchestrator._to_needs_user`, and
        deliberately blind to phase where `find_open` is not. `record` keys on
        (task, code, phase) because that is the right identity for an operator
        question — the same fault in `submitting` and in
        `submission_unconfirmed` genuinely reads differently. It is the wrong
        identity for a BUDGET: a fault that migrates one phase along would
        arrive at a fresh record and buy itself a second full allowance, which
        is exactly the unbounded retry the budget exists to prevent. Summing is
        the conservative direction — it can only ever spend the allowance
        faster.

        Reads through `open_blockers`, so a corrupt record RAISES here rather
        than reading as "nothing open", which would be the fail-open answer: it
        would hand the caller a budget of zero-spent and license a retry on
        evidence that could not be read.
        """
        return sum(
            b.recurrences for b in self.open_blockers()
            if (b.task_id, b.code) == (task_id, code)
        )

    def open_blockers_by_severity(self) -> list[Blocker]:
        """Every OPEN blocker, most urgent first (`primary_sort_key`).

        Deliberately a SECOND method rather than a reordering of
        `open_blockers`: callers that want the whole set — `find_open`,
        `open_task_ids`, `cli._reconcile_retired_blockers` — do not care
        about order, and changing the order under them would be churn with
        no reader. Nothing is filtered here; ranking a primary must never
        cost the operator sight of the rest."""
        return by_severity(self.open_blockers())

    def primary_blocker(self) -> Blocker | None:
        """THE open blocker — the most severe, most recent one, or `None`
        when nothing is open.

        The single answer to "which blocker is this loop stuck on?". With
        exactly one open it is that one, which is the overwhelmingly common
        case and identical to reading the set's only element; with several
        it is decided by `primary_sort_key`, never by directory order.
        Callers that name it MUST still say how many others are open —
        selecting a primary is not permission to hide the rest."""
        ordered = self.open_blockers_by_severity()
        return ordered[0] if ordered else None

    def open_task_ids(self) -> set[str]:
        """Every task id named by at least one OPEN blocker.

        The question `cli._reconcile_unblocked_tasks` asks of this store — "is
        there still anything to answer about this task?" — and the reason a
        `blocked` status is allowed to persist. A task that is absent from this
        set has nothing left justifying its quarantine, so leaving it out of
        `next_ready()` is a split brain rather than a decision (blk-01).

        EVERY KIND counts, deliberately unlike `cli._reconcile_retired_
        blockers`' `task_fatal` allowlist. That sweep CLOSES records, so it has
        to prove a record is closeable; this one closes nothing and only ever
        decides whether to keep a task out of the queue, so the conservative
        direction is the opposite: a `loop_fatal` record naming a task is still
        an open question about it, and keeping the task blocked while it stands
        costs a delay, where releasing it early costs a dispatch nobody
        authorised. `NO_TASK` needs no special case — `tasks._ID_RE` forbids
        parentheses, so `"(loop)"` can never be a real task id and can never
        match one.

        Reads through `open_blockers`, so a corrupt record RAISES here too
        rather than reading as "nothing open" — which would be exactly the
        wrong answer for a caller about to decide a quarantine is over.
        """
        return {b.task_id for b in self.open_blockers()}

    def resolve(self, blocker_id: str, answer: str) -> Blocker:
        """Mark `blocker_id` resolved with the operator's `answer`. Refuses
        an unknown id or one that is already resolved — resolution is a
        one-way, one-time action, never silently overwritten by a second
        `answer` call."""
        blocker = self.load(blocker_id)
        if blocker is None:
            raise StateError(f"no blocker with id '{blocker_id}'")
        if blocker.resolved_at is not None:
            raise StateError(
                f"blocker '{blocker_id}' is already resolved "
                f"(at {blocker.resolved_at}, answer: {blocker.answer!r})"
            )
        blocker.resolved_at = utcnow_iso()
        blocker.answer = answer
        self.save(blocker)
        return blocker
