"""Restart the dedicated autoloop Chrome profile — every instance of it, and
nothing else.

    python3 -m autoloop.browser.chrome_restart [--profile P] [--port N]

Python, not shell: the post-commit validation runner allows only
ruff/pytest/python/npm/npx/tsc, so a `.sh` helper cannot be validated at all.

Three rules, each earned by the predecessor stopping ONE pid and relaunching
into a survivor that still owned the port: stop EVERY process carrying
`--user-data-dir=<profile>`; poll (bounded) until nothing holds the port, since
a kill is not proof it was freed; confirm the endpoint answers before reporting
success, because owning the port is the success condition.

THE SAFETY BOUND: match the profile path EXACTLY, never the binary name — the
operator's everyday browser runs from that same binary under a different
`--user-data-dir`, and killing it would end their session. Every listing,
signal, probe and launch goes through `ChromeOps`, a REQUIRED argument of
`restart()`, so no test can inherit a live default.
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

#: Defaults mirror the env vars the shell helper honoured. Wiring this into
#: `browser.restart_command` is brw-08's job; nothing here reads the config.
DEFAULT_PROFILE = "~/.autoloop-chrome"
DEFAULT_PORT = 9222
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

#: Only the `=` form: a space-separated one would mean guessing where a path
#: with spaces ends, and a wrong guess here costs the operator's session.
PROFILE_FLAG = "--user-data-dir="


def profile_values(command: str) -> list[str]:
    """Every `--user-data-dir=` value in one command line, unnormalized. A value
    runs to the next ` --` or to end of line, so a profile containing spaces is
    read whole rather than cut at the first one."""
    values: list[str] = []
    index = command.find(PROFILE_FLAG)
    while index != -1:
        rest = command[index + len(PROFILE_FLAG):]
        end = rest.find(" --")
        values.append((rest if end == -1 else rest[:end]).strip())
        index = command.find(PROFILE_FLAG, index + 1)
    return values


def normalize_profile(path: str) -> str:
    """`~`-expanded, `.`-resolved, trailing-slash-free — "" if empty. Textual, so
    a symlink cannot make two different profiles compare equal."""
    expanded = os.path.expanduser(path.strip())
    return os.path.normpath(expanded) if expanded else ""


def matches_profile(command: str, profile: str) -> bool:
    """True only when `command` runs on EXACTLY this profile — the safety bound."""
    target = normalize_profile(profile)
    if not target:
        return False
    return any(normalize_profile(value) == target for value in profile_values(command))


def endpoint_body_is_ready(body: str) -> bool:
    """True only for a `/json/version` payload carrying a browser websocket URL:
    a 200 alone would also describe a browser that cannot be driven."""
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and bool(payload.get("webSocketDebuggerUrl"))


@dataclass(frozen=True)
class ChromeOps:
    """Everything this module does to the machine, plus a clock. `terminate` may
    raise ProcessLookupError if the pid already exited."""

    list_processes: Callable[[], list[tuple[int, str]]]
    terminate: Callable[[int], None]
    port_in_use: Callable[[int], bool]
    endpoint_ready: Callable[[int], bool]
    launch: Callable[[str, int], None]
    sleep: Callable[[float], None]

    @classmethod
    def real(cls, chrome_binary: str = DEFAULT_CHROME) -> "ChromeOps":
        """The live set. Built only by `main()`."""
        return cls(
            list_processes=_default_list_processes,
            terminate=_default_terminate,
            port_in_use=_default_port_in_use,
            endpoint_ready=_default_endpoint_ready,
            launch=lambda profile, port: _default_launch(chrome_binary, profile, port),
            sleep=time.sleep,
        )


def _default_list_processes() -> list[tuple[int, str]]:
    """(pid, command line) per process, via the `ps` call the shell helper used."""
    proc = subprocess.run(
        ["ps", "-eo", "pid,command"], capture_output=True, text=True, timeout=30, check=False
    )
    processes: list[tuple[int, str]] = []
    for line in proc.stdout.splitlines():
        head, _, command = line.strip().partition(" ")
        if head.isdigit() and command.strip():  # skips the header and junk lines
            processes.append((int(head), command.strip()))
    return processes


def _default_terminate(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def _default_port_in_use(port: int, timeout: float = 0.5) -> bool:
    """A connect probe, not a bind probe, which would race the browser we are
    about to start. Only an explicit refusal counts as free."""
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
        # The browser must outlive us: the loop reconnects from another process.
        start_new_session=True,
    )


@dataclass(frozen=True)
class RestartResult:
    ok: bool
    detail: str
    #: Matched and signalled — NOT a claim each exited. A survivor makes `ok` False.
    matched_pids: tuple[int, ...] = ()
    launched: bool = False


def _matching(ops: ChromeOps, profile: str) -> list[tuple[int, str]]:
    return [(pid, cmd) for pid, cmd in ops.list_processes() if matches_profile(cmd, profile)]


def _poll(check: Callable[[], bool], attempts: int, ops: ChromeOps, interval: float) -> bool:
    """True as soon as `check()` holds, False once `attempts` probes have failed.
    Bounded by a probe COUNT, not a deadline, so a test needs no faked clock."""
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

    Never raises for an ordinary failure: the caller is already handling a
    browser fault, and a second exception on top of the first turns recovery into
    a crash. The bounds total ≈123s, sized under the 180s `subprocess.run`
    timeout both callers wrap a restart command in (`cli._repair_browser`,
    `orchestrator._attempt_browser_restart`) — raise them together, never alone.
    """
    target = normalize_profile(profile)
    if not target or not os.path.isabs(target) or target == os.sep:
        # Empty, relative or root matches nothing or far too much, and a relative
        # path would resolve against Chrome's cwd rather than ours.
        return RestartResult(False, f"refusing: {profile!r} is not a usable profile directory")

    matches = _matching(ops, target)
    matched = tuple(pid for pid, _ in matches)
    for pid, _command in matches:
        log(f"stopping pid {pid} on profile {target}")
        try:
            ops.terminate(pid)
        except ProcessLookupError:
            continue  # Exited between listing and signal — the outcome we wanted.
        except OSError as exc:
            return RestartResult(False, f"cannot signal pid {pid}: {exc}", matched)

    if matches and not _poll(lambda: not _matching(ops, target), stop_attempts, ops, poll_interval):
        survivors = ", ".join(str(pid) for pid, _ in _matching(ops, target))
        return RestartResult(
            False,
            f"refusing to launch: pid(s) {survivors} on {target} did not exit "
            f"within {stop_attempts} probes",
            matched,
        )

    # Checked independently of the kill, which is not evidence: a process on
    # another profile, or a socket not yet reaped, holds the port just as well.
    if not _poll(lambda: not ops.port_in_use(port), port_attempts, ops, poll_interval):
        return RestartResult(
            False,
            f"refusing to launch: port {port} is still held after {port_attempts} "
            f"probes — a second instance could not bind it",
            matched,
        )

    log(f"launching chrome on profile {target}, debug port {port}")
    try:
        # The NORMALIZED profile, so this same matcher finds what we start.
        ops.launch(target, port)
    except OSError as exc:
        return RestartResult(False, f"cannot launch chrome: {exc}", matched)

    if not _poll(lambda: ops.endpoint_ready(port), ready_attempts, ops, poll_interval):
        return RestartResult(
            False,
            f"chrome was launched but did not answer on port {port} within {ready_attempts} probes",
            matched,
            launched=True,
        )
    return RestartResult(
        True,
        f"autoloop chrome up on port {port} (stopped {len(matched)} instance(s) on {target})",
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
        args.profile, args.port, ChromeOps.real(args.chrome), log=lambda m: print(m, flush=True)
    )
    if not result.ok:
        # stderr: both callers surface `result.stderr` on a non-zero exit, so a
        # diagnosis on stdout reaches the operator as "restart FAILED:" and then
        # nothing.
        print(result.detail, file=sys.stderr, flush=True)
        return 1
    print(result.detail, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
