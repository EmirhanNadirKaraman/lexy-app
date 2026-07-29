"""Task-owned change manifests.

Autonomous commits must never sweep up the whole working tree. Every executor
run gets a manifest: a content snapshot of the dirty tree BEFORE the task and
AFTER it. The difference — files the task created, modified, or deleted — is
the only thing a later commit approval may reference. Everything else
(pre-existing human work, files that changed outside the task) is refused
deterministically by `verify_commit`.

Snapshots hash file content (not just porcelain status), so a file that was
already dirty before the task but was further edited BY the task is correctly
classified as task-modified, and an untouched pre-existing change is correctly
excluded.

Persistence mirrors state.py: one JSON file per manifest under
`.autoloop/manifests/`, atomic replace.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import StateCorruptError
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
    for line in git.dirty_files():
        parsed = parse_porcelain_line(line)
        if parsed is None:
            continue
        status, path = parsed
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


@dataclass
class ChangeManifest:
    manifest_id: str
    task_id: str  # registry task id, or "audit"
    base_head: str
    baseline: dict[str, str]  # dirty tree BEFORE the task ran
    started_at: str = field(default_factory=utcnow_iso)
    created: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    finished_at: str | None = None

    @classmethod
    def begin(cls, manifest_id: str, task_id: str, git) -> "ChangeManifest":
        return cls(
            manifest_id=manifest_id,
            task_id=task_id,
            base_head=git.head_sha(),
            baseline=snapshot(git),
        )

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


def verify_commit(manifest: ChangeManifest, approved_paths: tuple[str, ...]) -> list[str]:
    """Deterministic commit gate. Returns human-readable violations (empty =
    allowed). Rules:

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
