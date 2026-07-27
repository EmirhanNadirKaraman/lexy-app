from __future__ import annotations

import uuid
from pathlib import Path
from typing import Optional

from spacy.language import Language

from app.exposure.counter import QualifiedExposureCounter
from app.exposure.models import ExposureEvent
from app.exposure.service import ExposureService
from app.extraction.extractor import UtteranceUnitExtractor
from app.learning.knowledge import KnowledgeState, UserKnowledgeStore
from app.learning.units import LearningUnit
from app.pipeline.diagnostics import PipelineRunDiagnostics
from app.pipeline.models import I1Match, PipelineConfig
from app.subtitles.ingestion import parse_srt
from app.subtitles.merging import SubtitleMerger
from app.subtitles.models import SubtitleFragment
from app.subtitles.quality import UtteranceQualityEvaluator
from app.subtitles.segmentation import SubtitleSegmenter


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
    accumulates as you call run() or record_exposure() over time.
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

    def run(
        self,
        srt_path: str | Path,
        user_id: str,
    ) -> list[I1Match]:
        """
        Run the full pipeline on an SRT file for the given user.

        Returns all i+1 matches in temporal order.  The store is NOT updated
        automatically — call record_exposure() after surfacing a match to the user.
        """
        fragments = parse_srt(srt_path)
        return self.run_fragments(fragments, user_id)

    def run_fragments(
        self,
        fragments: list[SubtitleFragment],
        user_id: str,
    ) -> list[I1Match]:
        """Run the pipeline starting from already-parsed SubtitleFragments."""
        windows = self._merger.merge_fragments(fragments)
        candidates = self._segmenter.segment_windows(
            windows, batch_size=self.config.nlp_batch_size
        )
        candidates = self._quality_filter.filter(candidates)
        extractions = self._extractor.extract_batch(
            candidates, batch_size=self.config.nlp_batch_size
        )

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

        fragments = parse_srt(srt_path)
        diag.fragments_ingested = len(fragments)

        windows = self._merger.merge_fragments(fragments)
        diag.windows_merged = len(windows)

        candidates_raw = self._segmenter.segment_windows(
            windows, batch_size=self.config.nlp_batch_size
        )
        diag.candidates_segmented = len(candidates_raw)

        self._quality_filter.reset_metrics()
        candidates = self._quality_filter.filter(candidates_raw)
        diag.candidates_accepted = len(candidates)
        diag.candidates_rejected = diag.candidates_segmented - diag.candidates_accepted
        for rule_name, rule_metrics in self._quality_filter.metrics.rules.items():
            if rule_metrics.times_rejected > 0:
                diag.rejection_by_rule[rule_name] = rule_metrics.times_rejected

        extractions = self._extractor.extract_batch(
            candidates, batch_size=self.config.nlp_batch_size
        )
        for ext in extractions:
            diag.total_units_extracted += len(ext.units)
            if not ext.units:
                diag.utterances_with_no_units += 1

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

        Providing utterance_id enables deduplication.  When omitted, a fresh
        UUID is used so every call is treated as a new unique exposure.
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
        """Bulk-mark units as known for a user (e.g. onboarding seed)."""
        self.store.seed_known_units(user_id, units, state=state)

    def get_summary(self, user_id: str) -> dict[KnowledgeState, int]:
        """Return a count of units per KnowledgeState for the given user."""
        return self.store.get_summary(user_id)
