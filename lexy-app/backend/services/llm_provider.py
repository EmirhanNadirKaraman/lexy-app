"""
llm_provider.py

The single seam between this app and whatever model actually answers.

Every LLM call in this codebase has the same shape: non-streaming, one
forced tool, tool input parsed as structured JSON. No call streams, none
uses more than one tool, and no tool is ever *executed* — `tools` is being
used purely as a JSON-schema enforcement mechanism. So the provider
interface is one method wide:

    structured(system, messages, schema, max_tokens) -> dict

`schema` is a plain JSON Schema object carrying two standard keywords that
every provider needs but expresses differently:

    title       -> Anthropic tool name         / OpenAI json_schema name
    description -> Anthropic tool description  / (prompt context elsewhere)

Keeping them as JSON Schema keywords rather than separate arguments is what
lets the interface stay four arguments wide and stay provider-neutral: a
future OpenAI-compatible adapter maps the same dict onto
`response_format={"type": "json_schema", ...}` with no call-site changes.

Deliberately NOT here yet: the OpenAI-compatible / local-model adapter.
When it lands it will be a second class implementing this Protocol, selected
by env var. `LLM_BASE_URL` is reserved for it and must accept any host —
the local server is expected to run on a different machine over a private
network, not on localhost.

The mock path is NOT a provider — see the module docstring note in
llm_service.py. `MOCK_LLM` short-circuits inside each domain function
because those fakes are computed from call arguments (target word, user
answer) that never reach this layer.
"""
from __future__ import annotations

import os
from typing import Any, Protocol, runtime_checkable

import anthropic

# The model every service used as a hardcoded literal before the seam existed.
# Overridable via LLM_MODEL — see the cache-key note in `model_id`.
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"


class LLMProviderError(RuntimeError):
    """The provider returned nothing usable for the requested schema.

    Raised instead of the bare `StopIteration` that the pre-seam
    `next(b for b in response.content if b.type == "tool_use")` produced —
    `StopIteration` inside a coroutine surfaces as an opaque RuntimeError
    and says nothing about which call failed.
    """


@runtime_checkable
class LLMProvider(Protocol):
    """Structured-JSON completion backend."""

    @property
    def model_id(self) -> str:
        """Identifier for the model actually serving requests.

        Feeds `llm_cache_service.make_cache_key`, which hashes the model into
        every key. Changing it re-namespaces the whole cache rather than
        returning another model's answers — safe, but a cold start.
        """
        ...

    async def structured(
        self,
        system: str,
        messages: list[dict],
        schema: dict,
        max_tokens: int,
    ) -> dict:
        """Return an object conforming to *schema*."""
        ...


def split_schema(schema: dict) -> tuple[str, str, dict[str, Any]]:
    """Split a JSON Schema into (name, description, input_schema).

    `title` and `description` are stripped from the returned body. That
    matters for byte-identity: the pre-seam tool constants carried neither
    key inside `input_schema`, and leaving them in would change the tool
    definition on the wire — accepted by the API, but a different prompt
    than before. `tests/test_llm_provider.py` pins this against the real
    schemas.
    """
    try:
        name = schema["title"]
    except KeyError:
        raise LLMProviderError(
            "schema is missing 'title' (used as the tool/format name)"
        ) from None
    description = schema.get("description", "")
    body = {k: v for k, v in schema.items() if k not in ("title", "description")}
    return name, description, body


class AnthropicProvider:
    """Claude via the Anthropic SDK, using forced single-tool calls.

    Byte-identical on the wire to the pre-seam call sites: same model, same
    `tools=[one]` + `tool_choice={"type": "tool", "name": ...}`, same
    system/messages/max_tokens pass-through.
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY")
        )
        self._model = model or os.getenv("LLM_MODEL") or DEFAULT_ANTHROPIC_MODEL

    @property
    def model_id(self) -> str:
        return self._model

    async def structured(
        self,
        system: str,
        messages: list[dict],
        schema: dict,
        max_tokens: int,
    ) -> dict:
        name, description, input_schema = split_schema(schema)
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            tools=[{
                "name": name,
                "description": description,
                "input_schema": input_schema,
            }],
            tool_choice={"type": "tool", "name": name},
            messages=messages,
        )
        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                return dict(block.input)
        raise LLMProviderError(
            f"no tool_use block in response for schema {name!r} "
            f"(model={self._model})"
        )


def get_provider() -> LLMProvider:
    """Build the configured provider. Anthropic is the only backend today.

    Constructed at import time by each service so `ANTHROPIC_API_KEY` is read
    exactly when it was before the seam existed.
    """
    return AnthropicProvider()
