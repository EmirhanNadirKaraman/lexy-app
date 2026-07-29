"""Audit subagent invocation via the Claude Code CLI.

The environment's real delegation facility is the `claude` CLI in headless
mode (`claude -p <prompt> --output-format json`) — no model API is used.
Agents are READ-ONLY by construction: `--allowedTools Read Grep Glob` plus an
explicit disallow of every editing/executing tool, and `--permission-mode
dontAsk` denies anything that would prompt in headless mode. Agents therefore
cannot edit files or spawn nested agents (Task/Agent is disallowed), which is
what "no uncontrolled nested delegation" means mechanically.

Tests never invoke the real CLI — AgentRunner is a protocol; the executor is
exercised with fakes, and ClaudeCliRunner itself is tested with a stubbed
subprocess runner.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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


@dataclass(frozen=True)
class AgentResult:
    domain: str
    raw_text: str
    returncode: int
    duration_seconds: float
    command: tuple[str, ...]
    error: str = ""

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
    ):
        self._repo_root = Path(repo_root)
        self._command = tuple(command)
        self._timeout = timeout_seconds
        self._runner = runner or subprocess.run

    def build_argv(self, spec: AgentSpec) -> list[str]:
        return [
            *self._command,
            "-p",
            spec.prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "dontAsk",
            "--allowedTools",
            *READ_ONLY_ALLOWED_TOOLS,
            "--disallowedTools",
            *DISALLOWED_TOOLS,
        ]

    def run(self, spec: AgentSpec) -> AgentResult:
        argv = self.build_argv(spec)
        started = time.monotonic()
        try:
            proc = self._runner(
                argv,
                cwd=str(self._repo_root),
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return AgentResult(
                domain=spec.domain,
                raw_text="",
                returncode=-1,
                duration_seconds=time.monotonic() - started,
                command=tuple(argv),
                error=f"agent timed out after {self._timeout}s",
            )
        except FileNotFoundError as exc:
            return AgentResult(
                domain=spec.domain,
                raw_text="",
                returncode=-1,
                duration_seconds=time.monotonic() - started,
                command=tuple(argv),
                error=f"agent command not found: {exc}",
            )
        text = _extract_result_text(proc.stdout or "")
        error = ""
        if proc.returncode != 0:
            error = (proc.stderr or proc.stdout or "").strip()[:2000] or "non-zero exit"
        return AgentResult(
            domain=spec.domain,
            raw_text=text,
            returncode=proc.returncode,
            duration_seconds=time.monotonic() - started,
            command=tuple(argv),
            error=error,
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
