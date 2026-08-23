"""The implementer's own validation call (impl-02): bound by the executor,
callable with nothing, capped per round, and reported from the loop's own
record rather than from anything the agent said.

The claim these pin: the write-capable agent can invoke the configured
validation commands against its own worker repo — with no command, no path, no
flag and no environment value supplied by the agent — and gets the result back
as text before it returns.

Sections 1–7 exercise the bound call and the round's reporting. **Section 8 is
the one that proves the claim**: it drives whole `execute()` rounds in which the
stand-in agent uses nothing but Write (to ask) and Read (to collect) — the two
tools a real implement subagent already has — and checks what the agent got, what
really launched, and what the round reported. Section 9 covers the states an
agent can leave the rendezvous in.

No `claude` CLI and no real validation binary is ever launched: the command
runner is a recording stub, which is also what makes "the agent supplied none
of these arguments" observable rather than asserted.
"""

import inspect
import threading
import time
from pathlib import Path

import pytest

from autoloop.audit.agents import AgentResult
from autoloop.git_gateway import GitGateway
from autoloop.implement_executor import (
    ADVISORY_PENDING_PREFIX,
    ADVISORY_REQUEST_FILE,
    ADVISORY_RESULT_FILE,
    ADVISORY_RESULT_PREFIX,
    ADVISORY_RESULT_TMP_FILE,
    ADVISORY_VALIDATION_MAX_CALLS,
    ADVISORY_VALIDATION_TIMEOUT_SECONDS,
    AdvisoryRendezvous,
    AdvisoryValidation,
    ImplementExecutor,
    _extract_assumptions,
    _extract_cleanup_requests,
    advisory_tool_descriptor,
    serve_advisory_tool_call,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import Task
from autoloop.validation import NOT_RUN

# Sibling test module, importable because pytest's prepend import mode puts this
# directory on `sys.path` — the same borrowing `test_test_selection.py` already
# does from this exact module.
from test_implement_executor import (
    FakeAgentRunner,
    implement_directive,
    make_agent_runner_factory,
    run_git,
)

RUFF = ("ruff", "check", ".")
SUITE = ("python3", "-m", "pytest", "autoloop/tests")


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


class RecordingRunner:
    """A `subprocess.run` stand-in that records exactly what it was asked to
    launch — the argv, the working directory, the environment and the timeout.

    Recording all four is the point: "the agent supplied no command, no path,
    no flag and no environment value" is checkable only against what really
    reached the launcher, never against what the caller meant to pass.

    `returncodes` is consumed one per call and the last value repeats, so a
    test can say "green then red" without a stateful closure.
    """

    def __init__(self, returncodes=(0,), stdout="All checks passed!\n", stderr=""):
        self.calls = []
        self._returncodes = list(returncodes) or [0]
        self._stdout = stdout
        self._stderr = stderr

    def __call__(self, argv, **kwargs):
        rc = self._returncodes[min(len(self.calls), len(self._returncodes) - 1)]
        out, err = self._stdout, self._stderr
        self.calls.append(
            {
                "argv": tuple(argv),
                "cwd": kwargs.get("cwd"),
                "env": kwargs.get("env"),
                "timeout": kwargs.get("timeout"),
            }
        )

        class Proc:
            returncode = rc
            stdout = out
            stderr = err

        return Proc()


class ExplodingRunner:
    def __init__(self):
        self.calls = 0

    def __call__(self, argv, **kwargs):
        self.calls += 1
        raise RuntimeError("the launcher fell over")


def make_service(cwd, commands=(RUFF,), runner=None, **kwargs):
    return AdvisoryValidation(
        commands=commands,
        cwd=cwd,
        command_runner=runner if runner is not None else RecordingRunner(),
        **kwargs,
    )


def make_task(task_id="t1", validation=(), validation_cwd=""):
    return Task(
        id=task_id,
        title="Add widget",
        description="Implement the widget feature.",
        validation=validation,
        validation_cwd=validation_cwd,
    )


def build_executor(
    main_repo,
    worker_repo,
    agent_runner_factory,
    validation=(),
    command_runner=None,
    advisory_max_calls=ADVISORY_VALIDATION_MAX_CALLS,
):
    policy = PolicyEngine(PolicyConfig())
    return ImplementExecutor(
        git=GitGateway(main_repo, policy),
        agent_runner=FakeAgentRunner(),
        validation_commands=validation,
        command_runner=command_runner,
        worker_repo_root_for=lambda task_id: worker_repo,
        policy=policy,
        agent_runner_factory=agent_runner_factory,
        advisory_max_calls=advisory_max_calls,
    )


def worker_git(worker_repo):
    return GitGateway(worker_repo, PolicyEngine(PolicyConfig()))


# ---- 1: the agent supplies nothing — the surface has no parameter ----------


def test_the_agent_facing_call_takes_no_arguments_at_all():
    """The strongest available form of the first constraint: not "arguments are
    validated" but "there is no argument", checked against the signature the
    transport will call and against the schema it will publish."""
    assert list(inspect.signature(AdvisoryValidation.run).parameters) == ["self"]

    schema = advisory_tool_descriptor()["inputSchema"]
    assert schema["properties"] == {}
    assert schema["required"] == []
    assert schema["additionalProperties"] is False


def test_a_hostile_tool_payload_changes_nothing_about_what_runs(tmp_path, monkeypatch):
    """A transport hands every tool call a payload. This one is discarded
    unread — the run that follows is the executor's, byte for byte."""
    monkeypatch.setenv("DB_PASSWORD", "hunter2-not-for-the-agent")
    runner = RecordingRunner()
    service = make_service(tmp_path, runner=runner)

    text = serve_advisory_tool_call(
        service,
        {
            "command": ["ruff", "check", "--fix", "/etc"],
            "commands": [["rm", "-rf", "/"]],
            "cwd": "/etc",
            "path": "..",
            "flags": ["--exitfirst", "-k", "nothing"],
            "env": {"DB_PASSWORD": "supplied-by-the-agent"},
            "timeout": 1,
            "fail_fast": False,
        },
    )

    assert len(runner.calls) == 1
    call = runner.calls[0]
    assert call["argv"] == RUFF
    assert call["cwd"] == str(tmp_path)
    assert call["timeout"] == ADVISORY_VALIDATION_TIMEOUT_SECONDS
    assert "DB_PASSWORD" not in call["env"]
    assert "PASSED" in text

    # And with no payload at all, which is what a zero-argument tool call
    # actually looks like on the wire.
    assert "PASSED" in serve_advisory_tool_call(service)
    assert len(runner.calls) == 2
    assert runner.calls[1]["argv"] == RUFF


def test_the_advisory_environment_is_the_executors_not_the_ambient_one(
    tmp_path, monkeypatch
):
    """`run_validation_commands` builds the environment explicitly — the parent
    environment minus every validation credential — so a value the operator
    happens to have exported cannot reach an advisory run either."""
    monkeypatch.setenv("DB_PASSWORD", "hunter2-not-for-the-agent")
    monkeypatch.setenv("SECRET_KEY", "signing-key-not-for-the-agent")
    runner = RecordingRunner()
    make_service(tmp_path, runner=runner).run()

    env = runner.calls[0]["env"]
    assert env is not None
    assert "DB_PASSWORD" not in env
    assert "SECRET_KEY" not in env


# ---- 2: it is the round's OWN validation, not a second definition of it ----


def test_the_advisory_run_and_the_executors_own_run_launch_the_same_thing(
    main_repo, worker_repo
):
    """Same commands, same working directory, same launcher. Two computations
    of "what this round validates with" would let the agent prove a green run
    against something the executor was never going to execute."""
    (worker_repo / "backend").mkdir()
    task = make_task(validation=(RUFF, SUITE), validation_cwd="backend")
    runner = RecordingRunner()
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}),
        validation=(("ruff", "check", "SOMETHING-ELSE"),),
        command_runner=runner,
    )

    outcome = executor.execute(implement_directive(), task)
    assert outcome.status == "ok"
    authoritative = [
        (call["argv"], call["cwd"]) for call in runner.calls
    ]

    executor._advisory_for(task, worker_git(worker_repo)).run()
    advisory = [(call["argv"], call["cwd"]) for call in runner.calls][len(authoritative):]

    assert advisory == authoritative
    assert authoritative[0][1] == str(worker_repo / "backend")


def test_the_result_is_text_naming_what_ran_and_how_it_went(tmp_path):
    green = make_service(tmp_path).run()
    assert isinstance(green, str)
    assert "PASSED" in green
    assert "ruff check .: PASS" in green

    red = make_service(tmp_path, runner=RecordingRunner(returncodes=(1,))).run()
    assert "FAILED" in red
    assert "ruff check .: FAIL" in red


# ---- 3: the cap — and NOT RUN is not a pass --------------------------------


def test_a_request_past_the_cap_executes_nothing_and_says_not_run(tmp_path):
    runner = RecordingRunner()
    service = make_service(tmp_path, runner=runner, max_calls=2)

    first, second, third = service.run(), service.run(), service.run()

    assert len(runner.calls) == 2, "the third request must not launch anything"
    assert "PASSED" in first and "PASSED" in second
    assert NOT_RUN in third
    assert "PASS" not in third.replace(NOT_RUN, "")
    assert service.runs == 2
    assert service.refused == 1
    assert service.requests == 3


def test_a_cap_of_zero_refuses_every_request_rather_than_running_one(tmp_path):
    runner = RecordingRunner()
    service = make_service(tmp_path, runner=runner, max_calls=0)
    assert NOT_RUN in service.run()
    assert runner.calls == []
    assert service.last_run_ok is None


def test_a_negative_cap_is_read_as_zero_not_as_unbounded(tmp_path):
    runner = RecordingRunner()
    service = make_service(tmp_path, runner=runner, max_calls=-5)
    assert NOT_RUN in service.run()
    assert runner.calls == []
    assert service.max_calls == 0


def test_the_default_cap_allows_the_fix_and_confirm_loop_and_stops_there(tmp_path):
    assert 1 <= ADVISORY_VALIDATION_MAX_CALLS <= 5
    runner = RecordingRunner()
    service = make_service(tmp_path, runner=runner)
    for _ in range(ADVISORY_VALIDATION_MAX_CALLS + 3):
        service.run()
    assert len(runner.calls) == ADVISORY_VALIDATION_MAX_CALLS


def test_the_cap_is_a_constructor_override_not_a_setting_nothing_reads(
    main_repo, worker_repo
):
    executor = build_executor(
        main_repo, worker_repo, make_agent_runner_factory(), advisory_max_calls=1
    )
    assert executor._advisory_for(make_task(), worker_git(worker_repo)).max_calls == 1


# ---- 4: every non-result answers honestly (the fail-open hunt) -------------


def test_an_empty_command_list_is_never_reported_as_a_pass(tmp_path):
    """`run_validation_commands` reports an empty list as PASSED — correct for
    the executor's own accounting, catastrophic as an answer to "did my change
    survive the suite?"."""
    runner = RecordingRunner()
    service = make_service(tmp_path, commands=(), runner=runner)
    text = service.run()

    assert NOT_RUN in text
    assert runner.calls == []
    assert service.last_run_ok is None
    assert service.runs == 0
    assert service.blocked == 1


def test_a_missing_working_directory_is_reported_not_silently_passed(tmp_path):
    runner = RecordingRunner()
    service = make_service(tmp_path / "not-there", runner=runner)
    text = service.run()

    assert NOT_RUN in text
    assert runner.calls == []
    assert service.last_run_ok is None
    assert service.blocked == 1


def test_a_launcher_that_explodes_is_a_failure_not_a_pass(tmp_path):
    """Never raises: this runs inside a transport serving an agent mid-turn,
    where an exception breaks the turn instead of reporting anything."""
    runner = ExplodingRunner()
    service = make_service(tmp_path, runner=runner)
    text = service.run()

    assert "RuntimeError" in text
    assert "FAILURE" in text
    assert service.last_run_ok is False
    assert runner.calls == 1


def test_the_last_run_flag_follows_the_real_result_not_the_first_one(tmp_path):
    """The note reports the LAST run, not the first and not the best. Both
    services are `expose()`d because that is what production does the moment a
    rendezvous is started for them — an unexposed service reports NOT OFFERED,
    which is a fact about the round rather than about the runs."""
    green_then_red = make_service(tmp_path, runner=RecordingRunner(returncodes=(0, 1)))
    green_then_red.expose()
    green_then_red.run()
    green_then_red.run()
    assert green_then_red.last_run_ok is False
    assert "FAILED." in green_then_red.note()
    assert "NOT OFFERED" not in green_then_red.note()

    red_then_green = make_service(tmp_path, runner=RecordingRunner(returncodes=(1, 0)))
    red_then_green.expose()
    red_then_green.run()
    red_then_green.run()
    assert red_then_green.last_run_ok is True
    assert "PASSED." in red_then_green.note()


def test_one_run_is_bounded_well_below_the_stall_detectors_own_silence_bound(tmp_path):
    """An advisory run writes no files, so to `stall.WorkerTreeProbe` it looks
    like silence. A run allowed to last as long as the stall bound could get
    the agent killed by the detector that exists to catch a wedge."""
    assert ADVISORY_VALIDATION_TIMEOUT_SECONDS < 1800
    runner = RecordingRunner()
    make_service(tmp_path, runner=runner).run()
    assert runner.calls[0]["timeout"] == ADVISORY_VALIDATION_TIMEOUT_SECONDS


def test_a_second_request_arriving_mid_run_is_already_counted(tmp_path):
    """The cap is committed BEFORE the run, not after it. A counter bumped
    afterwards would admit any number of overlapping runs — the exact hole a
    per-round budget exists to close — so this re-enters `run()` from inside
    the launcher, which is a request arriving while the first is still going."""
    seen = []

    class ReentrantRunner:
        def __init__(self):
            self.calls = 0

        def __call__(self, argv, **kwargs):
            self.calls += 1
            seen.append(service.run())

            class Proc:
                returncode = 0
                stdout = "All checks passed!\n"
                stderr = ""

            return Proc()

    runner = ReentrantRunner()
    service = make_service(tmp_path, runner=runner, max_calls=1)

    assert "PASSED" in service.run()
    assert runner.calls == 1, "the re-entrant request must not launch a second run"
    assert seen and NOT_RUN in seen[0]
    assert service.refused == 1


def test_a_malformed_command_record_says_not_run_instead_of_raising(tmp_path):
    runner = RecordingRunner()
    service = AdvisoryValidation(commands=None, cwd=tmp_path, command_runner=runner)

    assert NOT_RUN in service.run()
    assert runner.calls == []
    assert service.last_run_ok is None


def test_a_malformed_validation_record_cannot_turn_a_round_into_a_crash(
    main_repo, worker_repo
):
    """`_advisory_for` runs on EVERY round now, including the agent-failure
    path that returned before `task.validation` was ever read. A record
    `from_dict`'s coercion never produces must not convert that honest failure
    into an unhandled exception out of `execute()`."""
    executor = build_executor(
        main_repo, worker_repo, make_agent_runner_factory(fail=True), validation=(RUFF,)
    )
    # `Task` is a plain dataclass with no `__post_init__`, so a hand-built
    # record can hold a shape the loader would have coerced away.
    task = Task(id="t1", title="t", description="d", validation=None)

    outcome = executor.execute(implement_directive(), task)

    assert outcome.status == "error"
    assert "agent exploded" in outcome.summary
    assert "Agent self-validation:" in outcome.summary


# ---- 5: the executor still owns the verdict --------------------------------


def test_the_executors_own_run_still_happens_and_still_decides(main_repo, worker_repo):
    """A green advisory run neither skips, shortens nor stands in for the
    executor's own — which is free to fail the round straight afterwards."""
    runner = RecordingRunner(returncodes=(0, 1))
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}),
        validation=(RUFF,),
        command_runner=runner,
    )
    task = make_task()

    advisory_text = executor._advisory_for(task, worker_git(worker_repo)).run()
    assert "PASSED" in advisory_text

    outcome = executor.execute(implement_directive(), task)
    assert outcome.status == "error"
    assert "validation failed" in outcome.summary
    assert "FAIL" in outcome.validation
    assert len(runner.calls) == 2, "the executor ran the suite itself regardless"


def test_the_rounds_recorded_validation_is_never_the_advisory_text(
    main_repo, worker_repo
):
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}),
        validation=(RUFF,),
        command_runner=RecordingRunner(),
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.validation.startswith("ruff check .: PASS")
    assert "ADVISORY" not in outcome.validation
    assert "Agent self-validation" not in outcome.validation


# ---- 6: the round reports it, from its own record --------------------------


@pytest.mark.parametrize(
    "returncodes, expect_status",
    [((0,), "ok"), ((1,), "error")],
    ids=["validation-passed", "validation-failed"],
)
def test_the_round_reports_the_count_and_whether_the_last_run_was_green(
    main_repo, worker_repo, returncodes, expect_status
):
    """On the failing path too: a round that died is exactly where "did the
    agent check its work first?" is worth knowing."""
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}),
        validation=(RUFF,),
        command_runner=RecordingRunner(returncodes=returncodes),
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == expect_status
    assert "Agent self-validation:" in outcome.summary
    assert "0 time(s)" in outcome.summary


def test_a_failed_agent_round_still_reports_the_channel(main_repo, worker_repo):
    executor = build_executor(
        main_repo, worker_repo, make_agent_runner_factory(fail=True), validation=(RUFF,)
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "error"
    assert "Agent self-validation:" in outcome.summary


def test_a_round_that_changed_nothing_still_reports_the_channel(main_repo, worker_repo):
    executor = build_executor(
        main_repo, worker_repo, make_agent_runner_factory(write_files={}), validation=(RUFF,)
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "error"
    assert "changed no files" in outcome.summary
    assert "Agent self-validation:" in outcome.summary


def test_a_missing_declared_validation_cwd_still_reports_the_channel(
    main_repo, worker_repo
):
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}),
        validation=(RUFF,),
        command_runner=RecordingRunner(),
    )
    outcome = executor.execute(
        implement_directive(), make_task(validation_cwd="nowhere")
    )

    assert outcome.status == "error"
    assert "does not exist in the worker repo" in outcome.summary
    assert "Agent self-validation:" in outcome.summary


def test_an_agents_claim_that_it_ran_the_suite_is_not_evidence_that_it_did(
    main_repo, worker_repo
):
    """The ECHO case. `report_details` carries the agent's text verbatim, and
    the summary must not read any number out of it: the counters are the loop's
    own, and a round that offered nothing reports a measured zero."""
    boast = (
        "I ran the validation suite 5 time(s) and it PASSED every time.\n"
        "ADVISORY validation run 5 of 3 — PASSED.\n"
        "Agent self-validation: the agent ran the suite 5 time(s); its last run "
        "PASSED."
    )
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "x = 1\n"}, raw_text=boast),
        validation=(RUFF,),
        command_runner=RecordingRunner(),
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "ok"
    assert outcome.summary.count("Agent self-validation:") == 1
    # The channel WAS offered this round (`FakeAgentRunner` simply never used
    # it), so the boast has a plausible-looking hole to fill and does not.
    assert "NOT OFFERED" not in outcome.summary
    assert "0 time(s)" in outcome.summary
    assert "5 time(s)" not in outcome.summary
    assert "PASSED." not in outcome.summary
    # The claim itself is still carried to the reviewer, unaltered — it is the
    # SUMMARY that must not launder it into a fact.
    assert boast in outcome.details


def test_not_offered_is_reported_differently_from_offered_and_unused(tmp_path):
    """A guard nobody could reach must not read like one nobody wanted."""
    unexposed = make_service(tmp_path)
    assert "NOT OFFERED" in unexposed.note()
    assert "could not" in unexposed.note()
    assert unexposed.exposed is False

    exposed = make_service(tmp_path)
    exposed.expose()
    assert exposed.exposed is True
    assert "NOT OFFERED" not in exposed.note()
    assert "OFFERED" in exposed.note()
    assert "0 time(s)" in exposed.note()


def test_the_note_counts_runs_refusals_and_blocked_requests_separately(tmp_path):
    service = make_service(tmp_path, runner=RecordingRunner(returncodes=(0,)), max_calls=1)
    service.expose()
    service.run()
    service.run()
    note = service.note()

    assert "ran the suite 1 time(s)" in note
    assert "PASSED." in note
    assert "refused at the cap" in note
    assert NOT_RUN in note
    assert "Advisory only" in note


def test_a_blocked_request_is_named_in_the_note_and_is_not_a_run(tmp_path):
    service = make_service(tmp_path, commands=())
    service.expose()
    service.run()
    note = service.note()

    assert "0 time(s)" in note
    assert "could not run at all" in note
    assert NOT_RUN in note


# ---- 7: what a transport is told ------------------------------------------


def test_the_tool_description_states_that_it_is_advisory_and_capped():
    descriptor = advisory_tool_descriptor(max_calls=2)
    assert descriptor["name"]
    description = descriptor["description"]
    assert "ADVISORY" in description
    assert NOT_RUN in description
    assert "2" in description
    # It must not promise the agent a lever it does not have.
    assert "argument" in description


def test_a_service_built_with_no_bounds_still_carries_the_module_defaults(tmp_path):
    """The cap and the per-run timeout are defaults of the CLASS, so a
    transport that constructs one directly cannot end up unbounded by
    forgetting to pass them."""
    service = AdvisoryValidation(commands=(RUFF,), cwd=tmp_path, command_runner=RecordingRunner())
    assert service.max_calls == ADVISORY_VALIDATION_MAX_CALLS
    service.run()
    assert service.runs == 1


# ---- 8: END TO END — an agent with only Read and Write invokes it ----------
#
# This is the section that proves the task's one claim. Everything above tests
# the bound call; these tests drive a whole `execute()` round in which the
# "agent" uses NOTHING but the two tools a real implement agent has
# (`WRITE_ALLOWED_TOOLS` is Read/Grep/Glob/Edit/Write), and check what it got
# back, what really launched, and what the round reported afterwards.


class RendezvousAgentRunner:
    """A stand-in for the write-capable subagent that asks for validation the
    way a real one must: it WRITES the request file and READS the result file.

    No privileged access at all — it never touches `AdvisoryValidation`, never
    imports the executor's internals, and cannot name a command, a path, a flag
    or an environment value, because the only channel it has is a file whose
    content is discarded. If this class can get a real validation result, so can
    a `claude -p` agent holding the same two tools.

    It honours the stamped ordinal from the brief: the answer to request N is
    the one beginning `RESULT #N`. A runner that accepted any `RESULT` would
    silently pass on the stale answer from request N-1, which is the exact
    failure the stamp exists to prevent.
    """

    def __init__(
        self,
        worker_repo=None,
        asks=1,
        write_files=None,
        raw_text="done",
        wait_seconds=30.0,
        poll_seconds=0.005,
        fail=False,
    ):
        self.worker_repo = Path(worker_repo) if worker_repo is not None else None
        self.asks = asks
        self.write_files = write_files or {}
        self.raw_text = raw_text
        self.wait_seconds = wait_seconds
        self.poll_seconds = poll_seconds
        self.fail = fail
        self.specs = []
        self.answers = []

    def run(self, spec):
        self.specs.append(spec)
        for ordinal in range(1, self.asks + 1):
            self.answers.append(self._ask(ordinal))
        for rel, content in self.write_files.items():
            path = self.worker_repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        if self.fail:
            return AgentResult(
                domain=spec.domain,
                raw_text=self.raw_text,
                returncode=1,
                duration_seconds=0.1,
                command=("claude",),
                error="agent exploded",
            )
        return AgentResult(
            domain=spec.domain,
            raw_text=self.raw_text,
            returncode=0,
            duration_seconds=0.1,
            command=("claude",),
        )

    def _ask(self, ordinal):
        (self.worker_repo / ADVISORY_REQUEST_FILE).write_text("please run the suite\n")
        marker = f"{ADVISORY_RESULT_PREFIX} #{ordinal}"
        deadline = time.monotonic() + self.wait_seconds
        while time.monotonic() < deadline:
            try:
                text = (self.worker_repo / ADVISORY_RESULT_FILE).read_text()
            except OSError:
                text = ""
            if text.startswith(marker):
                return text
            time.sleep(self.poll_seconds)
        return f"NOTHING ANSWERED: no {marker} appeared within {self.wait_seconds}s"


def make_rendezvous_factory(**kwargs):
    def factory(root):
        return RendezvousAgentRunner(worker_repo=root, **kwargs)

    return factory


def test_an_agent_with_only_read_and_write_really_runs_the_configured_suite(
    main_repo, worker_repo
):
    """THE CLAIM. One `execute()` round: the agent asks with a Write, collects
    with a Read, and what actually launched is the executor's own command in the
    executor's own directory — nothing the agent named."""
    runner = RecordingRunner()
    agent = None

    def factory(root):
        nonlocal agent
        agent = RendezvousAgentRunner(
            worker_repo=root, asks=1, write_files={"feature.py": "x = 1\n"}
        )
        return agent

    executor = build_executor(
        main_repo, worker_repo, factory, validation=(RUFF,), command_runner=runner
    )
    outcome = executor.execute(implement_directive(), make_task())

    # The agent got a real result, as text, before it returned.
    assert agent.answers[0].startswith(f"{ADVISORY_RESULT_PREFIX} #1")
    assert "PASSED" in agent.answers[0]
    assert "ruff check .: PASS" in agent.answers[0]
    # Two launches: the agent's advisory run, then the executor's own.
    assert [call["argv"] for call in runner.calls] == [RUFF, RUFF]
    assert {call["cwd"] for call in runner.calls} == {str(worker_repo)}
    # The round is green on the executor's own run, and says what the agent did.
    assert outcome.status == "ok"
    assert "ran the suite 1 time(s)" in outcome.summary
    assert "PASSED." in outcome.summary
    assert outcome.validation.startswith("ruff check .: PASS")
    # The brief is what made this reachable.
    assert ADVISORY_REQUEST_FILE in agent.specs[0].prompt
    assert ADVISORY_RESULT_FILE in agent.specs[0].prompt


def test_the_agent_can_see_a_failure_fix_it_and_confirm_within_one_round(
    main_repo, worker_repo
):
    """The loop this whole task exists to close: red, fix, green — all inside
    the agent's own window, before anything is paid for."""
    runner = RecordingRunner(returncodes=(1, 0, 0), stdout="", stderr="boom\n")
    agent = None

    def factory(root):
        nonlocal agent
        agent = RendezvousAgentRunner(
            worker_repo=root, asks=2, write_files={"feature.py": "x = 1\n"}
        )
        return agent

    executor = build_executor(
        main_repo, worker_repo, factory, validation=(RUFF,), command_runner=runner
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert agent.answers[0].startswith(f"{ADVISORY_RESULT_PREFIX} #1")
    assert "FAILED" in agent.answers[0]
    assert agent.answers[1].startswith(f"{ADVISORY_RESULT_PREFIX} #2")
    assert "PASSED" in agent.answers[1]
    assert len(runner.calls) == 3, "two advisory runs and the executor's own"
    assert outcome.status == "ok"
    assert "ran the suite 2 time(s)" in outcome.summary
    assert "PASSED." in outcome.summary


def test_the_second_answer_cannot_be_read_as_the_first_ones_leftovers(
    main_repo, worker_repo
):
    """The agent has no delete tool, so between its second request and the
    executor taking it the result file still holds the FIRST answer. The stamp
    is what stops a green answer about an older tree being read as an answer
    about this one."""
    runner = RecordingRunner(returncodes=(0, 1))
    agent = None

    def factory(root):
        nonlocal agent
        agent = RendezvousAgentRunner(
            worker_repo=root, asks=2, write_files={"feature.py": "x = 1\n"}
        )
        return agent

    executor = build_executor(
        main_repo, worker_repo, factory, validation=(RUFF,), command_runner=runner
    )
    executor.execute(implement_directive(), make_task())

    assert "PASSED" in agent.answers[0]
    # Would be the stale PASS if the protocol had no ordinal.
    assert agent.answers[1].startswith(f"{ADVISORY_RESULT_PREFIX} #2")
    assert "FAILED" in agent.answers[1]


def test_the_cap_holds_end_to_end_and_the_agent_is_told_it_is_not_a_pass(
    main_repo, worker_repo
):
    runner = RecordingRunner()
    agent = None

    def factory(root):
        nonlocal agent
        agent = RendezvousAgentRunner(
            worker_repo=root, asks=2, write_files={"feature.py": "x = 1\n"}
        )
        return agent

    executor = build_executor(
        main_repo,
        worker_repo,
        factory,
        validation=(RUFF,),
        command_runner=runner,
        advisory_max_calls=1,
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert "PASSED" in agent.answers[0]
    assert NOT_RUN in agent.answers[1]
    assert "PASSED" not in agent.answers[1]
    assert len(runner.calls) == 2, "one advisory run plus the executor's own"
    assert "refused at the cap" in outcome.summary


def test_the_channel_leaves_nothing_behind_for_the_round_to_commit(
    main_repo, worker_repo
):
    """Neither rendezvous path is inside any task's approved scope, so a
    survivor is an out-of-scope write on the record and a parked candidate."""
    executor = build_executor(
        main_repo,
        worker_repo,
        make_rendezvous_factory(asks=2, write_files={"feature.py": "x = 1\n"}),
        validation=(RUFF,),
        command_runner=RecordingRunner(),
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.changed_paths == ("feature.py",)
    assert not (worker_repo / ADVISORY_REQUEST_FILE).exists()
    assert not (worker_repo / ADVISORY_RESULT_FILE).exists()
    assert not (worker_repo / ADVISORY_RESULT_TMP_FILE).exists()


def test_a_failed_agent_leaves_no_rendezvous_residue_either(main_repo, worker_repo):
    """The failure branch reads the tree BEFORE the success branch does
    (`_partial_work`), so the sweep has to be around the agent call itself."""
    executor = build_executor(
        main_repo,
        worker_repo,
        make_rendezvous_factory(asks=1, write_files={"feature.py": "x = 1\n"}, fail=True),
        validation=(RUFF,),
        command_runner=RecordingRunner(),
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "error"
    assert outcome.changed_paths == ("feature.py",)
    assert not (worker_repo / ADVISORY_REQUEST_FILE).exists()
    assert not (worker_repo / ADVISORY_RESULT_FILE).exists()


def test_a_round_with_nothing_to_run_offers_no_channel_and_still_sweeps(
    main_repo, worker_repo
):
    """A brief describing a channel that could only ever answer `NOT RUN` would
    spend the agent's turns for nothing — so it is not offered. The sweep runs
    anyway, which is also what clears residue left by a killed round."""
    agent = None

    def factory(root):
        nonlocal agent
        agent = RendezvousAgentRunner(
            worker_repo=root,
            asks=1,
            write_files={"feature.py": "x = 1\n"},
            wait_seconds=0.2,
        )
        return agent

    executor = build_executor(
        main_repo, worker_repo, factory, validation=(), command_runner=RecordingRunner()
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert ADVISORY_REQUEST_FILE not in agent.specs[0].prompt
    assert agent.answers[0].startswith("NOTHING ANSWERED")
    assert "NOT OFFERED" in outcome.summary
    # The agent wrote the request file anyway; the round must not carry it.
    assert outcome.changed_paths == ("feature.py",)
    assert not (worker_repo / ADVISORY_REQUEST_FILE).exists()


# ---- 9: the rendezvous itself — the states an agent can leave it in --------


def make_rendezvous(worker_repo, commands=(RUFF,), runner=None, **kwargs):
    service = make_service(worker_repo, commands=commands, runner=runner, **kwargs)
    return service, AdvisoryRendezvous(service, worker_repo, poll_seconds=0.01)


def test_a_hostile_request_file_is_still_a_zero_argument_request(worker_repo):
    """The file's CONTENT is the only thing an agent controls here, and it is
    read only to be discarded — so the most hostile payload available changes
    nothing about what launches."""
    runner = RecordingRunner()
    service, rendezvous = make_rendezvous(worker_repo, runner=runner)
    (worker_repo / ADVISORY_REQUEST_FILE).write_text(
        '{"command": ["rm", "-rf", "/"], "cwd": "/etc", "flags": ["--fix"],\n'
        ' "env": {"DB_PASSWORD": "supplied-by-the-agent"}}\n'
    )

    assert rendezvous.serve_once() is True

    assert [call["argv"] for call in runner.calls] == [RUFF]
    assert runner.calls[0]["cwd"] == str(worker_repo)
    assert "DB_PASSWORD" not in runner.calls[0]["env"]
    assert service.runs == 1
    rendezvous.stop()


def test_no_request_means_no_run_and_no_files(worker_repo):
    runner = RecordingRunner()
    service, rendezvous = make_rendezvous(worker_repo, runner=runner)

    assert rendezvous.serve_once() is False

    assert runner.calls == []
    assert service.requests == 0
    assert not (worker_repo / ADVISORY_RESULT_FILE).exists()


def test_a_directory_at_the_request_path_is_swept_not_left_behind(worker_repo):
    """`Write` creates parent directories, so an agent writing
    `<request>/note.txt` leaves a DIRECTORY where the request belongs. A sweep
    that gave up there would leave residue outside every task's approved
    paths."""
    nested = worker_repo / ADVISORY_REQUEST_FILE / "note.txt"
    nested.parent.mkdir(parents=True)
    nested.write_text("please run the suite")
    runner = RecordingRunner()
    service, rendezvous = make_rendezvous(worker_repo, runner=runner)

    assert rendezvous.serve_once() is True
    rendezvous.stop()

    assert service.runs == 1
    assert not (worker_repo / ADVISORY_REQUEST_FILE).exists()
    assert not (worker_repo / ADVISORY_RESULT_FILE).exists()


def test_a_symlinked_request_costs_the_link_and_never_its_target(tmp_path, worker_repo):
    secret = tmp_path / "outside.txt"
    secret.write_text("not the agent's to move")
    (worker_repo / ADVISORY_REQUEST_FILE).symlink_to(secret)
    service, rendezvous = make_rendezvous(worker_repo)

    assert rendezvous.serve_once() is True
    rendezvous.stop()

    assert service.runs == 1
    assert not (worker_repo / ADVISORY_REQUEST_FILE).is_symlink()
    assert secret.read_text() == "not the agent's to move"


def test_stale_files_from_a_killed_round_are_swept_before_the_agent_starts(
    worker_repo,
):
    (worker_repo / ADVISORY_RESULT_FILE).write_text("RESULT #1 — PASSED (last round)\n")
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("from a round that died\n")
    _, rendezvous = make_rendezvous(worker_repo)

    rendezvous.start()
    try:
        assert not (worker_repo / ADVISORY_RESULT_FILE).exists()
        assert not (worker_repo / ADVISORY_REQUEST_FILE).exists()
    finally:
        rendezvous.stop()


def test_stop_is_safe_and_still_sweeps_when_start_never_ran(worker_repo):
    (worker_repo / ADVISORY_RESULT_FILE).write_text("left over\n")
    service, rendezvous = make_rendezvous(worker_repo)

    rendezvous.stop()

    assert not (worker_repo / ADVISORY_RESULT_FILE).exists()
    assert service.exposed is False


def test_a_request_that_lands_after_the_round_ends_is_not_served(worker_repo):
    """The window between the agent returning and the tree being read. A run
    started there would publish its answer after the sweep — residue outside
    every approved path, on a round that is already over."""
    runner = RecordingRunner()
    service, rendezvous = make_rendezvous(worker_repo, runner=runner)
    rendezvous.start()
    rendezvous.stop()

    (worker_repo / ADVISORY_REQUEST_FILE).write_text("too late\n")
    assert rendezvous.serve_once() is False

    assert runner.calls == []
    assert service.requests == 0
    # The one file the late request did create is not this channel's to remove
    # after the round — the executor's own sweep already ran, and the request
    # was written afterwards, so it is reported as a change like any other.


def test_an_answer_that_finishes_after_the_round_ends_is_never_written(worker_repo):
    """`stop()` waits for a run in flight, but only for a bounded time — and
    `run_validation_commands` times out PER COMMAND, so a long list can outlast
    the wait. Past it the watcher is abandoned, and it must publish NOTHING:
    an answer landing after the sweep is residue outside every approved path,
    on a round that is already over."""
    release = threading.Event()
    started = threading.Event()

    def blocking_runner(argv, **kwargs):
        started.set()
        release.wait(10)

        class Proc:
            returncode = 0
            stdout = "All checks passed!\n"
            stderr = ""

        return Proc()

    service = make_service(worker_repo, runner=blocking_runner)
    rendezvous = AdvisoryRendezvous(
        service, worker_repo, poll_seconds=0.01, join_timeout=0.05
    )
    # After `start()`, never before it: `start()` sweeps stale files first, so
    # a request written ahead of it is (correctly) thrown away.
    rendezvous.start()
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("go\n")
    assert started.wait(5), "the advisory run must have begun"

    rendezvous.stop()
    assert not (worker_repo / ADVISORY_RESULT_FILE).exists()

    release.set()
    deadline = time.monotonic() + 5
    while service.runs == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service.runs == 1, "the abandoned run did complete"
    time.sleep(0.05)
    assert not (worker_repo / ADVISORY_RESULT_FILE).exists()
    assert not (worker_repo / ADVISORY_RESULT_TMP_FILE).exists()
    assert not (worker_repo / ADVISORY_REQUEST_FILE).exists()


def test_starting_the_rendezvous_is_what_marks_the_round_as_having_offered_it(
    worker_repo,
):
    service, rendezvous = make_rendezvous(worker_repo)
    assert service.exposed is False

    rendezvous.start()
    try:
        assert service.exposed is True
        assert "NOT OFFERED" not in service.note()
    finally:
        rendezvous.stop()


def test_the_brief_tells_the_agent_the_paths_the_executor_actually_watches(
    worker_repo,
):
    service, rendezvous = make_rendezvous(worker_repo, max_calls=2)
    brief = rendezvous.brief()

    assert ADVISORY_REQUEST_FILE in brief
    assert ADVISORY_RESULT_FILE in brief
    assert ADVISORY_PENDING_PREFIX in brief
    assert ADVISORY_RESULT_PREFIX in brief
    # The descriptor is the source of the WHAT, so a tool transport wired later
    # advertises the same sentences the agent has been reading all along.
    assert advisory_tool_descriptor(2)["description"] in brief
    assert "ADVISORY" in brief
    assert NOT_RUN in brief
    # It must not read as a grant of the shell the ground rules deny.
    assert "no shell" in brief
    assert service.max_calls == 2


def test_the_brief_cannot_be_echoed_back_as_a_disclosure_or_a_deletion(worker_repo):
    """Same property `_scope_instruction` and `_authoring_rules` are held to: an
    agent quoting its whole prompt back must not forge an `ASSUMPTION:` line or
    a `REMOVE-OUT-OF-SCOPE:` request."""
    _, rendezvous = make_rendezvous(worker_repo)
    brief = rendezvous.brief()

    assert _extract_assumptions(brief) == ()
    assert _extract_cleanup_requests(brief) == ()


def test_the_pending_marker_never_reads_as_a_finished_result(worker_repo):
    """A blocking run and a finished one must be distinguishable by the agent's
    stated test — first word — or it will act on an answer that does not exist
    yet."""
    slow = threading.Event()
    seen = []

    def blocking_runner(argv, **kwargs):
        seen.append((worker_repo / ADVISORY_RESULT_FILE).read_text())
        slow.set()

        class Proc:
            returncode = 0
            stdout = "All checks passed!\n"
            stderr = ""

        return Proc()

    _, rendezvous = make_rendezvous(worker_repo, runner=blocking_runner)
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("go\n")
    rendezvous.serve_once()
    rendezvous.stop()

    assert slow.is_set()
    assert seen[0].startswith(f"{ADVISORY_PENDING_PREFIX} #1")
    assert not seen[0].startswith(ADVISORY_RESULT_PREFIX)
