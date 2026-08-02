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
"""

from __future__ import annotations

import json
import os
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

    # ---- inspection ---------------------------------------------------------

    def read(self) -> LockInfo | None:
        """Current lock info, or None if no lock file exists. A corrupt lock
        file returns a LockInfo with pid=-1 (never provably dead → held)."""
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return LockInfo(
                pid=int(data["pid"]),
                hostname=str(data["hostname"]),
                started_at=str(data["started_at"]),
                run_id=str(data["run_id"]),
                state_dir=str(data["state_dir"]),
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
        return self

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
