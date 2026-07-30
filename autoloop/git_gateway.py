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

import os
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

    def _git_bytes(self, *args: str) -> bytes:
        """Policy-validated git call returning raw stdout.

        Separate from `_git` because blob content must not be decoded: hashing
        needs the exact bytes git holds, and text mode would mangle anything
        non-UTF-8.
        """
        verdict = self._policy.validate_git_command(args)
        if not verdict.allowed:
            raise GitOperationDenied(f"{verdict.code}: {verdict.reason}")
        proc = self._runner(["git", *args], cwd=str(self._repo_root), capture_output=True)
        if proc.returncode != 0:
            detail = proc.stderr or proc.stdout or b""
            if isinstance(detail, bytes):
                detail = detail.decode("utf-8", "replace")
            raise GitCommandError(
                f"git {' '.join(args)} failed (rc={proc.returncode}): {detail.strip()}"
            )
        return proc.stdout or b""

    def staged_blob(self, path: str) -> bytes:
        """The exact bytes staged for `path` (the index entry, not the file).

        This is what a commit will contain. Reading the working tree instead
        allows a swap-and-restore: stage altered bytes, put the approved bytes
        back on disk, and a worktree-based check sees nothing wrong.
        """
        return self._git_bytes("cat-file", "blob", f":{path}")

    def staged_mode(self, path: str) -> str:
        """Index mode for `path` ("100644", "120000" for a symlink, …), or ""."""
        raw = self._git_bytes("ls-files", "-s", "-z", "--", path).decode("utf-8", "surrogateescape")
        record = raw.split("\0", 1)[0]
        return record.split(" ", 1)[0] if record else ""

    def dirty_entries(self) -> list[tuple[str, str]]:
        """(XY status, path) for every pending change, NUL-parsed.

        `git status --porcelain` without `-z` QUOTES and escapes paths holding
        spaces, tabs or non-ASCII (`?? "has\\ttab.txt"`), so line splitting
        yields a string that matches no real path. Every security decision that
        depends on a pathname uses this NUL-delimited form instead.
        """
        raw = self._git_bytes("status", "--porcelain", "-z").decode("utf-8", "surrogateescape")
        records = [r for r in raw.split("\0") if r]
        entries: list[tuple[str, str]] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if len(record) < 4:
                continue
            status, path = record[:2], record[3:]
            # Rename/copy entries carry the ORIGINAL path as a second record.
            if status and status[0] in ("R", "C"):
                index += 1
            entries.append((status, path))
        return entries

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
        return {path for _status, path in self.dirty_entries()}

    def staged_paths(self) -> set[str]:
        raw = self._git_bytes("diff", "--cached", "--name-only", "-z").decode(
            "utf-8", "surrogateescape"
        )
        return {p for p in raw.split("\0") if p}

    # ---- write (only reachable from explicitly approved directives) ---------

    def commit(
        self, message: str, paths: tuple[str, ...], post_stage_check=None
    ) -> tuple[str, bool, str]:
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
        if post_stage_check is not None:
            # The index was built by `git add` above; a caller that binds
            # content (adopted manifests) re-verifies here so nothing unreviewed
            # can slip in between its first check and the staging call. On
            # refusal the index is restored before the error propagates.
            try:
                post_stage_check()
            except Exception:
                self._git("restore", "--staged", "--", *sorted(staged))
                raise
        summary = self._out("diff", "--cached", "--stat")
        self._git("commit", "-m", message)
        return self.head_sha(), False, summary

    def push(self, remote: str = "origin") -> str:
        branch = self.current_branch()
        if not branch:
            raise GitCommandError("cannot push: detached HEAD")
        proc = self._git("push", remote, branch)
        return (proc.stdout + proc.stderr).strip()

    # ---- immutable-tree commit path (adopted manifests) --------------------
    #
    # `git commit` is deliberately NOT used here. It runs hooks, and a
    # pre-commit hook can rewrite the index — and therefore the committed tree —
    # AFTER any verification the caller performed. Reproduced: an index check
    # passed, the hook replaced the approved file's bytes and staged an extra
    # file, and both landed in the commit. Instead: build a tree, verify that
    # immutable object, create a commit from it, verify the commit object, then
    # move the branch by compare-and-swap.

    #: The hooks an ordinary (non-amend) `git commit` can invoke.
    COMMIT_HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit")

    def hooks_dir(self) -> Path:
        """Effective hooks directory as GIT resolves it (honours core.hooksPath)."""
        raw = self._out("rev-parse", "--git-path", "hooks")
        path = Path(raw)
        return path if path.is_absolute() else self._repo_root / path

    def active_commit_hooks(self) -> tuple[Path, list[str]]:
        """(effective hooks dir, names of active commit hooks).

        A hook counts as active when git would treat the exact hook path as
        executable. `os.access(X_OK)` follows symlinks, so an executable symlink
        counts. `*.sample` files are never checked because only the exact hook
        names are, so a stock hooks directory is not a refusal condition.
        """
        directory = self.hooks_dir()
        active = [
            name
            for name in self.COMMIT_HOOKS
            if os.access(directory / name, os.X_OK) and (directory / name).is_file()
        ]
        return directory, active

    def symbolic_head_ref(self) -> str:
        """The branch ref HEAD points at, e.g. `refs/heads/main`.

        Raises on a detached HEAD: the adopted path commits onto a named branch
        by compare-and-swap and has no defined behaviour otherwise.
        """
        try:
            ref = self._out("symbolic-ref", "HEAD")
        except GitCommandError as exc:
            raise GitCommandError(
                "adopted commit requires a symbolic branch HEAD (detached HEAD, "
                f"rebase or bisect in progress?): {exc}"
            ) from exc
        if not ref.startswith("refs/heads/"):
            raise GitCommandError(f"HEAD does not point at a branch: {ref!r}")
        return ref

    def write_tree(self) -> str:
        """Write the current index to a tree object and return its id."""
        return self._out("write-tree")

    def tree_of(self, rev: str) -> str:
        return self._out("rev-parse", f"{rev}^{{tree}}")

    def tree_entries(self, tree: str) -> dict[str, tuple[str, str, str]]:
        """path -> (mode, object type, object id), recursively, NUL-parsed.

        `-z` is required: without it git quotes paths containing spaces, tabs or
        non-ASCII, and a security check keyed on the pathname would compare the
        wrong string. The object TYPE is returned too, so a caller can insist on
        a blob rather than assuming it.
        """
        raw = self._git_bytes("ls-tree", "-r", "-z", tree).decode("utf-8", "surrogateescape")
        entries: dict[str, tuple[str, str, str]] = {}
        for record in raw.split("\0"):
            if not record:
                continue
            meta, _, path = record.partition("\t")
            mode, kind, oid = meta.split(" ", 2)
            entries[path] = (mode, kind, oid)
        return entries

    def blob_bytes(self, oid: str) -> bytes:
        return self._git_bytes("cat-file", "blob", oid)

    def changed_paths(self, tree_a: str, tree_b: str) -> set[str]:
        raw = self._git_bytes(
            "diff-tree", "-r", "--name-only", "-z", tree_a, tree_b
        ).decode("utf-8", "surrogateescape")
        return {p for p in raw.split("\0") if p}

    def commit_tree(self, tree: str, parent: str, message: str) -> str:
        return self._out("commit-tree", tree, "-p", parent, "-m", message)

    @staticmethod
    def ident_identity(ident: str) -> str:
        """The `Name <email>` half of a git ident, dropping timestamp and zone.

        A git ident is `Name <email> <unix-ts> <tz>`. Author and committer are
        required to agree on THIS part; their timestamps may differ. `commit-tree`
        derives both from the same configuration, so a divergence means the
        environment overrode one of them.
        """
        head, sep, _ = ident.rpartition(">")
        return (head + sep) if sep else ident

    def read_commit(self, oid: str) -> dict:
        """Parse a commit object: tree, parents, author, committer, message."""
        raw = self._git_bytes("cat-file", "commit", oid).decode("utf-8", "replace")
        header, _, message = raw.partition("\n\n")
        info: dict = {"parents": [], "message": message}
        for line in header.splitlines():
            key, _, value = line.partition(" ")
            if key == "parent":
                info["parents"].append(value)
            elif key in ("tree", "author", "committer"):
                info[key] = value
        return info

    def update_ref_cas(self, ref: str, new_value: str, expected_old: str) -> None:
        """Move `ref` only if it still equals `expected_old` (git's own CAS)."""
        self._git("update-ref", ref, new_value, expected_old)

    def commit_adopted(
        self, message: str, paths: tuple[str, ...], verify_tree
    ) -> tuple[str, str, list[str]]:
        """Commit approved paths via an immutable verified tree.

        Sequence. No REF moves and no history is published before every check
        has passed. Note step 6 does write a commit object: a later failure (a
        lost CAS) therefore leaves an unreachable object in the database, which
        `git gc` prunes. That is reported, not hidden — it is not a ref move and
        not published history, but "nothing irreversible" would be inaccurate.

          1. refuse if any commit hook is active (never bypassed, never emulated)
          2. require a symbolic branch HEAD; record the ref and original HEAD
          3. stage exactly the approved paths; index must equal that set
          4. `write-tree` -> candidate tree
          5. `verify_tree(tree, parent_tree)` -> violations refuse here
          6. `commit-tree` with the original HEAD as the single parent
          7. read the commit object back and verify tree/parent/message/identity
          8. `update-ref` compare-and-swap against the original HEAD
          9. confirm HEAD now resolves to that commit and that exact tree

        Returns (commit sha, staged diff summary, residual dirty paths).
        Amend, merge commits and detached HEAD are unsupported by design.
        """
        if not paths:
            raise GitCommandError("adopted commit requires an explicit non-empty path list")
        approved = {p.strip() for p in paths if p.strip()}

        hooks_directory, active = self.active_commit_hooks()
        if active:
            raise GitCommandError(
                f"adopted commit refused: active commit hook(s) {active} in "
                f"{hooks_directory}. A hook can rewrite the index after verification, "
                "so content-bound authorization is not achievable through them. They "
                "were NOT executed and NOT bypassed — review or disable them, then "
                "retry."
            )

        ref = self.symbolic_head_ref()
        original_head = self.head_sha()
        parent_tree = self.tree_of("HEAD")

        self._git("add", "--", *sorted(approved))
        staged = self.staged_paths()
        if staged != approved:
            raise GitCommandError(
                "adopted commit refused: index holds "
                f"{sorted(staged)} but the approval covers {sorted(approved)}"
            )
        summary = self._out("diff", "--cached", "--stat")

        tree = self.write_tree()
        violations = verify_tree(tree, parent_tree)
        if violations:
            # Nothing has been created and no ref moved. The index and working
            # tree are left exactly as they are — reporting beats discarding.
            raise GitCommandError("adopted commit refused: " + "; ".join(violations))

        # Re-check hooks immediately before the object is written. A hook
        # installed (or the hooks directory repointed) after step 1 would
        # otherwise apply to a commit this call is about to create. This closes
        # the window inside this process; arbitrary concurrent mutation of git
        # configuration, hooks or the object database by an external hostile
        # process is outside the guarantee (see the module docstring).
        recheck_directory, recheck_active = self.active_commit_hooks()
        if (recheck_directory, recheck_active) != (hooks_directory, active):
            raise GitCommandError(
                "adopted commit refused: hook state changed during verification "
                f"(was {active or 'none'} in {hooks_directory}, now "
                f"{recheck_active or 'none'} in {recheck_directory})"
            )

        new_commit = self.commit_tree(tree, original_head, message)

        info = self.read_commit(new_commit)
        expected_message = message if message.endswith("\n") else message + "\n"
        if info.get("tree") != tree:
            raise GitCommandError(
                f"created commit {new_commit[:12]} carries tree "
                f"{info.get('tree', '?')[:12]}, expected the verified {tree[:12]}"
            )
        if info.get("parents") != [original_head]:
            raise GitCommandError(
                f"created commit has parents {info.get('parents')}, expected exactly "
                f"[{original_head}] (no merge, no amend)"
            )
        if info.get("message") != expected_message:
            raise GitCommandError("created commit message does not match the approved bytes")
        author, committer = info.get("author", ""), info.get("committer", "")
        if not author or not committer:
            raise GitCommandError("created commit lacks an author or committer identity")
        if self.ident_identity(author) != self.ident_identity(committer):
            raise GitCommandError(
                "created commit has a split author/committer identity: "
                f"{self.ident_identity(author)} vs {self.ident_identity(committer)}"
            )

        # Only now does anything become visible. CAS fails if the branch moved,
        # leaving the branch untouched and this commit object unreachable.
        try:
            self.update_ref_cas(ref, new_commit, original_head)
        except GitCommandError as exc:
            raise GitCommandError(
                f"adopted commit refused: {ref} moved during verification, so the "
                f"compare-and-swap against {original_head[:12]} failed. The branch was "
                f"NOT overwritten; commit object {new_commit[:12]} was written and is "
                f"now unreachable (a dangling object `git gc` will prune). Original "
                f"error: {exc}"
            ) from exc

        if self.head_sha() != new_commit:
            raise GitCommandError(
                f"after update-ref HEAD is {self.head_sha()[:12]}, expected "
                f"{new_commit[:12]}"
            )
        if self.tree_of("HEAD") != tree:
            raise GitCommandError("HEAD's tree is not the verified tree")

        return new_commit, summary, sorted(self.dirty_paths())
