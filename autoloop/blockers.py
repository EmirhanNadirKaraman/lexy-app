"""Persistent operator-facing blocker records.

Continuous mode (`cli.py`'s `run --continuous`) is multi-track: when a park
site in `orchestrator.py` is classified `task_fatal` (see `_to_needs_user`'s
`kind` parameter and the classification table in its module docstring),
only the ONE task at fault is set aside — the loop keeps working whatever
else is READY (`tasks.TaskRegistry.block`/`unblock`). A `loop_fatal` park
still stops the loop outright, exactly as every park did before this module
existed.

Either way — task_fatal or loop_fatal — the operator-facing question that
would have been shown at the park site is durably persisted here, one JSON
file per blocker, so it survives whatever the loop does next (clearing the
session state for a task_fatal park, or the loop simply exiting for a
loop_fatal one) and can be listed/answered later:

    python -m autoloop blockers            # list open ones (--all for resolved too)
    python -m autoloop answer <id> "..."   # resolve + unblock the task if task_fatal

Same crash-safety rule as every other store in this package
(`worktask.TaskExecutionStore`, `tasks.TaskStore`): a corrupt record RAISES
(`StateCorruptError`) rather than silently reading as absent. Reading a
blocker as "not there" when it actually failed to decode would let the
operator believe a problem was resolved (or never existed) when it did not —
exactly backwards for a record whose entire purpose is not losing track of
open questions.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import StateCorruptError, StateError
from .state import utcnow_iso

#: Sentinel `task_id` for a blocker not tied to any specific registry task
#: (e.g. a login expiry, or an iteration-budget exhaustion mid corrective
#: re-prompt — the loop-level failures every park used to be before
#: task_fatal existed). Never a real task id: `tasks._ID_RE` forbids
#: parentheses, so this can never collide with one.
NO_TASK = "(loop)"


@dataclass
class Blocker:
    id: str                 # stable: f"blk-{task_id}-{n:03d}"
    task_id: str
    kind: str                # "task_fatal" | "loop_fatal"
    code: str                # short machine slug, e.g. "attempt_count_ceiling"
    question: str            # the operator-facing text (what _to_needs_user was given)
    detail: str              # extra context (paths, shas, reasons)
    phase: str               # loop phase when it happened
    created_at: str
    resolved_at: str | None = None
    answer: str | None = None
    #: How many times this exact condition has re-parked. A restart or retry
    #: that hits the same wall must UPDATE this record, never mint a second
    #: one — otherwise a crash-retry loop silently fills the queue with
    #: duplicates of one problem and the operator cannot see how many
    #: distinct things are actually wrong.
    recurrences: int = 1
    last_seen_at: str = ""


class BlockerStore:
    """One JSON file per blocker under `directory`. Atomic temp-file +
    `os.replace` writes — same pattern as `tasks.TaskStore`.

    Filenames double as ids (`<id>.json`), so `next_id` can derive the next
    free sequence number for a task purely by listing the directory — there
    is no separate counter file that could drift out of sync with what is
    actually on disk.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, blocker_id: str) -> Path:
        return self.directory / f"{blocker_id}.json"

    def save(self, blocker: Blocker) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(blocker.id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(asdict(blocker), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def load(self, blocker_id: str) -> Blocker | None:
        path = self._path(blocker_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Blocker(**data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StateCorruptError(f"blocker record {path} is unreadable: {exc}") from exc

    def next_id(self, task_id: str) -> str:
        """`f"blk-{task_id}-{n:03d}"` for the next free `n` — scans existing
        files for `task_id` rather than keeping a separate counter."""
        prefix = f"blk-{task_id}-"
        n = 0
        if self.directory.exists():
            for path in self.directory.glob(f"{prefix}*.json"):
                suffix = path.stem[len(prefix):]
                if suffix.isdigit():
                    n = max(n, int(suffix))
        return f"{prefix}{n + 1:03d}"

    def find_open(self, task_id: str, code: str, phase: str) -> "Blocker | None":
        """An OPEN blocker for the same (task, code, phase), or None.

        Identity is the CONDITION, not the occurrence: the same task hitting
        the same failure in the same phase is one blocker seen repeatedly,
        which is what the operator needs to answer once. A resolved blocker
        is deliberately not matched — if the condition returns after being
        answered, that is genuinely new and deserves its own record."""
        for existing in self.open_blockers():
            if (existing.task_id, existing.code, existing.phase) == (task_id, code, phase):
                return existing
        return None

    def record(self, *, task_id, kind, code, question, detail, phase, now) -> "Blocker":
        """Idempotent upsert. Returns the existing open blocker for this
        condition with its recurrence count bumped, or a new one."""
        existing = self.find_open(task_id, code, phase)
        if existing is not None:
            from dataclasses import replace
            bumped = replace(
                existing,
                recurrences=existing.recurrences + 1,
                last_seen_at=now,
                question=question,
                detail=detail,
            )
            self.save(bumped)
            return bumped
        fresh = Blocker(
            id=self.next_id(task_id), task_id=task_id, kind=kind, code=code,
            question=question, detail=detail, phase=phase, created_at=now,
            last_seen_at=now,
        )
        self.save(fresh)
        return fresh

    def all_blockers(self) -> list[Blocker]:
        """Every blocker on disk, open or resolved, oldest id first per
        task (lexicographic filename order — ids are zero-padded, so this is
        also chronological within a task)."""
        if not self.directory.exists():
            return []
        loaded = [self.load(path.stem) for path in sorted(self.directory.glob("blk-*.json"))]
        return [b for b in loaded if b is not None]

    def open_blockers(self) -> list[Blocker]:
        return [b for b in self.all_blockers() if b.resolved_at is None]

    def resolve(self, blocker_id: str, answer: str) -> Blocker:
        """Mark `blocker_id` resolved with the operator's `answer`. Refuses
        an unknown id or one that is already resolved — resolution is a
        one-way, one-time action, never silently overwritten by a second
        `answer` call."""
        blocker = self.load(blocker_id)
        if blocker is None:
            raise StateError(f"no blocker with id '{blocker_id}'")
        if blocker.resolved_at is not None:
            raise StateError(
                f"blocker '{blocker_id}' is already resolved "
                f"(at {blocker.resolved_at}, answer: {blocker.answer!r})"
            )
        blocker.resolved_at = utcnow_iso()
        blocker.answer = answer
        self.save(blocker)
        return blocker
