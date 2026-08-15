"""Retiring a conversation that has become SLOW, not one that is broken.

Rotation (§5c, `test_transport_recovery.py`) answers a chat that cannot be used:
a second disproven send, a `ConversationUnusableError`, a confirmed silence. It
deliberately never fires for a slow answer, and that caution is right — a
rotation abandons the reviewer's context and the emergency budget is one per run.

This file covers the case that is neither: a conversation that still works and
has simply grown too large to compose in. Observed 2026-08-15 at 90+ request
packets, where `composer_timeout_seconds` had already been conceded 30 -> 180
and one element-read timeout cascaded into two Chrome restarts in four minutes
with no request ever sent.

The invariants under test, all of which follow from "degradation is not
breakage":

* the trigger is a COUNT of messages, so it cannot fire on one slow round;
* a refusal — no budget, no project, throttled, mid-delivery — LOGS and lets the
  round proceed in the existing thread. It never parks a working loop;
* it spends its own budget, never the rotation allowance;
* the move is recorded with the measurement that justified it, and the
  replacement thread is told the same thing in its first message.
"""

import pytest

from autoloop.browser.chatgpt import SubmitResult
from autoloop.errors import BrowserError
from autoloop.orchestrator import CONTINUATION_NOTE
from autoloop.policy import PolicyConfig, PolicyEngine
from autoloop.state import LoopState, Phase, StateStore

from test_transport_recovery import (  # noqa: E402 - see conftest sys.path
    CONV_URL,
    NEW_CONV_URL,
    PROJECT_URL,
    RotatingFakeClient,
    build,
    pending,
    stop_block,
    transcript_entries,
)


def degraded_state(packets=80, **overrides):
    """A loop sitting in `submitting` with a thread that is over the size
    threshold and a request nothing has been sent for yet."""
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    state.conversation_packets = packets
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


# ---- 1. the signal itself ---------------------------------------------------


def test_the_threshold_is_a_count_not_a_latency_trend():
    """Why a count: the degraded thread that motivated this never completed a
    round — two Chrome restarts in four minutes, no request sent — so a
    submit-to-response latency series would have measured nothing during the
    exact episode it was meant to catch."""
    engine = PolicyEngine(PolicyConfig(max_conversation_packets=80))
    assert not engine.conversation_is_degraded(79)
    assert engine.conversation_is_degraded(80)
    assert engine.conversation_is_degraded(200)


def test_zero_disables_retirement_entirely():
    """0 means "no ceiling", the convention `max_review_rounds` already sets."""
    engine = PolicyEngine(PolicyConfig(max_conversation_packets=0))
    assert not engine.conversation_is_degraded(10_000)


def test_the_retirement_budget_is_separate_from_the_rotation_budget():
    engine = PolicyEngine(
        PolicyConfig(max_conversation_rotations=1, max_conversation_retirements=2)
    )
    assert engine.check_retirement_budget(0).allowed
    assert engine.check_retirement_budget(1).allowed
    assert not engine.check_retirement_budget(2).allowed
    # Spending the emergency allowance leaves the planned one untouched, which
    # is the entire reason the two exist separately.
    assert not engine.check_rotation_budget(1).allowed
    assert engine.check_retirement_budget(1).allowed


def test_a_single_slow_round_cannot_reach_the_threshold(tmp_path):
    """The constraint §5c states outright: one slow answer never rotates. Here
    it holds structurally — a fresh conversation has counted one message, and
    one is not eighty."""
    client = RotatingFakeClient(responses=[stop_block()])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, _store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.conversation_packets == 1
    assert orch.state.conversation_url == CONV_URL
    assert transcript_entries(config, "conversation_retired") == []


# ---- 2. counting what is actually in the thread -----------------------------


def test_every_route_into_awaiting_counts_one_message(tmp_path):
    """A request also lands in the thread without this process sending it —
    `submitting`'s pre-send reconcile finds it already there. Counting only the
    send path would let the threshold arrive late."""
    client = RotatingFakeClient(responses=[stop_block()])
    client.seed(CONV_URL, "alr-test-0001")
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, _store, _config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert client.submitted == []  # nothing was sent; it was already there
    assert orch.state.conversation_packets == 1


def test_re_entering_submitting_does_not_count_the_same_message_twice(tmp_path):
    """A `--retry` after a park re-runs `submitting` for a request that is
    already in the conversation. The thread did not grow, so neither may the
    count."""
    client = RotatingFakeClient(responses=[stop_block()])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    # Already confirmed present: exactly what `submitted` means.
    state.pending_request = pending(submitted=True)
    state.conversation_packets = 7
    client.seed(CONV_URL, "alr-test-0001")
    orch, _store, _config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.conversation_packets == 7


# ---- 3. the retirement itself -----------------------------------------------


def test_an_oversized_conversation_is_retired_before_it_is_even_loaded(tmp_path):
    """The move happens instead of the send, and — the point of doing it here —
    without ever attaching to the thread being escaped. Loading a conversation
    that size is the expensive thing; a check that paid for it first could time
    out on the very condition it exists to answer."""
    client = RotatingFakeClient(responses=[stop_block()])
    orch, _store, _config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.retirements == 1
    assert orch.state.rotations == 0  # the emergency budget is untouched
    assert orch.state.conversation_url == NEW_CONV_URL
    assert orch.state.pending_request.conversation_url == NEW_CONV_URL
    assert orch.state.phase == Phase.AWAITING.value
    # Straight to the project page: the retired thread was never opened.
    assert client.retargets[0] == PROJECT_URL
    assert client.retargets[-1] == NEW_CONV_URL
    assert [url for url, _rid, _prompt in client.submitted] == [PROJECT_URL]


def test_the_retirement_is_logged_with_the_measurement_that_justified_it(tmp_path):
    """A rotation must never be a mystery in the transcript — least of all one
    the loop chose to perform on a working conversation."""
    client = RotatingFakeClient(responses=[stop_block()])
    state = degraded_state(packets=93)
    state.conversation_round_seconds = [61.0, 88.5, 140.2]
    orch, _store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    retired = transcript_entries(config, "conversation_retired")
    assert len(retired) == 1
    data = retired[0]["data"]
    assert data["reason"] == "conversation_degraded"
    assert data["conversation_packets"] == 93
    assert data["max_conversation_packets"] == 80
    assert data["round_seconds"] == [61.0, 88.5, 140.2]
    assert data["old_url"] == CONV_URL
    assert data["new_url"] == NEW_CONV_URL
    assert data["retirements"] == 1
    # And it is a rotation for every reader that already knew about rotations:
    # the drift guard and `doctor` read this record, so retirement needs no
    # second one.
    assert orch.state.last_rotation["reason"] == "conversation_degraded"


def test_the_replacement_thread_is_primed_and_told_why_it_exists(tmp_path):
    """`carry over what a fresh chat needs`: the priming convention an operator
    types by hand, plus the measurement — folded into the first message, because
    a retirement's first turn IS the request and a separate priming turn would
    cost a round."""
    client = RotatingFakeClient(responses=[stop_block()])
    state = degraded_state(packets=91)
    state.conversation_round_seconds = [54.0]
    orch, _store, _config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    _url, _rid, prompt = client.submitted[0]
    assert "conversation is reserved for autoloop" in prompt
    assert "91 messages" in prompt
    assert "54s" in prompt
    assert "only one Autoloop reads" in prompt
    # The retired thread is disowned, exactly as a rotation disowns one.
    assert "authoritative" in prompt
    # The ordinary recovery note belongs to the OTHER trigger.
    assert CONTINUATION_NOTE not in prompt


def test_the_replacement_thread_starts_its_own_size_and_latency_clocks(tmp_path):
    """One, not zero: the move's own message is the new thread's first packet.
    A count that disagreed with the thread by one would make the NEXT
    retirement's recorded justification off by one too."""
    client = RotatingFakeClient(responses=[stop_block()])
    state = degraded_state(packets=120)
    state.conversation_round_seconds = [200.0, 240.0]
    orch, _store, _config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.conversation_packets == 1
    assert orch.state.conversation_round_seconds == []


def test_a_retirement_survives_a_restart(tmp_path):
    client = RotatingFakeClient(responses=[stop_block()])
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    reloaded = StateStore(config.state_file).load()
    assert reloaded.conversation_url == NEW_CONV_URL
    assert reloaded.retirements == 1
    assert reloaded.conversation_packets == 1
    assert reloaded.last_rotation["reason"] == "conversation_degraded"


# ---- 4. a refusal never parks a working loop --------------------------------


def test_no_project_url_keeps_working_in_the_slow_thread(tmp_path):
    """The difference from `_attempt_rotation`, which parks on this precondition:
    there, the conversation cannot be used at all. Here it can — it is only
    slow — so parking would be worse than the slowness."""
    client = RotatingFakeClient(responses=[stop_block()])
    orch, _store, config = build(
        tmp_path, client, state=degraded_state(), project_url=""
    )

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.AWAITING.value  # the round went ahead
    assert orch.state.conversation_url == CONV_URL
    assert orch.state.retirements == 0
    assert [url for url, _rid, _p in client.submitted] == [CONV_URL]
    declined = transcript_entries(config, "retirement_declined")
    assert declined and declined[-1]["data"]["reason_code"] == "no_project_url"
    assert transcript_entries(config, "rotation_declined") == []  # nothing parked


def test_a_spent_retirement_budget_keeps_working_in_the_slow_thread(tmp_path):
    client = RotatingFakeClient(responses=[stop_block()])
    policy = PolicyConfig(max_conversation_retirements=1)
    orch, _store, config = build(
        tmp_path, client, state=degraded_state(retirements=1), policy=policy
    )

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.AWAITING.value
    assert orch.state.conversation_url == CONV_URL
    assert orch.state.retirements == 1  # not incremented
    declined = transcript_entries(config, "retirement_declined")
    assert declined and declined[-1]["data"]["reason_code"] == "retirement_budget"


def test_a_provider_that_cannot_rotate_never_spends_the_budget(tmp_path):
    """Probed like every other optional transport capability. Learning it by
    spending the budget on a doomed attempt would be worse."""

    class NoRotation(RotatingFakeClient):
        retarget = None
        current_url = None

    client = NoRotation(responses=[stop_block()])
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.retirements == 0
    assert orch.state.conversation_url == CONV_URL
    declined = transcript_entries(config, "retirement_declined")
    assert declined and declined[-1]["data"]["reason_code"] == "provider_cannot_rotate"


def test_a_failed_retirement_spends_its_budget_and_carries_on(tmp_path):
    """A retirement SENDS. If it fails after that, autoloop must not be able to
    open a second chat and post again — so the attempt is charged. What it must
    NOT do is park: the old thread is still there and still works."""

    class RetireSendExplodes(RotatingFakeClient):
        def submit(self, request_id, prompt):
            if self.conversation_url == PROJECT_URL:
                raise BrowserError("died right after posting")
            return super().submit(request_id, prompt)

    client = RetireSendExplodes(responses=[stop_block()])
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.retirements == 1  # spent, not refunded
    assert orch.state.conversation_url == CONV_URL  # nothing was bound
    assert orch.state.phase != Phase.NEEDS_USER.value
    failed = transcript_entries(config, "retirement_failed")
    assert failed and failed[-1]["data"]["reason_code"] == "retirement_failed"
    # The request keeps its original prompt: a `--resubmit` into the old chat
    # must not send it a note saying that chat is abandoned.
    assert "reserved for autoloop" not in orch.state.pending_request.prompt


def test_a_failed_retirement_drops_the_client_it_left_on_the_project_page(tmp_path):
    """The move retargets the cached client at the project page before it
    submits. Without dropping it, the next `attach()` would load THAT page and
    post the request where nobody is reading."""

    class RetireSendExplodes(RotatingFakeClient):
        def submit(self, request_id, prompt):
            if self.conversation_url == PROJECT_URL:
                raise BrowserError("died right after posting")
            return super().submit(request_id, prompt)

    client = RetireSendExplodes(responses=[stop_block()])
    orch, _store, _config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert client.closed


# ---- 5. deference: throttling, deliveries, in-flight turns ------------------


def test_a_throttled_loop_defers_rather_than_opening_another_chat(tmp_path):
    """A new chat is the same account, so retiring under a rate limit adds
    requests to the condition it is reacting to. brw-09 owns the real signal;
    this pins the seam it lands in."""

    class Throttled(RotatingFakeClient):
        def is_throttled(self):
            return True

    client = Throttled(responses=[stop_block()])
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.retirements == 0
    assert orch.state.conversation_url == CONV_URL
    deferred = transcript_entries(config, "retirement_deferred")
    assert deferred and deferred[-1]["data"]["reason_code"] == "throttled"


def test_a_throttle_recorded_on_state_defers_too(tmp_path):
    """The other half of the seam: a `throttled_until` instant on the state,
    read with `getattr` so brw-09 can add the field without editing the
    orchestrator."""
    client = RotatingFakeClient(responses=[stop_block()])
    state = degraded_state()
    state.throttled_until = "2999-01-01T00:00:00+00:00"
    orch, _store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.retirements == 0
    deferred = transcript_entries(config, "retirement_deferred")
    assert deferred and deferred[-1]["data"]["reason_code"] == "throttled"


def test_an_expired_or_unparseable_throttle_does_not_block_forever(tmp_path):
    """Fails OPEN. Failing closed would make retirement unreachable on any
    malformed value — and being wrong here costs one extra chat, not a request."""
    client = RotatingFakeClient(responses=[stop_block()])
    state = degraded_state()
    state.throttled_until = "not-a-timestamp"
    orch, _store, _config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.retirements == 1


def test_a_request_already_sent_is_never_retired_out_from_under_itself(tmp_path):
    """Something may already be in the old thread under this request id.
    Deferring costs one round; getting it wrong risks a double post."""
    client = RotatingFakeClient(
        submit_results=[SubmitResult.UNCONFIRMED], responses=[stop_block()]
    )
    state = degraded_state()
    state.pending_request = pending(send_attempted=True)
    orch, _store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.retirements == 0
    deferred = transcript_entries(config, "retirement_deferred")
    assert deferred and deferred[-1]["data"]["reason_code"] == "send_already_attempted"


def test_a_chunked_delivery_is_not_thrown_away_for_a_planned_move(tmp_path):
    """The parts live in the thread being retired and the verdict message names
    them. A rotation has no choice but to fall back to the omission notice; a
    retirement was never urgent, so it waits a round instead."""
    from autoloop.state import ChunkedDelivery

    client = RotatingFakeClient(responses=[stop_block()])
    state = degraded_state()
    state.pending_request = pending(
        delivery=ChunkedDelivery(
            parts=[{"part_id": "alr-test-0001-part-1", "index": 1, "total": 1, "text": "x"}],
            delivered=1,
            fallback_payload="body (diff omitted)",
        )
    )
    orch, _store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.retirements == 0
    assert orch.state.conversation_url == CONV_URL
    deferred = transcript_entries(config, "retirement_deferred")
    assert deferred and deferred[-1]["data"]["reason_code"] == "delivery_in_flight"


def test_an_attached_diff_is_not_left_behind(tmp_path):
    """The move submits the prompt WITHOUT re-uploading the attachment, so
    retiring here would ask for a verdict on a file the new chat does not have."""

    class UploadingClient(RotatingFakeClient):
        """Accepts the `attachment=` keyword the ordinary send path passes once
        a request carries one — without it the deferral could not be observed,
        because the round would die on a TypeError instead of proceeding."""

        def submit(self, request_id, prompt, attachment=None):
            return super().submit(request_id, prompt)

    client = UploadingClient(responses=[stop_block()])
    state = degraded_state()
    state.pending_request = pending(attachment="/tmp/autoloop-review-diffs/x.md")
    orch, _store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.retirements == 0
    deferred = transcript_entries(config, "retirement_deferred")
    assert deferred and deferred[-1]["data"]["reason_code"] == "attachment_in_flight"


# ---- 6. the budgets are run-scoped, the size is not -------------------------


def test_a_new_run_refills_the_retirement_budget_but_not_the_packet_count(tmp_path):
    """`conversation_packets` describes the CONVERSATION, which outlives the
    process. Zeroing it per run would make a long-lived thread permanently
    unretirable — every restart would forget its size."""
    from autoloop.cli import _reset_run_scoped_budgets
    from autoloop.config import AutoloopConfig, BrowserConfig

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=CONV_URL, project_url=PROJECT_URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(CONV_URL)
    state.retirements = 2
    state.conversation_packets = 61
    store.save(state)

    _reset_run_scoped_budgets(config)

    reloaded = store.load()
    assert reloaded.retirements == 0
    assert reloaded.conversation_packets == 61


def test_the_reset_is_not_skipped_when_only_retirements_were_spent(tmp_path):
    """The guard used to read `if not state.rotations: return`. A run that spent
    only the retirement budget would have kept it spent forever — the per-session
    trap that function exists to close, one field over."""
    from autoloop.cli import _reset_run_scoped_budgets
    from autoloop.config import AutoloopConfig, BrowserConfig

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=CONV_URL),
        policy=PolicyConfig(),
        state_dir=tmp_path / ".al",
    )
    store = StateStore(config.state_file)
    state = LoopState.new(CONV_URL)
    state.rotations = 0
    state.retirements = 2
    store.save(state)

    _reset_run_scoped_budgets(config)

    assert store.load().retirements == 0


def test_the_drift_guard_recognises_a_retirement_whose_config_heal_failed(tmp_path):
    """A retirement writes `last_rotation` but charges `retirements`. Without
    this, a heal that failed would strand the next run for having recovered
    correctly."""
    from autoloop.cli import _drift_is_recorded_rotation
    from autoloop.config import AutoloopConfig, BrowserConfig

    config = AutoloopConfig(
        browser=BrowserConfig(conversation_url=CONV_URL),
        policy=PolicyConfig(),
        state_dir=tmp_path,
    )
    state = LoopState.new(NEW_CONV_URL)
    state.rotations = 0
    state.retirements = 1
    state.last_rotation = {"old_url": CONV_URL, "new_url": NEW_CONV_URL}
    assert _drift_is_recorded_rotation(state, config)

    # Still narrow: a record matching neither side is still an edited config.
    state.last_rotation = {"old_url": "https://chatgpt.com/c/elsewhere", "new_url": NEW_CONV_URL}
    assert not _drift_is_recorded_rotation(state, config)


# ---- 7. the latency series is evidence, never a signal ----------------------


def test_round_latencies_are_recorded_but_decide_nothing(tmp_path):
    client = RotatingFakeClient(responses=[stop_block()])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    orch, _store, _config = build(tmp_path, client, state=state)

    orch.run(max_steps=2)  # submit, then read the reply

    assert len(orch.state.conversation_round_seconds) == 1
    assert orch.state.conversation_round_seconds[0] >= 0
    # A slow round on its own moves nothing: no retirement, no rotation.
    assert orch.state.retirements == 0
    assert orch.state.conversation_url == CONV_URL


def test_the_latency_series_is_bounded(tmp_path):
    from autoloop.state import MAX_ROUND_LATENCY_SAMPLES

    client = RotatingFakeClient(responses=[stop_block()])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.SUBMITTING.value
    state.pending_request = pending()
    state.conversation_round_seconds = [1.0] * (MAX_ROUND_LATENCY_SAMPLES + 4)
    orch, _store, _config = build(tmp_path, client, state=state)

    orch.run(max_steps=2)

    assert len(orch.state.conversation_round_seconds) == MAX_ROUND_LATENCY_SAMPLES


def test_a_request_with_no_send_stamp_records_no_latency(tmp_path):
    """A request in flight when this field was added has nothing to measure
    from. Recording nothing is the honest outcome; guessing one would put a
    fabricated number in the record a retirement is justified by."""
    client = RotatingFakeClient(responses=[stop_block()])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True, submitted_at="")
    orch, _store, _config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.conversation_round_seconds == []


def test_url_containment_still_governs_a_retirement(tmp_path):
    """Retirement reuses `_rotate_conversation`, so a chat that opens outside
    the configured project is refused here exactly as it is for a rotation —
    and, being a retirement, the refusal carries on rather than parking."""

    class OpensOutsideTheProject(RotatingFakeClient):
        def __init__(self, **kwargs):
            super().__init__(new_url="https://chatgpt.com/c/loose-chat", **kwargs)

    client = OpensOutsideTheProject(responses=[stop_block()])
    client.find_finds_nothing = True
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.conversation_url == CONV_URL
    assert orch.state.phase != Phase.NEEDS_USER.value
    assert transcript_entries(config, "retirement_failed")


@pytest.mark.parametrize("packets", [0, 1, 79])
def test_a_healthy_thread_is_never_touched(tmp_path, packets):
    client = RotatingFakeClient(responses=[stop_block()])
    orch, _store, config = build(tmp_path, client, state=degraded_state(packets=packets))

    orch.run(max_steps=1)

    assert orch.state.conversation_url == CONV_URL
    assert orch.state.retirements == 0
    # Silent by design: a transcript line per round saying nothing happened
    # would be noise, not evidence.
    assert transcript_entries(config, "retirement_declined") == []
    assert transcript_entries(config, "retirement_deferred") == []
