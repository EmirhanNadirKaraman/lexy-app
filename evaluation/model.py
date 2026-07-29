"""Shared types and the adapter seam every ingestion pipeline plugs into.

The harness must eventually compare five things (roadmap A1): the current
language-app pipeline, ``nlp_histo`` document packages, AI-assisted review,
reconstruction, and sentence segmentation. Rather than teach the metrics about
each, they all reduce to one shape — :class:`PredictedDocument` — produced by a
:class:`DocumentSource`.

Adding a pipeline is therefore a **new adapter file**, never a change to
:mod:`evaluation.metrics`. Exactly one adapter ships today
(:class:`evaluation.adapters.DoclingLayoutJsonSource`); the rest arrive with
A2–A8.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable


@dataclass(frozen=True)
class PredictedSentence:
    """One reader unit produced by a pipeline under test.

    Provenance fields are optional because today's pipelines cannot supply
    them: ``HierarchicalRow`` in the extraction stage carries no page, bbox or
    element id, and ``book_blocks`` has no reading order. When they are absent
    the provenance metric reports ``None`` (not zero) so a missing capability
    is never confused with a measured failure.
    """

    text: str
    source_pages: tuple[int, ...] = ()
    source_block_ids: tuple[str, ...] = ()
    source_element_ids: tuple[str, ...] = ()
    uncertain: bool = False
    """Kept, but the pipeline is unsure it is story text.

    The preferred alternative to deletion. A unit flagged this way costs almost
    nothing in the fidelity score — the reviewer can still remove it — whereas
    deleting it is irreversible.
    """
    confidence: float | None = None
    """Optional 0–1 confidence. ``None`` means the pipeline emits none."""

    @property
    def has_page_provenance(self) -> bool:
        return bool(self.source_pages)

    @property
    def has_block_provenance(self) -> bool:
        return bool(self.source_block_ids or self.source_element_ids)


@dataclass(frozen=True)
class DroppedElement:
    """Something the deterministic pipeline removed, and why.

    Recorded rather than merely discarded, because a deletion is the one
    decision the downstream reviewer cannot undo. A pipeline that cannot say
    what it deleted cannot be measured on information preservation at all —
    :func:`evaluation.metrics.fidelity_metrics` reports ``None`` for its
    deletion accounting rather than assuming the best.
    """

    text: str
    reason: str
    source_pages: tuple[int, ...] = ()
    source_block_ids: tuple[str, ...] = ()
    confidence: float | None = None


@dataclass
class PredictedDocument:
    """A pipeline's output for one document, in comparable form."""

    document_id: str
    sentences: list[PredictedSentence] = field(default_factory=list)
    dropped: list[DroppedElement] | None = None
    """What the pipeline deleted. ``None`` = "this pipeline does not report
    deletions", which is measured as an unknown, not as zero."""
    #: Free-form, recorded in the run result for debugging. Never measured.
    diagnostics: dict = field(default_factory=dict)

    @property
    def reports_deletions(self) -> bool:
        return self.dropped is not None

    @property
    def uncertain_units(self) -> list[PredictedSentence]:
        return [s for s in self.sentences if s.uncertain]

    @property
    def continuous_text(self) -> str:
        """Built identically to :attr:`evaluation.annotation.GoldDocument.continuous_text`.

        The two constructions must stay in lockstep — every offset-based metric
        assumes it.
        """
        return " ".join(s.text for s in self.sentences)

    @property
    def provides_page_provenance(self) -> bool:
        return any(s.has_page_provenance for s in self.sentences)

    @property
    def provides_block_provenance(self) -> bool:
        return any(s.has_block_provenance for s in self.sentences)


@runtime_checkable
class DocumentSource(Protocol):
    """Something that can produce a :class:`PredictedDocument` per document id.

    Implementations live in :mod:`evaluation.adapters`. Keep them thin: an
    adapter's job is to reshape an existing pipeline's output, not to add
    cleanup logic. Any cleverness here would be measured as pipeline quality
    and quietly flatter the thing under test.
    """

    #: Short identifier recorded in the run result, e.g. ``"docling_json"``.
    name: str

    def available_documents(self) -> Iterable[str]:
        """Document ids this source can produce output for."""
        ...

    def load(self, document_id: str) -> PredictedDocument:
        """Produce the pipeline's reader units for *document_id*."""
        ...
