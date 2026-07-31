"""The validation-environment boundary: test-database credentials reach the
post-writer validation subprocess and nothing else.

**The problem this exists to solve.** A task's declared validation can be the
real backend suite (`rt-01` declares `ruff check .` plus
`python3 -m pytest -n auto -q` under `lexy-app/backend`). That suite talks to
Postgres, reading `DB_HOST`/`DB_PORT`/`DB_NAME`/`DB_USER`/`DB_PASSWORD` and
`SECRET_KEY` from the process environment (verified: `database.py:70-73`,
`core/security.py:11`, `tests/conftest.py:37-42`). A worker repo is a fresh
clone, and `.env` is gitignored, so those variables are absent and every
DB-backed test fails authentication — validation cannot honestly run. The
three obvious fixes are all wrong: copying `.env` into worker repos puts the
production database password, the JWT signing key and the Anthropic API key
on disk in every worker; sourcing `.env` into the loop's own environment
hands all of it to the write-capable agent subprocess through ordinary
inheritance; narrowing the declared validation makes the check vacuous.

**What this module does instead.** One operator-authored file, outside every
directory the loop writes to, holding ONLY the six variables the suite needs,
parsed under a strict allowlist. Those values are injected into the
validation subprocess and REMOVED from the writer subprocess's environment.
The writer never learns the file's path either.

**What this is NOT.** It is not an OS sandbox. It separates credentials from
the writer PROCESS; it does not stop a process that can already run arbitrary
code from reading the file itself, and the write-capable agent has no path
jail (`docs/SECURITY.md` S24, still OPEN — escape is detected after the fact,
not prevented). The guarantee is scoped and mechanical: a writer subprocess
that reads its own environment — which is how every library, test helper and
config loader in this repository finds credentials — finds nothing.

**Allowlist deviation from the brief, stated plainly.** The brief named
`JWT_SECRET_KEY`. This repository reads `SECRET_KEY` (`core/security.py:11`,
which raises `RuntimeError("SECRET_KEY is not set in .env")` at import), and
`.env.example` ships `SECRET_KEY`. Verified empirically: in a fresh clone with
no `.env`, `pytest --collect-only` fails to import six test modules until
`SECRET_KEY` is set, after which all 1260 tests collect with no other variable
supplied. The allowlist therefore carries `SECRET_KEY`. Implementing the brief
literally would have rejected the real name as an unknown key and moved the
failure rather than fixing it. No aliasing: `JWT_SECRET_KEY` is not accepted.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Iterable, Mapping

from .errors import ConfigError

#: The ONLY variable names a validation env file may define, and the only ones
#: injected into a validation subprocess. Deliberately not extensible by
#: configuration: widening it is a security decision that belongs in a diff,
#: not in a TOML file. `ANTHROPIC_API_KEY` / `LLM_API_KEY` are absent on
#: purpose — provider keys have no business in a validation run, and the
#: backend imports cleanly without them (verified: 1260 tests collect with
#: `ANTHROPIC_API_KEY` unset; the Anthropic client only raises when a call is
#: actually made).
VALIDATION_ENV_ALLOWLIST: tuple[str, ...] = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
    "SECRET_KEY",
)
_ALLOWED = frozenset(VALIDATION_ENV_ALLOWLIST)

#: Values that are secret in the strong sense. These are ALWAYS redacted from
#: any text this module is asked to sanitize, regardless of length, and carry
#: a minimum length so a three-character password can never slip through a
#: length-based redaction rule (the hole is closed at load time instead).
SECRET_VALUE_KEYS = frozenset({"DB_PASSWORD", "SECRET_KEY"})
MIN_SECRET_LENGTH = 8

#: Non-secret values (host/port/name/user) are redacted too — the brief says
#: never log these values — but only above this length, so a two-character
#: database name cannot turn every digit of unrelated output into a redaction
#: marker. Secrets bypass this floor entirely (see above).
_MIN_REDACTABLE_LENGTH = 4

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Any environment variable whose NAME contains this is stripped from writer
#: subprocesses alongside the allowlist itself — belt and braces for the
#: brief's "the writer must never receive `validation_env_file`". The path is
#: a TOML config value and never enters an environment in the first place;
#: this makes that property hold even if a future caller passes it through one.
_PATH_VAR_MARKER = "VALIDATION_ENV_FILE"


class ValidationEnv:
    """A loaded validation env file: the six values, plus the operations that
    are allowed to touch them.

    The values are reachable only through `apply()` (which builds a subprocess
    environment) and `redact()` (which removes them from text). There is no
    accessor that returns them, `__repr__`/`__str__` name the keys and never
    the values, and the class is deliberately not a dataclass — a generated
    `__repr__` would print the mapping into the first pytest assertion diff or
    unhandled traceback that touched it.
    """

    __slots__ = ("_path", "_values")

    def __init__(self, path: Path, values: Mapping[str, str]):
        self._path = Path(path)
        self._values = dict(values)

    @property
    def path(self) -> Path:
        return self._path

    def keys(self) -> tuple[str, ...]:
        """The variable names present, in allowlist order. Names are not
        secret; values are never exposed."""
        return tuple(k for k in VALIDATION_ENV_ALLOWLIST if k in self._values)

    def __repr__(self) -> str:  # pragma: no cover - exercised via tests on str()
        return f"<ValidationEnv path={self._path} keys={list(self.keys())} values=redacted>"

    __str__ = __repr__

    def describe(self) -> dict:
        """Non-secret diagnostics for `doctor` and transcripts: where the file
        is and which names it defines. Never any value."""
        return {
            "validation_env_file": str(self._path),
            "keys": list(self.keys()),
            "values": "redacted (never logged, never sent to a reviewer)",
        }

    def apply(self, base_env: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment mapping the VALIDATION subprocess runs under.

        Starts from `base_env` (a copy of `os.environ` when omitted), strips
        every allowlisted name that was already there, then sets exactly the
        loaded values. The strip-then-set order is the point: whatever the
        operator happens to have exported — including a full `.env` sourced
        into the loop's shell, which this design exists to make unnecessary —
        cannot reach a validation run through inheritance. The file is the
        only channel.
        """
        env = strip_validation_vars(base_env)
        env.update(self._values)
        return env

    def redact(self, text: str) -> str:
        """`text` with every loaded value replaced by `[redacted <NAME>]`.

        Longest value first, so a value that contains another (a password
        embedded in a URL, a database name that is a prefix of the user name)
        is not half-replaced by the shorter one. Applied to every validation
        summary this loop produces, which is what keeps values out of
        `state.last_validation` — a string that reaches `state.json`, the
        transcript, blocker records AND the review packet sent to ChatGPT.
        """
        if not text:
            return text
        redactable = [
            (value, name)
            for name, value in self._values.items()
            if name in SECRET_VALUE_KEYS or len(value) >= _MIN_REDACTABLE_LENGTH
        ]
        for value, name in sorted(redactable, key=lambda pair: len(pair[0]), reverse=True):
            text = text.replace(value, f"[redacted {name}]")
        return text


def strip_validation_vars(base_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of `base_env` (default `os.environ`) with every allowlisted
    variable and every `*VALIDATION_ENV_FILE*` variable removed.

    This is what the write-capable `claude` subprocess and every worker git
    subprocess run under. It is a REMOVAL, not a failure to add: the writer
    inherits the loop's environment by construction, so the variables have to
    be taken out explicitly for the boundary to mean anything.
    """
    env = dict(os.environ if base_env is None else base_env)
    for key in list(env):
        if key in _ALLOWED or _PATH_VAR_MARKER in key.upper():
            del env[key]
    return env


def redact_with(env: "ValidationEnv | None", text: str) -> str:
    """`env.redact(text)` when there is a loaded env, `text` unchanged
    otherwise — so call sites do not each repeat the None check."""
    return text if env is None else env.redact(text)


# ---- the repository's own production marker ---------------------------------


def repo_declared_db_name(repo_root: Path) -> str:
    """The database name this repository declares as its application database,
    read from the git-tracked `.env.example`, or `""` if it cannot be
    determined.

    This is the ONE production marker the repository actually defines. It is
    an exact string the repo ships (`DB_NAME=german_vocabulary`, echoed by
    `postprocessing/db_config.py`'s fallback and `postprocessing/README.md`),
    not a pattern — there is deliberately no "contains prod / ends with _prod"
    heuristic here, because such a rule refuses correct setups and passes
    dangerous ones with equal confidence.

    Two limits, written down so nobody "improves" this later:

      * It is NOT a test-vs-production discriminator. It rejects one known
        name. An operator who points this at a second real database gets no
        warning, and there is no way for this module to detect that. Supplying
        a dedicated throwaway database remains the operator's responsibility.
      * There is NO host refusal, on purpose. `.env.example`'s `DB_HOST` is
        `localhost`, which is exactly where a legitimate dedicated test
        database lives — refusing it would refuse the intended configuration.
    """
    example = Path(repo_root) / ".env.example"
    try:
        text = example.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "DB_NAME":
            return value.strip().strip("\"'")
    return ""


# ---- loading ----------------------------------------------------------------


def _is_nested(candidate: Path, boundary: Path) -> bool:
    try:
        candidate.relative_to(boundary)
        return True
    except ValueError:
        return False


def _boundaries(repo_root: Path, state_dir: Path, workers_root: Path | None) -> list[tuple[str, Path]]:
    repo_resolved = Path(repo_root).resolve()
    out: list[tuple[str, Path]] = [
        ("the primary checkout", repo_resolved),
        ("the state directory", Path(state_dir).resolve()),
    ]
    if workers_root is not None:
        out.append(("the worker root", Path(workers_root).expanduser().resolve()))
    try:
        from .publisher import publisher_hooks_path, publisher_repo_path

        out.append(("the publisher repo", publisher_repo_path(state_dir)))
        out.append(("the publisher hooks dir", publisher_hooks_path(state_dir)))
    except Exception:  # pragma: no cover - publisher.py always importable in practice
        pass
    return out


def validate_validation_env_path(
    path: Path | str,
    repo_root: Path,
    state_dir: Path,
    workers_root: Path | None = None,
) -> list[str]:
    """Human-readable violations in the file's LOCATION and permissions —
    everything checkable without parsing it. Empty means it is safe to read.

    Refuses a relative path, a missing file, a non-regular file, a SYMLINK
    (checked before any `resolve()`, so a link pointing at `~/.env` is caught
    as a link rather than silently followed to its target), a file not owned
    by the current user, and any group/world permission bit. Location: the
    file must live outside the primary checkout, the state directory, the
    worker root and both publisher paths — so it is never inside a tree a
    worker can reach, never snapshotted by the escape detector, and never
    accidentally committed.

    Returns violations rather than raising so `doctor` can report all of them
    at once; `load_validation_env` turns the first into a `ConfigError`.
    """
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        return [
            f"validation_env_file must be an absolute path, got {path!r} "
            "(after expanding '~')"
        ]
    if raw.is_symlink():
        return [
            f"validation_env_file {raw} is a symlink — refused before resolving it, "
            "so a link into the real .env cannot be followed by accident"
        ]
    if not raw.exists():
        return [f"validation_env_file {raw} does not exist"]
    if not raw.is_file():
        return [f"validation_env_file {raw} is not a regular file"]

    violations: list[str] = []
    st = raw.stat()
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        violations.append(
            f"validation_env_file {raw} is group/world accessible (mode "
            f"{mode:04o}) — it holds credentials; run: chmod 600 {raw}"
        )
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        violations.append(
            f"validation_env_file {raw} is not owned by the user running the loop "
            f"(uid {st.st_uid} != {os.getuid()})"
        )

    resolved = raw.resolve()
    for label, boundary in _boundaries(repo_root, state_dir, workers_root):
        if _is_nested(resolved, boundary):
            violations.append(
                f"validation_env_file ({resolved}) is inside {label} ({boundary}) "
                "— it must live entirely outside every one of these, so no worker "
                "or publisher process can reach it and it can never be committed"
            )
    return violations


def parse_validation_env(text: str, source: Path | str) -> dict[str, str]:
    """Parse an env file body under the strict allowlist. Fails closed on
    anything unexpected; no message ever contains a value.

    Accepted line shapes: blank, `# comment`, and `KEY=VALUE` where KEY is an
    identifier in `VALIDATION_ENV_ALLOWLIST` and VALUE is non-empty, with
    optional surrounding matched quotes and no escape processing. Everything
    else — `export KEY=V`, a bare word, a repeated key, a key that is merely
    plausible (`DATABASE_URL`, `ANTHROPIC_API_KEY`) — raises.
    """
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(
                f"{source}:{lineno}: malformed line — expected KEY=VALUE, a comment, "
                "or a blank line"
            )
        key, _, value = line.partition("=")
        key = key.strip()
        if not _KEY_RE.match(key):
            raise ConfigError(
                f"{source}:{lineno}: malformed key — must be a plain identifier "
                "(no 'export ' prefix, no quoting, no spaces)"
            )
        if key not in _ALLOWED:
            raise ConfigError(
                f"{source}:{lineno}: {key!r} is not in the validation allowlist "
                f"({', '.join(VALIDATION_ENV_ALLOWLIST)}) — this file may define "
                "nothing else, so it can never become a channel for API keys or "
                "provider credentials"
            )
        if key in values:
            raise ConfigError(
                f"{source}:{lineno}: duplicate key {key!r} — refusing rather than "
                "silently taking the last one"
            )
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not value:
            raise ConfigError(f"{source}:{lineno}: {key} has an empty value")
        values[key] = value

    missing = [k for k in VALIDATION_ENV_ALLOWLIST if k not in values]
    if missing:
        raise ConfigError(
            f"{source} is missing required key(s): {', '.join(missing)} — all of "
            f"{', '.join(VALIDATION_ENV_ALLOWLIST)} must be present, because a "
            "partially configured database is a confusing failure deep inside a "
            "test run rather than a clear refusal here"
        )
    for key in sorted(SECRET_VALUE_KEYS):
        if len(values[key]) < MIN_SECRET_LENGTH:
            raise ConfigError(
                f"{source}: {key} is shorter than the {MIN_SECRET_LENGTH}-character "
                "minimum — short secrets are refused here so that redaction of "
                "validation output can never be defeated by a value too short to "
                "match safely"
            )
    return values


def load_validation_env(
    path: Path | str,
    repo_root: Path,
    state_dir: Path,
    workers_root: Path | None = None,
    forbidden_db_names: Iterable[str] = (),
) -> ValidationEnv:
    """Load and validate the file at `path`. Raises `ConfigError` on any
    violation; the message never contains a value.

    `forbidden_db_names` defaults to the repository's own declared database
    name (`repo_declared_db_name`) when left empty — see that function for
    exactly what that marker is and, more importantly, what it is not.
    """
    violations = validate_validation_env_path(path, repo_root, state_dir, workers_root)
    if violations:
        raise ConfigError(violations[0])

    resolved = Path(path).expanduser()
    values = parse_validation_env(resolved.read_text(encoding="utf-8"), resolved)

    forbidden = {n.strip().lower() for n in forbidden_db_names if n and n.strip()}
    if not forbidden:
        declared = repo_declared_db_name(repo_root)
        if declared:
            forbidden = {declared.lower()}
    if values["DB_NAME"].strip().lower() in forbidden:
        raise ConfigError(
            f"{resolved}: DB_NAME is the database name this repository declares "
            "in .env.example as its application database. Validation runs the "
            "real backend suite, which creates and deletes rows — point it at a "
            "dedicated throwaway database instead. (This check refuses one exact "
            "known name; it cannot tell a test database from a production one in "
            "general, so a dedicated database is still your responsibility.)"
        )
    return ValidationEnv(resolved, values)
