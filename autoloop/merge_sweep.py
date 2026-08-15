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
This module CALLS that method once per branch. It contributes exactly four
things `attempt` has no way to know about, all of which are properties of the
SEQUENCE rather than of any one merge:

1. **Enumeration** — which branches are outstanding at all.
2. **Order** — oldest publication first.
3. **Stopping** — the whole sweep halts at the first branch that does not land.
4. **Freshness** — `attempt` cannot tell whether the publication evidence it
   was handed came from a moment ago or from before the merges that preceded
   it, because it only ever sees one task. The sweep can, so the sweep is where
   the memoized answer is invalidated per branch (`_reconfirm`).

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

## "Could not look" is never "nothing to merge"

A completed task has THREE possible answers here, not two: its branch is in the
base, its branch is outstanding, or **the sweep could not tell**. The third has
to survive into the exit code, or it silently collapses into the first — the
2026-08-06 invisibility rebuilt one layer up, inside the tool written to end it.

`SweepResult.unresolved` is that third answer. Four states reach it, and what
they share is that being COMPLETED implies a confirmed publication
(`_mark_task_completed` fires on nothing else) — so for each of them there is
very likely a branch out there, and this module cannot name it:

* **The remote does not confirm the branch.** The ref was deleted or
  force-moved after completion, or the remote is unreachable right now.
  `_candidate_publication` cannot tell those apart and is not asked to: an
  unverifiable answer is not an answer.
* **The execution record cannot be READ** — a torn file, an I/O error, a
  permission failure. Named since 2026-08-15; before that it was logged and
  then dropped, so a sweep whose only completed task had a corrupt record
  reported `nothing_to_do` and exited 0 having inspected nothing.
* **There is no live record, and no archived one proves the work landed.**
  `_retired_publication_is_integrated` explains why the archive is read here
  when the merge window deliberately ignores it.
* **The record loads but names no candidate.** Completion implies a candidate,
  so such a record cannot be describing the publication completion implies.

None of the four is ATTEMPTED: there is nothing here this module is allowed to
merge, and inventing a branch out of a record's own claim is the fail-open
reading `_candidate_publication` exists to refuse. None of them HALTS the sweep
either — nothing has been mutated, so this is not the half-done state stopping
exists to prevent. Each leaves a hole in the publication order, which is safe
for the same reason a wrong order is: the next branch either applies or
conflicts, and a conflict stops cleanly.

But every one of them makes the run not-clear. `SweepResult.is_clear` is false
whenever `unresolved` is non-empty, so `merge-backlog` exits 1 and the startup
hook prints rather than staying quiet. Exit 0 means "I looked, and there is
provably nothing outstanding", never "I could not look".

## Enumeration evidence is not mutation authority

`seen` memoizes CONFIRMED publications so a long-running command does not
re-ask the remote about the same ref forever (`cli._candidate_publication`
documents the cache and why negatives are never cached). Sharing one such cache
between the ENUMERATION and the MERGES would quietly change what it means:
branch N would be merged on an `ls-remote` taken before branches 1..N-1 were
attempted, so a delete or force-move in between — another operator, a
`release`, a CI job — is hidden by the positive cache instead of caught by it.
A seven-branch sweep is minutes of merging and pushing; that window is real.

So each candidate's own key is EVICTED from `seen` immediately before its own
merge (`_reconfirm`), which forces one live `ls-remote` for that branch at the
moment it matters; re-adding it on success means `AutoMerger.attempt`'s own
publication check (its step 3) costs nothing on top. A ref that no longer
carries the reviewed candidate yields `UNCONFIRMED`, which is not in
`_CONTINUE_ON`: that branch is not merged, and neither is anything after it.

That is deliberately harsher than the enumeration-time version of the same
finding, which is merely named and passed over. A ref that changed WHILE the
sweep was running means something else is mutating the remote right now, and
that invalidates the whole enumeration rather than one entry of it. The other
way this answer comes back False — a transient `ls-remote` failure partway
through — stops the sweep for the plainer reason: mid-sweep is the one moment
when "the remote stopped answering" and "the ref moved" are indistinguishable
AND the base has already changed under the remaining candidates, so fail-closed
means halt. Nothing is lost either way; the next run re-enumerates from git.

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

import json
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

#: A PER-BRANCH outcome this module mints itself rather than getting back from
#: `auto_merge`: the remote no longer confirmed the candidate at the moment its
#: own merge was about to run. Deliberately not an `auto_merge` slug — nothing
#: reached `AutoMerger.attempt`, so claiming one of its outcomes would say the
#: merge machinery decided something it never saw.
UNCONFIRMED = "publication_unconfirmed"

#: The only two per-branch outcomes the sweep continues past. Everything else
#: — conflict, failed verification, deferral, a publication that stopped being
#: confirmed, an unexpected skip — halts it. `already_integrated` is a success:
#: merging an earlier branch can carry a later one in with it, which is exactly
#: what building on it means.
_CONTINUE_ON = (auto_merge.MERGED, auto_merge.ALREADY_INTEGRATED)


@dataclass(frozen=True)
class SweepCandidate:
    """One completed task whose published branch is not in the base yet."""

    task_id: str
    candidate_sha: str
    #: Where the enumeration confirmed the candidate, kept so the merge can
    #: re-ask the SAME ref without reloading the record. See `_reconfirm`.
    remote: str
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
    #: `(task_id, reason)` for a completed task the sweep could not JUDGE —
    #: an unconfirmed publication, an unreadable record, a retired record whose
    #: work cannot be shown to have landed, a record naming no candidate. Not
    #: attempted, not fatal, and never counted as clear. See the module
    #: docstring's "could not look" section for why all four belong together.
    unresolved: list[tuple[str, str]] = field(default_factory=list)
    #: The branch that halted the sweep, and the outcome it got — an
    #: `auto_merge` slug, or `UNCONFIRMED` when it never reached the merger.
    stopped_on: str = ""
    stopped_outcome: str = ""
    #: The gate's reasons, when the whole sweep was deferred.
    reasons: list[str] = field(default_factory=list)

    @property
    def is_clear(self) -> bool:
        """Is there provably nothing left unmerged? The ONE definition both
        callers use for their exit code and their output.

        An unresolved task counts as not-clear, and that is the whole point of
        this being a property rather than an outcome comparison. `unresolved`
        holds every completed task the sweep could not JUDGE — an unverifiable
        publication, a record it could not read, a retired record whose work it
        cannot show landed. All of them mean "I could not look", and reporting
        one of those runs as `nothing_to_do` would say "I looked, the backlog is
        clear" instead: the 2026-08-06 invisibility rebuilt one layer up, in the
        tool written to end it.
        """
        return self.outcome in (SWEPT, NOTHING_TO_DO) and not self.unresolved


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
        #: spares the OTHER records the gate re-reads on every branch a repeat
        #: round-trip. It is emphatically NOT authority for the branch about to
        #: be merged: `_reconfirm` evicts that key first, so every merge runs on
        #: an `ls-remote` taken after the previous merge, not before the sweep.
        seen: set = set()
        result = SweepResult(outcome=NOTHING_TO_DO)
        try:
            candidates = self._backlog(seen, result)
        except Exception as exc:      # noqa: BLE001 - a sweep must not stop a run
            self._log("merge_sweep_error", data={"error": f"{type(exc).__name__}: {exc}"})
            # Whatever the enumeration managed to judge before it blew up is
            # carried out with it, exactly as the gate-failure path below does:
            # the run is not clear either way, but the tasks it could not judge
            # are the ones an operator has to go and look at.
            return SweepResult(outcome=FAILED, unresolved=result.unresolved)
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
                outcome=FAILED, pending=result.pending, unresolved=result.unresolved
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
        """One branch, through the shared merge machinery, on publication
        evidence taken NOW rather than at enumeration.

        An exception is an outcome like any other here — it stops the sweep,
        because a branch that blew up mid-merge is exactly the state nothing
        further should be stacked onto."""
        try:
            published, why_not = self._reconfirm(candidate, seen)
            if not published:
                self._log(
                    "merge_sweep_publication_changed",
                    data={
                        "task_id": candidate.task_id,
                        "candidate_sha": candidate.candidate_sha,
                        "dest_ref": candidate.dest_ref,
                        "reason": why_not,
                    },
                )
                return UNCONFIRMED
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

    def _reconfirm(self, candidate: SweepCandidate, seen: set) -> tuple[bool, str]:
        """Does the remote STILL carry this candidate, asked right now?

        The eviction is the whole method. `seen` was populated during
        enumeration, potentially several merges and pushes ago; leaving this
        key in it would answer "published" from a cache instead of from the
        remote, which is the one question that must never be answered from a
        cache here (see the module docstring). Re-added on success by
        `_candidate_publication` itself, so `AutoMerger.attempt`'s own check
        does not pay for a second round-trip.
        """
        from . import cli

        seen.discard((candidate.remote, candidate.dest_ref, candidate.candidate_sha))
        return cli._candidate_publication(
            self._config,
            _publication_dict(
                candidate.candidate_sha, candidate.remote, candidate.dest_ref
            ),
            seen,
            self._git,
        )

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
                # Not fatal to the sweep — an unreadable record is one branch
                # this cannot see, not a reason to leave the other six unmerged
                # — but never silent either. Completion implies a confirmed
                # publication, so the record that will not load is the only
                # thing naming a branch that may well be outstanding. Until
                # 2026-08-15 this logged and then `continue`d, so a sweep whose
                # only completed task had a torn record exited 0.
                self._unresolved(
                    result, task_id, f"its execution record could not be read ({exc})"
                )
                continue
            if record is None:
                # Retired by `retire_execution` (`release`, or
                # `_reconcile_published_execution`, which completes the task and
                # archives the record in one call). Only the archive can say
                # whether that retirement happened over work that had already
                # landed; anything less is a guess.
                integrated, why_not = self._retired_publication_is_integrated(
                    head, task_id
                )
                if not integrated:
                    self._unresolved(result, task_id, why_not)
                continue
            if not record.candidate_sha:
                # A completed task's record HAS to name the candidate that was
                # published — that is what completion means here. One that does
                # not cannot be describing this task's publication, so the
                # branch behind it is unaccounted for rather than absent.
                self._unresolved(
                    result,
                    task_id,
                    "its execution record names no candidate, though completion "
                    "means one was published",
                )
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
                self._unresolved(result, task_id, why_not)
                continue
            candidates.append(
                SweepCandidate(
                    task_id=task_id,
                    candidate_sha=record.candidate_sha,
                    remote=record.intended_remote,
                    dest_ref=record.intended_remote_ref,
                    order=self._publication_order(task_id, record),
                )
            )
        candidates.sort(key=lambda c: c.order)
        return candidates

    def _unresolved(self, result: SweepResult, task_id: str, why_not: str) -> None:
        """Record a completed task the sweep could not judge. ONE funnel, so
        every such state reaches the same list, the same transcript entry and
        therefore the same exit code — the four of them differ only in the
        reason string, and a second path that forgot one of those three is how
        the corrupt-record case came to exit 0."""
        result.unresolved.append((task_id, why_not))
        self._log("merge_sweep_unresolved", data={"task_id": task_id, "reason": why_not})

    def _retired_publication_is_integrated(
        self, head: str, task_id: str
    ) -> tuple[bool, str]:
        """A COMPLETED task with no live execution record: is its work provably
        in the base already? `(True, "")` only when it demonstrably is.

        Why the archive is read here at all, when `cli._merge_window_blockers`
        deliberately does NOT recurse into it: the two ask different questions.
        The gate asks "could moving the base strand this?", and a retired record
        describes work nobody is going to build on, so ignoring it is right.
        This asks "is this branch in the base?", which retirement does not
        answer either way — `retire_execution` files a record away on
        publication being CONFIRMED, not on it having been merged, and with
        `auto_merge_enabled` off nothing merges it at all. Skip the archive and
        every such branch becomes invisible again, which is the whole failure
        this module exists to end.

        Read as raw JSON rather than through `TaskExecutionStore.load`: an
        archived record can predate any field this dataclass now requires, and
        `TaskExecution(**data)` would raise on it.

        **ANCESTRY decides, exactly as it does for a live record.** Either sha
        the record names being in the base answers the question, because git is
        authoritative about what is in the base and needs no second opinion.
        `published_sha` is CORROBORATION — it is the one field meaning "the
        remote confirmed this", written by `_dispatch_task_push` from an
        `ls-remote` — but requiring it would be a stricter rule than the live
        path applies, and a stricter rule with a date on it: the field only
        exists from 2026-08-15, so every archive written before then would be
        permanently unresolved with no action an operator could take, even with
        its candidate demonstrably merged. Wolf-crying, on exactly the records
        this branch exists to stop wolf-crying about.

        Every archived record for the task is considered and the first
        integrated one wins, so this does not depend on sorting `<reason>-<stamp>`
        labels chronologically (they are not lexicographically ordered across
        different reasons). Nothing here is ever merged FROM: an archived record
        is not a merge source, because `AutoMerger.attempt` reads the LIVE
        record and would skip the task anyway. The honest report is the output.
        """
        archive = Path(self._execution_store.directory) / "archive"
        try:
            paths = sorted(archive.glob(f"{task_id}-*.json"))
        except OSError as exc:
            return False, f"its execution archive could not be listed ({exc})"
        if not paths:
            return False, (
                "it has no execution record, live or archived — nothing names "
                "the candidate its completion says was published"
            )
        unreadable: list[str] = []
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                unreadable.append(f"{path.name} ({exc})")
                continue
            if not isinstance(data, dict):
                unreadable.append(f"{path.name} (not a record)")
                continue
            for sha in (data.get("candidate_sha"), data.get("published_sha")):
                if sha and self._is_integrated(head, str(sha)):
                    return True, ""
        if unreadable:
            return False, (
                "its record was retired and the archived copy could not be "
                "read: " + "; ".join(unreadable)
            )
        return False, (
            "its record was retired and no sha any archived copy names is an "
            f"ancestor of {head[:12]} — the branch may still be outstanding; "
            "merge it by hand"
        )

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


def _publication_dict(candidate_sha: str, remote: str, dest_ref: str) -> dict:
    """`_candidate_publication` reads its input with `.get()`, so it takes a
    plain dict. Built by hand from the three fields it actually reads rather
    than `dataclasses.asdict`, which would copy the whole record (including
    the report text) on every enumeration — and, since `_reconfirm` asks the
    same question again per branch, on every merge as well."""
    return {
        "candidate_sha": candidate_sha,
        "intended_remote": remote,
        "intended_remote_ref": dest_ref,
    }


def _as_record_dict(record) -> dict:
    """The same three fields, off a `TaskExecution`."""
    return _publication_dict(
        record.candidate_sha, record.intended_remote, record.intended_remote_ref
    )


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
