"""Package discovery, loading, checksum verification and path containment.

Gates 1–3 of the five in ``docs/INGESTION_PIPELINE.md`` §5. Everything here runs
before a single row is written, and none of it touches the database.

**Security posture.** A package may arrive from another machine, so every path
inside it is untrusted input. `resolve_within` is the one chokepoint: no file is
opened, hashed or recorded unless it resolves inside the package root. Precedent
is the S7 note in ``routers/content_requests.py`` — validate at the boundary,
before anything reaches the filesystem.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import contract
from .issues import FATAL, INFO, WARNING, IssueCollector

PACKAGE_ROOT_ENV = "PACKAGE_ROOT"
DEFAULT_PACKAGE_ROOT = Path(os.getenv("UPLOAD_ROOT", "uploads")) / "packages"

_CHUNK = 1024 * 1024


class PackageError(Exception):
    """Raised only when a package cannot be loaded far enough to be validated.

    Anything that *can* be expressed as a collected issue is, so the caller
    sees every defect at once. This is reserved for "there is no readable
    manifest", where continuing would produce noise rather than information.
    """


@dataclass
class LoadedPackage:
    """A package read off disk, not yet structurally validated."""

    package_root: Path
    document_id: str
    manifest: dict
    document: dict
    files: dict[str, str] = field(default_factory=dict)
    """Relative path → sha256, from ``CHECKSUMS.txt``. Empty when absent."""
    has_checksums: bool = False
    """Whether ``CHECKSUMS.txt`` exists at all.

    Distinct from ``not files``: a present-but-entirely-malformed file must be
    reported differently from an absent one.
    """
    checksum_errors: list[dict] = field(default_factory=list)
    """Parse defects, surfaced by :func:`verify_checksums` as fatal issues."""

    @property
    def elements(self) -> list[dict]:
        elements = self.document.get("elements")
        return elements if isinstance(elements, list) else []

    @property
    def page_dims(self) -> dict:
        dims = self.manifest.get("page_dims")
        return dims if isinstance(dims, dict) else {}

    @property
    def source_sha256(self) -> str | None:
        source = self.manifest.get("source")
        return source.get("sha256") if isinstance(source, dict) else None

    @property
    def profile(self) -> str | None:
        extractor = self.manifest.get("extractor")
        return extractor.get("profile") if isinstance(extractor, dict) else None

    @property
    def declared_warnings(self) -> list[dict]:
        warnings = self.manifest.get("warnings")
        return [w for w in warnings if isinstance(w, dict)] if isinstance(warnings, list) else []


# ---------------------------------------------------------------------------
# path containment (gate 3)
# ---------------------------------------------------------------------------


def package_root() -> Path:
    """Root under which every package must live.

    The API never accepts an absolute path from a client; it takes a package
    *name* and joins it here. That is what makes containment meaningful rather
    than advisory.
    """
    configured = os.getenv(PACKAGE_ROOT_ENV)
    return Path(configured) if configured else DEFAULT_PACKAGE_ROOT


def resolve_within(root: Path, relative: str) -> Path:
    """Resolve *relative* under *root*, refusing anything that escapes.

    Blocks the whole traversal family: ``../`` segments, absolute paths,
    and symlinks pointing outside (``resolve()`` follows links before the
    containment check, so a symlinked escape is caught too).

    Raises :class:`PackageError` rather than returning a sentinel — a caller
    that forgets to check a sentinel would open the file anyway.
    """
    if not isinstance(relative, str) or not relative.strip():
        raise PackageError("empty path in package")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise PackageError(f"absolute path not allowed in package: {relative!r}")

    root_resolved = root.resolve()
    target = (root_resolved / candidate).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise PackageError(f"path escapes package root: {relative!r}")
    return target


def is_contained(root: Path, relative: str) -> bool:
    try:
        resolve_within(root, relative)
        return True
    except PackageError:
        return False


# ---------------------------------------------------------------------------
# discovery + loading (gate: readability)
# ---------------------------------------------------------------------------


def discover_packages(root: Path | None = None) -> list[str]:
    """Package names under *root* that at least have a manifest."""
    base = (root or package_root())
    if not base.is_dir():
        return []
    return sorted(
        p.name for p in base.iterdir()
        if p.is_dir() and (p / contract.MANIFEST_FILENAME).is_file()
    )


def _read_json(path: Path, label: str) -> dict:
    if not path.is_file():
        raise PackageError(f"missing {label}: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PackageError(f"malformed {label}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise PackageError(f"{label} is not valid UTF-8: {exc}") from exc
    if not isinstance(payload, dict):
        raise PackageError(f"{label} must be a JSON object, got {type(payload).__name__}")
    return payload


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


@dataclass
class ParsedChecksums:
    """Result of parsing ``CHECKSUMS.txt`` — entries plus every defect found."""

    entries: dict[str, str] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)

    def error(self, code: str, message: str, **detail) -> None:
        self.errors.append({"code": code, "message": message, **detail})


def parse_checksums(text: str) -> ParsedChecksums:
    """Parse ``CHECKSUMS.txt`` — ``<sha256>  <relative path>`` per line.

    Blank lines and ``#`` comments are ignored. **Everything else is strict**,
    and each defect is collected rather than skipped:

    * a line that is not ``<digest> <path>``          → ``malformed_checksum_line``
    * a digest that is not 64 hex characters (sha256) → ``invalid_checksum_digest``
    * the same path listed twice                      → ``duplicate_checksum_entry``

    Duplicates matter more than they look: the previous implementation assigned
    into a dict, so a second entry for ``document.json`` silently replaced the
    first. A package could then carry one digest that verifies and one that does
    not, and the file would still pass. Now the ambiguity is reported and fatal.

    Only sha256 is supported, because that is the only algorithm the contract
    documents. An unfamiliar digest length is rejected rather than guessed at.
    """
    parsed = ParsedChecksums()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        parts = line.split(None, 1)
        if len(parts) != 2:
            parsed.error(
                "malformed_checksum_line",
                f"CHECKSUMS.txt line {lineno} is not '<sha256>  <path>'",
                line=lineno,
            )
            continue

        digest, relative = parts[0].strip(), parts[1].strip()
        if not relative:
            parsed.error(
                "malformed_checksum_line",
                f"CHECKSUMS.txt line {lineno} has no path",
                line=lineno,
            )
            continue
        if not _SHA256_RE.match(digest):
            parsed.error(
                "invalid_checksum_digest",
                f"CHECKSUMS.txt line {lineno} has digest {digest!r}; expected "
                "64 hex characters (sha256)",
                line=lineno,
                path=relative,
            )
            continue
        if relative in parsed.entries:
            parsed.error(
                "duplicate_checksum_entry",
                f"CHECKSUMS.txt lists {relative!r} more than once; the correct "
                "digest is ambiguous",
                line=lineno,
                path=relative,
            )
            continue
        parsed.entries[relative] = digest.lower()
    return parsed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_package(name_or_path: str, root: Path | None = None) -> LoadedPackage:
    """Load a package by name, relative to :func:`package_root`.

    Containment is enforced on the package name itself — ``load_package("../etc")``
    is refused before any file is opened.
    """
    base = (root or package_root())
    package_dir = resolve_within(base, name_or_path)
    if not package_dir.is_dir():
        raise PackageError(f"no such package: {name_or_path!r}")

    manifest = _read_json(package_dir / contract.MANIFEST_FILENAME, "manifest.json")
    document = _read_json(package_dir / contract.DOCUMENT_FILENAME, "document.json")

    checksums_path = package_dir / contract.CHECKSUMS_FILENAME
    has_checksums = checksums_path.is_file()
    parsed = ParsedChecksums()
    if has_checksums:
        parsed = parse_checksums(
            checksums_path.read_text(encoding="utf-8", errors="replace")
        )

    document_id = str(manifest.get("document_id") or package_dir.name)
    return LoadedPackage(
        package_root=package_dir,
        document_id=document_id,
        manifest=manifest,
        document=document,
        files=parsed.entries,
        has_checksums=has_checksums,
        checksum_errors=parsed.errors,
    )


# ---------------------------------------------------------------------------
# gate 1 — schema version
# ---------------------------------------------------------------------------


def validate_schema_version(pkg: LoadedPackage, issues: IssueCollector) -> None:
    version = pkg.manifest.get("package_schema_version")
    if version is None:
        issues.add(
            "missing_schema_version",
            "manifest has no package_schema_version",
            stage="schema",
        )
        return
    if not contract.is_compatible_schema(str(version)):
        issues.add(
            "incompatible_schema_version",
            f"package schema {version!r} is not compatible with "
            f"{contract.PACKAGE_SCHEMA_VERSION!r} (major version must match)",
            stage="schema",
            found=str(version),
            expected=contract.PACKAGE_SCHEMA_VERSION,
        )
        return

    for key in contract.REQUIRED_MANIFEST_KEYS:
        if key not in pkg.manifest:
            issues.add(
                "missing_manifest_key",
                f"manifest is missing required key {key!r}",
                stage="schema",
                key=key,
            )
    source = pkg.manifest.get("source")
    if not isinstance(source, dict):
        issues.add("malformed_source", "manifest.source must be an object", stage="schema")
    else:
        for key in contract.REQUIRED_SOURCE_KEYS:
            if not source.get(key):
                issues.add(
                    "missing_source_key",
                    f"manifest.source is missing {key!r}",
                    stage="schema",
                    key=key,
                )
    for key in contract.REQUIRED_DOCUMENT_KEYS:
        if key not in pkg.document:
            issues.add(
                "missing_document_key",
                f"document.json is missing required key {key!r}",
                stage="schema",
                key=key,
            )
    if not isinstance(pkg.document.get("elements", []), list):
        issues.add(
            "malformed_elements", "document.elements must be an array", stage="schema"
        )


# ---------------------------------------------------------------------------
# gate 2 — checksums
# ---------------------------------------------------------------------------


def verify_checksums(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Verify ``CHECKSUMS.txt``. Every failure here is **fatal**.

    ``CHECKSUMS.txt`` is a required member of the package layout (§4) and
    checksum verification is a mandatory gate (§5), so an absent file means the
    package is *incomplete* — not merely unverified. It does not proceed to
    planning or persistence.

    This is a deliberate tightening (2026-07-29). The previous behaviour warned
    and continued, on the reasoning that an early worker version shouldn't be
    blocked for no safety gain. That was the wrong trade: it made the one gate
    that establishes the bytes are what the worker wrote optional in practice,
    and "unverified" silently became the common case. Compatibility with an
    older producer belongs in the **schema version**, where it is explicit and
    negotiable — not in a validator that quietly accepts less.

    Failure modes, all fatal:

    * no ``CHECKSUMS.txt``                    → ``missing_checksums``
    * parse defects (malformed/dupe/digest)   → codes from :func:`parse_checksums`
    * a required package file not covered     → ``unchecksummed_required_file``
    * an entry naming a path outside the root → ``checksum_path_escape``
    * an entry naming an absent file          → ``checksum_missing_file``
    * a digest that does not match            → ``checksum_mismatch``
    """
    if not pkg.has_checksums:
        issues.add(
            "missing_checksums",
            f"package has no {contract.CHECKSUMS_FILENAME}; it is a required "
            "member of the package layout and cannot be skipped",
            stage="checksums",
        )
        return

    for error in pkg.checksum_errors:
        detail = {k: v for k, v in error.items() if k not in ("code", "message")}
        issues.add(error["code"], error["message"], stage="checksums", **detail)

    if not pkg.files:
        issues.add(
            "empty_checksums",
            f"{contract.CHECKSUMS_FILENAME} contains no usable entries",
            stage="checksums",
        )
        return

    # The two files that define the package must themselves be covered, or the
    # inventory proves nothing about the parts that matter most.
    for required in contract.REQUIRED_CHECKSUM_FILES:
        if required not in pkg.files:
            issues.add(
                "unchecksummed_required_file",
                f"{contract.CHECKSUMS_FILENAME} does not cover {required!r}, "
                "so its integrity is unverified",
                stage="checksums",
                path=required,
            )

    for relative, expected in sorted(pkg.files.items()):
        try:
            target = resolve_within(pkg.package_root, relative)
        except PackageError as exc:
            issues.add(
                "checksum_path_escape",
                f"CHECKSUMS.txt references a path outside the package: {exc}",
                stage="checksums",
                path=relative,
            )
            continue
        if not target.is_file():
            issues.add(
                "checksum_missing_file",
                f"CHECKSUMS.txt lists {relative!r} but it is not present",
                stage="checksums",
                path=relative,
            )
            continue
        actual = sha256_file(target)
        if actual != expected:
            issues.add(
                "checksum_mismatch",
                f"checksum mismatch for {relative!r}",
                stage="checksums",
                path=relative,
                expected=expected,
                actual=actual,
            )

    issues.add(
        "checksums_verified",
        f"verified {len(pkg.files)} file(s) against CHECKSUMS.txt",
        severity=INFO,
        stage="checksums",
    )


# ---------------------------------------------------------------------------
# gate 3 — path containment
# ---------------------------------------------------------------------------


def _referenced_paths(pkg: LoadedPackage) -> list[str]:
    """Every path the package asks the app to open."""
    paths: list[str] = list(pkg.files.keys())

    images = pkg.manifest.get("page_images")
    if isinstance(images, dict):
        template = images.get("path_template")
        if isinstance(template, str) and template:
            # Render the template for each declared page so traversal hidden in
            # the template itself ("../{page_index}.png") is caught.
            for page in pkg.page_dims:
                try:
                    paths.append(template.format(page_index=int(page)))
                except (ValueError, KeyError, IndexError):
                    paths.append(template)
                    break

    for element in pkg.elements:
        if not isinstance(element, dict):
            continue
        for key in ("image_path", "asset_path"):
            value = element.get(key)
            if isinstance(value, str) and value:
                paths.append(value)
    return paths


def validate_containment(pkg: LoadedPackage, issues: IssueCollector) -> None:
    for relative in _referenced_paths(pkg):
        try:
            resolve_within(pkg.package_root, relative)
        except PackageError as exc:
            issues.add(
                "path_escape",
                f"package path is not contained: {exc}",
                stage="containment",
                path=relative,
            )


# ---------------------------------------------------------------------------
# gate 5 — profile
# ---------------------------------------------------------------------------


def validate_profile(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Refuse a package produced by a profile that deletes German prose.

    Fatal by design: the biomedical profile's ``is_relevant_para`` removes
    30.5% of German prose (§2), and once the worker has dropped it the content
    is not in the package to recover.
    """
    profile = pkg.profile
    if not profile:
        issues.add(
            "missing_profile",
            "manifest.extractor.profile is absent; cannot confirm the extractor "
            "did not run content-deleting filters",
            stage="profile",
        )
        return
    if profile not in contract.ACCEPTED_PROFILES:
        issues.add(
            "unsupported_profile",
            f"extractor profile {profile!r} is not accepted "
            f"(expected one of {sorted(contract.ACCEPTED_PROFILES)})",
            stage="profile",
            profile=profile,
        )


def promote_declared_warnings(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Turn worker-declared warnings into importer warnings.

    They do not block the import; they exist so an unresolved extraction
    problem becomes review work instead of dying in the worker's log.
    """
    for warning in pkg.declared_warnings:
        code = str(warning.get("code") or "worker_warning")
        page = warning.get("page_index")
        issues.add(
            code,
            str(warning.get("detail") or f"worker reported {code}"),
            severity=WARNING,
            stage="worker",
            page_index=page if isinstance(page, int) else None,
        )


__all__ = [
    "FATAL",
    "LoadedPackage",
    "PACKAGE_ROOT_ENV",
    "PackageError",
    "discover_packages",
    "is_contained",
    "load_package",
    "package_root",
    "parse_checksums",
    "promote_declared_warnings",
    "resolve_within",
    "sha256_file",
    "validate_containment",
    "validate_profile",
    "validate_schema_version",
    "verify_checksums",
]
