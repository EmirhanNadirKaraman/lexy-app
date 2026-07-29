"""Metric calculations.

Each metric is tested against a hand-constructed case where the right answer is
obvious by inspection. A metric that is only exercised end-to-end will report a
wrong number confidently and nothing will fail.
"""
import pytest

from evaluation.annotation import parse_gold_text
from evaluation.metrics import (
    boundary_metrics,
    boundary_offsets,
    build_offset_map,
    document_metrics,
    evaluate_document,
    is_complete_unit,
    reader_quality,
    summarize,
    text_fidelity,
    window_diff_and_pk,
)
from evaluation.model import PredictedDocument, PredictedSentence


def gold(*sentences: str, pages: str = "1"):
    body = "\n".join(sentences)
    return parse_gold_text(f"==== PAGE {pages} ====\n{body}\n", "t")


def predicted(*sentences: str, **kw) -> PredictedDocument:
    return PredictedDocument(
        document_id="t", sentences=[PredictedSentence(text=s, **kw) for s in sentences]
    )


class TestBoundaryOffsets:
    def test_offsets_exclude_document_end(self):
        # Both sides always agree on the end of the document; counting it as a
        # boundary would inflate every score.
        text, offsets = boundary_offsets(["Ab.", "Cd."])
        assert text == "Ab. Cd."
        assert offsets == [3]

    def test_single_sentence_has_no_boundaries(self):
        assert boundary_offsets(["Nur einer."])[1] == []

    def test_empty_input(self):
        assert boundary_offsets([]) == ("", [])

    def test_offsets_account_for_joining_space(self):
        text, offsets = boundary_offsets(["A.", "B.", "C."])
        assert text == "A. B. C."
        assert offsets == [2, 5]
        assert text[2] == " " and text[5] == " "

    def test_empty_parts_dropped_without_shifting(self):
        assert boundary_offsets(["A.", "", "B."]) == boundary_offsets(["A.", "B."])


class TestOffsetMap:
    def test_identical_strings_map_identically(self):
        mapping = build_offset_map("hallo welt", "hallo welt")
        assert all(mapping[i] == i for i in range(10))

    def test_insertion_shifts_later_offsets(self):
        mapping = build_offset_map("ab", "aXb")
        assert mapping[0] == 0

    def test_end_offset_always_present(self):
        mapping = build_offset_map("abc", "abcdef")
        assert mapping[3] == 3


class TestBoundaryMetrics:
    def test_perfect_match(self):
        g = gold("Erster Satz.", "Zweiter Satz.")
        p = predicted("Erster Satz.", "Zweiter Satz.")
        m = boundary_metrics(g, p)
        assert (m.precision, m.recall, m.f1) == (1.0, 1.0, 1.0)
        assert m.missed_splits == 0 and m.spurious_splits == 0

    def test_merge_is_a_missed_split(self):
        g = gold("Erster Satz.", "Zweiter Satz.")
        p = predicted("Erster Satz. Zweiter Satz.")
        m = boundary_metrics(g, p)
        assert m.missed_splits == 1
        assert m.spurious_splits == 0
        assert m.recall == 0.0

    def test_over_split_is_a_spurious_split(self):
        g = gold("Erster Satz und noch mehr.")
        p = predicted("Erster Satz", "und noch mehr.")
        m = boundary_metrics(g, p)
        assert m.spurious_splits == 1
        assert m.missed_splits == 0

    def test_precision_and_recall_are_directional(self):
        # Two gold boundaries, one found: recall 0.5, precision 1.0.
        g = gold("A ist da.", "B ist da.", "C ist da.")
        p = predicted("A ist da.", "B ist da. C ist da.")
        m = boundary_metrics(g, p)
        assert m.recall == 0.5
        assert m.precision == 1.0

    def test_counts_are_derived_from_one_matching(self):
        # matched + missed == gold, matched + spurious == predicted. If these
        # ever disagree, splits and merges are being counted independently.
        g = gold("A ist da.", "B ist da.", "C ist da.")
        p = predicted("A ist da. B ist da.", "C ist da.")
        m = boundary_metrics(g, p)
        assert m.matched + m.missed_splits == m.gold_boundaries
        assert m.matched + m.spurious_splits == m.predicted_boundaries

    def test_no_boundaries_on_either_side_scores_zero_not_one(self):
        g = gold("Nur ein Satz.")
        p = predicted("Nur ein Satz.")
        m = boundary_metrics(g, p)
        assert m.f1 == 0.0  # nothing to get right; must not read as perfect

    def test_tolerance_absorbs_off_by_one(self):
        g = gold("Ein Satz.", "Noch einer.")
        p = predicted("Ein Satz .", "Noch einer.")
        assert boundary_metrics(g, p, tolerance=2).missed_splits == 0


class TestWindowDiffAndPk:
    def test_identical_segmentations_score_zero(self):
        wd, pk, k = window_diff_and_pk([10, 20], [10, 20], 30)
        assert wd == 0.0 and pk == 0.0 and k is not None

    def test_disagreement_scores_above_zero(self):
        wd, _, _ = window_diff_and_pk([10, 20], [5, 25], 30)
        assert wd > 0

    def test_too_short_returns_none_not_zero(self):
        # Returning 0.0 here would look like a perfect score on a document
        # that was never actually measured.
        assert window_diff_and_pk([], [], 1) == (None, None, None)

    def test_k_is_derived_from_segment_length(self):
        _, _, k_short = window_diff_and_pk([10], [10], 20)
        _, _, k_long = window_diff_and_pk([100], [100], 200)
        assert k_long > k_short


class TestTextFidelity:
    def test_identical(self):
        assert text_fidelity("hallo", "hallo") == (1.0, 0, 0)

    def test_missing_and_extra_are_reported_separately(self):
        # They have opposite fixes, so a single similarity number would hide
        # which one is happening.
        _, missing, extra = text_fidelity("abcdef", "abc")
        assert missing == 3 and extra == 0
        _, missing, extra = text_fidelity("abc", "abcdef")
        assert missing == 0 and extra == 3


class TestDocumentMetrics:
    def test_duplicate_units_counted(self):
        g = gold("Ein Satz.")
        p = predicted("Ein Satz.", "Ein Satz.", "Ein Satz.")
        assert document_metrics(g, p).duplicated_units == 2

    def test_removal_absorbed_into_a_sentence_still_counts_as_leaked(self):
        # The important case: an artifact swallowed into real prose corrupts
        # reading material, which is worse than a standalone bad unit.
        g = parse_gold_text("==== PAGE 1 ====\nDer Hof war still.\n", "t")
        g.meta = {"expected_removals": [{"text": "Kapitel 2", "kind": "heading"}]}
        p = predicted("Der Hof Kapitel 2 war still.")
        m = document_metrics(g, p)
        assert m.removals_leaked == 1
        assert m.removals_leaked_as_units == 0

    def test_clean_removal_is_not_leaked(self):
        g = parse_gold_text("==== PAGE 1 ====\nDer Hof war still.\n", "t")
        g.meta = {"expected_removals": [{"text": "Kapitel 2", "kind": "heading"}]}
        assert document_metrics(g, predicted("Der Hof war still.")).removals_leaked == 0


class TestReaderQuality:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("Ein vollständiger Satz.", True),
            ("Eine Frage?", True),
            ("»Ein Zitat«", True),
            ("Ein Fragment ohne Ende", False),
            ("7", False),
            ("*", False),
            ("a", False),
        ],
    )
    def test_is_complete_unit(self, text, expected):
        assert is_complete_unit(text) is expected

    def test_fragments_counted(self):
        g = gold("Ein Satz.")
        p = predicted("Ein Satz.", "ein Fragment ohne Ende")
        m = reader_quality(g, p)
        assert m.fragment_units == 1
        assert m.complete_units == 1

    def test_bare_numbers_and_punctuation_flagged(self):
        g = gold("Ein Satz.")
        m = reader_quality(g, predicted("Ein Satz.", "42", "***"))
        assert m.bare_number_units == 1
        assert m.punctuation_only_units == 1

    def test_page_spanning_recovery(self):
        g = parse_gold_text(
            "==== PAGE 1 ====\nEin Satz über <PB/> zwei Seiten.\n", "t"
        )
        assert reader_quality(g, predicted("Ein Satz über zwei Seiten.")).page_spanning_recovered == 1
        assert reader_quality(g, predicted("Ein Satz über", "zwei Seiten.")).page_spanning_recovered == 0

    def test_ordering_errors_detected(self):
        g = gold("Erstens A.", "Zweitens B.", "Drittens C.")
        in_order = reader_quality(g, predicted("Erstens A.", "Zweitens B.", "Drittens C."))
        swapped = reader_quality(g, predicted("Drittens C.", "Erstens A.", "Zweitens B."))
        assert in_order.ordering_errors == 0
        assert swapped.ordering_errors == 1

    def test_provenance_is_none_when_source_supplies_none(self):
        # None, not 0.0 — a missing capability must not read as a measured
        # failure.
        g = gold("Ein Satz.")
        m = reader_quality(g, predicted("Ein Satz."))
        assert m.page_provenance_coverage is None
        assert m.block_provenance_coverage is None

    def test_provenance_coverage_when_supplied(self):
        g = gold("Ein Satz.")
        p = predicted("Ein Satz.", source_pages=(1,), source_block_ids=("b1",))
        m = reader_quality(g, p)
        assert m.page_provenance_coverage == 1.0
        assert m.block_provenance_coverage == 1.0


class TestSummarize:
    def test_empty(self):
        assert summarize([]) == {"documents": 0}

    def test_micro_averages_boundaries(self):
        # A 1-boundary fixture must not outweigh a 3-boundary one.
        big = evaluate_document(
            gold("A ist da.", "B ist da.", "C ist da.", "D ist da."),
            predicted("A ist da.", "B ist da.", "C ist da.", "D ist da."),
        )
        small = evaluate_document(gold("X ist da.", "Y ist da."), predicted("X ist da. Y ist da."))
        s = summarize([big, small])
        assert s["gold_boundaries"] == 4
        assert s["missed_splits"] == 1
        # 3 matched of 4 gold boundaries = 0.75. A macro-average would give
        # (1.0 + 0.0) / 2 = 0.5, letting the 1-boundary fixture dominate.
        assert s["boundary_recall"] == 0.75
