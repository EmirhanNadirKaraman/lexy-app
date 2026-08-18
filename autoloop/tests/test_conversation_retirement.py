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
  `ConversationUnusableError(code="submission_never_appeared")`;
* a message absent only because the virtualized tail is unmounted does NOT
  condemn the chat: a mount that sights it, and a mount that cannot settle,
  both fall back to the ordinary `stage="start"` timeout;
* a throttle discovered at the moment of conclusion is still routed as a
  throttle, and a dead session still takes the restart path — the two worlds
  this classification must not swallow;
* at the orchestrator, the fault rotates WITHOUT restarting Chrome and
  WITHOUT charging the browser failure budget (`consecutive_failures`) — the
  brw-03 rule: a budget that decides recovery is hopeless must not be spent
  on a fault no restart could fix.
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
    NEW_CONV_URL,
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

    def __init__(self, history, *, window=2, step=2, throttle_after_scrolls=None):
        self.history = list(history)
        self.window = window
        self.step = step
        self.mounted = window
        self.scrolls = 0
        #: After this many gestures the account-throttle overlay goes up —
        #: mid-mount, past every per-iteration check the await loop ran.
        self.throttle_after_scrolls = throttle_after_scrolls
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


# ---- 1. proven bounded absence condemns the CONVERSATION --------------------


def test_a_submission_that_never_appears_is_the_conversation_not_the_browser(tmp_path):
    """The 2026-08-17 state, classified: request absent, tail mounted to the
    demonstrable END, page attachable and un-throttled throughout — a wedged
    conversation, raised as the one error that authorizes rotation."""
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


# ---- 2. an unmounted tail is not absence ------------------------------------


def test_a_message_hidden_by_the_unmounted_tail_does_not_condemn_the_chat(tmp_path):
    """The request IS in the conversation — six scrolls down, exactly where
    virtualization left the 2026-08-05 request. The mount sights it, so the
    ordinary silent-conversation timeout fires and rotation is never
    authorized."""
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
    the model is merely quiet. No gesture is spent and the existing
    `stage=\"start\"` machinery (three-strike rotation) keeps the case."""
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
    account limit it is — never relabelled a wedged conversation, because the
    rotation it would authorize SENDS into the throttled account."""
    session = WedgedPageSession(_filler(3), throttle_after_scrolls=1)
    client = make_client(
        session, FakeClock(), tmp_path, response_start_timeout=5.0, tail_mount_attempts=8
    )

    with pytest.raises(RateLimitedError):
        client.await_response(RID)


# ---- 4. at the orchestrator: rotate, no restart, no budget ------------------


def _missing_submission_error():
    return ConversationUnusableError(
        "the submission this loop made (alr-test-0001) never appeared: the "
        "conversation was read to its end without finding it",
        code="submission_never_appeared",
    )


def _raise_missing_submission(client):
    raise _missing_submission_error()


def test_a_missing_submission_rotates_without_restarting_or_charging_the_budget(tmp_path):
    """The whole point of the classification: the recovery is rotation, the
    browser is never restarted (a restart command IS configured, so one would
    be visible if attempted), and the browser failure budget — the counter
    that decides recovery is hopeless — is not spent on a fault no restart
    could fix."""
    client = RotatingFakeClient(responses=[_raise_missing_submission])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True, send_attempted=True)
    orch, store, config = build(tmp_path, client, state=state)
    # A restart that DID happen would run this and log `browser_restarted`.
    object.__setattr__(config.browser, "restart_command", ("true",))

    orch.run(max_steps=1)

    assert orch.state.rotations == 1
    assert orch.state.conversation_url == NEW_CONV_URL
    assert orch.state.conversation_epoch == 1
    assert orch.state.pending_request.conversation_url == NEW_CONV_URL
    assert orch.state.phase == Phase.AWAITING.value
    # No restart, no browser-failure accounting: the fault is charged to the
    # rotation budget alone.
    assert orch.state.consecutive_failures == 0
    assert orch.state.browser_restart_skips == 0
    assert transcript_entries(config, "browser_restarted") == []
    assert transcript_entries(config, "browser_error") == []
    rotated = transcript_entries(config, "conversation_rotated")
    assert len(rotated) == 1
    assert rotated[0]["data"]["reason"] == "submission_never_appeared"
    assert rotated[0]["data"]["old_url"] == CONV_URL
    assert rotated[0]["data"]["new_url"] == NEW_CONV_URL
    unusable = transcript_entries(config, "conversation_unusable")
    assert unusable and unusable[0]["data"]["reason_code"] == "submission_never_appeared"
    # Rotation opened the project page and bound the minted chat, as ever.
    assert PROJECT_URL in client.retargets
    assert client.retargets[-1] == NEW_CONV_URL


def test_the_rotation_survives_a_restart_of_the_process(tmp_path):
    """Same durability rule as every other rotation trigger: the rebinding is
    on disk before anything else runs."""
    client = RotatingFakeClient(responses=[_raise_missing_submission])
    state = LoopState.new(CONV_URL)
    state.phase = Phase.AWAITING.value
    state.pending_request = pending(submitted=True, send_attempted=True)
    orch, store, config = build(tmp_path, client, state=state)

    orch.run(max_steps=1)

    reloaded = StateStore(config.state_file).load()
    assert reloaded.rotations == 1
    assert reloaded.conversation_url == NEW_CONV_URL
    assert reloaded.pending_request.conversation_url == NEW_CONV_URL
    assert reloaded.last_rotation["reason"] == "submission_never_appeared"


def test_a_dead_session_in_awaiting_still_takes_the_restart_path(tmp_path):
    """brw-11's state 3 boundary, from this phase: no attachable page at all
    is a DEAD BROWSER, and it keeps the restart-and-budget recovery. The
    missing-submission classification must never swallow it — a fresh chat
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
