"""Gold-format parsing, and the round-trip that stops the format drifting."""
import json

import pytest

from evaluation.annotation import (
    AnnotationError,
    discover_document_ids,
    load_gold,
    parse_gold_text,
    serialize_gold_text,
)

SIMPLE = """\
==== PAGE 1 ====
Erster Satz.
Zweiter Satz.

Neuer Absatz beginnt.

==== PAGE 2 ====
Auf der zweiten Seite.
"""


class TestParse:
    def test_sentence_count_and_order(self):
        doc = parse_gold_text(SIMPLE, "t")
        assert [s.text for s in doc.sentences] == [
            "Erster Satz.",
            "Zweiter Satz.",
            "Neuer Absatz beginnt.",
            "Auf der zweiten Seite.",
        ]

    def test_blank_line_starts_new_paragraph(self):
        doc = parse_gold_text(SIMPLE, "t")
        assert [s.paragraph_index for s in doc.sentences] == [0, 0, 1, 2]
        assert doc.paragraph_count == 3

    def test_page_assignment(self):
        doc = parse_gold_text(SIMPLE, "t")
        assert [s.source_pages for s in doc.sentences] == [(1,), (1,), (1,), (2,)]
        assert doc.page_count == 2

    def test_sentence_indices_are_document_wide(self):
        doc = parse_gold_text(SIMPLE, "t")
        assert [s.sentence_index for s in doc.sentences] == [0, 1, 2, 3]

    def test_comments_ignored(self):
        doc = parse_gold_text("# a comment\n==== PAGE 1 ====\nSatz.\n", "t")
        assert len(doc.sentences) == 1

    def test_page_break_marker_yields_two_source_pages(self):
        doc = parse_gold_text(
            "==== PAGE 1 ====\nEin Satz über <PB/> zwei Seiten.\n", "t"
        )
        sentence = doc.sentences[0]
        assert sentence.source_pages == (1, 2)
        assert sentence.spans_pages
        assert "<PB/>" not in sentence.text
        # The marker leaves a single space behind, not a double one.
        assert sentence.text == "Ein Satz über zwei Seiten."

    def test_continuous_text_joins_with_single_space(self):
        doc = parse_gold_text(SIMPLE, "t")
        assert doc.continuous_text == (
            "Erster Satz. Zweiter Satz. Neuer Absatz beginnt. Auf der zweiten Seite."
        )


class TestParseErrors:
    def test_sentence_before_any_page_marker(self):
        with pytest.raises(AnnotationError, match="before any"):
            parse_gold_text("Satz ohne Seite.\n", "t")

    def test_non_increasing_page_number(self):
        text = "==== PAGE 2 ====\nA.\n==== PAGE 1 ====\nB.\n"
        with pytest.raises(AnnotationError, match="does not increase"):
            parse_gold_text(text, "t")

    def test_no_sentences(self):
        with pytest.raises(AnnotationError, match="no sentences"):
            parse_gold_text("==== PAGE 1 ====\n", "t")

    def test_error_names_the_line_number(self):
        with pytest.raises(AnnotationError, match=r":1:"):
            parse_gold_text("Satz ohne Seite.\n", "t")


class TestRoundTrip:
    @pytest.mark.parametrize(
        "text",
        [
            SIMPLE,
            "==== PAGE 1 ====\nNur ein Satz.\n",
            "==== PAGE 3 ====\nA.\nB.\n\nC.\n",
        ],
    )
    def test_parse_serialize_is_byte_identical(self, text):
        assert serialize_gold_text(parse_gold_text(text, "t")) == text

    def test_round_trip_preserves_page_spanning(self):
        text = "==== PAGE 1 ====\nEin Satz über zwei Seiten. <PB/>\n"
        doc = parse_gold_text(text, "t")
        again = parse_gold_text(serialize_gold_text(doc), "t")
        assert again.sentences[0].source_pages == (1, 2)

    def test_round_trip_of_every_committed_fixture(self, benchmark_dir):
        # The real guard: the committed corpus must survive the round trip, or
        # the format has drifted from the files people actually edit.
        for doc_id in discover_document_ids(benchmark_dir):
            original = (benchmark_dir / f"{doc_id}.gold.txt").read_text(
                encoding="utf-8"
            )
            doc = parse_gold_text(original, doc_id)
            reparsed = parse_gold_text(serialize_gold_text(doc), doc_id)
            assert [s.text for s in reparsed.sentences] == [
                s.text for s in doc.sentences
            ]
            assert [s.source_pages for s in reparsed.sentences] == [
                s.source_pages for s in doc.sentences
            ]


class TestLoading:
    def test_load_gold_reads_sidecar(self, benchmark_dir):
        doc = load_gold(benchmark_dir, "01_dialog_abkuerzungen")
        assert doc.phenomena
        assert doc.expected_removals
        assert any(r["kind"] == "running_header" for r in doc.expected_removals)

    def test_missing_gold_file_raises(self, tmp_path):
        with pytest.raises(AnnotationError, match="missing gold file"):
            load_gold(tmp_path, "nope")

    def test_invalid_sidecar_json_raises(self, tmp_path):
        (tmp_path / "x.gold.txt").write_text("==== PAGE 1 ====\nA.\n", encoding="utf-8")
        (tmp_path / "x.gold.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(AnnotationError, match="invalid JSON"):
            load_gold(tmp_path, "x")

    def test_sidecar_is_optional(self, tmp_path):
        (tmp_path / "x.gold.txt").write_text("==== PAGE 1 ====\nA.\n", encoding="utf-8")
        assert load_gold(tmp_path, "x").meta == {}

    def test_discover_is_sorted(self, benchmark_dir):
        ids = discover_document_ids(benchmark_dir)
        assert ids == sorted(ids)
        assert "01_dialog_abkuerzungen" in ids

    def test_discover_missing_directory_is_empty(self, tmp_path):
        assert discover_document_ids(tmp_path / "nope") == []


class TestCommittedCorpusIntegrity:
    """The fixtures are data; a typo in them silently corrupts every run."""

    def test_every_gold_has_matching_blocks_input(self, benchmark_dir):
        for doc_id in discover_document_ids(benchmark_dir):
            assert (benchmark_dir / f"{doc_id}.blocks.json").is_file(), doc_id

    def test_every_sidecar_is_valid_json_with_phenomena(self, benchmark_dir):
        for doc_id in discover_document_ids(benchmark_dir):
            payload = json.loads(
                (benchmark_dir / f"{doc_id}.gold.json").read_text(encoding="utf-8")
            )
            assert payload.get("phenomena"), doc_id

    def test_expected_removals_are_not_also_gold_sentences(self, benchmark_dir):
        # A string cannot be both something to delete and something to read.
        for doc_id in discover_document_ids(benchmark_dir):
            doc = load_gold(benchmark_dir, doc_id)
            sentences = {s.text for s in doc.sentences}
            for removal in doc.expected_removals:
                assert removal["text"] not in sentences, (doc_id, removal)
