"""Two branches recording a change note must merge without a conflict.

The failure, measured 2026-08-18: every task records what it changed in
`docs/SUMMARY.md` and `docs/TESTS.md`, so any two parallel branches touch the
same region of the same two files. The merge sweep halted three times in one
evening on exactly that and left five reviewed, published tasks unmerged for a
full day (dash-10, loop-02, brw-12, hlth-01, wrk-01), each resolved by hand.
One of those hand-resolutions is still visible in `docs/SUMMARY.md`'s
`orchestrator.py` row: ~4,500 characters duplicated and a sentence spliced in
half. That is the cost this file exists to stop recurring.

The fix has two halves and both are pinned here, because either alone is
useless:

  * `.gitattributes` gives the two trackers `merge=union`, so a conflicting
    hunk keeps BOTH sides instead of stopping the sweep;
  * `CLAUDE.md` §12 makes a change note ONE NEW LINE and forbids growing an
    existing one, because union resolves per LINE — two branches that grow the
    same line duplicate that whole line rather than merging the two additions.

`test_growing_the_same_giant_row_on_both_branches_duplicates_it` demonstrates
that second point on a 19,000-character row instead of asserting it, because
the brief that ordered this work said the union attribute is not sufficient on
its own and told us to verify the claim before relying on it.

Real git throughout, self-contained helpers, matching this package's test
convention (see `test_postcommit_primitives.py`'s docstring for why the small
`run_git` helpers are duplicated rather than imported).

**The fixture copies the repository's OWN `.gitattributes` into the test repo
rather than writing `merge=union` inline.** Writing the rule the test is about
would only prove that git implements union; copying the shipped file proves
THIS repository is configured, and fails if a later change deletes, renames or
narrows the rule.

The base branch is `work`, not `main`, matching `test_merge_sweep.py`: nothing
here pushes, but keeping the convention means a test moved between the two
files does not silently start exercising the protected-branch refusal.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from autoloop.git_gateway import GitGateway
from autoloop.policy import PolicyConfig, PolicyEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
GITATTRIBUTES = REPO_ROOT / ".gitattributes"
SUMMARY_DOC = REPO_ROOT / "docs" / "SUMMARY.md"
TESTS_DOC = REPO_ROOT / "docs" / "TESTS.md"
CLAUDE_DOC = REPO_ROOT / "CLAUDE.md"

BASE = "work"

#: The marker every tracker's append-only section ends with. Tasks append
#: BELOW it, at the end of the file.
NOTES_MARKER = "<!-- CHANGE-NOTES:"

#: A note line that grew past this is the shape the whole fix exists to
#: prevent. Deliberately generous — this is a guard against a row heading back
#: towards 19,410 characters, not a style rule about sentence length.
MAX_NOTE_LINE = 700


# --- helpers ------------------------------------------------------------------


def run_git(cwd, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def try_git(cwd, *args) -> subprocess.CompletedProcess:
    """Unchecked — used where a NON-zero exit is the thing being asserted."""
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def head(repo) -> str:
    return run_git(repo, "rev-parse", "HEAD").strip()


def is_clean(repo) -> bool:
    return not run_git(repo, "status", "--porcelain").strip()


def write(repo, rel: str, text: str) -> None:
    target = Path(repo) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def read(repo, rel: str) -> str:
    return (Path(repo) / rel).read_text(encoding="utf-8")


SUMMARY_SEED = """# SUMMARY.md

| Path | Purpose |
|---|---|
| `main.py` | FastAPI app. |

## Change notes — append ONE new line at the END of this file

<!-- CHANGE-NOTES: append below, one line per note, at the END of the file. Never edit a line above. -->

| Date | Task | Note |
|---|---|---|
| 2026-08-18 | seed-00 | the note that was already there |
"""

TESTS_SEED = """# TESTS.md

| File | What it covers |
|---|---|
| `test_auth.py` | register, login |

## Change notes — append ONE new line at the END of this file

<!-- CHANGE-NOTES: append below, one line per note, at the END of the file. Never edit a line above. -->

| Date | Task | Note |
|---|---|---|
| 2026-08-18 | seed-00 | the note that was already there |
"""


def build(tmp_path, extra_files=None):
    """A repo carrying the REAL `.gitattributes` plus seeded trackers."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", BASE)
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "commit.gpgsign", "false")
    # THE point of this fixture: the shipped rule, not one written here.
    write(repo, ".gitattributes", GITATTRIBUTES.read_text(encoding="utf-8"))
    write(repo, "docs/SUMMARY.md", SUMMARY_SEED)
    write(repo, "docs/TESTS.md", TESTS_SEED)
    for rel, content in (extra_files or {}).items():
        write(repo, rel, content)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")
    return repo


def note_line(task_id: str) -> str:
    return f"| 2026-08-19 | {task_id} | what {task_id} changed |\n"


def branch(repo, name, base_sha, edits) -> str:
    """Cut `name` from `base_sha`, apply `edits` (rel -> new text), commit.

    Leaves the checkout back on the base branch, so the caller merges into a
    base that never moved under it.
    """
    run_git(repo, "checkout", "-q", "-b", name, base_sha)
    for rel, text in edits.items():
        write(repo, rel, text)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", f"work on {name}")
    sha = head(repo)
    run_git(repo, "checkout", "-q", BASE)
    return sha


def recording_a_note(repo, task_id: str) -> dict:
    """The edit EVERY task makes: one new line at the end of both trackers."""
    return {
        "docs/SUMMARY.md": read(repo, "docs/SUMMARY.md") + note_line(task_id),
        "docs/TESTS.md": read(repo, "docs/TESTS.md") + note_line(task_id),
    }


def merge(repo, sha, message="integrate") -> subprocess.CompletedProcess:
    return try_git(repo, "merge", "--no-ff", "--no-edit", "-m", message, sha)


def attribute_rules() -> list[str]:
    return [
        " ".join(line.split())
        for line in GITATTRIBUTES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def notes_section(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    assert text.count(NOTES_MARKER) == 1, f"{path.name} must carry exactly one notes marker"
    return text.split(NOTES_MARKER, 1)[1].splitlines()[1:]


# --- the configuration --------------------------------------------------------


def test_the_repo_ships_a_union_rule_for_exactly_the_two_note_trackers():
    """Narrow on purpose. Union NEVER reports a conflict, so every path listed
    here trades away genuine edit/edit detection — acceptable for an
    append-only note ledger, not for anything else. A wildcard would extend
    that trade to files nobody decided about."""
    assert attribute_rules() == [
        "docs/SUMMARY.md merge=union",
        "docs/TESTS.md merge=union",
    ]
    assert not any("*" in rule for rule in attribute_rules())


# --- the claim: parallel notes merge -------------------------------------------


def test_two_branches_each_recording_a_note_merge_without_conflict(tmp_path):
    """The provable claim, in the shape the loop actually produces it: two
    branches cut from ONE base, each appending its own note to BOTH trackers,
    merged one after the other into that base."""
    repo = build(tmp_path)
    base = head(repo)

    first = branch(repo, "autoloop/task-a", base, recording_a_note(repo, "task-a"))
    second = branch(repo, "autoloop/task-b", base, recording_a_note(repo, "task-b"))

    assert merge(repo, first).returncode == 0
    result = merge(repo, second)
    assert result.returncode == 0, result.stdout + result.stderr

    for rel in ("docs/SUMMARY.md", "docs/TESTS.md"):
        text = read(repo, rel)
        assert "<<<<<<<" not in text
        assert note_line("task-a") in text, rel
        assert note_line("task-b") in text, rel
        # The note that was there before both branches is untouched.
        assert "| 2026-08-18 | seed-00 | the note that was already there |" in text
    assert is_clean(repo)


def test_three_branches_recording_notes_merge_in_sequence(tmp_path):
    """Two is the pair that broke; three is the property. A union merge leaves
    the accumulated notes in ARRIVAL order, so this asserts membership and
    never position."""
    repo = build(tmp_path)
    base = head(repo)
    ids = ("task-a", "task-b", "task-c")
    shas = [branch(repo, f"autoloop/{name}", base, recording_a_note(repo, name)) for name in ids]

    for sha in shas:
        result = merge(repo, sha)
        assert result.returncode == 0, result.stdout + result.stderr

    for rel in ("docs/SUMMARY.md", "docs/TESTS.md"):
        text = read(repo, rel)
        assert "<<<<<<<" not in text
        for name in ids:
            assert text.count(note_line(name)) == 1, f"{name} in {rel}"
    assert is_clean(repo)


def test_the_production_merge_call_honours_the_union_rule(tmp_path):
    """Through `GitGateway.merge_commit` — the call `auto_merge._merge` makes —
    rather than a bare `git merge`, so this cannot pass against a rule the
    sweep's own invocation would bypass."""
    repo = build(tmp_path)
    base = head(repo)
    first = branch(repo, "autoloop/task-a", base, recording_a_note(repo, "task-a"))
    second = branch(repo, "autoloop/task-b", base, recording_a_note(repo, "task-b"))

    git = GitGateway(repo, PolicyEngine(PolicyConfig(auto_merge_enabled=True)))
    git.merge_commit(first, "Merge task-a")
    git.merge_commit(second, "Merge task-b")

    assert git.conflicted_paths() == []
    for rel in ("docs/SUMMARY.md", "docs/TESTS.md"):
        text = read(repo, rel)
        assert note_line("task-a") in text and note_line("task-b") in text


# --- the premise the note shape rests on ---------------------------------------


def test_growing_the_same_giant_row_on_both_branches_duplicates_it(tmp_path):
    """`merge=union` is NOT sufficient on its own — verified, not assumed.

    Union resolves per LINE. On the shape this repo actually had (one table
    row per module, the longest 19,410 characters), two branches that each
    append their sentence INSIDE that row do not get their additions merged:
    the whole row is duplicated, once per side. Both notes survive, but the
    document now carries two ~19KB rows that disagree, and a reader cannot
    tell which is current. That is why a note is a new LINE.
    """
    giant = "| `orchestrator.py` | " + ("Persisted state machine. " * 780) + "|"
    repo = build(tmp_path, {"docs/SUMMARY.md": SUMMARY_SEED.replace(
        "| `main.py` | FastAPI app. |", giant
    )})
    assert len(giant) > 19_000
    base = head(repo)

    def grown(with_note: str) -> dict:
        text = read(repo, "docs/SUMMARY.md")
        return {"docs/SUMMARY.md": text.replace(giant, giant[:-1] + with_note + " |")}

    first = branch(repo, "autoloop/task-a", base, grown("APPENDED-BY-A."))
    second = branch(repo, "autoloop/task-b", base, grown("APPENDED-BY-B."))

    assert merge(repo, first).returncode == 0
    result = merge(repo, second)
    # No conflict — and that is precisely the problem, not the fix.
    assert result.returncode == 0, result.stdout + result.stderr

    rows = [ln for ln in read(repo, "docs/SUMMARY.md").splitlines()
            if ln.startswith("| `orchestrator.py` |")]
    assert len(rows) == 2, "union duplicated the row rather than merging the two additions"
    assert sum("APPENDED-BY-A." in row for row in rows) == 1
    assert sum("APPENDED-BY-B." in row for row in rows) == 1
    assert not any("APPENDED-BY-A." in row and "APPENDED-BY-B." in row for row in rows)


def test_editing_the_same_note_line_duplicates_it_instead_of_conflicting(tmp_path):
    """The accepted trade, pinned rather than left to be discovered.

    Git has no way to scope a merge driver to a REGION of a file, so union on
    these two trackers applies to the whole file: two branches that rewrite the
    SAME existing line get both versions concatenated and no warning. That is
    survivable for an append-only note ledger (the duplicate is visible prose
    in a documentation file) and is exactly why the rule is "never edit a line
    someone else wrote" and why no source file carries this attribute.
    """
    repo = build(tmp_path)
    base = head(repo)
    original = "| 2026-08-18 | seed-00 | the note that was already there |"

    def rewritten(text_for: str) -> dict:
        return {"docs/SUMMARY.md": read(repo, "docs/SUMMARY.md").replace(
            original, f"| 2026-08-18 | seed-00 | {text_for} |"
        )}

    first = branch(repo, "autoloop/task-a", base, rewritten("A's version"))
    second = branch(repo, "autoloop/task-b", base, rewritten("B's version"))

    assert merge(repo, first).returncode == 0
    assert merge(repo, second).returncode == 0

    text = read(repo, "docs/SUMMARY.md")
    assert "<<<<<<<" not in text
    assert "A's version" in text and "B's version" in text


# --- what must still conflict --------------------------------------------------


def test_a_real_conflict_in_a_source_file_still_stops_the_merge(tmp_path):
    """The bound on the whole change: only append-only change notes are in
    scope. A genuine source conflict must still stop the sweep, and the abort
    must leave the base exactly where it was."""
    repo = build(tmp_path, {"autoloop/thing.py": "TIMEOUT = 30\n"})
    base = head(repo)

    first = branch(repo, "autoloop/task-a", base, {"autoloop/thing.py": "TIMEOUT = 60\n"})
    second = branch(repo, "autoloop/task-b", base, {"autoloop/thing.py": "TIMEOUT = 90\n"})

    assert merge(repo, first).returncode == 0
    before = head(repo)
    result = merge(repo, second)

    assert result.returncode != 0
    assert "CONFLICT" in result.stdout + result.stderr
    assert "<<<<<<<" in read(repo, "autoloop/thing.py")
    conflicted = [ln[3:] for ln in run_git(repo, "status", "--porcelain").splitlines()
                  if ln.startswith("UU ")]
    assert conflicted == ["autoloop/thing.py"]

    run_git(repo, "merge", "--abort")
    assert head(repo) == before
    assert is_clean(repo)


def test_a_documentation_file_outside_the_rule_still_conflicts(tmp_path):
    """The rule is two paths, and this is what "narrow" buys: the same
    append-at-EOF edit that merges cleanly in the trackers still conflicts in
    a doc nobody granted union to."""
    repo = build(tmp_path, {"docs/TODO.md": "# TODO\n\n- one\n"})
    base = head(repo)

    first = branch(repo, "autoloop/task-a", base,
                   {"docs/TODO.md": read(repo, "docs/TODO.md") + "- from A\n"})
    second = branch(repo, "autoloop/task-b", base,
                    {"docs/TODO.md": read(repo, "docs/TODO.md") + "- from B\n"})

    assert merge(repo, first).returncode == 0
    result = merge(repo, second)
    assert result.returncode != 0
    assert "CONFLICT" in result.stdout + result.stderr
    run_git(repo, "merge", "--abort")


# --- the shape the instructions promise ----------------------------------------


def test_both_trackers_end_with_an_append_only_change_note_section():
    """A note is appended at the END of the file, so the section has to BE the
    end of the file — nothing may follow it that a later append would land
    inside."""
    for path in (SUMMARY_DOC, TESTS_DOC):
        lines = notes_section(path)
        assert lines, f"{path.name} has nothing after its notes marker"
        assert not any(ln.startswith("#") for ln in lines), (
            f"{path.name} has a heading after the notes marker — appends would land inside it"
        )
        tail = [ln for ln in lines if ln.strip()]
        assert tail[-1].startswith("| "), f"{path.name} must end on a note row"
        assert "| Date | Task | Note |" in tail


def test_every_change_note_line_is_short_enough_to_merge_by_line():
    """One note, one line. A note line that has grown into a paragraph is the
    old failure returning — union cannot merge two additions to one line."""
    for path in (SUMMARY_DOC, TESTS_DOC):
        for line in notes_section(path):
            assert len(line) <= MAX_NOTE_LINE, (
                f"{path.name}: a change note grew to {len(line)} chars — "
                "split it into a second line instead"
            )


def test_claude_md_tells_a_task_to_append_one_line():
    """The instruction has to match the shape, or the next task recreates the
    collision. Checked against `CLAUDE.md` because that is what an agent reads
    before it records anything."""
    text = CLAUDE_DOC.read_text(encoding="utf-8")
    assert "Record a change note as ONE NEW LINE" in text
    assert "Never grow an existing line" in text
    assert "merge=union" in text and "`.gitattributes`" in text
    assert "Change notes" in text
    assert "autoloop/tests/test_docs_merge.py" in text


def test_the_split_summary_row_kept_every_note_it_carried():
    """`state.py`, `transcript.py` was ONE 2,400-character row carrying four
    dated notes; it is now four lines, one note each. Pinned by the notes'
    own text, so a split that dropped or merged one fails here — the point of
    restructuring was that nothing is lost by it.
    """
    text = SUMMARY_DOC.read_text(encoding="utf-8")
    rows = [ln for ln in text.splitlines() if ln.startswith("| `state.py`, `transcript.py` |")]
    # `>=`, not `==`: §12 tells the next task that touches these modules to add
    # its own row, so an exact count would red the first time somebody OBEYS
    # the instruction this file exists to enforce. The no-loss property is
    # carried by the fragment counts below; `>= 4` still fails if the four
    # lines are collapsed back into one.
    assert len(rows) >= 4, "one dated note per line"
    for fragment in (
        "Atomic crash-safe JSON state",
        "append-only JSONL audit log.",
        "**2026-07-31, deliberately NOT a schema bump:**",
        "rotations == 0",
        "**2026-08-14, also not a schema bump (pkt-01):**",
        "`Phase.DELIVERING`, `LoopState.outbox_diff`",
        "**2026-08-16, also not a schema bump (auto-04):**",
        "`LoopState.stop_kind` + `stop_blocker_id`",
    ):
        assert text.count(fragment) == 1, f"{fragment!r} was lost or duplicated by the split"
    assert sum(len(row) for row in rows) > 2_000
