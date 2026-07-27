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
lets the interface stay four arguments wide and stay provider-neutral: the
same dict becomes an Anthropic tool definition or an OpenAI
`response_format={"type": "json_schema", ...}` with no call-site changes.

Two backends, selected by `LLM_PROVIDER`:

    anthropic          (default)  Claude via the Anthropic SDK
    openai_compatible             any server speaking /chat/completions

`openai_compatible` covers Ollama's `/v1`, llama.cpp's llama-server, vLLM,
LM Studio and TGI with one adapter — switching runtimes is a `LLM_BASE_URL`
change. **No host is assumed:** the intended deployment is a GPU desktop
reached over a private network (Tailscale), not localhost.

> Operational note: a self-hosted model server has no authentication worth
> the name. Keep it on Tailscale or another authenticated tunnel; do not
> expose it to the public internet. And measure quality before pointing the
> grading paths (`guided_evaluate`, `evaluate_production`) at a local model —
> those write real SRS state through progression_service.

The mock path is NOT a provider — see the module docstring note in
llm_service.py. `MOCK_LLM` short-circuits inside each domain function
because those fakes are computed from call arguments (target word, user
answer) that never reach this layer.
"""
from __future__ import annotations

import json
import os
from typing import Any, Protocol, runtime_checkable

import anthropic
import httpx

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


def _redact(url: str) -> str:
    """Strip any `user:pass@` credentials from a URL before it reaches an error.

    `LLM_API_KEY` is never put in a message, but a base URL can itself carry
    credentials (`http://user:token@host:11434/v1`). Error strings end up in
    logs and client-error reports, so scrub them there too.
    """
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    if not rest:
        return url
    _userinfo, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"


class OpenAICompatibleProvider:
    """Any server speaking OpenAI's `/chat/completions`.

    One adapter covers every practical way to self-host: Ollama's `/v1`
    endpoint, llama.cpp's `llama-server`, vLLM, LM Studio, TGI. Swapping
    runtimes is a `LLM_BASE_URL` change, which is why this is deliberately
    NOT an Ollama-native or llama.cpp-specific client.

    **The server is not assumed to be local.** `LLM_BASE_URL` takes any host
    — the expected deployment is a GPU desktop reachable over a private
    network (Tailscale), e.g. `http://desktop-name:11434/v1`. Nothing here
    binds to localhost.

    Uses `httpx`, already a declared dependency. The `openai` package is
    deliberately not used: it is not in requirements.txt, and the wire format
    is small enough that a direct POST is clearer than a client abstraction.
    """

    #: Servers that don't implement `json_schema` may still honour plain JSON
    #: mode. We ask for the schema first and fall back only on the retry.
    _RETRIES = 1

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        raw_base = base_url if base_url is not None else os.getenv("LLM_BASE_URL")
        if not raw_base:
            raise LLMProviderError(
                "LLM_PROVIDER=openai_compatible requires LLM_BASE_URL, e.g. "
                "http://desktop-name:11434/v1 (any reachable host — the model "
                "server is not assumed to be local)"
            )
        self._base_url = raw_base.rstrip("/")

        resolved_model = model or os.getenv("LLM_MODEL")
        if not resolved_model:
            # No sensible default exists across runtimes, and sending an empty
            # or wrong model name surfaces as an opaque 404 from the server.
            raise LLMProviderError(
                "LLM_PROVIDER=openai_compatible requires LLM_MODEL "
                "(the model name the server serves, e.g. 'gemma3:12b')"
            )
        self._model = resolved_model

        # Local servers usually ignore auth entirely; a placeholder keeps the
        # Authorization header well-formed for the ones that require any value.
        self._api_key = (
            api_key if api_key is not None else os.getenv("LLM_API_KEY")
        ) or "not-needed"

        raw_timeout = timeout if timeout is not None else os.getenv("LLM_TIMEOUT_SECONDS")
        try:
            self._timeout = float(raw_timeout) if raw_timeout else 60.0
        except (TypeError, ValueError):
            raise LLMProviderError(
                f"LLM_TIMEOUT_SECONDS must be a number, got {raw_timeout!r}"
            ) from None

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/chat/completions"

    def _fail(self, why: str) -> LLMProviderError:
        """Build an error carrying provider/model/base-url, never the key."""
        return LLMProviderError(
            f"{why} [provider=openai_compatible model={self._model} "
            f"base_url={_redact(self._base_url)}]"
        )

    def _build_payload(self, system, messages, schema, max_tokens, *, use_schema: bool) -> dict:
        name, description, body = split_schema(schema)
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if use_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": name,
                    "description": description,
                    "schema": body,
                    "strict": True,
                },
            }
        else:
            # Retry shape: servers without json_schema support often still
            # honour plain JSON mode. Restate the schema in the prompt so the
            # model has something to conform to.
            payload["response_format"] = {"type": "json_object"}
            payload["messages"][0] = {
                "role": "system",
                "content": (
                    f"{system}\n\nRespond with a single JSON object matching "
                    f"this schema:\n{json.dumps(body, sort_keys=True)}"
                ),
            }
        return payload

    def _parse(self, data: dict, schema_body: dict) -> dict:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise self._fail(f"response had no choices[0].message.content: {exc}") from exc

        if not isinstance(content, str):
            raise self._fail(f"message.content was {type(content).__name__}, expected str")

        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise self._fail(f"message.content was not valid JSON: {exc}") from exc

        if not isinstance(parsed, dict):
            raise self._fail(
                f"expected a JSON object, got {type(parsed).__name__}"
            )

        missing = [k for k in schema_body.get("required", []) if k not in parsed]
        if missing:
            raise self._fail(f"response is missing required field(s): {', '.join(missing)}")

        return parsed

    async def structured(
        self,
        system: str,
        messages: list[dict],
        schema: dict,
        max_tokens: int,
    ) -> dict:
        _name, _description, body = split_schema(schema)
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_error: LLMProviderError | None = None
        for attempt in range(self._RETRIES + 1):
            payload = self._build_payload(
                system, messages, schema, max_tokens, use_schema=(attempt == 0),
            )
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(
                        self.endpoint, json=payload, headers=headers,
                    )
            except httpx.HTTPError as exc:
                # Network-level failure: no point re-asking the same host with
                # a different response_format.
                raise self._fail(f"request failed: {type(exc).__name__}: {exc}") from exc

            if response.status_code >= 400:
                raise self._fail(
                    f"server returned HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )

            try:
                data = response.json()
            except ValueError as exc:
                last_error = self._fail(f"response body was not JSON: {exc}")
            else:
                try:
                    return self._parse(data, body)
                except LLMProviderError as exc:
                    last_error = exc

            if attempt < self._RETRIES:
                continue

        raise last_error if last_error else self._fail("no response")


#: Recognised `LLM_PROVIDER` values -> constructor.
_PROVIDERS = {
    "anthropic": AnthropicProvider,
    "openai_compatible": OpenAICompatibleProvider,
}


def get_provider() -> LLMProvider:
    """Build the provider named by `LLM_PROVIDER`; Anthropic when unset.

    Constructed at import time by each service so `ANTHROPIC_API_KEY` is read
    exactly when it was before the seam existed.

    With no new env vars set this returns `AnthropicProvider()` — existing
    deployments are unaffected. A misconfigured `openai_compatible` raises at
    construction (i.e. at import), which is deliberate: a backend that cannot
    reach its model server should fail loudly at boot, not on the first
    learner's chat message.
    """
    name = (os.getenv("LLM_PROVIDER") or "anthropic").strip().lower()
    try:
        factory = _PROVIDERS[name]
    except KeyError:
        raise LLMProviderError(
            f"unknown LLM_PROVIDER {name!r}; expected one of "
            f"{', '.join(sorted(_PROVIDERS))}"
        ) from None
    return factory()
