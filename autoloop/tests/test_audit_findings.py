"""Audit-agent output contract: strict per-item validation, rejection with
reasons, tolerance of prose around the JSON block."""

import json

from autoloop.audit.findings import Finding, parse_findings


def finding_dict(**overrides):
    base = {
        "id": "sec-01",
        "category": "security",
        "severity": "high",
        "confidence": "confirmed",
        "affected_files": ["lexy-app/backend/routers/books.py"],
        "symbols": ["import_package:200-230"],
        "evidence": "the route passes user input to resolve_within",
        "impact": "path traversal if the chokepoint regresses",
        "proposed_action": "add a regression test pinning resolve_within",
        "dependencies": [],
        "acceptance_criteria": ["test fails when resolve_within is bypassed"],
        "validation_commands": ["python3 -m pytest tests/test_document_package.py"],
        "safe_to_parallelize": True,
    }
    base.update(overrides)
    return base


def wrap(*items):
    return "Here is my report.\n```json\n" + json.dumps({"findings": list(items)}) + "\n```"


def test_valid_finding_parses():
    outcome = parse_findings(wrap(finding_dict()), domain="security_paths")
    assert outcome.rejected == []
    [finding] = outcome.findings
    assert isinstance(finding, Finding)
    assert finding.qualified_id == "security_paths:sec-01"
    assert finding.affected_files == ("lexy-app/backend/routers/books.py",)
    assert finding.safe_to_parallelize is True


def test_bare_array_accepted():
    outcome = parse_findings(json.dumps([finding_dict()]), domain="d")
    assert len(outcome.findings) == 1


def test_empty_findings_is_valid():
    outcome = parse_findings(wrap(), domain="d")
    assert outcome.findings == [] and outcome.rejected == []


def test_one_bad_item_does_not_sink_the_batch():
    outcome = parse_findings(
        wrap(finding_dict(), finding_dict(id="x", severity="catastrophic")), domain="d"
    )
    assert len(outcome.findings) == 1
    [rejected] = outcome.rejected
    assert "severity" in rejected.reason


def test_missing_key_rejected():
    item = finding_dict()
    del item["impact"]
    outcome = parse_findings(wrap(item), domain="d")
    assert outcome.findings == []
    assert "missing keys" in outcome.rejected[0].reason


def test_unknown_key_rejected():
    outcome = parse_findings(wrap(finding_dict(extra="nope")), domain="d")
    assert "unknown keys" in outcome.rejected[0].reason


def test_bad_category_rejected():
    outcome = parse_findings(wrap(finding_dict(category="vibes")), domain="d")
    assert "category" in outcome.rejected[0].reason


def test_bad_confidence_rejected():
    outcome = parse_findings(wrap(finding_dict(confidence="sure")), domain="d")
    assert "confidence" in outcome.rejected[0].reason


def test_empty_affected_files_rejected():
    outcome = parse_findings(wrap(finding_dict(affected_files=[])), domain="d")
    assert "affected_files" in outcome.rejected[0].reason


def test_non_bool_parallelize_rejected():
    outcome = parse_findings(wrap(finding_dict(safe_to_parallelize="yes")), domain="d")
    assert "safe_to_parallelize" in outcome.rejected[0].reason


def test_duplicate_ids_within_report_rejected():
    outcome = parse_findings(wrap(finding_dict(), finding_dict()), domain="d")
    assert len(outcome.findings) == 1
    assert "duplicate id" in outcome.rejected[0].reason


def test_non_json_output_rejected():
    outcome = parse_findings("I looked around and things seem fine!", domain="d")
    assert outcome.findings == []
    assert "not valid JSON" in outcome.rejected[0].reason


def test_wrong_top_level_shape_rejected():
    outcome = parse_findings(json.dumps({"results": []}), domain="d")
    assert "findings" in outcome.rejected[0].reason


def test_empty_output_rejected():
    outcome = parse_findings("", domain="d")
    assert "empty agent output" in outcome.rejected[0].reason
