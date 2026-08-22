"""ImplementExecutor with fake/stubbed agents: write-capable argv, automatic
model selection, worker-repo cwd isolation, changed_paths derived from real
git status (never the agent's own claim, and NUL-safe for a tricky filename),
agent/validation failure honesty, and "writes nothing outside the worker
repo". The real `claude` CLI is never invoked."""

import json
import subprocess
from pathlib import Path

import pytest

from autoloop.audit.agents import AgentResult, AgentSpec
from autoloop.contract import Decision, Directive
from autoloop.git_gateway import GitGateway
from autoloop.implement_executor import (
    IMPLEMENT_DISALLOWED_TOOLS,
    WRITE_ALLOWED_TOOLS,
    ImplementExecutor,
    _ADVERSARIAL_SELF_TEST,
    _agent_prompt,
    _approved_paths_section,
    implement_agent_runner,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import Task
from autoloop.transcript import DURATION_KEY, Stopwatch


def run_git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _init_repo(root: Path, branch: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    run_git(root, "init", "-q", "-b", branch)
    run_git(root, "config", "user.email", "t@e.c")
    run_git(root, "config", "user.name", "T")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("hi")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def main_repo(tmp_path):
    return _init_repo(tmp_path / "main", "main")


@pytest.fixture
def worker_repo(tmp_path):
    return _init_repo(tmp_path / "worker", "autoloop/t1")


def make_task(task_id="t1", approved_paths=(), description="Implement the widget feature."):
    """`approved_paths` defaults to EMPTY, which is the loop's "no scope
    authorized yet" state — every test whose claim is about the fix boundary
    must pass its own, or it silently exercises the report-only branch of
    `_approved_paths_section` instead of the listed one."""
    return Task(
        id=task_id,
        title="Add widget",
        description=description,
        approved_paths=tuple(approved_paths),
    )


def implement_directive(task_id="t1", feedback=None):
    decision = Decision.REVISE if feedback else Decision.IMPLEMENT
    return Directive(decision=decision, reason="r", task_id=task_id, feedback=feedback)


class FakeAgentRunner:
    """Stands in for a write-capable subagent: as a side effect of "running",
    it writes `write_files` onto disk under `worker_repo` — mirroring what a
    real agent's Edit/Write tool calls would do — then reports `raw_text`,
    which may say ANYTHING (including claiming a file it never touched); the
    executor must never trust it."""

    def __init__(self, worker_repo=None, write_files=None, fail=False, raw_text="done"):
        self.worker_repo = worker_repo
        self.write_files = write_files or {}
        self.fail = fail
        self.raw_text = raw_text
        self.specs = []

    def run(self, spec):
        self.specs.append(spec)
        if self.fail:
            return AgentResult(
                domain=spec.domain, raw_text="", returncode=1,
                duration_seconds=0.1, command=("claude",), error="agent exploded",
            )
        for rel, content in self.write_files.items():
            path = self.worker_repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return AgentResult(
            domain=spec.domain, raw_text=self.raw_text, returncode=0,
            duration_seconds=0.1, command=("claude",),
        )


def make_agent_runner_factory(write_files=None, fail=False, raw_text="done"):
    def factory(root):
        return FakeAgentRunner(worker_repo=root, write_files=write_files, fail=fail, raw_text=raw_text)

    return factory


def ok_command(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def fail_command(argv, **kwargs):
    class Proc:
        returncode = 1
        stdout = ""
        stderr = "boom: ruff found 3 errors\n"

    return Proc()


def build_executor(main_repo, worker_repo, agent_runner_factory, validation=(), command_runner=None):
    policy = PolicyEngine(PolicyConfig())
    git = GitGateway(main_repo, policy)
    return ImplementExecutor(
        git=git,
        # Standalone binding — never reached in these tests, since every one
        # supplies `worker_repo_root_for`/`policy`, which `_bindings_for`
        # prefers unconditionally (mirrors AuditExecutor).
        agent_runner=FakeAgentRunner(),
        validation_commands=validation,
        command_runner=command_runner,
        worker_repo_root_for=lambda task_id: worker_repo,
        policy=policy,
        agent_runner_factory=agent_runner_factory,
    )


# ---- 1 & 2: write-capable argv, automatic model selection -----------------


def test_argv_allows_edit_write_disallows_bash_task(tmp_path):
    runner = implement_agent_runner(tmp_path, runner=lambda *a, **k: None)
    spec = AgentSpec(domain="t1", title="Add widget", prompt="do the thing")
    argv = runner.build_argv(spec)
    allowed = argv[argv.index("--allowedTools") + 1 : argv.index("--disallowedTools")]
    disallowed = argv[argv.index("--disallowedTools") + 1 :]
    assert "Edit" in allowed
    assert "Write" in allowed
    assert "Bash" in disallowed
    assert "Task" in disallowed


def test_no_model_flag_is_passed_automatic_selection(tmp_path):
    runner = implement_agent_runner(tmp_path, runner=lambda *a, **k: None)
    spec = AgentSpec(domain="t1", title="Add widget", prompt="do the thing")
    argv = runner.build_argv(spec)
    assert "--model" not in argv


# ---- 3: cwd is the task's own worker repo ----------------------------------


def test_agent_cwd_is_the_worker_repo_not_main_checkout(main_repo, worker_repo):
    calls = []

    def stub(argv, **kwargs):
        calls.append(kwargs.get("cwd"))
        (Path(kwargs["cwd"]) / "feature.py").write_text("x = 1\n")

        class Proc:
            returncode = 0
            stdout = json.dumps({"result": "done"})
            stderr = ""

        return Proc()

    executor = build_executor(
        main_repo, worker_repo, lambda root: implement_agent_runner(root, runner=stub)
    )
    outcome = executor.execute(implement_directive(), make_task())
    assert outcome.status == "ok"
    assert calls == [str(worker_repo)]
    assert calls[0] != str(main_repo)


# ---- 4: changed_paths from real git status, agent's claim ignored ---------


def test_changed_paths_from_git_status_not_agent_claim(main_repo, worker_repo):
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(
            write_files={"real_change.py": "x = 1\n"},
            raw_text="I edited totally_fake_file_i_never_touched.py and much more",
        ),
    )
    outcome = executor.execute(implement_directive(), make_task())
    assert outcome.status == "ok"
    assert outcome.changed_paths == ("real_change.py",)
    assert "totally_fake_file_i_never_touched.py" not in outcome.changed_paths


# ---- 5: NUL-safe for a filename with a space AND a tab --------------------


def test_filename_with_space_and_tab_round_trips(main_repo, worker_repo):
    tricky = "has space\tand tab.py"
    executor = build_executor(
        main_repo, worker_repo, make_agent_runner_factory(write_files={tricky: "x = 1\n"})
    )
    outcome = executor.execute(implement_directive(), make_task())
    assert outcome.status == "ok"
    assert outcome.changed_paths == (tricky,)


# ---- 6: agent failure -> error, never raises -------------------------------


def test_agent_failure_is_error_not_raised(main_repo, worker_repo):
    executor = build_executor(main_repo, worker_repo, make_agent_runner_factory(fail=True))
    outcome = executor.execute(implement_directive(), make_task())
    assert outcome.status == "error"
    assert "agent exploded" in outcome.summary


# ---- 7: no files changed -> error ------------------------------------------


def test_no_files_changed_is_an_error(main_repo, worker_repo):
    executor = build_executor(main_repo, worker_repo, make_agent_runner_factory(write_files={}))
    outcome = executor.execute(implement_directive(), make_task())
    assert outcome.status == "error"
    assert "changed no files" in outcome.summary


# ---- 8: validation failure -> error, validation summary carried -----------


def test_validation_failure_is_error_with_summary(main_repo, worker_repo):
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "print('hi')\n"}),
        validation=(("ruff", "check", "."),),
        command_runner=fail_command,
    )
    outcome = executor.execute(implement_directive(), make_task())
    assert outcome.status == "error"
    assert "validation failed" in outcome.summary
    assert "FAIL" in outcome.validation


# ---- 9: success -> ok, changed_paths + validation populated ---------------


def test_success_is_ok_with_changed_paths_and_validation(main_repo, worker_repo):
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "print('hi')\n"}),
        validation=(("ruff", "check", "."),),
        command_runner=ok_command,
    )
    outcome = executor.execute(implement_directive(), make_task())
    assert outcome.status == "ok"
    assert outcome.changed_paths == ("feature.py",)
    assert outcome.validation.startswith("ruff check .: PASS")


# ---- assumptions: the disclosure that replaced asking a human --------------
#
# `ask_user` is retired, so an ambiguous task cannot be escalated mid-run. The
# agent is told to take the smallest reversible reading and to write an
# `ASSUMPTION:` line per choice; these pin that the instruction is actually
# given and that the lines are actually collected — the two halves are useless
# apart.


def test_the_prompt_tells_the_agent_to_take_the_smallest_reversible_reading(
    main_repo, worker_repo
):
    factory_runners = []

    def factory(root):
        runner = FakeAgentRunner(worker_repo=root, write_files={"feature.py": "x = 1\n"})
        factory_runners.append(runner)
        return runner

    executor = build_executor(main_repo, worker_repo, factory)
    executor.execute(implement_directive(), make_task())

    prompt = factory_runners[0].specs[0].prompt
    assert "SMALLEST REVERSIBLE READING" in prompt
    assert "ASSUMPTION:" in prompt
    # An instruction to take a reading without one to disclose it would be
    # strictly worse than the question it replaced.
    assert "shown to the reviewer" in prompt
    # And it must not send the agent looking for a human that is not there.
    assert "do NOT stop to ask" in prompt


def test_the_approved_decomposition_reaches_the_implementing_agent(
    main_repo, worker_repo
):
    """The other half of "approved before any code is written": a plan the
    implementing round cannot read is a record, not an instruction. The stored
    text is passed through verbatim, and it is labelled as approved — an agent
    that read it as a suggestion would be free to implement something else."""
    factory_runners = []

    def factory(root):
        runner = FakeAgentRunner(worker_repo=root, write_files={"feature.py": "x = 1\n"})
        factory_runners.append(runner)
        return runner

    task = make_task()
    task.decomposition = (
        "Approach: one commit\nFiles expected to change:\n  - feature.py\n"
        "This is one step:\n  1. add the widget and its test"
    )
    executor = build_executor(main_repo, worker_repo, factory)
    executor.execute(implement_directive(), task)

    prompt = factory_runners[0].specs[0].prompt
    assert "add the widget and its test" in prompt
    assert "Approved decomposition" in prompt
    assert "BEFORE any code was written" in prompt
    # The task's own description is still there — the plan adds to it.
    assert task.description in prompt


def test_a_task_with_no_stored_plan_gets_no_decomposition_section(
    main_repo, worker_repo
):
    """Nothing is fabricated for a task that predates the field or was
    dispatched by a path that carries no plan: the agent sees no heading rather
    than an empty one it might try to satisfy."""
    factory_runners = []

    def factory(root):
        runner = FakeAgentRunner(worker_repo=root, write_files={"feature.py": "x = 1\n"})
        factory_runners.append(runner)
        return runner

    executor = build_executor(main_repo, worker_repo, factory)
    executor.execute(implement_directive(), make_task())

    assert "Approved decomposition" not in factory_runners[0].specs[0].prompt


# ---- adversarial self-test: the instruction, and what it must NOT change ----
#
# Measured 2026-08-20 over every reviewer verdict in `transcript.jsonl` (580
# directives): 229 `revise` to 80 `push`, and 112 of those 229 are one shape —
# the claim is PARTLY met and one named case is not. The agent optimised for
# validation passing; the reviewer graded whether the claim held everywhere.
# `_ADVERSARIAL_SELF_TEST` is the instruction that closes that gap. These tests
# pin that it is actually given, on every path, and — the larger risk — that it
# took nothing away while being added.
#
# Revision round 2 (2026-08-22) moved the FIX BOUNDARY off the task text and
# onto `Task.approved_paths` (`_approved_paths_section`), because a description
# names files as context that the task was never authorized to write and a task
# with no decomposition was pointed at a document it does not have. So several
# of these tests now state an `approved_paths` explicitly: `make_task()` has
# none, which is the report-only branch, and a boundary claim tested there
# proves nothing about the listed one.


def capture_prompt(main_repo, worker_repo, task, directive):
    """The prompt a real `execute()` round actually sent, not a hand-built one.

    Goes through the executor rather than calling `_agent_prompt` directly
    wherever a test's claim is about a DECISION (implement vs revise), because
    the feedback branch lives in `_run_implementation` (line 650), not in the
    prompt builder — a direct call cannot exercise it and would pass against an
    executor that never passed feedback through at all."""
    captured = []

    def factory(root):
        runner = FakeAgentRunner(worker_repo=root, write_files={"feature.py": "x = 1\n"})
        captured.append(runner)
        return runner

    executor = build_executor(main_repo, worker_repo, factory)
    outcome = executor.execute(directive, task)
    return captured[0].specs[0].prompt, outcome


@pytest.mark.parametrize("feedback", [None, "the claim still fails for an empty list"])
def test_the_self_critique_instruction_is_given_for_implement_and_revise(
    main_repo, worker_repo, feedback
):
    """Both decisions, and `revise` is the one that matters most: by then the
    claim has ALREADY been judged not to hold in some case, so a revise round is
    the last place to stop asking for the next one."""
    directive = implement_directive(feedback=feedback)
    expected = Decision.REVISE if feedback else Decision.IMPLEMENT
    assert directive.decision is expected  # the helper flips on feedback

    task = make_task(approved_paths=("feature.py",))
    prompt, _ = capture_prompt(main_repo, worker_repo, task, directive)

    assert "ADVERSARIALLY TEST YOUR OWN CLAIM" in prompt
    # The boundary the instruction refers to, on both decisions — a revise
    # round must be told its scope for the same reason it is told to look
    # again: it is the round most likely to go fixing the next thing it finds.
    assert "THIS TASK'S APPROVED PATHS" in prompt
    assert "\n  feature.py\n" in prompt
    # The gap itself, named — passing validation is not the bar being graded.
    assert "DIFFERENT BARS" in prompt
    # The class the reviewer demonstrably hunts (port-02, prov-01, hlth-01).
    assert "FAIL-OPEN" in prompt
    assert "silently PASSES" in prompt
    # Evidence, not reassurance.
    assert "ADVERSARIAL CASES:" in prompt
    assert "where it is handled" in prompt
    assert "unfalsifiable" in prompt
    # And the previous round's feedback still reaches the agent — the addition
    # sits in the same `parts` list and must not have displaced it.
    if feedback:
        assert feedback in prompt


def test_the_instruction_is_bounded_to_the_claim_and_forbids_going_fixing(
    main_repo, worker_repo
):
    """THE bound. An unbounded "make it robust" would make the second-worst
    failure worse: `changed_paths_outside_approved` has already parked 9 tasks
    and cost port-01 its whole branch. The instruction has to say, in the
    prompt and not merely in a comment, that this is not permission to wander."""
    prompt, _ = capture_prompt(
        main_repo,
        worker_repo,
        make_task(approved_paths=("feature.py",)),
        implement_directive(),
    )

    assert "Fix only the failures inside THIS TASK'S APPROVED PATHS" in prompt
    assert "Anything outside them you REPORT rather than fix" in prompt
    assert "not permission to improve the code, widen the task" in prompt


def test_a_failure_outside_the_approved_paths_is_reported_not_fixed(
    main_repo, worker_repo
):
    """The boundary is the task's OWN `approved_paths` — the set the loop
    authorized this round against — and everything else is report-only.

    Round 1 bounded it to "the files this task's description and approved
    decomposition already cover" instead, which is the wrong set in both
    directions: a description names files as CONTEXT that the task was never
    authorized to write, so the agent was pointed at unapproved paths by the
    very sentence meant to keep it in scope. Asserted through a real round's
    prompt, and paired with the negative below so "outside" has a referent."""
    prompt, _ = capture_prompt(
        main_repo,
        worker_repo,
        make_task(approved_paths=("feature.py", "autoloop/tests/")),
        implement_directive(),
    )

    # Inside: the authorized set, listed literally, one per line.
    assert "\n  feature.py\n" in prompt
    assert "\n  autoloop/tests/\n" in prompt
    # Outside: reported, never fixed — and the report is the deliverable, so
    # the wording has to survive verbatim.
    assert "Anything outside them you REPORT rather than fix" in prompt
    assert "write a path the list does not name" in prompt
    # The directory-prefix rule the loop's own matcher applies
    # (`tasks.unauthorized_paths`), so "inside" means the same thing in the
    # prompt and in the check that grades the diff.
    assert "An entry ending in '/' covers the files under it" in prompt


def test_a_file_the_description_mentions_is_not_inside_the_fix_boundary(
    main_repo, worker_repo
):
    """The exact confusion the previous wording created, pinned.

    `lexy-app/backend/main.py` is named in the DESCRIPTION as context and is
    not in `approved_paths`. It must therefore appear in the prompt (the
    description is sent verbatim — the agent needs the context) and NOT in the
    fix boundary. Both halves are asserted: without the first, this test would
    pass just as well against a string that is simply nowhere in the prompt."""
    contextual = "lexy-app/backend/main.py"
    task = make_task(
        approved_paths=("feature.py",),
        description=f"Implement the widget feature. For context, {contextual} mounts it.",
    )

    prompt, _ = capture_prompt(main_repo, worker_repo, task, implement_directive())

    assert contextual in prompt  # carried by the description, as before
    assert contextual not in _ADVERSARIAL_SELF_TEST
    assert contextual not in _approved_paths_section(task.approved_paths)
    # ...and the instruction says so in as many words, because an agent reading
    # a description that names a file is exactly who needs telling.
    assert "merely MENTIONS is not approved unless the list names it" in prompt


def test_a_task_with_no_decomposition_still_gets_a_well_formed_prompt(
    main_repo, worker_repo
):
    """Most tasks carry an approved decomposition and some do not. A task
    without one must still get the instruction and a prompt with every other
    section intact — not a dangling reference and not a dropped clause.

    Round 1's instruction pointed at "this task's description and approved
    decomposition", so a task with no plan was sent to a document it does not
    have. The reference is now `_approved_paths_section`, which is rendered
    unconditionally — so the assertion is not merely that the prompt is well
    formed, but that the instruction no longer names a document that may be
    absent at all."""
    task = make_task(approved_paths=("feature.py",))
    assert not task.decomposition

    prompt, outcome = capture_prompt(main_repo, worker_repo, task, implement_directive())

    assert outcome.status == "ok"
    assert "ADVERSARIALLY TEST YOUR OWN CLAIM" in prompt
    assert "Approved decomposition" not in prompt
    # No dangling reference: the boundary names no optional document, and what
    # it does say "listed below" about is in the prompt underneath it.
    assert "decomposition" not in _ADVERSARIAL_SELF_TEST.lower()
    assert "description" not in _ADVERSARIAL_SELF_TEST.lower()
    # ...and what "listed below" points at is really below it. Indexed on the
    # SECTION's own opening, not on the phrase the instruction itself uses:
    # "THIS TASK'S APPROVED PATHS" occurs in both, and `str.index` would find
    # the instruction's own copy and compare the sentence with itself.
    assert prompt.index("listed below") < prompt.index(
        "THIS TASK'S APPROVED PATHS — declared by the task itself"
    )
    # Still well formed: every other section is present and separated.
    assert task.description in prompt
    assert "Ground rules:" in prompt
    assert "SMALLEST REVERSIBLE READING" in prompt
    assert "\n\n" in prompt
    assert not prompt.startswith("\n")


def test_a_task_with_no_approved_paths_gets_a_report_only_boundary(
    main_repo, worker_repo
):
    """The empty case is FAIL-CLOSED, and it is a real one: `approved_paths`
    empty is the state the orchestrator refuses to dispatch a write-capable
    round in at all (`docs/SECURITY.md` finding #2 / circular ownership), so a
    prompt built in it is already off the expected path.

    "No list" must therefore read as "fix nothing extra", never as an absent
    boundary — an instruction to hunt failures with the sentence that bounds it
    silently gone is precisely the fail-open shape the instruction itself is
    about."""
    task = make_task()
    assert task.approved_paths == ()

    prompt, outcome = capture_prompt(main_repo, worker_repo, task, implement_directive())

    assert outcome.status == "ok"
    assert "ADVERSARIALLY TEST YOUR OWN CLAIM" in prompt
    assert "the loop has NO approved path list" in prompt
    assert "REPORT-ONLY" in prompt
    assert "fix nothing beyond the change the task itself asks for" in prompt
    # Well formed around it: no empty section, no doubled separator.
    assert "\n\n\n" not in prompt
    assert prompt.strip() == prompt


def test_the_addition_grants_no_tool_and_relaxes_no_ground_rule(
    main_repo, worker_repo
):
    """The largest risk in adding an instruction is what it quietly takes away.

    The tool tuples are asserted VERBATIM rather than by membership: an
    assertion that `Bash` is still disallowed passes just as well against a
    tuple that has grown `Bash` an allowed twin."""
    assert WRITE_ALLOWED_TOOLS == ("Read", "Grep", "Glob", "Edit", "Write")
    assert IMPLEMENT_DISALLOWED_TOOLS == (
        "NotebookEdit", "Bash", "Task", "Agent", "WebFetch", "WebSearch",
    )

    prompt, _ = capture_prompt(
        main_repo, worker_repo, make_task(), implement_directive()
    )

    assert "you may Read, Grep, Glob, Edit and Write" in prompt
    assert "You have no Bash access" in prompt
    assert "must never attempt to reach any path outside it" in prompt
    assert "committing is not your job" in prompt
    assert "Do not delegate to another agent" in prompt

    # Checked against the CONSTANT and the new section's fixed prose, not the
    # assembled prompt: the ground rules legitimately say "no Bash access", so
    # a prompt-level search for a tool name matches that sentence and proves
    # nothing about the new text — and an approved path could itself be named
    # `autoloop/Bash.py`, which is the task's business and not this rule's.
    for tool in IMPLEMENT_DISALLOWED_TOOLS:
        assert tool not in _ADVERSARIAL_SELF_TEST
        assert tool not in _approved_paths_section(())
        assert tool not in _approved_paths_section(("feature.py",))


def test_the_prompt_states_the_approved_paths_and_widens_no_path_set(
    main_repo, worker_repo
):
    """`_agent_prompt` now READS `Task.approved_paths` — to state the boundary
    — and that is the whole of its scope contact. Stating a scope is not
    granting one, so what this pins is the difference:

      * the rendered set is EXACTLY the task's own list, compared as a set of
        parsed lines rather than with `in`, which would pass against a
        rendering that had quietly added entries beside the real ones;
      * `TRACKER_PATHS` are absent. `effective_approved_paths()` would add six
        of them, `CLAUDE.md` included — repo-wide paths this task never
        declared, inside a sentence that says "fix the failures inside these
        files". The narrower list is the deliberate choice;
      * a path outside the set appears nowhere;
      * the Task itself is not mutated by being rendered.

    `TaskExecution.allowed_paths` is untouched here by construction: this
    module neither reads nor writes it, and the post-commit ownership check
    (`tasks.unauthorized_paths` against the execution record) is what actually
    enforces scope — unchanged by this task."""
    approved = ("feature.py", "autoloop/tests/")
    task = make_task(approved_paths=approved)

    prompt, _ = capture_prompt(main_repo, worker_repo, task, implement_directive())

    section = _approved_paths_section(task.approved_paths)
    rendered = {
        line.strip() for line in section.splitlines() if line.startswith("  ")
    }
    assert rendered == set(approved)

    for tracker in ("CLAUDE.md", "docs/TESTS.md", "docs/SUMMARY.md", "docs/SECURITY.md"):
        assert tracker not in section
    assert "zz/sentinel/only/here.py" not in prompt
    assert task.approved_paths == approved  # rendering mutates nothing

    # And the text says what it is: a statement of the loop's authorization.
    assert "it grants nothing and widens nothing" in prompt


def test_the_cleanup_instruction_survives_alongside_the_new_section():
    """Both optional sections at once. The new text is appended into the same
    `parts` list, and the ordering comment there says cleanup goes after the
    ground rules and BEFORE the feedback that usually asks for the removal —
    so this pins that adjacency rather than merely that both strings exist."""
    task = Task(id="t1", title="Add widget", description="Implement it.")
    prompt = _agent_prompt(task, "remove the residue you added", ("stray.py",))

    assert "ADVERSARIALLY TEST YOUR OWN CLAIM" in prompt
    assert "REMOVE-OUT-OF-SCOPE: <repository-relative path>" in prompt
    assert "does not authorize" in prompt
    assert prompt.index("stray.py") < prompt.index("remove the residue you added")
    # The scope section sits with the instruction it bounds, ahead of both —
    # and this task has no approved paths, so the recorded residue path below
    # is the only path in the prompt. A cleanup grant is not a scope grant.
    assert prompt.index("the loop has NO approved path list") < prompt.index("stray.py")


def test_the_enumeration_reaches_the_reviewer_and_is_never_parsed_as_data(
    main_repo, worker_repo
):
    """The second half of the claim — "the round's output carries that
    enumeration" — and the fail-open case in the same raw text.

    CARRIED: no extractor exists for this section and none should. The agent's
    whole output already rides `result.raw_text` -> `ExecutionOutcome.details`
    -> `TaskExecution.report_details`, which `packet._format_executor_report`
    renders, so the enumeration reaches the reviewer through plumbing this
    change does not touch.

    NOT PARSED: `_ASSUMPTION_RE` is `^[ \\t]*assumption:` case-insensitive and
    anchored per line, and an adversarial-case list is exactly the kind of
    line-per-item prose that could collide with it. If it did, cases the agent
    merely CONSIDERED would be promoted into `TaskExecution.assumptions` — the
    durable record a reviewer reads most closely, and the one place a
    fabricated entry does the most damage. The real `ASSUMPTION:` line sits
    among the cases so that `assumptions` is asserted as an exact tuple: an
    empty-tuple assertion would pass just as well against extraction that is
    broken outright."""
    raw = (
        "I implemented the widget.\n"
        "ASSUMPTION: read 'recent' as the last 30 days\n"
        "ADVERSARIAL CASES:\n"
        "  empty input list — handled in widget.py:build, pinned by test_empty\n"
        "  assumption of a non-null config — cannot arise, the caller validates\n"
        "  malformed row read back as a verdict — echo case, rejected in parse()\n"
    )
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}, raw_text=raw),
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "ok"
    assert "ADVERSARIAL CASES:" in outcome.details
    assert "pinned by test_empty" in outcome.details
    assert outcome.assumptions == ("read 'recent' as the last 30 days",)


def test_the_new_instruction_has_its_own_ceiling():
    """Mirrors the per-clause ceilings the contract instructions carry
    (`NEXT_WORK_PREFERENCE` <= 420, `AUDIT_VS_READY_PREFERENCE` <= 470).

    `_agent_prompt` carries NO pinned budget of its own and does not inherit
    `CONTRACT_INSTRUCTIONS`' 3,700: that ceiling is justified in its own
    docstring as a PER-TURN tax on text re-sent on every turn of a
    conversation, whereas this prompt is built once per round (line 658) for a
    fresh `claude -p`. So nothing was breached by adding this. The ceiling here
    is self-imposed for the same reason the contract's clauses carry one: this
    is the part of the prompt most likely to attract elaboration, and the
    rationale for it belongs in the source comment beside the constant, which
    costs nothing, rather than in the text, which is re-sent to every agent.

    Re-pointing the boundary at `approved_paths` (revision round 2) grew the
    instruction; it did not breach the ceiling, and the ceiling did not move to
    accommodate it. The section carries its own bound on its FIXED prose, on
    both branches — the path list itself is the task's and is not something
    this module can shorten."""
    assert len(_ADVERSARIAL_SELF_TEST) <= 1800
    assert len(_approved_paths_section(())) <= 600
    assert len(_approved_paths_section(("feature.py",))) <= 600


def test_assumption_lines_are_collected_from_the_agents_own_output(
    main_repo, worker_repo
):
    raw = (
        "I looked at the task.\n"
        "ASSUMPTION: read 'recent' as the last 30 days, not since the last release\n"
        "Then I wrote the code.\n"
        "  assumption:   kept the existing default rather than changing it\n"
    )
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}, raw_text=raw),
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "ok"
    assert outcome.assumptions == (
        "read 'recent' as the last 30 days, not since the last release",
        "kept the existing default rather than changing it",
    )


def test_prose_about_the_convention_is_not_harvested_as_an_assumption(
    main_repo, worker_repo
):
    """Anchored at the start of a line for exactly this: an agent that
    explains what it was told ("...write an ASSUMPTION: line when...") must
    not have its own instructions read back as disclosures — the reviewer
    would be shown assumptions nobody made."""
    raw = "I was told to write an ASSUMPTION: line for each choice, and I made none.\n"
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}, raw_text=raw),
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "ok"
    assert outcome.assumptions == ()


def test_quoted_or_bulleted_echoes_of_the_instruction_are_not_disclosures(
    main_repo, worker_repo
):
    """The tightening of `_ASSUMPTION_RE` (2026-08-16).

    The anchor alone stops prose that mentions the convention mid-sentence, but
    it does not stop an agent that QUOTES the instruction it was given —
    `> ASSUMPTION: <what you assumed...>` is verbatim from the prompt, and a
    bulleted restatement of the rule is the same thing with `-`. Either one
    would fabricate a deliberate choice the agent never made, in the section a
    reviewer is most likely to read on its own.

    Both directions are pinned in ONE raw text on purpose: `assumptions == ()`
    against the echoes alone passes just as well against extraction that is
    broken outright, so a real declaration sits among them and the assertion is
    an exact-tuple equality."""
    raw = (
        "The instructions told me:\n"
        "> ASSUMPTION: <what you assumed, and what you would have asked>\n"
        "which I read as a rule about disclosure. Restating it for myself:\n"
        "- ASSUMPTION: one line per choice\n"
        "* ASSUMPTION: naming the reading I did not take\n"
        "> - ASSUMPTION: never a summary of the work\n"
        "Here is the one choice I actually made:\n"
        "ASSUMPTION: read 'recent' as the last 30 days, not since the last release\n"
    )
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}, raw_text=raw),
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "ok"
    assert outcome.assumptions == (
        "read 'recent' as the last 30 days, not since the last release",
    )


def test_the_prompt_tells_the_agent_that_a_prefixed_line_is_not_collected(
    main_repo, worker_repo
):
    """The other half of the tightening. Refusing the bulleted form silently
    would trade a fabricated disclosure for a missed one; the instruction has
    to state the shape it actually accepts."""
    factory_runners = []

    def factory(root):
        runner = FakeAgentRunner(worker_repo=root, write_files={"feature.py": "x = 1\n"})
        factory_runners.append(runner)
        return runner

    executor = build_executor(main_repo, worker_repo, factory)
    executor.execute(implement_directive(), make_task())

    prompt = factory_runners[0].specs[0].prompt
    assert "must come FIRST on its line" in prompt
    assert "NOT collected" in prompt


def test_every_assumption_reaches_the_record_however_many_the_round_wrote(
    main_repo, worker_repo
):
    """No per-round count cap, deliberately (the caps were removed 2026-08-16).

    `TaskExecution.assumptions` is the DURABLE record and the only copy that
    survives the next round — `report_details`, where these lines also appear,
    is replaced every round. Dropping the twentieth-and-later lines here made
    them unrecoverable the moment round 2 committed, which is exactly the
    cross-round persistence the record exists to provide. The chat-message
    bound lives in `packet._format_assumptions` instead."""
    raw = "\n".join(f"ASSUMPTION: choice number {i}" for i in range(40))
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}, raw_text=raw),
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert len(outcome.assumptions) == 40
    assert outcome.assumptions[0] == "choice number 0"
    assert outcome.assumptions[-1] == "choice number 39"
    # And no overflow notice stands in for the entries that used to be cut.
    assert not any("dropped" in text for text in outcome.assumptions)


def test_a_long_assumption_reaches_the_record_in_full(main_repo, worker_repo):
    """Same rule per LINE. A 500-character cut here deleted the end of a
    sentence permanently; the packet shortens what it renders instead, and says
    where the whole line is."""
    body = "x" * 2_000
    raw = f"ASSUMPTION: {body}\n"
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}, raw_text=raw),
    )
    outcome = executor.execute(implement_directive(), make_task())

    [only] = outcome.assumptions
    assert only == body
    assert not only.endswith("…")


def test_a_failed_round_reports_no_assumptions(main_repo, worker_repo):
    """A failed round is never committed (`_dispatch_task_postcommit` returns
    on a non-ok status), so an assumption from it would describe code that is
    not in the candidate the reviewer is shown."""
    raw = "ASSUMPTION: assumed something on my way to failing\n"
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}, raw_text=raw),
        validation=(("ruff", "check", "."),),
        command_runner=fail_command,
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "error"
    assert outcome.assumptions == ()


# ---- 10: nothing written outside the worker repo ---------------------------


def test_writes_nothing_outside_worker_repo(main_repo, worker_repo, tmp_path):
    autoloop_dir = tmp_path / ".autoloop"
    autoloop_dir.mkdir()
    marker = autoloop_dir / "marker.json"
    marker.write_text("{}")
    before_main = sorted(str(p) for p in main_repo.rglob("*"))

    executor = build_executor(
        main_repo, worker_repo, make_agent_runner_factory(write_files={"feature.py": "x = 1\n"})
    )
    outcome = executor.execute(implement_directive(), make_task())
    assert outcome.status == "ok"

    after_main = sorted(str(p) for p in main_repo.rglob("*"))
    assert after_main == before_main
    assert GitGateway(main_repo, PolicyEngine(PolicyConfig())).dirty_paths() == set()
    assert marker.read_text() == "{}"
    assert [p.name for p in autoloop_dir.iterdir()] == ["marker.json"]


# ---- bonus: defense-in-depth refusals + constructor contract --------------


def test_audit_decision_is_refused(main_repo, worker_repo):
    executor = build_executor(main_repo, worker_repo, make_agent_runner_factory())
    outcome = executor.execute(Directive(decision=Decision.AUDIT, reason="r"), None)
    assert outcome.status == "error"
    assert "supports only" in outcome.summary


def test_none_task_is_refused(main_repo, worker_repo):
    executor = build_executor(main_repo, worker_repo, make_agent_runner_factory())
    outcome = executor.execute(implement_directive(), None)
    assert outcome.status == "error"


def test_worker_repo_root_for_requires_policy_together(main_repo):
    policy = PolicyEngine(PolicyConfig())
    git = GitGateway(main_repo, policy)
    with pytest.raises(ValueError):
        ImplementExecutor(
            git=git,
            agent_runner=FakeAgentRunner(),
            worker_repo_root_for=lambda task_id: main_repo,
            policy=None,
        )
    with pytest.raises(ValueError):
        ImplementExecutor(
            git=git,
            agent_runner=FakeAgentRunner(),
            worker_repo_root_for=None,
            policy=policy,
        )


# ---- per-task validation (the vacuous-validation bug) ----------------------


def _writing_stub(argv, **kwargs):
    (Path(kwargs["cwd"]) / "feature.py").write_text("x = 1\n")

    class Proc:
        returncode = 0
        stdout = json.dumps({"result": "done"})
        stderr = ""

    return Proc()


def test_task_declared_validation_overrides_the_configured_default(main_repo, worker_repo):
    """The configured default is ruff + the autoloop and root suites, none of
    which touch `lexy-app/backend`. Without a per-task override, rt-01's change
    would pass validation with NOTHING exercising it — including the test the
    agent just wrote.

    The declared command is compared through `effective_validation_command`:
    since val-01 (2026-08-06) `run_validation_commands` adds the flags a
    validation run needs (`-p no:cacheprovider` here — the declared command
    already asks for `-n auto`), so the literal that was declared is not the
    literal that runs. What this test pins is unchanged: the DECLARED command
    is what ran, and the configured default was not substituted for it."""
    from autoloop.validation import effective_validation_command

    (worker_repo / "lexy-app" / "backend").mkdir(parents=True, exist_ok=True)
    ran = []

    def command_runner(argv, cwd=None, **kw):
        ran.append((tuple(argv), str(cwd)))

        class Proc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        return Proc()

    executor = build_executor(
        main_repo, worker_repo,
        lambda root: implement_agent_runner(root, runner=_writing_stub),
        validation=(("ruff", "check", "."),),
        command_runner=command_runner,
    )
    task = Task(
        id="rt-01", title="admin-gate", description="d",
        validation=(("python3", "-m", "pytest", "-n", "auto", "-q"),),
        validation_cwd="lexy-app/backend",
    )
    outcome = executor.execute(implement_directive(), task)

    assert outcome.status == "ok"
    argvs = [a for a, _ in ran]
    declared = ("python3", "-m", "pytest", "-n", "auto", "-q")
    assert effective_validation_command(declared) in argvs
    assert ("ruff", "check", ".") not in argvs, "the configured default must be REPLACED"
    assert all(c.endswith("lexy-app/backend") for _, c in ran), (
        "must run from the declared cwd — `python -m` needs it on sys.path"
    )


def test_empty_task_validation_keeps_the_configured_default(main_repo, worker_repo):
    ran = []

    def command_runner(argv, cwd=None, **kw):
        ran.append(tuple(argv))

        class Proc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        return Proc()

    executor = build_executor(
        main_repo, worker_repo,
        lambda root: implement_agent_runner(root, runner=_writing_stub),
        validation=(("ruff", "check", "."),),
        command_runner=command_runner,
    )
    executor.execute(implement_directive(), make_task())
    assert ("ruff", "check", ".") in ran


def test_missing_validation_cwd_is_an_honest_error_not_a_silent_pass(main_repo, worker_repo):
    """A declared directory that does not exist must not fall back to the repo
    root and report success — that is the vacuous pass all over again."""
    def command_runner(argv, cwd=None, **kw):
        class Proc:
            returncode = 0
            stdout = "ok\n"
            stderr = ""

        return Proc()

    executor = build_executor(
        main_repo, worker_repo,
        lambda root: implement_agent_runner(root, runner=_writing_stub),
        validation=(("ruff", "check", "."),),
        command_runner=command_runner,
    )
    task = Task(
        id="t1", title="t", description="d",
        validation=(("ruff", "check", "."),),
        validation_cwd="does/not/exist",
    )
    outcome = executor.execute(implement_directive(), task)
    assert outcome.status == "error"
    assert "validation_cwd" in outcome.summary


# ---- prof-01: timing this executor can never change what it returns --------
#
# `Orchestrator._dispatch_task_postcommit` wraps THIS call in a `Stopwatch` —
# it is the loop's most expensive operation and, until prof-01, the only one
# with no recorded duration at all. The executor itself is deliberately
# unchanged by that: it neither knows nor cares that it is being timed. These
# two tests pin both directions of that boundary against the REAL executor
# rather than a double, because "the measurement is outside the operation" is
# only worth anything if the operation really is untouched.


class _ExplodingClock:
    def __call__(self):
        raise RuntimeError("the clock is on fire")


class FakeClock:
    """Returns each reading in turn, then repeats the last one forever."""

    def __init__(self, *readings):
        self.readings = list(readings)

    def __call__(self):
        if len(self.readings) > 1:
            return self.readings.pop(0)
        return self.readings[0]


def _timed_run(main_repo, worker_repo, marker, clock=None):
    """Run the real executor inside a stopwatch, exactly as the orchestrator
    does: construct, execute, stamp the completion payload."""
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": f"x = 1  # {marker}\n"}),
    )
    watch = Stopwatch(clock) if clock is not None else Stopwatch()
    outcome = executor.execute(implement_directive(), make_task())
    payload = watch.stamp({"status": outcome.status, "summary": outcome.summary})
    return outcome, payload


def test_a_broken_timing_path_leaves_the_executor_outcome_unchanged(main_repo, worker_repo):
    """A clock that raises on every read costs the measurement and nothing
    else: same outcome as an untimed run, and the payload simply lacks the
    key — which is the shape of every record written before prof-01."""
    baseline, _ = _timed_run(main_repo, worker_repo, "a")
    timed, payload = _timed_run(main_repo, worker_repo, "b", clock=_ExplodingClock())

    assert timed.status == "ok"
    assert timed == baseline
    assert DURATION_KEY not in payload
    assert payload == {"status": timed.status, "summary": timed.summary}


def test_a_working_timing_path_measures_without_touching_the_outcome(main_repo, worker_repo):
    baseline, _ = _timed_run(main_repo, worker_repo, "a")
    timed, payload = _timed_run(
        main_repo, worker_repo, "b", clock=FakeClock(500.0, 512.25)
    )

    assert timed == baseline
    assert payload[DURATION_KEY] == 12.25
    assert payload["status"] == "ok"
