"""
Provider seam tests — services/llm_provider.py.

The seam's whole promise is that routing calls through it did NOT change what
gets sent to Anthropic. The load-bearing test here is
`test_rebuilt_tool_dicts_are_byte_identical_to_pre_seam`: it reconstructs the
tool definition from every schema constant and compares it against the tool
dict those call sites sent before the refactor, read out of git HEAD~ if
available. A `title` or `description` leaking into `input_schema` would be
accepted by the API without error while silently changing the prompt — that
assertion is what rules it out.

These are pure unit tests: no DB, no network, no API key needed.
"""
import json
import subprocess

import pytest

from backend.services import (
    book_llm_service,
    llm_provider,
    llm_service,
    reading_llm_service,
)


# ---------------------------------------------------------------------------
# split_schema
# ---------------------------------------------------------------------------


def test_split_schema_extracts_name_and_description():
    name, description, body = llm_provider.split_schema({
        "title": "do_thing",
        "description": "Does the thing.",
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    })

    assert name == "do_thing"
    assert description == "Does the thing."
    assert body == {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x"],
    }


def test_split_schema_strips_title_and_description_from_body():
    """The pre-seam `input_schema` dicts carried neither key. Leaving them in
    would change the bytes on the wire without any error surfacing."""
    _, _, body = llm_provider.split_schema({
        "title": "t", "description": "d", "type": "object", "properties": {},
    })

    assert "title" not in body
    assert "description" not in body


def test_split_schema_without_description_yields_empty_string():
    name, description, _ = llm_provider.split_schema({"title": "t", "type": "object"})
    assert (name, description) == ("t", "")


def test_split_schema_without_title_raises_provider_error():
    with pytest.raises(llm_provider.LLMProviderError, match="title"):
        llm_provider.split_schema({"type": "object"})


# ---------------------------------------------------------------------------
# AnthropicProvider — request shape
# ---------------------------------------------------------------------------


class _RecordingMessages:
    def __init__(self, blocks):
        self.blocks = blocks
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return type("_Resp", (), {"content": self.blocks})()


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, payload):
        self.input = payload


class _TextBlock:
    type = "text"
    text = "not a tool call"


def _provider_with(blocks, model="test-model"):
    provider = llm_provider.AnthropicProvider(api_key="test-key", model=model)
    recorder = _RecordingMessages(blocks)
    provider._client = type("_C", (), {"messages": recorder})()
    return provider, recorder


SAMPLE_SCHEMA = {
    "title": "sample_tool",
    "description": "A sample.",
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}


async def test_translates_schema_into_forced_single_tool_call():
    provider, recorder = _provider_with([_ToolUseBlock({"answer": "ok"})])

    await provider.structured(
        system="sys",
        messages=[{"role": "user", "content": "hi"}],
        schema=SAMPLE_SCHEMA,
        max_tokens=123,
    )

    sent = recorder.kwargs
    assert sent["model"] == "test-model"
    assert sent["max_tokens"] == 123
    assert sent["system"] == "sys"
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    # Exactly one tool, forced.
    assert sent["tools"] == [{
        "name": "sample_tool",
        "description": "A sample.",
        "input_schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    }]
    assert sent["tool_choice"] == {"type": "tool", "name": "sample_tool"}


async def test_extracts_the_tool_input_dict():
    provider, _ = _provider_with([_ToolUseBlock({"answer": "42", "extra": 1})])

    result = await provider.structured(
        system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
    )

    assert result == {"answer": "42", "extra": 1}


async def test_ignores_leading_text_blocks_and_finds_the_tool_use():
    provider, _ = _provider_with([_TextBlock(), _ToolUseBlock({"answer": "found"})])

    result = await provider.structured(
        system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
    )

    assert result == {"answer": "found"}


async def test_missing_tool_use_raises_clear_provider_error():
    """Pre-seam this was a bare `StopIteration` from a generator expression,
    which surfaces from a coroutine as an opaque RuntimeError naming nothing."""
    provider, _ = _provider_with([_TextBlock()])

    with pytest.raises(llm_provider.LLMProviderError) as exc:
        await provider.structured(
            system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
        )

    assert "sample_tool" in str(exc.value)
    assert "test-model" in str(exc.value)


async def test_empty_content_raises_provider_error():
    provider, _ = _provider_with([])

    with pytest.raises(llm_provider.LLMProviderError):
        await provider.structured(
            system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
        )


# ---------------------------------------------------------------------------
# model_id
# ---------------------------------------------------------------------------


def test_model_id_defaults_to_the_pre_seam_literal():
    """Every service hardcoded this exact string before the seam existed."""
    assert llm_provider.DEFAULT_ANTHROPIC_MODEL == "claude-haiku-4-5-20251001"
    assert llm_provider.AnthropicProvider(api_key="k").model_id == (
        "claude-haiku-4-5-20251001"
    )


def test_llm_model_env_var_overrides_the_default(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "some-other-model")
    assert llm_provider.AnthropicProvider(api_key="k").model_id == "some-other-model"


def test_explicit_model_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "from-env")
    assert llm_provider.AnthropicProvider(api_key="k", model="explicit").model_id == (
        "explicit"
    )


@pytest.mark.parametrize(
    "service",
    [llm_service, reading_llm_service, book_llm_service],
    ids=["llm_service", "reading_llm_service", "book_llm_service"],
)
def test_services_expose_a_provider_and_no_raw_client(service):
    """All three used to build their own `AsyncAnthropic`. The seam is only
    real if none of them still does."""
    assert hasattr(service, "_provider")
    assert isinstance(service._provider.model_id, str)
    assert not hasattr(service, "_client")
    assert not hasattr(service, "_MODEL")


@pytest.mark.parametrize(
    "call,expected_prompt_key",
    [
        (
            lambda pool: llm_service.get_examples_if_cached(
                "Haus", "word", "de", pool=pool,
            ),
            "prep_examples",
        ),
        (
            lambda pool: llm_service.get_grammar_explanation_if_cached(
                "some-slug", "de", pool=pool,
            ),
            "grammar_rule_explanation",
        ),
    ],
    ids=["get_examples_if_cached", "get_grammar_explanation_if_cached"],
)
async def test_readonly_cache_lookups_key_on_provider_model_id(
    monkeypatch, call, expected_prompt_key,
):
    """These two build cache keys without ever calling the LLM.

    They are the easiest sites to miss in a model-literal sweep (grep for
    `messages.create` doesn't find them). If they key on a stale literal while
    the write paths key on `provider.model_id`, reads and writes diverge and
    every lookup silently misses — a pure cache-cost regression with no error.
    """
    from backend.services import llm_cache_service

    seen_models = []
    real_make_key = llm_cache_service.make_cache_key

    def _spy(prompt_key, model, params):
        seen_models.append((prompt_key, model))
        return real_make_key(prompt_key, model, params)

    monkeypatch.setattr(llm_cache_service, "make_cache_key", _spy)
    monkeypatch.setattr(llm_cache_service, "get_cached", _AsyncNone())

    class _SentinelProvider:
        model_id = "sentinel-model"

    monkeypatch.setattr(llm_service, "_provider", _SentinelProvider())

    await call(object())  # any non-None pool; get_cached is stubbed

    assert seen_models == [(expected_prompt_key, "sentinel-model")]


class _AsyncNone:
    """Stand-in for `llm_cache_service.get_cached` that always misses."""

    async def __call__(self, *_args, **_kwargs):
        return None


# ---------------------------------------------------------------------------
# Byte-identity against the pre-seam tool definitions
# ---------------------------------------------------------------------------


#: Last revision whose service modules still constructed `AsyncAnthropic`
#: directly — i.e. the state these tests diff against. Pinned rather than
#: discovered: the original sliding `HEAD~n` window silently aged out as
#: unrelated commits landed, and 14 byte-identity checks degraded to skips
#: without anyone noticing. A pin cannot drift.
PRE_SEAM_REV = "9977a4e"

#: Tried before the pin so the tests still work while the refactor is
#: uncommitted or has just been amended/rebased (the SHA changes then).
_PRE_SEAM_SEARCH = ("HEAD", "HEAD~1", "HEAD~2", PRE_SEAM_REV)


def _pre_seam_module(path: str, modname: str):
    """Load a service module as it existed before the seam, from git.

    Returns None when no candidate revision is reachable — a shallow clone
    that lacks `PRE_SEAM_REV`, or a history rewrite that dropped it. The test
    skips rather than failing on repo shape, but that path is now genuinely
    exceptional rather than the normal outcome after a few commits.
    """
    import types

    for rev in _PRE_SEAM_SEARCH:
        try:
            src = subprocess.check_output(
                ["git", "show", f"{rev}:{path}"],
                text=True, stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            continue
        if "AsyncAnthropic(" not in src:
            continue  # already post-seam at this rev
        lines = [
            ln for ln in src.split("\n")
            if not ln.startswith("from . import") and "AsyncAnthropic(" not in ln
        ]
        mod = types.ModuleType(modname)
        exec(compile("\n".join(lines), path, "exec"), mod.__dict__)
        return mod
    return None


_BASE = "lexy-app/backend/services"

_CONSTANT_PAIRS = [
    (f"{_BASE}/llm_service.py", "_GUIDED_OPEN_TOOL", "_GUIDED_OPEN_SCHEMA", llm_service),
    (f"{_BASE}/llm_service.py", "_GUIDED_EVAL_TOOL", "_GUIDED_EVAL_SCHEMA", llm_service),
    (f"{_BASE}/llm_service.py", "_PREP_INFO_TOOL", "_PREP_INFO_SCHEMA", llm_service),
    (f"{_BASE}/llm_service.py", "_PREP_EXAMPLES_TOOL", "_PREP_EXAMPLES_SCHEMA", llm_service),
    (f"{_BASE}/llm_service.py", "_GUIDED_SUMMARY_TOOL", "_GUIDED_SUMMARY_SCHEMA", llm_service),
    (f"{_BASE}/llm_service.py", "_GRAMMAR_EXPLAIN_TOOL", "_GRAMMAR_EXPLAIN_SCHEMA", llm_service),
    (f"{_BASE}/llm_service.py", "_PRODUCTION_EVAL_TOOL", "_PRODUCTION_EVAL_SCHEMA", llm_service),
    (f"{_BASE}/llm_service.py", "_GLOSS_TOOL", "_GLOSS_SCHEMA", llm_service),
    (f"{_BASE}/reading_llm_service.py", "_TRANSLATE_TOOL", "_TRANSLATE_SCHEMA", reading_llm_service),
    (f"{_BASE}/reading_llm_service.py", "_EXPLAIN_TOOL", "_EXPLAIN_SCHEMA", reading_llm_service),
    (f"{_BASE}/book_llm_service.py", "_FIX_TOOL", "_FIX_SCHEMA", book_llm_service),
]


@pytest.mark.parametrize(
    "path,old_name,new_name,module",
    _CONSTANT_PAIRS,
    ids=[f"{p.rsplit('/', 1)[-1]}:{o}" for p, o, _, _ in _CONSTANT_PAIRS],
)
def test_rebuilt_tool_dicts_are_byte_identical_to_pre_seam(path, old_name, new_name, module):
    old = _pre_seam_module(path, f"pre_seam_{old_name}")
    if old is None or not hasattr(old, old_name):
        pytest.skip(
            f"pre-seam revision not reachable (tried {', '.join(_PRE_SEAM_SEARCH)}) "
            "— shallow clone or rewritten history"
        )

    name, description, body = llm_provider.split_schema(getattr(module, new_name))
    rebuilt = {"name": name, "description": description, "input_schema": body}

    assert rebuilt == getattr(old, old_name)


@pytest.mark.parametrize("language", ["de", "es", "en"])
def test_rebuilt_language_factory_tools_are_byte_identical(language):
    """`_make_eval_schema` / `_make_guided_hints_schema` build their schema per
    call, so the `en` special-case in the `language_detected` enum has to
    survive the rewrite too."""
    old = _pre_seam_module(f"{_BASE}/llm_service.py", "pre_seam_factories")
    if old is None or not hasattr(old, "_make_eval_tool"):
        pytest.skip(
            f"pre-seam revision not reachable (tried {', '.join(_PRE_SEAM_SEARCH)}) "
            "— shallow clone or rewritten history"
        )

    for old_fn, new_fn in (
        (old._make_eval_tool, llm_service._make_eval_schema),
        (old._make_guided_hints_tool, llm_service._make_guided_hints_schema),
    ):
        name, description, body = llm_provider.split_schema(new_fn(language))
        rebuilt = {"name": name, "description": description, "input_schema": body}
        assert rebuilt == old_fn(language)


# ---------------------------------------------------------------------------
# Provider selection (LLM_PROVIDER)
# ---------------------------------------------------------------------------


def test_default_provider_is_anthropic_with_no_env_set(monkeypatch):
    """The whole back-compat promise: an existing deployment that sets none of
    the new vars keeps the exact provider it had."""
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    provider = llm_provider.get_provider()

    assert isinstance(provider, llm_provider.AnthropicProvider)
    assert provider.model_id == llm_provider.DEFAULT_ANTHROPIC_MODEL


@pytest.mark.parametrize("value", ["anthropic", "Anthropic", "  ANTHROPIC  "])
def test_llm_provider_anthropic_selects_anthropic(monkeypatch, value):
    monkeypatch.setenv("LLM_PROVIDER", value)
    assert isinstance(llm_provider.get_provider(), llm_provider.AnthropicProvider)


def test_llm_provider_openai_compatible_selects_openai_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "http://desktop-name:11434/v1")
    monkeypatch.setenv("LLM_MODEL", "gemma3:12b")

    provider = llm_provider.get_provider()

    assert isinstance(provider, llm_provider.OpenAICompatibleProvider)
    assert provider.model_id == "gemma3:12b"


def test_openai_compatible_without_base_url_fails_clearly(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_MODEL", "gemma3:12b")

    with pytest.raises(llm_provider.LLMProviderError, match="LLM_BASE_URL"):
        llm_provider.get_provider()


def test_openai_compatible_without_model_fails_clearly(monkeypatch):
    """No default model name is meaningful across runtimes, and an empty one
    surfaces as an opaque 404 from the server."""
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_BASE_URL", "http://desktop-name:11434/v1")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    with pytest.raises(llm_provider.LLMProviderError, match="LLM_MODEL"):
        llm_provider.get_provider()


def test_unknown_llm_provider_value_fails_clearly(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")

    with pytest.raises(llm_provider.LLMProviderError) as exc:
        llm_provider.get_provider()

    assert "ollama" in str(exc.value)
    assert "anthropic" in str(exc.value)
    assert "openai_compatible" in str(exc.value)


def test_bad_timeout_value_fails_clearly(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "soon")

    with pytest.raises(llm_provider.LLMProviderError, match="LLM_TIMEOUT_SECONDS"):
        llm_provider.OpenAICompatibleProvider(
            base_url="http://host:11434/v1", model="m",
        )


# ---------------------------------------------------------------------------
# OpenAI-compatible provider — request shape
# ---------------------------------------------------------------------------


def _oai(**overrides):
    kwargs = {
        "base_url": "http://desktop-tailscale-name:11434/v1",
        "model": "gemma3:12b",
        "api_key": "test-secret-key",
        "timeout": 5.0,
    }
    kwargs.update(overrides)
    return llm_provider.OpenAICompatibleProvider(**kwargs)


class _FakeHTTPResponse:
    def __init__(self, payload=None, status_code=200, text=None, raw=None):
        self.status_code = status_code
        self._payload = payload
        self._raw = raw
        self.text = text if text is not None else "<body>"

    def json(self):
        if self._raw is not None:
            raise ValueError("not json")
        return self._payload


class _CapturingClient:
    """Stands in for `httpx.AsyncClient` as an async context manager."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, **kwargs):
        self.init_kwargs = kwargs
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0)


def _ok(payload_obj):
    return _FakeHTTPResponse({
        "choices": [{"message": {"content": json.dumps(payload_obj)}}]
    })


async def test_openai_request_targets_chat_completions_under_base_url(monkeypatch):
    client = _CapturingClient([_ok({"answer": "ok"})])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    await _oai().structured(
        system="be helpful",
        messages=[{"role": "user", "content": "hi"}],
        schema=SAMPLE_SCHEMA,
        max_tokens=99,
    )

    sent = client.calls[0]
    assert sent["url"] == (
        "http://desktop-tailscale-name:11434/v1/chat/completions"
    )


@pytest.mark.parametrize(
    "base_url",
    [
        "http://desktop-tailscale-name:11434/v1",
        "http://100.101.102.103:11434/v1",
        "http://desktop-name:8080/v1",
        "http://desktop-name:11434/v1/",  # trailing slash tolerated
    ],
)
async def test_remote_base_urls_are_supported_not_just_localhost(monkeypatch, base_url):
    client = _CapturingClient([_ok({"answer": "ok"})])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    await _oai(base_url=base_url).structured(
        system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
    )

    assert client.calls[0]["url"] == base_url.rstrip("/") + "/chat/completions"


async def test_openai_request_body_shape(monkeypatch):
    client = _CapturingClient([_ok({"answer": "ok"})])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    await _oai().structured(
        system="be helpful",
        messages=[
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ],
        schema=SAMPLE_SCHEMA,
        max_tokens=99,
    )

    body = client.calls[0]["json"]
    assert body["model"] == "gemma3:12b"
    assert body["max_tokens"] == 99
    # System first, then the caller's messages in order.
    assert body["messages"] == [
        {"role": "system", "content": "be helpful"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    # JSON Schema response format, with title/description lifted out of the body.
    assert body["response_format"]["type"] == "json_schema"
    js = body["response_format"]["json_schema"]
    assert js["name"] == "sample_tool"
    assert js["description"] == "A sample."
    assert js["schema"] == {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    assert "title" not in js["schema"]


async def test_openai_sends_bearer_auth_header(monkeypatch):
    client = _CapturingClient([_ok({"answer": "ok"})])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    await _oai().structured(
        system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
    )

    assert client.calls[0]["headers"]["Authorization"] == "Bearer test-secret-key"


async def test_missing_api_key_falls_back_to_placeholder(monkeypatch):
    """Local servers ignore auth; the header still has to be well-formed."""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = _CapturingClient([_ok({"answer": "ok"})])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    result = await _oai(api_key=None).structured(
        system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
    )

    assert result == {"answer": "ok"}
    assert client.calls[0]["headers"]["Authorization"] == "Bearer not-needed"


async def test_timeout_is_passed_to_the_http_client(monkeypatch):
    client = _CapturingClient([_ok({"answer": "ok"})])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    await _oai(timeout=12.5).structured(
        system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
    )

    assert client.init_kwargs["timeout"] == 12.5


# ---------------------------------------------------------------------------
# OpenAI-compatible provider — response handling
# ---------------------------------------------------------------------------


async def test_successful_response_returns_the_parsed_dict(monkeypatch):
    client = _CapturingClient([_ok({"answer": "hello", "extra": 1})])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    result = await _oai().structured(
        system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
    )

    assert result == {"answer": "hello", "extra": 1}


async def test_malformed_json_retries_once_then_raises(monkeypatch):
    bad = _FakeHTTPResponse({"choices": [{"message": {"content": "{not json"}}]})
    client = _CapturingClient([bad, bad])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    with pytest.raises(llm_provider.LLMProviderError, match="not valid JSON"):
        await _oai().structured(
            system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
        )

    assert len(client.calls) == 2, "one retry expected"


async def test_retry_falls_back_to_plain_json_mode(monkeypatch):
    """A server without json_schema support may still honour json_object."""
    bad = _FakeHTTPResponse({"choices": [{"message": {"content": "{not json"}}]})
    client = _CapturingClient([bad, _ok({"answer": "second try"})])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    result = await _oai().structured(
        system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
    )

    assert result == {"answer": "second try"}
    assert client.calls[0]["json"]["response_format"]["type"] == "json_schema"
    assert client.calls[1]["json"]["response_format"] == {"type": "json_object"}
    # The retry restates the schema so the model has something to conform to.
    assert "answer" in client.calls[1]["json"]["messages"][0]["content"]


async def test_response_missing_required_field_raises(monkeypatch):
    resp = _ok({"something_else": "x"})
    client = _CapturingClient([resp, _ok({"something_else": "x"})])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    with pytest.raises(llm_provider.LLMProviderError, match="missing required field"):
        await _oai().structured(
            system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
        )


async def test_non_object_json_raises(monkeypatch):
    resp = _FakeHTTPResponse({"choices": [{"message": {"content": '["a"]'}}]})
    client = _CapturingClient([resp, resp])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    with pytest.raises(llm_provider.LLMProviderError, match="expected a JSON object"):
        await _oai().structured(
            system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
        )


async def test_missing_choices_raises(monkeypatch):
    resp = _FakeHTTPResponse({"choices": []})
    client = _CapturingClient([resp, resp])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    with pytest.raises(llm_provider.LLMProviderError, match="choices"):
        await _oai().structured(
            system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
        )


async def test_http_error_status_raises_without_retry(monkeypatch):
    client = _CapturingClient([
        _FakeHTTPResponse(status_code=500, text="upstream exploded"),
    ])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    with pytest.raises(llm_provider.LLMProviderError, match="HTTP 500"):
        await _oai().structured(
            system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
        )

    assert len(client.calls) == 1, "a 500 should not be retried with another format"


async def test_connection_failure_raises_provider_error(monkeypatch):
    class _ExplodingClient(_CapturingClient):
        async def post(self, url, json=None, headers=None):
            raise llm_provider.httpx.ConnectError("no route to host")

    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", _ExplodingClient([]))

    with pytest.raises(llm_provider.LLMProviderError, match="request failed"):
        await _oai().structured(
            system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
        )


# ---------------------------------------------------------------------------
# Error messages must not leak credentials
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_response",
    [
        lambda: _FakeHTTPResponse({"choices": [{"message": {"content": "{bad"}}]}),
        lambda: _FakeHTTPResponse(status_code=401, text="unauthorized"),
    ],
    ids=["malformed-json", "http-error"],
)
async def test_errors_never_contain_the_api_key(monkeypatch, make_response):
    client = _CapturingClient([make_response(), make_response()])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    with pytest.raises(llm_provider.LLMProviderError) as exc:
        await _oai(api_key="sk-super-secret-value").structured(
            system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
        )

    message = str(exc.value)
    assert "sk-super-secret-value" not in message
    # ...but it does carry enough context to debug.
    assert "gemma3:12b" in message
    assert "desktop-tailscale-name" in message


async def test_errors_redact_credentials_embedded_in_the_base_url(monkeypatch):
    client = _CapturingClient([
        _FakeHTTPResponse(status_code=500, text="boom"),
    ])
    monkeypatch.setattr(llm_provider.httpx, "AsyncClient", client)

    with pytest.raises(llm_provider.LLMProviderError) as exc:
        await _oai(base_url="http://user:tok3n@desktop:11434/v1").structured(
            system="s", messages=[], schema=SAMPLE_SCHEMA, max_tokens=10,
        )

    assert "tok3n" not in str(exc.value)
    assert "***@desktop:11434" in str(exc.value)


def test_redact_leaves_credential_free_urls_alone():
    url = "http://desktop-name:11434/v1"
    assert llm_provider._redact(url) == url
