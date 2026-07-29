"""Report rendering and run-to-run comparison, including the guards."""
import json

import pytest

from evaluation import SCHEMA_VERSION
from evaluation.compare import (
    IncomparableRuns,
    check_comparable,
    compare_runs,
    document_deltas,
    render_comparison,
    summary_deltas,
)
from evaluation.report import load_run, render_report, write_report
from evaluation.runner import run_evaluation
from evaluation.adapters import BlockJsonSource


def base_run(**overrides) -> dict:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "label": "before",
        "source": "blocks_baseline",
        "corpus_hash": "abc123",
        "segmenter": "de_core_news_md",
        "corpus_dir": "benchmark/documents",
        "summary": {
            "content_preservation_rate": 1.0,
            "information_loss_score": 0.0,
            "boundary_f1": 0.9,
            "retained_artifact_chars": 10,
            "predicted_boundaries": 20,
        },
        "documents": [],
    }
    payload.update(overrides)
    return payload


class TestComparabilityGuards:
    def test_schema_mismatch_raises(self):
        with pytest.raises(IncomparableRuns, match="schema_version"):
            check_comparable(base_run(), base_run(schema_version="9.9.9"))

    def test_corpus_hash_mismatch_raises(self):
        # Otherwise an edited annotation reads as a code regression — the most
        # confusing way the harness could lie.
        with pytest.raises(IncomparableRuns, match="corpus_hash"):
            check_comparable(base_run(), base_run(corpus_hash="different"))

    def test_segmenter_mismatch_raises(self):
        with pytest.raises(IncomparableRuns, match="segmenter"):
            check_comparable(base_run(), base_run(segmenter="blank:de+sentencizer"))

    def test_non_strict_downgrades_to_no_raise(self):
        check_comparable(base_run(), base_run(corpus_hash="x"), strict=False)

    def test_different_source_is_only_a_warning(self):
        warnings = check_comparable(base_run(), base_run(source="docling_json"))
        assert any("different sources" in w for w in warnings)


class TestDeltas:
    def test_improvement_and_regression_direction(self):
        after = base_run(label="after")
        after["summary"]["boundary_f1"] = 0.95
        after["summary"]["retained_artifact_chars"] = 20
        deltas = {d.key: d for d in summary_deltas(base_run(), after)}
        assert deltas["boundary_f1"].improved is True
        assert deltas["retained_artifact_chars"].improved is False

    def test_neutral_counts_have_no_direction(self):
        # A descriptive count is neither an improvement nor a regression;
        # labelling it either way buries the metrics that carry a verdict.
        after = base_run(label="after")
        after["summary"]["predicted_boundaries"] = 12
        deltas = {d.key: d for d in summary_deltas(base_run(), after)}
        assert deltas["predicted_boundaries"].improved is None

    def test_critical_regression_sorts_above_larger_normal_one(self):
        after = base_run(label="after")
        after["summary"]["content_preservation_rate"] = 0.999  # tiny, critical
        after["summary"]["boundary_f1"] = 0.10  # huge, normal
        ordered = [d.key for d in summary_deltas(base_run(), after)]
        assert ordered.index("content_preservation_rate") < ordered.index("boundary_f1")

    def test_recoverable_regression_sorts_last(self):
        after = base_run(label="after")
        after["summary"]["retained_artifact_chars"] = 9999
        after["summary"]["boundary_f1"] = 0.89
        ordered = [d.key for d in summary_deltas(base_run(), after)]
        assert ordered.index("boundary_f1") < ordered.index("retained_artifact_chars")


class TestCompareRuns:
    def test_flags_irreversible_loss(self):
        after = base_run(label="after")
        after["summary"]["false_deleted_chars"] = 100
        after["summary"]["content_preservation_rate"] = 0.5
        result = compare_runs(base_run(), after)
        assert result["has_irreversible_loss"] is True
        assert "content_preservation_rate" in result["critical_regressions"]

    def test_no_loss_when_only_artifacts_regress(self):
        # Retaining more artifacts in exchange for better preservation is the
        # trade the architecture explicitly prefers.
        after = base_run(label="after")
        after["summary"]["retained_artifact_chars"] = 500
        result = compare_runs(base_run(), after)
        assert result["has_irreversible_loss"] is False

    def test_deltas_carry_severity(self):
        after = base_run(label="after")
        after["summary"]["boundary_f1"] = 0.95
        assert all("severity" in d for d in compare_runs(base_run(), after)["deltas"])

    def test_document_deltas_sorted_worst_first(self):
        before = base_run(
            documents=[
                {"document_id": "a", "boundary": {"f1": 0.9}},
                {"document_id": "b", "boundary": {"f1": 0.9}},
            ]
        )
        after = base_run(
            label="after",
            documents=[
                {"document_id": "a", "boundary": {"f1": 0.5}},
                {"document_id": "b", "boundary": {"f1": 0.95}},
            ],
        )
        assert [d.key for d in document_deltas(before, after)][0] == "a"


class TestRendering:
    def test_comparison_names_irreversible_regressions(self):
        after = base_run(label="after")
        after["summary"]["content_preservation_rate"] = 0.5
        text = render_comparison(base_run(), after)
        assert "IRREVERSIBLE" in text
        assert "content_preservation_rate" in text

    def test_comparison_of_identical_runs_reports_no_change(self):
        assert "No measured change" in render_comparison(base_run(), base_run(label="after"))

    def test_report_has_both_stages(self, benchmark_dir, tmp_path):
        run = run_evaluation(
            benchmark_dir, BlockJsonSource(benchmark_dir), label="t"
        )
        text = render_report(run)
        assert "Stage 1" in text and "Stage 2" in text
        assert "content_preservation_rate" in text
        assert "irreversible" in text.lower()

    def test_report_is_written_and_reloadable(self, benchmark_dir, tmp_path):
        run = run_evaluation(benchmark_dir, BlockJsonSource(benchmark_dir), label="t")
        md = write_report(run, tmp_path / "r.md")
        js = run.write_json(tmp_path / "r.json")
        assert md.read_text(encoding="utf-8").startswith("# Evaluation run")
        reloaded = load_run(js)
        assert reloaded["schema_version"] == SCHEMA_VERSION
        assert reloaded["summary"]["documents"] == len(run.results)

    def test_run_json_is_valid_and_embeds_baselines(self, benchmark_dir, tmp_path):
        run = run_evaluation(benchmark_dir, BlockJsonSource(benchmark_dir), label="t")
        payload = json.loads(
            run.write_json(tmp_path / "r.json").read_text(encoding="utf-8")
        )
        assert payload["frozen_baselines"]["values"]["is_relevant_para_drop_rate"][
            "value"
        ] == 30.5
