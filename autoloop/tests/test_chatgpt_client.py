"""Browser transport against an in-memory fake session that models what broke
in production: ChatGPT renders a user bubble optimistically, and that bubble
does NOT mean the server accepted the message.

The fake keeps `persisted` (server truth) separate from `dom` (what the page
currently shows). Only `goto`/`reload` re-derive `dom` from `persisted`, exactly
like a real page load — so an optimistic-only send disappears on reload, which
is the 2026-07-29 failure reproduced as a test.

No playwright, no network, no clock (virtual time throughout).
"""

from pathlib import Path

import pytest

from autoloop.browser.chatgpt import BrowserChatGPT, SubmitResult
from autoloop.browser.selectors import ChatGPTSelectors
from autoloop.errors import (
    BrowserError,
    LoginExpiredError,
    ResponseTimeoutError,
    SubmissionError,
)

SEL = ChatGPTSelectors()
CONV_URL = "https://chatgpt.com/g/g-p-project123/c/conv456"
RID = "alr-abcd1234-0001"
PROMPT = f"[autoloop request {RID} | iteration 1]\n\nCONTEXT\n\nbody\n\nEND-OF-PROMPT"


class FakeClock:
    """Virtual time: sleep() advances it and fires scripted events by call count."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = 0
        self.events: dict[int, object] = {}

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        self.sleeps += 1
        event = self.events.pop(self.sleeps, None)
        if event:
            event()


class FakeSession:
    """Models ChatGPT's composer + optimistic rendering + server persistence.

    send_mode:
      "persist"         — the message reaches the server (normal case)
      "optimistic_only" — a bubble is drawn but nothing persists (the bug)
      "drop"            — the click does nothing at all
    """

    def __init__(self, send_mode="persist", editor_registers_input=True):
        self.current_url = CONV_URL
        self.present = {SEL.composer, SEL.send_button}
        self.persisted: list[tuple[str, str]] = []
        self.dom: list[tuple[str, str]] = []
        self.composer = ""
        self.send_enabled = False
        self.send_mode = send_mode
        self.editor_registers_input = editor_registers_input
        #: When set, any navigation redirects to the auth page, as a real
        #: logged-out ChatGPT does.
        self.logged_out = False
        self.navigations: list[str] = []  # goto/reload calls, in order
        self.clicks: list[str] = []
        self.focused: list[str] = []
        self.keys: list[str] = []
        self.inserted: list[str] = []
        self.closed = False

    # -- server-truth helper used by tests ---------------------------------
    def seed(self, messages):
        self.persisted = list(messages)
        self.dom = list(messages)

    def _render_from_server(self):
        self.dom = list(self.persisted)
        self.composer = ""
        self.send_enabled = False

    # -- BrowserSession protocol ------------------------------------------
    def goto(self, url):
        self.navigations.append(f"goto:{url}")
        if self.logged_out:
            self.current_url = "https://auth.openai.com/authorize"
            self.present = {SEL.login_markers[0]}
            self.dom = []
            return
        self.current_url = url
        self._render_from_server()

    def reload(self):
        self.navigations.append("reload")
        self._render_from_server()

    def url(self):
        return self.current_url

    def exists(self, selector):
        return selector in self.present

    def is_enabled(self, selector):
        if selector == SEL.send_button:
            return self.send_enabled
        return selector in self.present

    def click(self, selector):
        self.clicks.append(selector)
        if selector != SEL.send_button:
            return
        if not self.send_enabled:
            return  # a disabled control cannot send
        text = self.composer
        if self.send_mode == "persist":
            self.persisted.append(("user", text))
            self.dom.append(("user", text))
        elif self.send_mode == "optimistic_only":
            self.dom.append(("user", text))  # drawn, never persisted
        self.composer = ""
        self.send_enabled = False

    def focus(self, selector):
        self.focused.append(selector)

    def press(self, keys):
        self.keys.append(keys)
        if keys == "Delete":
            self.composer = ""
            self.send_enabled = False

    def insert_text(self, text):
        self.inserted.append(text)
        self.composer += text
        if self.editor_registers_input:
            self.send_enabled = bool(self.composer)

    def inner_text(self, selector):
        return self.composer if selector == SEL.composer else ""

    def elements(self, selector, attr):
        return list(self.dom)

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG-fake")

    def html(self):
        return "<html>fake</html>"

    def close(self):
        self.closed = True


def make_client(session, clock, tmp_path, **overrides) -> BrowserChatGPT:
    kwargs = dict(
        response_timeout=30.0,
        response_start_timeout=20.0,
        submit_timeout=10.0,
        composer_timeout=10.0,
        input_sync_timeout=10.0,
        send_ready_timeout=10.0,
        reconcile_timeout=10.0,
        poll_interval=1.0,
        stability_seconds=2.0,
        diagnostics_dir=tmp_path / "diag",
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    kwargs.update(overrides)
    return BrowserChatGPT(session, CONV_URL, **kwargs)


def diagnostics(tmp_path):
    diag = tmp_path / "diag"
    return sorted(diag.iterdir()) if diag.exists() else []


def read_meta(folder):
    import json

    return json.loads((folder / "meta.json").read_text())


OLD_TURN = [("user", "conversation reserved for autoloop"), ("assistant", "Understood.")]


# ---- attach: navigation only when needed ------------------------------------


def test_attach_does_not_navigate_when_already_on_conversation(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    make_client(session, clock, tmp_path).attach()
    assert session.navigations == []  # crucially: no reload of a live page


def test_attach_navigates_when_page_is_elsewhere(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.current_url = "https://chatgpt.com/c/some-other-chat"
    make_client(session, clock, tmp_path).attach()
    assert session.navigations == [f"goto:{CONV_URL}"]


def test_attach_ignores_query_and_trailing_slash(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.current_url = CONV_URL + "/?model=gpt-5"
    make_client(session, clock, tmp_path).attach()
    assert session.navigations == []


def test_attach_times_out_without_composer(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.present = set()
    with pytest.raises(BrowserError):
        make_client(session, clock, tmp_path, composer_timeout=3.0).attach()
    assert diagnostics(tmp_path)


def test_attach_detects_logged_out(tmp_path):
    """A logged-out profile redirects the navigation to the auth page."""
    session, clock = FakeSession(), FakeClock()
    session.logged_out = True
    session.current_url = "https://chatgpt.com/c/elsewhere"  # forces a navigation
    with pytest.raises(LoginExpiredError):
        make_client(session, clock, tmp_path).attach()
    assert diagnostics(tmp_path)


def test_attach_detects_login_marker_without_redirect(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.present.add(SEL.login_markers[0])
    with pytest.raises(LoginExpiredError):
        make_client(session, clock, tmp_path).attach()


# ---- submission confirmation ------------------------------------------------


def test_optimistic_bubble_alone_does_not_confirm_submission(tmp_path):
    """The exact production failure: bubble drawn, nothing persisted, no
    assistant turn → UNCONFIRMED, never CONFIRMED."""
    session, clock = FakeSession(send_mode="optimistic_only"), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)
    result = client.submit(RID, PROMPT)
    assert result is SubmitResult.UNCONFIRMED
    # the bubble really is in the live DOM — and still did not count
    assert any(role == "user" and RID in text for role, text in session.dom)
    folder = diagnostics(tmp_path)[-1]
    meta = read_meta(folder)
    assert meta["send_attempted"] is True
    assert meta["retry_prohibited"] is True


def test_optimistic_bubble_disappears_on_reload(tmp_path):
    session, clock = FakeSession(send_mode="optimistic_only"), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)
    client.submit(RID, PROMPT)
    assert client.has_request(RID) is True  # live DOM shows it
    assert client.reconcile(RID) is False  # persisted history does not
    assert client.has_request(RID) is False  # and the bubble is gone


def test_assistant_start_confirms_submission(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    clock.events[1] = lambda: session.dom.append(("assistant", "thinking..."))
    client = make_client(session, clock, tmp_path)
    assert client.submit(RID, PROMPT) is SubmitResult.CONFIRMED


def test_generation_indicator_confirms_submission(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    clock.events[1] = lambda: session.present.add(SEL.stop_button)
    client = make_client(session, clock, tmp_path)
    assert client.submit(RID, PROMPT) is SubmitResult.CONFIRMED


def test_composer_clearing_alone_does_not_confirm(tmp_path):
    """Send clears the composer even when nothing persists — cleared input is
    not evidence."""
    session, clock = FakeSession(send_mode="optimistic_only"), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)
    result = client.submit(RID, PROMPT)
    assert session.composer == ""  # cleared
    assert result is SubmitResult.UNCONFIRMED  # still not confirmed


def test_already_persisted_skips_sending(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed([*OLD_TURN, ("user", f"request {RID} body")])
    client = make_client(session, clock, tmp_path)
    assert client.submit(RID, PROMPT) is SubmitResult.ALREADY_PERSISTED
    assert session.clicks == []
    assert session.inserted == []


# ---- composer input ---------------------------------------------------------


def test_composer_driven_with_focus_clear_insert(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    clock.events[1] = lambda: session.dom.append(("assistant", "ok"))
    client = make_client(session, clock, tmp_path)
    client.submit(RID, PROMPT)
    assert session.focused == [SEL.composer]
    assert session.keys == ["ControlOrMeta+a", "Delete"]
    assert session.inserted == [PROMPT]  # one bulk insert, not per character
    assert session.clicks == [SEL.send_button]


def test_send_button_never_clicked_when_editor_ignores_input(tmp_path):
    """Composer text visible but the editor never enables Send: fail before
    sending, so the outcome is unambiguous and retry stays safe."""
    session, clock = FakeSession(editor_registers_input=False), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path, send_ready_timeout=3.0)
    with pytest.raises(SubmissionError) as excinfo:
        client.submit(RID, PROMPT)
    assert "nothing was sent" in str(excinfo.value)
    assert session.clicks == []
    meta = read_meta(diagnostics(tmp_path)[-1])
    assert meta["tag"] == "send-not-ready"
    assert meta["send_attempted"] is False
    assert meta["retry_prohibited"] is False
    assert meta["composer_chars"] > 0  # text was visible...
    assert meta["send_button_enabled"] is False  # ...but Send stayed dead


def test_input_sync_failure_reported_without_sending(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)

    def swallow(text):  # editor drops the text entirely
        session.inserted.append(text)

    session.insert_text = swallow
    client = make_client(session, clock, tmp_path, input_sync_timeout=3.0)
    with pytest.raises(SubmissionError):
        client.submit(RID, PROMPT)
    assert session.clicks == []
    assert read_meta(diagnostics(tmp_path)[-1])["tag"] == "composer-not-synchronised"


def test_refuses_to_send_while_a_generation_is_running(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.present.add(SEL.stop_button)
    client = make_client(session, clock, tmp_path, send_ready_timeout=3.0)
    with pytest.raises(SubmissionError):
        client.submit(RID, PROMPT)
    assert session.clicks == []


# ---- reconciliation ---------------------------------------------------------


def test_reconcile_finds_persisted_request(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed([*OLD_TURN, ("user", f"request {RID}")])
    session.dom = []  # live DOM out of date; reload must fix it
    client = make_client(session, clock, tmp_path)
    assert client.reconcile(RID) is True
    assert "reload" in session.navigations


def test_reconcile_does_not_find_unpersisted_request(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path, reconcile_timeout=3.0)
    assert client.reconcile(RID) is False


def test_reconcile_waits_for_late_persistence(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)

    def late():
        session.persisted.append(("user", f"request {RID}"))
        session.dom.append(("user", f"request {RID}"))

    clock.events[2] = late
    client = make_client(session, clock, tmp_path)
    assert client.reconcile(RID) is True


# ---- awaiting: no navigation, correct turn ---------------------------------


def test_awaiting_never_navigates_or_reloads(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed([*OLD_TURN, ("user", f"request {RID}")])
    clock.events[1] = lambda: session.dom.append(("assistant", "THE ANSWER"))
    client = make_client(session, clock, tmp_path)
    assert client.await_response(RID) == "THE ANSWER"
    assert session.navigations == []


def test_streaming_answer_survives_repeated_polls(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed([*OLD_TURN, ("user", f"request {RID}")])
    session.present.add(SEL.stop_button)  # generating

    def grow(text, done=False):
        def _do():
            if session.dom and session.dom[-1][0] == "assistant":
                session.dom[-1] = ("assistant", text)
            else:
                session.dom.append(("assistant", text))
            if done:
                session.present.discard(SEL.stop_button)

        return _do

    clock.events[1] = grow("par")
    clock.events[2] = grow("partial")
    clock.events[3] = grow("partial then full", done=True)
    client = make_client(session, clock, tmp_path)
    assert client.await_response(RID) == "partial then full"
    assert session.navigations == []


def test_old_assistant_message_cannot_satisfy_the_request(tmp_path):
    """The stale `Understood.` sits BEFORE our request and must never be
    accepted as its answer."""
    session, clock = FakeSession(), FakeClock()
    session.seed([*OLD_TURN, ("user", f"request {RID}")])
    client = make_client(session, clock, tmp_path, response_start_timeout=5.0)
    with pytest.raises(ResponseTimeoutError) as excinfo:
        client.await_response(RID)
    assert "began" in str(excinfo.value)  # start bound, not completion bound
    assert read_meta(diagnostics(tmp_path)[-1])["tag"] == "response-not-started"


def test_response_start_bound_is_separate_from_completion_bound(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed([*OLD_TURN, ("user", f"request {RID}")])
    session.present.add(SEL.stop_button)  # starts, never finishes
    clock.events[1] = lambda: session.dom.append(("assistant", "forever"))
    client = make_client(
        session, clock, tmp_path, response_start_timeout=5.0, response_timeout=8.0
    )
    with pytest.raises(ResponseTimeoutError) as excinfo:
        client.await_response(RID)
    assert "did not complete" in str(excinfo.value)
    assert read_meta(diagnostics(tmp_path)[-1])["tag"] == "response-timeout"


def test_awaiting_detects_page_drift(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed([*OLD_TURN, ("user", f"request {RID}")])

    def drift():
        session.current_url = "https://chatgpt.com/c/another-chat"

    clock.events[1] = drift
    client = make_client(session, clock, tmp_path)
    with pytest.raises(BrowserError):
        client.await_response(RID)
    assert read_meta(diagnostics(tmp_path)[-1])["tag"] == "page-drifted"


def test_awaiting_detects_logout_mid_wait(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed([*OLD_TURN, ("user", f"request {RID}")])
    clock.events[1] = lambda: setattr(
        session, "current_url", "https://chatgpt.com/auth/login"
    )
    client = make_client(session, clock, tmp_path)
    with pytest.raises(LoginExpiredError):
        client.await_response(RID)


# ---- diagnostics ------------------------------------------------------------


def test_diagnostics_are_structured_and_secret_free(tmp_path):
    session, clock = FakeSession(send_mode="optimistic_only"), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)
    client.submit(RID, PROMPT)
    folder = diagnostics(tmp_path)[-1]
    meta = read_meta(folder)
    # every field the operator needs to diagnose without guessing
    for field in (
        "request_id",
        "stage",
        "configured_url",
        "actual_url",
        "composer_present",
        "composer_chars",
        "matching_user_messages",
        "assistant_after_request",
        "send_attempted",
        "reconciled",
        "retry_prohibited",
    ):
        assert field in meta, field
    assert meta["request_id"] == RID
    # and nothing that could carry credentials
    blob = (folder / "meta.json").read_text().lower()
    for forbidden in ("cookie", "authorization", "bearer", "session_token", "localstorage"):
        assert forbidden not in blob, forbidden


def test_session_protocol_cannot_read_credentials():
    from autoloop.browser import session as session_module
    from autoloop.browser.playwright_session import PlaywrightSession

    names = set(dir(session_module.BrowserSession)) | set(dir(PlaywrightSession))
    for banned in ("cookies", "get_cookies", "storage_state", "local_storage"):
        assert banned not in names, banned


def test_diagnostics_never_raise_without_a_directory(tmp_path):
    session, clock = FakeSession(), FakeClock()
    client = make_client(session, clock, tmp_path, diagnostics_dir=None)
    assert client.save_diagnostics("no-dir", request_id=RID, stage="x") is None


# ---- send_attempted is the transport's honest report ------------------------


def test_send_attempted_is_false_before_any_click(tmp_path):
    session, clock = FakeSession(editor_registers_input=False), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path, send_ready_timeout=3.0)
    with pytest.raises(SubmissionError):
        client.submit(RID, PROMPT)
    # The orchestrator relies on this to know a resend is safe.
    assert client.send_attempted is False


def test_send_attempted_is_true_once_send_is_clicked(tmp_path):
    session, clock = FakeSession(send_mode="optimistic_only"), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)
    assert client.submit(RID, PROMPT) is SubmitResult.UNCONFIRMED
    assert client.send_attempted is True


def test_send_attempted_survives_an_exception_after_the_click(tmp_path):
    """Login expiry during confirmation: the click already happened, and the
    transport must still report it so recovery reconciles instead of resending."""
    session, clock = FakeSession(send_mode="optimistic_only"), FakeClock()
    session.seed(OLD_TURN)

    def log_out():
        session.current_url = "https://chatgpt.com/auth/login"

    clock.events[1] = log_out
    client = make_client(session, clock, tmp_path)
    with pytest.raises(LoginExpiredError):
        client.submit(RID, PROMPT)
    assert client.send_attempted is True


def test_reconcile_performs_exactly_one_page_load(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed([*OLD_TURN, ("user", f"request {RID}")])
    client = make_client(session, clock, tmp_path)
    assert client.reconcile(RID) is True
    assert session.navigations == ["reload"]  # already here: reload, no goto


def test_reconcile_navigates_once_when_off_conversation(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed([*OLD_TURN, ("user", f"request {RID}")])
    session.current_url = "https://chatgpt.com/c/elsewhere"
    client = make_client(session, clock, tmp_path)
    assert client.reconcile(RID) is True
    assert session.navigations == [f"goto:{CONV_URL}"]
