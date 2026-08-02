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

Verdicts are advisory. Nothing here writes, locks, or touches the loop's
state — it is safe to run on any schedule, including while the loop is
mid-round.
"""

from __future__ import annotations

import json
import subprocess
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
            # fifteen-plus minutes while six subagents work.
            return Health(
                code=OK_RUNNING,
                needs_attention=False,
                summary=f"autoloop is working (agent running, quiet {silent:.0f}m)",
                phase=phase,
                silent_minutes=silent,
            )
        return Health(
            code=STUCK_SILENT,
            needs_attention=True,
            summary=f"autoloop looks stuck — no activity for {silent:.0f} minutes",
            detail=f"phase={phase}, no subagent running",
            phase=phase,
            silent_minutes=silent,
        )

    return Health(
        code=OK_RUNNING,
        needs_attention=False,
        summary=f"autoloop is running (phase={phase or 'starting'})",
        phase=phase,
        silent_minutes=silent,
    )
