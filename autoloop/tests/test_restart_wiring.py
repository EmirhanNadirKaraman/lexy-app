"""What `browser.restart_command` points at, and what happens when it still
points at the shell script retired on 2026-08-16.

The wiring is the part no other test covers. `test_chrome_restart.py` proves the
module restarts the right browser; nothing there says the loop ever calls it —
that lives in a TOML template and in the operator's own `.autoloop/config.toml`,
which is not in this repository at all. So the two failures this file exists to
catch are a template naming a module that does not exist, and a live config
still naming the script, which would otherwise surface as bash's exit 127 in the
middle of the browser fault a restart exists to clear.

Nothing here touches the machine: no process is listed, signalled or launched,
and `no_machine_access` makes that structural rather than a promise.
"""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from autoloop.config import (
    RESTART_COMMAND_REPLACEMENT,
    RETIRED_RESTART_SCRIPT,
    load_config,
)
from autoloop.errors import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "autoloop" / "config.example.toml"
RETIRED_SCRIPT = REPO_ROOT / "scripts" / RETIRED_RESTART_SCRIPT
RESTART_MODULE = REPO_ROOT / "autoloop" / "browser" / "chrome_restart.py"


@pytest.fixture(autouse=True)
def no_machine_access(monkeypatch):
    """Every route to a real browser, disabled for this whole file.

    A restart command's job is to signal and launch processes, so a test file
    about restart commands is exactly where an accidental live invocation would
    hide — and on a developer's machine the thing it would end is their own
    Chrome. Reading config and text needs none of these.
    """

    def refuse(*_args, **_kwargs):
        raise AssertionError("this file must never touch a real process")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "kill", refuse)


def _write_config(tmp_path: Path, restart_command: str) -> Path:
    """A minimal config carrying one `restart_command`, as TOML source."""
    workers_root = tmp_path / "workers"
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[browser]\n"
        'conversation_url = "https://chatgpt.com/c/restart-wiring"\n'
        f"restart_command = {restart_command}\n"
        "\n"
        "[paths]\n"
        f'workers_root = "{workers_root}"\n',
        encoding="utf-8",
    )
    return cfg


# --- the template an operator copies -----------------------------------------


def test_the_shipped_template_restarts_via_the_module():
    """Parsed by the real loader, not `tomllib`: the retired-script refusal
    lives in `load_config`, so a template that tripped it would ship a file
    nobody can copy."""
    config = load_config(EXAMPLE_CONFIG)
    assert config.browser.restart_command == RESTART_COMMAND_REPLACEMENT
    assert config.browser.restart_command[1:] == ("-m", "autoloop.browser.chrome_restart")


def test_the_module_the_template_names_exists():
    """The string compare above passes just as well for a typo'd dotted path —
    `python3 -m` would then exit 1 with `No module named ...` at the one moment
    the loop is already recovering from a fault."""
    module = ".".join(RESTART_COMMAND_REPLACEMENT[2:])
    assert importlib.util.find_spec(module) is not None, f"{module} is not importable"


def test_the_named_module_actually_runs_something_under_dash_m():
    """`python3 -m pkg.mod` on a module with no `__main__` guard executes the
    body, prints nothing and exits **0** — a restart command that reports
    success while restarting nothing, which is the precise bug this whole path
    was rewritten to end (docs/COMMON_ERRORS.md). Asserted on the source,
    because importing the module cannot tell the two apart."""
    body = RESTART_MODULE.read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in body
    assert "sys.exit(main())" in body, "an exit code the callers can read"


def test_the_template_never_advertises_the_retired_script():
    """Including in its comments: a pointer to a dead path is worth as little
    as a setting naming one."""
    assert RETIRED_RESTART_SCRIPT not in EXAMPLE_CONFIG.read_text(encoding="utf-8")


# --- a config that still names the retired script ----------------------------


@pytest.mark.parametrize(
    "restart_command",
    [
        '["bash", "scripts/restart_autoloop_chrome.sh"]',  # what the template shipped
        '["scripts/restart_autoloop_chrome.sh"]',
        '["./scripts/restart_autoloop_chrome.sh"]',
        '["/Users/op/dev/lexy/scripts/restart_autoloop_chrome.sh"]',
        '["bash", "-x", "scripts/restart_autoloop_chrome.sh"]',
    ],
)
def test_a_config_still_naming_the_retired_script_is_refused(tmp_path, restart_command):
    """Every invocation form, because the rule is a substring of any token and
    not a guess at the argv shape — an operator who wrote `bash -x` or an
    absolute path must get the same answer as one who copied the template."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write_config(tmp_path, restart_command))
    assert RETIRED_RESTART_SCRIPT in str(excinfo.value)


def test_the_refusal_carries_the_line_to_paste(tmp_path):
    """`cli.main` prints `error: <exc>` and nothing else, so this message is
    plausibly all the operator sees. A refusal that only says the old thing is
    gone leaves them to search for what replaced it — while their loop is
    down."""
    with pytest.raises(ConfigError) as excinfo:
        load_config(_write_config(tmp_path, '["bash", "scripts/restart_autoloop_chrome.sh"]'))
    message = str(excinfo.value)
    assert 'restart_command = ["python3", "-m", "autoloop.browser.chrome_restart"]' in message
    assert "RETIRED" in message
    # The failure it replaces: bash exiting 127 through `result.stderr`, which
    # names a path and nothing else.
    assert "No such file or directory" not in message


def test_the_module_invocation_itself_is_accepted(tmp_path):
    """The refusal is keyed to the retired name, not to restart commands in
    general — the mutation that refuses everything must fail somewhere."""
    config = load_config(_write_config(tmp_path, '["python3", "-m", "autoloop.browser.chrome_restart"]'))
    assert config.browser.restart_command == RESTART_COMMAND_REPLACEMENT


def test_an_unrelated_restart_command_is_left_alone(tmp_path):
    """An operator's own wrapper is still their business."""
    config = load_config(_write_config(tmp_path, '["bash", "scripts/my_own_restart.sh"]'))
    assert config.browser.restart_command == ("bash", "scripts/my_own_restart.sh")


# --- the retired script itself -----------------------------------------------


def test_the_retired_script_is_not_a_working_restart_path():
    """Correct in both worlds, because the file leaves in two steps: it is a
    failing tombstone while any live config might still invoke it, and `git rm`
    once none does. What must hold either way is that it never again exits 0
    having restarted nothing."""
    if not RETIRED_SCRIPT.exists():
        return  # removed outright — the end state
    body = RETIRED_SCRIPT.read_text(encoding="utf-8")
    assert "RETIRED" in body, "the file still exists, so it must say it is dead"
    assert "autoloop.browser.chrome_restart" in body, "it must name the replacement"
    # Asserted on statements rather than substrings: the prose here explains an
    # exit code and a `kill`, and a doc edit must not fail this test.
    statements = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert "exit 1" in statements, "a zero exit reads to both callers as a real restart"
    assert "exit 0" not in statements
    assert not [line for line in statements if line.startswith(("kill", "open ", "pkill"))], (
        "it must not act on any browser"
    )


# --- the guard above is real -------------------------------------------------


def test_no_test_here_can_reach_a_browser():
    """`no_machine_access` is autouse, so a future test that shells out to a
    restart command fails loudly instead of restarting the developer's Chrome.
    Pinned so the fixture cannot rot into a no-op."""
    for call in (
        lambda: subprocess.run(["true"]),
        lambda: subprocess.Popen(["true"]),
        lambda: os.kill(os.getpid(), 0),
    ):
        with pytest.raises(AssertionError, match="never touch a real process"):
            call()
