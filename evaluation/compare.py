"""Compare two evaluation runs.

The real deliverable of A1: a future developer must be able to answer *did this
change improve extraction / boundaries / reader quality / provenance* without
opening every document.

The comparison is **guarded**. Runs with different schema versions, corpus
hashes or segmenters are refused rather than diffed, because comparing unlike
things is worse than not comparing at all — it produces a confident number that
means nothing.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import SCHEMA_VERSION
from .report import (
    CRITICAL_METRICS,
    LOWER_IS_BETTER,
    NEUTRAL_METRICS,
    RECOVERABLE_METRICS,
    _fmt,
)

SEVERITY_CRITICAL = "critical"
SEVERITY_NORMAL = "normal"
SEVERITY_RECOVERABLE = "recoverable"

_SEVERITY_RANK = {
    SEVERITY_CRITICAL: 0,
    SEVERITY_NORMAL: 1,
    SEVERITY_RECOVERABLE: 2,
}


def severity_of(key: str) -> str:
    """Classify a metric by whether a regression in it can be undone later.

    ``critical`` = irreversible information loss; nothing downstream can
    recover it. ``recoverable`` = a retained artifact the multimodal reviewer
    can still delete. Everything else is ``normal``.

    This ordering is the architectural principle expressed as code: a
    deterministic stage must not be able to trade content preservation for a
    better boundary F1 and have the harness call it an improvement.
    """
    if key in CRITICAL_METRICS:
        return SEVERITY_CRITICAL
    if key in RECOVERABLE_METRICS:
        return SEVERITY_RECOVERABLE
    return SEVERITY_NORMAL


class IncomparableRuns(ValueError):
    """Raised when two runs must not be diffed. Always says which field differs."""


@dataclass
class Delta:
    key: str
    before: float | None
    after: float | None
    change: float | None
    improved: bool | None
    """``None`` when the metric has no direction or a value is missing."""

    @property
    def magnitude(self) -> float:
        return abs(self.change) if self.change is not None else 0.0

    @property
    def severity(self) -> str:
        return severity_of(self.key)

    @property
    def sort_key(self) -> tuple:
        """Severity class first, magnitude only as a tie-break within it."""
        return (_SEVERITY_RANK[self.severity], -self.magnitude)


def _direction(key: str, change: float | None) -> bool | None:
    if change is None or change == 0 or key in NEUTRAL_METRICS:
        return None
    return change < 0 if key in LOWER_IS_BETTER else change > 0


def check_comparable(before: dict, after: dict, strict: bool = True) -> list[str]:
    """Return warnings; raise :class:`IncomparableRuns` on a hard mismatch.

    Schema version and corpus hash are hard failures — the numbers genuinely
    are not comparable. A differing segmenter is also hard, since the
    full-model and fallback pipelines disagree on abbreviations and would show
    up as a boundary regression that no code change caused. Differing *source*
    is only a warning: comparing two pipelines on one corpus is the point.
    """
    for field in ("schema_version", "corpus_hash", "segmenter"):
        b, a = before.get(field), after.get(field)
        if b != a:
            message = f"{field} differs: {b!r} vs {a!r}"
            if strict:
                raise IncomparableRuns(message)

    warnings: list[str] = []
    if before.get("source") != after.get("source"):
        warnings.append(
            f"different sources: {before.get('source')!r} → {after.get('source')!r} "
            "(expected when comparing pipelines)"
        )
    if before.get("schema_version") != SCHEMA_VERSION:
        warnings.append(
            f"runs use schema {before.get('schema_version')}, "
            f"current is {SCHEMA_VERSION}"
        )
    return warnings


def summary_deltas(before: dict, after: dict) -> list[Delta]:
    """Per-metric deltas, largest magnitude first."""
    b_sum = before.get("summary", {})
    a_sum = after.get("summary", {})
    deltas: list[Delta] = []

    for key in sorted(set(b_sum) | set(a_sum)):
        b_val, a_val = b_sum.get(key), a_sum.get(key)
        if not isinstance(b_val, (int, float)) or not isinstance(a_val, (int, float)):
            continue
        change = a_val - b_val
        deltas.append(
            Delta(
                key=key,
                before=b_val,
                after=a_val,
                change=round(change, 4),
                improved=_direction(key, change),
            )
        )

    return sorted(deltas, key=lambda d: d.sort_key)


def document_deltas(before: dict, after: dict, metric: str = "f1") -> list[Delta]:
    """Per-document deltas on one boundary metric, largest regression first."""
    b_docs = {d["document_id"]: d for d in before.get("documents", [])}
    a_docs = {d["document_id"]: d for d in after.get("documents", [])}

    deltas: list[Delta] = []
    for doc_id in sorted(set(b_docs) | set(a_docs)):
        b_val = b_docs.get(doc_id, {}).get("boundary", {}).get(metric)
        a_val = a_docs.get(doc_id, {}).get("boundary", {}).get(metric)
        if b_val is None or a_val is None:
            deltas.append(Delta(doc_id, b_val, a_val, None, None))
            continue
        change = round(a_val - b_val, 4)
        deltas.append(Delta(doc_id, b_val, a_val, change, _direction(metric, change)))

    return sorted(deltas, key=lambda d: (d.change if d.change is not None else 0.0))


def render_comparison(
    before: dict, after: dict, strict: bool = True, top_n: int = 10
) -> str:
    """Markdown diff of two runs."""
    warnings = check_comparable(before, after, strict=strict)
    out: list[str] = []

    out.append(f"# Comparison — `{before.get('label')}` → `{after.get('label')}`")
    out.append("")
    out.append(f"- Corpus `{after.get('corpus_dir')}` (hash `{after.get('corpus_hash')}`)")
    out.append(f"- Segmenter: {after.get('segmenter')}")
    out.append("")
    for warning in warnings:
        out.append(f"> ⚠ {warning}")
    if warnings:
        out.append("")

    deltas = summary_deltas(before, after)
    improvements = [d for d in deltas if d.improved is True]
    regressions = [d for d in deltas if d.improved is False]

    critical_regressions = [d for d in regressions if d.severity == SEVERITY_CRITICAL]

    out.append("## Verdict")
    out.append("")
    if critical_regressions:
        out.append(
            f"🔴 **{len(critical_regressions)} IRREVERSIBLE regression(s)** — this change "
            "loses information the multimodal reviewer cannot recover:"
        )
        for d in critical_regressions:
            out.append(f"  - `{d.key}` {_fmt(d.before)} → {_fmt(d.after)} ({d.change:+})")
        out.append("")
    if not improvements and not regressions:
        out.append("No measured change.")
    else:
        out.append(
            f"- **{len(improvements)} improved**, **{len(regressions)} regressed** "
            f"({len(critical_regressions)} critical, "
            f"{sum(1 for d in regressions if d.severity == SEVERITY_RECOVERABLE)} recoverable)"
        )
    out.append("")

    if regressions:
        out.append("### Regressions — most severe first")
        out.append("")
        out.append(
            "Ordered by whether the loss can be undone downstream, not by "
            "numeric size.\n"
        )
        out.append("| Metric | Severity | Before | After | Change |")
        out.append("|---|---|---|---|---|")
        for d in regressions[:top_n]:
            out.append(
                f"| `{d.key}` | {d.severity} | {_fmt(d.before)} | "
                f"{_fmt(d.after)} | {d.change:+} |"
            )
        out.append("")

    if improvements:
        out.append("### Improvements")
        out.append("")
        out.append("| Metric | Severity | Before | After | Change |")
        out.append("|---|---|---|---|---|")
        for d in improvements[:top_n]:
            out.append(
                f"| `{d.key}` | {d.severity} | {_fmt(d.before)} | "
                f"{_fmt(d.after)} | {d.change:+} |"
            )
        out.append("")

    doc_deltas = [d for d in document_deltas(before, after) if d.change]
    if doc_deltas:
        out.append("### Per-document boundary F1")
        out.append("")
        out.append("| Document | Before | After | Change |")
        out.append("|---|---|---|---|")
        for d in doc_deltas[:top_n]:
            out.append(
                f"| `{d.key}` | {_fmt(d.before)} | {_fmt(d.after)} | {d.change:+} |"
            )
        out.append("")

    return "\n".join(out) + "\n"


def compare_runs(before: dict, after: dict, strict: bool = True) -> dict:
    """Machine-readable comparison, for CI gating."""
    warnings = check_comparable(before, after, strict=strict)
    deltas = summary_deltas(before, after)
    regressed = [d for d in deltas if d.improved is False]
    critical = [d.key for d in regressed if d.severity == SEVERITY_CRITICAL]
    return {
        "before": before.get("label"),
        "after": after.get("label"),
        "warnings": warnings,
        "improved": [d.key for d in deltas if d.improved is True],
        "regressed": [d.key for d in regressed],
        "critical_regressions": critical,
        "has_irreversible_loss": bool(critical),
        "deltas": [
            {
                "key": d.key,
                "severity": d.severity,
                "before": d.before,
                "after": d.after,
                "change": d.change,
                "improved": d.improved,
            }
            for d in deltas
        ],
    }
