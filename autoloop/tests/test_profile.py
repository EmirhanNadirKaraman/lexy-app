"""Measured durations, and the read-only `profile` command over them.

Covers the two halves of prof-01 separately, because they fail differently.

The WRITE half is `transcript.Stopwatch`: it must produce a duration that
matches a controlled elapsed time exactly, and it must be inert on every
failure — a clock that raises, a clock that runs backwards, a second `stop()`.
"Inert" means the timed operation's own result is byte-for-byte what it would
have been, with the duration key simply absent, which is the same shape as
every record written before durations existed.

The READ half is `profile_stages` / `render_profile`: pure functions over a
list of records. The transcript they read in production is append-only history
with 7,203 records that will never carry a duration, so the tests below assert
the DEGRADATION explicitly — a legacy transcript still reports, labelled
gap-derived, and a mixed one never puts a measured number and an inferred one
in the same column.
"""

import json
from argparse import Namespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autoloop import cli
from autoloop.lock import LoopLock
from autoloop.transcript import (
    DURATION_KEY,
    GAP_DERIVED,
    MEASURED,
    Stopwatch,
    format_seconds,
    measured_duration,
    profile_stages,
    read_records,
    render_profile,
    summarize,
)

URL = "https://chatgpt.com/c/abc"
BASE = datetime(2026, 8, 20, 10, 0, 0, tzinfo=timezone.utc)


def at(seconds: float) -> str:
    """A transcript `ts`, in exactly the form `state.utcnow_iso` writes it —
    tz-aware UTC at ONE-SECOND resolution. The resolution is the point: it is
    half the reason a gap-derived number is not a measurement."""
    return (BASE + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def rec(rtype, seconds, request_id=None, duration=None, **data):
    entry = {"ts": at(seconds), "type": rtype}
    if request_id is not None:
        entry["request_id"] = request_id
    payload = dict(data)
    if duration is not None:
        payload[DURATION_KEY] = duration
    if payload:
        entry["data"] = payload
    return entry


def write_transcript(path: Path, records, trailing="") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for entry in records:
            fh.write(json.dumps(entry) + "\n")
        if trailing:
            fh.write(trailing)
    return path


class FakeClock:
    """Returns each reading in turn, then repeats the last one forever."""

    def __init__(self, *readings):
        self.readings = list(readings)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if len(self.readings) > 1:
            return self.readings.pop(0)
        return self.readings[0]


class ExplodingClock:
    def __init__(self, fail_on=1):
        self.fail_on = fail_on
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls >= self.fail_on:
            raise RuntimeError("the clock is on fire")
        return 100.0


def stage_named(profiles, name):
    return next(p for p in profiles if p.stage.name == name)


# ---- the write half: Stopwatch ---------------------------------------------


def test_a_completed_operation_records_the_controlled_elapsed_time():
    watch = Stopwatch(FakeClock(1000.0, 1002.5))
    assert watch.stamp({"result": "ok"}) == {"result": "ok", DURATION_KEY: 2.5}


def test_stamp_leaves_the_operations_own_data_untouched():
    original = {"result": "ok", "prompt": "the packet"}
    stamped = Stopwatch(FakeClock(1.0, 3.0)).stamp(original)
    assert original == {"result": "ok", "prompt": "the packet"}  # not mutated
    assert stamped["result"] == "ok" and stamped["prompt"] == "the packet"


def test_first_stop_wins_so_a_restamped_event_does_not_grow():
    watch = Stopwatch(FakeClock(0.0, 4.0, 90.0))
    assert watch.stop() == 4.0
    assert watch.stop() == 4.0
    assert watch.stamp({})[DURATION_KEY] == 4.0


def test_a_backwards_clock_records_nothing_rather_than_a_negative():
    watch = Stopwatch(FakeClock(500.0, 490.0))
    assert watch.stop() is None
    assert watch.stamp({"status": "ok"}) == {"status": "ok"}


def test_a_backwards_reading_is_latched_like_any_other_first_stop():
    watch = Stopwatch(FakeClock(500.0, 490.0, 600.0))
    assert watch.stop() is None
    # The second reading would have produced a plausible 100s. First stop wins
    # for failures too, so a latched "unknown" never turns into a number.
    assert watch.stop() is None


def test_a_failure_inside_the_timing_path_never_reaches_the_operation():
    # The clock raises on construction AND on stop. Neither escapes, and the
    # operation's own payload comes back exactly as it went in.
    payload = {"status": "ok", "summary": "task 't1' implemented"}
    watch = Stopwatch(ExplodingClock(fail_on=1))
    assert watch.stop() is None
    assert watch.stamp(payload) == payload


def test_a_clock_that_fails_only_on_stop_records_nothing():
    watch = Stopwatch(ExplodingClock(fail_on=2))
    assert watch.stop() is None
    assert DURATION_KEY not in watch.stamp({"status": "ok"})


def test_a_non_finite_reading_records_nothing():
    assert Stopwatch(FakeClock(float("nan"), 5.0)).stop() is None
    assert Stopwatch(FakeClock(0.0, float("inf"))).stop() is None


def test_stamp_of_none_data_is_just_the_duration():
    assert Stopwatch(FakeClock(0.0, 1.25)).stamp(None) == {DURATION_KEY: 1.25}


# ---- reading a duration off a record ---------------------------------------


def test_measured_duration_refuses_everything_that_is_not_a_number():
    assert measured_duration(rec("executed", 0, duration=3.0)) == 3.0
    assert measured_duration(rec("executed", 0)) is None
    assert measured_duration(rec("executed", 0, duration="3.0")) is None
    assert measured_duration(rec("executed", 0, duration=True)) is None
    assert measured_duration(rec("executed", 0, duration=-1.0)) is None
    assert measured_duration({"type": "executed", "data": "not a dict"}) is None
    assert measured_duration({"type": "executed"}) is None


# ---- statistics -------------------------------------------------------------


def test_summarize_reports_count_median_mean_p90_and_total():
    stats = summarize([10, 1, 2, 3, 4, 5, 6, 7, 8, 9])
    assert stats.count == 10
    assert stats.median == 5.5
    assert stats.mean == 5.5
    assert stats.p90 == 9.0  # nearest-rank: ceil(0.9*10) = 9th smallest
    assert stats.total == 55.0


def test_summarize_of_an_odd_set_takes_the_middle_value():
    stats = summarize([4.0, 1.0, 100.0])
    assert stats.median == 4.0
    assert stats.p90 == 100.0
    assert stats.count == 3


def test_summarize_of_nothing_is_none_not_zero():
    assert summarize([]) is None


def test_format_seconds_scales_to_the_magnitude():
    assert format_seconds(0.25) == "0.25s"
    assert format_seconds(42.0) == "42.0s"
    assert format_seconds(150.0) == "2m30s"
    assert format_seconds(25200.0) == "7.0h"


# ---- reading the transcript file -------------------------------------------


def test_read_records_skips_only_the_unusable_lines(tmp_path):
    path = tmp_path / "transcript.jsonl"
    write_transcript(
        path,
        [rec("request_prepared", 0, "r1"), rec("request_submitted", 5, "r1")],
        # A blank line, a non-object, a line with no type, and the partial
        # write the live loop leaves at the tail of a file being appended to.
        trailing='\n"just a string"\n{"ts": "x"}\n{"ts": "y", "type": "exec',
    )
    read = read_records(path)
    assert [r["type"] for r in read.records] == ["request_prepared", "request_submitted"]
    assert read.skipped == 3


def test_read_records_of_a_missing_file_is_empty_not_an_error(tmp_path):
    read = read_records(tmp_path / "nope.jsonl")
    assert read.records == () and read.skipped == 0


# ---- profiling: measured ----------------------------------------------------


MEASURED_ROUND = [
    rec("request_prepared", 0, "r1", duration=0.5, chars=100),
    rec("request_submitted", 30, "r1", duration=12.0, result="ok"),
    rec("response_received", 300, "r1", raw="..."),
    rec("directive", 301, "r1", decision="implement"),
    rec("executed", 900, "r1", duration=590.0, status="ok"),
    rec("request_prepared", 1000, "r2", duration=1.5, chars=100),
    rec("request_submitted", 1010, "r2", duration=4.0, result="ok"),
    rec("response_received", 1200, "r2", raw="..."),
    rec("directive", 1201, "r2", decision="revise"),
    rec("executed", 1500, "r2", duration=298.0, status="ok"),
]


def test_profile_reports_measured_stats_per_stage():
    profiles = profile_stages(MEASURED_ROUND)
    prepare = stage_named(profiles, "prepare").measured
    assert (prepare.count, prepare.median, prepare.total) == (2, 1.0, 2.0)
    execute = stage_named(profiles, "execute").measured
    assert execute.count == 2
    assert execute.median == 444.0
    assert execute.mean == 444.0
    assert execute.p90 == 590.0
    assert execute.total == 888.0


def test_executed_pairs_with_its_directive_and_the_pair_is_timeable():
    """`executed` now carries the directive's request_id, so the historical
    pair is finally computable. Both sets are populated for the same two
    requests: the measured run, and the wider window that contains it."""
    execute = stage_named(profile_stages(MEASURED_ROUND), "execute")
    assert execute.measured.count == 2 and execute.measured.total == 888.0
    assert execute.gap.count == 2
    assert execute.gap.total == 599.0 + 299.0
    # The gap CONTAINS the measured run, which is the whole reason both are
    # worth printing — the difference is what the window swallowed.
    assert execute.gap.total > execute.measured.total


def test_a_measured_record_still_contributes_its_window_to_the_gap_set():
    """The two sets are independent, not mutually exclusive. A modern round
    reports its own elapsed time AND its timestamp window; neither suppresses
    the other. Here both rounds are measured and both windows pair, so the two
    counts happen to match — they are not required to, and in `MIXED` they do
    not."""
    submit = stage_named(profile_stages(MEASURED_ROUND), "submit")
    assert submit.measured.count == 2 and submit.measured.total == 16.0
    assert submit.gap.count == 2
    assert submit.gap.total == 30.0 + 10.0


def test_review_wait_stays_gap_derived_even_in_a_measured_transcript():
    wait = stage_named(profile_stages(MEASURED_ROUND), "review_wait")
    assert wait.measured is None  # there is no operation to measure — it IS the gap
    assert wait.gap.count == 2
    assert wait.gap.total == 270.0 + 190.0
    assert "GAP BY NATURE" in wait.stage.measured_note


# ---- profiling: legacy records, no duration anywhere ------------------------


LEGACY_ROUND = [
    rec("request_prepared", 0, "r1", chars=100),
    rec("browser_error", 10, phase="submitting", error="boom"),
    rec("browser_restarted", 20, ok=True),
    rec("request_submitted", 400, "r1", result="ok"),
    rec("response_received", 700, "r1", raw="..."),
    rec("directive", 701, "r1", decision="implement"),
    # The pre-prof-01 shape: no request_id at all on `executed`.
    rec("executed", 1300, status="ok"),
]


def test_legacy_records_still_report_and_are_labelled_gap_derived():
    profiles = profile_stages(LEGACY_ROUND)
    submit = stage_named(profiles, "submit")
    assert submit.measured is None
    assert submit.gap.count == 1 and submit.gap.total == 400.0
    text = render_profile(Path("/tmp/t.jsonl"), read_records(Path("/nope")), profiles)
    assert GAP_DERIVED in text
    assert "browser_error" in text  # the gap note says what is inside the window


def test_the_gap_window_is_the_interval_not_the_work():
    """The measured motivation, in miniature: 400 seconds of `submit` gap that
    contains two faults. The profiler reports the interval and says so; it does
    not pretend the packet took 400 seconds to build."""
    submit = stage_named(profile_stages(LEGACY_ROUND), "submit")
    assert submit.gap.total == 400.0
    assert "browser_error" in submit.stage.gap_note


def test_legacy_executed_cannot_be_paired_at_all():
    execute = stage_named(profile_stages(LEGACY_ROUND), "execute")
    assert execute.measured is None
    assert execute.gap is None  # no request_id on `executed` before prof-01


def test_prepare_has_no_historical_pair_and_says_so():
    prepare = stage_named(profile_stages(LEGACY_ROUND), "prepare")
    assert prepare.measured is None and prepare.gap is None
    assert prepare.stage.gap_start == "" and prepare.stage.gap_end == ""
    assert "no historical pair exists" in prepare.stage.gap_note


def test_render_over_legacy_records_never_prints_a_measured_number():
    text = render_profile(
        Path("/tmp/t.jsonl"), read_records(Path("/nope")), profile_stages(LEGACY_ROUND)
    )
    for line in text.splitlines():
        if line.strip().startswith(MEASURED) and "median" in line:
            raise AssertionError(f"a measured statistic appeared: {line!r}")


# ---- profiling: a MIXED transcript -----------------------------------------


MIXED = LEGACY_ROUND + [
    rec("request_prepared", 2000, "r2", duration=0.5),
    rec("request_submitted", 2200, "r2", duration=9.0, result="ok"),
    rec("response_received", 2400, "r2", raw="..."),
    rec("directive", 2401, "r2", decision="implement"),
    rec("executed", 2900, "r2", duration=480.0, status="ok"),
]


def test_mixed_transcript_keeps_the_two_sources_in_separate_sets():
    submit = stage_named(profile_stages(MIXED), "submit")
    # Only r2 recorded its own elapsed time — 9.0s of real transport work.
    # BOTH windows are still pairable, so the gap set holds r1's 400s (which no
    # measurement can explain) and r2's 200s (which one can). Separate SETS,
    # not separate populations: nothing is dropped to keep them apart.
    assert submit.measured.count == 1 and submit.measured.total == 9.0
    assert submit.gap.count == 2 and submit.gap.total == 400.0 + 200.0


def test_a_measured_stage_is_never_folded_into_its_own_gap_number():
    """The same request, both ways, on one stage: 480s of measured executor
    work inside a 499s window. Two numbers, two rows, neither adjusted by the
    other — the difference is the loop's own overhead around the round."""
    execute = stage_named(profile_stages(MIXED), "execute")
    assert execute.measured.count == 1 and execute.measured.total == 480.0
    assert execute.gap.count == 1 and execute.gap.total == 499.0
    assert execute.gap.total > execute.measured.total


def test_mixed_transcript_never_renders_the_two_sources_as_one_column():
    text = render_profile(
        Path("/tmp/t.jsonl"), read_records(Path("/nope")), profile_stages(MIXED)
    )
    stat_lines = [ln for ln in text.splitlines() if "median" in ln and "total" in ln]
    assert stat_lines, "expected at least one statistics line"
    for line in stat_lines:
        labels = [label for label in (MEASURED, GAP_DERIVED) if label in line]
        assert len(labels) == 1, f"a statistics line carried both sources: {line!r}"
    assert "The two are never combined into one number." in text


def test_the_report_says_not_to_sum_the_two_rows():
    """Independent sets over the same history means a round can appear in both
    rows, so the one arithmetic that would be wrong has to be named. Printing
    it beats deduplicating: dropping a modern round's window would throw away
    the comparison the pair exists for."""
    text = render_profile(
        Path("/tmp/t.jsonl"), read_records(Path("/nope")), profile_stages(MIXED)
    )
    assert "Do not sum them" in text
    assert "independent sample sets" in text
    assert "round can appear in both rows" in text


# ---- incomplete, malformed and unrelated records ---------------------------


def test_an_unfinished_stage_contributes_nothing():
    records = [
        rec("request_prepared", 0, "r1"),          # never submitted
        rec("request_submitted", 10, "r2"),        # never prepared
        rec("request_prepared", 20, "r3"),
        rec("request_submitted", 33, "r3"),
    ]
    submit = stage_named(profile_stages(records), "submit")
    assert submit.gap.count == 1 and submit.gap.total == 13.0


def test_the_first_completed_window_wins_per_request():
    records = [
        rec("request_prepared", 0, "r1"),
        rec("request_submitted", 5, "r1"),
        rec("request_submitted", 900, "r1"),  # a resend after a rotation
    ]
    assert stage_named(profile_stages(records), "submit").gap.total == 5.0


def test_unrelated_and_malformed_records_are_ignored():
    records = [
        {"type": "browser_error", "ts": at(0)},
        {"type": "request_prepared"},                       # no ts
        {"type": "request_prepared", "ts": "not-a-time", "request_id": "r1"},
        {"type": "request_submitted", "ts": at(9), "request_id": "r1"},
        rec("heartbeat", 3),
    ]
    profiles = profile_stages(records)
    assert all(p.measured is None and p.gap is None for p in profiles)


def test_a_backwards_pair_is_dropped_rather_than_counted_as_zero():
    records = [rec("request_prepared", 100, "r1"), rec("request_submitted", 40, "r1")]
    assert stage_named(profile_stages(records), "submit").gap is None


def test_an_empty_transcript_profiles_to_nothing_without_raising():
    profiles = profile_stages([])
    assert len(profiles) == 4
    assert all(p.measured is None and p.gap is None for p in profiles)


# ---- the CLI command --------------------------------------------------------


def write_config_toml(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[browser]\nconversation_url = "{URL}"\n\n'
        f'[paths]\nstate_dir = "{tmp_path / ".al"}"\n'
        f'workers_root = "{tmp_path / "workers_root"}"\n',
        encoding="utf-8",
    )
    return path


def snapshot_dir(root: Path):
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_the_command_is_wired_into_the_parser():
    args = cli.build_parser().parse_args(["profile"])
    assert args.func is cli._cmd_profile


def test_profile_command_prints_per_stage_timing(tmp_path, capsys):
    config_path = write_config_toml(tmp_path)
    write_transcript(tmp_path / ".al" / "transcript.jsonl", MIXED)
    assert cli._cmd_profile(Namespace(config=config_path)) == 0
    out = capsys.readouterr().out
    for stage in ("prepare", "submit", "review_wait", "execute"):
        assert stage in out
    assert MEASURED in out and GAP_DERIVED in out
    # All five statistics reach stdout, not just the ones a stage name implies.
    for label in ("n=", "median", "mean", "p90", "total"):
        assert label in out


def test_profile_command_without_a_transcript_says_so(tmp_path, capsys):
    config_path = write_config_toml(tmp_path)
    (tmp_path / ".al").mkdir(parents=True, exist_ok=True)
    assert cli._cmd_profile(Namespace(config=config_path)) == 0
    assert "no transcript at" in capsys.readouterr().out


def test_profile_command_over_an_all_legacy_transcript(tmp_path, capsys):
    config_path = write_config_toml(tmp_path)
    write_transcript(tmp_path / ".al" / "transcript.jsonl", LEGACY_ROUND)
    assert cli._cmd_profile(Namespace(config=config_path)) == 0
    out = capsys.readouterr().out
    assert GAP_DERIVED in out
    assert "every record here predates measured durations" in out


def test_transcript_override_profiles_an_archived_file(tmp_path, capsys):
    """The 7,203-record history that motivated this is an ARCHIVE, not the
    file the running loop appends to. `--transcript` points the same reader at
    it; the configured transcript is left alone."""
    config_path = write_config_toml(tmp_path)
    write_transcript(tmp_path / ".al" / "transcript.jsonl", MEASURED_ROUND)
    archived = write_transcript(tmp_path / "archive" / "old.jsonl", LEGACY_ROUND)
    args = cli.build_parser().parse_args(
        ["profile", "--config", str(config_path), "--transcript", str(archived)]
    )
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert str(archived) in out
    assert "every record here predates measured durations" in out


def test_transcript_override_of_a_missing_file_says_so(tmp_path, capsys):
    config_path = write_config_toml(tmp_path)
    write_transcript(tmp_path / ".al" / "transcript.jsonl", MIXED)
    missing = tmp_path / "archive" / "gone.jsonl"
    assert cli._cmd_profile(Namespace(config=config_path, transcript=missing)) == 0
    assert f"no transcript at {missing}" in capsys.readouterr().out


def test_transcript_override_of_a_non_transcript_prints_no_content(tmp_path, capsys):
    """The flag widens WHICH file is read and nothing else. Pointed at a file
    that is not a transcript, the reader finds no usable record and the command
    says so — it never falls back to printing what it read (SECURITY.md S36)."""
    config_path = write_config_toml(tmp_path)
    junk = tmp_path / "notes.txt"
    junk.write_text("SECRET-NOT-A-TRANSCRIPT\n", encoding="utf-8")
    assert cli._cmd_profile(Namespace(config=config_path, transcript=junk)) == 0
    out = capsys.readouterr().out
    assert "no usable records" in out
    assert "SECRET-NOT-A-TRANSCRIPT" not in out


def test_profile_runs_under_a_live_lock_and_mutates_nothing(tmp_path, capsys):
    """The read-only guarantee `status`/`tasks`/`blockers` already give.

    A live lock is exactly the condition under which an operator wants this —
    the loop is running, the transcript is growing, and the question is where
    the time is going. A command that took the lock could not answer it.
    """
    config_path = write_config_toml(tmp_path)
    state_dir = tmp_path / ".al"
    write_transcript(state_dir / "transcript.jsonl", MIXED)
    lock = LoopLock(state_dir).acquire()
    try:
        before = snapshot_dir(state_dir)
        assert cli._cmd_profile(Namespace(config=config_path)) == 0
        assert snapshot_dir(state_dir) == before
    finally:
        lock.release()
    assert "transcript" in capsys.readouterr().out


def test_profile_prints_only_aggregates_never_a_record_body(tmp_path, capsys):
    """The transcript carries whole review packets and whole reviewer replies.
    `profile` reads that file; it must never put any of it on stdout."""
    config_path = write_config_toml(tmp_path)
    write_transcript(
        tmp_path / ".al" / "transcript.jsonl",
        [
            rec("request_prepared", 0, "r1", duration=0.5),
            rec("request_submitted", 5, "r1", duration=2.0, prompt="SECRET-PACKET-BODY"),
            rec("response_received", 60, "r1", raw="SECRET-REVIEWER-REPLY"),
        ],
    )
    cli._cmd_profile(Namespace(config=config_path))
    out = capsys.readouterr().out
    assert "SECRET-PACKET-BODY" not in out
    assert "SECRET-REVIEWER-REPLY" not in out
