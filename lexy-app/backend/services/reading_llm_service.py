"""
reading_llm_service.py

LLM functions for the interactive reading feature.

  translate_sentence   -- translate a sentence into English (cached permanently)
  explain_in_context   -- explain a selected unit in the context of its sentence (cached)

Both funnel through llm_cache_service.get_or_compute (#24): cache hits skip
the LLM; concurrent misses on the same key serialise on a per-key
asyncio.Lock so the provider is called exactly once across the wave.
"""
from __future__ import annotations

import os

import asyncpg

from . import llm_cache_service, llm_provider

_provider = llm_provider.get_provider()
_MOCK = os.getenv("MOCK_LLM", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------------------
# Sentence translation
# ---------------------------------------------------------------------------

_TRANSLATE_SCHEMA = {
    "title": "translate_sentence",
    "description": "Translate a sentence into natural English.",
    "type": "object",
    "properties": {
        "translation": {
            "type": "string",
            "description": "A natural, fluent English translation of the sentence.",
        }
    },
    "required": ["translation"],

}


async def translate_sentence(
    sentence: str,
    language: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> str:
    """
    Translate a sentence from `language` into English.

    Cached permanently by (sentence, language) -- the same sentence always
    gets the same translation, so re-selecting it is instant.
    """
    if _MOCK:
        return f"[Mock translation of {language} sentence: \"{sentence[:60]}\"]"

    async def _compute() -> dict:
        response = await _provider.structured(
            system=(
                f"You are a precise translator. Translate the following {language} sentence into "
                f"natural, fluent English. Preserve the meaning faithfully. "
                f"You MUST call the translate_sentence tool."
            ),
            messages=[{"role": "user", "content": f"Translate: {sentence}"}],
            schema=_TRANSLATE_SCHEMA,
            max_tokens=256,
        )
        return {"translation": response["translation"]}

    if pool is None:
        return (await _compute())["translation"]

    cache_key = llm_cache_service.make_cache_key(
        "reading_translate", _provider.model_id,
        {"sentence": sentence, "language": language},
    )
    result = await llm_cache_service.get_or_compute(
        pool, cache_key, "reading_translate", _provider.model_id, _compute,
    )
    return result["translation"]


# ---------------------------------------------------------------------------
# Contextual explanation
# ---------------------------------------------------------------------------

_EXPLAIN_SCHEMA = {
    "title": "explain_in_context",
    "description": "Explain the meaning and usage of a selected word or phrase within its sentence.",
    "type": "object",
    "properties": {
        "explanation": {
            "type": "string",
            "description": (
                "2-3 sentences explaining the selected text specifically in this sentence. "
                "Cover: (1) what it means here, (2) its grammatical role or structure, "
                "(3) any useful pattern or usage note for learners."
            ),
        }
    },
    "required": ["explanation"],

}


async def explain_in_context(
    selection: str,
    sentence: str,
    language: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> str:
    """
    Explain `selection` in the context of `sentence`.

    Cached permanently by (canonical_selection, sentence, language).
    The selection is lowercased before hashing to avoid duplicate cache
    entries for the same unit with different capitalisation.
    """
    if _MOCK:
        return (
            f"[Mock explanation: \"{selection}\" is used in this sentence to express "
            f"a key concept. In {language}, this construction typically follows "
            f"the pattern shown here. Pay attention to the grammatical case used.]"
        )

    async def _compute() -> dict:
        response = await _provider.structured(
            system=(
                f"You are a {language} language learning assistant helping an intermediate learner "
                f"understand a word or phrase in context. "
                f"Be concise (2-3 sentences), practical, and focused on meaning-in-context. "
                f"Mention grammatical structure only when it matters for understanding. "
                f"You MUST call the explain_in_context tool."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Sentence: {sentence}\n\n"
                    f"Selected: \"{selection}\"\n\n"
                    f"Explain what \"{selection}\" means and how it works in this sentence."
                ),
            }],
            schema=_EXPLAIN_SCHEMA,
            max_tokens=512,
        )
        return {"explanation": response["explanation"]}

    if pool is None:
        return (await _compute())["explanation"]

    cache_key = llm_cache_service.make_cache_key(
        "reading_explain", _provider.model_id,
        {
            "selection": selection.lower(),
            "sentence": sentence,
            "language": language,
        },
    )
    result = await llm_cache_service.get_or_compute(
        pool, cache_key, "reading_explain", _provider.model_id, _compute,
    )
    return result["explanation"]
