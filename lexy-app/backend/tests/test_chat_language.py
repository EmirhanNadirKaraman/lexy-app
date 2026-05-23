"""
Stage 3 of second-language plan (2026-05-21).

Pins five contracts on top of the existing test_chat / test_free_chat
suites:

  1. The free-chat system prompt + evaluator tool are language-aware.
     _make_system('de') reads German-tutor; _make_system('es') reads
     Spanish-tutor and contains no German wording.
  2. The guided_hints tool description mentions the target language,
     not hardcoded German.
  3. POST /api/v1/chat/sessions persists `language` on the row, and the
     subsequent message handler reads it (via the chat_service mock).
  4. Free-chat message handler passes the session's stored language to
     llm_service.evaluate_and_reply AND to chat_service.match_learning_words,
     instead of the pre-Stage-3 hardcoded "de".
  5. Spanish free-chat language_detected="es" routes through the
     `free_chat_used_correctly` event (target == session_language),
     not the `mixed` branch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.services.llm_service import (
    _language_name,
    _make_eval_tool,
    _make_guided_hints_tool,
    _make_system,
)
from ._email_helper import make_test_email


# ---------------------------------------------------------------------------
# Prompt-shape unit tests
# ---------------------------------------------------------------------------

def test_language_name_known_codes():
    assert _language_name("de") == "German"
    assert _language_name("es") == "Spanish"
    assert _language_name("ja") == "Japanese"


def test_language_name_unknown_falls_back():
    assert _language_name("xx") == "the target language"
    assert _language_name(None) == "the target language"


def test_make_system_de_contains_german():
    prompt = _make_system("de")
    assert "German" in prompt
    # Same shape as the pre-Stage-3 constant — tutor framing intact.
    assert "language tutor" in prompt
    assert "evaluate_and_reply tool" in prompt


def test_make_system_es_contains_spanish_and_no_german():
    prompt = _make_system("es")
    assert "Spanish" in prompt
    # The key Stage-3 invariant — Spanish session must not still be a
    # German tutor.
    assert "German" not in prompt
    assert "language tutor" in prompt


def test_make_eval_tool_de_enum_includes_de():
    tool = _make_eval_tool("de")
    enum = tool["input_schema"]["properties"]["language_detected"]["enum"]
    assert "de" in enum
    assert "en" in enum
    assert "mixed" in enum


def test_make_eval_tool_es_enum_includes_es_not_de():
    tool = _make_eval_tool("es")
    enum = tool["input_schema"]["properties"]["language_detected"]["enum"]
    assert "es" in enum
    assert "en" in enum
    assert "mixed" in enum
    assert "de" not in enum


def test_guided_hints_tool_es_mentions_spanish_not_german():
    tool = _make_guided_hints_tool("es")
    descs = [
        p["description"]
        for p in tool["input_schema"]["properties"].values()
        if "description" in p
    ]
    joined = " ".join(descs)
    assert "Spanish" in joined
    assert "German" not in joined


def test_guided_hints_tool_de_still_mentions_german():
    tool = _make_guided_hints_tool("de")
    descs = [
        p["description"]
        for p in tool["input_schema"]["properties"].values()
        if "description" in p
    ]
    joined = " ".join(descs)
    assert "German" in joined


# ---------------------------------------------------------------------------
# Session creation persists language
# ---------------------------------------------------------------------------

async def _register(client: AsyncClient) -> dict:
    email = make_test_email()
    await client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_create_free_session_with_es_persists_language(client: AsyncClient, db_pool):
    headers = await _register(client)
    resp = await client.post(
        "/api/v1/chat/sessions",
        json={"session_type": "free", "language": "es"},
        headers=headers,
    )
    assert resp.status_code == 201
    sid = resp.json()["session_id"]

    lang = await db_pool.fetchval(
        "SELECT language FROM chat_sessions WHERE session_id = $1::uuid", sid,
    )
    assert lang == "es"


async def test_create_free_session_defaults_language_to_de(client: AsyncClient, db_pool):
    """A pre-Stage-3 client that doesn't send `language` gets the
    schema default 'de'. Locks the back-compat behaviour the spec
    requires."""
    headers = await _register(client)
    resp = await client.post(
        "/api/v1/chat/sessions",
        json={"session_type": "free"},
        headers=headers,
    )
    assert resp.status_code == 201
    sid = resp.json()["session_id"]
    lang = await db_pool.fetchval(
        "SELECT language FROM chat_sessions WHERE session_id = $1::uuid", sid,
    )
    assert lang == "de"


# ---------------------------------------------------------------------------
# Free-chat message handler honours session language
# ---------------------------------------------------------------------------

async def test_free_chat_message_uses_session_language_for_match(
    client: AsyncClient, db_pool, monkeypatch,
):
    """The message handler used to call match_learning_words(..., 'de')
    unconditionally. Post-Stage-3 it must pass the session's language."""
    headers = await _register(client)
    # Create a Spanish free chat session.
    resp = await client.post(
        "/api/v1/chat/sessions",
        json={"session_type": "free", "language": "es"},
        headers=headers,
    )
    sid = resp.json()["session_id"]

    # Mock the LLM so the test stays deterministic + free.
    from backend.services import chat_service, llm_service
    fake_reply = {
        "reply": "¡Hola!",
        "language_detected": "es",
        "corrections": [],
        "word_matches": [],
    }
    monkeypatch.setattr(llm_service, "evaluate_and_reply",
                        AsyncMock(return_value=fake_reply))
    matcher = AsyncMock(return_value=[])
    monkeypatch.setattr(chat_service, "match_learning_words", matcher)

    r = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"content": "Hola mundo"},
        headers=headers,
    )
    assert r.status_code == 201

    # match_learning_words got the SESSION's language, not "de".
    matcher.assert_awaited_once()
    pool_arg, user_id_arg, text_arg, lang_arg = matcher.await_args.args
    assert lang_arg == "es"

    # evaluate_and_reply also got the session language.
    eval_call = llm_service.evaluate_and_reply.await_args  # type: ignore[attr-defined]
    assert eval_call.args[-1] == "es"


async def test_free_chat_es_used_correctly_event_path(
    client: AsyncClient, db_pool, monkeypatch,
):
    """When language_detected matches the session language (es), the
    handler must fire free_chat_used_correctly, not free_chat_mixed_lang.
    Asserts the Stage-3 switch from the hardcoded `== 'de'` to
    `== session_language` works for non-German targets."""
    headers = await _register(client)
    resp = await client.post(
        "/api/v1/chat/sessions",
        json={"session_type": "free", "language": "es"},
        headers=headers,
    )
    sid = resp.json()["session_id"]

    from backend.services import chat_service, llm_service, progression_service
    monkeypatch.setattr(llm_service, "evaluate_and_reply", AsyncMock(return_value={
        "reply": "¡Hola!",
        "language_detected": "es",
        "corrections": [],
        "word_matches": [],
    }))
    monkeypatch.setattr(chat_service, "match_learning_words", AsyncMock(return_value=[
        {"item_id": 9999, "item_type": "word", "word": "hola"},
    ]))
    apply_mock = AsyncMock()
    monkeypatch.setattr(progression_service, "apply_progression", apply_mock)

    r = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"content": "Hola"},
        headers=headers,
    )
    assert r.status_code == 201

    apply_mock.assert_awaited()
    # First positional after pool/user/item/item_type is the event name.
    event = apply_mock.await_args.args[-1]
    assert event == "free_chat_used_correctly"


async def test_legacy_session_with_null_language_falls_back_to_de(
    client: AsyncClient, db_pool, monkeypatch,
):
    """A row predating migration 031 has language=NULL. The message
    handler must fall back to 'de' so legacy German sessions keep
    working byte-for-byte."""
    headers = await _register(client)
    user_id = await db_pool.fetchval(
        "SELECT user_id FROM users WHERE email LIKE $1 ORDER BY created_at DESC LIMIT 1",
        # cleanup_pattern() filters to this xdist worker
        # (see tests/_email_helper.py).
        __import__("backend.tests._email_helper", fromlist=["cleanup_pattern"]).cleanup_pattern(),
    )
    sid = await db_pool.fetchval(
        """
        INSERT INTO chat_sessions (user_id, session_type, language)
        VALUES ($1, 'free', NULL)
        RETURNING session_id
        """,
        user_id,
    )

    from backend.services import chat_service, llm_service
    monkeypatch.setattr(llm_service, "evaluate_and_reply", AsyncMock(return_value={
        "reply": "ok", "language_detected": "de",
        "corrections": [], "word_matches": [],
    }))
    matcher = AsyncMock(return_value=[])
    monkeypatch.setattr(chat_service, "match_learning_words", matcher)

    r = await client.post(
        f"/api/v1/chat/sessions/{sid}/messages",
        json={"content": "Hallo"},
        headers=headers,
    )
    assert r.status_code == 201
    pool_arg, user_id_arg, text_arg, lang_arg = matcher.await_args.args
    assert lang_arg == "de"


# ---------------------------------------------------------------------------
# Spanish phrase matching is a no-op (depends on Stage 1 dispatch)
# ---------------------------------------------------------------------------

async def test_spanish_free_chat_returns_no_phrase_matches(db_pool):
    """Confirms the Stage 1 dispatcher composition: match_learning_words
    with language='es' returns word matches only — phrase extraction
    short-circuits to []. Word path still works (asserted via the
    existing test_free_chat_progression.py); this test just pins the
    phrase no-op for Spanish."""
    from backend.services.chat_service import match_learning_words
    # Insert a Spanish word_table row + uwk for a synthetic user so the
    # word path has something to match. Skip if the language column on
    # word_table doesn't carry 'es' rows yet — the v1 corpus isn't ingested.
    email = make_test_email()
    user_id = await db_pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING user_id",
        email,
    )
    word_id = await db_pool.fetchval(
        """
        INSERT INTO word_table (word, lemma, pos, tag, language)
        VALUES ('hola', 'hola', 'X', 'X', 'es')
        ON CONFLICT (word, language, pos) DO UPDATE SET word = EXCLUDED.word
        RETURNING word_id
        """,
    )
    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge (user_id, item_id, item_type, status, passive_level, active_level)
        VALUES ($1::uuid, $2, 'word', 'learning', 1, 0)
        """,
        user_id, word_id,
    )

    matches = await match_learning_words(db_pool, str(user_id), "hola mundo", "es")

    # The Spanish word is matched (Stage 0 + Stage 1 + Stage 3 chain works).
    word_matches = [m for m in matches if m["item_type"] == "word"]
    phrase_matches = [m for m in matches if m["item_type"] == "phrase"]
    assert any(m["item_id"] == word_id for m in word_matches)
    # Phrase extractor is a no-op for Spanish (Stage 1 dispatcher) —
    # no Spanish phrase rows can come back.
    assert phrase_matches == []


# ---------------------------------------------------------------------------
# Mixed-language crediting fix (2026-05-23) on a non-German target.
# Mirrors test_free_chat_progression.py's German cases for Spanish: an
# EN-labelled message that contains a Spanish learning word still earns
# passive credit; an ES-labelled message earns both tracks. Real DB, so we
# assert the persisted levels, not just the event name.
# ---------------------------------------------------------------------------

async def _register_with_uid(client: AsyncClient, db_pool) -> tuple[dict, str]:
    email = make_test_email()
    await client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    uid = str(await db_pool.fetchval("SELECT user_id FROM users WHERE email = $1", email))
    return headers, uid


async def _seed_es_learning_word(db_pool, uid: str, surface: str) -> int:
    word_id = await db_pool.fetchval(
        """
        INSERT INTO word_table (word, lemma, pos, tag, language)
        VALUES ($1, $1, 'X', 'X', 'es')
        ON CONFLICT (word, language, pos) DO UPDATE SET word = EXCLUDED.word
        RETURNING word_id
        """,
        surface,
    )
    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge
            (user_id, item_id, item_type, status, passive_level, active_level)
        VALUES ($1::uuid, $2, 'word', 'learning', 1, 0)
        ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'learning'
        """,
        uid, word_id,
    )
    return word_id


async def _es_levels(db_pool, uid: str, word_id: int) -> dict:
    row = await db_pool.fetchrow(
        "SELECT passive_level, active_level FROM user_word_knowledge "
        "WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'",
        uid, word_id,
    )
    return dict(row) if row else {"passive_level": 0, "active_level": 0}


async def _create_es_session(client: AsyncClient, headers: dict) -> str:
    resp = await client.post(
        "/api/v1/chat/sessions",
        json={"session_type": "free", "language": "es"},
        headers=headers,
    )
    assert resp.status_code == 201
    return resp.json()["session_id"]


def _es_reply(language_detected: str) -> dict:
    return {
        "reply": "ok",
        "language_detected": language_detected,
        "corrections": [],
        "word_matches": [],
    }


async def test_spanish_english_label_with_target_word_advances_passive_only(
    client: AsyncClient, db_pool,
):
    """Spanish session, language_detected='en', message contains a Spanish
    learning word → free_chat_mixed_lang: passive grows, active does not.
    Spanish phrase extraction is a no-op, so only the word advances."""
    headers, uid = await _register_with_uid(client, db_pool)
    word_id = await _seed_es_learning_word(db_pool, uid, "hola")
    sid = await _create_es_session(client, headers)

    before = await _es_levels(db_pool, uid, word_id)

    with patch(
        "backend.routers.chat.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_es_reply("en"),
    ):
        r = await client.post(
            f"/api/v1/chat/sessions/{sid}/messages",
            json={"content": "hola"},
            headers=headers,
        )
    assert r.status_code == 201

    after = await _es_levels(db_pool, uid, word_id)
    assert after["passive_level"] > before["passive_level"], (
        "Spanish word in an EN-labelled message should earn passive credit"
    )
    assert after["active_level"] == before["active_level"], (
        "active_level must NOT change for an EN-labelled message"
    )


async def test_spanish_es_label_advances_both_tracks(client: AsyncClient, db_pool):
    """Spanish session, language_detected='es', message contains a Spanish
    learning word → free_chat_used_correctly: passive AND active grow. The
    real-DB complement of test_free_chat_es_used_correctly_event_path (which
    only checks the event name under mocks)."""
    headers, uid = await _register_with_uid(client, db_pool)
    word_id = await _seed_es_learning_word(db_pool, uid, "gato")
    sid = await _create_es_session(client, headers)

    before = await _es_levels(db_pool, uid, word_id)

    with patch(
        "backend.routers.chat.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_es_reply("es"),
    ):
        r = await client.post(
            f"/api/v1/chat/sessions/{sid}/messages",
            json={"content": "gato"},
            headers=headers,
        )
    assert r.status_code == 201

    after = await _es_levels(db_pool, uid, word_id)
    assert after["passive_level"] > before["passive_level"], "passive should grow for es+es"
    assert after["active_level"] > before["active_level"], "active should grow for es+es"


# ---------------------------------------------------------------------------
# #39 slice 2 — backend matcher parses Spanish with the Spanish model AND
# applies seeded lemma overrides (was: every language parsed with German model)
# ---------------------------------------------------------------------------

import importlib.util as _ilu  # noqa: E402

_ES_INSTALLED = _ilu.find_spec("es_core_news_sm") is not None


@pytest.mark.skipif(not _ES_INSTALLED, reason="es_core_news_sm not installed")
async def test_matcher_with_ids_applies_seeded_lemma_override(db_pool):
    """End-to-end #39: match_sentence_with_ids parses Spanish with the Spanish
    model and loads the migration-033-seeded override (duchaber->duchar), so
    'Se ducha…' yields the corrected canonical 'ducharse', never 'duchaberse'.
    Pre-fix this parsed Spanish with the German model and produced nothing
    usable."""
    from backend.services import matcher_service

    out = await matcher_service.match_sentence_with_ids(
        db_pool, "Se ducha por la mañana.", "es",
    )
    canon = {p["dictionary_entry"] for p in out}
    assert "ducharse" in canon, f"expected corrected canonical; got {canon}"
    assert "duchaberse" not in canon, "the broken canonical must not survive the override"
