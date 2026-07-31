"""Abstract LLM-conversation interface + provider registry.

The orchestrator talks to "one persistent reviewer conversation" through this
interface only. `BrowserChatGPT` (Playwright over CDP against chatgpt.com) is
the first implementation; a future Claude.ai or Gemini adapter needs only a
class satisfying LLMConversation plus a `register_provider` call — no
orchestrator, policy, or state changes.

Contract every implementation must honor:

* `attach` makes the conversation usable and is cheap/idempotent. It may
  navigate only when there is no page on the conversation, because it is called
  before every phase — including polling phases where a reload would destroy an
  in-flight answer.
* `submit` returns a `SubmitResult`. It must not report CONFIRMED on optimistic
  UI alone: confirmation requires evidence the server accepted the turn.
  Ambiguity is reported as UNCONFIRMED, never as success and never as an
  implicit retry.
* `reconcile` is the authority on what actually persisted (a controlled
  reload), and the only way an UNCONFIRMED submission may be resolved.
* `await_response` returns only a COMPLETED reply to the given request —
  never a stale one, never a partial one — and never navigates.
* Errors raise the shared BrowserError hierarchy (`errors.py`) so the
  orchestrator's failure routing stays provider-agnostic.

Two capabilities are OPTIONAL and probed with `getattr`, so an adapter that
implements only the protocol above stays valid:

* **Send observation** — `submit` may return `SubmitResult.REJECTED` when the
  provider can positively disprove acceptance (see `browser/observation.py`). An
  adapter that cannot returns UNCONFIRMED for the same situation, which is the
  historical behaviour: ambiguity, park for a human.
* **Rotation** — `retarget(url)` + `current_url()` let the orchestrator move an
  in-flight request to a replacement conversation. Without them, a wedged
  conversation parks instead of rotating.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Protocol

from .browser.chatgpt import SubmitResult
from .errors import ConfigError

if TYPE_CHECKING:
    from .config import AutoloopConfig

__all__ = [
    "LLMConversation",
    "SubmitResult",
    "available_providers",
    "create_conversation",
    "register_provider",
]


class LLMConversation(Protocol):
    def attach(self) -> None:
        """Make the one persistent conversation usable. Navigates only if
        needed. Idempotent."""
        ...

    def has_request(self, request_id: str) -> bool:
        """True if this request id appears in the currently loaded history."""
        ...

    def submit(self, request_id: str, prompt: str) -> SubmitResult: ...

    def reconcile(self, request_id: str) -> bool:
        """Controlled reload; True if the request is in persisted history."""
        ...

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
        response_start_timeout=config.browser.response_start_timeout_seconds,
        submit_timeout=config.browser.submit_timeout_seconds,
        composer_timeout=config.browser.composer_timeout_seconds,
        input_sync_timeout=config.browser.input_sync_timeout_seconds,
        send_ready_timeout=config.browser.send_ready_timeout_seconds,
        reconcile_timeout=config.browser.reconcile_timeout_seconds,
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
