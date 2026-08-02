"""`health`: is the loop working, or does it need a human?

A monitor is only as good as its false-alarm rate — an alert that fires while
the loop is fine teaches you to ignore alerts. So the tests that matter most
here are the ones proving it stays QUIET: an audit fan-out is legitimately
silent for fifteen-plus minutes, and a deliberate pause is a decision rather
than a fault.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from autoloop import health
from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.lock import LoopLock
from autoloop.state import LoopState, Phase, StateStore

URL = "https://chatgpt.com/c/health"
NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


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


def _state(config, **kw):
    config.state_dir.mkdir(parents=True, exist_ok=True)
    StateStore(config.state_file).save(
        LoopState(session_id="health", conversation_url=URL, **kw)
    )


def _transcript(config, minutes_ago: float):
    config.transcript_file.parent.mkdir(parents=True, exist_ok=True)
    stamp = (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    config.transcript_file.write_text(
        json.dumps({"ts": stamp, "type": "directive"}) + "\n", encoding="utf-8"
    )


def _blocker(config, code="approved_paths_missing"):
    return BlockerStore(config.blockers_dir).record(
        task_id="t-1", kind="task_fatal", code=code,
        question="task t-1 has no approved_paths", detail="",
        phase="executing", now=NOW.isoformat(timespec="seconds"),
    )


def _check(config, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("agent_probe", lambda: False)
    return health.check(config, **kw)


# --- staying quiet when the loop is fine (the false-alarm surface) ------------


def test_a_running_loop_needs_no_attention(config):
    _state(config, phase=Phase.EXECUTING.value)
    _transcript(config, minutes_ago=2)
    with LoopLock(config.state_dir):
        verdict = _check(config)

    assert verdict.code == health.OK_RUNNING
    assert verdict.needs_attention is False


def test_a_long_audit_fanout_is_not_stuck(config):
    """THE false alarm to avoid: six subagents run for fifteen-plus minutes
    and write nothing to the transcript. A live agent is proof of work."""
    _state(config, phase=Phase.EXECUTING.value)
    _transcript(config, minutes_ago=90)
    with LoopLock(config.state_dir):
        verdict = _check(config, agent_probe=lambda: True)

    assert verdict.needs_attention is False
    assert "agent running" in verdict.summary


def test_a_deliberate_pause_is_not_a_fault(config):
    _state(config, phase=Phase.EXECUTING.value)
    config.pause_file.parent.mkdir(parents=True, exist_ok=True)
    config.pause_file.touch()
    with LoopLock(config.state_dir):
        verdict = _check(config)

    assert verdict.code == health.OK_PAUSED
    assert verdict.needs_attention is False


def test_a_task_fatal_park_on_a_live_loop_is_not_escalated(config):
    """Continuous mode quarantines that task and carries on, so waking
    someone would be noise."""
    _state(config, phase=Phase.NEEDS_USER.value, park_kind="task_fatal")
    _transcript(config, minutes_ago=1)
    with LoopLock(config.state_dir):
        verdict = _check(config)

    assert verdict.needs_attention is False


# --- the things worth waking someone for --------------------------------------


def test_an_open_blocker_needs_attention(config):
    _state(config, phase=Phase.EXECUTING.value)
    _transcript(config, minutes_ago=1)
    _blocker(config)
    with LoopLock(config.state_dir):
        verdict = _check(config)

    assert verdict.code == health.STUCK_BLOCKED
    assert verdict.needs_attention is True
    assert verdict.open_blockers == 1


def test_a_loop_fatal_park_needs_attention(config):
    _state(
        config,
        phase=Phase.NEEDS_USER.value,
        park_kind="loop_fatal",
        question="the conversation is unusable",
    )
    with LoopLock(config.state_dir):
        verdict = _check(config)

    assert verdict.code == health.STUCK_PARKED
    assert "unusable" in verdict.detail


def test_a_failed_session_needs_attention(config):
    _state(config, phase=Phase.FAILED.value)
    with LoopLock(config.state_dir):
        verdict = _check(config)

    assert verdict.code == health.STUCK_FAILED


def test_a_crash_leaves_a_stale_lock(config):
    """The signal that the process died without unwinding."""
    lock = LoopLock(config.state_dir)
    lock.acquire()
    lock._owned = False  # keep the file behind
    data = json.loads(lock.path.read_text(encoding="utf-8"))
    data["pid"] = 999_999_999
    lock.path.write_text(json.dumps(data), encoding="utf-8")

    verdict = _check(config)

    assert verdict.code == health.STUCK_STALE_LOCK
    assert verdict.needs_attention is True


def test_a_loop_that_is_not_running_needs_attention(config):
    _state(config, phase=Phase.EXECUTING.value)
    verdict = _check(config)

    assert verdict.code == health.STUCK_NOT_RUNNING


def test_a_live_but_silent_loop_is_stuck(config):
    """Live lock, no agent, nothing written for longer than the threshold."""
    _state(config, phase=Phase.EXECUTING.value)
    _transcript(config, minutes_ago=90)
    with LoopLock(config.state_dir):
        verdict = _check(config)

    assert verdict.code == health.STUCK_SILENT
    assert verdict.needs_attention is True
    assert verdict.silent_minutes == pytest.approx(90, abs=1)


def test_the_silence_threshold_is_honoured(config):
    _state(config, phase=Phase.EXECUTING.value)
    _transcript(config, minutes_ago=20)
    with LoopLock(config.state_dir):
        assert _check(config, silence_minutes=45).needs_attention is False
        assert _check(config, silence_minutes=10).needs_attention is True


# --- transcript reading -------------------------------------------------------


def test_the_newest_entry_wins_and_only_the_tail_is_read(config):
    """Transcripts grow without bound; a check that runs every few minutes
    must not read the whole file."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"ts": (NOW - timedelta(minutes=m)).isoformat(timespec="seconds"),
                    "type": "x", "pad": "y" * 500})
        for m in range(400, 0, -1)
    ]
    config.transcript_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    last = health.last_transcript_event(config.transcript_file)
    assert last == NOW - timedelta(minutes=1)


def test_a_torn_or_missing_transcript_does_not_crash(config):
    assert health.last_transcript_event(config.transcript_file) is None

    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.transcript_file.write_text("", encoding="utf-8")
    assert health.last_transcript_event(config.transcript_file) is None

    # A half-written final line during a concurrent append.
    config.transcript_file.write_text(
        json.dumps({"ts": NOW.isoformat(timespec="seconds"), "type": "ok"})
        + "\n{\"ts\": \"2026-08-0",
        encoding="utf-8",
    )
    assert health.last_transcript_event(config.transcript_file) == NOW


def test_check_never_writes_anything(config):
    """It runs on a schedule, possibly mid-round. A write into the state dir
    is exactly what the escape detector parks the loop for."""
    _state(config, phase=Phase.EXECUTING.value)
    _transcript(config, minutes_ago=1)
    before = {p: p.stat().st_mtime_ns for p in config.state_dir.rglob("*") if p.is_file()}

    with LoopLock(config.state_dir):
        _check(config)

    after = {p: p.stat().st_mtime_ns for p in config.state_dir.rglob("*") if p.is_file()}
    assert before == after
