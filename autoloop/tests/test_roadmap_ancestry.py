"""Has a completed task's work actually landed? Asked of GIT, not of source.

The question is narrow on purpose, and the narrowness IS the feature: given a
completed task id, is there a commit whose SUBJECT names that id, and is that
commit an ancestor of the base head. Nothing else is claimed — not that the
capability exists, not that it works, not that the task can be retired.

Why this shape. A previous attempt answered "has this shipped?" by parsing
Python for definitions and behaviour, and reached its attempt ceiling with the
reviewer still finding false-positive shapes of source (the last: in a multiline
signature, every continuation line after `def …(` is indented deeper and reads
as a body). That surface is unbounded. Ancestry is decidable.

The evidence that this is the useful question, from 2026-08-17: four completed
tasks held every merge sweep because no record named the work they shipped, and
all four were resolved by hand with exactly this query — `audit-0001` at
07b659b, `dash-02` at dd28dfa, `pkt-02` at 95b77a1, and `pkt-03`, which had
shipped as four "pkt-03, part N" commits.

Every test here builds a REAL repository and runs real git. The one thing a
mocked git could not show is the thing worth pinning: what the classifier does
with a commit that exists on a ref but is not an ancestor.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoloop.cli import SHIPPED_LABELS, _format_shipped, build_parser
from autoloop.dashboard import (
    COMMIT_ANCESTRY,
    SHIPPED_STATES,
    commit_subjects,
    mentions_task_id,
    resolve_commit,
    shipped_report,
    shipped_states,
)


@pytest.fixture(autouse=True)
def _clean_dashboard_caches():
    """`is_ancestor` memoizes verdicts at module level, and `make_repo` commits
    fixed content with a fixed author — two repositories built in the same
    wall-clock second therefore share a sha in different directories. The cache
    key carries the repo path, so this is belt-and-braces; it also keeps a
    verdict from an earlier test out of a later one's report."""
    import autoloop.dashboard as dash

    for cache in (dash._ANCESTRY_CACHE, dash._SHALLOW_CACHE):
        cache.clear()
    yield
    for cache in (dash._ANCESTRY_CACHE, dash._SHALLOW_CACHE):
        cache.clear()


def run_git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def make_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    run_git(repo, "init", "-q", "-b", "work")
    run_git(repo, "config", "user.email", "t@e.com")
    run_git(repo, "config", "user.name", "T")
    return repo


def commit(repo: Path, subject: str, body: str = "") -> str:
    """One commit with `subject` as its subject line; returns the full sha."""
    stamp = str(len(list(repo.glob("f*.txt"))))
    (repo / f"f{stamp}.txt").write_text(body or subject + "\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", subject)
    return run_git(repo, "rev-parse", "HEAD").strip()


def head_of(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD").strip()


def completed(*ids: str) -> list[dict]:
    return [{"id": task_id, "title": f"{task_id} title", "status": "completed"} for task_id in ids]


def row_for(report: dict, task_id: str) -> dict:
    matches = [r for r in report["rows"] if r["id"] == task_id]
    assert matches, f"{task_id} has no row: {report['rows']}"
    return matches[0]


def snapshot(root: Path) -> dict:
    return {p: p.stat().st_mtime_ns for p in Path(root).rglob("*") if p.is_file()}


# ---- the three answers -------------------------------------------------------


def test_a_task_whose_commit_is_an_ancestor_reports_shipped_and_names_the_sha(tmp_path):
    """The 2026-08-17 case: the work landed, nothing recorded that it had, and
    the sha is the answer an operator needs to see."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    sha = commit(repo, "pkt-02: extract the packet builder")
    commit(repo, "unrelated follow-up")

    report = shipped_report(repo, completed("pkt-02"), head_of(repo))

    row = row_for(report, "pkt-02")
    assert row["state"] == "shipped"
    assert sha[:12] in row["detail"]
    assert [c["full"] for c in row["commits"]] == [sha]
    assert row["commits"][0]["ancestry"] == "in-base"
    assert report["counts"]["shipped"] == 1


def test_a_matching_commit_outside_the_base_reports_not_in_base(tmp_path):
    """The commit exists and its subject names the task — it is simply not in
    the base. That is a real negative, and the only one this report ever makes:
    it rests on git resolving the commit and answering no."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    base = head_of(repo)
    run_git(repo, "checkout", "-q", "-b", "side")
    side_sha = commit(repo, "dash-02: a side branch nobody merged")
    run_git(repo, "checkout", "-q", "work")

    report = shipped_report(repo, completed("dash-02"), base)

    row = row_for(report, "dash-02")
    assert row["state"] == "not-in-base"
    assert [c["full"] for c in row["commits"]] == [side_sha]
    assert row["commits"][0]["ancestry"] == "not-in-base"


def test_no_matching_commit_reports_unknown_and_never_not_shipped(tmp_path):
    """The fail-open reading this whole report exists to avoid. A task id that
    appears in no commit subject is UNKNOWN: the work may have shipped under a
    subject that never names the id, and calling that "not shipped" is how a
    report becomes a licence to redo work that already landed."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    commit(repo, "something else entirely")

    report = shipped_report(repo, completed("audit-0001"), head_of(repo))

    row = row_for(report, "audit-0001")
    assert row["state"] == "unknown"
    assert row["state"] != "not-in-base"
    assert row["commits"] == []
    assert "not" in row["detail"].lower() and "evidence" in row["detail"].lower()


# ---- matching the id as a WHOLE token ----------------------------------------


def test_a_longer_id_containing_the_task_id_is_not_a_match(tmp_path):
    """`pkt-03` must not match `pkt-030`, `x-pkt-03` or `pkt-03.5` — each is a
    different, equally valid registry id under `tasks._ID_RE`."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    commit(repo, "pkt-030: a different task")
    commit(repo, "x-pkt-03: another different task")
    commit(repo, "pkt-03.5: a third one")

    report = shipped_report(repo, completed("pkt-03"), head_of(repo))

    row = row_for(report, "pkt-03")
    assert row["state"] == "unknown", row["commits"]
    assert row["commits"] == []


def test_the_neighbouring_ids_still_match_themselves(tmp_path):
    """The boundary rule must not be so strict that a real id stops matching
    its own commit — the same walk that refuses `pkt-030` for `pkt-03` has to
    report `pkt-030` as shipped."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    sha = commit(repo, "pkt-030: a different task")

    report = shipped_report(repo, completed("pkt-030"), head_of(repo))

    row = row_for(report, "pkt-030")
    assert row["state"] == "shipped"
    assert [c["full"] for c in row["commits"]] == [sha]


@pytest.mark.parametrize(
    "subject, expected",
    [
        ("pkt-03: do the thing", True),
        ("pkt-03, part 1", True),
        ("Merge task pkt-03 (0fcc1c6) into autoloop/mainline", True),
        ("fix pkt-03", True),
        ("(pkt-03)", True),
        ("pkt-030: a neighbour", False),
        ("x-pkt-03: a neighbour", False),
        ("pkt-03.5: a neighbour", False),
        ("pkt-03-followup: a neighbour", False),
        ("PKT-03: shouting", False),
        ("", False),
    ],
)
def test_whole_token_matching(subject, expected):
    """Case-sensitive, bounded on both sides by the id alphabet. The trailing
    full stop is the documented cost: `.` is a legal id character, so
    "…pkt-03." does not match — conservative in the safe direction (a missed
    mention is `unknown`, never a false `shipped`), where dropping `.` from the
    class would make `pkt-03.5` match `pkt-03`."""
    assert mentions_task_id(subject, "pkt-03") is expected


def test_a_trailing_full_stop_is_the_documented_miss():
    """Pinned so the cost is visible rather than discovered. If someone widens
    the boundary class later, this test is where the trade-off is restated."""
    assert mentions_task_id("shipped as pkt-03.", "pkt-03") is False
    assert mentions_task_id("shipped as pkt-03 .", "pkt-03") is True


# ---- keeping every piece of evidence -----------------------------------------


def test_every_part_commit_of_a_multipart_task_is_retained(tmp_path):
    """`pkt-03` shipped as four "pkt-03, part N" commits. One aggregate verdict
    is the answer; four rows of evidence are what makes it checkable."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    shas = [commit(repo, f"pkt-03, part {n}") for n in (1, 2, 3, 4)]

    report = shipped_report(repo, completed("pkt-03"), head_of(repo))

    row = row_for(report, "pkt-03")
    assert row["state"] == "shipped"
    assert sorted(c["full"] for c in row["commits"]) == sorted(shas)
    assert {c["ancestry"] for c in row["commits"]} == {"in-base"}
    assert "4 of 4" in row["detail"]


def test_a_split_verdict_keeps_both_commits_with_their_own_classification(tmp_path):
    """One commit in the base and one not is `shipped` — some of the work
    landed — and the row still shows which is which, because the aggregate is a
    reading of the list and never a replacement for it."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    merged = commit(repo, "pkt-03, part 1")
    base = head_of(repo)
    run_git(repo, "checkout", "-q", "-b", "side")
    stranded = commit(repo, "pkt-03, part 2")
    run_git(repo, "checkout", "-q", "work")

    report = shipped_report(repo, completed("pkt-03"), base)

    row = row_for(report, "pkt-03")
    assert row["state"] == "shipped"
    by_sha = {c["full"]: c["ancestry"] for c in row["commits"]}
    assert by_sha == {merged: "in-base", stranded: "not-in-base"}
    assert "1 of 2" in row["detail"]


def test_a_merge_commit_subject_is_evidence_like_any_other(tmp_path):
    """The loop's own integration subjects name the task ("Merge task dash-14
    (17adda7efd76) into autoloop/mainline"), and for a task whose work commits
    say nothing that merge is the only mention there is."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    run_git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "some work with no id in the subject")
    run_git(repo, "checkout", "-q", "work")
    run_git(repo, "merge", "--no-ff", "-q", "-m", "Merge task dash-14 into autoloop/mainline", "side")
    merge_sha = head_of(repo)

    report = shipped_report(repo, completed("dash-14"), merge_sha)

    row = row_for(report, "dash-14")
    assert row["state"] == "shipped"
    assert [c["full"] for c in row["commits"]] == [merge_sha]


# ---- "git could not answer" is a third thing ---------------------------------


def test_an_indeterminate_ancestry_check_is_unverified_not_absence():
    """Step 4 of the plan, and the failure mode it guards: an ancestry check
    that cannot be determined must not become `unknown` (which claims a search
    found no mention) or `not-in-base` (which claims a negative)."""
    rows = shipped_states(
        completed("pkt-02"),
        [("a" * 40, "pkt-02: the work")],
        lambda sha: "unknown",
    )

    assert rows[0]["state"] == "unverified"
    assert rows[0]["commits"][0]["ancestry"] == "unverified"
    assert "could not resolve" in rows[0]["detail"]


def test_one_indeterminate_check_does_not_erase_a_definite_ancestor():
    """Definite evidence wins. A row with one confirmed ancestor and one
    unreadable commit is shipped, and the unreadable one is still listed."""
    good, bad = "a" * 40, "b" * 40
    verdicts = {good: "yes", bad: "unknown"}
    rows = shipped_states(
        completed("pkt-02"),
        [(good, "pkt-02: the work"), (bad, "pkt-02: more work")],
        lambda sha: verdicts[sha],
    )

    assert rows[0]["state"] == "shipped"
    assert {c["ancestry"] for c in rows[0]["commits"]} == {"in-base", "unverified"}


def test_a_failed_subject_search_is_unverified_not_unknown():
    """`None` from the search means git would not answer. Reporting that as
    `unknown` would claim a search that never ran — the exact fail-open reading
    the report exists to avoid, one level up from the ancestry check."""
    rows = shipped_states(completed("pkt-02"), None, lambda sha: "yes")

    assert rows[0]["state"] == "unverified"
    assert rows[0]["commits"] == []
    assert "could not be searched" in rows[0]["detail"]


def test_the_search_returns_none_rather_than_empty_on_an_unreadable_repo(tmp_path):
    """The distinction above only survives if the git-facing half preserves it.
    `_run` collapses failure and empty output to `""`; this path must not."""
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()

    assert commit_subjects(not_a_repo) is None


def test_a_report_over_an_unreadable_repo_judges_nothing(tmp_path):
    """End to end: every row unverified, `searched` false, and not one row
    saying anything about whether the work landed."""
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()

    report = shipped_report(not_a_repo, completed("pkt-02", "pkt-03"), "deadbeef")

    assert report["searched"] is False
    assert report["counts"]["unverified"] == 2
    assert {r["state"] for r in report["rows"]} == {"unverified"}


def test_resolve_commit_answers_empty_for_a_rev_that_names_nothing(tmp_path):
    repo = make_repo(tmp_path)
    sha = commit(repo, "init")

    assert resolve_commit(repo, "HEAD") == sha
    assert resolve_commit(repo, "no-such-branch") == ""
    assert resolve_commit(repo, "") == ""


# ---- scope: report only, act never -------------------------------------------


def test_only_completed_tasks_are_reported(tmp_path):
    """A pending task's id in a landed commit says nothing this report is
    entitled to say — the question is asked of COMPLETED tasks."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    commit(repo, "pkt-02: the work")
    roadmap = [
        {"id": "pkt-02", "title": "done", "status": "completed"},
        {"id": "pkt-09", "title": "queued", "status": "pending"},
        {"id": "pkt-10", "title": "gone", "status": "retired"},
    ]

    report = shipped_report(repo, roadmap, head_of(repo))

    assert [r["id"] for r in report["rows"]] == ["pkt-02"]


def test_the_report_writes_nothing_to_the_repository(tmp_path):
    """Read-only is load-bearing: the loop's escape detector parks a run when
    the primary checkout is dirty, so a report that refreshed the index while
    observing would stop the thing it observes."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    commit(repo, "pkt-02: the work")
    before = snapshot(repo)

    shipped_report(repo, completed("pkt-02"), head_of(repo))

    assert snapshot(repo) == before
    assert run_git(repo, "status", "--porcelain").strip() == ""


def test_every_per_commit_verdict_comes_from_the_pinned_vocabulary(tmp_path):
    """The per-commit axis has three values and only three — `unverified` is
    the one that must never be spelled as a fourth thing, since it is what keeps
    "git could not answer" out of the negative column."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    commit(repo, "pkt-02: the work")
    run_git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "pkt-02: more work")
    run_git(repo, "checkout", "-q", "work")

    report = shipped_report(repo, completed("pkt-02"), head_of(repo))

    verdicts = {c["ancestry"] for c in row_for(report, "pkt-02")["commits"]}
    assert verdicts and verdicts <= set(COMMIT_ANCESTRY)


def test_every_state_the_report_can_produce_has_a_label():
    """The four states and their operator-facing words are pinned against each
    other, so a fifth state cannot print as `?`."""
    assert set(SHIPPED_LABELS) == set(SHIPPED_STATES)


def test_the_cli_wires_a_read_only_report_command():
    from autoloop.cli import _cmd_shipped_report

    args = build_parser().parse_args(["shipped-report"])

    assert args.func is _cmd_shipped_report
    assert args.base == "HEAD"
    assert args.repo is None


def test_the_printed_report_shows_every_commit_and_the_caveat():
    """A payload carrying the evidence is not a report showing it. This is the
    display path: each matching commit prints with its own verdict, and the
    "no mention is not evidence" caveat is printed rather than implied."""
    report = {
        "base_branch": "autoloop/mainline",
        "base_head": "23f6829d9ad0",
        "searched": True,
        "searched_commits": 412,
        "counts": {"shipped": 1, "not-in-base": 0, "unverified": 0, "unknown": 1},
        "rows": [
            {
                "id": "pkt-03", "title": "packets", "state": "shipped",
                "detail": "0fcc1c6abcde names it and is an ancestor of the base head",
                "commits": [
                    {"sha": "0fcc1c6abcde", "full": "0fcc1c6abcde" + "0" * 28,
                     "subject": "pkt-03, part 1", "ancestry": "in-base"},
                    {"sha": "95b77a1fedcb", "full": "95b77a1fedcb" + "0" * 28,
                     "subject": "pkt-03, part 2", "ancestry": "not-in-base"},
                ],
            },
            {
                "id": "dash-02", "title": "dashboard", "state": "unknown",
                "detail": "no commit subject names this id — absence of a mention is NOT evidence",
                "commits": [],
            },
        ],
    }

    text = "\n".join(_format_shipped(report))

    assert "0fcc1c6abcde" in text and "95b77a1fedcb" in text
    assert "pkt-03, part 2" in text
    assert "in-base" in text and "not-in-base" in text
    assert "SHIPPED" in text and "NO MENTION" in text
    assert "not evidence" in text
    assert "412 commit subject(s)" in text


def test_a_failed_search_says_so_once_at_the_top():
    """With the search failed, every row is unverified for one shared reason.
    The report says that once rather than eleven times, and says it before the
    rows so nobody reads them as verdicts."""
    report = {
        "base_branch": "HEAD", "base_head": "23f6829d9ad0",
        "searched": False, "searched_commits": 0,
        "counts": {"shipped": 0, "not-in-base": 0, "unverified": 1, "unknown": 0},
        "rows": [{"id": "pkt-02", "title": "t", "state": "unverified",
                  "detail": "commit subjects could not be searched", "commits": []}],
    }

    lines = _format_shipped(report)

    assert "could NOT be searched" in lines[1]
    assert "UNVERIFIED" in "\n".join(lines)
