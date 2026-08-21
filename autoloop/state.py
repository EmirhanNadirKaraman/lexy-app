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
# NOT bumped for split acceptance either (`LoopState.split_requested_for` /
# `split_intent`, and the `SplitIntent` record they carry). Both default to the
# empty/`None` value and the default is the TRUTH about an older state file
# rather than a guess: a process written before this existed had asked for no
# split and had none half-applied, so "no ask outstanding" and "no intent to
# reconcile" describe it exactly. The fail-closed direction matters here more
# than usual — a wrongly-present intent would drive a retirement nobody asked
# for — and absence can only ever mean "nothing to do".
#
# NOT bumped for chunked packet delivery either (`LoopState.outbox_diff`,
# `PendingRequest.delivery`, and the new `Phase.DELIVERING` member). The two
# fields default to `None`, which is exactly right for a state file written
# before this existed: its payload was sent as one message, so there is no
# delivery to backfill. The phase is additive — an old state file cannot
# contain a value that did not exist when it was written, and `Phase(...)`
# still rejects anything unknown.
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


#: The task-definition fields a `SplitIntent` carries for each successor —
#: THE list, imported by `tasks.TaskRegistry.apply_split` / `split_applied`
#: rather than restated there.
#:
#: The import direction is deliberate and one-way: `tasks` already imports
#: `state` (for `utcnow_iso`), so the list lives here and `tasks` reads it.
#: Restating it on the registry side is how the record written at acceptance
#: and the comparison run at reconciliation would come to disagree about what
#: "the same successor" means — and that comparison is the whole of the
#: fail-closed guarantee.
#:
#: `priority` is deliberately ABSENT. It is the one task field an operator may
#: rewrite at any moment (`TaskStore.apply_priority`), so a successor whose
#: priority was steered between the crash and the recovery would compare unequal
#: to the intent and fail closed on a change the loop is supposed to tolerate.
#: Nothing about a split depends on the ordering of its successors.
SPLIT_DEFINITION_KEYS: tuple[str, ...] = (
    "id",
    "title",
    "description",
    "depends_on",
    "approved_paths",
)

#: WHICH execution record a `SplitIntent` undertook to archive — the record's
#: own account of which attempt it describes, captured at acceptance and
#: compared, in full, before that record is moved and again when an already
#: archived one is accepted as proof.
#:
#: It exists because `task_id` alone is not an identity. A crash after the
#: intent was written, followed by the parent being re-dispatched or repaired by
#: hand, leaves a DIFFERENT record living at `executions/<parent>.json`; matching
#: on the id would archive that replacement and discharge the intent as if the
#: original had been retired.
#:
#: These five fields and no others, deliberately. They are what the record says
#: about the attempt it belongs to — its branch, its worker directory, the base
#: it forked from and the candidate it produced — all written once and not
#: rewritten by ordinary bookkeeping. Everything else on `TaskExecution` is
#: bookkeeping that legitimately moves while an intent is outstanding
#: (`attempt_count`, `fault_attempt_count`, `attempt_ledger`, `review_round`,
#: `assumptions`, `report_summary`, …) — `cli._clear_fault_budget_on_answer`
#: rewrites one of them when an operator answers the very blocker a parked split
#: raises. Binding those, or a digest of the whole file, would turn answering the
#: park into a permanent park.
#:
#: The residual, stated rather than hidden: a replacement record created for the
#: same task on the same branch, from the same base, with nothing committed yet,
#: compares EQUAL to the original. What is then archived is a record whose every
#: identifying field matches the one the intent accepted, and the only thing lost
#: is bookkeeping the successors do not inherit. The worker half is pinned
#: independently by the repository's own HEAD (below), which such a replacement
#: does not reproduce.
SPLIT_RECORD_PROVENANCE_KEYS: tuple[str, ...] = (
    "task_id",
    "task_branch",
    "worktree_path",
    "task_base_sha",
    "candidate_sha",
)

#: WHICH worker repository a `SplitIntent` undertook to quarantine, read from the
#: repository itself at acceptance (`orchestrator._worker_repo_identity`).
#:
#: `path` is DIAGNOSTIC — it records where the worker stood when the intent was
#: written, for the park message and for a human. It is deliberately not part of
#: the comparison: a quarantined worker lives at a different path by definition,
#: so comparing it would fail on exactly the success case this proof exists to
#: recognise.
SPLIT_WORKER_PROVENANCE_KEYS: tuple[str, ...] = ("path", "branch", "head_sha")

#: The COMPARED half of a worker provenance block. A fresh `create()` for the
#: same task id reproduces the branch (`autoloop/<task_id>` is derived from the
#: id), so the branch alone identifies nothing; the HEAD commit is what makes a
#: replacement worker distinguishable from the one the split accepted.
SPLIT_WORKER_IDENTITY_KEYS: tuple[str, ...] = ("branch", "head_sha")


def split_worker_identity(provenance: dict | None) -> dict:
    """The compared projection of a worker provenance block.

    One projection, used for BOTH sides of every comparison, so the block the
    intent carries and the block read back off disk can never be narrowed
    differently.
    """
    source = provenance or {}
    return {key: str(source.get(key, "")) for key in SPLIT_WORKER_IDENTITY_KEYS}


def _load_split_provenance(
    kind: str,
    flag_name: str,
    flag: object,
    raw: object,
    keys: tuple[str, ...],
    non_empty: tuple[str, ...],
) -> dict | None:
    """Validate one provenance block against the retirement flag it belongs to.

    Fail-closed in BOTH directions, because each direction is a different lie:
    a flag saying there is a half to retire with no provenance to identify it
    would let recovery retire whatever it finds, and provenance with the flag
    off would describe a retirement nobody undertook.
    """
    if not isinstance(flag, bool):
        raise StateCorruptError(
            f"split intent {flag_name!r} must be a boolean, got {flag!r} — a "
            "coerced value either fabricates a retirement obligation or "
            "silently discharges one"
        )
    if not flag:
        if raw not in (None, {}):
            raise StateCorruptError(
                f"split intent carries {kind} provenance while {flag_name} is "
                f"False — it would describe a retirement nobody undertook: {raw!r}"
            )
        return None
    if not isinstance(raw, dict):
        raise StateCorruptError(
            f"split intent {flag_name} is True but its {kind} provenance is not "
            f"an object: {raw!r} — without it recovery cannot tell the accepted "
            f"{kind} from a replacement"
        )
    unknown = set(raw) - set(keys)
    if unknown:
        raise StateCorruptError(
            f"split intent {kind} provenance carries unknown field(s) {sorted(unknown)}"
        )
    loaded: dict = {}
    for key in keys:
        if key not in raw:
            raise StateCorruptError(
                f"split intent {kind} provenance is missing {key!r}"
            )
        value = raw[key]
        if not isinstance(value, str):
            raise StateCorruptError(
                f"split intent {kind} provenance field {key!r} is not a string: {value!r}"
            )
        if key in non_empty and not value:
            raise StateCorruptError(
                f"split intent {kind} provenance field {key!r} is empty — an empty "
                "identity compares equal to anything that cannot be read"
            )
        loaded[key] = value
    return loaded


@dataclass
class SplitIntent:
    """The durable record of a split that has been ACCEPTED but may not yet be
    applied to every store it spans.

    Split acceptance touches three things that cannot be written in one atomic
    operation — the task registry (`tasks.json`), the task's execution record
    (`executions/<id>.json`) and its worker repository (a directory) — so any
    crash between two of them leaves the three disagreeing: `tasks.json` says
    the parent is retired while its record and worker still describe live work.

    This record is the answer, and it is the same shape `publisher.py` already
    uses for a push that may or may not have landed: write the intent FIRST,
    then reconcile every store against it, idempotently, until they all agree.
    A crash is then never a contradiction — it is an unfinished intent, and the
    next start finishes it (`orchestrator._reconcile_split_intent`).

    Every field is captured at ACCEPTANCE and never recomputed afterwards,
    because a value re-derived during recovery is a value that can disagree
    with what was actually undertaken:

      * `label` is minted once and is what BOTH retirement halves are filed
        under (`executions/archive/<parent>-<label>.json`,
        `quarantine/<parent>-<label>`), so a re-run finds its own earlier work
        instead of creating a second copy under a fresh timestamp. This is why
        `worktask.retire_execution` — which mints its own label per call — is
        not usable here.
      * `retire_record` / `retire_worker` say which halves there were anything
        to undertake for. Without them, "the record is gone" is ambiguous
        between "this intent archived it" and "there was never one", and the
        second reading would let a missing archive pass as success.
      * `record_provenance` / `worker_provenance` say WHICH record and WHICH
        repository those flags are about. The flags alone answer "was there one
        to retire?"; only these answer "is the one on disk now still it?". A
        crash after this record is written, followed by the parent being
        re-dispatched or repaired by hand, leaves a DIFFERENT record at
        `executions/<parent>.json` and a DIFFERENT repository at
        `workers/<parent>` — and retiring by id alone would destroy that
        replacement while discharging the intent as though the original had
        been retired. Every comparison runs against these captured values, both
        before a live half is moved and when an already archived or quarantined
        half is accepted as proof; a mismatch parks with the intent intact
        rather than overwriting anything. See `SPLIT_RECORD_PROVENANCE_KEYS` and
        `SPLIT_WORKER_PROVENANCE_KEYS` for what each binds and why it is those
        fields.
      * `successors` carries the FULL definition of each new task, so recovery
        never has to re-read a directive that is no longer in hand.
    """

    parent_id: str
    #: One dict per successor, each carrying exactly `SPLIT_DEFINITION_KEYS`.
    #: Plain dicts rather than `contract.TaskSpec`, matching the convention
    #: `LoopState.task_execution` / `changeset` already use: they round-trip
    #: through the state file's JSON with no custom decoding, and this module
    #: does not import `contract`.
    successors: tuple[dict, ...]
    reason: str
    label: str
    #: Was there an execution record to archive when this intent was written?
    #: REQUIRED on the wire (see `from_dict`) — a default would read a real
    #: retirement obligation as "nothing to do".
    retire_record: bool
    #: Was there a worker repository to quarantine? Same rule.
    retire_worker: bool
    #: Identity of that record / that repository, REQUIRED exactly when the
    #: matching flag is True and refused when it is False (`__post_init__`).
    #: Keys are `SPLIT_RECORD_PROVENANCE_KEYS` / `SPLIT_WORKER_PROVENANCE_KEYS`.
    record_provenance: dict | None = None
    worker_provenance: dict | None = None
    created_at: str = field(default_factory=utcnow_iso)

    def __post_init__(self) -> None:
        """Enforce the flag/provenance pairing on EVERY construction, not only
        on the way back off disk.

        Both routes into this record have to obey the same rule — an intent
        minted in-process with a retirement flag set and no identity for it
        would be unverifiable from the moment it was written, and the state file
        is the wrong place to discover that.
        """
        self.record_provenance = _load_split_provenance(
            "execution-record",
            "retire_record",
            self.retire_record,
            self.record_provenance,
            SPLIT_RECORD_PROVENANCE_KEYS,
            non_empty=("task_id",),
        )
        self.worker_provenance = _load_split_provenance(
            "worker-repository",
            "retire_worker",
            self.retire_worker,
            self.worker_provenance,
            SPLIT_WORKER_PROVENANCE_KEYS,
            non_empty=SPLIT_WORKER_IDENTITY_KEYS,
        )
        if (
            self.record_provenance is not None
            and self.record_provenance["task_id"] != self.parent_id
        ):
            raise StateCorruptError(
                f"split intent for {self.parent_id!r} binds an execution record "
                f"belonging to {self.record_provenance['task_id']!r}"
            )

    def successor_ids(self) -> tuple[str, ...]:
        return tuple(str(spec.get("id", "")) for spec in self.successors)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "SplitIntent":
        """Rebuild an intent off the state file, or raise `StateCorruptError`.

        FAIL CLOSED on every field, unlike the tolerant `.get(...)` defaults the
        rest of this module uses for backward compatibility. There is no such
        thing as an intent written by an older build — the field did not exist —
        so a missing key here is corruption rather than history, and the two
        booleans in particular must never default: reading an absent
        `retire_worker` as `False` would silently discharge a quarantine that
        never happened.

        Nor are they COERCED. `bool(raw["retire_worker"])` reads the string
        `"false"` as True (fabricating a quarantine obligation that will never
        be dischargeable) and `0` as False (silently discharging a real one), so
        both flags must arrive as actual booleans — enforced, with the
        provenance blocks they pair with, in `__post_init__`.
        """
        if not isinstance(raw, dict):
            raise StateCorruptError(f"split intent is not an object: {raw!r}")
        missing = [
            key
            for key in (
                "parent_id",
                "successors",
                "reason",
                "label",
                "retire_record",
                "retire_worker",
            )
            if key not in raw
        ]
        if missing:
            raise StateCorruptError(
                f"split intent is missing required field(s) {sorted(missing)} — "
                "it cannot be reconciled, and guessing at either retirement half "
                "would discharge work that may never have happened"
            )
        specs = raw.get("successors")
        if not isinstance(specs, (list, tuple)) or not specs:
            raise StateCorruptError(
                f"split intent for {raw.get('parent_id')!r} carries no successors"
            )
        successors = tuple(_load_split_successor(spec) for spec in specs)
        return cls(
            parent_id=str(raw["parent_id"]),
            successors=successors,
            reason=str(raw.get("reason") or ""),
            label=str(raw["label"]),
            # Passed through UNCOERCED, with `__post_init__` doing the type
            # check and the pairing check — see this method's own docstring.
            retire_record=raw["retire_record"],
            retire_worker=raw["retire_worker"],
            record_provenance=raw.get("record_provenance"),
            worker_provenance=raw.get("worker_provenance"),
            created_at=str(raw.get("created_at") or ""),
        )


def _load_split_successor(spec: object) -> dict:
    """One successor definition off the state file, normalised to exactly
    `SPLIT_DEFINITION_KEYS`.

    The normalisation is what makes the reconciliation comparison meaningful:
    JSON has no tuples, so a round-tripped `depends_on` comes back as a list and
    would never compare equal to the registry's tuple. Doing it here — once, on
    the way in — is what keeps `TaskRegistry.split_applied` a comparison rather
    than a coercion.
    """
    if not isinstance(spec, dict):
        raise StateCorruptError(f"split intent successor is not an object: {spec!r}")
    unknown = set(spec) - set(SPLIT_DEFINITION_KEYS)
    if unknown:
        raise StateCorruptError(
            f"split intent successor {spec.get('id')!r} carries unknown "
            f"field(s) {sorted(unknown)}"
        )
    normalised: dict = {}
    for key in SPLIT_DEFINITION_KEYS:
        if key not in spec:
            raise StateCorruptError(
                f"split intent successor {spec.get('id')!r} is missing {key!r}"
            )
        value = spec[key]
        if key in ("depends_on", "approved_paths"):
            if isinstance(value, str) or not isinstance(value, (list, tuple)):
                raise StateCorruptError(
                    f"split intent successor {spec.get('id')!r} has a malformed "
                    f"{key!r}: {value!r}"
                )
            normalised[key] = tuple(str(item) for item in value)
        else:
            normalised[key] = str(value)
    return normalised


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
    #: The task the loop has ASKED the reviewer to split, and has not yet had an
    #: answer about. `""` — the ordinary state — means no ask is outstanding.
    #:
    #: An ask, never an authorization: it records that
    #: `orchestrator._split_request` appended the question to a round's payload,
    #: and nothing more. It is what makes the reviewer's `plan` reply mean
    #: "these tasks REPLACE that one" rather than the ordinary "add these to the
    #: roadmap", so it is spent by ANY other decision (see
    #: `orchestrator._dispatch`) — a reviewer who answers something else has
    #: declined, and a marker left standing would silently reinterpret an
    #: unrelated plan three rounds later as a retirement.
    split_requested_for: str = ""
    #: Serialised `SplitIntent` (a plain dict — `SplitIntent.to_dict()`, never a
    #: reconstructed dataclass instance here, same convention as
    #: `task_execution` / `changeset` above) for a split that has been accepted
    #: and may not yet be applied to all three stores it spans.
    #:
    #: `None` means there is nothing outstanding. Anything else is an obligation
    #: the next start must discharge BEFORE it selects or dispatches anything —
    #: see `orchestrator._reconcile_split_intent`, which is idempotent and is
    #: what turns a crash mid-acceptance into an unfinished intent rather than a
    #: registry that describes a task whose execution record and worker repo
    #: still exist.
    #:
    #: Cleared ONLY once every store has been INSPECTED and found to agree with
    #: it — never merely because the writes were attempted.
    split_intent: dict | None = None
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
