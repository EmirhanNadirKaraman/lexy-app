"""ChatGPT interaction on top of a BrowserSession.

`BrowserChatGPT` is the browser-driven implementation of the
`autoloop.conversation.LLMConversation` interface — the orchestrator only sees
that interface, so alternative providers (Claude.ai, Gemini, ...) are adapters
registered in `conversation.py`, not changes here.

The rules below exist because of a concrete failure (2026-07-29): a prompt was
typed, Send was clicked, ChatGPT rendered the user bubble *optimistically*, the
client accepted that bubble as proof of submission — and the message was never
persisted. A reload then erased the evidence and the client waited 15 minutes
for a reply that could not exist.

**Optimistic rendering is not submission.** A user bubble in the current,
unreloaded DOM proves only that the browser drew something. Submission counts
as CONFIRMED only when the server demonstrably accepted the turn:

  * an assistant response for *our* turn has begun (a node after our request,
    or generation is running), or
  * an explicit `reconcile()` — a controlled reload — finds our request id in
    persisted conversation history.

Anything weaker yields `SubmitResult.UNCONFIRMED`, which is an *ambiguous*
outcome, never an implicit retry: the backend may have accepted the message
while the browser failed to observe it, so re-sending could double-post.

**Navigation is explicit.** `attach()` navigates only when there is no page on
the conversation URL; `reconcile()` is the only reload. Ordinary awaiting polls
the live page so a streaming answer is never interrupted.

**Every wait is bounded** by its own timeout (composer readiness, input
synchronisation, Send readiness, submission confirmation, response start,
response completion, reconciliation), and every timeout writes a structured,
secret-free diagnostic snapshot.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import urlsplit

from ..errors import (
    BrowserError,
    LoginExpiredError,
    ResponseTimeoutError,
    SubmissionError,
)
from .selectors import ChatGPTSelectors
from .session import BrowserSession, Message


class SubmitResult(str, Enum):
    #: The request id was already in persisted history; nothing was sent.
    ALREADY_PERSISTED = "already_persisted"
    #: Sent, and the server demonstrably accepted the turn.
    CONFIRMED = "confirmed"
    #: A send was attempted, but acceptance could not be established. The
    #: caller must reconcile; it must NOT resend on its own.
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class TransportDiagnostics:
    """Structured failure context. Deliberately carries no cookies, tokens or
    storage — the session protocol cannot even read them."""

    tag: str
    request_id: str | None
    stage: str
    configured_url: str
    actual_url: str
    composer_present: bool
    composer_chars: int
    composer_has_request_id: bool | None
    send_button_present: bool
    send_button_enabled: bool
    message_count: int
    matching_user_messages: int
    assistant_after_request: bool
    generating: bool
    send_attempted: bool
    reconciled: bool
    retry_prohibited: bool
    note: str = ""


class BrowserChatGPT:
    def __init__(
        self,
        session: BrowserSession,
        conversation_url: str,
        *,
        selectors: ChatGPTSelectors | None = None,
        response_timeout: float = 900.0,
        response_start_timeout: float = 120.0,
        submit_timeout: float = 60.0,
        composer_timeout: float = 30.0,
        input_sync_timeout: float = 30.0,
        send_ready_timeout: float = 30.0,
        reconcile_timeout: float = 30.0,
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
        self._response_start_timeout = response_start_timeout
        self._submit_timeout = submit_timeout
        self._composer_timeout = composer_timeout
        self._input_sync_timeout = input_sync_timeout
        self._send_ready_timeout = send_ready_timeout
        self._reconcile_timeout = reconcile_timeout
        self._poll_interval = poll_interval
        self._stability_seconds = stability_seconds
        self._diagnostics_dir = diagnostics_dir
        self._sleep = sleep
        self._monotonic = monotonic
        # Diagnostic bookkeeping for the current request.
        self._send_attempted = False
        self._reconciled = False

    # ---- lifecycle ----------------------------------------------------------

    def attach(self) -> None:
        """Ensure a usable page on the configured conversation.

        Navigates ONLY when the current page is elsewhere (or nothing is
        loaded). Called at the top of every phase, so it must be a no-op in the
        common case — that is what keeps a streaming answer alive.
        """
        if not self._on_conversation():
            self._session.goto(self._conversation_url)
        self._await_composer("attach")

    def reconcile(self, request_id: str) -> bool:
        """Explicit reload, then check *persisted* history for `request_id`.

        This is the only reload in the client and the only authority on whether
        a submission actually landed.
        """
        # Exactly one page load: reload when already here, navigate when not.
        # (Doing both would double every reconcile's latency for nothing.)
        if self._on_conversation():
            self._session.reload()
        else:
            self._session.goto(self._conversation_url)
        self._reconciled = True
        self._await_composer("reconcile", request_id=request_id)
        deadline = self._monotonic() + self._reconcile_timeout
        while True:
            self._check_logged_in(request_id=request_id, stage="reconcile")
            if self.has_request(request_id):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(self._poll_interval)

    def close(self) -> None:
        self._session.close()

    @property
    def send_attempted(self) -> bool:
        """True once Send was clicked for the current request.

        The orchestrator reads this after a failed `submit()` to decide whether
        a resend is provably safe: False means nothing left the browser.
        """
        return self._send_attempted

    # ---- conversation reads -------------------------------------------------

    def messages(self) -> list[Message]:
        return [
            Message(role=attr, text=text)
            for attr, text in self._session.elements(self._sel.message, self._sel.role_attr)
        ]

    def has_request(self, request_id: str) -> bool:
        """True if a *user* message carrying this request-id is in the DOM.

        Meaningful only on a freshly loaded page (see `reconcile`). Never use it
        on its own to confirm a send — that is the bug this module documents.
        """
        return any(m.role == "user" and request_id in m.text for m in self.messages())

    def is_generating(self) -> bool:
        return self._session.exists(self._sel.stop_button)

    # ---- actions ------------------------------------------------------------

    def submit(self, request_id: str, prompt: str) -> SubmitResult:
        """Type and send one prompt. Never raises on mere ambiguity.

        Returns ALREADY_PERSISTED (nothing sent), CONFIRMED (server accepted),
        or UNCONFIRMED (send attempted, acceptance unknown → reconcile).
        """
        self._send_attempted = False
        if self.has_request(request_id):
            # Only trustworthy because callers attach/reconcile first.
            return SubmitResult.ALREADY_PERSISTED
        self._wait_not_generating(request_id)
        self._enter_prompt(request_id, prompt)
        self._await_send_ready(request_id)
        self._session.click(self._sel.send_button)
        self._send_attempted = True

        deadline = self._monotonic() + self._submit_timeout
        while True:
            self._check_logged_in(request_id=request_id, stage="submit-confirm")
            if self._response_started(self.messages(), request_id):
                return SubmitResult.CONFIRMED
            if self._monotonic() >= deadline:
                self.save_diagnostics(
                    "submit-unconfirmed",
                    request_id=request_id,
                    stage="submit-confirm",
                    retry_prohibited=True,
                    note=(
                        f"send was clicked but no assistant turn began within "
                        f"{self._submit_timeout}s; acceptance unknown — "
                        "reconcile before any resend"
                    ),
                )
                return SubmitResult.UNCONFIRMED
            self._sleep(self._poll_interval)

    def await_response(self, request_id: str) -> str:
        """Wait for the completed assistant answer to THIS request.

        Bounded twice: the response must *start* within
        `response_start_timeout` and *complete* within `response_timeout`.
        Polls the live page — no navigation, no reload.
        """
        start_deadline = self._monotonic() + self._response_start_timeout
        started = False
        stable_text: str | None = None
        stable_since = 0.0
        complete_deadline = self._monotonic() + self._response_timeout

        while True:
            self._check_logged_in(request_id=request_id, stage="await")
            self._require_on_conversation(request_id)
            msgs = self.messages()

            if not started:
                if self._response_started(msgs, request_id):
                    started = True
                    complete_deadline = self._monotonic() + self._response_timeout
                elif self._monotonic() >= start_deadline:
                    self.save_diagnostics(
                        "response-not-started",
                        request_id=request_id,
                        stage="await-start",
                        retry_prohibited=False,
                        note=(
                            "no assistant turn for this request began within "
                            f"{self._response_start_timeout}s"
                        ),
                    )
                    raise ResponseTimeoutError(
                        f"no assistant response to {request_id} began within "
                        f"{self._response_start_timeout}s"
                    )

            if started:
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
                if self._monotonic() >= complete_deadline:
                    self.save_diagnostics(
                        "response-timeout",
                        request_id=request_id,
                        stage="await-complete",
                        retry_prohibited=False,
                        note=(
                            "assistant turn began but did not settle within "
                            f"{self._response_timeout}s"
                        ),
                    )
                    raise ResponseTimeoutError(
                        f"assistant response to {request_id} did not complete within "
                        f"{self._response_timeout}s"
                    )
            self._sleep(self._poll_interval)

    # ---- turn matching ------------------------------------------------------

    def _request_index(self, msgs: list[Message], request_id: str) -> int | None:
        found = None
        for i, m in enumerate(msgs):
            if m.role == "user" and request_id in m.text:
                found = i
        return found

    def _response_started(self, msgs: list[Message], request_id: str) -> bool:
        """Has the assistant begun answering OUR turn?

        Requires our request to be present AND either an assistant node after
        it or an active generation. An assistant message *before* our request
        (the conversation's earlier replies) can never satisfy this.
        """
        idx = self._request_index(msgs, request_id)
        if idx is None:
            return False
        if any(m.role == "assistant" for m in msgs[idx + 1:]):
            return True
        return self.is_generating()

    def _completed_reply(self, msgs: list[Message], request_id: str) -> str | None:
        """Staleness guard: the reply must be the LAST message, authored by the
        assistant, positioned after our request, with generation stopped."""
        idx = self._request_index(msgs, request_id)
        if idx is None or idx >= len(msgs) - 1:
            return None
        last = msgs[-1]
        if last.role != "assistant" or not last.text.strip():
            return None
        if self.is_generating():
            return None
        return last.text

    # ---- input --------------------------------------------------------------

    def _enter_prompt(self, request_id: str, prompt: str) -> None:
        """Drive the contenteditable the way a person does: focus, clear with
        the keyboard, insert text as an input event, then verify the editor
        really holds the whole request."""
        self._session.focus(self._sel.composer)
        self._session.press("ControlOrMeta+a")
        self._session.press("Delete")
        self._session.insert_text(prompt)

        tail = prompt.strip()[-40:]
        deadline = self._monotonic() + self._input_sync_timeout
        while True:
            text = self._session.inner_text(self._sel.composer)
            if request_id in text and tail in text:
                return
            if self._monotonic() >= deadline:
                self.save_diagnostics(
                    "composer-not-synchronised",
                    request_id=request_id,
                    stage="input-sync",
                    retry_prohibited=False,
                    note=(
                        "composer did not contain the full request after "
                        f"{self._input_sync_timeout}s — no send was attempted"
                    ),
                )
                raise SubmissionError(
                    f"composer did not accept the full request {request_id} within "
                    f"{self._input_sync_timeout}s (nothing was sent)"
                )
            self._sleep(self._poll_interval)

    def _await_send_ready(self, request_id: str) -> None:
        deadline = self._monotonic() + self._send_ready_timeout
        while True:
            if self._session.exists(self._sel.send_button) and self._session.is_enabled(
                self._sel.send_button
            ):
                return
            if self._monotonic() >= deadline:
                self.save_diagnostics(
                    "send-not-ready",
                    request_id=request_id,
                    stage="send-ready",
                    retry_prohibited=False,
                    note=(
                        "the Send control never became enabled — the editor "
                        "likely did not register the input; no send was attempted"
                    ),
                )
                raise SubmissionError(
                    "the ChatGPT Send control did not become enabled within "
                    f"{self._send_ready_timeout}s (nothing was sent)"
                )
            self._sleep(self._poll_interval)

    def _wait_not_generating(self, request_id: str) -> None:
        deadline = self._monotonic() + self._send_ready_timeout
        while self.is_generating():
            if self._monotonic() >= deadline:
                self.save_diagnostics(
                    "still-generating",
                    request_id=request_id,
                    stage="pre-send",
                    retry_prohibited=False,
                    note="a previous generation was still running; nothing was sent",
                )
                raise SubmissionError(
                    "a previous generation is still running; refusing to submit"
                )
            self._sleep(self._poll_interval)

    # ---- page identity ------------------------------------------------------

    def _on_conversation(self) -> bool:
        try:
            current = self._session.url()
        except Exception:
            return False
        if not current:
            return False
        want, have = urlsplit(self._conversation_url), urlsplit(current)
        return (want.netloc, want.path.rstrip("/")) == (have.netloc, have.path.rstrip("/"))

    def _require_on_conversation(self, request_id: str) -> None:
        """Awaiting must not silently renavigate; a drifted page is an error the
        orchestrator recovers from by re-attaching."""
        if not self._on_conversation():
            self.save_diagnostics(
                "page-drifted",
                request_id=request_id,
                stage="await",
                retry_prohibited=False,
                note="the page left the configured conversation while awaiting",
            )
            raise BrowserError(
                f"page left the configured conversation while awaiting {request_id} "
                f"(now at {self._session.url()!r})"
            )

    def _await_composer(self, stage: str, request_id: str | None = None) -> None:
        deadline = self._monotonic() + self._composer_timeout
        while True:
            self._check_logged_in(request_id=request_id, stage=stage)
            if self._session.exists(self._sel.composer):
                return
            if self._monotonic() >= deadline:
                self.save_diagnostics(
                    "composer-not-found",
                    request_id=request_id,
                    stage=stage,
                    retry_prohibited=False,
                    note=(
                        "composer never appeared — page did not load, or the "
                        "ChatGPT UI changed (see autoloop/browser/selectors.py)"
                    ),
                )
                raise BrowserError(
                    "composer not found — page did not load, or the ChatGPT UI "
                    "changed (see autoloop/browser/selectors.py)"
                )
            self._sleep(self._poll_interval)

    def _check_logged_in(self, request_id: str | None = None, stage: str = "") -> None:
        url = self._session.url()
        logged_out = any(frag in url for frag in self._sel.logged_out_url_fragments) or any(
            self._session.exists(marker) for marker in self._sel.login_markers
        )
        if logged_out:
            self.save_diagnostics(
                "login-expired",
                request_id=request_id,
                stage=stage,
                retry_prohibited=self._send_attempted,
                note="ChatGPT session is logged out",
            )
            raise LoginExpiredError(
                "ChatGPT session is logged out — open the dedicated browser "
                "profile, log back in, then resume with `python -m autoloop run --retry`"
            )

    # ---- diagnostics --------------------------------------------------------

    def snapshot(
        self,
        tag: str,
        *,
        request_id: str | None = None,
        stage: str = "",
        retry_prohibited: bool = False,
        note: str = "",
    ) -> TransportDiagnostics:
        """Structured state for a failure report. Never raises; never touches
        cookies, tokens or storage."""

        def safe(fn, default):
            try:
                return fn()
            except Exception:
                return default

        msgs = safe(self.messages, [])
        composer_text = safe(lambda: self._session.inner_text(self._sel.composer), "")
        idx = self._request_index(msgs, request_id) if request_id else None
        return TransportDiagnostics(
            tag=tag,
            request_id=request_id,
            stage=stage,
            configured_url=self._conversation_url,
            actual_url=safe(self._session.url, "(unavailable)"),
            composer_present=safe(lambda: self._session.exists(self._sel.composer), False),
            composer_chars=len(composer_text),
            composer_has_request_id=(request_id in composer_text) if request_id else None,
            send_button_present=safe(lambda: self._session.exists(self._sel.send_button), False),
            send_button_enabled=safe(
                lambda: self._session.is_enabled(self._sel.send_button), False
            ),
            message_count=len(msgs),
            matching_user_messages=sum(
                1 for m in msgs if m.role == "user" and request_id and request_id in m.text
            ),
            assistant_after_request=(
                any(m.role == "assistant" for m in msgs[idx + 1:]) if idx is not None else False
            ),
            generating=safe(self.is_generating, False),
            send_attempted=self._send_attempted,
            reconciled=self._reconciled,
            retry_prohibited=retry_prohibited,
            note=note,
        )

    def save_diagnostics(self, tag: str, **kwargs) -> Path | None:
        """Best-effort failure snapshot: structured meta + page HTML +
        screenshot. Never raises."""
        diag = self.snapshot(tag, **kwargs)
        if self._diagnostics_dir is None:
            return None
        try:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            folder = Path(self._diagnostics_dir) / f"{stamp}-{tag}"
            folder.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        try:
            payload = {"ts": stamp, **asdict(diag)}
            (folder / "meta.json").write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
        try:
            (folder / "page.html").write_text(self._session.html(), encoding="utf-8")
        except Exception:
            pass
        try:
            self._session.screenshot(folder / "page.png")
        except Exception:
            pass
        return folder
