"""The loop's published liveness, readable without touching the checkout.

`health.check` answers the same question better, but it has to read the state
dir, the blocker store and the transcript — all inside `~/Documents` on this
machine, which macOS TCC puts out of reach of a launchd agent (`getcwd:
Operation not permitted`, exit 126). A monitor that only reads this one file,
written somewhere unprotected, needs no Full Disk Access grant.

So the loop publishes; the monitor judges. The split matters:

* **Staleness is the monitor's signal, not the loop's.** A loop that has hung,
  crashed, or been killed cannot write "I am stuck" — it simply stops writing.
  So the file carries a timestamp and the monitor applies the threshold. That
  is the one failure a self-report can never cover.
* **Everything the loop DOES know goes in the file.** Blockers, a park, a
  pause: the loop is alive and aware in each case, and a monitor that had to
  infer them from silence would be both slower and wrong (a pause is not a
  fault).

Written atomically, and never inside the checkout — see
`AutoloopConfig.heartbeat_file` for both reasons.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

#: Status values. `stopped` is written on a CLEAN exit, so a deliberate stop
#: is distinguishable from a crash — both leave the file unchanged afterwards,
#: and without this the monitor could only see "stale" and would cry wolf
#: every time you stopped the loop on purpose.
RUNNING = "running"
PAUSED = "paused"
PARKED = "parked"
BLOCKED = "blocked"
STOPPED = "stopped"

#: Statuses the monitor should wake someone for. `stopped` is deliberately NOT
#: here: you stopped it, you know.
ATTENTION_STATUSES = frozenset({PARKED, BLOCKED})


def write(
    path: Path,
    *,
    status: str,
    phase: str = "",
    session_id: str = "",
    open_blockers: int = 0,
    detail: str = "",
    now: datetime | None = None,
) -> None:
    """Publish one heartbeat. Best-effort by design.

    A monitor is an accessory: failing to write its input must never take down
    the run it is watching. Any error here is swallowed for that reason — the
    monitor's own staleness check is what notices a heartbeat that stopped
    arriving, whatever the cause.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    payload = {
        "ts": stamp,
        "pid": os.getpid(),
        "status": status,
        "phase": phase,
        "session_id": session_id,
        "open_blockers": open_blockers,
        "detail": detail[:300],
        "needs_attention": status in ATTENTION_STATUSES,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def publish(config, state=None, status: str = RUNNING, detail: str = "") -> None:
    """Write a heartbeat from whatever the caller already has in hand.

    Deliberately tolerant: called from the hot loop, so it must not need a
    fully-formed state, a readable blocker directory, or anything else that
    could raise while the loop is mid-round.
    """
    open_blockers = 0
    try:
        from .blockers import BlockerStore

        open_blockers = len(BlockerStore(config.blockers_dir).open_blockers())
    except Exception:
        pass

    if status == RUNNING and open_blockers:
        status = BLOCKED

    write(
        config.heartbeat_file,
        status=status,
        phase=getattr(state, "phase", "") or "",
        session_id=getattr(state, "session_id", "") or "",
        open_blockers=open_blockers,
        detail=detail or (getattr(state, "question", "") or ""),
    )
