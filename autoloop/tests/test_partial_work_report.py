"""What an UNCOMMITTED round reports about the work it actually produced.

The gap these pin (exec-01, measured 2026-08-16/17): a round that fails
validation, times out, or whose agent errors produces no commit and no packet,
so the reviewer answers from the summary alone — and that summary named only
the CAUSE. brw-11 wrote 2,347 insertions across four attempts, committed none,
and drew four identical `revise` directives, while an operator reading the
worker diff called the task too big in one look. The reviewer had nothing to
reach that conclusion with.

So the claim under test is narrow and has three halves:

* an uncommitted round states the FILE COUNT, the LINE COUNT and WHICH FILES;
* every one of those comes from git in the worker repo, never from the agent's
  own account of itself (`test_the_numbers_contradict_the_agents_own_claim` is
  the one that actually demonstrates this, by making the agent lie);
* a round that commits is byte-for-byte unchanged.

`build_executor`, the repo fixtures and the fake agents are borrowed from
`test_implement_executor` — the same sibling import `test_agent_self_validation`
already does. No `claude` CLI and no real validation binary ever runs.
"""

import re
from pathlib import Path

import pytest

from autoloop.audit.agents import AgentResult
from autoloop.implement_executor import (
    PARTIAL_WORK_MAX_PATH_CHARS,
    PARTIAL_WORK_MAX_PATHS,
    _bounded_paths,
    _partial_work_note,
)
from autoloop.stall import STALLED, PartialWork, StallReport
from autoloop.tasks import Task

# Sibling test module, importable because pytest's prepend import mode puts this
# directory on `sys.path` — the same borrowing `test_agent_self_validation.py`
# already does from this exact module.
from test_implement_executor import (
    build_executor,
    fail_command,
    implement_directive,
    make_agent_runner_factory,
    make_task,
    ok_command,
    run_git,
)

RUFF = (("ruff", "check", "."),)


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


#: The provenance clause the section carries. It is not decoration: the same
#: packet renders the agent's own report right next to this, and a reviewer can
#: only weigh the two differently if the text says which is which.
FROM_GIT = "not from the agent's report"


def failing_validation_round(main_repo, worker_repo, write_files, raw_text="done"):
    """One whole `execute()` round that writes `write_files` and then fails
    validation — the exact shape that produces no candidate and no packet."""
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files=write_files, raw_text=raw_text),
        validation=RUFF,
        command_runner=fail_command,
    )
    return executor.execute(implement_directive(), make_task())


def lines_reported(summary: str) -> int:
    match = re.search(r"~(\d+) line\(s\) written", summary)
    assert match, f"no line count in: {summary}"
    return int(match.group(1))


# ---- 1: a round that fails validation ---------------------------------------


def test_a_failed_validation_round_reports_files_lines_and_which_files(
    main_repo, worker_repo
):
    outcome = failing_validation_round(
        main_repo,
        worker_repo,
        {"feature.py": "print('hi')\n", "pkg/mod.py": "a\nb\nc\n"},
    )

    assert outcome.status == "error"
    # The CAUSE is still there — this adds evidence, it replaces nothing.
    assert "validation failed after implementation" in outcome.summary
    assert "FAIL" in outcome.validation
    # ...and now the WORK.
    assert "Partial work left in the worker repository" in outcome.summary
    assert "2 file(s) changed" in outcome.summary
    assert lines_reported(outcome.summary) == 4
    # WHICH files: the discriminator the counts cannot be.
    assert "feature.py" in outcome.summary
    assert "pkg/mod.py" in outcome.summary
    assert FROM_GIT in outcome.summary


def test_the_line_count_includes_edits_to_already_tracked_files(main_repo, worker_repo):
    """New files are counted by reading them; edits to a tracked file are
    counted from `git diff HEAD --stat`. A report that saw only the first would
    read as "barely started" for a round that rewrote an existing module."""
    outcome = failing_validation_round(main_repo, worker_repo, {"README.md": "hi\nthere\nagain\n"})

    assert "1 file(s) changed" in outcome.summary
    assert lines_reported(outcome.summary) >= 2
    assert "README.md" in outcome.summary


def test_the_numbers_contradict_the_agents_own_claim(main_repo, worker_repo):
    """THE test for "evidence, not the agent's summary of itself". The agent
    reports a sweeping change it never made; the round reports the one file it
    really wrote. Nothing the agent said appears in the evidence section."""
    outcome = failing_validation_round(
        main_repo,
        worker_repo,
        {"feature.py": "print('hi')\n"},
        raw_text=(
            "I made excellent progress: 900 lines across 40 files, mostly in "
            "services/invented.py. Nearly done."
        ),
    )

    assert "1 file(s) changed" in outcome.summary
    assert lines_reported(outcome.summary) == 1
    assert "services/invented.py" not in outcome.summary
    assert "40 file" not in outcome.summary
    # The agent's account is still carried — clearly labelled, and elsewhere.
    assert "services/invented.py" in outcome.details


# ---- 2: a round whose agent errored ------------------------------------------


def test_an_agent_error_round_reports_what_existed_at_that_point(main_repo, worker_repo):
    """The agent dies part-way through. Whatever it had already written is
    still on disk, and that — not "the agent failed" — is what tells a reviewer
    whether the next round starts from 600 lines or from zero."""

    class DyingAgent:
        def __init__(self, root):
            self.root = root

        def run(self, spec):
            (self.root / "half_written.py").write_text("a\nb\n")
            return AgentResult(
                domain=spec.domain, raw_text="", returncode=1, duration_seconds=1.0,
                command=("claude",), error="agent exploded",
            )

    executor = build_executor(main_repo, worker_repo, lambda root: DyingAgent(root), validation=RUFF)
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "error"
    assert "agent exploded" in outcome.summary
    assert "1 file(s) changed" in outcome.summary
    assert lines_reported(outcome.summary) == 2
    assert "half_written.py" in outcome.summary
    assert "Validation did not run." in outcome.summary
    assert outcome.changed_paths == ("half_written.py",)


def test_a_killed_round_gains_the_paths_without_a_second_set_of_counts(
    main_repo, worker_repo
):
    """A stall/ceiling kill — the timeout case. `StallReport.describe` already
    states the counts it measured at kill time, so a second, separately-measured
    pair would be two numbers for one fact. It has never carried PATHS, though,
    so those are added: new information, not a re-measurement."""
    report = StallReport(
        verdict=STALLED, elapsed_seconds=4210.0, silent_seconds=1801.0,
        stall_seconds=1800.0, ceiling_seconds=14400.0,
        partial=PartialWork(files_changed=1, lines_written=3),
    )

    class KilledAgent:
        def __init__(self, root):
            self.root = root

        def run(self, spec):
            (self.root / "half_written.py").write_text("a\nb\nc\n")
            return AgentResult(
                domain=spec.domain, raw_text="", returncode=-15,
                duration_seconds=report.elapsed_seconds, command=("claude",),
                error=report.describe(), stall=report,
            )

    executor = build_executor(main_repo, worker_repo, lambda root: KilledAgent(root), validation=RUFF)
    outcome = executor.execute(implement_directive(), make_task())

    assert "STALLED" in outcome.summary
    assert "half_written.py" in outcome.summary
    assert outcome.summary.count("file(s) changed") == 1


# ---- 3: the branches that are deliberately NOT touched -----------------------


def test_a_round_that_commits_is_unchanged(main_repo, worker_repo):
    """The success path carries no partial-work section at all. It is also the
    branch whose summary becomes the commit message, so a bounded-but-growing
    path list there would end up in the repository's history."""
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "print('hi')\n"}),
        validation=RUFF,
        command_runner=ok_command,
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "ok"
    assert outcome.summary.startswith(
        "task 't1' implemented: 1 file(s) changed; validation passed."
    )
    assert "Partial work left in the worker repository" not in outcome.summary
    assert "Paths it touched" not in outcome.summary
    assert FROM_GIT not in outcome.summary
    assert "feature.py" not in outcome.summary


def test_a_round_that_changed_nothing_makes_one_claim_not_two(main_repo, worker_repo):
    """Reaching this branch means the status read SUCCEEDED and returned
    nothing, which the existing sentence already states exactly. A
    `PartialWork` line here would restate it, not add evidence."""
    executor = build_executor(
        main_repo, worker_repo, make_agent_runner_factory(write_files={}), validation=RUFF
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert outcome.status == "error"
    assert "changed no files" in outcome.summary
    assert "Partial work left in the worker repository" not in outcome.summary


def test_a_missing_validation_cwd_round_reports_its_partial_work(main_repo, worker_repo):
    """The third no-candidate branch. Nothing is committed from here either, so
    the work leaves no trace the reviewer can read except this."""
    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "print('hi')\n"}),
        validation=RUFF,
        command_runner=ok_command,
    )
    task = Task(
        id="t1", title="t", description="d",
        validation=RUFF, validation_cwd="does/not/exist",
    )
    outcome = executor.execute(implement_directive(), task)

    assert outcome.status == "error"
    assert "validation_cwd" in outcome.summary
    assert "1 file(s) changed" in outcome.summary
    assert "feature.py" in outcome.summary


# ---- 4: the fail-open cases --------------------------------------------------


def test_an_unmeasurable_repo_reports_unknown_and_never_zero():
    """The fail-open this section could have shipped: an unreadable worker repo
    yields an empty path tuple, and rendering that as an empty list would say
    "the agent wrote nothing" when the truth is "we could not look"."""
    note = _partial_work_note((), PartialWork(measured=False, note="git status failed"))

    assert "UNKNOWN" in note
    assert "git status failed" in note
    assert "NOT a report of zero files" in note
    assert "NONE" not in note
    assert "0 file" not in note


def test_paths_are_still_named_when_only_the_counts_are_unknown():
    """The two measurements are separate git reads, so one can fail alone.
    Evidence available in one dimension is not thrown away because the other
    is missing."""
    note = _partial_work_note(("a.py", "b.py"), PartialWork(measured=False, note="diff failed"))

    assert "UNKNOWN" in note
    assert "a.py" in note and "b.py" in note


def test_counts_without_paths_say_the_list_is_missing_rather_than_empty():
    """The other half of the same seam: the counts read fine and the path read
    did not. Twelve files with no names printed silently would read as "and it
    touched nothing", the opposite of what the count says."""
    note = _partial_work_note((), PartialWork(files_changed=12, lines_written=340))

    assert "12 file(s) changed" in note
    assert "UNKNOWN" in note
    assert "NOT a report of zero files" in note


def test_a_measured_zero_is_not_reported_as_unknown():
    note = _partial_work_note((), PartialWork(files_changed=0, lines_written=0))

    assert "NONE" in note
    assert "UNKNOWN" not in note


def test_the_path_list_is_bounded_and_says_how_many_it_left_out():
    """A silent truncation reads exactly like complete coverage."""
    paths = tuple(f"pkg/module_{i:03d}.py" for i in range(PARTIAL_WORK_MAX_PATHS + 14))
    rendered = _bounded_paths(paths)

    assert rendered.count(", ") == PARTIAL_WORK_MAX_PATHS  # 19 separators + the tail
    assert "and 14 more not listed" in rendered
    assert "pkg/module_000.py" in rendered


def test_the_path_list_is_bounded_by_length_as_well_as_by_count():
    paths = ("a/" * 400 + "deep.py", "second.py")
    rendered = _bounded_paths(paths)

    # At least one path is always named — "and 2 more" with no example tells
    # the reviewer nothing about WHERE the work landed.
    assert rendered.startswith("a/a/")
    assert "and 1 more not listed" in rendered
    assert "second.py" not in rendered
    assert len(paths[0]) > PARTIAL_WORK_MAX_PATH_CHARS


def test_no_paths_and_no_note_renders_nothing_extra():
    assert _partial_work_note((), PartialWork(), with_counts=False) == ""


# ---- 5: the measurement is of the AGENT's tree, and only the worker repo -----


def test_validations_own_residue_is_not_counted_as_the_agents_work(
    main_repo, worker_repo
):
    """`ruff` can leave a cache directory that `git status -uall` reports. The
    measurement is taken BEFORE the authoritative run for exactly this reason:
    a count taken after it would fold validation's writes into the agent's and
    disagree with the `changed_paths` the same outcome carries."""

    def messy_failing_runner(argv, cwd=None, **kwargs):
        cache = Path(cwd) / ".ruff_cache"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "CACHEDB").write_text("x\n" * 50)

        class Proc:
            returncode = 1
            stdout = ""
            stderr = "boom\n"

        return Proc()

    executor = build_executor(
        main_repo,
        worker_repo,
        make_agent_runner_factory(write_files={"feature.py": "print('hi')\n"}),
        validation=RUFF,
        command_runner=messy_failing_runner,
    )
    outcome = executor.execute(implement_directive(), make_task())

    assert (worker_repo / ".ruff_cache" / "CACHEDB").exists()  # it really ran
    assert ".ruff_cache" not in outcome.summary
    assert "1 file(s) changed" in outcome.summary
    assert outcome.changed_paths == ("feature.py",)


def test_reporting_writes_nothing_outside_the_worker_repo(main_repo, worker_repo):
    """This is a read: `git status` and `git diff` in the worker repo, plus
    reading new files there. Nothing about it may touch the main checkout."""
    before = sorted(p.relative_to(main_repo).as_posix() for p in main_repo.rglob("*"))

    outcome = failing_validation_round(
        main_repo, worker_repo, {"feature.py": "print('hi')\n", "pkg/mod.py": "a\n"}
    )

    assert "2 file(s) changed" in outcome.summary
    after = sorted(p.relative_to(main_repo).as_posix() for p in main_repo.rglob("*"))
    assert before == after
