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
            if not task.title.strip() or not task.description.strip():
                raise TaskGraphError(
                    "empty_task_field", f"task '{task.id}' needs a title and a description"
                )
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

    def next_ready(self) -> Task | None:
        """First ready task in insertion order — deterministic."""
        ready = self.ready_tasks()
        return ready[0] if ready else None

    def summary(self) -> str:
        if not self._tasks:
            return "no tasks planned yet"
        counts = {state: 0 for state in TaskState}
        for task in self._tasks.values():
            counts[self.state_of(task.id)] += 1
        nxt = self.next_ready()
        parts = (
            f"{len(self._tasks)} tasks: {counts[TaskState.COMPLETED]} completed, "
            f"{counts[TaskState.IN_PROGRESS]} in progress, "
            f"{counts[TaskState.READY]} ready, {counts[TaskState.BLOCKED]} blocked, "
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
                Task(**{**raw, "depends_on": tuple(raw.get("depends_on", ()))})
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
