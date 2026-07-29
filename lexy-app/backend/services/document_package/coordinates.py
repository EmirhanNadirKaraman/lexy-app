"""The single conversion point between package space and database space (§6).

Two systems meet here and nowhere else:

===================  ============  =============  ==========================
System               Origin        y direction    Used by
===================  ============  =============  ==========================
Docling PDF space    bottom-left   up             the package, every bbox in it
fitz screen space    top-left      down           ``book_blocks.bbox_*``, PyMuPDF
===================  ============  =============  ==========================

In Docling space a well-formed box has ``y1 > y2`` — ``y1`` is the *top* edge,
because larger y is higher up the page. Verified against the real corpus: 100%
of 645 sampled elements.

Getting this wrong is silent. Nothing downstream would notice; every highlight
in the reader would simply be in the wrong place. Hence: one function, used by
the importer only, with a round-trip test.
"""
from __future__ import annotations

from dataclasses import dataclass


class CoordinateError(ValueError):
    """Raised when a bbox cannot be converted — missing page height, bad numbers."""


@dataclass(frozen=True)
class FitzBox:
    """Top-left-origin, y-down box, matching ``book_blocks.bbox_x0..y1``."""

    x0: float
    y0: float
    x1: float
    y1: float

    def as_columns(self) -> dict[str, float]:
        return {
            "bbox_x0": self.x0,
            "bbox_y0": self.y0,
            "bbox_x1": self.x1,
            "bbox_y1": self.y1,
        }


def to_fitz(bbox: dict, page_height: float) -> FitzBox:
    """Convert a package bbox to fitz screen space.

    Mirrors ``nlp-histo``'s ``BoundingBox.to_fitz_rect``, including its
    defensive ``max``/``min``: a box whose y values arrive inverted still
    converts to a sane rectangle rather than a negative-height one. The
    inversion itself is reported separately by
    :func:`validators.validate_bboxes`, so tolerating it here does not hide it.
    """
    if not isinstance(page_height, (int, float)) or page_height <= 0:
        raise CoordinateError(f"page_height must be positive, got {page_height!r}")
    try:
        x1 = float(bbox["x1"])
        y1 = float(bbox["y1"])
        x2 = float(bbox["x2"])
        y2 = float(bbox["y2"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CoordinateError(f"malformed bbox: {bbox!r}") from exc

    top = page_height - max(y1, y2)
    bottom = page_height - min(y1, y2)
    return FitzBox(x0=min(x1, x2), y0=top, x1=max(x1, x2), y1=bottom)


def from_fitz(box: FitzBox, page_height: float) -> dict:
    """Inverse of :func:`to_fitz`, for round-trip testing.

    Exactly inverse only when the input satisfied ``y0 < y1`` — which
    :func:`to_fitz` always guarantees on its output.
    """
    if not isinstance(page_height, (int, float)) or page_height <= 0:
        raise CoordinateError(f"page_height must be positive, got {page_height!r}")
    return {
        "x1": box.x0,
        "y1": page_height - box.y0,
        "x2": box.x1,
        "y2": page_height - box.y1,
    }


def page_height_for(page_dims: dict, page_index: int) -> float:
    """Look up a page's height, accepting str or int keys.

    JSON object keys are always strings, but a caller holding an int should not
    have to remember that.
    """
    dims = page_dims.get(str(page_index))
    if dims is None:
        dims = page_dims.get(page_index)
    if not isinstance(dims, dict):
        raise CoordinateError(f"no page_dims entry for page {page_index}")
    height = dims.get("height")
    if not isinstance(height, (int, float)) or isinstance(height, bool) or height <= 0:
        raise CoordinateError(f"page {page_index} has invalid height {height!r}")
    return float(height)
