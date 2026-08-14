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
