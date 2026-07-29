"""ChatGPT client against an in-memory fake session: submission confirmation,
duplicate detection, stale-response and streaming guards, login expiry,
timeouts + diagnostics. No playwright, no network."""

from pathlib import Path

import pytest

from autoloop.browser.chatgpt import BrowserChatGPT
from autoloop.browser.selectors import ChatGPTSelectors
from autoloop.errors import (
    BrowserError,
    LoginExpiredError,
    ResponseTimeoutError,
    SubmissionError,
)

SEL = ChatGPTSelectors()
CONV_URL = "https://chatgpt.com/c/test-conversation"
RID = "alr-abcd1234-0001"


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
    def __init__(self):
        self.current_url = CONV_URL
        self.present = {SEL.composer}
        self.msgs: list[tuple[str, str]] = []
        self.filled_text: str | None = None
        self.send_appends = True
        self.clicks: list[str] = []
        self.closed = False

    def goto(self, url):
        self.last_goto = url

    def url(self):
        return self.current_url

    def exists(self, selector):
        return selector in self.present

    def click(self, selector):
        self.clicks.append(selector)
        if selector == SEL.send_button and self.send_appends and self.filled_text is not None:
            self.msgs.append(("user", self.filled_text))

    def fill(self, selector, text):
        self.filled_text = text

    def elements(self, selector, attr):
        return list(self.msgs)

    def screenshot(self, path):
        Path(path).write_bytes(b"\x89PNG-fake")

    def html(self):
        return "<html>fake</html>"

    def close(self):
        self.closed = True


def make_client(session, clock, tmp_path, **overrides) -> BrowserChatGPT:
    kwargs = dict(
        response_timeout=30.0,
        submit_timeout=10.0,
        poll_interval=1.0,
        stability_seconds=2.0,
        diagnostics_dir=tmp_path / "diag",
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    kwargs.update(overrides)
    return BrowserChatGPT(session, CONV_URL, **kwargs)


def diagnostics_folders(tmp_path) -> list[Path]:
    diag = tmp_path / "diag"
    return sorted(diag.iterdir()) if diag.exists() else []


# ---- open_conversation ------------------------------------------------------


def test_open_conversation_ok(tmp_path):
    session, clock = FakeSession(), FakeClock()
    make_client(session, clock, tmp_path).open()
    assert session.last_goto == CONV_URL


def test_open_detects_login_redirect(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.current_url = "https://auth.openai.com/authorize?x=1"
    with pytest.raises(LoginExpiredError):
        make_client(session, clock, tmp_path).open()
    assert diagnostics_folders(tmp_path)


def test_open_detects_login_marker(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.present.add(SEL.login_markers[0])
    with pytest.raises(LoginExpiredError):
        make_client(session, clock, tmp_path).open()


def test_open_times_out_without_composer(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.present = set()
    with pytest.raises(BrowserError):
        make_client(session, clock, tmp_path, submit_timeout=3.0).open()
    folders = diagnostics_folders(tmp_path)
    assert folders and (folders[0] / "page.html").exists()


# ---- submit -----------------------------------------------------------------


def test_submit_fills_clicks_and_confirms(tmp_path):
    session, clock = FakeSession(), FakeClock()
    client = make_client(session, clock, tmp_path)
    client.submit(RID, f"header {RID}\nbody")
    assert session.clicks == [SEL.send_button]
    assert session.msgs == [("user", f"header {RID}\nbody")]


def test_submit_skips_duplicate(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.msgs = [("user", f"[autoloop request {RID} | iteration 1] older text")]
    client = make_client(session, clock, tmp_path)
    client.submit(RID, "should not be sent")
    assert session.clicks == []
    assert session.filled_text is None


def test_submit_unconfirmed_raises_with_diagnostics(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.send_appends = False  # the send silently does nothing
    client = make_client(session, clock, tmp_path)
    with pytest.raises(SubmissionError):
        client.submit(RID, f"prompt {RID}")
    assert diagnostics_folders(tmp_path)


def test_submit_waits_for_previous_generation(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.present.add(SEL.stop_button)
    clock.events[2] = lambda: session.present.discard(SEL.stop_button)
    client = make_client(session, clock, tmp_path)
    client.submit(RID, f"prompt {RID}")
    assert session.msgs[-1][0] == "user"


def test_submit_gives_up_if_generation_never_stops(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.present.add(SEL.stop_button)
    client = make_client(session, clock, tmp_path, submit_timeout=4.0)
    with pytest.raises(SubmissionError):
        client.submit(RID, f"prompt {RID}")


# ---- await_response ---------------------------------------------------------


def test_await_returns_stable_reply(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.msgs = [("user", f"request {RID}")]
    clock.events[1] = lambda: session.msgs.append(("assistant", "THE ANSWER"))
    client = make_client(session, clock, tmp_path)
    assert client.await_response(RID) == "THE ANSWER"


def test_await_waits_out_streaming_text(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.msgs = [("user", f"request {RID}")]
    clock.events[1] = lambda: session.msgs.append(("assistant", "partial"))
    clock.events[2] = lambda: session.msgs.__setitem__(-1, ("assistant", "partial then full"))
    client = make_client(session, clock, tmp_path)
    assert client.await_response(RID) == "partial then full"


def test_await_ignores_stale_assistant_message(tmp_path):
    # The only assistant message predates our request -> must never be returned.
    session, clock = FakeSession(), FakeClock()
    session.msgs = [("assistant", "OLD ANSWER"), ("user", f"request {RID}")]
    client = make_client(session, clock, tmp_path, response_timeout=6.0)
    with pytest.raises(ResponseTimeoutError):
        client.await_response(RID)
    assert diagnostics_folders(tmp_path)


def test_await_ignores_reply_while_generating(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.msgs = [("user", f"request {RID}"), ("assistant", "half-written")]
    session.present.add(SEL.stop_button)  # never finishes
    client = make_client(session, clock, tmp_path, response_timeout=6.0)
    with pytest.raises(ResponseTimeoutError):
        client.await_response(RID)


def test_await_detects_logout_mid_wait(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.msgs = [("user", f"request {RID}")]

    def log_out():
        session.current_url = "https://chatgpt.com/auth/login"

    clock.events[2] = log_out
    client = make_client(session, clock, tmp_path)
    with pytest.raises(LoginExpiredError):
        client.await_response(RID)


def test_already_submitted_only_matches_user_messages(tmp_path):
    session, clock = FakeSession(), FakeClock()
    session.msgs = [("assistant", f"echoing your id {RID}")]
    client = make_client(session, clock, tmp_path)
    assert not client.already_submitted(RID)
    session.msgs.append(("user", f"request {RID}"))
    assert client.already_submitted(RID)
