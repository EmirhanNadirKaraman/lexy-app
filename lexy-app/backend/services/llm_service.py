import os
import random

import asyncpg

from . import llm_cache_service, llm_provider

# Module-level so tests can swap the whole backend with
# `monkeypatch.setattr(llm_service, "_provider", Fake())`. Built at import
# time, matching when the raw AsyncAnthropic client used to read its key.
_provider = llm_provider.get_provider()

# MOCK_LLM stays a domain-level short-circuit rather than a MockProvider:
# these fakes are computed from call arguments (target word, user answer,
# requested language) that the provider interface never receives — it sees
# those values only as prose inside the prompt. Reconstructing them there
# would couple the mock to prompt wording. See docs/TESTS.md.
_MOCK = os.getenv("MOCK_LLM", "").lower() in ("1", "true", "yes")

_MOCK_REPLIES = [
    "Das ist gut! Kannst du mir mehr erzählen?",
    "Sehr interessant! Wie war dein Tag?",
    "Super! Ich freue mich, das zu hören.",
    "Toll gemacht! Magst du das erklären?",
    "Wunderbar! Was denkst du darüber?",
]

_MOCK_CORRECTIONS = [
    [{"original": "ich bin gegangen", "corrected": "ich bin gegangen", "explanation": "Mock: correct usage of Perfekt."}],
    [{"original": "das Hund", "corrected": "der Hund", "explanation": "Mock: 'Hund' is masculine, use 'der'."}],
    [],
    [],
    [],
]

_MOCK_HINTS = {
    "intent_hint": "Try expressing that you used up or consumed something.",
    "anchor_hint": "Tipp: Denke an ein Verb, das mit 'ver' beginnt…",
    "example": "Ich habe gestern Abend mein ganzes Taschengeld verbraucht.",
}

_MOCK_OPENINGS = [
    "Hallo! Ich plane gerade ein Wochenendausflug und bin mir noch nicht sicher, wohin ich fahren soll. Hast du irgendwelche Empfehlungen?",
    "Hey! Ich war gerade im Café und habe einen wirklich interessanten Menschen getroffen. Was machst du so am Wochenende?",
    "Guten Tag! Ich bereite gerade ein Abendessen für Freunde vor — habt ihr ein Lieblingsrezept, das ich ausprobieren sollte?",
]

# Stage 3 of second-language plan (2026-05-21): _SYSTEM and _EVAL_TOOL
# used to hardcode German. They're now constructed per-call from the
# session's target language so the LLM gets a Spanish-tutor prompt for
# Spanish sessions, German-tutor prompt for German sessions, etc.
#
# `_LANGUAGE_NAMES` is the source of human-readable names sent into
# prompts; codes outside the table fall back to a generic "target
# language" wording so a future ingest of (say) Polish content doesn't
# break the call. Stays in lockstep with frontend's
# `src/config/languages.ts` LANGUAGE_OPTIONS — when adding a target
# language, update both.

_LANGUAGE_NAMES: dict[str, str] = {
    "de": "German",
    "en": "English",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
    "ja": "Japanese",
    "ru": "Russian",
    "ko": "Korean",
    "tr": "Turkish",
    "pl": "Polish",
    "sv": "Swedish",
}


def _language_name(code: str | None) -> str:
    """Human-readable language name for prompt interpolation.

    Falls back to `"the target language"` for unknown codes — the LLM
    still reads naturally, just without a specific name to anchor on.
    """
    if not code:
        return "the target language"
    return _LANGUAGE_NAMES.get(code, "the target language")


def _make_system(language: str) -> str:
    """Build the free-chat system prompt for a given target language.

    German wording is byte-equivalent to the pre-Stage-3 constant when
    `language == 'de'` — same tone, same rules, same tool-only contract.
    For any other language we swap "German" for the target language's
    name; English stays the bilingual fallback.
    """
    name = _language_name(language)
    return (
        f"You are a warm, encouraging {name} language tutor in a "
        f"free-conversation practice app.\n"
        f"The learner is practising spoken {name}. Your job is twofold:\n"
        f"  1. Keep the conversation going naturally (reply in {name}).\n"
        f"  2. Quietly correct any language errors the learner made.\n"
        f"\n"
        f"Rules:\n"
        f"- Reply conversationally in {name}. Be friendly, brief, and encouraging.\n"
        f"- If the user wrote in English, reply in English but gently nudge them to try in {name}.\n"
        f"- List ONLY genuine language errors (grammar, wrong word, spelling). Skip style preferences.\n"
        f"- If there are no errors, return an empty corrections array.\n"
        f"- You MUST call the evaluate_and_reply tool — never respond with raw text.\n"
    )


def _make_eval_schema(language: str) -> dict:
    """Build the free-chat evaluator tool definition for a given target
    language. `language_detected` enum is parameterised so the LLM picks
    between the active target language, English, and `mixed` — pre-Stage-3
    this was hardcoded to `['de', 'en', 'mixed']`."""
    return {
        "title": "evaluate_and_reply",
        "description": "Produce a structured response: a conversational reply plus any corrections.",
        "type": "object",
        "properties": {
            "reply": {
                "type": "string",
                "description": "Your conversational reply.",
            },
            "language_detected": {
                "type": "string",
                "enum": [language, "en", "mixed"] if language != "en" else ["en", "mixed"],
                "description": "Dominant language of the user's message.",
            },
            "corrections": {
                "type": "array",
                "description": "Language errors found. Empty list if none.",
                "items": {
                    "type": "object",
                    "properties": {
                        "original":    {"type": "string"},
                        "corrected":   {"type": "string"},
                        "explanation": {"type": "string"},
                    },
                    "required": ["original", "corrected", "explanation"],
                },
            },
        },
        "required": ["reply", "language_detected", "corrections"],
    
    }


# ---------------------------------------------------------------------------
# Guided chat — opener
# ---------------------------------------------------------------------------

_GUIDED_OPEN_SCHEMA = {
    "title": "open_conversation",
    "description": "Generate the opening message of a guided conversation scenario.",
    "type": "object",
    "properties": {
        "opening": {
            "type": "string",
            "description": "The opening message in the target language (2-3 sentences).",
        }
    },
    "required": ["opening"],

}


async def guided_open(
    target_word: str,
    language: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> str:
    """
    Generate an opening scenario that naturally invites the target word without revealing it.
    Returns the opening message text.

    Cached permanently in llm_cache when pool is provided — the same
    word+language always produces a pedagogically valid opener.
    """
    if _MOCK:
        return random.choice(_MOCK_OPENINGS)

    system = (
        f"You are a warm, engaging {language} conversation partner starting a role-play. "
        f"The hidden pedagogical goal is for the learner to eventually use the word/phrase "
        f'"{target_word}" naturally — but you must NOT use it yourself. '
        f"Create a brief, realistic social scene (café, travel plans, weekend talk, etc.) "
        f"that makes it natural to respond using that kind of vocabulary. "
        f"Write in {language}, 2–3 sentences, no hints that this is an exercise. "
        f"You MUST call the open_conversation tool."
    )

    async def _compute() -> dict:
        response = await _provider.structured(
            system=system,
            messages=[{"role": "user", "content": "Start the conversation."}],
            schema=_GUIDED_OPEN_SCHEMA,
            max_tokens=256,
        )
        return {"opening": response["opening"]}

    if pool is None:
        # No pool → uncacheable single-shot path.
        return (await _compute())["opening"]

    cache_key = llm_cache_service.make_cache_key(
        "guided_open", _provider.model_id, {"target_word": target_word, "language": language}
    )
    result = await llm_cache_service.get_or_compute(
        pool, cache_key, "guided_open", _provider.model_id, _compute,
    )
    return result["opening"]


# ---------------------------------------------------------------------------
# Guided chat — progressive hints
# ---------------------------------------------------------------------------

def _make_guided_hints_schema(language: str) -> dict:
    """Stage 3: tool description text interpolates the target-language name
    so the LLM is told to produce hints in the right language. Pre-Stage-3
    this was a module-level constant whose descriptions hardcoded German.
    """
    name = _language_name(language)
    return {
        "title": "generate_hints",
        "description": "Generate three progressive learning hints for a target word/phrase.",
        "type": "object",
        "properties": {
            "intent_hint": {
                "type": "string",
                "description": (
                    "One sentence in English describing the concept or action to express. "
                    "Must NOT name the target word, its direct translation, or a clear synonym. "
                    "Describes what kind of meaning the learner should convey."
                ),
            },
            "anchor_hint": {
                "type": "string",
                "description": (
                    f"A short {name} clue — a related word, a prefix hint, "
                    f"or a closely related concept — that narrows the search "
                    f"without giving the full answer. Must NOT be the target word itself."
                ),
            },
            "example": {
                "type": "string",
                "description": (
                    f"A complete, natural {name} sentence that uses the target word in a realistic context. "
                    f"The target word must appear exactly as-is or in a natural inflected form."
                ),
            },
        },
        "required": ["intent_hint", "anchor_hint", "example"],
    
    }


async def guided_hints(
    target_word: str,
    language: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> dict:
    """
    Generate three progressive hints for a guided-chat target word.

    Returns:
        {"intent_hint": str, "anchor_hint": str, "example": str}

    Cached permanently by (target_word, language) — the same word always gets
    the same hints, so re-opening a session is instant. Stage 3 (2026-05-21):
    prompt + tool description are now language-aware so a Spanish target
    word doesn't get "German clue" wording. German cache entries written
    pre-Stage-3 stay valid and serve German hints; Spanish writes new
    cache entries under their own `language` key.
    """
    if _MOCK:
        return dict(_MOCK_HINTS)

    name = _language_name(language)
    system = (
        f'You are creating pedagogical hints for a language learner whose hidden target word/phrase is "{target_word}" in {name}.\n\n'
        f"Generate exactly three hints in order of increasing explicitness:\n"
        f"1. intent_hint — English only. Describe what concept or action to express WITHOUT naming the target or its translation.\n"
        f"2. anchor_hint — {name} only. Give a partial clue: a related word, a prefix hint, or a semantic neighbour. "
        f"Do NOT use the target word itself.\n"
        f"3. example — A full natural {name} sentence using the target word in a realistic everyday context.\n\n"
        f"You MUST call the generate_hints tool."
    )

    async def _compute() -> dict:
        response = await _provider.structured(
            system=system,
            messages=[{"role": "user", "content": "Generate the hints now."}],
            schema=_make_guided_hints_schema(language),
            max_tokens=512,
        )
        return {
            "intent_hint": response["intent_hint"],
            "anchor_hint":  response["anchor_hint"],
            "example":      response["example"],
        }

    if pool is None:
        return await _compute()

    cache_key = llm_cache_service.make_cache_key(
        "guided_hints", _provider.model_id, {"target_word": target_word, "language": language}
    )
    return await llm_cache_service.get_or_compute(
        pool, cache_key, "guided_hints", _provider.model_id, _compute,
    )


# ---------------------------------------------------------------------------
# Guided chat — per-turn evaluation + reply
# ---------------------------------------------------------------------------

_GUIDED_EVAL_SCHEMA = {
    "title": "guided_evaluate",
    "description": "Evaluate the learner's message and produce a structured reply for guided practice.",
    "type": "object",
    "properties": {
        "reply": {
            "type": "string",
            "description": "Conversational follow-up in the target language.",
        },
        "language_detected": {
            "type": "string",
            "enum": ["de", "en", "mixed"],
            "description": "Dominant language of the user's message.",
        },
        "corrections": {
            "type": "array",
            "description": "Genuine language errors only. Empty list if none.",
            "items": {
                "type": "object",
                "properties": {
                    "original":    {"type": "string"},
                    "corrected":   {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["original", "corrected", "explanation"],
            },
        },
        "target_used": {
            "type": "boolean",
            "description": "True if the learner used the target word/phrase or a clear inflection of it.",
        },
        "target_counted": {
            "type": "boolean",
            "description": (
                "True only if the usage is in natural, correct target-language context "
                "and counts toward mastery. False if used in English, forced, or grammatically wrong."
            ),
        },
        "feedback_short": {
            "type": "string",
            "description": (
                "One brief encouraging sentence about the target usage (e.g. 'Sehr gut, du hast X perfekt benutzt!'). "
                "Empty string if the target was not used."
            ),
        },
        "naturalness": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "Overall naturalness and quality of the learner's target-language usage in this turn.",
        },
    },
    "required": [
        "reply", "language_detected", "corrections",
        "target_used", "target_counted", "feedback_short", "naturalness",
    ],

}


async def guided_evaluate(
    user_content: str,
    history: list[dict],
    target_word: str,
    language: str,
) -> dict:
    """
    Evaluate one user turn in a guided session.

    Returns:
        {
            reply: str,
            language_detected: "de" | "en" | "mixed",
            corrections: [...],
            target_used: bool,
            target_counted: bool,
            feedback_short: str,
            naturalness: "high" | "medium" | "low",
        }
    """
    if _MOCK:
        used = target_word.lower() in user_content.lower()
        return {
            "reply": random.choice(_MOCK_REPLIES),
            "language_detected": "de",
            "corrections": random.choice(_MOCK_CORRECTIONS),
            "target_used": used,
            "target_counted": used,
            "feedback_short": f"Sehr gut, du hast '{target_word}' verwendet!" if used else "",
            "naturalness": "medium",
        }

    system = (
        f"You are a warm, encouraging {language} conversation partner in a guided practice session. "
        f'The hidden target word/phrase is "{target_word}". '
        f"The learner should use it naturally — never reveal the target or ask them to use it.\n\n"
        f"Per turn:\n"
        f"1. Continue the conversation naturally (reply in {language}).\n"
        f"2. Correct ONLY genuine language errors (grammar, wrong word, spelling). Skip style preferences.\n"
        f"3. Evaluate whether the learner used the target word/phrase.\n\n"
        f"Rules:\n"
        f'- target_used: true if "{target_word}" or a clear inflected form appears.\n'
        f"- target_counted: true only if used in natural, correct {language}. "
        f"False if used in English, grammatically wrong, or clearly forced.\n"
        f"- feedback_short: one encouraging sentence if used; empty string otherwise.\n"
        f"- naturalness: overall quality of the {language} in this turn.\n"
        f"- If the user wrote mostly in English, reply in English and gently nudge toward {language}.\n"
        f"- You MUST call the guided_evaluate tool."
    )

    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": user_content})

    response = await _provider.structured(
        system=system,
        messages=messages,
        schema=_GUIDED_EVAL_SCHEMA,
        max_tokens=1024,
    )

    return {
        "reply":             response["reply"],
        "language_detected": response["language_detected"],
        "corrections":       response.get("corrections", []),
        "target_used":       response["target_used"],
        "target_counted":    response["target_counted"],
        "feedback_short":    response.get("feedback_short", ""),
        "naturalness":       response.get("naturalness", "medium"),
    }


# ---------------------------------------------------------------------------
# Prep view — item info (translation + grammar explanation)
# ---------------------------------------------------------------------------

_PREP_INFO_SCHEMA = {
    "title": "item_prep_info",
    "description": "Provide structured language-learning prep information for a vocabulary item.",
    "type": "object",
    "properties": {
        "translation": {
            "type": "string",
            "description": "Concise English translation. For verbs include the base form (e.g. 'to spend (time)').",
        },
        "grammar_structure": {
            "type": "string",
            "description": (
                "The core grammatical pattern in compact form. "
                "Examples: 'verbringen + Akkusativ', 'sich freuen + über + Akkusativ', "
                "'Nomen (der/die/das)'. Keep it under 60 characters."
            ),
        },
        "grammar_explanation": {
            "type": "string",
            "description": (
                "2–4 sentences covering: (1) meaning and grammatical role, "
                "(2) required case/preposition/reflexive structure, "
                "(3) one common learner mistake to avoid."
            ),
        },
    },
    "required": ["translation", "grammar_structure", "grammar_explanation"],

}


async def prep_item_info(
    display_text: str,
    item_type: str,
    language: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> dict:
    """
    Return {translation, grammar_structure, grammar_explanation} for a vocabulary item.

    Cached permanently in llm_cache by (display_text, item_type, language).
    Called as part of GET /insights/prep — always loads, never deferred.
    """
    if _MOCK:
        return {
            "translation": "to spend (time)",
            "grammar_structure": f"{display_text} + Akkusativ",
            "grammar_explanation": (
                f"Mock: '{display_text}' is a common {language} {item_type}. "
                f"It typically requires the accusative case. "
                f"Common mistake: confusing it with a similar-sounding word."
            ),
        }

    system = (
        f"You are a concise {language} language learning assistant. "
        f"Provide structured prep information for a {item_type} the learner is about to practise. "
        f"Be precise, practical, and production-oriented. "
        f"You MUST call the item_prep_info tool."
    )

    async def _compute() -> dict:
        response = await _provider.structured(
            system=system,
            messages=[{
                "role": "user",
                "content": f"Provide prep information for the {language} {item_type}: \"{display_text}\"",
            }],
            schema=_PREP_INFO_SCHEMA,
            max_tokens=512,
        )
        return {
            "translation":         response["translation"],
            "grammar_structure":   response["grammar_structure"],
            "grammar_explanation": response["grammar_explanation"],
        }

    if pool is None:
        return await _compute()

    cache_key = llm_cache_service.make_cache_key(
        "prep_item_info", _provider.model_id,
        {"display_text": display_text, "item_type": item_type, "language": language},
    )
    return await llm_cache_service.get_or_compute(
        pool, cache_key, "prep_item_info", _provider.model_id, _compute,
    )


# ---------------------------------------------------------------------------
# Prep view — examples + templates (on-demand)
# ---------------------------------------------------------------------------

_PREP_EXAMPLES_SCHEMA = {
    "title": "item_examples",
    "description": "Generate a usage example and two reusable production templates for a vocabulary item.",
    "type": "object",
    "properties": {
        "example": {
            "type": "string",
            "description": "One clear, natural sentence in the target language using the item in a realistic context.",
        },
        "templates": {
            "type": "array",
            "description": (
                "Exactly 2 reusable sentence templates for production practice. "
                "Use [square bracket slots] for variable parts (e.g. [Zeit], [Person], [Ort]). "
                "Each template should be a complete sentence skeleton."
            ),
            "items": {"type": "string"},
            "minItems": 2,
            "maxItems": 2,
        },
    },
    "required": ["example", "templates"],

}


async def prep_generate_examples(
    display_text: str,
    item_type: str,
    language: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> dict:
    """
    Return {example: str, templates: [str, str]}.
    Cached permanently by (display_text, item_type, language).
    Called only when the user explicitly requests example generation.
    """
    if _MOCK:
        return {
            "example": f"Ich verwende '{display_text}' in einem Beispielsatz.",
            "templates": [
                f"Ich [Verb] {display_text} [Ergänzung].",
                f"[Person] hat {display_text} [Kontext] [Verb].",
            ],
        }

    system = (
        f"You are a {language} language learning assistant focused on production practice. "
        f"Generate a usage example and two reusable sentence templates for a {language} {item_type}. "
        f"Templates use [square bracket slots] for variable parts. "
        f"Favour everyday, naturalistic contexts. You MUST call the item_examples tool."
    )

    async def _compute() -> dict:
        response = await _provider.structured(
            system=system,
            messages=[{
                "role": "user",
                "content": f"Generate an example and templates for the {language} {item_type}: \"{display_text}\"",
            }],
            schema=_PREP_EXAMPLES_SCHEMA,
            max_tokens=256,
        )
        return {
            "example":   response["example"],
            "templates": response["templates"][:2],
        }

    if pool is None:
        return await _compute()

    cache_key = llm_cache_service.make_cache_key(
        "prep_examples", _provider.model_id,
        {"display_text": display_text, "item_type": item_type, "language": language},
    )
    return await llm_cache_service.get_or_compute(
        pool, cache_key, "prep_examples", _provider.model_id, _compute,
    )


async def get_examples_if_cached(
    display_text: str,
    item_type: str,
    language: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> dict | None:
    """
    Return cached examples/templates without generating.
    Returns None if not in cache — the caller shows a "Generate examples" button.
    """
    if pool is None:
        return None
    cache_key = llm_cache_service.make_cache_key(
        "prep_examples", _provider.model_id,
        {"display_text": display_text, "item_type": item_type, "language": language},
    )
    return await llm_cache_service.get_cached(pool, cache_key)


# ---------------------------------------------------------------------------
# Guided chat — post-session summary
# ---------------------------------------------------------------------------

_GUIDED_SUMMARY_SCHEMA = {
    "title": "guided_session_summary",
    "description": "Generate concise post-session feedback for a completed guided practice session.",
    "type": "object",
    "properties": {
        "what_went_well": {
            "type": "string",
            "description": "One specific sentence about what the learner did well.",
        },
        "what_to_improve": {
            "type": "string",
            "description": (
                "One sentence on the single most important thing to improve. "
                "Empty string if there is nothing significant."
            ),
        },
        "corrective_note": {
            "type": "string",
            "description": (
                "A direct corrective comment on the most critical grammar or usage error observed. "
                "Format: 'Use X instead of Y because...' "
                "Empty string if no significant errors were observed."
            ),
        },
    },
    "required": ["what_went_well", "what_to_improve", "corrective_note"],

}


async def guided_summarize(
    target_word: str,
    language: str,
    target_used: bool,
    target_counted: bool,
    sentence_quality: str,
    all_corrections: list[dict],
    total_turns: int,
) -> dict:
    """
    Generate concise post-session feedback after a guided chat session ends.

    Returns:
        {
            what_went_well: str,
            what_to_improve: str,   # empty string = nothing significant
            corrective_note: str,   # empty string = no corrections
        }
    """
    if _MOCK:
        if target_counted:
            return {
                "what_went_well": f"You used '{target_word}' naturally and correctly — well done.",
                "what_to_improve": "",
                "corrective_note": "",
            }
        elif target_used:
            return {
                "what_went_well": "You engaged with the topic confidently.",
                "what_to_improve": f"Make sure to use '{target_word}' in correct, natural {language} next time.",
                "corrective_note": "",
            }
        else:
            return {
                "what_went_well": "You kept the conversation going.",
                "what_to_improve": f"Try to work '{target_word}' into your response.",
                "corrective_note": "",
            }

    # Deduplicate corrections by original form, cap at 4 for prompt brevity
    seen: set[str] = set()
    unique_corrections: list[dict] = []
    for c in all_corrections:
        key = c.get("original", "")
        if key and key not in seen:
            seen.add(key)
            unique_corrections.append(c)
            if len(unique_corrections) >= 4:
                break

    corrections_text = "\n".join(
        f'  • "{c["original"]}" → "{c["corrected"]}": {c["explanation"]}'
        for c in unique_corrections
    ) or "  (none)"

    if target_counted:
        target_status = "used correctly in natural context"
    elif target_used:
        target_status = "used but not in correct/natural target-language context"
    else:
        target_status = "not used"

    system = (
        f"You are a concise, critical {language} language tutor writing a post-session summary.\n"
        f"Session details:\n"
        f"  Target word/phrase: \"{target_word}\"\n"
        f"  Target usage: {target_status}\n"
        f"  Overall sentence quality: {sentence_quality}\n"
        f"  Turns taken: {total_turns}\n"
        f"  Corrections observed:\n{corrections_text}\n\n"
        f"Write direct, honest, specific feedback. One sentence per field. "
        f"Be encouraging but critical — no empty praise.\n"
        f"You MUST call the guided_session_summary tool."
    )

    response = await _provider.structured(
        system=system,
        messages=[{"role": "user", "content": "Generate the session summary now."}],
        schema=_GUIDED_SUMMARY_SCHEMA,
        max_tokens=384,
    )

    return {
        "what_went_well":  response.get("what_went_well", ""),
        "what_to_improve": response.get("what_to_improve", ""),
        "corrective_note": response.get("corrective_note", ""),
    }


# ---------------------------------------------------------------------------
# Grammar rule — long explanation (on-demand, cached permanently)
# ---------------------------------------------------------------------------

_GRAMMAR_EXPLAIN_SCHEMA = {
    "title": "grammar_rule_explanation",
    "description": "Generate a detailed, learner-friendly explanation of a grammar rule.",
    "type": "object",
    "properties": {
        "long_explanation": {
            "type": "string",
            "description": (
                "3–5 sentences covering: (1) what the rule is and why it matters, "
                "(2) how it works with concrete examples, "
                "(3) the most common learner mistake and how to avoid it. "
                "Write directly for an intermediate language learner. "
                "Include at least two example sentences in the target language."
            ),
        },
    },
    "required": ["long_explanation"],

}


async def grammar_rule_explanation(
    slug: str,
    title: str,
    short_explanation: str,
    language: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> str:
    """
    Generate and cache a detailed explanation for a grammar rule.

    Returns the long_explanation string.
    Cached permanently by (slug, language) — grammar rules don't change.
    Called only when the user explicitly clicks "Learn more" in the UI.
    """
    if _MOCK:
        return (
            f"Mock: {title} is an important {language} grammar rule. "
            f"{short_explanation} "
            f"Example: 'Ich freue mich über das Geschenk.' "
            f"Common mistake: forgetting the reflexive pronoun or using the wrong case."
        )

    system = (
        f"You are a concise {language} grammar tutor writing learner-friendly rule explanations. "
        f"Be specific, practical, and include real example sentences. "
        f"You MUST call the grammar_rule_explanation tool."
    )

    async def _compute() -> dict:
        response = await _provider.structured(
            system=system,
            messages=[{
                "role": "user",
                "content": (
                    f"Explain the {language} grammar rule '{title}'.\n"
                    f"Short summary: {short_explanation}"
                ),
            }],
            schema=_GRAMMAR_EXPLAIN_SCHEMA,
            max_tokens=512,
        )
        return {"long_explanation": response["long_explanation"]}

    if pool is None:
        return (await _compute())["long_explanation"]

    cache_key = llm_cache_service.make_cache_key(
        "grammar_rule_explanation", _provider.model_id, {"slug": slug, "language": language}
    )
    result = await llm_cache_service.get_or_compute(
        pool, cache_key, "grammar_rule_explanation", _provider.model_id, _compute,
    )
    return result["long_explanation"]


async def get_grammar_explanation_if_cached(
    slug: str,
    language: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> str | None:
    """
    Return the cached long_explanation for a grammar rule without generating it.
    Returns None if not in cache — the caller surfaces a "Learn more" button.
    """
    if pool is None:
        return None
    cache_key = llm_cache_service.make_cache_key(
        "grammar_rule_explanation", _provider.model_id, {"slug": slug, "language": language}
    )
    cached = await llm_cache_service.get_cached(pool, cache_key)
    return cached["long_explanation"] if cached else None


# ---------------------------------------------------------------------------
# Free chat — evaluate and reply
# ---------------------------------------------------------------------------

async def evaluate_and_reply(
    user_content: str,
    history: list[dict],
    language: str = "de",
) -> dict:
    """
    Returns:
        {
            reply: str,
            language_detected: "<target>" | "en" | "mixed",
            corrections: [{"original", "corrected", "explanation"}, ...],
            word_matches: [],   # reserved for phrase-matcher integration
        }

    Stage 3 (second-language plan): `language` defaults to 'de' for
    back-compat. Callers should pass the chat session's stored
    language so the system prompt + evaluator enum match the user's
    target. The `language_detected` enum value the LLM returns will be
    the supplied `language` code (when the user wrote in the target
    language) or "en" / "mixed".
    """
    if _MOCK:
        return {
            "reply": random.choice(_MOCK_REPLIES),
            # Mock surfaces the requested target language so tests can
            # exercise the language-aware free_chat_* progression branches.
            "language_detected": language,
            "corrections": random.choice(_MOCK_CORRECTIONS),
            "word_matches": [],
        }

    messages = [{"role": m["role"], "content": m["content"]} for m in history]
    messages.append({"role": "user", "content": user_content})

    response = await _provider.structured(
        system=_make_system(language),
        messages=messages,
        schema=_make_eval_schema(language),
        max_tokens=1024,
    )

    return {
        "reply": response["reply"],
        "language_detected": response["language_detected"],
        "corrections": response.get("corrections", []),
        "word_matches": [],
    }


# ---------------------------------------------------------------------------
# Item gloss — short English label for SRS review prompts/answers (#0a-1)
# ---------------------------------------------------------------------------

_GLOSS_SCHEMA = {
    "title": "item_gloss",
    "description": "Return a short English gloss for a foreign-language word or phrase.",
    "type": "object",
    "properties": {
        "gloss": {
            "type": "string",
            "description": (
                "A concise English translation of the item — ideally 1-4 words. "
                "Not a full explanation. For verbs use the English infinitive (e.g. 'to eat'). "
                "For phrases give the natural English equivalent. Lowercase unless a proper noun."
            ),
        }
    },
    "required": ["gloss"],

}


# ---------------------------------------------------------------------------
# SRS production evaluation (#0a-2)
# ---------------------------------------------------------------------------

_PRODUCTION_EVAL_SCHEMA = {
    "title": "evaluate_production",
    "description": (
        "Judge whether the learner's answer correctly produces the target item. "
        "Accept reasonable inflections and minor capitalization differences. "
        "Reject answers that are clearly the wrong word, English instead of the "
        "target language, or meaningfully different in meaning."
    ),
    "type": "object",
    "properties": {
        "correct": {
            "type": "boolean",
            "description": "True if the learner produced the target item correctly (allowing reasonable inflection / case differences).",
        },
        "feedback": {
            "type": "string",
            "description": "One short sentence (<= 120 chars) explaining the verdict to the learner.",
        },
        "corrected_form": {
            "type": "string",
            "description": "The canonical target form. Empty string when not applicable.",
        },
    },
    "required": ["correct", "feedback", "corrected_form"],

}


async def evaluate_production(
    target_text: str,
    target_lemma: str | None,
    user_answer: str,
    language: str,
) -> dict:
    """Judge whether `user_answer` correctly produces the target item.

    Not cached — input is user-supplied and high-cardinality. The caller is
    expected to short-circuit obvious exact matches before invoking this.

    Returns:
        {
            "correct": bool,
            "feedback": str,
            "corrected_form": str,
        }
    """
    if _MOCK:
        # Deterministic stub for tests: substring match of target in user answer.
        normalized = (user_answer or "").strip().lower()
        target_lower = target_text.strip().lower()
        correct = bool(normalized) and target_lower in normalized
        return {
            "correct":        correct,
            "feedback":       "[mock] match" if correct else "[mock] not the target",
            "corrected_form": target_text,
        }

    lemma_clause = f"\nTarget lemma: {target_lemma}" if target_lemma else ""
    response = await _provider.structured(
        system=(
            f"You judge whether a learner's single-shot answer correctly produces a target "
            f"{language} item (word or phrase). Be lenient on inflection, case, and minor "
            f"spelling slips; be strict on meaning. Empty / wrong-language / unrelated answers "
            f"are incorrect. You MUST call the evaluate_production tool."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Target: {target_text}{lemma_clause}\n"
                f"Learner answer: {user_answer}\n\n"
                f"Did the learner produce the target correctly?"
            ),
        }],
        schema=_PRODUCTION_EVAL_SCHEMA,
        max_tokens=256,
    )
    return {
        "correct":        bool(response["correct"]),
        "feedback":       str(response["feedback"]),
        "corrected_form": str(response.get("corrected_form") or target_text),
    }


async def translate_item_gloss(
    text: str,
    item_type: str,
    language: str,
    *,
    pool: asyncpg.Pool | None = None,
) -> str:
    """Return a short English gloss for `text`. Permanently cached.

    Used by `review_service.get_due_cards` to populate prompt_text/answer_text
    on SRS cards so the frontend can show English on one side and the German
    item on the other (instead of showing the German on both sides today).

    Handles item_type ∈ {'word', 'phrase'}. Grammar rules don't need an LLM
    gloss — callers use `grammar_rule_table.short_explanation` directly.

    Cache key shape (prompt_key='item_gloss'):
        {"text": text.lower(), "item_type": item_type, "language": language}

    Two cache reads, in order (2026-07-28):

    1. the **curated** key — same params, but model `curated:words_4000_old`
       (`gloss_seed_service.CURATED_MODEL`), holding human translations seeded
       from `data/words_4000_old.txt`;
    2. the model-specific key, computing and caching on a miss exactly as
       before.

    Curated first because `make_cache_key` includes the model: rows written
    under one model id miss entirely once `LLM_MODEL` changes, which is the
    whole point of the provider seam. Checking the model-independent key first
    lets curated glosses survive a switch while genuine LLM output stays
    correctly model-scoped. Nothing here ever *writes* a curated row — only
    `scripts/seed_gloss_cache.py --apply` does.
    """
    if item_type not in ("word", "phrase"):
        raise ValueError(f"translate_item_gloss handles 'word'/'phrase', got {item_type!r}")

    if _MOCK:
        return f"[gloss:{text}]"

    params = {"text": text.lower(), "item_type": item_type, "language": language}

    if pool is not None:
        # Local import: gloss_seed_service imports llm_cache_service, and a
        # module-level import here would make llm_service depend on a seeding
        # module it never otherwise needs.
        from .gloss_seed_service import CURATED_MODEL

        curated = await llm_cache_service.get_cached(
            pool, llm_cache_service.make_cache_key("item_gloss", CURATED_MODEL, params),
        )
        if curated is not None:
            return curated["gloss"]

    async def _compute() -> dict:
        response = await _provider.structured(
            system=(
                f"You produce concise English glosses for {language}-language learning vocabulary. "
                f"Keep the gloss minimal (1-4 words for single words, short phrase for multi-word items). "
                f"You MUST call the item_gloss tool."
            ),
            messages=[{"role": "user", "content": f"Item: {text}\nType: {item_type}"}],
            schema=_GLOSS_SCHEMA,
            max_tokens=64,
        )
        return {"gloss": response["gloss"]}

    if pool is None:
        return (await _compute())["gloss"]

    cache_key = llm_cache_service.make_cache_key(
        "item_gloss", _provider.model_id, params,
    )
    result = await llm_cache_service.get_or_compute(
        pool, cache_key, "item_gloss", _provider.model_id, _compute,
    )
    return result["gloss"]
