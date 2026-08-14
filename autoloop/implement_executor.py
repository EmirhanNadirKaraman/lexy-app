"""The write-capable implementation executor — the `implement`/`revise`
counterpart to `audit/executor.py`'s AuditExecutor.

**The gap this closes.** `audit/executor.py`'s subagents are read-only by
construction (`--allowedTools Read Grep Glob`, `Edit`/`Write` explicitly
disallowed): they analyze and report, they never change anything. Before
this module existed, an `implement`/`revise` directive for a real repository
task had nowhere write-capable to go — the audit executor refuses it
(defense in depth; policy already blocks it upstream via
`policy.implement_enabled`), and `NullExecutor` only records that nothing
was done. `ImplementExecutor` is the thing that actually writes code: it
runs ONE write-capable `claude -p` subagent (via `implement_agent_runner`,
below — see `audit/agents.py` for why `ClaudeCliRunner`'s tool set is now a
constructor parameter rather than a fixed constant) against the task's own
isolated worker repo, then reports honestly on what changed.

Side effects per call: files inside the task's OWN worker repo (wherever the
agent's Edit/Write calls land) — nothing else. Unlike the audit executor,
this module never writes to `.autoloop/` and never writes a Markdown report;
there is no `run_dir_base`, no raw-output persistence, no `registry` (no
task-graph proposal — this executor does not invent new tasks). The ONE
thing it produces beyond the worker repo's own file changes is the
`ExecutionOutcome` it returns; the orchestrator's produce-then-review
machinery (`orchestrator.py:_dispatch_task_postcommit`) is what turns that
into a commit, structural verification, and a review packet — this module
has no opinion about any of that and never runs `git` itself except to READ
status.

**`changed_paths` is never the agent's word for what it did.** After the
agent returns, `_run_implementation` reads `git.dirty_paths_all()` — a real
`git status --porcelain -z -uall` round-trip against the worker repo — and
that is the ONLY source for `ExecutionOutcome.changed_paths`. `-uall`
(`--untracked-files=all`) specifically: the plain form collapses a new file
inside a brand-new directory to just the directory entry (`?? d/`), and that
collapsed form would go on to break the post-commit structural check, which
compares against LITERAL file paths (`services/new/` is not a match for
`services/new/foo.py`) — see `GitGateway.dirty_entries_all`'s docstring for
the reproduced failure mode this avoids.

**Model selection is automatic, deliberately.** `AgentSpec.model` is left at
its default (`""`), so `ClaudeCliRunner.build_argv` omits `--model` entirely
— no model table lives here or should be added; whatever the `claude` CLI
picks by default is what runs.

**The agent is bounded by SILENCE, not by elapsed time** (2026-08-14). The
write-capable runner this module builds carries a `stall.WorkerTreeProbe`, so
`ClaudeCliRunner` spawns and supervises rather than running under a wall-clock
timeout: while the worker repo keeps changing the agent runs, and it is killed
only after `stall_seconds` of no filesystem change at all (or at the absolute
ceiling, which should never fire). The retired `audit.agent_timeout_seconds`
killed six agents mid-write in two days and never once caught a hang — see
`stall.py`. A failed run of ANY kind now also reports what it left behind:
`_partial_work` reads the numbers from the worker repo's own git state, so a
reviewer can tell a wedge that produced 600 lines from one that produced
nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from .audit.agents import AgentRunner, AgentSpec, ClaudeCliRunner
from .contract import AUDIT_TASK_ID, TASK_DECISIONS, Decision, Directive
from .errors import GitError
from .executor import ExecutionOutcome
from .git_gateway import GitGateway
from .policy import PolicyEngine
from .stall import DEFAULT_CEILING_SECONDS, PartialWork, StallPolicy, WorkerTreeProbe
from .tasks import Task
from .validation import run_validation_commands
from .validation_env import ValidationEnv
from .worker_env import worker_env

#: Read/Grep/Glob for context, Edit/Write to make the change. `Bash` and
#: `Task`/`Agent` stay disallowed even though the agent can now write files:
#: the EXECUTOR (not the agent) runs validation and owns the commit, and a
#: subagent spawning nested agents is out of scope for this phase — see
#: `audit/agents.py`'s module docstring.
WRITE_ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob", "Edit", "Write")
IMPLEMENT_DISALLOWED_TOOLS: tuple[str, ...] = (
    "NotebookEdit",
    "Bash",
    "Task",
    "Agent",
    "WebFetch",
    "WebSearch",
)


def implement_agent_runner(
    root: Path,
    command: tuple[str, ...] = ("claude",),
    timeout_seconds: float = DEFAULT_CEILING_SECONDS,
    runner=None,
    policy: PolicyEngine | None = None,
    stall_policy: StallPolicy | None = None,
    spawn=None,
    clock=None,
    sleep=None,
) -> ClaudeCliRunner:
    """The ONE place a write-capable `ClaudeCliRunner` is constructed.

    `cli._build_executor` calls this for both `ImplementExecutor`'s
    standalone `agent_runner` and its `agent_runner_factory` (one call per
    task, rooted at that task's own worker repo — the exact pattern
    `AuditExecutor` already uses for its read-only subagents). Tests call it
    too, with a stubbed subprocess `runner`, so the argv asserted on in
    `tests/test_implement_executor.py` is the argv production actually
    sends — not a description of it.

    **`policy` is what turns the stall detector on.** Given one, this builds
    a `stall.WorkerTreeProbe` over a `GitGateway` rooted at `root` and running
    under the scrubbed `worker_env()` — the same construction
    `ImplementExecutor._bindings_for` makes for its own git access, so the
    probe observes the worker repository through the policy whitelist like
    everything else in the loop, and the write-capable agent is then bounded
    by SILENCE rather than by elapsed time (see `stall.py` for the six
    measured losses that bound cost). Without a `policy` there is no probe and
    the run falls back to the plain elapsed bound — which is what keeps every
    direct-`execute()` test and every stubbed-`runner` test working unchanged,
    and is why `timeout_seconds` now defaults to the absolute ceiling instead
    of the retired 900s: an unsupervised write-capable run should still not be
    cut off at a duration real tasks routinely exceed.
    """
    probe = None
    if policy is not None:
        probe = WorkerTreeProbe(GitGateway(root, policy, env=worker_env()), root)
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    if sleep is not None:
        kwargs["sleep"] = sleep
    return ClaudeCliRunner(
        repo_root=root,
        command=command,
        timeout_seconds=timeout_seconds,
        runner=runner,
        allowed_tools=WRITE_ALLOWED_TOOLS,
        disallowed_tools=IMPLEMENT_DISALLOWED_TOOLS,
        progress_probe=probe,
        stall_policy=stall_policy,
        spawn=spawn,
        **kwargs,
    )


def _agent_prompt(task: Task, feedback: str | None) -> str:
    parts = [
        "You are a write-capable coding subagent inside an automated "
        "repository task-implementation loop (a German language-learning "
        "app; see CLAUDE.md).",
        f"Task id: {task.id}",
        f"Title: {task.title}",
        task.description,
        "Ground rules: you may Read, Grep, Glob, Edit and Write. You may "
        "ONLY modify files inside your current working directory — this "
        "task's own isolated worker repository — and must never attempt to "
        "reach any path outside it. You have no Bash access and must not "
        "attempt to run `git` or any other command: committing is not your "
        "job, the orchestrator commits your changes after you finish and "
        "after validation passes. Do not delegate to another agent.",
    ]
    if feedback:
        parts.append(f"Revision feedback from the previous review round: {feedback}")
    return "\n\n".join(parts)


class ImplementExecutor:
    def __init__(
        self,
        git: GitGateway,
        agent_runner: AgentRunner,
        validation_commands: tuple[tuple[str, ...], ...] = (("ruff", "check", "."),),
        command_runner=None,
        worker_repo_root_for: Callable[[str], Path] | None = None,
        policy: PolicyEngine | None = None,
        agent_runner_factory: Callable[[Path], AgentRunner] | None = None,
        validation_env: ValidationEnv | None = None,
    ):
        """`git` / `agent_runner` are the STANDALONE bindings — used verbatim
        whenever `worker_repo_root_for` is not supplied (every direct
        `execute()` call in this module's own tests). `worker_repo_root_for`
        (a `path_for`-shaped callable, e.g. `WorkerRepoManager.path_for`) is
        how the orchestrator's produce-then-review wiring re-roots a call
        onto the task's OWN isolated worker repo: when set, `execute()`
        builds a fresh `GitGateway` rooted at `worker_repo_root_for(task.id)`
        for that one call, running under the scrubbed `worker_env()` mapping
        — `policy` (required together with `worker_repo_root_for`) is what
        that fresh `GitGateway` is constructed with. `agent_runner_factory`,
        if given, likewise builds a fresh write-capable `AgentRunner` rooted
        at the worker repo (e.g. `implement_agent_runner`, so the subagent's
        `cwd` is the worker repo, never the main checkout); when omitted,
        the construction-time `agent_runner` is reused as-is. This mirrors
        `AuditExecutor._bindings_for` exactly — see that class's docstring.
        """
        if (worker_repo_root_for is None) != (policy is None):
            raise ValueError(
                "ImplementExecutor requires 'worker_repo_root_for' and 'policy' "
                "together, or neither — passing only one would fail later as an "
                "opaque AttributeError deep inside the first git call instead of "
                "failing here, at construction time"
            )
        self._git = git
        self._agent_runner = agent_runner
        self._validation_commands = validation_commands
        self._command_runner = command_runner or subprocess.run
        self._worker_repo_root_for = worker_repo_root_for
        self._policy = policy
        self._agent_runner_factory = agent_runner_factory
        # The dedicated TEST database credentials the validation subprocess
        # runs under, or None for "validation gets no credentials". Held here
        # and passed ONLY to `run_validation_commands` below — never to the
        # agent runner, which runs under `strip_validation_vars()` (see
        # `audit/agents.py`) precisely so the writer cannot read them.
        self._validation_env = validation_env

    # ---- TaskExecutor -------------------------------------------------------

    def execute(self, directive: Directive, task: Task | None) -> ExecutionOutcome:
        if (
            task is None
            or directive.task_id == AUDIT_TASK_ID
            or directive.decision not in TASK_DECISIONS
        ):
            # Defense in depth: policy (`policy.implement_enabled` +
            # `_check_task_reference`) and the orchestrator's own dispatch
            # routing already keep the audit and non-task decisions away from
            # this executor. This refusal is never expected to fire in
            # production wiring; it exists so a direct `execute()` call (a
            # test, a future dispatch bug) fails honestly instead of
            # dereferencing a `None` task.
            return ExecutionOutcome(
                status="error",
                summary=(
                    "the implement executor supports only 'implement'/'revise' of "
                    "a real repository task — got "
                    f"'{directive.decision.value}'"
                    + (f" for task '{directive.task_id}'" if directive.task_id else "")
                ),
                validation="not run",
            )
        git, agent_runner = self._bindings_for(task)
        return self._run_implementation(directive, task, git, agent_runner)

    def _bindings_for(self, task: Task) -> tuple[GitGateway, AgentRunner]:
        if self._worker_repo_root_for is None:
            return self._git, self._agent_runner
        root = self._worker_repo_root_for(task.id)
        git = GitGateway(root, self._policy, env=worker_env())
        agent_runner = (
            self._agent_runner_factory(root)
            if self._agent_runner_factory is not None
            else self._agent_runner
        )
        return git, agent_runner

    # ---- implementation pipeline --------------------------------------------

    @staticmethod
    def _partial_work(git: GitGateway) -> tuple[tuple[str, ...], PartialWork]:
        """What survives in the worker repo after a failed agent run.

        Never raises: this runs on the failure path, and a report that can
        itself fail is not a report. A worker repo that cannot be read yields
        `PartialWork(measured=False)` and an empty path tuple — which is
        distinct from, and must not be confused with, a measured zero.
        """
        try:
            changed = tuple(sorted(git.dirty_paths_all()))
        except Exception:
            changed = ()
        try:
            return changed, WorkerTreeProbe(git).partial_work()
        except Exception as exc:
            return changed, PartialWork(
                measured=False, note=f"{type(exc).__name__} reading the worker repository"
            )

    def _run_implementation(
        self,
        directive: Directive,
        task: Task,
        git: GitGateway,
        agent_runner: AgentRunner,
    ) -> ExecutionOutcome:
        feedback = directive.feedback if directive.decision is Decision.REVISE else None
        spec = AgentSpec(domain=task.id, title=task.title, prompt=_agent_prompt(task, feedback))
        result = agent_runner.run(spec)
        if not result.ok:
            # A failed agent still leaves whatever it had already written in
            # the worker repo, and a reviewer cannot act on "the agent failed"
            # alone: a failure that produced 600 lines and one that produced
            # nothing call for opposite responses. So the numbers are read
            # here, from the worker repo's own git state (never from anything
            # the agent said), and reported alongside the cause.
            #
            # `changed_paths` on an error outcome is safe and deliberate:
            # `orchestrator._dispatch_task_postcommit` returns as soon as
            # `outcome.status != "ok"`, well before it ever reaches the commit
            # path, so nothing here can cause partial work to be committed.
            changed, partial = self._partial_work(git)
            summary = (
                f"task '{task.id}': implementation agent failed — "
                f"{result.error or f'rc={result.returncode}'}"
            )
            if result.stall is None:
                # A stall report already states the partial work it measured
                # at kill time; repeating it here would print two numbers for
                # one fact. Every OTHER failure has said nothing about it.
                summary += (
                    f" Partial work left in the worker repository: "
                    f"{partial.describe()}. Validation did not run."
                )
            return ExecutionOutcome(
                status="error",
                summary=summary,
                details=result.raw_text,
                validation="not run",
                changed_paths=changed,
            )

        try:
            changed = sorted(git.dirty_paths_all())
        except GitError as exc:
            # A whitelisted git read that ran but exited non-zero, or a
            # policy denial — either way this is an ordinary, reportable
            # failure of THIS task, not something to raise past the
            # orchestrator (nothing wraps `self._executor.execute(...)` in a
            # try/except at the call site).
            return ExecutionOutcome(
                status="error",
                summary=(
                    f"task '{task.id}': could not read the worker repo's status "
                    f"after the agent ran — {exc}"
                ),
                details=result.raw_text,
                validation="not run",
            )
        if not changed:
            return ExecutionOutcome(
                status="error",
                summary=(
                    f"task '{task.id}': the implementation agent ran but changed "
                    "no files in its worker repo — nothing to review"
                ),
                details=result.raw_text,
                validation="not run",
            )

        # A task may declare its own validation. Without this the configured
        # default (ruff + the autoloop and root-pipeline suites) runs for every
        # task regardless of what it touched — so a change under
        # `lexy-app/backend` would pass validation with nothing exercising it,
        # including the test the agent just wrote. An empty `task.validation`
        # keeps the configured default, which is right for tasks the default
        # does cover.
        commands = tuple(task.validation) or self._validation_commands
        validation_cwd = git.repo_root
        if task.validation_cwd:
            validation_cwd = git.repo_root / task.validation_cwd
            if not validation_cwd.is_dir():
                return ExecutionOutcome(
                    status="error",
                    summary=(
                        f"task '{task.id}': declared validation_cwd "
                        f"{task.validation_cwd!r} does not exist in the worker repo"
                    ),
                    details=result.raw_text,
                    validation="not run",
                    changed_paths=tuple(sorted(changed)),
                )
        passed, validation_summary = run_validation_commands(
            commands,
            validation_cwd,
            command_runner=self._command_runner,
            validation_env=self._validation_env,
        )
        if not passed:
            return ExecutionOutcome(
                status="error",
                summary=(
                    f"task '{task.id}': validation failed after implementation — "
                    f"{validation_summary}"
                ),
                details=result.raw_text,
                validation=validation_summary,
                changed_paths=tuple(changed),
            )

        return ExecutionOutcome(
            status="ok",
            summary=(
                f"task '{task.id}' implemented: {len(changed)} file(s) changed; "
                "validation passed."
            ),
            details=result.raw_text,
            validation=validation_summary,
            changed_paths=tuple(changed),
        )
