"""LLMConversation provider registry: lookup, registration of new adapters,
and the browser implementation satisfying the interface — all without
playwright installed."""

import pytest

from autoloop.browser.chatgpt import BrowserChatGPT
from autoloop.config import AutoloopConfig, BrowserConfig
from autoloop.conversation import (
    LLMConversation,
    available_providers,
    create_conversation,
    register_provider,
)
from autoloop.errors import ConfigError
from autoloop.policy import PolicyConfig


def make_config(provider="browser_chatgpt", tmp_path=None):
    from autoloop.config import ConversationConfig

    return AutoloopConfig(
        browser=BrowserConfig(conversation_url="https://chatgpt.com/c/x"),
        policy=PolicyConfig(),
        state_dir=tmp_path or "unused",
        conversation=ConversationConfig(provider=provider),
    )


def test_browser_chatgpt_is_registered():
    assert "browser_chatgpt" in available_providers()


def test_unknown_provider_raises(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        create_conversation("gemini", make_config(tmp_path=tmp_path))
    assert "browser_chatgpt" in str(excinfo.value)  # lists what IS available


def test_register_custom_provider(tmp_path):
    created = {}

    class FakeConversation:
        def open(self):
            pass

        def already_submitted(self, request_id):
            return False

        def submit(self, request_id, prompt):
            pass

        def await_response(self, request_id):
            return ""

        def close(self):
            pass

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


def test_browser_chatgpt_satisfies_interface():
    # Structural check without instantiating a browser: every protocol method
    # exists and is callable on the class.
    for method in ("open", "already_submitted", "submit", "await_response", "close"):
        assert callable(getattr(BrowserChatGPT, method)), method


def test_protocol_is_not_runtime_checkable_dependency():
    # The interface is a typing Protocol — importing it must not require
    # playwright or any provider.
    assert LLMConversation is not None
