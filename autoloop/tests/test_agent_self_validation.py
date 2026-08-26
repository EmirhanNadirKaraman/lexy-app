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

from autoloop import implement_executor
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
    ADVISORY_ZERO_CALL_RETURNS,
    AdvisoryRendezvous,
    AdvisoryValidation,
    ImplementExecutor,
    _combined_report,
    _extract_assumptions,
    _extract_cleanup_requests,
    _extract_delete_requests,
    _zero_call_return_instruction,
    advisory_tool_descriptor,
    serve_advisory_tool_call,
)
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import EXECUTION_ABORTED
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
    advisory_zero_call_returns=ADVISORY_ZERO_CALL_RETURNS,
    abort_file=None,
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
        advisory_zero_call_returns=advisory_zero_call_returns,
        # None (the production default is a real path) means "no abort
        # capability", which is what every test here wants except the one that
        # checks an abort outranks the withhold.
        abort_file=abort_file,
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
        # `FakeAgentRunner` cannot ask, so since the 2026-08-27 revision this
        # round would be WITHHELD before the executor's own run and there would
        # be no authoritative launch to compare against. Pinned off: §10a is
        # where the withhold is graded, and this test is about the two runs
        # being one computation. Same reason at every `advisory_zero_call_
        # returns=0` below.
        advisory_zero_call_returns=0,
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
        # The hand-back/withhold is pinned off here: the advisory run below is
        # made against a SEPARATE service, so the round's own record would still
        # read zero and the round would never reach the run this test is about.
        advisory_zero_call_returns=0,
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
        # Pinned off: a withheld round records `validation = "not run"`, which is
        # a true statement about a round that never reached its own run — and not
        # the recorded-verdict question this test asks.
        advisory_zero_call_returns=0,
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
        # Pinned off so the parametrized status really comes from the executor's
        # own run. With the withhold live, BOTH cases would end `error` for the
        # zero requests and this would grade nothing.
        advisory_zero_call_returns=0,
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
    """Deliberately NOT pinned: this round makes zero advisory requests too, and
    the point since the 2026-08-27 revision is that "changed no files" is the
    MORE fundamental refusal and is still the one reported. The withhold is
    checked later, and a round that produced nothing never reaches it."""
    executor = build_executor(
        main_repo, worker_repo, make_agent_runner_factory(write_files={}), validation=(RUFF,)
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "error"
    assert "changed no files" in outcome.summary
    assert "WITHHELD" not in outcome.summary, "the more fundamental refusal was swallowed"
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
    # THE SHARPEST ordering case, and also not pinned. An advisory run in this
    # round could only ever have answered `NOT RUN` naming that directory, so
    # withholding it for making no request would refuse the round for failing to
    # obtain evidence it could not obtain — and would tell the reviewer "it never
    # ran the suite" about a round whose real problem is its own configuration.
    assert "WITHHELD" not in outcome.summary
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
        # Pinned off so this stays a test about the SUMMARY's provenance on an
        # ordinary forwarded round. The echo case against the hand-back and the
        # withhold is `test_a_handback_cannot_be_talked_out_of` in §10a, which
        # runs at the real allowance.
        advisory_zero_call_returns=0,
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


# ---- 10: a round that never ran the suite, and an ask nobody answered ------
#
# advis-01 (2026-08-26; withhold added in the 2026-08-27 revision). Two
# behaviours over the one contract above:
#
#   1. a report that made ZERO advisory requests is handed BACK to the agent
#      rather than forwarded to the reviewer — bounded, because a refusal that
#      can loop is strictly worse than the park it replaces — and if the record
#      STILL shows zero when the allowance is spent, the round is WITHHELD:
#      `status="error"`, no commit, no candidate, no packet;
#   2. a request that reached `PENDING #n` and never became `RESULT #n` is
#      reported as UNANSWERED, distinct from a run that completed and FAILED.
#
# Everything below derives from the executor's own counters. The agent's prose is
# never an input, which is what section 6's echo test already pins for the older
# half of `note()` and what `test_a_handback_cannot_be_talked_out_of` pins here.


#: The first line of the hand-back section, computed from the production
#: renderer rather than copied — a literal would keep passing after the section
#: it is supposed to detect had been reworded out from under it.
HANDBACK_MARK = _zero_call_return_instruction(1, True).splitlines()[0]


def _proc(returncode=0, stdout="All checks passed!\n", stderr=""):
    class Proc:
        pass

    Proc.returncode = returncode
    Proc.stdout = stdout
    Proc.stderr = stderr
    return Proc


class ForgetfulAgentRunner(RendezvousAgentRunner):
    """An agent that forgets to run the suite until it is handed the round back.

    The FIRST invocation asks nothing at all — which is exactly what the 18
    never-asked rounds looked like — and every invocation after it asks once,
    through the same Write/Read rendezvous the parent uses. Nothing here reads a
    counter or an executor internal: the only thing it keys on is whether the
    prompt it was handed carries the hand-back section.
    """

    def __init__(self, *args, fail_when_handed_back=False, reports=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_when_handed_back = fail_when_handed_back
        self.reports = list(reports or [])
        self.invocations = 0
        self.handed_back = 0

    def run(self, spec):
        self.invocations += 1
        back = HANDBACK_MARK in spec.prompt
        if back:
            self.handed_back += 1
            self.fail = self.fail_when_handed_back
        # A hand-back that is going to FAIL asks nothing, so that test isolates
        # the failure path instead of also moving the run counters.
        self.asks = 1 if back and not self.fail_when_handed_back else 0
        if self.reports:
            self.raw_text = self.reports[min(self.invocations - 1, len(self.reports) - 1)]
        return super().run(spec)


def make_forgetful_factory(**kwargs):
    holder = {}

    def factory(root):
        holder["agent"] = ForgetfulAgentRunner(worker_repo=root, **kwargs)
        return holder["agent"]

    return factory, holder


# ---- 10a: behaviour 1 — the hand-back, and its bound -----------------------


def test_a_round_that_never_ran_the_suite_is_handed_back_to_the_agent(
    main_repo, worker_repo
):
    """THE CLAIM's first half. A report with zero advisory requests does not
    reach the reviewer as an ordinary candidate: the agent is re-invoked, and on
    that second invocation it really does run the configured suite."""
    runner = RecordingRunner()
    factory, holder = make_forgetful_factory(write_files={"feature.py": "x = 1\n"})
    executor = build_executor(
        main_repo, worker_repo, factory, validation=(RUFF,), command_runner=runner
    )

    outcome = executor.execute(implement_directive(), make_task())
    agent = holder["agent"]

    assert agent.invocations == 2, "the zero-request report was forwarded unchecked"
    assert agent.handed_back == 1
    assert HANDBACK_MARK not in agent.specs[0].prompt, "invocation 1 is an ordinary one"
    assert HANDBACK_MARK in agent.specs[1].prompt
    # It really ran, through the same file rendezvous — two launches, the agent's
    # and then the executor's own.
    assert agent.answers[0].startswith(f"{ADVISORY_RESULT_PREFIX} #1")
    assert [call["argv"] for call in runner.calls] == [RUFF, RUFF]
    assert outcome.status == "ok"
    assert "ran the suite 1 time(s)" in outcome.summary
    assert "handed this round back to the agent 1 time(s)" in outcome.summary


def test_the_hand_back_is_bounded_and_a_stubborn_round_is_withheld_from_review(
    main_repo, worker_repo
):
    """THE HARD PART, and the revision's claim in one round.

    An agent that never asks however often it is handed the round back must
    still END the round — a refusal that can loop would spend the whole round
    re-invoking and produce nothing, which is worse than the park it replaces.
    And when the allowance is spent with the record still at zero, the round
    does NOT go on as an ordinary candidate: it comes back `status="error"`, so
    `orchestrator._dispatch_task_postcommit` returns at its non-ok test — before
    the commit, before the packet — and there is nothing for a reviewer to
    approve. That half is proved end to end in `test_postcommit_flow.py::
    test_a_round_that_never_ran_the_suite_produces_no_candidate_to_review`.
    """
    runner = RecordingRunner()
    captured = []

    def factory(root):
        agent = FakeAgentRunner(worker_repo=root, write_files={"feature.py": "x = 1\n"})
        captured.append(agent)
        return agent

    executor = build_executor(
        main_repo, worker_repo, factory, validation=(RUFF,), command_runner=runner
    )
    outcome = executor.execute(implement_directive(), make_task())

    agent = captured[0]
    assert agent.specs, "the agent ran at least once"
    # THE BOUND: one ordinary invocation plus the allowance, and not one more,
    # whatever the agent does.
    assert len(agent.specs) == 1 + ADVISORY_ZERO_CALL_RETURNS
    assert outcome.status == "error", "a zero-request round was forwarded anyway"
    assert "WITHHELD from review" in outcome.summary
    # ...and it must not read as a red suite, which is the misreport behaviour 2
    # exists to stop. Nothing ran at all — no advisory run, and the executor's
    # own run is never reached.
    assert "validation failed" not in outcome.summary
    assert outcome.validation == "not run"
    assert runner.calls == [], "a withheld round still paid for a validation run"
    # The measured zero and the spent hand-back are both on the record.
    assert "0 time(s)" in outcome.summary
    assert "handed this round back" in outcome.summary
    # THE ATTEMPT BUDGET. An empty `fault_kind` is what routes this to
    # `ATTEMPT_TASK`/`executor_reported_failure` in the orchestrator, so a task
    # whose agent keeps skipping the suite is bounded ACROSS rounds by the
    # attempt ceiling that already exists. Naming a fault would spend the fault
    # budget instead and let it refuse forever.
    assert not outcome.fault_kind
    # The work is still reported even though it is not committed — the reviewer
    # of a withheld round gets the same evidence every other uncommitted round
    # carries.
    assert outcome.changed_paths == ("feature.py",)
    assert "feature.py" in outcome.summary
    assert outcome.assumptions == ()


@pytest.mark.parametrize("allowance", [0, -3], ids=["zero", "negative"])
def test_an_allowance_of_zero_or_less_disables_both_halves(
    main_repo, worker_repo, allowance
):
    """ONE KNOB, ONE MEANING. The allowance gates the hand-back AND the
    withhold, so zero means "this executor does not enforce the ask" — not
    "refuse the round without ever telling the agent to run the suite", which
    would be punishment without notice. A negative reads as zero, never as
    unbounded.

    This is the only way either half switches off, which is why the shipped
    value is pinned by the test below."""
    captured = []

    def factory(root):
        agent = FakeAgentRunner(worker_repo=root, write_files={"feature.py": "x = 1\n"})
        captured.append(agent)
        return agent

    executor = build_executor(
        main_repo,
        worker_repo,
        factory,
        validation=(RUFF,),
        command_runner=RecordingRunner(),
        advisory_zero_call_returns=allowance,
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert len(captured[0].specs) == 1
    assert "handed this round back" not in outcome.summary
    assert "WITHHELD" not in outcome.summary
    assert outcome.status == "ok"


def test_the_shipped_allowance_enforces_the_contract_and_no_operator_can_zero_it():
    """The guard the test above could otherwise be read as switching off.

    Two facts, and both are needed. The shipped allowance is at least one, so a
    production round always hands back before it withholds; and the knob is not
    reachable from configuration — `cli._build_executor` is the only production
    construction of `ImplementExecutor` and passes neither advisory argument, so
    no `config.toml` can leave the loop sitting at zero. The only callers that
    pass anything else are tests whose subject is something else entirely."""
    assert ADVISORY_ZERO_CALL_RETURNS >= 1
    cli_source = Path(implement_executor.__file__).with_name("cli.py").read_text(
        encoding="utf-8"
    )
    assert "advisory_zero_call_returns" not in cli_source, (
        "the allowance became configurable — re-read `ADVISORY_ZERO_CALL_RETURNS`, "
        "because an operator can now switch both halves of the contract off"
    )


def test_a_round_whose_agent_used_the_channel_is_not_handed_back(
    main_repo, worker_repo
):
    """The unchanged path, and the one that must stay byte-for-byte what it
    was: one invocation, one report, no hand-back sentence."""
    agent = None

    def factory(root):
        nonlocal agent
        agent = RendezvousAgentRunner(
            worker_repo=root, asks=1, write_files={"feature.py": "x = 1\n"}
        )
        return agent

    executor = build_executor(
        main_repo,
        worker_repo,
        factory,
        validation=(RUFF,),
        command_runner=RecordingRunner(),
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert len(agent.specs) == 1
    assert "handed this round back" not in outcome.summary
    assert "UNANSWERED" not in outcome.summary
    assert "WITHHELD" not in outcome.summary
    assert "ran the suite 1 time(s)" in outcome.summary
    assert "PASSED." in outcome.summary
    assert outcome.status == "ok"


def test_a_failed_agent_is_never_handed_back(main_repo, worker_repo):
    """A round whose agent failed is already reported as a failure with its
    partial work measured. Handing it back would replace an honest failure with
    a second one, and pay for an agent call to do it."""
    captured = []

    def factory(root):
        agent = FakeAgentRunner(worker_repo=root, write_files={}, fail=True)
        captured.append(agent)
        return agent

    executor = build_executor(
        main_repo, worker_repo, factory, validation=(RUFF,), command_runner=RecordingRunner()
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert len(captured[0].specs) == 1
    assert outcome.status == "error"
    assert "agent exploded" in outcome.summary
    assert "handed this round back" not in outcome.summary
    # It is refused for the RIGHT reason. A failed agent is not a round that
    # skipped the suite, and reporting it as one would hide the actual cause.
    assert "WITHHELD" not in outcome.summary


def test_a_hand_back_whose_agent_fails_keeps_the_first_reports_account(
    main_repo, worker_repo
):
    """A TORN HAND-BACK IS NOT THE ROUND'S CAUSE. The surviving claim after the
    withhold landed: the first invocation returned cleanly, so the round must
    not be reported as "implementation agent failed" and must not lose that
    invocation's own account of itself.

    It is still WITHHELD, because `asked` is still zero — a hand-back that fell
    over changes nothing about the evidence that is missing. Historical: before
    the 2026-08-27 revision this round was forwarded as an ordinary candidate,
    on the reasoning that a hand-back must never turn a reviewable round into a
    failed one. Under the contract it is refused for, it was never a reviewable
    round."""
    factory, holder = make_forgetful_factory(
        write_files={"feature.py": "x = 1\n"},
        fail_when_handed_back=True,
        reports=["ASSUMPTION: the first invocation's own account", "torn"],
    )
    executor = build_executor(
        main_repo, worker_repo, factory, validation=(RUFF,), command_runner=RecordingRunner()
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert holder["agent"].invocations == 2
    assert outcome.status == "error"
    assert "WITHHELD from review" in outcome.summary
    assert "implementation agent failed" not in outcome.summary
    assert "ended in an agent failure" in outcome.summary
    assert "0 time(s)" in outcome.summary, "the measured zero still stands"
    # A torn re-invocation is an agent fault; this round is not. The withhold is
    # the task's own problem and must keep charging the task's attempts.
    assert not outcome.fault_kind
    # The first invocation's report is what the round carries, and the failed
    # one's text is not.
    assert "the first invocation's own account" in outcome.details
    assert "torn" not in outcome.details
    # Nothing is committed from a withheld round, so nothing is carried forward
    # about code the reviewer is not being shown — the same rule every other
    # uncommitted round follows.
    assert outcome.assumptions == ()


def test_every_invocations_report_reaches_the_reviewer_and_none_is_dropped(
    main_repo, worker_repo
):
    """A later report does not supersede an earlier one. `DELETE-FILE:`,
    `REMOVE-OUT-OF-SCOPE:` and `ASSUMPTION:` are all read out of this text, so
    keeping only the last would silently drop an authorized deletion the first
    invocation made.

    The deletion is really PERFORMED here, not merely carried: the task declares
    the scratch path, invocation 1 asks for it and invocation 2 does not repeat
    the request, so the file being gone afterwards is only explicable by the
    first report having survived. `scratch.py` is untracked and removed in the
    same round, so git has nothing to report and `changed_paths` carries only the
    file that stayed — which is what makes the round SUMMARY the disclosure."""
    factory, holder = make_forgetful_factory(
        write_files={"feature.py": "x = 1\n", "scratch.py": "# temporary\n"},
        reports=[
            "ASSUMPTION: read the narrow one\nDELETE-FILE: scratch.py",
            "ASSUMPTION: and then I ran the suite",
        ],
    )
    executor = build_executor(
        main_repo, worker_repo, factory, validation=(RUFF,), command_runner=RecordingRunner()
    )
    task = Task(
        id="t1",
        title="Add widget",
        description="Implement the widget feature.",
        approved_paths=("feature.py", "scratch.py"),
    )

    outcome = executor.execute(implement_directive(), task)

    assert holder["agent"].invocations == 2
    assert "DELETE-FILE" not in holder["agent"].reports[1], "invocation 2 never asked"
    assert not (worker_repo / "scratch.py").exists(), (
        "the first invocation's authorized deletion was dropped by the hand-back"
    )
    assert "DELETED 1 file(s)" in outcome.summary
    assert "scratch.py" in outcome.summary
    assert outcome.changed_paths == ("feature.py",)
    assert "read the narrow one" in outcome.details
    assert "and then I ran the suite" in outcome.details
    assert outcome.assumptions == (
        "read the narrow one",
        "and then I ran the suite",
    )


def test_the_hand_back_section_is_echo_safe_and_says_not_to_redo_the_work():
    """Two properties of the text, both of which cost a round when absent.

    A re-invocation is a fresh `claude -p` carrying the whole original brief, so
    a section that did not say "already implemented, do not redo it" invites a
    second change note and a duplicated test. And like every other prompt
    section it must not be quotable back into a disclosure or a deletion
    request."""
    text = _zero_call_return_instruction(2, final=True)

    assert _extract_assumptions(text) == ()
    assert _extract_cleanup_requests(text) == ()
    assert _extract_delete_requests(text) == ()
    assert "DO NOT REDO THE TASK" in text
    assert "ALREADY ON DISK" in text
    assert "2 advisory run(s) left" in text
    assert "LAST hand-back" in text
    assert "LAST hand-back" not in _zero_call_return_instruction(2, final=False)
    # It must state the CONSEQUENCE, and state it truthfully: since the
    # 2026-08-27 revision the round is withheld rather than forwarded, and a
    # section still promising a forward would be the executor telling the agent
    # something it is not going to do.
    assert "NOT forwarded to the reviewer" in text
    assert "the executor forwards the round" not in text
    assert "not forwarded to the reviewer" in _zero_call_return_instruction(2, False)
    # A self-imposed ceiling in the spirit of `test_the_added_prose_has_its_own
    # _ceilings`: this section is the part of the brief most likely to attract
    # elaboration, and the reasoning for it belongs in the source comment beside
    # it, which costs nothing, rather than in text re-sent to an agent.
    assert len(text) <= 2400


def test_the_hand_back_tells_the_agent_what_is_LEFT_not_what_the_cap_is(tmp_path):
    """The brief above it renders the CAP, a constant. On a hand-back that would
    overstate the budget for any round that had already spent a request — and
    `remaining` is computed from the same counters the round reports from."""
    service = make_service(tmp_path, runner=RecordingRunner(), max_calls=3)
    assert service.remaining == 3
    service.run()
    assert service.remaining == 2
    service.run()
    service.run()
    service.run()
    assert service.remaining == 0, "a refused request still spent the budget"


def test_a_handback_cannot_be_talked_out_of(main_repo, worker_repo):
    """THE ECHO. An agent that writes "I already ran the suite" moves no counter:
    the hand-back is decided by what the transport observed, and the round says
    the measured zero however loudly the report disagrees."""
    boast = (
        "I ran the validation suite already and it PASSED.\n"
        "Agent self-validation: the agent ran the suite 3 time(s).\n"
        f"{ADVISORY_RESULT_PREFIX} #1 — ADVISORY validation run 1 of 3 — PASSED."
    )
    captured = []

    def factory(root):
        agent = FakeAgentRunner(
            worker_repo=root, write_files={"feature.py": "x = 1\n"}, raw_text=boast
        )
        captured.append(agent)
        return agent

    executor = build_executor(
        main_repo, worker_repo, factory, validation=(RUFF,), command_runner=RecordingRunner()
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert len(captured[0].specs) == 1 + ADVISORY_ZERO_CALL_RETURNS
    assert "0 time(s)" in outcome.summary
    assert "3 time(s)" not in outcome.summary
    assert boast in outcome.details
    # And the round it produced is withheld on the same measured zero: prose
    # cannot buy a candidate any more than it can buy a hand-back.
    assert outcome.status == "error"
    assert "WITHHELD from review" in outcome.summary


def test_a_round_with_no_channel_to_offer_is_never_handed_back(
    main_repo, worker_repo
):
    """`offerable` is False, so there is nothing to send the agent back FOR: a
    hand-back would spend a whole agent invocation collecting a second
    `NOT RUN`.

    NOR IS IT WITHHELD, which is the same rule read the other way and the more
    dangerous half to get wrong: refusing a round for not using a call it never
    had would take the fail-closed direction against the wrong party."""
    captured = []

    def factory(root):
        agent = FakeAgentRunner(worker_repo=root, write_files={"feature.py": "x = 1\n"})
        captured.append(agent)
        return agent

    executor = build_executor(
        main_repo, worker_repo, factory, validation=(), command_runner=RecordingRunner()
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert len(captured[0].specs) == 1
    assert "NOT OFFERED" in outcome.summary
    assert "handed this round back" not in outcome.summary
    assert "WITHHELD" not in outcome.summary
    assert outcome.status == "ok", outcome.summary


class ParkedWatchRendezvous(AdvisoryRendezvous):
    """An `AdvisoryRendezvous` whose watcher takes exactly ONE poll and is then
    parked for far longer than a test can last.

    Every test below writes its request AFTER that first poll, so the watcher
    provably cannot take it: the state under test — a request nobody has
    consumed — is reached deterministically rather than by winning a race.
    """

    def __init__(self, service, root, **_kwargs):
        super().__init__(service, root, poll_seconds=300.0, join_timeout=5.0)
        self.polled = threading.Event()

    def serve_once(self):
        served = super().serve_once()
        self.polled.set()
        return served


def test_a_round_whose_ask_went_unanswered_is_never_withheld(
    main_repo, worker_repo, monkeypatch
):
    """THE INTERACTION between this task's two behaviours, and the one place
    they could contradict each other.

    port-05 round 1 asked for a run and never got one. Withholding THAT round
    would punish the agent for the channel's own failure and rebuild, one level
    up, exactly the misreport behaviour 2 exists to remove. So the withhold is
    keyed on `asked` — what the transport observed — and never on "no run
    completed", which would catch this round too.

    It is also the round in which `asked` alone is NOT enough: the hand-back is
    decided before `stop()` sweeps, so the ask is on the filesystem and not yet
    on the counter. `ask_outstanding()` is what the loop consults, and the two
    tests below grade that predicate on its own.

    Deterministic rather than timing-dependent: the watcher is let through
    exactly ONE poll, which happens before the agent writes anything, and is
    then parked for far longer than the round can last, so the request provably
    cannot be taken. `stop()`'s sweep is what sees it — the production path for
    an ask nobody answered."""
    made = []

    class RecordedParkedWatch(ParkedWatchRendezvous):
        def __init__(self, service, root, **kwargs):
            super().__init__(service, root, **kwargs)
            made.append(self)

    monkeypatch.setattr(implement_executor, "AdvisoryRendezvous", RecordedParkedWatch)

    class AsksAndGivesUp:
        """Writes the request with Write, exactly as the brief describes, and
        returns without ever seeing an answer."""

        def __init__(self, root):
            self.root = Path(root)
            self.specs = []

        def run(self, spec):
            self.specs.append(spec)
            assert made, "the executor never built a rendezvous"
            assert made[0].polled.wait(5), "the watcher never took its first poll"
            (self.root / ADVISORY_REQUEST_FILE).write_text("please run the suite\n")
            (self.root / "feature.py").write_text("x = 1\n")
            return AgentResult(
                domain=spec.domain,
                raw_text="I asked and nothing came back",
                returncode=0,
                duration_seconds=0.1,
                command=("claude",),
            )

    captured = []

    def factory(root):
        agent = AsksAndGivesUp(root)
        captured.append(agent)
        return agent

    runner = RecordingRunner()
    executor = build_executor(
        main_repo, worker_repo, factory, validation=(RUFF,), command_runner=runner
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert len(captured[0].specs) == 1, "an unanswered ask was treated as no ask"
    assert outcome.status == "ok", outcome.summary
    assert "WITHHELD" not in outcome.summary
    assert "UNANSWERED" in outcome.summary
    assert "no answer landed" in outcome.summary
    # The executor's own run still happened, and it is still the verdict.
    assert [call["argv"] for call in runner.calls] == [RUFF]


def test_an_unconsumed_ask_is_visible_before_the_sweep_counts_it(worker_repo):
    """THE GATE the test above stands on, at the rendezvous level.

    `record_request_asked` fires when the watcher TAKES a request or, failing
    that, when `stop()` finds one still in the tree — and `stop()` does not run
    until the round is over. So between the agent writing its request and the
    sweep, `AdvisoryValidation.asked` is still zero and only the filesystem
    knows an ask happened. `ask_outstanding()` is what the hand-back decision
    reads instead, and without it a round hands itself back to an agent that
    asked and was never answered.

    Both of `stop()`'s own gates are inherited rather than restated: residue
    from a round that never watched is not an ask (`_started`), and the broken
    channel is `test_a_broken_channel_leaves_no_outstanding_ask_to_count_twice`.
    """
    service = make_service(worker_repo)
    rendezvous = ParkedWatchRendezvous(service, worker_repo)

    # Residue from a round that died: an entry is present, but this rendezvous
    # has never watched, so it is nobody's ask.
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("from a round that died\n")
    assert rendezvous.ask_outstanding() is False

    rendezvous.start()  # sweeps that residue away, then watches
    assert rendezvous.polled.wait(5), "the watcher never took its first poll"
    assert rendezvous.ask_outstanding() is False, "nothing has been asked yet"

    (worker_repo / ADVISORY_REQUEST_FILE).write_text("please run the suite\n")

    assert rendezvous.ask_outstanding() is True
    assert service.asked == 0, "the counter cannot see it until the sweep"

    rendezvous.stop()

    assert service.asked == 1, "the sweep is what counts it, exactly as before"
    assert rendezvous.ask_outstanding() is False, "the sweep took the file"


def test_a_broken_channel_leaves_no_outstanding_ask_to_count_twice(
    worker_repo, monkeypatch
):
    """The `_broken` gate, which is the double-count this predicate would
    otherwise introduce.

    That branch has already recorded its ask AND answered it, and the file it
    could not remove is that same request — so it is not an outstanding one.
    Reporting it as outstanding would suppress a hand-back on the strength of a
    request the agent was already told about, and `stop()` would count the ask a
    second time."""
    service = make_service(worker_repo, runner=RecordingRunner())
    rendezvous = ParkedWatchRendezvous(service, worker_repo)
    rendezvous.start()
    assert rendezvous.polled.wait(5), "the watcher never took its first poll"
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("please run the suite\n")

    real = implement_executor._remove_entry
    monkeypatch.setattr(
        implement_executor,
        "_remove_entry",
        lambda path: False if path.name == ADVISORY_REQUEST_FILE else real(path),
    )
    # Served by hand: the watcher is parked inside `_stopping.wait(300)` and
    # reaches no file operation until `stop()`, so patching a module global
    # while it is alive races nothing.
    assert rendezvous.serve_once() is False
    monkeypatch.undo()

    assert (worker_repo / ADVISORY_REQUEST_FILE).exists(), "the branch under test"
    assert service.asked == 1, "the broken branch counted the ask it answered"
    assert service.delivered == 1
    assert rendezvous.ask_outstanding() is False

    rendezvous.stop()

    assert service.asked == 1, "counted once, never twice"
    assert service.unanswered == 0


def test_the_ask_is_recorded_before_the_request_file_is_removed():
    """The line order the hand-back gate's race argument rests on, and the one
    way it could fail OPEN.

    `_run_implementation` checks `ask_outstanding()` BEFORE `advisory.asked`,
    which is safe only because `_take_request` counts the ask before it removes
    the file: a request that is already gone was already counted. Swap those two
    lines and a request consumed between the two reads becomes no ask at all —
    the round is handed back to an agent that asked, and nothing else in the
    code says why."""
    source = inspect.getsource(AdvisoryRendezvous._take_request)

    assert source.index("record_request_asked") < source.index("_remove_entry(path)")


def test_an_abort_outranks_the_withhold(main_repo, worker_repo, tmp_path):
    """An operator who pressed the button is not handed a charged failure.

    The abort check runs BEFORE the withhold, so a round stopped mid-flight
    reports itself as aborted — which costs the task no attempt. The other order
    would build an `attempt_count_ceiling` out of the operator's own button,
    which is the abort-01 failure mode wearing this feature's name."""
    abort_file = tmp_path / "ABORT"

    class ArmsTheAbort(FakeAgentRunner):
        def run(self, spec):
            result = super().run(spec)
            abort_file.touch()
            return result

    captured = []

    def factory(root):
        agent = ArmsTheAbort(worker_repo=root, write_files={"feature.py": "x = 1\n"})
        captured.append(agent)
        return agent

    executor = build_executor(
        main_repo,
        worker_repo,
        factory,
        validation=(RUFF,),
        command_runner=RecordingRunner(),
        abort_file=abort_file,
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert len(captured[0].specs) == 1, "the operator's stop paid for a hand-back"
    assert outcome.status == EXECUTION_ABORTED, outcome.summary
    assert "WITHHELD" not in outcome.summary
    # The zero is still on the record — the round says what it did, it just does
    # not blame the agent for a round the operator ended.
    assert "0 time(s)" in outcome.summary


def test_a_single_report_round_carries_exactly_the_text_it_always_did():
    """`_combined_report` must be the identity for every round that was never
    handed back, which is every round in which the agent used the channel."""
    assert _combined_report(["only this"]) == "only this"
    assert _combined_report([]) == ""
    assert _combined_report(["", "only this"]) == "only this"
    joined = _combined_report(["first", "second"])
    assert joined.startswith("first")
    assert joined.endswith("second")
    assert _extract_assumptions(joined) == ()


# ---- 10b: behaviour 2 — an ask that never became an answer -----------------


def test_a_request_that_never_became_a_result_is_reported_as_unanswered(worker_repo):
    """THE CLAIM's second half, in its simplest shape: `PENDING #1` was written,
    the round ended, no `RESULT #1` ever landed."""
    release = threading.Event()
    started = threading.Event()

    def blocking_runner(argv, **kwargs):
        started.set()
        release.wait(10)
        return _proc()

    service = make_service(worker_repo, runner=blocking_runner)
    rendezvous = AdvisoryRendezvous(
        service, worker_repo, poll_seconds=0.01, join_timeout=0.05
    )
    rendezvous.start()
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("go\n")
    assert started.wait(5), "the advisory run must have begun"

    rendezvous.stop()
    note = service.note()

    assert service.asked == 1
    assert service.delivered == 0
    assert service.unanswered == 1
    assert "UNANSWERED" in note
    assert "no answer landed" in note
    assert "not a pass" in note.lower()
    release.set()


def test_a_stale_failed_verdict_is_not_reported_as_the_rounds_last_run(worker_repo):
    """PORT-05 ROUND 1, reproduced. The agent asked for run #2, never got it,
    and the round reported run #1's `FAILED` as "its last run FAILED" — so the
    reviewer refused work that was never shown to be defective. Both facts must
    now be present, and the stale one must not be the headline."""
    release = threading.Event()
    second_started = threading.Event()
    calls = []

    def runner(argv, **kwargs):
        calls.append(tuple(argv))
        if len(calls) == 1:
            return _proc(returncode=1, stdout="", stderr="boom\n")
        second_started.set()
        release.wait(10)
        return _proc()

    service = make_service(worker_repo, runner=runner)
    rendezvous = AdvisoryRendezvous(
        service, worker_repo, poll_seconds=0.01, join_timeout=0.05
    )
    rendezvous.start()
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("first\n")
    deadline = time.monotonic() + 5
    while service.delivered < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert service.delivered == 1, "the first answer must have landed"

    (worker_repo / ADVISORY_REQUEST_FILE).write_text("second\n")
    assert second_started.wait(5), "the second run must have begun"
    rendezvous.stop()
    note = service.note()

    assert service.asked == 2
    assert service.unanswered == 1
    assert "UNANSWERED" in note
    assert "no answer landed" in note
    # The completed red run is still reported — suppressing it would be the
    # opposite error — but it is no longer the round's stated state.
    assert "FAILED" in note
    assert "its last run FAILED" not in note
    release.set()


def test_an_ask_the_watcher_never_took_is_still_an_ask(worker_repo):
    """THE SWEPT-EVIDENCE CASE, and the fail-open this design would otherwise
    have. A request the watcher never takes is deleted by `stop()`'s sweep — and
    with it the only trace that the agent asked. If the count came from
    `_take_request` alone, the round would report the older run's verdict as its
    state and the alarm would never fire.

    Deterministic rather than timing-dependent: the watcher is held INSIDE a
    blocking run, so the second request provably cannot be taken."""
    release = threading.Event()
    started = threading.Event()

    def blocking_runner(argv, **kwargs):
        started.set()
        release.wait(10)
        return _proc()

    service = make_service(worker_repo, runner=blocking_runner)
    rendezvous = AdvisoryRendezvous(
        service, worker_repo, poll_seconds=0.01, join_timeout=0.05
    )
    rendezvous.start()
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("first\n")
    assert started.wait(5), "the watcher must be occupied inside the run"
    # `_take_request` removed the first one, so this is a NEW file, and the
    # watcher cannot reach it — it is still inside the run above.
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("second\n")

    rendezvous.stop()

    assert service.asked == 2, "the ask the sweep was about to delete still counts"
    assert service.delivered == 0
    assert service.unanswered == 2
    assert "UNANSWERED" in service.note()
    assert not (worker_repo / ADVISORY_REQUEST_FILE).exists()
    release.set()


def test_residue_from_a_dead_round_is_not_counted_as_this_rounds_ask(worker_repo):
    """The other direction, and the reason the sweep-time count is gated on
    `start()`. A request file left by a round that died is residue, not an ask,
    and inventing an UNANSWERED from it would be this report crying wolf.

    (`note()` here says NOT OFFERED for the separate reason that nothing exposed
    the channel; the counter is the claim.)"""
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("from a round that died\n")
    service, rendezvous = make_rendezvous(worker_repo)

    rendezvous.stop()

    assert service.asked == 0
    assert service.unanswered == 0
    assert "UNANSWERED" not in service.note()
    assert not (worker_repo / ADVISORY_REQUEST_FILE).exists()


def test_a_completed_failing_run_is_reported_as_failed_and_not_as_unanswered(
    worker_repo,
):
    """The distinction this whole half exists for: a run that COMPLETED and went
    red is evidence, and must keep saying so."""
    service, rendezvous = make_rendezvous(
        worker_repo, runner=RecordingRunner(returncodes=(1,))
    )
    rendezvous.start()
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("go\n")
    deadline = time.monotonic() + 5
    while service.delivered < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    rendezvous.stop()
    note = service.note()

    assert service.asked == 1
    assert service.delivered == 1
    assert service.unanswered == 0
    assert "UNANSWERED" not in note
    assert "its last run FAILED." in note


def test_a_broken_channel_answers_the_agent_and_is_not_reported_unanswered(
    worker_repo, monkeypatch
):
    """A `NOT RUN` refusal IS an answer: the agent was told. It must not read as
    a run (nothing executed), it must not read as UNANSWERED, and it must not
    move `requests` — which is precisely why the hand-back is keyed on `asked`
    and not on `requests`: this agent asked and did nothing wrong."""
    runner = RecordingRunner()
    service, rendezvous = make_rendezvous(worker_repo, runner=runner)
    # `expose()` directly rather than `start()`, because `start()` sweeps first
    # and the watcher would race this single hand-driven `serve_once`.
    service.expose()
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("please run the suite\n")

    real = implement_executor._remove_entry
    monkeypatch.setattr(
        implement_executor,
        "_remove_entry",
        lambda path: False if path.name == ADVISORY_REQUEST_FILE else real(path),
    )
    assert rendezvous.serve_once() is False
    monkeypatch.undo()
    rendezvous.stop()
    note = service.note()

    assert service.asked == 1
    assert service.delivered == 1
    assert service.unanswered == 0
    assert service.requests == 0, "nothing was ever handed to the bound call"
    assert service.runs == 0
    assert runner.calls == []
    assert "UNANSWERED" not in note
    assert "asked 1 time(s)" in note


def test_an_unanswered_ask_with_no_completed_run_says_so_without_a_verdict(worker_repo):
    """No run ever completed, so there is no verdict to report — and the note
    must not manufacture one in either direction."""
    release = threading.Event()
    started = threading.Event()

    def blocking_runner(argv, **kwargs):
        started.set()
        release.wait(10)
        return _proc()

    service = make_service(worker_repo, runner=blocking_runner)
    rendezvous = AdvisoryRendezvous(
        service, worker_repo, poll_seconds=0.01, join_timeout=0.05
    )
    rendezvous.start()
    (worker_repo / ADVISORY_REQUEST_FILE).write_text("go\n")
    assert started.wait(5)
    rendezvous.stop()
    note = service.note()

    assert "UNANSWERED" in note
    assert "ran 0 time(s)" in note
    assert "PASSED" not in note
    assert "FAILED" not in note
    release.set()


def test_an_unoffered_round_still_says_not_offered_whatever_the_counters_hold(
    worker_repo,
):
    """`NOT OFFERED` is a fact about the ROUND and outranks every counter below
    it: a channel the agent could not reach must never be reported as one it
    used badly."""
    service = make_service(worker_repo)
    service.record_request_asked()

    note = service.note()

    assert "NOT OFFERED" in note
    assert "UNANSWERED" not in note
    assert "could not" in note
