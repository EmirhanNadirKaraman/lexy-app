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
