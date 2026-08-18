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
from autoloop.implement_executor import ImplementExecutor, implement_agent_runner
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import Task


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


def make_task(task_id="t1"):
    return Task(id=task_id, title="Add widget", description="Implement the widget feature.")


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
