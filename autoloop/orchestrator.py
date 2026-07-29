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
    GitError,
    LoginExpiredError,
    ManifestViolation,
    StateError,
    TaskGraphError,
)
from .manifest import ChangeManifest, ManifestStore, snapshot, verify_commit
from .executor import TaskExecutor
from .git_gateway import GitGateway
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
    StateStore,
    utcnow_iso,
)
from .tasks import Task, TaskRegistry, TaskStore
from .transcript import TranscriptLogger


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
        ctx = build_context(state, self._git, self._registry, request_id, state.outbox)
        prompt = build_prompt(request_id, next_iteration, render_context(ctx), state.outbox)
        state.pending_request = PendingRequest(
            request_id=request_id,
            payload=state.outbox,
            prompt=prompt,
            head_sha=ctx.head_sha,
            base_sha=ctx.base_sha,
            report_sha256=ctx.report_sha256,
            timestamp=ctx.timestamp,
        )
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
        verdict = self._policy.authorize_directive(
            directive, self._git.current_branch(), self._registry
        )
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
            violations = verify_commit(manifest, directive.commit_paths or ())
            if violations:
                raise ManifestViolation("commit refused: " + "; ".join(violations))
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
            output = self._git.push()
            actions.append("pushed current branch" + (f": {output}" if output else ""))
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
