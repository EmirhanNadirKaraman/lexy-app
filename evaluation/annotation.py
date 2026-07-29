"""The gold annotation format: a reviewable plain-text file plus a JSON sidecar.

Design constraints, in priority order:

1. **A human must be able to edit it in any text editor and see it is right.**
   That rules out character offsets in the file — they rot the moment someone
   fixes a typo. Every offset the metrics need is *derived at load time* from
   the text itself.
2. **Structure inline, lists in the sidecar.** Sentence, paragraph and page
   structure is what the eye checks, so it is expressed in the layout of the
   text. Everything list-shaped (expected removals, difficult regions, known
   artifacts) goes in ``<name>.gold.json`` instead of growing inline markup.
3. **Round-trips exactly.** ``parse → serialize`` is byte-identical, which is
   what stops the format drifting as documents are added.

Format
------

::

    # Lines starting with '#' are comments.
    ==== PAGE 1 ====
    Dr. Müller kam um 17 Uhr an.
    Er setzte sich.

    Der zweite Absatz beginnt hier.

    ==== PAGE 2 ====
    Ein Satz, der über den <PB/> Seitenumbruch hinweg weitergeht.

* ``==== PAGE n ====`` starts a page. Pages are 1-based.
* One **sentence** per line — this is the unit the reader will show.
* A **blank line** separates paragraphs.
* ``<PB/>`` inside a sentence marks a page break *within* that sentence. The
  marker is removed from the sentence text and turns into a second entry in
  ``source_pages``. This is how a page-spanning sentence is expressed without
  needing a second structural mechanism.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize_text

PAGE_RE = re.compile(r"^====\s*PAGE\s+(\d+)\s*====$")
PAGE_BREAK_MARKER = "<PB/>"
COMMENT_PREFIX = "#"


class AnnotationError(ValueError):
    """Raised when a gold file cannot be parsed. Always names the line number."""


@dataclass(frozen=True)
class GoldSentence:
    """One expected reader unit."""

    text: str
    """Sentence text with ``<PB/>`` removed and whitespace normalized."""

    paragraph_index: int
    """0-based index of the containing paragraph, document-wide."""

    sentence_index: int
    """0-based index of this sentence, document-wide."""

    source_pages: tuple[int, ...]
    """Pages this sentence draws from. Length > 1 means it spans a break."""

    @property
    def spans_pages(self) -> bool:
        return len(self.source_pages) > 1


@dataclass
class GoldDocument:
    """A parsed gold annotation: the expected output of a perfect pipeline."""

    document_id: str
    sentences: list[GoldSentence] = field(default_factory=list)
    #: Sidecar payload; see :meth:`sidecar_path`. Empty dict when absent.
    meta: dict = field(default_factory=dict)

    # -- derived views -----------------------------------------------------

    @property
    def continuous_text(self) -> str:
        """The whole document as one normalized string.

        This is the reference for every offset-based metric. It is built by
        joining sentences with a single space, which is exactly how
        :func:`evaluation.metrics.boundary_offsets` reconstructs the predicted
        side — the two must agree or all offsets shift.
        """
        return " ".join(s.text for s in self.sentences)

    @property
    def paragraph_count(self) -> int:
        return len({s.paragraph_index for s in self.sentences})

    @property
    def page_count(self) -> int:
        pages: set[int] = set()
        for s in self.sentences:
            pages.update(s.source_pages)
        return len(pages)

    @property
    def page_spanning_sentences(self) -> list[GoldSentence]:
        return [s for s in self.sentences if s.spans_pages]

    @property
    def expected_removals(self) -> list[dict]:
        """Text the pipeline is expected to drop (headers, footers, page numbers).

        These must NOT appear as reader units. :func:`evaluation.metrics.
        reader_quality` counts any predicted unit matching one of these as a
        false-negative removal.
        """
        return list(self.meta.get("expected_removals", []))

    @property
    def difficult_regions(self) -> list[dict]:
        return list(self.meta.get("difficult_regions", []))

    @property
    def phenomena(self) -> list[str]:
        return list(self.meta.get("phenomena", []))


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def parse_gold_text(text: str, document_id: str) -> GoldDocument:
    """Parse the plain-text half of a gold annotation.

    Raises :class:`AnnotationError` with a 1-based line number on any problem,
    because a silently mis-parsed gold file produces confidently wrong metrics.
    """
    doc = GoldDocument(document_id=document_id)
    current_page: int | None = None
    paragraph_index = -1
    started_paragraph = False

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()

        if not line:
            started_paragraph = False  # blank line closes the paragraph
            continue
        if line.startswith(COMMENT_PREFIX):
            continue

        page_match = PAGE_RE.match(line)
        if page_match:
            page = int(page_match.group(1))
            if current_page is not None and page <= current_page:
                raise AnnotationError(
                    f"{document_id}:{lineno}: page {page} does not increase "
                    f"(previous was {current_page})"
                )
            current_page = page
            started_paragraph = False
            continue

        if current_page is None:
            raise AnnotationError(
                f"{document_id}:{lineno}: sentence before any '==== PAGE n ====' marker"
            )

        if not started_paragraph:
            paragraph_index += 1
            started_paragraph = True

        pages = (current_page,)
        if PAGE_BREAK_MARKER in line:
            pages = (current_page, current_page + 1)
            line = line.replace(PAGE_BREAK_MARKER, " ")

        sentence_text = normalize_text(line)
        if not sentence_text:
            raise AnnotationError(f"{document_id}:{lineno}: empty sentence")

        doc.sentences.append(
            GoldSentence(
                text=sentence_text,
                paragraph_index=paragraph_index,
                sentence_index=len(doc.sentences),
                source_pages=pages,
            )
        )

    if not doc.sentences:
        raise AnnotationError(f"{document_id}: no sentences found")
    return doc


def serialize_gold_text(doc: GoldDocument) -> str:
    """Inverse of :func:`parse_gold_text`. Byte-identical round-trip."""
    lines: list[str] = []
    current_page: int | None = None
    last_paragraph: int | None = None

    for sentence in doc.sentences:
        page = sentence.source_pages[0]
        if page != current_page:
            if lines:
                lines.append("")
            lines.append(f"==== PAGE {page} ====")
            current_page = page
            last_paragraph = None

        if last_paragraph is not None and sentence.paragraph_index != last_paragraph:
            lines.append("")
        last_paragraph = sentence.paragraph_index

        text = sentence.text
        if sentence.spans_pages:
            # Re-insert the marker at the end; the exact position within the
            # sentence is not recoverable, and only the page set is measured.
            #
            # `current_page` deliberately stays on source_pages[0]. The parser
            # treats <PB/> as "this sentence also touches the next page"
            # WITHOUT advancing its page counter, so advancing here would
            # swallow the following '==== PAGE n ====' header and silently
            # reassign every later sentence to the wrong page.
            text = f"{text} {PAGE_BREAK_MARKER}"
        lines.append(text)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# filesystem
# ---------------------------------------------------------------------------


def gold_path(directory: Path, document_id: str) -> Path:
    return directory / f"{document_id}.gold.txt"


def sidecar_path(directory: Path, document_id: str) -> Path:
    return directory / f"{document_id}.gold.json"


def load_gold(directory: Path, document_id: str) -> GoldDocument:
    """Load ``<id>.gold.txt`` plus optional ``<id>.gold.json`` from *directory*."""
    text_file = gold_path(directory, document_id)
    if not text_file.is_file():
        raise AnnotationError(f"missing gold file: {text_file}")

    doc = parse_gold_text(text_file.read_text(encoding="utf-8"), document_id)

    side = sidecar_path(directory, document_id)
    if side.is_file():
        try:
            doc.meta = json.loads(side.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AnnotationError(f"{side}: invalid JSON — {exc}") from exc
    return doc


def discover_document_ids(directory: Path) -> list[str]:
    """Every document id with a ``.gold.txt`` in *directory*, sorted."""
    if not directory.is_dir():
        return []
    return sorted(p.name[: -len(".gold.txt")] for p in directory.glob("*.gold.txt"))
