"""Single-instance locking: atomic acquisition, live-vs-stale detection with
real separate processes, explicit-only recovery, clean-exit release."""

import json
import os
import socket
import subprocess
import sys

import pytest

from autoloop.errors import LockHeldError, StaleLockError
from autoloop.lock import LoopLock

HOLD_LOCK_SCRIPT = """
import sys, time
sys.path.insert(0, {repo_root!r})
from autoloop.lock import LoopLock
lock = LoopLock({state_dir!r})
lock.acquire()
print("HELD", flush=True)
time.sleep(60)
"""


def repo_root() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parents[2])


def test_acquire_release_roundtrip(tmp_path):
    lock = LoopLock(tmp_path)
    with lock:
        assert lock.path.exists()
        info = lock.read()
        assert info.pid == os.getpid()
        assert info.run_id == lock.run_id
    assert not lock.path.exists()


def test_second_acquire_same_process_fails_closed(tmp_path):
    with LoopLock(tmp_path):
        with pytest.raises(LockHeldError):
            LoopLock(tmp_path).acquire()


def test_live_lock_from_separate_process_fails_closed(tmp_path):
    script = HOLD_LOCK_SCRIPT.format(repo_root=repo_root(), state_dir=str(tmp_path))
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "HELD"
        with pytest.raises(LockHeldError) as excinfo:
            LoopLock(tmp_path).acquire()
        assert str(child.pid) in str(excinfo.value)
        # unlock must also refuse a live lock — no silent stealing
        with pytest.raises(LockHeldError):
            LoopLock(tmp_path).break_stale()
    finally:
        child.kill()
        child.wait()


def test_stale_lock_from_dead_process_detected(tmp_path):
    # A real process that has verifiably exited.
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    dead_pid = int(proc.stdout.strip())
    # Write a lock file as if that dead process still held it.
    lock = LoopLock(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    lock.path.write_text(
        json.dumps(
            {
                "pid": dead_pid,
                "hostname": socket.gethostname(),
                "started_at": "2026-07-29T00:00:00+00:00",
                "run_id": "deadbeef",
                "state_dir": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StaleLockError) as excinfo:
        LoopLock(tmp_path).acquire()
    assert "unlock" in str(excinfo.value)
    # explicit recovery removes it; a fresh acquire then succeeds
    info = LoopLock(tmp_path).break_stale()
    assert info.pid == dead_pid
    with LoopLock(tmp_path):
        pass


def test_foreign_host_lock_treated_as_live(tmp_path):
    lock = LoopLock(tmp_path)
    lock.path.parent.mkdir(exist_ok=True)
    lock.path.write_text(
        json.dumps(
            {
                "pid": 1,
                "hostname": "some-other-machine.local",
                "started_at": "t",
                "run_id": "r",
                "state_dir": str(tmp_path),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(LockHeldError):
        LoopLock(tmp_path).acquire()
    with pytest.raises(LockHeldError):
        LoopLock(tmp_path).break_stale()


def test_corrupt_lock_is_stale_but_diagnosable(tmp_path):
    lock = LoopLock(tmp_path)
    lock.path.parent.mkdir(exist_ok=True)
    lock.path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(StaleLockError):
        LoopLock(tmp_path).acquire()
    LoopLock(tmp_path).break_stale()
    assert not lock.path.exists()


def test_release_does_not_remove_someone_elses_lock(tmp_path):
    first = LoopLock(tmp_path)
    first.acquire()
    first.path.unlink()  # simulate: stale-recovered by an operator...
    second = LoopLock(tmp_path)
    second.acquire()  # ...and re-acquired by a new run
    first.release()  # the zombie's release must NOT remove the new lock
    assert second.path.exists()
    assert second.read().run_id == second.run_id
    second.release()


def test_break_stale_with_no_lock_raises(tmp_path):
    with pytest.raises(StaleLockError):
        LoopLock(tmp_path).break_stale()
