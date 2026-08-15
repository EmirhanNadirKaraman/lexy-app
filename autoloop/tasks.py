"""Task registry / task graph.

The roadmap ChatGPT plans against. Tasks have stable ids, dependencies, and a
stored status (pending / in_progress / completed); ready vs blocked is DERIVED
from dependencies, never stored, so it can not go stale. ChatGPT authorizes
work by task id only (contract v2) — free-form instructions are gone.

Graph invariants enforced on every mutation: unique slug ids, dependencies
must reference known tasks (same batch counts), no cycles, no completing a
task whose dependencies are incomplete. Violations raise TaskGraphError with a
stable code that the orchestrator reports back to ChatGPT.

Persistence mirrors state.py: one JSON file, atomic replace, schema-versioned.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

from .errors import StateCorruptError, StateError, TaskGraphError
from .state import utcnow_iso

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
    status: str = "pending"  # pending | in_progress | completed | blocked
    created_at: str = field(default_factory=utcnow_iso)
    completed_at: str | None = None
    #: Operator-facing reason, set by `TaskRegistry.block` and cleared by
    #: `unblock`. Empty for every task that has never been quarantined.
    #: New field with a default — an old `tasks.json` written before this
    #: existed loads fine (`Task(**raw)` falls back to the default), see
    #: `TaskRegistry.from_dict` below and `test_blockers.py`'s backward-
    #: compatibility test.
    blocked_reason: str = ""
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


#: Repository trackers every task may update, WITHOUT naming them in its own
#: `approved_paths`.
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
#: Why this is a narrow widening and not a hole:
#:   * Fixed constant, deliberately NOT configurable — widening the scope of
#:     every task must be a diff someone reviews, not a TOML edit.
#:   * Markdown trackers only. No code, no config, no test file, nothing
#:     executable, nothing that changes behaviour.
#:   * The paths still appear in `commit_range_paths` and in the review
#:     packet, so a reviewer sees every tracker edit; this removes a REFUSAL,
#:     not visibility.
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


def effective_approved_paths(approved: tuple[str, ...]) -> tuple[str, ...]:
    """A task's authorized paths PLUS the always-allowed trackers, sorted.

    The single place the two are combined, so the dispatch-time seed and the
    every-dispatch re-sync cannot disagree — if only one of them applied the
    trackers, the other would silently narrow the scope back on the next round.

    An EMPTY `approved` stays empty. "No scope authorized yet" must keep
    refusing dispatch (`docs/SECURITY.md` finding #2, circular ownership);
    returning just the trackers would turn an unscoped task into a dispatchable
    one that may write documentation, which is not what "unscoped" means.
    """
    if not approved:
        return ()
    return tuple(sorted(set(approved) | set(TRACKER_PATHS)))


class TaskRegistry:
    def __init__(self, tasks: list[Task] | None = None):
        self._tasks: dict[str, Task] = {}
        if tasks:
            self.add_many(tasks)

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
            seen_paths: set[str] = set()
            for approved in task.approved_paths:
                _validate_approved_path(approved)
                if approved in seen_paths:
                    raise TaskGraphError(
                        "duplicate_approved_path",
                        f"task '{task.id}' lists approved path {approved!r} more than once",
                    )
                seen_paths.add(approved)
            candidate[task.id] = task
        for task in tasks:
            for dep in task.depends_on:
                if dep not in candidate:
                    raise TaskGraphError(
                        "unknown_dependency",
                        f"task '{task.id}' depends on unknown task '{dep}'",
                    )
                if dep == task.id:
                    raise TaskGraphError(
                        "dependency_cycle", f"task '{task.id}' depends on itself"
                    )
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
        task.status = "in_progress"
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
        the first (e.g. re-dispatched after a crash). Refuses only a
        completed task, which can never legitimately need quarantining."""
        task = self.get(task_id)
        if task.status == "completed":
            raise TaskGraphError(
                "task_completed", f"task '{task_id}' is already completed — cannot block it"
            )
        task.status = "blocked"
        task.blocked_reason = reason
        return task

    def unblock(self, task_id: str) -> Task:
        """Reverse of `block`: back to `pending`, i.e. READY again once its
        dependencies are still satisfied (`state_of` re-derives that from
        `depends_on` exactly as for any other pending task). Called by
        `python -m autoloop answer` once the operator resolves the blocker
        that quarantined it."""
        task = self.get(task_id)
        if task.status != "blocked":
            raise TaskGraphError("task_not_blocked", f"task '{task_id}' is not blocked")
        task.status = "pending"
        task.blocked_reason = ""
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
        task = self.get(task_id)
        if self.state_of(task_id) is not TaskState.IN_PROGRESS:
            raise TaskGraphError(
                "task_not_in_progress",
                f"task '{task_id}' is not in progress (status {task.status!r}) — "
                "`release` only returns an interrupted round to pending; use "
                "`unblock` for a quarantined task",
            )
        task.status = "pending"
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
        if any(self._tasks[dep].status != "completed" for dep in task.depends_on):
            return TaskState.BLOCKED
        if task.status == "in_progress":
            return TaskState.IN_PROGRESS
        return TaskState.READY

    def ready_tasks(self) -> list[Task]:
        return [t for t in self._tasks.values() if self.state_of(t.id) is TaskState.READY]

    def set_priority(self, task_id: str, priority: int) -> Task:
        """Re-prioritise an existing task.

        Deliberately the ONLY mutation an operator can make from outside the
        normal `plan`/`seed_tasks.json` route (see `inbox.KIND_PRIORITY`).
        Priority changes what runs next; it cannot change what a task is
        allowed to touch, so it is safe to expose on a form in a way
        `approved_paths` would not be.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise TaskGraphError("unknown_task", f"no task with id '{task_id}'")
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise TaskGraphError(
                "bad_priority", f"priority must be an integer, got {priority!r}"
            )
        task.priority = priority
        return task

    def set_description(self, task_id: str, description: str) -> Task:
        """Rewrite an existing task's description.

        Registry primitive, deliberately with NO operator route to it. A
        description is what a write-capable agent reads as its instructions,
        which puts it in the same class as `approved_paths` rather than
        `priority` — `inbox.py` says so in as many words, and refuses a general
        "edit a task" request for exactly that reason. Exposing this through
        the inbox or a localhost form is therefore a separate decision that has
        to revisit that comment; it is not a follow-up this method implies.

        Validation is `_validate_description`, the SAME function creation goes
        through, so a description the registry would refuse to create a task
        with cannot be written onto an existing one either.

        Rejection is atomic: the lookup and the validation both run before the
        single assignment, so a refused call leaves the registry exactly as it
        was rather than half-mutated. An unknown id raises `task_unknown` via
        `get`, like every other mutator here — `set_priority`'s hand-rolled
        `unknown_task` is the outlier, and it is left alone because that code
        string reaches the operator through the inbox's refusal text.
        """
        task = self.get(task_id)
        _validate_description(task_id, description)
        task.description = description
        return task

    def next_ready(self) -> Task | None:
        """Highest-priority ready task; ties broken by id.

        Was insertion order. Ordering by `priority` first is what lets an
        operator steer a running loop — otherwise a task added later can
        never overtake one already queued, no matter how urgent.
        """
        ready = self.ready_tasks()
        if not ready:
            return None
        return sorted(ready, key=lambda t: (t.priority, t.id))[0]

    def summary(self) -> str:
        """One line of roadmap state, rendered into every review request.

        The READY count carries a priority-1 breakdown with it because
        `contract.AUDIT_VS_READY_PREFERENCE` tells the reviewer to implement or
        revise a ready task instead of ordering a fresh audit, and to weigh how
        urgent the queue is. A rule that depends on a number the reviewer
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
            f"{counts[TaskState.BLOCKED_BY_OPERATOR]} quarantined"
        )
        if nxt is not None:
            parts += f"; next ready: {nxt.id} — {nxt.title}"
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
        _check_acyclic(registry._tasks)
        return registry


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


class TaskStore:
    """Atomic JSON persistence for the registry (same pattern as StateStore)."""

    def __init__(self, path: Path):
        self.path = Path(path)

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

    def save(self, registry: TaskRegistry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(registry.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def archive(self) -> Path | None:
        if not self.path.exists():
            return None
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        os.replace(self.path, backup)
        return backup
