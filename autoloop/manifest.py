"""Change manifests: what a commit approval is allowed to touch.

**NON-PRODUCTION (2026-07-30, docs/SECURITY.md S21 / S22).** Both manifest
kinds below, and every function in this module, are retired from the
production dispatch path: `orchestrator.py`'s `_dispatch_git` (the only
caller of `verify_commit` / `verify_tree_content` / `render_adoption_block`,
and of `GitGateway.commit()` / `commit_adopted()`) was removed when the
legacy authorize-then-produce commit path was replaced end to end by
produce-then-review (`worktask.py`, `packet.py`, `_dispatch_task_postcommit`
— audit included). `ChangeManifest.adopt` already had no production caller
before that. This module is kept, unmodified otherwise, because it is still
exercised directly by `test_manifest.py` (unit-level primitive tests, not
integration through the orchestrator) — do not wire it back into
`orchestrator.py` without a fresh design review.

Two kinds, one gate.

**`executor` (unchanged, the default).** Every executor run gets a content
snapshot of the dirty tree BEFORE the task and AFTER it. The difference — files
the task created, modified, or deleted — is the only thing a later commit
approval may reference. Pre-existing human work and files that changed outside
the task are refused. Snapshots hash content (not just porcelain status), so a
file already dirty before the task but further edited BY the task is correctly
task-modified, while an untouched pre-existing change is correctly excluded.

**`adopted`.** Work that already exists in the tree — made by a human, or by
the lead outside the loop — has no executor provenance, so the check above can
never pass. Adoption records an EXPLICIT path list with a SHA-256 per path and
commits only if every requested file still hashes to the approved value.

That is not a weakening. The executor kind uses *provenance* ("did the task
create this?") as a proxy for what actually matters, *reviewedness* ("was this
exact content approved?"). Adoption checks the real property directly, and more
strictly: an executor-owned path is committable today even if its content
changed after the review, whereas an adopted path is not.

The integrity chain, unbroken end to end:

    file content
      → manifest.adopted[path] = {sha256, mode} (recorded at adopt time; the
        mode is STATED by the caller, never taken from the working tree, because
        identical bytes can be committed 100644 or 100755)
      → `render_adoption_block(manifest)` embedded in the review payload, so
        report_sha256 = sha256(payload) provably covers the hash table
      → manifest.presented_report_sha256 is stamped ONLY for a report that
        actually carried the block (a payload without it binds nothing, and the
        commit gate then refuses any approval answering that report)
      → ChatGPT's `reviewed` stamp must echo that report_sha256 (verify_review)
      → at commit: manifest.presented_report_sha256 == the answered request's
        report_sha256, every requested path is adopted, and every file still
        hashes to the approved value
      → after staging, an IMMUTABLE TREE object is built and every entry is
        verified as a complete tuple (path, mode, object type = blob, content
        sha256) — not the working tree (defeated by swap-and-restore) and not the
        index (a pre-commit hook rewrites it after any check). The commit is
        created from that exact verified tree and the branch moves by
        compare-and-swap

Path handling across this boundary is NUL-delimited (`-z`). Without it git quotes
and escapes paths containing spaces, tabs or non-ASCII, so a pathname-keyed check
would compare the wrong string.

Guarantee boundary: these checks are process-local. Arbitrary concurrent mutation
of git configuration, hooks or the object database by an external hostile process
is outside what they can promise.

Hashes are deliberately NOT carried in the directive. A directive is
model-authored text; integrity values inside it could diverge from the reviewed
report, which would let an approval authorize content nobody reviewed.

Persistence mirrors state.py: one JSON file per manifest under
`.autoloop/manifests/`, atomic replace.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import ManifestViolation, StateCorruptError
from .state import utcnow_iso

DELETED = "DELETED"


def parse_porcelain_line(line: str) -> tuple[str, str] | None:
    """`git status --porcelain` line → (XY status, path). Rename lines yield
    the destination path (the source shows up as a separate deletion when the
    rename is unstaged, which is the only mode the loop stages in)."""
    if len(line) < 4:
        return None
    status, rest = line[:2], line[3:]
    path = rest.split(" -> ")[-1].strip()
    if not path:
        return None
    return status, path


def snapshot(git) -> dict[str, str]:
    """Content snapshot of every dirty path. Values: "U:<sha256>" for
    untracked files, "T:<sha256>" for tracked-but-modified files, DELETED for
    tracked files deleted from the worktree. The prefix lets `finish`
    distinguish a task-created file (untracked) from a previously-clean
    tracked file the task modified; comparisons use content only."""
    result: dict[str, str] = {}
    root = Path(git.repo_root)
    # NUL-delimited: `status --porcelain` without -z quotes paths containing
    # spaces or tabs, and every decision here is keyed on the pathname.
    for status, path in git.dirty_entries():
        if "D" in status:
            result[path] = DELETED
            continue
        file_path = root / path
        prefix = "U" if status == "??" else "T"
        try:
            digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
            result[path] = f"{prefix}:{digest}"
        except (FileNotFoundError, IsADirectoryError):
            result[path] = DELETED if not file_path.exists() else f"{prefix}:UNREADABLE"
    return result


def _content(value: str | None) -> str | None:
    if value is None or value == DELETED:
        return value
    return value.split(":", 1)[-1]


KIND_EXECUTOR = "executor"
KIND_ADOPTED = "adopted"

#: Adopted-entry schema. v1 bound path -> sha256 only, which left the git file
#: MODE unbound: identical bytes can be committed as 100644 or 100755, so an
#: unapproved executable bit slipped through. v2 binds (sha256, mode) and v1
#: manifests are refused rather than silently reinterpreted.
ADOPTED_SCHEMA = 2

#: Only ordinary blobs may be adopted. Symlinks (120000) and submodules
#: (160000) are refused; so is anything else git might introduce.
ALLOWED_BLOB_MODES = frozenset({"100644", "100755"})


@dataclass
class ChangeManifest:
    manifest_id: str
    task_id: str  # registry task id, "audit", or "adopted" for adoption
    base_head: str
    baseline: dict[str, str]  # dirty tree BEFORE the task ran
    started_at: str = field(default_factory=utcnow_iso)
    created: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    finished_at: str | None = None
    #: Discriminator. Defaults to the executor kind so every manifest written
    #: before adoption existed still loads and behaves identically.
    kind: str = KIND_EXECUTOR
    #: Adopted kind only: path -> {"sha256": ..., "mode": ...} (schema v2).
    adopted: dict[str, dict] = field(default_factory=dict)
    #: Adopted kind only: entry-schema version. 0 on an executor manifest or a
    #: pre-v2 adopted manifest, which is refused at verification time.
    adopted_schema: int = 0
    #: Adopted kind only: report_sha256 of the review request that presented
    #: this manifest's hash table. Set by the orchestrator, never by a model.
    presented_report_sha256: str | None = None

    @classmethod
    def begin(cls, manifest_id: str, task_id: str, git) -> "ChangeManifest":
        return cls(
            manifest_id=manifest_id,
            task_id=task_id,
            base_head=git.head_sha(),
            baseline=snapshot(git),
        )

    @classmethod
    def adopt(cls, manifest_id: str, entries: "Mapping[str, str]", git) -> "ChangeManifest":
        """Adopt an EXPLICIT mapping of path -> approved git mode.

        Both halves are stated by the caller. The path list is never inferred
        from the dirty tree, so an unrelated edit cannot be swept into a later
        commit; and the MODE is never inferred from the working tree, so an
        executable bit is authorized only when someone approved it. If the
        stated mode disagrees with what git would stage, adoption is refused
        here rather than producing a manifest that could never commit.

        Raises ManifestViolation on: an empty mapping, an unsupported mode, a
        path outside the repository, a symlink (or a path under one), a missing
        file, a directory, a clean path (nothing to commit), or a deletion (no
        content to hash).
        """
        if not entries:
            raise ManifestViolation("adoption requires an explicit, non-empty path list")
        if not isinstance(entries, Mapping):
            raise ManifestViolation(
                "adoption requires a mapping of path -> approved git mode "
                '(e.g. {"a.py": "100644"}), so the mode is stated, never inferred'
            )
        paths = list(entries)
        for path in paths:
            if not isinstance(path, str) or not path.strip():
                raise ManifestViolation(f"invalid adopted path: {path!r}")
            mode = entries[path]
            if mode not in ALLOWED_BLOB_MODES:
                raise ManifestViolation(
                    f"'{path}' declares mode {mode!r}; only {sorted(ALLOWED_BLOB_MODES)} "
                    "may be adopted (symlinks, submodules and trees are refused)"
                )

        root = Path(git.repo_root).resolve()
        dirty = snapshot(git)
        adopted: dict[str, str] = {}
        for path in paths:
            candidate = Path(path)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ManifestViolation(
                    f"adopted path must be repository-relative without '..': '{path}'"
                )
            resolved = (root / candidate).resolve()
            if resolved != root and root not in resolved.parents:
                raise ManifestViolation(f"adopted path escapes the repository: '{path}'")
            # Symlinks are refused: `snapshot()` reads through the link and
            # hashes the TARGET's content, while git stages the link itself
            # (mode 120000, blob = the target path string). The reviewed bytes
            # would not be the committed bytes. A symlinked parent directory
            # redirects the same way, so the whole chain is checked.
            unresolved = root / candidate
            if unresolved.is_symlink() or any(
                parent.is_symlink() for parent in unresolved.parents if root in parent.parents
            ):
                raise ManifestViolation(
                    f"'{path}' is a symlink or lies under one — git stages the link, "
                    "not the target it resolves to, so the reviewed bytes would differ "
                    "from the committed bytes"
                )
            state = dirty.get(path)
            if state is None:
                raise ManifestViolation(
                    f"'{path}' is not a pending change — only dirty files can be "
                    "adopted (nothing would be committed for a clean path)"
                )
            if state == DELETED:
                raise ManifestViolation(
                    f"'{path}' is deleted — deletions carry no content to bind to a "
                    "hash and are not supported by adoption"
                )
            if not resolved.is_file():
                raise ManifestViolation(f"'{path}' is not a regular file")
            digest = _content(state)
            if not digest or digest == "UNREADABLE":
                raise ManifestViolation(f"'{path}' could not be hashed")
            declared = entries[path]
            actual_mode = "100755" if os.access(resolved, os.X_OK) else "100644"
            if declared != actual_mode:
                raise ManifestViolation(
                    f"'{path}' is approved as mode {declared} but git would stage "
                    f"{actual_mode} — fix the file mode or the approval; the mode is "
                    "never taken from the working tree"
                )
            adopted[path] = {"sha256": digest, "mode": declared}

        return cls(
            manifest_id=manifest_id,
            task_id=KIND_ADOPTED,
            base_head=git.head_sha(),
            baseline={},
            kind=KIND_ADOPTED,
            adopted=adopted,
            adopted_schema=ADOPTED_SCHEMA,
            # Adoption is complete the moment it is recorded; there is no task
            # to wait for, so the "unfinished manifest" gate does not apply.
            finished_at=utcnow_iso(),
        )

    def is_adopted(self) -> bool:
        return self.kind == KIND_ADOPTED

    def finish(self, current: dict[str, str]) -> None:
        """Classify the task's changes by diffing the after-snapshot against
        the baseline."""
        created, modified, deleted = [], [], []
        for path, value in sorted(current.items()):
            before = self.baseline.get(path)
            if value == DELETED:
                if before != DELETED:
                    deleted.append(path)
            elif before is None:
                # Not dirty at baseline: an untracked file is task-created; a
                # tracked file that is now dirty was modified by the task.
                if value.startswith("U:"):
                    created.append(path)
                else:
                    modified.append(path)
            elif _content(before) != _content(value):
                modified.append(path)
            # same content → pre-existing change the task did not touch
        # A path dirty at baseline that is now clean was reverted/absorbed —
        # it is not a task change either.
        self.created = created
        self.modified = modified
        self.deleted = deleted
        self.finished_at = utcnow_iso()

    def task_changed_paths(self) -> set[str]:
        return set(self.created) | set(self.modified) | set(self.deleted)


def render_adoption_block(manifest: ChangeManifest) -> str:
    """The canonical, byte-stable adoption table for a review payload.

    The orchestrator refuses to send an adopted manifest's review request unless
    the payload contains this exact text, which is what makes `report_sha256`
    cover the hash table rather than merely coexist with it.
    """
    if not manifest.is_adopted():
        raise ManifestViolation(
            f"manifest {manifest.manifest_id} is not an adopted manifest"
        )
    lines = [
        f"ADOPTED-MANIFEST v{manifest.adopted_schema} {manifest.manifest_id} "
        f"base={manifest.base_head}"
    ]
    for path in sorted(manifest.adopted):
        entry = manifest.adopted[path]
        lines.append(f"  {entry['mode']}  {entry['sha256']}  {path}")
    return "\n".join(lines)


def verify_adopted_commit(
    manifest: ChangeManifest, approved_paths: tuple[str, ...], git
) -> list[str]:
    """Content gate for an adopted manifest: every requested path must be
    adopted AND still hash to the approved value. Returns violations."""
    violations: list[str] = []
    if manifest.presented_report_sha256 is None:
        violations.append(
            f"adopted manifest {manifest.manifest_id} was never presented for review "
            "— no report is bound to its hash table"
        )
        return violations
    schema_problems = check_adopted_schema(manifest)
    if schema_problems:
        return schema_problems
    root = Path(git.repo_root)
    for path in approved_paths:
        entry = manifest.adopted.get(path)
        if entry is None:
            violations.append(
                f"'{path}' is not in adopted manifest {manifest.manifest_id} "
                "(only explicitly adopted paths may be committed)"
            )
            continue
        expected = entry["sha256"]
        target = root / path
        try:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
        except (FileNotFoundError, IsADirectoryError):
            violations.append(
                f"'{path}' no longer exists as a readable file — the approved "
                "content is gone"
            )
            continue
        if actual != expected:
            # Digests are identifying, not sensitive; content is never quoted.
            violations.append(
                f"'{path}' changed after approval — approved sha256 "
                f"{expected[:12]}…, current {actual[:12]}…"
            )
    return violations


SYMLINK_MODE = "120000"
GITLINK_MODE = "160000"


def check_adopted_schema(manifest: ChangeManifest) -> list[str]:
    """Refuse adopted manifests that predate mode binding, or are malformed.

    A v1 manifest bound only content, so reinterpreting it under v2 rules would
    silently invent a mode nobody approved. Such manifests are rejected, not
    upgraded.
    """
    if manifest.adopted_schema != ADOPTED_SCHEMA:
        return [
            f"adopted manifest {manifest.manifest_id} has entry schema "
            f"v{manifest.adopted_schema}, expected v{ADOPTED_SCHEMA} — a manifest "
            "written before file modes were bound cannot be reinterpreted; re-adopt "
            "the paths with explicit modes"
        ]
    violations = []
    for path, entry in manifest.adopted.items():
        if not isinstance(entry, dict) or "sha256" not in entry or "mode" not in entry:
            violations.append(f"adopted entry for '{path}' is malformed: {entry!r}")
        elif entry["mode"] not in ALLOWED_BLOB_MODES:
            violations.append(
                f"adopted entry for '{path}' declares unsupported mode {entry['mode']!r}"
            )
    return violations


def verify_commit(
    manifest: ChangeManifest, approved_paths: tuple[str, ...], git=None
) -> list[str]:
    """Deterministic commit gate. Returns human-readable violations (empty =
    allowed).

    Adopted manifests are content-verified (`verify_adopted_commit`, needs
    `git` for the repo root). Executor manifests keep their original provenance
    rules verbatim:

    * approved paths must be non-empty (the contract already enforces this;
      re-checked here as defense in depth);
    * the manifest must be finished (the task actually ran);
    * every approved path must be a path the TASK created/modified/deleted —
      pre-existing dirty files and files never touched by the task are
      refused, which is what makes implicit `git add -A` impossible to
      reintroduce via approvals.
    """
    violations: list[str] = []
    if not approved_paths:
        violations.append("no approved paths — commits require an explicit path list")
        return violations
    if manifest.is_adopted():
        if git is None:
            return [
                f"adopted manifest {manifest.manifest_id} cannot be verified without "
                "repository access"
            ]
        return verify_adopted_commit(manifest, approved_paths, git)
    if manifest.finished_at is None:
        violations.append(
            f"manifest {manifest.manifest_id} is unfinished — the task did not "
            "complete, nothing is committable"
        )
        return violations
    task_changed = manifest.task_changed_paths()
    for path in approved_paths:
        if path not in task_changed:
            if path in manifest.baseline:
                violations.append(
                    f"'{path}' was already modified before the task started "
                    "(pre-existing change, not the task's work)"
                )
            else:
                violations.append(
                    f"'{path}' was not changed by task {manifest.task_id} "
                    "(nothing to commit for it)"
                )
    return violations


class ManifestStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)

    def _path(self, manifest_id: str) -> Path:
        return self.directory / f"{manifest_id}.json"

    def save(self, manifest: ChangeManifest) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(manifest.manifest_id)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(asdict(manifest), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)

    def load(self, manifest_id: str) -> ChangeManifest | None:
        path = self._path(manifest_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ChangeManifest(**data)
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise StateCorruptError(f"manifest {path} is unreadable: {exc}") from exc


def verify_tree_content(
    manifest: ChangeManifest,
    approved_paths: tuple[str, ...],
    git,
    tree: str,
    parent_tree: str,
) -> list[str]:
    """Verify an IMMUTABLE tree object against the approval.

    This is the authoritative content gate. A tree id names fixed bytes: unlike
    the index (which `git commit`'s hooks can rewrite) or the working tree
    (which anyone can edit), the object verified here is the object committed.

    Checks: the tree's changed-path set versus the parent tree is EXACTLY the
    approved set; every approved path's blob hashes to the adopted sha256; no
    approved path is a symlink or submodule entry.
    """
    violations: list[str] = []
    if not manifest.is_adopted():  # pragma: no cover - callers gate on kind
        return violations
    schema_problems = check_adopted_schema(manifest)
    if schema_problems:
        return schema_problems
    approved = {p for p in approved_paths}

    changed = git.changed_paths(parent_tree, tree)
    unapproved = sorted(changed - approved)
    if unapproved:
        violations.append(
            f"tree changes unapproved paths {unapproved} — the commit would carry "
            "content that was never reviewed"
        )
    absent = sorted(approved - changed)
    if absent:
        violations.append(
            f"tree does not change approved paths {absent} — the commit would not "
            "contain the reviewed work"
        )

    entries = git.tree_entries(tree)
    for path in sorted(approved):
        expected = manifest.adopted.get(path)
        if expected is None:
            violations.append(f"'{path}' is not in adopted manifest {manifest.manifest_id}")
            continue
        entry = entries.get(path)
        if entry is None:
            violations.append(f"'{path}' is absent from the candidate tree")
            continue
        mode, kind, oid = entry
        # The complete tuple is authorized: path, mode, object type, content.
        if kind != "blob":
            violations.append(
                f"'{path}' is a {kind} in the tree, not a blob — only ordinary files "
                "may be adopted"
            )
            continue
        if mode not in ALLOWED_BLOB_MODES:
            violations.append(
                f"'{path}' is mode {mode} in the tree (symlink, submodule or "
                "unsupported); only " + " / ".join(sorted(ALLOWED_BLOB_MODES)) +
                " may be committed"
            )
            continue
        if mode != expected["mode"]:
            violations.append(
                f"'{path}' is mode {mode} in the tree but was approved as "
                f"{expected['mode']} — an unapproved file-mode change (identical "
                "bytes can still flip the executable bit)"
            )
            continue
        actual = hashlib.sha256(git.blob_bytes(oid)).hexdigest()
        if actual != expected["sha256"]:
            violations.append(
                f"'{path}' tree content does not match the approved bytes — "
                f"approved sha256 {expected['sha256'][:12]}…, tree {actual[:12]}…"
            )
    return violations
