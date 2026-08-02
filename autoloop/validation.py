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
so the binary allowlist cannot drift between the two call sites.

**The ENVIRONMENT a validation command runs under is owned here too, and it
is always explicit.** `run_validation_commands` never lets a subprocess simply
inherit `os.environ`: it passes `strip_validation_vars(os.environ)` — the
parent environment minus every name in `VALIDATION_ENV_ALLOWLIST` — and then
overlays the operator's `ValidationEnv` when one is configured. That ordering
is the whole boundary: the configured file is the ONLY way a database
credential can reach a validation run, so an operator who sources `.env` into
the loop's shell does not silently change what validation connects to, and a
run with no file configured fails honestly rather than picking up ambient
credentials. Summaries are redacted through the same `ValidationEnv` before
they are returned, because the returned string becomes
`state.last_validation` — which reaches `state.json`, the transcript, blocker
records, and the review packet sent to the reviewer.

Caveat for anyone editing `audit/executor.py`: that module has its OWN
validation runner (`AuditExecutor._run_validation`) which shares
`SAFE_VALIDATION_BINARIES` but not this function. It runs read-only audit
checks with no writer involved and deliberately gets NO credentials, so the
two can now differ on environment even though they agree on binaries. If that
ever needs to change, route it through this function rather than growing a
second env policy there.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Sequence

from .validation_env import ValidationEnv, redact_with, strip_validation_vars

#: Validation commands may only start with these binaries.
SAFE_VALIDATION_BINARIES = frozenset(
    {"ruff", "pytest", "python", "python3", "npm", "npx", "tsc"}
)


#: Lines that NAME what failed, as opposed to counting it. pytest prints
#: these in its short summary; unittest and ruff use the same leading words.
_FAILURE_LINE_RE = re.compile(r"^(FAILED|ERROR)\b")

#: Terminal colour codes. pytest emits them even under `-q` when it thinks it
#: has a tty, and they survive `capture_output` into blocker text and park
#: messages, where they render as literal `\x1b[31m` noise.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

#: Bounds on the digest. Generous enough to name a dozen failing tests,
#: bounded so a catastrophic run cannot flood a park message or a review
#: packet with tool output.
_DIGEST_MAX_LINES = 12
_DIGEST_MAX_CHARS = 700


def failure_digest(
    output: str,
    max_lines: int = _DIGEST_MAX_LINES,
    max_chars: int = _DIGEST_MAX_CHARS,
) -> str:
    """What actually failed, not just how many things did.

    This used to be `splitlines()[-1]` — the LAST line of output. For pytest
    that is the count line ("1 failed, 992 passed"), which discards the
    `FAILED <file>::<test>` lines printed immediately above it. The effect
    was that a refused commit could not be diagnosed from its own blocker
    record: on 2026-08-02 an audit was refused on a single failing test and
    the only way to learn which one was to re-run the tree by hand. A flaky
    gate is bad; a flaky gate that does not say what flaked is worse.

    Keeps the naming lines AND the final count line, since the count is what
    tells you whether one test failed or four hundred did.
    """
    text = _ANSI_RE.sub("", output).strip()
    if not text:
        return "(no output)"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    named = [line for line in lines if _FAILURE_LINE_RE.match(line)]
    picked = named[:max_lines]
    if len(named) > max_lines:
        picked.append(f"(+{len(named) - max_lines} more)")
    # The last line is the count/summary for every runner we use. Include it
    # unless it is already one of the named lines (a run whose only output
    # IS a FAILED line).
    if lines[-1] not in picked:
        picked.append(lines[-1])
    digest = "; ".join(picked)
    if len(digest) > max_chars:
        digest = digest[: max_chars - 3].rstrip() + "..."
    return digest


def run_validation_commands(
    commands: Sequence[tuple[str, ...]],
    cwd: Path,
    command_runner=None,
    timeout: float = 1800,
    validation_env: ValidationEnv | None = None,
) -> tuple[bool, str]:
    """Run every command in `commands` from `cwd`, in order.

    Returns `(all_passed, summary)`. `all_passed` is True only if every
    command ran and exited 0; a refused binary, a timeout, or a missing
    binary all count as a failure rather than raising, so a caller can report
    validation failure the same way it reports a nonzero exit. An empty
    `commands` sequence is reported as passed (nothing configured, nothing to
    fail) with a summary saying so — callers that require at least one
    command must check for that themselves.

    `validation_env`, when given, supplies the database credentials the
    commands run under (see this module's docstring) and redacts its own
    values out of `summary`. When it is None the subprocess environment is
    still built explicitly — the parent environment MINUS the allowlisted
    names — so "no file configured" means "no credentials", never "whatever
    the operator happened to export".
    """
    runner = command_runner or subprocess.run
    env = (
        validation_env.apply()
        if validation_env is not None
        else strip_validation_vars()
    )
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
                list(argv),
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
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
            output = (proc.stdout or "") + (proc.stderr or "")
            parts.append(f"{command}: FAIL ({failure_digest(output)})")
    if not parts:
        return True, "(no validation commands configured)"
    # Redacted LAST, over the assembled summary, so every branch above is
    # covered by one call rather than each remembering to sanitize itself.
    return all_ok, redact_with(validation_env, "; ".join(parts))
