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
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .errors import LockHeldError, StaleLockError
from .state import utcnow_iso

LOCK_FILENAME = "LOCK"


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
