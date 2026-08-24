"""Progress-based stall detection for the write-capable implementation agent.

**Why this exists.** The loop used to bound a subagent by ELAPSED TIME
(`audit.agent_timeout_seconds`, retired — see `config.py`). Measured on
2026-08-05/06, that bound never once caught a hung agent and destroyed
in-progress work six times:

    merge-01  1800s  591 insertions across 16 files
    merge-01  1800s  532 insertions across 15 files
    scope-01   900s  503 insertions
    dash-04    900s  631 insertions
    exec-01    900s  605 insertions
    hlth-01    900s  499 insertions

A hung agent leaves nothing behind; every one of those was mid-write. Raising
the ceiling 900 -> 1800 changed only the size of the loss, because the work
expanded to fill the budget. Elapsed time cannot distinguish a large task from
a wedged one, which is exactly why it failed.

**What replaces it.** Bound the LACK OF PROGRESS instead. While the worker
repository keeps changing, the agent runs. When nothing has changed for
`StallPolicy.stall_seconds`, that is a hang: kill it and report the stall,
naming how long it was silent and what it had produced. Absence of filesystem
change over minutes is a DIRECT observation of the thing being detected, not a
proxy for it.

**Where the observation comes from.** The worker repository itself —
`git status --porcelain -z -uall` through the policy-validated `GitGateway`,
plus `st_mtime_ns`/`st_size` of each dirty path. NEVER anything the agent
reports about itself, and never a raw filesystem walk: this module's own probe
runs `git status`, which refreshes `.git/index`, so a walk that included
`.git` would see churn every tick and the detector would never fire. The
per-path stat is load-bearing too — repeated Edits to the SAME file leave the
dirty-path set unchanged while the file itself keeps growing.

**The bound is replaced, not removed.** Without a stall detector a wedged
agent runs forever and the loop never recovers. `StallPolicy.ceiling_seconds`
is an absolute backstop set far above any real task (hours, not minutes). It
should effectively never fire; when it does, `StallReport.describe()` says so
loudly, because that is a finding rather than a routine timeout.

**Biases, stated on purpose.** Every ambiguous case errs toward letting the
agent finish, because killing a healthy agent is the mistake this module
exists to correct: a probe that cannot observe the tree never triggers a stall
kill (only the ceiling can end a run we cannot see), a sample that is merely
missing counts as "no evidence of silence" rather than as silence, and the
default window is generous — a long compile or a long test run is not a stall.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

#: Verdicts. `COMPLETED` means the process exited on its own — the ordinary
#: case, and the only one that is not a kill.
COMPLETED = "completed"
STALLED = "stalled"
CEILING = "ceiling"

#: No filesystem change for this long is a hang. Deliberately GENEROUS: the
#: failure being corrected is killing healthy agents, so the default is set
#: where a long compile, a long test run, or a long stretch of reading before
#: the first Edit all comfortably fit inside it.
DEFAULT_STALL_SECONDS = 1800.0
#: The backstop, hours rather than minutes. Not a task budget — a guarantee
#: that a pathological run still terminates.
DEFAULT_CEILING_SECONDS = 14400.0

#: How often the tree is sampled. Each sample costs one `git status` round
#: trip, so this is not free; it is also far finer than any window it feeds.
DEFAULT_POLL_SECONDS = 5.0
#: SIGTERM -> (grace) -> SIGKILL. Long enough for the CLI to flush what it has
#: already written, short enough that a kill is not itself a hang.
DEFAULT_TERMINATE_GRACE_SECONDS = 10.0
#: Poll step while waiting out that grace period.
_GRACE_POLL_SECONDS = 0.5

#: A new file larger than this is not line-counted for the report. The report
#: is a review aid; reading an arbitrarily large blob to produce it is not.
_MAX_COUNT_BYTES = 8 * 1024 * 1024

_INSERTIONS_RE = re.compile(r"(\d+) insertions?\(\+\)")


@dataclass(frozen=True)
class StallPolicy:
    """The two bounds plus the mechanics of applying them.

    `stall_seconds` must be strictly under `ceiling_seconds`: if it were not,
    the ceiling would always fire first and the stall detector — the whole
    point — would be unreachable while still looking configured.
    """

    stall_seconds: float = DEFAULT_STALL_SECONDS
    ceiling_seconds: float = DEFAULT_CEILING_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS
    terminate_grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS

    def __post_init__(self) -> None:
        for name in ("stall_seconds", "ceiling_seconds", "poll_seconds"):
            if getattr(self, name) <= 0:
                raise ValueError(f"StallPolicy.{name} must be > 0")
        if self.stall_seconds >= self.ceiling_seconds:
            raise ValueError(
                "StallPolicy.stall_seconds must be strictly below "
                "ceiling_seconds — otherwise the absolute backstop always "
                "fires first and the stall detector never runs, while still "
                "reading as configured"
            )


@dataclass(frozen=True)
class ProgressSample:
    """One observation of the worker tree. Compared by value: two equal
    samples mean nothing observable changed between them."""

    files: int = 0
    #: (path, st_mtime_ns, st_size) per dirty path, sorted. `(-1, -1)` for a
    #: path git reports but that cannot be stat'd (a deletion, a race).
    marks: tuple[tuple[str, int, int], ...] = ()


@dataclass(frozen=True)
class PartialWork:
    """What the agent had produced at the moment it was killed.

    A stall with substantial partial work and a stall with none mean very
    different things — the first is a writer that wedged, the second a run
    that never started — so the report always distinguishes them explicitly
    rather than printing `0` and leaving the reader to interpret it.
    """

    files_changed: int = 0
    lines_written: int = 0
    #: False when the worker repo could not be read at all. Distinct from
    #: "measured zero", which is a real and meaningful answer.
    measured: bool = True
    note: str = ""

    def describe(self) -> str:
        if not self.measured:
            reason = self.note or "the worker repository could not be read"
            return f"UNKNOWN — {reason}"
        if self.files_changed == 0:
            return (
                # "round", not "hang": since exec-01 this same sentence is read
                # by `implement_executor._partial_work_note` on every failed
                # round, most of which are not hangs at all (a provider error, a
                # crash, a failed validation). The claim — nothing changed, so
                # nothing was lost — is identical for all of them.
                "NONE — no file in the worker repository had changed, so this "
                "round produced nothing (no work was lost)"
            )
        body = (
            f"{self.files_changed} file(s) changed, ~{self.lines_written} line(s) "
            "written (insertions into tracked files plus the length of every new "
            "file — close to, but not identical with, git's own insertion count)"
        )
        return f"{body}{f'; {self.note}' if self.note else ''}"


@dataclass(frozen=True)
class StallReport:
    """The operator/reviewer-facing account of a kill.

    Carries the numbers a reviewer needs to tell the two kinds of stall apart
    (`partial`), plus whether validation had begun — a field rather than a
    fixed string, because "the executor validates only after the agent
    returns" is true of today's pipeline and would rot into a lie if that
    pipeline is ever reordered.
    """

    verdict: str
    elapsed_seconds: float
    silent_seconds: float
    stall_seconds: float
    ceiling_seconds: float
    partial: PartialWork = PartialWork()
    validation_started: bool = False
    #: True when the progress probe was failing at kill time — the silence
    #: above may then be unobserved rather than real.
    probe_blind: bool = False

    def describe(self) -> str:
        if self.verdict == CEILING:
            head = (
                f"ABSOLUTE CEILING FIRED after {self.elapsed_seconds:.0f}s "
                f"(audit.agent_ceiling_seconds = {self.ceiling_seconds:.0f}s). "
                "This backstop is set far above any real task and is NOT "
                "expected to fire — treat it as a finding worth investigating, "
                "not a routine timeout. The stall detector never saw a hang: "
                f"the worker repository last changed {self.silent_seconds:.0f}s "
                f"ago, inside the {self.stall_seconds:.0f}s stall window."
            )
        else:
            head = (
                f"STALLED: nothing in the worker repository changed for "
                f"{self.silent_seconds:.0f}s (stall window "
                f"audit.agent_stall_seconds = {self.stall_seconds:.0f}s), so the "
                f"agent was killed after {self.elapsed_seconds:.0f}s in total. "
                "Elapsed time was NOT the trigger — the absence of filesystem "
                "change was."
            )
        parts = [head, f"Partial work when it was killed: {self.partial.describe()}."]
        parts.append(
            "Validation HAD started."
            if self.validation_started
            else (
                "Validation had NOT started — the executor runs it only after "
                "the agent returns, so nothing here was linted or tested."
            )
        )
        if self.probe_blind:
            parts.append(
                "NOTE: the worker-repo progress probe was failing when this ended, "
                "so the silence reported above may be unobserved rather than real."
            )
        return " ".join(parts)


@dataclass(frozen=True)
class Supervision:
    verdict: str
    returncode: int | None = None
    #: Present only on a kill (`STALLED` / `CEILING`).
    report: StallReport | None = None

    @property
    def killed(self) -> bool:
        return self.verdict != COMPLETED


class ProgressProbe(Protocol):
    """The seam between "is it still working" and "how it is observed"."""

    def sample(self) -> ProgressSample: ...

    def partial_work(self) -> PartialWork: ...


class ProcessHandle(Protocol):
    """The subset of `subprocess.Popen` the supervisor needs. `subprocess.run`
    deliberately does NOT satisfy this: it hands back no handle, so there is
    no variant of it that can be killed part-way through — which is why the
    supervised path spawns rather than runs."""

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


# ---- observation: the worker repository itself -------------------------------


class WorkerTreeProbe:
    """Progress as the worker repository reports it.

    Every git call goes through `GitGateway`, so the policy whitelist covers
    this module exactly as it covers every other git access in the loop (see
    `git_gateway.py`'s module docstring — "every invocation goes through the
    policy whitelist, even read-only ones"). Nothing here shells out to git
    directly, and nothing here reads the agent's own account of its work.
    """

    def __init__(self, git, repo_root: Path | None = None):
        self._git = git
        self._root = Path(repo_root) if repo_root is not None else Path(git.repo_root)

    def sample(self) -> ProgressSample:
        entries = self._git.dirty_entries_all()
        marks: list[tuple[str, int, int]] = []
        for _status, path in sorted(entries):
            try:
                stat = (self._root / path).stat()
            except OSError:
                # A path git names but we cannot stat (deleted between the
                # status call and here). Recorded rather than dropped: its
                # appearance and disappearance are both real changes.
                marks.append((path, -1, -1))
                continue
            marks.append((path, stat.st_mtime_ns, stat.st_size))
        return ProgressSample(files=len(marks), marks=tuple(marks))

    def partial_work(self) -> PartialWork:
        try:
            entries = self._git.dirty_entries_all()
        except Exception as exc:  # GitError, or anything the gateway raises
            return PartialWork(
                measured=False,
                note=f"git status failed in the worker repo ({_describe(exc)})",
            )
        notes: list[str] = []
        lines = 0
        tracked = sum(1 for status, _path in entries if not status.startswith("?"))
        try:
            # Tracked edits. `git diff HEAD --stat` covers staged AND unstaged
            # changes against the commit the round started from; `--numstat`
            # would be exact but is not on the policy whitelist, and widening
            # that whitelist to prettify a report is not a trade worth making.
            # (`HEAD` always resolves in a real worker repo — `WorkerRepoManager
            # .create` ends with `git checkout -B <branch> FETCH_HEAD` — but a
            # failure here must still not be reported as a measured zero.)
            lines += _parse_insertions(self._git.worktree_diff_stat())
        except Exception as exc:
            notes.append(
                f"INCOMPLETE: the count EXCLUDES edits to {tracked} already-tracked "
                f"file(s) — reading the diff failed ({_describe(exc)})"
                if tracked
                else f"tracked-file insertions unavailable ({_describe(exc)})"
            )
        skipped = 0
        for status, path in entries:
            if not status.startswith("?"):
                continue
            # A brand-new file is not in any diff, so its whole length is what
            # the agent wrote.
            counted = _count_lines(self._root / path)
            if counted is None:
                skipped += 1
            else:
                lines += counted
        if skipped:
            notes.append(f"{skipped} new file(s) not line-counted (binary, huge or unreadable)")
        return PartialWork(
            files_changed=len(entries),
            lines_written=lines,
            measured=True,
            note="; ".join(notes),
        )


def _parse_insertions(stat_text: str) -> int:
    match = _INSERTIONS_RE.search(stat_text or "")
    return int(match.group(1)) if match else 0


def _count_lines(path: Path) -> int | None:
    """Lines in a new file, or None when it should not be counted."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > _MAX_COUNT_BYTES or b"\0" in data:
        return None
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _describe(exc: BaseException) -> str:
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


# ---- the supervisor ----------------------------------------------------------


def supervise(
    handle: ProcessHandle,
    probe: ProgressProbe,
    policy: StallPolicy,
    *,
    clock=time.monotonic,
    sleep=time.sleep,
    validation_started: bool = False,
) -> Supervision:
    """Run until the process exits, stalls, or hits the absolute ceiling.

    Pure over its collaborators — an injected handle, probe, clock and sleep —
    so the timing behaviour this module exists for is testable without any
    real process or any real waiting.

    Ordering inside the loop is deliberate:

    * `handle.poll()` FIRST, so a process that finished during the last sleep
      is `COMPLETED`, never posthumously "stalled".
    * The ceiling before the stall check, so the loudly-reported verdict wins
      when a run somehow satisfies both.
    * `handle.poll()` once more immediately before a stall kill, because the
      window between deciding and signalling is exactly where a slow agent
      finishes.
    """
    started = clock()
    last_change = started
    last_sample, blind = _observe(probe)
    while True:
        returncode = handle.poll()
        if returncode is not None:
            return Supervision(verdict=COMPLETED, returncode=returncode)

        now = clock()
        elapsed = now - started
        if elapsed >= policy.ceiling_seconds:
            return _kill(
                handle, probe, policy, CEILING, elapsed, now - last_change,
                blind, validation_started, clock, sleep,
            )

        sample, blind = _observe(probe)
        if not blind:
            if sample != last_sample:
                last_sample = sample
                last_change = now
            elif now - last_change >= policy.stall_seconds:
                returncode = handle.poll()
                if returncode is not None:
                    return Supervision(verdict=COMPLETED, returncode=returncode)
                return _kill(
                    handle, probe, policy, STALLED, elapsed, now - last_change,
                    blind, validation_started, clock, sleep,
                )
        # else: the probe could not observe the tree. That is NOT silence —
        # it is the absence of evidence about silence, and killing on it would
        # repeat the exact mistake this module corrects. Only the ceiling can
        # end a run we cannot see.
        sleep(policy.poll_seconds)


def _observe(probe: ProgressProbe) -> tuple[ProgressSample | None, bool]:
    """(sample, blind). A probe that raises is blind, never "unchanged"."""
    try:
        return probe.sample(), False
    except Exception:
        return None, True


def _kill(
    handle: ProcessHandle,
    probe: ProgressProbe,
    policy: StallPolicy,
    verdict: str,
    elapsed: float,
    silent: float,
    blind: bool,
    validation_started: bool,
    clock,
    sleep,
) -> Supervision:
    returncode = _stop(handle, policy, clock, sleep)
    try:
        partial = probe.partial_work()
    except Exception as exc:
        partial = PartialWork(measured=False, note=_describe(exc))
    return Supervision(
        verdict=verdict,
        returncode=returncode,
        report=StallReport(
            verdict=verdict,
            elapsed_seconds=elapsed,
            silent_seconds=silent,
            stall_seconds=policy.stall_seconds,
            ceiling_seconds=policy.ceiling_seconds,
            partial=partial,
            validation_started=validation_started,
            probe_blind=blind,
        ),
    )


def _stop(handle: ProcessHandle, policy: StallPolicy, clock, sleep) -> int | None:
    """SIGTERM, a bounded grace period, then SIGKILL.

    Bounded by ITERATIONS, not by a clock comparison: the grace loop must
    terminate even when handed a clock that does not advance, and a supervisor
    that can itself hang is not a hang detector.
    """
    _signal(handle.terminate)
    steps = max(1, int(policy.terminate_grace_seconds / _GRACE_POLL_SECONDS))
    for _ in range(steps):
        returncode = handle.poll()
        if returncode is not None:
            return returncode
        sleep(_GRACE_POLL_SECONDS)
    _signal(handle.kill)
    try:
        return handle.poll()
    except Exception:
        return None


def _signal(action) -> None:
    try:
        action()
    except Exception:
        # Already dead, or a handle that refuses the signal. Either way the
        # supervisor's own job (report and return) must still complete.
        pass


# ---- the real process --------------------------------------------------------


class ProcessGroupHandle:
    """`subprocess.Popen`, signalled as a process GROUP.

    The `claude` CLI spawns children. Signalling only the parent leaves
    orphans that keep writing into the worker repository after the kill, which
    makes the very numbers this module reports a moving target. Spawned with
    `start_new_session=True` so the group is ours alone and the signal can
    never reach the loop's own process tree.
    """

    def __init__(self, proc):
        self._proc = proc

    @property
    def pid(self) -> int:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    def poll(self) -> int | None:
        return self._proc.poll()

    def terminate(self) -> None:
        self._send(signal.SIGTERM, self._proc.terminate)

    def kill(self) -> None:
        self._send(signal.SIGKILL, self._proc.kill)

    def _send(self, sig, fallback) -> None:
        try:
            os.killpg(os.getpgid(self._proc.pid), sig)
        except (OSError, AttributeError):
            # No process group (already reaped), or a platform without
            # killpg/getpgid — fall back to the single process.
            _signal(fallback)


def spawn_supervised(argv, *, cwd, env, stdout, stderr) -> ProcessGroupHandle:
    """The default spawn for the supervised path.

    `stdout`/`stderr` are real FILE OBJECTS, not pipes, on purpose: a pipe
    that nobody drains fills its OS buffer and blocks the child forever, which
    would be a hang manufactured by the hang detector. Temporary files also
    live outside the worker repository, so the agent's own output can never
    register as filesystem progress.
    """
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    return ProcessGroupHandle(proc)
