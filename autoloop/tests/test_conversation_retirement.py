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
* a REFUSAL — no budget, no project, throttled, mid-delivery — LOGS and lets the
  round proceed in the existing thread. It never parks a working loop;
* a move that already POSTED the request and then failed is not a refusal: it
  adopts the chat that holds the message, or parks. Carrying on there would put
  the same request id in two conversations, and no other transport path in this
  loop does that (§4b below);
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


class RetireSendNeverHappens(RotatingFakeClient):
    """The move dies at the composer, BEFORE the send: `send_attempted` is
    never set, so nothing exists under this request id in any chat.

    The distinction is the whole point of `SendCertainty` — this is the only
    shape of failure that may go on to submit in the old conversation, and it
    is only provable because the transport reports it. (An earlier version of
    this double claimed it died "right after posting" while raising before it
    posted anything, which is exactly the case it needed to be modelling and
    was not.)"""

    def submit(self, request_id, prompt):
        if self.conversation_url == PROJECT_URL:
            raise BrowserError("the composer never accepted the input")
        return super().submit(request_id, prompt)


def test_a_retirement_that_provably_sent_nothing_carries_on(tmp_path):
    """A retirement SENDS. If it fails BEFORE that, the old thread is still
    there, still works, and still holds nothing under this request id — so the
    round proceeds in it. The attempt is still charged: a failure is an
    attempt, and refunding it would make this a retry loop."""
    client = RetireSendNeverHappens(responses=[stop_block()])
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.retirements == 1  # spent, not refunded
    assert orch.state.conversation_url == CONV_URL  # nothing was bound
    assert orch.state.phase != Phase.NEEDS_USER.value
    failed = transcript_entries(config, "retirement_failed")
    assert failed and failed[-1]["data"]["reason_code"] == "retirement_failed"
    assert failed[-1]["data"]["send_certainty"] == "unsent"
    # The request keeps its original prompt: a `--resubmit` into the old chat
    # must not send it a note saying that chat is abandoned.
    assert "reserved for autoloop" not in orch.state.pending_request.prompt
    # And the round really did go ahead in the old thread.
    assert [url for url, _rid, _p in client.submitted] == [CONV_URL]


def test_a_pre_send_failure_after_an_earlier_successful_send_still_carries_on(tmp_path):
    """`send_attempted` on the transport is STICKY — it stays True for the rest
    of the process once any send is clicked. Reading it after a failure that
    never reached the submit would report a send from an earlier round, park a
    loop whose thread is merely slow, and destroy the carry-on this whole
    reflex exists for. So certainty comes from the move's own position, and the
    flag is consulted only around the one call that can set it."""

    class RetargetDies(RotatingFakeClient):
        def retarget(self, url):
            if url == PROJECT_URL:
                raise BrowserError("the project page never loaded")
            return super().retarget(url)

    client = RetargetDies(responses=[stop_block()])
    # Exactly what an earlier successful round in the same process leaves behind.
    client.send_attempted = True
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.phase != Phase.NEEDS_USER.value
    failed = transcript_entries(config, "retirement_failed")
    assert failed and failed[-1]["data"]["send_certainty"] == "unsent"
    # The round went ahead in the old thread, which is only correct because the
    # move never reached its submit.
    assert [url for url, _rid, _p in client.submitted] == [CONV_URL]


def test_a_failed_retirement_drops_the_client_it_left_on_the_project_page(tmp_path):
    """The move retargets the cached client at the project page before it
    submits. Without dropping it, the next `attach()` would load THAT page and
    post the request where nobody is reading."""
    client = RetireSendNeverHappens(responses=[stop_block()])
    orch, _store, _config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert client.closed


# ---- 4b. a move that ALREADY POSTED may not carry on ------------------------
#
# The move is not one act: it opens a chat, posts the request, reads the URL the
# server assigned, checks that URL is in the project, and reconciles the chat.
# Everything after the post can fail with the message already sitting in a
# replacement chat — and carrying on there would submit the same request id a
# second time, in a second conversation. Nothing else in the transport does
# that: `submission_unconfirmed` never resends, `submission_rejected` reconciles
# first. So "a refusal never parks" governs the PRECONDITIONS above; a failure
# after the send is not a refusal.


class PostsThenDiesLater(RotatingFakeClient):
    """The exact failure the carry-on rule cannot survive: the replacement chat
    accepts and persists the request, and the move then dies on the reconcile
    that was supposed to confirm it."""

    def __init__(self, reconcile_failures=1, **kwargs):
        super().__init__(**kwargs)
        self.reconcile_failures = reconcile_failures

    def reconcile(self, request_id):
        if self.conversation_url == NEW_CONV_URL and self.reconcile_failures:
            self.reconcile_failures -= 1
            raise BrowserError("the page went away while confirming the send")
        return super().reconcile(request_id)


def test_a_persisted_send_is_never_submitted_again_into_the_old_chat(tmp_path):
    """The regression this section exists for. The request IS in the
    replacement chat when the move fails; the old conversation must never
    receive it too."""
    client = PostsThenDiesLater(responses=[stop_block()])
    orch, _store, _config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    # Posted exactly once, in the replacement chat, and never in the old one.
    assert [(url, rid) for url, rid, _p in client.submitted] == [
        (PROJECT_URL, "alr-test-0001")
    ]
    assert "alr-test-0001" in client.persisted[NEW_CONV_URL]
    assert client.persisted.get(CONV_URL, set()) == set()


def test_the_send_is_marked_on_disk_before_the_transport_is_handed_the_prompt(tmp_path):
    """A crash is the same failure without the exception, so the guarantee has
    to be on DISK, not in a live object. Killed in this window, recovery resumes
    at `submitting`, refuses the retirement on `send_already_attempted`,
    reconciles and parks — instead of cheerfully posting a duplicate."""
    seen = {}

    class ReadsTheStateFileMidSend(RotatingFakeClient):
        def submit(self, request_id, prompt):
            seen["request"] = StateStore(config.state_file).load().pending_request
            return super().submit(request_id, prompt)

    client = ReadsTheStateFileMidSend(responses=[stop_block()])
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert seen["request"].send_attempted is True
    assert seen["request"].last_send_outcome == "unknown"
    # And it is still the OLD conversation's request at that instant: nothing is
    # bound until the replacement chat is proven to hold the message.
    assert seen["request"].conversation_url == CONV_URL


def test_a_move_that_posted_and_then_failed_adopts_the_chat_holding_the_request(
    tmp_path,
):
    """Best outcome, and the common one for a transient failure on a step after
    the send: the move actually worked and only its bookkeeping died, so the
    chat is re-reconciled and adopted rather than abandoned with the request
    stranded in it."""
    client = PostsThenDiesLater(responses=[stop_block()])
    orch, _store, config = build(tmp_path, client, state=degraded_state(packets=93))

    orch.run(max_steps=1)

    assert orch.state.conversation_url == NEW_CONV_URL
    assert orch.state.pending_request.conversation_url == NEW_CONV_URL
    assert orch.state.phase == Phase.AWAITING.value
    assert orch.state.retirements == 1
    assert orch.state.conversation_packets == 1  # the new thread's own clock
    adopted = transcript_entries(config, "retirement_send_adopted")
    assert adopted and adopted[-1]["data"]["send_certainty"] == "persisted"
    assert adopted[-1]["data"]["url"] == NEW_CONV_URL
    # Bound like any other completed move, and recorded with the measurement
    # that justified it — an adoption is the same event arriving another way.
    retired = transcript_entries(config, "conversation_retired")
    assert retired and retired[-1]["data"]["conversation_packets"] == 93
    assert retired[-1]["data"]["adopted_after_failure"]
    # The committed prompt is the one that was actually posted, not the original.
    assert "reserved for autoloop" in orch.state.pending_request.prompt


def test_an_adoption_binds_only_to_a_chat_that_still_holds_the_request(tmp_path):
    """Adoption is a second door to the SAME proof, not a shortcut past it: a
    chat that does not contain the request is not bound to, however plausible
    its URL."""

    class PostsNowhereButLooksFine(RotatingFakeClient):
        """The address bar moves to a chat id the server never filled — so the
        move fails its reconcile, and so does every re-check."""

        def submit(self, request_id, prompt):
            result = super().submit(request_id, prompt)
            self.persisted.pop(NEW_CONV_URL, None)  # the turn evaporated
            return result

    client = PostsNowhereButLooksFine(responses=[stop_block()])
    client.find_finds_nothing = True
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.conversation_url == CONV_URL  # nothing was bound
    assert transcript_entries(config, "retirement_send_adopted") == []
    # Ambiguous, not carried on: the transport confirmed the send even though no
    # chat will show it, so a resend could still duplicate.
    assert orch.state.phase == Phase.NEEDS_USER.value
    assert [url for url, _rid, _p in client.submitted] == [PROJECT_URL]


def test_a_stranded_send_parks_and_names_where_it_last_saw_the_message(tmp_path):
    """Containment failure ALWAYS means the send happened — a chat exists only
    because a turn was accepted into it — so this can never be carried on. The
    message is in a chat outside the configured project, and the operator is
    told which one instead of being left to find it."""

    class OpensOutsideTheProject(RotatingFakeClient):
        def __init__(self, **kwargs):
            super().__init__(new_url="https://chatgpt.com/c/loose-chat", **kwargs)

    client = OpensOutsideTheProject(responses=[stop_block()])
    client.find_finds_nothing = True
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.resume_phase == Phase.SUBMITTING.value
    assert orch.state.conversation_url == CONV_URL  # never bound outside the project
    ambiguous = transcript_entries(config, "retirement_send_ambiguous")
    assert ambiguous and ambiguous[-1]["data"]["reason_code"] == "retirement_send_ambiguous"
    assert ambiguous[-1]["data"]["captured_url"] == "https://chatgpt.com/c/loose-chat"
    assert "loose-chat" in orch.state.question
    # The request was posted once, and never into the conversation being retired.
    assert [url for url, _rid, _p in client.submitted] == [PROJECT_URL]


def test_the_park_leaves_the_send_marked_so_a_retry_cannot_resend_it(tmp_path):
    """The park is not the mechanism — the MARK is. A resumed round must be
    safe at every entry point, not only through the door that parked."""

    class OpensOutsideTheProject(RotatingFakeClient):
        def __init__(self, **kwargs):
            super().__init__(new_url="https://chatgpt.com/c/loose-chat", **kwargs)

    client = OpensOutsideTheProject(responses=[stop_block()])
    client.find_finds_nothing = True
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)
    req = StateStore(config.state_file).load().pending_request
    assert req.send_attempted is True
    assert req.last_send_outcome == "accepted"

    # `--retry` resumes at `submitting`: the retirement refuses first
    # (`send_already_attempted`), the old chat is reconciled, the request is not
    # there, and the round parks again rather than posting a duplicate.
    orch.state.phase = Phase.SUBMITTING.value
    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert [url for url, _rid, _p in client.submitted] == [PROJECT_URL]
    deferred = transcript_entries(config, "retirement_deferred")
    assert deferred and deferred[-1]["data"]["reason_code"] == "send_already_attempted"
    assert transcript_entries(config, "submission_ambiguous")


def test_an_ambiguous_send_during_a_move_is_never_resent_either(tmp_path):
    """The submit raised AFTER the click, so the transport cannot say whether
    anything landed. Unknown acceptance never earns a resend anywhere else in
    this loop, and a retirement is not the exception."""

    class DiesAfterClickingSend(RotatingFakeClient):
        def submit(self, request_id, prompt):
            if self.conversation_url == PROJECT_URL:
                self.send_attempted = True  # the click happened...
                raise BrowserError("the tab died while confirming")  # ...the rest did not
            return super().submit(request_id, prompt)

    client = DiesAfterClickingSend(responses=[stop_block()])
    client.find_finds_nothing = True
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert client.submitted == []  # nothing the fake could record, and no resend
    failed = transcript_entries(config, "retirement_failed")
    assert failed and failed[-1]["data"]["send_certainty"] == "possible"
    ambiguous = transcript_entries(config, "retirement_send_ambiguous")
    assert ambiguous and ambiguous[-1]["data"]["send_certainty"] == "possible"


def test_an_ambiguous_send_that_turns_out_to_have_landed_is_adopted(tmp_path):
    """Same failure, different truth: the click did land, and the chat holding
    the request is found by CONTENT — the witness this transport already trusts
    over the address bar. Adopting beats parking, and beats resending twice
    over."""

    class DiesAfterTheSendLands(RotatingFakeClient):
        def submit(self, request_id, prompt):
            if self.conversation_url == PROJECT_URL:
                self.send_attempted = True
                self.seed(self._new_url, request_id)  # the server kept it
                raise BrowserError("the tab died while confirming")
            return super().submit(request_id, prompt)

    client = DiesAfterTheSendLands(responses=[stop_block()])
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.conversation_url == NEW_CONV_URL
    assert orch.state.phase == Phase.AWAITING.value
    adopted = transcript_entries(config, "retirement_send_adopted")
    assert adopted and adopted[-1]["data"]["url"] == NEW_CONV_URL
    assert client.find_calls  # found by content, not by the address bar
    assert [url for url, _rid, _p in client.submitted] == []


def test_a_send_the_transport_disproved_and_no_chat_holds_carries_on(tmp_path):
    """The one post-send failure that may still use the old thread, and only on
    the pair of facts `submission_rejected` already treats as conclusive:
    network disproof AND confirmed absence from the project. ChatGPT mints a
    chat id only for an accepted turn, so a refused turn leaves nothing
    behind."""
    client = RotatingFakeClient(
        submit_results=[SubmitResult.REJECTED, SubmitResult.CONFIRMED],
        responses=[stop_block()],
    )
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.phase != Phase.NEEDS_USER.value
    disproven = transcript_entries(config, "retirement_send_disproven")
    assert disproven and disproven[-1]["data"]["reason_code"] == "retirement_send_disproven"
    assert client.find_calls  # absence was CHECKED, not assumed
    # The round went ahead in the old thread — the only place the request is.
    assert [url for url, _rid, _p in client.submitted] == [PROJECT_URL, CONV_URL]
    assert orch.state.conversation_url == CONV_URL


def test_a_search_that_could_not_run_is_not_treated_as_absence(tmp_path):
    """The carry-on above rests on CONFIRMED absence, not on the absence of an
    answer. A search that failed knows nothing about where the message is, so
    the disproven send goes back to being ambiguous."""

    class SearchIsBroken(RotatingFakeClient):
        def find_conversation_with(self, request_id, project_url, limit=6):
            raise BrowserError("the project sidebar never rendered")

    client = SearchIsBroken(
        submit_results=[SubmitResult.REJECTED], responses=[stop_block()]
    )
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert transcript_entries(config, "retirement_send_disproven") == []
    ambiguous = transcript_entries(config, "retirement_send_ambiguous")
    assert ambiguous and ambiguous[-1]["data"]["send_certainty"] == "disproven"
    assert [url for url, _rid, _p in client.submitted] == [PROJECT_URL]


def test_a_disproved_send_that_actually_landed_is_adopted_not_repeated(tmp_path):
    """History outranks the network here exactly as it does in
    `_step_submission_rejected`: the status code said the send failed, the
    project says otherwise, and the project wins."""

    class RejectsButKeepsIt(RotatingFakeClient):
        def submit(self, request_id, prompt):
            result = super().submit(request_id, prompt)
            if self.conversation_url == PROJECT_URL:
                self.seed(self._new_url, request_id)  # persisted despite the verdict
            return result

    client = RejectsButKeepsIt(
        submit_results=[SubmitResult.REJECTED], responses=[stop_block()]
    )
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.conversation_url == NEW_CONV_URL
    assert orch.state.phase == Phase.AWAITING.value
    adopted = transcript_entries(config, "retirement_send_adopted")
    assert adopted and adopted[-1]["data"]["send_certainty"] == "disproven"
    assert [url for url, _rid, _p in client.submitted] == [PROJECT_URL]


def test_an_adoption_never_binds_the_thread_it_is_retiring(tmp_path):
    """A content search that answers with the OLD conversation is not a
    replacement chat — adopting it would spend an epoch and retire nothing,
    leaving the loop in the same slow thread while its record says it moved."""

    class FindsOnlyTheOldChat(RotatingFakeClient):
        def submit(self, request_id, prompt):
            result = super().submit(request_id, prompt)
            self.persisted.pop(NEW_CONV_URL, None)
            self.seed(CONV_URL, request_id)  # the only chat that holds it
            return result

    client = FindsOnlyTheOldChat(responses=[stop_block()])
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.conversation_url == CONV_URL
    assert orch.state.conversation_epoch == 0  # nothing was "moved"
    assert transcript_entries(config, "retirement_send_adopted") == []
    assert orch.state.phase == Phase.NEEDS_USER.value


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
    the configured project is refused here exactly as it is for a rotation:
    nothing is bound, and nothing is bound to the loose chat either.

    What it does NOT do is carry on. Containment can only fail after the send —
    the chat exists because a turn was accepted into it — so the message is out
    there, and the difference is covered in full by
    `test_a_stranded_send_parks_and_names_where_it_last_saw_the_message`."""

    class OpensOutsideTheProject(RotatingFakeClient):
        def __init__(self, **kwargs):
            super().__init__(new_url="https://chatgpt.com/c/loose-chat", **kwargs)

    client = OpensOutsideTheProject(responses=[stop_block()])
    client.find_finds_nothing = True
    orch, _store, config = build(tmp_path, client, state=degraded_state())

    orch.run(max_steps=1)

    assert orch.state.conversation_url == CONV_URL
    assert orch.state.pending_request.conversation_url == CONV_URL
    assert transcript_entries(config, "retirement_failed")
    assert transcript_entries(config, "retirement_send_adopted") == []


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
