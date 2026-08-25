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
   fix is therefore NOT to exclude the state dir from the snapshot. (Still
   true. The one immediate write that exists today is distinguished by an
   ATTESTATION outside the checkout plus a check of what actually changed —
   never by the path being `tasks.json`; see the note below.)
2. **Lost updates.** The running orchestrator holds the registry in memory and
   saves it on task-graph changes. An external edit can be silently overwritten
   by the next save, and the single-instance lock exists precisely to stop
   concurrent mutation.

So this inbox lives OUTSIDE the repository (beside `workers_root`, which is
already required to be external), carries REQUESTS rather than registry state,
and is drained by the loop itself at a safe point between steps.

**One exception since 2026-08-16, and only one: `priority`.** The dashboard now
writes that field straight into `tasks.json` (`TaskStore.apply_priority`)
instead of queueing it, so the loop is no longer the *only* writer of the
registry — it is the only writer of everything except this one integer. Neither
of the two reasons above was waved away to get there; each was answered:

1. The detector still snapshots `tasks.json`, and `.autoloop/` is still not
   excluded. The immediate write is ATTESTED in a ledger beside `workers_root`
   — outside the checkout, exactly where this inbox lives and for exactly the
   same reason — as an INTENT before the write and a COMPLETE after it, and the
   loop's exemption silences the change only when this window's COMPLETE records
   for that file chain unbroken to exactly the state it observed, and the bytes
   say nothing but `priority` moved. An agent editing that file still parks the
   loop loop-fatal — including one that reproduces a state an earlier legitimate
   edit merely passed through or announced — so the circular-ownership property
   is intact (`tasks.MutationLedger`,
   `orchestrator._operator_priority_exemption`).
2. Lost updates are answered by a lock, just not the run-level one. A
   fine-grained mutex (`tasks.task_file_mutex`) serialises load/mutate/save for
   BOTH writers, and `TaskStore.save` reconciles the on-disk priority into the
   loop's in-memory registry so a round that started before the edit cannot
   write the stale value back.

Why `priority` and nothing else: it is the field an operator uses to steer what
runs next, so a change that lands whenever the loop next drains has already
missed the decision it was for — and it is safe to change mid-flight precisely
because nothing already dispatched depends on it. Creation still queues here.
`KIND_PRIORITY` also stays: a request written into this directory by hand is
still drained and applied, and takes precedence over the file's value when it
is (see `TaskRegistry.priority_overrides`).

A request is a plain JSON object with the same shape `seed_tasks.json` uses.
Nothing here validates the task graph — `TaskRegistry.add_many` does that on
merge, so a bad request is refused by the same gate a ChatGPT `plan` goes
through, not by a second implementation that could drift from it.

**Mutations (2026-08-16; `urgent` added 2026-08-22, `shipped_elsewhere`
2026-08-23).** The vocabulary was `task` + `priority`. It is now `task` plus
eight mutations: `priority`, `description`, `approved_paths`, `depends_on`,
`block`, `unblock`, `urgent` and `shipped_elsewhere`. Four things keep that from
being the "general edit-a-task request" the `priority`-only design was written
to avoid:

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
   route to most of the mutation kinds (no CLI flag, no dashboard endpoint —
   `priority` has both and `shipped_elsewhere` has `python -m autoloop
   record-shipped`), and a hand-written file never passes through `submit`.
   Checking shape only on the
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
5. **`urgent` is single-slot and never last-write-wins.** It is the one kind
   whose effect is on the LOOP rather than on the task graph — it displaces the
   round in flight — so the rule in point 4 is deliberately not applied to it:
   `TaskRegistry.request_urgent` REFUSES a second request while another task's
   pin is still waiting for its dispatch, naming the incumbent, instead of
   overwriting it. Overwriting would let the second operator discard the
   displaced round the first one paid for without ever seeing that they had.
   It still decides nothing about timing: the pin is a fact in the registry,
   and `orchestrator._preempt_for_urgent` is what acts on it, only at a phase
   boundary the loop already treats as safe.

The widening this is honest about: an inbox request can now change what an
existing task is authorized to write, which `docs/SECURITY.md` S28 previously
recorded as impossible. It is recorded there rather than left implied.

**Operator intake lives at the bottom of this file** (intake-02, 2026-08-25):
a rough idea becomes a DRAFT through a question-and-answer exchange held in a
markdown file, and that draft reaches the registry only when the operator runs
`submit_draft`, which calls `TaskInbox.submit` above and nothing else. It adds
no request kind and no second submission route — see the banner comment there
for why it is in this module and why it is a file rather than a chat session.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, replace
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
#: `MUTATION_PAYLOAD` that creation has no field for. Two of them:
#:
#:   * `reason` — `Task.blocked_reason` on the registry side, the account an
#:     operator gives for holding a task (and for pinning one urgent).
#:   * `shipped_elsewhere` — the evidence payload of `KIND_SHIPPED_ELSEWHERE`
#:     (ship-01, 2026-08-23). Creation has no field for it and must not grow
#:     one: a task cannot be born already recorded as shipped under someone
#:     else's commits, and the registry refuses the record on anything but a
#:     pending or held task anyway.
#:
#: Membership here is what keeps `ALLOWED_FIELDS` the whole vocabulary
#: (`test_no_payload_field_falls_outside_both_per_kind_sets` is the drift
#: guard), and it earns the payload name a better refusal on the wrong kind:
#: `_check_creation` reports a mutation-only field as such and names the kinds
#: that take it, instead of "unknown field", which sends the author looking for
#: a typo. Nothing validates AGAINST this set — the per-kind checks do — so
#: adding a name here widens no request's shape.
MUTATION_ONLY_FIELDS = frozenset({"reason", "shipped_elsewhere"})

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
#: Make an existing task the loop's URGENT TARGET — the next task dispatched,
#: ahead of whatever round is in flight. Carries a `reason`, like `block`.
#:
#: The one mutation whose effect is on the LOOP rather than on the task graph,
#: and it is still a mutation of one field (`Task.urgent_at`) on one existing
#: task, applied through the registry by the same drain as every other kind.
#: Nothing here decides when the loop acts on it — see
#: `TaskRegistry.request_urgent` for what may hold the pin and
#: `orchestrator._preempt_for_urgent` for the safe boundary at which it is
#: acted on.
#:
#: A separate kind rather than `{"kind": "priority", "priority": 0}` because
#: priority CANNOT preempt: it only orders the next selection among READY
#: tasks, so a task already in flight is untouchable by it, and a P0 request
#: merely ties with every other P0 (measured 2026-08-21 — codex-01 lost the id
#: tiebreak to a P0 task already mid-round and nothing said so). Keeping the
#: two kinds apart also keeps the ordinary re-prioritisation harmless: it
#: cannot displace a round by accident.
KIND_URGENT = "urgent"
KIND_DESCRIPTION = "description"
KIND_APPROVED_PATHS = "approved_paths"
KIND_DEPENDS_ON = "depends_on"
KIND_BLOCK = "block"
KIND_UNBLOCK = "unblock"
#: Record that an existing task's work is already in the base under ANOTHER
#: task's commits (ship-01, 2026-08-23). Carries the EVIDENCE — the carrying
#: commits and a note saying whose they are — as one payload object.
#:
#: Here rather than only behind a `.autoloop/` edit because writing under
#: `.autoloop/` requires the loop STOPPED (the escape detector snapshots that
#: directory — see the module docstring), and the whole point of this record is
#: that it can be made about a live roadmap. The inbox is the attested path for
#: everything except `priority`, so this uses it rather than growing a second
#: immediate writer.
#:
#: A composite payload, unlike every other kind, and deliberately so: the two
#: halves are one fact and a request carrying shas with no account of them — or
#: an account with no shas — is exactly the unsupported assertion this record
#: exists to replace. `MUTATION_PAYLOAD`'s one-field-per-kind invariant is
#: unchanged: the field is one field, and its INSIDE is shape-checked by
#: `_check_shipped_elsewhere` with the same "carries only these keys" rule the
#: outer request gets.
#:
#: The registry re-checks everything here (`record_shipped_elsewhere`), and
#: nothing on this route asserts ANCESTRY: git is asked by
#: `cli._cmd_record_shipped` before the request is queued and again by every
#: reader afterwards. A hand-written request that skips that command still lands
#: — and a claim whose commits are not ancestors then reads as a disagreement on
#: the dashboard, which is the point of re-checking rather than trusting.
KIND_SHIPPED_ELSEWHERE = "shipped_elsewhere"

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
    KIND_URGENT: "reason",
    KIND_SHIPPED_ELSEWHERE: "shipped_elsewhere",
}

#: The keys the `shipped_elsewhere` payload object carries — BOTH required,
#: nothing else accepted. The per-kind field rule applied one level down, for
#: the same reason it exists one level up: a request naming a key its receiver
#: ignores has almost certainly not done what its author intended, and here the
#: silently-dropped key would be half the evidence.
SHIPPED_ELSEWHERE_KEYS = ("commits", "note")
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
    and hand-writing the file is the ONLY route to most of the mutation
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
    if kind in (KIND_DESCRIPTION, KIND_BLOCK, KIND_URGENT) and not isinstance(value, str):
        raise InboxError(f"a {kind} request needs {payload!r} as a string")
    if kind in (KIND_APPROVED_PATHS, KIND_DEPENDS_ON) and not isinstance(value, list):
        raise InboxError(
            f"a {kind} request needs {payload!r} as a list (use [] to clear it)"
        )
    if kind == KIND_SHIPPED_ELSEWHERE:
        _check_shipped_elsewhere(value)


def _check_shipped_elsewhere(value: object) -> None:
    """Shape-check the `shipped_elsewhere` payload object. Raises `InboxError`.

    TYPE and PRESENCE only, like every other arm above — whether a sha is
    well-formed, whether the note is blank, and whether the task may carry the
    record at all are the registry's calls (`_validate_shipped_commits`,
    `_validate_shipped_note`, `record_shipped_elsewhere`), so the operator reads
    one authority's words per question.

    What it does own is the composite: this is the only kind whose payload is an
    object, so "is it an object, and does it carry only the two keys" has no
    other home. Both keys are REQUIRED rather than defaulted: a request with
    commits and no note, or a note and no commits, is half a record, and the
    half that is missing is the half that makes the other half checkable.
    """
    if not isinstance(value, dict):
        raise InboxError(
            f"a {KIND_SHIPPED_ELSEWHERE} request needs {KIND_SHIPPED_ELSEWHERE!r} "
            f"as an object carrying {list(SHIPPED_ELSEWHERE_KEYS)}"
        )
    extra = sorted(set(value) - set(SHIPPED_ELSEWHERE_KEYS))
    if extra:
        raise InboxError(
            f"a {KIND_SHIPPED_ELSEWHERE} payload carries only "
            f"{list(SHIPPED_ELSEWHERE_KEYS)}; got {extra}"
        )
    missing = [key for key in SHIPPED_ELSEWHERE_KEYS if key not in value]
    if missing:
        raise InboxError(
            f"a {KIND_SHIPPED_ELSEWHERE} payload needs {missing} — the commits "
            "are the evidence and the note says whose they are; one without the "
            "other is not a record"
        )
    if not isinstance(value["commits"], list):
        raise InboxError(
            f"a {KIND_SHIPPED_ELSEWHERE} payload needs 'commits' as a list of "
            "full commit shas"
        )
    if not isinstance(value["note"], str):
        raise InboxError(f"a {KIND_SHIPPED_ELSEWHERE} payload needs 'note' as a string")


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
    if kind == KIND_URGENT:
        task = registry.request_urgent(task_id, spec.get("reason"))
        # Says what will happen next, because "urgent" on its own reads as a
        # flag rather than as an act: this request ends the round in flight at
        # the loop's next safe boundary and returns that task to pending.
        return (
            f"{task.id} -> URGENT (requested {task.urgent_at}: "
            f"{task.urgent_reason}); the loop preempts at its next safe phase "
            "boundary and dispatches this task"
        )
    if kind == KIND_SHIPPED_ELSEWHERE:
        # `or {}` for the never-raises promise alone: `check_request_shape` has
        # already refused a non-object payload at BOTH gates, so the fallback
        # can only be reached by a caller bypassing them — and it then hands the
        # registry an empty commit list, which `_validate_shipped_commits`
        # refuses in its own words rather than an `AttributeError` out of a
        # `.get` on a string.
        payload = spec.get(KIND_SHIPPED_ELSEWHERE)
        payload = payload if isinstance(payload, dict) else {}
        task = registry.record_shipped_elsewhere(
            task_id, payload.get("commits", ()), payload.get("note")
        )
        landed = ", ".join(sha[:12] for sha in task.shipped_commits)
        # Says what the record CLAIMS and where to check it, never that the
        # claim was verified here: nothing on the drain path talks to git.
        return (
            f"{task.id} -> shipped elsewhere under {landed} ({task.shipped_note}); "
            "ancestry is re-checked on every read — `python -m autoloop "
            "shipped-report` names it if the evidence stops holding"
        )
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
    and for most kinds today the only — operator route to a mutation, and
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


# ===========================================================================
# OPERATOR INTAKE — a rough idea, turned into a DRAFT by question and answer.
# ===========================================================================
#
# Everything below runs at AUTHORING TIME and produces a FILE. It never
# dispatches, never writes the registry, and reaches the queue only through
# `TaskInbox.submit` above — the same gate `add-task` and the dashboard form
# already use, called by `submit_draft` and by nothing else here.
#
# **Why this lives in `inbox.py`.** Its entire output is an inbox creation
# request, and the one rule this module exists to keep single is "a request is
# shape-checked by exactly one function". Putting the draft → request
# conversion one function away from `check_request_shape` is what stops a
# second, drifting copy of that rule appearing behind a friendlier front door.
#
# **Why a file and not a chat session.** There is no session state, so nothing
# to resume and nothing to lose: an operator who starts describing an idea and
# walks away has created a file in a directory nothing reads, which is what
# makes "an abandoned exchange leaves nothing behind" true by construction
# rather than by cleanup code. It is asynchronous (three answers now, two
# tomorrow), the artifact IS the task description so no meaning drifts in
# translation, and it is what makes ONE path possible — the CLI, a dropped-in
# `.md`/`.txt` and the dashboard all simply WRITE THIS FILE.
#
# **`ask_user` is the thing this must not become.** That verb was retired
# (`policy._RETIRED_DENIALS`) because it parked the loop on a question
# addressed to a human who, in an autonomous run, is not there to answer it.
# Intake is the opposite situation — the operator is present by definition,
# that is what makes it intake — so `refuse_if_round_running` refuses every
# question-asking entry point while a round is live, and fails CLOSED when it
# cannot tell. A question asked mid-round rebuilds `ask_user` under a new name.
#
# **The one place an LLM is appropriate here**, and it is worth being explicit
# because `path_suggest` deliberately is not one. Good clarifying questions
# cannot be derived mechanically. The safety comes from somewhere else instead:
# the output is a DRAFT the operator reads and edits, and nothing reaches the
# registry without that. Explainability is the human reading it, not the
# derivation. Note what the model is NOT allowed to contribute: the two
# questions only the operator can answer are CONSTANTS here, the evidence is
# read off git by `path_suggest`, and `approved_paths` is mechanical — so a
# provider that is down, throttled or terse degrades the interview and cannot
# fabricate any of the three things a reviewer would later believe.

#: Marks where the operator's own prose ends and the exchange begins. Every
#: reader here splits on it, so text ABOVE it is never rewritten by a pass:
#: the idea stays exactly as it was typed.
INTAKE_MARKER = "<!-- autoloop:intake v1 -->"

#: What a dropped-in idea file may be. Narrow on purpose — this is text an
#: operator wrote, and accepting `.py` or `.json` would mean adopting something
#: that is almost certainly not an idea.
INTAKE_SUFFIXES = (".md", ".txt")

#: A draft's slug, which becomes `<slug>.md` inside the intake directory. The
#: character class is the whole containment: no separator, no `..`, no leading
#: dot, so a slug arriving from an HTTP body cannot address a file outside the
#: intake directory however it is spelled.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: The two questions the SYSTEM CANNOT ANSWER, asked verbatim on the first
#: pass and required before any draft is emitted. They are constants rather
#: than model output precisely because a provider that is unavailable must not
#: be able to make a draft look complete — see `draft_blockers`.
REQUIRED_QUESTIONS = (
    "What would prove this worked? Name the ONE claim a reviewer can check.",
    "What would make it fail? Name the constraints that must not break.",
)

#: Batch size. Three to five at a time: dripping one question per pass turns an
#: afternoon's authoring into a week's, and twenty at once is not answered at
#: all. The required questions above count towards the same batch.
MAX_QUESTIONS_PER_PASS = 5

#: How many things phase 1 offers. Two or three, never a list — choosing
#: between options is a far easier act than authoring, and a list of forty
#: is authoring again with extra steps.
MAX_WORK_SUGGESTIONS = 3

_H_QUESTIONS = "## Questions"
_H_EVIDENCE = "## Evidence"
_H_ASSUMPTIONS = "## Assumptions"
_H_DRAFT = "## Draft"

#: A question line in the file: `?` for one you may leave blank, `?!` for one
#: only you can answer. The answer is whatever follows the FIRST `->`.
_QUESTION_RE = re.compile(r"^\?(!?)[ \t]*(.*)$")
#: A question in a MODEL REPLY. Tolerates the bullet and numbering a model
#: reaches for anyway; still requires the `?` marker, so ordinary prose in the
#: reply is not harvested as a question.
_REPLY_QUESTION_RE = re.compile(r"^[ \t]*(?:[-*•][ \t]*|\d+[.)][ \t]*)?\?[ \t]*(.+)$")
_SOURCE_RE = re.compile(r"[ \t]*\(source:[ \t]*(.+?)\)[ \t]*$")
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_TASK_HEAD_RE = re.compile(r"^###[ \t]+task:[ \t]*(.*)$")
#: A finding heading in a rendered audit report:
#: `#### db_migrations:db-01 — Author a new baseline migration …`
_AUDIT_FINDING_RE = re.compile(
    r"^####[ \t]+([A-Za-z0-9_]+:[A-Za-z0-9_.\-]+)[ \t]+[—–-][ \t]*(.+?)[ \t]*$"
)


class IntakeError(Exception):
    """An intake action that cannot proceed, in words for the operator.

    Separate from `InboxError`, which is about the SHAPE of a queued request.
    Everything raised here is about the exchange — an unreadable draft, a draft
    that is not ready, a round in flight, a provider that did not answer.
    """


# ---- the draft file: parse, render, round-trip -----------------------------


@dataclass(frozen=True)
class IntakeQuestion:
    """One question and, once the operator types one, its answer.

    `required` is the whole safety distinction. A blank answer to an OPTIONAL
    question is a legitimate answer meaning "you decide" or "not yet", and the
    system proceeds while SAYING what it assumed. A blank answer to a REQUIRED
    one is not: the task text is explicit that only the operator knows what
    they wanted, so assuming there would fabricate the provable claim — the
    thing that is then quoted back in the spec and believed.
    """

    text: str
    answer: str = ""
    required: bool = False

    @property
    def answered(self) -> bool:
        return bool(self.answer.strip())


@dataclass(frozen=True)
class Evidence:
    """Something the system ACTUALLY READ, with the source named.

    `source` is not decoration: the constraint is "do not invent measurements",
    and a claim an operator cannot go and check is exactly the fabricated count
    that is worse than no count. Every producer here names a real reader
    (`git ls-files`, a report path, `tasks.json`), and nothing a model said
    ever becomes an `Evidence` — see `_questions_from_reply`.
    """

    text: str
    source: str


@dataclass(frozen=True)
class DraftTask:
    """One proposed task — the part of the draft that is not derived.

    `approved_paths` entries may carry a trailing `  # reason`, exactly as the
    dashboard's Detect-paths button appends one, and `submit_draft` strips it
    again: the operator reads reasons, the registry validates paths.
    """

    id: str
    title: str
    priority: int = 100
    depends_on: tuple[str, ...] = ()
    approved_paths: tuple[str, ...] = ()
    #: Only set for a task that came out of a `plan` pass; a single-task draft
    #: derives its whole description from the artifact.
    description: str = ""


@dataclass(frozen=True)
class IntakeDraft:
    slug: str
    idea: str
    questions: tuple[IntakeQuestion, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    assumptions: tuple[str, ...] = ()
    tasks: tuple[DraftTask, ...] = ()

    @property
    def title(self) -> str:
        """The first non-blank line of the idea, as a task title."""
        for line in self.idea.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:120]
        return ""


def _normalize(text: str) -> str:
    """Text reduced to what two spellings of the same question share.

    Used for one job only — deciding whether a question has already been
    asked — so it is deliberately aggressive: punctuation, case and spacing
    all vanish. A model that returns our own question back with a comma moved
    must not add it a second time.
    """
    return _NORMALIZE_RE.sub(" ", text.lower()).strip()


def _split_marker(text: str) -> tuple[str, list[str]]:
    """`(idea, exchange_lines)`.

    A file with no marker is ALL idea — that is what a `.md` an operator wrote
    by hand looks like before the first pass touches it, and reading it as an
    idea is the only reading that does not throw their text away.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == INTAKE_MARKER:
            return "\n".join(lines[:i]).strip("\n"), lines[i + 1:]
    return text.strip("\n"), []


def parse_draft(text: str, slug: str = "") -> IntakeDraft:
    """Read a draft file. Never raises on content — an operator edits this by
    hand and half of them will be mid-edit.

    **Written for a HAND-MANGLED file, because that is the normal one.** The
    operator deletes the `->`, answers on the next line, reflows a long answer
    across three, pastes something with tabs. So: an answer is whatever follows
    the first `->` on the question's own line PLUS every following line in the
    section that is not itself a question, a heading or a comment. Losing an
    answer is the one failure this parser must not have — it is the operator's
    words, and nothing else in the flow can reconstruct them.
    """
    idea, lines = _split_marker(text)
    questions: list[IntakeQuestion] = []
    evidence: list[Evidence] = []
    assumptions: list[str] = []
    tasks: list[DraftTask] = []
    section = ""
    pending_paths = False
    description: list[str] | None = None

    def flush_description() -> None:
        nonlocal description
        if description is not None and tasks:
            tasks[-1] = replace(tasks[-1], description="\n".join(description).strip("\n"))
        description = None

    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("## "):
            flush_description()
            pending_paths = False
            section = stripped
            continue
        if section.startswith(_H_DRAFT) and description is not None:
            # Inside a `description:` block: an indented line continues it, and
            # anything at column zero ends it. Checked BEFORE the comment and
            # blank-line skips below, so a description may contain either.
            if not raw.strip() or raw[:1] in (" ", "\t"):
                description.append(raw[2:] if raw.startswith("  ") else raw.lstrip())
                continue
            flush_description()
        if stripped.startswith("<!--"):
            continue
        if section.startswith(_H_QUESTIONS):
            match = _QUESTION_RE.match(raw)
            if match:
                body = match.group(2)
                # The FIRST `->`, not the last: an ANSWER containing an arrow
                # is far likelier than a question containing one (nothing this
                # module writes ever does — `_questions_from_reply` rewrites a
                # model's `->` to a real arrow for exactly this reason), and
                # splitting on the last would eat the front of such an answer.
                head, sep, tail = body.partition("->")
                text_part = (head if sep else body).strip()
                if text_part:
                    questions.append(
                        IntakeQuestion(
                            text=text_part,
                            answer=tail.strip() if sep else "",
                            required=match.group(1) == "!",
                        )
                    )
                continue
            if stripped and questions:
                # A continuation line: the operator answered under the question
                # instead of after the arrow. Keep it.
                prior = questions[-1]
                joined = f"{prior.answer}\n{stripped}" if prior.answer else stripped
                questions[-1] = replace(prior, answer=joined)
            continue
        if not stripped:
            continue
        if section.startswith(_H_EVIDENCE):
            body = stripped[1:].strip() if stripped.startswith("-") else stripped
            match = _SOURCE_RE.search(body)
            evidence.append(
                Evidence(
                    text=_SOURCE_RE.sub("", body).strip() if match else body,
                    source=match.group(1).strip() if match else "",
                )
            )
            continue
        if section.startswith(_H_ASSUMPTIONS):
            assumptions.append(stripped[1:].strip() if stripped.startswith("-") else stripped)
            continue
        if section.startswith(_H_DRAFT):
            head = _TASK_HEAD_RE.match(stripped)
            if head:
                pending_paths = False
                tasks.append(DraftTask(id=head.group(1).strip(), title=""))
                continue
            if not tasks:
                continue
            if pending_paths and stripped.startswith("-"):
                entry = stripped[1:].strip()
                if entry:
                    tasks[-1] = replace(
                        tasks[-1], approved_paths=tasks[-1].approved_paths + (entry,)
                    )
                continue
            key, sep, value = stripped.partition(":")
            if not sep:
                continue
            key, value = key.strip().lower(), value.strip()
            pending_paths = False
            if key == "title":
                tasks[-1] = replace(tasks[-1], title=value)
            elif key == "priority":
                try:
                    tasks[-1] = replace(tasks[-1], priority=int(value))
                except ValueError:
                    # Left at whatever it was. `check_request_shape` refuses a
                    # non-integer priority at submit in its own words; guessing
                    # one here would hide the typo instead of reporting it.
                    pass
            elif key == "depends_on":
                deps = tuple(d.strip() for d in value.split(",") if d.strip())
                tasks[-1] = replace(tasks[-1], depends_on=deps)
            elif key == "approved_paths":
                pending_paths = True
            elif key == "description":
                description = []
    flush_description()
    return IntakeDraft(
        slug=slug,
        idea=idea,
        questions=tuple(questions),
        evidence=tuple(evidence),
        assumptions=tuple(assumptions),
        tasks=tuple(tasks),
    )


def _render_question(question: IntakeQuestion) -> list[str]:
    marker = "?!" if question.required else "?"
    answer_lines = question.answer.split("\n") if question.answer else [""]
    out = [f"{marker} {question.text} -> {answer_lines[0]}".rstrip()]
    out += [f"  {line}" for line in answer_lines[1:]]
    return out


def render_draft(draft: IntakeDraft) -> str:
    """The file, from the parsed draft. `parse_draft(render_draft(d))` is `d`.

    The idea is written back BYTE FOR BYTE. Every pass re-renders the sections
    below the marker, so a round trip that lost an answer or reflowed one would
    silently eat the operator's words on the next `ask` —
    `test_intake.py::test_a_draft_round_trips_through_parse_and_render` is what
    stops that.
    """
    out = [draft.idea.strip("\n"), "", INTAKE_MARKER, "", _H_QUESTIONS]
    out += [
        "<!-- Answer after the `->`, or on the lines under a question. -->",
        "<!-- `?!` is yours alone: nothing is drafted until both are answered. -->",
        "<!-- `?` may be left blank; a blank means 'you decide' and the "
        "assumption is written down below. -->",
    ]
    for question in draft.questions:
        out += _render_question(question)
    out += ["", f"{_H_EVIDENCE} — read from this repository; delete any line that does not fit"]
    if draft.evidence:
        out += [
            f"- {item.text}" + (f" (source: {item.source})" if item.source else "")
            for item in draft.evidence
        ]
    else:
        out.append("<!-- nothing yet -->")
    if draft.assumptions:
        out += ["", f"{_H_ASSUMPTIONS} — what a blank answer was taken to mean"]
        out += [f"- {line}" for line in draft.assumptions]
    if draft.tasks:
        out += [
            "",
            f"{_H_DRAFT} — nothing is filed until you run "
            "`python -m autoloop intake submit`",
            "<!-- approved_paths were SUGGESTED mechanically (path_suggest) and "
            "authorize nothing. -->",
            "<!-- Editing them here and submitting is the confirmation. -->",
        ]
        for task in draft.tasks:
            out += [
                "",
                f"### task: {task.id}",
                f"title: {task.title}",
                f"priority: {task.priority}",
                f"depends_on: {', '.join(task.depends_on)}",
                "approved_paths:",
            ]
            if not task.approved_paths:
                # An empty list here is the ordinary case for an idea that
                # names no file, and it must not read as "nothing needed":
                # `draft_specs` refuses a task with no scope, because the
                # registry accepts one and the orchestrator then never
                # dispatches it. Say what to do instead of leaving a blank.
                out.append(
                    "<!-- nothing was detected — name a file, folder or "
                    "function above, or type the paths here as `  - path`. "
                    "Submitting with none is refused. -->"
                )
            out += [f"  - {path}" for path in task.approved_paths]
            if task.description:
                out.append("description:")
                out += [f"  {line}".rstrip() for line in task.description.split("\n")]
    return "\n".join(out).rstrip("\n") + "\n"


# ---- where drafts live -----------------------------------------------------


def intake_dir_for(workers_root: Path | None, state_dir: Path) -> Path:
    """Where drafts live: a SIBLING of the inbox, never inside it.

    Beside `workers_root` for the same reason `inbox_dir_for` is — outside the
    checkout, so a draft an operator edits mid-round is invisible to the escape
    detector and cannot park the loop.

    **A sibling, and that is load-bearing.** `TaskInbox.drain` globs `*.json`
    in its own directory and MOVES anything that will not parse into
    `rejected/`. A decline ledger or a draft written inside that directory
    would be eaten by the next drain, which both destroys the record and
    reports a spurious problem line. Pinned by
    `test_intake.py::test_a_drain_ignores_everything_intake_writes`.
    """
    if workers_root is not None:
        return Path(workers_root).expanduser().parent / "intake"
    return Path(state_dir) / "intake"


def draft_path(intake_dir: Path, slug: str) -> Path:
    """`<intake_dir>/<slug>.md`, or `IntakeError` for a slug that is not one.

    The check is containment, not tidiness: `slug` arrives from an HTTP body on
    the dashboard route, and `_SLUG_RE` is what stops `../../etc/whatever` or
    an absolute path being addressed at all.
    """
    text = str(slug or "").strip()
    if not _SLUG_RE.match(text):
        raise IntakeError(
            f"{slug!r} is not a usable draft name — lowercase letters, digits, "
            "'.', '_' and '-' only, starting with a letter or digit"
        )
    return Path(intake_dir) / f"{text}.md"


def slug_for(name: str) -> str:
    """A filename or free phrase, reduced to a slug this module accepts.

    Used by the file entry point, where the natural name for the draft is the
    dropped file's own stem, and by nothing that decides authorization.
    """
    text = _NORMALIZE_RE.sub("-", str(name).lower()).strip("-")
    return text[:64].strip("-.") or "idea"


def list_drafts(intake_dir: Path) -> list[Path]:
    directory = Path(intake_dir)
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.md") if p.is_file())


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def read_draft(path: Path) -> IntakeDraft:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise IntakeError(f"could not read the draft {path}: {exc}") from exc
    return parse_draft(text, Path(path).stem)


def create_draft(intake_dir: Path, slug: str, idea: str) -> Path:
    """THE entry point every route converges on. Writes one file, nothing else.

    The CLI's `intake new --text`, the CLI's `intake new --file` and the
    dashboard's `POST /api/intake` all call exactly this, with the same
    arguments, and therefore produce a BYTE-IDENTICAL file — which is the only
    falsifiable reading of "three entry points, one path".
    (`test_intake.py::test_three_entry_points_produce_a_byte_identical_draft`.)

    Deliberately clock-free: a timestamp in the created file would make that
    equality depend on three calls landing in the same second, and there is
    nothing here a date answers that the filesystem does not.

    Refuses to overwrite. A second `new` on a live draft would silently discard
    an exchange the operator is halfway through, and nothing in the flow could
    get it back.
    """
    text = str(idea or "").strip()
    if not text:
        raise IntakeError(
            "nothing to work from — write a sentence or two about what you want"
        )
    path = draft_path(intake_dir, slug)
    if path.exists():
        raise IntakeError(
            f"{path} already exists — edit it, or pick another name. Nothing was "
            "written, so the exchange already in that file is intact."
        )
    _write_atomic(path, render_draft(IntakeDraft(slug=Path(path).stem, idea=text)))
    return path


def create_draft_from_file(intake_dir: Path, source: Path, slug: str = "") -> Path:
    """The `.md`/`.txt` entry point. Reads the file and calls `create_draft`.

    The suffix check is what makes "an idea file" mean something: a `.py` or a
    `.json` pointed at this is almost certainly a mistake, and adopting it
    would produce a draft whose idea is source code.
    """
    src = Path(source)
    if src.suffix.lower() not in INTAKE_SUFFIXES:
        raise IntakeError(
            f"{src} is not an idea file — {', '.join(INTAKE_SUFFIXES)} only"
        )
    try:
        text = src.read_text(encoding="utf-8")
    except OSError as exc:
        raise IntakeError(f"could not read {src}: {exc}") from exc
    return create_draft(intake_dir, slug or slug_for(src.stem), text)


def write_draft_text(intake_dir: Path, slug: str, text: str) -> Path:
    """Replace a draft's text with what the operator edited. Verbatim.

    No normalisation, no re-render: this is their file, and a route that
    "tidied" it on the way in would be editing an artifact that becomes the
    task description. The draft must already exist — this edits, it never
    creates, so a typo'd slug cannot quietly mint a second draft.
    """
    if not isinstance(text, str):
        raise IntakeError("the draft body must be text")
    path = draft_path(intake_dir, slug)
    if not path.exists():
        raise IntakeError(f"no draft at {path} — create it first")
    _write_atomic(path, text if text.endswith("\n") else text + "\n")
    return path


# ---- the authoring-time guard: is it safe to ask a question at all? --------


def round_is_running(state_dir: Path) -> tuple[bool, str]:
    """`(running, why)` — and it FAILS CLOSED.

    Every "is anything running" check in this codebase that guessed has guessed
    wrong in the permissive direction, so the rule here is: proceed only when
    there is positively no live lock. A lock file that cannot be read, or one
    whose contents are corrupt, answers TRUE — because the alternative is an
    intake question reaching a model while a round is mid-flight, which is
    `ask_user` rebuilt under a new name.

    Note the deliberate disagreement with `LoopLock.is_live`, which treats a
    corrupt lock as stale: it is answering "may I take this lock", where the
    permissive answer is recoverable. This answers "is it safe to ask", where
    it is not.
    """
    from .lock import LOCK_FILENAME, LoopLock

    path = Path(state_dir) / LOCK_FILENAME
    try:
        if not path.exists():
            return False, ""
        info = LoopLock(Path(state_dir)).read()
    except OSError as exc:
        return True, f"the loop's lock file could not be examined ({exc})"
    if info is None:
        return False, ""
    if info.pid == -1:
        return True, (
            "the loop's lock file is corrupt, so whether a round is running "
            "cannot be determined"
        )
    if LoopLock.is_live(info):
        return True, f"the loop is running ({info.describe()})"
    return False, ""


def refuse_if_round_running(state_dir: Path, what: str) -> None:
    """Raise `IntakeError` if `what` must not happen right now.

    Applied to every step that asks a QUESTION or offers work — `interview_step`
    through its callers, `gather_suggestions`, `plan_step`. Deliberately NOT
    applied to writing a draft file or to `submit_draft`: those touch nothing
    inside the checkout and inherit `add-task`'s "safe at any moment, even
    mid-run" property, and taking that away would be a regression dressed as
    caution.

    It never takes `LoopLock`. That lock is held for the whole run, so waiting
    on it would mean waiting for the loop to stop — the same reasoning
    `dashboard._submit_priority` spells out.
    """
    running, why = round_is_running(state_dir)
    if running:
        raise IntakeError(
            f"not now: {why}. {what} asks a question, and a question asked "
            "mid-round is what `ask_user` was retired for. Intake is safe "
            "precisely because the operator is present, which is an "
            "authoring-time fact. Try again when the round ends — writing and "
            "editing a draft, and submitting one, stay safe at any moment."
        )


# ---- evidence: what the system actually read -------------------------------


def repo_evidence(repo: Path, text: str) -> tuple[tuple[Evidence, ...], str]:
    """`(evidence, note)` — mechanical, cited, and honest about reading nothing.

    Delegates to `path_suggest`, which is the module that already answers "what
    does this text point at in this repository" without an LLM, and each line
    carries the reason it was proposed so the operator can reject it.

    **An empty list and a failed scan are DIFFERENT, and the note is how.**
    `path_suggest.tracked_files` returns `[]` for a git that errored, a git
    that is missing and a directory that is not a checkout, all of which mean
    "nothing was read" — and an empty Evidence section rendered from that would
    read as "nothing relevant exists here", which is a fabricated negative.
    """
    from .path_suggest import suggest, tracked_files

    root = Path(repo)
    files = tracked_files(root)
    if not files:
        return (), (
            f"no evidence gathered: git listed no tracked files under {root} "
            "(not a checkout, or git did not answer). NOTHING WAS READ — which "
            "is not the same as nothing being relevant."
        )
    try:
        found = suggest(text, root)
    except OSError as exc:
        return (), f"no evidence gathered: the repository scan failed ({exc})"
    if not found:
        return (), (
            f"read {len(files)} tracked files; none was named or defined by the "
            "text so far. Name a file, a folder or a function to get more."
        )
    return (
        tuple(
            Evidence(text=f"{item.path} — {item.reason}", source="git ls-files")
            for item in found
        ),
        "",
    )


# ---- the interview ---------------------------------------------------------


@dataclass(frozen=True)
class InterviewPass:
    """What one pass did, for the operator to read. No state lives here."""

    path: Path
    added_questions: tuple[str, ...] = ()
    added_evidence: tuple[str, ...] = ()
    evidence_note: str = ""
    provider_note: str = ""
    open_questions: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return not self.blockers


def draft_blockers(draft: IntakeDraft) -> tuple[str, ...]:
    """Why this draft may NOT be turned into a task yet. Empty means ready.

    **This is the fail-open gate of the whole feature, so it is positive-only.**
    Readiness is never inferred from the absence of something: not from "the
    model returned no more questions" (a provider that is down returns none),
    not from "nothing is left to ask". It requires POSITIVE evidence — both
    `REQUIRED_QUESTIONS` present in the file AND answered by the operator.
    Deleting a required question from the file therefore blocks the draft
    rather than clearing it.
    """
    out: list[str] = []
    if not draft.idea.strip():
        out.append("the idea is empty — say what you want, in a sentence or two")
    answered = {
        _normalize(q.text): q.answered for q in draft.questions if q.required
    }
    for question in REQUIRED_QUESTIONS:
        key = _normalize(question)
        if key not in answered:
            out.append(
                f"the required question is not in the file: {question!r} — run "
                "`intake ask` (it adds it), or paste it back under ## Questions "
                "prefixed with `?!`"
            )
        elif not answered[key]:
            out.append(f"unanswered, and only you can answer it: {question}")
    return tuple(out)


def _is_echo(candidate: str, draft: IntakeDraft, seen: set[str]) -> bool:
    """True when a 'new' question is text we handed the model ourselves.

    The prompt carries the idea, the questions already asked and the evidence,
    so a model with nothing to add will hand some of it back. Reading that as
    output is the ECHO failure: it produces an interview that never converges
    (the same question re-added every pass) and, for an evidence line, promotes
    something we already cited into a second, uncited claim.
    """
    key = _normalize(candidate)
    if not key or key in seen:
        return True
    if key in {_normalize(line) for line in draft.idea.splitlines() if line.strip()}:
        return True
    return key in {_normalize(item.text) for item in draft.evidence}


def _questions_from_reply(reply: str, draft: IntakeDraft, seen: set[str]) -> list[str]:
    """The questions in a model reply — and never anything else from it.

    A reply with no `?` line yields nothing, which is a degraded pass and NOT a
    finished interview: `draft_blockers` is what decides readiness, and it
    cannot be satisfied by silence.
    """
    out: list[str] = []
    local = set(seen)
    for line in str(reply or "").splitlines():
        match = _REPLY_QUESTION_RE.match(line)
        if not match:
            continue
        # `->` is the file's answer separator, so a question containing one
        # would split wrongly on the next read. The arrow is preserved as a
        # character the operator can still read.
        question = match.group(1).strip().replace("->", "→")
        if not question or _is_echo(question, draft, local):
            continue
        local.add(_normalize(question))
        out.append(question)
    return out


def interview_prompt(draft: IntakeDraft) -> str:
    """What the configured conversation provider is asked.

    It is given the idea, the answers so far and the evidence — and told which
    of those are already handled, because the failure mode of not saying so is
    a reply that restates them as questions. It is asked for OPTIONS within the
    design space, not for judgement about the product: the system is entitled
    to opinions about the codebase, which it can read, and none about what the
    operator wanted.
    """
    lines = [
        "You are helping an operator turn a rough idea into ONE task "
        "specification for an automated implementation loop. You are NOT "
        "writing the task. You are asking the questions that would let them "
        "write it.",
        "",
        "THE IDEA, as they wrote it:",
        draft.idea.strip() or "(blank)",
        "",
    ]
    answered = [q for q in draft.questions if q.answered]
    if answered:
        lines.append("ALREADY ANSWERED — do not ask these again:")
        lines += [f"- {q.text} => {q.answer}" for q in answered]
        lines.append("")
    open_questions = [q for q in draft.questions if not q.answered]
    if open_questions:
        lines.append("ALREADY ASKED and still open — do not repeat these either:")
        lines += [f"- {q.text}" for q in open_questions]
        lines.append("")
    if draft.evidence:
        lines.append(
            "ALREADY KNOWN about the repository (read mechanically, not by you "
            "— do not restate any of it as a question or as a finding):"
        )
        lines += [f"- {item.text}" for item in draft.evidence]
        lines.append("")
    lines += [
        "Ask AT MOST 5 and at least 3 NEW questions that would sharpen this "
        "into something a reviewer could judge. Rules:",
        "- Each one must be about the DESIGN SPACE — an option the operator "
        "has to choose between, e.g. 'does this need to remember where you "
        "stopped reading?'. Offer the option; do not choose it.",
        "- 'No' must be a cheap, sensible answer to every one of them.",
        "- Do not ask what would prove it worked, or what would make it fail: "
        "those are already asked verbatim.",
        "- Do not ask anything the repository could answer; that is read "
        "mechanically.",
        "",
        "Reply with NOTHING but the questions, one per line, each line "
        "starting with '?'. No preamble, no numbering, no commentary.",
    ]
    return "\n".join(lines)


def _assumption_lines(draft: IntakeDraft) -> tuple[str, ...]:
    """What a blank answer was taken to mean, said out loud.

    Only OPTIONAL questions can produce one — a blank required answer blocks
    the draft instead (`draft_blockers`). Written into the file so the operator
    corrects it on the next pass rather than discovering it in a task.
    """
    return tuple(
        f"{q.text} — left blank, so this is being treated as 'you decide': the "
        "draft claims nothing about it."
        for q in draft.questions
        if not q.required and not q.answered
    )


def suggested_paths(repo: Path | None, text: str) -> tuple[str, ...]:
    """`approved_paths` proposals, each as `path  # reason`.

    From `path_suggest` and from nowhere else — never from the interview, never
    from a plan reply. That module's docstring is the rule: "A suggestion is not
    an authorization, and the distinction is the whole design." Filling the
    field is help; submitting it is the operator's confirmation, exactly as the
    dashboard's Detect-paths button behaves.

    The trailing comment is the same convention that button uses, and
    `_strip_path_comment` removes it again at submit: the operator reads
    reasons, the registry validates paths.
    """
    if repo is None:
        return ()
    from .path_suggest import suggest

    try:
        found = suggest(text, Path(repo))
    except OSError:
        return ()
    return tuple(f"{item.path}  # {item.reason}" for item in found)


def _seed_task(draft: IntakeDraft, repo: Path | None) -> DraftTask:
    """The single task a ready draft proposes, with SUGGESTED paths."""
    return DraftTask(
        id=draft.slug,
        title=draft.title,
        approved_paths=suggested_paths(repo, render_answers(draft)),
    )


def render_answers(draft: IntakeDraft) -> str:
    """The idea plus every question and answer, as one block of text.

    Used for the description AND as the text the path suggester reads, so the
    scope proposed reflects everything the operator has said rather than only
    their opening sentence.
    """
    out = [draft.idea.strip()]
    for question in draft.questions:
        out.append("")
        out.append(question.text)
        out.append(question.answer.strip() or "(left blank — you decide)")
    return "\n".join(out)


def interview_step(
    path: Path, *, ask=None, repo: Path | None = None
) -> InterviewPass:
    """One pass: re-read, keep what is answered, ask sharper ones, re-render.

    The pass NEVER decides the interview is over. It adds questions, adds
    evidence, records the assumptions blanks imply and — only when
    `draft_blockers` is empty — emits the `## Draft` section. A provider that
    fails is reported and costs the pass its optional questions; it cannot
    advance the draft one inch, because the two questions that gate readiness
    are constants added here.

    `ask` is injected rather than constructed so a test can drive a pass with a
    stub, and so the CLI and the dashboard share ONE implementation with one
    provider wiring (`provider_asker`).
    """
    draft = read_draft(path)
    seen = {_normalize(q.text) for q in draft.questions}
    added: list[IntakeQuestion] = []
    for question in REQUIRED_QUESTIONS:
        if _normalize(question) not in seen:
            added.append(IntakeQuestion(question, required=True))
            seen.add(_normalize(question))

    provider_note = ""
    room = MAX_QUESTIONS_PER_PASS - len(added)
    if ask is None:
        provider_note = (
            "no conversation provider was wired for this pass, so only the "
            "questions this module holds as constants were added"
        )
    elif room > 0:
        try:
            reply = ask(interview_prompt(draft))
        except IntakeError as exc:
            reply = ""
            provider_note = (
                f"the interview could not reach the model: {exc} — the questions "
                "only you can answer were added anyway; run this again for "
                "design-space options."
            )
        fresh = _questions_from_reply(reply, draft, seen)
        if not fresh and not provider_note:
            provider_note = (
                "the model returned no usable question this pass (a reply with "
                "no '?' line, or only echoes of what it was given). That is a "
                "thin pass, NOT a finished interview."
            )
        for question in fresh[:room]:
            added.append(IntakeQuestion(question, required=False))
            seen.add(_normalize(question))

    questions = draft.questions + tuple(added)
    known = {item.text for item in draft.evidence}
    evidence_note = ""
    new_evidence: tuple[Evidence, ...] = ()
    if repo is not None:
        found, evidence_note = repo_evidence(
            repo, render_answers(replace(draft, questions=questions))
        )
        new_evidence = tuple(item for item in found if item.text not in known)

    draft = replace(
        draft,
        questions=questions,
        evidence=draft.evidence + new_evidence,
    )
    blockers = draft_blockers(draft)
    assumptions: tuple[str, ...] = ()
    if not blockers:
        # Only now, and only here, does the file gain a task. Assumptions are
        # written at the same moment for the same reason: this is the point at
        # which the system PROCEEDS on a blank, and that is when it owes the
        # operator an account of what it assumed.
        assumptions = _assumption_lines(draft)
        merged = list(draft.assumptions)
        merged += [line for line in assumptions if line not in draft.assumptions]
        draft = replace(
            draft,
            assumptions=tuple(merged),
            tasks=draft.tasks or (_seed_task(draft, repo),),
        )
    _write_atomic(Path(path), render_draft(draft))
    return InterviewPass(
        path=Path(path),
        added_questions=tuple(q.text for q in added),
        added_evidence=tuple(item.text for item in new_evidence),
        evidence_note=evidence_note,
        provider_note=provider_note,
        open_questions=tuple(q.text for q in draft.questions if not q.answered),
        blockers=blockers,
        assumptions=assumptions,
    )


# ---- the provider seat -----------------------------------------------------


def provider_asker(config):
    """An `ask` callable backed by the CONFIGURED conversation provider.

    `[conversation] provider`, not a new one, and for three reasons: it is
    already configured with a credential and a failure mode the operator knows;
    a second provider is a second thing to break and to keep in sync with the
    adapters; and the interview runs at authoring time, outside a round, so it
    never competes with the reviewer for the same session.

    Every transport fault becomes an `IntakeError` carrying the transport's own
    remedy text, because that is what the operator can act on — and because the
    caller's whole error policy is "a failed ask is a thin pass", which needs
    one exception type to be true.
    """

    def ask(prompt: str) -> str:
        import uuid

        from .conversation import (
            SubmitResult,
            create_conversation,
            transport_remedy,
        )
        from .errors import AutoloopError

        provider = config.conversation.provider
        try:
            client = create_conversation(provider, config)
        except AutoloopError as exc:
            raise IntakeError(f"{provider} could not be started: {exc}") from exc
        request_id = f"intake-{uuid.uuid4().hex[:12]}"
        try:
            client.attach()
            if client.submit(request_id, prompt) is SubmitResult.REJECTED:
                raise IntakeError(
                    f"{provider} refused the intake question. "
                    f"{transport_remedy(provider)}"
                )
            return client.await_response(request_id)
        except AutoloopError as exc:
            raise IntakeError(
                f"{provider} did not answer: {exc}. {transport_remedy(provider)}"
            ) from exc
        finally:
            try:
                client.close()
            except (OSError, AutoloopError):
                pass

    return ask


# ---- phase 3: one level of decomposition, through the existing verb --------


@dataclass(frozen=True)
class PlanPass:
    path: Path
    tasks: tuple[str, ...] = ()
    note: str = ""


def plan_prompt(draft: IntakeDraft) -> str:
    """Ask for the `plan` directive the contract already defines.

    No second decomposer is built here. `Decision.PLAN` already carries
    `tasks: [TaskSpec]` and `TaskRegistry.add_many` already validates every
    path fail-closed; what was missing was an ENTRY POINT that submits a goal
    for planning, which is this.
    """
    from .contract import PROTOCOL_VERSION

    return "\n".join(
        [
            "Split ONE goal into a small number of tasks — ONE LEVEL ONLY.",
            "",
            "Do not decompose further than one level. A task that turns out to "
            "be too large is split later, by a reviewer holding that task's own "
            "evidence, when it actually refuses to fit — not now, on a guess.",
            "",
            "THE GOAL:",
            render_answers(draft),
            "",
            "Reply with ONE fenced json block and nothing else — the SAME "
            "directive shape the reviewer uses, parsed by the same "
            "`contract.parse_response`, because there is no second decomposer "
            "here:",
            f'{{"version": {PROTOCOL_VERSION}, "decision": "plan", '
            '"reason": "<why this split>", "tasks": [{"id": "<slug>", '
            '"title": "<title>", "description": "<what to build and what must '
            'not break>", "depends_on": []}]}',
            "",
            "Omit approved_paths: the scope is proposed mechanically from the "
            "repository and confirmed by a human, and anything you wrote there "
            "would be discarded.",
        ]
    )


def plan_step(path: Path, *, ask, repo: Path | None = None) -> PlanPass:
    """Replace a ready draft's single task with the one-level split of it.

    Refuses on a draft that is not ready (`draft_blockers`) and on one that
    ALREADY holds more than one task — that would be a second level, and a
    second level is `split-03`'s, issued by a reviewer with the failing task's
    evidence in front of it. There is no recursive mode here to build.

    `approved_paths` on every produced task is re-derived from `path_suggest`
    and the reply's own paths are dropped, so a plan cannot arrive carrying its
    own permission slip.
    """
    from .contract import ContractError, Decision, parse_response

    draft = read_draft(path)
    blockers = draft_blockers(draft)
    if blockers:
        raise IntakeError(
            "this draft is not ready to be split: " + "; ".join(blockers)
        )
    if len(draft.tasks) > 1:
        raise IntakeError(
            f"this draft already holds {len(draft.tasks)} tasks — planning it "
            "again would be a second level, which belongs to the reviewer that "
            "sees a task actually refuse to fit, not to this command"
        )
    reply = ask(plan_prompt(draft))
    try:
        directive = parse_response(reply)
    except ContractError as exc:
        raise IntakeError(f"the reply was not a usable directive: {exc}") from exc
    if directive.decision is not Decision.PLAN or not directive.tasks:
        raise IntakeError(
            f"expected a 'plan' directive; got {directive.decision.value!r}. "
            "Nothing was written."
        )
    tasks = [
        DraftTask(
            id=spec.id,
            title=spec.title,
            depends_on=tuple(spec.depends_on),
            # `spec.approved_paths` is DISCARDED, deliberately and silently to
            # the model: a plan that could name its own scope would be a task
            # arriving with its own permission slip, which is exactly the
            # circularity `docs/SECURITY.md` #2 closes.
            approved_paths=suggested_paths(repo, f"{spec.title}\n{spec.description}"),
            description=spec.description,
        )
        for spec in directive.tasks
    ]
    _write_atomic(Path(path), render_draft(replace(draft, tasks=tuple(tasks))))
    return PlanPass(
        path=Path(path),
        tasks=tuple(task.id for task in tasks),
        note=directive.reason,
    )


# ---- submission: the ONLY thing here that queues anything ------------------


def _strip_path_comment(entry: str) -> str:
    return entry.split("#", 1)[0].strip()


def render_task_description(draft: IntakeDraft, task: DraftTask) -> str:
    """The description the queued task carries.

    Derived from the artifact at SUBMIT time rather than stored, so the
    operator editing any part of the file — an answer, an evidence line, an
    assumption — changes what is filed. That is the point of the file being the
    exchange: there is no translation step in which the meaning drifts.
    """
    out = [task.description.strip()] if task.description.strip() else []
    out.append(render_answers(draft))
    if draft.evidence:
        out += ["", "Evidence read from the repository (each one is checkable):"]
        out += [
            f"- {item.text}" + (f" [{item.source}]" if item.source else "")
            for item in draft.evidence
        ]
    if draft.assumptions:
        out += ["", "Assumed from a blank answer:"]
        out += [f"- {line}" for line in draft.assumptions]
    out += [
        "",
        f"Authored through `python -m autoloop intake` ({draft.slug}.md). "
        "approved_paths were proposed by path_suggest and confirmed by the "
        "operator, who submitted this.",
    ]
    return "\n".join(out).strip()


def draft_specs(draft: IntakeDraft) -> list[dict]:
    """The inbox creation requests this draft would file. Queues NOTHING.

    Separated from `submit_draft` so both the CLI and the dashboard can show an
    operator exactly what a submit would send, and so `submit_draft` can
    shape-check every spec BEFORE queueing any of them.
    """
    blockers = draft_blockers(draft)
    if blockers:
        raise IntakeError(
            "this draft is not finished: "
            + "; ".join(blockers)
            + ". Run `intake ask` and answer the `?!` questions."
        )
    if not draft.tasks:
        raise IntakeError(
            "there is no `## Draft` section yet — run `intake ask` until it "
            "emits one (it does that only once the `?!` questions are answered)"
        )
    specs: list[dict] = []
    for task in draft.tasks:
        paths = [p for p in (_strip_path_comment(e) for e in task.approved_paths) if p]
        if not paths:
            # A UI precondition, in the same terms `dashboard._submit_task`
            # uses it: the registry accepts an empty scope and the orchestrator
            # then refuses to dispatch the task forever, so filing one is a
            # trap rather than a task.
            raise IntakeError(
                f"task {task.id!r} has no approved paths — a task with no "
                "authorized scope can never be dispatched. Edit the "
                "`approved_paths:` list under `### task: " + task.id + "`."
            )
        specs.append(
            {
                "kind": KIND_TASK,
                "id": task.id,
                "title": task.title,
                "description": render_task_description(draft, task),
                "priority": task.priority,
                "depends_on": list(task.depends_on),
                "approved_paths": paths,
            }
        )
    return specs


def submit_draft(path: Path, inbox: TaskInbox) -> list[tuple[str, Path]]:
    """File this draft through the inbox. The ONE place intake queues anything.

    Every spec is shape-checked against `check_request_shape` — the same
    function `TaskInbox.submit` calls — BEFORE any of them is written, so a
    two-task plan whose second task is malformed queues neither. Half a split
    is worse than none: the operator would have to work out which half landed.

    Nothing else changes. The registry still validates the graph on merge, the
    loop still drains between steps, and the draft file is left exactly where
    it is — it is the operator's record of what they filed.

    The pre-check cannot make the WRITES atomic — the inbox is a directory of
    files and has no transaction — so a disk failure between the first and the
    second still leaves a partial split. That case REPORTS what landed rather
    than raising a bare `OSError`, because the recovery ("re-submit only the
    missing half, or delete the queued file") depends on knowing which one it
    was, and a traceback does not say.
    """
    draft = read_draft(path)
    specs = draft_specs(draft)
    for spec in specs:
        check_request_shape(spec)
    filed: list[tuple[str, Path]] = []
    for spec in specs:
        try:
            filed.append((str(spec["id"]), inbox.submit(spec)))
        except OSError as exc:
            landed = ", ".join(task_id for task_id, _ in filed) or "(nothing)"
            raise IntakeError(
                f"could not queue {spec['id']!r}: {exc}. Already queued and "
                f"still in the inbox: {landed}. Nothing was rolled back — "
                "re-submitting this draft would queue those a second time, so "
                "delete them from the inbox first or file the rest by hand."
            ) from exc
    return filed


# ---- phase 1: suggest, so the operator never faces a blank page ------------


@dataclass(frozen=True)
class WorkSuggestion:
    """One concrete thing worth doing, WITH the source it came from.

    **A suggestion that cannot name a file, a finding id, a measurement or a
    task id must not be offered.** A system that suggests work will keep
    suggesting work whether or not any is needed, so the citation is not a
    courtesy — it is the only thing separating this from noise dressed as
    initiative. Every producer below reads a real artifact and puts its path or
    id in `cite`.
    """

    key: str
    source: str
    cite: str
    headline: str
    fingerprint: str

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "source": self.source,
            "cite": self.cite,
            "headline": self.headline,
            "fingerprint": self.fingerprint,
        }


def _fingerprint(*parts: str) -> str:
    """Identity of the EVIDENCE behind a suggestion, for the decline ledger.

    Over the finding id and its headline — stable text from a rendered report —
    and deliberately NOT over anything that moves on its own (a date, a count,
    a rendered "N open"). A fingerprint that changed by itself would expire
    every decline immediately, which is the same as not recording them.
    """
    return hashlib.sha256(" ".join(parts).encode("utf-8")).hexdigest()[:16]


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def audit_finding_suggestions(repo: Path, report_glob: str, registry_text: str):
    """Findings in the rendered audit reports that the registry never mentions.

    "Unactioned" is answered mechanically and narrowly: the finding's qualified
    id does not appear anywhere in `tasks.json`. That is checkable by the
    operator in one grep, which is the property that matters — a cleverer test
    would be one they could not verify.
    """
    out: list[WorkSuggestion] = []
    root = Path(repo)
    try:
        reports = sorted(root.glob(report_glob))
    except (OSError, ValueError):
        return out
    for report in reports:
        try:
            text = report.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            cite_path = str(report.relative_to(root))
        except ValueError:
            cite_path = report.name
        for line in text.splitlines():
            match = _AUDIT_FINDING_RE.match(line)
            if not match:
                continue
            qualified, headline = match.group(1), match.group(2)
            if qualified in registry_text:
                continue
            out.append(
                WorkSuggestion(
                    key=f"audit_finding:{qualified}",
                    source="audit_finding",
                    cite=f"{cite_path} — finding {qualified}, unmentioned in tasks.json",
                    headline=headline,
                    fingerprint=_fingerprint(qualified, headline),
                )
            )
    return out


def ready_task_suggestions(tasks_data) -> list[WorkSuggestion]:
    """Tasks already on the roadmap whose dependencies are all done.

    The operator may simply not know these are waiting, and offering one costs
    nothing: it is work already authored and already scoped.
    """
    rows = (tasks_data or {}).get("tasks") if isinstance(tasks_data, dict) else None
    if not isinstance(rows, list):
        return []
    done = {
        str(r.get("id")): str(r.get("status") or "pending")
        for r in rows
        if isinstance(r, dict) and r.get("id")
    }
    out: list[WorkSuggestion] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        task_id = str(row.get("id") or "")
        if not task_id or done.get(task_id) != "pending":
            continue
        deps = row.get("depends_on") or ()
        if not isinstance(deps, (list, tuple)):
            continue
        if any(done.get(str(d)) not in ("completed", "shipped_elsewhere") for d in deps):
            continue
        title = str(row.get("title") or "")
        out.append(
            WorkSuggestion(
                key=f"ready_task:{task_id}",
                source="ready_task",
                cite=f"tasks.json — task {task_id}, pending with every dependency done",
                headline=title or task_id,
                fingerprint=_fingerprint(task_id, title),
            )
        )
    return out


def open_blocker_suggestions(blockers_dir: Path) -> list[WorkSuggestion]:
    """Defects the loop itself recorded and has not had answered.

    A measured number in the loop's own records: each one is a round that
    stopped, with an id, a code and the question it stopped on. Cited by the
    record's own filename so the operator can open it.
    """
    directory = Path(blockers_dir)
    if not directory.is_dir():
        return []
    out: list[WorkSuggestion] = []
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        if data.get("resolved_at") or data.get("archived_reason"):
            continue
        blocker_id = str(data.get("id") or path.stem)
        code = str(data.get("code") or "")
        question = str(data.get("question") or "").strip().splitlines()[:1]
        headline = (question[0] if question else code or blocker_id)[:200]
        out.append(
            WorkSuggestion(
                key=f"open_blocker:{blocker_id}",
                source="open_blocker",
                cite=f"{path.name} — open blocker {blocker_id} ({code})",
                headline=headline,
                fingerprint=_fingerprint(blocker_id, code),
            )
        )
    return out


def declines_file(intake_dir: Path) -> Path:
    """The decline ledger — a sibling of the drafts, inside the intake dir.

    NOT inside the inbox directory: `TaskInbox.drain` globs `*.json` there and
    moves anything it cannot parse into `rejected/`, which would eat this file
    and report a problem line for it on the next drain.
    """
    return Path(intake_dir) / "declined.json"


def load_declines(intake_dir: Path) -> dict:
    data = _read_json(declines_file(intake_dir))
    return data if isinstance(data, dict) else {}


def record_decline(intake_dir: Path, key: str, fingerprint: str) -> Path:
    """Remember that this was declined, AGAINST THE EVIDENCE IT CARRIED.

    Declining must be free, and it must stick: re-offering the same finding
    tomorrow teaches the operator to stop reading the offers. It stops sticking
    only when the evidence itself changes — a re-worded finding, a task whose
    title moved — which is the "without new evidence" half of the rule.
    """
    path = declines_file(intake_dir)
    data = load_declines(intake_dir)
    data[str(key)] = str(fingerprint)
    _write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
    return path


@dataclass(frozen=True)
class SuggestionOffer:
    suggestions: tuple[WorkSuggestion, ...] = ()
    sources: tuple[str, ...] = ()
    declined: int = 0


def gather_suggestions(
    repo: Path,
    *,
    report_glob: str,
    tasks_file: Path,
    blockers_dir: Path,
    intake_dir: Path,
    limit: int = MAX_WORK_SUGGESTIONS,
) -> SuggestionOffer:
    """Two or three concrete things to do, each citing where it came from.

    Round-robin across the sources rather than taking the first three of
    whichever one is longest: a panel showing three findings from one report is
    a report, and the point of this is a CHOICE.

    Every source reports whether it was READ, including when it found nothing —
    an empty offer with no explanation is indistinguishable from "there is
    nothing to do", and one of those two is a lie.
    """
    tasks_data = _read_json(tasks_file)
    try:
        registry_text = Path(tasks_file).read_text(encoding="utf-8")
    except OSError:
        registry_text = ""
    findings = audit_finding_suggestions(repo, report_glob, registry_text)
    ready = ready_task_suggestions(tasks_data)
    blocked = open_blocker_suggestions(blockers_dir)
    sources = [
        f"audit reports ({report_glob}): {len(findings)} finding(s) the registry "
        "never mentions",
        (
            f"{tasks_file}: {len(ready)} ready task(s)"
            if tasks_data is not None
            else f"{tasks_file}: NOT READ (missing or unparseable) — 0 ready tasks "
            "reported, which is not the same as none existing"
        ),
        (
            f"{blockers_dir}: {len(blocked)} open blocker record(s)"
            if Path(blockers_dir).is_dir()
            else f"{blockers_dir}: NOT READ (no such directory)"
        ),
    ]
    declines = load_declines(intake_dir)
    picked: list[WorkSuggestion] = []
    skipped = 0
    pools = [findings, ready, blocked]
    index = 0
    while len(picked) < limit and any(pools):
        pool = pools[index % len(pools)]
        index += 1
        if not pool:
            continue
        item = pool.pop(0)
        if declines.get(item.key) == item.fingerprint:
            skipped += 1
            continue
        picked.append(item)
    return SuggestionOffer(
        suggestions=tuple(picked), sources=tuple(sources), declined=skipped
    )


def draft_from_suggestion(intake_dir: Path, suggestion: WorkSuggestion, slug: str = ""):
    """Turn an ACCEPTED suggestion into a draft — through `create_draft`.

    The handoff passes through a human: the system offers, the operator picks,
    and only then does a file exist. Nothing here submits, and the citation is
    carried into the idea text so the draft can still name where it came from
    after the offer is gone.
    """
    idea = "\n".join(
        [
            suggestion.headline,
            "",
            f"Offered by `intake suggest` from {suggestion.cite}.",
            "Rewrite this in your own words — the citation above is the "
            "evidence, not the specification.",
        ]
    )
    return create_draft(intake_dir, slug or slug_for(suggestion.key), idea)
