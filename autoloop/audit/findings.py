"""Strict output contract for audit agents.

Agents must emit one JSON object {"findings": [...]} (a bare array is also
accepted). Every finding is validated field-by-field; an item that fails
validation becomes a RejectedItem with the exact reason — it is never guessed
into shape, and one bad item does not sink the batch.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

SEVERITIES = ("critical", "high", "medium", "low", "info")
CONFIDENCES = ("confirmed", "probable", "speculative")
CATEGORIES = (
    "defect",
    "security",
    "data_loss",
    "architecture",
    "doc_drift",
    "missing_test",
    "improvement",
    "human_decision",
    "style",
)

_FINDING_KEYS = {
    "id",
    "category",
    "severity",
    "confidence",
    "affected_files",
    "symbols",
    "evidence",
    "impact",
    "proposed_action",
    "dependencies",
    "acceptance_criteria",
    "validation_commands",
    "safe_to_parallelize",
}

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

FINDINGS_SCHEMA_TEXT = """\
Return EXACTLY one JSON object (fenced in ```json ... ``` or bare):
{"findings": [
  {
    "id": "short unique id within your report, e.g. sec-01",
    "category": "defect | security | data_loss | architecture | doc_drift | missing_test | improvement | human_decision | style",
    "severity": "critical | high | medium | low | info",
    "confidence": "confirmed (you verified it in the code) | probable | speculative",
    "affected_files": ["repo-relative paths"],
    "symbols": ["function/class or file:line-range, e.g. progression_service.apply_progression:178-210"],
    "evidence": "what you actually saw — quote code/doc lines, not impressions",
    "impact": "what goes wrong and for whom",
    "proposed_action": "one concrete, scoped change",
    "dependencies": ["ids of findings that must be fixed first, usually []"],
    "acceptance_criteria": ["observable outcomes that prove the fix"],
    "validation_commands": ["exact commands that verify, e.g. 'ruff check .'"],
    "safe_to_parallelize": true
  }
]}
Rules: every field is required. Do not report style opinions as defects — use
category "style" and they will not become tasks. Use confidence "speculative"
honestly; speculation is recorded but never promoted. An empty {"findings": []}
is a valid answer for a clean domain."""


@dataclass(frozen=True)
class Finding:
    id: str
    category: str
    severity: str
    confidence: str
    affected_files: tuple[str, ...]
    symbols: tuple[str, ...]
    evidence: str
    impact: str
    proposed_action: str
    dependencies: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    validation_commands: tuple[str, ...]
    safe_to_parallelize: bool
    domain: str = ""

    @property
    def qualified_id(self) -> str:
        return f"{self.domain}:{self.id}" if self.domain else self.id


@dataclass(frozen=True)
class RejectedItem:
    reason: str
    raw: str
    domain: str = ""


@dataclass
class ParseOutcome:
    findings: list[Finding] = field(default_factory=list)
    rejected: list[RejectedItem] = field(default_factory=list)
    #: False when the agent's output as a WHOLE was unusable (empty, not JSON,
    #: wrong top-level shape). That is a coverage GAP for the domain, not a
    #: per-item rejection: the agent may have found real problems we never got
    #: to see. Callers must report it as a failure rather than as "0 findings",
    #: or an entire domain vanishes behind a clean-looking summary.
    usable: bool = True


def _str_list(value: object, name: str, allow_empty: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"'{name}' must be a list of strings")
    items = tuple(v.strip() for v in value if v.strip())
    if not items and not allow_empty:
        raise ValueError(f"'{name}' must not be empty")
    return items


def _validate_finding(item: dict, domain: str) -> Finding:
    unknown = set(item) - _FINDING_KEYS
    if unknown:
        raise ValueError(f"unknown keys: {sorted(unknown)}")
    missing = _FINDING_KEYS - set(item)
    if missing:
        raise ValueError(f"missing keys: {sorted(missing)}")
    for key in ("id", "evidence", "impact", "proposed_action"):
        if not isinstance(item[key], str) or not item[key].strip():
            raise ValueError(f"'{key}' must be a non-empty string")
    if item["category"] not in CATEGORIES:
        raise ValueError(f"category '{item['category']}' not in {CATEGORIES}")
    if item["severity"] not in SEVERITIES:
        raise ValueError(f"severity '{item['severity']}' not in {SEVERITIES}")
    if item["confidence"] not in CONFIDENCES:
        raise ValueError(f"confidence '{item['confidence']}' not in {CONFIDENCES}")
    if not isinstance(item["safe_to_parallelize"], bool):
        raise ValueError("'safe_to_parallelize' must be a boolean")
    return Finding(
        id=item["id"].strip(),
        category=item["category"],
        severity=item["severity"],
        confidence=item["confidence"],
        affected_files=_str_list(item["affected_files"], "affected_files", allow_empty=False),
        symbols=_str_list(item["symbols"], "symbols", allow_empty=True),
        evidence=item["evidence"].strip(),
        impact=item["impact"].strip(),
        proposed_action=item["proposed_action"].strip(),
        dependencies=_str_list(item["dependencies"], "dependencies", allow_empty=True),
        acceptance_criteria=_str_list(
            item["acceptance_criteria"], "acceptance_criteria", allow_empty=True
        ),
        validation_commands=_str_list(
            item["validation_commands"], "validation_commands", allow_empty=True
        ),
        safe_to_parallelize=item["safe_to_parallelize"],
        domain=domain,
    )


def parse_findings(text: str, domain: str) -> ParseOutcome:
    outcome = ParseOutcome()
    if not isinstance(text, str) or not text.strip():
        outcome.rejected.append(RejectedItem("empty agent output", raw="", domain=domain))
        outcome.usable = False
        return outcome
    blocks = _JSON_BLOCK.findall(text)
    candidate = blocks[-1] if blocks else text.strip()
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        outcome.rejected.append(
            RejectedItem(f"agent output is not valid JSON: {exc}", raw=text[:2000], domain=domain)
        )
        outcome.usable = False
        return outcome
    if isinstance(data, dict):
        if set(data) != {"findings"}:
            outcome.rejected.append(
                RejectedItem(
                    f"top-level object must have exactly the 'findings' key, got {sorted(data)}",
                    raw=candidate[:2000],
                    domain=domain,
                )
            )
            outcome.usable = False
            return outcome
        items = data["findings"]
    else:
        items = data
    if not isinstance(items, list):
        outcome.rejected.append(
            RejectedItem("'findings' must be a list", raw=candidate[:2000], domain=domain)
        )
        outcome.usable = False
        return outcome
    seen_ids: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            outcome.rejected.append(
                RejectedItem(f"findings[{i}] is not an object", raw=str(item)[:500], domain=domain)
            )
            continue
        try:
            finding = _validate_finding(item, domain)
        except ValueError as exc:
            outcome.rejected.append(
                RejectedItem(
                    f"findings[{i}]: {exc}", raw=json.dumps(item)[:500], domain=domain
                )
            )
            continue
        if finding.id in seen_ids:
            outcome.rejected.append(
                RejectedItem(
                    f"findings[{i}]: duplicate id '{finding.id}' within one report",
                    raw=finding.id,
                    domain=domain,
                )
            )
            continue
        seen_ids.add(finding.id)
        outcome.findings.append(finding)
    return outcome
