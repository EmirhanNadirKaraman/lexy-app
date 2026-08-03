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
