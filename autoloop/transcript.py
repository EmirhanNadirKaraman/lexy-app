"""Append-only JSONL transcript of everything the loop does, plus the pure
readers that turn it into per-stage timing.

One entry per event: requests prepared/submitted, raw responses, parsed
directives, policy verdicts, git actions, executor outcomes, errors,
diagnostics pointers. The transcript is the audit log — the loop itself never
reads it back (recovery uses `state.py`), so its format can grow without
migration concerns. Payload fields live under "data" to avoid key collisions.

**Durations are MEASURED, never inferred** (prof-01, 2026-08-20). Every record
has always carried a `ts`, so a stage could be "timed" by subtracting two of
them — but that measures THE GAP, and the gap contains every browser fault,
restart and rate-limit wait that happened inside it. The measured 429-minute
`request_prepared` → `request_submitted` window that motivated this held 74
`browser_error`, 27 `browser_restarted` and 7 `rate_limited` records; packet
construction was a rounding error inside it. So an operation that finishes now
records its own monotonic elapsed time under `data.duration_seconds` on the
event it already emits (`Stopwatch`, below) — no new event types, no second
metrics store.

The transcript is APPEND-ONLY history: the 7,203 records written before this
existed will never gain the field, and nothing here pretends otherwise. The
profiler keeps measured and gap-derived samples as two INDEPENDENT sets and
reports them separately (`profile_stages`, `render_profile`), so a mixed
transcript never presents a measured number and an inferred one as the same
column. Independent means exactly that: a modern round contributes its measured
duration to one set AND its timestamp window to the other, because for `submit`
and `execute` the gap is a strictly WIDER window around the same operation, not
a degraded substitute for it — so the two rows are two views of the same
history and must never be summed. Reading is pure and takes no lock — see
`cli._cmd_profile`.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .state import utcnow_iso

#: The ONE key a measured duration is recorded under, in an event's own `data`.
#: One key for every stage, deliberately: the profiler asks each completion
#: event the same question, and a per-stage name would make "does this record
#: carry a measured duration?" a lookup table that drifts from the emit sites.
DURATION_KEY = "duration_seconds"


class Stopwatch:
    """Monotonic elapsed time for ONE operation, stamped onto its own event.

    Cheap by construction: two `time.monotonic()` calls (a vDSO read on Linux
    and macOS, no syscall in the ordinary case) and one dict copy per operation.
    Nothing is opened, nothing is written, and no control flow depends on it —
    this runs on every phase step, so it may not become a path that can be slow
    or that can fail an operation.

    **Total: `stop()` never raises.** `clock` is injectable so tests can drive
    it, which means it is also a thing that can misbehave, and a timing path
    that can throw would turn "we failed to measure this" into "the round
    failed". Every reading is guarded, and a reading that is missing, not
    finite, or BACKWARDS (a clock that went down — impossible for
    `time.monotonic`, entirely possible for an injected one) yields `None`,
    which `stamp` renders as an ABSENT key rather than a wrong number. A record
    with no `duration_seconds` is exactly the shape of every record written
    before this existed, and the profiler already handles it.

    **First stop wins.** The outcome is latched on the first `stop()` — value or
    `None` — and every later call returns it unchanged. A completion event that
    is stamped twice (a retry path re-rendering the same data) reports the same
    elapsed time both times instead of growing.
    """

    __slots__ = ("_clock", "_started", "_stopped", "_elapsed")

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._stopped = False
        self._elapsed: float | None = None
        self._started = self._read()

    def _read(self) -> float | None:
        try:
            value = float(self._clock())
        except Exception:
            return None
        return value if math.isfinite(value) else None

    def stop(self) -> float | None:
        """Seconds elapsed since construction, or `None` if it is not knowable."""
        if self._stopped:
            return self._elapsed
        self._stopped = True
        started, now = self._started, self._read()
        if started is None or now is None:
            return None
        elapsed = now - started
        if not math.isfinite(elapsed) or elapsed < 0.0:
            return None
        # Microseconds: finer than any stage this measures, and it keeps the
        # JSON short instead of carrying float noise into the record.
        self._elapsed = round(elapsed, 6)
        return self._elapsed

    def stamp(self, data: dict | None = None) -> dict:
        """`data` plus the measured duration — or a copy of `data` unchanged
        when the measurement failed. Never mutates the argument."""
        out = dict(data or {})
        elapsed = self.stop()
        if elapsed is not None:
            out[DURATION_KEY] = elapsed
        return out


class TranscriptLogger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        entry_type: str,
        *,
        iteration: int | None = None,
        request_id: str | None = None,
        data: dict | None = None,
    ) -> None:
        entry: dict = {"ts": utcnow_iso(), "type": entry_type}
        if iteration is not None:
            entry["iteration"] = iteration
        if request_id is not None:
            entry["request_id"] = request_id
        if data:
            entry["data"] = data
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Profiling: pure readers over the transcript. Nothing below writes anything,
# takes a lock, or imports the orchestrator — `profile` is safe while the loop
# runs, exactly like `status`, `tasks` and `blockers`.
# ---------------------------------------------------------------------------

#: The two sources a timing number can have, and they are never averaged
#: together. `MEASURED` is the operation's own monotonic elapsed time, recorded
#: by the code that performed it. `GAP_DERIVED` is the difference between two
#: transcript timestamps — the INTERVAL between two events, which includes
#: every failure, retry and wait that happened inside it, at the one-second
#: resolution `state.utcnow_iso` writes.
MEASURED = "measured"
GAP_DERIVED = "gap-derived"


@dataclass(frozen=True)
class Stats:
    """Summary of one sample set. `p90` is nearest-rank (`ceil(0.9n)`-th
    smallest, 1-indexed) — no interpolation, so the value printed is always a
    value that was actually observed, and a 3-sample set still has one."""

    count: int
    median: float
    mean: float
    p90: float
    total: float


def summarize(samples: Sequence[float]) -> Stats | None:
    """`None` for an empty set — distinct from, and never rendered as, zero."""
    values = sorted(float(s) for s in samples)
    if not values:
        return None
    n = len(values)
    mid = n // 2
    median = values[mid] if n % 2 else (values[mid - 1] + values[mid]) / 2.0
    rank = max(1, math.ceil(0.9 * n))
    return Stats(
        count=n,
        median=median,
        mean=sum(values) / n,
        p90=values[rank - 1],
        total=sum(values),
    )


@dataclass(frozen=True)
class Stage:
    """One timeable stage of a review round, and both ways it can be timed.

    `measured_event` is the completion event whose `data.duration_seconds` this
    stage reads; empty means the stage HAS no measured form (see `review_wait`)
    rather than that nobody got round to it. `gap_start`/`gap_end` are the pair
    of historical events whose timestamps bound the same stage, matched by
    `request_id`; both empty means no historical pair exists at all.
    """

    name: str
    description: str
    measured_event: str = ""
    gap_start: str = ""
    gap_end: str = ""
    #: Why the gap number is not the same quantity as the measured one — printed
    #: under the gap row so nobody reads the two as interchangeable.
    gap_note: str = ""
    #: Why there is no measured form, when there is none.
    measured_note: str = ""


#: The smallest stage set both sources can support, in round order.
#:
#: Deliberately four, not one per event type: a stage earns a row only when
#: SOMETHING can time it — an operation that records its own elapsed time, or a
#: pair of existing events that bound it and share a `request_id`. Everything
#: else in the 68-type vocabulary is a fault, a diagnostic or a state change,
#: and belongs in the windows these stages already contain.
STAGES: tuple[Stage, ...] = (
    Stage(
        name="prepare",
        description="build the review packet (plan delivery, build context, hash, render prompt)",
        measured_event="request_prepared",
        gap_note=(
            "no historical pair exists — nothing precedes request_prepared for a "
            "request, so records written before durations were recorded cannot "
            "contribute to this stage at all"
        ),
    ),
    Stage(
        name="submit",
        description="send the prepared packet to the reviewer",
        measured_event="request_submitted",
        gap_start="request_prepared",
        gap_end="request_submitted",
        gap_note=(
            "a strictly WIDER window than the measured send: the INTERVAL from "
            "preparation to an accepted send, which also holds the attach, the "
            "controlled reload and the duplicate check, plus every "
            "browser_error, browser_restarted and rate_limited record that fell "
            "inside it — and on a chunked round the whole delivering phase, "
            "which is real work no column measures (look for "
            "review_part_delivered inside the window before reading a large gap "
            "here as a fault)"
        ),
    ),
    Stage(
        name="review_wait",
        description="wait for the reviewer to answer",
        gap_start="request_submitted",
        gap_end="response_received",
        measured_note=(
            "GAP BY NATURE — this stage IS the wait, so there is no operation "
            "whose elapsed time could be measured instead, and no measured "
            "column will ever appear here"
        ),
        gap_note="reviewer latency, at the transcript's one-second timestamp resolution",
    ),
    Stage(
        name="execute",
        description="run the executor for one round (implementation agent, then validation)",
        measured_event="executed",
        gap_start="directive",
        gap_end="executed",
        gap_note=(
            "a strictly WIDER window than the measured run — the interval from "
            "the directive being parsed to the round finishing. Only pairs for "
            "records written after prof-01 (2026-08-20): before that the "
            "executed event carried no request_id, so directive -> executed "
            "could not be matched and this stage was unmeasured entirely"
        ),
    ),
)


@dataclass(frozen=True)
class StageProfile:
    """One stage's two INDEPENDENT sample sets.

    Both may be populated for the same request — see `profile_stages`. They are
    reported side by side and never added together.
    """

    stage: Stage
    measured: Stats | None
    gap: Stats | None


@dataclass(frozen=True)
class TranscriptRead:
    records: tuple[dict, ...]
    #: Lines that were present but unusable — malformed JSON (including the
    #: partial last line of a file the live loop is mid-append to), a JSON value
    #: that is not an object, or an object with no string `type`.
    skipped: int = 0


def read_records(path: Path) -> TranscriptRead:
    """Every usable record in `path`, tolerant line by line.

    Deliberately NOT one `try` around the whole loop: this reads a file the
    live loop is appending to, so the last line can be a partial write, and a
    whole-loop guard would throw away the 7,000 good records in front of it.
    An unreadable file yields an empty read rather than raising — `profile`
    reports what it found, it does not fail.
    """
    records: list[dict] = []
    skipped = 0
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return TranscriptRead((), 0)
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(record, dict) or not isinstance(record.get("type"), str):
                skipped += 1
                continue
            records.append(record)
    return TranscriptRead(tuple(records), skipped)


def measured_duration(record: dict) -> float | None:
    """The measured duration a record carries, or `None`.

    `None` covers every shape that is not a usable number — the key absent (an
    old record, or one whose timing failed), a string, a bool, NaN, or a
    negative. Refusing rather than coercing is the point: a fabricated number
    here would be indistinguishable from a real one downstream.
    """
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    raw = data.get(DURATION_KEY)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not math.isfinite(value) or value < 0.0:
        return None
    return value


def _record_epoch(record: dict) -> float | None:
    raw = record.get("ts")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if moment.tzinfo is None:
        # Every `ts` the loop writes is tz-aware UTC (`state.utcnow_iso`); a
        # naive one can only come from a hand-edited or foreign line, and
        # reading it as UTC is the assumption that keeps it comparable.
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _request_id(record: dict) -> str:
    rid = record.get("request_id")
    return rid if isinstance(rid, str) and rid else ""


def _measured_samples(records: Sequence[dict], event_type: str) -> list[float]:
    samples: list[float] = []
    for record in records:
        if record.get("type") != event_type:
            continue
        value = measured_duration(record)
        if value is None:
            continue
        samples.append(value)
    return samples


def _gap_samples(records: Sequence[dict], start_type: str, end_type: str) -> list[float]:
    """Timestamp deltas for `start_type` -> `end_type`, paired by `request_id`.

    FIRST pair wins per request: a stage that starts once and ends twice (a
    resend after a rotation) contributes the first completed window and drops
    the rest, and a start with no end contributes nothing at all. Records
    without a `request_id` cannot be paired and are skipped — the reason
    `directive` -> `executed` produced nothing before `executed` carried one.
    """
    open_at: dict[str, float] = {}
    samples: list[float] = []
    for record in records:
        rid = _request_id(record)
        if not rid:
            continue
        moment = _record_epoch(record)
        if moment is None:
            continue
        rtype = record.get("type")
        if rtype == start_type:
            open_at.setdefault(rid, moment)
            continue
        if rtype != end_type:
            continue
        started = open_at.pop(rid, None)
        if started is None:
            continue
        delta = moment - started
        if delta < 0.0:
            # Clock adjusted backwards between the two writes. Unknowable, not
            # zero — dropped for the same reason `Stopwatch` drops it.
            continue
        samples.append(delta)
    return samples


def profile_stages(
    records: Sequence[dict], stages: Sequence[Stage] = STAGES
) -> tuple[StageProfile, ...]:
    """Per-stage timing, measured and gap-derived kept apart.

    **The two sets are INDEPENDENT, not mutually exclusive.** A record that
    carries a measured duration still contributes its timestamp window to the
    gap set for the same stage, because the two are not competing estimates of
    one quantity: for `submit` and `execute` the gap is a strictly wider window
    that CONTAINS the measured operation, so `gap.count` reads as "rounds whose
    window could be paired" and `measured.count` as "rounds that recorded their
    own elapsed time". Suppressing a modern round's gap would throw away the
    one comparison that makes the pair worth printing — how much of the window
    was the work — and would redefine the gap row as "rounds we failed to
    measure", a population that empties out as instrumentation lands, taking
    the historical comparison with it. The two are rendered as separate rows
    and never summed together (`render_profile`).
    """
    profiles: list[StageProfile] = []
    for stage in stages:
        measured_samples: list[float] = []
        if stage.measured_event:
            measured_samples = _measured_samples(records, stage.measured_event)
        gap_samples: list[float] = []
        if stage.gap_start and stage.gap_end:
            gap_samples = _gap_samples(records, stage.gap_start, stage.gap_end)
        profiles.append(
            StageProfile(
                stage=stage,
                measured=summarize(measured_samples),
                gap=summarize(gap_samples),
            )
        )
    return tuple(profiles)


def format_seconds(value: float) -> str:
    """Short, fixed-width-ish rendering. Never negative — callers drop those."""
    if value < 10.0:
        return f"{value:.2f}s"
    if value < 60.0:
        return f"{value:.1f}s"
    if value < 3600.0:
        return f"{int(value // 60)}m{int(value % 60):02d}s"
    return f"{value / 3600.0:.1f}h"


def _stats_line(label: str, stats: Stats) -> str:
    return (
        f"  {label:<12} n={stats.count:<5} "
        f"median {format_seconds(stats.median):>8}  "
        f"mean {format_seconds(stats.mean):>8}  "
        f"p90 {format_seconds(stats.p90):>8}  "
        f"total {format_seconds(stats.total):>8}"
    )


def _wrapped_note(note: str, indent: str = "               ") -> list[str]:
    """Fold a note to a readable width without pulling in `textwrap` semantics
    the tests would then have to encode. Word-wrapped at 74 columns."""
    words, lines, current = note.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > 74 and current:
            lines.append(indent + current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(indent + current)
    return lines


def render_profile(
    path: Path, read: TranscriptRead, profiles: Sequence[StageProfile] | None = None
) -> str:
    """The `profile` command's whole output. Pure — takes the read, returns text."""
    profiles = profile_stages(read.records) if profiles is None else profiles
    header = f"records      {len(read.records)}"
    if read.skipped:
        header += f" ({read.skipped} unreadable line(s) skipped)"
    lines = [f"transcript   {path}", header, ""]
    for profile in profiles:
        stage = profile.stage
        lines.append(f"{stage.name} — {stage.description}")
        if not stage.measured_event:
            lines.append(f"  {MEASURED:<12} n/a")
            lines += _wrapped_note(stage.measured_note)
        elif profile.measured is None:
            lines.append(f"  {MEASURED:<12} n=0")
            # Two notes, not one sentence: the second must reach the reader
            # VERBATIM, and folding both into one string lets the word wrap
            # split the phrase that carries the whole meaning.
            lines += _wrapped_note(
                f"no record of type '{stage.measured_event}' carries a {DURATION_KEY}"
            )
            lines += _wrapped_note("every record here predates measured durations")
        else:
            lines.append(_stats_line(MEASURED, profile.measured))
        if not (stage.gap_start and stage.gap_end):
            lines.append(f"  {GAP_DERIVED:<12} n/a")
        elif profile.gap is None:
            lines.append(f"  {GAP_DERIVED:<12} n=0")
        else:
            lines.append(_stats_line(GAP_DERIVED, profile.gap))
        # ALWAYS, including under an `n/a` row: a stage with no historical pair
        # has to say why, or a blank reads as "nothing happened here".
        lines += _wrapped_note(stage.gap_note)
        lines.append("")
    lines += [
        "how to read this",
        f"  {MEASURED:<12} the operation's OWN monotonic elapsed time, recorded by the",
        "               code that performed it. This is the work.",
        f"  {GAP_DERIVED:<12} the interval between two transcript timestamps, at one-second",
        "               resolution. This is the GAP: it contains every fault, retry",
        "               and wait that happened inside the window, so it is an upper",
        "               bound on the work, not a measurement of it.",
        "  The two are never combined into one number. They are independent sample sets",
        "  over the same history: the gap window CONTAINS the measured operation, so a",
        "  round can appear in both rows. Do not sum them — read the pair, and take the",
        "  difference as the faults, retries and waits the window swallowed.",
    ]
    return "\n".join(lines)
