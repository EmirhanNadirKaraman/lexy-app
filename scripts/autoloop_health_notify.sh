#!/bin/bash
# Notify when autoloop needs a human. Meant for launchd/cron.
#
# Two things this gets right that a naive version does not:
#
#   * It runs `autoloop health` with the REPO as the working directory.
#     `state_dir = ".autoloop"` in the config is relative, so a scheduler that
#     inherits `/` (launchd does) would read a state dir that does not exist
#     and cheerfully report "not running" forever.
#
#   * It only notifies on a CHANGE of verdict. A check every 10 minutes
#     against a loop that has been blocked since breakfast would otherwise
#     produce 40 identical notifications, and the next real one gets ignored.
#
# Read-only: `health` takes no lock and writes nothing, so this is safe to run
# while the loop is mid-round.

set -u

REPO="${AUTOLOOP_REPO:-$HOME/Documents/GitHub/language-app}"
CONFIG="${AUTOLOOP_CONFIG:-.autoloop/config.toml}"
STATE_FILE="${AUTOLOOP_NOTIFY_STATE:-$HOME/.autoloop/last-health-code}"
LOG="${AUTOLOOP_NOTIFY_LOG:-$HOME/.autoloop/health-notify.log}"

mkdir -p "$(dirname "$STATE_FILE")" "$(dirname "$LOG")"

cd "$REPO" || {
  echo "$(date '+%F %T') FATAL: cannot cd to $REPO" >> "$LOG"
  exit 1
}

OUT=$(python3 -m autoloop health --json --config "$CONFIG" 2>&1)
RC=$?          # captured immediately: a pipe here would report the wrong status

# Parse without jq, which is not installed by default on macOS.
CODE=$(printf '%s' "$OUT" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('code', 'unparseable'))
except Exception:
    print('unparseable')
" 2>/dev/null)
SUMMARY=$(printf '%s' "$OUT" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('summary', ''), ('- ' + d['detail'][:120]) if d.get('detail') else '')
except Exception:
    print('autoloop health check could not be parsed')
" 2>/dev/null)

echo "$(date '+%F %T') rc=$RC code=$CODE $SUMMARY" >> "$LOG"

PREVIOUS=$(cat "$STATE_FILE" 2>/dev/null || echo "")
printf '%s' "$CODE" > "$STATE_FILE"

# Nothing wrong, or the same thing that was already reported.
[ "$RC" -eq 0 ] && exit 0
[ "$CODE" = "$PREVIOUS" ] && exit 0

# `display notification` needs the text as a quoted AppleScript string, so
# any embedded double quote would end it early.
SAFE=$(printf '%s' "$SUMMARY" | tr '\n' ' ' | sed 's/"/'"'"'/g' | cut -c1-200)
osascript -e "display notification \"$SAFE\" with title \"autoloop needs you\" sound name \"Submarine\"" 2>>"$LOG"
exit 0
