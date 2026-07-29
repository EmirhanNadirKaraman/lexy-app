"""Gate 4 — structural validation.

Each validator is a standalone function taking ``(pkg, issues)`` and returning
nothing, so it can be tested in isolation against a two-element package rather
than only through a full import. They deliberately do not raise: one malformed
element must not hide the twelve after it.

Severity follows the preservation principle (``docs/INGESTION_PIPELINE.md``
§2b). Anything that would make the app **misplace or lose** content is fatal
(duplicate ids, bad coordinate space, bboxes off the page). Anything that is
merely *unproven* — an unknown element type, a missing confidence object — is a
warning, because dropping the element would be the irreversible choice and the
downstream reviewer can still resolve it.
"""
from __future__ import annotations

from . import contract
from .issues import WARNING, IssueCollector
from .loader import LoadedPackage

_BBOX_KEYS = ("x1", "y1", "x2", "y2")
_BBOX_TOLERANCE_PT = 1.0
"""Slack when checking a bbox against page dimensions.

Rounding in the extractor puts edges a fraction of a point outside the media
box routinely; flagging those would bury the genuine cases in noise.
"""


def validate_element_ids(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Ids must be present, string, and unique — they are the join key."""
    seen: set[str] = set()
    for index, element in enumerate(pkg.elements):
        if not isinstance(element, dict):
            issues.add(
                "malformed_element",
                f"element at index {index} is not an object",
                stage="structural",
                index=index,
            )
            continue
        element_id = element.get("id")
        if not isinstance(element_id, str) or not element_id.strip():
            issues.add(
                "missing_element_id",
                f"element at index {index} has no usable id",
                stage="structural",
                index=index,
            )
            continue
        if element_id in seen:
            issues.add(
                "duplicate_element_id",
                f"element id {element_id!r} appears more than once",
                stage="structural",
                element_id=element_id,
            )
        seen.add(element_id)


def validate_required_keys(pkg: LoadedPackage, issues: IssueCollector) -> None:
    for element in pkg.elements:
        if not isinstance(element, dict):
            continue
        element_id = element.get("id")
        for key in contract.REQUIRED_ELEMENT_KEYS:
            if key not in element:
                issues.add(
                    "missing_element_key",
                    f"element {element_id!r} is missing required key {key!r}",
                    stage="structural",
                    element_id=element_id if isinstance(element_id, str) else None,
                    key=key,
                )


def validate_element_types(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Unknown types warn rather than fail.

    Deleting an element because its label is unrecognised is exactly the
    irreversible move the architecture forbids; keeping it and flagging it
    lets the reviewer decide.
    """
    for element in pkg.elements:
        if not isinstance(element, dict):
            continue
        element_type = element.get("type")
        if element_type is None:
            continue
        if not isinstance(element_type, str) or element_type not in contract.ELEMENT_TYPES:
            issues.add(
                "unknown_element_type",
                f"element {element.get('id')!r} has type {element_type!r}, "
                "which is not in the contract enum; it will import as 'unknown'",
                severity=WARNING,
                stage="structural",
                element_id=element.get("id") if isinstance(element.get("id"), str) else None,
                found=str(element_type),
            )


def validate_page_indices(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Page indices must be positive ints declared in ``page_dims``."""
    declared = {str(k) for k in pkg.page_dims}
    for element in pkg.elements:
        if not isinstance(element, dict):
            continue
        page = element.get("page_index")
        if page is None:
            continue
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            issues.add(
                "invalid_page_index",
                f"element {element.get('id')!r} has page_index {page!r}; "
                "expected a 1-based integer",
                stage="structural",
                element_id=element.get("id") if isinstance(element.get("id"), str) else None,
            )
            continue
        if declared and str(page) not in declared:
            issues.add(
                "undeclared_page",
                f"element {element.get('id')!r} is on page {page}, which is not "
                "in manifest.page_dims",
                stage="structural",
                element_id=element.get("id") if isinstance(element.get("id"), str) else None,
                page_index=page,
            )


def validate_page_dims(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """``page_dims`` is mandatory: a bbox is meaningless without page height (§6)."""
    if not pkg.page_dims:
        issues.add(
            "missing_page_dims",
            "manifest.page_dims is absent; bounding boxes cannot be converted "
            "to fitz coordinates without page height",
            stage="structural",
        )
        return
    for page, dims in pkg.page_dims.items():
        if not isinstance(dims, dict):
            issues.add(
                "malformed_page_dims",
                f"page_dims[{page!r}] is not an object",
                stage="structural",
            )
            continue
        for axis in ("width", "height"):
            value = dims.get(axis)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                issues.add(
                    "invalid_page_dimension",
                    f"page_dims[{page!r}].{axis} is {value!r}; expected a positive number",
                    stage="structural",
                    page_index=int(page) if str(page).isdigit() else None,
                )


def validate_parent_ids(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Every ``parent_id`` must resolve, and nothing may parent itself."""
    known = {
        e.get("id")
        for e in pkg.elements
        if isinstance(e, dict) and isinstance(e.get("id"), str)
    }
    # Page containers (``p0011``) are legitimate parents that are not elements.
    known |= {f"p{int(p):04d}" for p in pkg.page_dims if str(p).isdigit()}

    for element in pkg.elements:
        if not isinstance(element, dict):
            continue
        parent = element.get("parent_id")
        if parent in (None, ""):
            continue
        element_id = element.get("id")
        if parent == element_id:
            issues.add(
                "self_parent",
                f"element {element_id!r} is its own parent",
                stage="structural",
                element_id=element_id if isinstance(element_id, str) else None,
            )
            continue
        if parent not in known:
            issues.add(
                "unresolvable_parent_id",
                f"element {element_id!r} references parent {parent!r}, which does not exist",
                stage="structural",
                element_id=element_id if isinstance(element_id, str) else None,
                parent_id=str(parent),
            )


def validate_reading_order(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Reading order must be a duplicate-free ordering over the elements.

    Gaps are a **warning**, not fatal: a gap usually means the worker dropped an
    element, which the deletion accounting already surfaces, and the sequence is
    still totally ordered so the reader still works. Duplicates *are* fatal —
    two elements claiming position 42 have no defined order, and picking one
    silently would reorder a learner's text.
    """
    values: list[int] = []
    for element in pkg.elements:
        if not isinstance(element, dict):
            continue
        order = element.get("reading_order")
        if order is None:
            issues.add(
                "missing_reading_order",
                f"element {element.get('id')!r} has no reading_order",
                severity=WARNING,
                stage="structural",
                element_id=element.get("id") if isinstance(element.get("id"), str) else None,
            )
            continue
        if not isinstance(order, int) or isinstance(order, bool) or order < 0:
            issues.add(
                "invalid_reading_order",
                f"element {element.get('id')!r} has reading_order {order!r}; "
                "expected a non-negative integer",
                stage="structural",
                element_id=element.get("id") if isinstance(element.get("id"), str) else None,
            )
            continue
        values.append(order)

    if not values:
        return

    duplicates = sorted({v for v in values if values.count(v) > 1})
    if duplicates:
        issues.add(
            "duplicate_reading_order",
            f"reading_order values {duplicates} are used by more than one element",
            stage="structural",
            duplicates=duplicates,
        )

    ordered = sorted(set(values))
    expected = list(range(ordered[0], ordered[0] + len(ordered)))
    if ordered != expected:
        missing = sorted(set(expected) - set(ordered))
        issues.add(
            "reading_order_gaps",
            f"reading_order is not contiguous; {len(missing)} position(s) missing",
            severity=WARNING,
            stage="structural",
            missing=missing[:20],
        )


def validate_coordinate_space(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """The coordinate space must be declared and must be the one we convert from.

    Fatal because the failure is silent: importing bottom-left bboxes as if they
    were top-left puts every highlight in the wrong place, and nothing later in
    the pipeline would notice.
    """
    manifest_space = pkg.manifest.get("coordinate_space")
    if manifest_space is None:
        issues.add(
            "missing_coordinate_space",
            "manifest has no coordinate_space; refusing to guess",
            stage="structural",
        )
    elif manifest_space != contract.COORDINATE_SPACE:
        issues.add(
            "coordinate_space_mismatch",
            f"manifest coordinate_space is {manifest_space!r}, expected "
            f"{contract.COORDINATE_SPACE!r}",
            stage="structural",
            found=str(manifest_space),
            expected=contract.COORDINATE_SPACE,
        )

    for element in pkg.elements:
        if not isinstance(element, dict):
            continue
        bbox = element.get("bbox")
        if not isinstance(bbox, dict):
            continue
        space = bbox.get("coordinate_space")
        if space is not None and space != contract.COORDINATE_SPACE:
            issues.add(
                "coordinate_space_mismatch",
                f"element {element.get('id')!r} declares bbox coordinate_space "
                f"{space!r}, expected {contract.COORDINATE_SPACE!r}",
                stage="structural",
                element_id=element.get("id") if isinstance(element.get("id"), str) else None,
                found=str(space),
            )


def validate_bboxes(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Bboxes must be numeric, y-up, and inside their page."""
    dims = {str(k): v for k, v in pkg.page_dims.items() if isinstance(v, dict)}

    for element in pkg.elements:
        if not isinstance(element, dict):
            continue
        bbox = element.get("bbox")
        if bbox is None:
            continue
        element_id = element.get("id") if isinstance(element.get("id"), str) else None
        if not isinstance(bbox, dict):
            issues.add(
                "malformed_bbox",
                f"element {element_id!r} has a non-object bbox",
                stage="structural",
                element_id=element_id,
            )
            continue

        values = {}
        malformed = False
        for key in _BBOX_KEYS:
            value = bbox.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                issues.add(
                    "malformed_bbox",
                    f"element {element_id!r} bbox.{key} is {value!r}; expected a number",
                    stage="structural",
                    element_id=element_id,
                )
                malformed = True
                break
            values[key] = float(value)
        if malformed:
            continue

        if values["x2"] < values["x1"]:
            issues.add(
                "inverted_bbox_x",
                f"element {element_id!r} has x2 < x1",
                stage="structural",
                element_id=element_id,
            )
        # Docling space is y-up, so the top edge (y1) must exceed the bottom.
        if values["y1"] < values["y2"]:
            issues.add(
                "inverted_bbox_y",
                f"element {element_id!r} has y1 < y2, which contradicts the "
                f"{contract.COORDINATE_SPACE} convention (y1 is the top edge)",
                severity=WARNING,
                stage="structural",
                element_id=element_id,
            )

        page = element.get("page_index")
        page_dims = dims.get(str(page))
        if not page_dims:
            continue
        width = page_dims.get("width")
        height = page_dims.get("height")
        if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
            continue
        if (
            values["x1"] < -_BBOX_TOLERANCE_PT
            or values["x2"] > width + _BBOX_TOLERANCE_PT
            or min(values["y1"], values["y2"]) < -_BBOX_TOLERANCE_PT
            or max(values["y1"], values["y2"]) > height + _BBOX_TOLERANCE_PT
        ):
            issues.add(
                "bbox_outside_page",
                f"element {element_id!r} bbox extends outside page {page} "
                f"({width}×{height} pt)",
                stage="structural",
                element_id=element_id,
                page_index=page if isinstance(page, int) else None,
            )


def validate_confidence(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Confidence must be an object of nullable numbers in ``[0, 1]``.

    Malformed confidence warns rather than fails — an unreadable score is a
    reason to treat the element as unproven, not to discard it.
    """
    for element in pkg.elements:
        if not isinstance(element, dict):
            continue
        confidence = element.get("confidence")
        if confidence is None:
            continue
        element_id = element.get("id") if isinstance(element.get("id"), str) else None
        if not isinstance(confidence, dict):
            issues.add(
                "malformed_confidence",
                f"element {element_id!r} confidence must be an object, got "
                f"{type(confidence).__name__}",
                severity=WARNING,
                stage="structural",
                element_id=element_id,
            )
            continue
        for key, value in confidence.items():
            if value is None:
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                issues.add(
                    "malformed_confidence",
                    f"element {element_id!r} confidence.{key} is {value!r}; "
                    "expected a number or null",
                    severity=WARNING,
                    stage="structural",
                    element_id=element_id,
                )
            elif not 0.0 <= float(value) <= 1.0:
                issues.add(
                    "confidence_out_of_range",
                    f"element {element_id!r} confidence.{key} is {value}; expected 0–1",
                    severity=WARNING,
                    stage="structural",
                    element_id=element_id,
                )


def validate_provenance(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Provenance must be an object when present. Absent is a warning.

    Provenance completeness is a Stage 1 fidelity metric, so a package without
    it is importable but measurably weaker.
    """
    for element in pkg.elements:
        if not isinstance(element, dict):
            continue
        element_id = element.get("id") if isinstance(element.get("id"), str) else None
        provenance = element.get("provenance")
        if provenance is None:
            issues.add(
                "missing_provenance",
                f"element {element_id!r} has no provenance",
                severity=WARNING,
                stage="structural",
                element_id=element_id,
            )
        elif not isinstance(provenance, dict):
            issues.add(
                "malformed_provenance",
                f"element {element_id!r} provenance must be an object, got "
                f"{type(provenance).__name__}",
                severity=WARNING,
                stage="structural",
                element_id=element_id,
            )


#: Run in this order. Ids first — later validators report against them.
STRUCTURAL_VALIDATORS = (
    validate_element_ids,
    validate_required_keys,
    validate_element_types,
    validate_page_dims,
    validate_page_indices,
    validate_parent_ids,
    validate_reading_order,
    validate_coordinate_space,
    validate_bboxes,
    validate_confidence,
    validate_provenance,
)


def validate_structure(pkg: LoadedPackage, issues: IssueCollector) -> None:
    """Run every structural validator, collecting all findings."""
    for validator in STRUCTURAL_VALIDATORS:
        validator(pkg, issues)
