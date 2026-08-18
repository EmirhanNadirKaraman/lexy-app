"""Unlimited review rounds (guarded by convergence) and browser auto-restart.

Both exist because of the same session: three browser stalls each cleared by
restarting one Chrome profile by hand, and an audit abandoned at a hard cap of
2 while it was arguably still converging.

The last two sections cover interactions with the failure budget: the restart
cooldown, where the two guards used to cancel each other out, and ChatGPT's
account-level rate limit, where the browser recovery itself was the mechanism
of the failure.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from autoloop.blockers import BlockerStore
from autoloop.config import BrowserConfig
from autoloop.contract import Decision, Directive
from autoloop.errors import RateLimitedError, SessionLostError
from autoloop.orchestrator import Orchestrator
from autoloop.policy import PolicyConfig
from autoloop.state import Phase
from autoloop.worktask import TaskExecution

from test_orchestrator import build


def _orch():
    orch = Orchestrator.__new__(Orchestrator)
    orch._logged = []
    orch._log = lambda event, **kw: orch._logged.append((event, kw.get("data") or {}))
    orch._last_browser_restart = None
    return orch


def _execution(**kw):
    return TaskExecution(task_id="t1", task_branch="autoloop/t1",
                         worktree_path="/tmp/w", task_base_sha="a" * 40, **kw)


def revise(text):
    return Directive(decision=Decision.REVISE, reason="r", task_id="t1", feedback=text)


# ---- review rounds -----------------------------------------------------------


def test_the_round_cap_defaults_to_unlimited():
    """A hard 2 abandoned work that was still converging. The operator decides
    when to give up, not this constant."""
    assert PolicyConfig().max_review_rounds == 0


def test_identical_feedback_twice_is_what_stops_an_unlimited_loop():
    orch = _orch()
    execution = _execution(review_round=7)
    execution.last_revise_feedback = orch._normalise_feedback("Fix the duplicate findings.")

    assert orch._revise_feedback_is_unchanged(execution, revise("Fix the duplicate findings."))
    # Whitespace and case are noise, not a new request.
    assert orch._revise_feedback_is_unchanged(execution, revise("  fix   THE Duplicate\nfindings.  "))


def test_genuinely_different_feedback_always_gets_another_round():
    """The guard must not be fuzzy: two different complaints deserve a round
    each, however many have already run."""
    orch = _orch()
    execution = _execution(review_round=99)
    execution.last_revise_feedback = orch._normalise_feedback("Fix the duplicate findings.")

    assert not orch._revise_feedback_is_unchanged(execution, revise("Now fix the file scopes."))


def test_a_non_revise_directive_is_never_treated_as_repeated():
    orch = _orch()
    execution = _execution(review_round=3)
    execution.last_revise_feedback = "anything"
    push = Directive(decision=Decision.PUSH, reason="ok", task_id="t1")
    assert not orch._revise_feedback_is_unchanged(execution, push)


def test_empty_feedback_never_trips_the_guard():
    """Absent feedback is not evidence of repetition — stopping on it would
    abandon a task for saying nothing."""
    orch = _orch()
    execution = _execution(review_round=3)
    execution.last_revise_feedback = ""
    assert not orch._revise_feedback_is_unchanged(execution, revise(""))


# ---- browser auto-restart ----------------------------------------------------


def _with_browser(orch, **kw):
    class _Cfg:
        browser = BrowserConfig(conversation_url="https://chatgpt.com/c/x", **kw)
    orch._config = _Cfg()
    return orch


def test_no_restart_command_means_the_old_behaviour(monkeypatch):
    orch = _with_browser(_orch())
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    assert orch._attempt_browser_restart() is False
    assert called == [], "nothing may be executed when none is configured"


def test_a_configured_command_runs_and_reports_success(monkeypatch):
    orch = _with_browser(_orch(), restart_command=("true",))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="up", stderr=""),
    )
    assert orch._attempt_browser_restart() is True
    assert any(e == "browser_restarted" for e, _ in orch._logged)


def test_the_cooldown_stops_a_restart_loop(monkeypatch):
    """Without it a genuinely dead transport thrashes the browser instead of
    surfacing the fault."""
    orch = _with_browser(_orch(), restart_command=("true",), restart_cooldown_seconds=600.0)
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout="", stderr=""),
    )
    assert orch._attempt_browser_restart() is True
    assert orch._attempt_browser_restart() is False, "second attempt must be refused"
    assert any(e == "browser_restart_skipped" for e, _ in orch._logged)


def test_a_failing_restart_is_reported_not_raised(monkeypatch):
    orch = _with_browser(_orch(), restart_command=("false",))
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="", stderr="boom"),
    )
    assert orch._attempt_browser_restart() is False
    assert any(e == "browser_restart_failed" for e, _ in orch._logged)


def test_a_restart_command_that_cannot_run_never_escapes(monkeypatch):
    """A broken restart command must not become a new crash on top of the
    browser fault it was meant to fix."""
    orch = _with_browser(_orch(), restart_command=("nope",))

    def boom(*a, **k):
        raise OSError("no such file")

    monkeypatch.setattr(subprocess, "run", boom)
    assert orch._attempt_browser_restart() is False
    assert any(e == "browser_restart_failed" for e, _ in orch._logged)


# ---- cooldown vs failure budget ----------------------------------------------
#
# Observed 2026-08-04: four consecutive browser failures each logged
# `browser_restart_skipped {reason: within cooldown}`, so the loop could not
# restart Chrome — the one action that would have fixed the hang — while those
# same failures spent the 3-consecutive-failure budget. The budget ran out
# before the cooldown did and the session ended `failed` with no blocker
# record. Both guards are wanted; their interaction was the defect.


def _browser_orch(tmp_path, policy=None, state=None, **browser_kw):
    """A real Orchestrator (state store, policy engine, transcript) whose
    browser config carries the restart settings under test.

    `state` stands in for a SECOND process over the same state directory: pass
    the state a previous orchestrator left on disk and this one resumes from
    it, exactly as `run` does after a crash.
    """
    orch, _, _, _, _, _, _ = build(tmp_path, policy=policy, state=state)
    orch._config = dataclasses.replace(
        orch._config,
        browser=dataclasses.replace(orch._config.browser, **browser_kw),
    )
    return orch


def _fake_restart(monkeypatch, returncode):
    """Record every restart the loop actually executes."""
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, returncode, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _transcript(orch):
    """(event, data) pairs the orchestrator has logged, read back from disk."""
    path = orch._config.transcript_file
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        entries.append((record.get("type"), record.get("data") or {}))
    return entries


def test_a_failure_skipped_for_the_cooldown_does_not_spend_the_budget(tmp_path, monkeypatch):
    """A restart that was never attempted is not evidence that restarting
    fails — so the failure it could not act on must not count toward the
    budget that decides the transport is hopeless."""
    orch = _browser_orch(
        tmp_path, restart_command=("true",), restart_cooldown_seconds=600.0
    )
    calls = _fake_restart(monkeypatch, returncode=0)

    # The first failure gets a real restart, which starts the cooldown.
    orch._handle_browser_failure(Phase.SUBMITTING, SessionLostError("wedged"))
    assert len(calls) == 1
    assert orch.state.consecutive_failures == 0

    # The next three are refused for the cooldown: nothing is tried, so
    # nothing is charged.
    for _ in range(3):
        orch._handle_browser_failure(Phase.SUBMITTING, SessionLostError("wedged"))
    assert len(calls) == 1, "the cooldown must still stop a restart loop"
    assert orch.state.consecutive_failures == 0
    assert orch.state.browser_restart_skips == 3
    assert orch.state.phase == Phase.READY.value, "the loop keeps retrying"
    assert any(
        data.get("recovered") == "restart_skipped_cooldown"
        for event, data in _transcript(orch)
        if event == "browser_error"
    ), "the failure stays visible in the transcript even though it is not charged"


def test_a_restart_that_ran_and_failed_still_spends_the_budget(tmp_path, monkeypatch):
    """The budget must keep working for real breakage: a restart that actually
    ran and did not fix anything is exactly the evidence it exists to count."""
    orch = _browser_orch(
        tmp_path,
        policy=PolicyConfig(max_consecutive_failures=1),
        restart_command=("false",),
        restart_cooldown_seconds=0.0,  # every failure gets a genuine attempt
    )
    calls = _fake_restart(monkeypatch, returncode=1)

    orch._handle_browser_failure(Phase.SUBMITTING, SessionLostError("still down"))
    assert orch.state.consecutive_failures == 1
    assert orch.state.phase == Phase.READY.value

    orch._handle_browser_failure(Phase.SUBMITTING, SessionLostError("still down"))
    assert orch.state.consecutive_failures == 2
    assert orch.state.phase == Phase.FAILED.value
    assert orch.state.resume_phase == Phase.SUBMITTING.value
    assert len(calls) == 2, "both failures were genuinely acted on"


def test_no_restart_command_configured_still_spends_the_budget(tmp_path):
    """The exemption is for the cooldown ALONE. With no restart command there
    is nothing to try later either, so the default configuration's failure
    budget must stay reachable."""
    orch = _browser_orch(tmp_path, policy=PolicyConfig(max_consecutive_failures=1))

    orch._handle_browser_failure(Phase.SUBMITTING, SessionLostError("down"))
    orch._handle_browser_failure(Phase.SUBMITTING, SessionLostError("down"))
    assert orch.state.consecutive_failures == 2
    assert orch.state.phase == Phase.FAILED.value


def test_ending_on_skipped_restarts_parks_naming_the_cooldown(tmp_path, monkeypatch):
    """The exemption is bounded, and the bound ends in a blocker an operator
    can read — not a terminal phase whose cause only the transcript holds."""
    blockers = BlockerStore(tmp_path / "blockers")
    orch = _browser_orch(
        tmp_path,
        policy=PolicyConfig(max_browser_restart_skips=2),
        restart_command=("true",),
        restart_cooldown_seconds=600.0,
    )
    orch._blocker_store = blockers
    orch.state.phase = Phase.AWAITING.value  # where the run loop would be
    _fake_restart(monkeypatch, returncode=0)

    # One real restart, then three failures the cooldown refuses — one past
    # the skip budget.
    for _ in range(4):
        orch._handle_browser_failure(Phase.AWAITING, SessionLostError("wedged"))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.phase != Phase.FAILED.value
    assert orch.state.consecutive_failures == 0, "still never charged"
    assert orch.state.resume_phase == Phase.AWAITING.value
    assert orch.state.park_kind == "loop_fatal"

    open_blockers = blockers.open_blockers()
    assert len(open_blockers) == 1, "a session ending this way records a reason"
    parked = open_blockers[0]
    assert parked.code == "browser_restart_cooldown_blocked"
    assert parked.id == orch.state.park_blocker_id
    assert parked.phase == Phase.AWAITING.value
    assert "restart_cooldown_seconds" in parked.question
    assert "600" in parked.question, "the operator is told which number to look at"
    assert parked.question == orch.state.question


def test_a_real_restart_clears_the_skip_count(tmp_path, monkeypatch):
    """The skip counter describes ONE cooldown window. Once a restart actually
    runs, whatever the cooldown refused before it is settled — otherwise skips
    from unrelated windows would accumulate into a park."""
    orch = _browser_orch(
        tmp_path, restart_command=("true",), restart_cooldown_seconds=600.0
    )
    _fake_restart(monkeypatch, returncode=0)

    orch._handle_browser_failure(Phase.SUBMITTING, SessionLostError("wedged"))
    orch._handle_browser_failure(Phase.SUBMITTING, SessionLostError("wedged"))
    assert orch.state.browser_restart_skips == 1

    # The cooldown has elapsed (modelled by clearing the stamp), so the next
    # failure gets a genuine restart.
    orch._last_browser_restart = None
    orch._handle_browser_failure(Phase.SUBMITTING, SessionLostError("wedged"))
    assert orch.state.browser_restart_skips == 0
    assert orch.state.consecutive_failures == 0


def test_restart_command_must_be_a_list_of_strings(tmp_path):
    from autoloop.config import load_config
    from autoloop.errors import ConfigError

    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[browser]\nconversation_url = "https://chatgpt.com/c/x"\n'
        'restart_command = "restart.sh"\n\n'
        f'[paths]\nworkers_root = "{tmp_path / "w"}"\n', encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="list of strings"):
        load_config(cfg)


# ---- account rate limit vs the browser recovery -------------------------------
#
# Observed overnight 2026-08-14/15. ChatGPT throttled the account and put up its
# "Too many requests" overlay; the loop, having no selector for it, saw only a
# composer that would not take a click, called that a lost session, restarted
# Chrome and retried — from 07:56 onward. Restarting and retrying IS what
# generates requests too quickly, so it deepened the condition it was failing on
# and reported the deepening as further browser failures. pkt-03 burned through
# its five-attempt ceiling without ever reaching an approved review.
#
# A restart cannot help: the limit is account-level and server-side. So this
# fault must reach NONE of the browser recovery — not the restart, not the
# failure budget, not even the client drop (re-attaching navigates, and a
# navigation is another request).


def _sleeps(orch):
    """Record the back-offs instead of taking them."""
    taken = []
    orch._sleep = taken.append
    return taken


class _ThrottleAwareClient:
    """A transport whose overlay can be dismissed while the LIMIT holds.

    This is the distinction the whole escalation turns on. Dismissing the
    modal always succeeds — it is a click on a page the loop already has — and
    proves nothing: the limit is server-side and answers to a timer. A fake
    whose `is_rate_limited()` went permanently False after a dismissal would
    model the limit LIFTING, and would let a reset-on-dismissal
    implementation pass while, in production, the delay never doubled and the
    loop never parked.
    """

    def __init__(self):
        self.dismissals = 0
        self.closed = False

    def dismiss_rate_limit_modal(self):
        self.dismissals += 1
        return True  # the overlay is gone; the limit is not

    def is_rate_limited(self):
        return True  # still throttled, whatever the modal is doing

    def close(self):
        self.closed = True


def test_a_rate_limit_never_restarts_the_browser(tmp_path, monkeypatch):
    """The restart is not merely useless here, it is the mechanism of the
    failure: a fresh browser meets the same server-side wall and adds one more
    request to the window that caused it."""
    orch = _browser_orch(
        tmp_path, restart_command=("true",), restart_cooldown_seconds=0.0
    )
    calls = _fake_restart(monkeypatch, returncode=0)
    _sleeps(orch)

    orch._handle_rate_limited(Phase.SUBMITTING, RateLimitedError("throttled"))

    assert calls == [], "the one recovery that deepens this must not run"
    assert orch.state.browser_restart_skips == 0


def test_a_rate_limit_does_not_spend_the_failure_budget(tmp_path):
    """Same principle as the cooldown-skipped restart above: a failure nobody
    could have recovered from must not be charged to the budget that decides
    recovery is hopeless."""
    orch = _browser_orch(tmp_path, policy=PolicyConfig(max_consecutive_failures=1))
    _sleeps(orch)
    orch.state.phase = Phase.AWAITING.value

    for _ in range(4):
        orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert orch.state.consecutive_failures == 0
    assert orch.state.phase == Phase.AWAITING.value, "the phase is re-entered, not failed"
    assert orch.state.rate_limit_backoffs == 4


def test_the_client_is_kept_so_the_wait_costs_no_further_requests(tmp_path):
    """Every other browser handler drops the client. Here that is wrong:
    re-attaching navigates, and the page is already in the right place — the
    modal is dismissed where it stands."""
    client = _ThrottleAwareClient()
    orch = _browser_orch(tmp_path)
    orch._client = client
    _sleeps(orch)

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert orch._client is client
    assert client.closed is False
    assert client.dismissals == 1, "dismissed in place, on the page already held"


def test_no_client_is_constructed_just_to_dismiss(tmp_path):
    """`_get_client()` would BUILD one when none is held, and constructing the
    Playwright client binds to the conversation and can navigate — the extra
    request this path exists to avoid. With nothing held there is nothing to
    dismiss, and the next step's own attach raises again."""
    built = []
    orch = _browser_orch(tmp_path)
    orch._client = None
    orch._client_factory = lambda: built.append(1)
    _sleeps(orch)

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert built == []


def test_the_backoff_escalates_and_is_capped(tmp_path):
    """A limit still up after the first wait is a limit the first wait was too
    short for; re-probing on a fixed short interval is a slower version of the
    hammering this exists to stop."""
    orch = _browser_orch(
        tmp_path,
        rate_limit_backoff_seconds=10.0,
        rate_limit_backoff_max_seconds=40.0,
    )
    taken = _sleeps(orch)

    for _ in range(5):
        orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert taken == [10.0, 20.0, 40.0, 40.0, 40.0]


def test_the_recorded_wait_is_the_one_actually_taken(tmp_path):
    """Measured, not assumed from config — so the park can state a total that
    was really observed rather than one reconstructed from the schedule."""
    orch = _browser_orch(
        tmp_path,
        rate_limit_backoff_seconds=10.0,
        rate_limit_backoff_max_seconds=40.0,
    )
    taken = _sleeps(orch)

    for _ in range(3):
        orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert orch.state.rate_limit_wait_seconds == sum(taken)


def test_the_wait_is_durable_before_it_starts_and_credited_only_once_served(tmp_path):
    """A crash mid-wait must resume knowing a throttle is in progress AND how
    much of the wait is still owed. Reset to zero it would come back ready to
    hammer again; credited up front it would come back believing it had
    already waited, which is the same thing arriving by the other door."""
    orch = _browser_orch(tmp_path)
    checked = []

    def inspect_state_mid_wait(_seconds):
        reloaded = orch._store.load()
        checked.append(
            (
                reloaded.rate_limit_backoffs,
                reloaded.rate_limit_wait_seconds,
                reloaded.rate_limit_retry_not_before,
            )
        )

    orch._sleep = inspect_state_mid_wait

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    # Asserted OUTSIDE the callback: an assertion that only runs inside a
    # sleep nobody called would pass by never running at all.
    assert len(checked) == 1
    backoffs, waited, deadline = checked[0]
    assert backoffs == 1, "the streak is on disk before the sleep"
    assert waited == 0.0, "and no second of it is credited before it happens"
    assert deadline, "the instant the wait runs to is on disk too"
    assert datetime.fromisoformat(deadline) > datetime.now(timezone.utc)

    # Only now, with the sleep returned, is the wait a fact.
    assert orch.state.rate_limit_wait_seconds == 60.0
    assert orch.state.rate_limit_retry_not_before is None, "and nothing is still owed"
    assert orch._store.load().rate_limit_retry_not_before is None, "durably"


def test_a_crash_mid_wait_resumes_into_the_remaining_wait_not_the_browser(tmp_path):
    """THE durability test: the process that started the back-off dies inside
    it, and the one that takes over owes the rest of it.

    Without a persisted deadline the successor sees a counter it cannot
    distinguish from a wait already served, re-enters the step immediately, and
    a supervisor that restarts the loop skips every back-off in turn — the
    restart storm this whole path exists to stop, rebuilt one level up out of
    process restarts instead of browser ones.
    """
    first = _browser_orch(
        tmp_path,
        rate_limit_backoff_seconds=600.0,
        rate_limit_backoff_max_seconds=600.0,
    )
    first.state.phase = Phase.AWAITING.value

    def killed_mid_wait(_seconds):
        raise KeyboardInterrupt("SIGINT while waiting out the throttle")

    first._sleep = killed_mid_wait
    with pytest.raises(KeyboardInterrupt):
        first._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    handover = first._store.load()
    assert handover.rate_limit_retry_not_before, "the debt outlives the process"
    assert handover.rate_limit_wait_seconds == 0.0, "and none of it is paid yet"

    # A SECOND process over the same state directory.
    second = _browser_orch(
        tmp_path,
        state=handover,
        rate_limit_backoff_seconds=600.0,
        rate_limit_backoff_max_seconds=600.0,
    )
    order = []
    second._sleep = lambda seconds: order.append(("slept", seconds))
    second._step = lambda phase: order.append(("stepped", phase))
    # Construction alone reaches ChatGPT (it binds to the conversation and can
    # navigate), so it counts as touching the account, not merely as a step.
    second._client_factory = lambda: order.append(("client built", None))
    # Attached in place, as a resumed process finds it: the overlay outlives the
    # wait and does NOT record into `order` (it is a click on a page already
    # held, not a request), so the ordering assertion below is unaffected.
    client = _ThrottleAwareClient()
    second._client = client

    second.run(max_steps=1)

    assert [event for event, _ in order] == ["slept", "stepped"], (
        "nothing may touch ChatGPT — not a step, not even a client — until the "
        "rest of the wait has been served"
    )
    assert order[0][1] == pytest.approx(600.0, abs=5.0), "the REMAINDER, resumed"
    # The resumed process is where a stale overlay is most likely: the whole
    # wait elapsed with nobody holding the page, and the modal hides the
    # composer even after the server-side limit expires — left standing it
    # would read as a throttle that never lifts.
    assert client.dismissals == 1, "the overlay is cleared before the re-probe"
    assert second.state.rate_limit_retry_not_before is None, "and now settled"
    assert second.state.rate_limit_backoffs == 0, "the completed step ended the episode"
    # Serving an inherited wait is not a fresh occurrence of the throttle: the
    # transport raised once, across both processes.
    assert sum(1 for event, _ in _transcript(second) if event == "rate_limited") == 1


def test_a_resumed_wait_is_capped_by_the_schedule_not_by_the_stored_stamp(tmp_path):
    """A backward system-clock jump (or a hand-edited state file) would
    otherwise become an arbitrarily long sleep inside one step — and the
    heartbeat is published BETWEEN steps, so that is a gap in the record. The
    ceiling on one wait is what keeps the monitor's staleness alarm meaning
    what it says."""
    orch = _browser_orch(
        tmp_path,
        rate_limit_backoff_seconds=10.0,
        rate_limit_backoff_max_seconds=40.0,
    )
    orch.state.phase = Phase.AWAITING.value
    orch.state.rate_limit_backoffs = 1
    orch.state.rate_limit_retry_not_before = (
        datetime.now(timezone.utc) + timedelta(days=3)
    ).isoformat(timespec="milliseconds")
    taken = _sleeps(orch)
    orch._step = lambda _phase: None

    orch.run(max_steps=1)

    assert taken == [10.0], "clamped to the delay the schedule prescribes"


def test_an_unreadable_deadline_does_not_take_the_loop_down(tmp_path):
    """Fail open, deliberately: the counter is what bounds the episode, and if
    the limit is still up the next step raises again and re-enters the back-off
    with the streak intact. A hand-edited stamp must not be fatal."""
    orch = _browser_orch(tmp_path)
    orch.state.phase = Phase.AWAITING.value
    orch.state.rate_limit_retry_not_before = "whenever"
    taken = _sleeps(orch)
    stepped = []
    orch._step = stepped.append

    orch.run(max_steps=1)

    assert taken == []
    assert stepped == [Phase.AWAITING]
    assert any(event == "rate_limit_deadline_unreadable" for event, _ in _transcript(orch))
    # Discarded rather than kept: a stamp nothing can read is a wait nobody can
    # serve, and left in place it would re-log on every step of the session.
    assert orch.state.rate_limit_retry_not_before is None
    assert orch._store.load().rate_limit_retry_not_before is None


def test_a_resumed_wait_says_so_in_the_transcript(tmp_path):
    """The incident behind this task is a loop that was stuck for hours and
    said nothing. A process that sleeps ten minutes before its first step
    without a word is a smaller copy of it."""
    orch = _browser_orch(tmp_path)
    orch.state.phase = Phase.AWAITING.value
    orch.state.rate_limit_backoffs = 2
    orch.state.rate_limit_retry_not_before = (
        datetime.now(timezone.utc) + timedelta(seconds=90)
    ).isoformat(timespec="milliseconds")
    _sleeps(orch)
    orch._step = lambda _phase: None

    orch.run(max_steps=1)

    data = next(
        data for event, data in _transcript(orch) if event == "rate_limit_wait_resumed"
    )
    assert data["reason_code"] == "rate_limited"
    assert data["backoffs"] == 2
    assert data["remaining_seconds"] > 0


def test_dismissing_the_modal_is_not_evidence_the_limit_lifted(tmp_path):
    """THE mutation test for the whole escalation.

    The overlay is gone after every wait — the loop closed it. If that counted
    as cleared, the streak would reset on every occurrence: the delay would
    never double, `max_rate_limit_backoffs` would never accumulate, and the
    park would be unreachable. A fixed 60-second retry loop wearing the shape
    of a back-off, which is a slower version of the failure being fixed.
    """
    client = _ThrottleAwareClient()
    orch = _browser_orch(
        tmp_path,
        rate_limit_backoff_seconds=10.0,
        rate_limit_backoff_max_seconds=40.0,
    )
    orch._client = client
    taken = _sleeps(orch)

    for _ in range(3):
        orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert client.dismissals == 3, "dismissed every time, as it must be"
    assert orch.state.rate_limit_backoffs == 3, "and never once read as cleared"
    assert taken == [10.0, 20.0, 40.0], "so the wait actually escalates"


def test_a_completed_step_is_what_clears_the_streak(tmp_path):
    """The one honest signal that the limit lifted, and it costs no request.
    The counter describes ONE throttle episode; waits from unrelated episodes
    accumulating into a park would stop a working account."""
    orch, _, _, _, _, _, _ = build(tmp_path)
    orch.state.rate_limit_backoffs = 4
    orch.state.rate_limit_wait_seconds = 150.0
    orch.state.phase = Phase.READY.value
    # Stubbed so the property under test is the RESET SITE and not whatever a
    # particular phase happens to need — the symmetric twin of the test below.
    orch._step = lambda _phase: None

    orch.run(max_steps=1)

    assert orch.state.rate_limit_backoffs == 0
    assert orch.state.rate_limit_wait_seconds == 0.0
    assert orch._store.load().rate_limit_backoffs == 0, "and durably"


def test_a_step_that_raises_the_throttle_again_does_not_clear_it(tmp_path):
    """The counterpart: the reset must sit on the success path only, or it
    would undo the very count it was about to be charged."""
    orch, _, _, _, _, _, _ = build(tmp_path)
    _sleeps(orch)
    orch.state.phase = Phase.READY.value

    def always_throttled(_phase):
        raise RateLimitedError("throttled")

    orch._step = always_throttled

    orch.run(max_steps=2)

    assert orch.state.rate_limit_backoffs == 2


def test_ending_on_backoffs_parks_naming_the_throttle(tmp_path):
    """The whole point of the task: the operator sees "rate limited", not
    "browser session lost". Bounded, and the bound ends in a blocker they can
    read rather than a terminal phase whose cause only the transcript holds."""
    blockers = BlockerStore(tmp_path / "blockers")
    orch = _browser_orch(
        tmp_path,
        policy=PolicyConfig(max_rate_limit_backoffs=2),
        rate_limit_backoff_seconds=10.0,
    )
    orch._blocker_store = blockers
    orch.state.phase = Phase.AWAITING.value
    _sleeps(orch)

    for _ in range(3):
        orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.phase != Phase.FAILED.value
    assert orch.state.consecutive_failures == 0, "still never charged"
    assert orch.state.resume_phase == Phase.AWAITING.value

    open_blockers = blockers.open_blockers()
    assert len(open_blockers) == 1
    parked = open_blockers[0]
    assert parked.code == "rate_limited"
    assert "rate limited" in parked.question.lower()
    assert "30s" in parked.question, "the measured wait, so 'when' is answerable"
    assert "restart" in parked.question, "and that a restart is not the remedy"


def test_the_park_states_the_evidence_it_concluded_the_limit_from(tmp_path):
    """The park claims this is NOT a browser fault, which is a strong claim.
    It has to say what that rests on, or an operator cannot tell a modal that
    was really observed from a default reached because nothing could be
    asked — the four-hour failure of 2026-08-17 arriving through the park
    instead of through the wait."""
    blockers = BlockerStore(tmp_path / "blockers")
    orch = _browser_orch(
        tmp_path,
        policy=PolicyConfig(max_rate_limit_backoffs=1),
        rate_limit_backoff_seconds=10.0,
    )
    orch._blocker_store = blockers
    orch.state.phase = Phase.AWAITING.value
    orch._client = _ThrottleAwareClient()
    _sleeps(orch)

    for _ in range(2):
        orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    parked = blockers.open_blockers()[0]
    assert parked.code == "rate_limited"
    assert "throttle modal is up" in parked.question, "the observation, not an assumption"
    assert "NOT a browser fault" in parked.question, "asserted, because it was SEEN"


def test_a_park_that_never_SAW_the_modal_says_so_and_sends_the_operator_to_the_browser(
    tmp_path,
):
    """`RL_THROTTLED` is also the DEFAULT — what a page that cannot be probed
    and an endpoint that cannot be measured produce. Claiming "not a browser
    fault, the composer is present" on that evidence is the 2026-08-17 failure
    in a narrower case: four hours of waiting out a limit nobody confirmed. The
    wait and the remedy are unchanged; what the park CLAIMS is not."""
    blockers = BlockerStore(tmp_path / "blockers")
    orch = _browser_orch(
        tmp_path,
        policy=PolicyConfig(max_rate_limit_backoffs=1),
        rate_limit_backoff_seconds=10.0,
    )
    orch._blocker_store = blockers
    orch.state.phase = Phase.AWAITING.value
    orch._client = None  # nothing held; the endpoint is unmeasurable (conftest)
    _sleeps(orch)

    for _ in range(2):
        orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    parked = blockers.open_blockers()[0]
    assert parked.code == "rate_limited", "still bounded and still parked the same way"
    assert "NOT a browser fault" not in parked.question, "it never saw the modal"
    assert "never actually sighted" in parked.question
    assert "/json/list" in parked.question, "and the operator is told what to check"
    # The pins the throttle park has always carried survive both branches.
    assert "rate limited" in parked.question.lower()
    assert "restart" in parked.question
    assert "10s" in parked.question, "the measured wait — one completed 10s back-off"


# ---- a throttled account vs an unattachable browser ---------------------------
#
# Observed 2026-08-17. The operator closed the browser WINDOW: Chrome stayed
# alive, `/json/version` kept answering with a valid `webSocketDebuggerUrl` — so
# every check built on it reported a healthy browser — and `/json/list` returned
# ZERO targets. Playwright could not attach at all, so there was no page to
# dismiss a modal on and nothing to re-probe. The loop backed off its whole
# budget and parked saying "rate_limited" while the real cause was that it had
# no browser; a probe reported "still rate limited" for four hours. Restarting
# the profile restored 11 targets immediately.
#
# The no-restart rule above is NOT reversed by any of this: it assumes the
# browser is usable and merely being refused, and these tests pin the boundary
# of that assumption. State 1 must behave exactly as it did.


class _UnattachableClient:
    """A held client whose PAGE is gone: every probe raises, as Playwright does
    once the target it was bound to no longer exists. Nothing about it can
    prove or disprove a throttle — that is the point."""

    def __init__(self):
        self.closed = False

    def _dead(self):
        raise RuntimeError("Target page, context or browser has been closed")

    def is_rate_limited(self):
        self._dead()

    def composer_interactive(self):
        self._dead()

    def dismiss_rate_limit_modal(self):
        self._dead()

    def close(self):
        self.closed = True


class _ClearedClient:
    """The limit has lifted: no overlay, and a real click on the composer
    LANDS. Interaction evidence, not presence — a throttled page also has a
    composer that reports visible and enabled."""

    def __init__(self, clickable=True):
        self.clickable = clickable
        self.clicks = 0
        self.closed = False

    def is_rate_limited(self):
        return False

    def composer_interactive(self):
        self.clicks += 1
        return self.clickable

    def dismiss_rate_limit_modal(self):
        return True

    def close(self):
        self.closed = True


def test_a_throttle_modal_on_an_attachable_page_backs_off_without_restarting(
    tmp_path, monkeypatch
):
    """State 1, unchanged — and asserted against a target count of ZERO, the
    one reading that authorises a restart everywhere else.

    The modal is asked FIRST for exactly this reason: a page that can show it
    is a page the loop can drive, whatever a transient answer at the CDP
    endpoint says. Ordering it the other way would make an odd probe restart
    Chrome in the middle of a genuine account throttle — the response that
    deepens it."""
    orch = _browser_orch(
        tmp_path, restart_command=("true",), restart_cooldown_seconds=0.0
    )
    orch.state.phase = Phase.AWAITING.value
    client = _ThrottleAwareClient()
    orch._client = client
    orch._attachable_page_targets = lambda: 0
    calls = _fake_restart(monkeypatch, returncode=0)
    taken = _sleeps(orch)

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert calls == [], "a throttle still never restarts the browser"
    assert orch._client is client, "and still never drops the client"
    assert orch.state.rate_limit_backoffs == 1
    assert taken == [60.0], "it waits, exactly as before"


def test_zero_cdp_targets_restarts_once_and_spends_no_backoff_budget(tmp_path, monkeypatch):
    """State 3. The restart is a LOCAL recovery — it makes no request of
    ChatGPT — so it must not spend the budget that bounds waiting on the
    server, and it must not wait either: there is nothing to outlast."""
    orch = _browser_orch(
        tmp_path, restart_command=("true",), restart_cooldown_seconds=0.0
    )
    orch.state.phase = Phase.AWAITING.value
    client = _UnattachableClient()
    orch._client = client
    # Dead, then eleven targets once the profile has been restarted — the
    # numbers the incident actually produced.
    probes = [0, 11]
    orch._attachable_page_targets = lambda: probes.pop(0)
    calls = _fake_restart(monkeypatch, returncode=0)
    taken = _sleeps(orch)

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert len(calls) == 1, "exactly one restart per back-off cycle"
    assert orch.state.rate_limit_backoffs == 0, "no browser fault may spend that budget"
    assert orch.state.rate_limit_wait_seconds == 0.0
    assert orch.state.consecutive_failures == 0
    assert orch.state.browser_restart_skips == 0
    assert taken == [], "a dead browser is not something to wait out"
    assert client.closed is True, "the restart ends the process it was bound to"
    assert orch._client is None
    assert orch.state.phase == Phase.AWAITING.value, "the step is simply re-entered"
    assert probes == [], "the restart is re-probed, not assumed to have worked"
    events = [event for event, _ in _transcript(orch)]
    assert "browser_unattachable" in events
    assert "browser_reattached" in events
    assert "rate_limited" not in events, "it was never a rate limit"


def test_a_restart_that_still_yields_no_targets_parks_naming_the_browser(tmp_path, monkeypatch):
    """The four-hour failure, closed. An operator reading "rate limited" while
    the real problem is a dead browser waits for a limit that does not exist —
    so once one restart has not helped, the loop stops and says BROWSER."""
    blockers = BlockerStore(tmp_path / "blockers")
    orch = _browser_orch(
        tmp_path, restart_command=("true",), restart_cooldown_seconds=0.0
    )
    orch._blocker_store = blockers
    orch.state.phase = Phase.AWAITING.value
    orch._client = _UnattachableClient()
    orch._attachable_page_targets = lambda: 0  # still nothing to attach to
    calls = _fake_restart(monkeypatch, returncode=0)
    taken = _sleeps(orch)

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert len(calls) == 1, "one restart, then a verdict — never a restart loop"
    assert taken == []
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.resume_phase == Phase.AWAITING.value
    assert orch.state.rate_limit_backoffs == 0
    assert orch.state.consecutive_failures == 0

    parked = blockers.open_blockers()[0]
    assert parked.code == "browser_unattachable"
    assert parked.code != "rate_limited", "the whole point of the task"
    assert "BROWSER" in parked.question, "named as the cause, not the throttle"
    assert "no attachable page" in parked.question
    assert "chrome_restart" in parked.question, "and the operator is told what to run"
    # `autoloop start` prints `blocker.question[:160]`. Ordered
    # evidence-first, that view spends the whole summary on measurements and
    # cuts off the sentence saying what to do — the same rule
    # `describe_cdp_endpoint` follows.
    summary = parked.question[:160]
    assert "NOT A RATE LIMIT" in summary, "the misdiagnosis is corrected up front"
    assert "chrome_restart" in summary, "and the action survives the cut"


def test_the_episode_gets_ONE_restart_even_with_the_cooldown_disabled(tmp_path, monkeypatch):
    """The bound is per back-off cycle, not only per cooldown window.

    With `restart_cooldown_seconds = 0` the cooldown refuses nothing, and each
    restart here reports success while the re-probe still finds no page — so
    without a per-episode bound this is a restart loop thrashing Chrome, the
    exact failure the cooldown exists to prevent, arriving through the one
    path that had disabled it."""
    blockers = BlockerStore(tmp_path / "blockers")
    orch = _browser_orch(
        tmp_path, restart_command=("true",), restart_cooldown_seconds=0.0
    )
    orch._blocker_store = blockers
    orch.state.phase = Phase.AWAITING.value
    orch._client = _UnattachableClient()
    orch._attachable_page_targets = lambda: 0
    calls = _fake_restart(monkeypatch, returncode=0)

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))
    assert len(calls) == 1

    # The first call parked; this models the loop being sent back into the
    # same dead browser (a `run --retry` inside this process). It must not
    # spend a second restart on a browser that just failed to come back.
    orch.state.phase = Phase.AWAITING.value
    orch._client = _UnattachableClient()
    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert len(calls) == 1, "one restart per episode, cooldown or no cooldown"
    assert all(
        blocker.code == "browser_unattachable" for blocker in blockers.all_blockers()
    )
    spent = [
        data for event, data in _transcript(orch) if event == "browser_unattachable"
    ]
    assert [entry["restart_already_spent"] for entry in spent] == [False, True]


def test_a_completed_step_ends_the_episode_so_a_LATER_dead_browser_gets_its_own_restart(
    tmp_path, monkeypatch
):
    """The other half of the per-episode bound, and the one that was missing.

    State 3 deliberately does not increment `rate_limit_backoffs`, so the
    ordinary sequence — zero targets, restart, a normal step that completes —
    ends with that counter still 0. While the one-restart guard was cleared
    only inside `if rate_limit_backoffs:` it therefore stayed true for the rest
    of the process, and the NEXT unattachable browser, hours later and
    unrelated, was refused its own restart and parked
    `skipped_already_spent` — a working recovery spent once per process
    instead of once per fault.

    A step that COMPLETED is proof the browser works, which is exactly what
    ends an episode."""
    orch = _browser_orch(
        tmp_path, restart_command=("true",), restart_cooldown_seconds=0.0
    )
    orch.state.phase = Phase.AWAITING.value
    orch._client = _UnattachableClient()
    probes = [0, 11]  # dead, then alive once the profile has been restarted
    orch._attachable_page_targets = lambda: probes.pop(0)
    calls = _fake_restart(monkeypatch, returncode=0)
    _sleeps(orch)

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))
    assert len(calls) == 1
    assert probes == [], "the restart genuinely restored a page"

    # An ordinary step now completes. The phase was left untouched by the
    # recovery, so this is the step the loop was already in — and the counter
    # is ZERO, which is the whole point: the old reset site never ran here.
    assert orch.state.rate_limit_backoffs == 0
    orch._step = lambda _phase: None
    orch.run(max_steps=1)

    # A second, independent fault much later. Same browser config, fresh dead
    # client, nothing to attach to again.
    orch.state.phase = Phase.AWAITING.value
    orch._client = _UnattachableClient()
    orch._attachable_page_targets = lambda: 0
    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert len(calls) == 2, "a new episode gets its own restart, not a spent one"
    assert orch.state.rate_limit_backoffs == 0, (
        "and it is still a local recovery — brw-03's rule holds for the second "
        "episode as much as the first"
    )
    spent = [
        data for event, data in _transcript(orch) if event == "browser_unattachable"
    ]
    assert [entry["restart_already_spent"] for entry in spent] == [False, False]


def test_a_cleared_modal_resumes_instead_of_waiting(tmp_path, monkeypatch):
    """State 2. The overlay is gone AND a real click lands, so the limit has
    lifted and a wait would be a delay against nothing.

    The counter is still spent: it is the only thing bounding a page that
    keeps clearing between the raise and this check, and skipping both the
    sleep and the count would answer every occurrence with an immediate
    retry — the hammering the back-off exists to stop."""
    orch = _browser_orch(
        tmp_path, restart_command=("true",), restart_cooldown_seconds=0.0
    )
    orch.state.phase = Phase.AWAITING.value
    client = _ClearedClient()
    orch._client = client
    calls = _fake_restart(monkeypatch, returncode=0)
    taken = _sleeps(orch)

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert taken == [], "nothing left to outlast"
    assert calls == [], "a working page is never restarted"
    assert client.clicks == 1, "proven by interaction, not by the composer existing"
    assert orch._client is client
    assert orch.state.phase == Phase.AWAITING.value, "the step is re-entered"
    assert orch.state.rate_limit_retry_not_before is None
    assert orch.state.rate_limit_backoffs == 1, "still counted, so repeats escalate"
    assert any(event == "rate_limit_cleared" for event, _ in _transcript(orch))


def test_a_composer_that_will_not_take_a_click_is_not_a_clear(tmp_path):
    """The 2026-08-15 trap, in the classifier. A throttled page renders a
    composer that reports visible AND enabled while an overlay intercepts
    every click; three passive checks called that healthy. Absence of the
    modal alone must therefore never resume."""
    orch = _browser_orch(tmp_path)
    orch.state.phase = Phase.AWAITING.value
    orch._client = _ClearedClient(clickable=False)
    taken = _sleeps(orch)

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert taken == [60.0], "it waits"
    assert orch.state.rate_limit_backoffs == 1


def test_an_endpoint_that_cannot_be_measured_never_restarts(tmp_path, monkeypatch):
    """Unmeasurable is not evidence. A browser that genuinely cannot be
    reached raises `BrowserError` from the next step and gets the restart path
    built for it, on its own budget — so a probe that answers nothing must
    leave this handler behaving exactly as it did before the probe existed."""
    orch = _browser_orch(
        tmp_path, restart_command=("true",), restart_cooldown_seconds=0.0
    )
    orch.state.phase = Phase.AWAITING.value
    orch._client = None  # nothing held, so the page cannot be asked either
    orch._attachable_page_targets = lambda: None
    calls = _fake_restart(monkeypatch, returncode=0)
    taken = _sleeps(orch)

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    assert calls == []
    assert orch.state.rate_limit_backoffs == 1
    assert taken == [60.0]


def test_the_transcript_says_rate_limited_not_browser_error(tmp_path):
    """The transcript is where the overnight run left no trace at all — the
    words rate, limit and throttle appeared nowhere in it."""
    orch = _browser_orch(tmp_path)
    _sleeps(orch)

    orch._handle_rate_limited(Phase.SUBMITTING, RateLimitedError("throttled", stage="submit-input"))

    events = _transcript(orch)
    assert any(event == "rate_limited" for event, _ in events)
    assert not any(event == "browser_error" for event, _ in events)
    data = next(data for event, data in events if event == "rate_limited")
    assert data["stage"] == "submit-input"
