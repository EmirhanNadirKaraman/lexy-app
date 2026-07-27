"""
Free chat → progression tests.

Source of truth: lexy-app/features/free-chat-progression/tests.md

Covers:
  Unit (no DB):
    - _compute_sentence_quality for five natural-language cases
  Unit (DB):
    - match_learning_words: surface match, lemma match, known excluded,
      empty text, numbers only, deduplication
  Integration (real DB, mocked LLM):
    - German message (de)            → free_chat_used_correctly (both tracks advance)
    - Mixed message                  → free_chat_mixed_lang     (passive only)
    - English label + target word    → free_chat_mixed_lang     (passive only)
                                       [mixed-language crediting fix, 2026-05-23 —
                                        a target-language word in an English-classified
                                        message still earns passive credit]
    - English label + no target word → no progression
    - free_chat_matched is NOT fired by the chat router (confirmed by event mapping)
"""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.routers.chat import _compute_sentence_quality
from backend.services.chat_service import match_learning_words
from ._email_helper import make_test_email

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
SESSIONS = "/api/v1/chat/sessions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _email() -> str:
    return make_test_email()


async def _register_and_login(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    r = await client.post(LOGIN, json={"email": email, "password": "password123"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    uid = str(await db_pool.fetchval("SELECT user_id FROM users WHERE email = $1", email))
    return headers, uid


async def _get_word(db_pool) -> tuple[int, str, str]:
    # Exclude test-fixture pollution: other suites (e.g. test_words.py's
    # "learn anyway" path) insert synthetic surfaces like 'ζtest_<hex>' /
    # 'Bnk_<hex>' into the un-user-scoped word_table and the autouse cleanup
    # (which only deletes test users) can't reap them. Those surfaces contain
    # digits/underscores, which real corpus words never do — and they can't
    # round-trip the message tokenizer, so a LIMIT-1 grab of one silently
    # breaks every match-based test. Pick a plain word and stay deterministic.
    row = await db_pool.fetchrow(
        "SELECT word_id, word, language FROM word_table "
        "WHERE word !~ '[0-9_]' ORDER BY word_id LIMIT 1"
    )
    if row is None:
        pytest.skip("word_table has no plain word — run the subtitle pipeline first")
    return row["word_id"], row["word"], row["language"]


async def _mark_learning(db_pool, uid: str, word_id: int) -> None:
    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge
            (user_id, item_id, item_type, status, passive_level, active_level)
        VALUES ($1::uuid, $2, 'word', 'learning', 1, 0)
        ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'learning'
        """,
        uid, word_id,
    )


async def _get_knowledge(db_pool, uid: str, word_id: int) -> dict | None:
    row = await db_pool.fetchrow(
        """
        SELECT passive_level, active_level, times_used_correctly
          FROM user_word_knowledge
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
        """,
        uid, word_id,
    )
    return dict(row) if row else None


async def _create_free_session(client: AsyncClient, headers: dict) -> str:
    r = await client.post(SESSIONS, json={"session_type": "free"}, headers=headers)
    assert r.status_code == 201
    return r.json()["session_id"]


def _llm_result(language_detected: str) -> dict:
    return {
        "reply": "Sehr gut!",
        "language_detected": language_detected,
        "corrections": [],
        "word_matches": [],
    }


# ---------------------------------------------------------------------------
# Unit tests — _compute_sentence_quality
# ---------------------------------------------------------------------------

def test_quality_empty_list_is_needs_work():
    assert _compute_sentence_quality([]) == "needs_work"


def test_quality_all_high_is_excellent():
    assert _compute_sentence_quality(["high", "high", "high", "high", "high"]) == "excellent"


def test_quality_exactly_60_pct_high_is_excellent():
    # 3 out of 5 = 60 % → boundary: >= 0.6 → excellent
    assert _compute_sentence_quality(["high", "high", "high", "medium", "medium"]) == "excellent"


def test_quality_majority_low_is_needs_work():
    # 2 out of 3 lows = 67 % >= 50 %
    assert _compute_sentence_quality(["low", "low", "high"]) == "needs_work"


def test_quality_mixed_is_good():
    # 1 high (33 % < 60 %), 1 low (33 % < 50 %)
    assert _compute_sentence_quality(["high", "medium", "low"]) == "good"


# ---------------------------------------------------------------------------
# Unit tests — match_learning_words (real DB)
# ---------------------------------------------------------------------------

async def test_match_returns_learning_word_by_surface(client: AsyncClient, db_pool):
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    matches = await match_learning_words(db_pool, uid, word, language)

    assert any(m["item_id"] == word_id for m in matches)
    assert all(m["item_type"] == "word" for m in matches)


async def test_match_finds_word_by_lemma(client: AsyncClient, db_pool):
    """Tokens matching a word's lemma (not surface form) must also be returned."""
    row = await db_pool.fetchrow(
        """
        SELECT word_id, word, lemma, language FROM word_table
         WHERE lemma IS NOT NULL AND LOWER(lemma) != LOWER(word)
           AND word !~ '[0-9_]' AND lemma !~ '[0-9_]'
         LIMIT 1
        """
    )
    if row is None:
        pytest.skip("No word with lemma != surface form in word_table")

    word_id, _word, lemma, language = row["word_id"], row["word"], row["lemma"], row["language"]
    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    # Search using only the lemma form — surface form absent
    matches = await match_learning_words(db_pool, uid, lemma, language)

    assert any(m["item_id"] == word_id for m in matches)


async def test_match_excludes_known_words(client: AsyncClient, db_pool):
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
        VALUES ($1::uuid, $2, 'word', 'known')
        ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'known'
        """,
        uid, word_id,
    )

    matches = await match_learning_words(db_pool, uid, word, language)

    assert not any(m["item_id"] == word_id for m in matches)


async def test_match_returns_empty_for_empty_text(client: AsyncClient, db_pool):
    headers, uid = await _register_and_login(client, db_pool, _email())
    assert await match_learning_words(db_pool, uid, "", "de") == []


async def test_match_returns_empty_for_numbers_only(client: AsyncClient, db_pool):
    headers, uid = await _register_and_login(client, db_pool, _email())
    assert await match_learning_words(db_pool, uid, "123 456 789", "de") == []


async def test_match_deduplicates_repeated_word(client: AsyncClient, db_pool):
    """Same word appearing multiple times in a message yields exactly one match entry."""
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    matches = await match_learning_words(db_pool, uid, f"{word} {word} {word}", language)

    word_matches = [m for m in matches if m["item_id"] == word_id]
    assert len(word_matches) == 1


# ---------------------------------------------------------------------------
# Phrase matching — match_learning_words delegates to matcher_service (#5b)
# ---------------------------------------------------------------------------

async def _get_phrase(db_pool, language: str = "de") -> tuple[int, str, str, str] | None:
    """Return (phrase_id, canonical, surface_form, language) for any seeded phrase."""
    row = await db_pool.fetchrow(
        "SELECT phrase_id, canonical, surface_form, language FROM phrase_table "
        "WHERE language = $1 LIMIT 1",
        language,
    )
    return (row["phrase_id"], row["canonical"], row["surface_form"], row["language"]) if row else None


async def test_match_returns_learning_phrase_by_surface(client: AsyncClient, db_pool):
    """A phrase in 'learning' status whose surface form appears in the message
    must be returned with item_type='phrase'."""
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table is empty — seed the phrase pipeline first")
    phrase_id, canonical, surface_form, language = info

    headers, uid = await _register_and_login(client, db_pool, _email())
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'learning') "
        "ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'learning'",
        uid, phrase_id,
    )

    matches = await match_learning_words(db_pool, uid, surface_form, language)
    assert any(m["item_id"] == phrase_id and m["item_type"] == "phrase" for m in matches)


async def test_match_inflected_phrase_matches_canonical(client: AsyncClient, db_pool):
    """Inflected production (e.g. 'ich freue mich auf die Reise') must match the
    canonical 'sich freuen auf' via the spaCy-based phrase_finder."""
    row = await db_pool.fetchrow(
        "SELECT phrase_id FROM phrase_table WHERE canonical = $1 AND language = 'de'",
        "sich freuen auf",
    )
    if row is None:
        pytest.skip("phrase 'sich freuen auf' not seeded — skip inflection test")
    phrase_id = row["phrase_id"]

    headers, uid = await _register_and_login(client, db_pool, _email())
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'learning') "
        "ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'learning'",
        uid, phrase_id,
    )

    matches = await match_learning_words(db_pool, uid, "Ich freue mich auf die Reise.", "de")
    assert any(m["item_id"] == phrase_id and m["item_type"] == "phrase" for m in matches), (
        "expected inflected 'freue mich auf' to map to canonical 'sich freuen auf'"
    )


async def test_match_excludes_known_phrases(client: AsyncClient, db_pool):
    """A phrase with status='known' must NOT appear in matches."""
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table empty")
    phrase_id, _, surface_form, language = info

    headers, uid = await _register_and_login(client, db_pool, _email())
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'known') "
        "ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'known'",
        uid, phrase_id,
    )

    matches = await match_learning_words(db_pool, uid, surface_form, language)
    assert not any(m["item_id"] == phrase_id and m["item_type"] == "phrase" for m in matches)


async def test_match_word_and_phrase_returned_together(client: AsyncClient, db_pool):
    """A message containing both a tracked word and a tracked phrase must return
    both kinds of match in one call."""
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table empty")
    phrase_id, _, surface_form, language = info

    word_id, word, _ = await _get_word(db_pool)
    if language != "de":
        pytest.skip("test assumes de word + de phrase")

    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'learning') "
        "ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'learning'",
        uid, phrase_id,
    )

    matches = await match_learning_words(db_pool, uid, f"{word} {surface_form}", language)
    item_types = {m["item_type"] for m in matches}
    assert "word" in item_types
    assert "phrase" in item_types


# ---------------------------------------------------------------------------
# Integration tests — free chat HTTP endpoint (LLM mocked)
# ---------------------------------------------------------------------------

async def test_german_message_advances_both_tracks(client: AsyncClient, db_pool):
    """language_detected='de' → free_chat_used_correctly: passive_level AND active_level grow."""
    word_id, word, language = await _get_word(db_pool)
    if language != "de":
        pytest.skip("Need a German word for this test")

    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)
    session_id = await _create_free_session(client, headers)

    before = await _get_knowledge(db_pool, uid, word_id)
    before_passive = before["passive_level"] if before else 0
    before_active  = before["active_level"]  if before else 0

    with patch(
        "backend.routers.chat.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_llm_result("de"),
    ):
        resp = await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": word},
            headers=headers,
        )
    assert resp.status_code == 201

    after = await _get_knowledge(db_pool, uid, word_id)
    assert after["passive_level"] > before_passive, "passive_level should increase for German"
    assert after["active_level"]  > before_active,  "active_level should increase for German"


async def test_mixed_message_advances_passive_only(client: AsyncClient, db_pool):
    """language_detected='mixed' → free_chat_mixed_lang: passive grows, active unchanged."""
    word_id, word, language = await _get_word(db_pool)
    if language != "de":
        pytest.skip("Need a German word for this test")

    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)
    session_id = await _create_free_session(client, headers)

    before = await _get_knowledge(db_pool, uid, word_id)
    before_passive = before["passive_level"] if before else 0
    before_active  = before["active_level"]  if before else 0

    with patch(
        "backend.routers.chat.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_llm_result("mixed"),
    ):
        resp = await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": word},
            headers=headers,
        )
    assert resp.status_code == 201

    after = await _get_knowledge(db_pool, uid, word_id)
    assert after["passive_level"] > before_passive,         "passive_level should increase for mixed"
    assert after["active_level"]  == before_active, "active_level must NOT change for mixed"


async def test_english_label_with_target_word_advances_passive_only(client: AsyncClient, db_pool):
    """Mixed-language crediting fix (2026-05-23): a target-language learning
    word that appears in a message the LLM labels 'en' still earns PASSIVE
    credit (free_chat_mixed_lang) — passive_level grows, active does NOT, and
    no active SRS card is fabricated. Before the fix the 'en' label skipped
    matching entirely and this word got nothing."""
    word_id, word, language = await _get_word(db_pool)
    if language != "de":
        pytest.skip("Need a German word for this test")

    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)
    session_id = await _create_free_session(client, headers)

    before = await _get_knowledge(db_pool, uid, word_id)
    before_passive = before["passive_level"] if before else 0
    before_active  = before["active_level"]  if before else 0

    with patch(
        "backend.routers.chat.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_llm_result("en"),
    ):
        resp = await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": word},
            headers=headers,
        )
    assert resp.status_code == 201

    after = await _get_knowledge(db_pool, uid, word_id)
    assert after["passive_level"] > before_passive, (
        "passive_level should grow for an EN-labelled message containing a target word"
    )
    assert after["active_level"] == before_active, (
        "active_level must NOT change for an EN-labelled message"
    )

    # "active SRS does not advance": _mark_learning inserts uwk directly (no
    # apply_progression), so no active card exists, and free_chat_mixed_lang
    # has no active_srs action — confirm none was fabricated.
    active_card = await db_pool.fetchval(
        """
        SELECT 1 FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
           AND direction = 'active'
        """,
        uid, word_id,
    )
    assert active_card is None, "no active SRS card should be created for a mixed-credit turn"


async def test_english_message_without_target_word_triggers_no_progression(client: AsyncClient, db_pool):
    """language_detected='en' AND the message contains no target-language
    learning word → no progression at all. Complement of the case above: the
    always-on matcher finds nothing, so no event fires."""
    word_id, word, language = await _get_word(db_pool)
    if language != "de":
        pytest.skip("Need a German word for this test")

    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)
    session_id = await _create_free_session(client, headers)

    before = await _get_knowledge(db_pool, uid, word_id)

    # Gibberish content — deterministically contains none of the user's
    # learning words regardless of what _get_word returned.
    with patch(
        "backend.routers.chat.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_llm_result("en"),
    ):
        resp = await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": "qwerty asdfgh zxcvbn"},
            headers=headers,
        )
    assert resp.status_code == 201

    after = await _get_knowledge(db_pool, uid, word_id)
    if after and before:
        assert after["passive_level"] == before["passive_level"], "No progression when no target word present"
        assert after["active_level"]  == before["active_level"],  "No progression when no target word present"
