"""
pipeline.py
-----------
End-to-end German subtitle → i+1 utterance pipeline.

Wires all processing stages in order:

  SRT file
    │
    ▼  parse_srt()
  list[SubtitleFragment]
    │
    ▼  SubtitleMerger
  list[MergedSubtitleWindow]
    │
    ▼  SubtitleSegmenter
  list[CandidateUtterance]
    │
    ▼  UtteranceQualityEvaluator  (filter)
  list[CandidateUtterance]
    │
    ▼  UtteranceUnitExtractor     (batch)
  list[UtteranceExtractionResult]
    │
    ▼  UserKnowledgeStore.find_sole_unknown()
  list[I1Match]

The result type, I1Match, pairs each i+1 utterance with the single unit
the user does not yet know — exactly the information needed to choose what
to show next and to update the user's knowledge record after exposure.

Typical usage
-------------
    import spacy
    from pipeline import GermanSubtitlePipeline, PipelineConfig

    nlp = spacy.load("de_core_news_md")
    pipeline = GermanSubtitlePipeline(nlp)

    # Optionally seed known vocabulary for a new user
    from user_knowledge import KnowledgeState
    pipeline.seed_known_vocabulary(user_id, high_freq_lemmas, state=KnowledgeState.KNOWN_PASSIVE)

    matches = pipeline.run(srt_path, user_id)
    for m in matches:
        print(f"[{m.utterance.start_time:.1f}s] {m.utterance.text!r}  →  {m.target_unit.key!r}")

    # After surfacing utterance to user:
    pipeline.record_exposure(user_id, m.target_unit)

SRT parsing
-----------
The built-in parse_srt() handles standard SRT files.  SRT timestamps are
expected in the format:  00:00:01,234 --> 00:00:03,456
Embedded HTML tags (<i>, <b>, <font ...>) are stripped by SubtitleFragment.
"""
from __future__ import annotations

import hashlib
import re
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from spacy.language import Language

from exposure_counter import CountingPolicy, ExposureEvent, QualifiedExposureCounter
from pipeline_diagnostics import PipelineRunDiagnostics
from exposure_service import ExposureService
from learning_units import LearningUnit
from subtitle_cleaner import SubtitleTextCleaner
from subtitle_merger import SubtitleFragment, SubtitleMergeConfig, SubtitleMerger
from subtitle_segmenter import CandidateUtterance, SegmentationConfig, SubtitleSegmenter
from utterance_quality_filter import QualityFilterConfig, UtteranceQualityEvaluator
from utterance_unit_extractor import UnitExtractionConfig, UtteranceExtractionResult, UtteranceUnitExtractor
from user_knowledge import ExposurePolicy, KnowledgeFilterPolicy, KnowledgeState, UserKnowledgeStore


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class I1Match:
    """
    A single i+1 match: an utterance the user can learn from right now.

    Attributes:
        utterance:      The i+1 candidate utterance.
        target_unit:    The one unit in this utterance the user does not yet know.
        extraction:     Full extraction result (token_units, skipped_count, etc.)
                        for UI highlighting and debugging.
    """
    utterance: CandidateUtterance
    target_unit: LearningUnit
    extraction: UtteranceExtractionResult

    @property
    def utterance_id(self) -> str:
        """
        Stable identifier derived from utterance text and timing.

        Use this as the utterance_id argument when calling record_exposure()
        to enable deduplication across repeated surfacings of the same clip.
        """
        raw = f"{self.utterance.text}|{self.utterance.start_time:.3f}|{self.utterance.end_time:.3f}"
        return hashlib.md5(raw.encode()).hexdigest()[:16]

    def __repr__(self) -> str:
        return (
            f"I1Match("
            f"[{self.utterance.start_time:.2f}s–{self.utterance.end_time:.2f}s] "
            f"{self.utterance.text!r} → {self.target_unit.key!r})"
        )


# ---------------------------------------------------------------------------
# Pipeline configuration
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    """
    Aggregate configuration for all pipeline stages.

    All fields have sensible defaults — only override what you need.

    Attributes:
        merge:      SubtitleMergeConfig for the merging stage.
        segment:    SegmentationConfig for the segmentation stage.
        quality:    QualityFilterConfig for the quality filter stage.
        extract:    UnitExtractionConfig for the unit extraction stage.
        filter_policy:   KnowledgeFilterPolicy for i+1 state threshold.
        exposure_policy: ExposurePolicy for auto-advance on record_exposure().
        nlp_batch_size:  Batch size passed to nlp.pipe() calls.
    """
    merge: SubtitleMergeConfig = field(default_factory=SubtitleMergeConfig)
    segment: SegmentationConfig = field(default_factory=SegmentationConfig)
    quality: QualityFilterConfig = field(default_factory=QualityFilterConfig)
    extract: UnitExtractionConfig = field(default_factory=UnitExtractionConfig)
    filter_policy: KnowledgeFilterPolicy = field(default_factory=KnowledgeFilterPolicy)
    exposure_policy: ExposurePolicy = field(default_factory=ExposurePolicy)
    counting: CountingPolicy = field(default_factory=CountingPolicy)
    nlp_batch_size: int = 64


# ---------------------------------------------------------------------------
# SRT parser — helpers
# ---------------------------------------------------------------------------

_SRT_TIMESTAMP = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
    r"\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)

# Encoding preference order for subtitle files.
# utf-8-sig handles the Windows BOM; cp1252 covers German umlauts from
# Windows-origin files; latin-1 is the last resort (never raises a decode
# error because it maps all 256 byte values).
_SUBTITLE_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def _parse_timestamp(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _read_subtitle_file(path: Path) -> str:
    """
    Read a subtitle file, trying common encodings in order.

    Encoding priority:
        utf-8-sig  — UTF-8 with BOM (Windows Notepad saves this by default)
        utf-8      — plain UTF-8 (most modern encoders)
        cp1252     — Windows-1252 (very common for German subtitles from
                     legacy Windows tools; handles €, „, " and other
                     Windows-specific code points correctly)
        latin-1    — ISO-8859-1 (always succeeds; last resort)

    latin-1 maps all 256 byte values and therefore never raises
    UnicodeDecodeError.  The fallback chain always produces a result,
    though cp1252 handles the 0x80–0x9F range more accurately than latin-1
    for Windows-origin files.

    Args:
        path: Path to the subtitle file.

    Returns:
        File contents as a str.

    Raises:
        FileNotFoundError: if *path* does not exist (not caught here).
    """
    for encoding in _SUBTITLE_ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    # Unreachable in practice because latin-1 never raises, but keeps
    # the type checker and any future encoding list changes safe.
    return path.read_text(encoding="latin-1", errors="replace")


def parse_srt(
    path: str | Path,
    cleaner: Optional[SubtitleTextCleaner] = None,
) -> list[SubtitleFragment]:
    """
    Parse an SRT file into a list of SubtitleFragment objects.

    Handles:
      - Standard SRT timestamps: 00:00:01,234 --> 00:00:03,456
      - WebVTT-style dots:        00:00:01.234 --> 00:00:03.456
      - Windows (CRLF) and Unix (LF) line endings
      - Multi-line subtitle blocks (lines joined with a space)
      - Encoding fallback: utf-8-sig → utf-8 → cp1252 → latin-1
      - ASS/SSA styling tags, VTT timestamp cues, HTML entities,
        non-standard whitespace, stray SRT timestamps in body text
        (all cleaned via SubtitleTextCleaner)
      - Fragments that reduce to no alphabetic content after cleaning
        are silently dropped (e.g. "{\\an8}", "♪ ♪")

    Returns fragments in file order (index = 0-based over kept blocks).

    Args:
        path:    Path to the SRT or WebVTT file.
        cleaner: Optional pre-configured SubtitleTextCleaner.  A default
                 instance is created when omitted.

    Raises:
        FileNotFoundError: if *path* does not exist.
        ValueError:        if no valid SRT blocks are found after cleaning.
    """
    raw_text = _read_subtitle_file(Path(path))
    blocks = re.split(r"\r?\n\r?\n", raw_text.strip())

    if cleaner is None:
        cleaner = SubtitleTextCleaner()

    fragments: list[SubtitleFragment] = []

    for raw_block in blocks:
        lines = [ln.rstrip() for ln in raw_block.splitlines()]
        # Drop the optional sequence number line (first line, all digits)
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            continue

        # Timestamp line
        m = _SRT_TIMESTAMP.match(lines[0].strip())
        if not m:
            continue

        start = _parse_timestamp(*m.group(1, 2, 3, 4))
        end = _parse_timestamp(*m.group(5, 6, 7, 8))

        # Remaining lines are the subtitle text; join then clean.
        raw_content = " ".join(ln for ln in lines[1:] if ln.strip())
        if not raw_content:
            continue

        content = cleaner.clean(raw_content)

        # Drop fragments that cleaned down to pure noise (no letters).
        if not SubtitleTextCleaner.has_alphabetic_content(content):
            continue

        fragments.append(SubtitleFragment(
            text=content,
            start_time=start,
            end_time=end,
            index=len(fragments),
        ))

    if not fragments:
        raise ValueError(f"No valid SRT blocks found in {path!r}")

    return fragments


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class GermanSubtitlePipeline:
    """
    End-to-end pipeline from SRT file to i+1 utterance matches.

    Stages:
      1. parse_srt()                     → list[SubtitleFragment]
      2. SubtitleMerger                  → list[MergedSubtitleWindow]
      3. SubtitleSegmenter               → list[CandidateUtterance]
      4. UtteranceQualityEvaluator       → list[CandidateUtterance]  (filtered)
      5. UtteranceUnitExtractor          → list[UtteranceExtractionResult]
      6. UserKnowledgeStore              → list[I1Match]

    The UserKnowledgeStore is shared across run() calls, so knowledge state
    accumulates as you call run() or record_exposure() over time.  Reset a
    user with store.reset_user(user_id) if needed.
    """

    def __init__(
        self,
        nlp: Language,
        config: Optional[PipelineConfig] = None,
    ) -> None:
        self.nlp = nlp
        self.config = config or PipelineConfig()

        self._merger = SubtitleMerger(self.config.merge)
        self._segmenter = SubtitleSegmenter(nlp, self.config.segment)
        self._quality_filter = UtteranceQualityEvaluator(self.config.quality)
        self._extractor = UtteranceUnitExtractor(nlp, self.config.extract)
        self.store = UserKnowledgeStore(
            filter_policy=self.config.filter_policy,
            exposure_policy=self.config.exposure_policy,
        )
        _counter = QualifiedExposureCounter(self.config.counting)
        self.exposure_service = ExposureService(_counter, self.store)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(
        self,
        srt_path: str | Path,
        user_id: str,
    ) -> list[I1Match]:
        """
        Run the full pipeline on an SRT file for the given user.

        Returns all i+1 matches in temporal order.  The store is NOT updated
        automatically — call record_exposure() after surfacing a match to
        the user.

        Args:
            srt_path: Path to the SRT subtitle file.
            user_id:  Opaque user identifier.  State is tracked per user.

        Returns:
            List of I1Match objects, one per i+1-eligible utterance.
        """
        fragments = parse_srt(srt_path)
        return self.run_fragments(fragments, user_id)

    def run_fragments(
        self,
        fragments: list[SubtitleFragment],
        user_id: str,
    ) -> list[I1Match]:
        """
        Run the pipeline starting from already-parsed SubtitleFragments.

        Useful when you have fragments from a non-SRT source (e.g. WebVTT,
        or fragments already held in memory).
        """
        # Stage 1 — merge
        windows = self._merger.merge_fragments(fragments)

        # Stage 2 — segment
        candidates = self._segmenter.segment_windows(
            windows, batch_size=self.config.nlp_batch_size
        )

        # Stage 3 — quality filter
        candidates = self._quality_filter.filter(candidates)

        # Stage 4 — extract units (batch for throughput)
        extractions = self._extractor.extract_batch(
            candidates, batch_size=self.config.nlp_batch_size
        )

        # Stage 5 — i+1 filter
        matches: list[I1Match] = []
        for extraction in extractions:
            target = self.store.find_sole_unknown(user_id, extraction.units)
            if target is not None:
                matches.append(I1Match(
                    utterance=extraction.utterance,
                    target_unit=target,
                    extraction=extraction,
                ))

        return matches

    def run_with_diagnostics(
        self,
        srt_path: str | Path,
        user_id: str,
        record_exposures: bool = False,
    ) -> tuple[list[I1Match], PipelineRunDiagnostics]:
        """
        Run the full pipeline and return both matches and a diagnostics snapshot.

        Identical to run() in output, but also collects per-stage counters,
        quality-filter rejection breakdowns, i+1 eligibility statistics, and
        optional exposure metrics — all in a single pass.

        The quality filter's internal metrics are reset at the start of each
        call so rejection_by_rule reflects exactly this run.

        Args:
            srt_path:         Path to the SRT subtitle file.
            user_id:          Opaque user identifier.
            record_exposures: When True, call record_exposure() for each match
                              and populate diag.exposures_recorded /
                              diag.exposures_deduplicated.

        Returns:
            (matches, diag) — i+1 matches in temporal order plus diagnostics.
        """
        diag = PipelineRunDiagnostics()

        # Stage 1 — parse
        fragments = parse_srt(srt_path)
        diag.fragments_ingested = len(fragments)

        # Stage 2 — merge
        windows = self._merger.merge_fragments(fragments)
        diag.windows_merged = len(windows)

        # Stage 3 — segment
        candidates_raw = self._segmenter.segment_windows(
            windows, batch_size=self.config.nlp_batch_size
        )
        diag.candidates_segmented = len(candidates_raw)

        # Stage 4 — quality filter (reset for per-run counts)
        self._quality_filter.reset_metrics()
        candidates = self._quality_filter.filter(candidates_raw)
        diag.candidates_accepted = len(candidates)
        diag.candidates_rejected = diag.candidates_segmented - diag.candidates_accepted
        for rule_name, rule_metrics in self._quality_filter.metrics.rules.items():
            if rule_metrics.times_rejected > 0:
                diag.rejection_by_rule[rule_name] = rule_metrics.times_rejected

        # Stage 5 — extract units
        extractions = self._extractor.extract_batch(
            candidates, batch_size=self.config.nlp_batch_size
        )
        for ext in extractions:
            diag.total_units_extracted += len(ext.units)
            if not ext.units:
                diag.utterances_with_no_units += 1

        # Stage 6 — i+1 filter with blocker tracking
        matches: list[I1Match] = []
        for extraction in extractions:
            if not extraction.units:
                diag.ineligible_no_units += 1
                continue
            unknowns = self.store.unknown_units(user_id, extraction.units)
            n_unknown = len(unknowns)
            if n_unknown == 1:
                target = unknowns[0]
                match = I1Match(
                    utterance=extraction.utterance,
                    target_unit=target,
                    extraction=extraction,
                )
                matches.append(match)
                diag.eligible_utterances += 1
                if record_exposures:
                    event = self.record_exposure(
                        user_id=user_id,
                        unit=target,
                        utterance_id=match.utterance_id,
                    )
                    if event is not None:
                        diag.exposures_recorded += 1
                    else:
                        diag.exposures_deduplicated += 1
            elif n_unknown == 0:
                diag.ineligible_all_known += 1
            else:
                diag.ineligible_too_many_unknowns += 1
                for u in unknowns:
                    diag.blocking_unit_counts[u.key] = (
                        diag.blocking_unit_counts.get(u.key, 0) + 1
                    )

        return matches, diag

    # ------------------------------------------------------------------
    # Convenience wrappers that delegate to the store
    # ------------------------------------------------------------------

    def record_exposure(
        self,
        user_id: str,
        unit: LearningUnit,
        utterance_id: Optional[str] = None,
        session_id: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> Optional[ExposureEvent]:
        """
        Record that the user was shown an i+1 utterance with `unit` as target.

        Delegates to ExposureService, which updates both the exposure counter
        and the knowledge store.  Returns None (duplicate rejected) or the
        recorded ExposureEvent.

        Providing utterance_id enables deduplication: replaying the same
        subtitle clip will not inflate the exposure count.  Callers with an
        I1Match should pass match.utterance_id.  When utterance_id is omitted
        a fresh UUID is used, so every call is treated as a new unique exposure
        regardless of deduplication policy.

        Args:
            user_id:      The user who was shown the utterance.
            unit:         The acquisition target unit (sole unknown).
            utterance_id: Stable utterance identifier for deduplication.
                          Pass I1Match.utterance_id for pipeline matches.
                          Defaults to a random UUID (no deduplication).
            session_id:   Optional session scope for DEDUPLICATE_SESSION policy.
            source_id:    Optional source material identifier.

        Returns:
            ExposureEvent if accepted, None if rejected as a duplicate.
        """
        if utterance_id is None:
            utterance_id = str(uuid.uuid4())
        return self.exposure_service.record_qualified_exposure(
            user_id=user_id,
            unit=unit,
            utterance_id=utterance_id,
            session_id=session_id,
            source_id=source_id,
        )

    def seed_known_vocabulary(
        self,
        user_id: str,
        units: list[LearningUnit],
        state: KnowledgeState = KnowledgeState.KNOWN_PASSIVE,
    ) -> None:
        """
        Bulk-mark units as known for a user (e.g. onboarding seed).

        Without a seed, a new user has zero known units and the i+1 filter
        cannot fire — every utterance has multiple unknowns.  Seed with the
        ~200 most frequent German lemmas to bootstrap the pipeline.
        """
        self.store.seed_known_units(user_id, units, state=state)

    def get_summary(self, user_id: str) -> dict[KnowledgeState, int]:
        """Return a count of units per KnowledgeState for the given user."""
        return self.store.get_summary(user_id)



# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _demo() -> None:
    """
    Demonstrates the full pipeline using a small in-memory SRT snippet.

    Falls back to a blank spaCy model when de_core_news_md is not installed,
    which disables lemmatisation and separable verb detection but shows the
    pipeline structure end-to-end.
    """
    import tempfile

    try:
        import spacy
        nlp = spacy.load("de_core_news_md")
        print("  Using de_core_news_md (full lemmatisation + dependency parsing).\n")
    except OSError:
        import spacy
        nlp = spacy.blank("de")
        nlp.add_pipe("sentencizer")
        warnings.warn(
            "de_core_news_md not found — using blank model. "
            "Install with: python -m spacy download de_core_news_md",
            stacklevel=1,
        )
        print("  Using blank model — surface forms only.\n")

    # ------------------------------------------------------------------
    # Minimal SRT content
    # ------------------------------------------------------------------
    srt_content = """\
1
00:00:01,000 --> 00:00:03,200
Ich gehe heute ins Kino.

2
00:00:03,800 --> 00:00:05,500
Der Film ist wirklich fantastisch.

3
00:00:06,000 --> 00:00:07,200
Ja.

4
00:00:07,500 --> 00:00:10,100
Danach kaufen wir noch Popcorn.

5
00:00:11,000 --> 00:00:13,000
Das Popcorn kostet leider viel.

6
00:00:13,500 --> 00:00:15,000
Macht nichts, das lohnt sich.
"""

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".srt", encoding="utf-8", delete=False
    ) as f:
        f.write(srt_content)
        srt_path = f.name

    # ------------------------------------------------------------------
    # Build pipeline and seed user
    # ------------------------------------------------------------------
    pipeline = GermanSubtitlePipeline(nlp)
    USER = "demo_user"

    from learning_units import LearningUnit, LearningUnitType

    def lemma(k: str) -> LearningUnit:
        return LearningUnit(LearningUnitType.LEMMA, k, k)

    # Seed A1-level words as KNOWN_PASSIVE so the i+1 filter can fire.
    # The four words at the end are tuned so each demo sentence has exactly
    # one unknown, giving several i+1 hits even in this short clip.
    seed_words = [lemma(k) for k in [
        "ich", "du", "er", "sie", "wir", "ihr",
        "sein", "haben", "werden", "können", "müssen", "wollen",
        "der", "die", "das", "ein", "eine",
        "und", "oder", "aber", "weil", "dass", "wenn",
        "in", "mit", "von", "zu", "auf", "an", "für",
        "nicht", "auch", "noch", "schon", "sehr", "gut",
        "heute", "dann", "ja", "nein",
        "gehen", "kaufen", "kosten", "machen",
        "kino", "film", "geld",
        # calibration: known words to isolate one unknown per demo sentence
        "wirklich", "danach", "leider", "macht",
    ]]
    pipeline.seed_known_vocabulary(USER, seed_words)

    # ------------------------------------------------------------------
    # Diagnose
    # ------------------------------------------------------------------
    _, diag = pipeline.run_with_diagnostics(srt_path, USER)
    print(diag.format_summary(title="PIPELINE DIAGNOSTIC", source=srt_path, user=USER))

    # ------------------------------------------------------------------
    # Full run
    # ------------------------------------------------------------------
    print("\n" + "─" * 62)
    print("  i+1 MATCHES")
    print("─" * 62)

    matches = pipeline.run(srt_path, USER)

    if not matches:
        print(
            "\n  (no i+1 matches — with blank model, inflected surface forms like 'gehe'"
            "\n   don't match seeded lemmas like 'gehen'. Install de_core_news_md for"
            "\n   proper lemmatisation:  python -m spacy download de_core_news_md)"
        )
    else:
        for m in matches:
            units_known = [u.key for u in m.extraction.units if pipeline.store.is_known(USER, u)]
            print(f"\n  [{m.utterance.start_time:.1f}s–{m.utterance.end_time:.1f}s]")
            print(f"  Text   : {m.utterance.text!r}")
            print(f"  Target : {m.target_unit.key!r}  (the one unknown)")
            print(f"  Known  : {units_known}")

    # ------------------------------------------------------------------
    # Record exposure for the first match and show state change
    # ------------------------------------------------------------------
    if matches:
        first = matches[0]
        print("\n" + "─" * 62)
        print("  EXPOSURE RECORDING")
        print("─" * 62)
        state_before = pipeline.store.get_state(USER, first.target_unit)
        pipeline.record_exposure(USER, first.target_unit, utterance_id=first.utterance_id)
        state_after = pipeline.store.get_state(USER, first.target_unit)
        print(f"\n  Unit   : {first.target_unit.key!r}")
        print(f"  Before : {state_before.name}")
        print(f"  After  : {state_after.name}")

    # ------------------------------------------------------------------
    # Knowledge summary
    # ------------------------------------------------------------------
    print("\n" + "─" * 62)
    print("  USER KNOWLEDGE SUMMARY")
    print("─" * 62)
    summary = pipeline.get_summary(USER)
    print()
    for state, count in summary.items():
        if count > 0:
            bar = "█" * min(count, 40)
            print(f"  {state.label():<20} {bar}  ({count})")

    # Cleanup temp file
    import os
    os.unlink(srt_path)


if __name__ == "__main__":
    _demo()
