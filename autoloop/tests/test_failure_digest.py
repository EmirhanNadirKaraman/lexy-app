"""A refused commit must be diagnosable from its own record.

The validation summary used to keep only the LAST line of output. For pytest
that is the count line ("1 failed, 992 passed"), which discards the
`FAILED <file>::<test>` lines printed immediately above it. On 2026-08-02 an
audit commit was refused on one failing test and the only way to learn which
one was to re-run the tree by hand — which then passed, so the refusal was a
flake nobody could have identified from the blocker.
"""

import subprocess

import pytest

from autoloop.validation import failure_digest, run_validation_commands
from autoloop.validation_env import load_validation_env

# The real shape, colour codes and all: pytest emits ANSI even under `-q`
# when it believes it has a tty, and `capture_output` preserves it.
PYTEST_OUTPUT = (
    "\x1b[31m.....F.....\x1b[0m\n"
    "=========================== short test summary info ===========================\n"
    "\x1b[31mFAILED\x1b[0m autoloop/tests/test_blockers.py::\x1b[1mtest_round_trip\x1b[0m"
    " - assert 2 == 1\n"
    "\x1b[31m\x1b[1m1 failed\x1b[0m, \x1b[32m992 passed\x1b[0m, "
    "\x1b[33m1 skipped\x1b[0m\x1b[31m in 277.43s\x1b[0m\n"
)


def test_the_failing_test_is_named_not_just_counted():
    digest = failure_digest(PYTEST_OUTPUT)

    assert "test_blockers.py::test_round_trip" in digest
    assert "assert 2 == 1" in digest
    # The count still matters: it says whether one test failed or four hundred.
    assert "1 failed, 992 passed" in digest


def test_ansi_codes_are_stripped():
    """They survive capture_output into blocker records and park messages,
    where they render as literal escape noise."""
    digest = failure_digest(PYTEST_OUTPUT)
    assert "\x1b" not in digest
    assert "[31m" not in digest


def test_every_failing_test_is_named_up_to_the_cap():
    output = "\n".join(
        f"FAILED tests/test_mod.py::test_{i} - boom" for i in range(5)
    ) + "\n5 failed, 10 passed\n"

    digest = failure_digest(output)

    for i in range(5):
        assert f"test_{i}" in digest


def test_a_catastrophic_run_is_bounded_and_says_it_truncated():
    """A thousand failures must not flood a park message or a review packet —
    but silently dropping them would read as 'only 12 failed'."""
    output = "\n".join(
        f"FAILED tests/test_mod.py::test_{i} - boom" for i in range(400)
    ) + "\n400 failed\n"

    digest = failure_digest(output)

    assert len(digest) <= 700
    assert "more)" in digest, "truncation must be visible"
    assert "400 failed" in digest, "the total must survive truncation"


def test_output_with_no_named_failures_still_reports_something():
    """ruff, tsc and a crashed interpreter do not print `FAILED` lines."""
    assert "error: unexpected token" in failure_digest("error: unexpected token\n")
    assert failure_digest("") == "(no output)"
    assert failure_digest("   \n\n ") == "(no output)"


def test_a_lone_failed_line_is_not_duplicated():
    digest = failure_digest("FAILED tests/test_x.py::test_y - boom\n")
    assert digest.count("test_y") == 1


# --- the boundary this must not weaken ---------------------------------------


def test_the_digest_never_leaks_a_credential(tmp_path):
    """The digest reaches `state.last_validation`, and from there the
    transcript, blocker records and the review packet sent to ChatGPT. It is
    wider than the old one-line tail, so the redaction it passes through
    matters more, not less."""
    env_file = tmp_path / "validation.env"
    env_file.write_text(
        "DB_HOST=localhost\nDB_PORT=5432\nDB_NAME=lexy_validation\n"
        "DB_USER=lexy_validator\nDB_PASSWORD=sup3rs3cret-value\n"
        "SECRET_KEY=jwt-signing-key-not-real\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    repo = tmp_path / "repo"
    repo.mkdir()
    validation_env = load_validation_env(
        env_file, repo_root=repo, state_dir=repo / ".autoloop"
    )

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout=(
                "FAILED tests/test_db.py::test_connect - could not connect as "
                "lexy_validator using password sup3rs3cret-value to lexy_validation\n"
                "1 failed\n"
            ),
            stderr="",
        )

    ok, summary = run_validation_commands(
        [("pytest", "-q")], tmp_path, command_runner=fake_run,
        validation_env=validation_env,
    )

    assert ok is False
    assert "sup3rs3cret-value" not in summary
    assert "jwt-signing-key-not-real" not in summary
    assert "lexy_validator" not in summary
    # Still useful: the test name survives redaction.
    assert "test_db.py::test_connect" in summary


def test_a_passing_run_is_unchanged(tmp_path):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="all good\n", stderr="")

    ok, summary = run_validation_commands(
        [("ruff", "check", ".")], tmp_path, command_runner=fake_run
    )
    assert ok is True
    assert summary == "ruff check .: PASS"


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_failures_are_found_on_either_stream(tmp_path, stream):
    def fake_run(argv, **kwargs):
        payload = "FAILED tests/test_x.py::test_y - boom\n1 failed\n"
        return subprocess.CompletedProcess(
            argv, 1,
            stdout=payload if stream == "stdout" else "",
            stderr=payload if stream == "stderr" else "",
        )

    _, summary = run_validation_commands(
        [("pytest", "-q")], tmp_path, command_runner=fake_run
    )
    assert "test_x.py::test_y" in summary
