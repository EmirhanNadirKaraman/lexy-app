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

THE ONE EXEMPTION, AND WHY IT IS NOT A CRACK IN THE PARAGRAPH ABOVE.
`is_derived_bytecode` exempts CPython's own bytecode cache — a `.pyc`
sitting directly inside a `__pycache__/` directory whose sibling `.py` source
is a REGULAR FILE in the same snapshot side(s). That is a different class
from the volatile files the paragraph above refuses to exclude, and the
distinction is authorship: `state.json` and a blocker record are AUTHORED
(their bytes are the only copy of a claim, so tampering with them is
undetectable anywhere else), while a `__pycache__` entry is DERIVED — the
interpreter writes it from the `.py` next to it, and that `.py` stays fully
in scope, tracked byte for byte by this same snapshot. Nothing was excluded
because it is noisy; one class was excluded because its authoritative source
is still watched. "Still watched" is checked, not assumed: the source has to
be present as `kind="file"` (the only kind this snapshot hashes) on every
side the cache entry exists on, so a `.py` that is a symlink — watched as a
target string, with its bytes free to change underneath — does not qualify
its neighbours for the exemption.

Why it was needed: any out-of-band `import autoloop.<anything>` against the
primary checkout — a read-only dashboard restarting, a supervisor polling
`python3 -m autoloop health` — recompiles a stale `.pyc` mid-round, and
`__pycache__/` is gitignored, so `enumerate_checkout_paths` sees it and the
diff read as tampering. Three loop-fatal `checkout_escape_detected` parks on
2026-08-15/16, none caused by an agent, each costing a `reset --yes` and the
in-flight round (once, five tasks stranded `in_progress`). See
`docs/COMMON_ERRORS.md` and `docs/SECURITY.md` S24's 2026-08-16 addendum.

What the exemption deliberately does NOT cover, because each is a way
bytecode stops being merely derived:
  * a `.pyc` OUTSIDE `__pycache__` (the legacy, pre-PEP-3147 layout, e.g.
    `pkg/evil.pyc`). Those load with NO `.py` beside them, so a planted one
    is authored code with no source to compare it against.
  * a `.pyo` ANYWHERE, `__pycache__` included. No supported CPython emits
    that name — PEP 488 replaced it with the `.opt-N` infix of a `.pyc` — so
    `__pycache__/mod.cpython-312.pyo` is not a compile product; it is an
    authored file borrowing a derived-looking extension, and exempting it
    would grant a silent write beside every sourced module in the tree.
  * a `__pycache__` entry whose sibling source is missing, or is not a plain
    file, on any side the entry exists — an orphan nothing regenerates, or a
    "source" that is itself a symlink and so watched only as a target string
    while its bytes move freely. Both are the planted-code shape by another
    route. PEP 3147's cache layout is only ever consulted on behalf of the
    source next to it, so a normal package import will not load an orphan;
    that is a reason not to *trust* it, not a reason to stop reporting it.
  * any change of SHAPE at such a path — a symlink or a directory appearing
    where a cache file was, or vice versa. A compile never produces that.

Honest residual, recorded rather than argued away: "the next import
regenerates it" holds only while the cached header is STALE. An agent that
writes a `.pyc` can also write the `(mtime, size)` or PEP 552 source-hash
header that makes the interpreter accept it, so a forged cache entry whose
header matches its source would be loaded and would not be reported here.
Header verification is deliberately NOT attempted: it costs an attacker one
`os.stat` to defeat, while failing closed on an unreadable or foreign-magic
header would manufacture exactly the spurious loop-fatal parks this
exemption exists to stop. The residual is the same shape as, and strictly
smaller than, the `.git/hooks/` gap below — both need the OS-level sandbox
S24 tracks, not a wider snapshot.

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
import re
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


#: The directory CPython writes its bytecode cache into (PEP 3147). A cache
#: file must be DIRECTLY inside one of these to qualify for the exemption —
#: `pkg/evil.pyc`, the pre-PEP-3147 layout, is importable with no source
#: beside it and stays in scope (see the module docstring).
BYTECODE_CACHE_DIR = "__pycache__"

#: A cache file's name, as `importlib.util.cache_from_source` builds it:
#: `<stem>.<tag>[.opt-N].pyc`, e.g. `orchestrator.cpython-312.pyc` or
#: `orchestrator.cpython-312.opt-1.pyc`. The trailing `.<digits>` group is
#: CPython's atomic-write temp file (`_bootstrap_external._write_atomic`
#: writes `<name>.<pid>` and then `os.replace`s it into place) — a snapshot
#: that lands mid-write sees that name and nothing else, so it is the same
#: derived artifact under a transient name, not a separate path class.
#: `stem` is non-greedy so a source whose own name contains a dot
#: (`foo.bar.py` -> `foo.bar.cpython-312.pyc`) resolves back to `foo.bar`.
#:
#: `.pyc` ONLY, deliberately: no supported CPython writes a `.pyo` — PEP 488
#: folded the optimisation level into the `.opt-N` infix of a `.pyc` — so a
#: `__pycache__/mod.<tag>.pyo` is not a compile product at all. It is an
#: authored file wearing a derived-looking extension, and accepting it would
#: hand an attacker a silent write for every module in the tree that happens
#: to have a source beside it. It stays in scope and is reported.
_CACHE_FILE_RE = re.compile(r"^(?P<stem>.+?)\.[^.]+(?:\.opt-\d+)?\.pyc(?:\.\d+)?$")


def is_derived_bytecode(rel: str, *sides: CheckoutSnapshot) -> bool:
    """True when `rel` (repo-relative, as git reports it) is a CPython
    bytecode cache entry whose `.py` source is a REGULAR FILE in every one of
    `sides` — the one class of path `diff_snapshots` reports nothing for.

    `sides` are the snapshot sides on which the cache entry itself exists
    (before, after, or both — `diff_snapshots` passes exactly those). The
    source is checked per side rather than against the union of both key
    sets, because the whole justification for the exemption is that the
    authoritative source is watched BYTE FOR BYTE across the same window:
      * a source that exists only on one side (created or deleted mid-window)
        leaves the cache entry underived on the other, so the exemption does
        not apply there; the `.py`'s own creation/deletion is reported on its
        own path regardless.
      * `kind == "file"` is the byte-for-byte claim itself —
        `snapshot_checkout` only records `kind="file"` after hashing the
        contents. A `.py` that is a SYMLINK is watched only as a target
        string, so its bytes could change with nothing in the snapshot
        moving; a cache entry beside such a source is therefore NOT exempt.
        Same for a `dir_boundary` (a nested repo where the source should be).
    An orphan cache entry — no `.py` beside it, nothing that would ever
    regenerate it — fails this check too and is reported like any other path,
    which is what catches a cache file planted where no module exists.

    Called with no `sides` at all, this is False: a path nothing claims to
    have seen cannot have a verified source, and defaulting the other way
    would silently re-open everything above.

    Read the module docstring before widening this. In particular it must
    never grow to `.so`/`.pyd`: those are authored build outputs, and one
    dropped next to a module is a direct execution vector with no source in
    the checkout to compare it against.
    """
    if not sides:
        return False
    parts = rel.split("/")
    if len(parts) < 2 or parts[-2] != BYTECODE_CACHE_DIR:
        return False
    match = _CACHE_FILE_RE.match(parts[-1])
    if match is None:
        return False
    package_dir = parts[:-2]
    # `.py` only. `cache_from_source` maps a Windows-only `.pyw` onto the
    # same cache name, so such a module's cache is simply not exempt here —
    # a spurious park nobody on this platform can hit, versus guessing at
    # which of several sources a cache entry belongs to.
    source = "/".join([*package_dir, f"{match.group('stem')}.py"])
    for side in sides:
        state = side.get(source)
        if state is None or state.kind != "file":
            return False
    return True


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
    secret.

    Exactly one class of path is silent: a CPython bytecode cache entry
    (`is_derived_bytecode` — the module docstring argues why, and what stays
    in scope) that was a plain file on every side it existed, whose `.py`
    source is a plain file on those same sides. A cache entry that appears,
    changes, or vanishes as some import's side effect says nothing an agent
    could not equally say through the `.py` next to it, which this same diff
    still reports byte for byte.

    Known narrowness, stated rather than papered over: the exemption resolves
    a cache file back to its source by stripping ONE dotted tag group, so a
    cache name whose tag itself contains dots does not resolve and is
    reported. `pytest`'s assertion rewriter writes exactly such a name
    (`<mod>.<cache_tag>-pytest-<version>.pyc`, and a version like `8.3.4` has
    dots), so a validation run's rewritten TEST-module caches are still
    reported by `diff_worker_tree` even though the ordinary
    `<mod>.cpython-3XX.pyc` written for every non-test module it imports is
    not. Erring toward reporting is the safe direction here, and widening the
    stem resolution would mean guessing which of several prefixes a cache
    entry belongs to — deliberately not done. Validation is expected to run
    with `-B`/`PYTHONDONTWRITEBYTECODE` for that reason.
    """
    violations: list[str] = []
    known = set(before) | set(after)
    for path in sorted(known):
        prior = before.get(path)
        current = after.get(path)
        # The sides the cache entry EXISTS on are the sides its source has to
        # be watched on — passing the union of both key sets instead would
        # accept a source that is only half in scope (see
        # `is_derived_bytecode`).
        sides = [snap for snap, state in ((before, prior), (after, current)) if state is not None]
        if (
            is_derived_bytecode(path, *sides)
            and (prior is None or prior.kind == "file")
            and (current is None or current.kind == "file")
        ):
            # Created / rewritten / removed / re-moded by a compile. The mode
            # is checked in only as far as "still a plain file": CPython copies
            # a cache file's permission bits from its source
            # (`_bootstrap_external._calc_mode`), so an executable-bit change
            # here is an echo of one on the `.py` — which is reported on its
            # own path — and an exec bit means nothing to `import` anyway. A
            # symlink or directory appearing at this path is NOT a compile
            # product and falls through to the ordinary reporting below.
            continue
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


# ---- validation mutation guard ----------------------------------------------
#
# Validation is supposed to READ a tree and report on it. Nothing stops a test
# from writing, and the accepted v1 posture hands validation real (test-only)
# database credentials — so "a test wrote a credential into the tree" is a
# concrete way a secret leaves the process, whether by accident (a fixture
# dumping its config) or otherwise. The existing residual-dirty check in
# `_verify_committed` cannot see it: that runs BEFORE validation, and it is a
# `git status` check, so it is blind to ignored paths entirely.
#
# This compares the worker tree either side of the validation run. It reuses
# the same primitives as the checkout escape detector — content sha256,
# symlink target, executable bit, over tracked + untracked + ignored paths —
# and adds the index, because staging a change mutates `.git/index` without
# touching any working-tree file.


@dataclass(frozen=True)
class WorkerTreeState:
    files: CheckoutSnapshot
    #: sha256 of `.git/index` bytes. Compared literally: the brief asks for
    #: byte-for-byte, and a hash of the file is exactly that claim.
    index_sha256: str


def _index_path(repo_root: Path) -> Path:
    """`.git/index`, resolving the linked-worktree case where `.git` is a
    POINTER FILE rather than a directory (`WorktreeManager`'s repos) as well
    as the ordinary directory case (`WorkerRepoManager`'s `git init` repos)."""
    dot_git = Path(repo_root) / ".git"
    if dot_git.is_file():
        text = dot_git.read_text(encoding="utf-8", errors="replace").strip()
        if text.startswith("gitdir:"):
            target = Path(text[len("gitdir:"):].strip())
            if not target.is_absolute():
                target = Path(repo_root) / target
            return target / "index"
    return dot_git / "index"


def snapshot_worker_tree(git: GitGateway) -> WorkerTreeState:
    """Complete state of the worker tree, for comparison across a validation
    run.

    The index is hashed FIRST, before any git command this function issues:
    enumeration refreshes git's view and can rewrite `.git/index` as a side
    effect (the same trap that made a "read-only" dashboard write to the repo
    it observed — see `dashboard._run`). Hashing first means both snapshots
    measure the index as validation found it, not as this function left it.
    """
    index = _index_path(git.repo_root)
    index_sha = _hash_file(index) if index.is_file() else ""
    paths = enumerate_checkout_paths(git)
    return WorkerTreeState(
        files=snapshot_checkout(git.repo_root, paths), index_sha256=index_sha
    )


def diff_worker_tree(before: WorkerTreeState, after: WorkerTreeState) -> list[str]:
    """Human-readable mutations between two `snapshot_worker_tree` calls.

    PATHS ONLY, never contents — `diff_snapshots` guarantees that, and it is
    the property that makes this safe to put in a parked question or a blocker
    record when the thing validation wrote was a credential.
    """
    violations = list(diff_snapshots(before.files, after.files))
    if before.index_sha256 != after.index_sha256:
        violations.append(
            "git index changed during validation (a change was staged) — "
            "the working tree may look clean while the index does not"
        )
    return violations
