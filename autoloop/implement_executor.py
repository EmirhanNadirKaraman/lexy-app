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

**A round can also DELETE a file its own `approved_paths` already let it WRITE**
(del-01, 2026-08-25) — the case the two paragraphs above deliberately do not
reach, and the one the missing tool cost most. The prohibition never prevented
destruction: `Edit`/`Write` over an authorized path can already reduce that file
to nothing, so what the agent could not do was remove the ENTRY — destroy a file
cleanly rather than leave a committed zero-byte one (roadmap-01, 2026-08-18).
Three specs were bent around it in one night (brw-14, port-05, shrink-01,
2026-08-24/25), and a file MOVE — write the new path, delete the old — was not
expressible at all. `DELETE-FILE: <path>` is the third request form
(`_DELETE_RE`, `_apply_scoped_deletes`, `tasks.deletable_paths`) and its
authority is `Task.approved_paths`, NOT the out-of-scope record the two
instructions above select from: two authorities for two questions, never merged.

Nothing new bounds it, because the bound already existed. `tasks.deletable_paths`
gates the request on `tasks.unauthorized_paths` — the very function that records
an out-of-scope write — so an out-of-scope deletion is refused by exactly the
code that would have flagged an out-of-scope write, and an in-scope deletion goes
through `git status`/`changed_paths` and both scope comparisons like any other
change (`GitGateway.changed_paths` is `git diff-tree -r --name-only -z`, which
lists a DELETED path exactly as it lists a modified one). No tool is added:
`WRITE_ALLOWED_TOOLS` is unchanged and `Bash` is still disallowed. TRACKER PATHS
ARE REFUSED even though writing them is allowed — that grant exists so a task can
append a change note to a ledger every task shares, and it is not a licence to
remove one. Every deletion this executor performs is NAMED in the round summary
(`_scoped_delete_note`), computed from what was actually unlinked and never from
the agent's account of it, because a round that deletes a file and does not say
which has hidden the most consequential change it can make.

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

**A round that never ran the suite goes BACK to the agent once and is then
WITHHELD; an ask that was never answered says so** (advis-01, 2026-08-26;
withhold added by the 2026-08-27 revision). Two behaviours over the one contract
above, both keyed on counters this module owns.

Measured over the 147 rounds carrying an advisory line (2026-08-26), pairing
each round's self-validation outcome against the reviewer's NEXT decision: last
run PASSED, n=100, revise 41.0%; last run FAILED, n=27, revise 51.9%; NEVER
ASKED, n=18, revise 77.8%. Splitting each bucket's refusals by theme shows the
never-asked gap is not a general quality gap — `incomplete` is 14% there against
17% for PASSED — and puts the whole difference in the validation theme, 93%
against 17%. Read the limits with the number: n=14 refusals in that bucket, so
one case moves the 93% by seven points; the theming was a keyword pass, not
`reasons.py`; the relationship is correlational. What makes it actionable is the
SIGNATURE — rounds that skip the suite are refused for what the suite catches —
not the size of the gap.

So `_run_implementation` re-invokes the agent when NO ask is on the record —
`AdvisoryValidation.asked` is zero AND `AdvisoryRendezvous.ask_outstanding()` is
false, because a request the watcher has not taken yet is still an ask and is not
counted until `stop()` sweeps — and `ADVISORY_ZERO_CALL_RETURNS` bounds that at
one. The bound is the hard part: a refusal that can loop is strictly worse than
the park it replaces, so the counter is the executor's, is spent BEFORE the call,
and nothing the agent writes can raise it.

And when the allowance is spent and the record STILL shows zero, the round is
WITHHELD rather than forwarded (advis-01 revision, 2026-08-27): a report that was
never checked against the suite must not reach the reviewer as an ordinary
candidate, which is the whole finding above and would be given away by sending it
on anyway. The mechanism is the one every other refusal in this method already
uses — `status="error"` with NO `fault_kind` — so nothing is committed (
`orchestrator._dispatch_task_postcommit` returns at its `status != "ok"` branch,
well before the commit), no packet is built, and the round is charged as a TASK
attempt exactly like a failed validation or a round that changed no files. The
existing park is therefore preserved rather than replaced: this adds no park
kind, edits no orchestrator code, and reaches the attempt ceiling by the same
route those two already do. `fault_kind` is deliberately left empty — filling it
would charge the FAULT budget instead, and a refusal that does not consume the
task's own attempts is the one genuine fail-open in this design.

The check sits LATE — after the file-moving passes and the `validation_cwd`
check, immediately before the authoritative run — so that three more fundamental
refusals are still reported as themselves: a failed agent, a round that changed
no files, and a declared validation directory that does not exist. Withholding
ahead of that last one would refuse a round for not obtaining evidence the round
could not obtain.

A hand-back whose agent fails still keeps the FIRST invocation's result and
report — a torn re-invocation is not an account this round can act on, and the
round must not be reported as "implementation agent failed" when the first agent
returned cleanly. It is still withheld, because `asked` is still zero.

The second behaviour is the reporting one, and port-05 round 1 is its case: the
agent asked for run #2, never got it, and the round reported run #1's `FAILED`
as "its last run FAILED". The reviewer refused work that was never shown to be
defective, and a full round went on a reporting defect. `record_request_asked` /
`record_answer_delivered` are what the transport reports, `unanswered` is the
gap, and `note()` leads with UNANSWERED — "no answer landed" — whenever there is
one. The agent's own prose is not consulted for any of it.

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

**Every UNCOMMITTED round says what it produced, not just why it stopped**
(exec-01, 2026-08-24). A round that fails validation, times out or whose agent
errors makes no commit and no packet, so the reviewer answers from the summary
alone — and that summary named only the CAUSE. Measured cost: brw-11 wrote
2,347 insertions across four attempts, committed none, and drew four identical
`revise` directives, while an operator reading the worker diff called the task
too big in one look. `_partial_work_note` is what the reviewer reads instead:
files changed, lines written and — the part counts cannot supply — WHICH files,
on the failed-validation, missing-`validation_cwd` and agent-failed branches
alike. It is EVIDENCE: every field comes from `git status`/`git diff HEAD` in
the worker repo via `stall.WorkerTreeProbe`, and nothing in it is derived from
`result.raw_text`, which the packet renders separately and clearly labelled as
the agent's own account. The measurement is taken BEFORE the authoritative
validation run for the reason the residue trap above gives — a run that leaves
a cache directory would otherwise be counted as the agent's work — and the
success path is untouched: a round that commits reports exactly what it did
before.
"""

from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, NamedTuple, Sequence

from . import note_merge
from .audit.agents import AgentRunner, AgentSpec, ClaudeCliRunner, classify_agent_fault
from .contract import AUDIT_TASK_ID, TASK_DECISIONS, Decision, Directive
from .errors import GitError
from .executor import ExecutionOutcome
from .git_gateway import GitGateway
from .policy import PolicyEngine
from .stall import (
    DEFAULT_CEILING_SECONDS,
    PartialWork,
    ProcessGroupHandle,
    StallPolicy,
    WorkerTreeProbe,
    spawn_supervised,
)
from .state import EXECUTION_ABORTED
from .tasks import (
    TRACKER_PATHS,
    Task,
    authorized_cleanup_paths,
    deletable_paths,
    effective_approved_paths,
)
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


# ---- operator abort: killing the step in flight ------------------------------
#
# WHY THIS LIVES HERE. `pause` stops the loop at the TOP OF THE NEXT STEP, and a
# step is an agent call bounded by SILENCE rather than by elapsed time — so the
# wait is however long the current agent takes, and the loop is EASIEST to
# interrupt when idle and HARDEST when busy. Measured across one night of
# pause-and-edit jobs (2026-08-25): mean 39 minutes to land a pause, worst 60,
# one job abandoned without ever getting a boundary. `abort` is the second verb,
# for an operator who is present and waiting, and what it does is kill the
# PROCESS GROUP of whatever this module spawned.
#
# THE GROUP, NOT THE PROCESS, and in BOTH places this module spawns one:
#
#   * the write-capable agent, which spawns children of its own — that is the
#     whole reason `stall.ProcessGroupHandle` exists;
#   * the VALIDATION subprocess, which since impl-02 (2026-08-24) can be live
#     when an abort lands: the agent runs the suite mid-round through
#     `AdvisoryRendezvous`, and `pytest -n 4` is four worker processes. Those run
#     in the LOOP's process group, not the agent's, so killing only the agent
#     would leave four workers running against a worker repository nobody owns —
#     and `AdvisoryRendezvous.stop()`, which the round's `finally` always
#     reaches, would then wait up to `ADVISORY_STOP_JOIN_SECONDS` (11 minutes)
#     for that thread to finish, rebuilding the 39-minute wait under a new name.
#
# NEITHER PATH MODIFIES `stall.py`. The agent path INJECTS a spawn
# (`ClaudeCliRunner(spawn=...)`, already a seam) that wraps
# `stall.ProcessGroupHandle` with a flag check, so the kill happens inside
# `stall.supervise`'s existing poll loop and nothing about stall detection
# changes: `supervise` sees a returncode and reports `COMPLETED`, exactly as it
# does for an agent that exits on its own.

#: How often a spawned process is checked for "has the operator aborted".
#: Deliberately far finer than `stall.DEFAULT_POLL_SECONDS` for the validation
#: runner (which does its own waiting) and irrelevant for the agent (whose
#: cadence is `supervise`'s own poll). It bounds how long an abort waits, so it
#: is small; each tick is one `Path.exists()`.
ABORT_POLL_SECONDS = 0.25

#: SIGTERM -> (grace) -> SIGKILL, same shape and the same reasoning as
#: `stall.DEFAULT_TERMINATE_GRACE_SECONDS`: long enough for a CLI to flush what
#: it has already written, short enough that the kill is not itself a wait. An
#: operator pressing abort is waiting on this number.
ABORT_TERMINATE_GRACE_SECONDS = 5.0

#: Poll step inside a grace period.
_ABORT_GRACE_POLL_SECONDS = 0.25

#: The returncode reported for a command the abort killed or never started.
#: Negative like a signal exit, and distinct from any real one, so a reader can
#: tell "we killed this" from "it exited 137 by itself".
ABORT_RETURNCODE = -99

#: What a validation command that never launched says. Read by a human in
#: `state.last_validation`; it must not be mistakable for a pass.
_ABORT_NOT_STARTED = (
    "NOT STARTED — the round was aborted by the operator before this command ran"
)


def abort_flag_set(abort_file: Path | None) -> bool:
    """Has an operator asked for the step in flight to be killed?

    Takes the resolved PATH rather than a config: WHERE the flag lives is
    `state.abort_flag_file`'s single decision (outside the checkout, beside
    `PAUSE`, for the escape-detector reason recorded there), and this module is
    handed the answer by `cli._build_executor` rather than deriving a second one.
    `None` — every direct `execute()` call in the tests, and any embedder that
    does not wire it — means no abort capability at all, which is the same
    fail-closed default `cleanup_paths_for` and `revert_authority` take.

    Fail-open on an unreadable path, identically to `state.abort_requested` and
    `cli.pause_requested`: `Path.exists()` answers False rather than raising, so
    the loop keeps doing what it was already doing. Named rather than hidden —
    see `state.abort_requested` for why the other direction would be worse.
    """
    return abort_file is not None and Path(abort_file).exists()


class AbortLedger:
    """THIS round's own positive record that an abort has already ACTED here.

    **The race it closes** (abort-01 revision, 2026-08-26). The flag is a FILE,
    and `resume` — or an operator's own second command, or a wrapper script —
    deletes it. The sequence that costs a task an attempt is:

        flag appears -> the agent's process group is killed -> flag is cleared
        -> `_run_implementation` re-reads the flag, sees nothing, and reports
        the killed agent's own `not ok` as an ordinary failure

    …which charges the attempt, names a `fault_kind`, and eventually parks the
    task on `attempt_count_ceiling` blaming a wall the operator's own button
    built. Re-reading a file that a third party may delete is not a record of
    what this process DID; this is. Once something in this round has been killed
    or refused BECAUSE of the flag, the round stays aborted whatever happens to
    the file afterwards.

    **Sticky, deliberately, and in both directions.** Every abort-aware site asks
    `abort_in_effect(flag, ledger)` rather than the flag alone, so a flag cleared
    mid-kill cannot leave half a round torn down and the other half running: the
    advisory validation thread keeps refusing commands, the agent handle keeps
    killing, and `_run_implementation` keeps classifying. That also bounds
    `AdvisoryRendezvous.stop()`'s join, which is the specific way the 39-minute
    wait would otherwise be rebuilt under a new name.

    **Reset per round, unconditionally** (`_run_implementation`). `cli.
    _build_executor` constructs ONE of these for the process and shares it with
    the agent-runner factory, so a ledger that remembered a kill forever would
    classify the NEXT, perfectly healthy round as aborted — refunding an attempt
    nobody spent and shelving a task that was working. That direction is the
    dangerous one, and `test_operator_abort.py` pins it.

    **Locked**, because the two writers are genuinely concurrent: the agent
    handle is polled on the round's own thread while the advisory validation runs
    on `AdvisoryRendezvous`'s watcher thread. First writer wins, so the reason
    names what happened FIRST rather than last.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._reason = ""

    def reset(self) -> None:
        with self._lock:
            self._reason = ""

    def record(self, reason: str) -> None:
        """Note that this round killed or refused something because of the flag.

        `reason` is a full CLAUSE, not a noun — `_aborted_outcome` renders it
        into the operator's sentence verbatim, so what the round reports is what
        actually happened rather than one fixed guess about it.
        """
        with self._lock:
            if not self._reason:
                self._reason = reason

    @property
    def killed(self) -> bool:
        with self._lock:
            return bool(self._reason)

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason


def abort_in_effect(abort_file: Path | None, ledger: AbortLedger | None) -> bool:
    """Is this round being aborted — either right now, or already?

    THE flag question every abort-aware site asks, and it is two questions
    because the flag alone answers only one of them. The file says an operator is
    asking NOW; the ledger says this round has already acted on that ask and
    cannot un-act it. Either is sufficient, and the ledger is what survives the
    file being deleted (see `AbortLedger`).

    Fail-open on an unreadable flag path exactly as `abort_flag_set` is, and for
    the reason recorded there; the ledger is in-memory and cannot fail to be
    read.
    """
    return abort_flag_set(abort_file) or (ledger is not None and ledger.killed)


def _kill_group(handle, *, grace_seconds: float, sleep=time.sleep) -> int:
    """SIGTERM the process GROUP, wait out a bounded grace, then SIGKILL it.

    `handle` is a `stall.ProcessGroupHandle` (or anything with the same three
    methods): its `terminate`/`kill` are `os.killpg` calls, so a signal reaches
    every descendant the spawned process left behind — which is the entire point
    of this function and the one thing a plain `Popen.kill()` does not do.

    Bounded by ITERATIONS rather than by a clock comparison, the same rule
    `stall._stop` follows and for the same reason: a kill routine that can itself
    hang is not a kill routine.

    Always returns a non-`None` int. A caller that got `None` back would have to
    decide what an unreaped process means, and in `AbortableProcessHandle.poll`
    the answer would be "loop and re-signal it forever". After a SIGKILL to the
    group the process IS dead — `SIGKILL` cannot be caught, blocked or ignored —
    so `ABORT_RETURNCODE` reports that rather than pretending not to know.
    """
    steps = max(1, int(grace_seconds / _ABORT_GRACE_POLL_SECONDS))
    for signal_action in (handle.terminate, handle.kill):
        try:
            signal_action()
        except Exception:
            # Already reaped, or a handle that refuses the signal. Either way the
            # remaining work (observe, report) still has to happen.
            pass
        for _ in range(steps):
            try:
                returncode = handle.poll()
            except Exception:
                returncode = None
            if returncode is not None:
                return returncode
            sleep(_ABORT_GRACE_POLL_SECONDS)
    return ABORT_RETURNCODE


class AbortableProcessHandle:
    """A `stall.ProcessHandle` that also dies when the operator says so.

    Wraps the handle a real spawn produced and adds ONE behaviour: every `poll()`
    that would answer "still running" first asks whether the abort flag is set,
    and if it is, kills the whole process group and answers with the code that
    produced. `stall.supervise` calls `poll()` at the top of every one of its own
    iterations, so the kill lands within one `stall.StallPolicy.poll_seconds`
    (5s by default) plus the grace period — seconds, which is the claim.

    **It reports the kill through the ORDINARY channel.** `supervise` sees a
    returncode and returns `COMPLETED`; it does not learn that this was a kill,
    and must not — a `stall.StallReport` says "nothing changed for N seconds, so
    this agent was wedged", which would be a false statement about a healthy
    agent an operator stopped. What the round was aborted is established
    elsewhere: by the `AbortLedger` this handle writes its kill into, read by
    `_run_implementation`, and by the flag, read once more by
    `orchestrator._dispatch_task_postcommit`.

    **`aborted` is a positive record, not an inference.** It says THIS handle
    killed THIS process because of the flag, which is different from "the flag is
    set now" — the latter is also true for an agent that finished normally one
    tick earlier. The same record is written into the round-wide `AbortLedger`,
    which is what carries it past the flag being deleted; see that class.
    """

    def __init__(
        self,
        handle,
        abort_file: Path | None,
        *,
        ledger: AbortLedger | None = None,
        grace_seconds: float = ABORT_TERMINATE_GRACE_SECONDS,
        sleep=time.sleep,
    ):
        self._handle = handle
        self._abort_file = abort_file
        self._ledger = ledger
        self._grace_seconds = grace_seconds
        self._sleep = sleep
        self.aborted = False

    def poll(self) -> int | None:
        returncode = self._handle.poll()
        if returncode is not None:
            # FIRST, and the order is the same one `stall.supervise` documents: a
            # process that finished on its own is never posthumously "killed".
            return returncode
        if self.aborted or not abort_in_effect(self._abort_file, self._ledger):
            return None
        self.aborted = True
        # BEFORE the kill, not after: `_kill_group` waits out a grace period, and
        # a flag cleared during it would otherwise leave the round with a dead
        # agent and no record of why it died.
        if self._ledger is not None:
            self._ledger.record(
                "the implementation agent and every process it spawned were killed"
            )
        return _kill_group(
            self._handle, grace_seconds=self._grace_seconds, sleep=self._sleep
        )

    def terminate(self) -> None:
        self._handle.terminate()

    def kill(self) -> None:
        self._handle.kill()


def abort_aware_spawn(
    abort_file: Path | None,
    spawn=None,
    *,
    ledger: AbortLedger | None = None,
    sleep=time.sleep,
):
    """`stall.spawn_supervised`, wrapped so the operator can kill what it spawns.

    Returns a spawn callable with the signature `ClaudeCliRunner` expects, so it
    goes in through the `spawn=` seam that already exists for tests rather than
    through any change to `stall.py`. With no `abort_file` it returns the
    underlying spawn UNCHANGED — one fewer object in the ordinary path, and the
    honest representation of "no abort capability is wired here".

    `ledger` is the round-wide `AbortLedger` a kill is recorded in, or `None` for
    a construction that keeps no such record. Gated on `abort_file` alone, not on
    the ledger: a ledger with no flag path to watch has nothing to record.
    """
    inner = spawn or spawn_supervised
    if abort_file is None:
        return inner

    def _spawn(argv, *, cwd, env, stdout, stderr):
        return AbortableProcessHandle(
            inner(argv, cwd=cwd, env=env, stdout=stdout, stderr=stderr),
            abort_file,
            ledger=ledger,
            sleep=sleep,
        )

    return _spawn


def killable_run(
    argv,
    *,
    cwd=None,
    capture_output: bool = True,
    text: bool = False,
    timeout: float | None = None,
    env=None,
    abort_file: Path | None = None,
    ledger: AbortLedger | None = None,
    poll_seconds: float = ABORT_POLL_SECONDS,
    grace_seconds: float = ABORT_TERMINATE_GRACE_SECONDS,
    sleep=time.sleep,
    clock=time.monotonic,
) -> subprocess.CompletedProcess:
    """`subprocess.run`, for a command whose whole PROCESS GROUP must be killable.

    A drop-in for the `command_runner` seam `validation.run_validation_commands`
    calls (`runner(argv, cwd=, capture_output=, text=, timeout=, env=)`), with
    the same contract on the way out: a `CompletedProcess` carrying
    `returncode`/`stdout`/`stderr`, `subprocess.TimeoutExpired` on a timeout, and
    `FileNotFoundError` straight out of `Popen` for a binary that is not there.

    THREE differences from `subprocess.run`, each deliberate:

    * `start_new_session=True`, so the command and everything it spawns share a
      process group of their own. `pytest -n 4` is five processes; `subprocess
      .run`'s own timeout path kills only the one it started, leaving four
      workers writing into a worker repository nobody owns. Every kill here is
      `os.killpg` through `stall.ProcessGroupHandle`.
    * it watches `abort_file` — and the round's `ledger`, so a flag cleared
      mid-abort cannot re-arm a suite this round has already stopped — while it
      waits, and kills the group when either says so. That is what stops an abort
      from having to wait out a suite. Only THAT branch records into the ledger:
      the timeout below kills the same way for a different reason, and a suite
      that ran too long is the round's own failure, not the operator's stop.
    * output goes to temporary FILES rather than pipes. A pipe nobody drains
      fills its OS buffer and blocks the child forever, and this function cannot
      drain one while it is polling — the same reason `stall.spawn_supervised`
      uses files. `capture_output` is accepted for signature compatibility and
      ignored: output is always captured, which is a superset of what any caller
      asks for.

    The temporary files live in the system temp directory, NEVER under `cwd`:
    `cwd` is the worker repository, and a file created there mid-round is work
    the round would be judged on.
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=env,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        handle = ProcessGroupHandle(proc)
        deadline = clock() + timeout if timeout is not None else None
        timed_out = False
        while True:
            returncode = proc.poll()
            if returncode is not None:
                break
            if abort_in_effect(abort_file, ledger):
                # Recorded BEFORE the kill, and only on THIS branch: the timeout
                # branch below kills the same way for a different reason, and a
                # suite that ran too long is the round's own problem rather than
                # the operator's stop.
                if ledger is not None:
                    ledger.record(
                        "the validation subprocess and every process it spawned "
                        "were killed"
                    )
                returncode = _kill_group(
                    handle, grace_seconds=grace_seconds, sleep=sleep
                )
                break
            if deadline is not None and clock() >= deadline:
                returncode = _kill_group(
                    handle, grace_seconds=grace_seconds, sleep=sleep
                )
                timed_out = True
                break
            sleep(poll_seconds)
        out.seek(0)
        err.seek(0)
        stdout_bytes, stderr_bytes = out.read(), err.read()
    stdout = stdout_bytes.decode("utf-8", "replace") if text else stdout_bytes
    stderr = stderr_bytes.decode("utf-8", "replace") if text else stderr_bytes
    if timed_out:
        # Raised AFTER the group is dead, so the caller's `except
        # TimeoutExpired` branch never runs while workers are still writing —
        # which is what `subprocess.run` leaves behind today.
        raise subprocess.TimeoutExpired(list(argv), timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


def abort_aware_command_runner(
    command_runner, abort_file: Path | None, ledger: AbortLedger | None = None
):
    """The `command_runner` a round validates with, made abortable.

    Two layers, and the outer one is what makes a LIST of commands abortable
    rather than just the one that happens to be running: `run_validation_commands`
    executes its list in order, so a flag that appears during command 2 of 5 must
    stop 3, 4 and 5 from launching at all. The check before each command is that
    stop, and its `ABORT_RETURNCODE` reads as a failure — never as a pass — so a
    validation summary can never say green about commands nobody ran.

    The inner layer is `killable_run`, used only when no explicit runner was
    injected: an injected one is a test double or an embedder's own callable, and
    replacing it would change what the caller asked for. Such a runner still gets
    the before-each-command check, so an abort still stops the list; what it does
    not get is a group kill, because nothing here knows what it spawned.

    With no `abort_file` this returns exactly what it was given (or
    `subprocess.run`), so every unwired construction — every direct `execute()`
    test, every embedder — behaves byte for byte as it did before.
    """
    if abort_file is None:
        return command_runner or subprocess.run
    inner = command_runner or functools.partial(
        killable_run, abort_file=abort_file, ledger=ledger
    )

    def _run(argv, **kwargs):
        if abort_in_effect(abort_file, ledger):
            # A refusal is not a kill, and the ledger says so in those words: the
            # round's report is rendered from this clause verbatim, and "the
            # agent was killed" about a round whose agent had already returned
            # would be the executor overstating what it did.
            if ledger is not None:
                ledger.record(
                    "the round's remaining validation commands were refused "
                    "before launching"
                )
            return subprocess.CompletedProcess(
                list(argv), ABORT_RETURNCODE, "", _ABORT_NOT_STARTED
            )
        return inner(argv, **kwargs)

    return _run


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
    abort_file: Path | None = None,
    abort_ledger: AbortLedger | None = None,
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

    **`abort_file` is what turns the operator kill switch on**, and it composes
    with `spawn` rather than competing with it: the wrapper goes AROUND whatever
    spawn was passed (`abort_aware_spawn`), so a test that injects a fake spawn
    still gets one, wrapped. It only reaches the SUPERVISED path — without a
    `policy` there is no probe, `ClaudeCliRunner` falls back to
    `subprocess.run(..., timeout=)` and there is no handle to kill at all. That
    is the honest bound and it matches production, where `cli._build_executor`
    passes both to the per-task factory and neither to the standalone binding
    (which is never reached).

    **`abort_ledger` is the SAME object `cli._build_executor` hands the
    executor**, which is what makes a kill here visible to the round that has to
    classify it — the executor cannot reach into a runner this factory built, and
    a second ledger would remember the kill where nobody reads it. See
    `AbortLedger` for the race that record closes.
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
        spawn=abort_aware_spawn(abort_file, spawn, ledger=abort_ledger),
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

#: How many times ONE round may hand its own report BACK to the agent because
#: the executor's record shows the advisory channel was never used (advis-01,
#: 2026-08-26).
#:
#: THE BOUND IS THE HARD PART, not the resend. Measured over the 147 rounds
#: carrying an advisory line (2026-08-26): a round whose last advisory run PASSED
#: drew `revise` 41.0% of the time (n=100), one whose last run FAILED 51.9%
#: (n=27), and one that NEVER ASKED 77.8% (n=18) — and splitting those refusals
#: by theme puts the whole difference in the validation theme (93% against 17%),
#: not in a general quality gap. So a report that never ran the suite is worth
#: sending back. But a refusal that can LOOP is strictly worse than the forward
#: it replaces: it burns the whole round on re-invocations and produces nothing.
#: One is therefore the whole allowance — enough for "you forgot, go and run it",
#: and mechanically incapable of becoming a retry loop.
#:
#: The counter that spends this lives in `_run_implementation` and is incremented
#: BEFORE each re-invocation, so the loop terminates after `max_returns`
#: iterations whatever the agent does. Nothing the agent writes can raise it, and
#: `AdvisoryValidation` normalises a negative to zero exactly as it does for
#: `max_calls`, so a misconfiguration cannot read as "unbounded".
#:
#: ONE KNOB, ONE MEANING. This allowance gates BOTH halves of the contract: how
#: many times a round hands itself back, and whether a round whose record still
#: shows zero at the end is WITHHELD from review. Zero therefore means "this
#: executor does not enforce the ask at all" — no hand-back and no withhold —
#: rather than "refuse the round without ever telling the agent to run the
#: suite", which would be punishment without notice. Two things keep that from
#: being a guard that quietly switches itself off: the shipped value is 1, pinned
#: by a test, and it is NOT reachable from `config.toml` or `cli.py` — the only
#: callers that pass anything else are tests whose subject is something else
#: entirely (scope, abort, selection), which say so at the call site.
ADVISORY_ZERO_CALL_RETURNS = 1

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

    **Three states, not two** (advis-01, 2026-08-26). A request can be ASKED
    without ever being ANSWERED, and that is neither a run that passed nor a run
    that failed. The transport reports both halves — `record_request_asked` when
    it takes a request (or finds one unconsumed as the round ends) and
    `record_answer_delivered` when a `RESULT` really reaches the result file —
    so `unanswered` is the gap between them and `note()` renders it as "no answer
    landed". port-05 round 1 is the case: the agent asked for run #2, never got
    it, and the round reported its FIRST run's `FAILED` as "its last run FAILED".
    The reviewer refused work that was never shown to be defective.

    `asked` is also what decides whether the round is handed BACK to the agent
    and whether it is WITHHELD (see `ADVISORY_ZERO_CALL_RETURNS`), and it is
    deliberately not `requests`: the broken-channel branch of
    `AdvisoryRendezvous._take_request` answers the agent without ever calling
    `run()`, so a round keyed on `requests` would re-invoke an agent that asked,
    was answered, and did nothing wrong.

    It is not the WHOLE of the hand-back decision, for one reason of timing: an
    ask the watcher has not taken yet moves no counter here until
    `AdvisoryRendezvous.stop()` sweeps, which happens after the hand-back loop.
    `_run_implementation` therefore consults `ask_outstanding()` as well. The
    WITHHOLD, which is read later, needs no such companion — the sweep has
    already recorded that ask by then.
    """

    def __init__(
        self,
        commands: Sequence[Sequence[str]],
        cwd: Path,
        command_runner=None,
        validation_env: ValidationEnv | None = None,
        max_calls: int = ADVISORY_VALIDATION_MAX_CALLS,
        timeout: float = ADVISORY_VALIDATION_TIMEOUT_SECONDS,
        max_returns: int = ADVISORY_ZERO_CALL_RETURNS,
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
        self._max_returns = max(0, int(max_returns))
        #: Requests the TRANSPORT observed, which is a different question from
        #: `_requests` (runs the service was asked to perform). A request taken
        #: off the request path counts here even when nothing could be run for
        #: it, and so does one still sitting there when the round ends.
        self._asked = 0
        #: Answers that really reached the agent — a `RESULT` written to the
        #: result file, of any kind, including `NOT_RUN` ones. `PENDING` is not
        #: an answer and never counts.
        self._delivered = 0
        #: Times `_run_implementation` handed the round back to the agent for
        #: having made zero advisory requests, and how many of those returns
        #: ended in an agent failure the round then ignored.
        self._returns = 0
        self._returns_failed = 0
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

    def record_request_asked(self) -> None:
        """Record that the agent ASKED — the transport saw a request.

        Called by `AdvisoryRendezvous` at the two moments an ask is observable:
        when a request is taken off the request path, and when one is still
        sitting there as the round ends (never taken, therefore never answered).
        Both are the executor's OWN observation of the filesystem; neither reads
        a word the agent wrote about itself.

        Separate from `_requests` on purpose — see the class docstring. A
        transport that answers without running anything still moves this.
        """
        with self._lock:
            self._asked += 1

    def record_answer_delivered(self) -> None:
        """Record that an answer really reached the agent.

        An answer is a `RESULT` written to the result file, of any kind — a
        verdict, a `NOT_RUN`, or the refusal a broken channel publishes. A
        `PENDING` marker is not an answer, and a write that failed is not one
        either, which is why the transport calls this only after a successful
        `RESULT` write.
        """
        with self._lock:
            self._delivered += 1

    def record_returned_for_zero_calls(self) -> None:
        """Record one bounded hand-back of the round to the agent.

        Written by `_run_implementation`, which owns the allowance and spends it
        BEFORE each re-invocation — so a hand-back is on the record even if the
        invocation it pays for never comes back.
        """
        with self._lock:
            self._returns += 1

    def record_return_failed(self) -> None:
        """Record that a hand-back's agent did not come back cleanly.

        Deliberately separate from `record_returned_for_zero_calls`, which the
        caller has already made for this hand-back: one hand-back must count
        once. The round then keeps the EARLIER invocation's report, so this must
        never be reported as if the hand-back had replaced anything.
        """
        with self._lock:
            self._returns_failed += 1

    @property
    def exposed(self) -> bool:
        return self._exposed

    @property
    def asked(self) -> int:
        """Requests the transport observed. See `record_request_asked`."""
        return self._asked

    @property
    def delivered(self) -> int:
        """Answers that really reached the agent."""
        return self._delivered

    @property
    def unanswered(self) -> int:
        """Asks that never became an answer — `PENDING` with no `RESULT`.

        Floored at zero rather than allowed to go negative: the two counters are
        moved by a watcher thread and by `stop()`, and a report that printed
        "-1 unanswered" because of an ordering it did not anticipate would be
        less use than one that says zero. Under-reporting here is the safe
        direction only because the OVER-reporting direction would invent an
        unanswered ask, and `note()` leads with it.
        """
        return max(0, self._asked - self._delivered)

    @property
    def returns(self) -> int:
        return self._returns

    @property
    def max_returns(self) -> int:
        """The allowance `_run_implementation` spends. ONE source, read by the
        loop that bounds itself with it — not a second copy of the constant."""
        return self._max_returns

    @property
    def remaining(self) -> int:
        """Advisory runs this round could still pay for. Reported to a
        re-invoked agent, whose brief renders the CAP and would otherwise
        overstate what is left."""
        return max(0, self._max_calls - self._requests)

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

        **UNANSWERED leads when it happened** (advis-01, 2026-08-26), because it
        is the most recent fact about the channel and because the sentence it
        displaces is the one that misled a reviewer into refusing port-05 round
        1: "its last run FAILED" is a verdict about the tree as it stood before
        the ask nobody answered. Both facts are still reported — suppressing the
        completed run would be the opposite error — but in the order that makes
        the newer one impossible to miss.
        """
        if not self._exposed:
            return (
                " Agent self-validation: NOT OFFERED — no advisory validation "
                "channel was wired to the agent this round, so it ran the suite "
                "0 time(s). Read that as 'could not', not 'chose not to'."
            )
        # The transport's count wins wherever the two can disagree: a request
        # taken and answered without a run (the broken-channel branch) moves
        # `_asked` and never `_requests`, so keying on `_requests` alone would
        # report an agent that asked as one that never did.
        asked = max(self._asked, self._requests)
        unanswered = self.unanswered
        if unanswered:
            parts = [
                f" Agent self-validation: UNANSWERED — {unanswered} of the "
                f"agent's {asked} request(s) reached "
                f"`{ADVISORY_PENDING_PREFIX} #n` and never became "
                f"`{ADVISORY_RESULT_PREFIX} #n`, so no answer landed before the "
                "round ended. That is NOT a failing run and NOT a pass: nothing "
                "was proved either way, and no part of the agent's report can "
                "have acted on it."
            ]
            if self._results:
                verdict = "PASSED" if self._results[-1] else "FAILED"
                parts.append(
                    f" Earlier in the round the suite ran {self.runs} time(s) and "
                    f"the last run to COMPLETE {verdict} — a verdict about the "
                    "tree as it stood then, not about the one being reviewed."
                )
            else:
                parts.append(" The suite ran 0 time(s) this round.")
        elif not self._results and not asked:
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
                f"{asked} time(s) and the suite ran 0 time(s)."
            ]
        else:
            verdict = "PASSED" if self._results[-1] else "FAILED"
            parts = [
                f" Agent self-validation: the agent ran the suite {self.runs} "
                f"time(s); its last run {verdict}."
            ]
        if self._returns:
            parts.append(
                f" The executor handed this round back to the agent "
                f"{self._returns} time(s) (allowance {self._max_returns}) because "
                "its own record showed zero advisory requests — a report that "
                "never ran the suite is not forwarded unchecked."
            )
        if self._returns_failed:
            parts.append(
                f" {self._returns_failed} of those hand-back(s) ended in an agent "
                "failure, so the round kept the earlier invocation's report and "
                "stopped returning."
            )
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
        #: Did this rendezvous ever actually watch? Read by `stop()` alone, to
        #: decide whether a request file still sitting in the tree is THIS
        #: round's unanswered ask or residue from a round that died. `start()`
        #: sweeps before it sets this, so the two can never be confused.
        self._started = False
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
        self._started = True
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

        **The sweep is also the last chance to SEE an ask** (advis-01,
        2026-08-26). A request the watcher never took — written in the window
        between the last poll and the agent returning, or after `_stopping` was
        set — is about to be deleted, and with it the only evidence that the
        agent asked at all. Counting it here is what stops the round reporting an
        older run's verdict as "its last run" on a round whose newest ask went
        nowhere, and it is an observation of the executor's own filesystem rather
        than of anything the agent claimed. `ask_outstanding()` is that
        observation and carries the two gates it has always been made under —
        see it, rather than a second copy of the reasoning here. This is the one
        place that COUNTS what it reports; the hand-back decision reads the same
        predicate earlier and records nothing.
        """
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self._join_timeout)
        with self._lock:
            # `ask_outstanding()` takes no lock of its own, deliberately: this
            # call is already inside it and `threading.Lock` is not reentrant.
            if self.ask_outstanding():
                self._service.record_request_asked()
            self._sweep()

    def ask_outstanding(self) -> bool:
        """Is an ask sitting on the request path that nobody has taken yet?

        The SAME predicate `stop()` counts on, exposed so that the hand-back
        decision in `_run_implementation` can ask it BEFORE the sweep — an
        unconsumed request has not moved `AdvisoryValidation.asked` yet, and a
        round that read the counter alone would hand itself back to an agent
        that asked and was never answered (advis-01 revision, 2026-08-27).

        Read-only and counter-free on purpose. Recording the ask here instead
        would double-count it: the watcher's own `_take_request` records it when
        it takes the file, and `stop()` records it when it finds one left.

        Takes NO lock — `stop()` calls it from inside `self._lock` and
        `threading.Lock` is not reentrant, so acquiring here would deadlock the
        executor rather than fail a test.

        Gated twice, exactly as `stop()`'s sweep-time count always was. On
        `_started`: a request file in a rendezvous that never watched is residue
        from a round that died, not this round's ask. And on `_broken`: that
        branch already counted its ask AND answered it, so the file it could not
        remove is not a second one.
        """
        return bool(
            self._started and not self._broken and _entry_present(self.request_path)
        )

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
            if not _entry_present(path):
                return False
            self._payload = _read_bytes_or_empty(path)
            self._served += 1
            # THE ASK, recorded the moment it is observed and before anything can
            # go wrong with serving it. Every branch below this line is a way the
            # request might not produce an answer, and all of them must leave the
            # round able to say "the agent asked".
            self._service.record_request_asked()
            if not _remove_entry(path):
                # The request cannot be consumed, so serving it would re-serve
                # it on every tick until the cap absorbed the loop. Say so once
                # and stop, rather than spinning or going quiet.
                self._broken = True
                if self._write_result(
                    f"{ADVISORY_RESULT_PREFIX} #{self._served} — {NOT_RUN}: the "
                    f"executor could not consume `{ADVISORY_REQUEST_FILE}` "
                    "(something that is not a removable file is sitting at that "
                    "path), so nothing was executed and no further request can "
                    f"be made this round. {NOT_RUN} is not a pass."
                ):
                    # A `NOT_RUN` refusal IS an answer: the agent was told, and
                    # reporting the round as "no answer landed" would blame the
                    # channel for a message it delivered. What it must not do is
                    # read as a run, and it does not — nothing was executed and
                    # `_results` is untouched.
                    self._service.record_answer_delivered()
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
                # AFTER the sweep, i.e. an out-of-scope path on the record. The
                # ask therefore stays UNANSWERED, which is the honest report: the
                # agent had already returned without ever seeing this.
                return
            if self._write_result(text):
                self._service.record_answer_delivered()

    def _write_result(self, text: str) -> bool:
        """Stage, then rename. A reader must never see half an answer — the
        agent polls this file, and a torn read of a `RESULT` header with no body
        is indistinguishable from a run that reported nothing.

        Never raises: called under the lock from the watcher thread, where an
        exception would end the watcher and leave the agent polling forever.

        Returns whether the answer really landed, which is what
        `record_answer_delivered` is keyed on: a write that failed left the agent
        with nothing, and a round that counted it would report an answer nobody
        could read. `PENDING` writes deliberately ignore the return — a marker
        saying "still running" is not an answer.
        """
        tmp = self._tmp_path
        try:
            tmp.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
            os.replace(tmp, self.result_path)
            return True
        except OSError:
            _remove_entry(tmp)
            return False

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


def _entry_present(path: Path) -> bool:
    """Is something sitting at `path`? Never raises, and never follows a link.

    A symlink counts as present even when it dangles, because the question every
    caller is asking is "is there an entry here to consume or to sweep", not "is
    there readable content behind it". Fail-CLOSED on an unreadable path
    (`False`): the two callers are "take this request" and "the agent asked and
    was never answered", and inventing either from a `stat` that failed would be
    worse than missing it.
    """
    try:
        return path.is_symlink() or path.exists()
    except OSError:
        return False


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
    # Deliberately NOT `not _entry_present(path)`: that helper fails CLOSED on an
    # unreadable path (answering "nothing here"), which would read as "removed"
    # and let a request that cannot be consumed be re-served on every tick. The
    # two helpers want opposite fail directions, so they keep their own.
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


#: The line shape an IN-SCOPE DELETION is requested on — `DELETE-FILE:` first on
#: its line, optionally indented, nothing else in front of it. The same anchoring
#: discipline as the two anchors above, including the refusal of `-`/`*`/`>`
#: prefixes, so an agent summarising the instruction as a bullet is reporting,
#: not requesting.
#:
#: **Why a THIRD anchor and not a widened second one** (del-01, 2026-08-25). The
#: two above select from `TaskExecution.out_of_scope_paths`, a record the LOOP
#: writes from its own path comparisons and an agent can never add to. This one
#: selects from `Task.approved_paths`, which answers the opposite question — a
#: path the task IS allowed to touch. Merging the two would let one request form
#: choose between two different authorities, and the reviewer reading a request
#: line could no longer tell which one authorized it.
#:
#: **Echo-safety is doubly structural here.** An agent quoting the instruction
#: back emits the placeholder `<repository-relative path>`, and BOTH guards
#: refuse it independently: `tasks.deletable_paths` cannot authorize it (no
#: `approved_paths` entry can contain a `<` or a space —
#: `tasks._validate_approved_path` allowlists `[A-Za-z0-9._-]` segments — so
#: neither an exact entry nor a directory prefix can match it), and
#: `_remove_recorded_file` deletes nothing because no file with that name is on
#: disk. The second guard is the load-bearing one, because it holds whatever a
#: hand-edited `Task` record contains: this executor never deletes a path that
#: is not a real regular file inside the worker repo.
_DELETE_RE = re.compile(r"^[ \t]*delete-file:[ \t]*(.+)$", re.IGNORECASE | re.MULTILINE)


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


def _delete_instruction(
    approved: tuple[str, ...], trackers: tuple[str, ...] = TRACKER_PATHS
) -> str:
    """The IN-SCOPE DELETION section of the agent prompt, or "" when the task
    declares no approved paths at all.

    Rendered on EVERY ordinary round, unlike `_cleanup_instruction` — this is a
    capability of the task's own scope rather than an exception granted by a
    record, so there is no "occasion to use it" to wait for. It renders nothing
    for an unscoped task, which is the fail-closed branch and matches
    `_scope_instruction`'s: with no approved paths `tasks.deletable_paths`
    authorizes nothing, so describing the form would offer a capability that
    cannot fire.

    It names the trackers it refuses, LITERALLY, for the reason
    `_cleanup_instruction` lists its paths literally: a refusal an agent has to
    infer is a refusal it will run into instead. Those names come from
    `tasks.TRACKER_PATHS`, reviewed source, never from anything an agent wrote.

    Two sentences are load-bearing rather than decorative. One says the deletion
    is scope-checked exactly like a write, because "you may delete inside your
    scope" is one sentence away from "deleting is how you get out of your scope".
    The other asks the agent to NAME every deletion in its own report — the
    executor names them too, from what it actually unlinked
    (`_scoped_delete_note`), and the instruction exists so the agent's account
    and the loop's measurement can be compared rather than only one of them
    existing.
    """
    if not approved:
        return ""
    listed = "\n".join(f"  - {_scope_entry(p)}" for p in sorted(trackers))
    return (
        "DELETING A FILE — you can, inside the scope above, and only there.\n"
        "You have no Bash access and no delete tool, so this is not a command "
        "you compose: write one line per file, at the start of a line, in the "
        "exact form\n"
        "  DELETE-FILE: <repository-relative path>\n"
        "copying the path exactly — no quotes, no backticks, no leading './'. "
        "The executor unlinks the file after you finish and before validation "
        "runs, so the suite grades the tree that is actually committed.\n"
        "A deletion is SCOPE-CHECKED EXACTLY LIKE A WRITE, by the same code: a "
        "path under APPROVED SCOPE above is deleted, and a path outside it is "
        "refused and reported as refused, precisely as an edit to it would have "
        "been recorded as out of scope. Deleting is not a way out of your "
        "scope. Nothing here widens it, and this authorizes ONE unlink of one "
        "authorized file — never a directory, and never a path outside your "
        "working directory.\n"
        "These shared documentation trackers are REFUSED even though you may "
        "WRITE them:\n"
        f"{listed}\n"
        "Every task in this repository is granted those so it can APPEND its "
        "own change note; they are append-only ledgers, and a grant that exists "
        "for appending is not a licence to remove one. A request naming one of "
        "them deletes nothing and is reported as refused.\n"
        "SAY WHICH FILES YOU DELETED, in your own report, in prose. A deletion "
        "is the most consequential change you can make and the reviewer reads "
        "your report before the diff. The executor also names every file it "
        "actually removed, so this is not the record — it is your account of "
        "it, and the two are meant to be comparable.\n"
        "A MOVE is a write plus a deletion: write the new path, delete the old "
        "one, and git records it as a rename. BOTH paths must be in scope, or "
        "the half that is not will be refused and you will have made a copy "
        "rather than a move."
    )


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
        # Immediately after the list it is bounded by, and given the RAW
        # `approved_paths` rather than the effective set: the trackers the
        # effective set unions in are exactly the paths this section REFUSES,
        # and handing them to it as "your scope" would state the opposite of
        # what it goes on to say. `tasks.deletable_paths` makes the same split
        # on the same two inputs, so the prompt and the gate agree by
        # construction rather than by description.
        _delete_instruction(task.approved_paths),
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
    # Empties are dropped rather than joined, because one section is allowed to
    # render nothing: `_delete_instruction` returns "" for a task with no
    # approved paths (its fail-closed branch), and an unfiltered join would put
    # a blank paragraph between two sections instead. Every other entry is a
    # non-empty constant or a guarded append, so this changes nothing else.
    return "\n\n".join(p for p in parts if p)


def _zero_call_return_instruction(remaining: int, final: bool) -> str:
    """The section appended when a round is handed BACK to the agent for having
    never used the advisory validation channel (advis-01, 2026-08-26).

    **The dangerous failure here is not the resend, it is the REDO.** A
    re-invocation is a fresh `claude -p` with no memory of the first one
    (`ClaudeCliRunner.build_argv`), so it receives the whole original brief —
    decomposition, authoring rules and all — and the obvious reading of that
    brief is "implement this task". An agent that obeys it appends a SECOND
    change-note line, re-adds a test it already added, or rewrites a paragraph
    that is already there; every one of those either fails validation or reads to
    a reviewer as the loop having doubled the work. So this section says, before
    anything else, that the work is already on disk and must not be repeated.

    **It states what is LEFT, not what the cap is.** The brief above it renders
    `max_calls` (a constant), which on a hand-back overstates the budget whenever
    any request was already spent. `AdvisoryValidation.remaining` is the real
    number and is computed from the same counters this whole feature reports
    from.

    **Echo-safe by construction**, the same property `_advisory_instruction`,
    `_authoring_rules` and `_scope_instruction` are held to: no line begins with
    `ASSUMPTION:`, `REMOVE-OUT-OF-SCOPE:`, `REVERT-OUT-OF-SCOPE:` or
    `DELETE-FILE:`, so an agent quoting its whole prompt back forges neither a
    disclosure nor a request to delete or restore a file.
    """
    ending = (
        "This is the LAST hand-back this round gets, and it is not a formality: "
        "if the executor's record still shows zero requests when you return, the "
        "round is NOT forwarded to the reviewer at all — it is reported as a "
        "failed round, nothing is committed, and the task spends an attempt on "
        "it."
        if final
        else (
            "Further hand-backs are bounded and few; do not rely on another, and "
            "a round whose record still shows zero when they run out is not "
            "forwarded to the reviewer at all."
        )
    )
    return (
        "THIS ROUND WAS HANDED BACK TO YOU, AND THE TASK ABOVE IS ALREADY "
        "IMPLEMENTED — read this whole section before you touch a single file.\n"
        "An earlier invocation of you, in THIS SAME round, already worked the "
        "task described above, and its edits are ALREADY ON DISK in this working "
        "directory. What is missing is evidence: the executor's own record — not "
        "your report, not anyone's prose — shows that ZERO advisory validation "
        "requests were made this round, so nothing here has ever been run against "
        "the suite. That, and only that, is why you are reading this.\n"
        "DO NOT REDO THE TASK. Do not append another change note, do not re-add a "
        "test, do not rewrite a documentation paragraph, and do not start the "
        "work over: the earlier edits are still there, so every one of those "
        "lands TWICE and fails the round. Read the tree as it stands before you "
        "conclude that anything is missing.\n"
        "What to do, in order:\n"
        "  1. Ask for the validation run, exactly as the section above describes.\n"
        "  2. Poll for the stamped answer and read it.\n"
        "  3. Fix ONLY what it reports, and only inside your approved scope.\n"
        "  4. Return your report, ending with the ADVERSARIAL CASES section.\n"
        f"You have {remaining} advisory run(s) left this round. {ending} Once you "
        "have asked, the executor still runs the suite itself after you return, "
        "and that run is still the verdict.\n"
        "Your report does not replace the earlier one: both are carried to the "
        "reviewer, in order, so you need not repeat what the first already said."
    )


def _combined_report(texts: Sequence[str]) -> str:
    """Every completed invocation's report for this round, in order.

    ONE round can now produce more than one agent report (see
    `_zero_call_return_instruction`), and the later ones do not supersede the
    earlier ones: `DELETE-FILE:`, `REMOVE-OUT-OF-SCOPE:`, `REVERT-OUT-OF-SCOPE:`
    and `ASSUMPTION:` are all read out of this text, so keeping only the last
    report would silently DROP an authorized deletion or an undisclosed
    assumption the first invocation made. Concatenating is the conservative
    direction: the extractors already deduplicate, so a request repeated in both
    reports is still one request.

    Empty reports are dropped rather than joined, so a single-report round is
    byte-for-byte what it was before this existed — which is every round in which
    the agent used the channel.
    """
    kept = [text for text in texts if text]
    if len(kept) <= 1:
        return kept[0] if kept else ""
    return _REPORT_JOIN.join(kept)


#: What separates two invocations' reports in `_combined_report`. Prose, because
#: its reader is the reviewer; echo-safe, because its reader is also an agent
#: quoting the packet back.
_REPORT_JOIN = (
    "\n\n----- next agent invocation in the same round -----\n"
    "(The executor handed this round back to the agent because its record showed "
    "no advisory validation run. Each invocation's own report follows in order; "
    "none of them supersedes an earlier one.)\n\n"
)


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


def _extract_delete_requests(raw_text: str) -> tuple[str, ...]:
    """The paths an agent's output ASKED to have deleted from its OWN approved
    scope, in the order written.

    Identical rules to `_extract_cleanup_requests` — deduplicated, stripped, and
    normalised no further — for identical reasons, which is why all three go
    through `_extract_requests` rather than being written three times. A
    near-miss is FAIL-CLOSED here too: a path that is not literally inside the
    task's approved scope deletes nothing and the round reports the request as
    refused.
    """
    return _extract_requests(_DELETE_RE, raw_text)


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

    THE ONE UNLINK, shared by every authorization that can reach a deletion, so
    the guards below cannot drift into three versions of themselves: the
    `REMOVE-OUT-OF-SCOPE:` cleanup (`_apply_recorded_cleanup`, gated by
    `authorized_cleanup_paths` against the loop's own out-of-scope record), the
    created-path branch of a revert (`_revert_recorded_file`, same gate), and
    since del-01 the in-scope `DELETE-FILE:` request (`_apply_scoped_deletes`,
    gated by `tasks.deletable_paths` against `Task.approved_paths`). Each caller
    decides WHETHER a path may be deleted; this decides only whether the thing at
    that path is safely removable, and it is called only after a gate has already
    said yes — so every check here is defence in depth against a tampered record
    or a hand-edited `Task` rather than a real expectation.

    It refuses an absolute path, any `..` segment, anything whose parent does not
    resolve inside `root`, and anything that is not a regular file or a symlink —
    a directory is never removed, so no recursive delete exists on this path at
    all. That last refusal is what makes an ECHOED request harmless whatever the
    gate above it concluded: a path no file sits at is deleted by nobody.

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
    except (OSError, ValueError):
        # `ValueError` and not only `OSError`: a path holding a NUL byte raises
        # `ValueError: embedded null byte` out of the first `is_symlink()`,
        # before any syscall. Every caller reads False as "not removed", so
        # catching it keeps the malformed case fail-closed instead of raising
        # out of a round that is otherwise fine.
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


class _ScopedDeletes(NamedTuple):
    """What one round's IN-SCOPE deletion pass actually did, split by WHY.

    Six facts rather than one list, for the reason `_Reverts` gives: collapsing
    them is how a refusal comes to read like a success. The split here also
    decides which entries may be NAMED in the round summary and which may only
    be COUNTED, which is a security property rather than a presentation choice —
    that summary becomes the commit message
    (`orchestrator._dispatch_task_postcommit` builds `title\\n\\nsummary`), so an
    agent-chosen string of unbounded length must never reach it.

      * `done` — actually unlinked. NAMED (through `_bounded_paths`): the unlink
        succeeded, so every entry is a path that really was a regular file in the
        worker repo, which is the same bound `changed_paths` has. This is the
        one category the task's disclosure constraint is about.
      * `trackers` — a shared documentation ledger, refused although a WRITE to
        it is allowed. NAMED: membership is exact equality against
        `tasks.TRACKER_PATHS`, reviewed source, so the text is bounded by a set
        the repository wrote. Kept separate from `outside` because "outside your
        approved paths" is a FALSE statement about a path the task may write, and
        a reviewer chasing it would look in the wrong place.
      * `deferred` — the path is in this task's recorded out-of-scope set, which
        `REMOVE-OUT-OF-SCOPE:`/`REVERT-OUT-OF-SCOPE:` already govern. NAMED:
        bounded by the loop's own record. Nothing is done for it here, so the two
        authorities never act on one path in one round.
      * `outside` — no `approved_paths` entry covers it. COUNTED, never named.
      * `absent` — authorized, and nothing is at that path. COUNTED, never named:
        an unwritten path is a string the agent chose and no filesystem bounds it.
      * `failed` — authorized, something IS there, and it was not removed (a
        directory, or a path `_remove_recorded_file`'s guards refused, or an
        OSError). COUNTED for the same reason: `..` segments make the string
        agent-chosen again even though a real entry exists.
    """

    done: tuple[str, ...] = ()
    trackers: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()
    outside: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()


def _scoped_delete_note(deletes: _ScopedDeletes) -> str:
    """The sentence the round's summary carries about in-scope deletions, or "".

    THE DISCLOSURE del-01 requires, and it is computed from what this executor
    ACTUALLY UNLINKED — never from `result.raw_text`. An agent that writes "I
    deleted nothing" moves no entry here, and an agent that writes "I deleted
    forty files" moves none either. The prompt separately asks the agent to name
    its deletions in prose; that account rides to `report_details` and is exactly
    the thing this sentence exists to be comparable with.

    Naming versus counting follows `_cleanup_note`'s existing asymmetry and
    `_ScopedDeletes` states which category is which and why.
    """
    parts = []
    if deletes.done:
        parts.append(
            f" DELETED {len(deletes.done)} file(s) from this task's approved "
            f"paths: {_bounded_paths(deletes.done)}."
        )
    if deletes.trackers:
        parts.append(
            f" Refused to delete {len(deletes.trackers)} shared documentation "
            "tracker(s): "
            + ", ".join(deletes.trackers)
            + " — every task may APPEND a change note there, which is not a "
            "licence to remove one; nothing was deleted for them."
        )
    if deletes.outside:
        parts.append(
            f" Refused {len(deletes.outside)} deletion request(s) for path(s) "
            "outside this task's approved paths (nothing was deleted for them; "
            "the requests are in the executor report below)."
        )
    if deletes.deferred:
        parts.append(
            f" Left {len(deletes.deferred)} requested path(s) to the "
            "out-of-scope instructions, which already govern them: "
            + ", ".join(deletes.deferred)
            + "."
        )
    if deletes.absent:
        parts.append(
            f" {len(deletes.absent)} deletion request(s) named an authorized "
            "path with no file at it; nothing was deleted for them."
        )
    if deletes.failed:
        parts.append(
            f" Could NOT delete {len(deletes.failed)} authorized path(s) — a "
            "directory, or a path the unlink guards refused; nothing was "
            "deleted for them."
        )
    return "".join(parts)


#: How many partial-work paths an uncommitted round's summary NAMES before it
#: stops and says how many it left out.
#:
#: The path list is here because the counts alone cannot answer the question a
#: reviewer of a no-candidate round is actually asking. brw-11 wrote 2,347
#: insertions across four attempts and committed none of them, and that number
#: is equally consistent with "one more round finishes it" and "this task is
#: too big to land at all" — whereas three files that match the task's own plan
#: and twenty-two spread across four subsystems are not. So the discriminator
#: is WHICH files, and it is named rather than counted.
#:
#: Bounded, because the length of that list is decided by the work rather than
#: by this module, and what the bound dropped is ALWAYS stated: a silent
#: truncation reads exactly like complete coverage, which is the one way a
#: report of this kind can mislead while looking correct.
PARTIAL_WORK_MAX_PATHS = 20
#: A second bound on the same list, for the same reason, in the dimension the
#: count cannot see: twenty deeply-nested paths are far longer than twenty
#: top-level ones. At least one path is always named even if it alone exceeds
#: this — "and 34 more" with no example tells the reviewer nothing about WHERE.
PARTIAL_WORK_MAX_PATH_CHARS = 800

_PARTIAL_WORK_LABEL = "Partial work left in the worker repository"
#: Said out loud in the text, not just true of the code. The same packet also
#: renders `details = result.raw_text` — the agent's own account of itself —
#: and the whole value of this section is that it is NOT that. A reviewer can
#: only weigh the two differently if the packet says which is which.
_PARTIAL_WORK_SOURCE = "read from the worker repo's git state, not from the agent's report"


def _partial_work_note(
    changed: Sequence[str], partial: PartialWork, *, with_counts: bool = True
) -> str:
    """What an UNCOMMITTED round says about the work it actually produced, or "".

    Every branch that returns `status="error"` from `_run_implementation` after
    the agent has run produces no commit and no packet, so the reviewer sees
    only the summary. Before this existed it told them the CAUSE (the failing
    test, the agent's error) and nothing about the WORK, which is why four
    identical-looking brw-11 failures drew four identical `revise` directives.

    `with_counts=False` is the stall/ceiling case: `stall.StallReport.describe`
    has already stated the counts it measured at kill time, and printing a
    second, separately-measured pair for one fact is worse than printing none.
    It names no PATHS though, so the list is still added there — new
    information, not a re-measurement.

    The path sentence is keyed on `partial.measured`, never on `bool(changed)`.
    A worker repo that could not be read yields an empty path tuple, and
    rendering that as an empty list would report "the agent wrote nothing" when
    the truth is "we could not look" — the exact fail-open where a check
    silently passes because its input was missing.
    """
    parts: list[str] = []
    if with_counts:
        parts.append(f" {_PARTIAL_WORK_LABEL} ({_PARTIAL_WORK_SOURCE}): {partial.describe()}.")
    if changed:
        parts.append(f" Paths it touched ({_PARTIAL_WORK_SOURCE}): {_bounded_paths(changed)}.")
    elif not partial.measured or partial.files_changed:
        # Two separate git reads produce the two halves of this, so either can
        # fail alone. Both ways round, an ABSENT path list has to be said out
        # loud: silence here reads as "and it touched nothing", which is the
        # opposite of what a count of 12 with no names means.
        parts.append(
            " The list of paths it touched could not be read, so that list is "
            "UNKNOWN and is NOT a report of zero files."
        )
    return "".join(parts)


def _bounded_paths(changed: Sequence[str]) -> str:
    shown: list[str] = []
    used = 0
    for path in list(changed)[:PARTIAL_WORK_MAX_PATHS]:
        if shown and used + len(path) > PARTIAL_WORK_MAX_PATH_CHARS:
            break
        shown.append(path)
        used += len(path) + 2
    dropped = len(changed) - len(shown)
    body = ", ".join(shown)
    return f"{body}, and {dropped} more not listed" if dropped > 0 else body


def _measure_partial_work(git: GitGateway) -> PartialWork:
    """The counts, from the worker repo's own git state. Never raises.

    This runs only on paths that are already reporting a failure, and a report
    that can itself fail is not a report. An unreadable repo comes back as
    `measured=False`, which every reader must keep distinct from a measured
    zero — see `PartialWork.describe`.
    """
    try:
        return WorkerTreeProbe(git).partial_work()
    except Exception as exc:
        return PartialWork(
            measured=False, note=f"{type(exc).__name__} reading the worker repository"
        )


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
        advisory_zero_call_returns: int = ADVISORY_ZERO_CALL_RETURNS,
        abort_file: Path | None = None,
        abort_ledger: AbortLedger | None = None,
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
        # The operator kill switch, or None for "no abort capability" — the same
        # fail-closed default `cleanup_paths_for` and `revert_authority` take,
        # and the reason every direct `execute()` test is unaffected by this.
        # `cli._build_executor` binds it to `state.abort_flag_file(config)`, the
        # single decision about where that flag lives.
        self._abort_file = abort_file
        # THE ROUND'S OWN record of an abort having already acted, shared with
        # the agent runner the factory builds (`cli._build_executor` passes one
        # object to both) so a kill in either process group is visible to the one
        # place that classifies the round. Constructed here when nothing was
        # passed rather than left `None`, so every read below is unconditional:
        # an unwired executor simply owns a ledger nothing ever writes to, which
        # reads False forever and is byte-for-byte the old behaviour.
        self._abort_ledger = abort_ledger if abort_ledger is not None else AbortLedger()
        # WRAPPED, not merely stored, and it is the only line that makes an abort
        # survive a live validation run: BOTH the round's authoritative run and
        # the agent's mid-round advisory runs (`_advisory_for`) go through this
        # one attribute, so one wrap covers both. Without it an abort landing
        # during `pytest -n 4` would leave four workers writing into a worker
        # repository nobody owns, and the round's own `finally` would then wait
        # up to `ADVISORY_STOP_JOIN_SECONDS` for that thread — see
        # `abort_aware_command_runner`.
        self._command_runner = abort_aware_command_runner(
            command_runner, abort_file, self._abort_ledger
        )
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
        # How many times a round may be handed BACK to its agent for having made
        # zero advisory requests. A constructor override for the same reason
        # `advisory_max_calls` is one, and normalised inside `AdvisoryValidation`
        # so a negative reads as zero rather than as "unbounded" — the bound is
        # the whole point (see `ADVISORY_ZERO_CALL_RETURNS`).
        self._advisory_zero_call_returns = advisory_zero_call_returns

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
        return changed, _measure_partial_work(git)

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

    @staticmethod
    def _apply_scoped_deletes(
        git: GitGateway, task: Task, recorded: tuple[str, ...], raw_text: str
    ) -> _ScopedDeletes:
        """Delete the files this round asked for AND is authorized to delete.

        The claim del-01 exists to make, in one method: a round can remove a file
        whose path its own `approved_paths` already authorize it to WRITE, the
        removal shows up in `changed_paths` like any other change, and a removal
        outside those paths is refused.

        **The authority is `Task.approved_paths`, and the matcher is the loop's
        own.** `tasks.deletable_paths` runs `tasks.unauthorized_paths` over
        `effective_approved_paths(task.approved_paths)` — the same function, on
        the same list, that both of the loop's scope comparisons use to decide
        whether a WRITE was in scope. So the refusal here and the record there
        cannot drift into disagreeing about one path, and nothing is widened:
        this reads `approved_paths` and never writes it, and it grants exactly
        one unlink of one already-authorized file.

        **A tracker path is refused although a write to it is allowed** — see
        `tasks.deletable_paths` for why the append grant is not a delete grant.
        It is reported as its own category rather than as "outside your scope",
        which for a tracker would be false.

        **A path in `recorded` is LEFT ALONE and reported**, so the two
        authorities never both act on one path in one round. `recorded` is
        `TaskExecution.out_of_scope_paths`, which `REMOVE-OUT-OF-SCOPE:` and
        `REVERT-OUT-OF-SCOPE:` already govern; the sets are disjoint in the
        ordinary case (a path recorded out of scope was by construction NOT in
        `approved_paths` when it was recorded) and this branch is what keeps them
        disjoint after an operator widens a task's scope between rounds. Without
        it, a path named under `DELETE-FILE:` and `REVERT-OUT-OF-SCOPE:` in one
        report would be deleted and then restored, and both notes would be wrong
        about the end state.

        **Nothing is deleted that is not a real file.** Every authorized path is
        probed first and handed to `_remove_recorded_file`, whose guards refuse an
        absolute path, a `..` segment, a parent outside the worker repo and a
        directory. That is the guard that makes an echoed instruction inert
        whatever the gate concluded, and it is why `absent` is a category rather
        than an error.
        """
        requested = _extract_delete_requests(raw_text)
        if not requested:
            return _ScopedDeletes()
        authorized, outside, trackers = deletable_paths(requested, task.approved_paths)
        governed = set(recorded)
        deferred = sorted(p for p in authorized if p in governed)
        root = git.repo_root
        done: list[str] = []
        absent: list[str] = []
        failed: list[str] = []
        for rel in sorted(p for p in authorized if p not in governed):
            target = root / rel
            try:
                present = target.is_symlink() or target.exists()
            except (OSError, ValueError):
                present = False
            if not present:
                # The end state the request asked for already holds. Reported as
                # its own category and never as a failure — but reported, because
                # an agent asking to delete a path that is not there is usually
                # an agent that mistyped one that is.
                absent.append(rel)
            elif _remove_recorded_file(root, rel):
                done.append(rel)
            else:
                failed.append(rel)
        return _ScopedDeletes(
            done=tuple(done),
            trackers=tuple(sorted(trackers)),
            deferred=tuple(deferred),
            outside=tuple(sorted(outside)),
            absent=tuple(absent),
            failed=tuple(failed),
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
            max_returns=self._advisory_zero_call_returns,
        )

    def _aborted_outcome(
        self,
        task: Task,
        git: GitGateway,
        *,
        raw_text: str = "",
        note: str = "",
        measured: tuple[tuple[str, ...], PartialWork] | None = None,
        validation: str = "",
    ) -> ExecutionOutcome:
        """What a round killed by `abort` reports.

        REUSES `_partial_work` / `_partial_work_note` rather than inventing a
        second sentence, because the question a reviewer (and the operator) asks
        after an abort is exactly the one those already answer: an uncommitted
        round produced nothing a packet can show, so the only evidence of what it
        did is the worker repository's own git state. Same measurement, same
        wording, same provenance — read from `git status` / `git diff HEAD`,
        never from anything the agent said about itself.

        `status` is `EXECUTION_ABORTED`, not `"error"`: the orchestrator treats
        it as the second, independent signal that this round was aborted, so a
        flag cleared between here and there still ends the round as an abort.
        `fault_kind` is deliberately EMPTY — an abort is neither the task's
        failure nor the environment's, and naming a fault here would spend the
        fault budget this is required not to spend.

        **What it says was killed is read from the ledger, not assumed.** Three
        different things can end a round here — the agent's process group, the
        validation subprocess group, or a list of commands refused before it
        launched — and the round that was stopped BEFORE it spawned anything
        killed nothing at all. `AbortLedger.reason` is the clause whichever site
        acted wrote for itself; the fallback covers the flag-only case honestly
        rather than claiming a kill nobody performed.

        **`measured` exists because this is also called AFTER validation ran**
        (abort-01 revision, 2026-08-26). `_partial_work` reads the tree at the
        moment it is called, and the authoritative run can itself write into the
        worker repo — a `ruff` cache directory that `git status -uall` then
        reports. Re-measuring at the post-validation site would therefore fold
        validation's own residue into the agent's count and hand the operator a
        file list containing paths no agent wrote. The caller there passes the
        measurement taken BEFORE the suite launched, which is the same pair
        every other branch at that depth reports from. `None` means "measure
        now", which is what the two pre-validation sites want and what they got
        before this parameter existed.

        **`validation` exists because "did not run" stops being true there.** A
        suite that was launched and killed mid-flight, and a list of commands
        refused before launching, are different facts, and `run_validation_
        commands` has already written the one that happened: a `PASS`/`FAIL`/
        `NOT RUN` line per command, naming which ones got as far as executing.
        Empty means no authoritative run was reached at all — the honest report
        for every pre-validation site — and anything else is that run's own
        summary, passed through rather than re-narrated here.

        The sentence around it is keyed on the LEDGER, not on the flag, because
        the flag can also land after the suite has finished. A run this round
        killed or refused was cut short; a run that had already completed says
        so and keeps its verdict. Claiming the abort cut short a suite that
        finished would overstate what the operator's button did, and a suite
        that went red on the task's own merits is evidence the next round needs.
        """
        changed, partial = self._partial_work(git) if measured is None else measured
        acted = self._abort_ledger.reason or (
            "the round was stopped before it could finish"
        )
        if not validation:
            # No authoritative run was reached at all — every pre-validation
            # site, and the honest report for them.
            account = " Validation did not run."
        elif self._abort_ledger.killed:
            account = f" Validation was cut short by the abort: {validation}"
        else:
            # Reached on the flag ALONE, so nothing here killed or refused a
            # command: the suite had already finished when the operator's flag
            # landed, and calling that "cut short" would be this report
            # overstating what the abort actually did. Its verdict still rides
            # along — a run that went red on the task's own merits before
            # anybody pressed anything is evidence the next round needs, and
            # discarding it because the round ended as an abort would lose it.
            account = (
                f" Validation had already finished when the abort landed: {validation}"
            )
        summary = (
            f"task '{task.id}': the round was ABORTED by the operator — "
            f"{acted}, nothing was committed, and the task goes back to the "
            "queue with this work intact."
            + _partial_work_note(changed, partial)
            + account
            + note
        )
        return ExecutionOutcome(
            status=EXECUTION_ABORTED,
            summary=summary,
            details=raw_text,
            validation=validation or "not run (aborted)",
            changed_paths=changed,
        )

    def _run_implementation(
        self,
        directive: Directive,
        task: Task,
        git: GitGateway,
        agent_runner: AgentRunner,
    ) -> ExecutionOutcome:
        # FIRST, and unconditionally. The ledger is per ROUND while the executor
        # (and in production the object itself) is per PROCESS, so a kill this
        # method recorded an hour ago must not classify the round starting now.
        # That is the dangerous direction — it would refund an attempt nobody
        # spent and shelve a task that was working — and it is pinned by
        # `test_operator_abort.py`, not merely intended here.
        self._abort_ledger.reset()
        if abort_flag_set(self._abort_file):
            # BEFORE anything is spawned. The orchestrator's own check at the top
            # of each step normally catches this first, so reaching here means
            # the flag landed in the window between that check and this call —
            # narrow, but the answer for it is the cheap one: run no agent at
            # all. The round is still refunded and still returned to the queue,
            # because the orchestrator decides that from the flag, not from
            # whether an agent happened to start.
            return self._aborted_outcome(task, git)
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
        base_prompt = _agent_prompt(
            task,
            feedback,
            cleanup_paths,
            rendezvous.brief() if offered else "",
            bool(cleanup_paths) and bool(revert_base_sha),
        )
        spec = AgentSpec(domain=task.id, title=task.title, prompt=base_prompt)
        reports: list[str] = []
        try:
            if offered:
                rendezvous.start()
            result = agent_runner.run(spec)
            reports.append(result.raw_text)
            # A REPORT THAT NEVER RAN THE SUITE GOES BACK TO THE AGENT (advis-01,
            # 2026-08-26), because that specific round is refused for that
            # specific reason: of the 147 rounds carrying an advisory line
            # (measured 2026-08-26), the never-asked bucket drew `revise` 77.8%
            # of the time against 41.0% for a round whose last run passed — and
            # 93% of those refusals were on the validation theme, against 17% in
            # the passed bucket. It is not a general quality gap; it is the suite
            # catching what the suite catches.
            #
            # FIVE CONDITIONS, and each one is load-bearing:
            #
            #   * `offered` — a round whose channel could never run anything
            #     (`AdvisoryValidation.offerable`) has nothing to send the agent
            #     back FOR, and re-invoking it would spend a whole agent call to
            #     collect a second `NOT RUN`.
            #   * `result.ok` — a failed agent is already reported as a failure
            #     with its partial work measured; handing the round back would
            #     replace an honest failure with a second one.
            #   * `not rendezvous.ask_outstanding()` — an ask the watcher has not
            #     taken YET is still an ask, and this is the only place that can
            #     see it: `record_request_asked` fires when the request is taken
            #     or, failing that, when `stop()` finds it in the tree, and
            #     `stop()` does not run until the `finally` below. Without this
            #     the round hands itself back to an agent that asked and was
            #     never answered — port-05's own round, and the one behaviour 2
            #     exists to stop misreporting (advis-01 revision, 2026-08-27).
            #   * `advisory.asked == 0` — the TRANSPORT's count, not `requests`.
            #     The broken-channel branch answers the agent without ever
            #     calling `run()`, so keying on `requests` would re-invoke an
            #     agent that asked, was told the channel was broken, and did
            #     nothing wrong.
            #   * the abort check — an operator who pressed the button is not
            #     waiting for one more agent invocation.
            #
            # THE TWO ASK CHECKS ARE IN THIS ORDER, and the order is the whole of
            # the race argument. `_take_request` calls `record_request_asked()`
            # BEFORE `_remove_entry()` (see those two adjacent lines), so a
            # request file that is absent when `ask_outstanding()` looks was
            # already counted before it looked, and the `asked` read that follows
            # cannot miss it. Reading the counter first would leave a window in
            # which the file has just been consumed, the counter has not been
            # re-read, and a round that asked is handed back anyway. If those two
            # lines ever swap, this gate fails OPEN and nothing else here says so.
            #
            # THE BOUND IS THE POINT. `returns` is this executor's own counter,
            # incremented BEFORE each re-invocation and never derived from
            # anything the agent produced, so the loop ends after `max_returns`
            # iterations however the agent behaves. A refusal that could loop
            # would be strictly worse than the park it replaces — it would burn
            # the whole round on re-invocations and produce nothing. When the
            # allowance IS spent and the record still shows zero, the round does
            # not proceed as an ordinary candidate: see the withhold below.
            returns = 0
            while (
                offered
                and result.ok
                and not rendezvous.ask_outstanding()
                and advisory.asked == 0
                and returns < advisory.max_returns
                and not abort_in_effect(self._abort_file, self._abort_ledger)
            ):
                returns += 1
                final = returns >= advisory.max_returns
                advisory.record_returned_for_zero_calls()
                again = agent_runner.run(
                    AgentSpec(
                        domain=task.id,
                        title=task.title,
                        prompt=(
                            base_prompt
                            + "\n\n"
                            + _zero_call_return_instruction(advisory.remaining, final)
                        ),
                    )
                )
                if not again.ok:
                    # NEVER worse than not having asked. The first invocation's
                    # result is already good and its work is already on disk, so
                    # a failed hand-back keeps that result and stops: converting
                    # a round that would have been reviewed into an
                    # "implementation agent failed" would be this feature causing
                    # the loss it exists to prevent. The failed invocation's own
                    # text is deliberately NOT carried — a report from a run that
                    # did not complete is not an account this round can act on,
                    # and it is the input to the `DELETE-FILE:` extractor.
                    advisory.record_return_failed()
                    break
                reports.append(again.raw_text)
                result = again
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
        # EVERY completed invocation's report, in order, and the only text read
        # from here down. A round that was never handed back has exactly one, so
        # this is byte-for-byte `result.raw_text` for it; a round that was handed
        # back must not lose the first invocation's `DELETE-FILE:`,
        # `REMOVE-OUT-OF-SCOPE:` or `ASSUMPTION:` lines just because a later
        # invocation did not repeat them. See `_combined_report`.
        raw_text = _combined_report(reports)
        if abort_in_effect(self._abort_file, self._abort_ledger):
            # THE LEDGER, not only the flag, and that is the whole of the fix for
            # the abort-then-resume race (abort-01 revision, 2026-08-26): the
            # flag is a file, `resume` deletes it, and a flag cleared between the
            # kill and this line would have this round report its killed agent's
            # `not ok` as an ordinary failure — charging the attempt, naming a
            # fault, and building an `attempt_count_ceiling` out of the
            # operator's own button. `AbortLedger` is what this process DID, so
            # nothing a third party does to the file can un-say it.
            #
            # BEFORE `result.ok`, because the killed agent's own failure is a
            # CONSEQUENCE of the abort and reporting it as the cause would tell a
            # reviewer that a healthy agent had wedged. Before the cleanup,
            # delete and revert passes too, and before the authoritative
            # validation run: those are work this round is no longer doing, and
            # starting a suite after the operator asked the loop to stop is the
            # wait this whole verb exists to remove.
            #
            # The rendezvous has already been swept by the `finally` above, so
            # the tree `_partial_work` measures below carries no trace of the
            # channel — the same guarantee every other reader of it gets.
            return self._aborted_outcome(
                task, git, raw_text=raw_text, note=advisory.note()
            )
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
            # A stall report already states the COUNTS it measured at kill time;
            # repeating them here would print two separately-measured numbers
            # for one fact. Every OTHER failure — a provider error, a crash, the
            # `subprocess.TimeoutExpired` path in `ClaudeCliRunner.run`, which
            # carries no `stall` — has said nothing about them at all. The PATH
            # list is added on both, because no stall report has ever carried
            # one: it is new information, not a second measurement.
            summary += _partial_work_note(changed, partial, with_counts=result.stall is None)
            if result.stall is None:
                summary += " Validation did not run."
            # On this path too, and for the same reason a reviewer is told how
            # many lines a killed agent left: "wedged having never checked its
            # work" and "wedged after three red advisory runs" call for
            # different responses, and only the record can tell them apart.
            summary += advisory.note()
            return ExecutionOutcome(
                status="error",
                summary=summary,
                details=raw_text,
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

        # ALL THREE file-moving passes run HERE: after the agent, BEFORE the
        # status read below and BEFORE validation, all on purpose. Before the
        # status read, so what they did is part of `changed_paths` and therefore
        # of what gets staged and committed — a round whose only work is a
        # removal or a restore would otherwise fall into the "changed no files in
        # its worker repo" refusal with the change sitting on disk, uncommitted.
        # Before validation, so the suite runs against the tree that is actually
        # going to be committed rather than against one that still contains the
        # file being removed.
        #
        # FIRST, the in-scope deletion (del-01). Its order relative to the other
        # two decides nothing, deliberately: `_apply_scoped_deletes` leaves every
        # path in `cleanup_paths` to them and reports it, so no path is ever
        # touched by two authorities in one round and neither existing pass had
        # to learn about this one.
        deletes = self._apply_scoped_deletes(git, task, cleanup_paths, raw_text)
        # SECOND, the recorded out-of-scope removal (scope-04).
        removed, ignored = self._apply_recorded_cleanup(git, cleanup_paths, raw_text)
        # THIRD, the recorded out-of-scope restore (scope-05). The removal pass
        # above running first is what makes a path named under BOTH out-of-scope
        # instructions deterministic — see `_apply_recorded_reverts`.
        reverts = self._apply_recorded_reverts(
            git, cleanup_paths, revert_base_sha, raw_text, removed
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
                    + _scoped_delete_note(deletes)
                    + _revert_note(reverts, reverts_recorded)
                    + advisory.note()
                ),
                details=raw_text,
                validation="not run",
            )
        if not changed:
            return ExecutionOutcome(
                status="error",
                summary=(
                    f"task '{task.id}': the implementation agent ran but changed "
                    "no files in its worker repo — nothing to review"
                    + _scoped_delete_note(deletes)
                    + _revert_note(reverts, reverts_recorded)
                    + advisory.note()
                ),
                details=raw_text,
                validation="not run",
            )

        # ONE measurement of what this round produced, taken HERE: after the
        # `changed` read above, so the two describe the same tree, and — this is
        # the part that has to stay true — BEFORE validation runs. The
        # authoritative run can itself write into the worker repo (a `ruff`
        # cache directory that `git status -uall` would then report; see this
        # module's docstring on the residue trap), so a count taken after it
        # would fold validation's own writes into the agent's and disagree with
        # the `changed_paths` the same outcome carries. Both no-candidate
        # branches below report from this read and no other.
        #
        # Deliberately NOT taken on the two branches above it. Reaching `not
        # changed` means the status read SUCCEEDED and returned nothing, which
        # the sentence there already states; and on the `GitError` branch the
        # measurement would fail for the same reason the read did, so its own
        # message is the honest report. In both cases a `PartialWork` line would
        # restate what is already said rather than add evidence.
        partial = _measure_partial_work(git)

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
                    # A no-candidate round like any other: nothing is committed
                    # from here, so the work the agent did leaves no trace the
                    # reviewer can read except this.
                    + _partial_work_note(changed, partial)
                    + _scoped_delete_note(deletes)
                    + _revert_note(reverts, reverts_recorded)
                    + advisory.note()
                ),
                details=raw_text,
                validation="not run",
                changed_paths=tuple(sorted(changed)),
            )
        if offered and advisory.max_returns > 0 and advisory.asked == 0:
            # THE ROUND IS WITHHELD (advis-01 revision, 2026-08-27). The agent has
            # been handed this round back and the executor's own record STILL
            # shows zero advisory requests, so the report in hand was never
            # checked against the suite by the agent that wrote it — and that is
            # the exact round the measurement says the reviewer refuses, 77.8%
            # against 41.0%, with 93% of the refusals on the validation theme.
            # Forwarding it anyway would give away the whole finding.
            #
            # THE MECHANISM IS THE ONE ALREADY HERE, deliberately: `status=
            # "error"` with NO `fault_kind`, exactly like the failed-validation
            # and changed-nothing branches. `orchestrator._dispatch_task_
            # postcommit` returns at its `status != "ok"` test, before the commit
            # and before any packet, so the candidate cannot reach review; and an
            # empty `fault_kind` charges the round to the TASK's own attempt
            # budget, which is what makes a repeat bounded ACROSS rounds by the
            # attempt ceiling that already exists. Naming a fault instead would
            # spend the fault budget and let a stubborn task refuse forever. No
            # park kind is added and no orchestrator code changes: the existing
            # park is reached by the existing route.
            #
            # WHY HERE, AND NOT THE MOMENT THE AGENT RETURNED. Three refusals
            # above this line are about something MORE fundamental than missing
            # evidence, and each would be swallowed by an earlier withhold: an
            # agent that failed, a round that changed no files, and a declared
            # `validation_cwd` that does not exist. The last one is the sharpest —
            # an advisory run there could only ever have answered `NOT RUN`
            # naming that directory, so refusing the round for not obtaining
            # evidence it could not obtain would blame the wrong party, and the
            # reviewer would be told "it never ran the suite" about a round whose
            # real problem is its own configuration. What this placement DOES
            # short-circuit is the expensive part: the authoritative run below,
            # minutes of it, for a candidate that is not going to exist.
            #
            # The file-moving passes above have already run, exactly as they have
            # for the failed-validation branch since they existed, so their notes
            # are threaded below: a round that unlinked a file must say so
            # whether or not it is being forwarded.
            #
            # THREE CONDITIONS, and the middle one is not a rubber stamp:
            #
            #   * `offered` — a channel the agent could not reach is NOT OFFERED,
            #     and refusing a round for not using a call it never had would be
            #     the fail-closed direction taken against the wrong party.
            #   * `advisory.max_returns > 0` — the allowance gates both halves
            #     (see `ADVISORY_ZERO_CALL_RETURNS`). Withholding a round that
            #     was never handed back would be punishment without notice, and
            #     with an allowance configured the loop above has normally spent
            #     one by the time this is reached. NORMALLY, not always: an abort
            #     flag that appears before the loop and is cleared before the
            #     check below skips the hand-back without ending the round. So
            #     the sentence reports `advisory.returns`, the number actually on
            #     the record, rather than asserting the allowance was spent —
            #     a report that states a hand-back nobody made is the same class
            #     of defect as the stale verdict behaviour 2 removes.
            #   * `advisory.asked == 0` — the TRANSPORT's count. A round whose ask
            #     went UNANSWERED has `asked >= 1` and is NOT withheld: that round
            #     is port-05's, the one behaviour 2 exists to stop misreporting,
            #     and withholding it would punish the agent for the channel's own
            #     failure. Keying this on "no run completed" instead would do
            #     precisely that. This needs no companion to
            #     `ask_outstanding()`, unlike the hand-back loop above: `stop()`
            #     has already run in that method's `finally`, so an ask the
            #     watcher never took is on the counter by the time this is read,
            #     and the request file itself is gone.
            #
            # `result.ok` is already true here, so a hand-back whose own agent
            # failed still lands here on the FIRST invocation's good result —
            # withheld for the zero, never reported as "implementation agent
            # failed".
            return ExecutionOutcome(
                status="error",
                summary=(
                    f"task '{task.id}': WITHHELD from review — the executor's own "
                    "record shows ZERO advisory validation requests this round "
                    f"(handed back {advisory.returns} time(s) of an allowance of "
                    f"{advisory.max_returns}), so nothing here was ever run against "
                    "the suite by the agent that wrote it. That is NOT a failing "
                    "suite and NOT a pass: nothing was executed, so nothing is "
                    "known either way. Nothing is committed and no candidate is "
                    "produced; the work is still in the worker repo for the next "
                    "round."
                    + _partial_work_note(changed, partial)
                    + _scoped_delete_note(deletes)
                    + _revert_note(reverts, reverts_recorded)
                    + advisory.note()
                ),
                details=raw_text,
                validation="not run",
                changed_paths=tuple(changed),
            )

        # THE AUTHORITATIVE RUN. Independent of every advisory RESULT above it:
        # it runs the full configured list and is the only thing that sets
        # `validation` and decides the status. A green advisory run does not skip
        # it, shorten it or stand in for it — the agent's runs are evidence for
        # the AGENT, and this one is evidence for the reviewer. The only things
        # that can keep it from running are the branches above that already
        # decided this round produces NO candidate (a failed agent, an unreadable
        # repo, no files changed, a missing validation directory, and — since the
        # advis-01 revision — a withheld round); none of them is an advisory
        # verdict, and none of them lets the round be reviewed.
        passed, validation_summary = run_validation_commands(
            commands,
            validation_cwd,
            command_runner=self._command_runner,
            validation_env=self._validation_env,
        )
        if abort_in_effect(self._abort_file, self._abort_ledger):
            # THE SECOND HALF OF THE ABORT-THEN-RESUME RACE (abort-01 revision,
            # 2026-08-26), and the one the earlier round left open. The check at
            # the top of this method covers an abort that lands while the AGENT
            # runs; nothing covered an abort that lands while the AUTHORITATIVE
            # SUITE runs — which is a window of minutes, and the longest one left
            # in a round now that the agent itself is killable.
            #
            # The sequence: the flag appears mid-suite -> `killable_run` kills
            # the validation process group and records the ledger -> `resume`
            # removes the flag -> this line. Falling through to `not passed`
            # below would report the killed suite's own `rc=-99` as "validation
            # failed after implementation", which is a `status="error"` round:
            # the orchestrator's `abort_requested` read at
            # `_dispatch_task_postcommit` finds no flag either, so the task is
            # charged an attempt for a suite the operator stopped, and enough of
            # those build the `attempt_count_ceiling` this task exists to
            # prevent. The ledger is what this process DID, so it survives the
            # file being deleted.
            #
            # BEFORE `not passed`, for the same reason the agent check sits ahead
            # of `result.ok`: the failure is a CONSEQUENCE of the kill, and
            # naming it as the cause would tell a reviewer that the task's own
            # tests were red.
            #
            # `measured` and `validation` are what keep the report honest at THIS
            # depth, and neither is optional here. The counts are the pair taken
            # before the suite launched — re-measuring now would fold validation's
            # own residue into the agent's work — and the summary is the real
            # per-command `PASS`/`FAIL`/`NOT RUN` account, because a suite that
            # was launched and killed is not a suite that "did not run". The
            # delete and revert notes are threaded for the reason every other
            # branch below the restore threads them: those passes have ALREADY
            # run by this line, and telling an operator their work is "intact"
            # while silently omitting a file this round unlinked is exactly the
            # disclosure del-01 forbids skipping.
            return self._aborted_outcome(
                task,
                git,
                raw_text=raw_text,
                note=(
                    _scoped_delete_note(deletes)
                    + _revert_note(reverts, reverts_recorded)
                    + advisory.note()
                ),
                measured=(tuple(changed), partial),
                validation=validation_summary,
            )
        if not passed:
            return ExecutionOutcome(
                status="error",
                summary=(
                    f"task '{task.id}': validation failed after implementation — "
                    f"{validation_summary}"
                    # THE measured gap (exec-01). This branch already named the
                    # CAUSE — `validation_summary` carries the failing command
                    # and test — and said nothing whatever about the WORK, so
                    # four brw-11 rounds that wrote 2,347 uncommitted insertions
                    # between them were indistinguishable from four that wrote
                    # nothing, and drew the same `revise` four times.
                    + _partial_work_note(changed, partial)
                    + _scoped_delete_note(deletes)
                    + _revert_note(reverts, reverts_recorded)
                    + advisory.note()
                ),
                details=raw_text,
                validation=validation_summary,
                changed_paths=tuple(changed),
            )

        return ExecutionOutcome(
            status="ok",
            summary=(
                f"task '{task.id}' implemented: {len(changed)} file(s) changed; "
                "validation passed."
                + _cleanup_note(removed, ignored)
                # NEVER omitted on the success path, which is the disclosure
                # del-01 requires: this is the round that produces a candidate,
                # and a deletion the reviewer is not told about is the most
                # consequential change a round can hide. Computed from what was
                # actually unlinked, never from `result.raw_text`.
                + _scoped_delete_note(deletes)
                + _revert_note(reverts, reverts_recorded)
                + advisory.note()
            ),
            details=raw_text,
            validation=validation_summary,
            changed_paths=tuple(changed),
            # Only on the SUCCESS path, and only because nothing else can use
            # them: every failure branch above returns before a commit exists,
            # and `orchestrator._dispatch_task_postcommit` stops at
            # `outcome.status != "ok"` without ever reaching the record these
            # accumulate onto. An assumption about work that was thrown away
            # would be carried into the next round's packet describing code
            # that is not in it.
            assumptions=_extract_assumptions(raw_text),
        )
