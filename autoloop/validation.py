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

**WHICH tests a per-commit run needs is decided here too** — see
`select_validation_commands` and the "per-commit test selection" section at the
bottom of this module. That is a strictly separate function from the flag
normalization above and deliberately so: `effective_validation_command` is
asserted idempotent and depends on nothing but its argv, whereas selection reads
the repository's import graph and the commit's changed paths. Folding the two
together would make a pure argv rewrite depend on the filesystem.

`select_validation_commands` is deliberately PHASE-AGNOSTIC: it takes a command
list, a set of changed repo-relative paths and a repo root, and knows nothing
about commits. Pre-commit inputs satisfy that signature exactly — `git
status`-derived dirty paths and the worker-repo root — which is why adopting it
at the executor's own call site (`implement_executor.py`, where `changed` and
`git.repo_root` are already in hand) needs no change here at all. Only ONE call
site consumes it today (`orchestrator._run_post_commit_validation`); the
pre-commit run is still full, `PRECOMMIT_EVIDENCE` says so in the packet, and
`test_test_selection.py` pins both halves of that statement.

Caveat for anyone editing `audit/executor.py`: that module has its OWN
validation runner (`AuditExecutor._run_validation`) which shares
`SAFE_VALIDATION_BINARIES` but not this function. It runs read-only audit
checks with no writer involved and deliberately gets NO credentials — and no
flag normalization either, since it grades the checkout rather than a worker
repo a gate is about to inspect. If that ever needs to change, route it through
this function rather than growing a second policy there.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

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


# ---- per-commit test selection ----------------------------------------------
#
# WHY THIS IS AN IMPORT GRAPH AND NOT FILENAME SIMILARITY.
#
# The obvious rule — "a changed file selects the test file with the matching
# name" — was tried in this repository and is disproven by a specific commit.
# On 2026-08-06 auto-01's candidate f06454b5 changed 16 files and updated the
# four test files it touched. All five failures were in
# `autoloop/tests/test_v1_smoke.py`, a file that commit never touched: it
# imports `autoloop.publisher` and `autoloop.orchestrator` directly and
# exercises the publisher and protected-branch push guards the commit changed.
# A name-matching rule runs green there and ships the regression.
#
# Reachability answers the question that actually matters — can this test
# EXECUTE the changed code? — and it answers it in the direction the risk runs:
# from the changed module OUTWARD along the reverse of every import edge, so a
# test file nobody touched is selected whenever it can reach the change through
# any chain of repository modules. `test_v1_smoke.py` is one hop from
# `publisher.py` under that model, which is the property this whole section
# exists to have.
#
# WHAT IT CANNOT SEE, AND WHAT IS DONE ABOUT IT. A static graph misses coupling
# that is not an import statement: `importlib.import_module`, `__import__`,
# `pytest.importorskip`, a test that spawns a fresh interpreter, and a file that
# does not parse at all. None of those are argued away — every such file is put
# on the frontier UNCONDITIONALLY (`ImportGraph.opaque`), so it is reachable
# from any change and therefore always selected. It also cannot see a
# non-Python input: a `.toml` a test reads, a fixture, an `.ini` that decides
# collection. Those are not modelled at all; a commit touching one runs the FULL
# suite. Every judgement in this section resolves the same way — when
# reachability cannot be established, the answer is "run everything", never
# "assume unrelated".

#: The two answers to "which tests does this commit need?", i.e. the accepted
#: values of `[audit] test_selection`.
TEST_SELECTION_REACHABLE = "reachable"
TEST_SELECTION_FULL = "full"
TEST_SELECTION_MODES: tuple[str, ...] = (TEST_SELECTION_REACHABLE, TEST_SELECTION_FULL)

#: Directory names the graph walk never descends into. Dot-prefixed directories
#: are skipped as a class (`.git`, `.venv`, `.pytest_cache`, `.autoloop`); this
#: names the rest.
_GRAPH_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        "venv",
        "env",
        "site-packages",
        "build",
        "dist",
    }
)

#: Above this many Python files the walk gives up and the run widens to the
#: full suite. A bound on the WORK, not a judgement about the repository: this
#: parses every file with `ast` on every validated commit, and a checkout that
#: large is one where the parse itself would cost more than the tests it saves.
#: This repo is ~430 Python files, so the bound leaves an order of magnitude of
#: headroom.
_GRAPH_MAX_FILES = 6000

#: Function names whose presence means "this file's imports cannot be read from
#: its import statements". Matched on the called name only (`import_module`,
#: not `importlib.import_module`) so an aliased import cannot hide one.
_DYNAMIC_IMPORT_CALLS = frozenset(
    {
        "__import__",
        "import_module",
        "importorskip",
        "load_module",
        "exec_module",
        "module_from_spec",
        "spec_from_file_location",
        "run_module",
        "run_path",
    }
)

#: String constants that mean "this file may launch a fresh interpreter", which
#: can import anything in the repository without a single import statement.
#: `sys.executable` is detected separately, as an attribute.
#:
#: Only consulted for TEST files (see `_file_is_opaque`). Half the modules in
#: this package mention an interpreter name because running one is their job —
#: `run_validation_commands` two hundred lines above is the clearest case — and
#: treating those as opaque would put a production module on every change's
#: frontier, dragging in everything that imports it. What the signal is actually
#: about is a TEST that exercises repository code through a subprocess instead
#: of an import, which is the coupling the graph cannot see.
_INTERPRETER_LITERALS = frozenset({"python", "python3"})

#: How many entries the evidence line names before it counts the rest. The
#: string it builds becomes `state.last_validation` — state.json, the
#: transcript, blocker records and the CONTEXT block of every message — so it is
#: bounded for the same reason `failure_digest` and `packet._format_assumptions`
#: are.
_EVIDENCE_MAX_SELECTED = 12
_EVIDENCE_MAX_CONSIDERED = 8

#: The sentence that keeps a narrowed round from reading as LESS validation
#: than the round before it, which is the refusal this whole feature has to
#: survive (hlth-01 and dash-04 each burned five rounds on "the evidence is too
#: narrow", 2026-08-15/16).
#:
#: It is a claim about a DIFFERENT module, so it is stated as the fact it is
#: rather than as an argument, and it is pinned by a test:
#: `test_test_selection.py::test_the_pre_commit_run_is_not_narrowed_today`
#: drives a real `ImplementExecutor` round and asserts the configured command
#: ran unnarrowed. **When `implement_executor.py`'s call site is narrowed, this
#: sentence becomes false and that test fails — both are the reminder to
#: rewrite it here.**
PRECOMMIT_EVIDENCE = (
    "Scope of the narrowing: the POST-COMMIT re-run only. The executor's own "
    "PRE-COMMIT run of this same change (`implement_executor.py`) executed "
    "every configured command in full, so what is recorded here is a "
    "reachability-selected subset ADDED to a full-suite run of the same tree, "
    "never a replacement for one."
)

#: pytest flags that consume the NEXT argv token. A token following one of
#: these is a value, never a test path — `-m isolated` must not select a file
#: called `isolated`.
_PYTEST_VALUE_FLAGS = frozenset(
    {
        "-c",
        "-k",
        "-m",
        "-n",
        "-o",
        "-p",
        "-r",
        "-W",
        "--numprocesses",
        "--maxfail",
        "--rootdir",
        "--ignore",
        "--ignore-glob",
        "--deselect",
        "--dist",
        "--junitxml",
        "--tb",
        "--durations",
        "--log-file",
        "--override-ini",
    }
)

#: pytest flags that consume nothing. Anything starting with `-` that is in
#: NEITHER set (and is not a `--flag=value`) is UNKNOWN, and an unknown flag
#: makes this refuse to rewrite that command at all rather than guess which of
#: its tokens are paths.
_PYTEST_BOOL_FLAGS = frozenset(
    {
        "-q",
        "-qq",
        "-v",
        "-vv",
        "-s",
        "-x",
        "-l",
        "-ra",
        "-rA",
        "--quiet",
        "--verbose",
        "--exitfirst",
        "--collect-only",
        "--co",
        "--no-header",
        "--no-summary",
        "--strict-markers",
        "--strict-config",
        "--showlocals",
        "--full-trace",
        "--disable-warnings",
    }
)


def _is_test_file(rel: str) -> bool:
    """pytest's own default discovery rule, and only that.

    Deliberately not "lives under a directory called tests": a helper module
    sitting beside the tests is a graph NODE (things import it) but is not
    something to hand pytest as a path.
    """
    name = PurePosixPath(rel).name
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _module_parts(rel: str) -> tuple[str, ...]:
    """The dotted module a repo-relative path would be imported as."""
    parts = PurePosixPath(rel).parts
    if parts[-1] == "__init__.py":
        return tuple(parts[:-1])
    return tuple(parts[:-1]) + (parts[-1][:-3],)


def _python_files(root: Path) -> tuple[str, ...]:
    """Every `.py` file in the checkout, repo-relative, sorted.

    Sorted at every level (`os.walk`'s `dirnames` too) so the whole graph — and
    therefore the selection built from it — is a pure function of the tree, not
    of filesystem iteration order. Symlinked directories are not followed:
    `os.walk` does not by default, and a symlink out of the checkout would put
    files this cannot map to repo-relative paths into the graph.
    """
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _GRAPH_SKIP_DIRS and not name.startswith(".")
        )
        for name in sorted(filenames):
            if name.endswith(".py"):
                found.append(Path(dirpath, name).relative_to(root).as_posix())
                if len(found) > _GRAPH_MAX_FILES:
                    return tuple(sorted(found))
    return tuple(sorted(found))


def _file_is_opaque(tree: ast.AST, rel: str) -> bool:
    """Can this file reach repository code by some route other than its own
    import statements? A `True` here is not a defect report — it puts the file
    on the frontier for EVERY change, which is the conservative answer.

    A dynamic import counts anywhere; an interpreter subprocess counts only in a
    test file or a conftest, for the reason `_INTERPRETER_LITERALS` gives.
    """
    watch_interpreter = _is_test_file(rel) or PurePosixPath(rel).name == "conftest.py"
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = ""
            if isinstance(func, ast.Attribute):
                called = func.attr
            elif isinstance(func, ast.Name):
                called = func.id
            if called in _DYNAMIC_IMPORT_CALLS:
                return True
        elif watch_interpreter and isinstance(node, ast.Attribute):
            if (
                node.attr == "executable"
                and isinstance(node.value, ast.Name)
                and node.value.id == "sys"
            ):
                return True
        elif watch_interpreter and isinstance(node, ast.Constant):
            if isinstance(node.value, str) and node.value in _INTERPRETER_LITERALS:
                return True
    return False


def _imported_modules(tree: ast.AST, rel: str) -> set[str]:
    """Every dotted module name this file names in an import statement.

    Relative imports are resolved against the file's own package, so
    `from .validation import x` inside `autoloop/orchestrator.py` yields
    `autoloop.validation`. `from a import b` yields BOTH `a` and `a.b`, because
    `b` may be a submodule rather than an attribute and only the file map
    downstream can tell which.
    """
    parts = _module_parts(rel)
    package = list(parts) if rel.endswith("__init__.py") else list(parts[:-1])
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `level` counts dots: 1 = this package, 2 = its parent.
                keep = len(package) - (node.level - 1)
                if keep < 0:
                    continue
                base_parts = package[:keep]
            else:
                base_parts = []
            if node.module:
                base_parts = base_parts + node.module.split(".")
            base = ".".join(base_parts)
            if base:
                names.add(base)
            for alias in node.names:
                if alias.name != "*":
                    names.add(".".join(filter(None, [base, alias.name])))
    return names


@dataclass(frozen=True)
class ImportGraph:
    """Who imports whom, in the direction risk travels.

    `importers[path]` is every repo file that imports `path` — the REVERSE of
    the import statements, because the question being asked is "what could
    execute this?" rather than "what does this need?".
    """

    files: frozenset[str]
    importers: dict[str, frozenset[str]]
    #: Files whose own imports could not be read (dynamic import, interpreter
    #: subprocess, parse error). Seeded onto the frontier for every change.
    opaque: frozenset[str]
    #: `True` when the walk hit `_GRAPH_MAX_FILES` and stopped.
    truncated: bool = False

    @property
    def test_files(self) -> frozenset[str]:
        return frozenset(path for path in self.files if _is_test_file(path))

    def reachable_from(self, seeds: Iterable[str]) -> frozenset[str]:
        """Transitive closure over `importers`, starting from `seeds` plus every
        opaque file. Iterative rather than recursive — an import cycle is normal
        in a real package and must not blow the stack."""
        frontier = [path for path in sorted(set(seeds) | set(self.opaque)) if path in self.files]
        seen = set(frontier)
        while frontier:
            current = frontier.pop()
            for importer in self.importers.get(current, frozenset()):
                if importer not in seen:
                    seen.add(importer)
                    frontier.append(importer)
        return frozenset(seen)


def build_import_graph(root: Path) -> ImportGraph:
    """Parse every `.py` file under `root` and build the reverse import graph.

    Two edge kinds beyond the literal import statement, both conservative:

    * **Package `__init__.py`.** Importing `a.b.c` executes `a/__init__.py` and
      `a/b/__init__.py`, so an edge is added to each existing ancestor package.
      Without it a change to a package's `__init__` would reach nothing.
    * **`conftest.py`.** pytest applies a conftest to its whole directory tree
      without any file importing it. Every `.py` file under a conftest's
      directory therefore gets an edge FROM that conftest, so changing one
      selects the tests it configures.

    A file that cannot be parsed is recorded as opaque rather than skipped: its
    imports are unknown, so it must be treated as importing everything.
    """
    files = _python_files(root)
    truncated = len(files) > _GRAPH_MAX_FILES
    if truncated:
        return ImportGraph(frozenset(files), {}, frozenset(files), truncated=True)
    by_module: dict[str, str] = {}
    for rel in files:
        by_module[".".join(_module_parts(rel))] = rel
    importers: dict[str, set[str]] = {}
    opaque: set[str] = set()
    for rel in files:
        try:
            source = (root / rel).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            opaque.add(rel)
            continue
        if _file_is_opaque(tree, rel):
            opaque.add(rel)
        for module in _imported_modules(tree, rel):
            segments = module.split(".")
            for depth in range(1, len(segments) + 1):
                target = by_module.get(".".join(segments[:depth]))
                if target is not None and target != rel:
                    importers.setdefault(target, set()).add(rel)
    for rel in files:
        if PurePosixPath(rel).name != "conftest.py":
            continue
        prefix = PurePosixPath(rel).parent.as_posix()
        prefix = "" if prefix == "." else prefix + "/"
        for other in files:
            if other != rel and other.startswith(prefix):
                importers.setdefault(rel, set()).add(other)
    return ImportGraph(
        files=frozenset(files),
        importers={key: frozenset(value) for key, value in importers.items()},
        opaque=frozenset(opaque),
    )


@dataclass(frozen=True)
class TestSelection:
    """What a per-commit validation run decided to execute, and why.

    `evidence()` is the part a human reads: it becomes part of the validation
    summary, which becomes `state.last_validation` and reaches the reviewer in
    the CONTEXT block of the review message. A narrower run that cannot say what
    it narrowed to is exactly the evidence gap that gets a packet refused, so
    this record is built even when nothing was narrowed.
    """

    #: Commands to actually run, in configured order.
    commands: tuple[tuple[str, ...], ...]
    #: `True` when the whole configured suite runs unmodified.
    widened: bool
    #: Why it widened, or "" when it did not.
    reason: str
    #: Changed paths the decision was made from.
    considered: tuple[str, ...] = ()
    #: Test files selected, repo-relative, sorted.
    selected: tuple[str, ...] = ()
    #: How many test files the graph knows about at all.
    total_test_files: int = 0
    #: Configured commands dropped because no selected test lives under their
    #: declared paths, each with the reason — reported explicitly, never
    #: silently absent from the summary.
    skipped: tuple[tuple[tuple[str, ...], str], ...] = ()
    #: `False` when the configured list has no pytest command at all, i.e.
    #: there was no test selection to make. `evidence()` is then empty.
    applicable: bool = True

    def evidence(self) -> str:
        if not self.applicable:
            return ""
        if self.widened:
            return (
                "test selection: FULL SUITE — every configured test command ran "
                f"unmodified ({self.reason})."
            )
        selected = list(self.selected[:_EVIDENCE_MAX_SELECTED])
        if len(self.selected) > _EVIDENCE_MAX_SELECTED:
            selected.append(f"(+{len(self.selected) - _EVIDENCE_MAX_SELECTED} more)")
        considered = list(self.considered[:_EVIDENCE_MAX_CONSIDERED])
        if len(self.considered) > _EVIDENCE_MAX_CONSIDERED:
            considered.append(f"(+{len(self.considered) - _EVIDENCE_MAX_CONSIDERED} more)")
        dropped = ""
        if self.skipped:
            dropped = " Commands SKIPPED, no selected test under their paths: " + "; ".join(
                f"`{' '.join(argv)}` ({why})" for argv, why in self.skipped
            ) + "."
        return (
            "test selection: SUBSET by import-graph reachability — "
            f"{len(self.selected)} of {self.total_test_files} test file(s) are "
            f"reachable from the {len(self.considered)} changed path(s) in this "
            f"commit range [{', '.join(considered)}] by following the reverse of "
            "every import edge in the repository, transitively, so a test file "
            "the commit did not touch is still selected whenever it can reach "
            f"the change: [{', '.join(selected)}]. Each configured pytest command "
            "ran the selected files under its own declared paths, and every "
            "non-test command (ruff) ran unmodified. Sufficient because a test "
            "can only exercise changed code by importing it directly or through "
            "another repository module, and the cases where that cannot be read "
            "statically are not assumed away: a file using a dynamic import or "
            "spawning an interpreter, or one that fails to parse, is selected "
            "unconditionally, and a commit touching anything outside the "
            "resolvable Python import graph (config, fixtures, docs) runs the "
            f"FULL suite instead.{dropped} {PRECOMMIT_EVIDENCE} To widen, "
            "either OPERATOR lever (a `plan` directive can set neither): "
            '`[audit] test_selection = "full"` in the loop config, which takes '
            "effect on the next round and — since the pre-commit run is not "
            "narrowed either — restores exactly the pre-2026-08-20 behaviour; "
            "or this task's own `validation` commands (`autoloop task-add "
            "--validation ...`, or the same key through the operator inbox), "
            "which are run exactly as declared and never narrowed at either "
            "phase."
        )


def _root_is_narrowable(root: str) -> bool:
    """Is this positional pytest argument a plain repo-relative path?

    A node id (`file.py::test_x`), a glob, or an absolute path is refused
    rather than reinterpreted — the command then runs exactly as configured.
    """
    if not root or root.startswith("-"):
        return False
    if "::" in root or "*" in root or "?" in root:
        return False
    if root.startswith("/") or root.startswith("~"):
        return False
    return True


def _under_root(rel: str, root: str) -> bool:
    normalized = root.rstrip("/")
    if normalized in ("", "."):
        return True
    return rel == normalized or rel.startswith(normalized + "/")


def _positional_indices(args: Sequence[str]) -> list[int] | None:
    """Indices of the test-path arguments in `args` (everything after the
    `pytest` token), or `None` when the argv contains a flag this does not
    recognise.

    Fail-closed on purpose: mistaking a flag's value for a path would hand
    pytest a nonexistent file, and mistaking a path for a flag would silently
    drop a whole tree from the run. An unrecognised flag means the caller keeps
    the command exactly as configured.
    """
    indices: list[int] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            indices.extend(range(index + 1, len(args)))
            return indices
        if token.startswith("-"):
            if token.startswith("--") and "=" in token:
                index += 1
                continue
            if token in _PYTEST_VALUE_FLAGS:
                index += 2
                continue
            if token in _PYTEST_BOOL_FLAGS:
                index += 1
                continue
            # Attached short-flag value: `-nauto`, `-kfoo`.
            if len(token) > 2 and not token.startswith("--") and token[:2] in _PYTEST_VALUE_FLAGS:
                index += 1
                continue
            return None
        indices.append(index)
        index += 1
    return indices


def _retarget_pytest(
    argv: Sequence[str], selected: Sequence[str]
) -> tuple[tuple[str, ...] | None, str]:
    """`(command, note)` — the command to run in place of `argv`, or `None` when
    it should be skipped entirely.

    `note` is "" when `argv` is returned unchanged; otherwise it says what
    happened, for the evidence line. Non-pytest commands (`ruff`) are returned
    untouched, which is what keeps lint running in full on a narrowed round.
    """
    argv = tuple(argv)
    start = _pytest_index(argv)
    if start is None:
        return argv, ""
    args = argv[start + 1 :]
    indices = _positional_indices(args)
    if indices is None:
        return argv, "argv has a flag this selector does not recognise"
    if not indices:
        # No paths at all: the surface is whatever `testpaths` says. Injecting
        # paths would CHANGE what the command means, so it is left alone.
        return argv, "command declares no test paths (surface comes from pytest.ini)"
    roots = [args[i] for i in indices]
    if not all(_root_is_narrowable(root) for root in roots):
        return argv, "command targets a node id, glob or absolute path"
    keep = sorted(rel for rel in selected if any(_under_root(rel, root) for root in roots))
    if not keep:
        return None, f"no selected test file under {', '.join(roots)}"
    positional = set(indices)
    rest = [token for i, token in enumerate(args) if i not in positional]
    # Paths go back where the FIRST positional token was. Everything before it
    # is by construction non-positional, so a command ending in `--` keeps its
    # meaning: the `--` sits in `head` and the paths land after it.
    head = list(args[: indices[0]])
    tail = rest[len(head) :]
    return argv[: start + 1] + tuple(head) + tuple(keep) + tuple(tail), "narrowed"


def select_validation_commands(
    commands: Sequence[Sequence[str]],
    changed_paths: Sequence[str],
    repo_root: Path,
    mode: str = TEST_SELECTION_REACHABLE,
    full_reason: str = "",
) -> TestSelection:
    """Decide which of `commands`' tests this commit actually needs.

    `changed_paths` is what git says the commit range touched — never what an
    executor reported. `full_reason`, when non-empty, forces the full suite and
    is used verbatim as the recorded reason; that is how a caller says "this
    task declared its own validation" without this function having to know what
    a task is.

    Deterministic for a given (commands, changed_paths, tree): the file walk is
    sorted at every level, reachability is a set closure, and the selected list
    is sorted before it is used. Two runs over the same diff produce the same
    commands and the same evidence string.

    Every uncertainty widens. In order: no pytest command to narrow, mode
    `full`, a caller-supplied reason, no changed paths, a graph that could not
    be built or was truncated, a changed path that is not a Python file the
    graph resolves, and finally a reachability result of zero test files —
    which is treated as a gap in the model rather than as proof that no test
    exercises the change.
    """
    commands = tuple(tuple(argv) for argv in commands)
    applicable = any(_pytest_index(argv) is not None for argv in commands)
    # Blanks dropped rather than carried: git never reports one, but a blank
    # would reach `_module_parts`, whose `parts[-1]` raises on the empty path.
    # It would widen first (an empty string is not in `graph.files`), so this is
    # belt-and-braces against a later reordering of the checks below.
    considered = tuple(sorted({path for path in changed_paths if path.strip()}))

    def full(reason: str, total: int = 0) -> TestSelection:
        return TestSelection(
            commands=commands,
            widened=True,
            reason=reason,
            considered=considered,
            total_test_files=total,
            applicable=applicable,
        )

    if not applicable:
        return full("no pytest command is configured, so there is nothing to select")
    if full_reason:
        return full(full_reason)
    if mode != TEST_SELECTION_REACHABLE:
        return full('[audit] test_selection = "full"')
    if not considered:
        return full("no changed paths were established for this commit range")
    try:
        graph = build_import_graph(repo_root)
    except OSError as exc:
        return full(f"the repository import graph could not be read ({exc})")
    if graph.truncated:
        return full(
            f"the checkout has more than {_GRAPH_MAX_FILES} Python files, above "
            "the bound this selector will parse"
        )
    total = len(graph.test_files)
    if not total:
        return full("the import graph found no test files at all", total)
    unresolved = [path for path in considered if path not in graph.files]
    if unresolved:
        shown = ", ".join(unresolved[:_EVIDENCE_MAX_CONSIDERED])
        if len(unresolved) > _EVIDENCE_MAX_CONSIDERED:
            shown += f" (+{len(unresolved) - _EVIDENCE_MAX_CONSIDERED} more)"
        return full(
            f"{len(unresolved)} changed path(s) are not Python modules the import "
            f"graph resolves, so reachability cannot be established for them: {shown}",
            total,
        )
    reachable = graph.reachable_from(considered)
    selected = tuple(sorted(path for path in reachable if _is_test_file(path)))
    if not selected:
        return full(
            "reachability selected no test file at all, which is likelier a gap "
            "in the model than a change no test exercises",
            total,
        )
    kept: list[tuple[str, ...]] = []
    skipped: list[tuple[tuple[str, ...], str]] = []
    for argv in commands:
        rewritten, note = _retarget_pytest(argv, selected)
        if rewritten is None:
            skipped.append((argv, note))
            continue
        kept.append(rewritten)
    if not kept:
        return full(
            "every configured command would have been skipped, leaving nothing "
            "to validate",
            total,
        )
    return TestSelection(
        commands=tuple(kept),
        widened=False,
        reason="",
        considered=considered,
        selected=selected,
        total_test_files=total,
        skipped=tuple(skipped),
        applicable=True,
    )
