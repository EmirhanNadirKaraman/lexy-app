"""Command-line interface for autoloop.

    python -m autoloop run [--config PATH] [--kickoff FILE | --kickoff-audit |
                            --answer TEXT | --retry] [--null-executor]
                            [--continuous] [--max-steps N]
    python -m autoloop status | tasks | doctor | next-task | blockers [--all]
                                                               (read-only, no lock)
    python -m autoloop answer <blocker-id> "<text>"
    python -m autoloop retire <task-id> [--superseded-by ID ...] [--reason TEXT]
    python -m autoloop smoke-browser [--config PATH]
    python -m autoloop pause | resume | unlock | reset --yes [--tasks]
    python -m autoloop merge-window [--wait] | merge-backlog
    python -m autoloop reprovision-publisher --confirm
    python -m autoloop review-changeset --base <sha> --candidate <sha> [--packet FILE]

Locking: run / resume / reset / smoke-browser / answer / retire / release /
merge-backlog take
the single-instance lock on the state directory (fail closed against a live
process; `unlock` is the only stale-lock recovery, and it refuses live locks).
status / tasks / doctor / next-task / blockers / pause / merge-window stay
available while locked — they only report. `merge-backlog` moves the branch
head, so it does not.

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
from .blockers import NO_TASK, Blocker, BlockerStore
from .changeset_review import build_changeset_binding, build_changeset_packet
from .config import AutoloopConfig, load_config as _read_config_file
from .contract import AUDIT_TASK_ID, Decision, Directive
from .conversation import create_conversation
from . import health, heartbeat
from .doctor import DoctorProbes, _default_probe_cdp, exit_code, run_doctor
from .errors import (
    AutoloopError,
    ConfigError,
    ExecutorError,
    GitError,
    StateCorruptError,
    StateError,
    TaskGraphError,
)
from .executor import ExecutionOutcome, NullExecutor, TaskExecutor
from .git_gateway import GitGateway
from .implement_executor import ImplementExecutor, implement_agent_runner
from .inbox import InboxError, TaskInbox, apply_requests, inbox_dir_for
from .auto_merge import (
    UPGRADE_EXEC_FAILED,
    UPGRADE_EXECED,
    UPGRADE_PENDING,
    UPGRADE_PREFLIGHT_FAILED,
    UPGRADE_UNAPPLICABLE,
    PendingUpgrade,
    UpgradeStore,
)
from .lock import LoopLock
from .manifest import ManifestStore, snapshot as manifest_snapshot
from . import merge_sweep
from .orchestrator import SELF_UPGRADE, Orchestrator
from .policy import PolicyConfig, PolicyEngine
from .prompts import TEMPLATES, kickoff_payload, user_answer_payload
from .publisher import (
    Publisher,
    provision_publisher_repo,
    read_publisher_url_snapshot,
    redact_url,
    reprovision_publisher as _reprovision_publisher_snapshot,
)
from .stall import StallPolicy
from .state import TERMINAL_PHASES, LoopState, Phase, StateStore, utcnow_iso
from .tasks import Task, TaskRegistry, TaskState, TaskStore, mutation_ledger_for
from .transcript import TranscriptLogger
from .validation_env import load_validation_env
from .worker_env import WorkerRepoManager, validate_workers_root, verify_worker_isolation
from .worktask import IntentStore, TaskExecutionStore, retire_execution

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
    if state is not None and state.conversation_url != config.browser.conversation_url:
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
        # it covers both `run` paths. `smoke-browser` builds its own
        # orchestrator against the same state dir and would otherwise report a
        # false failure over an upgrade it cannot perform — see the constructor.
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
    # succeeded. Same reasoning as `_cmd_smoke_browser`'s positive `stop_kind`
    # gate; `_report_fault_stop` prints the summary itself, so this returns
    # rather than falling through to a second one.
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
    print(
        f"continuous mode: exhausted — no ready task, the repository fingerprint "
        f"is unchanged, and {len(blockers)} blocker(s) are still open:"
    )
    for b in sorted(blockers, key=lambda b: b.created_at):
        print(f"  {b.id}  task={b.task_id}  code={b.code}  {b.question}")
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
        _start_new_session(config, store)
        return True

    fingerprint = repo_fingerprint(Path.cwd())
    if fingerprint == _load_fingerprint(config):
        return False  # unchanged since the last audit — sleep, make no calls
    _save_fingerprint(config, fingerprint)
    _start_new_session(config, store)
    return True


def _start_new_session(config: AutoloopConfig, store: StateStore) -> None:
    state = LoopState.new(config.browser.conversation_url)
    state.outbox = TEMPLATES["audit_kickoff"].render()
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
    return "\n".join(lines)


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
    blockers = store.all_blockers() if args.all else store.open_blockers()
    if not blockers:
        print("no blockers recorded" if args.all else "no open blockers")
        return 0
    for b in sorted(blockers, key=lambda b: b.created_at):
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
    """Resolve a blocker with the operator's answer and, for a `task_fatal`
    blocker, unblock the task it quarantined so it becomes READY again.
    Takes the lock (like `run`/`reset`) — it mutates `tasks.json`, and must
    not race a live `run --continuous`."""
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
        blocker = blocker_store.resolve(args.blocker_id, args.text)  # raises on unknown/resolved
        print(f"blocker {blocker.id} resolved.")
        if blocker.kind != "task_fatal" or blocker.task_id == NO_TASK:
            print("(not tied to a quarantined task — nothing else to do.)")
            return 0
        # BEFORE the unblock, deliberately. `registry.unblock` raises
        # `TaskGraphError` for a task that is not BLOCKED_BY_OPERATOR, and plain
        # `run` (unlike `--continuous`) parks task_fatal without ever going
        # through `cli._handle_parked_task`, so that is a live shape — the early
        # return below would skip the budget reset in precisely the case it
        # exists for, leaving the operator editing the record by hand again.
        # Harmless for a RETIRED task: one that never dispatches never reads
        # either counter.
        _clear_fault_budget_on_answer(config, blocker)
        task_store, registry = _load_tasks(config)
        try:
            registry.unblock(blocker.task_id)
        except TaskGraphError as exc:
            print(
                f"task {blocker.task_id!r} could not be unblocked ({exc}) — the "
                "blocker itself is still resolved."
            )
            return 0
        task_store.save(registry)
        print(f"task {blocker.task_id} is ready again.")
    return 0


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


class _SmokeNeverExecutor:
    """The smoke test must never execute tasks — fail loud if dispatch tries."""

    def execute(self, directive, task):
        raise ExecutorError(
            "smoke test attempted to execute a task — this must never happen"
        )


def _cmd_smoke_browser(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    smoke_provider = getattr(args, "provider", None) or "browser_chatgpt"
    with LoopLock(config.state_dir):
        store = StateStore(config.smoke_dir / "state.json")
        store.archive()  # every smoke run starts fresh
        state = LoopState.new(config.browser.conversation_url)
        state.outbox = TEMPLATES["smoke_test"].render()
        store.save(state)
        # A smoke test must fail fast and must not grind through retries: one
        # browser failure ends it, and the reply bounds are minutes, not the
        # 15-minute audit-grade ceiling.
        # A smoke test is exactly ONE round-trip: one message, one reply. No
        # corrective re-prompts (parse retries), no second iteration, one
        # browser failure ends it. A malformed reply is a smoke FAILURE, not
        # something to negotiate over several messages in a reserved channel.
        smoke_policy = dataclasses.replace(
            config.policy,
            max_iterations=1,
            max_consecutive_failures=1,
            max_parse_retries=0,
            max_policy_denials=0,
        )
        smoke_config = dataclasses.replace(
            config,
            browser=dataclasses.replace(
                config.browser,
                response_start_timeout_seconds=min(
                    config.browser.response_start_timeout_seconds, 90.0
                ),
                response_timeout_seconds=min(config.browser.response_timeout_seconds, 120.0),
            ),
        )
        policy = PolicyEngine(smoke_policy)
        orchestrator = Orchestrator(
            config=smoke_config,
            store=store,
            state=state,
            policy=policy,
            git=GitGateway(Path.cwd(), policy),
            executor=_SmokeNeverExecutor(),
            transcript=TranscriptLogger(config.transcript_file),
            # `--provider`, which defaults to the browser rather than to
            # `conversation.provider` — see the parser. Smoke-testing the
            # browser is this command's purpose, and since Codex became the
            # primary reviewer the configured provider is no longer the one
            # that needs proving.
            client_factory=lambda: create_conversation(smoke_provider, smoke_config),
            registry=TaskRegistry(),
            task_store=TaskStore(config.smoke_dir / "tasks.json"),
            manifest_store=ManifestStore(config.smoke_dir / "manifests"),
        )
        outcome = orchestrator.run()
    # BOTH conditions, and the second is load-bearing rather than belt-and-
    # braces: `stopped` is reachable two ways since the policy-denial budget
    # started ending the run instead of parking (`orchestrator._to_fault_stop`),
    # and the smoke policy sets `max_policy_denials=0`, so the FIRST denied
    # reply reaches that terminal. Phase alone would therefore report PASS —
    # "received a valid contract stop" — for a reviewer that answered
    # `ask_user`, `implement`, or a commit approval, which is the exact
    # misbehaviour this command exists to catch. Gated on the POSITIVE value so
    # an unclassified stop (a legacy state file, a future fault site nobody
    # classified) reads as a failure.
    if outcome == Phase.STOPPED.value and state.stop_kind == "contract":
        print(
            "smoke-browser: PASS — submitted one request through the real "
            f"conversation and received a valid contract stop ({state.stop_reason!r})"
        )
        return 0
    # Named in the failure line because "ended in 'stopped'" is otherwise the
    # same words a PASS would print, and the operator's next question is
    # exactly which kind it was.
    kind = f" ({state.stop_kind} stop)" if state.stop_kind else ""
    print(
        f"smoke-browser: FAIL — loop ended in '{outcome}'{kind} "
        f"(question: {state.question!r}, stop_reason: {state.stop_reason!r}). "
        f"Diagnostics (if any) under {config.diagnostics_dir}."
    )
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
    open_blockers = BlockerStore(config.blockers_dir).open_blockers()
    if open_blockers:
        ok = False
        lines.append(f"blockers     {len(open_blockers)} OPEN — each needs a decision:")
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
        _, _start_registry = _load_tasks(config)
        _closed_blockers = _reconcile_retired_blockers(config, _start_registry)
    except (StateError, ConfigError, TaskGraphError, KeyError) as exc:
        print(f"tasks        UNREADABLE ({exc}) — retirements not reconciled")
        ok = False
    else:
        for _closed in _closed_blockers:
            print(
                f"blockers     {_closed.id} closed — task {_closed.task_id} is "
                "retired, so nobody can answer it"
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
    """
    config = load_config(args.config)
    with LoopLock(config.state_dir):
        task_store, registry = _load_tasks(config)
        try:
            task = registry.release(args.task_id)
        except TaskGraphError as exc:
            print(f"error: {exc}")
            return 1
        task_store.save(registry)
        print(f"task {task.id} released: in_progress -> pending")

        worker_repos = WorkerRepoManager(config.workers_root, config.worker_hooks_dir)
        retired = retire_execution(
            args.task_id, TaskExecutionStore(config.executions_dir), worker_repos
        )
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
    """
    config = load_config(args.config)
    with LoopLock(config.state_dir):
        task_store, registry = _load_tasks(config)
        previous = registry.get(args.task_id).status if registry.has(args.task_id) else ""
        try:
            task = registry.retire(
                args.task_id,
                superseded_by=tuple(args.superseded_by or ()),
                reason=args.reason or "",
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
    """
    config = load_config(args.config)
    store = BlockerStore(config.blockers_dir)
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
    print(f"blocker {archived.id} archived at {archived.resolved_at}")
    print("recorded as a machine reason, NOT as an operator answer")
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
    verdict = health.check(config, silence_minutes=args.silence_minutes)
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


@dataclasses.dataclass(frozen=True)
class UnansweredWindowCheck:
    """A git or remote question the window check ASKED and could not get an
    answer to.

    The merge window is deliberately fail-closed: an `ls-remote` that will not
    answer counts as "not published", which keeps the window shut and keeps the
    loop from merging on the strength of a failure. That is the right thing for
    a MERGE, and the wrong thing to show an operator without qualification —
    "task X is holding the window" and "we could not find out whether task X is
    holding the window" are different claims, and only one of them is worth
    acting on.

    So the failure is also recorded HERE, on a structured channel, for callers
    that need to tell the two apart. Structured rather than sniffed out of the
    reason text on purpose: the reason strings are prose, they have been
    reworded twice, and a reader that pattern-matched them would silently start
    reporting a blocker as a failure (or the reverse) the next time someone
    fixed a comma. `dashboard.merge_window` is the only consumer today and uses
    it to render `unknown` instead of `closed`.

    Nothing about the merge decision changes with it. The reason is still
    appended, the window is still shut, and a caller that passes no sink gets
    byte-identical output to before.
    """

    task_id: str
    question: str
    detail: str

    def __str__(self) -> str:
        return f"task {self.task_id}: {self.question} — {self.detail}"


def _note_unanswered(sink, task_id, question, exc) -> None:
    """Append one `UnansweredWindowCheck` to `sink`, or do nothing at all.

    `sink is None` is the default for every merge-path caller, so the recording
    is genuinely free for them — no allocation, no formatting of an exception
    they will not read.
    """
    if sink is None:
        return
    sink.append(UnansweredWindowCheck(
        task_id=str(task_id or "<unnamed record>"),
        question=question,
        detail=f"{type(exc).__name__}: {exc}",
    ))


def _candidate_publication(config, record, seen=None, git=None,
                           unanswered=None) -> tuple[bool, str]:
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

    `unanswered`, when supplied, is a list this appends an
    `UnansweredWindowCheck` to whenever the remote raises rather than answers.
    It changes NOTHING about the verdict — the reason is still returned and the
    window is still shut — and exists so a read-only caller can say "could not
    find out" where the merge path says "not published". A missing remote name
    or ref is NOT recorded: "never pushed" is an answer, arrived at without
    asking anyone.

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
        _note_unanswered(
            unanswered, record.get("task_id"),
            f"the remote could not be asked whether {remote}/{dest_ref} carries "
            "this candidate", exc,
        )
        return False, f"could not verify {remote}/{dest_ref} ({exc})"
    if not landed:
        return False, f"{remote}/{dest_ref} does not exist"
    if landed != candidate:
        return False, f"{remote}/{dest_ref} is at {landed[:12]}, not the candidate"
    if seen is not None:
        seen.add(key)
    return True, ""


def _candidate_is_retired(config, registry, task_id, record, git,
                          unanswered=None) -> str:
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

    `unanswered` is the same structured sink `_candidate_publication` takes, and
    the two "not an answer" branches above fill it: a checkout that cannot
    report its own head, and an `object_exists` probe that raises. Both of them
    keep the record — and so the window — exactly as they always did; the sink
    only lets a read-only caller say the answer was not had rather than
    reporting the fail-closed guess as a finding. A resolvable candidate is an
    ANSWER and records nothing.

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
    except (GitError, OSError) as exc:
        # the repository cannot answer; that is not an answer
        _note_unanswered(unanswered, task_id,
                         "the checkout could not be read at all", exc)
        return ""
    try:
        gateway.read_commit(candidate)
        return ""       # resolvable here: a moved base could still strand it
    except (GitError, OSError):
        pass
    try:
        if gateway.object_exists(candidate):
            return ""   # the object is there; reading it merely failed
    except (GitError, OSError) as exc:
        # corruption, I/O, a policy refusal — still not an answer
        _note_unanswered(
            unanswered, task_id,
            f"the checkout could not say whether {candidate[:12]} is present",
            exc,
        )
        return ""
    return (
        f"its worker repo {worktree_path} is gone and the checkout cannot "
        f"resolve {candidate[:12]}"
    )


def _merge_window_blockers(config, seen=None, git=None,
                           unanswered=None) -> tuple[list[str], list[str]]:
    """Why merging into the loop's base is unsafe right now, plus advisory
    notes about work that is safe but not yet reconciled. `([], notes)` means
    the window is open.

    THE single predicate for "may the branch head move". `auto_merge.py` calls
    this rather than re-deriving the same conditions: a second implementation
    that drifted by one case is how thirteen tasks get parked at once. See
    `_candidate_publication` for what `git` is for.

    An execution record with a candidate is the REAL hazard, and the one a
    phase check misses. It pins `task_base_sha`; moving the branch head under
    it strands the task — `orchestrator._rebase_execution_if_stale` refuses to
    re-base a record whose `review_round > 0` and parks it
    (`task_base_behind_head`), correctly, since a reviewer has already seen
    that candidate. Four tasks were stranded this way on 2026-08-02, every one
    of them by a merge that looked safe because no agent happened to be running
    at that instant.

    Records outlive the work they describe. `release` retires one now (see
    `worktask.retire_execution`) and publication advances one, but neither did
    before, and a record can still outlast its work by other routes. Counting
    those would close the window permanently on finished work — dogfooding this
    command reported two such records the moment it was written, and on
    2026-08-15 fourteen released tasks' records held it shut on work that
    existed only inside quarantined worker repos — and a tool that cries wolf
    gets ignored, which is the failure it exists to prevent.

    Three exemptions, and the difference between them matters:

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

    The residual, reported as a note rather than hidden: a published record is
    still re-dispatchable, and a `revise` naming it after the base moves would
    park on `task_base_behind_head`. That park is recoverable exactly as its
    message says (publish, abandon, or archive the record) and does not risk
    the work — but it is a real consequence of merging, so it is printed.

    `unanswered` is a THIRD, optional channel, and it is not part of the
    verdict. Pass a list and it collects one `UnansweredWindowCheck` per git or
    remote question that raised instead of answering; pass nothing (every merge
    caller does) and the behaviour is unchanged down to the byte. It exists
    because this function is fail-closed by design — an unreachable remote
    becomes a reason, which is right for deciding whether to MERGE and
    misleading as a report, since "task X is holding the window" and "we could
    not find out whether task X is holding the window" are different claims.
    `dashboard.merge_window` reads the sink to render `unknown` rather than
    `closed`; it deliberately does not, and must not, re-read the reason strings
    to work that out.
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
            if state in (
                TaskState.COMPLETED,
                TaskState.BLOCKED_BY_OPERATOR,
                TaskState.RETIRED,
            ):
                continue
        if not record.get("candidate_sha"):
            continue
        published, why_not = _candidate_publication(config, record, seen, git,
                                                    unanswered)
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
        retired = _candidate_is_retired(config, registry, task_id, record, git,
                                        unanswered)
        if retired:
            notes.append(
                f"task {task_id}: candidate {str(record.get('candidate_sha'))[:12]} "
                f"is NOT in flight — {retired}, and the task is back in the queue. "
                "The record should have been retired with its worker "
                "(`release` does this now); ignoring it for the window"
            )
            continue
        reasons.append(
            f"task {task_id} has a candidate "
            f"({str(record.get('candidate_sha'))[:12]}) bound to base "
            f"{str(record.get('task_base_sha'))[:12]} — {why_not}; "
            "merging would strand it"
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
        ("doctor", _cmd_doctor, "non-destructive preflight checks (never submits)"),
        (
            "smoke-browser",
            _cmd_smoke_browser,
            "submit ONE harmless smoke request through the live browser",
        ),
        ("unlock", _cmd_unlock, "remove a verifiably-stale lock (refuses live locks)"),
        ("pause", _cmd_pause, "ask a running loop to stop after its current phase"),
        ("resume", _cmd_resume, "clear the pause flag and continue the loop"),
    ):
        p = sub.add_parser(name, help=help_text)
        add_config(p)
        if name == "smoke-browser":
            # Defaults to the browser REGARDLESS of `conversation.provider`.
            # Since Codex became the primary reviewer, reading the configured
            # provider here would silently stop exercising the transport this
            # command exists for — and the browser is the fallback, so it is
            # precisely the one that must be proven before it is needed.
            # Explicit rather than hard-coded, so any provider can be smoked.
            p.add_argument(
                "--provider",
                default="browser_chatgpt",
                help="conversation provider to smoke (default: browser_chatgpt)",
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
    except AutoloopError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
