"""del-01: a round can delete a file INSIDE its own approved paths.

The gap this closes. `WRITE_ALLOWED_TOOLS` is Read/Grep/Glob/Edit/Write and
`Bash` is disallowed, so no tool in the write-capable agent's set removes a file.
That was never a decision to forbid deletion — deletion left with `Bash`, for
isolation reasons enforced separately by `escape_detector` — and the prohibition
never prevented destruction either: `Edit`/`Write` over an authorized path can
already reduce that file to nothing. What it stopped was destroying a file
CLEANLY, which is why roadmap-01 (2026-08-18) committed a zero-byte file where a
correct review had asked for an absent one, and why three specs in one night
(brw-14, port-05, shrink-01) had to be rewritten around the missing capability.

The rule these tests pin: a path the task's own `approved_paths` authorize it to
WRITE it may also DELETE; the removal shows up in `changed_paths` and is
scope-checked by the same code that records an out-of-scope write; a shared
documentation tracker is refused even though writing it is allowed; and the
round's own report names every file it removed.

Real git and a real `ImplementExecutor` throughout (the agent is stubbed; the
`claude` CLI is never invoked), because the claim spans four layers — the prompt,
the gate, the unlink, and the commit git actually makes. Small helpers are
duplicated rather than imported, per this suite's self-contained convention (see
`test_scope_cleanup.py`, whose fixture shape this mirrors deliberately).
"""

from __future__ import annotations

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
    _delete_instruction,
    _extract_delete_requests,
    _scoped_delete_note,
    _ScopedDeletes,
)
from autoloop.manifest import ManifestStore
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import (
    TRACKER_PATHS,
    Task,
    TaskRegistry,
    TaskStore,
    deletable_paths,
)
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import IntentStore, TaskExecutionStore
from autoloop.worktree import WorktreeManager

URL = "https://chatgpt.com/c/test-conversation"

#: In scope, written and committed by round 1, deleted by round 2. The whole
#: claim is about this path.
DOOMED = "extra.py"
#: In scope, and the thing that keeps a round from ending as "changed no files".
KEPT = "feature.py"
#: Tracked from the base commit, outside `approved_paths`, and never written by
#: any round — the "still refused" path.
OUTSIDE = "lexy-app/backend/main.py"
#: A shared append-only ledger every task may WRITE and no task may delete.
TRACKER = "docs/TESTS.md"

SCOPE = (KEPT, DOOMED)


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
    It writes files exactly as a real agent's Edit/Write calls would, and it can
    NEVER delete one — the constraint this whole feature exists to lift, so the
    double must not be given a shortcut around it either. Every deletion in this
    file therefore goes through the executor, which is the thing under test.
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
    approved_paths=SCOPE,
    validation=(),
    command_runner=None,
):
    """An orchestrator driving a REAL `ImplementExecutor` over a real worktree."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    run_git(repo_root, "init", "-q", "-b", "main")
    run_git(repo_root, "config", "user.email", "test@example.com")
    run_git(repo_root, "config", "user.name", "Test")
    run_git(repo_root, "config", "commit.gpgsign", "false")
    (repo_root / "README.md").write_text("hello\n")
    for rel, body in ((OUTSIDE, "app = 1\n"), (TRACKER, "# Tests\n")):
        path = repo_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
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
        cleanup_paths_for=cleanup_paths_for,
        # `ScriptedAgentRunner` indexes its script by CALL, and every step here is
        # written as one call per ROUND. Since advis-01 a round whose agent never
        # uses the advisory channel is handed back once — a second call inside
        # one round, which would consume the NEXT round's step and make each test
        # here grade a mechanism it is not about. The hand-back has its own
        # coverage in `test_agent_self_validation.py` §10a.
        advisory_zero_call_returns=0,
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


def revise(task_id="t1", feedback="drop the file we no longer need"):
    return Directive(
        decision=Decision.REVISE, reason="not quite", task_id=task_id, feedback=feedback
    )


def round_one():
    """Two in-scope files, both committed — so round 2 deletes a TRACKED path,
    which is the case `git add -- <path>` has to stage as a deletion."""
    return {
        "write": {KEPT: "print('hi')\n", DOOMED: "# scaffolding\n"},
        "text": "implemented the feature",
    }


def next_round(orch):
    orch.state.phase = Phase.READY.value


def reached_review(orch) -> bool:
    """Did THIS round end with a review packet rather than a park?"""
    return "POST-COMMIT REVIEW PACKET" in (orch.state.outbox or "")


def tree_paths(worktrees, task_id, sha) -> set[str]:
    """What the COMMIT contains, read from git rather than from the worktree."""
    out = run_git(worktrees.path_for(task_id), "ls-tree", "-r", "--name-only", sha)
    return {line for line in out.splitlines() if line}


def range_paths(worktrees, task_id, base, candidate) -> set[str]:
    """`git diff-tree -r --name-only` over the reviewed range — the same read
    `GitGateway.changed_paths`/`commit_range_paths` make, which is what the task
    claims lists a DELETED path exactly as it lists a modified one."""
    out = run_git(
        worktrees.path_for(task_id), "diff-tree", "-r", "--name-only", base, candidate
    )
    return {line for line in out.splitlines() if line}


# =============================================================================
# The gate: the same matcher that decides whether a WRITE was in scope
# =============================================================================


def test_the_gate_authorizes_a_path_inside_the_approved_scope():
    authorized, outside, trackers = deletable_paths((DOOMED,), SCOPE)
    assert authorized == {DOOMED}
    assert outside == set()
    assert trackers == set()


def test_the_gate_refuses_a_path_outside_the_approved_scope():
    authorized, outside, trackers = deletable_paths((OUTSIDE,), SCOPE)
    assert authorized == set()
    assert outside == {OUTSIDE}
    assert trackers == set()


def test_the_gate_honours_a_directory_prefix_exactly_as_a_write_does():
    """`unauthorized_paths` treats a trailing '/' as a subtree grant, and this
    reuses that function rather than restating the rule — so a prefix authorizes
    deleting inside it, and never a sibling directory that merely shares a
    character prefix."""
    authorized, outside, _trackers = deletable_paths(
        ("autoloop/tests/test_x.py", "autoloop/tests_backup/secret.py"),
        ("autoloop/tests/",),
    )
    assert authorized == {"autoloop/tests/test_x.py"}
    assert outside == {"autoloop/tests_backup/secret.py"}


def test_an_unscoped_task_can_delete_nothing():
    """`effective_approved_paths(())` is `()`, so an unscoped task gets the same
    fail-closed answer for deleting that it already gets for writing."""
    authorized, outside, _trackers = deletable_paths((DOOMED,), ())
    assert authorized == set()
    assert outside == {DOOMED}


@pytest.mark.parametrize("tracker", TRACKER_PATHS)
def test_every_tracker_path_is_refused_although_writing_it_is_allowed(tracker):
    """The grant exists so a task can APPEND its change note to a ledger every
    task shares. It is not a licence to remove one — and the refusal is its own
    category, because "outside your approved paths" would be false here."""
    authorized, outside, trackers = deletable_paths((tracker,), SCOPE)
    assert authorized == set()
    assert trackers == {tracker}
    assert outside == set(), "a tracker refusal must not be reported as out of scope"


def test_a_tracker_is_refused_even_when_the_task_declares_it_itself():
    """Unconditional, including when the task's OWN scope covers the tracker —
    by an exact entry or by a directory prefix. Two tasks' change notes are what
    is being protected, and which grant reaches the file does not change that."""
    for scope in ((TRACKER,), ("docs/",)):
        authorized, outside, trackers = deletable_paths((TRACKER,), scope)
        assert authorized == set(), scope
        assert trackers == {TRACKER}, scope
        assert outside == set(), scope


# =============================================================================
# The request line: anchored, deduplicated, echo-proof
# =============================================================================


def test_the_request_line_is_anchored_and_deduplicated():
    assert _extract_delete_requests(
        f"DELETE-FILE: {DOOMED}\n  DELETE-FILE: {KEPT}\nDELETE-FILE: {DOOMED}\n"
    ) == (DOOMED, KEPT)


@pytest.mark.parametrize(
    "line",
    [
        "- DELETE-FILE: {p}",
        "> DELETE-FILE: {p}",
        "* DELETE-FILE: {p}",
        "see the DELETE-FILE: {p} convention",
    ],
)
def test_a_marked_up_line_is_prose_about_the_rule_not_a_request(line):
    """Same anchoring discipline as `_ASSUMPTION_RE`, `_CLEANUP_RE` and
    `_REVERT_RE`: a bullet or a quote marker is an agent summarising its
    instructions, not asking for a deletion."""
    assert _extract_delete_requests(line.format(p=DOOMED)) == ()


# =============================================================================
# The prompt: the capability is stated, and stated inside its bound
# =============================================================================


def test_the_prompt_states_the_form_and_the_tracker_refusal():
    task = Task(
        id="t1", title="Add widget", description="Implement it.", approved_paths=SCOPE
    )
    prompt = _agent_prompt(task, None, ())

    assert "DELETE-FILE: <repository-relative path>" in prompt
    # The sentence that stops "you may delete inside your scope" being read as
    # "deleting is how you get out of your scope".
    assert "SCOPE-CHECKED EXACTLY LIKE A WRITE" in prompt
    for tracker in TRACKER_PATHS:
        assert tracker in prompt
    # A move is the thing this unlocks, and the thing it does not: both halves
    # have to be in scope.
    assert "A MOVE is a write plus a deletion" in prompt


def test_an_unscoped_task_is_told_nothing_about_deleting():
    """Fail-closed branch: with no approved paths nothing is deletable, so
    describing the form would offer a capability that cannot fire."""
    task = Task(id="t1", title="Add widget", description="Implement it.")
    prompt = _agent_prompt(task, None, ())
    assert "DELETE-FILE" not in prompt
    # And the section it would have followed is still there, unchanged.
    assert "APPROVED SCOPE: none." in prompt


def test_no_new_tool_is_granted():
    """The point is one bounded capability, not a shell. The executor performs
    the unlink; the agent's tool set is exactly what it was."""
    assert WRITE_ALLOWED_TOOLS == ("Read", "Grep", "Glob", "Edit", "Write")
    assert "Bash" in IMPLEMENT_DISALLOWED_TOOLS


def test_the_delete_section_forges_nothing_and_stays_bounded():
    """The two questions every added prompt section is asked here: can an agent
    quoting it back forge a request on another channel, and does it stay
    bounded. Both `_ASSUMPTION_RE` and `_CLEANUP_RE`/`_REVERT_RE` match an anchor
    at the start of an INDENTED line, so this section must contain no line that
    begins with one — the property `_authoring_rules` is already held to."""
    section = _delete_instruction(SCOPE)

    for line in section.splitlines():
        stripped = line.lstrip().upper()
        assert not stripped.startswith("ASSUMPTION:")
        assert not stripped.startswith("REMOVE-OUT-OF-SCOPE:")
        assert not stripped.startswith("REVERT-OUT-OF-SCOPE:")
    # It is ONE section: `_agent_prompt` joins on a blank line and every reader
    # that locates a section by its heading depends on this one containing none.
    assert "\n\n" not in section
    # Bounded like the other fixed prose — this is the kind of section that
    # attracts "one more sentence" and it is re-read by every round.
    assert len(section) <= 2200


# =============================================================================
# THE claim, end to end
# =============================================================================


def test_a_round_deletes_a_tracked_file_inside_its_approved_paths(tmp_path):
    """THE claim. Round 1 commits two in-scope files; round 2 removes one of
    them and the candidate does not contain it.

    Also the proof that `commit_and_capture` can stage what this produces: the
    deleted path is TRACKED at round 2's HEAD, so `dirty_paths_all()` reports it
    as ` D extra.py` and `git add -- extra.py` stages the removal. If it could
    not, the round would raise `GitCommandError` and park as `commit_refused`
    instead of reaching review.
    """
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [round_one(), {"text": f"DELETE-FILE: {DOOMED}"}],
    )

    orch._dispatch_executor(implement(task.id))
    after_one = execution_store.load(task.id)
    assert DOOMED in tree_paths(worktrees, task.id, after_one.candidate_sha)

    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    after_two = execution_store.load(task.id)
    assert reached_review(orch), "the deletion round reached review, not a park"
    assert after_two.candidate_sha != after_one.candidate_sha, "it committed"
    assert not (worktrees.path_for(task.id) / DOOMED).exists()
    assert DOOMED not in tree_paths(worktrees, task.id, after_two.candidate_sha), (
        "absent from the candidate, not committed as a zero-byte file"
    )
    assert KEPT in tree_paths(worktrees, task.id, after_two.candidate_sha)


def test_the_removal_appears_in_the_reviewed_range_as_a_removal(tmp_path):
    """`changed_paths` visibility. `git diff-tree -r --name-only` lists a DELETED
    path exactly as it lists a modified one, which is why nothing new had to be
    built to bound this — and it is what puts the deletion in front of the
    reviewer as a diff rather than only as a sentence."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [round_one(), {"text": f"DELETE-FILE: {DOOMED}"}],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    round_two_range = range_paths(
        worktrees,
        task.id,
        f"{execution.candidate_sha}^",
        execution.candidate_sha,
    )
    assert DOOMED in round_two_range, "the deletion is a changed path like any other"


def test_the_in_scope_deletion_is_scope_checked_and_records_no_overrun(tmp_path):
    """Scope-checked exactly like any other change: the same comparison runs, and
    an authorized path passes it, so nothing lands in the out-of-scope record."""
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [round_one(), {"text": f"DELETE-FILE: {DOOMED}"}],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert execution.out_of_scope_paths == ()
    assert execution.removed_out_of_scope_paths == (), (
        "an in-scope deletion is not out-of-scope cleanup and must not be "
        "recorded as one"
    )


def test_a_delete_only_round_still_reaches_review(tmp_path):
    """The unlink has to happen BEFORE the `git status` read, or a round whose
    only work is a removal reports "changed no files in its worker repo" and dies
    with the deletion sitting uncommitted on disk."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [round_one(), {"text": f"DELETE-FILE: {DOOMED}"}],  # nothing else at all
    )
    orch._dispatch_executor(implement(task.id))
    first = execution_store.load(task.id).candidate_sha
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert reached_review(orch)
    assert execution.candidate_sha != first
    assert not (worktrees.path_for(task.id) / DOOMED).exists()


def test_validation_runs_against_the_tree_the_deletion_produced(tmp_path):
    """Validating before the unlink would grade a tree that still contains the
    file, i.e. not the one being committed."""
    seen: list[bool] = []

    def recording_runner(argv, **kwargs):
        seen.append((Path(kwargs["cwd"]) / DOOMED).exists())

        class Proc:
            returncode = 0
            stdout = "All checks passed!\n"
            stderr = ""

        return Proc()

    orch, _worktrees, _execution_store, task, _runner = build_loop(
        tmp_path,
        [round_one(), {"text": f"DELETE-FILE: {DOOMED}"}],
        validation=(("ruff", "check", "."),),
        command_runner=recording_runner,
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    assert seen == [True, False], (
        "round 1 validated with the file present, round 2 after its removal"
    )


def test_a_file_created_and_deleted_in_one_round_leaves_no_trace_but_is_reported(
    tmp_path,
):
    """The shape where "the removal appears in `changed_paths`" has nothing to
    appear: an UNTRACKED file that is written and then deleted in the same round
    is absent from `git status` entirely, so it reaches neither `changed_paths`
    nor `git add`.

    That is exactly right for the commit — git has nothing to record — and it is
    why the round REPORT is the disclosure and not the diff. It also proves
    `commit_and_capture` is never handed a path git would refuse: an untracked,
    now-absent path would fail `git add -- <path>` with "did not match any
    files" and park the task `commit_refused`."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one(),
            {
                "write": {KEPT: "print('again')\n", "scratch.py": "# temporary\n"},
                "text": "Wrote a scratch file, then removed it.\nDELETE-FILE: scratch.py",
            },
        ],
        approved_paths=(KEPT, DOOMED, "scratch.py"),
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id, "drop the scratch file"))

    execution = execution_store.load(task.id)
    assert reached_review(orch), "the round committed rather than parking"
    assert not (worktrees.path_for(task.id) / "scratch.py").exists()
    assert "scratch.py" not in tree_paths(worktrees, task.id, execution.candidate_sha)
    # Invisible to git, visible to the reviewer.
    assert "DELETED 1 file(s)" in execution.report_summary
    assert "scratch.py" in execution.report_summary


def test_a_move_is_a_write_plus_a_delete(tmp_path):
    """What this unlocks. Both halves in scope: write the new path, delete the
    old, and the candidate holds the new one alone."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one(),
            {
                "write": {"moved.py": "# scaffolding\n"},
                "text": f"Moved it.\nDELETE-FILE: {DOOMED}",
            },
        ],
        approved_paths=(KEPT, DOOMED, "moved.py"),
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id, "rename it"))

    execution = execution_store.load(task.id)
    committed = tree_paths(worktrees, task.id, execution.candidate_sha)
    assert "moved.py" in committed
    assert DOOMED not in committed
    assert execution.out_of_scope_paths == ()


# =============================================================================
# Disclosure: the round says which files it removed
# =============================================================================


def test_the_round_report_names_every_deleted_path(tmp_path):
    """A round that deletes a file and does not say which has hidden the most
    consequential change it can make. The agent's own text here says NOTHING
    about the deletion beyond the bare request line — the sentence has to come
    from what the executor actually unlinked, not from the agent's account."""
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [round_one(), {"text": f"DELETE-FILE: {DOOMED}"}],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    summary = execution_store.load(task.id).report_summary
    assert "DELETED 1 file(s)" in summary
    assert DOOMED in summary


def test_a_round_that_commits_nothing_still_discloses_the_deletion(tmp_path):
    """The branch every other disclosure test misses, and the one a reviewer
    asking "can a deletion ever be hidden?" would look at.

    A round whose ONLY work is creating and deleting an untracked in-scope path
    leaves `git status` empty, so `_run_implementation` returns
    `status="error"` ("changed no files") and the orchestrator returns from
    `_dispatch_task_postcommit` BEFORE `execution.report_summary` is written.
    So the disclosure travels a DIFFERENT route here — `outcome.summary` into
    the `implementation_review` packet in `state.outbox` — and that route is the
    only one available, which is why it is asserted separately rather than
    assumed to follow from the committed case."""
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one(),
            {"write": {"scratch.py": "# temporary\n"}, "text": "DELETE-FILE: scratch.py"},
        ],
        approved_paths=(KEPT, DOOMED, "scratch.py"),
    )
    orch._dispatch_executor(implement(task.id))
    first = execution_store.load(task.id).candidate_sha
    next_round(orch)
    orch._dispatch_executor(revise(task.id, "drop the scratch file"))

    assert not reached_review(orch), "no candidate: the round changed no files"
    assert execution_store.load(task.id).candidate_sha == first
    outbox = orch.state.outbox or ""
    assert "changed no files in its worker repo" in outbox
    assert "DELETED 1 file(s)" in outbox
    assert "scratch.py" in outbox


def test_the_named_deletion_list_is_bounded_and_says_what_it_dropped():
    """`_bounded_paths` caps the one list this note NAMES, because the summary
    becomes the commit message and a directory-prefix scope can authorize
    arbitrarily many files. A silent truncation would read exactly like complete
    coverage, so the dropped count is always stated."""
    note = _scoped_delete_note(
        _ScopedDeletes(done=tuple(f"pkg/mod_{i:03d}.py" for i in range(25)))
    )

    assert "DELETED 25 file(s)" in note
    assert "pkg/mod_000.py" in note
    assert "and 5 more not listed" in note
    assert "pkg/mod_024.py" not in note


def test_the_report_is_computed_from_the_unlink_not_from_the_agents_account(tmp_path):
    """An ECHO must not become evidence. The agent claims forty deletions and
    asks for none; the summary reports what happened, which is nothing."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one(),
            {
                "write": {KEPT: "print('again')\n"},
                "text": "I deleted 40 files, including extra.py and main.py.",
            },
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    summary = execution_store.load(task.id).report_summary
    assert "DELETED" not in summary
    assert (worktrees.path_for(task.id) / DOOMED).exists()


# =============================================================================
# What stays refused
# =============================================================================


def test_a_deletion_outside_the_approved_paths_is_refused(tmp_path):
    """Refused by the mechanism that already refuses an out-of-scope write —
    `tasks.unauthorized_paths` over `effective_approved_paths`, the same call the
    loop's own two scope comparisons make."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one(),
            # Real work alongside the refused request, so the round commits and
            # its report reaches the record.
            {"write": {KEPT: "print('again')\n"}, "text": f"DELETE-FILE: {OUTSIDE}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert (worktrees.path_for(task.id) / OUTSIDE).exists(), "still there"
    assert OUTSIDE in tree_paths(worktrees, task.id, execution.candidate_sha)
    # Refused, and VISIBLE as refused.
    assert "Refused 1 deletion request(s)" in execution.report_summary
    # Counted, never quoted: the path is a string the agent chose, and this
    # summary becomes the commit message.
    assert OUTSIDE not in execution.report_summary


def test_a_tracker_deletion_is_refused_and_says_so_plainly(tmp_path):
    """A WRITE to `docs/TESTS.md` is allowed for every task; a deletion is not,
    and the refusal names the file and the reason rather than calling it out of
    scope."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one(),
            {"write": {KEPT: "print('again')\n"}, "text": f"DELETE-FILE: {TRACKER}"},
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    summary = execution.report_summary
    assert (worktrees.path_for(task.id) / TRACKER).exists()
    assert TRACKER in tree_paths(worktrees, task.id, execution.candidate_sha)
    assert "Refused to delete 1 shared documentation tracker(s)" in summary
    assert TRACKER in summary
    assert "not a licence to remove one" in summary
    assert "outside this task's approved paths" not in summary


def test_an_echo_of_the_instruction_deletes_nothing(tmp_path):
    """Echo-safety is doubly structural: the prompt's example is the placeholder
    `<repository-relative path>`, which no `approved_paths` entry can authorize
    (segments are `[A-Za-z0-9._-]`) and which no file on disk is named."""
    orch, worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [
            round_one(),
            {
                "write": {KEPT: "print('again')\n"},
                # At the START of its line, so the ANCHOR matches and the
                # placeholder really is extracted as a request — otherwise this
                # would test the anchoring rule a second time instead of the
                # two guards that stop an echo.
                "text": (
                    "Following the instruction I was given:\n"
                    "DELETE-FILE: <repository-relative path>\n"
                    "but there was nothing to delete."
                ),
            },
        ],
    )
    orch._dispatch_executor(implement(task.id))
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    assert (worktrees.path_for(task.id) / DOOMED).exists()
    assert "DELETED" not in execution_store.load(task.id).report_summary


def test_the_capability_never_widens_the_task_scope(tmp_path):
    """The bound the whole feature sits inside: this reads `approved_paths` and
    writes nothing, so neither persisted authorization field may gain a thing."""
    orch, _worktrees, execution_store, task, _runner = build_loop(
        tmp_path,
        [round_one(), {"text": f"DELETE-FILE: {DOOMED}"}],
    )
    orch._dispatch_executor(implement(task.id))
    allowed_before = execution_store.load(task.id).allowed_paths
    next_round(orch)
    orch._dispatch_executor(revise(task.id))

    execution = execution_store.load(task.id)
    assert execution.allowed_paths == allowed_before
    assert task.approved_paths == SCOPE


# =============================================================================
# The unlink's own guards, exercised directly
# =============================================================================


def scratch_executor(tmp_path, approved, files=(), recorded=()):
    """A real `GitGateway` over a real repo plus the pieces `_apply_scoped_
    deletes` needs — no orchestrator, so a guard can be aimed at directly."""
    root = tmp_path / "scratch"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.email", "test@example.com")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "config", "commit.gpgsign", "false")
    for rel, body in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    (root / "seed.txt").write_text("seed\n")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    git = GitGateway(root, PolicyEngine(PolicyConfig(implement_enabled=True)))
    task = Task(
        id="t1", title="t", description="d", approved_paths=tuple(approved)
    )
    return git, task, tuple(recorded)


def test_a_directory_is_never_deleted(tmp_path):
    """No recursive delete exists on this path at all. `pkg` is an EXACT approved
    entry here, so the scope gate authorizes it and the refusal has to come from
    `_remove_recorded_file`, which removes only a regular file or a symlink."""
    git, task, recorded = scratch_executor(
        tmp_path, ("pkg",), files=(("pkg/mod.py", "x = 1\n"),)
    )
    result = ImplementExecutor._apply_scoped_deletes(
        git, task, recorded, "DELETE-FILE: pkg"
    )
    assert result.done == ()
    assert result.failed == ("pkg",)
    assert (git.repo_root / "pkg" / "mod.py").exists()


def test_a_traversal_out_of_the_worker_repo_is_refused(tmp_path):
    """A directory prefix authorizes by `startswith`, so a `..` segment can slip
    through the SCOPE gate. `_remove_recorded_file` refuses it, which is why the
    guard is defence in depth rather than decoration — and the refused string is
    COUNTED, never named, because it is unbounded and the summary becomes a
    commit message."""
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("do not delete me\n")
    git, task, recorded = scratch_executor(
        tmp_path, ("pkg/",), files=(("pkg/mod.py", "x = 1\n"),)
    )
    result = ImplementExecutor._apply_scoped_deletes(
        git, task, recorded, "DELETE-FILE: pkg/../../secret.txt"
    )
    assert result.done == ()
    assert outside_file.exists()


def test_an_absolute_path_is_refused_by_the_scope_gate(tmp_path):
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("do not delete me\n")
    git, task, recorded = scratch_executor(tmp_path, ("pkg/",))
    result = ImplementExecutor._apply_scoped_deletes(
        git, task, recorded, f"DELETE-FILE: {outside_file}"
    )
    assert result.done == ()
    assert result.outside == (str(outside_file),)
    assert outside_file.exists()


def test_an_authorized_path_with_no_file_at_it_is_reported_not_failed(tmp_path):
    git, task, recorded = scratch_executor(tmp_path, ("pkg/",))
    result = ImplementExecutor._apply_scoped_deletes(
        git, task, recorded, "DELETE-FILE: pkg/never-written.py"
    )
    assert result.done == ()
    assert result.absent == ("pkg/never-written.py",)


def test_a_symlink_is_unlinked_as_the_link_and_its_target_survives(tmp_path):
    """The rule `_remove_recorded_file` already follows, now reachable from a
    third gate: a link pointing out of the worker repo costs the link."""
    target = tmp_path / "outside.txt"
    target.write_text("survive\n")
    git, task, recorded = scratch_executor(tmp_path, ("pkg/",))
    link = git.repo_root / "pkg"
    link.mkdir()
    (link / "link.txt").symlink_to(target)

    result = ImplementExecutor._apply_scoped_deletes(
        git, task, recorded, "DELETE-FILE: pkg/link.txt"
    )
    assert result.done == ("pkg/link.txt",)
    assert not (link / "link.txt").is_symlink()
    assert target.exists() and target.read_text() == "survive\n"


def test_a_path_the_out_of_scope_record_governs_is_left_to_that_instruction(tmp_path):
    """The two authorities never both act on one path in one round. Reachable
    only after an operator widens a task's scope to cover a path an earlier round
    already overran; without this branch a report naming it under both forms
    would delete it and then restore it, and both notes would be wrong."""
    git, task, recorded = scratch_executor(
        tmp_path,
        ("pkg/",),
        files=(("pkg/mod.py", "x = 1\n"),),
        recorded=("pkg/mod.py",),
    )
    result = ImplementExecutor._apply_scoped_deletes(
        git, task, recorded, "DELETE-FILE: pkg/mod.py"
    )
    assert result.done == ()
    assert result.deferred == ("pkg/mod.py",)
    assert (git.repo_root / "pkg" / "mod.py").exists()


def test_no_request_line_does_nothing_at_all(tmp_path):
    """The ordinary round: every field empty, no filesystem call made."""
    git, task, recorded = scratch_executor(
        tmp_path, ("pkg/",), files=(("pkg/mod.py", "x = 1\n"),)
    )
    result = ImplementExecutor._apply_scoped_deletes(
        git, task, recorded, "I implemented the feature and wrote a test."
    )
    assert result == ImplementExecutor._apply_scoped_deletes(git, task, recorded, "")
    assert result.done == ()
    assert (git.repo_root / "pkg" / "mod.py").exists()
