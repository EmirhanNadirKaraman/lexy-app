"""A merge backlog that cannot drain must escalate, not be logged hourly and
read by nobody (sweep-01, 2026-08-25).

The sweep did its job faithfully: it refused to merge past a completed task it
could not judge, and said so once an hour for nine days. Nothing read it.
Measured on this repository — `audit-0001` held the sweep for 225.8 hours
across 108 consecutive sweeps, from 2026-08-15 23:13Z to 2026-08-25 09:03Z,
with five approved tasks queued behind it; two of those five rotted into
unmergeable candidates while they waited, and `health --json` mentioned merges
zero times.

So the tests here come in two halves, and the split is the claim:

* the sweep's own record is now DECIDABLE — every invocation writes exactly one
  terminal entry, including the ones that find nothing — while what it merges
  is untouched (`test_merge_sweep.py` owns that, and none of it changes);
* `health --json` reads those entries back and reports what is unresolved, what
  is queued behind it and how long, escalating on AGE rather than on presence.

The false-alarm surface matters as much as the detection, exactly as it does in
`test_health.py`: a field that goes red on every one-sweep hold would be ignored
precisely like the log line it replaces, so the boundary is tested from both
sides.

Self-contained helpers, matching this package's test convention — no git and no
network here: the emitter half drives `BacklogSweeper` over an enumeration that
never reaches a remote, and the reader half writes transcript entries directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from autoloop import health, merge_sweep
from autoloop.blockers import BlockerStore
from autoloop.config import AutoloopConfig, BrowserConfig, PolicyConfig
from autoloop.lock import LoopLock
from autoloop.merge_sweep import BacklogSweeper
from autoloop.policy import PolicyEngine
from autoloop.state import LoopState, Phase, StateStore
from autoloop.tasks import TaskState
from autoloop.transcript import TranscriptLogger
from autoloop.worktask import TaskExecutionStore

URL = "https://chatgpt.com/c/merge-backlog-health"
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)

#: The measured case, used verbatim so the assertions read like the incident.
UNRESOLVED = ("audit-0001",)
QUEUED = ("bind-01", "split-01", "dash-17", "release-01", "split-02")


@pytest.fixture
def config(tmp_path):
    checkout = tmp_path / "repo"
    checkout.mkdir()
    return AutoloopConfig(
        browser=BrowserConfig(conversation_url=URL),
        policy=PolicyConfig(auto_merge_enabled=True),
        state_dir=checkout / ".autoloop",
        workers_root=tmp_path / "workers",
    )


# --- writing a transcript ------------------------------------------------------


def _entry(config, entry_type, *, hours_ago=0.0, data=None, ts=None):
    path = config.transcript_file
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (
        ts
        if ts is not None
        else (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    )
    row = {"ts": stamp, "type": entry_type}
    if data is not None:
        row["data"] = data
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _held(config, *, hours_ago, unresolved=UNRESOLVED, pending=QUEUED, ts=None):
    """One `merge_sweep_held` entry, shaped exactly as `BacklogSweeper.sweep`
    writes it."""
    _entry(
        config,
        merge_sweep.SWEEP_HELD_EVENT,
        hours_ago=hours_ago,
        ts=ts,
        data={"unresolved": list(unresolved), "pending": list(pending)},
    )


def _idle(config, *, hours_ago, unresolved=()):
    """One `merge_sweep_nothing_to_do` entry — an invocation that found nothing
    to merge, which may still have found something it could not judge."""
    _entry(
        config,
        merge_sweep.SWEEP_IDLE_EVENT,
        hours_ago=hours_ago,
        data={"unresolved": list(unresolved), "pending": []},
    )


def _alive(config, *, phase=Phase.EXECUTING.value):
    """A loop that is provably working: state, a live lock, and a transcript
    line two minutes old.

    The recent line is load-bearing — the silence alarm reads the NEWEST entry,
    so a test whose only transcript content is a held sweep from 200 hours ago
    would come back `silent` and prove nothing about this field. Call it after
    the sweep entries a test wants.
    """
    config.state_dir.mkdir(parents=True, exist_ok=True)
    StateStore(config.state_file).save(
        LoopState(session_id="sweep-01", conversation_url=URL, phase=phase)
    )
    _entry(config, "directive", hours_ago=0.03)
    return LoopLock(config.state_dir)


def _check(config, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("agent_probe", lambda: False)
    # Deterministic: the real probe reads this machine's sysctls, and no verdict
    # here may depend on when the test host last slept.
    kw.setdefault("sleep_probe", lambda start, end: health.SleepEvidence(0.0, "test"))
    return health.check(config, **kw)


# --- staying quiet while the sweep is fine (the false-alarm surface) -----------


def test_a_transcript_with_no_sweep_history_says_nothing_about_merges(config):
    """The ordinary case for every loop that has never held a sweep — including
    every loop with `auto_merge_enabled` off, which never sweeps at all."""
    with _alive(config):
        verdict = _check(config)

    assert verdict.held_merge_sweep is None
    assert verdict.code == health.OK_RUNNING
    assert verdict.needs_attention is False


def test_a_missing_transcript_is_not_a_hold(config):
    """A state directory the loop has never written to has no sweep history to
    have an opinion about. Distinct from an unreadable one, below."""
    assert health.held_merge_sweep(config.transcript_file) is None


def test_a_young_hold_is_reported_as_data_not_as_an_alarm(config):
    """AGE is the signal, not presence. A backlog held for one sweep is a phase
    boundary — a ref force-moved during a release, a remote that did not answer
    this minute — and it clears itself on the next run, so escalating on it
    would make every healthy sweep a false alarm and this field would be ignored
    exactly like the log line it replaces."""
    _held(config, hours_ago=1)
    with _alive(config):
        verdict = _check(config)

    assert verdict.code == health.OK_RUNNING
    assert verdict.needs_attention is False, "one held sweep is not an outage"
    assert verdict.held_merge_sweep is not None, "but it is still visible"
    assert verdict.held_merge_sweep.unresolved == UNRESOLVED
    assert verdict.held_merge_sweep.pending == QUEUED
    assert verdict.held_merge_sweep.held_hours == pytest.approx(1.0)
    assert "merge sweep" not in verdict.summary, "quiet means quiet in the text"
    assert verdict.detail == ""


def test_the_threshold_is_strict_and_tested_from_both_sides(config):
    """Exactly at the threshold is not past it — the same `>` the silence alarm
    uses, so the two read alike."""
    _held(config, hours_ago=health.DEFAULT_HELD_SWEEP_HOURS)
    with _alive(config):
        at_the_line = _check(config)

    assert at_the_line.needs_attention is False
    assert at_the_line.held_merge_sweep.held_hours == pytest.approx(
        health.DEFAULT_HELD_SWEEP_HOURS
    )

    # The same transcript again, with the run starting half an hour earlier —
    # rewritten rather than appended to, because a hold is dated from the FIRST
    # sweep of its run and appending an older entry would not move it.
    config.transcript_file.unlink()
    _held(config, hours_ago=health.DEFAULT_HELD_SWEEP_HOURS + 0.5)
    with _alive(config):
        past_it = _check(config)

    assert past_it.needs_attention is True
    assert past_it.code == health.STUCK_MERGE_BACKLOG


def test_an_operator_threshold_moves_the_line(config):
    """`--held-sweep-hours` exists so an operator who wants it tighter is not
    editing the module. Same hold, two thresholds, two answers."""
    _held(config, hours_ago=2)
    with _alive(config):
        assert _check(config, held_sweep_hours=6.0).needs_attention is False
        assert _check(config, held_sweep_hours=1.0).needs_attention is True


# --- the nine days, reported --------------------------------------------------


def test_an_aged_hold_names_what_is_unresolved_what_is_queued_and_for_how_long(config):
    """The whole claim, in the shape the incident actually had. "A backlog
    exists" is not actionable; "audit-0001 is unresolved, 5 tasks queued, held
    225.8h" is."""
    for hours in (225.8, 100.0, 2.0):
        _held(config, hours_ago=hours)
    with _alive(config):
        verdict = _check(config)

    assert verdict.code == health.STUCK_MERGE_BACKLOG
    assert verdict.needs_attention is True, (
        "nine days of approved work sitting unmerged must not read as fine"
    )
    finding = verdict.held_merge_sweep
    assert finding.unresolved == UNRESOLVED
    assert finding.pending == QUEUED
    assert finding.sweeps == 3
    assert finding.held_hours == pytest.approx(225.8)
    for task_id in UNRESOLVED + QUEUED:
        assert task_id in verdict.detail, "every id, not a sample"
    assert "225.8h" in verdict.detail
    assert "3 sweep(s)" in verdict.detail


def test_the_age_is_measured_from_the_FIRST_held_sweep_not_the_last(config):
    """A sweep that keeps saying "held" every hour must not look like an
    hour-old problem, and one that has STOPPED running must not look like a
    fresh one either — the last observation is reported beside the age rather
    than instead of it."""
    _held(config, hours_ago=200)
    _held(config, hours_ago=100)
    _held(config, hours_ago=50)
    finding = health.held_merge_sweep(config.transcript_file, NOW)

    assert finding.held_hours == pytest.approx(200.0)
    assert finding.sweeps == 3
    assert finding.first_seen.startswith((NOW - timedelta(hours=200)).strftime("%Y-%m-%d"))
    assert finding.last_seen.startswith((NOW - timedelta(hours=50)).strftime("%Y-%m-%d"))


def test_the_queue_reported_is_the_newest_sweeps_not_the_oldest(config):
    """The lists are as-of the last sweep that spoke. A queue that shrank
    because four of its branches were merged by hand must not still read as
    five, or the operator is sent after work that already landed."""
    _held(config, hours_ago=200, pending=QUEUED)
    _held(config, hours_ago=10, pending=("split-02",))
    finding = health.held_merge_sweep(config.transcript_file, NOW)

    assert finding.pending == ("split-02",)
    assert finding.held_hours == pytest.approx(200.0), "the hold is still 200h old"


# --- clearing it --------------------------------------------------------------


@pytest.mark.parametrize("cleared", merge_sweep.SWEEP_CLEARED_EVENTS)
def test_a_later_sweep_that_got_past_enumeration_clears_the_hold(config, cleared):
    """All three are written PAST the hold check, which returns before them, so
    any one of them proves the enumeration found nothing it could not judge.
    An alarm that cannot be cleared is ignored, which is the failure this whole
    field exists to end."""
    _held(config, hours_ago=200)
    _entry(config, cleared, hours_ago=1, data={"merged": ["bind-01"]})
    with _alive(config):
        verdict = _check(config)

    assert health.held_merge_sweep(config.transcript_file, NOW) is None
    assert verdict.held_merge_sweep is None
    assert verdict.needs_attention is False


def test_a_sweep_that_found_NOTHING_clears_the_hold(config):
    """The path that had no evidence at all before sweep-01: a clear sweep used
    to write nothing, so "still held" and "held, then fixed" left identical
    transcripts. In a healthy loop `auto_merge` integrates each completion as it
    lands, so the startup sweep is clean and silent for weeks — meaning a hold
    fixed by hand would have been reported forever."""
    _held(config, hours_ago=200)
    _idle(config, hours_ago=1)
    with _alive(config):
        verdict = _check(config)

    assert verdict.held_merge_sweep is None
    assert verdict.needs_attention is False


def test_a_sweep_with_nothing_to_merge_but_something_to_JUDGE_stays_held(config):
    """`_backlog` can record an unresolved task and then find no candidate to
    queue behind it — `is_clear` has always been false for that, and it is a
    backlog that cannot drain with an empty queue, not a clear run. The queue is
    reported as empty rather than inherited from the older entry."""
    _held(config, hours_ago=200, pending=QUEUED)
    _idle(config, hours_ago=1, unresolved=("audit-0001",))
    finding = health.held_merge_sweep(config.transcript_file, NOW)

    assert finding is not None
    assert finding.unresolved == ("audit-0001",)
    assert finding.pending == (), "the stale queue must not be carried forward"
    assert finding.held_hours == pytest.approx(200.0)
    assert "queued behind it: nothing" in finding.describe()


def test_a_hold_can_START_from_an_unjudgeable_sweep_with_no_queue(config):
    """The same condition with no earlier held entry at all: one completed task
    whose branch is gone stops every future sweep from being clear, and it is
    the only thing that will ever say so."""
    _idle(config, hours_ago=48, unresolved=("gone-01",))
    finding = health.held_merge_sweep(config.transcript_file, NOW)

    assert finding.unresolved == ("gone-01",)
    assert finding.pending == ()
    assert finding.held_hours == pytest.approx(48.0)


def test_a_new_hold_after_a_cleared_one_is_dated_from_the_new_one(config):
    """The run is UNBROKEN by definition; a cleared sweep in between ends it.
    Dating a fresh hold from a resolved one would report an outage on the first
    ordinary hold after a bad week."""
    _held(config, hours_ago=200)
    _entry(config, merge_sweep.SWEEP_COMPLETED_EVENT, hours_ago=150, data={"merged": []})
    _held(config, hours_ago=1)
    finding = health.held_merge_sweep(config.transcript_file, NOW)

    assert finding.sweeps == 1
    assert finding.held_hours == pytest.approx(1.0)


def test_a_sweep_that_CRASHED_neither_starts_nor_clears_a_hold(config):
    """`merge_sweep_error` is also written per-branch (followed by
    `merge_sweep_stopped`), so treating it as terminal would misread a composite
    run — and a crashed enumeration proves nothing about a hold either way.
    Ignoring it keeps the hold reported, which is the closed direction."""
    _entry(config, "merge_sweep_error", hours_ago=300, data={"error": "boom"})
    assert health.held_merge_sweep(config.transcript_file, NOW) is None

    _held(config, hours_ago=200)
    _entry(config, "merge_sweep_error", hours_ago=1, data={"error": "boom"})
    finding = health.held_merge_sweep(config.transcript_file, NOW)

    assert finding is not None and finding.held_hours == pytest.approx(200.0)


def test_unrelated_loop_activity_does_not_clear_a_hold(config):
    """A busy loop writes thousands of records between two sweeps. Only the
    sweep's own terminal entries answer this question."""
    _held(config, hours_ago=200)
    for entry_type in ("directive", "executed", "auto_merge_skipped", "response_received"):
        _entry(config, entry_type, hours_ago=2, data={"task_id": "unrelated"})
    finding = health.held_merge_sweep(config.transcript_file, NOW)

    assert finding is not None and finding.sweeps == 1


# --- co-occurrence: it must not be shadowed by another verdict ----------------


def test_a_held_sweep_rides_along_on_a_verdict_that_already_needs_attention(config):
    """The strand-01 lesson, applied to the next silent failure. An open blocker
    returns from `_judge` long before any late check could fire, and a nine-day
    merge backlog is happy to co-occur with one — so this is carried on EVERY
    verdict, in the field and in the detail, and the operator is still sent to
    the blocker."""
    BlockerStore(config.blockers_dir).record(
        task_id="t-1", kind="task_fatal", code="approved_paths_missing",
        question="task t-1 has no approved_paths", detail="",
        phase="executing", now=NOW.isoformat(timespec="seconds"),
    )
    _held(config, hours_ago=225.8)
    with _alive(config):
        verdict = _check(config)

    assert verdict.code == health.STUCK_BLOCKED, "the blocker keeps its own code"
    assert verdict.needs_attention is True
    assert verdict.held_merge_sweep.unresolved == UNRESOLVED
    assert "audit-0001" in verdict.detail
    assert "approved_paths" in verdict.detail, "and the blocker is still named first"


def test_a_held_sweep_survives_a_loop_that_is_not_running(config):
    """The other early return, and the one the measured case was most likely to
    be sitting under."""
    _held(config, hours_ago=225.8)
    verdict = _check(config)

    assert verdict.code == health.STUCK_NOT_RUNNING
    assert verdict.held_merge_sweep is not None
    assert "audit-0001" in verdict.detail


def test_the_held_sweep_does_not_displace_the_stranded_tasks_it_arrives_with(config):
    """`_with_held_sweep` `replace`s onto the verdict rather than rebuilding it,
    so a field an earlier step decided cannot be dropped by a later one."""
    _held(config, hours_ago=225.8)
    stranded = health.Health(
        code=health.STUCK_STRANDED,
        needs_attention=True,
        summary="autoloop has 1 task(s) stranded in_progress",
        detail="stranded in_progress after an environment fault: t1 (interrupted)",
        stranded_tasks=("t1",),
    )

    verdict = health._with_held_sweep(config, stranded, NOW, 6.0)

    assert verdict.code == health.STUCK_STRANDED, "the stranded task is the sharper call"
    assert verdict.stranded_tasks == ("t1",)
    assert verdict.held_merge_sweep.unresolved == UNRESOLVED
    assert "t1 (interrupted)" in verdict.detail and "audit-0001" in verdict.detail


# --- could not look is not "not held" -----------------------------------------


def test_an_unreadable_transcript_does_not_read_as_no_hold(config):
    """The fail-open this field would otherwise have: a check that silently
    PASSES when what it needs is unavailable. `_strand_survey` applies the same
    rule to the task registry."""
    config.transcript_file.parent.mkdir(parents=True, exist_ok=True)
    config.transcript_file.mkdir()  # a directory where the file should be

    finding = health.held_merge_sweep(config.transcript_file, NOW)
    verdict = _check(config)

    assert finding is not None, "unreadable must not answer 'not held'"
    assert "could not be read" in finding.note
    assert finding.sweeps == 0 and finding.held_hours is None
    assert verdict.needs_attention is True
    assert "could not be read" in verdict.detail
    # And on a verdict that had nothing else to say, it is the whole verdict.
    assert health._with_held_sweep(
        config,
        health.Health(code=health.OK_RUNNING, needs_attention=False, summary="fine"),
        NOW,
        6.0,
    ).summary == "autoloop cannot verify whether the merge sweep is held"


def test_a_hold_whose_age_cannot_be_MEASURED_escalates_rather_than_looking_young(
    config,
):
    """Unknown is not young. A held entry with an unreadable `ts` would
    otherwise fall below every threshold forever — a guard quietly switching
    itself off on the one input it cannot read."""
    _held(config, hours_ago=0, ts="not-a-timestamp")
    with _alive(config):
        verdict = _check(config)

    finding = verdict.held_merge_sweep
    assert finding.held_hours is None
    assert "cannot be measured" in finding.note
    assert verdict.needs_attention is True
    assert verdict.code == health.STUCK_MERGE_BACKLOG


def test_a_future_stamp_reads_as_zero_rather_than_as_a_negative_age(config):
    """A wall clock adjusted backwards is not a hold from the future. Zero is
    the quiet reading and the next check re-judges in minutes — the direction
    `current_round_age_seconds` already takes on skew."""
    _held(config, hours_ago=-3)
    with _alive(config):
        verdict = _check(config)

    assert verdict.held_merge_sweep.held_hours == 0.0
    assert verdict.needs_attention is False


def test_torn_foreign_and_shapeless_lines_are_skipped_one_at_a_time(config):
    """The live loop appends to this file, so the last line can be a partial
    write — and one torn line must not throw away the history in front of it,
    the same rule `transcript.read_records` follows."""
    _held(config, hours_ago=200)
    with open(config.transcript_file, "a", encoding="utf-8") as handle:
        handle.write('"merge_sweep_completed"\n')            # JSON, not an object
        # Past the prefix filter, so the TYPE check is what refuses it.
        handle.write(
            json.dumps({"ts": "x", "type": 7, "data": {"of": "merge_sweep_completed"}})
            + "\n"
        )
        handle.write(
            json.dumps(
                {"ts": NOW.isoformat(), "type": "response_received",
                 "data": {"raw": "the reviewer said merge_sweep_completed"}}
            )
            + "\n"
        )
        handle.write('{"ts": "2026-08-25T11:00:00+00:00", "type": "merge_sweep_hel')

    finding = health.held_merge_sweep(config.transcript_file, NOW)

    assert finding is not None, "a torn tail must not hide the hold in front of it"
    assert finding.sweeps == 1
    assert finding.held_hours == pytest.approx(200.0)


def test_a_held_entry_with_an_unusable_payload_still_counts_as_held(config):
    """A hand-edited or truncated entry names nobody, and naming nobody is not
    evidence that nothing is held. The TYPE decides; the lists only describe."""
    _entry(config, merge_sweep.SWEEP_HELD_EVENT, hours_ago=200, data={"unresolved": "audit-0001"})
    finding = health.held_merge_sweep(config.transcript_file, NOW)

    assert finding is not None
    assert finding.unresolved == () and finding.pending == ()
    assert "(the entry named none)" in finding.describe()


def test_an_idle_entry_with_an_unusable_unresolved_list_clears(config):
    """The mirror of the rule above, and deliberately the other way: an idle
    entry only ever CLEARS unless it names unjudgeable work, so an unreadable
    list there is treated as naming none. It is written by this loop, one line
    per invocation, and reading a garbled one as a hold would invent an alarm
    out of a formatting accident rather than out of a decision."""
    _held(config, hours_ago=200)
    _entry(config, merge_sweep.SWEEP_IDLE_EVENT, hours_ago=1, data={"unresolved": None})

    assert health.held_merge_sweep(config.transcript_file, NOW) is None


# --- the wire ----------------------------------------------------------------


def test_the_verdict_serialises_with_the_held_sweep(config):
    """`to_json` is what the notifier reads; a field the dataclass has and the
    payload does not would be a detector nobody downstream can see."""
    _held(config, hours_ago=225.8)
    with _alive(config):
        payload = json.loads(_check(config).to_json())

    assert payload["code"] == health.STUCK_MERGE_BACKLOG
    assert payload["needs_attention"] is True
    assert payload["held_merge_sweep"]["unresolved"] == list(UNRESOLVED)
    assert payload["held_merge_sweep"]["pending"] == list(QUEUED)
    assert payload["held_merge_sweep"]["held_hours"] == pytest.approx(225.8)
    assert payload["held_merge_sweep"]["sweeps"] == 1


def test_a_quiet_verdict_still_carries_the_key(config):
    """`null`, not a missing key: a reader that has to `.get()` around absence
    cannot tell "not held" from "this build does not report it"."""
    with _alive(config):
        payload = json.loads(_check(config).to_json())

    assert payload["held_merge_sweep"] is None


def test_the_event_names_are_the_ones_already_in_the_transcript(config):
    """Pinned as LITERALS. The 110 `merge_sweep_held` entries written before
    this existed carry these exact names, so a rename both ends agreed on would
    still stop nine days of history being readable — and the reader would go
    quiet rather than fail."""
    assert merge_sweep.SWEEP_HELD_EVENT == "merge_sweep_held"
    assert merge_sweep.SWEEP_IDLE_EVENT == "merge_sweep_nothing_to_do"
    assert merge_sweep.SWEEP_CLEARED_EVENTS == (
        "merge_sweep_completed",
        "merge_sweep_deferred",
        "merge_sweep_stopped",
    )
    assert health.SWEEP_HELD_EVENT is merge_sweep.SWEEP_HELD_EVENT, (
        "one vocabulary, imported — not a second copy that can drift"
    )


# --- the emitter: one terminal entry per invocation, and nothing else changes --


class _FakeGit:
    """Enough git for an invocation that enumerates nothing: the sweep probes
    the checkout, and `_backlog` asks for the head sha. Nothing here reaches a
    remote, which is the point — the entry under test is written before any
    merge could be attempted."""

    def head_sha(self):
        return "0" * 40

    def dirty_files(self):
        return []


class _Task:
    def __init__(self, task_id):
        self.id = task_id


class _CompletedRegistry:
    """Every task it holds is COMPLETED — the only state the sweep enumerates."""

    def __init__(self, task_ids):
        self._tasks = [_Task(task_id) for task_id in task_ids]

    def all_tasks(self):
        return list(self._tasks)

    def state_of(self, task_id):
        return TaskState.COMPLETED


class _RecordingMerger:
    def __init__(self):
        self.attempted = []

    def attempt(self, task_id, seen=None):
        self.attempted.append(task_id)
        return "merged"


def _sweeper(config, task_ids=(), merger=None):
    return BacklogSweeper(
        config=config,
        git=_FakeGit(),
        policy=PolicyEngine(config.policy),
        execution_store=TaskExecutionStore(config.executions_dir),
        registry=_CompletedRegistry(task_ids),
        log=TranscriptLogger(config.transcript_file).append,
        merger=merger or _RecordingMerger(),
    )


def _entries(config, entry_type=None):
    path = config.transcript_file
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if entry_type is None or r["type"] == entry_type]


def test_an_invocation_with_nothing_to_merge_says_so_exactly_once(config):
    """The entry that makes the sequence decidable. One line per INVOCATION —
    per-branch silence is untouched, and a branch already in the base still logs
    nothing at all."""
    merger = _RecordingMerger()

    result = _sweeper(config, merger=merger).sweep()

    assert result.outcome == merge_sweep.NOTHING_TO_DO
    assert result.is_clear is True
    assert merger.attempted == [], "nothing was merged, and nothing was asked"
    idle = _entries(config, merge_sweep.SWEEP_IDLE_EVENT)
    assert len(idle) == 1
    assert idle[0]["data"] == {"unresolved": [], "pending": []}
    assert [r["type"] for r in _entries(config)] == [merge_sweep.SWEEP_IDLE_EVENT]


def test_an_invocation_that_could_not_JUDGE_a_task_names_it_with_an_empty_queue(config):
    """A completed task whose execution record will not load, and no candidate
    to queue behind it. `is_clear` is false, the sweep merged nothing, and the
    terminal entry now says which task and that the queue is empty — the data
    that never left the log."""
    config.executions_dir.mkdir(parents=True, exist_ok=True)
    (config.executions_dir / "audit-0001.json").write_text("{ not json", encoding="utf-8")
    merger = _RecordingMerger()

    result = _sweeper(config, task_ids=("audit-0001",), merger=merger).sweep()

    assert result.outcome == merge_sweep.NOTHING_TO_DO, "unchanged: no queue, no hold"
    assert result.is_clear is False
    assert [t for t, _why in result.unresolved] == ["audit-0001"]
    assert merger.attempted == [], "an unjudgeable task is never merged past"
    assert _entries(config, merge_sweep.SWEEP_HELD_EVENT) == [], (
        "`held` is a claim about branches that were withheld, and there were none"
    )
    idle = _entries(config, merge_sweep.SWEEP_IDLE_EVENT)
    assert idle[0]["data"] == {"unresolved": ["audit-0001"], "pending": []}


def test_what_the_sweep_wrote_is_what_health_reads(config):
    """End to end, with no hand-written transcript in the middle: the sweep
    records its own decision, and `health --json` reports that decision back.
    An assertion over text a test itself wrote would prove nothing about
    either half."""
    config.executions_dir.mkdir(parents=True, exist_ok=True)
    (config.executions_dir / "audit-0001.json").write_text("{ not json", encoding="utf-8")

    _sweeper(config, task_ids=("audit-0001",)).sweep()
    later = datetime.now(timezone.utc) + timedelta(hours=48)
    verdict = health.check(
        config,
        now=later,
        agent_probe=lambda: False,
        sleep_probe=lambda start, end: health.SleepEvidence(0.0, "test"),
    )

    assert verdict.needs_attention is True
    assert verdict.held_merge_sweep.unresolved == ("audit-0001",)
    assert verdict.held_merge_sweep.held_hours == pytest.approx(48.0, abs=0.1)
    assert "audit-0001" in verdict.detail
