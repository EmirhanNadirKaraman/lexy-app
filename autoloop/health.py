"""Is the loop working, or does it need a human?

A monitor is only as good as its false-alarm rate. An alert that fires while
the loop is happily running teaches you to ignore alerts, and then the one
that matters is ignored too. So every signal here is chosen to distinguish
"quiet because it is working" from "quiet because it is stuck", and the
default thresholds are deliberately generous.

Signals, and why these rather than the obvious ones:

* **The lock, not a process name.** `LoopLock.is_live` is boot-aware and
  authoritative. Matching process names is what it looks like you should do
  and it is wrong twice over: the loop runs as `autoloop start` OR
  `autoloop run` depending on how it was launched, and `pgrep -fc` counts
  PATTERNS, not processes. Both mistakes were made against this very loop on
  2026-08-02 and both produced a confident wrong answer.

* **Transcript age, not `state.json` mtime.** State is written at phase
  TRANSITIONS, so a healthy loop mid-`executing` can leave it untouched for
  twenty minutes. Reading its mtime as liveness reports a working loop as
  dead — also made, also confidently wrong.

* **A live agent suppresses the silence alarm.** An audit fan-out runs six
  subagents for fifteen-plus minutes and writes nothing to the transcript
  while it does. That is the single most likely false alarm, so an agent
  process being alive is treated as proof of work even when everything else
  is quiet.

* **Silence is awake time, not wall-clock.** A laptop that sleeps for hours
  is indistinguishable from a hung loop if silence is measured on the wall
  clock — observed 2026-08-05: "no activity for 224 minutes" over a machine
  that had been asleep, with the loop writing again 69 seconds after wake.
  This is the SAME wrong assumption `lock.boot_time_epoch` already corrects
  for locks (kern.boottime / /proc/stat btime, deliberately never a
  monotonic clock — macOS stops the monotonic clock during sleep, the exact
  case in question), so the same evidence family is used here:
  kern.sleeptime/kern.waketime on darwin, CLOCK_BOOTTIME−CLOCK_MONOTONIC on
  linux. Proven sleep is subtracted from the silence before it is judged,
  and when wake history cannot be established the check FAILS TOWARD QUIET
  on this one axis — a false "stuck" trains a human to ignore the monitor,
  while a missed detection is retried by the next check minutes later.

Verdicts are advisory. Nothing here writes, locks, or touches the loop's
state — it is safe to run on any schedule, including while the loop is
mid-round.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .blockers import BlockerStore
from .lock import LoopLock
from .state import Phase, StateStore

#: How long a live loop may write nothing before it is called stuck. Generous
#: on purpose: an audit fan-out is quiet for fifteen-plus minutes, and a
#: review round can wait on a human-speed reviewer. Tightening this trades a
#: faster alert for false alarms, which is the wrong trade for a monitor.
#: Do NOT raise it to accommodate laptop sleep either — sleep is discounted
#: from the measured silence instead (`machine_sleep_in_window`); a bigger
#: threshold would only make a genuinely hung loop slower to surface.
DEFAULT_SILENCE_MINUTES = 45.0

#: Verdict codes. `needs_attention` is what a scheduler acts on.
OK_RUNNING = "running"
OK_PAUSED = "paused"
OK_IDLE = "idle"
STUCK_BLOCKED = "blocked"
STUCK_PARKED = "parked"
STUCK_FAILED = "failed"
STUCK_STALE_LOCK = "stale_lock"
STUCK_SILENT = "silent"
STUCK_NOT_RUNNING = "not_running"


@dataclass(frozen=True)
class Health:
    code: str
    needs_attention: bool
    summary: str
    detail: str = ""
    phase: str = ""
    open_blockers: int = 0
    silent_minutes: float | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def _agent_running(pattern: str = "claude -p") -> bool:
    """Is a subagent alive? Suppresses the silence alarm.

    `pgrep -f` LISTS matches; `pgrep -fc` counts patterns and will happily
    report 0 while a match is running. Use the listing and count lines.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(result.stdout.strip())


@dataclass(frozen=True)
class SleepEvidence:
    """What the platform can prove about machine sleep inside a time window.

    `asleep_seconds is None` means wake history could not be established —
    the caller must then fail toward quiet, and `note` says why so the
    verdict's detail can explain itself. When it is a number, it is seconds
    of the window the machine PROVABLY did not spend awake (0.0 is a real
    answer: awake throughout).
    """

    asleep_seconds: float | None
    note: str


def _parse_timeval_sec(out: str) -> float:
    """`{ sec = 1754126400, usec = 837291 } Sat Aug  2 ...` → 1754126400.0.

    The same output shape — and the same parse — as `lock.boot_time_epoch`
    uses for kern.boottime, so the two modules read one family of evidence.
    """
    marker = "sec = "
    start = out.index(marker) + len(marker)
    end = start
    while end < len(out) and out[end].isdigit():
        end += 1
    return float(out[start:end])


def _sysctl_timeval_epoch(name: str) -> float | None:
    try:
        out = subprocess.run(
            ["sysctl", "-n", name],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        return _parse_timeval_sec(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _darwin_sleep_evidence(window_start: float, window_end: float) -> SleepEvidence:
    """Sleep overlap from kern.sleeptime/kern.waketime — the last sleep→wake
    pair, which is exactly the incident shape (a long sleep, the check
    running right after wake). Earlier sleeps in a long window are invisible
    to these sysctls, so this can under-discount, never over-discount.
    """
    slept = _sysctl_timeval_epoch("kern.sleeptime")
    woke = _sysctl_timeval_epoch("kern.waketime")
    if slept is None or woke is None:
        return SleepEvidence(None, "kern.sleeptime/kern.waketime unreadable")
    if slept <= 0 and woke <= 0:
        return SleepEvidence(0.0, "no sleep recorded this boot")
    if slept <= 0 or woke < slept:
        # A wake without its sleep (or the reverse) is not a window we can
        # subtract; guessing here is how a hung loop gets excused.
        return SleepEvidence(None, "kern.sleeptime/kern.waketime inconsistent")
    overlap = min(woke, window_end) - max(slept, window_start)
    return SleepEvidence(max(0.0, overlap), "kern.sleeptime/kern.waketime")


def _linux_sleep_evidence(window_start: float, window_end: float) -> SleepEvidence:
    """CLOCK_BOOTTIME advances during suspend, CLOCK_MONOTONIC does not (on
    linux), so their difference is total suspend since boot. It carries no
    placement, so the whole amount is credited to the window (clamped):
    over-crediting can only miss a detection, never fabricate one — the
    direction this monitor is built to fail toward.
    """
    try:
        total = time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
    except (AttributeError, OSError):
        return SleepEvidence(None, "CLOCK_BOOTTIME unavailable")
    if total <= 0:
        return SleepEvidence(0.0, "no suspend recorded this boot")
    window = max(0.0, window_end - window_start)
    return SleepEvidence(min(total, window), "CLOCK_BOOTTIME-CLOCK_MONOTONIC")


def machine_sleep_in_window(window_start: float, window_end: float) -> SleepEvidence:
    """Proven machine sleep (seconds) inside [window_start, window_end] epoch
    seconds, or an unavailable verdict the caller must treat as quiet."""
    if sys.platform == "darwin":
        return _darwin_sleep_evidence(window_start, window_end)
    if sys.platform.startswith("linux"):
        return _linux_sleep_evidence(window_start, window_end)
    return SleepEvidence(None, f"no wake-history source on {sys.platform}")


def last_transcript_event(path: Path) -> datetime | None:
    """Timestamp of the newest transcript entry, or None.

    Reads only the tail: a long run's transcript grows without bound and a
    health check must stay cheap enough to run every few minutes.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, size - 65536))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            stamp = json.loads(line).get("ts")
        except json.JSONDecodeError:
            continue  # a torn final line during a concurrent append
        if not stamp:
            continue
        try:
            parsed = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def check(
    config,
    now: datetime | None = None,
    silence_minutes: float = DEFAULT_SILENCE_MINUTES,
    agent_probe=_agent_running,
    sleep_probe=machine_sleep_in_window,
) -> Health:
    """Judge the loop. Read-only, and safe to run mid-round."""
    now = now or datetime.now(timezone.utc)

    lock = LoopLock(config.state_dir)
    info = lock.read()
    live = info is not None and LoopLock.is_live(info)

    if info is not None and not live:
        return Health(
            code=STUCK_STALE_LOCK,
            needs_attention=True,
            summary="autoloop crashed — a stale lock is left behind",
            detail=f"{info.describe()}; recover with `python -m autoloop start`",
        )

    blockers = BlockerStore(config.blockers_dir).open_blockers()
    state = StateStore(config.state_file).load() if config.state_file.exists() else None
    phase = state.phase if state is not None else ""

    # Blockers first: they are the reason a human is needed, and they outlive
    # the session that raised them.
    if blockers:
        first = blockers[0]
        return Health(
            code=STUCK_BLOCKED,
            needs_attention=True,
            summary=f"autoloop needs a decision — {len(blockers)} open blocker(s)",
            detail=f"{first.id} ({first.code}): {first.question[:200]}",
            phase=phase,
            open_blockers=len(blockers),
        )

    if state is not None and Phase(phase) is Phase.FAILED:
        return Health(
            code=STUCK_FAILED,
            needs_attention=True,
            summary="autoloop session FAILED",
            detail="resolve with `python -m autoloop run --retry`",
            phase=phase,
        )

    if state is not None and Phase(phase) is Phase.NEEDS_USER:
        # A task_fatal park is one continuous mode handles by quarantining
        # that task and carrying on, so it is only worth waking someone for
        # when the loop is not running to handle it.
        handled = getattr(state, "park_kind", None) == "task_fatal" and live
        if not handled:
            return Health(
                code=STUCK_PARKED,
                needs_attention=True,
                summary="autoloop is parked and waiting for you",
                detail=(state.question or "(no question recorded)")[:200],
                phase=phase,
            )

    # A pause is a decision, not a fault.
    if config.pause_file.exists() or config.legacy_pause_file.exists():
        return Health(
            code=OK_PAUSED,
            needs_attention=False,
            summary="autoloop is paused (`resume` to continue)",
            phase=phase,
        )

    if not live:
        return Health(
            code=STUCK_NOT_RUNNING,
            needs_attention=True,
            summary="autoloop is not running",
            detail="start it with `python -m autoloop start`",
            phase=phase,
        )

    last = last_transcript_event(config.transcript_file)
    silent = None if last is None else (now - last).total_seconds() / 60.0
    if silent is not None and silent > silence_minutes:
        if agent_probe():
            # The commonest false alarm: an audit fan-out is quiet for
            # fifteen-plus minutes while six subagents work. Checked before
            # sleep evidence on purpose — a live agent is proof of work
            # whatever the wall clock or the wake history says.
            return Health(
                code=OK_RUNNING,
                needs_attention=False,
                summary=f"autoloop is working (agent running, quiet {silent:.0f}m)",
                phase=phase,
                silent_minutes=silent,
            )
        evidence = sleep_probe(last.timestamp(), now.timestamp())
        if evidence.asleep_seconds is None:
            # Fail toward quiet: without wake history a slept laptop and a
            # hung loop are indistinguishable, and a false "stuck" is the
            # alarm that teaches a human to ignore this monitor.
            return Health(
                code=OK_RUNNING,
                needs_attention=False,
                summary=(
                    f"autoloop is quiet ({silent:.0f}m) but wake history is "
                    "unavailable — not calling it stuck"
                ),
                detail=(
                    f"{evidence.note}; the machine may have been asleep, "
                    "and the next check re-judges in minutes"
                ),
                phase=phase,
                silent_minutes=silent,
            )
        asleep = evidence.asleep_seconds / 60.0
        awake_silent = max(0.0, silent - asleep)
        if awake_silent > silence_minutes:
            discount = f", {asleep:.0f}m of machine sleep discounted" if asleep > 0 else ""
            return Health(
                code=STUCK_SILENT,
                needs_attention=True,
                summary=f"autoloop looks stuck — no activity for {awake_silent:.0f} minutes",
                detail=f"phase={phase}, no subagent running{discount}",
                phase=phase,
                silent_minutes=awake_silent,
            )
        return Health(
            code=OK_RUNNING,
            needs_attention=False,
            summary=(
                f"autoloop is running (quiet {silent:.0f}m, "
                f"{asleep:.0f}m of it machine sleep)"
            ),
            phase=phase,
            silent_minutes=awake_silent,
        )

    return Health(
        code=OK_RUNNING,
        needs_attention=False,
        summary=f"autoloop is running (phase={phase or 'starting'})",
        phase=phase,
        silent_minutes=silent,
    )
