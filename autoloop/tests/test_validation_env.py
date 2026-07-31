"""The validation-environment boundary (`autoloop/validation_env.py`).

These tests ARE the deliverable for this changeset — the module's whole value
is a set of negative guarantees, and a negative guarantee that is not tested
is a comment. Each test below names the failure it exists to catch.

Nothing here needs a database except `test_real_db_validation_command_succeeds`,
which SKIPS with instructions unless the operator points
`AUTOLOOP_TEST_VALIDATION_ENV_FILE` at a real dedicated test database. Credential
DELIVERY into a real DB client is proven without a server by
`test_validation_subprocess_delivers_credentials_to_a_real_db_client`, which
dials a loopback listener and reads the Postgres startup packet off the wire.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from autoloop.audit.agents import AgentSpec, ClaudeCliRunner
from autoloop.errors import ConfigError
from autoloop.implement_executor import implement_agent_runner
from autoloop.validation import run_validation_commands
from autoloop.validation_env import (
    VALIDATION_ENV_ALLOWLIST,
    load_validation_env,
    parse_validation_env,
    repo_declared_db_name,
    strip_validation_vars,
    validate_validation_env_path,
)
from autoloop.worker_env import worker_env

GOOD_BODY = """
# dedicated throwaway test database
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=autoloop_validation_test
DB_USER=validation_user
DB_PASSWORD=super-secret-password
SECRET_KEY=jwt-signing-key-for-tests
""".lstrip()


def write_env_file(path: Path, body: str = GOOD_BODY, mode: int = 0o600) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory that is not the checkout, the state dir, or workers_root."""
    d = tmp_path / "outside"
    d.mkdir()
    return d


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    d = tmp_path / "checkout"
    (d / ".autoloop").mkdir(parents=True)
    (d / ".env.example").write_text("DB_NAME=german_vocabulary\n", encoding="utf-8")
    return d


def load(path: Path, repo: Path) -> object:
    return load_validation_env(path, repo_root=repo, state_dir=repo / ".autoloop")


# ---- 1. the writer cannot observe validation variables ----------------------


def test_writer_subprocess_cannot_observe_validation_variables(tmp_path, monkeypatch):
    """The failure this catches: the write-capable `claude` subprocess
    inherits the loop's environment, so exporting DB credentials for
    validation would hand them to the agent too. It must be an explicit
    removal, and the agent must never learn the file's path either."""
    for name in VALIDATION_ENV_ALLOWLIST:
        monkeypatch.setenv(name, f"secret-value-for-{name}")
    monkeypatch.setenv("AUTOLOOP_VALIDATION_ENV_FILE", "/home/me/validation.env")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout='{"result": "done"}', stderr="")

    runner = implement_agent_runner(tmp_path, runner=fake_run)
    runner.run(AgentSpec(domain="t", title="t", prompt="do the thing"))

    for name in VALIDATION_ENV_ALLOWLIST:
        assert name not in seen["env"], f"writer subprocess received {name}"
    assert "AUTOLOOP_VALIDATION_ENV_FILE" not in seen["env"]
    # ...and nothing leaked through argv either.
    joined = " ".join(seen["argv"])
    assert "secret-value-for" not in joined
    assert "/home/me/validation.env" not in joined
    # The subprocess is still usable: PATH and friends survive the strip.
    assert seen["env"].get("PATH")


def test_read_only_audit_agent_is_stripped_too(tmp_path, monkeypatch):
    """One unconditional strip in `ClaudeCliRunner.run` — not a rule the
    implement path remembers and the audit path forgets."""
    monkeypatch.setenv("DB_PASSWORD", "super-secret-password")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout='{"result": "ok"}', stderr="")

    ClaudeCliRunner(repo_root=tmp_path, runner=fake_run).run(
        AgentSpec(domain="d", title="t", prompt="p")
    )
    assert "DB_PASSWORD" not in seen["env"]


def test_worker_git_env_is_stripped_too(monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "super-secret-password")
    monkeypatch.setenv("SECRET_KEY", "jwt-signing-key-for-tests")
    env = worker_env()
    assert "DB_PASSWORD" not in env
    assert "SECRET_KEY" not in env


# ---- 2. the validator receives exactly the allowlist ------------------------


def test_validator_receives_exactly_the_allowlist(tmp_path, repo, outside, monkeypatch):
    """The positive half of the boundary: the validation subprocess gets the
    six values FROM THE FILE, and an ambient value of the same name is
    overridden rather than inherited (an operator who sourced `.env` into the
    loop's shell must not silently change what validation connects to)."""
    monkeypatch.setenv("DB_NAME", "ambient_production_db")
    monkeypatch.setenv("PATH", os.environ.get("PATH", ""))
    env_file = write_env_file(outside / "validation.env")
    loaded = load(env_file, repo)

    seen = {}

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    ok, _ = run_validation_commands(
        [("ruff", "check", ".")], tmp_path, command_runner=fake_run, validation_env=loaded
    )
    assert ok
    assert seen["env"]["DB_NAME"] == "autoloop_validation_test"
    assert seen["env"]["DB_HOST"] == "127.0.0.1"
    assert seen["env"]["DB_PORT"] == "5432"
    assert seen["env"]["DB_USER"] == "validation_user"
    assert seen["env"]["DB_PASSWORD"] == "super-secret-password"
    assert seen["env"]["SECRET_KEY"] == "jwt-signing-key-for-tests"


def test_no_configured_file_means_no_credentials_not_ambient_ones(tmp_path, monkeypatch):
    """Fail-closed: with no file configured, validation must not silently
    pick up whatever the operator exported — otherwise the boundary is
    advisory and `run --continuous` from a shell that sourced `.env` would
    quietly validate against the production database."""
    monkeypatch.setenv("DB_NAME", "ambient_production_db")
    monkeypatch.setenv("DB_PASSWORD", "ambient-password")
    seen = {}

    def fake_run(argv, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    run_validation_commands([("ruff", "check", ".")], tmp_path, command_runner=fake_run)
    assert "DB_NAME" not in seen["env"]
    assert "DB_PASSWORD" not in seen["env"]


# ---- 3. API / provider keys cannot pass through -----------------------------


@pytest.mark.parametrize(
    "key", ["ANTHROPIC_API_KEY", "LLM_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY"]
)
def test_provider_keys_cannot_pass_through(tmp_path, repo, outside, key):
    """The file is an allowlist, not a general env loader — so it can never
    become the channel that hands a provider key to a subprocess."""
    env_file = write_env_file(outside / "validation.env", GOOD_BODY + f"{key}=sk-ant-secret\n")
    with pytest.raises(ConfigError) as exc:
        load(env_file, repo)
    assert key in str(exc.value)
    assert "sk-ant-secret" not in str(exc.value)


# ---- 4. unknown and duplicate keys fail closed ------------------------------


def test_unknown_key_is_refused(tmp_path, repo, outside):
    env_file = write_env_file(outside / "validation.env", GOOD_BODY + "DATABASE_URL=postgres://x\n")
    with pytest.raises(ConfigError, match="not in the validation allowlist"):
        load(env_file, repo)


def test_duplicate_key_is_refused(tmp_path, repo, outside):
    """Refused rather than last-one-wins: a duplicated DB_NAME is exactly how
    someone ends up validating against a database they did not intend."""
    env_file = write_env_file(outside / "validation.env", GOOD_BODY + "DB_NAME=other_db\n")
    with pytest.raises(ConfigError, match="duplicate key"):
        load(env_file, repo)


def test_malformed_line_is_refused(repo, outside):
    env_file = write_env_file(outside / "validation.env", GOOD_BODY + "this is not an assignment\n")
    with pytest.raises(ConfigError, match="malformed line"):
        load(env_file, repo)


def test_export_prefix_is_refused(repo, outside):
    """`export DB_HOST=...` is a shell script, not an env file — accepting it
    silently would mean the key is `export DB_HOST`, which never matches."""
    body = GOOD_BODY.replace("DB_HOST=127.0.0.1", "export DB_HOST=127.0.0.1")
    env_file = write_env_file(outside / "validation.env", body)
    with pytest.raises(ConfigError, match="malformed key"):
        load(env_file, repo)


@pytest.mark.parametrize("missing", VALIDATION_ENV_ALLOWLIST)
def test_missing_required_key_is_refused(repo, outside, missing):
    body = "\n".join(
        line for line in GOOD_BODY.splitlines() if not line.startswith(f"{missing}=")
    )
    env_file = write_env_file(outside / "validation.env", body + "\n")
    with pytest.raises(ConfigError, match=f"missing required key.*{missing}"):
        load(env_file, repo)


def test_empty_value_is_refused(repo, outside):
    body = GOOD_BODY.replace("DB_PASSWORD=super-secret-password", "DB_PASSWORD=")
    env_file = write_env_file(outside / "validation.env", body)
    with pytest.raises(ConfigError, match="empty value"):
        load(env_file, repo)


def test_short_secret_is_refused_so_redaction_cannot_be_defeated(repo, outside):
    """A three-character password would be too short to redact safely out of
    subprocess output; the hole is closed at load time instead of by a
    length threshold at redaction time."""
    body = GOOD_BODY.replace("DB_PASSWORD=super-secret-password", "DB_PASSWORD=abc")
    env_file = write_env_file(outside / "validation.env", body)
    with pytest.raises(ConfigError, match="shorter than the 8-character minimum"):
        load(env_file, repo)


def test_repo_declared_db_name_is_refused(repo, outside):
    """The one production marker this repository actually defines."""
    assert repo_declared_db_name(repo) == "german_vocabulary"
    body = GOOD_BODY.replace("DB_NAME=autoloop_validation_test", "DB_NAME=german_vocabulary")
    env_file = write_env_file(outside / "validation.env", body)
    with pytest.raises(ConfigError, match="declares"):
        load(env_file, repo)


def test_localhost_host_is_allowed(repo, outside):
    """The counterpart to the check above, pinned so nobody adds a host
    refusal later: a dedicated test database normally lives on localhost, so
    refusing that host would refuse the intended configuration."""
    body = GOOD_BODY.replace("DB_HOST=127.0.0.1", "DB_HOST=localhost")
    loaded = load(write_env_file(outside / "validation.env", body), repo)
    assert "DB_HOST" in loaded.keys()


# ---- 5. unsafe permissions and symlinks are refused -------------------------


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_group_or_world_readable_file_is_refused(repo, outside, mode):
    env_file = write_env_file(outside / "validation.env", mode=mode)
    with pytest.raises(ConfigError, match="group/world accessible"):
        load(env_file, repo)


def test_symlink_is_refused_before_it_is_followed(repo, outside, tmp_path):
    """A symlink named `validation.env` pointing at the real `.env` would
    otherwise sail through every content check by resolving to a file that
    happens to parse — the refusal must happen BEFORE resolution."""
    real = write_env_file(tmp_path / "real-validation.env")
    link = outside / "validation.env"
    link.symlink_to(real)
    with pytest.raises(ConfigError, match="symlink"):
        load(link, repo)


def test_relative_path_is_refused(repo):
    with pytest.raises(ConfigError, match="absolute path"):
        load(Path("validation.env"), repo)


def test_missing_file_is_refused(repo, outside):
    with pytest.raises(ConfigError, match="does not exist"):
        load(outside / "nope.env", repo)


def test_file_inside_the_checkout_is_refused(repo):
    inside = repo / "validation.env"
    write_env_file(inside)
    with pytest.raises(ConfigError, match="inside the primary checkout"):
        load(inside, repo)


def test_file_inside_the_state_dir_is_refused(repo):
    inside = repo / ".autoloop" / "validation.env"
    write_env_file(inside)
    with pytest.raises(ConfigError, match="inside"):
        load(inside, repo)


def test_file_inside_workers_root_is_refused(repo, tmp_path):
    workers = tmp_path / "workers"
    workers.mkdir()
    inside = write_env_file(workers / "validation.env")
    violations = validate_validation_env_path(
        inside, repo, repo / ".autoloop", workers_root=workers
    )
    assert any("worker root" in v for v in violations)


# ---- 6. error output redacts values -----------------------------------------


def test_failed_validation_summary_redacts_every_value(tmp_path, repo, outside):
    """`run_validation_commands`'s summary becomes `state.last_validation`,
    which reaches state.json, the transcript, blocker records AND the review
    packet sent to the reviewer — so a password echoed by a failing test is a
    prompt leak, not just a log leak."""
    loaded = load(write_env_file(outside / "validation.env"), repo)

    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr=(
                "asyncpg.exceptions.InvalidPasswordError: password authentication "
                "failed: user=validation_user db=autoloop_validation_test "
                "password=super-secret-password key=jwt-signing-key-for-tests "
                "host=127.0.0.1"
            ),
        )

    ok, summary = run_validation_commands(
        [("pytest", "-q")], tmp_path, command_runner=failing, validation_env=loaded
    )
    assert not ok
    assert "super-secret-password" not in summary
    assert "jwt-signing-key-for-tests" not in summary
    assert "validation_user" not in summary
    assert "autoloop_validation_test" not in summary
    assert "[redacted DB_PASSWORD]" in summary


def test_repr_and_describe_never_show_values(repo, outside):
    """A generated dataclass `__repr__` would print the mapping into the first
    pytest assertion diff or unhandled traceback that touched it."""
    loaded = load(write_env_file(outside / "validation.env"), repo)
    for text in (repr(loaded), str(loaded), str(loaded.describe())):
        assert "super-secret-password" not in text
        assert "jwt-signing-key-for-tests" not in text
    assert "DB_PASSWORD" in repr(loaded)  # names are fine


def test_parse_errors_never_quote_the_value(outside):
    with pytest.raises(ConfigError) as exc:
        parse_validation_env("PGPASSWORD=hunter2-the-secret\n", outside / "x.env")
    assert "hunter2-the-secret" not in str(exc.value)


# ---- 7. failed validation stays quarantined and consumes an attempt ---------


def test_failed_validation_reports_error_and_leaves_nothing_committed(tmp_path, repo, outside):
    """Credentials do not soften the failure path: a validation failure is
    still `status="error"` with the changed paths recorded, which is what
    drives the orchestrator's quarantine-and-consume-an-attempt behaviour
    (`_dispatch_task_postcommit` increments `attempt_count` BEFORE the
    executor runs, so this outcome cannot avoid consuming one)."""
    from autoloop.contract import Decision, Directive
    from autoloop.implement_executor import ImplementExecutor
    from autoloop.tasks import Task

    class FakeAgent:
        def run(self, spec):
            from autoloop.audit.agents import AgentResult

            return AgentResult(
                domain=spec.domain, raw_text="edited", returncode=0,
                duration_seconds=0.1, command=("claude",),
            )

    class FakeGit:
        repo_root = tmp_path

        def dirty_paths_all(self):
            return ["services/thing.py"]

    loaded = load(write_env_file(outside / "validation.env"), repo)
    calls = []

    def failing(argv, **kwargs):
        calls.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="1 failed")

    executor = ImplementExecutor(
        git=FakeGit(),
        agent_runner=FakeAgent(),
        validation_commands=(("pytest", "-q"),),
        command_runner=failing,
        validation_env=loaded,
    )
    outcome = executor.execute(
        Directive(decision=Decision.IMPLEMENT, reason="do it", task_id="rt-01"),
        Task(id="rt-01", title="t", description="d"),
    )
    assert outcome.status == "error"
    assert "validation failed" in outcome.summary
    assert outcome.changed_paths == ("services/thing.py",)
    # the credentials really did reach the validation subprocess...
    assert calls and calls[0]["DB_NAME"] == "autoloop_validation_test"


# ---- 8. a real DB-backed validation command succeeds with test credentials --


def _postgres_startup_probe(port: int) -> str:
    """A `python3 -c` program that connects to Postgres using ONLY the mapped
    environment. Used against a loopback listener, so it proves credential
    DELIVERY into a real driver without needing a server."""
    return (
        "import os, asyncpg, asyncio\n"
        "async def main():\n"
        "    try:\n"
        "        await asyncio.wait_for(asyncpg.connect(\n"
        "            host=os.environ['DB_HOST'], port=int(os.environ['DB_PORT']),\n"
        "            database=os.environ['DB_NAME'], user=os.environ['DB_USER'],\n"
        "            password=os.environ['DB_PASSWORD']), timeout=5)\n"
        "    except Exception:\n"
        "        pass\n"
        "asyncio.run(main())\n"
    )


def test_validation_subprocess_delivers_credentials_to_a_real_db_client(tmp_path, repo, outside):
    """End-to-end without a database: a REAL `python3` subprocess, launched
    through `run_validation_commands`, uses asyncpg to dial a listener this
    test owns — and the Postgres startup packet on the wire carries exactly
    the user and database from the file. Proves the values survive the whole
    path (file → ValidationEnv.apply → subprocess env → driver), which a
    mocked runner cannot show."""
    pytest.importorskip("asyncpg")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    received: list[bytes] = []

    #: asyncpg opens with an SSLRequest (8 bytes, body 80877103) BEFORE it
    #: sends the startup packet carrying user/database. A listener that just
    #: reads once sees only that handshake probe, so it must decline TLS with
    #: a single 'N' and read again — verified empirically, the first read is
    #: b"\x00\x00\x00\x08\x04\xd2\x16/".
    _SSL_REQUEST = b"\x00\x00\x00\x08\x04\xd2\x16\x2f"

    def accept_once():
        try:
            conn, _ = listener.accept()
            conn.settimeout(5)
            first = conn.recv(4096)
            if first == _SSL_REQUEST:
                conn.sendall(b"N")
                first = conn.recv(4096)
            received.append(first)
            conn.close()
        except OSError:  # pragma: no cover - listener closed early
            pass

    thread = threading.Thread(target=accept_once, daemon=True)
    thread.start()

    body = GOOD_BODY.replace("DB_PORT=5432", f"DB_PORT={port}")
    loaded = load(write_env_file(outside / "validation.env", body), repo)
    run_validation_commands(
        [(sys.executable, "-c", _postgres_startup_probe(port))],
        tmp_path,
        validation_env=loaded,
        timeout=30,
    )
    thread.join(timeout=10)
    listener.close()

    assert received, "the validation subprocess never dialled the listener"
    packet = received[0]
    assert b"validation_user" in packet
    assert b"autoloop_validation_test" in packet


def test_real_db_validation_command_succeeds(tmp_path, repo):
    """The live half: run a real DB-backed command against a real dedicated
    test database. Skipped unless the operator supplies one — this repository
    has no test database and its credentials cannot be invented here.

    To run it for real:
        printf 'DB_HOST=127.0.0.1\\nDB_PORT=5432\\nDB_NAME=<test-db>\\n'\\
               'DB_USER=<user>\\nDB_PASSWORD=<pw>\\nSECRET_KEY=<key>\\n' \\
            > ~/.autoloop/validation.env && chmod 600 ~/.autoloop/validation.env
        AUTOLOOP_TEST_VALIDATION_ENV_FILE=~/.autoloop/validation.env \\
            python3 -m pytest autoloop/tests/test_validation_env.py -k real_db
    """
    configured = os.environ.get("AUTOLOOP_TEST_VALIDATION_ENV_FILE", "")
    if not configured:
        pytest.skip(
            "no AUTOLOOP_TEST_VALIDATION_ENV_FILE — set it to a validation env "
            "file for a DEDICATED test database to exercise a live connection"
        )
    pytest.importorskip("asyncpg")
    loaded = load_validation_env(
        Path(configured).expanduser(),
        repo_root=repo,
        state_dir=repo / ".autoloop",
    )
    probe = (
        "import os, asyncpg, asyncio\n"
        "async def main():\n"
        "    conn = await asyncpg.connect(\n"
        "        host=os.environ['DB_HOST'], port=int(os.environ['DB_PORT']),\n"
        "        database=os.environ['DB_NAME'], user=os.environ['DB_USER'],\n"
        "        password=os.environ['DB_PASSWORD'])\n"
        "    assert await conn.fetchval('SELECT 1') == 1\n"
        "    await conn.close()\n"
        "asyncio.run(main())\n"
    )
    ok, summary = run_validation_commands(
        [(sys.executable, "-c", probe)], tmp_path, validation_env=loaded, timeout=60
    )
    assert ok, summary


# ---- config plumbing ---------------------------------------------------------

_CONFIG_URL = "https://chatgpt.com/c/abc123"


def _config_text(validation_line: str) -> str:
    return (
        f'[browser]\nconversation_url = "{_CONFIG_URL}"\n\n'
        f'[paths]\nworkers_root = "/tmp/al-workers"\n{validation_line}'
    )


def test_relative_validation_env_file_refused_by_load_config(tmp_path):
    from autoloop.config import load_config

    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_text('validation_env_file = "relative/validation.env"\n'))
    with pytest.raises(ConfigError, match="absolute"):
        load_config(cfg)


def test_absolute_validation_env_file_loads(tmp_path, outside):
    from autoloop.config import load_config

    env_file = write_env_file(outside / "validation.env")
    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_text(f'validation_env_file = "{env_file}"\n'))
    assert load_config(cfg).validation_env_file == env_file


def test_unset_validation_env_file_is_none(tmp_path):
    from autoloop.config import load_config

    cfg = tmp_path / "config.toml"
    cfg.write_text(_config_text(""))
    assert load_config(cfg).validation_env_file is None


# ---- the strip helper itself -------------------------------------------------


def test_strip_validation_vars_removes_names_and_path_vars():
    base = {
        "PATH": "/usr/bin",
        "DB_PASSWORD": "x",
        "SECRET_KEY": "y",
        "AUTOLOOP_VALIDATION_ENV_FILE": "/p",
        "KEEP_ME": "z",
    }
    out = strip_validation_vars(base)
    assert out == {"PATH": "/usr/bin", "KEEP_ME": "z"}
