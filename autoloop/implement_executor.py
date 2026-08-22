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
`approved_paths` and `allowed_paths` are neither read nor written here, so an
EDIT to the same path is as unauthorized as it ever was.

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

import re
import subprocess
from pathlib import Path
from typing import Callable

from .audit.agents import AgentRunner, AgentSpec, ClaudeCliRunner, classify_agent_fault
from .contract import AUDIT_TASK_ID, TASK_DECISIONS, Decision, Directive
from .errors import GitError
from .executor import ExecutionOutcome
from .git_gateway import GitGateway
from .policy import PolicyEngine
from .stall import DEFAULT_CEILING_SECONDS, PartialWork, StallPolicy, WorkerTreeProbe
from .tasks import Task, authorized_cleanup_paths
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
#: instruction is pinned to the claim THIS task states and to the files its
#: description and decomposition already cover, and anything outside that is
#: reported rather than fixed. Note what it does NOT do: it reads no scope field
#: — `Task.approved_paths` and `TaskExecution.allowed_paths` are still neither
#: read nor written anywhere in this module — grants no tool, and relaxes no
#: ground rule. It adds an instruction and nothing else.
#:
#: **Evidence, not reassurance.** "I checked and it is fine" is unfalsifiable
#: and costs a reviewer the same read as no answer at all, so the instruction
#: asks for the cases considered AND where each is handled. A reviewer can check
#: that artifact against the diff instead of deriving the case list from
#: scratch.
#:
#: **The enumeration is DELIBERATELY NOT PARSED.** No regex, no
#: `ExecutionOutcome` field, no packet section — unlike `_ASSUMPTION_RE` and
#: `_CLEANUP_RE`, which exist because something COMPUTES with what they match (a
#: durable cross-round record; an actual unlink). Nothing computes with this: it
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
#: One coupling to know before editing the text: it refers to the "approved
#: decomposition" in LOWER CASE, and two tests assert that a task without a plan
#: gets no `"Approved decomposition"` heading. Only capitalisation separates the
#: reference from the heading, so capitalising it here turns those tests red —
#: which is the right outcome (the assertion is doing its job), but the reason
#: would be obscure without this note.
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
    "Fix only the failures inside the files this task's description and "
    "approved decomposition already cover. Anything outside them you REPORT "
    "rather than fix. This is bounded to the claim you were given — it is not "
    "permission to improve the code, widen the task, or write a path the task "
    "does not name.\n"
    "Then show your work: \"I checked and it is fine\" is unfalsifiable and "
    "the reviewer cannot verify it. End your report with a section headed\n"
    "  ADVERSARIAL CASES:\n"
    "and under it one line per case, naming the case AND where it is handled — "
    "the function that handles it, the test that pins it, or why it cannot "
    "arise. List the cases you considered and dismissed too; the reviewer is "
    "checking your reasoning, not only your diff."
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


def _cleanup_instruction(paths: tuple[str, ...]) -> str:
    """The cleanup section of the agent prompt, or "" when there is nothing to
    clean up.

    Rendered ONLY when the loop has actually recorded out-of-scope residue for
    this execution, so the ordinary round's prompt is unchanged and no agent is
    told about a capability it has no occasion to use. The paths are listed
    literally because the match is exact: an agent that retypes a path
    approximately gets a refusal, not a near-miss deletion.

    The wording is doing one specific job beyond describing the mechanism — it
    has to stop the capability being read as scope. "You may remove this file"
    is one sentence away from "this file is mine to edit", and the second
    reading is the one that would quietly undo the never-widened
    `allowed_paths` rule this whole mechanism is built to preserve.
    """
    if not paths:
        return ""
    listed = "\n".join(f"  {p}" for p in paths)
    return (
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
    task: Task, feedback: str | None, cleanup_paths: tuple[str, ...] = ()
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
    ]
    cleanup = _cleanup_instruction(cleanup_paths)
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
    seen: list[str] = []
    for match in _CLEANUP_RE.finditer(raw_text or ""):
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
        consulted: this authorizes one unlink of one already-recorded file, and
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

    def _run_implementation(
        self,
        directive: Directive,
        task: Task,
        git: GitGateway,
        agent_runner: AgentRunner,
    ) -> ExecutionOutcome:
        feedback = directive.feedback if directive.decision is Decision.REVISE else None
        # Read BEFORE the agent runs, because it is what the prompt has to
        # state, and read through the callable rather than from anything the
        # agent or this round produced — see `self._cleanup_paths_for`.
        cleanup_paths = self._recorded_cleanup_paths(task)
        spec = AgentSpec(
            domain=task.id,
            title=task.title,
            prompt=_agent_prompt(task, feedback, cleanup_paths),
        )
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
                "validation passed." + _cleanup_note(removed, ignored)
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
