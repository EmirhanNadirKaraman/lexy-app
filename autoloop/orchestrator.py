"""The orchestrator: a persisted state machine around one LLM conversation.

Phases (see state.Phase):

    ready ──► submitting ──► awaiting ──► executing ─┬─► ready        (loop)
                   │                                 ├─► stopped      (stop,
                   │                                 │   or a fault the loop
                   │                                 │   cannot ask about —
                   │                                 │   see stop_kind)
                   │ send attempted,                  └─► needs_user  (budgets)
                   │ acceptance unknown
                   ▼
   submission_unconfirmed ──reconcile, then search──┬─► awaiting   (it is there)
                                                    └─► needs_user (ambiguous;
                                                        NEVER an automatic resend)

Phase 2 additions on top of the v1 machine:

* Work is task-based: `plan` fills the TaskRegistry, `implement`/`revise`
  reference task ids, policy validates the referenced task's graph state.
* Review integrity: every request is stamped (request_id, timestamp,
  head_sha, base_sha, report_sha256) in its CONTEXT block; git approvals must
  echo the stamp (`contract.verify_review`) AND the repository HEAD must still
  equal the approved head at execution time.
* The full prompt is rendered once in `ready` and persisted, so a crash-retry
  resubmits byte-identical content and the stamps stay truthful.
* All outgoing payloads come from `prompts.TEMPLATES`; the CONTEXT block is
  built by `context.build_context` from state + git + registry.

Phase 3.1 (browser-transport repair) additions:

* `submitting` reconciles once (controlled reload) BEFORE sending, so the
  duplicate check reads persisted history — an optimistic user bubble in the
  live DOM is never taken as proof a message was accepted.
* An attempted-but-unconfirmed send lands in `submission_unconfirmed`, which
  only ever READS. It never resends: the backend may have accepted a
  message the browser failed to observe, so an automatic retry could
  double-post. Resolution is either "it did persist" → awaiting, or a park for
  the operator (`run --retry` to reconcile again, `run --resubmit` to allow one
  more send of the same request id).
* Reading is two-stage, because a reload proves presence but cannot establish
  absence in a VIRTUALIZED list: when the reload comes back empty, the
  by-content search (`find_conversation_with`, which mounts the tail) gets the
  last word. Finding the request in this request's own conversation resolves
  the park automatically — nothing is ambiguous and resuming sends nothing.
  Not finding it changes nothing: the loop still parks. Presence is proved and
  acted on; absence is never inferred and never acted on
  (`_resolve_or_park_ambiguous`).
* `awaiting` performs no navigation at all, so a streaming answer survives.

Transport-recovery additions (this change):

* A send whose acceptance is POSITIVELY DISPROVEN by the browser's own network
  activity lands in `submission_rejected`, not `submission_unconfirmed`. That
  phase reconciles for confirmation and, on confirmed absence, permits exactly
  ONE same-chat resend of the same request id. Unknown acceptance still never
  earns a resend — the whole point of the distinction.
* A second confirmed rejection, or a conversation that is structurally unusable,
  may `rotate`: open one fresh chat in the configured project, prove it usable,
  and rebind the in-flight request to it. Bounded by
  `policy.max_conversation_rotations` (default 1 per run); no project URL
  configured means no rotation, ever — the loop parks instead.
* Every request carries its OWN authoritative `conversation_url` and
  `conversation_epoch`. Submitting, awaiting and reconciling all follow the
  request's URL, never the loop's current one, so a late reply in an abandoned
  chat cannot authorize anything.
* A CONFIRMED, persisted send whose assistant turn never begins generating
  (`ResponseTimeoutError` with `stage="start"`, repeated) is a THIRD, distinct
  rotation trigger — the "silent conversation" — layered on top of the
  ordinary failure budget rather than routed around it. Three consecutive
  such timeouts for the same request, an accumulated measured wait of at
  least 3x `response_start_timeout_seconds`, and a FINAL reconciliation that
  still finds no assistant turn started, together authorize exactly one
  rotation (`_handle_response_start_timeout` / `_attempt_silence_rotation`).
  A response that already started and is merely slow (`stage="complete"`)
  never qualifies, and a reply that appears during that final reconciliation
  cancels the attempt.

Note for the merge with the blocker/quarantine work (commit 5346551, branch
`feat/autoloop-postcommit-review`): every park site added here goes through the
existing two-argument `_to_needs_user` and emits a stable `reason_code` in its
transcript event (`submission_unknown`, `rotation_unavailable`,
`rotation_cap_reached`, `rotation_failed`, `conversation_unusable`). When the two
lines meet, classifying them is a matter of passing the kind that matches each
code — no new taxonomy is invented here.

Failure routing:

* LoginExpiredError            → needs_user, resume_phase preserved (--retry)
* RateLimitedError             → classified first, then BACK OFF and re-probe,
                                 phase unchanged: an account-level THROTTLE is
                                 not a transport fault and no restart can clear
                                 it. For that state — a throttle modal up on an
                                 attachable page — it never restarts the
                                 browser, never spends
                                 `max_consecutive_failures` and never drops the
                                 client (each is another request into the window
                                 that caused the limit). Charged to
                                 `policy.max_rate_limit_backoffs`, which ends
                                 in a needs_user park naming the throttle
                                 (`rate_limited`).
                                 The same error with NO attachable page behind
                                 it is not a rate limit at all: nothing is
                                 refusing the loop, it has no browser. That
                                 state (`RL_BROWSER_UNATTACHABLE`) drops the
                                 client, restarts the profile ONCE, re-probes,
                                 and otherwise parks naming the BROWSER
                                 (`browser_unattachable`) — outside the
                                 back-off budget, which bounds waiting on the
                                 server. A limit that has already cleared
                                 resumes without a wait
                                 (`_classify_rate_limit_state`)
* ResponseTimeoutError(start)  → ordinary budget as below, PLUS: 3rd
                                 consecutive one for the same request may
                                 rotate (see "silent conversation" above)
* other BrowserError           → drop conversation, try a browser restart,
                                 retry same phase; failure budget exhausted →
                                 failed (resume via --retry). EXCEPT when the
                                 restart was skipped for
                                 `browser.restart_cooldown_seconds`: that
                                 failure was never acted on, so it is charged
                                 to `policy.max_browser_restart_skips` instead
                                 and ends in a needs_user park naming the
                                 cooldown (`browser_restart_cooldown_blocked`)
* GitError                     → reported back to ChatGPT (budget-capped);
                                 in `ready` (context build) → needs_user with
                                 the outbox preserved
* ContractError (parse)        → corrective re-prompt; budget-capped
* review-integrity mismatch    → failure_recovery re-prompt; denial budget
                                 exhausted → needs_user (the repository may
                                 have moved under the loop — a human-side
                                 explanation exists, so it asks)
* plan reject                  → failure_recovery re-prompt; denial budget
                                 exhausted → needs_user (the roadmap is an
                                 operator-owned artefact they can repair)
* policy denial                → failure_recovery re-prompt; denial budget
                                 exhausted → STOPPED with `stop_kind="fault"`
                                 (`_to_fault_stop`). Not a park: only the
                                 reviewer could produce a different directive,
                                 so there is no question for a human to answer.
                                 A blocker record is still written.

This routing is only as good as the transport's promise to raise inside the
hierarchy, and on 2026-08-15 that promise broke: a Playwright driver-channel
failure is a PLAIN `Exception` (`rewrite_error` gives it no type), so it matched
no clause here, reached the top level and ended the process with
`phase=submitting`, `stop_reason=None` and no blocker. `browser/playwright_session.py`
now guards every Playwright call POSITIONALLY rather than by type, so a browser
fault always arrives here as a `BrowserError` and always leaves a record. Add no
catch-all in `run()` — the routing is by type by design, and a blanket handler
would swallow the non-browser bugs each clause above deliberately distinguishes.

Blocker classification (continuous mode, see `docs/AUTOLOOP.md`'s blockers
section for the full table):

* Every `_to_needs_user` call site is classified `kind="task_fatal"` (one
  task's own unit of work cannot proceed — `cli.py`'s `run --continuous`
  quarantines that task via `TaskRegistry.block` and keeps working whatever
  else is READY) or `kind="loop_fatal"` (the environment or the operator is
  the problem — the whole loop stops, exactly as every park did before this
  classification existed). The default is `kind="loop_fatal"`: an
  unclassified or newly added park site fails closed, never silently
  quarantining a task on a park nobody has reasoned about.
* Every park, of either kind, is persisted as a `blockers.Blocker` (when a
  `BlockerStore` is configured — always true in production; `None` in tests
  that construct a minimal `Orchestrator`) carrying the exact question text
  the operator would have seen, so `python -m autoloop blockers`/`answer`
  can list and resolve it even after the session that recorded it is gone.
* A FAULT STOP (`_to_fault_stop`) records the same `loop_fatal` blocker
  without parking: the run ends in `stopped` with `stop_kind="fault"`. Use it
  where the cause is the reviewer's own behaviour, so no operator answer could
  change what happens next; use `_to_needs_user` wherever a human doing
  something makes the SAME session resumable.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from . import environment
from . import escape_detector
from .auto_merge import (
    UPGRADE_PENDING,
    AutoMerger,
    MergeDeferral,
    MergeDeferralStore,
    UpgradeStore,
)
from .blockers import NO_TASK, BlockerStore
from .browser.chatgpt import SubmitResult
from .browser.observation import SendOutcome
from .browser.playwright_session import attachable_page_targets
from .changeset_review import ChangesetBinding
from .config import AutoloopConfig
from .config_writer import update_conversation_url
from .context import build_context, render_context
from .contract import (
    AUDIT_TASK_ID,
    COMMIT_DECISIONS,
    PUSH_DECISIONS,
    RETIRED_DECISIONS,
    REVIEWED_DECISIONS,
    TASK_DECISIONS,
    Decision,
    Directive,
    parse_response,
    verify_review,
)
from .errors import (
    AutoloopError,
    BrowserError,
    ContractError,
    ConversationSearchInconclusive,
    ConversationUnusableError,
    ExecutorError,
    EnvironmentDriftError,
    GitCommandError,
    GitError,
    LoginExpiredError,
    QuotaExhaustedError,
    RateLimitedError,
    ResponseTimeoutError,
    StateError,
    TaskGraphError,
)
from .manifest import ManifestStore
from .executor import ExecutionOutcome, TaskExecutor
from . import heartbeat
from .git_gateway import GitGateway
from .packet import (
    DIFF_INCLUDE_MAX_CHARS,
    attached_payload,
    build_review_packet_with_diff,
    omission_payload,
    payload_carries_diff,
    plan_chunked_delivery,
)
from .policy import PolicyEngine, Verdict, retired_decision_verdict
from .publisher import Publisher, redact_url
from .worker_env import verify_worker_isolation, worker_env, worker_repo_is_reusable
from .prompts import (
    TEMPLATES,
    build_prompt,
    git_error_payload,
    parse_error_payload,
    plan_rejected_payload,
    policy_denied_payload,
    review_mismatch_payload,
)
from .state import (
    TERMINAL_PHASES,
    ChunkedDelivery,
    LastResponse,
    LoopState,
    PendingRequest,
    Phase,
    PostcommitBinding,
    ProviderSwitch,
    RotationRecord,
    StateStore,
    utcnow_iso,
)
from .inbox import apply_requests
from .tasks import (
    TRACKER_PATHS,
    Task,
    TaskRegistry,
    TaskState,
    TaskStore,
    effective_approved_paths,
    unauthorized_paths,
)
from .transcript import Stopwatch, TranscriptLogger
from .validation import run_validation_commands, select_validation_commands
from .worktask import (
    ATTEMPT_FAULT,
    ATTEMPT_OPEN,
    ATTEMPT_PENDING,
    ATTEMPT_PENDING_FAULT,
    ATTEMPT_TASK,
    REASON_SENT_FOR_REVIEW,
    CommitIntent,
    IntentStore,
    Reconciliation,
    TaskExecution,
    TaskExecutionStore,
    accumulate_assumptions,
    attempt_outcome,
    compose_reason,
    format_attempt,
    reconcile_after_crash,
    retire_execution,
    split_attempt,
)
from .worktree import WorktreeManager


#: Independent ceiling on commit/packet attempts for one task, including those
#: that never produced a review. `review_round` counts only dispatched reviews,
#: so on its own it would let repeated structural refusals churn locally
#: without bound.
#:
#: A STRUCTURAL REFUSAL IS CHARGED HERE, deliberately, and that decision is the
#: whole reason this constant is not simply "rounds the reviewer judged". A
#: refusal for touching files outside `approved_paths`, a failing validation, a
#: post-commit verification failure — the reviewer never saw any of them, but
#: every one is a genuine defect in the candidate, produced by the task's own
#: work, and repeating it is exactly the unbounded local churn this ceiling
#: exists to stop. Giving refusals a separate, smaller allowance was the
#: alternative considered; it was rejected because it splits one kind of
#: failure across two counters while solving nothing — a refusal-heavy task and
#: a revise-heavy task are both the task failing to converge, and they belong
#: in the same budget.
MAX_TASK_ATTEMPTS = 5

#: `run()`'s outcome when a merge has changed the loop's own code and the loop
#: has reached a boundary at which its process may be replaced. NOT a phase:
#: the session is mid-flight and untouched — the outbox is durable, no packet
#: has been prepared, nothing is parked — so the caller either performs the
#: replacement (`cli._self_upgrade_at_boundary`, continuous mode) or reports it
#: and leaves the record for the next run. Resuming after either is the
#: ordinary "state is non-terminal, keep going" path, with no special case.
SELF_UPGRADE = "self_upgrade"

#: The SECOND budget, and the one that answers "a fault must not spend a task's
#: attempt budget" (task budget-01, 2026-08-17) without removing the bound that
#: charging faults was providing.
#:
#: Measured 2026-08-15..17: brw-09 reached 5 attempts with review_round 1 (four
#: structural refusals), exec-01 reached 5 with review_round 1 after two rounds
#: died to a provider 429 that "produced no work", brw-11 reached 4 with an
#: agent-level API error at 368 seconds, and port-01, dash-04 and hlth-01 had
#: the same shape. All six were repaired by an operator editing the execution
#: record by hand. Two unrelated things were sharing one counter.
#:
#: They no longer do: a round destroyed by a provider throttle, a supervisor
#: kill, a session-ending browser fault or a process that died mid-round is
#: charged HERE, and a task converging through real review rounds is no longer
#: killed by rounds it did not cause. What has NOT changed is that faults still
#: cost something and still terminate — a task that dies to a fault every
#: single round exhausts this budget in `MAX_TASK_FAULT_ATTEMPTS` dispatches and
#: parks on `fault_attempt_ceiling`, which is the bound the pre-executor
#: increment used to supply on its own.
#:
#: TOTAL BOUND, stated rather than derived: every dispatch appends exactly one
#: `TaskExecution.attempt_ledger` entry and charges exactly one of the two
#: counters, so `attempt_count + fault_attempt_count == len(attempt_ledger)`
#: is an invariant (reclassification MOVES a charge, never drops one), and a
#: dispatch requires BOTH counters to be strictly under their ceilings.
#: One task can therefore never dispatch more than
#: `MAX_TASK_ATTEMPTS + MAX_TASK_FAULT_ATTEMPTS - 1` = 9 times without an
#: operator intervening — and the only intervention that grants more is
#: answering the `fault_attempt_ceiling` blocker, which resets the fault
#: counter alone and leaves `attempt_count` exactly where it was
#: (`cli._clear_fault_budget_on_answer`).
MAX_TASK_FAULT_ATTEMPTS = 5

#: Appended to a request that is being re-sent into a replacement conversation.
#: One line, because the payload it follows is already self-contained — every
#: turn carries its own CONTEXT block and the full response contract. It says
#: which conversation is authoritative and nothing else; replaying the
#: abandoned chat's history by hand would be reconstructing evidence rather
#: than continuing work.
CONTINUATION_NOTE = (
    "[autoloop transport note] This request continues an Autoloop session after "
    "a conversation-transport recovery. The previous conversation is abandoned "
    "and nothing in it is authoritative. Reply only here; this conversation is "
    "the only one Autoloop reads."
)


#: A replacement chat has no address inside the project until its first turn is
#: processed, so the rotation polls for one. Bounded: an address that never
#: becomes a project conversation is a failed rotation, not something to wait on
#: forever.
#: How long to wait for the address bar to show a conversation UNDER THE
#: PROJECT after a rotation posts — not merely an address that differs from
#: the project page, which is what this used to wait for and is evidence of
#: nothing (see `PLACEHOLDER_CONVERSATION_PREFIX`). Only the FAST PATH — when
#: it expires, the chat is found by the request id it contains
#: (`find_conversation_with`), so this being short costs a few page loads
#: rather than the whole rotation. It used to be the only witness, and 20s
#: against an account whose composer needs 180s failed three rotations that
#: had actually succeeded. 30s is deliberate on that account: the content
#: search is the backstop, so a longer wait would only delay it.
ROTATION_URL_TIMEOUT_SECONDS = 30.0
ROTATION_URL_POLL_SECONDS = 0.5

#: The address a chat opened from a project page carries BEFORE its first
#: message lands: `https://chatgpt.com/c/WEB:<uuid>` — note it is not under the
#: project prefix at all, so it fails the membership check exactly like a chat
#: in some other project would (2026-08-16, one LOOP-FATAL park). It is used
#: ONLY to explain a refusal to the operator; nothing branches on it, because a
#: URL shape ChatGPT owns is the wrong thing to build control flow on, and an
#: address about to gain the project prefix must be waited through, not judged.
PLACEHOLDER_CONVERSATION_PREFIX = "WEB:"


def _conversation_id(url: str) -> str | None:
    """The id a `/c/<id>` URL ends with, or None for anything else.

    Mirrors `BrowserChatGPT._conversation_id` deliberately rather than
    importing it: URL comparison on the orchestrator's side of the seam is
    already its own (`_url_in_project`), and the alternative is the
    orchestrator reaching into one adapter's private helper to reason about
    conversations every provider has.
    """
    segs = [seg for seg in urlsplit(url).path.split("/") if seg]
    if len(segs) >= 2 and segs[-2] == "c":
        return segs[-1]
    return None


def _is_placeholder_conversation(url: str) -> bool:
    """True for the pre-persistence address a brand-new chat carries.

    Explanatory only — see `PLACEHOLDER_CONVERSATION_PREFIX`. A rotation waits
    the same bounded wait for EVERY address that is not yet inside the project;
    this exists so the park it eventually writes says "the placeholder was
    still there" instead of leaving an operator to recognise the shape.
    """
    conversation = _conversation_id(url)
    return bool(conversation and conversation.startswith(PLACEHOLDER_CONVERSATION_PREFIX))


#: The four outcomes of `Orchestrator._browser_restart_outcome`. They are
#: distinguished — rather than collapsed into "did the browser come back" —
#: because `RESTART_SKIPPED_COOLDOWN` is the one case where recovery was never
#: ATTEMPTED, and a failure nobody tried to recover from must not be charged to
#: the budget that decides recovery is hopeless. See `_handle_browser_failure`.
RESTART_OK = "restarted"
RESTART_FAILED = "failed"
RESTART_SKIPPED_COOLDOWN = "skipped_cooldown"
RESTART_DISABLED = "disabled"


#: The three worlds `Orchestrator._classify_rate_limit_state` tells apart
#: before it backs off, and before it concludes a limit still holds. Only the
#: third one restarts anything — see `_handle_rate_limited`, whose refusal to
#: restart on a THROTTLE is unchanged and load-bearing.
RL_THROTTLED = "throttled"
RL_CLEARED = "cleared"
RL_BROWSER_UNATTACHABLE = "browser_unattachable"

#: A fifth restart outcome, produced only by `_recover_unattachable_browser`
#: and never by `_browser_restart_outcome`: this throttle episode has already
#: spent its ONE restart on an unattachable browser. Distinct from
#: `RESTART_SKIPPED_COOLDOWN` because it is a per-episode bound rather than a
#: time one, and the operator-facing sentence differs — nothing is about to
#: elapse.
RESTART_SKIPPED_ALREADY_SPENT = "skipped_already_spent"

#: The one piece of evidence that PROVES a throttle: the modal's testid found
#: on a page the loop can drive. `RL_THROTTLED` is also the default answer —
#: what a page that could not be probed and an endpoint that could not be
#: measured produce — so the two are told apart by this exact string rather
#: than by the classification, and the park says only what was really seen.
MODAL_SIGHTED = "the throttle modal is up on a page this loop can drive"


class Orchestrator:
    #: The clock every measured stage duration is read from (prof-01,
    #: 2026-08-20). MONOTONIC, not wall clock: this measures an interval, and
    #: `utcnow_iso` — the thing the transcript already stamps — is the wall
    #: clock, which an NTP step or a DST edge can move underneath a running
    #: operation. Held as a class attribute rather than a constructor
    #: parameter so no existing construction site changes; a test that needs a
    #: controlled elapsed time sets `orch._timing_clock = fake`, and the
    #: instance attribute shadows this.
    _timing_clock = staticmethod(time.monotonic)

    def __init__(
        self,
        config: AutoloopConfig,
        store: StateStore,
        state: LoopState,
        policy: PolicyEngine,
        git: GitGateway,
        executor: TaskExecutor,
        transcript: TranscriptLogger,
        client_factory,
        registry: TaskRegistry,
        task_store: TaskStore,
        manifest_store: ManifestStore,
        worktrees: WorktreeManager | None = None,
        execution_store: TaskExecutionStore | None = None,
        intent_store: IntentStore | None = None,
        blocker_store: BlockerStore | None = None,
        validation_runner=None,
        validation_env=None,
        task_inbox=None,
        publisher: Publisher | None = None,
        worker_repos=None,
        publisher_url_snapshot: str | None = None,
        config_path: Path | None = None,
        provider_factory=None,
        sleep=time.sleep,
        self_upgrade_enabled: bool = False,
    ):
        self._config = config
        self._store = store
        self.state = state
        self._policy = policy
        self._git = git
        self._executor = executor
        self._transcript = transcript
        self._client_factory = client_factory
        #: Optional provider-aware factory (`provider_name -> LLMConversation`).
        #: Additive on purpose: when it is absent the zero-argument
        #: `client_factory` is used exactly as before, so every existing caller
        #: and test is unaffected and single-provider runs stay single-provider.
        #: `cli._build_orchestrator` passes one, which is what makes failover
        #: reachable in production.
        self._provider_factory = provider_factory
        self._registry = registry
        self._task_store = task_store
        #: Vestigial since the S21 retirement (2026-07-30, docs/SECURITY.md):
        #: kept as a constructor parameter so every existing caller/test is
        #: unaffected, but nothing in this class writes to it anymore — the
        #: only writer was `_dispatch_executor`'s legacy manifest branch, and
        #: the only reader was `_step_ready`'s adoption stamping, both removed.
        self._manifest_store = manifest_store
        # Produce-then-review commit path. `worktrees` / `worker_repos` are
        # the two mutually-exclusive backends for where a task's own working
        # repository lives (`worker_repos` wins if both are set — see
        # `_dispatch_task_postcommit`); `execution_store` / `intent_store`
        # are required alongside either one. Since the S21 retirement this is
        # the ONLY dispatch path for audit/implement/revise — an Orchestrator
        # built with none of these configured raises `ExecutorError` the
        # first time it needs to dispatch, rather than silently doing
        # nothing (see the guard at the top of `_dispatch_task_postcommit`).
        self._worktrees = worktrees
        self._execution_store = execution_store
        self._intent_store = intent_store
        #: Persisted operator-facing blocker records (`blockers.py`).
        #: Optional, like every other produce-then-review collaborator:
        #: `None` (many existing tests that hand-build a minimal
        #: Orchestrator) means `_to_needs_user` still classifies and logs
        #: every park exactly as before, it just does not ALSO persist a
        #: `Blocker` — see that method. `_build_orchestrator` (production)
        #: always wires a real one.
        self._blocker_store = blocker_store
        #: Injected `subprocess.run`-compatible callable for post-commit
        #: validation, mirroring `AuditExecutor`'s `command_runner` — lets
        #: tests avoid depending on a real `ruff`/`pytest` install.
        self._validation_runner = validation_runner
        #: Dedicated TEST database credentials for the POST-COMMIT validation
        #: re-run (`validation_env.py`). `None` = that re-run gets none, which
        #: is the pre-existing behaviour and correct for validation commands
        #: that never touch a database. Same object the `ImplementExecutor`
        #: holds; both are post-writer validation, and the writer subprocess
        #: itself is explicitly stripped of these variables.
        self._validation_env = validation_env
        #: Operator task requests submitted from outside the checkout
        #: (`inbox.TaskInbox`). Optional: `None` (most tests) simply means
        #: nothing is drained, exactly as before this existed.
        self._task_inbox = task_inbox
        #: monotonic timestamp of the last browser restart, for the cooldown.
        self._last_browser_restart = None
        #: True once this episode has already spent its ONE restart on an
        #: unattachable browser (`_recover_unattachable_browser`). The restart
        #: cooldown normally bounds that on its own, but a deployment running
        #: `restart_cooldown_seconds = 0` would otherwise get a restart loop:
        #: each attempt "succeeds", each re-probe still finds no page.
        #:
        #: An EPISODE ends in two places. A step that COMPLETES (`run`'s
        #: `else` branch, UNCONDITIONALLY: state 3 never increments
        #: `rate_limit_backoffs`, so a clear nested inside that counter would
        #: never fire for the very fault this bounds) — that one is evidence.
        #: And any `RateLimitedError` classified as something OTHER than
        #: unattachable (`_handle_rate_limited`), which includes the default
        #: reached when nothing could be probed at all; see that site for why
        #: that is deliberate rather than evidence. Neither is a successful
        #: re-probe after the restart: targets that exist at probe time and
        #: are gone at attach time would then restart, clear, restart — the
        #: loop the bound exists to stop.
        self._rate_limit_browser_restarted = False
        # Autoloop M2 (`publisher.py`). Optional and independently gated from
        # the `worktrees`/`execution_store`/`intent_store` triple above: when
        # `None` (every existing caller and test), `_dispatch_task_push`
        # publishes exactly as before — `worktree_git.push_exact` straight
        # from the task's own linked worktree. When set, publication routes
        # through the dedicated `Publisher` repo instead: the reviewed
        # candidate is imported by exact object id from the worktree, then
        # published from the SEPARATE publisher repo, never from the
        # worktree's own (main-checkout-shared) git configuration.
        self._publisher = publisher
        # Autoloop M2 worker side. When set, a task's working repository is a
        # SEPARATE local repo (no remote, no credentials, controlled empty
        # hooks dir) instead of a linked worktree that shares `.git` — and
        # therefore `origin` — with the main checkout. Gated like the rest:
        # `None` keeps the pre-M2 linked-worktree behaviour.
        self._worker_repos = worker_repos
        #: The publisher's remote-url snapshot at the moment it was last
        #: (re)provisioned (`publisher.read_publisher_url_snapshot`), passed
        #: in by the caller rather than re-read here on every dispatch — so a
        #: `reprovision-publisher` run mid-session by a DIFFERENT process is
        #: picked up only on the next Orchestrator construction, never
        #: silently mid-run. Only meaningful when `publisher` is set;
        #: `_dispatch_task_push` compares it against the MAIN checkout's
        #: LIVE `remote.<remote>.url` before every publish and refuses (never
        #: auto-heals) on a mismatch — see that method's docstring.
        self._publisher_url_snapshot = publisher_url_snapshot
        #: Where the config file lives, so a completed rotation can point it at
        #: the replacement conversation. Defaults to the conventional location
        #: under `state_dir`; the CLI passes the path it actually loaded, since
        #: `--config` can put it anywhere. Only ever written through
        #: `config_writer`, which refuses git-tracked paths.
        self._config_path = Path(config_path) if config_path else config.state_dir / "config.toml"
        #: Auto-merge's retry queue (`auto_merge.MergeDeferralStore`). Built
        #: here rather than inside `_auto_merge_after_completion` because TWO
        #: places need the same view of it: the merger itself, and
        #: `_reconcile_published_execution`, which must not retire an execution
        #: record while a deferral still depends on it (see that method). A
        #: second store constructed at the other site would be the same
        #: directory, but the coupling is deliberate enough to make explicit.
        self._merge_deferrals = MergeDeferralStore(config.merge_deferrals_dir)
        #: The one `PendingUpgrade` record (`auto_merge.UpgradeStore`). WRITTEN
        #: by the merger after a merge that touched `autoloop/`; READ here, at
        #: the between-round boundary, to decide whether to hand the process
        #: back to the caller for replacement. Nothing in this class execs.
        self._upgrades = UpgradeStore(config.pending_upgrade_file)
        #: May this orchestrator end a round with `SELF_UPGRADE` at all?
        #:
        #: Default OFF, and enabled in exactly one place — `cli.
        #: _build_orchestrator`, which is what both `run` paths use. The record
        #: lives under `state_dir`, so every orchestrator sharing that directory
        #: would otherwise see it, including `smoke-browser`'s: that command
        #: builds its own, starts it at `ready` with no pending request (the
        #: boundary shape exactly), and reports PASS only for a clean contract
        #: stop — so an unrelated pending upgrade would make a diagnostic
        #: command fail while diagnosing nothing. A caller that cannot act on
        #: the boundary should not be offered it.
        self._self_upgrade_enabled = bool(self_upgrade_enabled)
        #: Injected so a rate-limit back-off can be tested without waiting out
        #: a real one. The ONLY thing in this class that blocks deliberately;
        #: every other wait belongs to the transport or to a subprocess.
        self._sleep = sleep
        self._client = None

    # ---- main loop ----------------------------------------------------------

    def run(self, max_steps: int | None = None) -> str:
        """Run until a terminal phase, a pause request, or max_steps."""
        steps = 0
        while True:
            # BOTH locations: the flag moved outside the checkout (see
            # `AutoloopConfig.pause_file`), and one written by an older
            # build must still stop the loop rather than be ignored.
            if (
                self._config.pause_file.exists()
                or self._config.legacy_pause_file.exists()
            ):
                self._log("paused")
                heartbeat.publish(self._config, self.state, heartbeat.PAUSED)
                return "paused"
            # One per phase step. That is as often as a single-threaded loop
            # can report: it is blocked inside an agent call for minutes at a
            # time, which is exactly why the monitor's staleness threshold is
            # generous rather than tight.
            heartbeat.publish(self._config, self.state)
            # Between steps, never inside one. The inbox lives outside the
            # checkout, so an operator may submit at any instant — including
            # while a write-capable agent is running — but the MERGE has to
            # happen where no execute window is open and the registry is not
            # being written by anything else. Here the loop holds its own lock
            # and owns the registry, so it stays the only writer of tasks.json.
            self._drain_task_inbox()
            phase = Phase(self.state.phase)
            if phase in TERMINAL_PHASES:
                return phase.value
            # After the terminal check, so a parked loop reports what it is
            # parked on rather than a restart nobody can act on, and before the
            # step budget, so `--max-steps` cannot hide the boundary.
            if self._self_upgrade_due(phase):
                return SELF_UPGRADE
            if max_steps is not None and steps >= max_steps:
                return phase.value
            steps += 1
            # Before the step, because the step is what talks to ChatGPT. An
            # unfinished back-off left by a killed process is served here — the
            # ordinary case is no wait outstanding and this costs nothing. It
            # sits AFTER the terminal-phase check on purpose: a parked loop is
            # not about to make a request, so it should return, not sleep.
            self._await_rate_limit_deadline()
            try:
                self._step(phase)
            except LoginExpiredError as exc:
                # Deliberately caught BEFORE ConversationUnusableError: a
                # logged-out profile is an account problem, and opening a new
                # chat cannot fix it. Rotating here would spend the run's one
                # rotation on a login prompt.
                self._log("login_expired", data={"error": str(exc)})
                self._drop_client()
                self._to_needs_user(
                    str(exc),
                    resume_phase=phase.value,
                    kind="loop_fatal",
                    code="login_expired",
                )
            except RateLimitedError as exc:
                # Neighbour of the clause below, and distinct from it for one
                # reason: an exhausted ALLOWANCE is answered by changing
                # provider, while a temporary account THROTTLE is answered by
                # waiting. Neither is a BrowserError, and this one especially
                # must not become one — the browser recovery is restart and
                # retry, and both are additional requests into the window that
                # caused the limit.
                self._handle_rate_limited(phase, exc)
            except QuotaExhaustedError as exc:
                # Not a BrowserError, and caught explicitly: an exhausted plan
                # allowance is an account condition. Routed through the failure
                # budget it would spend three retries in seconds and land in
                # `failed`, describing neither the cause nor the remedy.
                self._handle_quota_exhausted(phase, exc)
            except ConversationUnusableError as exc:
                self._handle_conversation_unusable(phase, exc)
            except ResponseTimeoutError as exc:
                # Caught BEFORE the generic BrowserError below (it is one),
                # so a repeated, confirmed-silent response-START timeout can
                # be routed to the rotation entry condition instead of only
                # ever retrying on the ordinary failure budget. See
                # `_handle_response_start_timeout`.
                self._handle_response_start_timeout(phase, exc)
            except BrowserError as exc:
                self._handle_browser_failure(phase, exc)
            except GitError as exc:
                self._handle_git_failure(phase, exc)
            except StateError as exc:
                # LAST in the chain, so every specific handler above still
                # wins, and safe to add because StateError parents only
                # StateCorruptError — neither is caught elsewhere here.
                #
                # A state inconsistency genuinely needs a human. What it must
                # NOT do is leave by propagating out of the process: this loop
                # explains itself on the way down everywhere else, and this one
                # path did not, so two runs vanished leaving no blocker, no
                # park and no heartbeat — indistinguishable from being killed,
                # and invisible to the monitor whose whole job is noticing
                # (2026-08-03, twice). Park instead, with a durable record.
                self._log("state_error", data={"error": str(exc)})
                self._to_needs_user(
                    str(exc),
                    resume_phase=phase.value,
                    kind="loop_fatal",
                    code="state_inconsistent",
                )
            else:
                # A step that COMPLETED is the only honest evidence that
                # ChatGPT's account throttle has lifted, and it is free — no
                # extra request, no probe. Dismissing the modal proves nothing:
                # it is gone because the loop closed it, not because the
                # server-side limit expired, so resetting on that would make
                # the back-off a fixed-interval retry that never escalates,
                # never reaches `max_rate_limit_backoffs` and never parks —
                # a slower version of the hammering this exists to stop.
                #
                # The same completed step also ends any UNATTACHABLE-BROWSER
                # episode, and that clear sits OUTSIDE the counter check below
                # on purpose: state 3 deliberately never increments
                # `rate_limit_backoffs`, so nested inside it the common
                # sequence (zero targets → restart → ordinary step completes)
                # skipped the reset entirely and left the guard true for the
                # rest of the process — parking the next, unrelated incident
                # as `skipped_already_spent` instead of giving it the one
                # restart it is owed. In-memory only, so no save is owed for
                # it.
                self._rate_limit_browser_restarted = False
                if self.state.rate_limit_backoffs:
                    self.state.rate_limit_backoffs = 0
                    self.state.rate_limit_wait_seconds = 0.0
                    # The episode is over, so any deadline it left is stale.
                    # Normally already `None` (the wait cleared it when it
                    # finished); belt and braces for the step that completes
                    # against a state file some other path wrote.
                    self.state.rate_limit_retry_not_before = None
                    self._store.save(self.state)

    def _step(self, phase: Phase) -> None:
        if phase is Phase.READY:
            self._step_ready()
        elif phase is Phase.DELIVERING:
            self._step_delivering()
        elif phase is Phase.SUBMITTING:
            self._step_submitting()
        elif phase is Phase.SUBMISSION_UNCONFIRMED:
            self._step_submission_unconfirmed()
        elif phase is Phase.SUBMISSION_REJECTED:
            self._step_submission_rejected()
        elif phase is Phase.AWAITING:
            self._step_awaiting()
        elif phase is Phase.EXECUTING:
            self._step_executing()
        else:  # pragma: no cover - terminal phases filtered in run()
            raise StateError(f"cannot step from phase {phase.value}")

    # ---- the restart boundary ------------------------------------------------

    def _self_upgrade_due(self, phase: Phase) -> bool:
        """Is this the moment at which the process may be replaced?

        **`READY` with no pending request, and nothing else.** That is the
        instant BEFORE the next request is prepared: `_step_ready` is what
        builds a packet, so nothing has been sent and nothing is awaited; the
        payload lives in `state.outbox`, already saved; the executor is not
        running (a write-capable agent runs inside `_step_executing`, a
        different phase, synchronously — reaching here means it returned); and
        every `TaskExecution` was written by whoever last touched it, since
        this class saves records as it goes rather than at an exit. A
        replacement here loses nothing: the successor loads the same state file
        and prepares the same request from the same outbox.

        Every other phase is mid-round by construction and is refused —
        `submitting` and `submission_unconfirmed` have a packet in flight,
        `awaiting` has a reviewer holding one, `executing` has an agent writing
        into a worker repo. `pending_request` is checked SEPARATELY from the
        phase because it outlives its own phase: a request answered and not yet
        consumed is still a packet this loop owes something to.

        Reads the record, never writes it. An unreadable or already-settled
        record answers False — the fail-closed direction here is to keep
        running the code that works. So does an orchestrator whose caller never
        asked for the boundary (`self_upgrade_enabled`, see the constructor).
        """
        if not self._self_upgrade_enabled:
            return False
        if phase is not Phase.READY or self.state.pending_request is not None:
            return False
        try:
            record = self._upgrades.load()
        except OSError:
            return False
        if record is None or record.status != UPGRADE_PENDING:
            return False
        self._log(
            "self_upgrade_boundary",
            data={
                "base_sha": record.base_sha,
                "task_id": record.task_id,
                "paths": list(record.paths)[:20],
                "phase": phase.value,
            },
        )
        return True

    # ---- phases -------------------------------------------------------------

    def _step_ready(self) -> None:
        state = self.state
        next_iteration = state.iteration + 1
        verdict = self._policy.check_iteration_budget(next_iteration)
        if not verdict.allowed:
            # Checked BEFORE consuming outbox, so --retry after raising the
            # limit re-enters ready with the payload intact.
            self._to_needs_user(
                verdict.reason,
                resume_phase=Phase.READY.value,
                kind="loop_fatal",
                code="iteration_budget_exhausted",
            )
            return
        if state.outbox is None:
            raise StateError("phase=ready but outbox is empty — nothing to send")
        request_id = f"alr-{state.session_id[:8]}-{next_iteration:04d}"
        # Planned BEFORE the context is built, because a payload that cannot be
        # chunked is rewritten to the omission form in here — and the hash has
        # to cover the packet as it finally stands, not the one that was ruled
        # out.
        #
        # The hash then covers `state.outbox`: the COMPLETE logical packet,
        # patch inline, whether or not that patch fits in one message.
        # `sent_payload` is what actually goes out; the two differ only when the
        # patch is being deposited as parts first, and the abridged message
        # names every part id. Hashing the DELIVERED message instead would let
        # an approval bind to a review of less than the whole change — the
        # replay gap `report_sha256` exists to close.
        #
        # Measured from here — the first line of packet CONSTRUCTION — to the
        # line that finishes it, four statements down. Everything inside is
        # work this step actually does: planning the delivery, reading the
        # repository for the context, hashing the payload, rendering the
        # prompt. FROZEN THERE, not at the `request_prepared` record below:
        # between the two sit the pending-postcommit/changeset lookups, the
        # binding refusal, the `PendingRequest` construction and an execution
        # -store round trip, and `stamp()` stops a watch that is still running
        # — so leaving it going would report that bookkeeping as construction
        # time under a MEASURED label, which is the gap-is-not-the-work error
        # this exists to remove.
        prepare_watch = self._stopwatch()
        plan = self._plan_delivery(state, request_id)
        ctx = build_context(
            state,
            self._git,
            self._registry,
            request_id,
            state.outbox,
            executions=self._execution_store,
            # `config` is what lets the block carry the merge-window state, and
            # `self._git` is the gateway that check is asked through — the same
            # pair `auto_merge` passes to `cli._merge_window_blockers`, for the
            # reason `cli._candidate_publication` documents: the CLI's own
            # gateway is rooted at `Path.cwd()`, which is not this process's
            # checkout.
            config=self._config,
        )
        sent_payload = plan.final_payload if plan is not None else state.outbox
        prompt = build_prompt(request_id, next_iteration, render_context(ctx), sent_payload)
        prepare_watch.stop()  # the packet exists — everything below is bookkeeping
        postcommit = self._current_pending_postcommit(state.outbox, ctx.report_sha256)
        changeset = self._current_pending_changeset(state.outbox, ctx.report_sha256)
        if state.changeset and (state.changeset or {}).get("candidate_sha") and changeset is None:
            # A changeset review was queued but the payload does not carry all
            # four identifiers, so nothing could bind it. Refuse HERE, before
            # the packet is sent: otherwise a full review round is spent and
            # the approval that comes back cannot publish anything (that is
            # how A2 was found — after the round, not before it). There is
            # deliberately no fallback to an unbound send.
            queued = state.changeset or {}
            missing = [
                name
                for name in ("base_sha", "candidate_sha", "branch", "dest_ref")
                if not queued.get(name) or queued.get(name) not in (state.outbox or "")
            ]
            self._to_needs_user(
                "a changeset review is queued but its packet does not contain "
                f"{', '.join(missing)} as literal text, so the approval could "
                "never be bound to the candidate. Nothing was sent. Re-queue "
                "with `review-changeset` (its default rendering always includes "
                "the four identifiers) or add them to your --packet body.",
                kind="loop_fatal",
                code="changeset_binding_missing",
                detail=(
                    f"candidate={queued.get('candidate_sha', '')[:12]} "
                    f"missing={','.join(missing)}"
                ),
            )
            return
        state.pending_request = PendingRequest(
            request_id=request_id,
            payload=state.outbox,
            prompt=prompt,
            prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            head_sha=ctx.head_sha,
            base_sha=ctx.base_sha,
            report_sha256=ctx.report_sha256,
            timestamp=ctx.timestamp,
            postcommit=postcommit,
            changeset=changeset,
            # Bound HERE, at birth, not lazily on first touch.
            # `_bind_request_conversation` refuses to bind after a rotation —
            # correctly, since an unbound request cannot be attributed and
            # pointing it at the NEW chat would be the wrong repair. But its
            # premise, that every request created since carries its own
            # binding, was false: this call omitted the field, so a request was
            # born unbound and only became attributable when something touched
            # it. A rotation in that window made the guard fire on a request
            # minutes old and killed the run (2026-08-03, twice).
            conversation_url=state.conversation_url,
            conversation_epoch=state.conversation_epoch,
            delivery=(
                ChunkedDelivery(
                    parts=[
                        {
                            "part_id": part.part_id,
                            "index": part.index,
                            "total": part.total,
                            "text": part.text,
                        }
                        for part in plan.parts
                    ],
                    delivered=0,
                    fallback_payload=plan.fallback_payload,
                )
                if plan is not None
                else None
            ),
        )
        if postcommit is not None:
            # Bind the exact report this candidate was reviewed under, on the
            # TaskExecution record, so a later approval answering a DIFFERENT
            # report can never authorize publishing it.
            execution = self._execution_store.load(postcommit.task_id)
            if execution is not None:
                execution.presented_report_sha256 = ctx.report_sha256
                execution.review_request_id = request_id
                self._execution_store.save(execution)
        # The attachment moves ONTO the request, then leaves shared state. A
        # path left here would outlive its packet and be attached to a LATER
        # review — presenting one change's diff as another's, the exact
        # substitution report_sha256 exists to prevent.
        if state.outbox_attachment and state.pending_request is not None:
            state.pending_request.attachment = state.outbox_attachment
        state.outbox = None
        state.outbox_diff = None
        state.outbox_attachment = None
        state.iteration = next_iteration
        # A chunked packet deposits its parts BEFORE anything is asked. The
        # verdict message is `submitting`'s job and stays untouched: it is only
        # reached from `delivering` once every part is confirmed.
        state.phase = (
            Phase.DELIVERING.value if plan is not None else Phase.SUBMITTING.value
        )
        self._log(
            "request_prepared",
            request_id=request_id,
            data=prepare_watch.stamp({
                "head_sha": ctx.head_sha,
                "base_sha": ctx.base_sha,
                "report_sha256": ctx.report_sha256,
                "timestamp": ctx.timestamp,
                "chars": len(prompt),
                "diff_parts": len(plan.parts) if plan is not None else 0,
            }),
        )
        self._store.save(state)

    def _plan_delivery(self, state: LoopState, request_id: str):
        """Decide how `state.outbox` reaches the reviewer, and normalise the
        outbox to match that decision BEFORE anything hashes it.

        Returns a `packet.DeliveryPlan` when the payload's patch is too large
        for one message and CAN be delivered as numbered parts; `None`
        otherwise. `None` covers two very different situations, and the
        difference is already resolved by the time it is returned:

        * the ordinary case — no oversized patch, `state.outbox` is sent as it
          stands, exactly as before chunking existed;
        * chunking was ruled out (the stored patch does not match the payload,
          a part id would collide with the request id, or the patch needs more
          parts than `packet.DIFF_MAX_PARTS`) — `state.outbox` is REWRITTEN to
          the omission notice here, so the caller cannot accidentally send a
          payload that was already known not to fit.

        The provider's ability to deliver parts at all is deliberately NOT
        checked here: it is a property of the live client, and this phase must
        stay transport-free. `_step_delivering` probes it and falls back on the
        first step instead.
        """
        diff = (state.outbox_diff or "").strip()
        if not state.outbox or not diff:
            return None
        if not payload_carries_diff(state.outbox, diff):
            # The recorded patch is not inside THIS payload — a packet queued
            # by `_finish_postcommit` was replaced before it was sent (a git
            # failure re-prompt, an operator edit, a hand-modified state file).
            # Drop the stale patch rather than rewrite an unrelated payload
            # around it; nothing here is oversized as far as we can prove.
            state.outbox_diff = None
            self._log(
                "review_diff_plan_skipped",
                request_id=request_id,
                data={"reason_code": "diff_not_in_payload", "diff_chars": len(diff)},
            )
            return None
        if len(diff) <= DIFF_INCLUDE_MAX_CHARS:
            return None  # fits in one message: nothing to plan, nothing to omit
        task_exec = state.task_execution or {}

        # Prefer an upload. The composer cannot be PROVEN to hold a large
        # patch — `_enter_prompt` reads the editor back and a 30,000-character
        # part never returns its own tail — so chunking fails permanently on
        # exactly the changes most worth reviewing. A file sidesteps the editor
        # (measured 2026-08-15: a 336 KB .md was read in full). The diff is
        # written OUTSIDE the checkout: a file created under the repository
        # mid-run is what escape_detector reports, and it would park the loop.
        attachment = self._write_diff_attachment(request_id, diff)
        if attachment is not None:
            state.outbox = attached_payload(state.outbox, diff, Path(attachment).name)
            state.outbox_attachment = attachment
            self._log(
                "review_diff_attached",
                request_id=request_id,
                data={"diff_chars": len(diff), "path": attachment},
            )
            return None  # no parts: one message carries the verdict request

        plan = plan_chunked_delivery(
            state.outbox,
            diff,
            request_id,
            task_id=str(task_exec.get("task_id", "")),
            candidate_sha=str(task_exec.get("candidate_sha", "")),
        )
        if plan is None:
            state.outbox = omission_payload(state.outbox, diff)
            self._log(
                "review_diff_omitted",
                request_id=request_id,
                data={"reason_code": "not_chunkable", "diff_chars": len(diff)},
            )
        return plan

    def _write_diff_attachment(self, request_id: str, diff: str) -> str | None:
        """Write `diff` to a file for upload, or None if that is not possible.

        None is not a failure path — it means "fall back to what this loop did
        before attachments existed", so a provider without upload support, or a
        filesystem that refuses, degrades to chunking rather than stopping.
        """
        if not getattr(self._config.browser, "attach_oversized_diff", False):
            return None
        try:
            root = Path(tempfile.gettempdir()) / "autoloop-review-diffs"
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{request_id.replace('-', '_')}.md"
            path.write_text(diff, encoding="utf-8")
            return str(path)
        except OSError as exc:
            self._log(
                "review_diff_attachment_failed",
                request_id=request_id,
                data={"error": f"{type(exc).__name__}: {exc}"},
            )
            return None

    def _step_delivering(self) -> None:
        """Deposit an oversized packet's diff as numbered parts, then hand the
        verdict request to `submitting`.

        Three rules, and they are what make this safe rather than merely
        clever:

        1. **All or nothing.** No decision is requested until every part is
           confirmed present. A half-delivered patch plus a verdict is approval
           on a partial diff — strictly worse than omitting the diff, because
           the omission notice at least tells the reviewer exactly what it
           cannot see. The transition to `submitting` happens after the loop
           below, never inside it, and `_step_submitting` re-checks the same
           condition rather than trusting the transition.
        2. **Fall back to omission on any failure.** A part that does not land
           sends the pre-chunking notice — which now also disowns whatever
           parts DID land — instead of proceeding with what arrived.
        3. **The integrity binding is untouched.** `report_sha256` covers the
           complete logical packet (`req.payload`, patch inline); the parts and
           the abridged verdict message are both derived from it. A fallback
           re-renders that packet and re-stamps every place its digest is held
           (`_fall_back_to_omission`), so the hash never describes something
           other than what the reviewer was shown.

        Confirmation is by READBACK from persisted history, the same standard a
        submission is held to — a part is confirmed because the conversation
        shows it, never because the send returned.
        """
        state = self.state
        req = state.pending_request
        if req is None:
            raise StateError("phase=delivering but no pending request")
        delivery = req.delivery
        if delivery is None or not delivery.parts:
            raise StateError(
                f"phase=delivering but request {req.request_id} carries no delivery "
                "plan — there is nothing to deliver and nothing that authorises "
                "asking for a verdict"
            )
        client = self._client_for_request(req)
        client.attach()
        if not getattr(client, "supports_chunked_delivery", False):
            # Probed, not assumed. A provider whose "conversation" is a fresh
            # process per turn (`codex.conversation`) accumulates no shared
            # history, so parts sent to it would be separate reviews of
            # fragments rather than one review of the whole patch. Falling back
            # is the pre-chunking behaviour for that provider, not a
            # regression.
            self._fall_back_to_omission(req, reason_code="provider_cannot_chunk")
            return

        while delivery.delivered < len(delivery.parts):
            part = delivery.parts[delivery.delivered]
            part_id = str(part.get("part_id", ""))
            if not self._deliver_part(client, req, part_id, str(part.get("text", ""))):
                self._fall_back_to_omission(
                    req, reason_code="part_not_confirmed", part_id=part_id
                )
                return
            delivery.delivered += 1
            # Persisted per part, so a crash resumes at the first UNCONFIRMED
            # part instead of re-posting the ones already in the conversation.
            self._store.save(state)
            self._log(
                "review_part_delivered",
                request_id=req.request_id,
                data={
                    "part_id": part_id,
                    "index": delivery.delivered,
                    "total": len(delivery.parts),
                    "chars": len(str(part.get("text", ""))),
                },
            )

        self._log(
            "review_parts_complete",
            request_id=req.request_id,
            data={"parts": len(delivery.parts), "part_ids": delivery.part_ids()},
        )
        state.phase = Phase.SUBMITTING.value
        self._store.save(state)

    def _deliver_part(self, client, req: PendingRequest, part_id: str, text: str) -> bool:
        """Send one part and report whether persisted history now shows it.

        `submit`'s own return value is deliberately not the verdict. A part
        asks for nothing, so no assistant turn starts for it and the browser
        transport reports UNCONFIRMED after its submit timeout — which is the
        EXPECTED outcome here, not an ambiguity to park a human on. What
        settles it is the same thing that settles a submission: a reload, and
        the id being there afterwards.
        """
        if self._part_present(client, part_id):
            # Already in the conversation: a resumed delivery, or a send whose
            # confirmation the previous process never got to record.
            return True
        result = client.submit(part_id, text)
        if result is SubmitResult.ALREADY_PERSISTED:
            return True
        confirmed = client.reconcile(part_id) or self._part_present(client, part_id)
        if not confirmed:
            self._log(
                "review_part_absent",
                request_id=req.request_id,
                data={
                    "part_id": part_id,
                    "submit_result": getattr(result, "value", str(result)),
                    "observations": self._observation_summary(client),
                },
            )
        return confirmed

    @staticmethod
    def _part_present(client, part_id: str) -> bool:
        """Is `part_id` in the conversation as the loop can currently read it?

        Mounts the virtualized message tail first, when the provider offers
        that. ChatGPT renders only the newest few turns into the DOM
        (docs/AUTOLOOP.md §11), so a part several turns back can be present
        server-side and absent from `innerText` — and a rendered-but-unpainted
        message reading as missing is exactly how a complete delivery would be
        thrown away and replaced by an omission notice.

        Probed with `getattr`, like every other optional transport capability,
        and its ordinary failures are swallowed: mounting more history can only
        ever ADD evidence, so it must never be able to turn a present part into
        an absent one by raising.

        `BrowserError` is deliberately NOT swallowed. Its subclasses are the
        two conditions the loop routes rather than retries — a logged-out
        profile (`LoginExpiredError`, which no amount of scrolling fixes) and a
        wedged conversation (`ConversationUnusableError`, which authorizes a
        rotation). Eating those here would demote a routed fault into a silent
        "the part is absent", and the loop would answer a login prompt by
        omitting a diff.
        """
        mount = getattr(client, "mount_message_tail", None)
        if mount is not None:
            try:
                mount()
            except BrowserError:
                raise
            except Exception:
                pass
        return bool(client.has_request(part_id))

    def _fall_back_to_omission(
        self, req: PendingRequest, *, reason_code: str, part_id: str = ""
    ) -> None:
        """Abandon a chunked delivery and ask for the verdict on the packet
        with its diff OMITTED — the behaviour that predates chunking, and still
        the honest one: the reviewer is told exactly what it cannot see.

        The fallback payload also disowns any parts that already landed, so a
        reviewer never holds a fragment it believes is the whole patch.

        Re-stamping is the delicate part. Swapping the payload changes
        `report_sha256`, and that digest is held in THREE places that must
        agree or a legitimate approval is refused at push time — long after the
        mistake, with nothing on screen explaining it:

          1. the request itself (payload / prompt / prompt_sha256 / report_sha256)
          2. `postcommit.packet_sha256`, which `_dispatch_task_push` compares
          3. `TaskExecution.presented_report_sha256`, the record's own binding

        Safe to do here precisely because nothing has been sent under this
        request id yet — parts carry their own ids (`packet.diff_part_id`), so
        no message in the conversation claims the digest being replaced.
        `review_round` is deliberately NOT re-incremented: it was spent in
        `_finish_postcommit` and this is the same round, delivered differently.
        """
        state = self.state
        delivery = req.delivery
        landed = delivery.delivered if delivery is not None else 0
        payload = delivery.fallback_payload if delivery is not None else req.payload
        if not payload:  # pragma: no cover - defensive; always captured at plan time
            payload = req.payload
        ctx = build_context(state, self._git, self._registry, req.request_id, payload)
        prompt = build_prompt(
            req.request_id, state.iteration, render_context(ctx), payload
        )
        req.payload = payload
        req.prompt = prompt
        req.prompt_sha256 = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        req.report_sha256 = ctx.report_sha256
        req.head_sha = ctx.head_sha
        req.base_sha = ctx.base_sha
        req.timestamp = ctx.timestamp
        req.delivery = None
        if req.postcommit is not None:
            req.postcommit.packet_sha256 = ctx.report_sha256
            execution = self._execution_store.load(req.postcommit.task_id)
            if execution is not None:
                execution.presented_report_sha256 = ctx.report_sha256
                execution.review_request_id = req.request_id
                self._execution_store.save(execution)
        self._log(
            "review_diff_omitted",
            request_id=req.request_id,
            data={
                "reason_code": reason_code,
                "part_id": part_id,
                "parts_landed": landed,
                "report_sha256": ctx.report_sha256,
            },
        )
        state.phase = Phase.SUBMITTING.value
        self._store.save(state)

    def _step_submitting(self) -> None:
        state = self.state
        req = state.pending_request
        if req is None:
            raise StateError("phase=submitting but no pending request")
        # Rule 1, enforced here rather than left to the phase ordering that
        # normally satisfies it: this is the message that asks for a verdict,
        # so it is the right place to refuse when the patch it refers to is
        # only partly in the conversation. Without this check, "never ask on a
        # partial delivery" would be a property of two lines being in the right
        # order — true today, and silently untrue after any future edit that
        # reorders them.
        if req.delivery is not None and not req.delivery.complete:
            raise StateError(
                f"request {req.request_id} asks for a review decision while only "
                f"{req.delivery.delivered} of {len(req.delivery.parts)} diff parts "
                "are confirmed. Refusing to request a verdict on a partial patch."
            )
        client = self._client_for_request(req)
        client.attach()
        # One controlled reload BEFORE sending, so the duplicate check reads
        # persisted history rather than whatever the DOM happens to show (a
        # crashed previous run can leave an optimistic bubble behind).
        req.reconcile_attempts += 1
        if client.reconcile(req.request_id):
            self._log("request_already_submitted", request_id=req.request_id)
            req.submitted = True
            state.phase = Phase.AWAITING.value
            self._store.save(state)
            return

        if req.send_attempted:
            # A send was already attempted for this id and reconciliation says
            # it did not persist. What that licenses depends entirely on WHY:
            #
            #  * the transport disproved acceptance -> `submission_rejected`
            #    owns the decision (it may spend one same-chat resend, or
            #    rotate). Routing there rather than deciding here is what makes
            #    a crash mid-recovery resume from the same evidence the live
            #    run had, instead of silently downgrading to "ambiguous".
            #  * anything else -> genuinely ambiguous, park. The backend may
            #    have accepted a message the browser never observed.
            #  * the provider declares `idempotent_submit` -> there is no
            #    ambiguity to park on. A transport whose failed send appended
            #    nothing to any durable conversation cannot double-post on a
            #    retry, so the rule below — written for a shared, persistent
            #    chat thread — describes nothing. Probed as a capability rather
            #    than inferred, so the provider states the property and this
            #    code stays the one place that reasons about it.
            if req.last_send_outcome == SendOutcome.REJECTED.value:
                state.phase = Phase.SUBMISSION_REJECTED.value
                self._store.save(state)
                return
            if getattr(client, "idempotent_submit", False):
                req.send_attempted = False
                self._log(
                    "resend_authorized",
                    request_id=req.request_id,
                    data={
                        "reason_code": "idempotent_transport",
                        "provider": self.active_provider(),
                    },
                )
                self._store.save(state)
                # Deliberately fall THROUGH to the ordinary send path below
                # rather than parking: with nothing left behind by the failed
                # attempt, re-issuing is the correct action, not a compromise.
            else:
                self._resolve_or_park_ambiguous(req, reconciled=True)
                return

        # Defensive integrity check: `req.prompt` is set once in `ready` and
        # never recomputed before a resend, so this should never fire in
        # normal operation. It exists to catch on-disk corruption or manual
        # state-file tampering between a crash and a retry — sending a prompt
        # nobody actually reviewed the stamps for is worse than parking.
        # `prompt_sha256 == ""` means it was never stamped (a hand-built
        # `PendingRequest`, or a state file from before this field existed —
        # SCHEMA_VERSION was deliberately not bumped for it) rather than a
        # mismatch, so that case is skipped, not treated as corruption.
        actual_prompt_sha256 = hashlib.sha256(req.prompt.encode("utf-8")).hexdigest()
        if req.prompt_sha256 and actual_prompt_sha256 != req.prompt_sha256:
            self._to_needs_user(
                f"pending request {req.request_id}'s prompt does not match its "
                "recorded prompt_sha256 — the state file may be corrupted or "
                "was edited by hand. Refusing to send a prompt that was never "
                "reviewed. Inspect .autoloop/state.json before retrying.",
                resume_phase=Phase.SUBMITTING.value,
                kind="loop_fatal",
                code="prompt_integrity_mismatch",
                detail=f"request_id={req.request_id}",
            )
            return

        # Mark PESSIMISTICALLY and durably before handing control to the
        # transport: from here on a send may have happened, so any crash or
        # exception must lead recovery to reconcile rather than resend. The
        # marker is cleared below only when the transport proves nothing left
        # the browser. (Setting it after submit() returns would lose the fact
        # whenever submit raised *after* clicking Send — e.g. login expiry
        # during confirmation, a dying page, or SIGKILL — and the next run
        # would happily post a duplicate.)
        req.send_attempted = True
        # Stamped with the send, not the preparation: a request prepared under
        # one provider and handed to another must name the one that actually
        # answered it, or the `reviewed` stamp it comes back with is
        # unattributable.
        req.provider = self.active_provider()
        self._store.save(state)
        # Measured around the SEND itself, at its call site — not around the
        # attach, the controlled reload or the duplicate check above, which are
        # separate transport work and would inflate this into a second gap
        # number wearing a measured label. Those stay inside the
        # `request_prepared` → `request_submitted` gap, where the profiler
        # already says the window is wider than the work. Every early return
        # above and below leaves the round unsent, so none of them stamps a
        # duration — a rejected or unconfirmed send is not a completed submit.
        # The window CLOSES on the transport's return (below), not at the
        # `request_submitted` record: the verdict persistence and the two
        # reconciliation branches in between are the loop's bookkeeping about
        # the send, not the send.
        submit_watch = self._stopwatch()
        try:
            # The attachment rides with the request it belongs to. Passed
            # positionally-by-name so a provider that does not accept it fails
            # loudly at the call rather than silently sending a review request
            # whose diff never arrived.
            attachment = getattr(req, "attachment", "") or None
            if attachment:
                result = client.submit(req.request_id, req.prompt, attachment=attachment)
            else:
                result = client.submit(req.request_id, req.prompt)
        except BrowserError:
            if not getattr(client, "send_attempted", True):
                # Nothing was sent (composer/Send never accepted the input), so
                # a later retry is unambiguous and may submit normally.
                req.send_attempted = False
                self._store.save(state)
            raise
        # THE boundary: the transport has answered. Frozen here, stamped onto
        # `request_submitted` further down (first stop wins, so the later
        # `stamp` reports this reading and not a fresher one).
        submit_watch.stop()
        # Persist the transport's verdict before acting on it, so a crash
        # between here and the next step cannot lose the distinction between
        # "disproven" and "unknown".
        req.last_send_outcome = self._client_send_outcome(client)
        if result is SubmitResult.REJECTED:
            # The browser's own send request failed: acceptance is disproven,
            # not merely unobserved. Still not self-authorizing — the dedicated
            # phase reconciles for confirmation before anything is resent.
            req.last_send_outcome = SendOutcome.REJECTED.value
            state.phase = Phase.SUBMISSION_REJECTED.value
            self._log(
                "submission_rejected",
                request_id=req.request_id,
                data={
                    "note": "the browser's own send request failed — confirming by reconciliation",
                    "observations": self._observation_summary(client),
                },
            )
            self._store.save(state)
            return
        if result is SubmitResult.UNCONFIRMED:
            # Ambiguous: the send was clicked but acceptance is unknown. Park in
            # a dedicated phase — resending here could double-post.
            state.phase = Phase.SUBMISSION_UNCONFIRMED.value
            self._log(
                "submission_unconfirmed",
                request_id=req.request_id,
                data={"note": "send attempted, acceptance unknown — reconciling next"},
            )
            self._store.save(state)
            return

        req.submitted = True
        state.phase = Phase.AWAITING.value
        self._log(
            "request_submitted",
            request_id=req.request_id,
            data=submit_watch.stamp({"result": result.value, "prompt": req.prompt}),
        )
        self._store.save(state)

    def _step_submission_unconfirmed(self) -> None:
        """Resolve an ambiguous send by READING only — never by resending.

        Two reads, in order of cost: the controlled reload below, then (only
        when it comes back empty) the by-content search in
        `_resolve_or_park_ambiguous`, which mounts the tail of a virtualized
        list the reload's window may never have painted. Both prove presence;
        neither can conclude absence into an action.
        """
        state = self.state
        req = state.pending_request
        if req is None:
            raise StateError("phase=submission_unconfirmed but no pending request")
        client = self._client_for_request(req)
        client.attach()
        req.reconcile_attempts += 1
        persisted = client.reconcile(req.request_id)
        self._log(
            "reconciled",
            request_id=req.request_id,
            data={"persisted": persisted, "attempts": req.reconcile_attempts},
        )
        if persisted:
            req.submitted = True
            state.phase = Phase.AWAITING.value
            self._store.save(state)
            return
        self._resolve_or_park_ambiguous(req, reconciled=True)

    def _step_submission_rejected(self) -> None:
        """Resolve a DISPROVEN send.

        Reconciliation is still the authority — persisted history outranks the
        network every time, so a request that turns out to be there simply
        proceeds. Only confirmed absence unlocks anything, and what it unlocks
        is bounded: one same-chat resend of the same request id, then (on a
        second confirmed rejection) at most one rotation.
        """
        state = self.state
        req = state.pending_request
        if req is None:
            raise StateError("phase=submission_rejected but no pending request")
        client = self._client_for_request(req)
        client.attach()
        req.reconcile_attempts += 1
        persisted = client.reconcile(req.request_id)
        self._log(
            "reconciled",
            request_id=req.request_id,
            data={
                "persisted": persisted,
                "attempts": req.reconcile_attempts,
                "after": "rejected_send",
            },
        )
        if persisted:
            # The network said the send failed and history says it is there.
            # History wins: it is the direct evidence, the status code is a
            # proxy. Resending now would be the double-post this whole phase
            # exists to prevent.
            req.submitted = True
            req.last_send_outcome = SendOutcome.ACCEPTED.value
            state.phase = Phase.AWAITING.value
            self._store.save(state)
            return

        if req.resends_used < 1:
            # Confirmed absent, once. Nothing is in the conversation, so
            # sending the same request id again cannot duplicate anything.
            req.resends_used += 1
            req.send_attempted = False
            req.last_send_outcome = ""
            state.phase = Phase.SUBMITTING.value
            self._log(
                "resend_authorized",
                request_id=req.request_id,
                data={
                    "reason_code": "send_rejected_confirmed_absent",
                    "resends_used": req.resends_used,
                    "conversation_epoch": req.conversation_epoch,
                },
            )
            self._store.save(state)
            return

        # Confirmed absent twice, in the same chat, with the transport
        # disproving both sends. The chat itself is the suspect now.
        self._attempt_rotation(req, reason="send_rejected_twice")

    def _rate_limit_delay(self, backoffs: int) -> float:
        """The wait for the `backoffs`-th consecutive throttle, doubling from
        `browser.rate_limit_backoff_seconds` up to
        `rate_limit_backoff_max_seconds`.

        Escalating rather than fixed because a limit still up after the first
        wait is a limit the first wait was too short for, and re-probing on a
        fixed short interval is itself a request stream — a slower version of
        the hammering this exists to stop.
        """
        base = max(0.0, float(self._config.browser.rate_limit_backoff_seconds))
        ceiling = max(0.0, float(self._config.browser.rate_limit_backoff_max_seconds))
        delay = base * (2 ** max(0, backoffs - 1))
        return min(delay, ceiling) if ceiling else delay

    def _rate_limit_deadline(self) -> datetime | None:
        """The persisted `retry_not_before` instant, or `None` when no wait is
        in progress.

        An unparseable value reads as no wait rather than raising, and is
        DISCARDED on the spot — a stamp nothing can read is a wait nobody can
        serve, and left in place it would re-log on every step for the rest of
        the session. The counter is what bounds the episode; a state file
        somebody hand-edited must not take the loop down, and if the limit is
        still in force the next step raises `RateLimitedError` again and
        re-enters the back-off with `rate_limit_backoffs` intact.
        """
        raw = self.state.rate_limit_retry_not_before
        if not raw:
            return None
        try:
            deadline = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            # TypeError as well as ValueError: a hand-edited file can hold a
            # NUMBER there, and `fromisoformat(123)` raises the one the obvious
            # `except ValueError` misses — straight out of `run()`, which is the
            # opposite of what this fail-open exists for.
            self._log(
                "rate_limit_deadline_unreadable",
                data={"reason_code": "rate_limited", "retry_not_before": raw},
            )
            self.state.rate_limit_retry_not_before = None
            self._store.save(self.state)
            return None
        # A file written by hand can carry a naive stamp; everything this loop
        # writes is UTC-aware.
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline

    def _await_rate_limit_deadline(self) -> None:
        """Serve whatever remains of a back-off STARTED BY AN EARLIER PROCESS,
        before this one touches ChatGPT. No-op — and free — when no wait is in
        progress, which is every ordinary step.

        This is the half that makes the back-off durable. `rate_limit_backoffs`
        alone records that a wait was ENTERED, not that it was served: a
        process killed just after that save would resume treating the whole
        delay as waited and re-enter the browser step immediately, so a
        supervisor restarting the loop could skip every back-off in turn — the
        restart storm this task exists to stop, one level up.

        The remainder is CLAMPED to the delay the schedule prescribes for the
        current streak. Without it a backward system-clock jump or a
        hand-edited stamp becomes an arbitrarily long sleep, and this loop
        publishes its heartbeat BETWEEN steps: a sleep inside one is a gap in
        the record, which is why `browser.rate_limit_backoff_max_seconds` is
        deliberately kept under the monitor's 45-minute staleness threshold.
        The clamp is what keeps that ceiling meaning what it says.
        """
        deadline = self._rate_limit_deadline()
        if deadline is None:
            return
        planned = self._rate_limit_delay(self.state.rate_limit_backoffs)
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        remaining = min(planned, max(0.0, remaining))
        self._log(
            "rate_limit_wait_resumed",
            data={
                "reason_code": "rate_limited",
                "backoffs": self.state.rate_limit_backoffs,
                "retry_not_before": self.state.rate_limit_retry_not_before,
                "remaining_seconds": remaining,
                "waited_seconds": self.state.rate_limit_wait_seconds,
            },
        )
        self._serve_rate_limit_wait(remaining, planned)

    def _serve_rate_limit_wait(self, seconds: float, planned: float) -> None:
        """Sleep `seconds`, then credit the wait, clear the deadline and clear
        the modal. The one place any of that happens, so the fresh and the
        resumed path cannot drift apart.

        `planned` is credited rather than `seconds` because they differ only on
        the resumed path, where the difference was spent with this process
        dead — and for a server-side limit the remedy is calendar time in which
        the account makes no requests, which a dead process supplies as well as
        a sleeping one. Crediting after the sleep, never before, is what keeps
        `rate_limit_wait_seconds` a record of waiting that actually happened:
        a crash mid-wait credits nothing, and the resuming process credits the
        episode once, when its deadline is finally met.
        """
        if seconds > 0:
            self._sleep(seconds)
        state = self.state
        state.rate_limit_wait_seconds += planned
        state.rate_limit_retry_not_before = None
        self._store.save(state)
        # Clear the overlay so the next step meets the page. This is NOT a
        # verdict on the limit — see `_dismiss_rate_limit_modal`. The streak
        # ends where a step completes (`run`), not here.
        self._dismiss_rate_limit_modal()

    def _dismiss_rate_limit_modal(self) -> None:
        """Close the throttle overlay, if this transport can, so the next step
        meets the page rather than the modal.

        Necessary because the modal hides the composer even after the
        server-side limit expires: left standing, a stale one would read as a
        throttle that never lifts and spend the whole back-off budget against
        nothing.

        **Its result is deliberately not returned, because it means nothing.**
        The overlay being gone afterwards says only that the loop closed it —
        the limit is server-side and answers to a timer, not to a click. The
        one honest signal that it lifted is a STEP that completes, which is
        where the streak is reset (see `run`). Treating a dismissal as
        evidence made the back-off a fixed-interval retry that never
        escalated and could never park.

        Reads `self._client` directly rather than `_get_client()`: that would
        CONSTRUCT a client when none is held, and constructing the Playwright
        one binds to the configured conversation and can navigate — the extra
        request this whole path exists to avoid. No client held means nothing
        to dismiss; the next step's own `attach` sees the modal and raises,
        which is the correct outcome.
        """
        client = self._client
        if client is None:
            return
        dismiss = getattr(client, "dismiss_rate_limit_modal", None)
        if dismiss is None:
            return
        try:
            dismiss()
        except Exception:
            # Best-effort throughout. A failed dismissal costs one more
            # occurrence of an already-bounded wait, never a second fault on
            # top of the throttle.
            pass

    def _attachable_page_targets(self) -> int | None:
        """How many pages the configured CDP endpoint reports, or None when
        that could not be measured.

        A method rather than a direct call so a test can describe the browser
        without opening a socket — the loop suite is hermetic, and this is the
        one probe here that dials anything.
        """
        return attachable_page_targets(self._config.browser.cdp_url)

    def _composer_takes_a_click(self) -> bool:
        """Positive interaction evidence from the page already held, or False.

        Never constructs a client (same rule as `_dismiss_rate_limit_modal`:
        constructing the Playwright one binds to the conversation and can
        navigate, which is another request into the window that caused the
        limit), and never claims interactivity for a transport that cannot
        demonstrate it.
        """
        probe = getattr(self._client, "composer_interactive", None)
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:
            return False

    def _classify_rate_limit_state(self) -> tuple[str, str]:
        """Which of three worlds a `RateLimitedError` actually arrived from:
        `(RL_THROTTLED | RL_CLEARED | RL_BROWSER_UNATTACHABLE, evidence)`.

        The back-off's whole justification — no restart, no failure budget, no
        client drop — assumes the browser is USABLE and merely being refused.
        On 2026-08-17 that assumption was false: the operator closed the
        browser window, Chrome stayed alive, `/json/version` kept answering
        with a valid `webSocketDebuggerUrl`, and `/json/list` returned ZERO
        targets. Playwright could not attach at all, so there was no page to
        dismiss a modal on and nothing to re-probe. The loop spent its whole
        budget waiting and parked saying "rate_limited" while the real cause
        was that it had no browser; a probe reported "still rate limited" for
        four hours.

        Order is the safety property. **The modal on a held page is asked
        FIRST**, so a genuine throttle is classified from the evidence the
        selector docstring calls the only reliable one, and never from a
        transiently odd answer at the CDP endpoint. State 1 therefore behaves
        exactly as it did before this existed.

        A composer is not consulted as presence: it reports visible and
        enabled while the overlay swallows every click (2026-08-15, three false
        all-clears). `RL_CLEARED` requires a real click to LAND.

        The target count is the last question, asked only when the page could
        not answer. Zero attachable pages is the one condition that means "not
        a rate limit"; an unmeasurable endpoint is NOT evidence of anything and
        keeps today's behaviour, because a browser that cannot be reached at
        all already raises `BrowserError` from the next step and gets the
        restart-and-budget path built for it.
        """
        modal: bool | None = None
        note = "no client is held, so the page itself could not be probed"
        if self._client is not None:
            try:
                modal = bool(self._client.is_rate_limited())
            except Exception as exc:
                # Includes the transport that has no such probe. "Could not
                # ask" is not "throttled" — the same rule `_check_throttled`
                # already applies in the other direction.
                modal = None
                note = f"the held page could not be probed ({type(exc).__name__}: {exc})"
        if modal is True:
            return (RL_THROTTLED, MODAL_SIGHTED)
        if modal is False:
            if self._composer_takes_a_click():
                return (
                    RL_CLEARED,
                    "the throttle modal is gone and a real click on the composer landed",
                )
            note = "no throttle modal on the held page, but the composer would not take a click"
        targets = self._attachable_page_targets()
        if targets == 0:
            return (
                RL_BROWSER_UNATTACHABLE,
                f"{self._config.browser.cdp_url} answers but lists no attachable page "
                f"target, so there is no page to be throttled ({note})",
            )
        if targets is None:
            return (RL_THROTTLED, f"{note}; the CDP endpoint could not be measured")
        return (RL_THROTTLED, f"{note}; the CDP endpoint lists {targets} attachable page(s)")

    def _recover_unattachable_browser(
        self, phase: Phase, exc: RateLimitedError, evidence: str
    ) -> None:
        """State 3: there is no browser to be rate limited. Restart it once and
        re-probe, or park NAMING THE BROWSER.

        Deliberately outside the back-off budget. `rate_limit_backoffs` bounds
        waiting on the SERVER; this is a local recovery that makes no request
        of ChatGPT at all, and charging it there would spend an operator's
        evidence about the throttle on a fault that is not one.

        The client IS dropped here, unlike every other path out of
        `_handle_rate_limited`: there is no page behind it, and the restart is
        about to end the browser process it was bound to. That is not a
        weakening of the no-drop rule — that rule protects a page that still
        exists.

        Exactly one restart, then one re-probe, then a decision. Returning
        without parking on a still-dead browser would leave the loop to
        rediscover the same state on its next step with nothing bounding the
        repetition. The bound is per EPISODE
        (`_rate_limit_browser_restarted`), not only per cooldown window: a
        deployment running `restart_cooldown_seconds = 0` has disabled the
        time bound, and every attempt here "succeeds" while the re-probe still
        finds no page — a restart loop by another door.

        A recovered re-probe does NOT end the episode; a step that COMPLETES
        does (`run`). Targets can exist at probe time and be gone at attach
        time, and clearing the bound here would answer that with restart →
        probe OK → clear → restart, unbounded and never parking. Ending it on
        a completed step keeps the bound per-episode in both directions: this
        recovery cannot repeat inside one episode, and an episode that really
        ended cannot leave the next fault with a spent restart.
        """
        state = self.state
        self._log(
            "browser_unattachable",
            data={
                "reason_code": "browser_unattachable",
                "phase": phase.value,
                "evidence": evidence,
                "error": str(exc),
                "backoffs": state.rate_limit_backoffs,
                "restart_already_spent": self._rate_limit_browser_restarted,
            },
        )
        self._drop_client()
        if self._rate_limit_browser_restarted:
            # This episode's one restart is gone and the browser is STILL
            # unattachable. Restarting again is the thrash the cooldown exists
            # to prevent — and with `restart_cooldown_seconds = 0` the cooldown
            # would not prevent it. Park instead, which is the answer a second
            # failed recovery has earned.
            outcome = RESTART_SKIPPED_ALREADY_SPENT
        else:
            self._rate_limit_browser_restarted = True
            outcome = self._browser_restart_outcome()
        if outcome == RESTART_OK:
            targets = self._attachable_page_targets()
            if targets != 0:
                # None (unmeasurable) counts as recovered: a restart that
                # reported success and an endpoint this cannot measure are not
                # evidence the browser is still dead, and the next step is the
                # honest re-probe. The phase is untouched, so that step is the
                # one the loop was already in.
                self._log(
                    "browser_reattached",
                    data={
                        "reason_code": "browser_unattachable",
                        "phase": phase.value,
                        "page_targets": targets,
                    },
                )
                return
        # The session ends here, so a candidate this task had out for review
        # dies with it — same rule as the throttle park below.
        self._note_round_fault("browser_unattachable")
        restart_note = {
            RESTART_OK: "The browser was restarted and STILL lists no attachable page.",
            RESTART_FAILED: "The configured browser.restart_command ran and failed.",
            RESTART_SKIPPED_COOLDOWN: (
                "A restart was refused because browser.restart_cooldown_seconds "
                f"({self._config.browser.restart_cooldown_seconds:g}s) had not elapsed "
                "since the last one."
            ),
            RESTART_DISABLED: (
                "No browser.restart_command is configured, so nothing could be "
                "restarted automatically."
            ),
            RESTART_SKIPPED_ALREADY_SPENT: (
                "This throttle episode had already restarted the browser once "
                "and it came back with nothing to attach to, so it was not "
                "restarted again."
            ),
        }.get(outcome, "The browser restart did not complete.")
        self._to_needs_user(
            # The ACTION leads and the evidence follows, for the same reason
            # `describe_cdp_endpoint` is ordered that way: `autoloop start`
            # prints `blocker.question[:160]`, and a summary that spends all of
            # it on measurements cuts off the sentence saying what to do.
            "THE BROWSER, NOT A RATE LIMIT: nothing can attach to a page — open "
            "the Chrome profile's window, or run "
            "`python3 -m autoloop.browser.chrome_restart`, then resume with "
            f"`python -m autoloop run --retry`. {evidence}. {restart_note} "
            "ChatGPT's throttle overlay could not be the cause — there is no "
            "page for it to cover, and a limit the loop cannot even ask about "
            "is not a limit it should wait out. A closed WINDOW is the usual "
            "cause and the reason this went unnoticed: Chrome keeps running and "
            "/json/version keeps answering, so every check that only curls that "
            "endpoint reports a healthy browser. Last transport error: "
            f"{exc}",
            resume_phase=phase.value,
            kind="loop_fatal",
            code="browser_unattachable",
            detail=(
                f"phase={phase.value} restart={outcome} "
                f"cdp_url={self._config.browser.cdp_url} page_targets=0"
            ),
        )

    def _handle_rate_limited(self, phase: Phase, exc: RateLimitedError) -> None:
        """ChatGPT is throttling the account. Wait it out; do not fight it.

        Three things this must NOT do, each of them the reflex of every other
        browser-fault handler here:

        * **No restart.** The limit is account-level and server-side, so a
          fresh browser meets the same wall while adding another request. On
          2026-08-14/15 the loop restarted and retried from 07:56 onward,
          deepening the very condition it was failing on and reporting each
          round as `browser session lost`.
        * **No `consecutive_failures`.** Same principle the cooldown-skipped
          restart established (`_handle_browser_failure`): a failure nobody
          could have recovered from must not be charged to the budget that
          decides recovery is hopeless. These get
          `policy.max_rate_limit_backoffs` instead, so the exemption still
          ends somewhere an operator can see.
        * **No `_drop_client`.** Re-attaching navigates, and a navigation is
          another request. The page stays exactly where it is; the modal is
          dismissed in place and re-probed there.

        All three hold for a THROTTLE, which is what every line above is
        about, and none of them is relaxed by what follows.

        What follows is the other question, asked FIRST because those rules
        assume its answer: **is there a browser at all?** A `RateLimitedError`
        can also arrive with no attachable page behind it, and then none of
        the reasoning holds — nothing is being refused, so nothing is worth
        waiting out. `_classify_rate_limit_state` separates the two (and a
        third: a limit that has already cleared), and only
        `RL_BROWSER_UNATTACHABLE` reaches `_recover_unattachable_browser`,
        which restarts once and otherwise parks naming the BROWSER. It is
        classified before the counter moves, because a local recovery must not
        spend a budget that bounds waiting on the server.

        The wait itself is DURABLE, not just its counter: the deadline is
        persisted before the sleep and served by `_await_rate_limit_deadline`
        on whatever process is running when it expires. See that method for the
        restart-storm this closes.

        The phase is left untouched, so the loop re-enters the step it was in
        and republishes its heartbeat. **That re-entry IS the re-probe**, and
        the streak is reset only when it completes (`run`'s `else` branch).
        Nothing here may reset it: after the sleep the overlay is dismissed,
        and a dismissed overlay is gone because the loop closed it, not
        because the server-side limit expired. Reading that as "cleared" made
        every occurrence reset the count, so the delay never doubled, the
        budget never accumulated and the park was unreachable — a 60-second
        retry loop wearing the shape of a back-off.
        """
        state = self.state
        # Before the counter moves, so a browser fault never spends a budget
        # that exists to bound waiting on the SERVER (brw-03's rule).
        classification, evidence = self._classify_rate_limit_state()
        if classification == RL_BROWSER_UNATTACHABLE:
            self._recover_unattachable_browser(phase, exc, evidence)
            return
        # Everything that reaches here is classified as something other than
        # unattachable, so this episode's spent restart is settled and the next
        # unattachable browser is a new fault with its own recovery. That
        # INCLUDES the default `RL_THROTTLED` reached when nothing could be
        # probed — deliberately, and it is the safe direction: a browser that
        # genuinely cannot be reached raises `BrowserError` from the next step
        # and gets the restart path built for it on its own budget, whereas
        # holding the guard on unprobed evidence would refuse a real
        # zero-target fault hours later the recovery it is owed.
        self._rate_limit_browser_restarted = False
        state.rate_limit_backoffs += 1
        verdict = self._policy.check_rate_limit_backoff_budget(state.rate_limit_backoffs)
        delay = self._rate_limit_delay(state.rate_limit_backoffs)
        self._log(
            "rate_limited",
            data={
                "phase": phase.value,
                "error": str(exc),
                "reason_code": "rate_limited",
                "stage": getattr(exc, "stage", ""),
                "backoffs": state.rate_limit_backoffs,
                "backoff_seconds": delay,
                "waited_seconds": state.rate_limit_wait_seconds,
                # What the browser looked like when this was classified, so a
                # reader of the transcript can tell a modal that was actually
                # seen from a default reached because nothing could be asked.
                "classification": classification,
                "evidence": evidence,
            },
        )
        if not verdict.allowed:
            waited = state.rate_limit_wait_seconds
            # The session ends here. If a task had a candidate out for review,
            # that review is lost with it — see `_note_round_fault`, and the
            # third bullet above for why the same principle already applies to
            # `consecutive_failures`. This extends it to the one budget still
            # charged for faults.
            self._note_round_fault("provider_rate_limited")
            # The park may assert "not a browser fault" ONLY when the modal was
            # actually sighted. `RL_THROTTLED` is also the DEFAULT — the answer
            # for a page that could not be probed and an endpoint that could not
            # be measured — and telling an operator the composer was present the
            # whole time on that evidence is the 2026-08-17 failure in a
            # narrower case: four hours spent waiting out a limit nobody had
            # confirmed. Both branches keep the wait, the remedy and the word
            # "restart"; they differ in what they claim to know.
            sighted = evidence == MODAL_SIGHTED
            if sighted:
                verdict_text = (
                    "This is NOT a browser fault — the composer is present the "
                    "whole time and a restart cannot help, because the limit is "
                    "server-side."
                )
            else:
                verdict_text = (
                    "CHECK THE BROWSER BEFORE WAITING: the throttle modal was "
                    "never actually sighted during these back-offs, so a limit "
                    "that had already lifted — or a browser with no page to be "
                    "throttled at all — looks exactly like this. "
                    "`curl http://127.0.0.1:9222/json/list` (NOT /json/version, "
                    "which answers even for a browser whose window is closed): "
                    "no page target means this was never a rate limit, and a "
                    "restart IS the remedy."
                )
            self._to_needs_user(
                f"{verdict.reason}: ChatGPT has rate limited this account "
                f"('Too many requests — please wait a few minutes before trying "
                f"again') and it has not lifted across {state.rate_limit_backoffs} "
                f"throttled attempts and {waited:g}s of completed waiting. "
                f"{verdict_text} Leave the account idle for a "
                "while (an hour is usually more than enough), then resume with "
                "`python -m autoloop run --retry`. If it keeps happening, raise "
                "browser.rate_limit_backoff_seconds so the loop waits longer "
                f"before re-probing. Last error: {exc}. What the browser looked "
                # The sentence that stops this park from asserting more than
                # was measured, and the input to the branch above.
                f"like when this was classified: {evidence}",
                resume_phase=phase.value,
                kind="loop_fatal",
                code="rate_limited",
                detail=(
                    f"phase={phase.value} backoffs={state.rate_limit_backoffs} "
                    f"waited_seconds={waited:g} next_backoff_seconds={delay:g} "
                    f"classification={classification}"
                ),
            )
            return
        if classification == RL_CLEARED:
            # State 2: the overlay is gone AND a real click on the composer
            # landed, so there is nothing left to outlast — waiting would be a
            # delay against a limit that has already lifted.
            #
            # The counter is still spent, deliberately. It is the only thing
            # bounding a page that keeps clearing between the raise and this
            # check, and without it a transport raising `RateLimitedError`
            # every step would be answered by an immediate retry every step —
            # the hammering the back-off exists to stop, arriving through the
            # one door that skips the sleep. Escalation is preserved: the next
            # occurrence gets the delay its streak prescribes.
            self._log(
                "rate_limit_cleared",
                data={
                    "reason_code": "rate_limited",
                    "phase": phase.value,
                    "backoffs": state.rate_limit_backoffs,
                    "evidence": evidence,
                },
            )
            self._store.save(state)
            return
        # Persisted BEFORE the sleep — the count AND the instant the wait runs
        # to. A crash during the wait must resume knowing both, or the next
        # process starts from zero with no wait outstanding and is ready to
        # hammer again. The elapsed total is deliberately NOT credited here:
        # crediting a wait that has not happened yet is how a killed process
        # comes back believing it already waited.
        state.rate_limit_retry_not_before = (
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat(timespec="milliseconds")
        self._store.save(state)
        # `delay` itself, not the clock difference: this process opened the
        # window a moment ago and owes the whole of it. Serving the remainder
        # against the deadline is the RESUMED path's job.
        self._serve_rate_limit_wait(delay, delay)

    def _handle_quota_exhausted(self, phase: Phase, exc: QuotaExhaustedError) -> None:
        """Hand the reviewer role to the fallback provider, or park saying why.

        The handover is safe here for a reason worth stating: the request has
        no captured reply, so nothing was authorized under the exhausted
        provider, and the fallback is a transport that has never seen this
        request. It is *recorded* for a different reason — the reviewer grants
        authority, so an approval must stay attributable to the transport that
        produced it. A silent swap would leave a `reviewed` stamp with no
        answer to "reviewed by whom".
        """
        state = self.state
        from_provider = self.active_provider()
        self._log(
            "quota_exhausted",
            data={
                "phase": phase.value,
                "provider": from_provider,
                "error": str(exc),
                "reason_code": "quota_exhausted",
            },
        )
        self._drop_client()
        req = state.pending_request
        fallback = self._config.conversation.fallback_provider

        def park(code: str, extra: str) -> None:
            # Every exit from this handler that parks ends the session, so a
            # review a task had already earned dies with it. Same rule as the
            # rate-limit handler; `_note_round_fault` is a no-op unless a
            # candidate was genuinely out for review.
            self._note_round_fault("provider_quota_exhausted")
            self._to_needs_user(
                f"{exc} {extra}",
                resume_phase=phase.value,
                kind="loop_fatal",
                code=code,
            )

        if not fallback or fallback == from_provider:
            park(
                "quota_exhausted",
                "No usable conversation.fallback_provider is configured, so the "
                "loop cannot hand over. Wait for the allowance window to reset, "
                "buy credits, or set conversation.fallback_provider (the browser "
                "provider draws on a separate quota).",
            )
            return
        if req is None:
            # Nothing in flight to re-issue. Switching would still be safe, but
            # it would be a change nobody can attribute to a request, so leave
            # the decision with the operator.
            park(
                "quota_exhausted",
                "No request was in flight, so there is nothing to hand over. "
                f"Set conversation.provider to '{fallback}' and retry.",
            )
            return
        if state.last_response is not None:
            # Belt to the phase machine's braces: quota can only bite while a
            # request is unanswered. A captured reply here would mean the
            # handover was about to straddle an answered turn, which is the one
            # shape that could put two reviewers inside one round.
            park(
                "quota_exhausted",
                "A captured reply is still pending execution, so the reviewer "
                "cannot be handed over mid-turn. Resolve the pending response "
                "first.",
            )
            return
        verdict = self._policy.check_provider_switch_budget(state.provider_switches)
        if not verdict.allowed:
            park(
                "provider_switch_budget",
                f"{verdict.reason}. Both configured providers have now reported "
                "an exhausted allowance in this run — waiting for a reset is the "
                "only remedy that does not cost money.",
            )
            return

        record = ProviderSwitch(
            from_provider=from_provider,
            to_provider=fallback,
            request_id=req.request_id,
            reason="quota_exhausted",
        )
        state.active_provider = fallback
        state.provider_switches += 1
        state.last_provider_switch = asdict(record)
        req.provider = fallback
        # The fallback is a DIFFERENT transport that has never seen this
        # request, so the transport-level marks left by the exhausted one
        # describe nothing here. Clearing them is what lets `submitting`
        # re-issue instead of parking on `submission_ambiguous` — and it is
        # only sound because those marks are per-transport facts, not claims
        # about the request itself.
        req.send_attempted = False
        req.last_send_outcome = ""
        req.resends_used = 0
        req.submitted = False
        req.conversation_url = state.conversation_url
        req.conversation_epoch = state.conversation_epoch
        state.phase = Phase.SUBMITTING.value
        self._log(
            "provider_switched",
            request_id=req.request_id,
            data=asdict(record) | {"provider_switches": state.provider_switches},
        )
        self._store.save(state)

    def _handle_conversation_unusable(
        self, phase: Phase, exc: ConversationUnusableError
    ) -> None:
        """The configured conversation loaded and is broken.

        Distinct from `_handle_browser_failure`: that one counts consecutive
        failures and retries the same phase with a fresh client, which for a
        wedged chat just re-runs the same failure until the budget is spent.
        Accounting is deliberately NOT shared — a rotation attempt does not
        also increment `consecutive_failures`, or one fault would be charged to
        two budgets and the loop would fail earlier than either one describes.
        For the same reason this handler never touches the browser restart
        machinery: both shapes of the error (an attach that found no composer,
        and a submission this loop made that provably never appeared — see
        `ConversationUnusableError.code`) were established THROUGH a working,
        un-throttled page, so a restart could not possibly help and would only
        spend recovery on a fault it cannot fix. The 2026-08-17 incident is
        the second shape misrouted: a missing submission surfaced as a locator
        timeout, read as a lost session, and bought ten minutes of 45-second
        Chrome restarts against a chat only rotation could fix.

        `reason` carries the error's own code into the transcript and the
        `RotationRecord`, so a rotation forced by a vanished submission stays
        distinguishable from one forced by a chat that would not load.
        """
        state = self.state
        reason = getattr(exc, "code", "") or "conversation_unusable"
        self._log(
            "conversation_unusable",
            data={"phase": phase.value, "error": str(exc), "reason_code": reason},
        )
        self._drop_client()
        req = state.pending_request
        if req is None:
            # Nothing in flight to rebind; treat as an ordinary browser fault
            # so the failure budget still governs a dead conversation at rest.
            self._handle_browser_failure(phase, exc)
            return
        # `resume_phase` is not set here: every park below routes through
        # `_park_rotation` -> `_to_needs_user`, which sets it from the live
        # phase. Assigning it here too would just be overwritten.
        self._attempt_rotation(req, reason=reason)

    def _handle_response_start_timeout(
        self, phase: Phase, exc: ResponseTimeoutError
    ) -> None:
        """A confirmed, persisted send whose assistant turn never starts —
        the "silent conversation" fault distinct from every other rotation
        trigger, where a send was disproven or the chat itself was broken.
        Here the send is known good; the model simply never begins.

        Every occurrence still goes through `_handle_browser_failure` FIRST,
        exactly like `_handle_conversation_unusable` layers rotation ON TOP
        of (never instead of) the ordinary failure budget for a wedged chat.
        Only on the THIRD consecutive `stage="start"` timeout for the SAME
        request — with that budget still allowing a retry — does this
        attempt the additional proof rotation requires (see
        `_attempt_silence_rotation`). A `stage="complete"` timeout (a
        response that already started and is merely slow or stalled) is
        never a candidate and is routed through unconditionally.

        "No resubmission happened between these windows" holds by
        construction, not by an extra guard here: `awaiting` has no
        transition back to `submitting` except through a completed
        rotation, which resets `start_timeouts` to 0 — so a nonzero count
        can only describe consecutive timeouts in one conversation, for one
        submission, with nothing resent in between.
        """
        if phase is not Phase.AWAITING or exc.stage != "start":
            self._handle_browser_failure(phase, exc)
            return

        state = self.state
        req = state.pending_request
        if req is None:  # pragma: no cover - defensive; awaiting always has one
            self._handle_browser_failure(phase, exc)
            return

        req.start_timeouts += 1
        req.start_timeout_wait_seconds += (
            exc.elapsed
            if exc.elapsed is not None
            else self._config.browser.response_start_timeout_seconds
        )
        # Persists the incremented, still-ordinary-failure counters durably
        # (via `_handle_browser_failure`'s own save) before anything below
        # risks a further network call — a crash here must resume with the
        # timeout count intact, not lose it back to 0.
        self._handle_browser_failure(phase, exc)
        if state.phase != Phase.AWAITING.value or req.start_timeouts < 3:
            # Either the ordinary budget already parked/failed the loop, or
            # this is only the first or second such timeout — an ordinary
            # retry, not yet a rotation candidate.
            return

        floor = 3 * self._config.browser.response_start_timeout_seconds
        if req.start_timeout_wait_seconds < floor:
            # Computed from the CURRENT config, not a literal 360 — and
            # deliberately NOT an assertion. `elapsed` on each of the three
            # timeouts was measured against whatever
            # `response_start_timeout_seconds` was configured AT THE TIME;
            # an operator raising that value between processes (this trigger
            # is exactly the kind of fault a restart can land in the middle
            # of) can leave a true, honestly-measured accumulated wait below
            # a floor computed from the NEW value. That is insufficient
            # evidence, not corruption — the correct response is to keep
            # retrying ordinarily, never to crash the loop over it.
            self._log(
                "response_silence_wait_below_floor",
                request_id=req.request_id,
                data={
                    "reason_code": "response_silence_wait_below_floor",
                    "start_timeouts": req.start_timeouts,
                    "accumulated_wait_seconds": req.start_timeout_wait_seconds,
                    "floor_seconds": floor,
                },
            )
            return
        self._attempt_silence_rotation(req)

    # ---- conversation rotation ---------------------------------------------

    def _attempt_rotation(self, req: PendingRequest, reason: str) -> None:
        """Move the in-flight request to a fresh chat in the same project.

        Parks — never proceeds — unless every precondition holds. The sequence
        is deliberately "prove, then bind": the new chat's URL is only written
        anywhere after the request has been reconciled against it, because the
        address bar is exactly the kind of evidence this changeset exists to
        stop trusting.
        """
        state = self.state
        project_url = self._config.browser.project_url
        if not project_url:
            self._park_rotation(
                req,
                "rotation_unavailable",
                "the conversation cannot be used and no browser.project_url is "
                "configured, so autoloop cannot open a replacement chat. `run "
                "--retry` alone will not clear this. Set browser.project_url to "
                "the ChatGPT project this conversation belongs to and then retry, "
                "or move the loop to a healthy conversation by hand.",
            )
            return
        verdict = self._policy.check_rotation_budget(state.rotations)
        if not verdict.allowed:
            self._park_rotation(
                req,
                "rotation_cap_reached",
                f"this run could not reach the conversation and {verdict.reason}. "
                "The budget is per RUN: starting a new run (`run --retry`, or "
                "`run --continuous` again) begins with a fresh one, so if the "
                "cause was transport — a dropped network, a browser that died "
                "mid-navigation — fix that and start a new run. A spent rotation "
                "is NOT evidence the chat itself is broken; open the conversation "
                "by hand before concluding it is. Raise "
                "policy.max_conversation_rotations only to allow more rotations "
                "WITHIN one run, which is rarely the actual problem. If a new run "
                "reaches the chat but no reply ever starts, check whether the "
                "request was ever posted — the loop resumes into `awaiting` and "
                "will wait for a response to a message that never landed.",
            )
            return

        if req.delivery is not None:
            # A chunked packet's parts live in the conversation being ABANDONED.
            # A rotation carries only the verdict message, which would name part
            # ids the replacement chat does not contain — the reviewer would be
            # asked to decide on a patch that is not there. Re-sending the parts
            # is not an option either: the rotation posts the verdict message
            # itself, so they would arrive after the question. Fall back to the
            # omission notice first — rule 2 applied to a different failure, and
            # the reviewer is told plainly what it cannot see.
            #
            # Deliberately AFTER both preconditions: a rotation refused for a
            # missing `project_url` or a spent budget sends nothing, so the old
            # conversation still holds the whole delivery and there is nothing
            # to give up.
            self._fall_back_to_omission(req, reason_code="rotation_leaves_parts_behind")
        old_url = req.conversation_url or state.conversation_url
        # Consume the budget BEFORE the attempt, durably. A rotation SENDS a
        # message; if the process dies between that send and the binding below,
        # recovery must not be able to open a second chat and post again. Same
        # pessimism as `send_attempted`, for the same reason — and it is why a
        # failed attempt still costs a rotation.
        state.rotations += 1
        self._store.save(state)
        try:
            new_url, sent_prompt = self._rotate_conversation(req, project_url)
        except LoginExpiredError:
            raise
        except RateLimitedError as exc:
            # Caught ahead of the generic clause so the park says THROTTLE
            # rather than "opening a replacement chat failed", which would
            # send the operator looking at the conversation. Parked rather
            # than backed off: the rotation budget is already spent and the
            # attempt may have posted, so this is not a step that can simply
            # be re-entered after a wait. Not re-raised either — this method
            # is reached from inside `run()`'s own except clauses, where a
            # raise would leave the try entirely and end the process.
            self._log(
                "rate_limited",
                request_id=req.request_id,
                data={"reason_code": "rate_limited", "context": "rotation",
                      "error": str(exc)},
            )
            self._drop_client()
            self._park_rotation(
                req,
                "rate_limited",
                "ChatGPT rate limited this account while a replacement chat "
                f"was being opened: {exc}. The rotation attempt is spent — it "
                "may have posted before the throttle bit, so autoloop will not "
                "try again on its own. Leave the account idle for a while, "
                "check whether the replacement chat exists, then resume.",
            )
            return
        except (BrowserError, AutoloopError) as exc:
            self._log(
                "rotation_failed",
                request_id=req.request_id,
                data={"reason_code": "rotation_failed", "error": str(exc)},
            )
            self._drop_client()
            self._park_rotation(
                req,
                "rotation_failed",
                f"the conversation is unusable and opening a replacement chat "
                f"failed: {exc}. The rotation attempt is spent — it may have "
                "posted before failing, so autoloop will not try again on its own. "
                f"State still points at the RETIRED conversation ({old_url or 'unknown'}) "
                "with this request marked submitted against it, so a plain restart "
                "resumes there — which is the conversation that was just found "
                "unusable. If the message above names an address, open it: when a "
                "replacement chat exists and holds this request, move the loop to it "
                "(point browser.conversation_url at that chat, then `reset`, since "
                "the drift guard requires state and config to agree) rather than "
                "resending — a resend into a chat that already has the request posts "
                "it twice.",
            )
            return

        # Only now is the request's prompt the one that was actually sent. Doing
        # this before the send would leave a failed rotation holding a prompt
        # that announces the conversation is abandoned, in the conversation that
        # was never abandoned — which is exactly where `--resubmit` would send it.
        req.prompt = sent_prompt
        req.prompt_sha256 = hashlib.sha256(sent_prompt.encode("utf-8")).hexdigest()
        record = RotationRecord(
            old_url=old_url,
            new_url=new_url,
            request_id=req.request_id,
            reason=reason,
            epoch=state.conversation_epoch + 1,
        )
        state.conversation_epoch = record.epoch
        state.conversation_url = new_url
        state.last_rotation = asdict(record)
        req.conversation_url = new_url
        req.conversation_epoch = record.epoch
        req.submitted = True
        req.send_attempted = True
        req.last_send_outcome = SendOutcome.ACCEPTED.value
        # Fresh conversation, fresh silence clock: whatever `start_timeouts`
        # counted described the RETIRED conversation, which this request no
        # longer belongs to. Carrying it forward would let a future timeout
        # in the new chat inherit a count it did not earn.
        req.start_timeouts = 0
        req.start_timeout_wait_seconds = 0.0
        # A completed rotation is itself a successful transport action — a
        # send AND a reconciliation both just succeeded against the new
        # conversation — exactly the evidence `_step_awaiting`'s own success
        # path resets this counter on. Not resetting it here would leave the
        # replacement chat starting from whatever count the RETIRED one had
        # accrued, which for the silent-conversation trigger specifically
        # means a single further timeout in the brand-new chat could exceed
        # `max_consecutive_failures` and fail the loop before the rotation
        # cap even gets a chance to refuse a second rotation. Cannot loop:
        # `max_conversation_rotations` bounds rotations, not this reset.
        state.consecutive_failures = 0
        state.phase = Phase.AWAITING.value
        state.resume_phase = None
        self._log(
            "conversation_rotated",
            request_id=req.request_id,
            data=asdict(record) | {"rotations": state.rotations},
        )
        self._store.save(state)
        self._heal_config_url(new_url)

    def _attempt_silence_rotation(self, req: PendingRequest) -> None:
        """The third consecutive response-START timeout for `req`, with the
        ordinary failure budget still allowing another try. One thing
        remains before rotation may fire: proof, not assumption, that the
        conversation is STILL silent right now — a reply could have landed
        in the gap between the third timeout and this check, or across a
        restart.

        This is the ONLY extra step the "silent conversation" trigger needs:
        past this point it reuses `_attempt_rotation` for everything else
        (the project_url precondition, the rotation budget,
        `_rotate_conversation`, and the persisted `RotationRecord`) exactly
        like `_handle_conversation_unusable` and `_step_submission_rejected`
        do — their own disproof already came from the transport itself, so
        neither needs this call.

        `LoginExpiredError` is parked HERE, not re-raised: this method is
        itself called from inside `run()`'s `except ResponseTimeoutError`
        handler, so a raise here would leave the try/except entirely (a
        sibling `except LoginExpiredError` on the SAME try never catches an
        exception raised from within another branch of it) and crash the
        process instead of parking. `_attempt_rotation`'s own re-raise is
        safe only for its OTHER caller, `_step_submission_rejected`, which
        runs inside that same try — never rotate for login expiry, but never
        let discovering that take the whole loop down either.
        """
        try:
            client = self._client_for_request(req)
            client.attach()
            still_silent = self._reconcile_no_response(client, req.request_id)
        except LoginExpiredError as exc:
            self._log("login_expired", data={"error": str(exc), "context": "silence_check"})
            self._drop_client()
            self._to_needs_user(
                str(exc),
                resume_phase=Phase.AWAITING.value,
                kind="loop_fatal",
                code="login_expired",
            )
            return
        except (BrowserError, AutoloopError) as exc:
            # The check itself could not complete. This is NOT evidence the
            # conversation is silent — it is an ordinary transport hiccup on
            # the confirmation step, so it changes nothing further: the
            # ordinary failure budget already decided (in
            # `_handle_response_start_timeout`) that the loop retries
            # `awaiting` with a fresh client, and that decision is left
            # standing.
            self._log(
                "response_silence_check_failed",
                request_id=req.request_id,
                data={"reason_code": "response_silence_check_failed", "error": str(exc)},
            )
            self._drop_client()
            return
        if not still_silent:
            # A reply appeared between the third timeout and this reload.
            # The conversation was never broken — just slow — so the streak
            # is stale and must not survive to threaten a future timeout.
            self._log(
                "response_silence_check_cancelled",
                request_id=req.request_id,
                data={
                    "reason_code": "response_started_during_reconciliation",
                    "start_timeouts": req.start_timeouts,
                },
            )
            req.start_timeouts = 0
            req.start_timeout_wait_seconds = 0.0
            self._store.save(self.state)
            return
        self._log(
            "response_silence_confirmed",
            request_id=req.request_id,
            data={
                "reason_code": "response_start_silence",
                "start_timeouts": req.start_timeouts,
                "start_timeout_wait_seconds": req.start_timeout_wait_seconds,
            },
        )
        self._attempt_rotation(req, reason="response_start_silence")

    @staticmethod
    def _reconcile_no_response(client, request_id: str) -> bool:
        """Probe the optional final-silence-check capability the same way
        `_client_for_request` probes `retarget`/`current_url`: a provider
        without it (every non-Playwright adapter today) cannot PROVE the
        conversation silent by reconciliation — only the live polling
        `await_response` already did — so this fails closed. No capability,
        no rotation on this trigger.
        """
        check = getattr(client, "reconcile_no_response", None)
        if check is None:
            return False
        return check(request_id)

    def _rotate_conversation(
        self, req: PendingRequest, project_url: str
    ) -> tuple[str, str]:
        """Open one new chat in the project and land `req` in it.

        Returns `(new_conversation_url, prompt_actually_sent)` — both already
        verified. Mutates nothing on `req`: a rotation that fails partway must
        leave the request exactly as it was, still bound to the old
        conversation, so the caller commits the new prompt only on success.

        ChatGPT does not mint a chat's durable address until it has its first
        turn, so the order is forced: retarget to the project page, submit
        there — that submit IS the priming message, sent exactly once — and
        only then read the address the server assigned. Every step after the
        submit is verification.

        The address it shows in the meantime is not the project page: a chat
        opened from a project posts under a PLACEHOLDER
        (`https://chatgpt.com/c/WEB:<uuid>`) that is not under the project
        prefix at all. So "the address moved off the project page" was never
        evidence the chat exists, and judging membership on it refused every
        rotation (2026-08-16). The wait below is what primes the chat; the
        membership rule itself is unchanged and still refuses a replacement
        that really is outside the project.
        """
        client = self._get_client()
        retarget = getattr(client, "retarget", None)
        current_url = getattr(client, "current_url", None)
        if retarget is None or current_url is None:
            raise BrowserError(
                "the configured conversation provider does not support rotation "
                "(no retarget/current_url); rotate by hand"
            )
        retarget(project_url)
        client.attach()

        prompt = self._continuation_prompt(req)
        # ONE send, never retried. This submit is issued from the PROJECT PAGE,
        # where every send opens a NEW chat — so a second attempt would not
        # retry anything, it would create a second conversation and orphan the
        # first, with the request live in both. Anything short of acceptance
        # therefore raises, and the caller parks; the rotation budget was
        # already spent before this method was entered, for the same reason.
        result = client.submit(req.request_id, prompt)
        if result not in (SubmitResult.CONFIRMED, SubmitResult.ALREADY_PERSISTED):
            raise BrowserError(
                f"the replacement chat did not accept the request ({result.value})"
            )

        # Wait for the address to become a project conversation — the question
        # the membership check actually asks — rather than merely to differ
        # from the project page.
        #
        # The old condition stopped at the first change, and the first change
        # is the placeholder `/c/WEB:<uuid>` a chat carries until its first
        # message lands. That address is under no project, so the check refused
        # it every time and the loop parked LOOP-FATAL on a rotation that had
        # in fact worked (2026-08-16): an operator sent one short message by
        # hand and the same address became `/g/g-p-<project>-<slug>/c/<uuid>`,
        # which passes this check unchanged. Priming, not the rule, was
        # missing.
        #
        # Bounded, and deliberately blind to the placeholder's shape: an
        # address that is not in the project yet is waited through whatever it
        # looks like, and an address that never gets there refuses below with
        # the address actually observed.
        deadline = time.monotonic() + ROTATION_URL_TIMEOUT_SECONDS
        new_url = current_url()
        while not self._url_in_project(new_url, project_url) and time.monotonic() < deadline:
            time.sleep(ROTATION_URL_POLL_SECONDS)
            new_url = current_url()
        if not self._url_in_project(new_url, project_url):
            # The address bar is a poor witness for a chat that was just
            # created: ChatGPT mints `/c/<id>` some time after accepting the
            # first message, and on a slow account that outlasts any polling
            # window worth having. So ask the CONTENT instead — the request id
            # is in the message and identifies the chat without the URL.
            #
            # This is not a nicety. Three rotations failed on the timeout while
            # the chat existed and held the request, each leaving an orphan
            # nobody read, and each reporting "the chat id was never assigned"
            # about a chat that plainly had one (2026-08-03).
            by_content = getattr(client, "find_conversation_with", None)
            found = None
            searched = False
            if by_content is not None:
                try:
                    found = by_content(req.request_id, project_url)
                    searched = True
                except (BrowserError, AutoloopError):
                    found = None
            # `found` is NOT re-checked against `_url_in_project`, deliberately.
            # The search reads the PROJECT'S OWN chat list, so its scoping is
            # the membership check — and it builds candidates with `urljoin`
            # against the project page, which yields a prefix-less
            # `https://chatgpt.com/c/<id>` for a chat that is inside the project
            # (same trap `_same_conversation` documents). Re-applying the
            # address-bar rule here would refuse every by-content rescue in
            # production while passing every test that types URLs by hand,
            # undoing the 2026-08-03 fix. The rule this changeset defends is the
            # one on the address bar above, which is where the refusal belongs.
            if found:
                self._log(
                    "rotation_found_by_content",
                    request_id=req.request_id,
                    data={"url": found, "note": "address bar had not caught up"},
                )
                new_url = found
            else:
                # Name the address ACTUALLY OBSERVED. A generic "timed out"
                # sends the next operator hunting a browser fault; the
                # placeholder shape says plainly that the chat was never
                # primed, and a foreign `/c/<id>` says plainly that it opened
                # somewhere else.
                if new_url.rstrip("/") == project_url.rstrip("/"):
                    detail = "it is still the project page"
                elif _is_placeholder_conversation(new_url):
                    # Says what was SEEN and nothing more. "so the chat never
                    # took the message" would be an absence claim nothing here
                    # established — the 2026-08-03 failure was a chat that held
                    # the request while its address lagged — and a park that
                    # implies absence steers an operator to `--resubmit`.
                    # A fragment, like the other two branches, so the clause
                    # appended below joins it as one sentence instead of running
                    # two together across a full stop.
                    detail = (
                        f"the address never became a project conversation — it was "
                        f"still the pre-persistence placeholder {new_url!r} when the "
                        f"wait expired (a chat opened from a project shows one until "
                        f"its first message lands)"
                    )
                else:
                    detail = f"{new_url!r} is not under {project_url!r}"
                if searched:
                    # Only claimable because the search RAN and came back empty.
                    # A provider without the capability, or a search that raised,
                    # read no history at all, and a park that says "no chat
                    # carries this" on the strength of a read nobody performed is
                    # manufactured evidence.
                    detail += " and no chat in the project carries this request"
                raise BrowserError(
                    f"the replacement chat is not inside the configured project: {detail}"
                )
        retarget(new_url)
        # The address bar said the send landed here; make the conversation say
        # it. Until this returns True the rotation has not happened and nothing
        # is bound to the new URL.
        if not client.reconcile(req.request_id):
            raise BrowserError(
                "the replacement chat does not contain the request after "
                "reconciliation — refusing to bind to it"
            )
        return new_url, prompt

    @staticmethod
    def _url_in_project(candidate: str, project_url: str) -> bool:
        """True when `candidate` is a conversation under `project_url`.

        Compares the project path prefix, so a chat that opened outside the
        project (a stray navigation, a redirect to the plain composer) is
        refused rather than silently adopted.
        """
        if not candidate:
            return False
        want, have = urlsplit(project_url), urlsplit(candidate)
        if want.netloc != have.netloc:
            return False
        project_path = want.path.rstrip("/")
        # ".../project" is the project landing page; conversations live at
        # ".../c/<id>" under the same /g/<...> prefix.
        base = project_path.rsplit("/", 1)[0] if project_path.endswith("/project") else project_path

        # Compare SEGMENTS, and allow the last one to carry a slug suffix.
        # ChatGPT writes the project landing page as `/g/g-p-<id>/project` but
        # its conversations as `/g/g-p-<id>-<slugified-project-name>/c/<id>`,
        # so a plain `startswith(base + "/c/")` rejects a chat that really is
        # inside the project. On 2026-08-03 a rotation created a chat, posted
        # the request into it, and then refused its own successful result on
        # this check — and the SAME check rejected the conversation the loop
        # had been using all day, which is what proves it was never
        # discriminating good from bad.
        #
        # The suffix must be `-<something>`: `g-p-abc` may match `g-p-abc-x`
        # but never `g-p-abcdef`, the segment-boundary trap that `approved_
        # paths` prefixes hit too.
        want_segs = [seg for seg in base.split("/") if seg]
        have_segs = [seg for seg in have.path.split("/") if seg]
        if len(have_segs) < len(want_segs) + 2:
            return False
        for index, wanted in enumerate(want_segs):
            got = have_segs[index]
            last = index == len(want_segs) - 1
            if got != wanted and not (last and got.startswith(wanted + "-")):
                return False
        return have_segs[len(want_segs)] == "c" and bool(have_segs[len(want_segs) + 1])

    def _continuation_prompt(self, req: PendingRequest) -> str:
        """The same request, plus one line saying the transport moved.

        The payload is already self-contained (`prompts.build_prompt` re-sends
        the CONTEXT block and the full contract every turn), so the new chat is
        contract-complete without replaying any history. The note exists so the
        reviewer knows which conversation is authoritative — not to reconstruct
        what the abandoned one said.
        """
        return req.prompt.rstrip("\n") + "\n\n" + CONTINUATION_NOTE

    def _heal_config_url(self, new_url: str) -> None:
        """Point the config file at the new conversation.

        Best-effort by design: the rotation itself is already committed to
        state, and state is what this run follows. A failure here only means
        the NEXT session would start from the old URL, which the CLI's
        drift guard detects and reports — a worse outcome than a healed config,
        but far better than unwinding a rotation that already succeeded.
        """
        try:
            update_conversation_url(self._config_path, new_url, Path.cwd())
        except (AutoloopError, OSError) as exc:
            self._log(
                "config_heal_failed",
                data={"reason_code": "config_heal_failed", "error": str(exc)},
            )

    def _park_rotation(self, req: PendingRequest, reason_code: str, question: str) -> None:
        self._log(
            "rotation_declined",
            request_id=req.request_id,
            data={"reason_code": reason_code, "rotations": self.state.rotations},
        )
        # Every rotation refusal is loop_fatal: the conversation channel itself
        # is unusable or exhausted, which no other task can route around. The
        # fail-closed default would already give loop_fatal — passing it
        # explicitly, with the caller's own reason_code, is what makes the
        # persisted blocker say WHICH refusal it was instead of "unclassified".
        self._to_needs_user(
            question,
            resume_phase=self.state.phase,
            kind="loop_fatal",
            code=reason_code,
        )

    def _current_pending_postcommit(
        self, payload: str, report_sha256: str
    ) -> PostcommitBinding | None:
        """Bind `payload` to a produce-then-review candidate, but only if
        `payload` actually carries that candidate's identifiers as literal
        text — mirroring `carries_block`'s adoption-block check above. A
        corrective re-prompt or any other payload legitimately carries none
        of this, and must bind nothing: an approval answering THAT request
        must never be treated as if it reviewed a candidate it never showed.

        `state.task_execution` (not `last_manifest_id` — deliberately a
        separate field, see `state.py`) is refreshed by `_finish_postcommit`
        every time a round finishes, success or failure, so it always
        reflects the most recently produced candidate. Requiring all four
        identifiers (task_id, branch, base_sha, candidate_sha — the latter
        two 40-hex shas) as substrings makes an accidental match on ordinary
        prose effectively impossible without needing an exact-block compare.
        """
        task_exec = self.state.task_execution
        if not task_exec or not task_exec.get("candidate_sha"):
            return None
        task_id = task_exec.get("task_id", "")
        task_branch = task_exec.get("task_branch", "")
        base_sha = task_exec.get("task_base_sha", "")
        candidate_sha = task_exec.get("candidate_sha", "")
        if not all(
            value and value in payload for value in (task_id, task_branch, base_sha, candidate_sha)
        ):
            return None
        execution = self._execution_store.load(task_id)
        if execution is None or execution.candidate_sha != candidate_sha:
            return None
        worktree_git = GitGateway(Path(execution.worktree_path), self._policy)
        candidate_tree_sha = worktree_git.tree_of(candidate_sha)
        return PostcommitBinding(
            task_id=task_id,
            task_branch=task_branch,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            candidate_tree_sha=candidate_tree_sha,
            packet_sha256=report_sha256,
        )

    def _current_pending_changeset(
        self, payload: str, report_sha256: str
    ) -> ChangesetBinding | None:
        """Bind `payload` to an operator-authored changeset, exactly like
        `_current_pending_postcommit` above binds one to a produce-then-review
        candidate — see that method's docstring for the "why literal
        substrings" reasoning, which applies identically here.

        `state.changeset` (set by `python -m autoloop review-changeset`, see
        `changeset_review.build_changeset_binding`) is the ONLY source: there
        is no `TaskExecutionStore` to cross-check against, because there is
        no task and no separate worktree — the candidate lives directly in
        THIS checkout (`self._git`), which is exactly why `candidate_tree_sha`
        is re-derived from `self._git` rather than a worktree gateway.
        """
        raw = self.state.changeset
        if not raw or not raw.get("candidate_sha"):
            return None
        base_sha = raw.get("base_sha", "")
        candidate_sha = raw.get("candidate_sha", "")
        branch = raw.get("branch", "")
        dest_ref = raw.get("dest_ref", "")
        if not all(
            value and value in payload for value in (base_sha, candidate_sha, branch, dest_ref)
        ):
            return None
        candidate_tree_sha = self._git.tree_of(candidate_sha)
        return ChangesetBinding(
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            candidate_tree_sha=candidate_tree_sha,
            branch=branch,
            dest_ref=dest_ref,
            packet_sha256=report_sha256,
        )

    def _resolve_or_park_ambiguous(self, req: PendingRequest, reconciled: bool) -> None:
        """Last check before parking: is the request actually THERE?

        `reconcile()` reads the conversation's mounted window, and ChatGPT
        mounts a WINDOW of a chat rather than its history. On 2026-08-05
        `alr-af11e1b3-0006` parked here as `submission_ambiguous` while the
        conversation held the request AND its answer (`decision push`) — the
        turn was simply not painted. That park cost a human, and there was
        never anything ambiguous about it.

        So before parking, ask the by-content search, which mounts the tail
        and refuses to answer at all unless it demonstrably read the chat to
        its end (`BrowserChatGPT.find_conversation_with`).

        **The asymmetry is the design, and only one direction is automated:**

        * *The search PROVES the request present in this request's own
          conversation* → resolve and resume. Nothing is ambiguous, resuming
          sends nothing, and the risk is zero: it is the "it did persist"
          branch of `_step_submission_unconfirmed`, reached by better evidence.
        * *Anything else* → park exactly as before. Absence is the conclusion
          a flaky read gets wrong, and acting on it means a resend, which can
          duplicate a request the backend accepted. Proving presence lets the
          loop proceed; absence is never inferred and never acted on.

        A hit in a DIFFERENT chat parks too, and is not a contradiction: it
        proves the id exists somewhere, not that THIS send landed where the
        loop is listening. Rotation deliberately reuses the request id in the
        replacement chat, so a hit elsewhere can be a retired copy — and
        adopting a chat on that evidence is a rotation-grade rebinding
        (epoch, `state.conversation_url`, the config URL) taken on a duplicate
        id. The operator gets told which chat instead, which is the one fact
        that was missing. Nor is such a hit evidence of ABSENCE here: the
        search returns on its first sighting and stops walking.
        """
        found, note = self._search_for_request(req)
        if found is not None and self._same_conversation(found, req.conversation_url):
            state = self.state
            req.submitted = True
            state.phase = Phase.AWAITING.value
            self._log(
                "submission_confirmed_by_search",
                request_id=req.request_id,
                data={
                    "reason_code": "found_in_persisted_history_by_content",
                    "url": found,
                    "reconcile_attempts": req.reconcile_attempts,
                    "note": (
                        "reconciliation read a window that had not mounted the "
                        "turn; the request is present, so nothing was sent"
                    ),
                },
            )
            self._store.save(state)
            return
        self._park_ambiguous(req, reconciled, found=found, search_note=note)

    def _search_for_request(self, req: PendingRequest) -> tuple[str | None, str]:
        """The chat that PROVABLY carries `req`, per the by-content search, and
        a sentence saying what the search actually did.

        The URL is returned only on a positive sighting. Five outcomes — no
        `browser.project_url` to search, a provider without the capability
        (probed with `getattr`, like `retarget`/`current_url`), a search that
        refused to conclude (`ConversationSearchInconclusive`), a page that
        was wedged (`ConversationUnusableError`, caught for a reason of its
        own — see the `except`), or a genuine "in none of these chats" —
        return None, which leaves the caller parking. That collapse is safe in
        exactly one direction: None here never authorizes anything, it only
        declines to cancel a park.

        The note exists because those outcomes collapse into one value and must
        NOT collapse in what the operator is told. "The project was read and
        the request is in none of it" and "no search ran at all" point at
        completely different next actions, and a park that claimed the former
        while doing the latter would be manufacturing evidence.

        **A BROKEN BROWSER IS NOT ONE OF THOSE OUTCOMES, and is deliberately
        not caught here.** `SessionLostError`, `LoginExpiredError` and ordinary
        `BrowserError` each already have a route in `run()` — restart the
        browser and re-enter this phase, or park as `login_expired` with this
        phase as the resume point — and every one of those routes SENDS
        NOTHING, so letting them through costs nothing and keeps the recovery
        the loop was built with. Catching them would trade a recoverable
        transport fault for a `submission_ambiguous` park that names the wrong
        cause: a dead CDP connection says nothing whatever about whether the
        request is in the conversation, and reporting it as evidence
        uncertainty is the same misclassification this method exists to
        remove.

        The one browser fault that IS caught is `ConversationUnusableError`,
        and not because it says too little — because its route ACTS. The
        `except` below carries that reasoning.
        """
        project_url = self._config.browser.project_url
        if not project_url:
            self._log(
                "presence_search_skipped",
                request_id=req.request_id,
                data={"reason_code": "no_project_url"},
            )
            return None, (
                "No by-content search ran: browser.project_url is not configured, "
                "so autoloop has no chat list to read."
            )
        client = self._client_for_request(req)
        search = getattr(client, "find_conversation_with", None)
        if search is None:
            self._log(
                "presence_search_skipped",
                request_id=req.request_id,
                data={"reason_code": "provider_cannot_search"},
            )
            return None, (
                "No by-content search ran: this provider cannot search a project "
                "by message content."
            )
        try:
            found = search(req.request_id, project_url)
        except (ConversationSearchInconclusive, ConversationUnusableError) as exc:
            # Two refusals, one park, for two different reasons.
            #
            # `ConversationSearchInconclusive` is the search's own verdict on
            # its evidence: it read a page it could not vouch for, or never
            # proved it reached the end of a virtualized list. It is declining
            # to rule EITHER way, so it has said nothing about presence — and
            # "said nothing" must not read as "absent".
            #
            # `ConversationUnusableError` is caught for the opposite reason:
            # not because its normal route says too little, but because that
            # route ACTS. It is the one browser fault that authorizes a
            # rotation (see its docstring), and `_rotate_conversation` POSTS
            # the request id into the replacement chat — a send, from the one
            # phase whose entire contract is that only `--resubmit` repeats
            # one. The search walks the project page and up to `limit` OTHER
            # chats, so the wedged page here is usually not even this request's
            # conversation: letting a stranger's broken chat authorize a repost
            # of this request is exactly the duplicate `submission_ambiguous`
            # exists to prevent. A wedged page is also no evidence about
            # presence, so the safe park is the honest answer either way.
            #
            # Every OTHER browser fault propagates untouched — see the
            # docstring.
            self._log(
                "presence_search_inconclusive",
                request_id=req.request_id,
                data={
                    "reason_code": "search_refused_to_conclude",
                    # Which of the two: an evidence refusal or a wedged page.
                    # They park identically and read almost identically, so the
                    # transcript is the only place they stay distinguishable.
                    "kind": type(exc).__name__,
                    "error": str(exc),
                },
            )
            return None, (
                f"A by-content search ran and did NOT conclude ({exc}), so it "
                "is not evidence the request is missing — only that this read "
                "could not settle it."
            )
        self._log(
            "presence_search_completed",
            request_id=req.request_id,
            data={"found": found, "project_url": project_url},
        )
        if found is None:
            # The ONLY branch that may talk about absence. The park's base
            # sentence deliberately claims nothing beyond "the readback did not
            # see it" (see `_park_ambiguous`), so "either" would dangle here —
            # and this is the one outcome where a search actually walked the
            # chats to their end, which is what makes the stronger wording
            # earned rather than assumed.
            return None, (
                f"A by-content search of {project_url} read its recent chats to "
                "the end and did not find the request in any of them — the "
                "strongest evidence of absence autoloop can gather, though still "
                "not proof the backend never accepted the message."
            )
        return found, (
            f"A by-content search DID find {req.request_id} in {found}, which is "
            f"not this request's conversation ({req.conversation_url}) — a "
            "replacement chat reuses the request id, so that may be a retired "
            "copy. Read that chat before deciding; do NOT `--resubmit` on the "
            "strength of this line alone."
        )

    @staticmethod
    def _same_conversation(candidate: str, bound: str) -> bool:
        """True when two URLs name the SAME chat.

        A conversation is identified by its `/c/<id>`, never by the project
        prefix in front of it. ChatGPT rewrites that prefix with the project
        slug, and `find_conversation_with` builds candidates with `urljoin`
        against the project page, so the same chat legitimately arrives here
        as `https://chatgpt.com/c/<id>` while the request is bound to
        `https://chatgpt.com/g/g-p-<id>-<slug>/c/<id>`. Comparing the strings
        would call those two different chats — turning every resolution back
        into a park in production while passing every test that types URLs by
        hand. Same trap `BrowserChatGPT._is_candidate_page` documents.

        Falls back to a path compare when neither side is a `/c/<id>` URL, so
        a non-ChatGPT provider is compared exactly, not waved through.
        """
        if not candidate or not bound:
            return False
        left, right = urlsplit(candidate), urlsplit(bound)
        if left.netloc != right.netloc:
            return False
        left_id, right_id = _conversation_id(candidate), _conversation_id(bound)
        if left_id is not None or right_id is not None:
            return left_id == right_id
        return left.path.rstrip("/") == right.path.rstrip("/")

    def _park_ambiguous(
        self,
        req: PendingRequest,
        reconciled: bool,
        found: str | None = None,
        search_note: str = "",
    ) -> None:
        """Stop on an ambiguous submission. Never resends automatically.

        **The question states the evidence obtained, and nothing beyond it.**
        The base sentence used to open "the request is not in persisted history
        after reconciliation" — a claim `reconcile()` cannot support, because it
        reads the conversation's MOUNTED WINDOW and ChatGPT mounts a window of a
        chat rather than its history (that is the whole reason
        `_resolve_or_park_ambiguous` exists). In the no-project,
        search-inconclusive, wedged-page and no-search-capability parks, that
        sentence asserted absence and the note beneath it then said absence was
        never established — manufactured evidence, in the exact path this code
        was written to repair, pointing an operator at `--resubmit`. So the base
        sentence now reports only what the readback did: it did not SEE the
        request. Language strong enough to mean "it is not there" belongs to the
        one branch that earned it — the search that read the chats to their end
        and came back empty — and lives in `search_note`, not here.

        `search_note` says what the by-content search did — it is the operator's
        only way to tell "the project was read and the request is in none of it"
        apart from "no search ran", and those point at different next actions.
        Its only caller is `_resolve_or_park_ambiguous`, which always has one;
        it defaults empty so a future park that never searched says nothing
        about a search rather than inventing one.
        """
        self._log(
            "submission_ambiguous",
            request_id=req.request_id,
            data={
                "reconciled": reconciled,
                "reconcile_attempts": req.reconcile_attempts,
                "found_elsewhere": found,
            },
        )
        self._to_needs_user(
            f"submission of {req.request_id} is AMBIGUOUS: a send was attempted and "
            "reconciliation did not SEE the request in the window it read back. That "
            "readback is the conversation's mounted window, not its full history, so "
            "it reports what the page had rendered rather than what the chat holds. "
            "Autoloop will not resend on its own — the backend may have accepted a "
            "message the browser never observed, so resending risks a duplicate post. "
            + (search_note + " " if search_note else "")
            + "Inspect the conversation, then either `run --retry` (reconcile again) "
            "or `run --resubmit` (send this same request id once more; if it did "
            "land, it is detected and not duplicated).",
            resume_phase=Phase.SUBMISSION_UNCONFIRMED.value,
            kind="loop_fatal",
            code="submission_ambiguous",
            detail=f"request_id={req.request_id} reconcile_attempts={req.reconcile_attempts}",
        )

    def _step_awaiting(self) -> None:
        state = self.state
        req = state.pending_request
        if req is None:
            raise StateError("phase=awaiting but no pending request")
        # Aimed at the request's OWN conversation. After a rotation the loop's
        # current conversation and this request's are the same, but a request
        # that predates the rotation keeps its old binding — so a reply that
        # arrives late in an abandoned chat is never the one that gets read.
        client = self._client_for_request(req)
        # attach() navigates only when the page is absent or elsewhere — never a
        # reload here, or a streaming answer would be destroyed mid-flight.
        client.attach()
        raw = client.await_response(req.request_id)
        state.last_response = LastResponse(
            request_id=req.request_id,
            raw=raw,
            received_at=utcnow_iso(),
            head_sha=req.head_sha,
            base_sha=req.base_sha,
            report_sha256=req.report_sha256,
            postcommit=req.postcommit,
            changeset=req.changeset,
            # THE changeset-binding bug (docs/AUTOLOOP_TODO.md A2). This is the
            # only production construction of `LastResponse`, and it carried
            # `postcommit` but silently dropped `changeset` — so an operator
            # changeset bound correctly at request time arrived at dispatch
            # unbound, and `_dispatch_changeset_push` refused a push it should
            # have published. The binding was never "lost in transit": it was
            # built, submitted and awaited intact, then not copied here.
            conversation_url=req.conversation_url,
            conversation_epoch=req.conversation_epoch,
            provider=req.provider or self.active_provider(),
        )
        state.pending_request = None
        state.consecutive_failures = 0
        state.phase = Phase.EXECUTING.value
        self._log("response_received", request_id=req.request_id, data={"raw": raw})
        self._store.save(state)

    def _step_executing(self) -> None:
        state = self.state
        resp = state.last_response
        if resp is None:
            raise StateError("phase=executing but no response recorded")
        try:
            directive = parse_response(resp.raw)
        except ContractError as exc:
            self._handle_parse_error(exc)
            return
        state.parse_retries = 0
        state.last_decision = directive.decision.value
        self._log(
            "directive",
            request_id=resp.request_id,
            data={
                "decision": directive.decision.value,
                "reason": directive.reason,
                "task_id": directive.task_id,
                "planned_tasks": len(directive.tasks) if directive.tasks else 0,
                "commit_message": directive.commit_message,
                "question": directive.question,
            },
        )
        # A postcommit-bound push publishes `resp.postcommit.task_branch`, an
        # entirely different ref than whatever the main checkout has current
        # (usually the branch the orchestrator itself runs from, e.g. "main").
        # A changeset-bound push (`resp.changeset`) similarly targets its own
        # recorded `branch`, not necessarily whatever this checkout happens
        # to be on by the time the approval is dispatched — the checkout
        # normally still IS that branch (the whole point of a changeset
        # review), but `resp.changeset.branch` is the pinned value from
        # binding time, and using it rather than a fresh lookup means a
        # config change to `protected_branches`, or an operator switching
        # branches, between review and dispatch cannot retroactively make a
        # protected destination look unprotected. `authorize_directive`'s
        # protected-branch gate must judge the ACTUAL push destination in
        # either case, not the main checkout's current branch — otherwise a
        # produce-then-review or changeset push would be evaluated against
        # the wrong branch name (denying it whenever the main checkout sits
        # on "main"/"master", the exact opposite of what protected_branches
        # is meant to gate).
        if resp.postcommit is not None and directive.decision in PUSH_DECISIONS:
            destination_branch = resp.postcommit.task_branch
        elif resp.changeset is not None and directive.decision in PUSH_DECISIONS:
            destination_branch = resp.changeset.branch
        else:
            destination_branch = self._git.current_branch()
        verdict = self._policy.authorize_directive(directive, destination_branch, self._registry)
        if not verdict.allowed:
            self._handle_policy_denial(directive, verdict)
            return
        if directive.decision in REVIEWED_DECISIONS:
            try:
                verify_review(directive, resp.request_id, resp.head_sha, resp.report_sha256)
            except ContractError as exc:
                self._handle_review_mismatch(exc)
                return
            # The generic "repository HEAD must still equal the reviewed
            # head_sha" staleness check below does not apply to a
            # changeset-bound PUSH: `head_sha` there is THIS checkout's HEAD
            # at packet-render time, and this checkout IS the branch the
            # operator keeps committing to — advancing it after the review
            # was sent is the expected, legitimate case a changeset review
            # exists to handle (publish the reviewed candidate, not
            # whatever HEAD has since become). See `changeset_review`'s
            # module docstring for the full reasoning. Identity for this
            # path is carried entirely by `resp.changeset.candidate_sha`
            # plus the `report_sha256` digest just verified above. Narrowed
            # to `decision is PUSH` (not every REVIEWED_DECISIONS member):
            # `resp.changeset` is only ever meant to answer a `push` (see
            # `PendingRequest.changeset`'s docstring) — a `commit`/
            # `commit_and_push` reply somehow carrying one still gets the
            # ordinary staleness check, and separately lands in
            # `legacy_git_path_retired` either way (`_dispatch`'s changeset
            # branch only fires for `Decision.PUSH`).
            if directive.decision is not Decision.PUSH or resp.changeset is None:
                current_head = self._git.head_sha()
                if current_head != directive.reviewed.head_sha:
                    self._handle_review_mismatch(
                        ContractError(
                            "review_mismatch:head_moved",
                            f"repository HEAD is {current_head[:12]} but the approval "
                            f"references {directive.reviewed.head_sha[:12]} — the tree "
                            "changed since the review",
                        )
                    )
                    return
        state.policy_denials = 0
        self._dispatch(directive)

    # ---- dispatch -----------------------------------------------------------

    def _dispatch(self, directive: Directive) -> None:
        state = self.state
        decision = directive.decision
        if decision is Decision.STOP:
            state.stop_reason = directive.reason
            state.last_response = None
            state.phase = Phase.STOPPED.value
            # Classified explicitly, not left at the default: `stopped` is now
            # reached two ways (here, and `_to_fault_stop`), and a reader that
            # inferred "clean" from the phase alone would announce a run that
            # died on a wall as a healthy finish. See `LoopState.stop_kind`.
            state.stop_kind = "contract"
            self._log("stopped", data={"reason": directive.reason, "kind": "contract"})
            self._store.save(state)
        elif decision in RETIRED_DECISIONS:
            # Retired 2026-08-06, mirroring the retired legacy git path below.
            # `authorize_directive` denies a retired decision unconditionally,
            # so a directive normally never reaches this branch at all — which
            # is exactly why the branch has to STAY. Deleting it would drop
            # `ASK_USER` into the terminal `else` and dispatch it to the
            # executor, which has no task to run and no notion of a question:
            # a retirement that opened a worse hole than it closed. Refused
            # here through the SAME budget-capped corrective-reprompt
            # machinery, so a directive arriving by any path that skipped the
            # policy gate still cannot park the loop or reach the executor.
            #
            # The verdict comes from `policy.retired_decision_verdict`, the
            # one place the retirement's code and guidance text are written,
            # so the reviewer is told the same thing whichever site caught it.
            self._handle_policy_denial(directive, retired_decision_verdict(decision))
        elif decision is Decision.PLAN:
            self._dispatch_plan(directive)
        elif decision is Decision.PUSH and state.last_response is not None and (
            state.last_response.changeset is not None
        ):
            # A push answering an operator-changeset review packet publishes
            # via the Publisher, sourced entirely from the response's binding
            # (never from `directive`, and never a fallback to the legacy
            # path below) — see `_dispatch_changeset_push`'s docstring.
            # Checked BEFORE the postcommit branch below only because both
            # conditions can never be true at once in practice (see
            # `state.PendingRequest.changeset`'s docstring); the order itself
            # carries no meaning.
            self._dispatch_changeset_push(directive, state.last_response)
        elif decision is Decision.PUSH and state.last_response is not None and (
            state.last_response.postcommit is not None
        ):
            # A push answering a produce-then-review packet publishes via
            # `push_exact`, sourced entirely from the response's binding
            # (never from `directive` — see `_dispatch_task_push`'s
            # docstring).
            self._dispatch_task_push(directive, state.last_response)
        elif decision in COMMIT_DECISIONS or decision in PUSH_DECISIONS:
            # The legacy authorize-then-produce commit/push path (`commit`,
            # `commit_and_push`, and any `push` NOT bound to a produce-then-
            # review candidate or an operator-changeset review (the bound
            # cases are handled above) was retired 2026-07-30
            # (docs/SECURITY.md S21: closed by retirement, not by
            # a fix — see `_dispatch_task_postcommit`). Nothing in this
            # codebase's own prompts ever asks ChatGPT for these anymore; if
            # one arrives anyway (a stale habit, a hand-crafted directive), it
            # is refused through the SAME budget-capped corrective-reprompt
            # machinery as any other policy denial — never silently routed to
            # the executor, which does not understand a git decision.
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "legacy_git_path_retired",
                    "direct commit/push is no longer supported — the orchestrator "
                    "commits automatically after implementing (or auditing) a "
                    "task in its own worker repo; the only valid approval is "
                    "`push` with the `reviewed` stamp answering a postcommit or "
                    "operator-changeset review packet",
                ),
            )
        else:  # audit / implement / revise
            self._dispatch_executor(directive)

    def _dispatch_plan(self, directive: Directive) -> None:
        state = self.state
        specs = directive.tasks or ()
        try:
            self._registry.add_many(
                [
                    Task(
                        id=s.id,
                        title=s.title,
                        description=s.description,
                        depends_on=s.depends_on,
                        approved_paths=s.approved_paths,
                    )
                    for s in specs
                ]
            )
        except TaskGraphError as exc:
            state.policy_denials += 1
            self._log("plan_rejected", data={"code": exc.code, "error": str(exc)})
            budget = self._policy.check_denial_budget(state.policy_denials)
            state.last_response = None
            if not budget.allowed:
                # PARKS, where `_handle_policy_denial`'s exhaustion of the same
                # `state.policy_denials` counter now STOPS. Deliberate, and the
                # divergence is the interesting part: a rejected plan is a
                # task-GRAPH fault (a cycle, a dependency on an id that does
                # not exist, a duplicate), and the roadmap it is about is an
                # artefact an operator owns and can repair between runs — so
                # there is a real question to hold the session open for. A
                # policy denial has no such external repair: the only thing
                # that could produce a different directive is the reviewer.
                self._to_needs_user(
                    f"{budget.reason} — last plan rejection: {exc}",
                    kind="loop_fatal",
                    code="plan_denial_budget_exhausted",
                    detail=f"task_graph_error={exc.code}: {exc}",
                )
                return
            state.outbox = plan_rejected_payload(exc.code, str(exc))
            state.phase = Phase.READY.value
            self._store.save(state)
            return
        self._task_store.save(self._registry)
        self._log("plan_accepted", data={"ids": [s.id for s in specs]})
        state.outbox = TEMPLATES["plan_ack"].render(
            added_count=str(len(specs)), roadmap=self._registry.summary()
        )
        state.last_response = None
        state.consecutive_failures = 0
        state.phase = Phase.READY.value
        self._store.save(state)

    def _dispatch_executor(self, directive: Directive) -> None:
        """`audit`/`implement`/`revise` all run through the SAME produce-
        then-review commit path (`_dispatch_task_postcommit`) — the legacy
        authorize-then-produce/change-manifest branch that used to live here
        was retired 2026-07-30 (docs/SECURITY.md S21: closed by retirement,
        not by a fix). The audit is dispatched as a task-shaped unit of work
        too (`_resolve_audit_task`), so there is no more "task is None" fork:
        every directive that reaches this method ends up in its own isolated
        worker repo, committed automatically, reviewed from the immutable
        commit, never from a pre-commit manifest.
        """
        state = self.state
        is_audit = (
            directive.decision is Decision.AUDIT or directive.task_id == AUDIT_TASK_ID
        )
        if not is_audit and directive.decision in TASK_DECISIONS:
            task = self._registry.get(directive.task_id)
            if directive.decomposition is not None:
                # The approved plan, made durable BEFORE the executor runs, in
                # the same save as `mark_in_progress` — so a task can never be
                # in progress against a plan nothing recorded. `implement`
                # cannot get this far without one (policy's
                # `_check_decomposition`); `revise` reaching here with one is a
                # deliberate reshape, and reaching here without one leaves the
                # stored plan exactly as it was.
                self._registry.set_decomposition(
                    task.id, directive.decomposition.render()
                )
            self._registry.mark_in_progress(task.id)
            self._task_store.save(self._registry)
            state.current_task = {
                "task_id": task.id,
                "title": task.title,
                "decision": directive.decision.value,
                "started_at": utcnow_iso(),
            }
        else:  # audit, or revise of the audit pseudo-task
            task = self._resolve_audit_task(directive, state)
            if task is None:
                return  # parked; _resolve_audit_task already reported why

        self._dispatch_task_postcommit(directive, task, state)

    def _audit_unit_quarantined(self, unit_id: str) -> bool:
        """Has the operator quarantined this exact audit unit?

        Audit units are synthetic — minted here, never planned — so most of
        the time the registry has never heard of one. That is the ordinary
        case (a fresh audit), not an error: only a unit that previously
        parked task_fatal is in the registry at all, put there by
        `TaskRegistry.block`. Anything unknown is therefore dispatchable.

        Asked of `state_of` rather than of the status string, so RETIRED
        counts too. It is the same question — "has a human taken this unit out
        of the queue?" — and a retirement is the stronger form of the answer:
        it never resolves at all. Reading the string would have re-dispatched
        a retired unit, which is the failure this method was written for
        (2026-08-02, a quarantined `audit-0001` dispatched four times), with
        one word changed.
        """
        if not self._registry.has(unit_id):
            return False
        return self._registry.state_of(unit_id) in (
            TaskState.BLOCKED_BY_OPERATOR,
            TaskState.RETIRED,
        )

    def _resolve_audit_task(self, directive: Directive, state: LoopState) -> Task | None:
        """The audit's own stable per-run identity, distinct from the
        protocol-level pseudo-id `AUDIT_TASK_ID` ("audit" — what ChatGPT
        always sends as `task_id` on a revise-of-audit directive, per the
        contract). `WorkerRepoManager` keys one worker repo per id and
        `TaskExecution.review_round`/`attempt_count` cap per id too, so if
        every audit reused the literal "audit", a second audit in the same
        session could never create its repo, and the first audit's round/
        attempt counters would permanently cap every later one — continuous
        mode's "one audit per changed fingerprint" selection policy (see
        `cli.py`) depends on audits NOT sharing an id across runs.

        A fresh `audit` decision mints a new id (`audit-<iteration>`, unique
        per request since `state.iteration` only ever increases — and stable
        across a crash-redispatch of the SAME request, exactly like the
        retired manifest id was). A `revise` targeting the pseudo-id resumes
        the id the immediately preceding audit/revise round in this arc
        used — read from `state.current_task` BEFORE this call overwrites
        it — so round 2 lands on the SAME worker repo, branch and
        `TaskExecution` as round 1 instead of forking a new one and losing
        round 1's commit. Returns `None` (having already parked) if a
        revise arrives with no audit currently on record.
        """
        if directive.decision is Decision.AUDIT:
            unit_id = f"audit-{state.iteration:04d}"
            # The audit pseudo-task skips `authorize_directive`'s
            # `_check_task_reference`, so nothing else asks whether the id
            # about to be dispatched is one the operator already quarantined.
            # It matters because the id is NOT unique per attempt: it is
            # `audit-<iteration>`, and an audit that parks before completing
            # its iteration re-mints the SAME id next time. Observed
            # 2026-08-02: a quarantined `audit-0001` was re-dispatched four
            # times in a row, each costing a full ChatGPT round trip, each
            # parking on the same stale execution record — the loop looked
            # alive while making no progress at all.
            #
            # Denying rather than parking is the point. A park here would
            # re-quarantine an already-quarantined task and leave ChatGPT
            # none the wiser, which is precisely the cycle that churned.
            # A denial re-prompts with the reason and is bounded by
            # `check_denial_budget`, so ChatGPT can pick a real task and a
            # genuinely stuck loop still stops instead of spinning.
            if self._audit_unit_quarantined(unit_id):
                self._handle_policy_denial(
                    directive,
                    Verdict.deny(
                        "audit_unit_quarantined",
                        f"the audit unit {unit_id} is quarantined "
                        "(blocked_by_operator, or retired) and re-running `audit` would "
                        "re-dispatch that same unit, not a fresh one — the id is "
                        "derived from the loop iteration, which a parked audit "
                        "does not advance. Choose a different ready task, or ask "
                        "the operator to clear the quarantine (`autoloop "
                        f"blockers` / `answer`) and archive "
                        f".autoloop/executions/{unit_id}.json.",
                    ),
                )
                return None
        else:
            prior = (state.current_task or {}).get("task_id") or ""
            if not prior.startswith("audit-"):
                state.last_response = None
                self._to_needs_user(
                    "revise of the audit pseudo-task has no audit currently on "
                    "record for this session (state.current_task carries no audit "
                    "unit id) — nothing was executed. Run `audit` first.",
                    kind="loop_fatal",
                    code="audit_revise_no_record",
                )
                return None
            unit_id = prior
        state.current_task = {
            "task_id": unit_id,
            "title": "repository audit",
            "decision": directive.decision.value,
            "started_at": utcnow_iso(),
        }
        return Task(id=unit_id, title="repository audit", description="repository audit")

    # ---- produce-then-review commit path (pass 2a + 2b) ----------------------
    #
    # A real task runs in its own worktree/branch (`WorktreeManager`) and
    # commits immediately after the executor reports success and the commit
    # passes structural + re-run-validation checks — never gated on a prior
    # chat approval, because none exists for this path. On ANY check failure
    # the commit is left exactly where it is: nothing here can roll it back
    # (reset/checkout/clean are not on the git command whitelist), so refusal
    # means "park and report", never "undo". A clean pass builds the review
    # packet (`packet.build_review_packet`) and sends it for review — success
    # re-enters `ready`, it does not park (pass 2b; pass 2a parked here with
    # an "awaiting review, not wired yet" placeholder).
    #
    # Maximum two review rounds per task (`execution.review_round`): round 1
    # is the initial `implement`, round 2 is one `revise`. A THIRD round
    # (another `revise` once `review_round == 2`) never reaches the executor
    # or ChatGPT — `_park_round_cap` parks immediately with both the full
    # range diff and the latest round's own diff, plus the feedback that
    # triggered it.
    #
    # The audit runs through here too (2026-07-30): `_dispatch_executor`
    # resolves it to a synthetic `Task` (`_resolve_audit_task`) with its own
    # stable per-run id, so `task` below is never `None` — there is exactly
    # ONE produce-then-review commit path, audit included.

    def _tracker_paths(self) -> tuple[str, ...]:
        """The repository's implicitly-approved documentation trackers:
        `tasks.TRACKER_PATHS`, and nothing else.

        Read through ONE accessor so every `effective_approved_paths` call in
        this file passes the same list. The dispatch-time seed and the
        every-dispatch re-sync below COMPARE against it and ASSIGN from it: if
        those two ever read different values, the execution record reads dirty
        on every dispatch and is rewritten forever.

        **It returns a reviewed CONSTANT, deliberately — not `self._config`.**
        A tracker is granted to every scoped task without being named in it, so
        the list is authorization surface, and `.autoloop/config.toml` is
        gitignored: sourcing it from there would let an unreviewed edit widen
        every task at once. Reading any config value here reopens
        `docs/SECURITY.md` S31 and must not be done without revisiting it. The
        accessor stays (rather than inlining the constant at the three call
        sites) because it is the one place a test can pin that property —
        `test_config_repo_section.py` scans this file for both halves.
        """
        return TRACKER_PATHS

    def _open_attempt(self, execution: TaskExecution) -> None:
        """Charge one dispatch and record it as OPEN.

        First of the four operations over `TaskExecution.attempt_ledger`, and
        the order they run in is the whole design:

          `_reconcile_unfinished_attempts`  at dispatch, BEFORE the ceilings
          `_open_attempt`                   at dispatch, just before the executor
          `_finalise_attempt`               on EVERY exit of the dispatched round
          `_note_round_fault`               at a session-ending fault handler

        The invariant they maintain: at most one entry is ever OPEN, it is
        always the last one, and it exists only between this method and the
        round's exit. Everything else here follows from that.

        Charged BEFORE the executor runs and persisted immediately — the M1
        finding #3 property this must not lose. A crash, a restart or a
        validation failure that never reaches `commit_and_capture` has already
        consumed something by the time it happens; which budget it consumed is
        settled afterwards, but that it consumed one is settled here.

        The one case that skips the task budget is a round the loop already
        knows is a redo: `pending_fault_code` is set when a fault destroyed a
        review this task had already earned (`_note_round_fault`) or when the
        redo of such a review was itself taken by the environment
        (`_settle_attempt` rule 4), and it is consumed exactly once per
        dispatch — cleared here, in the same save. Consumed once, but possibly
        re-armed afterwards: a recovery chain that keeps being interrupted keeps
        its replacement dispatches on the fault budget, and pays a
        `fault_attempt_count` charge for each one, so it still ends.

        A redo is still opened OPEN (as `ATTEMPT_PENDING_FAULT`, carrying the
        fault code as its origin), never straight to `ATTEMPT_FAULT`. Writing
        the settled label here would say "this round has finished" before it has
        started, and the round's own exit would then have nothing to stamp — see
        `worktask.ATTEMPT_PENDING_FAULT` for the accounting bug that produced.
        """
        ordinal = len(execution.attempt_ledger) + 1
        fault_code = execution.pending_fault_code
        if fault_code:
            execution.fault_attempt_count += 1
            budget, reason = ATTEMPT_PENDING_FAULT, fault_code
            execution.pending_fault_code = ""
        else:
            execution.attempt_count += 1
            budget, reason = ATTEMPT_PENDING, "dispatched"
        execution.attempt_ledger += (format_attempt(ordinal, budget, reason),)
        self._execution_store.save(execution)

    def _settle_attempt(
        self, execution: TaskExecution, index: int, budget: str, reason: str
    ) -> bool:
        """Stamp the OPEN ledger entry at `index` with the budget it really
        spent and why, moving the charge between counters if the round's outcome
        disagrees with what its dispatch provisionally charged.

        Shared by `_finalise_attempt` and `_reconcile_unfinished_attempts` so
        the two cannot drift: settling a round is one rule, applied in one
        place, whether the round reached an exit or the process died in it.

        THE RULE, in the order it is applied:

        1. The outcome decides. `budget` is what the CALLER observed — a fault
           the round could not have avoided, or the task's own work being wrong
           — and that is normally the budget charged, whichever one the dispatch
           provisionally picked.
        2. One exception, and only one: a round OPENED on the fault budget that
           ends `sent_for_review` STAYS on the fault budget. It exists purely to
           redo a review a fault destroyed, and it achieved exactly that;
           charging it to `attempt_count` would bill the task twice for one
           review, which is the whole defect budget-01 is about.
        3. Anything else a redo ends as — a failed validation, a structural
           refusal, an escape — is a genuine new defect no reviewer has seen, so
           the charge moves BACK to the task budget. A redo does not launder a
           task failure into a fault.
        4. A redo that STAYS on the fault budget without reaching a review
           re-arms `pending_fault_code` with its own origin, because the review
           that fault destroyed is still destroyed and the next dispatch is
           still the recovery of it, not the task's own next try. Without this
           the chain silently fell back onto `attempt_count` at its second
           interruption — the defect this rule closes.

        Rule 4 lands here rather than at any one caller BECAUSE this method is
        the shared one: the three ways a redo can be interrupted without
        producing a review all arrive through it, and only one of them was ever
        named. `_reconcile_unfinished_attempts` brings the process that died
        mid-redo (`interrupted_mid_round`); `_finalise_attempt` brings the redo
        whose agent hit a provider throttle or a stall kill
        (`ExecutionOutcome.fault_kind`) and the one that found its worker repo
        drifted (`worker_environment_drift`). They are the same event to this
        accounting — the environment took the round — so they get the same
        answer, in one place, instead of three call sites each remembering.

        Returns whether an open entry was actually settled. Never drops a
        charge: the counters always still sum to `len(attempt_ledger)`.
        """
        entries = list(execution.attempt_ledger)
        ordinal, opened_as, opened_reason = split_attempt(entries[index])
        if opened_as not in ATTEMPT_OPEN:
            return False
        opened_on_fault = opened_as == ATTEMPT_PENDING_FAULT
        charged = ATTEMPT_FAULT if opened_on_fault else ATTEMPT_TASK
        target = ATTEMPT_FAULT if budget == ATTEMPT_FAULT else ATTEMPT_TASK
        if opened_on_fault and reason == REASON_SENT_FOR_REVIEW:
            target = ATTEMPT_FAULT
        if opened_on_fault and target == ATTEMPT_FAULT and reason != REASON_SENT_FOR_REVIEW:
            # Rule 4. All three clauses carry weight. `opened_on_fault`: only a
            # redo has a lost review to still be recovering. `target`: a redo
            # that failed on its OWN merits has just moved back to the task
            # budget, and the task now owes a fix rather than a retry, so the
            # chain ends there. `reason`: a redo that REACHED the reviewer must
            # not arm a recovery for a review that is not lost — if that review
            # dies too, `_note_round_fault` arms it then, on evidence.
            #
            # The ORIGIN is carried forward, not this round's own fault code, so
            # a chain reads as one chain — `browser_session_lost>...` on every
            # entry, each with its own outcome — rather than as a fresh incident
            # per interruption.
            #
            # Bounded, which is the whole reason this is safe: re-arming costs a
            # `fault_attempt_count` charge (`_open_attempt` increments it when it
            # consumes the marker), so a chain interrupted forever walks into
            # `fault_attempt_ceiling` and parks. It is a carried-forward
            # allowance, never an exemption.
            execution.pending_fault_code = opened_reason
        if target != charged:
            # MOVE, never drop: the two counters together always equal
            # `len(attempt_ledger)`, which is what keeps the combined bound
            # (see `MAX_TASK_FAULT_ATTEMPTS`) exact rather than approximate.
            delta = 1 if target == ATTEMPT_FAULT else -1
            execution.fault_attempt_count += delta
            execution.attempt_count -= delta
        entries[index] = format_attempt(
            ordinal,
            target,
            # The origin survives only for a redo. An ordinary round's open
            # reason is the `"dispatched"` placeholder, which says nothing.
            compose_reason(opened_reason if opened_on_fault else "", reason),
        )
        execution.attempt_ledger = tuple(entries)
        return True

    def _finalise_attempt(
        self, execution: TaskExecution, budget: str, reason: str
    ) -> None:
        """Stamp the open attempt with the budget it is charged to and why.

        Called on every exit path of a dispatched round, which is what makes a
        still-OPEN entry mean one specific thing rather than being a guess: the
        process died before the round finished. Nothing else can leave one
        behind — including a redo, which is why a redo is opened OPEN too.

        Idempotent and one-way. An entry that has already been settled is left
        exactly as it is — a later call cannot re-charge it, cannot move it
        between budgets, and cannot rewrite the reason. That is the direct
        guard against a genuine task failure being quietly relabelled a fault
        by anything downstream of the round that produced it.
        """
        if not execution.attempt_ledger:
            return
        if not self._settle_attempt(execution, -1, budget, reason):
            return
        self._execution_store.save(execution)

    def _reconcile_unfinished_attempts(self, execution: TaskExecution) -> None:
        """Settle any attempt still recorded as OPEN, as the fault it was.

        Reached only at the start of a dispatch, so every entry it can see
        belongs to an EARLIER round — and an earlier round that never stamped
        itself is a round that never reached one of its own exits. Two ways that
        happens, and both are environmental. The process did not survive: an
        operator pause or restart mid-round, a kill, a crash inside the agent.
        Or a `GitError` escaped `_dispatch_task_postcommit` to
        `_handle_git_failure` — git unreadable while verifying the commit or
        listing its range — which the loop already treats as an environment
        failure, charging `consecutive_failures` and returning to `ready` rather
        than blaming the task. Every failure the ROUND itself decided stamps
        before it returns, so neither shape can be the work being wrong. Both are
        faults by the definition this task is built on (the round produced no
        reviewable outcome), and they are charged accordingly. A round that was
        already on the fault budget (a redo) simply stays there; the charge does
        not move, only the stamp is added.

        **This cannot silently reclassify a genuine task failure.** The only
        entries it touches are ones that positively read as OPEN, and a failure
        that actually happened — validation failed, the commit was refused, the
        paths were outside scope, the reviewer said revise — went through
        `_finalise_attempt` and reads `task`. `_settle_attempt` refuses to
        re-stamp, so there is no path by which a settled entry can become open
        again. An unparseable or hand-edited entry reads as neither (see
        `worktask.split_attempt`) and is left alone.

        Still bounded: these charges land in `fault_attempt_count`, so a task
        whose process dies every single round parks on `fault_attempt_ceiling`
        rather than churning forever.

        One case deserves naming because it used to leak. A process that dies
        while a REDO is open settles that redo onto the fault budget here — and
        the review the redo existed to recover is still lost, so the dispatch
        after it is still recovery work. `_settle_attempt` rule 4 re-arms
        `pending_fault_code` from the redo's own origin for exactly that reason,
        which is why this method runs BEFORE `_open_attempt` reads the marker.
        It is still classification, not authorization: the fact that carries
        forward is one the settled entry already states, the ceilings below are
        read after it, and every replacement dispatch is charged.
        """
        reclaimed = 0
        for index in range(len(execution.attempt_ledger)):
            if self._settle_attempt(
                execution, index, ATTEMPT_FAULT, "interrupted_mid_round"
            ):
                reclaimed += 1
        if not reclaimed:
            return
        self._execution_store.save(execution)
        self._log(
            "attempt_reclassified",
            data={
                "task_id": execution.task_id,
                "reason": "interrupted_mid_round",
                "attempts": reclaimed,
                "attempt_count": execution.attempt_count,
                "fault_attempt_count": execution.fault_attempt_count,
                # Non-empty when one of the settled rounds was a redo the
                # environment took: the recovery is still open and the NEXT
                # dispatch is charged to the fault budget. Logged so that
                # carry-forward is visible in the transcript rather than only
                # inferable from the ledger two entries later.
                "pending_fault_code": execution.pending_fault_code,
            },
        )

    def _note_round_fault(self, code: str) -> None:
        """Record that a session-ending fault destroyed a review this task had
        already earned, so the redo is charged to the fault budget.

        The narrow case `_finalise_attempt` cannot cover. When a round commits
        and hands its packet to the reviewer, that round is a real attempt and
        is charged as one — it produced work. But if the SESSION then dies on a
        rate limit, an exhausted allowance or a browser failure that spent its
        budget, the review never arrives and the whole round has to be performed
        again. brw-11 lost three rounds that way with its fix already committed
        and passing.

        Conditional on the last entry being a SETTLED round whose OUTCOME was
        `sent_for_review`, and deliberately indifferent to which budget that
        round spent. Both halves matter:

        * settled, because a fault at any other moment either left an open entry
          (which `_reconcile_unfinished_attempts` settles, and whose recovery
          chain `_settle_attempt` rule 4 carries forward without needing this
          method) or interrupted nothing this task can be credited for, and
          marking the latter would hand out free attempts on a condition nobody
          checked;
        * outcome rather than the whole reason, and either budget, because a
          redo that reaches review is recorded as
          `fault|<origin>>sent_for_review`. Matching the literal pair
          `(task, "sent_for_review")` — which this did until the composed reason
          existed — missed exactly that entry, so a SECOND session-ending fault
          went unmarked and its redo was charged to `attempt_count`. Consecutive
          faults must not alternate back into the task's budget.

        Best-effort throughout: this runs inside a failure handler, and a
        bookkeeping write that could itself raise would turn one fault into two.
        """
        if self._execution_store is None:
            return
        try:
            record = self.state.task_execution or {}
            task_id = record.get("task_id") or ""
            if not task_id:
                return
            execution = self._execution_store.load(task_id)
            if execution is None or not execution.attempt_ledger:
                return
            _, budget, reason = split_attempt(execution.attempt_ledger[-1])
            if budget not in (ATTEMPT_TASK, ATTEMPT_FAULT):
                return
            if attempt_outcome(reason) != REASON_SENT_FOR_REVIEW:
                return
            if execution.pending_fault_code:
                return          # already marked by an earlier fault in this episode
            execution.pending_fault_code = code
            self._execution_store.save(execution)
            self._log(
                "round_fault_noted",
                data={"task_id": task_id, "code": code},
            )
        except Exception:
            return

    def _dispatch_task_postcommit(self, directive: Directive, task: Task, state: LoopState) -> None:
        if (self._worktrees is None and self._worker_repos is None) or (
            self._execution_store is None or self._intent_store is None
        ):
            # There is no other dispatch path left to fall back to (the
            # legacy authorize-then-produce/manifest branch was retired
            # 2026-07-30 — see docs/SECURITY.md S21). An Orchestrator built
            # without a full worktrees-or-worker_repos / execution_store /
            # intent_store set (e.g. `_cmd_smoke_browser`'s deliberately
            # minimal construction) cannot execute ANY directive that reaches
            # here — fail with a clear, catchable error rather than a raw
            # AttributeError on the first `None.create(...)`/`None.load(...)`.
            raise ExecutorError(
                "this Orchestrator has no produce-then-review collaborators "
                "configured (need worker_repos or worktrees, plus "
                "execution_store and intent_store) — audit/implement/revise "
                "cannot run"
            )
        # Matches `_dispatch_executor`'s own `is_audit` computation EXACTLY
        # (deliberately duplicated, not shared — see that method's docstring
        # and `cli._DispatchingExecutor`, which makes the identical decision
        # a third time to route to the read-only vs write-capable executor).
        # The audit's write surface is already bounded by a DIFFERENT,
        # non-agent-controlled mechanism (`MarkdownPolicy` — at most one new
        # `docs/AUDIT_<date>.md`), so it is deliberately exempt from the
        # `approved_paths` gate below: that gate exists because an
        # `implement`/`revise` agent's own report was the ONLY thing
        # authorizing its scope (Autoloop M1 finding #2/#3), which was never
        # true for the audit.
        is_audit = directive.decision is Decision.AUDIT or directive.task_id == AUDIT_TASK_ID

        if not is_audit and not task.approved_paths:
            # Fail-closed gate for M1 finding #2 (circular task ownership):
            # `approved_paths` is the task's machine-checkable authorization
            # scope, set BEFORE the writer ever starts (a `plan` directive's
            # `TaskSpec.approved_paths`, or `seed_tasks.json`). A task with
            # none is not "unscoped" — it is simply never dispatchable, so an
            # omitted scope cannot silently fall back to "whatever the agent
            # touched" (which is exactly the circularity being closed: the
            # agent's own report must never be allowed to EXPAND its own
            # authorization).
            self._to_needs_user(
                f"task {task.id}: has no `approved_paths` — a write-capable "
                "implement/revise cannot be dispatched without an explicit, "
                "pre-authorized set of repository-relative paths it may "
                "touch. Add `approved_paths` to the task (via `plan` or "
                "seed_tasks.json) and try again. Nothing was executed.",
                kind="task_fatal",
                code="approved_paths_missing",
                task_id=task.id,
            )
            return

        # Bound from the Task HERE — before the writer runs, on the first
        # dispatch only — and then FROZEN on the execution record (B4b).
        #
        # `allowed_paths` below is deliberately re-synced from the Task on
        # every dispatch, because the Task is its authorization source. The
        # validation binding is the opposite: once written it is never
        # re-derived, so nothing downstream of the first dispatch can replace
        # or widen what the reviewed commit is checked against — not an agent
        # result, not a later edit to `seed_tasks.json`/`tasks.json`, not a
        # ChatGPT response. A resumed or crash-recovered round re-runs exactly
        # the commands the round was dispatched under.
        #
        # The ONE exception is a record with no binding at all (written before
        # this field existed, or hand-built by a test/embedder): adopting the
        # Task's value once is strictly better than falling back to the
        # generic audit set, and it cannot widen anything, because there was
        # nothing there to widen.
        declared_validation = (
            () if is_audit else tuple(tuple(c) for c in task.validation)
        )
        declared_validation_cwd = "" if is_audit else (task.validation_cwd or "")

        execution = self._execution_store.load(task.id)
        resumed = execution is not None
        # The three-fact reuse decision (wrk-01) is made HERE, before the
        # stale-base reconciliation below ever runs — that path would
        # otherwise quarantine and rebuild a perfectly valid worker solely
        # because the recorded base is behind the branch head, evicting the
        # very round being resumed. The probe asks exactly what the record
        # claims and nothing more: the recorded `worktree_path` exists, is a
        # git repository in its own right, and is checked out on the
        # recorded `task_branch`. When it passes, the decision is passed
        # into `_rebase_execution_if_stale` (which then skips its
        # nothing-reviewed-yet re-base) and carried through the rest of the
        # dispatch (`reused_recorded_worker` below).
        recorded_worker_reusable = False
        if execution is not None and self._worker_repos is not None:
            recorded_worker_reusable = worker_repo_is_reusable(
                Path(execution.worktree_path), execution.task_branch
            )
            execution = self._rebase_execution_if_stale(
                execution, task, worker_reusable=recorded_worker_reusable
            )
            if execution is None:
                return          # parked; _rebase_execution_if_stale explained why
        if execution is None:
            # First dispatch for this task: base sha is recorded BEFORE any
            # implementation work starts, from the MAIN checkout's HEAD (the
            # commit the task's branch forks from). `allowed_paths` is seeded
            # from `task.approved_paths` — fixed for the task's whole
            # lifetime, never from anything the executor reports — EXCEPT for
            # the audit, whose scope is bounded by `MarkdownPolicy` instead
            # (see the `is_audit` comment above) and keeps the pre-M1
            # accumulate-from-`changed_paths` behaviour, unchanged.
            base_sha = self._git.head_sha()
            allowed_paths = (
                ()
                if is_audit
                else effective_approved_paths(task.approved_paths, self._tracker_paths())
            )
            if self._worker_repos is not None:
                repo = self._worker_repos.create(task.id, self._git.repo_root, base_sha)
                execution = TaskExecution(
                    task_id=task.id,
                    task_branch=repo.branch,
                    worktree_path=str(repo.path),
                    task_base_sha=base_sha,
                    allowed_paths=allowed_paths,
                    validation_commands=declared_validation,
                    validation_cwd=declared_validation_cwd,
                )
            else:
                execution = self._worktrees.create(task.id, base_sha)
                execution.allowed_paths = allowed_paths
                execution.validation_commands = declared_validation
                execution.validation_cwd = declared_validation_cwd
            self._execution_store.save(execution)
        elif not is_audit:
            dirty = False
            # Computed ONCE and both compared against and assigned from, so the
            # two can never read a different tracker list (see
            # `_tracker_paths`).
            resynced = effective_approved_paths(task.approved_paths, self._tracker_paths())
            if execution.allowed_paths != resynced:
                # Re-synced every dispatch: `task.approved_paths` is the single
                # source of truth for a real task's authorization, and is never
                # derived from `execution` state, only ever written INTO it.
                execution.allowed_paths = resynced
                dirty = True
            if not execution.validation_commands and declared_validation:
                # Backfill ONLY — an unbound record adopts the Task's value
                # once. A record that already carries a binding is left alone
                # even when the Task now says something different: that
                # difference is exactly the widening this must not honour.
                execution.validation_commands = declared_validation
                execution.validation_cwd = declared_validation_cwd
                dirty = True
            if dirty:
                self._execution_store.save(execution)
        state.task_execution = asdict(execution)
        self._store.save(state)

        # A resumed round REUSES the worker its execution record names,
        # whenever that worker is still exactly what the record says it is:
        # present on disk, a git repository in its own right, and checked out
        # on the recorded `task_branch` (`worker_repo_is_reusable` — those
        # three facts and nothing more, decided ABOVE, before stale-base
        # reconciliation could touch the worker). Reuse touches nothing: no
        # clone, no branch switch, no rewrite of the record — and the
        # decision is CARRIED THROUGH preparation (`reused_recorded_worker`
        # below), so a reused worker is used exactly as it stands:
        # uncommitted residue from the interrupted round it is resuming is
        # the work being resumed, not grounds for
        # `_prepare_write_capable_worker` to quarantine it and start over.
        # Anything else falls back to the SAME creation call a first
        # dispatch makes — the recorded base fetched from the primary
        # checkout onto the recorded branch — so a missing directory is
        # simply recreated, while a path that exists but is not a git
        # repository (or is on the wrong branch) makes
        # `WorkerRepoManager.create` refuse with its usual actionable error.
        # No repair and no deletion: salvaging a half-broken worker is an
        # operator's decision, not this dispatch's. The re-probe on the
        # not-reusable side is for one case only: the stale-base re-base
        # above may have just rebuilt the worker at the recorded path, and
        # that fresh worker must not be handed to `create()` a second time.
        reused_recorded_worker = False
        if resumed and self._worker_repos is not None:
            if recorded_worker_reusable or worker_repo_is_reusable(
                Path(execution.worktree_path), execution.task_branch
            ):
                reused_recorded_worker = True
            else:
                self._worker_repos.create(
                    task.id,
                    self._git.repo_root,
                    execution.task_base_sha,
                    branch=execution.task_branch,
                )
                self._log(
                    "worker_recreated",
                    data={
                        "task_id": task.id,
                        "worktree_path": execution.worktree_path,
                        "task_branch": execution.task_branch,
                        "task_base_sha": execution.task_base_sha,
                    },
                )

        if self._worker_repos is not None:
            # The scrubbed env is not decoration: a GitGateway with no explicit
            # env inherits the CALLING process's credential helper and system
            # config regardless of how isolated the repo's own on-disk config
            # is, so an isolation check run without it inspects the wrong thing.
            worktree_git = GitGateway(
                Path(execution.worktree_path), self._policy, env=worker_env()
            )
            violations = verify_worker_isolation(worktree_git)
            if violations:
                # loop_fatal, not task_fatal: isolation is provided by
                # WorkerRepoManager/worker_env() for EVERY task, not
                # something specific to this one — a violation here means
                # the isolation mechanism itself may be compromised (a
                # credential/config leak), which would very likely recur on
                # the next task too. That is an environment-shaped problem,
                # not "this one unit of work can't proceed" — quarantining
                # just this task and churning on to the next would silently
                # repeat a security-relevant failure rather than surfacing it.
                self._to_needs_user(
                    f"task {task.id}: the worker repository is not isolated — "
                    + "; ".join(violations)
                    + ". Nothing was executed or committed.",
                    kind="loop_fatal",
                    code="worker_isolation_violation",
                    task_id=task.id,
                    detail="; ".join(violations),
                )
                return
        else:
            worktree_git = GitGateway(Path(execution.worktree_path), self._policy)

        pending_intent = self._intent_store.load(task.id)
        if pending_intent is not None:
            # A previous attempt at this task wrote an intent and then the
            # process died somewhere around the `git commit` it describes.
            # Classify BEFORE doing anything else — see `worktask.py` (F8).
            recon = reconcile_after_crash(
                pending_intent, worktree_git.head_sha(), execution.task_base_sha, worktree_git
            )
            if recon is Reconciliation.AMBIGUOUS:
                # Deliberately do NOT clear the intent: it is the only durable
                # record of what this task's commit attempt was expecting, and
                # leaving it in place means a later re-dispatch reconciles to
                # the SAME AMBIGUOUS verdict rather than silently treating an
                # unresolved situation as a fresh start. Only an operator
                # (inspecting `execution.worktree_path` by hand, then clearing
                # the intent file directly) resolves this.
                state.last_response = None
                self._to_needs_user(
                    f"task {task.id}: crash reconciliation for branch "
                    f"{execution.task_branch} is AMBIGUOUS — the branch tip "
                    "cannot be safely attributed to this task's own commit "
                    "attempt. Nothing was rolled back or overwritten; inspect "
                    f"{execution.worktree_path} by hand, then clear the "
                    "commit intent once resolved.",
                    kind="task_fatal",
                    code="crash_reconciliation_ambiguous",
                    task_id=task.id,
                    detail=f"branch={execution.task_branch} worktree={execution.worktree_path}",
                )
                return
            if recon is Reconciliation.RECOVERABLE:
                # The commit DID happen; only persisting candidate_sha was
                # lost. Adopt the branch head — do NOT commit again. For the
                # AUDIT only (see `is_audit` above), union the recovered
                # round's planned paths into the accumulated ownership set
                # (see `TaskExecution.allowed_paths`) exactly as before M1;
                # for a real task, `allowed_paths` is already the fixed
                # `task.approved_paths` set from creation and must NOT be
                # widened by anything the executor (or a crash-recovered
                # intent, which is itself just a recording of what the
                # executor reported) claims it touched — that self-widening
                # is the exact circularity M1 finding #2/#3 closes.
                if is_audit:
                    execution.allowed_paths = tuple(
                        sorted(set(execution.allowed_paths) | set(pending_intent.planned_paths))
                    )
                execution.candidate_sha = worktree_git.head_sha()
                self._execution_store.save(execution)
                self._intent_store.clear(task.id)
                state.task_execution = asdict(execution)
                self._store.save(state)
                # `_finish_postcommit` stamps the attempt the CRASHED process
                # opened, as the round it really was: the commit happened, so
                # its outcome is the one it earned — `sent_for_review` on the
                # budget it was dispatched against (`task`, or `fault` for a
                # redo, which `_settle_attempt` keeps there). Reaching the
                # reconciliation below instead would have refunded a successful
                # round as an interruption, which is precisely the
                # reclassification that must never occur; the early return here
                # is what prevents it.
                self._finish_postcommit(execution, worktree_git, state, task)
                return
            # NO_COMMIT: the commit this intent describes never happened.
            # Clear the stale intent and fall through to attempt it fresh.
            self._intent_store.clear(task.id)

        # Settle what earlier rounds actually spent BEFORE either ceiling is
        # read, so a task whose process was killed mid-round is judged against
        # the budget that failure really belongs to. Nothing here can touch a
        # round that finished — see the method's own docstring.
        self._reconcile_unfinished_attempts(execution)

        if execution.attempt_count >= MAX_TASK_ATTEMPTS:
            self._to_needs_user(
                f"task {task.id}: {execution.attempt_count} commit attempts on "
                f"{execution.task_branch} without reaching an approved review "
                f"(cap {MAX_TASK_ATTEMPTS}). A structural refusal consumes an "
                "attempt but not a review round, so this ceiling is what stops "
                "unbounded local churn. Nothing was rolled back or pushed.",
                kind="task_fatal",
                code="attempt_count_ceiling",
                task_id=task.id,
                detail=(
                    f"attempt_count={execution.attempt_count} cap={MAX_TASK_ATTEMPTS} "
                    f"branch={execution.task_branch} "
                    f"ledger={','.join(execution.attempt_ledger)}"
                ),
            )
            return
        if execution.fault_attempt_count >= MAX_TASK_FAULT_ATTEMPTS:
            # The other half of the split, and the reason faults can be spared
            # the task budget at all: they are not spared a budget. A task that
            # loses every round to a provider throttle, a killed agent or a
            # process that dies mid-round ends HERE instead of churning on
            # forever, and the blocker says which of the two walls it hit.
            self._to_needs_user(
                f"task {task.id}: {execution.fault_attempt_count} rounds on "
                f"{execution.task_branch} were lost to faults rather than to "
                f"the work itself (cap {MAX_TASK_FAULT_ATTEMPTS}) — provider "
                "throttles, killed agents, or rounds the process did not "
                "survive. None of them was spent from the task's own attempt "
                f"budget ({execution.attempt_count}/{MAX_TASK_ATTEMPTS}); what keeps "
                "failing is the environment around it. Nothing was rolled back "
                "or pushed. The per-attempt record is in the execution's "
                "`attempt_ledger`.",
                kind="task_fatal",
                code="fault_attempt_ceiling",
                task_id=task.id,
                detail=(
                    f"fault_attempt_count={execution.fault_attempt_count} "
                    f"cap={MAX_TASK_FAULT_ATTEMPTS} "
                    f"attempt_count={execution.attempt_count} "
                    f"branch={execution.task_branch} "
                    f"ledger={','.join(execution.attempt_ledger)}"
                ),
            )
            return
        cap = self._policy.config.max_review_rounds
        if cap and execution.review_round >= cap:
            self._park_round_cap(execution, worktree_git, directive, state, task)
            return
        # Unlimited rounds are only safe with this: a reviewer repeating itself
        # verbatim means the executor did not change what was asked, so another
        # round cannot change its own outcome. Same principle as B6 — a retry
        # that cannot alter its result is not a retry.
        if self._revise_feedback_is_unchanged(execution, directive):
            self._park_unchanged_feedback(execution, directive, task)
            return

        # M1 finding #3 (failed-round isolation): a write-capable attempt
        # never starts from a dirty primary checkout, and never resumes a
        # worker repo left dirty by a previous attempt that crashed or failed
        # validation without committing — UNLESS this dispatch already
        # decided, above, to reuse the recorded worker (wrk-01): a resumed
        # round's own uncommitted partial work is what it is resuming, so the
        # reuse decision is passed through and the dirty-residue quarantine
        # does not run for it. Audit-exempt (see `is_audit` above)
        # and worktrees-fallback-exempt (see `_prepare_write_capable_worker`'s
        # own docstring) — scoped to the path production actually uses.
        if self._worker_repos is not None and not is_audit:
            refreshed = self._prepare_write_capable_worker(
                task, execution, worktree_git,
                reused_recorded_worker=reused_recorded_worker,
            )
            if refreshed is None:
                return  # already parked
            worktree_git = refreshed

        # M1 finding #3 (bounded attempts): charged and PERSISTED before the
        # executor ever runs, not after a commit — so a crash, a restart, or a
        # validation failure that never reaches `commit_and_capture` all consume
        # an attempt exactly like a successful one does. (The corresponding
        # increment used to live in `_finish_postcommit`, reached only after a
        # commit; it is gone from there now — see that method.)
        #
        # What `_open_attempt` adds on top of the bare `attempt_count += 1` it
        # replaced is the RECORD: the attempt is written down as open, and the
        # round's own exit says which budget it spent and why (budget-01,
        # 2026-08-17). The charge itself still lands here, before any work.
        self._open_attempt(execution)
        # Remember what this round was asked to change, so the NEXT round can
        # tell "the reviewer wants something new" from "the reviewer is
        # repeating itself". Recorded HERE, at the point the round is actually
        # dispatched, so a round refused before this never poisons the memory.
        if directive.decision is Decision.REVISE and (directive.feedback or "").strip():
            execution.last_revise_feedback = self._normalise_feedback(directive.feedback)
        self._execution_store.save(execution)

        # Snapshot the environment (hooks / push destination) BEFORE the
        # executor runs, so a hook installed mid-task (e.g. by a dependency
        # postinstall script) is caught rather than silently trusted.
        env_snapshot = environment.snapshot(worktree_git)
        # Save before executing: a crash mid-execution resumes in `executing`
        # and re-dispatches the same directive (executors must tolerate that;
        # re-entering here re-loads the same `execution` record above).
        self._store.save(state)
        # THE stage that had no recorded duration at all before prof-01: the
        # implementation agent, minutes per round, plus the validation run that
        # follows it. Started BEFORE the branch below, not inside one of its
        # arms — production always takes the escape-detection path, so a
        # stopwatch started in the `else` would have measured nothing that
        # actually runs. The escape snapshots are inside the window on purpose:
        # they are part of what this round spends to produce a candidate.
        # It is FROZEN the instant either arm returns, not at the `executed`
        # record: `state.last_validation`, the request-id lookup and the
        # payload construction below are the loop writing down what happened,
        # and folding them in would report bookkeeping as agent time.
        execute_watch = self._stopwatch()
        if self._worker_repos is not None and not is_audit:
            # M1 finding #1 (escape detection): bracket the write-capable
            # call with a filesystem snapshot of the PRIMARY checkout. See
            # `escape_detector.py`'s module docstring for the full threat
            # model and why this is detection, checked before commit/review,
            # never prevention.
            outcome = self._execute_with_escape_detection(directive, task)
            execute_watch.stop()
            if outcome is None:
                # Escape detected, already parked. A TASK attempt: the agent
                # wrote outside its worker repository, which is the work
                # misbehaving, not the environment failing it.
                self._finalise_attempt(execution, ATTEMPT_TASK, "checkout_escape_detected")
                return
        else:
            outcome = self._executor.execute(directive, task)
            execute_watch.stop()
        state.last_validation = outcome.validation or "(none)"
        # `request_id` at last: the review request whose directive dispatched
        # this round. Without it `directive` -> `executed` could not be paired
        # by anything, which is why the loop's single most expensive stage had
        # no timing of any kind. Read from `state.last_response` rather than
        # threaded down as a parameter — this method is reached from three
        # dispatch paths and is re-entered verbatim after a crash in
        # `executing`, and on that re-entry the response is exactly what the
        # loop resumed from. Omitted, never invented, when there is none.
        response = state.last_response
        self._log(
            "executed",
            request_id=response.request_id if response is not None else None,
            data=execute_watch.stamp({
                "decision": directive.decision.value,
                "task_id": task.id,
                "status": outcome.status,
                "summary": outcome.summary,
                "validation": outcome.validation,
            }),
        )
        if outcome.status != "ok":
            # THE measured case (exec-01, brw-11): the executor came back
            # having produced nothing because the agent provider threw a
            # session-limit 429, or because the stall supervisor killed it.
            # The reviewer never saw a candidate, and nothing about the task
            # could have prevented it — so that round is charged to the fault
            # budget, and only when the executor POSITIVELY named the cause
            # (`ExecutionOutcome.fault_kind`). A failed validation, an
            # unreadable worker repo and an agent that changed no files all
            # leave it empty and stay task attempts, which is what keeps a
            # task that simply cannot pass its own tests bounded.
            if outcome.fault_kind:
                self._finalise_attempt(execution, ATTEMPT_FAULT, outcome.fault_kind)
            else:
                self._finalise_attempt(execution, ATTEMPT_TASK, "executor_reported_failure")
            state.outbox = TEMPLATES["implementation_review"].render(
                task_id=task.id,
                task_title=task.title,
                decision=directive.decision.value,
                status=outcome.status,
                summary=outcome.summary,
                details=outcome.details,
                validation=outcome.validation or "(none)",
            )
            state.last_response = None
            state.consecutive_failures = 0
            state.phase = Phase.READY.value
            self._store.save(state)
            return

        if not is_audit:
            # M1 finding #2/#3, now ADVISORY (operator decision 2026-08-05).
            # The pre-commit scope check. `outcome.changed_paths` is the
            # executor's own report of what this round wrote.
            #
            # The COMPARISON is unchanged, deliberately: same
            # `unauthorized_paths` matcher, same inputs. `effective_approved_
            # paths`, not `task.approved_paths` — the always-allowed trackers
            # are part of a task's authorization, and this check has to agree
            # with the post-commit one that compares against
            # `execution.allowed_paths`. Using the raw field here flagged a
            # tracker edit the later check would have allowed — two checks,
            # two answers.
            #
            # What changed is only the CONSEQUENCE. This used to park the task
            # (`task_fatal`, `changed_paths_outside_approved`) before
            # `commit_and_capture` ran at all. It parked six rounds in three
            # days, every one of them legitimate work and at least three of
            # them caused by a task scope that was simply guessed wrong when
            # the task was written. A scope declared up front is a PREDICTION
            # of what the work will touch; a wrong prediction is a fact the
            # reviewer should see, not a reason to throw the round away. So
            # the out-of-scope paths are recorded on the execution record and
            # the round proceeds to commit and review, where a human reads
            # them alongside the diff.
            #
            # Recorded from what the comparison produced — never from anything
            # the agent says about its own scope. `execution.allowed_paths` is
            # untouched here and stays derived solely from
            # `task.approved_paths`: this records that authorization was
            # exceeded, it never grants it.
            #
            # Two things this relaxation does NOT reach, both different
            # mechanisms: a task with an EMPTY `approved_paths` is still
            # refused dispatch outright (above — no scope declared still means
            # not dispatchable), and a write that lands OUTSIDE the worker
            # repository entirely is still loop-fatal escape detection
            # (`_execute_with_escape_detection`), which is about confinement,
            # not scope.

            # The cleanup rule's record (scope-04, 2026-08-19). Read FIRST,
            # against `execution.out_of_scope_paths` as it stands BEFORE this
            # round's own overrun is unioned in below — a path this round both
            # created and deleted was never previously recorded, so it is not
            # cleanup and must not be recorded as such.
            #
            # Sourced from git, not from the executor: `dirty_entries_all()` is
            # the same uncommitted status the round is about to stage, so a
            # deletion is git's own account of the tree. The executor DOES
            # report removals in its summary, and that report is a claim like
            # every other one — this is the fact.
            #
            # The status test matches the unmerged `DD`/`AD` shapes too, which
            # cannot occur here (nothing merges into a worker repo mid-round)
            # and would cost nothing if they did: the intersection below is the
            # gate, so the widest a loose status match can be wrong is to
            # under- or over-report the RECORD. It can never authorize a
            # deletion — the deletion has already happened, by an executor that
            # checked the same recorded set before making it.
            #
            # Guarded on the record being non-empty, so the ordinary round —
            # every task that never overran its scope — pays nothing for this:
            # `-uall` is a materially more expensive stat walk (see
            # `GitGateway.dirty_entries_all`), and with nothing recorded there
            # is by definition nothing a deletion could be cleanup OF.
            cleaned: set[str] = set()
            if execution.out_of_scope_paths:
                deleted = {
                    path
                    for status, path in worktree_git.dirty_entries_all()
                    if "D" in status
                }
                cleaned = deleted & set(execution.out_of_scope_paths)
            if cleaned:
                # Union, never a replacement, and `out_of_scope_paths` is
                # deliberately left intact — see
                # `TaskExecution.removed_out_of_scope_paths` for why the two
                # sets overlap on purpose.
                execution.removed_out_of_scope_paths = tuple(
                    sorted(set(execution.removed_out_of_scope_paths) | cleaned)
                )
                self._log(
                    "out_of_scope_paths_removed",
                    data={"task_id": task.id, "removed": sorted(cleaned)},
                )
            outside = unauthorized_paths(
                outcome.changed_paths,
                effective_approved_paths(task.approved_paths, self._tracker_paths()),
            )
            if outside:
                # Union, not replace — see `TaskExecution.out_of_scope_paths`.
                # Persisted by the `save` a few lines below, alongside the
                # executor's report.
                execution.out_of_scope_paths = tuple(
                    sorted(set(execution.out_of_scope_paths) | outside)
                )
                self._log(
                    "changed_paths_outside_approved",
                    data={
                        "task_id": task.id,
                        "outside": sorted(outside),
                        "approved": sorted(task.approved_paths),
                        "advisory": True,
                    },
                )

        # Captured BEFORE the commit, while the outcome is still in hand: this
        # is the executor's own account of the round, and `_finish_postcommit`
        # (which renders the packet) is also reachable by crash-recovery
        # adoption, where no `ExecutionOutcome` exists in this process. Only
        # `summary` survives in the commit message; `details` had nowhere to go
        # at all on the success path.
        execution.report_summary = outcome.summary or ""
        execution.report_details = outcome.details or ""
        # ACCUMULATED, where the two report fields above are REPLACED, and the
        # asymmetry is deliberate. A report describes the round that produced
        # the current candidate. An assumption describes a choice baked into
        # code that is still in `task_base_sha..candidate_sha` — the range the
        # reviewer is being asked to authorize — so a later round assuming
        # nothing must not erase it. `accumulate_assumptions` owns the union
        # rule so this cannot drift into an assignment.
        execution.assumptions = accumulate_assumptions(
            execution.assumptions, outcome.assumptions
        )
        self._execution_store.save(execution)

        message = f"{task.title}\n\n{outcome.summary}".strip()
        parent = worktree_git.head_sha()
        intent = CommitIntent.create(
            task.id, execution.task_branch, parent, outcome.changed_paths, message
        )
        try:
            candidate_sha, staged_summary = worktree_git.commit_and_capture(
                message,
                outcome.changed_paths,
                self._intent_store,
                intent,
                env_snapshot=env_snapshot,
            )
        except EnvironmentDriftError as exc:
            # A hook appeared, core.hooksPath moved, or a url rewrite was added
            # WHILE this task ran. The cause is the shared environment, not this
            # unit of work: quarantining just this task would leave the same
            # condition in place for every task after it, and the loop would
            # march through the whole backlog blocking each one in turn. So this
            # is loop_fatal even though it surfaced inside a task.
            #
            # Charged to the FAULT budget for the same reason it is loop_fatal:
            # the environment moved under the round, the round produced no
            # reviewable outcome, and nothing about this task's work could have
            # avoided it. Note the contrast with the generic handler directly
            # below, which stamps ATTEMPT_TASK: HEAD drift and an empty path
            # list are this unit of work getting it wrong, so they keep spending
            # the task's own budget. The two classifications differ because the
            # two causes do — the same reason the two `except` clauses are
            # separate and ordered, which `test_blockers.py::test_environment_
            # drift_is_loop_fatal_not_a_task_refusal` pins by reading this
            # method's source.
            self._finalise_attempt(execution, ATTEMPT_FAULT, "worker_environment_drift")
            self._intent_store.clear(task.id)
            state.last_response = None
            self._to_needs_user(
                f"task {task.id}: the git environment changed under this task — "
                f"{exc}. Nothing was committed. This affects every task, not "
                "just this one, so the loop is stopping rather than moving on.",
                kind="loop_fatal",
                code="worker_environment_drift",
                task_id=task.id,
                detail=str(exc),
            )
            return
        except GitCommandError as exc:
            # HEAD drift or an empty path list — genuinely scoped to this unit
            # of work. Environment drift is caught above and is NOT task_fatal.
            # Every OTHER refusal in this path parks for the
            # operator, so this one does too rather than escaping as a raw
            # error: the commit did not happen, nothing was rolled back, and a
            # human needs to look at why the task's environment moved.
            self._finalise_attempt(execution, ATTEMPT_TASK, "commit_refused")
            self._intent_store.clear(task.id)
            state.last_response = None
            self._to_needs_user(
                f"task {task.id}: the commit was refused before it happened — "
                f"{exc}. Nothing was committed and nothing was rolled back.",
                kind="task_fatal",
                code="commit_refused",
                task_id=task.id,
                detail=str(exc),
            )
            return
        # Ordering matters for crash safety: commit -> persist candidate_sha
        # -> clear the intent -> THEN verify. If the process dies during
        # verification the commit is already both real and recorded, which is
        # the honest state (the commit exists either way).
        if is_audit:
            # Audit only — see the `is_audit` comment near the top of this
            # method for why a real task's `allowed_paths` must NOT be
            # widened here (that is exactly the circularity M1 finding #2/#3
            # closes; the pre-commit check above RECORDS it on
            # `execution.out_of_scope_paths` when `outcome.changed_paths`
            # leaves `approved_paths`, and still never widens the scope).
            execution.allowed_paths = tuple(
                sorted(set(execution.allowed_paths) | set(outcome.changed_paths))
            )
        execution.candidate_sha = candidate_sha
        self._execution_store.save(execution)
        self._intent_store.clear(task.id)
        state.task_execution = asdict(execution)
        self._store.save(state)
        self._log(
            "staged_diff", data={"task_id": task.id, "candidate_sha": candidate_sha, "summary": staged_summary}
        )
        self._finish_postcommit(execution, worktree_git, state, task)

    def _prepare_write_capable_worker(
        self,
        task: Task,
        execution: TaskExecution,
        worktree_git: GitGateway,
        *,
        reused_recorded_worker: bool = False,
    ) -> GitGateway | None:
        """Preflight for a write-capable (non-audit) dispatch through
        `self._worker_repos`, run once the pending-intent crash check in
        `_dispatch_task_postcommit` has already ruled out a recoverable
        in-flight commit for this exact worktree. Two independent
        fail-closed gates (Autoloop M1 finding #3, failed-round isolation):

          * the PRIMARY checkout (index + working tree) must be clean —
            refused outright (loop_fatal: this is an environmental
            precondition that affects every task, not just this one) rather
            than trusted as a baseline for the escape detector's snapshot
            (`_execute_with_escape_detection`);
          * the WORKER repo must have no uncommitted residue. Any dispatch
            that reaches here without having committed (a crashed agent, a
            validation failure that returned `status="error"` before
            `commit_and_capture` ever ran) leaves files on disk in the SAME
            worker repo `execution.worktree_path` always points at; reusing
            that dirty worktree for the next attempt would let content that
            failed its OWN validation ride along into a LATER round's
            commit — the exact bug reported. Residue is QUARANTINED (moved
            out of `workers_root` via `WorkerRepoManager.quarantine`, never
            deleted — preserved on disk for diagnosis, but no longer
            reachable by any future `create()` for this task id) and a
            fresh worker repo is created from `execution.candidate_sha` (a
            round already committed — a later `revise` still builds on that
            committed work) or `execution.task_base_sha` (nothing has
            committed yet). The fetch SOURCE differs accordingly:
            `candidate_sha` exists only inside the object database we just
            quarantined, so that recreation fetches from the quarantined
            path, not the primary checkout; `task_base_sha` always exists in
            the primary checkout, so that recreation fetches from there as
            usual.

        The ONE exemption from the second gate is
        `reused_recorded_worker=True` (wrk-01): the caller already decided,
        via `worker_repo_is_reusable`, to resume the exact worker the
        execution record names — right path, a git repository, checked out
        on the recorded `task_branch`. That decision means the worker is
        used AS IT STANDS: uncommitted residue there is the interrupted
        round's own partial work being resumed, so quarantining it and
        recreating the repo would discard precisely what the resume exists
        to keep. The flag changes nothing else — the primary-checkout gate
        and the symlink re-check below still run, and a dispatch whose
        recorded worker did NOT pass the reuse probe (or a first dispatch,
        which has no record at all) keeps the quarantine behaviour exactly
        as before.

        Returns the `GitGateway` to actually use — `worktree_git` unchanged
        when it was already clean (or reused as-is), or a fresh one rooted
        at the recreated repo — or `None` if this already parked
        (`_to_needs_user` was called); the caller must return immediately
        without dispatching the executor.
        """
        if self._git.is_dirty():
            self._to_needs_user(
                f"task {task.id}: the primary checkout is not clean (staged "
                "or unstaged changes present) — refusing to start a "
                "write-capable agent. A dirty primary checkout cannot be "
                "used as a trustworthy baseline for detecting whether the "
                "agent wrote outside its worker repository, and this "
                "affects every task, not just this one.",
                kind="loop_fatal",
                code="primary_checkout_dirty",
                task_id=task.id,
                detail=f"dirty={self._git.dirty_files()}",
            )
            return None

        if not reused_recorded_worker and worktree_git.dirty_entries_all():
            label = f"attempt{execution.attempt_count + 1}-{utcnow_iso().replace(':', '')}"
            quarantined_at = self._worker_repos.quarantine(task.id, label)
            self._log(
                "worker_quarantined",
                data={
                    "task_id": task.id,
                    "quarantined_at": str(quarantined_at),
                    "reason": "residual uncommitted state from a prior attempt",
                },
            )
            # `candidate_sha`, if set, names a commit made INSIDE the worker
            # repo we just quarantined — its own local git object database,
            # never pushed or fetched anywhere else. The primary checkout
            # does not have that object, so fetching it from
            # `self._git.repo_root` fails outright. Fetch from the
            # quarantined copy instead (still a real, valid git repo on
            # disk — `quarantine()` moves it, never deletes it — so the
            # object is reachable there). Only `task_base_sha` — which by
            # construction always exists in the primary checkout — uses the
            # primary checkout as the fetch source.
            if execution.candidate_sha:
                resume_sha = execution.candidate_sha
                fetch_source = quarantined_at
            else:
                resume_sha = execution.task_base_sha
                fetch_source = self._git.repo_root
            repo = self._worker_repos.create(task.id, fetch_source, resume_sha)
            worktree_git = GitGateway(repo.path, self._policy, env=worker_env())
            violations = verify_worker_isolation(worktree_git)
            if violations:
                self._to_needs_user(
                    f"task {task.id}: the freshly recreated worker repository "
                    "is not isolated — " + "; ".join(violations) + ". Nothing "
                    "was executed.",
                    kind="loop_fatal",
                    code="worker_isolation_violation",
                    task_id=task.id,
                    detail="; ".join(violations),
                )
                return None

        # v1 task-scope hardening: `task.approved_paths` is validated as a
        # STRING by `tasks._validate_approved_path` (no globs, no '..', not
        # absolute) when the task was planned/seeded, but that check has no
        # filesystem awareness by design. Re-checked HERE, against the
        # worker repo's actual on-disk content, immediately before a
        # write-capable agent runs: a path that looks like an ordinary
        # relative file can still traverse a symlink that already exists at
        # or above it, writing through to somewhere outside the repository
        # entirely.
        symlink_violations = escape_detector.find_symlink_traversal(
            worktree_git.repo_root, task.approved_paths
        )
        if symlink_violations:
            self._to_needs_user(
                f"task {task.id}: approved path(s) traverse a symlink — "
                + "; ".join(symlink_violations)
                + ". Nothing was executed.",
                kind="task_fatal",
                code="approved_path_symlink_traversal",
                task_id=task.id,
                detail="; ".join(symlink_violations),
            )
            return None
        return worktree_git

    def _execute_with_escape_detection(
        self, directive: Directive, task: Task
    ) -> ExecutionOutcome | None:
        """Wraps a write-capable executor call with the Autoloop M1 escape
        DETECTOR — a deterministic filesystem snapshot of the PRIMARY
        checkout taken immediately before and immediately after the call,
        nothing wider. See `escape_detector.py`'s module docstring for the
        full threat model (detection, not prevention) and for why bracketing
        exactly this call — rather than the whole task dispatch — is what
        makes an explicit exclusion list for Autoloop's own volatile files
        (state.json, the blocker/execution/intent stores, ...) unnecessary:
        nothing in this orchestrator writes any of them between the two
        snapshots below. The single exemption that DOES exist —
        `escape_detector.is_derived_bytecode`, for `__pycache__/*.pyc`
        entries that carry a tag some interpreter really emits and whose `.py`
        source is itself snapshotted AS A REGULAR FILE — is argued there too;
        it is
        what stops an out-of-band `import autoloop.…` (a dashboard restart, a
        `health --json` poll) from parking this round as tampering.

        The ONE injected exemption alongside it is
        `_operator_priority_exemption` — an operator re-prioritising a task
        from the dashboard writes `.autoloop/tasks.json` immediately, which
        this window would otherwise report as an agent escape. Read that
        method and `tasks.MutationLedger` before touching it: it silences a
        change only when the ledger OUTSIDE the checkout attests the loop's own
        `TaskStore` made it AND the bytes differ in nothing but `priority`.

        Returns the outcome, or `None` if an escape was detected — already
        parked (loop_fatal: the isolation mechanism itself may be
        compromised) — in which case the caller must return immediately
        without committing anything.
        """
        # The task file's mutex is an always-empty coordination file inside the
        # state dir. Created here, before the "before" snapshot, so an operator
        # edit that has to take it mid-round finds it already there: an
        # identical empty file on both sides is not a violation and needs no
        # exemption at all. See `TaskStore.ensure_mutex_file`.
        self._task_store.ensure_mutex_file()
        exempt = self._operator_priority_exemption()
        paths_before = escape_detector.enumerate_checkout_paths(self._git)
        before = escape_detector.snapshot_checkout(self._git.repo_root, paths_before)
        outcome = self._executor.execute(directive, task)
        # Re-enumerate rather than re-snapshotting the SAME path list: a
        # brand-new file the agent created would not appear in
        # `paths_before` at all, so "created outside the worker repo" could
        # never be detected without a fresh enumeration here too.
        paths_after = escape_detector.enumerate_checkout_paths(self._git)
        after = escape_detector.snapshot_checkout(
            self._git.repo_root, sorted(set(paths_before) | set(paths_after))
        )
        violations = escape_detector.diff_snapshots(before, after, exempt=exempt)
        if not violations:
            # An operator may have steered the queue while the agent ran. The
            # registry this process holds in memory predates that edit, so
            # adopt it now: the ordering `next_ready()` uses is then the one on
            # disk, and the next ordinary save cannot write the stale value
            # back over it. `TaskStore.save` reconciles too — this is the same
            # single implementation, called early enough to affect this round.
            adopted = self._task_store.reconcile_priorities(self._registry)
            if adopted:
                self._log("operator_priority_adopted", data={"tasks": adopted})
        if violations:
            self._to_needs_user(
                f"task {task.id}: the write-capable agent changed the "
                "PRIMARY checkout outside its worker repository — "
                + "; ".join(violations)
                + ". This is LOOP-FATAL: the isolation mechanism itself may "
                "be compromised. Nothing was committed. This is DETECTION, "
                "not prevention — the change already happened and is NOT "
                "reverted automatically; inspect and resolve it by hand.",
                kind="loop_fatal",
                code="checkout_escape_detected",
                task_id=task.id,
                detail="; ".join(violations),
            )
            return None
        return outcome

    def _operator_priority_exemption(self):
        """The `diff_snapshots` predicate that lets an operator re-prioritise a
        task while a write-capable agent is running — and nothing else.

        `None` (i.e. no exemption at all, the pre-2026-08-16 behaviour) whenever
        the exemption cannot be justified: no ledger configured on the task
        store, or a task file that is not inside the observed checkout, in which
        case a change to it is not this detector's business anyway.

        Captures the task file's BYTES and the mutation ledger's WATERMARK up
        front, before the agent runs, and does so under the task file's own
        mutex so the pair describes one instant.

        The bytes are the "what changed" evidence: the snapshot records only a
        digest, and a digest cannot answer "did this differ in nothing but
        `priority`". They are checked against the snapshot's own
        `content_sha256` before being used, so a file that changed between this
        read and the snapshot cannot be compared against the wrong baseline.

        The watermark is what makes the attestation about THIS window: only
        completed mutations recorded after it can authorize anything, so a
        priority edit from an earlier round — including one whose intermediate
        state a later direct edit might reproduce — proves nothing here. Read
        under the same lock as the bytes for the reason `capture_priority_window`
        gives: taken separately they can straddle an in-flight edit and turn a
        benign operator write into a loop-fatal park.

        Deliberately narrow, item by item:
          * one path — the task file, spelled as the snapshot spells it;
          * never a creation or a deletion (`prior`/`current` present), and
            never a shape change: an operator edit rewrites an existing plain
            file, so anything else is reported;
          * never an executable-bit change, which no `os.replace` of a JSON
            document produces;
          * and then `TaskStore.attested_priority_edit`, which is where the two
            real questions (who wrote it, what it changed) are answered.
        """
        store = self._task_store
        if getattr(store, "ledger", None) is None:
            return None
        try:
            relative = str(
                Path(store.path).resolve().relative_to(Path(self._git.repo_root).resolve())
            )
        except (ValueError, OSError):
            return None  # task file outside the checkout: not snapshotted
        try:
            baseline, watermark = store.capture_priority_window()
        except (OSError, StateError):
            # No baseline, or the mutex could not be taken: there is no
            # exemption to offer, so every change to the file is reported. Same
            # direction as no ledger at all.
            return None

        def exempt(path: str, prior, current) -> bool:
            if path != relative or prior is None or current is None:
                return False
            if prior.kind != "file" or current.kind != "file":
                return False
            if prior.executable != current.executable:
                return False
            return store.attested_priority_edit(
                baseline, prior.content_sha256 or "", current.content_sha256 or "",
                watermark,
            )

        return exempt

    def _park_round_cap(
        self,
        execution: TaskExecution,
        worktree_git: GitGateway,
        directive: Directive,
        state: LoopState,
        task: Task,
    ) -> None:
        """Round 3 never reaches the executor or ChatGPT. Presents BOTH the
        full accumulated diff and the latest round's own diff (parent =
        the previous round's tip, derived from `commit_list` rather than a
        separately persisted field, since each round is exactly one commit),
        so a human can see the whole arc and the most recent change without
        re-deriving either by hand."""
        state.last_response = None
        base, candidate = execution.task_base_sha, execution.candidate_sha
        commits = worktree_git.commit_list(base, candidate)
        previous_tip = commits[-2]["sha"] if len(commits) >= 2 else base
        try:
            full_diff = worktree_git.range_diff(base, candidate)
        except GitCommandError as exc:
            full_diff = f"(unavailable: {exc})"
        try:
            latest_diff = worktree_git.range_diff(previous_tip, candidate)
        except GitCommandError as exc:
            latest_diff = f"(unavailable: {exc})"
        self._to_needs_user(
            f"task {task.id}: review round cap (2) reached on branch "
            f"{execution.task_branch} at candidate {candidate[:12]} — a third "
            "revision round is never sent to ChatGPT. Latest feedback: "
            f"{directive.feedback or '(none — last directive was not revise)'}"
            f"\n\n--- full diff {base[:12]}..{candidate[:12]} ---\n{full_diff}"
            f"\n\n--- latest round diff {previous_tip[:12]}..{candidate[:12]} ---"
            f"\n{latest_diff}",
            kind="task_fatal",
            code="review_round_cap",
            task_id=task.id,
            detail=f"branch={execution.task_branch} candidate={candidate}",
        )

    @staticmethod
    def _normalise_feedback(text: str) -> str:
        """Collapse whitespace and case so trivial re-wording does not read as
        a different request. Deliberately NOT fuzzy: two genuinely different
        complaints should always be allowed to run another round."""
        return " ".join((text or "").split()).strip().lower()

    def _revise_feedback_is_unchanged(self, execution: TaskExecution, directive: Directive) -> bool:
        if directive.decision is not Decision.REVISE:
            return False
        current = self._normalise_feedback(directive.feedback or "")
        if not current:
            return False
        return current == execution.last_revise_feedback

    def _park_unchanged_feedback(self, execution: TaskExecution, directive: Directive, task: Task) -> None:
        self._to_needs_user(
            f"task {task.id}: the reviewer asked for the same change twice in a "
            f"row (round {execution.review_round}) — the executor did not alter "
            "what was asked, so a further round cannot change the outcome. "
            "Nothing was dispatched. Read the feedback and either scope the task "
            "differently or fix it by hand.\n\nRepeated feedback: "
            f"{directive.feedback}",
            kind="task_fatal",
            code="review_feedback_unchanged",
            task_id=task.id,
            detail=f"round={execution.review_round} candidate={execution.candidate_sha}",
        )

    def _verify_committed(
        self, execution: TaskExecution, worktree_git: GitGateway
    ) -> tuple[list[str], str]:
        """The post-commit review gate for `execution.candidate_sha`. Returns
        `(failures, validation_summary)`; `failures` empty means the commit
        passes every blocking check.

        Path ownership is compared against `execution.allowed_paths` — the
        UNION of every round's `changed_paths` committed so far, not just the
        latest round's. `commit_range_paths(task_base_sha, candidate_sha)`
        spans the WHOLE range once `review_round > 0`, so comparing it against
        only the latest round's paths would wrongly flag an earlier round's
        legitimate paths as "outside" on a second review.

        That one comparison is ADVISORY (2026-08-05): it MUTATES
        `execution.out_of_scope_paths` rather than adding to `failures`, and
        the caller persists the record immediately after. Every other check
        here still refuses.
        """
        failures: list[str] = []
        candidate = execution.candidate_sha
        if not worktree_git.is_descendant(candidate, execution.task_base_sha):
            failures.append(
                f"candidate {candidate[:12]} is not a descendant of task base "
                f"{execution.task_base_sha[:12]}"
            )
        touched = worktree_git.commit_range_paths(execution.task_base_sha, candidate)
        if not touched:
            failures.append("commit range is empty — nothing was actually committed")
        # Same matcher as the pre-commit check, so a directory prefix means the
        # same thing at both ends. Set subtraction here would have flagged
        # after the commit what was accepted before it.
        #
        # ADVISORY since 2026-08-05, for the same reason and by the same
        # operator decision as the pre-commit one — and it had to change in
        # the SAME breath. Relaxing only the pre-commit check would have moved
        # the park downstream rather than removing it: the round would commit,
        # arrive here, and park as `post_commit_verification_failed` for the
        # very same paths. So this no longer appends to `failures`; it records
        # onto the execution record and the candidate goes to review.
        #
        # The comparison itself is untouched, against `execution.allowed_paths`
        # exactly as before. This side reads git's own
        # `commit_range_paths(task_base_sha, candidate_sha)` — the authoritative
        # account of what the commits actually touched, not the executor's
        # report — which is why it also catches what the pre-commit side
        # structurally cannot: a path added by a commit hook strictly after
        # that check ran (`test_hook_adding_unexpected_path_is_recorded_not_
        # refused`, which is why that test is the sharpest proof this site
        # went advisory too — site 1 never sees the path at all). It now
        # surfaces in the packet instead of parking. Every OTHER failure —
        # ancestry, an empty range, a dirty worktree, failing validation,
        # validation mutating the tree — is untouched and still refuses.
        outside = unauthorized_paths(touched, execution.allowed_paths)
        if outside:
            # Union, not replace: the range spans every round, and the
            # pre-commit side may already have recorded these. Saved by
            # `_finish_postcommit`, which persists `execution` right after this
            # returns.
            execution.out_of_scope_paths = tuple(
                sorted(set(execution.out_of_scope_paths) | outside)
            )
        residual = worktree_git.dirty_entries()
        if residual:
            failures.append(
                "worktree is not clean after commit — residual change(s): "
                + ", ".join(f"{status} {path}" for status, path in residual)
            )
        # Bracket the validation run. The residual-dirty check above cannot
        # cover this: it runs BEFORE validation, and it is a `git status`
        # check, so it is blind to ignored paths — exactly where a test that
        # dumped its configuration would land. Validation now receives real
        # (test-only) database credentials, so "a test wrote a credential into
        # the tree" is a concrete leak path, and it stays a refusal even when
        # the file it wrote is one the task was approved to touch: approval
        # authorises the AGENT to edit a path, never validation to mutate one.
        # One class is exempt on both sides, via the same
        # `escape_detector.is_derived_bytecode` rule the checkout detector
        # uses: the `__pycache__` entries any `pytest` run compiles from
        # sources already in the tree. Nothing here declares them harmless —
        # their `.py` sources are still diffed byte for byte — and the wording
        # below says so rather than claiming validation wrote nothing at all.
        tree_before = escape_detector.snapshot_worker_tree(worktree_git)
        # `touched` is git's own account of the commit range — the same value
        # the scope check above uses, and the only honest input for deciding
        # which tests this candidate needs. Never `outcome.changed_paths`: a
        # report naming fewer files than it changed would choose its own
        # validation.
        validation_ok, validation_summary = self._run_post_commit_validation(
            execution, touched
        )
        mutations = escape_detector.diff_worker_tree(
            tree_before, escape_detector.snapshot_worker_tree(worktree_git)
        )
        if not validation_ok:
            failures.append(f"post-commit validation failed: {validation_summary}")
        if mutations:
            # Paths only — `diff_worker_tree` never reports contents, so this
            # is safe to park with even when what was written was a secret.
            # The mutated files are left on disk, uncommitted: the candidate is
            # refused, and the worker repo is the evidence.
            failures.append(
                "validation MUTATED the worker tree beyond its own bytecode "
                "cache (validation must read, never write; the candidate is "
                "refused and the worker repo is preserved uncommitted as "
                "evidence): " + "; ".join(sorted(mutations))
            )
        return failures, validation_summary

    def _run_post_commit_validation(
        self, execution: TaskExecution, changed_paths: Iterable[str] = ()
    ) -> tuple[bool, str]:
        """Re-run the task's OWN declared validation against its worktree,
        AFTER the commit exists.

        Pre-commit validation (`outcome.validation`, whatever the executor
        itself reports) is not sufficient: a commit hook can change committed
        content in ways the executor never saw. That is only a real check if
        it re-runs the SAME commands — which is what
        `execution.validation_commands` (persisted at dispatch from
        `Task.validation`) provides. It previously re-ran
        `config.audit.validation_commands` instead, so a task that declared
        its own validation precisely because the default does not cover its
        change had the reviewed commit graded by the default anyway
        (`docs/AUTOLOOP_TODO.md` B4b).

        Falls back to the configured default only when the task declared
        nothing — identical to `ImplementExecutor`'s own
        `tuple(task.validation) or self._validation_commands`, so the two ends
        of the check agree by construction rather than by coincidence. The
        audit path declares none and therefore still runs the default.

        **Which TESTS the configured commands run is narrowed here**, by
        `validation.select_validation_commands`, from `changed_paths` — git's
        own account of the commit range, passed in by `_verify_committed`. The
        model and its widening rules live in that module; what this method owns
        is the two cases where selection is refused outright, and both are the
        same principle:

        * **A task that declared its own `validation` is never narrowed.** That
          list exists because the default does not cover the change, so it is
          taken literally. It is also the per-task way to demand a full run.
        * **A declared `validation_cwd` is never narrowed.** Selection resolves
          repo-relative changed paths against the repo root; a command running
          from a subdirectory takes paths relative to THAT directory, and the
          two would not line up. The backend suite is exactly this case.

        **This is the only call site that narrows.** A round validates twice,
        and the OTHER run — `ImplementExecutor`'s own, before the commit — still
        executes every configured command in full. That is a scope boundary, not
        a design one: the selector takes a command list, changed repo-relative
        paths and a repo root, all three of which that call site already holds
        (`sorted(git.dirty_paths_all())` and `git.repo_root`, read a few lines
        above its `run_validation_commands` call); adopting it there is a change
        to `implement_executor.py` plus one constructor argument threaded from
        `cli._build_executor`, neither of which was in val-02's approved paths.
        Until that lands, a round costs one full suite plus one narrowed suite,
        and `validation.PRECOMMIT_EVIDENCE` tells the reviewer exactly that
        rather than letting a narrowed summary read as the whole story.

        Whatever it decides is APPENDED to the returned summary, which becomes
        `state.last_validation` and reaches the reviewer in the CONTEXT block of
        the review message — the place validation evidence has always been read.
        A run that narrowed and could not say so is the evidence gap that gets a
        packet refused; a run with no pytest command to narrow says nothing,
        because there was no decision to report.
        """
        commands = execution.validation_commands or self._config.audit.validation_commands
        cwd = Path(execution.worktree_path)
        full_reason = ""
        if execution.validation_commands:
            full_reason = (
                "this task declares its own validation commands, which are run "
                "exactly as declared and never narrowed"
            )
        if execution.validation_cwd:
            cwd = cwd / execution.validation_cwd
            full_reason = (
                f"validation runs from {execution.validation_cwd!r}, not the repo "
                "root, so repo-relative reachability does not apply"
            )
            if not cwd.is_dir():
                # Refuse rather than silently validating the repo root: the
                # declared directory not existing in the COMMITTED tree is
                # itself a failure of the change under review.
                return False, (
                    f"declared validation_cwd {execution.validation_cwd!r} does not "
                    "exist in the committed worker repo"
                )
        selection = select_validation_commands(
            commands,
            tuple(changed_paths),
            Path(execution.worktree_path),
            mode=self._config.audit.test_selection,
            full_reason=full_reason,
        )
        passed, summary = run_validation_commands(
            selection.commands,
            cwd,
            command_runner=self._validation_runner,
            validation_env=self._validation_env,
        )
        evidence = selection.evidence()
        return passed, f"{summary}; {evidence}" if evidence else summary

    def _finish_postcommit(
        self,
        execution: TaskExecution,
        worktree_git: GitGateway,
        state: LoopState,
        task: Task,
    ) -> None:
        # `attempt_count` is NOT incremented here (M1 finding #3) — it is
        # incremented and persisted BEFORE the executor ever runs (see
        # `_dispatch_task_postcommit`), so a validation failure or a crash
        # that never reaches this method still consumes an attempt. This
        # method is reached either right after a fresh commit (that
        # increment already happened earlier in the SAME call) or via
        # crash-recovery adoption (the increment happened in the EARLIER,
        # crashed process, before ITS executor call) — either way the count
        # on disk is already correct and must not be bumped again here.
        #
        # What this method DOES do to the attempt record is STAMP it. All three
        # of its exits report a TASK outcome, and every one of them does so on
        # purpose:
        #
        #   * post-commit verification failed — a structural refusal. The
        #     reviewer never saw it, and it is charged anyway: it is a genuine
        #     defect in the candidate this task produced, and repeating it is
        #     exactly the local churn `MAX_TASK_ATTEMPTS` bounds.
        #   * the review packet could not be built — a git failure, but scoped
        #     to this candidate (an oversized range-diff is a property of what
        #     was committed). Fail closed: not positively an environmental
        #     fault, so the task's own budget pays.
        #   * the packet went out — the round produced work and reached the
        #     reviewer. This is the case `attempt_count` was invented for.
        #
        # A round the loop opened on the FAULT budget (a redo of a review some
        # session-ending fault destroyed) reports the same three outcomes and is
        # settled by the same rule in `_settle_attempt`: reaching the reviewer
        # keeps it on the fault budget, because that is the review it was sent
        # to recover; either refusal moves it back onto the task's, because a
        # refusal is a fresh defect and a redo must not launder one.
        #
        # The crash-recovery adoption path lands here too, and that matters:
        # the attempt the DEAD process opened is stamped `task` here, so the
        # reconciliation in `_dispatch_task_postcommit` can never later see it
        # as unfinished and refund a round that genuinely committed.
        failures, validation_summary = self._verify_committed(execution, worktree_git)
        state.last_validation = validation_summary
        # `review_round` counts REVIEWS, not commit attempts. It is incremented
        # only where a packet is actually sent (below), never here: a structural
        # refusal (residual dirty file, failing post-commit validation, a hook
        # that added a path) parks without any packet reaching ChatGPT, so
        # charging it against a two-round review budget would exhaust that
        # budget without a single review having happened — and would report the
        # confusing "out of review rounds" when nothing was ever reviewed. A
        # structural refusal is already fail-closed on its own.
        execution.candidate_commit_count = len(
            worktree_git.commit_list(execution.task_base_sha, execution.candidate_sha)
        )
        self._execution_store.save(execution)
        state.task_execution = asdict(execution)
        self._log(
            "postcommit_review",
            data={
                "task_id": task.id,
                "candidate_sha": execution.candidate_sha,
                "review_round": execution.review_round,
                "candidate_commit_count": execution.candidate_commit_count,
                "failures": failures,
                # Advisory, so it is deliberately NOT in `failures` — but it
                # belongs in the transcript either way, so an operator reading
                # the log sees a scope overrun without opening the packet.
                "out_of_scope_paths": list(execution.out_of_scope_paths),
            },
        )
        state.last_response = None
        if failures:
            self._finalise_attempt(
                execution, ATTEMPT_TASK, "post_commit_verification_failed"
            )
            self._to_needs_user(
                f"task {task.id}: commit {execution.candidate_sha[:12]} on "
                f"{execution.task_branch} (round {execution.review_round}) was "
                "created but REFUSED at post-commit review. The commit is NOT "
                "rolled back and NOT pushed — reset/checkout/clean are not "
                f"reachable through this gateway. Reasons: {'; '.join(failures)}",
                kind="task_fatal",
                code="post_commit_verification_failed",
                task_id=task.id,
                detail="; ".join(failures),
            )
            return
        # A packet that exceeds `range_diff`'s byte cap (or any other git
        # failure while rendering it) parks here, not via the generic
        # GitError/budget path: the commit already exists, nothing here can
        # roll it back, and no amount of re-prompting ChatGPT changes that —
        # the same "park and report, never undo" rule as every other refusal
        # in this method.
        try:
            packet_text, packet_diff = build_review_packet_with_diff(
                execution, worktree_git, task
            )
        except GitCommandError as exc:
            self._finalise_attempt(execution, ATTEMPT_TASK, "review_packet_build_failed")
            self._to_needs_user(
                f"task {task.id}: commit {execution.candidate_sha[:12]} on "
                f"{execution.task_branch} (round {execution.review_round + 1}) "
                "passed post-commit review, but the review packet could not be "
                f"built — {exc}. The commit is NOT rolled back and NOT pushed; "
                "nothing was sent to ChatGPT.",
                kind="task_fatal",
                code="review_packet_build_failed",
                task_id=task.id,
                detail=str(exc),
            )
            return
        # Only here — a packet exists and is about to become `outbox`. A packet
        # that could not be built consumed no review round either.
        execution.review_round += 1
        # Stamped BEFORE the save below, so the round's classification and the
        # review round it earned reach disk together. `sent_for_review` is also
        # the outcome `_note_round_fault` looks for: a session that then dies on
        # a fault destroyed a review this task had already earned. That holds
        # whichever budget this round was charged to — a redo of a lost review
        # settles as `fault|<origin>>sent_for_review` and is recognised the
        # same, so consecutive faults keep landing on the fault budget.
        self._finalise_attempt(execution, ATTEMPT_TASK, REASON_SENT_FOR_REVIEW)
        self._execution_store.save(execution)
        state.task_execution = asdict(execution)
        state.outbox = TEMPLATES["postcommit_review"].render(
            task_id=task.id, task_title=task.title, packet=packet_text
        )
        # The patch, carried alongside the payload it is already inside, so
        # `_step_ready` can plan a chunked delivery for it without re-reading
        # git. Only when it is actually too large for one message: an ordinary
        # packet leaves this `None` and takes exactly the path it always did.
        state.outbox_diff = (
            packet_diff if len(packet_diff.strip()) > DIFF_INCLUDE_MAX_CHARS else None
        )
        state.consecutive_failures = 0
        state.phase = Phase.READY.value
        self._store.save(state)

    @staticmethod
    def _candidate_is_on_task_line(
        worktree_git: GitGateway, candidate: str, execution: TaskExecution
    ) -> bool:
        """Is `candidate` a commit on THIS task's own line of history, with the
        recorded base already integrated into it? The pre-push authorization
        that stops an approval publishing something built somewhere else.

        The direct form — the base is an ancestor of the candidate — is the
        answer for every candidate produced by an ordinary round, and is tried
        first and unchanged.

        The second form exists for a candidate CARRIED PAST a moved base
        (`_carry_reviewed_candidate_past`). There the recorded base is a
        mainline head that was merged INTO the task branch, so the reviewed
        candidate is that merge's first parent rather than its descendant, and
        the direct question answers "no" for work that is perfectly sound.
        Asking the branch TIP instead restores exactly the same guarantee from
        the other side: the tip must contain the candidate (so the approved
        commit really is on this task's branch) AND contain the base (so the
        base really was integrated, not bypassed). Both, or this refuses —
        neither half is sufficient alone, and a tip that contains only one of
        them is precisely the unrelated-history case the check exists for.

        THE TIP IS READ AT PUSH TIME, WHICH IS LATER THAN APPROVAL TIME, and
        that is only safe because of a check twenty lines up. A later round
        commits on top of the merge and the tip advances, so on its own the
        second form would accept any candidate ever committed on this branch —
        a SUPERSEDED one included. `_dispatch_task_push` refuses unconditionally
        before reaching here unless `execution.candidate_sha` is still exactly
        the approved sha (`push_candidate_stale`), so a superseded candidate
        never gets this far. Do not reorder those two, and do not reuse this
        helper anywhere that check has not already run.
        """
        base = execution.task_base_sha
        if worktree_git.is_descendant(candidate, base):
            return True
        tip = worktree_git.head_sha()
        return worktree_git.is_descendant(tip, candidate) and worktree_git.is_descendant(tip, base)

    def _dispatch_task_push(self, directive: Directive, resp: LastResponse) -> None:
        """Publish a produce-then-review candidate via `push_exact`.

        `directive` is used ONLY for logging/reason text — never for identity.
        A `push` directive cannot carry a task_id at all (`contract._forbid`
        rejects it at parse time), so `resp.postcommit` — the binding
        captured the moment THIS packet was sent, not a fresh
        `TaskExecutionStore` lookup — is the only source of which task and
        which candidate sha this approval concerns. That is what makes
        swapping the candidate underneath an approval a REFUSAL rather than a
        silent wrong-commit publish: if `TaskExecutionStore` now disagrees
        with the binding, or the recorded candidate no longer resolves, or
        its tree no longer matches what was reviewed, nothing is pushed.

        No `env_snapshot` is passed to `push_exact` here — pass 2a/2b persist
        no environment snapshot across the review round-trip (only within a
        single `commit_and_capture` call), so there is nothing honest to
        compare against. `push_exact`'s own unconditional checks (active
        push hooks, `insteadOf`/`pushurl` presence) still run regardless.

        **Autoloop M2 routing.** When `self._publisher` is set, publication
        goes through it instead of pushing directly from the task's own
        worktree: `Publisher.import_candidate` fetches `binding.candidate_sha`
        (and ONLY that literal object id — never worktree HEAD, never a ref)
        from `execution.worktree_path` into the publisher's own,
        SEPARATE repository, verifies the imported object id and type, and
        `Publisher.publish` then calls the exact same `GitGateway.push_exact`
        the worktree branch below calls — reused, not reimplemented — rooted
        at the publisher repo instead of the worktree. `binding.candidate_sha`
        remains the ONLY source of what gets published in either branch; this
        method never reads a fresh "current" sha from anywhere once the
        binding checks above have passed.

        **Publisher URL drift (v1 policy).** When `self._publisher` is set,
        the main checkout's LIVE `remote.<remote>.url` is compared against
        `self._publisher_url_snapshot` (captured at provisioning time, never
        auto-updated — see `publisher.provision_publisher_repo`) BEFORE
        anything else runs. A mismatch means the main checkout's origin
        changed since the publisher was last (re)provisioned; this refuses
        and parks rather than publishing to the stale snapshotted
        destination, naming the exact operator command
        (`reprovision-publisher --confirm`) that is the ONLY way to update
        it. `Publisher.publish` is ALSO given `expected_url=` the same
        snapshot as belt-and-braces (`push_exact` re-checks it against the
        PUBLISHER repo's own config immediately before pushing), but that
        second check cannot see the main-checkout-vs-snapshot drift this one
        exists for — the publisher repo's own config only ever reflects the
        snapshot itself (`provision_publisher_repo` re-asserts it on every
        call), never the main checkout's current, possibly-drifted value.
        """
        state = self.state
        binding = resp.postcommit
        if self._publisher is not None:
            live_url = self._git.config_get(f"remote.{self._publisher.remote}.url")
            if live_url != self._publisher_url_snapshot:
                self._to_needs_user(
                    f"task {binding.task_id}: push refused — the publisher's "
                    f"remote url snapshot ({redact_url(self._publisher_url_snapshot or '')}) "
                    "no longer matches the main checkout's configured "
                    f"remote.{self._publisher.remote}.url "
                    f"({redact_url(live_url)}). Autoloop never updates the "
                    "snapshot automatically. Verify the new destination is "
                    "correct, then run `python -m autoloop reprovision-publisher "
                    "--confirm`. Nothing was published.",
                    kind="loop_fatal",
                    code="publisher_url_drift",
                    task_id=binding.task_id,
                    detail=(
                        f"snapshot={redact_url(self._publisher_url_snapshot or '')} "
                        f"live={redact_url(live_url)}"
                    ),
                )
                return
        execution = self._execution_store.load(binding.task_id)
        if execution is None or execution.candidate_sha != binding.candidate_sha:
            self._to_needs_user(
                f"task {binding.task_id}: push refused — the reviewed candidate "
                f"{binding.candidate_sha[:12]} is no longer this task's current "
                "candidate (a later round advanced it, or the execution record "
                "is gone). Nothing was pushed; re-review the current state "
                "before approving again.",
                kind="loop_fatal",
                code="push_candidate_stale",
                task_id=binding.task_id,
                detail=f"approved={binding.candidate_sha}",
            )
            return
        worktree_git = GitGateway(Path(execution.worktree_path), self._policy)
        if not self._candidate_is_on_task_line(worktree_git, binding.candidate_sha, execution):
            self._to_needs_user(
                f"task {binding.task_id}: push refused — candidate "
                f"{binding.candidate_sha[:12]} is not a descendant of task base "
                f"{execution.task_base_sha[:12]}. Nothing was pushed.",
                kind="loop_fatal",
                code="push_not_descendant",
                task_id=binding.task_id,
                detail=f"candidate={binding.candidate_sha} base={execution.task_base_sha}",
            )
            return
        try:
            info = worktree_git.read_commit(binding.candidate_sha)
        except GitCommandError as exc:
            self._to_needs_user(
                f"task {binding.task_id}: push refused — the reviewed candidate "
                f"{binding.candidate_sha[:12]} no longer resolves: {exc}. Nothing "
                "was pushed.",
                kind="loop_fatal",
                code="push_candidate_unresolvable",
                task_id=binding.task_id,
                detail=str(exc),
            )
            return
        if info.get("tree") != binding.candidate_tree_sha:
            self._to_needs_user(
                f"task {binding.task_id}: push refused — candidate "
                f"{binding.candidate_sha[:12]}'s tree changed since it was "
                f"reviewed (was {binding.candidate_tree_sha[:12]}, now "
                f"{info.get('tree', '?')[:12]}). Nothing was pushed.",
                kind="loop_fatal",
                code="push_tree_mismatch",
                task_id=binding.task_id,
                detail=f"reviewed_tree={binding.candidate_tree_sha} now={info.get('tree', '?')}",
            )
            return

        remote = self._publisher.remote if self._publisher is not None else (
            execution.intended_remote or "origin"
        )
        dest_ref = f"refs/heads/{binding.task_branch}"
        # Durable push intent, recorded BEFORE the network call — mirrors
        # `CommitIntent`'s "write before the risky operation" pattern, so a
        # crash between a successful `git push` and this method returning is
        # recoverable from the remote ref alone rather than re-pushed.
        execution.intended_remote = remote
        execution.intended_remote_ref = dest_ref
        self._execution_store.save(execution)

        landed = (
            self._publisher.remote_ref_sha(dest_ref)
            if self._publisher is not None
            else worktree_git.remote_ref_sha(remote, dest_ref)
        )
        if landed != binding.candidate_sha:
            # Same `allow_protected_push` gating as the legacy path below —
            # `authorize_directive` already decided whether
            # `resp.postcommit.task_branch` being in `protected_branches` is
            # allowed (using that same flag); `push_exact`'s own protected-ref
            # check must agree, or `allow_protected_push=True` would silently
            # be inert for this path too.
            gateway_protected = (
                () if self._policy.config.allow_protected_push
                else self._policy.config.protected_branches
            )
            try:
                if self._publisher is not None:
                    # Import first: bring the EXACT reviewed object into the
                    # publisher's own, separate object database from the
                    # worker's worktree, by literal sha — never a ref, never
                    # worker HEAD. `import_candidate` itself re-verifies the
                    # imported object id and that it is a commit.
                    self._publisher.import_candidate(
                        execution.worktree_path, binding.candidate_sha
                    )
                    landed = self._publisher.publish(
                        binding.candidate_sha,
                        dest_ref,
                        gateway_protected,
                        expected_url=self._publisher_url_snapshot,
                    )
                else:
                    landed = worktree_git.push_exact(
                        remote,
                        binding.candidate_sha,
                        dest_ref,
                        gateway_protected,
                    )
            except GitCommandError as exc:
                # Distinguish a PROTECTED-branch refusal from every other
                # push refusal (remote unreachable, non-fast-forward, ...) —
                # computed the SAME way `push_exact` itself decides it
                # (bare branch name OR the full ref, against the SAME
                # `gateway_protected` set already computed above, which
                # already accounts for `allow_protected_push`), never by
                # sniffing the exception text. This is what makes
                # `push_refused_protected` (Autoloop M1 finding #7) a real,
                # emitted code instead of a dead key in
                # `cli._RESOLUTION_PRECONDITIONS` — `_precondition_protected`
                # there refuses to let ANY answer text clear it, which only
                # matters if the code is ever actually produced.
                branch_name = dest_ref[len("refs/heads/"):] if dest_ref.startswith("refs/heads/") else dest_ref
                is_protected_refusal = bool(gateway_protected) and (
                    branch_name in gateway_protected or dest_ref in gateway_protected
                )
                self._to_needs_user(
                    f"task {binding.task_id}: push of {binding.candidate_sha[:12]} "
                    f"to {remote}/{dest_ref} was REFUSED — {exc}. Nothing was "
                    "pushed; the commit itself is unaffected.",
                    kind="loop_fatal",
                    code="push_refused_protected" if is_protected_refusal else "push_refused",
                    task_id=binding.task_id,
                    detail=f"remote={remote} dest_ref={dest_ref} error={exc}",
                )
                return
        execution.candidate_commit_count = len(
            worktree_git.commit_list(execution.task_base_sha, execution.candidate_sha)
        )
        # ADVANCE the record to match what git now says. `landed` came from the
        # remote — the pre-push `ls-remote`, or `push_exact`'s own fresh
        # reconciliation of what it pushed — never from the record, the
        # directive or the executor, so this persists an observation rather than
        # a claim.
        #
        # ADVANCED, not retired, and the order matters:
        # `_auto_merge_after_completion` runs a few lines below and loads this
        # record for `worktree_path` / `candidate_sha` / `intended_remote_ref`,
        # and a merge it DEFERS is retried from the same record on a later
        # completion. Retiring here would break integration outright and make a
        # deferred merge unrecoverable. Retirement belongs to `release`, which
        # abandons the work rather than shipping it.
        #
        # Why write it at all: publication and the task lifecycle are updated in
        # separate places and can disagree, and nothing on the record marked the
        # difference between "pushed" and "push attempted" —
        # `intended_remote_ref` is written BEFORE the network call on purpose,
        # so a refused push leaves a record indistinguishable from a landed one.
        # A record still describing in-flight work after its candidate shipped
        # is a latent park: `_rebase_execution_if_stale` meets it on the next
        # revise and refuses to re-base work "a reviewer has already seen", when
        # that work is in fact durable on its own branch. `merge-window`
        # reported exactly that for `audit-0002` on 2026-08-15.
        if landed == binding.candidate_sha:
            execution.published_sha = landed
            execution.published_at = utcnow_iso()
        self._execution_store.save(execution)
        # Clear `state.task_execution` now that the candidate is actually
        # published — NOT just re-mirror it. It is display/tracking state
        # only now (the S21 retirement removed the legacy-push guard that
        # used to read it to decide whether a fall-through push was safe —
        # see docs/SECURITY.md S21); clearing it keeps `status`/`_summary`
        # honest about there being nothing left "awaiting publication". The
        # `TaskExecutionStore` record on disk is untouched.
        state.task_execution = None
        self._mark_task_completed(binding.task_id)
        self._log(
            "task_pushed",
            data={
                "task_id": binding.task_id,
                "candidate_sha": binding.candidate_sha,
                "remote": remote,
                "dest_ref": dest_ref,
            },
        )
        state.outbox = TEMPLATES["git_report"].render(
            summary_line=(
                f"pushed {landed[:12]} to {remote}/{dest_ref} (task {binding.task_id})"
            )
        )
        state.last_response = None
        state.consecutive_failures = 0
        state.phase = Phase.READY.value
        self._store.save(state)
        # AFTER the state save, deliberately. `cli._merge_window_blockers`
        # reads the phase from `state.json` on DISK, and the last thing
        # written there before this point was `phase=executing` (set in
        # `_await_response`). Calling the gate any earlier in this method
        # would see that stale value, report "a phase is executing", and defer
        # every single merge forever — a feature that logs busily and never
        # integrates anything. The registry write in `_mark_task_completed`
        # above matters for the same reason: it is what makes the gate exempt
        # the record we just published instead of treating it as a hazard.
        self._auto_merge_after_completion(binding.task_id)

    def _auto_merge_after_completion(self, task_id: str) -> None:
        """Merge this task's published branch into the base and push it
        (`auto_merge.py`), when `policy.auto_merge_enabled` is on.

        Publication is not integration: B10 retires a task once its candidate
        is durable on its own side branch, and before this call existed that
        was where the work stopped. On 2026-08-06 seven completed tasks were
        unmerged at once, including fixes for failures the loop was still
        hitting.

        Wrapped in the same "never undo a successful push" discipline as
        `_mark_task_completed`: the push already landed and the task is
        already completed, so an integration problem is logged, never parked.
        `AutoMerger` guards each task individually too; this outer guard
        covers the construction itself.

        The AUDIT pseudo-task reaches here as well, since `_dispatch_task_push`
        does not distinguish it. Its unit id is only sometimes in the registry
        (`cli` registers synthetic `audit-NNNN` units so `block` can quarantine
        them), so the outcome is one of two, both correct: unregistered → the
        merger's registry check skips it; registered and completed by
        `_mark_task_completed` → its Markdown report is integrated like any
        other completed task's work, which is what an operator would do by
        hand anyway.
        """
        if not self._policy.config.auto_merge_enabled:
            return
        if self._execution_store is None:
            return
        try:
            AutoMerger(
                config=self._config,
                git=self._git,
                policy=self._policy,
                execution_store=self._execution_store,
                registry=self._registry,
                log=self._log,
                deferrals=self._merge_deferrals,
            ).after_completion(task_id)
        except Exception as exc:      # noqa: BLE001 - bookkeeping must not undo a push
            self._log(
                "auto_merge_error",
                data={"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"},
            )

    def _mark_task_completed(self, task_id: str) -> None:
        """Retire a task whose reviewed candidate has actually landed on its
        remote branch (AUTOLOOP_TODO B10).

        Until this existed, NOTHING at runtime ever wrote `status="completed"`:
        `TaskRegistry.mark_completed` had no runtime caller (only tests), and
        `Decision` has no terminal member, so a reviewer could not express
        "done" either. Every task the loop published therefore stayed
        `in_progress` for good — the roadmap over-reported work remaining, and
        `cli._merge_window_blockers` (which exempted only COMPLETED /
        BLOCKED_BY_OPERATOR) held the merge window shut permanently until it
        was re-gated on publication instead.

        Called from exactly ONE place, immediately after `push_exact` has been
        reconciled against the remote — `landed == candidate_sha` is confirmed
        by a fresh `ls-remote`, never inferred from the push's own exit status.
        "Completed" therefore means "the reviewed object is durable on its own
        side branch", which is the strongest claim the loop can make on its
        own. It deliberately does NOT mean "merged into the base": nothing here
        observes merges, and inventing a completion the loop cannot verify is
        how `presented_report_sha256`-style trust gaps start.

        Every failure is swallowed to a log, because the push already
        succeeded. Turning a bookkeeping problem into a park would strand a
        task whose work is safely published — the exact "park and report, never
        undo" inversion this method exists downstream of. Two cases are
        expected rather than exceptional:

        * `task_completed` — the crash-recovery reconciliation path re-enters
          here for a push that already landed in an earlier process. Marking
          an already-completed task is a no-op, not an error.
        * `task_blocked_by_operator` — a task quarantined between dispatch and
          push. `mark_completed` refuses it, and that refusal is correct: an
          operator's quarantine records a decision they have not made yet, and
          completing over it deletes the decision rather than resolving it.
          That guard did not exist until this method needed it (2026-08-04):
          `mark_completed` checked COMPLETED and BLOCKED but not
          BLOCKED_BY_OPERATOR, unlike its `mark_in_progress` sibling, so the
          first version of this method silently completed a quarantined task.

        The audit pseudo-task is not a registry task and never reaches this
        path (`_dispatch_task_push` requires a `postcommit` binding, which the
        audit never produces), but `has()` is checked anyway rather than
        assumed — `get()` on an unknown id raises, and this method must not be
        the thing that breaks a successful publish.
        """
        if not self._registry.has(task_id):
            self._log("task_completion_skipped", data={"task_id": task_id, "reason": "unknown_task"})
            return
        try:
            self._registry.mark_completed(task_id)
            self._task_store.save(self._registry)
        except (TaskGraphError, OSError) as exc:
            self._log(
                "task_completion_failed",
                data={"task_id": task_id, "error": str(exc)},
            )
            return
        self._log("task_completed", data={"task_id": task_id})

    def _dispatch_changeset_push(self, directive: Directive, resp: LastResponse) -> None:
        """Publish an operator-authored changeset via the Publisher —
        `_dispatch_task_push`'s sibling for the case where there is no task,
        no `TaskExecutionStore` record, and no separate worktree: the
        reviewed candidate lives directly in the checkout this orchestrator
        itself runs from (`self._git`), because the operator committed it
        there directly (see `changeset_review.py`'s module docstring for the
        full "why" and why the generic HEAD-moved staleness check does not
        gate this path — `_step_executing` skips it for exactly this
        method).

        `directive` is used ONLY for logging/reason text — never for
        identity, same discipline as `_dispatch_task_push`: `resp.changeset`
        — the binding captured the moment THIS packet was sent — is the only
        source of which candidate this approval concerns. If the candidate
        no longer resolves, is no longer a descendant of the reviewed base,
        or its tree no longer matches what was reviewed, nothing is pushed.

        **A Publisher is REQUIRED here — there is no direct-push fallback**
        like `_dispatch_task_push`'s no-publisher branch. A direct push from
        `self._git` would push straight from the SAME checkout this
        orchestrator runs from, using its ordinary (non-scrubbed, non-
        firewalled) git configuration — exactly the retired legacy
        direct-push shape (docs/SECURITY.md S21) this feature exists to
        replace, not reintroduce. `import_candidate` instead fetches
        `resp.changeset.candidate_sha` — by literal object id, from
        `self._git.repo_root` as a local filesystem source — into the
        Publisher's own, separate repository, and `publish` pushes from
        there.
        """
        state = self.state
        binding = resp.changeset
        if self._publisher is None:
            self._to_needs_user(
                f"changeset push refused — candidate {binding.candidate_sha[:12]}: "
                "no publisher is configured. An operator-changeset review can "
                "only be published through the Publisher (see "
                "`cli._build_orchestrator`); there is no direct-push fallback "
                "for this path. Nothing was pushed.",
                kind="loop_fatal",
                code="changeset_publisher_required",
                detail=f"candidate={binding.candidate_sha}",
            )
            return
        live_url = self._git.config_get(f"remote.{self._publisher.remote}.url")
        if live_url != self._publisher_url_snapshot:
            self._to_needs_user(
                f"changeset push refused — the publisher's remote url snapshot "
                f"({redact_url(self._publisher_url_snapshot or '')}) no longer "
                f"matches the main checkout's configured "
                f"remote.{self._publisher.remote}.url ({redact_url(live_url)}). "
                "Autoloop never updates the snapshot automatically. Verify the "
                "new destination is correct, then run `python -m autoloop "
                "reprovision-publisher --confirm`. Nothing was published.",
                kind="loop_fatal",
                code="publisher_url_drift",
                detail=(
                    f"snapshot={redact_url(self._publisher_url_snapshot or '')} "
                    f"live={redact_url(live_url)}"
                ),
            )
            return
        if not self._git.is_descendant(binding.candidate_sha, binding.base_sha):
            self._to_needs_user(
                f"changeset push refused — candidate {binding.candidate_sha[:12]} "
                f"is not a descendant of the reviewed base {binding.base_sha[:12]}. "
                "Nothing was pushed.",
                kind="loop_fatal",
                code="push_not_descendant",
                detail=f"candidate={binding.candidate_sha} base={binding.base_sha}",
            )
            return
        try:
            info = self._git.read_commit(binding.candidate_sha)
        except GitCommandError as exc:
            self._to_needs_user(
                f"changeset push refused — the reviewed candidate "
                f"{binding.candidate_sha[:12]} no longer resolves: {exc}. Nothing "
                "was pushed.",
                kind="loop_fatal",
                code="push_candidate_unresolvable",
                detail=str(exc),
            )
            return
        if info.get("tree") != binding.candidate_tree_sha:
            self._to_needs_user(
                f"changeset push refused — candidate {binding.candidate_sha[:12]}'s "
                f"tree changed since it was reviewed (was "
                f"{binding.candidate_tree_sha[:12]}, now {info.get('tree', '?')[:12]}). "
                "Nothing was pushed.",
                kind="loop_fatal",
                code="push_tree_mismatch",
                detail=f"reviewed_tree={binding.candidate_tree_sha} now={info.get('tree', '?')}",
            )
            return
        gateway_protected = (
            () if self._policy.config.allow_protected_push
            else self._policy.config.protected_branches
        )
        try:
            # Import first: bring the EXACT reviewed object into the
            # publisher's own, separate object database from THIS checkout,
            # by literal sha — never a ref, never this checkout's own HEAD.
            self._publisher.import_candidate(self._git.repo_root, binding.candidate_sha)
            landed = self._publisher.publish(
                binding.candidate_sha,
                binding.dest_ref,
                gateway_protected,
                expected_url=self._publisher_url_snapshot,
            )
        except GitCommandError as exc:
            branch_name = (
                binding.dest_ref[len("refs/heads/"):]
                if binding.dest_ref.startswith("refs/heads/")
                else binding.dest_ref
            )
            is_protected_refusal = bool(gateway_protected) and (
                branch_name in gateway_protected or binding.dest_ref in gateway_protected
            )
            self._to_needs_user(
                f"changeset push of {binding.candidate_sha[:12]} to "
                f"{self._publisher.remote}/{binding.dest_ref} was REFUSED — {exc}. "
                "Nothing was pushed; the commit itself is unaffected.",
                kind="loop_fatal",
                code="push_refused_protected" if is_protected_refusal else "push_refused",
                detail=f"dest_ref={binding.dest_ref} error={exc}",
            )
            return
        # Cleared now that the candidate is actually published — mirrors
        # `_dispatch_task_push` clearing `state.task_execution`.
        state.changeset = None
        self._log(
            "changeset_pushed",
            data={
                "candidate_sha": binding.candidate_sha,
                "remote": self._publisher.remote,
                "dest_ref": binding.dest_ref,
            },
        )
        state.outbox = TEMPLATES["git_report"].render(
            summary_line=(
                f"pushed {landed[:12]} to {self._publisher.remote}/{binding.dest_ref} "
                "(operator changeset)"
            )
        )
        state.last_response = None
        state.consecutive_failures = 0
        state.phase = Phase.READY.value
        self._store.save(state)

    # ---- failure handling ---------------------------------------------------

    def _handle_parse_error(self, exc: ContractError) -> None:
        state = self.state
        state.parse_retries += 1
        self._log("parse_error", data={"code": exc.code, "error": str(exc)})
        verdict = self._policy.check_parse_budget(state.parse_retries)
        if not verdict.allowed:
            state.last_response = None
            self._to_needs_user(
                f"{verdict.reason} — last error: {exc}. Inspect the conversation, "
                "then answer with `run --answer '<message to ChatGPT>'`.",
                kind="loop_fatal",
                code="parse_budget_exhausted",
                detail=str(exc),
            )
            return
        state.outbox = parse_error_payload(exc.code, str(exc))
        state.last_response = None
        state.phase = Phase.READY.value
        self._store.save(state)

    def _handle_policy_denial(self, directive: Directive, verdict) -> None:
        state = self.state
        state.policy_denials += 1
        self._log(
            "policy_denied",
            data={
                "decision": directive.decision.value,
                "code": verdict.code,
                "reason": verdict.reason,
            },
        )
        budget = self._policy.check_denial_budget(state.policy_denials)
        if not budget.allowed:
            state.last_response = None
            # STOPS, it does not park (see `_to_fault_stop`). The reviewer has
            # now proposed refused directives more times than the budget
            # allows; there is no question for a human here, because the only
            # thing that could change the next directive is the reviewer, and
            # the reviewer is what ran out of budget. Parking held the session
            # open for an answer nobody could give — the exact stall that
            # retiring `ask_user` set out to remove, reached by the reviewer
            # ANSWERING `ask_user` repeatedly instead of by the decision
            # parking directly. The blocker record and the operator-facing
            # text are unchanged; only the terminal is.
            self._to_fault_stop(
                f"{budget.reason} — last denial: {verdict.reason}",
                code="policy_denial_budget_exhausted",
                task_id=directive.task_id,
                detail=f"decision={directive.decision.value} verdict_code={verdict.code}",
            )
            return
        state.outbox = policy_denied_payload(directive.decision.value, verdict.reason)
        state.last_response = None
        state.phase = Phase.READY.value
        self._store.save(state)

    def _handle_review_mismatch(self, exc: ContractError) -> None:
        state = self.state
        state.policy_denials += 1
        self._log("review_mismatch", data={"code": exc.code, "error": str(exc)})
        budget = self._policy.check_denial_budget(state.policy_denials)
        if not budget.allowed:
            state.last_response = None
            # PARKS, like the plan-rejection site above and unlike
            # `_handle_policy_denial`, though all three spend the same
            # `state.policy_denials` counter. A repeated review mismatch means
            # approvals keep arriving stamped for a tree that has since moved —
            # which can equally be the REPOSITORY moving under the loop (a
            # concurrent checkout, a hook, an operator committing in the same
            # worktree), not just a reviewer echoing a stale stamp. That is an
            # integrity signal with a human-side explanation, so it keeps the
            # terminal that holds the session open and asks.
            self._to_needs_user(
                f"{budget.reason} — last review mismatch: {exc}",
                kind="loop_fatal",
                code="review_mismatch_budget_exhausted",
                detail=str(exc),
            )
            return
        state.outbox = review_mismatch_payload(exc.code, str(exc))
        state.last_response = None
        state.phase = Phase.READY.value
        self._store.save(state)

    def _attempt_browser_restart(self) -> bool:
        """True when a restart actually ran and reported success.

        Thin wrapper over `_browser_restart_outcome` for callers that only
        need "did the browser come back" — everything that must also know WHY
        it did not (the cooldown case is not the same fault as a failed or
        unconfigured restart) reads the outcome directly.
        """
        return self._browser_restart_outcome() == RESTART_OK

    def _browser_restart_outcome(self) -> str:
        """Restart the browser via the operator-declared command, at most once
        per cooldown. Returns which of four things happened:

        * `RESTART_OK` — the command ran and reported success.
        * `RESTART_FAILED` — the command ran and reported failure, or could
          not be run at all.
        * `RESTART_SKIPPED_COOLDOWN` — a restart was due and was refused
          because `browser.restart_cooldown_seconds` had not elapsed since
          the last one. THE ONLY outcome meaning recovery was never
          attempted, and the reason this reports an outcome rather than a
          bool at all: see `_handle_browser_failure`.
        * `RESTART_DISABLED` — no `restart_command` is configured (the
          default). Deliberately NOT folded into the skip above: with no
          command there is nothing to try later either, so those failures
          keep spending the ordinary failure budget exactly as they always
          have — otherwise the budget would be unreachable for every
          deployment that has not configured a restart command.

        Declared rather than inferred: the loop knows a `cdp_url`, not which
        Chrome owns it, and pattern-matching process lists to decide what to
        kill is how an automation takes down someone's everyday browser. The
        shipped implementation — `python3 -m autoloop.browser.chrome_restart`,
        the value `config.example.toml` ships since 2026-08-16 — matches one
        profile by its `--user-data-dir` EXACTLY, stops every instance on it,
        and confirms the CDP endpoint answers before reporting success.

        The cooldown matters as much as the restart: without it a genuinely
        dead transport becomes a restart loop, thrashing the browser instead of
        surfacing the fault.
        """
        command = self._config.browser.restart_command
        if not command:
            return RESTART_DISABLED
        now = time.monotonic()
        cooldown = self._config.browser.restart_cooldown_seconds
        if self._last_browser_restart is not None and (
            now - self._last_browser_restart
        ) < cooldown:
            self._log("browser_restart_skipped", data={"reason": "within cooldown"})
            return RESTART_SKIPPED_COOLDOWN
        self._last_browser_restart = now
        try:
            proc = subprocess.run(
                list(command), capture_output=True, text=True, timeout=120
            )
        except (OSError, subprocess.SubprocessError) as restart_exc:
            self._log("browser_restart_failed", data={"error": str(restart_exc)})
            return RESTART_FAILED
        ok = proc.returncode == 0
        self._log(
            "browser_restarted" if ok else "browser_restart_failed",
            data={
                "returncode": proc.returncode,
                "output": (proc.stdout or proc.stderr or "").strip()[-400:],
            },
        )
        return RESTART_OK if ok else RESTART_FAILED

    def _handle_browser_failure(self, phase: Phase, exc: BrowserError) -> None:
        state = self.state
        # Try a restart BEFORE charging the failure budget. A stalled browser is
        # the fault this recovers from, so spending the budget on it means three
        # stalls end the run even when every one was recoverable — which is what
        # happened three times in one session before this existed. A restart
        # that actually runs makes this attempt free; one that RAN and failed,
        # or that does not exist because no `restart_command` is configured,
        # spends the budget exactly as before. The third case — a restart the
        # cooldown refused — is neither, and is handled on its own below.
        self._drop_client()
        outcome = self._browser_restart_outcome()
        if outcome == RESTART_SKIPPED_COOLDOWN:
            # The one action that could have fixed this was refused because
            # the cooldown was still running — so nothing was tried, and an
            # untried recovery is no evidence that recovery does not work.
            # Charging these to the failure budget ended a session on
            # 2026-08-04: four consecutive failures each logged
            # `browser_restart_skipped`, the budget ran out before the
            # cooldown did, and the loop reached `failed` having never once
            # restarted Chrome. They are charged to their own bounded budget
            # instead (`policy.max_browser_restart_skips`), which ends in a
            # park that NAMES the cooldown rather than a terminal phase with
            # no blocker record. Both guards are still wanted; only their
            # interaction was wrong.
            state.browser_restart_skips += 1
            self._log(
                "browser_error",
                data={"phase": phase.value, "error": str(exc),
                      "kind": type(exc).__name__,
                      "recovered": "restart_skipped_cooldown",
                      "restart_skips": state.browser_restart_skips},
            )
            skip_verdict = self._policy.check_browser_restart_skip_budget(
                state.browser_restart_skips
            )
            if skip_verdict.allowed:
                # Phase unchanged — the loop re-enters it with a fresh client,
                # and once the cooldown elapses the next failure gets a real
                # restart attempt, which is the whole point of not dying here.
                self._store.save(state)
                return
            cooldown = self._config.browser.restart_cooldown_seconds
            self._note_round_fault("browser_restart_cooldown_blocked")
            self._to_needs_user(
                f"{skip_verdict.reason}: each of the last "
                f"{state.browser_restart_skips} browser failures was left "
                "unrecovered because browser.restart_cooldown_seconds "
                f"({cooldown:g}s) had not elapsed since the previous restart, "
                "so no restart was attempted for any of them. Restart the "
                "browser by hand (python3 -m autoloop.browser.chrome_restart, "
                "run from the checkout), or "
                "lower browser.restart_cooldown_seconds, then resume. "
                f"Last error: {exc}",
                resume_phase=phase.value,
                kind="loop_fatal",
                code="browser_restart_cooldown_blocked",
                detail=(
                    f"phase={phase.value} kind={type(exc).__name__} "
                    f"cooldown_seconds={cooldown} "
                    f"restart_skips={state.browser_restart_skips}"
                ),
            )
            return
        # A restart command that actually RAN — whether it succeeded or not —
        # settles the question the skip counter was holding open, so it starts
        # over. `RESTART_DISABLED` cannot leave it nonzero (reaching the skip
        # branch requires a configured command), so this is a no-op there.
        state.browser_restart_skips = 0
        if outcome == RESTART_OK:
            self._log(
                "browser_error",
                data={"phase": phase.value, "error": str(exc),
                      "kind": type(exc).__name__, "recovered": "restarted"},
            )
            self._store.save(state)
            return
        state.consecutive_failures += 1
        self._log(
            "browser_error",
            data={"phase": phase.value, "error": str(exc), "kind": type(exc).__name__},
        )
        verdict = self._policy.check_failure_budget(state.consecutive_failures)
        if not verdict.allowed:
            # The run is over. A candidate that was out for review when the
            # browser gave up will have to be re-produced by a later session,
            # and that redo is a fault's cost, not the task's — brw-11 lost
            # three rounds exactly this way with its fix already committed.
            self._note_round_fault("browser_session_lost")
            state.resume_phase = phase.value
            state.stop_reason = f"{verdict.reason} — last error: {exc}"
            state.phase = Phase.FAILED.value
        # else: phase unchanged — the loop re-enters it with a fresh client.
        self._store.save(state)

    def _handle_git_failure(self, phase: Phase, exc: GitError) -> None:
        state = self.state
        self._log("git_error", data={"phase": phase.value, "error": str(exc)})
        if phase is Phase.READY:
            # Context build failed before a request existed; the outbox is
            # intact, so park retryably instead of overwriting it.
            self._to_needs_user(
                f"git unavailable while preparing the request: {exc}",
                resume_phase=Phase.READY.value,
                kind="loop_fatal",
                code="git_unavailable_in_ready",
                detail=str(exc),
            )
            return
        if phase is Phase.DELIVERING:
            # Same shape as `ready` above, and for a sharper reason. The generic
            # path below writes a git-error payload into `outbox` and returns to
            # `ready`, which then OVERWRITES `pending_request` with a fresh one —
            # abandoning a part-delivered patch in the conversation with nothing
            # left to disown it. That is rule 2 broken by a route that never
            # decided to break it. Reachable in practice: `_fall_back_to_omission`
            # builds a context, and `build_context` reads git.
            self._to_needs_user(
                f"git unavailable while delivering the review packet: {exc}. "
                "The request and its delivery cursor are intact; parts already "
                "confirmed are not re-sent on retry.",
                resume_phase=Phase.DELIVERING.value,
                kind="loop_fatal",
                code="git_unavailable_in_delivering",
                detail=str(exc),
            )
            return
        state.consecutive_failures += 1
        decision = "unknown"
        if state.last_response is not None:
            try:
                decision = parse_response(state.last_response.raw).decision.value
            except ContractError:
                pass
        verdict = self._policy.check_failure_budget(state.consecutive_failures)
        if not verdict.allowed:
            state.last_response = None
            self._to_needs_user(
                f"repeated git failures — last error: {exc}",
                kind="loop_fatal",
                code="git_failure_budget_exhausted",
                detail=str(exc),
            )
            return
        state.outbox = git_error_payload(decision, str(exc))
        state.last_response = None
        state.phase = Phase.READY.value
        self._store.save(state)

    # ---- helpers ------------------------------------------------------------

    def _to_needs_user(
        self,
        question: str,
        resume_phase: str | None = None,
        *,
        kind: str = "loop_fatal",
        code: str = "unclassified",
        task_id: str | None = None,
        detail: str = "",
    ) -> None:
        """Park the loop on `needs_user`, classified `kind` (see the module
        docstring's "Blocker classification" section). `kind` and `code`
        deliberately default to the fail-closed values (`"loop_fatal"`,
        `"unclassified"`) so a park site nobody has explicitly reasoned
        about — today's or a future one — stops the whole loop rather than
        silently being treated as safe to quarantine and churn past.

        Every call — classified or not — is persisted as a `blockers.
        Blocker` when `self._blocker_store` is configured, carrying the
        EXACT `question` text, so `python -m autoloop blockers`/`answer`
        can show the operator precisely what they would have seen here.
        `task_id` is descriptive metadata on the record either way; only
        `cli.py`'s continuous-mode handling treats it as actionable, and
        only for `kind="task_fatal"`.
        """
        state = self.state
        originating_phase = state.phase
        state.question = question
        state.resume_phase = resume_phase
        state.phase = Phase.NEEDS_USER.value
        state.stop_kind = ""
        state.park_kind = kind
        state.park_task_id = task_id
        state.park_blocker_id = None
        self._log(
            "needs_user",
            data={
                "question": question,
                "resume_phase": resume_phase,
                "kind": kind,
                "code": code,
                "task_id": task_id,
            },
        )
        if self._blocker_store is not None:
            blocker_task_id = task_id or NO_TASK
            # Idempotent: re-parking on the same (task, code, phase) updates
            # the existing open record instead of minting a duplicate, so a
            # restart/retry against the same wall does not fill the queue.
            blocker = self._blocker_store.record(
                task_id=blocker_task_id,
                kind=kind,
                code=code,
                question=question,
                detail=detail,
                phase=originating_phase,
                now=utcnow_iso(),
                # Attribution, so a later run can tell a live blocker from one
                # abandoned by a session that has since been reset.
                session_id=state.session_id or "",
            )
            state.park_blocker_id = blocker.id
        self._store.save(state)

    def _to_fault_stop(
        self,
        reason: str,
        *,
        code: str,
        task_id: str | None = None,
        detail: str = "",
    ) -> None:
        """End the run on `stopped` because the loop hit a wall no further
        message can get past — the terminal that REPLACES a park for causes
        only the reviewer could have avoided.

        The distinction against `_to_needs_user` is the whole point. A park
        asks a question and holds the session open for the answer; that is the
        right shape for an environmental fault (a logged-out browser, a
        publisher url that drifted, a dirty checkout) where a human doing
        something makes the very same session resumable. It is the WRONG shape
        for "the reviewer kept proposing directives policy refuses": there is
        no answer to give — the session's next step would be to ask the same
        reviewer the same thing again — so parking left an autonomous run
        waiting on a human who has nothing to decide. That was the last park
        `ask_user`'s retirement could still reach (a reviewer that answers
        `ask_user` until the denial budget runs out), which is why retiring the
        decision was only half the job.

        Ending is not the same as forgetting. Everything the park recorded is
        still recorded here:

        * `stop_reason` carries the exact text the park's `question` would
          have, so `status` / `_summary` read the same;
        * a `blockers.Blocker` is still written (`kind="loop_fatal"`, the same
          `code`), so `python -m autoloop blockers` / `answer` show and resolve
          it exactly as before — `stop_blocker_id` names the record;
        * `stop_kind="fault"` is what tells this apart from a reviewer's own
          `stop`. Continuous mode reads it to STOP rather than treat the
          terminal as a clean boundary and kick off a fresh session into the
          same wall (`cli._run_continuous`), and `smoke-browser` reads it to
          keep reporting FAIL for a misbehaving reply.

        `resume_phase` is cleared unconditionally: a fault stop is terminal,
        and leaving a resumable phase behind would advertise a `run --retry`
        that re-enters the phase that just failed.
        """
        state = self.state
        originating_phase = state.phase
        state.stop_reason = reason
        state.stop_kind = "fault"
        state.question = None
        state.resume_phase = None
        state.phase = Phase.STOPPED.value
        state.stop_blocker_id = None
        # The SAME event type a contract stop logs, with the classification
        # alongside it: everything that greps the transcript for `stopped`
        # keeps working, and anything that cares which kind it was can read
        # `kind` rather than inferring it from the reason's prose.
        self._log(
            "stopped",
            data={
                "reason": reason,
                "kind": "fault",
                "code": code,
                "task_id": task_id,
            },
        )
        if self._blocker_store is not None:
            blocker = self._blocker_store.record(
                task_id=task_id or NO_TASK,
                # `loop_fatal`, never `task_fatal`: quarantining one task would
                # imply the rest of the roadmap can proceed, and a reviewer that
                # spent the denial budget is not a per-task condition.
                kind="loop_fatal",
                code=code,
                question=reason,
                detail=detail,
                phase=originating_phase,
                now=utcnow_iso(),
                session_id=state.session_id or "",
            )
            state.stop_blocker_id = blocker.id
        self._store.save(state)

    def active_provider(self) -> str:
        """The provider currently holding the reviewer role.

        State wins over config once a failover has happened, so a resumed run
        does not silently return to a transport whose allowance is spent and
        exhaust it again.
        """
        return self.state.active_provider or self._config.conversation.provider

    def _outstanding_merge_deferral(self, task_id: str) -> tuple[MergeDeferral | None, bool]:
        """`(deferral, known)` — the task's undrained auto-merge retry, if any,
        and whether the store could actually be read.

        Fail-closed the way this file's other "may I discard something" checks
        are: an unreadable or corrupt deferral answers `(None, False)`, which
        every caller must treat as "assume one exists". A dropped retry is
        indistinguishable from the unmerged-forever state auto-merge exists to
        end (`MergeDeferralStore`'s own docstring makes the same argument for
        raising on corruption), so guessing "there is none" is the one answer
        that cannot be walked back.

        `_merge_deferrals` is always set by `__init__`, but an Orchestrator
        hand-built by a test via `__new__` may not have it; that reads as
        unknown too, rather than as an absent deferral.
        """
        store = getattr(self, "_merge_deferrals", None)
        if store is None:
            return None, False
        try:
            return store.load(task_id), True
        except (StateError, OSError) as exc:
            self._log(
                "merge_deferral_unreadable",
                data={"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"},
            )
            return None, False

    def _reconcile_published_execution(self, execution: TaskExecution, task: Task) -> bool:
        """Did this record turn out to describe work that ALREADY SHIPPED? If
        so, complete the task and retire the record — or PIN it, when an
        undrained auto-merge deferral is still reading from it (see below).
        True either way, and it means the caller stops here.

        The other direction of the same drift `release` used to leave behind.
        The task lifecycle and the execution record are written in separate
        places, so a task can be PUBLISHED while its record still describes a
        candidate in flight. `merge-window` reported exactly that on
        2026-08-15:

            note: task audit-0002: candidate 8d96c52aeca4 is published at
            origin/refs/heads/autoloop/audit-0002 — safe to merge past, but its
            record still reads in_progress, so a later revise would park it

        Benign for merging, and the check says so — but a latent park. Revise
        that task after the base moves and `_rebase_execution_if_stale` refuses
        to re-base "work a reviewer has already seen", which is the right rule
        for work that is only in a worker repo and the wrong one for work that
        is durable on its own remote branch. Nothing is discarded by moving on
        from a published candidate.

        GIT IS THE AUTHORITY, never the record. `published_sha` says only where
        to go and ask; the decision comes from a fresh `remote_ref_sha`
        round-trip that must come back equal to the candidate. That ordering is
        deliberate — `intended_remote`/`intended_remote_ref` are push INTENT,
        written BEFORE the network call, so a record whose push was REFUSED
        carries the same fields as one whose push landed, and only the remote
        can tell them apart (the same reasoning as
        `cli._candidate_publication`, which is why this does not simply trust
        `published_sha` either).

        Fail-closed everywhere else: no candidate, no recorded destination, a
        record predating `published_sha`, an unreachable remote, a ref at a
        different sha — all return False and leave the existing park exactly as
        it was. An unanswerable question is never answered "already shipped".

        A QUARANTINED (or dependency-blocked) task is left to the park too, and
        that guard has to live here rather than in `_mark_task_completed`.
        `mark_completed` refuses `BLOCKED_BY_OPERATOR` — correctly, since an
        operator's quarantine records a decision they have not made yet — and
        `_mark_task_completed` swallows that refusal to a log, which is the
        right behaviour AFTER a successful push. Reaching it from here would be
        different: the record is already retired by then, so the task would end
        up with no record, no completion, no park and nothing behind the blocker
        the operator was about to answer. Retiring is only safe where completing
        is.

        A record an OUTSTANDING MERGE DEFERRAL still depends on is pinned
        instead of retired, and this is the one exemption that is about another
        subsystem rather than about this one. `_dispatch_task_push` already
        states the rule (see its "ADVANCED, not retired" comment): auto-merge
        reads `candidate_sha` / `worktree_path` / `intended_remote_ref` back off
        the live record, and a deferred merge is retried from that same record
        on a later completion. Archive it here and the next drain finds no
        record, `AutoMerger.attempt` SKIPS the task — which also CLEARS the
        deferral — and published-but-unmerged work silently stops being retried.
        That is the backlog auto-merge exists to end, rebuilt one task at a
        time. So the pin: log it, complete the task (the same fresh `ls-remote`
        that would have justified retiring justifies completing, and auto-merge
        will only touch a COMPLETED task), and stop this dispatch without
        parking. Retirement happens on a later dispatch, once the deferral has
        drained — `_deferrals.clear` runs only on a merge that was pushed and
        confirmed, or on a task the merger has explicitly written off.

        Parking instead would be worse than doing nothing: its message asks the
        operator to publish, abandon, or ARCHIVE the record, and archiving is
        precisely the action that strands the deferral.

        Retirement uses the SAME `retire_execution` as `release`: record and
        worker filed away together under one label, nothing deleted, so the
        candidate stays recoverable even though it is also on the remote.
        """
        candidate = execution.candidate_sha
        remote = execution.intended_remote
        dest_ref = execution.intended_remote_ref
        if not candidate or not remote or not dest_ref:
            return False
        if execution.published_sha != candidate:
            return False
        if self._registry.has(task.id) and self._registry.state_of(task.id) in (
            TaskState.BLOCKED,
            TaskState.BLOCKED_BY_OPERATOR,
        ):
            return False
        try:
            landed = self._git.remote_ref_sha(remote, dest_ref)
        except (GitError, OSError):
            return False
        if landed != candidate:
            return False

        deferral, deferral_known = self._outstanding_merge_deferral(task.id)
        if deferral is not None or not deferral_known:
            self._log(
                "execution_retire_pinned_by_deferral",
                data={
                    "task_id": task.id,
                    "candidate_sha": candidate,
                    "deferral_reason": deferral.reason if deferral is not None else "",
                    "deferral_attempts": deferral.attempts if deferral is not None else 0,
                    "known": deferral_known,
                },
            )
            self._mark_task_completed(task.id)
            return True

        try:
            retired = retire_execution(
                task.id,
                self._execution_store,
                self._worker_repos,
                reason=f"published-{candidate[:12]}",
            )
        except (GitError, OSError, StateError) as exc:
            self._log(
                "execution_retire_failed",
                data={"task_id": task.id, "error": f"{type(exc).__name__}: {exc}"},
            )
            # Which half failed decides what to do next, and asking the store is
            # the only honest way to know. If the RECORD half never landed,
            # nothing changed and the caller's park is still the right answer.
            # If it landed and the WORKER half failed, falling back to the park
            # would name a record that no longer exists — and the leftover
            # worker announces itself at the next dispatch anyway (see
            # `retire_execution`'s ordering note), so stop here instead.
            try:
                return self._execution_store.load(task.id) is None
            except StateError:
                return False
        self._log(
            "execution_retired_published",
            data={
                "task_id": task.id,
                "candidate_sha": candidate,
                "remote": remote,
                "dest_ref": dest_ref,
                "record_archived_to": str(retired.record_path or ""),
                "worker_quarantined_to": str(retired.worker_path or ""),
            },
        )
        self._mark_task_completed(task.id)
        return True

    def _carry_reviewed_candidate_past(
        self, execution: TaskExecution, task: Task, head: str
    ) -> str:
        """Merge `head` INTO this task's own branch so a REVIEWED candidate
        survives the branch head moving under it. Returns `""` when the record
        was carried forward and saved; otherwise the reason the caller must
        park with, phrased to follow "the head could not be merged … either —".

        MERGE, NOT RE-BASE, and the difference is the whole point. Re-basing
        rewrites the reviewed commits, changing their shas — and an approval
        binds to a candidate by exact sha, so the reviewed object would simply
        cease to exist. A merge adds one commit and rewrites nothing:
        `candidate_sha` still resolves, still has the tree the reviewer saw,
        and is still reachable from the branch tip. Only `task_base_sha` moves.

        WHY `task_base_sha` MOVES TO `head` rather than staying put or naming
        the merge commit. Every review artifact is a DIRECT tree-to-tree diff
        of `task_base_sha..candidate_sha` (`range_diff`/`commit_range_paths`
        are `diff-tree`, not a history walk), so:
          * leaving the old base would put everything mainline added since
            into the reviewer's diff and into the `out_of_scope_paths`
            comparison — a diff of someone else's work, attributed to this
            task, quite possibly over `RANGE_DIFF_MAX_BYTES`;
          * naming the merge commit would show only the NEXT round's changes
            and hide every earlier round from the reviewer.
        The new head is the one value that yields exactly this task's net
        change, because the merge already put everything up to `head` on the
        branch. `candidate_sha`, `candidate_commit_count`, `review_round`,
        `attempt_count`, `fault_attempt_count` and the ledger are all left
        alone — a moving base must refill no budget and forget no round.

        FIVE PRECONDITIONS, each of which falls back to the park unchanged.
        This is deliberately conservative: the park it replaces was correct,
        so anything this cannot establish stays parked rather than guessed.

          1. The record names a worker repository. `Path("")` resolves to the
             CWD, so an empty `worktree_path` would otherwise send the probe
             below at the primary checkout.
          2. The record names a candidate — there is nothing to preserve, and
             so nothing to prefer over the ordinary re-base, without one.
          3. That worker still passes `worker_repo_is_reusable` (exists, is a
             git repository in its own right, checked out on the recorded
             branch). RE-PROBED here rather than reading the caller's
             `worker_reusable` flag: that flag carries the round-0 reuse
             decision, and a record rescued from any other call site must
             behave identically. `_dispatch_task_implement` re-probes for the
             same reason.
          4. The worker is CLEAN. Merging over uncommitted residue is exactly
             the quiet discard the refusal exists to prevent — and for a
             resumed round that residue IS the work being resumed.
          5. The branch tip contains `candidate_sha`. This is what makes "the
             approval binding survives" a checked fact rather than an
             assumption about which commit the branch happens to be sitting on.

        A GENUINE CONFLICT PARKS, unchanged. `merge_foreign_commit` aborts and
        reports the conflicted paths; resolving them here would be the same
        silent rewrite of reviewed work, one level down.

        NOT gated by `auto_merge_enabled`, deliberately. That flag exists
        because auto-merge moves the SHARED branch head with no operator in the
        loop; this merge moves one worker repository's own private branch and
        touches neither the primary checkout nor any remote. Gating it would
        mean a deployment with auto-merge off keeps the park this exists to
        remove — while still getting a head that moves under it, since an
        operator merging by hand moves it just as well.

        One interaction worth knowing about rather than guarding: a PENDING
        COMMIT INTENT from a crash mid-commit is classified further down this
        dispatch by `reconcile_after_crash`, whose sanity gate is "the intent's
        expected parent is `task_base_sha` or a descendant of it". After a
        carry-forward that gate answers no, so such a round parks AMBIGUOUS
        instead of `task_base_behind_head`. Both are parks needing a human and
        neither destroys anything, so this does not refuse on that account —
        but the blocker an operator sees for that (rare) overlap changes.
        """
        old_base = execution.task_base_sha
        worktree_path = (execution.worktree_path or "").strip()
        if not worktree_path:
            return "the record names no worker repository"
        candidate = execution.candidate_sha
        if not candidate:
            return "the record names no candidate commit to preserve"
        if not worker_repo_is_reusable(Path(worktree_path), execution.task_branch):
            return (
                f"its worker repository {worktree_path} is not a git repository "
                f"checked out on {execution.task_branch or '(no branch recorded)'}"
            )
        # The scrubbed env is not decoration here either (see the same
        # construction in `_dispatch_task_implement`): a worker-rooted gateway
        # without it resolves the CALLING process's ambient git config.
        worker = GitGateway(Path(worktree_path), self._policy, env=worker_env())
        try:
            if worker.is_dirty():
                return (
                    "its worker repository has uncommitted changes, and merging "
                    "over them could destroy work no reviewer has seen"
                )
            tip = worker.head_sha()
            if not worker.is_descendant(tip, candidate):
                return (
                    f"its worker branch tip {tip[:12]} does not contain the "
                    f"reviewed candidate {candidate[:12]}"
                )
            attempt = worker.merge_foreign_commit(
                # Absolute, resolved: the policy layer refuses a relative
                # fetch source outright, and `GitGateway` does not resolve
                # `repo_root` for itself.
                str(Path(self._git.repo_root).resolve()),
                head,
                f"autoloop: merge branch head {head[:12]} into task {task.id} "
                f"(reviewed candidate {candidate[:12]} preserved)",
            )
        except (GitError, OSError) as exc:
            return f"its worker repository could not be merged: {type(exc).__name__}: {exc}"

        if not attempt.merged:
            self._log(
                "execution_base_carry_forward_refused",
                data={
                    "task_id": task.id,
                    "old_base": old_base,
                    "head": head,
                    "candidate_sha": candidate,
                    "conflicted_paths": list(attempt.conflicted_paths),
                    "restored": attempt.restored,
                    "error": attempt.error,
                },
            )
            detail = (
                "it conflicts at " + ", ".join(attempt.conflicted_paths)
                if attempt.conflicted_paths
                else f"git refused: {attempt.error}"
            )
            if not attempt.restored:
                detail += (
                    " (and the worker repository is NOT clean again — look at "
                    f"{worktree_path} before anything else touches it)"
                )
            return detail

        execution.task_base_sha = head
        self._execution_store.save(execution)
        self._log(
            "execution_base_carried_forward",
            data={
                "task_id": task.id,
                "old_base": old_base,
                "new_base": head,
                "merge_sha": attempt.head_sha,
                # Every one of these is asserted to be UNCHANGED by this path:
                # the reviewed object still exists, the round count still says
                # how many reviews happened, and neither budget was refilled.
                "candidate_sha": candidate,
                "review_round": execution.review_round,
                "attempt_count": execution.attempt_count,
                "fault_attempt_count": execution.fault_attempt_count,
                "worktree_path": worktree_path,
            },
        )
        return ""

    def _rebase_execution_if_stale(
        self, execution: TaskExecution, task: Task, *, worker_reusable: bool = False
    ):
        """Re-point a retry at the CURRENT branch head when its pinned base has
        been left behind (docs/AUTOLOOP_TODO.md B9).

        task_base_sha is recorded once, at first dispatch. Without this, a task
        that failed for a reason since FIXED on the branch rebuilds its worker
        at the old base and fails identically -- forever. Observed 2026-08-02:
        audit-0001 was refused for two failing tests, those tests were fixed one
        commit later, and the retry failed with the same two because its base
        predated the fix. The tell was the worker suite reporting 929 passed
        against the checkout's 931.

        Returns the execution to use, or None when it parked or reconciled
        instead (either way this dispatch stops here).

        Three cases, deliberately different:

        * Nothing reviewed yet (review_round == 0) -- re-base. The worker is
          QUARANTINED rather than deleted, so a refused candidate stays on disk
          as evidence, and attempt_count is PRESERVED: a moving base must not
          silently refill the retry budget, or a task could churn forever by
          re-basing. The ONE exemption is `worker_reusable=True` (wrk-01):
          the caller already established, BEFORE calling here, that the
          recorded worker passes the three-fact reuse probe — the recorded
          `worktree_path` exists, is a git repository, and is checked out on
          the recorded `task_branch` — so the round it holds is resumed on
          that worker as it stands, and a base that merely moved on is not
          grounds to quarantine and rebuild it. Nothing recorded changes
          (the stale base included). Only this nothing-reviewed-yet re-base
          is skipped by the flag: a reviewed record still reconciles or
          parks exactly as below.
        * A review already happened AND the candidate is confirmed published --
          RECONCILE, do not park (`_reconcile_published_execution`). Nothing
          would be discarded: the reviewed object is durable on its own branch.
        * A review already happened and the candidate is not published --
          CARRY IT FORWARD: merge the current head INTO the task branch
          (`_carry_reviewed_candidate_past`) and continue at the new base.
          Only when that cannot be done safely does this still park, naming
          both shas and the reason. Discarding a candidate a reviewer has seen
          is not a decision to make quietly -- and a merge discards nothing:
          every reviewed commit keeps its exact sha and stays reachable, so an
          approval that binds to one by sha still names an object that exists.
          The old behaviour was to park here unconditionally, which made HUMAN
          RESPONSE TIME fatal to a candidate: the head walks forward while a
          task waits for its blocker to be answered (every other task's
          completion auto-merges), so answering one park caused the next.
          23 of 108 parks before 2026-08-20 were this one code; roadmap-01 was
          unstuck and re-parked four minutes later.
        """
        head = self._git.head_sha()
        base = execution.task_base_sha
        if not base or base == head:
            return execution
        # Strictly BEHIND the branch, not merely different: a base that is not
        # an ancestor means something unusual (a rewritten branch), and
        # silently re-pointing that is worse than stopping.
        if not self._git.is_descendant(head, base):
            return execution

        if execution.review_round > 0 and self._reconcile_published_execution(execution, task):
            return None
        if execution.review_round > 0:
            refusal = self._carry_reviewed_candidate_past(execution, task, head)
            if not refusal:
                return execution
            self._to_needs_user(
                f"task {task.id}: its recorded base {base[:12]} is behind the "
                f"branch head {head[:12]}, but a review round has already run "
                f"against candidate {(execution.candidate_sha or '(none)')[:12]}. "
                f"The head could not be merged into the task branch either — "
                f"{refusal}. Re-basing would discard work a reviewer has "
                "already seen, so nothing was changed. Either publish or "
                "abandon that candidate, or archive "
                f".autoloop/executions/{task.id}.json to start fresh at the "
                "current head.",
                kind="task_fatal",
                code="task_base_behind_head",
                task_id=task.id,
                detail=(
                    f"base={base} head={head} "
                    f"review_round={execution.review_round} refusal={refusal}"
                ),
            )
            return None

        if worker_reusable:
            # wrk-01: reuse wins over the re-base. See the docstring — the
            # record is returned exactly as loaded, so the resumed round
            # continues in the recorded worker at the recorded (stale) base.
            self._log(
                "execution_rebase_skipped_worker_reused",
                data={
                    "task_id": task.id,
                    "task_base_sha": base,
                    "head": head,
                    "worktree_path": execution.worktree_path,
                    "task_branch": execution.task_branch,
                },
            )
            return execution

        stamp = utcnow_iso().replace(":", "").replace("-", "")
        try:
            quarantined = self._worker_repos.quarantine(
                task.id, f"stalebase-{base[:12]}-{stamp}"
            )
        except (GitError, OSError):
            quarantined = None      # nothing on disk yet; recreation is enough
        repo = self._worker_repos.create(task.id, self._git.repo_root, head)
        execution.task_base_sha = head
        execution.task_branch = repo.branch
        execution.worktree_path = str(repo.path)
        execution.candidate_sha = ""      # belonged to the old base
        execution.candidate_commit_count = 0
        self._execution_store.save(execution)
        self._log(
            "execution_rebased",
            data={
                "task_id": task.id,
                "old_base": base,
                "new_base": head,
                "quarantined": str(quarantined) if quarantined else None,
                "attempt_count": execution.attempt_count,
                # Preserved by the same rule and for the same reason: a moving
                # base must not refill EITHER budget. The ledger rides along
                # untouched (it is on this same record), so the per-attempt
                # reasons survive a re-base too.
                "fault_attempt_count": execution.fault_attempt_count,
            },
        )
        return execution

    def _drain_task_inbox(self) -> None:
        """Merge any operator-submitted task requests into the registry.

        Called between steps (see `run`). Never raises: an inbox problem is an
        operator's typo, and a typo must not stop a loop that is mid-task. A
        request the registry refuses (duplicate id, unknown dependency, bad
        approved path) is reported and dropped — `TaskRegistry.add_many` is the
        single validation gate, the same one a ChatGPT `plan` goes through, so
        there is no second implementation here to drift from it.
        """
        if self._task_inbox is None:
            return
        try:
            specs, problems = self._task_inbox.drain()
        except OSError as exc:  # pragma: no cover - unreadable inbox directory
            self._log("task_inbox_error", data={"error": str(exc)})
            return
        for problem in problems:
            self._log("task_inbox_rejected", data={"reason": problem})
        if not specs:
            return
        added, reprioritised, refused = apply_requests(self._registry, specs)
        if added or reprioritised:
            self._task_store.save(self._registry)
        self._log(
            "task_inbox_drained",
            data={"added": added, "reprioritised": reprioritised, "refused": refused},
        )

    def _get_client(self):
        if self._client is None:
            provider = self.active_provider()
            # `client_factory` stays the way the CONFIGURED provider is built,
            # and `provider_factory` is consulted only once a failover has
            # actually moved the active provider away from it. Preferring the
            # provider-aware one unconditionally would silently bypass every
            # caller that injects its conversation through `client_factory` —
            # which is how the whole test suite does it, and how a run with no
            # failover in sight should keep behaving.
            if self._provider_factory is not None and provider != self._config.conversation.provider:
                self._client = self._provider_factory(provider)
            else:
                self._client = self._client_factory()
        return self._client

    def _client_for_request(self, req: PendingRequest):
        """A client aimed at THIS request's conversation, not the loop's.

        Every phase that touches a pending request goes through here, so
        "reconcile a historical request against the URL it was sent to" is a
        property of the code path rather than a rule each site remembers. After
        a rotation the two differ, and using the current URL to reconcile an
        older request would ask the wrong chat whether it holds a message it
        never received.
        """
        self._bind_request_conversation(req)
        client = self._get_client()
        retarget = getattr(client, "retarget", None)
        aimed_at = getattr(client, "conversation_url", None)
        if retarget is not None and aimed_at != req.conversation_url:
            retarget(req.conversation_url)
        return client

    def _bind_request_conversation(self, req: PendingRequest) -> None:
        """Give a request its authoritative conversation if it has none.

        Only reachable for requests written before this field existed, and only
        ever correct because it cannot happen after a rotation: the binding is
        taken on first touch, every request created since carries its own, and
        `rotations == 0` means the loop URL *is* this request's URL. Guarded
        rather than assumed — an unbound request surfacing after a rotation
        would be a real inconsistency, and silently pointing it at the new chat
        is exactly the wrong repair.
        """
        if req.conversation_url:
            return
        if self.state.rotations:
            raise StateError(
                f"request {req.request_id} has no conversation binding but this run "
                f"has already rotated {self.state.rotations} time(s); it cannot be "
                "attributed to a conversation. Inspect .autoloop/state.json."
            )
        req.conversation_url = self.state.conversation_url
        req.conversation_epoch = self.state.conversation_epoch
        self._store.save(self.state)

    @staticmethod
    def _client_send_outcome(client) -> str:
        """The transport's verdict, or "unknown" from a provider without the
        optional observation capability — which is every provider today except
        the Playwright one, and must stay behaviourally identical to before."""
        outcome = getattr(client, "send_outcome", None)
        if outcome is None:
            return SendOutcome.UNKNOWN.value
        return getattr(outcome, "value", str(outcome))

    @staticmethod
    def _observation_summary(client) -> list[dict]:
        """Observations as plain dicts for the transcript: path, status and a
        coarse failure string. `SendObservation` has nowhere to put a header,
        cookie or body, so this cannot carry credentials into the log."""
        observations = getattr(client, "send_observations", None) or []
        return [asdict(obs) for obs in observations]

    def _drop_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def _log(self, entry_type: str, request_id: str | None = None, data: dict | None = None):
        self._transcript.append(
            entry_type,
            iteration=self.state.iteration,
            request_id=request_id,
            data=data,
        )

    def _stopwatch(self) -> Stopwatch:
        """Start measuring one operation. Total — never raises, never blocks.

        The whole timing path is `Stopwatch(clock)` here and `watch.stamp(...)`
        at the completion event this method's caller already emits. There is no
        new event, no second store, and no branch that depends on the result:
        an operation whose duration could not be read logs exactly the record
        it logged before this existed.
        """
        return Stopwatch(self._timing_clock)
