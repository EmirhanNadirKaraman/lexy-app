"""Integrate a completed task's published side branch into the base.

Publication is not integration. `orchestrator._mark_task_completed` (B10)
retires a task the moment its reviewed candidate is confirmed on its own side
branch on the remote — the strongest claim the loop can make on its own — and
then stops. Nothing merges that branch anywhere. On **2026-08-06 seven
completed tasks were unmerged at once**, among them the tab reaper and the
Python restart module: fixes for failures that were still happening in the
running loop while their code sat on branches nobody had pulled.

This module closes that last hop. It runs immediately after a completion, it
merges the candidate into the branch the loop builds against, and it pushes
that branch. Pushing is not optional — a merge that lives only in the local
checkout is the same invisibility one level down.

Scope, so the boundary survives review:

* **This module handles completions it SEES.** The deferral queue below is its
  own retry state for those, nothing more.
* **The backlog of branches published before this existed is NOT in scope.**
  That is `merge_sweep.py`, which enumerates completed tasks and decides
  merged-ness by git ancestry. It CALLS `attempt` below once per branch rather
  than reimplementing any of this; keep it that way, and keep the enumeration
  out of here — this reacts to one event, that one looks for work nobody
  reported.

## The gate is the whole design

Merging moves HEAD, and every in-flight `TaskExecution` pins a
`task_base_sha`. `orchestrator._rebase_execution_if_stale` re-bases a record
that has not been reviewed yet, but PARKS one whose `review_round > 0` —
correctly, since discarding a candidate a reviewer has already seen is not a
decision to make quietly. On 2026-08-06 thirteen tasks held unpublished
candidates, so an eager merge at that moment would have parked thirteen of
them.

So the same predicate the operator's `merge-window` command uses gates this
one: `cli._merge_window_blockers` — no unpublished candidate bound to the
current base, no executing phase. It is CALLED, not reimplemented. A second
copy that drifted by a single case is exactly how thirteen tasks get stranded.
A published candidate does not block: its reviewed object is durable on its
own branch, so a moved base cannot discard it.

When the gate is shut the merge is **deferred**, never parked: a record goes
into `MergeDeferralStore` and the next completion retries it. A deferred merge
is a normal state, not a fault. Parking would take a loop that is working and
stop it over an integration step that can simply happen later.

## Failure discipline

Every failure here swallows to a transcript log. By the time this runs, the
push has already succeeded and the task is already completed — turning an
integration problem into a park would strand work that is safely published,
the exact "park and report, never undo" inversion `_mark_task_completed`
exists downstream of. Nothing in this module raises into the orchestrator.

Two things are still refused outright rather than worked around:

* **Conflicts.** `git merge --abort`, verify the head and the tree are exactly
  what they were, report which files conflicted. Never force, never leave a
  half-merged checkout behind.

  **One conflict shape, and only one, is resolved instead of aborted** (since
  2026-08-19, docs-01): two branches that each appended their own change-note
  lines to the terminal append-only section of `docs/SUMMARY.md` /
  `docs/TESTS.md`. Every task writes those two files, so that collision
  happened to EVERY pair of parallel branches — it halted the sweep three times
  in one evening on 2026-08-18 and left five reviewed, published tasks unmerged
  for a day. `_resolve_note_conflicts` below reads the three sides git recorded
  in the index and hands them to `note_merge.resolve_note_append`, which
  returns a combined text only if each side changed nothing but an append to
  that section. Anything else — a conflict in the prose above it, an edited or
  reordered existing note line, a conflicted path outside those two files —
  returns `None` and falls straight through to the same abort as before.
  Nothing else in this module's conflict handling was weakened, and no other
  path is auto-resolved.
* **A remote base that has moved.** Checked BEFORE anything mutates the
  checkout, not left to `push_exact` failing afterwards — a local merge onto a
  base that is already behind the remote produces a head that cannot be
  fast-forwarded and has to be unwound by hand.

## What counts as evidence

A merge command returning 0 is not evidence the merge happened. After it, this
re-reads the head and requires all of: it moved, it contains the candidate, it
contains the old head, and the tree is clean. Only then does it push, and the
push's own confirmation is `push_exact`'s fresh `ls-remote`, never the push's
exit status.

## A merge can change the code doing the merging

Merging into the checkout does not reload a live Python process. Measured
2026-08-18: plan-01 merged a hard gate at 06:23:59 into a loop that started at
04:07:03, and dash-10 STARTED after that merge without the gate ever applying;
brw-11's browser fix, merged 00:58, was inert the same way all night. The loop
can ship improvements to itself that it then cannot use.

This module contributes exactly the SIGNAL for that, and nothing else. When a
verified merge touches any path under `autoloop/`, `_note_loop_code_merge`
writes one `PendingUpgrade` record naming the sha the process would have to be
running. It does not preflight anything, and it emphatically does not replace
the process: this code runs mid-round, with a task just published and the
session about to prepare its next request. Deciding, and the `os.execv` itself,
belong to the between-round boundary — `orchestrator.run` offers it,
`cli._self_upgrade_at_boundary` performs it. Recording is a bookkeeping step
under the same discipline as everything else here: it can log, it can decline,
it can never turn a real merge into a reported failure.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from . import note_merge
from .config import AutoloopConfig
from .errors import GitError, StateCorruptError
from .git_gateway import GitGateway
from .policy import PolicyEngine
from .state import utcnow_iso
from .tasks import TaskRegistry, TaskState
from .worktask import TaskExecutionStore

#: Outcome slugs. Returned from `AutoMerger.attempt` and used verbatim as the
#: transcript entry type (prefixed `auto_merge_`), so a log grep and a test
#: assertion name the same thing.
MERGED = "merged"                    # merged and pushed — the whole job
ALREADY_INTEGRATED = "already_integrated"   # head already contains it, base already pushed
DEFERRED = "deferred"                # gate shut / base moved / dirty — retry later
CONFLICT = "conflict"                # aborted cleanly, base unchanged
FAILED = "failed"                    # something went wrong; nothing was pushed
SKIPPED = "skipped"                  # not applicable (no record, not completed, ...)
DISABLED = "disabled"                # policy.auto_merge_enabled is false

#: Everything under here is the loop's OWN code. A merge that touches one of
#: these paths changed the program this process is running, and a running
#: Python process does not notice: modules were loaded at startup and merging
#: into the checkout does not reload them. Measured 2026-08-18 — plan-01's hard
#: gate merged at 06:23:59 into a process started at 04:07:03, and dash-10
#: started AFTER the merge without it; brw-11's browser fix, merged 00:58, was
#: inert for the whole night the same way.
LOOP_CODE_PREFIX = "autoloop/"

#: `PendingUpgrade.status`, in the order one record moves through them. A
#: record only ever leaves `pending` by being SETTLED (or cleared), which is
#: what makes the exec one-shot — see `UpgradeStore`.
UPGRADE_PENDING = "pending"            # a merge changed loop code; not acted on yet
UPGRADE_EXECED = "execed"              # the `os.execv` did not return: a successor is running
UPGRADE_PREFLIGHT_FAILED = "preflight_failed"   # the merged tree does not import
UPGRADE_UNAPPLICABLE = "unapplicable"  # the merged checkout is not the running tree
UPGRADE_EXEC_FAILED = "exec_failed"    # nothing was replaced; this process carries on


@dataclass
class MergeDeferral:
    """One completed task whose integration has not happened yet.

    Keyed by task id (one file per task), because a task has exactly one
    candidate worth integrating at a time — a second deferral for the same
    task is the same fact seen again, and `attempts` records that rather than
    minting a duplicate. Same reasoning as `BlockerStore.record`'s idempotent
    upsert.
    """

    task_id: str
    candidate_sha: str
    #: The side branch the candidate was published to, for the operator who
    #: wants to finish the merge by hand.
    dest_ref: str
    #: The base head at the moment of deferral — what the merge WOULD have
    #: moved. Recorded so a stale deferral is recognisable as stale.
    base_sha: str
    reason: str
    created_at: str
    last_seen_at: str = ""
    attempts: int = 1


class MergeDeferralStore:
    """One JSON file per task id under `directory`, atomic temp + `os.replace`
    — the same shape as `BlockerStore` and `TaskExecutionStore`.

    A corrupt record RAISES rather than reading as absent, for the reason
    every other store in this package does: reading a deferral as "not there"
    would silently drop the retry, and a dropped retry is indistinguishable
    from the unmerged-forever state this module exists to end.
    """

    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, task_id: str) -> Path:
        return self.directory / f"{task_id}.json"

    def save(self, deferral: MergeDeferral) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(deferral.task_id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(asdict(deferral), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def load(self, task_id: str) -> MergeDeferral | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            return MergeDeferral(**json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StateCorruptError(f"merge deferral {path} is unreadable: {exc}") from exc

    def all_deferrals(self) -> list[MergeDeferral]:
        """Every deferral on disk, oldest first by `created_at` so a queue
        that built up over several sessions retries in the order it formed."""
        if not self.directory.exists():
            return []
        loaded = [self.load(path.stem) for path in sorted(self.directory.glob("*.json"))]
        return sorted((d for d in loaded if d is not None), key=lambda d: d.created_at)

    def record(self, *, task_id, candidate_sha, dest_ref, base_sha, reason, now):
        """Idempotent upsert — bumps `attempts` on an existing record rather
        than creating a second one for the same task."""
        existing = self.load(task_id)
        if existing is not None:
            existing.candidate_sha = candidate_sha
            existing.dest_ref = dest_ref
            existing.base_sha = base_sha
            existing.reason = reason
            existing.last_seen_at = now
            existing.attempts += 1
            self.save(existing)
            return existing
        fresh = MergeDeferral(
            task_id=task_id,
            candidate_sha=candidate_sha,
            dest_ref=dest_ref,
            base_sha=base_sha,
            reason=reason,
            created_at=now,
            last_seen_at=now,
        )
        self.save(fresh)
        return fresh

    def clear(self, task_id: str) -> None:
        self._path(task_id).unlink(missing_ok=True)


@dataclass
class PendingUpgrade:
    """One merge that changed the loop's own code, and what became of it.

    ONE record, not one per task: the question it answers is "is the process
    running the code in this checkout", which has a single answer at a time.
    A later merge overwrites it, because that later merge is a DIFFERENT
    upgrade rather than a retry of the earlier one.
    """

    #: The base head AFTER the merge — the exact commit whose code a fresh
    #: interpreter would load. Recorded because the one-shot rule is keyed on
    #: it: a sha that has already been exec'd for is never exec'd for again.
    base_sha: str
    #: The base head before the merge, so a transcript reader can see the move.
    previous_base_sha: str
    #: The task's reviewed candidate that was merged.
    candidate_sha: str
    task_id: str
    #: The checkout the merge moved (`GitGateway.repo_root`). Compared against
    #: the package root this process actually imported from before anything is
    #: replaced: if they differ, the merge changed code this process does not
    #: run, and re-execing would accomplish nothing.
    repo_root: str
    #: The `autoloop/` paths the merge touched, capped for the record's sake.
    #: Evidence for the operator, never re-derived from.
    paths: list
    status: str
    recorded_at: str
    #: When the record left `pending`, and why. `detail` carries the preflight
    #: error when there is one — the whole point of a failed preflight is that
    #: it is REPORTED rather than fatal.
    settled_at: str = ""
    detail: str = ""


class UpgradeStore:
    """The single `pending_upgrade.json`, written atomically (temp +
    `os.replace`) like every other store here.

    **A record that cannot be read is NO record**, which is the opposite of
    `MergeDeferralStore`'s rule and deliberately so. There, absent means a
    dropped retry — the invisibility the module exists to end — so a corrupt
    file has to raise. Here, absent means "do not replace the process", and
    refusing to act is the safe direction for an unreadable marker: the merged
    code is on disk either way and the next process start picks it up. Raising
    instead would take a bookkeeping file and park a loop that is working.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def save(self, record: PendingUpgrade) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def load(self) -> PendingUpgrade | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return PendingUpgrade(**data)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def loop_code_paths(paths) -> list:
    """The subset of `paths` that is the loop's own code.

    Literal prefix match on `autoloop/`, tests included: the claim is "any file
    under `autoloop/`", and a merge that only changes this package's tests
    still changes what `python -m autoloop` imports. Narrowing it to
    "non-test" would narrow the claim.
    """
    return sorted(p for p in paths if p == "autoloop" or p.startswith(LOOP_CODE_PREFIX))


class AutoMerger:
    """Merges completed tasks into the base. One instance per completion
    event; `after_completion` is the only entry point the orchestrator uses.

    `log(entry_type, data=...)` matches `Orchestrator._log`'s signature so the
    orchestrator can pass its own bound method and every merge and deferral
    lands in the same transcript as everything else, with task id, sha and
    reason — which is what the operator greps when a branch is missing.
    """

    def __init__(
        self,
        *,
        config: AutoloopConfig,
        git: GitGateway,
        policy: PolicyEngine,
        execution_store: TaskExecutionStore,
        registry: TaskRegistry,
        log,
        deferrals: MergeDeferralStore | None = None,
        upgrades: "UpgradeStore | None" = None,
    ):
        self._config = config
        self._git = git
        self._policy = policy
        self._execution_store = execution_store
        self._registry = registry
        self._log = log
        self._deferrals = deferrals or MergeDeferralStore(config.merge_deferrals_dir)
        #: Where a merge that changed the loop's own code is recorded. Written
        #: here and read at the restart boundary (`orchestrator.run`), never
        #: acted on inside this module: merging and replacing the process are
        #: two different moments, and this one is mid-round by construction.
        self._upgrades = upgrades or UpgradeStore(config.pending_upgrade_file)

    # ---- entry point --------------------------------------------------------

    def after_completion(self, task_id: str) -> dict[str, str]:
        """Retry every earlier deferral, then integrate `task_id`.

        Draining FIRST is what makes "retry after the next completion" real:
        the deferred tasks were blocked by a condition (a gate, a moved base)
        that the intervening work may well have cleared, and they are older,
        so they go first.

        Returns `{task_id: outcome}` for the caller's benefit; the orchestrator
        ignores it and reads the transcript instead. Never raises — see the
        module docstring.
        """
        outcomes: dict[str, str] = {}
        if not self._policy.config.auto_merge_enabled:
            return {task_id: DISABLED}
        #: Confirmed publications, memoized for this invocation only, exactly
        #: as `_cmd_merge_window` does it — a drain of five deferrals would
        #: otherwise re-ask the remote about the same published candidates
        #: five times over.
        seen: set = set()
        try:
            pending = [d.task_id for d in self._deferrals.all_deferrals()]
        except (StateCorruptError, OSError) as exc:
            self._log("auto_merge_error", data={"task_id": task_id, "error": str(exc)})
            pending = []
        for pending_id in pending:
            if pending_id == task_id:
                continue        # handled below, from its live execution record
            outcomes[pending_id] = self._guarded_attempt(pending_id, seen)
        outcomes[task_id] = self._guarded_attempt(task_id, seen)
        return outcomes

    def _guarded_attempt(self, task_id: str, seen: set) -> str:
        """`attempt`, with the module's failure discipline around it: the push
        already succeeded, so nothing that happens here may propagate."""
        try:
            return self.attempt(task_id, seen)
        except Exception as exc:      # noqa: BLE001 - deliberate; see the module docstring
            self._log(
                "auto_merge_error",
                data={"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"},
            )
            return FAILED

    # ---- one task -----------------------------------------------------------

    def attempt(self, task_id: str, seen: set | None = None) -> str:
        """Integrate one completed task. Returns an outcome slug.

        The order below is load-bearing: **nothing mutates the checkout until
        every precondition has passed.** Reading a record, resolving an object
        and asking the remote where its base is are all safe to abandon
        halfway; a merge is not.
        """
        seen = set() if seen is None else seen
        if not self._policy.config.auto_merge_enabled:
            return DISABLED

        # 1. Is this task ours to integrate at all?
        if not self._registry.has(task_id):
            return self._skip(task_id, "task is not in the registry")
        if self._registry.state_of(task_id) is not TaskState.COMPLETED:
            # A task quarantined between publication and here records an
            # operator decision. Merging over it would act on work the
            # operator has explicitly set aside.
            return self._skip(
                task_id, f"task is {self._registry.state_of(task_id).value}, not completed"
            )
        execution = self._execution_store.load(task_id)
        if execution is None or not execution.candidate_sha:
            return self._skip(task_id, "no execution record with a candidate")
        candidate = execution.candidate_sha
        record = asdict(execution)

        # 2. The base. `current_branch()` is authoritative — the loop merges
        #    into whatever it is actually building against, and a detached
        #    HEAD has no branch to push.
        base_branch = self._git.current_branch()
        if not base_branch:
            return self._defer(execution, "HEAD is detached — no base branch to merge into")
        base_ref = f"refs/heads/{base_branch}"
        # `branch.<name>.remote` is where git itself would push this branch.
        # Falling back to "origin" rather than refusing keeps a checkout with
        # no upstream configured working, which is the common case for a
        # branch the loop created.
        remote = self._git.config_get(f"branch.{base_branch}.remote") or "origin"

        # 3. Publication, re-confirmed against the remote. `mark_completed`
        #    already required it once, but a deferral can be days old and a
        #    force-push in between would mean the branch no longer carries
        #    this candidate. Fail-closed: only an `ls-remote` equal to the
        #    candidate counts.
        from . import cli

        published, why_not = cli._candidate_publication(
            self._config, record, seen, self._git
        )
        if not published:
            return self._defer(execution, f"candidate is not published — {why_not}")

        # 4. Make the candidate object resolvable HERE. The worker repo is a
        #    separate clone, so the main checkout has never seen this commit.
        #    `fetch_object` pulls exactly that one object by literal id from a
        #    local filesystem path and creates no ref (a fetch from `origin`
        #    is not policy-legal, and would not be narrower anyway).
        if not self._ensure_object(candidate, execution.worktree_path):
            return self._defer(
                execution,
                f"candidate {candidate[:12]} does not resolve in {self._git.repo_root} "
                f"and could not be fetched from {execution.worktree_path or '(no worktree)'}",
            )

        head = self._git.head_sha()

        # 5. Has the remote base moved out from under us? BEFORE any mutation:
        #    merging onto a base the remote is already ahead of produces a head
        #    that cannot be fast-forwarded, and unwinding that needs the
        #    history rewrites this gateway deliberately cannot perform.
        try:
            remote_base = self._git.remote_ref_sha(remote, base_ref)
        except (GitError, OSError) as exc:
            return self._defer(
                execution, f"could not read {remote}/{base_ref} ({exc}) — fail closed"
            )
        if remote_base and not self._contains(head, remote_base):
            return self._defer(
                execution,
                f"{remote}/{base_ref} is at {remote_base[:12]}, which local "
                f"{base_branch} ({head[:12]}) does not contain — the base moved; "
                "reconcile it by hand first",
            )

        # 6. Already merged? Crash-recovery re-entry and a deferral that was
        #    merged-but-not-pushed both land here. The push below still runs:
        #    an unpushed merge is not an integrated one.
        already_merged = self._contains(head, candidate)

        if not already_merged:
            # 7. THE GATE. Same predicate as `merge-window`, called not copied.
            reasons, notes = cli._merge_window_blockers(self._config, seen, self._git)
            # The gate's notes are the ONLY report of a record that is being
            # ignored rather than respected — a published-but-unretired record,
            # or one a `release` left behind. `merge-window` prints them to the
            # operator; this path has no operator, so discarding them (as it did
            # until 2026-08-15) made "the window opened because a record was
            # written off" indistinguishable from "the window was simply clear".
            for note in notes:
                self._log("auto_merge_window_note", data={"task_id": task_id, "note": note})
            if reasons:
                return self._defer(execution, "merge window closed: " + "; ".join(reasons))
            if self._git.is_dirty():
                return self._defer(
                    execution,
                    "the checkout has uncommitted changes — a conflict abort could "
                    "not restore it to exactly this state",
                )

            outcome = self._merge(execution, candidate, base_branch, head)
            if outcome != MERGED:
                return outcome      # CONFLICT (aborted) or FAILED (unverified)
            merged_head = self._git.head_sha()
            # Recorded off the VERIFIED merge, before the push. The push
            # publishes the base; it does not decide what code is in this
            # checkout, and a push that is refused (a protected base branch,
            # every time) still leaves the process running code the working
            # tree no longer holds. The `already_merged` branch above records
            # nothing on purpose: the invocation that actually merged it
            # already did, and re-recording would re-offer a sha whose one-shot
            # may already be spent.
            self._note_loop_code_merge(task_id, candidate, head, merged_head)
            head = merged_head

        # 8. Push. The merge is worthless until the base is published.
        if remote_base == head:
            self._deferrals.clear(task_id)
            self._log(
                "auto_merge_already_integrated",
                data={
                    "task_id": task_id,
                    "candidate_sha": candidate,
                    "base_branch": base_branch,
                    "base_sha": head,
                    "reason": f"{remote}/{base_ref} already at {head[:12]}",
                },
            )
            return ALREADY_INTEGRATED
        return self._push(execution, candidate, base_branch, base_ref, remote, head)

    # ---- steps --------------------------------------------------------------

    def _merge(self, execution, candidate: str, base_branch: str, pre_head: str) -> str:
        """Merge, or abort cleanly. Returns `MERGED`, `CONFLICT` or `FAILED`."""
        task_id = execution.task_id
        message = f"Merge task {task_id} ({candidate[:12]}) into {base_branch}"
        try:
            self._git.merge_commit(candidate, message)
        except GitError as exc:
            # Read the conflicts BEFORE aborting — the abort clears them.
            try:
                conflicts = self._git.conflicted_paths()
            except GitError:
                conflicts = []
            # The ONE resolvable shape (see the module docstring): both sides
            # only appended change-note lines to a tracker's append-only
            # section. Anything else returns False, having touched nothing, and
            # falls through to exactly the abort this path always did.
            if not self._resolve_note_conflicts(task_id, candidate, conflicts, message):
                return self._abort(execution, candidate, pre_head, conflicts, str(exc))

        new_head = self._git.head_sha()
        problem = self._verify_merge(new_head, pre_head, candidate)
        if problem:
            # Deliberately NOT undone. `reset` is absent from the git
            # whitelist by design, and inventing a way around that to clean up
            # after a merge nobody understands is worse than stopping with an
            # accurate report. Nothing is pushed.
            self._log(
                "auto_merge_failed",
                data={
                    "task_id": task_id,
                    "candidate_sha": candidate,
                    "base_branch": base_branch,
                    "base_sha_before": pre_head,
                    "base_sha_after": new_head,
                    "reason": problem,
                },
            )
            return FAILED
        self._log(
            "auto_merge_merged",
            data={
                "task_id": task_id,
                "candidate_sha": candidate,
                "base_branch": base_branch,
                "base_sha_before": pre_head,
                "base_sha_after": new_head,
            },
        )
        return MERGED

    # ---- the one auto-resolved conflict shape -------------------------------
    #
    # Reachable only from `_merge`'s conflict branch, and only for the two
    # paths in `note_merge.NOTE_TRACKERS`. Read `note_merge.py`'s module
    # docstring before changing anything here: the rule it implements is
    # deliberately narrow, and every widening of it is a case where the sweep
    # stops asking a human.

    def _resolve_note_conflicts(self, task_id, candidate, conflicts, message) -> bool:
        """Combine two branches' appended change notes. True ONLY when the
        merge has been resolved AND committed here; False leaves the checkout
        exactly as `git merge` left it, for `_abort` to restore.

        **Every conflicted path is resolved into memory before ANY file is
        written.** A path that resolves followed by one that refuses would
        otherwise leave a rewritten tracker in a half-merged tree, and the
        abort that follows would be cleaning up after this method rather than
        after git.
        """
        if not conflicts:
            return False
        outside = [path for path in conflicts if path not in note_merge.NOTE_TRACKERS]
        if outside:
            # Not even partially: a real conflict anywhere in the merge means
            # the whole merge needs a human, so nothing is resolved.
            self._note_refusal(
                task_id, candidate, conflicts,
                f"conflicted path(s) outside the change-note trackers: {outside}",
            )
            return False

        resolutions: dict[str, str] = {}
        for path in sorted(conflicts):
            try:
                base = self._git.merge_stage_blob(path, 1).decode("utf-8")
                ours = self._git.merge_stage_blob(path, 2).decode("utf-8")
                theirs = self._git.merge_stage_blob(path, 3).decode("utf-8")
                merged = (Path(self._git.repo_root) / path).read_text(encoding="utf-8")
            except (GitError, OSError, UnicodeDecodeError) as exc:
                self._note_refusal(
                    task_id, candidate, conflicts,
                    f"could not read all three sides of {path}: "
                    f"{type(exc).__name__}: {exc}",
                )
                return False
            combined = note_merge.resolve_note_append(base, ours, theirs, merged)
            if combined is None:
                self._note_refusal(
                    task_id, candidate, conflicts,
                    f"{path} is not two branches appending change notes — "
                    "something outside the append-only section conflicted, or a "
                    "line that was already there was edited, deleted or reordered",
                )
                return False
            resolutions[path] = combined

        try:
            for path, text in resolutions.items():
                (Path(self._git.repo_root) / path).write_text(text, encoding="utf-8")
            self._git.add_paths(sorted(resolutions))
            self._git.commit_staged(
                f"{message}\n\nAppend-only change notes combined automatically in "
                + ", ".join(sorted(resolutions))
                + "."
            )
        except (GitError, OSError) as exc:
            # The tree now holds this method's writes on top of git's
            # half-merged state. `_merge` calls `_abort` next, which restores
            # both — and verifies that it did.
            self._note_refusal(
                task_id, candidate, conflicts,
                f"combined the change notes but could not commit them: "
                f"{type(exc).__name__}: {exc}",
            )
            return False

        self._log(
            "auto_merge_notes_resolved",
            data={
                "task_id": task_id,
                "candidate_sha": candidate,
                "paths": sorted(resolutions),
            },
        )
        return True

    def _note_refusal(self, task_id, candidate, conflicts, reason: str) -> None:
        """Why a conflict was NOT auto-resolved, in the transcript.

        Logged on every refusal, not just the interesting ones: the sweep is
        about to stop, and "the resolver looked at this and declined, for this
        reason" is the difference between an operator reading one entry and
        reconstructing the merge by hand.
        """
        self._log(
            "auto_merge_notes_refused",
            data={
                "task_id": task_id,
                "candidate_sha": candidate,
                "conflicted_files": sorted(conflicts),
                "reason": reason,
            },
        )

    def _note_loop_code_merge(
        self, task_id: str, candidate: str, pre_head: str, new_head: str
    ) -> None:
        """Record that this merge changed the loop's own code, if it did.

        **Nothing here may change the merge's outcome.** By the time this runs
        the merge is verified and the push is next; a raise would reach
        `_guarded_attempt` and report a real integration as `failed`. So every
        failure — a git that will not diff, a state dir that will not take the
        write — swallows to a transcript entry, exactly as the rest of this
        module does, and the loop simply keeps running the old code (which is
        what it was doing anyway).

        A docs-only merge is silent: no record, and therefore nothing at the
        restart boundary to act on.
        """
        try:
            changed = self._git.changed_paths(pre_head, new_head)
        except Exception as exc:      # noqa: BLE001 - deliberate; see the docstring
            self._log(
                "self_upgrade_error",
                data={
                    "task_id": task_id,
                    "base_sha": new_head,
                    "error": f"{type(exc).__name__}: {exc}",
                    "note": "could not read what the merge changed — no restart offered",
                },
            )
            return
        loop_paths = loop_code_paths(changed)
        if not loop_paths:
            return
        record = PendingUpgrade(
            base_sha=new_head,
            previous_base_sha=pre_head,
            candidate_sha=candidate,
            task_id=task_id,
            repo_root=str(self._git.repo_root),
            paths=loop_paths[:50],
            status=UPGRADE_PENDING,
            recorded_at=utcnow_iso(),
        )
        try:
            self._upgrades.save(record)
        except OSError as exc:
            self._log(
                "self_upgrade_error",
                data={
                    "task_id": task_id,
                    "base_sha": new_head,
                    "error": f"{type(exc).__name__}: {exc}",
                    "note": "could not record the upgrade — no restart offered",
                },
            )
            return
        self._log(
            "self_upgrade_pending",
            data={
                "task_id": task_id,
                "candidate_sha": candidate,
                "base_sha_before": pre_head,
                "base_sha": new_head,
                "paths": loop_paths[:50],
                "path_count": len(loop_paths),
            },
        )

    def _verify_merge(self, new_head: str, pre_head: str, candidate: str) -> str:
        """An empty string when the merge demonstrably happened, else why not.

        A zero exit from `git merge` is not evidence: `--no-ff` on an
        already-contained commit, a hook, or a git version doing something
        unexpected all exit 0 without producing the integration this claims.
        """
        if new_head == pre_head:
            return (
                f"HEAD did not move (still {pre_head[:12]}) — the merge command "
                "succeeded but integrated nothing"
            )
        if not self._contains(new_head, candidate):
            return f"HEAD {new_head[:12]} does not contain candidate {candidate[:12]}"
        if not self._contains(new_head, pre_head):
            return (
                f"HEAD {new_head[:12]} does not contain the previous base "
                f"{pre_head[:12]} — history was rewritten, not merged"
            )
        if self._git.is_dirty():
            return f"the checkout is dirty after the merge: {self._git.dirty_files()}"
        return ""

    def _abort(self, execution, candidate, pre_head, conflicts, error) -> str:
        """Restore the base to exactly what it was, and say which files
        conflicted. Never force, never resolve."""
        task_id = execution.task_id
        restored = True
        detail = ""
        try:
            self._git.merge_abort()
        except GitError as exc:
            restored = False
            detail = f"merge --abort failed: {exc}"
        if restored:
            # `--abort` returning 0 is not evidence either.
            after = self._git.head_sha()
            if after != pre_head:
                restored = False
                detail = f"abort left HEAD at {after[:12]}, expected {pre_head[:12]}"
            elif self._git.is_dirty():
                restored = False
                detail = f"abort left the tree dirty: {self._git.dirty_files()}"
        self._log(
            "auto_merge_conflict",
            data={
                "task_id": task_id,
                "candidate_sha": candidate,
                "base_sha": pre_head,
                "conflicted_files": conflicts,
                "restored": restored,
                "reason": error,
                "detail": detail,
            },
        )
        if not restored:
            # The one case worth its own loud entry: the checkout is not in a
            # state this module can reason about anymore. Still not a park —
            # the operator's own `git status` is the right next step.
            self._log(
                "auto_merge_abort_incomplete",
                data={"task_id": task_id, "base_sha": pre_head, "detail": detail},
            )
        return CONFLICT

    def _push(self, execution, candidate, base_branch, base_ref, remote, head) -> str:
        """Publish the merged base. Computed the same way `_dispatch_task_push`
        computes it, so `allow_protected_push` means one thing in both."""
        task_id = execution.task_id
        gateway_protected = (
            () if self._policy.config.allow_protected_push
            else self._policy.config.protected_branches
        )
        try:
            landed = self._git.push_exact(remote, head, base_ref, gateway_protected)
        except (GitError, OSError) as exc:
            # The merge stands locally. A retry will find `already_merged` true
            # and come straight back here to push, which is exactly right — so
            # the deferral is kept rather than cleared.
            self._defer(
                execution,
                f"merged locally at {head[:12]} but the push to {remote}/{base_ref} "
                f"failed: {exc}",
                entry_type="auto_merge_push_refused",
            )
            return DEFERRED
        if landed != head:
            self._log(
                "auto_merge_failed",
                data={
                    "task_id": task_id,
                    "candidate_sha": candidate,
                    "base_branch": base_branch,
                    "reason": f"{remote}/{base_ref} is at {landed[:12]}, expected {head[:12]}",
                },
            )
            return FAILED
        self._deferrals.clear(task_id)
        self._log(
            "auto_merge_pushed",
            data={
                "task_id": task_id,
                "candidate_sha": candidate,
                "base_branch": base_branch,
                "base_sha": head,
                "remote": remote,
                "dest_ref": base_ref,
            },
        )
        return MERGED

    # ---- helpers ------------------------------------------------------------

    def _contains(self, head: str, other: str) -> bool:
        """`other` is `head` or an ancestor of it. An object git cannot
        resolve answers False rather than raising: for every caller here the
        question is "is this already accounted for", and an unknown object is
        emphatically not."""
        try:
            return self._git.is_descendant(head, other)
        except GitError:
            return False

    def _ensure_object(self, sha: str, source_path: str) -> bool:
        try:
            self._git.read_commit(sha)
            return True
        except GitError:
            pass
        if not source_path:
            return False
        resolved = Path(source_path).expanduser()
        if not resolved.is_absolute() or not resolved.exists():
            return False
        try:
            self._git.fetch_object(str(resolved), sha)
            self._git.read_commit(sha)
        except (GitError, OSError):
            return False
        return True

    def _skip(self, task_id: str, reason: str) -> str:
        """Not applicable. The deferral (if any) goes, because retrying it
        would report the same non-applicability forever."""
        self._deferrals.clear(task_id)
        self._log("auto_merge_skipped", data={"task_id": task_id, "reason": reason})
        return SKIPPED

    def _defer(self, execution, reason: str, entry_type: str = "auto_merge_deferred") -> str:
        """Record it and move on. A deferred merge is a normal state."""
        base_sha = ""
        try:
            base_sha = self._git.head_sha()
        except GitError:
            pass
        self._deferrals.record(
            task_id=execution.task_id,
            candidate_sha=execution.candidate_sha,
            dest_ref=execution.intended_remote_ref,
            base_sha=base_sha,
            reason=reason,
            now=utcnow_iso(),
        )
        self._log(
            entry_type,
            data={
                "task_id": execution.task_id,
                "candidate_sha": execution.candidate_sha,
                "base_sha": base_sha,
                "reason": reason,
            },
        )
        return DEFERRED
