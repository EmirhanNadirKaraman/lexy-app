"""The document-package contract, as constants.

This module is the app's half of the interface described in
``docs/INGESTION_PIPELINE.md`` §4. It deliberately contains **no logic** — just
the values both sides must agree on — so that a contract change is a visible
one-line diff rather than a behavioural surprise buried in a validator.

Nothing here imports ``nlp_histo``, Docling, Torch or Transformers. The backend
works purely from the package on disk; that boundary is the deliverable (§3).
"""
from __future__ import annotations

PACKAGE_SCHEMA_VERSION = "1.0.0"
"""Package schema this backend understands.

Compatibility is **major-version only**: ``1.x`` is accepted, ``2.x`` is not.
A minor bump means additive fields the importer can ignore.
"""

IMPORT_SCHEMA_VERSION = "1.0.0"
"""Version of the ``import_result.json`` this backend writes."""

MANIFEST_FILENAME = "manifest.json"
DOCUMENT_FILENAME = "document.json"
CHECKSUMS_FILENAME = "CHECKSUMS.txt"
IMPORT_RESULT_FILENAME = "import_result.json"
PAGES_DIRNAME = "pages"
ASSETS_DIRNAME = "assets"

COORDINATE_SPACE = "pdf_points_bottom_left"
"""The only coordinate space the contract admits.

Docling space: origin bottom-left, y-up, so a well-formed bbox has ``y1 > y2``
(y1 is the *top* edge). ``book_blocks`` stores fitz screen space instead —
origin top-left, y-down. The conversion happens exactly once, at the import
boundary (§6), which is why a mismatch here is fatal rather than a warning:
silently accepting the wrong space would misplace every bbox in the reader.
"""

PAGE_INDEX_BASE = 1
"""Pages are **1-based** everywhere in the package — there is no zero-based half.

Every page-bearing field counts from 1 and they must all agree:

* ``element.page_index``
* ``bbox.page`` — a redundant copy of ``page_index``, not a second numbering
* ``manifest.page_dims`` keys (``"1"``, ``"2"``, …)
* the element-id prefix ``p{page:04d}`` and ``parent_id`` page containers
* ``manifest.page_images.path_template`` (``pages/{page_index:04d}.png``)

Stated as a constant because the alternative was folklore. ``bbox.page``
duplicating ``page_index`` looks like it *could* be a different coordinate
convention — it is not, and a reader who assumes otherwise would "fix" a real
mismatch by adding an off-by-one. See :func:`validators.validate_bbox_page_consistency`.
"""

ELEMENT_TYPES = frozenset(
    {
        "paragraph",
        "heading",
        "list_item",
        "caption",
        "footnote",
        "table",
        "figure",
        "page_header",
        "page_footer",
        "page_number",
        "formula",
        "code",
        "index",
        "checkbox",
        "unknown",
    }
)
"""Our own stable type enum — deliberately **not** Docling's labels.

The worker maps its 13 observed raw labels onto these and preserves the
original in ``docling_label``. ``nlp-histo`` stringifies Docling labels
defensively in four separate places precisely because they shift between
releases; the contract must not inherit that fragility.

``unknown`` exists so an unmapped label degrades to a warning rather than
losing the element — consistent with the preservation principle (§2b).
"""

PROSE_TYPES = frozenset({"paragraph", "list_item", "heading", "caption", "footnote"})
"""Types that carry reader-facing text. Used only for reporting counts here;
what becomes a sentence is A5's decision, not the importer's."""

ACCEPTED_PROFILES = frozenset({"german_fiction"})
"""Extractor profiles this backend will import.

The biomedical profile runs ``is_relevant_para``, which deletes 30.5% of German
prose (§2). Refusing it at the boundary is cheaper and far safer than trying to
detect the damage afterwards — by then the content is already gone.
"""

REQUIRED_CHECKSUM_FILES = (MANIFEST_FILENAME, DOCUMENT_FILENAME)
"""Files whose integrity ``CHECKSUMS.txt`` must cover.

These two *are* the package — everything else (page images, assets) is
regenerable. An inventory that omits them proves nothing about the parts that
matter, so a missing entry is fatal rather than merely incomplete.
"""

CHECKSUM_ALGORITHM = "sha256"
"""The only digest the contract documents. An unrecognised digest length is
rejected rather than guessed at; adding another algorithm is a contract change,
not a validator tweak."""

REQUIRED_MANIFEST_KEYS = ("package_schema_version", "document_id", "source", "extractor")
REQUIRED_SOURCE_KEYS = ("sha256",)
REQUIRED_DOCUMENT_KEYS = ("elements",)
REQUIRED_ELEMENT_KEYS = ("id", "page_index", "type")

STATUS_IMPORTED = "imported"
STATUS_REJECTED = "rejected"
STATUS_PARTIAL = "partial_rejected"


def schema_major(version: str) -> int:
    """Major component of a dotted version string.

    Raises :class:`ValueError` on anything unparseable — a malformed version is
    a fatal contract violation, not something to guess about.
    """
    head = str(version).split(".", 1)[0].strip()
    if not head.isdigit():
        raise ValueError(f"unparseable schema version: {version!r}")
    return int(head)


def is_compatible_schema(version: str) -> bool:
    """True when *version* shares this backend's major version."""
    try:
        return schema_major(version) == schema_major(PACKAGE_SCHEMA_VERSION)
    except ValueError:
        return False
