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
