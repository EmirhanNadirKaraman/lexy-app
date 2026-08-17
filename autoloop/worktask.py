"""Produce-then-review task execution records and the pre-commit intent
marker that lets a crash between "commit ran" and "the SHA was persisted" be
reconciled safely.

Two things live here.

**`TaskExecution`** is the per-task bookkeeping record: which branch and
worktree the task ran in, the base sha recorded BEFORE any implementation
work started, and — filled in only after a real `git commit` returns — the
resulting candidate sha. It also carries the review round and the stamps
(`presented_report_sha256`, `review_request_id`) that bind a later approval to
the exact report that was reviewed, mirroring the naming already used for the
authorize-then-produce path's `reviewed` stamp (see `contract.py`), plus the
executor's own CLAIMED account of the work — `report_summary` / `report_details`
and `assumptions`, the last of which is where a task's ambiguities go now that
`ask_user` is retired and nothing can stop mid-run to ask about one.

**`CommitIntent`** is a durable marker written to disk BEFORE `git commit`
runs and cleared only after the resulting candidate sha has been persisted
into `TaskExecution`. Its only job is surviving a crash in the window where a
commit may or may not have happened. `git commit_and_capture` (in
`git_gateway.py`) writes it first, then commits, then reads `rev-parse HEAD`
— never predicting or precomputing the sha — so if the process dies between
"commit exited 0" and "the candidate sha was saved", the intent file is the
only durable evidence that a commit was ATTEMPTED and what it was attempting.

**Why identity and the commit message cannot tell a loop commit from a human
one (F8).** The loop uses one git identity and one message convention for
every commit; nothing stops a human working in the same worktree from using
the same author config and a similar message. So `reconcile_after_crash` never
looks at author, committer or message text. The only things it trusts are:

  * **parent linkage** — does the branch tip's *first* parent equal exactly
    the parent this task's commit was going to have (`expected_parent_sha`,
    recorded in the intent before `git commit` ran)? A merge commit (more
    than one parent) is refused even if the first parent matches, because a
    merge is never what `commit_and_capture` produces;
  * **the changed-path set** — is everything the branch tip actually changed
    (`GitGateway.commit_range_paths`) a SUBSET of what this task planned to
    touch (`planned_paths`, likewise recorded before the commit)? A commit
    that changed nothing recorded as planned is refused too (a genuinely
    empty change is not what the recorded intent describes, and treating it
    as trivially "a subset of anything" would recoverable something that was
    never the planned work).

Both signals are necessary and neither is sufficient on its own — a human
commit that happens to share the exact parent AND touch only planned paths by
coincidence is indistinguishable from the loop's own commit, and is
*deliberately* still classified RECOVERABLE (there is no stronger signal
available; the risk is bounded by how narrow "same parent + subset of a
specific planned path list" actually is). Anything looser than that — a
different parent, extra parents, or so much as one path outside the plan —
is AMBIGUOUS and parked for the operator rather than guessed at.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Sequence

from .errors import StateCorruptError
from .state import utcnow_iso


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_json(path: Path, data: dict) -> None:
    """temp-file + `os.replace`, with the temp file AND the containing
    directory entry fsync'd.

    `os.replace` alone gives crash-safe ORDERING (a reader never sees a
    half-written file) but not crash-safe PERSISTENCE — a power loss between
    the write and the OS actually flushing it can still lose the file. The
    intent marker's entire purpose is surviving a crash, so both fsyncs
    matter here; this mirrors the `os.open`/`os.write`/`os.fsync` pattern
    already used for the run lock (`lock.py`), extended with a directory
    fsync for the rename itself.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


@dataclass
class TaskExecution:
    task_id: str
    task_branch: str  # e.g. "autoloop/<task_id>"
    worktree_path: str
    task_base_sha: str  # recorded BEFORE any implementation
    candidate_sha: str = ""  # read from HEAD only AFTER git commit returns
    candidate_commit_count: int = 0
    review_round: int = 0
    #: Normalised text of the most recent `revise` feedback. Compared
    #: against the next one: identical feedback twice means the reviewer
    #: is asking for something the executor did not change, so another
    #: round cannot change its own outcome. This is what makes an
    #: unlimited round budget safe.
    last_revise_feedback: str = ""
    #: Every commit/packet attempt for this task that is the TASK's own —
    #: INCLUDING ones that never produced a review. `review_round`
    #: deliberately counts only dispatched reviews, so on its own it would let
    #: structural refusals churn locally without bound. This is the
    #: independent ceiling on that, and it still is: a validation failure, a
    #: structural refusal, a post-commit refusal and a round that reached the
    #: reviewer all land here.
    #:
    #: What does NOT land here, since 2026-08-17, is a round destroyed by
    #: something the task could not have avoided — a provider 429, an agent
    #: killed by the stall supervisor, a process that died mid-round. Those go
    #: to `fault_attempt_count` below. Both budgets are bounded; neither is
    #: spent by the other. See `attempt_ledger` for the per-attempt record of
    #: which one was charged and why.
    attempt_count: int = 0
    #: Attempts charged to the FAULT budget instead of `attempt_count`.
    #:
    #: Why a SECOND budget rather than an exemption. `attempt_count` is
    #: incremented before the executor runs precisely so a crash, a restart or
    #: a validation failure that never reaches a commit still consumes an
    #: attempt — that is the only bound on a task that dies every round
    #: without ever reaching a reviewer, and simply exempting faults would
    #: delete it. So faults keep a ceiling; they just keep their OWN, and a
    #: task converging through real review rounds is no longer killed by
    #: rounds it did not cause.
    #:
    #: The total number of dispatches for one task stays deterministically
    #: bounded: every dispatch appends exactly one `attempt_ledger` entry and
    #: charges exactly one of the two counters, so
    #: `attempt_count + fault_attempt_count == len(attempt_ledger)` holds
    #: (reclassification MOVES a charge, it never drops one). Since a dispatch
    #: requires both counters to be strictly under their ceilings, the ledger
    #: can never grow past `MAX_TASK_ATTEMPTS + MAX_TASK_FAULT_ATTEMPTS - 1`
    #: without an operator intervening. The one thing that resets this counter
    #: is an operator answering the `fault_attempt_ceiling` blocker it produced
    #: (`cli._clear_fault_budget_on_answer`) — an explicit, recorded decision
    #: to grant a fresh allowance, never something the loop does to itself.
    fault_attempt_count: int = 0
    #: One entry per dispatched attempt, in dispatch order, saying WHICH budget
    #: that attempt was charged to and WHY: `"<ordinal>|<budget>|<reason>"`
    #: with `budget` one of `ATTEMPT_PENDING` / `ATTEMPT_TASK` /
    #: `ATTEMPT_FAULT` (see `format_attempt` / `split_attempt`).
    #:
    #: This exists because the record did not say. An operator repaired six
    #: tasks by hand between 2026-08-15 and 08-17 (brw-09, exec-01, port-01,
    #: brw-11, dash-04, hlth-01), and an external watcher script had to GUESS
    #: which attempts were faults by comparing `attempt_count` against
    #: `review_round` — a heuristic, because nothing recorded the reason. Now
    #: it is recorded, per attempt, at the moment the attempt ends.
    #:
    #: A preformatted string rather than a nested dataclass on purpose:
    #: `TaskExecutionStore.load` does `TaskExecution(**data)` and does not
    #: rehydrate nested dataclasses, so a dataclass here would silently load
    #: back as a dict. Same idiom, and same first-seen ordering rule, as
    #: `assumptions` below.
    attempt_ledger: tuple[str, ...] = ()
    #: Set when a session-ending fault (a rate limit that outlasted its
    #: back-off budget, an exhausted provider allowance, a browser failure that
    #: spent the failure budget) killed the loop while THIS task had a
    #: committed candidate waiting to be reviewed. The work survived; the
    #: review did not, so the round has to be redone — and that redo is charged
    #: to the fault budget rather than the task's.
    #:
    #: Consumed exactly once, by the next dispatch, which clears it. A positive
    #: marker written at the fault, not a condition inferred afterwards: that
    #: is what keeps this from becoming the same guess the watcher script was
    #: making.
    pending_fault_code: str = ""
    presented_report_sha256: str = ""
    review_request_id: str = ""
    intended_remote: str = ""
    intended_remote_ref: str = ""
    #: The candidate sha CONFIRMED to be on `intended_remote_ref`, and when.
    #:
    #: Written by `orchestrator._dispatch_task_push` at the one point where
    #: publication is already established from git — the fresh `ls-remote`
    #: reconciliation that `_mark_task_completed` depends on — and never from
    #: anything the record, the executor or a directive asserts about itself.
    #: `intended_remote`/`intended_remote_ref` above are push INTENT, written
    #: BEFORE the network call on purpose, so a refused push leaves them
    #: looking exactly like a landed one; these two are the other half of that
    #: pair, and the only fields here that mean "it actually landed".
    #:
    #: Publication is where the other half of the record/lifecycle drift lives.
    #: A task can be published while its record still describes work in flight
    #: — merge-window reported exactly that for `audit-0002` on 2026-08-15
    #: ("safe to merge past, but its record still reads in_progress, so a later
    #: revise would park it"). Advancing the record here is what lets
    #: `_rebase_execution_if_stale` tell "reviewed work that would be
    #: discarded" from "work that already shipped" — after RE-CONFIRMING it
    #: against the remote, because git is the authority and this field is only
    #: a pointer at what to go and ask about.
    #:
    #: Empty on every record written before this field existed, and on every
    #: candidate that has not published — both load as "not known to have
    #: landed", which is the fail-closed reading.
    published_sha: str = ""
    published_at: str = ""
    #: Union of `changed_paths` across every round committed so far (pass 1's
    #: round produces the first set; each `revise` round adds its own on top).
    #: The post-commit path-ownership check (`Orchestrator._verify_committed`)
    #: compares this — not a single round's `changed_paths` — against
    #: everything `commit_range_paths(task_base_sha, candidate_sha)` reports,
    #: because that range spans EVERY round once round > 0. Comparing against
    #: only the latest round's paths would wrongly flag round 1's legitimate
    #: paths as "outside" on round 2's review. Stored sorted for a stable,
    #: deterministic on-disk representation.
    allowed_paths: tuple[str, ...] = ()
    #: The validation the TASK declared, persisted at dispatch so the
    #: post-commit re-run checks the same thing the executor checked.
    #:
    #: Without this the post-commit re-run fell back to
    #: `config.audit.validation_commands` — the generic repo-health set — so a
    #: task that declared its own validation precisely because the default does
    #: not cover what it changes had its REVIEWED COMMIT graded by the default
    #: anyway. The declared suite ran once, pre-commit, against a tree that a
    #: commit hook could still change. That is the exact gap produce-then-review
    #: exists to close, so the commands travel with the execution record rather
    #: than being re-derived from config (or from a `Task` the crash-recovery
    #: path may not have in hand).
    #:
    #: Empty means "the task declared none" — the caller then uses the
    #: configured default, matching `ImplementExecutor`'s own
    #: `tuple(task.validation) or self._validation_commands`.
    validation_commands: tuple[tuple[str, ...], ...] = ()
    #: Directory the validation runs from, relative to the worker repo root
    #: (`Task.validation_cwd`). Persisted for the same reason: running the
    #: right commands from the wrong directory checks nothing.
    validation_cwd: str = ""
    #: What the executor SAID it did — `ExecutionOutcome.summary`/`.details`
    #: from the round that produced `candidate_sha`.
    #:
    #: Persisted rather than passed along, because `_finish_postcommit` is also
    #: reached by crash-recovery adoption, where the executor ran in an earlier
    #: process and no `ExecutionOutcome` exists in this one. Only the summary
    #: reaches the commit message today (`title\n\nsummary`); the details were
    #: discarded entirely on the success path.
    #:
    #: These are CLAIMS, and the packet labels them as such. Everything else in
    #: a review packet is read from immutable git objects; this is the one
    #: section the executor authors. It exists so the reviewer can judge intent
    #: — "is this the right change?" — which a raw diff answers badly. It must
    #: never become the basis of an authorization decision: `allowed_paths`
    #: above is the scope, and the post-commit ownership check compares git's
    #: own `commit_range_paths` against THAT, never against this.
    report_summary: str = ""
    report_details: str = ""
    #: Paths this task's commits touched that `allowed_paths` did not authorize.
    #:
    #: ADVISORY since 2026-08-05. Both scope gates — the pre-commit one against
    #: `outcome.changed_paths` and the post-commit one against
    #: `commit_range_paths` — still run `tasks.unauthorized_paths` with exactly
    #: the inputs they always did; only the CONSEQUENCE changed. Where they used
    #: to park the task (`changed_paths_outside_approved` /
    #: `post_commit_verification_failed`) they now record the result here and let
    #: the round proceed to review. Operator decision after six refusals in three
    #: days, every one legitimate work and at least three caused by a task scope
    #: that was simply guessed wrong: a scope declared up front is a prediction,
    #: and a wrong prediction should inform the reviewer, not stop the work.
    #:
    #: Written ONLY from what those comparisons produced — never from anything an
    #: agent reports about its own scope. This records that authorization was
    #: exceeded; it never grants it. `allowed_paths` above remains the
    #: authorization, still derived solely from `task.approved_paths`, and is
    #: never widened by what lands here.
    #:
    #: ACCUMULATED across rounds (union), not replaced. The post-commit
    #: comparison spans the whole `task_base_sha..candidate_sha` range, so it is
    #: already cumulative; making the pre-commit one replace instead would let a
    #: clean round 2 erase round 1's finding from the record and then have the
    #: post-commit pass silently put it back. Stored sorted, like
    #: `allowed_paths`, for a stable on-disk representation.
    #:
    #: An empty `approved_paths` is a DIFFERENT rule and is NOT relaxed: a task
    #: that declared no scope is refused dispatch outright and never reaches
    #: either comparison.
    out_of_scope_paths: tuple[str, ...] = ()
    #: Readings the executor CHOSE where the task did not say, one string each
    #: (`ExecutionOutcome.assumptions`, produced per round).
    #:
    #: This field is the durable half of retiring `ask_user`. An ambiguous task
    #: used to be escalatable mid-run; it is not any more, so the executor
    #: takes the SMALLEST REVERSIBLE READING and records what it took. Durable
    #: rather than passed along for the same reason `report_summary` above is:
    #: the packet is rendered by `_finish_postcommit`, which crash-recovery
    #: adoption also reaches with no `ExecutionOutcome` in the process — an
    #: assumption that lived only in the outcome object would silently vanish
    #: from exactly the review that most needed it.
    #:
    #: ACCUMULATED across rounds, like `out_of_scope_paths` and for the same
    #: reason: a round-2 executor that assumed nothing must not erase what
    #: round 1 assumed and shipped — those lines still describe code inside
    #: `task_base_sha..candidate_sha`, which is the range the reviewer is
    #: authorizing. Duplicates are dropped (the same assumption restated in a
    #: later round is one assumption).
    #:
    #: Stored in FIRST-SEEN order, deliberately unlike the sorted path tuples
    #: above: a path set has no meaningful order so sorting buys a stable
    #: representation for free, whereas these are prose whose order carries
    #: which round chose what. The order is still deterministic — it is the
    #: order the rounds ran in — so the on-disk form is stable for a given
    #: history.
    #:
    #: COMPLETE on disk, and bounded only when RENDERED — every line the
    #: executor wrote, at the length it wrote it. With unlimited review rounds
    #: this list can outgrow a chat message, but that is a constraint on the
    #: packet, not on the record: `packet._format_assumptions` shows the newest
    #: entries that fit (shortening any single over-long one, and saying so),
    #: while every entry stays here in full for a crash-recovery adoption or an
    #: after-the-fact read. Truncating the record instead would delete evidence
    #: permanently to solve a problem that only exists at render time — and
    #: because `report_details` is replaced each round, "permanently" is
    #: literal: no other copy of a dropped line survives the next round.
    #:
    #: CLAIMS, never authorization. Same rule as `report_summary`: an executor
    #: cannot widen its scope, pass its validation, or license a push by
    #: writing a sentence here. The only thing this can do is inform the
    #: reviewer's judgement, which is precisely what it is for.
    assumptions: tuple[str, ...] = ()


#: There is deliberately NO per-round cap here, and there was one until
#: 2026-08-16: `MAX_ASSUMPTIONS_PER_ROUND` (20) / `MAX_ASSUMPTION_CHARS` (500)
#: bounded what `implement_executor._extract_assumptions` handed over, which
#: bounded a chat message by editing a durable record. The two are not the same
#: constraint. `report_details` — the only other place those lines survive — is
#: REPLACED every round, so the twenty-first line of round 1 was unrecoverable
#: the moment round 2 committed, and the record's whole reason to exist is that
#: it is the thing which does NOT get replaced. The packet is bounded instead,
#: where the size limit actually applies (`packet.ASSUMPTIONS_MAX_CHARS`,
#: `packet.ASSUMPTION_MAX_CHARS_EACH`), and it states what it withheld.


#: `attempt_ledger` budget labels. `ATTEMPT_PENDING` is the OPEN state written
#: at dispatch and replaced by one of the other two when the round ends; an
#: entry still reading `pending` therefore means exactly one thing — the
#: process died between the dispatch and the round's own exit.
ATTEMPT_PENDING = "pending"
ATTEMPT_TASK = "task"
ATTEMPT_FAULT = "fault"

#: Separator for a ledger entry. Chosen because no reason slug or ordinal
#: contains it, so `split_attempt` never has to guess.
_LEDGER_SEP = "|"


def format_attempt(ordinal: int, budget: str, reason: str) -> str:
    """One `attempt_ledger` entry. `reason` is a short machine slug (the same
    vocabulary as a `blockers.Blocker.code`), never free prose — an operator
    greps these."""
    return f"{ordinal}{_LEDGER_SEP}{budget}{_LEDGER_SEP}{reason}"


def split_attempt(entry: str) -> tuple[int, str, str]:
    """`(ordinal, budget, reason)` for one `attempt_ledger` entry.

    Tolerant on the way IN so a hand-edited or truncated record cannot crash a
    dispatch: an unparseable ordinal reads as 0 and a missing field as `""`.
    An entry that does not name a known budget therefore reads as neither
    `ATTEMPT_PENDING` nor a charge, which is the fail-closed direction — the
    reconciliation below only ever touches entries that positively say
    `pending`.
    """
    parts = entry.split(_LEDGER_SEP, 2)
    while len(parts) < 3:
        parts.append("")
    try:
        ordinal = int(parts[0])
    except ValueError:
        ordinal = 0
    return ordinal, parts[1], parts[2]


def accumulate_assumptions(
    existing: Sequence[str], incoming: Sequence[str]
) -> tuple[str, ...]:
    """`existing` plus whatever in `incoming` is new, in first-seen order.

    The one place `TaskExecution.assumptions` grows, so the union rule lives
    here rather than being restated at the call site (`orchestrator.
    _dispatch_task_postcommit`) where a later round could quietly get it wrong
    by assigning instead of merging.

    Blank and whitespace-only entries are dropped — an empty assumption
    discloses nothing and would render as a stray bullet in the packet — and
    each entry is compared after stripping, so the same sentence with trailing
    whitespace does not appear twice.

    **Deliberately UNBOUNDED, and the bound lives in `packet.
    _format_assumptions` instead.** `policy.max_review_rounds` defaults to
    unlimited, so this list really can grow past what one chat message can
    carry — but that is a MESSAGE constraint, and this is a durable record.
    Truncating here would destroy evidence permanently to solve a problem that
    only exists at render time, and it would do so in the file a crash-recovery
    adoption or an after-the-fact investigation reads. The packet renders the
    newest entries that fit and says how many it did not show; the record keeps
    all of them.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for item in list(existing) + list(incoming):
        text = (item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
    return tuple(merged)


@dataclass
class CommitIntent:
    task_id: str
    task_branch: str
    expected_parent_sha: str
    planned_paths: tuple[str, ...]
    planned_paths_digest: str
    message_sha256: str
    #: Random per-attempt token, generated BEFORE `git commit` and written into
    #: the commit message as a trailer. Parent linkage plus a planned-path
    #: subset cannot establish PROVENANCE — a human commit with the same parent
    #: touching only planned paths is indistinguishable. A 128-bit token that
    #: exists only in this record and in the resulting commit is the difference
    #: between "looks like ours" and "is ours".
    nonce: str = ""
    created_at: str = field(default_factory=utcnow_iso)

    @staticmethod
    def new_nonce() -> str:
        return secrets.token_hex(16)

    def trailer(self) -> str:
        return f"Autoloop-Intent: {self.nonce}"

    @classmethod
    def create(
        cls,
        task_id: str,
        task_branch: str,
        expected_parent_sha: str,
        planned_paths: Sequence[str],
        message: str,
    ) -> "CommitIntent":
        """Build an intent with both digests computed from the real values,
        so the persisted digest can never drift from what was actually
        planned. `planned_paths` is stored sorted — order never carries
        meaning for a path set, and a stable order makes the digest stable
        too. Paths are not stripped: interior whitespace/tabs in a filename
        are part of its identity."""
        paths = tuple(sorted(p for p in planned_paths if p))
        return cls(
            task_id=task_id,
            task_branch=task_branch,
            expected_parent_sha=expected_parent_sha,
            planned_paths=paths,
            planned_paths_digest=_sha256_hex("\n".join(paths).encode("utf-8")),
            message_sha256=_sha256_hex(message.encode("utf-8")),
            nonce=cls.new_nonce(),
        )


class Reconciliation(str, Enum):
    #: Branch tip is still exactly the recorded expected parent — the
    #: commit this intent describes never happened.
    NO_COMMIT = "no_commit"
    #: Branch tip is a single-parent child of the expected parent, and its
    #: changed-path set is a (non-empty) subset of what was planned. Safe to
    #: treat as this task's own commit and resume from it.
    RECOVERABLE = "recoverable"
    #: Anything else. Deliberately the wide bucket — parked for the operator
    #: rather than guessed at, because the cost of wrongly auto-adopting
    #: someone else's commit is far worse than a parked task.
    AMBIGUOUS = "ambiguous"


class TaskExecutionStore:
    """One JSON file per task id under `directory`. A corrupt record RAISES
    (`StateCorruptError`) rather than being read as absent — silently
    treating corruption as "no execution record" would erase a task's
    provenance (its base sha, its candidate sha, its review state) exactly
    when a crash makes that provenance most needed."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, task_id: str) -> Path:
        return self.directory / f"{task_id}.json"

    def save(self, execution: TaskExecution) -> None:
        data = asdict(execution)
        data["allowed_paths"] = sorted(execution.allowed_paths)
        data["out_of_scope_paths"] = sorted(execution.out_of_scope_paths)
        data["validation_commands"] = [list(c) for c in execution.validation_commands]
        # NOT sorted, unlike the two path sets above — see
        # `TaskExecution.assumptions`: accumulation order is which round chose
        # what, and sorting prose alphabetically would destroy that while
        # buying nothing (the order is already deterministic).
        data["assumptions"] = list(execution.assumptions)
        # Dispatch order, like `assumptions` and for the same reason: the
        # ordinal in each entry IS which round it describes, so sorting would
        # destroy the one thing the ledger is for.
        data["attempt_ledger"] = list(execution.attempt_ledger)
        _atomic_write_json(self._path(execution.task_id), data)

    def load(self, task_id: str) -> TaskExecution | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["allowed_paths"] = tuple(data.get("allowed_paths", ()))
            # Same JSON-has-no-tuples coercion, and the same `.get` default: a
            # record written before the scope check became advisory has no key
            # at all and loads as "nothing recorded out of scope".
            data["out_of_scope_paths"] = tuple(data.get("out_of_scope_paths", ()))
            # JSON has no tuples: a record written before this field existed
            # has no key at all, and one written after has lists-of-lists.
            # Both normalise to the same shape, so an older record loads as
            # "declared none" rather than raising.
            data["validation_commands"] = tuple(
                tuple(c) for c in data.get("validation_commands", ())
            )
            # Same `.get` default as the three above, and it is the whole of
            # the backward compatibility: a record written before assumptions
            # were captured has no key, and loads as "none recorded" — which
            # is the truth about it, not a guess. (It is NOT the same claim as
            # "the executor assumed nothing"; `packet._format_executor_report`
            # is what keeps those two readings apart for a reviewer.)
            data["assumptions"] = tuple(data.get("assumptions", ()))
            # Same `.get` default again: a record written before the attempt
            # ledger existed has no key, and loads as "no per-attempt reasons
            # recorded". Its `attempt_count` is still honoured as-is — the
            # missing ledger is NOT read as "those attempts were faults", which
            # would retroactively refund a budget nobody can audit.
            data["attempt_ledger"] = tuple(data.get("attempt_ledger", ()))
            return TaskExecution(**data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StateCorruptError(
                f"task execution record {path} is unreadable: {exc}"
            ) from exc

    def clear(self, task_id: str) -> None:
        self._path(task_id).unlink(missing_ok=True)

    def archive(self, task_id: str, label: str) -> Path | None:
        """MOVE (never delete) the record for `task_id` into
        `directory/archive/<task_id>-<label>.json`. Returns the destination,
        or `None` when there was no record to retire.

        The counterpart to `WorkerRepoManager.quarantine`, and deliberately
        the same shape: a record describes work that may still be recoverable
        (its `candidate_sha` names a real commit inside the worker repo being
        quarantined alongside it), so retiring it must never be `clear()`.
        Same uniqueness contract too — a colliding destination raises rather
        than clobbering an earlier retirement's evidence.

        **The `archive/` SUBDIRECTORY is load-bearing.** Both readers of these
        records — `cli._merge_window_blockers` and `dashboard.merge_states` —
        glob `executions/*.json`, which does not recurse. Filing a retired
        record one level down is therefore what actually takes it out of the
        merge-window gate; renaming it in place would leave it counted.

        No `validate_task_id` here, deliberately, even though the other half of
        a retirement (`WorkerRepoManager.quarantine`) does call it. That check
        refuses `..`, which `TaskRegistry`'s own id rule
        (`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`) permits — so adding it would make
        `release` raise on a legal task id that has no worker repo, a path that
        works today. It would also buy nothing: the registry rule admits no
        `/`, so `<task_id>-<label>.json` is always a single filename and `..`
        without a separator traverses nothing. `save`/`load`/`clear` build their
        names from the same id on the same reasoning.
        """
        path = self._path(task_id)
        if not path.exists():
            return None
        dest_dir = self.directory / "archive"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{task_id}-{label}.json"
        if dest.exists():
            raise StateCorruptError(
                f"execution archive destination {dest} already exists — 'label' "
                "must be unique per call"
            )
        os.replace(path, dest)
        return dest


@dataclass(frozen=True)
class Retirement:
    """What one `retire_execution` call filed away, and under what label."""

    label: str
    record_path: Path | None = None
    worker_path: Path | None = None


def retire_execution(
    task_id: str,
    execution_store: TaskExecutionStore,
    worker_repos,
    reason: str = "released-by-operator",
) -> Retirement:
    """Retire BOTH halves of a task's execution — the `TaskExecution` record
    AND the worker repository that produced it — under ONE label, in one call.

    Why one call and one label. `release` used to fix the task STATUS and
    quarantine the WORKER REPO, and left the execution record exactly where it
    was, `candidate_sha` and all. The record then claimed live unpublished work
    for a task that had been returned to pending and would be redone from
    scratch, and `cli._merge_window_blockers` — which reads those records — held
    the merge window shut on it. Observed 2026-08-15: releasing 25 stranded
    tasks the day before left 14 such records, every one bound to the
    pre-merge HEAD, and the window could not reopen by itself because each of
    those tasks would have had to be re-dispatched AND re-published first. With
    `auto_merge_enabled` on, the next task to complete logged
    `auto_merge_deferred "merge window closed"` and the published-but-unmerged
    backlog started rebuilding silently. An operator archived the 14 records by
    hand.

    The two halves describe the same attempt, so retiring them is ONE call: no
    caller can reach one half without invoking the other, which is the drift
    this exists to prevent. That is a structural guarantee about the CALL, not
    an atomic transaction over the filesystem — the ordering rule below is what
    covers a half that fails. The shared label is what makes the pairing visible
    on disk afterwards: the quarantined worker at
    `quarantine/<task_id>-<label>` and the archived record at
    `executions/archive/<task_id>-<label>.json` name each other, so a human
    reading either one can find the other half of the same attempt.

    RECORD FIRST, worker second, deliberately. Either half can fail
    (a filesystem error, a colliding label), and the two residues are not
    equally safe. A left-behind record is SILENT: it holds the merge window
    shut and nothing announces it — the exact failure this function exists to
    end. A left-behind worker repo is LOUD: the next dispatch's
    `WorkerRepoManager.create` refuses to write into an existing directory and
    parks with a message naming it. Order the risk so the surviving failure is
    the one that reports itself.
    """
    stamp = utcnow_iso().replace("+00:00", "Z").replace(":", "").replace("-", "")
    label = f"{reason}-{stamp}"
    record_path = execution_store.archive(task_id, label)
    worker_path = None
    if worker_repos is not None and worker_repos.path_for(task_id).exists():
        worker_path = worker_repos.quarantine(task_id, label)
    return Retirement(label=label, record_path=record_path, worker_path=worker_path)


class IntentStore:
    """One JSON file per task id under `directory`, written durably BEFORE
    `git commit` runs and cleared only after the resulting candidate sha has
    been persisted to `TaskExecutionStore`. A corrupt intent file RAISES
    rather than reading as absent: the entire point of this marker is that
    its absence means "no commit was attempted", so treating corruption as
    absence would misclassify a genuinely in-flight commit as `NO_COMMIT`
    instead of surfacing the corruption for the operator to look at."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, task_id: str) -> Path:
        return self.directory / f"{task_id}.json"

    def save(self, intent: CommitIntent) -> None:
        data = asdict(intent)
        data["planned_paths"] = list(intent.planned_paths)
        _atomic_write_json(self._path(intent.task_id), data)

    def load(self, task_id: str) -> CommitIntent | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["planned_paths"] = tuple(data.get("planned_paths", ()))
            return CommitIntent(**data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StateCorruptError(f"commit intent {path} is unreadable: {exc}") from exc

    def clear(self, task_id: str) -> None:
        self._path(task_id).unlink(missing_ok=True)


def reconcile_after_crash(
    intent: CommitIntent, branch_head: str, base_sha: str, git
) -> Reconciliation:
    """Classify what happened to `intent`'s commit after a crash.

    `base_sha` is the task's `task_base_sha` (recorded before implementation
    started); `intent.expected_parent_sha` is the parent this specific commit
    attempt was going to have (recorded immediately before `git commit` ran,
    which on a review round after the first is a LATER sha than
    `task_base_sha`, not the same one). As a sanity gate, the expected parent
    must itself be `base_sha` or a descendant of it — `merge-base
    --is-ancestor` is reflexive, so this one call covers both — otherwise the
    intent does not even belong to this task's lineage and nothing below can
    be trusted enough to call RECOVERABLE.

    See the module docstring for why parent linkage + a path-subset check is
    the strongest available signal and why identity/message are not used at
    all.
    """
    expected = intent.expected_parent_sha
    if not git.is_descendant(expected, base_sha):
        return Reconciliation.AMBIGUOUS
    if branch_head == expected:
        return Reconciliation.NO_COMMIT
    info = git.read_commit(branch_head)
    parents = info.get("parents", [])
    if len(parents) != 1 or parents[0] != expected:
        # Anything other than exactly one parent matching `expected` —
        # including a merge commit whose FIRST parent happens to match — is
        # not what `commit_and_capture` produces.
        return Reconciliation.AMBIGUOUS
    # PROVENANCE. Parent linkage and a path subset prove shape, not authorship:
    # a human commit with the same parent touching only planned paths matches
    # both. The nonce was generated before `git commit` and exists only in this
    # record and in the message of the commit it produced, so requiring it is
    # what turns "looks like ours" into "is ours". Its absence is AMBIGUOUS —
    # parked for a human — not a rejection of the commit itself.
    if intent.nonce:
        message = git.read_commit(branch_head).get("message", "")
        if intent.trailer() not in message:
            return Reconciliation.AMBIGUOUS
    changed = git.commit_range_paths(expected, branch_head)
    planned = set(intent.planned_paths)
    if not changed or not changed.issubset(planned):
        # An empty changed-path set is refused too: it is not "trivially a
        # subset of the plan", it is not the planned work at all.
        return Reconciliation.AMBIGUOUS
    return Reconciliation.RECOVERABLE
