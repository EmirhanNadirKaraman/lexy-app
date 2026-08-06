"""The shipped validation list must stay parallel, cache-free, and honest
about the `isolated` marker.

Post-commit validation re-runs the full suites against a task's committed
worker repo on EVERY round, revises included. Three properties of that command
list are easy to lose in a one-line edit and expensive to lose in production,
so each is pinned here rather than left to the comment that explains it:

  * `-n auto` — the whole point of the list being parallel. Dropping it costs
    minutes of wall clock per round and nothing fails, so nothing would notice.
  * `-p no:cacheprovider` — a failing test writes `.pytest_cache/` into the
    worker repo, and the gate after validation refuses a tree validation
    dirtied. Losing this turns one refusal into two (2026-08-03).
  * the dedicated `-m isolated` run, SERIAL. `pytest.ini` deselects the marker
    from every default run; if the separate command disappears the test stops
    running and every suite stays green. If it gained `-n` it would be sharing
    a process pool with other tests, which is the one thing the marker exists
    to prevent.

Read from `config.example.toml` rather than asserted against a literal: that
file is what an operator copies to `.autoloop/config.toml` (`docs/AUTOLOOP.md`
§8 Setup), so it is the artifact whose contents actually decide what a
validation run does. Nothing else in the repo reads it, which is precisely why
it can rot unnoticed.
"""

import configparser
import subprocess
import tomllib
from pathlib import Path

from autoloop.validation import SAFE_VALIDATION_BINARIES, run_validation_commands

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "autoloop" / "config.example.toml"


def shipped_commands() -> list[tuple[str, ...]]:
    """`[audit].validation_commands` from the shipped example config."""
    with EXAMPLE_CONFIG.open("rb") as handle:
        data = tomllib.load(handle)
    commands = data["audit"]["validation_commands"]
    assert isinstance(commands, list) and commands, "the example ships no validation"
    # The same shape `config.load_config` enforces on the way in — a list that
    # fails this would raise ConfigError for whoever copied the file.
    for argv in commands:
        assert isinstance(argv, list) and argv, f"not a non-empty argv list: {argv!r}"
        assert all(isinstance(token, str) for token in argv), f"non-string in {argv!r}"
    return [tuple(argv) for argv in commands]


def pairs(argv: tuple[str, ...]) -> set[tuple[str, str]]:
    """Adjacent flag/value pairs, so `-m isolated` is distinguishable from the
    `-m pytest` in `python3 -m pytest` by its VALUE, not by position."""
    return set(zip(argv, argv[1:]))


def is_pytest(argv: tuple[str, ...]) -> bool:
    return "pytest" in argv


def is_isolated_run(argv: tuple[str, ...]) -> bool:
    return ("-m", "isolated") in pairs(argv)


def test_the_shipped_commands_are_all_launchable():
    """A shipped command `run_validation_commands` would REFUSE is worse than
    no command: it reports a failure the operator did not cause and cannot fix
    by fixing their code."""
    for argv in shipped_commands():
        binary = Path(argv[0]).name
        assert binary in SAFE_VALIDATION_BINARIES, (
            f"{binary!r} is not in SAFE_VALIDATION_BINARIES, so {argv!r} would be "
            "refused unrun"
        )


def test_the_shared_pytest_runs_are_parallel():
    shared = [a for a in shipped_commands() if is_pytest(a) and not is_isolated_run(a)]
    assert shared, "no shared pytest command left to parallelise"
    for argv in shared:
        assert ("-n", "auto") in pairs(argv), (
            f"{' '.join(argv)} lost `-n auto`; post-commit validation is back to "
            "running the whole suite serially on every round"
        )


def test_every_pytest_run_keeps_the_cache_plugin_off():
    for argv in shipped_commands():
        if not is_pytest(argv):
            continue
        assert ("-p", "no:cacheprovider") in pairs(argv), (
            f"{' '.join(argv)} lost `-p no:cacheprovider`; a failing test will write "
            ".pytest_cache into the worker repo and the tree-clean gate will refuse "
            "the commit a second time for validation's own residue"
        )


def test_the_isolated_marker_still_has_a_dedicated_command():
    isolated = [argv for argv in shipped_commands() if is_isolated_run(argv)]
    assert len(isolated) == 1, (
        "exactly one shipped command must run the `isolated` marker — "
        f"found {len(isolated)}. Zero means that coverage runs nowhere while "
        "every suite stays green."
    )


def test_the_isolated_command_stays_single_process():
    (argv,) = [a for a in shipped_commands() if is_isolated_run(a)]
    offenders = [
        token
        for token in argv
        if token.startswith("-n") or token.startswith("--numprocesses")
    ]
    assert not offenders, (
        f"the isolated run must not be parallel, found {offenders!r}. The marker "
        "means 'this test needs its own process'; handing it an xdist worker "
        "alongside others gives back exactly the company it was marked to avoid."
    )


def test_parallelism_is_never_hidden_in_addopts():
    """`-n auto` belongs on the individual command, not in `pytest.ini`.

    In `addopts` it would reach every invocation — including the dedicated
    isolated run above, and the `--collect-only` subprocess in
    `test_crash_safety.py` that asks pytest which tests carry the marker.
    """
    parser = configparser.ConfigParser()
    parser.read(REPO_ROOT / "pytest.ini")
    addopts = parser.get("pytest", "addopts", fallback="")
    assert " -n" not in f" {addopts}", f"parallelism leaked into addopts: {addopts!r}"
    assert "--numprocesses" not in addopts, f"same, spelled out: {addopts!r}"
    # The complement, and the reason the dedicated command has to exist at all.
    assert "not isolated" in addopts, f"default run must exclude it; addopts={addopts!r}"


def test_a_parallel_run_still_reports_pass_fail_per_command(tmp_path):
    """Parallelism lives inside one command; the summary is still one PASS/FAIL
    report per command, so a failure still names WHICH suite failed."""
    commands = shipped_commands()
    failing = next(a for a in commands if is_pytest(a) and not is_isolated_run(a))

    def fake_run(argv, **kwargs):
        if tuple(argv) == failing:
            return subprocess.CompletedProcess(
                argv, 1,
                stdout="FAILED autoloop/tests/test_x.py::test_y - boom\n1 failed\n",
                stderr="",
            )
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    ok, summary = run_validation_commands(commands, tmp_path, command_runner=fake_run)

    assert ok is False
    # Asserted per command rather than by counting segments: `failure_digest`
    # joins its own lines with "; " as well, so splitting the summary on that
    # separator would make the count depend on the interior text of a digest.
    assert f"{' '.join(failing)}: FAIL" in summary
    assert "test_x.py::test_y" in summary, "the failing test must still be named"
    for argv in commands:
        if argv == failing:
            continue
        assert f"{' '.join(argv)}: PASS" in summary
