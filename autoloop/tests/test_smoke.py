"""`smoke-browser` is RETIRED (brw-16, 2026-08-25), and this file pins that it
refuses PLAINLY — which is a stronger claim than "it exits non-zero".

The command existed to prove the BROWSER transport before a real run needed it:
it defaulted to `browser_chatgpt`, archived the previous smoke state under
`.autoloop/smoke/`, took the loop lock and drove one real round-trip to a
PASS/FAIL verdict. No browser-backed provider is registered any more, so the
seat it smoked does not exist.

Two outcomes had to be avoided, and each is a test below rather than a comment:

* a command that silently cannot work — so the refusal SAYS SO, in the exit
  code and in words naming the transport that went away;
* the command quietly repurposed into a generic smoke of whatever
  `conversation.provider` names — which is what the previous candidate for this
  task shipped. That is a different command wearing this one's name: it reports
  PASS about a transport nobody asked it for, and every runbook that types
  `smoke-browser` then mis-describes what it does. So the tests here assert the
  ABSENCES: no provider is constructed, no orchestrator is built, the loop lock
  is never taken, and nothing under the state dir is created, read or moved.

Note what is NOT imported: this module no longer touches `autoloop.browser` at
all (it used to import `SubmitResult` for its fake conversation), so the test
tree has one fewer edge into the package this task disconnected.
"""

import json

import pytest

from autoloop import cli

RETIRED_ARGV = [
    ["smoke-browser"],
    ["smoke-browser", "--provider", "browser_chatgpt"],
    ["smoke-browser", "--provider", "codex_cli"],
    ["smoke-browser", "--provider", ""],
]


def write_config(tmp_path, provider="codex_cli"):
    """A VALID config, deliberately. The refusal must not depend on the config
    being broken — it must not depend on the config at all."""
    config = tmp_path / "config.toml"
    config.write_text(
        "[conversation]\n"
        f'provider = "{provider}"\n'
        "[paths]\n"
        f'state_dir = "{tmp_path / ".al"}"\n'
        f'workers_root = "{tmp_path / "workers_root"}"\n',
        encoding="utf-8",
    )
    return config


def test_smoke_browser_refuses_and_says_why(tmp_path, capsys):
    config = write_config(tmp_path)

    assert cli.main(["smoke-browser", "--config", str(config)]) == 2

    out = capsys.readouterr().out
    assert "RETIRED" in out
    assert "browser" in out.lower(), "names the transport that went away"
    assert "doctor" in out, "points at the preflight command that still works"
    # Never the words a working smoke printed: an operator grepping their logs
    # for PASS/FAIL must not find either.
    assert "PASS" not in out
    assert "FAIL" not in out


@pytest.mark.parametrize(
    "argv",
    RETIRED_ARGV,
    ids=["bare", "retired-provider", "registered-provider", "empty-provider"],
)
def test_every_invocation_shape_gets_the_same_refusal(tmp_path, capsys, argv):
    """`--provider` is accepted and inert. It survives ONLY so the invocation a
    runbook or a shell history holds — `smoke-browser --provider
    browser_chatgpt` — reaches this sentence instead of argparse's
    "unrecognized arguments", which exits with the same code and none of the
    explanation. So every shape must land on the identical message; a provider
    name must not select a different path, least of all a working one."""
    config = write_config(tmp_path)

    assert cli.main([*argv, "--config", str(config)]) == 2
    assert capsys.readouterr().out.strip() == cli.SMOKE_BROWSER_RETIRED


def test_it_never_exits_zero(tmp_path, capsys):
    """The fail-open case: a retired command that returned 0 would be read by a
    wrapper script, a cron job or the self-upgrade runbook (`doctor` +
    `smoke-browser`, both green → promote the new version) as evidence the
    transport was proven. Nothing was proven, so nothing may look like it."""
    for argv in RETIRED_ARGV:
        assert cli.main([*argv, "--config", str(write_config(tmp_path))]) != 0
    capsys.readouterr()


# ---- the absences ------------------------------------------------------------


def test_no_provider_and_no_orchestrator_are_constructed(tmp_path, monkeypatch, capsys):
    """The heart of "refuses plainly". Both boom-patches are on `cli`'s own
    names, which is where the retired body reached them from."""

    def boom(*args, **kwargs):
        raise AssertionError("a retired command must construct nothing")

    monkeypatch.setattr(cli, "create_conversation", boom)
    monkeypatch.setattr(cli, "Orchestrator", boom)
    monkeypatch.setattr(cli, "PolicyEngine", boom)
    monkeypatch.setattr(cli, "GitGateway", boom)

    assert cli.main(["smoke-browser", "--config", str(write_config(tmp_path))]) == 2
    assert "RETIRED" in capsys.readouterr().out


def test_the_loop_lock_is_never_taken(tmp_path, monkeypatch, capsys):
    """Asserted directly, because the cost of getting this wrong is paid by a
    RUNNING loop: `smoke-browser` used to hold `.autoloop/LOCK` for the length
    of a live round-trip, so a retired command that still took it would block a
    real run in order to print a sentence."""

    def boom(*args, **kwargs):
        raise AssertionError("a retired command must not take the loop lock")

    monkeypatch.setattr(cli, "LoopLock", boom)

    assert cli.main(["smoke-browser", "--config", str(write_config(tmp_path))]) == 2
    assert "RETIRED" in capsys.readouterr().out


def test_a_live_foreign_lock_does_not_change_the_answer(tmp_path, capsys):
    """The same claim without the monkeypatch, through the real `LoopLock`.

    A lock owned by another HOST is live by definition (`LoopLock.is_live` fails
    closed for a pid it cannot verify), so any attempt to acquire it here would
    raise `LockHeldError` and `cli.main` would print `error: another autoloop
    process holds …` and return 1. Seeing the ordinary refusal and exit 2
    instead is proof no acquisition was attempted."""
    config = write_config(tmp_path)
    lock = tmp_path / ".al" / "LOCK"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(
        json.dumps(
            {
                "pid": 4242,
                "hostname": "some-other-machine",
                "started_at": "2026-08-25T00:00:00+00:00",
                "run_id": "r",
                "state_dir": str(tmp_path / ".al"),
            }
        ),
        encoding="utf-8",
    )

    assert cli.main(["smoke-browser", "--config", str(config)]) == 2

    captured = capsys.readouterr()
    assert "RETIRED" in captured.out
    assert "another autoloop process holds" not in captured.err


def test_the_smoke_state_is_neither_archived_nor_written(tmp_path, capsys):
    """`.autoloop/smoke/` is left exactly as found. The old command archived it
    on every run (`store.archive()`), so a retired one that still did would
    destroy the last real smoke session's evidence while doing nothing."""
    config = write_config(tmp_path)
    smoke_state = tmp_path / ".al" / "smoke" / "state.json"
    smoke_state.parent.mkdir(parents=True)
    smoke_state.write_text('{"session_id": "previous"}', encoding="utf-8")
    before = sorted(p.name for p in smoke_state.parent.iterdir())

    assert cli.main(["smoke-browser", "--config", str(config)]) == 2
    capsys.readouterr()

    assert json.loads(smoke_state.read_text())["session_id"] == "previous"
    assert sorted(p.name for p in smoke_state.parent.iterdir()) == before
    assert not list(smoke_state.parent.glob("state.json.bak-*"))


def test_nothing_at_all_appears_under_the_state_dir(tmp_path, capsys):
    """Wider than the previous test and cheaper to keep honest: no transcript
    line, no diagnostics directory, no state file, no lock. The state dir does
    not even have to exist."""
    config = write_config(tmp_path)
    state_dir = tmp_path / ".al"

    assert cli.main(["smoke-browser", "--config", str(config)]) == 2
    capsys.readouterr()

    assert not state_dir.exists()


# ---- it does not read the config either --------------------------------------


def test_a_missing_config_still_gets_the_refusal(tmp_path, capsys):
    """No config is loaded, so a `--config` that points at nothing cannot turn
    the refusal into a `ConfigError` about a file the command had no reason to
    open. `cli.main` maps `AutoloopError` to exit 1, so 2-with-the-message is
    the observable difference."""
    missing = tmp_path / "nope" / "config.toml"

    assert cli.main(["smoke-browser", "--config", str(missing)]) == 2
    assert capsys.readouterr().out.strip() == cli.SMOKE_BROWSER_RETIRED


def test_a_malformed_config_still_gets_the_refusal(tmp_path, capsys):
    """The other unreadable shape: valid path, invalid TOML."""
    broken = tmp_path / "config.toml"
    broken.write_text("[conversation\nprovider = ", encoding="utf-8")

    assert cli.main(["smoke-browser", "--config", str(broken)]) == 2
    assert capsys.readouterr().out.strip() == cli.SMOKE_BROWSER_RETIRED


def test_a_config_naming_the_retired_browser_provider_still_gets_the_refusal(
    tmp_path, capsys
):
    """`conversation.provider = "browser_chatgpt"` is the state an operator
    upgrading mid-flight is in. It must reach the same sentence — not a
    provider-registration error, and certainly not a run."""
    config = write_config(tmp_path, provider="browser_chatgpt")

    assert cli.main(["smoke-browser", "--config", str(config)]) == 2
    assert capsys.readouterr().out.strip() == cli.SMOKE_BROWSER_RETIRED


# ---- the command is still REGISTERED, not deleted ----------------------------


def test_the_subcommand_still_parses():
    """Deleting the subparser would make the same invocation exit 2 as well —
    from argparse, on stderr, saying "invalid choice". Keeping it registered is
    what turns that into an explanation, so the registration itself is pinned."""
    args = cli.build_parser().parse_args(["smoke-browser"])

    assert args.func is cli._cmd_smoke_browser
    assert args.provider == ""


def test_the_help_text_says_it_is_retired():
    """An operator reading `--help` must not have to run it to find out."""
    help_text = cli.build_parser().format_help()

    assert "smoke-browser" in help_text
    assert "RETIRED" in help_text
