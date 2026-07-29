"""Deterministic German sentence segmentation — the baseline under test.

This is *not* a new segmenter for production. It is the reference
implementation the harness measures, so that A5's real
``sentence_service`` has a number to beat and a regression to be caught by.

It mirrors ``subtitle_segmenter.py``, the repo's only existing segmentation
implementation, which already emits ``char_start``/``char_end`` per sentence.
Reusing that shape means A5 can adopt this without re-deriving it.

Measured behaviour on the German phenomena that matter (see
``docs/INGESTION_PIPELINE.md`` §2): spaCy ``de_core_news_md`` handles ``Dr.``,
``z. B.``, ``usw.``, ordinal ``am 3. Oktober``, ``Nr. 5``, ``„…"`` and
ellipsis correctly. The one case it cannot handle is a *missing* terminal
period across a line break — that requires evidence from the page and is the
narrow job reserved for AI review.
"""
from __future__ import annotations

import functools
import re

from .model import PredictedSentence
from .normalize import normalize_text

DEFAULT_MODEL = "de_core_news_md"
MIN_CHARS = 3
"""Shorter than this is never a reader unit. Same threshold as ``subtitle_segmenter``."""

#: Abbreviations that must never end a sentence. spaCy's German parser already
#: gets most of these right; the list is belt-and-braces for the blank-model
#: fallback, which uses the rule-based sentencizer and has no parser to lean on.
GERMAN_ABBREVIATIONS = (
    "z.B.", "z. B.", "u.a.", "u. a.", "d.h.", "d. h.", "bzw.", "usw.", "etc.",
    "ca.", "vgl.", "Nr.", "Dr.", "Prof.", "Hr.", "Fr.", "St.", "Bd.", "Abb.",
    "ggf.", "inkl.", "max.", "min.", "evtl.", "Jh.", "Jhd.", "Mio.", "Mrd.",
)


class SegmenterUnavailable(RuntimeError):
    """Raised when no usable spaCy model is installed."""


@functools.lru_cache(maxsize=4)
def _load_pipeline(model: str):
    """Load *model*, falling back to a blank German pipeline + sentencizer.

    The fallback keeps the harness runnable in a bare environment (CI, a fresh
    clone) without a model download. It is measurably worse on abbreviations,
    so the run result records which one was used — comparing a full-model run
    against a fallback run would be meaningless, and
    :func:`evaluation.compare.compare_runs` guards on it.
    """
    import spacy

    try:
        return spacy.load(model, exclude=["ner"]), model
    except (OSError, IOError):
        nlp = spacy.blank("de")
        nlp.add_pipe("sentencizer")
        return nlp, f"blank:de+sentencizer (fallback, {model} unavailable)"


def segmenter_info(model: str = DEFAULT_MODEL) -> str:
    """Human-readable name of the pipeline that will actually be used."""
    try:
        _, name = _load_pipeline(model)
    except ImportError as exc:  # pragma: no cover - spaCy is a hard dependency
        raise SegmenterUnavailable("spaCy is not installed") from exc
    return name


_ABBREV_TAIL = re.compile(
    r"(?:^|\s)(" + "|".join(re.escape(a) for a in GERMAN_ABBREVIATIONS) + r")$"
)


def _ends_with_abbreviation(text: str) -> bool:
    return bool(_ABBREV_TAIL.search(text.strip()))


def segment(text: str, model: str = DEFAULT_MODEL) -> list[str]:
    """Split *text* into sentences.

    Post-processing beyond spaCy: a candidate ending in a known abbreviation is
    merged with the next one **only when that next candidate starts with a
    lowercase letter**.

    The lowercase condition is load-bearing, not defensive. German capitalizes
    every sentence start (and all nouns), so a capitalized follower is a new
    sentence even after an abbreviation — ``"…Milch usw. Dann ging sie."`` is
    two sentences, and merging on the abbreviation alone destroys a boundary
    spaCy's parser got right. Only a lowercase follower indicates a genuine
    continuation such as ``"z. B. für Kinder"``.
    """
    normalized = normalize_text(text)
    if not normalized:
        return []

    nlp, _ = _load_pipeline(model)
    raw = [s.text.strip() for s in nlp(normalized).sents]

    merged: list[str] = []
    for candidate in raw:
        if not candidate:
            continue
        if (
            merged
            and _ends_with_abbreviation(merged[-1])
            and candidate[:1].islower()
        ):
            merged[-1] = f"{merged[-1]} {candidate}"
        else:
            merged.append(candidate)

    return [s for s in merged if len(s) >= MIN_CHARS]


def segment_to_sentences(
    text: str,
    model: str = DEFAULT_MODEL,
    source_pages: tuple[int, ...] = (),
    source_block_ids: tuple[str, ...] = (),
) -> list[PredictedSentence]:
    """:func:`segment`, wrapped in :class:`PredictedSentence` with provenance."""
    return [
        PredictedSentence(
            text=s,
            source_pages=source_pages,
            source_block_ids=source_block_ids,
        )
        for s in segment(text, model)
    ]
