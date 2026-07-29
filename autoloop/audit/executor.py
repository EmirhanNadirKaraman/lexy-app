"""The audit executor — the only production TaskExecutor in Phase 3.

Supports exactly two directives: `audit` and `revise` of the audit pseudo-task
(feedback → another pass). Everything else returns an error outcome (policy
blocks it upstream; this is defense in depth).

Orchestration model: the MAIN executor owns sequencing, validation, agent
fan-out, reconciliation, Markdown output and the task-graph proposal.
Subagents (read-only `claude -p` invocations, see agents.py) only analyze and
return structured findings — they cannot edit files or delegate further.

Side effects per run, all captured by the task-owned change manifest:
* `.autoloop/audit/<run-id>/` — raw agent outputs, parsed findings,
  reconciliation, proposed tasks (JSON; never committed — gitignored).
* `docs/AUDIT_<date>.md` — the ONE new Markdown file (via MarkdownPolicy).
"""

from __future__ import annotations

import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ..contract import AUDIT_TASK_ID, Decision, Directive
from ..executor import ExecutionOutcome
from ..git_gateway import GitGateway
from ..tasks import Task, TaskRegistry
from .agents import AgentResult, AgentRunner, AgentSpec
from .findings import FINDINGS_SCHEMA_TEXT, parse_findings
from .markdown import MarkdownPolicy
from .reconcile import reconcile
from .report import ValidationRun, render_report
from .taskgen import generate_tasks

# Validation commands may only start with these binaries — the audit runs
# repo checks, never repo mutations.
SAFE_VALIDATION_BINARIES = frozenset(
    {"ruff", "pytest", "python", "python3", "npm", "npx", "tsc"}
)

DEFAULT_DOMAINS: tuple[tuple[str, str, str], ...] = (
    (
        "repo_structure",
        "Architecture & repository structure",
        "Map the actual architecture against what CLAUDE.md and docs/SUMMARY.md "
        "claim. Look for: dead or orphaned modules, duplicated responsibilities, "
        "layering violations (routers writing state directly, services importing "
        "routers), the two-backend split (lexy-app/backend vs root pipeline "
        "modules), and import-time side effects.",
    ),
    (
        "tests_ci",
        "Tests, linting, CI and validation",
        "Assess test coverage honestly against docs/TESTS.md. Look for: suites "
        "not run by any documented command, tests that can never fail, xdist "
        "isolation hazards, the pytest-vs-python-m-pytest sys.path trap, ruff "
        "scope gaps, CI workflows that don't run what the docs claim.",
    ),
    (
        "security_paths",
        "Security, path handling and data integrity",
        "Read docs/SECURITY.md first; verify open findings and 'verified "
        "strengths' still hold. Look for: raw SQL, path traversal around "
        "uploads/document packages, subprocess call sites, auth/ownership "
        "filters on routers with path params, JSONB settings whitelist, LLM "
        "prompt-injection surfaces.",
    ),
    (
        "db_migrations",
        "Database and migration safety",
        "Review alembic migrations for: edited-after-merge migrations, "
        "destructive operations without guards, drift between models/schemas.py "
        "and the migration chain, missing indexes implied by hot queries, and "
        "irreversible data transformations.",
    ),
    (
        "docs_drift",
        "Documentation drift",
        "Compare CLAUDE.md, docs/SUMMARY.md, docs/TESTS.md, docs/ROADMAP.md and "
        "docs/TODO.md against the code. Report claims that are demonstrably "
        "stale (files that moved/died, counts that changed, commands that no "
        "longer work). Cite the exact doc line and the contradicting code.",
    ),
    (
        "ingestion_pipeline",
        "Document-ingestion pipeline and A1/A2 status",
        "Review docs/INGESTION_PIPELINE.md and the shipped A1 (evaluation/) and "
        "A2 (services/document_package/, book_import_service) work. Look for: "
        "contract mismatches between the offline worker design and the import "
        "path, coordinate-space errors, validation gaps, and the import-path "
        "convention issue in test_document_package.py.",
    ),
)


def _agent_prompt(title: str, charter: str, scope: str | None, feedback: str | None) -> str:
    parts = [
        "You are ONE read-only audit agent inside an automated repository audit "
        "of this codebase (a German language-learning app; see CLAUDE.md).",
        f"Your domain: {title}.",
        charter,
        "Ground rules: you have READ-ONLY access (Read/Grep/Glob). Do not "
        "attempt to edit files, run commands, or delegate. Only report what you "
        "can support with specific evidence from files you actually read — "
        "quote paths and lines. Stay in your domain.",
    ]
    if scope:
        parts.append(f"Additional scope requested by the reviewer: {scope}")
    if feedback:
        parts.append(f"Revision feedback on the previous audit pass: {feedback}")
    parts.append(FINDINGS_SCHEMA_TEXT)
    return "\n\n".join(parts)


class AuditExecutor:
    def __init__(
        self,
        git: GitGateway,
        agent_runner: AgentRunner,
        markdown: MarkdownPolicy,
        registry: TaskRegistry,
        run_dir_base: Path,
        validation_commands: tuple[tuple[str, ...], ...] = (("ruff", "check", "."),),
        max_parallel_agents: int = 3,
        domains: tuple[tuple[str, str, str], ...] = DEFAULT_DOMAINS,
        command_runner=None,
    ):
        self._git = git
        self._agent_runner = agent_runner
        self._markdown = markdown
        self._registry = registry
        self._run_dir_base = Path(run_dir_base)
        self._validation_commands = validation_commands
        self._max_parallel = max(1, max_parallel_agents)
        self._domains = domains
        self._command_runner = command_runner or subprocess.run

    # ---- TaskExecutor -------------------------------------------------------

    def execute(self, directive: Directive, task: Task | None) -> ExecutionOutcome:
        is_audit = directive.decision is Decision.AUDIT
        is_audit_revision = (
            directive.decision is Decision.REVISE and directive.task_id == AUDIT_TASK_ID
        )
        if not (is_audit or is_audit_revision):
            return ExecutionOutcome(
                status="error",
                summary=(
                    "the audit executor supports only 'audit' and 'revise' of the "
                    f"audit in this phase — got '{directive.decision.value}'"
                    + (f" for task '{directive.task_id}'" if directive.task_id else "")
                ),
                validation="not run",
            )
        return self._run_audit(
            scope=directive.scope, feedback=directive.feedback if is_audit_revision else None
        )

    # ---- audit pipeline -----------------------------------------------------

    def _run_audit(self, scope: str | None, feedback: str | None) -> ExecutionOutcome:
        now = datetime.now(timezone.utc)
        date = now.strftime("%Y-%m-%d")
        run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        run_dir = self._run_dir_base / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        branch = self._git.current_branch()
        head = self._git.head_sha()
        dirty = len(self._git.dirty_files())

        validation_runs = [self._run_validation(cmd) for cmd in self._validation_commands]

        specs = [
            AgentSpec(domain=slug, title=title, prompt=_agent_prompt(title, charter, scope, feedback))
            for slug, title, charter in self._domains
        ]
        results = self._run_agents(specs, run_dir)

        findings, parse_rejects, agent_failures = [], [], []
        for result in results:
            if not result.ok:
                agent_failures.append(
                    f"{result.domain}: {result.error or f'rc={result.returncode}'}"
                )
                continue
            outcome = parse_findings(result.raw_text, result.domain)
            findings.extend(outcome.findings)
            parse_rejects.extend(outcome.rejected)

        reconciled = reconcile(findings, parse_rejects)
        proposal = generate_tasks(reconciled, self._registry)

        (run_dir / "reconciled.json").write_text(
            json.dumps(
                {
                    "buckets": {
                        b: [asdict(f) for f in fs] for b, fs in reconciled.buckets.items()
                    },
                    "rejected": [asdict(r) for r in reconciled.rejected],
                    "duplicates": reconciled.duplicates,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (run_dir / "proposed_tasks.json").write_text(
            json.dumps([t.to_dict() for t in proposal.tasks], indent=2), encoding="utf-8"
        )

        report = render_report(
            date=date,
            branch=branch,
            head_sha=head,
            dirty_count=dirty,
            scope=scope,
            feedback=feedback,
            validation_runs=validation_runs,
            reconciled=reconciled,
            proposal=proposal,
            agent_failures=agent_failures,
            raw_reports_dir=str(run_dir),
        )
        report_path = f"docs/AUDIT_{date}.md"
        self._markdown.write(report_path, report)

        accepted = len(reconciled.accepted())
        validation_summary = "; ".join(
            f"{run.command}: {'PASS' if run.returncode == 0 else 'FAIL'}"
            for run in validation_runs
        ) or "not run"
        summary = (
            f"Audit complete: {accepted} accepted findings "
            f"({len(reconciled.rejected)} rejected, {len(reconciled.duplicates)} deduped) "
            f"across {len(self._domains)} domains "
            f"({len(agent_failures)} agent failures); {len(proposal.tasks)} tasks proposed. "
            f"Report written to {report_path} (the only file this audit changed — "
            "approve that exact path if you commit)."
        )
        return ExecutionOutcome(
            status="ok" if not agent_failures else "error",
            summary=summary
            if not agent_failures
            else summary + " COVERAGE INCOMPLETE — see agent failures in the report.",
            details=report,
            validation=validation_summary,
        )

    def _run_agents(self, specs: list[AgentSpec], run_dir: Path) -> list[AgentResult]:
        raw_dir = run_dir / "raw"
        raw_dir.mkdir(exist_ok=True)
        with ThreadPoolExecutor(max_workers=self._max_parallel) as pool:
            results = list(pool.map(self._agent_runner.run, specs))
        for result in results:
            (raw_dir / f"{result.domain}.txt").write_text(
                result.raw_text or f"(no output; error: {result.error})", encoding="utf-8"
            )
            (raw_dir / f"{result.domain}.meta.json").write_text(
                json.dumps(
                    {
                        "domain": result.domain,
                        "returncode": result.returncode,
                        "duration_seconds": round(result.duration_seconds, 1),
                        "error": result.error,
                        "command": list(result.command),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return results

    def _run_validation(self, argv: tuple[str, ...]) -> ValidationRun:
        command = " ".join(argv)
        binary = Path(argv[0]).name if argv else ""
        if binary not in SAFE_VALIDATION_BINARIES:
            return ValidationRun(
                command=command,
                returncode=-1,
                tail=f"refused: '{binary}' is not a safe validation binary",
            )
        try:
            proc = self._command_runner(
                list(argv),
                cwd=str(self._git.repo_root),
                capture_output=True,
                text=True,
                timeout=1800,
            )
        except subprocess.TimeoutExpired:
            return ValidationRun(command=command, returncode=-1, tail="timed out")
        except FileNotFoundError:
            return ValidationRun(command=command, returncode=-1, tail="binary not found")
        output = (proc.stdout or "") + (proc.stderr or "")
        tail = output.strip().splitlines()[-1].strip() if output.strip() else ""
        return ValidationRun(command=command, returncode=proc.returncode, tail=tail[:200])
