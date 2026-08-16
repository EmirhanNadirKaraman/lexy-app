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
from urllib.parse import urlsplit

import pytest

from autoloop.browser.chatgpt import BrowserChatGPT, SubmitResult
from autoloop.browser.selectors import ChatGPTSelectors
from autoloop.errors import (
    AutoloopError,
    BrowserError,
    ConversationSearchInconclusive,
    ConversationUnusableError,
    LoginExpiredError,
    RateLimitedError,
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
        #: Files handed to set_input_files, and whether the upload "takes".
        #: accepts_uploads=False models the silent no-op the chip guard exists
        #: to catch: the call returns, and nothing ever attaches.
        self.uploaded: list[tuple[str, str]] = []
        self.accepts_uploads = True
        self.seen_uploads: set[str] = set()
        #: filename revealed once the duplicate modal is dismissed
        self.duplicate_reveals_tile: str | None = None
        #: polls the upload takes to finish before Send enables
        self.upload_completes_after = 1
        self.upload_pending = 0
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

    def throttle(self, with_dismiss_button=True):
        """Model ChatGPT's ACCOUNT rate limit exactly as it presents.

        The overlay appears and intercepts pointer events. Nothing else
        changes: the composer is still present, still enabled, the message
        list is intact, and the page's `inner_text` contains none of the
        modal's prose. Every one of those was checked live on 2026-08-15 and
        every one reported healthy against a firmly throttled account — which
        is why this fake keeps them healthy too. A test that passes against a
        fake which ALSO removes the composer proves nothing about the fault.
        """
        self.present.add(SEL.rate_limit_modal)
        if with_dismiss_button:
            self.present.add(SEL.rate_limit_dismiss[0])

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
            enabled = self.send_enabled
            self._tick_upload()
            return enabled
        return selector in self.present

    def click(self, selector):
        self.clicks.append(selector)
        if selector in SEL.rate_limit_dismiss:
            self.present.discard(SEL.rate_limit_modal)
            self.present.discard(SEL.rate_limit_dismiss[0])
            return
        if selector == SEL.duplicate_file_dismiss:
            self.present.discard(SEL.duplicate_file_modal)
            if self.duplicate_reveals_tile:
                self.present.add(
                    SEL.attachment_chip_for.format(filename=self.duplicate_reveals_tile)
                )
                self.send_enabled = True  # an already-uploaded file is complete
            return
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
        if SEL.rate_limit_modal in self.present:
            # What Playwright actually reports while the overlay is up: the
            # element is there, so the locator resolves, and the click never
            # lands because something else is on top of it. Verbatim shape of
            # the message the loop logged all night on 2026-08-14/15.
            raise BrowserError(
                "Locator.click: Timeout 30000ms exceeded. "
                'waiting for locator("#prompt-textarea")'
            )
        self.focused.append(selector)

    def press(self, keys):
        self.keys.append(keys)
        if keys == "Delete":
            self.composer = ""
            self.send_enabled = False

    def set_input_files(self, selector, path):
        import os

        self.uploaded.append((selector, path))
        name = os.path.basename(path)
        if not self.accepts_uploads:
            return
        if name in self.seen_uploads:
            # ChatGPT refuses a file it has already seen with a modal instead
            # of attaching it a second time.
            self.present.add(SEL.duplicate_file_modal)
            return
        self.seen_uploads.add(name)
        # The tile is a PROMISE: it renders immediately, while the upload is
        # still in flight. Send stays disabled until upload_completes_after
        # further polls — measured at ~6s against ~0.5s for the tile.
        self.present.add(SEL.attachment_chip_for.format(filename=name))
        self.upload_pending = self.upload_completes_after

    def _tick_upload(self):
        if self.upload_pending > 0:
            self.upload_pending -= 1
            if self.upload_pending == 0:
                self.send_enabled = True

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


# ---- send observation: the transport can now DISPROVE acceptance ------------
#
# `FakeSession` deliberately does NOT implement the observation capability, so
# every test above exercises the no-capability path and must keep behaving
# exactly as it did before this feature existed. `ObservingSession` adds it.


class ObservingSession(FakeSession):
    """FakeSession plus the optional send-observation capability.

    `scripted` is the list of observations the "network" reports for the next
    send; `windows` records each time a window was opened, so a test can prove
    an observation is never attributed to an earlier turn.
    """

    def __init__(self, *args, scripted=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.scripted = list(scripted)
        self.windows = 0
        self._pending: list = []

    def start_send_observation(self):
        self.windows += 1
        self._pending = []

    def click(self, selector):
        super().click(selector)
        if selector == SEL.send_button:
            self._pending = list(self.scripted)

    def take_send_observations(self):
        out, self._pending = self._pending, []
        return out


def observation(status=None, failure=""):
    from autoloop.browser.observation import SendObservation

    return SendObservation(path="/backend-api/conversation", status=status, failure=failure)


def test_failed_send_request_is_reported_as_rejected_not_unconfirmed(tmp_path):
    """The whole point: a send the browser itself reports as failed is
    DISPROVEN, so the orchestrator may confirm and resend rather than park."""
    session = ObservingSession(send_mode="optimistic_only", scripted=[observation(status=500)])
    clock = FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)
    assert client.submit(RID, PROMPT) is SubmitResult.REJECTED
    assert client.send_attempted is True
    assert session.windows == 1  # the window opened for exactly this send


def test_a_request_that_never_completed_is_rejected(tmp_path):
    session = ObservingSession(
        send_mode="optimistic_only",
        scripted=[observation(status=None, failure="net::ERR_INTERNET_DISCONNECTED")],
    )
    clock = FakeClock()
    session.seed(OLD_TURN)
    assert make_client(session, clock, tmp_path).submit(RID, PROMPT) is SubmitResult.REJECTED


def test_a_mixed_window_stays_unconfirmed(tmp_path):
    """A failure followed by a success is precisely where a resend could
    double-post, so it must not resolve to either verdict."""
    session = ObservingSession(
        send_mode="optimistic_only",
        scripted=[observation(status=500), observation(status=200)],
    )
    clock = FakeClock()
    session.seed(OLD_TURN)
    assert make_client(session, clock, tmp_path).submit(RID, PROMPT) is SubmitResult.UNCONFIRMED


def test_a_successful_status_alone_does_not_confirm(tmp_path):
    """200 is not persistence. Without an assistant turn for our request the
    result stays UNCONFIRMED — the history check is load-bearing."""
    session = ObservingSession(send_mode="optimistic_only", scripted=[observation(status=200)])
    clock = FakeClock()
    session.seed(OLD_TURN)
    assert make_client(session, clock, tmp_path).submit(RID, PROMPT) is SubmitResult.UNCONFIRMED


def test_history_outranks_a_rejecting_status(tmp_path):
    """If the turn is demonstrably live, a discouraging status code loses."""
    session = ObservingSession(send_mode="persist", scripted=[observation(status=500)])
    clock = FakeClock()
    session.seed(OLD_TURN)
    clock.events[1] = lambda: session.dom.append(("assistant", "answering now"))
    client = make_client(session, clock, tmp_path)
    assert client.submit(RID, PROMPT) is SubmitResult.CONFIRMED


def test_a_session_without_the_capability_behaves_exactly_as_before(tmp_path):
    session, clock = FakeSession(send_mode="optimistic_only"), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)
    assert client.submit(RID, PROMPT) is SubmitResult.UNCONFIRMED
    assert client.send_outcome.value == "unknown"


def test_rejection_diagnostics_carry_the_observations_and_no_secrets(tmp_path):
    session = ObservingSession(send_mode="optimistic_only", scripted=[observation(status=403)])
    clock = FakeClock()
    session.seed(OLD_TURN)
    make_client(session, clock, tmp_path).submit(RID, PROMPT)
    folders = diagnostics(tmp_path)
    assert folders
    meta = read_meta(folders[-1])
    assert meta["send_outcome"] == "rejected"
    assert meta["send_observations"] == [
        {"path": "/backend-api/conversation", "status": 403, "failure": ""}
    ]
    blob = (folders[-1] / "meta.json").read_text().lower()
    for forbidden in ("cookie", "authorization", "bearer"):
        assert forbidden not in blob


# ---- conversation-unusable: the narrow rotation trigger ---------------------


def test_a_loaded_conversation_without_a_composer_is_unusable(tmp_path):
    """On the conversation URL, logged in, no composer: THIS chat is wedged."""
    session, clock = FakeSession(), FakeClock()
    session.present = set()  # loaded, but no composer ever appears
    with pytest.raises(ConversationUnusableError):
        make_client(session, clock, tmp_path, composer_timeout=3.0).attach()


def test_a_page_that_never_reached_the_conversation_is_only_a_browser_error(tmp_path):
    """A page stuck elsewhere is a transport fault on the ordinary failure
    budget — rotating for it would spend the run's one rotation on a blip."""
    session, clock = FakeSession(), FakeClock()
    session.present = set()
    session.current_url = "https://chatgpt.com/c/somewhere-else"

    def stay_put(url):
        session.navigations.append(f"goto:{url}")  # navigation does not take

    session.goto = stay_put
    with pytest.raises(BrowserError) as exc:
        make_client(session, clock, tmp_path, composer_timeout=3.0).attach()
    assert not isinstance(exc.value, ConversationUnusableError)


def test_an_explicit_unavailable_marker_is_unusable(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.present = {SEL.conversation_error_markers[0]}
    with pytest.raises(ConversationUnusableError):
        make_client(session, clock, tmp_path).attach()


def test_retarget_moves_every_page_identity_check(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)
    other = "https://chatgpt.com/g/g-p-proj/c/replacement"
    client.retarget(other)
    assert client.conversation_url == other
    client.attach()  # now considers the old URL "elsewhere" and navigates
    assert session.navigations == [f"goto:{other}"]


# ---------------------------------------------------------------------------
# Attachment delivery (2026-08-15)
#
# The composer cannot be verified to hold a large patch: `_enter_prompt` reads
# the editor back, and a 30,000-character part never returns its own tail, so
# the client refuses to send — correctly, and permanently. Uploading the diff
# sidesteps the editor. What must not be lost in the move is the proof that the
# file arrived: a review packet whose diff never landed would be approved
# unseen, which is strictly worse than a send that fails loudly.
# ---------------------------------------------------------------------------


def test_an_attachment_is_uploaded_before_the_prompt_is_typed(tmp_path):
    """Order matters: attaching after the text would leave a window where the
    send control is live with the prompt but without the diff."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    # Waiting for the upload to COMPLETE costs polls before the send, so the
    # scripted reply lands later than on the plain typed path.
    clock.events[3] = lambda: session.dom.append(("assistant", "ok"))
    client = make_client(session, clock, tmp_path)

    assert client.submit(RID, PROMPT, attachment="/tmp/diff.md") is SubmitResult.CONFIRMED
    assert session.uploaded == [(SEL.file_input, "/tmp/diff.md")]
    assert SEL.attachment_chip_for.format(filename="diff.md") in session.present


def test_no_attachment_leaves_the_typed_path_untouched(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    clock.events[1] = lambda: session.dom.append(("assistant", "ok"))
    client = make_client(session, clock, tmp_path)

    assert client.submit(RID, PROMPT) is SubmitResult.CONFIRMED
    assert session.uploaded == []


def test_an_upload_that_silently_does_nothing_refuses_to_send(tmp_path):
    """The failure the chip guard exists for: set_input_files returns, no
    attachment ever appears, and without the check the loop would ask for a
    verdict on a diff the reviewer never received."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.accepts_uploads = False
    client = make_client(session, clock, tmp_path)
    before = len(session.persisted)

    with pytest.raises(SubmissionError) as excinfo:
        client.submit(RID, PROMPT, attachment="/tmp/diff.md")
    message = str(excinfo.value)
    assert "attachment" in message and "nothing was sent" in message
    # Seeded history is still there; what matters is that NO NEW turn was sent.
    assert len(session.persisted) == before
    assert session.uploaded == [(SEL.file_input, "/tmp/diff.md")]


def test_a_file_already_on_the_composer_is_not_uploaded_again(tmp_path):
    """Idempotence, and it is not an optimisation. A retry after a failed send
    finds its own file still attached, and re-uploading it raises ChatGPT's
    duplicate-file modal, which covers the composer and blocks everything."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.present.add(SEL.attachment_chip_for.format(filename="diff.md"))
    session.send_enabled = True  # an already-attached file has finished uploading
    clock.events[1] = lambda: session.dom.append(("assistant", "ok"))
    client = make_client(session, clock, tmp_path)

    assert client.submit(RID, PROMPT, attachment="/tmp/diff.md") is SubmitResult.CONFIRMED
    assert session.uploaded == [], "the file was already there; nothing to upload"


def test_the_duplicate_file_modal_refuses_rather_than_sending(tmp_path):
    """ChatGPT answers a repeat upload with a modal instead of an attachment.
    The modal covers the composer, so proceeding would type into a blocked
    page — and sending would ask for a verdict on a diff that is not attached."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.seen_uploads.add("diff.md")  # ChatGPT has seen this file before
    client = make_client(session, clock, tmp_path)
    before = len(session.persisted)

    with pytest.raises(SubmissionError) as excinfo:
        client.submit(RID, PROMPT, attachment="/tmp/diff.md")
    assert "duplicate-file" in str(excinfo.value)
    assert "nothing was sent" in str(excinfo.value)
    assert len(session.persisted) == before
    # Dismissed, not merely detected: the modal covers the composer, so
    # leaving it up would block every later attempt too.
    assert SEL.duplicate_file_dismiss in session.clicks
    assert SEL.duplicate_file_modal not in session.present


def test_the_tile_must_name_THIS_file_not_merely_any_attachment(tmp_path):
    """A previous attempt's file still on the composer must not read as
    success: the review request would carry a diff belonging to another
    change — the substitution report_sha256 exists to prevent."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.present.add(SEL.attachment_chip_for.format(filename="someone-elses.md"))
    session.accepts_uploads = False  # our own upload silently does nothing
    client = make_client(session, clock, tmp_path)

    with pytest.raises(SubmissionError) as excinfo:
        client.submit(RID, PROMPT, attachment="/tmp/diff.md")
    assert "diff.md" in str(excinfo.value)


def test_a_duplicate_that_turns_out_to_be_attached_proceeds(tmp_path):
    """Dismissing the modal can reveal the file already on the composer — that
    is a success, not a failure, and re-uploading would only raise it again."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.seen_uploads.add("diff.md")
    session.duplicate_reveals_tile = "diff.md"
    # Dismissing the modal costs an extra poll, so the reply is scheduled a
    # tick later than in the tests that take the plain path.
    clock.events[2] = lambda: session.dom.append(("assistant", "ok"))
    client = make_client(session, clock, tmp_path)

    assert client.submit(RID, PROMPT, attachment="/tmp/diff.md") is SubmitResult.CONFIRMED
    assert SEL.duplicate_file_dismiss in session.clicks


def test_the_send_waits_for_the_upload_to_finish_not_merely_to_appear(tmp_path):
    """The bug that lost request alr-75bdba23-0002. The tile is a PROMISE:
    measured live, it rendered after 0.5s while Send stayed disabled until
    6.3s. Sending in that window was accepted, consumed the attachment, and
    persisted no turn at all — surfacing as submission_ambiguous with the
    review request simply absent from the conversation."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.upload_completes_after = 3  # slow upload: tile early, Send late
    clock.events[5] = lambda: session.dom.append(("assistant", "ok"))
    client = make_client(session, clock, tmp_path)

    assert client.submit(RID, PROMPT, attachment="/tmp/diff.md") is SubmitResult.CONFIRMED
    # Nothing was typed before the upload finished: the composer is cleared
    # first precisely so an enabled Send can only mean the upload is done.
    assert session.uploaded == [(SEL.file_input, "/tmp/diff.md")]


def test_an_upload_that_never_finishes_refuses_to_send(tmp_path):
    """A tile that appears and an upload that never completes must not send.
    The attachment would be consumed and the turn lost."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.upload_completes_after = 10_000  # never, within the timeout
    client = make_client(session, clock, tmp_path)
    before = len(session.persisted)

    with pytest.raises(SubmissionError) as excinfo:
        client.submit(RID, PROMPT, attachment="/tmp/diff.md")
    assert "did not finish uploading" in str(excinfo.value)
    assert "nothing was sent" in str(excinfo.value)
    assert len(session.persisted) == before


# ---------------------------------------------------------------------------
# Finding a conversation by the request it contains (2026-08-05)
#
# `find_conversation_with` identifies a rotated chat by CONTENT when the
# address bar has not caught up. Two things kept it from being trustworthy
# enough to decide anything with:
#
#   * ChatGPT's message list is VIRTUALIZED. alr-af11e1b3-0006 parked as
#     `submission_ambiguous` while the chat held the request and its answer;
#     seeing them by hand took pressing End and scrolling six times before the
#     tail rendered. A search that reads only the painted window reports "not
#     in persisted history" about a message that is merely unmounted.
#   * It concluded from whatever page it was on. A rotation mid-flight moves
#     that shared page, and a confident answer about the wrong chat is worse in
#     BOTH directions than the park it would replace: it can name a chat that
#     never saw the request, or report absent while looking away from the one
#     that has it.
#
# The fake below is purpose-built rather than an extension of `FakeSession`:
# that one keeps a single flat `persisted` list and an `elements` that ignores
# its selector, so it can model neither a chat list nor per-conversation
# history — and ~40 tests depend on its current shape.
# ---------------------------------------------------------------------------

PROJECT_URL = "https://chatgpt.com/g/g-p-project123/project"
CHAT_ONE = "https://chatgpt.com/g/g-p-project123/c/chat-one"
CHAT_TWO = "https://chatgpt.com/g/g-p-project123/c/chat-two"
STRAY_CHAT = "https://chatgpt.com/g/g-p-project123/c/somewhere-else"


def _turns(count, marker="earlier"):
    """`count` complete user/assistant exchanges — i.e. 2×count messages."""
    out = []
    for i in range(count):
        out.append(("user", f"{marker} question {i}"))
        out.append(("assistant", f"{marker} answer {i}"))
    return out


def _chat_holding(request_id, before=6):
    """A conversation whose LAST message carries the request.

    Where a rotation puts it, and exactly where virtualization leaves it
    unpainted: 13 messages against a 2-message initial window needs six
    scrolls to reach — the number the human needed on 2026-08-05.
    """
    return _turns(before) + [("user", f"[autoloop request {request_id} | iteration 1]")]


class FakeProjectSession:
    """A project chat list plus per-conversation history behind a virtualized
    message list. Keyboard-only: it does NOT implement the optional
    `scroll_to_end` capability, so the client falls back to pressing End.

    A fresh load paints only `window` messages, counted from the TOP — the tail
    is unmounted, which is the 2026-08-05 failure. Each gesture paints `step`
    more, and nothing else does: a search that skips the mount can only ever
    see the opening turns.
    """

    def __init__(self, chats, *, links=None, window=2, step=2, url_suffix="", growing=False):
        self.chats = {url: list(history) for url, history in chats.items()}
        # Real hrefs are relative, so the client's urljoin is exercised too.
        self.links = list(links) if links is not None else [
            urlsplit(url).path for url in self.chats
        ]
        self.window = window
        self.step = step
        #: Appended to every URL the page lands on, as ChatGPT appends
        #: `?model=…` of its own accord.
        self.url_suffix = url_suffix
        #: A conversation that keeps producing messages, so the end of the list
        #: moves away as fast as the gesture reaches it and absence is never
        #: established.
        self.growing = growing
        self.current_url = PROJECT_URL
        self.present = {SEL.composer}
        self.mounted = window
        self.scrolls = 0
        #: How many nodes each read of the message list returned. The
        #: sliding-window fake below keeps this CONSTANT while painting
        #: different messages, which is why a node count cannot prove the
        #: mount reached the tail.
        self.window_sizes = []
        self.navigations = []
        self.keys = []
        self.logged_out = False
        #: Where a `goto(PROJECT_URL)` actually lands (a slug rewrite, or an
        #: SPA that restored some other page). None = where it was asked to.
        self.project_lands_on = None
        #: ChatGPT's canonical rewrite of a conversation URL: the project
        #: segment grows the project's name. Same chat, different path.
        self.slug = None
        #: Rotation mid-flight: after this many gestures the shared page is on
        #: `drift_to`, fully painted.
        self.drift_after_scrolls = None
        self.drift_to = None

    # -- BrowserSession protocol -------------------------------------------
    def goto(self, url):
        self.navigations.append(f"goto:{url}")
        if self.logged_out:
            self.current_url = "https://auth.openai.com/authorize"
            self.present = {SEL.login_markers[0]}
            return
        if url == PROJECT_URL and self.project_lands_on:
            self.current_url = self.project_lands_on
        else:
            self.current_url = self._slugged(url) + self.url_suffix
        self._remount()  # a fresh load re-virtualizes the list

    def reload(self):
        self.navigations.append("reload")
        self._remount()

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
        self.keys.append(keys)
        if keys == "End":
            self._paint_more()

    def insert_text(self, text):
        pass

    def inner_text(self, selector):
        return ""

    def elements(self, selector, attr):
        if selector == SEL.conversation_link:
            if not self._on_project_page():
                return []  # only a project page lists that project's chats
            return [(href, f"chat titled {i}") for i, href in enumerate(self.links)]
        if selector == SEL.message:
            window = list(self._window(self._history()))
            self.window_sizes.append(len(window))
            return window
        return []

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG-fake")

    def html(self):
        return "<html>fake</html>"

    def close(self):
        pass

    # -- virtualization ----------------------------------------------------
    @staticmethod
    def _same(a, b):
        pa, pb = urlsplit(a or ""), urlsplit(b or "")
        return (pa.netloc, pa.path.rstrip("/")) == (pb.netloc, pb.path.rstrip("/"))

    @staticmethod
    def _chat_id(url):
        segs = [seg for seg in urlsplit(url or "").path.split("/") if seg]
        return segs[-1] if len(segs) >= 2 and segs[-2] == "c" else None

    def _slugged(self, url):
        """`/g/g-p-<id>/c/<chat>` -> `/g/g-p-<id><slug>/c/<chat>`: the same
        conversation under the path ChatGPT canonicalises it to."""
        if not self.slug or self._chat_id(url) is None:
            return url
        parts = urlsplit(url)
        segs = parts.path.split("/")
        segs[2] += self.slug
        return f"{parts.scheme}://{parts.netloc}{'/'.join(segs)}"

    def _on_project_page(self):
        """Any project landing page, slug and query included — ChatGPT rewrites
        `/g/g-p-<id>/project` with the project's name, and the chat list is on
        the rewritten page just the same."""
        return urlsplit(self.current_url).path.rstrip("/").endswith("/project")

    def _history(self):
        for url, history in self.chats.items():
            same_chat = (
                self._chat_id(url) is not None
                and self._chat_id(url) == self._chat_id(self.current_url)
            )
            if self._same(url, self.current_url) or same_chat:
                return history
        return []

    def _remount(self):
        """What a fresh page load does to the virtualized list."""
        self.mounted = self.window

    def _window(self, history):
        """The slice of `history` currently in the DOM. Counted from the TOP,
        so this fake's tail is what stays unpainted — the 2026-08-05 shape."""
        return history[: self.mounted]

    def _advance(self):
        """What one "go to the end" gesture mounts."""
        self.mounted += self.step

    def _at_list_end(self):
        """Is the view at the END of the list — the position a real
        `scroll_to_end` reads off the scroll container?

        Honest about this fake's own geometry rather than about whether a
        gesture happened: the whole point of the signal is that it comes from
        the container, not from the gesture claiming success."""
        return self.mounted >= len(self._history())

    def _paint_all(self):
        """Every message at once, for a chat we did not scroll ourselves."""
        self.mounted = 10_000

    def _paint_more(self):
        self.scrolls += 1
        self._advance()
        if self.growing:
            self._history().extend(_turns(self.step, marker=f"live {self.scrolls}"))
        if (
            self.drift_after_scrolls is not None
            and self.scrolls >= self.drift_after_scrolls
            and self.drift_to
        ):
            self.current_url = self.drift_to
            self._paint_all()  # the chat we drifted onto is fully painted
        return self._at_list_end()


class FakeScrollingSession(FakeProjectSession):
    """The same page, with the optional `scroll_to_end` capability a real
    `PlaywrightSession` provides — gesture AND position, because reporting
    where the view got to is the half that lets absence be established at
    all."""

    def scroll_to_end(self, selector):
        assert selector == SEL.message  # the list being mounted, not the composer
        return self._paint_more()


class FakeSlidingWindowSession(FakeScrollingSession):
    """A virtualizer that keeps a CONSTANT-SIZE mounted window: every gesture
    mounts newer nodes and DROPS the older ones it replaces, so
    `len(messages())` never moves however far the list travels.

    The shape the node count cannot see, and the reason convergence is judged
    on content. Against a count-based proof this fake settles after two
    gestures — the count read 6 before and after, so "it stopped growing" —
    and reports a request that six more gestures would have painted as absent
    from persisted history. That is the 2026-08-05 park, reintroduced by the
    fix for it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: Index of the first mounted node. `window` nodes are mounted from
        #: here; everything before and after is out of the DOM entirely.
        self.offset = 0

    def _remount(self):
        super()._remount()
        self.offset = 0

    def _window(self, history):
        # Clamped at the end: a gesture cannot scroll past the last message,
        # so the window comes to rest on the tail rather than off it.
        start = self._start(history)
        end = start + self.window
        return history[start:end]

    def _start(self, history):
        return max(0, min(self.offset, len(history) - self.window))

    def _at_list_end(self):
        # The container is at its end once the mounted window reaches the last
        # message — the node count is the same 6 either way, which is exactly
        # why the count is not the signal.
        history = self._history()
        return self._start(history) + self.window >= len(history)

    def _advance(self):
        self.offset += self.step

    def _paint_all(self):
        self.window = 10_000
        self.offset = 0


class FakeStuckGestureSession(FakeScrollingSession):
    """A gesture that MOUNTS NOTHING, honestly reported.

    The real shape of this is an End press that went to whatever held focus
    instead of the scroller, or a scroll container the gesture never found. The
    view does not move, so the mounted window is byte-identical read after
    read — indistinguishable, from content alone, from a list that has finished
    painting. `position` is what the session says about where it got to:
    False (there is more below) or None (it cannot tell). Neither is a licence
    to call anything absent.
    """

    def __init__(self, *args, position=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.position = position

    def scroll_to_end(self, selector):
        assert selector == SEL.message
        self.scrolls += 1
        return self.position


def test_a_request_in_the_already_mounted_window_is_found(tmp_path):
    """The cheap case stays cheap: a request already painted needs no scrolling
    at all, and mounting more could only ever confirm it."""
    session = FakeScrollingSession({CHAT_ONE: [("user", f"[autoloop request {RID}]")]})
    client = make_client(session, FakeClock(), tmp_path)

    assert client.find_conversation_with(RID, PROJECT_URL) == CHAT_ONE
    assert session.scrolls == 0


def test_a_request_only_in_the_unmounted_tail_is_also_found(tmp_path):
    """THE defect, reproduced: the request is in persisted history and simply
    not painted. Skipping the mount step fails this test — which is exactly how
    alr-af11e1b3-0006 was parked as ambiguous while its answer sat in the
    chat."""
    session = FakeScrollingSession({CHAT_ONE: _turns(2), CHAT_TWO: _chat_holding(RID)})
    client = make_client(session, FakeClock(), tmp_path)

    # The window a plain readback sees on a fresh load does NOT contain it.
    session.goto(CHAT_TWO)
    assert client.has_request(RID) is False

    assert client.find_conversation_with(RID, PROJECT_URL) == CHAT_TWO
    assert session.scrolls >= 6  # the tail was six scrolls down, as observed


def test_a_sliding_constant_size_window_is_not_settled_by_a_steady_node_count(tmp_path):
    """The node count is not a convergence proof, and this is the shape that
    shows it: the virtualizer mounts newer nodes as it drops older ones, so the
    count reads 6 at every single read while six different messages go past.

    A mount that settles on "the count stopped growing" concludes after two
    gestures, reads an intermediate window, and reports the request absent —
    the 2026-08-05 park, produced by the code written to prevent it. Judging
    convergence on the window's CONTENT is what keeps the gesture going until
    the request is painted.
    """
    session = FakeSlidingWindowSession({CHAT_ONE: _chat_holding(RID)}, window=6, step=2)
    client = make_client(session, FakeClock(), tmp_path)

    assert client.find_conversation_with(RID, PROJECT_URL) == CHAT_ONE
    # Four gestures to walk a 13-message chat 6 nodes at a time — well past the
    # two a count-based proof would have stopped at.
    assert session.scrolls >= 4
    # ... and the count that proof watches never moved once.
    assert set(session.window_sizes) == {6}


def test_a_request_the_window_slid_past_is_still_found(tmp_path):
    """Evidence accumulates across windows; it is not read off the last one.

    Once the assistant has answered (the 2026-08-05 chat held the request AND
    its reply), the request is no longer the final message — so a wide enough
    slide carries it into the mounted window and out again. Mounting to the end
    and THEN calling `has_request` is a false absence in exactly that case,
    which is why the verdict comes from what the mount saw.
    """
    history = _turns(3, marker="before") + [
        ("user", f"[autoloop request {RID} | iteration 1]"),
        ("assistant", "decision: push"),
    ] + _turns(4, marker="after")
    session = FakeSlidingWindowSession({CHAT_ONE: history}, window=4, step=4)
    client = make_client(session, FakeClock(), tmp_path)

    # Fully mounted, the tail no longer shows the request at all.
    session.goto(CHAT_ONE)
    for _ in range(10):
        session.scroll_to_end(SEL.message)
    assert client.has_request(RID) is False

    assert client.find_conversation_with(RID, PROJECT_URL) == CHAT_ONE


def test_a_sliding_window_that_comes_to_rest_still_reports_absent(tmp_path):
    """The content proof must still be able to say no. A sliding window stops
    changing once it reaches the tail, and a request in none of the chats is
    absent — otherwise the fix would trade every false absence for a refusal
    and rotation would never get an answer."""
    session = FakeSlidingWindowSession({CHAT_ONE: _turns(6)}, window=6, step=2)
    client = make_client(session, FakeClock(), tmp_path)

    assert client.find_conversation_with(RID, PROJECT_URL) is None
    assert session.scrolls > 0
    assert set(session.window_sizes) == {6}


def test_a_gesture_that_mounts_nothing_never_settles_into_a_false_absence(tmp_path):
    """An unchanged window is not a proof on its own, and this is the shape
    that shows it: the gesture never moves the view, so the SAME opening window
    comes back read after read while the request sits eleven messages below it.

    Judged on content alone, that is a settled list — two byte-identical reads,
    conclude absent, park a request that was delivered and answered. The
    session reporting it is NOT at the end of the list is the only thing that
    separates "fully mounted" from "the gesture did nothing"."""
    session = FakeStuckGestureSession({CHAT_ONE: _chat_holding(RID)}, position=False)
    client = make_client(session, FakeClock(), tmp_path, tail_mount_attempts=4)

    with pytest.raises(ConversationSearchInconclusive) as excinfo:
        client.find_conversation_with(RID, PROJECT_URL)
    assert "never reached its end" in str(excinfo.value)
    # It really did keep trying — the refusal is about evidence, not effort.
    assert session.scrolls == 4


def test_a_gesture_that_reports_no_position_never_settles_either(tmp_path):
    """The same fault through the other adapter failure: a session that cannot
    measure where it got to. `None` is not a quiet `True` — an adapter written
    before the signal existed, or one whose measurement failed, keeps every
    sighting it makes and loses only the ability to rule things OUT."""
    session = FakeStuckGestureSession({CHAT_ONE: _chat_holding(RID)}, position=None)
    client = make_client(session, FakeClock(), tmp_path, tail_mount_attempts=4)

    with pytest.raises(ConversationSearchInconclusive) as excinfo:
        client.find_conversation_with(RID, PROJECT_URL)
    assert "cannot report a scroll position" in str(excinfo.value)


def test_a_stuck_gesture_still_reports_a_request_it_can_already_see(tmp_path):
    """Refusing to conclude ABSENCE must not cost a sighting. The window that
    never moves is enough when the request is in it — a positive is direct
    evidence about the conversation and needs no statement about the scroll
    position at all."""
    session = FakeStuckGestureSession(
        {CHAT_ONE: [("user", f"[autoloop request {RID}]")]}, position=None
    )
    client = make_client(session, FakeClock(), tmp_path, tail_mount_attempts=4)

    assert client.find_conversation_with(RID, PROJECT_URL) == CHAT_ONE


def test_the_chat_list_is_read_from_the_href_not_the_title(tmp_path):
    """Regression: `elements` yields (attribute value, inner text), so the href
    comes FIRST. Unpacking it the other way round read each chat's TITLE as its
    href — no title contains "/c/", so every candidate was skipped and the
    search could only return None, whatever the project held."""
    session = FakeScrollingSession({CHAT_ONE: _chat_holding(RID)})
    client = make_client(session, FakeClock(), tmp_path)

    assert client.find_conversation_with(RID, PROJECT_URL) == CHAT_ONE
    assert f"goto:{CHAT_ONE}" in session.navigations


def test_a_genuinely_absent_request_is_reported_absent(tmp_path):
    """The guard must not become "never says no" — a request in none of the
    chats is absent, and rotation depends on being told so."""
    session = FakeScrollingSession({CHAT_ONE: _turns(4), CHAT_TWO: _turns(4)})
    client = make_client(session, FakeClock(), tmp_path)

    assert client.find_conversation_with(RID, PROJECT_URL) is None
    # and only after the lists demonstrably finished painting
    assert session.scrolls > 0


def test_a_search_that_drifts_refuses_to_conclude_present(tmp_path):
    """A rotation mid-flight moves the shared page. Returning the candidate
    here would bind the loop to a chat that never saw the request — the answer
    came off a different one."""
    session = FakeScrollingSession(
        {CHAT_ONE: _turns(4), STRAY_CHAT: _chat_holding(RID)},
        links=[urlsplit(CHAT_ONE).path],  # the stray is not in this project
    )
    session.drift_after_scrolls, session.drift_to = 1, STRAY_CHAT
    client = make_client(session, FakeClock(), tmp_path)

    with pytest.raises(ConversationSearchInconclusive) as excinfo:
        client.find_conversation_with(RID, PROJECT_URL)
    assert "in either direction" in str(excinfo.value)


def test_a_search_that_drifts_refuses_to_conclude_absent(tmp_path):
    """The other direction of the same fault: the chat we drifted onto does not
    hold the request, and reporting absent from it would be a verdict about a
    conversation nobody asked about."""
    session = FakeScrollingSession(
        {CHAT_ONE: _chat_holding(RID), STRAY_CHAT: _turns(4)},
        links=[urlsplit(CHAT_ONE).path],
    )
    session.drift_after_scrolls, session.drift_to = 1, STRAY_CHAT
    client = make_client(session, FakeClock(), tmp_path)

    with pytest.raises(ConversationSearchInconclusive):
        client.find_conversation_with(RID, PROJECT_URL)


def test_a_project_page_that_lands_elsewhere_is_refused_before_reading_a_chat(tmp_path):
    """The chat list of some other page is not this project's, so neither
    finding nor missing the request in it means anything."""
    session = FakeScrollingSession({CHAT_ONE: _chat_holding(RID)})
    session.project_lands_on = STRAY_CHAT
    client = make_client(session, FakeClock(), tmp_path)

    with pytest.raises(ConversationSearchInconclusive) as excinfo:
        client.find_conversation_with(RID, PROJECT_URL)
    assert "chat list" in str(excinfo.value)
    assert session.navigations == [f"goto:{PROJECT_URL}"]  # nothing else opened


def test_a_slugged_project_url_is_not_drift(tmp_path):
    """ChatGPT rewrites `/g/g-p-<id>/project` with the project's name. Refusing
    that would refuse the page it just loaded, every time."""
    session = FakeScrollingSession({CHAT_ONE: _chat_holding(RID)})
    session.project_lands_on = "https://chatgpt.com/g/g-p-project123-my-project/project"
    client = make_client(session, FakeClock(), tmp_path)

    assert client.find_conversation_with(RID, PROJECT_URL) == CHAT_ONE


def test_a_slugged_conversation_url_is_not_drift(tmp_path):
    """The sharper half of the same trap: a conversation is identified by its
    `/c/<id>`, not by the project prefix ChatGPT rewrites in front of it. An
    exact path compare here would make EVERY search inconclusive in production
    while passing every test that types its URLs by hand."""
    session = FakeScrollingSession({CHAT_ONE: _chat_holding(RID)})
    session.slug = "-my-project"
    client = make_client(session, FakeClock(), tmp_path)

    assert client.find_conversation_with(RID, PROJECT_URL) == CHAT_ONE
    assert session.current_url.startswith("https://chatgpt.com/g/g-p-project123-my-project/c/")


def test_a_different_chat_id_is_still_drift(tmp_path):
    """Tolerating the prefix must not tolerate the id — that is the whole
    identity of a conversation."""
    session = FakeScrollingSession(
        {CHAT_ONE: _turns(4), CHAT_TWO: _chat_holding(RID)},
        links=[urlsplit(CHAT_ONE).path],
    )
    session.drift_after_scrolls, session.drift_to = 1, CHAT_TWO
    client = make_client(session, FakeClock(), tmp_path)

    with pytest.raises(ConversationSearchInconclusive):
        client.find_conversation_with(RID, PROJECT_URL)


def test_a_query_string_is_not_drift(tmp_path):
    """`?model=…` appears on its own; an exact URL compare would refuse every
    candidate in production while passing every test that omits it."""
    session = FakeScrollingSession({CHAT_ONE: _chat_holding(RID)}, url_suffix="/?model=gpt-5")
    client = make_client(session, FakeClock(), tmp_path)

    assert client.find_conversation_with(RID, PROJECT_URL) == CHAT_ONE


def test_a_list_still_painting_when_the_bound_runs_out_does_not_report_absent(tmp_path):
    """Unseen and absent are different things in a virtualized list. A chat
    whose messages never stop arriving has not been ruled out — it has run out
    of scrolls."""
    session = FakeScrollingSession({CHAT_ONE: _turns(4)}, growing=True)
    client = make_client(session, FakeClock(), tmp_path, tail_mount_attempts=4)

    with pytest.raises(ConversationSearchInconclusive) as excinfo:
        client.find_conversation_with(RID, PROJECT_URL)
    assert "unseen and absent are not the same thing" in str(excinfo.value)
    # The gestures spent and the reason separate the ways to be unsettled, and
    # they call for different responses: this list keeps growing under the
    # gesture, so its end moves away as fast as the view reaches it — not the
    # same fault as a gesture that never moved, or a session that cannot
    # measure. An operator reading the park needs to know which.
    assert "(4 gestures)" in str(excinfo.value)
    assert "never reached its end" in str(excinfo.value)


class FakeStreamingTailSession(FakeScrollingSession):
    """A chat pinned to the BOTTOM while it is still generating: the view is
    genuinely at the end of the list (ChatGPT follows the answer down) and the
    window changes on every read as tokens land."""

    def scroll_to_end(self, selector):
        assert selector == SEL.message
        self.scrolls += 1
        self._history().append(("assistant", f"token {self.scrolls}"))
        self._paint_all()
        return True


def test_a_chat_still_streaming_at_the_end_of_the_list_does_not_report_absent(tmp_path):
    """Being at the end is not the same as having finished painting it, which
    is why both proofs are required. A streaming answer keeps the view at the
    bottom while the content underneath it changes every read — so the end
    signal alone would settle a list that is still arriving, and conclude
    absence from a window the next token could have completed."""
    session = FakeStreamingTailSession({CHAT_ONE: _turns(2)})
    client = make_client(session, FakeClock(), tmp_path, tail_mount_attempts=4)

    with pytest.raises(ConversationSearchInconclusive) as excinfo:
        client.find_conversation_with(RID, PROJECT_URL)
    assert "still changing at the end of the list" in str(excinfo.value)


def test_a_session_without_the_scroll_capability_presses_end(tmp_path):
    """The gesture is an OPTIONAL capability, probed like every other one: a
    session that cannot scroll programmatically falls back to the key a human
    presses, and still finds the tail."""
    session = FakeProjectSession({CHAT_ONE: _chat_holding(RID)})
    assert not hasattr(session, "scroll_to_end")
    client = make_client(session, FakeClock(), tmp_path)

    assert client.find_conversation_with(RID, PROJECT_URL) == CHAT_ONE
    assert session.keys.count("End") >= 6


def test_a_session_that_cannot_report_a_position_never_reports_absent(tmp_path):
    """The negative twin of the fallback, and the cost that makes the fallback
    honest. The End key presses and paints, but nothing about it says whether
    the view reached the end of the list — and on a real page End goes to
    whatever holds focus, so a gesture that painted nothing looks exactly like
    one that reached the tail.

    So a keyboard-only session keeps its sightings and gives up ABSENCE. The
    request here is in none of these chats, and the honest answer is still "I
    cannot tell", because this adapter cannot tell."""
    session = FakeProjectSession({CHAT_ONE: _turns(4), CHAT_TWO: _turns(4)})
    assert not hasattr(session, "scroll_to_end")
    client = make_client(session, FakeClock(), tmp_path, tail_mount_attempts=4)

    with pytest.raises(ConversationSearchInconclusive) as excinfo:
        client.find_conversation_with(RID, PROJECT_URL)
    assert "cannot report a scroll position" in str(excinfo.value)


def test_a_logged_out_profile_is_routed_not_demoted_to_inconclusive(tmp_path):
    """The auth redirect lands the page somewhere that is plainly not the
    project — and it must still surface as a login expiry, because that is the
    fault the loop routes. Checking page identity before logging in would turn
    "log back in" into "cannot tell"."""
    session = FakeScrollingSession({CHAT_ONE: _chat_holding(RID)})
    session.logged_out = True
    client = make_client(session, FakeClock(), tmp_path)

    with pytest.raises(LoginExpiredError):
        client.find_conversation_with(RID, PROJECT_URL)


def test_the_walk_is_bounded_by_limit(tmp_path):
    """A project accumulates chats and each candidate costs a page load. The
    one we want was just created, so it is at the top or nowhere."""
    chats = {
        f"https://chatgpt.com/g/g-p-project123/c/chat-{i}": _turns(2) for i in range(5)
    }
    session = FakeScrollingSession(chats)
    client = make_client(session, FakeClock(), tmp_path)

    assert client.find_conversation_with(RID, PROJECT_URL, limit=2) is None
    opened = [nav for nav in session.navigations if "/c/" in nav]
    assert len(opened) == 2


def test_an_inconclusive_search_leaves_a_diagnostic(tmp_path):
    """A refusal is a result, so it is evidenced like every other transport
    failure — otherwise the operator sees a rotation fail with nothing to
    read."""
    session = FakeScrollingSession({CHAT_ONE: _turns(4)})
    session.project_lands_on = STRAY_CHAT
    client = make_client(session, FakeClock(), tmp_path)

    with pytest.raises(ConversationSearchInconclusive):
        client.find_conversation_with(RID, PROJECT_URL)
    folder = diagnostics(tmp_path)[-1]
    assert "conversation-search-inconclusive" in folder.name
    assert PROJECT_URL in read_meta(folder)["note"]


# ---------------------------------------------------------------------------
# ChatGPT's ACCOUNT rate limit
# ---------------------------------------------------------------------------
#
# Recorded overnight 2026-08-14/15: from 07:56 the loop reported nothing but
#
#   browser session lost: Locator.click: Timeout 30000ms exceeded.
#     waiting for locator("#prompt-textarea")
#
# and answered each one by restarting Chrome and retrying — which is what
# generates requests too quickly, so it deepened the exact condition it was
# failing on. The words "rate", "limit" and "throttle" appear nowhere in the
# transcript for that period; the only trace was a composer that would not take
# a click. An operator found it by opening the browser and reading the screen.
#
# THE TRAP THESE TESTS EXIST TO PIN: the composer still EXISTS and reports
# visible/enabled while throttled. Presence is not evidence the page is usable.


def test_a_throttled_page_still_has_a_composer_and_is_still_unusable(tmp_path):
    """The whole fault in one assertion pair. Any readiness probe written
    against composer presence alone reports a false all-clear here."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.throttle()
    client = make_client(session, clock, tmp_path)

    assert session.exists(SEL.composer), "the composer is right there"
    with pytest.raises(RateLimitedError):
        client.attach()


def test_the_throttle_is_detected_by_testid_not_by_its_prose(tmp_path):
    """The visible wording moves with copy edits and locale; the testid does
    not. A live search for "Too many requests" in the page text reported
    healthy while the account was firmly limited, so the prose is not even a
    fallback — it is a false negative."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.present.add(SEL.rate_limit_modal)
    client = make_client(session, clock, tmp_path)

    assert "Too many requests" not in session.inner_text(SEL.composer)
    assert client.is_rate_limited() is True


def test_a_throttle_is_not_a_browser_error(tmp_path):
    """The routing depends on this: `BrowserError` is answered by dropping the
    client, restarting Chrome and retrying, and every one of those is another
    request into the window that caused the limit."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.throttle()
    client = make_client(session, clock, tmp_path)

    with pytest.raises(RateLimitedError) as caught:
        client.attach()
    assert not isinstance(caught.value, BrowserError)
    assert isinstance(caught.value, AutoloopError)


def test_a_throttle_snapshot_records_the_modal_beside_the_present_composer(tmp_path):
    """An existing dump shows `composer_present: true`, which is precisely the
    misleading half. The modal flag next to it is what makes the snapshot
    readable by someone who was not there."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.throttle()
    client = make_client(session, clock, tmp_path)

    with pytest.raises(RateLimitedError):
        client.attach()
    meta = read_meta(diagnostics(tmp_path)[-1])
    assert meta["rate_limit_modal_present"] is True
    assert meta["composer_present"] is True, "the trap, recorded in the evidence"


def test_a_throttle_arriving_mid_send_is_named_rather_than_reported_as_lost(tmp_path):
    """The overnight path, end to end. `focus()` fails with the click timeout
    Playwright really produced; without the re-read that becomes a generic
    transport fault and the loop restarts the browser."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)
    client.attach()  # healthy at this point

    session.throttle()
    with pytest.raises(RateLimitedError):
        client.submit(RID, PROMPT)
    assert client.send_attempted is False, "nothing left the browser"


def test_an_ordinary_composer_failure_is_still_an_ordinary_failure(tmp_path):
    """The re-read must not turn every send fault into a throttle: with no
    modal, the original error passes through untouched."""
    session, clock = FakeSession(editor_registers_input=False), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)

    with pytest.raises(SubmissionError):
        client.submit(RID, PROMPT)


def test_a_throttle_during_an_await_is_named_rather_than_timed_out(tmp_path):
    """`await_response` would otherwise sit out its whole start timeout and
    report silence, which reads as a slow model rather than a limited
    account."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)
    clock.events[1] = lambda: session.throttle()

    with pytest.raises(RateLimitedError):
        client.await_response(RID)


def test_dismissing_the_modal_clears_it_and_the_page_works_again(tmp_path):
    """The modal hides the composer even after the server-side limit expires,
    so a stale one must not read as a continuing throttle."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.throttle()
    client = make_client(session, clock, tmp_path)

    assert client.dismiss_rate_limit_modal() is True
    assert client.is_rate_limited() is False
    client.attach()  # no longer raises


def test_dismissal_reports_the_probe_not_the_click(tmp_path):
    """The element carrying the testid is a full-screen overlay, so the
    button may be a sibling rather than a descendant and the candidate
    selectors may match nothing. A click that hit nothing must never read as
    cleared."""
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    session.throttle(with_dismiss_button=False)
    client = make_client(session, clock, tmp_path)

    assert client.dismiss_rate_limit_modal() is False
    assert client.is_rate_limited() is True


def test_dismissing_when_nothing_is_up_is_a_no_op(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.seed(OLD_TURN)
    client = make_client(session, clock, tmp_path)

    assert client.dismiss_rate_limit_modal() is True
    assert session.clicks == [], "nothing to dismiss, nothing clicked"


def test_a_logged_out_profile_still_wins_over_the_throttle_check(tmp_path):
    """Ordered deliberately: a logged-out page has no conversation to
    throttle, and answering an auth prompt with a back-off would wait out a
    limit that does not exist."""
    session, clock = FakeSession(), FakeClock()
    session.present.add(SEL.login_markers[0])
    session.throttle()
    client = make_client(session, clock, tmp_path)

    with pytest.raises(LoginExpiredError):
        client.attach()
