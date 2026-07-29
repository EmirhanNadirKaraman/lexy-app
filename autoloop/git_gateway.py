"""Safe git access for the orchestrator.

Every invocation goes through the policy whitelist (`PolicyEngine.
validate_git_command`) before it runs — even read-only ones — so no code path
in the loop can reach a git feature the policy does not know about. Commands
run via subprocess with an argument list (never `shell=True`) and an explicit
cwd; command arguments are program-constructed except the commit message and
optional staged paths, which come from a validated Directive and are passed as
argv elements (no shell interpolation).

Staging is EXACT (Phase 3): `commit` requires a non-empty path list, stages
only those paths (`git add -- <paths>`), verifies the resulting index matches
the approved set (unstaging any surprise via `git restore --staged`), captures
the staged diff summary, and only then commits. `git add -A` no longer exists
anywhere — the policy whitelist rejects `-A` and requires `--`, so it cannot
be reintroduced without changing the whitelist itself.

`commit` is idempotent for crash recovery: if no approved path differs from
HEAD and HEAD's message equals the requested message, the commit already
happened — return it. `push` always pushes the current branch by explicit
refspec, never a bare `git push`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import GitCommandError, GitOperationDenied
from .policy import PolicyEngine


class GitGateway:
    def __init__(self, repo_root: Path, policy: PolicyEngine, runner=None):
        self._repo_root = Path(repo_root)
        self._policy = policy
        self._runner = runner or subprocess.run

    @property
    def repo_root(self) -> Path:
        return self._repo_root

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        verdict = self._policy.validate_git_command(args)
        if not verdict.allowed:
            raise GitOperationDenied(f"{verdict.code}: {verdict.reason}")
        proc = self._runner(
            ["git", *args],
            cwd=str(self._repo_root),
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            raise GitCommandError(
                f"git {' '.join(args)} failed (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()}"
            )
        return proc

    def _out(self, *args: str) -> str:
        return self._git(*args).stdout.strip()

    # ---- read-only ----------------------------------------------------------

    def current_branch(self) -> str:
        return self._out("branch", "--show-current")

    def head_sha(self) -> str:
        return self._out("rev-parse", "HEAD")

    def head_message(self) -> str:
        return self._out("log", "-1", "--format=%B")

    def status_porcelain(self) -> str:
        return self._git("status", "--porcelain").stdout

    def dirty_files(self) -> list[str]:
        return [line for line in self.status_porcelain().splitlines() if line.strip()]

    def is_dirty(self) -> bool:
        return bool(self.dirty_files())

    def dirty_paths(self) -> set[str]:
        paths = set()
        for line in self.dirty_files():
            if len(line) > 3:
                paths.add(line[3:].split(" -> ")[-1].strip())
        return paths

    def staged_paths(self) -> set[str]:
        out = self._out("diff", "--cached", "--name-only")
        return {line.strip() for line in out.splitlines() if line.strip()}

    # ---- write (only reachable from explicitly approved directives) ---------

    def commit(self, message: str, paths: tuple[str, ...]) -> tuple[str, bool, str]:
        """Stage EXACTLY the approved paths and commit. Returns
        (sha, already_committed, staged_diff_summary).

        There is deliberately no all-paths mode: an empty/None path list is a
        hard error, and staging is verified path-for-path before the commit
        runs. Idempotent for crash recovery: if none of the approved paths
        differ from HEAD and HEAD carries the requested message, the commit
        already happened."""
        if not paths:
            raise GitCommandError("commit requires an explicit non-empty path list")
        approved = {p.strip() for p in paths if p.strip()}
        touchable = self.dirty_paths() | self.staged_paths()
        if not (approved & touchable):
            if self.head_message() == message.strip():
                return self.head_sha(), True, ""
            raise GitCommandError(
                "nothing to commit: none of the approved paths differ from HEAD "
                "and HEAD's message does not match the requested one"
            )
        self._git("add", "--", *sorted(approved))
        staged = self.staged_paths()
        extra = staged - approved
        if extra:
            # Something was already sitting in the index — undo and refuse.
            self._git("restore", "--staged", "--", *sorted(extra))
            raise GitCommandError(
                f"refusing to commit: index contained unapproved paths {sorted(extra)} "
                "(now unstaged)"
            )
        if not staged:
            raise GitCommandError(
                "refusing to commit: staging the approved paths produced an empty index"
            )
        summary = self._out("diff", "--cached", "--stat")
        self._git("commit", "-m", message)
        return self.head_sha(), False, summary

    def push(self, remote: str = "origin") -> str:
        branch = self.current_branch()
        if not branch:
            raise GitCommandError("cannot push: detached HEAD")
        proc = self._git("push", remote, branch)
        return (proc.stdout + proc.stderr).strip()
