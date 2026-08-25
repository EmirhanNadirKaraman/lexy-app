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
  PARKS the loop (`send_rejected_twice`, `conversation_unusable`) — see
  "Conversation rotation is gone" below for what used to happen instead.
* Every request carries its OWN authoritative `conversation_url` and
  `conversation_epoch`. Submitting, awaiting and reconciling all follow the
  request's URL, never the loop's current one, so a late reply in an abandoned
  chat cannot authorize anything.
* A CONFIRMED, persisted send whose assistant turn never begins generating
  (`ResponseTimeoutError` with `stage="start"`) is counted per request
  (`PendingRequest.start_timeouts` / `start_timeout_wait_seconds`) and then
  handed to the ordinary failure budget like any other transport fault. The
  counters are diagnostic: they used to earn a third recovery, and nothing
  acts on them now (see below).

Conversation rotation is gone (brw-15, 2026-08-25):

* Until this change a wedged conversation could `rotate`: open one fresh chat in
  `browser.project_url`, post the in-flight request into it, prove the chat
  holds it, and rebind the loop. `_attempt_rotation`, `_attempt_silence_rotation`,
  `_rotate_conversation`, `_park_rotation` and their helpers are removed, and
  with them the blocker codes only they could raise — `rotation_unavailable`,
  `rotation_cap_reached`, `rotation_failed` and
  `rotation_unsupported_by_transport`.
* Why: rotation is a ChatGPT-project concept end to end, and the DEFAULT
  transport is a subprocess with no conversation to rotate away from — see
  `conversation._BROWSER_BACKED`, which is where this module is allowed to know
  a provider by name and this docstring is not. Two disproven sends on such a
  transport reached the machinery without passing any fault handler, and 11 of
  the loop's first 103 blocker records were `rotation_failed` — a recovery that
  parked more runs than it saved. Parking is what a wedged conversation gets
  now, and moving the loop to a fresh chat is an operator action
  (`browser.conversation_url` + `reset`).
* What stays: `state.rotations`, `state.last_rotation` and
  `policy.max_conversation_rotations` are untouched in `state.py` / `policy.py`,
  so `cli._drift_is_recorded_rotation` still recognises a config/state
  disagreement left by a rotation an OLDER process performed. Nothing in this
  module increments the counter any more, so on a state file written from here
  it reads 0.
* `autoloop/browser/` still ships `retarget`/`current_url`/
  `find_conversation_with`. `find_conversation_with` is live — the by-content
  presence search (`_search_for_request`) uses it; the other two are now only
  used by the browser adapter itself.

Urgent preemption (2026-08-22):

* An operator can take the loop NOW without waiting for the round in flight, by
  marking a task urgent through the inbox (`inbox.KIND_URGENT` ->
  `TaskRegistry.request_urgent`). The loop observes the pin between steps and
  acts on it only at `_at_round_boundary` — `ready` with no pending request,
  the SAME instant a self-upgrade may replace the process — so a request that
  arrives while a review packet is outstanding waits rather than stranding it.
* Acting on it means: return the displaced task to pending through the one
  release path (`release_task_to_pending`, shared with `cli._cmd_release`, so
  the status, the worker repo and the execution record always move together),
  record what was displaced (`LoopState.preemption` + a `task_preempted`
  transcript entry), and end the round as a `stopped` session with
  `stop_kind = PREEMPTION_STOP_KIND` — a CLEAN boundary, which is why every
  existing caller of `run()` needs no new branch to handle it.
* The urgent task is then the next unit of work actually DISPATCHED, not merely
  the next one offered: `cli._start_new_session` opens the session a preemption
  starts on that task (`cli.urgent_kickoff_payload`) instead of on the audit
  kickoff, and `_refused_ahead_of_urgent` refuses every other implement/revise
  AND a fresh audit until it has started. A `revise` continuing an audit arc
  already in flight is the one exemption, and it cannot follow a preemption —
  see that method.
* The review gate is untouched. Nothing here skips a packet, assumes a verdict
  or authorizes a push; the displaced candidate stays in its quarantined worker
  repo. The only enforcement added is the dispatch refusal above, through the
  ordinary policy-denial re-prompt.

Note for the merge with the blocker/quarantine work (commit 5346551, branch
`feat/autoloop-postcommit-review`): every park site added here goes through the
existing two-argument `_to_needs_user` and emits a stable `reason_code` in its
transcript event (`submission_unknown`, `send_rejected_twice`,
`conversation_unusable`). When the two lines meet, classifying them is a matter
of passing the kind that matches each code — no new taxonomy is invented here.

Failure routing:

Every transport fault below arrives as a `BrowserError` subclass — the hierarchy
in `errors.py` is named after the first implementation, not after the subsystem
that failed — so before any of it applies, `_route_transport_fault` asks whether
the ACTIVE PROVIDER is browser-backed (`conversation.transport_is_browser_backed`).
It is for the browser provider, and for that one nothing changes. For every other
transport the fault goes to `_handle_transport_failure` instead, which runs no
`restart_command`, never touches `browser_restart_skips` or
`policy.max_browser_restart_skips`, writes `transport_error` rather than
`browser_error`, and — on the ordinary failure budget running out — parks
(`transport_failure_budget_exhausted`) with a remedy for the transport actually
in use. On 2026-08-22 the absence of that split answered a `codex exec` fault by
launching Chrome, spending the browser recovery budget, and parking with advice
to restart a browser the run does not use.

One additional move belongs to the non-browser side and to nothing else: while
`awaiting`, a transport that declares `idempotent_submit` and whose own
`reconcile` confirms the reply is gone re-enters `submitting` and RE-RUNS the
invocation (`_replay_unrecoverable_await`, bounded by `MAX_AWAIT_REPLAYS`).
A transport without that declaration is never re-run automatically.

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
* ResponseTimeoutError(start)  → ordinary budget as below. The per-request
                                 timeout counters are still kept (see above);
                                 nothing acts on them since brw-15
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

Autonomous recovery (halt-02, 2026-08-25):

* A CONFIG FLAG, DEFAULT OFF (`config.AutonomyConfig`). With it off — every
  existing deployment, every direct `AutoloopConfig(...)` — every park below
  behaves exactly as it did, and turning it off again restores that.
* With it on, `_to_needs_user` gives an ALLOWLISTED code two stages instead of
  one park: re-enter the recovery path the loop already has (`--retry`'s phase
  re-entry, or `--resubmit`'s same-id re-issue), bounded by a budget counted on
  the durable blocker record; and, once that path is exhausted, park
  `task_fatal` naming the task in flight so `cli._handle_parked_task` sets that
  ONE task aside and the loop carries on. The allowlist, the per-code budgets
  and the argument for each are `blockers.AUTONOMOUS_RECOVERIES`; the five
  parks it may never reach are `blockers.HARD_HALT_CODES`.
* halt-02 named SEVEN codes; the table holds SIX. The missing one is the
  conversation-rotation failure this file's brw-15 section describes, which has
  had no producer since that change — a code no live provider can raise is
  removed rather than automated, so it is absent by decision.
* Three of those six get a budget of ZERO, and that is the honest answer rather
  than a gap: `worker_environment_drift`, `publisher_url_drift` and
  `crash_reconciliation_ambiguous` have no in-process recovery path at all —
  their remedies are an operator repairing the shared git environment,
  confirming a new push destination, and clearing a commit intent by hand, none
  of which the loop may perform for itself. An empty recovery path is exhausted
  immediately, so the set-aside fires at once.

Repeated stops (stop-01, 2026-08-23):

* A reviewer's `stop` is a VERDICT, not a failure, so no budget ever counted
  one. On 2026-08-20 that let a loop refuse-and-restart three times in fifteen
  minutes over one lost postcommit binding while `health` reported `running`,
  `open_blockers: 0`, `needs_attention: FALSE` the whole time. `stop` ends the
  session, `--continuous` opens another, the kickoff draws the same refusal.
* `_handle_contract_stop` now charges every stop to a durable ledger
  (`state.StopRepetitionStore`) keyed by `_stop_situation_fingerprint` — the
  SITUATION being stopped about, not the wording of the refusal. The
  `MAX_REPEATED_STOPS`-th consecutive stop about one unchanged situation parks
  `loop_fatal` (`code="stop_livelock"`) with the reviewer's last reason quoted
  verbatim, so the blocker machinery, `health`, the monitor and `answer` all
  behave exactly as they do for any other park. Anything that changes the
  situation — a published candidate, a completed task, a registry mutation, a
  new execution record — restarts the count with no special case.
* ONE stop is unchanged in every respect: same `stopped` phase, same
  `stop_kind="contract"`, same clean boundary for continuous mode.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

from . import environment
from . import escape_detector
from . import note_merge
from .auto_merge import (
    UPGRADE_PENDING,
    AutoMerger,
    MergeDeferral,
    MergeDeferralStore,
    UpgradeStore,
)
from .blockers import (
    NO_TASK,
    RECOVER_BY_RESUBMITTING,
    RECOVER_BY_RESUMING,
    STRANDED_AFTER_FAULT,
    BlockerStore,
    autonomous_recovery,
)
from .browser.chatgpt import SubmitResult
from .browser.observation import SendOutcome
from .browser.playwright_session import attachable_page_targets
from .changeset_review import ChangesetBinding
from .config import AutoloopConfig
from .context import build_context, render_context
from .conversation import transport_is_browser_backed, transport_remedy
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
from .health import (
    StrandedRound,
    current_round_age_seconds,
    round_ceiling_for,
    stranded_fault_rounds,
)
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
    StateStore,
    StopRepetitionStore,
    postcommit_binding_from_record,
    stop_repetition_file,
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
    Retirement,
    SplitIntent,
    SplitIntentStore,
    TaskExecution,
    TaskExecutionStore,
    accumulate_assumptions,
    attempt_outcome,
    compose_reason,
    format_attempt,
    reconcile_after_crash,
    reconcile_split_acceptance,
    retire_execution,
    split_attempt,
    split_intents_dir,
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

#: How many times a reviewer `recut` may discard ONE task's execution and send
#: it back to be cut again from the base. The cut after this parks for a human
#: (`Orchestrator._park_recut_cap`) instead of recutting again.
#:
#: TWO, and the number is an argument rather than a taste. A recut is the
#: reviewer's claim that the BRANCH is the problem — contaminated history, work
#: far outside scope, a structural dead end — and a fresh cut from the current
#: base is the complete remedy for that claim. One clean rebuild that still
#: cannot produce a reviewable candidate is ordinary bad luck (a base that moved
#: under it, an agent round that died); TWO is the point at which "the branch was
#: contaminated" stops explaining the evidence, because the second cut shared
#: nothing with the first except the task's own description, scope and approved
#: plan. What is left is the SPECIFICATION, and no third branch fixes a spec.
#:
#: Three was the alternative and is rejected for what it costs: each cut is a
#: full executor round plus a review round on work that is then thrown away, so
#: a third buys one more identical experiment at the price of the operator
#: attention this cap exists to summon. One was rejected in the other direction
#: — it makes the first bad round unrecoverable and pushes every recut into the
#: park it is meant to avoid.
#:
#: Counted on `tasks.Task.recut_count` (durable; a recut archives the execution
#: record, so a count kept only there would reset to 0 on every cut) and
#: mirrored onto `worktask.TaskExecution.recut_count`. `_recut_count_for` reads
#: the HIGHER of the two.
MAX_TASK_RECUTS = 2

#: The `retire_execution` label a recut's execution record and worker repo are
#: filed under, so the two halves name each other on disk as
#: `<task>-recut-by-reviewer-<stamp>` and a human reading either one can tell at
#: a glance that the REVIEWER discarded the round, rather than an operator
#: releasing it by hand or an urgent request displacing it.
RECUT_RETIREMENT_REASON = "recut-by-reviewer"

# ---- the attempt ceiling's classification bounds (ceil-01, 2026-08-25) ------
#
# A task that reaches `MAX_TASK_ATTEMPTS` no longer parks for a human on the
# ceiling alone: it asks the REVIEWER — which already holds the candidate and
# the verdict history — to classify it, and the reply decides between a named
# remaining fix on an extended budget and a decomposition. Measured 2026-08-24
# over 131 resolved blockers, `attempt_count_ceiling` was the single largest
# cause of operator-blocked time (12 parks, 32.0h, median 1.34h — the highest
# median of the top five), because unlike the mechanical parks it needs a
# JUDGEMENT, and the judgement waited for a person.
#
# NOTHING HERE RAISES `MAX_TASK_ATTEMPTS`. That constant bounds unbounded local
# churn and a structural refusal still spends an attempt without spending a
# review round; every number below is a bound ON TOP of it, and each one is
# small on purpose:
#
#   * the extension is +2 attempts, granted at most once per task, so a task
#     that is genuinely looping reaches a hard wall at 7 dispatches rather than
#     never;
#   * a decomposition may not recurse past `MAX_SPLIT_DEPTH`;
#   * a child inherits its parent's SPEND, so a split cannot refund a budget.
#
# The park is not removed either — it is moved to the end of the sequence. When
# both remedies are spent the task still parks `attempt_count_ceiling`, with the
# same code an operator's tooling already knows.

#: How many attempts one reviewer-granted extension adds to `MAX_TASK_ATTEMPTS`.
#:
#: TWO, and the number is an argument. The measured case this exists for is a
#: task one attempt from done: blk-01 sat at attempt 5 of 5, round 3, zero
#: faults, with a verdict that endorsed eight things by name and left ONE fix,
#: remedy spelled out. One attempt would grant exactly that and nothing for the
#: round in which the named fix turns out to need a second pass; five would be a
#: second full budget, i.e. the refund this must not become.
CEILING_EXTENSION_ATTEMPTS = 2

#: How many times ONE task's attempt budget may be extended this way.
#:
#: ONE. The extension answers a specific claim — "the objections are shrinking
#: and this is the last fix" — and a task that spends 7 attempts without landing
#: has falsified it. A second grant would be the reviewer arguing with its own
#: evidence, and it is exactly the unbounded-churn back door the task's own
#: specification forbids. The ceiling is then still not a park: a task with no
#: extension left is asked again, and the only answer left on offer is a
#: decomposition or `stop`.
MAX_CEILING_EXTENSIONS = 1

#: How deep a ceiling decomposition may recurse. 0 is an ordinary planned task;
#: a child carries its parent's depth + 1.
#:
#: ONE — children cannot be split again. The narrowest bound that satisfies "a
#: subtask that hits its own ceiling must not be able to split again without
#: limit", and the reversible one: raising it later is a constant, lowering it
#: after tasks exist on disk at depth 2 is not. A child at the bound is not
#: stranded — it still gets its own extension, and then the hard wall.
MAX_SPLIT_DEPTH = 1

#: The floor under a child's attempt budget after it inherits its parent's spend.
#:
#: Without a floor the arithmetic is self-defeating: a parent at 5/5 subtracts 5
#: from `MAX_TASK_ATTEMPTS` and every child is born at its own ceiling, which
#: rebuilds the park the split exists to remove while passing any naive
#: "attempts were not refunded" test. TWO is the deliberate, bounded concession:
#: one attempt to produce a candidate and one to answer the first review of it.
#: It is still strictly less than `MAX_TASK_ATTEMPTS`, so no child ever receives
#: a fresh budget.
MIN_CHILD_ATTEMPTS = 2

#: The fewest subtasks a decomposition may name.
#:
#: TWO, and it is an anti-refund rule rather than a style rule. One child would
#: inherit the parent's spend, get the floor, and hand the SAME unit of work
#: `MIN_CHILD_ATTEMPTS` more attempts under a new id — a rename that buys
#: budget. A reviewer that believes the work is one unit is asking for an
#: extension, and the refusal says so.
MIN_CEILING_SPLIT_TASKS = 2

#: The `retire_execution` label a ceiling-split parent's execution record and
#: worker repo are filed under, so the two halves name each other on disk and a
#: human reading either one can tell the round was decomposed rather than
#: released, recut or displaced.
CEILING_SPLIT_RETIREMENT_REASON = "split-at-attempt-ceiling"

#: `blockers.Blocker.code` for a split acceptance the startup reconciliation
#: could NOT settle by itself (split-04, 2026-08-25) — the marker is unreadable,
#: it cannot be shown to describe what the registry recorded, or the parent's
#: record and worker refused to move.
#:
#: Recorded WITHOUT parking, like `blockers.STRANDED_AFTER_FAULT` and for the
#: same reason: the loop is working, on other tasks, and what is being reported
#: is one retired parent's leftover artefacts. Both of the consequences that
#: constant documents apply here too — it is invisible to
#: `test_m1_hardening._emitted_blocker_codes` (which AST-walks `_to_needs_user`
#: and `_to_fault_stop` only), so it is deliberately absent from
#: `cli._RESOLUTION_PRECONDITIONS`, and it never changes a task's status.
SPLIT_ACCEPTANCE_UNRECONCILED = "split_acceptance_unreconciled"

#: Longest any single evidence section of the ceiling classification request may
#: be. The request deliberately carries NO range diff — `packet
#: .RANGE_DIFF_MAX_BYTES` already refused port-01's at 414KB, and blk-01's
#: candidate is 1,821 insertions across 11 files — so what is left (the attempt
#: ledger, the last feedback, the stored plan, the touched-file list) is small,
#: and this bounds the one of them that is reviewer-authored prose. Overflow is
#: always STATED, never silent: a truncation that reads as complete coverage is
#: the same fail-open as no evidence at all.
CEILING_REQUEST_SECTION_MAX_CHARS = 4000

#: Filename of the wanted-verb tally under `AutoloopConfig.state_dir`.
WANTED_DECISIONS_FILENAME = "wanted_decisions.json"

#: How many DISTINCT wanted verbs the tally keeps before folding the rest into
#: one bucket. The value is reviewer-authored free text (see
#: `contract.Directive.wanted_decision`), so without a bound it is a way for the
#: reviewer to grow a file in the loop's state directory one key per round. Fifty
#: is far more vocabulary than this protocol will ever plausibly be missing, and
#: the fold is visible rather than silent — the counts still add up.
MAX_WANTED_DECISION_KINDS = 50

#: How long one wanted verb may be. It is meant to be A WORD ("split",
#: "rebase"); anything longer is a sentence in the wrong field, and a reviewer
#: cannot make the record unreadable by sending a paragraph.
MAX_WANTED_DECISION_CHARS = 40

#: Where the overflow of both bounds above is counted, so a folded or truncated
#: entry still appears in the total instead of vanishing.
WANTED_DECISION_OVERFLOW = "(other)"


def wanted_decisions_file(state_dir: Path) -> Path:
    """Where `WantedDecisionTally` keeps its counts."""
    return Path(state_dir) / WANTED_DECISIONS_FILENAME


class WantedDecisionTally:
    """Cumulative count of the verbs reviewers said they WOULD have used, as a
    small JSON object: `{"recut": 7, "split": 3}`.

    **Why a file of its own rather than a `LoopState` field.** A reviewer names
    a missing verb once in a while, across many sessions, and the whole value of
    the number is that it accumulates — but `cli._select_and_kickoff` replaces
    the entire `LoopState` at every session boundary, so a counter there would
    be reset by the ordinary transition it is trying to count. Exactly the
    reasoning `state.StopRepetition` records for itself.

    **It ENFORCES NOTHING, and that is why it is tolerant where
    `StopRepetitionStore` raises.** That ledger decides whether a park fires, so
    reading a corrupt one as "no stops" would switch the park off silently. This
    one decides nothing at all: it is evidence for a HUMAN, who reads it and
    files a task. An unreadable file therefore reads as empty and is rewritten,
    rather than taking a round down over a counter — but the caller is told
    (`record`'s second return value), so a lost history is stated in the
    transcript instead of looking like a first sighting.

    Both bounds are reviewer-facing: `MAX_WANTED_DECISION_CHARS` truncates one
    verb, `MAX_WANTED_DECISION_KINDS` folds the (N+1)th distinct verb into
    `WANTED_DECISION_OVERFLOW`. Neither drops a count.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def normalise(wanted: str) -> str:
        """One verb, as it is counted: whitespace collapsed, lower-cased and
        truncated. `""` when there is nothing left, which the caller reads as
        "nothing to record" rather than as an empty key."""
        text = " ".join(str(wanted or "").split()).lower()
        return text[:MAX_WANTED_DECISION_CHARS]

    def load(self) -> dict[str, int]:
        """The stored counts, or `{}` when there are none or they are
        unreadable. Never raises — see the class docstring."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        counts: dict[str, int] = {}
        for key, value in data.items():
            # Each entry is validated on its own, so one hand-edited row costs
            # its own count and not the whole history.
            if not isinstance(key, str) or not key:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                continue
            counts[key] = value
        return counts

    def record(self, wanted: str) -> tuple[dict[str, int], bool]:
        """Count one occurrence of `wanted`; return `(counts, reset)`.

        `reset` is True when a file was on disk and could not be read, so the
        counts returned are a fresh start rather than a continuation. The caller
        puts that in the transcript.
        """
        key = self.normalise(wanted)
        if not key:
            return self.load(), False
        existed = self.path.exists()
        counts = self.load()
        reset = existed and not counts
        if key not in counts and len(counts) >= MAX_WANTED_DECISION_KINDS:
            key = WANTED_DECISION_OVERFLOW
        counts[key] = counts.get(key, 0) + 1
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(
                json.dumps(counts, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            # `Path.replace`, not `os.replace`: same atomic rename, and this
            # module does not import `os`.
            tmp.replace(self.path)
        except OSError:
            # An unwritable state directory must not take a round down over a
            # counter. The occurrence still reaches the transcript through the
            # caller's event; only the cumulative file misses it.
            pass
        return counts, reset

    @staticmethod
    def render(counts: dict[str, int]) -> str:
        """`wanted: recut x7, split x3`, or `""` when nothing has been counted.

        THE operator-facing line, and the reason the tally exists at all: a
        directive-by-directive record answers "did anyone ask for this?", and
        only the total answers "is it worth a task?". Ordered by count, then by
        name so equal counts render deterministically.
        """
        if not counts:
            return ""
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return "wanted: " + ", ".join(f"{name} x{count}" for name, count in ordered)

#: `run()`'s outcome when a merge has changed the loop's own code and the loop
#: has reached a boundary at which its process may be replaced. NOT a phase:
#: the session is mid-flight and untouched — the outbox is durable, no packet
#: has been prepared, nothing is parked — so the caller either performs the
#: replacement (`cli._self_upgrade_at_boundary`, continuous mode) or reports it
#: and leaves the record for the next run. Resuming after either is the
#: ordinary "state is non-terminal, keep going" path, with no special case.
SELF_UPGRADE = "self_upgrade"

#: `LoopState.stop_kind` for a session an operator's urgent request ended.
#:
#: Deliberately a `stopped` session rather than a new `run()` outcome string
#: like `SELF_UPGRADE` above. Every existing caller of `run()` already treats a
#: non-fault `stopped` as a CLEAN ROUND BOUNDARY — reassess, select, start a
#: fresh session — which is exactly what must happen next here, so a caller
#: that has never heard of preemption does the right thing rather than falling
#: through an unhandled branch. `cli._is_fault_stop` reads `"fault"`
#: positively, so this is not a fault; `cli._cmd_smoke_browser` reads
#: `"contract"` positively, so this is not a healthy round-trip either.
PREEMPTION_STOP_KIND = "preempted"

#: The `retire_execution` label a displaced round's worker repo and execution
#: record are filed under, so the two halves name each other on disk as
#: `<task>-displaced-by-urgent-<stamp>` and a human reading either one can tell
#: at a glance that the work was PREEMPTED rather than released by hand or
#: abandoned by a park.
PREEMPTION_RETIREMENT_REASON = "displaced-by-urgent"

#: How many consecutive reviewer `stop` verdicts about the SAME unresolved
#: situation the loop will spend before it parks instead of opening yet another
#: session (`_observe_contract_stop` / `_park_stop_livelock`).
#:
#: A `stop` is a VERDICT, not a failure — `policy.max_consecutive_failures`
#: never counted these, which is why on 2026-08-20 a loop refused three times in
#: fifteen minutes over one lost postcommit binding while `health` reported
#: `running`, `open_blockers: 0`, `needs_attention: FALSE` throughout. This
#: bounds the REPETITION; one stop still means exactly what it always did.
#:
#: WHY THREE, AND WHAT THREE COSTS. Each cycle is one full reviewer turn plus
#: one packet build, and the incident's own cadence (20:05:49 → 20:10:54 →
#: 20:16:24) makes that about five minutes of wall clock. The park fires ON the
#: third matching stop, so the ceiling is three reviewer turns and three packet
#: builds — roughly ten to eleven minutes — of which the last two sessions are
#: the wasted ones. Two would be cheaper and is the number rejected: a reviewer
#: legitimately declining twice in a row while an operator works in another
#: window is ordinary, and a false park costs a human answer, which is the
#: thing this mechanism is spending. Above three the saving shrinks (a fourth
#: cycle buys no new evidence — the fingerprint is already identical) while the
#: quota burned grows linearly, so three is the smallest count that is not
#: plausibly a coincidence.
MAX_REPEATED_STOPS = 3

#: How many sent produce-then-review packets `LoopState.sent_postcommits` keeps
#: (oldest evicted first). The ledger answers exactly one question — "did this
#: loop send a postcommit packet under the request id this approval names?" —
#: and an approval that names a packet from eight rounds ago is not an approval
#: anyone should honour: the review round cap is two, so a live review arc is
#: never more than a handful of requests long, and every entry past that
#: describes work a later round has already superseded (which
#: `_dispatch_task_push` would refuse as `push_candidate_stale` anyway).
#:
#: Bounded at all because this lives in the state file, which is rewritten on
#: every step: an unbounded list would grow with the session and make every
#: save larger for records nothing can act on. EVICTION IS FAIL-CLOSED — an
#: approval naming an evicted packet resolves no binding and is refused, never
#: resolved against a different packet.
MAX_SENT_POSTCOMMIT_RECORDS = 8


@dataclass(frozen=True)
class Release:
    """What one `release_task_to_pending` call ACTUALLY moved.

    A plain `(task, Retirement)` tuple could only describe the happy path, and
    the reporting built on it then lied in the one case worth reporting: the
    status move is durable before the artefacts move (see the ORDER note
    below), so a failed retirement leaves a task that IS pending while the
    caller said it was not. This type carries the two halves separately, so a
    partial release is recorded as the partial thing it is.

    `artifacts_retired` False always comes with `obstacle` set, and with
    whichever residue survived named exactly: `stale_worker_path` is the
    directory still sitting where the next dispatch would create one, and
    `stale_execution_record` says the live record is still in the merge-window
    gate. It is only ever RETURNED to a caller that asked for it
    (`tolerate_retirement_failure=True`); every other caller gets the failure
    raised, which is what `cli._cmd_release` has always done.

    `residue_resumable` is the difference between the two endings a caller has
    to report differently, and it is a CHECKED fact rather than a hope — see
    `_repair_orphaned_record`.
    """

    task: Task
    retirement: Retirement | None = None
    #: The STATUS half. Always True on a returned `Release` — `registry.release`
    #: raises rather than returning when it will not move — and named anyway,
    #: so a reader of the record does not have to know that to interpret it.
    status_moved: bool = True
    artifacts_retired: bool = True
    obstacle: str = ""
    stale_worker_path: str = ""
    stale_execution_record: bool = False
    #: The residue is a record PLUS a worker that `worker_repo_is_reusable`
    #: accepts, so the next dispatch of this task resumes it instead of
    #: refusing — at the cost of a merge window that stays shut until that
    #: round publishes. False means the opposite ending: a worker the next
    #: dispatch will refuse to create over, which an operator has to move.
    residue_resumable: bool = False


def release_task_to_pending(
    task_id: str,
    registry: TaskRegistry,
    execution_store,
    worker_repos,
    *,
    persist,
    reason: str = "released-by-operator",
    tolerate_retirement_failure: bool = False,
    move=None,
) -> Release:
    """Return an in-progress task to pending, and retire the execution it
    leaves behind. Returns a `Release` describing what actually moved.

    THE release path, and the only one — `cli._cmd_release` and
    `Orchestrator._preempt_for_urgent` both come through here, because the
    thing that goes wrong with releasing a task is doing one third of it. Three
    things have to move together (the wording is `_cmd_release`'s own, kept
    verbatim because it is the rule):

    * the STATUS, or `next_ready` keeps skipping it;
    * the stale WORKER REPO, or the next dispatch refuses (`create()` will not
      write into an existing directory);
    * the EXECUTION RECORD, or `_merge_window_blockers` keeps reading a
      `candidate_sha` for work that no longer exists as in-flight.

    That third one was silently left behind until 2026-08-15 and cost an
    operator 14 hand-archived records; `worktask.retire_execution` does the
    last two in one call precisely so a caller cannot do one and forget the
    other. This function adds the first, so a SECOND caller cannot rebuild the
    same gap one level up — which is what a preemption re-implementing the
    sequence would have done.

    ORDER, and it is not arbitrary. `registry.release` raises before anything
    else happens, so a task that may not be released (not in progress) leaves
    the worker repo and the record exactly where they are. `persist` then makes
    the status durable BEFORE the artefacts move: a process that dies in
    between leaves a pending task with a stale worker, which the next dispatch
    refuses loudly, rather than a task still marked in-progress whose worker has
    already been quarantined — a state no command can move.

    **A FAILED RETIREMENT IS NEVER ROLLED BACK, AND WHO HEARS ABOUT IT IS THE
    CALLER'S CHOICE (`tolerate_retirement_failure`).** Rolling the status back
    is not an option either way: the status is already durable by the time the
    artefacts move, so undoing it would put the task back in `in_progress` —
    the exact invisible-to-`next_ready` state of 2026-08-21 — with its worker
    repo and record still on disk. What differs is the ENDING:

    * **False (the default, and `cli._cmd_release`)** — the failure is
      re-raised, unchanged and of its original type, so the operator's command
      fails loudly and `cli.main` reports it and exits 1. That is the behaviour
      this command has always had, and `test_recovery_commands.py::
      test_the_record_is_retired_before_the_worker` is its pin: the record is
      retired first, the worker survives, and the error propagates. A human ran
      that command and is reading its output; an exception is not a silent
      failure to them.
    * **True (`Orchestrator._preempt_for_urgent`)** — the failure comes back
      inside the `Release` instead. Nobody is watching a preemption, the round
      is ending regardless, and raising here would take the process down at the
      one moment an operator is waiting for their urgent task. The `Release`
      then says precisely how far it got, and the loop records it.

    Either way the `except` clause catches `GitError`, which the colliding-
    quarantine-label case (`WorkerRepoManager.quarantine`) actually raises: it
    is not an `OSError`, so a caller catching only the filesystem errors it
    believed covered that case never saw it.

    **ONE RETRY, under a distinct label, because a label collision is the
    likelier of the two named failures.** `retire_execution` derives its label
    from `reason` plus a whole-second timestamp, so two retirements of one task
    inside the same second collide by construction; retrying under
    `<reason>-retry` changes the label rather than the second and is what turns
    that case back into a clean retirement. The retry is safe to repeat because
    both halves are absence-tolerant — `archive` returns None when the record
    has already moved, and the worker is only quarantined `if … exists()`.

    **AND ON THE TOLERATING PATH THE RESIDUE IS LEFT IN THE BEST SHAPE
    AVAILABLE, WHICH IS NOT ALWAYS A GOOD ONE.** When even the retry fails,
    `_repair_orphaned_record` puts the execution record back beside the worker
    that could not be moved — but only when that worker is one the next
    dispatch would RESUME
    (`worker_repo_is_reusable`). Then the residue is exactly what a process
    killed between `persist()` and the retirement leaves, and the resume path
    already handles it. Otherwise the split is left as it is and reported: a
    live record shuts the repository-wide merge window, and paying that for a
    worker the next dispatch would refuse anyway buys nothing. So "prevent" is
    true for the label collision (the retry) and for a resumable worker, and
    the remaining case is REPORTED rather than prevented — nothing in this
    function can move a directory the filesystem refused to move.

    `persist` is a callable rather than a `TaskStore`, because the two callers
    save through different objects (the CLI's own store, the orchestrator's
    `self._task_store`) and because a function that took a store would have to
    decide what else to write. It is called exactly once, always.

    `move` is WHICH registry transition returns the row to pending, defaulting
    to `registry.release`. It exists for exactly one caller —
    `Orchestrator._dispatch_recut`, which passes `registry.recut` — and it is a
    parameter rather than a second copy of this function because everything
    BELOW the status move is identical for both verbs and is the part that has
    historically gone wrong: the ordering, the label-collision retry, the
    orphaned-record repair, the partial-`Release` reporting. A recut
    re-implementing that sequence one level up is precisely the gap this
    function was written to close, one caller later. It changes nothing for the
    default: `registry.release` is still what refuses a task that may not be
    released, still before anything else happens.

    Nothing is deleted. The worker is MOVED to quarantine (an interrupted round
    usually has real work in it) and the record is MOVED to
    `executions/archive/`, both under one label, so the candidate stays
    recoverable and the two halves name each other on disk — except on the
    retry, where the labels necessarily differ and `_archived_record_path`
    recovers the pairing for the record instead.
    """
    task = (move or registry.release)(task_id)
    persist()
    recorded = _loaded_execution(task_id, execution_store)
    problems = []
    first_failure = None
    for attempt_reason in (reason, f"{reason}-retry"):
        try:
            retirement = retire_execution(
                task_id, execution_store, worker_repos, reason=attempt_reason
            )
        except (GitError, StateError, OSError) as exc:
            problems.append(str(exc))
            if first_failure is None:
                first_failure = exc
            continue
        if retirement.record_path is None and recorded is not None:
            # The record was archived by the attempt that then failed on the
            # worker, so this attempt found nothing left to file and reported
            # `None` — which a reader would take as "there was no record". Look
            # up where the earlier attempt put it, so the two halves can still
            # be found from one another even though the retry changed the label.
            retirement = replace(
                retirement,
                record_path=_archived_record_path(task_id, execution_store, reason),
            )
        return Release(task=task, retirement=retirement)
    if not tolerate_retirement_failure:
        # The default ending, and the one `cli._cmd_release` has always had: the
        # original error, of its original type, to a human who is reading the
        # output of a command they just ran. Raised BEFORE `_repair_orphaned_
        # record`, deliberately — restoring a record shuts the repository-wide
        # merge window, and paying that silently behind a traceback would be a
        # cost nobody chose. The status move stands (see above), so what the
        # operator is looking at is a task that IS pending with its artefacts
        # still on disk.
        raise first_failure
    resumable = _worker_can_resume(recorded)
    _repair_orphaned_record(task_id, execution_store, recorded, problems, resumable)
    stale_record = _surviving_execution_record(task_id, execution_store)
    return Release(
        task=task,
        artifacts_retired=False,
        obstacle="; then ".join(problems),
        stale_worker_path=_surviving_worker_path(task_id, worker_repos),
        stale_execution_record=stale_record,
        residue_resumable=stale_record and resumable,
    )


def _archived_record_path(task_id: str, execution_store, reason: str):
    """Where the FIRST retirement attempt filed the execution record, when the
    retry could not report it because there was nothing left to file.

    Best-effort and read-only. Built from the naming
    `TaskExecutionStore.archive` documents (`archive/<task_id>-<label>.json`,
    label = `<reason>-<stamp>`), newest last because the stamp sorts
    lexicographically; `None` when nothing matches, which reports exactly as it
    did before — an unknown path is better said as unknown.
    """
    try:
        matches = sorted(
            (Path(execution_store.directory) / "archive").glob(
                f"{task_id}-{reason}-*.json"
            )
        )
    except (AttributeError, OSError):  # pragma: no cover - store without a directory
        return None
    return matches[-1] if matches else None


def _loaded_execution(task_id: str, execution_store):
    """The live `TaskExecution` before a retirement is attempted, or None.

    Read for one purpose — `_repair_orphaned_record` — and unreadable counts
    as absent: a record this cannot parse is one it must not try to put back.
    """
    if execution_store is None:
        return None
    try:
        return execution_store.load(task_id)
    except (StateError, OSError):  # pragma: no cover - unreadable record
        return None


def _worker_can_resume(recorded) -> bool:
    """Would the next dispatch RESUME the worker this record names, rather
    than try to create one over it?

    The same question `_dispatch_task_postcommit` asks
    (`worker_repo_is_reusable`: the directory is the top level of a git
    repository and is on the recorded branch), asked here so the repair below
    is a checked promise instead of a hopeful one. A directory that merely
    exists does NOT qualify, and that is the common shape of a half-written
    worker.
    """
    if recorded is None or not recorded.worktree_path:
        return False
    return worker_repo_is_reusable(Path(recorded.worktree_path), recorded.task_branch)


def _repair_orphaned_record(
    task_id, execution_store, recorded, problems, resumable: bool
) -> None:
    """Put the execution record BACK beside a worker the next dispatch can
    actually resume — and ONLY then.

    `retire_execution` files the record first and the worker second (its own
    ordering rule), so a worker half that fails leaves the two halves split:
    the record archived, the worker still at `workers/<task_id>`. When that
    worker is resumable, the split is pure loss — with no record the dispatch
    takes the first-dispatch branch and `WorkerRepoManager.create` refuses to
    write into the directory that is still there, discarding a round that could
    have been continued. Re-saving the record restores exactly what a killed
    process leaves, which the resume path already handles.

    **The `resumable` gate is what makes that trade honest, and it is not
    optional.** A live record holds the merge window shut
    (`cli._merge_window_blockers` exempts a record only for a terminal task, a
    PUBLISHED candidate, a base already behind the head, or a worker that is
    GONE — none of which this residue satisfies), and the window is the
    repository-wide merge sweep, not this one task. Paying that to save a
    resumable round is worth it; paying it for a worker the next dispatch would
    refuse anyway buys nothing and hides the refusal behind a stalled sweep.
    So a non-resumable residue is left SPLIT and reported as the directory an
    operator has to move.

    Re-SAVED from the object read before the attempt, rather than moved back
    out of `archive/`: the in-memory record is the authoritative copy, and
    reaching into the archive would need this module to spell a filename only
    `TaskExecutionStore` should own — with nothing better to do if that move
    failed too. The cost is one stale duplicate under `archive/`, which no
    reader looks at (`cli._merge_window_blockers` and `dashboard.merge_states`
    glob `executions/*.json`, which does not recurse).

    The restored record carries its `candidate_sha`, `review_round` and
    `attempt_count` forward, so the resumed dispatch continues under the
    counters the displaced round had already spent (`_park_round_cap` reads the
    second, `MAX_TASK_ATTEMPTS` the third). That is the intended reading — it is
    the same unit of work being continued, not a fresh one — and it is stated
    because a repair that quietly RESET them would look identical here and
    would hand the task a budget it had already used.

    Best-effort and never fatal. A repair that itself fails is appended to
    `problems`, so the caller's `obstacle` carries both halves of what went
    wrong rather than only the first.
    """
    if recorded is None or execution_store is None or not resumable:
        return
    if _surviving_execution_record(task_id, execution_store):
        return  # the record never moved — nothing was orphaned
    try:
        execution_store.save(recorded)
    except OSError as exc:  # pragma: no cover - unwritable executions dir
        problems.append(f"the execution record could not be restored either: {exc}")


def _surviving_worker_path(task_id: str, worker_repos) -> str:
    """The worker repo a failed retirement left behind, or `""`.

    Reported rather than merely counted because it is the one thing that
    BREAKS the next dispatch (`WorkerRepoManager.create` refuses to write into
    an existing directory), and because the remedy is a single `mv` an
    operator can only run if they are told the path. Best-effort: a manager
    that cannot even answer where the repo would be is not worth failing a
    release over.
    """
    if worker_repos is None:
        return ""
    try:
        path = worker_repos.path_for(task_id)
        return str(path) if path.exists() else ""
    except (ValueError, OSError):  # pragma: no cover - a manager that cannot answer
        return ""


def _surviving_execution_record(task_id: str, execution_store) -> bool:
    """Is the LIVE execution record still there after a failed retirement?

    The silent half of the pair: a surviving record announces nothing and
    holds the merge window shut (`cli._merge_window_blockers`), so a release
    that could not move it has to say so itself.
    """
    if execution_store is None:
        return False
    try:
        return execution_store.load(task_id) is not None
    except (StateError, OSError):  # pragma: no cover - unreadable record
        return True


def _preemption_stop_reason(target: Task, displaced_id: str, record: dict) -> str:
    """`LoopState.stop_reason` for a preempted session — THREE endings, not two.

    A release is two durable steps, so a preemption has three outcomes and
    each needs its own sentence: the status moved and the artefacts were
    quarantined (ordinary); the status moved but the artefacts did not (the
    task is selectable again, and one directory has to be moved by hand before
    it can be); the status did not move at all. Collapsing the middle case into
    either neighbour is what the earlier two-way message did — it reported a
    task that WAS pending as "NOT returned to pending", sending an operator to
    fix a status that was already correct while the residue that actually
    blocks the next dispatch went unnamed.
    """
    head = f"preempted for urgent task {target.id} ({target.urgent_reason}); "
    if not record["displaced_returned_to_pending"]:
        return f"{head}{displaced_id} was NOT returned to pending — {record['obstacle']}"
    if record["displaced_artifacts_retired"]:
        return f"{head}{displaced_id} was returned to pending"
    residue = record["stale_worker_path"] or "(no worker repo left behind)"
    if record["residue_resumable"]:
        return (
            f"{head}{displaced_id} was returned to pending, but its execution "
            f"could not be retired — {record['obstacle']}. Its worker repo "
            f"({residue}) and execution record were left paired and resumable, "
            "so the next dispatch of it continues that round; the merge window "
            "stays shut until that round publishes"
        )
    return (
        f"{head}{displaced_id} was returned to pending, but its execution could "
        f"not be retired — {record['obstacle']}. Move {residue} aside before "
        f"{displaced_id} is dispatched again — it is not resumable, so the "
        "dispatch will refuse to create a worker over it"
    )


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

def _conversation_id(url: str) -> str | None:
    """The id a `/c/<id>` URL ends with, or None for anything else.

    Mirrors `BrowserChatGPT._conversation_id` deliberately rather than
    importing it: URL comparison on the orchestrator's side of the seam is
    already its own (`_same_conversation`), and the alternative is the
    orchestrator reaching into one adapter's private helper to reason about
    conversations every provider has.
    """
    segs = [seg for seg in urlsplit(url).path.split("/") if seg]
    if len(segs) >= 2 and segs[-2] == "c":
        return segs[-1]
    return None


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


#: How many times ONE request may be re-invoked by
#: `Orchestrator._replay_unrecoverable_await` because its transport can no
#: longer produce the reply the loop is waiting for.
#:
#: The real need is one per PROCESS: `codex.conversation.CodexConversation`
#: keeps its reply in an in-memory dict, so a request submitted before a restart
#: is unrecoverable afterwards and re-running is the recovery that transport
#: already promises. Three, so a run that is replaced a couple of times
#: mid-request for unrelated reasons (a self-upgrade exec, an operator kill)
#: still recovers rather than parking on the second one.
#:
#: A module constant rather than a `policy.PolicyConfig` field on purpose: this
#: bounds a re-run the TRANSPORT declared safe, not an operator preference, and
#: a knob here would invite raising it — which is how "bounded" becomes "keeps
#: re-invoking a reviewer that never answers". Deliberately uncoupled from
#: `max_consecutive_failures`: exhausting this budget does not end the run, it
#: hands the fault back to that one, which is what produces the park.
MAX_AWAIT_REPLAYS = 3


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
        #: The repeated-stop ledger (`state.StopRepetitionStore`). DERIVED from
        #: `config.state_dir` rather than taken as a constructor parameter, and
        #: that is the point: every other optional collaborator here can be left
        #: `None` by a construction site that has not heard of it, and a park
        #: that only fires when somebody remembered to wire it is a park that
        #: silently does not fire. Nothing to pass, nothing to forget — every
        #: Orchestrator that ran `__init__` counts repeated stops.
        self._stop_repetitions = StopRepetitionStore(
            stop_repetition_file(config.state_dir)
        )
        #: The wanted-verb tally (`WantedDecisionTally`). DERIVED from
        #: `config.state_dir` for exactly the reason above, plus one of its own:
        #: this counter's whole value is being CUMULATIVE across sessions, and a
        #: collaborator a construction site can forget to pass is a counter that
        #: is quietly zero in production.
        self._wanted_decisions = WantedDecisionTally(
            wanted_decisions_file(config.state_dir)
        )
        #: In-flight split acceptances (`worktask.SplitIntentStore`). DERIVED
        #: from `config.state_dir` for exactly the reason the two stores above
        #: are, and here the argument is at its sharpest: this marker is the
        #: ONLY durable evidence that a decomposition was half-applied, so a
        #: store a construction site can forget to pass is a recovery that
        #: silently never runs — and the state it recovers from is invisible by
        #: construction (a retired parent nothing will dispatch again). Nothing
        #: to pass, nothing to forget: every Orchestrator that ran `__init__`
        #: reconciles.
        self._split_intents = SplitIntentStore(split_intents_dir(config.state_dir))
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
        #: The `blockers.Blocker.id` an autonomous retry is currently riding on
        #: (halt-02), or `""`. Set by `_autonomous_retry`, consumed by
        #: `_close_recovered_blocker` on the first completed step, and dropped
        #: without being closed by any park. IN MEMORY DELIBERATELY: it means
        #: "this process retried and has not yet seen the retry work", which is
        #: not a fact about the loop that should outlive the process — a
        #: successor reads the still-open record as budget already spent, which
        #: is the direction that retries less.
        self._autonomous_recovered_blocker = ""
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
        #: Where the config file lives. Its one writer, the rotation's config
        #: heal, is gone since brw-15; the path is still carried because
        #: `cli._build_orchestrator` passes it and a future writer would need
        #: the same value. Defaults to the conventional location under
        #: `state_dir`; the CLI passes the path it actually loaded, since
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
        #: The phase at which this process FIRST saw the current urgent pin, so
        #: the preemption record can say what it waited through rather than only
        #: where it acted. In-memory and deliberately not persisted: it is
        #: evidence about this process's own observation, and a restart that
        #: inherited a stale value would claim to have waited through a phase it
        #: never saw. `None` means "not seen yet", and it is cleared again once
        #: the preemption it describes has been recorded.
        self._urgent_first_seen_phase = None
        self._client = None

    # ---- main loop ----------------------------------------------------------

    def run(self, max_steps: int | None = None) -> str:
        """Run until a terminal phase, a pause request, or max_steps."""
        # STARTUP, before the first step and before anything reads the state
        # directory: a decomposition this process's predecessor half-applied
        # leaves the registry describing a retired parent whose execution record
        # is still live, which holds the repository-wide merge window shut and
        # announces nothing. See `_reconcile_split_acceptance`. Costs one
        # `is_dir()` when no split has ever been accepted, which is the ordinary
        # case, and never raises.
        self._reconcile_split_acceptance()
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
            # AFTER the self-upgrade check, and the order is deliberate rather
            # than incidental: a self-upgrade replaces the process at this same
            # boundary and comes straight back to it, so the preemption is
            # delayed by one exec and then acted on by the merged code — while
            # the reverse order would end the round first and leave the pending
            # upgrade for a boundary that no longer exists in this session.
            # Both sit AFTER the terminal check, so a parked loop reports what
            # it is parked on rather than ending a round nobody is running.
            if self._preempt_for_urgent(phase):
                return Phase.STOPPED.value
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
                # logged-out profile is an account problem, and the conversation
                # is not the thing at fault. Parking as `conversation_unusable`
                # would name the wrong subsystem in the blocker.
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
                self._route_transport_fault(
                    phase, exc, self._handle_conversation_unusable
                )
            except ResponseTimeoutError as exc:
                # Caught BEFORE the generic BrowserError below (it is one), so
                # a response-START timeout can advance the per-request silence
                # counters before it takes the ordinary failure budget. See
                # `_handle_response_start_timeout`.
                self._route_transport_fault(
                    phase, exc, self._handle_response_start_timeout
                )
            except BrowserError as exc:
                self._route_transport_fault(phase, exc, self._handle_browser_failure)
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
                # Same evidence, a second reader: an autonomous retry that is
                # followed by a completed step has recovered, and its blocker
                # record can be closed with a machine reason. Ordinary runs
                # (and every run with autonomy off) have no marker set and
                # this returns immediately.
                self._close_recovered_blocker()
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

    # ---- the round boundary ---------------------------------------------------

    def _at_round_boundary(self, phase: Phase) -> bool:
        """Is the loop between rounds, with nothing outstanding?

        **`READY` with no pending request, and nothing else.** That is the
        instant BEFORE the next request is prepared: `_step_ready` is what
        builds a packet, so nothing has been sent and nothing is awaited; the
        payload lives in `state.outbox`, already saved; the executor is not
        running (a write-capable agent runs inside `_step_executing`, a
        different phase, synchronously — reaching here means it returned); and
        every `TaskExecution` was written by whoever last touched it, since
        this class saves records as it goes rather than at an exit.

        Every other phase is mid-round by construction — `delivering`,
        `submitting` and `submission_unconfirmed`/`submission_rejected` have a
        packet in flight or an unresolved send, `awaiting` has a reviewer
        holding one, `executing` has an agent writing into a worker repo.
        `pending_request` is checked SEPARATELY from the phase because it
        outlives its own phase: a request answered and not yet consumed is
        still a packet this loop owes something to.

        ONE predicate, two users (`_self_upgrade_due`, `_preempt_for_urgent`),
        because they are asking the same question — may this session be
        interrupted right now — and a second copy would be a second answer.
        That matters most for the phases where an interruption is destructive:
        stopping in `submitting` or `awaiting` strands a review packet, and a
        preemption that stranded an approved push would have traded a slow
        queue for a lost candidate.
        """
        return phase is Phase.READY and self.state.pending_request is None

    def _self_upgrade_due(self, phase: Phase) -> bool:
        """Is this the moment at which the process may be replaced?

        The boundary itself is `_at_round_boundary` above — `READY` with no
        pending request — and this adds the one question that is specific to a
        replacement: is there a merged upgrade waiting for it. A replacement at
        that boundary loses nothing: the successor loads the same state file and
        prepares the same request from the same outbox.

        Reads the record, never writes it. An unreadable or already-settled
        record answers False — the fail-closed direction here is to keep
        running the code that works. So does an orchestrator whose caller never
        asked for the boundary (`self_upgrade_enabled`, see the constructor).
        """
        if not self._self_upgrade_enabled:
            return False
        if not self._at_round_boundary(phase):
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

    # ---- preemption ----------------------------------------------------------

    def _displaced_work_exists(self, task_id: str) -> bool:
        """Is there a PLANNED task in flight under `task_id` for a preemption
        to displace?

        The guard that keeps a preemption from being a no-op that ends healthy
        sessions in a circle. Without it, a pin observed by a loop with nothing
        running would end the round, the next selection would start a fresh
        session, that session would reach the same boundary with the same pin
        still live, and the loop would spend every iteration ending sessions
        that had done nothing — the pin alone is what steers the next selection
        (`TaskRegistry.next_ready`), and an idle loop needs no preemption to
        pick up an urgent task.

        In flight means the registry knows this id AND its stored status is
        `in_progress`. Read as the stored string rather than through
        `state_of`, which reports BLOCKED for an in-progress task with an
        incomplete dependency and would fall silent on the very task that is
        hardest to release (the same reasoning `_refuse_immutable` documents).

        **An AUDIT unit already in flight is deliberately NOT displaced.** An
        audit round holds no task in the queue and its work is a report, so
        quarantining it mid-write spends the round twice — once to produce the
        report and once to redo it — to save at most the tail of a round the
        loop has already paid for. Waiting it out costs the pin nothing it can
        still get: the urgent task is what `next_ready()` returns for the round
        after, and `urgent_awaiting_boundary` makes the wait visible while it
        happens.

        The bound on that wait is ONE round, not one per lap, and it is
        `_refused_ahead_of_urgent` that supplies it: a FRESH `audit` is refused
        while the pin is live, and `cli._start_new_session` opens a pinned
        session on the urgent task rather than on the audit kickoff. Until
        2026-08-22 neither was true — the session a preemption started invited
        an `audit` and the gate let it through — so this exemption was load
        bearing in a way it no longer is. It stays because displacing a
        read-only report round is still the wrong trade, not because a lap
        would otherwise churn.
        """
        if not task_id or not self._registry.has(task_id):
            return False
        return self._registry.get(task_id).status == "in_progress"

    def _preempt_for_urgent(self, phase: Phase) -> bool:
        """End the round in flight so an operator's urgent task can take the
        loop. True when this round was preempted (the caller ends `run`).

        THE preemption. What it costs and what it does not:

        * **It waits for a safe boundary.** `_at_round_boundary` — `ready` with
          no pending request, the same instant a self-upgrade may replace the
          process. A request that arrives mid-`submitting` or mid-`awaiting`
          is observed, logged as waiting, and acted on when the round comes
          back to `ready`; interrupting there would strand a review packet or
          an approved push, which is the one thing a preemption must never buy
          its speed with.
        * **It releases the displaced task properly, or not at all.**
          `release_task_to_pending` moves the status, the worker repo and the
          execution record together — the one implementation, shared with
          `cli._cmd_release`. Only a PLANNED task in flight is displaced at all
          (`_displaced_work_exists`); an audit round is waited out rather than
          displaced, for the reason recorded there. When the artefact half
          fails anyway, the record says so as its own fact
          (`displaced_artifacts_retired`, `stale_worker_path`) rather than
          claiming the status move did not happen: it did, durably, before the
          artefacts were touched.
        * **It records what it took.** `state.preemption` and the
          `task_preempted` transcript entry carry the displaced task, the
          phase the request was first seen at, the phase it was acted on at,
          the urgent target and its reason, and the quarantine label plus the
          two paths the work was moved to. A preemption that silently discarded
          twenty minutes of work would be worse than a slow queue.
        * **It changes NOTHING about review, approval or publication.** No
          packet is skipped, no verdict is assumed, no push is authorized: the
          round simply ends between rounds, exactly as a reviewer's own `stop`
          does, and the displaced candidate stays in its quarantined worker
          where it can be inspected or re-dispatched.

        There is deliberately NO pause flag anywhere in this path. The
        documented failure of the manual sequence is two operators each
        pausing, the first timing out and calling `resume` on its way out, and
        the second waiting on a lock that never clears. A loop that observes
        the request itself, at its own boundary, takes no lock to strand — and
        `TaskRegistry.request_urgent` allows only one live pin at a time, so
        there is never a second preemption in flight to strand in the first
        place.

        Ends the session as a `stopped` round with
        `stop_kind = PREEMPTION_STOP_KIND` rather than returning a new outcome
        string, so every existing caller treats it as the clean round boundary
        it is. The registry is saved before this returns, because continuous
        mode reloads `tasks.json` from disk at the top of its next iteration —
        an unsaved release or pin would simply not exist there.
        """
        target = self._registry.live_urgent_target()
        if target is None:
            return False
        state = self.state
        displaced_id = (state.current_task or {}).get("task_id") or ""
        if displaced_id == target.id:
            # Defense in depth, and unreachable today by construction:
            # `live_urgent_target` only returns a READY task and a displaced
            # task is IN_PROGRESS, so one id cannot be both. Kept because the
            # failure it prevents is the worst one available here — quarantining
            # the very work the request asked for — and it would become
            # reachable the moment `live_urgent_target` widened.
            return False
        if not self._displaced_work_exists(displaced_id):
            return False
        if self._urgent_first_seen_phase is None:
            self._urgent_first_seen_phase = phase.value
        if not self._at_round_boundary(phase):
            self._log(
                "urgent_awaiting_boundary",
                data={
                    "urgent_task_id": target.id,
                    "displaced_task_id": displaced_id,
                    "phase": phase.value,
                    "note": (
                        "an urgent request is pending; the round continues to a "
                        "safe boundary (ready, no packet outstanding) before it "
                        "is displaced"
                    ),
                },
            )
            return False

        execution = None
        if self._execution_store is not None:
            try:
                execution = self._execution_store.load(displaced_id)
            except (StateError, OSError):  # pragma: no cover - unreadable record
                execution = None
        released = False
        obstacle = ""
        release = None
        try:
            release = release_task_to_pending(
                displaced_id,
                self._registry,
                self._execution_store,
                self._worker_repos,
                persist=lambda: self._task_store.save(self._registry),
                reason=PREEMPTION_RETIREMENT_REASON,
                # A retirement that fails comes back INSIDE the `Release` here
                # rather than being raised, which is the opposite of what the
                # same call does for `cli._cmd_release`. Nobody is watching a
                # preemption: the round is ending either way, and taking the
                # process down at the one moment an operator is waiting for
                # their urgent task would be the worse ending. An operator who
                # ran `release` by hand IS watching, so that path still raises.
                tolerate_retirement_failure=True,
            )
            released = release.status_moved
        except (TaskGraphError, StateError, OSError) as exc:
            # The STATUS half, and the only half that can still raise: a task
            # the registry will not release (`TaskGraphError`), or a
            # `tasks.json` that could not be written (`StateError`/`OSError`
            # out of `persist`). Near-unreachable, since `_displaced_work_exists`
            # has just read the status `release` demands, and recorded rather
            # than re-raised because the round ends either way — taking the
            # process down at the one moment an operator is watching for their
            # urgent task would be the worse ending.
            #
            # The ARTEFACT half does not arrive here at all, because of the
            # `tolerate_retirement_failure=True` above: a retirement that fails
            # comes back inside the `Release` instead, since by then the status
            # move is already durable and reporting it as "not returned to
            # pending" was a lie about a task `next_ready` can already see.
            obstacle = str(exc)
            self._log(
                "preemption_release_failed",
                data={"displaced_task_id": displaced_id, "error": str(exc)},
            )
        if release is not None and not release.artifacts_retired:
            obstacle = release.obstacle
            self._log(
                "preemption_retirement_failed",
                data={
                    "displaced_task_id": displaced_id,
                    "error": release.obstacle,
                    "stale_worker_path": release.stale_worker_path,
                    "stale_execution_record": release.stale_execution_record,
                    "residue_resumable": release.residue_resumable,
                },
            )

        retirement = getattr(release, "retirement", None)
        record = {
            "urgent_task_id": target.id,
            "urgent_reason": target.urgent_reason,
            "urgent_requested_at": target.urgent_at,
            "displaced_task_id": displaced_id,
            "displaced_returned_to_pending": released,
            # Separate from the line above, deliberately: the status move is
            # durable before the artefacts move, so "pending again" and "its
            # worker repo was quarantined" are two different facts and a
            # preemption can honestly report the first without the second.
            "displaced_artifacts_retired": bool(
                release is not None and release.artifacts_retired
            ),
            "first_observed_phase": self._urgent_first_seen_phase or phase.value,
            "preempted_at_phase": phase.value,
            "obstacle": obstacle,
            "quarantine_label": getattr(retirement, "label", "") or "",
            "quarantined_worker_path": str(getattr(retirement, "worker_path", "") or ""),
            "archived_execution_record": str(getattr(retirement, "record_path", "") or ""),
            # The residue, named exactly, plus the one fact that decides what an
            # operator should do about it. Resumable means the pair was kept and
            # the next dispatch continues that round, at the cost of a merge
            # window that stays shut until it publishes; not resumable means a
            # directory that has to be moved before this task can run again.
            "stale_worker_path": getattr(release, "stale_worker_path", "") or "",
            "stale_execution_record": bool(
                getattr(release, "stale_execution_record", False)
            ),
            "residue_resumable": bool(getattr(release, "residue_resumable", False)),
            "displaced_candidate_sha": getattr(execution, "candidate_sha", "") or "",
            "displaced_review_round": getattr(execution, "review_round", 0) or 0,
            "displaced_attempt_count": getattr(execution, "attempt_count", 0) or 0,
            "at": utcnow_iso(),
        }
        state.preemption = record
        state.current_task = None
        state.task_execution = None
        state.last_response = None
        # The packet this round was about to send is about the task that just
        # went back to the queue, so it is discarded with it rather than left
        # to be sent by anything that re-enters `ready`.
        state.outbox = None
        state.outbox_diff = None
        state.outbox_attachment = None
        state.phase = Phase.STOPPED.value
        state.stop_kind = PREEMPTION_STOP_KIND
        state.stop_reason = _preemption_stop_reason(target, displaced_id, record)
        self._store.save(state)
        self._log("task_preempted", data=record)
        self._urgent_first_seen_phase = None
        return True

    # ---- phases -------------------------------------------------------------

    def _step_ready(self) -> None:
        state = self.state
        # FIRST, and before `build_context` reads `next_ready()` below: a task
        # the environment stranded is returned to the pool in time to appear in
        # the very packet that asks what to do next, rather than one round
        # later. Ahead of the iteration-budget check too — a local
        # reconciliation costs nothing and a budget park must not leave a task
        # unscheduled and unreported behind it (strand-01).
        self._reconcile_stranded_tasks()
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
        # A corrective re-prompt carries none of the candidate's identifiers, so
        # the bind above answers `None` for it — correctly, since the correction
        # presents nothing. What it is NOT is a new review: the packet under
        # review has not changed and the candidate has not moved, so the binding
        # of the request being corrected is inherited here rather than dropped.
        # Consumed unconditionally (the helper always clears) and used only when
        # nothing was freshly bound: a payload that really does present a
        # candidate binds to THAT one, with its own packet digest.
        carried = self._consume_carried_postcommit(state)
        if postcommit is None and carried is not None:
            postcommit = carried
            self._log(
                "postcommit_carry_applied",
                request_id=request_id,
                data={
                    "task_id": carried.task_id,
                    "candidate_sha": carried.candidate_sha,
                    # The digest of the PACKET this binding came from, not of
                    # this request — the correction presents no packet, and
                    # rewriting it to `ctx.report_sha256` would claim it did.
                    "packet_sha256": carried.packet_sha256,
                },
            )
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
            # Every request that leaves here carrying a binding is recorded, an
            # inherited one included: the ledger's question is "did this loop
            # send a request bound to that candidate under this id", and a
            # correction that inherited a binding did.
            self._record_sent_postcommit(
                state, request_id, ctx.head_sha, ctx.report_sha256, postcommit
            )
        if postcommit is not None and postcommit is not carried:
            # Bind the exact report this candidate was reviewed under, on the
            # TaskExecution record, so a later approval answering a DIFFERENT
            # report can never authorize publishing it.
            #
            # Skipped for an INHERITED binding, deliberately. These two fields
            # record which report actually PRESENTED the candidate, and a
            # corrective re-prompt presents no packet — overwriting them with
            # the correction's own digest would replace the record of the
            # review with a record of the apology for a malformed reply.
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
        wedged conversation (`ConversationUnusableError`, which parks). Eating
        those here would demote a routed fault into a silent "the part is
        absent", and the loop would answer a login prompt by omitting a diff.
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
            # The ledger is the FOURTH place holding this digest, and it has to
            # move with the other three or an approval naming this request id
            # would be checked against the stamps of a packet that was never
            # sent. `_record_sent_postcommit` replaces the entry in place.
            self._record_sent_postcommit(
                state, req.request_id, ctx.head_sha, ctx.report_sha256, req.postcommit
            )
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
            #    owns the decision (it may spend one same-chat resend, then
            #    parks). Routing there rather than deciding here is what makes
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
        second confirmed rejection) a park. Until brw-15 that second rejection
        could open a replacement chat instead; see `_park_send_rejected_twice`.
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
        # disproving both sends. The chat itself is the suspect now — and since
        # brw-15 there is no automatic move to another one, so this stops.
        self._park_send_rejected_twice(req)

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
        if self._transport_is_browser_backed():
            # Before the counter moves, so a browser fault never spends a budget
            # that exists to bound waiting on the SERVER (brw-03's rule).
            classification, evidence = self._classify_rate_limit_state()
            if classification == RL_BROWSER_UNATTACHABLE:
                self._recover_unattachable_browser(phase, exc, evidence)
                return
        else:
            # A run with no browser is not asked the browser question at all.
            # `_classify_rate_limit_state` reaches its third world by dialling
            # `browser.cdp_url`, which answers about whatever Chrome happens to
            # be running on this host — nothing to do with the transport that
            # raised. A zero-target answer there would restart a browser for a
            # non-browser fault, which is exactly the confusion this class of
            # bug is made of, and even the classification's PROSE would put
            # browser evidence into a codex park. No codex transport raises
            # `RateLimitedError` today (`codex/protocol_errors.py` says so
            # explicitly); this closes the door before someone opens it.
            classification, evidence = (
                RL_THROTTLED,
                f"the {self.active_provider()} transport reported a throttle; "
                "this run drives no browser, so no page or CDP endpoint was "
                "probed",
            )
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
            if not self._transport_is_browser_backed():
                # A THIRD branch, and it exists for the same reason the guard
                # above the classification does: both of the branches below
                # tell an operator to reason about a browser, and one of them
                # tells them to curl a CDP endpoint and restart Chrome. On a run
                # that drives no browser that is advice about someone else's
                # program — the exact failure recov-01 undoes, arriving through
                # the one park in this handler rather than through the restart.
                # Unreachable today (no non-browser transport raises
                # `RateLimitedError`), and closed anyway: leaving half a door
                # shut is not shutting it.
                verdict_text = (
                    f"This is NOT a transport fault — the {self.active_provider()} "
                    "transport reported an account-level limit, and this run "
                    "drives no browser, so nothing local can clear it."
                )
                evidence_label = "What was observed"
            elif sighted:
                verdict_text = (
                    "This is NOT a browser fault — the composer is present the "
                    "whole time and a restart cannot help, because the limit is "
                    "server-side."
                )
                evidence_label = "What the browser looked like"
            else:
                evidence_label = "What the browser looked like"
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
                f"before re-probing. Last error: {exc}. {evidence_label} when "
                # The sentence that stops this park from asserting more than
                # was measured, and the input to the branch above. Its LABEL
                # follows the transport for the same reason the verdict above
                # does: "what the browser looked like" is a false premise on a
                # run that has none.
                f"this was classified: {evidence}",
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
        # Same rule, one more mark: a replay budget describes re-invocations of
        # the OLD transport, which the new one has never performed.
        req.replays_used = 0
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
        """The configured conversation loaded and is broken. Park.

        Distinct from `_handle_browser_failure`: that one counts consecutive
        failures and retries the same phase with a fresh client, which for a
        wedged chat just re-runs the same failure until the budget is spent.
        Accounting is deliberately NOT shared — this park does not also
        increment `consecutive_failures`, or one fault would be charged to two
        budgets and the loop would fail earlier than either one describes.
        For the same reason this handler never touches the browser restart
        machinery: both shapes of the error (an attach that found no composer,
        and a submission this loop made that provably never appeared — see
        `ConversationUnusableError.code`) were established THROUGH a working,
        un-throttled page, so a restart could not possibly help and would only
        spend recovery on a fault it cannot fix. The 2026-08-17 incident is
        the second shape misrouted: a missing submission surfaced as a locator
        timeout, read as a lost session, and bought ten minutes of 45-second
        Chrome restarts against a chat no restart could fix.

        Until brw-15 (2026-08-25) this was the entry point to conversation
        rotation, which opened a replacement chat in `browser.project_url` and
        moved the in-flight request into it. That machinery is gone, so the one
        remaining honest answer is to stop and say which chat is wedged and why.
        The error's own `code` is carried into the transcript and into the
        blocker's detail, so a vanished submission stays distinguishable from a
        chat that would not load; the blocker CODE is the fixed
        `conversation_unusable`, because a code set that varies with the
        transport's error strings is one `cli._RESOLUTION_PRECONDITIONS` and
        `test_m1_hardening._emitted_blocker_codes` cannot reason about.

        Reached only for a browser-backed transport (`_route_transport_fault`),
        so the remedy may name the browser.
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
            # Nothing in flight; treat as an ordinary browser fault so the
            # failure budget still governs a dead conversation at rest.
            self._handle_browser_failure(phase, exc)
            return
        self._to_needs_user(
            f"the conversation this loop is using is unusable ({reason}): {exc}. "
            f"Request {req.request_id} is still bound to it "
            f"({req.conversation_url or state.conversation_url or 'unknown'}), so a "
            "plain restart resumes in the same chat. Autoloop no longer opens a "
            "replacement chat on its own: open the conversation by hand and see "
            "whether it works. If it does not, point browser.conversation_url at a "
            "fresh chat and `reset` — the drift guard requires state and config to "
            "agree — rather than resending, since a resend into a chat that already "
            "holds this request posts it twice.",
            resume_phase=phase.value,
            kind="loop_fatal",
            code="conversation_unusable",
            detail=f"request_id={req.request_id} reason_code={reason}",
        )

    def _handle_response_start_timeout(
        self, phase: Phase, exc: ResponseTimeoutError
    ) -> None:
        """A confirmed, persisted send whose assistant turn never starts —
        the "silent conversation" fault, where the send is known good and the
        model simply never begins.

        Handled by the ordinary failure budget, exactly like every other
        transport fault, with ONE extra thing done first: the per-request
        counters are advanced, so the state file records how long this request
        actually waited and across how many windows. `_handle_browser_failure`
        is what persists them — a crash here must resume with the count intact,
        not lose it back to 0.

        Until brw-15 (2026-08-25) a third consecutive `stage="start"` timeout,
        past an accumulated-wait floor and confirmed by one final
        reconciliation, additionally authorized a conversation rotation.
        Nothing reads the counters now; they are kept because they are the only
        durable record of a silent conversation and because
        `PendingRequest`/`state.py` are not this change's to edit. A
        `stage="complete"` timeout (a response that already started and is
        merely slow) never counted and still does not.
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
        self._handle_browser_failure(phase, exc)

    def _park_send_rejected_twice(self, req: PendingRequest) -> None:
        """Two sends of one request disproven in a row, in the same chat. Stop.

        The one caller is `_step_submission_rejected`, and getting here means
        the transport reported BOTH sends rejected and reconciliation confirmed
        the request is in the conversation neither time. A third identical
        attempt is exactly the duplicate the `submission_rejected` phase exists
        to bound, so the loop stops and says so.

        Until brw-15 (2026-08-25) this was the third and — on an ordinary day —
        only reachable door into conversation rotation. `_step_submission_
        rejected` arrives here without passing any fault handler, and the
        subprocess codex transport returns REJECTED on every non-zero exit, so
        two consecutive codex failures on one request walked straight into a
        recovery that opens a ChatGPT chat. With `browser.project_url` unset
        (the normal codex deployment) that parked telling the operator to set it
        "to the ChatGPT project this conversation belongs to"; with it left set
        by an operator who had moved off the browser, it spent
        `state.rotations` and then failed inside the rotation because the
        transport has no `retarget`. Neither outcome had anything to do with the
        fault.

        So the remedy is chosen from the transport actually in use, and nothing
        here is browser-specific unless the run is.
        """
        provider = self.active_provider()
        if self._transport_is_browser_backed():
            remedy = (
                "Open the conversation by hand and check whether it accepts a "
                "message at all. If it does not, point browser.conversation_url "
                "at a fresh chat and `reset` — the drift guard requires state "
                "and config to agree — rather than resending here."
            )
        else:
            remedy = transport_remedy(provider)
        self._to_needs_user(
            f"two sends of {req.request_id} were disproven in a row: the "
            f"{provider} transport reported each one rejected and reconciliation "
            "confirmed the request is not in the conversation either time. "
            "Autoloop will not send it a third time on its own. "
            f"{remedy} "
            "Then `python -m autoloop run --retry` to reconcile once more — and "
            "if the request is still absent with the transport repaired, `run "
            "--resubmit`, which authorizes exactly one more send of this same "
            "request id, so a message that did land is detected rather than "
            "duplicated. A plain `--retry` alone re-parks here: the one same-chat "
            "resend this phase allows is already spent.",
            resume_phase=self.state.phase,
            kind="loop_fatal",
            code="send_rejected_twice",
            detail=f"request_id={req.request_id} resends_used={req.resends_used}",
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

    # ---- postcommit bindings that outlive one request -----------------------
    #
    # A produce-then-review packet is presented ONCE, and the approval that
    # publishes it may not arrive in the reply to that packet. Two things can
    # come between them, and each loses the binding a different way:
    #
    #   * a CORRECTIVE RE-PROMPT (parse error, policy denial, review mismatch,
    #     plan rejection) — the loop replaces the request with a correction
    #     whose payload carries none of the candidate's identifiers, so
    #     `_current_pending_postcommit` binds it to nothing. That is what
    #     happened on 2026-08-20 (prof-01): a `unexpected_field` parse error on
    #     the review packet turned a candidate that had passed four review
    #     rounds and full validation into APPROVED AND UNPUBLISHABLE, and the
    #     reviewer's correct refusals then livelocked the loop for fifteen
    #     minutes. `carry_postcommit` carries the binding across the
    #     correction: the packet under review has not changed, so the
    #     correction is about the same candidate.
    #   * the CONVERSATION MOVING ON — `last_response` is only the most recent
    #     thing sent, and an approval that names the packet's own request id is
    #     better evidence of what was approved than "whatever was last sent".
    #     `sent_postcommits` is the loop's record of what it presented, so that
    #     name can be checked rather than disbelieved.
    #
    # Neither is an authorization. A binding says which candidate a request
    # PRESENTED; every push-time check in `_dispatch_task_push` (the execution
    # record still shows this candidate, it still resolves, it is still on the
    # task's line, its tree still matches) runs against it exactly as before,
    # and an approval that resolves no binding at all is still refused. This
    # restores a binding that was lost; it never invents one.

    @staticmethod
    def _sent_postcommit_records(state: LoopState) -> list[dict]:
        """`state.sent_postcommits` as a list of dicts, whatever is on disk.

        The one place the field's shape is trusted, so a hand-edited or
        half-written state file cannot raise from three different call sites.
        A non-list reads as EMPTY and a non-dict entry is dropped — fail-closed
        in both directions, since an entry that is not there resolves no
        approval. `reversed(5)` and `for record in 5` both raise `TypeError`
        deep inside `_step_ready` / `_dispatch`, where nothing catches it.
        """
        raw = state.sent_postcommits
        if not isinstance(raw, list):
            return []
        return [record for record in raw if isinstance(record, dict)]

    def _record_sent_postcommit(
        self,
        state: LoopState,
        request_id: str,
        head_sha: str,
        report_sha256: str,
        binding: PostcommitBinding,
    ) -> None:
        """Remember that `request_id` went out carrying `binding`.

        Called from the only two places a request's stamps are ever written —
        `_step_ready` (a request is born) and `_fall_back_to_omission` (an
        unsent request is re-stamped around an omitted diff). Keeping both in
        step is what stops a ledger entry from describing a digest the request
        no longer has, which would refuse a legitimate approval at push time
        with nothing on screen explaining it.

        Replaces any existing entry for the same id rather than appending a
        second one, so re-stamping updates rather than duplicating.
        """
        records = [
            record
            for record in self._sent_postcommit_records(state)
            if record.get("request_id") != request_id
        ]
        records.append(
            {
                "request_id": request_id,
                "head_sha": head_sha,
                "report_sha256": report_sha256,
                "postcommit": asdict(binding),
            }
        )
        state.sent_postcommits = records[-MAX_SENT_POSTCOMMIT_RECORDS:]

    def _forget_sent_postcommits_for_task(self, state: LoopState, task_id: str) -> None:
        """Drop every ledger entry for `task_id` — called once its candidate is
        actually published.

        Not tidying. Publication does not change `execution.candidate_sha`, so
        without this a SECOND `push` naming the same already-answered packet
        would resolve the same binding, pass every push-time check, and re-run
        the completion path (re-marking the task completed, re-triggering the
        auto-merge) for work that already shipped. Forgetting the packet makes
        that a refusal instead.
        """
        state.sent_postcommits = [
            record
            for record in self._sent_postcommit_records(state)
            if not (
                isinstance(record.get("postcommit"), dict)
                and record["postcommit"].get("task_id") == task_id
            )
        ]

    def _carry_postcommit_forward(
        self, binding: PostcommitBinding | None = None
    ) -> None:
        """Record the binding of the response being corrected, so the corrective
        re-prompt about to be queued inherits it.

        Called by all five corrective-re-prompt sites (`_handle_parse_error`,
        `_handle_policy_denial`, `_handle_review_mismatch`,
        `_dispatch_plan`'s plan-rejection branch, and `_handle_git_failure`)
        and deliberately not
        special-cased per site: they build the same kind of message — "that
        reply cannot be acted on, send another one about the SAME presented
        state" — and a binding that survives one of them and not the others is
        a trap that only shows up on whichever path nobody tested. Plan
        rejection self-resolves to a no-op: a `plan` reply answers a request
        that never carried a binding, so there is nothing to carry.

        `binding` is the binding the CALLER already resolved for this response,
        and it wins over `resp.postcommit` when both exist — the same
        precedence `_push_binding` and `_dispatch` use, stated once so there is
        never a second resolution order for the same question. It exists
        because `resp.postcommit` is not the only authoritative binding a
        response can have: an approval that NAMES a postcommit packet this loop
        sent (`_approval_packet`) resolves one while `resp.postcommit` is
        `None`, and a correction built from THAT response — bad stamps
        (`_handle_review_mismatch`) or a refused decision
        (`_handle_policy_denial`) — would otherwise go out unbound. That is not
        a hypothetical: `review_mismatch_payload` tells the reviewer to stamp
        "THIS request if you are approving the state described above", so the
        re-stamped approval names the CORRECTION, `_approval_packet` no longer
        applies (the reviewed id is the response's own), and an unbound
        correction turns the loop's own repair request into
        `legacy_git_path_retired` — the exact bug this whole mechanism exists to
        remove, reached one path over.

        The other three sites pass nothing, and each for a reason rather than by
        omission. `_handle_parse_error` is raised before any directive exists to
        resolve a binding FROM; plan rejection answers a request that never
        carried one; and `_handle_git_failure` is reached from `run`'s exception
        handlers, which have no resolved binding in scope. That last one does
        not dead-end: `git_error_payload` does not redirect the stamp to THIS
        request, so the reviewer re-names the packet and `_approval_packet`
        resolves it from the ledger — and if it does stamp the correction, it
        meets `_legacy_git_verdict`'s actionable `revise` refusal.

        Passing it WIDENS NOTHING. It decides what the next request is bound
        to, never whether a push is authorized: the corrected approval still
        goes through `authorize_directive` (re-evaluated against the carried
        binding's own `task_branch`, so a protected-branch denial denies again),
        still through `verify_review` against the correction's own three
        stamps, and still through every check in `_dispatch_task_push`.

        MUST be called BEFORE the caller clears `state.last_response`, and only
        on the path that actually sends a correction — a budget exhaustion
        parks or stops instead, and leaving a carry behind for a request that
        will never be built is how a binding attaches to something unrelated.
        """
        state = self.state
        resp = state.last_response
        if binding is None:
            binding = resp.postcommit if resp is not None else None
        if binding is None:
            return
        state.carry_postcommit = asdict(binding)
        self._log(
            "postcommit_carry_recorded",
            # `resp` is not None at any site that reaches here today, but the
            # early return above stopped being the thing that guarantees it the
            # moment an explicit binding could arrive without one.
            request_id=resp.request_id if resp is not None else None,
            data={
                "task_id": binding.task_id,
                "candidate_sha": binding.candidate_sha,
            },
        )

    def _consume_carried_postcommit(self, state: LoopState) -> PostcommitBinding | None:
        """Take the carried binding, if there is a usable one, and clear it.

        ALWAYS clears, whether or not the value was usable and whether or not
        the caller ends up using it: a carry is for the NEXT request and one
        left behind would attach to a later, unrelated one.

        An unreadable record (hand-edited state file, a half-written save)
        reads as ABSENT and says so in the transcript rather than raising —
        `postcommit_binding_from_record` explains why the tolerant reader is
        the right one here. Absent means the corrective re-prompt binds
        nothing, which is exactly the behaviour that predates this mechanism.

        A carry is only meaningful while the candidate it names is still this
        session's live, unpublished candidate, so it is checked against
        `state.task_execution` before it is honoured. In the intended path that
        is always true — the carry is consumed by the very next request, and
        only a publish or a further round can move `task_execution` — which is
        exactly why the check is worth making: it costs nothing when the
        mechanism is behaving and it is the one thing that would stop a carry
        surviving into some future path nobody has written yet from
        re-presenting a superseded candidate as bound.
        """
        raw = state.carry_postcommit
        state.carry_postcommit = None
        if not raw:
            return None
        binding = postcommit_binding_from_record(raw)
        if binding is None:
            self._log("postcommit_carry_unusable", data={"record": str(raw)[:200]})
            return None
        live = state.task_execution if isinstance(state.task_execution, dict) else {}
        if (
            live.get("task_id") != binding.task_id
            or live.get("candidate_sha") != binding.candidate_sha
        ):
            self._log(
                "postcommit_carry_stale",
                data={
                    "task_id": binding.task_id,
                    "candidate_sha": binding.candidate_sha,
                    "live_task_id": str(live.get("task_id") or ""),
                    "live_candidate_sha": str(live.get("candidate_sha") or ""),
                },
            )
            return None
        return binding

    def _approval_packet(
        self, directive: Directive, resp: LastResponse | None
    ) -> tuple[dict | None, PostcommitBinding | None]:
        """The sent postcommit packet this approval NAMES, when it names one
        other than the request it is answering — `(record, binding)`, or
        `(None, None)`.

        A BACKUP for the `last_response` binding, not a replacement, and the
        gate that makes it one is `reviewed.request_id != resp.request_id`.
        Every ordinary round (and every corrective re-prompt that inherited a
        binding) answers the request it was asked, so those keep taking exactly
        the path they always did — `verify_review` against the response's own
        stamps, `_dispatch` against `resp.postcommit` — and nothing this method
        does can change their outcome. Only the case the loop used to have no
        answer for at all consults the ledger. That ordering is deliberate:
        making the ledger primary would put a second source of truth in front
        of the common path, where any future site that re-stamps a request
        without updating the ledger would start refusing legitimate approvals.

        NOT A WIDENING. The named packet's own three stamps are then what
        `verify_review` demands, so the approval must still match the exact
        request it claims to have reviewed; the binding it yields still names
        one candidate sha and one tree; and every push-time check runs
        unchanged. What it removes is only the loop's inability to recognise
        its own packet once the conversation moved past it.

        Restricted to `push`: `commit` / `commit_and_push` are retired
        (docs/SECURITY.md S21) and must stay refused, so there is no reason to
        resolve a binding for them. Skipped entirely when the response carries
        an operator-changeset binding, so `_dispatch_changeset_push`'s path is
        untouched.
        """
        if directive.decision is not Decision.PUSH:
            return None, None
        if resp is None or resp.changeset is not None:
            return None, None
        reviewed = directive.reviewed
        if reviewed is None or not reviewed.request_id:
            return None, None
        if reviewed.request_id == resp.request_id:
            return None, None
        for record in reversed(self._sent_postcommit_records(self.state)):
            if record.get("request_id") != reviewed.request_id:
                continue
            binding = postcommit_binding_from_record(record.get("postcommit"))
            if binding is None:
                self._log(
                    "postcommit_packet_record_unusable",
                    data={"request_id": reviewed.request_id},
                )
                return None, None
            return record, binding
        return None, None

    def _push_binding(
        self, directive: Directive, resp: LastResponse | None
    ) -> PostcommitBinding | None:
        """Which produce-then-review candidate this `push` publishes: the
        packet the approval NAMES if that is a packet this loop sent, otherwise
        the binding of the request it is answering. `None` means no
        authoritative binding exists and the push must be refused."""
        _record, binding = self._approval_packet(directive, resp)
        if binding is not None:
            return binding
        return resp.postcommit if resp is not None else None

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
        loop is listening. An operator who moved the loop by hand, or a
        conversation rotation an older process performed (the machinery is gone
        since brw-15, but its leftovers are not), reuses the request id in the
        replacement chat, so a hit elsewhere can be a retired copy — and
        adopting a chat on that evidence would rebind epoch,
        `state.conversation_url` and the config URL on a duplicate id. The
        operator gets told which chat instead, which is the one fact that was
        missing. Nor is such a hit evidence of ABSENCE here: the search returns
        on its first sighting and stops walking.
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
            # route CONDEMNS THE WRONG CHAT. The search walks the project page
            # and up to `limit` OTHER chats, so the wedged page here is usually
            # not even this request's conversation — and
            # `_handle_conversation_unusable` would park loop_fatal naming the
            # conversation this request is bound to, on evidence gathered from
            # a stranger's. (Before brw-15 it was worse still: that route
            # rotated, which POSTS the request id into a replacement chat — a
            # send, from the one phase whose entire contract is that only
            # `--resubmit` repeats one.) A wedged page is also no evidence about
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
        # Aimed at the request's OWN conversation, never at `state.
        # conversation_url`. The two are the same for every request this
        # process creates; they diverge for one created before the loop was
        # moved to another chat (an operator repointing the config, or a
        # rotation an older process performed) — and then a reply that arrives
        # late in the abandoned chat is never the one that gets read.
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
                # The verb the reviewer would have used, when it named one.
                # `None` on every ordinary directive, which is every directive
                # written before this field existed.
                "wanted_decision": directive.wanted_decision,
            },
        )
        # Recorded and COUNTED, never acted on. This is the only place the
        # field is consumed, and what it produces is a transcript event and a
        # number in a file — `_dispatch` below branches on `directive.decision`
        # alone, and nothing anywhere converts this string to a `Decision`. See
        # `contract.Directive.wanted_decision` for why that bound is the whole
        # point of the field.
        self._record_wanted_decision(directive, resp)
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
        #
        # `named_packet` is the sent postcommit packet this approval NAMES when
        # that is NOT the request it answers — the "the conversation moved on"
        # case (see `_approval_packet`). It is `None` for every ordinary round,
        # and where it is `None` everything below is byte-for-byte what it was.
        named_packet, named_binding = self._approval_packet(directive, resp)
        push_binding = named_binding if named_binding is not None else resp.postcommit
        if push_binding is not None and directive.decision in PUSH_DECISIONS:
            destination_branch = push_binding.task_branch
        elif resp.changeset is not None and directive.decision in PUSH_DECISIONS:
            destination_branch = resp.changeset.branch
        else:
            destination_branch = self._git.current_branch()
        verdict = self._policy.authorize_directive(directive, destination_branch, self._registry)
        if not verdict.allowed:
            # `named_binding`, not `push_binding`: the carry's job is to rescue
            # the binding that `resp.postcommit` does NOT hold, and passing the
            # latter would be handing `_carry_postcommit_forward` the value it
            # already reads for itself. Where both exist they name the same
            # candidate anyway (`_consume_carried_postcommit` refuses a carry
            # that is not the live one). `None` for every non-push denial,
            # which is every denial that reached here before.
            self._handle_policy_denial(directive, verdict, named_binding)
            return
        if directive.decision in REVIEWED_DECISIONS:
            # Verified against the packet the approval NAMES when it names one
            # this loop sent, otherwise against the request it answers — never
            # against a mixture of the two. An approval that names an earlier
            # packet must match THAT packet's three stamps exactly, which is
            # the same demand `verify_review` has always made, asked of the
            # request the reviewer says it reviewed rather than of whatever was
            # sent most recently.
            expected_request_id = resp.request_id
            expected_head_sha = resp.head_sha
            expected_report_sha256 = resp.report_sha256
            if named_packet is not None:
                expected_request_id = str(named_packet.get("request_id", ""))
                expected_head_sha = str(named_packet.get("head_sha", ""))
                expected_report_sha256 = str(named_packet.get("report_sha256", ""))
                self._log(
                    "approval_bound_to_named_packet",
                    request_id=resp.request_id,
                    data={
                        "named_request_id": expected_request_id,
                        "task_id": named_binding.task_id,
                        "candidate_sha": named_binding.candidate_sha,
                    },
                )
            try:
                verify_review(
                    directive,
                    expected_request_id,
                    expected_head_sha,
                    expected_report_sha256,
                )
            except ContractError as exc:
                # The correction this raises asks for a RE-STAMP of the same
                # state, against THIS request (`review_mismatch_payload`) — so
                # it has to carry the binding the approval resolved, or the
                # reply the loop just asked for arrives unbindable.
                self._handle_review_mismatch(exc, named_binding)
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
                        ),
                        named_binding,
                    )
                    return
        state.policy_denials = 0
        self._dispatch(directive)

    def _record_wanted_decision(self, directive: Directive, resp: LastResponse) -> None:
        """Count and record a `wanted_decision`, and do nothing else with it.

        A NO-OP when the directive carries none, which is every directive today
        — no event, no file, nothing on disk. That is what makes the field free
        for the existing protocol: a reply that omits it behaves exactly as it
        did before this existed.

        **It cannot influence what happens next, structurally.** This method
        neither returns a value nor mutates anything the dispatch reads: it
        writes one transcript event and increments one counter in
        `state_dir/wanted_decisions.json`. `_dispatch` selects its branch from
        `directive.decision`, a `Decision` enum member the parser produced, and
        this string is never converted to one — `parse_response` deliberately
        does not even validate it against `Decision`, so a reviewer writing
        `"push"` here gets it COUNTED, not executed. That is the hard bound the
        field is designed around (`docs/SECURITY.md` finding #2's circular
        ownership: the reviewer must never be able to name an action the policy
        engine did not authorize).

        The event's `result` key carries the rendered running total, because
        `dashboard.collect`'s recent-events feed shows `decision`/`code`/
        `error`/`result` in that order — with none of the first three present,
        an operator watching the live page reads `wanted: recut x7, split x3`
        without anything in the dashboard having to know this field exists.
        """
        wanted = directive.wanted_decision
        if not wanted:
            return
        counts, reset = self._wanted_decisions.record(wanted)
        self._log(
            "wanted_decision",
            request_id=resp.request_id if resp is not None else None,
            data={
                "wanted": WantedDecisionTally.normalise(wanted),
                # What the reviewer sent verbatim, before normalisation — a
                # value that was truncated or folded should still be readable
                # in full somewhere.
                "wanted_raw": wanted,
                # NOT under the key `decision`: that is the dashboard's first
                # detail choice, and this event is about the verb that was NOT
                # taken. Naming it `with_decision` keeps the two apart in the
                # record as well as on the page.
                "with_decision": directive.decision.value,
                "reason": directive.reason,
                "task_id": directive.task_id,
                "tally": dict(counts),
                # True only when a tally file existed and could not be read, so
                # a rebuilt count is never mistaken for a first sighting.
                "tally_reset": reset,
                "result": WantedDecisionTally.render(counts),
            },
        )

    # ---- dispatch -----------------------------------------------------------

    def _dispatch(self, directive: Directive) -> None:
        state = self.state
        decision = directive.decision
        # Resolved ONCE, here, so the branch condition and the argument passed
        # to `_dispatch_task_push` cannot disagree about which candidate this
        # approval publishes. `None` for every decision that is not a `push`.
        push_binding = (
            self._push_binding(directive, state.last_response)
            if decision is Decision.PUSH and state.last_response is not None
            else None
        )
        if decision is Decision.STOP:
            self._handle_contract_stop(directive)
        elif decision is Decision.RECUT:
            # The reviewer discarding an unsalvageable candidate itself. Its own
            # branch rather than a case inside `_dispatch_executor`, because it
            # runs NO executor and starts no round: it retires the execution and
            # puts the task back in the queue, and the next `implement` for it
            # is an ordinary first dispatch off the current base.
            self._dispatch_recut(directive)
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
        elif decision is Decision.PUSH and push_binding is not None:
            # A push answering a produce-then-review packet publishes via
            # `push_exact`, sourced entirely from the binding (never from
            # `directive` — see `_dispatch_task_push`'s docstring).
            #
            # The binding is `resp.postcommit` for every ordinary round, and
            # for a corrective re-prompt that inherited one. `_push_binding`
            # additionally recognises an approval that NAMES a postcommit
            # packet this loop sent, for the case where `last_response` has
            # moved past it — the same identity, resolved by the id the
            # reviewer cited rather than by recency.
            self._dispatch_task_push(directive, state.last_response, push_binding)
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
            self._handle_policy_denial(directive, self._legacy_git_verdict(decision))
        else:  # audit / implement / revise
            self._dispatch_executor(directive)

    def _unpublished_candidate(self) -> tuple[str, str] | None:
        """`(task_id, candidate_sha)` of the produce-then-review candidate this
        session is holding unpublished, or `None`.

        Read from `state.task_execution`, which `_dispatch_task_push` clears the
        moment a candidate is actually published — so a value here means there
        really is work waiting for an approval, not merely that a task once ran.
        Display only: nothing branches on it and no push is ever sourced from it
        (that is the whole point of `PostcommitBinding`).
        """
        task_exec = self.state.task_execution
        if not isinstance(task_exec, dict):
            return None
        task_id = str(task_exec.get("task_id") or "")
        candidate_sha = str(task_exec.get("candidate_sha") or "")
        if not task_id or not candidate_sha:
            return None
        return task_id, candidate_sha

    def _legacy_git_verdict(self, decision: Decision) -> Verdict:
        """The refusal for a retired git decision — `commit`, `commit_and_push`,
        or a `push` no review packet binds.

        THE REFUSAL MUST NOT DEAD-END, and for an unbound `push` the generic
        sentence does. On 2026-08-20 the reviewer answered it by correctly
        refusing every subsequent packet ("the controller is repeatedly starting
        fresh sessions instead of presenting the required postcommit review
        packet"), each refusal ended the session, and each new session sent
        another kickoff — three cycles in fifteen minutes, `needs_attention`
        FALSE throughout, until a human discarded four rounds of approved work.
        The reviewer had nothing else to say: it was being told its approval was
        invalid, not what would make one valid.

        So when there IS an unpublished candidate on record, the refusal names
        it and names the one move that produces a packet an approval can bind
        to. It does not widen anything: the decision is still denied, the
        retired path is still unreachable, and `revise` goes through
        `authorize_directive` and the round cap like any other — a task with no
        review round left parks for an operator with the accumulated diff
        (`_park_round_cap`) instead of looping, which is the outcome this text
        is trying to reach anyway.

        The original sentence is kept verbatim and appended to rather than
        rewritten: it is the answer for `commit` / `commit_and_push`, where
        there is nothing to re-present because the commit the reviewer is asking
        for does not exist.
        """
        reason = (
            "direct commit/push is no longer supported — the orchestrator "
            "commits automatically after implementing (or auditing) a "
            "task in its own worker repo; the only valid approval is "
            "`push` with the `reviewed` stamp answering a postcommit or "
            "operator-changeset review packet"
        )
        candidate = self._unpublished_candidate()
        if decision is Decision.PUSH and candidate is not None:
            task_id, candidate_sha = candidate
            reason += (
                f". An unpublished candidate for task '{task_id}' "
                f"({candidate_sha[:12]}) IS on record here, but nothing in this "
                "session binds your approval to a review packet that presented "
                "it, so this loop cannot publish it and resending `push` will be "
                "refused the same way. It has to be PRESENTED AGAIN before it "
                f"can be approved: reply `revise` with task_id '{task_id}' and "
                "the loop will produce and send a fresh postcommit review "
                "packet — approve that packet's request_id"
            )
        return Verdict.deny("legacy_git_path_retired", reason)

    # ---- recut: the reviewer discards an unsalvageable candidate ------------
    #
    # THE ONE DESTRUCTIVE ACTION THE REVIEWER TAKES WITHOUT AN OPERATOR, and the
    # bounds below are what make that acceptable rather than decoration.
    #
    # Why it exists. `Decision` had eight members and none of them meant "this
    # branch is contaminated, cut it again from the base". On 2026-08-20 a
    # reviewer that had reached exactly that conclusion about port-01 issued the
    # only verb available — `revise` — while its own `reason` argued against
    # another ordinary retry, and the round before that it had spelled the
    # remedy out in prose. Prose in a `reason` field executes nothing. An
    # operator then performed the recovery by hand twice in one day
    # (roadmap-01, port-01); `changed_paths_outside_approved` has parked nine
    # distinct tasks, and four completed scope tasks never stopped it. Detection
    # existed; recovery was entirely manual.
    #
    # The five bounds, each with the check that carries it:
    #
    #   * CAP — `MAX_TASK_RECUTS` cuts per task, counted durably on
    #     `tasks.Task.recut_count` (`_recut_count_for`), the cut after which
    #     parks for a human (`_park_recut_cap`).
    #   * NEVER DISCARD WORK THAT MAY ALREADY BE APPROVED — a published
    #     candidate is refused outright, and so is one whose verdict is still
    #     outstanding (`_recut_outstanding_verdict`). Evidence this is real:
    #     budget-01's record was archived by an operator release at 21:33:52Z,
    #     54 seconds before the reviewer returned PUSH for that exact candidate.
    #   * NOTHING IS DELETED — the retirement goes through
    #     `release_task_to_pending`, i.e. `worktask.retire_execution`, which
    #     MOVES the record to `executions/archive/` and the worker to
    #     `quarantine/`, under one label, in one call.
    #   * DISTINCT FROM `stop` — `stop` parks because a human must decide; this
    #     is the reviewer deciding. `contract._RESPONSE_FORMAT` says so in those
    #     words, and says to use `stop` when unsure.
    #   * RECORDED — the transition is logged as `task_recut` with the
    #     reviewer's own reason and both retirement destinations, so a task that
    #     silently restarted is never a mystery afterwards.
    #
    # Everything here refuses through `_handle_policy_denial` rather than
    # parking, deliberately: a denial re-prompts with the reason, is bounded by
    # `check_denial_budget`, and lets the reviewer choose `stop` if a human
    # really is needed. A park would hold an autonomous session open for an
    # answer nobody is there to give. The ONE exception is the cap, which parks
    # on purpose — that is the whole point of a cap.

    def _recut_count_for(self, task: Task, execution) -> int:
        """How many cuts this task has already spent: the HIGHER of the durable
        registry count and whatever the live execution record mirrors.

        Two copies and `max`, not one copy, because each survives a failure the
        other does not. A recut ARCHIVES the execution record, so a count kept
        only there reads 0 on the fresh record and the cap enforces nothing;
        conversely a `tasks.json` row written before `recut_count` existed loads
        as 0 while the record that the last cut seeded still says 1. Taking the
        larger is the only combination in which neither loss can LOWER the
        count, which is the direction that matters — a cap that reads too low is
        a cap that is off.

        Defensive on the record's side only: `tasks.Task.recut_count` is
        validated at load (`tasks._persisted_recut_count` refuses anything that
        is not a non-negative int), while a `TaskExecution` is rehydrated by
        `TaskExecution(**data)` with no such gate, so a hand-edited record could
        hold a string. That reads as 0 here and the registry's own count still
        stands — never as a crash inside a dispatch.
        """
        recorded = getattr(execution, "recut_count", 0) if execution is not None else 0
        if isinstance(recorded, bool) or not isinstance(recorded, int) or recorded < 0:
            recorded = 0
        return max(int(task.recut_count or 0), recorded)

    def _recut_outstanding_verdict(
        self, state: LoopState, task_id: str, candidate_sha: str
    ) -> str:
        """The request id of a review packet that PRESENTED this exact candidate
        and that the directive being dispatched is not the reply to — or `""`
        when no verdict is outstanding.

        **What "outstanding" means here, precisely.** The loop presents a
        candidate once and the next directive it dispatches is the verdict on
        it. So for the candidate the reviewer is answering ABOUT, there is by
        construction no outstanding verdict — the reviewer IS the verdict, and
        refusing that case would refuse the recut's primary use (port-01's
        reviewer recutting the task whose packet it is reading). What is NOT
        answered is a packet for a DIFFERENT candidate: `_approval_packet` lets
        an approval resolve a packet by the request id it names, so a candidate
        still in `state.sent_postcommits` remains approvable by a later reply.
        Discarding one of those is exactly the budget-01 shape — work destroyed
        while a verdict for it was still in flight — with the reviewer in the
        operator's seat.

        Two ways a reply counts as the verdict on this candidate, and both are
        needed:

          * its OWN binding names it (`resp.postcommit`), which covers a
            corrective re-prompt that inherited the binding — there the answered
            request id is the correction's, not the packet's;
          * the ledger entry's request id is the one being answered.

        Reads the ledger through `_sent_postcommit_records`, which already
        fail-closes a non-list to empty and drops non-dict entries. An empty
        ledger means no outstanding verdict, and that is the honest answer
        rather than a hole: `sent_postcommits` lives on `LoopState`, which
        `cli._select_and_kickoff` replaces per session, so a task parked in an
        earlier session genuinely has no packet this loop is still waiting on.

        A candidate that was never committed (`candidate_sha == ""`) can never
        match an entry, so nothing was ever presented and nothing is
        outstanding — the caller does not special-case it.
        """
        resp = state.last_response
        bound = resp.postcommit if resp is not None else None
        if (
            bound is not None
            and bound.task_id == task_id
            and bound.candidate_sha == candidate_sha
        ):
            return ""
        answered_id = resp.request_id if resp is not None else ""
        outstanding = ""
        for record in self._sent_postcommit_records(state):
            presented = record.get("postcommit")
            if not isinstance(presented, dict):
                continue
            if (
                presented.get("task_id") != task_id
                or presented.get("candidate_sha") != candidate_sha
            ):
                continue
            request_id = str(record.get("request_id") or "")
            if request_id and request_id == answered_id:
                # This directive answers the packet that presented it. Return
                # immediately rather than remembering it: one answered
                # presentation settles the candidate, however many times it was
                # presented (a re-stamped or re-sent packet records a second
                # entry for the same candidate).
                return ""
            outstanding = request_id or "(a packet with no recorded request id)"
        return outstanding

    def _dispatch_recut(self, directive: Directive) -> None:
        """Retire `directive.task_id`'s execution and return the task to the
        queue, so its next dispatch is cut fresh from the current base.

        Every refusal below happens BEFORE anything moves, and the order is
        cheapest-and-most-specific first so the reviewer is told the actual
        reason rather than the first one that happens to fire.
        """
        state = self.state
        task_id = directive.task_id or ""

        if task_id == AUDIT_TASK_ID or task_id.startswith("audit-"):
            # An audit unit is synthetic, minted per iteration and never
            # planned, so there is no queue to return it to and a "fresh cut"
            # of it is just the next `audit`. Refused by NAME rather than by
            # registry lookup: most audit units are not in the registry at all,
            # so a lookup would answer `task_unknown` and send the reviewer
            # looking for a planning mistake.
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "recut_audit_unit",
                    f"'{task_id}' is an audit unit, not a roadmap task — there "
                    "is no queue to return it to, and a fresh audit is simply "
                    "`audit`. Nothing was discarded.",
                ),
            )
            return
        if not self._registry.has(task_id):
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "task_unknown",
                    f"task '{task_id}' is not in the registry, so there is no "
                    "execution to discard. Nothing was changed.",
                ),
            )
            return
        obstacle = self._registry.recut_obstacle(task_id)
        if obstacle is not None:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    obstacle.code,
                    f"{obstacle}. `recut` discards an in-flight or quarantined "
                    "round; it can neither un-finish completed work nor release "
                    "an operator's hold. Nothing was discarded.",
                ),
            )
            return
        if self._execution_store is None or self._worker_repos is None:
            # BOTH halves are required, and the worker manager is the one worth
            # spelling out: `retire_execution` quarantines only
            # `if worker_repos is not None`, so without one it would archive the
            # record, report success, and leave the contaminated worktree
            # exactly where the next dispatch looks for it — a recut that says
            # it discarded the branch and did not. Refusing is the honest
            # answer: this loop cannot perform the operation the verb promises.
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "recut_unavailable",
                    "this loop has no execution store and worker-repository "
                    "manager configured, so it cannot retire both halves of an "
                    "execution — and retiring one half is worse than retiring "
                    "neither. Nothing was discarded.",
                ),
            )
            return
        if state.pending_request is not None:
            # Defence in depth against a state the ordinary single-request flow
            # does not reach (`_step_awaiting` clears the pending request before
            # `_step_executing` runs). It is cheap, and it is the literal form
            # of the bound: never discard a candidate while this loop is waiting
            # to hear about one.
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "recut_verdict_outstanding",
                    "this loop is still waiting on a reply to request "
                    f"'{state.pending_request.request_id}', so a verdict is in "
                    "flight and nothing may be discarded yet. Nothing was "
                    "changed.",
                ),
            )
            return
        try:
            execution = self._execution_store.load(task_id)
        except (StateError, OSError) as exc:
            # Unreadable, NOT absent. Refusing is the fail-closed reading: a
            # record this cannot parse may name a published candidate or a
            # candidate under review, and archiving it unread would destroy the
            # only evidence of which.
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "recut_record_unreadable",
                    f"task '{task_id}' has an execution record this loop cannot "
                    f"read ({exc}), so it cannot be shown safe to discard. An "
                    "operator has to look at it. Nothing was discarded.",
                ),
            )
            return
        if execution is None:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "recut_no_execution",
                    f"task '{task_id}' has no execution record, so there is no "
                    "candidate to discard — its next dispatch is already a "
                    "fresh cut from the current base. Nothing was changed.",
                ),
            )
            return
        if execution.published_sha:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "recut_candidate_published",
                    f"task '{task_id}' has ALREADY PUBLISHED candidate "
                    f"{execution.published_sha[:12]} to "
                    f"{execution.intended_remote}/{execution.intended_remote_ref} "
                    f"at {execution.published_at or 'an unrecorded time'} — "
                    "published work is never discarded by this loop. If it is "
                    "wrong, that is a new task, not a recut. Nothing was "
                    "changed.",
                ),
            )
            return
        outstanding = self._recut_outstanding_verdict(
            state, task_id, execution.candidate_sha
        )
        if outstanding:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "recut_verdict_outstanding",
                    f"candidate {execution.candidate_sha[:12]} for task "
                    f"'{task_id}' was presented for review under request "
                    f"'{outstanding}', which this reply does not answer — so an "
                    "approval for it can still arrive and the work may already "
                    "be approved. Judge that packet first (approve it, or "
                    "`revise` the task), then recut if it is still needed. "
                    "Nothing was discarded.",
                ),
            )
            return
        task = self._registry.get(task_id)
        spent = self._recut_count_for(task, execution)
        if spent >= MAX_TASK_RECUTS:
            self._park_recut_cap(directive, task, execution, spent)
            return

        # A recut ARCHIVES the execution record, so the next dispatch starts
        # from `attempt_count = 0` — the ceiling this task may have asked to be
        # classified at no longer exists (ceil-01). Cleared here, in the same
        # registry write the retirement persists, or the stale marker would meet
        # the fresh cut's first `implement`: an identical plan would park it
        # `ceiling_plan_unchanged`, and a differing one would spend its single
        # extension on a budget nothing had spent.
        #
        # `registry.recut` clears it too, via the `_return_to_pending` all three
        # return-to-pending verbs share, so this is the belt to that braces and
        # is idempotent either way. Kept because this is the call site where the
        # reason is legible, and because it is the reason the shared clear was
        # written — `release` and `shelve` reach the same stale marker by the
        # same route and had no such line.
        if task.ceiling_plan_requested_at:
            self._registry.clear_ceiling_plan_request(task_id)
        try:
            release = release_task_to_pending(
                task_id,
                self._registry,
                self._execution_store,
                self._worker_repos,
                persist=lambda: self._task_store.save(self._registry),
                reason=RECUT_RETIREMENT_REASON,
                # Nobody is watching this the way an operator watches
                # `python -m autoloop release`: raising here would take the
                # process down mid-round. The `Release` says how far it got and
                # the park below reports it.
                tolerate_retirement_failure=True,
                move=self._registry.recut,
            )
        except TaskGraphError as exc:  # pragma: no cover - `recut_obstacle` ran
            # The registry refused the move after `recut_obstacle` said it
            # would not. Nothing has moved (`_return_to_pending` raises before
            # the assignment), so this is a denial and not a park.
            self._handle_policy_denial(
                directive, Verdict.deny(exc.code, f"{exc}. Nothing was discarded.")
            )
            return

        retirement = release.retirement
        self._log(
            "task_recut",
            request_id=state.last_response.request_id if state.last_response else None,
            data={
                "task_id": task_id,
                # The reviewer's OWN words for why, kept verbatim: this is the
                # transition's only account of itself, and a recut whose reason
                # is not in the transcript is the "task that silently restarted"
                # this record exists to prevent.
                "reason": directive.reason,
                "wanted_decision": directive.wanted_decision,
                "discarded_candidate": execution.candidate_sha,
                "discarded_base": execution.task_base_sha,
                "recut_count": release.task.recut_count,
                "cap": MAX_TASK_RECUTS,
                "label": retirement.label if retirement is not None else "",
                "archived_record": (
                    str(retirement.record_path)
                    if retirement is not None and retirement.record_path is not None
                    else ""
                ),
                "quarantined_worker": (
                    str(retirement.worker_path)
                    if retirement is not None and retirement.worker_path is not None
                    else ""
                ),
                "artifacts_retired": release.artifacts_retired,
                "obstacle": release.obstacle,
            },
        )
        # The task's own bookkeeping is gone; so must every pointer this session
        # still holds to it, or a later approval naming the discarded packet
        # would resolve a binding to work that no longer has a record.
        self._forget_sent_postcommits_for_task(state, task_id)
        if isinstance(state.task_execution, dict) and (
            state.task_execution.get("task_id") == task_id
        ):
            state.task_execution = None
        if isinstance(state.current_task, dict) and (
            state.current_task.get("task_id") == task_id
        ):
            state.current_task = None
        carried = state.carry_postcommit
        if isinstance(carried, dict) and carried.get("task_id") == task_id:
            state.carry_postcommit = None

        if not release.artifacts_retired:
            # The STATUS move is already durable (`release_task_to_pending`
            # persists before the artefacts move), so the task is pending with
            # its worker repo and/or its record still on disk — the next
            # dispatch would refuse to create over that directory, and a
            # surviving record holds the repository-wide merge window shut.
            # Neither is something a further message to the reviewer can fix.
            state.last_response = None
            self._to_needs_user(
                f"task {task_id}: the reviewer's `recut` returned the task to "
                "the queue, but its artefacts could not be retired — "
                f"{release.obstacle}. "
                + (
                    f"The worker repository is still at {release.stale_worker_path}, "
                    "where the next dispatch will refuse to create one. "
                    if release.stale_worker_path
                    else ""
                )
                + (
                    "Its execution record is still live, so the merge window "
                    "stays shut on it. "
                    if release.stale_execution_record
                    else ""
                )
                + "Move them aside by hand, then the task can be dispatched "
                f"again. Reviewer's reason: {directive.reason}",
                kind="task_fatal",
                code="recut_retirement_failed",
                task_id=task_id,
                detail=(
                    f"obstacle={release.obstacle} "
                    f"stale_worker={release.stale_worker_path} "
                    f"stale_record={release.stale_execution_record} "
                    f"residue_resumable={release.residue_resumable}"
                ),
            )
            return

        state.outbox = self._recut_report(directive, task_id, execution, release)
        state.last_response = None
        state.consecutive_failures = 0
        state.phase = Phase.READY.value
        self._store.save(state)

    def _recut_report(self, directive, task_id, execution, release) -> str:
        """What the loop tells the reviewer after a recut landed.

        Built here rather than from a `prompts.TEMPLATES` entry because it says
        one thing that no template shape covers: exactly what was discarded, and
        exactly how many cuts are left before the task parks for a human. A
        reviewer that cannot see the second number cannot spend the first
        budget sensibly.
        """
        retirement = release.retirement
        remaining = MAX_TASK_RECUTS - release.task.recut_count
        return (
            f"RECUT APPLIED — task {task_id} is back in the queue.\n\n"
            f"Your reason: {directive.reason}\n\n"
            f"Discarded candidate {execution.candidate_sha[:12] or '(none committed)'} "
            f"cut from base {execution.task_base_sha[:12]} "
            f"(review round {execution.review_round}, "
            f"{execution.attempt_count} attempt(s) spent).\n"
            "Nothing was deleted: the execution record was archived to "
            f"{retirement.record_path if retirement and retirement.record_path else '(no record on disk)'} "
            "and the worker repository quarantined at "
            f"{retirement.worker_path if retirement and retirement.worker_path else '(no worker on disk)'}"
            f", both under the label {retirement.label if retirement else '(none)'}.\n\n"
            f"This was recut {release.task.recut_count} of {MAX_TASK_RECUTS}. "
            + (
                f"{remaining} recut(s) remain for this task; after that it parks "
                "for a human instead of being cut again."
                if remaining > 0
                else "No recuts remain: another `recut` of this task will park "
                "for a human rather than cut it again."
            )
            + "\n\nThe next `implement` for this task starts from the CURRENT "
            "base with an empty tree. If the same work fails again from a clean "
            "cut, the task's specification is the problem, not its branch — say "
            "so with `stop` or reshape it with a new `decomposition`."
        )

    def _park_recut_cap(
        self, directive: Directive, task: Task, execution, spent: int
    ) -> None:
        """The cut after `MAX_TASK_RECUTS` parks for a human instead of cutting
        again — the bound that makes handing the reviewer a destructive verb
        acceptable.

        A PARK, deliberately, where every other recut refusal is a denial. The
        other refusals have an answer the reviewer can act on ("judge that
        packet first", "that task is not in flight"); this one does not. Two
        clean rebuilds that still could not produce a reviewable candidate is
        evidence about the SPECIFICATION, and re-prompting the reviewer would
        only invite the third cut the cap exists to refuse.

        Nothing is discarded here: the candidate, the worker repo and the
        execution record are all exactly where the last cut left them, so the
        operator has the whole arc to read.
        """
        self.state.last_response = None
        self._to_needs_user(
            f"task {task.id}: the reviewer asked to recut it again, but it has "
            f"already been recut {spent} time(s) (cap {MAX_TASK_RECUTS}). A task "
            "that cannot produce a clean candidate in that many cuts from the "
            "base has a specification problem, not a branch problem — the cuts "
            "shared nothing but its description, scope and approved plan. "
            "Nothing was discarded: candidate "
            f"{execution.candidate_sha[:12] or '(none)'} on "
            f"{execution.task_branch} and its worker repo are untouched. Rewrite "
            "the task or retire it; do not simply allow another cut.\n\n"
            f"Reviewer's reason: {directive.reason}",
            kind="task_fatal",
            code="recut_cap",
            task_id=task.id,
            detail=(
                f"recut_count={spent} cap={MAX_TASK_RECUTS} "
                f"branch={execution.task_branch} candidate={execution.candidate_sha}"
            ),
        )

    # ---- repeated stops -----------------------------------------------------
    #
    # A reviewer's `stop` is a legitimate verdict: it means a human should
    # decide, and one of them must keep working exactly as it always has. What
    # is NOT legitimate is the loop answering that verdict by opening another
    # session, sending another kickoff, and collecting the same refusal for as
    # long as the process runs. That happened on 2026-08-20 — three refusals in
    # fifteen minutes over one lost postcommit binding — and every automated
    # signal stayed green while it did, because a verdict is not a failure and
    # `policy.max_consecutive_failures` counts failures.
    #
    # So what is bounded here is the REPETITION, and specifically the repetition
    # OF A SITUATION rather than of stops. See `_stop_situation_fingerprint` for
    # what "the same situation" means and why the reviewer's own words are not
    # part of it.

    def _handle_contract_stop(self, directive: Directive) -> None:
        """Dispatch a reviewer's `stop`: end the session as it always has,
        unless this is the `MAX_REPEATED_STOPS`-th consecutive stop about one
        unresolved situation, in which case PARK instead.

        The ordering matters and is the reason the count is taken first: a stop
        that ends the session is a clean boundary continuous mode reacts to by
        starting the next session, so by the time anything downstream could
        notice a pattern the evidence (this session) has already been replaced.
        """
        record = self._observe_contract_stop(directive)
        if record is None:
            return  # the ledger was unusable; already parked, say nothing more
        if record.count >= MAX_REPEATED_STOPS:
            self._park_stop_livelock(record)
            return
        state = self.state
        state.stop_reason = directive.reason
        state.last_response = None
        state.phase = Phase.STOPPED.value
        # Classified explicitly, not left at the default: `stopped` is now
        # reached two ways (here, and `_to_fault_stop`), and a reader that
        # inferred "clean" from the phase alone would announce a run that
        # died on a wall as a healthy finish. See `LoopState.stop_kind`.
        state.stop_kind = "contract"
        self._log(
            "stopped",
            data={
                "reason": directive.reason,
                "kind": "contract",
                # How close this stop is to the park, so an operator watching
                # the transcript can see a livelock forming rather than only
                # learning about it once it has been paid for.
                "repeat_count": record.count,
                "repeat_ceiling": MAX_REPEATED_STOPS,
            },
        )
        self._store.save(state)

    def _observe_contract_stop(self, directive: Directive):
        """Charge this stop to the repeated-stop ledger and return the updated
        `state.StopRepetition`, or `None` when the ledger could not be used —
        in which case the loop has ALREADY been parked and the caller must do
        nothing further.

        FAIL-CLOSED, deliberately. A ledger that cannot be read or written —
        or a fingerprint that cannot be computed, since the digest is taken
        inside this same guard — cannot count anything, and the tempting
        reading of that ("no record, so this is the first stop") restarts the
        count on every single stop: the park would never fire, the loop would
        keep opening sessions, and no signal anywhere would say the detector
        had stopped working. That is precisely the shape of the incident this
        exists to end, one level up. So the loop parks instead, naming the
        file, because a guard that cannot run should be as loud as the thing
        it guards against.
        """
        try:
            return self._stop_repetitions.observe(
                fingerprint=self._stop_situation_fingerprint(),
                reason=directive.reason or "",
                session_id=self.state.session_id or "",
                now=utcnow_iso(),
            )
        except (StateError, OSError) as exc:
            path = self._stop_repetitions.path
            self._log(
                "stop_repetition_ledger_unusable",
                data={"path": str(path), "error": f"{type(exc).__name__}: {exc}"},
            )
            self._to_needs_user(
                "the repeated-stop ledger could not be read, written or "
                "computed, so the loop cannot tell a reviewer stopping it "
                "about something new from the same refusal repeating. Rather "
                "than keep opening sessions with that check switched off, the "
                f"loop has parked. Delete {path} (it is a counter — nothing "
                "else depends on it) and answer this blocker to carry on. The "
                "reviewer's stop was NOT acted on; its reason was: "
                f"{directive.reason!r}. The underlying error was "
                f"{type(exc).__name__}: {exc}",
                kind="loop_fatal",
                code="stop_repetition_ledger_unusable",
                detail=f"{path}: {type(exc).__name__}: {exc}",
            )
            return None

    def _stop_situation_fingerprint(self) -> str:
        """A digest of the situation a reviewer is stopping the loop ABOUT.

        Two stops carrying the same digest are two stops about one unresolved
        situation; a different digest means something moved between them and
        the count starts again. This is the whole definition of both "the same
        unresolved situation" and "reset on progress" — nothing enumerates the
        ways progress can happen, because every one of them changes one of the
        four inputs below.

        WHAT GOES IN, against the dimensions the situation is defined by:

        * **Task identity** — the session's `current_task` id, or `""`. Empty
          is the INCIDENT's own shape, not an edge case: those three refusals
          answered fresh kickoffs, which carry no selected task, and the task
          at fault (prof-01, holding an unpublishable candidate) was visible
          only through its execution record below.
        * **Execution record** and **candidate publication** — the raw bytes of
          every live `TaskExecutionStore` record. Publication is recorded there
          (`published_sha` / `published_at`), so "a candidate was published" is
          a change to this input and needs no separate term. The whole record
          counts, not a chosen subset: "no change in the execution record" is
          what the situation is defined by, and picking fields would silently
          decide that a change in an unpicked one is not progress.
        * **Registry state** — `TaskRegistry.to_dict()`. A completed task, a
          new task, a block, a re-prioritisation, an approved decomposition:
          every registry mutation moves this.
        * **Phase progress** — invariant at this observation point rather than
          absent. `_dispatch` only ever runs from `executing`, so the phase is
          the same string for every stop and could not distinguish two of them;
          progress that would have shown as a phase change (a dispatch, a
          packet, a publication) shows in the three inputs above instead.

        WHAT DELIBERATELY STAYS OUT:

        * **The reviewer's reason text.** In the incident the three refusals
          were worded differently while describing one situation, so a
          text-keyed counter would have missed it entirely. Two stops about
          genuinely different things differ HERE, in the state, which is also
          why "different reasons do not park" is really "different situations
          do not park".
        * **Repository HEAD.** Publication is already covered above, and HEAD
          moves for reasons that have nothing to do with the stopped situation
          — a merge sweep landing another task's work, an operator's own
          commit. Including it would hand the livelock a way to reset itself.
        * **Session id, timestamps, iteration counters.** They differ on every
          round by construction, so any of them would make every fingerprint
          unique and the park unreachable.

        Fail-closed on unreadable inputs: an execution record that cannot be
        read still contributes its raw bytes (never a decode, so corruption
        cannot raise here), and an OSError enumerating the directory is left to
        propagate to `_observe_contract_stop`, which parks. Quietly digesting
        "nothing" instead would make an unreadable store look like steady
        progress and switch the detector off.
        """
        state = self.state
        task_id = ""
        if isinstance(state.current_task, dict):
            # `task_id`, spelled the way BOTH writers spell it
            # (`_dispatch_task_postcommit` and `_resolve_audit_task`, and
            # `cli._summary` reads the same key). `id` is the `Task` dataclass's
            # field name and is NOT what lands in this dict — reading it here
            # would leave this term permanently empty, which is a term silently
            # contributing nothing rather than a term that is absent.
            task_id = str(state.current_task.get("task_id") or "")
        parts = [f"task={task_id}"]
        parts.append(
            "registry="
            + hashlib.sha256(
                json.dumps(self._registry.to_dict(), sort_keys=True).encode("utf-8")
            ).hexdigest()
        )
        parts.append(f"executions={self._execution_records_digest()}")
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()

    def _execution_records_digest(self) -> str:
        """Content digest of every LIVE task-execution record.

        Raw bytes per file, sorted by name — never `TaskExecutionStore.load`.
        Two reasons, both about not lying: a corrupt record raises from
        `load()`, and a fingerprint that raised would have to be answered by
        guessing; and the question here is only "did this change", for which
        the bytes are a better answer than a decoded subset of them.

        `glob("*.json")` does not recurse, so `archive/` is excluded — and
        retiring a record still shows up, because the top-level file it moves
        from disappears from this listing.

        No store configured (a hand-built Orchestrator) digests as the empty
        string, which is stable rather than absent: the other inputs then carry
        the fingerprint on their own.
        """
        store = getattr(self, "_execution_store", None)
        directory = getattr(store, "directory", None) if store is not None else None
        if directory is None or not Path(directory).is_dir():
            return ""
        digest = hashlib.sha256()
        for path in sorted(Path(directory).glob("*.json")):
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("utf-8"))
            digest.update(b"\n")
        return digest.hexdigest()

    def _park_stop_livelock(self, record) -> None:
        """The park this whole mechanism exists to produce.

        `_to_needs_user`, not a new terminal of its own, and that is the
        requirement rather than a convenience: it is what writes a
        `blockers.Blocker`, so `health` reports `stuck_blocked` /
        `needs_attention` and the AFK monitor alarms, and it is what makes
        `python -m autoloop answer <id> "..."` able to clear this exactly like
        any other blocker. The failure being fixed was an automated monitor
        that stayed green, so a quiet exit here would fix nothing.

        `loop_fatal`, not `task_fatal`. Repeated identical refusals are
        evidence about the CONTROLLER — in the incident the reviewer was right
        every time and the fault was the loop's own lost postcommit binding —
        and quarantining whichever task happens to be named would assert that
        the rest of the roadmap can proceed, which is exactly what nobody
        knows. It also matches this file's fail-closed default for a park whose
        blast radius is not established.

        THE REASON IS QUOTED VERBATIM. In the incident the reviewer's text was
        the diagnosis: it identified the controller's fault precisely, and an
        operator reading only "stopped repeatedly" would have had to rediscover
        it. It is quoted rather than summarised for the same reason
        `_to_needs_user` persists the exact `question`.

        A reason with NOTHING READABLE in it falls back to a placeholder rather
        than ending the question on a colon. `contract._require_str` already
        refuses an empty or whitespace-only `reason`, so no reviewer reply can
        produce this — the fallback covers a `Directive` handed straight to
        `_dispatch`, and a park whose text simply stopped would read as a bug in
        this mechanism rather than as the absence of an explanation. The
        `.strip()` costs nothing on a real reason: the contract already stripped
        it, so this cannot alter one word the reviewer wrote.

        THE LEDGER IS CLEARED HERE. Otherwise the count stays at or above the
        ceiling forever and the very next stop — after the operator answers,
        with the loop possibly fixed — parks again immediately, which is a new
        livelock wearing the old one's clothes. Cleared, a relapse costs the
        same bounded `MAX_REPEATED_STOPS` cycles as the first one did, and the
        blocker's own `recurrences` is what says it has happened before.
        """
        reason = (record.last_reason or "").strip() or "(no reason recorded)"
        self._log(
            "stop_livelock_parked",
            data={
                "count": record.count,
                "fingerprint": record.fingerprint,
                "first_seen_at": record.first_seen_at,
                "reason": reason,
            },
        )
        self.state.last_response = None
        # BEFORE the park, so a park that itself fails still leaves the counter
        # reset rather than a state that re-parks on every subsequent stop.
        try:
            self._stop_repetitions.clear()
        except OSError as exc:
            self._log(
                "stop_repetition_ledger_unusable",
                data={
                    "path": str(self._stop_repetitions.path),
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
        self._to_needs_user(
            f"The reviewer has answered `stop` {record.count} times in a row "
            "about the same unresolved situation — same task, same task "
            "registry, same execution records (so no candidate was published "
            "between them either, since publishing writes one) "
            f"(first seen {record.first_seen_at}). A `stop` is a verdict, not "
            "a failure, so nothing was counting these: the loop was ending each "
            "session and opening another one, and would have gone on doing that "
            "for as long as the process ran. It has parked instead. Read the "
            "reviewer's last words below — they usually name what is actually "
            "wrong — fix that, then answer this blocker. The reviewer's own "
            f"reason, verbatim: {reason}",
            kind="loop_fatal",
            code="stop_livelock",
            detail=(
                f"consecutive stops: {record.count} (ceiling "
                f"{MAX_REPEATED_STOPS}); situation fingerprint "
                f"{record.fingerprint}; first seen {record.first_seen_at}; "
                f"last seen {record.last_seen_at}"
            ),
        )

    def _dispatch_plan(self, directive: Directive) -> None:
        state = self.state
        specs = directive.tasks or ()
        ceiling_parent = self._ceiling_split_parent(state)
        if ceiling_parent is not None:
            # A `plan` while a task is waiting on an attempt-ceiling
            # classification IS that classification's decompose answer. Handled
            # in its own method because it does two things an ordinary plan never
            # does — carry the parent's spend onto the children, and retire the
            # parent into them — and because every one of its refusals has to
            # happen before either.
            self._dispatch_ceiling_split(directive, ceiling_parent)
            return
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
            # Routed through the same helper as the other three corrective
            # re-prompts rather than exempted, so no site is the one nobody
            # thought about. In practice it is a no-op: a `plan` reply answers a
            # request that never carried a postcommit binding, so there is
            # nothing to carry — which the helper decides by looking, instead of
            # this site asserting it. Recorded before `last_response` is cleared
            # on the line below, and undone by the budget branch that parks.
            self._carry_postcommit_forward()
            state.last_response = None
            if not budget.allowed:
                state.carry_postcommit = None
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

    def _refused_ahead_of_urgent(self, directive: Directive, is_audit: bool) -> bool:
        """Refuse an executor dispatch that would run ahead of the urgent pin.
        True when it refused (the caller returns immediately).

        THE URGENT PIN, enforced rather than merely offered. The pin already
        puts its task at the head of `next_ready()`, and that is the only task
        the CONTEXT block briefs (`context.build_context`'s `next_ready`), so
        the reviewer normally names it — but policy authorizes any READY task by
        id, so "offered next" is not "dispatched next", and the second one is
        the whole claim. Refused through `_handle_policy_denial`, the same
        budget-capped corrective re-prompt every other refused directive gets:
        the reviewer is told what to send instead and why, and a reviewer that
        keeps ignoring it runs out of denial budget and ends the run loudly
        rather than quietly working around the operator.

        **A FRESH `audit` IS REFUSED TOO** (2026-08-22, second round). It was
        exempt, on the reasoning that an audit takes no task out of the queue —
        true, but it takes the LOOP, for a measured 1282-second executor round,
        which is the only thing an urgent request is asking for. The exemption
        was not idle either: every new session opened on the audit kickoff,
        including the one a preemption had just started, so the reviewer was
        being invited to spend that round. `cli._start_new_session` now opens a
        pinned session on the urgent task instead, which is what makes this
        refusal a correction rather than a wall — the session is told what to
        send in the same breath as being told what it may not.

        **A `revise` OF THE AUDIT PSEUDO-TASK IS NOT REFUSED**, and the
        distinction is not cosmetic. That directive continues an audit arc
        already in flight — round 1's commit lives in a worker repo that only
        round 2 can reach (`_resolve_audit_task` resumes the same unit id) — so
        refusing it would abandon real work to save nothing: the round it
        would displace is one this loop already paid for. It also cannot
        smuggle a round in AFTER a preemption, which is the case that has to
        hold: a preemption clears `state.current_task`, and a revise-of-audit
        with no audit on record parks (`audit_revise_no_record`) instead of
        minting a unit.

        `python -m autoloop run --kickoff-audit` builds the audit payload
        directly rather than through `_start_new_session`, so an operator who
        runs it while their own pin is live still reaches this refusal. That is
        left as is deliberately: the two instructions contradict each other,
        and the refusal names the pin that is being contradicted.
        """
        urgent = self._registry.live_urgent_target()
        if urgent is None:
            return False
        if directive.decision is Decision.AUDIT:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "urgent_target_pending",
                    f"task '{urgent.id}' was marked URGENT by the operator "
                    f"({urgent.urgent_reason}) and must be the next unit of work "
                    "dispatched — a fresh `audit` is a full executor round and "
                    "cannot start ahead of it. Send `implement` with task_id "
                    f"'{urgent.id}' instead; the roadmap line in the CONTEXT "
                    "block names it too. The audit is not cancelled — request it "
                    "again once the urgent task has been dispatched. Nothing was "
                    "executed and no attempt was spent.",
                ),
            )
            return True
        if is_audit or directive.decision not in TASK_DECISIONS:
            return False
        if directive.task_id == urgent.id:
            return False
        self._handle_policy_denial(
            directive,
            Verdict.deny(
                "urgent_target_pending",
                f"task '{urgent.id}' was marked URGENT by the operator "
                f"({urgent.urgent_reason}) and must be the next task "
                f"dispatched — `{directive.decision.value}` of "
                f"'{directive.task_id}' cannot start ahead of it. Send "
                f"the same decision for '{urgent.id}' instead; the "
                "roadmap line in the CONTEXT block names it too. "
                "Nothing was executed and no attempt was spent.",
            ),
        )
        return True

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

        The one gate in front of all three is `_refused_ahead_of_urgent`: while
        an operator's urgent pin is live, nothing but that task's own dispatch
        (and a `revise` continuing an audit arc already in flight) may start.
        """
        state = self.state
        is_audit = (
            directive.decision is Decision.AUDIT or directive.task_id == AUDIT_TASK_ID
        )
        if self._refused_ahead_of_urgent(directive, is_audit):
            return
        if not is_audit and directive.decision in TASK_DECISIONS:
            task = self._registry.get(directive.task_id)
            if not self._ceiling_reply_ok(directive, task):
                # This task asked the reviewer to classify it at its attempt
                # ceiling and the answer was refused or parked. Checked BEFORE
                # `set_decomposition` below, which is what keeps an ordinary
                # mid-task plan reshape from silently widening a ceiling: the
                # grant happens there, gated on the request, or not at all.
                return
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

    # ---- strand reconciliation (strand-01) -----------------------------------

    def _reconcile_stranded_tasks(self) -> None:
        """Return every task the environment stranded to the queue, or file a
        blocker saying why it could not be.

        THE invariant this exists for: after a round ends in an environment
        fault, its task is either back in the pool `next_ready()` draws from, or
        an OPEN BLOCKER names it and says why it is not. There was a third state
        until strand-01, and a task could sit in it indefinitely with no symptom
        other than its own absence — on 2026-08-22 an outage killed fourteen
        consecutive rounds in ten minutes, and three of the six tasks dispatched
        into it (scope-05 P1, contract-01, recov-01) sat `in_progress` for
        twenty-one hours. `next_ready()` returns READY tasks and an
        `in_progress` task is not one, so the loop never re-offered them; no
        blocker was filed; the dashboard showed work in flight. They were found
        only because an unrelated analysis happened to list in-progress tasks.

        **Run at the top of `_step_ready`, and the ORDER is the point.**
        `_step_ready` is where the next packet is built, and `build_context`
        reads `next_ready()` while building it — so a task released here is in
        the pool for the very packet that asks the reviewer what to do next,
        rather than one round later.

        **What is safe to requeue is decided by `health.stranded_fault_rounds`**
        (see it for the four conditions, including the one that keeps this from
        firing on a healthy round: the task the loop's own session names as
        current is exempt while its round is YOUNGER THAN THE ROUND CEILING, so
        a round that has just faulted keeps the reviewer's redo — that is how
        quota-01 and dash-18 recovered on their own in the incident).

        **That exemption is bounded, and the bound is not decoration.**
        `state.task_execution` is replaced only by the NEXT dispatch, so a
        faulted task that nothing displaces stays "current" indefinitely — with
        an unconditional exemption this sweep would skip it forever while
        `next_ready()` also refuses it and no blocker named it, which is this
        task's own defect rebuilt one level up. Past `health.round_ceiling_for`
        (the agent ceiling the executor KILLS a round at, plus a grace for the
        validation/commit/packet tail) the round provably is not executing, so
        the task is judged like any other strand: requeued if the shape is safe,
        blocked if it is not. Nothing here can sweep a live round — this method
        runs from `_step_ready`, the loop is single-threaded, and a round that
        is executing is inside `_dispatch_executor` rather than here.

        **The record is KEPT, and the status is moved with the bare
        `TaskRegistry.release`.** This deliberately does NOT call
        `release_task_to_pending`, which is otherwise THE release path
        (`cli._cmd_release`, `_preempt_for_urgent`), and the reason is exact
        rather than stylistic: that function retires the execution record, so
        the next dispatch mints a fresh one with `attempt_count = 0` and
        `fault_attempt_count = 0`. That would hand the task an allowance it did
        not earn and delete the only bound on an outage — fault, requeue, fresh
        record, fault — which is the dispatch loop this must not build. Keeping
        the record is what makes the bound provable: every requeued dispatch
        still charges `fault_attempt_count`, so a task faulting into a dead API
        walks into `fault_attempt_ceiling` in `MAX_TASK_FAULT_ATTEMPTS`
        dispatches and parks with a blocker, exactly as it does without this
        sweep. Nothing here refills either counter, and nothing here resets
        `pending_fault_code`.

        Keeping the record costs nothing elsewhere: a record with an empty
        `candidate_sha` does not hold the merge window shut
        (`cli._merge_window_blockers` skips it), and the safe shape is empty by
        definition.

        **A task whose budget is already spent is NOT requeued** — the next
        dispatch would refuse it and park anyway, and the strand would stay
        invisible until something chose it. It gets the blocker instead, which
        is the same answer the ceiling itself would give, one round earlier and
        without needing the reviewer to pick the task first.

        Saves `tasks.json` only when something actually moved: this runs every
        round, and a file rewritten on each pass is noise in the escape
        detector's snapshot for no gain (the same rule
        `cli._reconcile_unblocked_tasks` follows).
        """
        if self._execution_store is None:
            return
        current = (self.state.task_execution or {}).get("task_id") or ""
        strands = stranded_fault_rounds(
            self._registry,
            self._execution_store,
            current,
            current_round_age_seconds(self.state),
            round_ceiling_for(self._config),
        )
        if not strands:
            return
        released: list[StrandedRound] = []
        for strand in strands:
            if strand.obstacle:
                self._report_strand_blocker(strand, strand.obstacle)
                continue
            # THE SAME ceiling the dispatch would apply, not a second copy of the
            # base constant (ceil-01): a task whose budget the reviewer extended
            # has attempts left, and blocking it here as "already spent" would
            # park exactly the task that was just told to carry on. Falls back to
            # `MAX_TASK_ATTEMPTS` for an id the registry does not hold, which is
            # the reading that refuses rather than the one that admits.
            task_cap = (
                self._attempt_cap_for(self._registry.get(strand.task_id))
                if self._registry.has(strand.task_id)
                else MAX_TASK_ATTEMPTS
            )
            if (
                strand.attempt_count >= task_cap
                or strand.fault_attempt_count >= MAX_TASK_FAULT_ATTEMPTS
            ):
                self._report_strand_blocker(
                    strand,
                    "its attempt budget is already spent "
                    f"(attempts {strand.attempt_count}/{task_cap}, "
                    f"faults {strand.fault_attempt_count}/{MAX_TASK_FAULT_ATTEMPTS}), "
                    "so the next dispatch would refuse it",
                )
                continue
            try:
                self._registry.release(strand.task_id)
            except TaskGraphError as exc:
                # Reported, never skipped. A silent `continue` here is the exact
                # fail-open this whole sweep exists to close: the task would
                # stay in_progress, out of the pool, with nothing saying so.
                self._report_strand_blocker(
                    strand, f"the registry refused to release it ({exc.code})"
                )
                continue
            released.append(strand)
        if not released:
            return
        self._task_store.save(self._registry)
        for strand in released:
            self._log(
                "task_strand_requeued",
                data={
                    "task_id": strand.task_id,
                    "fault_code": strand.fault_code,
                    # Both counters, so the bound is auditable from the
                    # transcript alone: neither is changed by the requeue, and a
                    # reader can watch `fault_attempt_count` climb toward the
                    # ceiling across an outage instead of inferring it.
                    "attempt_count": strand.attempt_count,
                    "fault_attempt_count": strand.fault_attempt_count,
                    # Which of the two ways it stopped being scheduled: the loop
                    # dispatched something else, or the loop still names this
                    # task and only the round ceiling proved that claim stale.
                    # The second arm is invisible in the state file itself —
                    # `task_execution` still names the task after this runs —
                    # so the transcript is the only place it can be read.
                    "stale_current": strand.stale_current,
                    "note": (
                        "its round was destroyed by the environment and nothing "
                        "was scheduling it — returned to pending with its "
                        "execution record and both attempt budgets untouched"
                    ),
                },
            )

    def _report_strand_blocker(self, strand: StrandedRound, obstacle: str) -> None:
        """File (or retain) an OPEN blocker naming a task this sweep would not
        return to the queue, and say so in the transcript.

        The other half of the invariant. A task that cannot be requeued
        automatically must still be VISIBLE — the twenty-one hours the incident
        cost were bought by silence, not by the strand itself.

        **Retains rather than re-records.** An open blocker for the same
        `(task_id, code)` is left exactly as it is: `BlockerStore.record` is an
        idempotent upsert that bumps `recurrences`, and `recurrences` means "this
        condition re-parked", not "a sweep looked at it again" — running every
        round, this would inflate it into noise. Matched on task and code and
        deliberately NOT on phase (unlike `BlockerStore.find_open`), because the
        loop's phase when it notices a strand says nothing about the strand.

        **Never changes the task's status.** Not to `blocked`, not to anything:
        the unsafe cases are precisely the ones holding a candidate or a
        reviewed round, and `blocked` is a merge-window exemption
        (`cli._merge_window_blockers` treats BLOCKED_BY_OPERATOR as terminal) —
        so quarantining one of these would open the window on live reviewed
        work. Reporting is the whole action.

        Best-effort on the store, loud in the transcript. A blocker directory
        that cannot be read or written must not take down a round, so the
        transcript entry is written either way and carries what went wrong; the
        entry is what a later reader greps for the task id and the fault code.
        """
        blocker_id = ""
        note = ""
        if self._blocker_store is None:
            note = "no blocker store configured — this strand is reported here only"
        else:
            # Said out loud because it changes what the operator is looking at:
            # the loop's own state (and so the dashboard's in-flight panel)
            # still names this task as the round in progress, and only the
            # round ceiling established that the round is over. Without this
            # line the blocker and the dashboard disagree with no explanation.
            stale = (
                " The loop's state still names it as the round in flight, but "
                "that round is older than the round ceiling, so it is not "
                "running."
                if strand.stale_current
                else ""
            )
            question = (
                f"task {strand.task_id} was left in progress by a round the "
                f"environment destroyed ({strand.fault_code}), and it cannot be "
                f"returned to the queue automatically: {obstacle}.{stale} Nothing "
                "will schedule it until you decide. Read its execution record, then "
                "either continue it (re-dispatch, once the fault is over) or "
                f"`python -m autoloop release {strand.task_id}` — which retires "
                "the worker repo and the execution record, so the task starts "
                "over. Answering this blocker records your decision; it does "
                "NOT move the task."
            )
            detail = (
                f"fault_code={strand.fault_code} "
                f"candidate_sha={strand.candidate_sha[:12] or '(none)'} "
                f"review_round={strand.review_round} "
                f"attempt_count={strand.attempt_count}/{MAX_TASK_ATTEMPTS} "
                f"fault_attempt_count={strand.fault_attempt_count}/"
                f"{MAX_TASK_FAULT_ATTEMPTS} "
                f"stale_current={'yes' if strand.stale_current else 'no'}"
            )
            # TWO try blocks, not one, and the split is deliberate: the LOOKUP
            # is a de-duplication convenience and the RECORD is the report. A
            # blocker directory this cannot read must not therefore go
            # unreported — it degrades to "record anyway, possibly a second
            # time", because a duplicate blocker is a nuisance and a missing one
            # is the invisibility this whole sweep exists to end.
            try:
                existing = next(
                    (
                        blocker
                        for blocker in self._blocker_store.open_blockers()
                        if blocker.task_id == strand.task_id
                        and blocker.code == STRANDED_AFTER_FAULT
                    ),
                    None,
                )
            except (StateError, OSError) as exc:
                existing = None
                note = f"the open blockers could not be read ({exc}) — recording anyway"
            if existing is not None:
                return  # already open and already reported — say nothing more
            try:
                blocker_id = self._blocker_store.record(
                    task_id=strand.task_id,
                    # `task_fatal`: one task is set aside and the loop keeps
                    # working the rest of the roadmap, which is exactly what is
                    # happening. It does not stop continuous mode (only
                    # exhaustion reads the open set), and it does stop `start`
                    # and wake `health`, which is the point.
                    kind="task_fatal",
                    code=STRANDED_AFTER_FAULT,
                    question=question,
                    detail=detail,
                    phase=self.state.phase,
                    now=utcnow_iso(),
                    session_id=self.state.session_id or "",
                ).id
            except (StateError, OSError) as exc:
                note = f"the blocker could not be recorded ({exc})"
        self._log(
            "task_strand_blocked",
            data={
                "task_id": strand.task_id,
                "fault_code": strand.fault_code,
                "obstacle": obstacle,
                "blocker_id": blocker_id,
                "candidate_sha": strand.candidate_sha,
                "review_round": strand.review_round,
                "attempt_count": strand.attempt_count,
                "fault_attempt_count": strand.fault_attempt_count,
                "stale_current": strand.stale_current,
                "note": note,
            },
        )

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
            # Mirrored from the registry onto the record the moment the record
            # exists, so the reviewer and the operator can see that this
            # candidate is the Nth cut of the task where its base sha and
            # attempt budget already are. The AUTHORITATIVE count stays on the
            # `Task` — this record is archived by the very operation that
            # increments it (see `worktask.TaskExecution.recut_count`).
            seeded_recuts = 0 if is_audit else int(task.recut_count or 0)
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
                    recut_count=seeded_recuts,
                )
            else:
                execution = self._worktrees.create(task.id, base_sha)
                execution.allowed_paths = allowed_paths
                execution.validation_commands = declared_validation
                execution.validation_cwd = declared_validation_cwd
                execution.recut_count = seeded_recuts
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

        attempt_cap = self._attempt_cap_for(task)
        if execution.attempt_count >= attempt_cap:
            # Since ceil-01 this is not automatically a park: the reviewer is
            # asked to classify the task against its own candidate first, and
            # only an exhausted or already-asked task ends here for a human. The
            # audit is exempt — it is not a roadmap task, so there is no registry
            # row to record a request on and nothing to decompose it into.
            self._handle_attempt_ceiling(
                directive,
                task,
                execution,
                worktree_git,
                state,
                attempt_cap,
                is_audit=is_audit,
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
            # NO postcommit carry here, and this is the boundary of that
            # mechanism rather than an omission. `_carry_postcommit_forward` is
            # for a CORRECTIVE re-prompt — a message that presents nothing new
            # and asks again about a packet that has not changed. This is the
            # opposite: an executor round RAN and is reporting what happened to
            # it. Carrying a binding across it would let an approval of the
            # report publish the candidate from BEFORE the round the reviewer
            # asked for, which is a different presented state, not a re-prompt
            # about the same one.
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

    # ---- the attempt ceiling: classify, then extend or decompose ------------
    #
    # The sequence, in the order a task meets it:
    #
    #   1. `_attempt_cap_for` says what this task's ceiling actually is —
    #      `MAX_TASK_ATTEMPTS`, plus whatever a reviewer already granted, minus
    #      whatever a parent already spent.
    #   2. `_handle_attempt_ceiling` decides between ASKING and PARKING. It asks
    #      once, records that it asked, and parks if it has already asked or if
    #      no remedy is left.
    #   3. `_ceiling_reply_ok` reads the answer that comes back on the next
    #      `implement`/`revise`: a plan that DIFFERS buys an extension, a plan
    #      identical to the stored one parks, and no plan at all falls through to
    #      the park in step 2.
    #   4. `_dispatch_ceiling_split` reads the other answer — a `plan` — and
    #      applies it: children carrying the parent's spend, parent retired into
    #      them, record archived and worker quarantined under one label.
    #
    # Nothing here refunds an attempt, and nothing here raises
    # `MAX_TASK_ATTEMPTS`. Every route out of a ceiling is bounded by a constant
    # declared at the top of this module, and the last route is still the park
    # this replaced, under the same `attempt_count_ceiling` code.

    @staticmethod
    def _nonneg_int(value: object) -> int:
        """`value` as a non-negative int, or 0.

        Defensive for the same reason `_recut_count_for` is on its side of the
        pair: `tasks._persisted_nonneg_int` refuses an unreadable stored value at
        LOAD, but a `Task` handed in by an embedder or a test goes through no
        such gate, and a `bool` is an `int` in Python. A budget computed from
        `True` would be quietly wrong rather than loudly broken.
        """
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return 0
        return int(value)

    def _attempt_cap_for(self, task: Task) -> int:
        """How many attempts THIS task gets before it reaches its ceiling.

        `MAX_TASK_ATTEMPTS` for every ordinary task, which is the whole of the
        old behaviour. Two adjustments on top, and the ORDER of them is the
        anti-refund rule:

          * a child of a ceiling split first SUBTRACTS the parent's spend, and
            the result is floored at `MIN_CHILD_ATTEMPTS` so the child is not
            born already at its ceiling;
          * a reviewer-granted extension is added AFTERWARDS, so it is worth
            `CEILING_EXTENSION_ATTEMPTS` real attempts to a child too. Adding
            before the floor would have made the grant worth nothing for exactly
            the tasks that had least budget — a guard that reads as granted while
            behaving as if it were not.

        The consequence worth stating: a child's cap is
        `MIN_CHILD_ATTEMPTS + CEILING_EXTENSION_ATTEMPTS` at its most generous,
        which is strictly less than `MAX_TASK_ATTEMPTS`. No subtask ever holds a
        full fresh budget, extended or not.
        """
        inherited = self._nonneg_int(getattr(task, "inherited_attempts", 0))
        extensions = min(
            self._nonneg_int(getattr(task, "attempt_extensions", 0)),
            MAX_CEILING_EXTENSIONS,
        )
        base = MAX_TASK_ATTEMPTS - inherited
        if inherited:
            base = max(base, MIN_CHILD_ATTEMPTS)
        return base + extensions * CEILING_EXTENSION_ATTEMPTS

    def _ceiling_remedies(self, task: Task) -> tuple[bool, bool]:
        """`(may_extend, may_split)` for a task standing at its ceiling.

        Both are read from the DURABLE registry row, never from the execution
        record: a split archives that record, so a bound counted there would
        read zero on the next one.
        """
        extensions = self._nonneg_int(getattr(task, "attempt_extensions", 0))
        depth = self._nonneg_int(getattr(task, "split_depth", 0))
        return extensions < MAX_CEILING_EXTENSIONS, depth < MAX_SPLIT_DEPTH

    def _handle_attempt_ceiling(
        self,
        directive: Directive,
        task: Task,
        execution: TaskExecution,
        worktree_git: GitGateway,
        state: LoopState,
        cap: int,
        *,
        is_audit: bool,
    ) -> None:
        """A task has reached its attempt ceiling: ask the reviewer to classify
        it, or park when asking is not available or is not owed.

        THE ORDER IS THE SAFETY PROPERTY, so it is stated rather than implied:

          * an AUDIT unit parks exactly as it always did — it is synthetic, has
            no registry row to record a request against, and cannot be
            decomposed into roadmap tasks;
          * a task that has ALREADY ASKED parks. This is the ping-pong bound.
            The ceiling check runs before `_open_attempt` and before any policy
            denial, so it spends neither budget; a loop that merely re-asked
            would re-ask forever with every automated signal green, which is the
            exact shape of the stop-livelock `MAX_REPEATED_STOPS` had to bound.
          * a task with NO REMEDY LEFT parks, under the same
            `attempt_count_ceiling` code an operator's tooling already knows;
          * and a failure to RECORD the request parks too. Fail-closed on
            purpose: a loop that asked but cannot remember asking is a loop that
            asks again.

        Everything else asks, and the ask costs no attempt.
        """
        if is_audit or not self._registry.has(task.id):
            self._park_attempt_ceiling(
                task,
                execution,
                cap,
                note=(
                    "The audit is not a roadmap task: there is no registry row to "
                    "record a classification against and nothing to decompose it "
                    "into, so it parks here as it always has."
                    if is_audit
                    else "This id is not in the task registry, so no "
                    "classification can be recorded against it."
                ),
            )
            return
        if task.ceiling_plan_requested_at:
            self._park_ceiling_plan_unanswered(directive, task, execution, cap)
            return
        may_extend, may_split = self._ceiling_remedies(task)
        if not (may_extend or may_split):
            self._park_attempt_ceiling(
                task,
                execution,
                cap,
                note=(
                    f"The reviewer has already classified this task: its attempt "
                    f"budget was extended {self._nonneg_int(task.attempt_extensions)} "
                    f"time(s) (cap {MAX_CEILING_EXTENSIONS}) and it sits at split "
                    f"depth {self._nonneg_int(task.split_depth)} of "
                    f"{MAX_SPLIT_DEPTH}, so neither a further extension nor a "
                    "further decomposition is available. What is left is a "
                    "specification problem, not a budget one."
                ),
            )
            return
        try:
            self._registry.request_ceiling_plan(task.id, utcnow_iso())
            self._task_store.save(self._registry)
        except (TaskGraphError, StateError, OSError) as exc:
            # RECORD FIRST, ask second — and if the record cannot be written,
            # do not ask at all. The marker is the only thing that stops the
            # next dispatch asking again, so asking without it is the ping-pong
            # this park exists to prevent.
            self._park_attempt_ceiling(
                task,
                execution,
                cap,
                note=(
                    "The loop could not record an attempt-ceiling classification "
                    f"request for this task ({exc}), so it did not ask: an "
                    "unrecorded request would be re-asked on every dispatch."
                ),
            )
            return
        # Read BEFORE `last_response` is cleared, or the request this ceiling was
        # reached under could never be paired with the question it produced.
        answered = state.last_response.request_id if state.last_response else None
        state.outbox = self._ceiling_plan_request(
            task, execution, worktree_git, cap, may_extend, may_split
        )
        state.last_response = None
        state.consecutive_failures = 0
        state.phase = Phase.READY.value
        self._log(
            "attempt_ceiling_plan_requested",
            request_id=answered,
            data={
                "task_id": task.id,
                "attempt_count": execution.attempt_count,
                "cap": cap,
                "base_cap": MAX_TASK_ATTEMPTS,
                "review_round": execution.review_round,
                "may_extend": may_extend,
                "may_split": may_split,
                "candidate_sha": execution.candidate_sha,
                "ledger": list(execution.attempt_ledger),
            },
        )
        self._store.save(state)

    def _park_attempt_ceiling(
        self, task: Task, execution: TaskExecution, cap: int, note: str = ""
    ) -> None:
        """The original ceiling park, kept verbatim in substance and under the
        same `attempt_count_ceiling` code, with one sentence added saying WHY
        this particular ceiling hit was not handed to the reviewer.

        The code is deliberately unchanged: `cli`, the blocker store and an
        operator's own greps all key off it, and a task that genuinely churned
        through every remedy has hit exactly the wall that code has always
        named.
        """
        self.state.last_response = None
        self._to_needs_user(
            f"task {task.id}: {execution.attempt_count} commit attempts on "
            f"{execution.task_branch} without reaching an approved review "
            f"(cap {cap}). A structural refusal consumes an "
            "attempt but not a review round, so this ceiling is what stops "
            "unbounded local churn. Nothing was rolled back or pushed."
            + (f"\n\n{note}" if note else ""),
            kind="task_fatal",
            code="attempt_count_ceiling",
            task_id=task.id,
            detail=(
                f"attempt_count={execution.attempt_count} cap={cap} "
                f"base_cap={MAX_TASK_ATTEMPTS} "
                f"extensions={self._nonneg_int(getattr(task, 'attempt_extensions', 0))}/"
                f"{MAX_CEILING_EXTENSIONS} "
                f"split_depth={self._nonneg_int(getattr(task, 'split_depth', 0))}/"
                f"{MAX_SPLIT_DEPTH} "
                f"branch={execution.task_branch} "
                f"ledger={','.join(execution.attempt_ledger)}"
            ),
        )

    def _park_ceiling_plan_unanswered(
        self,
        directive: Directive,
        task: Task,
        execution: TaskExecution,
        cap: int,
    ) -> None:
        """The reviewer was asked to classify this task and the reply did not.

        A PARK, and the one that makes the whole mechanism bounded. Asking costs
        no attempt and no denial budget, so "ask again" has no natural end; a
        second ask would be the loop talking to itself while `needs_attention`
        stayed false — measured elsewhere in this file as three refusals in
        fifteen minutes with every signal green.

        In practice `policy._check_decomposition` refuses a plan-less
        `implement`/`revise` on a waiting task first, with a correction the
        reviewer can act on and a denial budget bounding the correction. Reaching
        here means either that gate was bypassed or the reviewer answered with
        something that is neither a plan nor a decomposition — an operator has
        the whole arc, including the request that went out.
        """
        self.state.last_response = None
        self._to_needs_user(
            f"task {task.id}: it reached its attempt ceiling "
            f"({execution.attempt_count}/{cap}) and asked the reviewer to "
            f"classify it at {task.ceiling_plan_requested_at}, but the reply did "
            "not answer the question: neither a NEW decomposition for this task "
            "(which would have extended its budget for a named remaining fix) "
            "nor a `plan` decomposing it into subtasks. The loop does not ask "
            "twice — a second request costs no attempt and no denial budget, so "
            "it would repeat forever. Nothing was rolled back or pushed; the "
            f"candidate on {execution.task_branch} is untouched.\n\n"
            "Answering this blocker does NOT re-ask the reviewer: the task is "
            "still at its ceiling and still marked as having asked, so the next "
            "dispatch parks here again. While the round is still in progress, "
            f"`python -m autoloop shelve {task.id}` clears the marker and keeps "
            "the record, so the next dispatch asks afresh against the same "
            "candidate and the same spent attempts — or parks "
            "`attempt_count_ceiling` if no remedy is left. (`release` and "
            "`recut` clear it too, but they archive the record, so the task "
            "starts over at attempt 0.) Once continuous mode has quarantined "
            "the task (`blocked`) those verbs refuse it and unblocking does "
            "not clear the marker, so the route from there is to clear this "
            "task's "
            "`ceiling_plan_requested_at` in `tasks.json` with the loop stopped. "
            "Otherwise rewrite, decompose or retire the task — and do "
            "NOT raise MAX_TASK_ATTEMPTS, which is the only bound on local "
            "churn.\n\n"
            f"Last directive: {directive.decision.value} — {directive.reason}",
            kind="task_fatal",
            code="ceiling_plan_unanswered",
            task_id=task.id,
            detail=(
                f"attempt_count={execution.attempt_count} cap={cap} "
                f"requested_at={task.ceiling_plan_requested_at} "
                f"decision={directive.decision.value} "
                f"branch={execution.task_branch}"
            ),
        )

    def _park_ceiling_plan_unchanged(self, directive: Directive, task: Task) -> None:
        """The reviewer answered the classification request with the plan that is
        already on record.

        THE ECHO CASE, and it parks rather than re-prompting because the request
        itself contains the stored plan — the reviewer is shown it so that it can
        differ from it, which is exactly what makes handing it straight back
        indistinguishable from not having classified at all. A planner fixed on
        one axis re-proposes that axis; the task's own specification names this
        as the failure to require against rather than to hope about.

        Compared after `_normalise_feedback` (whitespace and case), the same
        normalisation the repeated-feedback bound uses. Deliberately NOT fuzzy:
        two genuinely different plans must always be allowed through, so only an
        exact match after normalisation is refused.
        """
        self.state.last_response = None
        self._to_needs_user(
            f"task {task.id}: the reviewer answered its attempt-ceiling "
            "classification request with the SAME plan that is already on "
            "record, so nothing about the task has been reclassified — the "
            "budget was not extended and no decomposition was applied. The "
            "stored plan was included in the request precisely so the new one "
            "could differ from it. Nothing was rolled back or pushed.\n\n"
            "Answering this blocker does NOT re-ask the reviewer: the task is "
            "still at its ceiling and still marked as having asked. While the "
            f"round is still in progress, `python -m autoloop shelve {task.id}` "
            "clears the marker and keeps the record, so the next dispatch asks "
            "afresh against the same candidate (`release` and `recut` clear it "
            "too, but they archive the record and the task starts over at "
            "attempt 0). Once continuous mode has quarantined the task "
            "(`blocked`) those verbs refuse it and unblocking does not clear "
            "the marker, so from there "
            "clear this task's `ceiling_plan_requested_at` in `tasks.json` with "
            "the loop stopped — or rewrite, decompose or retire the task.\n\n"
            f"Reviewer's reason: {directive.reason}",
            kind="task_fatal",
            code="ceiling_plan_unchanged",
            task_id=task.id,
            detail=(
                f"requested_at={task.ceiling_plan_requested_at} "
                f"decision={directive.decision.value}"
            ),
        )

    def _ceiling_reply_ok(self, directive: Directive, task: Task) -> bool:
        """Read a task-decision reply to an attempt-ceiling request. True when
        the dispatch may continue.

        Called from `_dispatch_executor` BEFORE `set_decomposition`, which is the
        gate that matters: that method stores any plan an `implement`/`revise`
        carries, so an extension granted beside it without this check would mean
        every ordinary mid-task reshape silently widened the ceiling. The grant
        happens ONLY for a task that actually asked.

        Returns True unchanged for every task that is not waiting, which is all
        of them nearly all of the time.
        """
        if not getattr(task, "ceiling_plan_requested_at", ""):
            return True
        plan = directive.decomposition
        if plan is None:
            # `policy._check_decomposition` refuses this first in the ordinary
            # flow. Falling through leaves the marker set, so the ceiling check
            # below parks the task as unanswered rather than dispatching a round
            # against a budget nobody extended.
            return True
        if self._normalise_feedback(plan.render()) == self._normalise_feedback(
            task.decomposition
        ):
            self._park_ceiling_plan_unchanged(directive, task)
            return False
        may_extend, _ = self._ceiling_remedies(task)
        if not may_extend:
            # Refused, not parked: the request told the reviewer that no
            # extension was left and that a decomposition or `stop` was what
            # remained, so there IS an answer it can act on — which is the
            # standing rule for every refusal the reviewer can correct.
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "ceiling_extension_spent",
                    f"task '{task.id}' has already had its attempt budget "
                    f"extended {self._nonneg_int(task.attempt_extensions)} time(s) "
                    f"(cap {MAX_CEILING_EXTENSIONS}), so a new plan for it cannot "
                    "buy another one. A task that spends a second budget without "
                    "landing has falsified the claim an extension makes. Answer "
                    "the classification request with `plan` — at least "
                    f"{MIN_CEILING_SPLIT_TASKS} subtasks, each independently "
                    "reviewable — or with `stop`. Nothing was executed and no "
                    "attempt was spent.",
                ),
            )
            return False
        try:
            self._registry.grant_attempt_extension(task.id)
        except TaskGraphError as exc:  # pragma: no cover - guarded above
            self._handle_policy_denial(
                directive, Verdict.deny(exc.code, f"{exc}. Nothing was executed.")
            )
            return False
        self._log(
            "attempt_ceiling_extended",
            data={
                "task_id": task.id,
                "extensions": task.attempt_extensions,
                "cap": MAX_CEILING_EXTENSIONS,
                "granted_attempts": CEILING_EXTENSION_ATTEMPTS,
                "attempt_cap": self._attempt_cap_for(task),
                "reason": directive.reason,
            },
        )
        return True

    @staticmethod
    def _bounded_section(text: str, limit: int = CEILING_REQUEST_SECTION_MAX_CHARS) -> str:
        """`text`, cut to `limit` characters, SAYING SO when it cut.

        A silent truncation reads exactly like complete coverage, which is the
        fail-open this whole request is trying to avoid: the reviewer is
        classifying on this evidence, and evidence that quietly stops early
        invites the wrong classification with no signal that anything is
        missing.
        """
        body = (text or "").strip()
        if not body:
            return "(none recorded)"
        if len(body) <= limit:
            return body
        dropped = len(body) - limit
        return f"{body[:limit]}\n… [{dropped} more character(s) withheld to keep this request deliverable]"

    def _ceiling_plan_request(
        self,
        task: Task,
        execution: TaskExecution,
        worktree_git: GitGateway,
        cap: int,
        may_extend: bool,
        may_split: bool,
    ) -> str:
        """What the loop asks the reviewer when a task reaches its ceiling.

        Built here rather than from a `prompts.TEMPLATES` entry for the reason
        `_recut_report` gives: it says things no template shape covers — which
        remedies are still available for THIS task, and what each answer must
        look like.

        **NO RANGE DIFF, deliberately.** The packet cap already bites on exactly
        the tasks that reach a ceiling: port-01's range diff was refused at 414KB
        against `RANGE_DIFF_MAX_BYTES` of 400,000, and blk-01's candidate is
        1,821 insertions across 11 files. What classification needs is not the
        diff — it is whether the reviewer's own objections are SHRINKING or
        RELOCATING, which the verdict history answers and the diff does not. So
        this carries the attempt ledger, the last feedback, the stored plan and
        the touched-file list, each bounded and each saying what it withheld.
        """
        touched: list[str] = []
        touched_note = ""
        if execution.candidate_sha:
            try:
                touched = sorted(
                    worktree_git.commit_range_paths(
                        execution.task_base_sha, execution.candidate_sha
                    )
                )
            except GitError as exc:
                # `GitError`, not just `GitCommandError`: an unreadable repo
                # raises the base class, and losing the whole classification
                # request to it would send this round back through
                # `_handle_git_failure` with the ceiling still unclassified. A
                # missing file list is a stated gap; a missing request is not.
                touched_note = f"(the file list is unavailable: {exc})"
        else:
            touched_note = "(no candidate has been committed on this branch yet)"
        answers = []
        if may_extend:
            answers.append(
                "  A. A NAMED REMAINING FIX — the objections are shrinking and "
                "this candidate is close. Reply `revise` (or `implement`) with "
                f"task_id '{task.id}' AND a `decomposition` that says what is "
                "left. The plan MUST DIFFER from the one on record below; an "
                f"identical one parks. That grants {CEILING_EXTENSION_ATTEMPTS} "
                f"more attempts, once per task ({MAX_CEILING_EXTENSIONS} "
                "granted so far: "
                f"{self._nonneg_int(task.attempt_extensions)})."
            )
        if may_split:
            answers.append(
                "  B. A DECOMPOSITION — the objections keep RELOCATING: each "
                "round finds a deeper hole in the same property, and the task "
                "has become the too-big task this exists to split. Reply `plan` "
                f"with at least {MIN_CEILING_SPLIT_TASKS} subtasks, each with "
                "its own `approved_paths` and each independently reviewable. "
                "This task is then retired into them, its record archived and "
                "its worker quarantined — nothing is deleted. Each subtask "
                f"INHERITS the {execution.attempt_count} attempt(s) already "
                "spent here, so a split never refunds a budget."
            )
        answers.append(
            "  C. `stop` — a human should decide. Use this when neither of the "
            "above is honest; it is always available."
        )
        unavailable = []
        if not may_extend:
            unavailable.append(
                f"an extension (already granted {self._nonneg_int(task.attempt_extensions)} "
                f"of {MAX_CEILING_EXTENSIONS})"
            )
        if not may_split:
            unavailable.append(
                f"a decomposition (this task is at split depth "
                f"{self._nonneg_int(task.split_depth)} of {MAX_SPLIT_DEPTH})"
            )
        return (
            f"ATTEMPT CEILING REACHED — task {task.id} needs a classification, "
            "not a park.\n\n"
            f"{task.title}\n\n"
            f"It has spent {execution.attempt_count} of {cap} attempts "
            f"(base cap {MAX_TASK_ATTEMPTS}"
            + (
                f", minus {self._nonneg_int(task.inherited_attempts)} inherited "
                "from the parent it was split from"
                if self._nonneg_int(task.inherited_attempts)
                else ""
            )
            + (
                f", plus {self._nonneg_int(task.attempt_extensions) * CEILING_EXTENSION_ATTEMPTS} "
                "already granted"
                if self._nonneg_int(task.attempt_extensions)
                else ""
            )
            + f") across {execution.review_round} review round(s), with "
            f"{execution.fault_attempt_count} round(s) charged to the separate "
            "fault budget. An attempt is spent by a structural refusal or a "
            "failed validation as well as by a round you judged, so this ceiling "
            "is what bounds local churn — it is NOT being raised.\n\n"
            f"Branch {execution.task_branch}, base "
            f"{execution.task_base_sha[:12]}, candidate "
            f"{execution.candidate_sha[:12] or '(none committed)'}.\n\n"
            "--- attempt ledger (one entry per dispatch: ordinal|budget|outcome) ---\n"
            + self._bounded_section(
                "\n".join(execution.attempt_ledger) or "(none recorded)"
            )
            + "\n\n--- files this candidate touches ---\n"
            + (touched_note or self._bounded_section("\n".join(touched)))
            + "\n\n--- your most recent revise feedback (normalised) ---\n"
            + self._bounded_section(execution.last_revise_feedback)
            + "\n\n--- the plan currently on record for this task ---\n"
            + self._bounded_section(task.decomposition)
            + "\n\n--- what the executor last reported ---\n"
            + self._bounded_section(execution.report_summary)
            + "\n\nCLASSIFY IT. Read your own verdict history: objections that "
            "are SHRINKING (one named fix left, the rest endorsed) and "
            "objections that keep RELOCATING (every round a deeper hole in the "
            "same property) have opposite remedies, and the counters above "
            "cannot tell them apart. Answer with exactly one of:\n\n"
            + "\n\n".join(answers)
            + (
                "\n\nNot available for this task: " + "; ".join(unavailable) + "."
                if unavailable
                else ""
            )
            + "\n\nThis is asked ONCE. A reply that is none of the above parks "
            "the task for a human. Nothing has been executed, nothing was "
            "rolled back, and no attempt was spent on this request."
        )

    # ---- the decomposition half: a `plan` that answers the request ----------

    def _ceiling_split_parent(self, state: LoopState) -> Task | None:
        """The task a `plan` is answering the ceiling request FOR, or None.

        None is the ordinary answer — nearly every `plan` is roadmap work and
        must keep going through `_dispatch_plan` untouched.

        Attribution is deliberately conservative, because a split applied to the
        wrong parent retires a task nobody asked to retire: the task named by
        `state.current_task` wins (it is the dispatch that hit the ceiling), a
        single waiting task is unambiguous, and anything else — two tasks waiting
        from different sessions with nothing naming either — is treated as an
        ORDINARY plan and logged. Those parents then park as unanswered on their
        next dispatch, which is the pre-existing behaviour rather than a new
        failure.
        """
        pending = self._registry.ceiling_plan_pending()
        if not pending:
            return None
        current = state.current_task
        current_id = (
            str(current.get("task_id") or "") if isinstance(current, dict) else ""
        )
        for task in pending:
            if task.id == current_id:
                return task
        if len(pending) == 1:
            return pending[0]
        self._log(
            "ceiling_split_parent_ambiguous",
            data={"pending": sorted(t.id for t in pending), "current": current_id},
        )
        return None

    def _dispatch_ceiling_split(self, directive: Directive, parent: Task) -> None:
        """Apply a `plan` that answers `parent`'s attempt-ceiling request.

        EVERY refusal happens before anything moves, and the writes that do move
        are ordered so the residue a crash leaves is the loudest one available:
        the children are added and the parent retired into them by
        `release_task_to_pending`, which persists the registry ONCE (children +
        retirement in the same file write) before it touches the execution
        record or the worker repo on disk.

        **ORDERING IS NOT ENOUGH, AND THIS IS WHERE THE DURABLE MARKER COMES
        IN.** Acceptance spans three stores — the registry, the execution record
        and the worker repository — and there is no point at which all three
        commit together. So a process that dies after the registry save and
        before `retire_execution` leaves `tasks.json` saying the parent is
        retired while its record and worker are still live: not stale, but
        CONTRADICTORY, and silent, since the record holds the repository-wide
        merge window shut (`cli._merge_window_blockers`) and the parent will
        never be dispatched again for anything to notice.

        A `SplitIntent` is therefore written durably immediately below — after
        the last refusal, before the first mutation — and cleared only once all
        three agree. `Orchestrator.run` reconciles against it at startup
        (`_reconcile_split_acceptance`), which FINISHES a registry write that
        landed and DISCARDS a marker for one that did not. The registry, never
        the marker, decides which of those it is.

        There is no bespoke split mechanism here on purpose. `add_many` is
        atomic, `retire(superseded_by=...)` is the registry's own supersession
        (and re-points the parent's dependents at the successors), and
        `release_task_to_pending` is the same record-and-worker retirement a
        `recut` and an operator `release` already use.
        """
        state = self.state
        specs = directive.tasks or ()
        if self._execution_store is None or self._worker_repos is None:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "ceiling_split_unavailable",
                    "this loop has no execution store and worker-repository "
                    "manager configured, so it cannot retire the parent's record "
                    "and worker — and adding subtasks while leaving the parent's "
                    "candidate live is worse than doing neither. Nothing was "
                    "changed.",
                ),
            )
            return
        may_extend, may_split = self._ceiling_remedies(parent)
        if not may_split:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "ceiling_split_depth",
                    f"task '{parent.id}' is already a subtask at split depth "
                    f"{self._nonneg_int(parent.split_depth)} (cap "
                    f"{MAX_SPLIT_DEPTH}), so it cannot be decomposed again — "
                    "splitting without a bound is how one looping task becomes "
                    "an unbounded family of them. "
                    + (
                        "Answer with a differing `decomposition` for it instead, "
                        "or with `stop`."
                        if may_extend
                        else "Answer with `stop`."
                    )
                    + " Nothing was changed.",
                ),
            )
            return
        if len(specs) < MIN_CEILING_SPLIT_TASKS:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "ceiling_split_too_small",
                    f"a decomposition of '{parent.id}' must name at least "
                    f"{MIN_CEILING_SPLIT_TASKS} subtasks; this plan names "
                    f"{len(specs)}. One subtask inherits the parent's spend and "
                    "then hands the SAME unit of work a fresh floor of attempts "
                    "under a new id, which is a rename that buys budget. If the "
                    "work really is one unit, that is answer A — `revise` with a "
                    "`decomposition` that differs from the one on record. "
                    "Nothing was changed.",
                ),
            )
            return
        named_parent = [s.id for s in specs if s.id == parent.id]
        if named_parent:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "ceiling_split_names_parent",
                    f"this plan names '{parent.id}' as one of its own subtasks. "
                    "The parent is retired into its children, so a child under "
                    "the same id cannot be created and the split would be "
                    "refused halfway through. Give each subtask a new id. "
                    "Nothing was changed.",
                ),
            )
            return
        blocking = self._in_progress_dependents(parent.id)
        if blocking:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "ceiling_split_dependent_in_progress",
                    f"task(s) {', '.join(blocking)} depend on '{parent.id}' and "
                    "are in progress, so retiring it would rewrite the "
                    "dependencies a running dispatch is being judged against. "
                    "Let them finish first. Nothing was changed.",
                ),
            )
            return
        try:
            execution = self._execution_store.load(parent.id)
        except (StateError, OSError) as exc:
            # Unreadable, NOT absent — the same fail-closed reading `recut` takes:
            # a record this cannot parse may name a published candidate, and the
            # spend it carries is what every child's budget is derived from.
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "ceiling_split_record_unreadable",
                    f"task '{parent.id}' has an execution record this loop cannot "
                    f"read ({exc}), so neither its published state nor the "
                    "attempts its children must inherit can be established. An "
                    "operator has to look at it. Nothing was changed.",
                ),
            )
            return
        if execution is None:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "ceiling_split_no_execution",
                    f"task '{parent.id}' has no execution record, so there is no "
                    "spend for its children to inherit and nothing to retire. "
                    "Nothing was changed.",
                ),
            )
            return
        if execution.published_sha:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "ceiling_split_candidate_published",
                    f"task '{parent.id}' has ALREADY PUBLISHED candidate "
                    f"{execution.published_sha[:12]} — published work is never "
                    "retired by this loop. If it is wrong, that is a new task. "
                    "Nothing was changed.",
                ),
            )
            return

        inherited = self._nonneg_int(execution.attempt_count)
        child_depth = self._nonneg_int(parent.split_depth) + 1
        children = [
            Task(
                id=s.id,
                title=s.title,
                description=s.description,
                depends_on=s.depends_on,
                approved_paths=s.approved_paths,
                inherited_attempts=inherited,
                split_depth=child_depth,
            )
            for s in specs
        ]
        child_ids = tuple(child.id for child in children)
        # THE DURABLE MARKER, and it goes HERE — after the last refusal above
        # (every one of which says "Nothing was changed", and would be made a
        # liar by a marker claiming a split was in flight) and before the first
        # mutation below. From this line until the marker is cleared, a crash at
        # any point is reconcilable from disk; see `_reconcile_split_acceptance`
        # and the section at the bottom of `worktask.py`.
        #
        # A marker that cannot be WRITTEN refuses the split outright. Continuing
        # without it is the fail-open reading: the split would then be exactly as
        # crash-unsafe as it was before this existed, with nothing saying so.
        try:
            self._split_intents.save(
                SplitIntent(
                    parent_id=parent.id,
                    child_ids=child_ids,
                    reason=CEILING_SPLIT_RETIREMENT_REASON,
                )
            )
        except OSError as exc:
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    "ceiling_split_intent_unwritable",
                    f"the durable marker recording this decomposition of "
                    f"'{parent.id}' could not be written ({exc}), and without it a "
                    "crash partway through would leave the registry describing a "
                    "task whose execution record and worker repository still "
                    "exist, with nothing able to find it afterwards. Nothing was "
                    "changed.",
                ),
            )
            return
        try:
            self._registry.add_many(children)
        except TaskGraphError as exc:
            # `add_many` is atomic, so the registry is exactly as it was and the
            # parent is still waiting: this is a denial, not a park.
            self._clear_split_intent(parent.id)
            self._log("plan_rejected", data={"code": exc.code, "error": str(exc)})
            self._handle_policy_denial(
                directive,
                Verdict.deny(
                    exc.code,
                    f"{exc}. The decomposition of '{parent.id}' was not applied "
                    "and nothing was changed.",
                ),
            )
            return
        self._registry.clear_ceiling_plan_request(parent.id)
        try:
            release = release_task_to_pending(
                parent.id,
                self._registry,
                self._execution_store,
                self._worker_repos,
                persist=lambda: self._task_store.save(self._registry),
                reason=CEILING_SPLIT_RETIREMENT_REASON,
                tolerate_retirement_failure=True,
                move=lambda tid: self._registry.retire(
                    tid,
                    superseded_by=child_ids,
                    reason=(
                        "decomposed at the attempt ceiling into "
                        + ", ".join(child_ids)
                    ),
                ),
            )
        except TaskGraphError as exc:
            # The children are in the registry and the parent is not retired.
            # Persist that — it is what is true — and park loudly rather than
            # leaving a half-applied split only this process knows about.
            self._task_store.save(self._registry)
            # The marker is SPENT, and dropping it here is not optional. This
            # state is half-applied but it is not contradictory: the parent is
            # still live and still owns its record and its worker, which is
            # precisely what the park below tells the operator. A marker left
            # standing would have the next start's reconciliation read a live
            # parent, answer UNAPPLIED and drop it anyway — so this is the same
            # decision, taken where the fact is already known.
            self._clear_split_intent(parent.id)
            state.last_response = None
            self._to_needs_user(
                f"task {parent.id}: its decomposition into "
                f"{', '.join(child_ids)} was half-applied — the subtasks now "
                f"exist, but the parent could not be retired into them ({exc}). "
                "Its candidate, worker repository and execution record are all "
                "untouched. Retire it by hand once the obstacle is cleared, or "
                "remove the subtasks.",
                kind="task_fatal",
                code="ceiling_split_parent_not_retired",
                task_id=parent.id,
                detail=f"children={','.join(child_ids)} error={exc.code}: {exc}",
            )
            return

        retirement = release.retirement
        self._log(
            "task_ceiling_split",
            request_id=state.last_response.request_id if state.last_response else None,
            data={
                "task_id": parent.id,
                "reason": directive.reason,
                "wanted_decision": directive.wanted_decision,
                "children": list(child_ids),
                "inherited_attempts": inherited,
                "child_split_depth": child_depth,
                "child_attempt_cap": self._attempt_cap_for(children[0]),
                "parent_attempt_count": execution.attempt_count,
                "discarded_candidate": execution.candidate_sha,
                "label": retirement.label if retirement is not None else "",
                "archived_record": (
                    str(retirement.record_path)
                    if retirement is not None and retirement.record_path is not None
                    else ""
                ),
                "quarantined_worker": (
                    str(retirement.worker_path)
                    if retirement is not None and retirement.worker_path is not None
                    else ""
                ),
                "artifacts_retired": release.artifacts_retired,
                "obstacle": release.obstacle,
            },
        )
        if release.artifacts_retired:
            # All three stores agree, so the marker has nothing left to say.
            # KEPT when they do not — the branch at the bottom of this method —
            # because that residue is exactly what the next start's
            # reconciliation is for: the parent IS retired in the registry, so a
            # record still sitting in `executions/*.json` is holding the
            # repository-wide merge window shut on work nobody will ever
            # dispatch again.
            self._clear_split_intent(parent.id)
        # The parent's bookkeeping is gone; so must every pointer this session
        # still holds to it — the same cleanup a recut does, and for the same
        # reason: a later approval naming the retired packet would otherwise
        # resolve a binding to work that no longer has a record.
        self._forget_sent_postcommits_for_task(state, parent.id)
        if isinstance(state.task_execution, dict) and (
            state.task_execution.get("task_id") == parent.id
        ):
            state.task_execution = None
        if isinstance(state.current_task, dict) and (
            state.current_task.get("task_id") == parent.id
        ):
            state.current_task = None
        carried = state.carry_postcommit
        if isinstance(carried, dict) and carried.get("task_id") == parent.id:
            state.carry_postcommit = None

        if not release.artifacts_retired:
            state.last_response = None
            self._to_needs_user(
                f"task {parent.id}: it was decomposed into "
                f"{', '.join(child_ids)} and retired, but its artefacts could "
                f"not be retired — {release.obstacle}. "
                + (
                    f"The worker repository is still at {release.stale_worker_path}. "
                    if release.stale_worker_path
                    else ""
                )
                + (
                    "Its execution record is still live, so the merge window "
                    "stays shut on it. "
                    if release.stale_execution_record
                    else ""
                )
                + "Move them aside by hand. The subtasks are in the roadmap and "
                "can be dispatched once that is done. The split-acceptance "
                "marker is deliberately KEPT, so the next start retries the "
                "retirement by itself once the obstacle is gone.",
                kind="task_fatal",
                code="ceiling_split_retirement_failed",
                task_id=parent.id,
                detail=(
                    f"children={','.join(child_ids)} "
                    f"obstacle={release.obstacle} "
                    f"stale_worker={release.stale_worker_path} "
                    f"stale_record={release.stale_execution_record}"
                ),
            )
            return

        state.outbox = self._ceiling_split_report(
            directive,
            parent,
            execution,
            child_ids,
            inherited,
            # Read off a real child through the SAME helper the dispatch will
            # judge it by, never re-derived here: a report that computed the
            # budget a second way could tell the reviewer a number the loop does
            # not enforce.
            self._attempt_cap_for(children[0]),
            release,
        )
        state.last_response = None
        state.consecutive_failures = 0
        state.phase = Phase.READY.value
        self._store.save(state)

    def _in_progress_dependents(self, task_id: str) -> list[str]:
        """Ids of DIRECT dependents of `task_id` that are in progress.

        The one obstacle `TaskRegistry.retire` refuses that this dispatch can
        check cheaply beforehand — and it must, because by the time `retire`
        raises, the children have already been added. Everything else `retire`
        refuses (completed, retired, shipped-elsewhere) cannot describe a task
        that just hit its attempt ceiling.
        """
        return sorted(
            task.id
            for task in self._registry.all_tasks()
            if task_id in task.depends_on and task.status == "in_progress"
        )

    def _ceiling_split_report(
        self, directive, parent, execution, child_ids, inherited, child_cap, release
    ) -> str:
        """What the loop tells the reviewer after a ceiling split landed.

        Says the two numbers a reviewer cannot spend sensibly without: what each
        child inherited, and what that leaves it. A report that showed only the
        new ids would read as "you got a fresh start", which is the one thing
        this must not imply.
        """
        retirement = release.retirement
        return (
            f"DECOMPOSITION APPLIED — task {parent.id} is retired into "
            f"{', '.join(child_ids)}.\n\n"
            f"Your reason: {directive.reason}\n\n"
            f"Its candidate {execution.candidate_sha[:12] or '(none committed)'} "
            f"on {execution.task_branch} was NOT deleted: the execution record "
            "was archived to "
            f"{retirement.record_path if retirement and retirement.record_path else '(no record on disk)'} "
            "and the worker repository quarantined at "
            f"{retirement.worker_path if retirement and retirement.worker_path else '(no worker on disk)'}"
            f", both under the label {retirement.label if retirement else '(none)'}.\n\n"
            f"THE BUDGET IS NOT REFUNDED. Each subtask inherits the {inherited} "
            f"attempt(s) already spent, so each starts with {child_cap} of the "
            f"usual {MAX_TASK_ATTEMPTS} — a decomposition is a re-scoping, not a "
            "fresh allowance. Each may still be extended once at its own "
            f"ceiling, and none of them may be decomposed again (split depth cap "
            f"{MAX_SPLIT_DEPTH}).\n\n"
            "Each subtask needs its own `decomposition` on the `implement` that "
            "starts it, and cannot be dispatched at all without `approved_paths`."
        )

    # ---- split acceptance: the startup half ---------------------------------

    def _clear_split_intent(self, parent_id: str) -> None:
        """Drop `parent_id`'s split-acceptance marker. Never raises.

        The marker is bookkeeping about work that is already decided, so a
        state directory that refuses the unlink must not take down the dispatch
        that just succeeded — the worst case is one stale marker, and the next
        start's reconciliation reads the REGISTRY rather than the marker, so a
        stale one is answered correctly (UNAPPLIED or an idempotent COMPLETED)
        and dropped then.
        """
        try:
            self._split_intents.clear(parent_id)
        except OSError as exc:  # pragma: no cover - unwritable state dir
            self._log(
                "split_acceptance_marker_not_cleared",
                data={"task_id": parent_id, "error": str(exc)},
            )

    def _reconcile_split_acceptance(self) -> None:
        """Finish, or discard, every split acceptance a crash left in flight.

        THE invariant this exists for: after `_dispatch_ceiling_split` has
        started, the task registry, the parent's execution record and the
        parent's worker repository are either all in agreement, or a durable
        marker names the parent and the next start settles it. There is no third
        state, and before split-04 there was: a process that died between the
        registry save and `retire_execution` left `tasks.json` saying the parent
        was retired while `executions/<parent>.json` still described live
        unpublished work — which shuts the repository-wide merge window
        (`cli._merge_window_blockers`) for every other task, with nothing
        reporting it and no dispatch of the retired parent ever coming to
        notice.

        **Run from `run()`, before the first step, and that is startup rather
        than a per-round sweep.** A crash mid-split leaves the phase wherever the
        dying round had it, so waiting for `_step_ready` would delay this behind
        an arbitrary amount of other work — and the merge window is read by
        other commands in the meantime. It is cheap enough to sit there: one
        `is_dir()` when nothing is in flight, which is every start but the ones
        that need it.

        **The registry decides, never the marker** (`worktask.
        reconcile_split_acceptance`). A marker only says a split was ATTEMPTED;
        whether it happened is read from the parent's row, every time. So a
        registry write that never landed discards the marker and touches no
        artefact, one that landed finishes the artefact half idempotently, and
        anything that cannot be shown to be either touches nothing and reports.

        Nothing here raises. This runs before the loop's own error handling is
        in play, and a recovery that took the process down would be worse than
        the state it recovers from.
        """
        try:
            pending = self._split_intents.pending()
        except OSError as exc:  # pragma: no cover - unreadable state dir
            self._log("split_acceptance_scan_failed", data={"error": str(exc)})
            return
        for parent_id in pending:
            try:
                intent = self._split_intents.load(parent_id)
            except (StateError, OSError) as exc:
                # Reported, never skipped, and the marker is KEPT. A corrupt
                # marker read as absent is the alarm switching itself off: the
                # contradictory state it points at would stay, with the last
                # thing that knew about it now discarded.
                self._report_split_blocker(
                    parent_id,
                    "its split-acceptance marker is on disk but unreadable "
                    f"({exc}), so what was half-applied cannot be established",
                )
                continue
            if intent is None:  # pragma: no cover - removed between the two calls
                continue
            try:
                result = reconcile_split_acceptance(
                    intent, self._registry, self._execution_store, self._worker_repos
                )
            except (StateError, GitError, TaskGraphError, OSError) as exc:
                # `reconcile_split_acceptance` catches the retirement's own
                # failures and reports them as FAILED; this covers a registry or
                # store that misbehaves in some way it does not model. Same
                # ending either way — keep the marker, name the task.
                self._report_split_blocker(
                    parent_id,
                    f"its split acceptance could not be reconciled ({exc})",
                )
                continue
            if not result.intent_is_spent:
                self._report_split_blocker(parent_id, result.detail)
                continue
            self._clear_split_intent(parent_id)
            retirement = result.retirement
            self._log(
                "split_acceptance_reconciled",
                data={
                    "task_id": parent_id,
                    "outcome": result.outcome.value,
                    "children": list(intent.child_ids),
                    "detail": result.detail,
                    "label": retirement.label if retirement is not None else "",
                    "archived_record": (
                        str(retirement.record_path)
                        if retirement is not None
                        and retirement.record_path is not None
                        else ""
                    ),
                    "quarantined_worker": (
                        str(retirement.worker_path)
                        if retirement is not None
                        and retirement.worker_path is not None
                        else ""
                    ),
                },
            )

    def _report_split_blocker(self, parent_id: str, obstacle: str) -> None:
        """File (or retain) an OPEN blocker naming a split acceptance this
        start could not settle, and say so in the transcript.

        The other half of the invariant, and the same shape
        `_report_strand_blocker` uses for the same reason: what is being
        reported is INVISIBLE otherwise. A retired parent is never dispatched,
        so nothing else will ever look at it again — and if its execution record
        survived, the cost is a merge window that stays shut for every other
        task in the repository.

        **Retains the BLOCKER rather than re-recording it, and logs anyway.** An
        open blocker for the same `(task_id, code)` is left exactly as it is
        (`BlockerStore.record` bumps `recurrences`, which means "this condition
        re-parked", not "a start looked at it again") — but the transcript entry
        is written every start regardless. See the comment at that call for why
        the early return `_report_strand_blocker` uses would be a fail-open
        here: the parent is retired, and a retired task's open blockers are
        resolved automatically.

        **Never changes the task's status, and never parks the loop.** The task
        is already retired; the loop is working, on other tasks. Reporting is
        the whole action.

        Best-effort on the store, loud in the transcript — a blocker directory
        that cannot be read or written must not take down a start, so the
        transcript entry is written either way and carries what went wrong.
        """
        blocker_id = ""
        note = ""
        if self._blocker_store is None:
            note = "no blocker store configured — this is reported here only"
        else:
            question = (
                f"task {parent_id} was decomposed at its attempt ceiling and the "
                f"acceptance could not be finished automatically: {obstacle}. Its "
                "subtasks are in the roadmap and can be dispatched. What is left "
                f"is the parent's own leftovers — look for "
                f"{self._execution_store.path_for(parent_id) if self._execution_store is not None else 'its execution record'} "
                "and its worker repository. A surviving execution record holds the "
                "merge window shut for EVERY task, so it is the half worth moving "
                "first. Answering this blocker records your decision; it does NOT "
                "move anything."
            )
            detail = (
                f"obstacle={obstacle} "
                f"marker={self._split_intents.path_for(parent_id)}"
            )
            # TWO try blocks, not one, and the split is deliberate: the LOOKUP is
            # a de-duplication convenience and the RECORD is the report. A
            # blocker directory this cannot read must not therefore go
            # unreported — a duplicate blocker is a nuisance, a missing one is
            # the invisibility this whole sweep exists to end.
            try:
                existing = next(
                    (
                        blocker
                        for blocker in self._blocker_store.open_blockers()
                        if blocker.task_id == parent_id
                        and blocker.code == SPLIT_ACCEPTANCE_UNRECONCILED
                    ),
                    None,
                )
            except (StateError, OSError) as exc:
                existing = None
                note = f"the open blockers could not be read ({exc}) — recording anyway"
            if existing is not None:
                blocker_id = existing.id
                note = "an open blocker for this already exists"
            else:
                try:
                    blocker_id = self._blocker_store.record(
                        task_id=parent_id,
                        kind="task_fatal",
                        code=SPLIT_ACCEPTANCE_UNRECONCILED,
                        question=question,
                        detail=detail,
                        phase=self.state.phase,
                        now=utcnow_iso(),
                        session_id=self.state.session_id or "",
                    ).id
                except (StateError, OSError) as exc:
                    note = f"the blocker could not be recorded ({exc})"
        # WRITTEN EVERY START, deliberately unlike `_report_strand_blocker`,
        # which returns early once its blocker is open. Two reasons that one
        # does not transfer. This runs once per process START, not once per
        # round, so the recurrence-noise argument does not apply. And the
        # blocker here may not survive: `cli._reconcile_retired_blockers`
        # resolves open blockers naming a RETIRED task, and the parent is
        # retired in every shape that reaches this method — so a report living
        # only in the blocker store could be closed out from under the
        # condition it describes, leaving the marker in place with nothing
        # saying so. The transcript is the durable half nothing else closes.
        self._log(
            "split_acceptance_unreconciled",
            data={
                "task_id": parent_id,
                "obstacle": obstacle,
                "blocker_id": blocker_id,
                "note": note,
            },
        )

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

    def _dispatch_task_push(
        self,
        directive: Directive,
        resp: LastResponse,
        binding: PostcommitBinding | None = None,
    ) -> None:
        """Publish a produce-then-review candidate via `push_exact`.

        `directive` is used ONLY for logging/reason text — never for identity.
        A `push` directive cannot carry a task_id at all (`contract._forbid`
        rejects it at parse time), so the BINDING — captured the moment the
        packet was sent, not a fresh `TaskExecutionStore` lookup — is the only
        source of which task and which candidate sha this approval concerns.
        That is what makes swapping the candidate underneath an approval a
        REFUSAL rather than a silent wrong-commit publish: if
        `TaskExecutionStore` now disagrees with the binding, or the recorded
        candidate no longer resolves, or its tree no longer matches what was
        reviewed, nothing is pushed.

        `binding` defaults to `resp.postcommit`, which is where it comes from
        for every ordinary round. `_dispatch` passes it explicitly so an
        approval that NAMES a postcommit packet this loop sent resolves to that
        packet's binding instead (`_approval_packet`) — a different way of
        FINDING the binding, never a different set of checks: everything below
        this line reads `binding` and nothing else, exactly as it always did.

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
        if binding is None:
            binding = resp.postcommit
        if binding is None:  # pragma: no cover - `_dispatch` never routes here unbound
            raise StateError(
                "postcommit push dispatched with no binding — the caller must "
                "resolve one (see `_push_binding`) or refuse the push"
            )
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
        # And forget the packets that presented this candidate. Publication does
        # not advance `execution.candidate_sha`, so a SECOND `push` naming the
        # same already-answered packet would otherwise resolve the same binding,
        # pass every check above, and re-run this completion path for work that
        # has already shipped. Dropping the entries makes that a refusal.
        self._forget_sent_postcommits_for_task(state, binding.task_id)
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
        # A parse error is a FORMATTING failure, not a new review: the packet
        # under review has not changed and the candidate has not moved, so the
        # correction inherits the binding of the request it is correcting.
        # Recorded before `last_response` is cleared, and only on this path —
        # the budget-exhaustion return above parks, and a carry left behind for
        # a request that will never be built is how a binding attaches to
        # something unrelated.
        self._carry_postcommit_forward()
        state.outbox = parse_error_payload(exc.code, str(exc))
        state.last_response = None
        state.phase = Phase.READY.value
        self._store.save(state)

    def _handle_policy_denial(
        self,
        directive: Directive,
        verdict,
        binding: PostcommitBinding | None = None,
    ) -> None:
        """`binding` is the binding the caller already resolved for this
        response, forwarded to `_carry_postcommit_forward` — see its docstring.
        Only the `push` sites in `_step_executing` have one to pass; every other
        caller (a refused executor decision, a retired decision, the legacy git
        path) resolves none by construction and leaves it `None`, which is
        byte-for-byte the behaviour they had."""
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
        # Same reasoning as `_handle_parse_error`'s carry, and deliberately not
        # a different rule: a denial asks for a different decision about the
        # SAME presented state, and dropping the binding here would leave an
        # approval that answers the correction unpublishable in exactly the way
        # a parse error used to.
        self._carry_postcommit_forward(binding)
        state.outbox = policy_denied_payload(directive.decision.value, verdict.reason)
        state.last_response = None
        state.phase = Phase.READY.value
        self._store.save(state)

    def _handle_review_mismatch(
        self, exc: ContractError, binding: PostcommitBinding | None = None
    ) -> None:
        """`binding` is the binding the caller already resolved for this
        response, forwarded to `_carry_postcommit_forward` — see its docstring.
        It matters most HERE: `review_mismatch_payload` asks the reviewer to
        stamp THIS request, so an unbound correction guarantees the re-stamped
        approval resolves nothing."""
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
        # The carry matters MOST here: this correction exists because an
        # approval arrived with the wrong stamps, and its whole purpose is to
        # get a correctly stamped one back. Dropping the binding would make the
        # re-stamped approval unpublishable — the loop refusing the very reply
        # it asked for.
        self._carry_postcommit_forward(binding)
        state.outbox = review_mismatch_payload(exc.code, str(exc))
        state.last_response = None
        state.phase = Phase.READY.value
        self._store.save(state)

    # ---- transport-aware fault routing --------------------------------------

    def _transport_is_browser_backed(self) -> bool:
        """Would restarting the browser be a recovery for THIS run's faults?

        Asked of the ACTIVE provider (`active_provider()`, so a run that has
        failed over to the browser answers for the browser), never of the client
        object. The client is the wrong witness twice over: the failure handlers
        below run after `_drop_client`, and a transport whose factory raised
        never produced an object at all — both of which read as "no client" and
        would have to fall back to a default. See `conversation._BROWSER_BACKED`
        for why the default is "not a browser".
        """
        return transport_is_browser_backed(self.active_provider())

    def _route_transport_fault(self, phase: Phase, exc: BrowserError, browser_handler) -> None:
        """Send one transport fault to the recovery its TRANSPORT has.

        Every transport fault arrives here as a `BrowserError` subclass — the
        hierarchy is named after the first implementation, not after the
        subsystem each fault came from — so the exception type cannot answer
        "may I restart Chrome". This is the single place that asks, and the
        three `except` clauses in `run` keep their existing order and their
        existing browser handlers: for the browser provider this is a
        pass-through and nothing about restart, cooldown or the fault budget
        changes.

        For anything else `_handle_transport_failure` takes it instead. That is
        the 2026-08-22 incident: a `ResponseTimeoutError` raised by a
        SUBPROCESS reached `_handle_response_start_timeout` ->
        `_handle_browser_failure`, which launched Chrome on the browser profile
        (pid 29055, `--remote-debugging-port=9222`), spent the browser fault
        budget on it, and parked the loop advising a browser restart. The run
        had no browser in its design at all.
        """
        if self._transport_is_browser_backed():
            browser_handler(phase, exc)
            return
        self._handle_transport_failure(phase, exc)

    def _replay_unrecoverable_await(self, phase: Phase, exc: BrowserError) -> bool:
        """Re-enter `submitting` when the reply this request is waiting for
        cannot exist any more AND the transport says re-running is safe. True
        when that happened and the caller must stop.

        THE FAULT THIS UNDOES IS A PHASE THAT CANNOT BE SATISFIED.
        `codex.conversation.CodexConversation` captures its reply in an
        in-memory dict, because a CLI turn is synchronous — "the waiting already
        happened ... there is nothing to poll for". A request submitted before a
        process restart is therefore unrecoverable afterwards and
        `await_response` correctly says so. But the PERSISTED phase says
        `awaiting`, which assumes the reply is somewhere the process can go and
        re-read: true for the browser, where it sits in the chat thread; false
        for a subprocess whose stdout is gone. Left alone the loop waits, fails,
        retries and eventually parks over a reply that can never appear.

        Persisting the reply would be the wrong repair: the dict is a handoff
        between two calls inside one round, not a durable artifact, and storing
        it would keep a value whose only source has exited while leaving the
        unsatisfiable phase reachable. Re-running is the recovery this transport
        already promises.

        Three gates, all required, none of them inferred:

        * **`phase is AWAITING`** — the only phase in which a missing reply is
          the fault. `SubprocessCodexRunner.run` raises the same
          `ResponseTimeoutError` type from `submit` when the CLI outruns
          `codex.timeout_seconds`; that one happens in `submitting`, where the
          existing send machinery already owns the decision, and reading it as a
          replay signal would re-invoke on top of a possibly-live process.
        * **`idempotent_submit`** — THE licence, and only it. A transport that
          does not declare it is not re-run automatically, because that is
          exactly how a duplicate turn gets posted into a shared thread. Probed
          on the client that is still held, never on one built here: a
          `_get_client()` inside a failure handler can raise and leave `run`'s
          `except` with no park at all, so an absent client answers "no replay".
        * **`reconcile` confirms absence** — the transport's own authority on
          whether a reply exists. Presence outranks everything; a reply that
          turns out to be there is an ordinary fault, not a replay.

        Charged to `MAX_AWAIT_REPLAYS` and to nothing else. A replay that
        actually ran is a recovery that was PERFORMED, and the same rule
        `_handle_browser_failure` already applies to a restart that ran holds
        here: it is not evidence recovery fails. The ordinary failure budget
        still bites, because a transport that cannot answer fails again on the
        submit side and is charged there.
        """
        if phase is not Phase.AWAITING:
            return False
        state = self.state
        req = state.pending_request
        if req is None:  # pragma: no cover - defensive; awaiting always has one
            return False
        client = self._client
        if client is None:
            return False
        provider = self.active_provider()
        if not getattr(client, "idempotent_submit", False):
            # Not a refusal to recover — the ordinary budget still runs. It is
            # a refusal to RE-RUN, recorded so a transcript reader can see the
            # capability was asked for and answered no.
            self._log(
                "transport_replay_declined",
                request_id=req.request_id,
                data={
                    "reason_code": "not_idempotent",
                    "provider": provider,
                    "phase": phase.value,
                },
            )
            return False
        try:
            reply_exists = bool(client.reconcile(req.request_id))
        except Exception as reconcile_exc:
            # Could not ask is not "absent". Fail closed: no replay, and the
            # fault falls through to the ordinary budget.
            self._log(
                "transport_replay_declined",
                request_id=req.request_id,
                data={
                    "reason_code": "reconcile_failed",
                    "provider": provider,
                    "error": f"{type(reconcile_exc).__name__}: {reconcile_exc}",
                },
            )
            return False
        if reply_exists:
            return False
        if req.replays_used >= MAX_AWAIT_REPLAYS:
            self._log(
                "transport_replay_declined",
                request_id=req.request_id,
                data={
                    "reason_code": "replay_budget",
                    "provider": provider,
                    "replays_used": req.replays_used,
                    "max_replays": MAX_AWAIT_REPLAYS,
                },
            )
            return False
        req.replays_used += 1
        # The marks the FAILED invocation left describe an invocation that
        # appended nothing anywhere — that is what `idempotent_submit` asserts —
        # so they must not make `submitting` treat the re-issue as ambiguous.
        # `resends_used` is deliberately NOT reset: it bounds same-chat resends
        # and this is not one.
        req.send_attempted = False
        req.submitted = False
        req.last_send_outcome = ""
        req.start_timeouts = 0
        req.start_timeout_wait_seconds = 0.0
        state.phase = Phase.SUBMITTING.value
        self._log(
            "transport_replay_authorized",
            request_id=req.request_id,
            data={
                "reason_code": "idempotent_submit_unrecoverable_reply",
                "provider": provider,
                "replays_used": req.replays_used,
                "max_replays": MAX_AWAIT_REPLAYS,
                "error": str(exc),
                "kind": type(exc).__name__,
            },
        )
        # AFTER the reconcile, never before: dropping a codex client discards
        # the in-memory reply stash, which is the very thing the reconcile above
        # reads. Dropped now because the replay IS a fresh invocation.
        self._drop_client()
        self._store.save(state)
        return True

    def _handle_transport_failure(self, phase: Phase, exc: BrowserError) -> None:
        """A fault from a transport that is not a browser.

        Everything browser-specific is absent by construction, not by a flag
        checked halfway down: no `restart_command` is run, `browser_restart_skips`
        is never touched, `policy.max_browser_restart_skips` is never consulted,
        and no `browser_error` record is written. The event is `transport_error`
        and it names the provider, so the transcript says which subsystem
        actually failed.

        What is KEPT is the ordinary failure budget. `consecutive_failures` is
        the loop's generic "this keeps failing" counter — git failures spend it
        too — and a transport that cannot answer must still end somewhere. The
        alternative, an exemption, would let a permanently broken codex retry
        forever, which is a worse version of the fault this replaces.

        The end of that budget is a PARK, not `failed`: parking records a
        `blockers.Blocker` carrying the exact guidance, and the guidance names a
        fix for the transport in use. That guidance lives in
        `conversation.transport_remedy` rather than here, because this module is
        held provider-agnostic by test — the registry is the one place that may
        know a provider by name.
        """
        state = self.state
        provider = self.active_provider()
        # Before `_drop_client`, because the replay decision reads the held
        # client's own `reconcile`.
        if self._replay_unrecoverable_await(phase, exc):
            return
        self._drop_client()
        state.consecutive_failures += 1
        self._log(
            "transport_error",
            data={
                "phase": phase.value,
                "provider": provider,
                "error": str(exc),
                "kind": type(exc).__name__,
                "consecutive_failures": state.consecutive_failures,
            },
        )
        verdict = self._policy.check_failure_budget(state.consecutive_failures)
        if verdict.allowed:
            # Phase unchanged — the loop re-enters it with a fresh client.
            self._store.save(state)
            return
        # The run is over for this transport. Same accounting rule as the
        # browser path: a candidate that was out for review when the transport
        # gave up has to be re-produced, and that redo is a fault's cost rather
        # than the task's. The CODE differs deliberately —
        # `browser_session_lost` would file a subprocess fault under the browser.
        self._note_round_fault("transport_session_lost")
        self._to_needs_user(
            f"the {provider} transport failed "
            f"{state.consecutive_failures} times in a row, more than "
            f"policy.max_consecutive_failures "
            f"({self._policy.config.max_consecutive_failures}) allows. "
            f"{transport_remedy(provider)} "
            f"Then resume with `python -m autoloop run --retry`. "
            f"Last error: {exc}",
            resume_phase=phase.value,
            kind="loop_fatal",
            code="transport_failure_budget_exhausted",
            detail=(
                f"phase={phase.value} provider={provider} "
                f"kind={type(exc).__name__} "
                f"consecutive_failures={state.consecutive_failures}"
            ),
        )

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
        # The FIFTH corrective re-prompt, found by walking every site that
        # replaces `last_response` with a payload asking for another decision
        # rather than by the list in the brief. It is the same shape as a policy
        # denial — "nothing further was done, decide how to proceed" about a
        # state that has not changed — so it carries the binding for the same
        # reason. The staleness guard in `_consume_carried_postcommit` is what
        # makes that safe here specifically: a git failure is exactly the moment
        # the repository may have moved, and a carry whose candidate is no
        # longer `state.task_execution`'s is discarded rather than honoured.
        self._carry_postcommit_forward()
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

        **Autonomous mode (halt-02, 2026-08-25) intercepts here, and ONLY
        here.** With `config.autonomy.enabled` — default False — a `code` that
        `blockers.autonomous_recovery` recognises does not necessarily park:
        the blocker record is written exactly as it always is, and then the
        recovery path that already exists is re-entered, bounded by a budget
        counted on that durable record. When the budget is spent the loop still
        parks, but `task_fatal` naming the task in flight, so the existing
        quarantine in `cli._handle_parked_task` sets that one task aside and
        the loop keeps working. Every step of that is fail-closed: no store, no
        resolvable task, an unrecognised code, a hard halt, a `resume_phase`
        that is missing or terminal, or a resubmit with no request all fall
        through to the ordinary park below, unchanged.
        """
        state = self.state
        originating_phase = state.phase
        # Resolved BEFORE the record is written, because it decides the record's
        # own `kind` and `task_id` — and `BlockerStore.record` upserts on
        # (task, code, phase) without ever rewriting either field, so a record
        # first written `loop_fatal`/`(loop)` could not be promoted later. One
        # identity for the whole episode, decided once.
        plan = self._autonomy_plan(code)
        if plan is not None:
            set_aside = self._autonomous_set_aside_task(task_id)
            if set_aside is None:
                # There is no task to set aside, so the second half of
                # autonomous recovery cannot happen — and the first half must
                # not either. Retrying toward a park that would still stop the
                # loop just spends rounds to arrive at the same halt.
                plan = None
            else:
                kind = "task_fatal"
                task_id = set_aside
        blocker = None
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
            if plan is not None and self._autonomous_retry(
                plan, blocker, code=code, resume_phase=resume_phase
            ):
                return
        # From here down the loop parks, exactly as it did before autonomous
        # mode existed. The marker is dropped rather than acted on: a park
        # means the retry did NOT recover, so its record must stay open.
        self._autonomous_recovered_blocker = ""
        state.question = question
        state.resume_phase = resume_phase
        state.phase = Phase.NEEDS_USER.value
        state.stop_kind = ""
        state.park_kind = kind
        state.park_task_id = task_id
        state.park_blocker_id = blocker.id if blocker is not None else None
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
        self._store.save(state)

    # ---- autonomous recovery (halt-02, 2026-08-25) --------------------------

    def _autonomy_plan(self, code: str):
        """`blockers.AutonomousRecovery` for `code`, or `None` for "park
        exactly as this loop parks today".

        Four fail-closed gates, in the order they can be answered cheapest
        first. The `is not True` on the flag is deliberate rather than a
        `not ...`: `load_config` refuses a non-boolean, but an `AutoloopConfig`
        built directly (every construction in the test suite) is not validated
        by anything, and a truthy string must not read as consent to act
        without an operator.

        A configured `BlockerStore` is REQUIRED, and not merely because the
        retry budget is counted on it. Setting a task aside deletes the session
        file (`cli._handle_parked_task`) on the strength of the blocker record
        holding the question durably; with no store there is no such record,
        and the question would be destroyed rather than filed.
        """
        autonomy = getattr(self._config, "autonomy", None)
        if autonomy is None or getattr(autonomy, "enabled", False) is not True:
            return None
        if self._blocker_store is None:
            return None
        return autonomous_recovery(code)

    def _autonomous_set_aside_task(self, task_id: str | None) -> str | None:
        """WHICH task autonomous mode would set aside for a fault raised here,
        or `None` when there is no task to set aside.

        The park site's own `task_id` wins — it is the only one that knows a
        fault belongs to a task other than the one currently executing (a push
        approval's `binding.task_id`, say). Otherwise the task in flight, read
        off `state.task_execution`, which is the serialised record of whichever
        task is running the produce-then-review path.

        `None` — a fault raised while no task is in flight, e.g. a login expiry
        during a plan request — is a real answer and not a failure: there is
        nothing to quarantine, so the loop parks as it always did rather than
        inventing a victim.
        """
        if task_id:
            return task_id
        execution = self.state.task_execution
        if isinstance(execution, dict):
            candidate = execution.get("task_id")
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return None

    def _autonomous_retry(self, plan, blocker, *, code: str, resume_phase: str | None) -> bool:
        """Re-enter `plan`'s recovery path. True when the loop moved and the
        caller must return without parking; False when it could not, and the
        caller parks (as a set-aside, since a plan exists) instead.

        The budget is `min(the code's own allowance, config.autonomy.
        max_recovery_attempts)` — the config value can only ever restrain the
        table — and is metered on `BlockerStore.open_recurrences`, i.e. across
        every OPEN record for this (task, code) rather than the one record just
        written. A fault that moves one phase along therefore continues
        spending the same allowance instead of being handed a fresh one.

        `RECOVER_UNAVAILABLE` returns False by construction, which is the whole
        point of that value: the recovery path for those codes is empty, so it
        is exhausted on the first occurrence and the set-aside fires at once.
        """
        state = self.state
        budget = min(plan.max_attempts, self._config.autonomy.max_recovery_attempts)
        # `record` has already counted THIS occurrence, so the retries already
        # spent are one fewer than the total. `max(0, ...)` guards a record
        # written with a nonsense count rather than trusting arithmetic on it.
        spent = max(0, self._blocker_store.open_recurrences(blocker.task_id, code) - 1)
        if spent >= budget:
            return False
        if plan.action == RECOVER_BY_RESUMING:
            target = self._resumable_phase(resume_phase)
            if target is None:
                return False
            state.phase = target.value
        elif plan.action == RECOVER_BY_RESUBMITTING:
            # The same request id on purpose, exactly like `cli.
            # _authorize_resubmit`: if the earlier attempt did land,
            # reconciliation detects it and nothing is duplicated.
            if state.pending_request is None:
                return False
            state.pending_request.send_attempted = False
            state.phase = Phase.SUBMITTING.value
        else:
            return False
        state.question = None
        state.resume_phase = None
        state.stop_kind = ""
        state.park_kind = None
        state.park_task_id = None
        state.park_blocker_id = None
        # Consumed by the first COMPLETED step (`run`'s else branch), which is
        # the only honest evidence the fault is behind the loop. Held in memory
        # only: a process that dies before that step leaves the record open,
        # and the next one reads its recurrences as budget already spent —
        # fewer retries, never more.
        self._autonomous_recovered_blocker = blocker.id
        self._log(
            "autonomous_recovery",
            data={
                "code": code,
                "action": plan.action,
                "blocker_id": blocker.id,
                "task_id": blocker.task_id,
                "attempt": spent + 1,
                "budget": budget,
                "phase": state.phase,
            },
        )
        self._store.save(state)
        return True

    @staticmethod
    def _resumable_phase(raw: str | None) -> "Phase | None":
        """`raw` as a phase the loop can actually step, or `None`.

        A park with no `resume_phase` has said it is not resumable, and a
        terminal one would re-enter the park it just left; both mean "there is
        no recovery path here", which is the same answer as an unparseable
        value. Refusing all three in one place is why the retry cannot fall
        into a phase nobody chose.
        """
        try:
            phase = Phase(raw)
        except ValueError:
            return None
        return None if phase in TERMINAL_PHASES else phase

    def _close_recovered_blocker(self) -> None:
        """A step COMPLETED after an autonomous retry: close that blocker.

        Called from `run`'s else branch, beside the rate-limit reset, and for
        the same reason it is — a completed step is the only free, honest
        evidence that the condition the loop retried through has cleared.

        Never raises. A store that cannot be written leaves the record open,
        which spends the next occurrence's budget rather than granting it, so
        the failure mode of this method is fewer retries.
        """
        blocker_id = getattr(self, "_autonomous_recovered_blocker", "")
        if not blocker_id:
            return
        self._autonomous_recovered_blocker = ""
        if self._blocker_store is None:  # pragma: no cover - never set without one
            return
        try:
            self._blocker_store.close_recovered(
                blocker_id,
                "autonomous recovery: a step completed after the retry, so the "
                "fault this recorded is behind the loop",
            )
        except (StateError, OSError) as exc:
            self._log(
                "autonomous_recovery_close_failed",
                data={"blocker_id": blocker_id, "error": f"{type(exc).__name__}: {exc}"},
            )

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

    def _note_conflict_resolver(
        self, task_id: str, head: str, candidate: str, merge_message: str
    ):
        """The `resolve_conflicts` hook `merge_foreign_commit` offers, bound to
        this task so it can report itself.

        Returns `(worker_git, conflicts) -> bool`. The decision is entirely
        `note_merge.combine_conflicted_notes`'s — the same function
        `auto_merge.AutoMerger._resolve_note_conflicts` calls — so this is the
        reporting half and nothing more. The gateway hands it the gateway that
        is actually mid-merge rather than one built here, which is what makes
        "the three sides it read belong to THIS merge" structural instead of a
        convention.

        Both outcomes are logged, refusals included: this path is about to park
        a reviewed candidate, and "the resolver looked at this and declined, for
        this reason" is the difference between an operator reading one entry and
        reconstructing a merge by hand. Its own entry types rather than
        `auto_merge`'s, because the recovery an operator reaches for differs.
        """

        def resolve(worker_git, conflicts) -> bool:
            outcome = note_merge.combine_conflicted_notes(
                worker_git,
                conflicts,
                merge_message,
                # The incoming head becomes this task's base, so ITS lines lead
                # and the task's own additions stay at the end of the ledger.
                # See `note_merge.OURS_FIRST` — the other order leaves a branch
                # that can never be merged back out.
                lead=note_merge.THEIRS_FIRST,
            )
            if not outcome.resolved:
                self._log(
                    "execution_base_notes_refused",
                    data={
                        "task_id": task_id,
                        "head": head,
                        "candidate_sha": candidate,
                        "conflicted_files": sorted(conflicts),
                        "reason": outcome.refusal,
                    },
                )
                return False
            self._log(
                "execution_base_notes_resolved",
                data={
                    "task_id": task_id,
                    "head": head,
                    "candidate_sha": candidate,
                    "paths": list(outcome.paths),
                },
            )
            return True

        return resolve

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

        ONE CONFLICT SHAPE IS COMBINED INSTEAD OF PARKED (notes-04,
        2026-08-23), and it is the SAME one, decided by the SAME code, that
        `auto_merge.AutoMerger._merge` already combines when a task is merged
        the other way: both sides only appended change-note lines to the
        terminal append-only section of a tracker in `note_merge.NOTE_TRACKERS`.
        `note_merge.combine_conflicted_notes` is handed the in-progress merge
        (via `merge_foreign_commit`'s `resolve_conflicts` hook, so it sees the
        three index stages before the abort clears them) and either concludes
        the merge or declines, and a decline lands on the park below with the
        message it always had.

        Why this direction needed it at all: every task appends a change note
        by construction, so two tasks in flight across one merge collide in the
        trackers by DEFAULT. Measured 2026-08-23, hours after notes-03 widened
        the tracker list: `blk-quota-01-002` parked `task_base_behind_head`
        because the head conflicted at `docs/SUMMARY.md` and `docs/TESTS.md` —
        both in that list, both the append-at-the-end shape — and an 11-file
        reviewed candidate that had passed validation was abandoned. Widening
        WHICH files may be combined bought nothing here, because the resolver
        was never consulted in this direction at all.

        `THEIRS_FIRST` is not a detail. Here "theirs" is the incoming head,
        which becomes this task's new base, so its note lines must come FIRST
        and the task's own additions must stay at the very end — otherwise the
        branch's section no longer starts with its base's byte for byte and the
        eventual merge back OUT refuses forever (the ctx-01 shape; see
        `note_merge.OURS_FIRST`).

        NOTHING ELSE IS WEAKENED. A conflict in any path outside that list —
        a source file, or a tracker's own prose above the marker — refuses the
        whole merge and parks. The five preconditions below still run FIRST, so
        a dirty worker or a tip that lost the candidate is still refused before
        any merge is attempted, and a resolution that cannot be verified is
        reported as a failure by `merge_foreign_commit` rather than accepted.

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
            merge_message = (
                f"autoloop: merge branch head {head[:12]} into task {task.id} "
                f"(reviewed candidate {candidate[:12]} preserved)"
            )
            attempt = worker.merge_foreign_commit(
                # Absolute, resolved: the policy layer refuses a relative
                # fetch source outright, and `GitGateway` does not resolve
                # `repo_root` for itself.
                str(Path(self._git.repo_root).resolve()),
                head,
                merge_message,
                resolve_conflicts=self._note_conflict_resolver(
                    task.id, head, candidate, merge_message
                ),
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
        property of the code path rather than a rule each site remembers. The
        two differ once the loop has been moved to another chat, and using the
        current URL to reconcile an older request would ask the wrong chat
        whether it holds a message it never received.
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
        ever correct while the loop has never left the chat it started in: the
        binding is taken on first touch, every request created since carries
        its own, and `rotations == 0` means the loop URL *is* this request's
        URL. Guarded rather than assumed — an unbound request surfacing after
        the loop moved would be a real inconsistency, and silently pointing it
        at the new chat is exactly the wrong repair.

        `state.rotations` is the guard, and brw-15 removed the only code that
        incremented it. The check is KEPT rather than deleted because the field
        is not: a state file written by an older process can still carry a
        nonzero count, and that file is exactly the one where this request's
        binding cannot be inferred. It reads 0 for every state this code
        writes, which makes the guard inert — not fail-open, since a request
        with no binding in a run that never moved genuinely does belong to
        `state.conversation_url`.
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
