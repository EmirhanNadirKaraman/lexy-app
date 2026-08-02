#!/usr/bin/env python3
"""Judge autoloop from its heartbeat alone. Standalone by requirement.

This file is COPIED outside the repository by `install_health_monitor.sh` and
run from there. It must therefore import nothing from `autoloop` and nothing
from PyPI: macOS TCC blocks a launchd agent from reading `~/Documents` at all,
which is the whole reason the heartbeat exists. If you find yourself adding an
`autoloop` import here, the monitor stops working and the failure looks like
exit 126 with `Operation not permitted`.

Exit codes:
    0  fine, or a state nobody needs waking for
    1  needs a human
    2  the check itself could not run

Notifies only on a CHANGE of verdict — a loop blocked since breakfast would
otherwise produce one alert per interval, and the next real one gets ignored.
A check that breaks still notifies: a monitor that goes quiet when it fails is
the worst kind.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

DEFAULT_HEARTBEAT = os.path.expanduser("~/.autoloop/heartbeat.json")
DEFAULT_STATE = os.path.expanduser("~/.autoloop/last-health-code")

#: A single-threaded loop is blocked inside an agent call for minutes at a
#: time — an audit fan-out runs six subagents for fifteen-plus — so it cannot
#: heartbeat during one. Generous on purpose: a false alarm teaches you to
#: ignore alarms, and then the real one is ignored too.
DEFAULT_STALE_MINUTES = 45.0


def verdict(path, stale_minutes, now=None):
    """Return (code, needs_attention, message)."""
    now = now or datetime.now(timezone.utc)
    try:
        with open(path, encoding="utf-8") as handle:
            beat = json.load(handle)
    except FileNotFoundError:
        return ("no_heartbeat", True, "autoloop has never run, or its heartbeat is missing")
    except (OSError, json.JSONDecodeError) as exc:
        return ("unreadable", True, f"autoloop heartbeat unreadable: {exc}")

    status = beat.get("status", "unknown")
    detail = (beat.get("detail") or "").strip().replace("\n", " ")

    # A clean stop is a decision, not a fault — and its heartbeat is stale by
    # definition, so this must be judged BEFORE staleness.
    if status == "stopped":
        return ("stopped", False, "autoloop is stopped (clean exit)")
    if status == "paused":
        return ("paused", False, "autoloop is paused")

    try:
        beat_at = datetime.fromisoformat(beat["ts"])
    except (KeyError, TypeError, ValueError):
        return ("unreadable", True, "autoloop heartbeat has no usable timestamp")
    if beat_at.tzinfo is None:
        beat_at = beat_at.replace(tzinfo=timezone.utc)

    age = (now - beat_at).total_seconds() / 60.0
    if age > stale_minutes:
        return (
            "stale",
            True,
            f"autoloop looks stuck — no heartbeat for {age:.0f} minutes "
            f"(phase={beat.get('phase') or 'unknown'})",
        )

    if beat.get("needs_attention"):
        return (
            status,
            True,
            f"autoloop needs a decision — {status}"
            + (f": {detail[:120]}" if detail else ""),
        )
    return (status, False, f"autoloop is {status} (phase={beat.get('phase') or '?'})")


def notify(message):
    """macOS notification. Quotes are stripped: the message becomes an
    AppleScript string literal, and an embedded quote would end it early."""
    safe = message.replace('"', "'").replace("\\", "")[:200]
    try:
        subprocess.run(
            [
                "osascript",
                "-e",
                f'display notification "{safe}" with title "autoloop needs you" '
                'sound name "Submarine"',
            ],
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--heartbeat", default=DEFAULT_HEARTBEAT)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--stale-minutes", type=float, default=DEFAULT_STALE_MINUTES)
    parser.add_argument("--quiet", action="store_true", help="never notify; just report")
    args = parser.parse_args()

    code, needs_attention, message = verdict(args.heartbeat, args.stale_minutes)
    print(f"{code}: {message}")

    previous = ""
    try:
        with open(args.state, encoding="utf-8") as handle:
            previous = handle.read().strip()
    except OSError:
        pass
    try:
        os.makedirs(os.path.dirname(args.state), exist_ok=True)
        with open(args.state, "w", encoding="utf-8") as handle:
            handle.write(code)
    except OSError:
        pass

    if needs_attention and code != previous and not args.quiet:
        notify(message)
    return 1 if needs_attention else 0


if __name__ == "__main__":
    sys.exit(main())
