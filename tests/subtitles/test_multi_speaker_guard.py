"""
test_multi_speaker_guard.py
---------------------------
Focused tests for the two multi-speaker safeguards in SubtitleMerger.
"""

from app.subtitles.merging import SubtitleMergeConfig, SubtitleMerger
from app.subtitles.models import SubtitleFragment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def frag(text: str, start: float, end: float) -> SubtitleFragment:
    return SubtitleFragment(text=text, start_time=start, end_time=end)


def merge(fragments: list[SubtitleFragment], **cfg_kwargs) -> list:
    config = SubtitleMergeConfig(**cfg_kwargs) if cfg_kwargs else SubtitleMergeConfig()
    return SubtitleMerger(config).merge_fragments(fragments)


# ---------------------------------------------------------------------------
# TestDialogueDashVeto
# ---------------------------------------------------------------------------

class TestDialogueDashVeto:

    def test_dash_prefix_overrides_soft_signal(self):
        windows = merge([
            frag("Wenn du möchtest,", 0.0, 1.2),
            frag("- Können wir morgen reden.", 1.4, 2.5),
        ])
        assert len(windows) == 2

    def test_dash_prefix_overrides_tiny_gap_unconditional_merge(self):
        windows = merge([
            frag("Sie hat das alles wirklich sehr gut gemacht.", 0.0, 2.0),
            frag("- Wirklich?", 2.05, 2.5),
        ])
        assert len(windows) == 2

    def test_en_dash_prefix_blocked(self):
        windows = merge([
            frag("Das war schön.", 0.0, 1.0),
            frag("– Danke schön.", 1.2, 1.9),
        ])
        assert len(windows) == 2

    def test_em_dash_prefix_blocked(self):
        windows = merge([
            frag("Das war schön.", 0.0, 1.0),
            frag("— Danke schön.", 1.2, 1.9),
        ])
        assert len(windows) == 2

    def test_three_consecutive_dash_turns_produce_three_windows(self):
        windows = merge([
            frag("- Das ist doch lächerlich.", 0.0, 1.2),
            frag("- Wieso das?", 1.25, 1.8),
            frag("- Na ja, du weißt schon.", 1.85, 2.8),
        ])
        assert len(windows) == 3

    def test_dash_guard_only_checks_next_fragment_not_current(self):
        windows = merge([
            frag("- Er hat das Buch wirklich gut gelesen.", 0.0, 2.0),
            frag("Interessant.", 2.05, 2.5),
        ])
        assert len(windows) == 1

    def test_html_wrapped_dash_detected(self):
        windows = merge([
            frag("Das war wirklich sehr schön so.", 0.0, 1.5),
            frag("<i>- Wirklich?</i>", 1.55, 2.0),
        ])
        assert len(windows) == 2

    def test_dash_guard_disabled_falls_through_to_other_logic(self):
        windows = merge(
            [
                frag("Sie hat das Buch wirklich sehr gelesen.", 0.0, 2.0),
                frag("- Interessant.", 2.05, 2.5),
            ],
            guard_dialogue_dash=False,
            guard_short_complete_turn=False,
        )
        assert len(windows) == 1


# ---------------------------------------------------------------------------
# TestSameSpeakerContinuation
# ---------------------------------------------------------------------------

class TestSameSpeakerContinuation:

    def test_comma_continuation_merges(self):
        windows = merge([
            frag("Ich gehe morgen ins Kino,", 0.0, 1.5),
            frag("wenn du mitkommen möchtest.", 1.7, 2.8),
        ])
        assert len(windows) == 1
        assert "wenn du mitkommen möchtest." in windows[0].text

    def test_lowercase_start_continuation_merges(self):
        windows = merge([
            frag("Das ist das Haus,", 0.0, 1.2),
            frag("in dem sie aufgewachsen ist.", 1.4, 2.5),
        ])
        assert len(windows) == 1

    def test_continuation_word_at_end_merges(self):
        windows = merge([
            frag("Er wartet schon seit einer Stunde auf", 0.0, 1.5),
            frag("seinen alten Freund.", 1.7, 2.5),
        ])
        assert len(windows) == 1

    def test_hyphen_word_break_across_fragments_merges(self):
        windows = merge([
            frag("Das ist sein absoluter Lieblings-", 0.0, 1.5),
            frag("film aller Zeiten.", 1.55, 2.5),
        ])
        assert len(windows) == 1
        assert "Lieblingsfilm" in windows[0].text

    def test_short_fragment_without_strong_punct_merges(self):
        windows = merge([
            frag("Und", 0.0, 0.3),
            frag("dann kam er endlich nach Hause.", 0.35, 1.5),
        ])
        assert len(windows) == 1

    def test_three_fragment_continuation_chain_fully_merges(self):
        windows = merge([
            frag("Sie hat das Buch,", 0.0, 0.8),
            frag("das er ihr geschenkt hatte,", 0.9, 1.8),
            frag("in einer Nacht gelesen.", 1.9, 2.8),
        ])
        assert len(windows) == 1
        assert len(windows[0].fragments) == 3


# ---------------------------------------------------------------------------
# TestShortCompleteTurnBoundary
# ---------------------------------------------------------------------------

class TestShortCompleteTurnBoundary:

    def test_question_answer_pair_at_tiny_gap_splits(self):
        windows = merge([
            frag("Kommst du mit?", 0.0, 0.8),
            frag("Ja, klar doch.", 0.85, 1.5),
        ])
        assert len(windows) == 2

    def test_exclamation_response_at_tiny_gap_splits(self):
        windows = merge([
            frag("Nein!", 0.0, 0.4),
            frag("Doch, ich komme!", 0.45, 1.0),
        ])
        assert len(windows) == 2

    def test_greeting_and_follow_up_question_split(self):
        windows = merge([
            frag("Guten Morgen!", 0.0, 0.7),
            frag("Wie geht es Ihnen?", 0.75, 1.5),
        ])
        assert len(windows) == 2

    def test_existing_sentence_boundary_at_normal_gap_still_works(self):
        windows = merge([
            frag("Das war wirklich fantastisch.", 0.0, 1.5),
            frag("Jetzt fahren wir nach Hause.", 1.8, 3.0),
        ])
        assert len(windows) == 2

    def test_guard_fires_at_exactly_max_complete_turn_words(self):
        windows = merge([
            frag("Das ist doch wirklich sehr schön.", 0.0, 1.2),
            frag("Stimmt.", 1.25, 1.6),
        ])
        assert len(windows) == 2

    def test_guard_does_not_fire_one_above_threshold(self):
        windows = merge([
            frag("Das ist doch wirklich sehr schön gewesen.", 0.0, 1.5),
            frag("Stimmt.", 1.55, 1.9),
        ])
        assert len(windows) == 1

    def test_lowercase_next_bypasses_guard(self):
        windows = merge([
            frag("Gut.", 0.0, 0.3),
            frag("weil du recht hast.", 0.35, 1.0),
        ])
        assert len(windows) == 1

    def test_custom_threshold_tightens_guard(self):
        windows = merge(
            [
                frag("Gut gemacht.", 0.0, 0.5),
                frag("Danke sehr.", 0.55, 1.0),
            ],
            max_complete_turn_words=2,
        )
        assert len(windows) == 2


# ---------------------------------------------------------------------------
# TestExistingMergesUnaffected
# ---------------------------------------------------------------------------

class TestExistingMergesUnaffected:

    def test_comma_ended_fragment_still_merges(self):
        windows = merge([
            frag("Er kommt nicht,", 0.0, 1.0),
            frag("Weil er krank ist.", 1.2, 2.2),
        ])
        assert len(windows) == 1

    def test_long_utterance_merges_unconditionally_at_tiny_gap(self):
        windows = merge([
            frag("Er hat das Buch wirklich sehr gut gelesen!", 0.0, 2.0),
            frag("Das war eine schöne Geschichte.", 2.05, 3.0),
        ])
        assert len(windows) == 1

    def test_fragment_ending_with_continuation_word_still_merges(self):
        windows = merge([
            frag("Wir fahren jetzt nach", 0.0, 1.0),
            frag("München.", 1.2, 1.8),
        ])
        assert len(windows) == 1

    def test_lowercase_continuation_after_strong_punct_still_merges(self):
        windows = merge([
            frag("Ich weiß das nicht.", 0.0, 1.2),
            frag("weil ich nicht dabei war.", 1.3, 2.2),
        ])
        assert len(windows) == 1

    def test_both_guards_disabled_restores_unconditional_tiny_gap_merge(self):
        windows = merge(
            [
                frag("Wirklich?", 0.0, 0.5),
                frag("Ja, natürlich.", 0.55, 1.1),
            ],
            guard_dialogue_dash=False,
            guard_short_complete_turn=False,
        )
        assert len(windows) == 1

    def test_continuation_chain_preserved_before_dash_turn(self):
        windows = merge([
            frag("Sie kommt morgen,", 0.0, 1.0),
            frag("wenn sie Zeit hat.", 1.2, 2.0),
            frag("- Gut.", 2.2, 2.6),
        ])
        assert len(windows) == 2
        assert "morgen" in windows[0].text
        assert "Zeit hat." in windows[0].text
        assert windows[1].text == "Gut."


# ---------------------------------------------------------------------------
# TestEdgeCases
# ---------------------------------------------------------------------------

class TestEdgeCases:

    def test_bare_dash_with_only_whitespace_does_not_trigger_guard(self):
        windows = merge([
            frag("Er kommt morgen,", 0.0, 1.0),
            frag("– ", 1.2, 1.4),
            frag("wenn er Zeit hat.", 1.5, 2.4),
        ])
        assert len(windows) >= 1

    def test_single_hyphen_fragment_does_not_trigger_guard(self):
        windows = merge([
            frag("Das war schön,", 0.0, 1.0),
            frag("-", 1.2, 1.3),
            frag("wirklich schön.", 1.4, 2.2),
        ])
        assert len(windows) >= 1

    def test_inline_dash_in_text_body_does_not_trigger_guard(self):
        windows = merge([
            frag("Er hat das Buch wirklich sehr gut gelesen.", 0.0, 2.0),
            frag("Gut—aber warum so schnell?", 2.05, 2.8),
        ])
        assert len(windows) == 1

    def test_gap_above_maximum_vetoed_before_guards_run(self):
        windows = merge([
            frag("- Das war gut.", 0.0, 1.0),
            frag("- Wirklich?", 1.8, 2.3),
        ])
        assert len(windows) == 2

    def test_five_fragment_realistic_sequence(self):
        windows = merge([
            frag("Ich gehe,", 0.0, 0.8),
            frag("weil es schon spät ist.", 0.9, 1.8),
            frag("- Schön.", 2.0, 2.4),
            frag("- Bis morgen!", 2.45, 2.9),
            frag("Tschüss!", 2.95, 3.3),
        ])
        assert len(windows) == 4
        assert "weil es schon spät ist." in windows[0].text
        assert windows[1].text == "Schön."
        assert windows[2].text == "Bis morgen!"
        assert windows[3].text == "Tschüss!"
