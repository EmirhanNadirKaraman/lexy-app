"""scope-05: a round can PUT BACK what an earlier round of the same task edited
out of scope — and nothing else.

The hole this closes, observed on port-01 (2026-08-20). scope-04 gave a round a
way to DELETE a path the loop recorded it created outside `approved_paths`, and
`implement_executor` said its own limit outright: "an EDIT to the same path is
as unauthorized as it ever was". port-01's contamination was ten EDITED files
and zero creations, so `REMOVE-OUT-OF-SCOPE` could do nothing for any of them;
the reviewer's instruction to strip the residue was unperformable for the second
time, and because a revise builds on the same branch the same edits were handed
to every following round. 8 commits over 11 attempts, a contaminated set that
could not shrink, and a branch an operator discarded by hand.

The rule these tests pin: what the loop ITSELF recorded as written out of scope
is exactly the set a later round may restore, the content comes from git at the
execution record's immutable `task_base_sha`, and nothing about scope moves.

Real git and a real `ImplementExecutor` throughout (the agent is stubbed; the
`claude` CLI is never invoked), for the same reason `test_scope_cleanup.py` does
it — the claim spans the prompt the agent reads, the restore the executor
performs, and the record that is written. Small helpers are duplicated rather
than imported, per this suite's self-contained convention.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from autoloop.audit.agents import AgentResult
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.git_gateway import GitGateway
from autoloop.implement_executor import (
    IMPLEMENT_DISALLOWED_TOOLS,
    WRITE_ALLOWED_TOOLS,
    ImplementExecutor,
    _agent_prompt,
    _revert_recorded_file,
)
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import (
    IntentStore,
    RecordedRevertAuthority,
    TaskExecution,
    TaskExecutionStore,
)
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/test-conversation"

#: A tracked file that exists at the base commit, is OUTSIDE `approved_paths`,
#: and round 1 EDITS. port-01's shape, and the one `REMOVE-OUT-OF-SCOPE` cannot
#: express: deleting it would be a second, worse overrun.
EDITED = "lexy-app/backend/main.py"
BASE_TEXT = "app = 1\n"
OVERRUN_TEXT = "app = 1\nSNEAKED = True\n"

#: Tracked at the base, outside `approved_paths`, and written by NO round — so
#: the loop never records it and no request can ever authorize touching it.
UNTOUCHED = "lexy-app/backend/other.py"
UNTOUCHED_TEXT = "other = 2\n"

#: The file round 1 CREATES out of scope. It has no content at the base, which
#: is where `REVERT` and `REMOVE` deliberately converge.
CREATED = "autoloop/obsolete.py"

#: In `approved_paths`: legitimate work, never recorded out of scope, never
#: revertable however it is named.
IN_SCOPE = "feature.py"


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def ok_validation(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


class ScriptedAgentRunner:
    """A write-capable subagent, scripted per round.

    Writes files exactly as a real agent's Edit/Write calls would, and can never
    delete or restore one — the production constraint (`WRITE_ALLOWED_TOOLS`, no
    `Bash`) that this whole feature exists to work around. A double with a
    shortcut around it would pass against an implementation that never needed to
    exist. The last entry repeats if more rounds run than were scripted.
    """

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = 0
        self.root: Path | None = None
        self.prompts: list[str] = []

    def run(self, spec):
        self.prompts.append(spec.prompt)
        step = self.rounds[min(self.calls, len(self.rounds) - 1)]
        self.calls += 1
        for rel, content in (step.get("write") or {}).items():
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return AgentResult(
            domain=spec.domain,
            raw_text=step.get("text", "done"),
            returncode=0,
            duration_seconds=0.1,
            command=("claude",),
        )


class FakeAuthority:
    """A `RecordedRevertAuthority`-shaped double, so the absent/malformed/raising
    inputs can be reached without corrupting a record the orchestrator also uses
    (`task_base_sha` drives ancestry checks, worker recreation and the reviewed
    range — rewriting it in place would break the round for unrelated reasons)."""

    def __init__(self, sha="", raises=False, record_raises=False):
        self.sha = sha
        self.raises = raises
        self.record_raises = record_raises
        self.recorded: list[tuple[str, tuple[str, ...]]] = []

    def base_sha(self, task_id: str) -> str:
        if self.raises:
            raise RuntimeError("no record")
        return self.sha

    def record_reverted(self, task_id, paths) -> None:
        if self.record_raises:
            raise RuntimeError("record unwritable")
        self.recorded.append((task_id, tuple(paths)))


def build_loop(
    tmp_path,
    rounds,
    task_id="t1",
    approved_paths=(IN_SCOPE,),
    validation=(),
    command_runner=None,
    wire_cleanup=True,
    revert_authority="real",
):
    """An orchestrator driving a REAL `ImplementExecutor` over a real worktree.

    `revert_authority=None` reproduces an embedder that never wires one — the
    fail-closed default, where no revert authority exists at all. Any other
    object is used as-is (see `FakeAuthority`).
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n")
    for rel, text in ((EDITED, BASE_TEXT), (UNTOUCHED, UNTOUCHED_TEXT)):
        target = repo_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    run_git(repo_root, "add", "-A")
    run_git(repo_root, "commit", "-q", "-m", "init")

    policy = PolicyEngine(PolicyConfig(implement_enabled=True))
    git = GitGateway(repo_root, policy)
    worktrees = WorktreeManager(git, tmp_path / "worktrees")
    execution_store = TaskExecutionStore(tmp_path / "executions")
    intent_store = IntentStore(tmp_path / "intents")

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(implement_enabled=True),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(URL)
    store.save(state)

    task = Task(
        id=task_id,
        title=f"Title {task_id}",
        description="desc",
        approved_paths=tuple(approved_paths),
    )
    registry = TaskRegistry([task])
    task_store = TaskStore(config.tasks_file)
    task_store.save(registry)

    runner = ScriptedAgentRunner(rounds)

    def factory(root):
        runner.root = Path(root)
        return runner

    def cleanup_paths_for(tid: str) -> tuple[str, ...]:
        execution = execution_store.load(tid)
        return tuple(execution.out_of_scope_paths) if execution is not None else ()

    if revert_authority == "real":
        revert_authority = RecordedRevertAuthority(execution_store)

    executor = ImplementExecutor(
        git=git,
        agent_runner=runner,
        validation_commands=validation,
        command_runner=command_runner,
        worker_repo_root_for=worktrees.path_for,
        policy=policy,
        agent_runner_factory=factory,
        cleanup_paths_for=cleanup_paths_for if wire_cleanup else None,
        revert_authority=revert_authority,
    )

    def no_client():
        raise AssertionError("no browser client expected in this test")

    orch = Orchestrator(
        config=config,
        store=store,
        state=state,
        policy=policy,
        git=git,
        executor=executor,
        transcript=TranscriptLogger(config.transcript_file),
        client_factory=no_client,
        registry=registry,
        task_store=task_store,
        manifest_store=ManifestStore(config.manifests_dir),
        worktrees=worktrees,
        execution_store=execution_store,
        intent_store=intent_store,
        validation_runner=ok_validation,
    )
    return orch, worktrees, execution_store, task, runner


def implement(task_id="t1"):
    return Directive(decision=Decision.IMPLEMENT, reason="do it", task_id=task_id)


def revise(task_id="t1", feedback="revert the out-of-scope edits"):
    return Directive(
        decision=Decision.REVISE, reason="not quite", task_id=task_id, feedback=feedback
    )


def round_one_edits_out_of_scope():
    """Round 1 of the port-01 shape: real work in scope, one file EDITED out."""
    return {
        "write": {IN_SCOPE: "print('hi')\n", EDITED: OVERRUN_TEXT},
        "text": "implemented the feature",
    }


def next_round(orch):
    orch.state.phase = Phase.READY.value


def reached_review(orch) -> bool:
    return "POST-COMMIT REVIEW PACKET" in (orch.state.outbox or "")


def worker_text(worktrees, task_id, rel) -> str:
    return (worktrees.path_for(task_id) / rel).read_text(encoding="utf-8")


def tree_paths(worktrees, task_id, sha) -> set[str]:
    out = run_git(worktrees.path_for(task_id), "ls-tree", "-r", "--name-only", sha)
    return {line for line in out.splitlines() if line}


def blob_at(worktrees, task_id, sha, rel) -> str:
    return run_git(worktrees.path_for(task_id), "show", f"{sha}:{rel}")


def range_paths(worktrees, task_id, base, candidate) -> set[str]:
    """`commit_range_paths`' own question, asked of real git: what does the
    reviewed range actually contain? THE discriminating check for a revert — a
    byte comparison can pass while the path is still in the diff (a stale mode
    bit alone keeps it there), and narrowing this range back toward the declared
    scope is the entire point of the feature."""
    out = run_git(
        worktrees.path_for(task_id), "diff", "--name-only", base, candidate
    )
    return {line for line in out.splitlines() if line}


# =============================================================================
# THE claim
# =============================================================================


def test_a_later_round_reverts_the_out_of_scope_edit_its_earlier_round_made(tmp_path):
    """Round 1 edits a pre-existing file outside its scope and the loop records
    it; round 2 names that exact path and the executor restores it to its
    `task_base_sha` content, byte for byte."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {"text": f"REVERT-OUT-OF-SCOPE: {EDITED}"},
        ],
    )

    orch._dispatch_executor(implement(task.id))
    after_one = execution_store.load(task.id)
    assert after_one.out_of_scope_paths == (EDITED,), "the loop recorded the overrun"
    assert worker_text(worktrees, task.id, EDITED) == OVERRUN_TEXT
    assert blob_at(worktrees, task.id, after_one.candidate_sha, EDITED) == OVERRUN_TEXT

    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    after_two = execution_store.load(task.id)
    assert reached_review(orch), "the repair round reached review, not a park"
    assert worker_text(worktrees, task.id, EDITED) == BASE_TEXT
    assert blob_at(worktrees, task.id, after_two.candidate_sha, EDITED) == BASE_TEXT, (
        "byte-identical to the task_base_sha content, in the COMMIT"
    )


def test_the_reverted_path_leaves_the_reviewed_range_entirely(tmp_path):
    """What the repair is FOR. `commit_range_paths(task_base_sha, candidate)` is
    the range every path section of the review packet is computed from, and a
    path restored to its base content has the same bytes at both ends of it."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {"text": f"REVERT-OUT-OF-SCOPE: {EDITED}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    base = execution_store.load(task.id).task_base_sha
    assert EDITED in range_paths(
        worktrees, task.id, base, execution_store.load(task.id).candidate_sha
    ), "round 1's overrun really is in the reviewed range"

    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    after_two = execution_store.load(task.id)
    reviewed = range_paths(worktrees, task.id, base, after_two.candidate_sha)
    assert EDITED not in reviewed, "the repair narrowed the range back into scope"
    assert IN_SCOPE in reviewed, "and left the round's legitimate work alone"


def test_the_revert_is_recorded_on_the_execution_record(tmp_path):
    """The record is what keeps "a round took its overrun back" distinguishable
    from "no round ever overran" — the diff cannot show it, by construction."""
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {"text": f"REVERT-OUT-OF-SCOPE: {EDITED}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert execution.reverted_out_of_scope_paths == (EDITED,)
    # Regression history, not a scratch pad — same rule as the removal record.
    assert EDITED in execution.out_of_scope_paths
    assert execution.removed_out_of_scope_paths == (), "a revert is not a deletion"


def test_the_revert_record_survives_the_orchestrators_own_save(tmp_path):
    """The window that would silently eat it: the orchestrator loads the record
    BEFORE dispatch and saves its in-memory copy AFTER the executor returns, so
    a plain last-writer-wins save would drop the executor's mid-dispatch write
    every time. `TaskExecutionStore.save` unions this one field with disk."""
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {"text": f"REVERT-OUT-OF-SCOPE: {EDITED}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    reloaded = TaskExecutionStore(tmp_path / "executions").load(task.id)
    assert reloaded.reverted_out_of_scope_paths == (EDITED,), (
        "JSON has no tuples: the load half of the serialization has to exist too"
    )


def test_the_revert_is_named_in_the_round_report(tmp_path):
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {"text": f"REVERT-OUT-OF-SCOPE: {EDITED}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    summary = execution_store.load(task.id).report_summary
    assert "Reverted 1 recorded out-of-scope path(s)" in summary
    assert EDITED in summary


def test_a_revert_only_round_still_reaches_review(tmp_path):
    """The restore has to happen BEFORE the `git status` read, or a round whose
    only work is a repair reports "changed no files" and dies with the restored
    content sitting uncommitted on disk."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {"text": f"REVERT-OUT-OF-SCOPE: {EDITED}"},  # nothing else at all
        ],
    )
    orch._dispatch_executor(implement(task.id))
    first_candidate = execution_store.load(task.id).candidate_sha
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert reached_review(orch)
    assert execution.candidate_sha != first_candidate, "the repair was committed"
    assert worker_text(worktrees, task.id, EDITED) == BASE_TEXT


def test_validation_runs_against_the_tree_the_revert_produced(tmp_path):
    """The second ordering constraint. Validating before the restore would grade
    a tree that still holds the overrun, i.e. not the one being committed."""
    seen: list[str] = []

    def recording_runner(argv, **kwargs):
        seen.append((Path(kwargs["cwd"]) / EDITED).read_text(encoding="utf-8"))

        class Proc:
            returncode = 0
            stdout = "All checks passed!\n"
            stderr = ""

        return Proc()

    orch, _worktrees, _execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {"text": f"REVERT-OUT-OF-SCOPE: {EDITED}"},
        ],
        validation=(("ruff", "check", "."),),
        command_runner=recording_runner,
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    assert seen == [OVERRUN_TEXT, BASE_TEXT]


# =============================================================================
# The overlap with REMOVE, decided and pinned
# =============================================================================


def test_reverting_a_path_that_did_not_exist_at_the_base_makes_it_absent(tmp_path):
    """The decided overlap rule. A created file has no base content, so its base
    STATE is absence and a revert produces exactly that — the one shape on which
    the two instructions cannot differ. It is recorded as reverted (this
    executor restored it) and, because git saw a deletion, also as removed."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            {
                "write": {IN_SCOPE: "print('hi')\n", CREATED: "# scaffolding\n"},
                "text": "implemented the feature",
            },
            {"text": f"REVERT-OUT-OF-SCOPE: {CREATED}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    assert execution_store.load(task.id).out_of_scope_paths == (CREATED,)
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert not (worktrees.path_for(task.id) / CREATED).exists()
    assert CREATED not in tree_paths(worktrees, task.id, execution.candidate_sha)
    assert execution.reverted_out_of_scope_paths == (CREATED,)
    assert execution.removed_out_of_scope_paths == (CREATED,), (
        "both records are true of a created path; neither is double-counting"
    )


def test_asking_for_both_removes_the_path_and_reports_the_revert_superseded(tmp_path):
    """Fixed order, stated in the prompt and pinned here: the removal runs first
    and a revert of the same path is reported rather than performed. Restoring
    the base bytes afterwards would quietly undo the deletion the same report
    asked for."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {
                "text": (
                    f"REMOVE-OUT-OF-SCOPE: {EDITED}\n"
                    f"REVERT-OUT-OF-SCOPE: {EDITED}\n"
                )
            },
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert not (worktrees.path_for(task.id) / EDITED).exists(), "the removal won"
    assert execution.removed_out_of_scope_paths == (EDITED,)
    assert execution.reverted_out_of_scope_paths == ()
    assert "superseded by this round's removal" in execution.report_summary


# =============================================================================
# What stays refused
# =============================================================================


def test_a_path_never_recorded_as_out_of_scope_is_never_reverted(tmp_path):
    """Not a general escape hatch. `UNTOUCHED` is tracked from the base commit,
    is outside `approved_paths`, and no round of this task ever wrote it — so no
    recorded evidence exists and the request buys nothing."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            # Real work alongside the refused request, so the round commits and
            # its report reaches the record — a round that changed nothing at
            # all never gets that far.
            {
                "write": {IN_SCOPE: "print('hi again')\n"},
                "text": f"REVERT-OUT-OF-SCOPE: {UNTOUCHED}",
            },
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert UNTOUCHED not in execution.out_of_scope_paths, "no recorded evidence"
    assert worker_text(worktrees, task.id, UNTOUCHED) == UNTOUCHED_TEXT
    assert execution.reverted_out_of_scope_paths == ()
    # Refused, and VISIBLE as refused — a silently dropped request reads exactly
    # like a satisfied one to the reviewer who asked for it.
    assert "Ignored 1 revert request(s)" in execution.report_summary
    # Counted, never quoted: the path is a string the agent chose, and this
    # summary becomes the commit message.
    assert UNTOUCHED not in execution.report_summary


def test_an_in_scope_path_is_never_reverted(tmp_path):
    """A path inside `approved_paths` is authorized work, so the loop never
    records it out of scope and no revert request can reach it — the round's own
    edit to it survives untouched."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {
                "write": {IN_SCOPE: "print('round two')\n"},
                "text": f"REVERT-OUT-OF-SCOPE: {IN_SCOPE}",
            },
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert worker_text(worktrees, task.id, IN_SCOPE) == "print('round two')\n"
    assert execution.reverted_out_of_scope_paths == ()
    assert "Ignored 1 revert request(s)" in execution.report_summary


def test_the_revert_never_widens_the_task_scope(tmp_path):
    """The bound the whole feature is built inside: repair authority is not
    scope, so neither persisted authorization field may gain a thing."""
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {"text": f"REVERT-OUT-OF-SCOPE: {EDITED}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    allowed_before = execution_store.load(task.id).allowed_paths
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert execution.allowed_paths == allowed_before
    assert EDITED not in execution.allowed_paths
    # `approved_paths` is the Task's, and is the source `allowed_paths` is
    # re-synced from on every dispatch — it must be untouched too.
    assert task.approved_paths == (IN_SCOPE,)


def test_editing_a_recorded_path_is_still_unauthorized(tmp_path):
    """The asymmetry, restated for the second instruction: being revertable is
    not being ownable. An ordinary edit to the same recorded path gains nothing
    — it lands and is recorded out of scope, exactly as before."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {"write": {EDITED: "app = 1\nEDITED AGAIN\n"}, "text": "edited it"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    allowed_before = execution_store.load(task.id).allowed_paths
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert worker_text(worktrees, task.id, EDITED) == "app = 1\nEDITED AGAIN\n"
    assert execution.reverted_out_of_scope_paths == ()
    assert EDITED in execution.out_of_scope_paths, "still flagged for the reviewer"
    assert execution.allowed_paths == allowed_before


def test_an_echo_of_the_instruction_reverts_nothing(tmp_path):
    """Echo-safety is structural, not textual: the prompt's example is the
    placeholder `<repository-relative path>`, which is not a path the loop can
    ever have recorded."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {
                "write": {IN_SCOPE: "print('hi again')\n"},
                "text": (
                    "I was told to write REVERT-OUT-OF-SCOPE: <repository-relative "
                    "path>\nbut there was nothing to put back."
                ),
            },
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    assert worker_text(worktrees, task.id, EDITED) == OVERRUN_TEXT
    assert execution_store.load(task.id).reverted_out_of_scope_paths == ()


@pytest.mark.parametrize(
    "line",
    [
        "- REVERT-OUT-OF-SCOPE: {p}",
        "> REVERT-OUT-OF-SCOPE: {p}",
        "* REVERT-OUT-OF-SCOPE: {p}",
        "see the REVERT-OUT-OF-SCOPE: {p} convention",
    ],
)
def test_a_marked_up_line_is_prose_about_the_rule_not_a_request(tmp_path, line):
    """Same anchoring discipline as `_CLEANUP_RE`: a bullet or quote marker is an
    agent summarising its instructions, not asking for a repair."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {
                "write": {IN_SCOPE: "print('hi again')\n"},
                "text": line.format(p=EDITED),
            },
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    assert worker_text(worktrees, task.id, EDITED) == OVERRUN_TEXT
    assert execution_store.load(task.id).reverted_out_of_scope_paths == ()


# =============================================================================
# Fail-closed: every absent, malformed or unreadable input
# =============================================================================


def test_without_the_injected_authority_nothing_is_reverted(tmp_path):
    """Fail-closed default: an embedder that never wires `revert_authority`
    grants nothing, and its agents are never even told the capability exists."""
    orch, worktrees, execution_store, task, runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {
                "write": {IN_SCOPE: "print('hi again')\n"},
                "text": f"REVERT-OUT-OF-SCOPE: {EDITED}",
            },
        ],
        revert_authority=None,
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert worker_text(worktrees, task.id, EDITED) == OVERRUN_TEXT
    assert execution.reverted_out_of_scope_paths == ()
    assert all("REVERT-OUT-OF-SCOPE" not in prompt for prompt in runner.prompts)
    # And it says WHICH fail-closed branch it took. "no authority is wired" and
    # "git could not read the base" have different remedies, so reporting the
    # first as the second sends a reviewer hunting a corrupt repository.
    assert "no revert authority is wired" in execution.report_summary


@pytest.mark.parametrize(
    "authority",
    [
        FakeAuthority(sha=""),                    # no record / no base recorded
        FakeAuthority(raises=True),               # unreadable or corrupt record
        FakeAuthority(sha="not-a-sha"),           # a value git must never see
        FakeAuthority(sha="-" * 40),              # a flag-shaped value
    ],
)
def test_an_unusable_base_sha_offers_nothing_and_reverts_nothing(tmp_path, authority):
    orch, worktrees, execution_store, task, runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {
                "write": {IN_SCOPE: "print('hi again')\n"},
                "text": f"REVERT-OUT-OF-SCOPE: {EDITED}",
            },
        ],
        revert_authority=authority,
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert worker_text(worktrees, task.id, EDITED) == OVERRUN_TEXT
    assert execution.reverted_out_of_scope_paths == ()
    assert all("REVERT-OUT-OF-SCOPE" not in prompt for prompt in runner.prompts)
    assert "No task base commit was available" in execution.report_summary


def test_an_unreadable_base_tree_reverts_nothing_and_never_deletes(tmp_path):
    """The worst fail-open available here, closed and pinned: a base tree git
    cannot read must NOT fall through to "the path did not exist at the base",
    which would silently convert every revert request into a deletion."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {
                "write": {IN_SCOPE: "print('hi again')\n"},
                "text": f"REVERT-OUT-OF-SCOPE: {EDITED}",
            },
        ],
        # Well-formed and offerable, so the capability IS advertised — but it
        # names no object this repository holds.
        revert_authority=FakeAuthority(sha="0" * 40),
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert (worktrees.path_for(task.id) / EDITED).exists(), "NOT deleted"
    assert worker_text(worktrees, task.id, EDITED) == OVERRUN_TEXT
    assert execution.reverted_out_of_scope_paths == ()
    assert "could not be read" in execution.report_summary
    assert "NOTHING was reverted" in execution.report_summary


def test_a_record_that_cannot_be_written_is_reported_not_swallowed(tmp_path):
    """The repair is real and in the tree; a reviewer reading a record with no
    revert on it has to be told which of the two situations they are in."""
    authority = FakeAuthority(sha="", record_raises=True)
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {"text": f"REVERT-OUT-OF-SCOPE: {EDITED}"},
        ],
        revert_authority=authority,
    )
    orch._dispatch_executor(implement(task.id))
    # Point the double at the real base only after round 1 recorded it.
    authority.sha = execution_store.load(task.id).task_base_sha
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert worker_text(worktrees, task.id, EDITED) == BASE_TEXT, "restored anyway"
    assert execution.reverted_out_of_scope_paths == ()
    assert "could NOT be written to the execution record" in execution.report_summary


def test_a_revert_survives_a_round_whose_validation_then_fails(tmp_path):
    """Recorded at restore time, not on the success path: the worker tree is not
    rewound when a round fails, so the next round commits the same restored
    content and a success-only record would lose the repair silently."""

    calls = []

    def green_then_red(argv, **kwargs):
        calls.append(argv)

        class Proc:
            returncode = 0 if len(calls) == 1 else 1
            stdout = "All checks passed!\n" if len(calls) == 1 else "boom\n"
            stderr = ""

        return Proc()

    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_edits_out_of_scope(),
            {"text": f"REVERT-OUT-OF-SCOPE: {EDITED}"},
        ],
        validation=(("ruff", "check", "."),),
        command_runner=green_then_red,
    )
    # Round 1 passes and commits (so the overrun is recorded); round 2 restores
    # the file and THEN fails validation, so nothing is committed.
    orch._dispatch_executor(implement(task.id))
    first_candidate = execution_store.load(task.id).candidate_sha
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert execution.candidate_sha == first_candidate, "round 2 committed nothing"
    assert worker_text(worktrees, task.id, EDITED) == BASE_TEXT, "not rewound"
    assert execution.reverted_out_of_scope_paths == (EDITED,)


# =============================================================================
# No new capability for the agent
# =============================================================================


def test_the_agent_gains_no_tool(tmp_path):
    """The bound the task was given: the executor performs the restore, exactly
    as it performs the unlink. The write-capable agent's tool set does not move,
    and `Bash` stays disallowed."""
    assert WRITE_ALLOWED_TOOLS == ("Read", "Grep", "Glob", "Edit", "Write")
    assert "Bash" in IMPLEMENT_DISALLOWED_TOOLS
    assert "Task" in IMPLEMENT_DISALLOWED_TOOLS


def test_a_task_with_no_recorded_residue_is_told_nothing_about_reverting():
    """The ordinary round's prompt is unchanged."""
    task = Task(id="t1", title="Add widget", description="Implement it.")
    assert "REVERT-OUT-OF-SCOPE" not in _agent_prompt(task, None, (), "", True)


def test_the_prompt_states_the_form_and_grants_no_editing_authority():
    task = Task(id="t1", title="Add widget", description="Implement it.")
    prompt = _agent_prompt(task, "revert it", (EDITED,), "", True)

    assert EDITED in prompt
    assert "REVERT-OUT-OF-SCOPE: <repository-relative path>" in prompt
    assert "widens your approved scope by exactly nothing" in prompt
    # The created-path rule and the both-instructions order are stated, not
    # left for the agent to discover by being surprised.
    assert "has no content to restore" in prompt
    assert "reported as superseded" in prompt


def test_the_prompt_omits_the_revert_form_when_it_could_not_be_performed():
    task = Task(id="t1", title="Add widget", description="Implement it.")
    prompt = _agent_prompt(task, "revert it", (EDITED,), "", False)
    assert "REMOVE-OUT-OF-SCOPE" in prompt, "the scope-04 half is untouched"
    assert "REVERT-OUT-OF-SCOPE" not in prompt


# =============================================================================
# The restore itself, unit level
# =============================================================================


def _unit_repo(tmp_path):
    root = tmp_path / "unit"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.email", "test@example.com")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "plain.txt").write_text(BASE_TEXT)
    script = root / "run.sh"
    script.write_text("#!/bin/sh\necho base\n")
    os.chmod(script, 0o755)
    (root / "nested").mkdir()
    (root / "nested" / "deep.txt").write_text("deep\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    git = GitGateway(root, PolicyEngine(PolicyConfig()))
    entries = git.tree_entries(git.tree_of(run_git(root, "rev-parse", "HEAD").strip()))
    return root, git, entries


def test_the_executable_bit_is_restored_with_the_bytes(tmp_path):
    """Git tracks one permission bit. Restoring the content and leaving the mode
    wrong keeps the path in the range diff forever — which is exactly the thing
    a revert exists to take out of it."""
    root, git, entries = _unit_repo(tmp_path)
    script = root / "run.sh"
    script.write_text("#!/bin/sh\necho overrun\n")
    os.chmod(script, 0o644)

    assert _revert_recorded_file(git, "run.sh", entries["run.sh"])
    assert script.read_text() == "#!/bin/sh\necho base\n"
    assert script.stat().st_mode & 0o111, "the exec bit came back"
    assert run_git(root, "status", "--porcelain") == "", "git sees no difference"


def test_a_symlink_at_the_target_is_replaced_not_written_through(tmp_path):
    """A recorded path that is now a symlink costs the link and leaves its
    target alone — the same rule `_remove_recorded_file` follows."""
    root, git, entries = _unit_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("do not touch\n")
    target = root / "plain.txt"
    target.unlink()
    target.symlink_to(outside)

    assert _revert_recorded_file(git, "plain.txt", entries["plain.txt"])
    assert not target.is_symlink()
    assert target.read_text() == BASE_TEXT
    assert outside.read_text() == "do not touch\n"


def test_a_directory_at_the_target_is_refused(tmp_path):
    """No recursive delete is reachable from a revert, exactly as none is from a
    removal."""
    root, git, entries = _unit_repo(tmp_path)
    target = root / "plain.txt"
    target.unlink()
    target.mkdir()
    (target / "child.txt").write_text("still here\n")

    assert not _revert_recorded_file(git, "plain.txt", entries["plain.txt"])
    assert (target / "child.txt").read_text() == "still here\n"


@pytest.mark.parametrize("rel", ["", "   ", "/etc/passwd", "../escape.txt",
                                 "nested/../../escape.txt"])
def test_a_path_that_escapes_the_worker_repo_is_refused(tmp_path, rel):
    root, git, entries = _unit_repo(tmp_path)
    assert not _revert_recorded_file(git, rel, entries["plain.txt"])
    assert not (tmp_path / "escape.txt").exists()


@pytest.mark.parametrize(
    "entry",
    [
        ("120000", "blob", "0" * 40),   # a symlink at the base
        ("160000", "commit", "0" * 40),  # a submodule at the base
        ("100644", "tree", "0" * 40),    # not a blob at all
        "not-a-tuple",
    ],
)
def test_a_base_entry_that_is_not_a_regular_file_is_refused(tmp_path, entry):
    """Writing a symlink's target path out as file bytes is not the base state,
    it is a new and stranger edit."""
    root, git, _entries = _unit_repo(tmp_path)
    (root / "plain.txt").write_text("overrun\n")
    assert not _revert_recorded_file(git, "plain.txt", entry)
    assert (root / "plain.txt").read_text() == "overrun\n"


def test_a_missing_parent_directory_is_recreated(tmp_path):
    """A round that deleted the whole directory a recorded path lived in must
    still be able to put the file back."""
    root, git, entries = _unit_repo(tmp_path)
    (root / "nested" / "deep.txt").unlink()
    (root / "nested").rmdir()

    assert _revert_recorded_file(git, "nested/deep.txt", entries["nested/deep.txt"])
    assert (root / "nested" / "deep.txt").read_text() == "deep\n"


def test_a_path_already_at_the_base_state_is_reported_as_restored(tmp_path):
    """End-state semantics, stated once: the request asks for the file to match
    the base, and it does. Idempotent for both shapes — present and absent."""
    root, git, entries = _unit_repo(tmp_path)
    assert _revert_recorded_file(git, "plain.txt", entries["plain.txt"])
    assert _revert_recorded_file(git, "never-existed.txt", None)
    assert not (root / "never-existed.txt").exists()


# =============================================================================
# The PRODUCTION wiring — the half a capability is worthless without
# =============================================================================
#
# Everything above builds its own `ImplementExecutor` and hands it a
# `revert_authority`. That proves the mechanism and proves NOTHING about a live
# run: the first round of scope-05 shipped exactly this suite with
# `cli._build_executor` passing no `revert_authority` at all, so every real round
# took the fail-closed branch, never saw `REVERT-OUT-OF-SCOPE:` in its prompt,
# and the task's one claim did not hold anywhere outside these tests. These
# exercise the real CLI construction instead.


def _cli_repo(tmp_path):
    """A throwaway checkout `cli._build_orchestrator` will really run against.

    Three things it needs that an ordinary fixture repo does not: a commit (the
    publisher provisioning reads the checkout), an `origin` remote (that is what
    the publisher repo is cloned/snapshotted from), and a `workers_root` OUTSIDE
    the checkout — `validate_workers_root` refuses the run otherwise.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "work")
    run_git(repo, "config", "user.email", "t@e.com")
    run_git(repo, "config", "user.name", "T")
    run_git(repo, "config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("x\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "init")

    upstream = tmp_path / "upstream.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(upstream)], check=True, capture_output=True
    )
    run_git(repo, "remote", "add", "origin", str(upstream))

    (repo / ".autoloop").mkdir()
    (repo / ".autoloop" / "config.toml").write_text(
        '[browser]\nconversation_url = "https://chatgpt.com/c/abc"\n\n'
        f'[paths]\nworkers_root = "{tmp_path / "outside" / "workers"}"\n',
        encoding="utf-8",
    )
    return repo


def _cli_orchestrator(tmp_path, monkeypatch):
    """The production collaborator set, built the way `run` builds it.

    Deliberately NOT `null_executor=True` (which is what
    `test_task_inbox.py`'s own CLI-construction test uses): that returns a
    `NullExecutor` before `ImplementExecutor` is ever constructed, so every
    assertion below would be about an object the flag prevented from existing.
    """
    import argparse

    from autoloop import cli
    from autoloop.config import load_config

    repo = _cli_repo(tmp_path)
    monkeypatch.chdir(repo)
    config = load_config(repo / ".autoloop" / "config.toml")
    store, state = cli._load_state(config)
    if state is None:
        state = LoopState.new(config.browser.conversation_url)
        store = StateStore(config.state_file)
        store.save(state)
    task_store, registry = cli._load_tasks(config)
    args = argparse.Namespace(
        config=repo / ".autoloop" / "config.toml", null_executor=False
    )
    return cli._build_orchestrator(config, args, store, state, task_store, registry)


def test_the_cli_wires_a_revert_authority_into_the_production_executor(
    tmp_path, monkeypatch
):
    """The regression this exists for: `cli._build_executor` passing no
    `revert_authority`, which leaves a live run permanently on the fail-closed
    branch while the whole mechanism above passes."""
    orch = _cli_orchestrator(tmp_path, monkeypatch)
    implement = orch._executor._implement

    assert isinstance(implement._revert_authority, RecordedRevertAuthority)
    # Its sibling, asserted alongside deliberately: WHICH paths may be named
    # still comes from `cleanup_paths_for` and from nothing else, so a round
    # with a revert authority and no cleanup reader could authorize nothing.
    assert implement._cleanup_paths_for is not None


def test_the_wired_authority_reads_and_writes_the_orchestrators_own_record(
    tmp_path, monkeypatch
):
    """`isinstance` is not enough, and the gap it leaves is the silent one: an
    authority bound to a DIFFERENT `TaskExecutionStore` than the loop writes
    through would type-check, read no base sha for any real task, and record
    every repair where nobody looks. So this asks it about a record the
    orchestrator's own store wrote."""
    orch = _cli_orchestrator(tmp_path, monkeypatch)
    authority = orch._executor._implement._revert_authority

    orch._execution_store.save(
        TaskExecution(
            task_id="wired-1",
            task_branch="autoloop/wired-1",
            worktree_path=str(tmp_path / "w"),
            task_base_sha="a" * 40,
        )
    )

    assert authority.base_sha("wired-1") == "a" * 40, "same store, read side"

    authority.record_reverted("wired-1", ("lexy-app/backend/main.py",))
    reloaded = orch._execution_store.load("wired-1")
    assert reloaded.reverted_out_of_scope_paths == ("lexy-app/backend/main.py",), (
        "same store, write side"
    )


def test_the_wired_executor_offers_the_revert_form_for_a_recorded_path(
    tmp_path, monkeypatch
):
    """End to end through the production object: a task with a recorded
    out-of-scope path and a usable base sha gets a prompt that names the second
    request form. Round 1 of scope-05 could not have passed this — its executor
    had no authority, so `_revert_base_sha` returned "" and the form was never
    rendered."""
    orch = _cli_orchestrator(tmp_path, monkeypatch)
    implement = orch._executor._implement
    task = Task(id="wired-2", title="t", description="d", approved_paths=(IN_SCOPE,))

    orch._execution_store.save(
        TaskExecution(
            task_id="wired-2",
            task_branch="autoloop/wired-2",
            worktree_path=str(tmp_path / "w"),
            task_base_sha="b" * 40,
            out_of_scope_paths=(EDITED,),
        )
    )

    prompt = _agent_prompt(
        task,
        "revert it",
        implement._recorded_cleanup_paths(task),
        "",
        bool(implement._revert_base_sha(task)),
    )
    assert EDITED in prompt, "the recorded path is listed for exact copying"
    assert "REVERT-OUT-OF-SCOPE: <repository-relative path>" in prompt
    assert "widens your approved scope by exactly nothing" in prompt


def test_a_corrupt_execution_record_offers_no_revert_and_never_raises(tmp_path):
    """The one adversarial case the wiring itself creates. `TaskExecutionStore.
    load` RAISES `StateCorruptError` on an unreadable record, and until
    production wired a real `RecordedRevertAuthority` nothing ever called it on
    the executor's path — so that raise was theoretical and is now live, and it
    fires before the agent runs. Fail-closed: no base sha, no capability, no
    exception out of the round."""
    store = TaskExecutionStore(tmp_path / "executions")
    (tmp_path / "executions").mkdir(parents=True)
    (tmp_path / "executions" / "corrupt-1.json").write_text("{not json", encoding="utf-8")

    executor = ImplementExecutor(
        git=GitGateway(tmp_path, PolicyEngine(PolicyConfig())),
        agent_runner=ScriptedAgentRunner([{"text": "x"}]),
        revert_authority=RecordedRevertAuthority(store),
    )
    task = Task(id="corrupt-1", title="t", description="d")

    with pytest.raises(Exception):
        store.load("corrupt-1")          # the record really is unreadable
    assert executor._revert_base_sha(task) == ""
    assert executor._record_reverted(task, (EDITED,)) is False


def test_a_task_with_no_record_yet_offers_no_revert(tmp_path):
    """A first dispatch has no execution record at all, so there is no base sha
    to restore from — "" rather than a guess, and the same answer the absent
    authority gives."""
    executor = ImplementExecutor(
        git=GitGateway(tmp_path, PolicyEngine(PolicyConfig())),
        agent_runner=ScriptedAgentRunner([{"text": "x"}]),
        revert_authority=RecordedRevertAuthority(
            TaskExecutionStore(tmp_path / "executions")
        ),
    )
    assert executor._revert_base_sha(Task(id="never", title="t", description="d")) == ""
