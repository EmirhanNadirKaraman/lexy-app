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
authorize-then-produce path's `reviewed` stamp (see `contract.py`).

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
    #: Every commit/packet attempt for this task, INCLUDING ones that never
    #: produced a review. `review_round` deliberately counts only dispatched
    #: reviews, so on its own it would let structural refusals churn locally
    #: without bound. This is the independent ceiling on that.
    attempt_count: int = 0
    presented_report_sha256: str = ""
    review_request_id: str = ""
    intended_remote: str = ""
    intended_remote_ref: str = ""
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
        data["validation_commands"] = [list(c) for c in execution.validation_commands]
        _atomic_write_json(self._path(execution.task_id), data)

    def load(self, task_id: str) -> TaskExecution | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["allowed_paths"] = tuple(data.get("allowed_paths", ()))
            # JSON has no tuples: a record written before this field existed
            # has no key at all, and one written after has lists-of-lists.
            # Both normalise to the same shape, so an older record loads as
            # "declared none" rather than raising.
            data["validation_commands"] = tuple(
                tuple(c) for c in data.get("validation_commands", ())
            )
            return TaskExecution(**data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StateCorruptError(
                f"task execution record {path} is unreadable: {exc}"
            ) from exc

    def clear(self, task_id: str) -> None:
        self._path(task_id).unlink(missing_ok=True)


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
