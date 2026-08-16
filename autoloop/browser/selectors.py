"""ChatGPT DOM selectors, isolated so UI drift is a one-file fix.

Defaults match chatgpt.com as of 2026-07. If OpenAI changes the DOM, override
individual selectors here (or subclass in a follow-up config knob) — the client
logic never hard-codes a selector string.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChatGPTSelectors:
    composer: str = "#prompt-textarea"
    send_button: str = '[data-testid="send-button"]'
    #: Hidden behind the attach button; Playwright can still set files on it.
    file_input: str = 'input[type=file]'
    #: Proof an attachment actually landed, as a format string taking the
    #: FILENAME. ChatGPT renders an upload as a "file tile" carrying
    #: role="group" and aria-label="<filename>" — there is no data-testid on
    #: it (checked live 2026-08-15).
    #:
    #: Matching the filename rather than "any attachment" is deliberate: it
    #: proves THE RIGHT file is on the composer. A previous attempt's file
    #: still sitting there would otherwise read as success and send a review
    #: request whose diff belongs to another change.
    attachment_chip_for: str = '[role="group"][aria-label="{filename}"]'
    #: ChatGPT refuses a file it has already seen with a blocking modal rather
    #: than attaching it again. A retry re-uploads the same path, so this is
    #: the ordinary case on the second attempt, not an edge case.
    duplicate_file_modal: str = '[data-testid="modal-duplicate-file"]'
    #: Its only control ("OK"). The modal must be DISMISSED, not merely
    #: detected: it covers the composer, so leaving it up blocks every
    #: subsequent attempt as well as this one.
    duplicate_file_dismiss: str = '[data-testid="modal-duplicate-file"] button'
    #: ChatGPT's ACCOUNT-level throttle ("Too many requests… We have
    #: temporarily limited access to your conversations to protect your data.
    #: Please wait a few minutes before trying again." / "Got it"). A
    #: full-screen `absolute inset-0` overlay at z-50, captured live
    #: 2026-08-15 by clicking while throttled — Playwright named it as the
    #: element that intercepted the click.
    #:
    #: Matched on the TESTID, never on the prose: the wording and the locale
    #: both move, the testid does not. And the prose cannot be found anyway —
    #: a search for "Too many requests" in the page's `inner_text` reported
    #: healthy against a firmly throttled account, along with two other
    #: passive checks (page loads, message count) and, worst of all, composer
    #: presence.
    #:
    #: **THE TRAP:** the composer still EXISTS and reports visible/enabled
    #: while this is up — the overlay only intercepts pointer events. So
    #: `exists(composer)` is NOT evidence the page is usable, and any
    #: readiness probe written against it alone reports a false all-clear.
    #: Only attempting an interaction, or testing for THIS, tells them apart.
    rate_limit_modal: str = '[data-testid="modal-conversation-history-rate-limit"]'
    #: Candidates for the modal's single dismiss control ("Got it"), tried in
    #: order until one exists. A tuple rather than one string because the
    #: element carrying the testid is the full-screen OVERLAY, so the button
    #: may be a sibling of it rather than a descendant — and nothing about the
    #: live capture settles which. Dismissal is deliberately best-effort for
    #: exactly that reason: the verdict comes from `rate_limit_modal` being
    #: GONE on the next probe, never from a click having succeeded.
    rate_limit_dismiss: tuple[str, ...] = (
        '[data-testid="modal-conversation-history-rate-limit"] button',
        '[role="dialog"] button',
    )
    stop_button: str = '[data-testid="stop-button"]'
    message: str = "[data-message-author-role]"
    #: Links to conversations in a project's chat list. Used to find a
    #: chat by the request it contains when the address bar has not yet
    #: caught up — see `BrowserChatGPT.find_conversation_with`.
    conversation_link: str = 'a[href*="/c/"]'
    role_attr: str = "data-message-author-role"
    login_markers: tuple[str, ...] = (
        '[data-testid="login-button"]',
        '[data-testid="welcome-login-button"]',
    )
    logged_out_url_fragments: tuple[str, ...] = (
        "auth.openai.com",
        "/auth/login",
        "chatgpt.com/auth",
    )
    #: Markers that mean THIS conversation is broken (deleted, unavailable,
    #: failed to load its history) as opposed to the browser being unhappy in
    #: general. Only these — plus a loaded-but-composerless conversation page —
    #: authorize a rotation, so keep the list specific to the conversation.
    conversation_error_markers: tuple[str, ...] = (
        '[data-testid="conversation-not-found"]',
        '[data-testid="conversation-unavailable"]',
    )
    #: Composer of a not-yet-created chat inside a project. Present on the
    #: project page before any turn exists, which is where a rotation starts.
    new_chat_composer: str = "#prompt-textarea"
