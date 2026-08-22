"""Validation stops at the first failing command, and still accounts for the
rest.

WHY. The executor round IS the loop's clock: measured over the 23 days to
2026-08-22, packet build took 0.98s median, submit 12-18s, the reviewer's
verdict ~0s and the merge sweep ~20s — against an executor round of 1,282s
median and 42.3 minutes at worst. 64% of measurable executor time produced
nothing reviewable, and 79 of 261 revise verdicts were caused by something
orthogonal to the work being done (an inherited test failure, a scope
violation, an overlong documentation change-note line). Those defects are named
by the CHEAPEST configured command, and `run_validation_commands` used to pay
for both full pytest suites and the serial `isolated` re-run before reporting
one of them.

WHAT IS ASSERTED HERE, and what is deliberately NOT. The claim is about
REPORTING and EXECUTION, never about the verdict: `all_passed` is False either
way, so nothing here loosens a gate. Two properties have to hold together, and
each is useless alone —

  * the later commands really do not launch (the saving), and
  * the summary still names every configured command, with `NOT RUN`
    distinguishable from `PASS` (the evidence the reviewer decides on).

A test for the first alone passes against a runner that reports a single
collapsed verdict; a test for the second alone passes against one that ran
everything anyway.
"""

import subprocess

import pytest

from test_validation_parallelism import (  # sibling test module, bare-name import
    LEGACY_SERIAL,
    is_isolated_run,
    is_pytest,
    shipped_commands,
)

from autoloop.validation import (
    NOT_RUN,
    effective_validation_command,
    effective_validation_commands,
    run_validation_commands,
)

RUFF = ("ruff", "check", ".")
SUITE_A = ("python3", "-m", "pytest", "autoloop/tests", "-q", "-p", "no:cacheprovider")
SUITE_B = ("python3", "-m", "pytest", "tests/", "-q", "-p", "no:cacheprovider")


def runner_failing(*fail_argv):
    """A `subprocess.run` stand-in recording every argv it is HANDED.

    Failure is decided by the EFFECTIVE argv (the one a caller sees after
    `effective_validation_commands`), so a test can name a configured command
    and still fail the command that really runs.
    """
    seen: list[tuple[str, ...]] = []
    failing = {effective_validation_command(argv) for argv in fail_argv}

    def runner(argv, **kwargs):
        seen.append(tuple(argv))
        broken = tuple(argv) in failing
        return subprocess.CompletedProcess(
            argv,
            1 if broken else 0,
            stdout=(
                "FAILED autoloop/tests/test_docs_merge.py::test_note_line - too long\n"
                "1 failed, 900 passed\n"
                if broken
                else "ok\n"
            ),
            stderr="",
        )

    return runner, seen


def named(summary: str, argv) -> str:
    """The one report segment for `argv`, or "" — asserted on rather than the
    whole summary, because `failure_digest` joins its own lines with "; " too,
    so splitting the summary by that separator is not a per-command split."""
    command = " ".join(effective_validation_command(argv))
    for token in ("PASS", "FAIL", NOT_RUN, "TIMEOUT", "NOT FOUND", "REFUSED"):
        if f"{command}: {token}" in summary:
            return token
    return ""


# ---- 1. the saving: later commands do not launch ----------------------------


def test_the_first_failing_command_stops_the_run(tmp_path):
    """The claim the whole task is for: a round whose cheapest check fails does
    not pay for the expensive ones."""
    runner, seen = runner_failing(RUFF)

    ok, _summary = run_validation_commands(
        (RUFF, SUITE_A, SUITE_B), tmp_path, command_runner=runner
    )

    assert ok is False
    assert seen == [effective_validation_command(RUFF)], (
        "a failing lint still paid for both pytest suites"
    )


def test_the_commands_that_did_not_run_are_named_as_not_run(tmp_path):
    """`NOT RUN` is EVIDENCE, not an omission. The reviewer decides partly on
    what was exercised, so a command that never launched must be visible and
    must not read as one that passed."""
    runner, _seen = runner_failing(RUFF)

    _ok, summary = run_validation_commands(
        (RUFF, SUITE_A, SUITE_B), tmp_path, command_runner=runner
    )

    assert named(summary, RUFF) == "FAIL"
    assert named(summary, SUITE_A) == NOT_RUN
    assert named(summary, SUITE_B) == NOT_RUN


def test_the_summary_still_names_every_configured_command(tmp_path):
    """One line per configured command, whatever happened to it — the
    per-command report is not collapsed to a single verdict."""
    runner, _seen = runner_failing(SUITE_A)

    _ok, summary = run_validation_commands(LEGACY_SERIAL, tmp_path, command_runner=runner)

    for argv in effective_validation_commands(LEGACY_SERIAL):
        assert " ".join(argv) in summary, f"{' '.join(argv)} vanished from the report"
    assert named(summary, LEGACY_SERIAL[0]) == "PASS", "the command BEFORE the failure ran"
    assert named(summary, LEGACY_SERIAL[1]) == "FAIL"
    assert named(summary, LEGACY_SERIAL[2]) == NOT_RUN
    assert named(summary, LEGACY_SERIAL[3]) == NOT_RUN


def test_the_failing_test_is_still_named(tmp_path):
    """Short-circuiting must not cost the diagnosis: the digest that made a
    refusal actionable (2026-08-02) is still there."""
    runner, _seen = runner_failing(SUITE_A)

    _ok, summary = run_validation_commands(LEGACY_SERIAL, tmp_path, command_runner=runner)

    assert "test_docs_merge.py::test_note_line" in summary


def test_the_report_says_it_stopped_and_how_to_run_everything(tmp_path):
    """A future reader has to be able to tell a deliberate stop from a run that
    lost commands, and a deliberate ORDER from an accidental one."""
    runner, _seen = runner_failing(RUFF)

    _ok, summary = run_validation_commands(
        (RUFF, SUITE_A, SUITE_B), tmp_path, command_runner=runner
    )

    assert "STOPPED at the first failing command" in summary
    assert "2 command(s)" in summary
    assert "CONFIGURED order, cheapest first" in summary
    assert "fail_fast=False" in summary


def test_the_note_names_only_levers_that_exist(tmp_path):
    """The note becomes `state.last_validation` and reaches the reviewer. A
    sentence telling them to set a config key nothing reads would be a false
    statement shipped inside a change about reporting honestly — and
    `[audit] validation_run` is exactly the key this task did NOT ship (see
    `docs/AUTOLOOP.md` §4h)."""
    runner, _seen = runner_failing(RUFF)

    _ok, summary = run_validation_commands((RUFF, SUITE_A), tmp_path, command_runner=runner)

    assert "[audit]" not in summary
    assert "validation_run" not in summary


# ---- 2. everything that is NOT allowed to change ----------------------------


def test_a_passing_run_is_byte_identical(tmp_path):
    """Nothing is skipped when nothing fails, so there is nothing to say about
    it. Pinned exactly, not by substring: an unconditional ordering note would
    ride on every round's `state.last_validation` for no reader."""
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    ok, summary = run_validation_commands((RUFF,), tmp_path, command_runner=runner)

    assert ok is True
    assert summary == "ruff check .: PASS"


def test_a_passing_multi_command_run_still_reports_each_command(tmp_path):
    runner, seen = runner_failing()

    ok, summary = run_validation_commands(
        (RUFF, SUITE_A, SUITE_B), tmp_path, command_runner=runner
    )

    assert ok is True
    assert len(seen) == 3
    for argv in (RUFF, SUITE_A, SUITE_B):
        assert named(summary, argv) == "PASS"
    assert NOT_RUN not in summary
    assert "STOPPED" not in summary


def test_a_failure_in_the_last_command_adds_no_note(tmp_path):
    """There is nothing after it to skip, so the summary is what it always
    was — the note appears only when it has something to explain."""
    runner, seen = runner_failing(SUITE_B)

    ok, summary = run_validation_commands(
        (RUFF, SUITE_A, SUITE_B), tmp_path, command_runner=runner
    )

    assert ok is False
    assert len(seen) == 3
    assert NOT_RUN not in summary
    assert "STOPPED" not in summary


def test_an_empty_command_list_still_reports_passed(tmp_path):
    ok, summary = run_validation_commands((), tmp_path, command_runner=runner_failing()[0])

    assert ok is True
    assert summary == "(no validation commands configured)"


@pytest.mark.parametrize(
    "boom, token",
    [
        (subprocess.TimeoutExpired(cmd="pytest", timeout=1), "TIMEOUT"),
        (FileNotFoundError("pytest"), "NOT FOUND"),
    ],
)
def test_a_timeout_or_missing_binary_is_still_a_failure_not_an_exception(
    tmp_path, boom, token
):
    """Unchanged promise: every failure mode is reported, none raises past the
    caller. What IS new is that these stop the run too — they are failures by
    this function's own definition, so treating them as "keep going" would make
    the report's own vocabulary inconsistent."""
    seen: list[tuple[str, ...]] = []

    def runner(argv, **kwargs):
        seen.append(tuple(argv))
        raise boom

    ok, summary = run_validation_commands(
        (RUFF, SUITE_A), tmp_path, command_runner=runner
    )

    assert ok is False
    assert named(summary, RUFF) == token
    assert named(summary, SUITE_A) == NOT_RUN
    assert len(seen) == 1


def test_a_refused_binary_is_still_a_failure_and_launches_nothing(tmp_path):
    """The allowlist refusal predates this and is untouched. It is also a
    failure, so it stops the run — and the refused command itself was never
    handed to the runner, which is the property that made it a refusal."""
    runner, seen = runner_failing()

    ok, summary = run_validation_commands(
        (("rm", "-rf", "/"), SUITE_A), tmp_path, command_runner=runner
    )

    assert ok is False
    assert "REFUSED" in summary
    assert named(summary, SUITE_A) == NOT_RUN
    assert seen == []


def test_the_summary_is_still_redacted_end_to_end(tmp_path):
    """Redaction runs LAST over the assembled summary, so the lines fail-fast
    added are covered by the same call. Asserted rather than assumed: this
    string reaches state.json, the transcript, blocker records and the review
    packet."""
    from autoloop.validation_env import ValidationEnv

    env = ValidationEnv(
        tmp_path / "validation.env",
        {
            "DB_HOST": "127.0.0.1",
            "DB_PORT": "5432",
            "DB_NAME": "autoloop_validation_test",
            "DB_USER": "validation_user",
            "DB_PASSWORD": "super-secret-password",
            "SECRET_KEY": "jwt-signing-key-for-tests",
        }
    )

    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr="auth failed for super-secret-password\n"
        )

    ok, summary = run_validation_commands(
        (RUFF, SUITE_A), tmp_path, command_runner=runner, validation_env=env
    )

    assert ok is False
    assert "super-secret-password" not in summary
    assert NOT_RUN in summary


# ---- 3. the other mode ------------------------------------------------------


def test_the_full_run_mode_still_executes_everything(tmp_path):
    """`fail_fast=False` is the "how much is broken" question, and it answers
    it: every command runs and every failure is reported, not just the first."""
    runner, seen = runner_failing(RUFF, SUITE_B)

    ok, summary = run_validation_commands(
        (RUFF, SUITE_A, SUITE_B), tmp_path, command_runner=runner, fail_fast=False
    )

    assert ok is False
    assert len(seen) == 3, "the full run stopped early"
    assert named(summary, RUFF) == "FAIL"
    assert named(summary, SUITE_A) == "PASS"
    assert named(summary, SUITE_B) == "FAIL", "the SECOND failure is the point of this mode"
    assert NOT_RUN not in summary
    assert "STOPPED" not in summary


def test_the_full_run_mode_reports_every_failure_kind(tmp_path):
    """A refusal in the middle of a full run must not swallow the commands
    after it either."""
    runner, seen = runner_failing(SUITE_A)

    ok, summary = run_validation_commands(
        (("rm", "-rf", "/"), SUITE_A, SUITE_B),
        tmp_path,
        command_runner=runner,
        fail_fast=False,
    )

    assert ok is False
    assert "REFUSED" in summary
    assert named(summary, SUITE_A) == "FAIL"
    assert named(summary, SUITE_B) == "PASS"
    assert len(seen) == 2


# ---- 4. the order is deliberate, and it is the shipped one ------------------


def test_the_cheapest_shipped_command_really_runs_first():
    """Fail-fast makes ORDER load-bearing, so the shipped template's order is
    now a property worth pinning rather than a preference.

    The rule asserted is the one that is decidable without a stopwatch: the
    first configured command is the LINT (no pytest anywhere in it), which is
    seconds against suites that are minutes. The two pytest suites' order
    relative to each other is deliberately NOT pinned — it needs measurement
    this test cannot do, and an unmeasured claim is what this task is against.
    """
    shipped = shipped_commands()
    assert shipped, "the example ships no validation commands"
    assert not is_pytest(shipped[0]), (
        f"the first shipped command is {' '.join(shipped[0])}, a test suite — "
        "under fail-fast that is what every round pays before a lint failure "
        "can report"
    )
    assert shipped[0][0] == "ruff"


def test_the_serial_isolated_rerun_is_shipped_last():
    """It is the one command that cannot be parallelised (the marker means
    "this test needs its own process"), so it is the most expensive per test
    and belongs at the end of a list that now stops at the first failure."""
    shipped = shipped_commands()
    isolated = [index for index, argv in enumerate(shipped) if is_isolated_run(argv)]
    assert isolated == [len(shipped) - 1], (
        f"the isolated re-run sits at {isolated}, not last: {len(shipped) - 1}"
    )


# ---- 5. production really gets the default ----------------------------------


def test_the_pre_commit_executor_run_stops_at_the_first_failure(tmp_path):
    """The round that matters. `ImplementExecutor`'s own run is the one whose
    failure throws the round away before a commit exists, and it takes the
    default — so the saving is a property of production wiring, not only of
    this function's signature."""
    from autoloop.audit.agents import AgentResult
    from autoloop.contract import Decision, Directive
    from autoloop.implement_executor import ImplementExecutor
    from autoloop.tasks import Task

    class FakeAgent:
        def run(self, spec):
            return AgentResult(
                domain=spec.domain, raw_text="edited", returncode=0,
                duration_seconds=0.1, command=("claude",),
            )

    class FakeGit:
        repo_root = tmp_path

        def dirty_paths_all(self):
            return ["docs/SUMMARY.md"]

    runner, seen = runner_failing(RUFF)
    executor = ImplementExecutor(
        git=FakeGit(),
        agent_runner=FakeAgent(),
        validation_commands=(RUFF, SUITE_A, SUITE_B),
        command_runner=runner,
    )

    outcome = executor.execute(
        Directive(decision=Decision.IMPLEMENT, reason="do it", task_id="val-03"),
        Task(id="val-03", title="t", description="d"),
    )

    assert outcome.status == "error"
    assert "validation failed" in outcome.summary
    assert seen == [effective_validation_command(RUFF)]
    assert NOT_RUN in outcome.validation


def test_neither_production_call_site_overrides_the_default():
    """Structural, and it carries a claim no behavioural test here can: the two
    production callers get fail-fast because they pass nothing, so a future
    `fail_fast=False` at either site is a deliberate decision that has to be
    made in the open rather than a default drifting back."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for name in ("implement_executor.py", "orchestrator.py"):
        source = (root / name).read_text(encoding="utf-8")
        assert "run_validation_commands(" in source, f"{name} no longer calls it"
        assert "fail_fast" not in source, (
            f"{name} now sets fail_fast explicitly — if that is intended, say so "
            "in docs/AUTOLOOP.md §4h and update this test"
        )
