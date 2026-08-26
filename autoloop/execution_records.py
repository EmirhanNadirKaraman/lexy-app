"""Execution-record lifecycle: returning a task to pending, and retiring the
execution it leaves behind.

ONE SUBJECT, lifted out of `orchestrator.py` unchanged (shrink-01, 2026-08-26).
`release_task_to_pending` is THE release path — `cli._cmd_release`, and in
`orchestrator.py` the urgent preemption, the reviewer's recut, the ceiling
split and halt-03's autonomous rebuild, five callers in all — and the six
helpers beside it exist for nothing else. Before this module, every one of them
was called from exactly one place:
`_archived_record_path`, `_loaded_execution`, `_worker_can_resume`,
`_repair_orphaned_record` and `_surviving_worker_path` from that function's own
body, and `_surviving_execution_record` from it and from
`_repair_orphaned_record`. `Release` is the type describing what one such call
actually moved, built nowhere else. That is the cohesion argument, and it is
checkable: a caller graph with a single root, plus the result type only that
root constructs.

What did NOT come with them, deliberately. `_preemption_stop_reason` stays in
`orchestrator.py`: it renders `LoopState.stop_reason` for a preempted session
out of the record the loop wrote from a `Release`, which is the preemption's
reporting, not this lifecycle. The dispatch-side reuse probe
(`_dispatch_task_postcommit`'s own `worker_repo_is_reusable` calls) stays there
too — this module asks the same question, of `worker_env`, for its own repair
gate.

Nothing changed in the move: same definitions, same order, same signatures,
same behaviour. `orchestrator.py` imports both public names straight back, so
`orchestrator.release_task_to_pending` and `orchestrator.Release` still resolve
exactly as before — `cli.py` and `autoloop/tests/test_task_split.py` import the
first from there and are untouched.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .errors import GitError, StateError
from .tasks import Task, TaskRegistry
from .worker_env import worker_repo_is_reusable
from .worktask import Retirement, retire_execution


@dataclass(frozen=True)
class Release:
    """What one `release_task_to_pending` call ACTUALLY moved.

    A plain `(task, Retirement)` tuple could only describe the happy path, and
    the reporting built on it then lied in the one case worth reporting: the
    status move is durable before the artefacts move (see the ORDER note
    below), so a failed retirement leaves a task that IS pending while the
    caller said it was not. This type carries the two halves separately, so a
    partial release is recorded as the partial thing it is.

    `artifacts_retired` False always comes with `obstacle` set, and with
    whichever residue survived named exactly: `stale_worker_path` is the
    directory still sitting where the next dispatch would create one, and
    `stale_execution_record` says the live record is still in the merge-window
    gate. It is only ever RETURNED to a caller that asked for it
    (`tolerate_retirement_failure=True`); every other caller gets the failure
    raised, which is what `cli._cmd_release` has always done.

    `residue_resumable` is the difference between the two endings a caller has
    to report differently, and it is a CHECKED fact rather than a hope — see
    `_repair_orphaned_record`.
    """

    task: Task
    retirement: Retirement | None = None
    #: The STATUS half. Always True on a returned `Release` — `registry.release`
    #: raises rather than returning when it will not move — and named anyway,
    #: so a reader of the record does not have to know that to interpret it.
    status_moved: bool = True
    artifacts_retired: bool = True
    obstacle: str = ""
    stale_worker_path: str = ""
    stale_execution_record: bool = False
    #: The residue is a record PLUS a worker that `worker_repo_is_reusable`
    #: accepts, so the next dispatch of this task resumes it instead of
    #: refusing — at the cost of a merge window that stays shut until that
    #: round publishes. False means the opposite ending: a worker the next
    #: dispatch will refuse to create over, which an operator has to move.
    residue_resumable: bool = False


def release_task_to_pending(
    task_id: str,
    registry: TaskRegistry,
    execution_store,
    worker_repos,
    *,
    persist,
    reason: str = "released-by-operator",
    tolerate_retirement_failure: bool = False,
    move=None,
) -> Release:
    """Return an in-progress task to pending, and retire the execution it
    leaves behind. Returns a `Release` describing what actually moved.

    THE release path, and the only one — `cli._cmd_release` and
    `Orchestrator._preempt_for_urgent` both come through here, because the
    thing that goes wrong with releasing a task is doing one third of it. Three
    things have to move together (the wording is `_cmd_release`'s own, kept
    verbatim because it is the rule):

    * the STATUS, or `next_ready` keeps skipping it;
    * the stale WORKER REPO, or the next dispatch refuses (`create()` will not
      write into an existing directory);
    * the EXECUTION RECORD, or `_merge_window_blockers` keeps reading a
      `candidate_sha` for work that no longer exists as in-flight.

    That third one was silently left behind until 2026-08-15 and cost an
    operator 14 hand-archived records; `worktask.retire_execution` does the
    last two in one call precisely so a caller cannot do one and forget the
    other. This function adds the first, so a SECOND caller cannot rebuild the
    same gap one level up — which is what a preemption re-implementing the
    sequence would have done.

    ORDER, and it is not arbitrary. `registry.release` raises before anything
    else happens, so a task that may not be released (not in progress) leaves
    the worker repo and the record exactly where they are. `persist` then makes
    the status durable BEFORE the artefacts move: a process that dies in
    between leaves a pending task with a stale worker, which the next dispatch
    refuses loudly, rather than a task still marked in-progress whose worker has
    already been quarantined — a state no command can move.

    **A FAILED RETIREMENT IS NEVER ROLLED BACK, AND WHO HEARS ABOUT IT IS THE
    CALLER'S CHOICE (`tolerate_retirement_failure`).** Rolling the status back
    is not an option either way: the status is already durable by the time the
    artefacts move, so undoing it would put the task back in `in_progress` —
    the exact invisible-to-`next_ready` state of 2026-08-21 — with its worker
    repo and record still on disk. What differs is the ENDING:

    * **False (the default, and `cli._cmd_release`)** — the failure is
      re-raised, unchanged and of its original type, so the operator's command
      fails loudly and `cli.main` reports it and exits 1. That is the behaviour
      this command has always had, and `test_recovery_commands.py::
      test_the_record_is_retired_before_the_worker` is its pin: the record is
      retired first, the worker survives, and the error propagates. A human ran
      that command and is reading its output; an exception is not a silent
      failure to them.
    * **True (`Orchestrator._preempt_for_urgent`)** — the failure comes back
      inside the `Release` instead. Nobody is watching a preemption, the round
      is ending regardless, and raising here would take the process down at the
      one moment an operator is waiting for their urgent task. The `Release`
      then says precisely how far it got, and the loop records it.

    Either way the `except` clause catches `GitError`, which the colliding-
    quarantine-label case (`WorkerRepoManager.quarantine`) actually raises: it
    is not an `OSError`, so a caller catching only the filesystem errors it
    believed covered that case never saw it.

    **ONE RETRY, under a distinct label, because a label collision is the
    likelier of the two named failures.** `retire_execution` derives its label
    from `reason` plus a whole-second timestamp, so two retirements of one task
    inside the same second collide by construction; retrying under
    `<reason>-retry` changes the label rather than the second and is what turns
    that case back into a clean retirement. The retry is safe to repeat because
    both halves are absence-tolerant — `archive` returns None when the record
    has already moved, and the worker is only quarantined `if … exists()`.

    **AND ON THE TOLERATING PATH THE RESIDUE IS LEFT IN THE BEST SHAPE
    AVAILABLE, WHICH IS NOT ALWAYS A GOOD ONE.** When even the retry fails,
    `_repair_orphaned_record` puts the execution record back beside the worker
    that could not be moved — but only when that worker is one the next
    dispatch would RESUME
    (`worker_repo_is_reusable`). Then the residue is exactly what a process
    killed between `persist()` and the retirement leaves, and the resume path
    already handles it. Otherwise the split is left as it is and reported: a
    live record shuts the repository-wide merge window, and paying that for a
    worker the next dispatch would refuse anyway buys nothing. So "prevent" is
    true for the label collision (the retry) and for a resumable worker, and
    the remaining case is REPORTED rather than prevented — nothing in this
    function can move a directory the filesystem refused to move.

    `persist` is a callable rather than a `TaskStore`, because the two callers
    save through different objects (the CLI's own store, the orchestrator's
    `self._task_store`) and because a function that took a store would have to
    decide what else to write. It is called exactly once, always.

    `move` is WHICH registry transition returns the row to pending, defaulting
    to `registry.release`. It exists for exactly one caller —
    `Orchestrator._dispatch_recut`, which passes `registry.recut` — and it is a
    parameter rather than a second copy of this function because everything
    BELOW the status move is identical for both verbs and is the part that has
    historically gone wrong: the ordering, the label-collision retry, the
    orphaned-record repair, the partial-`Release` reporting. A recut
    re-implementing that sequence one level up is precisely the gap this
    function was written to close, one caller later. It changes nothing for the
    default: `registry.release` is still what refuses a task that may not be
    released, still before anything else happens.

    Nothing is deleted. The worker is MOVED to quarantine (an interrupted round
    usually has real work in it) and the record is MOVED to
    `executions/archive/`, both under one label, so the candidate stays
    recoverable and the two halves name each other on disk — except on the
    retry, where the labels necessarily differ and `_archived_record_path`
    recovers the pairing for the record instead.
    """
    task = (move or registry.release)(task_id)
    persist()
    recorded = _loaded_execution(task_id, execution_store)
    problems = []
    first_failure = None
    for attempt_reason in (reason, f"{reason}-retry"):
        try:
            retirement = retire_execution(
                task_id, execution_store, worker_repos, reason=attempt_reason
            )
        except (GitError, StateError, OSError) as exc:
            problems.append(str(exc))
            if first_failure is None:
                first_failure = exc
            continue
        if retirement.record_path is None and recorded is not None:
            # The record was archived by the attempt that then failed on the
            # worker, so this attempt found nothing left to file and reported
            # `None` — which a reader would take as "there was no record". Look
            # up where the earlier attempt put it, so the two halves can still
            # be found from one another even though the retry changed the label.
            retirement = replace(
                retirement,
                record_path=_archived_record_path(task_id, execution_store, reason),
            )
        return Release(task=task, retirement=retirement)
    if not tolerate_retirement_failure:
        # The default ending, and the one `cli._cmd_release` has always had: the
        # original error, of its original type, to a human who is reading the
        # output of a command they just ran. Raised BEFORE `_repair_orphaned_
        # record`, deliberately — restoring a record shuts the repository-wide
        # merge window, and paying that silently behind a traceback would be a
        # cost nobody chose. The status move stands (see above), so what the
        # operator is looking at is a task that IS pending with its artefacts
        # still on disk.
        raise first_failure
    resumable = _worker_can_resume(recorded)
    _repair_orphaned_record(task_id, execution_store, recorded, problems, resumable)
    stale_record = _surviving_execution_record(task_id, execution_store)
    return Release(
        task=task,
        artifacts_retired=False,
        obstacle="; then ".join(problems),
        stale_worker_path=_surviving_worker_path(task_id, worker_repos),
        stale_execution_record=stale_record,
        residue_resumable=stale_record and resumable,
    )


def _archived_record_path(task_id: str, execution_store, reason: str):
    """Where the FIRST retirement attempt filed the execution record, when the
    retry could not report it because there was nothing left to file.

    Best-effort and read-only. Built from the naming
    `TaskExecutionStore.archive` documents (`archive/<task_id>-<label>.json`,
    label = `<reason>-<stamp>`), newest last because the stamp sorts
    lexicographically; `None` when nothing matches, which reports exactly as it
    did before — an unknown path is better said as unknown.
    """
    try:
        matches = sorted(
            (Path(execution_store.directory) / "archive").glob(
                f"{task_id}-{reason}-*.json"
            )
        )
    except (AttributeError, OSError):  # pragma: no cover - store without a directory
        return None
    return matches[-1] if matches else None


def _loaded_execution(task_id: str, execution_store):
    """The live `TaskExecution` before a retirement is attempted, or None.

    Read for one purpose — `_repair_orphaned_record` — and unreadable counts
    as absent: a record this cannot parse is one it must not try to put back.
    """
    if execution_store is None:
        return None
    try:
        return execution_store.load(task_id)
    except (StateError, OSError):  # pragma: no cover - unreadable record
        return None


def _worker_can_resume(recorded) -> bool:
    """Would the next dispatch RESUME the worker this record names, rather
    than try to create one over it?

    The same question `_dispatch_task_postcommit` asks
    (`worker_repo_is_reusable`: the directory is the top level of a git
    repository and is on the recorded branch), asked here so the repair below
    is a checked promise instead of a hopeful one. A directory that merely
    exists does NOT qualify, and that is the common shape of a half-written
    worker.
    """
    if recorded is None or not recorded.worktree_path:
        return False
    return worker_repo_is_reusable(Path(recorded.worktree_path), recorded.task_branch)


def _repair_orphaned_record(
    task_id, execution_store, recorded, problems, resumable: bool
) -> None:
    """Put the execution record BACK beside a worker the next dispatch can
    actually resume — and ONLY then.

    `retire_execution` files the record first and the worker second (its own
    ordering rule), so a worker half that fails leaves the two halves split:
    the record archived, the worker still at `workers/<task_id>`. When that
    worker is resumable, the split is pure loss — with no record the dispatch
    takes the first-dispatch branch and `WorkerRepoManager.create` refuses to
    write into the directory that is still there, discarding a round that could
    have been continued. Re-saving the record restores exactly what a killed
    process leaves, which the resume path already handles.

    **The `resumable` gate is what makes that trade honest, and it is not
    optional.** A live record holds the merge window shut
    (`cli._merge_window_blockers` exempts a record only for a terminal task, a
    PUBLISHED candidate, a base already behind the head, or a worker that is
    GONE — none of which this residue satisfies), and the window is the
    repository-wide merge sweep, not this one task. Paying that to save a
    resumable round is worth it; paying it for a worker the next dispatch would
    refuse anyway buys nothing and hides the refusal behind a stalled sweep.
    So a non-resumable residue is left SPLIT and reported as the directory an
    operator has to move.

    Re-SAVED from the object read before the attempt, rather than moved back
    out of `archive/`: the in-memory record is the authoritative copy, and
    reaching into the archive would need this module to spell a filename only
    `TaskExecutionStore` should own — with nothing better to do if that move
    failed too. The cost is one stale duplicate under `archive/`, which no
    reader looks at (`cli._merge_window_blockers` and `dashboard.merge_states`
    glob `executions/*.json`, which does not recurse).

    The restored record carries its `candidate_sha`, `review_round` and
    `attempt_count` forward, so the resumed dispatch continues under the
    counters the displaced round had already spent (`_park_round_cap` reads the
    second, `MAX_TASK_ATTEMPTS` the third). That is the intended reading — it is
    the same unit of work being continued, not a fresh one — and it is stated
    because a repair that quietly RESET them would look identical here and
    would hand the task a budget it had already used.

    Best-effort and never fatal. A repair that itself fails is appended to
    `problems`, so the caller's `obstacle` carries both halves of what went
    wrong rather than only the first.
    """
    if recorded is None or execution_store is None or not resumable:
        return
    if _surviving_execution_record(task_id, execution_store):
        return  # the record never moved — nothing was orphaned
    try:
        execution_store.save(recorded)
    except OSError as exc:  # pragma: no cover - unwritable executions dir
        problems.append(f"the execution record could not be restored either: {exc}")


def _surviving_worker_path(task_id: str, worker_repos) -> str:
    """The worker repo a failed retirement left behind, or `""`.

    Reported rather than merely counted because it is the one thing that
    BREAKS the next dispatch (`WorkerRepoManager.create` refuses to write into
    an existing directory), and because the remedy is a single `mv` an
    operator can only run if they are told the path. Best-effort: a manager
    that cannot even answer where the repo would be is not worth failing a
    release over.
    """
    if worker_repos is None:
        return ""
    try:
        path = worker_repos.path_for(task_id)
        return str(path) if path.exists() else ""
    except (ValueError, OSError):  # pragma: no cover - a manager that cannot answer
        return ""


def _surviving_execution_record(task_id: str, execution_store) -> bool:
    """Is the LIVE execution record still there after a failed retirement?

    The silent half of the pair: a surviving record announces nothing and
    holds the merge window shut (`cli._merge_window_blockers`), so a release
    that could not move it has to say so itself.
    """
    if execution_store is None:
        return False
    try:
        return execution_store.load(task_id) is not None
    except (StateError, OSError):  # pragma: no cover - unreadable record
        return True
