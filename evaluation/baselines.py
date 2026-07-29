"""Frozen measurements, recorded as data rather than recomputed.

Three of the figures in ``docs/INGESTION_PIPELINE.md`` §2 are produced by
*importing* functions from ``language-app/nlp_histo/``, the vendored copy that
roadmap task **A11** deletes. If the harness recomputed them, the suite would
break the day that directory goes away.

So they are frozen here with full provenance. Re-measuring them is a deliberate
act performed in the worker environment, not something the test suite does.

The other baseline figures — unterminated rate, lowercase-start rate,
bare-number blocks, label distribution — need no ``nlp_histo`` import and ARE
recomputed live by ``scripts/eval/extraction_stats.py``. Only the
profile-dependent ones are frozen.
"""
from __future__ import annotations

from dataclasses import dataclass

CORPUS_DESCRIPTION = (
    "27 German graded readers (A2–B1), 1699 pages, 27459 elements, "
    "as Docling layout JSON at files/json/*_layout.json (gitignored)"
)


@dataclass(frozen=True)
class FrozenBaseline:
    key: str
    value: float
    unit: str
    what: str
    source: str
    measured_on: str


#: Measured 2026-07-29 against language-app/nlp_histo/parsers/, verified
#: byte-identical to nlp-histo/src/nlp_histo/parsers/ via `diff -rq`.
FROZEN: tuple[FrozenBaseline, ...] = (
    FrozenBaseline(
        key="is_relevant_para_drop_rate",
        value=30.5,
        unit="percent",
        what=(
            "Share of German prose blocks that nlp_histo's is_relevant_para "
            "would delete. Its 4–19-word branch requires a verb or a "
            "biomedical entity; short German dialogue has neither."
        ),
        source="nlp_histo/parsers/layout_utils.py:360-398",
        measured_on="2026-07-29",
    ),
    FrozenBaseline(
        key="remove_citations_mutation_rate",
        value=9.1,
        unit="percent",
        what="Share of German prose blocks whose text remove_citations silently alters.",
        source="nlp_histo/parsers/text_processing.py:286",
        measured_on="2026-07-29",
    ),
    FrozenBaseline(
        key="is_cut_off_recall_german",
        value=4.0,
        unit="percent",
        what=(
            "Share of unterminated German blocks ContextAwareStitcher._is_cut_off "
            "detects. Its connector list is English. A German-tuned rule "
            "(lowercase-next ∪ German connectors ∪ hyphen) reaches 41.1%."
        ),
        source="nlp_histo/parsers/text_processing.py:127",
        measured_on="2026-07-29",
    ),
    FrozenBaseline(
        key="german_tuned_stitch_recall",
        value=41.1,
        unit="percent",
        what="Recall of the proposed German stitching rule on the same blocks.",
        source="docs/INGESTION_PIPELINE.md §2",
        measured_on="2026-07-29",
    ),
    FrozenBaseline(
        key="spacy_de_hard_case_accuracy",
        value=87.5,
        unit="percent",
        what=(
            "spaCy de_core_news_md on 8 hard German segmentation cases: 7/8. "
            "The only failure is a missing terminal period across a line break."
        ),
        source="docs/INGESTION_PIPELINE.md §2",
        measured_on="2026-07-29",
    ),
)

BY_KEY: dict[str, FrozenBaseline] = {b.key: b for b in FROZEN}


def as_dict() -> dict:
    """Serializable form, embedded in every run result for provenance."""
    return {
        "corpus": CORPUS_DESCRIPTION,
        "values": {
            b.key: {
                "value": b.value,
                "unit": b.unit,
                "what": b.what,
                "source": b.source,
                "measured_on": b.measured_on,
            }
            for b in FROZEN
        },
    }
