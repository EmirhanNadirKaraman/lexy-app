"""The audit executor — the only production TaskExecutor in Phase 3.

Supports exactly two directives: `audit` and `revise` of the audit pseudo-task
(feedback → another pass). Everything else returns an error outcome (policy
blocks it upstream; this is defense in depth).

Orchestration model: the MAIN executor owns sequencing, validation, agent
fan-out, reconciliation, Markdown output and the task-graph proposal.
Subagents (read-only `claude -p` invocations, see agents.py) only analyze and
return structured findings — they cannot edit files or delegate further.

Side effects per run:
* `.autoloop/audit/<run-id>/` — raw agent outputs, parsed findings,
  reconciliation, proposed tasks (JSON; never committed — gitignored). Always
  rooted at the MAIN checkout's `run_dir_base`, never at a worker repo — see
  the "produce-then-review" note below.
* `docs/AUDIT_<date>.md` — the ONE new Markdown file (via MarkdownPolicy).

**Produce-then-review (2026-07-30).** The audit is dispatched by the
orchestrator as a task-shaped unit of work (`Orchestrator._dispatch_task_postcommit`
with a synthetic `Task`, id like `audit-0007`), so it runs, writes its report,
and commits inside its OWN isolated worker repo — never the main checkout —
exactly like a real implementation task. `git` / `markdown` / `agent_runner`
passed to `__init__` are the STANDALONE defaults (used by every existing
direct-call test here, and whenever `task` is `None`); when the orchestrator
also supplies `worker_repo_root_for` (+ `policy`, +, optionally,
`agent_runner_factory`), `execute()` re-roots all three onto
`worker_repo_root_for(task.id)` for that one call. `run_dir_base` is
deliberately NEVER re-rooted — it must stay outside any worker repo's working
tree, or its raw-output files would show up as untracked residue and fail the
post-commit "worktree is clean" check.

One consequence worth flagging: since the worker repo is a fresh clone of the
main checkout's HEAD at `task_base_sha`, `dirty = len(git.dirty_files())` is
always 0 there, and any uncommitted work sitting in the MAIN checkout is
invisible to the audit — the audit only ever sees committed history.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..contract import AUDIT_TASK_ID, Decision, Directive
from ..errors import AuditError
from ..executor import ExecutionOutcome
from ..git_gateway import GitGateway
from ..policy import PolicyEngine
from ..tasks import Task, TaskRegistry
from ..validation import SAFE_VALIDATION_BINARIES
from ..worker_env import worker_env
from .agents import AgentResult, AgentRunner, AgentSpec
from .findings import FINDINGS_SCHEMA_TEXT, parse_findings
from .markdown import MarkdownPolicy
from .reconcile import reconcile


from .report import ValidationRun, render_report
from .taskgen import generate_tasks


def _reshape_prompt(items) -> str:
    """Ask an agent to re-express its own oversized findings.

    Carries the original items verbatim, so the agent is compressing its own
    words rather than re-deriving the finding from scratch — a re-derivation
    could quietly come back as a different, weaker finding.
    """
    blocks = []
    for item in items:
        blocks.append(
            f"Finding '{item.finding_id}' exceeds the structural bounds:\n"
            + "\n".join(f"  - {reason}" for reason in item.reasons)
            + "\nOriginal:\n```json\n"
            + json.dumps(item.item, indent=2, ensure_ascii=False)
            + "\n```"
        )
    return (
        "You previously reported findings that are too long to include in the "
        "audit report. Re-express EVERY finding below within the limits, "
        "preserving its substance: the same defect, the same impact, the same "
        "acceptance criteria. Cite locations as `path:line` instead of quoting "
        "code. If a finding covers several genuinely separate problems, split "
        "it into several findings — that is the intended fix, and ids may gain "
        "a suffix (e.g. 'sec-01a', 'sec-01b').\n\n"
        "Do NOT drop a finding, and do not soften its severity or confidence "
        "to make it shorter.\n\n"
        + "\n\n".join(blocks)
        + "\n\n"
        + FINDINGS_SCHEMA_TEXT
    )

# SAFE_VALIDATION_BINARIES lives in `..validation` now — shared with the
# produce-then-review post-commit validation check in `orchestrator.py`, so
# the two call sites can't drift apart on what's safe to launch. The audit
# runs repo checks, never repo mutations.

# (slug, title, charter, model). Order IS the wave order: with
# max_parallel_agents=3 the first three run concurrently, then the rest — so
# wave 1 is the two Haiku inventory domains plus one Sonnet reader, wave 2 is
# the three Sonnet judgement domains. Models are per domain on purpose:
# mechanical inventory (docs drift, test/CI/command inventory) does not need an
# expensive model, and nothing here runs on the lead's model.
DEFAULT_DOMAINS: tuple[tuple[str, str, str, str], ...] = (
    (
        "docs_drift",
        "Documentation drift",
        "Compare CLAUDE.md, docs/SUMMARY.md, docs/TESTS.md, docs/ROADMAP.md and "
        "docs/TODO.md against the code. Report claims that are demonstrably "
        "stale (files that moved/died, counts that changed, commands that no "
        "longer work). Cite the exact doc line and the contradicting code.",
        "haiku",
    ),
    (
        "tests_ci",
        "Tests, linting, CI and validation",
        "Assess test coverage honestly against docs/TESTS.md. Look for: suites "
        "not run by any documented command, tests that can never fail, xdist "
        "isolation hazards, the pytest-vs-python-m-pytest sys.path trap, ruff "
        "scope gaps, CI workflows that don't run what the docs claim.",
        "haiku",
    ),
    (
        "repo_structure",
        "Architecture & repository structure",
        "Map the actual architecture against what CLAUDE.md and docs/SUMMARY.md "
        "claim. Look for: dead or orphaned modules, duplicated responsibilities, "
        "layering violations (routers writing state directly, services importing "
        "routers), the two-backend split (lexy-app/backend vs root pipeline "
        "modules), and import-time side effects.",
        "sonnet",
    ),
    (
        "security_paths",
        "Security, path handling and data integrity",
        "Read docs/SECURITY.md first; verify open findings and 'verified "
        "strengths' still hold. Look for: raw SQL, path traversal around "
        "uploads/document packages, subprocess call sites, auth/ownership "
        "filters on routers with path params, JSONB settings whitelist, LLM "
        "prompt-injection surfaces.",
        "sonnet",
    ),
    (
        "db_migrations",
        "Database and migration safety",
        "Review alembic migrations for: edited-after-merge migrations, "
        "destructive operations without guards, drift between models/schemas.py "
        "and the migration chain, missing indexes implied by hot queries, and "
        "irreversible data transformations.",
        "sonnet",
    ),
    (
        "ingestion_pipeline",
        "Document-ingestion pipeline and A1/A2 status",
        "Review docs/INGESTION_PIPELINE.md and the shipped A1 (evaluation/) and "
        "A2 (services/document_package/, book_import_service) work. Look for: "
        "contract mismatches between the offline worker design and the import "
        "path, coordinate-space errors, validation gaps, and the import-path "
        "convention issue in test_document_package.py.",
        "sonnet",
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


#: The audit report goes to ChatGPT as the `details` of a review payload, and
#: `report_sha256` is computed over exactly those bytes. A real report is ~70 kB,
#: which produced 104k/113k/122k-character messages — roughly three quarters of
#: everything this loop ever sent, and the load that wedged a conversation into
#: accepting messages and never generating a reply. The full text is on disk and
#: in the reviewed commit, whose diff the post-commit packet already carries, so
#: inlining it again bought nothing.
MAX_REPORT_DETAILS_CHARS = 8_000


def cap_report_details(report: str, report_path: str) -> str:
    """A bounded excerpt of `report` for the review payload.

    Keeps the HEAD of the report — the coverage table and findings summary lead
    it, which is what a reviewer needs to judge whether the audit is sound.
    Truncation is announced rather than silent: a reviewer must never believe
    they read the whole thing, and the path to the full text is named.
    """
    if len(report) <= MAX_REPORT_DETAILS_CHARS:
        return report
    kept = report[:MAX_REPORT_DETAILS_CHARS]
    omitted = len(report) - len(kept)
    return (
        kept
        + f"\n\n[... {omitted:,} characters omitted. This is an EXCERPT, not the "
        f"report. The full text is committed at {report_path} and appears in "
        "full in this candidate's diff. ...]"
    )


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
        worker_repo_root_for: Callable[[str], Path] | None = None,
        policy: PolicyEngine | None = None,
        agent_runner_factory: Callable[[Path], AgentRunner] | None = None,
    ):
        """`git` / `markdown` / `agent_runner` are the STANDALONE bindings —
        used verbatim whenever `task` is `None` (every direct `execute()` call
        in this module's own tests) or `worker_repo_root_for` is not supplied.

        `worker_repo_root_for` (a `path_for`-shaped callable, e.g.
        `WorkerRepoManager.path_for`) is how the orchestrator's produce-then-
        review wiring re-roots a call onto the audit's own isolated worker
        repo: when set AND `task` is not `None`, `execute()` builds a FRESH
        `GitGateway`/`MarkdownPolicy` rooted at `worker_repo_root_for(task.id)`
        for that one call — `policy` (required together with
        `worker_repo_root_for`) is what that fresh `GitGateway` is
        constructed with, running under the scrubbed `worker_env()` mapping
        exactly like the orchestrator's own worktree gateway. `agent_runner_factory`,
        if given, likewise builds a fresh `AgentRunner` rooted at the worker
        repo (e.g. a `ClaudeCliRunner` whose `cwd` is the worker repo, so
        read-only subagents inspect the audit's own frozen checkout, not the
        main checkout); when omitted, the construction-time `agent_runner` is
        reused as-is (its own `cwd`, whatever that is).
        """
        self._git = git
        self._agent_runner = agent_runner
        self._markdown = markdown
        self._registry = registry
        self._run_dir_base = Path(run_dir_base)
        self._validation_commands = validation_commands
        self._max_parallel = max(1, max_parallel_agents)
        self._domains = domains
        self._command_runner = command_runner or subprocess.run
        self._worker_repo_root_for = worker_repo_root_for
        self._policy = policy
        self._agent_runner_factory = agent_runner_factory

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
        git, markdown, agent_runner = self._bindings_for(task)
        return self._run_audit(
            git,
            markdown,
            agent_runner,
            scope=directive.scope,
            feedback=directive.feedback if is_audit_revision else None,
        )

    def _bindings_for(
        self, task: Task | None
    ) -> tuple[GitGateway, MarkdownPolicy, AgentRunner]:
        if self._worker_repo_root_for is None or task is None:
            return self._git, self._markdown, self._agent_runner
        root = self._worker_repo_root_for(task.id)
        git = GitGateway(root, self._policy, env=worker_env())
        markdown = MarkdownPolicy(root)
        agent_runner = (
            self._agent_runner_factory(root)
            if self._agent_runner_factory is not None
            else self._agent_runner
        )
        return git, markdown, agent_runner

    # ---- audit pipeline -----------------------------------------------------

    def _run_audit(
        self,
        git: GitGateway,
        markdown: MarkdownPolicy,
        agent_runner: AgentRunner,
        scope: str | None,
        feedback: str | None,
    ) -> ExecutionOutcome:
        now = datetime.now(timezone.utc)
        date = now.strftime("%Y-%m-%d")
        run_id = now.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        run_dir = self._run_dir_base / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        branch = git.current_branch()
        head = git.head_sha()
        dirty = len(git.dirty_files())

        validation_runs = [self._run_validation(git, cmd) for cmd in self._validation_commands]

        specs = [
            AgentSpec(
                domain=slug,
                title=title,
                prompt=_agent_prompt(title, charter, scope, feedback),
                model=model,
            )
            for slug, title, charter, model in self._domains
        ]
        results = self._run_agents(agent_runner, specs, run_dir)

        findings, parse_rejects, agent_failures, covered = [], [], [], []
        oversized = []
        for result in results:
            if not result.ok:
                agent_failures.append(
                    f"{result.domain}: {result.error or f'rc={result.returncode}'}"
                )
                continue
            outcome = parse_findings(result.raw_text, result.domain)
            parse_rejects.extend(outcome.rejected)
            if not outcome.usable:
                # The agent exited 0 but its output could not be read at all —
                # the domain is UNCOVERED. Reporting this as "0 findings" while
                # claiming no agent failures is exactly how a whole security
                # review once vanished from a clean-looking summary.
                reason = outcome.rejected[0].reason if outcome.rejected else "unusable output"
                agent_failures.append(f"{result.domain}: output unusable — {reason}")
                continue
            findings.extend(outcome.findings)
            oversized.extend(outcome.oversized)
            covered.append(result.domain)

        if oversized:
            findings.extend(
                self._reshape_oversized(agent_runner, oversized, run_dir)
            )

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
            covered_domains=tuple(covered),
            all_domains=tuple(slug for slug, *_ in self._domains),
            raw_reports_dir=str(run_dir),
        )
        report_path = f"docs/AUDIT_{date}.md"
        markdown.write(report_path, report)

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
            "committed automatically once validation passes)."
        )
        return ExecutionOutcome(
            status="ok" if not agent_failures else "error",
            summary=summary
            if not agent_failures
            else summary + " COVERAGE INCOMPLETE — see agent failures in the report.",
            details=cap_report_details(report, report_path),
            validation=validation_summary,
            # produce-then-review path: this is the ONE file the audit ever
            # changes (`MarkdownPolicy` enforces at most one new report per
            # run), so it is exactly `commit_and_capture`'s planned-paths set.
            changed_paths=(report_path,),
        )

    def _reshape_oversized(self, agent_runner, oversized, run_dir) -> list:
        """One bounded re-expression round for findings that blew the
        structural bounds. Returns the reshaped findings.

        Exactly one attempt, by design. A finding that is still oversized after
        being told precisely what was wrong is not going to converge by being
        asked again, and the three tempting alternatives are all worse than
        stopping: looping burns the agent budget on one stubborn item, silently
        accepting it puts the outlier back in the report the bounds exist to
        keep out, and dropping or truncating it destroys a finding nobody has
        read yet. So this parks, with the finding preserved on disk.
        """
        from .findings import oversize_reasons, parse_findings

        by_domain: dict[str, list] = {}
        for item in oversized:
            by_domain.setdefault(item.domain, []).append(item)

        specs = [
            AgentSpec(
                domain=f"{domain}-reshape",
                title=f"{domain} (re-express oversized findings)",
                prompt=_reshape_prompt(items),
                model="",
            )
            for domain, items in by_domain.items()
        ]
        # Its own subdirectory so the reshape round's raw output sits beside
        # the first pass's rather than overwriting it — both are evidence.
        reshape_dir = run_dir / "reshape"
        reshape_dir.mkdir(parents=True, exist_ok=True)
        results = self._run_agents(agent_runner, specs, reshape_dir)

        reshaped: list = []
        still_oversized: list[str] = []
        produced: dict[str, int] = {domain: 0 for domain in by_domain}
        for result in results:
            domain = result.domain.removesuffix("-reshape")
            if not result.ok:
                still_oversized.append(
                    f"{domain}: reshape agent failed ({result.error or result.returncode})"
                )
                continue
            outcome = parse_findings(result.raw_text, domain)
            for finding in outcome.findings:
                if oversize_reasons(finding):  # defensive; parse_findings filters these
                    still_oversized.append(f"{domain}:{finding.id} still oversized")
                else:
                    reshaped.append(finding)
                    produced[domain] = produced.get(domain, 0) + 1
            for item in outcome.oversized:
                still_oversized.append(
                    f"{domain}:{item.finding_id} still oversized — {item.reasons[0]}"
                )

        # COUNT the findings back. A reshape is allowed to SPLIT one oversized
        # finding into several (that is the intended fix, so ids legitimately
        # change), which rules out matching by id — but it is never allowed to
        # return FEWER than it was given. Without this, an agent replying
        # `{"findings": []}` would look like a clean success while the finding
        # it was asked about disappeared: the quietest of the three losses this
        # method exists to prevent, and the one a reader could never detect.
        for domain, items in by_domain.items():
            if produced.get(domain, 0) < len(items):
                still_oversized.append(
                    f"{domain}: reshape returned {produced.get(domain, 0)} findings for "
                    f"{len(items)} oversized one(s) — findings were lost, not shortened"
                )

        if still_oversized:
            preserved = run_dir / "oversized_findings.json"
            try:
                preserved.parent.mkdir(parents=True, exist_ok=True)
                preserved.write_text(
                    json.dumps([asdict(o) for o in oversized], indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                pass
            raise AuditError(
                "audit findings could not be brought within the structural bounds "
                f"after one reshape round: {'; '.join(still_oversized)}. The "
                f"findings are preserved verbatim at {preserved} — nothing was "
                "truncated or discarded. Review them by hand, or split them into "
                "separate findings, before re-running the audit."
            )
        return reshaped

    def _run_agents(
        self, agent_runner: AgentRunner, specs: list[AgentSpec], run_dir: Path
    ) -> list[AgentResult]:
        raw_dir = run_dir / "raw"
        raw_dir.mkdir(exist_ok=True)
        with ThreadPoolExecutor(max_workers=self._max_parallel) as pool:
            results = list(pool.map(agent_runner.run, specs))
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

    def _run_validation(self, git: GitGateway, argv: tuple[str, ...]) -> ValidationRun:
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
                cwd=str(git.repo_root),
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
