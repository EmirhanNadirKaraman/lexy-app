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


# ---- failure summarisation: advisory banners are not causes ------------------
#
# The CLI prints a connectors notice to stderr BEFORE anything else when the
# subagent runs nested inside a Claude Code session. The old capture took
# `stderr[:2000]` — the head — so that banner became the whole reported cause
# of every non-zero exit, travelled into the review packet, and came back as a
# directive asking an operator to unset a variable that was not set anywhere.

CONNECTORS_BANNER = (
    "⚠ claude.ai connectors are disabled because ANTHROPIC_API_KEY or another "
    "auth source is set and takes precedence over your claude.ai login · "
    "Unset it to load your organization's connectors"
)


def test_a_leading_advisory_banner_is_not_reported_as_the_cause():
    """The regression. The real error follows the banner; it must be what the
    summary leads with."""
    from autoloop.audit.agents import summarize_failure

    stderr = f"{CONNECTORS_BANNER}\nTypeError: cannot read property 'x' of undefined\n"
    summary = summarize_failure(stderr, "", 1)

    assert summary.startswith("TypeError: cannot read property")
    assert "connectors are disabled" not in summary.split("(advisory")[0]


def test_banner_only_stderr_says_there_was_no_diagnostic_output():
    """The case that actually happened: nothing but the banner. Reporting it
    as the cause sends someone to fix a variable that is not set — so say
    plainly that there was no diagnosis, and mark the notice as not the
    cause."""
    from autoloop.audit.agents import summarize_failure

    summary = summarize_failure(CONNECTORS_BANNER + "\n", "", 1)

    assert "NO diagnostic output" in summary
    assert "not the cause" in summary
    assert "unset" not in summary.lower().split("advisory notice")[0]


def test_a_long_traceback_keeps_its_TAIL_where_the_cause_lives():
    """A head-only excerpt loses the exception line: tracebacks put the cause
    last. Both ends are kept."""
    from autoloop.audit.agents import summarize_failure

    stderr = "\n".join(f"  File \"mod{i}.py\", line {i}, in f" for i in range(400))
    stderr += "\nValueError: the actual cause\n"
    summary = summarize_failure(stderr, "", 1)

    assert "ValueError: the actual cause" in summary
    assert "elided" in summary


def test_plain_failures_are_unchanged_and_stdout_is_the_fallback():
    from autoloop.audit.agents import summarize_failure

    assert "boom" in summarize_failure("boom\n", "", 2)
    # stderr empty -> stdout is used, as before
    assert "from stdout" in summarize_failure("", "from stdout\n", 2)
    # nothing at all -> honest, and names the exit code
    assert "no output" in summarize_failure("", "", 3)


def test_runner_reports_the_real_cause_end_to_end(tmp_path):
    """Through `ClaudeCliRunner.run`, not just the helper."""
    import subprocess as _sp

    from autoloop.audit.agents import AgentSpec, ClaudeCliRunner

    def fake_run(argv, **kwargs):
        return _sp.CompletedProcess(
            argv, 1, stdout="", stderr=f"{CONNECTORS_BANNER}\nOSError: disk full\n"
        )

    result = ClaudeCliRunner(repo_root=tmp_path, runner=fake_run).run(
        AgentSpec(domain="d", title="t", prompt="p")
    )
    assert not result.ok
    assert result.error.startswith("OSError: disk full")
