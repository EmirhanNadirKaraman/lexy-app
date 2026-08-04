"""What survives a machine going away mid-run.

Three failures that only show up when the process does not get to unwind:
a lock whose pid was reassigned by a reboot, a state file whose rename
outlived its data, and a SIGTERM that skipped the release.
"""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from autoloop import lock as lock_module
from autoloop.errors import LockHeldError
from autoloop.lock import LoopLock, boot_time_epoch
from autoloop.state import LoopState, StateStore


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _a_state() -> LoopState:
    return LoopState(session_id="crash-safety", conversation_url="https://example.invalid/c/1")


def _write_lock(state_dir: Path, *, pid: int, started_at: str) -> None:
    (state_dir / "LOCK").write_text(
        json.dumps(
            {
                "pid": pid,
                "hostname": __import__("socket").gethostname(),
                "started_at": started_at,
                "run_id": "run-under-test",
                "state_dir": str(state_dir),
            }
        ),
        encoding="utf-8",
    )


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


# --- pid reuse across a reboot ------------------------------------------------


def test_lock_written_before_boot_is_stale_even_when_its_pid_is_alive(tmp_path, monkeypatch):
    """The reboot case. A power-off leaves the lock naming a pid that the next
    boot may hand to something else; probing it reports "live" and sends the
    operator to stop an innocent process."""
    boot = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(lock_module, "boot_time_epoch", lambda: boot.timestamp())
    # os.getpid() is unquestionably alive — this is the reused-pid stand-in.
    _write_lock(tmp_path, pid=os.getpid(), started_at=_iso(boot - timedelta(hours=2)))

    lock = LoopLock(tmp_path)
    assert LoopLock.is_live(lock.read()) is False
    assert lock.break_stale().run_id == "run-under-test"
    assert not lock.path.exists()


def test_lock_written_after_boot_still_defers_to_the_pid_probe(tmp_path, monkeypatch):
    boot = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(lock_module, "boot_time_epoch", lambda: boot.timestamp())
    _write_lock(tmp_path, pid=os.getpid(), started_at=_iso(datetime.now(timezone.utc)))

    lock = LoopLock(tmp_path)
    assert LoopLock.is_live(lock.read()) is True
    with pytest.raises(LockHeldError):
        lock.break_stale()


def test_boot_check_never_breaks_a_lock_it_cannot_date(tmp_path, monkeypatch):
    """Unreadable boot time, unparseable stamp, and a naive stamp all fall
    back to the pid probe rather than guessing."""
    lock = LoopLock(tmp_path)

    monkeypatch.setattr(lock_module, "boot_time_epoch", lambda: None)
    _write_lock(tmp_path, pid=os.getpid(), started_at=_iso(datetime.now(timezone.utc) - timedelta(days=9)))
    assert LoopLock.is_live(lock.read()) is True

    boot = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(lock_module, "boot_time_epoch", lambda: boot.timestamp())

    _write_lock(tmp_path, pid=os.getpid(), started_at="not-a-timestamp")
    assert LoopLock.is_live(lock.read()) is True

    # Naive: comparable to an epoch only by guessing a zone, and guessing
    # wrong here would break a live lock.
    _write_lock(tmp_path, pid=os.getpid(), started_at="2020-01-01T00:00:00")
    assert LoopLock.is_live(lock.read()) is True


def test_clock_slack_absorbs_a_stamp_at_the_boot_boundary(tmp_path, monkeypatch):
    """One-second stamps and NTP steps mean "just before boot" is not
    evidence. Slack must resolve toward live, never toward recoverable."""
    boot = datetime.now(timezone.utc)
    monkeypatch.setattr(lock_module, "boot_time_epoch", lambda: boot.timestamp())
    within = timedelta(seconds=lock_module.BOOT_CLOCK_SLACK_SECONDS / 2)
    _write_lock(tmp_path, pid=os.getpid(), started_at=_iso(boot - within))

    assert LoopLock.is_live(LoopLock(tmp_path).read()) is True


def test_boot_time_is_real_and_in_the_past_on_this_platform():
    boot = boot_time_epoch()
    if boot is None:
        pytest.skip(f"no boot-time source on {sys.platform}")
    now = datetime.now(timezone.utc).timestamp()
    assert 0 < boot < now
    assert now - boot < 365 * 24 * 3600  # sanity: not an epoch-zero misparse


def test_a_dead_pid_is_still_stale_without_any_boot_evidence(tmp_path, monkeypatch):
    """The pre-existing path stays exactly as it was."""
    monkeypatch.setattr(lock_module, "boot_time_epoch", lambda: None)
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    _write_lock(tmp_path, pid=proc.pid, started_at=_iso(datetime.now(timezone.utc)))
    assert LoopLock.is_live(LoopLock(tmp_path).read()) is False


# --- state durability ---------------------------------------------------------


def test_state_save_fsyncs_data_before_publishing_the_rename(tmp_path, monkeypatch):
    """A power cut can land the rename without the blocks behind it. Assert
    the ordering that prevents it: the file's own fsync precedes os.replace."""
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def spy_fsync(fd):
        try:
            mode = os.fstat(fd).st_mode
        except OSError:  # pragma: no cover - defensive
            mode = 0
        import stat

        events.append("fsync_dir" if stat.S_ISDIR(mode) else "fsync_file")
        return real_fsync(fd)

    def spy_replace(src, dst):
        events.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    monkeypatch.setattr(os, "replace", spy_replace)

    StateStore(tmp_path / "state.json").save(_a_state())

    assert "fsync_file" in events, f"data never fsynced: {events}"
    assert events.index("fsync_file") < events.index("replace"), events
    # Directory fsync is what makes the rename itself durable; it is
    # best-effort, so only its ordering is pinned when present.
    if "fsync_dir" in events:
        assert events.index("replace") < events.index("fsync_dir"), events


def test_state_save_survives_a_filesystem_that_refuses_directory_fsync(tmp_path, monkeypatch):
    """Some network mounts cannot fsync a directory. A save that otherwise
    succeeded must not fail over that."""
    import stat as stat_mod

    real_fsync = os.fsync

    def picky_fsync(fd):
        if stat_mod.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory fsync unsupported")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", picky_fsync)
    store = StateStore(tmp_path / "state.json")
    store.save(_a_state())
    assert store.load() is not None


def test_state_save_leaves_no_temp_file_behind(tmp_path):
    store = StateStore(tmp_path / "state.json")
    store.save(_a_state())
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


# --- SIGTERM releases the lock ------------------------------------------------

SIGNAL_RUN_SCRIPT = """
import sys, time
sys.path.insert(0, {repo_root!r})
from autoloop.lock import LoopLock

lock = LoopLock({state_dir!r})
try:
    with lock:
        print("HELD", flush=True)
        time.sleep(60)
except SystemExit as exc:
    print("EXIT", exc.code, flush=True)
"""


def _spawn_holder(state_dir: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            SIGNAL_RUN_SCRIPT.format(repo_root=str(_repo_root()), state_dir=str(state_dir)),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "HELD"
    return proc


@contextlib.contextmanager
def holder(state_dir: Path):
    """A lock-holding subprocess that is always reaped.

    Without the `finally`, a test that fails leaves a child holding a lock
    and sleeping for a minute — which then competes for CPU with everything
    after it. A flaky test that leaks processes makes its neighbours flaky
    too, which is the harder failure to trace back.
    """
    proc = _spawn_holder(state_dir)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - kill is reliable
            pass
        if proc.stdout is not None:
            proc.stdout.close()


def _await_lock_release(lock_path: Path, timeout: float = 60.0) -> bool:
    """Poll for the lock to disappear.

    Deliberately NOT `proc.wait(timeout=...)` followed by an instant check.
    That asserts something stricter than the guarantee: it requires the whole
    interpreter to have finished shutting down by a fixed deadline, and on a
    loaded machine it does not. Caught by this suite's own flake hunt —
    `proc.wait(timeout=30)` raised TimeoutExpired inside a full-suite run
    while passing 25/25 in isolation, and it refused three of the loop's
    commits before it was tracked down.

    The property under test is that the lock is RELEASED, and the release
    happens before the process exits, so polling the artefact tests the real
    behaviour with no timing assumption about interpreter teardown.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not lock_path.exists():
            return True
        time.sleep(0.05)
    return not lock_path.exists()


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP"])
def test_termination_signal_releases_the_lock(tmp_path, signame):
    """Without a handler, Python's default action for both signals is to die
    without unwinding — so the ordinary way to stop a run left a lock that
    the next start refused to take."""
    lock_path = tmp_path / "LOCK"
    with holder(tmp_path) as proc:
        assert lock_path.exists()
        proc.send_signal(getattr(signal, signame))
        assert _await_lock_release(lock_path), f"{signame} left a lock behind"

    # And the next run can start without operator recovery.
    with LoopLock(tmp_path):
        pass


# A holder whose cleanup outlives any shutdown grace period. Unwinding from
# the handler's SystemExit reaches this `finally` and stops there, so the
# lock's own context-manager exit is never run — only a release performed
# INSIDE the handler can have happened. Mirrors the real shape: a SIGTERM
# arriving mid-fan-out unwinds into ThreadPoolExecutor.shutdown(wait=True),
# which waits for agents that run for minutes.
BLOCKED_CLEANUP_SCRIPT = """
import sys, time
sys.path.insert(0, {repo_root!r})
from autoloop.lock import LoopLock

with LoopLock({state_dir!r}):
    try:
        print("HELD", flush=True)
        time.sleep(120)
    finally:
        time.sleep(120)
"""


def test_lock_is_released_before_unwinding_not_by_it(tmp_path):
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            BLOCKED_CLEANUP_SCRIPT.format(
                repo_root=str(_repo_root()), state_dir=str(tmp_path)
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "HELD"
        lock_path = tmp_path / "LOCK"
        assert lock_path.exists()

        proc.send_signal(signal.SIGTERM)

        deadline = time.monotonic() + 15
        while lock_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not lock_path.exists(), (
            "lock outlived SIGTERM — it is being released by unwinding, which a "
            "shutdown's grace period does not wait for"
        )
        # The point of the test: still running, cleanup still blocked, and the
        # lock is already gone. A SIGKILL now would cost nothing.
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait(timeout=30)


@pytest.mark.isolated
def test_sigint_release_is_unchanged(tmp_path):
    lock_path = tmp_path / "LOCK"
    with holder(tmp_path) as proc:
        proc.send_signal(signal.SIGINT)
        assert _await_lock_release(lock_path), "SIGINT left a lock behind"


def test_sigkill_still_leaves_a_lock_that_recovery_can_clear(tmp_path):
    """No handler can run for SIGKILL — the honest guarantee is that what it
    leaves behind is recoverable, not that it leaves nothing."""
    with holder(tmp_path) as proc:
        proc.kill()
        proc.wait(timeout=30)

    lock_path = tmp_path / "LOCK"
    assert lock_path.exists()
    lock = LoopLock(tmp_path)
    assert LoopLock.is_live(lock.read()) is False
    lock.break_stale()
    assert not lock_path.exists()


def test_the_isolated_marker_is_registered_and_actually_used():
    """Isolation must not decay into deletion.

    An `isolated` test runs in its own process and nowhere else, so if the
    marker were dropped, mistyped, or the dedicated command removed, the test
    would stop running and every suite would stay green — silent coverage
    loss, which is worse than the flake it replaces.

    Asks CONFIGPARSER and PYTEST rather than grepping the files. The first
    version of this test grepped, and two of its three assertions matched
    their own text: `-m "not isolated"` appears in the explanatory comment,
    and `@pytest.mark.isolated` appears in the assertion line itself. Both
    mutations passed. A guard that reads its own source proves nothing.
    """
    import configparser
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]

    parser = configparser.ConfigParser()
    parser.read(root / "pytest.ini")
    addopts = parser.get("pytest", "addopts", fallback="")
    markers = parser.get("pytest", "markers", fallback="")
    assert "not isolated" in addopts, f"default run must exclude it; addopts={addopts!r}"
    assert "isolated" in markers, "the marker must be declared, or pytest ignores typos"

    # Authoritative: ask pytest which tests carry the marker.
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "autoloop/tests/test_crash_safety.py",
         "-m", "isolated", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=str(root), capture_output=True, text=True, timeout=120,
    ).stdout
    assert "test_sigint_release_is_unchanged" in collected, (
        "the flaky test must actually carry the marker, not merely mention it"
    )
