"""Per-task worktree lifecycle for the produce-then-review commit path.

Each task executes in its own linked git worktree, checked out onto a fresh
branch off the task's base sha. Isolation is structural: two tasks never
share a working tree or an index, and — because git itself refuses to check
the same branch out into two worktrees at once — two attempts to work the
SAME task can never silently race each other either. That refusal
(`fatal: '<branch>' is already checked out at '<path>'`) is relied on as a
fail-closed property, not reimplemented here.

`task_id` becomes two things at once: a filesystem path component
(`root_dir/<task_id>`) and part of a git ref name
(`<branch_prefix><task_id>`). Both readings are hostile to `..` (path
traversal / a git ref component git itself rejects), a leading `-` (parsed as
a flag by many git subcommands and by naive argv-building code), an absolute
path, and anything outside `[A-Za-z0-9._-]` — so `task_id` is validated
against all of that before it touches either a path or `git worktree add`.
Exotic residuals git's own ref-name rules still reject (a trailing `.`, a
`.lock` suffix, `@{`, ...) are deliberately NOT reimplemented here — `git
worktree add` surfaces its own `GitCommandError` for those, and reproducing
`check-ref-format` would be one more place for the rules to drift apart.

`WorktreeManager` is constructed with the MAIN checkout's `GitGateway` — every
`git worktree ...` call in this module runs from there. The task's own
commit/verify sequence afterwards runs through a SEPARATE `GitGateway` rooted
at `TaskExecution.worktree_path`, which callers construct themselves (see
`orchestrator.py`).
"""

from __future__ import annotations

import re
from pathlib import Path

from .errors import GitCommandError
from .worktask import TaskExecution

#: First character must be alnum (rules out a leading `-`, which argv parsers
#: and many git subcommands read as a flag, and rules out a leading `.`, which
#: rules out a bare "." on its own). Remaining characters may be alnum, `.`,
#: `_` or `-`. No `/`, so this can never smuggle in a path separator.
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_task_id(task_id: str) -> None:
    """Raise `ValueError` unless `task_id` is safe to use as both a path
    component (`root_dir/<task_id>`) and a git ref component
    (`<branch_prefix><task_id>`)."""
    if not task_id:
        raise ValueError("task_id must not be empty")
    if not _TASK_ID_RE.match(task_id):
        raise ValueError(
            f"task_id {task_id!r} must match ^[A-Za-z0-9][A-Za-z0-9._-]*$ "
            "(no '/', no leading '-' or '.', no whitespace)"
        )
    if ".." in task_id:
        # The character class alone admits '.', so "a..b" or "a.." pass it —
        # '..' must be refused explicitly (path traversal; also invalid in a
        # git ref component).
        raise ValueError(f"task_id {task_id!r} must not contain '..'")


class WorktreeManager:
    """Creates and removes per-task linked worktrees.

    `git` is a `GitGateway` rooted at the MAIN checkout. `root_dir` is where
    every task's worktree is created, as `root_dir/<task_id>`. Branches are
    named `<branch_prefix><task_id>`.

    `root_dir` MUST resolve outside the main checkout's working tree (or, if
    unavoidably nested, be gitignored there). A worktree created INSIDE the
    checkout git itself is managing shows up as untracked/dirty paths in that
    checkout's own `git status` — which would poison `ChangeManifest.begin`'s
    baseline for the audit path (`manifest.py`), since that path snapshots
    the main checkout's dirty tree. `config.state_dir` (`.autoloop/` by
    default, already gitignored) is the natural home once this is wired into
    `cli.py` — not done in this pass.
    """

    def __init__(self, git, root_dir: Path, branch_prefix: str = "autoloop/"):
        self._git = git
        self.root_dir = Path(root_dir)
        self.branch_prefix = branch_prefix

    def branch_for(self, task_id: str) -> str:
        validate_task_id(task_id)
        return f"{self.branch_prefix}{task_id}"

    def path_for(self, task_id: str) -> Path:
        validate_task_id(task_id)
        return self.root_dir / task_id

    def create(self, task_id: str, base_sha: str) -> TaskExecution:
        """`git worktree add -b <branch> <root_dir>/<task_id> <base_sha>`.

        Refuses BEFORE calling git if `task_id` is unsafe (see
        `validate_task_id`), if a worktree already exists at the target path,
        or if the target branch already exists — all three give a specific,
        actionable error rather than whatever generic message git itself
        would produce for the same situation.
        """
        branch = self.branch_for(task_id)
        path = self.path_for(task_id)
        if path.exists():
            raise GitCommandError(
                f"worktree create refused: a worktree already exists at "
                f"{path} for task {task_id!r} — remove it first, or this is "
                "a resumed task and the caller should load its TaskExecution "
                "record instead of calling create() again"
            )
        if self._git.branch_exists(branch):
            raise GitCommandError(
                f"worktree create refused: branch {branch!r} already exists "
                f"— task {task_id!r} may already have a worktree elsewhere, "
                "or the branch name collides with unrelated history"
            )
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._git.worktree_add(str(path), branch, base_sha)
        return TaskExecution(
            task_id=task_id,
            task_branch=branch,
            worktree_path=str(path),
            task_base_sha=base_sha,
        )

    def remove(self, task_id: str, force: bool = False) -> None:
        """`git worktree remove [--force] <path>` then `git worktree prune`.

        Never deletes the branch — the branch and its commits survive
        removal of the worktree that checked it out.
        """
        path = self.path_for(task_id)
        self._git.worktree_remove(str(path), force=force)
        self._git.worktree_prune()

    #: `git worktree list --porcelain` uses these two capitalized keys; every
    #: other line (`branch`, `detached`, `bare`, `locked`, `prunable`) is
    #: already lowercase and passed through as-is.
    _KEY_ALIASES = {"worktree": "path", "HEAD": "head"}

    def list_worktrees(self) -> list[dict]:
        """Parse `git worktree list --porcelain` into a list of dicts, one
        per worktree, each with at least `path` and `head`, plus whichever of
        `branch` / `detached` / `bare` / `locked` / `prunable` git reported.
        A value-less flag line (`detached`, `bare`, a reason-less `locked`)
        is recorded as `True` rather than an empty string."""
        raw = self._git.worktree_list_porcelain()
        entries: list[dict] = []
        current: dict = {}
        for line in raw.splitlines():
            if not line.strip():
                if current:
                    entries.append(current)
                    current = {}
                continue
            key, _, value = line.partition(" ")
            key = self._KEY_ALIASES.get(key, key)
            current[key] = value if value else True
        if current:
            entries.append(current)
        return entries
