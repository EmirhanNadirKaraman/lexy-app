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


def _awake_throughout(start, end):
    return health.SleepEvidence(0.0, "awake throughout (test)")


def _asleep_for(minutes):
    return lambda start, end: health.SleepEvidence(minutes * 60.0, "test sleep window")


def _darwin_history(monkeypatch, *, boot, sleeptime=0.0, waketime=0.0):
    """Give `machine_sleep_in_window` a synthetic darwin wake history, so the
    REAL evidence-boundary logic runs against known facts instead of the test
    host's own sysctls. 0.0/0.0 is macOS for 'no sleep this boot'."""
    monkeypatch.setattr(health.sys, "platform", "darwin")
    monkeypatch.setattr(health, "boot_time_epoch", lambda: boot)
    monkeypatch.setattr(
        health,
        "_sysctl_timeval_epoch",
        {"kern.sleeptime": sleeptime, "kern.waketime": waketime}.get,
    )


def _check(config, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("agent_probe", lambda: False)
    # Deterministic default: the real probe reads this machine's sysctls, and a
    # test's verdict must not depend on when the test host last slept.
    kw.setdefault("sleep_probe", _awake_throughout)
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


# --- machine sleep must not read as silence (the 2026-08-05 false alarm) ------


def test_a_transcript_aged_only_by_machine_sleep_is_not_stuck(config):
    """The incident: 224 wall-clock minutes, all but a sliver of them a
    laptop asleep. Sleep is not silence — nothing could possibly have run."""
    _state(config, phase=Phase.AWAITING.value)
    _transcript(config, minutes_ago=224)
    windows = []

    def probe(start, end):
        windows.append((start, end))
        return health.SleepEvidence(220 * 60.0, "test sleep window")

    with LoopLock(config.state_dir):
        verdict = _check(config, sleep_probe=probe)

    assert verdict.code == health.OK_RUNNING
    assert verdict.needs_attention is False
    assert verdict.silent_minutes == pytest.approx(4, abs=1)
    # The probe is asked about exactly the transcript-to-now interval.
    assert windows == [
        ((NOW - timedelta(minutes=224)).timestamp(), NOW.timestamp())
    ]


def test_the_same_age_awake_throughout_is_stuck(config):
    """The mutation guard: a version that discounts ALL silence — instead of
    only proven sleep — must fail here."""
    _state(config, phase=Phase.AWAITING.value)
    _transcript(config, minutes_ago=224)
    with LoopLock(config.state_dir):
        verdict = _check(config, sleep_probe=_awake_throughout)

    assert verdict.code == health.STUCK_SILENT
    assert verdict.needs_attention is True
    assert verdict.silent_minutes == pytest.approx(224, abs=1)
    assert "no subagent running" in verdict.detail


def test_sleep_short_of_explaining_the_silence_is_still_stuck(config):
    """A partial discount must not excuse the remainder: 100m wall-clock with
    30m asleep is 70 awake minutes — over the threshold."""
    _state(config, phase=Phase.AWAITING.value)
    _transcript(config, minutes_ago=100)
    with LoopLock(config.state_dir):
        verdict = _check(config, sleep_probe=_asleep_for(30))

    assert verdict.code == health.STUCK_SILENT
    assert verdict.silent_minutes == pytest.approx(70, abs=1)
    assert "30m not provably awake discounted" in verdict.detail


def test_unavailable_wake_history_fails_toward_quiet_and_says_why(config):
    """No wake history means a slept laptop and a hung loop cannot be told
    apart — and a false 'stuck' is what discredits the monitor."""
    _state(config, phase=Phase.AWAITING.value)
    _transcript(config, minutes_ago=90)

    def probe(start, end):
        return health.SleepEvidence(None, "kern.sleeptime/kern.waketime unreadable")

    with LoopLock(config.state_dir):
        verdict = _check(config, sleep_probe=probe)

    assert verdict.needs_attention is False
    assert verdict.code == health.OK_RUNNING
    assert "wake history" in verdict.summary
    assert "kern.sleeptime/kern.waketime unreadable" in verdict.detail


def test_a_live_agent_wins_before_wake_history_is_consulted(config):
    """The existing proof-of-work rule stays load-bearing: a live agent
    suppresses the alarm whatever the wake history would have said."""
    _state(config, phase=Phase.EXECUTING.value)
    _transcript(config, minutes_ago=90)

    def probe(start, end):  # pragma: no cover - reaching it IS the failure
        raise AssertionError("sleep evidence must not be needed while an agent runs")

    with LoopLock(config.state_dir):
        verdict = _check(config, agent_probe=lambda: True, sleep_probe=probe)

    assert verdict.needs_attention is False
    assert "agent running" in verdict.summary


def test_multiple_sleeps_in_one_window_cannot_be_read_as_awake_silence(
    config, monkeypatch
):
    """100 minutes of silence holding TWO 30-minute sleeps: actual awake
    silence is 40 minutes, but kern.sleeptime/kern.waketime carry only the
    second sleep. Crediting the invisible 40 pre-pair minutes as awake would
    report 70 awake minutes => falsely stuck. Partial history must not
    authorize the alarm — only the tail since the last wake is proven."""
    _state(config, phase=Phase.AWAITING.value)
    _transcript(config, minutes_ago=100)
    _darwin_history(
        monkeypatch,
        boot=(NOW - timedelta(hours=20)).timestamp(),
        sleeptime=(NOW - timedelta(minutes=40)).timestamp(),
        waketime=(NOW - timedelta(minutes=10)).timestamp(),
    )
    with LoopLock(config.state_dir):
        verdict = _check(config, sleep_probe=health.machine_sleep_in_window)

    assert verdict.code == health.OK_RUNNING
    assert verdict.needs_attention is False
    assert verdict.silent_minutes == pytest.approx(10, abs=1)


def test_a_transcript_predating_the_current_boot_is_not_awake_silence(
    config, monkeypatch
):
    """A reboot inside the silence window: the machine was off, and 'no sleep
    recorded this boot' is true — but reading it as 224 awake minutes would
    be the same false alarm as the slept laptop. `lock.boot_time_epoch` is
    the shared boundary; time before it joins the discount."""
    _state(config, phase=Phase.AWAITING.value)
    _transcript(config, minutes_ago=224)
    _darwin_history(monkeypatch, boot=(NOW - timedelta(minutes=5)).timestamp())
    with LoopLock(config.state_dir):
        verdict = _check(config, sleep_probe=health.machine_sleep_in_window)

    assert verdict.code == health.OK_RUNNING
    assert verdict.needs_attention is False
    assert verdict.silent_minutes == pytest.approx(5, abs=1)


def test_partial_history_still_surfaces_a_hang_via_the_proven_awake_tail(
    config, monkeypatch
):
    """Failing quiet on the unprovable head must not blind the monitor: the
    tail since the last wake IS proven awake, and a hang keeps growing it.
    An hour of proven awake silence with no agent is stuck, whatever may
    hide before the last sleep — the mutation guard against a version that
    goes blanket-quiet whenever history is partial."""
    _state(config, phase=Phase.EXECUTING.value)
    _transcript(config, minutes_ago=200)
    _darwin_history(
        monkeypatch,
        boot=(NOW - timedelta(hours=20)).timestamp(),
        sleeptime=(NOW - timedelta(minutes=190)).timestamp(),
        waketime=(NOW - timedelta(minutes=60)).timestamp(),
    )
    with LoopLock(config.state_dir):
        verdict = _check(config, sleep_probe=health.machine_sleep_in_window)

    assert verdict.code == health.STUCK_SILENT
    assert verdict.needs_attention is True
    assert verdict.silent_minutes == pytest.approx(60, abs=1)
    assert "not provably awake discounted" in verdict.detail


# --- the platform evidence itself ---------------------------------------------


def test_the_darwin_evidence_is_the_last_sleeps_overlap_with_the_window(monkeypatch):
    """Windows starting at or after kern.sleeptime are COMPLETELY characterised
    by the pair (no sleep postdates kern.waketime while we run), so they get
    the exact overlap — awake-throughout included."""
    values = {"kern.sleeptime": 100.0, "kern.waketime": 400.0}
    monkeypatch.setattr(health, "_sysctl_timeval_epoch", values.get)

    assert health._darwin_sleep_evidence(200.0, 500.0).asleep_seconds == 200.0
    # A window entirely after the wake is provably awake — a real zero.
    assert health._darwin_sleep_evidence(400.0, 1000.0).asleep_seconds == 0.0


def test_a_darwin_window_predating_the_last_sleep_credits_only_the_proven_tail(
    monkeypatch,
):
    """The sysctls carry ONE pair, so a window starting before that sleep
    reaches time they cannot see — earlier finished sleeps hide there. Only
    the tail since the wake counts as awake; the rest joins the discount
    rather than authorizing a stuck verdict on unproven wakefulness."""
    values = {"kern.sleeptime": 400.0, "kern.waketime": 700.0}
    monkeypatch.setattr(health, "_sysctl_timeval_epoch", values.get)
    evidence = health._darwin_sleep_evidence(0.0, 800.0)

    # 100s provably awake since the wake; the other 700 are the 300s sleep
    # plus 400 unprovable pre-pair seconds, discounted alike.
    assert evidence.asleep_seconds == 700.0
    assert "predates the last sleep" in evidence.note


def test_an_unreadable_boot_time_makes_wake_history_unavailable(monkeypatch):
    """Sleep evidence describes one boot; without knowing when that boot was,
    the window cannot be tied to it. Honest unavailability, not a zero."""
    monkeypatch.setattr(health, "boot_time_epoch", lambda: None)
    evidence = health.machine_sleep_in_window(0.0, 1000.0)

    assert evidence.asleep_seconds is None
    assert "boot time unreadable" in evidence.note


def test_pre_boot_time_joins_the_discount(monkeypatch):
    """The off/reboot stretch before this boot could not have run the loop —
    it is discounted with the sleep, and the note says why."""
    _darwin_history(monkeypatch, boot=600.0)  # no sleep recorded this boot
    evidence = health.machine_sleep_in_window(0.0, 1000.0)

    assert evidence.asleep_seconds == 600.0
    assert "pre-boot" in evidence.note


def test_darwin_never_slept_this_boot_is_a_real_zero(monkeypatch):
    """0/0 is macOS for 'no sleep since boot' — established history, not a
    failure to read it, so a stuck verdict stays reachable."""
    monkeypatch.setattr(health, "_sysctl_timeval_epoch", lambda name: 0.0)
    evidence = health._darwin_sleep_evidence(0.0, 1000.0)

    assert evidence.asleep_seconds == 0.0


def test_darwin_unreadable_or_inconsistent_sysctls_are_unavailable(monkeypatch):
    monkeypatch.setattr(health, "_sysctl_timeval_epoch", lambda name: None)
    assert health._darwin_sleep_evidence(0.0, 1000.0).asleep_seconds is None

    # A wake stamped before its sleep is not a window anyone can subtract.
    values = {"kern.sleeptime": 500.0, "kern.waketime": 200.0}
    monkeypatch.setattr(health, "_sysctl_timeval_epoch", values.get)
    evidence = health._darwin_sleep_evidence(0.0, 1000.0)
    assert evidence.asleep_seconds is None
    assert "inconsistent" in evidence.note


def test_the_timeval_parse_matches_locks_boottime_format():
    out = "{ sec = 1754126400, usec = 837291 } Sat Aug  2 12:00:00 2026\n"
    assert health._parse_timeval_sec(out) == 1754126400.0


def test_linux_suspend_total_is_clamped_to_the_window(monkeypatch):
    monkeypatch.setattr(health.time, "CLOCK_BOOTTIME", 7, raising=False)
    monkeypatch.setattr(health.time, "clock_gettime", lambda clk: 5000.0)
    monkeypatch.setattr(health.time, "monotonic", lambda: 2000.0)

    # 3000s suspended since boot, but only a 1000s window to explain.
    assert health._linux_sleep_evidence(0.0, 1000.0).asleep_seconds == 1000.0
    # A window wider than the total gets the whole total, no more.
    assert health._linux_sleep_evidence(0.0, 10000.0).asleep_seconds == 3000.0


def test_linux_zero_suspend_is_a_real_zero(monkeypatch):
    monkeypatch.setattr(health.time, "CLOCK_BOOTTIME", 7, raising=False)
    monkeypatch.setattr(health.time, "clock_gettime", lambda clk: 2000.0)
    monkeypatch.setattr(health.time, "monotonic", lambda: 2000.0)

    assert health._linux_sleep_evidence(0.0, 1000.0).asleep_seconds == 0.0


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
