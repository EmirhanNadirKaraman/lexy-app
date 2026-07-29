"""Human-readable reports and run-to-run comparison.

Two audiences, two formats. The JSON written by
:meth:`evaluation.runner.RunResult.write_json` is what tooling consumes; the
Markdown here is what a person reads. Both come from the same object, so they
can never disagree.
"""
from __future__ import annotations

import json
from pathlib import Path

from .annotation import GoldDocument
from .model import PredictedDocument
from .normalize import normalize_text
from .runner import RunResult

#: Summary fields where a *lower* number is better. Used to orient the arrows
#: in a comparison so a reader never has to remember which way is good.
LOWER_IS_BETTER = frozenset(
    {
        "spurious_splits",
        "missed_splits",
        "window_diff_mean",
        "pk_mean",
        "missing_chars",
        "extra_chars",
        "duplicated_units",
        "removals_leaked",
        "removals_leaked_as_units",
        "fragment_units",
        "ordering_errors",
        "false_deleted_chars",
        "false_deleted_sentences",
        "retained_artifact_chars",
        "unjustified_deletions",
        "information_loss_score",
    }
)

CRITICAL_METRICS = frozenset(
    {
        "content_preservation_rate",
        "false_deleted_chars",
        "false_deleted_sentences",
        "unjustified_deletions",
        "information_loss_score",
    }
)
"""Stage 1 information loss — irreversible, so ranked above everything else.

A regression here cannot be repaired by the downstream reviewer, which is why
:mod:`evaluation.compare` sorts these ahead of any boundary or reader metric
regardless of numeric magnitude. A 0.01 drop in preservation outranks a 0.2
drop in F1.
"""

NEUTRAL_METRICS = frozenset(
    {
        "documents",
        "gold_boundaries",
        "predicted_boundaries",
        "gold_chars",
        "total_units",
        "complete_units",
        "expected_removals",
        "reported_deletions",
        "uncertain_units",
        "artifacts_removed",
    }
)
"""Descriptive counts with no inherent direction.

Fewer predicted boundaries is not better or worse on its own — it is a
consequence of a segmentation choice whose quality is already scored by
precision/recall. Labelling these as improvements or regressions buries the
metrics that actually carry a verdict. ``uncertain_units`` is here deliberately:
flagging more units is neither good nor bad in itself, it is the *alternative
to deleting them*, and the benefit already shows up as higher preservation.
"""

RECOVERABLE_METRICS = frozenset(
    {
        "retained_artifact_chars",
        "removals_leaked",
        "removals_leaked_as_units",
        "extra_chars",
        "uncertain_units",
    }
)
"""Artifacts the pipeline kept. The reviewer can still delete these, so a
regression is genuinely low-severity — and is often the correct trade for
better content preservation."""

_STAGE1 = (
    "content_preservation_rate",
    "information_loss_score",
    "false_deleted_chars",
    "false_deleted_sentences",
    "unjustified_deletions",
    "reported_deletions",
    "artifact_removal_rate",
    "retained_artifact_chars",
    "uncertain_units",
    "reading_order_correctness",
)

_STAGE2 = (
    "boundary_f1",
    "boundary_precision",
    "boundary_recall",
    "window_diff_mean",
    "pk_mean",
    "complete_unit_rate",
    "text_similarity_mean",
    "removals_leaked",
    "fragment_units",
    "ordering_errors",
)

_HEADLINE = _STAGE1 + _STAGE2


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_report(run: RunResult, error_examples: int = 5) -> str:
    """Markdown report: summary, per-document table, then concrete errors."""
    summary = run.summary
    out: list[str] = []

    out.append(f"# Evaluation run — `{run.label}`")
    out.append("")
    out.append(f"- **Source:** `{run.source_name}`")
    out.append(f"- **Corpus:** `{run.corpus_dir}` (hash `{run.corpus_hash}`)")
    out.append(f"- **Segmenter:** {run.segmenter}")
    out.append(f"- **Boundary tolerance:** {run.tolerance} char(s)")
    out.append(f"- **Created:** {run.created_at}")
    out.append("")

    out.append("## Stage 1 — deterministic fidelity (information preservation)")
    out.append("")
    out.append(
        "> Losing content is **irreversible**: the reviewer only sees what this "
        "stage emitted. Retaining an artifact is recoverable. These are ranked "
        "first for that reason, not because they are larger numbers."
    )
    out.append("")
    out.append("| Metric | Value |")
    out.append("|---|---|")
    for key in _STAGE1:
        if key in summary:
            out.append(f"| `{key}` | {_fmt(summary[key])} |")
    out.append("")

    out.append("## Stage 2 — reader quality")
    out.append("")
    out.append("| Metric | Value |")
    out.append("|---|---|")
    for key in _STAGE2:
        if key in summary:
            out.append(f"| `{key}` | {_fmt(summary[key])} |")
    out.append(f"| `documents` | {summary.get('documents', 0)} |")
    out.append("")

    if run.skipped:
        out.append("## Skipped documents")
        out.append("")
        for item in run.skipped:
            out.append(f"- `{item['document_id']}` — {item['reason']}")
        out.append("")

    out.append("## Per-document")
    out.append("")
    out.append(
        "Sorted by information loss (worst first) — Stage 1 before Stage 2.\n"
    )
    out.append(
        "| Document | Preserved | Loss | Lost chars | Lost sents | F1 | "
        "WinDiff | Frag | Leaked |"
    )
    out.append("|---|---|---|---|---|---|---|---|---|")
    for r in sorted(
        run.results, key=lambda r: -r.fidelity.information_loss_score
    ):
        out.append(
            f"| `{r.document_id}` | "
            f"{r.fidelity.content_preservation_rate:.3f} | "
            f"{r.fidelity.information_loss_score:.3f} | "
            f"{r.fidelity.false_deleted_chars} | "
            f"{r.fidelity.false_deleted_sentences} | "
            f"{r.boundary.f1:.3f} | {_fmt(r.boundary.window_diff)} | "
            f"{r.reader.fragment_units} | {r.document.removals_leaked} |"
        )
    out.append("")

    weakest = sorted(run.results, key=lambda r: -r.fidelity.information_loss_score)[
        :error_examples
    ]
    if weakest:
        out.append(f"## Weakest {len(weakest)} document(s) — by information loss")
        out.append("")
        for r in weakest:
            out.append(
                f"### `{r.document_id}` — loss {r.fidelity.information_loss_score:.3f}, "
                f"preserved {r.fidelity.content_preservation_rate:.3f}"
            )
            if r.phenomena:
                out.append(f"Phenomena: {', '.join(f'`{p}`' for p in r.phenomena)}")
            out.append("")
            if r.fidelity.false_deleted_chars:
                out.append(
                    f"- **{r.fidelity.false_deleted_chars} char(s) of genuine content lost "
                    f"({r.fidelity.false_deleted_sentences} whole sentence(s))** — irreversible"
                )
            if r.fidelity.unjustified_deletions:
                out.append(
                    f"- **{r.fidelity.unjustified_deletions} deletion(s) removed text "
                    "that appears in gold**"
                )
            out.append(
                f"- retained artifacts {r.fidelity.retained_artifact_chars} char(s), "
                f"{r.fidelity.uncertain_units} unit(s) flagged uncertain (recoverable)"
            )
            out.append(f"- boundary F1 {r.boundary.f1:.3f}")
            out.append(
                f"- gold sentences {r.document.gold_sentences}, "
                f"predicted {r.document.predicted_sentences}"
            )
            out.append(
                f"- missed splits {r.boundary.missed_splits}, "
                f"spurious {r.boundary.spurious_splits}"
            )
            out.append(
                f"- fragments {r.reader.fragment_units}/{r.reader.total_units}, "
                f"bare numbers {r.reader.bare_number_units}"
            )
            out.append(
                f"- page-spanning recovered "
                f"{r.reader.page_spanning_recovered}/{r.reader.page_spanning_expected}"
            )
            if r.document.removals_leaked:
                out.append(f"- **{r.document.removals_leaked} expected removal(s) leaked**")
            out.append("")

    return "\n".join(out) + "\n"


def sentence_diff(
    gold: GoldDocument, predicted: PredictedDocument, limit: int = 10
) -> list[str]:
    """Concrete gold-vs-predicted mismatches, for eyeballing a failure.

    Not a metric — a debugging aid. Returns human-readable lines naming
    sentences present on one side only.
    """
    gold_units = [normalize_text(s.text) for s in gold.sentences]
    pred_units = [normalize_text(s.text) for s in predicted.sentences]
    gold_set, pred_set = set(gold_units), set(pred_units)

    lines: list[str] = []
    for unit in gold_units:
        if unit not in pred_set:
            lines.append(f"- MISSING (gold only): {unit!r}")
            if len(lines) >= limit:
                return lines
    for unit in pred_units:
        if unit not in gold_set:
            lines.append(f"- EXTRA (predicted only): {unit!r}")
            if len(lines) >= limit:
                return lines
    return lines


def write_report(run: RunResult, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(run), encoding="utf-8")
    return path


def load_run(path: Path) -> dict:
    """Load a run-result JSON written by :meth:`RunResult.write_json`."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
