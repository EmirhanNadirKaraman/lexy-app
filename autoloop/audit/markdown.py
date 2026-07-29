"""Markdown-only write gate for the audit executor.

During an audit the executor may write Markdown and nothing else. The allowed
surface is a fixed allowlist of canonical documentation files plus AT MOST ONE
new consolidated dated report (docs/AUDIT_YYYY-MM-DD.md). Anything else —
production code, config, a second new file — is refused with AuditError before
any byte is written. Every write is recorded (and additionally lands in the
task-owned change manifest via the orchestrator's tree snapshots).

Phase 3 policy note: the executor itself only ever writes the dated report;
canonical-doc corrections are proposed as tasks instead of silently rewriting
history. The allowlist below is what a later phase may write WITH approval —
the gate is already enforced now so tests pin it.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..errors import AuditError

CANONICAL_MARKDOWN = (
    "CLAUDE.md",
    "docs/ROADMAP.md",
    "docs/SUMMARY.md",
    "docs/TESTS.md",
    "docs/SECURITY.md",
    "docs/INGESTION_PIPELINE.md",
    "docs/AUTOLOOP.md",
    "docs/TODO.md",
)

_NEW_REPORT = re.compile(r"^docs/AUDIT_\d{4}-\d{2}-\d{2}\.md$")


class MarkdownPolicy:
    def __init__(self, repo_root: Path):
        self._repo_root = Path(repo_root)
        self._new_reports_written = 0
        self.written: list[str] = []

    def check_writable(self, rel_path: str) -> None:
        path = rel_path.replace("\\", "/")
        if ".." in Path(path).parts or Path(path).is_absolute():
            raise AuditError(f"refusing path outside the repository: '{rel_path}'")
        if not path.endswith(".md"):
            raise AuditError(
                f"audit may update Markdown only — refusing to write '{rel_path}'"
            )
        if path in CANONICAL_MARKDOWN:
            return
        if _NEW_REPORT.match(path):
            if self._new_reports_written >= 1:
                raise AuditError(
                    "audit may create at most ONE new dated report per run — "
                    f"refusing second new file '{rel_path}'"
                )
            return
        raise AuditError(
            f"'{rel_path}' is not a canonical Markdown file nor the dated audit "
            f"report — allowed: {', '.join(CANONICAL_MARKDOWN)} or docs/AUDIT_YYYY-MM-DD.md"
        )

    def write(self, rel_path: str, content: str) -> Path:
        self.check_writable(rel_path)
        target = self._repo_root / rel_path
        is_new_report = _NEW_REPORT.match(rel_path.replace("\\", "/")) is not None
        target.write_text(content, encoding="utf-8")
        if is_new_report:
            self._new_reports_written += 1
        self.written.append(rel_path)
        return target
