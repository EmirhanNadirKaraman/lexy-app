"""Command-line interface for autoloop.

    python -m autoloop run [--config PATH] [--kickoff FILE | --kickoff-audit |
                            --answer TEXT | --retry] [--null-executor]
    python -m autoloop status | tasks | doctor      (read-only, no lock)
    python -m autoloop smoke-browser [--config PATH]
    python -m autoloop pause | resume | unlock | reset --yes

Locking: run / resume / reset / smoke-browser take the single-instance lock on
the state directory (fail closed against a live process; `unlock` is the only
stale-lock recovery, and it refuses live locks). status / tasks / doctor /
pause stay available while locked.

Exit codes: 0 = clean end (stopped / paused / step budget), 2 = the loop parked
itself (needs_user / failed) and wants operator attention, 1 = hard error.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

from .audit.agents import ClaudeCliRunner
from .audit.executor import AuditExecutor
from .audit.markdown import MarkdownPolicy
from .config import AutoloopConfig, load_config
from .conversation import create_conversation
from .doctor import DoctorProbes, exit_code, run_doctor
from .errors import AutoloopError, ConfigError, ExecutorError, StateError
from .executor import NullExecutor
from .git_gateway import GitGateway
from .lock import LoopLock
from .manifest import ManifestStore
from .orchestrator import Orchestrator
from .policy import PolicyEngine
from .prompts import TEMPLATES, kickoff_payload, user_answer_payload
from .state import LoopState, Phase, StateStore
from .tasks import TaskRegistry, TaskStore
from .transcript import TranscriptLogger

DEFAULT_CONFIG = Path(".autoloop/config.toml")


def _load_state(config: AutoloopConfig) -> tuple[StateStore, LoopState | None]:
    store = StateStore(config.state_file)
    state = store.load()
    if state is not None and state.conversation_url != config.browser.conversation_url:
        raise ConfigError(
            "browser.conversation_url in the config differs from the one this "
            "session started with. Restore the config value or `reset` the state "
            "to begin a new session."
        )
    return store, state


def _load_tasks(config: AutoloopConfig) -> tuple[TaskStore, TaskRegistry]:
    task_store = TaskStore(config.tasks_file)
    registry = task_store.load()
    if registry is None:
        registry = TaskRegistry()
    return task_store, registry


def _build_executor(config: AutoloopConfig, args, git: GitGateway, registry: TaskRegistry):
    if getattr(args, "null_executor", False) or config.executor.kind == "null":
        return NullExecutor()
    runner = ClaudeCliRunner(
        repo_root=git.repo_root,
        command=config.audit.agent_command,
        timeout_seconds=config.audit.agent_timeout_seconds,
    )
    return AuditExecutor(
        git=git,
        agent_runner=runner,
        markdown=MarkdownPolicy(git.repo_root),
        registry=registry,
        run_dir_base=config.audit_dir,
        validation_commands=config.audit.validation_commands,
        max_parallel_agents=config.audit.max_parallel_agents,
    )


def _build_orchestrator(config, args, store, state, task_store, registry) -> Orchestrator:
    policy = PolicyEngine(config.policy)
    git = GitGateway(Path.cwd(), policy)
    return Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=policy,
        git=git,
        executor=_build_executor(config, args, git, registry),
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=lambda: create_conversation(config.conversation.provider, config),
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
    )


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    with LoopLock(config.state_dir):
        return _run_locked(args, config)


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
    ]
    if state.last_decision:
        lines.append(f"last decision {state.last_decision}")
    if state.current_task:
        lines.append(
            f"current task {state.current_task.get('task_id') or '(audit)'}: "
            f"{(state.current_task.get('title') or '')[:120]}"
        )
    if state.last_manifest_id:
        lines.append(f"manifest     {state.last_manifest_id}")
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
    run.set_defaults(func=_cmd_run)

    for name, func, help_text in (
        ("status", _cmd_status, "show session, lock and roadmap state (read-only)"),
        ("tasks", _cmd_tasks, "list the task graph with derived states (read-only)"),
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

    reset = sub.add_parser("reset", help="archive the session state")
    add_config(reset)
    reset.add_argument("--yes", action="store_true")
    reset.set_defaults(func=_cmd_reset)
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
