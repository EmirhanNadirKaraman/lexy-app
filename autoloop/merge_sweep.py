"""Sweep the backlog of published-but-unmerged branches into the base.

`auto_merge.py` reacts to ONE completion: the task it just saw publish gets
merged, and its deferral queue retries the ones it saw earlier. Neither
covers a branch that was published before any of that existed, or published
by a process that died before it could integrate anything. Those branches
have no event left to react to, and nothing ever looks for them again.

On **2026-08-06 seven completed tasks were published and unmerged at the same
time** — auto-08, auto-12, brw-01, brw-07, inbox-09, rt-10, rt-11 — while the
base was still at d2d4d6b. Nothing surfaced it. It took a hand-written
`git ls-remote` loop to notice, and two of the seven were fixes for failures
the loop was still hitting while their code sat on branches nobody had
pulled. This module is the thing that would have noticed.

## What it does NOT reimplement

Every rule about *how* a branch reaches the base already exists in
`auto_merge.AutoMerger.attempt`: the merge window gate, the dirty-checkout
refusal, the moved-remote-base check, the merge itself, the four-part
verification that the merge really happened, the conflict abort, and the push.
This module CALLS that method once per branch. It contributes exactly three
things `attempt` has no way to know about:

1. **Enumeration** — which branches are outstanding at all.
2. **Order** — oldest publication first.
3. **Stopping** — the whole sweep halts at the first branch that does not land.

A second implementation of the merge rules that drifted by one case is the
same failure `_merge_window_blockers` exists to prevent; see `auto_merge.py`'s
own note about calling the gate rather than copying it.

## Unmerged-ness is decided by git ancestry, and by nothing else

`merge-base --is-ancestor <candidate> <base HEAD>`. Not the task's status, not
the branch's name, not whether a `MergeDeferral` exists for it. Status and
name are precisely what made the 2026-08-06 backlog invisible: every one of
those seven tasks was `completed` with a plausibly-named branch on origin, and
both facts are equally true of a branch that landed in the base an hour ago.
Only ancestry distinguishes them.

A candidate the checkout cannot resolve answers "not an ancestor" rather than
raising, and that is not merely fail-open: a commit that is an ancestor of
HEAD is by definition in this object database, so an unresolvable one is
provably not integrated here. It goes to `attempt`, which fetches it from the
task's own worker repo (`_ensure_object`) before deciding anything.

The registry is still read — the sweep only touches COMPLETED tasks, matching
`AutoMerger.attempt`'s own refusal to merge over a task an operator has
quarantined — but it is used to narrow the candidate set, never to conclude
that something is already merged.

A completed, unmerged task whose branch the remote does NOT confirm is named
(`merge_sweep_skipped`) and passed over rather than halting the sweep. There is
nothing to merge from — `_mark_task_completed` only fires on a confirmed
publication, so this means the ref was deleted or force-moved afterwards, or
the remote is unreachable right now — and nothing has been mutated, so it is
not the half-done state stopping exists to prevent. It does leave a hole in the
publication order, which is safe for the same reason a wrong order is: the next
branch either applies or conflicts, and a conflict stops cleanly.

**A skipped branch is never reported as a clear backlog.** `SweepResult.
is_clear` is false whenever anything was skipped, so the command exits 1 and
the startup hook prints rather than staying quiet. `_candidate_publication`
cannot tell "the ref is gone" from "the remote did not answer", and it is not
asked to: an unverifiable answer is not an answer. Letting an offline run
report `nothing_to_do` would be this module saying "I looked, the backlog is
clear" when the truth is "I could not look" — the 2026-08-06 invisibility
rebuilt one layer up, inside the tool written to end it.

## Order: oldest publication first

Attempting the backlog in arbitrary order manufactures conflicts that do not
really exist. If branch B was cut after branch A and touches what A touched,
merging B first collides with A's changes on the base; merging A first makes
B apply cleanly. The order is therefore publication order:

* `TaskExecution.published_at` when the record carries one.
* A record with an EMPTY `published_at` predates the field (added 2026-08-15,
  see `worktask.py`), so it is older than every record that has one and sorts
  ahead of all of them. That is not a guess — it is what an absent field
  means here, and it happens to be exactly the 2026-08-06 backlog this module
  was written for.
* Within that older group, the candidate commit's own committer timestamp
  breaks the tie, since a branch cut from another branch commits later than
  the one it builds on. Unreadable timestamp sorts first; ties break on task
  id so two runs over the same backlog always attempt it in the same order.

Order is a heuristic, and it is allowed to be, because **stopping makes a
wrong order safe**: a mis-ordered pair conflicts, the sweep aborts that merge
and halts with the base byte-identical, and the operator resolves it. A wrong
order costs a stalled sweep. It never costs a corrupted base.

## Stop at the first branch that does not land

Not just conflicts. `AutoMerger._merge` deliberately does NOT undo a merge
that failed verification (`reset` is absent from the git whitelist by design),
so continuing past a `failed` outcome would stack a second merge onto a head
nobody understands. A deferral means a precondition the whole sweep shares
(the remote base moved, the checkout went dirty) has stopped holding. All of
them halt the sweep, the remaining branches are left untouched and NAMED in
the transcript, and the operator gets one situation to reason about instead of
a half-swept backlog with one branch aborted somewhere in the middle.

One deferral is per-branch rather than sweep-wide and still stops everything,
deliberately: `AutoMerger._ensure_object` failing — a candidate the checkout
cannot resolve and whose recorded worker repo is gone. This gateway has no
policy-legal way to fetch it from the remote (`fetch_object` takes a local path
by design), so that branch cannot be integrated here at all, and every sweep
from now on will stop at it. That is the intended report, not an oversight: the
branches behind it may well build on it, and a tool quietly working around a
branch it can never merge is how a backlog becomes invisible again. The
recovery is an operator `git fetch` + merge, or a `release` of the record; both
end the stall, and `merge_sweep_stopped` names the branch and the remainder.

## The gate defers the whole sweep, never part of it

`cli._merge_window_blockers` is checked ONCE before the first merge. Shut →
nothing is attempted at all. Letting each branch discover the shut gate for
itself would write one `MergeDeferral` per branch and log N deferrals for one
condition, which is the "part of it" this exists to avoid. `attempt` still
re-checks the gate per branch, and that check stays: it is the race guard for
a window that shuts mid-sweep.

## No state of its own

There is no sweep queue on disk. The work-list is re-derived from git ancestry
on every invocation, so a sweep that stopped halfway simply re-enumerates what
is left the next time it runs — idempotent by construction, and incapable of
the stale-record drift that `MergeDeferralStore` needs `attempts` and
`last_seen_at` to survive. A branch this module merges IS recorded, in the
transcript, by `AutoMerger`'s own `auto_merge_pushed` entry.

## Failure discipline

Identical to `auto_merge.py`'s, and for the same reason: this runs at startup,
before the loop has done anything. An integration problem must never stop a
run from starting, so every failure swallows to a transcript entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import auto_merge
from .auto_merge import AutoMerger
from .config import AutoloopConfig
from .errors import GitError, StateCorruptError
from .git_gateway import GitGateway
from .policy import PolicyEngine
from .tasks import TaskState
from .transcript import TranscriptLogger
from .worktask import TaskExecutionStore

#: Outcome slugs, used verbatim as the tail of the transcript entry type
#: (`merge_sweep_<slug>`), so a log grep and a test assertion name the same
#: thing — the same convention `auto_merge.py` follows.
SWEPT = "swept"                  # every outstanding branch reached the base
NOTHING_TO_DO = "nothing_to_do"  # no completed, published, unmerged branch
DEFERRED = "deferred"            # the gate was shut: NOTHING was attempted
STOPPED = "stopped"              # a branch did not land; the rest are untouched
DISABLED = "disabled"            # policy.auto_merge_enabled is false
FAILED = "failed"                # the sweep itself could not run

#: The only two per-branch outcomes the sweep continues past. Everything else
#: — conflict, failed verification, deferral, an unexpected skip — halts it.
#: `already_integrated` is a success: merging an earlier branch can carry a
#: later one in with it, which is exactly what building on it means.
_CONTINUE_ON = (auto_merge.MERGED, auto_merge.ALREADY_INTEGRATED)


@dataclass(frozen=True)
class SweepCandidate:
    """One completed task whose published branch is not in the base yet."""

    task_id: str
    candidate_sha: str
    dest_ref: str
    #: `(group, timestamp, task_id)` — see the module docstring's order section.
    #: `group` is 0 for a record with no `published_at` (older than any record
    #: that has one) and 1 otherwise.
    order: tuple


@dataclass
class SweepResult:
    """What one sweep did. Returned for the CLI to print and for tests to
    assert on; the transcript is what an operator greps after the fact."""

    outcome: str
    #: Branches that reached the base, in the order they were attempted.
    merged: list[str] = field(default_factory=list)
    #: Branches still outstanding: the gate shut before any of them was tried,
    #: or the sweep stopped — in which case the one it stopped ON leads the
    #: list, since it did not land either. Named so "the rest were left alone"
    #: is a checkable claim rather than an absence.
    pending: list[str] = field(default_factory=list)
    #: `(task_id, reason)` for a completed, unmerged task whose publication
    #: could not be confirmed against the remote. Not attempted, not fatal.
    skipped: list[tuple[str, str]] = field(default_factory=list)
    #: The branch that halted the sweep, and the `auto_merge` outcome it got.
    stopped_on: str = ""
    stopped_outcome: str = ""
    #: The gate's reasons, when the whole sweep was deferred.
    reasons: list[str] = field(default_factory=list)

    @property
    def is_clear(self) -> bool:
        """Is there provably nothing left unmerged? The ONE definition both
        callers use for their exit code and their output.

        A skipped branch counts as not-clear, and that is the whole point of
        this being a property rather than an outcome comparison. `skipped`
        holds branches whose publication could not be CONFIRMED, which covers
        both "the ref is gone" and "the remote could not be reached" —
        `_candidate_publication` deliberately does not distinguish them,
        because an unverifiable answer is not an answer. Reporting an offline
        run as `nothing_to_do` would say "I looked, the backlog is clear" when
        the truth is "I could not look": the 2026-08-06 invisibility rebuilt
        one layer up, in the tool written to end it.
        """
        return self.outcome in (SWEPT, NOTHING_TO_DO) and not self.skipped


class BacklogSweeper:
    """Enumerate, order and integrate the outstanding published branches.

    `log(entry_type, data=...)` matches `TranscriptLogger.append` and
    `Orchestrator._log`, so the caller passes whichever it already holds and
    every sweep lands in the same transcript as everything else.

    `merger` exists for tests that want to observe the attempt ORDER without a
    real merge; production leaves it `None` and gets a real `AutoMerger` built
    from the same collaborators.
    """

    def __init__(
        self,
        *,
        config: AutoloopConfig,
        git: GitGateway,
        policy: PolicyEngine,
        execution_store: TaskExecutionStore,
        registry,
        log,
        merger=None,
    ):
        self._config = config
        self._git = git
        self._policy = policy
        self._execution_store = execution_store
        self._registry = registry
        self._log = log
        self._merger = merger or AutoMerger(
            config=config,
            git=git,
            policy=policy,
            execution_store=execution_store,
            registry=registry,
            log=log,
        )

    # ---- entry point --------------------------------------------------------

    def sweep(self) -> SweepResult:
        """Integrate every outstanding branch, oldest first, stopping at the
        first one that does not land. Never raises — see the module docstring.
        """
        if not self._policy.config.auto_merge_enabled:
            return SweepResult(outcome=DISABLED)
        #: Confirmed publications, memoized for this invocation only, exactly
        #: as `_cmd_merge_window` and `AutoMerger.after_completion` do it. It
        #: is passed to BOTH the enumeration and every `attempt`, so a branch
        #: costs one `ls-remote` for the whole sweep rather than one per phase.
        seen: set = set()
        result = SweepResult(outcome=NOTHING_TO_DO)
        try:
            candidates = self._backlog(seen, result)
        except Exception as exc:      # noqa: BLE001 - a sweep must not stop a run
            self._log("merge_sweep_error", data={"error": f"{type(exc).__name__}: {exc}"})
            return SweepResult(outcome=FAILED)
        if not candidates:
            return result

        result.pending = [c.task_id for c in candidates]
        self._log(
            "merge_sweep_backlog",
            data={
                "pending": list(result.pending),
                "detail": [
                    {"task_id": c.task_id, "candidate_sha": c.candidate_sha,
                     "dest_ref": c.dest_ref}
                    for c in candidates
                ],
            },
        )

        # THE GATE, once, for the whole sweep. See the module docstring.
        from . import cli

        try:
            reasons, notes = cli._merge_window_blockers(self._config, seen, self._git)
        except Exception as exc:      # noqa: BLE001 - fail closed, never merge
            self._log("merge_sweep_error", data={"error": f"{type(exc).__name__}: {exc}"})
            return SweepResult(
                outcome=FAILED, pending=result.pending, skipped=result.skipped
            )
        for note in notes:
            self._log("merge_sweep_window_note", data={"note": note})
        if reasons:
            result.outcome = DEFERRED
            result.reasons = list(reasons)
            self._log(
                "merge_sweep_deferred",
                data={"reasons": list(reasons), "pending": list(result.pending)},
            )
            return result

        for index, candidate in enumerate(candidates):
            outcome = self._attempt(candidate, seen)
            if outcome not in _CONTINUE_ON:
                result.outcome = STOPPED
                result.stopped_on = candidate.task_id
                result.stopped_outcome = outcome
                result.pending = [c.task_id for c in candidates[index:]]
                self._log(
                    "merge_sweep_stopped",
                    data={
                        "task_id": candidate.task_id,
                        "outcome": outcome,
                        "merged": list(result.merged),
                        "remaining": list(result.pending),
                    },
                )
                return result
            result.merged.append(candidate.task_id)

        result.outcome = SWEPT
        result.pending = []
        self._log("merge_sweep_completed", data={"merged": list(result.merged)})
        return result

    def _attempt(self, candidate: SweepCandidate, seen: set) -> str:
        """One branch, through the shared merge machinery. An exception is an
        outcome like any other here — it stops the sweep, because a branch that
        blew up mid-merge is exactly the state nothing further should be
        stacked onto."""
        try:
            return self._merger.attempt(candidate.task_id, seen)
        except Exception as exc:      # noqa: BLE001 - deliberate; see the docstring
            self._log(
                "merge_sweep_error",
                data={
                    "task_id": candidate.task_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            return auto_merge.FAILED

    # ---- enumeration --------------------------------------------------------

    def _backlog(self, seen: set, result: SweepResult) -> list[SweepCandidate]:
        """Every completed task whose published candidate is not an ancestor of
        the base head, ordered oldest publication first.

        Ordered cheapest-first, like `cli._candidate_is_retired`: registry
        state, then a local file read, then local git, and only then the
        remote. A task that fails an earlier check never costs a round-trip.
        """
        from . import cli

        head = self._git.head_sha()
        candidates: list[SweepCandidate] = []
        for task in self._registry.all_tasks():
            task_id = task.id
            if self._registry.state_of(task_id) is not TaskState.COMPLETED:
                continue
            try:
                record = self._execution_store.load(task_id)
            except (StateCorruptError, OSError) as exc:
                # Loud, and not fatal to the sweep: an unreadable record is one
                # branch this cannot see, not a reason to leave the other six
                # unmerged. It is named so the operator can repair it.
                self._log(
                    "merge_sweep_error", data={"task_id": task_id, "error": str(exc)}
                )
                continue
            if record is None or not record.candidate_sha:
                continue
            # ANCESTRY, and nothing else, decides merged-ness. Silent on
            # purpose: a branch already in the base is the ordinary case for
            # every task the loop has ever completed, and one log line each
            # would bury the handful that actually need integrating.
            if self._is_integrated(head, record.candidate_sha):
                continue
            published, why_not = cli._candidate_publication(
                self._config, _as_record_dict(record), seen, self._git
            )
            if not published:
                # Completed + unmerged + no branch on the remote carrying this
                # candidate. `_mark_task_completed` only fires on a confirmed
                # publication, so this means the ref was deleted or force-moved
                # afterwards — or the remote cannot be reached right now. Named
                # rather than merged: there is nothing here this module is
                # allowed to integrate, and inventing a branch to merge from a
                # record's own claim is the fail-open reading
                # `_candidate_publication` exists to refuse.
                result.skipped.append((task_id, why_not))
                self._log(
                    "merge_sweep_skipped", data={"task_id": task_id, "reason": why_not}
                )
                continue
            candidates.append(
                SweepCandidate(
                    task_id=task_id,
                    candidate_sha=record.candidate_sha,
                    dest_ref=record.intended_remote_ref,
                    order=self._publication_order(task_id, record),
                )
            )
        candidates.sort(key=lambda c: c.order)
        return candidates

    def _is_integrated(self, head: str, candidate_sha: str) -> bool:
        """Is `candidate_sha` already in the base? An object git cannot resolve
        answers False: a commit that is an ancestor of HEAD is in this object
        database by definition, so "cannot resolve" IS "not integrated here"."""
        try:
            return self._git.is_descendant(head, candidate_sha)
        except (GitError, OSError):
            return False

    def _publication_order(self, task_id: str, record) -> tuple:
        """`(group, timestamp, task_id)`. See the module docstring."""
        stamp = _parse_iso(getattr(record, "published_at", ""))
        if stamp is not None:
            return (1, stamp, task_id)
        # No usable `published_at`: the record predates the field, so it is
        # older than anything that has one. The candidate's committer date
        # orders that older group among itself.
        return (0, self._commit_timestamp(record.candidate_sha), task_id)

    def _commit_timestamp(self, sha: str) -> float:
        """The candidate's committer timestamp, or 0.0 when the checkout
        cannot read it (an object it has never fetched, most often). Uses the
        `read_commit` the gateway already exposes — a sweep is not a reason to
        widen the git whitelist."""
        try:
            ident = self._git.read_commit(sha).get("committer", "")
        except (GitError, OSError):
            return 0.0
        return _ident_timestamp(ident)


# ---- helpers ----------------------------------------------------------------


def _as_record_dict(record) -> dict:
    """`_candidate_publication` reads its input with `.get()`, so it takes a
    plain dict. Built by hand from the three fields it actually reads rather
    than `dataclasses.asdict`, which would copy the whole record (including
    the report text) on every enumeration."""
    return {
        "candidate_sha": record.candidate_sha,
        "intended_remote": record.intended_remote,
        "intended_remote_ref": record.intended_remote_ref,
    }


def _parse_iso(value: str) -> float | None:
    """Epoch seconds for an ISO-8601 stamp, or None when there isn't one to
    read. A naive stamp is read as UTC — `state.utcnow_iso` only ever writes
    aware ones, and guessing the local zone for a legacy value would make the
    order depend on where the loop happens to run."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _ident_timestamp(ident: str) -> float:
    """The unix seconds out of a git ident (`Name <email> <unix-ts> <tz>`).
    0.0 for anything that does not parse — an unorderable record sorts first,
    which for this group is also the fail-safe direction (see the module
    docstring: a wrong order costs a stalled sweep, never a bad base)."""
    parts = ident.split()
    if len(parts) < 2:
        return 0.0
    try:
        return float(int(parts[-2]))
    except ValueError:
        return 0.0


# ---- construction -----------------------------------------------------------


def sweep_backlog(config: AutoloopConfig, *, git=None, log=None) -> SweepResult:
    """Build the collaborators and sweep. The single entry point for both
    callers — `run`'s startup hook and the `merge-backlog` command — so the
    two cannot drift into sweeping different things.

    `GitGateway(Path.cwd(), ...)` matches every other gateway construction in
    `cli.py`: the operator runs the command from the checkout, and the loop
    process runs there too.
    """
    from . import cli

    policy = PolicyEngine(config.policy)
    gateway = git if git is not None else GitGateway(Path.cwd(), policy)
    logger = log if log is not None else TranscriptLogger(config.transcript_file).append
    _, registry = cli._load_tasks(config)
    return BacklogSweeper(
        config=config,
        git=gateway,
        policy=policy,
        execution_store=TaskExecutionStore(config.executions_dir),
        registry=registry,
        log=logger,
    ).sweep()


def sweep_on_startup(config: AutoloopConfig, *, git=None, log=None) -> SweepResult:
    """`sweep_backlog` with the outer guard the startup path needs: nothing
    here — not a corrupt registry, not an unreadable config, not a git that
    will not answer — may stop a run from starting. The sweep's own internals
    already swallow; this covers the CONSTRUCTION, the same way
    `orchestrator._auto_merge_after_completion` wraps `AutoMerger`'s.
    """
    if not config.policy.auto_merge_enabled:
        return SweepResult(outcome=DISABLED)
    try:
        return sweep_backlog(config, git=git, log=log)
    except Exception as exc:      # noqa: BLE001 - a sweep must not stop a run
        try:
            entry = log or TranscriptLogger(config.transcript_file).append
            entry("merge_sweep_error", data={"error": f"{type(exc).__name__}: {exc}"})
        except Exception:         # noqa: BLE001 - logging the failure may fail too
            pass
        return SweepResult(outcome=FAILED)
