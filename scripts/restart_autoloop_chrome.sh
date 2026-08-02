#!/usr/bin/env bash
# Restart ONLY the dedicated autoloop Chrome profile.
#
# Wired in via `[browser].restart_command` so the loop can recover from a
# session loss without a human. Three browser stalls in one session were each
# cleared by exactly this, by hand.
#
# It targets the profile by its --user-data-dir and nothing else: never
# `pkill Chrome`, which would take down the operator's everyday browser. If no
# process matches, it just launches one.
set -uo pipefail

PROFILE="${AUTOLOOP_CHROME_PROFILE:-$HOME/.autoloop-chrome}"
PORT="${AUTOLOOP_CHROME_PORT:-9222}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

pid="$(ps -eo pid,command | grep -- "--user-data-dir=${PROFILE}" | grep -v grep | awk '{print $1}' | head -1)"
if [ -n "${pid}" ]; then
    echo "stopping autoloop chrome (pid ${pid}, profile ${PROFILE})"
    kill -TERM "${pid}" 2>/dev/null
    for _ in $(seq 1 20); do
        kill -0 "${pid}" 2>/dev/null || break
        sleep 1
    done
    kill -0 "${pid}" 2>/dev/null && { echo "refusing: pid ${pid} would not exit"; exit 1; }
fi

[ -x "${CHROME}" ] || { echo "chrome not found at ${CHROME}"; exit 1; }
open -na "Google Chrome" --args --user-data-dir="${PROFILE}" --remote-debugging-port="${PORT}"

# Report readiness rather than assuming it: the loop reconnects immediately
# after this returns, and a half-started browser looks exactly like the fault
# being recovered from.
for _ in $(seq 1 30); do
    if curl -s --max-time 2 "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
        echo "autoloop chrome up on ${PORT}"
        exit 0
    fi
    sleep 1
done
echo "chrome did not expose CDP on ${PORT} within 30s"
exit 1
