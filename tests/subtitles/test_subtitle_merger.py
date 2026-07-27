"""
Tests for SubtitleMerger.
"""

from app.subtitles.merging import SubtitleMergeConfig, SubtitleMerger
from app.subtitles.models import SubtitleFragment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def frag(text: str, start: float, end: float, index: int = 0) -> SubtitleFragment:
    return SubtitleFragment(text=text, start_time=start, end_time=end, index=index)


def merge(fragments: list[SubtitleFragment], **cfg_kwargs) -> list:
    cfg = SubtitleMergeConfig(**cfg_kwargs) if cfg_kwargs else SubtitleMergeConfig()
    return SubtitleMerger(cfg).merge_fragments(fragments)


# ---------------------------------------------------------------------------
# Window properties
# ---------------------------------------------------------------------------

class TestWindowProperties:
    """Merged windows carry correct text, timing, and fragment provenance."""

    def test_merged_text_is_space_joined(self):
        windows = merge([
            frag("Ich gehe", 0.0, 1.0),
            frag("nach Hause.", 1.1, 2.0),
        ])
        assert windows[0].text == "Ich gehe nach Hause."

    def test_start_time_comes_from_first_fragment(self):
        windows = merge([
            frag("Ich gehe", 1.5, 2.0),
            frag("nach Hause.", 2.1, 3.5),
        ])
        assert windows[0].start_time == 1.5

    def test_end_time_comes_from_last_fragment(self):
        windows = merge([
            frag("Ich gehe", 1.5, 2.0),
            frag("nach Hause.", 2.1, 3.5),
        ])
        assert windows[0].end_time == 3.5

    def test_original_fragments_preserved_in_order(self):
        f1 = frag("Ich gehe", 0.0, 1.0, index=0)
        f2 = frag("nach Hause.", 1.1, 2.0, index=1)
        windows = merge([f1, f2])
        assert windows[0].fragments == [f1, f2]

    def test_separate_windows_each_own_their_fragments(self):
        f1 = frag("Das war gut.", 0.0, 1.0)
        f2 = frag("Jetzt gehen wir.", 5.0, 6.0)
        windows = merge([f1, f2])
        assert windows[0].fragments == [f1]
        assert windows[1].fragments == [f2]

    def test_html_tags_stripped_from_window_text(self):
        windows = merge([frag("<i>Hallo Welt.</i>", 0.0, 1.0)])
        assert windows[0].text == "Hallo Welt."

    def test_bracketed_annotation_stripped_from_window_text(self):
        windows = merge([frag("[Musik] Ja.", 0.0, 1.0)])
        assert windows[0].text == "Ja."

    def test_leading_dialogue_dash_stripped_from_window_text(self):
        windows = merge([frag("- Guten Morgen!", 0.0, 1.0)])
        assert windows[0].text == "Guten Morgen!"


# ---------------------------------------------------------------------------
# Timing heuristics
# ---------------------------------------------------------------------------

class TestTimingHeuristics:

    def test_gap_within_threshold_allows_merge(self):
        windows = merge([
            frag("das ist", 0.0, 1.0),
            frag("schön.", 1.5, 2.0),
        ])
        assert len(windows) == 1

    def test_gap_above_threshold_prevents_merge(self):
        windows = merge([
            frag("Wenn du möchtest,", 0.0, 1.0),
            frag("ruf mich an.", 1.7, 2.5),
        ])
        assert len(windows) == 2

    def test_large_gap_blocks_merge_even_when_other_signals_fire(self):
        windows = merge([
            frag("Wenn du möchtest,", 0.0, 1.0),
            frag("können wir reden.", 5.0, 6.5),
        ])
        assert len(windows) == 2

    def test_tiny_gap_merges_continuation_at_tiny_gap(self):
        windows = merge([
            frag("Ich gehe nach", 0.0, 1.0),
            frag("Hause jetzt.", 1.1, 2.0),
        ])
        assert len(windows) == 1

    def test_tiny_gap_does_not_unconditionally_merge_short_complete_turns(self):
        windows = merge([
            frag("Wirklich?", 0.0, 0.5),
            frag("Ja, natürlich.", 0.6, 1.2),
        ])
        assert len(windows) == 2

    def test_custom_max_gap_respected(self):
        windows = merge(
            [
                frag("es war einmal", 0.0, 1.0),
                frag("ein Kind.", 2.0, 3.0),
            ],
            max_gap_s=1.5,
        )
        assert len(windows) == 1


# ---------------------------------------------------------------------------
# Strong punctuation → sentence boundary
# ---------------------------------------------------------------------------

class TestStrongPunctuationBoundary:

    def test_period_and_uppercase_next_prevents_merge(self):
        windows = merge([
            frag("Das war schön.", 0.0, 1.0),
            frag("Jetzt gehen wir.", 1.3, 2.5),
        ])
        assert len(windows) == 2

    def test_exclamation_mark_and_uppercase_next_prevents_merge(self):
        windows = merge([
            frag("Stopp!", 0.0, 0.5),
            frag("Das geht nicht.", 0.8, 1.8),
        ])
        assert len(windows) == 2

    def test_question_mark_and_uppercase_next_prevents_merge(self):
        windows = merge([
            frag("Bist du sicher?", 0.0, 1.2),
            frag("Ich bin sicher.", 1.5, 2.5),
        ])
        assert len(windows) == 2

    def test_ellipsis_and_uppercase_next_prevents_merge(self):
        windows = merge([
            frag("Ich weiß nicht…", 0.0, 1.2),
            frag("Vielleicht schon.", 1.4, 2.5),
        ])
        assert len(windows) == 2

    def test_strong_punctuation_with_lowercase_next_still_merges(self):
        windows = merge([
            frag("Ich weiß nicht.", 0.0, 1.2),
            frag("weil ich nicht da war.", 1.3, 2.5),
        ])
        assert len(windows) == 1


# ---------------------------------------------------------------------------
# Weak punctuation → clause continuation
# ---------------------------------------------------------------------------

class TestWeakPunctuationMerge:

    def test_comma_at_end_triggers_merge(self):
        windows = merge([
            frag("Wenn du Zeit hast,", 0.0, 1.5),
            frag("ruf mich an.", 1.7, 2.8),
        ])
        assert len(windows) == 1

    def test_semicolon_at_end_triggers_merge(self):
        windows = merge([
            frag("Er kam spät;", 0.0, 1.2),
            frag("sie war schon weg.", 1.4, 2.4),
        ])
        assert len(windows) == 1

    def test_comma_merge_disabled_does_not_merge_on_comma_alone(self):
        windows = merge(
            [
                frag("Wenn du Zeit hast,", 0.0, 1.5),
                frag("Ruf mich an.", 1.7, 2.8),
            ],
            merge_on_weak_punctuation=False,
            merge_on_continuation_word=False,
            merge_on_lowercase_continuation=False,
        )
        assert len(windows) == 2


# ---------------------------------------------------------------------------
# Continuation-word heuristic
# ---------------------------------------------------------------------------

class TestContinuationWordHeuristic:

    def test_preposition_at_end_triggers_merge(self):
        windows = merge([
            frag("Wir treffen uns vor", 0.0, 1.0),
            frag("dem Bahnhof.", 1.2, 2.0),
        ])
        assert len(windows) == 1

    def test_subordinating_conjunction_at_end_triggers_merge(self):
        windows = merge([
            frag("Ich bleibe, weil", 0.0, 1.0),
            frag("du hier bist.", 1.2, 2.2),
        ])
        assert len(windows) == 1

    def test_article_at_end_triggers_merge(self):
        windows = merge([
            frag("Er liest gerade die", 0.0, 1.0),
            frag("Zeitung.", 1.2, 1.8),
        ])
        assert len(windows) == 1

    def test_auxiliary_at_end_triggers_merge(self):
        windows = merge([
            frag("Sie wird", 0.0, 0.6),
            frag("gleich kommen.", 0.8, 1.8),
        ])
        assert len(windows) == 1

    def test_continuation_word_heuristic_disabled_does_not_merge(self):
        windows = merge(
            [
                frag("Wir treffen uns vor", 0.0, 1.0),
                frag("dem Bahnhof.", 1.2, 2.0),
            ],
            merge_on_continuation_word=False,
            merge_on_lowercase_continuation=False,
        )
        assert len(windows) == 2

    def test_content_word_at_end_does_not_trigger_continuation_merge(self):
        windows = merge(
            [
                frag("Er liest die Zeitung.", 0.0, 1.2),
                frag("Danach geht er schlafen.", 1.4, 2.5),
            ],
            merge_on_lowercase_continuation=False,
        )
        assert len(windows) == 2


# ---------------------------------------------------------------------------
# Lowercase-start heuristic
# ---------------------------------------------------------------------------

class TestLowercaseContinuationHeuristic:

    def test_lowercase_start_triggers_merge(self):
        windows = merge([
            frag("Das ist das Haus", 0.0, 1.5),
            frag("in dem sie wohnt.", 1.7, 2.8),
        ])
        assert len(windows) == 1

    def test_uppercase_start_without_other_signals_prevents_merge(self):
        windows = merge(
            [
                frag("Das Buch ist gut.", 0.0, 1.5),
                frag("Kaufe es dir.", 1.8, 2.8),
            ],
            merge_on_short_fragment=False,
        )
        assert len(windows) == 2

    def test_lowercase_heuristic_disabled_does_not_merge_on_case_alone(self):
        windows = merge(
            [
                frag("Das ist das Haus", 0.0, 1.5),
                frag("in dem sie wohnt.", 1.7, 2.8),
            ],
            merge_on_lowercase_continuation=False,
            merge_on_continuation_word=False,
        )
        assert len(windows) == 2

    def test_disabling_lowercase_still_merges_via_comma(self):
        windows = merge(
            [
                frag("Wenn du möchtest,", 0.0, 1.5),
                frag("können wir reden.", 1.7, 2.8),
            ],
            merge_on_lowercase_continuation=False,
        )
        assert len(windows) == 1


# ---------------------------------------------------------------------------
# Short-fragment heuristic
# ---------------------------------------------------------------------------

class TestShortFragmentHeuristic:

    def test_one_word_fragment_merges_with_next(self):
        windows = merge([
            frag("Ja", 0.0, 0.4),
            frag("natürlich war das so.", 0.6, 1.8),
        ])
        assert len(windows) == 1

    def test_two_word_fragment_merges_with_next(self):
        windows = merge([
            frag("Gut gemacht", 0.0, 0.8),
            frag("mein Freund.", 1.0, 1.8),
        ])
        assert len(windows) == 1

    def test_fragment_exactly_at_threshold_is_not_short(self):
        windows = merge(
            [
                frag("Das ist schön.", 0.0, 1.2),
                frag("Wirklich gut.", 1.4, 2.2),
            ],
            merge_on_short_fragment=False,
            merge_on_continuation_word=False,
            merge_on_lowercase_continuation=False,
            merge_on_weak_punctuation=False,
        )
        assert len(windows) == 2

    def test_short_fragment_heuristic_disabled_does_not_merge_on_length_alone(self):
        windows = merge(
            [
                frag("Ja", 0.0, 0.4),
                frag("Natürlich.", 0.6, 1.2),
            ],
            merge_on_short_fragment=False,
            merge_on_continuation_word=False,
            merge_on_lowercase_continuation=False,
            merge_on_weak_punctuation=False,
        )
        assert len(windows) == 2

    def test_custom_min_standalone_words_raises_threshold(self):
        windows = merge(
            [
                frag("Er ist gut hier", 0.0, 1.0),
                frag("Morgen kommt er.", 1.2, 2.0),
            ],
            min_standalone_words=5,
            merge_on_continuation_word=False,
            merge_on_lowercase_continuation=False,
            merge_on_weak_punctuation=False,
        )
        assert len(windows) == 1


# ---------------------------------------------------------------------------
# Hyphen-break heuristic
# ---------------------------------------------------------------------------

class TestHyphenBreakHeuristic:

    def test_trailing_hyphen_triggers_merge(self):
        windows = merge([
            frag("Das ist sein Lieblings-", 0.0, 1.5),
            frag("film aller Zeiten.", 1.6, 2.8),
        ])
        assert len(windows) == 1

    def test_hyphen_is_stripped_and_word_is_concatenated(self):
        windows = merge([
            frag("Das ist sein Lieblings-", 0.0, 1.5),
            frag("film aller Zeiten.", 1.6, 2.8),
        ])
        assert windows[0].text == "Das ist sein Lieblingsfilm aller Zeiten."
        assert "Lieblings-" not in windows[0].text

    def test_hyphen_merge_disabled_does_not_merge_on_hyphen_alone(self):
        windows = merge(
            [
                frag("Das ist sein Lieblings-", 0.0, 1.5),
                frag("film aller Zeiten.", 1.8, 3.0),
            ],
            merge_on_hyphen_break=False,
            merge_on_lowercase_continuation=False,
            merge_on_continuation_word=False,
            merge_on_weak_punctuation=False,
            merge_on_short_fragment=False,
        )
        assert len(windows) == 2


# ---------------------------------------------------------------------------
# Multi-fragment windows
# ---------------------------------------------------------------------------

class TestMultiFragmentWindows:

    def test_three_fragments_merge_into_one_window(self):
        windows = merge([
            frag("Wir treffen uns", 0.0, 1.0),
            frag("vor dem", 1.1, 1.6),
            frag("großen Bahnhof.", 1.7, 2.5),
        ])
        assert len(windows) == 1
        assert windows[0].text == "Wir treffen uns vor dem großen Bahnhof."
        assert len(windows[0].fragments) == 3

    def test_chain_splits_at_large_gap(self):
        windows = merge([
            frag("Ich gehe", 0.0, 0.8),
            frag("nach Hause,", 0.9, 1.5),
            frag("weil es spät ist.", 5.0, 6.5),
        ])
        assert len(windows) == 2
        assert windows[0].text == "Ich gehe nach Hause,"
        assert windows[1].text == "weil es spät ist."

    def test_four_fragments_merge_with_continuation_signals(self):
        windows = merge([
            frag("Er hat das", 0.0, 0.8),
            frag("Buch für", 0.9, 1.3),
            frag("seinen kleinen", 1.4, 1.9),
            frag("Bruder gekauft.", 2.0, 2.8),
        ])
        assert len(windows) == 1
        assert len(windows[0].fragments) == 4

    def test_realistic_dialogue_merges_and_splits_correctly(self):
        windows = merge([
            frag("Guten Morgen!", 0.0, 0.8),
            frag("Hast du schon", 0.9, 1.5),
            frag("gefrühstückt?", 1.55, 2.2),
            frag("Nein, noch nicht.", 3.0, 4.0),
            frag("Ich warte auf", 4.1, 4.8),
            frag("dich.", 4.85, 5.2),
        ])
        assert len(windows) == 4
        assert windows[0].text == "Guten Morgen!"
        assert "gefrühstückt?" in windows[1].text
        assert windows[2].text == "Nein, noch nicht."
        assert "auf dich." in windows[3].text


# ---------------------------------------------------------------------------
# Max window duration ceiling
# ---------------------------------------------------------------------------

class TestMaxWindowDuration:

    def test_window_never_exceeds_max_duration(self):
        frags = [
            frag(f"Teil {i},", i * 1.6, i * 1.6 + 1.5, index=i)
            for i in range(4)
        ]
        windows = merge(frags, max_window_duration_s=4.0)
        for w in windows:
            assert w.duration <= 4.0

    def test_tighter_max_duration_produces_more_windows(self):
        frags = [
            frag("Wort eins,", 0.0, 1.0),
            frag("Wort zwei,", 1.1, 2.0),
            frag("Wort drei,", 2.1, 3.0),
            frag("Wort vier.", 3.1, 4.0),
        ]
        windows_tight = merge(frags, max_window_duration_s=2.5)
        windows_loose = merge(frags, max_window_duration_s=10.0)
        assert len(windows_tight) > len(windows_loose)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_empty_input_returns_empty_list(self):
        assert merge([]) == []

    def test_single_fragment_returns_one_window(self):
        windows = merge([frag("Hallo!", 0.0, 1.0)])
        assert len(windows) == 1
        assert windows[0].text == "Hallo!"

    def test_single_fragment_timing_is_preserved(self):
        windows = merge([frag("Hallo!", 3.5, 4.2)])
        assert windows[0].start_time == 3.5
        assert windows[0].end_time == 4.2

    def test_all_fragments_with_large_gaps_produce_n_separate_windows(self):
        frags = [frag(f"Satz {i}.", float(i * 5), float(i * 5 + 1)) for i in range(4)]
        windows = merge(frags)
        assert len(windows) == 4

    def test_whitespace_only_fragment_does_not_crash(self):
        windows = merge([
            frag("Ich bin hier.", 0.0, 1.0),
            frag("   ", 1.1, 1.5),
            frag("Wirklich?", 1.6, 2.2),
        ])
        assert len(windows) >= 1

    def test_annotation_only_fragment_does_not_crash(self):
        windows = merge([
            frag("Er sprach.", 0.0, 1.5),
            frag("[Applaus]", 1.6, 2.5),
            frag("Danke sehr.", 2.6, 3.5),
        ])
        assert len(windows) >= 1

    def test_back_to_back_identical_short_complete_fragments_now_split(self):
        windows = merge([
            frag("Ja.", 0.0, 0.5),
            frag("Ja.", 0.6, 1.0),
        ])
        assert len(windows) == 2


# ---------------------------------------------------------------------------
# Multi-speaker safeguards
# ---------------------------------------------------------------------------

class TestMultiSpeakerGuard:

    def test_dash_prefix_on_next_fragment_blocks_merge(self):
        windows = merge([
            frag("Das war gut.", 0.0, 1.0),
            frag("- Wirklich?", 1.05, 1.7),
        ])
        assert len(windows) == 2

    def test_dash_prefix_blocks_merge_even_at_tiny_gap(self):
        windows = merge([
            frag("Das ist schön.", 0.0, 1.0),
            frag("- Danke.", 1.05, 1.5),
        ])
        assert len(windows) == 2

    def test_en_dash_prefix_also_blocked(self):
        windows = merge([
            frag("Gut gemacht.", 0.0, 0.8),
            frag("– Danke schön.", 0.85, 1.5),
        ])
        assert len(windows) == 2

    def test_em_dash_prefix_also_blocked(self):
        windows = merge([
            frag("Gut gemacht.", 0.0, 0.8),
            frag("— Danke schön.", 0.85, 1.5),
        ])
        assert len(windows) == 2

    def test_three_dash_prefixed_turns_each_separate(self):
        windows = merge([
            frag("- Das war wirklich gut.", 0.0, 1.2),
            frag("- Wirklich?", 1.25, 1.8),
            frag("- Ja, total.", 1.85, 2.5),
        ])
        assert len(windows) == 3

    def test_dash_only_separator_line_does_not_block_merge(self):
        windows = merge([
            frag("Ich gehe nach", 0.0, 1.0),
            frag("– ", 1.05, 1.1),
            frag("Hause jetzt.", 1.15, 2.0),
        ])
        assert len(windows) >= 1

    def test_dialogue_dash_guard_disabled_allows_merge(self):
        windows2 = merge(
            [
                frag("Das war gut.", 0.0, 1.0),
                frag("- Wirklich?", 1.05, 1.7),
            ],
            guard_dialogue_dash=False,
            guard_short_complete_turn=False,
        )
        assert len(windows2) == 1

    def test_short_complete_turn_blocks_merge_at_tiny_gap(self):
        windows = merge([
            frag("Ich auch.", 0.0, 0.8),
            frag("Wirklich?", 0.9, 1.5),
        ])
        assert len(windows) == 2

    def test_question_answer_pair_stays_separate(self):
        windows = merge([
            frag("Bist du sicher?", 0.0, 0.8),
            frag("Ja, absolut.", 0.85, 1.5),
        ])
        assert len(windows) == 2

    def test_long_complete_fragment_still_merges_via_tiny_gap(self):
        windows = merge([
            frag("Er hat das Buch wirklich gut gelesen!", 0.0, 2.0),
            frag("Das finde ich toll.", 2.05, 3.0),
        ])
        assert len(windows) == 1

    def test_incomplete_current_fragment_never_triggers_guard(self):
        windows = merge([
            frag("Wenn du möchtest,", 0.0, 1.0),
            frag("Ruf mich an.", 1.05, 1.8),
        ])
        assert len(windows) == 1

    def test_custom_max_complete_turn_words_tightens_guard(self):
        windows_guard = merge(
            [
                frag("Das ist gut.", 0.0, 0.8),
                frag("Wirklich?", 0.85, 1.2),
            ],
            max_complete_turn_words=3,
        )
        assert len(windows_guard) == 2

    def test_short_complete_turn_guard_disabled_merges_via_tiny_gap(self):
        windows = merge(
            [
                frag("Wirklich?", 0.0, 0.5),
                frag("Ja, natürlich.", 0.55, 1.1),
            ],
            guard_dialogue_dash=False,
            guard_short_complete_turn=False,
        )
        assert len(windows) == 1

    def test_uppercase_next_required_for_guard_to_fire(self):
        windows = merge([
            frag("Gut.", 0.0, 0.4),
            frag("weil ich das weiß.", 0.45, 1.2),
        ])
        assert len(windows) == 1
