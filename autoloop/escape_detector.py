"""Filesystem escape DETECTOR for the primary checkout, bracketing a
write-capable agent invocation.

READ THIS BEFORE TRUSTING IT. This module is an ESCAPE DETECTOR, not an OS
security sandbox. It does not stop a write-capable agent from touching
anything on disk — nothing here has that power, because the agent runs as an
ordinary subprocess with the same filesystem permissions as the orchestrator
itself (see `docs/AUTOLOOP.md` and `docs/SECURITY.md` finding #1). What it
DOES do: take a deterministic snapshot of the primary checkout immediately
before a write-capable agent runs, take a second one immediately after, and
report — paths only, never contents — anything that changed. That is
after-the-fact detection, checked BEFORE the resulting work is committed or
reviewed, not prevention. A sufficiently fast or careful escape (e.g. writing
and then reverting a file before the "after" snapshot runs) is not caught by
this or by any other mechanism in this codebase; closing that class of gap
needs real process/filesystem sandboxing (a container, a chroot, an OS-level
jail), which is out of scope here — see docs/SECURITY.md.

WHY THE WINDOW IS NARROW, NOT THE EXCLUSION LIST. An earlier design excluded
Autoloop's own volatile files (state.json, transcript.jsonl, the blocker/
execution/intent stores, the publisher repo) from the snapshot, on the theory
that the orchestrator itself writes them across a task's lifecycle. That
directly conflicts with the requirement that tampering with those exact files
BY THE AGENT be detected. The fix is to snapshot around the narrowest
possible window instead — immediately before and after the write-capable
`TaskExecutor.execute()` call — rather than around the whole task dispatch.
`ImplementExecutor` (the only write-capable executor) never writes to
`.autoloop/` itself (see its own module docstring); the orchestrator's own
writes to state.json / the execution store / the blocker store all happen
strictly before the "before" snapshot or strictly after the "after" one is
compared (`orchestrator.py`'s `_dispatch_task_postcommit`). So the exclusion
list this module needs is EMPTY, by measurement, not by omission — and that
is strictly stronger than a broad-window design with those paths excluded:
it means an agent that tampers with state.json or a blocker record DURING
its run is caught, which a wide-window exclusion-list design could not do.
`AuditExecutor` (read-only tools, but it DOES write Markdown reports under
`.autoloop/audit/` by design) is deliberately NOT wrapped by this detector —
see `orchestrator.py`'s call site, which applies it only to the non-audit
(write-capable) dispatch path.

SCOPE: the checkout's WORKING TREE, not `.git/` internals. "Tracked,
untracked and ignored" is exactly the three categories `git status`
enumerates over the working tree; a write into `.git/hooks/`, `.git/config`,
or `.git/refs/*` in the PRIMARY checkout (as opposed to the task's own
isolated worker repo, which already has its own dedicated defenses — see
`worker_env.py`) is NOT covered by this module. That is a real, separate,
more severe class of escape (a planted commit hook or rewritten remote would
compromise every future commit in the checkout) that this pass does not
close — recorded honestly as an open gap in docs/SECURITY.md rather than
silently left unmentioned. `Orchestrator` DOES separately hash the checkout's
own `.git` worktree-pointer file resolution to catch the narrower, high-value
case of `workers_root` having been pointed inside a linked worktree's real
git-common-dir (see `worker_env.validate_workers_root`) — that is a
config-validation check, not something this module performs.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from .git_gateway import GitGateway


@dataclass(frozen=True)
class PathState:
    #: "file" | "symlink" | "dir_boundary" (a directory git refused to
    #: descend into — almost always a nested git repository; see
    #: `GitGateway.list_ignored_paths`'s docstring). Directories that are NOT
    #: a boundary never appear here at all: every plain file inside an
    #: ordinary directory is enumerated individually.
    kind: str
    content_sha256: str | None
    symlink_target: str | None
    executable: bool


#: path (repo-relative, exactly as git reported it) -> PathState. A path
#: absent from the snapshot means "did not exist as a file/symlink/boundary
#: at snapshot time" — plain Python dict identity/equality is all `diff`
#: needs, so this is intentionally not its own class.
CheckoutSnapshot = dict[str, PathState]


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def enumerate_checkout_paths(git: GitGateway) -> list[str]:
    """Every path git considers part of the checkout: tracked (regardless of
    working-tree state), untracked-and-not-ignored, and ignored — the three
    categories `git status` reports, enumerated literally (never collapsed to
    a containing directory) via `git ls-files`. Order is not meaningful;
    de-duplicated (a path cannot be in more than one category, but nothing
    here assumes that of git)."""
    paths: set[str] = set()
    paths.update(git.list_tracked_paths())
    paths.update(git.list_untracked_paths())
    paths.update(git.list_ignored_paths())
    return sorted(paths)


def snapshot_checkout(repo_root: Path, paths: list[str]) -> CheckoutSnapshot:
    """A `CheckoutSnapshot` of `paths` (repo-relative, as `enumerate_checkout_
    paths` returns) resolved against `repo_root`, read directly off the
    filesystem — never through git's object store — because content read via
    git would reflect the INDEX for tracked files, not necessarily the
    working tree (see `escape_detector` module docstring: a clean-checkout
    precondition makes those equal at the "before" snapshot, but the "after"
    snapshot's whole job is detecting exactly the case where they might no
    longer be).

    A path that no longer exists at snapshot time (a benign race between
    enumeration and reading, or a path that was already gone) is simply
    omitted — its absence is itself meaningful to `diff_snapshots` on the
    "before" side (never existed / already gone) and on the "after" side
    (deleted during the window)."""
    repo_root = Path(repo_root)
    snapshot: CheckoutSnapshot = {}
    for rel in paths:
        full = repo_root / rel
        try:
            st = os.lstat(full)
        except OSError:
            continue  # gone by the time we got here — treated as absent
        import stat as _stat

        if _stat.S_ISLNK(st.st_mode):
            try:
                target = os.readlink(full)
            except OSError:
                continue
            snapshot[rel] = PathState(
                kind="symlink", content_sha256=None, symlink_target=target, executable=False
            )
        elif _stat.S_ISREG(st.st_mode):
            try:
                digest = _hash_file(full)
            except OSError:
                continue
            executable = bool(st.st_mode & 0o111)
            snapshot[rel] = PathState(
                kind="file", content_sha256=digest, symlink_target=None, executable=executable
            )
        elif _stat.S_ISDIR(st.st_mode):
            # git ls-files reported a path it would not descend into — a
            # nested repository boundary (see `list_ignored_paths`). Recorded
            # as present/absent only; granular content changes inside it are
            # not visible to this snapshot (documented limitation, see the
            # module docstring).
            snapshot[rel] = PathState(
                kind="dir_boundary", content_sha256=None, symlink_target=None, executable=False
            )
        # Anything else (fifo, socket, device) is not a shape a normal write
        # produces here and is deliberately not modeled.
    return snapshot


def diff_snapshots(before: CheckoutSnapshot, after: CheckoutSnapshot) -> list[str]:
    """Human-readable violations: creation, deletion, content change,
    symlink-target change, or executable-bit change for any path. PATHS
    ONLY — content is never included, so this is safe to put in a blocker
    record or an operator-facing message even if the changed file held a
    secret."""
    violations: list[str] = []
    for path in sorted(set(before) | set(after)):
        prior = before.get(path)
        current = after.get(path)
        if prior is None and current is not None:
            violations.append(f"created outside the worker repo: {path} ({current.kind})")
        elif prior is not None and current is None:
            violations.append(f"deleted outside the worker repo: {path} (was {prior.kind})")
        elif prior is not None and current is not None:
            if prior.kind != current.kind:
                violations.append(
                    f"type changed outside the worker repo: {path} ({prior.kind} -> {current.kind})"
                )
            elif prior.kind == "file" and prior.content_sha256 != current.content_sha256:
                violations.append(f"content changed outside the worker repo: {path}")
            elif prior.kind == "symlink" and prior.symlink_target != current.symlink_target:
                violations.append(f"symlink target changed outside the worker repo: {path}")
            if prior.kind == current.kind and prior.executable != current.executable:
                violations.append(f"executable bit changed outside the worker repo: {path}")
    return violations


def find_symlink_traversal(repo_root: Path, paths: list[str]) -> list[str]:
    """Human-readable violations for any `paths` entry (repo-relative, from
    `Task.approved_paths`) that traverses a symlink ANYWHERE along its
    component path, including the leaf itself, that currently exists on
    disk under `repo_root`.

    This is a separate, filesystem-aware check from
    `tasks._validate_approved_path` (which only inspects the STRING and has
    no repo-root awareness by design — see that function's docstring): a
    string like `docs/SECURITY.md` looks like an ordinary safe relative
    path, but if `docs` (or `docs/SECURITY.md` itself) already exists on
    disk as a symlink, following it writes through to wherever the symlink
    actually points — which may be entirely outside the repository. A path
    component that does not exist yet (the common case for a brand-new
    approved path) is not a violation; there is nothing to traverse until
    something is there. Run at dispatch time (`orchestrator.py`), against
    the task's own worker repo — never assumed to have been checked once
    and cached, since the worker repo is freshly created per attempt."""
    root = Path(repo_root)
    violations: list[str] = []
    for rel in paths:
        current = root
        for part in Path(rel).parts:
            current = current / part
            if current.is_symlink():
                try:
                    target = os.readlink(current)
                except OSError:
                    target = "?"
                violations.append(
                    f"approved path {rel!r} traverses a symlink at "
                    f"{current.relative_to(root)} -> {target}"
                )
                break
    return violations
