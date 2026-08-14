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

**The pytest flags a validation run needs are owned here too** — see
`effective_validation_commands` below. They are applied to whatever the caller
passes rather than left to the config file, because the config file is copied
once per deployment and then never re-read from the template: an operator whose
`.autoloop/config.toml` predates a flag would keep running without it forever,
and every path into a validation run (the configured default, a task's declared
`validation`, and an `execution.validation_commands` record persisted by a
session that dispatched before the flag existed) funnels through this function.

Caveat for anyone editing `audit/executor.py`: that module has its OWN
validation runner (`AuditExecutor._run_validation`) which shares
`SAFE_VALIDATION_BINARIES` but not this function. It runs read-only audit
checks with no writer involved and deliberately gets NO credentials — and no
flag normalization either, since it grades the checkout rather than a worker
repo a gate is about to inspect. If that ever needs to change, route it through
this function rather than growing a second policy there.
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

#: One xdist worker per core. Validation re-runs the whole configured suite
#: against the committed worker repo on EVERY round, revises included, against
#: a change that usually touches two or three files — serially that is minutes
#: of wall clock per round, and nothing about it FAILS, so nothing would ever
#: report it. Needs `pytest-xdist` in the interpreter the loop invokes (the
#: root `requirements.txt` pulls it in via `lexy-app/backend/requirements.txt`);
#: without it pytest exits 4 on an unrecognised argument and every task fails
#: validation at once, which is the first thing to check if that ever happens.
PARALLEL_ARGS: tuple[str, ...] = ("-n", "auto")

#: pytest writes `.pytest_cache/` the moment a test fails. Validation runs
#: inside the task's own worker repo and the gate right after it refuses a
#: worktree validation dirtied, so one failing test produced two refusals
#: instead of one (2026-08-03). Disabling the plugin keeps a failing run from
#: mutating the tree it is grading.
NO_CACHE_ARGS: tuple[str, ...] = ("-p", "no:cacheprovider")

#: A marker expression that SELECTS `isolated` (as opposed to `not isolated`,
#: which every default run carries via `pytest.ini`'s `addopts`). Such a run
#: stays single-process: the marker means "this test needs its own process",
#: and handing it an xdist worker alongside others gives back exactly the
#: company it was marked to avoid. Ambiguous expressions err toward serial.
_SELECTS_ISOLATED_RE = re.compile(r"\bisolated\b")
_DESELECTS_ISOLATED_RE = re.compile(r"\bnot\s+isolated\b")


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


def _pytest_index(argv: Sequence[str]) -> int | None:
    """Index of the `pytest` token, or None when this is not a pytest run.

    Matched structurally rather than by asking whether "pytest" appears
    anywhere in the argv: a test PATH can contain the word, and `python3 -c`
    programs (`test_validation_env.py` runs one) must never be mistaken for a
    pytest invocation and handed pytest flags.
    """
    if not argv:
        return None
    if Path(argv[0]).name == "pytest":
        return 0
    # Exactly the two interpreter names `SAFE_VALIDATION_BINARIES` allows —
    # deliberately NOT a `python3*` prefix. A versioned basename like
    # `python3.13` (what `sys.executable` gives on many installs) is REFUSED
    # unrun by the allowlist above, so normalizing it here would only decide
    # flags for a command that never launches, and the two rules would drift.
    if Path(argv[0]).name not in ("python", "python3"):
        return None
    # `-m pytest`, wherever it sits (`python3 -X dev -m pytest` is legal).
    # This is the INTERPRETER's `-m`; every `-m` after it is pytest's own
    # marker flag, which is why callers below only ever look past this index.
    for index in range(1, len(argv) - 1):
        if argv[index] == "-m" and Path(argv[index + 1]).name == "pytest":
            return index + 1
    return None


def _declares(args: Sequence[str], flag: str, value: str | None = None) -> bool:
    """Is `flag` already present in `args`, in any spelling pytest accepts?

    Covers separated (`-n auto`, `--numprocesses auto`) and attached (`-nauto`,
    `--numprocesses=auto`) forms. With `value` given, only that exact value
    counts — so `-p no:randomly` does not read as `-p no:cacheprovider`.
    """
    # Every branch below either matches or keeps looking — a non-matching
    # spelling of the same flag (`-p no:randomly` when asked about
    # `-p no:cacheprovider`) must not shadow a real one later in the argv.
    for index, token in enumerate(args):
        if token == flag:
            if value is None or (index + 1 < len(args) and args[index + 1] == value):
                return True
        elif flag.startswith("--") and token.startswith(flag + "="):
            if value is None or token == f"{flag}={value}":
                return True
        elif len(flag) == 2 and len(token) > 2 and token.startswith(flag):
            if value is None or token == flag + value:
                return True
    return False


def _selects_isolated(args: Sequence[str]) -> bool:
    """Does this run ask for the `isolated` marker? (`args` excludes the
    interpreter's own `-m pytest`, so every `-m` here is pytest's.)"""
    for index, token in enumerate(args):
        expression = ""
        if token == "-m" and index + 1 < len(args):
            expression = args[index + 1]
        elif token.startswith("-m") and len(token) > 2:
            expression = token[2:]
        if not expression:
            continue
        if _SELECTS_ISOLATED_RE.search(expression) and not _DESELECTS_ISOLATED_RE.search(
            expression
        ):
            return True
    return False


def effective_validation_command(argv: Sequence[str]) -> tuple[str, ...]:
    """The command that will really run, given the one that was configured.

    A pytest invocation gains `-n auto` (unless it selects the `isolated`
    marker, or already says how many processes it wants — an explicit `-n 0`
    or `-n 4` is an operator decision and is never overridden) and
    `-p no:cacheprovider`. Everything else — `ruff`, `npx vitest`, a bare
    `python3 -c` probe — is returned unchanged.

    Flags are inserted immediately AFTER the `pytest` token rather than
    appended: a command ending in `--` (everything after it is a path, not a
    flag) would otherwise turn `-n auto` into two filenames pytest cannot
    collect.
    """
    argv = tuple(argv)
    start = _pytest_index(argv)
    if start is None:
        return argv
    args = argv[start + 1 :]
    injected: list[str] = []
    if (
        not _selects_isolated(args)
        and not _declares(args, "-n")
        and not _declares(args, "--numprocesses")
    ):
        injected.extend(PARALLEL_ARGS)
    if not _declares(args, "-p", "no:cacheprovider"):
        injected.extend(NO_CACHE_ARGS)
    if not injected:
        return argv
    return argv[: start + 1] + tuple(injected) + args


def effective_validation_commands(
    commands: Sequence[Sequence[str]],
) -> tuple[tuple[str, ...], ...]:
    """`effective_validation_command` over a whole configured list.

    This does NOT add pytest commands to a list that had none, and it does not
    change which tests a command selects — `-m isolated` still runs only the
    isolated marker, and a default run still excludes it via `pytest.ini`. It
    changes only HOW a configured pytest command runs.
    """
    return tuple(effective_validation_command(argv) for argv in commands)


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

    Every command is normalized through `effective_validation_commands` first,
    and it is the EFFECTIVE command that runs and that the summary names — so
    the report says what was actually executed rather than what was configured
    a deployment ago. The summary is still one `PASS`/`FAIL` line per command:
    parallelism lives inside a command, never across the report.
    """
    runner = command_runner or subprocess.run
    env = (
        validation_env.apply()
        if validation_env is not None
        else strip_validation_vars()
    )
    parts: list[str] = []
    all_ok = True
    for argv in effective_validation_commands(commands):
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
