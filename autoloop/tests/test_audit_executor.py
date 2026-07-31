"""Audit executor end-to-end with fake agents + stubbed validation commands:
domain fan-out, raw-report persistence, reconciliation, Markdown-only report,
task proposal, revise-of-audit, audit-only policy, failure honesty."""

import json
import subprocess

import pytest

from autoloop.audit.agents import AgentResult
from autoloop.audit.executor import AuditExecutor
from autoloop.audit.markdown import MarkdownPolicy
from autoloop.contract import Decision, Directive
from autoloop.git_gateway import GitGateway
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.tasks import TaskRegistry


def run_git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.email", "t@e.c")
    run_git(root, "config", "user.name", "T")
    run_git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("hi")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-q", "-m", "init")
    return root


def good_findings(fid="f1", category="defect"):
    return json.dumps(
        {
            "findings": [
                {
                    "id": fid,
                    "category": category,
                    "severity": "high",
                    "confidence": "confirmed",
                    "affected_files": ["a.py"],
                    "symbols": [],
                    "evidence": "seen",
                    "impact": "bad",
                    "proposed_action": "fix the thing",
                    "dependencies": [],
                    "acceptance_criteria": ["works"],
                    "validation_commands": ["ruff check ."],
                    "safe_to_parallelize": True,
                }
            ]
        }
    )


class FakeRunner:
    def __init__(self, outputs=None, fail_domains=()):
        self.outputs = outputs or {}
        self.fail_domains = set(fail_domains)
        self.specs = []

    def run(self, spec):
        self.specs.append(spec)
        if spec.domain in self.fail_domains:
            return AgentResult(
                domain=spec.domain, raw_text="", returncode=1,
                duration_seconds=0.1, command=("claude",), error="boom",
            )
        text = self.outputs.get(spec.domain, json.dumps({"findings": []}))
        return AgentResult(
            domain=spec.domain, raw_text=text, returncode=0,
            duration_seconds=0.1, command=("claude",),
        )


def ok_command(argv, **kwargs):
    class Proc:
        returncode = 0
        stdout = "All checks passed!\n"
        stderr = ""

    return Proc()


def build_executor(repo, tmp_path, runner=None, validation=(("ruff", "check", "."),)):
    git = GitGateway(repo, PolicyEngine(PolicyConfig()))
    return AuditExecutor(
        git=git,
        agent_runner=runner or FakeRunner(),
        markdown=MarkdownPolicy(repo),
        registry=TaskRegistry(),
        run_dir_base=tmp_path / "runs",
        validation_commands=validation,
        max_parallel_agents=2,
        command_runner=ok_command,
    )


def audit_directive(scope=None):
    return Directive(decision=Decision.AUDIT, reason="r", scope=scope)


def test_full_audit_run(repo, tmp_path):
    runner = FakeRunner(outputs={"security_paths": good_findings("sec-1", "security")})
    executor = build_executor(repo, tmp_path, runner)
    outcome = executor.execute(audit_directive(scope="look at uploads"), None)
    assert outcome.status == "ok"
    # all six default domains fanned out, scope threaded into prompts
    assert len(runner.specs) == 6
    assert all("look at uploads" in s.prompt for s in runner.specs)
    assert all("READ-ONLY" in s.prompt for s in runner.specs)
    # one accepted finding -> one proposed task, in the report
    assert "1 accepted findings" in outcome.summary
    assert "au-001" in outcome.details
    assert "fix the thing" in outcome.details
    # the ONLY repo file written is the dated report
    written = [p for p in (repo / "docs").iterdir()]
    assert len(written) == 1 and written[0].name.startswith("AUDIT_")
    assert outcome.validation.startswith("ruff check .: PASS")


def test_raw_reports_persisted_separately(repo, tmp_path):
    runner = FakeRunner(outputs={"docs_drift": good_findings("d1", "doc_drift")})
    executor = build_executor(repo, tmp_path, runner)
    executor.execute(audit_directive(), None)
    [run_dir] = (tmp_path / "runs").iterdir()
    raw = run_dir / "raw"
    assert (raw / "docs_drift.txt").read_text() == good_findings("d1", "doc_drift")
    assert (raw / "docs_drift.meta.json").exists()
    assert (run_dir / "reconciled.json").exists()
    proposed = json.loads((run_dir / "proposed_tasks.json").read_text())
    assert proposed[0]["id"] == "au-001"


def test_agent_failure_reported_as_incomplete(repo, tmp_path):
    runner = FakeRunner(fail_domains={"db_migrations"})
    executor = build_executor(repo, tmp_path, runner)
    outcome = executor.execute(audit_directive(), None)
    assert outcome.status == "error"
    assert "COVERAGE INCOMPLETE" in outcome.summary
    assert "db_migrations" in outcome.details


def test_revise_of_audit_threads_feedback(repo, tmp_path):
    runner = FakeRunner()
    executor = build_executor(repo, tmp_path, runner)
    directive = Directive(
        decision=Decision.REVISE, reason="r", task_id="audit", feedback="check migration 036"
    )
    outcome = executor.execute(directive, None)
    assert outcome.status == "ok"
    assert all("check migration 036" in s.prompt for s in runner.specs)
    assert "check migration 036" in outcome.details


def test_non_audit_decisions_refused(repo, tmp_path):
    executor = build_executor(repo, tmp_path)
    directive = Directive(decision=Decision.IMPLEMENT, reason="r", task_id="t1")
    outcome = executor.execute(directive, None)
    assert outcome.status == "error"
    assert "supports only" in outcome.summary


def test_unsafe_validation_command_refused(repo, tmp_path):
    executor = build_executor(repo, tmp_path, validation=(("rm", "-rf", "/"),))
    outcome = executor.execute(audit_directive(), None)
    assert "FAIL" in outcome.validation
    assert "not a safe validation binary" in outcome.details


# ---- model allocation (operator Decision 2) --------------------------------


def test_domain_allocation_is_two_haiku_inventory_then_four_sonnet():
    from autoloop.audit.executor import DEFAULT_DOMAINS

    assert len(DEFAULT_DOMAINS) == 6
    slugs = [d[0] for d in DEFAULT_DOMAINS]
    models = [d[3] for d in DEFAULT_DOMAINS]
    # Order is wave order: wave 1 = first three (max_parallel_agents=3).
    assert slugs[:3] == ["docs_drift", "tests_ci", "repo_structure"]
    assert models[:3] == ["haiku", "haiku", "sonnet"]
    assert models[3:] == ["sonnet", "sonnet", "sonnet"]
    # Mechanical inventory never runs on an expensive model, and the lead's
    # model is never delegated to.
    assert dict(zip(slugs, models))["docs_drift"] == "haiku"
    assert dict(zip(slugs, models))["tests_ci"] == "haiku"
    assert "opus" not in models
    assert "fable" not in models


def test_executor_routes_each_domain_to_its_model(repo, tmp_path):
    runner = FakeRunner()
    build_executor(repo, tmp_path, runner).execute(audit_directive(), None)
    routed = {spec.domain: spec.model for spec in runner.specs}
    assert routed == {
        "docs_drift": "haiku",
        "tests_ci": "haiku",
        "repo_structure": "sonnet",
        "security_paths": "sonnet",
        "db_migrations": "sonnet",
        "ingestion_pipeline": "sonnet",
    }


def test_parallelism_is_capped_at_the_configured_worker_count(repo, tmp_path):
    """Six domains, cap of three: never more than three agents in flight."""
    import threading

    live = {"now": 0, "peak": 0}
    lock = threading.Lock()
    gate = threading.Barrier(3, timeout=5)

    class CountingRunner(FakeRunner):
        def run(self, spec):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            try:
                # Force three to be genuinely concurrent, so a serial
                # implementation would deadlock the barrier instead of passing.
                gate.wait()
                return super().run(spec)
            finally:
                with lock:
                    live["now"] -= 1

    executor = AuditExecutor(
        git=GitGateway(repo, PolicyEngine(PolicyConfig())),
        agent_runner=CountingRunner(),
        markdown=MarkdownPolicy(repo),
        registry=TaskRegistry(),
        run_dir_base=tmp_path / "runs",
        validation_commands=(),
        max_parallel_agents=3,
        command_runner=ok_command,
    )
    executor.execute(audit_directive(), None)
    assert live["peak"] == 3  # exactly the cap, excluding the lead


def test_shipped_config_caps_workers_at_three():
    from autoloop.config import AuditConfig

    assert AuditConfig().max_parallel_agents == 3


# ---- coverage honesty (the defect ChatGPT caught) --------------------------


def test_unusable_agent_output_is_reported_as_a_coverage_failure(repo, tmp_path):
    """The live regression: security_paths returned truncated JSON, contributed
    zero findings, and the report still claimed '0 agent failures'."""
    truncated = '{"findings": [{"id": "sec-01", "evidence": "unterminated'
    runner = FakeRunner(outputs={"security_paths": truncated})
    outcome = build_executor(repo, tmp_path, runner).execute(audit_directive(), None)
    assert outcome.status == "error"
    assert "COVERAGE INCOMPLETE" in outcome.summary
    assert "security_paths" in outcome.details
    assert "output unusable" in outcome.details


def test_report_shows_per_domain_coverage_and_names_the_missing_domain(repo, tmp_path):
    truncated = '{"findings": [{"id": "sec-01"'
    runner = FakeRunner(outputs={"security_paths": truncated})
    outcome = build_executor(repo, tmp_path, runner).execute(audit_directive(), None)
    assert "## Domain coverage — 5/6 domains reported usable output" in outcome.details
    assert "| `security_paths` | **NO** | 0 |" in outcome.details
    assert "treat their areas as unaudited, not as clean" in outcome.details


def test_full_coverage_reports_six_of_six(repo, tmp_path):
    outcome = build_executor(repo, tmp_path, FakeRunner()).execute(audit_directive(), None)
    assert "## Domain coverage — 6/6 domains reported usable output" in outcome.details
    assert "COVERAGE INCOMPLETE" not in outcome.details
    assert outcome.status == "ok"


def test_one_bad_finding_does_not_mark_a_domain_uncovered(repo, tmp_path):
    """A single rejected item is not a coverage gap — the domain still reported."""
    mixed = json.dumps(
        {"findings": [json.loads(good_findings())["findings"][0], {"id": "broken"}]}
    )
    runner = FakeRunner(outputs={"db_migrations": mixed})
    outcome = build_executor(repo, tmp_path, runner).execute(audit_directive(), None)
    assert outcome.status == "ok"
    assert "## Domain coverage — 6/6 domains reported usable output" in outcome.details


def test_report_details_are_capped_for_the_review_payload():
    """A ~70 kB report inlined into `details` produced 104k/113k/122k-character
    messages — most of everything this loop ever sent, and the load that wedged
    a conversation into accepting messages and never replying."""
    from autoloop.audit.executor import MAX_REPORT_DETAILS_CHARS, cap_report_details

    report = "COVERAGE TABLE\n" + ("finding line\n" * 8000)
    assert len(report) > 70_000
    capped = cap_report_details(report, "docs/AUDIT_2026-07-31.md")

    assert len(capped) < MAX_REPORT_DETAILS_CHARS + 500
    assert capped.startswith("COVERAGE TABLE"), "the head is what a reviewer needs"
    assert "EXCERPT" in capped, "truncation must be announced, never silent"
    assert "docs/AUDIT_2026-07-31.md" in capped, "must name where the full text is"


def test_a_short_report_is_not_truncated():
    from autoloop.audit.executor import cap_report_details

    assert cap_report_details("a short report", "p.md") == "a short report"
