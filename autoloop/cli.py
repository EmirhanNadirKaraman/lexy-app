"""Command-line interface for autoloop.

    python -m autoloop run [--config PATH] [--kickoff FILE | --kickoff-audit |
                            --answer TEXT | --retry] [--null-executor]
                            [--continuous] [--max-steps N]
    python -m autoloop status | tasks | doctor | next-task | blockers [--all]
                                                               (read-only, no lock)
    python -m autoloop answer <blocker-id> "<text>"
    python -m autoloop retire <task-id> [--superseded-by ID ...] [--reason TEXT]
                                        [--rewrite-dependents]
    python -m autoloop smoke-browser [--config PATH] [--provider NAME]
                            (RETIRED, brw-16 2026-08-25: it smoked the browser
                             transport, and no browser-backed provider is
                             registered any more. Prints why and exits 2 —
                             loads nothing, locks nothing, runs nothing)
    python -m autoloop pause | resume | unlock | reset --yes [--tasks]
    python -m autoloop merge-window [--wait] | merge-backlog
    python -m autoloop shipped-report [--repo PATH] [--base REV]  (read-only)
    python -m autoloop record-shipped <task-id> --commit REV --note "..."
    python -m autoloop reprovision-publisher --confirm
    python -m autoloop review-changeset --base <sha> --candidate <sha> [--packet FILE]

Locking: run / resume / reset / answer / retire / release /
merge-backlog take
the single-instance lock on the state directory (fail closed against a live
process; `unlock` is the only stale-lock recovery, and it refuses live locks).
status / tasks / doctor / next-task / blockers / pause / merge-window /
shipped-report stay available while locked — they only report. `merge-backlog`
moves the branch head, so it does not. `record-shipped` stays available too, and
for a different reason: it writes only to the INBOX, outside the checkout, like
`add-task` and `urgent` — the loop applies it between steps.

**Blockers (`blockers.py`).** `run --continuous` no longer stops on every
park: a `task_fatal` one (see `orchestrator._to_needs_user`'s classification)
quarantines just that task and keeps working whatever else is READY; a
`loop_fatal` one still stops the loop. Either way the operator-facing
question is durably recorded as a `Blocker` — `blockers` lists open ones,
`answer <id> "<text>"` resolves one and, for a `task_fatal` blocker, makes
its task READY again. See `docs/AUTOLOOP.md`'s blockers section.

Exit codes: 0 = clean end (stopped / paused / step budget), 2 = the loop parked
itself (needs_user / failed) and wants operator attention, 1 = hard error.

**Produce-then-review, unconditionally (2026-07-30).** `_build_orchestrator`
always constructs the full collaborator set (`WorkerRepoManager`,
`TaskExecutionStore`, `IntentStore`, a provisioned `Publisher`) — the plain
`run` command has exactly one dispatch path now; see docs/SECURITY.md S21.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .audit.agents import ClaudeCliRunner
from .audit.executor import AuditExecutor
from .audit.markdown import MarkdownPolicy
from .blockers import NO_TASK, Blocker, BlockerStore, by_severity
from .changeset_review import build_changeset_binding, build_changeset_packet
from .config import AutoloopConfig, load_config as _read_config_file
from .contract import AUDIT_TASK_ID, Decision, Directive
from .conversation import create_conversation
from . import health, heartbeat
from .doctor import DoctorProbes, _default_probe_cdp, exit_code, run_doctor
from .errors import (
    AutoloopError,
    ConfigError,
    GitError,
    LockHeldError,
    StaleLockError,
    StateCorruptError,
    StateError,
    TaskGraphError,
)
from .executor import ExecutionOutcome, NullExecutor, TaskExecutor
from .git_gateway import GitGateway
from .implement_executor import ImplementExecutor, implement_agent_runner
from .inbox import (
    KIND_URGENT,
    MAX_WORK_SUGGESTIONS,
    InboxError,
    IntakeError,
    TaskInbox,
    apply_requests,
    audit_finding_suggestions,
    create_draft,
    draft_blockers,
    create_draft_from_file,
    draft_from_suggestion,
    draft_path,
    draft_specs,
    gather_suggestions,
    inbox_dir_for,
    intake_dir_for,
    interview_step,
    list_drafts,
    open_blocker_suggestions,
    plan_step,
    provider_asker,
    read_draft,
    ready_task_suggestions,
    record_decline,
    refuse_if_round_running,
    submit_draft,
)
from .auto_merge import (
    UPGRADE_EXEC_FAILED,
    UPGRADE_EXECED,
    UPGRADE_PENDING,
    UPGRADE_PREFLIGHT_FAILED,
    UPGRADE_UNAPPLICABLE,
    MergeDeferralStore,
    PendingUpgrade,
    UpgradeStore,
)
from .lock import LoopLock
from .manifest import ManifestStore, snapshot as manifest_snapshot
from . import merge_sweep
from .orchestrator import (
    MAX_REPEATED_STOPS,
    PREEMPTION_STOP_KIND,
    SELF_UPGRADE,
    Orchestrator,
    release_task_to_pending,
)
from .policy import PolicyConfig, PolicyEngine
from .prompts import (
    TEMPLATES,
    PromptTemplate,
    kickoff_payload,
    user_answer_payload,
)
from .publisher import (
    Publisher,
    provision_publisher_repo,
    read_publisher_url_snapshot,
    redact_url,
    reprovision_publisher as _reprovision_publisher_snapshot,
)
from .stall import StallPolicy
from .state import (
    TERMINAL_PHASES,
    LoopState,
    Phase,
    StateStore,
    StopRepetitionStore,
    stop_repetition_file,
    utcnow_iso,
)
from .tasks import Task, TaskRegistry, TaskState, TaskStore, mutation_ledger_for
from .transcript import TranscriptLogger, build_profile, read_records, render_profile
from .validation_env import load_validation_env
from .worker_env import WorkerRepoManager, validate_workers_root, verify_worker_isolation
from .worktask import (
    IntentStore,
    RecordedRevertAuthority,
    TaskExecutionStore,
    preserve_execution,
)

DEFAULT_CONFIG = Path(".autoloop/config.toml")

#: Migration notices already printed by THIS process. Deliberately the only
#: piece of "have we said this yet" state in the codebase: `config.load_config`
#: stays pure and returns notices as data, so nothing about which tests ran
#: first can change what a config parses to.
_EMITTED_MIGRATION_NOTICES: set[str] = set()


def emit_migration_notices(config: AutoloopConfig, stream=None) -> None:
    """Print each retired-key notice on stderr, at most once per process.

    **stderr, not stdout**: `status`, `tasks`, `next-task` and `blockers` are
    read-only commands whose stdout gets piped and parsed. A notice on stdout
    would corrupt that output; on stderr it reaches the operator regardless.

    **Once per process**, because the loop calls `load_config` on every command
    and `run --continuous` is a long-lived process — a notice repeated each
    round is one an operator learns to scroll past, which defeats the point of
    warning at all.
    """
    stream = sys.stderr if stream is None else stream
    for notice in config.migration_notices:
        if notice in _EMITTED_MIGRATION_NOTICES:
            continue
        _EMITTED_MIGRATION_NOTICES.add(notice)
        print(notice, file=stream)


def load_config(path: Path) -> AutoloopConfig:
    """`config.load_config` plus the operator-facing notice for retired keys.

    Every CLI command reads its config through this wrapper, so a config naming
    a retired key is reported no matter which command is run. It is also what
    the test suite monkeypatches (`cli.load_config`), and patching it continues
    to bypass both the file read and the notice — as those tests intend.
    """
    config = _read_config_file(path)
    emit_migration_notices(config)
    return config


def _load_state(config: AutoloopConfig) -> tuple[StateStore, LoopState | None]:
    store = StateStore(config.state_file)
    state = store.load()
    # An UNSET `browser.conversation_url` is not drift, it is the absence of a
    # configured conversation — the normal shape of a config since brw-16
    # (2026-08-25) removed the browser provider and made `[browser]` optional.
    # Without this an operator who deletes the now-unused section mid-session
    # could not start the loop again without a `reset`, which is exactly the
    # tidy-up this refusal has no business blocking: nothing aims a registered
    # transport at `state.conversation_url`. A config that DOES declare a URL is
    # still held to it, unchanged.
    configured_url = config.browser.conversation_url
    if state is not None and configured_url and state.conversation_url != configured_url:
        if not _drift_is_recorded_rotation(state, config):
            raise ConfigError(
                "browser.conversation_url in the config differs from the one this "
                "session started with. Restore the config value or `reset` the state "
                "to begin a new session."
            )
    return store, state


def _drift_is_recorded_rotation(state: LoopState, config: AutoloopConfig) -> bool:
    """Is this drift the loop's own rotation rather than an edited config?

    A completed rotation writes the new URL to state and then heals the config.
    If the process died between those two steps — or the heal was refused — the
    config still names the chat the loop deliberately abandoned, and refusing to
    start would strand the session on exactly the fault it just recovered from.

    Narrow on purpose. The state must carry a rotation record whose `new_url` is
    where the state now points AND whose `old_url` is what the config still
    says: precisely the "we moved, the file did not" shape. Any other
    disagreement — an operator pointing the config somewhere new, a stale state
    file, a rotation record that matches neither side — still refuses, because
    those are the cases where continuing would silently run against a
    conversation nobody chose.
    """
    record = state.last_rotation
    if not record or not state.rotations:
        return False
    return (
        record.get("new_url") == state.conversation_url
        and record.get("old_url") == config.browser.conversation_url
    )


def _load_tasks(config: AutoloopConfig) -> tuple[TaskStore, TaskRegistry]:
    """The task store and its registry, wired to the mutation ledger.

    The ledger is where an immediate operator priority edit is attested
    (`tasks.MutationLedger`). It is passed HERE — the one place a real run
    builds its store — so the loop reads the same file the dashboard writes;
    both derive the path from `tasks.mutation_ledger_for`, never by spelling it
    twice.
    """
    task_store = TaskStore(
        config.tasks_file,
        ledger=mutation_ledger_for(config.workers_root, config.state_dir),
    )
    registry = task_store.load()
    if registry is None:
        registry = _seed_registry(config)
    return task_store, registry


def _seed_registry(config: AutoloopConfig) -> TaskRegistry:
    """A fresh `TaskRegistry`, seeded from the git-tracked `seed_tasks.json`
    when `.autoloop/tasks.json` has never been written (a brand-new
    deployment, or right after `reset`). Read-only: `TaskStore.save` (called
    from the normal dispatch path the first time anything touches the task
    graph — `plan`, `mark_in_progress`, ...) is what actually creates
    `tasks.json` on disk, exactly the "created on demand" semantics
    `TaskStore`/`TaskRegistry` already have; this function never writes, so a
    session that never touches the task graph leaves nothing behind, same as
    an empty registry always did."""
    registry = TaskRegistry()
    seed_path = config.seed_tasks_file
    if not seed_path.exists():
        return registry
    try:
        specs = json.loads(seed_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"cannot parse seed tasks file {seed_path}: {exc}") from exc
    registry.add_many(
        [
            Task(
                id=spec["id"],
                title=spec["title"],
                description=spec["description"],
                depends_on=tuple(spec.get("depends_on", ())),
                # Threaded explicitly: a field the seed declares and this
                # function forgets is silently dropped, which is how
                # `validation`/`validation_cwd` were lost before. A dropped
                # priority is worse than useless — it looks set in the file
                # while `next_ready` falls back to the id tie-break.
                priority=int(spec.get("priority", 100)),
                validation=tuple(tuple(c) for c in spec.get("validation", ())),
                validation_cwd=spec.get("validation_cwd", ""),
                approved_paths=tuple(spec.get("approved_paths", ())),
            )
            for spec in specs
        ]
    )
    return registry


class _DispatchingExecutor:
    """Routes a directive to the read-only audit executor or the
    write-capable implement executor.

    The orchestrator holds exactly ONE `TaskExecutor` (`self._executor`,
    constructed once in `_build_orchestrator` and called from a single site,
    `orchestrator.py`'s `_dispatch_task_postcommit`) — teaching it about two
    executors would mean widening the `TaskExecutor` protocol or the
    orchestrator's own dispatch method, which is more than this needs. A
    small dispatcher built here, in the CLI's wiring layer, keeps both real
    executors constructible in the same process without touching
    `orchestrator.py` or `executor.py` at all.

    Routing matches `orchestrator._dispatch_executor`'s own `is_audit`
    computation EXACTLY (`directive.decision is Decision.AUDIT or
    directive.task_id == AUDIT_TASK_ID`), so a revise-of-audit directive
    (`decision=REVISE`, `task_id="audit"`) still reaches `AuditExecutor`
    rather than `ImplementExecutor` — the same condition, duplicated
    deliberately rather than imported as a shared helper, since the two call
    sites making the same decision independently is exactly the "policy
    denies it upstream, the executor refuses it downstream" defense-in-depth
    shape both executors already rely on (see each one's own refusal branch).
    """

    def __init__(self, audit_executor: AuditExecutor, implement_executor: ImplementExecutor):
        self._audit = audit_executor
        self._implement = implement_executor

    def execute(self, directive: Directive, task: Task | None) -> ExecutionOutcome:
        is_audit = directive.decision is Decision.AUDIT or directive.task_id == AUDIT_TASK_ID
        executor = self._audit if is_audit else self._implement
        return executor.execute(directive, task)


def _load_validation_env(config: AutoloopConfig, repo_root: Path):
    """The configured `ValidationEnv`, or None when no file is configured.

    Raises `ConfigError` (refusing the whole run) when a file IS configured
    but is unsafe or unparseable — never falls back to "no credentials",
    because a task whose declared validation needs a database would then
    report a failure that looks like a code problem instead of a
    configuration one.
    """
    if config.validation_env_file is None:
        return None
    return load_validation_env(
        config.validation_env_file,
        repo_root=repo_root,
        state_dir=config.state_dir,
        workers_root=config.workers_root,
        # Where the TARGET repository declares its application database. Passed
        # here AND in `doctor.py`, never defaulted in one of the two: doctor
        # exists to report exactly what a real run enforces, so a divergence
        # would let it come back clean on a config this refuses.
        env_example_file=config.repo.env_example_file,
        env_example_db_key=config.repo.env_example_db_key,
    )


def _recorded_out_of_scope_paths(execution_store: TaskExecutionStore):
    """`cleanup_paths_for` for `ImplementExecutor`: this task's own recorded
    out-of-scope residue, straight off the execution record.

    The narrow cleanup authority (scope-04) has exactly one source, and this
    binds it: `TaskExecution.out_of_scope_paths`, which
    `orchestrator._dispatch_task_postcommit` and `_verify_committed` write from
    their own path comparisons and nothing else writes at all. Reading it here
    rather than threading it through `TaskExecutor.execute` keeps the protocol
    two-argument and keeps the authority on the LOOP's side of the boundary: the
    executor is handed the record's answer, never asked for one.

    A task with no record yet (its first dispatch) yields the empty tuple, which
    is the whole of the "an earlier round must have created it" rule.
    """

    def read(task_id: str) -> tuple[str, ...]:
        execution = execution_store.load(task_id)
        return tuple(execution.out_of_scope_paths) if execution is not None else ()

    return read


def _build_executor(
    config: AutoloopConfig,
    args,
    git: GitGateway,
    registry: TaskRegistry,
    worker_repos: WorkerRepoManager,
    policy: PolicyEngine,
    validation_env=None,
    cleanup_paths_for=None,
    revert_authority=None,
) -> TaskExecutor:
    if getattr(args, "null_executor", False) or config.executor.kind == "null":
        return NullExecutor()
    # Read-only audit subagents keep an ELAPSED bound — they change no files,
    # so there is no progress to observe, and a timeout there costs a re-run
    # rather than destroying work. The write-capable executor below is the one
    # that gets the stall detector (`autoloop/stall.py`).
    stall_policy = StallPolicy(
        stall_seconds=config.audit.agent_stall_seconds,
        ceiling_seconds=config.audit.agent_ceiling_seconds,
    )
    audit_runner = ClaudeCliRunner(
        repo_root=git.repo_root,
        command=config.audit.agent_command,
        timeout_seconds=config.audit.audit_agent_timeout_seconds,
    )
    audit_executor = AuditExecutor(
        git=git,
        agent_runner=audit_runner,
        markdown=MarkdownPolicy(git.repo_root),
        registry=registry,
        run_dir_base=config.audit_dir,
        validation_commands=config.audit.validation_commands,
        max_parallel_agents=config.audit.max_parallel_agents,
        # Re-root the audit onto its OWN worker repo per call (2026-07-30 —
        # audit is task-shaped now, see orchestrator.py's
        # `_resolve_audit_task`): `worker_repo_root_for` is `path_for`
        # itself, `policy` is what the fresh worktree `GitGateway` is built
        # with (running under the scrubbed `worker_env()`), and
        # `agent_runner_factory` gives read-only subagents a `cwd` inside
        # that same worker repo rather than the main checkout.
        worker_repo_root_for=worker_repos.path_for,
        policy=policy,
        agent_runner_factory=lambda root: ClaudeCliRunner(
            repo_root=root,
            command=config.audit.agent_command,
            timeout_seconds=config.audit.audit_agent_timeout_seconds,
        ),
        # Where the repository being audited may ship its OWN domain charters.
        # Read per call from that call's repo root; absent means the built-in
        # charters, which is what every deployment that predates the file gets.
        charters_file=config.repo.audit_charters_file,
    )
    implement_executor = ImplementExecutor(
        git=git,
        # The STANDALONE binding, rooted at the main checkout and never
        # reached in production (the factory below wins whenever
        # `worker_repo_root_for` is set, which it always is here). It gets no
        # `policy=` and therefore no progress probe ON PURPOSE: a probe built
        # here would watch the main checkout, not a worker repo, and would
        # report progress made by something else entirely.
        agent_runner=implement_agent_runner(
            git.repo_root,
            command=config.audit.agent_command,
            timeout_seconds=config.audit.agent_ceiling_seconds,
        ),
        # Same validation commands and agent CLI settings as the audit —
        # there is no separate `[implement]` config section (kept minimal;
        # add one if the two ever need to diverge).
        validation_commands=config.audit.validation_commands,
        # ONLY the implement executor gets the credentials — the audit
        # executor above deliberately does not (read-only agents, no writer,
        # no reason for a database).
        validation_env=validation_env,
        worker_repo_root_for=worker_repos.path_for,
        policy=policy,
        # The production binding. `policy=` is what builds the
        # `stall.WorkerTreeProbe` over THIS task's worker repo, so the agent
        # is bounded by silence in the tree it is actually writing to.
        agent_runner_factory=lambda root: implement_agent_runner(
            root,
            command=config.audit.agent_command,
            timeout_seconds=config.audit.agent_ceiling_seconds,
            policy=policy,
            stall_policy=stall_policy,
        ),
        # The cleanup exception's only authority (scope-04). Absent — nothing
        # passes one — the executor grants no cleanup at all, so this wiring is
        # what turns the capability on for a real run and for nothing else.
        cleanup_paths_for=cleanup_paths_for,
        # The REVERT exception's only authority (scope-05, 2026-08-24), and the
        # same story one field on: without this keyword the executor offers no
        # revert at all, never mentions `REVERT-OUT-OF-SCOPE:` in the prompt, and
        # refuses every request — the fail-closed default. It supplies the base
        # sha a restore reads from and records what was restored; WHICH paths may
        # be named still comes from `cleanup_paths_for` alone, so there is
        # exactly one authorizing list and this adds no second one.
        revert_authority=revert_authority,
    )
    return _DispatchingExecutor(audit_executor, implement_executor)


def _build_orchestrator(config, args, store, state, task_store, registry) -> Orchestrator:
    """Construct the full produce-then-review collaborator set. After this,
    `run` (continuous or not) has exactly ONE dispatch path for
    audit/implement/revise — see docs/SECURITY.md S21."""
    policy = PolicyEngine(config.policy)
    git = GitGateway(Path.cwd(), policy)
    # Autoloop M1 finding #1: refuse — never silently fall back to the old
    # `config.workers_dir` (nested inside the checkout) — before a
    # `WorkerRepoManager` capable of running real tasks is ever constructed.
    # `doctor` runs the identical check (`validate_workers_root`) as a
    # non-fatal report; this is the one that actually stops a run.
    workers_root_violations = validate_workers_root(config.workers_root, git.repo_root, config.state_dir)
    if workers_root_violations:
        raise ConfigError(
            "paths.workers_root is not safe to use: " + "; ".join(workers_root_violations)
        )
    worker_repos = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
    # The validation-environment boundary. Loaded ONCE, here, and handed only
    # to the two post-writer validation sites (the ImplementExecutor's own run
    # and the orchestrator's post-commit re-run). A bad file refuses the run
    # rather than degrading to "validation without a database", which would
    # look like a pass while proving nothing — the same fail-closed shape as
    # `validate_workers_root` directly above. `doctor` reports the identical
    # checks non-fatally.
    validation_env = _load_validation_env(config, git.repo_root)
    execution_store = TaskExecutionStore(config.executions_dir)
    intent_store = IntentStore(config.intents_dir)
    blocker_store = BlockerStore(config.blockers_dir)
    publisher_path = provision_publisher_repo(config.state_dir, git)
    publisher = Publisher(publisher_path, "origin", policy)
    publisher_url_snapshot = read_publisher_url_snapshot(config.state_dir)
    return Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=policy,
        git=git,
        executor=_build_executor(
            config, args, git, registry, worker_repos, policy, validation_env,
            # The SAME store the orchestrator below writes execution records
            # with — the cleanup authority has to read what the loop recorded,
            # not a second view of it.
            cleanup_paths_for=_recorded_out_of_scope_paths(execution_store),
            # And the same store again, for the two things a RESTORE needs off
            # the record that a deletion does not: the `task_base_sha` its
            # content is read from, and the durable note of what was put back.
            # One store, so "what the loop recorded" and "what the executor may
            # repair" cannot drift apart.
            revert_authority=RecordedRevertAuthority(execution_store),
        ),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: create_conversation(config.conversation.provider, config),
        # Provider-aware factory: this is what makes a quota failover reachable.
        # The zero-argument one above stays for callers that never switch.
        provider_factory=lambda provider: create_conversation(provider, config),
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worker_repos=worker_repos,
        execution_store=execution_store,
        intent_store=intent_store,
        blocker_store=blocker_store,
        publisher=publisher,
        publisher_url_snapshot=publisher_url_snapshot,
        task_inbox=TaskInbox(inbox_dir_for(config.workers_root, config.state_dir)),
        validation_env=validation_env,
        # `--config` can put the file anywhere, so pass the path actually
        # loaded rather than re-deriving the conventional one. Callers that
        # build an args namespace without it (tests, embedders) fall back to
        # the default, which is where `DEFAULT_CONFIG` points.
        config_path=Path(getattr(args, "config", None) or DEFAULT_CONFIG),
        # The ONE construction allowed to end a round for a self-upgrade, and
        # it covers both `run` paths. Every other orchestrator built against the
        # same state dir — tests, embedders, and until brw-16 (2026-08-25)
        # `smoke-browser`'s own — sees the pending record and must ignore it
        # rather than fail on an upgrade it cannot perform; see the constructor.
        self_upgrade_enabled=True,
    )


def _reset_run_scoped_budgets(config: AutoloopConfig) -> None:
    """Start every run with the budgets that are documented as per-run.

    `state.rotations` is checked against `policy.max_conversation_rotations`,
    described everywhere as "per run" — but it lives in the state file, which
    outlives the process. So it was really per SESSION, and one transport
    failure (a dropped network, a browser that died mid-navigation) spent the
    budget permanently: every later `run --retry` re-read the same count and
    parked with the same reason. The escapes the park message offered were
    both wrong — raise a policy cap for a rotation that was never needed, or
    `reset`, which used to take the task registry with it.

    Resetting here rather than in `_build_orchestrator` is load-bearing:
    `_run_continuous` rebuilds the orchestrator on EVERY iteration
    (`cli.py`'s while loop), so resetting there would refill the budget
    between rotations and remove the cap entirely. This runs once per
    process, which is what "per run" has always claimed to mean.

    A rotation still costs its budget the moment it is attempted, and a
    failed attempt is still not refunded — within one run the cap is exactly
    as strict as before. What changes is that a deliberate operator restart,
    like `--retry` itself, is treated as a new run.
    """
    store, state = _load_state(config)
    if state is None or not state.rotations:
        return
    spent = state.rotations
    state.rotations = 0
    store.save(state)
    TranscriptLogger(config.transcript_file).append(
        "rotation_budget_reset",
        data={
            "rotations_spent_in_previous_run": spent,
            "note": "per-run budget; a new run starts fresh",
        },
    )


def _sweep_backlog_on_startup(config: AutoloopConfig) -> bool:
    """Integrate any published-but-unmerged branch before the loop starts.
    Returns whether it is safe for the loop to run in this checkout.

    Once per PROCESS, here rather than anywhere inside the loop: `Orchestrator.
    run()` is called per iteration of `_run_continuous` (which rebuilds the
    orchestrator each time), so hooking either would re-sweep every round for a
    backlog that only changes when something completes — and completions
    already have `auto_merge.py`. `start` and `resume` both funnel into
    `_cmd_run`, so one hook covers all three commands and both the single-round
    and `--continuous` paths.

    Inside the lock, because it moves the branch head. Silent only when the
    backlog is PROVABLY clear (`SweepResult.is_clear`) — an operator starting a
    loop with nothing outstanding should see nothing new, but a completed task
    the sweep could not judge is exactly the thing that must not pass
    unmentioned, whether it was the remote, the record or the archive that
    would not answer. Every outcome is in the transcript regardless.

    **Reports and continues, EXCEPT when the sweep left the checkout somewhere
    it did not finish putting it** (`SweepResult.is_reconciled`). Almost every
    way a sweep merges nothing mutates nothing — held on an unjudgeable task,
    deferred by the window, refused over a dirty checkout, stopped on a conflict
    that aborted back to the exact pre-merge head — and all of those print and
    let the loop start, because the branch being complained about has already
    waited days and refusing to start over it would be the strictly worse
    failure.

    Two ways do not. A merge that ran and then failed verification is
    deliberately NOT undone (`reset` is off the git whitelist by design), and a
    merge whose push was refused leaves the base moved locally and absent from
    the remote — the latter reported as `auto_merge.DEFERRED`, which is why the
    question is answered from a probe of the checkout rather than from the
    outcome slug. Dispatching ordinary roadmap work onto either state is the
    exact thing stopping the sweep exists to prevent: the loop would build its
    next task on a head nobody verified, or push work stacked on a merge the
    remote has never seen. There is no policy-legal automatic undo, so the only
    honest response is to stop and hand it to the operator.
    """
    result = merge_sweep.sweep_on_startup(config)
    if result.outcome == merge_sweep.DISABLED:
        return True
    if result.outcome == merge_sweep.NOTHING_TO_DO and result.is_clear:
        return True
    for line in _format_sweep(result):
        print(line)
    print("")
    if not result.is_reconciled:
        print(
            "NOT starting the loop: this checkout is not in a state the sweep "
            "finished putting it in (above), and every round would build on it. "
            "Reconcile it by hand, then start again.\n"
        )
        return False
    return True


# ---- running the code the loop just merged -----------------------------------
#
# A merge into the checkout does not reload a live Python process. Measured
# 2026-08-18: plan-01's hard gate merged at 06:23:59 into a loop started at
# 04:07:03, and dash-10 started AFTER the merge without it; brw-11's browser fix
# was inert the same way all night. So the loop replaces its own interpreter,
# and everything below exists to bound WHEN.
#
# `os.execv`, never `importlib.reload`: reloading modules in a running
# orchestrator leaves half-reloaded modules and live objects holding the old
# classes, in a process that authorizes git pushes. `execv` replaces the image
# wholesale and PRESERVES THE PID, which is also what keeps `LoopLock` valid
# across the replacement (see `lock.py`'s handoff section).

#: What the preflight subprocess imports. Deliberately the modules a fresh
#: `python -m autoloop` loads on the way to its first decision — `policy` is the
#: one the 2026-08-18 measurement names — and deliberately NOT a walk of the
#: whole package: `browser.playwright_session` and the codex client have
#: optional third-party dependencies, so a machine without them would fail
#: every preflight and silently disable this feature for good.
PREFLIGHT_MODULES = (
    "autoloop",
    "autoloop.policy",
    "autoloop.auto_merge",
    "autoloop.merge_sweep",
    "autoloop.orchestrator",
    "autoloop.cli",
)

#: Long enough for a cold interpreter importing spaCy-free pure-Python modules
#: on a loaded machine; a timeout counts as a FAILED preflight, which keeps the
#: old code running rather than replacing it on an unanswered question.
PREFLIGHT_TIMEOUT_SECONDS = 120.0


def _package_root() -> Path:
    """The directory holding the `autoloop` package this process imported —
    i.e. the tree a fresh interpreter would load from."""
    return Path(__file__).resolve().parent.parent


def _preflight_import(root: Path) -> tuple[bool, str]:
    """Does the merged tree import? `(True, "")` only when it demonstrably does.

    In a SUBPROCESS, because that is the only way to answer the question at all:
    this process already holds the old modules, and importing again would either
    hit `sys.modules` or (with a reload) produce exactly the half-swapped state
    `execv` exists to avoid.

    `-c` puts the cwd at `sys.path[0]`, so running it in `root` imports the
    checkout rather than any installed copy — the same tree the replacement will
    load, since `_self_upgrade_at_boundary` has already required that the merged
    checkout IS this package's root.

    A failure is a report, never a fault: the loop keeps running the code it has,
    which works.
    """
    script = "\n".join(f"import {name}" for name in PREFLIGHT_MODULES)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, f"rc={proc.returncode} {detail[-2000:]}"
    return True, ""


def _settle_upgrade(
    store: UpgradeStore,
    record: PendingUpgrade,
    status: str,
    detail: str,
    log,
) -> str:
    """Move the record out of `pending` and say why. Returns `status`.

    Every carry-on path goes through here, and that is what keeps the boundary
    from becoming a spin: while the record says `pending`, `Orchestrator.run`
    keeps handing the process back at every round. A record that cannot be
    WRITTEN is therefore cleared instead — losing the marker costs at most one
    delayed restart (the merged code is on disk and the next process start runs
    it), while leaving it costs the loop its ability to make progress.
    """
    record.status = status
    record.detail = detail
    record.settled_at = utcnow_iso()
    try:
        store.save(record)
    except OSError as exc:
        detail = f"{detail} (and the record could not be written: {exc})"
        try:
            store.clear()
        except OSError:
            pass
    log(
        f"self_upgrade_{status}",
        data={"base_sha": record.base_sha, "task_id": record.task_id, "detail": detail},
    )
    return status


def _self_upgrade_at_boundary(config: AutoloopConfig, lock: LoopLock | None) -> str:
    """Replace this process with a fresh interpreter running the merged tree.

    **Does not return on success** — `os.execv` never returns. A return value is
    therefore always "carry on in this process", and names why:

    * `none` — nothing pending (a docs-only merge leaves no record at all), or
      the record has already been settled. The `execed` case is the one-shot:
      a merge that imports and then fails at runtime must not produce a restart
      loop, so a sha that has been exec'd for once is never exec'd for again,
      whatever happened to the process that tried.
    * `unapplicable` — the merged checkout is not the tree this process imported
      from. Replacing would load the same code again.
    * `preflight_failed` — the merged tree does not import. The loop keeps
      running the OLD code, which works, and the failure is reported. A bad
      merge must be reported, not fatal.
    * `exec_failed` — the one-shot marker could not be written, the lock could
      not be armed for the handoff, or `os.execv` itself refused. The lock case
      is refused rather than risked: the successor would find a live lock (its
      own pid), fail closed and end the run. Either way the sha is spent — a
      replacement that was attempted and did not happen is not a licence to try
      the same one again. In none of the three is the record left saying
      `execed`: it is settled `exec_failed`, or cleared outright when it cannot
      be written at all (`_settle_upgrade`). `execed` means "a successor is
      running", and it is what `_confirm_self_upgrade` retires one iteration
      later — but here there is no successor, so the iterations that follow are
      this same old process's.

    The caller reaches here only at `Orchestrator.run`'s boundary, so "never
    mid-round, and never while an agent holds a worker" is established there,
    by the phase, not re-derived here.
    """
    store = UpgradeStore(config.pending_upgrade_file)
    log = TranscriptLogger(config.transcript_file).append
    record = store.load()
    if record is None or record.status != UPGRADE_PENDING:
        return "none"

    running = _package_root()
    merged = Path(record.repo_root).resolve() if record.repo_root else running
    if merged != running:
        return _settle_upgrade(
            store,
            record,
            UPGRADE_UNAPPLICABLE,
            f"the merge moved {merged}, but this process imports autoloop from "
            f"{running} — replacing it would load the same code again",
            log,
        )

    ok, detail = _preflight_import(running)
    if not ok:
        return _settle_upgrade(store, record, UPGRADE_PREFLIGHT_FAILED, detail, log)

    # ONE SHOT, recorded BEFORE the replacement and durable across it. If the
    # write fails the exec does not happen: an un-recorded exec is exactly the
    # restart loop this rule exists to prevent.
    record.status = UPGRADE_EXECED
    record.settled_at = utcnow_iso()
    try:
        store.save(record)
    except OSError as exc:
        return _settle_upgrade(
            store,
            record,
            UPGRADE_EXEC_FAILED,
            f"the one-shot marker could not be written ({exc}), so the "
            "replacement was not attempted",
            log,
        )

    # Arming mints a one-use token into this process's environment and writes
    # it into the lock file; `os.execv` inherits the environment, so ONLY the
    # image this call is about to be replaced by can present it. Nothing
    # between here and the exec may spawn a child — a token in the environment
    # is inherited by every subprocess started while it is set, and the
    # preflight (the one subprocess on this path) has already run.
    if lock is None or not lock.mark_exec_handoff(f"self_upgrade {record.base_sha[:12]}"):
        return _settle_upgrade(
            store,
            record,
            UPGRADE_EXEC_FAILED,
            "the state-dir lock could not be armed for the handoff, so the "
            "replacement was not attempted — its successor would have found a "
            "live lock and refused to start",
            log,
        )

    # The documented launch shape (`python -m autoloop ...`), rebuilt rather
    # than reused: `sys.argv[0]` under `-m` is the path to `__main__.py`, and
    # re-running THAT as a script breaks its relative imports.
    argv = [sys.executable, "-m", "autoloop", *sys.argv[1:]]
    log(
        "self_upgrade_exec",
        data={
            "base_sha": record.base_sha,
            "task_id": record.task_id,
            "paths": list(record.paths)[:20],
            "pid": os.getpid(),
            "argv": argv,
        },
    )
    print(
        f"\nrestarting into {record.base_sha[:12]} — the merge for task "
        f"{record.task_id} changed the loop's own code (same pid, lock held).\n"
    )
    # There is no "after" to flush in: `execv` replaces the image, so anything
    # still buffered is simply lost.
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execv(sys.executable, argv)
    except OSError as exc:
        # No successor is coming, so both halves of the handoff go: the marker
        # on disk and the token in this process's environment. Leaving the
        # token would hand an authorization to every subprocess this run spawns
        # afterwards.
        lock.clear_exec_handoff()
        # And the record is SETTLED, not left saying `execed`. It was written
        # as `execed` a few lines up because it has to be durable BEFORE the
        # call — but that status now means "a replacement is running", and
        # `_confirm_self_upgrade` retires it after one iteration and logs
        # `self_upgrade_confirmed`. This process never went anywhere: the next
        # iteration of `run --continuous` is the OLD code's, and confirming
        # there would record a replacement that did not happen. Settling keeps
        # the one shot exactly as it was — the record has left `pending`, so no
        # boundary will offer this sha again.
        return _settle_upgrade(
            store,
            record,
            UPGRADE_EXEC_FAILED,
            f"os.execv refused the replacement ({type(exc).__name__}: {exc}), so "
            "this process is still running the code it started with — the sha is "
            "spent all the same",
            log,
        )
    return UPGRADE_EXECED      # pragma: no cover - execv does not return


def _confirm_self_upgrade(config: AutoloopConfig) -> bool:
    """One completed iteration under the new code retires the one-shot marker.

    "Completed" deliberately means one full pass of `_run_continuous` — which
    may be a poll that found nothing to do. That still proves what the marker
    guards against: the replacement imported the merged tree, read its config,
    state and registry, ran the selection policy and came back. A stricter
    definition (a finished task) would leave the marker armed for hours on an
    idle loop and block the next upgrade behind work that may never arrive.

    Until this runs, the record says `execed` and no boundary will act on that
    sha again — so a replacement that dies early is never retried.

    `execed` is the ONLY status this confirms, and that is what keeps the
    entry honest: it says a replacement completed an iteration, so a status
    that means "the replacement did not happen" must not reach here. An
    `os.execv` that raises settles the record to `exec_failed` for exactly
    that reason (`_self_upgrade_at_boundary`) — the process carries on, the
    next iteration is the OLD code's, and nothing is confirmed.
    """
    store = UpgradeStore(config.pending_upgrade_file)
    record = store.load()
    if record is None or record.status != UPGRADE_EXECED:
        return False
    try:
        store.clear()
    except OSError:
        return False
    TranscriptLogger(config.transcript_file).append(
        "self_upgrade_confirmed",
        data={
            "base_sha": record.base_sha,
            "task_id": record.task_id,
            "note": "one full iteration completed under the merged code",
        },
    )
    return True


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with LoopLock(config.state_dir) as lock:
        _reset_run_scoped_budgets(config)
        if not _sweep_backlog_on_startup(config):
            # Returned BEFORE the try, so the `finally` below never runs and
            # `stopped` is NOT what gets published: that status is deliberately
            # outside `ATTENTION_STATUSES` ("you stopped it, you know"), and
            # nobody chose this. `parked` is the honest one — the loop is not
            # running and a person has to decide something. Publishing SOMETHING
            # is the point: a previous run that died leaves a `running` beat, and
            # refusing silently would leave the monitor reading it forever while
            # the only notice is on a terminal nobody is watching.
            _, refused_state = _load_state(config)
            heartbeat.publish(
                config,
                refused_state,
                heartbeat.PARKED,
                detail="startup sweep left the checkout unreconciled — reconcile "
                       "it by hand, then `merge-backlog`",
            )
            return 1
        try:
            if getattr(args, "continuous", False):
                _validate_continuous_args(args)
                # The lock travels with it: `_run_continuous` is where the
                # process may replace itself, and the handoff has to be armed
                # on the lock this `with` block actually holds.
                return _run_continuous(args, config, lock)
            return _run_locked(args, config)
        finally:
            # A CLEAN exit publishes `stopped`, which is how the monitor tells
            # "you stopped it" from "it died". Without this both look identical
            # — a heartbeat that simply stopped arriving — and every deliberate
            # stop would raise an alarm. Reached on the SIGTERM path too, whose
            # SystemExit unwinds through here (the lock is already released by
            # then, inside the handler).
            _, final_state = _load_state(config)
            heartbeat.publish(
                config, final_state, heartbeat.STOPPED, detail="run exited"
            )


def _validate_continuous_args(args: argparse.Namespace) -> None:
    """`--continuous` manages session kickoff, resume and the step budget
    itself (see `_run_continuous`) — combining it with a flag that manually
    drives ONE round is almost certainly not what was meant, so it is
    refused outright rather than silently ignored."""
    manual = [
        name
        for name, value in (
            ("--kickoff", args.kickoff),
            ("--kickoff-audit", args.kickoff_audit),
            ("--answer", args.answer),
            ("--retry", args.retry),
            ("--resubmit", args.resubmit),
            ("--max-steps", args.max_steps is not None),
        )
        if value
    ]
    if manual:
        raise StateError(
            "--continuous manages session kickoff, resume and stepping itself "
            f"— {', '.join(manual)} is not valid alongside it. Resolve a park "
            "with `run --retry`/`--answer` etc. WITHOUT --continuous first, "
            "then restart `run --continuous`."
        )


def _run_locked(args: argparse.Namespace, config: AutoloopConfig) -> int:
    store, state = _load_state(config)
    task_store, registry = _load_tasks(config)
    # Startup, before anything selects a task, and on the registry this round
    # will actually use — the single-round counterpart to the sweep at the top
    # of every `_run_continuous` iteration. A `blocked` row with no open blocker
    # left is out of `next_ready()` for no reason (blk-01); a plain `run` is one
    # of the two ways an operator restarts after answering one.
    _print_auto_unblocked(_reconcile_unblocked_tasks(config, task_store, registry))

    if state is None:
        if args.kickoff_audit:
            payload = TEMPLATES["audit_kickoff"].render()
        elif args.kickoff:
            payload = kickoff_payload(Path(args.kickoff).read_text(encoding="utf-8"))
        else:
            raise StateError(
                "no saved session. Start one with `run --kickoff-audit` (first "
                "autonomous audit) or `run --kickoff <report-file>`."
            )
        state = LoopState.new(config.browser.conversation_url)
        state.outbox = payload
        store.save(state)
    elif args.kickoff or args.kickoff_audit:
        raise StateError(
            "a session already exists — `reset` it before starting a new one"
        )

    if args.answer is not None:
        if Phase(state.phase) is not Phase.NEEDS_USER or state.resume_phase is not None:
            raise StateError(
                "--answer is only valid when the loop is parked on a question "
                "it raised for the operator (phase=needs_user without a "
                "retryable phase)"
            )
        state.outbox = user_answer_payload(
            state.question or "(question unavailable)", args.answer
        )
        state.question = None
        state.parse_retries = 0
        state.policy_denials = 0
        state.phase = Phase.READY.value
        store.save(state)
    elif args.resubmit:
        _authorize_resubmit(state)
        store.save(state)
    elif args.retry:
        if state.resume_phase is None:
            raise StateError(
                "--retry needs a retryable failure (login expiry / browser "
                "failure / raised iteration budget); none is recorded"
            )
        state.phase = state.resume_phase
        state.resume_phase = None
        state.question = None
        state.consecutive_failures = 0
        # All three fault counts, for the same reason: the operator has
        # intervened (a `browser_restart_cooldown_blocked` park is resumed
        # exactly here, after restarting the browser by hand; a `rate_limited`
        # park after leaving the account idle), so no budget should arrive
        # already spent.
        state.browser_restart_skips = 0
        state.rate_limit_backoffs = 0
        state.rate_limit_wait_seconds = 0.0
        # And any back-off still owed with it: the operator has already left
        # the account idle, so making them wait out a deadline recorded before
        # they intervened would answer their intervention by ignoring it.
        state.rate_limit_retry_not_before = None
        store.save(state)
    elif Phase(state.phase) in (Phase.NEEDS_USER, Phase.FAILED, Phase.STOPPED):
        # A fault stop takes its own branch, because BOTH suggestions below are
        # impossible for one: `--answer` requires `needs_user` and `--retry`
        # requires a `resume_phase` a fault stop deliberately clears. Printing
        # them would send the operator to two commands that raise.
        if _is_fault_stop(state):
            return _report_fault_stop(config, state, registry)
        print(_summary(config, state, registry))
        print("\nLoop is parked. Use --answer / --retry, or `reset` to start over.")
        return 2

    orchestrator = _build_orchestrator(config, args, store, state, task_store, registry)
    outcome = orchestrator.run(max_steps=args.max_steps)
    # Checked BEFORE the ordinary ending, and it exits non-zero even though the
    # PHASE is the one a healthy run ends in: `stopped` stopped meaning
    # "finished" the moment the loop could end itself on a wall
    # (`orchestrator._to_fault_stop`), and a wrapper script or cron job reading
    # the exit code must not be told a run that died on the denial budget
    # succeeded. Gated on the POSITIVE `stop_kind` for the same reason every
    # other reader of that field is (see `LoopState.stop_kind`), so an
    # unclassified stop reads as the failure it is; `_report_fault_stop` prints
    # the summary itself, so this returns rather than falling through to a
    # second one.
    if _is_fault_stop(orchestrator.state):
        return _report_fault_stop(config, orchestrator.state, registry)
    if outcome == SELF_UPGRADE:
        # Reported, not performed. A single-round `run` carries flags that are
        # not safe to re-run — `--kickoff` refuses a session that now exists,
        # `--answer` refuses a phase that is no longer `needs_user` — so
        # re-execing this argv would end the process on a StateError. The
        # record stays `pending`; the session is mid-flight and untouched, so
        # the next start picks up both.
        print(_summary(config, orchestrator.state, registry))
        print(
            "\nA merge changed the loop's own code, so this process is running "
            "a stale copy. Nothing was lost — the session is exactly where it "
            "was. Start it again (`run --continuous` restarts itself for this)."
        )
        return 0
    print(_summary(config, orchestrator.state, registry))
    # Before the generic ending line, because "Loop ended: stopped" on its own
    # reads as the reviewer's own `stop` — which is the one thing a preemption
    # is not. Silent for every other stop.
    _report_preemption(orchestrator.state)
    print(f"\nLoop ended: {outcome}")
    return 2 if outcome in (Phase.NEEDS_USER.value, Phase.FAILED.value) else 0


#: How long `run --continuous` sleeps, locally, between selection-policy
#: checks when there is genuinely nothing to do (no ready task, repository
#: fingerprint unchanged since the last audit). Not configurable in v1 —
#: keep the surface small.
CONTINUOUS_POLL_SECONDS = 30.0


def _run_continuous(
    args: argparse.Namespace, config: AutoloopConfig, lock: LoopLock | None = None
) -> int:
    """`run --continuous`: loop the existing phase machine indefinitely,
    working around task-scoped blockers instead of halting on them.

    Per outer iteration: a saved session in a NON-terminal phase (mid-flight
    — ready/submitting/awaiting/executing/submission_unconfirmed) is resumed
    via the ordinary, entirely unmodified `Orchestrator.run()` — this is what
    makes a killed-and-restarted `run --continuous` pick up the saved phase
    rather than re-deriving anything (item 13 of the v1 smoke checklist). At
    a clean boundary (no session yet, or the last one ended STOPPED),
    `_select_and_kickoff` runs the selection policy.

    **A session parked on `needs_user` is split by classification**
    (`orchestrator._to_needs_user`'s `kind`, persisted as `state.park_kind`
    — see `_handle_parked_task`): `task_fatal` quarantines the ONE task at
    fault (`TaskRegistry.block`) and clears the session, so the very next
    iteration starts a clean round on whatever else is READY; `loop_fatal`
    (including a missing/unrecognised classification — fail-closed) stops
    the continuous loop outright, exactly as every park did before this
    split existed. `failed` (budget-exhausted browser/git failures — a
    separate phase, never routed through `_to_needs_user`, so never
    classified) always stops the loop too — resolve either with a plain
    `run --retry`/`--answer` (WITHOUT `--continuous`), then restart.

    **A `stopped` session is split too**, and this is the one place the split
    matters most: `stop_kind="contract"` (the reviewer decided the round was
    done) is the clean boundary it has always been, while `stop_kind="fault"`
    (`orchestrator._to_fault_stop` — today, the exhausted policy-denial budget)
    stops the continuous loop like a `loop_fatal` park. Treating a fault stop
    as a boundary would kick off a fresh session into the identical wall on the
    very next iteration; see `_report_fault_stop`.

    **A CONTRACT STOP IS STILL A CLEAN BOUNDARY — but not an unlimited one.**
    Ending the session and selecting again is the right response to one `stop`,
    and to the second one about something else. It is the wrong response to the
    same unresolved situation over and over: on 2026-08-20 this loop spent three
    reviewer turns in fifteen minutes collecting three refusals of one lost
    postcommit binding, and would have kept going for as long as the process
    ran, with `health` reporting `running` / `needs_attention: FALSE`
    throughout. Nothing changes HERE — the bound lives where the stop is
    dispatched (`orchestrator._handle_contract_stop`), and it converts the
    `MAX_REPEATED_STOPS`-th consecutive stop about an unchanged situation into
    an ordinary `loop_fatal` park. This function then handles it through the
    `outcome == Phase.NEEDS_USER.value` branch above like any other park: a
    blocker record exists, `health`/the monitor go red, and `python -m autoloop
    answer <id> "..."` clears it. What the operator sees is the loop stopping
    with the reviewer's own last words quoted, instead of a green dashboard.

    **SELF-UPGRADE.** `Orchestrator.run` can also return `SELF_UPGRADE`: a
    merge has changed the loop's own code and the session has reached a round
    boundary. This is the ONLY place the process replaces itself, and the
    replacement is `os.execv` in the same pid holding the same lock — see
    `_self_upgrade_at_boundary`, which preflights the merged tree first and
    keeps running the old code if it does not import.

    **EXHAUSTION.** Once a clean boundary finds no READY task AND the
    repository fingerprint is unchanged, that used to always mean "sleep and
    poll again" — and still does, UNLESS there is at least one OPEN blocker
    at that point. With one, "nothing ready + nothing new to audit +
    something is still waiting on a human" is "nothing can proceed
    autonomously": every open blocker (id, task, question) is printed and
    the process exits 0 — a clean end, not an error — rather than sleeping
    forever next to unresolved questions nobody asked to see. Zero open
    blockers is still the ordinary idle steady state, unchanged from before
    (and untouched here — the check runs strictly AFTER
    `_select_and_kickoff` returns `False`, so the zero-Claude/zero-ChatGPT
    guarantee that function provides is exactly as before).
    """
    blocker_store = BlockerStore(config.blockers_dir)
    iterations = 0
    upgrade_checked = False
    while True:
        if pause_requested(config):
            print("paused")
            return 0
        # One completed iteration is what retires a self-upgrade's one-shot
        # marker (`_confirm_self_upgrade`). Checked at the TOP of the second
        # iteration rather than at the bottom of the first: every branch below
        # ends the iteration with its own `continue`, and a bottom-of-loop
        # confirmation would be skipped by whichever one somebody forgot.
        if iterations and not upgrade_checked:
            upgrade_checked = True
            _confirm_self_upgrade(config)
        iterations += 1
        store, state = _load_state(config)
        task_store, registry = _load_tasks(config)
        # At the TOP of the iteration, not down at the exhaustion check: the
        # readers that see an orphaned QUARANTINE are out of process
        # (`health.check`, the heartbeat, a monitor), so a loop with plenty of
        # ready work would otherwise report `stuck_blocked` for hours while
        # working perfectly. Orphaned is the operative word — a `loop_fatal`
        # record is never orphaned by a retirement, whatever task it names, and
        # this sweep leaves it open. This is also the only sweep an operator who runs
        # `run --continuous` directly, without `start`, ever gets — and the six
        # pre-`RETIRED` retirements change status on the load one line up, with
        # no command run to notice their records were left open. Costs a set
        # comprehension over the registry when nothing is retired.
        _reconcile_retired_blockers(config, registry)
        # The same sweep run the other way, and on the registry object this
        # iteration is about to hand the orchestrator — not on a fresh load.
        # A task released into a COPY would still be `blocked` in the object
        # `next_ready()` reads, and the round's first ordinary
        # `task_store.save` would write the stale status straight back over the
        # reconciliation. Second, not first: `_reconcile_retired_blockers` may
        # archive a retired task's quarantine, and a retired task is never a
        # candidate here anyway (`blocker_derived_blocked` reads the stored
        # status), so the order costs nothing and keeps the retirement the
        # stronger answer.
        _print_auto_unblocked(_reconcile_unblocked_tasks(config, task_store, registry))

        if state is not None and Phase(state.phase) not in TERMINAL_PHASES:
            orchestrator = _build_orchestrator(config, args, store, state, task_store, registry)
            outcome = orchestrator.run()
            if outcome == "paused":
                return 0
            if outcome == SELF_UPGRADE:
                # Normally does not return: the process is replaced here, with
                # the pid and the lock intact, and comes back at the top of
                # `_cmd_run` running the merged code. When it DOES return —
                # preflight failed, the merge is not this tree, the lock could
                # not be armed, `os.execv` itself raised — the record has been
                # settled, so the next round will not offer the same sha again,
                # AND it no longer says `execed`, so the confirmation at the top
                # of the next iteration has nothing to retire. The loop simply
                # carries on with the code it has.
                _self_upgrade_at_boundary(config, lock)
                continue
            if outcome == Phase.NEEDS_USER.value:
                if _handle_parked_task(config, store, task_store, registry, orchestrator.state) == "task_fatal":
                    continue
                return 2
            if outcome == Phase.FAILED.value:
                print(_summary(config, orchestrator.state, registry))
                print(
                    f"\ncontinuous mode stopped: {outcome} — resolve with `run "
                    "--retry` (WITHOUT --continuous), then restart `run --continuous`."
                )
                return 2
            if _is_fault_stop(orchestrator.state):
                return _report_fault_stop(config, orchestrator.state, registry)
            # A preemption ends the round as a `stopped` session too, and is a
            # clean boundary in exactly the same sense — the next iteration
            # selects again, and the urgent task is what `next_ready()` now
            # returns. Printed rather than passed over silently: the operator
            # who asked for it needs to see what it cost.
            _report_preemption(orchestrator.state)
            continue  # a CONTRACT stop -> reassess at the top of the loop

        if state is not None and Phase(state.phase) is Phase.NEEDS_USER:
            # Found already-parked at the top of an iteration (e.g. a
            # restart after an earlier process was killed). Same
            # classification split as the freshly-parked case above —
            # `state.park_kind` was persisted at park time, so a plain
            # `store.load()` here sees exactly what `orchestrator.state`
            # would have.
            if _handle_parked_task(config, store, task_store, registry, state) == "task_fatal":
                continue
            return 2

        if state is not None and Phase(state.phase) is Phase.FAILED:
            print(_summary(config, state, registry))
            print(
                f"\ncontinuous mode stopped: {state.phase} — resolve with `run "
                "--retry` (WITHOUT --continuous), then restart `run --continuous`."
            )
            return 2

        if state is not None and _is_fault_stop(state):
            # The same check as the freshly-stopped case above, for a session
            # found already fault-stopped at the top of an iteration (a
            # restart after the earlier process exited). Without it, a restart
            # would read `stopped` as a clean boundary and kick straight back
            # into the wall the previous process just died on.
            return _report_fault_stop(config, state, registry)

        # Clean boundary: no session yet, or the last one ended in a CONTRACT
        # stop — the reviewer's own decision that the round was finished.
        if _select_and_kickoff(config, store, registry):
            continue
        # Already reconciled at the top of this iteration, so what is left here
        # genuinely needs a human: exhaustion is announced as "nothing can
        # proceed until someone answers these", and a question about superseded
        # work is one nobody can answer.
        open_blockers = blocker_store.open_blockers()
        if open_blockers:
            _print_blocker_summary(open_blockers)
            return 0
        time.sleep(CONTINUOUS_POLL_SECONDS)


def _is_preemption_stop(state: LoopState) -> bool:
    """Did an operator's urgent request end this session?

    Reads `stop_kind` POSITIVELY, exactly like `_is_fault_stop` below and for
    the same reason: an unclassified stop must fall on the harmless side. A
    session this returns False for is treated as whatever it already was, and
    the only thing being True changes is that the displacement gets printed.
    """
    return (
        Phase(state.phase) is Phase.STOPPED
        and state.stop_kind == PREEMPTION_STOP_KIND
    )


def _report_preemption(state: LoopState) -> None:
    """Print what an urgent preemption displaced, for the operator watching.

    The DURABLE record is the `task_preempted` transcript entry plus the
    quarantined worker repo and archived execution record on disk, which name
    each other under one label; this is what the terminal shows in the moment.
    It prints the quarantine paths rather than a summary of them because the
    displaced round's committed candidate is still in that worker, and the
    operator's next question is always where it went.

    THREE displaced-task lines, not two, because a release has two durable
    steps: status moved and artefacts retired, status moved and artefacts NOT
    retired, or status not moved at all. The middle one is the reason this
    branches — collapsing it into "NOT returned to pending" sends an operator
    to fix a status that is already correct — and it carries one of two
    remedies, keyed on whether the residue is resumable.

    Silent for a session that was not preempted, so both callers can call it
    unconditionally on a clean stop.
    """
    if not _is_preemption_stop(state):
        return
    record = state.preemption or {}
    displaced = record.get("displaced_task_id") or "(nothing)"
    print(
        f"\npreempted for URGENT task {record.get('urgent_task_id')} "
        f"({record.get('urgent_reason')})"
    )
    if not record.get("displaced_returned_to_pending"):
        print(
            f"  displaced    {displaced}: NOT returned to pending — "
            f"{record.get('obstacle') or 'no reason recorded'}"
        )
    elif record.get("displaced_artifacts_retired", True):
        print(f"  displaced    {displaced}: in_progress -> pending, selectable again")
    else:
        # The half-succeeded release, printed as the two facts it is. Merging it
        # into either neighbour misdirects the operator: "NOT returned to
        # pending" sends them to fix a status that is already correct, and
        # "selectable again" on its own hides a residue that keeps the merge
        # window shut for as long as it survives.
        print(
            f"  displaced    {displaced}: in_progress -> pending, selectable again "
            "— but its execution could NOT be retired: "
            f"{record.get('obstacle') or 'no reason recorded'}"
        )
        if record.get("stale_worker_path"):
            print(f"  residue      worker repo still at {record['stale_worker_path']}")
        if record.get("stale_execution_record"):
            print(
                f"  residue      the execution record for {displaced} is still live "
                "— the merge window stays shut while it is"
            )
        if record.get("residue_resumable"):
            print(
                f"  ACTION       none required: the pair is resumable, so the next "
                f"dispatch of {displaced} continues that round. The merge window "
                "reopens when it publishes — retire the pair by hand if it has to "
                "reopen sooner."
            )
        else:
            print(
                f"  ACTION       move {record.get('stale_worker_path') or 'the worker repo'} "
                f"aside before {displaced} is dispatched again — it is not "
                "resumable, so the dispatch will refuse to create a worker over it."
            )
    print(
        f"  boundary     request first seen at phase "
        f"{record.get('first_observed_phase')}, acted on at "
        f"{record.get('preempted_at_phase')} (no review packet was interrupted)"
    )
    candidate = (record.get("displaced_candidate_sha") or "")[:12] or "(none)"
    print(
        f"  quarantined  candidate={candidate} "
        f"review_round={record.get('displaced_review_round')} "
        f"attempts={record.get('displaced_attempt_count')}"
    )
    for label, key in (
        ("worker repo", "quarantined_worker_path"),
        ("record", "archived_execution_record"),
    ):
        if record.get(key):
            print(f"  {label:<12} {record[key]} (kept, not deleted)")


def _is_fault_stop(state: LoopState) -> bool:
    """Did the LOOP end this session on a wall, rather than the reviewer
    ending it with `stop`?

    Reads `stop_kind` positively (`== "fault"`) rather than "not contract", so
    the ambiguous values fall on the harmless side: a state file written before
    the classification existed, or one hand-built by a test, carries `""` and is
    treated as an ordinary clean boundary — which is exactly how continuous
    mode treated every `stopped` session before fault stops existed. The cost
    of being wrong that way is one extra round; being wrong the other way would
    halt a healthy loop on every completed session.
    """
    return Phase(state.phase) is Phase.STOPPED and state.stop_kind == "fault"


def _report_fault_stop(
    config: AutoloopConfig, state: LoopState, registry: TaskRegistry
) -> int:
    """Print why the loop stopped itself and exit 2 — the fault-stop
    counterpart to `_handle_parked_task`'s loop_fatal branch.

    Continuous mode MUST end here rather than fall through to
    `_select_and_kickoff`. A fault stop means the reviewer spent the denial
    budget proposing directives policy refuses; the selection policy would see
    a terminal phase, find the same READY task, and start a fresh session
    straight back into the same wall — burning a full Claude/ChatGPT round per
    iteration while looking like progress. That churn is the reason this
    terminal is distinguishable from a contract stop at all.

    Shared with plain `run` (both its pre-flight branch and its ending) rather
    than continuous-mode-only, which is why the wording names no mode. Both
    callers otherwise reach a message that is wrong for this terminal: the
    parked branch offers `--answer`/`--retry`, and BOTH raise for a fault stop
    — `--answer` requires `needs_user`, `--retry` requires the `resume_phase`
    a fault stop deliberately clears. The recovery named here is the one that
    works, and it is a real one: the blocker record carries the question, and
    `reset` is what makes a new session possible.

    The session file is deliberately left in place (unlike the task_fatal park,
    which removes it): there is no task to quarantine and nothing to resume, so
    what remains is evidence, and `status` should still be able to show it.
    """
    print(_summary(config, state, registry))
    print(
        "\nthe loop ended itself on a fault it cannot ask about "
        "(stop_kind=fault) — the reviewer kept proposing directives policy "
        "refuses, so no answer from you would change the next one.\n"
        "See `python -m autoloop blockers` for the recorded reason; resolve it "
        "with `answer <blocker-id> \"<text>\"`, then `reset --yes` to begin a "
        "new session. `run --answer` / `run --retry` do not apply to this "
        "terminal and will refuse."
    )
    return 2


#: Ids minted by `Orchestrator._resolve_audit_task` (`audit-<iteration>`).
_AUDIT_UNIT_RE = re.compile(r"^audit-\d{4,}$")


def _register_synthetic_audit_unit(registry: TaskRegistry, task_id: str) -> None:
    """Make an audit unit quarantinable by adding it to the registry first.

    Audit units are minted per run (`audit-<iteration>`, see
    `Orchestrator._resolve_audit_task`), never planned, so the registry has
    normally never heard of one. `TaskRegistry.block` then refuses it as
    `task_unknown`, and `_handle_parked_task`'s fail-closed branch escalates
    the park to loop_fatal — which meant EVERY audit that failed its own
    post-commit validation stopped the whole loop, when the intent was to
    quarantine that one unit and carry on. Observed 2026-08-02: `audit-0003`
    was refused on a flaky test and took continuous mode down with it.

    Registering it makes the quarantine real and durable, which a second
    mechanism depends on: `_audit_unit_quarantined` reads the registry to
    decide whether re-minting the same id would re-dispatch a dead unit. If
    the block never lands, that guard sees nothing and the churn it exists to
    stop comes back.

    Deliberately narrow — only ids that match a minted audit unit, and only
    when the registry does not already know one. A real planned task that
    `block` refuses still escalates exactly as before.
    """
    if not _AUDIT_UNIT_RE.match(task_id) or registry.has(task_id):
        return
    registry.add(
        Task(
            id=task_id,
            title="repository audit",
            description=(
                "synthetic audit unit, registered so its failed round can be "
                "quarantined rather than stopping the loop"
            ),
        )
    )


def _handle_parked_task(
    config: AutoloopConfig,
    store: StateStore,
    task_store: TaskStore,
    registry: TaskRegistry,
    state: LoopState,
) -> str:
    """A session just parked on `needs_user`. Returns `"task_fatal"` (the
    caller should `continue` — the one task at fault has been quarantined
    and the session state file is already removed here, so the next
    iteration starts fresh) or `"loop_fatal"` (the caller should print and
    exit 2).

    Fail-closed: only the EXACT string `"task_fatal"`, together with a
    `park_task_id` that actually resolves to a blockable task, is treated as
    task_fatal. `"loop_fatal"`, `None` (a state file written before this
    classification existed, or a hand-built one in a test), or a
    `park_task_id` that `TaskRegistry.block` refuses (unknown id, already
    completed) all fall through to loop_fatal. A `blockers.Blocker` record
    was already written by `orchestrator._to_needs_user` regardless of
    kind — this function only decides what the LOOP does next, never
    whether a blocker exists.

    `task_store.save(registry)` is load-bearing, not decoration: the caller
    reloads `tasks.json` fresh from disk on every outer iteration
    (`_load_tasks`), so an unpersisted `block()` would be invisible on the
    very next pass and the task would come back READY — silently undoing
    the quarantine and burning a fresh Claude/ChatGPT round on the same
    failure.
    """
    if state.park_kind == "task_fatal" and state.park_task_id:
        reason = state.question or "(no reason recorded)"
        _register_synthetic_audit_unit(registry, state.park_task_id)
        try:
            registry.block(state.park_task_id, reason)
        except TaskGraphError as exc:
            if exc.code == "task_completed":
                # The park is stale: its task finished after the park was
                # written — typically the operator published the candidate the
                # park was complaining about and marked the task done. There is
                # nothing to quarantine, and `block` refuses a completed task
                # precisely because quarantining finished work is meaningless.
                #
                # Escalating here (as the generic branch does) made resolving a
                # park BY COMPLETING ITS TASK produce a session that could never
                # be recovered, only archived — hit twice on 2026-08-02, once
                # per published candidate. Same shape as the `in_progress` gap
                # `release` closed: a state the fail-closed branch cannot tell
                # apart from a real fault.
                print(
                    f"task {state.park_task_id} is already completed — its park is "
                    "stale and there is nothing to quarantine.\n"
                    "continuous mode continues with other ready tasks."
                )
                store.path.unlink(missing_ok=True)
                return "task_fatal"
            print(
                f"blocker for task {state.park_task_id!r} is classified task_fatal, "
                f"but the task could not be quarantined ({exc}) — treating this park "
                "as loop_fatal instead of silently dropping it."
            )
        else:
            task_store.save(registry)
            # NOT `store.archive()`: archiving would leave one
            # `state.json.bak-<timestamp>` behind per task_fatal park, and a
            # long continuous run can hit many of these — unbounded litter
            # for a file whose entire content (question/detail/phase/task)
            # already survives durably in the just-written `Blocker` record.
            store.path.unlink(missing_ok=True)
            print(
                f"task {state.park_task_id} blocked (task_fatal): {reason}\n"
                "continuous mode continues with other ready tasks — see "
                "`python -m autoloop blockers`."
            )
            return "task_fatal"
    print(_summary(config, state, registry))
    print(
        "\ncontinuous mode stopped: needs_user (loop_fatal) — resolve with "
        "`run --retry` / `--answer` (WITHOUT --continuous), then restart "
        "`run --continuous`."
    )
    return "loop_fatal"


def _print_blocker_summary(blockers: list[Blocker]) -> None:
    # `by_severity`, not a local `sorted(key=created_at)`: the loop must not
    # have two opinions about which open blocker matters most (blk-02). Every
    # one of them is still printed — ranking picks a reading order, never a
    # shortlist.
    ordered = by_severity(blockers)
    print(
        f"continuous mode: exhausted — no ready task, the repository fingerprint "
        f"is unchanged, and {len(blockers)} blocker(s) are still open:"
    )
    for b in ordered:
        print(f"  {b.id}  task={b.task_id}  code={b.code}  {b.question}")
    if len(ordered) > 1:
        print(
            f"\nMost severe first — {ordered[0].id} is the primary one; the "
            f"other {len(ordered) - 1} above are open too and each still needs "
            "an answer."
        )
    print(
        "\nResolve with `python -m autoloop answer <blocker-id> \"<text>\"`, then "
        "restart `run --continuous`."
    )


def _select_and_kickoff(
    config: AutoloopConfig, store: StateStore, registry: TaskRegistry
) -> bool:
    """The continuous-mode selection policy (`Do NOT build another
    task-graph engine` — this reuses `TaskRegistry.next_ready()` exactly as
    it is). Returns `True` if a new round was started (the caller should
    reassess immediately), `False` if there is genuinely nothing to do (the
    caller should sleep).

    Order is load-bearing for the zero-calls guarantee: `registry.
    next_ready()` and the fingerprint comparison are both pure local reads —
    neither constructs an Orchestrator, a browser client, or an executor.
    While a ready task exists OR the fingerprint is unchanged from the last
    check, nothing here so much as imports a network-capable object; an
    unchanged fingerprint with no ready task returns `False` having made
    ZERO Claude and ZERO ChatGPT calls.
    """
    if registry.next_ready() is not None:
        _start_new_session(config, store, registry)
        return True

    fingerprint = repo_fingerprint(Path.cwd())
    if fingerprint == _load_fingerprint(config):
        return False  # unchanged since the last audit — sleep, make no calls
    _save_fingerprint(config, fingerprint)
    # No registry passed, and it would change nothing if it were: a live pin
    # requires its task to be READY, and any READY task makes `next_ready()`
    # non-None, so this branch is only ever reached with no pin live.
    _start_new_session(config, store)
    return True


#: The audit kickoff's counterpart for a session opened while an operator's
#: URGENT pin is live. It exists because the audit kickoff is what the loop says
#: on EVERY new session, including the one a preemption just started: offered
#: the audit, a reviewer reasonably answers `audit`, and the urgent task waits
#: out a full executor round (measured 1282s) that the operator's request was
#: supposed to skip. `Orchestrator._refused_ahead_of_urgent` refuses that audit,
#: so this is also what keeps the refusal from being a wall — the session is
#: told what to send instead, in the same breath.
#:
#: It lives HERE rather than in `prompts.TEMPLATES` because `_start_new_session`
#: is its only caller and `prompts.py` was outside this task's approved paths.
#: A `PromptTemplate` all the same, so the strictness that makes that library
#: worth having still applies to this payload: an unknown or missing field
#: raises `TemplateError` instead of rendering a mangled prompt. The two checks
#: `test_prompts.py` runs over `TEMPLATES` (strict render, and no template
#: offering a retired decision) are run over this one in
#: `test_urgent_preemption.py`, so moving it out of that dict costs it no
#: coverage.
URGENT_KICKOFF = PromptTemplate(
    name="urgent_kickoff",
    body=(
        "New session, opened for an URGENT operator request. Task "
        "{task_id} was marked urgent ({urgent_reason}) and the round in "
        "flight was ended for it at a safe boundary — no review packet "
        "was interrupted and nothing was published.\n\n"
        "{task_id}: {task_title}\n\n{plan_note}\n\n"
        "Reply `implement` with task_id \"{task_id}\". No other "
        "`implement`/`revise`, and no fresh `audit`, will be accepted "
        "until this task has been dispatched — the review, approval and "
        "publication of its work are entirely unchanged."
    ),
)


def urgent_kickoff_payload(
    task_id: str, task_title: str, urgent_reason: str, has_plan: bool
) -> str:
    """Open a new session on the operator's urgent task rather than the audit.

    `has_plan` decides ONE sentence, and it is the sentence that makes the
    difference between this working and only looking like it works. Policy
    refuses an `implement` that carries no `decomposition` unless the task
    already holds an approved one (`policy._check_decomposition`), so a kickoff
    that asked flatly for `implement` would trade audit churn for DENIAL churn
    on exactly the tasks that have never been planned — and the denial budget
    ends a run. It is a parameter rather than a registry read so this stays a
    pure payload builder, like every helper in `prompts.py`: the caller has the
    registry in hand already.
    """
    plan_note = (
        "This task already has an approved decomposition on record, so the "
        "directive does not need to carry one — send `decomposition` only if "
        "you are deliberately reshaping the plan."
        if has_plan
        else (
            "This task has NO approved decomposition yet, so the directive must "
            "carry one: an `implement` without a plan is refused by policy."
        )
    )
    return URGENT_KICKOFF.render(
        task_id=task_id,
        task_title=task_title,
        urgent_reason=urgent_reason,
        plan_note=plan_note,
    )


def _start_new_session(
    config: AutoloopConfig, store: StateStore, registry: TaskRegistry | None = None
) -> None:
    """Open the next session, and decide what it OPENS ON.

    Ordinarily the audit kickoff, unchanged: a fresh session offers the
    repository audit and the reviewer picks from there.

    **With an operator's URGENT pin live it opens on that task instead**
    (`urgent_kickoff_payload` above), and that is the difference between the
    preemption reordering the queue and actually saving the operator anything.
    The session a preemption starts is a NEW session, so before this it opened
    on the audit kickoff like any other — the reviewer was invited to answer
    `audit`, `_dispatch_executor` let audits through, and the urgent task waited
    out a full executor round (measured 1282s) that the whole mechanism exists
    to skip. The dispatch gate now refuses that audit too, so opening on the
    audit kickoff would additionally spend denial budget on a question this
    loop asked itself.

    `registry` is optional so the no-pin callers read unchanged and a caller
    that has no registry to hand still gets the ordinary kickoff; a missing
    registry means "no pin", which is the fail-safe direction — one audit round
    of delay, never a session that cannot start.
    """
    state = LoopState.new(config.browser.conversation_url)
    urgent = registry.live_urgent_target() if registry is not None else None
    if urgent is None:
        state.outbox = TEMPLATES["audit_kickoff"].render()
    else:
        state.outbox = urgent_kickoff_payload(
            urgent.id, urgent.title, urgent.urgent_reason, bool(urgent.decomposition)
        )
    store.save(state)


def repo_fingerprint(repo_root: Path) -> str:
    """HEAD sha + a content digest of the dirty tree. Cheap enough to
    compute on every continuous-mode iteration without ever needing a Claude
    or ChatGPT call just to check "did anything change". Reuses
    `manifest.snapshot`, which already content-hashes every dirty path (not
    merely porcelain status), so the digest is sensitive to real edits, not
    to touched mtimes or `git status`'s ordering."""
    git = GitGateway(repo_root, PolicyEngine(PolicyConfig()))
    head = git.head_sha()
    dirty = manifest_snapshot(git)
    dirty_digest = hashlib.sha256(
        "\n".join(f"{path}={value}" for path, value in sorted(dirty.items())).encode("utf-8")
    ).hexdigest()
    return f"{head}:{dirty_digest}"


def _load_fingerprint(config: AutoloopConfig) -> str | None:
    path = config.continuous_fingerprint_file
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None  # corrupt cache: treat as "unknown", not fatal
    return data.get("fingerprint") if isinstance(data, dict) else None


def _save_fingerprint(config: AutoloopConfig, fingerprint: str) -> None:
    path = config.continuous_fingerprint_file
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"fingerprint": fingerprint}), encoding="utf-8")
    os.replace(tmp, path)


def _authorize_resubmit(state: LoopState) -> None:
    """Operator override for an ambiguous submission — the ONLY way a send is
    repeated. Reuses the same request id on purpose: if the earlier attempt did
    land, reconciliation detects it and nothing is duplicated."""
    ambiguous = Phase(state.phase) is Phase.SUBMISSION_UNCONFIRMED or (
        Phase(state.phase) is Phase.NEEDS_USER
        and state.resume_phase == Phase.SUBMISSION_UNCONFIRMED.value
    )
    if not ambiguous or state.pending_request is None:
        raise StateError(
            "--resubmit is only valid on an ambiguous submission "
            "(phase=submission_unconfirmed, or parked with it as the resumable "
            "phase). Use --retry for ordinary recoverable failures."
        )
    state.pending_request.send_attempted = False
    state.phase = Phase.SUBMITTING.value
    state.resume_phase = None
    state.question = None
    state.consecutive_failures = 0


def _open_blocker_count_display(config: AutoloopConfig) -> str:
    """Open-blocker count for `_summary` — a single corrupt blocker record
    must not take `status` (or every continuous-mode park message, which
    also calls `_summary`) down with it. Unlike `blockers`, whose entire job
    is surfacing that corruption loudly (`_cmd_blockers` lets it propagate),
    this is a status LINE among many; failing the whole summary over one bad
    file would hide the rest of the operator-relevant state too."""
    try:
        return str(len(BlockerStore(config.blockers_dir).open_blockers()))
    except StateCorruptError:
        return "? (unreadable — see `python -m autoloop blockers`)"


def _summary(config: AutoloopConfig, state: LoopState, registry: TaskRegistry) -> str:
    lines = [
        f"session      {state.session_id}",
        f"phase        {state.phase}"
        + (f" (resumable: {state.resume_phase})" if state.resume_phase else ""),
        f"iteration    {state.iteration}",
        f"conversation {state.conversation_url}",
        f"provider     {config.conversation.provider}",
        f"executor     {config.executor.kind}",
        f"roadmap      {registry.summary()}",
        f"paused flag  {'yes' if pause_requested(config) else 'no'}",
        f"open blockers{_open_blocker_count_display(config)}",
    ]
    if state.last_decision:
        lines.append(f"last decision {state.last_decision}")
    if state.current_task:
        lines.append(
            f"current task {state.current_task.get('task_id') or '(audit)'}: "
            f"{(state.current_task.get('title') or '')[:120]}"
        )
    # Produce-then-review record for whichever task/audit is currently
    # running that path (see `worktask.TaskExecution`) — the ONE dispatch
    # path since the S21 retirement, so this is the thing to look at for
    # "what is the loop actually doing to the repository right now."
    task_exec = state.task_execution
    if task_exec:
        candidate = task_exec.get("candidate_sha") or ""
        lines.append(f"worker task  {task_exec.get('task_id')}")
        lines.append(
            f"worker repo  branch={task_exec.get('task_branch')} "
            f"path={task_exec.get('worktree_path')}"
        )
        lines.append(f"base sha     {(task_exec.get('task_base_sha') or '')[:12] or '(none)'}")
        lines.append(f"candidate    {candidate[:12] if candidate else '(none)'}")
        lines.append(f"review round {task_exec.get('review_round')}")
        if task_exec.get("intended_remote_ref"):
            lines.append(
                f"publish dest {task_exec.get('intended_remote')}/"
                f"{task_exec.get('intended_remote_ref')}"
            )
    # Publisher target — never the raw url if it carries userinfo
    # credentials (`https://user:token@host/...`); `redact_url` strips that
    # unconditionally before anything reaches stdout.
    publisher_snapshot = read_publisher_url_snapshot(config.state_dir)
    if publisher_snapshot is not None:
        lines.append(f"publisher    remote=origin url={redact_url(publisher_snapshot)}")
    if state.reviewed_commit:
        lines.append(f"reviewed     {state.reviewed_commit[:12]}")
    if state.question:
        lines.append(f"question     {state.question}")
    if state.stop_reason:
        lines.append(f"stop reason  {state.stop_reason}")
    repeated = _repeated_stop_display(config)
    if repeated:
        lines.append(repeated)
    return "\n".join(lines)


def _repeated_stop_display(config: AutoloopConfig) -> str:
    """The `repeated stop` summary line, or `""` when there is nothing to say.

    Visibility for the counter `orchestrator._handle_contract_stop` keeps: the
    2026-08-20 livelock was caught only because a person noticed the phase had
    not changed, so a loop climbing toward `MAX_REPEATED_STOPS` should be
    readable in `status` BEFORE it has paid for the park.

    Silent — no line at all — when no stop has been counted yet, which is the
    ordinary state and keeps the summary byte-identical to what it was. Silent
    for an unreadable ledger too, deliberately unlike `_observe_contract_stop`,
    which parks on one: this is a status LINE among many and it is not the
    thing that enforces anything. The enforcement point is the one that must
    fail closed; failing the whole summary here would hide the rest of the
    operator-relevant state over a counter file. Same reasoning as
    `_open_blocker_count_display` beside it, and the same shape of catch —
    `OSError` included, because a path that is a directory raises that rather
    than `StateCorruptError`.
    """
    try:
        record = StopRepetitionStore(stop_repetition_file(config.state_dir)).load()
    except (StateCorruptError, OSError):
        return ""
    if record is None or record.count < 1:
        return ""
    # `repeat stops ` is 13 characters, the label width every other line in
    # `_summary` uses (`open blockers` is the same trick with no trailing space).
    return (
        f"repeat stops {record.count} consecutive stop(s) about one unchanged "
        f"situation (parks at {MAX_REPEATED_STOPS})"
    )


def _cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _, state = _load_state(config)
    _, registry = _load_tasks(config)
    lock_info = LoopLock(config.state_dir).read()
    if lock_info is not None:
        liveness = "LIVE" if LoopLock.is_live(lock_info) else "STALE (see `unlock`)"
        print(f"lock         {liveness}: {lock_info.describe()}")
    if state is None:
        print("no saved session")
        return 0
    print(_summary(config, state, registry))
    return 0


def _cmd_tasks(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _, registry = _load_tasks(config)
    tasks = registry.all_tasks()
    if not tasks:
        print("no tasks planned yet")
        return 0
    for task in tasks:
        state = registry.state_of(task.id).value
        deps = f" (after {', '.join(task.depends_on)})" if task.depends_on else ""
        print(f"[{state:11}] {task.id}: {task.title}{deps}")
    print(f"\n{registry.summary()}")
    return 0


def _cmd_next_task(args: argparse.Namespace) -> int:
    """Dry-run selection — read-only, no lock, never implements or commits.
    Prints exactly what continuous mode's selection policy would pick right
    now (`TaskRegistry.next_ready()`, unmodified — see `_select_and_kickoff`)."""
    config = load_config(args.config)
    _, registry = _load_tasks(config)
    task = registry.next_ready()
    if task is None:
        print("no ready task")
        return 0
    print(f"{task.id} — {task.title}")
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    """Per-stage timing from the transcript. Read-only, no lock — like
    `status`/`tasks`/`blockers`/`next-task`, and safe while the loop runs.

    Reads ONE file (`config.transcript_file`) and prints aggregates. It opens
    no state, no registry and no repository, so it cannot mutate anything and
    cannot be blocked by a live run; the reader is tolerant line by line
    (`transcript.read_records`) because the file it reads is being appended to
    while it reads it, and the last line can be half-written.

    Only AGGREGATES are printed. The transcript carries full review packets
    (`request_submitted.data.prompt`) and full reviewer responses
    (`response_received.data.raw`); this command must never grow a flag that
    prints a record body — see docs/SECURITY.md S36. The read is reduced to a
    `TranscriptProfile` — counts, flags and per-stage `Stats` of floats —
    BEFORE anything renders, so the layer that writes to stdout holds no
    record at all rather than holding one it is trusted not to print.

    `--transcript FILE` points the same reader at an ARCHIVED transcript — a
    rotated file, a copy taken off another machine, the 7,203-record history
    that motivated this. It widens which file is read and nothing else: the
    renderer still receives only that aggregate, so a file that is not a
    transcript profiles to "no usable records" rather than putting any of its
    content on stdout. Read with `getattr` so every caller that builds the
    namespace without the flag keeps working.
    """
    config = load_config(args.config)
    override = getattr(args, "transcript", None)
    path = Path(override) if override else config.transcript_file
    if not path.exists():
        print(f"no transcript at {path}")
        return 0
    read = read_records(path)
    if not read.records:
        print(f"transcript   {path}\nno usable records")
        return 0
    print(render_profile(path, build_profile(read)))
    return 0


def _age(created_at: str) -> str:
    """Human-readable elapsed time since `created_at` (an ISO-8601
    timestamp, always UTC — see `state.utcnow_iso`). Falls back to the raw
    string on anything unparseable rather than raising: this is a display
    helper for `blockers`, not a place where a malformed timestamp should
    take the whole command down."""
    try:
        then = datetime.fromisoformat(created_at)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - then
    except ValueError:
        return created_at
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _cmd_add_task(args: argparse.Namespace) -> int:
    """Submit a task request to the inbox. Deliberately takes NO lock and
    touches neither the repository nor the state dir, so it is safe to run at
    any moment — including while a write-capable agent is mid-run. The running
    loop merges it between steps; if no loop is running, the next one drains it
    on startup."""
    config = load_config(args.config)
    inbox = TaskInbox(inbox_dir_for(config.workers_root, config.state_dir))
    spec = {
        "id": args.id,
        "title": args.title,
        "description": args.description,
        "priority": args.priority,
    }
    if args.depends_on:
        spec["depends_on"] = list(args.depends_on)
    if args.approved_path:
        spec["approved_paths"] = list(args.approved_path)
    if args.validation_cwd:
        spec["validation_cwd"] = args.validation_cwd
    if args.validation:
        spec["validation"] = [v.split() for v in args.validation]
    try:
        path = inbox.submit(spec)
    except InboxError as exc:
        print(f"error: {exc}")
        return 1
    print(
        f"queued task '{args.id}' (priority {args.priority}) -> {path}\n"
        "The running loop picks it up between steps; the task graph is validated "
        "on merge, so a duplicate id or unknown dependency is reported there."
    )
    return 0


def _intake_dir(config: AutoloopConfig) -> Path:
    return intake_dir_for(config.workers_root, config.state_dir)


def _all_suggestions(config: AutoloopConfig) -> list:
    """Every citable suggestion, unfiltered and unlimited.

    Used only to resolve a KEY an operator typed back at us (`accept`,
    `decline`). The OFFER is `gather_suggestions`, which bounds and de-declines
    it; this is the lookup behind it, so a key that scrolled off the offer is
    still resolvable.
    """
    tasks_file = config.tasks_file
    try:
        registry_text = tasks_file.read_text(encoding="utf-8")
    except OSError:
        registry_text = ""
    tasks_data = None
    if registry_text:
        try:
            tasks_data = json.loads(registry_text)
        except ValueError:
            tasks_data = None
    return (
        audit_finding_suggestions(Path.cwd(), config.repo.audit_report_glob, registry_text)
        + ready_task_suggestions(tasks_data)
        + open_blocker_suggestions(config.blockers_dir)
    )


def _print_pass(result) -> None:
    print(f"draft: {result.path}")
    for question in result.added_questions:
        print(f"  + ? {question}")
    for line in result.added_evidence:
        print(f"  + evidence: {line}")
    if result.evidence_note:
        print(f"  evidence: {result.evidence_note}")
    if result.provider_note:
        print(f"  note: {result.provider_note}")
    for line in result.assumptions:
        print(f"  assumed: {line}")
    if result.blockers:
        print("  NOT a draft yet:")
        for line in result.blockers:
            print(f"    - {line}")
        print(
            "  Answer the `?!` questions in the file, then run `intake ask` "
            "again. Nothing has been queued."
        )
    else:
        print(
            "  READY — a `## Draft` section is in the file. Check the "
            "approved_paths (they are SUGGESTED, and authorize nothing until "
            "you submit), then `python -m autoloop intake submit --id "
            f"{result.path.stem}`."
        )


def _cmd_intake(args: argparse.Namespace) -> int:
    """Operator intake: a rough idea, interviewed into a DRAFT task.

    Every subcommand here is AUTHORING-TIME. Nothing dispatches, nothing
    writes the registry, and the only one that queues anything at all is
    `submit`, which goes through the same `TaskInbox` `add-task` uses.

    The three question-asking subcommands (`ask`, `suggest`, `plan`) refuse
    while a round is live — `ask_user` was retired for parking the loop on a
    question nobody was there to answer, and asking mid-round would rebuild it.
    `new`, `show`, `list` and `submit` are safe at any moment, exactly like
    `add-task`, because they touch nothing inside the checkout.
    """
    config = load_config(args.config)
    intake_dir = _intake_dir(config)
    repo = Path.cwd()
    action = args.intake_cmd
    try:
        return _run_intake(action, args, config, intake_dir, repo)
    except OSError as exc:
        # A drafts directory that is a file, a read-only volume, a full disk.
        # Reported as a refusal rather than a traceback: `main` catches
        # `IntakeError`, and every one of these is something the operator can
        # act on. Nothing here has queued anything by this point except a
        # `submit`, which reports its own partial state (`submit_draft`).
        raise IntakeError(f"the intake directory could not be used: {exc}") from exc


def _run_intake(action, args, config: AutoloopConfig, intake_dir: Path, repo: Path) -> int:
    """The subcommand bodies. Split out so `_cmd_intake` owns the one
    OSError-to-refusal boundary rather than repeating it per branch."""
    if action == "list":
        drafts = list_drafts(intake_dir)
        if not drafts:
            print(f"no drafts in {intake_dir}")
            return 0
        for path in drafts:
            draft = read_draft(path)
            blockers = draft_blockers(draft)
            state = "ready" if not blockers else f"{len(blockers)} open"
            print(f"{path.stem:24} {state:12} {draft.title[:60]}")
        return 0

    if action == "new":
        if args.file:
            path = create_draft_from_file(intake_dir, Path(args.file), args.id or "")
        else:
            if not args.id:
                raise IntakeError("--id is required when the idea is given as --text")
            path = create_draft(intake_dir, args.id, args.text or "")
        print(
            f"draft: {path}\n"
            "Nothing has been queued and no task exists. Run "
            f"`python -m autoloop intake ask --id {path.stem}` to be asked "
            "about it; delete the file to abandon it, which leaves nothing "
            "behind."
        )
        return 0

    if action == "show":
        path = draft_path(intake_dir, args.id)
        try:
            print(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise IntakeError(f"could not read {path}: {exc}") from exc
        return 0

    if action == "ask":
        refuse_if_round_running(config.state_dir, "the intake interview")
        result = interview_step(
            draft_path(intake_dir, args.id),
            ask=None if args.no_model else provider_asker(config),
            repo=repo,
        )
        _print_pass(result)
        return 0

    if action == "plan":
        refuse_if_round_running(config.state_dir, "intake planning")
        result = plan_step(
            draft_path(intake_dir, args.id), ask=provider_asker(config), repo=repo
        )
        print(f"draft: {result.path}")
        print(f"  one level, {len(result.tasks)} task(s): {', '.join(result.tasks)}")
        if result.note:
            print(f"  reason given: {result.note}")
        print(
            "  Nothing was queued. Deeper splits are not produced here: a task "
            "that turns out to be too large is split later by a reviewer that "
            "sees it refuse, on that task's own evidence."
        )
        return 0

    if action == "submit":
        path = draft_path(intake_dir, args.id)
        if args.dry_run:
            for spec in draft_specs(read_draft(path)):
                print(f"{spec['id']} (priority {spec['priority']})")
                print(f"  approved_paths: {', '.join(spec['approved_paths'])}")
                print(f"  description: {len(spec['description'])} chars")
            print("Nothing was queued — this was a dry run.")
            return 0
        inbox = TaskInbox(inbox_dir_for(config.workers_root, config.state_dir))
        filed = submit_draft(path, inbox)
        for task_id, queued in filed:
            print(f"queued task '{task_id}' -> {queued}")
        print(
            "The running loop picks these up between steps; the task graph is "
            "validated on merge, so a duplicate id or unknown dependency is "
            f"reported there. The draft is still at {path}."
        )
        return 0

    if action == "suggest":
        refuse_if_round_running(config.state_dir, "offering work to do")
        offer = gather_suggestions(
            repo,
            report_glob=config.repo.audit_report_glob,
            tasks_file=config.tasks_file,
            blockers_dir=config.blockers_dir,
            intake_dir=intake_dir,
            limit=args.limit,
        )
        for line in offer.sources:
            print(f"read: {line}")
        if offer.declined:
            print(f"({offer.declined} previously declined, evidence unchanged)")
        if not offer.suggestions:
            print(
                "nothing to offer. That is a statement about the sources listed "
                "above and nothing else — an empty offer is not a claim that "
                "there is no work."
            )
            return 0
        print("")
        for item in offer.suggestions:
            print(f"{item.key}\n  {item.headline}\n  cite: {item.cite}")
        print(
            "\nPick one: `intake accept <key> --id <slug>`. Declining is free: "
            "`intake decline <key>` — it is not offered again unless its "
            "evidence changes."
        )
        return 0

    if action in ("accept", "decline"):
        found = [item for item in _all_suggestions(config) if item.key == args.key]
        if not found:
            raise IntakeError(
                f"no suggestion with key {args.key!r} — run `intake suggest` for "
                "the current offer"
            )
        item = found[0]
        if action == "decline":
            record_decline(intake_dir, item.key, item.fingerprint)
            print(
                f"declined {item.key}. It will not be offered again unless its "
                "evidence changes."
            )
            return 0
        path = draft_from_suggestion(intake_dir, item, args.id or "")
        print(
            f"draft: {path}\n"
            f"Seeded from {item.cite}. Nothing has been queued. Rewrite the "
            f"idea in your own words, then `intake ask --id {path.stem}`."
        )
        return 0

    raise IntakeError(f"unknown intake action {action!r}")


def _cmd_urgent(args: argparse.Namespace) -> int:
    """Make an existing task the loop's URGENT TARGET: the next task
    dispatched, ahead of whatever round is in flight.

    Like `add-task`, it takes NO lock and writes only to the inbox — outside
    the checkout, so it is safe at any instant, including while a write-capable
    agent is mid-run. The running loop drains it between steps, and acts on it
    only at a phase boundary it already treats as safe
    (`orchestrator._preempt_for_urgent`): a request that arrives while a review
    packet is outstanding waits for `ready` rather than stranding it.

    **It is checked before it is queued, through the registry's own
    `request_urgent`.** A task that is blocked, quarantined, retired, already
    completed, already in flight, unscoped, or that arrives while another
    task's pin is still live is refused HERE, with the registry's own wording,
    and nothing is queued. That is not a second rule set: it is a dry run of
    the one authority — the same method the drain calls — against the registry
    as it stands, deliberately not saved. The drain re-checks authoritatively,
    because the graph can move between this call and the next drain; what this
    buys is that the ordinary refusals reach the operator at the prompt instead
    of in a transcript entry they have to go looking for.

    It does NOT weaken the review gate in any way: the urgent task is
    dispatched through the identical implement/review/approve/publish path as
    any other, and the round it displaces keeps its committed candidate in a
    quarantined worker repo.
    """
    config = load_config(args.config)
    _, registry = _load_tasks(config)
    try:
        # Dry run against a registry nothing will save. The refusal text and
        # its stable `code` come from the one implementation, so an operator
        # cannot be told two different things about the same request.
        registry.request_urgent(args.task_id, args.reason)
    except TaskGraphError as exc:
        print(f"error: {exc}")
        return 1
    inbox = TaskInbox(inbox_dir_for(config.workers_root, config.state_dir))
    try:
        path = inbox.submit(
            {"kind": KIND_URGENT, "id": args.task_id, "reason": args.reason}
        )
    except InboxError as exc:  # pragma: no cover - shape is built here
        print(f"error: {exc}")
        return 1
    print(
        f"queued URGENT request for '{args.task_id}' -> {path}\n"
        "The running loop applies it between steps and preempts at its next "
        "SAFE boundary (phase=ready with no packet outstanding) — it never "
        "interrupts a review that is in flight. The displaced task returns to "
        "pending and its worker repo is quarantined, not deleted; review, "
        "approval and publication are unchanged.\n"
        "One preemption at a time: a second urgent request is refused while "
        "this one is still waiting to be dispatched."
    )
    return 0


def _cmd_record_shipped(args: argparse.Namespace) -> int:
    """Record that a task's work is already in the base under OTHER commits.

    Like `add-task` and `urgent`, it takes NO lock and writes only to the inbox
    — outside the checkout, so it is safe at any instant, including while a
    write-capable agent is mid-run. That is the requirement rather than a
    convenience: writing under `.autoloop/` needs the loop stopped (the escape
    detector snapshots that directory), and the whole point of this record is
    that it can be made about a live roadmap.

    **The evidence is CHECKED before it is queued, and the check is git's.**
    Every `--commit` is resolved to a full sha in `--repo` and then asked
    `merge-base --is-ancestor` against `--base`. Three answers, three outcomes,
    and the third is the one that matters:

      * an ancestor → this commit may be recorded;
      * provably not an ancestor → REFUSED, naming it. An operator asserting
        that work shipped under a commit the base does not contain is exactly
        the unsupported assertion this record replaces;
      * git could not answer (a shallow clone, an object nobody fetched, an
        unreadable repository) → ALSO REFUSED. "Could not look" is not
        "verified", and a command that queued on an indeterminate check would
        be a verification step that switches itself off precisely when it
        cannot see.

    That check is a GATE, not a guarantee, and the record does not rest on it:
    the base moves, so `shipped-report` and the dashboard re-ask the same
    question of the same shas on every read. A record whose commits stop being
    ancestors reads as a disagreement from that moment on. This command exists
    so the ordinary mistake is caught at the prompt rather than surfacing as a
    disagreement an hour later.

    The registry half is dry-run first, exactly as `urgent` does it — through
    `record_shipped_elsewhere`, the same method the drain calls, against a
    registry nothing will save — so a task that is in progress, completed,
    retired or unknown is refused HERE in the registry's own words. The drain
    re-checks authoritatively, because the graph can move in between.

    It records; it never converts. Nothing here inspects other tasks, guesses
    which commits might carry the work, or rewrites a status because a
    heuristic matched — the commits come from the operator, and git's answer
    about them is what decides.
    """
    from . import dashboard
    from .inbox import KIND_SHIPPED_ELSEWHERE

    config = load_config(args.config)
    _, registry = _load_tasks(config)
    repo = args.repo or Path.cwd()
    head = dashboard.resolve_commit(repo, args.base)
    if not head:
        print(
            f"error: cannot resolve {args.base!r} in {repo} — the base head is "
            "what the evidence is checked against, so nothing is queued rather "
            "than queueing a claim nobody could check"
        )
        return 1
    resolved: list[str] = []
    for rev in args.commit:
        sha = dashboard.resolve_commit(repo, rev)
        if not sha:
            print(
                f"error: {rev!r} names no commit in {repo} — an unresolvable "
                "revision cannot be evidence"
            )
            return 1
        verdict = dashboard.is_ancestor(repo, sha, head)
        if verdict == "no":
            print(
                f"error: {sha[:12]} ({rev}) is NOT an ancestor of {head[:12]} — "
                "recording it would claim work is in this base that git says is "
                "not. Nothing was queued"
            )
            return 1
        if verdict != "yes":
            print(
                f"error: git could not decide whether {sha[:12]} ({rev}) is an "
                f"ancestor of {head[:12]} — a shallow clone, an object this "
                "checkout has never fetched, or a repository it cannot read. "
                "'Could not look' is not 'verified', so nothing was queued"
            )
            return 1
        if sha in resolved:
            print(f"error: {rev!r} resolves to {sha[:12]}, which is already listed")
            return 1
        resolved.append(sha)
    try:
        # Dry run against a registry nothing will save, exactly as `urgent`
        # does it: the refusal text and its stable `code` come from the one
        # implementation, so an operator cannot be told two different things
        # about the same request.
        registry.record_shipped_elsewhere(args.task_id, resolved, args.note)
    except TaskGraphError as exc:
        print(f"error: {exc}")
        return 1
    inbox = TaskInbox(inbox_dir_for(config.workers_root, config.state_dir))
    try:
        path = inbox.submit({
            "kind": KIND_SHIPPED_ELSEWHERE,
            "id": args.task_id,
            KIND_SHIPPED_ELSEWHERE: {"commits": resolved, "note": args.note},
        })
    except InboxError as exc:  # pragma: no cover - shape is built here
        print(f"error: {exc}")
        return 1
    listed = ", ".join(sha[:12] for sha in resolved)
    print(
        f"queued shipped-elsewhere record for '{args.task_id}' -> {path}\n"
        f"  evidence: {listed} — each verified as an ancestor of {head[:12]} "
        "just now\n"
        f"  note: {args.note}\n"
        "The running loop applies it between steps. Recording it satisfies the "
        "tasks that depend on this one, keeps it out of the scheduler, and does "
        "NOT put it in the merge sweep — it never had a branch.\n"
        "The evidence is re-checked on every read: if these commits stop being "
        "ancestors of the base head, the record reads as a disagreement rather "
        "than as done (`python -m autoloop shipped-report`)."
    )
    return 0


def _cmd_drain_inbox(args: argparse.Namespace) -> int:
    """Merge queued task requests into the registry WITHOUT stepping the phase
    machine.

    `run` drains between steps, but that also dispatches whatever the current
    phase is — so with a review packet waiting in the outbox there was no way
    to apply queued requests without also sending it. This separates the two.

    Takes the single-instance lock, because it writes `tasks.json`: the loop
    must stay the only writer, and "the loop" here means "whoever holds the
    lock". Refuses rather than racing a live run.
    """
    config = load_config(args.config)
    with LoopLock(config.state_dir):
        task_store, registry = _load_tasks(config)
        inbox = TaskInbox(inbox_dir_for(config.workers_root, config.state_dir))
        specs, problems = inbox.drain()
        for problem in problems:
            print(f"  unreadable: {problem}")
        if not specs:
            print("no queued task requests")
            return 0
        added, reprioritised, refused = apply_requests(registry, specs)
        if added or reprioritised:
            task_store.save(registry)
        for line in added:
            print(f"  added        {line}")
        for line in reprioritised:
            print(f"  reprioritised {line}")
        for line in refused:
            print(f"  REFUSED      {line}")
        print(
            f"{len(added)} added, {len(reprioritised)} reprioritised, "
            f"{len(refused)} refused"
        )
    return 2 if refused else 0


def _cmd_blockers(args: argparse.Namespace) -> int:
    """Read-only, no lock — like `status`/`tasks`/`doctor`/`next-task`.
    Lists open blockers by default; `--all` also shows resolved ones."""
    config = load_config(args.config)
    store = BlockerStore(config.blockers_dir)
    # The default (open-only) listing is a TRIAGE view — the operator reads it
    # to decide what to deal with first — so it uses the one primary ordering
    # (`blockers.primary_sort_key`: severity, then recency, then id) rather
    # than the directory order that made a recovery script pick a task_fatal
    # record over a loop_fatal one on 2026-08-21. `--all` stays chronological:
    # it includes resolved records and is a history, where ranking a closed
    # loop_fatal above an open task_fatal would say something untrue.
    blockers = (
        sorted(store.all_blockers(), key=lambda b: b.created_at)
        if args.all
        else store.open_blockers_by_severity()
    )
    if not blockers:
        print("no blockers recorded" if args.all else "no open blockers")
        return 0
    for b in blockers:
        status = "open" if b.resolved_at is None else f"resolved ({b.resolved_at})"
        print(f"[{status}] {b.id}  task={b.task_id}  kind={b.kind}  code={b.code}  age={_age(b.created_at)}")
        print(f"    {b.question}")
        if b.answer is not None:
            print(f"    answer: {b.answer}")
    return 0


#: Blocker codes whose condition lives in the ENVIRONMENT, not in an answer.
#: Text alone must never clear these: the operator saying "fixed it" is not
#: evidence that it is fixed, and resolving on a promise would send the loop
#: straight back into the same wall — this time with the blocker marked
#: resolved, so the queue would understate what is actually wrong.
#: Each maps to a precondition that is RE-CHECKED at answer time.
def _precondition_browser(config) -> str:
    from .doctor import DoctorProbes, run_doctor
    results = run_doctor(config, Path.cwd(), probes=DoctorProbes())
    bad = [r for r in results if r.status != "ok" and r.name in
           ("cdp", "playwright", "provider", "conversation_url", "browser_live")]
    if bad:
        return "browser/login checks still failing: " + ", ".join(
            f"{r.name} ({r.detail})" for r in bad)
    return ""


def _precondition_publisher_url(config) -> str:
    from .publisher import publisher_url_snapshot_path, read_publisher_url_snapshot, redact_url
    from .git_gateway import GitGateway
    from .policy import PolicyEngine
    snap = read_publisher_url_snapshot(config.state_dir)
    live = GitGateway(Path.cwd(), PolicyEngine(config.policy)).config_get("remote.origin.url")
    if not snap:
        return f"no publisher url snapshot at {publisher_url_snapshot_path(config.state_dir)}"
    if snap != live:
        return (
            f"publisher snapshot ({redact_url(snap)}) still differs from the "
            f"configured remote ({redact_url(live)}) — run "
            "`python -m autoloop reprovision-publisher --confirm` first"
        )
    return ""



def _precondition_protected(config) -> str:
    return (
        "a protected destination cannot be cleared by an answer — the target "
        "must be corrected to a non-protected branch, or protected_branches "
        f"changed deliberately (currently {list(config.policy.protected_branches)})"
    )


def _precondition_worker_environment_drift(config) -> str:
    """Dedicated recheck for `worker_environment_drift` — previously
    mismapped to `_precondition_browser` (Autoloop M1 finding #7), whose
    doctor probes (cdp/playwright/provider/conversation_url/browser_live)
    never look at git hooks or worker isolation at all, so ANY answer text
    resolved this blocker regardless of whether the environment it describes
    was still broken. Reuses the SAME primitives `doctor.py`'s
    `worker_isolation` check and `orchestrator.py`'s own environment-drift
    detection are built on (`worker_env.validate_workers_root` /
    `verify_worker_isolation`) — a throwaway probe worker repo is created
    and verified exactly like `doctor`'s check 6, proving the
    WorkerRepoManager pipeline itself is currently isolated, not just that
    the code that builds it reads correctly."""
    root_violations = validate_workers_root(config.workers_root, Path.cwd(), config.state_dir)
    if root_violations:
        return "workers_root is not safe to use: " + "; ".join(root_violations)
    probe_id = "precondition-drift-probe"
    worker_repos = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
    worker_repos.remove(probe_id)  # clear any stale probe from a crashed prior check
    try:
        policy = PolicyEngine(config.policy)
        git = GitGateway(Path.cwd(), policy)
        worker = worker_repos.create(probe_id, git.repo_root, git.head_sha())
        try:
            probe_git = worker.gateway(PolicyEngine(config.policy))
            violations = verify_worker_isolation(
                probe_git, expected_hooks_dir=worker_repos.hooks_dir_for(probe_id)
            )
            if violations:
                return "worker environment still not isolated: " + "; ".join(violations)
        finally:
            worker_repos.remove(probe_id)
    except AutoloopError as exc:
        return f"could not verify the worker environment: {exc}"
    return ""


def _precondition_checkout_clean(config) -> str:
    """Dedicated recheck for `primary_checkout_dirty` — re-runs the exact
    `is_dirty()` check the orchestrator itself used to park in the first
    place, so an operator's answer text alone cannot clear a checkout that
    is still genuinely dirty.

    Deliberately NOT reused for `checkout_escape_detected` (an earlier
    version of this fix did, and that was wrong — see
    `_precondition_checkout_escape_detected` below for why `is_dirty()` is
    not a sufficient recheck for a detected escape)."""
    from .git_gateway import GitGateway
    from .policy import PolicyEngine

    git = GitGateway(Path.cwd(), PolicyEngine(config.policy))
    if git.is_dirty():
        return "the primary checkout still has staged or unstaged changes: " + ", ".join(
            git.dirty_files()
        )
    return ""


def _precondition_checkout_escape_detected(config) -> str:
    """`checkout_escape_detected` (a write-capable agent wrote something
    outside its own worker repo — `escape_detector.py`, Autoloop M1 finding
    #2) can NEVER be cleared by an `answer`, unconditionally — mirrors
    `_precondition_protected`'s unconditional-refusal shape, for the same
    reason: some conditions are not "recheck and see", they need a human to
    actually look.

    Why NOT a dirty-checkout recheck (an earlier version of this fix used
    one, shared with `primary_checkout_dirty`, and it was wrong): the escape
    detector's snapshot deliberately covers a SUPERSET of what `GitGateway.
    is_dirty()` can ever see — tracked *and* untracked *and* IGNORED paths
    (`escape_detector.enumerate_checkout_paths`; see that module's docstring
    and `test_escape_detector_detects_ignored_content_change`, which proves
    `git.dirty_files() == []` even while the detector correctly flags the
    tampering). Autoloop's own state directory is gitignored in production,
    so the canonical scenario this detector exists to catch — an agent
    forging `.autoloop/state.json` or a blocker record to cover its tracks —
    is EXACTLY a case where the working tree stays clean throughout. A
    dirty-checkout recheck would clear that blocker on nothing. The loop is
    already stopped (`loop_fatal`) either way, so refusing the text path
    unconditionally costs nothing and is the honest posture for a
    security-shaped detection: the reported paths need a human to actually
    read them, not an automated recheck to wave through."""
    return (
        "a detected filesystem escape cannot be cleared by an answer — the "
        "paths `escape_detector` reported were left exactly as the agent "
        "wrote them (nothing here reverts anything) and must be inspected "
        "directly. Once satisfied, reset or archive this session rather "
        "than answering it — see docs/SECURITY.md S24."
    )


#: code -> precondition. Anything NOT listed resolves by answer text alone
#: (every task_fatal code), which is the intended behaviour.
#: KEEP EVERY KEY HERE MATCHED TO A REAL, EMITTED `code=` LITERAL —
#: `test_m1_hardening.py::test_every_precondition_key_matches_a_real_emitted_code`
#: AST-walks `orchestrator.py` for every `code=` argument passed to
#: `_to_needs_user` (including inside a ternary, like the one below) and
#: fails if a key here has no match, so this cannot silently drift back to a
#: dead mapping the way `git_failure_budget` (real code:
#: `git_failure_budget_exhausted`) and `push_refused_protected` (previously
#: never emitted at all) did.
_RESOLUTION_PRECONDITIONS = {
    "login_expired": _precondition_browser,
    "submission_ambiguous": _precondition_browser,
    "git_failure_budget_exhausted": _precondition_browser,
    "publisher_url_drift": _precondition_publisher_url,
    # `changeset_publisher_required` (`Orchestrator._dispatch_changeset_push`)
    # fires when `self._publisher is None` — unreachable through
    # `cli._build_orchestrator`, which always provisions one, but a genuinely
    # environmental condition wherever it IS reachable (a hand-built
    # Orchestrator). Reuses `_precondition_publisher_url` rather than a new
    # function: that check already refuses to clear on text alone unless a
    # real publisher url snapshot exists and matches the live remote — the
    # closest existing recheck to "a publisher is actually configured".
    "changeset_publisher_required": _precondition_publisher_url,
    # `browser_unattachable` (brw-11): the CDP endpoint answers but there is no
    # page to attach to, so the loop had no browser at all. Unlike its sibling
    # `rate_limited` — which deliberately has NO entry, because the only recheck
    # that could establish whether a server-side limit still holds is another
    # request against it — this one is recheckable locally and for free, and an
    # answer given while the window is still closed just re-parks. Reuses the
    # browser check: its `cdp`/`browser_live` probes are exactly "can something
    # attach to this endpoint".
    "browser_unattachable": _precondition_browser,
    "worker_environment_drift": _precondition_worker_environment_drift,
    "worker_isolation_violation": _precondition_worker_environment_drift,
    "push_refused_protected": _precondition_protected,
    "primary_checkout_dirty": _precondition_checkout_clean,
    "checkout_escape_detected": _precondition_checkout_escape_detected,
}


def _cmd_answer(args: argparse.Namespace) -> int:
    """Resolve a blocker with the operator's answer and, when that was the LAST
    open blocker for the task it quarantined, unblock the task so it becomes
    READY again. Takes the lock (like `run`/`reset`) — it mutates `tasks.json`,
    and must not race a live `run --continuous`.

    "Last" is the part that used to be missing on both sides. A task can hold
    more than one open blocker, and the unblock was unconditional, so the first
    answer requeued a task the second question was still about; conversely the
    command could close the last record naming a task and leave that task
    `blocked` anyway (a `loop_fatal` record, or an unblock the registry refused
    because the task was not blocked YET — port-01, 2026-08-19). Both halves are
    now decided from the same reading of the store, and ONE reconciliation
    (`_reconcile_unblocked_tasks`) performs every release, so no OTHER task is
    left in that state either.

    **The release is the sweep's, not a second targeted unblock** (blk-01,
    review round 3). Doing both meant two writes to `tasks.json` from one
    command and two different rules for who may be released — the targeted
    `registry.unblock` accepts any `blocked` task, including an OPERATOR hold,
    which is exactly what `blocker_derived_blocked` exists to exclude. The
    messages below are derived from what the reconciliation actually did, so
    what this command prints and what it wrote cannot disagree.

    **Fails closed** (`_requeue_after_close`). If the task graph cannot be read,
    reconciled or saved, the resolution is written back OPEN and the command
    exits 1 without reporting a resolution — resolving a blocker and requeueing
    what it was holding is ONE operation or it is the bug this task exists to
    end. Nothing is printed as done before it is done, including the fault-budget
    reset. The one branch where the answer DOES stand is the restore itself
    failing, and it is reported as that ("remains CLOSED (resolved) on disk"),
    never as "was NOT resolved" — an operator who believed that line would answer
    the same blocker twice."""
    config = load_config(args.config)
    with LoopLock(config.state_dir):
        blocker_store = BlockerStore(config.blockers_dir)
        pending = blocker_store.load(args.blocker_id)
        if pending is not None and pending.resolved_at is None:
            check = _RESOLUTION_PRECONDITIONS.get(pending.code)
            if check is not None:
                problem = check(config)
                if problem:
                    print(
                        f"blocker {pending.id} ({pending.code}) NOT resolved — its "
                        f"condition is environmental and is still present:\n  {problem}\n"
                        "Fix the condition, then run `answer` again. An answer alone "
                        "cannot clear it."
                    )
                    return 1
        # The snapshot `_reopen_blocker` writes back if the requeue cannot be
        # completed. Copied rather than aliased: `pending` is already a distinct
        # instance from the one `resolve` mutates (each `load` decodes afresh),
        # and a copy keeps that true no matter what the store does later.
        before = dataclasses.replace(pending) if pending is not None else None
        blocker = blocker_store.resolve(args.blocker_id, args.text)  # raises on unknown/resolved
        released, registry, outcome = _requeue_after_close(config, blocker_store, before)
        if outcome == _REQUEUE_REOPENED:
            print(
                f"blocker {blocker.id} was NOT resolved — its answer was not "
                "recorded and nothing was requeued."
            )
            return 1
        if outcome != _REQUEUE_OK:
            # The restore itself failed, so the resolution really is on disk.
            # Saying "not resolved" here would be the false line the rest of
            # this command is written to avoid.
            print(
                f"blocker {blocker.id} remains CLOSED (resolved) on disk and "
                "nothing was requeued — see the error above for what to do."
            )
            return 1
        print(f"blocker {blocker.id} resolved.")
        if blocker.kind != "task_fatal" or blocker.task_id == NO_TASK:
            print("(not tied to a quarantined task — nothing else to do.)")
        else:
            # After the requeue landed, never before it. `registry.unblock`
            # refuses a task that is not `blocked`, and plain `run` (unlike
            # `--continuous`) parks task_fatal without ever going through
            # `cli._handle_parked_task` — so a task_fatal answer routinely
            # requeues nothing and the reset still has to happen, which is the
            # case it exists for. What it must NOT do is happen on a command
            # that failed closed: a refilled budget with the blocker written
            # back open is the hand-repair budget-01 removed, reintroduced.
            _clear_fault_budget_on_answer(config, blocker)
            if blocker.task_id in {task_id for task_id, _ in released}:
                print(f"task {blocker.task_id} is ready again.")
            elif blocker.task_id in blocker_store.open_task_ids():
                # A task can hold more than one open blocker — `record` mints a
                # separate one per (task, code, phase), so two distinct failures
                # on one task are two questions. Releasing it on the FIRST answer
                # put it back in `next_ready()` with the second still unanswered.
                print(
                    f"task {blocker.task_id} stays blocked — another blocker is "
                    "still open for it (`python -m autoloop blockers`)."
                )
            else:
                _print_answer_no_requeue(registry, blocker.task_id)
        # Every OTHER task the same reconciliation released. The answered task
        # has its own line above; this is how an operator learns that closing
        # one record also cleared a quarantine somewhere else in the graph.
        _print_auto_unblocked([r for r in released if r[0] != blocker.task_id])
    return 0


def _print_answer_no_requeue(registry: TaskRegistry | None, task_id: str) -> None:
    """Say why an answered task was not returned to the queue, when nothing open
    names it any more.

    Three shapes reach here and they want different words. The task may not be
    in the registry at all; it may not be `blocked` (a task_fatal park under
    plain `run` leaves it `in_progress`, and a retired task is not quarantined
    either — `task_not_blocked` / `task_retired`, the codes this command has
    always printed for that shape); or it may be blocked as an OPERATOR hold,
    which the reconciliation deliberately never touches.

    Asked through `TaskRegistry.unblock_obstacle`, which reports without
    transitioning. Calling `unblock` to find out would answer the question by
    releasing the operator's hold.
    """
    if registry is None or not registry.has(task_id):
        print(
            f"task {task_id!r} could not be unblocked (no such task in the "
            "registry) — the blocker itself is still resolved."
        )
        return
    obstacle = registry.unblock_obstacle(task_id)
    if obstacle is None:
        print(
            f"task {task_id} stays blocked — it is an operator hold, which an "
            "answer to a blocker does not clear."
        )
        return
    print(
        f"task {task_id!r} could not be unblocked ({obstacle}) — the "
        "blocker itself is still resolved."
    )


def _clear_fault_budget_on_answer(config: AutoloopConfig, blocker: Blocker) -> None:
    """Reset the FAULT attempt budget when an operator answers the park it
    caused, and only then.

    Without this, answering a `fault_attempt_ceiling` blocker unblocks the task
    and achieves nothing: `fault_attempt_count` is still at the cap, so the very
    next dispatch parks on the identical wall. That is precisely the mechanical
    hand-repair budget-01 exists to remove — an operator editing the execution
    record to put a counter back, the same edit every time.

    Deliberately narrow in three ways:

    * only for `code == "fault_attempt_ceiling"`. Answering an
      `attempt_count_ceiling` does NOT touch `attempt_count`: that budget bounds
      the task's own unproductive churn, and silently refilling it would hand
      a task five more rounds of the same failure on a keystroke.
    * only the `fault_attempt_count` COUNTER. `candidate_sha`, `review_round`,
      `last_revise_feedback` and every `attempt_ledger` entry are preserved, so
      the history of which rounds were faults survives the reset — this grants
      a fresh allowance, it does not erase what was spent. Nothing is appended
      to the ledger either: entries are per-dispatch, and an operator decision
      is not a dispatch. The resolved `Blocker` record is where that decision
      is written down.
    * best-effort. An unreadable or missing execution record must not turn a
      successful `answer` into a failure; the blocker is already resolved and
      the task already unblocked by the time this runs.
    """
    if blocker.code != "fault_attempt_ceiling":
        return
    try:
        store = TaskExecutionStore(config.executions_dir)
        execution = store.load(blocker.task_id)
        if execution is None or not execution.fault_attempt_count:
            return
        cleared = execution.fault_attempt_count
        execution.fault_attempt_count = 0
        store.save(execution)
        print(
            f"fault attempt budget for {blocker.task_id} reset "
            f"({cleared} -> 0); its own attempt budget "
            f"({execution.attempt_count}) is unchanged."
        )
    except (StateError, OSError) as exc:
        print(f"(could not reset the fault attempt budget: {exc})")


def _cmd_reprovision_publisher(args: argparse.Namespace) -> int:
    """The ONLY way the publisher's remote-url snapshot changes after its
    first write (`publisher.reprovision_publisher`) — an explicit, confirmed
    operator action. Nothing in the directive-dispatch path can reach this;
    it is not wired into `Orchestrator`/`authorize_directive` at all."""
    config = load_config(args.config)
    if not args.confirm:
        print(
            "reprovision-publisher re-snapshots the publisher's remote url "
            "from the main checkout's CURRENT configuration. Pass --confirm "
            "to do it — this is the ONLY way the snapshot changes."
        )
        return 1
    with LoopLock(config.state_dir):
        git = GitGateway(Path.cwd(), PolicyEngine(config.policy))
        url = _reprovision_publisher_snapshot(config.state_dir, git, confirm=True)
    print(f"publisher url snapshot updated: {redact_url(url)}")
    return 0


def _cmd_review_changeset(args: argparse.Namespace) -> int:
    """Queue an operator-authored changeset (already committed directly on
    this checkout's branch — never produced by this loop's own executor) for
    ChatGPT review, bound to an exact base/candidate sha pair. A later
    `run` sends the queued packet; a stamped `push` approval that echoes it
    publishes `--candidate` (and only `--candidate`) through the Publisher —
    see `changeset_review.py` and `Orchestrator._dispatch_changeset_push`.

    Refuses (via `build_changeset_binding`, before any session is touched)
    if either sha is not literal 40-hex, does not resolve to a commit,
    `--candidate` is not a descendant of `--base`, or the checked-out branch
    is protected. Requires no EXISTING session — exactly like `run
    --kickoff` — so this never silently merges into or clobbers unrelated
    in-flight state; `reset` (or resolve the existing session) first.
    """
    config = load_config(args.config)
    with LoopLock(config.state_dir):
        store, state = _load_state(config)
        if state is not None:
            raise StateError(
                "a session already exists — resolve it (`run --retry`/"
                "`--answer`) or `reset` it before queuing a changeset review"
            )
        policy = PolicyEngine(config.policy)
        git = GitGateway(Path.cwd(), policy)
        binding = build_changeset_binding(git, policy, args.base, args.candidate)
        body = Path(args.packet).read_text(encoding="utf-8") if args.packet else None
        packet_text = build_changeset_packet(git, binding, body=body)
        state = LoopState.new(config.browser.conversation_url)
        state.changeset = dataclasses.asdict(binding)
        state.outbox = TEMPLATES["changeset_review"].render(
            branch=binding.branch, dest_ref=binding.dest_ref, packet=packet_text
        )
        store.save(state)
    print(
        f"changeset review queued: {binding.candidate_sha[:12]} -> "
        f"{binding.dest_ref}. Run `python -m autoloop run` to send it."
    )
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    results = run_doctor(config, Path.cwd(), probes=DoctorProbes())
    width = max(len(r.name) for r in results)
    for result in results:
        print(f"[{result.status:>4}] {result.name:<{width}}  {result.detail}")
    code = exit_code(results)
    print("\ndoctor:", "all checks passed" if code == 0 else "FAILURES above")
    return code


#: What `smoke-browser` prints now, and the whole of what it does.
#:
#: One string rather than an f-string built at call time, because there is
#: nothing to interpolate: the command reads no config, so it knows no state
#: dir, no provider name and no diagnostics path. That is the point — see
#: `_cmd_smoke_browser`.
SMOKE_BROWSER_RETIRED = (
    "smoke-browser: RETIRED — this command existed to prove the BROWSER "
    "transport before a real run needed it, and no browser-backed conversation "
    "provider is registered any more (brw-16, 2026-08-25). Nothing ran: no "
    "config was read, no provider was built, the loop lock was not taken and "
    "the smoke state under `<state_dir>/smoke/` was not touched. For preflight "
    "checks against the transport the loop DOES use, run `python -m autoloop "
    "doctor`, which reaches the configured provider without submitting "
    "anything."
)


def _cmd_smoke_browser(args: argparse.Namespace) -> int:
    """RETIRED since brw-16 (2026-08-25). Prints why, exits 2, does nothing else.

    It used to default to `browser_chatgpt` regardless of
    `conversation.provider` — the browser was the fallback, so it was the seat
    most worth proving before it was needed — archive the previous smoke state
    under `.autoloop/smoke/`, take the loop lock, and drive one real round-trip
    (request id, CONTEXT stamp, parser, transcript) to a PASS/FAIL verdict.
    There is no browser-backed provider to build any more, so the seat it
    smoked does not exist.

    REFUSES PLAINLY, and each clause of that is a failure mode ruled out:

    * No config is loaded — so the refusal is the same on a missing, malformed
      or unreadable `--config`, and cannot fail with a `ConfigError` about a
      file it had no reason to open.
    * No provider is constructed, so nothing dials a transport.
    * The loop lock is NOT taken, so this can never block a live run, and
      `.autoloop/smoke/` is not archived, written or read.

    NOT repurposed into a generic smoke of `conversation.provider`, which is
    the shape a previous round shipped: that is a different command wearing
    this one's name, it would report PASS about a transport nobody asked it
    for, and it turns a browser-named command into something an operator's
    runbook silently mis-describes. If the loop wants one round-trip through
    the configured provider, that is a new command with its own name and its
    own review — not a rename by silence. Kept registered rather than deleted
    so a typed-from-memory invocation gets this sentence instead of an argparse
    usage error, and `args` is ignored for the same reason.
    """
    print(SMOKE_BROWSER_RETIRED)
    return 2


def _cmd_unlock(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    lock = LoopLock(config.state_dir)
    info = lock.break_stale()  # raises for live locks / missing file
    print(f"stale lock removed ({info.describe()})")
    return 0


#: What `start` may repair on its own, and what it must only report.
#:
#: The split is not conservatism for its own sake. Everything on the safe side
#: is decidable from evidence the machine already has — a lock whose owner is
#: provably dead, a CDP port that does not answer. Everything on the other side
#: needs a judgement that would destroy work if guessed wrong: archiving an
#: execution record discards the link to a reviewed candidate, quarantining a
#: worker repo moves the only copy of a branch, and "resolving" a blocker means
#: answering a question nobody has read. A repair command that guesses at those
#: is worse than none, because it looks like it worked.
def _repair_stale_lock(config) -> tuple[str, bool]:
    """Clear a lock whose owner is provably dead. Never one that is alive."""
    lock = LoopLock(config.state_dir)
    info = lock.read()
    if info is None:
        return ("lock         none held", True)
    if LoopLock.is_live(info):
        return (f"lock         HELD by a LIVE process ({info.describe()})", False)
    lock.break_stale()
    return (f"lock         stale lock removed ({info.describe()})", True)


def _repair_browser(config) -> tuple[str, bool]:
    """Restart the browser only if CDP does not answer, and only via the
    operator-declared command — the loop knows a `cdp_url`, not which Chrome
    owns it, and pattern-matching process lists is how an automation takes
    down someone's everyday browser."""
    try:
        _default_probe_cdp(f"{config.browser.cdp_url}/json/version")
        return ("browser      CDP answering", True)
    except Exception:
        pass
    if not config.browser.restart_command:
        return (
            "browser      CDP silent and no browser.restart_command configured "
            "— start the dedicated profile by hand",
            False,
        )
    result = subprocess.run(
        list(config.browser.restart_command),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        return (f"browser      restart FAILED: {result.stderr.strip()[:200]}", False)
    try:
        _default_probe_cdp(f"{config.browser.cdp_url}/json/version")
    except Exception as exc:
        return (f"browser      restarted but CDP still silent ({exc})", False)
    return ("browser      restarted, CDP answering", True)


def _report_blockers_and_phase(config) -> tuple[list[str], bool]:
    """Report what needs a human. Never resolve it."""
    lines: list[str] = []
    ok = True
    open_blockers = BlockerStore(config.blockers_dir).open_blockers_by_severity()
    if open_blockers:
        ok = False
        lines.append(f"blockers     {len(open_blockers)} OPEN — each needs a decision:")
        if len(open_blockers) > 1:
            # Say which one is primary AND how many else are open. Naming a
            # primary without the count would trade "the glob picked one" for
            # "there is only one" — a different wrong impression, not a fix.
            # Skipped entirely when exactly one is open, so that (common) case
            # prints exactly what it always did.
            lines.append(
                f"               most severe first: {open_blockers[0].id} is primary, "
                f"{len(open_blockers) - 1} other(s) also open"
            )
        for blocker in open_blockers:
            lines.append(f"               {blocker.id} ({blocker.kind}/{blocker.code})")
            lines.append(f"               {blocker.question[:160]}")
            lines.append(f'               resolve: python -m autoloop answer {blocker.id} "..."')
    else:
        lines.append("blockers     none open")

    _, state = _load_state(config)
    if state is None:
        lines.append("session      none yet — a fresh one will be selected")
        return lines, ok
    phase = Phase(state.phase)
    if phase is Phase.NEEDS_USER:
        # Not every park needs a human. `_handle_parked_task` quarantines a
        # `task_fatal` park's task and keeps going on the rest of the roadmap
        # — that is the whole point of the task_fatal/loop_fatal split. Only
        # a loop_fatal park (or an unresolved question) actually stops
        # continuous mode, so refusing on every `needs_user` would send you
        # off to resolve something the loop was about to handle itself.
        if state.park_kind == "task_fatal" and not open_blockers:
            lines.append(
                "session      parked task_fatal, no open blocker — continuous mode "
                "quarantines that task and continues"
            )
        else:
            ok = False
            lines.append(
                "session      PARKED at needs_user — continuous mode will stop immediately"
            )
            lines.append(f"               {(state.question or '(no question recorded)')[:160]}")
            lines.append('               resolve: python -m autoloop run --answer "..."  (or --retry)')
    elif phase is Phase.FAILED:
        ok = False
        lines.append("session      FAILED — continuous mode will stop immediately")
        lines.append("               resolve: python -m autoloop run --retry")
    else:
        lines.append(f"session      phase={phase.value} iteration={state.iteration}")
    return lines, ok


def _cmd_start(args: argparse.Namespace) -> int:
    """Repair what is provably safe, report what is not, then run.

    This is a START-time command, and deliberately not a pre-stop one.
    Stopping is already clean — SIGTERM and SIGHUP release the lock, `pause`
    finishes the current phase, and every state write is atomic and fsynced.
    More to the point, a pre-stop repair is unreliable by construction: the
    cases it would exist for (a power cut, a crash, a kernel panic) are
    exactly the ones that never give you the chance to run it. Recovery has
    to work from evidence left behind, not from cooperation before the fact.
    """
    config = load_config(args.config)
    print("autoloop start — repairing what is safe, reporting what is not\n")

    # A LIVE lock is the healthy already-running case, not a fault, and
    # pressing start twice must not read as "something needs a decision".
    # Checked before anything else because every repair below would be acting
    # underneath a working process: restarting its browser mid-request is a
    # real way to break a run that was fine.
    held = LoopLock(config.state_dir).read()
    if held is not None and LoopLock.is_live(held):
        print(f"lock         HELD by a live process ({held.describe()})")
        print("\nalready running — nothing to do.")
        print("  watch it:  python -m autoloop status")
        print("  stop it:   python -m autoloop pause")
        return 0

    ok = True
    for repair in (_repair_stale_lock, _repair_browser):
        line, healthy = repair(config)
        print(line)
        ok = ok and healthy

    if clear_pause(config):
        print("pause        flag cleared (start is an explicit request to run)")
    else:
        print("pause        not set")

    # Before the blocker report, not after: a QUARANTINE whose task is retired
    # is not a decision anyone can make, so listing it under "each needs a
    # decision" and refusing to start is the wrong answer to it. Only that kind
    # — a `loop_fatal` record naming the same task is a loop-wide condition the
    # retirement does not touch, and still stops `start`. This is where
    # the six pre-`RETIRED` retirements get their blockers reconciled —
    # `tasks._migrate_retirements` re-files those on load, with no command run
    # and nothing else to notice their records were left open (`run
    # --continuous` sweeps too, for the operator who skips `start`).
    #
    # Reported, never fatal. `start` exists to say what is wrong rather than to
    # die of it, and a task file it cannot read is a finding for the operator —
    # the `run` underneath will raise on the same file if they start anyway.
    # `KeyError`/`TaskGraphError` are in the net because the sweep is the first
    # thing here to call `state_of`, which raises on a graph `from_dict`
    # deliberately tolerates: a dependency naming a task that does not exist
    # survives the load (`_check_acyclic` uses `color.get`) and then fails on
    # the bare `self._tasks[dep]` lookup. `dashboard.task_groups` catches the
    # same pair for the same reason. That is a finding to report, not a
    # traceback out of the command whose whole job is reporting findings.
    try:
        _start_store, _start_registry = _load_tasks(config)
        _closed_blockers = _reconcile_retired_blockers(config, _start_registry)
        # And the reverse split brain, in the same preflight and before
        # anything selects a task: a `blocked` row whose every blocker is
        # already resolved or archived is excluded from `next_ready()` with
        # nothing left to justify it (blk-01). `start` is where a registry that
        # arrived in that state gets repaired, for the same reason the
        # retirement sweep lives here — nothing else would notice.
        _requeued = _reconcile_unblocked_tasks(config, _start_store, _start_registry)
    except (StateError, ConfigError, TaskGraphError, KeyError) as exc:
        print(f"tasks        UNREADABLE ({exc}) — retirements not reconciled")
        ok = False
    else:
        for _closed in _closed_blockers:
            print(
                f"blockers     {_closed.id} closed — task {_closed.task_id} is "
                "retired, so nobody can answer it"
            )
        for _task_id, _prior_reason in _requeued:
            print(
                f"tasks        {_task_id} returned to the queue — it was blocked "
                f"with no open blocker (was: {_prior_reason or '(no reason recorded)'})"
            )

    lines, healthy = _report_blockers_and_phase(config)
    for line in lines:
        print(line)
    ok = ok and healthy

    if not ok:
        print(
            "\nNOT started — the items above need a decision, and guessing at them "
            "would discard work. Resolve them with the commands shown, then run "
            "`start` again."
        )
        return 2
    if args.check_only:
        print("\nall clear (--check-only, not starting)")
        return 0
    print("\nall clear — starting continuous mode")
    args.continuous = True
    for name in ("kickoff", "answer"):
        setattr(args, name, None)
    for name in ("kickoff_audit", "retry", "resubmit"):
        setattr(args, name, False)
    args.max_steps = None
    return _cmd_run(args)


def _cmd_release(args: argparse.Namespace) -> int:
    """Return a task stranded IN-PROGRESS to pending, and retire the execution
    it leaves behind — both the worker repo and the `TaskExecution` record.

    A `loop_fatal` park mid-round leaves the task marked in-progress with
    nothing to finish it, so it is never picked again. THREE things have to
    move, not one:

    * the STATUS, or `next_ready` keeps skipping it;
    * the stale WORKER REPO, or the next dispatch refuses (`create()` will
      not write into an existing directory);
    * the EXECUTION RECORD, or `_merge_window_blockers` keeps reading a
      `candidate_sha` for work that no longer exists as in-flight — the
      commit is only inside the worker repo this command just quarantined,
      unreachable from the checkout, and the task is pending again.

    That third one was silently left behind until 2026-08-15. Releasing 25
    stranded tasks the day before left 14 records pinned to the pre-merge
    HEAD, and the merge window could not reopen by itself: every one of those
    tasks would have had to be re-dispatched AND re-published first. The
    operator archived the 14 by hand.

    Nothing is deleted. The worker is MOVED to quarantine (an interrupted
    round usually has real work in it — `dash-02` had a half-finished
    dashboard change) and the record is MOVED to `executions/archive/`, both
    under one label, so the candidate stays recoverable and the two halves
    name each other on disk. `worktask.retire_execution` does both in one
    call precisely so a future caller cannot do one and forget the other.

    The three-part sequence itself lives in
    `orchestrator.release_task_to_pending`, shared with the urgent-preemption
    path (`_preempt_for_urgent`), so a preemption cannot rebuild the same
    "moved one of the three" gap one level up. This command keeps its own
    refusal and its own reporting; only the move is shared.

    **A retirement that FAILS propagates**, unchanged and of its original type,
    for `main` to report and exit 1 on. That is this command's established
    contract (`test_recovery_commands.py::
    test_the_record_is_retired_before_the_worker`) and it is kept deliberately:
    a human ran this command and is reading its output, so a raised error is
    the loud ending, not a silent one. What it does NOT mean is that nothing
    happened — the status move is durable first, so the task IS pending again
    while its worker repo and record are still on disk, and re-running this
    command will not help (`registry.release` refuses a task that is no longer
    `in_progress`); move the worker repo aside instead.

    The urgent-preemption caller passes `tolerate_retirement_failure=True` and
    takes the opposite ending, because nobody is watching a preemption — see
    `orchestrator.release_task_to_pending`.

    The two "nothing to retire" lines below are about ABSENCE only (a task
    parked before it ever committed has neither half) and, because a failure
    now raises before reaching them, cannot absorb one.
    """
    config = load_config(args.config)
    with LoopLock(config.state_dir):
        task_store, registry = _load_tasks(config)
        worker_repos = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
        try:
            released = release_task_to_pending(
                args.task_id,
                registry,
                TaskExecutionStore(config.executions_dir),
                worker_repos,
                persist=lambda: task_store.save(registry),
            )
        except TaskGraphError as exc:
            print(f"error: {exc}")
            return 1
        print(f"task {released.task.id} released: in_progress -> pending")

        retired = released.retirement
        if retired.record_path is not None:
            print(
                f"execution record moved to {retired.record_path} (kept, not "
                "deleted — its candidate is still in the quarantined worker)"
            )
        else:
            print("no execution record to retire")
        if retired.worker_path is not None:
            print(
                f"worker repo moved to {retired.worker_path} "
                "(kept, not deleted — it may hold work)"
            )
        else:
            print("no worker repo to clear")
    return 0


#: `LoopState.stop_kind` for a session an operator's `shelve` detached from the
#: task it was running. Its own value rather than `"contract"` or
#: `PREEMPTION_STOP_KIND`, because every reader of that field gates on the
#: POSITIVE value it wants (see `LoopState.stop_kind`): reusing `"contract"`
#: would make every "did a reviewer really answer?" gate say yes for a session
#: no reviewer ever answered, and reusing `"preempted"` would make
#: `_is_preemption_stop` print a
#: displacement that never happened. Unrecognised-by-everything is the correct
#: reading — `_run_continuous` treats it as the clean boundary it is, because
#: only `"fault"` stops the loop.
SHELVE_STOP_KIND = "shelved"

#: The phases in which the loop OWES A REVIEW PACKET whose acceptance it cannot
#: yet prove, and in which `shelve` therefore refuses outright — whatever task
#: the packet is about. `delivering` is mid-deposit of a chunked payload,
#: `submitting` has a request created and possibly sent, the two
#: `submission_*` phases are a send whose acceptance is unknown or disproved,
#: and `awaiting` has a reviewer holding one. Exiting at any of them strands a
#: packet nobody can classify afterwards.
#:
#: The same set `orchestrator._at_round_boundary` refuses a preemption at,
#: arrived at from the other side: that predicate names the one SAFE phase
#: (`ready`, no pending request) and this names the unsafe ones, so the
#: `pending_request` half is checked separately here for the same reason it is
#: there — a request outlives its own phase.
PACKET_OUTSTANDING_PHASES = frozenset(
    {
        Phase.DELIVERING,
        Phase.SUBMITTING,
        Phase.SUBMISSION_UNCONFIRMED,
        Phase.SUBMISSION_REJECTED,
        Phase.AWAITING,
    }
)


def _session_names_task(state: LoopState, task_id: str) -> bool:
    """Does this saved session belong to `task_id`?

    THREE fields, not one, because three different readers decide "which task
    is this session about" from three different places and shelving has to
    satisfy all of them: `current_task` is what `orchestrator._preempt_for_
    urgent` reads, `task_execution` is the serialised record a resumed dispatch
    rehydrates, and `park_task_id` is what `cli._handle_parked_task` quarantines
    on the next continuous iteration. A session that names the task in ANY of
    them can still act on it, so any of them counts.
    """
    return task_id != "" and task_id in {
        (state.current_task or {}).get("task_id") or "",
        (state.task_execution or {}).get("task_id") or "",
        state.park_task_id or "",
    }


def _shelve_session_refusal(state: LoopState | None, task_id: str) -> str:
    """Why this session must not be shelved out from under, or `""` when it may.

    TWO guards with deliberately different scopes.

    **Guard 1 is loop-wide and refuses outright.** A review packet outstanding
    (`PACKET_OUTSTANDING_PHASES`, or a `pending_request` that outlived its
    phase) is not about one task: whatever it concerns, ending the session there
    strands a packet whose acceptance is unknown, and unknown acceptance is the
    one thing no later reconciliation can undo. It refuses even when the packet
    belongs to some OTHER task, because the hazard is the packet, not the task.

    **Guard 2 is task-scoped and only fires when this session could still act on
    the task being shelved** (`_session_names_task`). A session about something
    else needs no detaching, so its phase is none of shelve's business. When it
    IS about this task, the session has to be detachable, and only two phases
    are: `ready` (guard 1 has already established there is no pending request)
    and `stopped` (already a boundary — nothing to detach). The rest are refused
    with the specific reason, because each would lose something a shelve has no
    business losing:

    * `executing` holds a reviewer's directive that was received and not yet
      dispatched. Detaching discards a verdict the reviewer already gave.
    * `needs_user` is a park that OWNS this task: the next continuous iteration
      runs `_handle_parked_task`, which quarantines `park_task_id`. Shelving
      would be silently undone one iteration later — the task would come back
      `blocked`, not `pending`.
    * `failed` is an exhausted failure budget. `run --retry` is the route out,
      and rewriting that state into a clean stop would hide a wall the next
      iteration walks straight back into.

    An unrecognised phase refuses, fail-closed: whether a packet is outstanding
    is exactly what cannot be decided about a phase this build does not know.
    """
    if state is None:
        return ""
    try:
        phase = Phase(state.phase)
    except ValueError:
        return (
            f"the session is in an unrecognised phase {state.phase!r}, so "
            "whether it owes a review packet cannot be decided — refusing "
            "rather than guessing. Inspect the state file, or "
            "`python -m autoloop reset --yes` if the session is genuinely dead."
        )
    if phase in PACKET_OUTSTANDING_PHASES:
        return (
            f"a review packet is outstanding (phase {phase.value}) — leaving "
            "the session there would strand a packet whose acceptance nobody "
            "can establish afterwards. Let the round reach its boundary "
            "(`python -m autoloop run`, WITHOUT --continuous) and shelve then."
        )
    if state.pending_request is not None:
        return (
            f"request {state.pending_request.request_id} is still pending in "
            f"phase {phase.value} — a request outlives its own phase, and "
            "shelving on top of one strands it exactly as the packet phases "
            "would. Finish or resolve it first (`python -m autoloop run`, "
            "WITHOUT --continuous)."
        )
    if not _session_names_task(state, task_id):
        return ""
    if phase in (Phase.READY, Phase.STOPPED):
        return ""
    if phase is Phase.EXECUTING:
        return (
            f"the session is mid-round on {task_id} (phase executing): a "
            "reviewer's response has been received and not yet dispatched, and "
            "detaching would discard a verdict that was already given. Let it "
            "dispatch (`python -m autoloop run`, WITHOUT --continuous), then "
            "shelve."
        )
    if phase is Phase.NEEDS_USER:
        return (
            f"the session is parked on {task_id} (phase needs_user), and the "
            "park owns it: the next continuous iteration quarantines "
            "`park_task_id`, so this task would come back `blocked` rather "
            "than pending and the shelve would be silently undone. Resolve the "
            "park first — `python -m autoloop blockers`, then `answer` or "
            "`archive-blocker`."
        )
    if phase is Phase.FAILED:
        return (
            f"the session ended on an exhausted failure budget (phase failed) "
            f"while running {task_id}. Resolve that with `python -m autoloop "
            "run --retry` (WITHOUT --continuous) first; rewriting it into a "
            "clean stop here would only hide the wall."
        )
    return (  # pragma: no cover - every Phase member is handled above
        f"the session is in phase {phase.value} on {task_id}, which shelve has "
        "no rule for — refusing rather than guessing"
    )


def _detach_shelved_session(store: StateStore, state: LoopState | None, task_id: str) -> str:
    """Stop the saved session from pulling the loop straight back onto the task
    that was just shelved. Returns one line saying what it did.

    THE second failure mode, observed 2026-08-20 on dash-09: flipping a status
    to pending redirects nothing on its own, because `run --continuous` resumes
    a saved session in a NON-terminal phase BEFORE it ever consults
    `next_ready()` (`_run_continuous`'s `Phase(state.phase) not in
    TERMINAL_PHASES` branch). The loop restarted straight back onto the same
    task, and only `reset --yes` broke the pull.

    Ending the session as a `stopped` round is the same ending
    `orchestrator._preempt_for_urgent` uses, and for the same reason: every
    existing caller already treats `stopped` as the clean boundary it is, so
    the next iteration falls through to `_select_and_kickoff` and selects by
    priority again. NOT `store.path.unlink()` (what `_handle_parked_task` does
    for a task_fatal park): a selection at that boundary opens a brand-new
    `LoopState` either way, so deleting buys nothing and costs the one thing
    worth keeping — a `stop_reason` an operator can read in `status` afterwards
    saying why the session ended.

    The outbox is cleared with it, exactly as a preemption clears it: the
    payload queued in `ready` is about the task that just went back to the
    queue, so leaving it would send a packet about a round nobody is running.

    Called only after `_shelve_session_refusal` returned `""`, so the phase
    here is `ready`, a terminal phase, or a session about some other task.
    """
    if state is None:
        return "session: none on disk — nothing was holding the loop to this task"
    phase = Phase(state.phase)
    if not _session_names_task(state, task_id):
        return (
            f"session: in phase {phase.value} and does not name {task_id} — "
            "left exactly as it was"
        )
    if phase in TERMINAL_PHASES:
        return (
            f"session: already at a stop (phase {phase.value}) — the next "
            "`run --continuous` iteration selects afresh, so there was nothing "
            "to detach"
        )
    state.current_task = None
    state.task_execution = None
    state.last_response = None
    state.outbox = None
    state.outbox_diff = None
    state.outbox_attachment = None
    state.phase = Phase.STOPPED.value
    state.stop_kind = SHELVE_STOP_KIND
    state.stop_reason = (
        f"task {task_id} was shelved by the operator: its execution record and "
        "worker repository were left in place, so its next dispatch resumes "
        "that round rather than starting a new one"
    )
    store.save(state)
    return (
        f"session: detached from {task_id} (was {phase.value}, now stopped/"
        f"{SHELVE_STOP_KIND}) — `run --continuous` selects by priority again "
        "instead of resuming this task's session"
    )


def _branches_waiting_on_the_merge_window(config) -> tuple[list[str], str]:
    """Every published-but-unmerged branch a shut merge window is withholding,
    as `(lines, obstacle)`.

    Read off the auto-merge DEFERRAL records, which is where this fact already
    lives: `AutoMerger.attempt` records one per completed task whose merge it
    had to defer, naming the side branch, the candidate and the reason. That is
    a local file read — no remote, no `ls-remote` — which matters here, because
    this runs inside an operator command that has just moved a status and must
    not fail on a network that is down.

    It is also, precisely, what nobody read on 2026-08-20: dash-12's preserved
    candidate held the window shut on four branches for six hours and nothing
    named the connection until these records were opened by hand.

    Deliberately NOT `merge_sweep.sweep_backlog`, which would answer the same
    question more completely and at the cost of a remote round-trip per
    completed task — and which MERGES. A report has no business doing that.
    The residual is stated where it is printed: a branch that has never been
    attempted yet has no deferral record, so this is a floor on what is waiting,
    never a ceiling.
    """
    try:
        deferrals = MergeDeferralStore(config.merge_deferrals_dir).all_deferrals()
    except (StateCorruptError, OSError) as exc:
        return [], f"the merge-deferral records could not be read ({exc})"
    return [
        f"{d.task_id}: {d.dest_ref or '(no branch recorded)'} "
        f"(candidate {d.candidate_sha[:12]}, deferred {d.attempts}x since "
        f"{d.created_at} — last reason: {d.reason})"
        for d in deferrals
    ], ""


def _shelved_candidate_window_report(config, task_id: str, preserved) -> list[str]:
    """What the preserved candidate costs the repository, in the operator's own
    terms: is the merge window shut, is THIS task what shuts it, and which
    branches are waiting behind it.

    Reported because the operator will not otherwise see the connection. A kept
    candidate is a real hazard — moving the head under it is what parks a task
    on `task_base_behind_head` — and `shelve` deliberately does NOT exempt it
    from `_merge_window_blockers`. The exemption would reopen the window by
    lying about the hazard; naming the cost is the honest alternative.

    The predicate is CALLED, never re-derived: a second implementation of "may
    the branch head move" that drifted by one case is how thirteen tasks get
    parked at once (see `_merge_window_blockers`). A failure to evaluate it is
    reported as UNKNOWN and read as SHUT, never as open — the fail-closed
    direction, and the same one the predicate takes internally.
    """
    lines: list[str] = []
    # The ONLY short circuit, and it is the only one that is decidable without
    # asking: no record means no `candidate_sha` for any reader to find, so
    # keeping this task's round withholds nothing. Deliberately NOT gated on
    # `holds_a_candidate` — a record that is present but UNREADABLE reports an
    # empty candidate here, and answering "this task holds no candidate" from
    # that would be a claim made without asking, which is this report's own
    # fail-open shape.
    if preserved.record_path is None:
        lines.append(
            "merge window: there is no execution record here, so keeping this "
            "task's round withholds nothing from the merge sweep"
        )
        return lines
    try:
        reasons, notes = _merge_window_blockers(config, set())
    except (StateError, ConfigError, TaskGraphError, KeyError, GitError, OSError) as exc:
        lines.append(
            f"merge window: UNKNOWN — the predicate could not be evaluated "
            f"({exc}). Treat it as SHUT: the candidate you just preserved is "
            "still on disk and still bound to its base."
        )
        return lines
    mine = [r for r in reasons if r.startswith(f"task {task_id} ")]
    if mine:
        lines.append(
            "merge window SHUT, and the candidate you just preserved is what "
            "holds it:"
        )
        lines.extend(f"  - {reason}" for reason in mine)
        for reason in reasons:
            if reason not in mine:
                lines.append(f"  - (also) {reason}")
    elif reasons:
        lines.append(
            "merge window SHUT, but not by anything this task preserved (its "
            "record names no candidate, or one that is exempt or already "
            "behind — see the notes below):"
        )
        lines.extend(f"  - {reason}" for reason in reasons)
    else:
        lines.append(
            "merge window OPEN — nothing this task preserved is holding it "
            "(no candidate on the record, or one that is published, already "
            "behind the head, or belongs to a terminal task)"
        )
    for note in notes:
        lines.append(f"  note: {note}")
    if not mine:
        return lines

    waiting, obstacle = _branches_waiting_on_the_merge_window(config)
    if obstacle:
        lines.append(f"  waiting behind it: UNKNOWN — {obstacle}")
    elif waiting:
        # "waiting on THIS window" is exact rather than loose, and the reason is
        # `merge_sweep`'s own shape: the sweep checks this predicate ONCE before
        # the first merge and is all-or-nothing, so a shut window withholds
        # every published-but-unmerged branch regardless of why each was
        # individually deferred. Each entry still carries its own reason, so an
        # operator can tell a window deferral from a dirty-checkout one.
        lines.append(
            f"  {len(waiting)} published branch(es) are deferred and unmerged; "
            "the sweep is all-or-nothing and checks this window once, so NONE "
            "of them can be swept while it is shut:"
        )
        lines.extend(f"    - {entry}" for entry in waiting)
        lines.append(
            "    (deferral records only — a branch the sweep has not tried yet "
            "has none, so this is a floor, not a ceiling)"
        )
    else:
        lines.append(
            "  no merge deferral is recorded yet, so nothing is known to be "
            "waiting behind it — but the window stays shut for the whole "
            "repository until this task publishes or is released"
        )
    return lines


def _cmd_shelve(args: argparse.Namespace) -> int:
    """Return a task stranded IN-PROGRESS to pending and KEEP the round it
    holds — the sibling of `release`, not a replacement for it.

    `release` is "throw it back and redo it": `worktask.retire_execution` moves
    the execution record to `executions/archive/` and the worker repo to
    quarantine, so the next dispatch starts a fresh record from scratch. That is
    the right answer often enough to be the default, and it is exactly the wrong
    answer when the round in flight is work somebody asked to keep.

    Observed 2026-08-20. dash-12 held a candidate of 5 files / 1160 insertions
    at review round 1, and its reviewer's instruction was explicit: "resume the
    existing dash-12 worker repository and preserve its partial implementation;
    do not restart the task". It was also stranded `in_progress`, which
    `next_ready()` skips, so nothing would ever pick it up — and while stranded
    its unpublished candidate held the merge window shut on base-02, dash-14 and
    val-02 for six hours. `release` would have discarded precisely what the
    reviewer asked to keep, so the status was edited in `tasks.json` by hand,
    with the loop stopped, three separate times in one night.

    THE RESUME PATH ALREADY EXISTS; this command only reaches it. A dispatch
    loads the record (`_dispatch_task_postcommit`), probes the recorded worker
    with `worker_env.worker_repo_is_reusable` BEFORE the staleness check, and
    on a pass resumes that worker as it stands — a base that merely moved on is
    not grounds to quarantine and rebuild it. So a task whose record and worker
    are left intact already resumes correctly; the only thing missing was a
    supported, attested way to make it selectable. Three things therefore move,
    or rather two move and one is proved NOT to:

    * the STATUS, `in_progress -> pending` (`TaskRegistry.shelve`), or
      `next_ready` keeps skipping it;
    * the SESSION, which otherwise drags the loop straight back onto the same
      task before priority is ever consulted (`_detach_shelved_session`);
    * the EXECUTION RECORD and the WORKER REPO, which are left exactly where
      they are and ATTESTED as still resumable (`worktask.preserve_execution`
      runs the same three-fact probe the dispatch will).

    THREE THINGS THIS DELIBERATELY DOES NOT DO.

    * **It does not change `release`.** Both verbs are legitimate and the
      choice is the operator's.
    * **It does not exempt the preserved candidate from the merge window.** A
      kept candidate IS a real hazard — moving the head under it is what parks
      a task on `task_base_behind_head` — so `_merge_window_blockers` must keep
      seeing it. What this command adds is the sentence nobody had: the window
      is shut, THIS is what shuts it, and these branches are waiting.
    * **It does not refund the attempt budget.** Preserving a round preserves
      its cost. `release` resets the budget only as a consequence of archiving
      the record, never by editing a counter, and this archives nothing.

    REFUSALS. Under the loop lock like every operator write beneath
    `.autoloop/`, and refusing outright while a review packet is outstanding —
    see `_shelve_session_refusal` for the two guards and why their scopes
    differ. Nothing durable is written until both refusals and the registry's
    own have passed: the registry move below is in memory, and `task_store.save`
    is the first write.

    ORDER, and it is the same rule `release` follows: the STATUS is made durable
    BEFORE the session is detached. A process that dies in between leaves a
    pending task whose session still names it — the loop resumes that round,
    which is what shelving promised the next dispatch would do anyway. The
    reverse order would leave the task `in_progress` with its session gone: the
    invisible-to-`next_ready` dead end this command exists to end, rebuilt by
    the command itself.
    """
    config = load_config(args.config)
    with LoopLock(config.state_dir):
        try:
            state_store, state = _load_state(config)
        except (StateError, ConfigError) as exc:
            # FAIL CLOSED. An unreadable session is precisely the one in which
            # "is a packet outstanding?" cannot be answered, and answering it
            # "no" by default is the failure the guard exists to prevent.
            print(
                f"error: the session state could not be read ({exc}) — refusing "
                "to shelve, because whether a review packet is outstanding is "
                "exactly what cannot be established from it."
            )
            return 1
        task_store, registry = _load_tasks(config)
        try:
            task = registry.shelve(args.task_id)
        except TaskGraphError as exc:
            print(f"error: {exc}")
            return 1
        refusal = _shelve_session_refusal(state, args.task_id)
        if refusal:
            # Nothing has been persisted: `registry.shelve` moved an in-memory
            # status and `task_store.save` has not run, so returning here leaves
            # the task exactly as it was found.
            print(f"error: {args.task_id} was NOT shelved — {refusal}")
            return 1

        preserved = preserve_execution(
            args.task_id,
            TaskExecutionStore(config.executions_dir),
            WorkerRepoManager(config.workers_root, config.worker_hooks_dir),
        )
        task_store.save(registry)
        print(f"task {task.id} shelved: in_progress -> pending")

        if preserved.record_path is not None:
            print(
                f"execution record KEPT at {preserved.record_path} — candidate "
                f"{(preserved.candidate_sha or '(none)')[:12]}, review round "
                f"{preserved.review_round}, attempt {preserved.attempt_count} "
                "(not refunded: preserving a round preserves its cost)"
            )
        else:
            print(
                "no execution record to keep — nothing on disk describes an "
                "in-flight round for this task, so its next dispatch starts a "
                "fresh record. (Absence, not a claim about why: a task parked "
                "before it ever committed looks the same as one whose record "
                "was already retired.)"
            )
        if preserved.worker_path is not None:
            print(f"worker repo KEPT at {preserved.worker_path} — not quarantined")
        else:
            print("no worker repo on disk to keep")
        if preserved.record_path is not None:
            if preserved.resumable:
                print(
                    "next dispatch RESUMES this round: the recorded worker "
                    "passes the same three-fact reuse probe the dispatch runs "
                    "(present, a git repository, on the recorded branch)"
                )
            else:
                print(
                    "WARNING — the next dispatch will NOT resume this round: "
                    f"{preserved.obstacle or 'the reuse probe did not pass'}. "
                    "Nothing was moved, so the evidence is all still on disk; "
                    "fix the worker, or use `release` if the round is genuinely "
                    "not worth keeping."
                )

        print(_detach_shelved_session(state_store, state, args.task_id))
        for line in _shelved_candidate_window_report(config, args.task_id, preserved):
            print(line)

        # What the loop will actually do next, rather than what "set aside"
        # sounds like it should do. Shelving returns a task to the QUEUE; it
        # does not lower its priority, so a task that is still the most urgent
        # thing ready is selected again on the very next iteration — and
        # resumed, which is correct and is the whole claim, but is not what an
        # operator who typed "shelve" is necessarily expecting.
        nxt = registry.next_ready()
        if nxt is not None and nxt.id == args.task_id:
            print(
                f"note: {args.task_id} is still what `next_ready()` returns, so "
                "the loop picks it straight back up (resuming this round). Lower "
                "its priority, or `urgent` another task, to work on something "
                "else first."
            )
        elif nxt is not None:
            print(f"note: the loop's next selection is {nxt.id}")
        else:
            print("note: nothing else is ready — the loop will idle or audit")
    return 0


def _reconcile_retired_blockers(config, registry) -> list[Blocker]:
    """Close the open QUARANTINE blockers of RETIRED tasks. Returns what it closed.

    A retirement lives in `tasks.json`; a quarantine lives in its own record
    under `blockers/`. Retiring a quarantined task used to move only the first,
    which is a split brain rather than a tidiness problem: the blocker record is
    read INDEPENDENTLY of the registry by `_report_blockers_and_phase` (so
    `start` refuses to run), by `health.check` (so the loop reports
    `stuck_blocked`) and by `heartbeat` (so a monitor alarms). The dashboard row
    would say RETIRED / "waits on nobody" while the loop stayed stopped waiting
    on exactly that task, which is the same two-meanings-at-once failure
    `TaskState.RETIRED` exists to end, rebuilt one file over.

    `archive_stale`, never `resolve`: an archival writes `archived_reason` and
    leaves `answer` None. Nobody answered these questions — the work they belong
    to was superseded — and writing an answer would forge the operator
    confirmation `_RESOLUTION_PRECONDITIONS` exists to demand. Nothing is
    deleted: the record keeps its question, its detail, its recurrence count and
    its session id, and gains a machine reason naming the retirement.

    **Only `kind="task_fatal"` records, and it is an ALLOWLIST.** The question a
    quarantine asks is about the one task at fault, so retiring that task really
    does make it unanswerable. A `loop_fatal` blocker is the opposite: it is a
    LOOP-WIDE safety condition that merely happened to be recorded while some
    task was in flight, and several of them name that task —
    `checkout_escape_detected`, `primary_checkout_dirty`, a worker/publisher
    environment failure. Retiring the in-flight task must not manufacture
    resolution of those: `start` would proceed, `health` would go quiet, and the
    dirty checkout or the escaped write would still be there. So every
    `loop_fatal` record is preserved regardless of its task id, until its own
    `_RESOLUTION_PRECONDITIONS` recheck clears it (or `archive-blocker` does,
    which stays deliberately unrestricted — it is the operator's explicit
    escape hatch for the ones no answer can close).

    The decision is per BLOCKER, not per task: a retired task holding both kinds
    has its quarantine closed and its loop-fatal record left open, which is the
    honest reading of each. An unrecognised or empty `kind` is treated as
    loop_fatal and left alone, matching the fail-closed default in
    `orchestrator._to_needs_user` and `_handle_parked_task` — a record we cannot
    classify is not one to close automatically.

    Two further exclusions. `NO_TASK` (`"(loop)"`) blockers are never swept — a
    login expiry or an exhausted iteration budget is a loop-level condition that
    no task retirement answers. And a task that is merely quarantined keeps its
    blocker, obviously; membership is decided by `state_of`, so a genuine
    failure like audit-0003 is untouched.

    Three callers: `_cmd_retire_task` (the supported route — a retirement
    decided today), `_cmd_start`'s preflight, and the top of every
    `_run_continuous` iteration. The last two are what cover the six
    pre-`RETIRED` retirements, which are re-filed in memory by
    `tasks._migrate_retirements` on load — their status moves with no command
    run, and nothing else would notice their records were left open. The
    continuous sweep is at the TOP of the iteration rather than down at the
    exhaustion check because the readers that misjudge an orphaned blocker are
    out of process: a loop with plenty of ready work would otherwise leave
    `health.check` reporting `stuck_blocked` for hours while working perfectly.

    Idempotent, and safe to call on every pass: `open_blockers()` is re-read
    each time, so an already-archived record is simply not there to archive
    again.
    """
    retired = {
        task.id
        for task in registry.all_tasks()
        if registry.state_of(task.id) is TaskState.RETIRED
    }
    if not retired:
        return []
    store = BlockerStore(config.blockers_dir)
    closed: list[Blocker] = []
    for blocker in store.open_blockers():
        # Allowlist, not a `!= "loop_fatal"` denylist: `kind` is a bare string
        # with no default, so an old or hand-written record can carry anything,
        # and anything we cannot classify must stay open.
        if blocker.kind != "task_fatal":
            continue
        if blocker.task_id == NO_TASK or blocker.task_id not in retired:
            continue
        successors = ", ".join(registry.get(blocker.task_id).superseded_by)
        closed.append(
            store.archive_stale(
                blocker.id,
                f"task {blocker.task_id} was retired"
                + (f" (superseded by {successors})" if successors else "")
                + " — this question belongs to work that will not be dispatched "
                "again, and was never answered",
            )
        )
    return closed


def _reconcile_unblocked_tasks(config, task_store, registry) -> list[tuple[str, str]]:
    """Return every blocker-derived `blocked` task to the queue once no OPEN
    blocker names it. Returns `(task_id, prior_reason)` for what it released.

    The mirror image of `_reconcile_retired_blockers` above, and it exists for
    the same reason: a quarantine is TWO halves — a status in `tasks.json` and a
    question in its own record under `blockers/` — and moving only one is a
    split brain rather than untidiness. That sweep answers "the task is gone, so
    close its question"; this one answers "the question is gone, so requeue its
    task".

    Observed on port-01 (2026-08-19). It parked with `review_packet_build_
    failed`, the operator answered that blocker and it was resolved, and hours
    later the registry still read `status=blocked` with port-01 absent from
    every open blocker — excluded from `next_ready()` with nothing left to
    justify it. No supported command could undo it: `answer` needs an OPEN
    blocker (and already REPORTED the split brain — "could not be unblocked
    (task_not_blocked)" — before dropping it on the floor), `release` refuses
    anything that is not `in_progress`, `retire` means "never worked again"
    rather than "runnable again", and there is no `unblock`. The only route out
    was editing `tasks.json` by hand with the loop stopped, which also needs a
    pause window the escape detector otherwise punishes.

    So the state is reconciled rather than a repair command being added. What
    that costs, stated plainly: a task blocked with no record on disk AT ALL —
    a hand-edited status, a park written by a build with no blocker store — is
    released too. That is the claim taken literally ("a task is `blocked` only
    while it has at least one OPEN blocker"), and it is the safe direction: the
    task goes back into the queue, where the condition that quarantined it will
    re-fire and record a blocker properly, rather than sitting invisible.

    THREE things it does not do, each load-bearing:

    * It never touches a `blockers.Blocker`. Not resolved, not archived, not
      bumped — resolution stays an operator act (or `archive_stale`'s explicit
      machine reason), and a sweep that could close records would be a way to
      launder exactly the confirmation `_RESOLUTION_PRECONDITIONS` demands.
      Membership is decided by READING them.
    * It never reaches an operator hold. `TaskRegistry.blocker_derived_blocked`
      excludes `hold_origin == HOLD_ORIGIN_OPERATOR`, which is the only
      provenance marker anything here trusts — a hold placed through the inbox
      has no blocker record by design, so "nothing open names it" is true of
      one from the instant it is placed. Only the derived `blocked` that
      MIRRORS a record is reconciled.
    * It never widens what counts as blocking. Any OPEN blocker naming the task
      keeps it out, whatever its `kind` (see `BlockerStore.open_task_ids`).

    Every release is written to the TRANSCRIPT, with the reason the task was
    carrying — `unblock()` clears `blocked_reason`, so afterwards the transcript
    is the only place the transition stays legible. A task that returns to the
    queue on nobody's authority must not do it silently.

    Saves ONLY when something moved, so the ordinary case (nothing to
    reconcile) writes nothing at all — this runs at the top of every continuous
    iteration, and a `tasks.json` rewritten on each pass would be noise in the
    escape detector's snapshot for no gain. Idempotent for the same reason: a
    second call finds the task `pending` and has nothing to do.

    **Cannot raise once `task_store.save` has returned.** The transcript append
    that follows it is reported rather than raised, exactly as
    `_cmd_retire_task` reports a sweep it could not run after a durable
    retirement. That is not tidiness: `_requeue_after_close` REOPENS the blocker
    it just closed when this function raises, and a raise from after the save
    would reopen a blocker whose task is already back in the queue — the
    opposite split brain. So the save is the single point of no return, and
    everything after it degrades to a warning.
    """
    candidates = registry.blocker_derived_blocked()
    if not candidates:
        return []
    open_task_ids = BlockerStore(config.blockers_dir).open_task_ids()
    released: list[tuple[str, str]] = []
    for task in candidates:
        if task.id in open_task_ids:
            continue
        reason = task.blocked_reason
        registry.unblock(task.id)
        released.append((task.id, reason))
    if not released:
        return []
    task_store.save(registry)
    try:
        log = TranscriptLogger(config.transcript_file).append
        for task_id, reason in released:
            log(
                "task_auto_unblocked",
                data={
                    "task_id": task_id,
                    "prior_status": "blocked",
                    # Kept because `unblock` clears it: without this the account
                    # of WHY the task was ever quarantined survives nowhere once
                    # the blocker that carried the question is closed.
                    "prior_blocked_reason": reason,
                    "note": (
                        "no OPEN blocker named this task, so its quarantine had "
                        "nothing left to justify it — returned to the queue"
                    ),
                },
            )
    except OSError as exc:
        print(
            f"warning: the requeue of {', '.join(t for t, _ in released)} could "
            f"not be written to the transcript ({exc}) — the release itself is "
            "saved"
        )
    return released


def _print_auto_unblocked(released: list[tuple[str, str]]) -> None:
    """One operator-facing line per task `_reconcile_unblocked_tasks` released.

    Separate from the sweep so the four callers that print this wording share
    it (`_run_locked`, `_run_continuous`, `_cmd_answer`,
    `_archive_blocker_locked`) — `_cmd_start` is the fifth reconcile site and
    prints its own column-aligned form, since every other line of that preflight
    is a padded `label  detail` pair. Either way the sweep's transcript entry is
    the durable record and this is only what the operator happens to be looking
    at.
    """
    for task_id, reason in released:
        print(
            f"task {task_id} returned to the queue — no open blocker remained "
            f"(was: {reason or '(no reason recorded)'})"
        )


#: What a requeue is allowed to fail with and still be HANDLED rather than
#: raised. Same net the tolerant sweep used to carry: `KeyError` for the reason
#: `_cmd_start` names (a `depends_on` naming a task that no longer exists
#: survives `from_dict` and fails on the later lookup), `OSError` because both
#: stores are files. Anything outside it is a bug and gets a traceback.
_REQUEUE_FAULTS = (StateError, ConfigError, TaskGraphError, KeyError, OSError)

#: `_requeue_after_close`'s outcome. THREE values, not a bool, because the two
#: failures want opposite sentences from the calling command: `_REOPENED` means
#: nothing changed and the operator can just run the command again, while
#: `_CLOSE_STANDS` means the record really is closed on disk with its task not
#: requeued — the one state nothing here can repair by itself. Reporting the
#: second as "nothing changed" would be the false line `_cmd_answer`'s
#: "the message has to be true" rule exists to prevent.
_REQUEUE_OK = "ok"
_REQUEUE_REOPENED = "reopened"
_REQUEUE_CLOSE_STANDS = "close_stands"


def _requeue_after_close(
    config, blocker_store: BlockerStore, before: Blocker
) -> tuple[list[tuple[str, str]], TaskRegistry | None, str]:
    """The task half of closing `before` — run so BOTH halves land or NEITHER
    does. Returns `(released, registry, outcome)`; `registry` is the post-sweep
    in-memory graph (None when it could not be read) and `outcome` is one of
    `_REQUEUE_OK` / `_REQUEUE_REOPENED` / `_REQUEUE_CLOSE_STANDS`.

    Called by the only two commands that CLOSE a blocker (`answer`,
    `archive-blocker`), and it is where those commands FAIL CLOSED. Closing the
    last open record naming a quarantined task and leaving that task `blocked`
    is precisely the split brain blk-01 exists to make impossible, so a close
    whose requeue cannot be completed is not allowed to stand as a close: the
    record is written back exactly as it was read, the command reports the
    failure, and it exits non-zero.

    The earlier shape did the opposite. `_sweep_unblocked_tasks` caught the same
    faults, printed a warning, and left the archival or the resolution standing
    with the note that "the loop's next start" would repair it — which is the
    invariant restated as a promise about some later process rather than
    enforced here. An hour of `status=blocked` with nothing open naming the task
    is the state the claim says cannot exist, whether or not something would
    eventually notice.

    Scope: SYNCHRONOUS command failures only. A process killed between the
    close and the requeue still leaves the split state, and the startup sweeps
    (`_cmd_start`, `_run_locked`, `_run_continuous`) are what repair that — they
    stay deliberately TOLERANT, since a startup sweep has no blocker of its own
    to put back and refusing to start would trade a repairable state for an
    unstartable loop.

    `_reconcile_unblocked_tasks` cannot raise once its `task_store.save` has
    returned (see its docstring), so a fault reaching here proves the task half
    is untouched and reopening is safe.
    """
    try:
        task_store, registry = _load_tasks(config)
        released = _reconcile_unblocked_tasks(config, task_store, registry)
    except _REQUEUE_FAULTS as exc:
        print(f"error: the task graph could not be reconciled ({exc})")
        restored = _reopen_blocker(blocker_store, before)
        return [], None, _REQUEUE_REOPENED if restored else _REQUEUE_CLOSE_STANDS
    return released, registry, _REQUEUE_OK


def _reopen_blocker(blocker_store: BlockerStore, before: Blocker) -> bool:
    """Write `before` back exactly as it was read, so a close that could not
    requeue is no close at all. True when the record really was restored.

    A plain `save` of the pre-close snapshot rather than a `reopen()` on the
    store: nothing else may ever move a blocker from closed back to open, and
    adding that verb would be a way to un-answer an operator's answer. This is
    an undo of a write THIS command made microseconds ago, inside the lock,
    from the record it read before making it.

    Best-effort at the very end, and it says so when it fails — a snapshot that
    cannot be written back leaves the one state nothing here can fix by itself,
    so the operator has to be told in the terms that matter (the record is
    CLOSED, the task was not requeued) rather than getting a traceback.
    """
    try:
        blocker_store.save(before)
    except OSError as exc:
        print(
            f"error: blocker {before.id} could NOT be reopened ({exc}) — it is "
            "closed on disk and its task was not returned to the queue. Reopen "
            "the record by hand before starting the loop, or let the startup "
            "sweep requeue the task."
        )
        return False
    print(
        f"blocker {before.id} was reopened — nothing changed. Closing the last "
        "blocker of a quarantined task returns that task to the queue in the "
        "same operation, so a close that cannot requeue is not a close."
    )
    return True


def _cmd_retire_task(args: argparse.Namespace) -> int:
    """Record that a task is superseded and will never be worked again.

    The operator route to `TaskRegistry.retire`, and the manual fallback for a
    pre-`RETIRED` retirement whose `blocked_reason` was reworded and so did not
    match `tasks._RETIREMENTS` on load (that table is guarded on the reason
    text precisely so it never retires something nobody retired).

    Distinct from `release`, which is about an INTERRUPTED round: that returns
    a task to the queue, this takes it out of the queue for good. Distinct from
    the quarantine `answer` clears, too — a quarantine is a question waiting
    for the operator, a retirement is an answer that already happened under
    another task id.

    `--superseded-by` is repeatable and optional. Optional because `dash-01` —
    the task that motivated this command — went stale rather than being
    replaced, and naming an invented successor would put a false chain in the
    record. Successors are NOT required to exist: brw-06 was split into
    brw-07 + brw-08 before either was planned.

    Retiring a QUARANTINED task moves both halves of its state: the registry
    row here, and the open blocker record that quarantined it
    (`_reconcile_retired_blockers`). Moving only the first would leave `start`,
    `health` and the heartbeat still stopped on a question about work nobody is
    going to do. Only that record: a `loop_fatal` blocker naming the same task
    is a loop-wide condition (a dirty checkout, an escaped write) that this
    command has no evidence about, so it stays open and `start` stays refused.

    Nothing is deleted, here or in the registry: the task keeps its id, its
    description, its reason, and its place in the graph; the blocker keeps its
    question and gains a machine reason rather than a forged answer. Takes the
    loop lock, like `release`, because it writes `tasks.json`.

    A repeat is not an update. `TaskRegistry.retire` refuses a second
    retirement that would add, change or reword anything, and reports an exact
    repeat as the no-op it is — a bare `retire brw-02` must never be the
    command that erases brw-02's recorded successor.

    **It refuses a retirement that would strand a dependent** (retire-01), and
    this command's job is to make that refusal actionable: it prints every
    direct dependent by name and the transitive count beside it, because 4
    direct dependents and 21 tasks behind them are different decisions.
    `--superseded-by` naming live successors lifts the refusal and re-points
    those dependents at them; `--rewrite-dependents` is the explicit opt-in
    that drops the dependency instead. Either way the rewrite and the
    retirement are one registry mutation followed by one `save`, so a crash
    cannot land half of it — and on success the new `depends_on` of every
    dependent is echoed, read back off the registry rather than assumed.
    """
    config = load_config(args.config)
    with LoopLock(config.state_dir):
        task_store, registry = _load_tasks(config)
        previous = registry.get(args.task_id).status if registry.has(args.task_id) else ""
        try:
            # Read BEFORE the retirement, and inside the same `try` so an
            # unknown id is reported as the ordinary refusal rather than as a
            # traceback: afterwards the dependents no longer name this task, so
            # there would be nothing left to report having re-pointed.
            strand = registry.stranded_dependents(args.task_id)
            task = registry.retire(
                args.task_id,
                superseded_by=tuple(args.superseded_by or ()),
                reason=args.reason or "",
                rewrite_dependents=bool(getattr(args, "rewrite_dependents", False)),
            )
        except TaskGraphError as exc:
            print(f"error: {exc}")
            return 1
        task_store.save(registry)
        # After the save, and reported rather than raised: the retirement is
        # already durable at this point, so a graph whose `state_of` cannot be
        # computed (a dependency naming a task that does not exist survives
        # `from_dict` by design) must not turn a completed retirement into a
        # traceback. What it costs is the blocker sweep, which `start` retries.
        try:
            closed = _reconcile_retired_blockers(config, registry)
        except (TaskGraphError, KeyError) as exc:
            closed = []
            print(
                f"warning: the task graph could not be read to reconcile blockers "
                f"({exc}) — the retirement is saved; any blocker for {task.id} is "
                "still open"
            )
    successors = ", ".join(task.superseded_by) or "nothing (stale, not replaced)"
    if previous == "retired":
        print(f"task {task.id} was already retired; nothing changed")
    else:
        print(f"task {task.id} retired: {previous} -> retired")
    print(
        f"superseded by {successors}\n"
        f"reason kept: {task.blocked_reason or '(none recorded)'}"
    )
    if not strand.strands:
        print("dependents: none — nothing was waiting on this task")
    elif previous == "retired":
        # The repeat returned early and rewrote nothing, so these are reported
        # as the state of the graph, never as an outcome of this command. A
        # strand an earlier retirement left behind is re-pointed with a
        # `depends_on` mutation (`TaskRegistry.set_depends_on`).
        print(f"dependents unchanged: {strand.describe()}")
    else:
        # NOT `strand.describe()`. That sentence is written for the REFUSAL and
        # says "N tasks blocked in total" in the present tense — true while the
        # retirement is being refused, false the moment it goes through, since
        # these are the dependents that were just re-pointed. Both counts are
        # still reported, because the transitive one is what the operator's
        # decision turned on; only the tense changes.
        direct = len(strand.direct)
        total = len(strand.transitive)
        print(
            f"dependents re-pointed: {direct} that named {task.id} directly; "
            f"{total} task{'s' if total != 1 else ''} "
            f"{'was' if total == 1 else 'were'} waiting on it in total, "
            "counting those behind them"
        )
        for dependent_id in strand.direct:
            now = ", ".join(registry.get(dependent_id).depends_on) or "nothing"
            print(f"  {dependent_id} now depends on {now}")
    for blocker in closed:
        print(
            f"blocker {blocker.id} closed — its task is retired "
            "(archived with a machine reason, NOT an operator answer)"
        )
    return 0


def _cmd_archive_blocker(args: argparse.Namespace) -> int:
    """Close a blocker whose session is gone, recording a machine reason.

    Some blockers cannot be answered at all — `checkout_escape_detected`
    refuses every answer by design, because a text reply would fabricate
    exactly the human confirmation it exists to demand. Its own message says
    to archive the session instead. But archiving the session left the
    blocker RECORD open, `start` refuses to run with an open blocker, and
    nothing on the CLI could close it: the only way out was to call
    `BlockerStore.archive_stale` from a Python one-liner. Hit for real on
    2026-08-02.

    Not a backdoor for answering. It writes `archived_reason`, never
    `answer`, and it REFUSES a blocker belonging to the session that is
    still live — otherwise it would become the "clear the escape detection"
    button that `_RESOLUTION_PRECONDITIONS` deliberately withholds.

    **Takes the loop lock, like `answer` and `retire`** (blk-01, review round
    2). Archiving can close the LAST open record naming a quarantined task, and
    when it does, that task has to return to the queue in the same operation
    (`_reconcile_unblocked_tasks`) — otherwise this command is itself a way to
    manufacture exactly the split brain the sweep exists to end. Requeueing
    means writing `tasks.json`, so it needs the lock that owns that file.

    The earlier shape archived first, READ the lock afterwards, and skipped the
    requeue when a live loop held it. Two faults, one design and one race. The
    design fault: a successful archival could durably leave its task `blocked`
    with no open blocker — the loop's next iteration would fix it, but "the
    next iteration" can be an hour of a state the invariant says cannot exist.
    The race: `read()` + write is check-then-act, so a loop starting in that
    window got its `tasks.json` written underneath it anyway.

    **And it FAILS CLOSED inside the lock too** (blk-01, review round 3). Taking
    the lock only removed the race; the archival still landed first and the
    requeue was best-effort, so a `tasks.json` that would not parse left exactly
    the same durable split state with a warning printed over it. Now the
    archival is undone (`_requeue_after_close` → `_reopen_blocker`) and the
    command exits 1 whenever the task half cannot be completed, so "archived"
    is never printed for an archival that did not requeue what it closed. If the
    undo itself cannot be written, the command says the record remains CLOSED
    rather than "nothing changed" — the two want different next moves, which is
    the same reason the lock refusal above says which state it left behind.

    So the whole command is inside the lock, and when the lock cannot be taken
    NOTHING happens — not the archival, not the requeue. That is also the
    stronger version of the escape-detector argument this command used to make:
    `.autoloop/` sits inside the tree `escape_detector.enumerate_checkout_paths`
    snapshots (ignored paths included), so a write here mid-round reads as an
    agent escaping its worker repo; holding the lock is what proves no round is
    in flight, rather than a read that could go stale a microsecond later.

    A LIVE lock and a STALE one both refuse, and both say the blocker is
    untouched — an operator who sees a lock error has to know the record is
    still open, since "archived but not requeued" and "not archived at all" want
    different next moves. Never `break_stale`: locks are not stolen here any
    more than anywhere else, and `unlock` is the documented recovery.
    """
    config = load_config(args.config)
    try:
        with LoopLock(config.state_dir):
            return _archive_blocker_locked(config, args)
    except (LockHeldError, StaleLockError) as exc:
        # Nothing inside the block raises either of these, so this catch can
        # only ever be the acquisition failing — i.e. before `archive_stale`
        # touched anything.
        print(f"error: {exc}")
        print(
            f"blocker {args.blocker_id} was NOT archived — nothing changed. "
            "Archiving the last blocker of a quarantined task returns that task "
            "to the queue in the same operation, so this command writes "
            "`tasks.json` and needs the loop lock."
        )
        return 1


def _archive_blocker_locked(config: AutoloopConfig, args: argparse.Namespace) -> int:
    """The body of `archive-blocker`, with the loop lock already held.

    Split out so the lock is a single `with` around every read AND every write:
    the session check reads `state.json`, the archival writes a blocker record,
    the sweep writes `tasks.json`, and the undo of a failed sweep writes the
    blocker record back. All four inside, so nothing decided here can be
    invalidated between the deciding and the writing.
    """
    store = BlockerStore(config.blockers_dir)
    # Also the pre-close snapshot `_reopen_blocker` writes back if the requeue
    # cannot be completed: `archive_stale` decodes its own copy from disk, so
    # this one is never mutated by the archival it is the undo for.
    blocker = store.load(args.blocker_id)
    if blocker is None:
        print(f"error: no blocker with id {args.blocker_id!r}")
        return 1

    _, state = _load_state(config)
    if state is not None and blocker.session_id and blocker.session_id == state.session_id:
        print(
            f"error: blocker {blocker.id} belongs to the CURRENT session "
            f"({state.session_id}), which is still live. Archiving is for a "
            "blocker whose session has been retired — resolve it, or archive "
            "the session first (`reset --yes` keeps the task registry)."
        )
        return 1
    try:
        archived = store.archive_stale(args.blocker_id, args.reason)
    except StateError as exc:
        print(f"error: {exc}")
        return 1
    # The other half of the state, moved with the first. An archival that closed
    # the last record naming a quarantined task leaves that task `blocked` with
    # nothing left to justify it, which is the split brain blk-01 exists to make
    # impossible — not a tidiness job for whoever runs next. So it FAILS CLOSED:
    # a task graph that cannot be read, reconciled or saved puts `blocker` back
    # exactly as it was read, and this command reports no archival at all.
    released, _registry, outcome = _requeue_after_close(config, store, blocker)
    if outcome == _REQUEUE_REOPENED:
        print(f"blocker {args.blocker_id} was NOT archived — nothing changed.")
        return 1
    if outcome != _REQUEUE_OK:
        # The restore itself failed, so the archival really is on disk — the one
        # state this command cannot repair by itself, and the one it must not
        # describe as "nothing changed".
        print(
            f"blocker {args.blocker_id} remains CLOSED (archived) on disk and "
            "nothing was requeued — see the error above for what to do."
        )
        return 1
    # Only now, because an archival that cannot requeue is not an archival and
    # must not be announced as one.
    print(f"blocker {archived.id} archived at {archived.resolved_at}")
    print("recorded as a machine reason, NOT as an operator answer")
    _print_auto_unblocked(released)
    return 0


def pause_requested(config: AutoloopConfig) -> bool:
    """Is a pause in effect? Reads BOTH locations.

    `pause_file` moved outside the checkout (see its docstring), but a flag
    written by an older build sits at `legacy_pause_file`. Ignoring that one
    would leave the operator with a loop that keeps running after they asked
    it to stop — the failure mode a pause flag exists to prevent.
    """
    return config.pause_file.exists() or config.legacy_pause_file.exists()


def clear_pause(config: AutoloopConfig) -> bool:
    """Remove the flag from both locations. True if either existed."""
    cleared = False
    for path in (config.pause_file, config.legacy_pause_file):
        if path.exists():
            path.unlink()
            cleared = True
    return cleared


def _cmd_health(args: argparse.Namespace) -> int:
    """Judge the loop and exit 0 (fine) or 1 (needs you).

    Read-only and lock-free, so a scheduler may run it at any moment,
    including mid-round. The exit code is the contract: a cron wrapper only
    has to test it, and `--json` carries the reason for anything richer.
    """
    config = load_config(args.config)
    verdict = health.check(
        config,
        silence_minutes=args.silence_minutes,
        held_sweep_hours=args.held_sweep_hours,
    )
    if args.json:
        print(verdict.to_json())
    else:
        print(verdict.summary)
        if verdict.detail:
            print(f"  {verdict.detail}")
    return 1 if verdict.needs_attention else 0


def _window_git(config) -> GitGateway:
    """The gateway `merge-window` confirms publication through.

    A seam, for two reasons: the check must be testable without a network, and
    it is built lazily so a run with no candidate to verify makes no subprocess
    call at all. `Path.cwd()` matches every other gateway construction in this
    module — the operator runs `merge-window` from the checkout, as its own
    docstring shows.
    """
    return GitGateway(Path.cwd(), PolicyEngine(config.policy))


def _candidate_publication(config, record, seen=None, git=None) -> tuple[bool, str]:
    """Has this record's reviewed candidate already landed on its own remote
    branch? Returns `(published, why_not)`.

    `git`, when supplied, is the gateway to ask instead of building one from
    `_window_git`. It exists for `auto_merge.py`, which runs INSIDE the
    orchestrator and already holds a gateway rooted at the real checkout —
    `_window_git` builds against `Path.cwd()`, which is the operator's shell
    for the `merge-window` command and something else entirely for a loop
    process. Left as `None` by the CLI so `merge-window`'s own tests keep
    monkeypatching `_window_git` exactly as before.

    Fail-closed by construction: ONLY an `ls-remote` that comes back equal to
    `candidate_sha` counts as published. The record's own `intended_remote_ref`
    is not evidence — `orchestrator._dispatch_task_push` writes it BEFORE the
    network call on purpose ("durable push intent", so a crash between a
    successful push and the method returning is recoverable from the remote ref
    alone). A record whose push was REFUSED therefore carries exactly the same
    two fields as one whose push landed, and only the remote can tell them
    apart. Anything unverifiable — no remote configured, ls-remote failing,
    offline — reports not-published, which keeps the window shut.

    `seen` memoizes CONFIRMED publications for the life of one command
    invocation, and deliberately nothing else. `--wait` polls every 15s by
    default, so a long wait with three published candidates would otherwise
    re-ask the remote about all three forever — hundreds of round-trips per
    hour for an answer that cannot change (a published ref moving would be a
    force-push, which invalidates the candidate anyway, and the next invocation
    re-checks from scratch). The fail-closed branches make that worse than
    wasteful: throttle the remote enough and every lookup starts failing, at
    which point the wait talks itself into never opening. Negatives are never
    cached — an unpublished candidate becoming published is exactly the event
    `--wait` exists to notice.
    """
    candidate = str(record.get("candidate_sha") or "")
    remote = str(record.get("intended_remote") or "")
    dest_ref = str(record.get("intended_remote_ref") or "")
    if not remote or not dest_ref:
        return False, "never pushed"
    key = (remote, dest_ref, candidate)
    if seen is not None and key in seen:
        return True, ""
    gateway = git if git is not None else _window_git(config)
    try:
        landed = gateway.remote_ref_sha(remote, dest_ref)
    except (GitError, OSError) as exc:
        return False, f"could not verify {remote}/{dest_ref} ({exc})"
    if not landed:
        return False, f"{remote}/{dest_ref} does not exist"
    if landed != candidate:
        return False, f"{remote}/{dest_ref} is at {landed[:12]}, not the candidate"
    if seen is not None:
        seen.add(key)
    return True, ""


def _candidate_is_retired(config, registry, task_id, record, git) -> str:
    """Is this record a DEFECT rather than a hazard? A one-line reason if so,
    `""` if it must still be respected.

    A candidate holds the merge window shut because moving the base under it
    strands real, reviewed, unpublished work. Three things together prove that
    a given record is not describing such work, and all three are required:

    1. **The task is pending or dependency-blocked.** Not in progress, so no
       round is going to finish this candidate; the task will be redone from
       scratch when it is picked again. (Completed and operator-quarantined
       tasks never reach here — the caller exempts them first.)
    2. **The worker repo it names is gone.** `worktree_path` is recorded at
       dispatch and points at the ONLY place the candidate commit exists, so a
       path that was recorded and is no longer on disk means the commit has
       been moved out of reach (quarantined) or destroyed. An EMPTY
       `worktree_path` is not evidence of anything and is deliberately not
       accepted — "we never recorded where it was" is not "we know it is gone".
    3. **The checkout cannot resolve the candidate.** Asked affirmatively, and
       only ever answered from git: the repository has to prove it is readable
       (`head_sha`) before its "no such object" counts as an answer. Anything
       unverifiable — no repository, git unavailable, a probe that raises for
       any other reason — reports `""` and keeps the window shut, matching
       `_candidate_publication`'s fail-closed rule.

       That last clause is why the failure of `read_commit` is NOT the answer
       on its own. `cat-file commit` dies with the same status for a missing
       object, a corrupt one, an I/O error and a policy refusal, so "it raised"
       proves only that the question went unanswered — and writing a record off
       on it would hand the merge window an unreachable-looking candidate
       whenever the repository was merely having a bad day. So a raise here
       leads to ONE more question, `GitGateway.object_exists`, which answers
       True/False only from `cat-file -e`'s exit code and raises on anything
       ambiguous. Only an explicit False — git itself saying the object
       database does not hold this commit — writes the record off.

    That combination is what `release` leaves behind, and it is provably not
    in-flight. Fourteen such records held the window shut on 2026-08-15 (see
    `worktask.retire_execution`). `release` now retires the record itself, so
    this is the belt-and-braces for records that predate the fix or drifted
    some other way — which is why the caller reports it as a NOTE rather than
    swallowing it. A record that should have been retired is a defect worth
    seeing, not a thing to ignore silently.

    Ordered cheapest-first: registry lookup, then two filesystem checks, then
    git. A record that fails an earlier check never reaches a subprocess.
    """
    if not registry.has(task_id):
        # An id the registry has never heard of is not evidence of anything.
        return ""
    if registry.state_of(task_id) not in (TaskState.READY, TaskState.BLOCKED):
        return ""
    worktree_path = str(record.get("worktree_path") or "")
    if not worktree_path or Path(worktree_path).exists():
        return ""
    candidate = str(record.get("candidate_sha") or "")
    gateway = git if git is not None else _window_git(config)
    try:
        gateway.head_sha()
    except (GitError, OSError):
        return ""       # the repository cannot answer; that is not an answer
    try:
        gateway.read_commit(candidate)
        return ""       # resolvable here: a moved base could still strand it
    except (GitError, OSError):
        pass
    try:
        if gateway.object_exists(candidate):
            return ""   # the object is there; reading it merely failed
    except (GitError, OSError):
        return ""       # corruption, I/O, a policy refusal — still not an answer
    return (
        f"its worker repo {worktree_path} is gone and the checkout cannot "
        f"resolve {candidate[:12]}"
    )


#: Where a record's `task_base_sha` sits relative to the branch head RIGHT NOW,
#: as `_candidate_base_ancestry` reports it. Only `BASE_BEHIND` stops a record
#: from holding the merge window shut, and only ever by turning it into a note.
BASE_AT_HEAD = "at_head"        # the recorded base IS the head a merge would move
BASE_BEHIND = "behind"          # the head is already past it: a PROPER ancestor
BASE_UNVERIFIED = "unverified"  # git could not place it, or placed it nowhere


def _candidate_base_ancestry(config, record, git=None) -> tuple[str, str]:
    """Where does this record's `task_base_sha` sit relative to the branch head?
    Returns `(verdict, detail)`, the detail being a clause the caller splices
    into whatever it decides to say.

    This is the whole distinction the merge window turns on, and it exists
    because the two cases the old check treated as one are not the same harm:

    * **The base IS the head** (`BASE_AT_HEAD`). In-flight work about to be
      reviewed. Moving the head under it is exactly the `task_base_behind_head`
      failure — 17 blockers, the most common code in this system's history — so
      this one still holds the window shut.
    * **The base is a PROPER ancestor of the head** (`BASE_BEHIND`). The head
      moved past this candidate already; the record is in the state the guard
      exists to prevent, and has been for however many commits. Merging cannot
      inflict it a second time. Measured 2026-08-21: two such records (bases
      `eecae9c6` and `4964d400`, 10 and 12 commits behind head `23f6829d`) held
      the window shut on four finished, reviewed, published branches — two of
      them loop fixes that stay inert until merged, so the loop was being kept
      from its own repairs by a guard protecting work already past saving.
    * **Anything else** (`BASE_UNVERIFIED`). Fail closed, exactly as
      `_candidate_publication` does: an unanswerable question is never answered
      "safe". Four shapes reach it, and each carries its own detail so the
      operator can tell them apart — no recorded base at all, a checkout that
      will not name its head, a `merge-base` that failed, and a base git places
      OUTSIDE the head's history (a rewritten branch, or another history
      entirely). That last one is an answer rather than a failure, but it is not
      the affirmative "already behind" this exemption requires, so it blocks.

    **Asked affirmatively of the repository, and only of the repository.** No
    inference from timestamps, review rounds, or how old a record looks. Errors
    caught are `(GitError, OSError)` and nothing wider: a broad catch would make
    a typo in a gateway method name indistinguishable from "git could not
    answer", which would silently switch this exemption off and rebuild the
    exact bug it exists to fix, with every test still green.

    ONE `is_descendant` call, mirroring `orchestrator._rebase_execution_if_stale`
    (the only other implementation of this same question) line for line: plain
    string equality decides "same commit", `is_descendant(head, base)` decides
    "behind". `task_base_sha` is only ever written from `head_sha()`, so both
    sides are full 40-character shas and equality is exact; an abbreviated one
    would already be a worse hazard at that call site than at this one.

    `git`, when supplied, is the gateway to ask instead of building one from
    `_window_git` — same seam, and for the same reason, as
    `_candidate_publication`.
    """
    base = str(record.get("task_base_sha") or "")
    if not base:
        return BASE_UNVERIFIED, (
            "its record names no base at all, leaving nothing to place against "
            "the head"
        )
    gateway = git if git is not None else _window_git(config)
    try:
        head = gateway.head_sha()
    except (GitError, OSError) as exc:
        return BASE_UNVERIFIED, f"the checkout would not name its head ({exc})"
    if base == head:
        return BASE_AT_HEAD, f"that base IS the current head {head[:12]}"
    try:
        behind = gateway.is_descendant(head, base)
    except (GitError, OSError) as exc:
        return BASE_UNVERIFIED, (
            f"git could not place it against head {head[:12]} ({exc})"
        )
    if behind:
        return BASE_BEHIND, f"a proper ancestor of head {head[:12]}"
    return BASE_UNVERIFIED, (
        f"git places it OUTSIDE the history of head {head[:12]} — not an "
        "ancestor at all, which is a rewritten branch rather than ordinary drift"
    )


def _merge_window_blockers(config, seen=None, git=None) -> tuple[list[str], list[str]]:
    """Why merging into the loop's base is unsafe right now, plus advisory
    notes about work that is safe but not yet reconciled. `([], notes)` means
    the window is open.

    THE single predicate for "may the branch head move". `auto_merge.py` calls
    this rather than re-deriving the same conditions: a second implementation
    that drifted by one case is how thirteen tasks get parked at once. See
    `_candidate_publication` for what `git` is for.

    An execution record with a candidate BOUND TO THE CURRENT HEAD is the REAL
    hazard, and the one a phase check misses. It pins `task_base_sha`; moving
    the branch head under it strands the task — `orchestrator.
    _rebase_execution_if_stale` refuses to re-base a record whose
    `review_round > 0`, and parks it (`task_base_behind_head`) when it cannot
    carry the candidate forward, correctly, since a reviewer has already seen
    that candidate. Four tasks were stranded this way on 2026-08-02, every one
    of them by a merge that looked safe because no agent happened to be running
    at that instant.

    **"Bound to the current head" is the whole of it, and it is asked of git.**
    A record whose base is a PROPER ANCESTOR of the head is already behind:
    the head moved past it commits ago, so moving it again cannot inflict a
    state that is already true. Such a record is reported as a NOTE and does not
    block (`_candidate_base_ancestry`, which defines the three verdicts and why
    everything unverifiable still blocks). Measured 2026-08-21: two records 10
    and 12 commits behind the head held the window shut on four finished,
    reviewed, PUBLISHED branches — dash-16, roadmap-01, prof-01, bind-01 — for a
    day, two of which were loop fixes that stay inert until merged. Keeping the
    window shut restored neither stranded candidate; it only withheld the four.
    Narrowing this was reasonable only after base-02 (merged 2026-08-20) made a
    moving head survivable for a reviewed record — `_carry_reviewed_candidate_
    past` merges the head INTO the task branch and the round continues — but
    base-02 is not the justification on its own: it still bails on a dirty
    worker tree and on a merge conflict. The justification is that the harm is
    already done for exactly these records and for no others.

    Records outlive the work they describe. `release` retires one now (see
    `worktask.retire_execution`) and publication advances one, but neither did
    before, and a record can still outlast its work by other routes. Counting
    those would close the window permanently on finished work — dogfooding this
    command reported two such records the moment it was written, and on
    2026-08-15 fourteen released tasks' records held it shut on work that
    existed only inside quarantined worker repos — and a tool that cries wolf
    gets ignored, which is the failure it exists to prevent.

    Four exemptions, and the difference between them matters:

    * The task reached a terminal registry state (completed / quarantined).
    * The candidate is already PUBLISHED on its own side branch, confirmed
      against the remote. Its reviewed object is durable there and the operator
      merges it later as an ordinary branch, so moving the base cannot discard
      it. This is the exemption that keeps the window usable at all: nothing in
      the loop ever calls `TaskRegistry.mark_completed` (verified 2026-08-04 —
      the only callers are tests, and `Decision` has no terminal member for a
      reviewer to express "done"), so a task that publishes stays `in_progress`
      forever and the terminal-state exemption above never fires for it. Gating
      on a transition that has no producer closed the window permanently.
    * The record is a DEFECT rather than a hazard: its task is back in the
      queue AND its worker repo is gone AND the checkout cannot resolve the
      candidate (`_candidate_is_retired`, which explains each condition and
      why all three are needed). That is what a `release` used to leave
      behind, and it is provably not in-flight work — there is no reachable
      commit for a moved base to strand. Reported as a NOTE, never hidden: a
      record that should have been retired is worth seeing.
    * The record's base is ALREADY a proper ancestor of the head. Also a NOTE,
      for the same reason and by the same rule: the task will need a
      merge-forward or a recut before it can be reviewed again, and dropping it
      from the blockers must not drop it from the operator's view. Reported, not
      hidden; visible, not blocking.

    Nothing here changes the ALL-OR-NOTHING sweep. `merge_sweep` checks this
    predicate once for the whole backlog and merges every branch or none; this
    only changes what closes the window, never how the sweep behaves once it is
    open.

    The residual, reported as a note rather than hidden: a published record is
    still re-dispatchable, and a `revise` naming it after the base moves would
    park on `task_base_behind_head`. That park is recoverable exactly as its
    message says (publish, abandon, or archive the record) and does not risk
    the work — but it is a real consequence of merging, so it is printed.
    """
    reasons: list[str] = []
    notes: list[str] = []

    # `state_dir` is routinely a RELATIVE path (`.autoloop` in the shipped
    # config), so it resolves against the caller's cwd. Run from anywhere but
    # the checkout — a sibling worktree, a cron wrapper with its own working
    # directory — and the glob below finds nothing, which reads as "no in-flight
    # candidate" and prints OPEN. That is the exact false answer this command
    # exists to prevent, arrived at by reading the wrong directory. Hit while
    # dry-running this very change from a worktree on 2026-08-04.
    if not config.state_dir.is_dir():
        return [
            f"state directory {config.state_dir} does not exist (resolved from "
            f"{Path.cwd()}) — nothing could be read, so nothing can be called safe"
        ], notes

    _, registry = _load_tasks(config)
    executions = sorted(config.state_dir.glob("executions/*.json"))
    for path in executions:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        task_id = record.get("task_id") or path.stem
        if registry.has(task_id):
            state = registry.state_of(task_id)
            # RETIRED belongs here for the same reason the other two do: it is
            # a terminal registry state, and this docstring's own exemption
            # says so ("the task reached a terminal registry state"). It is
            # also a REGRESSION GUARD — these six tasks were stored as
            # `blocked` and exempted via BLOCKED_BY_OPERATOR, so giving
            # retirement its own status without listing it here would let a
            # superseded task's leftover execution record hold the merge
            # window shut permanently, on work nobody will ever finish.
            # SHIPPED_ELSEWHERE belongs here for exactly the reason RETIRED
            # does, and it is a regression guard in the same literal sense. The
            # five records ship-01 exists for were parked `blocked` — i.e.
            # exempted through BLOCKED_BY_OPERATOR — and recording them as
            # shipped elsewhere MOVES them out of that arm. Without this line,
            # converting a parked task to the honest record would REMOVE its
            # exemption, so a leftover execution record would start holding the
            # merge window shut on work that is already in the base and has no
            # branch to wait for. The task is terminal, nothing will dispatch it
            # again, and moving the base cannot strand it.
            if state in (
                TaskState.COMPLETED,
                TaskState.BLOCKED_BY_OPERATOR,
                TaskState.RETIRED,
                TaskState.SHIPPED_ELSEWHERE,
            ):
                continue
        if not record.get("candidate_sha"):
            continue
        published, why_not = _candidate_publication(config, record, seen, git)
        if published:
            # The residual differs by whether the RECORD knows what the remote
            # just told us. A record carrying a confirmed `published_sha` is
            # reconciled on the next revise (`orchestrator.
            # _reconcile_published_execution` re-asks the remote and retires it);
            # one written before that field existed still meets the old
            # `task_base_behind_head` park, so it is still reported as a park.
            reconcilable = record.get("published_sha") == record.get("candidate_sha")
            notes.append(
                f"task {task_id}: candidate {str(record.get('candidate_sha'))[:12]} is "
                f"published at {record.get('intended_remote')}/"
                f"{record.get('intended_remote_ref')} — safe to merge past, "
                + (
                    "and its record records that publication, so a later revise "
                    "reconciles it against the remote rather than parking"
                    if reconcilable
                    else "but its record does not record that publication, so a "
                    "later revise would park it"
                )
            )
            continue
        retired = _candidate_is_retired(config, registry, task_id, record, git)
        if retired:
            notes.append(
                f"task {task_id}: candidate {str(record.get('candidate_sha'))[:12]} "
                f"is NOT in flight — {retired}, and the task is back in the queue. "
                "The record should have been retired with its worker "
                "(`release` does this now); ignoring it for the window"
            )
            continue
        candidate = str(record.get("candidate_sha"))[:12]
        # `or "(none)"` reads the same way `_candidate_base_ancestry` does, so a
        # record with no base prints one legible sentence rather than "bound to
        # base  —" with a hole in it (or the bare `None` an absent key used to
        # render). It is the one branch where the reason is about the ABSENCE.
        base = str(record.get("task_base_sha") or "")[:12] or "(none)"
        # LAST, so the two exemptions above keep their existing meanings: a
        # released record that is also already behind stays the retirement note
        # it has always been, not this one.
        verdict, detail = _candidate_base_ancestry(config, record, git)
        if verdict == BASE_BEHIND:
            notes.append(
                f"task {task_id}: candidate {candidate} is bound to base "
                f"{base}, {detail} — it is ALREADY behind, so moving the head "
                "cannot strand it any further than it is. Not holding the "
                "window; it will need a merge-forward or a recut before it can "
                "be reviewed again (its next dispatch attempts the merge-forward "
                "and parks on task_base_behind_head if that refuses)"
            )
            continue
        reasons.append(
            f"task {task_id} has a candidate ({candidate}) bound to base "
            f"{base} — {why_not}; "
            + (
                f"{detail}, so merging would strand it"
                if verdict == BASE_AT_HEAD
                # Fail closed, and say so: this is not "already behind", it is
                # "cannot be shown to be", and the two must not read alike.
                else f"{detail}, so it is treated as bound to the head and "
                "merging would strand it"
            )
        )

    _, state = _load_state(config)
    if state is not None and Phase(state.phase) is Phase.EXECUTING:
        reasons.append("a phase is executing — an agent may be mid-write")

    return reasons, notes


def _cmd_merge_window(args: argparse.Namespace) -> int:
    """Is it safe to merge into the branch the loop builds against?

    Exists because the operator and the loop share one branch, and every
    merge into it while a task holds a candidate invalidates that task. The
    loop then refuses to rebase — correctly, since a reviewer has already seen
    the candidate — and parks. That happened four times in one day, each time
    because "no agent is running" was mistaken for "safe".

    Exit 0 = safe, 1 = not. With `--wait` it blocks until safe or the timeout
    expires, so the intended use is:

        git switch -c fix/whatever && ...work...
        python -m autoloop merge-window --wait && git switch <base> && git merge --ff-only fix/whatever
    """
    config = load_config(args.config)
    deadline = time.monotonic() + args.timeout
    seen: set = set()          # confirmed publications, for this invocation only
    while True:
        reasons, notes = _merge_window_blockers(config, seen)
        if not reasons:
            print("merge window OPEN — no unpublished candidate, no executing phase")
            for note in notes:
                print(f"  note: {note}")
            return 0
        if not args.wait or time.monotonic() > deadline:
            print("merge window CLOSED:")
            for reason in reasons:
                print(f"  - {reason}")
            for note in notes:
                print(f"  note: {note}")
            if not args.wait:
                print("\n`--wait` blocks until it opens.")
            else:
                print(f"\ngave up after {args.timeout:.0f}s.")
            return 1
        time.sleep(args.poll)


def _format_sweep(result: "merge_sweep.SweepResult") -> list[str]:
    """One block of operator-facing lines for a finished sweep, shared by the
    `merge-backlog` command and the startup hook so both describe the same
    outcome the same way."""
    lines = [f"merge backlog: {result.outcome}"]
    for task_id in result.merged:
        lines.append(f"  merged      {task_id}")
    for task_id, why in result.unresolved:
        lines.append(f"  UNJUDGED    {task_id} — {why}")
    if result.unresolved:
        # Never folded into the outcome line. "Could not look" and "looked,
        # nothing there" are the two answers this whole module exists to keep
        # apart, and the exit code says so too (`is_clear`). The per-task
        # reason above carries the CAUSE — an unanswering remote, a record that
        # would not load, a retirement over work that cannot be shown to have
        # landed — because each needs a different thing done about it.
        lines.append(
            f"  {len(result.unresolved)} completed task(s) could not be judged; "
            "each is named above with why. Every one of them is a branch that "
            "may still be outstanding — NOT the same as 'nothing to merge'. "
            "Fix what the reasons name and run this again."
        )
    if result.outcome == merge_sweep.HELD:
        lines.append(
            f"  nothing was merged; {len(result.pending)} branch(es) left "
            "untouched: " + (", ".join(result.pending) if result.pending else "(none)")
        )
        lines.append(
            "  a task named above could not be judged, and this sweep merges "
            "branches that may be DESCENDED from it — merging one of them would "
            "carry that unconfirmed work into the base without ever confirming "
            "it. Resolve what the reasons name, then run `merge-backlog` again."
        )
    if result.outcome == merge_sweep.DEFERRED:
        for reason in result.reasons:
            lines.append(f"  - {reason}")
        lines.append(
            f"  nothing was merged; {len(result.pending)} branch(es) still "
            "outstanding — the whole sweep waits for the window, never half of it"
        )
    if result.outcome == merge_sweep.STOPPED:
        lines.append(
            f"  STOPPED at  {result.stopped_on} ({result.stopped_outcome}) — "
            "see the transcript for the detail"
        )
        remaining = [t for t in result.pending if t != result.stopped_on]
        lines.append(
            f"  {len(remaining)} branch(es) left untouched: "
            + (", ".join(remaining) if remaining else "(none)")
        )
        # Only claimed when it was ESTABLISHED. Stopping is not by itself a
        # restoration: a merge that failed verification is deliberately left in
        # place, and a refused push leaves the base moved locally under the
        # `deferred` slug. `is_reconciled` is a probe of the checkout, not a
        # reading of the outcome — see `merge_sweep.py`'s "a stop is not
        # automatically a restoration". The block below says what to do about
        # it; this line only stops the report from asserting the opposite.
        if result.is_reconciled:
            lines.append(
                "  the base is exactly as it was before this branch. Resolve it by "
                "hand, then run `merge-backlog` again."
            )
        else:
            lines.append(
                "  the base is NOT as it was before this branch — HEAD was "
                f"{_short_sha(result.base_before)} when this started and is "
                f"{_short_sha(result.base_after)} now."
            )
    if result.outcome == merge_sweep.DISABLED:
        lines.append(
            "  policy.auto_merge_enabled is false — this command moves the "
            "branch head, so it is opt-in like the auto-merge it reuses"
        )
    if result.outcome == merge_sweep.NOTHING_TO_DO and not result.unresolved:
        lines.append(
            "  every completed task's published branch is already an ancestor "
            "of HEAD"
        )
    if not result.is_reconciled:
        # Last, so it reads as the conclusion of whatever the outcome block
        # above described, and shared across outcomes: a crash after a merge
        # (`failed`) needs the same reconciliation as a stop after one.
        lines.append(f"  UNRECONCILED {result.unreconciled}")
        lines.append(
            "  nothing here can put that back: `reset` is off the git whitelist "
            "by design, so an unverified or unpushed merge is reported, never "
            "undone. Reconcile it by hand (`git status`, `git log --oneline -5`) "
            "before running the loop or this command again — the loop will not "
            "start until you do."
        )
    return lines


def _short_sha(sha: str) -> str:
    """A sha for an operator, or an explicit marker that there was none to
    print. `""` here means the checkout would not answer, which is a different
    thing from a sha and must not render as an empty gap in the sentence."""
    return sha[:12] if sha else "(unreadable)"


def _cmd_merge_backlog(args: argparse.Namespace) -> int:
    """Merge every published-but-unmerged branch into the base, oldest first.

    The on-demand half of `merge_sweep.py`; `run` does the same thing at
    startup. Exists because publication is not integration and nothing used to
    look BACK: on 2026-08-06 seven completed tasks were published and unmerged
    at once, the base still at d2d4d6b, and it took a hand-written `ls-remote`
    loop to notice.

    Takes the loop lock — unlike `merge-window`, which only reports, this
    MOVES the branch head and pushes it, so it cannot run alongside a live
    loop.

    Exit 0 means the backlog is PROVABLY clear, and nothing weaker
    (`SweepResult.is_clear`): deferred, stopped, the flag off, or so much as one
    completed task the sweep could not JUDGE — an unconfirmed publication, a
    record it could not read, a retired record whose work it cannot show landed
    — all exit 1. Exiting 0 on any of those would be this command reporting "I
    looked, there is nothing there" for a run in which it could not look, which
    is the exact invisibility it exists to end.

    One unjudgeable task does more than change the exit code: it withholds the
    whole sweep (`merge_sweep.HELD`), because a branch that IS judgeable may be
    descended from it. `merge_sweep.py`'s "could not look" section has the
    reasoning and what it costs.

    A stop is reported as a restoration only when the checkout was OBSERVED to
    be where the stopping attempt found it. The two ways it is not — a merge
    that failed verification (left in place on purpose) and one whose push was
    refused (moved locally, absent from the remote, reported `deferred`) — print
    an UNRECONCILED block instead, and are the one sweep outcome that also stops
    `run` from starting the loop. The exit code does not change: neither was
    clear to begin with.
    """
    config = load_config(args.config)
    with LoopLock(config.state_dir):
        result = merge_sweep.sweep_backlog(config)
    for line in _format_sweep(result):
        print(line)
    return 0 if result.is_clear else 1


#: How a `shipped-report` row's state is labelled for an operator. The words are
#: pinned here rather than derived from the state slug because two of them carry
#: the distinction the whole report exists for: NO MENTION is not "not shipped",
#: and UNVERIFIED is not either.
SHIPPED_LABELS = {
    "shipped": "SHIPPED     ",
    "not-in-base": "NOT IN BASE ",
    "unverified": "UNVERIFIED  ",
    "unknown": "NO MENTION  ",
}

#: The same for a `shipped_elsewhere` record's own re-check. Separate table
#: because the words mean different things on the two sides: `VERIFIED` is a
#: claim about commits the RECORD names, while `SHIPPED` above is a claim about
#: commits a SEARCH found. `NO EVIDENCE` is not a weaker `INVALIDATED` — it means
#: the row names no commits at all, so there was nothing to ask git about.
SHIPPED_ELSEWHERE_LABELS = {
    "verified": "VERIFIED    ",
    "invalidated": "INVALIDATED ",
    "unverified": "UNVERIFIED  ",
    "unsupported": "NO EVIDENCE ",
}


def _format_shipped(report: dict) -> list[str]:
    """One block of operator-facing lines for a finished shipped-report.

    Every matching commit is printed with its OWN ancestry verdict, not just the
    aggregate: four `pkt-03, part N` commits are four lines of evidence, and a
    task whose evidence is split (one commit in the base, one not) must show
    both — the aggregate is a reading of the list, never a replacement for it.
    """
    from .dashboard import SHIPPED_STATES

    rows = report.get("rows") or []
    lines = [
        f"shipped report — base {report.get('base_branch')} at "
        f"{report.get('base_head') or '(unreadable)'}"
    ]
    if not report.get("searched"):
        lines.append(
            "  commit subjects could NOT be searched — every row below is "
            "unverified for that one reason, and none of them says anything "
            "about whether the work landed"
        )
    else:
        lines.append(
            f"  searched {report.get('searched_commits', 0)} commit subject(s) "
            "on every ref"
        )
    counts = report.get("counts") or {}
    lines.append(
        "  " + ", ".join(f"{state} {counts.get(state, 0)}" for state in SHIPPED_STATES)
    )
    if not rows:
        # NOT an early return any more. It used to be one, and since ship-01
        # that would skip the two blocks below — so a roadmap whose only
        # evidence-backed record had just been invalidated would print "no
        # completed task to report on" and stop, which is the report going
        # silent about the exact row it exists to surface.
        lines.append("  no completed task to report on")
    for row in rows:
        label = SHIPPED_LABELS.get(row.get("state") or "", "?           ")
        lines.append(f"  {label} {row.get('id')} — {row.get('title')}")
        lines.append(f"               {row.get('detail')}")
        for commit in row.get("commits") or ():
            lines.append(
                f"               {commit.get('ancestry') or '':<12} "
                f"{commit.get('sha')}  {commit.get('subject')}"
            )
    if rows:
        lines.append(
            "  NO MENTION means no commit subject names the id — it is not "
            "evidence that the work is missing, and this report never acts on "
            "any row."
        )
    lines.extend(_format_elsewhere(report))
    lines.extend(_format_disagreements(report))
    return lines


def _format_elsewhere(report: dict) -> list[str]:
    """The shipped-elsewhere half: every record's own carrying commits, each
    re-checked against THIS base head.

    Printed even when the subject search failed, because these rows do not
    depend on it — the record names its commits, so ancestry is asked directly.
    That asymmetry is worth the extra block: a report that went silent about the
    evidence-backed records whenever `git log --all` hiccupped would hide the
    one kind of row that is still perfectly answerable.
    """
    from .dashboard import SHIPPED_ELSEWHERE_STATES

    rows = report.get("elsewhere") or []
    if not rows:
        return ["  no task is recorded as shipped elsewhere"]
    counts = {
        state: sum(1 for row in rows if row.get("state") == state)
        for state in SHIPPED_ELSEWHERE_STATES
    }
    lines = [
        "  shipped elsewhere — records re-checked against this head, never "
        "trusted from when they were written",
        "  " + ", ".join(f"{state} {counts[state]}" for state in SHIPPED_ELSEWHERE_STATES),
    ]
    for row in rows:
        label = SHIPPED_ELSEWHERE_LABELS.get(row.get("state") or "", "?           ")
        lines.append(f"  {label} {row.get('id')} — {row.get('title')}")
        lines.append(f"               {row.get('detail')}")
        for commit in row.get("commits") or ():
            lines.append(
                f"               {commit.get('ancestry') or '':<12} "
                f"{commit.get('sha')}"
            )
    return lines


def _format_disagreements(report: dict) -> list[str]:
    """Where the registry and the base disagree, and what could not be judged.

    The `unverified` list is printed unconditionally when it is non-empty, right
    under the findings, because the failure this whole report guards against is
    "I could not look" reading as "there is nothing to see". An empty findings
    list with four unchecked rows above it must not print as a clean bill of
    health, so it does not.
    """
    disagreements = report.get("disagreements") or {}
    rows = disagreements.get("rows") or []
    unverified = disagreements.get("unverified") or []
    lines = [
        f"  registry / code disagreements: {len(rows)} "
        f"({disagreements.get('proven', 0)} proven)"
    ]
    for row in rows:
        strength = "PROVEN  " if row.get("proven") else "UNPROVEN"
        lines.append(f"  {strength} {row.get('kind')}  {row.get('id')}")
        lines.append(f"               {row.get('detail')}")
    for row in unverified:
        lines.append(
            f"  UNCHECKED {row.get('record')}  {row.get('id')} — "
            "no evidence either way, and never counted as agreeing"
        )
    lines.append(
        "  Nothing here is converted automatically: a disagreement is reported "
        "for a human, never resolved by changing the record that made it."
    )
    return lines


def _cmd_shipped_report(args: argparse.Namespace) -> int:
    """For every COMPLETED task: which commits name it, and are any in the base?

    Read-only and lock-free, deliberately — it may run alongside a live loop.
    It answers ONE question and claims nothing else: given a completed task id,
    is there a commit whose SUBJECT names that id, and is that commit an
    ancestor of the base head. It reads no source, decides nothing about
    capabilities, and writes nothing at all: no registry save, no execution
    record, no merge, no ref, no priority, no status. Nothing here retires,
    completes, reopens or unblocks a task on the strength of its own output.

    Exists because on 2026-08-17 four completed tasks held every merge sweep
    with no record naming the work they shipped, and all four were resolved by
    hand with exactly this query. See `dashboard.shipped_report`.

    The registry is read through the config, like every other read-only command
    here; the git half reads `--repo` (default: the current checkout), because
    the commits and the base head are properties of a checkout rather than of
    the state dir.

    Since ship-01 (2026-08-23) it also re-checks every `shipped_elsewhere`
    record's own carrying commits against this head, and prints where the two
    directions disagree. Still read-only, still never acting: a record whose
    evidence has stopped holding is REPORTED, never rewritten, completed or
    converted.

    Exit 0 means every completed task got an ANSWER and nothing provably
    disagrees. "No commit subject names this id" is a real answer to the
    question asked, so it does NOT on its own make the exit non-zero — that is
    the fail-open guard, kept: absence of a mention is absence of evidence, and
    an exit code that treated it as proof would be a licence to undo work that
    landed. Exit 1 means either something could not be judged (the search
    failed, git could not resolve a commit, a shipped-elsewhere record could not
    be checked) or the registry PROVABLY disagrees with the base — a recorded
    carrying commit that is not an ancestor, a shipped-elsewhere row naming no
    commits, or a completed task whose naming commits are all outside the base.
    "I could not look" and "I looked, and it does not hold" are both reasons to
    stop; only the second is a claim about the code.
    """
    from . import dashboard

    config = load_config(args.config)
    _, registry = _load_tasks(config)
    repo = args.repo or Path.cwd()
    head = dashboard.resolve_commit(repo, args.base)
    if not head:
        print(
            f"cannot resolve {args.base!r} in {repo} — the base head is what "
            "every row is judged against, so nothing is reported rather than "
            "reporting against a head nobody could read"
        )
        return 1
    branch = args.base
    roadmap = [
        {"id": task.id, "title": task.title, "status": task.status,
         # The evidence half. Read off the Task rather than re-derived, because
         # re-deriving it is what this record exists to stop: the shas the
         # operator recorded are what gets re-checked, not a fresh guess at
         # which commits might carry the work.
         "shipped_commits": list(task.shipped_commits),
         "shipped_note": task.shipped_note,
         "shipped_at": task.shipped_at}
        for task in registry.all_tasks()
    ]
    report = dashboard.shipped_report(repo, roadmap, head, branch)
    for line in _format_shipped(report):
        print(line)
    unjudged = report["counts"].get("unverified", 0)
    disagreements = report.get("disagreements") or {}
    # Anything other than `verified` on a shipped-elsewhere record: it either
    # provably no longer holds, or it could not be checked. Both are reasons to
    # stop, and neither may be rounded down to "the record is fine".
    unsettled_records = [
        row for row in (report.get("elsewhere") or ()) if row.get("state") != "verified"
    ]
    return (
        1
        if unjudged
        or not report.get("searched")
        or disagreements.get("proven")
        or unsettled_records
        else 0
    )


def _cmd_pause(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    config.pause_file.parent.mkdir(parents=True, exist_ok=True)
    config.pause_file.touch()
    print(
        "pause requested — a running orchestrator finishes its current phase "
        "and exits; `resume` clears the flag and continues"
    )
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if clear_pause(config):
        print("pause flag cleared")
    args.kickoff = None
    args.kickoff_audit = False
    args.answer = None
    args.retry = False
    args.resubmit = False
    args.max_steps = None
    args.null_executor = False
    return _cmd_run(args)


def _cmd_reset(args: argparse.Namespace) -> int:
    """Archive the SESSION. The roadmap survives unless asked for explicitly.

    `reset` used to archive `tasks.json` alongside the state, unprompted. The
    two have nothing to do with each other: the session is one conversation
    and its in-flight request, while the registry is the accumulated roadmap
    — imported audit findings, operator-set priorities, quarantine decisions.
    Reaching for `reset` to clear a wedged session therefore discarded work
    that had no bearing on the problem, and the only sign was one line of
    output after it had already happened.

    `--tasks` is the opt-in for the rare case that genuinely means it. Both
    archives are moves, not deletions, so a mistake is recoverable from the
    printed `.bak-<stamp>` path either way.
    """
    config = load_config(args.config)
    if not args.yes:
        target = "the current session state and the TASK REGISTRY" if args.tasks else (
            "the current session state (the task registry is kept — pass --tasks "
            "to archive it too)"
        )
        print(f"reset archives {target}; pass --yes to confirm")
        return 1
    stores = [("state", StateStore(config.state_file))]
    if args.tasks:
        stores.append(("tasks", TaskStore(config.tasks_file)))
    with LoopLock(config.state_dir):
        for label, store in stores:
            backup = store.archive()
            if backup is None:
                print(f"no {label} to reset")
            else:
                print(f"{label} archived to {backup}")
    if not args.tasks:
        print("task registry kept (--tasks archives it too)")
    print("transcript, manifests and audit runs kept as-is")
    return 0


def _cmd_dashboard(args: argparse.Namespace) -> int:
    """Serve the live tracker. Takes NO lock — it is meant to run alongside a
    live `run --continuous`, which is the only time it is useful, and `LoopLock`
    is held for the whole of such a run.

    Observation is read-only. It has exactly two write paths, neither of which
    can disturb the loop: a new task is queued to the inbox outside the
    checkout, and a task's PRIORITY is written straight into `tasks.json` under
    the fine-grained mutex the loop's own saves take (`tasks.task_file_mutex`),
    attested so the escape detector does not read it as an agent escape. It
    still writes nothing else in the state dir and never touches the working
    tree."""
    from .dashboard import main as dashboard_main

    repo = args.repo or Path.cwd()
    return dashboard_main(["--repo", str(repo), "--port", str(args.port)])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoloop")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_config(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", type=Path, default=DEFAULT_CONFIG)

    run = sub.add_parser("run", help="start or resume the loop")
    add_config(run)
    run.add_argument("--kickoff", help="file with the initial report (new sessions only)")
    run.add_argument(
        "--kickoff-audit",
        action="store_true",
        help="start a new session that offers ChatGPT the first repository audit",
    )
    run.add_argument(
        "--answer", help="answer to the operator question this run parked on"
    )
    run.add_argument("--retry", action="store_true", help="retry after a recoverable failure")
    run.add_argument(
        "--resubmit",
        action="store_true",
        help="authorize ONE more send of an ambiguous submission (same request id)",
    )
    run.add_argument("--max-steps", type=int, default=None, help="stop after N phase steps")
    run.add_argument(
        "--null-executor",
        action="store_true",
        help="dry run: record directives without executing them",
    )
    run.add_argument(
        "--continuous",
        action="store_true",
        help=(
            "loop indefinitely: resume any in-flight session, otherwise "
            "auto-select a ready task or run one audit per repository "
            "fingerprint change, sleeping locally when there is nothing to "
            "do (mutually exclusive with --kickoff/--kickoff-audit/--answer/"
            "--retry/--resubmit/--max-steps, which it manages itself)"
        ),
    )
    run.set_defaults(func=_cmd_run)

    release = sub.add_parser(
        "release",
        help=(
            "return a task stranded IN-PROGRESS by an interrupted round to "
            "pending, and move its stale worker repo to quarantine"
        ),
    )
    add_config(release)
    release.add_argument("task_id")
    release.set_defaults(func=_cmd_release)

    shelve = sub.add_parser(
        "shelve",
        help=(
            "return a task stranded IN-PROGRESS to pending and KEEP the round "
            "it holds — its execution record and worker repo stay put, so the "
            "next dispatch RESUMES that candidate instead of starting over "
            "(the sibling of `release`, which discards it)"
        ),
    )
    add_config(shelve)
    shelve.add_argument("task_id")
    shelve.set_defaults(func=_cmd_shelve)

    urgent = sub.add_parser(
        "urgent",
        help=(
            "make an existing task the next one dispatched, preempting the "
            "round in flight at the loop's next safe boundary (the displaced "
            "task returns to pending; review and publication are unchanged)"
        ),
    )
    add_config(urgent)
    urgent.add_argument("task_id")
    urgent.add_argument(
        "reason",
        help=(
            "why this cannot wait — required, so a preemption that discards a "
            "round's work is never unaccounted for"
        ),
    )
    urgent.set_defaults(func=_cmd_urgent)

    shipped_elsewhere = sub.add_parser(
        "record-shipped",
        help=(
            "record that a task's work is already in the base under ANOTHER "
            "task's commits (evidence-backed: every --commit is verified as an "
            "ancestor of --base before anything is queued)"
        ),
    )
    add_config(shipped_elsewhere)
    shipped_elsewhere.add_argument("task_id")
    shipped_elsewhere.add_argument(
        "--commit",
        action="append",
        required=True,
        metavar="REV",
        help=(
            "a commit that carries this work; repeatable. Any revision git can "
            "resolve — it is stored as the full sha, so the record means the "
            "same commit in every checkout"
        ),
    )
    shipped_elsewhere.add_argument(
        "--note",
        required=True,
        help=(
            "where the work landed, in words ('shipped under inbox-02's "
            "commits') — the commits say which, this says whose"
        ),
    )
    shipped_elsewhere.add_argument(
        "--repo", type=Path, default=None,
        help="checkout the commits are resolved and checked in (default: cwd)",
    )
    shipped_elsewhere.add_argument(
        "--base", default="HEAD",
        help="the base head each commit must be an ancestor of (default: HEAD)",
    )
    shipped_elsewhere.set_defaults(func=_cmd_record_shipped)

    retire = sub.add_parser(
        "retire",
        help=(
            "record that a task is superseded and will never be worked again "
            "(kept, never deleted — the successor ids are the record)"
        ),
    )
    add_config(retire)
    retire.add_argument("task_id")
    retire.add_argument(
        "--superseded-by",
        action="append",
        metavar="TASK_ID",
        help=(
            "the task that continues this one; repeatable. Omit when the task "
            "went stale rather than being replaced — an invented successor is "
            "worse than none"
        ),
    )
    retire.add_argument(
        "--reason",
        default="",
        help="why it was retired; the existing reason is kept when this is omitted",
    )
    retire.add_argument(
        "--rewrite-dependents",
        action="store_true",
        help=(
            "re-point every task that depends on this one, in the SAME operation: "
            "the retired id is replaced by whichever --superseded-by successors are "
            "live tasks, and dropped when none is. Needed only when the retirement "
            "would otherwise be refused for stranding them"
        ),
    )
    retire.set_defaults(func=_cmd_retire_task)

    archive_blocker = sub.add_parser(
        "archive-blocker",
        help=(
            "close a blocker whose session has been retired, recording a "
            "machine reason (never an operator answer)"
        ),
    )
    add_config(archive_blocker)
    archive_blocker.add_argument("blocker_id")
    archive_blocker.add_argument(
        "--reason",
        required=True,
        help="why this blocker is dead — required, so an archival is never a silent delete",
    )
    archive_blocker.set_defaults(func=_cmd_archive_blocker)

    window = sub.add_parser(
        "merge-window",
        help=(
            "is it safe to merge into the loop's branch? exit 0 = yes. A merge "
            "while a task holds a candidate strands that task"
        ),
    )
    add_config(window)
    window.add_argument("--wait", action="store_true", help="block until the window opens")
    window.add_argument("--timeout", type=float, default=7200, help="give up after N seconds")
    window.add_argument("--poll", type=float, default=15, help="seconds between checks")
    window.set_defaults(func=_cmd_merge_window)

    backlog = sub.add_parser(
        "merge-backlog",
        help=(
            "merge every completed task's published branch that is not yet in "
            "the base, oldest publication first, stopping at the first conflict"
        ),
    )
    add_config(backlog)
    backlog.set_defaults(func=_cmd_merge_backlog)

    shipped = sub.add_parser(
        "shipped-report",
        help=(
            "for every completed task: which commit subjects name it, and is "
            "any of them an ancestor of the base head? (read-only, no lock, "
            "never acts)"
        ),
    )
    add_config(shipped)
    shipped.add_argument(
        "--repo", type=Path, default=None,
        help="checkout whose commits are searched (default: cwd)",
    )
    shipped.add_argument(
        "--base", default="HEAD",
        help=(
            "the base head every matching commit is judged against "
            "(default: HEAD)"
        ),
    )
    shipped.set_defaults(func=_cmd_shipped_report)

    healthp = sub.add_parser(
        "health",
        help=(
            "is the loop working or stuck? read-only, no lock; exit 0 = fine, "
            "1 = needs attention (for cron/launchd)"
        ),
    )
    add_config(healthp)
    healthp.add_argument("--json", action="store_true", help="machine-readable verdict")
    healthp.add_argument(
        "--silence-minutes",
        type=float,
        default=health.DEFAULT_SILENCE_MINUTES,
        help=(
            "how long a live loop may write nothing before it counts as stuck "
            f"(default {health.DEFAULT_SILENCE_MINUTES:.0f}; an audit fan-out is "
            "legitimately quiet for 15+ minutes)"
        ),
    )
    healthp.add_argument(
        "--held-sweep-hours",
        type=float,
        default=health.DEFAULT_HELD_SWEEP_HOURS,
        help=(
            "how long the merge sweep may be held by work it cannot judge "
            f"before that needs attention (default "
            f"{health.DEFAULT_HELD_SWEEP_HOURS:.0f}; one held sweep is a phase "
            "boundary and clears itself, a hundred is an outage)"
        ),
    )
    healthp.set_defaults(func=_cmd_health)

    start = sub.add_parser(
        "start",
        help=(
            "repair what is provably safe (stale lock, dead browser, pause "
            "flag), report what needs a decision, then run continuously"
        ),
    )
    add_config(start)
    start.add_argument(
        "--check-only",
        action="store_true",
        help="repair and report, but do not start the loop",
    )
    start.set_defaults(func=_cmd_start)

    for name, func, help_text in (
        ("status", _cmd_status, "show session, lock and roadmap state (read-only)"),
        ("tasks", _cmd_tasks, "list the task graph with derived states (read-only)"),
        (
            "next-task",
            _cmd_next_task,
            "dry-run: print the task continuous mode would select next (read-only)",
        ),
        (
            "profile",
            _cmd_profile,
            "per-stage timing from the transcript (read-only, no lock)",
        ),
        ("doctor", _cmd_doctor, "non-destructive preflight checks (never submits)"),
        (
            "smoke-browser",
            _cmd_smoke_browser,
            "RETIRED: smoked the browser transport, which is no longer "
            "registered — prints why and exits 2, running nothing",
        ),
        ("unlock", _cmd_unlock, "remove a verifiably-stale lock (refuses live locks)"),
        ("pause", _cmd_pause, "ask a running loop to stop after its current phase"),
        ("resume", _cmd_resume, "clear the pause flag and continue the loop"),
    ):
        p = sub.add_parser(name, help=help_text)
        add_config(p)
        if name == "profile":
            p.add_argument(
                "--transcript",
                type=Path,
                default=None,
                help="profile an archived transcript instead of the configured one",
            )
        if name == "smoke-browser":
            # Still ACCEPTED, and deliberately inert, since brw-16 (2026-08-25).
            # The command is retired; the flag survives only so that a typed
            # `smoke-browser --provider browser_chatgpt` — the invocation a
            # runbook or a shell history holds — reaches the retirement notice
            # instead of dying on argparse's "unrecognized arguments" with the
            # same exit code and none of the explanation.
            p.add_argument(
                "--provider",
                default="",
                help="ignored — the command is retired and smokes nothing",
            )
        p.set_defaults(func=func)

    add_task = sub.add_parser(
        "add-task",
        help="queue a new task for the loop (safe at any time, even mid-run)",
    )
    add_config(add_task)
    add_task.add_argument("--id", required=True, help="stable slug id")
    add_task.add_argument("--title", required=True)
    add_task.add_argument("--description", required=True)
    add_task.add_argument(
        "--priority", type=int, default=100,
        help="ascending: 1 outranks 2; default 100 sorts last",
    )
    add_task.add_argument("--depends-on", action="append", default=[])
    add_task.add_argument(
        "--approved-path", action="append", default=[],
        help="repeatable; the exact paths this task may touch",
    )
    add_task.add_argument("--validation", action="append", default=[],
                          help='repeatable, e.g. --validation "ruff check ."')
    add_task.add_argument("--validation-cwd", default="")

    # ---- intake: an idea, interviewed into a draft ---------------------------
    # AUTHORING TIME ONLY. `ask`, `suggest` and `plan` refuse while a round is
    # live (`inbox.refuse_if_round_running`); `new`, `show`, `list` and
    # `submit` are safe at any moment, exactly like `add-task`, because they
    # touch nothing inside the checkout. `submit` is the only one that queues.
    intake = sub.add_parser(
        "intake",
        help=(
            "turn a rough idea into a DRAFT task by question and answer "
            "(authoring-time; nothing is filed until you submit)"
        ),
    )
    intake_sub = intake.add_subparsers(dest="intake_cmd", required=True)

    intake_new = intake_sub.add_parser(
        "new", help="start a draft from typed text or a .md/.txt file"
    )
    add_config(intake_new)
    intake_new.add_argument("--id", default="", help="draft name (defaults to the file stem)")
    intake_new.add_argument("--text", default="", help="the idea, in a sentence or two")
    intake_new.add_argument(
        "--file", default="", help="a .md/.txt file holding the idea"
    )

    for name, help_text in (
        ("ask", "one interview pass: add questions and evidence, re-read answers"),
        ("show", "print the draft file"),
        ("submit", "file the draft through the inbox (the ONLY step that queues)"),
        ("plan", "split a ready draft into ONE level of tasks"),
    ):
        p = intake_sub.add_parser(name, help=help_text)
        add_config(p)
        p.add_argument("--id", required=True, help="the draft name")
        if name == "ask":
            p.add_argument(
                "--no-model",
                action="store_true",
                help=(
                    "add only the questions this module holds as constants; "
                    "ask no model at all"
                ),
            )
        if name == "submit":
            p.add_argument(
                "--dry-run",
                action="store_true",
                help="print what would be queued and queue nothing",
            )

    intake_list = intake_sub.add_parser("list", help="every draft and whether it is ready")
    add_config(intake_list)

    intake_suggest = intake_sub.add_parser(
        "suggest", help="two or three cited things worth doing, so you face no blank page"
    )
    add_config(intake_suggest)
    intake_suggest.add_argument(
        "--limit", type=int, default=MAX_WORK_SUGGESTIONS,
        help="how many to offer; small on purpose — this is a choice, not a list",
    )

    for name, help_text in (
        ("accept", "start a draft from an offered suggestion"),
        ("decline", "never offer this again unless its evidence changes"),
    ):
        p = intake_sub.add_parser(name, help=help_text)
        add_config(p)
        p.add_argument("key", help="the suggestion key printed by `intake suggest`")
        if name == "accept":
            p.add_argument("--id", default="", help="draft name (defaults to the key)")

    intake.set_defaults(func=_cmd_intake)

    blockers = sub.add_parser(
        "blockers", help="list open operator-facing blockers (read-only, no lock)"
    )
    add_config(blockers)
    blockers.add_argument(
        "--all", action="store_true", help="also show resolved blockers"
    )
    drain = sub.add_parser(
        "drain-inbox",
        help="merge queued task requests into the roadmap without running a step",
    )
    add_config(drain)
    drain.set_defaults(func=_cmd_drain_inbox)

    add_task.set_defaults(func=_cmd_add_task)
    blockers.set_defaults(func=_cmd_blockers)

    answer = sub.add_parser(
        "answer",
        help="resolve a blocker and, if task_fatal, make its task READY again",
    )
    add_config(answer)
    answer.add_argument("blocker_id", help="the blocker id, e.g. blk-t1-001")
    answer.add_argument("text", help="the operator's answer/decision")
    answer.set_defaults(func=_cmd_answer)

    dash = sub.add_parser(
        "dashboard",
        help=(
            "serve the live tracker on localhost (no lock; reads only, except "
            "an immediate task-priority edit)"
        ),
    )
    add_config(dash)
    dash.add_argument("--port", type=int, default=8787)
    dash.add_argument(
        "--repo", type=Path, default=None,
        help="checkout whose .autoloop/ to read (default: cwd)",
    )
    dash.set_defaults(func=_cmd_dashboard)

    reset = sub.add_parser(
        "reset", help="archive the session state (keeps the task registry)"
    )
    add_config(reset)
    reset.add_argument("--yes", action="store_true")
    reset.add_argument(
        "--tasks",
        action="store_true",
        help="ALSO archive the task registry (the roadmap: imported findings, "
        "priorities, quarantine decisions). Off by default — a wedged session "
        "is not a reason to discard the roadmap.",
    )
    reset.set_defaults(func=_cmd_reset)

    reprovision = sub.add_parser(
        "reprovision-publisher",
        help="re-snapshot the publisher's remote url from the main checkout (explicit, confirmed only)",
    )
    add_config(reprovision)
    reprovision.add_argument(
        "--confirm",
        action="store_true",
        help="required — the ONLY way the publisher url snapshot changes",
    )
    reprovision.set_defaults(func=_cmd_reprovision_publisher)

    review_changeset = sub.add_parser(
        "review-changeset",
        help=(
            "queue an operator-authored changeset already committed on this "
            "branch for ChatGPT review, bound to an exact base/candidate sha"
        ),
    )
    add_config(review_changeset)
    review_changeset.add_argument("--base", required=True, help="base commit sha (40-hex)")
    review_changeset.add_argument(
        "--candidate", required=True, help="candidate commit sha (40-hex)"
    )
    review_changeset.add_argument(
        "--packet",
        default=None,
        help=(
            "use this file's text as the packet body instead of the "
            "git-rendered diff (the branch/dest_ref/base_sha/candidate_sha "
            "header always comes from git, never from this file)"
        ),
    )
    review_changeset.set_defaults(func=_cmd_review_changeset)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (AutoloopError, InboxError, IntakeError) as exc:
        # `InboxError` and `IntakeError` are deliberately NOT `AutoloopError`
        # subclasses — they are refusals of an operator REQUEST, not loop
        # faults — but they are still messages written for the person at the
        # prompt, and a traceback is not one.
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
