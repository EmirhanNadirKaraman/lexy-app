#!/bin/bash
# Install the durable autoloop health monitor (macOS launchd).
#
# The point of the copy: macOS TCC blocks a launchd agent from reading
# `~/Documents`, so a job pointed at the checkout dies with exit 126 and
# `Operation not permitted`. Everything the agent touches therefore lives
# under ~/.autoloop — the checker script and the heartbeat the loop writes
# there. No Full Disk Access grant is needed, because no protected path is
# ever read.
#
# Idempotent: re-run after changing the checker to update the installed copy.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET_DIR="$HOME/.autoloop"
LABEL="com.autoloop.health"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
INTERVAL="${AUTOLOOP_HEALTH_INTERVAL:-600}"

mkdir -p "$TARGET_DIR" "$HOME/Library/LaunchAgents"

cp "$REPO_DIR/scripts/check_heartbeat.py" "$TARGET_DIR/check_heartbeat.py"
chmod +x "$TARGET_DIR/check_heartbeat.py"
echo "installed checker -> $TARGET_DIR/check_heartbeat.py"

PYTHON_BIN="$(command -v python3)"

cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$TARGET_DIR/check_heartbeat.py</string>
  </array>
  <!-- Deliberately NOT the repo: a working directory inside ~/Documents is
       what TCC refuses, and nothing here needs it. -->
  <key>WorkingDirectory</key>
  <string>$TARGET_DIR</string>
  <!-- StartInterval, not StartCalendarInterval: this should resume on its own
       cadence after sleep or a reboot rather than waiting for a clock match. -->
  <key>StartInterval</key>
  <integer>$INTERVAL</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$TARGET_DIR/health-monitor.log</string>
  <key>StandardErrorPath</key>
  <string>$TARGET_DIR/health-monitor.err</string>
</dict>
</plist>
PLISTEOF
echo "wrote $PLIST (every ${INTERVAL}s)"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "loaded $LABEL"

sleep 3
if launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
  echo "running. recent output:"
  tail -3 "$TARGET_DIR/health-monitor.log" 2>/dev/null || true
  tail -3 "$TARGET_DIR/health-monitor.err" 2>/dev/null || true
else
  echo "WARNING: agent did not load; inspect with:"
  echo "  launchctl print gui/$(id -u)/$LABEL"
  exit 1
fi

echo
echo "uninstall:  launchctl bootout gui/\$(id -u)/$LABEL && rm $PLIST"
