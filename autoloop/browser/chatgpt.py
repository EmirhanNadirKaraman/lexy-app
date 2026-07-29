"""ChatGPT interaction on top of a BrowserSession.

`BrowserChatGPT` is the browser-driven implementation of the
`autoloop.conversation.LLMConversation` interface — the orchestrator only
sees that interface, so alternative providers (Claude.ai, Gemini, ...) are
adapters registered in `conversation.py`, not changes here.

Robustness rules this client enforces:

* Duplicate submissions — every prompt embeds its request-id; before sending,
  and again after a crash/restart, existing *user* messages are scanned for
  that id. Present → the prompt is already in the conversation, never resend.
* Stale responses — a reply counts only if it is the LAST message in the
  conversation, is an assistant message, and appears AFTER the user message
  carrying the current request-id. Old assistant messages can never be
  mistaken for the new answer.
* Completion detection — the stop/generating button must be absent AND the
  candidate text must be unchanged for `stability_seconds` before the reply is
  accepted (streaming produces growing text; the stability window filters it).
* Login expiry — checked on open and on every await poll (URL fragments +
  login-button markers). Raises LoginExpiredError; the loop parks for a human.
* Diagnostics — on submit failure / timeout / logout, a timestamped folder
  gets a screenshot (best effort), the page HTML, and a meta.json.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ..errors import (
    BrowserError,
    LoginExpiredError,
    ResponseTimeoutError,
    SubmissionError,
)
from .selectors import ChatGPTSelectors
from .session import BrowserSession, Message


class BrowserChatGPT:
    def __init__(
        self,
        session: BrowserSession,
        conversation_url: str,
        *,
        selectors: ChatGPTSelectors | None = None,
        response_timeout: float = 900.0,
        submit_timeout: float = 60.0,
        poll_interval: float = 2.0,
        stability_seconds: float = 3.0,
        diagnostics_dir: Path | None = None,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        self._session = session
        self._conversation_url = conversation_url
        self._sel = selectors or ChatGPTSelectors()
        self._response_timeout = response_timeout
        self._submit_timeout = submit_timeout
        self._poll_interval = poll_interval
        self._stability_seconds = stability_seconds
        self._diagnostics_dir = diagnostics_dir
        self._sleep = sleep
        self._monotonic = monotonic

    # ---- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        self._session.goto(self._conversation_url)
        deadline = self._monotonic() + self._submit_timeout
        while True:
            self._check_logged_in()
            if self._session.exists(self._sel.composer):
                return
            if self._monotonic() >= deadline:
                self.save_diagnostics("composer-not-found")
                raise BrowserError(
                    "composer not found — page did not load, or the ChatGPT UI "
                    "changed (see autoloop/browser/selectors.py)"
                )
            self._sleep(self._poll_interval)

    def close(self) -> None:
        self._session.close()

    # ---- conversation reads -------------------------------------------------

    def messages(self) -> list[Message]:
        return [
            Message(role=attr, text=text)
            for attr, text in self._session.elements(self._sel.message, self._sel.role_attr)
        ]

    def already_submitted(self, request_id: str) -> bool:
        """True if a *user* message carrying this request-id already exists."""
        return any(
            m.role == "user" and request_id in m.text for m in self.messages()
        )

    def is_generating(self) -> bool:
        return self._session.exists(self._sel.stop_button)

    # ---- actions ------------------------------------------------------------

    def submit(self, request_id: str, prompt: str) -> None:
        if self.already_submitted(request_id):
            return
        self._wait_not_generating()
        self._session.fill(self._sel.composer, prompt)
        self._session.click(self._sel.send_button)
        deadline = self._monotonic() + self._submit_timeout
        while self._monotonic() < deadline:
            if self.already_submitted(request_id):
                return
            self._sleep(self._poll_interval)
        self.save_diagnostics("submit-unconfirmed")
        raise SubmissionError(
            f"prompt {request_id} was not confirmed in the conversation "
            f"within {self._submit_timeout}s"
        )

    def await_response(self, request_id: str) -> str:
        deadline = self._monotonic() + self._response_timeout
        stable_text: str | None = None
        stable_since: float = 0.0
        while self._monotonic() < deadline:
            self._check_logged_in()
            msgs = self.messages()
            candidate = self._completed_reply(msgs, request_id)
            if candidate is not None:
                if candidate == stable_text:
                    if self._monotonic() - stable_since >= self._stability_seconds:
                        return candidate
                else:
                    stable_text = candidate
                    stable_since = self._monotonic()
            else:
                stable_text = None
            self._sleep(self._poll_interval)
        self.save_diagnostics("response-timeout")
        raise ResponseTimeoutError(
            f"no completed assistant response to {request_id} "
            f"within {self._response_timeout}s"
        )

    def _completed_reply(self, msgs: list[Message], request_id: str) -> str | None:
        """The staleness guard: only the last message qualifies, only if it is
        an assistant message that comes after our request's user message, and
        only while generation is not running."""
        request_idx = None
        for i, m in enumerate(msgs):
            if m.role == "user" and request_id in m.text:
                request_idx = i
        if request_idx is None or request_idx >= len(msgs) - 1:
            return None
        last = msgs[-1]
        if last.role != "assistant" or not last.text.strip():
            return None
        if self.is_generating():
            return None
        return last.text

    # ---- helpers ------------------------------------------------------------

    def _wait_not_generating(self) -> None:
        deadline = self._monotonic() + self._submit_timeout
        while self.is_generating():
            if self._monotonic() >= deadline:
                self.save_diagnostics("still-generating")
                raise SubmissionError(
                    "a previous generation is still running; refusing to submit"
                )
            self._sleep(self._poll_interval)

    def _check_logged_in(self) -> None:
        url = self._session.url()
        logged_out = any(frag in url for frag in self._sel.logged_out_url_fragments) or any(
            self._session.exists(marker) for marker in self._sel.login_markers
        )
        if logged_out:
            self.save_diagnostics("login-expired")
            raise LoginExpiredError(
                "ChatGPT session is logged out — open the dedicated browser "
                "profile, log back in, then resume with `python -m autoloop run --retry`"
            )

    def save_diagnostics(self, tag: str) -> Path | None:
        """Best-effort failure snapshot. Never raises."""
        if self._diagnostics_dir is None:
            return None
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            folder = Path(self._diagnostics_dir) / f"{stamp}-{tag}"
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        try:
            (folder / "page.html").write_text(self._session.html(), encoding="utf-8")
        except Exception:
            pass
        try:
            self._session.screenshot(folder / "page.png")
        except Exception:
            pass
        try:
            meta = {"tag": tag, "url": self._session.url(), "ts": stamp}
            (folder / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            pass
        return folder
