"""End-to-end runs over a tiny corpus and over the committed benchmark."""
import pytest

from evaluation.adapters import BlockJsonSource
from evaluation.cli import main
from evaluation.runner import corpus_hash, run_evaluation


class TestTinyCorpus:
    def test_full_pipeline_produces_metrics(self, tiny_corpus):
        run = run_evaluation(tiny_corpus, BlockJsonSource(tiny_corpus), label="mini")
        assert len(run.results) == 1
        result = run.results[0]
        assert result.document_id == "mini"
        # 'Der Hund' + 'schlief.' arrive as separate blocks; joining recovers
        # the sentence, so nothing genuine should be lost.
        assert result.fidelity.false_deleted_sentences == 0
        assert result.fidelity.content_preservation_rate == 1.0

    def test_joining_beats_per_block_on_boundaries(self, tiny_corpus):
        joined = run_evaluation(
            tiny_corpus,
            BlockJsonSource(tiny_corpus, join_across_blocks=True),
            label="joined",
        )
        per_block = run_evaluation(
            tiny_corpus,
            BlockJsonSource(tiny_corpus, join_across_blocks=False),
            label="per_block",
        )
        assert joined.summary["boundary_f1"] >= per_block.summary["boundary_f1"]

    def test_dropping_page_numbers_removes_the_artifact(self, tiny_corpus):
        dropping = run_evaluation(
            tiny_corpus,
            BlockJsonSource(tiny_corpus, drop_bare_numbers=True),
            label="drop",
        )
        assert dropping.summary["artifact_removal_rate"] == 1.0
        # …and does not cost any genuine content.
        assert dropping.summary["false_deleted_chars"] == 0

    def test_missing_source_document_is_skipped_not_dropped(self, tiny_corpus, tmp_path):
        # A shrinking corpus must not look like an improving score.
        run = run_evaluation(tiny_corpus, BlockJsonSource(tmp_path / "empty"), label="x")
        assert run.results == []
        assert run.skipped and run.skipped[0]["document_id"] == "mini"


class TestCommittedBenchmark:
    def test_every_document_evaluates(self, benchmark_dir):
        run = run_evaluation(benchmark_dir, BlockJsonSource(benchmark_dir), label="bench")
        assert len(run.results) == 4
        assert not run.skipped

    def test_no_genuine_sentence_is_lost_by_the_baseline(self, benchmark_dir):
        # The baseline retains artifacts rather than deleting content, which is
        # exactly the behaviour the architecture asks for. If this starts
        # failing, a heuristic has become too aggressive.
        run = run_evaluation(benchmark_dir, BlockJsonSource(benchmark_dir), label="bench")
        assert run.summary["false_deleted_sentences"] == 0

    def test_reading_order_fault_is_detected(self, benchmark_dir):
        run = run_evaluation(benchmark_dir, BlockJsonSource(benchmark_dir), label="bench")
        by_id = {r.document_id: r for r in run.results}
        assert by_id["04_kapitel_layout"].reader.ordering_errors > 0

    def test_page_spanning_sentence_is_not_recovered_by_naive_join(self, benchmark_dir):
        # Pinned as a known limitation: a footer and page number sit between
        # the two halves, so naive concatenation cannot rejoin them. A4
        # (reconstruction) is what should flip this to 1/1.
        run = run_evaluation(benchmark_dir, BlockJsonSource(benchmark_dir), label="bench")
        by_id = {r.document_id: r for r in run.results}
        spanning = by_id["02_seitenumbruch"].reader
        assert spanning.page_spanning_expected == 1
        assert spanning.page_spanning_recovered == 0

    def test_corpus_hash_is_stable_and_content_sensitive(self, benchmark_dir, tmp_path):
        ids = ["01_dialog_abkuerzungen"]
        assert corpus_hash(benchmark_dir, ids) == corpus_hash(benchmark_dir, ids)
        (tmp_path / "01_dialog_abkuerzungen.gold.txt").write_text(
            "==== PAGE 1 ====\nAnders.\n", encoding="utf-8"
        )
        assert corpus_hash(tmp_path, ids) != corpus_hash(benchmark_dir, ids)


class TestCLI:
    def test_list_command(self, capsys):
        assert main(["list"]) == 0
        assert "01_dialog_abkuerzungen" in capsys.readouterr().out

    def test_run_writes_artifacts(self, tmp_path, benchmark_dir, capsys):
        code = main(
            ["run", "--label", "t", "--corpus", str(benchmark_dir), "--out", str(tmp_path)]
        )
        assert code == 0
        assert (tmp_path / "t.json").is_file()
        assert (tmp_path / "t.md").is_file()

    def test_compare_command(self, tmp_path, benchmark_dir, capsys):
        for label, extra in (("a", []), ("b", ["--no-join"])):
            main(
                ["run", "--label", label, "--corpus", str(benchmark_dir),
                 "--out", str(tmp_path)] + extra
            )
        capsys.readouterr()
        code = main(
            ["compare", str(tmp_path / "a.json"), str(tmp_path / "b.json"),
             "--json", str(tmp_path / "diff.json")]
        )
        assert code == 0
        assert (tmp_path / "diff.json").is_file()

    def test_compare_refuses_mismatched_corpora(self, tmp_path, benchmark_dir, capsys):
        main(["run", "--label", "a", "--corpus", str(benchmark_dir), "--out", str(tmp_path)])
        import json

        payload = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))
        payload["corpus_hash"] = "tampered"
        payload["label"] = "b"
        (tmp_path / "b.json").write_text(json.dumps(payload), encoding="utf-8")
        capsys.readouterr()
        assert main(["compare", str(tmp_path / "a.json"), str(tmp_path / "b.json")]) == 2

    def test_run_on_empty_corpus_fails_loudly(self, tmp_path, capsys):
        (tmp_path / "corpus").mkdir()
        assert main(["run", "--label", "t", "--corpus", str(tmp_path / "corpus"),
                     "--out", str(tmp_path)]) == 1

    def test_run_on_missing_corpus_exits_2(self, tmp_path):
        assert main(["run", "--label", "t", "--corpus", str(tmp_path / "nope"),
                     "--out", str(tmp_path)]) == 2


class TestSegmentation:
    @pytest.mark.parametrize(
        "text,expected_count",
        [
            ("Dr. Weber kam an. Er setzte sich.", 2),
            ("Das gilt z. B. für Kinder. Andere nicht.", 2),
            ("Sie kaufte Brot, Milch usw. Dann ging sie.", 2),
            ("Am 3. Oktober war Feiertag. Danach nicht.", 2),
            ("Sie zögerte … dann ging sie. Die Tür war offen.", 2),
        ],
    )
    def test_german_hard_cases(self, text, expected_count):
        from evaluation.segmentation import segment

        assert len(segment(text)) == expected_count

    def test_min_chars_drops_stray_units(self):
        from evaluation.segmentation import segment

        assert "7" not in segment("Ein Satz steht hier. 7")
