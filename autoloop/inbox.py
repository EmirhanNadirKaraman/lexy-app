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

**Mutations (2026-08-16).** The vocabulary was `task` + `priority`. It is now
`task` plus six mutations: `priority`, `description`, `approved_paths`,
`depends_on`, `block` and `unblock`. Four things keep that from being the
"general edit-a-task request" the `priority`-only design was written to avoid:

1. **The registry decides, here and at creation.** Every mutation lands on a
   `TaskRegistry` method that calls the SAME validator creation calls —
   `_validate_description`, `_validate_approved_paths`, `_validate_depends_on`
   + `_check_acyclic`. Submission checks SHAPE only (is the field present, is
   it a list), so a refusal an operator reads is always the registry's own
   words, never a second rule set drifting from it.
2. **Nothing in flight can be edited.** `TaskRegistry._refuse_immutable`
   refuses `description`, `approved_paths` and `depends_on` on an
   `in_progress` task, because all three are what a dispatch that has ALREADY
   STARTED is judged against, and each one strands the round in a state no
   command can move it out of. It refuses `completed` and `retired` too:
   those are records, not queue.
3. **Blocking is reversible.** `block` goes through
   `TaskRegistry.operator_block`, which stamps the reason, and `unblock`
   through `operator_unblock`, which releases only what was stamped. Without
   the pair, an inbox block would be a one-way door: it creates no
   `blockers.Blocker` record, and `python -m autoloop answer` — the only
   route out of `blocked` — needs one. `retire` is deliberately NOT in the
   vocabulary for exactly that reason; it has no reverse at all, by design.
4. **Submission order is application order.** `drain` returns oldest-first and
   `apply_requests` makes ONE pass in that order, so two requests against the
   same field resolve last-write-wins and a mutation queued before its
   target's creation is refused rather than held back and retried later.

The widening this is honest about: an inbox request can now change what an
existing task is authorized to write, which `docs/SECURITY.md` S28 previously
recorded as impossible. It is recorded there rather than left implied.
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
        # Mutation-only: the account an operator gives for holding a task.
        # `Task.blocked_reason` on the registry side, but never settable at
        # creation — a task nobody has looked at yet cannot already be held.
        "reason",
    }
)
REQUIRED_FIELDS = ("id", "title", "description")

#: Request kinds. `task` creates a new task (the original shape; a request with
#: no `kind` is treated as one, so files written before this existed still
#: drain). The rest MUTATE an existing task, one field per kind.
#:
#: The original comment here said a `priority` request was "deliberately NOT a
#: general 'edit a task' request" because `description` and `approved_paths`
#: are authorization surface that belongs in a reviewable `plan` /
#: `seed_tasks.json`. That reasoning is kept, not deleted, because it names the
#: real hazard — but the conclusion has changed, and the module docstring above
#: says what replaced it. In short: the hazard was never "which field", it was
#: "edited against what". A scope rewritten while a dispatch is being judged
#: against it is the dangerous case, and `TaskRegistry._refuse_immutable`
#: refuses exactly that; a scope corrected on a task still sitting in the queue
#: is the ordinary operator action the loop had no route for at all.
#:
#: `retire` is NOT here and must not be added. It is written-once and has no
#: reverse by design (`TaskRegistry.retire`), so an inbox request that could
#: reach it would be the unblockable one-way state `block`/`unblock` are
#: shaped to avoid.
KIND_TASK = "task"
KIND_PRIORITY = "priority"
KIND_DESCRIPTION = "description"
KIND_APPROVED_PATHS = "approved_paths"
KIND_DEPENDS_ON = "depends_on"
KIND_BLOCK = "block"
KIND_UNBLOCK = "unblock"

#: kind -> the ONE payload field it carries besides `kind` and `id`. `None`
#: means the kind is the whole instruction (`unblock` names a task and says
#: "release it"; there is nothing else to say).
#:
#: This table IS the per-kind field rule: `submit` refuses anything outside
#: `{"kind", "id", <payload>}`, so a request naming a field its kind ignores is
#: reported at submit rather than silently dropped on merge — the same reason
#: `ALLOWED_FIELDS` exists. Note the payload name matches `Task`'s field name
#: wherever there is one, so an operator writing a mutation by hand does not
#: have to learn a second vocabulary.
MUTATION_PAYLOAD: dict[str, str | None] = {
    KIND_PRIORITY: "priority",
    KIND_DESCRIPTION: "description",
    KIND_APPROVED_PATHS: "approved_paths",
    KIND_DEPENDS_ON: "depends_on",
    KIND_BLOCK: "reason",
    KIND_UNBLOCK: None,
}
MUTATION_KINDS = tuple(MUTATION_PAYLOAD)
KINDS = (KIND_TASK, *MUTATION_KINDS)


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
        racing a submit can never observe a half-written file.

        SHAPE only — is this request even usable, given its kind. Whether the
        description is blank, the path is well-formed, the dependency exists or
        the task is in a state that may be edited are all the registry's calls,
        made on merge, so the operator reads one authority's words rather than
        two rule sets that agree until they don't.
        """
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
        if kind in MUTATION_PAYLOAD:
            self._check_mutation(kind, spec)
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

    @staticmethod
    def _check_mutation(kind: str, spec: dict) -> None:
        """Shape-check one mutation request. Raises `InboxError`.

        Driven off `MUTATION_PAYLOAD` rather than a branch per kind, so adding
        a kind cannot forget the "carries only these fields" rule — which is
        the check that turns a typo'd field name into a refusal at submit
        instead of a silently ignored instruction on merge.
        """
        if not str(spec.get("id", "")).strip():
            raise InboxError(f"a {kind} request needs the task 'id'")
        payload = MUTATION_PAYLOAD[kind]
        allowed = {"kind", "id"} | ({payload} if payload else set())
        extra = set(spec) - allowed
        if extra:
            carries = " + ".join(sorted(allowed - {"kind"}))
            raise InboxError(
                f"a {kind} request carries only {carries}; got {sorted(extra)}"
            )
        if payload is None:
            return
        if payload not in spec:
            raise InboxError(f"a {kind} request needs {payload!r}")
        value = spec[payload]
        # Type only, and only where JSON can express the wrong one. The
        # registry owns every question about CONTENT — blank text, malformed
        # paths, unknown dependencies — so nothing below asks one.
        if kind == KIND_PRIORITY and not isinstance(value, int):
            raise InboxError("a priority request needs an integer 'priority'")
        if kind in (KIND_DESCRIPTION, KIND_BLOCK) and not isinstance(value, str):
            raise InboxError(f"a {kind} request needs {payload!r} as a string")
        if kind in (KIND_APPROVED_PATHS, KIND_DEPENDS_ON) and not isinstance(value, list):
            raise InboxError(
                f"a {kind} request needs {payload!r} as a list (use [] to clear it)"
            )

    def submit_priority(self, task_id: str, priority: int) -> Path:
        """Re-prioritise an existing task. Same safety as `submit`: outside the
        checkout, no lock, safe mid-run."""
        return self.submit({"kind": KIND_PRIORITY, "id": task_id, "priority": priority})

    def submit_mutation(self, kind: str, task_id: str, value=None) -> Path:
        """Queue any mutation kind. Same safety as `submit`.

        One entry point rather than five near-identical `submit_*` helpers, so
        the payload field name is read from `MUTATION_PAYLOAD` — the same table
        `_check_mutation` uses — instead of being spelled out again per method
        and drifting from it. `submit_priority` keeps its own name because
        callers already use it.
        """
        if kind not in MUTATION_PAYLOAD:
            raise InboxError(
                f"unknown mutation kind {kind!r}; expected one of {list(MUTATION_KINDS)}"
            )
        spec = {"kind": kind, "id": task_id}
        payload = MUTATION_PAYLOAD[kind]
        if payload is not None:
            spec[payload] = value
        return self.submit(spec)

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


def _apply_mutation(registry, kind: str, spec: dict) -> str:
    """Apply ONE mutation and return the line reported for it.

    Raises whatever the registry raises — `apply_requests` owns the "never
    stop the batch" promise, and swallowing here would hide which request was
    refused and why.

    Every branch hands the operator's value to the registry as it arrived,
    unconverted. That is the point of "registry-derived refusal reasons": a
    `None` description or a string where a list belongs must be refused by
    `_validate_description` / `_validate_approved_paths` in their words, not
    coerced into something valid by a `str()`/`tuple()` on the way past.
    `priority` is the one exception, and only because `int()` was already
    there before mutations existed — narrowing it now would refuse the
    string-typed priorities the dashboard has always been able to queue.
    """
    task_id = str(spec.get("id", ""))
    if kind == KIND_PRIORITY:
        task = registry.set_priority(task_id, int(spec.get("priority", 100)))
        # Unchanged wording: this line is what the dashboard's operator has
        # been reading since priorities were the only mutation.
        return f"{task.id} -> {task.priority}"
    if kind == KIND_DESCRIPTION:
        task = registry.set_description(task_id, spec.get("description"))
        return f"{task.id} -> description ({len(task.description)} chars)"
    if kind == KIND_APPROVED_PATHS:
        task = registry.set_approved_paths(task_id, spec.get("approved_paths"))
        scope = ", ".join(task.approved_paths) or "(nothing — undispatchable)"
        return f"{task.id} -> approved_paths: {scope}"
    if kind == KIND_DEPENDS_ON:
        task = registry.set_depends_on(task_id, spec.get("depends_on"))
        deps = ", ".join(task.depends_on) or "(none)"
        return f"{task.id} -> depends_on: {deps}"
    if kind == KIND_BLOCK:
        task = registry.operator_block(task_id, spec.get("reason"))
        return f"{task.id} -> blocked: {task.blocked_reason}"
    task = registry.operator_unblock(task_id)
    return f"{task.id} -> pending (hold released)"


def apply_requests(registry, specs: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """Merge drained requests into `registry`. Returns
    `(added, applied, refused)`, all human-readable.

    THE single merge implementation, shared by the running loop
    (`Orchestrator._drain_task_inbox`) and the on-demand
    `python -m autoloop drain-inbox`. Two copies would drift, and a drift here
    means the same request behaves differently depending on who applied it —
    the same reasoning as `tasks.effective_approved_paths`.

    The middle bucket is every ACCEPTED MUTATION, not just re-prioritisations.
    It stays one bucket, and stays in second position, deliberately: both
    callers unpack three values positionally and both gate `task_store.save()`
    on `if added or <middle>` — a fourth bucket that either of them forgot to
    add to that condition would apply a mutation in memory and never persist
    it, which the next in-memory save silently overwrites. One bucket cannot
    be half-wired. Each line says what it did, so the report is readable
    whatever label a caller prints in front of it.

    Requests are applied in the order given, which `drain` guarantees is
    submission order, in a SINGLE pass. Two requests against one field are
    therefore last-write-wins, and a mutation queued before the task it names
    exists is refused rather than deferred — deferring would make the outcome
    depend on what else happened to be in the batch, and an operator can
    resubmit far more cheaply than they can reason about a retry queue.

    Never raises. The registry is the only validation authority, so a refused
    request is reported and dropped rather than aborting the batch: one
    operator typo must not stop a running loop, nor discard the fifteen good
    requests queued behind it.
    """
    from .errors import TaskGraphError
    from .tasks import Task

    added: list[str] = []
    applied: list[str] = []
    refused: list[str] = []
    for spec in specs:
        kind = spec.get("kind", KIND_TASK)
        # `isinstance` BEFORE the membership test, and not for tidiness: a
        # hand-written `"kind": []` makes `kind in MUTATION_PAYLOAD` raise
        # `TypeError: unhashable type` from OUTSIDE the try below, which would
        # break this function's never-raises promise and take the whole drain —
        # and the running loop's step — down with one malformed file.
        if not isinstance(kind, str) or kind not in KINDS:
            # Only reachable from a hand-written file: `submit` refuses an
            # unknown kind. Named rather than folded into the creation branch,
            # which would refuse it for whichever unrelated field it lacks.
            refused.append(
                f"{spec.get('id')}: unknown kind {kind!r}; expected one of {list(KINDS)}"
            )
            continue
        if kind in MUTATION_PAYLOAD:
            try:
                line = _apply_mutation(registry, kind, spec)
            except (TaskGraphError, ValueError, TypeError) as exc:
                refused.append(f"{spec.get('id')}: {exc}")
            else:
                applied.append(line)
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
    return added, applied, refused
