"""Abstract LLM-conversation interface + provider registry.

The orchestrator talks to "one persistent reviewer conversation" through this
interface only. `BrowserChatGPT` (Playwright over CDP against chatgpt.com) is
the first implementation; a future Claude.ai or Gemini adapter needs only a
class satisfying LLMConversation plus a `register_provider` call — no
orchestrator, policy, or state changes.

Contract every implementation must honor:

* `submit` embeds nothing — the prompt already carries the request-id; the
  implementation must make `already_submitted(request_id)` reflect the real
  conversation (it is the crash-safe duplicate guard).
* `await_response` returns only a COMPLETED reply to the given request —
  never a stale or still-streaming one.
* Errors raise the shared BrowserError hierarchy (`errors.py`) so the
  orchestrator's failure routing stays provider-agnostic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Protocol

from .errors import ConfigError

if TYPE_CHECKING:
    from .config import AutoloopConfig


class LLMConversation(Protocol):
    def open(self) -> None:
        """Open / focus the one persistent conversation. Idempotent."""
        ...

    def already_submitted(self, request_id: str) -> bool:
        """True if a request carrying this id already exists in the conversation."""
        ...

    def submit(self, request_id: str, prompt: str) -> None: ...

    def await_response(self, request_id: str) -> str: ...

    def close(self) -> None: ...


ConversationFactory = Callable[["AutoloopConfig"], LLMConversation]


def _browser_chatgpt_factory(config: "AutoloopConfig") -> LLMConversation:
    # Lazy imports: playwright only exists in live deployments.
    from .browser.chatgpt import BrowserChatGPT
    from .browser.playwright_session import PlaywrightSession

    session = PlaywrightSession.connect(config.browser.cdp_url)
    return BrowserChatGPT(
        session,
        config.browser.conversation_url,
        response_timeout=config.browser.response_timeout_seconds,
        submit_timeout=config.browser.submit_timeout_seconds,
        poll_interval=config.browser.poll_interval_seconds,
        stability_seconds=config.browser.stability_seconds,
        diagnostics_dir=config.diagnostics_dir,
    )


_PROVIDERS: dict[str, ConversationFactory] = {
    "browser_chatgpt": _browser_chatgpt_factory,
}


def register_provider(name: str, factory: ConversationFactory) -> None:
    _PROVIDERS[name] = factory


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def create_conversation(provider: str, config: "AutoloopConfig") -> LLMConversation:
    factory = _PROVIDERS.get(provider)
    if factory is None:
        raise ConfigError(
            f"unknown conversation provider '{provider}' — available: "
            f"{', '.join(available_providers())}"
        )
    return factory(config)
