"""A send that never appears means the CONVERSATION is wedged, not the browser.

Observed 2026-08-17, 09:05–09:15, and it fooled every check that existed: the
composer was clickable, no throttle modal was up, Chrome was healthy with 12
CDP targets, and the account was demonstrably writing — an operator posted by
hand in a DIFFERENT conversation. The loop's own submission simply never
appeared (`submitted=True`, `send_attempted=True`, the chat pinned at 33
messages for ten minutes). The symptom surfaced as a locator timeout on the
message the loop believed it sent, which reads as a browser fault, so the
recovery chosen was RESTART CHROME — every 45 seconds, for ten minutes,
against a fault no restart could ever fix. Rotation fixed it in seconds, by
hand, because nothing classified the state.

What these tests pin, in order:

* proven bounded absence of the loop's own submission — on an attachable,
  un-throttled page, with the message tail mounted to its END — raises
  `ConversationUnusableError(code="submission_never_appeared")`, from BOTH
  surfaces the fault wears: the response-start bound expiring cleanly, and
  the incident's actual shape — the awaiting read itself dying with a
  locator timeout already labelled a lost session
  (`_classify_awaiting_read_failure`);
* a message absent only because the virtualized tail is unmounted does NOT
  condemn the chat: a mount that sights it, and a mount that cannot settle,
  both leave the caller's own fault standing (the ordinary `stage="start"`
  timeout, or the original read failure);
* a throttle discovered at the moment of conclusion is still routed as a
  throttle, and a browser whose probe cannot read the page keeps the
  lost-session fault — the two worlds this classification must not swallow;
* at the orchestrator, the fault PARKS (`conversation_unusable`) WITHOUT
  restarting Chrome and WITHOUT charging the browser failure budget
  (`consecutive_failures`) — the brw-03 rule: a budget that decides recovery is
  hopeless must not be spent on a fault no restart could fix — including
  end-to-end through the REAL client code for the exact reported locator-timeout
  shape.

Until brw-15 (2026-08-25) the last bullet said "rotates": the fault opened a
replacement chat in the configured ChatGPT project and moved the request into
it. That machinery is gone, and the classification is what survives it — which
is the right way round, since the 2026-08-17 incident was a MISCLASSIFICATION
(ten minutes of 45-second Chrome restarts against a chat no restart could fix),
not a missing recovery. An operator now moves the loop by hand, which is what
the operator did on the day.
"""

import pytest

from autoloop.errors import (
    ConversationUnusableError,
    RateLimitedError,
    ResponseTimeoutError,
    SessionLostError,
)
from autoloop.state import LoopState, Phase, StateStore

from test_chatgpt_client import (  # noqa: E402 - see conftest sys.path
    RID,
    SEL,
    FakeClock,
    diagnostics,
    make_client,
    read_meta,
)
from test_chatgpt_client import CONV_URL as CLIENT_CONV_URL  # noqa: E402
from test_transport_recovery import (  # noqa: E402 - see conftest sys.path
    CONV_URL,
    PROJECT_URL,
    RotatingFakeClient,
    build,
    pending,
    transcript_entries,
)


def _filler(count, marker="earlier"):
    """`count` complete user/assistant exchanges — none carrying the request."""
    out = []
    for i in range(count):
        out.append(("user", f"{marker} question {i}"))
        out.append(("assistant", f"{marker} answer {i}"))
    return out


class WedgedPageSession:
    """The 2026-08-17 page, as `await_response` reads it: every read answers
    (attachable), no throttle modal, composer present — and the loop's own
    submission nowhere in the conversation.

    The message list virtualizes from the TOP: a fresh window mounts `window`
    messages, each end-gesture paints `step` more and honestly reports whether
    the view reached the end — the position signal absence needs. A session
    without the gesture (see `NoPositionSession`) can therefore never
    establish absence, which is one of the guards under test.
    """

    def __init__(
        self, history, *, window=2, step=2, throttle_after_scrolls=None, read_faults=()
    ):
        self.history = list(history)
        self.window = window
        self.step = step
        self.mounted = window
        self.scrolls = 0
        #: After this many gestures the account-throttle overlay goes up —
        #: mid-mount, past every per-iteration check the await loop ran.
        self.throttle_after_scrolls = throttle_after_scrolls
        #: Exceptions to raise from message-list reads, consumed one per read.
        #: How the incident actually surfaced: the read waiting for the loop's
        #: own message died with a locator timeout (a `SessionLostError` once
        #: `PlaywrightSession._call` labels it), while every LATER read of the
        #: same page answered fine.
        self.read_faults = list(read_faults)
        self.message_reads = 0
        self.current_url = CLIENT_CONV_URL
        self.present = {SEL.composer}
        self.keys = []
        self.navigations = []

    # -- BrowserSession protocol ------------------------------------------
    def goto(self, url):
        self.navigations.append(f"goto:{url}")
        self.current_url = url
        self.mounted = self.window

    def reload(self):
        self.navigations.append("reload")
        self.mounted = self.window

    def url(self):
        return self.current_url

    def exists(self, selector):
        return selector in self.present

    def is_enabled(self, selector):
        return selector in self.present

    def click(self, selector):
        pass

    def focus(self, selector):
        pass

    def press(self, keys):
        # End paints nothing here: without the scroll capability the gesture
        # is blind, exactly like a real End that went to the wrong element.
        self.keys.append(keys)

    def insert_text(self, text):
        pass

    def inner_text(self, selector):
        return ""

    def elements(self, selector, attr):
        if selector == SEL.message:
            self.message_reads += 1
            if self.read_faults:
                raise self.read_faults.pop(0)
            return self.history[: self.mounted]
        return []

    def scroll_to_end(self, selector):
        assert selector == SEL.message
        self.scrolls += 1
        self.mounted = min(self.mounted + self.step, len(self.history))
        if (
            self.throttle_after_scrolls is not None
            and self.scrolls >= self.throttle_after_scrolls
        ):
            self.present.add(SEL.rate_limit_modal)
        return self.mounted >= len(self.history)

    def screenshot(self, path):
        from pathlib import Path

        Path(path).write_bytes(b"\x89PNG-fake")

    def html(self):
        return "<html>fake</html>"

    def close(self):
        pass


class NoPositionSession(WedgedPageSession):
    """The same page through an adapter that cannot measure its scroll
    position. `scroll_to_end = None` makes the client's capability probe
    (`getattr(session, "scroll_to_end", None)`) miss, so `_scroll_message_tail`
    falls back to the End key — which here mounts nothing and reports no
    position. Such a session's sightings are as good as anyone's; it can
    never rule anything OUT."""

    scroll_to_end = None


#: The lost-session label the incident actually wore: `PlaywrightSession._call`
#: wrapping the locator timeout on the message the loop was waiting for —
#: `.nth(33)`, the message after the 33 the wedged chat was pinned at.
LOCATOR_TIMEOUT = (
    "browser session lost (TimeoutError: Locator.get_attribute: Timeout "
    "30000ms exceeded. waiting for locator(\"[data-message-author-role]\")"
    ".nth(33))"
)


def _locator_timeout():
    return SessionLostError(LOCATOR_TIMEOUT)


class DeadReadsSession(WedgedPageSession):
    """brw-11's state 3, as `await_response` meets it: a browser nothing can
    attach to, so EVERY read dies — the classification probe included. With no
    page to gather evidence from, the lost-session fault must stand and keep
    its restart recovery."""

    def _dead(self):
        raise SessionLostError(
            "browser session lost (Connection closed while reading from the driver)"
        )

    def url(self):
        self._dead()

    def exists(self, selector):
        self._dead()

    def elements(self, selector, attr):
        self._dead()


# ---- 1. proven bounded absence condemns the CONVERSATION --------------------


def test_a_submission_that_never_appears_is_the_conversation_not_the_browser(tmp_path):
    """The 2026-08-17 state, classified: request absent, tail mounted to the
    demonstrable END, page attachable and un-throttled throughout — a wedged
    conversation, raised as the one error the orchestrator routes away from the
    browser recovery."""
    session = WedgedPageSession(_filler(3))  # 6 messages, none the request
    client = make_client(session, FakeClock(), tmp_path, response_start_timeout=5.0)

    with pytest.raises(ConversationUnusableError) as excinfo:
        client.await_response(RID)

    assert excinfo.value.code == "submission_never_appeared"
    assert "never appeared" in str(excinfo.value)
    # Absence was concluded from a mounted tail, not from the opening window.
    assert session.scrolls >= 2
    meta = read_meta(diagnostics(tmp_path)[-1])
    assert meta["tag"] == "submission-never-appeared"
    assert meta["stage"] == "submission-absent"


# ---- 1b. the incident's actual surface: the read itself dies ----------------


def test_the_locator_timeout_on_the_missing_message_is_probed_not_restarted(tmp_path):
    """THE reported shape, at the client: the read waiting for the loop's own
    message dies with a locator timeout (already labelled a lost session),
    every LATER read of the same page answers, the tail mounts to its
    demonstrable END, and the message is provably absent. What must leave
    `await_response` is the wedged-conversation classification — never the
    browser fault the label claims, which is what bought ten minutes of
    45-second Chrome restarts on 2026-08-17."""
    session = WedgedPageSession(_filler(3), read_faults=[_locator_timeout()])
    client = make_client(session, FakeClock(), tmp_path, response_start_timeout=5.0)

    with pytest.raises(ConversationUnusableError) as excinfo:
        client.await_response(RID)

    assert excinfo.value.code == "submission_never_appeared"
    # The failed read really happened, and absence was then concluded from a
    # mounted tail read off the same, demonstrably attachable page.
    assert session.message_reads > 1
    assert session.scrolls >= 2
    meta = read_meta(diagnostics(tmp_path)[-1])
    assert meta["tag"] == "submission-never-appeared"
    assert meta["stage"] == "submission-absent"


def test_a_read_failure_with_the_request_present_keeps_the_browser_fault(tmp_path):
    """The transient world: the read died, but the probe finds the chat
    demonstrably HOLDING the loop's message. Nothing is wedged, so nothing may
    condemn the conversation — the original lost-session fault stands and takes
    the restart recovery it always had."""
    history = _filler(1) + [("user", f"[autoloop request {RID} | iteration 1]")]
    session = WedgedPageSession(
        history, window=len(history), read_faults=[_locator_timeout()]
    )
    client = make_client(session, FakeClock(), tmp_path, response_start_timeout=5.0)

    with pytest.raises(SessionLostError) as excinfo:
        client.await_response(RID)

    assert "nth(33)" in str(excinfo.value)  # the original fault, not a relabel
    assert session.scrolls == 0  # the sighting needed no gestures


def test_a_dead_browser_still_takes_the_lost_session_route(tmp_path):
    """brw-11's state 3: EVERY read dies, the classification probe included.
    No attachable page means no evidence, and no evidence licenses nothing —
    the original fault leaves `await_response` unrelabelled, so the
    orchestrator's restart-and-budget recovery still owns it."""
    session = DeadReadsSession(_filler(3))
    client = make_client(session, FakeClock(), tmp_path, response_start_timeout=5.0)

    with pytest.raises(SessionLostError) as excinfo:
        client.await_response(RID)

    assert "Connection closed" in str(excinfo.value)
    assert session.scrolls == 0  # the probe never got as far as a gesture


def test_an_unsettleable_probe_keeps_the_browser_fault(tmp_path):
    """The read died AND absence cannot be proven: an adapter with no position
    signal mounts forever without ruling anything out. Unproven absence
    licenses nothing — the original fault stands, exactly as it does when the
    clean-timeout path cannot settle."""
    session = NoPositionSession(_filler(3), read_faults=[_locator_timeout()])
    client = make_client(
        session, FakeClock(), tmp_path, response_start_timeout=5.0, tail_mount_attempts=4
    )

    with pytest.raises(SessionLostError) as excinfo:
        client.await_response(RID)

    assert "nth(33)" in str(excinfo.value)
    assert session.keys.count("End") == 4  # it tried; it could not prove


def test_a_throttle_discovered_by_the_probe_is_still_a_throttle(tmp_path):
    """The probe's conclusion checks outrank BOTH labels: an overlay up at the
    moment absence settles routes as the account limit — never left as a lost
    session (a restart adds a request to the window that caused it), and never
    relabelled a wedged chat (a park about the conversation names the wrong
    cause, and back-off is the only thing that clears a throttle)."""
    session = WedgedPageSession(
        _filler(3), throttle_after_scrolls=1, read_faults=[_locator_timeout()]
    )
    client = make_client(
        session, FakeClock(), tmp_path, response_start_timeout=5.0, tail_mount_attempts=8
    )

    with pytest.raises(RateLimitedError):
        client.await_response(RID)


# ---- 2. an unmounted tail is not absence ------------------------------------


def test_a_message_hidden_by_the_unmounted_tail_does_not_condemn_the_chat(tmp_path):
    """The request IS in the conversation — six scrolls down, exactly where
    virtualization left the 2026-08-05 request. The mount sights it, so the
    ordinary silent-conversation timeout fires and the chat is never
    condemned."""
    history = _filler(6) + [("user", f"[autoloop request {RID} | iteration 1]")]
    session = WedgedPageSession(history)  # initial window misses the tail
    client = make_client(session, FakeClock(), tmp_path, response_start_timeout=5.0)

    with pytest.raises(ResponseTimeoutError) as excinfo:
        client.await_response(RID)

    assert excinfo.value.stage == "start"
    assert session.scrolls > 0  # the tail really was mounted before ruling


def test_an_unprovable_absence_keeps_the_ordinary_timeout(tmp_path):
    """A session that cannot report a scroll position can never establish
    absence — the mount never settles, so even a genuinely missing submission
    falls back to the ordinary timeout rather than condemning the chat on
    evidence nobody gathered."""
    session = NoPositionSession(_filler(3))
    assert getattr(session, "scroll_to_end", None) is None  # capability absent
    client = make_client(
        session, FakeClock(), tmp_path, response_start_timeout=5.0, tail_mount_attempts=4
    )

    with pytest.raises(ResponseTimeoutError) as excinfo:
        client.await_response(RID)

    assert excinfo.value.stage == "start"
    assert session.keys.count("End") == 4  # it tried; it could not prove


def test_a_request_visible_in_the_window_never_triggers_the_mount(tmp_path):
    """The silent-conversation case unchanged: the request is on the page and
    the model is merely quiet. No gesture is spent and the ordinary
    `stage=\"start\"` timeout keeps the case."""
    history = _filler(1) + [("user", f"[autoloop request {RID} | iteration 1]")]
    session = WedgedPageSession(history, window=len(history))
    client = make_client(session, FakeClock(), tmp_path, response_start_timeout=5.0)

    with pytest.raises(ResponseTimeoutError) as excinfo:
        client.await_response(RID)

    assert excinfo.value.stage == "start"
    assert session.scrolls == 0


# ---- 3. the other worlds keep their own routes ------------------------------


def test_a_throttle_arriving_mid_mount_is_still_a_throttle(tmp_path):
    """The overlay going up while the mount dwells must be routed as the
    account limit it is — never relabelled a wedged conversation, because that
    park names the wrong cause and forecloses the back-off that clears it."""
    session = WedgedPageSession(_filler(3), throttle_after_scrolls=1)
    client = make_client(
        session, FakeClock(), tmp_path, response_start_timeout=5.0, tail_mount_attempts=8
    )

    with pytest.raises(RateLimitedError):
        client.await_response(RID)


# ---- 4. at the orchestrator: park, no restart, no budget --------------------
#
# The recovery this section asserted was ROTATION until brw-15 (2026-08-25)
# removed it. The rule it was written for is untouched and is what these tests
# still pin: a fault established THROUGH a working, un-throttled page must not
# spend the browser recovery — no Chrome restart, no `consecutive_failures`
# increment — because no restart could fix it. What changed is only where the
# fault goes afterwards: a `conversation_unusable` park naming the wedged chat,
# instead of a replacement chat opened automatically.


def _missing_submission_error():
    return ConversationUnusableError(
        "the submission this loop made (alr-test-0001) never appeared: the "
        "conversation was read to its end without finding it",
        code="submission_never_appeared",
    )


def _raise_missing_submission(client):
    raise _missing_submission_error()


def test_a_missing_submission_parks_without_restarting_or_charging_the_budget(tmp_path):
    """The whole point of the classification, and the half of it that outlives
    the rotation: the browser is never restarted (a restart command IS
    configured, so one would be visible if attempted), and the browser failure
    budget — the counter that decides recovery is hopeless — is not spent on a
    fault no restart could fix. The park names the chat and the error's own
    code, so the operator sees WHICH shape of unusable this was."""
    client = RotatingFakeClient(responses=[_raise_missing_submission])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True, send_attempted=True)
    orch, store, config = build(tmp_path, client, state=state)
    # A restart that DID happen would run this and log `browser_restarted`.
    object.__setattr__(config.browser, "restart_command", ("true",))

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.park_kind == "loop_fatal"
    assert orch.state.resume_phase == Phase.AWAITING.value
    # Nothing moved: the loop is still pinned to the conversation it condemned,
    # which is exactly what the park has to tell the operator.
    assert orch.state.rotations == 0
    assert orch.state.conversation_url == CONV_URL
    assert orch.state.conversation_epoch == 0
    assert orch.state.pending_request.conversation_url == CONV_URL
    # No restart, no browser-failure accounting.
    assert orch.state.consecutive_failures == 0
    assert orch.state.browser_restart_skips == 0
    assert transcript_entries(config, "browser_restarted") == []
    assert transcript_entries(config, "browser_error") == []
    assert transcript_entries(config, "conversation_rotated") == []
    unusable = transcript_entries(config, "conversation_unusable")
    assert unusable and unusable[0]["data"]["reason_code"] == "submission_never_appeared"
    question = orch.state.question or ""
    assert "submission_never_appeared" in question
    assert CONV_URL in question
    # And it never navigated anywhere to find a replacement.
    assert PROJECT_URL not in client.retargets
    assert client.submitted == []


def test_the_park_survives_a_restart_of_the_process(tmp_path):
    """Same durability rule the rotation had: whatever this fault decided is on
    disk before anything else runs, so the next process resumes on it rather
    than re-deciding from a state that never got written."""
    client = RotatingFakeClient(responses=[_raise_missing_submission])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True, send_attempted=True)
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    reloaded = StateStore(config.state_file).load()
    assert reloaded.phase == Phase.NEEDS_USER.value
    assert reloaded.rotations == 0
    assert reloaded.conversation_url == CONV_URL
    assert reloaded.pending_request.conversation_url == CONV_URL
    assert reloaded.last_rotation in (None, {})
    assert "submission_never_appeared" in (reloaded.question or "")


class IncidentAwaitClient(RotatingFakeClient):
    """`RotatingFakeClient` whose awaiting runs the REAL
    `BrowserChatGPT.await_response` over a scripted page — so an orchestrator
    test exercises the actual classification code rather than a stand-in that
    raises the right error by fiat."""

    def __init__(self, browser_client, **kwargs):
        super().__init__(**kwargs)
        self._browser = browser_client

    def await_response(self, request_id):
        self.awaited.append((self.conversation_url, request_id))
        return self._browser.await_response(request_id)


def test_the_reported_locator_timeout_parks_without_restart_or_budget(tmp_path):
    """The full reported shape, through the orchestrator and the REAL client
    code: a locator/read timeout while waiting for the loop's own submitted
    message (`submitted=True`, `send_attempted=True`), then healthy-page and
    mounted-tail evidence showing that message absent. No Chrome restart (a
    restart command IS configured, so one would log `browser_restarted`), and no
    browser-budget increment. This is the exact path that spent 2026-08-17
    restarting a healthy browser; the classification is what stopped that, and
    it is unaffected by brw-15 removing the recovery it used to feed."""
    session = WedgedPageSession(_filler(3), read_faults=[_locator_timeout()])
    browser_client = make_client(
        session, FakeClock(), tmp_path, response_start_timeout=5.0
    )
    client = IncidentAwaitClient(browser_client)
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True, send_attempted=True)
    orch, store, config = build(tmp_path, client, state=state)
    object.__setattr__(config.browser, "restart_command", ("true",))

    orch.run(max_steps=1)

    assert orch.state.phase == Phase.NEEDS_USER.value
    assert orch.state.rotations == 0
    assert orch.state.conversation_url == CONV_URL
    assert orch.state.consecutive_failures == 0
    assert orch.state.browser_restart_skips == 0
    assert transcript_entries(config, "browser_restarted") == []
    assert transcript_entries(config, "browser_error") == []
    assert transcript_entries(config, "conversation_rotated") == []
    unusable = transcript_entries(config, "conversation_unusable")
    assert unusable and unusable[0]["data"]["reason_code"] == "submission_never_appeared"


def test_a_dead_session_in_awaiting_still_takes_the_restart_path(tmp_path):
    """brw-11's state 3 boundary, from this phase: a `SessionLostError` that
    ESCAPES the client is one the client's own probe could not do better
    than — no attachable page, a sighted request, or unprovable absence (see
    the section-1b tests for which is which). At the orchestrator that is a
    browser fault and keeps the restart-and-budget recovery: a fresh chat
    cannot fix a browser nothing can attach to."""

    def _dead(client):
        raise SessionLostError("browser session lost (TimeoutError: ...)")

    client = RotatingFakeClient(responses=[_dead])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True, send_attempted=True)
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    assert orch.state.rotations == 0
    assert orch.state.consecutive_failures == 1  # the ordinary budget, as before
    assert orch.state.phase == Phase.AWAITING.value  # retried with a fresh client
    assert transcript_entries(config, "conversation_rotated") == []
