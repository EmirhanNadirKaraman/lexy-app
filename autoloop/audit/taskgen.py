"""Findings → proposed task graph.

The output is a PROPOSAL: it is embedded in the audit report for ChatGPT,
which adopts it (possibly edited) via a normal contract-v3 `plan` decision —
the registry is never mutated by the audit itself.

Priority reflects the mandated order: (1) irreversible data-loss/correctness,
(2) security, (3) dependency ordering (expressed as graph edges, not a rank),
(4) migration/architecture risk, (5) missing regression protection,
(6) maintainability, (7) documentation, (8) optional improvements.

Ids are `au-NNN`, which structurally cannot collide with the repo's existing
roadmap ids (A1–A11 etc.); collisions with the live task registry are checked
explicitly anyway.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..tasks import TaskRegistry
from .findings import Finding
from .reconcile import ReconciledAudit

_PRIORITY_BY_CATEGORY = {
    "data_loss": 1,
    "defect": 1,
    "security": 2,
    "architecture": 4,
    "missing_test": 5,
    "improvement": 6,
    "doc_drift": 7,
}
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

_RESERVED_ID = re.compile(r"^A\d+$", re.IGNORECASE)


@dataclass(frozen=True)
class ProposedTask:
    id: str
    title: str
    description: str
    priority: int
    depends_on: tuple[str, ...]
    scope: str
    non_goals: str
    acceptance_criteria: tuple[str, ...]
    validation_commands: tuple[str, ...]
    expected_files: tuple[str, ...]
    parallelizable: bool
    finding_ids: tuple[str, ...]

    def to_plan_dict(self) -> dict:
        """The shape ChatGPT re-emits inside a `plan` decision."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "depends_on": list(self.depends_on),
        }

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TaskGraphProposal:
    tasks: list[ProposedTask] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (finding id, reason)


def _title(finding: Finding) -> str:
    text = finding.proposed_action.strip().split("\n")[0]
    return text[:97] + "..." if len(text) > 100 else text


def _description(finding: Finding, task: dict) -> str:
    lines = [
        f"Priority: P{task['priority']} | severity {finding.severity} | "
        f"confidence {finding.confidence} | from audit finding {finding.qualified_id}",
        f"Scope: {finding.proposed_action}",
        "Non-goals: nothing beyond the scope above — no drive-by refactors, "
        "no changes to files outside the expected list without re-approval.",
        f"Impact if unfixed: {finding.impact}",
        f"Evidence: {finding.evidence}",
        f"Expected files: {', '.join(finding.affected_files)}",
    ]
    if finding.acceptance_criteria:
        lines.append("Acceptance criteria: " + "; ".join(finding.acceptance_criteria))
    if finding.validation_commands:
        lines.append("Validation: " + "; ".join(finding.validation_commands))
    lines.append(f"Parallelizable: {'yes' if finding.safe_to_parallelize else 'no'}")
    return "\n".join(lines)


def generate_tasks(reconciled: ReconciledAudit, registry: TaskRegistry) -> TaskGraphProposal:
    proposal = TaskGraphProposal()
    promotable = sorted(
        reconciled.promotable(),
        key=lambda f: (
            _PRIORITY_BY_CATEGORY.get(f.category, 8),
            _SEVERITY_RANK.get(f.severity, 9),
            f.qualified_id,
        ),
    )

    # First pass: assign ids in priority order.
    id_by_finding: dict[str, str] = {}
    seq = 1
    assigned: list[tuple[str, Finding]] = []
    for finding in promotable:
        while True:
            candidate = f"au-{seq:03d}"
            seq += 1
            if not registry.has(candidate) and not _RESERVED_ID.match(candidate):
                break
        id_by_finding[finding.qualified_id] = candidate
        # Agents reference dependencies by their local id; map both forms.
        id_by_finding.setdefault(finding.id, candidate)
        assigned.append((candidate, finding))

    # Second pass: resolve dependencies finding-id -> task-id.
    for task_id, finding in assigned:
        deps: list[str] = []
        unresolved: list[str] = []
        for dep in finding.dependencies:
            mapped = id_by_finding.get(dep)
            if mapped and mapped != task_id:
                deps.append(mapped)
            elif registry.has(dep):
                deps.append(dep)  # dependency on an existing roadmap task
            else:
                unresolved.append(dep)
        priority = _PRIORITY_BY_CATEGORY.get(finding.category, 8)
        task = {
            "priority": priority,
        }
        description = _description(finding, task)
        if unresolved:
            description += (
                f"\nNote: unresolved dependency references from the audit: {unresolved}"
            )
        proposal.tasks.append(
            ProposedTask(
                id=task_id,
                title=_title(finding),
                description=description,
                priority=priority,
                depends_on=tuple(dict.fromkeys(deps)),
                scope=finding.proposed_action,
                non_goals=(
                    "Nothing beyond the stated scope; no changes outside the "
                    "expected files without re-approval."
                ),
                acceptance_criteria=finding.acceptance_criteria,
                validation_commands=finding.validation_commands,
                expected_files=finding.affected_files,
                parallelizable=finding.safe_to_parallelize,
                finding_ids=(finding.qualified_id,),
            )
        )

    for bucket_finding in reconciled.buckets["human_decisions"]:
        proposal.skipped.append(
            (bucket_finding.qualified_id, "human decision — surfaced to the operator, not a task")
        )
    return proposal
