"""Persistent loop state with atomic writes.

One small JSON file holds everything needed to resume after a crash: the
current phase, the in-flight request (with its request-id, used for duplicate
detection in the conversation itself), the last raw response (so parsing and
execution can be redone idempotently), and the loop counters. Full request /
response texts also go to the append-only transcript (`transcript.py`); the
state file is the recovery source of truth, the transcript is the audit log.

Writes are atomic (temp file + os.replace in the same directory) so a crash
mid-save can never leave a half-written state file.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import StateCorruptError, StateError

if TYPE_CHECKING:
    # Type-checking / static-analysis only — see `_load_changeset`'s
    # docstring for why the REAL import stays local to that function rather
    # than living at module level.
    from .changeset_review import ChangesetBinding

# v2 (2026-07-29): review-integrity stamps on PendingRequest/LastResponse,
# last_decision / last_validation for the review context. Breaking on purpose —
# v1 sessions predate contract v2 and must be reset, not migrated mid-flight.
# v3 (2026-07-30): `task_execution` — the serialised `worktask.TaskExecution`
# for whichever task is currently running the produce-then-review commit path
# (worktree path/branch, base/candidate sha, review round). Breaking on
# purpose, same as v1->v2: a v2 session has no worktree/candidate-sha
# provenance to backfill, so it must be reset rather than guessed at.
#
# NOT bumped for the pass-2b additions below (`PendingRequest.prompt_sha256`,
# `PendingRequest.postcommit` / `LastResponse.postcommit`): both are new
# dataclass fields with defaults, so `PendingRequest(**data)` /
# `LastResponse(**data)` tolerate an old on-disk dict that lacks the keys —
# there is nothing to backfill because a session with no in-flight postcommit
# review simply has `postcommit = None`, which is exactly the correct value
# for it.
#
# NOT bumped for the transport-recovery additions either
# (`PendingRequest.conversation_url` / `conversation_epoch` / `resends_used` /
# `last_send_outcome`, `LoopState.conversation_epoch` / `rotations` /
# `last_rotation`). Same reasoning, and the one field that could be
# misinterpreted is handled explicitly rather than by default: an on-disk
# request predating this change carries `conversation_url = ""`, which the
# orchestrator binds to `state.conversation_url` the first time it touches the
# request. That binding is correct because it can only happen while
# `rotations == 0` — before any rotation the global URL *is* every request's
# URL — and after it the request carries its own, so a later rotation cannot
# retroactively re-point an old request at the new chat.
#
# NOT bumped for the provider-failover additions either
# (`PendingRequest.provider`, `LastResponse.provider`,
# `LoopState.active_provider` / `provider_switches` / `last_provider_switch`).
# All defaulted, and the defaults are the truth rather than a guess: a session
# written before this existed ran on whatever `conversation.provider` said and
# had no way to switch, so "" (meaning "ask the config") and 0 describe it
# exactly.
#
# NOT bumped for the "silent conversation" rotation entry condition either
# (`PendingRequest.start_timeouts` / `start_timeout_wait_seconds`). Same
# reasoning again: both are new fields with defaults of 0, and 0 is exactly
# correct for a request loaded from a state file written before this existed
# — it has no recorded response-start timeouts to backfill, and treating it
# as having none is the truth, not a guess.
#
# NOT bumped for the operator-changeset review additions either
# (`PendingRequest.changeset` / `LastResponse.changeset` / `LoopState.
# changeset` — see `changeset_review.ChangesetBinding`). Same reasoning as
# `postcommit` above: all three are new fields with a `None` default, and a
# session written before this existed has no in-flight changeset review to
# backfill — `None` is exactly correct for it.
#
# NOT bumped for the fault-stop classification either (`LoopState.stop_kind` /
# `stop_blocker_id`). Both default to the empty/None value, and — unlike most of
# the additions above — the default here is deliberately NOT one of the two real
# classifications: a session written before this existed ended in `stopped` for
# a reason nobody recorded, and every reader must treat that as "unclassified"
# rather than guessing "contract". `cli._cmd_smoke_browser` gates PASS on the
# POSITIVE value (`stop_kind == "contract"`), so an unclassified stop reads as a
# failure, which is the fail-closed direction.
#
# NOT bumped for the rate-limit wait deadline either
# (`LoopState.rate_limit_retry_not_before`). It defaults to `None`, which is
# exactly right for a state file written before it existed: that process was not
# in the middle of a throttle back-off, so there is no wait to resume.
#
# NOT bumped for chunked packet delivery either (`LoopState.outbox_diff`,
# `PendingRequest.delivery`, and the new `Phase.DELIVERING` member). The two
# fields default to `None`, which is exactly right for a state file written
# before this existed: its payload was sent as one message, so there is no
# delivery to backfill. The phase is additive — an old state file cannot
# contain a value that did not exist when it was written, and `Phase(...)`
# still rejects anything unknown.
#
# NOT bumped for urgent preemption either (`LoopState.preemption`, plus the new
# `"preempted"` value of the existing `stop_kind`). The field defaults to `None`,
# which is exactly right for a state file written before it existed: that
# session was never preempted, so there is nothing to backfill. `stop_kind` gains
# no field at all, and every reader of it already gates on the POSITIVE value it
# wants, so an old `""` keeps reading as unclassified rather than as this.
#
# NOT bumped for repeated-stop detection either (`StopRepetition` /
# `StopRepetitionStore` below). Nothing was added to `LoopState` at all: the
# ledger is its OWN file under `state_dir` (`stop_repetition_file`), because the
# thing it counts is a sequence of SESSIONS and a field here would be discarded
# by the very session replacement it exists to observe. An old state file
# therefore needs no backfill, and a deployment with no ledger file yet simply
# starts counting from its next stop.
#
# NOT bumped for the postcommit binding a corrective re-prompt inherits either
# (`LoopState.carry_postcommit` / `LoopState.sent_postcommits`). Both default to
# the empty value, and the empty value is the truth rather than a guess: a
# session written before this existed had no re-prompt to carry a binding onto
# and had recorded no sent packets, because nothing recorded them. It is also
# the direction that fails CLOSED — an empty ledger resolves no approval and an
# absent carry binds nothing, so an old state file behaves exactly as it did
# before this existed (an unbound `push` is refused) rather than resolving a
# binding out of a record nobody wrote.
#
# NOT bumped for transport-aware fault recovery either
# (`PendingRequest.replays_used`). Same reasoning as `start_timeouts` above: a
# new field defaulting to 0, and 0 is the truth rather than a guess for a
# request loaded from a state file written before it existed — no process had
# ever re-invoked it, because nothing could. The direction of the default is the
# safe one too: 0 means the request still has its whole replay budget, and the
# budget only ever authorizes a re-run on a transport that declares
# `idempotent_submit`, i.e. one whose failed invocation provably appended
# nothing anywhere.
SCHEMA_VERSION = 3


def _fsync_dir(directory: Path) -> None:
    """Make a rename inside `directory` durable. Best-effort by design: not
    every filesystem allows opening a directory for fsync, and a save that
    succeeded is not worth failing over a platform that cannot confirm it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Phase(str, Enum):
    READY = "ready"            # outbox holds the next payload to send
    # The request's payload is too large for one message and is being deposited
    # as numbered parts BEFORE the message that asks for a verdict. Nothing has
    # been asked yet: a request in this phase has sent zero or more parts and no
    # question. All-or-nothing — the verdict message is only reached once every
    # part is confirmed present in persisted history; any part that does not
    # land sends the omission notice instead (see `packet.plan_chunked_delivery`
    # and `Orchestrator._step_delivering`).
    DELIVERING = "delivering"
    SUBMITTING = "submitting"  # pending_request created, may or may not be sent
    # A send was attempted but acceptance is UNKNOWN. Only reconciliation (a
    # controlled reload) may resolve this; it must never auto-resend, because
    # the backend may have accepted a message the browser failed to observe.
    SUBMISSION_UNCONFIRMED = "submission_unconfirmed"
    # A send was attempted and the transport POSITIVELY DISPROVED acceptance
    # (the browser's own send request failed — see browser/observation.py).
    # Distinct from SUBMISSION_UNCONFIRMED because the recovery differs: here
    # reconciliation can confirm absence, and confirmed absence licenses
    # exactly one same-chat resend of the same request id. Unknown acceptance
    # never earns that.
    SUBMISSION_REJECTED = "submission_rejected"
    AWAITING = "awaiting"      # submission confirmed, waiting for the reply
    EXECUTING = "executing"    # raw response captured; parse -> policy -> dispatch
    NEEDS_USER = "needs_user"  # human input required (question / retry)
    # The reviewer decided stop, OR the loop ended itself on a fault it cannot
    # ask anyone about (see `LoopState.stop_kind` — the two are the SAME phase
    # and are told apart only by that field, never by the phase alone).
    STOPPED = "stopped"
    FAILED = "failed"          # failure budget exhausted


TERMINAL_PHASES = frozenset({Phase.NEEDS_USER, Phase.STOPPED, Phase.FAILED})


@dataclass
class PostcommitBinding:
    """Which produce-then-review candidate a single request/response pair
    concerns — captured ONCE, when the review packet is sent, and never
    recomputed from a later "latest state" lookup.

    `packet_sha256` duplicates `PendingRequest.report_sha256` /
    `LastResponse.report_sha256` on purpose: those fields are generic (every
    request carries one, postcommit or not), so a reader who only has a
    `PostcommitBinding` in hand (e.g. `_dispatch_task_push`) does not have to
    reach into the surrounding request to know what report this binding
    belongs to.

    `candidate_tree_sha` is the candidate commit's tree object id, captured
    at the SAME moment as `candidate_sha`. Because git objects are content
    addressed, re-deriving `tree_of(candidate_sha)` at push time and
    comparing it to this value only fails if something replaced the object
    (`git replace`) or the object database itself is inconsistent — the kind
    of tamper `report_sha256` alone cannot see, since that hash covers the
    rendered packet TEXT, not a fresh read of the object right before
    publishing it.
    """

    task_id: str
    task_branch: str
    base_sha: str
    candidate_sha: str
    candidate_tree_sha: str
    packet_sha256: str


@dataclass
class ChunkedDelivery:
    """Progress through a chunked packet delivery, for ONE pending request.

    Distinct from `packet.DeliveryPlan`, which is the pure, freshly computed
    rendering: this is the durable half — what has actually been confirmed
    present in the conversation, plus the payload to fall back to if the rest
    does not make it.

    `delivered` is a cursor into `parts`, saved after each part is confirmed by
    READBACK from persisted history (never on the send's own say-so). A crash
    mid-delivery therefore resumes at the first unconfirmed part instead of
    re-posting the ones already there.

    `fallback_payload` is the SAME packet with its diff replaced by the
    omission notice. It is captured up front rather than re-derived at failure
    time so the fallback needs nothing from git, the worktree, or a diff that
    may no longer render identically — a part failing is the worst moment to
    depend on any of that.
    """

    #: `[{"part_id": str, "index": int, "total": int, "text": str}, ...]` in
    #: send order. Plain dicts, like `LoopState.task_execution`, because they
    #: round-trip through the state file's JSON with no custom decoding.
    parts: list[dict] = field(default_factory=list)
    delivered: int = 0
    fallback_payload: str = ""

    @property
    def complete(self) -> bool:
        """Every part confirmed. The ONLY condition under which a verdict may
        be requested — see `Orchestrator._step_submitting`'s guard, which
        refuses rather than asks when this is False."""
        return bool(self.parts) and self.delivered >= len(self.parts)

    def part_ids(self) -> list[str]:
        return [str(part.get("part_id", "")) for part in self.parts]


@dataclass
class PendingRequest:
    request_id: str
    payload: str
    submitted: bool = False
    #: True once a send was clicked, whether or not it was confirmed. Gates
    #: automatic resubmission: an attempted-but-unconfirmed send may only be
    #: resolved by reconciliation or an explicit operator `--resubmit`.
    send_attempted: bool = False
    reconcile_attempts: int = 0
    # The fully rendered prompt is stored so a crash-retry resubmits the exact
    # bytes that were stamped, and the stamps below stay truthful.
    prompt: str = ""
    #: sha256 of `prompt`, stamped when `prompt` is built and re-checked
    #: immediately before `client.submit` is called. `prompt` is never
    #: recomputed between those two points in normal operation, so this
    #: should never fail — it exists to catch on-disk corruption or manual
    #: state-file tampering between a crash and a `--retry` sending a prompt
    #: nobody actually reviewed the stamps for.
    prompt_sha256: str = ""
    #: Absolute path to a file uploaded WITH this request — the review diff,
    #: when it is delivered as an attachment rather than as message text.
    #: Belongs to the request, not the loop: a path left on shared state would
    #: outlive its packet and attach one change's diff to another's review.
    attachment: str = ""
    template: str = ""
    head_sha: str = ""
    base_sha: str = ""
    report_sha256: str = ""
    timestamp: str = ""
    #: Set only when this request's payload is a produce-then-review packet
    #: (`packet.build_review_packet`, wrapped by the `postcommit_review`
    #: template). `None` for every other kind of request — audit reports,
    #: corrective re-prompts, ordinary commit approvals, and so on.
    postcommit: PostcommitBinding | None = None
    #: Set only when this request's payload is an operator-changeset review
    #: packet (`changeset_review.build_changeset_packet`, wrapped by the
    #: `changeset_review` template). Mutually exclusive with `postcommit` in
    #: practice (each is bound from a distinct `state` field —
    #: `task_execution` vs `changeset` — and only one is normally set at a
    #: time), but nothing enforces that structurally; both are simply `None`
    #: for every other kind of request.
    changeset: ChangesetBinding | None = None
    #: THE authoritative conversation for this request. Every submit, await and
    #: reconcile for it targets this URL — never `LoopState.conversation_url`,
    #: which moves when a rotation happens. That is what makes a late reply in
    #: an abandoned chat structurally unable to authorize anything: it is not
    #: in the conversation this request is bound to, so it is never read.
    #: Empty only on a request written before this field existed; see the
    #: SCHEMA_VERSION note above for why binding it lazily is sound.
    conversation_url: str = ""
    #: Which conversation generation the binding above belongs to. Incremented
    #: by every rotation. Carried alongside the URL rather than derived from it
    #: so two chats that somehow share a URL still cannot be confused, and so
    #: the transcript can say plainly which generation a turn happened in.
    conversation_epoch: int = 0
    #: Same-chat resends performed for this request id. Capped at one, and only
    #: ever spent after reconciliation has CONFIRMED the request is absent.
    resends_used: int = 0
    #: The transport's last verdict for this request ("accepted" / "rejected" /
    #: "unknown" — `browser.observation.SendOutcome`). Persisted so recovery
    #: after a crash resumes from the same evidence the live run had, rather
    #: than downgrading to "unknown" and parking a human unnecessarily.
    last_send_outcome: str = ""
    #: Which conversation provider this request was last SENT through. Recorded
    #: because the reviewer grants authority: an approval carrying a `reviewed`
    #: stamp must be attributable to the transport (and therefore the model)
    #: that produced it. Empty on a request written before this field existed,
    #: which is honest — those predate any possibility of a switch.
    provider: str = ""
    #: Consecutive response-START timeouts (`ResponseTimeoutError` with
    #: `stage="start"`) observed for this request while `awaiting`, in the
    #: SAME conversation. Only ever incremented by
    #: `orchestrator._handle_response_start_timeout`, and only for that
    #: stage — a response that already started and merely took too long
    #: never touches this. Reset to 0 by a completed rotation (a fresh
    #: conversation gets a fresh silence clock) and by a reconciliation that
    #: finds the conversation is no longer silent. At 3, with the ordinary
    #: failure budget still allowing a retry, the loop performs ONE final
    #: reconciliation of the current conversation; confirmed continued
    #: silence there is what may authorize a rotation (see
    #: `docs/AUTOLOOP.md` §5c).
    start_timeouts: int = 0
    #: Set only when this request's payload is too large for one message and
    #: is being delivered as numbered parts first (`packet.plan_chunked_delivery`).
    #: `None` — every ordinary request — means the payload is sent as a single
    #: message exactly as it always was. Cleared to `None` by a fallback to the
    #: omission notice, which is what makes "a request with a delivery still
    #: has parts outstanding" a real, checkable condition rather than a
    #: leftover.
    delivery: ChunkedDelivery | None = None
    #: Accumulated ACTUAL wait (`ResponseTimeoutError.elapsed`, monotonic
    #: seconds — not the configured timeout value) behind `start_timeouts`.
    #: Checked against a floor computed from
    #: `config.browser.response_start_timeout_seconds` (3x it) before a
    #: rotation may fire, so the loop is acting on a total wait it actually
    #: measured, not merely assumed from configuration. Reset alongside
    #: `start_timeouts`.
    start_timeout_wait_seconds: float = 0.0
    #: How many times this request has been RE-INVOKED because its transport
    #: could no longer produce the reply it was waiting for
    #: (`orchestrator._replay_unrecoverable_await`). Only ever spent by a
    #: transport that declares `idempotent_submit`, and only after that
    #: transport's own `reconcile` has confirmed the reply is absent — so each
    #: one is a re-run that provably cannot double-post. Bounded by
    #: `orchestrator.MAX_AWAIT_REPLAYS`; past it the fault is charged to the
    #: ordinary failure budget and ends in a park, rather than re-invoking a
    #: reviewer forever. Never reset by a replay (that is the whole point of
    #: counting them); cleared with the other per-transport marks when the
    #: reviewer role moves to another provider.
    replays_used: int = 0


@dataclass
class LastResponse:
    request_id: str
    raw: str
    received_at: str
    # Review-integrity stamp copied from the pending request, so `executing`
    # can verify a git approval idempotently after a crash.
    head_sha: str = ""
    base_sha: str = ""
    report_sha256: str = ""
    #: Carried over from the `PendingRequest` this response answers. The
    #: orchestrator's push dispatch reads the candidate sha ONLY from here —
    #: never from a fresh `TaskExecutionStore` lookup, and never from
    #: anything in the directive itself (a `push` directive cannot even
    #: carry a task_id — see `contract._forbid`). That is what makes an
    #: approval of candidate A structurally unable to publish a swapped-in
    #: candidate B.
    postcommit: PostcommitBinding | None = None
    #: Carried over from the `PendingRequest` this response answers, exactly
    #: like `postcommit` above but for an operator-changeset review — see
    #: `changeset_review.ChangesetBinding` and
    #: `Orchestrator._dispatch_changeset_push`.
    changeset: ChangesetBinding | None = None
    #: Which conversation this reply was actually read from, copied from the
    #: request it answers. Recorded so "only a response captured from the
    #: request's bound conversation may authorize action" is auditable after
    #: the fact, not merely enforced in the moment by whichever client the
    #: awaiting phase happened to hold.
    conversation_url: str = ""
    conversation_epoch: int = 0
    #: Which provider produced this reply, copied from the request it answers.
    #: This is the field that makes "who authorized this commit" answerable
    #: after a failover — without it the transcript says a directive was
    #: reviewed but not by which reviewer.
    provider: str = ""


def _load_postcommit(raw: dict | None) -> PostcommitBinding | None:
    return PostcommitBinding(**raw) if raw else None


#: Every field a `PostcommitBinding` record must carry to be usable.
_POSTCOMMIT_FIELDS = (
    "task_id",
    "task_branch",
    "base_sha",
    "candidate_sha",
    "candidate_tree_sha",
    "packet_sha256",
)


def postcommit_binding_from_record(raw: object) -> PostcommitBinding | None:
    """A `PostcommitBinding` from a plain dict, or `None` when the dict is not
    one — the TOLERANT reader, for the two places a binding is stored as loose
    JSON rather than as a typed field (`LoopState.carry_postcommit`,
    `LoopState.sent_postcommits`).

    Deliberately different from `_load_postcommit` above, which is the strict
    reader for `PendingRequest.postcommit` / `LastResponse.postcommit` and
    raises through `LoopState.from_dict`'s `StateCorruptError` clause. There is
    nothing to catch a raise here: both callers run deep inside
    `Orchestrator._step_ready` / `_dispatch`, whose exception handling is by
    type and has no clause for a `TypeError` from `PostcommitBinding(**data)`,
    so a hand-edited or half-written record would end the process with a
    traceback, no park and no blocker.

    `None` is the fail-CLOSED answer, which is why returning it rather than
    raising is safe: an unreadable carry binds nothing and an unreadable ledger
    entry resolves nothing, so the approval that depended on it is REFUSED —
    the same outcome as before either mechanism existed. Every field must be a
    non-empty string; no writer here produces anything else, and honouring a
    partially-filled record would publish against identifiers nobody stamped.
    """
    if not isinstance(raw, dict):
        return None
    values: dict[str, str] = {}
    for name in _POSTCOMMIT_FIELDS:
        value = raw.get(name)
        if not isinstance(value, str) or not value:
            return None
        values[name] = value
    return PostcommitBinding(**values)


def _load_changeset(raw: dict | None) -> ChangesetBinding | None:
    if not raw:
        return None
    # Local (call-time) import, deliberately not hoisted to module level:
    # `PostcommitBinding` avoids this entirely by living IN this module, but
    # `ChangesetBinding` lives in `changeset_review.py` per the brief for
    # this feature. `changeset_review.py` has legitimate reasons to import
    # FROM `state.py` in the future (session/`PendingRequest` construction
    # for the CLI's `review-changeset` command lives right next to it) — a
    # module-level `from .changeset_review import ChangesetBinding` here
    # would make that a real import cycle the moment it happened. This
    # function is the only place the real class is ever needed at runtime,
    # so it is the only place that imports it.
    from .changeset_review import ChangesetBinding

    return ChangesetBinding(**raw)


def _load_delivery(raw: dict | None) -> ChunkedDelivery | None:
    return ChunkedDelivery(**raw) if raw else None


def _load_pending_request(raw: dict | None) -> PendingRequest | None:
    if not raw:
        return None
    data = dict(raw)
    data["postcommit"] = _load_postcommit(data.get("postcommit"))
    data["changeset"] = _load_changeset(data.get("changeset"))
    data["delivery"] = _load_delivery(data.get("delivery"))
    return PendingRequest(**data)


def _load_last_response(raw: dict | None) -> LastResponse | None:
    if not raw:
        return None
    data = dict(raw)
    data["postcommit"] = _load_postcommit(data.get("postcommit"))
    data["changeset"] = _load_changeset(data.get("changeset"))
    return LastResponse(**data)


@dataclass
class ProviderSwitch:
    """One completed handover of the reviewer role to the fallback provider.

    Written only after the switch is committed, so its presence means the
    handover happened. Carries no credentials and no message text; `reason` is
    a stable code (`quota_exhausted`), not free prose.
    """

    from_provider: str
    to_provider: str
    request_id: str
    reason: str
    at: str = field(default_factory=lambda: utcnow_iso())


@dataclass
class RotationRecord:
    """One completed conversation rotation. Written only after the new chat has
    been proven usable AND the request reconciled against it, so its presence
    means the move finished — not that it was attempted.

    Carries no credentials and no message text; `reason` is a stable code
    (`conversation_unusable`, `send_rejected_twice`), not free prose.
    """

    old_url: str
    new_url: str
    request_id: str
    reason: str
    epoch: int
    at: str = field(default_factory=lambda: utcnow_iso())


@dataclass
class LoopState:
    session_id: str
    conversation_url: str
    phase: str = Phase.READY.value
    iteration: int = 0
    consecutive_failures: int = 0
    #: Consecutive browser failures whose restart was SKIPPED because
    #: `browser.restart_cooldown_seconds` had not elapsed since the last one.
    #: Deliberately its own counter rather than part of `consecutive_failures`
    #: above: a restart that never ran says nothing about whether restarting
    #: works, and charging those failures to the failure budget killed a
    #: session on 2026-08-04 in which Chrome was never once restarted. Reset
    #: the moment a restart command actually runs (success or failure), and
    #: bounded by `policy.max_browser_restart_skips` so the exemption ends in
    #: a park naming the cooldown rather than an unbounded retry — see
    #: `orchestrator._handle_browser_failure`.
    browser_restart_skips: int = 0
    #: Consecutive back-offs taken because ChatGPT is throttling the ACCOUNT
    #: (`errors.RateLimitedError` — its "Too many requests" overlay). Its own
    #: counter for the same reason `browser_restart_skips` is: the loop cannot
    #: recover from a server-side limit, only outlast it, so these are not
    #: evidence that the transport is hopeless and must never spend
    #: `consecutive_failures`. Charging them there is what turned one overnight
    #: throttle into a restart-and-retry storm that deepened the limit it was
    #: failing on (2026-08-14/15). Reset only when a STEP COMPLETES — never on
    #: a clear overlay probe, which says only that the loop closed the modal
    #: (see `orchestrator.run`'s `else` branch and `_dismiss_rate_limit_modal`)
    #: — and bounded by `policy.max_rate_limit_backoffs` so the exemption ends
    #: in a park NAMING the throttle rather than an unbounded wait — see
    #: `orchestrator._handle_rate_limited`.
    rate_limit_backoffs: int = 0
    #: Seconds of back-off this throttle episode has actually COMPLETED,
    #: credited when each wait finishes rather than when it starts — so the park
    #: message states a wait that really elapsed. Same distinction as
    #: `PendingRequest.start_timeout_wait_seconds`. Reset with the counter.
    #:
    #: A completed wait is credited its full scheduled length even when part of
    #: it was spent with the process dead (crash mid-wait, resumed against
    #: `rate_limit_retry_not_before` below). That is not pre-crediting: the
    #: remedy for a server-side limit is calendar time in which the account
    #: makes no requests, and a process that is not running makes none.
    rate_limit_wait_seconds: float = 0.0
    #: ISO-8601 UTC instant before which nothing may touch ChatGPT, set when a
    #: back-off STARTS and cleared when it finishes. `None` outside a wait.
    #:
    #: The counter above alone is not enough to make the back-off durable: it
    #: records that a wait was entered, not that it was served. A process killed
    #: just after saving it would resume with the whole delay treated as already
    #: waited and walk straight back into the browser step, so repeated restarts
    #: could skip the very back-off this exists to enforce — the restart-storm
    #: shape again, one level up. `orchestrator.run` honours whatever remains of
    #: this deadline before EVERY step, so the wait survives the process that
    #: began it.
    rate_limit_retry_not_before: str | None = None
    parse_retries: int = 0
    policy_denials: int = 0
    outbox: str | None = None
    #: The raw patch text embedded in `outbox`, when that payload is a review
    #: packet whose diff is too large for one chat message. `None` for every
    #: other payload, and for a packet that fits — which is the common case and
    #: behaves exactly as it always has.
    #:
    #: Carried rather than recovered later by slicing `outbox`, because
    #: `_step_ready` needs the exact bytes to build the parts and cannot ask
    #: git for them (the diff lives in the task's own worktree, and the payload
    #: may have been queued by an earlier process). It is a duplicate of text
    #: already inside `outbox`, so it is checked against it before use: the
    #: inline section must appear there exactly once or the loop falls back to
    #: the omission notice. That check is what stops a hand-edited or corrupted
    #: state file from delivering parts that differ from the hashed packet.
    outbox_diff: str | None = None
    #: Absolute path to the file holding `outbox_diff`, when the patch is
    #: being delivered as an UPLOAD rather than as message text.
    #:
    #: Written outside the checkout on purpose: anything created under the
    #: repository mid-run is what `escape_detector` reports, and it would park
    #: the loop loop-fatal. Set by `_plan_delivery` and consumed by
    #: `_step_submitting`; cleared with the rest of the outbox once the request
    #: is answered, so a stale path can never be attached to a later packet.
    outbox_attachment: str | None = None
    pending_request: PendingRequest | None = None
    last_response: LastResponse | None = None
    current_task: dict | None = None
    reviewed_commit: str | None = None
    last_decision: str | None = None
    last_validation: str | None = None
    last_manifest_id: str | None = None
    #: Serialised `worktask.TaskExecution` (a plain dict — `dataclasses.
    #: asdict(execution)`, never a reconstructed dataclass instance here) for
    #: the task currently running the produce-then-review commit path.
    #: Deliberately separate from `last_manifest_id`, which belongs to the
    #: OLD authorize-then-produce/manifest path and means something different
    #: (a `ChangeManifest` id against the main checkout, not a worktree).
    task_execution: dict | None = None
    #: Serialised `changeset_review.ChangesetBinding` (a plain dict —
    #: `dataclasses.asdict(binding)`, never a reconstructed dataclass
    #: instance here — same convention as `task_execution` above) for an
    #: operator changeset queued by `python -m autoloop review-changeset`
    #: and not yet published. Deliberately separate from `task_execution`:
    #: there is no task, no worktree, and no `TaskExecutionStore` record
    #: behind it — the candidate lives directly in THIS checkout. Cleared
    #: (`None`) once `Orchestrator._dispatch_changeset_push` actually
    #: publishes it.
    changeset: dict | None = None
    #: Every produce-then-review packet this session has BOUND A REQUEST TO,
    #: oldest first, as plain dicts (`{"request_id", "head_sha", "report_sha256",
    #: "postcommit": asdict(binding)}` — same convention as `task_execution` /
    #: `last_rotation` beside it). Bounded by
    #: `orchestrator.MAX_SENT_POSTCOMMIT_RECORDS`.
    #:
    #: This is the loop's own record of WHAT IT PRESENTED, and it exists
    #: because `last_response` is not that record: it is merely the most recent
    #: thing the loop sent, which a corrective re-prompt, a git report or any
    #: other round replaces. A stamped approval names the request it reviewed
    #: (`Directive.reviewed.request_id`); this is what lets that name be
    #: checked against a packet the loop can prove it sent, instead of being
    #: refused because the conversation moved on.
    #:
    #: Written only from `_step_ready` / `_fall_back_to_omission`, the only two
    #: places a request's `report_sha256` is ever stamped — so an entry cannot
    #: describe a request whose digest has since been rewritten underneath it.
    #: An entry is therefore written when the request is BUILT, one step before
    #: it goes out; a request that never reaches the reviewer leaves an entry no
    #: approval can ever name, because no approval saw the id.
    #: NEVER an authorization on its own: it says which candidate a request
    #: presented, and every push-time check in `_dispatch_task_push` still runs
    #: against it unchanged.
    sent_postcommits: list[dict] = field(default_factory=list)
    #: The `PostcommitBinding` (as a plain dict) that the NEXT corrective
    #: re-prompt must inherit, or `None` when there is none.
    #:
    #: A corrective re-prompt — parse error, policy denial, review mismatch,
    #: plan rejection — is a formatting/decision correction about a packet that
    #: has NOT changed: the candidate has not moved and nothing new has been
    #: reviewed. Its payload carries none of the candidate's identifiers, so
    #: `_current_pending_postcommit` binds it to nothing, and before this field
    #: existed an approval answering the correction was structurally
    #: unpublishable (2026-08-20, prof-01). Set by `_carry_postcommit_forward`
    #: from the response being corrected, consumed and cleared by the very next
    #: `_step_ready`, so it can never attach to a request that is not the
    #: correction it was recorded for.
    #:
    #: "The response being corrected" is `LastResponse.postcommit` for an
    #: ordinary round and the binding the caller already RESOLVED for that
    #: response otherwise — an approval naming an earlier packet
    #: (`_approval_packet`) has an authoritative binding while
    #: `LastResponse.postcommit` is None, and a correction built from one of
    #: those went out unbound until 2026-08-24. Either way this holds one
    #: binding, for one candidate, for one request.
    carry_postcommit: dict | None = None
    #: Current conversation generation. Requests are stamped with it, so a
    #: response captured under an older epoch can be recognised and ignored.
    conversation_epoch: int = 0
    #: Completed rotations THIS RUN. Checked against
    #: `PolicyConfig.max_conversation_rotations` BEFORE each rotation, and
    #: zeroed once per process by `cli._reset_run_scoped_budgets`. It lives
    #: in the state file only so a crash mid-rotation cannot refund the
    #: budget it already spent — not to accumulate across runs. Before that
    #: reset existed the field was really per-SESSION, so a single dropped
    #: network spent it permanently and no `run --retry` could recover.
    rotations: int = 0
    #: The most recent completed rotation, as a plain dict (`asdict` of a
    #: `RotationRecord`). Read by the CLI's config-drift guard: after a
    #: rotation the state legitimately points somewhere the config does not
    #: yet, and this record is what distinguishes that from an operator
    #: editing the config out from under a live session.
    last_rotation: dict | None = None
    #: The conversation provider currently holding the reviewer role. Empty
    #: means "whatever `conversation.provider` says" — the state only starts
    #: carrying a value once something has reason to disagree with the config,
    #: which is exactly a failover. Kept in state rather than read from config
    #: on every step so a handover survives a restart: a run that switched
    #: because Codex was spent must not quietly switch back on resume and
    #: exhaust the same allowance again.
    active_provider: str = ""
    #: Completed provider handovers this run, checked against
    #: `PolicyConfig.max_provider_switches` BEFORE each one.
    provider_switches: int = 0
    #: The most recent completed handover (`asdict` of a `ProviderSwitch`).
    last_provider_switch: dict | None = None
    question: str | None = None
    resume_phase: str | None = None
    stop_reason: str | None = None
    #: WHY the loop is in `stopped` — the field that keeps one phase from
    #: meaning two opposite things:
    #:
    #: * `"contract"` — the reviewer answered `stop`. The run finished the way
    #:   it is supposed to; `stop_reason` is the reviewer's own words.
    #: * `"fault"` — the LOOP ended itself because it hit a wall no further
    #:   message could get past (today: the policy-denial budget, see
    #:   `orchestrator._to_fault_stop`). `stop_reason` describes the wall, a
    #:   `blockers.Blocker` records it for `python -m autoloop blockers`, and
    #:   `stop_blocker_id` below names that record.
    #: * `"preempted"` — an operator's urgent request took the loop
    #:   (`orchestrator._preempt_for_urgent`). Like `"contract"` and unlike
    #:   `"fault"` this is a CLEAN boundary: the round was ended deliberately at
    #:   a safe phase, the displaced task is back in the queue, and the very
    #:   next selection is meant to start a new session — so continuous mode
    #:   carries on rather than stopping. `stop_reason` names the urgent target
    #:   and `LoopState.preemption` records what was displaced.
    #: * `""` — unclassified: a state file written before this field existed,
    #:   or any phase other than `stopped`.
    #:
    #: Every reader must gate on the POSITIVE value it wants rather than
    #: treating "not fault" as clean — `cli._cmd_smoke_browser` reports PASS
    #: only for `"contract"`, so an unclassified or newly added fault stop
    #: fails closed instead of being announced as a healthy round-trip.
    stop_kind: str = ""
    #: The `blockers.Blocker.id` a `"fault"` stop was recorded under, if a
    #: `BlockerStore` was configured. The counterpart to `park_blocker_id`
    #: below, deliberately a separate field: a fault stop is not a park, and
    #: nothing that reads a park (`cli._handle_parked_task`, `--answer`'s
    #: `needs_user` gate) should be able to pick this up by accident.
    stop_blocker_id: str | None = None
    #: Classification of the CURRENT `needs_user` park (see
    #: `orchestrator._to_needs_user`'s `kind` parameter and
    #: `docs/AUTOLOOP.md`'s blockers section): `"task_fatal"` (one task was
    #: set aside; `park_task_id` names it) or `"loop_fatal"` (the whole loop
    #: stops). `None` outside `needs_user`, or for a state file written
    #: before this existed — `cli.py`'s continuous-mode handling treats a
    #: missing/unrecognised value as `loop_fatal` (fail-closed), never as
    #: task_fatal. New fields with defaults — nothing to backfill on an old
    #: state file, same reasoning as the pass-2b additions above.
    park_kind: str | None = None
    park_task_id: str | None = None
    #: The `blockers.Blocker.id` this park was recorded under, if a
    #: `BlockerStore` was configured (always true in production; `None` in
    #: tests/tools that construct a minimal `Orchestrator`).
    park_blocker_id: str | None = None
    #: What this session gave up when an operator's urgent request preempted
    #: it, or `None` for every session that was never preempted.
    #:
    #: Written by `orchestrator._preempt_for_urgent` in the same save that ends
    #: the round, and it is the reason a preemption is not a silent discard:
    #: it names the DISPLACED task, the phase the request was first observed at,
    #: the phase it was acted on at, the urgent target that took the loop, and
    #: the quarantine label plus the paths the displaced round's worker repo and
    #: execution record were moved to. `stop_kind == "preempted"` is what says a
    #: `stopped` session ended this way; this is what says what it cost.
    #:
    #: TWO SEPARATE TRUTHS about the release, never collapsed into one:
    #: `displaced_returned_to_pending` is the STATUS half and
    #: `displaced_artifacts_retired` is the ARTEFACT half, because the status is
    #: made durable first and a retirement can fail after it. When the second is
    #: false, `stale_worker_path` and `stale_execution_record` name what
    #: survived — deliberately left PAIRED, since that is the shape a killed
    #: round leaves and the next dispatch resumes it rather than refusing.
    #:
    #: A plain dict, like `current_task` / `task_execution` / `last_rotation`
    #: beside it: it is a record to display and log, nothing branches on its
    #: fields, and a dataclass would need its own loader in `from_dict`.
    #:
    #: DURABLE ONLY AS LONG AS THE SESSION. The next selection replaces the
    #: whole `LoopState`, so the transcript's `task_preempted` entry — written
    #: from this same dict — is the permanent record; this field is what
    #: `status` and the operator's terminal can still show in between.
    preemption: dict | None = None
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def new(cls, conversation_url: str) -> "LoopState":
        return cls(session_id=uuid.uuid4().hex, conversation_url=conversation_url)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LoopState":
        try:
            pending = data.get("pending_request")
            last = data.get("last_response")
            kwargs = dict(data)
            kwargs["pending_request"] = _load_pending_request(pending)
            kwargs["last_response"] = _load_last_response(last)
            return cls(**kwargs)
        except (KeyError, TypeError) as exc:
            raise StateCorruptError(f"state file has an unexpected shape: {exc}") from exc


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> LoopState | None:
        if not self.path.exists():
            return None
        text = self.path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StateCorruptError(
                f"cannot decode {self.path}: {exc}. The file was left untouched; "
                "inspect it or run `python -m autoloop reset --yes`."
            ) from exc
        if not isinstance(data, dict):
            raise StateCorruptError(f"{self.path} does not contain a JSON object")
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise StateError(
                f"state schema version {version!r} != supported {SCHEMA_VERSION}; "
                "reset the state or migrate it by hand"
            )
        return LoopState.from_dict(data)

    def save(self, state: LoopState) -> None:
        state.updated_at = utcnow_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        # Temp-file + rename alone is atomic against a killed PROCESS but not
        # against a killed MACHINE: without these fsyncs the rename can reach
        # the disk while the data blocks it points at have not, leaving a
        # state file that is empty or truncated after a power cut. Both are
        # needed — the first makes the contents durable, the second makes the
        # rename durable. A filesystem that refuses to fsync a directory
        # (some network mounts) is not a reason to fail the save.
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        _fsync_dir(self.path.parent)

    def archive(self) -> Path | None:
        """Move the current state file aside (used by `reset`). Returns the backup path."""
        if not self.path.exists():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        os.replace(self.path, backup)
        return backup


#: Filename of the repeated-stop ledger under `AutoloopConfig.state_dir`.
#: Derived rather than added to `AutoloopConfig` so nothing has to be wired:
#: `Orchestrator.__init__` builds the store from `config.state_dir` itself, and
#: a guard that a construction site can forget to pass is a guard that switches
#: itself off the first time somebody adds one.
STOP_REPETITION_FILENAME = "stop_repetition.json"


def stop_repetition_file(state_dir: Path) -> Path:
    """Where `StopRepetitionStore` keeps its one record."""
    return Path(state_dir) / STOP_REPETITION_FILENAME


@dataclass
class StopRepetition:
    """How many times IN A ROW the reviewer has answered `stop` about ONE
    unresolved situation, and what it last said about it.

    Deliberately NOT a `LoopState` field. A reviewer's `stop` ENDS the session
    (`Phase.STOPPED`), and `cli._select_and_kickoff` then replaces the whole
    `LoopState` with a fresh one — so a counter living there would be reset by
    the very transition it exists to count, and the livelock this record
    detects (2026-08-20: three kickoff→stop rounds in fifteen minutes) would be
    invisible in exactly the state it happens in.

    `fingerprint` is `Orchestrator._stop_situation_fingerprint`'s digest of the
    situation the stop was issued about; `count` is how many consecutive stops
    have now carried that same digest. A stop with a DIFFERENT fingerprint
    replaces the record and restarts the count at 1 — that is the whole of
    "reset on progress", and it is why nothing here has to enumerate the ways
    progress can happen.

    `last_reason` is the reviewer's own words from the most recent stop, kept
    verbatim so the park it eventually produces can quote them. In the incident
    the reviewer's text WAS the diagnosis (it named the controller's fault
    precisely), and an operator reading only "stopped repeatedly" would have
    had to rediscover it.
    """

    fingerprint: str
    count: int
    last_reason: str
    first_seen_at: str
    last_seen_at: str
    last_session_id: str = ""


class StopRepetitionStore:
    """The one `StopRepetition` record, as a small JSON file.

    Same crash-safety rule as `blockers.BlockerStore` and
    `worktask.TaskExecutionStore`: a corrupt record RAISES
    (`StateCorruptError`) rather than reading as absent. Reading an unreadable
    ledger as "no stops recorded" would restart the count on every stop, so the
    park would never fire and nothing would say why — the guard would have
    switched itself off silently, which is the exact failure shape this whole
    mechanism exists to end. `Orchestrator._observe_contract_stop` turns the
    raise into a loop_fatal park naming this file.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> StopRepetition | None:
        """The stored record, `None` when there is none, `StateCorruptError`
        when there is one this cannot be trusted to read.

        TYPES ARE CHECKED, not merely unpacked, and that is the whole reason
        this is not a two-line `StopRepetition(**data)`. `count` is compared
        against `MAX_REPEATED_STOPS` and incremented; a hand-edited `"3"` would
        raise `TypeError` from the comparison, deep inside `_dispatch`, where
        NOTHING catches it — `Orchestrator.run`'s handler chain is by exception
        type and has no clause for it, so the process would end with a
        traceback, no park and no blocker, which is the one failure shape this
        package went out of its way to eliminate (see `run`'s `StateError`
        clause). Raising here instead routes it to `_observe_contract_stop`,
        which parks with a record naming this file.

        A count below 1 is refused for the same reason from the other side: it
        is not a shape the writer produces, and honouring a hand-edited `-1000`
        would silently postpone the park by a thousand stops — the guard
        switched off by a plausible-looking number rather than by an error.
        """
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError(f"expected a JSON object, got {type(data).__name__}")
            record = StopRepetition(**data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StateCorruptError(
                f"stop-repetition ledger {self.path} is unreadable: {exc}"
            ) from exc
        # `bool` is an `int` in Python, and `True >= 3` is a legal comparison
        # that answers False — exactly the quiet wrong answer this rejects.
        if isinstance(record.count, bool) or not isinstance(record.count, int):
            raise StateCorruptError(
                f"stop-repetition ledger {self.path} has a non-integer count "
                f"{record.count!r}"
            )
        if record.count < 1:
            raise StateCorruptError(
                f"stop-repetition ledger {self.path} has count {record.count}, "
                "which no writer produces"
            )
        for field_name in ("fingerprint", "last_reason", "first_seen_at",
                           "last_seen_at", "last_session_id"):
            if not isinstance(getattr(record, field_name), str):
                raise StateCorruptError(
                    f"stop-repetition ledger {self.path} has a non-string "
                    f"{field_name}"
                )
        return record

    def save(self, record: StopRepetition) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    def observe(
        self, *, fingerprint: str, reason: str, session_id: str, now: str
    ) -> StopRepetition:
        """Record one reviewer `stop` about `fingerprint` and return the
        updated record — `count == 1` for a situation not seen last time,
        `previous + 1` for the same one again.

        The comparison is the WHOLE definition of "the same unresolved
        situation": equality of the caller's digest, never of `reason`. The
        three refusals in the 2026-08-20 incident were worded differently while
        describing one situation, so matching on text would have missed it;
        conversely two stops about genuinely different things carry different
        digests and each start at 1.
        """
        previous = self.load()
        if previous is not None and previous.fingerprint == fingerprint:
            record = StopRepetition(
                fingerprint=fingerprint,
                count=previous.count + 1,
                last_reason=reason,
                first_seen_at=previous.first_seen_at,
                last_seen_at=now,
                last_session_id=session_id,
            )
        else:
            record = StopRepetition(
                fingerprint=fingerprint,
                count=1,
                last_reason=reason,
                first_seen_at=now,
                last_seen_at=now,
                last_session_id=session_id,
            )
        self.save(record)
        return record
