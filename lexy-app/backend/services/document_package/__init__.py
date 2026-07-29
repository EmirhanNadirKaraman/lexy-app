"""Document-package import (roadmap **A2**).

Implements the app half of the ingestion boundary described in
``docs/INGESTION_PIPELINE.md``: the offline ``nlp-histo`` worker writes a
versioned package to disk, and this package validates and imports it.

**The package on disk is the entire interface.** Nothing here imports
``nlp_histo``, Docling, Torch or Transformers — that boundary is what keeps the
GPU dependencies out of the API process, and ``tests/test_document_package.py``
asserts it rather than trusting it.

Module map:

=================  ==========================================================
``contract``       Versions, enums, coordinate space. No logic.
``issues``         ``ValidationIssue`` + ``IssueCollector`` (fatal/warn/info).
``loader``         Discovery, loading, checksums, path containment.
``validators``     Individually testable structural validators.
``coordinates``    The single Docling↔fitz conversion point (§6).
``persistence``    ``PersistenceBackend`` seam + dry-run + the A3 placeholder.
``result``         ``import_result.json``.
=================  ==========================================================

Orchestration lives in ``services/book_import_service.py``, alongside
``book_service`` so it is discoverable by name.
"""
from .contract import (
    IMPORT_SCHEMA_VERSION,
    PACKAGE_SCHEMA_VERSION,
    STATUS_IMPORTED,
    STATUS_PARTIAL,
    STATUS_REJECTED,
)
from .issues import FATAL, INFO, WARNING, IssueCollector, ValidationIssue
from .loader import LoadedPackage, PackageError, discover_packages, load_package
from .persistence import (
    DryRunPersistence,
    PendingSchemaPersistence,
    PersistenceBackend,
    PersistenceNotAvailable,
    PersistOutcome,
    PlannedBlock,
    build_plan,
)
from .result import ImportResult, build_result

__all__ = [
    "DryRunPersistence",
    "FATAL",
    "IMPORT_SCHEMA_VERSION",
    "INFO",
    "ImportResult",
    "IssueCollector",
    "LoadedPackage",
    "PACKAGE_SCHEMA_VERSION",
    "PackageError",
    "PendingSchemaPersistence",
    "PersistOutcome",
    "PersistenceBackend",
    "PersistenceNotAvailable",
    "PlannedBlock",
    "STATUS_IMPORTED",
    "STATUS_PARTIAL",
    "STATUS_REJECTED",
    "ValidationIssue",
    "WARNING",
    "build_plan",
    "build_result",
    "discover_packages",
    "load_package",
]
