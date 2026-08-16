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

**A positive rejection is different from ambiguity.** When the optional
send-observation capability is present (`observation.py`), the browser's own
request to the conversation endpoint can *disprove* acceptance: a 4xx/5xx, or a
request that never completed, means the turn was refused. That yields
`SubmitResult.REJECTED` — still not a licence to resend on its own, but a
verdict the orchestrator can confirm by reconciliation and then act on, instead
of parking a human on every dropped send. Absent the capability, or on any mixed
or missing evidence, the result stays UNCONFIRMED and nothing about the old
behaviour changes.

**Navigation is explicit.** `attach()` navigates only when there is no page on
the conversation URL; `reconcile()` is the only reload. Ordinary awaiting polls
the live page so a streaming answer is never interrupted.

**Every wait is bounded** by its own timeout (composer readiness, input
synchronisation, Send readiness, submission confirmation, response start,
response completion, reconciliation), and every timeout writes a structured,
secret-free diagnostic snapshot.

Two further OPTIONAL capabilities the orchestrator probes with `getattr`:

* `supports_chunked_delivery` (declared below) — this transport keeps one
  persistent conversation, so an oversized review packet may be deposited as
  numbered parts before the message that asks for a verdict.
* `mount_message_tail()` — scroll older turns into the virtualized message list
  before a readback concludes something is absent (docs/AUTOLOOP.md §11: a
  10-message conversation mounted only the 6 newest nodes). Not implemented
  here yet; `Orchestrator._part_present` calls it when present and reads only
  what is mounted when it is not, which is the historical behaviour.
  (`find_conversation_with` mounts the tail for its OWN readback through the
  private `_mount_message_tail`. Private on purpose: publishing the name would
  silently change how chunked delivery confirms its parts, which is a separate
  decision from making the search trustworthy.)
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..errors import (
    BrowserError,
    ConversationSearchInconclusive,
    ConversationUnusableError,
    LoginExpiredError,
    ResponseTimeoutError,
    SubmissionError,
)
from .observation import SendObservation, SendOutcome, classify_submission
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
    #: A send was attempted and the browser's own request to the conversation
    #: endpoint demonstrably failed. Acceptance is DISPROVEN, not merely
    #: unknown. Still not self-authorizing: the caller confirms absence by
    #: reconciliation before it may resend. Only ever produced when the
    #: session implements the optional send-observation capability.
    REJECTED = "rejected"


@dataclass(frozen=True)
class TailMount:
    """What one attempt at painting a virtualized message list established.

    `found` is the OR over every window the mount observed, not a property of
    the last one — see `BrowserChatGPT._mount_message_tail` for why the final
    window is the wrong place to look.

    `settled` means the mounted window demonstrably STOPPED CHANGING, which is
    the only state in which `found=False` describes the conversation rather
    than the viewport. False means the attempt bound ran out or the gesture
    failed, and the caller must refuse to conclude rather than report absent.
    (Trivially True alongside `found`: a sighting needs no convergence.)

    `gestures` is how many "go to the end" gestures were actually spent, for
    diagnostics and for tests that pin the 2026-08-05 six-scroll observation.
    """

    found: bool
    settled: bool
    gestures: int


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
    #: Verdict from the optional network observation ("unknown" when the
    #: session does not implement it) and the raw observations behind it.
    #: Each observation is a path + status + coarse failure string; there is
    #: nowhere in `SendObservation` to put a header, cookie or body, so this
    #: field cannot leak credentials into a diagnostics dump.
    send_outcome: str = SendOutcome.UNKNOWN.value
    send_observations: tuple[dict, ...] = ()


class BrowserChatGPT:
    #: Declares that this transport holds ONE persistent, shared conversation,
    #: so several messages sent in sequence accumulate as context the next
    #: message can refer back to. That is what a chunked review packet needs:
    #: the diff arrives as numbered parts and the message asking for a verdict
    #: refers to them. Probed by the orchestrator with `getattr`, so a provider
    #: that does not set it (`codex.conversation`, whose every turn is a fresh
    #: process with no shared history) keeps the pre-chunking behaviour — an
    #: oversized diff is omitted with a notice rather than split into parts
    #: that would each be reviewed as if it were the whole change.
    supports_chunked_delivery = True

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
        tail_mount_attempts: int = 40,
        tail_mount_settle_reads: int = 2,
        rejection_grace_seconds: float | None = None,
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
        # How many "go to the end" gestures a single readback may spend
        # painting a virtualized message list, and how many consecutive
        # gestures that leave the mounted window UNCHANGED prove it has
        # finished.
        #
        # The proof is the window's CONTENT, never its node count. A
        # virtualizer is free to slide a constant-size window — mounting newer
        # nodes while dropping older ones — so a count that reads 6 before and
        # after a gesture says nothing about whether six different messages
        # went past. Two consecutive gestures that leave the window identical
        # do say something.
        #
        # The attempt bound is deliberately generous. Running out means the
        # window was STILL changing, which is not a licence to call anything
        # absent — so a bound set too tight turns an ordinary long conversation
        # into a refusal, and that failure mode is invisible in tests (every
        # fixture is short). Six scrolls sufficed by hand on 2026-08-05 and a
        # real gesture mounts a viewport rather than a node or two, so 40
        # leaves room for a chat an order of magnitude longer. The cost is paid
        # only while the window keeps changing: a settled conversation ends
        # after two identical reads.
        self._tail_mount_attempts = tail_mount_attempts
        self._tail_mount_settle_reads = tail_mount_settle_reads
        # After the first observed send response arrives, wait this long before
        # ruling on it. The web client can retry a failed request behind our
        # back, and a window holding both a failure and a success must classify
        # as UNKNOWN rather than as whichever landed first.
        self._rejection_grace_seconds = (
            rejection_grace_seconds if rejection_grace_seconds is not None else poll_interval * 2
        )
        self._diagnostics_dir = diagnostics_dir
        self._sleep = sleep
        self._monotonic = monotonic
        # Diagnostic bookkeeping for the current request.
        self._send_attempted = False
        self._reconciled = False
        self._observations: list[SendObservation] = []
        self._send_outcome = SendOutcome.UNKNOWN

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

    def reconcile_no_response(self, request_id: str) -> bool:
        """Explicit reload, then report whether the assistant has NOT yet
        begun answering `request_id`, per PERSISTED history.

        `reconcile()`'s counterpart for a confirmed, persisted send whose
        assistant turn never started: `reconcile()` only proves OUR turn
        persisted, which is already known true in that situation, so it
        cannot answer "has the model started replying?" This can — and it
        answers from a FRESH reload rather than the live page
        `await_response` was already polling, because a dropped
        subscription can leave the live DOM stale even where a reload would
        show the truth. This is the orchestrator's "silent conversation"
        rotation entry condition's final check (see
        `orchestrator._attempt_silence_rotation`); an optional capability
        like `retarget`/`current_url`, probed the same way.

        Returns True while the conversation still shows no assistant turn
        for this request (the conversation may still be a rotation
        candidate); False the moment any evidence of a started response
        appears, which cancels rotation.
        """
        if self._on_conversation():
            self._session.reload()
        else:
            self._session.goto(self._conversation_url)
        self._reconciled = True
        self._await_composer("reconcile-no-response", request_id=request_id)
        self._check_logged_in(request_id=request_id, stage="reconcile-no-response")
        return not self._response_started(self.messages(), request_id)

    def close(self) -> None:
        self._session.close()

    @property
    def conversation_url(self) -> str:
        """The conversation this client currently considers authoritative."""
        return self._conversation_url

    def retarget(self, url: str) -> None:
        """Point the client at a different conversation.

        Used only by rotation, in two steps: first at the project page (where a
        new chat has no `/c/<id>` yet), then at the captured conversation URL
        once the first turn has created it. Every page-identity check
        (`attach`, `_require_on_conversation`, `reconcile`) follows this value,
        so retargeting is what stops a post-rotation await from reading the
        abandoned chat.
        """
        self._conversation_url = url

    def current_url(self) -> str:
        """The page's live URL. Used by rotation to learn the id the server
        assigned to a new chat; never trusted on its own — the caller
        reconciles against it before binding anything to it."""
        return self._session.url()

    @property
    def send_outcome(self) -> SendOutcome:
        """Network verdict for the most recent `submit()` call."""
        return self._send_outcome

    @property
    def send_observations(self) -> list[SendObservation]:
        return list(self._observations)

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

    def find_conversation_with(
        self, request_id: str, project_url: str, limit: int = 6
    ) -> str | None:
        """The conversation holding `request_id`, found by CONTENT.

        The address bar is a poor witness for a chat that was just created:
        ChatGPT mints `/c/<id>` some time after accepting the first message,
        and on a slow account that is longer than any polling window worth
        having. Rotation used to give up at 20 seconds and report "the chat id
        was never assigned" — while the chat existed, held the request, and sat
        in the project list (2026-08-03, three times, each leaving an orphan
        chat with a live request nobody read).

        The request id is already in the message, so it identifies the chat
        without help from the URL. This reads the project's list newest-first
        and returns the first conversation whose PERSISTED history carries the
        id, or None.

        Bounded by `limit`: a project accumulates chats, and walking all of
        them costs a page load each. The one we want was just created, so it is
        at the top or nowhere.

        Two things stand between "read the DOM" and an answer worth acting on,
        both learned from a park that should never have happened:

        * **A DOM read is a WINDOW, not history.** The message list is
          virtualized. On 2026-08-05 `alr-af11e1b3-0006` parked as
          `submission_ambiguous`; reading the chat by hand found the request
          present AND already answered, and seeing it took pressing End and
          scrolling six times before the tail rendered. So this MOUNTS the tail
          first (`_mount_message_tail`) and treats absence as concluded only
          once the mounted window demonstrably stops CHANGING — otherwise "not
          in persisted history" is a statement about the scroll position. The
          verdict comes from what the mount SAW, not from a readback after it:
          a sliding window can mount the request and drop it again, so the
          final window is not where the evidence lives.
        * **It confirms WHICH page it read.** Every navigation here is a page
          the client shares with everything else; a rotation mid-flight can
          leave it on a different chat, and a search that reads one
          conversation while believing it read another is confidently wrong in
          both directions — it can name a chat that does not hold the request,
          or report absent while looking away from the one that does. Page
          identity is therefore checked after each navigation AND again around
          the conclusive read.

        Returns the conversation URL, or None when the request is in none of
        the chats examined. Raises `ConversationSearchInconclusive` instead of
        answering when either guarantee above fails — refusing to conclude is
        the point, and it is strictly better than a confident wrong answer.
        """
        self._session.goto(project_url)
        self._await_composer("find_conversation", request_id=request_id)
        if not self._on_project(project_url):
            self._refuse_search(
                request_id,
                "find-conversation-list",
                f"asked for the chat list of {project_url!r} and landed on "
                f"{self._safe_url() or '(unavailable)'!r} — the conversations "
                "listed there are not this project's, so neither finding nor "
                "missing the request in them means anything",
            )
        hrefs: list[str] = []
        # `elements` yields (attribute value, inner text) — the href FIRST.
        # Unpacking it the other way round read each chat's TITLE as its href,
        # and no title contains "/c/", so every candidate was skipped and the
        # search could only ever return None.
        for href, _title in self._session.elements(self._sel.conversation_link, "href"):
            if not href or "/c/" not in href:
                continue
            full = urljoin(project_url, href)
            if full not in hrefs:
                hrefs.append(full)
            if len(hrefs) >= limit:
                break

        unsettled: list[tuple[str, TailMount]] = []
        for candidate in hrefs:
            self._session.goto(candidate)
            self._await_composer("find_conversation", request_id=request_id)
            self._require_search_page(candidate, request_id, "opened")
            mount = self._mount_message_tail(request_id, candidate)
            # Bracket the READ, not just the navigation: the drift this guards
            # against happens while the mount is dwelling. The mount confirms
            # the page before every window it reads, so this covers the one gap
            # left — between its last read and this conclusion.
            self._require_search_page(candidate, request_id, "read")
            if mount.found:
                return candidate
            if not mount.settled:
                unsettled.append((candidate, mount))

        if unsettled:
            # A positive hit stands on its own — every window behind it was
            # read off a confirmed page — so only the ABSENT verdict is
            # withheld here, and only because these chats never finished
            # painting. The gesture count goes in the note because it separates
            # the two ways to be unsettled: a chat that spent the whole bound
            # still changing is a long or streaming conversation, while one that
            # stopped early had its gesture fail.
            detail = ", ".join(f"{url} ({m.gestures} gestures)" for url, m in unsettled)
            self._refuse_search(
                request_id,
                "find-conversation-unmounted",
                f"{len(unsettled)} conversation(s) were still painting messages "
                f"when the mount bound ({self._tail_mount_attempts} scrolls) ran "
                f"out — {detail}; the request was not seen, but "
                "unseen and absent are not the same thing in a virtualized list",
            )
        return None

    def _mount_message_tail(self, request_id: str, expected_url: str) -> TailMount:
        """Paint the newest turns into the virtualized message list.

        ChatGPT mounts a WINDOW of a conversation, never its history, so what
        `messages()` returns is as much a statement about the scroll position
        as about the chat. On 2026-08-05 the conversation holding
        `alr-af11e1b3-0006` needed End plus six scrolls before its tail
        rendered — and the request was in that tail, already answered, while
        the loop parked it as `submission_ambiguous`.

        **The node count proves nothing.** A virtualizer may slide a
        constant-size window, mounting newer nodes as it drops older ones, so
        `len(messages())` can sit at 6 while six different messages are painted
        and scrolled past. Convergence is therefore judged on the window's
        CONTENT: only `_tail_mount_settle_reads` consecutive gestures that
        leave it identical settle the list. What that cannot distinguish is two
        genuinely different windows whose text is byte-identical — possible in
        a fixture, not in a chat where every autoloop turn carries its own
        request id.

        The proof's other boundary, stated because it is real and not closed
        here: an unchanged window means the GESTURE stopped mounting, which is
        the tail when the gesture works and the opening window when it does not
        (`_scroll_message_tail` falls back to End, and End goes to whatever
        holds focus). Telling "short chat, fully mounted" from "the gesture did
        nothing" needs an end-of-list or scroll-position signal, and
        `BrowserSession` exposes none — adding one is a protocol change, not a
        change to this readback. Requiring a change before allowing a settle is
        NOT the fix: it would refuse every conversation short enough to fit in
        the first window.

        Evidence ACCUMULATES across windows rather than being read off the
        final one: the search returns on the first sighting, because a slide
        wide enough can carry the request in and out again between two reads.

        `BrowserError` passes through: its subclasses are the faults the loop
        routes rather than retries (a logged-out profile, a dead session), and
        swallowing one here would demote it into "the request is absent".
        Anything else the gesture raises costs only the settled verdict.

        Every window is read off a page confirmed to still be `expected_url`,
        so a chat drifted onto mid-mount can neither donate a sighting nor
        launder one by drifting back; that check refuses the whole search
        rather than returning.

        This is deliberately more suspicious than the count it replaces: a
        conversation still streaming never settles, and the caller answers
        `ConversationSearchInconclusive` instead of `None`. That is the trade
        the search exists to make — refusing to rule beats ruling wrong.
        """
        fingerprint: tuple[tuple[str, str], ...] | None = None
        unchanged = 0
        gestures = 0
        for _ in range(self._tail_mount_attempts):
            self._require_search_page(expected_url, request_id, "mount")
            window = self.messages()
            if any(m.role == "user" and request_id in m.text for m in window):
                # Positive evidence needs no further painting, and taking it
                # HERE is what makes the accumulation monotone: a later gesture
                # can scroll this window away.
                return TailMount(found=True, settled=True, gestures=gestures)
            current = tuple((m.role, m.text) for m in window)
            if current == fingerprint:
                unchanged += 1
                if unchanged >= self._tail_mount_settle_reads:
                    return TailMount(found=False, settled=True, gestures=gestures)
            else:
                unchanged = 0
                fingerprint = current
            try:
                self._scroll_message_tail()
            except BrowserError:
                raise
            except Exception:
                return TailMount(found=False, settled=False, gestures=gestures)
            gestures += 1
            self._sleep(self._poll_interval)
        return TailMount(found=False, settled=False, gestures=gestures)

    def _scroll_message_tail(self) -> None:
        """One "go to the end of the conversation" gesture.

        `scroll_to_end` is an OPTIONAL session capability, probed exactly like
        `start_send_observation`, so an in-memory fake or a future adapter
        without it stays valid — it falls back to the End key, which is what a
        human presses. The fallback can legitimately do nothing (End goes to
        whatever holds focus), which is why the caller judges progress by what
        the mounted window then CONTAINS rather than by this returning.
        """
        scroll = getattr(self._session, "scroll_to_end", None)
        if scroll is not None:
            scroll(self._sel.message)
            return
        self._session.press("End")

    def is_generating(self) -> bool:
        return self._session.exists(self._sel.stop_button)

    # ---- actions ------------------------------------------------------------

    def submit(
        self, request_id: str, prompt: str, attachment: str | None = None
    ) -> SubmitResult:
        """Type and send one prompt. Never raises on mere ambiguity.

        Returns ALREADY_PERSISTED (nothing sent), CONFIRMED (server accepted),
        or UNCONFIRMED (send attempted, acceptance unknown → reconcile).

        `attachment` is an absolute path uploaded BEFORE the prompt is typed.
        It exists because the composer cannot be verified to hold a large
        patch: `_enter_prompt` reads the editor back and a 30,000-character
        part never returns its own tail, so the client refuses to send —
        correctly, but permanently. A file sidesteps the editor entirely
        (measured 2026-08-15: a 336 KB .md was read in full, quoting canaries
        from its last line). The PROMPT stays small and inline, so the
        review-integrity values remain in the message where the reviewer must
        copy them from.
        """
        self._send_attempted = False
        self._observations = []
        self._send_outcome = SendOutcome.UNKNOWN
        if self.has_request(request_id):
            # Only trustworthy because callers attach/reconcile first.
            return SubmitResult.ALREADY_PERSISTED
        self._wait_not_generating(request_id)
        if attachment is not None:
            self._attach_file(request_id, attachment)
        self._enter_prompt(request_id, prompt)
        self._await_send_ready(request_id)
        # Open the observation window immediately before the click, so nothing
        # the page did earlier can be attributed to this turn.
        self._start_observation()
        self._session.click(self._sel.send_button)
        self._send_attempted = True

        deadline = self._monotonic() + self._submit_timeout
        first_observation_at: float | None = None
        while True:
            self._check_logged_in(request_id=request_id, stage="submit-confirm")
            self._collect_observations()
            if first_observation_at is None and self._observations:
                first_observation_at = self._monotonic()
            # Persisted history beats the network: an assistant turn for our
            # request is direct evidence of acceptance, whatever the transport
            # thought it saw.
            if self._response_started(self.messages(), request_id):
                self._send_outcome = SendOutcome.ACCEPTED
                return SubmitResult.CONFIRMED

            grace_expired = (
                first_observation_at is not None
                and self._monotonic() - first_observation_at >= self._rejection_grace_seconds
            )
            timed_out = self._monotonic() >= deadline
            if grace_expired or timed_out:
                self._send_outcome = classify_submission(self._observations)
                if self._send_outcome is SendOutcome.REJECTED:
                    self.save_diagnostics(
                        "submit-rejected",
                        request_id=request_id,
                        stage="submit-confirm",
                        retry_prohibited=False,
                        note=(
                            "the browser's own send request failed — acceptance "
                            "is disproven, not unknown; confirm by reconciliation "
                            "before resending"
                        ),
                    )
                    return SubmitResult.REJECTED
                if timed_out:
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
                # Observed something non-rejecting (a 2xx, or a mixed window).
                # Neither proves our turn is live, so keep waiting for history
                # until the submit deadline rules.
                first_observation_at = None
            self._sleep(self._poll_interval)

    def await_response(self, request_id: str) -> str:
        """Wait for the completed assistant answer to THIS request.

        Bounded twice: the response must *start* within
        `response_start_timeout` and *complete* within `response_timeout`.
        Polls the live page — no navigation, no reload.
        """
        wait_start = self._monotonic()
        start_deadline = wait_start + self._response_start_timeout
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
                        f"{self._response_start_timeout}s",
                        stage="start",
                        elapsed=self._monotonic() - wait_start,
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
                        f"{self._response_timeout}s",
                        stage="complete",
                    )
            self._sleep(self._poll_interval)

    # ---- network observation (optional capability) --------------------------

    def _start_observation(self) -> None:
        """Open an observation window if the session can provide one.

        Probed with `getattr`, exactly like `send_attempted` is probed in the
        orchestrator: a session without the capability (every in-memory fake, a
        future non-Playwright adapter) simply never contributes observations,
        so `classify_submission` returns UNKNOWN and the transport behaves
        precisely as it did before this capability existed.
        """
        start = getattr(self._session, "start_send_observation", None)
        if start is None:
            return
        try:
            start()
        except Exception:
            # Observation must never be able to fail a send.
            pass

    def _collect_observations(self) -> None:
        take = getattr(self._session, "take_send_observations", None)
        if take is None:
            return
        try:
            self._observations.extend(take())
        except Exception:
            pass

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

    def _attach_file(self, request_id: str, path: str) -> None:
        """Upload `path` and PROVE the right file is on the composer.

        Three things this must get right, all learned live on 2026-08-15:

        * **The tile carries no data-testid.** An upload renders as
          `role="group" aria-label="<filename>"`. Matching the FILENAME rather
          than "any attachment" is what makes this proof: a previous attempt's
          file still sitting on the composer would otherwise read as success
          and send a review request whose diff belongs to another change.
        * **Re-uploading is the ordinary retry case, and ChatGPT blocks it.**
          A file it has already seen produces a duplicate-file modal instead of
          a second attachment. So check for the tile FIRST — if the right file
          is already there, the upload has nothing to do.
        * **A silent no-op is indistinguishable from success.** Without the
          tile check, `set_input_files` returning would be taken as proof, and
          the reviewer would be asked to approve a diff it never received.
        """
        filename = Path(path).name
        chip = self._sel.attachment_chip_for.format(filename=filename)

        # Idempotent: a retry after a failed send usually finds its own file
        # still attached, and re-uploading it only raises the duplicate modal.
        if self._session.exists(chip):
            self._await_upload_complete(request_id, filename)
            return

        # Clear the composer BEFORE attaching. The upload-complete signal below
        # is the Send control becoming enabled, and typed text enables it on
        # its own — so any leftover text would make an in-flight upload look
        # finished. `_enter_prompt` clears it again; doing it here is what
        # makes the signal mean what it says.
        self._session.focus(self._sel.composer)
        self._session.press("ControlOrMeta+a")
        self._session.press("Delete")

        self._session.set_input_files(self._sel.file_input, path)

        deadline = self._monotonic() + self._input_sync_timeout
        while True:
            if self._session.exists(chip):
                self._await_upload_complete(request_id, filename)
                return
            if self._session.exists(self._sel.duplicate_file_modal):
                # "You've already uploaded this file." ChatGPT refuses to
                # attach it a second time. DISMISS it before deciding anything:
                # the modal covers the composer, so leaving it up would block
                # every later attempt as well as this one.
                self._session.click(self._sel.duplicate_file_dismiss)
                self._sleep(self._poll_interval)
                if self._session.exists(chip):
                    self._await_upload_complete(request_id, filename)
                    return  # it was already attached after all
                self.save_diagnostics(
                    "attachment-duplicate-modal",
                    request_id=request_id,
                    stage="attach",
                    retry_prohibited=False,
                    note=(
                        f"{filename} was refused as a duplicate and is not on the "
                        "composer — the modal was dismissed, nothing was sent"
                    ),
                )
                raise SubmissionError(
                    f"attachment {filename} for {request_id} was refused as a "
                    "duplicate-file and is not attached (nothing was sent)"
                )
            if self._monotonic() >= deadline:
                self.save_diagnostics(
                    "attachment-not-accepted",
                    request_id=request_id,
                    stage="attach",
                    retry_prohibited=False,
                    note=(
                        f"no tile for {filename} within {self._input_sync_timeout}s "
                        "— nothing was sent"
                    ),
                )
                raise SubmissionError(
                    f"attachment {filename} for {request_id} did not appear within "
                    f"{self._input_sync_timeout}s (nothing was sent)"
                )
            self._sleep(self._poll_interval)

    def _await_upload_complete(self, request_id: str, filename: str) -> None:
        """Block until the attachment has finished uploading, not merely appeared.

        The tile is a PROMISE, not a completion. Measured live 2026-08-15 on a
        105 KB file: the tile rendered after 0.5s while the Send control stayed
        disabled until 6.3s, which is when the upload actually finished.

        Sending in that window is what lost request alr-75bdba23-0002 — the
        click was accepted, the attachment was consumed, and no turn ever
        persisted, surfacing as `submission_ambiguous`. The composer is empty
        at this point (the caller clears it), so an enabled Send control can
        only mean the upload is done: typed text would enable it on its own and
        make an in-flight upload look finished.
        """
        deadline = self._monotonic() + self._input_sync_timeout
        while True:
            if self._session.exists(self._sel.send_button) and self._session.is_enabled(
                self._sel.send_button
            ):
                return
            if self._monotonic() >= deadline:
                self.save_diagnostics(
                    "attachment-upload-incomplete",
                    request_id=request_id,
                    stage="attach",
                    retry_prohibited=False,
                    note=(
                        f"{filename} attached but the upload did not complete within "
                        f"{self._input_sync_timeout}s — nothing was sent"
                    ),
                )
                raise SubmissionError(
                    f"attachment {filename} for {request_id} did not finish uploading "
                    f"within {self._input_sync_timeout}s (nothing was sent)"
                )
            self._sleep(self._poll_interval)

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

    def _safe_url(self) -> str:
        """The page's URL, or "" when it cannot be read. Never raises — every
        caller here is deciding page identity, and a failed read is simply not
        a match."""
        try:
            return self._session.url() or ""
        except Exception:
            return ""

    def _on_url(self, expected: str) -> bool:
        """Is the page `expected`, ignoring query, fragment and a trailing slash?

        The one comparison rule for page identity in this client. ChatGPT
        appends `?model=…` of its own accord, so an exact string compare would
        refuse the page it just loaded.
        """
        current = self._safe_url()
        if not current:
            return False
        want, have = urlsplit(expected), urlsplit(current)
        return (want.netloc, want.path.rstrip("/")) == (have.netloc, have.path.rstrip("/"))

    def _on_project(self, project_url: str) -> bool:
        """Is the page the project's own landing page?

        Deliberately looser than `_on_url` in one direction and stricter in
        another. Looser: ChatGPT writes the landing page as
        `/g/g-p-<id>/project` but rewrites the id segment with a slug
        (`/g/g-p-<id>-<project-name>/…`), so the last wanted segment may carry
        a `-<suffix>` — `g-p-abc` matches `g-p-abc-x` and never `g-p-abcdef`,
        the segment-boundary trap `Orchestrator._url_in_project` documents.
        Stricter: nothing may follow but the `project` segment itself, so a
        page that restored a CONVERSATION under the project is refused. Its
        chat list is not the project's, and reading one would answer a question
        about the project from a single chat's sidebar.
        """
        current = self._safe_url()
        if not current:
            return False
        want, have = urlsplit(project_url), urlsplit(current)
        if want.netloc != have.netloc:
            return False
        want_segs = [seg for seg in want.path.split("/") if seg]
        if want_segs and want_segs[-1] == "project":
            want_segs.pop()
        have_segs = [seg for seg in have.path.split("/") if seg]
        if len(have_segs) < len(want_segs):
            return False
        for index, wanted in enumerate(want_segs):
            got = have_segs[index]
            last = index == len(want_segs) - 1
            if got != wanted and not (last and got.startswith(wanted + "-")):
                return False
        return have_segs[len(want_segs):] in ([], ["project"])

    @staticmethod
    def _conversation_id(url: str) -> str | None:
        """The id a `/c/<id>` URL ends with, or None for anything else."""
        segs = [seg for seg in urlsplit(url).path.split("/") if seg]
        if len(segs) >= 2 and segs[-2] == "c":
            return segs[-1]
        return None

    def _is_candidate_page(self, candidate: str) -> bool:
        """Is the page the conversation `candidate` names?

        A conversation is identified by its `/c/<id>`, NOT by the project
        prefix in front of it: ChatGPT rewrites that prefix with the project's
        slug, and refusing the rewrite would refuse the page it just loaded —
        the trap `_on_project` documents, and the one that would turn this
        guard into "every search is inconclusive" in production while passing
        every test that types URLs by hand. A different id, or no id at all (an
        auth page, the project list), is still a refusal.
        """
        wanted = self._conversation_id(candidate)
        if wanted is None:
            return self._on_url(candidate)
        current = self._safe_url()
        if not current:
            return False
        return (
            urlsplit(candidate).netloc == urlsplit(current).netloc
            and self._conversation_id(current) == wanted
        )

    def _require_search_page(self, expected: str, request_id: str, stage: str) -> None:
        """Refuse to read a page that is not the one the search asked for."""
        if self._is_candidate_page(expected):
            return
        self._refuse_search(
            request_id,
            f"find-conversation-{stage}",
            f"the search asked for {expected!r} and is on "
            f"{self._safe_url() or '(unavailable)'!r} — whatever it reads there "
            f"is not evidence about {expected!r}, in either direction",
        )

    def _refuse_search(self, request_id: str, stage: str, note: str) -> None:
        """Snapshot and raise. The refusal is the product, so it is evidenced
        like any other transport failure."""
        self.save_diagnostics(
            "conversation-search-inconclusive",
            request_id=request_id,
            stage=stage,
            retry_prohibited=False,
            note=note,
        )
        raise ConversationSearchInconclusive(note)

    def _on_conversation(self) -> bool:
        return self._on_url(self._conversation_url)

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
            if any(
                self._session.exists(marker) for marker in self._sel.conversation_error_markers
            ):
                self.save_diagnostics(
                    "conversation-unusable",
                    request_id=request_id,
                    stage=stage,
                    retry_prohibited=False,
                    note="the conversation reports itself unavailable",
                )
                raise ConversationUnusableError(
                    "the configured conversation reports itself unavailable "
                    "(deleted, or its history failed to load)"
                )
            if self._session.exists(self._sel.composer):
                return
            if self._monotonic() >= deadline:
                # Distinguish "this chat is wedged" from "the browser is
                # unhappy". Only the former authorizes a rotation, and a run
                # gets one — spending it on a slow page load would leave none
                # for the real thing. `_on_conversation()` is what separates
                # them: it means the page demonstrably reached the configured
                # conversation (and `_check_logged_in` above has already ruled
                # out an auth redirect) yet still has no composer.
                on_conversation = self._on_conversation()
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
                if on_conversation:
                    raise ConversationUnusableError(
                        "the configured conversation loaded but never produced a "
                        "composer — this chat appears wedged"
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
            send_outcome=self._send_outcome.value,
            send_observations=tuple(asdict(obs) for obs in self._observations),
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
