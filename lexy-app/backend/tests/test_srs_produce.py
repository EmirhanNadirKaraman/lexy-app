"""
#0a-2 backend tests — POST /api/v1/srs/review/{card_id}/produce.

Covers:
  - Ownership + direction guards (passive / grammar_rule / not-found).
  - Exact-match fast path skips the LLM and counts correct.
  - LLM-judged correct → active_review_correct → SM-2 advance.
  - LLM-judged incorrect → active_review_incorrect → SM-2 reset.
  - "I don't know" path still goes through the existing /review/{id} endpoint.
  - Production response shape matches SRSProductionResponse.
"""

import pytest
from httpx import AsyncClient

from backend.services import llm_service
from ._email_helper import make_test_email
from ._auth_helper import register_and_login

SRS_DUE     = "/api/v1/srs/due"
SRS_REVIEW  = "/api/v1/srs/review"
REGISTER    = "/api/v1/auth/register"
LOGIN       = "/api/v1/auth/login"


def _email() -> str:
    return make_test_email()


async def _register(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    """Register + login. Returns (auth_headers, user_id_str).

    Delegates to the shared helper; see tests/_auth_helper.py.
    """
    return await register_and_login(client, db_pool, email)


async def _get_word(db_pool) -> tuple[int, str, str]:
    # Owned, uniquely-named word (xdist-safe — see conftest / docs/TESTS.md).
    # The surface doubles as the /produce answer; exact-match grades it correct.
    from ._word_helper import insert_owned_word
    wid, surface = await insert_owned_word(db_pool, language="de")
    return wid, surface, "de"


async def _get_grammar_rule(db_pool) -> tuple[int, str] | None:
    row = await db_pool.fetchrow("SELECT rule_id, language FROM grammar_rule_table LIMIT 1")
    return (row["rule_id"], row["language"]) if row else None


async def _active_card_for_word(client: AsyncClient, db_pool, headers, uid: str, word_id: int) -> int:
    """Mark a word as learning for *uid* (creates both cards), return the active card_id."""
    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )
    return await db_pool.fetchval(
        "SELECT card_id FROM srs_cards "
        "WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word' AND direction = 'active'",
        uid, word_id,
    )


@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch):
    """Hermetic tests — never call the real Anthropic API."""
    monkeypatch.setattr(llm_service, "_MOCK", True)


# ---------------------------------------------------------------------------
# Guard rails
# ---------------------------------------------------------------------------

async def test_produce_rejects_passive_card_with_400(client: AsyncClient, db_pool):
    word_id, word_text, _ = await _get_word(db_pool)
    headers, uid = await _register(client, db_pool, _email())
    # Get the passive card (same word, direction='passive').
    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )
    passive_card_id = await db_pool.fetchval(
        "SELECT card_id FROM srs_cards "
        "WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word' AND direction = 'passive'",
        uid, word_id,
    )

    resp = await client.post(
        f"{SRS_REVIEW}/{passive_card_id}/produce",
        json={"answer": word_text},
        headers=headers,
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "passive_card"


async def test_produce_rejects_grammar_rule_card_with_400(client: AsyncClient, db_pool):
    info = await _get_grammar_rule(db_pool)
    if info is None:
        pytest.skip("grammar_rule_table empty")
    rule_id, _ = info
    headers, uid = await _register(client, db_pool, _email())
    # Grammar rule cards are always passive; manually seed an active one to
    # exercise the grammar_rule guard rather than the passive guard.
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'grammar_rule', 'learning')",
        uid, rule_id,
    )
    card_id = await db_pool.fetchval(
        """
        INSERT INTO srs_cards
            (user_id, item_id, item_type, direction,
             due_date, interval_days, ease_factor, repetitions)
        VALUES ($1::uuid, $2, 'grammar_rule', 'active', NOW(), 1.0, 2.5, 0)
        RETURNING card_id
        """,
        uid, rule_id,
    )

    resp = await client.post(
        f"{SRS_REVIEW}/{card_id}/produce",
        json={"answer": "anything"},
        headers=headers,
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "grammar_rule"


async def test_produce_rejects_card_belonging_to_other_user(client: AsyncClient, db_pool):
    word_id, word_text, _ = await _get_word(db_pool)
    headers_a, uid_a = await _register(client, db_pool, _email())
    headers_b, _     = await _register(client, db_pool, _email())
    card_id = await _active_card_for_word(client, db_pool, headers_a, uid_a, word_id)

    resp = await client.post(
        f"{SRS_REVIEW}/{card_id}/produce",
        json={"answer": word_text},
        headers=headers_b,
    )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "card_not_found"


async def test_produce_requires_auth(client: AsyncClient):
    resp = await client.post(f"{SRS_REVIEW}/999/produce", json={"answer": "x"})
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Exact-match fast path — skips LLM
# ---------------------------------------------------------------------------

async def test_exact_match_is_correct_without_calling_llm(client: AsyncClient, db_pool, monkeypatch):
    """Normalized exact match (case-insensitive, whitespace-tolerant) must not
    invoke the LLM evaluator. We monkey-patch evaluate_production to assert
    it never runs."""
    word_id, word_text, _ = await _get_word(db_pool)
    headers, uid = await _register(client, db_pool, _email())
    card_id = await _active_card_for_word(client, db_pool, headers, uid, word_id)

    async def _explode(*args, **kwargs):
        raise AssertionError("LLM evaluator must not be called on exact match")

    monkeypatch.setattr(llm_service, "evaluate_production", _explode)

    resp = await client.post(
        f"{SRS_REVIEW}/{card_id}/produce",
        json={"answer": f"  {word_text.upper()}  "},   # whitespace + case
        headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["correct"] is True
    assert body["card_id"] == card_id
    assert body["submitted"].strip().lower() == word_text.lower()
    assert body["expected"] == word_text


# ---------------------------------------------------------------------------
# LLM eval — correct / incorrect routing
# ---------------------------------------------------------------------------

async def test_llm_judged_correct_advances_active_card(client: AsyncClient, db_pool, monkeypatch):
    """Non-exact answer → LLM eval → correct → active_review_correct event."""
    word_id, word_text, _ = await _get_word(db_pool)
    headers, uid = await _register(client, db_pool, _email())
    card_id = await _active_card_for_word(client, db_pool, headers, uid, word_id)

    async def _fake_eval(target_text, target_lemma, user_answer, language):
        return {"correct": True, "feedback": "good inflection", "corrected_form": target_text}

    monkeypatch.setattr(llm_service, "evaluate_production", _fake_eval)

    resp = await client.post(
        f"{SRS_REVIEW}/{card_id}/produce",
        json={"answer": f"{word_text}-inflected"},
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["correct"] is True

    # Active card advanced (reps > 0) and active_level incremented per the rule.
    card_after = await db_pool.fetchrow(
        "SELECT repetitions FROM srs_cards WHERE card_id = $1", card_id,
    )
    assert card_after["repetitions"] >= 1

    uwk = await db_pool.fetchrow(
        "SELECT active_level, times_used_correctly FROM user_word_knowledge "
        "WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'",
        uid, word_id,
    )
    assert uwk["active_level"] >= 1
    assert uwk["times_used_correctly"] >= 1


async def test_llm_judged_incorrect_resets_active_card(client: AsyncClient, db_pool, monkeypatch):
    """Wrong answer → active_review_incorrect → SM-2 reset (interval=1, reps=0)."""
    word_id, _, _ = await _get_word(db_pool)
    headers, uid = await _register(client, db_pool, _email())
    card_id = await _active_card_for_word(client, db_pool, headers, uid, word_id)

    # Pre-advance the card so we can see a reset.
    await db_pool.execute(
        "UPDATE srs_cards SET interval_days = 7, ease_factor = 2.5, repetitions = 3 "
        "WHERE card_id = $1",
        card_id,
    )

    async def _fake_eval(target_text, target_lemma, user_answer, language):
        return {"correct": False, "feedback": "wrong word", "corrected_form": target_text}

    monkeypatch.setattr(llm_service, "evaluate_production", _fake_eval)

    resp = await client.post(
        f"{SRS_REVIEW}/{card_id}/produce",
        json={"answer": "etwas anderes"},
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["correct"] is False

    card_after = await db_pool.fetchrow(
        "SELECT interval_days, repetitions FROM srs_cards WHERE card_id = $1", card_id,
    )
    assert card_after["interval_days"] == 1.0
    assert card_after["repetitions"] == 0


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------

async def test_response_shape(client: AsyncClient, db_pool):
    word_id, word_text, _ = await _get_word(db_pool)
    headers, uid = await _register(client, db_pool, _email())
    card_id = await _active_card_for_word(client, db_pool, headers, uid, word_id)

    resp = await client.post(
        f"{SRS_REVIEW}/{card_id}/produce",
        json={"answer": word_text},
        headers=headers,
    )

    body = resp.json()
    assert set(body) == {"card_id", "correct", "expected", "submitted", "feedback"}
    assert body["card_id"] == card_id
    assert isinstance(body["correct"], bool)
    assert isinstance(body["expected"], str)
    assert isinstance(body["submitted"], str)
    assert isinstance(body["feedback"], str)


# ---------------------------------------------------------------------------
# "I don't know" — uses the existing /review/{card_id} endpoint
# ---------------------------------------------------------------------------

async def test_dont_know_still_works_via_existing_endpoint(client: AsyncClient, db_pool):
    """The frontend's 'I don't know' button calls POST /srs/review/{id}
    with correct=false — same path used by self-graded passive answers."""
    word_id, _, _ = await _get_word(db_pool)
    headers, uid = await _register(client, db_pool, _email())
    card_id = await _active_card_for_word(client, db_pool, headers, uid, word_id)

    # Pre-advance the card.
    await db_pool.execute(
        "UPDATE srs_cards SET interval_days = 7, ease_factor = 2.5, repetitions = 3 "
        "WHERE card_id = $1",
        card_id,
    )

    resp = await client.post(
        f"{SRS_REVIEW}/{card_id}",
        json={"correct": False},
        headers=headers,
    )

    assert resp.status_code == 200
    card_after = await db_pool.fetchrow(
        "SELECT interval_days, repetitions FROM srs_cards WHERE card_id = $1", card_id,
    )
    assert card_after["interval_days"] == 1.0
    assert card_after["repetitions"] == 0


# ---------------------------------------------------------------------------
# Production helper unit tests (no HTTP)
# ---------------------------------------------------------------------------

async def test_evaluate_production_mock_substring_match():
    """Mock mode: substring of target in answer → correct."""
    result = await llm_service.evaluate_production("Auto", "Auto", "ich kaufe ein Auto", "de")
    assert result["correct"] is True
    assert "mock" in result["feedback"].lower()


async def test_evaluate_production_mock_no_match():
    result = await llm_service.evaluate_production("Auto", "Auto", "Banane", "de")
    assert result["correct"] is False


async def test_evaluate_production_empty_answer_is_incorrect():
    result = await llm_service.evaluate_production("Auto", "Auto", "", "de")
    assert result["correct"] is False
