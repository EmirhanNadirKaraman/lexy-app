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
* **Idempotent submit** — `idempotent_submit` declares that a FAILED `submit`
  appended nothing to any durable conversation, so re-issuing the same request
  id cannot double-post. It is the ONLY licence the orchestrator has to re-run
  an invocation on its own account (`Orchestrator._replay_unrecoverable_await`
  and the `submitting` ambiguity branch). A transport that does not set it is
  never re-run automatically.

**Which faults are recovered by restarting a browser is a property of the
TRANSPORT, not of the exception type** — see `transport_is_browser_backed`
below. Every transport fault arrives as a `BrowserError` subclass (the routing
in `orchestrator.run` is by type, and `errors.py` names the hierarchy after the
first implementation), so the type alone cannot say whether launching Chrome is
a recovery or a category error. On 2026-08-22 a `codex_cli` run answered a
`ResponseTimeoutError` from a SUBPROCESS by starting Chrome on the browser
profile, spending the browser fault budget, and parking the loop with advice to
restart a browser it does not use.
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
    "browser_backed_providers",
    "create_conversation",
    "register_provider",
    "transport_is_browser_backed",
    "transport_remedy",
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


def _transcript_log(config: "AutoloopConfig") -> Callable[[str, dict], None]:
    """The failure logger every PRODUCTION codex adapter is built with.

    The codex adapters all take `log=` and all default it to a no-op. That
    default is right for a unit test that only wants a return value; it was
    catastrophic in production, because the factories constructed them without
    passing anything. `CodexConversation.submit` has always written
    `codex_invocation_failed` on every non-zero exit, with a comment saying it
    "turns the first real exhaustion into a one-line config fix instead of an
    investigation" — and across a 24-day transcript that record appeared ZERO
    times. When the loop then parked twice on a false exhaustion (2026-08-22),
    the exit code and stderr that would have named the real fault had already
    been thrown away, so the investigation had to start from the account API.

    Fixed HERE, at the construction site, rather than by removing the no-op
    default: a default that keeps tests hermetic is reasonable, and a
    production adapter built without a logger is the bug.

    Deliberately not wrapped in `try`. `orchestrator._log` does not guard its
    writes either, and a transcript this cannot write to is a real fault that
    should be visible rather than a record that quietly does not exist — which
    is the shape of the failure this whole function is undoing.
    """
    from .transcript import TranscriptLogger

    logger = TranscriptLogger(config.transcript_file)

    def log(event: str, data: dict) -> None:
        logger.append(event, data=data)

    return log


def _codex_cli_factory(config: "AutoloopConfig") -> LLMConversation:
    # Lazy import for symmetry with the browser factory — nothing here depends
    # on the codex binary existing until a run actually selects this provider.
    from .codex.conversation import CodexConversation, SubprocessCodexRunner
    from .codex.quota import DEFAULT_QUOTA_PATTERNS, DEFAULT_RATE_LIMIT_PATTERNS

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
        # Two lists, because a short-window throttle and a spent weekly
        # allowance are different events with different remedies, and only the
        # second one is worth parking a loop over.
        rate_limit_patterns=codex.rate_limit_patterns or DEFAULT_RATE_LIMIT_PATTERNS,
        log=_transcript_log(config),
    )


def _codex_app_server_factory(config: "AutoloopConfig") -> LLMConversation:
    # Lazy, like the other two. Nothing here needs the codex binary to exist
    # until a run selects this provider and `attach()` launches it.
    from .codex.app_server import AppServerClient, SubprocessAppServer
    from .codex.app_server_conversation import CodexAppServerConversation
    from .codex.protocol_errors import (
        DEFAULT_QUOTA_ERROR_CODES,
        DEFAULT_RATE_LIMIT_ERROR_CODES,
    )

    codex = config.codex
    transport = SubprocessAppServer(
        command=codex.app_server_command,
        # Same containment the subprocess adapter states: outside the checkout,
        # so the reviewer has no filesystem business here at all. Not a sandbox
        # claim — see `codex/app_server.py`.
        cwd=Path(codex.working_dir) if codex.working_dir else None,
    )
    # Same wiring as the subprocess seat, and for the same reason: this
    # transport's own `codex_app_server_failed` digest never reached the
    # transcript either, because both objects were constructed without a
    # logger. See `_transcript_log`.
    log = _transcript_log(config)
    client = AppServerClient(
        transport,
        timeout_seconds=codex.timeout_seconds,
        quota_codes=codex.quota_error_codes or DEFAULT_QUOTA_ERROR_CODES,
        # Two vocabularies here for the same reason the subprocess seat above
        # takes two pattern lists: a short-window throttle and a spent weekly
        # allowance are different events, and only the second is worth parking
        # a loop over.
        rate_limit_codes=(
            codex.rate_limit_error_codes or DEFAULT_RATE_LIMIT_ERROR_CODES
        ),
        working_dir=codex.working_dir,
        log=log,
    )
    return CodexAppServerConversation(
        client,
        part_chars=codex.app_server_part_chars,
        max_attachment_chars=codex.app_server_max_attachment_chars,
        log=log,
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

#: Providers whose faults a BROWSER RESTART can actually recover — the ones for
#: which `orchestrator._handle_browser_failure`, `browser.restart_command`,
#: `browser.restart_cooldown_seconds` and `policy.max_browser_restart_skips`
#: describe anything at all. Membership is what the orchestrator asks before it
#: launches Chrome, charges the browser fault budget, or writes a park naming
#: the browser.
#:
#: **Keyed by the provider NAME, not by an attribute on the client object, and
#: that is the load-bearing choice.** A `getattr(client, ...)` probe fails OPEN
#: in the exact shape this exists to prevent: the failure handlers run with the
#: client already dropped, and a transport whose FACTORY raised never produced
#: an object to ask. Both answer "no client" and would default to the historical
#: browser behaviour — which is starting Chrome for a subprocess fault.
#: `active_provider()` answers with nothing held at all.
#:
#: **An unknown name is NOT browser-backed**, which is the fail-closed
#: direction: a provider registered by a future adapter gets the transport-
#: generic recovery (retry on the ordinary failure budget, park naming the
#: provider) rather than a browser launch nobody asked for. The cost is real and
#: is stated rather than hidden: a THIRD-PARTY Playwright adapter registered
#: under some other name silently loses auto-restart until it declares itself
#: with `register_provider(..., browser_backed=True)`. That is a lost recovery,
#: which shows up as retries and a park; the other direction is an automation
#: killing and relaunching a browser a run never used.
_BROWSER_BACKED: set[str] = {"browser_chatgpt"}


def register_provider(
    name: str, factory: ConversationFactory, *, browser_backed: bool = False
) -> None:
    """Register `name` as a selectable `conversation.provider`.

    `browser_backed=True` additionally declares that this transport's faults are
    recovered by `browser.restart_command` — see `_BROWSER_BACKED`. Default
    False: an adapter that says nothing is treated as not-a-browser, so the
    orchestrator never restarts Chrome on its behalf.
    """
    _PROVIDERS[name] = factory
    if browser_backed:
        _BROWSER_BACKED.add(name)
    else:
        _BROWSER_BACKED.discard(name)


def available_providers() -> list[str]:
    return sorted(_PROVIDERS)


def browser_backed_providers() -> list[str]:
    return sorted(_BROWSER_BACKED)


def transport_is_browser_backed(provider: str) -> bool:
    """Would restarting the browser be a recovery for this provider's faults?

    False for every name this module does not positively know to drive a
    browser, including the empty string and a name that was never registered —
    see `_BROWSER_BACKED` for why the unknown case answers this way.
    """
    return provider in _BROWSER_BACKED


#: What an operator can actually DO about a fault from each NON-browser
#: transport, quoted into the park `orchestrator._handle_transport_failure`
#: writes. Here rather than in the orchestrator because that module is held
#: provider-agnostic by test (`test_codex_app_server.py::test_the_orchestrator_
#: policy_and_state_modules_stay_provider_agnostic`), and this one is the
#: registry: knowing a provider by name is its job.
#:
#: This exists because the alternative is what shipped. On 2026-08-22 a
#: `codex_cli` run parked advising the operator to "restart the browser by hand
#: (python3 -m autoloop.browser.chrome_restart)" or to lower
#: `browser.restart_cooldown_seconds`. Neither can repair a subprocess fault,
#: and advice naming a subsystem the run does not use is worse than no advice:
#: it sends the investigation somewhere else, which is where that one went.
_TRANSPORT_REMEDIES: dict[str, str] = {
    "codex_cli": (
        "Run codex.command (default `codex exec`) by hand from a shell and see "
        "what it says; `codex login` if it refuses. This run's own "
        "codex_invocation_failed transcript records carry the exit code and the "
        "stderr tail for every failed invocation. If the invocations are being "
        "killed at the deadline, raise codex.timeout_seconds. To keep going on "
        "the other transport instead, set conversation.fallback_provider. No "
        "browser is involved in this run: restarting one changes nothing."
    ),
    "codex_app_server": (
        "Run codex.app_server_command by hand from a shell and see what it "
        "says; `codex login` if it refuses. This run's own "
        "codex_app_server_failed transcript records carry the protocol error "
        "that ended each turn. If turns are being cut off at the deadline, "
        "raise codex.timeout_seconds. To keep going on the other transport "
        "instead, set conversation.fallback_provider. No browser is involved in "
        "this run: restarting one changes nothing."
    ),
}


def transport_remedy(provider: str) -> str:
    """Operator-actionable advice for a fault from `provider`.

    Never mentions restarting a browser, because the only caller is the
    non-browser recovery path. A provider with no entry gets the generic
    branch rather than silence — an unnamed transport with no advice would
    recreate the original fault in a new place.
    """
    remedy = _TRANSPORT_REMEDIES.get(provider)
    if remedy:
        return remedy
    return (
        f"Check the transport named by conversation.provider ({provider!r}) "
        "and whatever records it writes to this run's transcript. No browser is "
        "involved in this run, so restarting one changes nothing. To keep going "
        "on a different transport, set conversation.fallback_provider."
    )


def create_conversation(provider: str, config: "AutoloopConfig") -> LLMConversation:
    factory = _PROVIDERS.get(provider)
    if factory is None:
        raise ConfigError(
            f"unknown conversation provider '{provider}' — available: "
            f"{', '.join(available_providers())}"
        )
    return factory(config)
