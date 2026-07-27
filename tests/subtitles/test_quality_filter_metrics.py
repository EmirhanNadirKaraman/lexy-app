"""
test_quality_filter_metrics.py
------------------------------
Pytest suite for the per-rule rejection metrics instrumentation added to
UtteranceQualityEvaluator.
"""
import pytest

from app.subtitles.models import MergedSubtitleWindow, SubtitleFragment, CandidateUtterance
from app.subtitles.quality import (
    UtteranceQualityEvaluator,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_candidate(text: str) -> CandidateUtterance:
    window = MergedSubtitleWindow(
        fragments=[SubtitleFragment(text=text, start_time=0.0, end_time=3.0)],
        text=text,
        start_time=0.0,
        end_time=3.0,
    )
    return CandidateUtterance(
        text=text,
        start_time=0.0,
        end_time=3.0,
        source_window=window,
        char_start=0,
        char_end=len(text),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def evaluator() -> UtteranceQualityEvaluator:
    """Fresh evaluator with default config for each test."""
    return UtteranceQualityEvaluator()


# ---------------------------------------------------------------------------
# TestValidUtteranceAccepted
# ---------------------------------------------------------------------------

class TestValidUtteranceAccepted:

    def test_total_evaluated_increments_after_one_call(self, evaluator):
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        assert evaluator.metrics.total_evaluated == 1

    def test_total_passed_increments_for_valid_utterance(self, evaluator):
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        assert evaluator.metrics.total_passed == 1

    def test_total_rejected_zero_for_valid_utterance(self, evaluator):
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        assert evaluator.metrics.total_rejected == 0

    def test_overall_rejection_rate_zero_after_clean_pass(self, evaluator):
        evaluator.evaluate(make_candidate("Das Buch liegt auf dem Tisch."))
        assert evaluator.metrics.overall_rejection_rate == 0.0

    def test_all_six_default_checks_appear_in_rules(self, evaluator):
        evaluator.evaluate(make_candidate("Sie arbeitet sehr hart und lernt viel."))
        m = evaluator.metrics
        for rule in (
            "token_count", "incomplete_ending", "suspicious_start",
            "all_caps", "alpha_ratio", "word_repetition",
        ):
            assert rule in m.rules, f"Expected rule '{rule}' in metrics after a normal evaluation"

    def test_valid_utterance_has_zero_rejections_for_all_rules(self, evaluator):
        evaluator.evaluate(make_candidate("Er liest ein spannendes Buch."))
        m = evaluator.metrics
        for rule_name, rm in m.rules.items():
            assert rm.times_rejected == 0, f"Rule '{rule_name}' should not reject a clean sentence"


# ---------------------------------------------------------------------------
# TestSingleRuleRejection
# ---------------------------------------------------------------------------

class TestSingleRuleRejection:

    def test_token_count_rejection_increments_token_count_counter(self, evaluator):
        evaluator.evaluate(make_candidate("Vielleicht."))
        assert evaluator.metrics.rules["token_count"].times_rejected == 1
        assert evaluator.metrics.total_rejected == 1

    def test_token_count_rejection_does_not_fire_other_counters(self, evaluator):
        evaluator.evaluate(make_candidate("Vielleicht."))
        m = evaluator.metrics
        for rule in ("incomplete_ending", "suspicious_start",
                     "all_caps", "alpha_ratio", "word_repetition"):
            assert m.rules[rule].times_rejected == 0, (
                f"Rule '{rule}' should not be rejected for a short-but-clean token"
            )

    def test_incomplete_ending_rejection_increments_correct_counter(self, evaluator):
        evaluator.evaluate(make_candidate("Ich warte auf"))
        assert evaluator.metrics.rules["incomplete_ending"].times_rejected == 1

    def test_suspicious_start_rejection_increments_correct_counter(self, evaluator):
        evaluator.evaluate(make_candidate("und dann kam er nach Hause."))
        assert evaluator.metrics.rules["suspicious_start"].times_rejected == 1

    def test_word_repetition_rejection_increments_correct_counter(self, evaluator):
        evaluator.evaluate(make_candidate("Ja ja ja ja ja nein."))
        assert evaluator.metrics.rules["word_repetition"].times_rejected == 1

    def test_alpha_ratio_rejection_increments_correct_counter(self, evaluator):
        evaluator.evaluate(make_candidate("♪ La la ♪"))
        assert evaluator.metrics.rules["alpha_ratio"].times_rejected == 1

    def test_all_caps_rejection_increments_correct_counter(self, evaluator):
        evaluator.evaluate(make_candidate("ICH WILL DICH NICHT VERLIEREN NIEMALS"))
        assert evaluator.metrics.rules["all_caps"].times_rejected == 1

    def test_rejection_rate_is_one_for_the_single_fired_rule(self, evaluator):
        evaluator.evaluate(make_candidate("Vielleicht."))
        assert evaluator.metrics.rules["token_count"].rejection_rate == 1.0

    def test_rejection_rate_is_zero_for_unfired_rule(self, evaluator):
        evaluator.evaluate(make_candidate("Vielleicht."))
        assert evaluator.metrics.rules["word_repetition"].rejection_rate == 0.0


# ---------------------------------------------------------------------------
# TestMultipleRuleCounters
# ---------------------------------------------------------------------------

class TestMultipleRuleCounters:

    def test_two_different_rules_each_fire_exactly_once(self, evaluator):
        evaluator.evaluate(make_candidate("Vielleicht."))
        evaluator.evaluate(make_candidate("Ich warte auf"))
        m = evaluator.metrics
        assert m.rules["token_count"].times_rejected == 1
        assert m.rules["incomplete_ending"].times_rejected == 1

    def test_total_evaluated_counts_all_utterances(self, evaluator):
        for text in (
            "Vielleicht.",
            "Ich warte auf",
            "Ich gehe morgen ins Kino.",
        ):
            evaluator.evaluate(make_candidate(text))
        assert evaluator.metrics.total_evaluated == 3

    def test_total_passed_plus_rejected_equals_total_evaluated(self, evaluator):
        for text in (
            "Vielleicht.",
            "Ich warte auf",
            "Ich gehe morgen ins Kino.",
            "Das Wetter ist heute sehr schön.",
        ):
            evaluator.evaluate(make_candidate(text))
        m = evaluator.metrics
        assert m.total_passed + m.total_rejected == m.total_evaluated

    def test_rule_evaluated_count_equals_number_of_non_whitelist_calls(self, evaluator):
        for text in (
            "Ich gehe morgen ins Kino.",
            "Das Buch liegt auf dem Tisch.",
            "Vielleicht.",
        ):
            evaluator.evaluate(make_candidate(text))
        assert evaluator.metrics.rules["token_count"].times_evaluated == 3

    def test_same_rule_rejected_multiple_times_accumulates(self, evaluator):
        for text in ("Vielleicht.", "Schön.", "Wirklich."):
            evaluator.evaluate(make_candidate(text))
        assert evaluator.metrics.rules["token_count"].times_rejected == 3


# ---------------------------------------------------------------------------
# TestWhitelistMetrics
# ---------------------------------------------------------------------------

class TestWhitelistMetrics:

    def test_whitelist_match_increments_total_passed(self, evaluator):
        evaluator.evaluate(make_candidate("Keine Ahnung."))
        assert evaluator.metrics.total_passed == 1

    def test_whitelist_match_increments_whitelist_accepted(self, evaluator):
        evaluator.evaluate(make_candidate("Keine Ahnung."))
        assert evaluator.metrics.whitelist_accepted == 1

    def test_whitelist_match_does_not_increment_total_rejected(self, evaluator):
        evaluator.evaluate(make_candidate("Keine Ahnung."))
        assert evaluator.metrics.total_rejected == 0

    def test_whitelist_fast_path_skips_all_heuristic_checks(self, evaluator):
        evaluator.evaluate(make_candidate("Danke schön!"))
        m = evaluator.metrics
        for rule in ("token_count", "incomplete_ending", "suspicious_start",
                     "all_caps", "alpha_ratio", "word_repetition"):
            assert rule not in m.rules, (
                f"Rule '{rule}' must not appear when only a whitelist utterance was evaluated"
            )

    def test_whitelist_accepted_not_incremented_for_normal_pass(self, evaluator):
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino mit meiner Freundin."))
        assert evaluator.metrics.whitelist_accepted == 0

    def test_whitelist_and_normal_pass_tracked_separately(self, evaluator):
        evaluator.evaluate(make_candidate("Keine Ahnung."))
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        m = evaluator.metrics
        assert m.total_passed == 2
        assert m.whitelist_accepted == 1

    def test_multiple_whitelist_entries_accumulate(self, evaluator):
        for phrase in ("Keine Ahnung.", "Danke schön!", "Natürlich.", "Genau."):
            evaluator.evaluate(make_candidate(phrase))
        assert evaluator.metrics.whitelist_accepted == 4


# ---------------------------------------------------------------------------
# TestMetricsConsistency
# ---------------------------------------------------------------------------

class TestMetricsConsistency:

    def test_invariant_holds_after_every_individual_evaluation(self, evaluator):
        texts = (
            "Ich gehe morgen ins Kino.",
            "Vielleicht.",
            "Keine Ahnung.",
            "und dann kam er nach Hause.",
            "Das Buch liegt auf dem Tisch.",
        )
        for i, text in enumerate(texts, start=1):
            evaluator.evaluate(make_candidate(text))
            m = evaluator.metrics
            assert m.total_passed + m.total_rejected == m.total_evaluated, (
                f"Invariant broken after {i} evaluations"
            )

    def test_rule_evaluated_never_exceeds_total_evaluated(self, evaluator):
        for text in (
            "Ich gehe morgen ins Kino.",
            "Das Wetter ist heute sehr schön.",
            "Keine Ahnung.",
        ):
            evaluator.evaluate(make_candidate(text))
        m = evaluator.metrics
        for name, rm in m.rules.items():
            assert rm.times_evaluated <= m.total_evaluated, (
                f"Rule '{name}' evaluated more times than total utterances"
            )

    def test_rule_rejected_never_exceeds_rule_evaluated(self, evaluator):
        for text in (
            "Vielleicht.",
            "Ich warte auf",
            "Ja ja ja ja ja nein.",
            "Ich gehe morgen ins Kino.",
        ):
            evaluator.evaluate(make_candidate(text))
        for name, rm in evaluator.metrics.rules.items():
            assert rm.times_rejected <= rm.times_evaluated, (
                f"Rule '{name}' rejected more times than it was evaluated"
            )

    def test_snapshot_is_frozen_after_further_evaluations(self, evaluator):
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        snapshot = evaluator.metrics

        evaluator.evaluate(make_candidate("Das Buch liegt auf dem Tisch."))
        evaluator.evaluate(make_candidate("Vielleicht."))

        assert snapshot.total_evaluated == 1
        assert evaluator.metrics.total_evaluated == 3

    def test_known_split_is_recorded_correctly(self, evaluator):
        good = "Ich gehe morgen ins Kino."
        bad  = "Vielleicht."
        for _ in range(7):
            evaluator.evaluate(make_candidate(good))
        for _ in range(3):
            evaluator.evaluate(make_candidate(bad))
        m = evaluator.metrics
        assert m.total_evaluated == 10
        assert m.total_passed == 7
        assert m.total_rejected == 3
        assert m.rules["token_count"].times_rejected == 3


# ---------------------------------------------------------------------------
# TestResetMetrics
# ---------------------------------------------------------------------------

class TestResetMetrics:

    def test_reset_zeroes_total_evaluated(self, evaluator):
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        evaluator.reset_metrics()
        assert evaluator.metrics.total_evaluated == 0

    def test_reset_zeroes_total_passed(self, evaluator):
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        evaluator.reset_metrics()
        assert evaluator.metrics.total_passed == 0

    def test_reset_zeroes_whitelist_accepted(self, evaluator):
        evaluator.evaluate(make_candidate("Keine Ahnung."))
        evaluator.reset_metrics()
        assert evaluator.metrics.whitelist_accepted == 0

    def test_reset_clears_rule_entries(self, evaluator):
        evaluator.evaluate(make_candidate("Vielleicht."))
        assert "token_count" in evaluator.metrics.rules
        evaluator.reset_metrics()
        assert evaluator.metrics.rules == {}

    def test_counters_accumulate_from_zero_after_reset(self, evaluator):
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        evaluator.evaluate(make_candidate("Vielleicht."))
        evaluator.reset_metrics()
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        m = evaluator.metrics
        assert m.total_evaluated == 1
        assert m.total_passed == 1
        assert m.total_rejected == 0

    def test_double_reset_is_idempotent(self, evaluator):
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        evaluator.reset_metrics()
        evaluator.reset_metrics()
        assert evaluator.metrics.total_evaluated == 0


# ---------------------------------------------------------------------------
# TestFormatTable
# ---------------------------------------------------------------------------

class TestFormatTable:

    def test_returns_a_string(self, evaluator):
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        assert isinstance(evaluator.metrics.format_table(), str)

    def test_includes_rule_name_for_fired_rule(self, evaluator):
        evaluator.evaluate(make_candidate("Vielleicht."))
        assert "token_count" in evaluator.metrics.format_table()

    def test_includes_total_counts_header(self, evaluator):
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        table = evaluator.metrics.format_table()
        assert "evaluated" in table
        assert "passed" in table

    def test_fresh_evaluator_does_not_crash(self, evaluator):
        table = evaluator.metrics.format_table()
        assert isinstance(table, str)
        assert "0" in table

    def test_whitelist_row_uses_dash_for_rejected_and_rate(self, evaluator):
        evaluator.evaluate(make_candidate("Keine Ahnung."))
        table = evaluator.metrics.format_table()
        assert "—" in table
