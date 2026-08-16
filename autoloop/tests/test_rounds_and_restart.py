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


def _browser_orch(tmp_path, policy=None, **browser_kw):
    """A real Orchestrator (state store, policy engine, transcript) whose
    browser config carries the restart settings under test."""
    orch, _, _, _, _, _, _ = build(tmp_path, policy=policy)
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


def test_the_count_is_durable_before_the_wait_starts(tmp_path):
    """A crash mid-wait must resume knowing a throttle is in progress. Reset to
    zero, it would come back ready to hammer again."""
    orch = _browser_orch(tmp_path)
    checked = []

    def inspect_state_mid_wait(_seconds):
        reloaded = orch._store.load()
        checked.append((reloaded.rate_limit_backoffs, reloaded.rate_limit_wait_seconds))

    orch._sleep = inspect_state_mid_wait

    orch._handle_rate_limited(Phase.AWAITING, RateLimitedError("throttled"))

    # Asserted OUTSIDE the callback: an assertion that only runs inside a
    # sleep nobody called would pass by never running at all.
    assert checked == [(1, 60.0)]


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
