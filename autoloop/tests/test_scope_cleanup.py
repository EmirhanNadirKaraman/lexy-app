"""scope-04: a task can delete what IT created out of scope, and nothing else.

The deadlock this closes, observed on roadmap-01 (2026-08-18). Advisory path
scope (2026-08-05) lets an out-of-scope write LAND and be recorded rather than
blocked, so round 1 created `autoloop/obsolete.py` and the record duly said so.
The reviewer then asked — correctly, and twice verbatim — that the file be
absent from the candidate rather than committed as a zero-byte addition. The
write-capable agent has `Read/Grep/Glob/Edit/Write` and no `Bash`, so it could
not delete anything at all; the identical feedback tripped
`review_feedback_unchanged` and the task parked after 8 rounds with its actual
implementation already accepted.

The rule these tests pin: what the loop ITSELF recorded as written out of scope,
from its own diff, is exactly the set a later round may remove — deletion only,
exact paths only, and no widening of `approved_paths` or `allowed_paths` for
anything.

Real git and a real `ImplementExecutor` throughout (the agent is stubbed; the
`claude` CLI is never invoked), because the claim spans three layers — the
prompt the agent reads, the unlink the executor performs, and the record the
orchestrator writes — and a test that stubbed the executor would prove none of
the interesting half. Small helpers are duplicated rather than imported, per
this suite's self-contained convention (see `test_postcommit_primitives.py`).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoloop.audit.agents import AgentResult
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.git_gateway import GitGateway
from autoloop.implement_executor import ImplementExecutor, _agent_prompt
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import Task, TaskRegistry, TaskStore, authorized_cleanup_paths
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import IntentStore, TaskExecutionStore
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/test-conversation"

#: The out-of-scope file round 1 creates — roadmap-01's own path.
RESIDUE = "autoloop/obsolete.py"
#: A tracked file that exists from the base commit, is NOT in `approved_paths`,
#: and no round ever writes. Deliberately not under `autoloop/tests/` and not a
#: `TRACKER_PATHS` entry: a "still refused" assertion aimed at a path the task
#: was approved to touch would be pinning the wrong rule.
UNRECORDED = "lexy-app/backend/main.py"


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

    Each entry is `{"write": {path: text}, "text": "<what the agent said>"}`.
    It writes files, exactly as a real agent's Edit/Write calls would, and it
    can NEVER delete one — which is the constraint this whole feature exists to
    work around, so the double must not be given a shortcut around it either.
    The last entry repeats if more rounds run than were scripted.
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


def build_loop(
    tmp_path,
    rounds,
    task_id="t1",
    approved_paths=("feature.py",),
    validation=(),
    command_runner=None,
    wire_cleanup=True,
):
    """An orchestrator driving a REAL `ImplementExecutor` over a real worktree.

    `wire_cleanup=False` reproduces an embedder that never passes
    `cleanup_paths_for` — the fail-closed default, where no cleanup authority
    exists at all.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n")
    unrelated = repo_root / UNRECORDED
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("app = 1\n")
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

    executor = ImplementExecutor(
        git=git,
        agent_runner=runner,
        validation_commands=validation,
        command_runner=command_runner,
        worker_repo_root_for=worktrees.path_for,
        policy=policy,
        agent_runner_factory=factory,
        cleanup_paths_for=cleanup_paths_for if wire_cleanup else None,
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


def revise(task_id="t1", feedback="remove the residue you added"):
    return Directive(
        decision=Decision.REVISE, reason="not quite", task_id=task_id, feedback=feedback
    )


def round_one_creates_residue():
    """Round 1 of the roadmap-01 shape: real work in scope, one file out."""
    return {
        "write": {"feature.py": "print('hi')\n", RESIDUE: "# scaffolding\n"},
        "text": "implemented the feature",
    }


def next_round(orch):
    """Advance past round 1's review so the next dispatch is round 2."""
    orch.state.phase = Phase.READY.value


def reached_review(orch) -> bool:
    """Did THIS round end with a review packet rather than a park?

    Read from `state.outbox`, which `_finish_postcommit` writes directly, so
    the assertion does not depend on the browser transport running afterwards.
    Every round replaces it, and no other exit renders this marker — a failed
    round writes the `implementation_review` template and a park writes a
    blocker — so the marker's presence is specific to the round just dispatched.
    """
    return "POST-COMMIT REVIEW PACKET" in (orch.state.outbox or "")


def tree_paths(worktrees, task_id, sha) -> set[str]:
    """What the COMMIT contains, read from git rather than from the worktree."""
    out = run_git(worktrees.path_for(task_id), "ls-tree", "-r", "--name-only", sha)
    return {line for line in out.splitlines() if line}


# =============================================================================
# The matcher: exact paths, nothing else
# =============================================================================


def test_the_matcher_authorizes_only_an_exactly_recorded_path():
    authorized, refused = authorized_cleanup_paths(
        (RESIDUE, "autoloop/other.py"), (RESIDUE,)
    )
    assert authorized == {RESIDUE}
    assert refused == {"autoloop/other.py"}


@pytest.mark.parametrize(
    "requested",
    [
        "autoloop/",                 # the recorded file's directory
        "autoloop/obsolete.py.bak",  # the recorded path as a character prefix
        "autoloop/obsolete.pyc",     # ditto, one character on
        "obsolete.py",               # the basename alone
        "./autoloop/obsolete.py",    # the same file, spelled differently
    ],
)
def test_the_matcher_never_matches_by_prefix_or_neighbourhood(requested):
    """`unauthorized_paths` treats a trailing '/' as a subtree grant; this
    matcher must not, or recording one file would license deleting a tree."""
    authorized, refused = authorized_cleanup_paths((requested,), (RESIDUE,))
    assert authorized == set()
    assert refused == {requested}


def test_an_empty_record_authorizes_nothing():
    authorized, refused = authorized_cleanup_paths((RESIDUE,), ())
    assert authorized == set()
    assert refused == {RESIDUE}


# =============================================================================
# The prompt: the exception is stated, and stated narrowly
# =============================================================================


def test_a_task_with_no_recorded_residue_is_told_nothing_about_cleanup():
    """The ordinary round's prompt is unchanged — no agent is told about a
    capability it has no occasion to use."""
    task = Task(id="t1", title="Add widget", description="Implement it.")
    prompt = _agent_prompt(task, None, ())
    assert "REMOVE-OUT-OF-SCOPE" not in prompt


def test_the_prompt_lists_the_recorded_paths_and_grants_no_editing_authority():
    task = Task(id="t1", title="Add widget", description="Implement it.")
    prompt = _agent_prompt(task, "remove the residue", (RESIDUE,))

    assert RESIDUE in prompt
    assert "REMOVE-OUT-OF-SCOPE: <repository-relative path>" in prompt
    # The sentence that stops "you may delete this" being read as "this is
    # yours to work in" — the reading that would quietly undo the
    # never-widened `allowed_paths` rule.
    assert "does not authorize" in prompt
    assert "edit, recreate, rename into" in prompt


# =============================================================================
# The deadlock itself, end to end
# =============================================================================


def test_a_later_round_deletes_the_out_of_scope_file_its_earlier_round_created(tmp_path):
    """THE claim. Round 1 creates residue out of scope and the loop records it;
    round 2 removes that exact file and reaches review instead of parking."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {"text": f"REMOVE-OUT-OF-SCOPE: {RESIDUE}"},
        ],
    )

    orch._dispatch_executor(implement(task.id))
    after_one = execution_store.load(task.id)
    assert after_one.out_of_scope_paths == (RESIDUE,), "the loop recorded the overrun"
    assert (worktrees.path_for(task.id) / RESIDUE).exists()
    assert RESIDUE in tree_paths(worktrees, task.id, after_one.candidate_sha)

    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    after_two = execution_store.load(task.id)
    assert reached_review(orch), "the cleanup round reached review, not a park"
    assert not (worktrees.path_for(task.id) / RESIDUE).exists()
    assert RESIDUE not in tree_paths(worktrees, task.id, after_two.candidate_sha), (
        "absent from the candidate, not committed as a zero-byte addition"
    )


def test_the_deletion_is_recorded_and_the_overrun_evidence_survives(tmp_path):
    """The cleanup vanishes from the reviewed range — `commit_range_paths` is a
    tree-to-tree diff, so a file created and deleted inside it is simply not
    there. The record is what keeps "a round removed this" distinguishable from
    "no round ever wrote it"."""
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {"text": f"REMOVE-OUT-OF-SCOPE: {RESIDUE}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert execution.removed_out_of_scope_paths == (RESIDUE,)
    # Regression history, not a scratch pad: cleaning up does not erase the
    # record that authorization was exceeded in the first place.
    assert RESIDUE in execution.out_of_scope_paths


def test_the_record_survives_a_reload(tmp_path):
    """JSON has no tuples: the load half of the serialization has to exist, or
    the field comes back as a list and every equality above is a false pass."""
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {"text": f"REMOVE-OUT-OF-SCOPE: {RESIDUE}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    reloaded = TaskExecutionStore(tmp_path / "executions").load(task.id)
    assert reloaded.removed_out_of_scope_paths == (RESIDUE,)


def test_the_deletion_never_widens_the_task_scope(tmp_path):
    """The bound the whole feature is built inside: cleanup authority is not
    scope, so neither persisted authorization field may gain a thing."""
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {"text": f"REMOVE-OUT-OF-SCOPE: {RESIDUE}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    allowed_before = execution_store.load(task.id).allowed_paths
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert execution.allowed_paths == allowed_before
    assert RESIDUE not in execution.allowed_paths
    # `approved_paths` is the Task's, and is the source `allowed_paths` is
    # re-synced from on every dispatch — it must be untouched too.
    assert task.approved_paths == ("feature.py",)


def test_a_cleanup_only_round_still_reaches_review(tmp_path):
    """The unlink has to happen BEFORE the `git status` read, or a round whose
    only work is a removal reports "changed no files in its worker repo" and
    dies with the deletion sitting uncommitted on disk."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {"text": f"REMOVE-OUT-OF-SCOPE: {RESIDUE}"},  # nothing else at all
        ],
    )
    orch._dispatch_executor(implement(task.id))
    first_candidate = execution_store.load(task.id).candidate_sha
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert reached_review(orch)
    assert execution.candidate_sha != first_candidate, "the removal was committed"
    assert not (worktrees.path_for(task.id) / RESIDUE).exists()


def test_the_removal_is_named_in_the_round_report(tmp_path):
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {"text": f"REMOVE-OUT-OF-SCOPE: {RESIDUE}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    summary = execution_store.load(task.id).report_summary
    assert "Removed 1 recorded out-of-scope path(s)" in summary
    assert RESIDUE in summary


# =============================================================================
# What stays refused
# =============================================================================


def test_a_path_never_recorded_as_out_of_scope_is_still_refused(tmp_path):
    """Not a general escape hatch. `UNRECORDED` is tracked from the base
    commit, is outside `approved_paths`, and no round of this task ever wrote
    it — so no recorded evidence exists and the request buys nothing."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            # Real work alongside the refused request, so the round commits and
            # its report reaches the record — a round that changed nothing at
            # all returns "changed no files in its worker repo" and never gets
            # that far, which would make this assert round 1's summary.
            {
                "write": {"feature.py": "print('hi again')\n"},
                "text": f"REMOVE-OUT-OF-SCOPE: {UNRECORDED}",
            },
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert (worktrees.path_for(task.id) / UNRECORDED).exists(), "still there"
    assert UNRECORDED in tree_paths(worktrees, task.id, execution.candidate_sha)
    assert execution.removed_out_of_scope_paths == ()
    # Refused, and VISIBLE as refused — a silently dropped request reads
    # exactly like a satisfied one to the reviewer who asked for it.
    assert "Ignored 1 removal request(s)" in execution.report_summary
    # Counted, never quoted: the path is a string the agent chose, and this
    # summary becomes the commit message.
    assert UNRECORDED not in execution.report_summary


def test_editing_a_recorded_path_is_still_unauthorized(tmp_path):
    """The asymmetry, stated as a test: the exception is DELETION. An ordinary
    edit to the same recorded path gains nothing from having been recorded —
    it lands and is recorded out of scope, exactly as before."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {"write": {RESIDUE: "# still here, now edited\n"}, "text": "edited it"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    allowed_before = execution_store.load(task.id).allowed_paths
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert (worktrees.path_for(task.id) / RESIDUE).exists()
    assert RESIDUE in tree_paths(worktrees, task.id, execution.candidate_sha)
    assert execution.removed_out_of_scope_paths == (), "an edit is not a cleanup"
    assert RESIDUE in execution.out_of_scope_paths, "still flagged for the reviewer"
    assert execution.allowed_paths == allowed_before


def test_recreating_a_removed_path_in_a_later_round_grants_no_authority(tmp_path):
    """Deleting a path does not turn it into a path this task may then own."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {"text": f"REMOVE-OUT-OF-SCOPE: {RESIDUE}"},
            {"write": {RESIDUE: "# back again\n"}, "text": "recreated it"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id, "remove it"))
    allowed_before = execution_store.load(task.id).allowed_paths
    next_round(orch)
    orch._dispatch_executor(revise(task.id, "now put something there"))

    execution = execution_store.load(task.id)
    assert execution.allowed_paths == allowed_before
    assert RESIDUE not in execution.allowed_paths
    assert RESIDUE in tree_paths(worktrees, task.id, execution.candidate_sha)
    assert RESIDUE in execution.out_of_scope_paths


def test_an_echo_of_the_instruction_deletes_nothing(tmp_path):
    """Echo-safety is structural, not textual: the prompt's example is the
    placeholder `<repository-relative path>`, which is not a path the loop can
    ever have recorded — so an agent quoting its own instructions back deletes
    nothing, whatever the anchoring rules do."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {
                "write": {"feature.py": "print('hi again')\n"},
                "text": (
                    "I was told to write REMOVE-OUT-OF-SCOPE: <repository-relative "
                    "path>\nbut there was nothing to remove."
                ),
            },
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert (worktrees.path_for(task.id) / RESIDUE).exists()
    assert execution.removed_out_of_scope_paths == ()


@pytest.mark.parametrize(
    "line",
    [
        "- REMOVE-OUT-OF-SCOPE: {p}",
        "> REMOVE-OUT-OF-SCOPE: {p}",
        "* REMOVE-OUT-OF-SCOPE: {p}",
        "see the REMOVE-OUT-OF-SCOPE: {p} convention",
    ],
)
def test_a_marked_up_line_is_prose_about_the_rule_not_a_request(tmp_path, line):
    """Same anchoring discipline as `_ASSUMPTION_RE`: a bullet or quote marker
    is an agent summarising its instructions, not asking for a deletion."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {
                "write": {"feature.py": "print('hi again')\n"},
                "text": line.format(p=RESIDUE),
            },
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    assert (worktrees.path_for(task.id) / RESIDUE).exists()
    assert execution_store.load(task.id).removed_out_of_scope_paths == ()


def test_without_the_injected_reader_there_is_no_cleanup_authority_at_all(tmp_path):
    """Fail-closed default: an embedder that never wires `cleanup_paths_for`
    grants nothing, and its agents are never even told the capability exists."""
    orch, worktrees, execution_store, task, runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {
                "write": {"feature.py": "print('hi again')\n"},
                "text": f"REMOVE-OUT-OF-SCOPE: {RESIDUE}",
            },
        ],
        wire_cleanup=False,
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    assert (worktrees.path_for(task.id) / RESIDUE).exists()
    assert execution_store.load(task.id).removed_out_of_scope_paths == ()
    assert all("REMOVE-OUT-OF-SCOPE" not in prompt for prompt in runner.prompts)


# =============================================================================
# Ordering: the tree that is validated is the tree that is committed
# =============================================================================


def test_validation_runs_against_the_tree_the_removal_produced(tmp_path):
    """The second ordering constraint. Validating before the unlink would grade
    a tree that still contains the file, i.e. not the one being committed."""
    seen: list[bool] = []

    def recording_runner(argv, **kwargs):
        # `cwd` is the worker repo the round is about to commit from, so this
        # is the tree the suite actually graded.
        seen.append((Path(kwargs["cwd"]) / RESIDUE).exists())

        class Proc:
            returncode = 0
            stdout = "All checks passed!\n"
            stderr = ""

        return Proc()

    orch, _worktrees, _execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one_creates_residue(),
            {"text": f"REMOVE-OUT-OF-SCOPE: {RESIDUE}"},
        ],
        validation=(("ruff", "check", "."),),
        command_runner=recording_runner,
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    assert seen == [True, False], (
        "round 1 validated with the file present, round 2 after its removal"
    )
