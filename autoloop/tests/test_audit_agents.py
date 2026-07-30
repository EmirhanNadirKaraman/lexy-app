"""ClaudeCliRunner with a stubbed subprocess: read-only argv construction,
result unwrapping, timeout and missing-binary handling. The real CLI is never
invoked."""

import json
import subprocess

from autoloop.audit.agents import (
    DISALLOWED_TOOLS,
    READ_ONLY_ALLOWED_TOOLS,
    AgentSpec,
    ClaudeCliRunner,
)

SPEC = AgentSpec(domain="docs_drift", title="Documentation drift", prompt="audit the docs")


class Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_argv_is_read_only_headless(tmp_path):
    runner = ClaudeCliRunner(tmp_path, command=("claude",))
    argv = runner.build_argv(SPEC)
    assert argv[:3] == ["claude", "-p", "audit the docs"]
    assert "--output-format" in argv and "json" in argv
    assert "--permission-mode" in argv
    for tool in READ_ONLY_ALLOWED_TOOLS:
        assert tool in argv
    for tool in DISALLOWED_TOOLS:  # Edit/Write/Bash/Task/... all disallowed
        assert tool in argv[argv.index("--disallowedTools"):]


def test_result_json_unwrapped(tmp_path):
    payload = json.dumps({"result": '{"findings": []}', "cost_usd": 0.01})

    def stub(argv, **kwargs):
        return Proc(stdout=payload)

    result = ClaudeCliRunner(tmp_path, runner=stub).run(SPEC)
    assert result.ok
    assert result.raw_text == '{"findings": []}'


def test_plain_stdout_passed_through(tmp_path):
    def stub(argv, **kwargs):
        return Proc(stdout='{"findings": []}')

    result = ClaudeCliRunner(tmp_path, runner=stub).run(SPEC)
    assert result.raw_text == '{"findings": []}'


def test_timeout_reported_not_raised(tmp_path):
    def stub(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    result = ClaudeCliRunner(tmp_path, timeout_seconds=1, runner=stub).run(SPEC)
    assert not result.ok
    assert "timed out" in result.error


def test_missing_binary_reported(tmp_path):
    def stub(argv, **kwargs):
        raise FileNotFoundError("claude")

    result = ClaudeCliRunner(tmp_path, runner=stub).run(SPEC)
    assert not result.ok
    assert "not found" in result.error


def test_nonzero_exit_captures_stderr(tmp_path):
    def stub(argv, **kwargs):
        return Proc(stdout="", returncode=2, stderr="rate limited")

    result = ClaudeCliRunner(tmp_path, runner=stub).run(SPEC)
    assert not result.ok
    assert "rate limited" in result.error


# ---- per-domain model routing ----------------------------------------------


def test_model_flag_passed_when_a_domain_names_one(tmp_path):
    spec = AgentSpec(domain="docs_drift", title="t", prompt="p", model="haiku")
    argv = ClaudeCliRunner(tmp_path).build_argv(spec)
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "haiku"


def test_model_flag_omitted_when_unset(tmp_path):
    argv = ClaudeCliRunner(tmp_path).build_argv(SPEC)  # model defaults to ""
    assert "--model" not in argv


def test_model_routing_does_not_weaken_read_only_flags(tmp_path):
    spec = AgentSpec(domain="d", title="t", prompt="p", model="sonnet")
    argv = ClaudeCliRunner(tmp_path).build_argv(spec)
    for tool in READ_ONLY_ALLOWED_TOOLS:
        assert tool in argv
    for tool in DISALLOWED_TOOLS:
        assert tool in argv[argv.index("--disallowedTools"):]
