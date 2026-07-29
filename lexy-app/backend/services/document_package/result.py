"""The ``import_result.json`` payload (``docs/INGESTION_PIPELINE.md`` §4).

The worker↔app feedback contract: what imported, what was skipped and why,
which warnings became review work, and which gate failed. It is what makes a
re-run diffable, and it is the record that makes a false deletion auditable.

All three statuses are modelled — ``imported``, ``rejected``,
``partial_rejected`` — even though only the first two are reachable today.
``partial_rejected`` becomes reachable when an import can legitimately keep a
subset of pages; wiring the vocabulary now means that change is additive.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import contract
from .issues import IssueCollector
from .persistence import PersistOutcome

_GATES = ("schema", "checksums", "containment", "structural", "profile")


@dataclass
class ImportResult:
    document_id: str
    status: str
    source_sha256: str | None = None
    doc_id: str | None = None
    dry_run: bool = True
    imported_at: str = ""
    package_schema_version: str | None = None
    counts: dict = field(default_factory=dict)
    validation: dict = field(default_factory=dict)
    skipped: list[dict] = field(default_factory=list)
    warnings_promoted: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    info: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status != contract.STATUS_REJECTED

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["import_schema_version"] = contract.IMPORT_SCHEMA_VERSION
        return payload

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=False) + "\n"

    def write(self, directory: Path) -> Path:
        """Write ``import_result.json`` beside the package.

        Best-effort: a read-only package directory must not turn a successful
        import into a failure. The caller already holds the same object.
        """
        path = Path(directory) / contract.IMPORT_RESULT_FILENAME
        try:
            path.write_text(self.to_json(), encoding="utf-8")
        except OSError:
            return path
        return path

    def summary_line(self) -> str:
        counts = self.counts
        return (
            f"{self.document_id}: {self.status} "
            f"({'dry-run' if self.dry_run else 'persisted'}) — "
            f"{counts.get('blocks_planned', 0)} block(s) from "
            f"{counts.get('elements_in', 0)} element(s), "
            f"{len(self.errors)} error(s), {len(self.warnings_promoted)} warning(s)"
        )


def build_result(
    *,
    document_id: str,
    issues: IssueCollector,
    dry_run: bool,
    source_sha256: str | None = None,
    package_schema_version: str | None = None,
    elements_in: int = 0,
    plan_size: int = 0,
    skipped: list[dict] | None = None,
    outcome: PersistOutcome | None = None,
    status_override: str | None = None,
) -> ImportResult:
    """Assemble the result from the collected issues and the persistence outcome."""
    if status_override:
        status = status_override
    elif not issues.ok:
        status = contract.STATUS_REJECTED
    else:
        status = contract.STATUS_IMPORTED

    validation = {gate: issues.stage_status(gate) for gate in _GATES}
    if status == contract.STATUS_REJECTED:
        validation["outcome"] = "rejected"

    counts = {
        "elements_in": elements_in,
        "blocks_planned": plan_size,
        "elements_skipped": len(skipped or []),
        "pages_written": outcome.pages_written if outcome else 0,
        "blocks_written": outcome.blocks_written if outcome else 0,
        # Sentences are A5's output; reported as 0 rather than omitted so the
        # field exists in the contract from the start.
        "sentences": 0,
    }

    return ImportResult(
        document_id=document_id,
        status=status,
        source_sha256=source_sha256,
        doc_id=outcome.doc_id if outcome else None,
        dry_run=dry_run,
        imported_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        package_schema_version=package_schema_version,
        counts=counts,
        validation=validation,
        skipped=list(skipped or []),
        warnings_promoted=[
            {**issue.as_dict(), "action": "queued_for_review"}
            for issue in issues.warnings
        ],
        errors=[issue.as_dict() for issue in issues.fatal],
        info=[issue.as_dict() for issue in issues.infos],
        notes=list(outcome.notes) if outcome else [],
    )
