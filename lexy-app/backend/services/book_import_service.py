"""Document-package import orchestration (roadmap **A2**).

``verify → validate → dry-run → persist``, in that order, with **no database
mutation before every gate has passed**. That ordering is the safety property:
a package that fails validation must never leave half-written rows behind.

Scope, deliberately narrow. This service does *not* do sentence reconstruction
(A4), segmentation (A5), AI review (A8), worker invocation (A10), or any LLM
call. It turns a package on disk into validated, planned rows and a report.

Nothing here imports ``nlp_histo``, Docling, Torch or Transformers.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .document_package import contract
from .document_package.issues import IssueCollector
from .document_package.loader import (
    LoadedPackage,
    PackageError,
    discover_packages,
    load_package,
    package_root,
    promote_declared_warnings,
    validate_containment,
    validate_profile,
    validate_schema_version,
    verify_checksums,
)
from .document_package.persistence import (
    DryRunPersistence,
    PendingSchemaPersistence,
    PersistenceBackend,
    PersistenceNotAvailable,
    build_plan,
)
from .document_package.result import ImportResult, build_result
from .document_package.validators import validate_structure

logger = logging.getLogger(__name__)

INTEGRITY_FAILURE_CODES = frozenset(
    {
        "checksum_mismatch",
        "checksum_missing_file",
        "checksum_path_escape",
        "malformed_checksum_line",
        "invalid_checksum_digest",
        "duplicate_checksum_entry",
        "empty_checksums",
        "unchecksummed_required_file",
    }
)
"""Checksum failures that make the package's *contents* untrustworthy.

When one of these fires, structural validation is skipped: its findings would
describe bytes that are not what the worker wrote, which sends someone chasing
a bug that does not exist. ``missing_checksums`` is deliberately absent — an
absent inventory is a fatal *completeness* failure, but the content itself
still parsed and its structural errors are genuine.
"""


def list_packages(root: Path | None = None) -> list[str]:
    """Package names available under ``PACKAGE_ROOT``."""
    return discover_packages(root)


def validate_package(pkg: LoadedPackage) -> IssueCollector:
    """Run all five gates in the documented order, collecting every finding.

    Order matters and is not cosmetic (§5):

    1. **schema** — an incompatible package is not worth structurally checking;
       its fields may mean something else entirely.
    2. **checksums** — establish the bytes are what the worker wrote before
       reasoning about their contents.
    3. **containment** — before any referenced path could be opened.
    4. **structural** — ids, pages, parents, reading order, geometry.
    5. **profile** — refuse an extractor profile that deletes German prose.

    Gates do **not** short-circuit on the first fatal, because independent
    defects are worth reporting together. The one exception is an incompatible
    schema version: continuing past it would produce a wall of misleading
    structural errors about fields that legitimately changed shape.
    """
    issues = IssueCollector()

    validate_schema_version(pkg, issues)
    if "incompatible_schema_version" in issues.codes():
        return issues

    verify_checksums(pkg, issues)
    # Containment always runs: it is a security gate about *declared paths*, not
    # about content integrity, and it is exactly when the bytes are suspect that
    # you want it.
    validate_containment(pkg, issues)

    if issues.codes() & INTEGRITY_FAILURE_CODES:
        # The bytes are not what the worker hashed, so structural findings would
        # describe corrupted content and send someone chasing the wrong bug.
        # A *missing* CHECKSUMS.txt is deliberately not in this set: the content
        # still parsed and is internally consistent, so its structural errors
        # are real and worth reporting alongside the missing-inventory failure.
        promote_declared_warnings(pkg, issues)
        return issues

    validate_structure(pkg, issues)
    validate_profile(pkg, issues)
    promote_declared_warnings(pkg, issues)
    return issues


async def import_package(
    package_name: str,
    *,
    user_id: str,
    dry_run: bool = True,
    root: Path | None = None,
    backend: PersistenceBackend | None = None,
    write_result: bool = True,
) -> ImportResult:
    """Import one package.

    Defaults to ``dry_run=True``. That is not timidity — until **A3** adds
    migration 038, ``book_blocks`` has no ``reading_order`` column and its
    ``block_type`` is only ever ``'text'``, so a "real" import would silently
    discard the two things the package exists to carry. Refusing is the correct
    behaviour; :class:`PendingSchemaPersistence` says so explicitly.

    A load failure still returns a well-formed :class:`ImportResult` rather than
    raising, so a caller always has something to report and to persist.
    """
    try:
        pkg = load_package(package_name, root)
    except PackageError as exc:
        issues = IssueCollector()
        issues.add(
            "package_unreadable",
            str(exc),
            stage="schema",
            package=str(package_name),
        )
        return build_result(
            document_id=str(package_name),
            issues=issues,
            dry_run=dry_run,
        )

    issues = validate_package(pkg)
    plan, skipped = build_plan(pkg)

    if not issues.ok:
        # Rejected: report everything, touch nothing.
        result = build_result(
            document_id=pkg.document_id,
            issues=issues,
            dry_run=dry_run,
            source_sha256=pkg.source_sha256,
            package_schema_version=str(pkg.manifest.get("package_schema_version") or ""),
            elements_in=len(pkg.elements),
            plan_size=len(plan),
            skipped=skipped,
        )
        logger.info("package import rejected: %s", result.summary_line())
        if write_result:
            result.write(pkg.package_root)
        return result

    if backend is None:
        backend = DryRunPersistence() if dry_run else PendingSchemaPersistence()

    try:
        outcome = await backend.persist(pkg, plan, user_id=user_id)
    except PersistenceNotAvailable as exc:
        issues.add(
            "persistence_unavailable",
            str(exc),
            stage="structural",
        )
        result = build_result(
            document_id=pkg.document_id,
            issues=issues,
            dry_run=dry_run,
            source_sha256=pkg.source_sha256,
            package_schema_version=str(pkg.manifest.get("package_schema_version") or ""),
            elements_in=len(pkg.elements),
            plan_size=len(plan),
            skipped=skipped,
        )
        if write_result:
            result.write(pkg.package_root)
        return result

    result = build_result(
        document_id=pkg.document_id,
        issues=issues,
        dry_run=outcome.dry_run,
        source_sha256=pkg.source_sha256,
        package_schema_version=str(pkg.manifest.get("package_schema_version") or ""),
        elements_in=len(pkg.elements),
        plan_size=len(plan),
        skipped=skipped,
        outcome=outcome,
    )
    logger.info("package import complete: %s", result.summary_line())
    if write_result:
        result.write(pkg.package_root)
    return result


async def dry_run_package(
    package_name: str,
    *,
    user_id: str,
    root: Path | None = None,
) -> ImportResult:
    """Validate and plan without writing anything.

    Performs *every* step a real import would, including the full plan build, so
    the report is genuinely predictive rather than a partial rehearsal.
    """
    return await import_package(
        package_name, user_id=user_id, dry_run=True, root=root, write_result=False
    )


__all__ = [
    "contract",
    "dry_run_package",
    "import_package",
    "list_packages",
    "package_root",
    "validate_package",
]
