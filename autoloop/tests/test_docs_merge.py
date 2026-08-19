"""Two branches recording a change note must merge — and nothing else may.

The failure, measured 2026-08-18: every task records what it changed in
`docs/SUMMARY.md` and `docs/TESTS.md`, so any two parallel branches touch the
same region of the same two files. The merge sweep halted three times in one
evening on exactly that and left five reviewed, published tasks unmerged for a
full day (dash-10, loop-02, brw-12, hlth-01, wrk-01), each resolved by hand.
One of those hand-resolutions is still visible in `docs/SUMMARY.md`'s
`orchestrator.py` row: ~4,500 characters duplicated and a sentence spliced in
half. That is the cost this file exists to stop recurring.

The fix has two halves and both are pinned here:

  * each tracker ends with an append-only **change-note section**, and
    `CLAUDE.md` §12 makes a note ONE NEW LINE appended at the very end of it;
  * `autoloop/note_merge.py`, wired into `auto_merge.AutoMerger._merge`,
    combines two branches' appended lines — and ONLY those.

**Almost every test here runs through the production merge path**
(`merge_sweep.sweep_backlog` → `AutoMerger.attempt` → `_merge`), against a real
checkout with a real origin, because that is the invocation that was halting.
A bare `git merge` of the same two branches still conflicts, which is asserted
too: the resolver is doing this, not git, and general merge behaviour was not
weakened to get it.

`merge=union` was shipped for this on 2026-08-19 and removed the same day. Two
tests demonstrate why against real git rather than asserting it — union
duplicates a grown 19,000-character row instead of merging the two additions,
and union silently concatenates a genuine prose conflict that must stop the
sweep. Both write the attribute INLINE, since the repository deliberately no
longer ships it.

Real git throughout, self-contained helpers, matching this package's test
convention (see `test_postcommit_primitives.py`'s docstring for why the small
`run_git` helpers are duplicated rather than imported).

The base branch is `work`, not `main`, matching `test_merge_sweep.py`:
`protected_branches` defaults to `("main", "master")`, so a `main` base would
exercise `push_exact`'s protected refusal rather than the integration path.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autoloop import merge_sweep, note_merge
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.git_gateway import GitGateway
from autoloop.note_merge import NOTES_MARKER, resolve_note_append
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import Task, TaskRegistry, TaskStore
from autoloop.worktask import TaskExecution, TaskExecutionStore

REPO_ROOT = Path(__file__).resolve().parents[2]
GITATTRIBUTES = REPO_ROOT / ".gitattributes"
SUMMARY_DOC = REPO_ROOT / "docs" / "SUMMARY.md"
TESTS_DOC = REPO_ROOT / "docs" / "TESTS.md"
CLAUDE_DOC = REPO_ROOT / "CLAUDE.md"

BASE = "work"
BASE_REF = f"refs/heads/{BASE}"
URL = "https://chatgpt.com/c/docs-merge"

TRACKERS = ("docs/SUMMARY.md", "docs/TESTS.md")

#: A note line that grew past this is the shape the whole fix exists to
#: prevent. Deliberately generous — this is a guard against a row heading back
#: towards 19,410 characters, not a style rule about sentence length. The
#: RESOLVER deliberately enforces no such cap (a long note would then halt the
#: sweep); this is the documentation-shape half.
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


def ref_sha(repo, ref) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", ref], cwd=str(repo), capture_output=True, text=True
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def contains(repo, descendant, ancestor) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=str(repo), capture_output=True, text=True,
    ).returncode == 0


def is_clean(repo) -> bool:
    return not run_git(repo, "status", "--porcelain").strip()


def write(repo, rel: str, text: str) -> None:
    target = Path(repo) / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def read(repo, rel: str) -> str:
    return (Path(repo) / rel).read_text(encoding="utf-8")


# --- the seeded trackers ------------------------------------------------------
#
# Same shape as the real files: ordinary documentation, then a terminal
# append-only section opened by the notes marker, then a table whose last rows
# are notes tasks already recorded.

MARKER_LINE = f"{NOTES_MARKER} append below, one line per note, at the END of the file. -->"

PROSE_ROW = "| `main.py` | FastAPI app. |"
SEED_NOTE_A = "| 2026-08-17 | seed-00 | the note that was already there |"
SEED_NOTE_B = "| 2026-08-18 | seed-01 | the second note that was already there |"


def seed(title: str) -> str:
    return (
        f"# {title}\n"
        "\n"
        "| Path | Purpose |\n"
        "|---|---|\n"
        f"{PROSE_ROW}\n"
        "| `state.py` | Crash-safe JSON state. |\n"
        "\n"
        "## Change notes — append ONE new line at the END of this file\n"
        "\n"
        f"{MARKER_LINE}\n"
        "\n"
        "| Date | Task | Note |\n"
        "|---|---|---|\n"
        f"{SEED_NOTE_A}\n"
        f"{SEED_NOTE_B}\n"
    )


SUMMARY_SEED = seed("SUMMARY.md")
TESTS_SEED = seed("TESTS.md")


def note_line(task_id: str) -> str:
    return f"| 2026-08-19 | {task_id} | what {task_id} changed |\n"


# --- the production harness ---------------------------------------------------


class Trackers:
    """A checkout, its origin, and one published branch per task — the exact
    state `merge_sweep.sweep_backlog` is written to find and integrate.

    Modelled on `test_merge_sweep.py`'s `Backlog`, trimmed to what these tests
    need. `publish` gives every record an explicit `published_at` so the
    sweep's oldest-publication-first order is fixed rather than resolved by a
    committer-date tie-break inside the same second.
    """

    def __init__(self, repo, origin, config):
        self.repo = repo
        self.origin = origin
        self.config = config
        self.execution_store = TaskExecutionStore(config.executions_dir)
        self.task_store = TaskStore(config.tasks_file)
        self.registry = TaskRegistry()
        self.task_store.save(self.registry)
        self._published = 0

    def publish(self, task_id, files, *, base=None):
        """One completed task whose reviewed candidate is on its own branch on
        origin and is NOT in the base. `base` defaults to the CURRENT head, so
        callers that want parallel branches pass the same base to each."""
        base_sha = base or self.head()
        branch = f"autoloop/{task_id}"
        ref = f"refs/heads/{branch}"
        run_git(self.repo, "checkout", "-q", "-b", branch, base_sha)
        for rel, text in files.items():
            write(self.repo, rel, text)
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-q", "-m", f"work for {task_id}")
        sha = self.head()
        run_git(self.repo, "push", "-q", "origin", f"{sha}:{ref}")
        run_git(self.repo, "checkout", "-q", BASE)

        self._published += 1
        self.execution_store.save(
            TaskExecution(
                task_id=task_id,
                task_branch=branch,
                worktree_path="",
                task_base_sha=base_sha,
                candidate_sha=sha,
                review_round=1,
                intended_remote="origin",
                intended_remote_ref=ref,
                published_sha=sha,
                published_at=f"2026-08-19T0{self._published}:00:00Z",
            )
        )
        self.registry.add_many([Task(id=task_id, title=f"Title {task_id}", description="d")])
        self.registry.mark_completed(task_id)
        self.task_store.save(self.registry)
        return sha

    def recording_a_note(self, task_id):
        """The edit EVERY task makes: one new line at the end of both
        trackers, everything above it untouched."""
        return {rel: seed_for(rel) + note_line(task_id) for rel in TRACKERS}

    # -- running it --

    def git(self):
        return GitGateway(self.repo, PolicyEngine(self.config.policy))

    def sweep(self):
        return merge_sweep.sweep_backlog(self.config, git=self.git())

    # -- observing it --

    def entries(self, entry_type=None):
        path = self.config.transcript_file
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if entry_type is None or r["type"] == entry_type]

    def head(self):
        return head(self.repo)

    def origin_base(self):
        return ref_sha(self.origin, BASE_REF)

    def read(self, rel):
        return read(self.repo, rel)


def seed_for(rel: str) -> str:
    return SUMMARY_SEED if rel.endswith("SUMMARY.md") else TESTS_SEED


def build(tmp_path, *, extra_files=None):
    repo = tmp_path / "repo"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", BASE)
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "commit.gpgsign", "false")
    write(repo, "docs/SUMMARY.md", SUMMARY_SEED)
    write(repo, "docs/TESTS.md", TESTS_SEED)
    for rel, content in (extra_files or {}).items():
        write(repo, rel, content)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    run_git(repo, "remote", "add", "origin", str(origin))
    run_git(repo, "push", "-q", "-u", "origin", BASE)

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(auto_merge_enabled=True),
        state_dir=tmp_path / ".al",
        workers_root=tmp_path / "workers",
    )
    config.state_dir.mkdir(parents=True, exist_ok=True)
    return Trackers(repo, origin, config)


def assert_stopped_after_first(t, first, second):
    """The sweep integrated `first` and refused `second`, leaving the checkout
    somewhere it can be reasoned about: no conflict markers on disk, no
    half-merge, and the base exactly where the first merge left it."""
    assert contains(t.repo, t.head(), first), "the first branch should have merged"
    assert not contains(t.repo, t.head(), second), "the second must NOT have landed"
    assert is_clean(t.repo), "an aborted merge must leave the checkout clean"
    assert t.entries("auto_merge_conflict"), "the conflict must be reported"


# --- the claim: parallel notes merge through the production path --------------


def test_two_branches_each_recording_a_note_merge_through_the_sweep(tmp_path):
    """The provable claim, in the shape the loop actually produces it: two
    branches cut from ONE base, each appending its own note to BOTH trackers,
    integrated one after the other by the real merge sweep."""
    t = build(tmp_path)
    base = t.head()

    first = t.publish("task-a", t.recording_a_note("task-a"), base=base)
    second = t.publish("task-b", t.recording_a_note("task-b"), base=base)

    t.sweep()

    assert contains(t.repo, t.head(), first)
    assert contains(t.repo, t.head(), second)
    assert contains(t.repo, t.head(), base), "without discarding what was there"
    assert is_clean(t.repo)
    assert t.origin_base() == t.head(), "the merged base must be PUSHED, not merely merged"
    for rel in TRACKERS:
        text = t.read(rel)
        assert "<<<<<<<" not in text
        assert text.count(note_line("task-a")) == 1, rel
        assert text.count(note_line("task-b")) == 1, rel
        # Everything that was there before both branches is untouched.
        assert text.count(SEED_NOTE_A) == 1 and text.count(SEED_NOTE_B) == 1, rel
        assert text.count(PROSE_ROW) == 1, rel
        assert text.count(MARKER_LINE) == 1, rel


def test_three_branches_recording_notes_merge_in_sequence(tmp_path):
    """Two is the pair that broke; three is the property. The notes end up in
    ARRIVAL order, so this asserts membership and count, never position."""
    t = build(tmp_path)
    base = t.head()
    ids = ("task-a", "task-b", "task-c")
    shas = [t.publish(name, t.recording_a_note(name), base=base) for name in ids]

    t.sweep()

    for sha in shas:
        assert contains(t.repo, t.head(), sha)
    for rel in TRACKERS:
        text = t.read(rel)
        assert "<<<<<<<" not in text
        for name in ids:
            assert text.count(note_line(name)) == 1, f"{name} in {rel}"
        assert text.count(SEED_NOTE_A) == 1 and text.count(SEED_NOTE_B) == 1, rel
    assert is_clean(t.repo)


def test_the_auto_resolution_is_named_in_the_transcript(tmp_path):
    """A merge the loop resolved without a human is exactly the thing an
    operator must be able to find afterwards, so it gets its own entry rather
    than passing as an ordinary merge."""
    t = build(tmp_path)
    base = t.head()
    t.publish("task-a", t.recording_a_note("task-a"), base=base)
    t.publish("task-b", t.recording_a_note("task-b"), base=base)

    t.sweep()

    resolved = t.entries("auto_merge_notes_resolved")
    assert len(resolved) == 1, "one resolution, for the second branch only"
    assert resolved[0]["data"]["paths"] == list(TRACKERS)
    assert resolved[0]["data"]["task_id"] == "task-b"


def test_a_note_appended_beside_a_new_table_row_still_merges(tmp_path):
    """The ordinary task, which changes a file AND records a note: one branch
    adds a row in the documentation above while both append notes. Git merges
    the one-sided row on its own, the resolver combines the tail, and the
    sweep does not stop."""
    t = build(tmp_path)
    base = t.head()
    with_row = SUMMARY_SEED.replace(
        PROSE_ROW, PROSE_ROW + "\n| `note_merge.py` | Section-aware note resolver. |"
    )
    t.publish(
        "task-a",
        {"docs/SUMMARY.md": with_row + note_line("task-a"),
         "docs/TESTS.md": TESTS_SEED + note_line("task-a")},
        base=base,
    )
    t.publish("task-b", t.recording_a_note("task-b"), base=base)

    t.sweep()

    text = t.read("docs/SUMMARY.md")
    assert "<<<<<<<" not in text
    assert text.count("| `note_merge.py` | Section-aware note resolver. |") == 1
    assert text.count(note_line("task-a")) == 1
    assert text.count(note_line("task-b")) == 1
    assert is_clean(t.repo)


# --- what must still conflict -------------------------------------------------


def test_a_concurrent_edit_to_tracker_prose_still_conflicts(tmp_path):
    """The property the union attribute could not keep, and the reason it was
    removed: two branches rewriting the same line of documentation ABOVE the
    append-only section is a real content conflict and must stop the sweep."""
    t = build(tmp_path)
    base = t.head()

    def rewritten(text_for):
        return {"docs/SUMMARY.md": SUMMARY_SEED.replace(PROSE_ROW, f"| `main.py` | {text_for} |")}

    first = t.publish("task-a", rewritten("A's description"), base=base)
    second = t.publish("task-b", rewritten("B's description"), base=base)

    t.sweep()

    assert_stopped_after_first(t, first, second)
    text = t.read("docs/SUMMARY.md")
    assert "A's description" in text and "B's description" not in text


def test_prose_conflicts_even_when_both_branches_also_append_notes(tmp_path):
    """The hard case, and the one that proves the resolver is section-aware
    rather than file-aware: the TAIL of both sides is a clean append and would
    combine happily, while the prose above it genuinely conflicts. Git leaves
    markers there, `resolve_note_append` sees them, and the whole merge is
    refused."""
    t = build(tmp_path)
    base = t.head()

    def edited(task_id, text_for):
        return {
            "docs/SUMMARY.md": SUMMARY_SEED.replace(
                PROSE_ROW, f"| `main.py` | {text_for} |"
            ) + note_line(task_id),
            "docs/TESTS.md": TESTS_SEED + note_line(task_id),
        }

    first = t.publish("task-a", edited("task-a", "A's description"), base=base)
    second = t.publish("task-b", edited("task-b", "B's description"), base=base)

    t.sweep()

    assert_stopped_after_first(t, first, second)
    assert note_line("task-b") not in t.read("docs/SUMMARY.md")


def test_one_refusing_tracker_stops_the_whole_merge_even_if_the_other_resolved(tmp_path):
    """Nothing is written until EVERY conflicted path has resolved.

    The conflicts are walked in sorted order, so `docs/SUMMARY.md` here is a
    clean pair of appends that resolves, and `docs/TESTS.md` — reached second —
    carries a genuine prose conflict and refuses. The merge is a single
    decision: `SUMMARY.md` must be left exactly as git's abort finds it, not
    carrying a resolution for a merge that never happened.
    """
    t = build(tmp_path)
    base = t.head()

    def edited(task_id, text_for):
        return {
            "docs/SUMMARY.md": SUMMARY_SEED + note_line(task_id),
            "docs/TESTS.md": TESTS_SEED.replace(
                PROSE_ROW, f"| `main.py` | {text_for} |"
            ) + note_line(task_id),
        }

    first = t.publish("task-a", edited("task-a", "A's description"), base=base)
    second = t.publish("task-b", edited("task-b", "B's description"), base=base)

    t.sweep()

    assert_stopped_after_first(t, first, second)
    summary = t.read("docs/SUMMARY.md")
    assert note_line("task-a") in summary
    assert note_line("task-b") not in summary, (
        "the resolved SUMMARY text must never reach disk when TESTS.md refused"
    )
    assert t.entries("auto_merge_notes_resolved") == []


def test_a_concurrent_edit_to_an_existing_note_line_still_conflicts(tmp_path):
    """A note already in the ledger is not append-only content — it is a claim
    somebody made. Two branches rewriting it must stop the sweep exactly like
    two branches rewriting a line of code."""
    t = build(tmp_path)
    base = t.head()

    def rewritten(text_for):
        return {"docs/SUMMARY.md": SUMMARY_SEED.replace(
            SEED_NOTE_A, f"| 2026-08-17 | seed-00 | {text_for} |"
        )}

    first = t.publish("task-a", rewritten("A's version"), base=base)
    second = t.publish("task-b", rewritten("B's version"), base=base)

    t.sweep()

    assert_stopped_after_first(t, first, second)
    text = t.read("docs/SUMMARY.md")
    assert "A's version" in text and "B's version" not in text


def test_deleting_an_existing_note_conflicts_with_a_concurrent_append(tmp_path):
    """One branch removes a note that was already there while recording its
    own; the other simply records its own. Both touch the end of the file, so
    git conflicts — and the deleting side is no longer "the base plus an
    append", so the resolver declines rather than quietly dropping a note
    nobody agreed to drop."""
    t = build(tmp_path)
    base = t.head()
    first = t.publish(
        "task-a",
        {"docs/SUMMARY.md": SUMMARY_SEED.replace(SEED_NOTE_A + "\n", "") + note_line("task-a")},
        base=base,
    )
    second = t.publish(
        "task-b", {"docs/SUMMARY.md": SUMMARY_SEED + note_line("task-b")}, base=base
    )

    t.sweep()

    assert_stopped_after_first(t, first, second)


def test_reordering_existing_notes_conflicts_with_a_concurrent_append(tmp_path):
    """Same shape as deletion, and it matters for the same reason: a reorder
    preserves every character and still is not an append, so accepting it
    would mean the resolver was comparing SETS of lines rather than proving
    the base survived unchanged."""
    t = build(tmp_path)
    base = t.head()
    swapped = SUMMARY_SEED.replace(
        f"{SEED_NOTE_A}\n{SEED_NOTE_B}\n", f"{SEED_NOTE_B}\n{SEED_NOTE_A}\n"
    )
    assert swapped != SUMMARY_SEED, "the fixture must actually reorder something"

    first = t.publish("task-a", {"docs/SUMMARY.md": swapped + note_line("task-a")}, base=base)
    second = t.publish(
        "task-b", {"docs/SUMMARY.md": SUMMARY_SEED + note_line("task-b")}, base=base
    )

    t.sweep()

    assert_stopped_after_first(t, first, second)


def test_a_real_conflict_in_a_source_file_still_stops_the_sweep(tmp_path):
    """The bound on the whole change: only append-only change notes are in
    scope. A genuine source conflict must still stop the sweep, and the abort
    must leave the base exactly where the previous merge put it."""
    t = build(tmp_path, extra_files={"autoloop/thing.py": "TIMEOUT = 30\n"})
    base = t.head()

    first = t.publish("task-a", {"autoloop/thing.py": "TIMEOUT = 60\n"}, base=base)
    second = t.publish("task-b", {"autoloop/thing.py": "TIMEOUT = 90\n"}, base=base)

    t.sweep()

    assert_stopped_after_first(t, first, second)
    assert t.read("autoloop/thing.py") == "TIMEOUT = 60\n"


def test_a_documentation_file_outside_the_two_trackers_still_conflicts(tmp_path):
    """What "narrow" buys: the same append-at-EOF edit that combines in the
    trackers still conflicts in a doc nobody granted the resolver."""
    t = build(tmp_path, extra_files={"docs/TODO.md": "# TODO\n\n- one\n"})
    base = t.head()
    todo = "# TODO\n\n- one\n"

    first = t.publish("task-a", {"docs/TODO.md": todo + "- from A\n"}, base=base)
    second = t.publish("task-b", {"docs/TODO.md": todo + "- from B\n"}, base=base)

    t.sweep()

    assert_stopped_after_first(t, first, second)
    refusals = t.entries("auto_merge_notes_refused")
    assert refusals, "the resolver must say why it declined"
    assert "outside" in refusals[-1]["data"]["reason"]
    assert refusals[-1]["data"]["conflicted_files"] == ["docs/TODO.md"]


def test_every_refusal_says_why_in_the_transcript(tmp_path):
    """A sweep that stops has to explain itself, or the next operator
    reconstructs the merge by hand to find out what happened."""
    t = build(tmp_path)
    base = t.head()

    def rewritten(text_for):
        return {"docs/SUMMARY.md": SUMMARY_SEED.replace(PROSE_ROW, f"| `main.py` | {text_for} |")}

    t.publish("task-a", rewritten("A's description"), base=base)
    t.publish("task-b", rewritten("B's description"), base=base)

    t.sweep()

    refusals = t.entries("auto_merge_notes_refused")
    assert len(refusals) == 1
    assert refusals[0]["data"]["conflicted_files"] == ["docs/SUMMARY.md"]
    assert refusals[0]["data"]["reason"]
    assert t.entries("auto_merge_notes_resolved") == []


# --- the resolver is what does this, not a weakened git -----------------------


def test_a_bare_git_merge_of_two_note_appends_still_conflicts(tmp_path):
    """The repository ships no merge attribute, so git's own behaviour on
    these two files is unchanged: an operator merging by hand gets a conflict,
    exactly as before. Only the loop's merge path resolves it — which is what
    keeps "do not weaken the merge sweep's conflict handling in general"
    checkable rather than a claim."""
    repo = tmp_path / "plain"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", BASE)
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "commit.gpgsign", "false")
    write(repo, ".gitattributes", GITATTRIBUTES.read_text(encoding="utf-8"))
    write(repo, "docs/SUMMARY.md", SUMMARY_SEED)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")
    base = head(repo)

    for name, text in (("a", "task-a"), ("b", "task-b")):
        run_git(repo, "checkout", "-q", "-b", name, base)
        write(repo, "docs/SUMMARY.md", SUMMARY_SEED + note_line(text))
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", name)
        run_git(repo, "checkout", "-q", BASE)

    assert try_git(repo, "merge", "--no-ff", "--no-edit", "-m", "one", "a").returncode == 0
    result = try_git(repo, "merge", "--no-ff", "--no-edit", "-m", "two", "b")
    assert result.returncode != 0
    assert "CONFLICT" in result.stdout + result.stderr
    run_git(repo, "merge", "--abort")


# --- why `merge=union` was rejected, demonstrated rather than asserted --------


def union_repo(tmp_path, name, seed_text):
    """A plain repo whose `.gitattributes` grants `merge=union` INLINE. The
    repository deliberately no longer ships that rule, so these two tests have
    to state it themselves — they are evidence about git's union driver, not
    about this repository's configuration."""
    repo = tmp_path / name
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", BASE)
    run_git(repo, "config", "user.email", "test@example.com")
    run_git(repo, "config", "user.name", "Test")
    run_git(repo, "config", "commit.gpgsign", "false")
    write(repo, ".gitattributes", "docs/SUMMARY.md merge=union\n")
    write(repo, "docs/SUMMARY.md", seed_text)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "init")
    return repo, head(repo)


def union_branch(repo, name, base_sha, text):
    run_git(repo, "checkout", "-q", "-b", name, base_sha)
    write(repo, "docs/SUMMARY.md", text)
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", name)
    run_git(repo, "checkout", "-q", BASE)


def test_union_duplicates_a_grown_row_instead_of_merging_the_two_additions(tmp_path):
    """Union is NOT sufficient on its own — verified, not assumed, because the
    brief that ordered this work said so and told us to check.

    Union resolves per LINE. On the shape this repo actually had (one table row
    per module, the longest 19,410 characters), two branches that each append
    their sentence INSIDE that row do not get their additions merged: the whole
    row is duplicated, once per side. Both notes survive, but the document now
    carries two ~19KB rows that disagree and a reader cannot tell which is
    current. That is why a note is a new LINE.
    """
    giant = "| `orchestrator.py` | " + ("Persisted state machine. " * 780) + "|"
    assert len(giant) > 19_000
    repo, base = union_repo(tmp_path, "grown", SUMMARY_SEED.replace(PROSE_ROW, giant))

    for name, suffix in (("a", "APPENDED-BY-A."), ("b", "APPENDED-BY-B.")):
        grown = SUMMARY_SEED.replace(PROSE_ROW, giant[:-1] + suffix + " |")
        union_branch(repo, name, base, grown)

    assert try_git(repo, "merge", "--no-ff", "--no-edit", "-m", "one", "a").returncode == 0
    result = try_git(repo, "merge", "--no-ff", "--no-edit", "-m", "two", "b")
    assert result.returncode == 0, result.stdout + result.stderr

    rows = [ln for ln in read(repo, "docs/SUMMARY.md").splitlines()
            if ln.startswith("| `orchestrator.py` |")]
    assert len(rows) == 2, "union duplicated the row rather than merging the two additions"
    assert sum("APPENDED-BY-A." in row for row in rows) == 1
    assert sum("APPENDED-BY-B." in row for row in rows) == 1
    assert not any("APPENDED-BY-A." in row and "APPENDED-BY-B." in row for row in rows)


def test_union_would_have_swallowed_a_genuine_prose_conflict(tmp_path):
    """The decisive reason union was removed on the day it shipped.

    Git has no way to scope a merge attribute to a REGION of a file, so
    `merge=union` on a tracker does not mean "combine the appended notes" — it
    means the file can no longer conflict at ALL. Here two branches rewrite the
    same line of DOCUMENTATION, nowhere near the notes section, and union
    reports success while leaving both contradictory versions in the file with
    no warning. `test_a_concurrent_edit_to_tracker_prose_still_conflicts` is
    the same scenario through the shipped configuration, and it stops.
    """
    repo, base = union_repo(tmp_path, "prose", SUMMARY_SEED)
    for name, text_for in (("a", "A's description"), ("b", "B's description")):
        union_branch(repo, name, base,
                     SUMMARY_SEED.replace(PROSE_ROW, f"| `main.py` | {text_for} |"))

    assert try_git(repo, "merge", "--no-ff", "--no-edit", "-m", "one", "a").returncode == 0
    result = try_git(repo, "merge", "--no-ff", "--no-edit", "-m", "two", "b")

    assert result.returncode == 0, "union never conflicts — that is the problem"
    text = read(repo, "docs/SUMMARY.md")
    assert "<<<<<<<" not in text
    assert "A's description" in text and "B's description" in text


def attribute_rules() -> list[str]:
    return [
        " ".join(line.split())
        for line in GITATTRIBUTES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_the_repo_ships_no_merge_attribute_at_all():
    """`.gitattributes` is kept and is deliberately rule-free: it is where the
    next person to reach for `merge=union` finds out that it was tried,
    measured and removed. A rule appearing here is a decision that needs its
    own review, so the assertion is exact emptiness rather than "no union"."""
    assert attribute_rules() == []
    text = GITATTRIBUTES.read_text(encoding="utf-8")
    assert "merge=union" in text, "the removal has to stay explained, or it comes back"
    assert "autoloop/note_merge.py" in text


# --- the resolver's own rules -------------------------------------------------
#
# Unit-level, on the pure function, for the cases a real merge cannot easily
# stage — git's own working file is an INPUT here, so it can be written by hand.


def tracker(rows: str, notes: str) -> str:
    return (
        "# Tracker\n\n| Path | Purpose |\n|---|---|\n"
        f"{rows}"
        f"\n## Change notes\n\n{MARKER_LINE}\n\n| Date | Task | Note |\n|---|---|---|\n"
        f"{notes}"
    )


BASE_DOC = tracker(f"{PROSE_ROW}\n", f"{SEED_NOTE_A}\n")


def test_the_resolver_combines_two_appends_in_arrival_order():
    ours = BASE_DOC + note_line("task-a")
    theirs = BASE_DOC + note_line("task-b")

    combined = resolve_note_append(BASE_DOC, ours, theirs, BASE_DOC)

    assert combined == BASE_DOC + note_line("task-a") + note_line("task-b")
    assert combined.count(SEED_NOTE_A) == 1


def test_the_resolver_keeps_gits_merge_of_everything_above_the_section():
    """The head of the file is git's answer, not this module's. A one-sided
    prose change is already merged in the working file, and the result must
    carry it — otherwise resolving the notes would silently revert it."""
    merged_head_doc = tracker("| `main.py` | Rewritten by one side. |\n", f"{SEED_NOTE_A}\n")
    ours = BASE_DOC + note_line("task-a")
    theirs = BASE_DOC + note_line("task-b")

    combined = resolve_note_append(BASE_DOC, ours, theirs, merged_head_doc)

    assert combined is not None
    assert "| `main.py` | Rewritten by one side. |" in combined
    assert PROSE_ROW not in combined
    assert note_line("task-a") in combined and note_line("task-b") in combined


def test_the_resolver_refuses_when_git_left_a_conflict_above_the_section():
    conflicted = tracker(
        "<<<<<<< HEAD\n| `main.py` | A. |\n=======\n| `main.py` | B. |\n>>>>>>> theirs\n",
        f"{SEED_NOTE_A}\n",
    )
    ours = BASE_DOC + note_line("task-a")
    theirs = BASE_DOC + note_line("task-b")

    assert resolve_note_append(BASE_DOC, ours, theirs, conflicted) is None


def test_the_resolver_refuses_an_edited_existing_note_line():
    ours = BASE_DOC.replace(SEED_NOTE_A, "| 2026-08-17 | seed-00 | reworded |")
    theirs = BASE_DOC + note_line("task-b")

    assert resolve_note_append(BASE_DOC, ours, theirs, BASE_DOC) is None


def test_the_resolver_refuses_a_grown_last_line():
    """The original failure, at the level of the function: an addition that
    continues the last existing line instead of starting a new one."""
    ours = BASE_DOC.rstrip("\n") + " grown by A. |\n"
    theirs = BASE_DOC + note_line("task-b")

    assert resolve_note_append(BASE_DOC, ours, theirs, BASE_DOC) is None


def test_the_resolver_refuses_an_appended_heading():
    """A heading after the notes table would make every later append land
    inside it rather than at the end of the ledger."""
    ours = BASE_DOC + "\n## Something new\n\n- a bullet\n"
    theirs = BASE_DOC + note_line("task-b")

    assert resolve_note_append(BASE_DOC, ours, theirs, BASE_DOC) is None


def test_the_resolver_refuses_a_missing_or_duplicated_marker():
    ours = BASE_DOC + note_line("task-a")
    theirs = BASE_DOC + note_line("task-b")

    assert resolve_note_append(BASE_DOC.replace(MARKER_LINE, ""), ours, theirs, BASE_DOC) is None
    assert resolve_note_append(BASE_DOC, ours, theirs, BASE_DOC + MARKER_LINE + "\n") is None


def test_the_resolver_refuses_when_neither_side_appended_anything():
    """Whatever git conflicted on, it was not two branches recording notes."""
    assert resolve_note_append(BASE_DOC, BASE_DOC, BASE_DOC, BASE_DOC) is None


def test_the_resolver_is_scoped_to_exactly_the_two_trackers():
    """The whole blast radius of auto-resolution. Literal paths, no glob, no
    prefix: every entry here is a file the loop may merge without a human in a
    case where git said "conflict"."""
    assert note_merge.NOTE_TRACKERS == frozenset(TRACKERS)
    assert not any("*" in path for path in note_merge.NOTE_TRACKERS)


# --- the shape the instructions promise ---------------------------------------


def notes_section(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    # Exactly once, and this is the assertion that earns its keep: the SHIPPED
    # tracker is an input to `resolve_note_append`, which refuses outright when
    # the marker count is not 1 (`note_merge.py` line ~159). A second copy —
    # written by a task quoting the comment while documenting the machinery,
    # which is how `docs/SUMMARY.md` first shipped this fix — therefore turns
    # auto-resolution off for that file with no symptom except the sweep
    # halting again. Say so here, or the next reader reads a count mismatch as
    # a formatting nit.
    assert text.count(NOTES_MARKER) == 1, (
        f"{path.name} must carry exactly one notes marker — a second copy anywhere "
        "in the file makes resolve_note_append refuse EVERY parallel merge of it. "
        "Refer to the marker as CHANGE-NOTES in prose instead of writing the "
        "comment out again."
    )
    return text.split(NOTES_MARKER, 1)[1].splitlines()[1:]


def test_both_trackers_end_with_an_append_only_change_note_section():
    """A note is appended at the END of the file, so the section has to BE the
    end of the file — nothing may follow it that a later append would land
    inside, and the resolver refuses an appended heading for the same reason."""
    for path in (SUMMARY_DOC, TESTS_DOC):
        # Load-bearing, and the one precondition whose failure is invisible:
        # `resolve_note_append` treats a file whose section does not end in a
        # newline as a grown last line and refuses EVERY later parallel merge,
        # with a reason that does not mention newlines.
        assert path.read_text(encoding="utf-8").endswith("\n"), (
            f"{path.name} must end with a newline — without it the next task's "
            "note continues the last line instead of starting its own, and the "
            "resolver refuses the merge"
        )
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
    old failure returning. Enforced HERE and deliberately not in the resolver:
    a long note is a documentation-shape problem, and refusing to merge it
    would turn a style violation into a halted sweep."""
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
    assert "Never grow, edit or reorder an existing line" in text
    assert "CHANGE-NOTES" in text
    assert "autoloop/note_merge.py" in text
    assert "autoloop/tests/test_docs_merge.py" in text
    # The removal is documented so it is not reintroduced by the next reader
    # who finds a one-line attribute more attractive than a resolver.
    assert "merge=union" in text


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
