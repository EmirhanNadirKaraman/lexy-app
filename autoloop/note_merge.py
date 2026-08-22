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
"""

from __future__ import annotations

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


def resolve_note_append(base: str, ours: str, theirs: str, merged: str) -> str | None:
    """The combined text, or `None` to leave the conflict standing.

    `base`/`ours`/`theirs` are the three sides git recorded in the index for
    the conflicted path (stages 1/2/3 — authoritative, and the reason this does
    not parse conflict markers to work out who changed what). `merged` is git's
    own half-merged working file, used ONLY for the part of the document before
    the notes marker: git already merged that correctly or already marked it
    conflicted, and re-deciding it here would be reimplementing a 3-way merge
    nobody asked for.

    Returns `base` section text plus ours' added lines plus theirs', in that
    order. Arrival order, not chronological — the notes carry their own date
    column, exactly as they did under the union driver this replaced.
    """
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
        # the case is handled rather than left to chance.
        return merged_head + base_tail + ours_added
    return merged_head + base_tail + ours_added + theirs_added
