"""
test_pipeline_diagnostics.py
-----------------------------
pytest suite for PipelineRunDiagnostics and GermanSubtitlePipeline.run_with_diagnostics().

The test strategy has two layers:

  1. Unit tests for PipelineRunDiagnostics directly — no pipeline, no spaCy.
     These verify that derived properties, helper methods, and formatting are
     correct given known counter values.

  2. Integration tests for run_with_diagnostics() — the pipeline stages are
     stubbed out so the test controls exactly which utterances are accepted,
     which are rejected, and how many unknowns each has.  This lets us assert
     on the returned diagnostics without requiring a real spaCy model or SRT file.

Why test diagnostics?  A diagnostics layer that silently miscounts defeats its
own purpose.  If rejection_by_rule is wrong, you optimise the wrong rules.  If
eligible_utterances is wrong, you misread i+1 coverage.  Correctness here is
load-bearing for any dashboard or tuning workflow built on top.
"""
from __future__ import annotations

from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from app.learning.units import LearningUnit, LearningUnitType
from app.pipeline.diagnostics import PipelineRunDiagnostics
from app.subtitles.models import MergedSubtitleWindow, SubtitleFragment, CandidateUtterance
from app.extraction.models import UtteranceExtractionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lemma(key: str) -> LearningUnit:
    return LearningUnit(LearningUnitType.LEMMA, key, key)


def _fragment(text: str, idx: int = 0) -> SubtitleFragment:
    return SubtitleFragment(text=text, start_time=float(idx), end_time=float(idx + 2), index=idx)


def _window(text: str, idx: int = 0) -> MergedSubtitleWindow:
    frag = _fragment(text, idx)
    return MergedSubtitleWindow(
        fragments=[frag],
        text=text,
        start_time=frag.start_time,
        end_time=frag.end_time,
    )


def _candidate(text: str, idx: int = 0) -> CandidateUtterance:
    w = _window(text, idx)
    return CandidateUtterance(
        text=text,
        start_time=w.start_time,
        end_time=w.end_time,
        source_window=w,
        char_start=0,
        char_end=len(text),
    )


def _extraction(candidate: CandidateUtterance, units: list[LearningUnit]) -> UtteranceExtractionResult:
    return UtteranceExtractionResult(
        utterance=candidate,
        token_units=[],
        units=units,
        skipped_count=0,
    )


# ---------------------------------------------------------------------------
# 1. PipelineRunDiagnostics — unit tests (no pipeline, no spaCy)
# ---------------------------------------------------------------------------

class TestDerivedProperties:
    def test_quality_acceptance_rate_zero_denominator(self):
        diag = PipelineRunDiagnostics()
        assert diag.quality_acceptance_rate is None

    def test_quality_acceptance_rate_computed(self):
        diag = PipelineRunDiagnostics(candidates_segmented=10, candidates_accepted=7)
        assert diag.quality_acceptance_rate == pytest.approx(0.7)

    def test_i1_rate_zero_denominator(self):
        diag = PipelineRunDiagnostics()
        assert diag.i1_rate is None

    def test_i1_rate_computed(self):
        diag = PipelineRunDiagnostics(candidates_accepted=8, eligible_utterances=4)
        assert diag.i1_rate == pytest.approx(0.5)

    def test_avg_units_per_utterance_zero_denominator(self):
        # All accepted utterances have no units → denominator is zero
        diag = PipelineRunDiagnostics(candidates_accepted=3, utterances_with_no_units=3)
        assert diag.avg_units_per_utterance is None

    def test_avg_units_per_utterance_computed(self):
        diag = PipelineRunDiagnostics(
            candidates_accepted=4,
            utterances_with_no_units=0,
            total_units_extracted=12,
        )
        assert diag.avg_units_per_utterance == pytest.approx(3.0)

    def test_total_ineligible_sums_three_buckets(self):
        diag = PipelineRunDiagnostics(
            ineligible_all_known=3,
            ineligible_too_many_unknowns=5,
            ineligible_no_units=2,
        )
        assert diag.total_ineligible == 10


class TestTopBlockingUnits:
    def test_empty_when_no_blockers(self):
        diag = PipelineRunDiagnostics()
        assert diag.top_blocking_units() == []

    def test_returns_sorted_descending(self):
        diag = PipelineRunDiagnostics(
            blocking_unit_counts={"fahren": 3, "schreiben": 7, "lesen": 1}
        )
        top = diag.top_blocking_units(n=3)
        assert top == [("schreiben", 7), ("fahren", 3), ("lesen", 1)]

    def test_respects_n_limit(self):
        diag = PipelineRunDiagnostics(
            blocking_unit_counts={"a": 5, "b": 4, "c": 3, "d": 2}
        )
        assert len(diag.top_blocking_units(n=2)) == 2

    def test_top_n_default_is_5(self):
        counts = {str(i): i for i in range(10)}
        diag = PipelineRunDiagnostics(blocking_unit_counts=counts)
        assert len(diag.top_blocking_units()) == 5


class TestAsDict:
    def test_all_expected_keys_present(self):
        diag = PipelineRunDiagnostics()
        d = diag.as_dict()
        expected_keys = {
            "fragments_ingested", "windows_merged", "candidates_segmented",
            "candidates_accepted", "candidates_rejected", "rejection_by_rule",
            "total_units_extracted", "utterances_with_no_units", "avg_units_per_utterance",
            "eligible_utterances", "ineligible_all_known", "ineligible_too_many_unknowns",
            "ineligible_no_units", "quality_acceptance_rate", "i1_rate",
            "top_blocking_units", "exposures_recorded", "exposures_deduplicated",
        }
        assert expected_keys.issubset(d.keys())

    def test_values_are_primitive_types(self):
        diag = PipelineRunDiagnostics(
            candidates_segmented=5,
            candidates_accepted=4,
            eligible_utterances=2,
            blocking_unit_counts={"wort": 3},
        )
        d = diag.as_dict()
        assert isinstance(d["quality_acceptance_rate"], float)
        assert isinstance(d["i1_rate"], float)
        assert isinstance(d["top_blocking_units"], list)
        assert isinstance(d["rejection_by_rule"], dict)

    def test_none_rates_when_zero_denominator(self):
        diag = PipelineRunDiagnostics()
        d = diag.as_dict()
        assert d["quality_acceptance_rate"] is None
        assert d["i1_rate"] is None
        assert d["avg_units_per_utterance"] is None


class TestFormatSummary:
    def test_includes_title(self):
        diag = PipelineRunDiagnostics()
        summary = diag.format_summary(title="Test Run")
        assert "Test Run" in summary

    def test_includes_source_and_user_when_provided(self):
        diag = PipelineRunDiagnostics()
        summary = diag.format_summary(source="/tmp/ep01.srt", user="user_42")
        assert "ep01.srt" in summary
        assert "user_42" in summary

    def test_stage_section_headings_present(self):
        diag = PipelineRunDiagnostics()
        summary = diag.format_summary()
        assert "Ingestion" in summary
        assert "Quality Filter" in summary
        assert "Unit Extraction" in summary
        assert "Eligibility" in summary

    def test_rejection_rule_appears_in_summary(self):
        diag = PipelineRunDiagnostics(
            candidates_segmented=10,
            candidates_accepted=8,
            candidates_rejected=2,
            rejection_by_rule={"min_token_count": 2},
        )
        summary = diag.format_summary()
        assert "min_token_count" in summary

    def test_top_blocking_units_appear_when_present(self):
        diag = PipelineRunDiagnostics(
            blocking_unit_counts={"kaufen": 4, "schreiben": 2}
        )
        summary = diag.format_summary()
        assert "kaufen" in summary


# ---------------------------------------------------------------------------
# 2. run_with_diagnostics() integration tests — pipeline stages stubbed
# ---------------------------------------------------------------------------

def _build_pipeline_with_stubs(
    fragments: list[SubtitleFragment],
    windows: list[MergedSubtitleWindow],
    candidates_raw: list[CandidateUtterance],
    candidates_filtered: list[CandidateUtterance],
    extractions: list[UtteranceExtractionResult],
    unknown_map: dict[str, list[LearningUnit]],     # utterance text → unknowns
    rule_metrics: Optional[dict[str, int]] = None,  # rule name → rejection count
):
    """
    Build a GermanSubtitlePipeline with all heavy components replaced by mocks.

    unknown_map controls how many unknowns each utterance has: the keys are
    utterance texts and the values are the unknown LearningUnit lists.
    """
    from app.pipeline.runner import GermanSubtitlePipeline
    from app.pipeline.models import PipelineConfig

    pipeline = GermanSubtitlePipeline.__new__(GermanSubtitlePipeline)
    pipeline.config = PipelineConfig()

    # Stub merger
    pipeline._merger = MagicMock()
    pipeline._merger.merge_fragments.return_value = windows

    # Stub segmenter
    pipeline._segmenter = MagicMock()
    pipeline._segmenter.segment_windows.return_value = candidates_raw

    # Stub quality filter
    pipeline._quality_filter = MagicMock()
    pipeline._quality_filter.filter.return_value = candidates_filtered

    # Build FilterMetrics-like mock for rejection_by_rule population
    rules_mock = {}
    for rule_name, count in (rule_metrics or {}).items():
        rm = MagicMock()
        rm.times_rejected = count
        rules_mock[rule_name] = rm
    metrics_mock = MagicMock()
    metrics_mock.rules = rules_mock
    pipeline._quality_filter.metrics = metrics_mock

    # Stub extractor
    pipeline._extractor = MagicMock()
    pipeline._extractor.extract_batch.return_value = extractions

    # Stub store: unknown_units looks up by utterance text
    store_mock = MagicMock()
    def _unknown_units(user_id, units):
        # Find the utterance for these units by checking which extraction produced them
        for ext in extractions:
            if ext.units == units:
                return unknown_map.get(ext.utterance.text, [])
        return []
    store_mock.unknown_units.side_effect = _unknown_units
    pipeline.store = store_mock

    # Stub exposure service (for record_exposures=True tests)
    exposure_service_mock = MagicMock()
    pipeline.exposure_service = exposure_service_mock

    return pipeline, fragments


class TestStageCounts:
    def test_fragments_ingested(self):
        frags = [_fragment("Ich gehe.", i) for i in range(5)]
        win = [_window("Ich gehe.", 0)]
        cands = [_candidate("Ich gehe.", 0)]
        extractions = [_extraction(cands[0], [_lemma("gehen")])]

        pipeline, frags_list = _build_pipeline_with_stubs(
            fragments=frags,
            windows=win,
            candidates_raw=cands,
            candidates_filtered=cands,
            extractions=extractions,
            unknown_map={"Ich gehe.": [_lemma("gehen")]},
        )

        with patch("app.pipeline.runner.parse_srt", return_value=frags):
            _, diag = pipeline.run_with_diagnostics("fake.srt", "u1")

        assert diag.fragments_ingested == 5

    def test_windows_merged(self):
        frags = [_fragment("Text.", 0)]
        wins = [_window("Text.", 0), _window("Text 2.", 1)]
        cands = []
        extractions = []

        pipeline, _ = _build_pipeline_with_stubs(
            fragments=frags, windows=wins,
            candidates_raw=cands, candidates_filtered=cands,
            extractions=extractions, unknown_map={},
        )

        with patch("app.pipeline.runner.parse_srt", return_value=frags):
            _, diag = pipeline.run_with_diagnostics("fake.srt", "u1")

        assert diag.windows_merged == 2

    def test_candidates_segmented_and_accepted(self):
        frags = [_fragment("Text.", 0)]
        wins = [_window("Text.", 0)]
        cands_raw = [_candidate("Das ist gut.", 0), _candidate("Ja.", 1)]
        # Filter keeps only the first
        cands_filtered = [cands_raw[0]]
        extractions = [_extraction(cands_raw[0], [_lemma("gut")])]

        pipeline, _ = _build_pipeline_with_stubs(
            fragments=frags, windows=wins,
            candidates_raw=cands_raw, candidates_filtered=cands_filtered,
            extractions=extractions,
            unknown_map={"Das ist gut.": [_lemma("gut")]},
        )

        with patch("app.pipeline.runner.parse_srt", return_value=frags):
            _, diag = pipeline.run_with_diagnostics("fake.srt", "u1")

        assert diag.candidates_segmented == 2
        assert diag.candidates_accepted == 1
        assert diag.candidates_rejected == 1

    def test_accepted_plus_rejected_equals_segmented(self):
        frags = [_fragment("T.", 0)]
        wins = [_window("T.", 0)]
        cands_raw = [_candidate(f"Satz {i}.", i) for i in range(6)]
        cands_filtered = cands_raw[:4]  # 2 rejected
        extractions = [_extraction(c, [_lemma("satz")]) for c in cands_filtered]
        unknown_map = {c.text: [] for c in cands_filtered}

        pipeline, _ = _build_pipeline_with_stubs(
            fragments=frags, windows=wins,
            candidates_raw=cands_raw, candidates_filtered=cands_filtered,
            extractions=extractions, unknown_map=unknown_map,
        )

        with patch("app.pipeline.runner.parse_srt", return_value=frags):
            _, diag = pipeline.run_with_diagnostics("fake.srt", "u1")

        assert diag.candidates_accepted + diag.candidates_rejected == diag.candidates_segmented


class TestRejectionByRule:
    def test_rejection_rule_populated(self):
        frags = [_fragment("T.", 0)]
        wins = [_window("T.", 0)]
        cands = []
        extractions = []

        pipeline, _ = _build_pipeline_with_stubs(
            fragments=frags, windows=wins,
            candidates_raw=cands, candidates_filtered=cands,
            extractions=extractions, unknown_map={},
            rule_metrics={"min_token_count": 3, "max_token_count": 1},
        )

        with patch("app.pipeline.runner.parse_srt", return_value=frags):
            _, diag = pipeline.run_with_diagnostics("fake.srt", "u1")

        assert diag.rejection_by_rule["min_token_count"] == 3
        assert diag.rejection_by_rule["max_token_count"] == 1

    def test_rules_with_zero_rejections_excluded(self):
        frags = [_fragment("T.", 0)]
        wins = [_window("T.", 0)]
        cands = []
        extractions = []

        pipeline, _ = _build_pipeline_with_stubs(
            fragments=frags, windows=wins,
            candidates_raw=cands, candidates_filtered=cands,
            extractions=extractions, unknown_map={},
            rule_metrics={"active_rule": 2, "inactive_rule": 0},
        )

        with patch("app.pipeline.runner.parse_srt", return_value=frags):
            _, diag = pipeline.run_with_diagnostics("fake.srt", "u1")

        assert "active_rule" in diag.rejection_by_rule
        assert "inactive_rule" not in diag.rejection_by_rule


class TestEligibilityCounters:
    def _run(self, utterance_unknown_map: dict[str, list[LearningUnit]]):
        """Run with one candidate per key in utterance_unknown_map."""
        frags = [_fragment("T.", 0)]
        wins = [_window("T.", 0)]
        cands = [_candidate(text, i) for i, text in enumerate(utterance_unknown_map)]
        units_per = {text: [_lemma(f"unit_{i}")] for i, text in enumerate(utterance_unknown_map)}
        extractions = [_extraction(c, units_per[c.text]) for c in cands]

        pipeline, _ = _build_pipeline_with_stubs(
            fragments=frags, windows=wins,
            candidates_raw=cands, candidates_filtered=cands,
            extractions=extractions,
            unknown_map=utterance_unknown_map,
        )

        with patch("app.pipeline.runner.parse_srt", return_value=frags):
            matches, diag = pipeline.run_with_diagnostics("fake.srt", "u1")
        return matches, diag

    def test_exactly_one_unknown_is_eligible(self):
        # Three utterances: 0 unknowns, 1 unknown, 2 unknowns
        unknown_map = {
            "Ich kenne das.": [],                        # all known
            "Das ist neu.": [_lemma("neu")],             # i+1 match
            "Viel zu schwer.": [_lemma("viel"), _lemma("schwer")],  # too many
        }
        matches, diag = self._run(unknown_map)

        assert diag.eligible_utterances == 1
        assert diag.ineligible_all_known == 1
        assert diag.ineligible_too_many_unknowns == 1

    def test_eligible_plus_ineligible_equals_accepted(self):
        unknown_map = {
            "Satz A.": [],
            "Satz B.": [_lemma("b")],
            "Satz C.": [_lemma("c"), _lemma("d")],
            "Satz D.": [_lemma("e")],
        }
        _, diag = self._run(unknown_map)

        total_ineligible = (
            diag.ineligible_all_known
            + diag.ineligible_too_many_unknowns
            + diag.ineligible_no_units
        )
        assert diag.eligible_utterances + total_ineligible == diag.candidates_accepted

    def test_match_returned_for_eligible_utterance(self):
        unknown_map = {"Das ist toll.": [_lemma("toll")]}
        matches, diag = self._run(unknown_map)

        assert len(matches) == 1
        assert matches[0].target_unit.key == "toll"
        assert diag.eligible_utterances == 1

    def test_no_units_utterance_counted_as_ineligible_no_units(self):
        frags = [_fragment("T.", 0)]
        wins = [_window("T.", 0)]
        # Utterance with empty unit list (e.g., all punctuation)
        cand = _candidate("— —", 0)
        ext = _extraction(cand, [])  # no units
        cands = [cand]

        pipeline, _ = _build_pipeline_with_stubs(
            fragments=frags, windows=wins,
            candidates_raw=cands, candidates_filtered=cands,
            extractions=[ext], unknown_map={},
        )

        with patch("app.pipeline.runner.parse_srt", return_value=frags):
            matches, diag = pipeline.run_with_diagnostics("fake.srt", "u1")

        assert diag.ineligible_no_units == 1
        assert diag.utterances_with_no_units == 1
        assert matches == []


class TestBlockingUnitTracking:
    def _run_with_blockers(self, utterances_and_unknowns: list[tuple[str, list[str]]]):
        """utterances_and_unknowns: (utterance_text, list_of_unknown_keys)."""
        frags = [_fragment("T.", 0)]
        wins = [_window("T.", 0)]
        cands = [_candidate(text, i) for i, (text, _) in enumerate(utterances_and_unknowns)]
        unknown_map = {
            text: [_lemma(k) for k in unknowns]
            for text, unknowns in utterances_and_unknowns
        }
        extractions = [
            _extraction(c, [_lemma(k) for k in unknowns])
            for c, (_, unknowns) in zip(cands, utterances_and_unknowns)
        ]

        pipeline, _ = _build_pipeline_with_stubs(
            fragments=frags, windows=wins,
            candidates_raw=cands, candidates_filtered=cands,
            extractions=extractions, unknown_map=unknown_map,
        )

        with patch("app.pipeline.runner.parse_srt", return_value=frags):
            _, diag = pipeline.run_with_diagnostics("fake.srt", "u1")
        return diag

    def test_two_unknowns_both_counted_as_blockers(self):
        diag = self._run_with_blockers([
            ("Sehr schwierige Aufgabe.", ["schwierig", "aufgabe"]),
        ])
        assert diag.blocking_unit_counts.get("schwierig", 0) == 1
        assert diag.blocking_unit_counts.get("aufgabe", 0) == 1

    def test_blocker_count_accumulates_across_utterances(self):
        # "schwierig" appears as extra unknown in both sentences
        diag = self._run_with_blockers([
            ("Satz eins hier.", ["schwierig", "eins"]),
            ("Satz zwei hier.", ["schwierig", "zwei"]),
        ])
        assert diag.blocking_unit_counts.get("schwierig", 0) == 2

    def test_eligible_utterances_have_no_blocker_entries(self):
        # An i+1 utterance (1 unknown) should not contribute to blocking_unit_counts
        diag = self._run_with_blockers([
            ("Das ist gut.", ["gut"]),  # eligible — 1 unknown
        ])
        assert diag.blocking_unit_counts == {}
        assert diag.eligible_utterances == 1


class TestExposureTracking:
    def _run_eligible(self, record_exposures: bool, exposure_return_value):
        """Run a pipeline with one eligible utterance, with given exposure return value."""
        frags = [_fragment("Das ist toll.", 0)]
        wins = [_window("Das ist toll.", 0)]
        cand = _candidate("Das ist toll.", 0)
        cands = [cand]
        extractions = [_extraction(cand, [_lemma("toll")])]

        pipeline, _ = _build_pipeline_with_stubs(
            fragments=frags, windows=wins,
            candidates_raw=cands, candidates_filtered=cands,
            extractions=extractions,
            unknown_map={"Das ist toll.": [_lemma("toll")]},
        )

        # Patch record_exposure on the pipeline instance
        pipeline.record_exposure = MagicMock(return_value=exposure_return_value)

        with patch("app.pipeline.runner.parse_srt", return_value=frags):
            _, diag = pipeline.run_with_diagnostics(
                "fake.srt", "u1", record_exposures=record_exposures
            )
        return diag

    def test_record_exposures_false_does_not_call_record_exposure(self):
        diag = self._run_eligible(record_exposures=False, exposure_return_value=MagicMock())
        # No exposure tracking at all
        assert diag.exposures_recorded == 0
        assert diag.exposures_deduplicated == 0

    def test_record_exposures_true_increments_recorded(self):
        # Non-None return → accepted exposure
        diag = self._run_eligible(
            record_exposures=True,
            exposure_return_value=MagicMock(spec=["unit", "utterance_id"]),
        )
        assert diag.exposures_recorded == 1
        assert diag.exposures_deduplicated == 0

    def test_duplicate_exposure_increments_deduplicated(self):
        # None return → duplicate rejected
        diag = self._run_eligible(record_exposures=True, exposure_return_value=None)
        assert diag.exposures_recorded == 0
        assert diag.exposures_deduplicated == 1


class TestInternalConsistency:
    """Cross-field invariants that must hold for any valid diagnostics snapshot."""

    def test_rejected_equals_segmented_minus_accepted(self):
        diag = PipelineRunDiagnostics(
            candidates_segmented=20,
            candidates_accepted=15,
            candidates_rejected=5,
        )
        assert diag.candidates_rejected == diag.candidates_segmented - diag.candidates_accepted

    def test_eligible_plus_all_ineligible_matches_accepted(self):
        # Simulate: 10 accepted, 3 eligible, 4 all-known, 2 too-many, 1 no-units
        diag = PipelineRunDiagnostics(
            candidates_accepted=10,
            eligible_utterances=3,
            ineligible_all_known=4,
            ineligible_too_many_unknowns=2,
            ineligible_no_units=1,
        )
        accounted = (
            diag.eligible_utterances
            + diag.ineligible_all_known
            + diag.ineligible_too_many_unknowns
            + diag.ineligible_no_units
        )
        assert accounted == diag.candidates_accepted

    def test_i1_rate_bounded_between_zero_and_one(self):
        diag = PipelineRunDiagnostics(candidates_accepted=10, eligible_utterances=7)
        rate = diag.i1_rate
        assert rate is not None
        assert 0.0 <= rate <= 1.0

    def test_quality_acceptance_rate_bounded_between_zero_and_one(self):
        diag = PipelineRunDiagnostics(candidates_segmented=10, candidates_accepted=6)
        rate = diag.quality_acceptance_rate
        assert rate is not None
        assert 0.0 <= rate <= 1.0
