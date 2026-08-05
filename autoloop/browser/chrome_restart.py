"""Restart the dedicated autoloop Chrome profile — EVERY instance of it, and
nothing else.

    python3 -m autoloop.browser.chrome_restart [--profile P] [--port N]

Why this is Python rather than a shell script: the post-commit validation
runner allows only ruff/pytest/python/npm/npx/tsc, so a `.sh` helper cannot be
validated at all (`bash -n` is refused as an unsafe binary). Everything here is
driven by pytest instead, with every external effect injected.

WHAT WENT WRONG WITHOUT IT. The predecessor stopped ONE process by pid and
relaunched. With two Chromes on the same profile it stopped the wrong one and
the survivor kept the debug port, so the replacement could never bind it.
Observed 2026-08-04: the helper reported stopping pid 90888 while pid 94120
still owned 9222; the loop's next connects reached a browser whose devtools
target no longer matched, the websocket CONNECTED and then hung for the full
180s timeout, four times over, until the failure budget was spent. The tell is
a connected websocket followed by a timeout — a genuinely absent endpoint
fails earlier, while retrieving the websocket URL.

So the three rules this module exists to enforce:

  1. Stop every process whose command line carries `--user-data-dir=<profile>`,
     not the first pid that matches.
  2. Poll until nothing holds the debug port before relaunching, bounded.
     A kill is not proof the port was freed.
  3. Confirm the debug endpoint answers before reporting success. Owning the
     port is the success condition; a zero exit from a launcher is not.

THE SAFETY BOUND: match the profile path EXACTLY, never the binary name. The
operator's everyday browser runs from the very same binary with a different
`--user-data-dir`, and killing it would end their session mid-work. Comparison
is on the normalized `--user-data-dir` VALUE, so neither a substring
(`…/.autoloop-chrome-old`) nor a sibling profile can be caught by accident.
Verified by hand 2026-08-05: exactly one process matched the loop profile while
the operator's Chrome, a separate pid, was untouched.

Every process listing, signal, port probe and launch goes through `ChromeOps`,
which `restart()` takes as a REQUIRED argument — there is no live default to
forget to override, so no test can kill a real process, bind a real port, or
start a real browser.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable

#: Defaults mirror the env vars the shell helper already honoured, so the
#: operator's existing overrides keep working. Wiring this into
#: `browser.restart_command` is brw-08's job; nothing here reads the config.
DEFAULT_PROFILE = "~/.autoloop-chrome"
DEFAULT_PORT = 9222
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

#: Only the `=` form. Chrome's own docs use it, the profile is written that way
#: everywhere in this repo, and accepting a space-separated form would mean
#: guessing where a path with spaces ends — a wrong guess on the safety bound
#: is the operator's session.
PROFILE_FLAG = "--user-data-dir="


# ---- pure helpers (no process, no port, no network) --------------------------


def profile_values(command: str) -> list[str]:
    """Every `--user-data-dir=` value in one command line, unnormalized.

    A value runs to the next ` --` or to the end of the line, so a profile
    containing spaces (`~/Library/Application Support/…`, which is where the
    operator's own Chrome lives) is read whole rather than truncated at the
    first space.
    """
    values: list[str] = []
    index = command.find(PROFILE_FLAG)
    while index != -1:
        rest = command[index + len(PROFILE_FLAG):]
        end = rest.find(" --")
        values.append((rest if end == -1 else rest[:end]).strip())
        index = command.find(PROFILE_FLAG, index + 1)
    return values


def normalize_profile(path: str) -> str:
    """`~`-expanded, `.`/`..`-resolved, trailing-slash-free — or "" if empty.

    Deliberately textual: no `realpath`, which would touch the filesystem and
    could make two different profiles compare equal through a symlink.
    """
    expanded = os.path.expanduser(path.strip())
    return os.path.normpath(expanded) if expanded else ""


def matches_profile(command: str, profile: str) -> bool:
    """True only when `command` runs on EXACTLY this profile.

    The whole safety bound is this one comparison. It is on the flag's value,
    never on the binary: `…/Google Chrome --user-data-dir=<anything else>` is
    the operator's browser and must survive.
    """
    target = normalize_profile(profile)
    if not target:
        return False
    return any(normalize_profile(value) == target for value in profile_values(command))


def endpoint_body_is_ready(body: str) -> bool:
    """True only for a `/json/version` payload carrying a browser websocket URL.

    Split out from the HTTP call so the "is it really up?" rule is a pure
    string→bool function with a test of its own. A 200 alone is too weak: the
    fault being recovered from is a browser that answers and cannot be driven,
    and `webSocketDebuggerUrl` is the exact field the loop needs next.
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload.get("webSocketDebuggerUrl"))


# ---- injected boundary -------------------------------------------------------


@dataclass(frozen=True)
class ChromeOps:
    """The four things this module does to the machine, plus a clock.

    `restart()` takes one of these as a required positional argument. Building
    the live set is `ChromeOps.real()`, called only from `main()`.
    """

    #: (pid, command line) for every process on the machine.
    list_processes: Callable[[], list[tuple[int, str]]]
    #: Ask one pid to exit. May raise ProcessLookupError if it already has.
    terminate: Callable[[int], None]
    #: True when anything holds the debug port.
    port_in_use: Callable[[int], bool]
    #: True when the debug endpoint answers usefully.
    endpoint_ready: Callable[[int], bool]
    #: Start Chrome on (profile, port). Returns as soon as it is spawned.
    launch: Callable[[str, int], None]
    sleep: Callable[[float], None]

    @classmethod
    def real(cls, chrome_binary: str = DEFAULT_CHROME) -> "ChromeOps":
        return cls(
            list_processes=_default_list_processes,
            terminate=_default_terminate,
            port_in_use=_default_port_in_use,
            endpoint_ready=_default_endpoint_ready,
            launch=lambda profile, port: _default_launch(chrome_binary, profile, port),
            sleep=time.sleep,
        )


def _default_list_processes() -> list[tuple[int, str]]:
    """Every process as (pid, command line), via `ps -eo pid,command`.

    Same invocation the shell helper used against the operator's Mac, so the
    output shape is known-good there. Unparseable lines (the header, anything
    without a command) are skipped rather than guessed at.
    """
    result = subprocess.run(
        ["ps", "-eo", "pid,command"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    processes: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        head, _, command = line.strip().partition(" ")
        command = command.strip()
        if not command:
            continue
        try:
            pid = int(head)
        except ValueError:
            continue
        processes.append((pid, command))
    return processes


def _default_terminate(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def _default_port_in_use(port: int, timeout: float = 0.5) -> bool:
    """True when something accepts a loopback connection on `port`.

    A connect probe, not a bind probe: binding would race the browser we are
    about to start for the very port it needs.

    Only an explicit refusal counts as free. Anything else — a timeout, an
    unreachable stack — is not PROOF the port is free, and the expensive
    mistake here is launching a second Chrome that then cannot bind.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except ConnectionRefusedError:
        return False
    except OSError:
        return True


def _default_endpoint_ready(port: int, timeout: float = 2.0) -> bool:
    url = f"http://127.0.0.1:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local CDP
            if response.status != 200:
                return False
            body = response.read(4096).decode("utf-8", "replace")
    except (OSError, ValueError):
        return False
    return endpoint_body_is_ready(body)


def _default_launch(binary: str, profile: str, port: int) -> None:
    if not os.access(binary, os.X_OK):
        raise OSError(f"chrome is not executable at {binary}")
    subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [binary, f"--user-data-dir={profile}", f"--remote-debugging-port={port}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # The browser has to outlive this process — the loop restarts it and
        # then reconnects from a different process entirely.
        start_new_session=True,
    )


# ---- the restart itself ------------------------------------------------------


@dataclass(frozen=True)
class RestartResult:
    ok: bool
    detail: str
    #: Every pid that matched the profile and was signalled — NOT a claim that
    #: each one exited. A survivor is what makes `ok` False, and the detail
    #: names it.
    matched_pids: tuple[int, ...] = ()
    launched: bool = False


def _matching(ops: ChromeOps, profile: str) -> list[tuple[int, str]]:
    return [(pid, cmd) for pid, cmd in ops.list_processes() if matches_profile(cmd, profile)]


def _poll(check: Callable[[], bool], attempts: int, ops: ChromeOps, interval: float) -> bool:
    """True as soon as `check()` holds; False once `attempts` probes have failed.

    Bounded by a COUNT of probes rather than a wall-clock deadline, so "how
    long does it wait" is a countable assertion in a test and needs no faked
    clock. Probes first and sleeps between, so an already-true condition costs
    no delay.
    """
    for attempt in range(attempts):
        if check():
            return True
        if attempt + 1 < attempts:
            ops.sleep(interval)
    return False


def restart(
    profile: str,
    port: int,
    ops: ChromeOps,
    *,
    stop_attempts: int = 20,
    port_attempts: int = 20,
    ready_attempts: int = 25,
    poll_interval: float = 1.0,
    log: Callable[[str], None] = lambda _message: None,
) -> RestartResult:
    """Stop every Chrome on `profile`, wait for `port`, relaunch, prove it.

    Never raises for an ordinary failure: the caller is a loop already handling
    a browser fault, and a second exception on top of the first one is how a
    recovery path becomes a crash. Failures come back as `ok=False` with a
    detail the operator can act on.

    The default bounds are sized against the 180s `subprocess.run` timeout both
    callers wrap a restart command in (`cli._repair_browser`,
    `orchestrator._attempt_browser_restart`). Worst case is roughly
    20s (stop: 20 probes, 19 waits) + 29s (port: 20 probes at 0.5s, 19 waits) +
    74s (ready: 25 probes at 2s, 24 waits) ≈ 123s, leaving ~57s of margin.
    Overrunning that budget would turn a recovery into an uncaught
    TimeoutExpired, so raise these together with that timeout, not alone.
    """
    target = normalize_profile(profile)
    if not target or not os.path.isabs(target) or target == os.sep:
        # An empty, relative or root profile would either match nothing or
        # match far too much, and a relative path is meaningless anyway — it
        # would resolve against Chrome's cwd, not ours.
        return RestartResult(False, f"refusing: {profile!r} is not a usable profile directory")

    matches = _matching(ops, target)
    matched = tuple(pid for pid, _ in matches)
    for pid, _command in matches:
        log(f"stopping pid {pid} on profile {target}")
        try:
            ops.terminate(pid)
        except ProcessLookupError:
            # It exited between the listing and the signal. That is the
            # outcome we wanted.
            continue
        except OSError as exc:
            return RestartResult(False, f"cannot signal pid {pid}: {exc}", matched)

    if matches and not _poll(
        lambda: not _matching(ops, target), stop_attempts, ops, poll_interval
    ):
        survivors = ", ".join(str(pid) for pid, _ in _matching(ops, target))
        return RestartResult(
            False,
            f"refusing to launch: pid(s) {survivors} on profile {target} "
            f"did not exit within {stop_attempts} probes",
            matched,
        )

    # Independent of the kill, because the kill is not evidence. A survivor on
    # another profile, or a socket not yet reaped, holds the port just as well
    # — and launching into that is precisely the bug: a second Chrome that
    # cannot bind the port, next to one whose devtools target the loop no
    # longer matches.
    if not _poll(lambda: not ops.port_in_use(port), port_attempts, ops, poll_interval):
        return RestartResult(
            False,
            f"refusing to launch: port {port} is still held after {port_attempts} probes "
            f"— a second instance could not bind it",
            matched,
        )

    log(f"launching chrome on profile {target}, debug port {port}")
    try:
        # The NORMALIZED profile, so the instance we start is one this same
        # matcher will find next time.
        ops.launch(target, port)
    except OSError as exc:
        return RestartResult(False, f"cannot launch chrome: {exc}", matched)

    if not _poll(lambda: ops.endpoint_ready(port), ready_attempts, ops, poll_interval):
        return RestartResult(
            False,
            f"chrome was launched but did not answer on port {port} within "
            f"{ready_attempts} probes",
            matched,
            launched=True,
        )
    return RestartResult(
        True,
        f"autoloop chrome up on port {port} (stopped {len(matched)} instance(s) "
        f"on {target})",
        matched,
        launched=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m autoloop.browser.chrome_restart",
        description="Restart every Chrome on the dedicated autoloop profile, and nothing else.",
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("AUTOLOOP_CHROME_PROFILE", DEFAULT_PROFILE),
        help="the --user-data-dir to match on and relaunch with",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("AUTOLOOP_CHROME_PORT", str(DEFAULT_PORT)),
        help="remote debugging port that must be free, then owned",
    )
    parser.add_argument(
        "--chrome",
        default=os.environ.get("AUTOLOOP_CHROME_BINARY", DEFAULT_CHROME),
        help="path to the Chrome binary",
    )
    args = parser.parse_args(argv)

    result = restart(
        args.profile,
        args.port,
        ChromeOps.real(args.chrome),
        log=lambda message: print(message, flush=True),
    )
    if not result.ok:
        # stderr, because that is what reports it: `cli._repair_browser` and
        # `orchestrator._attempt_browser_restart` both surface
        # `result.stderr` on a non-zero exit. A diagnosis printed to stdout
        # reaches the operator as "restart FAILED:" with nothing after it.
        print(result.detail, file=sys.stderr, flush=True)
        return 1
    print(result.detail, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
