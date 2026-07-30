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

Failure routing:

* LoginExpiredError            → needs_user, resume_phase preserved (--retry)
* other BrowserError           → drop conversation, retry same phase; failure
                                 budget exhausted → failed (resume via --retry)
* GitError                     → reported back to ChatGPT (budget-capped);
                                 in `ready` (context build) → needs_user with
                                 the outbox preserved
* ContractError (parse)        → corrective re-prompt; budget-capped
* review-integrity mismatch    → failure_recovery re-prompt; denial budget
* policy denial / plan reject  → failure_recovery re-prompt; denial budget
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path

from . import environment
from .browser.chatgpt import SubmitResult
from .config import AutoloopConfig
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
    BrowserError,
    ContractError,
    GitCommandError,
    GitError,
    LoginExpiredError,
    ManifestViolation,
    StateError,
    TaskGraphError,
)
from .manifest import (
    ChangeManifest,
    ManifestStore,
    render_adoption_block,
    snapshot,
    verify_commit,
    verify_tree_content,
)
from .executor import TaskExecutor
from .git_gateway import GitGateway
from .packet import build_review_packet
from .policy import PolicyEngine
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
        validation_runner=None,
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
        self._manifest_store = manifest_store
        # Produce-then-review commit path (pass 2a). ALL THREE optional and
        # gated together: when `worktrees` is None (every existing caller and
        # test), `_dispatch_executor` takes the old authorize-then-produce/
        # manifest branch unchanged for every decision, audit included. When
        # set, a real (non-audit) task runs in its own worktree and commits
        # automatically once validation passes — see `_dispatch_task_postcommit`.
        self._worktrees = worktrees
        self._execution_store = execution_store
        self._intent_store = intent_store
        #: Injected `subprocess.run`-compatible callable for post-commit
        #: validation, mirroring `AuditExecutor`'s `command_runner` — lets
        #: tests avoid depending on a real `ruff`/`pytest` install.
        self._validation_runner = validation_runner
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
                self._log("login_expired", data={"error": str(exc)})
                self._drop_client()
                self._to_needs_user(str(exc), resume_phase=phase.value)
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
            self._to_needs_user(verdict.reason, resume_phase=Phase.READY.value)
            return
        if state.outbox is None:
            raise StateError("phase=ready but outbox is empty — nothing to send")
        request_id = f"alr-{state.session_id[:8]}-{next_iteration:04d}"
        adopted = self._current_adopted_manifest()
        # Bind the hash table to the bytes being reviewed, but only when the
        # payload actually carries it. Refusing to SEND otherwise would deadlock
        # the loop: error re-prompts and other template payloads legitimately
        # carry no adoption block, and the loop must still be able to report a
        # refusal. The load-bearing check is at commit time — an approval that
        # answers a report which did not carry the table is refused there,
        # either as "never presented" or as a stale-report mismatch.
        carries_block = (
            adopted is not None and render_adoption_block(adopted) in state.outbox
        )
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
        if carries_block:
            # Record WHICH reviewed report carried the table. A later approval
            # must echo this same report_sha256, so an approval answering any
            # other report can never authorize this adoption.
            adopted.presented_report_sha256 = ctx.report_sha256
            self._manifest_store.save(adopted)
        if postcommit is not None:
            # Mirror the adoption stamp above, on the TaskExecution record
            # instead of a ChangeManifest: bind the exact report this
            # candidate was reviewed under, so a later approval answering a
            # DIFFERENT report can never authorize publishing it.
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
        client = self._get_client()
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
            # it did not persist. Resending is forbidden without an explicit
            # operator decision (`run --resubmit`): the backend may have
            # accepted a message the browser never observed.
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
        client = self._get_client()
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

    def _current_adopted_manifest(self) -> ChangeManifest | None:
        """The adopted manifest awaiting review, if the current one is adopted."""
        if self.state.last_manifest_id is None:
            return None
        manifest = self._manifest_store.load(self.state.last_manifest_id)
        if manifest is None or not manifest.is_adopted():
            return None
        return manifest

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
        )

    def _step_awaiting(self) -> None:
        state = self.state
        req = state.pending_request
        if req is None:
            raise StateError("phase=awaiting but no pending request")
        client = self._get_client()
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
            self._to_needs_user(directive.question or "(no question given)")
        elif decision is Decision.PLAN:
            self._dispatch_plan(directive)
        elif decision is Decision.PUSH and state.last_response is not None and (
            state.last_response.postcommit is not None
        ):
            # A push answering a produce-then-review packet publishes via
            # `push_exact`, sourced entirely from the response's binding
            # (never from `directive` — see `_dispatch_task_push`'s
            # docstring). `commit_and_push` deliberately does NOT take this
            # branch: there is nothing new to commit here (the commit already
            # exists), so a `commit_and_push` reply falls through to
            # `_dispatch_git`, whose manifest gate refuses it with a clear
            # "no change manifest recorded" error — a more honest response
            # than silently reinterpreting it as a bare push.
            self._dispatch_task_push(directive, state.last_response)
        elif decision in COMMIT_DECISIONS or decision in PUSH_DECISIONS:
            self._dispatch_git(directive)
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
                self._to_needs_user(f"{budget.reason} — last plan rejection: {exc}")
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
        state = self.state
        task = None
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
            state.current_task = {
                "task_id": AUDIT_TASK_ID,
                "title": "repository audit",
                "decision": directive.decision.value,
                "started_at": utcnow_iso(),
            }

        if task is not None and self._worktrees is not None:
            # Produce-then-review commit path. Audit never takes this branch
            # (task is always None for it) — it keeps the manifest-based path
            # below unconditionally, report content and all.
            self._dispatch_task_postcommit(directive, task, state)
            return

        # Task-owned change manifest: snapshot the dirty tree BEFORE the task.
        # The manifest id is stable across a crash-redispatch (same iteration),
        # so recovery overwrites the same manifest instead of forking it.
        manifest_id = f"{task.id if task else AUDIT_TASK_ID}-i{state.iteration:04d}"
        manifest = ChangeManifest.begin(manifest_id, task.id if task else AUDIT_TASK_ID, self._git)
        self._manifest_store.save(manifest)
        state.current_task["manifest_id"] = manifest_id
        # Save before executing: a crash mid-execution resumes in `executing`
        # and re-dispatches the same directive (executors must tolerate that).
        self._store.save(state)
        outcome = self._executor.execute(directive, task)
        manifest.finish(snapshot(self._git))
        self._manifest_store.save(manifest)
        state.last_manifest_id = manifest_id
        self._log(
            "manifest",
            data={
                "manifest_id": manifest_id,
                "created": manifest.created,
                "modified": manifest.modified,
                "deleted": manifest.deleted,
            },
        )
        state.last_validation = outcome.validation or "(none)"
        self._log(
            "executed",
            data={
                "decision": directive.decision.value,
                "task_id": task.id if task else None,
                "status": outcome.status,
                "summary": outcome.summary,
                "validation": outcome.validation,
            },
        )
        if task is None:  # audit, or revise of the audit — both yield a report
            report = outcome.summary + ("\n\n" + outcome.details if outcome.details else "")
            state.outbox = TEMPLATES["audit"].render(report=report)
        elif outcome.status == "ok":
            state.outbox = TEMPLATES["commit_approval"].render(
                task_id=task.id,
                task_title=task.title,
                summary=outcome.summary,
                details=outcome.details,
                validation=outcome.validation or "(none)",
            )
        else:
            state.outbox = TEMPLATES["implementation_review"].render(
                task_id=task.id if task else "(none)",
                task_title=task.title if task else "(none)",
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

    def _dispatch_task_postcommit(self, directive: Directive, task: Task, state: LoopState) -> None:
        execution = self._execution_store.load(task.id)
        if execution is None:
            # First dispatch for this task: base sha is recorded BEFORE any
            # implementation work starts, from the MAIN checkout's HEAD (the
            # commit the task's branch forks from).
            base_sha = self._git.head_sha()
            execution = self._worktrees.create(task.id, base_sha)
            self._execution_store.save(execution)
        state.task_execution = asdict(execution)
        self._store.save(state)

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
                    "commit intent once resolved."
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
        except GitCommandError as exc:
            # Environment drift (a hook installed mid-task), HEAD drift, or an
            # empty path list. Every OTHER refusal in this path parks for the
            # operator, so this one does too rather than escaping as a raw
            # error: the commit did not happen, nothing was rolled back, and a
            # human needs to look at why the task's environment moved.
            self._intent_store.clear(task.id)
            state.last_response = None
            self._to_needs_user(
                f"task {task.id}: the commit was refused before it happened — "
                f"{exc}. Nothing was committed and nothing was rolled back."
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
            f"\n{latest_diff}"
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
                f"reachable through this gateway. Reasons: {'; '.join(failures)}"
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
                "nothing was sent to ChatGPT."
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
        """
        state = self.state
        binding = resp.postcommit
        execution = self._execution_store.load(binding.task_id)
        if execution is None or execution.candidate_sha != binding.candidate_sha:
            self._to_needs_user(
                f"task {binding.task_id}: push refused — the reviewed candidate "
                f"{binding.candidate_sha[:12]} is no longer this task's current "
                "candidate (a later round advanced it, or the execution record "
                "is gone). Nothing was pushed; re-review the current state "
                "before approving again."
            )
            return
        worktree_git = GitGateway(Path(execution.worktree_path), self._policy)
        if not worktree_git.is_descendant(binding.candidate_sha, execution.task_base_sha):
            self._to_needs_user(
                f"task {binding.task_id}: push refused — candidate "
                f"{binding.candidate_sha[:12]} is not a descendant of task base "
                f"{execution.task_base_sha[:12]}. Nothing was pushed."
            )
            return
        try:
            info = worktree_git.read_commit(binding.candidate_sha)
        except GitCommandError as exc:
            self._to_needs_user(
                f"task {binding.task_id}: push refused — the reviewed candidate "
                f"{binding.candidate_sha[:12]} no longer resolves: {exc}. Nothing "
                "was pushed."
            )
            return
        if info.get("tree") != binding.candidate_tree_sha:
            self._to_needs_user(
                f"task {binding.task_id}: push refused — candidate "
                f"{binding.candidate_sha[:12]}'s tree changed since it was "
                f"reviewed (was {binding.candidate_tree_sha[:12]}, now "
                f"{info.get('tree', '?')[:12]}). Nothing was pushed."
            )
            return

        remote = execution.intended_remote or "origin"
        dest_ref = f"refs/heads/{binding.task_branch}"
        # Durable push intent, recorded BEFORE the network call — mirrors
        # `CommitIntent`'s "write before the risky operation" pattern, so a
        # crash between a successful `git push` and this method returning is
        # recoverable from the remote ref alone rather than re-pushed.
        execution.intended_remote = remote
        execution.intended_remote_ref = dest_ref
        self._execution_store.save(execution)

        landed = worktree_git.remote_ref_sha(remote, dest_ref)
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
                    "pushed; the commit itself is unaffected."
                )
                return
        execution.candidate_commit_count = len(
            worktree_git.commit_list(execution.task_base_sha, execution.candidate_sha)
        )
        self._execution_store.save(execution)
        # Clear `state.task_execution` now that the candidate is actually
        # published — NOT just re-mirror it. `_dispatch_git`'s legacy-push
        # guard refuses whenever `state.task_execution` shows a live
        # `candidate_sha` with no matching binding on the current response;
        # leaving the just-published candidate there would make that guard
        # refuse EVERY later legacy push for the rest of the session (e.g. an
        # unrelated audit's `commit_and_push`), forever, since nothing else
        # ever clears it. The `TaskExecutionStore` record on disk is
        # untouched — this only clears the in-memory "awaiting publication"
        # marker other dispatch paths read.
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

    def _dispatch_git(self, directive: Directive) -> None:
        state = self.state
        actions: list[str] = []
        commit_sha = None
        if directive.decision in COMMIT_DECISIONS:
            # Task-owned change-manifest gate: the approved paths must be
            # exactly work the last executed task produced. Violations raise
            # ManifestViolation (a GitError) and are reported back to ChatGPT.
            if state.last_manifest_id is None:
                raise ManifestViolation(
                    "commit refused: no change manifest recorded — no task has "
                    "produced changes in this session"
                )
            manifest = self._manifest_store.load(state.last_manifest_id)
            if manifest is None:
                raise ManifestViolation(
                    f"commit refused: manifest {state.last_manifest_id} is missing"
                )
            if manifest.is_adopted():
                # The approval must answer the very request that presented this
                # adoption. Without this, an approval bound to an older report
                # could authorize a table it never saw.
                answered = state.last_response.report_sha256 if state.last_response else None
                if manifest.presented_report_sha256 is None:
                    raise ManifestViolation(
                        f"commit refused: adopted manifest {manifest.manifest_id} was "
                        "never presented for review — no report carried its hash table, "
                        "so nothing about this content has been approved"
                    )
                if manifest.presented_report_sha256 != answered:
                    raise ManifestViolation(
                        "commit refused: adopted manifest "
                        f"{manifest.manifest_id} was presented in report "
                        f"{manifest.presented_report_sha256[:12]}… but the approval "
                        f"answers report {(answered or '(none)')[:12]}… — a stale "
                        "approval cannot authorize this adoption"
                    )
            violations = verify_commit(manifest, directive.commit_paths or (), self._git)
            if violations:
                raise ManifestViolation("commit refused: " + "; ".join(violations))

            if manifest.is_adopted():
                # Immutable-tree path. `git commit` is not used: its hooks can
                # rewrite the index after any check, and a reproduced attack
                # showed a pre-commit hook replacing approved bytes AND adding
                # an unapproved file to the commit. The tree object verified
                # here is the object committed.
                def _verify_tree(tree: str, parent_tree: str) -> list[str]:
                    return verify_tree_content(
                        manifest, directive.commit_paths or (), self._git, tree, parent_tree
                    )

                commit_sha, staged_summary, residual = self._git.commit_adopted(
                    directive.commit_message or "", directive.commit_paths, _verify_tree
                )
                already = False
                if residual:
                    self._log("residual_changes", data={"paths": residual})
                    actions.append(
                        "residual uncommitted changes remain (not reset): "
                        + ", ".join(residual)
                    )
            else:
                commit_sha, already, staged_summary = self._git.commit(
                    directive.commit_message or "", directive.commit_paths
                )
            state.reviewed_commit = commit_sha
            self._log(
                "staged_diff",
                data={"manifest_id": manifest.manifest_id, "summary": staged_summary},
            )
            actions.append(
                f"commit {'already existed (recovered)' if already else 'created'}: "
                f"{commit_sha}"
                + (f"\nstaged diff:\n{staged_summary}" if staged_summary else "")
            )
            if directive.task_id:
                try:
                    self._registry.mark_completed(directive.task_id)
                    self._task_store.save(self._registry)
                    actions.append(f"task {directive.task_id} marked completed")
                except TaskGraphError as exc:
                    if exc.code != "task_completed":  # already done = crash recovery
                        raise
        if directive.decision in PUSH_DECISIONS:
            # Fail closed rather than publish the wrong destination: a
            # produce-then-review candidate is on record (state.task_execution
            # carries a real candidate_sha) but THIS response carries no
            # matching postcommit binding — either the packet that presented
            # it was never sent (a parse-error/policy-denial re-prompt
            # overwrote the outbox first) or this response answers a
            # different, unrelated request entirely. Either way, pushing
            # "whatever the main checkout's current branch is" here would
            # publish the wrong branch. `_dispatch` already routes a
            # postcommit-bound `push` to `_dispatch_task_push` before this
            # method is ever reached, so reaching here WITH a live candidate
            # and WITHOUT a binding is exactly the mismatch case.
            task_exec = state.task_execution or {}
            resp = state.last_response
            if task_exec.get("candidate_sha") and not (
                resp is not None and resp.postcommit is not None
            ):
                self._to_needs_user(
                    "refusing to push through the legacy git path: a "
                    f"produce-then-review candidate ({task_exec.get('candidate_sha', '')[:12]}"
                    f" on task {task_exec.get('task_id')!r}) is on record but this "
                    "response carries no matching review binding — publishing "
                    "the main checkout's current branch here could be the wrong "
                    "destination. Nothing was pushed."
                )
                return
            push_sha = self._git.head_sha()
            current_branch = self._git.current_branch()
            if not current_branch:
                raise GitCommandError("cannot push: detached HEAD")
            dest_ref = f"refs/heads/{current_branch}"
            # `authorize_directive` already gated protected-branch pushes on
            # `allow_protected_push` before this point was ever reached, so
            # `push_exact`'s OWN protected-ref check (which has no such
            # escape hatch — see its docstring) must be told the same thing,
            # or `allow_protected_push=True` would authorize the push at the
            # policy layer and then have `push_exact` refuse it anyway,
            # silently making that config knob inert for this path.
            gateway_protected = (
                () if self._policy.config.allow_protected_push
                else self._policy.config.protected_branches
            )
            landed = self._git.push_exact("origin", push_sha, dest_ref, gateway_protected)
            actions.append(f"pushed {landed[:12]} to origin/{dest_ref}")
        summary = "; ".join(actions)
        self._log("git_action", data={"decision": directive.decision.value, "summary": summary})
        if directive.decision is Decision.COMMIT:
            message_line = (directive.commit_message or "").splitlines()[0]
            state.outbox = TEMPLATES["push_approval"].render(
                commit_sha=(commit_sha or "")[:12], commit_message=message_line
            )
        else:
            state.outbox = TEMPLATES["git_report"].render(summary_line=summary)
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
                "then answer with `run --answer '<message to ChatGPT>'`."
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
            self._to_needs_user(f"{budget.reason} — last denial: {verdict.reason}")
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
            self._to_needs_user(f"{budget.reason} — last review mismatch: {exc}")
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
            self._to_needs_user(f"repeated git failures — last error: {exc}")
            return
        state.outbox = git_error_payload(decision, str(exc))
        state.last_response = None
        state.phase = Phase.READY.value
        self._store.save(state)

    # ---- helpers ------------------------------------------------------------

    def _to_needs_user(self, question: str, resume_phase: str | None = None) -> None:
        state = self.state
        state.question = question
        state.resume_phase = resume_phase
        state.phase = Phase.NEEDS_USER.value
        self._log("needs_user", data={"question": question, "resume_phase": resume_phase})
        self._store.save(state)

    def _get_client(self):
        if self._client is None:
            self._client = self._client_factory()
        return self._client

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
