"""Normalization is the foundation every offset-based metric stands on.

Tested directly rather than only through the metrics, because a normalization
bug shows up there as a plausible-looking wrong number rather than a failure.
"""
import unicodedata

from evaluation.normalize import dehyphenate, join_normalized, normalize_text


class TestNormalizeText:
    def test_collapses_all_whitespace_runs_to_single_space(self):
        assert normalize_text("a  b\t\tc\n\nd") == "a b c d"

    def test_strips_leading_and_trailing(self):
        assert normalize_text("  hallo  ") == "hallo"

    def test_newlines_become_spaces_not_removed(self):
        # A dropped newline would glue two words together and shift every
        # later offset by one character.
        assert normalize_text("erste\nzweite") == "erste zweite"

    def test_nfc_composition(self):
        decomposed = "u" + "̈" + "ber"  # u + combining diaeresis
        assert normalize_text(decomposed) == "über"
        assert unicodedata.is_normalized("NFC", normalize_text(decomposed))

    def test_soft_hyphen_removed(self):
        assert normalize_text("Fens­ter") == "Fenster"

    def test_zero_width_characters_removed(self):
        assert normalize_text("a​b‌c﻿") == "abc"

    def test_preserves_german_punctuation(self):
        # These ARE the phenomena under measurement — normalizing them away
        # would make the harness blind to the cases it exists to catch.
        text = "»Das ist … gut«, sagte sie – laut."
        assert normalize_text(text) == text

    def test_does_not_case_fold(self):
        assert normalize_text("Der Hof") == "Der Hof"

    def test_empty_and_whitespace_only(self):
        assert normalize_text("") == ""
        assert normalize_text("   \n\t ") == ""

    def test_idempotent(self):
        once = normalize_text("a  b\n c")
        assert normalize_text(once) == once


class TestJoinNormalized:
    def test_drops_empty_parts_before_joining(self):
        # An empty part must not contribute a double space; that would shift
        # every subsequent boundary offset by one.
        assert join_normalized(["eins", "", "  ", "zwei"]) == "eins zwei"

    def test_single_space_between_parts(self):
        assert join_normalized(["eins ", " zwei"]) == "eins zwei"

    def test_empty_list(self):
        assert join_normalized([]) == ""


class TestDehyphenate:
    def test_rejoins_lowercase_continuation(self):
        assert dehyphenate("Fens- ter") == "Fenster"

    def test_rejoins_across_newline(self):
        assert dehyphenate("los-\ngelassen") == "losgelassen"

    def test_leaves_real_compound_alone(self):
        # Uppercase after the hyphen means a genuine compound in German,
        # not a typesetter's line break.
        assert dehyphenate("Nord- Süd") == "Nord- Süd"

    def test_leaves_unbroken_hyphen_alone(self):
        assert dehyphenate("E-Mail") == "E-Mail"
