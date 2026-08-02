"""The pause flag must live outside the snapshotted tree.

`escape_detector` enumerates ignored paths on purpose — `.autoloop/` is
gitignored in production, so an agent forging state there is exactly what it
exists to catch. The pause flag used to live at `state_dir / "PAUSE"`, inside
that tree, so running the documented `pause` command while a task was
dispatched created a file the detector reported as an escape and parked the
loop `loop_fatal`. The supported way to stop the loop broke it (2026-08-02).
"""

import argparse

import pytest

from autoloop import cli
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig

URL = "https://chatgpt.com/c/pause-location"


@pytest.fixture
def config(tmp_path):
    """`workers_root` a SIBLING of the checkout, as production requires."""
    checkout = tmp_path / "repo"
    checkout.mkdir()
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=checkout / ".autoloop",
        workers_root=tmp_path / "workers",
    )


def _args(config=None):
    return argparse.Namespace(config=None)


# --- the property the whole fix rests on --------------------------------------


def test_the_pause_flag_lives_outside_the_checkout(config):
    checkout = config.state_dir.parent
    assert checkout not in config.pause_file.parents, (
        f"{config.pause_file} is inside the checkout {checkout} — the escape "
        "detector snapshots that tree and will report the flag as an escape"
    )
    assert config.state_dir not in config.pause_file.parents


def test_it_sits_beside_the_workers_root_like_the_inbox(config):
    """Same placement, same reason: that path is already guaranteed absolute
    and outside the checkout by `worker_env.validate_workers_root`."""
    from autoloop.inbox import inbox_dir_for

    assert config.pause_file.parent == config.workers_root.parent
    assert config.pause_file.parent == inbox_dir_for(config.workers_root, config.state_dir).parent


def test_pause_writes_only_to_the_new_location(config, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda _p: config)

    assert cli._cmd_pause(_args()) == 0

    assert config.pause_file.exists()
    assert not config.legacy_pause_file.exists(), "must not write inside the checkout"
    assert cli.pause_requested(config)


# --- a flag from an older build must not be silently ignored ------------------


def test_a_legacy_flag_still_pauses(config):
    """The operator asked for a pause. Reading only the new path would leave
    them with a loop that keeps running — the exact failure a pause prevents."""
    config.legacy_pause_file.parent.mkdir(parents=True, exist_ok=True)
    config.legacy_pause_file.touch()

    assert cli.pause_requested(config)


def test_resume_clears_both_locations(config, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda _p: config)
    config.pause_file.parent.mkdir(parents=True, exist_ok=True)
    config.pause_file.touch()
    config.legacy_pause_file.parent.mkdir(parents=True, exist_ok=True)
    config.legacy_pause_file.touch()

    assert cli.clear_pause(config) is True

    assert not config.pause_file.exists()
    assert not config.legacy_pause_file.exists()
    assert not cli.pause_requested(config)


def test_clearing_nothing_reports_nothing(config):
    assert cli.clear_pause(config) is False


# --- the interaction that caused the bug --------------------------------------


def test_the_pause_flag_is_not_a_path_the_escape_detector_watches(tmp_path):
    """The regression, driven through the REAL detector rather than by
    asserting a path shape: create the flag between the two snapshots and
    prove nothing is reported. With the flag inside `state_dir` this is the
    exact diff that parked the loop."""
    import subprocess

    from autoloop.escape_detector import (
        diff_snapshots,
        enumerate_checkout_paths,
        snapshot_checkout,
    )
    from autoloop.git_gateway import GitGateway
    from autoloop.policy import PolicyEngine

    checkout = tmp_path / "repo"
    checkout.mkdir()
    for argv in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "T"),
        ("config", "commit.gpgsign", "false"),
    ):
        subprocess.run(["git", *argv], cwd=checkout, check=True, capture_output=True)
    (checkout / "README.md").write_text("hi\n", encoding="utf-8")
    # `.autoloop/` is gitignored in production — that is why the detector's
    # ignored-path enumeration reaches it at all.
    (checkout / ".gitignore").write_text(".autoloop/\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=checkout, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "base"], cwd=checkout, check=True, capture_output=True
    )

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=checkout / ".autoloop",
        workers_root=tmp_path / "workers",
    )
    git = GitGateway(checkout, PolicyEngine(PolicyConfig()))

    paths = enumerate_checkout_paths(git)
    before = snapshot_checkout(checkout, paths)

    config.pause_file.parent.mkdir(parents=True, exist_ok=True)
    config.pause_file.touch()

    after = snapshot_checkout(checkout, enumerate_checkout_paths(git))
    assert diff_snapshots(before, after) == [], "pausing must not look like an escape"

    # And the control: the OLD location does trip it, which is the bug.
    config.legacy_pause_file.parent.mkdir(parents=True, exist_ok=True)
    config.legacy_pause_file.touch()
    after_legacy = snapshot_checkout(checkout, enumerate_checkout_paths(git))
    assert diff_snapshots(before, after_legacy), (
        "the old in-checkout location should still be detectable — if this "
        "passes, the test proves nothing about the move"
    )
