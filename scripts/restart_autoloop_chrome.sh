#!/usr/bin/env bash
# RETIRED 2026-08-16 (task brw-08). Replaced by autoloop/browser/chrome_restart.py.
#
# This file no longer restarts anything. It is a tombstone: it says what to run
# instead, to whoever still runs it.
#
# NOT for the loop's own restart path — that route is already closed, and more
# firmly than deleting this file would close it. A `browser.restart_command`
# still naming this script is refused by `config.load_config`, so such a config
# never loads and this is never launched. This is for the humans and the odds
# and ends: muscle memory, a shell-history line, an open doc, a wrapper of your
# own outside `restart_command`. Deleting it answers all of those with bash's
# exit 127 and "No such file or directory".
#
# Remove it (`git rm`) once the live configs have been switched over and the
# path has stopped being typed.
#
# Why Python: the post-commit validation runner allows only
# ruff/pytest/python/npm/npx/tsc, so a `.sh` helper cannot be validated at all —
# this one shipped a bug (stop ONE pid, relaunch into a survivor that still
# owned the debug port, report success) that no test in this repo could have
# caught. The module stops EVERY Chrome on the profile, waits for the port to
# actually free, and proves the endpoint answers before reporting success.
# Its history is in docs/COMMON_ERRORS.md; its tests in
# autoloop/tests/test_chrome_restart.py.
#
# Exits NON-ZERO with the message on stderr, deliberately: both callers
# (`cli._repair_browser`, `orchestrator._attempt_browser_restart`) surface
# `result.stderr` only on a non-zero exit, and a recovery command that exits 0
# while restarting nothing is the exact bug recorded against this file.
set -uo pipefail

cat >&2 <<'MSG'
scripts/restart_autoloop_chrome.sh was RETIRED on 2026-08-16 and does nothing.

Set this in your .autoloop/config.toml, under [browser]:

  restart_command = ["python3", "-m", "autoloop.browser.chrome_restart"]

Run the loop from the checkout: `-m` resolves `autoloop` from the working
directory, exactly as this script's relative path did. The module honours the
same AUTOLOOP_CHROME_PROFILE / AUTOLOOP_CHROME_PORT variables and takes
--profile / --port / --chrome.

To restart Chrome by hand right now:

  python3 -m autoloop.browser.chrome_restart
MSG
exit 1
