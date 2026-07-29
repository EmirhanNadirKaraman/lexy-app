"""Reconciliation: bucket classification, cross-agent dedupe keeping the
higher-quality instance, and hard rejection of speculation and style."""

from autoloop.audit.findings import Finding
from autoloop.audit.reconcile import reconcile


def finding(fid="f1", domain="d1", category="defect", severity="high",
            confidence="confirmed", files=("a.py",), deps=()):
    return Finding(
        id=fid,
        category=category,
        severity=severity,
        confidence=confidence,
        affected_files=tuple(files),
        symbols=(),
        evidence="saw it",
        impact="bad",
        proposed_action="fix it",
        dependencies=tuple(deps),
        acceptance_criteria=("fixed",),
        validation_commands=("ruff check .",),
        safe_to_parallelize=True,
        domain=domain,
    )


def test_buckets_by_category():
    result = reconcile(
        [
            finding("f1", category="defect"),
            finding("f2", category="security", files=("b.py",)),
            finding("f3", category="data_loss", files=("c.py",)),
            finding("f4", category="architecture", files=("d.py",)),
            finding("f5", category="doc_drift", files=("e.md",)),
            finding("f6", category="missing_test", files=("f.py",)),
            finding("f7", category="improvement", files=("g.py",)),
            finding("f8", category="human_decision", files=("h.py",)),
        ]
    )
    assert [f.id for f in result.buckets["confirmed_defects"]] == ["f1"]
    assert sorted(f.id for f in result.buckets["security_risks"]) == ["f2", "f3"]
    assert [f.id for f in result.buckets["architectural_risks"]] == ["f4"]
    assert [f.id for f in result.buckets["documentation_drift"]] == ["f5"]
    assert [f.id for f in result.buckets["missing_tests"]] == ["f6"]
    assert [f.id for f in result.buckets["optional_improvements"]] == ["f7"]
    assert [f.id for f in result.buckets["human_decisions"]] == ["f8"]


def test_speculation_rejected_not_bucketed():
    result = reconcile([finding("f1", confidence="speculative")])
    assert result.accepted() == []
    assert "speculation" in result.rejected[0].reason


def test_style_rejected_not_bucketed():
    result = reconcile([finding("f1", category="style")])
    assert result.accepted() == []
    assert "stylistic" in result.rejected[0].reason


def test_cross_agent_duplicate_keeps_higher_quality():
    weaker = finding("a1", domain="agent_a", confidence="probable", severity="medium")
    stronger = finding("b1", domain="agent_b", confidence="confirmed", severity="high")
    result = reconcile([weaker, stronger])
    [kept] = result.buckets["confirmed_defects"]
    assert kept.qualified_id == "agent_b:b1"
    assert ("agent_a:a1", "agent_b:b1") in result.duplicates


def test_different_files_are_not_duplicates():
    result = reconcile([finding("f1", files=("a.py",)), finding("f2", files=("b.py",))])
    assert len(result.buckets["confirmed_defects"]) == 2


def test_promotable_excludes_human_decisions():
    result = reconcile(
        [finding("f1"), finding("f2", category="human_decision", files=("b.py",))]
    )
    assert [f.id for f in result.promotable()] == ["f1"]


def test_parse_rejects_carried_through():
    from autoloop.audit.findings import RejectedItem

    rejects = [RejectedItem("bad json", raw="x", domain="d")]
    result = reconcile([], rejects)
    assert result.rejected == rejects
