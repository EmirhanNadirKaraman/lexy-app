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
        state.pending_request = PendingRequest(
            request_id=request_id,
            payload=state.outbox,
            prompt=prompt,
            head_sha=ctx.head_sha,
            base_sha=ctx.base_sha,
            report_sha256=ctx.report_sha256,
            timestamp=ctx.timestamp,
        )
        if carries_block:
            # Record WHICH reviewed report carried the table. A later approval
            # must echo this same report_sha256, so an approval answering any
            # other report can never authorize this adoption.
            adopted.presented_report_sha256 = ctx.report_sha256
            self._manifest_store.save(adopted)
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

    def _current_adopted_manifest(self) -> ChangeManifest | None:
        """The adopted manifest awaiting review, if the current one is adopted."""
        if self.state.last_manifest_id is None:
            return None
        manifest = self._manifest_store.load(self.state.last_manifest_id)
        if manifest is None or not manifest.is_adopted():
            return None
        return manifest

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

    # ---- produce-then-review commit path (pass 2a) --------------------------
    #
    # A real task runs in its own worktree/branch (`WorktreeManager`) and
    # commits immediately after the executor reports success and the commit
    # passes structural + re-run-validation checks — never gated on a prior
    # chat approval, because none exists for this path. On ANY check failure
    # the commit is left exactly where it is: nothing here can roll it back
    # (reset/checkout/clean are not on the git command whitelist), so refusal
    # means "park and report", never "undo". Building the actual review
    # packet (the message that shows ChatGPT the diff and lets it request a
    # revision) is explicitly OUT of scope for this pass — see the module
    # docstring in `worktree.py`/`worktask.py`. A clean pass therefore also
    # parks today, with an honest "awaiting review, not wired yet" message,
    # rather than inventing that content early.

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
                # lost. Adopt the branch head — do NOT commit again.
                allowed_paths = set(pending_intent.planned_paths)
                execution.candidate_sha = worktree_git.head_sha()
                self._execution_store.save(execution)
                self._intent_store.clear(task.id)
                state.task_execution = asdict(execution)
                self._store.save(state)
                self._finish_postcommit(execution, worktree_git, allowed_paths, state, task)
                return
            # NO_COMMIT: the commit this intent describes never happened.
            # Clear the stale intent and fall through to attempt it fresh.
            self._intent_store.clear(task.id)

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
        execution.candidate_sha = candidate_sha
        self._execution_store.save(execution)
        self._intent_store.clear(task.id)
        state.task_execution = asdict(execution)
        self._store.save(state)
        self._log(
            "staged_diff", data={"task_id": task.id, "candidate_sha": candidate_sha, "summary": staged_summary}
        )
        self._finish_postcommit(execution, worktree_git, set(outcome.changed_paths), state, task)

    def _verify_committed(
        self, execution: TaskExecution, worktree_git: GitGateway, allowed_paths: set[str]
    ) -> tuple[list[str], str]:
        """The post-commit review gate for `execution.candidate_sha`. Returns
        `(failures, validation_summary)`; `failures` empty means the commit
        passes every check.

        NOTE — round > 0 (a revision on top of an already-reviewed commit) is
        not handled correctly here: `commit_range_paths(task_base_sha,
        candidate_sha)` spans the WHOLE base..candidate range, so on a second
        round it would also include the FIRST round's paths, not just this
        round's. The ownership set would need to be a union across rounds (or
        a diff from the round's own parent) to be correct there. Revision
        rounds are the review loop and are explicitly out of scope for this
        pass — round 0 (every scenario this method is exercised against here)
        is unaffected because `task_base_sha` and `execution.candidate_sha`'s
        only parent are the same commit.
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
        outside = touched - allowed_paths
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
        allowed_paths: set[str],
        state: LoopState,
        task: Task,
    ) -> None:
        failures, validation_summary = self._verify_committed(execution, worktree_git, allowed_paths)
        state.last_validation = validation_summary
        execution.review_round += 1
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
        self._to_needs_user(
            f"task {task.id}: commit {execution.candidate_sha[:12]} on "
            f"{execution.task_branch} (round {execution.review_round}) passed "
            "post-commit review. Awaiting a review packet for ChatGPT to see "
            "the diff — that construction is not implemented yet; park here "
            "for the operator in the meantime."
        )

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
