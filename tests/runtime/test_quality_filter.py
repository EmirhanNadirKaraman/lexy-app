"""
Runtime-aligned port of the quality-filter tests (#17 salvage batch 3).

Targets the RUNTIME module `utterance_quality_filter.py` (975 lines), not the
orphan refactor at `src/app/subtitles/quality.py`.

Why this file exists: the quality filter decides which candidate utterances are
good enough to become learning material. Before this batch it had **zero**
runtime coverage — every test lived against the refactor. A regression here
either floods the corpus with junk (song-lyric fragments, all-caps shouting,
mid-sentence cutoffs) or silently drops usable sentences.

Selection: the ported tests lock **which rule fires for which input** and the
counter invariant. Pure presentation (`format_table` output) and counter-reset
plumbing were deliberately left behind — see the module docstring in
`docs/TESTS.md` for the batch rationale.

Parity verified before porting: `QualityFilterConfig` has the same 11 fields and
defaults in both trees; `UtteranceQualityEvaluator.evaluate`, `.metrics`, and the
`CandidateUtterance` / `MergedSubtitleWindow` / `SubtitleFragment` dataclass
fields are identical. No production code was changed.

Ported nodeids (src/app suite):
  tests/subtitles/test_quality_filter_metrics.py::TestSingleRuleRejection::* (9)
  tests/subtitles/test_quality_filter_metrics.py::TestWhitelistMetrics::* (4 of 7)
  tests/subtitles/test_quality_filter_metrics.py::TestMetricsConsistency::test_invariant_holds_after_every_individual_evaluation
"""
import pytest

from subtitle_merger import MergedSubtitleWindow, SubtitleFragment
from subtitle_segmenter import CandidateUtterance
from utterance_quality_filter import UtteranceQualityEvaluator


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


@pytest.fixture
def evaluator() -> UtteranceQualityEvaluator:
    """Fresh evaluator with default config for each test."""
    return UtteranceQualityEvaluator()


class TestSingleRuleRejection:
    """Each heuristic must fire for its own input and only its own input.
    These are the highest-value assertions in the file: they pin the mapping
    from bad-input-shape to the rule that catches it."""

    def test_token_count_rejection_increments_token_count_counter(self, evaluator):
        evaluator.evaluate(make_candidate("Vielleicht."))
        assert evaluator.metrics.rules["token_count"].times_rejected == 1
        assert evaluator.metrics.total_rejected == 1

    def test_token_count_rejection_does_not_fire_other_counters(self, evaluator):
        """A short-but-clean utterance must trip exactly one rule. If other
        counters also fire, rejection reasons reported to the user are wrong."""
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
        """Music-note runs are the classic subtitle noise this rule exists for."""
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


class TestWhitelist:
    """Whitelisted short utterances bypass every heuristic. Without the fast
    path, common conversational fillers would be rejected by token_count and
    never reach the learner."""

    def test_whitelist_match_increments_total_passed(self, evaluator):
        evaluator.evaluate(make_candidate("Keine Ahnung."))
        assert evaluator.metrics.total_passed == 1

    def test_whitelist_match_increments_whitelist_accepted(self, evaluator):
        evaluator.evaluate(make_candidate("Keine Ahnung."))
        assert evaluator.metrics.whitelist_accepted == 1

    def test_whitelist_match_does_not_increment_total_rejected(self, evaluator):
        evaluator.evaluate(make_candidate("Keine Ahnung."))
        assert evaluator.metrics.total_rejected == 0

    def test_whitelist_accepted_not_incremented_for_normal_pass(self, evaluator):
        """A normal passing sentence must not be miscounted as a whitelist hit,
        or the metrics table overstates how much the fast path is doing."""
        evaluator.evaluate(make_candidate("Ich gehe morgen ins Kino."))
        assert evaluator.metrics.whitelist_accepted == 0


class TestMetricsConsistency:
    """passed + rejected == evaluated, after every single call."""

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
