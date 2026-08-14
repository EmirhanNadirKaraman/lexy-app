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
#
# The three rules below all exist because this script previously reported
# success while restarting nothing at all (2026-08-14, see
# docs/COMMON_ERRORS.md §1). A recovery command that lies is worse than one
# that fails: the loop retried against the same wedged browser for hours,
# every attempt logging `returncode 0` and "autoloop chrome up".
#
#   1. Kill the MAIN browser, not a renderer. Every helper process inherits
#      --user-data-dir, so a naive match returns ~10 pids, and the old
#      `head -1` took the LOWEST — reliably a renderer. Chrome respawns a
#      killed renderer instantly, so the browser never actually restarted.
#   2. Match the profile EXACTLY. A substring match makes `.autoloop-chrome`
#      also select `.autoloop-chrome-backup`.
#   3. Prove the port is FREE before launching. The readiness probe cannot
#      tell a newly started browser from the stale one that never died and
#      still holds the port — which is what turned (1) into a silent success.
set -uo pipefail

PROFILE="${AUTOLOOP_CHROME_PROFILE:-$HOME/.autoloop-chrome}"
PORT="${AUTOLOOP_CHROME_PORT:-9222}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Main browser processes for this exact profile.
#
# Three filters, each load-bearing:
#   * the command must BE the browser binary. Helpers live under
#     .../Frameworks/.../Google Chrome Helper, and — the trap that bit the
#     first version of this fix — the `awk` below carries the profile path in
#     its OWN argv, so a plain content match selects the matcher itself. This
#     is what `grep -v grep` used to cover.
#   * `--type=` excludes renderer/gpu/utility children.
#   * the profile must match exactly, so `.autoloop-chrome` does not also
#     select `.autoloop-chrome-backup`.
main_pids() {
    ps -eo pid=,command= | awk -v prof="--user-data-dir=${PROFILE}" -v chrome="${CHROME}" '
        BEGIN { plen = length(prof) }
        {
            pid = $1
            cmd = substr($0, index($0, $2))
            if (index(cmd, chrome) != 1) next
            if (index(cmd, "--type=") > 0) next
            p = index(cmd, prof)
            if (p == 0) next
            nxt = substr(cmd, p + plen, 1)
            if (nxt != "" && nxt != " ") next
            print pid
        }
    '
}

port_held() {
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1
}

pids="$(main_pids)"
if [ -n "${pids}" ]; then
    echo "stopping autoloop chrome (pids: $(echo "${pids}" | tr '\n' ' ' | sed 's/ $//'), profile ${PROFILE})"
    # shellcheck disable=SC2086
    kill -TERM ${pids} 2>/dev/null

    for _ in $(seq 1 20); do
        [ -z "$(main_pids)" ] && break
        sleep 1
    done

    # A wedged Chrome ignores SIGTERM — the exact state this script exists to
    # clear, observed holding CDP open-but-unresponsive for days. Escalating is
    # the whole point; refusing here would leave the loop stuck forever.
    remaining="$(main_pids)"
    if [ -n "${remaining}" ]; then
        echo "SIGTERM ignored, escalating to SIGKILL: $(echo "${remaining}" | tr '\n' ' ' | sed 's/ $//')"
        # shellcheck disable=SC2086
        kill -9 ${remaining} 2>/dev/null
        for _ in $(seq 1 10); do
            [ -z "$(main_pids)" ] && break
            sleep 1
        done
    fi

    if [ -n "$(main_pids)" ]; then
        echo "refusing: autoloop chrome would not exit"
        exit 1
    fi
fi

# The port must be free now. If it is not, something we did not start is
# serving CDP on it, and launching would produce a browser with no debugging
# port while the probe below happily succeeds against the impostor.
for _ in $(seq 1 10); do
    port_held || break
    sleep 1
done
if port_held; then
    echo "refusing: port ${PORT} still held after stopping the profile's browser"
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN 2>/dev/null | tail -n +2
    exit 1
fi

[ -x "${CHROME}" ] || { echo "chrome not found at ${CHROME}"; exit 1; }
open -na "Google Chrome" --args --user-data-dir="${PROFILE}" --remote-debugging-port="${PORT}"

# Report readiness rather than assuming it: the loop reconnects immediately
# after this returns, and a half-started browser looks exactly like the fault
# being recovered from. Require webSocketDebuggerUrl, not just a 200 — that
# field is what the loop actually connects to.
for _ in $(seq 1 30); do
    if curl -s --max-time 2 "http://127.0.0.1:${PORT}/json/version" 2>/dev/null \
        | grep -q "webSocketDebuggerUrl"; then
        echo "autoloop chrome up on ${PORT} (pid $(main_pids | head -1))"
        exit 0
    fi
    sleep 1
done
echo "chrome did not expose CDP on ${PORT} within 30s"
exit 1
