"""What `browser.restart_command` points at, and what an unmigrated config still
gets when it points at the shell script retired on 2026-08-16.

The wiring is the part no other test covers. `test_chrome_restart.py` proves the
module restarts the right browser; nothing there says the loop ever calls it —
that lives in a TOML template and in the operator's own `.autoloop/config.toml`,
which is not in this repository at all. So this file pins the two ends of the
transition:

* the shipped template names the module, and that module exists and runs;
* a live config still naming the script keeps LOADING, unchanged, because the
  loader deliberately does not refuse it — refusing would break `status`,
  `doctor`, `run` and the recovery commands on every deployment that had not yet
  hand-edited its config, i.e. take away the tooling needed to fix it. What the
  operator gets instead is the tombstone: a restart that fails non-zero carrying
  the exact line to paste, rather than bash's exit 127 naming a missing file.

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

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "autoloop" / "config.example.toml"
RETIRED_SCRIPT = REPO_ROOT / "scripts" / RETIRED_RESTART_SCRIPT
RESTART_MODULE = REPO_ROOT / "autoloop" / "browser" / "chrome_restart.py"

#: The config line an operator has to end up with, built from the constant so a
#: change to the replacement command cannot leave the tombstone advertising the
#: old one.
PASTE_LINE = "restart_command = [" + ", ".join(f'"{t}"' for t in RESTART_COMMAND_REPLACEMENT) + "]"


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


def _tombstone_message() -> str:
    """What the retired script actually prints on stderr — the heredoc body,
    without the comments around it."""
    body = RETIRED_SCRIPT.read_text(encoding="utf-8")
    _, _, rest = body.partition("<<'MSG'\n")
    assert rest, "the tombstone no longer prints a heredoc message"
    message, _, _ = rest.partition("\nMSG\n")
    return message


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
    """Parsed by the real loader rather than bare `tomllib`, so the assertion
    covers the template as something copyable — every other rule in
    `load_config` applies to it too."""
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
    "restart_command, expected",
    [
        # What the template used to ship.
        ('["bash", "scripts/restart_autoloop_chrome.sh"]', ("bash", "scripts/restart_autoloop_chrome.sh")),
        ('["scripts/restart_autoloop_chrome.sh"]', ("scripts/restart_autoloop_chrome.sh",)),
        ('["./scripts/restart_autoloop_chrome.sh"]', ("./scripts/restart_autoloop_chrome.sh",)),
        (
            '["/Users/op/dev/lexy/scripts/restart_autoloop_chrome.sh"]',
            ("/Users/op/dev/lexy/scripts/restart_autoloop_chrome.sh",),
        ),
        (
            '["bash", "-x", "scripts/restart_autoloop_chrome.sh"]',
            ("bash", "-x", "scripts/restart_autoloop_chrome.sh"),
        ),
    ],
)
def test_a_config_still_naming_the_retired_script_still_loads(tmp_path, restart_command, expected):
    """The transition rule, and the mutation test against re-adding a refusal.

    The live `.autoloop/config.toml` is not in this repository, so on the day
    this change merges every unmigrated deployment is still holding one of these
    lines. Refusing them in `load_config` would fail `status`, `doctor`, `run`
    and the recovery commands alike — every tool the operator would reach for —
    over a setting only a browser restart reads. So the value LOADS, and loads
    exactly as written: not refused, not rewritten to the module, since the
    loader inventing a command that starts a browser is not a repair either.
    Every invocation form, because "we handle the template's spelling" is not
    the same claim as "we leave restart commands alone".
    """
    config = load_config(_write_config(tmp_path, restart_command))
    assert config.browser.restart_command == expected


def test_the_module_invocation_itself_is_accepted(tmp_path):
    """The complement of the above: the value the operator migrates TO also
    round-trips, so `load_config` is shown to pass restart commands through
    rather than merely tolerate the old one."""
    config = load_config(_write_config(tmp_path, '["python3", "-m", "autoloop.browser.chrome_restart"]'))
    assert config.browser.restart_command == RESTART_COMMAND_REPLACEMENT


def test_an_unrelated_restart_command_is_left_alone(tmp_path):
    """An operator's own wrapper is still their business."""
    config = load_config(_write_config(tmp_path, '["bash", "scripts/my_own_restart.sh"]'))
    assert config.browser.restart_command == ("bash", "scripts/my_own_restart.sh")


# --- the retired script itself -----------------------------------------------


def test_the_retired_script_is_still_on_disk():
    """Load-bearing, not leftover. Since a config naming it still loads, this
    file is what an unmigrated deployment actually launches after a browser
    fault. Delete it and that deployment gets bash's exit 127 — `restart
    FAILED: … No such file or directory` — during the fault the restart exists
    to clear, saying nothing about what to write instead. It goes (`git rm`) in
    a later cleanup, once live configs have been migrated."""
    assert RETIRED_SCRIPT.exists(), (
        "the tombstone is the compatibility path for configs that still name it"
    )


def test_the_retired_script_is_not_a_working_restart_path():
    """What must never come back: a zero exit, or any action on a browser.
    Asserted on statements rather than substrings — the prose here explains an
    exit code and a `kill`, and a doc edit must not fail this test."""
    body = RETIRED_SCRIPT.read_text(encoding="utf-8")
    assert "RETIRED" in body, "it must say it is dead"
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


def test_the_retired_script_carries_the_line_to_paste():
    """This is where the "must not be a bare file-not-found" requirement lives
    now that the loader refuses nothing. Both callers surface `result.stderr`
    only on a non-zero exit, so this message is plausibly all the operator sees
    of the fault — it has to carry the fix, not just the news.

    Asserted on the heredoc the script prints, not on the file: the comments
    above it discuss bash's exit 127 by name, and a test that could not tell an
    explanation from the message would pass on a script that only explains."""
    message = _tombstone_message()
    assert "autoloop.browser.chrome_restart" in message, "it must name the replacement"
    assert PASTE_LINE in message, "the literal config line, ready to paste"
    assert "No such file or directory" not in message


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
