"""Adapter behaviour, including the join/no-join contrast the harness exists to measure."""
import json

import pytest

from evaluation.adapters import BlockJsonSource, DoclingLayoutJsonSource


def write_blocks(directory, doc_id, blocks):
    (directory / f"{doc_id}.blocks.json").write_text(
        json.dumps({"document_id": doc_id, "blocks": blocks}), encoding="utf-8"
    )


class TestBlockJsonSource:
    def test_discovers_and_sorts(self, tmp_path):
        write_blocks(tmp_path, "b", [{"text": "Ein Satz."}])
        write_blocks(tmp_path, "a", [{"text": "Ein Satz."}])
        assert list(BlockJsonSource(tmp_path).available_documents()) == ["a", "b"]

    def test_missing_document_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            BlockJsonSource(tmp_path).load("nope")

    def test_non_prose_types_excluded(self, tmp_path):
        write_blocks(
            tmp_path,
            "d",
            [
                {"block_id": "b1", "page": 1, "type": "PICTURE", "text": ""},
                {"block_id": "b2", "page": 1, "type": "CAPTION", "text": "Abbildung 1"},
                {"block_id": "b3", "page": 1, "type": "TEXT", "text": "Der Hof war still."},
            ],
        )
        doc = BlockJsonSource(tmp_path).load("d")
        assert "Abbildung" not in doc.continuous_text
        assert doc.diagnostics["blocks_prose"] == 1

    def test_checkbox_types_are_treated_as_prose(self, tmp_path):
        # On the real corpus 1307 CHECKBOX_UNSELECTED blocks hold ordinary
        # German words. Filtering them out would hide a real misclassification
        # behind the adapter instead of measuring it.
        write_blocks(
            tmp_path,
            "d",
            [{"block_id": "b1", "page": 1, "type": "CHECKBOX_UNSELECTED", "text": "Der Hof war still."}],
        )
        assert "Hof" in BlockJsonSource(tmp_path).load("d").continuous_text

    def test_join_recovers_a_sentence_split_across_blocks(self, tmp_path):
        write_blocks(
            tmp_path,
            "d",
            [
                {"block_id": "b1", "page": 1, "type": "TEXT", "text": "Der Hund"},
                {"block_id": "b2", "page": 1, "type": "TEXT", "text": "schlief tief."},
            ],
        )
        joined = BlockJsonSource(tmp_path, join_across_blocks=True).load("d")
        per_block = BlockJsonSource(tmp_path, join_across_blocks=False).load("d")
        assert [s.text for s in joined.sentences] == ["Der Hund schlief tief."]
        # Per-block segmentation cannot recover it — this is the fragment
        # behaviour a block-per-unit reader produces today.
        assert len(per_block.sentences) == 2

    def test_dehyphenation_applied_when_joining(self, tmp_path):
        write_blocks(
            tmp_path,
            "d",
            [
                {"block_id": "b1", "page": 1, "type": "TEXT", "text": "Er hatte los-"},
                {"block_id": "b2", "page": 1, "type": "TEXT", "text": "gelassen."},
            ],
        )
        assert "losgelassen" in BlockJsonSource(tmp_path).load("d").continuous_text

    def test_drop_bare_numbers_option(self, tmp_path):
        write_blocks(
            tmp_path,
            "d",
            [
                {"block_id": "b1", "page": 1, "type": "TEXT", "text": "Der Hof war still."},
                {"block_id": "b2", "page": 1, "type": "TEXT", "text": "42"},
            ],
        )
        kept = BlockJsonSource(tmp_path, drop_bare_numbers=False).load("d")
        dropped = BlockJsonSource(tmp_path, drop_bare_numbers=True).load("d")
        assert kept.diagnostics["blocks_prose"] == 2
        assert dropped.diagnostics["blocks_prose"] == 1

    def test_provenance_is_populated(self, tmp_path):
        write_blocks(
            tmp_path,
            "d",
            [{"block_id": "b1", "page": 3, "type": "TEXT", "text": "Der Hof war still."}],
        )
        doc = BlockJsonSource(tmp_path).load("d")
        assert doc.provides_page_provenance
        assert doc.sentences[0].source_pages == (3,)
        assert doc.sentences[0].source_block_ids == ("b1",)

    def test_empty_document_yields_no_sentences(self, tmp_path):
        write_blocks(tmp_path, "d", [])
        assert BlockJsonSource(tmp_path).load("d").sentences == []


class TestDoclingLayoutJsonSource:
    def test_reads_elements_key(self, tmp_path):
        (tmp_path / "book_layout.json").write_text(
            json.dumps(
                {"elements": [{"page": 1, "type": "TEXT", "text": "Der Hof war still."}]}
            ),
            encoding="utf-8",
        )
        source = DoclingLayoutJsonSource(tmp_path)
        assert list(source.available_documents()) == ["book"]
        assert "Hof" in source.load("book").continuous_text

    def test_missing_directory_is_empty_not_an_error(self, tmp_path):
        # The real corpus is gitignored; the committed suite must survive its
        # absence rather than erroring.
        assert list(DoclingLayoutJsonSource(tmp_path / "nope").available_documents()) == []
