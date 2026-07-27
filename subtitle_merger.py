"""
subtitle_merger.py
------------------
Subtitle fragment merging stage for a German language-learning pipeline.

Takes raw SubtitleFragment objects (one per SRT/VTT block) and merges
consecutive fragments into MergedSubtitleWindow objects that represent
utterance candidates, suitable for downstream spaCy sentence segmentation.

Usage:
    from subtitle_merger import (
        SubtitleFragment,
        SubtitleMergeConfig,
        SubtitleMerger,
    )

    fragments = [SubtitleFragment(...), ...]
    merger = SubtitleMerger()
    windows = merger.merge_fragments(fragments)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Module-level linguistic constants (German)
# ---------------------------------------------------------------------------

# Words that, when a fragment ends with them, signal syntactic incompleteness.
# A bare preposition, article, conjunction, or modal at the fragment boundary
# means the fragment cannot stand alone — the next fragment must follow.
_CONTINUATION_WORDS: frozenset[str] = frozenset({
    # Coordinating conjunctions
    "und", "oder", "aber", "denn", "sondern",
    # Subordinating conjunctions
    "weil", "dass", "wenn", "ob", "als", "während", "obwohl",
    "damit", "sodass", "nachdem", "bevor", "bis", "seit", "falls",
    "sofern", "solange", "sobald", "indem", "seitdem",
    # Prepositions — a bare preposition at fragment end is always incomplete
    "in", "an", "auf", "über", "unter", "vor", "hinter", "neben",
    "zwischen", "mit", "nach", "bei", "von", "zu", "aus", "durch",
    "für", "gegen", "ohne", "um", "wegen", "trotz",
    # Articles
    "der", "die", "das", "dem", "den", "des",
    "ein", "eine", "einen", "einem", "einer", "eines",
    # Auxiliary / modal verbs that commonly appear at clause-split positions
    "haben", "sein", "werden", "können", "müssen", "dürfen",
    "sollen", "wollen", "mögen",
    # Relative / question words that open a dependent clause
    "dessen", "deren", "was", "wer", "wie", "wo", "wohin",
})

# Punctuation that unambiguously ends a sentence.
_STRONG_PUNCT: frozenset[str] = frozenset({".", "!", "?", "…"})

# Punctuation that ends a clause but not a sentence (implies continuation).
_WEAK_PUNCT: frozenset[str] = frozenset({",", ";"})

# Matches a fragment whose raw text (after HTML stripping) opens with a
# dialogue-marker dash.  The \S anchor ensures there is actual content after
# the dash so bare separator lines ("---", "– ") are not matched.
_DIALOGUE_DASH_RE = re.compile(r"^\s*[-–—]\s*\S", re.UNICODE)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SubtitleFragment:
    """
    A single raw block from an SRT/VTT file. Immutable after parsing.

    Attributes:
        text:       Raw subtitle text, may include HTML tags or annotations.
        start_time: Block start time in seconds.
        end_time:   Block end time in seconds.
        index:      Original block index from the subtitle file (0-based).
    """
    text: str
    start_time: float
    end_time: float
    index: int = 0

    @property
    def duration(self) -> float:
        """Duration of this subtitle block in seconds."""
        return self.end_time - self.start_time

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def cleaned_text(self) -> str:
        """
        Return text with HTML tags, bracketed annotations, and leading
        dialogue dashes removed. Does not alter punctuation or letter case.
        """
        t = self.text
        t = re.sub(r"<[^>]+>", "", t)           # <i>, </i>, etc.
        t = re.sub(r"\[.*?\]|\(.*?\)", "", t)    # [Musik], (lacht), (Applaus)
        t = re.sub(r"^\s*[-–—]\s*", "", t)       # leading dialogue dash
        return t.strip()


@dataclass
class MergedSubtitleWindow:
    """
    A contiguous group of subtitle fragments merged into one utterance
    candidate, ready for spaCy sentence segmentation.

    Attributes:
        fragments:   The original fragments that form this window, in order.
        text:        Cleaned, joined text of all fragments.
        start_time:  Start time of the first fragment (seconds).
        end_time:    End time of the last fragment (seconds).
    """
    fragments: list[SubtitleFragment]
    text: str
    start_time: float
    end_time: float

    @property
    def duration(self) -> float:
        return self.end_time - self.start_time

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def __repr__(self) -> str:
        preview = self.text if len(self.text) <= 60 else self.text[:57] + "..."
        return (
            f"MergedSubtitleWindow("
            f"n={len(self.fragments)}, "
            f"{self.start_time:.2f}s–{self.end_time:.2f}s, "
            f"{preview!r})"
        )


@dataclass
class SubtitleMergeConfig:
    """
    Thresholds and feature toggles for SubtitleMerger.
    All times are in seconds; word counts are integer minimums.

    Tuning guide:
        max_gap_s:            Raise for slower speakers or documentary-style
                              subtitles. Lower for rapid-fire dialogue.
        tiny_gap_s:           Fragments within this gap are almost certainly
                              one burst — merge unconditionally.
        max_window_duration_s: Hard ceiling. Keeps downstream NLP from receiving
                              multi-sentence walls of text.
        min_standalone_words: 3 works well for German (a verb phrase with subject
                              is typically ≥ 3 tokens).
    """

    # ---- Timing thresholds ----

    # Fragments further apart than this are never merged.
    max_gap_s: float = 0.6

    # Gap below this is treated as "essentially no pause" — the timing alone
    # is sufficient reason to merge regardless of other signals.
    tiny_gap_s: float = 0.15

    # Hard ceiling on a merged window's total span, to prevent runaway merges.
    max_window_duration_s: float = 8.0

    # ---- Linguistic thresholds ----

    # Fragment shorter than this is too incomplete to stand alone and will be
    # merged if any timing constraint allows it.
    min_standalone_words: int = 3

    # ---- Feature toggles (disable individually for ablation / debugging) ----
    merge_on_lowercase_continuation: bool = True
    merge_on_weak_punctuation: bool = True
    merge_on_continuation_word: bool = True
    merge_on_short_fragment: bool = True
    merge_on_hyphen_break: bool = True

    # ---- Multi-speaker safeguards ----

    # Veto a merge when the *next* fragment's raw text starts with a dialogue
    # marker dash (-, –, —).  This is the most explicit speaker-change signal:
    # the subtitle encoder has explicitly labelled the fragment as a new turn.
    # Fires before the unconditional tiny-gap merge so it always takes effect.
    guard_dialogue_dash: bool = True

    # Extend the sentence-boundary veto to cover tiny gaps when the *current*
    # fragment looks like a complete short speaker turn.  Normally the veto
    # only fires for gaps > tiny_gap_s; with this guard it also fires at tiny
    # gaps when the current fragment ends with strong punctuation and is short
    # (≤ max_complete_turn_words words).  This catches rapid back-and-forth
    # exchanges like "Wirklich? / Ja, natürlich." that arrive with near-zero
    # inter-fragment gaps yet clearly belong to different speakers.
    guard_short_complete_turn: bool = True

    # Word-count ceiling used by guard_short_complete_turn.  Utterances longer
    # than this are unlikely to be rapid single-turn responses and are left to
    # the standard boundary detection logic.
    max_complete_turn_words: int = 6


# ---------------------------------------------------------------------------
# Merger
# ---------------------------------------------------------------------------

class SubtitleMerger:
    """
    Merges consecutive SubtitleFragment objects into MergedSubtitleWindow
    objects using timing and German-specific linguistic heuristics.

    Each heuristic lives in its own method so it can be:
      - unit-tested in isolation
      - overridden in a subclass
      - toggled via SubtitleMergeConfig

    Decision flow in _should_merge():
      1. Hard veto: gap > max_gap_s → never merge
      2. Hard veto: looks like a sentence boundary (strong punct + uppercase
                   start + gap > tiny_gap_s) → never merge
      3. Unconditional merge: gap ≤ tiny_gap_s (timing artifact)
      4. Soft signals: any enabled heuristic firing → merge
    """

    def __init__(self, config: Optional[SubtitleMergeConfig] = None) -> None:
        self.config = config or SubtitleMergeConfig()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def merge_fragments(
        self,
        fragments: list[SubtitleFragment],
    ) -> list[MergedSubtitleWindow]:
        """
        Merge a list of subtitle fragments into utterance windows.

        Fragments are processed left-to-right. The current accumulation group
        is flushed to a MergedSubtitleWindow whenever _should_merge() returns
        False or adding the next fragment would exceed max_window_duration_s.

        Args:
            fragments: Raw subtitle fragments sorted by start_time.

        Returns:
            List of MergedSubtitleWindow objects in temporal order.
        """
        if not fragments:
            return []

        windows: list[MergedSubtitleWindow] = []
        current_group: list[SubtitleFragment] = [fragments[0]]

        for nxt in fragments[1:]:
            cur = current_group[-1]
            projected_duration = nxt.end_time - current_group[0].start_time

            if (
                projected_duration <= self.config.max_window_duration_s
                and self._should_merge(cur, nxt)
            ):
                current_group.append(nxt)
            else:
                windows.append(self._build_window(current_group))
                current_group = [nxt]

        windows.append(self._build_window(current_group))
        return windows

    # ------------------------------------------------------------------
    # Merge decision
    # ------------------------------------------------------------------

    def _should_merge(
        self,
        current: SubtitleFragment,
        nxt: SubtitleFragment,
    ) -> bool:
        """
        Return True if `current` and `nxt` should belong to the same window.

        Hard constraints are evaluated first and can veto unconditionally.
        If no veto applies, a tiny gap merges unconditionally, and otherwise
        any single soft signal is enough to merge.
        """
        gap = nxt.start_time - current.end_time

        # --- Hard veto 1: gap exceeds maximum ---
        if gap > self.config.max_gap_s:
            return False

        # --- Hard veto 2: explicit dialogue-dash speaker marker ---
        # Checked before the unconditional tiny-gap merge so that an explicit
        # speaker marker always wins, even when fragments are back-to-back.
        if (
            self.config.guard_dialogue_dash
            and self._next_starts_with_dialogue_dash(nxt)
        ):
            return False

        # --- Hard veto 3: sentence boundary ---
        # Fires when the current fragment ends a sentence and the next opens
        # one.  Two sub-conditions trigger it:
        #   a) Standard: any gap above the timing-noise threshold (tiny_gap_s).
        #   b) Extended: tiny gap but current is a short complete turn —
        #      rapid back-and-forth exchanges can have near-zero gaps yet
        #      still be different speakers.
        if (
            self._ends_with_strong_punctuation(current)
            and self._starts_with_uppercase(nxt)
        ):
            if gap > self.config.tiny_gap_s:
                return False
            if (
                self.config.guard_short_complete_turn
                and current.word_count <= self.config.max_complete_turn_words
            ):
                return False

        # --- Unconditional merge: essentially zero gap ---
        # Only reached when neither sentence-boundary veto fired.
        if gap <= self.config.tiny_gap_s:
            return True

        # --- Soft signals: first True wins ---
        signals: list[bool] = [
            self.config.merge_on_weak_punctuation
                and self._ends_with_weak_punctuation(current),
            self.config.merge_on_continuation_word
                and self._ends_with_continuation_word(current),
            self.config.merge_on_lowercase_continuation
                and self._starts_with_lowercase(nxt),
            self.config.merge_on_short_fragment
                and self._is_too_short(current),
            self.config.merge_on_hyphen_break
                and self._ends_with_hyphen(current),
        ]
        return any(signals)

    # ------------------------------------------------------------------
    # Individual heuristics
    # ------------------------------------------------------------------

    def _ends_with_strong_punctuation(self, frag: SubtitleFragment) -> bool:
        """
        Return True if the fragment ends with sentence-final punctuation
        (. ! ? …). These are reliable signals that a thought is complete.
        """
        cleaned = frag.cleaned_text().rstrip()
        return bool(cleaned) and cleaned[-1] in _STRONG_PUNCT

    def _ends_with_weak_punctuation(self, frag: SubtitleFragment) -> bool:
        """
        Return True if the fragment ends with clause-level punctuation
        (, ;) that implies the sentence continues in the next fragment.
        """
        cleaned = frag.cleaned_text().rstrip()
        return bool(cleaned) and cleaned[-1] in _WEAK_PUNCT

    def _ends_with_continuation_word(self, frag: SubtitleFragment) -> bool:
        """
        Return True if the last word of the fragment is a conjunction,
        preposition, article, or auxiliary that syntactically requires a
        following constituent (i.e. the fragment cannot be complete as-is).

        Trailing punctuation on the last token is stripped before lookup
        so "mit," and "mit" both resolve correctly.
        """
        cleaned = frag.cleaned_text()
        tokens = cleaned.lower().split()
        if not tokens:
            return False
        last_token = re.sub(r"[^\w]", "", tokens[-1], flags=re.UNICODE)
        return last_token in _CONTINUATION_WORDS

    def _starts_with_lowercase(self, frag: SubtitleFragment) -> bool:
        """
        Return True if the fragment begins with a lowercase letter.

        In German, almost every sentence-initial word is capitalised (proper
        nouns, nouns, sentence-starting adjectives). A lowercase start is
        therefore a strong indicator that this fragment continues the previous
        one rather than opening a new sentence.
        """
        cleaned = frag.cleaned_text().lstrip()
        return bool(cleaned) and cleaned[0].islower()

    def _starts_with_uppercase(self, frag: SubtitleFragment) -> bool:
        """Return True if the fragment begins with an uppercase letter."""
        cleaned = frag.cleaned_text().lstrip()
        return bool(cleaned) and cleaned[0].isupper()

    def _is_too_short(self, frag: SubtitleFragment) -> bool:
        """
        Return True if the fragment contains fewer words than
        min_standalone_words, meaning it is unlikely to be a complete
        utterance on its own and should be merged with what follows.
        """
        return frag.word_count < self.config.min_standalone_words

    def _ends_with_hyphen(self, frag: SubtitleFragment) -> bool:
        """
        Return True if the fragment ends with a hyphen, indicating a word
        has been split across two consecutive subtitle blocks. The hyphen
        is stripped and the two halves are concatenated without a space
        during window construction (see _join_texts).
        """
        return frag.cleaned_text().rstrip().endswith("-")

    def _next_starts_with_dialogue_dash(self, nxt: SubtitleFragment) -> bool:
        """
        Return True if the next fragment's raw text begins with a
        dialogue-marker dash (-, –, —) followed by non-whitespace content.

        The check is performed on raw text (not cleaned_text) because the
        cleaning step strips these markers.  HTML tags are removed first
        since some encoders wrap entire lines as ``<i>- Text</i>``.

        A bare separator line such as "---" or "– " does not match because
        _DIALOGUE_DASH_RE requires at least one non-whitespace character
        after the dash.
        """
        raw = re.sub(r"<[^>]+>", "", nxt.text)
        return bool(_DIALOGUE_DASH_RE.match(raw))

    # ------------------------------------------------------------------
    # Window construction
    # ------------------------------------------------------------------

    def _build_window(self, group: list[SubtitleFragment]) -> MergedSubtitleWindow:
        """
        Construct a MergedSubtitleWindow from an accumulated group of
        fragments. Timings span the whole group; text is cleaned and joined.
        """
        cleaned_texts = [f.cleaned_text() for f in group]
        merged_text = self._join_texts(cleaned_texts)
        return MergedSubtitleWindow(
            fragments=list(group),
            text=merged_text,
            start_time=group[0].start_time,
            end_time=group[-1].end_time,
        )

    @staticmethod
    def _join_texts(texts: list[str]) -> str:
        """
        Join cleaned fragment texts into a single string.

        Hyphenated word breaks (fragment ending with '-') are concatenated
        directly without inserting a space, so "Lieblings-" + "film" becomes
        "Lieblingsfilm". All other joins use a single space.
        """
        if not texts:
            return ""
        result = texts[0]
        for text in texts[1:]:
            if result.endswith("-"):
                result = result[:-1] + text
            else:
                result = result + " " + text
        return result.strip()


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _make(index: int, text: str, start: float, end: float) -> SubtitleFragment:
    return SubtitleFragment(text=text, start_time=start, end_time=end, index=index)


def _demo() -> None:
    """
    Demonstrates several real-world subtitle merging scenarios.

    Each scenario prints the input fragments and the resulting windows.
    """

    scenarios: list[tuple[str, list[SubtitleFragment]]] = [

        # 1. Simple continuation: small gap, lowercase start
        ("Lowercase continuation across small gap", [
            _make(0, "Ich wollte dir nur sagen,", 1.00, 2.40),
            _make(1, "dass ich morgen nicht kommen kann.", 2.55, 4.10),
        ]),

        # 2. Fragment ends with preposition → definite incomplete fragment
        ("Ends with preposition (continuation word)", [
            _make(0, "Wir treffen uns", 5.00, 6.20),
            _make(1, "vor dem", 6.30, 6.90),
            _make(2, "Bahnhof.", 7.00, 7.80),
        ]),

        # 3. Comma end → clause continues
        ("Comma-ended fragment merged with following clause", [
            _make(0, "Wenn du möchtest,", 10.00, 11.50),
            _make(1, "können wir das morgen besprechen.", 11.65, 13.20),
        ]),

        # 4. Short fragment merged regardless of punctuation
        ("Short fragment (2 words) merged", [
            _make(0, "Ich auch.", 15.00, 15.80),
            _make(1, "Wirklich?", 15.90, 16.50),
            _make(2, "Ja, natürlich.", 16.60, 17.50),
        ]),

        # 5. Hyphenated word break across two blocks
        ("Hyphen break across subtitle blocks", [
            _make(0, "Das ist mein absoluter Lieblings-", 20.00, 21.40),
            _make(1, "film aller Zeiten.", 21.42, 22.80),
        ]),

        # 6. Clear sentence boundary: large gap, strong punctuation, uppercase
        ("Sentence boundary respected: gap + strong punct + uppercase", [
            _make(0, "Das war fantastisch.", 30.00, 31.50),
            _make(1, "Jetzt gehen wir nach Hause.", 33.20, 35.00),
        ]),

        # 7. Realistic multi-fragment scene with mixed signals
        ("Mixed: some merges, some breaks", [
            _make(0, "Guten Morgen!", 40.00, 40.90),
            _make(1, "Hast du schon", 41.00, 41.80),
            _make(2, "gefrühstückt?", 41.85, 42.70),
            _make(3, "Nein, noch nicht.", 43.50, 44.60),
            _make(4, "Ich warte auf", 44.70, 45.40),
            _make(5, "dich.", 45.45, 45.80),
        ]),

        # 8. Explicit dialogue dashes — hard veto even at tiny gap
        ("Dialogue dashes → each turn stays separate", [
            _make(0, "- Das war wirklich gut.", 50.00, 51.20),
            _make(1, "- Wirklich?", 51.25, 51.80),
            _make(2, "- Ja, total.", 51.85, 52.60),
        ]),

        # 9. No dashes, but short complete turns fire the guard
        ("Short complete turns — guard splits without dashes", [
            _make(0, "Ich auch.", 55.00, 55.80),
            _make(1, "Wirklich?", 55.90, 56.50),
            _make(2, "Ja, natürlich.", 56.60, 57.50),
        ]),
    ]

    merger = SubtitleMerger()

    for title, fragments in scenarios:
        print(f"\n{'─' * 60}")
        print(f"  {title}")
        print(f"{'─' * 60}")
        print("  INPUT:")
        for f in fragments:
            print(f"    [{f.start_time:.2f}s–{f.end_time:.2f}s]  {f.text!r}")
        windows = merger.merge_fragments(fragments)
        print(f"  OUTPUT ({len(windows)} window{'s' if len(windows) != 1 else ''}):")
        for w in windows:
            frag_indices = [str(f.index) for f in w.fragments]
            print(f"    [{w.start_time:.2f}s–{w.end_time:.2f}s] (frags {','.join(frag_indices)})  {w.text!r}")


if __name__ == "__main__":
    _demo()
