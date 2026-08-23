"""Combine two branches' appended change notes — and NOTHING else.

## What this exists for

Every task records what it changed in the repository's documentation trackers,
so any two parallel branches touch the same region of the same files. The
merge sweep halted three times in one evening on exactly that (2026-08-18) and
left five reviewed, published tasks unmerged for a full day — dash-10, loop-02,
brw-12, hlth-01, wrk-01 — each resolved by hand.

Every tracker in `NOTE_TRACKERS` ends with an append-only **change-note
section**, opened by the `NOTES_MARKER` comment, and `CLAUDE.md` §12 says a note
is ONE NEW LINE appended at the very end. This module is the other half: given
the three sides git itself recorded for a conflicted tracker, it returns the
combined text when — and only when — the conflict is two branches each appending
their own complete note lines to that terminal section.

## Why the list is four paths and not two (notes-03, 2026-08-23)

It shipped covering `docs/SUMMARY.md` and `docs/TESTS.md` only, because those
were the two files with a delimited section. `docs/SECURITY.md` and
`docs/COMMON_ERRORS.md` are recorded in by every task under the same rules
(`CLAUDE.md` §12/§14, `tasks.TRACKER_PATHS`) and collided exactly the same way,
and a conflict in ONE uncovered path refuses the WHOLE merge — so the two
covered files bought nothing whenever a third tracker collided too. Measured
2026-08-22: bind-01, split-01 and dash-17 were cut from one base and all three
were refused with `conflicted path(s) outside the change-note trackers`, over
six documentation conflicts that were all the same append-at-the-end shape.

The order that made the widening safe is the point, and it is the rule for any
future addition: **the delimited section comes first, the list second.** A path
in `NOTE_TRACKERS` whose file has no marked append-only region is a file whose
ORDINARY PROSE this module would start combining.

## Why not `merge=union`, which is one line of `.gitattributes`

Union was tried and removed (see `.gitattributes`). It never reports a conflict
at all, on ANY hunk of the file it is attached to, and git has no way to scope a
merge attribute to a REGION. So union bought clean note merges by silently
giving up edit/edit detection for the whole tracker: two branches rewriting the
same sentence produced two contradictory copies of it and no warning. That is
the property the brief required be kept ("a real content conflict in the same
file must still conflict"), so the attribute could not be the answer.

Union is also insufficient on its own — it resolves per LINE, so two branches
that each GREW the same 19,410-character table row duplicated the whole row
instead of merging the two additions. `autoloop/tests/test_docs_merge.py`
demonstrates both facts against real git rather than asserting them.

## Why not a custom `merge.<driver>` gitattribute either

A custom driver has the same region problem, plus a worse one: it is configured
in `.git/config` (`merge.<name>.driver`), which is NOT part of the repository,
so every clone — main checkout and every per-task worker repo — would have to
install it out of band. The loop cannot install it for them:
`policy._ALLOWED_GIT["config"]` admits `--get`/`--get-regexp`/`--get-all` and
nothing that writes, so shipping a driver means widening a security whitelist.
Wiring this module into `AutoMerger._merge` instead needs no policy change at
all, and reaches every merge the loop performs (`auto_merge.py` after a
completion, `merge_sweep.py` over the backlog) because both go through that one
method.

## The rule, stated as narrowly as it is implemented

`resolve_note_append` returns a merged text ONLY when all of these hold:

  * every side — base, ours, theirs, and git's own half-merged working file —
    carries the notes marker exactly once;
  * git's working file has no conflict marker ANYWHERE BEFORE that marker, i.e.
    everything outside the append-only section merged cleanly on its own;
  * ours and theirs each hold the base's section text as a literal PREFIX and
    add only whole lines after it — so an edit, a deletion, a rewrite or a
    reorder of any pre-existing note line disqualifies that side outright;
  * the added lines open no new section (`#`) and are not conflict-marker
    debris.

Anything else returns `None`, and the caller aborts the merge exactly as it did
before this module existed. That is deliberate: the failure mode of being too
strict is a merge sweep that stops and asks for a human, which is the behaviour
of every other file in the repository.

**Everything outside the section is git's own 3-way merge, untouched.** This
module never merges prose. It keeps git's merged text for the head of the file
verbatim and only rebuilds the tail, which is why a concurrent edit to the
tracker's prose still conflicts: git leaves markers there, and the check above
sees them.

## Two directions, one resolver (notes-04, 2026-08-23)

The loop merges these trackers in BOTH directions and needs the same rule in
each:

  * task branch INTO the base branch — `auto_merge.AutoMerger._merge`, the
    direction this module shipped for;
  * the base branch's head INTO a task branch — `orchestrator.
    _carry_reviewed_candidate_past`, which refreshes a REVIEWED candidate's
    recorded base when the head moved under it.

Only the first was wired, so the second refused every change-note collision and
parked `task_base_behind_head` — measured hours after notes-03 shipped, on
`blk-quota-01-002`: quota-01 conflicted at `docs/SUMMARY.md` and `docs/TESTS.md`,
both in `NOTE_TRACKERS`, both exactly the shape this exists to combine, and an
11-file reviewed candidate was abandoned over it. `combine_conflicted_notes`
below is the plumbing both call sites now share (read three index stages, hand
them to `resolve_note_append`, write/stage/commit only once EVERY path
resolved), so there is one rule and one place to change it.

**The two directions differ in ONE thing: which side's appended lines go
first**, and it is load-bearing rather than cosmetic — see `lead` below.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import GitError

#: The trackers whose terminal change-note section may be combined. A literal
#: list, never a prefix or a glob: this list is the whole blast radius of the
#: resolver, and every path added to it is a file that can be merged without a
#: human in a case where git said "conflict".
#:
#: **A path may only be added once the file itself carries exactly one
#: `NOTES_MARKER`-delimited append-only section at its END.** Without that
#: region there is no boundary between prose and ledger for `split_at_marker`
#: to find; the marker-count check below refuses first, so every merge of that
#: file stops — but the reason a reader sees is "not two branches appending
#: change notes" rather than "this file was never prepared", which is why the
#: precondition is stated here and tested. Two tests hold the pair together:
#: `test_docs_merge.py::test_every_shipped_tracker_ends_with_an_append_only_section`
#: checks each file named here actually has one, and
#: `::test_a_tracker_without_the_marker_is_refused_rather_than_combined` proves
#: the resolver refuses rather than guessing when one does not.
#:
#: `CLAUDE.md` and `docs/SCHEMA.md` are trackers a task may write
#: (`tasks.TRACKER_PATHS`) and are deliberately NOT here: they have no
#: append-only section, so a collision in either still stops the sweep.
NOTE_TRACKERS: frozenset[str] = frozenset(
    {
        "docs/COMMON_ERRORS.md",
        "docs/SECURITY.md",
        "docs/SUMMARY.md",
        "docs/TESTS.md",
    }
)

#: The HTML comment opening each tracker's append-only section. Everything
#: BEFORE it is ordinary documentation this module refuses to reconcile;
#: everything after it is the note ledger. It has to appear exactly once — a
#: second copy means a side rewrote the section boundary itself.
NOTES_MARKER = "<!-- CHANGE-NOTES:"

#: The longest a single change-note line may be, counted over the WHOLE line —
#: the `| date | task-id | note |` cells included, since that is what a reader
#: and a checker both see. Deliberately generous: this is a guard against a row
#: heading back towards the 19,410 characters that started all of this, not a
#: style rule about sentence length.
#:
#: It lives here, beside the resolver it protects, because two consumers need
#: the SAME number and a second hand-maintained copy would eventually disagree
#: with the one that is enforced:
#:
#:   * `test_docs_merge.py::test_every_change_note_line_is_short_enough_to_merge_by_line`
#:     enforces it against the shipped trackers, as `len(line) <= this` — at
#:     most, not fewer than;
#:   * `implement_executor._authoring_rules` states it in the brief every
#:     implementing agent receives, so the rule arrives as INPUT instead of as
#:     a validation rejection twenty minutes later (measured 2026-08-21:
#:     merge-04 and blk-02 each lost a full round to discovering it by failing).
#:
#: The RESOLVER deliberately does not enforce it — see `_added_lines`, which
#: says why a long note must stay a documentation-shape problem rather than a
#: halted merge sweep.
MAX_NOTE_LINE_CHARS: int = 700

#: `lead` — whose appended note lines are written FIRST into the combined
#: section. Git's own words for the two sides of a merge: OURS is the branch
#: being merged INTO (stage 2, `HEAD`), THEIRS is the branch being merged IN
#: (stage 3).
#:
#: **This is not a cosmetic ordering.** `resolve_note_append` requires each
#: side's section to hold the merge base's section as a literal PREFIX, so the
#: rule that keeps a branch mergeable forever is: *a branch's change-note
#: section must be everything its base already carried, followed by that
#: branch's own additions.* Whichever side's history becomes the merge BASE of
#: the next merge therefore has to lead.
#:
#:   * Merging a task INTO the base branch (`auto_merge`): the base branch is
#:     the accumulator every later task is cut from, so its own lines lead and
#:     the task's follow — `OURS_FIRST`, the default, unchanged since docs-01.
#:   * Merging the base branch's head INTO a task (`orchestrator.
#:     _carry_reviewed_candidate_past`): the incoming head becomes that task's
#:     new base, so ITS lines must lead and the task's own additions stay at the
#:     end — `THEIRS_FIRST`.
#:
#: Getting the second one wrong is not a cosmetic defect, it is a task branch
#: that can never be merged out again: with the task's lines placed above lines
#: the new base already carries, the branch's section no longer STARTS with the
#: base's, and every later merge of that tracker refuses. That exact shape was
#: hit by hand on 2026-08-21 (ctx-01) and had to be repaired by moving six note
#: lines to the end of the ledger — see those notes in `docs/SUMMARY.md` and
#: `docs/TESTS.md`. `test_base_refresh_notes.py::
#: test_a_refreshed_task_branch_still_merges_back_out_through_auto_merge` is the
#: round-trip that would catch a regression.
#:
#: An unrecognised value raises rather than defaulting: silently falling back to
#: `OURS_FIRST` in the refresh direction is precisely the unmergeable-branch bug
#: above, arriving without a word.
OURS_FIRST = "ours-first"
THEIRS_FIRST = "theirs-first"

#: The four line shapes git writes into a conflicted working file. `|||||||`
#: only appears under `merge.conflictStyle=diff3`/`zdiff3`, which nothing here
#: sets — it is checked anyway so a repository that configures it does not
#: quietly slip a conflict past the head check below.
_CONFLICT_MARKERS = ("<<<<<<<", "=======", ">>>>>>>", "|||||||")


def is_conflict_marker(line: str) -> bool:
    """A line git wrote to mark a conflict, not content.

    Exact shapes only (`=======`, or a marker followed by a space and a label),
    because a Markdown setext underline is also a run of `=`. No line in either
    tracker is a bare `=======` today; if one ever is, this reads it as a
    marker and the resolver refuses — which stops the sweep rather than
    resolving something it misread, the safe direction.
    """
    return any(line == marker or line.startswith(marker + " ") for marker in _CONFLICT_MARKERS)


def split_at_marker(text: str) -> tuple[str, str]:
    """`(everything before the notes marker, the marker and everything after)`.

    The marker LINE belongs to the tail on purpose: a side that edited the
    marker itself then fails the prefix test below instead of having its edit
    silently accepted as part of the untouched head.
    """
    index = text.find(NOTES_MARKER)
    return text[:index], text[index:]


def _added_lines(added: str) -> list[str] | None:
    """The lines one side appended, or `None` if they are not appendable.

    Blank lines are allowed — a task that separates its note with an empty line
    is doing nothing dangerous, and refusing would stop the sweep over
    whitespace. A heading is NOT: appending `## something` would put a new
    section after the notes table, and every later append would land inside it
    rather than at the end of the ledger.

    Deliberately NO length cap. A note that grew into a paragraph is a
    documentation-shape problem, pinned separately by
    `test_docs_merge.py::test_every_change_note_line_is_short_enough_to_merge_by_line`;
    enforcing it HERE would turn a style violation into a halted merge sweep.
    """
    if added == "":
        return []
    if not added.endswith("\n"):
        # A partial last line means the "append" started mid-line, which is
        # exactly the grow-an-existing-line shape this whole change exists to
        # end.
        return None
    lines = added.splitlines()
    for line in lines:
        if line.lstrip().startswith("#") or is_conflict_marker(line) or NOTES_MARKER in line:
            return None
    return lines


def resolve_note_append(
    base: str, ours: str, theirs: str, merged: str, *, lead: str = OURS_FIRST
) -> str | None:
    """The combined text, or `None` to leave the conflict standing.

    `base`/`ours`/`theirs` are the three sides git recorded in the index for
    the conflicted path (stages 1/2/3 — authoritative, and the reason this does
    not parse conflict markers to work out who changed what). `merged` is git's
    own half-merged working file, used ONLY for the part of the document before
    the notes marker: git already merged that correctly or already marked it
    conflicted, and re-deciding it here would be reimplementing a 3-way merge
    nobody asked for.

    Returns the `base` section text plus both sides' added lines, `lead`
    deciding which side's come first (see `OURS_FIRST` / `THEIRS_FIRST` above —
    the default is the task-into-base direction and is what every caller before
    notes-04 got). Arrival order, not chronological — the notes carry their own
    date column, exactly as they did under the union driver this replaced.
    """
    if lead not in (OURS_FIRST, THEIRS_FIRST):
        raise ValueError(
            f"lead must be {OURS_FIRST!r} or {THEIRS_FIRST!r}, not {lead!r} — "
            "the ordering decides whether the merged branch can ever be merged "
            "again, so an unrecognised value is refused rather than defaulted"
        )
    if any(text.count(NOTES_MARKER) != 1 for text in (base, ours, theirs, merged)):
        return None

    merged_head, _merged_tail = split_at_marker(merged)
    if any(is_conflict_marker(line) for line in merged_head.splitlines()):
        # Something OUTSIDE the append-only section conflicted. That is an
        # ordinary documentation conflict and must stop the sweep.
        return None

    _base_head, base_tail = split_at_marker(base)
    _ours_head, ours_tail = split_at_marker(ours)
    _theirs_head, theirs_tail = split_at_marker(theirs)

    if not base_tail.endswith("\n"):
        # Without a final newline an "append" continues the last existing note
        # line instead of starting its own.
        return None
    if not ours_tail.startswith(base_tail) or not theirs_tail.startswith(base_tail):
        # One side edited, deleted, rewrote or reordered a line that was
        # already there. Never auto-resolved: that is a real content conflict.
        return None

    ours_added = ours_tail[len(base_tail):]
    theirs_added = theirs_tail[len(base_tail):]
    if _added_lines(ours_added) is None or _added_lines(theirs_added) is None:
        return None
    if not (ours_added + theirs_added).strip():
        # Nothing was appended by either side, so whatever git conflicted on
        # is not the thing this module handles.
        return None
    if ours_added == theirs_added:
        # Both sides appended the identical text. Git resolves that on its own
        # and never reaches here, but concatenating would duplicate a note, so
        # the case is handled rather than left to chance. `lead` is irrelevant
        # when the two are byte-identical.
        return merged_head + base_tail + ours_added
    if lead == THEIRS_FIRST:
        return merged_head + base_tail + theirs_added + ours_added
    return merged_head + base_tail + ours_added + theirs_added


@dataclass(frozen=True)
class NoteResolution:
    """What `combine_conflicted_notes` did with an in-progress merge.

    A VALUE, not an exception: "these two sides are not both appending change
    notes" is the ORDINARY answer here, and every caller routes it to the same
    place — the abort/park it would have taken anyway. Only a caller bug
    (`lead` misspelt) raises.

    * `resolved` — the only field to branch on. True means every conflicted
      path was combined, written, staged AND committed, so the merge is
      concluded and the caller must not abort it.
    * `paths` — what was combined, sorted. Empty unless `resolved`.
    * `refusal` — why not, for the transcript. Empty when `resolved`. Never
      empty when not: a sweep that stops without saying why is how an operator
      ends up reconstructing the merge by hand.
    """

    resolved: bool
    paths: tuple[str, ...] = ()
    refusal: str = ""


def combine_conflicted_notes(
    git, conflicts, message: str, *, lead: str = OURS_FIRST
) -> NoteResolution:
    """Combine two branches' appended change notes in the merge `git` is
    currently in the middle of — the shared plumbing behind both directions.

    `git` is a `GitGateway` rooted at the repository holding the conflict (the
    primary checkout for `auto_merge`, a worker repository for the base
    refresh); `conflicts` is what git reported unmerged; `message` is the merge
    commit message the caller would have used, which this extends with a line
    naming the files it combined so the automatic resolution is visible in
    `git log` and not only in the transcript.

    **Every conflicted path is resolved into memory before ANY file is
    written.** A path that resolves followed by one that refuses would
    otherwise leave a rewritten tracker in a half-merged tree, and the abort
    that follows would be cleaning up after this function rather than after
    git.

    Deliberately does NO logging. The two callers log to different transcript
    entry types (`auto_merge_notes_*` and `execution_base_notes_*`) because an
    operator reading one is asking a different question from an operator
    reading the other; the decision is shared, the reporting is not.
    """
    if lead not in (OURS_FIRST, THEIRS_FIRST):
        raise ValueError(f"lead must be {OURS_FIRST!r} or {THEIRS_FIRST!r}, not {lead!r}")

    paths = sorted(conflicts)
    if not paths:
        # Nothing is unmerged, so whatever failed was not a content conflict.
        # Refusing here is what stops an unreadable `git status` (which reports
        # no conflicted path at all) from being read as "everything resolved".
        return NoteResolution(
            False, refusal="git reported no conflicted path, so there is nothing to combine"
        )

    outside = [path for path in paths if path not in NOTE_TRACKERS]
    if outside:
        # Not even partially: a real conflict anywhere in the merge means the
        # whole merge needs a human, so nothing is resolved.
        return NoteResolution(
            False,
            refusal=f"conflicted path(s) outside the change-note trackers: {outside}",
        )

    resolutions: dict[str, str] = {}
    for path in paths:
        try:
            base = git.merge_stage_blob(path, 1).decode("utf-8")
            ours = git.merge_stage_blob(path, 2).decode("utf-8")
            theirs = git.merge_stage_blob(path, 3).decode("utf-8")
            merged = (Path(git.repo_root) / path).read_text(encoding="utf-8")
        except (GitError, OSError, UnicodeDecodeError) as exc:
            return NoteResolution(
                False,
                refusal=(
                    f"could not read all three sides of {path}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
        combined = resolve_note_append(base, ours, theirs, merged, lead=lead)
        if combined is None:
            return NoteResolution(
                False,
                refusal=(
                    f"{path} is not two branches appending change notes — "
                    "something outside the append-only section conflicted, or a "
                    "line that was already there was edited, deleted or reordered"
                ),
            )
        resolutions[path] = combined

    try:
        for path, text in resolutions.items():
            (Path(git.repo_root) / path).write_text(text, encoding="utf-8")
        git.add_paths(sorted(resolutions))
        git.commit_staged(
            f"{message}\n\nAppend-only change notes combined automatically in "
            + ", ".join(sorted(resolutions))
            + "."
        )
    except (GitError, OSError) as exc:
        # The tree now holds this function's writes on top of git's half-merged
        # state. Both callers abort next, which restores both — and verifies
        # that it did.
        return NoteResolution(
            False,
            refusal=(
                f"combined the change notes but could not commit them: "
                f"{type(exc).__name__}: {exc}"
            ),
        )

    return NoteResolution(True, paths=tuple(sorted(resolutions)))
