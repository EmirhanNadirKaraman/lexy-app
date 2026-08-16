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

1. **Two authorities, split by question, one implementation each.** SHAPE — is
   the field present, is it the right JSON type, does this kind even carry it —
   belongs to `check_request_shape` here. CONTENT — is the description blank,
   is the path well-formed, does the dependency exist, may this task be edited
   at all — belongs to the registry, whose mutators call the SAME validators
   creation calls (`_validate_description`, `_validate_approved_paths`,
   `_validate_depends_on` + `_check_acyclic`). So a refusal an operator reads is
   always one authority's own words, never a second rule set drifting from it.

   The shape rule is PER KIND, in both directions: `_check_mutation` bounds a
   mutation to `{"kind", "id", <its payload>}` and `_check_creation` bounds a
   `task` to `CREATION_FIELDS`. A single global field set cannot say this — it
   accepted `{"kind": "task", …, "reason": …}`, which submitted cleanly and
   then dropped the reason on merge, which is the silent-ignore the per-kind
   rule exists to prevent.

   And it runs at BOTH gates — `TaskInbox.submit` and `apply_requests` — off
   the one function, because hand-writing the JSON file is the ONLY operator
   route to the five new kinds (no CLI flag, no dashboard endpoint), and a
   hand-written file never passes through `submit`. Checking shape only on the
   way in therefore left the documented route unchecked: a hand-written
   creation carrying `reason`, or a `block` carrying a stray `approved_paths`,
   reached `apply_requests`, which consumed the fields it recognised and
   ignored the rest — the same silent drop, arrived at from the other side.
2. **Nothing in flight can be edited.** `TaskRegistry._refuse_immutable`
   refuses `description`, `approved_paths` and `depends_on` on an
   `in_progress` task, because all three are what a dispatch that has ALREADY
   STARTED is judged against, and each one strands the round in a state no
   command can move it out of. It refuses `completed` and `retired` too:
   those are records, not queue.
3. **Blocking is reversible, and only its own kind of block is.** `block` goes
   through `TaskRegistry.operator_block`, which records the hold's origin in
   `Task.hold_origin`, and `unblock` through `operator_unblock`, which releases
   only a task carrying that origin. Without the pair, an inbox block would be
   a one-way door: it creates no `blockers.Blocker` record, and
   `python -m autoloop answer` — the only route out of `blocked` — needs one.
   Provenance is a stored field and NOT the `blocked_reason` text, which
   loop-raised quarantines write too; reading it out of that text made a real
   quarantine releasable from here whenever its reason happened to start with
   `OPERATOR_HOLD_PREFIX`. `retire` is deliberately NOT in the vocabulary for
   the same family of reasons; it has no reverse at all, by design.
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

#: Fields a CREATION request (`kind: "task"`, or no kind at all) may carry.
#: Anything else is refused — at submit AND on merge, off the one
#: `check_request_shape` — rather than silently dropped, because a request that
#: names a field the receiver ignores has almost certainly not done what its
#: author intended.
#:
#: This is the whole of the creation contract: `reason` is deliberately absent,
#: because a task nobody has looked at yet cannot already be held, and
#: `apply_requests`' creation branch has no way to express it. Letting it
#: through here (which the first cut of the mutation vocabulary did, by
#: checking one global field set) accepted `{"kind": "task", …, "reason": …}`
#: at submit and then ignored the reason on merge — exactly the silent drop the
#: per-kind rule below refuses for every other field.
#:
#: Exactly the keys `apply_requests`' creation branch reads, plus the `kind`
#: discriminator itself — no more and no less. A field the merge consumes but
#: this omits would be unreachable, and a field this admits but the merge
#: ignores is the silent drop again.
#:
#: A STORED-TASK field is therefore refused rather than ignored: `status`,
#: `created_at`, `blocked_reason`, `superseded_by` and `hold_origin` are all
#: real `Task` attributes, but the inbox carries REQUESTS, not registry rows,
#: and none of them is something an operator gets to assert on the way in.
#: Since 2026-08-16 (review round 3) that refusal reaches the hand-written
#: route too, where such a key previously vanished and the task landed without
#: it — a `seed_tasks.json`-shaped file dropped into the inbox is now refused
#: with the offending key named instead of quietly meaning something else.
CREATION_FIELDS = frozenset(
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

#: Fields only a MUTATION request can carry, i.e. everything in
#: `MUTATION_PAYLOAD` that creation has no field for. Today just `reason` —
#: `Task.blocked_reason` on the registry side, the account an operator gives
#: for holding a task.
MUTATION_ONLY_FIELDS = frozenset({"reason"})

#: Every field name the vocabulary knows, across all kinds. A UNION, not a
#: contract: no single request may carry all of these, and nothing validates
#: against it (see `_check_creation` / `_check_mutation`, which each hold their
#: own kind's set). It exists because `dashboard.TASK_REQUEST_FIELDS`
#: documents itself as narrower than "the inbox's own allowed fields", and that
#: comparison needs something to point at.
ALLOWED_FIELDS = CREATION_FIELDS | MUTATION_ONLY_FIELDS
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
#: This table IS the per-kind field rule for mutations: `submit` refuses
#: anything outside `{"kind", "id", <payload>}`, so a request naming a field its
#: kind ignores is reported at submit rather than silently dropped on merge —
#: the same reason `CREATION_FIELDS` bounds the other kind. Note the payload
#: name matches `Task`'s field name wherever there is one, so an operator
#: writing a mutation by hand does not have to learn a second vocabulary.
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
    """A request whose SHAPE is unusable, whoever wrote it.

    Raised out of `TaskInbox.submit` (so an operator using the API sees it
    immediately, and nothing malformed reaches the queue) and caught inside
    `apply_requests` (so a hand-written file that never passed through `submit`
    is refused as that request's `refused` line, without stopping the batch).
    """


def check_request_shape(spec: object) -> str:
    """Shape-check ONE request and return its resolved kind. Raises `InboxError`.

    THE shape gate, called by `TaskInbox.submit` on the way in and by
    `apply_requests` on the way out, so a hand-written JSON file gets exactly
    the rule an API submit gets. Two copies would drift, and a drift here means
    the field an operator typed is refused by one route and silently ignored by
    the other — which is the whole defect the per-kind rule exists to prevent,
    and hand-writing the file is the ONLY route to five of the six mutation
    kinds today.

    SHAPE only, deliberately: is this request even usable, given its kind.
    Whether the description is blank, the path is well-formed, the dependency
    exists or the task is in a state that may be edited are all the registry's
    calls, made by the mutator on merge, so the operator reads one authority's
    words per question rather than two rule sets that agree until they don't.

    The allowed-field check is PER KIND, and the kind is therefore resolved
    first. A single global set cannot express the contract — `reason` is legal
    on a `block` and meaningless on a `task`, so a global check either refuses a
    valid hold or accepts a creation request carrying a field the merge
    silently ignores.
    """
    if not isinstance(spec, dict):
        raise InboxError("a task request must be a JSON object")
    kind = spec.get("kind", KIND_TASK)
    # `isinstance` BEFORE the membership test, and not for tidiness: a
    # hand-written `"kind": []` reaches `kind in MUTATION_PAYLOAD` below, which
    # is a dict lookup and raises `TypeError: unhashable type` rather than the
    # `InboxError` every caller here is written to expect — taking the whole
    # drain, and the running loop's step, down with one malformed file.
    if not isinstance(kind, str) or kind not in KINDS:
        raise InboxError(f"unknown kind {kind!r}; expected one of {list(KINDS)}")
    if kind in MUTATION_PAYLOAD:
        _check_mutation(kind, spec)
    else:
        _check_creation(spec)
    return kind


def _check_creation(spec: dict) -> None:
    """Shape-check one CREATION request. Raises `InboxError`.

    The creation half of the same per-kind rule `_check_mutation` applies:
    a request carries only the fields its kind can act on. `reason` is the
    field this exists to catch — it is in the vocabulary, so a global check
    waved it through onto a `task` request that then dropped it on merge.
    """
    unknown = set(spec) - CREATION_FIELDS
    if unknown:
        # A mutation-only field gets its own sentence. `{"kind": "task",
        # "reason": …}` is a request whose author meant a hold, and
        # "unknown field" alone would send them looking for a typo.
        mutation_only = sorted(unknown & MUTATION_ONLY_FIELDS)
        hint = (
            f"; {mutation_only} is mutation-only — use one of "
            f"{list(MUTATION_KINDS)}"
            if mutation_only
            else ""
        )
        carries = sorted(CREATION_FIELDS - {"kind"})
        raise InboxError(
            f"unknown field(s) {sorted(unknown)} on a {KIND_TASK} request; it "
            f"carries only {carries}{hint}"
        )
    if "priority" in spec and not isinstance(spec["priority"], int):
        raise InboxError("priority must be an integer (ascending; 1 outranks 2)")
    missing = [f for f in REQUIRED_FIELDS if not str(spec.get(f, "")).strip()]
    if missing:
        raise InboxError(f"missing required field(s): {', '.join(missing)}")


def _check_mutation(kind: str, spec: dict) -> None:
    """Shape-check one mutation request. Raises `InboxError`.

    Driven off `MUTATION_PAYLOAD` rather than a branch per kind, so adding
    a kind cannot forget the "carries only these fields" rule — which is
    the check that turns a typo'd field name into a refusal instead of a
    silently ignored instruction.
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

        Shape is checked by `check_request_shape`, the SAME function
        `apply_requests` runs on merge, so a request queued through this API and
        one an operator writes into the directory by hand are held to one rule.
        Raising here rather than queueing is the only difference: nothing
        malformed reaches the queue in the first place, so the operator finds
        out at the call instead of in a drain log later.

        Content — a blank description, a globbed path, an unknown dependency, a
        task the loop is currently running — is not asked about here at all.
        That is the registry's half of the split; see `check_request_shape`.
        """
        check_request_shape(spec)

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
    `priority`'s `int()` is the one conversion left, and it is now belt and
    braces rather than a compatibility shim: `_check_mutation` refuses a
    non-integer `priority` before this function runs, at BOTH gates since the
    shape check moved into `apply_requests`, and both dashboard endpoints
    coerce with `int()` of their own before queueing. It is kept because
    `set_priority` re-refuses a non-int anyway (`bad_priority`), so nothing
    reaches the registry unchecked either way — not because a string-typed
    priority is still expected to arrive.
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


def _request_id(spec: object) -> str:
    """The id `apply_requests` prints in front of a refusal.

    `str(spec.get("id"))` for a dict, which renders a missing id as `None`
    exactly as the f-string it replaces did — the refusal lines are unchanged.
    The non-dict arm exists for the never-raises promise alone: `drain` only
    ever yields dicts, but a caller passing something else must get a refusal
    rather than an `AttributeError` out of the id lookup itself.
    """
    return str(spec.get("id")) if isinstance(spec, dict) else repr(spec)


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

    **Shape is re-checked here, by the same `check_request_shape` `submit`
    calls.** Not belt-and-braces: hand-writing the JSON file is the documented —
    and today the only — operator route to five of the six mutation kinds, and
    such a file reaches this function without ever passing through `submit`.
    Without the re-check the loop below simply consumed the keys it recognised
    and ignored the rest, so a creation carrying `reason` and a `block`
    carrying a stray `approved_paths` both "succeeded" while doing something
    other than what their author wrote — the exact silent-ignore the per-kind
    rule exists to prevent, reached from the side that has no gate.

    Never raises, and that governs how the re-check is wired: an `InboxError`
    becomes THAT request's `refused` line and the batch continues. The two
    authorities keep their halves — shape is the inbox's answer, content the
    registry's — and a refusal of either kind is reported and dropped rather
    than aborting the pass: one operator typo must not stop a running loop, nor
    discard the fifteen good requests queued behind it.
    """
    from .errors import TaskGraphError
    from .tasks import Task

    added: list[str] = []
    applied: list[str] = []
    refused: list[str] = []
    for spec in specs:
        try:
            # The RESOLVED kind, taken from the checker rather than re-read off
            # the spec: two reads would be a second source of truth for the one
            # thing this call exists to centralise. It also means the unknown/
            # unhashable-kind cases are named here as such, instead of falling
            # through to the creation branch and being refused for whichever
            # unrelated field they happen to lack.
            kind = check_request_shape(spec)
        except InboxError as exc:
            refused.append(f"{_request_id(spec)}: {exc}")
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
