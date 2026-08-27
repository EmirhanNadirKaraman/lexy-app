"""A completed task is judged by ANCESTRY, not by whether a commit subject
names it.

MEASURED 2026-08-25. `shipped-report` returned seven `completed_unwitnessed`
rows — bind-01, dash-02, dash-17, release-01, scope-02, split-01, split-02 —
and two of the seven had shipped:

    dash-02   archived record dash-02-reconciled-as-dd28dfa-<stamp>.json
    scope-02  live record executions/scope-02.json

Neither commit subject contains its task id, so the search behind that verdict
could not see them. The evidence was already on disk and already trusted one
module over: `merge_sweep` asks git about the sha in the execution record
instead of reading subjects, and answers all seven correctly.

The claim under test, in both halves:

  * a completed task whose record (live, or the newest archived generation)
    names an ancestor of the base head leaves the disagreement list; one whose
    record names a sha the base does not contain is the DEFINITE
    `completed_not_in_base`; and `completed_unwitnessed` is reachable only when
    nothing anywhere names a candidate;
  * the archive-generation rules exist in exactly ONE place —
    `merge_sweep.execution_record_ancestry` — and the sweep and the report get
    their answers from it, so the two cannot drift.

Real git wherever ancestry is the question, and the production writer
(`TaskExecutionStore`) wherever a record is. A hand-written JSON record would
prove the reader parses a shape nothing writes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest

from autoloop import cli, dashboard, merge_sweep
from autoloop.git_gateway import GitGateway
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.worktask import TaskExecution, TaskExecutionStore

#: A well-formed sha that resolves to nothing — used where the test is about
#: the RECORD rather than about git.
SHA_A = "a" * 40

#: Two archive labels in `retire_execution`'s own shape: `<reason>-<stamp>`,
#: stamp `YYYYMMDDTHHMMSSZ`. The reason contains `-`, exactly like a task id, so
#: these also exercise the "read the stamp off the END" rule.
OLDER = "released-by-operator-20260801T000000Z"
NEWER = "published-abc123def456-20260810T000000Z"


# --- helpers ------------------------------------------------------------------


def run_git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def make_repo(tmp_path, name="repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "work")
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "commit.gpgsign", "false")
    return repo


def commit(repo: Path, subject: str) -> str:
    (repo / "log.txt").write_text(subject + "\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", subject)
    return run_git(repo, "rev-parse", "HEAD").strip()


def side_commit(repo: Path, subject: str, branch: str) -> str:
    """A commit on its own branch, never merged — the "published, outstanding"
    shape. Returns to `work` so the base head is unchanged."""
    run_git(repo, "checkout", "-q", "-b", branch)
    sha = commit(repo, subject)
    run_git(repo, "checkout", "-q", "work")
    return sha


def head_of(repo: Path) -> str:
    return run_git(repo, "rev-parse", "HEAD").strip()


def roadmap_row(tid, status="completed", **kw):
    """A row shaped like `collect()`'s tolerant roadmap read."""
    row = {"id": tid, "title": f"Title {tid}", "status": status,
           "shipped_commits": [], "shipped_note": "", "shipped_at": ""}
    row.update(kw)
    return row


def write_record(executions: Path, task_id, candidate, *, published="",
                 archive_as=None) -> TaskExecutionStore:
    """One execution record, written by the loop's own writer and optionally
    retired into `executions/archive/` under `archive_as`."""
    store = TaskExecutionStore(executions)
    store.directory.mkdir(parents=True, exist_ok=True)
    store.save(TaskExecution(
        task_id=task_id,
        task_branch=f"autoloop/{task_id}",
        worktree_path="",
        task_base_sha=SHA_A,
        candidate_sha=candidate,
        published_sha=published,
    ))
    if archive_as:
        store.archive(task_id, archive_as)
    return store


def report(repo: Path, executions, *ids, head=None) -> dict:
    return dashboard.shipped_report(
        repo,
        [roadmap_row(tid) for tid in ids],
        head or head_of(repo),
        executions_dir=executions,
    )


def findings(out: dict) -> dict:
    return {row["id"]: row for row in out["disagreements"]["rows"]}


@pytest.fixture(autouse=True)
def _clean_dashboard_caches():
    """`is_ancestor` memoizes verdicts at module level and the commit-subject
    search is cached per repo. Two repositories built in the same wall-clock
    second with identical content share shas, so a verdict from an earlier test
    must not answer for a later one."""
    for cache in (dashboard._ANCESTRY_CACHE, dashboard._SHALLOW_CACHE,
                  dashboard._SUBJECT_CACHE):
        cache.clear()
    yield
    for cache in (dashboard._ANCESTRY_CACHE, dashboard._SHALLOW_CACHE,
                  dashboard._SUBJECT_CACHE):
        cache.clear()


# --- 1. the two measured rows that had shipped --------------------------------


def test_a_LIVE_record_naming_an_ancestor_takes_the_task_off_the_list(tmp_path):
    """scope-02, exactly: live record `executions/scope-02.json`, candidate
    d2d4d6b8c in the base, and not one commit subject naming the id."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    carrier = commit(repo, "one unresolvable changed path must not veto")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "scope-02", carrier)

    out = report(repo, executions, "scope-02")

    assert out["rows"][0]["state"] == "unknown", (
        "the SEARCH still finds nothing — subject matching became corroboration, "
        "not a thing that was made to succeed"
    )
    assert out["disagreements"]["rows"] == []
    assert [r["id"] for r in out["disagreements"]["witnessed"]] == ["scope-02"]
    assert carrier[:12] in out["disagreements"]["witnessed"][0]["detail"]
    assert out["disagreements"]["counts"]["completed_unwitnessed"] == 0


def test_an_ARCHIVED_record_naming_an_ancestor_does_the_same(tmp_path):
    """dash-02, exactly: the live record was retired by the reconciler as
    `dash-02-reconciled-as-dd28dfa-<stamp>.json`, and only the archive can say
    that the retirement happened over work that had already landed."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    carrier = commit(repo, "a subject that never names the task")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "dash-02", carrier,
                 archive_as="reconciled-as-dd28dfa-20260810T000000Z")

    out = report(repo, executions, "dash-02")

    assert out["disagreements"]["rows"] == []
    assert [r["id"] for r in out["disagreements"]["witnessed"]] == ["dash-02"]
    assert "archived" in out["disagreements"]["witnessed"][0]["detail"]


def test_the_published_sha_answers_when_the_candidate_cannot(tmp_path):
    """Either sha the record names settles it — `published_sha` is the field
    meaning "the remote confirmed this", and a record whose candidate the base
    cannot resolve is still answerable through it."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    carrier = commit(repo, "no id here either")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "pub-01", "", published=carrier)

    out = report(repo, executions, "pub-01")

    assert [r["id"] for r in out["disagreements"]["witnessed"]] == ["pub-01"]


# --- 2. the definite verdict, and the one that stays unproven -----------------


def test_a_record_naming_a_sha_the_base_lacks_is_a_PROVEN_disagreement(tmp_path):
    """The report already had the right word for this — it just never reached
    it without a commit subject to read."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    stranded = side_commit(repo, "work nobody merged", "autoloop/gone-01")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "gone-01", stranded)

    out = report(repo, executions, "gone-01")

    finding = findings(out)["gone-01"]
    assert finding["kind"] == "completed_not_in_base"
    assert finding["proven"] is True
    assert out["disagreements"]["proven"] == 1
    assert out["disagreements"]["witnessed"] == []


def test_only_a_task_with_NO_record_anywhere_stays_unwitnessed(tmp_path):
    """What the kind now means, and the whole reason it is still not proof: no
    subject names it AND nothing on disk names a candidate."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    commit(repo, "something else entirely")
    executions = tmp_path / "state" / "executions"
    executions.mkdir(parents=True)

    out = report(repo, executions, "split-02")

    finding = findings(out)["split-02"]
    assert finding["kind"] == "completed_unwitnessed"
    assert finding["proven"] is False
    assert "no execution record, live or archived" in finding["detail"]


def test_a_record_that_names_NO_candidate_is_unwitnessed_too(tmp_path):
    """A record naming neither sha names no branch to look for, which is the
    same answer as no record at all — and emphatically not `not-in-base`, which
    would claim git refuted something nobody claimed."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "blank-01", "")

    out = report(repo, executions, "blank-01")

    finding = findings(out)["blank-01"]
    assert finding["kind"] == "completed_unwitnessed"
    assert finding["proven"] is False
    assert "names no candidate" in finding["detail"]


def test_a_subject_that_names_it_and_is_outside_the_base_is_untouched(tmp_path):
    """The `not-in-base` branch is not re-decided by this change: it was already
    the definite verdict, reached from evidence of its own."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    side_commit(repo, "bind-01: the work nobody merged", "side")
    executions = tmp_path / "state" / "executions"
    executions.mkdir(parents=True)

    out = report(repo, executions, "bind-01")

    assert findings(out)["bind-01"]["kind"] == "completed_not_in_base"
    assert out["rows"][0]["state"] == "not-in-base"


# --- 3. the generation rules, reached through the report ----------------------


def test_an_OLDER_archived_retirement_cannot_answer_for_the_NEWEST(tmp_path):
    """The subtle half of the rule the report now shares. The released attempt
    landed and the retry that actually completed the task is still outstanding;
    judging on whichever copy names an ancestor would clear it."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    landed = commit(repo, "attempt one landed, unnamed")
    outstanding = side_commit(repo, "the retry, unnamed", "autoloop/twice-01-retry")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "twice-01", landed, archive_as=OLDER)
    write_record(executions, "twice-01", outstanding, archive_as=NEWER)

    out = report(repo, executions, "twice-01")

    finding = findings(out)["twice-01"]
    assert finding["kind"] == "completed_not_in_base"
    assert finding["proven"] is True
    assert NEWER in finding["detail"], "the operator is told WHICH generation"
    assert out["disagreements"]["witnessed"] == []


def test_the_NEWEST_generation_being_in_the_base_clears_a_retried_task(tmp_path):
    """The control that keeps the rule above from being merely strict: a
    superseded attempt is usually abandoned and must not be required to land."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    abandoned = side_commit(repo, "the released attempt", "autoloop/retried-01")
    shipped = commit(repo, "the retry landed, unnamed")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "retried-01", abandoned, archive_as=OLDER)
    write_record(executions, "retried-01", shipped, archive_as=NEWER)

    out = report(repo, executions, "retried-01")

    assert out["disagreements"]["rows"] == []
    assert [r["id"] for r in out["disagreements"]["witnessed"]] == ["retried-01"]


def test_another_TASKS_archived_copy_cannot_answer_through_a_shared_PREFIX(tmp_path):
    """`rt-1-*.json` matches `rt-1-b-published-<stamp>.json`, and the sibling's
    copy here carries the newer stamp — so without the owner check it BECOMES
    rt-1's newest generation and clears it with a commit that has nothing to do
    with rt-1."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    stranded = side_commit(repo, "the branch that never landed", "autoloop/rt-1")
    sibling = commit(repo, "the sibling landed")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "rt-1", stranded, archive_as="published-20260801T000000Z")
    write_record(executions, "rt-1-b", sibling, archive_as="published-20260810T000000Z")

    out = report(repo, executions, "rt-1")

    finding = findings(out)["rt-1"]
    assert finding["kind"] == "completed_not_in_base"
    assert "rt-1-b" not in finding["detail"], (
        "and the sibling's copy is not what it was judged on"
    )


# --- 4. "could not look" is never either verdict ------------------------------


def test_an_UNORDERABLE_archive_is_unchecked_rather_than_either_verdict(tmp_path):
    """An unstamped label could be the newest and there is no way to tell. That
    is not a clearance and it is not a disagreement — and above all it is not
    `unwitnessed`, which would say nothing names a candidate when two files
    do."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    landed = commit(repo, "attempt one landed")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "murky-01", landed, archive_as="released-by-operator")
    write_record(executions, "murky-01", landed, archive_as="published-20260810T000000Z")

    out = report(repo, executions, "murky-01")

    assert out["disagreements"]["rows"] == []
    assert out["disagreements"]["witnessed"] == []
    unchecked = out["disagreements"]["unverified"]
    assert [r["id"] for r in unchecked] == ["murky-01"]
    assert "cannot be put in order" in unchecked[0]["detail"]
    assert "murky-01-released-by-operator.json" in unchecked[0]["detail"]


def test_a_TORN_live_record_is_unchecked_never_unwitnessed(tmp_path):
    """The fail-open this whole section is written against: a record nobody can
    read names a candidate nobody can read, and calling that "nothing names a
    candidate" switches the alarm off exactly when it cannot see."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    executions = tmp_path / "state" / "executions"
    executions.mkdir(parents=True)
    (executions / "torn-01.json").write_text("{not json", encoding="utf-8")

    out = report(repo, executions, "torn-01")

    assert out["disagreements"]["rows"] == []
    assert [r["id"] for r in out["disagreements"]["unverified"]] == ["torn-01"]
    assert "could not be read" in out["disagreements"]["unverified"][0]["detail"]


def test_a_TORN_newest_archived_copy_is_unchecked_too(tmp_path):
    """Same rule one directory down: the newest generation is the one that
    answers, so a newest copy that will not load leaves the question open — an
    older readable one does not get to answer in its place."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    landed = commit(repo, "attempt one landed")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "torn-arch-01", landed, archive_as=OLDER)
    write_record(executions, "torn-arch-01", landed, archive_as=NEWER)
    (executions / "archive" / f"torn-arch-01-{NEWER}.json").write_text(
        "{not json", encoding="utf-8"
    )

    out = report(repo, executions, "torn-arch-01")

    assert out["disagreements"]["rows"] == []
    assert [r["id"] for r in out["disagreements"]["unverified"]] == ["torn-arch-01"]
    assert "newest archived copy could not be read" in (
        out["disagreements"]["unverified"][0]["detail"]
    )


def test_an_INDETERMINATE_ancestry_check_is_unchecked(tmp_path, monkeypatch):
    """A shallow clone, an object nobody fetched, an unreadable repository. The
    record names a candidate and git will not say — rounding that up clears an
    outstanding branch, rounding it down reports a healthy roadmap as broken."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "murk-02", SHA_A)
    monkeypatch.setattr(dashboard, "is_ancestor", lambda *a, **k: "unknown")

    out = report(repo, executions, "murk-02")

    assert out["disagreements"]["rows"] == []
    assert out["disagreements"]["witnessed"] == []
    assert [r["id"] for r in out["disagreements"]["unverified"]] == ["murk-02"]


def test_an_id_no_registry_could_have_issued_reads_no_file_at_all(tmp_path):
    """Both reads build a PATH out of the id, and the page's roadmap read is
    tolerant — it hands over whatever `tasks.json` holds, unvalidated. A
    separator or a leading dot must not send the lookup one directory up, so the
    id is refused before either read: this fails without the guard, because the
    file the traversal lands on is a real, readable, ancestral record."""
    executions = tmp_path / "state" / "executions"
    write_record(executions, "real-01", SHA_A)
    (executions.parent / "sneaky.json").write_text(
        json.dumps({"task_id": "sneaky", "candidate_sha": SHA_A}), encoding="utf-8"
    )

    answer = merge_sweep.execution_record_ancestry(
        executions, "../sneaky", lambda _sha: "yes"
    )

    assert answer.verdict == merge_sweep.RECORD_ABSENT
    assert "not a task id" in answer.detail
    for bogus in ("", "a/b", ".hidden", "..\\up"):
        assert merge_sweep.execution_record_ancestry(
            executions, bogus, lambda _sha: "yes"
        ).verdict == merge_sweep.RECORD_ABSENT


def test_a_reader_that_blows_up_leaves_the_row_UNCHECKED(tmp_path, monkeypatch):
    """The belt on a 2-second poll. `execution_record_ancestry` is written not
    to raise; if it ever does, the page must not 500 and the row must not be
    cleared or condemned on the strength of a crash."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "boom-01", SHA_A)

    def boom(*_args, **_kw):
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(merge_sweep, "execution_record_ancestry", boom)
    out = report(repo, executions, "boom-01")

    assert out["disagreements"]["rows"] == []
    assert out["disagreements"]["witnessed"] == []
    assert [r["id"] for r in out["disagreements"]["unverified"]] == ["boom-01"]
    assert "the disk went away" in out["disagreements"]["unverified"][0]["detail"]


def test_no_state_directory_says_so_instead_of_claiming_nothing_names_it(tmp_path):
    """`executions_dir=None` means the state directory could not be resolved.
    The row stays UNPROVEN — nothing here proves the work is missing — and it
    says that no record was consulted, rather than reading as "nothing on disk
    names a candidate", which nobody checked."""
    repo = make_repo(tmp_path)
    commit(repo, "init")

    out = report(repo, None, "orphan-01")

    finding = findings(out)["orphan-01"]
    assert finding["kind"] == "completed_unwitnessed"
    assert finding["proven"] is False
    assert "could not be resolved" in finding["detail"]


# --- 5. what ancestry proves, and what it does not ----------------------------


def test_an_OURS_supersede_reads_as_accounted_for_and_the_limit_is_STATED(tmp_path):
    """THE bound, executed rather than asserted in prose. `git merge -s ours`
    makes the candidate a genuine ancestor while taking NONE of its content —
    which is how bind-01, dash-17 and split-01 were recorded on 2026-08-25. Two
    of those three really are in the base under a successor's commits; split-01
    was discarded and split-04 redoes it, and nothing in git can tell them
    apart.

    So this passes deliberately: the branch IS accounted for. The docstrings are
    part of the test because the limit is only safe while the next reader is
    told about it — a report that answered "is this task's code present" would
    be wrong here.

    The real supersede subjects DID name their task ids, which is how the
    subject-only report reached the same wrong answer by a worse route. Here the
    subject names nothing, so the RECORD is what decides and the limit is the
    one being exercised rather than the search.
    """
    repo = make_repo(tmp_path)
    commit(repo, "init")
    run_git(repo, "checkout", "-q", "-b", "autoloop/split-01")
    (repo / "only-here.txt").write_text("content that was discarded\n", encoding="utf-8")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "work that was later discarded")
    discarded = head_of(repo)
    run_git(repo, "checkout", "-q", "work")
    run_git(repo, "merge", "-q", "-s", "ours", "--no-ff", "--no-edit", "-m",
            "supersede: took no content", discarded)
    executions = tmp_path / "state" / "executions"
    write_record(executions, "split-01", discarded)

    out = report(repo, executions, "split-01")

    assert [r["id"] for r in out["disagreements"]["witnessed"]] == ["split-01"]
    assert not (repo / "only-here.txt").exists(), (
        "the content really is absent — this is the case the verdict cannot see"
    )
    for text in (merge_sweep.RecordAncestry.__doc__,
                 dashboard.registry_disagreements.__doc__):
        # Case-folded on purpose: both docstrings SHOUT the words, and an
        # assertion that depended on which one shouts would fail on a rewording
        # that kept the claim.
        assert "accounted for" in text.lower()
        assert "-s ours" in text


# --- 6. exactly one implementation --------------------------------------------


def test_the_sweep_and_the_report_read_the_archive_through_the_SAME_function(
    tmp_path, monkeypatch
):
    """The extraction, pinned. Both callers are routed through
    `execution_record_ancestry`, so a change to the generation rules reaches
    both or neither — a second, divergent copy is a worse outcome than the bug
    this closed."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    landed = commit(repo, "unnamed carrier")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "both-01", landed, archive_as=NEWER)
    real = merge_sweep.execution_record_ancestry
    seen: list[str] = []

    def recording(directory, task_id, ancestry, **kw):
        seen.append(task_id)
        return real(directory, task_id, ancestry, **kw)

    monkeypatch.setattr(merge_sweep, "execution_record_ancestry", recording)
    report(repo, executions, "both-01")
    sweeper = merge_sweep.BacklogSweeper(
        config=None,
        git=GitGateway(repo, PolicyEngine(PolicyConfig())),
        policy=None,
        execution_store=TaskExecutionStore(executions),
        registry=None,
        log=lambda *a, **kw: None,
        merger=object(),
    )
    integrated, why_not = sweeper._retired_publication_is_integrated(
        head_of(repo), "both-01"
    )

    assert seen == ["both-01", "both-01"], "one function, both callers"
    assert integrated is True and why_not == ""


def test_the_two_readers_reach_the_SAME_verdict_on_an_outstanding_branch(tmp_path):
    """The same state, both questions. The sweep must hold the backlog and the
    report must call it a proven disagreement — one archive, one answer."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    outstanding = side_commit(repo, "unnamed and unmerged", "autoloop/hold-01")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "hold-01", outstanding, archive_as=NEWER)
    sweeper = merge_sweep.BacklogSweeper(
        config=None,
        git=GitGateway(repo, PolicyEngine(PolicyConfig())),
        policy=None,
        execution_store=TaskExecutionStore(executions),
        registry=None,
        log=lambda *a, **kw: None,
        merger=object(),
    )

    integrated, why_not = sweeper._retired_publication_is_integrated(
        head_of(repo), "hold-01"
    )
    out = report(repo, executions, "hold-01")

    assert integrated is False
    assert "merge it by hand" in why_not
    assert findings(out)["hold-01"]["kind"] == "completed_not_in_base"


def test_the_verdict_vocabulary_is_the_sweeps_own(tmp_path):
    """The dashboard mirrors the four verdicts rather than importing them at
    module load. Pinned value for value, so a rename there fails here instead of
    silently sending every completed row down the `else` branch."""
    assert (dashboard.RECORD_IN_BASE, dashboard.RECORD_NOT_IN_BASE,
            dashboard.RECORD_UNVERIFIED, dashboard.RECORD_ABSENT) == (
        merge_sweep.RECORD_VERDICTS
    )


def test_looking_writes_nothing(tmp_path):
    """Read-only, and it must stay so: this runs against a checkout the loop is
    writing, and the escape detector parks the loop over a stray write."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    carrier = commit(repo, "unnamed carrier")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "quiet-01", carrier, archive_as=NEWER)
    before = {p: p.read_bytes() for p in sorted(executions.rglob("*"))
              if p.is_file()}

    report(repo, executions, "quiet-01")

    after = {p: p.read_bytes() for p in sorted(executions.rglob("*"))
             if p.is_file()}
    assert after == before
    assert run_git(repo, "status", "--porcelain") == ""


# --- 7. the operator command, end to end --------------------------------------


def _configure(repo: Path, state_dir: Path, workers_root: Path) -> Path:
    """The loop's own config, in the checkout, where `dashboard._state_dir`
    reads it from and `--config` points the CLI at it. One file, so the report's
    resolution and the loop's cannot name different directories."""
    config_path = repo / ".autoloop" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "[paths]\n"
        f'state_dir = "{state_dir}"\n'
        f'workers_root = "{workers_root}"\n',
        encoding="utf-8",
    )
    return config_path


def test_shipped_report_reads_the_records_of_the_checkout_it_is_pointed_at(tmp_path):
    """DONE WHEN, through the real command: dash-02 and scope-02 read as shipped
    rather than unwitnessed, and the task whose record names an outstanding sha
    is the one finding left."""
    repo = make_repo(tmp_path, "checkout")
    commit(repo, "init")
    carrier = commit(repo, "a subject naming no task at all")
    outstanding = side_commit(repo, "still on its branch", "autoloop/gone-01")
    state_dir = tmp_path / "state"
    config_path = _configure(repo, state_dir, tmp_path / "outside" / "workers")
    executions = state_dir / "executions"
    write_record(executions, "scope-02", carrier)
    write_record(executions, "dash-02", carrier,
                 archive_as="reconciled-as-dd28dfa-20260810T000000Z")
    write_record(executions, "gone-01", outstanding)
    registry = TaskRegistry([
        Task(id=tid, title=f"Title {tid}", description="d",
             approved_paths=("docs/A.md",))
        for tid in ("scope-02", "dash-02", "gone-01")
    ])
    for tid in ("scope-02", "dash-02", "gone-01"):
        registry.mark_completed(tid)
    state_dir.mkdir(parents=True, exist_ok=True)
    TaskStore(state_dir / "tasks.json").save(registry)

    code = cli._cmd_shipped_report(argparse.Namespace(
        config=config_path, repo=repo, base=head_of(repo)
    ))

    assert code == 1, "the one task whose branch is outstanding is a finding"
    out = dashboard.shipped_report(repo, [
        {"id": t.id, "title": t.title, "status": t.status, "shipped_commits": [],
         "shipped_note": "", "shipped_at": ""}
        for t in registry.all_tasks()
    ], head_of(repo))
    assert set(findings(out)) == {"gone-01"}, (
        "dash-02 and scope-02 shipped; the report can now say which"
    )
    assert {r["id"] for r in out["disagreements"]["witnessed"]} == {
        "dash-02", "scope-02"
    }


def test_the_PRINTED_row_says_the_record_is_what_cleared_it(tmp_path):
    """A row the record cleared leaves the disagreement list — and that is the
    one row whose evidence would otherwise appear NOWHERE an operator reads: it
    still prints as NO MENTION (the search really did find nothing, and that
    state is not rewritten), and the disagreements block below is where it is
    now absent. Absence plus silence reads exactly like "we stopped looking",
    so the row's own detail carries the record's answer."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    carrier = commit(repo, "a subject naming no task at all")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "scope-02", carrier)

    out = report(repo, executions, "scope-02")
    text = "\n".join(cli._format_shipped(out))

    assert out["rows"][0]["state"] == "unknown", "the SEARCH's answer is unchanged"
    assert out["counts"]["unknown"] == 1 and out["counts"]["shipped"] == 0
    assert "NO MENTION" in text and "scope-02" in text
    assert "accounted for in the base" in text
    assert "registry / code disagreements: 0 (0 proven)" in text


def test_the_report_resolves_the_state_directory_the_LOOP_writes(tmp_path):
    """No `executions_dir` handed over: the report asks `config.resolve_state_
    dir`, the same rule `load_config` uses, so the records it reads are the ones
    the loop writes. A second rule here is port-06's bug rebuilt."""
    repo = make_repo(tmp_path, "checkout")
    commit(repo, "init")
    carrier = commit(repo, "no task id in this subject")
    state_dir = tmp_path / "elsewhere" / "state"
    _configure(repo, state_dir, tmp_path / "outside" / "workers")
    write_record(state_dir / "executions", "found-01", carrier)

    out = dashboard.shipped_report(repo, [roadmap_row("found-01")], head_of(repo))

    assert [r["id"] for r in out["disagreements"]["witnessed"]] == ["found-01"]


def test_an_unconfigured_checkout_still_reports_and_says_it_read_nothing(tmp_path):
    """`_state_dir` RAISES on a checkout with no config, and the report must
    neither crash nor pretend it consulted a record."""
    repo = make_repo(tmp_path, "checkout")
    commit(repo, "init")
    commit(repo, "nothing to do with any task")

    out = dashboard.shipped_report(repo, [roadmap_row("nowhere-01")], head_of(repo))

    finding = findings(out)["nowhere-01"]
    assert finding["kind"] == "completed_unwitnessed"
    assert "could not be resolved" in finding["detail"]


def test_the_live_panel_reads_records_from_the_pages_own_state_directory(tmp_path):
    """`collect` resolves the state directory once for the whole page and hands
    it down; `disagreement_report` must take that hand-over rather than
    resolving a second time."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    carrier = commit(repo, "unnamed carrier")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "panel-01", carrier)

    out = dashboard.disagreement_report(
        repo, [roadmap_row("panel-01")], head_of(repo), executions_dir=executions
    )

    assert out["rows"] == []
    assert [r["id"] for r in out["witnessed"]] == ["panel-01"]


def test_a_poll_that_did_not_search_still_judges_nothing_from_a_record(tmp_path):
    """With the subject search skipped every completed row is `unverified` —
    "could not look" — and the record must not promote one of them to a verdict
    behind that. The two evidence sources answer different questions and the
    weaker state wins."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    carrier = commit(repo, "unnamed carrier")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "quiet-02", carrier)

    out = dashboard.disagreement_report(
        repo, [roadmap_row("quiet-02")], head_of(repo), None,
        executions_dir=executions,
    )

    assert out["searched"] is False
    assert out["rows"] == [] and out["witnessed"] == []
    assert [r["id"] for r in out["unverified"]] == ["quiet-02"]


def test_the_payload_carries_the_witnessed_rows_so_none_of_them_vanish(tmp_path):
    """A row that LEAVES the disagreement list is listed rather than dropped:
    "no disagreement" and "resolved by the record" must stay distinguishable, or
    the next reader cannot tell a clean report from one that stopped looking."""
    repo = make_repo(tmp_path)
    commit(repo, "init")
    carrier = commit(repo, "unnamed carrier")
    executions = tmp_path / "state" / "executions"
    write_record(executions, "kept-01", carrier)

    out = report(repo, executions, "kept-01")
    payload = json.loads(json.dumps(out["disagreements"]))

    assert payload["witnessed"][0]["id"] == "kept-01"
    assert payload["witnessed"][0]["title"] == "Title kept-01"
