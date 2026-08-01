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


def _quality(finding: Finding) -> tuple:
    return (
        _CONFIDENCE_RANK.get(finding.confidence, 9),
        _SEVERITY_RANK.get(finding.severity, 9),
    )


def _is_duplicate_candidate(a: Finding, b: Finding) -> bool:
    """Do these two look like the same underlying issue?

    Same category, plus either an identical file set (the original rule) or an
    overlap in BOTH files and symbols. Overlap is deliberately only a
    *candidate* test, never proof: two genuinely distinct defects routinely
    live in the same function. That is safe here only because merging keeps
    everything from both — see `_merge`. If merging ever became "keep the
    better one", this predicate would have to become much stricter.
    """
    if a.category != b.category:
        return False
    if frozenset(a.affected_files) == frozenset(b.affected_files):
        return True
    files_overlap = bool(set(a.affected_files) & set(b.affected_files))
    symbols_overlap = bool(set(a.symbols) & set(b.symbols))
    return files_overlap and symbols_overlap


def _merge_text(primary: str, other: str, other_id: str) -> str:
    """Keep both statements, attributed, unless the second adds nothing.

    Substring containment is the only "adds nothing" test used: anything
    cleverer would be a judgement call about meaning, and getting that wrong
    silently loses a finding's substance.
    """
    if not other or other in primary:
        return primary
    return f"{primary}\n  - also reported as {other_id}: {other}"


def _merge_unique(*sequences) -> tuple[str, ...]:
    """Union preserving first-seen order — no set(), which would scramble the
    order the agents chose and make reports nondeterministic."""
    seen, out = set(), []
    for sequence in sequences:
        for item in sequence:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return tuple(out)


def _merge(primary: Finding, other: Finding) -> Finding:
    """Fold `other` into `primary`, preserving every unique statement.

    The pre-2026-08-01 behaviour kept the higher-quality instance and discarded
    the other entirely, recording only a `(dropped, kept)` id pair. That threw
    away impacts, acceptance criteria and evidence that no other finding
    carried — a real loss that a reader of the report could not even detect.
    Merging is what makes the widened candidate test above safe.
    """
    best, worst = (primary, other) if _quality(primary) <= _quality(other) else (other, primary)
    return Finding(
        id=best.id,
        category=best.category,
        severity=best.severity,
        confidence=best.confidence,
        affected_files=_merge_unique(primary.affected_files, other.affected_files),
        symbols=_merge_unique(primary.symbols, other.symbols),
        evidence=_merge_text(best.evidence, worst.evidence, worst.qualified_id),
        impact=_merge_text(best.impact, worst.impact, worst.qualified_id),
        proposed_action=_merge_text(
            best.proposed_action, worst.proposed_action, worst.qualified_id
        ),
        dependencies=_merge_unique(primary.dependencies, other.dependencies),
        acceptance_criteria=_merge_unique(
            primary.acceptance_criteria, other.acceptance_criteria
        ),
        validation_commands=_merge_unique(
            primary.validation_commands, other.validation_commands
        ),
        # Conservative: if either instance says a change is unsafe to run in
        # parallel, the merged finding is unsafe. The optimistic direction
        # would let one agent's optimism override another's caution.
        safe_to_parallelize=primary.safe_to_parallelize and other.safe_to_parallelize,
        domain=best.domain,
    )


def reconcile(
    findings: list[Finding], parse_rejects: list[RejectedItem] | None = None
) -> ReconciledAudit:
    result = ReconciledAudit()
    result.rejected.extend(parse_rejects or [])

    # Dedupe by MERGING, never by discarding. Linear scan rather than a dict
    # key, because the candidate test is an overlap relation and not an
    # equality: two findings can both match a third without matching each
    # other, and a hash key cannot express that.
    kept: list[Finding] = []
    for finding in findings:
        for i, existing in enumerate(kept):
            if _is_duplicate_candidate(existing, finding):
                merged = _merge(existing, finding)
                dropped, survivor = (
                    (finding, existing)
                    if _quality(existing) <= _quality(finding)
                    else (existing, finding)
                )
                # "duplicates" now means "folded into", not "discarded" — the
                # dropped id no longer has its own block, but everything it
                # said is inside the surviving one.
                result.duplicates.append((dropped.qualified_id, survivor.qualified_id))
                kept[i] = merged
                break
        else:
            kept.append(finding)

    for finding in kept:
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
