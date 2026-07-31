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

from .errors import StateCorruptError, StateError

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
# NOT bumped for the "silent conversation" rotation entry condition either
# (`PendingRequest.start_timeouts` / `start_timeout_wait_seconds`). Same
# reasoning again: both are new fields with defaults of 0, and 0 is exactly
# correct for a request loaded from a state file written before this existed
# — it has no recorded response-start timeouts to backfill, and treating it
# as having none is the truth, not a guess.
SCHEMA_VERSION = 3


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Phase(str, Enum):
    READY = "ready"            # outbox holds the next payload to send
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
    STOPPED = "stopped"        # ChatGPT decided stop
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
    #: Which conversation this reply was actually read from, copied from the
    #: request it answers. Recorded so "only a response captured from the
    #: request's bound conversation may authorize action" is auditable after
    #: the fact, not merely enforced in the moment by whichever client the
    #: awaiting phase happened to hold.
    conversation_url: str = ""
    conversation_epoch: int = 0


def _load_postcommit(raw: dict | None) -> PostcommitBinding | None:
    return PostcommitBinding(**raw) if raw else None


def _load_pending_request(raw: dict | None) -> PendingRequest | None:
    if not raw:
        return None
    data = dict(raw)
    data["postcommit"] = _load_postcommit(data.get("postcommit"))
    return PendingRequest(**data)


def _load_last_response(raw: dict | None) -> LastResponse | None:
    if not raw:
        return None
    data = dict(raw)
    data["postcommit"] = _load_postcommit(data.get("postcommit"))
    return LastResponse(**data)


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
    parse_retries: int = 0
    policy_denials: int = 0
    outbox: str | None = None
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
    #: Current conversation generation. Requests are stamped with it, so a
    #: response captured under an older epoch can be recognised and ignored.
    conversation_epoch: int = 0
    #: Completed rotations this run. Checked against
    #: `PolicyConfig.max_conversation_rotations` BEFORE each rotation.
    rotations: int = 0
    #: The most recent completed rotation, as a plain dict (`asdict` of a
    #: `RotationRecord`). Read by the CLI's config-drift guard: after a
    #: rotation the state legitimately points somewhere the config does not
    #: yet, and this record is what distinguishes that from an operator
    #: editing the config out from under a live session.
    last_rotation: dict | None = None
    question: str | None = None
    resume_phase: str | None = None
    stop_reason: str | None = None
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
        tmp.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def archive(self) -> Path | None:
        """Move the current state file aside (used by `reset`). Returns the backup path."""
        if not self.path.exists():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        os.replace(self.path, backup)
        return backup
