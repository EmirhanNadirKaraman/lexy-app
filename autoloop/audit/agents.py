"""Subagent invocation via the Claude Code CLI — shared by the audit and the
implement executors.

The environment's real delegation facility is the `claude` CLI in headless
mode (`claude -p <prompt> --output-format json`) — no model API is used.
`--permission-mode dontAsk` denies anything that would prompt in headless
mode.

**Tool set is a constructor parameter, not a fixed constant (since the
implement executor landed).** `ClaudeCliRunner.__init__` takes
`allowed_tools`/`disallowed_tools`, defaulting to `READ_ONLY_ALLOWED_TOOLS`
(Read/Grep/Glob) / `DISALLOWED_TOOLS` (every editing/executing tool) — every
existing caller (the audit executor, `test_audit_agents.py`) omits both and
gets the exact same read-only argv as before this became configurable.
`autoloop/implement_executor.py`'s `implement_agent_runner` is the OTHER
construction site: it passes a write-capable set (Read/Grep/Glob/Edit/Write)
so its subagent can produce a change. `Bash` and `Task`/`Agent` stay
disallowed on BOTH paths — the executor (not the agent) runs validation and
commits, and a subagent spawning nested agents is out of scope for either
phase; that is what "no uncontrolled nested delegation" means mechanically,
independent of which tool set is otherwise in force.

**Two ways to bound a run, chosen by whether progress can be OBSERVED.**
A write-capable agent runs against a worker repository whose changes are a
direct, first-hand signal that it is still working, so it is supervised by
`stall.py`'s progress detector: spawn, watch the tree, kill only on silence
(or on the absolute ceiling). A read-only audit agent produces no filesystem
change at all, so no such signal exists for it and elapsed time remains the
only bound available — which is also the RIGHT bound there, because a timeout
on a read-only agent costs a re-run and never destroys work. Passing
`progress_probe` is what selects the supervised path; every caller that omits
it keeps the exact `subprocess.run(..., timeout=...)` behaviour it had before
this existed. See `stall.py`'s module docstring for the six measured losses
that motivated the split.

Tests never invoke the real CLI — AgentRunner is a protocol; the executors
are exercised with fakes, and ClaudeCliRunner itself is tested with a
stubbed subprocess runner (unsupervised path) or a fake spawn + fake clock
(supervised path).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..stall import (
    ProgressProbe,
    StallPolicy,
    StallReport,
    spawn_supervised,
    supervise,
)
from ..validation_env import strip_validation_vars

READ_ONLY_ALLOWED_TOOLS = ("Read", "Grep", "Glob")
DISALLOWED_TOOLS = (
    "Edit",
    "Write",
    "NotebookEdit",
    "Bash",
    "Task",
    "Agent",
    "WebFetch",
    "WebSearch",
)


@dataclass(frozen=True)
class AgentSpec:
    domain: str  # slug, e.g. "security_paths"
    title: str
    prompt: str
    #: Model alias for this domain ("haiku" / "sonnet" / "opus"). Empty means
    #: "whatever the CLI defaults to". Routing is per domain so mechanical
    #: inventory work does not run on an expensive model — see the allocation
    #: in `executor.DEFAULT_DOMAINS`.
    model: str = ""


@dataclass(frozen=True)
class AgentResult:
    domain: str
    raw_text: str
    returncode: int
    duration_seconds: float
    command: tuple[str, ...]
    error: str = ""
    #: Present ONLY when the supervisor killed this run — a stall or the
    #: absolute ceiling. Defaulted, so every existing construction site (and
    #: every test fake) is unaffected. `error` already carries the same story
    #: as prose; this is the structured form the executor uses to report the
    #: partial-work numbers without re-deriving them.
    stall: StallReport | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.error


class AgentRunner(Protocol):
    def run(self, spec: AgentSpec) -> AgentResult: ...


class ClaudeCliRunner:
    def __init__(
        self,
        repo_root: Path,
        command: tuple[str, ...] = ("claude",),
        timeout_seconds: float = 900.0,
        runner=None,
        allowed_tools: tuple[str, ...] = READ_ONLY_ALLOWED_TOOLS,
        disallowed_tools: tuple[str, ...] = DISALLOWED_TOOLS,
        progress_probe: ProgressProbe | None = None,
        stall_policy: StallPolicy | None = None,
        spawn=None,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        """`allowed_tools`/`disallowed_tools` default to the read-only audit
        set — every caller that does not pass them (every existing one)
        builds the exact same argv as before these became parameters. Pass a
        different pair (see `implement_executor.implement_agent_runner`) to
        run a write-capable subagent instead.

        `progress_probe` is what selects HOW the run is bounded, and it is
        the only switch:

        * absent (every audit caller) — `subprocess.run(..., timeout=
          timeout_seconds)`, byte for byte the behaviour that existed before
          the stall detector. `timeout_seconds` is an ELAPSED bound and means
          exactly what it always meant.
        * present (the write-capable implement runner) — spawn and supervise
          against `stall_policy`: killed on SILENCE in the worker repository,
          not on elapsed time, with `stall_policy.ceiling_seconds` as the
          absolute backstop. `timeout_seconds` is then unused, deliberately:
          two live time bounds on one run is how a "progress-based" detector
          quietly goes back to being a timeout.

        `spawn`/`clock`/`sleep` exist so the supervised path is testable with
        no real process and no real waiting; production leaves all three at
        their defaults.
        """
        self._repo_root = Path(repo_root)
        self._command = tuple(command)
        self._timeout = timeout_seconds
        self._runner = runner or subprocess.run
        self._allowed_tools = tuple(allowed_tools)
        self._disallowed_tools = tuple(disallowed_tools)
        self._progress_probe = progress_probe
        self._stall_policy = stall_policy or StallPolicy()
        self._spawn = spawn or spawn_supervised
        self._clock = clock
        self._sleep = sleep

    def build_argv(self, spec: AgentSpec) -> list[str]:
        model_flag = ["--model", spec.model] if spec.model else []
        return [
            *self._command,
            "-p",
            spec.prompt,
            *model_flag,
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            *self._allowed_tools,
            "--disallowedTools",
            *self._disallowed_tools,
        ]

    def run(self, spec: AgentSpec) -> AgentResult:
        """Never raises. Every failure — expected or not — comes back as an
        `AgentResult` carrying the cause.

        `_run_agents` fans these out through `list(pool.map(...))`, so ONE
        escaping exception discards the whole batch, including the domains
        that already finished. One agent falling over is a single coverage
        gap (the executor turns `not result.ok` into an `agent_failures`
        entry); it is not a reason to lose an audit run. The whole body is
        guarded, not just the subprocess call — building the argv, reading
        `proc.stdout` / `proc.returncode` and decoding the output are all part
        of the same failure surface."""
        started = time.monotonic()
        # Bound BEFORE the try, so reporting a failure can never itself fail.
        # `build_argv` reads `spec.model` and the configured tool tuples and is
        # therefore inside the guard — but `_failed` puts `argv` in the result,
        # so an unbound name there would turn a caught exception back into an
        # escaping one, in the exact handler that exists to stop that. The
        # fallback is the base command: enough to say WHAT was being run.
        # (`spec.domain` is read the same way and is not similarly guarded —
        # whatever it were captured into would need a guard of its own.)
        argv: list[str] = list(self._command)
        # EXPLICIT removal, not merely a failure to add: a subagent inherits
        # the loop's environment by construction, so the validation database
        # credentials have to be taken back out for the boundary in
        # `validation_env.py` to mean anything. Applied to BOTH tool sets —
        # the write-capable implement runner is the one the brief names, but
        # a read-only audit subagent has no business seeing them either, and
        # one unconditional strip cannot be forgotten at a future call site.
        # `strip_validation_vars` also drops any `*VALIDATION_ENV_FILE*`
        # variable, so the agent never learns where the file lives.
        try:
            argv = self.build_argv(spec)
            if self._progress_probe is None:
                proc = self._runner(
                    argv,
                    cwd=str(self._repo_root),
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                    env=strip_validation_vars(),
                )
                stdout, stderr = proc.stdout or "", proc.stderr
                returncode, stall = proc.returncode, None
            else:
                stdout, stderr, returncode, stall = self._run_supervised(argv)
            text = _extract_result_text(stdout)
            error = ""
            if stall is not None:
                # The stall report IS the cause — it already names the silence,
                # the elapsed time and the partial work. Whatever the killed
                # process left on stderr is a consequence of the kill, not the
                # reason for it, so it must not be reported as one.
                error = stall.describe()
            elif returncode != 0:
                error = summarize_failure(stderr, stdout, returncode)
            return AgentResult(
                domain=spec.domain,
                raw_text=text,
                returncode=returncode,
                duration_seconds=time.monotonic() - started,
                command=tuple(argv),
                error=error,
                stall=stall,
            )
        except subprocess.TimeoutExpired:
            return self._failed(spec, argv, started, f"agent timed out after {self._timeout}s")
        # BEFORE the broad clause below, and it must stay there:
        # FileNotFoundError is an OSError subclass, so a broad `except
        # Exception` placed above would swallow the one message that tells an
        # operator the `claude` binary is missing rather than misbehaving.
        except FileNotFoundError as exc:
            return self._failed(spec, argv, started, f"agent command not found: {exc}")
        except Exception as exc:  # noqa: BLE001 — deliberately total; see the docstring
            # The TYPE NAME is not decoration. `str(exc)` is empty for a bare
            # `MemoryError`/`RuntimeError()`, and an AgentResult whose `error`
            # is "" reads as `ok` (see `AgentResult.ok`) — a domain that blew
            # up would be counted as covered with zero findings. The message
            # is non-empty unconditionally.
            detail = str(exc).strip()
            described = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
            return self._failed(spec, argv, started, f"agent raised {described}")

    def _run_supervised(self, argv: list[str]) -> tuple[str, str, int, StallReport | None]:
        """Spawn, watch the worker tree, collect whatever the run produced.

        Output goes to temporary FILES rather than pipes for two reasons, both
        load-bearing: an undrained pipe blocks the child once its OS buffer
        fills — a hang manufactured by the hang detector — and a temp file
        sits outside the worker repository, so the agent's own output can
        never be mistaken for filesystem progress by the probe watching that
        repository. Partial output from a killed run is kept: it is often the
        only account of what the agent was doing when it wedged.
        """
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            handle = self._spawn(
                argv,
                cwd=str(self._repo_root),
                env=strip_validation_vars(),
                stdout=out,
                stderr=err,
            )
            supervision = supervise(
                handle,
                self._progress_probe,
                self._stall_policy,
                clock=self._clock,
                sleep=self._sleep,
            )
            out.seek(0)
            stdout = out.read().decode("utf-8", "replace")
            err.seek(0)
            stderr = err.read().decode("utf-8", "replace")
        returncode = supervision.returncode
        return stdout, stderr, (returncode if returncode is not None else -1), supervision.report

    def _failed(
        self, spec: AgentSpec, argv: list[str], started: float, error: str
    ) -> AgentResult:
        return AgentResult(
            domain=spec.domain,
            raw_text="",
            returncode=-1,
            duration_seconds=time.monotonic() - started,
            command=tuple(argv),
            error=error,
        )


#: stderr lines the CLI prints that are ADVISORY, never a cause of failure.
#:
#: The connectors notice is the one that cost real time: the loop's subagents
#: run nested inside a Claude Code session, so they inherit its auth context,
#: the CLI decides "another auth source" is present and disables claude.ai
#: connectors, and it prints that to stderr BEFORE anything else. The old
#: capture took `stderr[:2000]` — the HEAD — so this banner became the entire
#: reported cause of every non-zero exit. It travelled into the executor
#: summary, into the review packet, and out as a directive asking an operator
#: to unset `ANTHROPIC_API_KEY` — a variable that was not set anywhere, while
#: the actual failure was never shown at all.
#:
#: Matched as substrings against stripped lines, case-insensitively. Keep this
#: list SHORT and specific: anything matched here is dropped from the reported
#: cause, so a pattern that is too broad hides real failures — the exact bug
#: this exists to fix.
BENIGN_STDERR_MARKERS: tuple[str, ...] = (
    "claude.ai connectors are disabled",
    "unset it to load your organization's connectors",
)

#: Head and tail kept when output is long. Both ends, because a traceback puts
#: its cause LAST while a banner puts itself first — keeping only one end loses
#: whichever the failure happens to be.
_EXCERPT_SIDE = 900


def _is_benign(line: str) -> bool:
    lowered = line.strip().lower().lstrip("⚠! ").strip()
    return any(marker in lowered for marker in BENIGN_STDERR_MARKERS)


def _excerpt(text: str) -> str:
    text = text.strip()
    if len(text) <= _EXCERPT_SIDE * 2:
        return text
    dropped = len(text) - _EXCERPT_SIDE * 2
    return f"{text[:_EXCERPT_SIDE]}\n… [{dropped} chars elided] …\n{text[-_EXCERPT_SIDE:]}"


def summarize_failure(stderr: str | None, stdout: str | None, returncode: int) -> str:
    """What actually went wrong, with advisory banners demoted rather than
    reported as the cause.

    Returns the substantive output when there is any. When the output is
    NOTHING BUT advisory notices, it says so explicitly instead of presenting
    a warning as the failure — "exited N with no diagnostic output" is a
    worse-sounding but far more honest answer, and it is the one that sends
    someone looking in the right place.
    """
    raw = (stderr or "").strip() or (stdout or "").strip()
    if not raw:
        return f"non-zero exit ({returncode}) with no output on stderr or stdout"

    lines = raw.splitlines()
    substantive = [ln for ln in lines if ln.strip() and not _is_benign(ln)]
    advisory = [ln.strip() for ln in lines if ln.strip() and _is_benign(ln)]

    if substantive:
        summary = _excerpt("\n".join(substantive))
        if advisory:
            # Kept, but clearly separated from the cause and never first.
            summary += f"\n(advisory, not the cause: {advisory[0][:160]})"
        return summary

    return (
        f"non-zero exit ({returncode}) with NO diagnostic output — stderr held "
        f"only advisory notice(s), which are not the cause: {advisory[0][:200]}"
    )


def _extract_result_text(stdout: str) -> str:
    """`--output-format json` wraps the reply; unwrap `result` when present,
    fall back to the raw stdout otherwise."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(data, dict) and isinstance(data.get("result"), str):
        return data["result"]
    return stdout
