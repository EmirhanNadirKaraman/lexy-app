"""The loop publishes; a monitor outside the checkout judges.

`health.check` answers the same question better, but it reads the state dir,
the blocker store and the transcript — all inside `~/Documents` here, which
macOS TCC puts out of reach of a launchd agent (`Operation not permitted`,
exit 126). The heartbeat exists so a monitor can judge without touching a
protected path, and therefore without a Full Disk Access grant.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autoloop import heartbeat
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.state import LoopState

URL = "https://chatgpt.com/c/heartbeat"
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
CHECKER = Path(__file__).resolve().parents[2] / "scripts" / "check_heartbeat.py"


@pytest.fixture
def config(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(),
        state_dir=checkout / ".autoloop",
        workers_root=tmp_path / "workers",
    )


# --- where it lives -----------------------------------------------------------


def test_the_heartbeat_is_written_outside_the_checkout(config):
    """Both reasons are load-bearing: the escape detector snapshots the
    checkout mid-round, and TCC blocks a launchd agent from reading it."""
    checkout = config.state_dir.parent

    assert checkout not in config.heartbeat_file.parents
    assert config.heartbeat_file.parent == config.workers_root.parent


def test_writing_it_is_invisible_to_the_escape_detector(tmp_path):
    """Driven through the REAL detector: a heartbeat published mid-round must
    not look like an agent escaping its worker repo."""
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
    (checkout / ".gitignore").write_text(".autoloop/\n", encoding="utf-8")
    (checkout / "README.md").write_text("hi\n", encoding="utf-8")
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
    before = snapshot_checkout(checkout, enumerate_checkout_paths(git))

    heartbeat.publish(config, LoopState(session_id="s", conversation_url=URL))

    after = snapshot_checkout(checkout, enumerate_checkout_paths(git))
    assert diff_snapshots(before, after) == []


# --- what it carries ----------------------------------------------------------


def test_it_records_what_the_loop_knows(config):
    state = LoopState(session_id="sess-1", conversation_url=URL, phase="executing")
    heartbeat.publish(config, state)

    beat = json.loads(config.heartbeat_file.read_text(encoding="utf-8"))
    assert beat["status"] == "running"
    assert beat["phase"] == "executing"
    assert beat["session_id"] == "sess-1"
    assert beat["needs_attention"] is False
    assert beat["pid"] > 0
    datetime.fromisoformat(beat["ts"])  # parseable


def test_open_blockers_turn_a_running_beat_into_an_alarm(config):
    from autoloop.blockers import BlockerStore

    BlockerStore(config.blockers_dir).record(
        task_id="t-1", kind="task_fatal", code="approved_paths_missing",
        question="task t-1 has no approved_paths", detail="",
        phase="executing", now=NOW.isoformat(timespec="seconds"),
    )
    heartbeat.publish(config, LoopState(session_id="s", conversation_url=URL))

    beat = json.loads(config.heartbeat_file.read_text(encoding="utf-8"))
    assert beat["status"] == "blocked"
    assert beat["open_blockers"] == 1
    assert beat["needs_attention"] is True


def test_writing_never_raises(config, monkeypatch):
    """A monitor is an accessory. Failing to write its input must never take
    down the run it is watching."""
    monkeypatch.setattr(
        heartbeat.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    )
    heartbeat.publish(config, LoopState(session_id="s", conversation_url=URL))


def test_the_write_is_atomic(config):
    heartbeat.publish(config, LoopState(session_id="s", conversation_url=URL))
    leftovers = list(config.heartbeat_file.parent.glob("*.tmp"))
    assert leftovers == []


# --- the standalone checker (no autoloop import allowed) ----------------------


def _run_checker(hb_path, state_path, *extra):
    return subprocess.run(
        [sys.executable, str(CHECKER), "--heartbeat", str(hb_path),
         "--state", str(state_path), "--quiet", *extra],
        capture_output=True, text=True, timeout=60,
    )


def test_the_checker_imports_nothing_from_autoloop():
    """It is copied outside the repo and run from there. An `autoloop` import
    would make it unimportable exactly where it has to work."""
    source = CHECKER.read_text(encoding="utf-8")
    assert "import autoloop" not in source
    assert "from autoloop" not in source


def test_a_fresh_heartbeat_is_fine(tmp_path):
    hb = tmp_path / "heartbeat.json"
    heartbeat.write(hb, status="running", phase="executing")
    result = _run_checker(hb, tmp_path / "state")

    assert result.returncode == 0
    assert "running" in result.stdout


def test_a_stale_heartbeat_is_an_alarm(tmp_path):
    hb = tmp_path / "heartbeat.json"
    heartbeat.write(hb, status="running", phase="executing",
                    now=datetime.now(timezone.utc) - timedelta(hours=3))
    result = _run_checker(hb, tmp_path / "state")

    assert result.returncode == 1
    assert "stuck" in result.stdout


def test_a_clean_stop_is_not_an_alarm_even_though_it_is_stale(tmp_path):
    """The distinction the whole `stopped` status exists for. Judged BEFORE
    staleness, or every deliberate stop would cry wolf."""
    hb = tmp_path / "heartbeat.json"
    heartbeat.write(hb, status="stopped",
                    now=datetime.now(timezone.utc) - timedelta(days=2))
    result = _run_checker(hb, tmp_path / "state")

    assert result.returncode == 0
    assert "stopped" in result.stdout


def test_a_pause_is_not_an_alarm_either(tmp_path):
    hb = tmp_path / "heartbeat.json"
    heartbeat.write(hb, status="paused",
                    now=datetime.now(timezone.utc) - timedelta(days=2))
    assert _run_checker(hb, tmp_path / "state").returncode == 0


def test_a_missing_or_broken_heartbeat_still_reports(tmp_path):
    """A monitor that goes quiet when it breaks is the worst kind."""
    missing = _run_checker(tmp_path / "nope.json", tmp_path / "state")
    assert missing.returncode == 1

    broken = tmp_path / "heartbeat.json"
    broken.write_text("{not json", encoding="utf-8")
    assert _run_checker(broken, tmp_path / "state").returncode == 1


def test_a_blocked_beat_needs_attention(tmp_path):
    hb = tmp_path / "heartbeat.json"
    heartbeat.write(hb, status="blocked", open_blockers=2, detail="task t-1 has no paths")
    result = _run_checker(hb, tmp_path / "state")

    assert result.returncode == 1
    assert "decision" in result.stdout
