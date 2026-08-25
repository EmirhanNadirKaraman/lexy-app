"""LLMConversation provider registry: what is registered, what is not, and the
registration seam a future adapter arrives through — all without playwright
installed.

The claim this file exists to pin since brw-16 (2026-08-25): **no
`register_provider` call installs a browser-backed conversation provider.** The
registry, the protocol and the `browser_backed=True` declaration are UNCHANGED
and still work — that is the other half of the claim, and it is what conv-05's
Claude reviewer arrives through. One registration was removed; the mechanism
was not.
"""

import pytest

from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.conversation import (
    LLMConversation,
    SubmitResult,
    available_providers,
    browser_backed_providers,
    create_conversation,
    register_provider,
    transport_is_browser_backed,
)
from autoloop.errors import ConfigError
from autoloop.policy import PolicyConfig


def make_config(provider="codex_cli", tmp_path=None):
    from autoloop.config import ConversationConfig

    return AutoloopConfig(
        browser=BrowserConfig(),
        policy=PolicyConfig(),
        state_dir=tmp_path or "unused",
        conversation=ConversationConfig(provider=provider),
    )


# ---- the claim ---------------------------------------------------------------


def test_no_browser_provider_is_registered():
    """THE registration half of brw-16's claim, asserted on the registry itself
    rather than on a symptom of it."""
    assert available_providers() == ["codex_app_server", "codex_cli"]
    assert "browser_chatgpt" not in available_providers()


def test_nothing_registered_is_browser_backed():
    """The second half: no shipped provider claims a browser restart would
    recover it, so `orchestrator._handle_browser_failure` and the
    `browser.restart_command` / `restart_cooldown_seconds` /
    `policy.max_browser_restart_skips` settings describe nothing a real run can
    reach."""
    assert browser_backed_providers() == []
    for name in available_providers():
        assert not transport_is_browser_backed(name), name


def test_the_retired_browser_name_is_now_an_unknown_provider(tmp_path):
    """It is not special-cased anywhere in the registry: it fails exactly the
    way `gemini` does, with a message naming what IS available."""
    with pytest.raises(ConfigError) as excinfo:
        create_conversation("browser_chatgpt", make_config(tmp_path=tmp_path))
    message = str(excinfo.value)
    assert "unknown conversation provider 'browser_chatgpt'" in message
    assert "codex_cli" in message  # lists what IS available
    assert not transport_is_browser_backed("browser_chatgpt")


def test_unknown_provider_raises(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        create_conversation("gemini", make_config(tmp_path=tmp_path))
    assert "codex_cli" in str(excinfo.value)  # lists what IS available


# ---- the seam that must NOT have been weakened -------------------------------


class FakeConversation:
    def attach(self):
        pass

    def has_request(self, request_id):
        return False

    def reconcile(self, request_id):
        return False

    def submit(self, request_id, prompt):
        return SubmitResult.CONFIRMED

    def await_response(self, request_id):
        return ""

    def close(self):
        pass


def test_register_custom_provider(tmp_path):
    created = {}

    def factory(config):
        created["config"] = config
        return FakeConversation()

    register_provider("fake_for_test", factory)
    try:
        config = make_config(tmp_path=tmp_path)
        conversation = create_conversation("fake_for_test", config)
        assert isinstance(conversation, FakeConversation)
        assert created["config"] is config
    finally:
        from autoloop import conversation as conversation_module

        conversation_module._PROVIDERS.pop("fake_for_test", None)


def test_a_new_adapter_can_still_declare_itself_browser_backed(tmp_path):
    """conv-05's seam, and the bound on this whole change: removing the browser
    REGISTRATION must not remove the ability to register one. An adapter that
    declares itself gets every browser recovery the orchestrator has, with no
    edit to this module."""
    from autoloop import conversation as conversation_module

    name = "fake_browser_adapter_for_test"
    try:
        register_provider(name, lambda config: FakeConversation(), browser_backed=True)
        assert name in available_providers()
        assert transport_is_browser_backed(name)
        assert browser_backed_providers() == [name]
        assert isinstance(create_conversation(name, make_config(tmp_path=tmp_path)),
                          FakeConversation)
        # And silence still means "not a browser", in both directions.
        register_provider(name, lambda config: FakeConversation())
        assert not transport_is_browser_backed(name)
    finally:
        conversation_module._PROVIDERS.pop(name, None)
        conversation_module._BROWSER_BACKED.discard(name)
    # The registry is left exactly as this module found it.
    assert browser_backed_providers() == []


def test_protocol_is_not_runtime_checkable_dependency():
    # The interface is a typing Protocol — importing it must not require
    # playwright or any provider.
    assert LLMConversation is not None
