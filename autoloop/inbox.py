"""Operator task inbox — add or reprioritise work while the loop is running.

**Why a queue and not a direct edit.** The obvious way to add a task is to edit
`.autoloop/tasks.json`. Two things break that:

1. **The escape detector snapshots it.** `escape_detector.enumerate_checkout_
   paths` covers tracked, untracked AND ignored paths, so `.autoloop/` is
   inside the before/after snapshot taken around every write-capable agent
   call. An operator edit landing mid-execute is indistinguishable from an
   agent writing outside its worker repo, and parks the loop LOOP-FATAL. That
   coverage is not an oversight: `tasks.json` holds `approved_paths`, so an
   agent that could edit it undetected could widen its own authorization —
   exactly the circular ownership `docs/SECURITY.md` finding #2 closes. The
   fix is therefore NOT to exclude the state dir from the snapshot.
2. **Lost updates.** The running orchestrator holds the registry in memory and
   saves it on task-graph changes. An external edit can be silently overwritten
   by the next save, and the single-instance lock exists precisely to stop
   concurrent mutation.

So this inbox lives OUTSIDE the repository (beside `workers_root`, which is
already required to be external), carries REQUESTS rather than registry state,
and is drained by the loop itself at a safe point between steps. The loop
remains the only writer of `tasks.json`, under its own lock.

A request is a plain JSON object with the same shape `seed_tasks.json` uses.
Nothing here validates the task graph — `TaskRegistry.add_many` does that on
merge, so a bad request is refused by the same gate a ChatGPT `plan` goes
through, not by a second implementation that could drift from it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

#: Fields a request may carry. Anything else is refused at submit time rather
#: than silently dropped on merge — a request that names a field the registry
#: ignores has almost certainly not done what its author intended.
ALLOWED_FIELDS = frozenset(
    {
        "kind",
        "id",
        "title",
        "description",
        "depends_on",
        "priority",
        "validation",
        "validation_cwd",
        "approved_paths",
    }
)
REQUIRED_FIELDS = ("id", "title", "description")

#: Request kinds. `task` creates a new task (the original shape; a request
#: with no `kind` is treated as one, so files written before this existed
#: still drain). `priority` re-prioritises an EXISTING task and carries only
#: an id and a number — deliberately NOT a general "edit a task" request:
#: description and, above all, `approved_paths` are authorization surface, and
#: an operator changing those should go through `plan`/`seed_tasks.json` where
#: it is reviewable, not through a form on a localhost page.
KIND_TASK = "task"
KIND_PRIORITY = "priority"
KINDS = (KIND_TASK, KIND_PRIORITY)


class InboxError(Exception):
    """A request that cannot even be written — bad shape, caught at submit."""


class TaskInbox:
    """A directory of pending task requests, outside the checkout.

    `submit` is safe to call at ANY time, including while a write-capable
    agent is mid-run: nothing here touches the repository or the state dir.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    # ---- operator side ------------------------------------------------------

    def submit(self, spec: dict) -> Path:
        """Write one request. Atomic (temp file + `os.replace`), so a drain
        racing a submit can never observe a half-written file."""
        if not isinstance(spec, dict):
            raise InboxError("a task request must be a JSON object")
        unknown = set(spec) - ALLOWED_FIELDS
        if unknown:
            raise InboxError(
                f"unknown field(s) {sorted(unknown)}; allowed: {sorted(ALLOWED_FIELDS)}"
            )
        kind = spec.get("kind", KIND_TASK)
        if kind not in KINDS:
            raise InboxError(f"unknown kind {kind!r}; expected one of {list(KINDS)}")
        if "priority" in spec and not isinstance(spec["priority"], int):
            raise InboxError("priority must be an integer (ascending; 1 outranks 2)")
        if kind == KIND_PRIORITY:
            if not str(spec.get("id", "")).strip():
                raise InboxError("a priority request needs the task 'id'")
            if not isinstance(spec.get("priority"), int):
                raise InboxError("a priority request needs an integer 'priority'")
            extra = set(spec) - {"kind", "id", "priority"}
            if extra:
                raise InboxError(
                    f"a priority request carries only id + priority; got {sorted(extra)}"
                )
        else:
            missing = [f for f in REQUIRED_FIELDS if not str(spec.get(f, "")).strip()]
            if missing:
                raise InboxError(f"missing required field(s): {', '.join(missing)}")

        self.directory.mkdir(parents=True, exist_ok=True)
        # Lexicographic filename order MUST equal submission order — `drain`
        # promises it, and with equal priorities it decides which task the loop
        # picks first. The readable UTC stamp alone is not enough (two submits
        # in the same second tie), and a `monotonic_ns() % N` tiebreaker WRAPS,
        # which sorted a later request first — caught by the round-trip test.
        # A zero-padded `time_ns()` is fixed-width, so string order is
        # chronological order; the pid only disambiguates two processes landing
        # in the same nanosecond.
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        name = f"{stamp}-{time.time_ns():019d}-{os.getpid()}.json"
        path = self.directory / name
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return path

    def submit_priority(self, task_id: str, priority: int) -> Path:
        """Re-prioritise an existing task. Same safety as `submit`: outside the
        checkout, no lock, safe mid-run."""
        return self.submit({"kind": KIND_PRIORITY, "id": task_id, "priority": priority})

    # ---- loop side ----------------------------------------------------------

    def pending(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return sorted(p for p in self.directory.glob("*.json") if p.is_file())

    def drain(self) -> tuple[list[dict], list[str]]:
        """Every pending request, oldest first, removed from the inbox.

        Returns `(specs, problems)`. A file that will not parse is MOVED to a
        `rejected/` sibling rather than deleted or left in place: leaving it
        would re-fail on every drain forever, and deleting it would destroy
        what the operator wrote. Returning problems instead of raising is the
        point — one malformed request must never stop a running loop.
        """
        specs: list[dict] = []
        problems: list[str] = []
        for path in self.pending():
            try:
                spec = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(spec, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as exc:
                problems.append(f"{path.name}: {exc}")
                self._reject(path)
                continue
            specs.append(spec)
            path.unlink(missing_ok=True)
        return specs, problems

    def _reject(self, path: Path) -> None:
        rejected = self.directory / "rejected"
        rejected.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(path, rejected / path.name)
        except OSError:  # pragma: no cover - best effort; never break the loop
            pass


def inbox_dir_for(workers_root: Path | None, state_dir: Path) -> Path:
    """Where the inbox lives: beside `workers_root`, which is already required
    to be an absolute path outside the checkout, its `.git`, the state dir and
    the publisher paths (`worker_env.validate_workers_root`). Sharing that
    guarantee is why the inbox is placed there rather than under `state_dir`,
    which IS inside the snapshotted tree.

    Falls back to `state_dir/inbox` only when no `workers_root` is configured —
    a configuration `load_config` already refuses for any real run, so this
    branch exists for hand-built configs in tests rather than production.
    """
    if workers_root is not None:
        return Path(workers_root).expanduser().parent / "inbox"
    return Path(state_dir) / "inbox"


def apply_requests(registry, specs: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Merge drained requests into `registry`. Returns
    `(added, reprioritised, refused)`, all human-readable.

    THE single merge implementation, shared by the running loop
    (`Orchestrator._drain_task_inbox`) and the on-demand
    `python -m autoloop drain-inbox`. Two copies would drift, and a drift here
    means the same request behaves differently depending on who applied it —
    the same reasoning as `tasks.effective_approved_paths`.

    Never raises. `TaskRegistry.add_many` is the only validation authority, so
    a refused request is reported and dropped rather than aborting the batch:
    one operator typo must not stop a running loop, nor discard the fifteen
    good requests queued behind it.
    """
    from .errors import TaskGraphError
    from .tasks import Task

    added: list[str] = []
    reprioritised: list[str] = []
    refused: list[str] = []
    for spec in specs:
        if spec.get("kind") == KIND_PRIORITY:
            try:
                task = registry.set_priority(
                    str(spec.get("id", "")), int(spec.get("priority", 100))
                )
            except (TaskGraphError, ValueError, TypeError) as exc:
                refused.append(f"{spec.get('id')}: {exc}")
            else:
                reprioritised.append(f"{task.id} -> {task.priority}")
            continue
        try:
            task = Task(
                id=str(spec.get("id", "")),
                title=str(spec.get("title", "")),
                description=str(spec.get("description", "")),
                depends_on=tuple(spec.get("depends_on", ()) or ()),
                priority=int(spec.get("priority", 100)),
                validation=tuple(tuple(c) for c in spec.get("validation", ()) or ()),
                validation_cwd=str(spec.get("validation_cwd", "") or ""),
                approved_paths=tuple(spec.get("approved_paths", ()) or ()),
            )
            # One at a time: `add_many` is atomic per call, so batching would let
            # one bad request reject every good one queued alongside it.
            registry.add_many([task])
        except (TaskGraphError, ValueError, TypeError) as exc:
            refused.append(f"{spec.get('id')}: {exc}")
        else:
            added.append(f"{task.id} (priority {task.priority})")
    return added, reprioritised, refused
