"""Single-instance locking for a state directory.

Exactly one autoloop process may operate on a state dir at a time. The lock is
a JSON file created with O_CREAT|O_EXCL (atomic on POSIX); it records pid,
hostname, start time, run id and the state dir, so a crashed run stays
diagnosable.

Semantics — deliberately fail-closed:

* An existing lock whose owner is a LIVE process (same host, pid alive) →
  LockHeldError. Waiting or stopping that process are the only options.
* An existing lock from a DIFFERENT host, or whose pid we cannot probe →
  treated as live (we cannot prove it dead), still LockHeldError.
* An existing lock written BEFORE the machine's current boot → provably
  stale whatever its pid says. Pids are reassigned across a reboot, so a
  lock left behind by a power-off can otherwise name a pid that some
  unrelated process now holds, and `os.kill(pid, 0)` reports it live. That
  turns the documented recovery into a dead end pointing at an innocent
  process. The boot check runs BEFORE the pid probe for exactly that
  reason; when boot time cannot be read the probe still decides, so this
  can only ever declare MORE locks recoverable, never fewer.
* An existing lock whose owner is verifiably dead → StaleLockError, which
  names the explicit recovery command. Locks are NEVER stolen silently —
  recovery is `python -m autoloop unlock`, which itself refuses live locks.
* Clean exit releases the lock (context-manager), and only if we still own it
  (run id match), so `unlock` + a new run can never be un-done by a zombie.

## The one exception: a lock this very process handed to itself

`cli._self_upgrade_at_boundary` replaces the loop's interpreter with
`os.execv` so a merge that changed `autoloop/` actually runs. `execv` REPLACES
the process image — the pid survives, no `finally` runs, so the lock file is
never released and never has to be: from outside, one live pid holds one lock
file across the whole replacement, with no instant at which another process
could take it. That is the point of `execv` rather than spawn-and-exit.

But the new image re-runs `_cmd_run`, which acquires the lock again with a
fresh run id — and would find a lock naming a LIVE pid (its own) and fail
closed. So `acquire` adopts, on evidence and only on evidence:

* the lock must carry an `exec_handoff` marker, written by
  `mark_exec_handoff` immediately before `execv`,
* naming this hostname and THIS pid, twice — as the lock's owner and inside
  the marker,
* and the adoption CLEARS the marker, so it can be used exactly once.

A live lock without that marker is still `LockHeldError`, and so is one whose
marker names a different pid. Both matter: pids are reused, and a general
"same pid may take the lock" rule would be a lock-stealing hole rather than a
handoff. `started_at` is preserved across the adoption because the lock really
has been held continuously since then — which also keeps the predates-boot
check honest.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .errors import LockHeldError, StaleLockError
from .state import utcnow_iso

LOCK_FILENAME = "LOCK"

#: How far a lock's `started_at` must predate boot before we call it stale.
#: `started_at` has one-second resolution and wall clocks get adjusted (NTP
#: steps, a timezone-confused RTC), so a lock written seconds either side of
#: boot is not evidence of anything. Slack buys safety in one direction only:
#: it can leave a genuinely dead lock needing the pid probe, never declare a
#: live one recoverable.
BOOT_CLOCK_SLACK_SECONDS = 120.0


@dataclass(frozen=True)
class LockInfo:
    pid: int
    hostname: str
    started_at: str
    run_id: str
    state_dir: str
    #: Set ONLY between `mark_exec_handoff` and the `os.execv` that follows it
    #: — `{"pid", "run_id", "at", "reason"}`. Its presence is what lets the
    #: replacement image adopt this lock instead of failing closed on its own
    #: live pid; adopting clears it. Absent (the normal state) for every lock
    #: this package has ever written, which is why `read` defaults it rather
    #: than requiring it.
    exec_handoff: dict | None = None

    def describe(self) -> str:
        return (
            f"pid={self.pid} host={self.hostname} started={self.started_at} "
            f"run_id={self.run_id}"
        )


def boot_time_epoch() -> float | None:
    """Unix time of this machine's last boot, or None if it can't be read.

    Deliberately NOT derived from a monotonic clock. On macOS
    `CLOCK_MONOTONIC` stops during sleep, so uptime-minus-now on a laptop
    that has slept reports a boot far more recent than the real one — which
    would make a LIVE lock look like it predates boot. `kern.boottime` and
    `/proc/stat`'s `btime` are the authoritative values on their platforms;
    anywhere else we return None and let the pid probe decide.
    """
    try:
        if sys.platform == "darwin":
            out = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
            # `{ sec = 1754126400, usec = 837291 } Sat Aug  2 ...`
            marker = "sec = "
            start = out.index(marker) + len(marker)
            end = start
            while end < len(out) and out[end].isdigit():
                end += 1
            return float(out[start:end])
        if sys.platform.startswith("linux"):
            for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def _predates_boot(started_at: str) -> bool:
    """True only when `started_at` is provably older than the current boot."""
    boot = boot_time_epoch()
    if boot is None:
        return False
    try:
        started = datetime.fromisoformat(started_at)
    except ValueError:
        return False
    if started.tzinfo is None:
        # A naive stamp cannot be compared to an absolute epoch without
        # guessing a timezone, and guessing wrong here would break a live
        # lock. `utcnow_iso` is always tz-aware; anything else is foreign.
        return False
    return started.timestamp() < boot - BOOT_CLOCK_SLACK_SECONDS


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


class LoopLock:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / LOCK_FILENAME
        self.run_id = uuid.uuid4().hex
        self._owned = False
        self._prev_handlers: list[tuple[int, object]] = []
        #: The run id this lock was ADOPTED from, when `acquire` took over a
        #: lock this same pid handed to itself across `os.execv`. Empty for an
        #: ordinary acquisition, so a caller can report the continuity.
        self.adopted_run_id = ""

    # ---- inspection ---------------------------------------------------------

    def read(self) -> LockInfo | None:
        """Current lock info, or None if no lock file exists. A corrupt lock
        file returns a LockInfo with pid=-1 (never provably dead → held)."""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            handoff = data.get("exec_handoff")
            return LockInfo(
                pid=int(data["pid"]),
                hostname=str(data["hostname"]),
                started_at=str(data["started_at"]),
                run_id=str(data["run_id"]),
                state_dir=str(data["state_dir"]),
                exec_handoff=handoff if isinstance(handoff, dict) else None,
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return LockInfo(
                pid=-1,
                hostname="(corrupt lock file)",
                started_at="?",
                run_id="?",
                state_dir=str(self.state_dir),
            )

    @staticmethod
    def is_live(info: LockInfo) -> bool:
        if info.pid == -1:
            return False  # corrupt file: nothing live can renew it — stale
        if info.hostname != socket.gethostname():
            return True  # cannot verify a foreign host's pid — fail closed
        if _predates_boot(info.started_at):
            # Written before this boot, so its owner did not survive to
            # here. Checked ahead of the probe because the pid it names may
            # since have been handed to an unrelated process.
            return False
        return _pid_alive(info.pid)

    # ---- lifecycle ----------------------------------------------------------

    # ---- termination handling ----------------------------------------------
    #
    # Ctrl-C already unwinds cleanly: KeyboardInterrupt runs `__exit__`.
    # Python's default action for SIGTERM and SIGHUP is to die WITHOUT running
    # `finally`, so the orderly-looking ways to stop (a shutdown, a logout,
    # plain `kill`) were the ones that left a lock behind. This lives on the
    # lock rather than at one call site because every holder needs it — `run`
    # is the long one, but `smoke-browser` drives a real browser and
    # `review-changeset` waits on a reviewer, and a per-command wrapper would
    # protect whichever ones somebody remembered.
    #
    # The release happens INSIDE the handler, before unwinding, and that is
    # the point rather than a detail: a SIGTERM mid-fan-out unwinds into
    # `ThreadPoolExecutor.shutdown(wait=True)`, which waits on agents that run
    # for minutes, while a shutdown's grace period is seconds. Nothing else is
    # written here — state is consistent at every instant (atomic saves), and
    # touching the pause flag would silently gag the NEXT run.

    def _install_termination_handlers(self) -> None:
        def _terminate(signum, _frame):
            self.release()
            raise SystemExit(128 + signum)

        for name in ("SIGTERM", "SIGHUP"):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                self._prev_handlers.append((signum, signal.signal(signum, _terminate)))
            except ValueError:
                # Not the main thread (embedded or test use). The caller's own
                # cleanup still applies; we simply cannot add to it here.
                pass

    def _restore_termination_handlers(self) -> None:
        while self._prev_handlers:
            signum, previous = self._prev_handlers.pop()
            try:
                signal.signal(signum, previous)
            except ValueError:
                pass

    def acquire(self) -> "LoopLock":
        self.state_dir.mkdir(parents=True, exist_ok=True)
        info = LockInfo(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            started_at=utcnow_iso(),
            run_id=self.run_id,
            state_dir=str(self.state_dir),
        )
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = self.read()
            if existing is None:  # raced with a release; one retry
                return self.acquire()
            if self._is_handoff_to_this_process(existing):
                return self._adopt(existing)
            if self.is_live(existing):
                raise LockHeldError(
                    f"another autoloop process holds {self.path} "
                    f"({existing.describe()}). Wait for it or stop it — "
                    "locks are never stolen."
                ) from None
            raise StaleLockError(
                f"stale lock at {self.path} ({existing.describe()}) — its owner "
                "is no longer running. Inspect it, then recover with: "
                "python -m autoloop unlock"
            ) from None
        try:
            os.write(fd, json.dumps(asdict(info), indent=2).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._owned = True
        self._install_termination_handlers()
        return self

    # ---- self-upgrade handoff (`os.execv`, same pid) ------------------------

    def _write(self, info: LockInfo) -> None:
        """Replace the lock file's contents in one step — temp file in the same
        directory, fsync, `os.replace`.

        Never unlink-then-recreate. An unlink would open a window in which the
        lock does not exist, which is exactly the window "the lock stays valid
        across the replacement" denies; `os.replace` is atomic on POSIX, so a
        concurrent `read()` sees the old bytes or the new ones and never
        nothing.
        """
        tmp = self.path.with_name(self.path.name + f".tmp.{os.getpid()}")
        fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            os.write(fd, json.dumps(asdict(info), indent=2).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self.path)

    def _is_handoff_to_this_process(self, info: LockInfo) -> bool:
        """Is this lock one THIS process handed to itself across `os.execv`?

        Three independent facts, all required: the marker exists, the lock is
        ours by host and pid, and the marker names the same pid. Asked this
        narrowly because the weaker rule — "the lock's pid is my pid, so it is
        mine" — is a lock-stealing hole: pids are reused within a boot, so a
        dead run's lock plus an unlucky pid assignment would let an unrelated
        process walk past a refusal the rest of this module exists to make.
        """
        handoff = info.exec_handoff
        if not isinstance(handoff, dict):
            return False
        if info.hostname != socket.gethostname():
            return False
        me = os.getpid()
        return info.pid == me and handoff.get("pid") == me

    def _adopt(self, existing: LockInfo) -> "LoopLock":
        """Take over the lock this process left for itself, under a new run id.

        `started_at` is carried over: the lock genuinely has been held since
        then (same pid, no gap), and rewriting it to now would misreport how
        long this state dir has been locked and weaken the predates-boot check.
        The marker is dropped, so the adoption cannot happen twice.
        """
        self._write(
            LockInfo(
                pid=os.getpid(),
                hostname=existing.hostname,
                started_at=existing.started_at,
                run_id=self.run_id,
                state_dir=str(self.state_dir),
            )
        )
        self.adopted_run_id = existing.run_id
        self._owned = True
        self._install_termination_handlers()
        return self

    def mark_exec_handoff(self, reason: str = "") -> bool:
        """Arm the lock for an `os.execv` in THIS process. Returns whether it
        was armed.

        Called immediately before the exec and nowhere else. Refuses unless we
        own the lock and the file still names our run — the caller treats a
        `False` here as "do not replace the process", because a replacement
        whose successor cannot acquire the lock would end the run.
        """
        if not self._owned:
            return False
        current = self.read()
        if current is None or current.run_id != self.run_id:
            return False
        try:
            self._write(
                LockInfo(
                    pid=current.pid,
                    hostname=current.hostname,
                    started_at=current.started_at,
                    run_id=current.run_id,
                    state_dir=current.state_dir,
                    exec_handoff={
                        "pid": os.getpid(),
                        "run_id": self.run_id,
                        "at": utcnow_iso(),
                        "reason": reason,
                    },
                )
            )
        except OSError:
            return False
        return True

    def clear_exec_handoff(self) -> None:
        """Disarm it again — the exec did not happen (`os.execv` itself
        refused). Best effort: a marker left behind is harmless (only this pid
        could ever use it, and this pid already owns the lock), so a failure
        here must not propagate into a run that is otherwise fine."""
        if not self._owned:
            return
        current = self.read()
        if current is None or current.run_id != self.run_id:
            return
        try:
            self._write(
                LockInfo(
                    pid=current.pid,
                    hostname=current.hostname,
                    started_at=current.started_at,
                    run_id=current.run_id,
                    state_dir=current.state_dir,
                )
            )
        except OSError:
            pass

    def release(self) -> None:
        if not self._owned:
            return
        current = self.read()
        if current is not None and current.run_id == self.run_id:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._owned = False
        self._restore_termination_handlers()

    def __enter__(self) -> "LoopLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    # ---- explicit recovery (the `unlock` command) ---------------------------

    def break_stale(self) -> LockInfo:
        """Remove a verifiably-stale lock. Refuses live locks — there is no
        force path past a running process."""
        info = self.read()
        if info is None:
            raise StaleLockError(f"no lock file at {self.path} — nothing to recover")
        if self.is_live(info):
            raise LockHeldError(
                f"refusing to remove a LIVE lock ({info.describe()}). "
                "Stop that process instead."
            )
        self.path.unlink()
        return info
