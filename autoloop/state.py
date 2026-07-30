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
    question: str | None = None
    resume_phase: str | None = None
    stop_reason: str | None = None
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
