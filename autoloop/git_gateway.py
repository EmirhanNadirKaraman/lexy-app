"""Safe git access for the orchestrator.

Every invocation goes through the policy whitelist (`PolicyEngine.
validate_git_command`) before it runs — even read-only ones — so no code path
in the loop can reach a git feature the policy does not know about. Commands
run via subprocess with an argument list (never `shell=True`) and an explicit
cwd; command arguments are program-constructed except the commit message and
optional staged paths, which come from a validated Directive and are passed as
argv elements (no shell interpolation).

Staging is EXACT (Phase 3): every commit path stages only the approved paths
(`git add -- <paths>`), verifies the resulting index matches the approved set
(unstaging any surprise via `git restore --staged`), captures the staged diff
summary, and only then commits. `git add -A` no longer exists anywhere — the
policy whitelist rejects `-A` and requires `--`, so it cannot be reintroduced
without changing the whitelist itself.

There is no ambient `push()` — it pushed whatever the current branch tip
happened to be at call time, exactly the race + wrong-destination class M1
exists to close (removed 2026-07-30, pass 2b). Every push goes through
`push_exact`, by an explicit, already-resolved `<sha>:<dest_ref>` refspec.

**S21 retirement (2026-07-30).** The old authorize-then-produce/manifest
`commit()` method — plain `git commit`, gated only on `ChangeManifest`
provenance — is REMOVED. A `pre-commit` hook could rewrite an approved path's
bytes, or stage an extra unapproved file, strictly AFTER that provenance check
ran, and both would land in the commit (reproduced; see docs/SECURITY.md
S21). Its only caller (`orchestrator.py`'s `_dispatch_git`) was removed in the
same change — S21 is closed by retiring the vulnerable path, not by fixing
it. `commit_adopted` (below) already closed the same hole a different way —
build and verify an immutable tree, then `commit-tree` + `update-ref`
compare-and-swap, refusing outright if any commit hook is active — but it
ALSO has no production caller anymore (`ChangeManifest.adopt` likewise has
none): see its own docstring and docs/SECURITY.md S22. It is kept for its
unit-level tests only; do not wire it back into `orchestrator.py` without a
fresh design review.

**Produce-then-review path** (`commit_and_capture` / `push_exact`, alongside
`worktask.py` and `environment.py` — the only commit/push path with a live
production caller after the S21 retirement): unlike the two commit primitives
above, which verify BEFORE committing, this path commits first with hooks
enabled, reads the resulting sha honestly from `rev-parse HEAD` (never
predicted), and leaves review to `range_diff`/`commit_range_paths` reading
the immutable commit that already exists. `push_exact` then publishes ONLY an
explicit, already-resolved `<sha>:<dest_ref>` refspec — never a branch name —
with `+`-prefix and non-hex-source refspecs refused at both this layer and
the policy layer (F2), and any `url.*.insteadOf` rule refused outright before
any network access (F5). See `worktask.py` for the crash-recovery story
(`CommitIntent` / `reconcile_after_crash`, F8) and `environment.py` for
detecting a hook or push destination that changed mid-task. `worktree_add` /
`worktree_remove` / `worktree_prune` / `worktree_list_porcelain` /
`branch_exists` give each task its own linked worktree and branch (see
`worktree.py`'s `WorktreeManager`); the commit/verify sequence for a task's
changes then runs through a SEPARATE `GitGateway` rooted at that worktree's
path, not this one.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Sequence

from .errors import EnvironmentDriftError, GitCommandError, GitOperationDenied
from .policy import PolicyEngine
from .worktask import CommitIntent, IntentStore

#: A push refspec's source must be a literal, already-resolved 40-hex commit
#: id — never a branch name, tag, or `HEAD`, any of which can move between
#: review and push.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
#: `refs/heads/<name>`, matching the policy layer's shape. The character
#: class deliberately excludes `+` and does not by itself exclude `..` (a
#: name like `refs/heads/a/../b` matches it), so `..` is checked separately.
_BRANCH_REF_RE = re.compile(r"^refs/heads/[A-Za-z0-9._/-]+$")


class GitGateway:
    def __init__(self, repo_root: Path, policy: PolicyEngine, runner=None, env: dict | None = None):
        """`env`, when given, is passed to every subprocess invocation this
        gateway makes (`subprocess.run(..., env=env)`), REPLACING the
        inherited process environment rather than layering on top of it —
        exactly `subprocess.run`'s own `env=` semantics. `None` (the
        default) means "inherit the current process environment", identical
        to every caller before this parameter existed — zero behavior change
        for any existing construction site.

        This is how a `GitGateway` rooted at a worker repo is made to
        actually SEE the scrubbed environment `worker_env.worker_env()`
        builds: without an explicit `env`, every git subprocess this class
        runs would inherit whatever ambient credential helper / system
        config the CALLING process happens to have, regardless of how
        isolated the worker repo's own on-disk git config is — verified:
        constructing a plain `GitGateway` (no `env`) against a freshly
        created no-remote worker repo and reading `credential.helper`
        through it still reported the CALLING process's ambient
        `osxkeychain` helper, because `git config --get-regexp` walks the
        system/global config layers of whatever environment the subprocess
        itself runs under, not the repo's local config alone.
        """
        self._repo_root = Path(repo_root)
        self._policy = policy
        self._runner = runner or subprocess.run
        self._env = env

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
            env=self._env,
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
        proc = self._runner(
            ["git", *args], cwd=str(self._repo_root), capture_output=True, env=self._env
        )
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

    @staticmethod
    def _parse_status_porcelain_z(raw: str) -> list[tuple[str, str]]:
        """Shared NUL-record parser for `git status --porcelain -z` output,
        with or without `-uall`. Rename/copy entries carry the ORIGINAL path
        as a second record, consumed here so callers never see it."""
        records = [r for r in raw.split("\0") if r]
        entries: list[tuple[str, str]] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if len(record) < 4:
                continue
            status, path = record[:2], record[3:]
            if status and status[0] in ("R", "C"):
                index += 1
            entries.append((status, path))
        return entries

    def dirty_entries(self) -> list[tuple[str, str]]:
        """(XY status, path) for every pending change, NUL-parsed.

        `git status --porcelain` without `-z` QUOTES and escapes paths holding
        spaces, tabs or non-ASCII (`?? "has\\ttab.txt"`), so line splitting
        yields a string that matches no real path. Every security decision that
        depends on a pathname uses this NUL-delimited form instead.

        Untracked files use git's default collapsed reporting: a brand-new
        directory shows as one entry for the directory itself (`?? d/`), not
        one per file inside it. Every existing caller of this method only
        ever needs "is anything dirty" / "what top-level things changed", so
        that collapsing is harmless here. `dirty_entries_all` below is the
        one to use when a caller needs the LITERAL file paths.
        """
        raw = self._git_bytes("status", "--porcelain", "-z").decode("utf-8", "surrogateescape")
        return self._parse_status_porcelain_z(raw)

    def dirty_entries_all(self) -> list[tuple[str, str]]:
        """Like `dirty_entries`, but with `-uall`
        (`--untracked-files=all`): a new file inside a brand-new directory is
        reported as its OWN full path (`?? d/f.py`) rather than collapsed to
        the directory (`?? d/`) — verified empirically (`git status
        --porcelain -z` vs `-z -uall` against a freshly created `d/f.py`).

        This matters wherever the result is turned into an exact path set
        that must literally match what git itself will diff, e.g.
        `ImplementExecutor`'s `changed_paths`: that set becomes both
        `commit_and_capture`'s staged-paths argument (`git add --
        <paths>`, which stages a directory recursively and would "work") AND
        the post-commit structural check's expected-paths set, which compares
        against `commit_range_paths` — LITERAL file paths. `services/new/`
        is not a superset match for `services/new/foo.py`, so a collapsed
        directory entry there is a reproducible false structural refusal for
        the ordinary case of an agent creating a new package directory.

        Kept as a SEPARATE method rather than changing `dirty_entries`'s
        default: every other caller of `dirty_entries` only needs "is
        anything dirty", and `-uall` is a materially more expensive stat walk
        that nothing else here should pay for.
        """
        raw = self._git_bytes("status", "--porcelain", "-z", "-uall").decode(
            "utf-8", "surrogateescape"
        )
        return self._parse_status_porcelain_z(raw)

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

    def dirty_paths_all(self) -> set[str]:
        """`dirty_paths`, but backed by `dirty_entries_all` — see that
        method's docstring for why a caller needing literal file paths
        (rather than "is anything dirty") must use this instead."""
        return {path for _status, path in self.dirty_entries_all()}

    def staged_paths(self) -> set[str]:
        raw = self._git_bytes("diff", "--cached", "--name-only", "-z").decode(
            "utf-8", "surrogateescape"
        )
        return {p for p in raw.split("\0") if p}

    # ---- checkout enumeration (M1 escape detector) ---------------------------
    #
    # `git status` (even with `-uall`) only reports what CHANGED against the
    # index, and by default collapses an ignored directory to one entry for
    # the directory itself — neither is what a filesystem snapshot needs: it
    # must cover every tracked path regardless of whether it currently
    # matches the index (so a working-tree-only edit to an untouched tracked
    # file is comparable byte-for-byte across two snapshots), and every
    # ignored path individually (so a write inside an ignored directory is
    # not hidden behind a single collapsed directory entry). `git ls-files`
    # does neither collapsing.

    def list_tracked_paths(self) -> list[str]:
        """Every tracked path (`git ls-files -z`) — independent of whether
        the working tree currently matches the index, unlike `dirty_*`,
        which only reports what CHANGED."""
        raw = self._git_bytes("ls-files", "-z").decode("utf-8", "surrogateescape")
        return [p for p in raw.split("\0") if p]

    def list_untracked_paths(self) -> list[str]:
        """Untracked, non-ignored paths, one per file — `.gitignore`d
        content is excluded here and reported separately by
        `list_ignored_paths` instead."""
        raw = self._git_bytes("ls-files", "-z", "--others", "--exclude-standard").decode(
            "utf-8", "surrogateescape"
        )
        return [p for p in raw.split("\0") if p]

    def list_ignored_paths(self) -> list[str]:
        """Ignored paths, one per file where git can see individual files —
        a nested git repository (a real `.git` boundary git will not cross,
        e.g. a stale pre-external-workers-root worker repo left on disk under
        the checkout) is reported as ONE entry for its own root instead, since
        git itself refuses to enumerate inside a repository boundary."""
        raw = self._git_bytes(
            "ls-files", "-z", "--others", "--ignored", "--exclude-standard"
        ).decode("utf-8", "surrogateescape")
        return [p for p in raw.split("\0") if p]

    def config_get(self, key: str) -> str:
        """`git config --get <key>`, or "" if unset.

        `git config --get` exits 1 when the key is not set — that is a
        normal "not configured" outcome, not a command failure, so this
        calls `_git` with `check=False` rather than letting the default
        `check=True` raise on it.
        """
        proc = self._git("config", "--get", key, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def config_get_all(self, key: str) -> list[str]:
        """EVERY value for a multi-valued config key. `config --get` returns
        only the LAST value, which is why `remote.<n>.url` needed this: git
        pushes to ALL configured urls, so reading one and calling it "the"
        destination misses an exfiltration remote entirely."""
        proc = self._git("config", "--get-all", key, check=False)
        if proc.returncode != 0:
            return []
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def config_get_regexp(self, pattern: str) -> str:
        """`git config --get-regexp <pattern>`, or "" if nothing matches.

        Used instead of `git remote -v` / `git remote get-url` wherever a
        check needs the LITERAL configured value: both of those commands
        report the value AFTER a `url.*.insteadOf` rewrite is applied, which
        is exactly the thing F5 needs to detect rather than transparently
        reflect.
        """
        proc = self._git("config", "--get-regexp", pattern, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def commit_range_paths(self, base_sha: str, candidate_sha: str) -> set[str]:
        """Every path that differs between two commits, diffing their trees
        DIRECTLY (same plumbing as `changed_paths`) rather than walking
        history — so a path touched only by an intermediate commit between
        `base_sha` and `candidate_sha` (e.g. one a hook created) is never
        missed just because it isn't the final state of some other path.

        NUL-delimited (`-z`): without it, `diff-tree`'s default output quotes
        and escapes paths containing tabs or double quotes, and a caller
        keying a security decision on the pathname would compare the wrong
        string.
        """
        return self.changed_paths(base_sha, candidate_sha)

    def is_descendant(self, candidate: str, base: str) -> bool:
        """True if `base` is `candidate` itself or an ancestor of it.

        `git merge-base --is-ancestor` exits 0 for "yes" and 1 for "no" —
        both are normal outcomes of asking the question, not command
        failures — so this treats only those two codes specially. Any other
        exit code (e.g. an unresolvable object) is a genuine error and is
        raised rather than silently reported as "not a descendant".
        """
        proc = self._git("merge-base", "--is-ancestor", base, candidate, check=False)
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        raise GitCommandError(
            f"git merge-base --is-ancestor {base} {candidate} failed "
            f"(rc={proc.returncode}): {(proc.stderr or proc.stdout).strip()}"
        )

    #: Above this many bytes, `range_diff`/`range_diff_stat` refuse outright
    #: rather than truncate. A truncated diff can hide exactly the change a
    #: reviewer needs to see, which would be worse than an explicit refusal.
    RANGE_DIFF_MAX_BYTES = 400_000

    #: Flags common to `range_diff` and `range_diff_stat`: no external diff
    #: driver, no textconv filter, no ANSI color in the reviewed text, and a
    #: rename always shown as delete+add rather than hidden behind a rename
    #: heuristic.
    _RANGE_DIFF_SAFETY_FLAGS = (
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-renames",
    )

    def range_diff(self, base_sha: str, candidate_sha: str) -> str:
        """The full patch text between two commits, plumbing-rendered so no
        external diff driver or textconv filter can execute and no rename
        heuristic can hide a path's real diff.

        Refuses (raises `GitCommandError`) above `RANGE_DIFF_MAX_BYTES`
        rather than truncating.
        """
        raw = self._git_bytes(
            "diff-tree", "-r", "-p", *self._RANGE_DIFF_SAFETY_FLAGS, base_sha, candidate_sha
        )
        if len(raw) > self.RANGE_DIFF_MAX_BYTES:
            raise GitCommandError(
                f"range diff {base_sha[:12]}..{candidate_sha[:12]} is {len(raw)} "
                f"bytes, over the {self.RANGE_DIFF_MAX_BYTES}-byte cap — refusing "
                "rather than truncating a diff a reviewer must see in full"
            )
        return raw.decode("utf-8", "replace")

    def range_diff_stat(self, base_sha: str, candidate_sha: str) -> str:
        """Like `range_diff` but the `--stat` summary only, same safety
        flags and the same refuse-rather-than-truncate cap."""
        raw = self._git_bytes(
            "diff-tree", "-r", "--stat", *self._RANGE_DIFF_SAFETY_FLAGS, base_sha, candidate_sha
        )
        if len(raw) > self.RANGE_DIFF_MAX_BYTES:
            raise GitCommandError(
                f"range diff stat {base_sha[:12]}..{candidate_sha[:12]} is "
                f"{len(raw)} bytes, over the {self.RANGE_DIFF_MAX_BYTES}-byte cap "
                "— refusing rather than truncating"
            )
        return raw.decode("utf-8", "replace")

    def branch_exists(self, branch: str) -> bool:
        """True if `refs/heads/<branch>` resolves. Used by `WorktreeManager`
        to refuse creating a worktree on a branch name that is already taken,
        BEFORE `git worktree add -b` fails with its own (less specific)
        error."""
        proc = self._git("rev-parse", "--verify", f"refs/heads/{branch}", check=False)
        return proc.returncode == 0

    def worktree_list_porcelain(self) -> str:
        return self._out("worktree", "list", "--porcelain")

    def commit_list(self, base_sha: str, candidate_sha: str) -> list[dict]:
        """Commits strictly after `base_sha` up to and including
        `candidate_sha`, oldest first, each as `{"sha", "subject", "parents"}`.

        `rev-list` alone fixes the exact ordered commit set (newest first, so
        reversed here for an oldest-first report); metadata for each comes
        from `read_commit` (already-tested `cat-file commit` parsing) rather
        than a `--format=%H %P %s` line, which is only unambiguous when the
        subject never starts with a token that itself looks like a 40-hex
        parent sha — not a guarantee this can make. No `log`-specific flags
        are needed as a result.
        """
        raw_shas = self._out("rev-list", f"{base_sha}..{candidate_sha}")
        shas = [s for s in raw_shas.splitlines() if s]
        shas.reverse()  # rev-list is newest-first; report oldest-first
        commits = []
        for sha in shas:
            info = self.read_commit(sha)
            message = info.get("message", "")
            lines = message.splitlines()
            subject = lines[0] if lines else ""
            commits.append({"sha": sha, "subject": subject, "parents": info.get("parents", [])})
        return commits

    # ---- write (only reachable from explicitly approved directives) ---------
    #
    # `commit()` — the legacy authorize-then-produce/manifest commit method,
    # a plain `git commit` gated only on manifest provenance — was removed
    # here 2026-07-30 (docs/SECURITY.md S21: closed by retirement, not by a
    # fix; a `pre-commit` hook could rewrite an approved path's bytes or
    # stage an extra unapproved file AFTER the manifest check ran, and both
    # would land in the commit — reproduced, see S21's detail entry). Its
    # only caller (`orchestrator.py`'s `_dispatch_git`) was removed in the
    # same change. `commit_and_capture` (below) and `commit_adopted` (further
    # down) are the two remaining commit primitives; neither has this hole —
    # see their own docstrings.

    # ---- worktree lifecycle (produce-then-review: one worktree per task) ---
    #
    # Run from the MAIN checkout's gateway, never from inside a worktree being
    # managed — `WorktreeManager` is constructed with the main repo's
    # GitGateway. The commit/verify sequence for a task's own changes then
    # runs through a SEPARATE GitGateway rooted at `worktree_path`.

    def worktree_add(self, path: str, branch: str, base_sha: str) -> None:
        """`git worktree add -b <branch> <path> <base_sha>` — always creates a
        NEW branch (never attaches an existing one), so the only way this can
        collide with another worktree is the pre-checks in `WorktreeManager`.
        Git itself still refuses to check the same branch out twice in two
        worktrees; that refusal is relied on as defense in depth here, not
        reimplemented."""
        self._git("worktree", "add", "-b", branch, path, base_sha)

    def worktree_remove(self, path: str, force: bool = False) -> None:
        """`git worktree remove [--force] <path>`. Never deletes the branch —
        the branch (and its commits) survive worktree removal."""
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(path)
        self._git(*args)

    def worktree_prune(self) -> None:
        self._git("worktree", "prune")

    # ---- integration (auto_merge.py) ----------------------------------------
    #
    # The only place in this gateway that moves the CHECKOUT's own branch head.
    # Everything else either reads, or writes a commit onto a worker's separate
    # worktree, or publishes an already-resolved sha by refspec. Both methods
    # here are deliberately thin: the ordering, the gate, the verification and
    # the abort-on-conflict discipline all live in `auto_merge.py`, so this
    # class keeps its "one git invocation per method, no policy of its own"
    # shape.

    #: Conflict markers in `git status --porcelain`'s XY field. `U` on either
    #: side is git's unmerged flag; `AA` (both added) and `DD` (both deleted)
    #: are the two conflict states that use no `U` at all.
    _CONFLICT_STATUSES = ("DD", "AA")

    def merge_commit(self, sha: str, message: str) -> None:
        """`git merge --no-ff --no-edit -m <message> <sha>` in this checkout.

        `--no-ff` on purpose: a real merge commit records WHEN a task's
        candidate was integrated and keeps the base's first-parent history a
        list of integrations. A fast-forward would move the head just as well,
        but the caller then has no commit of its own to name in a log or a
        deferral record.

        Raises `GitCommandError` on any non-zero exit, conflicts included —
        the caller is expected to read `conflicted_paths()` BEFORE calling
        `merge_abort()`, because the abort clears them.

        Nothing here validates `sha`: the policy layer independently refuses
        anything that is not a literal 40-hex commit id (`sub == "merge"` in
        `validate_git_command`), so a branch name cannot reach git even if a
        caller passes one.
        """
        self._git("merge", "--no-ff", "--no-edit", "-m", message, sha)

    def merge_abort(self) -> None:
        """`git merge --abort` — restore the checkout to exactly its
        pre-merge state. Never combined with any other flag (the policy layer
        refuses that shape outright).

        A zero exit is not by itself evidence the checkout is clean again;
        `auto_merge.py` re-reads `head_sha()` and `is_dirty()` afterwards,
        the same discipline `push_exact` applies to its own confirmation.
        """
        self._git("merge", "--abort")

    def conflicted_paths(self) -> list[str]:
        """Paths git currently reports as unmerged, sorted.

        Read from `dirty_entries()` rather than `git diff --diff-filter=U`,
        which would need a wider `diff` flag whitelist for information the
        status output already carries. NUL-parsed via `dirty_entries`, so a
        conflicted path containing a space or a tab is reported literally.
        """
        return sorted(
            path
            for status, path in self.dirty_entries()
            if "U" in status or status in self._CONFLICT_STATUSES
        )

    # ---- produce-then-review commit path ------------------------------------
    #
    # Unlike `commit_adopted`, hooks are expected to run here — the artifact
    # under review is the resulting commit's diff (`range_diff`), read from
    # immutable git objects AFTER the commit exists, not a pre-verified tree.
    # This is the "produce" half of produce-then-review: nothing here decides
    # whether the commit is good, only that the intent survives a crash and
    # the sha is read honestly.

    def commit_and_capture(
        self,
        message: str,
        paths: tuple[str, ...],
        intent_store: IntentStore,
        intent: CommitIntent,
        env_snapshot=None,
    ) -> tuple[str, str]:
        """Write `intent` durably, stage exactly `paths`, run a NORMAL
        `git commit` (hooks enabled, never `--no-verify`), then read the
        candidate sha from `rev-parse HEAD` only after the commit call
        returns. Returns `(candidate_sha, staged_diff_summary)`.

        Sequence, in order: (1) `intent_store.save(intent)` — durable BEFORE
        anything else runs; (2) `git add -- <paths>`; (3) `git commit -m
        <message>`; (4) `rev-parse HEAD`. The sha is never predicted or
        precomputed — step 4 is the only source of truth for what was
        actually committed, because a hook can change the tree between steps
        2 and 3 in ways nothing upstream of `rev-parse HEAD` could know about.

        Paths are NOT stripped: a leading/trailing space or tab in a filename
        is part of its identity (unlike `commit`, which strips — this method
        deliberately does not inherit that). Only fully empty/whitespace-only
        entries are dropped as invalid.

        Deliberately does NOT inherit `commit`'s message-equality idempotency
        shortcut ("nothing to commit, but HEAD's message already matches" ->
        treat as already done): F8 exists precisely because message + parent
        + author identity cannot distinguish this task's commit from an
        unrelated one, so that shortcut would reintroduce the exact ambiguity
        `reconcile_after_crash` (`worktask.py`) is built to resolve instead.
        Crash recovery for this path goes through `reconcile_after_crash`,
        never through a message match.
        """
        approved = tuple(p for p in paths if p and p.strip())
        if not approved:
            raise GitCommandError(
                "commit_and_capture requires an explicit non-empty path list"
            )
        if env_snapshot is not None:
            # A hook installed since the task started would run during the
            # `git commit` below. Detecting it afterwards is too late: the
            # commit already exists and the hook has already run.
            from .environment import verify_unchanged

            drift = verify_unchanged(env_snapshot, self)
            if drift:
                raise EnvironmentDriftError(
                    "commit_and_capture refuses: the git environment changed "
                    "since this task started — " + "; ".join(drift)
                )
        head = self.head_sha()
        if head != intent.expected_parent_sha:
            # The intent was built against a different HEAD than the one we are
            # about to commit onto. Refusing here beats committing and letting
            # `reconcile_after_crash` sort it out afterwards: that would classify
            # the result AMBIGUOUS (correct, but only discoverable after a crash),
            # whereas the mismatch is knowable right now, before anything is
            # staged or any hook runs.
            raise GitCommandError(
                f"commit_and_capture refuses: HEAD is {head[:12]} but the commit "
                f"intent expects parent {intent.expected_parent_sha[:12]}. The "
                "branch moved after the intent was recorded — rebuild the intent "
                "against the current HEAD rather than committing onto a base "
                "nobody planned for."
            )
        intent_store.save(intent)
        self._git("add", "--", *sorted(approved))
        summary = self._out("diff", "--cached", "--stat")
        # The intent nonce goes INTO the commit message so crash recovery can
        # establish provenance rather than inferring it from shape.
        full_message = message
        if getattr(intent, "nonce", ""):
            full_message = f"{message}\n\n{intent.trailer()}"
        self._git("commit", "-m", full_message)
        candidate_sha = self._out("rev-parse", "HEAD")
        return candidate_sha, summary

    def _remote_ref_map(self, remote: str) -> dict:
        """Every ref the remote advertises, as {refname: oid}. Used to prove a
        push changed exactly one ref and published no tag or extra branch."""
        out = {}
        proc = self._git("ls-remote", remote, check=False)
        if proc.returncode != 0:
            return out
        for line in proc.stdout.splitlines():
            parts = line.split("\t")
            # Only real refs. `HEAD` is a symref that resolves as soon as a
            # bare repo gains its first branch, so comparing it would flag a
            # normal first push as an unexpected extra ref.
            if len(parts) == 2 and parts[1].startswith("refs/"):
                out[parts[1]] = parts[0]
        return out

    def remote_ref_sha(self, remote: str, dest_ref: str) -> str:
        """The sha `dest_ref` currently points to on `remote`, or "" if the
        ref does not exist there. Always a fresh `ls-remote` round-trip —
        never a previous push's captured output — which is exactly the
        distinction `push_exact` relies on to confirm what actually landed."""
        raw = self._out("ls-remote", "--heads", remote, dest_ref)
        if not raw:
            return ""
        first_line = raw.splitlines()[0]
        sha, _, _ref = first_line.partition("\t")
        return sha.strip()

    def fetch_object(self, source_path: str, want_sha: str) -> None:
        """`git fetch <source_path> <want_sha>` — pull exactly one
        already-existing object, by its literal 40-hex id, from a LOCAL
        filesystem path. Used by `publisher.py`'s `Publisher.import_candidate`
        to bring a worker's candidate commit into the publisher's object
        database without ever configuring a remote for the worker.

        No refspec destination is given, so this never creates or moves a ref
        in THIS repository — the object becomes reachable in the object
        database and is anchored only via `FETCH_HEAD`, which is all a
        caller needs: every later reference to the object (verification,
        `push_exact`) is by its literal sha, never by a ref. Repeating the
        fetch is harmless — fetching an object already present is a normal,
        side-effect-free no-op (verified against a real repo).

        The policy layer (F2-style, `policy.py`) independently enforces that
        `want_sha` is a literal 40-hex id with no ':' or '+' and that
        `source_path` is an absolute filesystem path with no URL scheme —
        this method does not re-derive those checks, it relies on
        `validate_git_command` refusing before any subprocess runs.

        This does NOT verify the fetched object's type or identity — that is
        the caller's job (`Publisher.import_candidate` calls `read_commit`
        immediately afterward, which raises on anything that is not a valid
        commit object).

        No `-c credential.helper=` belt-and-braces here (unlike
        `WorkerRepoManager`'s own `fetch` call in `worker_env.py`, which adds
        it): `source_path` is already policy-verified to be an absolute
        local filesystem path with no URL scheme, so credential resolution
        cannot fire for it regardless; adding a global `-c` override would
        require teaching `validate_git_command` to recognize and strip a
        `-c key=value` PREFIX before its subcommand check, which is a
        policy-layer change wider than this method needs.
        """
        self._git("fetch", source_path, want_sha)

    def push_exact(
        self,
        remote: str,
        sha: str,
        dest_ref: str,
        protected_refs: Sequence[str],
        expected_url: str | None = None,
        env_snapshot=None,
    ) -> str:
        """Push exactly one already-existing commit to exactly one ref, by an
        explicit `<sha>:<dest_ref>` refspec — never a bare branch push (which
        pushes whatever the current branch happens to be) and never able to
        force anything, because the refspec is built ONLY by concatenating
        the two values this method itself validates below.

        Every check runs BEFORE any network access:

          * `sha` must be a literal 40-hex commit id — a branch name, tag or
            `HEAD` can move between review and push, so only an
            already-resolved id may be pinned into the refspec;
          * `dest_ref` must match `refs/heads/<name>`, contain no `..`
            (checked separately from the regex: a name like
            `refs/heads/a/../b` matches the character class but is not a
            single flat name), and its branch part must not be in
            `protected_refs` — checked against BOTH the bare name and the
            full ref, so a caller cannot dodge the check by passing whichever
            form it happens to skip;
          * no `+` may appear anywhere in the constructed refspec (F2). Given
            the two regexes above, neither half can actually contain `+`, so
            this is unreachable today — kept anyway as an independent check
            that still stands between a force-push refspec and the network
            if either regex is ever loosened later;
          * no `url.*.insteadOf` rule may be configured ANYWHERE in the
            repository's git config (F5). This refuses on the rule's mere
            PRESENCE rather than trying to reason about whether a specific
            rule would rewrite this specific remote — `git remote -v` /
            `git remote get-url` reflect the rewritten destination, not the
            configured one, so presence is the only signal that does not
            depend on reproducing git's own URL-matching logic;
          * `remote.<remote>.pushurl` must NOT be configured. When set, git
            pushes there INSTEAD of `remote.<remote>.url` — verified
            empirically: pointing `url` at one bare repo and `pushurl` at a
            second sends the commit to the second while `git config --get
            remote.<remote>.url` (and this method's own url check below)
            keeps reporting the first, unchanged. Refusing on presence,
            exactly like the `insteadOf` check, is the only response that
            does not depend on reasoning about which URL git will actually
            use in a given git version;
          * `remote.<remote>.url` is read directly via `config --get` (never
            `get-url`, same reason) and must be non-empty; if the caller
            supplies `expected_url`, it must match exactly.

        After the push, `ls-remote --heads <remote> <dest_ref>` — a fresh
        network round-trip, never the push command's own captured output —
        must report exactly `sha`; anything else raises. Returns that
        confirmed sha.
        """
        if not _SHA_RE.match(sha or ""):
            raise GitCommandError(f"push_exact refuses a non-40-hex sha: {sha!r}")
        if ".." in (dest_ref or "") or not _BRANCH_REF_RE.match(dest_ref or ""):
            raise GitCommandError(
                f"push_exact refuses dest_ref {dest_ref!r}: must match "
                "'refs/heads/<name>' with no '..'"
            )
        branch_name = dest_ref[len("refs/heads/"):]
        protected = set(protected_refs)
        if branch_name in protected or dest_ref in protected:
            raise GitCommandError(
                f"push_exact refuses protected ref {dest_ref!r} (branch {branch_name!r})"
            )
        refspec = f"{sha}:{dest_ref}"
        if "+" in refspec:
            raise GitCommandError(
                f"push_exact refuses a '+' anywhere in the refspec: {refspec!r}"
            )

        instead_of = self.config_get_regexp(r"^url\..*\.insteadof$")
        if instead_of:
            raise GitCommandError(
                "push_exact refuses: url.*.insteadOf rule(s) are configured "
                f"({instead_of!r}). These silently redirect a push to another "
                "host, and `git remote -v`/`git remote get-url` would show the "
                "REWRITTEN destination rather than the configured one — so the "
                "only response that does not depend on reasoning about which "
                "rule 'would' apply is refusing on their mere presence."
            )

        pushurl = self.config_get(f"remote.{remote}.pushurl")
        if pushurl:
            raise GitCommandError(
                f"push_exact refuses: remote.{remote}.pushurl is configured "
                "— git would push THERE instead of remote."
                f"{remote}.url, silently bypassing the url check below. The "
                "value is deliberately not shown: a pushurl can carry embedded "
                "credentials (https://user:token@host), and this message reaches "
                "the parked question, the transcript and the persisted blocker "
                "record. Inspect it locally with "
                f"`git config --get remote.{remote}.pushurl`."
            )

        if env_snapshot is not None:
            from .environment import verify_unchanged

            drift = verify_unchanged(env_snapshot, self)
            if drift:
                raise EnvironmentDriftError(
                    "push_exact refuses: the git environment changed since this "
                    "task started — " + "; ".join(drift)
                )
        _hookdir, push_hooks = self.active_push_hooks()
        if push_hooks:
            raise GitCommandError(
                f"push_exact refuses: active push hook(s) {push_hooks} in "
                f"{_hookdir}. A pre-push hook runs arbitrary code during this "
                "push and was reproduced publishing an unreviewed commit to a "
                "protected branch. It was NOT executed and NOT bypassed."
            )
        # `push.followTags` publishes ANNOTATED tags alongside an explicit sha
        # refspec — verified: a `v1` tag landed on the remote from a
        # `<sha>:refs/heads/x` push. A tag is an unreviewed ref, so this is
        # publication outside the reviewed operation.
        follow_tags = self.config_get("push.followTags").strip().lower()
        if follow_tags in ("true", "1", "yes", "on"):
            raise GitCommandError(
                "push_exact refuses: push.followTags is enabled, which publishes "
                "annotated tags alongside the explicit refspec — refs nobody "
                "reviewed. Unset it for this repository."
            )
        # `remote.<n>.mirror` makes git refuse refspecs outright today, but
        # refusing here keeps the failure legible rather than a raw git fatal.
        if self.config_get(f"remote.{remote}.mirror").strip().lower() in ("true", "1", "yes", "on"):
            raise GitCommandError(
                f"push_exact refuses: remote.{remote}.mirror is enabled — mirror "
                "semantics publish the whole ref namespace, not one reviewed commit."
            )
        # A remote may carry MORE THAN ONE url, and git pushes to every one of
        # them — verified: two configured urls both received the commit while
        # `config --get` reported only one. Reading a single url and calling it
        # "the destination" would miss an added exfiltration remote entirely.
        all_urls = self.config_get_all(f"remote.{remote}.url")
        if len(all_urls) > 1:
            raise GitCommandError(
                f"push_exact refuses: remote {remote!r} has {len(all_urls)} "
                f"configured urls ({all_urls}) and git pushes to ALL of them. "
                "Exactly one destination must be configured."
            )
        configured_url = self.config_get(f"remote.{remote}.url")
        if not configured_url:
            raise GitCommandError(f"push_exact refuses: remote {remote!r} has no configured url")
        if expected_url is not None and configured_url != expected_url:
            raise GitCommandError(
                f"push_exact refuses: remote.{remote}.url is {configured_url!r}, "
                f"caller expected {expected_url!r}"
            )

        # Snapshot the remote's ref namespace so anything that appears beyond
        # the one approved ref is detectable. Post-hoc, but publication of an
        # extra ref is exactly what the checks above are trying to prevent, and
        # a residual surprise must not pass silently.
        refs_before = self._remote_ref_map(remote)

        self._git("push", remote, refspec)

        # Re-assert the redirection config BEFORE trusting the confirmation
        # below: `ls-remote` follows the FETCH url, so a rule added between the
        # pre-push checks and this point would confirm against a different
        # repository than the push actually reached.
        if self.config_get_regexp(r"^url\..*\.insteadof$") or self.config_get(
            f"remote.{remote}.pushurl"
        ):
            raise GitCommandError(
                "push_exact: push destination config changed during the push — "
                "the post-push confirmation cannot be trusted"
            )
        refs_after = self._remote_ref_map(remote)
        unexpected = {
            name: oid
            for name, oid in refs_after.items()
            if name != dest_ref and refs_before.get(name) != oid
        }
        if unexpected:
            raise GitCommandError(
                f"push_exact: the push changed refs beyond {dest_ref} on {remote}: "
                f"{sorted(unexpected)}. Nothing here can un-publish them — "
                "investigate the repository's push configuration and hooks."
            )
        landed = self.remote_ref_sha(remote, dest_ref)
        if landed != sha:
            raise GitCommandError(
                f"push_exact: after pushing, {remote}'s {dest_ref} is {landed!r}, "
                f"expected {sha!r}"
            )
        return landed

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

    #: The hook `git push` can invoke. Tracked separately because it fires
    #: inside `push_exact`'s own push, not at commit time: a `pre-push` hook
    #: running `git push origin HEAD:refs/heads/main` was reproduced publishing
    #: an unreviewed commit to a protected branch as a side effect of an
    #: otherwise correct exact-refspec push.
    PUSH_HOOKS = ("pre-push",)

    def hooks_dir(self) -> Path:
        """Effective hooks directory as GIT resolves it (honours core.hooksPath)."""
        raw = self._out("rev-parse", "--git-path", "hooks")
        path = Path(raw)
        return path if path.is_absolute() else self._repo_root / path

    def active_hooks(self, names: "tuple[str, ...]") -> tuple[Path, list[str]]:
        """(effective hooks dir, active hooks among `names`). See
        `active_commit_hooks` for what "active" means."""
        directory = self.hooks_dir()
        active = [
            name
            for name in names
            if os.access(directory / name, os.X_OK) and (directory / name).is_file()
        ]
        return directory, active

    def active_push_hooks(self) -> tuple[Path, list[str]]:
        """(effective hooks dir, active push hooks). Checked immediately before
        `git push`, because a hook can be installed after the task snapshot."""
        return self.active_hooks(self.PUSH_HOOKS)

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

        **NON-PRODUCTION / UNREACHABLE from any CLI route or dispatch path
        (2026-07-30, docs/SECURITY.md S22).** This method's own design closed
        S21 (a `pre-commit` hook rewriting content after verification) the
        right way — build and verify an immutable tree, then `commit-tree` +
        `update-ref` compare-and-swap, refusing outright if any commit hook
        is active. But its only production caller was `orchestrator.py`'s
        `_dispatch_git`, removed in the same change that retired the sibling
        (vulnerable) `commit()` method above; `ChangeManifest.adopt`
        (`manifest.py`), the thing that would produce an adopted manifest to
        commit here, likewise has no production caller. Nothing in `cli.py`
        or `orchestrator.py` constructs the inputs this method needs anymore.
        It is kept, unmodified, because it is still exercised directly by
        `test_git_gateway.py` / `test_manifest.py` (unit-level primitive
        tests, not integration through the orchestrator) — do not repair or
        re-wire it without a fresh design review; see S22.

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
