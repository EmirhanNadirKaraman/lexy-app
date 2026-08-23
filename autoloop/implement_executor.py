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

**An ambiguity is resolved, not escalated** (see `_SMALLEST_REVERSIBLE_READING`).
`ask_user` is retired, so there is no way for this executor to stop mid-run and
ask about a task that does not say what it wants. The agent is instructed to
take the smallest reversible reading and to write an `ASSUMPTION:` line for each
choice; `_extract_assumptions` collects those lines VERBATIM and they ride the
outcome to `TaskExecution.assumptions`, which is what puts them in front of the
reviewer who authorizes the result. Unlike `changed_paths`, this IS the agent's
own word — safely so, because nothing computes with it (see
`packet._format_executor_report` for the same argument about the report text).

Nothing is dropped or shortened on the way to the record: `report_details` — the
other place these lines appear — is REPLACED every round, so an entry this
executor withheld would be gone for good the moment the next round ran. The
size bounds live at render time, where the constraint actually is
(`packet.ASSUMPTIONS_MAX_CHARS`, `packet.ASSUMPTION_MAX_CHARS_EACH`).

**A round can delete what an earlier round of the same task created out of
scope** (scope-04, 2026-08-19), and that is the ONLY deletion this executor
performs. The agent cannot delete anything itself — `WRITE_ALLOWED_TOOLS` is
Read/Grep/Glob/Edit/Write, `Bash` is disallowed — so "remove the residue you
added", a correct review, used to be literally unperformable and produced a
committed zero-byte file instead of an absent one (roadmap-01, 2026-08-18). The
agent now writes a `REMOVE-OUT-OF-SCOPE: <path>` line and this executor unlinks
the file, but ONLY when that exact path is already in the loop's own
`TaskExecution.out_of_scope_paths` record (`_cleanup_instruction`,
`_apply_recorded_cleanup`, `tasks.authorized_cleanup_paths`). The agent's line
selects from that record; it can never add to it. Nothing about scope changes:
an EDIT to the same path is as unauthorized as it ever was.

**A round can also PUT BACK what an earlier round of the same task edited out
of scope** (scope-05, 2026-08-24), which is the shape the sentence above could
not reach. scope-04 covers a CREATED file; port-01's contamination on
2026-08-20 was ten EDITED files and zero creations, so "strip the residue" was
once again unperformable, and because a revise builds on the same branch the
same edits were handed to every following round — 8 commits over 11 attempts,
a contaminated set that could not shrink, and a branch an operator eventually
discarded by hand. `REVERT-OUT-OF-SCOPE: <path>` is the second request form
(`_REVERT_RE`, `_apply_recorded_reverts`, `_revert_recorded_file`) and it runs
under EXACTLY the authority above: the same record, the same exact-match gate,
the same "the agent selects, it never adds". The content is read from git at
`TaskExecution.task_base_sha` — a LOOP-written field no agent can influence, and
the commit `commit_range_paths` already measures the candidate against — so the
result is checkable rather than being a second edit the agent authored from
memory. No tool is added (`WRITE_ALLOWED_TOOLS` is unchanged and `Bash` is
still disallowed), `approved_paths`/`allowed_paths` are neither read nor
written, and a path with no content at the base is restored to ABSENCE, which
is where the two instructions deliberately converge. Fail-closed everywhere the
input is missing: no injected `revert_authority`, no base sha, or a base tree
git will not read means nothing is reverted and every request says so.
`worktask.RecordedRevertAuthority` supplies the base sha and records the result
on `TaskExecution.reverted_out_of_scope_paths`.

**The prompt asks the agent to attack its own claim** (impl-01, 2026-08-22).
`_ADVERSARIAL_SELF_TEST` is given on every implement AND revise round: hunt the
cases in which THIS task's own stated claim would still fail, fix only what
falls inside the scope the task was already authorized against, report the rest,
and end the report with an `ADVERSARIAL CASES:` enumeration saying where each
case is handled. It adds an instruction and grants nothing — the tool set, the
ground rules and worker-repo confinement are untouched, and the enumeration is
prose for the reviewer that nothing in the loop parses.

**The prompt carries the repository's mechanical authoring rules** (brief-01,
2026-08-22). `_authoring_rules` states the change-note shape and the ENFORCED
line-length limit, read from `note_merge.MAX_NOTE_LINE_CHARS` at render time —
the same constant `test_docs_merge.py` checks the shipped trackers against, so
the brief cannot drift out of step with the test. Measured cost of not doing
this: two full rounds on 2026-08-21 (merge-04, blk-02), each ~20 minutes, each
implementing its task correctly and discarded over a documentation line length
the agent had no way to learn except by failing. It is deliberately the small
set of mechanical formatting rules that reject correct work, not a summary of
the test suite; it grants nothing and changes no check.

That bound is only actionable if the agent can SEE where the line is, so
`_scope_instruction` renders it: the effective approved paths, from
`tasks.effective_approved_paths` — the same function the pre-commit scope gate
calls (`orchestrator.py:5337`) and the same one `TaskExecution.allowed_paths` is
re-synced from on every dispatch (`orchestrator.py:4874`, `:4898`). So the list
the agent reads and the list the loop grades against are one computation, not
two descriptions of one. Rendering it GRANTS nothing: authorization still comes
from the Task, this module still neither reads nor writes `allowed_paths`, and
`tasks.unauthorized_paths` remains the only thing that decides what a round was
allowed to change. (This module's one contact with a scope-adjacent record is
the cleanup path above, which is unchanged and scoped by
`_apply_recorded_cleanup`'s own docstring.)

**Model selection is automatic, deliberately.** `AgentSpec.model` is left at
its default (`""`), so `ClaudeCliRunner.build_argv` omits `--model` entirely
— no model table lives here or should be added; whatever the `claude` CLI
picks by default is what runs.

**The round's own validation is a bound, zero-argument CALL** (impl-02,
2026-08-23). `AdvisoryValidation` closes over the four things
`_run_implementation` already computes for its authoritative run — the command
list, the working directory, the command runner and the validation environment
— and exposes ONE method that takes no arguments at all. The agent cannot name
a command, a path, a flag or an environment value, because there is no
parameter to name one in: `serve_advisory_tool_call` accepts whatever payload
a transport hands it and discards it unread. The run is ADVISORY — the
executor still runs validation itself afterwards, and that run alone decides
`ExecutionOutcome.validation` and the round's status.

Cost is bounded twice: `ADVISORY_VALIDATION_MAX_CALLS` requests per round, and
`ADVISORY_VALIDATION_TIMEOUT_SECONDS` per run. A request past the cap executes
nothing and says `validation.NOT_RUN`, which is deliberately different
information from `PASS` — the same rule val-03 established for the executor's
own run, and the one thing this must not weaken. So is a round with no
commands configured and one whose declared `validation_cwd` is missing: an
advisory channel that answered "green" because it had nothing to run would be
exactly the fail-open it exists to prevent.

`note()` is what the round reports, and it is computed from THIS module's own
counters — never from `result.raw_text`. An agent that writes "I ran the suite
five times and it was green" moves no number here. It also distinguishes
"offered and unused" from NOT OFFERED, because a guard nobody could reach must
not read as one nobody wanted.

**The TRANSPORT is a filesystem rendezvous, because that is the one the agent
demonstrably already has.** `AdvisoryRendezvous` watches two fixed paths in the
worker repo: the agent Writes anything to `.autoloop-validation-request` to ask,
and Reads `.autoloop-validation-result.txt` to get the answer. Nothing else is
granted — Read and Write are already in `WRITE_ALLOWED_TOOLS`, so this adds no
tool, no flag, no shell and no new process, and `IMPLEMENT_DISALLOWED_TOOLS` is
untouched.

An MCP tool would have been the obvious shape and is NOT what shipped, for two
reasons that are about verifiability rather than taste. `cli._build_executor`'s
`agent_runner_factory` lambda is the only place holding both the configured
validation commands and the worker root, so a per-round spec cannot reach
`ClaudeCliRunner.build_argv` without editing `cli.py`; and the `--mcp-config`
family's exact spelling and headless approval behaviour cannot be checked from
inside this loop (the implementing agent has no shell). A wrong flag fails every
implement round at spawn, including the round that would fix it. A rendezvous
over two file paths fails, when it fails, by the agent simply not being answered
— and `note()` reports that as a measured zero. `advisory_tool_descriptor` is
kept and is the source of the brief's own wording, so the day a tool transport
IS wired, the description it publishes is the one the agent has been reading.

The residue trap is handled here rather than left for a later round. An advisory
run happens INSIDE the agent's window, i.e. before `git.dirty_paths_all()` reads
the tree, so anything left behind is picked up as changed work — whereas the
executor's own run happens after that read. Both rendezvous paths are swept in a
`finally` around the agent call itself, so every reader downstream (the
failure-path `_partial_work`, the cleanup pass, the status read) sees a tree with
no trace of the channel. VERIFIED: `.gitignore` lists neither `.ruff_cache` nor
`.pytest_cache` (2026-08-23), and pytest's is already suppressed for every pytest
command by `validation.NO_CACHE_ARGS`. NOT verified without a shell: whether
`ruff check` really leaves a `.ruff_cache/` that `git status -uall` reports here
— that one is a property of the commands, not of this channel, and it would show
up as an unexpected `changed_paths` entry rather than as a wrong verdict.

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

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, NamedTuple, Sequence

from . import note_merge
from .audit.agents import AgentRunner, AgentSpec, ClaudeCliRunner, classify_agent_fault
from .contract import AUDIT_TASK_ID, TASK_DECISIONS, Decision, Directive
from .errors import GitError
from .executor import ExecutionOutcome
from .git_gateway import GitGateway
from .policy import PolicyEngine
from .stall import DEFAULT_CEILING_SECONDS, PartialWork, StallPolicy, WorkerTreeProbe
from .tasks import Task, authorized_cleanup_paths, effective_approved_paths
from .validation import NOT_RUN, run_validation_commands
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


# ---- the round's own validation, callable by the agent ----------------------
#
# WHY THIS IS A BOUND CALL AND NOT A SHELL. The write-capable agent can write a
# test and cannot run one: `WRITE_ALLOWED_TOOLS` is Read/Grep/Glob/Edit/Write
# and `Bash` sits in `IMPLEMENT_DISALLOWED_TOOLS`, so validation is the agent's
# FIRST feedback and it arrives after the whole round is paid for. Measured over
# every round since val-03 went live (2026-08-23): five rounds failed AFTER
# implementing, 3.29h between them — a quarter of all executor time in the
# window — and FOUR of the five failed a test the round itself had just written.
# stop-01's was "test script exhausted: no response left", a scripted fake given
# fewer replies than the code asks for: one execution catches it in under a
# second, and it cost 51.6 minutes.
#
# The restriction stays. What is added is ONE fixed call, not a shell: every
# input is bound here, from values the executor already computed, and the
# agent-facing surface has no parameter at all. That is the whole security
# argument — there is no argument to sanitize, because there is no argument.


#: How many advisory runs ONE round may pay for.
#:
#: A bound on COST, and the reason the cap is small: the executor's own run is
#: still owed on top of these, and a round that could re-run the suite freely
#: would spend its whole `agent_ceiling_seconds` proving the same thing. Three
#: is enough for the loop this exists to close (run, see the failure, fix it,
#: confirm) and stops well short of an unbounded retry loop.
ADVISORY_VALIDATION_MAX_CALLS = 3

#: The wall-clock bound on ONE advisory run — deliberately far below
#: `run_validation_commands`' own 1800s default, which is also the default
#: `audit.agent_stall_seconds`. An advisory run writes no files, so to the
#: `stall.WorkerTreeProbe` watching the worker repo it looks exactly like
#: SILENCE; a run allowed to last as long as the stall bound could get the agent
#: killed mid-call by the detector that is supposed to catch a wedge.
ADVISORY_VALIDATION_TIMEOUT_SECONDS = 600.0

#: The name a transport publishes the zero-argument call under.
ADVISORY_TOOL_NAME = "run_validation"


def advisory_tool_descriptor(max_calls: int = ADVISORY_VALIDATION_MAX_CALLS) -> dict:
    """What the zero-argument call IS, in the form a tool transport publishes.

    Its production caller today is `_advisory_instruction`, which renders
    `description` into the agent's brief verbatim — so the sentences the agent
    reads and the sentences an MCP/`--mcp-config` transport would advertise are
    one string, not two that drift. That is the whole reason this survives the
    filesystem rendezvous shipping first: the description is transport-neutral
    on purpose (it says what happens and what is fixed, never how to call it),
    and the `how` lives in `_advisory_instruction` beside the paths it names.

    The `inputSchema` is the MECHANICAL form of this task's first constraint:
    no properties, nothing required, `additionalProperties` false. A caller that
    somehow sends a payload anyway is handled by `serve_advisory_tool_call`,
    which discards it unread — the schema states the rule, the handler does not
    depend on the rule being honoured.
    """
    return {
        "name": ADVISORY_TOOL_NAME,
        "description": (
            "Run this repository's configured validation (lint/tests) against "
            "your own worker repo, exactly as the executor will run it after "
            "you return. It takes NO arguments: the commands, the working "
            "directory and the environment are fixed by the executor, and "
            "nothing you supply can change any of them. The result comes back "
            "to you as text. This run is ADVISORY — the executor runs "
            "validation itself afterwards and that run is the verdict. At most "
            f"{max_calls} run(s) per round; past that the request executes "
            f"nothing and says {NOT_RUN}, which is not a pass."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    }


class AdvisoryValidation:
    """The round's configured validation, bound to executor-owned inputs and
    callable with nothing.

    Constructed by `ImplementExecutor._advisory_for` from the same four values
    `_run_implementation` uses for its authoritative run — so the agent's run
    and the executor's run are the same commands in the same directory under
    the same environment, and a green advisory run means what the agent will
    naturally read it to mean. They are two SEPARATE runs: nothing here is
    consulted by, shortens, or stands in for the executor's own call, which
    happens unconditionally after the agent returns.

    **The agent supplies nothing.** `run()` takes no parameter, so there is no
    channel through which a command, a path, a flag or an environment value
    could arrive from the agent. `serve_advisory_tool_call` is the transport
    boundary and drops whatever payload it is handed.

    **Every answer that is not a real result says so.** The cap, an empty
    command list, a missing working directory and an exception inside the run
    all return `NOT_RUN`/failure text rather than silence or a pass. An
    advisory channel that answered "green" when it had nothing to run would be
    the exact fail-open this exists to prevent, and it would be believed —
    that is the whole point of giving the agent an answer at all.

    **The counters are the loop's own record.** `note()` reads them and nothing
    else; the agent's report text is never consulted. A round that never
    offered the call says NOT OFFERED rather than "0 runs", because "could not"
    and "chose not to" are different facts and only one of them is a finding
    about the agent.
    """

    def __init__(
        self,
        commands: Sequence[Sequence[str]],
        cwd: Path,
        command_runner=None,
        validation_env: ValidationEnv | None = None,
        max_calls: int = ADVISORY_VALIDATION_MAX_CALLS,
        timeout: float = ADVISORY_VALIDATION_TIMEOUT_SECONDS,
    ):
        # Normalised defensively: an unusable list becomes the EMPTY list,
        # which `run()` answers as NOT_RUN and never as a pass. The round-level
        # version of this guard is in `ImplementExecutor._advisory_for`, which
        # is where a malformed `Task.validation` would actually be met.
        try:
            self._commands = tuple(tuple(argv) for argv in commands)
        except TypeError:
            self._commands = ()
        self._cwd = Path(cwd)
        self._command_runner = command_runner
        # The SAME credentials the executor's own run gets, and for one reason:
        # this object runs inside the loop's own process, so the values never
        # approach the agent's process, and `run_validation_commands` redacts
        # them out of the text before it is returned. A CROSS-PROCESS transport
        # (an MCP server spawned by the CLI) must NOT carry this or the file it
        # came from — that would put a credential, or the path to one, on the
        # agent's own command line and undo `validation_env.py`'s promise that
        # "the writer never learns the file's path either".
        self._validation_env = validation_env
        self._max_calls = max(0, int(max_calls))
        self._timeout = timeout
        #: One entry per run that actually EXECUTED commands: True if every
        #: command that ran passed. Requests refused at the cap and requests
        #: that could not run are counted separately and never land here — a
        #: list that mixed them could not answer "was the last RUN green".
        self._results: list[bool] = []
        self._requests = 0
        self._refused = 0
        self._blocked = 0
        self._exposed = False
        # Guards the counter that DECIDES the cap, and nothing else. `+=` is
        # not atomic, so two tool calls served concurrently could each read the
        # same count and both be admitted — a cap that holds only if the
        # transport happens to be single-threaded is not a cap. The lock is
        # released before the run itself, so it bounds the NUMBER of runs
        # without serialising them into one 600-second queue.
        self._lock = threading.Lock()

    # ---- what a transport binds --------------------------------------------

    def expose(self) -> None:
        """Record that this round actually OFFERED the call to the agent.

        Called by `AdvisoryRendezvous.start()` — the one thing that actually
        puts the channel in front of an agent — and by any future transport
        that does the same. It changes no behaviour; it changes what a zero
        MEANS in `note()`, which is the difference between "the agent did not
        check its work" and "the agent had no way to".
        """
        self._exposed = True

    @property
    def exposed(self) -> bool:
        return self._exposed

    @property
    def runs(self) -> int:
        """Runs that really executed commands."""
        return len(self._results)

    @property
    def requests(self) -> int:
        return self._requests

    @property
    def refused(self) -> int:
        return self._refused

    @property
    def blocked(self) -> int:
        return self._blocked

    @property
    def max_calls(self) -> int:
        return self._max_calls

    @property
    def offerable(self) -> bool:
        """Is there anything here worth telling the agent about?

        False when nothing could ever execute — no commands configured, or a
        cap of zero. Offering the channel then would spend the agent's turns
        collecting `NOT_RUN` answers it can do nothing with, and `note()` would
        report OFFERED for a round in which the agent could not possibly have
        checked anything.

        Deliberately does NOT test the working directory. A missing
        `validation_cwd` is a round-fatal error the executor reports on its own
        run, and the honest thing for the agent to hear meanwhile is the
        `NOT_RUN` text naming that directory — not silence.
        """
        return bool(self._commands) and self._max_calls > 0

    @property
    def last_run_ok(self) -> bool | None:
        """Was the last executed run green? None when nothing ever ran.

        None is a third state on purpose: `False` would report a red run that
        never happened, and `True` would be the fail-open.
        """
        return self._results[-1] if self._results else None

    # ---- the agent-facing call ---------------------------------------------

    def run(self) -> str:
        """Run the bound validation once and return the result AS TEXT.

        Takes no arguments — see the class docstring. Never raises: this is
        called from inside a transport serving an agent mid-turn, and an
        exception there would break the turn rather than report a failure. A
        run that could not complete is reported as a failure, never as a pass.
        """
        with self._lock:
            self._requests += 1
            over_budget = self._requests > self._max_calls
            if over_budget:
                self._refused += 1
        if over_budget:
            return (
                f"{NOT_RUN}: this round's advisory validation budget of "
                f"{self._max_calls} run(s) is already spent, so nothing was "
                f"executed. {NOT_RUN} is not a pass — it is the absence of "
                "evidence either way. The executor runs validation itself after "
                "you return; fix what your last run reported rather than asking "
                "again."
            )
        if not self._commands:
            with self._lock:
                self._blocked += 1
            return (
                f"{NOT_RUN}: no validation commands are configured for this "
                f"round, so there was nothing to execute. {NOT_RUN} is not a "
                "pass."
            )
        if not self._cwd.is_dir():
            with self._lock:
                self._blocked += 1
            return (
                f"{NOT_RUN}: the working directory this round's validation runs "
                f"in ({self._cwd}) does not exist, so nothing was executed. "
                f"{NOT_RUN} is not a pass — the executor's own run will fail on "
                "the same directory."
            )
        try:
            ok, summary = run_validation_commands(
                self._commands,
                self._cwd,
                command_runner=self._command_runner,
                timeout=self._timeout,
                validation_env=self._validation_env,
            )
        except Exception as exc:  # noqa: BLE001 — see the docstring
            self._results.append(False)
            return (
                "ADVISORY validation could not complete: "
                f"{type(exc).__name__}: {str(exc).strip() or '(no detail)'}. "
                "Treat this as a FAILURE, not a pass — nothing was proved."
            )
        self._results.append(bool(ok))
        verdict = "PASSED" if ok else "FAILED"
        return (
            f"ADVISORY validation run {self.runs} of {self._max_calls} — "
            f"{verdict}.\n{summary}\n"
            "This run is advisory: the executor runs the same commands itself "
            "after you return, and that run is what decides the round."
        )

    # ---- what the round reports --------------------------------------------

    def note(self) -> str:
        """The sentence the round's summary carries about this channel.

        Leading space, so callers concatenate it onto an existing summary the
        way `_cleanup_note` already does. Built ONLY from the counters above —
        the agent's own account of what it ran is not an input here, and the
        test that pins that feeds a report claiming five green runs and expects
        a zero.
        """
        if not self._exposed:
            return (
                " Agent self-validation: NOT OFFERED — no advisory validation "
                "channel was wired to the agent this round, so it ran the suite "
                "0 time(s). Read that as 'could not', not 'chose not to'."
            )
        if not self._results and not self._requests:
            parts = [
                " Agent self-validation: OFFERED, and the agent ran the suite 0 "
                "time(s) — it had the call available and did not use it."
            ]
        elif not self._results:
            # It ASKED and got nothing back. Reporting that as "did not use it"
            # would blame the agent for a channel that refused it, which is the
            # same misreading `NOT OFFERED` exists to prevent one level up.
            parts = [
                " Agent self-validation: OFFERED; the agent asked "
                f"{self._requests} time(s) and the suite ran 0 time(s)."
            ]
        else:
            verdict = "PASSED" if self._results[-1] else "FAILED"
            parts = [
                f" Agent self-validation: the agent ran the suite {self.runs} "
                f"time(s); its last run {verdict}."
            ]
        if self._refused:
            parts.append(
                f" {self._refused} further request(s) were refused at the cap of "
                f"{self._max_calls} and executed nothing ({NOT_RUN}, not a pass)."
            )
        if self._blocked:
            parts.append(
                f" {self._blocked} request(s) could not run at all (no commands "
                f"configured, or a missing working directory) — {NOT_RUN}, not a "
                "pass."
            )
        parts.append(
            " Advisory only; the validation summary recorded for this round is "
            "the executor's own run."
        )
        return "".join(parts)


def serve_advisory_tool_call(service: AdvisoryValidation, arguments=None) -> str:
    """The transport boundary: one tool call in, the run's text out.

    `arguments` exists because transports hand a payload to every tool call,
    and it is accepted ONLY so that it can be discarded here, once, in a place a
    test can point at. It is never inspected, never merged into anything and
    never reaches `run()` — which has no parameter to receive it with. That is
    what makes "the agent supplies no command, no path, no flag and no
    environment value" a property of the code's SHAPE rather than of a
    validation routine that could be wrong about a payload.
    """
    del arguments
    return service.run()


# ---- the transport: two fixed paths in the worker repo ----------------------
#
# WHY A FILE RENDEZVOUS AND NOT AN MCP TOOL. See the module docstring: an MCP
# tool needs `cli.py` (the only place holding both the validation commands and
# the worker root) and needs `--mcp-config`'s exact spelling and headless
# approval behaviour verified against the installed CLI — which the agent
# implementing it cannot do, having no shell. Read and Write are already in
# `WRITE_ALLOWED_TOOLS` and are exercised by every round, so a rendezvous over
# two fixed paths is the transport whose availability is not a guess.
#
# Nothing about the security posture moves. No tool is added,
# `IMPLEMENT_DISALLOWED_TOOLS` is unchanged, no process is spawned for the agent
# to talk to, and the credentials in `ValidationEnv` never leave this process:
# the run happens HERE, on the loop's side, and only redacted text crosses back.

#: What the agent writes to ASK. Content irrelevant — existence is the request.
ADVISORY_REQUEST_FILE = ".autoloop-validation-request"

#: What the executor writes the ANSWER to, and the agent reads.
ADVISORY_RESULT_FILE = ".autoloop-validation-result.txt"

#: Where the answer is staged so a reader never sees half of one. Swept with
#: the other two — an interrupted write must not leave a third path behind.
ADVISORY_RESULT_TMP_FILE = ".autoloop-validation-result.tmp"

#: The result file's first word while a run is in flight, and when it is done.
#: The agent polls on this distinction, so the two must not be prefixes of one
#: another and neither may appear at the start of the other's body.
ADVISORY_PENDING_PREFIX = "PENDING"
ADVISORY_RESULT_PREFIX = "RESULT"

#: How often the watcher looks for a request. Small: this is the latency the
#: agent pays before it sees `PENDING`, and the cost is one `stat` per tick.
ADVISORY_POLL_SECONDS = 0.25

#: How long `stop()` waits for a run that is still in flight when the agent
#: returns. Generous on purpose — the alternative is the executor's own
#: authoritative run starting while an advisory run is still executing the same
#: suite in the same directory. Bounded so a wedged runner cannot hang a round.
#:
#: Not a guarantee that the wait covers every run: `run_validation_commands`
#: applies its timeout PER COMMAND, so a long list can outlast this. What is
#: guaranteed is the part that matters for correctness — an abandoned run writes
#: NOTHING, because `_publish` finds `_stopping` set (see `stop()`). The cost of
#: exceeding the bound is two suites running at once for a while, not residue
#: and not a wrong verdict.
ADVISORY_STOP_JOIN_SECONDS = ADVISORY_VALIDATION_TIMEOUT_SECONDS + 60.0


def _advisory_instruction(max_calls: int) -> str:
    """The brief's section on the channel: what it is, and how to use it.

    The WHAT is `advisory_tool_descriptor`'s description, verbatim, so a future
    tool transport advertises the same sentences (see that function). The HOW is
    here, beside the constants it names, because it is the only part that is
    specific to this transport.

    **Echo-safe by construction.** No line in this text begins with
    `ASSUMPTION:`, `REMOVE-OUT-OF-SCOPE:` or `REVERT-OUT-OF-SCOPE:`, so an agent
    that quotes the whole brief back forges neither a disclosure nor a request
    to delete or restore a file — the same property `_authoring_rules` and
    `_scope_instruction` are held to.

    One sentence is doing real work and is not decoration: the ground rules
    above say the agent has no shell and must not run commands, and this section
    would read as a contradiction without saying which it is. It is not a
    command the agent composes; it is a request for the executor's own run.
    """
    return (
        "RUN THE SUITE BEFORE YOU RETURN — you can, and finding your own "
        "mistake costs a minute instead of a whole round.\n"
        f"{advisory_tool_descriptor(max_calls)['description']}\n"
        "You still have no shell and still may not run `git` or any other "
        "command: this is not a command you compose, it is a request for the "
        "executor's own validation run, which it performs on your behalf.\n"
        "How to ask, with the Read and Write tools you already have:\n"
        f"  1. Write anything at all to `{ADVISORY_REQUEST_FILE}` in your "
        "working directory. The content is discarded unread — the file's "
        "EXISTENCE is the whole request, and there is no field in it through "
        "which a command, a path, a flag or an environment value could be "
        "passed.\n"
        f"  2. Read `{ADVISORY_RESULT_FILE}`. Every answer is STAMPED WITH YOUR "
        f"REQUEST NUMBER: your first request is answered by "
        f"`{ADVISORY_RESULT_PREFIX} #1`, your second by "
        f"`{ADVISORY_RESULT_PREFIX} #2`. While a run is in flight the file "
        f"reads `{ADVISORY_PENDING_PREFIX} #n` instead. Re-read it until you "
        f"see `{ADVISORY_RESULT_PREFIX} #n` for the request you just made — a "
        "LOWER number is the previous answer and yours has not been taken yet, "
        "and a full run takes minutes, so expect several reads.\n"
        "  3. Fix what it reports, then ask again to confirm if you have "
        "budget left.\n"
        "Both files are the executor's control channel, not part of your "
        "change: it deletes them before it reads what you changed, so nothing "
        "you write there is committed and nothing you leave there counts as "
        "work. The result is a snapshot of the tree AS IT STOOD WHEN YOU ASKED "
        "— edit something afterwards and the answer is about the older tree."
    )


class AdvisoryRendezvous:
    """The agent's end of `AdvisoryValidation`: two fixed paths in the repo the
    agent is already working in.

    `start()` sweeps stale files, marks the service EXPOSED and puts a watcher
    thread on the request path. `stop()` stops it and sweeps again — and it is
    safe on a rendezvous that was never started, which is what makes the
    `finally` in `_run_implementation` a total guarantee rather than a
    happy-path one.

    **The agent supplies nothing, structurally.** The two paths are constants
    joined onto the executor's own root; the request file's bytes are handed to
    `serve_advisory_tool_call`, whose only purpose is to discard them. There is
    no field, header or filename convention through which the agent could name
    a command, a directory, a flag or an environment value, because nothing that
    arrives from the agent is ever read.

    **Residue is the failure mode that would cost a round**, since neither path
    is inside any task's `approved_paths`: a file left behind is an out-of-scope
    write on the record and a parked candidate. So the sweep runs on stop
    unconditionally (not only when a request was served), tolerates a directory
    or a symlink sitting at either path, and never raises.
    """

    def __init__(
        self,
        service: AdvisoryValidation,
        root: Path,
        poll_seconds: float = ADVISORY_POLL_SECONDS,
        join_timeout: float = ADVISORY_STOP_JOIN_SECONDS,
    ):
        self._service = service
        self._root = Path(root)
        self._poll = poll_seconds
        self._join_timeout = join_timeout
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        # Serialises the two file-touching moments (taking a request + writing
        # PENDING, and publishing the RESULT) against `stop()`'s sweep. Without
        # it a run still in flight could write its answer AFTER the sweep and
        # leave exactly the residue the sweep exists to remove. The validation
        # run itself is OUTSIDE the lock, so `stop()` is never blocked behind a
        # ten-minute suite.
        self._lock = threading.Lock()
        self._broken = False
        #: The bytes the last request file held. Kept only to be handed to
        #: `serve_advisory_tool_call`, which discards them; it exists so that
        #: "the payload is discarded" is a step in the real code path rather
        #: than a property of a code path nothing takes.
        self._payload = b""
        #: How many requests have been TAKEN. Stamped into every `PENDING` and
        #: every answer, and the whole of the staleness defence: the agent
        #: cannot delete the result file (it has no delete tool), so between
        #: writing its second request and the watcher taking it, the file still
        #: holds the FIRST answer. An unstamped protocol would let a green
        #: answer to an older tree be read as an answer to the current one —
        #: a fail-open, and the expensive kind, since it would be believed.
        self._served = 0

    # ---- the paths ----------------------------------------------------------

    @property
    def request_path(self) -> Path:
        return self._root / ADVISORY_REQUEST_FILE

    @property
    def result_path(self) -> Path:
        return self._root / ADVISORY_RESULT_FILE

    @property
    def _tmp_path(self) -> Path:
        return self._root / ADVISORY_RESULT_TMP_FILE

    def brief(self) -> str:
        """The prompt section describing this channel."""
        return _advisory_instruction(self._service.max_calls)

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Sweep, expose, and begin watching. Idempotent enough to be safe: a
        second call replaces nothing, because `_run_implementation` calls it
        once."""
        self._sweep()
        self._service.expose()
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._watch, name="autoloop-advisory-validation", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop watching and leave no trace. Safe if `start()` never ran.

        Order matters, and the two orders a publish can take are both covered.
        `_stopping` is set FIRST (an `Event` needs no lock of its own), so a run
        still in flight finds it set when it reaches `_publish` and writes
        nothing at all. Then the thread is joined, bounded, so the ordinary case
        never races. Then the sweep runs UNDER THE LOCK, so a publish that had
        already passed its check either completed its write before the sweep
        (which then removes it) or waits behind the sweep and finds `_stopping`
        set. Either way nothing survives `stop()`.
        """
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self._join_timeout)
        with self._lock:
            self._sweep()

    def _watch(self) -> None:
        # Never raises: an exception here would kill the watcher silently and
        # the agent would poll a result file that never appears. `serve_once`
        # is already total; this is the second layer.
        while not self._stopping.is_set():
            try:
                self.serve_once()
            except Exception:  # noqa: BLE001 — see above
                pass
            # `wait` rather than `sleep`, so `stop()` is not held up by a tick.
            self._stopping.wait(self._poll)

    # ---- serving ------------------------------------------------------------

    def serve_once(self) -> bool:
        """Answer ONE pending request, if there is one. True when one was.

        Called on a timer by `_watch`, and directly by tests — the same entry
        point, so what the tests exercise is what production runs.
        """
        if not self._take_request():
            return False
        ordinal = self._served
        text = serve_advisory_tool_call(self._service, self._payload)
        self._publish(f"{ADVISORY_RESULT_PREFIX} #{ordinal} — {text}")
        return True

    def _take_request(self) -> bool:
        """Consume a request and announce that it is running. True if there was
        one.

        Everything that touches the filesystem here happens under the lock and
        behind the stopping check, so a request arriving as the round ends is
        either fully served or not started — never half-served with a file left
        behind.
        """
        with self._lock:
            if self._stopping.is_set() or self._broken:
                return False
            path = self.request_path
            try:
                present = path.is_symlink() or path.exists()
            except OSError:
                return False
            if not present:
                return False
            self._payload = _read_bytes_or_empty(path)
            self._served += 1
            if not _remove_entry(path):
                # The request cannot be consumed, so serving it would re-serve
                # it on every tick until the cap absorbed the loop. Say so once
                # and stop, rather than spinning or going quiet.
                self._broken = True
                self._write_result(
                    f"{ADVISORY_RESULT_PREFIX} #{self._served} — {NOT_RUN}: the "
                    f"executor could not consume `{ADVISORY_REQUEST_FILE}` "
                    "(something that is not a removable file is sitting at that "
                    "path), so nothing was executed and no further request can "
                    f"be made this round. {NOT_RUN} is not a pass."
                )
                return False
            self._write_result(
                f"{ADVISORY_PENDING_PREFIX} #{self._served} — your advisory "
                "validation run has started. Re-read this file until it reads "
                f"`{ADVISORY_RESULT_PREFIX} #{self._served}`; a full run takes "
                "minutes."
            )
            return True

    def _publish(self, text: str) -> None:
        with self._lock:
            if self._stopping.is_set():
                # The round is over. Writing now would put residue in the tree
                # AFTER the sweep, i.e. an out-of-scope path on the record.
                return
            self._write_result(text)

    def _write_result(self, text: str) -> None:
        """Stage, then rename. A reader must never see half an answer — the
        agent polls this file, and a torn read of a `RESULT` header with no body
        is indistinguishable from a run that reported nothing.

        Never raises: called under the lock from the watcher thread, where an
        exception would end the watcher and leave the agent polling forever.
        """
        tmp = self._tmp_path
        try:
            tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
            os.replace(tmp, self.result_path)
        except OSError:
            _remove_entry(tmp)

    def _sweep(self) -> None:
        """Remove every trace of the channel. Total, and never raises."""
        for path in (self.request_path, self.result_path, self._tmp_path):
            _remove_entry(path)


def _read_bytes_or_empty(path: Path) -> bytes:
    """The request file's content, or `b""` — which is the same thing here.

    Nothing reads what this returns: it goes straight to
    `serve_advisory_tool_call`, which deletes it. It is read at all so that the
    discard is a real step on the real path, and it is read DEFENSIVELY because
    the alternative (an exception) would abort a request the agent is waiting
    on. A symlink is not followed — a request file that points somewhere else
    yields nothing, rather than making the loop read a file of the agent's
    choosing for no reason.
    """
    try:
        if path.is_symlink():
            return b""
        return path.read_bytes()
    except OSError:
        return b""


def _remove_entry(path: Path) -> bool:
    """Make `path` not exist. True when it is gone afterwards (including when
    it never existed).

    The blast radius is one of three FIXED names joined onto the round's own
    root — the agent contributes no part of it, so there is no traversal to
    defend against and the recursive branch below cannot reach anything but a
    directory the channel itself named. That branch exists because `Write`
    creates parent directories: an agent that writes
    `.autoloop-validation-request/note.txt` leaves a DIRECTORY at the request
    path, and a sweep that gave up there would leave residue outside every
    task's approved paths, which parks the candidate.

    A symlink is removed as the LINK and never followed, so a link pointing out
    of the worker repo costs the link and leaves its target alone.
    """
    try:
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    except OSError:
        pass
    try:
        return not (path.is_symlink() or path.exists())
    except OSError:
        return False


#: The line shape an assumption is reported on: the DECLARATION form and only
#: that — `ASSUMPTION:` first on its line, optionally indented with spaces or
#: tabs, nothing else in front of it. Case-insensitive because the agent writes
#: this by hand.
#:
#: The anchor is the whole safety property, and leading punctuation is where it
#: leaks. Prose about the convention ("write an ASSUMPTION: line when...") is
#: excluded by the anchor alone, but a MARKUP prefix is not: `> ASSUMPTION:
#: <what you assumed...>` is what an agent quoting the instruction it was given
#: writes, and `- ASSUMPTION: ...` is what one summarising the rule as a bullet
#: writes. Admitting either lets an echo of the prompt become a disclosure the
#: agent never made, in the one section of the packet a reviewer is most likely
#: to read on its own — so `>`, `-` and `*` are refused. The cost is the
#: opposite failure (a genuine disclosure written as a bullet is not collected),
#: which is why `_SMALLEST_REVERSIBLE_READING` states the exact form and says
#: what a prefix does; between a missed line that is still in `report_details`
#: and a fabricated line presented as a deliberate choice, the miss is the one
#: to take.
#:
#: One residual is accepted rather than solved: the instruction's own example
#: line is written in the accepted form (indented two spaces), so an agent that
#: reproduces the prompt VERBATIM and unmarked is collected. That is inherent —
#: any rendering of "the exact form" is by construction indistinguishable from a
#: declaration in that form — and it is bounded by what such an echo says: the
#: placeholder text `<what you assumed, and what you would have asked>`, which a
#: reviewer reads as an echo, not as a choice. The markup-prefixed shapes above
#: are the ones worth refusing, because those look like real sentences.
_ASSUMPTION_RE = re.compile(r"^[ \t]*assumption:[ \t]*(.+)$", re.IGNORECASE | re.MULTILINE)

#: What the agent is told to do with an ambiguity, and how to disclose it.
#:
#: This is the executor-side half of retiring `ask_user`. The loop cannot stop
#: and ask a human mid-run any more, so the instruction has to say what to do
#: INSTEAD — and "use your judgement" is not that. Two rules, both narrow:
#: prefer the reading that is smallest and easiest to undo, and write down the
#: reading you took. The second is what keeps the first honest; an undisclosed
#: assumption looks exactly like a misunderstanding by the time a reviewer sees
#: the diff, and the review is the last point at which either can be caught.
_SMALLEST_REVERSIBLE_READING = (
    "If the task is ambiguous, do NOT stop to ask — this loop has no human in "
    "it to answer, and a question here would just stall the run. Take the "
    "SMALLEST REVERSIBLE READING: the narrowest interpretation that satisfies "
    "the task as written, preferring a change that is easy to undo or extend "
    "over one that forecloses the other reading. Then disclose it: write one "
    "line per choice, at the start of a line, in the exact form\n"
    "  ASSUMPTION: <what you assumed, and what you would have asked>\n"
    "The word must come FIRST on its line (indenting is fine). A line that "
    "starts with a bullet, a quote marker or a number — `- ASSUMPTION:`, "
    "`* ASSUMPTION:`, `> ASSUMPTION:` — is read as prose about this "
    "instruction, not as a disclosure, and is NOT collected.\n"
    "These lines are collected verbatim and shown to the reviewer who "
    "authorizes your work, so write them for that reader — one sentence, "
    "concrete, naming the alternative reading you did not take. Do not use "
    "them for a summary of what you did; that goes in your normal report."
)


#: What the agent is told to do about the ways its OWN change fails, before it
#: returns.
#:
#: **The measured gap this closes** (2026-08-20, over every reviewer verdict in
#: `transcript.jsonl` — 580 directives): 229 `revise` against 80 `push`, a 74%
#: rejection rate. Clustering the 229 revise reasons, 112 of them (49%) are the
#: SAME SHAPE — "still", "despite", "remains": the claim is partly met and one
#: named case is not. A further 16 (7%) are fail-open ("silently disable", "can
#: mistake", "permanently disable"). The rounds were not sloppy; they were
#: CALIBRATED DIFFERENTLY. Everything else in this prompt points the agent at
#: making the change, and `_run_implementation` then grades it by whether
#: validation passed — while the reviewer grades whether the task's one stated
#: claim holds in EVERY case. Nothing here asked the agent to look for how its
#: own change fails, so half of all rounds died in the gap between those two
#: bars. This is the reviewer's own job, handed back one step earlier, where the
#: agent still has the context and the tools to act on it.
#:
#: **Bounded on purpose, because the unbounded version makes the SECOND-worst
#: failure worse.** "Make it robust" is an open invitation to go fix things, and
#: `changed_paths_outside_approved` has already parked 9 distinct tasks and cost
#: port-01 its entire branch (10 unapproved files across 8 commits). So the
#: instruction is pinned to the claim THIS task states and to the scope the task
#: was already authorized against; a failure outside that scope is REPORTED
#: rather than fixed, which is the half a reviewer can still act on.
#:
#: **The scope is named by RENDERING IT** (revision round 6, 2026-08-22, on the
#: reviewer's instruction — this reverses round 5, which had removed the list).
#: The bound above is only usable if the agent can tell an in-scope fix from a
#: report-only finding, and "your approved scope" does not let it: the agent
#: cannot see `Task.approved_paths`, and guessing at the line is how a round
#: either overruns it or, worse, silently declines a fix it was authorized to
#: make. So `_scope_instruction` below renders the list this sentence means, and
#: round 5's two objections are answered rather than avoided — the list comes
#: from `tasks.effective_approved_paths` (so the always-authorized trackers are
#: IN it, and it is the same computation both scope gates use, not a paraphrase
#: of one), and the entries are the ones `tasks._validate_approved_path` already
#: allowlisted to `[A-Za-z0-9._-]` segments, rendered one line per entry so a
#: record that somehow escaped that check still cannot open a line. Nothing
#: about authorization moves: `tasks.unauthorized_paths` against
#: `TaskExecution.allowed_paths` is still the only thing that decides what a
#: round was allowed to change, and this module still neither reads nor writes
#: that record.
#:
#: **Evidence, not reassurance.** "I checked and it is fine" is unfalsifiable
#: and costs a reviewer the same read as no answer at all, so the instruction
#: asks for the cases considered AND where each is handled. A reviewer can check
#: that artifact against the diff instead of deriving the case list from
#: scratch.
#:
#: **The enumeration is DELIBERATELY NOT PARSED.** No regex, no
#: `ExecutionOutcome` field, no packet section — unlike `_ASSUMPTION_RE`,
#: `_CLEANUP_RE` and `_REVERT_RE`, which exist because something COMPUTES with
#: what they match (a durable cross-round record; an actual unlink; an actual
#: restore from the base commit). Nothing computes with this: it
#: is prose for a human reviewer, and it already reaches them, because
#: `result.raw_text` rides to `ExecutionOutcome.details` and on into
#: `TaskExecution.report_details`, which `packet._format_executor_report`
#: renders. Adding an extractor would mean a new field in `worktask.py` and a
#: new section in `packet.py` to carry text that is already carried. The heading
#: is a fixed string so a reviewer can find it by eye; it is not an anchor, and
#: it deliberately collides with neither regex above, so an adversarial-case
#: line can never be harvested into the assumptions a reviewer reads most
#: closely.
#:
#: One rule before editing the text: it must not name a document or a list it
#: does not itself carry. Round 1 said "the files this task's description and
#: approved decomposition already cover", which pointed a task with no plan at a
#: document it does not have. The reference below ("under APPROVED SCOPE") is
#: safe because `_agent_prompt` renders that section UNCONDITIONALLY — a task
#: with no approved paths gets the fail-closed branch of `_scope_instruction`,
#: not an absent section — so the referent exists on every path this prompt
#: takes.
_ADVERSARIAL_SELF_TEST = (
    "Before you return, ADVERSARIALLY TEST YOUR OWN CLAIM. Validation passing "
    "and the claim holding are DIFFERENT BARS: the reviewer grades whether the "
    "one claim this task states holds in every case, and most rejected rounds "
    "die in that gap. So spend your last effort trying to BREAK your own "
    "change — hunt the inputs and states in which the claim you were asked to "
    "make would still fail: empty, missing, malformed or unavailable input, "
    "the boundary, the error path, the case an earlier round already got "
    "wrong.\n"
    "Look hardest at FAIL-OPEN failures, where rounds most often lose: a check "
    "that silently PASSES, or a guard that quietly switches itself off, when "
    "what it needs is absent or unreadable — the alarm never fires and nothing "
    "says so. An ECHO is the same class: text that was GIVEN to a model, read "
    "back as if it were evidence the model produced.\n"
    "Fix only the failures that fall INSIDE THIS TASK'S APPROVED SCOPE — the "
    "exact path list under APPROVED SCOPE below, which is the list the loop "
    "grades your diff against. A failure outside it you REPORT rather than "
    "fix, naming the file and what breaks, so the next round can pick it up. "
    "This is bounded to the claim you were given: it is not permission to "
    "improve the code, to widen the task, or to go fixing whatever you find "
    "on the way.\n"
    "Then show your work: \"I checked and it is fine\" is unfalsifiable and "
    "the reviewer cannot verify it. End your report with a section headed\n"
    "  ADVERSARIAL CASES:\n"
    "and under it one line per case, naming the case AND where it is handled — "
    "the function that handles it, the test that pins it, or why it cannot "
    "arise. List the cases you considered and dismissed too; the reviewer is "
    "checking your reasoning, not only your diff."
)


#: The heading of the section `_ADVERSARIAL_SELF_TEST` points at, and the prose
#: under the rendered list. Fixed text, no interpolation — the only thing the
#: task contributes to this section is the path lines between them.
_SCOPE_HEADING = (
    "APPROVED SCOPE — the exact paths this round is graded against, and what "
    'the sentence above means by "this task\'s approved scope":'
)
_SCOPE_TRAILER = (
    "That list is the loop's own answer, not a summary of one: it is "
    "`tasks.effective_approved_paths` — the paths this task declared, UNION "
    "the documentation trackers every task is authorized to record itself in "
    "— which is exactly what your diff is compared against afterwards. An "
    "entry ending in '/' means that directory and everything under it; any "
    "other entry means that one file. Being listed is not an instruction to "
    "touch a file: it is where a failure you find is yours to FIX. A failure "
    "anywhere else you REPORT, naming the file and what breaks."
)

#: The fail-closed branch: rendered instead of a list when the task declares no
#: approved paths at all. It exists so the reference in `_ADVERSARIAL_SELF_TEST`
#: can never dangle, and it says the safe thing rather than nothing — an absent
#: section would read as "no limit stated", which is the opposite of what an
#: empty scope means (`tasks.effective_approved_paths` returns `()` for an
#: unscoped task precisely so it authorizes nothing). Unreachable in production:
#: `orchestrator._dispatch_task_postcommit` refuses to dispatch a task with no
#: `approved_paths` (`approved_paths_missing`) before any agent runs, so the
#: last sentence asks the agent to say so if it ever reads this.
_SCOPE_NONE = (
    "APPROVED SCOPE: none. This task declares no approved paths, so nothing "
    "here is authorized to change. REPORT what you find — every case, in your "
    "own report — and change nothing. The loop refuses to dispatch an unscoped "
    "task, so if you are reading this line at all, say so in your report too."
)


def _scope_entry(path: str) -> str:
    """One approved-path entry as it appears in the prompt — on ONE line.

    Identity on every entry the loop can actually produce, and deliberately so:
    `tasks._validate_approved_path` already refuses anything whose segments are
    not `[A-Za-z0-9._-]` (no whitespace, no control characters, no globs), and a
    SECOND path validator here is exactly the drift `tasks.unauthorized_paths`'
    docstring argues against — two implementations of one rule eventually
    disagree, and this one would be the copy nothing enforces.

    So this is not validation; it is rendering. `Task` is a plain dataclass with
    no `__post_init__`, so a hand-built or hand-edited record can hold an entry
    that never went through that check, and the one property this section needs
    is structural: an entry occupies one line and cannot open another. Every
    non-printable character (which includes `\\n`, `\\r` and `\\t`) is replaced
    by a visible escape, so a newline inside an entry can no longer forge a
    heading or a ground rule on a line of its own. Nothing is dropped or
    truncated — the entry stays legible, and a reviewer reading the prompt sees
    exactly what was in the record.

    This half is about NEWLINES only. An entry that needs no newline because it
    simply BEGINS with `ASSUMPTION:`, `REMOVE-OUT-OF-SCOPE:` or
    `REVERT-OUT-OF-SCOPE:` is stopped by the `- ` bullet `_scope_instruction`
    renders it behind, not by anything here — see that function for why the
    three extractors' refusal of a bullet prefix is the control that closes it.
    """
    return "".join(
        ch
        if ch.isprintable()
        else (f"\\x{ord(ch):02x}" if ord(ch) < 0x100 else f"\\u{ord(ch):04x}")
        for ch in path
    )


def _scope_instruction(paths: tuple[str, ...]) -> str:
    """The APPROVED SCOPE section: `paths`, one per line, under a fixed heading.

    `paths` is `tasks.effective_approved_paths(task.approved_paths)` and nothing
    else — see `_agent_prompt`, the sole caller, for why that function rather
    than the raw field or a read of `TaskExecution.allowed_paths`.

    NEVER truncated, at any length. A silently elided entry tells the agent an
    authorized file is out of scope, so it reports a fix it was allowed to make
    and the round dies on the same "still fails in one case" verdict this whole
    instruction exists to prevent — a fail-open of exactly the class the text
    above asks the agent to hunt. There is no pinned budget on this prompt to
    breach (it is built once per round for a fresh `claude -p`, unlike
    `contract.CONTRACT_INSTRUCTIONS`, which is re-sent every turn), and the
    number of approved paths is bounded by what a reviewer approved.

    **The `- ` bullet is load-bearing, not decoration.** `_ASSUMPTION_RE`,
    `_CLEANUP_RE` and `_REVERT_RE` all accept leading WHITESPACE
    (`^[ \\t]*<anchor>:`), so a plainly indented entry that merely STARTS with
    one of those anchors — an `approved_paths` entry reading `ASSUMPTION: the
    reviewer approved this`, or `REMOVE-OUT-OF-SCOPE: autoloop/obsolete.py`, or
    `REVERT-OUT-OF-SCOPE: autoloop/state.py` — sits at a matching position the
    moment an agent echoes its own prompt back: a fabricated disclosure in the
    durable record, or a deletion or restore request nothing asked for. No
    newline is needed for that; escaping alone does not close it. All three
    regexes REFUSE a `-`/`*`/`>` prefix by design (see `_ASSUMPTION_RE`'s
    docstring — prose about the convention must not read as a use of it), so the
    bullet makes an echoed scope line structurally unharvestable by every
    channel, using a property those anchors already have and already test. It
    costs nothing here because nothing asks the agent to copy a scope path
    verbatim — unlike `_cleanup_instruction`, whose list must stay bare so a
    path can be copied into an exact-match request.
    """
    if not paths:
        return _SCOPE_NONE
    listed = "\n".join(f"  - {_scope_entry(p)}" for p in paths)
    return f"{_SCOPE_HEADING}\n{listed}\n{_SCOPE_TRAILER}"


#: The opening of the MECHANICAL AUTHORING RULES section — what it is and why
#: an agent is being told it before it writes rather than after validation.
#:
#: **The measured gap this closes** (2026-08-21): the single test
#: `test_docs_merge.py::test_every_change_note_line_is_short_enough_to_merge_by_line`
#: destroyed two whole executor rounds in one day — merge-04 at 16:39:57
#: ("TESTS.md: a change note grew to 976 chars") and blk-02 at 17:43:57
#: ("SUMMARY.md: a change note grew to 773 chars"). Both rounds implemented
#: their task correctly and were thrown away over a documentation line-length
#: rule the agent is never told about. It cannot find the rule on its own
#: either: it has Read/Grep/Glob/Edit/Write and no way to know which of ~200
#: test files gate its diff, so it rediscovers the limit by failing, once per
#: round, forever.
#:
#: **Narrow on purpose.** This is NOT a summary of the test suite and must not
#: grow into one. The target is the small set of MECHANICAL formatting rules
#: that reject otherwise-correct work — rules an agent cannot infer from the
#: file it is editing and cannot satisfy by understanding the task better.
#: Project conventions belong in `CLAUDE.md`, which the agent already reads.
_AUTHORING_HEADING = (
    "MECHANICAL AUTHORING RULES — repository formatting rules that reject an "
    "otherwise-correct round at validation. They are stated here because you "
    "cannot see the checks that enforce them, and rediscovering one by failing "
    "costs the whole round."
)

#: The generic name for the trackers, used only if `NOTE_TRACKERS` is somehow
#: empty. An empty list would silently turn the sentence into "the change-note
#: section of " — a rule with no subject, which is the fail-open shape (the
#: text still looks like guidance while naming nothing).
_AUTHORING_TRACKERS_FALLBACK = "the repository's documentation trackers"


def _authoring_rules() -> str:
    """The mechanical change-note rules, with the ENFORCED limit in them.

    Reads `note_merge.MAX_NOTE_LINE_CHARS` and `note_merge.NOTE_TRACKERS`
    through the module at call time, deliberately — not `from ... import` at
    module scope. The number in the brief and the number the validator enforces
    have to be one value, so the two consumers name one constant
    (`test_docs_merge.py` imports the same one), and reading it here means a
    changed limit reaches the next brief with no second edit. A hard-coded copy
    that silently disagreed with the test would be worse than saying nothing:
    the agent would keep to a limit nothing enforces, and still lose the round
    to the one that does.

    The wording carries two precision points that the digits alone do not, and
    both are places a technically-true brief still fails the round:

      * the check is `len(line) <= MAX`, so the text says AT MOST — "under" or
        "fewer than" would be off by one at the boundary;
      * it measures the WHOLE line, `| date | task-id |` cells included, so a
        brief that said "keep your note short" would let a 690-character note
        sit in a 740-character row and fail anyway.

    The marker sentence is the second mechanical rule of the same section and
    the same test file, kept for the same reason: `notes_section` requires the
    CHANGE-NOTES comment to appear EXACTLY once, and quoting it in full while
    explaining the machinery — how docs-01 shipped the bug in `SUMMARY.md`
    itself — turns automatic note merging off for that tracker with no symptom
    but the merge sweep halting. It carries no number, so it adds no drift
    surface.
    """
    trackers = ", ".join(sorted(note_merge.NOTE_TRACKERS)) or _AUTHORING_TRACKERS_FALLBACK
    limit = note_merge.MAX_NOTE_LINE_CHARS
    return (
        f"{_AUTHORING_HEADING}\n"
        f"Recording a change note ({trackers}): each of those files ends with "
        "an append-only change-note section, opened by a CHANGE-NOTES comment. "
        "A note is ONE NEW LINE appended after the last line of that section. "
        "Do not grow, edit, reorder or delete a line that is already there, "
        "and put nothing after your own line. Name that marker CHANGE-NOTES in "
        "prose and never write the comment out in full a second time: a "
        "duplicate switches automatic note merging off for the whole file, "
        "silently.\n"
        f"Every line in that section must be AT MOST {limit} characters — the "
        "WHOLE line, counting the leading `| date | task-id |` cells, not just "
        f"your sentence. Exactly {limit} passes; one more fails. A single "
        "over-long note fails validation and the round is discarded with all "
        "the work in it, so when a note does not fit, append a SECOND line "
        "rather than a longer one."
    )


#: The line shape a CLEANUP REQUEST is written on — `REMOVE-OUT-OF-SCOPE:`
#: first on its line, optionally indented, nothing else in front of it.
#: Deliberately the same anchoring discipline as `_ASSUMPTION_RE` above,
#: including the refusal of `-`/`*`/`>` prefixes, so an agent summarising the
#: instruction as a bullet is reporting, not requesting.
#:
#: The agent has no way to delete a file itself: `WRITE_ALLOWED_TOOLS` is
#: Read/Grep/Glob/Edit/Write and `Bash` is disallowed, so "remove the residue
#: you added" — a correct and common review — was literally unperformable, and
#: the observed result was a zero-byte file committed in place of an absent one
#: (roadmap-01, 2026-08-18). This line is how the agent asks the executor to do
#: the one thing it cannot; `_apply_recorded_cleanup` is what decides whether
#: the request is authorized.
#:
#: **Echo-safety is structural here, not textual** — a stronger property than
#: `_ASSUMPTION_RE` has, and the reason this can afford to be a plain-text
#: channel at all. An agent that quotes this instruction verbatim emits the
#: placeholder `<repository-relative path>`, which is not a path the loop ever
#: recorded, so `authorized_cleanup_paths` refuses it and nothing is deleted.
#: A fabricated request cannot delete anything either: the authority is the
#: persisted record, and this line only ever SELECTS from it.
_CLEANUP_RE = re.compile(
    r"^[ \t]*remove-out-of-scope:[ \t]*(.+)$", re.IGNORECASE | re.MULTILINE
)


#: The line shape a REVERT REQUEST is written on — `REVERT-OUT-OF-SCOPE:`
#: first on its line, optionally indented, nothing else in front of it. The
#: same anchoring discipline as `_CLEANUP_RE` and `_ASSUMPTION_RE` above,
#: including the refusal of `-`/`*`/`>` prefixes, so an agent summarising the
#: instruction as a bullet is reporting, not requesting.
#:
#: **Why a SECOND anchor and not a widened first one** (scope-05, 2026-08-24).
#: scope-04's exception covers a CREATED file: the agent asks and the executor
#: unlinks. It could do nothing at all for an out-of-scope EDIT, which is the
#: shape that actually parks tasks — port-01's contamination on 2026-08-20 was
#: ten edited files and zero creations, so "strip the residue" was again
#: literally unperformable and the same edits were handed to every following
#: round on the same branch. The two instructions stay DISTINCT because their
#: end states differ on a file that existed before the task: `REMOVE` deletes
#: it, `REVERT` puts the base's bytes back. They CONVERGE on a created path,
#: which has no base content — see `_revert_recorded_file`, where "restore the
#: base state" of a path absent at the base is defined, deliberately and
#: testably, as making it absent.
#:
#: Echo-safety is structural here for the same reason as `_CLEANUP_RE`: an
#: agent quoting this instruction back emits the placeholder
#: `<repository-relative path>`, which is not a path the loop ever recorded, so
#: `authorized_cleanup_paths` refuses it and nothing is restored. A fabricated
#: request cannot restore anything either — the authority is the persisted
#: record, and this line only ever SELECTS from it.
_REVERT_RE = re.compile(
    r"^[ \t]*revert-out-of-scope:[ \t]*(.+)$", re.IGNORECASE | re.MULTILINE
)


def _cleanup_instruction(paths: tuple[str, ...], revert_enabled: bool = False) -> str:
    """The cleanup section of the agent prompt, or "" when there is nothing to
    clean up.

    Rendered ONLY when the loop has actually recorded out-of-scope residue for
    this execution, so the ordinary round's prompt is unchanged and no agent is
    told about a capability it has no occasion to use. The paths are listed
    literally because the match is exact: an agent that retypes a path
    approximately gets a refusal, not a near-miss deletion.

    `revert_enabled` adds the SECOND request form, and only when a revert could
    actually be performed this round — `ImplementExecutor._run_implementation`
    passes True only when a `revert_authority` is wired AND it yielded a base
    sha. Fail-closed exactly like the section as a whole: with no authority the
    paragraph is absent, the anchor never appears in the prompt, and every
    `REVERT-OUT-OF-SCOPE:` line an agent writes anyway is ignored and reported.

    The wording is doing one specific job beyond describing the mechanism — it
    has to stop the capability being read as scope. "You may remove this file"
    is one sentence away from "this file is mine to edit", and the second
    reading is the one that would quietly undo the never-widened
    `allowed_paths` rule this whole mechanism is built to preserve. The revert
    paragraph repeats that sentence rather than relying on the first one: it is
    the form an agent reaches for when the path is a file it merely EDITED, i.e.
    exactly when "this file is mine to work in" is most tempting.
    """
    if not paths:
        return ""
    listed = "\n".join(f"  {p}" for p in paths)
    text = (
        "Out-of-scope residue from an earlier round of THIS task — the loop "
        "recorded these paths itself, from its own diff of what your earlier "
        "rounds committed, because your approved scope did not cover them:\n"
        f"{listed}\n"
        "You may ask for any of them to be DELETED. You have no Bash access "
        "and no delete tool, so write one line per path, at the start of a "
        "line, in the exact form\n"
        "  REMOVE-OUT-OF-SCOPE: <repository-relative path>\n"
        "copying the path exactly as listed above — no quotes, no backticks, "
        "no leading './'. A path that does not match one of the lines above "
        "exactly is ignored and reported as ignored. The executor performs the "
        "deletion after you finish and before validation runs.\n"
        "This is permission to REMOVE, and nothing else. It does not authorize "
        "you to edit, recreate, rename into, or otherwise write these paths, "
        "and it says nothing at all about their directories or any other file. "
        "Remove only what the review actually asks you to remove; residue a "
        "reviewer has not objected to is work, not litter."
    )
    if revert_enabled:
        text += (
            "\nDeleting is wrong for a path that ALREADY EXISTED before this "
            "task and was merely edited out of scope — the file has to stay, "
            "it just has to say what it said before. For those, ask for the "
            "edit to be UNDONE instead, one line per path, at the start of a "
            "line, in the exact form\n"
            "  REVERT-OUT-OF-SCOPE: <repository-relative path>\n"
            "copying the path exactly as listed above, under the same rules: a "
            "path that does not match one of those lines exactly is ignored and "
            "reported as ignored. The executor restores the file from the "
            "task's recorded base commit — the content git holds, not what you "
            "believe the file used to say, so you do not need to reconstruct it "
            "and must not try. It runs after you finish and before validation, "
            "like the deletion.\n"
            "A path that did not exist at the base commit has no content to "
            "restore, so reverting it makes it absent — the same end state "
            "REMOVE gives. Asking for BOTH on one path is not an error: the "
            "removal is performed and the revert of that same path is reported "
            "as superseded.\n"
            "This is permission to PUT BACK, and nothing else. It authorizes no "
            "edit of your own to these paths, and it widens your approved scope "
            "by exactly nothing — an ordinary edit to any of them is as "
            "unauthorized as it was before they were ever recorded."
        )
    return text


#: Introduces `tasks.Task.decomposition` in the agent's prompt.
#:
#: The reviewer approved this plan before any code was written, so it is the
#: shape of the work rather than a suggestion — but it is still PROSE, and the
#: agent implements it in one dispatch: nothing here schedules a step, and the
#: orchestrator does not dispatch per step (splitting a task is `split-01`'s
#: mechanism). The instruction is "work them in order and do not widen the
#: plan", which is what makes each step reviewable in the diff a reviewer
#: eventually reads.
_DECOMPOSITION_HEADER = (
    "Approved decomposition — agreed with the reviewer BEFORE any code was "
    "written. Work the steps in order and keep to their scope; if the plan "
    "turns out to be wrong, say so in your report rather than quietly "
    "implementing a different one.\n"
)


def _agent_prompt(
    task: Task,
    feedback: str | None,
    cleanup_paths: tuple[str, ...] = (),
    advisory_brief: str = "",
    revert_enabled: bool = False,
) -> str:
    parts = [
        "You are a write-capable coding subagent inside an automated "
        "repository task-implementation loop (a German language-learning "
        "app; see CLAUDE.md).",
        f"Task id: {task.id}",
        f"Title: {task.title}",
        task.description,
    ]
    if task.decomposition:
        parts.append(_DECOMPOSITION_HEADER + task.decomposition)
    parts += [
        "Ground rules: you may Read, Grep, Glob, Edit and Write. You may "
        "ONLY modify files inside your current working directory — this "
        "task's own isolated worker repository — and must never attempt to "
        "reach any path outside it. You have no Bash access and must not "
        "attempt to run `git` or any other command: committing is not your "
        "job, the orchestrator commits your changes after you finish and "
        "after validation passes. Do not delegate to another agent.",
        _SMALLEST_REVERSIBLE_READING,
        # Unconditional — every implement AND every revise round gets it. A
        # revise round is where the claim has ALREADY been judged not to hold
        # in some case, so it is the last place to drop the instruction to go
        # looking for the next one. Placed here, after the ground rules that
        # bound what it may touch and before the cleanup section, so the
        # documented cleanup-then-feedback adjacency below is untouched.
        _ADVERSARIAL_SELF_TEST,
        # Immediately after it, because the sentence above says "below" and a
        # reference whose referent moved is the round-1 bug. Also
        # unconditional: an unscoped task gets `_SCOPE_NONE` rather than an
        # empty section, so no round is left reading "your approved scope"
        # with nothing to read it against.
        #
        # `effective_approved_paths` with its DEFAULT trackers is the same
        # call, with the same argument, that the pre-commit scope gate makes
        # (`orchestrator.py:5337`, via `Orchestrator._tracker_paths()` which
        # returns `tasks.TRACKER_PATHS`) and that seeds and re-syncs
        # `TaskExecution.allowed_paths` on every dispatch. Two computations of
        # one list would be a prompt that quietly disagrees with the check —
        # so if the tracker source ever moves off that constant, this call has
        # to move with it (pinned by
        # `test_the_prompt_and_the_scope_gate_read_the_same_tracker_source`).
        _scope_instruction(effective_approved_paths(task.approved_paths)),
        # Last of the unconditional sections, so it displaces nothing: the
        # adversarial instruction still names the scope list "below" and still
        # sits immediately above it, and the cleanup-then-feedback adjacency
        # below is untouched. Unconditional because the rule it states is
        # unconditional — every round records a change note, and a round that
        # does not is not harmed by reading one paragraph about the shape of
        # one.
        _authoring_rules(),
    ]
    if advisory_brief:
        # Rendered ONLY when a rendezvous is actually running for this round
        # (`AdvisoryValidation.offerable`), because a brief describing a channel
        # nothing answers would spend the agent's turns polling a file that
        # never appears — the same "told about a capability it does not have"
        # failure `_cleanup_instruction` avoids by staying silent. Placed after
        # the unconditional sections so it displaces none of them: the
        # adversarial instruction still names the scope list "below" and still
        # sits immediately above it.
        parts.append(advisory_brief)
    cleanup = _cleanup_instruction(cleanup_paths, revert_enabled)
    if cleanup:
        # After the ground rules, which say the agent cannot run commands, and
        # before the feedback that is usually what asks for the removal.
        parts.append(cleanup)
    if feedback:
        parts.append(f"Revision feedback from the previous review round: {feedback}")
    return "\n\n".join(parts)


def _extract_cleanup_requests(raw_text: str) -> tuple[str, ...]:
    """The paths an agent's output ASKED to have deleted, in the order written.

    Deduplicated (a path requested twice is one request) and stripped of
    surrounding whitespace, and normalised no further than that. No quote,
    backtick or `./` stripping, deliberately: every normalisation step is a
    place where a string the agent controls gets closer to a path the loop
    recorded, and a non-match here is FAIL-CLOSED — it deletes nothing, and the
    round reports the request as ignored. The prompt states the exact form and
    says to copy the path literally, so a near-miss is an agent that did not
    follow a stated instruction, not a case worth guessing at.
    """
    return _extract_requests(_CLEANUP_RE, raw_text)


def _extract_revert_requests(raw_text: str) -> tuple[str, ...]:
    """The paths an agent's output ASKED to have restored, in the order written.

    Identical rules to `_extract_cleanup_requests` — deduplicated, stripped, and
    normalised no further — for identical reasons, which is why both go through
    `_extract_requests` rather than being written twice. A near-miss is
    FAIL-CLOSED here too: it restores nothing and the round reports the request
    as ignored.
    """
    return _extract_requests(_REVERT_RE, raw_text)


def _extract_requests(pattern: "re.Pattern[str]", raw_text: str) -> tuple[str, ...]:
    """Shared body of the two extractors above — see `_extract_cleanup_requests`
    for why nothing here normalises a path beyond stripping whitespace."""
    seen: list[str] = []
    for match in pattern.finditer(raw_text or ""):
        text = match.group(1).strip()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def _cleanup_note(removed: tuple[str, ...], ignored: tuple[str, ...]) -> str:
    """The sentence the round's summary carries about cleanup, or "".

    Asymmetric on purpose. REMOVED paths are named in full: every one of them
    came out of the loop's own record (`authorized_cleanup_paths` returns a
    subset of it), so the text is bounded by a set the loop wrote and cannot be
    grown by the agent. IGNORED paths are COUNTED and never named: those are
    strings the agent chose, of any length and any content, and this summary
    becomes the commit message (`orchestrator._dispatch_task_postcommit` builds
    `title\\n\\nsummary`). A refused request must be visible — a silently
    dropped one reads exactly like a satisfied one to the reviewer who asked
    for the removal — but it does not need to be quoted to be visible, and the
    agent's own output is already carried verbatim in `report_details`.
    """
    parts = []
    if removed:
        parts.append(
            f" Removed {len(removed)} recorded out-of-scope path(s): "
            + ", ".join(removed)
            + "."
        )
    if ignored:
        parts.append(
            f" Ignored {len(ignored)} removal request(s) for path(s) this task "
            "has no recorded out-of-scope write to (nothing was deleted for "
            "them; the requests are in the executor report below)."
        )
    return "".join(parts)


def _remove_recorded_file(root: Path, rel: str) -> bool:
    """Unlink `rel` inside `root`. True only when a file was actually removed.

    Called ONLY for a path `authorized_cleanup_paths` has already matched
    against the loop's own record, so every check here is defence in depth
    against a record that has been tampered with rather than a real
    expectation. It refuses an absolute path, any `..` segment, anything whose
    parent does not resolve inside `root`, and anything that is not a regular
    file or a symlink — a directory is never removed, so no recursive delete
    exists on this path at all.

    A symlink is unlinked as the LINK, never followed: `Path.unlink` removes
    the entry, so a recorded path that is somehow a symlink to something
    outside the worker repo costs the link and leaves its target alone.

    False for a path that is already absent, which is a no-op rather than a
    failure: the end state the request asked for (the file is not in the tree)
    already holds, and there is nothing to report to a reviewer about it.
    """
    if not rel.strip() or rel.startswith("/") or ".." in Path(rel).parts:
        return False
    target = root / rel
    try:
        if not target.is_symlink() and not target.is_file():
            return False
        base = root.resolve()
        parent = target.parent.resolve()
        if parent != base and base not in parent.parents:
            return False
        target.unlink()
    except OSError:
        return False
    return True


class _Reverts(NamedTuple):
    """What one round's revert pass actually did, split by WHY.

    Six separate facts rather than one list, because a reviewer who asked for
    the residue to be stripped needs to tell them apart, and because collapsing
    them is how a refusal comes to read like a success:

      * `done` — restored to the base state. Named in full downstream: every
        entry came out of the loop's own record.
      * `failed` — authorized (the record holds it) and NOT restored. Also
        named: same bounded source, and a silent one here is the fail-open.
      * `refused` — the record does not hold it. COUNTED, never named: these
        are strings the agent chose and the round summary becomes the commit
        message (the rule `_cleanup_note` already follows).
      * `superseded` — also removed by this round's removal pass, which ran
        first. Named; the removal is the end state.
      * `base_unavailable` — this round has no base commit to restore from: no
        `revert_authority` was wired, or the record names none, or what it
        names is not a plain object id. NOTHING was restored.
      * `base_unreadable` — there IS a base sha and git would not read its
        tree. Also nothing restored.

    The last two are separate booleans rather than one, because they are
    different facts with different remedies — "the capability is not wired
    here" versus "git could not answer" — and reporting the first as the second
    would send a reviewer looking for a corrupt repository. Both are kept out of
    `failed`'s wording for the same reason.
    """

    done: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    base_unavailable: bool = False
    base_unreadable: bool = False


#: Blob modes a revert will write back. Git tracks exactly one bit of file
#: permission, so these two are the whole of "a regular file at the base".
#: A `120000` (symlink) or `160000` (submodule) entry is REFUSED rather than
#: written out as file bytes — restoring a symlink as a text file containing its
#: target path is not the base state, it is a new and stranger edit.
_REVERTABLE_BLOB_MODES = ("100644", "100755")

#: What `_revert_base_sha` will pass to git. Defense in depth on a value that
#: comes from a persisted record rather than from an agent: anything that is not
#: a plain hex object name is refused HERE, before it can reach `rev-parse` as
#: an argument. The policy whitelist would already refuse a token starting with
#: `-`, so this is the second layer, not the only one.
_BASE_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _revert_recorded_file(git: GitGateway, rel: str, entry) -> bool:
    """Put `rel` back to the base state described by `entry`. True when it is
    there afterwards.

    `entry` is `(mode, kind, oid)` from `GitGateway.tree_entries` of the base
    commit's tree, or None when the base tree has no such path. Called ONLY for
    a path `authorized_cleanup_paths` has already matched against the loop's own
    record, so — exactly like `_remove_recorded_file`, whose guards this repeats
    and reuses — every check here is defence in depth against a tampered record
    rather than a real expectation.

    **The created-path rule, stated once and in code.** A path with no entry at
    the base did not exist there, so its base state is ABSENCE and this deletes
    it, through `_remove_recorded_file` and its guards rather than a second
    unlink. That is the deliberate convergence of the two instructions on the
    one shape where they cannot differ; `_REVERT_RE`'s comment says why they
    stay distinct everywhere else. A path that is ALREADY absent is True with
    nothing done: the requested end state holds.

    **It never falls through to that branch on an error.** `entry is None` means
    git positively answered "not at the base". A base tree that could not be
    read produces no entries to consult at all and is refused one level up
    (`_apply_recorded_reverts`, `base_unreadable`) — an unreadable base silently
    turning a revert into a deletion is the worst outcome available here.

    Refuses, in order: an empty/absolute/`..` path, a parent that does not
    resolve inside the worker repo, a non-blob or non-regular-file base entry, a
    blob git will not hand over, and a directory sitting at the target path (no
    recursive delete is reachable from a revert, exactly as none is from a
    removal). A SYMLINK at the target is unlinked as the link and replaced by
    the file — never written through, so a link pointing out of the worker repo
    costs the link and leaves its target alone.
    """
    root = git.repo_root
    if not rel.strip() or rel.startswith("/") or ".." in Path(rel).parts:
        return False
    target = root / rel
    try:
        # BEFORE anything is created: `Path.resolve()` is non-strict, so this
        # answers for a parent that does not exist yet, and the `mkdir` below
        # therefore cannot be the thing that puts a directory outside the root.
        base = root.resolve()
        parent = target.parent.resolve()
        if parent != base and base not in parent.parents:
            return False
    except OSError:
        return False
    if entry is None:
        try:
            if not (target.is_symlink() or target.exists()):
                return True     # already absent; the base state holds
        except OSError:
            return False
        return _remove_recorded_file(root, rel)
    try:
        mode, kind, oid = entry
    except (TypeError, ValueError):
        return False
    if kind != "blob" or mode not in _REVERTABLE_BLOB_MODES:
        return False
    try:
        content = git.blob_bytes(oid)
    except Exception:
        return False
    try:
        if target.is_symlink():
            target.unlink()
        elif target.is_dir():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        # Git tracks ONE permission bit. Restoring the bytes and leaving the
        # bit wrong would leave the path in `commit_range_paths` forever — the
        # diff would still show a mode change — which is precisely the thing a
        # revert exists to take out of the reviewed range.
        current = target.stat().st_mode
        os.chmod(
            target,
            (current | 0o111) if mode == "100755" else (current & ~0o111),
        )
    except OSError:
        return False
    return True


def _revert_note(reverts: _Reverts, recorded_ok: bool) -> str:
    """The sentence the round's summary carries about reverts, or "".

    Same asymmetry as `_cleanup_note`, and for the same reason: paths that came
    out of the loop's record are NAMED (the text is bounded by a set the loop
    wrote), and paths the agent chose are COUNTED (this summary becomes the
    commit message and an agent-chosen string has no bound at all).

    `recorded_ok` is False when the restore happened but the note of it could
    not be written to the execution record. Said out loud rather than swallowed:
    the repair is real and in the tree either way, and a reviewer reading a
    record with no revert on it needs to know which of the two it is.
    """
    parts = []
    if reverts.done:
        parts.append(
            f" Reverted {len(reverts.done)} recorded out-of-scope path(s) to "
            "their task base content: " + ", ".join(reverts.done) + "."
        )
    if reverts.superseded:
        parts.append(
            f" {len(reverts.superseded)} revert request(s) were superseded by "
            "this round's removal of the same path(s): "
            + ", ".join(reverts.superseded)
            + "."
        )
    if reverts.base_unavailable:
        parts.append(
            " No task base commit was available to this round — no revert "
            "authority is wired, or the execution record names none — so "
            "NOTHING was reverted and every revert request was refused."
        )
    elif reverts.base_unreadable:
        parts.append(
            " The task's recorded base commit could not be read, so NOTHING "
            "was reverted this round — every revert request was refused."
        )
    elif reverts.failed:
        parts.append(
            f" Could not revert {len(reverts.failed)} recorded path(s): "
            + ", ".join(reverts.failed)
            + "."
        )
    if reverts.refused:
        parts.append(
            f" Ignored {len(reverts.refused)} revert request(s) for path(s) "
            "this task has no recorded out-of-scope write to (nothing was "
            "restored for them; the requests are in the executor report below)."
        )
    if reverts.done and not recorded_ok:
        parts.append(
            " The revert(s) above could NOT be written to the execution record; "
            "the files are restored in the tree but the record does not say so."
        )
    return "".join(parts)


def _extract_assumptions(raw_text: str) -> tuple[str, ...]:
    """The `ASSUMPTION:` lines in an agent's own output, in the order written.

    Read out of the transcript rather than asked for as structured output for
    the same reason `changed_paths` is read from `git status` rather than
    taken from the agent's word: this executor runs ONE `claude -p` call and
    gets back text, so a separate structured channel would be a second call to
    keep in sync with the first. The difference is that this text is only ever
    SHOWN to a reviewer — nothing computes with it — so text is a sufficient
    carrier here in a way it explicitly is not for a path set.

    **Every matching line is kept, at its full length.** This function feeds a
    DURABLE record (`TaskExecution.assumptions`, accumulated across rounds), and
    it is the last point at which the text still exists anywhere the loop keeps:
    `report_details` holds the same lines, but it is REPLACED every round, so a
    line dropped or shortened here is gone from round 2 onwards — which is the
    cross-round persistence the record exists to provide, defeated at its
    source. The bounds belong where the constraint is, at render time
    (`packet.ASSUMPTIONS_MAX_CHARS` for the section, `packet.
    ASSUMPTION_MAX_CHARS_EACH` for one line), and the packet says what it
    withheld so a reviewer never reads a shortened list as complete.

    Whitespace is stripped from each captured line and empty captures are
    dropped: an empty assumption discloses nothing, and stripping is what makes
    the accumulator's duplicate check see one sentence as one entry.
    """
    found: list[str] = []
    for match in _ASSUMPTION_RE.finditer(raw_text or ""):
        text = match.group(1).strip()
        if not text:
            continue
        found.append(text)
    return tuple(found)


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
        cleanup_paths_for: Callable[[str], tuple[str, ...]] | None = None,
        revert_authority=None,
        advisory_max_calls: int = ADVISORY_VALIDATION_MAX_CALLS,
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
        # Reads THIS task's persisted `TaskExecution.out_of_scope_paths` — the
        # loop's own record of what earlier rounds demonstrably wrote outside
        # their scope, and the sole authority for the cleanup exception (see
        # `_cleanup_instruction` / `tasks.authorized_cleanup_paths`). Injected
        # as a callable, exactly like `worker_repo_root_for` above, so this
        # module keeps knowing nothing about where executions are stored;
        # `cli._build_executor` binds it to the same `TaskExecutionStore` the
        # orchestrator writes those records with. None — every direct
        # `execute()` test, and any embedder that does not wire it — means NO
        # cleanup authority at all, which is the fail-closed default.
        self._cleanup_paths_for = cleanup_paths_for
        # The revert exception's only authority (scope-05, 2026-08-24). An
        # object with `base_sha(task_id)` and `record_reverted(task_id, paths)`
        # — `worktask.RecordedRevertAuthority` in a wired run. It supplies the
        # BASE SHA a restore reads from and the place a completed restore is
        # written down; it deliberately does NOT supply the path list, which
        # stays `cleanup_paths_for`'s so that one record answers "is this path
        # authorized" for both instructions rather than two readers drifting.
        # `cli._build_orchestrator` binds it to the SAME `TaskExecutionStore` it
        # binds `cleanup_paths_for` to, which is what keeps that true in a real
        # run rather than only by intention.
        #
        # None — every direct `execute()` test, and any embedder that does not
        # wire it — means NO revert authority at all: the prompt never mentions
        # the capability, and every `REVERT-OUT-OF-SCOPE:` line is ignored and
        # reported as ignored. Same fail-closed default as `cleanup_paths_for`.
        self._revert_authority = revert_authority
        # How many advisory runs ONE round may pay for. A constructor override
        # rather than a config key: a key would have to be read in `cli.py` and
        # threaded through from there, and a setting nothing reads is worse than
        # no setting at all.
        self._advisory_max_calls = advisory_max_calls

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

    def _recorded_cleanup_paths(self, task: Task) -> tuple[str, ...]:
        """What the loop has recorded as this task's own out-of-scope residue.

        Fail-closed at both ends: no injected reader means no cleanup authority
        (an embedder that has not wired one is not silently granted the
        exception), and a reader that raises — a missing record, an unreadable
        or corrupt execution file — yields an empty set rather than propagating.
        The consequence of an empty answer is only that a removal cannot be
        requested this round; the consequence of guessing at one would be a
        deletion nothing authorized.
        """
        if self._cleanup_paths_for is None:
            return ()
        try:
            return tuple(self._cleanup_paths_for(task.id) or ())
        except Exception:
            return ()

    @staticmethod
    def _apply_recorded_cleanup(
        git: GitGateway, recorded: tuple[str, ...], raw_text: str
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Perform the deletions this round asked for AND is authorized to make.

        Returns `(removed, ignored)`: the paths actually unlinked, and the
        requested paths `recorded` does not cover. The gate is
        `tasks.authorized_cleanup_paths` and nothing else — the agent's line
        selects from the loop's record, it never adds to it, so a request for a
        path no round was ever recorded as writing out of scope deletes nothing
        no matter how it is phrased.

        Nothing here touches scope. `Task.approved_paths` and
        `TaskExecution.allowed_paths` are not read, not written and not
        consulted by this method, and no deletion decision anywhere consults
        them: this authorizes one unlink of one already-recorded file, and
        an ordinary edit to that same path stays exactly as unauthorized as it
        was — it lands, it is recorded out of scope, and the reviewer judges it,
        which is what the 2026-08-05 advisory-scope amendment already does.
        """
        requested = _extract_cleanup_requests(raw_text)
        if not requested:
            return (), ()
        authorized, ignored = authorized_cleanup_paths(requested, recorded)
        removed = tuple(
            sorted(p for p in authorized if _remove_recorded_file(git.repo_root, p))
        )
        return removed, tuple(sorted(ignored))

    @staticmethod
    def _apply_recorded_reverts(
        git: GitGateway,
        recorded: tuple[str, ...],
        base_sha: str,
        raw_text: str,
        removed: tuple[str, ...] = (),
    ) -> _Reverts:
        """Restore the paths this round asked for AND is authorized to restore.

        The claim scope-05 exists to make, in one method: an agent can name a
        path an earlier round of the same task EDITED outside its declared
        scope, and that path goes back to its `task_base_sha` content.

        **The authority is scope-04's, unchanged.** `recorded` is
        `TaskExecution.out_of_scope_paths` — the loop's own record, written from
        its own two path comparisons and never from anything an agent asserts —
        and `tasks.authorized_cleanup_paths` is the same exact-match gate the
        removal uses. The agent's line SELECTS from that record; nothing it
        writes can extend it. `Task.approved_paths` and
        `TaskExecution.allowed_paths` are not read, not written and not consulted
        anywhere on this path: an ordinary edit to a recorded path stays exactly
        as unauthorized as it was, and this only ever narrows a diff back toward
        the declared scope.

        **The content comes from git, never from the agent.** One `ls-tree` of
        the base commit's tree answers, for every requested path at once, both
        "what did this say at the base" and "did it exist at the base at all".
        That is what makes a revert checkable rather than a second edit — the
        base sha is LOOP-written and is the commit the reviewed range is already
        measured from, so "restored to base" and "gone from the range" are the
        same statement. A stale-base refresh moves it; that is fine and is why
        the property is phrased about the range rather than about a fixed sha.

        **Fail-closed at every absent input.** No base sha, a base sha that is
        not a plain object name, or a base tree git will not read: NOTHING is
        restored and every request is refused with a reason. In particular an
        unreadable base never reaches `_revert_recorded_file`'s created-path
        branch, so it cannot silently become a deletion.

        `removed` is what this round's removal pass already deleted, and it runs
        FIRST. A path named under both instructions is therefore removed, and
        its revert is reported as SUPERSEDED rather than performed — otherwise
        restoring the base bytes would quietly undo the deletion the same report
        asked for. The order is fixed here and stated in the prompt.
        """
        requested = _extract_revert_requests(raw_text)
        if not requested:
            return _Reverts()
        authorized, refused = authorized_cleanup_paths(requested, recorded)
        already = set(removed)
        superseded = sorted(p for p in authorized if p in already)
        targets = sorted(p for p in authorized if p not in already)
        if not targets:
            return _Reverts(
                refused=tuple(sorted(refused)), superseded=tuple(superseded)
            )
        if not base_sha:
            # No authority wired, or a record with no usable base sha. Nothing
            # is restorable, and saying so is the point: a round that reported
            # nothing here would read exactly like one that had nothing to do.
            return _Reverts(
                failed=tuple(targets),
                refused=tuple(sorted(refused)),
                superseded=tuple(superseded),
                base_unavailable=True,
            )
        try:
            entries = git.tree_entries(git.tree_of(base_sha))
        except Exception:
            return _Reverts(
                failed=tuple(targets),
                refused=tuple(sorted(refused)),
                superseded=tuple(superseded),
                base_unreadable=True,
            )
        done, failed = [], []
        for path in targets:
            if _revert_recorded_file(git, path, entries.get(path)):
                done.append(path)
            else:
                failed.append(path)
        return _Reverts(
            done=tuple(done),
            failed=tuple(failed),
            refused=tuple(sorted(refused)),
            superseded=tuple(superseded),
        )

    def _revert_base_sha(self, task: Task) -> str:
        """The commit a revert restores from, or "" for "no revert authority".

        Fail-closed at three points, matching `_recorded_cleanup_paths`: no
        injected authority means no revert capability at all; an authority that
        raises — a missing, unreadable or corrupt execution record — yields ""
        rather than propagating; and a value that is not a plain hex object name
        is refused here rather than handed to git. The consequence of "" is only
        that no revert can be requested this round. The consequence of guessing
        would be a file rewritten from a commit nothing authorized.
        """
        if self._revert_authority is None:
            return ""
        try:
            sha = str(self._revert_authority.base_sha(task.id) or "").strip()
        except Exception:
            return ""
        return sha if _BASE_SHA_RE.match(sha) else ""

    def _record_reverted(self, task: Task, paths: tuple[str, ...]) -> bool:
        """Write the completed reverts onto the execution record. True on
        success, and True when there was nothing to write.

        Called immediately after the restore rather than on the success path
        with `assumptions`, because that is when the fact becomes true: the
        files are on disk from that moment and a round whose validation then
        fails does NOT rewind the worker tree — the next round commits the very
        same restored content. Recording only on success would lose exactly
        those repairs, silently.

        Never raises. A record that cannot be written is reported in the round
        summary (`_revert_note`) instead, because the repair itself is real and
        a reviewer must not read the missing record as "no revert happened".
        """
        if not paths:
            return True
        if self._revert_authority is None:
            return False
        try:
            self._revert_authority.record_reverted(task.id, paths)
        except Exception:
            return False
        return True

    def _validation_commands_for(self, task: Task) -> tuple[tuple[str, ...], ...]:
        """The commands THIS round validates with.

        A task may declare its own validation. Without that the configured
        default (ruff + the autoloop and root-pipeline suites) runs for every
        task regardless of what it touched — so a change under
        `lexy-app/backend` would pass validation with nothing exercising it,
        including the test the agent just wrote. An empty `task.validation`
        keeps the configured default, which is right for tasks the default does
        cover.

        Extracted so the advisory run and the authoritative run are ONE
        computation rather than two descriptions of one. Two would eventually
        disagree, and the shape of that disagreement is the worst one available
        here: the agent proving a green run against commands the executor was
        never going to use.
        """
        return tuple(task.validation) or self._validation_commands

    @staticmethod
    def _validation_cwd_for(task: Task, git: GitGateway) -> Path:
        """Where those commands run. Pure path arithmetic — whether the
        directory EXISTS is a separate question, answered where the answer is
        acted on (`_run_implementation` returns an honest error for the
        executor's own run; `AdvisoryValidation.run` returns `NOT_RUN` text for
        the agent's). Computing it here and checking it there is what lets this
        be called before the agent runs without moving that check."""
        if task.validation_cwd:
            return git.repo_root / task.validation_cwd
        return git.repo_root

    def _advisory_for(self, task: Task, git: GitGateway) -> AdvisoryValidation:
        """The zero-argument validation call this round would offer its agent.

        Every input is bound here, from this executor's own state and this
        task's own record: the commands, the directory, the runner and the
        credentials. The returned object has no parameter through which any of
        them could be replaced — see `AdvisoryValidation`.

        Never raises, and that is not decoration. This is built on EVERY round
        now, including the agent-failure and changed-nothing paths that
        returned before `task.validation` was ever read — so a record
        `TaskRegistry.from_dict`'s coercion did not produce (`validation=None`
        on a hand-built `Task`) would turn a round that used to report its
        failure honestly into an unhandled exception out of `execute()`, which
        nothing at the orchestrator's call site catches. The fallback binds
        NOTHING to run, which `run()` reports as `NOT_RUN`. The AUTHORITATIVE
        run is deliberately left to meet the same value unguarded wherever it
        reaches it: this protects the advisory channel, it does not paper over
        a bad record.
        """
        try:
            commands = self._validation_commands_for(task)
            cwd = self._validation_cwd_for(task, git)
        except Exception:
            commands, cwd = (), git.repo_root
        return AdvisoryValidation(
            commands=commands,
            cwd=cwd,
            command_runner=self._command_runner,
            validation_env=self._validation_env,
            max_calls=self._advisory_max_calls,
        )

    def _run_implementation(
        self,
        directive: Directive,
        task: Task,
        git: GitGateway,
        agent_runner: AgentRunner,
    ) -> ExecutionOutcome:
        feedback = directive.feedback if directive.decision is Decision.REVISE else None
        # Bound BEFORE the agent runs, because the agent is who it exists for:
        # the brief has to name the channel and the watcher has to be up before
        # the first tool call can reach it.
        advisory = self._advisory_for(task, git)
        rendezvous = AdvisoryRendezvous(advisory, git.repo_root)
        offered = advisory.offerable
        # Read BEFORE the agent runs, because it is what the prompt has to
        # state, and read through the callable rather than from anything the
        # agent or this round produced — see `self._cleanup_paths_for`.
        cleanup_paths = self._recorded_cleanup_paths(task)
        # Read here too, and for the same reason: the prompt only offers the
        # revert form when one could actually be performed. Doubly fail-closed
        # — no recorded paths means no request can be authorized anyway, and no
        # base sha means there is nothing to restore FROM.
        revert_base_sha = self._revert_base_sha(task)
        spec = AgentSpec(
            domain=task.id,
            title=task.title,
            prompt=_agent_prompt(
                task,
                feedback,
                cleanup_paths,
                rendezvous.brief() if offered else "",
                bool(cleanup_paths) and bool(revert_base_sha),
            ),
        )
        try:
            if offered:
                rendezvous.start()
            result = agent_runner.run(spec)
        finally:
            # TOTAL, and around the agent call ALONE. Every reader of the tree
            # below — `_partial_work`'s `dirty_paths_all` on the agent-failure
            # branch, `_apply_recorded_cleanup`, the status read, and the
            # authoritative validation run — must see a tree with no trace of
            # the channel, because neither rendezvous path is inside any task's
            # `approved_paths` and a survivor is an out-of-scope write on the
            # record. `stop()` sweeps even when `start()` never ran, which is
            # also what clears residue left by a round that was killed.
            rendezvous.stop()
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
            # On this path too, and for the same reason a reviewer is told how
            # many lines a killed agent left: "wedged having never checked its
            # work" and "wedged after three red advisory runs" call for
            # different responses, and only the record can tell them apart.
            summary += advisory.note()
            return ExecutionOutcome(
                status="error",
                summary=summary,
                details=result.raw_text,
                validation="not run",
                changed_paths=changed,
                # The ONE branch here that can be environmental. Computed from
                # `result.stall` and `result.error` — structured signals this
                # method already holds — never from the summary text above.
                # Every other `status="error"` return in this method leaves it
                # empty, because a failed validation, an unreadable worker repo
                # and an agent that changed nothing are all the task's own
                # problem and must keep consuming the task's attempt budget.
                fault_kind=classify_agent_fault(result),
            )

        # BEFORE the status read below and BEFORE validation, both on purpose.
        # Before the status read, so the deletion is part of `changed_paths` and
        # therefore of what gets staged and committed — a cleanup-only round
        # that changed nothing else would otherwise fall into the "changed no
        # files in its worker repo" refusal with the removal sitting on disk,
        # uncommitted. Before validation, so the suite runs against the tree
        # that is actually going to be committed rather than against one that
        # still contains the file being removed.
        removed, ignored = self._apply_recorded_cleanup(git, cleanup_paths, result.raw_text)
        # SECOND, and inside the same window, for the same two reasons: the
        # restored content has to be part of `changed_paths` so it is staged and
        # committed, and validation has to grade the tree that is committed. The
        # removal running first is what makes a path named under both
        # instructions deterministic — see `_apply_recorded_reverts`.
        reverts = self._apply_recorded_reverts(
            git, cleanup_paths, revert_base_sha, result.raw_text, removed
        )
        reverts_recorded = self._record_reverted(task, reverts.done)

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
                    # On every branch below the restore, not only the success
                    # one: a revert is already ON DISK and already on the
                    # execution record by this point, and a round that failed
                    # afterwards must not report itself as having done nothing
                    # to a path the record now says it repaired.
                    + _revert_note(reverts, reverts_recorded)
                    + advisory.note()
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
                    + _revert_note(reverts, reverts_recorded)
                    + advisory.note()
                ),
                details=result.raw_text,
                validation="not run",
            )

        # The SAME two computations the advisory call was bound to — see
        # `_validation_commands_for` for why they are one function and not two
        # copies. The `is_dir()` check stays HERE, where its failure is a
        # reportable outcome of the round; moving it next to the computation
        # would move a tested error branch for no gain.
        commands = self._validation_commands_for(task)
        validation_cwd = self._validation_cwd_for(task, git)
        if task.validation_cwd and not validation_cwd.is_dir():
            return ExecutionOutcome(
                status="error",
                summary=(
                    f"task '{task.id}': declared validation_cwd "
                    f"{task.validation_cwd!r} does not exist in the worker repo"
                    + _revert_note(reverts, reverts_recorded)
                    + advisory.note()
                ),
                details=result.raw_text,
                validation="not run",
                changed_paths=tuple(sorted(changed)),
            )
        # THE AUTHORITATIVE RUN. Independent of everything above: it runs
        # unconditionally, it runs the full configured list, and it is the only
        # thing that sets `validation` and decides the status. A green advisory
        # run does not skip it, shorten it or stand in for it — the agent's runs
        # are evidence for the AGENT, and this one is evidence for the reviewer.
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
                    + _revert_note(reverts, reverts_recorded)
                    + advisory.note()
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
                + _cleanup_note(removed, ignored)
                + _revert_note(reverts, reverts_recorded)
                + advisory.note()
            ),
            details=result.raw_text,
            validation=validation_summary,
            changed_paths=tuple(changed),
            # Only on the SUCCESS path, and only because nothing else can use
            # them: every failure branch above returns before a commit exists,
            # and `orchestrator._dispatch_task_postcommit` stops at
            # `outcome.status != "ok"` without ever reaching the record these
            # accumulate onto. An assumption about work that was thrown away
            # would be carried into the next round's packet describing code
            # that is not in it.
            assumptions=_extract_assumptions(result.raw_text),
        )
