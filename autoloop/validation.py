"""Shared validation-command execution.

Two call sites run configured "validation" commands (lint/tests) against a
working tree and need to agree on the same safety boundary: the audit
executor (`audit/executor.py`, summarizing repo health) and the
produce-then-review post-commit check (`orchestrator.py`, re-running
validation against a task's own worktree AFTER its commit exists — a
pre-commit hook can change committed content in ways pre-commit validation
never saw, so the check must re-run for real rather than trusting the
executor's own report).

Both refuse to launch anything outside `SAFE_VALIDATION_BINARIES` — validation
runs repo CHECKS, never repo mutations. This module owns the one definition
so the allowlist cannot drift between the two call sites.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Sequence

#: Validation commands may only start with these binaries.
SAFE_VALIDATION_BINARIES = frozenset(
    {"ruff", "pytest", "python", "python3", "npm", "npx", "tsc"}
)


def run_validation_commands(
    commands: Sequence[tuple[str, ...]],
    cwd: Path,
    command_runner=None,
    timeout: float = 1800,
) -> tuple[bool, str]:
    """Run every command in `commands` from `cwd`, in order.

    Returns `(all_passed, summary)`. `all_passed` is True only if every
    command ran and exited 0; a refused binary, a timeout, or a missing
    binary all count as a failure rather than raising, so a caller can report
    validation failure the same way it reports a nonzero exit. An empty
    `commands` sequence is reported as passed (nothing configured, nothing to
    fail) with a summary saying so — callers that require at least one
    command must check for that themselves.
    """
    runner = command_runner or subprocess.run
    parts: list[str] = []
    all_ok = True
    for argv in commands:
        command = " ".join(argv)
        binary = Path(argv[0]).name if argv else ""
        if binary not in SAFE_VALIDATION_BINARIES:
            all_ok = False
            parts.append(f"{command}: REFUSED (binary {binary!r} is not a safe validation binary)")
            continue
        try:
            proc = runner(
                list(argv), cwd=str(cwd), capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            all_ok = False
            parts.append(f"{command}: TIMEOUT")
            continue
        except FileNotFoundError:
            all_ok = False
            parts.append(f"{command}: NOT FOUND")
            continue
        ok = proc.returncode == 0
        all_ok = all_ok and ok
        if ok:
            parts.append(f"{command}: PASS")
        else:
            # A short tail of the combined output on failure — enough for a
            # park message or a log line to be actionable without echoing an
            # unbounded amount of tool output.
            output = (proc.stdout or "") + (proc.stderr or "")
            tail = output.strip().splitlines()[-1].strip() if output.strip() else "(no output)"
            parts.append(f"{command}: FAIL ({tail[:200]})")
    if not parts:
        return True, "(no validation commands configured)"
    return all_ok, "; ".join(parts)
