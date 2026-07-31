"""The orchestrator: a persisted state machine around one LLM conversation.

Phases (see state.Phase):

    ready ──► submitting ──► awaiting ──► executing ─┬─► ready        (loop)
                   │                                 ├─► stopped      (stop)
                   │ send attempted,                  └─► needs_user  (ask_user / budgets)
                   │ acceptance unknown
                   ▼
       submission_unconfirmed ──reconcile──┬─► awaiting   (it did persist)
                                           └─► needs_user (ambiguous; NEVER
                                                           an automatic resend)

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
  only ever reconciles. It never resends: the backend may have accepted a
  message the browser failed to observe, so an automatic retry could
  double-post. Resolution is either "it did persist" → awaiting, or a park for
  the operator (`run --retry` to reconcile again, `run --resubmit` to allow one
  more send of the same request id).
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
* ResponseTimeoutError(start)  → ordinary budget as below, PLUS: 3rd
                                 consecutive one for the same request may
                                 rotate (see "silent conversation" above)
* other BrowserError           → drop conversation, retry same phase; failure
                                 budget exhausted → failed (resume via --retry)
* GitError                     → reported back to ChatGPT (budget-capped);
                                 in `ready` (context build) → needs_user with
                                 the outbox preserved
* ContractError (parse)        → corrective re-prompt; budget-capped
* review-integrity mismatch    → failure_recovery re-prompt; denial budget
* policy denial / plan reject  → failure_recovery re-prompt; denial budget

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
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from . import environment
from .blockers import NO_TASK, BlockerStore
from .browser.chatgpt import SubmitResult
from .browser.observation import SendOutcome
from .config import AutoloopConfig
from .config_writer import update_conversation_url
from .context import build_context, render_context
from .contract import (
    AUDIT_TASK_ID,
    COMMIT_DECISIONS,
    PUSH_DECISIONS,
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
    ConversationUnusableError,
    ExecutorError,
    EnvironmentDriftError,
    GitCommandError,
    GitError,
    LoginExpiredError,
    ResponseTimeoutError,
    StateError,
    TaskGraphError,
)
from .manifest import ManifestStore
from .executor import TaskExecutor
from .git_gateway import GitGateway
from .packet import build_review_packet
from .policy import PolicyEngine, Verdict
from .publisher import Publisher, redact_url
from .worker_env import verify_worker_isolation, worker_env
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
    LastResponse,
    LoopState,
    PendingRequest,
    Phase,
    PostcommitBinding,
    RotationRecord,
    StateStore,
    utcnow_iso,
)
from .tasks import Task, TaskRegistry, TaskStore
from .transcript import TranscriptLogger
from .validation import run_validation_commands
from .worktask import (
    CommitIntent,
    IntentStore,
    Reconciliation,
    TaskExecution,
    TaskExecutionStore,
    reconcile_after_crash,
)
from .worktree import WorktreeManager


#: Independent ceiling on commit/packet attempts for one task, including those
#: that never produced a review. `review_round` counts only dispatched reviews,
#: so on its own it would let repeated structural refusals churn locally
#: without bound.
MAX_TASK_ATTEMPTS = 5

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


class Orchestrator:
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
        publisher: Publisher | None = None,
        worker_repos=None,
        publisher_url_snapshot: str | None = None,
        config_path: Path | None = None,
    ):
        self._config = config
        self._store = store
        self.state = state
        self._policy = policy
        self._git = git
        self._executor = executor
        self._transcript = transcript
        self._client_factory = client_factory
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
        self._client = None

    # ---- main loop ----------------------------------------------------------

    def run(self, max_steps: int | None = None) -> str:
        """Run until a terminal phase, a pause request, or max_steps."""
        steps = 0
        while True:
            if self._config.pause_file.exists():
                self._log("paused")
                return "paused"
            phase = Phase(self.state.phase)
            if phase in TERMINAL_PHASES:
                return phase.value
            if max_steps is not None and steps >= max_steps:
                return phase.value
            steps += 1
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

    def _step(self, phase: Phase) -> None:
        if phase is Phase.READY:
            self._step_ready()
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
        ctx = build_context(state, self._git, self._registry, request_id, state.outbox)
        prompt = build_prompt(request_id, next_iteration, render_context(ctx), state.outbox)
        postcommit = self._current_pending_postcommit(state.outbox, ctx.report_sha256)
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
        state.outbox = None
        state.iteration = next_iteration
        state.phase = Phase.SUBMITTING.value
        self._log(
            "request_prepared",
            request_id=request_id,
            data={
                "head_sha": ctx.head_sha,
                "base_sha": ctx.base_sha,
                "report_sha256": ctx.report_sha256,
                "timestamp": ctx.timestamp,
                "chars": len(prompt),
            },
        )
        self._store.save(state)

    def _step_submitting(self) -> None:
        state = self.state
        req = state.pending_request
        if req is None:
            raise StateError("phase=submitting but no pending request")
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
            if req.last_send_outcome == SendOutcome.REJECTED.value:
                state.phase = Phase.SUBMISSION_REJECTED.value
                self._store.save(state)
                return
            self._park_ambiguous(req, reconciled=True)
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
        self._store.save(state)
        try:
            result = client.submit(req.request_id, req.prompt)
        except BrowserError:
            if not getattr(client, "send_attempted", True):
                # Nothing was sent (composer/Send never accepted the input), so
                # a later retry is unambiguous and may submit normally.
                req.send_attempted = False
                self._store.save(state)
            raise
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
            data={"result": result.value, "prompt": req.prompt},
        )
        self._store.save(state)

    def _step_submission_unconfirmed(self) -> None:
        """Resolve an ambiguous send by reconciliation only — never by resending."""
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
        self._park_ambiguous(req, reconciled=True)

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
        """
        state = self.state
        self._log(
            "conversation_unusable",
            data={"phase": phase.value, "error": str(exc), "reason_code": "conversation_unusable"},
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
        self._attempt_rotation(req, reason="conversation_unusable")

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
                f"the conversation is unusable and {verdict.reason}. `run --retry` "
                "alone will not clear this — it will park here again with the same "
                "reason. A second rotation in one run usually means the fault is "
                "not the chat, so inspect the project first; raise "
                "policy.max_conversation_rotations only if you have established "
                "otherwise.",
            )
            return

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
                "posted before failing, so autoloop will not try again on its own.",
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

        ChatGPT does not mint a `/c/<id>` until a chat has its first turn, so
        the order is forced: retarget to the project page, submit there, and
        only then read the id the server assigned. Every step after the submit
        is verification.
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
        result = client.submit(req.request_id, prompt)
        if result not in (SubmitResult.CONFIRMED, SubmitResult.ALREADY_PERSISTED):
            raise BrowserError(
                f"the replacement chat did not accept the request ({result.value})"
            )

        new_url = current_url()
        if not self._url_in_project(new_url, project_url):
            raise BrowserError(
                f"the new chat is not inside the configured project "
                f"({new_url!r} vs {project_url!r})"
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
        # ".../c/<id>" directly under the same /g/<slug> prefix.
        base = project_path.rsplit("/", 1)[0] if project_path.endswith("/project") else project_path
        return have.path.startswith(base + "/c/")

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

    def _park_ambiguous(self, req: PendingRequest, reconciled: bool) -> None:
        """Stop on an ambiguous submission. Never resends automatically."""
        self._log(
            "submission_ambiguous",
            request_id=req.request_id,
            data={"reconciled": reconciled, "reconcile_attempts": req.reconcile_attempts},
        )
        self._to_needs_user(
            f"submission of {req.request_id} is AMBIGUOUS: a send was attempted but "
            "the request is not in persisted history after reconciliation. Autoloop "
            "will not resend on its own — the backend may have accepted a message "
            "the browser never observed, so resending risks a duplicate post. "
            "Inspect the conversation, then either `run --retry` (reconcile again) "
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
            conversation_url=req.conversation_url,
            conversation_epoch=req.conversation_epoch,
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
        # `authorize_directive`'s protected-branch gate must judge THAT
        # destination, not the main checkout's — otherwise every
        # produce-then-review push would be evaluated against the wrong
        # branch name (denying it whenever the main checkout sits on
        # "main"/"master", the exact opposite of what protected_branches is
        # meant to gate).
        destination_branch = (
            resp.postcommit.task_branch
            if resp.postcommit is not None and directive.decision in PUSH_DECISIONS
            else self._git.current_branch()
        )
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
            self._log("stopped", data={"reason": directive.reason})
            self._store.save(state)
        elif decision is Decision.ASK_USER:
            state.last_response = None
            self._to_needs_user(
                directive.question or "(no question given)",
                kind="loop_fatal",
                code="ask_user",
            )
        elif decision is Decision.PLAN:
            self._dispatch_plan(directive)
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
            # review candidate — the bound case is handled above) was retired
            # 2026-07-30 (docs/SECURITY.md S21: closed by retirement, not by
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
                    "`push` with the `reviewed` stamp answering a postcommit "
                    "review packet",
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
        execution = self._execution_store.load(task.id)
        if execution is None:
            # First dispatch for this task: base sha is recorded BEFORE any
            # implementation work starts, from the MAIN checkout's HEAD (the
            # commit the task's branch forks from).
            base_sha = self._git.head_sha()
            if self._worker_repos is not None:
                repo = self._worker_repos.create(task.id, self._git.repo_root, base_sha)
                execution = TaskExecution(
                    task_id=task.id,
                    task_branch=repo.branch,
                    worktree_path=str(repo.path),
                    task_base_sha=base_sha,
                )
            else:
                execution = self._worktrees.create(task.id, base_sha)
            self._execution_store.save(execution)
        state.task_execution = asdict(execution)
        self._store.save(state)

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
                # lost. Adopt the branch head — do NOT commit again. Union the
                # recovered round's planned paths into the accumulated
                # ownership set (see `TaskExecution.allowed_paths`).
                execution.allowed_paths = tuple(
                    sorted(set(execution.allowed_paths) | set(pending_intent.planned_paths))
                )
                execution.candidate_sha = worktree_git.head_sha()
                self._execution_store.save(execution)
                self._intent_store.clear(task.id)
                state.task_execution = asdict(execution)
                self._store.save(state)
                self._finish_postcommit(execution, worktree_git, state, task)
                return
            # NO_COMMIT: the commit this intent describes never happened.
            # Clear the stale intent and fall through to attempt it fresh.
            self._intent_store.clear(task.id)

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
                    f"branch={execution.task_branch}"
                ),
            )
            return
        if execution.review_round >= 2:
            self._park_round_cap(execution, worktree_git, directive, state, task)
            return

        # Snapshot the environment (hooks / push destination) BEFORE the
        # executor runs, so a hook installed mid-task (e.g. by a dependency
        # postinstall script) is caught rather than silently trusted.
        env_snapshot = environment.snapshot(worktree_git)
        # Save before executing: a crash mid-execution resumes in `executing`
        # and re-dispatches the same directive (executors must tolerate that;
        # re-entering here re-loads the same `execution` record above).
        self._store.save(state)
        outcome = self._executor.execute(directive, task)
        state.last_validation = outcome.validation or "(none)"
        self._log(
            "executed",
            data={
                "decision": directive.decision.value,
                "task_id": task.id,
                "status": outcome.status,
                "summary": outcome.summary,
                "validation": outcome.validation,
            },
        )
        if outcome.status != "ok":
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

    def _verify_committed(
        self, execution: TaskExecution, worktree_git: GitGateway
    ) -> tuple[list[str], str]:
        """The post-commit review gate for `execution.candidate_sha`. Returns
        `(failures, validation_summary)`; `failures` empty means the commit
        passes every check.

        Path ownership is checked against `execution.allowed_paths` — the
        UNION of every round's `changed_paths` committed so far, not just the
        latest round's. `commit_range_paths(task_base_sha, candidate_sha)`
        spans the WHOLE range once `review_round > 0`, so comparing it against
        only the latest round's paths would wrongly flag an earlier round's
        legitimate paths as "outside" on a second review.
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
        outside = touched - set(execution.allowed_paths)
        if outside:
            failures.append(
                "commit touched path(s) outside what the task was allowed to "
                f"touch: {sorted(outside)}"
            )
        residual = worktree_git.dirty_entries()
        if residual:
            failures.append(
                "worktree is not clean after commit — residual change(s): "
                + ", ".join(f"{status} {path}" for status, path in residual)
            )
        validation_ok, validation_summary = self._run_post_commit_validation(
            execution.worktree_path
        )
        if not validation_ok:
            failures.append(f"post-commit validation failed: {validation_summary}")
        return failures, validation_summary

    def _run_post_commit_validation(self, worktree_path: str) -> tuple[bool, str]:
        """Re-run the SAME validation commands the audit executor uses
        (`config.audit.validation_commands`), against the task's own worktree,
        AFTER the commit exists. Pre-commit validation (`outcome.validation`,
        whatever the executor itself reports) is not sufficient: a commit
        hook can change committed content in ways the executor never saw."""
        return run_validation_commands(
            self._config.audit.validation_commands,
            Path(worktree_path),
            command_runner=self._validation_runner,
        )

    def _finish_postcommit(
        self,
        execution: TaskExecution,
        worktree_git: GitGateway,
        state: LoopState,
        task: Task,
    ) -> None:
        execution.attempt_count += 1
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
            },
        )
        state.last_response = None
        if failures:
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
            packet_text = build_review_packet(execution, worktree_git, task)
        except GitCommandError as exc:
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
        self._execution_store.save(execution)
        state.task_execution = asdict(execution)
        state.outbox = TEMPLATES["postcommit_review"].render(
            task_id=task.id, task_title=task.title, packet=packet_text
        )
        state.consecutive_failures = 0
        state.phase = Phase.READY.value
        self._store.save(state)

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
        if not worktree_git.is_descendant(binding.candidate_sha, execution.task_base_sha):
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
                self._to_needs_user(
                    f"task {binding.task_id}: push of {binding.candidate_sha[:12]} "
                    f"to {remote}/{dest_ref} was REFUSED — {exc}. Nothing was "
                    "pushed; the commit itself is unaffected.",
                    kind="loop_fatal",
                    code="push_refused",
                    task_id=binding.task_id,
                    detail=f"remote={remote} dest_ref={dest_ref} error={exc}",
                )
                return
        execution.candidate_commit_count = len(
            worktree_git.commit_list(execution.task_base_sha, execution.candidate_sha)
        )
        self._execution_store.save(execution)
        # Clear `state.task_execution` now that the candidate is actually
        # published — NOT just re-mirror it. It is display/tracking state
        # only now (the S21 retirement removed the legacy-push guard that
        # used to read it to decide whether a fall-through push was safe —
        # see docs/SECURITY.md S21); clearing it keeps `status`/`_summary`
        # honest about there being nothing left "awaiting publication". The
        # `TaskExecutionStore` record on disk is untouched.
        state.task_execution = None
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
            self._to_needs_user(
                f"{budget.reason} — last denial: {verdict.reason}",
                kind="loop_fatal",
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

    def _handle_browser_failure(self, phase: Phase, exc: BrowserError) -> None:
        state = self.state
        state.consecutive_failures += 1
        self._log(
            "browser_error",
            data={"phase": phase.value, "error": str(exc), "kind": type(exc).__name__},
        )
        self._drop_client()
        verdict = self._policy.check_failure_budget(state.consecutive_failures)
        if not verdict.allowed:
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
            )
            state.park_blocker_id = blocker.id
        self._store.save(state)

    def _get_client(self):
        if self._client is None:
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
