"""Reconciliation of audit-agent findings.

Deterministic: dedupe across agents, classify into the report buckets, and
reject what must never become work — speculation and style opinions. Rejected
items stay visible in the report (with reasons); they are just never promoted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .findings import Finding, RejectedItem

# Report buckets, in presentation order.
BUCKETS = (
    "confirmed_defects",
    "security_risks",
    "architectural_risks",
    "documentation_drift",
    "missing_tests",
    "optional_improvements",
    "human_decisions",
)

_CATEGORY_TO_BUCKET = {
    "defect": "confirmed_defects",
    "security": "security_risks",
    "data_loss": "security_risks",
    "architecture": "architectural_risks",
    "doc_drift": "documentation_drift",
    "missing_test": "missing_tests",
    "improvement": "optional_improvements",
    "human_decision": "human_decisions",
}

_CONFIDENCE_RANK = {"confirmed": 0, "probable": 1, "speculative": 2}
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass
class ReconciledAudit:
    buckets: dict[str, list[Finding]] = field(
        default_factory=lambda: {name: [] for name in BUCKETS}
    )
    rejected: list[RejectedItem] = field(default_factory=list)
    duplicates: list[tuple[str, str]] = field(default_factory=list)  # (dropped, kept)

    def accepted(self) -> list[Finding]:
        return [f for bucket in BUCKETS for f in self.buckets[bucket]]

    def promotable(self) -> list[Finding]:
        """Findings that may become tasks: everything accepted except human
        decisions (those go to the operator/ChatGPT, not the executor)."""
        return [
            f
            for bucket in BUCKETS
            if bucket != "human_decisions"
            for f in self.buckets[bucket]
        ]


def _dedupe_key(finding: Finding) -> tuple:
    return (finding.category, frozenset(finding.affected_files))


def _quality(finding: Finding) -> tuple:
    return (
        _CONFIDENCE_RANK.get(finding.confidence, 9),
        _SEVERITY_RANK.get(finding.severity, 9),
    )


def reconcile(
    findings: list[Finding], parse_rejects: list[RejectedItem] | None = None
) -> ReconciledAudit:
    result = ReconciledAudit()
    result.rejected.extend(parse_rejects or [])

    # Dedupe: same category + same affected file set = the same underlying
    # issue seen by different agents. Keep the highest-quality instance.
    kept: dict[tuple, Finding] = {}
    for finding in findings:
        key = _dedupe_key(finding)
        existing = kept.get(key)
        if existing is None:
            kept[key] = finding
        elif _quality(finding) < _quality(existing):
            result.duplicates.append((existing.qualified_id, finding.qualified_id))
            kept[key] = finding
        else:
            result.duplicates.append((finding.qualified_id, existing.qualified_id))

    for finding in kept.values():
        if finding.category == "style":
            result.rejected.append(
                RejectedItem(
                    "stylistic preference — recorded, never promoted to a task",
                    raw=finding.qualified_id,
                    domain=finding.domain,
                )
            )
            continue
        if finding.confidence == "speculative":
            result.rejected.append(
                RejectedItem(
                    "unsupported speculation — recorded, never promoted to a task",
                    raw=finding.qualified_id,
                    domain=finding.domain,
                )
            )
            continue
        bucket = _CATEGORY_TO_BUCKET[finding.category]
        result.buckets[bucket].append(finding)

    for bucket in BUCKETS:
        result.buckets[bucket].sort(key=_quality)
    return result
