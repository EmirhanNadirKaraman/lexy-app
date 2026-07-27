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


def _pre_seam_module(path: str, modname: str):
    """Load a service module as it existed before the seam, from git.

    Returns None when the pre-seam revision isn't reachable (shallow clone,
    or the refactor already committed and squashed) — the test skips rather
    than failing on repo shape.
    """
    import types

    for rev in ("HEAD", "HEAD~1", "HEAD~2"):
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
        pytest.skip("pre-seam revision not reachable from this checkout")

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
        pytest.skip("pre-seam revision not reachable from this checkout")

    for old_fn, new_fn in (
        (old._make_eval_tool, llm_service._make_eval_schema),
        (old._make_guided_hints_tool, llm_service._make_guided_hints_schema),
    ):
        name, description, body = llm_provider.split_schema(new_fn(language))
        rebuilt = {"name": name, "description": description, "input_schema": body}
        assert rebuilt == old_fn(language)
