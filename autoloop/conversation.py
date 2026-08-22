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
* **Chunked delivery** — `supports_chunked_delivery` declares that this adapter
  holds ONE persistent conversation, so a review packet whose diff is too large
  for a single message may be deposited as numbered parts before the message
  that asks for a verdict. An adapter that does not declare it gets the
  historical behaviour: the diff is omitted with a notice saying so. The
  declaration is a claim about shared history, not about size limits — a
  transport whose every turn is a separate process (`codex.conversation`) must
  not set it, because parts sent to it would be reviewed as separate fragments.
  `codex.app_server_conversation` DOES set it, and the difference is exactly
  that: it holds one thread, every turn goes onto it, and `thread/read` reads
  the parts and the question back as one context.
* **Mounting the message tail** — `mount_message_tail()` scrolls older turns
  into a virtualized message list, so a readback does not conclude a message is
  absent when it is merely unmounted. Optional and best-effort: its failures
  are swallowed, and an adapter without it simply reads what is rendered.
"""

from __future__ import annotations

from pathlib import Path
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

    def submit(
        self, request_id: str, prompt: str, attachment: str | None = None
    ) -> SubmitResult:
        """Send one prompt, optionally uploading `attachment` first.

        A provider that cannot attach must raise rather than drop the file: a
        review packet delivered without its diff would be approved unseen.
        """
        ...

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

    # Pass the conversation so the session binds to THAT tab rather than
    # whatever ChatGPT page is open first — the profile collects strays
    # (a chat a failed rotation left behind), and adopting one attaches
    # the loop to the wrong conversation.
    session = PlaywrightSession.connect(
        config.browser.cdp_url, conversation_url=config.browser.conversation_url
    )
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


def _codex_cli_factory(config: "AutoloopConfig") -> LLMConversation:
    # Lazy import for symmetry with the browser factory — nothing here depends
    # on the codex binary existing until a run actually selects this provider.
    from .codex.conversation import CodexConversation, SubprocessCodexRunner
    from .codex.quota import DEFAULT_QUOTA_PATTERNS

    codex = config.codex
    runner = SubprocessCodexRunner(
        command=codex.command,
        sandbox_args=codex.sandbox_args,
        timeout_seconds=codex.timeout_seconds,
        cwd=Path(codex.working_dir) if codex.working_dir else None,
    )
    return CodexConversation(
        runner,
        quota_patterns=codex.quota_patterns or DEFAULT_QUOTA_PATTERNS,
    )


def _codex_app_server_factory(config: "AutoloopConfig") -> LLMConversation:
    # Lazy, like the other two. Nothing here needs the codex binary to exist
    # until a run selects this provider and `attach()` launches it.
    from .codex.app_server import AppServerClient, SubprocessAppServer
    from .codex.app_server_conversation import CodexAppServerConversation
    from .codex.protocol_errors import DEFAULT_QUOTA_ERROR_CODES

    codex = config.codex
    transport = SubprocessAppServer(
        command=codex.app_server_command,
        # Same containment the subprocess adapter states: outside the checkout,
        # so the reviewer has no filesystem business here at all. Not a sandbox
        # claim — see `codex/app_server.py`.
        cwd=Path(codex.working_dir) if codex.working_dir else None,
    )
    client = AppServerClient(
        transport,
        timeout_seconds=codex.timeout_seconds,
        quota_codes=codex.quota_error_codes or DEFAULT_QUOTA_ERROR_CODES,
        working_dir=codex.working_dir,
    )
    return CodexAppServerConversation(
        client,
        part_chars=codex.app_server_part_chars,
        max_attachment_chars=codex.app_server_max_attachment_chars,
    )


_PROVIDERS: dict[str, ConversationFactory] = {
    "browser_chatgpt": _browser_chatgpt_factory,
    # Two codex transports, both selectable, neither replacing the other. The
    # subprocess one is unchanged and stays the default codex seat;
    # `codex_app_server` is the one that can chunk, because it has a thread.
    # `conversation.fallback_provider` may still name any of the three.
    "codex_cli": _codex_cli_factory,
    "codex_app_server": _codex_app_server_factory,
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
