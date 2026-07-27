"""
subtitle_segmenter.py
---------------------
Sentence / utterance segmentation stage for a German language-learning pipeline.

Takes MergedSubtitleWindow objects (output of SubtitleMerger) and splits each
window's merged text into CandidateUtterance objects using spaCy sentence
boundaries (doc.sents).

Both spaCy sentence segmentation components are supported transparently:
  - "sentencizer"  rule-based, fast, punctuation-driven
  - "senter"       neural, higher recall on conversational German (no punctuation)

The caller is responsible for configuring the nlp pipeline. This class only
calls nlp(text) / nlp.pipe(texts) and reads doc.sents.

Usage:
    import spacy
    from subtitle_segmenter import CandidateUtterance, SegmentationConfig, SubtitleSegmenter

    nlp = spacy.load("de_core_news_md")          # includes "senter" by default
    segmenter = SubtitleSegmenter(nlp)
    candidates = segmenter.segment_windows(windows)
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Literal, Optional

from spacy.language import Language
from spacy.tokens import Doc

from subtitle_merger import MergedSubtitleWindow


# ---------------------------------------------------------------------------
# Components known to set sentence boundaries in a spaCy pipeline.
# Used only for the startup validation warning.
# ---------------------------------------------------------------------------
_SENT_COMPONENTS: frozenset[str] = frozenset({"sentencizer", "senter", "parser"})


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class CandidateUtterance:
    """
    A single candidate learnable unit produced by segmenting a MergedSubtitleWindow.

    Attributes:
        text:          Stripped sentence / utterance text.
        start_time:    Approximate start time in seconds (see timing note).
        end_time:      Approximate end time in seconds (see timing note).
        source_window: The MergedSubtitleWindow this sentence came from.
        char_start:    Character offset of this sentence within source_window.text.
        char_end:      End character offset within source_window.text (exclusive).

    Timing note:
        When spaCy splits a merged window into multiple sentences, the exact
        per-sentence timestamps are unknown — the original subtitle blocks only
        mark the boundaries of the whole window. Times here are estimated by
        linear interpolation over the character position of the sentence within
        the window text, assuming a roughly uniform speech rate.

        This is a deliberate simplification. Sub-second accuracy is not required
        for a language-learning app: the timestamp is used to seek a video to
        roughly the right position, not for transcript alignment. Forced-alignment
        tools (e.g. WhisperX, wav2vec) would be needed for frame-accurate timing.

        For windows that produce only a single sentence the times equal the
        original window's start_time / end_time exactly (no approximation).
    """
    text: str
    start_time: float
    end_time: float
    source_window: MergedSubtitleWindow
    char_start: int
    char_end: int

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time

    @property
    def char_length(self) -> int:
        return self.char_end - self.char_start

    def __repr__(self) -> str:
        preview = self.text if len(self.text) <= 60 else self.text[:57] + "..."
        return (
            f"CandidateUtterance("
            f"{self.start_time:.2f}s–{self.end_time:.2f}s, "
            f"{preview!r})"
        )


@dataclass
class SegmentationConfig:
    """
    Configuration for SubtitleSegmenter.

    Attributes:
        component:  Which spaCy sentence segmentation component the nlp pipeline
                    is expected to contain. Used only for the startup validation
                    warning — the segmenter reads doc.sents regardless of which
                    component is active.

                    "senter" (neural) handles sentences without explicit punctuation
                    better, which matters for subtitle text where speakers trail off.
                    "sentencizer" (rule-based) is faster and fully deterministic.

        min_chars:  Drop candidate utterances whose stripped text is shorter than
                    this many characters. Guards against artefacts like lone
                    punctuation marks that spaCy occasionally segments out.
    """
    component: Literal["sentencizer", "senter"] = "senter"
    min_chars: int = 3


# ---------------------------------------------------------------------------
# Segmenter
# ---------------------------------------------------------------------------

class SubtitleSegmenter:
    """
    Splits each MergedSubtitleWindow into CandidateUtterance objects using
    spaCy's sentence segmentation.

    The nlp pipeline must already include a sentence-segmentation component
    ("sentencizer", "senter", or "parser"). This class does not modify the
    pipeline — configure it before passing to __init__.

    Two processing modes:
      segment_window()   — single window, calls nlp(text) directly.
      segment_windows()  — batch of windows, uses nlp.pipe() for throughput.
    """

    def __init__(
        self,
        nlp: Language,
        config: Optional[SegmentationConfig] = None,
    ) -> None:
        self.nlp = nlp
        self.config = config or SegmentationConfig()
        self._validate_pipeline()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def segment_window(self, window: MergedSubtitleWindow) -> list[CandidateUtterance]:
        """
        Segment a single MergedSubtitleWindow into CandidateUtterance objects.

        Args:
            window: A merged subtitle window with non-empty text.

        Returns:
            List of CandidateUtterance objects, one per spaCy sentence that
            passes the min_chars filter. Empty list if the window text is blank.
        """
        text = window.text.strip()
        if not text:
            return []
        doc = self.nlp(text)
        return self._extract_candidates(window, doc)

    def segment_windows(
        self,
        windows: list[MergedSubtitleWindow],
        batch_size: int = 64,
    ) -> list[CandidateUtterance]:
        """
        Segment a list of MergedSubtitleWindows into CandidateUtterance objects.

        Uses nlp.pipe() for batched processing, which is significantly faster
        than calling nlp() once per window when the list is large.

        Args:
            windows:    Merged windows to segment, in temporal order.
            batch_size: Number of texts passed to nlp.pipe() at a time.
                        Tune this for your memory / throughput tradeoff.

        Returns:
            Flat list of CandidateUtterance objects preserving input order.
        """
        if not windows:
            return []

        texts = [w.text.strip() for w in windows]
        result: list[CandidateUtterance] = []

        for window, doc in zip(windows, self.nlp.pipe(texts, batch_size=batch_size)):
            if not window.text.strip():
                continue
            result.extend(self._extract_candidates(window, doc))

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_candidates(
        self,
        window: MergedSubtitleWindow,
        doc: Doc,
    ) -> list[CandidateUtterance]:
        """
        Build CandidateUtterance objects from a parsed spaCy Doc.

        Shared by segment_window() and segment_windows() so the extraction
        logic lives in exactly one place.
        """
        total_chars = len(window.text.strip())
        candidates: list[CandidateUtterance] = []

        for sent in doc.sents:
            sent_text = sent.text.strip()
            if len(sent_text) < self.config.min_chars:
                continue

            start_time, end_time = self._interpolate_times(
                window, sent.start_char, sent.end_char, total_chars
            )
            candidates.append(
                CandidateUtterance(
                    text=sent_text,
                    start_time=start_time,
                    end_time=end_time,
                    source_window=window,
                    char_start=sent.start_char,
                    char_end=sent.end_char,
                )
            )

        return candidates

    @staticmethod
    def _interpolate_times(
        window: MergedSubtitleWindow,
        char_start: int,
        char_end: int,
        total_chars: int,
    ) -> tuple[float, float]:
        """
        Estimate per-sentence times by interpolating over character position.

        For a window with duration D spanning chars 0..N, a sentence occupying
        chars [a, b] is assigned:
            start_time = window.start_time + (a / N) * D
            end_time   = window.start_time + (b / N) * D

        For a single-sentence window this returns the window's exact bounds.
        """
        if total_chars == 0:
            return window.start_time, window.end_time

        duration = window.end_time - window.start_time
        start_time = window.start_time + (char_start / total_chars) * duration
        end_time = window.start_time + (char_end / total_chars) * duration
        return start_time, end_time

    def _validate_pipeline(self) -> None:
        """
        Emit a warning at construction time if no sentence segmentation
        component is found in the pipeline.

        Does not raise — a custom component or a pipeline already iterated
        with sentences set externally would also work. The warning is a
        development aid, not a hard constraint.
        """
        active = set(self.nlp.pipe_names)
        if not active & _SENT_COMPONENTS:
            warnings.warn(
                f"No sentence segmentation component found in spaCy pipeline. "
                f"Expected one of {sorted(_SENT_COMPONENTS)}, "
                f"got: {sorted(active)}. "
                f"doc.sents will be empty at runtime.",
                UserWarning,
                stacklevel=3,
            )


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _demo() -> None:
    """
    Demonstrates single-window and multi-window segmentation.

    Uses spacy.blank("de") + sentencizer so no model download is required.
    For production, replace with spacy.load("de_core_news_md") which includes
    the neural "senter" component.
    """
    import spacy
    from subtitle_merger import SubtitleFragment

    nlp = spacy.blank("de")
    nlp.add_pipe("sentencizer")

    cfg = SegmentationConfig(component="sentencizer", min_chars=3)
    segmenter = SubtitleSegmenter(nlp, cfg)

    scenarios: list[tuple[str, MergedSubtitleWindow]] = [

        # 1. Single sentence — times should equal window bounds exactly
        (
            "Single sentence: times equal window bounds",
            MergedSubtitleWindow(
                fragments=[SubtitleFragment("Ich gehe nach Hause.", 2.0, 4.5, index=0)],
                text="Ich gehe nach Hause.",
                start_time=2.0,
                end_time=4.5,
            ),
        ),

        # 2. Two sentences — times are interpolated by character position
        (
            "Two sentences: times interpolated",
            MergedSubtitleWindow(
                fragments=[
                    SubtitleFragment("Ich liebe dieses Buch.", 5.0, 6.2, index=1),
                    SubtitleFragment("Es ist fantastisch.", 6.3, 8.0, index=2),
                ],
                text="Ich liebe dieses Buch. Es ist fantastisch.",
                start_time=5.0,
                end_time=8.0,
            ),
        ),

        # 3. Three sentences from a long merged window
        (
            "Three sentences",
            MergedSubtitleWindow(
                fragments=[
                    SubtitleFragment("Guten Morgen! Hast du gut", 10.0, 11.5, index=3),
                    SubtitleFragment("geschlafen? Ich schon.", 11.6, 13.0, index=4),
                ],
                text="Guten Morgen! Hast du gut geschlafen? Ich schon.",
                start_time=10.0,
                end_time=13.0,
            ),
        ),

        # 4. Window with a very short artefact — filtered by min_chars
        (
            "Short artefact dropped",
            MergedSubtitleWindow(
                fragments=[SubtitleFragment("Ja. Ok. Ich verstehe das jetzt.", 20.0, 22.0, index=5)],
                text="Ja. Ok. Ich verstehe das jetzt.",
                start_time=20.0,
                end_time=22.0,
            ),
        ),
    ]

    for title, window in scenarios:
        print(f"\n{'─' * 60}")
        print(f"  {title}")
        print(f"{'─' * 60}")
        print(f"  INPUT:  [{window.start_time:.2f}s–{window.end_time:.2f}s]  {window.text!r}")
        candidates = segmenter.segment_window(window)
        print(f"  OUTPUT ({len(candidates)} candidate{'s' if len(candidates) != 1 else ''}):")
        for c in candidates:
            print(f"    [{c.start_time:.2f}s–{c.end_time:.2f}s]  {c.text!r}")

    # Batch mode
    print(f"\n{'─' * 60}")
    print("  Batch mode via segment_windows()")
    print(f"{'─' * 60}")
    all_windows = [w for _, w in scenarios]
    all_candidates = segmenter.segment_windows(all_windows)
    print(f"  {len(all_windows)} windows → {len(all_candidates)} candidates total")


if __name__ == "__main__":
    _demo()
