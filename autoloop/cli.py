"""Command-line interface for autoloop.

    python -m autoloop run [--config PATH] [--kickoff FILE | --kickoff-audit |
                            --answer TEXT | --retry] [--null-executor]
                            [--continuous] [--max-steps N]
    python -m autoloop status | tasks | doctor | next-task | blockers [--all]
                                                               (read-only, no lock)
    python -m autoloop answer <blocker-id> "<text>"
    python -m autoloop smoke-browser [--config PATH]
    python -m autoloop pause | resume | unlock | reset --yes
    python -m autoloop reprovision-publisher --confirm

Locking: run / resume / reset / smoke-browser / answer take the single-
instance lock on the state directory (fail closed against a live process;
`unlock` is the only stale-lock recovery, and it refuses live locks). status /
tasks / doctor / next-task / blockers / pause stay available while locked.

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
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .audit.agents import ClaudeCliRunner
from .audit.executor import AuditExecutor
from .audit.markdown import MarkdownPolicy
from .blockers import NO_TASK, Blocker, BlockerStore
from .config import AutoloopConfig, load_config
from .contract import AUDIT_TASK_ID, Decision, Directive
from .conversation import create_conversation
from .doctor import DoctorProbes, exit_code, run_doctor
from .errors import (
    AutoloopError,
    ConfigError,
    ExecutorError,
    StateCorruptError,
    StateError,
    TaskGraphError,
)
from .executor import ExecutionOutcome, NullExecutor, TaskExecutor
from .git_gateway import GitGateway
from .implement_executor import ImplementExecutor, implement_agent_runner
from .lock import LoopLock
from .manifest import ManifestStore, snapshot as manifest_snapshot
from .orchestrator import Orchestrator
from .policy import PolicyConfig, PolicyEngine
from .prompts import TEMPLATES, kickoff_payload, user_answer_payload
from .publisher import (
    Publisher,
    provision_publisher_repo,
    read_publisher_url_snapshot,
    redact_url,
    reprovision_publisher as _reprovision_publisher_snapshot,
)
from .state import TERMINAL_PHASES, LoopState, Phase, StateStore
from .tasks import Task, TaskRegistry, TaskStore
from .transcript import TranscriptLogger
from .worker_env import WorkerRepoManager, validate_workers_root, verify_worker_isolation
from .worktask import IntentStore, TaskExecutionStore

DEFAULT_CONFIG = Path(".autoloop/config.toml")


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
    task_store = TaskStore(config.tasks_file)
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


def _build_executor(
    config: AutoloopConfig,
    args,
    git: GitGateway,
    registry: TaskRegistry,
    worker_repos: WorkerRepoManager,
    policy: PolicyEngine,
) -> TaskExecutor:
    if getattr(args, "null_executor", False) or config.executor.kind == "null":
        return NullExecutor()
    audit_runner = ClaudeCliRunner(
        repo_root=git.repo_root,
        command=config.audit.agent_command,
        timeout_seconds=config.audit.agent_timeout_seconds,
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
            timeout_seconds=config.audit.agent_timeout_seconds,
        ),
    )
    implement_executor = ImplementExecutor(
        git=git,
        agent_runner=implement_agent_runner(
            git.repo_root,
            command=config.audit.agent_command,
            timeout_seconds=config.audit.agent_timeout_seconds,
        ),
        # Same validation commands and agent CLI settings as the audit —
        # there is no separate `[implement]` config section (kept minimal;
        # add one if the two ever need to diverge).
        validation_commands=config.audit.validation_commands,
        worker_repo_root_for=worker_repos.path_for,
        policy=policy,
        agent_runner_factory=lambda root: implement_agent_runner(
            root,
            command=config.audit.agent_command,
            timeout_seconds=config.audit.agent_timeout_seconds,
        ),
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
        executor=_build_executor(config, args, git, registry, worker_repos, policy),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: create_conversation(config.conversation.provider, config),
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worker_repos=worker_repos,
        execution_store=execution_store,
        intent_store=intent_store,
        blocker_store=blocker_store,
        publisher=publisher,
        publisher_url_snapshot=publisher_url_snapshot,
        # `--config` can put the file anywhere, so pass the path actually
        # loaded rather than re-deriving the conventional one. Callers that
        # build an args namespace without it (tests, embedders) fall back to
        # the default, which is where `DEFAULT_CONFIG` points.
        config_path=Path(getattr(args, "config", None) or DEFAULT_CONFIG),
    )


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with LoopLock(config.state_dir):
        if getattr(args, "continuous", False):
            _validate_continuous_args(args)
            return _run_continuous(args, config)
        return _run_locked(args, config)


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
                "--answer is only valid when the loop is parked on an ask_user "
                "question (phase=needs_user without a retryable phase)"
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
        store.save(state)
    elif Phase(state.phase) in (Phase.NEEDS_USER, Phase.FAILED, Phase.STOPPED):
        print(_summary(config, state, registry))
        print("\nLoop is parked. Use --answer / --retry, or `reset` to start over.")
        return 2

    orchestrator = _build_orchestrator(config, args, store, state, task_store, registry)
    outcome = orchestrator.run(max_steps=args.max_steps)
    print(_summary(config, orchestrator.state, registry))
    print(f"\nLoop ended: {outcome}")
    return 2 if outcome in (Phase.NEEDS_USER.value, Phase.FAILED.value) else 0


#: How long `run --continuous` sleeps, locally, between selection-policy
#: checks when there is genuinely nothing to do (no ready task, repository
#: fingerprint unchanged since the last audit). Not configurable in v1 —
#: keep the surface small.
CONTINUOUS_POLL_SECONDS = 30.0


def _run_continuous(args: argparse.Namespace, config: AutoloopConfig) -> int:
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
    while True:
        if config.pause_file.exists():
            print("paused")
            return 0
        store, state = _load_state(config)
        task_store, registry = _load_tasks(config)

        if state is not None and Phase(state.phase) not in TERMINAL_PHASES:
            orchestrator = _build_orchestrator(config, args, store, state, task_store, registry)
            outcome = orchestrator.run()
            if outcome == "paused":
                return 0
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
            continue  # STOPPED -> reassess at the top of the loop

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

        # Clean boundary: no session yet, or the last one ended STOPPED.
        if _select_and_kickoff(config, store, registry):
            continue
        open_blockers = blocker_store.open_blockers()
        if open_blockers:
            _print_blocker_summary(open_blockers)
            return 0
        time.sleep(CONTINUOUS_POLL_SECONDS)


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
        try:
            registry.block(state.park_task_id, reason)
        except TaskGraphError as exc:
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
        f"paused flag  {'yes' if config.pause_file.exists() else 'no'}",
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
#: (ask_user and every task_fatal code), which is the intended behaviour.
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
            client_factory=lambda: create_conversation(
                smoke_config.conversation.provider, smoke_config
            ),
            registry=TaskRegistry(),
            task_store=TaskStore(config.smoke_dir / "tasks.json"),
            manifest_store=ManifestStore(config.smoke_dir / "manifests"),
        )
        outcome = orchestrator.run()
    if outcome == Phase.STOPPED.value:
        print(
            "smoke-browser: PASS — submitted one request through the real "
            f"conversation and received a valid contract stop ({state.stop_reason!r})"
        )
        return 0
    print(
        f"smoke-browser: FAIL — loop ended in '{outcome}' "
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
    if config.pause_file.exists():
        config.pause_file.unlink()
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
    config = load_config(args.config)
    if not args.yes:
        print("reset archives the current session state; pass --yes to confirm")
        return 1
    with LoopLock(config.state_dir):
        for label, store in (
            ("state", StateStore(config.state_file)),
            ("tasks", TaskStore(config.tasks_file)),
        ):
            backup = store.archive()
            if backup is None:
                print(f"no {label} to reset")
            else:
                print(f"{label} archived to {backup}")
    print("transcript, manifests and audit runs kept as-is")
    return 0


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
    run.add_argument("--answer", help="answer to a pending ask_user question")
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
        p.set_defaults(func=func)

    blockers = sub.add_parser(
        "blockers", help="list open operator-facing blockers (read-only, no lock)"
    )
    add_config(blockers)
    blockers.add_argument(
        "--all", action="store_true", help="also show resolved blockers"
    )
    blockers.set_defaults(func=_cmd_blockers)

    answer = sub.add_parser(
        "answer",
        help="resolve a blocker and, if task_fatal, make its task READY again",
    )
    add_config(answer)
    answer.add_argument("blocker_id", help="the blocker id, e.g. blk-t1-001")
    answer.add_argument("text", help="the operator's answer/decision")
    answer.set_defaults(func=_cmd_answer)

    reset = sub.add_parser("reset", help="archive the session state")
    add_config(reset)
    reset.add_argument("--yes", action="store_true")
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
