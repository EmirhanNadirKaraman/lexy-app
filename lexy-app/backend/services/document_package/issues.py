"""Validation issues, collected rather than raised one at a time.

A package that fails five independent checks should report five problems, not
the first one. Someone fixing a worker bug needs the whole list; making them
re-run the importer once per defect is a bad trade for slightly simpler code.

Three severities, because they have genuinely different consequences:

* ``fatal``   — the import must not proceed. No database mutation happens.
* ``warning`` — imported, but something needs a human or a later review pass.
  Warnings are *promoted* into the import result (§4) so they become review
  triggers rather than log noise.
* ``info``    — recorded for the report; never affects the outcome.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

FATAL = "fatal"
WARNING = "warning"
INFO = "info"

_ORDER = {FATAL: 0, WARNING: 1, INFO: 2}


@dataclass(frozen=True)
class ValidationIssue:
    """One problem, tied to a stable code and — where possible — a location."""

    code: str
    """Stable machine-readable slug, e.g. ``duplicate_element_id``.

    Codes are the contract with the report and any future CI gate; the
    human-readable ``message`` is free to change wording without breaking them.
    """
    message: str
    severity: str = FATAL
    stage: str = "unknown"
    """Which gate produced it: ``schema``/``checksums``/``containment``/
    ``structural``/``profile``."""
    element_id: str | None = None
    page_index: int | None = None
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        payload = asdict(self)
        return {k: v for k, v in payload.items() if v not in (None, {}, "")}


class IssueCollector:
    """Accumulates issues across gates and answers "may we write to the DB?".

    Deliberately not an exception: validators return normally and keep going, so
    one malformed element does not hide the twelve after it.
    """

    def __init__(self) -> None:
        self._issues: list[ValidationIssue] = []

    def add(
        self,
        code: str,
        message: str,
        *,
        severity: str = FATAL,
        stage: str = "unknown",
        element_id: str | None = None,
        page_index: int | None = None,
        **detail,
    ) -> ValidationIssue:
        issue = ValidationIssue(
            code=code,
            message=message,
            severity=severity,
            stage=stage,
            element_id=element_id,
            page_index=page_index,
            detail=detail,
        )
        self._issues.append(issue)
        return issue

    def extend(self, issues: list[ValidationIssue]) -> None:
        self._issues.extend(issues)

    # -- queries -----------------------------------------------------------

    @property
    def issues(self) -> list[ValidationIssue]:
        """All issues, most severe first, insertion order preserved within a tier."""
        return sorted(self._issues, key=lambda i: _ORDER.get(i.severity, 9))

    @property
    def fatal(self) -> list[ValidationIssue]:
        return [i for i in self._issues if i.severity == FATAL]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self._issues if i.severity == WARNING]

    @property
    def infos(self) -> list[ValidationIssue]:
        return [i for i in self._issues if i.severity == INFO]

    @property
    def ok(self) -> bool:
        """True when nothing fatal was found — i.e. persistence may proceed."""
        return not self.fatal

    def codes(self) -> set[str]:
        return {i.code for i in self._issues}

    def stage_status(self, stage: str) -> str:
        """``pass`` / ``warn`` / ``fail`` for one gate, for the import result."""
        relevant = [i for i in self._issues if i.stage == stage]
        if any(i.severity == FATAL for i in relevant):
            return "fail"
        if any(i.severity == WARNING for i in relevant):
            return "warn"
        return "pass"

    def as_list(self) -> list[dict]:
        return [i.as_dict() for i in self.issues]

    def __len__(self) -> int:
        return len(self._issues)

    def __bool__(self) -> bool:  # pragma: no cover - trivial
        return bool(self._issues)
