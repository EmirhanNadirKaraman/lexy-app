"""Task registry / task graph.

The roadmap ChatGPT plans against. Tasks have stable ids, dependencies, and a
stored status (pending / in_progress / completed / blocked / retired); ready vs
blocked is DERIVED from dependencies, never stored, so it can not go stale.
ChatGPT authorizes work by task id only (contract v2) — free-form instructions
are gone.

Three states mean "not running right now" and they are NOT interchangeable —
conflating them is what made the dashboard's blocked count unreadable:
`BLOCKED` waits on a dependency and resolves itself, `BLOCKED_BY_OPERATOR`
waits on a human and resolves when they answer, `RETIRED` waits on nobody
because the work already happened (or stopped) under another id. See
`TaskState` and `_RETIREMENTS`.

A FOURTH, `SHIPPED_ELSEWHERE` (ship-01, 2026-08-23), is not one of those: it
does not mean "not running", it means DONE — under another task's commits.
Unlike `RETIRED` it SATISFIES a dependency (`SATISFIES_DEPENDENCY`), because a
dependent waiting for that work is waiting for something that is already in the
base. It carries the evidence rather than a flag: `Task.shipped_commits` names
the commits that carry the work, and every reader re-checks their ancestry
against the current base instead of trusting the record once. This module does
no git of its own — it never has, by design — so a record whose evidence has
stopped holding is REPORTED as a disagreement (`dashboard.shipped_elsewhere_
states`), never silently converted back. See `record_shipped_elsewhere`.

Graph invariants enforced on every mutation: unique slug ids, dependencies
must reference known tasks (same batch counts), no cycles, no completing a
task whose dependencies are incomplete. Violations raise TaskGraphError with a
stable code that the orchestrator reports back to ChatGPT.

Persistence mirrors state.py: one JSON file, atomic replace, schema-versioned —
plus, since 2026-08-16, a SHORT-LIVED mutex around read-modify-write, because
atomic replace alone cannot make "load, mutate, save" atomic. See
`task_file_mutex` and `TaskStore`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path

from .errors import StateCorruptError, StateError, TaskGraphError
from .state import utcnow_iso

try:  # POSIX advisory locking. Absent on Windows, which this loop never runs on.
    import fcntl
except ImportError:  # pragma: no cover - not a platform any caller here uses
    fcntl = None

TASKS_SCHEMA_VERSION = 1

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: A single path SEGMENT (between '/'s) for `Task.approved_paths`: no glob
#: metacharacters (`*?[]{}`), no whitespace, and no leading '-' (a
#: flag-injection habit elsewhere in this package). Checked per-segment, not on
#: the whole string, so `lexy-app/backend/routers/books.py` is built from
#: segments this regex accepts individually.
#:
#: A leading '.' or '_' IS allowed. The original pattern required an
#: alphanumeric first character, which made ordinary repository files
#: unrepresentable — `lexy-app/backend/tests/_auth_helper.py` and `.gitignore`
#: were both refused while the error message claimed '_' was legal. That is a
#: scope-authoring failure, not a safety property: '.' and '..' segments are
#: refused separately below, which is the check that actually matters.
_APPROVED_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")


def _validate_approved_path(path: object) -> None:
    """Raise `TaskGraphError` unless `path` is an EXACT, repository-relative
    path safe to use as a git pathspec and a filesystem path: a non-empty
    string, no leading/trailing whitespace, no leading '/' or '~' (not
    absolute, not home-relative), no '\\\\' (Windows separator confusion), no
    '.' or '..' segment, and every segment restricted to
    `_APPROVED_PATH_SEGMENT_RE` — which by construction excludes every glob
    metacharacter. This is intentionally a strict ALLOWLIST rather than a
    blocklist of bad characters: v1 accepts exact paths only, so anything
    that is not obviously a plain relative file path is refused rather than
    guessed at. Symlink-traversal (whether an existing ancestor component on
    disk is a symlink) cannot be checked here — this module has no
    filesystem/repo-root awareness by design (see the module docstring) — so
    that check is re-run at dispatch time instead, immediately before a
    write-capable agent runs (`orchestrator.py`)."""
    if not isinstance(path, str) or not path or path != path.strip():
        raise TaskGraphError(
            "bad_approved_path", f"approved path {path!r} must be a non-empty string with no padding"
        )
    if path.startswith("/") or path.startswith("~"):
        raise TaskGraphError(
            "bad_approved_path",
            f"approved path {path!r} must be repository-relative — absolute paths "
            "and '~' are refused",
        )
    if "\\" in path:
        raise TaskGraphError(
            "bad_approved_path", f"approved path {path!r} must use '/' separators, not '\\\\'"
        )
    # A trailing '/' marks a DIRECTORY PREFIX ("everything under here").
    # Stripped before segment checks so the empty final segment it produces is
    # not mistaken for the '//' case refused below.
    segments = path.rstrip("/").split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise TaskGraphError(
            "bad_approved_path",
            f"approved path {path!r} must not contain '..', '.', or an empty "
            "segment (no '//' or leading/trailing '/')",
        )
    for seg in segments:
        if not _APPROVED_PATH_SEGMENT_RE.match(seg):
            raise TaskGraphError(
                "bad_approved_path",
                f"approved path {path!r} has an invalid segment {seg!r} — only "
                "[A-Za-z0-9._-] is allowed (no globs, no whitespace, no leading '-')",
            )


def _validate_approved_paths(task_id: object, paths: object) -> tuple[str, ...]:
    """Return `paths` as a tuple, or raise `TaskGraphError`.

    THE approved-path LIST check — `_validate_approved_path` above checks one
    entry, this checks the collection, and the difference matters because the
    duplicate rule used to live inline in `add_many` and nowhere else. A
    mutation reusing only the singular helper could therefore write a
    duplicate scope that creation refuses, which is exactly the drift
    `_validate_description` was extracted to prevent.

    SHAPE is checked first, and it is not decoration: `approved_paths` is
    declared as a tuple but nothing stops a caller passing the bare string
    `"a.py"`, and iterating that yields one-character "paths" — the same
    silent per-character split `_persisted_superseded_by` documents for
    `superseded_by`. Every downstream consumer (`unauthorized_paths`,
    `effective_approved_paths`, the manifest) would then reason about the
    letters of a filename as if they were files.
    """
    if isinstance(paths, str) or not isinstance(paths, (list, tuple)):
        raise TaskGraphError(
            "bad_approved_path",
            f"task '{task_id}' needs approved_paths as a list of repository-relative "
            f"paths, got {paths!r}",
        )
    seen: set[str] = set()
    for approved in paths:
        _validate_approved_path(approved)
        if approved in seen:
            raise TaskGraphError(
                "duplicate_approved_path",
                f"task '{task_id}' lists approved path {approved!r} more than once",
            )
        seen.add(approved)
    return tuple(paths)


def _validate_depends_on(
    task_id: object, depends_on: object, known: dict[str, "Task"]
) -> tuple[str, ...]:
    """Return `depends_on` as a tuple, or raise `TaskGraphError`.

    THE dependency check, shared by `TaskRegistry.add_many` (creation, where
    `known` is the candidate graph so intra-batch dependencies resolve) and
    `TaskRegistry.set_depends_on` (mutation, where it is the live graph). Same
    "one validator, every caller" rule as `_validate_description` and
    `_validate_superseded_by`.

    What it does NOT check is cycles: that needs the whole graph with the new
    edges already in it, so it stays with `_check_acyclic` and each caller runs
    it against a CANDIDATE rather than against a registry it has already
    mutated.

    The shape arm is new relative to the inline loop this replaces. A bare
    string `"ab"` used to be iterated per character and refused — but as
    `unknown task 'a'`, which sends the reader looking for a task that was
    never named.
    """
    if isinstance(depends_on, str) or not isinstance(depends_on, (list, tuple)):
        raise TaskGraphError(
            "bad_depends_on",
            f"task '{task_id}' needs depends_on as a list of task ids, got {depends_on!r}",
        )
    for dep in depends_on:
        if not isinstance(dep, str) or not _ID_RE.match(dep):
            raise TaskGraphError(
                "bad_depends_on",
                f"task '{task_id}' names {dep!r} as a dependency, which is not a "
                "valid task id (slug of [A-Za-z0-9._-], max 64)",
            )
        if dep not in known:
            raise TaskGraphError(
                "unknown_dependency", f"task '{task_id}' depends on unknown task '{dep}'"
            )
        if dep == task_id:
            raise TaskGraphError(
                "dependency_cycle", f"task '{task_id}' depends on itself"
            )
    return tuple(depends_on)


def _substitute_dependency(
    depends_on: tuple[str, ...],
    retired_id: str,
    successors: tuple[str, ...],
    owner_id: str,
) -> tuple[str, ...]:
    """A NEW tuple: `depends_on` with `retired_id` replaced by `successors`,
    in position.

    How a retirement makes its dependents' dependencies satisfiable again (see
    `TaskRegistry.retire`). An empty `successors` drops the edge outright,
    which is what `rewrite_dependents=True` means when the task went stale
    rather than being replaced.

    Returns rather than mutates, because `_retirement_rewrites` has to be able
    to plan every dependent and then abandon the whole plan. Position is
    preserved rather than appending at the end, so a dependency list still
    reads in the order it was planned in.

    Two shapes are filtered, and both are reachable rather than theoretical:

      * a successor that IS the dependent — retire A into B where B already
        waits on A. B continues the work; it does not wait on itself, and
        `_validate_depends_on` refuses a self-edge outright.
      * a repeat — the dependent already named the successor alongside the
        retired task, so the substitution would produce `('b', 'b')`.
        `_validate_depends_on` does NOT refuse duplicates (unlike
        `_validate_superseded_by`), so nothing downstream would have caught it.
    """
    rewritten: list[str] = []
    for dep in depends_on:
        for candidate in (successors if dep == retired_id else (dep,)):
            if candidate == owner_id or candidate in rewritten:
                continue
            rewritten.append(candidate)
    return tuple(rewritten)


def _validate_description(task_id: object, description: object) -> None:
    """Raise `TaskGraphError` unless `description` is a non-blank string.

    THE description check, called from both `TaskRegistry.add_many` (creation)
    and `TaskRegistry.set_description` (mutation), so the two cannot drift.
    Two implementations would mean a description the registry refuses to be
    created with could still be written onto an existing task — same reasoning
    as `unauthorized_paths` and `effective_approved_paths` above.

    Whitespace-only is refused (`strip()`), and so is a non-string: creation
    reached this through `Task.description.strip()` and would have raised
    `AttributeError` on a non-string, which is a crash rather than a refusal,
    and mutation takes its value straight from a caller. The value is NOT
    normalised — creation stores the string it was given, padding and all, so
    mutation stores it unchanged too.
    """
    if not isinstance(description, str) or not description.strip():
        raise TaskGraphError(
            "empty_task_field", f"task '{task_id}' needs a non-empty description"
        )


def _validate_urgent_reason(task_id: object, reason: object) -> None:
    """Raise `TaskGraphError` unless `reason` is a non-blank string.

    Same rule and the same wording shape as `operator_block`'s: a preemption
    displaces a round that may be twenty minutes into real work, so the record
    of why has to exist before anything is discarded. Its own validator rather
    than a call to `_validate_description` so the message names the act the
    operator performed — a refusal reading "needs a non-empty description" for
    an `urgent` request sends them to the wrong field.
    """
    if not isinstance(reason, str) or not reason.strip():
        raise TaskGraphError(
            "empty_task_field",
            f"marking task '{task_id}' urgent needs a non-empty reason — a "
            "preemption with no account of why is worse than a slow queue",
        )


def _validate_superseded_by(task_id: object, successors: object) -> tuple[str, ...]:
    """Return `successors` as a tuple, or raise `TaskGraphError`.

    THE successor check — the same "one validator, every caller" rule
    `_validate_description` is written for, and here there are FOUR of them:

      * `TaskRegistry.add_many` — creation, so a `seed_tasks.json` row is
        checked;
      * `TaskRegistry.retire` — mutation;
      * `_persisted_superseded_by`, i.e. `TaskRegistry.from_dict` — a stored or
        hand-edited `tasks.json` row, which reaches `Task` WITHOUT going
        through `add_many` (see the "bypass add_many" comment there);
      * `_migrate_retirements` — the successors this module writes itself.

    The third one is why this docstring names its callers instead of saying
    "creation and mutation". It used to claim persisted rows were checked while
    `from_dict` only ran `tuple()` over the field, so a bare `"brw-06"` loaded
    as `('b','r','w','-','0','6')` — six successors, each a valid id, no error
    anywhere — and a `null` reached the operator as `'NoneType' object is not
    iterable`, which names neither the task nor the field.

    SHAPE only, deliberately. Each entry must be a well-formed task id, no
    entry may name the task itself, and no id may repeat. What is NOT checked
    is whether the successor exists: a supersession is a historical record,
    not a schedule. brw-06 was retired into brw-07 + brw-08 at the reviewer's
    request before either existed, and refusing that would have forced the
    operator to either invent the successors early or leave the chain in
    prose. Nothing dispatches off this field, so an id that never materialises
    costs a dangling pointer in a record, not a broken graph.
    """
    if isinstance(successors, str) or not isinstance(successors, (list, tuple)):
        raise TaskGraphError(
            "bad_superseded_by",
            f"task '{task_id}' needs superseded_by as a list of task ids, "
            f"got {successors!r}",
        )
    seen: set[str] = set()
    for successor in successors:
        if not isinstance(successor, str) or not _ID_RE.match(successor):
            raise TaskGraphError(
                "bad_superseded_by",
                f"task '{task_id}' names {successor!r} as a successor, which is "
                "not a valid task id (slug of [A-Za-z0-9._-], max 64)",
            )
        if successor == task_id:
            raise TaskGraphError(
                "bad_superseded_by", f"task '{task_id}' cannot supersede itself"
            )
        if successor in seen:
            raise TaskGraphError(
                "bad_superseded_by",
                f"task '{task_id}' names successor {successor!r} more than once",
            )
        seen.add(successor)
    return tuple(successors)


def _persisted_superseded_by(raw: dict) -> tuple[str, ...]:
    """`superseded_by` off a stored row, validated, as a tuple.

    `TaskRegistry.from_dict` deliberately bypasses `add_many` — re-validating a
    stored graph would reject states only the running loop can produce — so
    every field it reads is on its own for checking, and this one was not
    checked at all: `tuple(raw.get("superseded_by", ()))` turns the bare string
    `"brw-06"` into six single-character "successors" — silently, and from there
    into the dashboard, the refusal messages, and the next `save` — while a
    `null` came out as `'NoneType' object is not iterable`, a corruption error
    naming neither the task nor the field.

    Same authority as every other caller (`_validate_superseded_by`), and it
    FAILS CLOSED into `StateCorruptError` — the registry's normal answer to a
    file it cannot trust (`from_dict`'s own KeyError/TypeError arm,
    `TaskStore.load`'s decode arm). Reading a malformed chain as "no successor"
    would quietly delete the record this whole field exists to keep.

    A MISSING key is not malformed — it defaults to `()`, the backward
    compatibility every `tasks.json` written before this field relies on. An
    explicit `null` is: `asdict` serialises `()` as `[]` and never as `null`,
    so nothing this package writes can produce one.
    """
    try:
        return _validate_superseded_by(raw.get("id"), raw.get("superseded_by", ()))
    except TaskGraphError as exc:
        raise StateCorruptError(f"task file has an invalid superseded_by: {exc}") from exc


#: A FULL commit object id — 40 hex for SHA-1, 64 for SHA-256 — lowercase only.
#:
#: Full, deliberately, and this is the load-bearing half of "the record carries
#: the evidence". An abbreviation is ambiguous: it names whatever object it
#: happens to prefix in whichever checkout re-checks it, so the same record
#: could verify in one clone and resolve to a different commit in another. The
#: whole point of storing the evidence is that anyone can re-run the ancestry
#: check and get the SAME answer, which an abbreviation cannot promise.
#: `cli._cmd_record_shipped` resolves whatever the operator typed to a full sha
#: (`dashboard.resolve_commit`) before queueing, so nothing about this is a
#: usability tax on the operator route.
#:
#: Lowercase because that is what git prints and what `dashboard._LOG_LINE`
#: parses; normalising here would make two spellings of one sha compare unequal
#: in `record_shipped_elsewhere`'s idempotence check while looking identical.
_COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _validate_shipped_commits(task_id: object, commits: object) -> tuple[str, ...]:
    """Return `commits` as a tuple of full shas, or raise `TaskGraphError`.

    THE evidence check, and it is a SHAPE check only — this module has no
    repository awareness (see the module docstring and `_validate_approved_
    path`), so whether a sha is an ancestor of the base head is decided by the
    caller that has a checkout (`cli._cmd_record_shipped` before the request is
    queued) and re-decided continuously by the reader that has one
    (`dashboard.shipped_elsewhere_states`).

    An EMPTY list is refused, and that refusal is the point of the whole
    function: "this shipped somewhere" with nothing naming where is exactly the
    operator assertion this state exists to replace. A record that cannot be
    checked is not evidence, and the only way to keep it from becoming one is to
    refuse to write it.

    SHAPE is checked first, and for the reason `_validate_approved_paths` gives:
    a bare string `"4f2a…"` is iterable, so without this it would be stored as
    one single-character "commit" per character — the same silent per-character
    split `_persisted_superseded_by` documents, on the field the dependents of
    this task are unblocked on the strength of.
    """
    if isinstance(commits, str) or not isinstance(commits, (list, tuple)):
        raise TaskGraphError(
            "bad_shipped_commits",
            f"task '{task_id}' needs shipped commits as a list of full commit "
            f"shas, got {commits!r}",
        )
    if not commits:
        raise TaskGraphError(
            "bad_shipped_commits",
            f"task '{task_id}' cannot be recorded as shipped elsewhere with no "
            "commits — the record IS the evidence, and a claim naming nothing "
            "cannot be re-checked against the base",
        )
    seen: set[str] = set()
    for sha in commits:
        if not isinstance(sha, str) or not _COMMIT_SHA_RE.match(sha):
            raise TaskGraphError(
                "bad_shipped_commits",
                f"task '{task_id}' names {sha!r} as a carrying commit, which is "
                "not a full lowercase commit sha (40 or 64 hex characters) — an "
                "abbreviation names a different object in a different checkout",
            )
        if sha in seen:
            raise TaskGraphError(
                "bad_shipped_commits",
                f"task '{task_id}' names carrying commit {sha!r} more than once",
            )
        seen.add(sha)
    return tuple(commits)


def _validate_shipped_note(task_id: object, note: object) -> None:
    """Raise `TaskGraphError` unless `note` is a non-blank string.

    Its own validator rather than a call to `_validate_description`, for the
    reason `_validate_urgent_reason` has one: the refusal has to name the act
    the operator performed, and "needs a non-empty description" sends them to
    the wrong field.

    Required, not optional. The shas say WHICH commits; this says WHERE the work
    landed in words a human can follow ("shipped under inbox-02's commits"), and
    it is what the dashboard group prints — the same job `superseded_by` does for
    a retirement. A record with shas and no account of them is a puzzle rather
    than a record.
    """
    if not isinstance(note, str) or not note.strip():
        raise TaskGraphError(
            "empty_task_field",
            f"recording task '{task_id}' as shipped elsewhere needs a non-empty "
            "note saying where the work landed — the commits say which, the note "
            "says whose",
        )


def _persisted_shipped_commits(raw: dict) -> tuple[str, ...]:
    """`shipped_commits` off a stored row, validated, as a tuple.

    Same authority and the same failure mode as `_persisted_superseded_by`:
    `TaskRegistry.from_dict` bypasses `add_many` by design, so this is the ONLY
    gate a stored or hand-edited row passes, and `tuple(raw.get(...))` over a
    bare string would load one "commit" per character — on the field that
    decides whether this task's dependents may dispatch.

    FAILS CLOSED into `StateCorruptError`, like its sibling: reading a malformed
    evidence list as "no evidence" would silently delete the claim's own
    support while leaving the claim standing, which is precisely the fail-open
    this state exists to prevent.

    A MISSING key defaults to `()` — that is every `tasks.json` written before
    this field existed, and it is not malformed. `_validate_shipped_commits`
    refuses an empty list on the WRITE path, so `()` here can only mean "this
    row predates the field"; a row that also says `status: shipped_elsewhere` is
    an evidence-free claim, and it is reported as a disagreement by the readers
    rather than refused at load (refusing would take the whole registry — and
    with it every dashboard panel — down over one hand-edited row).
    """
    commits = raw.get("shipped_commits", ())
    if commits == () or commits == []:
        return ()
    try:
        return _validate_shipped_commits(raw.get("id"), commits)
    except TaskGraphError as exc:
        raise StateCorruptError(f"task file has invalid shipped_commits: {exc}") from exc


def _persisted_recut_count(raw: dict) -> int:
    """`recut_count` off a stored row, validated, as an int.

    Same authority as `_persisted_shipped_commits` above — `from_dict` bypasses
    `add_many`, so this is the only gate a stored or hand-edited row passes —
    and the same fail-closed ending, for a sharper reason: this number is the
    ONLY bound on a destructive action the reviewer can take by itself
    (`contract.Decision.RECUT`). Reading a value it cannot trust as 0 would not
    merely lose information, it would silently hand back the whole allowance.

    A MISSING key, and an explicit `null`, both default to 0: that is every
    `tasks.json` written before this field existed, and it is not malformed —
    such a row genuinely has never been recut, because nothing could recut it.

    Everything else raises. `True` is refused explicitly because `bool` is an
    `int` in Python and `True >= 2` is a legal comparison answering False —
    exactly the quiet wrong answer this rejects. A negative is refused because
    no writer produces one and honouring `-1000` would postpone the cap by a
    thousand cuts, which is the guard switched off by a plausible-looking
    number rather than by an error.
    """
    value = raw.get("recut_count", 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StateCorruptError(
            f"task file has an invalid recut_count {value!r} for task "
            f"{raw.get('id')!r} — it must be a non-negative integer"
        )
    return value


def _persisted_nonneg_int(raw: dict, field: str) -> int:
    """One of the ceil-01 budget counters off a stored row, validated, as an int.

    The generalised `_persisted_recut_count`, and it exists for the same reason
    that one does: `from_dict` bypasses `add_many`, so this is the ONLY gate a
    stored or hand-edited row passes, and every field it covers
    (`attempt_extensions`, `inherited_attempts`, `split_depth`) is an input to a
    BOUND — how far the attempt ceiling may be widened, how much of the parent's
    spend a subtask inherits, how deep splitting may recurse. Reading a value it
    cannot trust as 0 would not merely lose information; on all three it hands
    back allowance the loop never granted.

    A MISSING key, and an explicit `null`, both default to 0: that is every
    `tasks.json` written before these fields existed, and it is not malformed —
    such a row genuinely has no extension, no inherited spend and no split
    ancestry, because nothing could have given it one.

    Everything else raises, including `True` (a `bool` IS an `int` in Python, so
    `True >= 1` is a legal comparison answering the quiet wrong thing) and any
    negative (nothing writes one, and `-1000` would postpone a cap by a thousand
    rounds — a guard switched off by a plausible number rather than by an error).

    Not folded into `_persisted_recut_count`: that one names its field in its
    own error text and is quoted by name in `Task.recut_count`'s docstring and
    in the recut tests, and merging them would rewrite a shipped message to buy
    four lines.
    """
    value = raw.get(field, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StateCorruptError(
            f"task file has an invalid {field} {value!r} for task "
            f"{raw.get('id')!r} — it must be a non-negative integer"
        )
    return value


def _shipped_hint(task: "Task") -> str:
    """A parenthetical naming the carrying commits, empty when none is recorded:
    ` (shipped under 4f2a9c1b3d5e, 9b1c…)`.

    The `_successor_hint` of this state, and it exists for the same reason:
    "this task shipped elsewhere" without naming where sends the reader back to
    the free text the evidence fields replace. Abbreviated to 12 characters for
    a refusal message — the full shas are in the record, and a refusal quoting
    four 40-character ids is unreadable.
    """
    if not task.shipped_commits:
        return ""
    return " (shipped under " + ", ".join(s[:12] for s in task.shipped_commits) + ")"


def _successor_hint(task: "Task") -> str:
    """A parenthetical naming the successors, empty when none is recorded:
    ` (superseded by brw-07, brw-08)`.

    Every refusal that mentions a retirement carries it, because "this task is
    retired" without naming what replaced it sends the reader back to the
    free-text reason this field exists to replace.
    """
    return f" (superseded by {', '.join(task.superseded_by)})" if task.superseded_by else ""


#: The value `Task.hold_origin` carries when — and ONLY when — a task was held
#: through the inbox by `TaskRegistry.operator_block`. It is the ONE thing
#: `operator_unblock` will release.
#:
#: Both meanings of `status == "blocked"` are the same field, and they must not
#: be reversible by the same route. A `task_fatal` park (`cli._handle_parked_
#: task` → `block`) records a real failure and is resolved by
#: `python -m autoloop answer`, which also resolves the `blockers.Blocker`
#: record tied to it. An OPERATOR HOLD placed through the inbox has no blocker
#: record at all, so `answer` cannot reach it — that is precisely the
#: one-way state `operator_block`/`operator_unblock` exist to avoid, and the
#: reverse has to come from the inbox too.
#:
#: Without a provenance marker the inbox's reverse would release BOTH: an
#: operator (or anything that can write to the inbox directory) could clear a
#: quarantine the loop raised, leaving its blocker open and unanswered while the
#: task went straight back into `ready_tasks()`. So the reverse is narrowed by
#: provenance rather than by trust.
#:
#: **A dedicated field, deliberately NOT the reason text.** This started life as
#: a `blocked_reason.startswith(OPERATOR_HOLD_PREFIX)` test, and that was a hole
#: rather than a shortcut: `blocked_reason` is unconstrained free text written
#: by ordinary loop-raised quarantines too (`_handle_parked_task` passes the
#: park detail straight through), so a genuine quarantine whose reason merely
#: BEGINS with the prefix — a park detail quoting an operator's note, or a
#: crafted one — read as a hold and was releasable from the inbox with its
#: blocker still open. That is the exact laundering this pair is supposed to
#: make impossible. `_RETIREMENTS` already warns against reading free-text
#: reasons for meaning; this is the same warning applied to the one place it was
#: still being ignored. The origin is now written by exactly one method, never
#: inferred, and `block()` clears it unconditionally — see `Task.hold_origin`.
HOLD_ORIGIN_OPERATOR = "operator"

#: Prose. `operator_block` puts this in front of the reason it is given so a
#: human reading `tasks.json`, the dashboard or a drain log can see at a glance
#: which kind of `blocked` they are looking at.
#:
#: NOTHING BRANCHES ON IT. Provenance is `Task.hold_origin` (above); this string
#: carries no authority, and a `blocked_reason` that happens to start with it
#: proves nothing about who wrote it. Keep those two facts together: the moment
#: a caller tests this prefix instead of the field, the hole described above is
#: back.
OPERATOR_HOLD_PREFIX = "operator hold: "

#: Statuses whose task FIELDS (description, approved_paths, depends_on) an
#: operator may still rewrite: the task is still ahead of the loop, so nothing
#: is currently being judged against them.
#:
#: `in_progress` is excluded because every one of those fields is live during a
#: dispatch — see `TaskRegistry._refuse_immutable` for what each one strands.
#: `completed` and `retired` are excluded because they are terminal records:
#: rewriting the scope a finished commit was checked against, or the
#: description of work that shipped under another id, edits history rather than
#: steering the queue.
#:
#: `blocked` IS mutable, and that is the point — correcting a description or
#: widening a scope is exactly what a quarantined task usually needs before its
#: blocker can be answered.
_MUTABLE_STATUSES = frozenset({"pending", "blocked"})

#: The statuses that SATISFY a dependency — the whole of `state_of`'s
#: dependency test, and THE one place that rule is written down.
#:
#: `completed` means the work shipped under this task's own id.
#: `shipped_elsewhere` means it shipped under another task's commits, with the
#: commits named on the row (`Task.shipped_commits`). Both are the same fact for
#: a dependent: what it was waiting for is in the base. That is the difference
#: from `retired`, which means SUPERSEDED — the work described here did not
#: happen, someone else's task describes what will — and which therefore
#: satisfies nothing and strands every dependent (`retire`'s own strand
#: precondition exists because of it).
#:
#: A frozenset rather than four hand-written `!= "completed"` tests, because
#: there were four: `state_of`, `stranded_dependents` (via `_TERMINAL_STATUSES`),
#: `dashboard._waiting_on` and `dashboard._dep_states`. Four copies of a rule is
#: the drift this module writes a docstring against on every other page — and
#: here a copy that disagreed would show one panel a task as READY while another
#: showed it BLOCKED on a dependency that is done.
SATISFIES_DEPENDENCY = frozenset({"completed", "shipped_elsewhere"})

#: Statuses a task never leaves. `completed` is the successful terminal state;
#: `retired` is the unsuccessful one (`TaskState.RETIRED` — superseded, never
#: coming back, and deliberately with no reverse); `shipped_elsewhere` is the
#: second successful one (`TaskState.SHIPPED_ELSEWHERE` — done, under another
#: task's commits).
#:
#: The distinction the strand check turns on: `state_of` satisfies a dependency
#: on the statuses in `SATISFIES_DEPENDENCY` and on NOTHING ELSE, so a
#: dependency on a task that has reached a terminal status OUTSIDE that set can
#: never be satisfied by waiting. A task already sitting in ANY of these,
#: meanwhile, cannot itself be stranded — it is a record, not queue, and nothing
#: is waiting to dispatch it. That is why `shipped_elsewhere` belongs here as
#: well as in `SATISFIES_DEPENDENCY`: leave it out and `retire` starts refusing
#: valid retirements on behalf of a "dependent" that is already done.
_TERMINAL_STATUSES = frozenset({"completed", "retired", "shipped_elsewhere"})


def satisfies_dependency(state: "TaskState") -> bool:
    """Does a dependency in `state` count as done?

    The DERIVED-state form of `SATISFIES_DEPENDENCY`, for callers holding a
    `TaskState` rather than a stored status — `dashboard._waiting_on` is the
    one. Keyed off the same constant rather than listing the states again, so
    the two forms of the rule cannot disagree; every status in that set maps to
    the `TaskState` of the same value (`state_of` answers each one before the
    dependency check), which is what makes the value comparison exact rather
    than approximate.
    """
    return state.value in SATISFIES_DEPENDENCY


def is_directory_prefix(approved: str) -> bool:
    """A trailing '/' means "this directory and everything under it"."""
    return approved.endswith("/")


def unauthorized_paths(changed, approved) -> set[str]:
    """Which of `changed` no entry in `approved` authorizes.

    THE single matcher, used by both the pre-commit gate and the post-commit
    ownership check. Two implementations would drift, and a drift here means a
    path refused before the commit but accepted after (or worse, the reverse) —
    the same reasoning as `effective_approved_paths`.

    An entry is either an exact repository-relative file path, or a directory
    prefix ending in '/'. Prefix matching is on SEGMENT boundaries, which the
    trailing slash gives for free: `lexy-app/backend/routers/` authorizes
    `.../routers/books.py` but never `.../routers_backup/secret.py`. Exact
    entries never match by prefix, so naming a file authorizes that file alone.
    """
    exact = {a for a in approved if not is_directory_prefix(a)}
    prefixes = tuple(a for a in approved if is_directory_prefix(a))
    return {
        path
        for path in changed
        if path not in exact and not any(path.startswith(pre) for pre in prefixes)
    }


def authorized_cleanup_paths(requested, recorded) -> tuple[set[str], set[str]]:
    """Split `requested` into `(authorized, refused)` against `recorded`.

    The cleanup rule, and the ONLY matcher for it (scope-04, 2026-08-19). A
    task that DEMONSTRABLY created a file outside its scope must be able to
    delete it again — `recorded` is that demonstration, and nothing else is:
    it is `TaskExecution.out_of_scope_paths`, which the loop writes from its
    own two path comparisons (`unauthorized_paths` against
    `outcome.changed_paths` before the commit, and against git's
    `commit_range_paths` after it) and never from anything an agent asserts.
    `requested` is the opposite kind of input — an agent asking for something —
    and this function is the whole of what stands between the two.

    EXACT MATCH ONLY, deliberately unlike `unauthorized_paths` above. That one
    answers "does this scope AUTHORIZE this path", where a trailing '/' is a
    deliberate grant over a subtree. This one answers "did the loop see this
    exact path written out of scope", which is a statement about one file. So
    no prefix rule of any kind: a recorded `autoloop/obsolete.py` authorizes
    deleting `autoloop/obsolete.py` and nothing else — not a sibling, not
    `autoloop/obsolete.py.bak`, and not its directory. A recorded entry cannot
    be a directory prefix in the first place (both comparisons feed it literal
    file paths), and if one ever appeared it would still authorize only a file
    literally named with the trailing slash, i.e. nothing.

    This grants REPAIR and grants nothing else. It never reaches
    `Task.approved_paths` or `TaskExecution.allowed_paths`, so editing,
    recreating or renaming into an authorized path remains exactly as
    unauthorized as it was before the file was ever recorded.

    TWO callers since scope-05 (2026-08-24), one matcher, one authority:
    `implement_executor._apply_recorded_cleanup` acts on the `authorized` half
    by UNLINKING the file (`REMOVE-OUT-OF-SCOPE:`, scope-04), and
    `_apply_recorded_reverts` acts on it by restoring the file to its content at
    `TaskExecution.task_base_sha` (`REVERT-OUT-OF-SCOPE:`). Both narrow a diff
    back toward the declared scope and neither widens it; both do nothing else
    with what this returns. A second matcher for the second instruction is
    exactly the drift this function's own "one matcher" rule warns about, so
    there is not one — a path either is in the loop's record or it is not, and
    that single answer gates both requests.

    `refused` is returned rather than dropped so the caller can report what it
    ignored. A silently ignored request looks identical to a satisfied one in
    the round's outcome, and the reviewer who asked for the removal is the
    person who most needs to know it did not happen.
    """
    recorded_set = set(recorded)
    authorized = {p for p in requested if p in recorded_set}
    return authorized, set(requested) - authorized


class TaskState(str, Enum):
    READY = "ready"            # pending, all dependencies completed
    BLOCKED = "blocked"        # pending, at least one dependency incomplete
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    #: Quarantined by the orchestrator after a `task_fatal` park (see
    #: `orchestrator._to_needs_user` / `docs/AUTOLOOP.md`'s blockers
    #: section) — deliberately a DIFFERENT value than `BLOCKED`, which means
    #: "waiting on an incomplete dependency" and resolves itself once that
    #: dependency completes. A quarantined task never resolves itself; it
    #: stays out of `ready_tasks()`/`next_ready()` until an operator answers
    #: the recorded blocker (`python -m autoloop answer`), which calls
    #: `unblock()` below.
    BLOCKED_BY_OPERATOR = "blocked_by_operator"
    #: Superseded and never coming back. The THIRD meaning that used to be
    #: stored as `blocked`, and the one that made the dashboard's blocked count
    #: worthless: `BLOCKED` resolves itself when a dependency completes,
    #: `BLOCKED_BY_OPERATOR` resolves when an operator answers, and RETIRED
    #: resolves for nobody — the work was already done, or abandoned, under a
    #: different task id. As of 2026-08-14 six of the seven `blocked` tasks
    #: were retirements saying so only in free-text `blocked_reason`, so an
    #: operator reading "7 blocked" was reading one number for two opposite
    #: calls to action (needs you / needs nobody).
    #:
    #: The successor ids live in `Task.superseded_by`, so the supersession
    #: chain is machine-readable rather than prose. Neither the task nor its
    #: reason is ever deleted — the chain is the only record that (say)
    #: brw-07/brw-08 continue brw-02/brw-04, and that is regression history in
    #: the same sense `docs/SECURITY.md` findings are.
    RETIRED = "retired"
    #: DONE, under another task's commits (ship-01, 2026-08-23). The state the
    #: other three could not express, and each of them was tried first:
    #:
    #:   * `COMPLETED` — the merge sweep enumerates completed tasks and treats
    #:     one whose record names no candidate as UNRESOLVED, which makes the
    #:     whole invocation non-mutating (`merge_sweep.HELD`). Completing five
    #:     tasks that never had a branch to tidy the registry would have held
    #:     every future merge; one empty execution record already held the sweep
    #:     for hours on 2026-08-21.
    #:   * `RETIRED` — means SUPERSEDED BY NAMED SUCCESSORS, a different fact,
    #:     and it satisfies no dependency (`SATISFIES_DEPENDENCY`), so anything
    #:     waiting on it waits forever. `retire` also refuses a completed task
    #:     ("it was not superseded"), so it cannot describe the other half of the
    #:     disagreement at all.
    #:   * `BLOCKED_BY_OPERATOR` — where the five sat as a stopgap. It means "do
    #:     not work this", not "this shipped", and its reason is hand-written
    #:     prose that the next reader has to re-derive.
    #:
    #: What it carries is EVIDENCE, not a flag: `Task.shipped_commits` names the
    #: commits, `Task.shipped_note` says whose they are. The registry stores and
    #: shape-checks them; ancestry is checked by whoever has a checkout, at
    #: record time (`cli._cmd_record_shipped`) and again on every read
    #: (`dashboard.shipped_elsewhere_states`). A record pointing at a sha that is
    #: no longer an ancestor of the base head reads as a DISAGREEMENT and is
    #: never auto-converted back — detecting is in scope, rewriting is not.
    #:
    #: The sweep never asks it for a branch: `merge_sweep._backlog` enumerates
    #: `TaskState.COMPLETED` and nothing else, so this state is skipped without
    #: becoming a fourth `unresolved` reason — which would rebuild the exact
    #: failure it exists to avoid.
    SHIPPED_ELSEWHERE = "shipped_elsewhere"


@dataclass
class Task:
    id: str
    title: str
    description: str
    depends_on: tuple[str, ...] = ()
    #: Scheduling order for `next_ready()`. ASCENDING — 1 outranks 2, and
    #: the default sorts last, so an existing roadmap keeps its insertion
    #: order until someone actually assigns priorities. Matches the P0/P1/P2
    #: vocabulary the audit reports already use. Ties break on id, so the
    #: selection stays deterministic (a non-deterministic `next_ready` would
    #: make "which task ran" depend on dict ordering).
    priority: int = 100
    status: str = "pending"  # pending | in_progress | completed | blocked | retired
    created_at: str = field(default_factory=utcnow_iso)
    completed_at: str | None = None
    #: Operator-facing reason, set by `TaskRegistry.block` and cleared by
    #: `unblock`. Empty for every task that has never been quarantined.
    #: `retire` writes it too and NOTHING clears it — a retirement's reason is
    #: the prose half of the record `superseded_by` makes machine-readable, and
    #: deleting it would delete the only account of why the work stopped.
    #: New field with a default — an old `tasks.json` written before this
    #: existed loads fine (`Task(**raw)` falls back to the default), see
    #: `TaskRegistry.from_dict` below and `test_blockers.py`'s backward-
    #: compatibility test.
    blocked_reason: str = ""
    #: WHO put this task in `blocked`, as a value rather than as prose.
    #: `HOLD_ORIGIN_OPERATOR` means an inbox hold placed by
    #: `TaskRegistry.operator_block`; `""` means everything else, including
    #: every loop-raised `task_fatal` quarantine and every task that has never
    #: been blocked at all.
    #:
    #: The authority `operator_unblock` reads. It exists as its own field
    #: because the alternative — matching `OPERATOR_HOLD_PREFIX` against
    #: `blocked_reason` — reads provenance out of unconstrained free text that
    #: the loop also writes, so a real quarantine could be released from the
    #: inbox with its `blockers.Blocker` record still open (see
    #: `HOLD_ORIGIN_OPERATOR`). Only `operator_block` ever sets it, `block` and
    #: `unblock` both clear it, and no reason text can produce it.
    #:
    #: New field with a default — an old `tasks.json` written before it existed
    #: loads with `""`, i.e. as a loop quarantine, which is the SAFE direction:
    #: a pre-existing hold has to be released by `python -m autoloop answer`
    #: (or re-held) rather than an unmarked row being releasable from the
    #: inbox. Same backward-compatible pattern as `blocked_reason` /
    #: `superseded_by` / `approved_paths`, plus a normalising read in
    #: `from_dict` so a hand-edited `null` cannot become `None` here.
    hold_origin: str = ""
    #: Which task(s) continue this one, set by `retire`. Empty is legal and
    #: means "retired with no successor" — `dash-01` went stale rather than
    #: being superseded, and inventing a successor for it would be worse than
    #: recording none.
    #:
    #: NOT a dependency. It is deliberately excluded from `_check_acyclic` and
    #: from the known-task check `depends_on` gets: a successor may be planned
    #: later (or never — a retired task's successor can itself be retired), and
    #: nothing schedules off this field. Only its SHAPE is validated
    #: (`_validate_superseded_by`). New field with a default, same
    #: backward-compatible pattern as `blocked_reason`/`approved_paths`.
    superseded_by: tuple[str, ...] = ()
    #: Validation commands for THIS task, overriding the configured default.
    #: Empty means "use the configured commands". A task whose change lives
    #: outside what the default commands exercise must declare its own, or
    #: validation passes vacuously: the configured set is ruff + the autoloop
    #: and root-pipeline suites, none of which touch `lexy-app/backend`, so a
    #: backend change would be "validated" without a single test running
    #: against it — including the test the agent just wrote.
    validation: tuple[tuple[str, ...], ...] = ()
    #: Directory, relative to the repo root, that `validation` runs from.
    #: Empty means the repo root. The backend suite must run from
    #: `lexy-app/backend` — `python -m` puts the cwd on `sys.path`, which some
    #: of its tests need (CLAUDE.md §11).
    validation_cwd: str = ""
    #: The machine-checkable authorization scope for a write-capable
    #: (`implement`/`revise`) dispatch of this task — EXACT repository-
    #: relative paths, validated by `_validate_approved_path` on the way in
    #: (`TaskRegistry.add_many`, so both a ChatGPT `plan` and
    #: `seed_tasks.json` go through the same gate). Empty means "no scope has
    #: been authorized yet"; `orchestrator._dispatch_task_postcommit` refuses
    #: to dispatch a non-audit implement/revise for a task whose
    #: `approved_paths` is empty (see docs/SECURITY.md finding #2/circular
    #: ownership) rather than letting the executor's own report define its
    #: authorization. New files must be named explicitly up front — there is
    #: no "anything the agent happens to touch" default. New field with a
    #: default — an old `tasks.json` written before this existed loads fine
    #: (`Task(**raw)` falls back to `()`), same backward-compatible pattern as
    #: `blocked_reason`/`validation` above.
    approved_paths: tuple[str, ...] = ()
    #: The approved decomposition this task is implemented from, as the text
    #: `contract.Decomposition.render()` produced — approach, expected files,
    #: and either one step or an ordered list of independently reviewable ones.
    #: Empty means "no plan approved yet", and
    #: `policy.authorize_directive` refuses an `implement` in that state unless
    #: the directive itself carries one (see `contract.Decomposition`).
    #:
    #: TEXT rather than a list of step records, deliberately. It is agent
    #: instructions in the same category as `description`, nothing schedules or
    #: dispatches per step, and a machine-actionable step list here would be a
    #: second way to split a task. There is exactly one way, and it is a
    #: reviewer's `plan` answering an attempt-ceiling classification request
    #: (`orchestrator._dispatch_ceiling_split`, ceil-01): subtasks added,
    #: parent's spend carried onto them, parent retired into them. This comment
    #: used to credit `split-01` with that mechanism — a task recorded completed
    #: whose work never shipped, so it named code that did not exist.
    #:
    #: Written only by `set_decomposition`, from the dispatch path, before the
    #: executor for that round starts. New field with a default, so a
    #: `tasks.json` written before it existed loads unchanged — same
    #: backward-compatible pattern as `approved_paths` above.
    decomposition: str = ""
    #: WHEN this task was made the loop's urgent target, as a UTC timestamp.
    #: `""` — the ordinary state — means "not urgent".
    #:
    #: THE PIN. At most one task in the registry carries it at a time
    #: (`TaskRegistry.request_urgent` refuses a second while the first is still
    #: waiting), it sorts that task ahead of everything else in `next_ready()`
    #: regardless of `priority`, and `mark_in_progress` CONSUMES it — the pin
    #: asks for one dispatch, not for permanent precedence.
    #:
    #: A separate field rather than `priority = 0`, and that is the whole point
    #: of it. Priority cannot preempt: it is an integer other tasks already
    #: share, so raising a task to P0 on 2026-08-21 only TIED with the P0 task
    #: already mid-round and lost the id tiebreak ("brw-13" < "codex-01"). A
    #: field that exists on exactly one task cannot tie, and unlike a magic
    #: number it also cannot be reached by an ordinary operator re-prioritisation
    #: that meant nothing so strong.
    #:
    #: New field with a default, so a `tasks.json` written before it existed
    #: loads unchanged — same backward-compatible pattern as `decomposition`,
    #: plus the normalising read in `from_dict` that keeps a hand-edited `null`
    #: from becoming `None` here.
    urgent_at: str = ""
    #: The operator's account of WHY this task is urgent, required by
    #: `request_urgent` for the same reason `operator_block` requires one: a
    #: preemption that discards a round's work with no recorded reason is the
    #: free-text blocker problem, not a fix for it. `""` whenever `urgent_at`
    #: is, and cleared with it.
    urgent_reason: str = ""
    #: The commits that CARRY this task's work, when it shipped under another
    #: task's id — full lowercase shas, written only by
    #: `record_shipped_elsewhere` and validated by `_validate_shipped_commits`
    #: on both the write path and the load path
    #: (`_persisted_shipped_commits`).
    #:
    #: THE EVIDENCE, and the reason `status == "shipped_elsewhere"` is not a
    #: flag. Everything that reads this state re-checks these shas against the
    #: CURRENT base head rather than trusting the row: a record whose commits
    #: have stopped being ancestors (a rebase, a force-move, a reset) reads as a
    #: disagreement, exactly like a `completed` task whose work is not there.
    #: Empty means the row predates this field — on a `shipped_elsewhere` row it
    #: is an evidence-free claim, reported as a disagreement rather than
    #: believed.
    #:
    #: NOT a dependency and not a branch. Nothing schedules off it, the merge
    #: sweep never asks this task for a branch (it never had one), and no code
    #: here fetches, resolves or merges these commits.
    shipped_commits: tuple[str, ...] = ()
    #: WHERE the work landed, in words — "shipped under inbox-02's commits".
    #: Required by `record_shipped_elsewhere`; the machine-readable half is
    #: `shipped_commits` above, and this is the half the dashboard group prints,
    #: the same job `superseded_by` does for a retirement.
    shipped_note: str = ""
    #: WHEN the record was written, as a UTC timestamp. Not evidence — the
    #: ancestry check is — but it is what tells a reader whether they are
    #: looking at a claim made before or after the base moved.
    shipped_at: str = ""
    #: How many times a reviewer `recut` has discarded this task's execution and
    #: sent it back to be cut again from the base (recut-01, 2026-08-24).
    #: Incremented by `recut` below, and by nothing else.
    #:
    #: THE DURABLE COUNT, and the reason it lives here rather than only on the
    #: execution record the cap is nominally "enforced on". A recut ARCHIVES
    #: that record (`worktask.retire_execution`) and the next dispatch writes a
    #: fresh one, so a counter kept solely there would read 0 on every cut and
    #: the cap would enforce nothing — a guard that switches itself off exactly
    #: when it is needed. `worktask.TaskExecution.recut_count` mirrors this for
    #: the reviewer and the operator to read; `orchestrator._recut_count_for`
    #: takes the HIGHER of the two, so neither copy can lower the count.
    #:
    #: NEVER reset. Two clean rebuilds that still could not produce a reviewable
    #: candidate is evidence about the SPECIFICATION, and that evidence has to
    #: survive the very operation it is bounding. An operator who decides a task
    #: deserves another cut edits `tasks.json` with the loop stopped, which is a
    #: deliberate, visible act — exactly what handing a destructive verb to the
    #: reviewer is not allowed to be.
    #:
    #: New field with a default, same backward-compatible pattern as the fields
    #: above, plus `_persisted_recut_count`, which REFUSES a stored value it
    #: cannot read rather than defaulting it to 0.
    recut_count: int = 0
    #: How many times the reviewer has EXTENDED this task's attempt budget after
    #: it reached the attempt ceiling (ceil-01, 2026-08-25). Written only by
    #: `grant_attempt_extension` below.
    #:
    #: The budget it extends is `orchestrator.MAX_TASK_ATTEMPTS`, and the number
    #: of extensions is itself capped (`orchestrator.MAX_CEILING_EXTENSIONS`), so
    #: this widens the ceiling by a bounded amount and never removes it. It lives
    #: HERE, not on the execution record, for exactly the reason `recut_count`
    #: does: a split or a recut ARCHIVES that record, and a grant counted only
    #: there would read 0 on the next one — a bound that switches itself off at
    #: the moment it is doing work.
    #:
    #: Validated at load by `_persisted_nonneg_int`, which REFUSES a value it
    #: cannot read rather than defaulting it to 0: reading an unreadable grant
    #: count as "none spent yet" hands back the whole allowance silently.
    attempt_extensions: int = 0
    #: Attempts this task's PARENT had already spent when a reviewer decomposed
    #: it at the attempt ceiling — carried onto every child at creation and never
    #: rewritten afterwards.
    #:
    #: THE ANTI-REFUND. A split that gave each subtask a fresh
    #: `MAX_TASK_ATTEMPTS` would make the ceiling mean nothing: a looping task
    #: could buy an unbounded budget by being split again and again. The parent's
    #: spend is therefore a DEBT the children inherit —
    #: `orchestrator._attempt_cap_for` subtracts it — floored so that a child
    #: still has usable attempts (`orchestrator.MIN_CHILD_ATTEMPTS`), because a
    #: child born with a budget of zero simply rebuilds the park this exists to
    #: remove.
    inherited_attempts: int = 0
    #: How many ceiling decompositions separate this task from a task a reviewer
    #: planned directly: 0 for every ordinary task, parent's + 1 for a child.
    #: `orchestrator.MAX_SPLIT_DEPTH` reads it, and is what stops a subtask
    #: splitting again without limit.
    split_depth: int = 0
    #: WHEN this task asked the reviewer to classify it at the attempt ceiling,
    #: as a UTC timestamp. `""` — the ordinary state — means "not waiting on a
    #: classification".
    #:
    #: THE ASKED-ONCE RECORD, and the reason a ceiling hit cannot ping-pong. The
    #: ceiling check spends no attempt and no denial budget, so a loop that
    #: merely re-asked on every dispatch would re-ask forever with every
    #: automated signal green. `orchestrator._handle_attempt_ceiling` parks
    #: instead when this is already set: the reviewer was asked, and an answer
    #: that did not classify the task is not grounds to ask again.
    ceiling_plan_requested_at: str = ""


#: The repository trackers every task may update, WITHOUT naming them in its
#: own `approved_paths`. THE list — not a default that something else can
#: override at runtime. It reaches `effective_approved_paths` as its `trackers`
#: argument, and the only value any production caller passes is this constant
#: (`Orchestrator._tracker_paths`).
#:
#: Not a convenience. `CLAUDE.md` makes updating these a CONDITION of doing the
#: work: §12 requires `docs/SUMMARY.md` whenever a file is added, removed or
#: changes responsibility, and `docs/TESTS.md` whenever a test is added,
#: removed or renamed; §14 requires `docs/SECURITY.md` in the same change as a
#: security-relevant edit; §12 requires `docs/COMMON_ERRORS.md` when something
#: new breaks. So a task that adds a file and does not list `SUMMARY.md`
#: cannot be completed while obeying the repo's own rules — the agent must
#: violate one or the other. rt-01 hit exactly that twice: the scope was
#: widened by four paths after the first refusal and STILL missed
#: `docs/SUMMARY.md`, because enumerating obligations by hand per task does
#: not converge.
#:
#: **Fixed constant, deliberately NOT runtime configuration.** Widening the
#: scope of every task at once must be a diff someone reviews, never an edit to
#: a file nobody reviews. `.autoloop/config.toml` is exactly such a file — it
#: lives under the gitignored state directory — so this list is not read from
#: it, and `[repo]` has no key that can supply one.
#:
#: **That was tried, on 2026-08-16, and withdrawn before shipping** (port-02,
#: `docs/SECURITY.md` S31). The rejected design was `[repo].tracker_paths`
#: bounded by a filename-suffix blocklist ("nothing that looks like code or
#: configuration"). The blocklist cannot carry that weight: `.env`,
#: `.gitignore`, `Makefile`, `Dockerfile`, `Gemfile` and any extensionless
#: script are all behaviour-changing files with no refused suffix, and the set
#: of such names is open-ended, so extending the list is not a fix. A hard
#: control was being swapped for an unenforceable heuristic.
#:
#: **How another repository declares its own trackers, then.** By editing this
#: constant — which is not a workaround, because `autoloop/` is VENDORED into
#: the repository it operates on (this package sits at the root of the app it
#: audits). So the constant already is per-repository metadata living in
#: git-tracked source: changing it in a target repo is a commit in that repo's
#: reviewed history, which is precisely the property a config edit lacks. The
#: obligations are real everywhere and the filenames are not, so the filenames
#: belong in the checkout that has them — just not in a file outside review.
#: If a declaration in tracked repository DATA (a committed `.autoloop.toml`
#: at the repo root) is ever wanted instead, that is the recorded forward path
#: in S31; the requirement it must meet is that changing it appears in the
#: repository's history, not that it is merely per-repository.
#:
#: The residual risk, stated rather than hidden: `docs/SECURITY.md` is the
#: security tracker, so an agent can now edit the record of a finding without
#: that being named in its task. It is documentation and not a control — the
#: controls are in code — but a weakened finding is a real (if modest) way to
#: mislead a later reader, and it is the reason this list stays this short.
#:
#: Widened 2026-08-04 by two entries, each earned by a REFUSAL rather than
#: guessed at. Three tasks in two days escaped scope, and every escape was the
#: same shape: a doc update describing the task's OWN change.
#:   * `CLAUDE.md` — rt-06 updated the backend test count (1258 → 1266) that
#:     its own 8 new tests made stale. §11 quotes that count in two places.
#:   * `docs/SCHEMA.md` — rt-02 added one migration-table row recording that
#:     025's downgrade is now guarded, which IS the change rt-02 exists to make.
#: Answering these one task at a time does not converge, for the same reason
#: rt-01 did not: the obligation is repo-wide, so enumerating it per task keeps
#: missing a different entry each time.
#:
#: `CLAUDE.md` is the sharpest entry here, and is called out rather than lumped
#: in with the docs. Unlike them it is not only a record — it is the
#: INSTRUCTIONS future agents read, so an executor can now edit the rules it
#: will later operate under without that being named in its task. Three things
#: bound that, none of which is "trust the agent": the file changes no runtime
#: behaviour, `approved_paths` is still enforced from the Task and never from
#: anything an agent writes, and every edit stays visible in
#: `commit_range_paths`.
#:
#: One interaction worth naming, because it used to weaken the old "the
#: reviewer sees every tracker edit" argument: since report-first packets
#: (2026-08-04) a diff over `packet.DIFF_INCLUDE_MAX_CHARS` was OMITTED, so on
#: a large commit the reviewer saw the tracker PATH in the changed-path list —
#: always rendered, always git-read — but not the edited TEXT. Since chunked
#: delivery (2026-08-14, `docs/AUTOLOOP.md` §5d-bis) an oversized patch is
#: normally sent as numbered parts instead, so the edited text does reach the
#: reviewer. The gap is narrower, not closed: a patch that still cannot be
#: chunked (no shared conversation on the provider, a part that fails to land,
#: over `packet.DIFF_MAX_PARTS`) falls back to the same omission. That is an
#: argument for keeping this list short, not for trusting it less.
TRACKER_PATHS: tuple[str, ...] = (
    "CLAUDE.md",
    "docs/COMMON_ERRORS.md",
    "docs/SCHEMA.md",
    "docs/SECURITY.md",
    "docs/SUMMARY.md",
    "docs/TESTS.md",
)


def effective_approved_paths(
    approved: tuple[str, ...], trackers: tuple[str, ...] = TRACKER_PATHS
) -> tuple[str, ...]:
    """A task's authorized paths PLUS the always-allowed trackers, sorted.

    The single place the two are combined, so the dispatch-time seed and the
    every-dispatch re-sync cannot disagree — if only one of them applied the
    trackers, the other would silently narrow the scope back on the next round.
    Both callers in `orchestrator.py` must pass the SAME `trackers` value for
    the same reason: a re-sync comparing against a different list than it
    assigns would report the record dirty on every dispatch, forever.

    `trackers` is a PARAMETER so tests can state a list explicitly, and it is
    fed from reviewed source in production and from nowhere else: the only
    caller is `Orchestrator._tracker_paths()`, which returns `TRACKER_PATHS`.
    Nothing reads it out of the loop's runtime config, deliberately — see
    `TRACKER_PATHS` above and `docs/SECURITY.md` S31. A repository declares its
    own list by editing that constant in its own vendored copy, which is a
    reviewed commit.

    An EMPTY `approved` stays empty. "No scope authorized yet" must keep
    refusing dispatch (`docs/SECURITY.md` finding #2, circular ownership);
    returning just the trackers would turn an unscoped task into a dispatchable
    one that may write documentation, which is not what "unscoped" means. That
    is a property of `approved`, never of `trackers`: a repository that
    declares NO trackers at all still authorizes exactly what each task
    declares, and an unscoped task still gets nothing.
    """
    if not approved:
        return ()
    return tuple(sorted(set(approved) | set(trackers)))


def deletable_paths(
    requested, approved: tuple[str, ...], trackers: tuple[str, ...] = TRACKER_PATHS
) -> tuple[set[str], set[str], set[str]]:
    """Split `requested` into `(authorized, outside, tracker_refused)` — may a
    round DELETE a file its own `approved_paths` already let it WRITE?

    The del-01 rule (2026-08-25), and deliberately a DIFFERENT question from
    `authorized_cleanup_paths` above. That one answers "did the loop record this
    exact path as written OUTSIDE the task's scope", and its authority is
    `TaskExecution.out_of_scope_paths` — a record no agent can add to. This one
    answers "is this path INSIDE the task's scope", and its authority is
    `Task.approved_paths`. Two authorities for two questions, kept apart on
    purpose: merging them would let a request select from something it can
    influence.

    **The scope half is `unauthorized_paths` and nothing else.** Not a copy of
    its rule, not a second matcher with the same intent — the function itself, on
    `effective_approved_paths(approved, trackers)`, which is the same call the
    prompt's APPROVED SCOPE section and both of the loop's own scope comparisons
    make. So a deletion is refused by EXACTLY the code that records an
    out-of-scope write, a trailing '/' means a subtree at both ends, and the two
    can never drift into disagreeing about one path. An empty `approved` makes
    `effective_approved_paths` return `()`, so an unscoped task can delete
    nothing — the same fail-closed answer it already gets for writing.

    **A TRACKER PATH IS REFUSED even though writing it is allowed**, and that is
    the one place this deliberately does not follow the write rule.
    `effective_approved_paths` grants every task write access to `TRACKER_PATHS`
    so it can APPEND its change note; those files are append-only ledgers shared
    by every task in the repository, and a grant that exists for appending must
    not become a licence to remove one. The refusal is checked FIRST and reported
    as its own category, never folded into `outside`: "outside your approved
    paths" would be a false statement about a path the task may write, and a
    reviewer chasing that sentence would look in the wrong place.

    It refuses a tracker unconditionally — including when the task's OWN
    `approved_paths` covers it independently (an exact `docs/TESTS.md` entry, or
    a `docs/` prefix). Two tasks' change notes are the thing being protected, and
    which grant happens to reach the file does not change that.

    `outside` and `tracker_refused` are returned rather than dropped so the
    caller can report what it refused, for the reason `authorized_cleanup_paths`
    gives: a silently ignored request looks identical to a satisfied one.
    """
    tracker_set = set(trackers)
    requested_set = set(requested)
    tracker_refused = requested_set & tracker_set
    rest = requested_set - tracker_refused
    outside = unauthorized_paths(rest, effective_approved_paths(approved, trackers))
    return rest - outside, outside, tracker_refused


@dataclass(frozen=True)
class StrandReport:
    """Who a task would leave waiting forever if it reached a terminal
    non-completed status right now — the answer `TaskRegistry.
    stranded_dependents` returns.

    ONE report shape for the whole question, deliberately. Only `retire`
    reaches such a status today, but the rule is a property of `state_of`
    (`!= "completed"` satisfies nothing), not of retirement, so any future
    terminal transition asks the same question by calling the same method
    rather than re-deriving "who depends on this" per call site. That is what
    the CLI prints, what the refusal message is built from, and what a test
    asserts against.

    `direct` names the tasks that declare the id in their own `depends_on` —
    every one of them, because those are the dependencies that become
    unsatisfiable, and a refusal that truncated the list would leave an
    operator re-running the command to discover the rest. `transitive` is the
    whole closure INCLUDING `direct`, in breadth-first order: 4 direct reads
    very differently from 21 in total, and the operator's decision turns on
    the second number (measured 2026-08-20 — `roadmap-01` had 4 direct
    dependents and the entire 21-task ingest line behind them).

    A dependent already `completed` or `retired` appears in NEITHER, and is
    not descended through. It cannot be stranded — it is a record, nothing is
    waiting to dispatch it — and counting it would make the refusal fire on
    graphs where nothing is actually at risk.

    `in_progress` is the subset of `direct` that is mid-round. Split out
    because those are the ones a dependency rewrite may not touch: `state_of`
    checks dependencies BEFORE the in-progress branch, so rewriting them
    under a running dispatch is exactly the strand `_refuse_immutable`
    documents.
    """

    direct: tuple[str, ...] = ()
    transitive: tuple[str, ...] = ()
    in_progress: tuple[str, ...] = ()

    @property
    def strands(self) -> bool:
        """Is there anything to refuse for? False for the ordinary case — a
        task nothing depends on, which must retire exactly as it does today."""
        return bool(self.direct)

    def describe(self) -> str:
        """`4 dependents (ingest-01, …); 21 tasks blocked in total, counting
        those behind them`.

        BOTH numbers, always, each labelled. The transitive count is not
        printed only when it is larger — a reader who saw it sometimes and not
        others would have no way to tell "nothing behind them" from "this
        message does not report that". It equals the direct count when nothing
        is behind them, which is a fact, not an omission. An unlabelled `21`
        beside a list of 4 ids, meanwhile, reads as a bug.

        Says `no dependents` rather than an empty string when there is nothing,
        so a caller that prints it unconditionally still prints a sentence.
        """
        if not self.direct:
            return "no dependents"
        listed = ", ".join(self.direct)
        total = len(self.transitive)
        text = (
            f"{len(self.direct)} dependent{'s' if len(self.direct) != 1 else ''} "
            f"({listed}); {total} task{'s' if total != 1 else ''} blocked in total"
        )
        if total > len(self.direct):
            text += ", counting those behind them"
        return text


class TaskRegistry:
    def __init__(self, tasks: list[Task] | None = None):
        self._tasks: dict[str, Task] = {}
        #: Ids whose priority THIS registry has deliberately set since the last
        #: save. In-memory only, never persisted, cleared by `TaskStore.save`.
        #:
        #: It exists because `TaskStore.save` reconciles priorities from disk
        #: (an operator may have edited one while this registry sat in memory
        #: for a whole round — see that method), and reconciliation must not
        #: undo a change the loop itself just made. A drained inbox `priority`
        #: request is exactly that case: it is a deliberate write, so it takes
        #: precedence over whatever the file says.
        self._priority_overrides: set[str] = set()
        if tasks:
            self.add_many(tasks)

    def priority_overrides(self) -> frozenset[str]:
        """Ids whose priority this registry set since the last save."""
        return frozenset(self._priority_overrides)

    def clear_priority_overrides(self) -> None:
        """Called by `TaskStore.save` once the values are on disk: from then on
        the file and this registry agree, so the next reconciliation has
        nothing to protect."""
        self._priority_overrides.clear()

    # ---- mutation -----------------------------------------------------------

    def add_many(self, tasks: list[Task]) -> None:
        """Add a batch atomically: either every task passes validation or none
        is added. Dependencies may reference earlier-known tasks or tasks in
        the same batch."""
        candidate = dict(self._tasks)
        for task in tasks:
            if not _ID_RE.match(task.id or ""):
                raise TaskGraphError(
                    "bad_task_id",
                    f"'{task.id}' is not a valid task id (slug of [A-Za-z0-9._-], max 64)",
                )
            if task.id in candidate:
                raise TaskGraphError("duplicate_task", f"task id '{task.id}' already exists")
            if not task.title.strip():
                raise TaskGraphError(
                    "empty_task_field", f"task '{task.id}' needs a title"
                )
            _validate_description(task.id, task.description)
            task.approved_paths = _validate_approved_paths(task.id, task.approved_paths)
            task.superseded_by = _validate_superseded_by(task.id, task.superseded_by)
            # Only when something is there: every ordinary creation leaves this
            # empty, and `_validate_shipped_commits` refuses an empty list
            # because on the WRITE path emptiness means an evidence-free claim.
            # A `seed_tasks.json` row that does carry evidence is held to the
            # same shape rule `record_shipped_elsewhere` applies, so creation
            # cannot be the one door a malformed sha list walks through.
            if task.shipped_commits:
                task.shipped_commits = _validate_shipped_commits(
                    task.id, task.shipped_commits
                )
            candidate[task.id] = task
        # A second pass, after every task in the batch is in `candidate`, so a
        # dependency on a task added by this same call resolves.
        for task in tasks:
            task.depends_on = _validate_depends_on(task.id, task.depends_on, candidate)
        _check_acyclic(candidate)
        self._tasks = candidate

    def add(self, task: Task) -> None:
        self.add_many([task])

    def mark_in_progress(self, task_id: str) -> Task:
        task = self.get(task_id)
        state = self.state_of(task_id)
        if state is TaskState.COMPLETED:
            raise TaskGraphError("task_completed", f"task '{task_id}' is already completed")
        if state is TaskState.BLOCKED:
            raise TaskGraphError(
                "task_blocked",
                f"task '{task_id}' is blocked by incomplete dependencies",
            )
        if state is TaskState.BLOCKED_BY_OPERATOR:
            # Defense in depth alongside `policy._check_task_reference` (the
            # primary gate every TASK_DECISIONS directive passes through
            # before dispatch reaches here): without this, a caller that
            # bypasses policy (a test, a future dispatch path) could
            # silently overwrite `status="blocked"` with `"in_progress"`,
            # un-quarantining a task no operator ever answered.
            raise TaskGraphError(
                "task_blocked_by_operator",
                f"task '{task_id}' is quarantined — answer its blocker first "
                "(`python -m autoloop answer`)",
            )
        if state is TaskState.RETIRED:
            # The same defense in depth, for the opposite reason: a quarantine
            # is a decision nobody has made yet, a retirement is one already
            # made. Re-dispatching a retired task redoes work that shipped
            # under its successor's id — and `policy._check_task_reference`
            # denying it is not the only thing that should stand in the way.
            raise TaskGraphError(
                "task_retired",
                f"task '{task_id}' is retired{_successor_hint(task)} — plan a "
                "new task instead of reviving it",
            )
        if state is TaskState.SHIPPED_ELSEWHERE:
            # The same defense in depth, and the one that actually catches the
            # dispatch path: `policy._check_task_reference` has no arm for this
            # state and falls through to `Verdict.ok()`, so THIS refusal is what
            # stands between a directive naming a shipped-elsewhere id and a
            # round that redoes work already in the base. (policy.py is outside
            # ship-01's approved scope; the gap is reported rather than reached
            # into.) Redoing it is not merely wasted — the second attempt would
            # commit a second copy of code that is already there.
            raise TaskGraphError(
                "task_shipped_elsewhere",
                f"task '{task_id}' is recorded as shipped elsewhere"
                f"{_shipped_hint(task)} — its work is already in the base; plan a "
                "new task rather than redoing it",
            )
        task.status = "in_progress"
        # THE PIN IS CONSUMED HERE, and this is the only place it is spent.
        # An urgent request asks for ONE dispatch — this one — not for
        # permanent precedence, and leaving the marker on would keep the task
        # ahead of every future selection while also holding the single urgent
        # slot shut against the next operator who needs it. Cleared at the
        # moment the dispatch it asked for actually starts, so a request that
        # never reached a dispatch still stands.
        task.urgent_at = ""
        task.urgent_reason = ""
        return task

    def mark_completed(self, task_id: str) -> Task:
        task = self.get(task_id)
        state = self.state_of(task_id)
        if state is TaskState.COMPLETED:
            raise TaskGraphError("task_completed", f"task '{task_id}' is already completed")
        if state is TaskState.BLOCKED:
            raise TaskGraphError(
                "task_blocked",
                f"task '{task_id}' cannot complete while dependencies are incomplete",
            )
        if state is TaskState.BLOCKED_BY_OPERATOR:
            # The same defense in depth `mark_in_progress` applies, and for the
            # same reason: a quarantine records a decision the operator has not
            # made yet. Completing over it does not resolve that decision, it
            # deletes it — the task disappears from the roadmap as "done" and
            # the operator is never asked. Found 2026-08-04 by the B10 test:
            # `_mark_task_completed` runs after a successful push, and a task
            # quarantined between dispatch and push was silently completed,
            # `blocked_reason` and all.
            raise TaskGraphError(
                "task_blocked_by_operator",
                f"task '{task_id}' is quarantined — answer its blocker before "
                "completing it (`python -m autoloop answer`)",
            )
        if state is TaskState.RETIRED:
            # Completing a retirement would erase it: the row would read as
            # finished work on the dashboard, the supersession chain would
            # point at a "completed" task nobody ever ran, and the merge panel
            # would start asking git whether a commit that does not exist is in
            # the branch. A retired task's outcome already happened elsewhere.
            raise TaskGraphError(
                "task_retired",
                f"task '{task_id}' is retired{_successor_hint(task)} — the work "
                "it describes did not complete under this id",
            )
        if state is TaskState.SHIPPED_ELSEWHERE:
            # Completing this would DELETE the evidence-backed record and put
            # the task straight into the merge sweep's enumeration, where it has
            # no branch and no execution record — the `unresolved` → `HELD`
            # shape this state was created to keep out of the sweep, arrived at
            # by tidying. The bare `task.status = "completed"` below would
            # otherwise do exactly that, silently, keeping the shas on the row
            # while the status stopped meaning what they support.
            raise TaskGraphError(
                "task_shipped_elsewhere",
                f"task '{task_id}' is recorded as shipped elsewhere"
                f"{_shipped_hint(task)} — its work landed under another task's "
                "commits, so it never had a branch of its own to complete. "
                "Completing it would put it into the merge sweep with nothing to "
                "merge, which holds every sweep",
            )
        task.status = "completed"
        task.completed_at = utcnow_iso()
        return task

    def block(self, task_id: str, reason: str) -> Task:
        """Quarantine `task_id` after a `task_fatal` park: set it aside so
        `ready_tasks()`/`next_ready()` skip it (via `state_of` below,
        without either of those methods needing to change) while continuous
        mode keeps working whatever else is READY. Idempotent — blocking an
        already-blocked task just refreshes the reason, since a task can in
        principle hit a second `task_fatal` park before an operator answers
        the first (e.g. re-dispatched after a crash). Refuses a completed task,
        which can never legitimately need quarantining, and a RETIRED one.

        The retired refusal is the last write path that could silently
        un-retire a task: a bare `task.status = "blocked"` would overwrite
        `"retired"` and put the row back under "needs a human", with the
        supersession chain still attached and nothing to say what happened. It
        should be unreachable — this is called from `_handle_parked_task` for a
        task that just parked, and a retired task cannot be dispatched
        (`policy._check_task_reference`, `mark_in_progress`) — so reaching it
        means the dispatch guards were bypassed, and `_handle_parked_task`
        fail-closing to loop_fatal on the refusal is the right answer to that,
        not something to smooth over."""
        task = self.get(task_id)
        if task.status == "completed":
            raise TaskGraphError(
                "task_completed", f"task '{task_id}' is already completed — cannot block it"
            )
        if task.status == "retired":
            raise TaskGraphError(
                "task_retired",
                f"task '{task_id}' is retired{_successor_hint(task)} — it cannot be "
                "quarantined, and should never have been dispatched",
            )
        if task.status == "shipped_elsewhere":
            # The same reasoning as the retired refusal directly above, and the
            # same shape of hole it closes: the bare `task.status = "blocked"`
            # below would overwrite the record, leaving the shas and the note on
            # a row that now reads "needs a human" with nothing to say what
            # happened. It should be unreachable — `_handle_parked_task` blocks
            # a task that just parked, and `mark_in_progress` refuses to
            # dispatch this state — so reaching it means the dispatch guards
            # were bypassed, and that caller's fail-close to loop_fatal is the
            # right answer rather than something to smooth over.
            raise TaskGraphError(
                "task_shipped_elsewhere",
                f"task '{task_id}' is recorded as shipped elsewhere"
                f"{_shipped_hint(task)} — it cannot be quarantined, and should "
                "never have been dispatched",
            )
        task.status = "blocked"
        task.blocked_reason = reason
        # UNCONDITIONAL, and never a function of `reason`. This is the loop's
        # quarantine, so its provenance is "not an operator hold" whatever the
        # reason text happens to say — including a park detail that begins with
        # `OPERATOR_HOLD_PREFIX`. Clearing (rather than leaving) the field also
        # covers the idempotent re-block: a second park on a task must not
        # inherit a marker from whatever put it in `blocked` the first time.
        task.hold_origin = ""
        return task

    def retire(
        self, task_id: str, superseded_by=(), reason: str = "",
        rewrite_dependents: bool = False,
    ) -> Task:
        """Record that `task_id` is superseded and will never be worked again.

        The third meaning `blocked` used to carry (see `TaskState.RETIRED`).
        Distinct from `block` because the two ask opposite things of an
        operator: a quarantine is a question waiting for them, a retirement is
        an answer that already happened somewhere else.

        `superseded_by` names the successor task(s) — the machine-readable half
        of what used to be prose in `blocked_reason`. Empty is legal:
        `dash-01` went stale at dispatch with nothing to continue it, and
        naming an invented successor would be worse than naming none.

        NOTHING IS DELETED. `blocked_reason` is preserved when `reason` is
        empty and overwritten only when a new one is given, `depends_on`,
        `approved_paths` and the timestamps are untouched, and there is no
        method here that removes a task from the registry. The supersession
        chain is the only record that a successor continues this work, so it
        is kept for the same reason `docs/SECURITY.md` keeps resolved
        findings: regression history.

        Accepts an IN-PROGRESS task, unlike `release`'s mirror-image guard.
        That is not an oversight — `dash-01` was `in_progress` at dispatch with
        no candidate and no execution record, which is exactly the shape of
        work that needs retiring, and a pending-only rule would have refused
        the one task that most needed this. It accepts a quarantined task too:
        deciding that a `task_fatal` park is never going to be worked is a
        retirement, and forcing it through `unblock` first would put the task
        back in the READY queue in between.

        Refuses `completed`, mirroring `block`: finished work is not superseded
        work, and rewriting it as retired would hide a real completion from the
        merge panel. That is no longer the ONLY refusal — see the strand
        precondition below, which was added after this paragraph was written.

        WRITTEN ONCE. A retirement is a historical record, so a second `retire`
        on the same task may not add, remove, change or reword anything: an
        exact repeat is a no-op that returns the task untouched, and everything
        else raises `task_already_retired`. `block` is idempotent in the other
        direction — it refreshes the reason — because a quarantine is a live
        question that can legitimately re-fire; a supersession cannot.

        The case that forced this: `python -m autoloop retire brw-02` with no
        `--superseded-by` used to reach an unconditional
        `task.superseded_by = successors` and assign `()`, silently erasing the
        `('brw-06',)` chain that is the ONLY record brw-06 continues brw-02 —
        deleting exactly the regression history everything else here is careful
        to keep. Argue with a recorded retirement by planning a task, not by
        overwriting the record of the last one.

        REFUSED WHEN IT WOULD STRAND A DEPENDENT (retire-01, 2026-08-23). This
        used to be documented as an accepted consequence: a task that DEPENDS
        on a retired one stays BLOCKED forever, because `state_of` counts a
        dependency satisfied only when it is `completed`, and a retirement has
        no reverse. The consequence is real — what was wrong is calling it
        acceptable. There is no supported command that returns such a
        dependent: `answer` needs an open blocker, `release` needs an
        in-progress task, `retire` means never worked again, and there is no
        `unblock`. Hand-editing `tasks.json` with the loop stopped was the only
        exit, which is the route blk-01 exists to remove. Measured 2026-08-20:
        an operator asked for `roadmap-01`, which had 4 direct dependents and
        the whole 21-task ingest line behind them; a human caught it, nothing
        in the code would have.

        So `stranded_dependents` runs first, and a retirement that would leave
        any dependent waiting forever is REFUSED, naming every one of them plus the
        transitive count. Refused, not silently repaired: rewriting another
        task's `depends_on` is a roadmap decision, and inferring it from a
        retirement would make this command edit tasks nobody named.

        Two things lift the refusal, and both do their work in THIS call so a
        crash cannot leave half of it done:

          * `superseded_by` naming successors that are ALL live tasks in this
            graph (present, and not themselves retired). Satisfaction is
            DIRECT: the successor id replaces `task_id` in each affected
            dependent's `depends_on`, which is what the dependents were
            actually waiting for. Lifting the refusal WITHOUT that rewrite
            would be a lie — the dependents would still name a retired id and
            still never dispatch. A successor that is not planned yet cannot
            satisfy anything (brw-06 was retired into brw-07/brw-08 before
            either existed), so it refuses and says so; the shape-only rule in
            `_validate_superseded_by` is unchanged for a task with nothing
            depending on it.
          * `rewrite_dependents=True` — the explicit opt-in. Same rewrite, but
            it accepts a partial or empty successor list: the retired id is
            replaced by whichever named successors are live, and dropped
            outright when none is. `python -m autoloop retire
            --rewrite-dependents`.

        Only DIRECT dependents are rewritten. The transitive ones never named
        this task; they unblock by themselves once the direct ones do, and
        rewriting them would edit dependencies the retirement says nothing
        about. That is why the report carries both numbers.

        An IN-PROGRESS direct dependent refuses the whole operation instead
        (`task_in_progress`): its dependencies are what the running dispatch is
        judged against, and rewriting them mid-round is precisely the strand
        `_refuse_immutable` exists to prevent.

        Retirement itself is NOT weakened by any of this. There is still no
        un-retire, it is still written once, and this is a precondition that
        runs before the write — never a way back out of one.
        """
        task = self.get(task_id)
        if task.status == "completed":
            raise TaskGraphError(
                "task_completed",
                f"task '{task_id}' is already completed — it was not superseded",
            )
        if task.status == "shipped_elsewhere":
            # Mirrors the completed refusal directly above, and for the same
            # reason: retirement means SUPERSEDED — the work described here did
            # not happen and a successor task describes what will. This row says
            # the opposite, with commits naming where the work already is.
            # Retiring it would also strand every dependent, since a retirement
            # satisfies nothing (`SATISFIES_DEPENDENCY`), so it would take a
            # record that unblocks its dependents and turn it into one that
            # blocks them forever.
            raise TaskGraphError(
                "task_shipped_elsewhere",
                f"task '{task_id}' is recorded as shipped elsewhere"
                f"{_shipped_hint(task)} — it was not superseded, its work is in "
                "the base, and retiring it would strand every task depending on it",
            )
        successors = _validate_superseded_by(task_id, superseded_by)
        if task.status == "retired":
            if rewrite_dependents:
                # A flag that quietly does nothing is worse than a refusal: the
                # operator would read "already retired; nothing changed" and
                # believe the strand they were trying to clear had been
                # cleared. The written-once record is not reopened for it —
                # a dependency left stranded by an EARLIER retirement is
                # rewritten through the `depends_on` mutation (`inbox`
                # `KIND_DEPENDS_ON` / `set_depends_on`), which is the operation
                # that actually describes what is being changed.
                raise TaskGraphError(
                    "task_already_retired",
                    f"task '{task_id}' is already retired{_successor_hint(task)} — "
                    "a repeat cannot rewrite dependents, because it cannot rewrite "
                    "the retirement either. Re-point the stranded tasks with a "
                    "`depends_on` mutation instead",
                )
            # Terminal and immutable. The two checks below are deliberately
            # asymmetric with the write path underneath: an OMITTED successor
            # list or reason means "say nothing about it", never "clear it", so
            # only a value that actually disagrees with what is recorded is a
            # refusal. Everything else falls through to the early return, which
            # is what keeps a repeat from reaching the assignments at all.
            if successors and successors != task.superseded_by:
                recorded = _successor_hint(task) or " with no successor recorded"
                raise TaskGraphError(
                    "task_already_retired",
                    f"task '{task_id}' is already retired{recorded} — a retirement "
                    "is history and is never rewritten; plan a new task instead",
                )
            if reason and reason != task.blocked_reason:
                raise TaskGraphError(
                    "task_already_retired",
                    f"task '{task_id}' is already retired and its reason is on "
                    f"record ({task.blocked_reason or '(none recorded)'}) — "
                    "retiring it again cannot reword it",
                )
            return task
        # EVERY refusal is behind this call and none of the writes are, which is
        # the atomicity guarantee: a retirement that cannot go through touches
        # neither the task nor a single dependent, so there is no half-applied
        # state to unwind and no un-retire needed to unwind it.
        rewrites = self._retirement_rewrites(task, successors, rewrite_dependents)
        task.status = "retired"
        task.superseded_by = successors
        if reason:
            task.blocked_reason = reason
        for dependent_id, depends_on in rewrites:
            self._tasks[dependent_id].depends_on = depends_on
        return task

    def _retirement_rewrites(
        self, task: Task, successors: tuple[str, ...], rewrite_dependents: bool
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """The `(dependent id, new depends_on)` pairs `retire` must write, or a
        `TaskGraphError` saying why the retirement is refused.

        PURE — it validates the whole mutation and mutates nothing. `retire`
        applies what it returns, in one pass, after every check has passed.
        Rewriting through `set_depends_on` in a loop was the obvious
        alternative and is the wrong one: it writes each task as it goes, so a
        refusal on the third dependent leaves the first two already re-pointed
        at a task that then never gets retired.

        The cycle check runs ONCE, on a candidate graph carrying every rewrite
        at once — the same shape `set_depends_on` uses, for the same reason.
        Substituting a successor can genuinely close a loop (retire A into B
        where B already waits on a dependent of A), and a per-task check would
        pass each edge individually and still build the cycle. Exactly ONE edge
        is exempt from it — a self-edge on the subject, which no supported
        route can produce and which is exempted anyway so the two halves of
        that guard cannot drift; see the comment at the candidate build. Every
        other cycle, however it got there, still refuses the whole operation.
        """
        report = self.stranded_dependents(task.id)
        if not report.strands:
            return ()  # the ordinary retirement: nothing is waiting on this task
        live = tuple(
            s for s in successors if self.has(s) and self._tasks[s].status != "retired"
        )
        unusable = tuple(s for s in successors if s not in live)
        if not rewrite_dependents and (not successors or unusable):
            raise TaskGraphError(
                "task_would_strand_dependents",
                f"retiring task '{task.id}' would strand {report.describe()} — a "
                "retired task never satisfies a dependency and a retirement has no "
                "reverse, so each of those would wait forever with no command able "
                f"to release it. {self._strand_remedy(task, unusable)}",
            )
        if report.in_progress:
            raise TaskGraphError(
                "task_in_progress",
                f"{', '.join(report.in_progress)} depends on '{task.id}' and is in "
                "progress — rewriting a running round's dependencies is what strands "
                "it (`state_of` reads them before the in-progress branch, so "
                "`mark_completed` and `release` would both refuse afterwards). Wait "
                "for the round, or `python -m autoloop release` it first",
            )
        planned: list[tuple[str, tuple[str, ...]]] = []
        candidate = dict(self._tasks)
        # A SELF-EDGE ON THE SUBJECT is dropped from the candidate — from that
        # copy only, never from the stored row, which this operation does not
        # write at all. The SECOND half of the guard in `stranded_dependents`,
        # and it exists because the first half alone is not enough: that one
        # refuses to count the subject as its own dependent, so the loop below
        # never re-points that row, so the edge rides into `_check_acyclic`
        # untouched and refuses an otherwise valid retirement as
        # `dependency_cycle: X -> X` — naming the retirement itself as the
        # loop. The subject is the task GOING terminal; its own dependencies
        # are inert afterwards (`state_of` answers RETIRED before it reads
        # them), so this edge can strand nobody and must not veto.
        #
        # UNREACHABLE TODAY, exactly as the comment there says: `from_dict`
        # cycle-checks the whole stored graph, so a `tasks.json` naming a task
        # after itself fails to LOAD rather than reaching a retirement, and
        # both mutation routes refuse one outright. Kept, and tested by
        # corrupting a loaded registry in memory, so the two halves of the
        # guard cannot drift apart — one without the other is worse than
        # neither, because it converts "reported as its own dependent" into
        # "refused for a cycle nobody can remove".
        #
        # ONLY the entries equal to `task.id`, and only on the subject. Two
        # narrower-than-obvious choices, each fail-open if widened:
        #   * dropping the subject from `candidate` entirely would be worse
        #     than the bug — `_check_acyclic` reads `color.get(dep)`, so a
        #     missing key is neither GRAY nor WHITE and EVERY edge into the
        #     subject stops being walked;
        #   * clearing the subject's whole `depends_on` would hide a cycle
        #     running back through a dependent this operation does not rewrite
        #     (a `completed`/`retired` one still names the subject and is
        #     excluded from `report.direct`).
        # A self-edge on any OTHER task is a corruption this retirement was not
        # asked about, and still refuses the whole thing.
        if task.id in task.depends_on:
            candidate[task.id] = replace(
                task, depends_on=tuple(d for d in task.depends_on if d != task.id)
            )
        for dependent_id in report.direct:
            dependent = self._tasks[dependent_id]
            depends_on = _substitute_dependency(
                dependent.depends_on, task.id, live, dependent_id
            )
            _validate_depends_on(dependent_id, depends_on, self._tasks)
            candidate[dependent_id] = replace(dependent, depends_on=depends_on)
            planned.append((dependent_id, depends_on))
        _check_acyclic(candidate)
        return tuple(planned)

    def _strand_remedy(self, task: Task, unusable: tuple[str, ...]) -> str:
        """The second half of the strand refusal: what the operator can do.

        Split out because the two situations need different sentences and a
        combined one would be wrong in both. Naming NO successor is the common
        case and the remedy is to name one; naming a successor the graph cannot
        wait on is the surprising case, and the message has to say which id and
        why, or the operator re-runs the identical command.
        """
        if not unusable:
            return (
                "Name the task that continues this work with `--superseded-by` (its "
                f"id replaces '{task.id}' in each dependent), or pass "
                "`--rewrite-dependents` to drop the dependency in the same operation."
            )
        why = ", ".join(
            f"{s} is not a task in this graph"
            if not self.has(s)
            else f"{s} is itself retired"
            for s in unusable
        )
        return (
            f"`--superseded-by` names {', '.join(unusable)}, which nothing can wait "
            f"on ({why}) — a supersession is a record and a successor need not exist, "
            "but only a live one can satisfy a dependency. Name a planned successor, "
            "or pass `--rewrite-dependents` to re-point the dependents anyway."
        )

    def unblock_obstacle(self, task_id: str) -> TaskGraphError | None:
        """Why `unblock(task_id)` would refuse, or None if it would succeed.

        Split out of `unblock` so a caller can REPORT the refusal without
        performing — or risking — the transition. `cli._cmd_answer` needs
        exactly that since blk-01: the release itself is done by
        `cli._reconcile_unblocked_tasks`, which is narrowed by provenance
        (`blocker_derived_blocked` excludes an operator hold), but the command
        still has to tell an operator why a task their answer did not requeue
        was never quarantined to begin with. Asking that question by CALLING
        `unblock` would answer it by releasing an operator's hold.

        Returns the error rather than a string so the wording — and the stable
        `code` an operator greps for (`task_retired`, `task_not_blocked`) —
        lives in exactly one place. `unblock` raises what this returns.
        """
        task = self.get(task_id)
        if task.status == "retired":
            # `answer` reaches here (`cli._cmd_answer`), so the message has to
            # say which of the two non-resolving states this is rather than
            # "not blocked" — a retired task looks blocked in every listing
            # written before this state existed.
            return TaskGraphError(
                "task_retired",
                f"task '{task_id}' is retired{_successor_hint(task)}, not "
                "quarantined — there is no blocker to answer",
            )
        if task.status == "shipped_elsewhere":
            # Its own arm rather than the generic "not blocked" below, for the
            # reason the retired arm has one: `answer` reaches here, and every
            # one of the five records this state was created for was sitting in
            # `blocked` the day before. An operator whose answer did not requeue
            # the task needs to be told it is not waiting on them at all.
            return TaskGraphError(
                "task_shipped_elsewhere",
                f"task '{task_id}' is recorded as shipped elsewhere"
                f"{_shipped_hint(task)}, not quarantined — there is no blocker to "
                "answer",
            )
        if task.status != "blocked":
            return TaskGraphError("task_not_blocked", f"task '{task_id}' is not blocked")
        return None

    def unblock(self, task_id: str) -> Task:
        """Reverse of `block`: back to `pending`, i.e. READY again once its
        dependencies are still satisfied (`state_of` re-derives that from
        `depends_on` exactly as for any other pending task). Called by
        `cli._reconcile_unblocked_tasks` once no OPEN blocker names the task
        any more, and by the inbox's `operator_unblock`.

        Deliberately no reverse of `retire`. Un-retiring is not an operator
        answer — it is the claim that a supersession was wrong, which means
        planning a task, not flipping a status back to pending and losing the
        chain in the process.
        """
        obstacle = self.unblock_obstacle(task_id)
        if obstacle is not None:
            raise obstacle
        task = self.get(task_id)
        task.status = "pending"
        task.blocked_reason = ""
        # Released is released: a pending task has no hold to have an origin,
        # and a marker left behind here would still be sitting on the row the
        # next time the loop quarantines it.
        task.hold_origin = ""
        return task

    def release(self, task_id: str) -> Task:
        """Return an IN-PROGRESS task to pending, so it can be picked again.

        A task is marked in-progress at dispatch and cleared when the round
        finishes. A `loop_fatal` park in between finishes nothing, so the
        task stays in-progress forever: `state_of` reports IN_PROGRESS,
        `next_ready` skips it, and no command could move it. `unblock` is not
        that command — it refuses anything that is not `blocked`, correctly,
        since a quarantine and an interrupted round are different situations
        with different evidence behind them.

        Observed 2026-08-02: `dash-02` was interrupted mid-round by an escape
        detection whose cause turned out to be operator activity, and the
        only way to get it back was to edit `tasks.json` by hand.

        Narrow on purpose. Refuses a task that is not in progress, so it
        cannot quietly un-complete finished work or launder a quarantine
        (`blocked` still goes through `unblock`, which is what the blocker
        record is tied to).
        """
        return self._return_to_pending(
            task_id,
            verb="release",
            remedy="returns an interrupted round to pending",
        )

    def shelve(self, task_id: str) -> Task:
        """Return an IN-PROGRESS task to pending WITHOUT discarding the round
        it holds. The registry half of `cli._cmd_shelve`.

        The STATUS move is byte-for-byte `release`'s — a task is selectable
        again or it is not, and there is only one way to be pending — so the
        two share `_return_to_pending` below rather than keeping two copies of
        one assignment that could drift. **The difference between the two verbs
        is entirely in what the CALLER does with the artefacts**, and it is the
        whole point of having two: `release` goes on to retire the execution
        record and quarantine the worker repo (`worktask.retire_execution`), so
        the task is redone from scratch; `shelve` deliberately leaves both where
        they are (`worktask.preserve_execution` attests that it did), so the
        next dispatch RESUMES the recorded round — same `candidate_sha`, same
        `review_round`, same `attempt_count`.

        Its own entry point rather than an argument to `release`, for a reason
        an operator feels: the refusal text differs. "`release` only returns an
        interrupted round to pending" is the wrong sentence to print at somebody
        who typed `shelve`, and a shared method taking a verb string is how you
        get one that is right for both.

        Observed 2026-08-20: dash-12 held a 5-file candidate at review round 1
        and its reviewer asked in as many words to "resume the existing dash-12
        worker repository and preserve its partial implementation; do not
        restart the task". It was also stranded `in_progress`, which
        `next_ready` skips. `release` was the wrong tool — it would have thrown
        away exactly what the reviewer asked to keep — and there was no right
        one, so `tasks.json` was edited by hand, with the loop stopped, three
        times in one night.

        Narrow on the same terms as `release`: anything whose STORED status is
        not `in_progress` is refused, so this can neither un-complete finished
        work nor launder a quarantine.

        **The one place it is deliberately WIDER than `release`, and it is the
        `state_of` reading rather than a new permission.** `state_of` returns
        BLOCKED — not IN_PROGRESS — for an in-progress task with an incomplete
        dependency, because the dependency test runs before the `in_progress`
        test. `release` asks `state_of` and therefore refuses exactly the task
        that is hardest to get back; this asks the STORED status, which is the
        same reading `_displaced_work_exists` and `_refuse_immutable` already
        take, and for the reason the first of those records: "`state_of` …
        would fall silent on the very task that is hardest to release".

        That widening grants nothing. `in_progress` is written by
        `mark_in_progress` alone, so a row carrying it IS a dispatched round
        whatever its dependencies now say, and the result of moving it is a
        `pending` row that `state_of` still reports as BLOCKED and `next_ready`
        still skips until the dependency lands — honest, and selectable the
        moment it is. `release` is untouched: changing what it accepts is out
        of bounds, and the two readings are one keyword apart here so the
        difference is visible rather than buried in a second copy.

        The dead end it reaches is already written down one method over:
        `_refuse_immutable` exists because such a task "can be neither
        completed nor returned to pending". That is still true of `release`,
        and it is what this arm answers.
        """
        return self._return_to_pending(
            task_id,
            verb="shelve",
            remedy="sets an interrupted round aside without discarding it",
            by_stored_status=True,
        )

    def recut_obstacle(self, task_id: str) -> TaskGraphError | None:
        """Why `recut` would refuse `task_id`, or `None` when it would move it.

        The ASKABLE form of the refusal, split out for the reason
        `unblock_obstacle` is: `orchestrator._dispatch_recut` has to tell the
        reviewer WHY before it performs anything destructive, and a caller that
        learned the answer by attempting the move and catching the exception
        would already have charged a cut by the time it knew. One set of rules,
        two readers, no second copy to drift.

        Raises `task_unknown` for an id that is not in the graph rather than
        answering `None`, so a caller cannot ask about a typo and read the
        silence as "eligible" — the same fail-closed reading `stranded_dependents`
        takes.
        """
        task = self.get(task_id)
        if task.status == "blocked" and task.hold_origin == HOLD_ORIGIN_OPERATOR:
            # Its own arm, ahead of the status test below, because that test
            # ACCEPTS `blocked`: without this the reviewer could release an
            # operator's hold by naming it in a recut, which is exactly the
            # laundering `blocker_derived_blocked` exists to prevent.
            return TaskGraphError(
                "task_operator_hold",
                f"task '{task_id}' is held by the operator "
                f"({task.blocked_reason or 'no reason recorded'}) — `recut` "
                "never releases an operator hold; only the operator does",
            )
        if task.status not in ("in_progress", "blocked"):
            return TaskGraphError(
                "task_not_in_progress",
                f"task '{task_id}' has status {task.status!r}, and `recut` only "
                "discards a round that is in progress or quarantined — there is "
                "nothing in flight to discard",
            )
        return None

    def recut(self, task_id: str) -> Task:
        """Return a task to pending so its execution can be DISCARDED and cut
        again from the current base, and charge the cut. The registry half of
        the reviewer's `recut` verdict (`contract.Decision.RECUT`).

        The third member of the `release` / `shelve` family, and it shares their
        one status assignment (`_return_to_pending`) for the same reason they
        share it with each other: there is exactly one way to be pending, and
        three copies of that line is a bug in whichever one drifts. What differs
        is entirely what the CALLER does with the artefacts —
        `orchestrator._dispatch_recut` retires both halves through
        `worktask.retire_execution`, exactly as `release` does.

        **Its own entry point, and `release` is deliberately untouched.** That
        method says "Narrow on purpose" and is what `cli._cmd_release` and
        `Orchestrator._preempt_for_urgent` call; widening it would change
        behaviour for two callers this has no business changing. The refusal
        text also has to name the verb the caller actually used.

        **Wider than `release` in exactly one way: it accepts a `blocked` task.**
        That is not a courtesy, it is the whole point. A contaminated candidate
        is normally already parked `task_fatal` by the time a recut is warranted
        — port-01 was `blocked` on `attempt_count_ceiling` when its reviewer
        reached that conclusion — so a verb that only accepted `in_progress`
        would refuse precisely when it is needed. Like `shelve`, it reads the
        STORED status rather than `state_of`: `state_of` answers BLOCKED for an
        in-progress task with an incomplete dependency, which would again hide
        the task that is hardest to recover.

        **It never reaches an OPERATOR HOLD.** `status == "blocked"` carries two
        meanings and they must not be reversible by the same route: a
        `task_fatal` quarantine is the loop's own record of a failure, while a
        hold placed through the inbox is a human saying "not this one, not now"
        and has no blocker record at all. The same test `blocker_derived_blocked`
        and `_cmd_unblock` already use (`hold_origin != HOLD_ORIGIN_OPERATOR`)
        keeps this from becoming a way for the reviewer to launder that hold.

        Terminal statuses are refused by `_return_to_pending`'s own reading, so
        a completed, retired or shipped-elsewhere task cannot be un-finished
        here any more than it can by `release`.

        The COUNT is charged here rather than by the caller, so it is written in
        the same object the status move writes and reaches disk in the caller's
        single `persist()`. A recut that moved the status and then failed to
        record the cut would hand the task its allowance back.
        """
        obstacle = self.recut_obstacle(task_id)
        if obstacle is not None:
            raise obstacle
        moved = self._return_to_pending(
            task_id,
            verb="recut",
            remedy=(
                "discards an in-progress or quarantined round and cuts the task "
                "again from the base"
            ),
            by_stored_status=True,
            also_accepts=("blocked",),
        )
        # Released is released, exactly as `unblock` treats it: a pending task
        # has no hold, and a marker left behind here would still be sitting on
        # the row the next time the loop quarantines it.
        moved.blocked_reason = ""
        moved.hold_origin = ""
        moved.recut_count += 1
        return moved

    def _return_to_pending(
        self,
        task_id: str,
        *,
        verb: str,
        remedy: str,
        by_stored_status: bool = False,
        also_accepts: tuple[str, ...] = (),
    ) -> Task:
        """The one status move behind `release`, `shelve` and `recut`, and the
        one refusal. All three verbs mean "this round is over for now", and
        there is exactly one way to be pending, so letting them keep three
        copies of this assignment would be a bug in whichever one drifted.

        Since ceil-01 it also drops a pending attempt-ceiling classification
        request, for the same reason and with the same argument against three
        copies — see the comment on that line, which is where the reasoning
        for each of the three verbs is written down.

        `by_stored_status` is the first documented difference between them —
        see `shelve`, which explains why it reads `task.status` directly and
        why `release` must keep asking `state_of`. Default False, so a future
        caller that does not think about it gets `release`'s stricter reading.

        `also_accepts` is the second, and it is only ever `("blocked",)`, only
        ever from `recut` — see that method for why a quarantined task is
        exactly the one a recut has to be able to reach, and for the operator-
        hold refusal that runs BEFORE this and is not repeated here. Default
        empty, so a caller that does not think about it accepts `in_progress`
        alone. It is deliberately a status LIST rather than a boolean: the
        refusal below prints what the verb does accept, and a caller that widens
        the set without widening the message produces a refusal that lies.
        """
        if also_accepts and not by_stored_status:  # pragma: no cover - caller bug
            # The `state_of` reading below cannot express "or blocked" — it
            # answers BLOCKED for several unrelated shapes — so a caller that
            # widened the set without also asking for the stored reading would
            # have its widening SILENTLY IGNORED. Refuse the combination rather
            # than accept a call that does not do what it says.
            raise ValueError(
                "_return_to_pending: `also_accepts` requires `by_stored_status`"
            )
        task = self.get(task_id)
        accepted = ("in_progress",) + tuple(also_accepts)
        movable = (
            task.status in accepted
            if by_stored_status
            else self.state_of(task_id) is TaskState.IN_PROGRESS
        )
        if not movable:
            raise TaskGraphError(
                "task_not_in_progress",
                f"task '{task_id}' is not in progress (status {task.status!r}) — "
                f"`{verb}` only {remedy}; use "
                "`unblock` for a quarantined task",
            )
        task.status = "pending"
        # A pending task has no round to be at the ceiling OF, so it carries no
        # unanswered classification request either (ceil-01). Cleared for all
        # three verbs here, because all three end the round the request was
        # asked about, and a marker left behind is a REFUND in the two that
        # archive the execution record: the next dispatch starts at attempt 0,
        # where an identical plan would park the task `ceiling_plan_unchanged`
        # and a differing one would spend its single extension on a budget
        # nothing had spent. `shelve` keeps the record, so clearing it there
        # means the next dispatch ASKS AFRESH against the same candidate
        # instead of parking `ceiling_plan_unanswered` — an operator who sets a
        # stalled round aside has answered nothing, and the "never ask twice"
        # bound is per round, not per task lifetime.
        #
        # AFTER the status move, so a refused verb leaves the marker exactly
        # where it found it: the refusal above raises rather than returning,
        # and a cleared-but-unpersisted marker on a task that did not move is
        # the same stale record read the other way round.
        #
        # `unblock` deliberately does NOT clear it. That is the one route back
        # to pending that ends no round — see
        # `orchestrator._park_ceiling_plan_unanswered`, which tells an operator
        # in as many words that answering the blocker does not re-ask the
        # reviewer.
        task.ceiling_plan_requested_at = ""
        return task

    # ---- lookup -------------------------------------------------------------

    def get(self, task_id: str) -> Task:
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskGraphError("task_unknown", f"no task with id '{task_id}'")
        return task

    def has(self, task_id: str) -> bool:
        return task_id in self._tasks

    def all_tasks(self) -> list[Task]:
        return list(self._tasks.values())  # insertion order

    def state_of(self, task_id: str) -> TaskState:
        task = self.get(task_id)
        if task.status == "completed":
            return TaskState.COMPLETED
        if task.status == "blocked":
            return TaskState.BLOCKED_BY_OPERATOR
        # Before the dependency check, exactly like `blocked` above: a retired
        # task usually still declares the dependencies it was planned with, and
        # reading it as BLOCKED would put it back under "waiting on brw-01" —
        # the flat-status confusion this state exists to end, rebuilt one row
        # further down.
        if task.status == "retired":
            return TaskState.RETIRED
        # Before the dependency check for the same reason as the three above,
        # and with an extra one of its own: this row is a RECORD, so reading it
        # as BLOCKED would put a task whose work is demonstrably in the base
        # back under "waiting on inbox-02" — and it would then be counted as an
        # unsatisfied dependency by nothing, since `SATISFIES_DEPENDENCY` below
        # already answers for the dependents.
        if task.status == "shipped_elsewhere":
            return TaskState.SHIPPED_ELSEWHERE
        # `SATISFIES_DEPENDENCY`, never a hand-written `!= "completed"`. That
        # test used to be spelled out in four places; the constant is what keeps
        # the Roadmap panel, the dependency graph and this method from
        # disagreeing about whether a dependency is done. See the constant.
        if any(
            self._tasks[dep].status not in SATISFIES_DEPENDENCY
            for dep in task.depends_on
        ):
            return TaskState.BLOCKED
        if task.status == "in_progress":
            return TaskState.IN_PROGRESS
        return TaskState.READY

    def ready_tasks(self) -> list[Task]:
        return [t for t in self._tasks.values() if self.state_of(t.id) is TaskState.READY]

    def stranded_dependents(self, task_id: str) -> StrandReport:
        """Who would wait on `task_id` forever if it reached a terminal
        non-completed status right now (see `StrandReport`).

        THE shared precondition, and it is shared on purpose: the rule belongs
        to `state_of`'s dependency test — `!= "completed"` is satisfied by
        nothing else, so a dependency on a task that ended any other way is
        unsatisfiable and there is no command that clears it (`answer` needs an
        open blocker, `release` needs an in-progress task, `retire` means never
        again, and there is no `unblock`). `retire` is the only transition that
        reaches such a status today; a second one calls this rather than
        restating the rule, which is how the two cannot drift.

        Reads the STORED `status`, and never calls `state_of`, for the reason
        `blocker_derived_blocked` gives: `state_of` raises `KeyError` on a graph
        whose `depends_on` names a task that no longer exists — a shape
        `from_dict` deliberately tolerates — and a guard that either crashes or
        gets wrapped in a `try: … except: continue` on the graph it is meant to
        judge is a guard that fails OPEN on exactly the malformed input it
        should refuse. Nothing here indexes `self._tasks[dep]`; the edges are
        walked backwards, by asking each task whether it NAMES the id, so a
        dangling dependency is simply an edge to nowhere.

        Raises `task_unknown` for an id that is not in the graph, so a caller
        cannot ask about a typo and read the empty answer as "safe".
        """
        self.get(task_id)
        direct: list[str] = []
        transitive: list[str] = []
        seen: set[str] = set()
        frontier = [task_id]
        while frontier:
            current = frontier.pop(0)
            for candidate in self._tasks.values():
                if current not in candidate.depends_on:
                    continue
                # A record cannot be stranded, and its own dependents are not
                # stranded THROUGH it: a completed dependency is satisfied, and
                # a retired one is already unsatisfiable on its own account
                # rather than by anything this operation does.
                if candidate.status in _TERMINAL_STATUSES or candidate.id in seen:
                    continue
                # The subject is never its own stranded dependent. It is the
                # task GOING terminal, so it is a record afterwards, not queue,
                # and reporting it as a dependent of itself would then have the
                # rewrite edit the very row being retired.
                #
                # DEFENCE IN DEPTH, not a reachable shape — say so plainly,
                # because the reverse claim is easy to write and wrong. Every
                # construction route already refuses a self-edge:
                # `_validate_depends_on` at `add_many` and `set_depends_on`,
                # and `_check_acyclic` inside `from_dict`, which cycle-checks
                # the WHOLE stored graph on load (a self-edge is a one-node
                # cycle). So a hand-edited `tasks.json` naming a task after
                # itself does not reach this method — it fails to LOAD. What
                # this guard covers is an in-memory corruption, and the day
                # `from_dict` is relaxed to tolerate a stored cycle the way it
                # already tolerates a dangling `depends_on`.
                if candidate.id == task_id:
                    continue
                seen.add(candidate.id)
                transitive.append(candidate.id)
                if current == task_id:
                    # The frontier starts as exactly `[task_id]`, so every
                    # direct dependent is found on the first pass — before any
                    # transitive one can claim its place in `seen`.
                    direct.append(candidate.id)
                frontier.append(candidate.id)
        return StrandReport(
            direct=tuple(direct),
            transitive=tuple(transitive),
            in_progress=tuple(d for d in direct if self._tasks[d].status == "in_progress"),
        )

    def blocker_derived_blocked(self) -> list[Task]:
        """Every task whose `blocked` status MIRRORS a `blockers.Blocker`
        record — i.e. every quarantine the LOOP raised for itself, and nothing
        an operator placed by hand.

        The candidate set for `cli._reconcile_unblocked_tasks` (blk-01), which
        returns such a task to the queue once no OPEN blocker names it. Both
        halves of that state are supposed to move together: a quarantine lives
        in `tasks.json`, the question that caused it lives in its own record
        under `blockers/`, and a task left `blocked` with every record closed is
        excluded from `next_ready()` with nothing left to justify it. Same
        argument as `cli._reconcile_retired_blockers`, run the other way.

        An OPERATOR HOLD is excluded, and that is the point of reading
        `hold_origin` rather than the status alone: `operator_block` places a
        hold that has NO blocker record at all (see `HOLD_ORIGIN_OPERATOR`), so
        "no open blocker names it" is true of every hold from the instant it is
        placed. A sweep that ignored provenance would release the operator's
        quarantine on its next pass — the inbox's own reverse is deliberately
        narrowed the same way, and this one has no operator behind it at all.

        Reads the STORED `status`, never `state_of()`. Two reasons, and the
        second is the sharp one: `state_of` maps `blocked` to
        `BLOCKED_BY_OPERATOR` regardless of provenance (so it cannot answer the
        question this method exists for), and it raises `KeyError` on a graph
        whose `depends_on` names a task that no longer exists — a shape
        `from_dict` deliberately tolerates, and one a reconciliation sweep must
        not crash the loop over.
        """
        return [
            task
            for task in self._tasks.values()
            if task.status == "blocked" and task.hold_origin != HOLD_ORIGIN_OPERATOR
        ]

    def in_progress_tasks(self) -> list[Task]:
        """Every task whose STORED status is `in_progress` — the candidate set
        for the strand sweep (`orchestrator.Orchestrator._reconcile_stranded_
        tasks`, strand-01).

        The sibling of `blocker_derived_blocked` above, one status over, and it
        reads the stored string for the SAME two reasons. `state_of` cannot
        answer this question at all: it reports `BLOCKED` for an in-progress
        task whose dependency is incomplete (the dependency test runs BEFORE the
        `in_progress` branch — see `state_of`), so a sweep asking it would fall
        silent on exactly the row hardest to move, the way `_refuse_immutable`
        and `_displaced_work_exists` both document. And it raises `KeyError` on
        a graph whose `depends_on` names a task that no longer exists — a shape
        `from_dict` deliberately tolerates, and one a reconciliation sweep must
        not crash the loop over.

        Reports; decides nothing. Whether a given in-progress task is STRANDED
        (its round ended in an environment fault and the loop moved on) is a
        question about its execution record, which this module has no awareness
        of by design — see `health.stranded_fault_rounds`, which owns it.
        """
        return [task for task in self._tasks.values() if task.status == "in_progress"]

    def set_priority(self, task_id: str, priority: int) -> Task:
        """Re-prioritise an existing task.

        The one mutation an operator can make from outside the normal
        `plan`/`seed_tasks.json` route without going through the inbox at all:
        since 2026-08-16 the dashboard applies it IMMEDIATELY, through
        `TaskStore.apply_priority` (`inbox.KIND_PRIORITY` still exists and is
        still drained, for anything that queues one by hand). Priority changes
        what runs next; it cannot change what a task is allowed to touch, so it
        is safe to expose on a form — and safe to change mid-flight — in a way
        `approved_paths` would not be.

        Every call is remembered in `_priority_overrides` so that
        `TaskStore.save`'s reconciliation does not read the disk value back
        over a change the caller just made deliberately.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskGraphError("unknown_task", f"no task with id '{task_id}'")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TaskGraphError(
                "bad_priority", f"priority must be an integer, got {priority!r}"
            )
        task.priority = priority
        self._priority_overrides.add(task_id)
        return task

    def _refuse_immutable(self, task: Task, field: str) -> None:
        """Raise unless `task`'s content fields may still be rewritten.

        THE strand guard, shared by `set_description`, `set_approved_paths` and
        `set_depends_on` so the three cannot disagree about when an edit is
        safe. `set_priority` deliberately does NOT call it: priority only
        orders `next_ready()`, so changing it on a running or finished task is
        meaningless rather than damaging, and narrowing it now would break the
        one mutation the dashboard already queues.

        Why `in_progress` is the sharp one — each field is being judged against
        RIGHT NOW by a dispatch that has already started, and in every case the
        task ends up somewhere no command can move it from:

          * `depends_on` — `state_of` checks dependencies BEFORE the
            in_progress branch, so a new incomplete dependency reports the task
            as BLOCKED. `mark_completed` then refuses it ("cannot complete
            while dependencies are incomplete") and `release` refuses it too
            (it demands `state_of(...) is IN_PROGRESS`). The round finishes,
            the work is pushed, and the task can be neither completed nor
            returned to pending.
          * `approved_paths` — the dispatch was authorized against the OLD
            scope, and the post-commit ownership check runs against the new
            one. The agent's legitimate writes read as unauthorized paths and
            the task parks `task_fatal`.
          * `description` — the instructions the agent is working from were
            sent at dispatch. Rewriting them mid-round cannot reach the agent,
            and leaves the record disagreeing with what was actually asked, so
            the review packet describes work nobody requested.

        Deliberately checked on the STORED `status`, never on `state_of()`.
        `state_of` reports BLOCKED for an in-progress task with an incomplete
        dependency (the check runs first, see above), so a `state_of`-based
        guard would fall silent on precisely the task that is already in the
        stranded shape.
        """
        if task.status in _MUTABLE_STATUSES:
            return
        if task.status == "in_progress":
            raise TaskGraphError(
                "task_in_progress",
                f"task '{task.id}' is in progress — its {field} is what the running "
                "dispatch is being judged against, and changing it now would strand "
                "the round (neither completable nor releasable). Wait for it to "
                "finish, or `python -m autoloop release` it first",
            )
        if task.status == "completed":
            raise TaskGraphError(
                "task_completed",
                f"task '{task.id}' is already completed — rewriting its {field} "
                "edits the record of work that already shipped; plan a new task",
            )
        if task.status == "retired":
            raise TaskGraphError(
                "task_retired",
                f"task '{task.id}' is retired{_successor_hint(task)} — a retirement "
                f"is history and its {field} is never rewritten; plan a new task",
            )
        if task.status == "shipped_elsewhere":
            # A record, exactly like the two above. Rewriting the scope or the
            # description of work that is already in the base edits history, and
            # rewriting `depends_on` would change what a row nothing dispatches
            # claims to have waited for. The pragma that used to sit on the
            # fall-through below is gone with this arm: that branch was
            # unreachable only while `completed`, `retired` and `in_progress`
            # were the whole of the non-mutable set, and adding a status without
            # naming it here is precisely how it becomes reachable.
            raise TaskGraphError(
                "task_shipped_elsewhere",
                f"task '{task.id}' is recorded as shipped elsewhere"
                f"{_shipped_hint(task)} — its work is already in the base and its "
                f"{field} is never rewritten; plan a new task",
            )
        raise TaskGraphError(  # pragma: no cover - defensive; every status has an arm
            "task_not_mutable",
            f"task '{task.id}' has status {task.status!r}, which cannot be edited",
        )

    def set_description(self, task_id: str, description: str) -> Task:
        """Rewrite an existing task's description.

        Reachable from the inbox since 2026-08-16 as `kind: "description"`.
        The docstring here used to say the opposite — "deliberately with NO
        operator route to it" — and named the decision that would have to be
        revisited before one existed. This is that decision, so it is recorded
        rather than quietly reversed: a description IS agent instructions, and
        what makes exposing it acceptable is not that it stopped being
        authorization surface but that `_refuse_immutable` keeps it out of
        reach of any task a dispatch is currently reading. See
        `inbox.KIND_DESCRIPTION` and `docs/SECURITY.md` S28.

        Validation is `_validate_description`, the SAME function creation goes
        through, so a description the registry would refuse to create a task
        with cannot be written onto an existing one either.

        Rejection is atomic: the lookup, the strand guard and the validation
        all run before the single assignment, so a refused call leaves the
        registry exactly as it was rather than half-mutated. An unknown id
        raises `task_unknown` via `get`, like every other mutator here —
        `set_priority`'s hand-rolled `unknown_task` is the outlier, and it is
        left alone because that code string reaches the operator through the
        inbox's refusal text.
        """
        task = self.get(task_id)
        self._refuse_immutable(task, "description")
        _validate_description(task_id, description)
        task.description = description
        return task

    def set_decomposition(self, task_id: str, decomposition: str) -> Task:
        """Record the plan a reviewer approved for this task.

        Called from the dispatch path (`orchestrator._dispatch_executor`) with
        `contract.Decomposition.render()`'s text, for an `implement` that
        carries one and for a `revise` that reshapes one. There is deliberately
        no operator route and no inbox kind: this field is the record of a
        REVIEWER's approval, and a second author for it would make "approved"
        mean two different things.

        REPLACES, like `set_approved_paths`. A reshape has to be able to remove
        a step, and a merging setter could only ever add.

        **Accepts `in_progress`, where `_refuse_immutable` refuses
        `description`.** Not an oversight in the strand guard, and the
        difference is the timing rather than the field: `description` is
        rewritten by an OPERATOR at an arbitrary moment, so it can land in the
        middle of a dispatch that is already being judged against it. This is
        written by the dispatch itself, before that round's executor starts,
        and is then read by the prompt built after it — a revise round
        reshaping the plan is exactly the moment the reviewer is entitled to,
        and refusing it would leave the only route to a reshaped plan being a
        task nobody can revise. Nothing downstream judges the commit against
        this field (`approved_paths` is what bounds the writes), so a change
        here cannot strand a round the way the three guarded fields can.

        Terminal records are still refused: rewriting the plan of work that
        already shipped, or of a retirement, edits history rather than steering
        the queue — the same reasoning `_refuse_immutable` applies to
        `completed` and `retired`.

        Blank is refused rather than treated as "clear it". An empty
        decomposition is the state that means "no plan approved yet", and
        arriving there by writing one would let a reshape silently un-approve
        the task.
        """
        task = self.get(task_id)
        if task.status in _TERMINAL_STATUSES:
            # Driven off `_TERMINAL_STATUSES` rather than a literal pair, so a
            # status added to that set cannot fall through this guard and let a
            # dispatch rewrite the approved plan of finished work. That is how
            # the pair `("completed", "retired")` would have failed the day
            # `shipped_elsewhere` arrived.
            code = {
                "completed": "task_completed",
                "retired": "task_retired",
            }.get(task.status, "task_shipped_elsewhere")
            hint = _successor_hint(task) or _shipped_hint(task)
            raise TaskGraphError(
                code,
                f"task '{task_id}' is {task.status}{hint} — its "
                "decomposition is the record of what was approved and is not "
                "rewritten; plan a new task",
            )
        if not isinstance(decomposition, str) or not decomposition.strip():
            raise TaskGraphError(
                "empty_task_field",
                f"task '{task_id}' needs a non-empty decomposition — an empty one "
                "means no plan has been approved",
            )
        task.decomposition = decomposition
        return task

    # ---- the attempt ceiling's own three writes (ceil-01) --------------------
    #
    # Written by the DISPATCH path, like `set_decomposition` directly above and
    # for the same reason: they record what a reviewer decided about a task that
    # is mid-round, at the moment the round decides it. So they follow that
    # method's guard — terminal records refused, `in_progress` accepted — rather
    # than `_refuse_immutable`'s, which exists for operator edits that can land
    # at an arbitrary moment and strand a dispatch already being judged against
    # the field. There is deliberately no operator route and no inbox kind to any
    # of them: an operator who wants to hand a task more attempts already has
    # one, `python -m autoloop answer` on the blocker, and a second author for a
    # budget would make "granted" mean two different things.
    #
    # The CAPS these counters are read against live in `orchestrator`
    # (`MAX_CEILING_EXTENSIONS`, `MAX_SPLIT_DEPTH`), never here — the same
    # arrangement `recut_count`/`MAX_TASK_RECUTS` uses, and for the same reason:
    # the registry owns the durable count, the dispatch owns the policy, and
    # `tasks` importing `orchestrator` would be a cycle.

    def request_ceiling_plan(self, task_id: str, now: str) -> Task:
        """Record that this task has ASKED the reviewer to classify it at the
        attempt ceiling.

        Idempotent in the only sense that matters: an already-waiting task keeps
        its ORIGINAL timestamp. The field answers "has this task already asked?",
        and refreshing it on a second ask would erase the very fact that stops a
        ceiling hit re-asking forever (`Task.ceiling_plan_requested_at`).

        A blank timestamp is refused rather than stored: `""` is the value that
        means "not waiting", so arriving there by writing one would silently
        un-ask the question.
        """
        task = self.get(task_id)
        self._refuse_terminal_ceiling_write(task, "attempt-ceiling plan request")
        if not isinstance(now, str) or not now.strip():
            raise TaskGraphError(
                "empty_task_field",
                f"task '{task_id}' needs a non-empty timestamp for its "
                "attempt-ceiling plan request — an empty one means it never asked",
            )
        if not task.ceiling_plan_requested_at:
            task.ceiling_plan_requested_at = now
        return task

    def grant_attempt_extension(self, task_id: str) -> Task:
        """Extend this task's attempt budget by one grant, and clear the request
        that asked for it — in ONE call, so the two can never disagree.

        Separating them is the failure worth naming: a grant that left the
        request standing would park the task as unanswered on its very next
        dispatch, and a cleared request with no grant would send it straight back
        into the ceiling it just classified. Both halves here or neither.

        The CAP is the caller's (`orchestrator.MAX_CEILING_EXTENSIONS`); this
        method counts. It refuses to grant to a task that never asked, which is
        what keeps an ordinary mid-task `decomposition` reshape from quietly
        buying a wider ceiling.
        """
        task = self.get(task_id)
        self._refuse_terminal_ceiling_write(task, "attempt-budget extension")
        if not task.ceiling_plan_requested_at:
            raise TaskGraphError(
                "ceiling_plan_not_requested",
                f"task '{task_id}' did not ask for a classification at the "
                "attempt ceiling, so there is no request for an extension to "
                "answer — an ordinary plan reshape does not widen the ceiling",
            )
        task.attempt_extensions += 1
        task.ceiling_plan_requested_at = ""
        return task

    def clear_ceiling_plan_request(self, task_id: str) -> Task:
        """Drop a pending attempt-ceiling request WITHOUT granting anything.

        The decomposition half's counterpart to `grant_attempt_extension`: a
        reviewer that answers the request by splitting the task has answered it,
        and the parent is about to be retired into its children — but a retired
        row still carrying the marker would be picked up as the pending parent of
        the NEXT plan (`ceiling_plan_pending` filters terminal rows for the same
        reason, so this is the belt to that braces).

        Never used to "expire" a request. There is no timeout here: the request
        is answered, or the task parks on its next dispatch.
        """
        task = self.get(task_id)
        self._refuse_terminal_ceiling_write(task, "attempt-ceiling plan request")
        task.ceiling_plan_requested_at = ""
        return task

    def ceiling_plan_pending(self) -> list[Task]:
        """Every LIVE task waiting on an attempt-ceiling classification.

        Terminal rows are filtered out, not merely expected to be absent: a
        retired parent whose marker survived would otherwise be offered as the
        parent of an unrelated later `plan`, which is a split applied to the
        wrong task.
        """
        return [
            task
            for task in self._tasks.values()
            if task.ceiling_plan_requested_at and task.status not in _TERMINAL_STATUSES
        ]

    def _refuse_terminal_ceiling_write(self, task: Task, what: str) -> None:
        """Terminal records take none of the three writes above.

        Driven off `_TERMINAL_STATUSES` rather than a literal tuple, for the
        reason `set_decomposition` spells out: a status added to that set must
        not fall through and let a dispatch rewrite the budget of finished work.
        """
        if task.status not in _TERMINAL_STATUSES:
            return
        code = {
            "completed": "task_completed",
            "retired": "task_retired",
        }.get(task.status, "task_shipped_elsewhere")
        hint = _successor_hint(task) or _shipped_hint(task)
        raise TaskGraphError(
            code,
            f"task '{task.id}' is {task.status}{hint} — a {what} cannot be "
            "recorded against work that is already finished; plan a new task",
        )

    def set_approved_paths(self, task_id: str, paths) -> Task:
        """Replace an existing task's authorization scope.

        REPLACES, never merges. An operator correcting a scope has to be able
        to take a path away, and a merging mutator can only ever widen — which
        would make this a one-way ratchet on the field that decides what an
        agent may write.

        Clearing it to `()` is legal and means "no scope authorized": that is
        the state `effective_approved_paths` keeps empty and
        `_dispatch_task_postcommit` refuses to dispatch, so revoking a scope
        parks the task rather than silently granting the trackers. Creation
        goes the other way (`dashboard._submit_task` refuses an empty list)
        because a NEW task with no scope is an undispatchable trap, while an
        EXISTING one being un-authorized is the whole point.

        Validation is `_validate_approved_paths`, the same function
        `add_many` calls, so a scope the registry would refuse to create a task
        with cannot be written onto an existing one — including the duplicate
        rule, which is why that check was extracted rather than reused by eye.

        Atomic: shape, every path and the duplicate rule are all checked before
        the single assignment.
        """
        task = self.get(task_id)
        self._refuse_immutable(task, "approved_paths")
        task.approved_paths = _validate_approved_paths(task_id, paths)
        return task

    def set_depends_on(self, task_id: str, depends_on) -> Task:
        """Replace an existing task's dependencies.

        REPLACES, for the same reason `set_approved_paths` does: an operator
        who cannot remove a dependency cannot correct a mistaken one, and a
        task waiting on a dependency that will never complete is stuck forever
        (`state_of` only counts `completed` as satisfied — see `retire`).

        Since retire-01 a NEW retirement can no longer create that shape:
        `retire` refuses one that would strand a dependent, and rewrites the
        dependents itself when the operator asks it to. This stays the route
        for a strand an EARLIER retirement already left behind — a repeat
        `retire` cannot rewrite anything, because it cannot rewrite the
        retirement either.

        Validation is `_validate_depends_on` (shape, known ids, no self-edge)
        followed by `_check_acyclic`, which is what creation runs; the cycle
        check needs the whole graph, so it runs against a CANDIDATE copy
        carrying the new edges. That ordering is the atomicity guarantee: a
        refused call never touches `self._tasks`, so the registry cannot be
        left holding a cycle that has to be reverted.
        """
        task = self.get(task_id)
        self._refuse_immutable(task, "depends_on")
        deps = _validate_depends_on(task_id, depends_on, self._tasks)
        candidate = dict(self._tasks)
        candidate[task_id] = replace(task, depends_on=deps)
        _check_acyclic(candidate)
        task.depends_on = deps
        return task

    def operator_block(self, task_id: str, reason: str) -> Task:
        """Hold a task out of the queue at an operator's request.

        NOT a second `block`. `block` records a `task_fatal` park and its only
        caller (`cli._handle_parked_task`) blocks a task that is by definition
        `in_progress` — it was dispatched, it parked, nothing cleared the
        status. Putting the strand guard inside `block` would therefore turn
        every park into a `loop_fatal` escalation, because that caller
        fail-closes on any refusal. So the guard lives here, in the
        operator-facing entry point, and `block` stays exactly as it was.

        Three refusals, all before the delegate writes anything:

          * `in_progress` — quarantining a running round strands it. The round
            finishes and pushes, then `mark_completed` refuses
            (`task_blocked_by_operator`), which is the B10 failure deliberately
            rather than accidentally.
          * already `blocked` — `block` is idempotent and REFRESHES the reason,
            which is right for a park that re-fires and wrong here: it would
            overwrite the recorded account of a real quarantine with an
            operator's note AND give it an operator `hold_origin`, i.e. convert
            a quarantine into something the inbox may release.
          * `completed` / `retired` — delegated to `block`, which already
            refuses both.

        Provenance is recorded as `hold_origin = HOLD_ORIGIN_OPERATOR`, which is
        the ONLY thing `operator_unblock` reads, and this is the only method
        that ever writes it. The reason also gets `OPERATOR_HOLD_PREFIX` in
        front of it, but that is prose for whoever reads the row — it decides
        nothing, deliberately, because `blocked_reason` is free text the loop
        writes too.

        Written AFTER the delegate returns, so a hold `block` refuses
        (completed / retired) leaves no marker behind on a task that was never
        held.
        """
        task = self.get(task_id)
        if not isinstance(reason, str) or not reason.strip():
            raise TaskGraphError(
                "empty_task_field",
                f"holding task '{task_id}' needs a non-empty reason — a hold with no "
                "account of why is the free-text blocker problem, not a fix for it",
            )
        if task.status == "in_progress":
            raise TaskGraphError(
                "task_in_progress",
                f"task '{task_id}' is in progress — holding it now would strand the "
                "round, which would finish and then be refused completion. Wait for "
                "it, or `python -m autoloop release` it first",
            )
        if task.status == "blocked":
            raise TaskGraphError(
                "task_blocked_by_operator",
                f"task '{task_id}' is already blocked and its reason is on record "
                f"({task.blocked_reason or '(none recorded)'}) — an operator hold "
                "must not overwrite it",
            )
        held = self.block(task_id, OPERATOR_HOLD_PREFIX + reason)
        held.hold_origin = HOLD_ORIGIN_OPERATOR
        return held

    def operator_unblock(self, task_id: str) -> Task:
        """Reverse of `operator_block`, and ONLY of that.

        The reason this pair exists rather than a bare `block` request kind: a
        hold placed through the inbox creates no `blockers.Blocker` record, and
        `python -m autoloop answer` — the only route out of `blocked` — takes a
        blocker id and unblocks the task that blocker names. There is no
        standalone `unblock` command. So an inbox that could block and not
        unblock would write a state with no way back.

        Narrowed by provenance, not by trust: a task quarantined by the loop
        does not carry `hold_origin == HOLD_ORIGIN_OPERATOR`, and releasing it
        here would put it back in `ready_tasks()` with its blocker still open
        and unanswered. Those go through `answer`, which resolves both halves
        together.

        The gate is the FIELD and nothing else. It used to be
        `blocked_reason.startswith(OPERATOR_HOLD_PREFIX)`, which asked an
        unconstrained free-text field — one ordinary quarantines write too —
        to prove who wrote it, so a park detail beginning with those characters
        was releasable from here. Testing both the field and the prefix would
        keep that text load-bearing; testing only the field is what makes the
        answer authoritative.
        """
        task = self.get(task_id)
        if task.status == "blocked" and task.hold_origin != HOLD_ORIGIN_OPERATOR:
            raise TaskGraphError(
                "task_blocked_by_operator",
                f"task '{task_id}' was quarantined by the loop, not held by an "
                "operator — resolve its blocker with `python -m autoloop answer`, "
                "which records the answer and unblocks the task together",
            )
        return self.unblock(task_id)

    # ---- shipped under another task's commits -------------------------------

    def record_shipped_elsewhere(self, task_id: str, commits, note: str) -> Task:
        """Record that `task_id`'s work is in the base under OTHER commits.

        The registry half of ship-01. It decides ONE thing — may this task carry
        such a record, and is the record well-formed — and nothing about whether
        the evidence is TRUE right now: this module has no repository awareness
        by design (module docstring, `_validate_approved_path`), so ancestry is
        checked by the caller that has a checkout before the request is queued
        (`cli._cmd_record_shipped`) and re-checked on every read by the one that
        has one (`dashboard.shipped_elsewhere_states`). Same split as everywhere
        else here: the inbox owns a request's SHAPE, this owns its CONTENT, and
        the reader with git owns its TRUTH.

        **It stores evidence, not an assertion.** `commits` must be a non-empty
        list of full lowercase shas and `note` a non-blank account of whose they
        are — refused otherwise, because "trust me, this shipped" is precisely
        the hand-written prose this state replaces. What makes the record safe
        is not that it was hard to write but that anyone can re-run the check:
        the shas are on the row, and a sha that is no longer an ancestor of the
        base head makes the record read as a DISAGREEMENT rather than as done.

        **It satisfies dependents** (`SATISFIES_DEPENDENCY`) — the difference
        from `retire`, and the main reason the state exists. inbox-03 and
        inbox-04 both depend on inbox-02; recording inbox-02 as retired would
        leave them BLOCKED with no command able to release them.

        **Accepted from `pending` and from an OPERATOR HOLD**, the two states
        the five measured records were actually in (all five were parked
        through the inbox as a stopgap). A LOOP-RAISED quarantine is refused —
        see the `hold_origin` arm below, which is the same provenance test
        `operator_unblock` reads and exists for the same reason. Also refused
        from:

          * `in_progress` — a dispatch is running against this task right now,
            and flipping it terminal mid-round strands it exactly as
            `_refuse_immutable` describes: `mark_completed` and `release` would
            both then refuse it.
          * `completed` — this is the OTHER half of the disagreement, and it
            must not be laundered. bind-01, split-01 and dash-17 are completed
            with their work provably absent; converting them here would rewrite
            a wrong record into a differently-wrong one instead of showing it.
            A completed task whose work really did land under someone else's
            commits already reads as shipped on the report, and needs nothing.
          * `retired` — a supersession is written once and is history.

        **Re-recording is allowed**, unlike `retire`'s written-once rule, and
        the asymmetry is deliberate. A retirement records a DECISION, which
        cannot change without a new decision; this records an OBSERVATION about
        commits, and observations legitimately move — a rebase renames every
        carrying sha, and the record must be able to follow rather than sit
        there permanently disagreeing. The safety that buys it is the same
        continuous re-check: a rewrite pointing at commits that are not
        ancestors is visible as a disagreement the moment it is made, so a wrong
        rewrite cannot hide. An identical re-record is a NO-OP that keeps the
        original `shipped_at`, so a resubmitted request does not make the record
        say it was written later than it was.

        There is deliberately NO route back to `pending`. Un-recording is the
        claim that the evidence was wrong, which the disagreement report already
        surfaces for a human; a status flip that silently re-queued a task whose
        code is in the base would be the same one-way-door problem
        `block`/`unblock` are shaped to avoid, pointed the other way.

        Rejection is atomic: the lookup, the state refusals and both validators
        all run before the first assignment, so a refused call leaves the
        registry exactly as it was.
        """
        task = self.get(task_id)
        if task.status == "in_progress":
            raise TaskGraphError(
                "task_in_progress",
                f"task '{task_id}' is in progress — recording it as shipped "
                "elsewhere now would strand the round, which would finish and "
                "then be refused completion. Wait for it, or "
                "`python -m autoloop release` it first",
            )
        if task.status == "completed":
            raise TaskGraphError(
                "task_completed",
                f"task '{task_id}' is already completed — a completed record that "
                "the code cannot show is a DISAGREEMENT to look at, not a record "
                "to rewrite. `python -m autoloop shipped-report` names it",
            )
        if task.status == "retired":
            raise TaskGraphError(
                "task_retired",
                f"task '{task_id}' is retired{_successor_hint(task)} — a retirement "
                "is history and is never rewritten; plan a new task",
            )
        if task.status == "blocked" and task.hold_origin != HOLD_ORIGIN_OPERATOR:
            # A LOOP QUARANTINE, and the refusal is about the OTHER FILE. A
            # `task_fatal` park writes a `blockers.Blocker` record beside the
            # registry row, and that record is read INDEPENDENTLY of the
            # registry — by `start`'s preflight, by `health.check` and by the
            # heartbeat. Moving this row to a terminal status without closing it
            # is the split brain `cli._reconcile_retired_blockers` was written
            # for: the dashboard would say "already done, elsewhere" while the
            # loop stayed stopped waiting on exactly this task.
            #
            # Refused rather than reconciled here, for two reasons. This module
            # has no blocker store and must not grow one (it has no filesystem
            # awareness at all, by design), and closing an unanswered question
            # from a drain would forge the operator confirmation
            # `_RESOLUTION_PRECONDITIONS` demands. Both supported steps already
            # exist: `python -m autoloop answer` resolves the blocker AND
            # returns the task to pending, and this command then records it.
            #
            # An OPERATOR HOLD is accepted, and the asymmetry is exactly the one
            # `operator_unblock` turns on: a hold placed through the inbox
            # creates no blocker record at all, so there is nothing to orphan.
            # Provenance is the stored field and never the reason text.
            raise TaskGraphError(
                "task_blocked_by_operator",
                f"task '{task_id}' was quarantined by the loop, not held by an "
                "operator, so a `blockers.Blocker` record is open against it and "
                "would be orphaned by this. Resolve it with `python -m autoloop "
                "answer` — which closes the blocker and returns the task to "
                "pending — then record it",
            )
        recorded = _validate_shipped_commits(task_id, commits)
        _validate_shipped_note(task_id, note)
        if (
            task.status == "shipped_elsewhere"
            and task.shipped_commits == recorded
            and task.shipped_note == note
        ):
            return task
        task.status = "shipped_elsewhere"
        task.shipped_commits = recorded
        task.shipped_note = note
        task.shipped_at = utcnow_iso()
        # `blocked_reason` is PRESERVED — it is the account of why the task was
        # parked, which is history in the same sense a retirement's reason is,
        # and five of these rows carry the stopgap note explaining the park.
        # `hold_origin` is CLEARED, for the reason `unblock` clears it: the task
        # is no longer blocked, so there is no hold to have an origin, and a
        # marker left behind would still be sitting on the row deciding who may
        # release a quarantine that no longer exists.
        task.hold_origin = ""
        return task

    # ---- the urgent pin (preemption) ----------------------------------------

    def live_urgent_target(self) -> Task | None:
        """The one task carrying a pin that still has a dispatch coming, or None.

        LIVE means `urgent_at` is set AND the task is still READY — i.e. the
        dispatch the pin asks for has not happened and is not impossible. Every
        other pinned state is STALE and answers None here, which is what keeps
        the single slot from wedging: a pinned task that was later blocked by a
        new dependency, quarantined, retired or completed would otherwise hold
        the slot for good, and every later urgent request would be refused
        forever on behalf of a preemption that can never occur. `request_urgent`
        clears such a marker when it grants the slot to someone else.

        `state_of` is called inside a `try`, unlike the straight-line use in
        `ready_tasks`: it raises `KeyError` on a graph whose `depends_on` names
        a task that no longer exists (a shape `from_dict` deliberately
        tolerates — see `blocker_derived_blocked`), and this method is called
        from the loop's own hot path between steps. A graph it cannot judge has
        no live target rather than taking the run down.

        At most one task can be pinned at a time, so the first match is THE
        match; the loop over all tasks exists to find it, not to choose among
        several.
        """
        for task in self._tasks.values():
            if not task.urgent_at:
                continue
            try:
                state = self.state_of(task.id)
            except KeyError:  # pragma: no cover - dangling dependency graph
                continue
            if state is TaskState.READY:
                return task
        return None

    def _refuse_unurgentable(self, task: Task) -> None:
        """Raise unless `task` is in a state a preemption could actually
        dispatch it from.

        The BOUND on what an urgent request may name. Every arm names the state
        it found, because the failure this exists to prevent is a silent no-op:
        a request for a task with an unmet dependency that is accepted, never
        dispatched, and leaves the operator watching a loop that ignored them.

        `in_progress` gets its own sentence rather than falling into a generic
        "not ready": the request has already been granted in the only sense
        that matters, and an operator told "not ready" about the task the loop
        is running would resubmit.
        """
        state = self.state_of(task.id)
        if state is TaskState.IN_PROGRESS:
            raise TaskGraphError(
                "task_in_progress",
                f"task '{task.id}' is already the round in flight — there is "
                "nothing to preempt it with",
            )
        if state is TaskState.COMPLETED:
            raise TaskGraphError(
                "task_completed",
                f"task '{task.id}' is already completed — it will not be "
                "dispatched again",
            )
        if state is TaskState.SHIPPED_ELSEWHERE:
            # Without an arm this state falls through to the `approved_paths`
            # check below and the pin is ACCEPTED — the silent no-op accept this
            # method's own docstring names as the failure it exists to prevent,
            # on a task that can never be dispatched at all. The operator would
            # watch a loop that displaced nothing and ran something else.
            raise TaskGraphError(
                "task_shipped_elsewhere",
                f"task '{task.id}' is recorded as shipped elsewhere"
                f"{_shipped_hint(task)} — its work is already in the base, so "
                "preempting for it would displace a round for nothing",
            )
        if state is TaskState.BLOCKED:
            waiting = ", ".join(
                dep
                for dep in task.depends_on
                if self._tasks[dep].status not in SATISFIES_DEPENDENCY
            )
            raise TaskGraphError(
                "task_blocked",
                f"task '{task.id}' waits on incomplete dependencies ({waiting}) "
                "— preempting for it would displace a round for a task the loop "
                "still cannot dispatch",
            )
        if state is TaskState.BLOCKED_BY_OPERATOR:
            recorded = task.blocked_reason or "no reason recorded"
            raise TaskGraphError(
                "task_blocked_by_operator",
                f"task '{task.id}' is blocked ({recorded}) — release it first "
                "(`python -m autoloop answer`, or an `unblock` request for an "
                "operator hold)",
            )
        if state is TaskState.RETIRED:
            raise TaskGraphError(
                "task_retired",
                f"task '{task.id}' is retired{_successor_hint(task)} — plan a new "
                "task instead of preempting for one that will not be worked",
            )
        if not task.approved_paths:
            # `_dispatch_task_postcommit` refuses an unscoped implement/revise
            # (docs/SECURITY.md finding #2), so accepting this would displace a
            # round in order to dispatch a task that parks on arrival. Refused
            # here, where the operator can still fix it with an
            # `approved_paths` request, rather than three minutes later in a
            # blocker.
            raise TaskGraphError(
                "no_approved_paths",
                f"task '{task.id}' has no approved_paths, so no dispatch of it "
                "can start — send an `approved_paths` request first",
            )

    def request_urgent(self, task_id: str, reason: str) -> Task:
        """Make `task_id` the loop's urgent target: the next task dispatched,
        ahead of whatever is in flight.

        The registry half of preemption. It decides ONE thing — may this task
        hold the pin — and nothing about how the loop acts on it: the safe
        boundary, the release of the displaced round and the quarantine of its
        work all belong to `orchestrator._preempt_for_urgent`, which reads
        `live_urgent_target()`. Same split as everywhere else here: the inbox
        owns a request's SHAPE, this owns its CONTENT, the loop owns its
        TIMING.

        ONE AT A TIME, enforced structurally. A second request while another
        task's pin is still live is REFUSED, naming the incumbent — not queued
        behind it, and not silently overwriting it. Queueing would mean two
        preemptions in flight with one round to displace between them, which is
        exactly the shape the manual pause/resume sequence failed at
        (`docs/AUTOLOOP.md` §4f-quinquies); overwriting would let the second
        operator discard the first's displaced round without knowing they had.
        The refusal is loud and the operator can resubmit once the first target
        has been dispatched, which clears the pin (`mark_in_progress`).

        IDEMPOTENT for the SAME task: a repeat while the pin is already live
        returns it untouched, keeping the original `urgent_at` and the original
        reason. A second submission of the same request is almost always an
        operator who is not sure the first landed, and rewriting the timestamp
        would make the record say the preemption was asked for later than it
        was.

        Rejection is atomic. The lookup, the reason check, the incumbent check
        and every state refusal all run before the first assignment, so a
        refused call leaves the registry exactly as it was — including the
        incumbent's own pin, which is only cleared once this call is certain to
        grant the slot.
        """
        task = self.get(task_id)
        _validate_urgent_reason(task_id, reason)
        incumbent = self.live_urgent_target()
        if incumbent is not None and incumbent.id == task.id:
            return task
        if incumbent is not None:
            raise TaskGraphError(
                "urgent_already_pending",
                f"task '{incumbent.id}' is already the urgent target (requested "
                f"{incumbent.urgent_at}: {incumbent.urgent_reason}) and has not "
                f"been dispatched yet — one preemption at a time. Wait for it to "
                f"start, then request '{task_id}' again",
            )
        self._refuse_unurgentable(task)
        # Only now, with the grant certain: any marker still on another row is
        # STALE by definition (`live_urgent_target` just answered None), and
        # leaving it would put a second `urgent_at` in the file for a
        # preemption that can never happen.
        for other in self._tasks.values():
            if other.urgent_at:
                other.urgent_at = ""
                other.urgent_reason = ""
        task.urgent_at = utcnow_iso()
        task.urgent_reason = reason
        return task

    def next_ready(self) -> Task | None:
        """Highest-priority ready task; ties broken by id — unless one carries
        the URGENT PIN, which outranks both.

        Was insertion order. Ordering by `priority` first is what lets an
        operator steer a running loop — otherwise a task added later can
        never overtake one already queued, no matter how urgent.

        Priority alone still cannot PREEMPT, and that is why the pin sorts
        ahead of it rather than being expressed as a smaller number. Measured
        2026-08-21: codex-01 was raised to P0 while brw-13 held the loop, but
        brw-13 was already P0 — so P0 only TIED, the id tiebreak decided it
        ("brw-13" < "codex-01"), and the urgent task lost silently. A pin no
        other task can hold cannot tie.
        """
        ready = self.ready_tasks()
        if not ready:
            return None
        return sorted(ready, key=lambda t: (0 if t.urgent_at else 1, t.priority, t.id))[0]

    def summary(self) -> str:
        """One line of roadmap state, rendered into every review request.

        The READY count carries a priority-1 breakdown with it because
        `contract.AUDIT_VS_READY_PREFERENCE` tells the reviewer to implement a
        ready task instead of ordering a fresh audit, and to weigh how urgent
        the queue is. A rule that depends on a number the reviewer
        cannot see is not a rule — so the two are coupled the same way
        `context.IN_FLIGHT_LABEL` is, and pinned by test on both sides.

        Priority 1 specifically, not "the lowest priority present": P1 is the
        audit reports' own vocabulary for "this blocks other work", and the
        default is 100, so a roadmap nobody has prioritised reports 0 rather
        than reporting every task as urgent.
        """
        if not self._tasks:
            return "no tasks planned yet"
        counts = {state: 0 for state in TaskState}
        ready_priority_one = 0
        for task in self._tasks.values():
            state = self.state_of(task.id)
            counts[state] += 1
            if state is TaskState.READY and task.priority == 1:
                ready_priority_one += 1
        nxt = self.next_ready()
        parts = (
            f"{len(self._tasks)} tasks: {counts[TaskState.COMPLETED]} completed, "
            f"{counts[TaskState.IN_PROGRESS]} in progress, "
            f"{counts[TaskState.READY]} ready ({ready_priority_one} at priority 1), "
            f"{counts[TaskState.BLOCKED]} blocked, "
            f"{counts[TaskState.BLOCKED_BY_OPERATOR]} quarantined, "
            # Counted separately for the same reason the state exists: folded
            # into `quarantined` it reads as work waiting on the reviewer, and
            # `AUDIT_VS_READY_PREFERENCE` has them weighing exactly that.
            f"{counts[TaskState.RETIRED]} retired, "
            # And separately from `completed`, though both are done: this count
            # is work with no branch of its own, so a reviewer reading it as
            # completed would expect the merge sweep to have something to
            # integrate for it. `dashboard.roadmap_stats` copies this sentence
            # word for word and a test pins the two equal.
            f"{counts[TaskState.SHIPPED_ELSEWHERE]} shipped elsewhere"
        )
        if nxt is not None:
            parts += f"; next ready: {nxt.id} — {nxt.title}"
        urgent = self.live_urgent_target()
        if urgent is not None:
            # Rendered for the REVIEWER, not only for the operator: this line
            # is `context.build_context`'s `roadmap_status`, and a reviewer that
            # cannot see the pin has no way to know why the loop just ended a
            # round — or that naming any other task will be refused
            # (`orchestrator._dispatch_executor`). Same coupling as the
            # priority-1 breakdown above, and pinned by test on both sides.
            parts += (
                f"; URGENT: {urgent.id} was requested by the operator and must be "
                f"the next task dispatched ({urgent.urgent_reason})"
            )
        return parts

    # ---- persistence --------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema_version": TASKS_SCHEMA_VERSION,
            "tasks": [asdict(t) for t in self._tasks.values()],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRegistry":
        try:
            tasks = [
                Task(**{
                    **raw,
                    "depends_on": tuple(raw.get("depends_on", ())),
                    # JSON has no tuples: a round-trip yields lists of lists.
                    "validation": tuple(
                        tuple(c) for c in raw.get("validation", ())
                    ),
                    "approved_paths": tuple(raw.get("approved_paths", ())),
                    # Missing key -> `""`, which is how every `tasks.json`
                    # written before this field existed loads: as a loop
                    # quarantine, the safe reading (see `Task.hold_origin`).
                    # Coerced rather than validated, and compared EXACTLY
                    # everywhere: a hand-edited `null` must become `""` rather
                    # than `None` (which would blow up on the next `str`
                    # operation), and anything else that is not the literal
                    # marker simply is not one. Deliberately no strip/lower —
                    # normalising here would ACCEPT near-misses, and this field
                    # decides whether a quarantine can be released.
                    "hold_origin": str(raw.get("hold_origin", "") or ""),
                    # Same coercion, same reason: a missing key is a
                    # `tasks.json` written before this field existed and loads
                    # as "no plan approved yet", while a hand-edited `null`
                    # must become `""` rather than `None` (which would blow up
                    # on the next `strip`). See `Task.decomposition`.
                    "decomposition": str(raw.get("decomposition", "") or ""),
                    # Same coercion again, and the same reason: a missing key
                    # is a `tasks.json` written before the urgent pin existed
                    # and loads as "not urgent", while a hand-edited `null`
                    # must become `""` rather than `None` — every reader here
                    # tests this field for truthiness and then prints it.
                    "urgent_at": str(raw.get("urgent_at", "") or ""),
                    "urgent_reason": str(raw.get("urgent_reason", "") or ""),
                    # Same coercion, same reason as the four above: a missing
                    # key is a `tasks.json` written before ship-01 and loads as
                    # "nothing recorded", while a hand-edited `null` must become
                    # `""` rather than `None` — both are printed and stripped by
                    # their readers without a None check.
                    "shipped_note": str(raw.get("shipped_note", "") or ""),
                    "shipped_at": str(raw.get("shipped_at", "") or ""),
                    # VALIDATED, not just tuple()-converted, exactly like
                    # `superseded_by` below and for a sharper reason: this path
                    # never reaches `add_many`, so it is the only gate a stored
                    # or hand-edited row passes, and this is the field whose
                    # contents unblock this task's dependents. A bare string sha
                    # would otherwise load as 40 single-character "commits".
                    "shipped_commits": _persisted_shipped_commits(raw),
                    # VALIDATED for the same reason, and the sharpest of the
                    # three: this is the only bound on a destructive action the
                    # reviewer takes without an operator. See
                    # `_persisted_recut_count` — an unreadable value raises
                    # rather than defaulting to "no cuts spent yet".
                    "recut_count": _persisted_recut_count(raw),
                    # VALIDATED for the same reason as `recut_count` directly
                    # above, and each one bounds something: how far the attempt
                    # ceiling may be widened, how much of a parent's spend a
                    # subtask inherits, and how deep splitting may recurse. See
                    # `_persisted_nonneg_int` — an unreadable value raises
                    # rather than loading as "nothing spent yet".
                    "attempt_extensions": _persisted_nonneg_int(
                        raw, "attempt_extensions"
                    ),
                    "inherited_attempts": _persisted_nonneg_int(
                        raw, "inherited_attempts"
                    ),
                    "split_depth": _persisted_nonneg_int(raw, "split_depth"),
                    # Coerced, not validated, exactly like `urgent_at` above and
                    # for the same reason: a missing key is a `tasks.json`
                    # written before ceil-01 and loads as "not waiting on a
                    # classification", while a hand-edited `null` must become
                    # `""` rather than `None` — every reader tests this for
                    # truthiness and then prints it.
                    "ceiling_plan_requested_at": str(
                        raw.get("ceiling_plan_requested_at", "") or ""
                    ),
                    # VALIDATED, not just tuple()-converted — this path never
                    # reaches `add_many` (see the bypass below), so it is the
                    # only gate a stored row passes. See
                    # `_persisted_superseded_by`.
                    "superseded_by": _persisted_superseded_by(raw),
                })
                for raw in data["tasks"]
            ]
        except (KeyError, TypeError) as exc:
            raise StateCorruptError(f"task file has an unexpected shape: {exc}") from exc
        registry = cls()
        # Bypass add_many: stored tasks were validated on the way in, and
        # re-validation must not reject a completed graph (e.g. status fields).
        for task in tasks:
            if task.id in registry._tasks:
                raise StateCorruptError(f"task file contains duplicate id '{task.id}'")
            registry._tasks[task.id] = task
        _migrate_retirements(registry._tasks)
        _check_acyclic(registry._tasks)
        return registry


#: The retirements that predate `TaskState.RETIRED`: `{task_id: (markers,
#: successors)}`.
#:
#: A one-shot data migration living in code, because the data it migrates does
#: not live in this repository — `tasks.json` is loop state under `.autoloop/`,
#: outside the checkout, and there is no schema-migration runner for it. This
#: is the whole of the mechanism: `from_dict` applies it, so the very next
#: `TaskStore.save` persists the result and the dashboard (which builds a
#: registry from the same JSON without writing) shows it immediately.
#:
#: Each entry was read off that task's own `blocked_reason` as of 2026-08-14:
#:   brw-02, brw-04  superseded by brw-06
#:   brw-05          retired alongside brw-02/brw-04
#:   brw-06          split at the reviewer's request (blk-(loop)-018) into
#:                   brw-07 + brw-08
#:   sub-01          superseded by sub-02 and sub-03
#:   dash-01         went stale on 2026-08-03 — in_progress at dispatch with no
#:                   candidate and no execution record, so nothing will ever
#:                   finish it. No successor: it was abandoned, not replaced,
#:                   and inventing one would put a false chain in the record.
#: audit-0003 is deliberately ABSENT. It is the one genuine failure among the
#: seven, and it must keep asking for an operator.
#:
#: brw-05 records brw-02/brw-04 rather than brw-06 because that is what its own
#: reason says. The chain then stays traversable one hop at a time
#: (brw-05 → brw-02 → brw-06 → brw-07/brw-08) and no link is asserted that a
#: human did not write down.
#:
#: TWO guards, not one. A task is migrated only when its stored status is still
#: `blocked` AND one of its markers still appears in its `blocked_reason`
#: (case-insensitive). The status guard makes it idempotent — after the first
#: save nothing here matches again. The marker guard makes it self-limiting: if
#: brw-02 is ever revived and later quarantined for a real reason, the reason no
#: longer matches and this leaves it alone. A task whose reason was reworded
#: simply does not migrate and stays quarantined, which is the pre-existing
#: state rather than a wrong one — `python -m autoloop retire` is then the
#: manual route, and that is the ONLY intended fallback. Never widen this into
#: a general "parse the reason for a successor id" heuristic: the reasons are
#: free text, and a heuristic that misfires retires a task nobody retired.
_RETIREMENTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "brw-02": (("brw-06",), ("brw-06",)),
    "brw-04": (("brw-06",), ("brw-06",)),
    "brw-05": (("brw-02", "brw-04"), ("brw-02", "brw-04")),
    "brw-06": (("brw-07", "brw-08"), ("brw-07", "brw-08")),
    "sub-01": (("sub-02", "sub-03"), ("sub-02", "sub-03")),
    "dash-01": (("stale",), ()),
}


def _migrate_retirements(tasks: dict[str, Task]) -> None:
    """Re-file the pre-`RETIRED` retirements listed in `_RETIREMENTS`.

    In place, on load, guarded twice (see `_RETIREMENTS`). `blocked_reason` is
    left exactly as it was: the prose is the account of WHY, `superseded_by` is
    the machine-readable WHO, and the migration adds the second without
    touching the first.
    """
    for task_id, (markers, successors) in _RETIREMENTS.items():
        task = tasks.get(task_id)
        if task is None or task.status != "blocked":
            continue
        reason = (task.blocked_reason or "").lower()
        if not any(marker in reason for marker in markers):
            continue
        task.status = "retired"
        # Through the same authority as every other writer, so the table above
        # cannot become the one place a malformed chain enters the registry.
        task.superseded_by = _validate_superseded_by(task_id, successors)


def _check_acyclic(tasks: dict[str, Task]) -> None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {tid: WHITE for tid in tasks}

    def visit(tid: str, stack: list[str]) -> None:
        color[tid] = GRAY
        for dep in tasks[tid].depends_on:
            if color.get(dep) == GRAY:
                cycle = " -> ".join(stack + [tid, dep])
                raise TaskGraphError("dependency_cycle", f"dependency cycle: {cycle}")
            if color.get(dep) == WHITE:
                visit(dep, stack + [tid])
        color[tid] = BLACK

    for tid in tasks:
        if color[tid] == WHITE:
            visit(tid, [])


# ---- the fine-grained task-file mutex ---------------------------------------
#
# `TaskStore.save` has always been atomic in the sense `os.replace` gives:
# a reader never sees half a file. That is NOT the same as a read-modify-write
# being atomic, and the difference is exactly what an operator lost. Two
# writers each doing `load() -> mutate -> save()` interleave like this:
#
#     dashboard: load (priority 3)                     loop: load (priority 3)
#     dashboard: set priority 2, save                  loop: mark_completed, save
#
# and whichever `save` lands second silently discards the other's change. Both
# directions are real losses; losing a completion or a quarantine is far worse
# than losing a priority, which is why the immediate priority write may not be
# uncoordinated.
#
# The run-level `LoopLock` cannot be that coordination. It is held for the
# WHOLE run (that is why `answer` and `release` refuse while the loop is up), so
# waiting for it means waiting for the loop to stop — the opposite of an
# immediate edit. This mutex is the opposite shape: one lock file per task file,
# held for the milliseconds of load/mutate/save and nothing else.
#
# THE LOCK FILE IS NEVER WRITTEN TO — it is an empty coordination file, and that
# is load-bearing rather than incidental. `.autoloop/` sits inside the tree
# `escape_detector` snapshots around every write-capable agent call, so a lock
# file whose CONTENT changed mid-round would be reported as an escape. Empty and
# pre-created (`TaskStore.ensure_mutex_file`, called before the "before"
# snapshot) it is byte-identical on both sides and produces no violation at all
# — which is strictly better than teaching the detector to ignore it.

#: Suffix appended to the task file's own name to get its mutex file.
TASKS_MUTEX_SUFFIX = ".lock"

#: How long a caller waits for the mutex before giving up. Every holder does a
#: file read, an in-memory mutation and a file write, so a wait this long means
#: something is wrong (a stopped process holding a `flock`, an unusually slow
#: filesystem) rather than ordinary contention. Failing LOUDLY is the point: a
#: priority edit that silently did not happen is the defect this whole change
#: exists to fix.
MUTEX_TIMEOUT_SECONDS = 10.0

_MUTEX_POLL_SECONDS = 0.01


class TaskStoreBusy(StateError):
    """The task-file mutex could not be taken within `MUTEX_TIMEOUT_SECONDS`.

    A `StateError` subclass so callers that already treat state faults as
    reportable (the CLI, the dashboard's write endpoint) surface it as an error
    instead of quietly writing nothing.
    """


@dataclass
class _MutexEntry:
    """Per-lock-file state: the in-process gate plus the cross-process one.

    Both are needed, and neither replaces the other. `flock` is per OPEN FILE
    DESCRIPTION, so a second `open()` + `LOCK_EX` inside the same process blocks
    against itself — which is a self-deadlock the moment `save()` (which takes
    the mutex) is called inside a caller's own `with store.lock():`. The
    `RLock` makes re-entry free for the owning thread and serialises the others;
    `depth` decides when the file lock is really taken and really released, and
    is only ever touched by the thread holding the `RLock`.
    """

    lock: threading.RLock
    depth: int = 0
    handle: object = None


#: Keyed by RESOLVED lock-file path, so `/x/tasks.json` and `/x/./tasks.json`
#: are one mutex rather than two that would then deadlock on each other's
#: `flock`. Module level because two `TaskStore` INSTANCES pointing at the same
#: file must share it — the dashboard builds a fresh store per request.
_MUTEX_REGISTRY: dict[str, _MutexEntry] = {}
_MUTEX_REGISTRY_GUARD = threading.Lock()


def _mutex_entry(lock_path: Path) -> _MutexEntry:
    key = str(Path(lock_path).resolve())
    with _MUTEX_REGISTRY_GUARD:
        entry = _MUTEX_REGISTRY.get(key)
        if entry is None:
            entry = _MutexEntry(threading.RLock())
            _MUTEX_REGISTRY[key] = entry
        return entry


def mutex_path_for(tasks_path: Path) -> Path:
    """The lock file guarding `tasks_path`. Beside it, never inside it."""
    return Path(str(tasks_path) + TASKS_MUTEX_SUFFIX)


def _acquire_file_lock(lock_path: Path, deadline: float):
    """An exclusive `flock` on `lock_path`, or `TaskStoreBusy`.

    Opened `"a+b"`: created when absent, NEVER truncated and never written, so
    the file's bytes stay empty for its whole life (see the section comment
    above — the escape detector watches this directory).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    if fcntl is None:  # pragma: no cover - POSIX everywhere this runs
        return handle  # in-process serialisation only; documented, not silent
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                raise TaskStoreBusy(
                    f"another process has held {lock_path} for more than "
                    f"{MUTEX_TIMEOUT_SECONDS:.0f}s — nothing was written"
                ) from None
            time.sleep(_MUTEX_POLL_SECONDS)


def _release_file_lock(handle) -> None:
    with contextlib.suppress(OSError):
        if fcntl is not None:  # pragma: no branch - POSIX everywhere this runs
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        handle.close()


@contextlib.contextmanager
def task_file_mutex(tasks_path: Path, timeout: float = MUTEX_TIMEOUT_SECONDS):
    """Hold the fine-grained mutex for `tasks_path` for the body of the block.

    RE-ENTRANT within a thread, so `save()` nested inside a caller's own
    `with store.lock():` is free rather than a deadlock. EXCLUSIVE across
    threads (the `RLock`) and across processes (the `flock`). Raises
    `TaskStoreBusy` rather than blocking forever.

    A mutex only one side respects is not a mutex: every writer of the task
    file takes this — `TaskStore.save`, `TaskStore.archive`, the dashboard's
    immediate priority write — and the whole read-modify-write goes inside one
    hold, never just the write half.
    """
    lock_path = mutex_path_for(tasks_path)
    entry = _mutex_entry(lock_path)
    deadline = time.monotonic() + timeout
    if not entry.lock.acquire(timeout=max(0.0, timeout)):
        raise TaskStoreBusy(
            f"another thread has held the mutex for {lock_path} for more than "
            f"{timeout:.0f}s — nothing was written"
        )
    try:
        if entry.depth == 0:
            entry.handle = _acquire_file_lock(lock_path, deadline)
        entry.depth += 1
        try:
            yield
        finally:
            entry.depth -= 1
            if entry.depth == 0 and entry.handle is not None:
                _release_file_lock(entry.handle)
                entry.handle = None
    finally:
        entry.lock.release()


# ---- the mutation ledger (attestation, OUTSIDE the checkout) -----------------

#: The ledger's filename. It lives beside `workers_root` — the same placement
#: the inbox, the PAUSE flag and the heartbeat already use, and for the same
#: reason: that directory is required to be outside the checkout, its `.git`,
#: the state dir and the publisher paths (`worker_env.validate_workers_root`),
#: so nothing written here is inside the tree the escape detector snapshots.
LEDGER_FILENAME = "task-mutations.jsonl"

#: What a ledger record's `kind` says for the one mutation an operator may
#: apply immediately. Nothing else is ever recorded, so a record is never a
#: general "I edited the task file" claim.
LEDGER_KIND_PRIORITY = "priority"

#: A record's `phase`, and the distinction the whole attestation now turns on.
#:
#: `apply_priority` writes an INTENT before it touches the task file and a
#: COMPLETE only after the bytes have landed. Only COMPLETE authorizes anything.
#:
#: Why both, rather than one record: they answer opposite failure modes and
#: neither covers the other.
#:   * The INTENT is what makes a ledger that cannot be appended to leave the
#:     task file UNTOUCHED. The append is attempted first, so a broken ledger
#:     directory aborts the edit before any write instead of producing a change
#:     nothing can attest — which would park the loop the moment it landed
#:     inside a detection window.
#:   * The COMPLETE is what keeps that same intent from becoming a licence. A
#:     write that was announced and then FAILED still leaves its intent on
#:     record; if an intent contributed an edge, an agent could write exactly
#:     the state that intent named and be exempted for a change no operator
#:     ever made. So an intent is a note about a write that was about to be
#:     attempted, never evidence that one happened.
LEDGER_PHASE_INTENT = "intent"
LEDGER_PHASE_COMPLETE = "complete"


def canonical_task_path(path) -> str:
    """The one spelling of a task file's path that a ledger record carries and
    that `completed_chain` compares against.

    Symlinks resolved, so `/x/./tasks.json`, a relative spelling, and macOS's
    `/var` -> `/private/var` all reduce to one string. Both sides of the
    attestation go through this function for the same reason `_mutex_entry`
    resolves its key: a record filed under one spelling and looked up under
    another is a record that silently proves nothing.

    Falls back to an absolute (unresolved) path if the filesystem refuses to
    resolve it — that keeps two callers on the same machine agreeing rather
    than raising in the middle of a detection window.
    """
    try:
        return str(Path(path).resolve())
    except OSError:  # pragma: no cover - resolve() is non-strict; needs a hostile fs
        return str(Path(path).absolute())


def mutation_ledger_for(workers_root: Path | None, state_dir: Path) -> Path:
    """Where the mutation ledger lives, resolved exactly like
    `inbox.inbox_dir_for` so both sides of the attestation (the dashboard that
    writes records, the orchestrator that reads them) compute one path from one
    function.

    Falls back INSIDE the state dir only when no `workers_root` is configured —
    a configuration `load_config` already refuses for any real run. In that
    fallback the ledger is inside the snapshotted tree and an edit mid-round is
    reported as an escape, which is the safe direction: the attestation must
    live somewhere an agent's own checkout writes cannot reach, and if there is
    no such place configured there is no attestation to be had.
    """
    if workers_root is not None:
        return Path(workers_root).expanduser().parent / LEDGER_FILENAME
    return Path(state_dir) / LEDGER_FILENAME


class MutationLedger:
    """Append-only record of the operator priority edits `TaskStore` applied.

    WHAT IT IS FOR, precisely. `.autoloop/tasks.json` is inside the tree
    `escape_detector` snapshots around every write-capable agent call, and that
    coverage is not an oversight — the file holds `approved_paths`, so an agent
    able to edit it undetected could widen its own authorization
    (`docs/SECURITY.md` finding #2). An immediate priority edit therefore has to
    be distinguishable from an agent writing into the checkout, and "the path is
    `tasks.json`" is not a distinction — excluding the file wholesale would hand
    back exactly the hole the coverage closes.

    So the detector's exemption asks TWO independent questions, and both must
    answer yes (`TaskStore.attested_priority_edit`):

      1. **Who** — do the COMPLETED mutations recorded here DURING THIS
         DETECTION WINDOW, for THIS task file, form one sequential chain that
         starts at the digest the snapshot saw BEFORE and ends at the one it saw
         AFTER? This file is outside the checkout, so a write into the checkout
         alone cannot produce a record. An agent that edits `tasks.json`
         directly is unattested and is reported.
      2. **What** — do the two file states differ ONLY in `priority` values
         (`priority_only_change`, which reads the actual bytes rather than
         trusting this record's `kind`)? So even a forged record cannot launder
         a widened `approved_paths`, a flipped `status` or a rewritten
         `depends_on`.

    **WHY A CHAIN AND NOT REACHABILITY.** Question 1 was originally "is the
    after-digest REACHABLE from the before-digest by any sequence of hops",
    over the whole ledger. Reachability is a strictly weaker claim than "this
    is what happened", and the gap is exploitable in two ways that a chain
    closes:

      * **The round trip.** An operator moves a priority 3 -> 2 and back to 3.
        The file ends where it began, but the intermediate digest B is now
        reachable from A forever. An agent that later writes exactly state B
        into the checkout — which it can, B being an ordinary priority-only
        state — is then "attested" for a change no operator made. Read as a
        chain, the same two records say A -> B -> A, whose terminal state is A;
        an observed after-state of B is not that terminal state, so it is
        reported.
      * **The announced-but-failed write.** A reachability walk cannot tell an
        intent from an outcome, so a write that was recorded and then failed
        left a usable edge to a state that never existed on disk. Hence
        `LEDGER_PHASE_COMPLETE`: only records written AFTER the bytes landed
        are edges at all.

    Two more bindings keep the chain about THIS window and THIS file: records
    below the caller's watermark (`TaskStore.priority_edit_watermark`, captured
    before the agent starts) are not part of it, and neither are records naming
    another task file (`canonical_task_path`) — a second loop sharing a ledger
    must not be able to attest edits to a checkout it does not own.

    Honest residual, recorded rather than argued away: this is an attestation,
    not an authenticator. Nothing signs a record, so an agent that writes
    outside its worker repo AND knows this format can append one — but question
    2 still bounds what that buys it to the priority field, which the task
    graph's own design already treats as safe to change mid-flight (nothing
    already dispatched depends on it). Real confinement needs the OS-level
    sandbox `docs/SECURITY.md` S24 tracks, not a wider ledger.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def _append(self, *, tasks_path: Path, before: str, after: str, phase: str,
                kind: str, ids: tuple[str, ...]) -> None:
        """Append one record. Raises `OSError` if it cannot be written.

        Deliberately NOT best-effort, in either phase. See `record_intent` and
        `record_complete` for what each failure means.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "at": utcnow_iso(),
                "pid": os.getpid(),
                "path": canonical_task_path(tasks_path),
                "kind": kind,
                "phase": phase,
                "ids": list(ids),
                "before": before,
                "after": after,
            },
            ensure_ascii=False,
        )
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def record_intent(self, *, tasks_path: Path, before: str, after: str,
                      kind: str = LEDGER_KIND_PRIORITY, ids: tuple[str, ...] = ()) -> None:
        """Announce a write that is about to be attempted. Authorizes NOTHING.

        Load-bearing despite that: `apply_priority` appends this BEFORE it
        touches the task file, so a ledger that cannot be written leaves the
        task file exactly as it was. The alternative ordering (write, then
        attest) has no such recovery — it produces a change nothing can attest,
        which parks the loop the moment it lands inside a detection window.
        """
        self._append(tasks_path=tasks_path, before=before, after=after,
                     phase=LEDGER_PHASE_INTENT, kind=kind, ids=ids)

    def record_complete(self, *, tasks_path: Path, before: str, after: str,
                        kind: str = LEDGER_KIND_PRIORITY,
                        ids: tuple[str, ...] = ()) -> None:
        """Record that the bytes really landed. The ONLY thing that becomes an
        edge in `completed_chain`, and therefore the only thing that can silence
        the escape detector."""
        self._append(tasks_path=tasks_path, before=before, after=after,
                     phase=LEDGER_PHASE_COMPLETE, kind=kind, ids=ids)

    def read(self) -> list[dict]:
        """Every readable record, oldest first. A missing ledger is `[]`, and a
        malformed LINE is skipped rather than raising — an unusable record
        proves nothing, and refusing to read the whole file because of one bad
        line would turn a stray byte into a loop-fatal park."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        records = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def count(self) -> int:
        """How many readable records exist right now — the watermark a detection
        window is opened with.

        A COUNT over `read()` rather than a byte offset, because `read()` is
        what the window is later sliced out of and the two must agree about
        which records are "already there". The ledger is append-only and never
        pruned, so a record's index is stable: a line skipped as malformed
        before the window opens is skipped identically afterwards.
        """
        return len(self.read())

    def completed_chain(
        self,
        start: str,
        *,
        since: int,
        tasks_path,
        kind: str = LEDGER_KIND_PRIORITY,
    ) -> list[str] | None:
        """The sequence of file states that COMPLETED mutations of `tasks_path`
        walked through since record index `since`, beginning at `start` —
        `[start]` when the window is empty, or `None` when the records do not
        describe one unbroken chain from `start`.

        The proof `attested_priority_edit` needs, and deliberately not a
        reachability set (see the class docstring for the two holes that
        closes). Three filters decide what is even looked at, and each is a
        binding rather than a convenience:

          * `since` — the caller's watermark, captured before the agent it is
            judging began. An edit from an EARLIER window says nothing about
            this one, and leaving old edges in scope is what let a long-dead
            round trip keep authorizing.
          * `tasks_path` — canonicalised on both sides. A second loop appending
            to a shared ledger must not attest a change to a checkout it does
            not own.
          * `phase == LEDGER_PHASE_COMPLETE` — an intent is a note about a write
            that was about to be attempted. A record from a write that FAILED
            must not become a licence to reproduce the state it named. A record
            written before this field existed carries no phase and is therefore
            not a completion either, which is the safe direction.

        Records that pass those filters must chain EXACTLY: the first one's
        `before` is `start`, each subsequent one's `before` is its predecessor's
        `after`. A gap means something other than `apply_priority` wrote the
        file in between, and there is no honest way to describe that as an
        operator priority edit — so it is `None`, not a shorter chain.
        """
        wanted = canonical_task_path(tasks_path)
        chain = [start]
        for record in self.read()[max(0, since):]:
            if record.get("kind") != kind:
                continue
            if record.get("phase") != LEDGER_PHASE_COMPLETE:
                continue
            if record.get("path") != wanted:
                continue
            before, after = record.get("before"), record.get("after")
            if not isinstance(before, str) or not isinstance(after, str):
                return None
            if not before or not after or before != chain[-1]:
                return None
            chain.append(after)
        return chain


def _comparable_without_priority(data: object):
    """A task file reduced to everything EXCEPT the priority of each task, or
    `None` when it is not a task file at all.

    `None` is never equal to anything here (its callers check for it), so two
    unreadable files do not compare equal and get exempted together.
    """
    if not isinstance(data, dict):
        return None
    rows = data.get("tasks")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    rest = {key: value for key, value in data.items() if key != "tasks"}
    stripped = [{k: v for k, v in row.items() if k != "priority"} for row in rows]
    return rest, stripped


def priority_only_change(before: bytes, after: bytes) -> bool:
    """Do these two task-file contents differ ONLY in `priority` values?

    The CONTENT half of the escape-detector exemption (see `MutationLedger`),
    and the half that does not trust anything an attestation claims about
    itself: it compares the two files with every task's `priority` removed, so a
    changed `approved_paths`, `status`, `depends_on`, `description` or task set
    — anything that could widen an agent's authorization or rewrite the queue —
    is not a priority edit whatever any record says.

    Row ORDER is significant, deliberately. `TaskStore` writes tasks in registry
    order and `apply_priority` never reorders them, so a reordered file was
    written by something else.
    """
    try:
        left = _comparable_without_priority(json.loads(before))
        right = _comparable_without_priority(json.loads(after))
    except ValueError:
        return False
    return left is not None and left == right


class TaskStore:
    """Atomic JSON persistence for the registry (same pattern as StateStore),
    serialised by a SHORT-LIVED mutex.

    `os.replace` makes a save atomic; it does nothing for the read-modify-write
    around it, which is where updates actually go missing (see the section
    comment above `task_file_mutex`). So every method here that reads and then
    writes holds `task_file_mutex` for the whole sequence, and so must every
    other writer of this file — the dashboard's immediate priority edit takes
    the same lock through `apply_priority`.

    `ledger` is where operator priority edits are attested, and it is optional
    only in the sense that a store built without one cannot apply an immediate
    edit (`apply_priority` refuses rather than writing something the escape
    detector would report as an agent escape). Production wiring passes
    `mutation_ledger_for(config.workers_root, config.state_dir)`.
    """

    def __init__(self, path: Path, ledger: Path | None = None):
        self.path = Path(path)
        self.ledger = MutationLedger(ledger) if ledger is not None else None

    # ---- coordination -------------------------------------------------------

    def lock(self, timeout: float = MUTEX_TIMEOUT_SECONDS):
        """The fine-grained mutex for this task file, as a context manager.

        Public because a caller doing its own load/mutate/save must be able to
        put ALL THREE inside one hold — `save()` taking the lock by itself would
        only make the write atomic, which it already was.
        """
        return task_file_mutex(self.path, timeout=timeout)

    def ensure_mutex_file(self) -> Path:
        """Create the (always empty) lock file if it does not exist yet, and
        return it.

        Called by the orchestrator immediately BEFORE the escape detector's
        "before" snapshot. The lock file lives in the state dir, inside the
        snapshotted tree, so a dashboard edit that had to create it mid-round
        would show up as `created outside the worker repo` — a false loop-fatal
        park. Pre-created, its bytes are empty and identical on both sides and
        the detector has nothing to report, which needs no exemption at all.
        """
        lock_path = mutex_path_for(self.path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            with open(lock_path, "a+b"):
                pass
        return lock_path

    # ---- persistence --------------------------------------------------------

    def load(self) -> TaskRegistry | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateCorruptError(f"cannot decode {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise StateCorruptError(f"{self.path} does not contain a JSON object")
        if data.get("schema_version") != TASKS_SCHEMA_VERSION:
            raise StateError(
                f"task schema version {data.get('schema_version')!r} != "
                f"supported {TASKS_SCHEMA_VERSION}"
            )
        return TaskRegistry.from_dict(data)

    @staticmethod
    def _serialize(registry: TaskRegistry) -> bytes:
        """The exact bytes a save writes. One function, because the digest
        recorded in the ledger has to be the digest of the file that lands —
        a second `json.dumps` with different arguments would chain the
        attestation to a state that never existed on disk."""
        return json.dumps(
            registry.to_dict(), ensure_ascii=False, indent=2
        ).encode("utf-8")

    def _write_bytes(self, data: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.path)

    def _digest(self) -> str:
        """sha256 of the task file as it is right now; `""` when there is none.
        Read under the caller's mutex hold, so it is the digest the next write
        chains from."""
        try:
            return hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError:
            return ""

    def save(self, registry: TaskRegistry) -> None:
        """Persist `registry`, under the mutex, after reconciling the ONE field
        another writer is allowed to have changed underneath.

        The reconciliation is not decoration. A running loop holds its registry
        in memory for a whole round; an operator priority edit that lands
        mid-round is on disk, not in that object, so the next ordinary save
        (`mark_in_progress`, `mark_completed`, a park) would write the stale
        value back and silently undo it — the lost update the inbox was
        originally built to avoid, arriving from the other side. So every save
        adopts the on-disk `priority` for tasks the caller has not itself
        re-prioritised.

        **Inbox priority requests take precedence over the disk value**, and
        that is what `TaskRegistry.priority_overrides()` is for: applying a
        drained request is a deliberate write, so an id `set_priority` touched
        since the last save keeps the value in memory. Same rule as
        `inbox.apply_requests`' own last-write-wins.

        The sharp edge that follows, stated for the next author: `set_priority`
        is the ONLY thing that records an override. A priority passed to
        `Task(...)`/`add_many` does not, so saving a freshly CONSTRUCTED task
        over a stored row with the same id adopts the stored number. Nothing in
        production can hit that — a new task's id is by definition not on disk,
        and `cli._seed_registry` runs only when `load()` returned `None` — but a
        test that hand-builds a second registry for a path it already saved to
        will see the first value win.

        Reconciliation FAILS OPEN. A corrupt, foreign-schema or unreadable task
        file adopts nothing and the save proceeds: this method is on the path
        that records completions and quarantines, and a save that started
        refusing because the file it is about to overwrite will not parse would
        be a far worse bug than a late priority.
        """
        with self.lock():
            self.reconcile_priorities(registry)
            self._write_bytes(self._serialize(registry))
            registry.clear_priority_overrides()

    def reconcile_priorities(self, registry: TaskRegistry) -> list[str]:
        """Copy the on-disk `priority` onto every in-memory task whose priority
        the caller has not itself changed since the last save. Returns the ids
        adopted, so a caller can log what an operator steered.

        Priority ONLY. Nothing else on disk is trusted over the in-memory
        registry, because nothing else on disk can have been written by the
        operator path: `apply_priority` is the only immediate write, and it can
        express nothing but this field.
        """
        with self.lock():
            try:
                disk = self.load()
            except (StateError, OSError, ValueError):
                return []  # fail open — see `save`
            if disk is None:
                return []
            overrides = registry.priority_overrides()
            adopted = []
            for task in registry.all_tasks():
                if task.id in overrides or not disk.has(task.id):
                    continue
                stored = disk.get(task.id).priority
                if stored != task.priority:
                    task.priority = stored
                    adopted.append(task.id)
            return adopted

    def apply_priority(self, task_id: str, priority: int) -> Task:
        """Set one task's priority and PERSIST IT NOW. Returns the task as read
        back from disk.

        The operator's immediate steering path (`dashboard`'s `/api/priority`).
        Queueing it through the inbox meant the value only became true when the
        loop next drained between steps, while the page kept re-rendering from
        `tasks.json` — so a save that worked and a save that did not looked
        identical, and the operator resubmitted. A priority that lands minutes
        later has already missed the decision it was meant to influence.

        Everything happens inside ONE mutex hold: load, mutate, attest, write,
        and the read-back. It takes NO `LoopLock` — that lock is held for the
        entire run, so waiting for it would mean waiting for the loop to stop.

        The return value is re-READ from the file rather than echoed from the
        argument or from the in-memory registry. That read-back is the proof of
        persistence the operator's row shows; echoing the input would redisplay
        the number they typed whether or not anything reached the disk.

        Refusals, all of which leave the file untouched:
          * no task file yet — this path may not CREATE the registry. The loop
            seeds it from `seed_tasks.json` on its first real save
            (`cli._seed_registry`), and materialising it from a priority form
            would be a brand-new write path, not a priority edit.
          * no ledger configured — the write would be unattestable and would
            park the loop as an escape if it landed mid-round. See
            `MutationLedger`.
          * unknown task, or a non-integer priority — `TaskRegistry.set_priority`
            raises `TaskGraphError`, unchanged.

        Two ledger records, not one, and the order is the design (see
        `MutationLedger.record_intent` / `record_complete`): the INTENT goes
        first so an unwritable ledger aborts before anything is written, and the
        COMPLETE goes after the bytes land so a failed write leaves behind a
        note rather than a licence.
        """
        if self.ledger is None:
            raise StateError(
                "no mutation ledger configured for this task store — an "
                "immediate priority write must be attestable outside the "
                "checkout or the loop's escape detector reports it as an agent "
                "escape (see MutationLedger)"
            )
        with self.lock():
            registry = self.load()
            if registry is None:
                raise StateError(
                    f"no task registry at {self.path} yet — the loop writes it "
                    "on its first task-graph change; a priority edit does not "
                    "create it"
                )
            registry.set_priority(task_id, priority)
            payload = self._serialize(registry)
            before = self._digest()
            after = hashlib.sha256(payload).hexdigest()
            # Announced BEFORE the write: a ledger that cannot be appended to
            # must leave the task file untouched rather than produce a change
            # nothing can attest. This record authorizes nothing by itself.
            self.ledger.record_intent(
                tasks_path=self.path, before=before, after=after,
                kind=LEDGER_KIND_PRIORITY, ids=(task_id,),
            )
            self._write_bytes(payload)
            try:
                # Only now is there an outcome to attest. If THIS append fails
                # the change has already persisted, so the error has to say so:
                # re-raising the bare OSError would read as "nothing happened",
                # which is precisely the defect this whole path exists to fix.
                self.ledger.record_complete(
                    tasks_path=self.path, before=before, after=after,
                    kind=LEDGER_KIND_PRIORITY, ids=(task_id,),
                )
            except OSError as exc:  # pragma: no cover - the intent just succeeded here
                raise StateError(
                    f"priority for {task_id!r} WAS written to {self.path}, but "
                    f"the completion could not be recorded in {self.ledger.path} "
                    f"({exc}) — the change is live and unattested, so a round in "
                    "flight may report it as a checkout escape"
                ) from exc
            persisted = self.load()
            if persisted is None or not persisted.has(task_id):  # pragma: no cover
                raise StateError(
                    f"priority for {task_id!r} did not survive the write to "
                    f"{self.path}"
                )
            return persisted.get(task_id)

    def priority_edit_watermark(self) -> int:
        """How many ledger records exist right now — the mark that opens a
        detection window.

        Captured by `orchestrator._operator_priority_exemption` before the
        write-capable agent starts, and passed back to `attested_priority_edit`
        afterwards, so the attestation describes mutations from THIS window and
        not from the loop's whole history. `0` when no ledger is configured; the
        predicate refuses outright in that case anyway.

        Take it under the same `lock()` hold as the baseline bytes it will be
        compared against — the two are one observation, and a record appended
        between them would be attributed to the wrong side of the window.
        """
        return 0 if self.ledger is None else self.ledger.count()

    def capture_priority_window(self) -> tuple[bytes, int]:
        """The (task-file bytes, ledger watermark) pair that opens a detection
        window, read as ONE instant under the mutex.

        Read separately, they can straddle an in-flight `apply_priority`: the
        bytes come out as state A while the watermark is taken after the A -> B
        completion was appended, so the chain walk later finds an empty window,
        concludes nothing legitimate happened, and parks the loop on a perfectly
        benign operator edit. That is the same read-modify-write race this whole
        change exists to close, so it is closed on the reading side too.

        Raises `OSError` if the task file cannot be read, and `TaskStoreBusy` if
        the mutex cannot be taken — both leave the caller to decide, which it
        does by declining to offer an exemption at all.
        """
        with self.lock():
            return self.path.read_bytes(), self.priority_edit_watermark()

    def attested_priority_edit(self, before_bytes: bytes, before_sha: str,
                               after_sha: str, watermark: int) -> bool:
        """Is the change from `before_sha` to `after_sha` an operator priority
        edit this store applied during this detection window — and nothing more?

        The predicate behind the escape detector's one exemption for this file.
        Both halves must hold (see `MutationLedger` for why either alone is
        insufficient):

          * WHO — the COMPLETED priority mutations recorded since `watermark`
            for THIS task file form one unbroken chain that starts at
            `before_sha` and whose TERMINAL state is `after_sha`
            (`MutationLedger.completed_chain`). The ledger is outside the
            checkout, so an agent editing `tasks.json` from inside it produces
            no record and is reported. An intent from a failed write is not a
            hop, and an intermediate state from an earlier round trip is not a
            terminal one.
          * WHAT — the file differs from `before_bytes` only in `priority`
            values, checked against the actual bytes rather than against the
            ledger's own claim about itself.

        THE OBSERVED AFTER-STATE IS EXACTLY WHAT IS ON DISK NOW, and both the
        chain and the byte comparison are held to it: the current bytes are
        re-read under the mutex and must hash to `after_sha`. That is a
        deliberate reversal of the previous behaviour, which allowed the file to
        have moved on to a LATER attested state since the snapshot. Allowing
        that is what made "reachable from" the test instead of "is the outcome
        of", and reachability is the weaker claim the round-trip hole lived in.

        The residual that reversal buys, stated rather than hidden: a SECOND
        legitimate operator edit landing between the after-snapshot and this
        call is now reported as an escape, and the gap is the remainder of
        `snapshot_checkout`'s walk over the checkout rather than microseconds.
        That is a spurious loop-fatal park an operator can read and recover from
        (`reset --yes`), traded for closing a laundering path that is silent by
        construction — the safe direction of the two.
        """
        if self.ledger is None or not before_sha or not after_sha:
            return False
        if hashlib.sha256(before_bytes).hexdigest() != before_sha:
            return False
        with self.lock():
            try:
                current_bytes = self.path.read_bytes()
            except OSError:
                return False
            if hashlib.sha256(current_bytes).hexdigest() != after_sha:
                return False
            chain = self.ledger.completed_chain(
                before_sha, since=watermark, tasks_path=self.path,
                kind=LEDGER_KIND_PRIORITY,
            )
            if chain is None or chain[-1] != after_sha:
                return False
            return priority_only_change(before_bytes, current_bytes)

    def archive(self) -> Path | None:
        """Move the task file aside (`reset --tasks`), under the mutex.

        The lock matters here for the same reason it does in `save`: without it
        an immediate priority write can land between the `exists()` check and
        the `os.replace`, and the operator's edit is archived into a backup
        nobody is looking at while the loop starts from a seed.
        """
        with self.lock():
            if not self.path.exists():
                return None
            from datetime import datetime, timezone

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = self.path.with_name(f"{self.path.name}.bak-{stamp}")
            os.replace(self.path, backup)
            return backup
