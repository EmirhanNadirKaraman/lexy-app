"""
SRS review feature tests.

Source of truth: lexy-app/features/srs-review/tests.md

Covers:
  Unit (no DB):
    - compute_delta for all four review progression events
  Integration (real DB):
    - GET /srs/due: empty for new user, returns card, display_text = word surface,
      excludes known items, requires language param, respects limit
    - POST /srs/review/{card_id}: returns success, advances SM-2 on correct,
      resets SM-2 on incorrect, returns 404 for another user's card

Implementation matches feature spec — no fixes required.
"""
from httpx import AsyncClient

from backend.services.progression_service import compute_delta
from ._email_helper import make_test_email
from ._auth_helper import register_and_login

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
SRS_DUE  = "/api/v1/srs/due"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _email() -> str:
    return make_test_email()


async def _register_and_get_user(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    """Register + login. Returns (auth_headers, user_id_str).

    Delegates to the shared helper; see tests/_auth_helper.py.
    """
    return await register_and_login(client, db_pool, email)


# `make_word` / `srs_word` are shared fixtures in conftest.py (promoted from here
# so every test file can own its words instead of picking a shared word_table
# row). See the conftest docstrings + docs/TESTS.md for the isolation rationale.


async def _get_srs_card(pool, user_id: str, item_id: int, item_type: str, direction: str) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT interval_days, ease_factor, repetitions, due_date
          FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = $3 AND direction = $4
        """,
        user_id, item_id, item_type, direction,
    )
    return dict(row) if row else None


async def _mark_learning_and_get_card_id(client, headers, language, word_id) -> int:
    """Mark word as learning (creates passive SRS card due NOW) and return card_id."""
    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )
    resp = await client.get(SRS_DUE, params={"language": language}, headers=headers)
    cards = resp.json()
    assert cards, "Expected a due card after marking word as learning"
    return cards[0]["card_id"]


# ---------------------------------------------------------------------------
# Unit tests — compute_delta for review events
# ---------------------------------------------------------------------------

def test_passive_review_correct_delta():
    """Passive correct review: advances passive SRS card AND bumps passive_level.

    A controlled review is stronger evidence than a subtitle click (which gives
    passive_delta=1), so successful reviews should at minimum match that.
    """
    d = compute_delta("passive_review_correct")
    assert d.passive_delta == 1
    assert d.active_delta == 0
    assert d.passive_srs == "correct"
    assert d.active_srs is None


def test_passive_review_incorrect_delta():
    """Passive incorrect review: only penalises passive SRS card, no level deltas."""
    d = compute_delta("passive_review_incorrect")
    assert d.passive_delta == 0
    assert d.active_delta == 0
    assert d.passive_srs == "incorrect"
    assert d.active_srs is None


def test_active_review_correct_delta():
    """Active correct: advances both levels, times_used_correctly, and active SRS card."""
    d = compute_delta("active_review_correct")
    assert d.passive_delta == 1
    assert d.active_delta == 1
    assert d.times_used_correctly_delta == 1
    assert d.passive_srs is None
    assert d.active_srs == "correct"


def test_active_review_incorrect_delta():
    """Active incorrect: only penalises active SRS card, no level deltas."""
    d = compute_delta("active_review_incorrect")
    assert d.passive_delta == 0
    assert d.active_delta == 0
    assert d.passive_srs is None
    assert d.active_srs == "incorrect"


# ---------------------------------------------------------------------------
# GET /srs/due
# ---------------------------------------------------------------------------

async def test_due_returns_empty_list_for_new_user(client: AsyncClient, db_pool, srs_word):
    word_id, _, language = srs_word
    headers, _ = await _register_and_get_user(client, db_pool, _email())

    resp = await client.get(SRS_DUE, params={"language": language}, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == []


async def test_due_returns_card_after_marking_word_learning(client: AsyncClient, db_pool, srs_word):
    """status_marked_learning creates both passive and active SRS cards due NOW → both appear in /srs/due."""
    word_id, _, language = srs_word
    headers, _ = await _register_and_get_user(client, db_pool, _email())

    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    resp = await client.get(SRS_DUE, params={"language": language}, headers=headers)

    assert resp.status_code == 200
    cards = resp.json()
    assert len(cards) == 2
    directions = {c["direction"] for c in cards}
    assert directions == {"passive", "active"}
    for card in cards:
        assert card["item_id"] == word_id
        assert card["item_type"] == "word"
        assert "card_id" in card
        assert "display_text" in card


async def test_due_card_display_text_is_word_surface_form(client: AsyncClient, db_pool, srs_word):
    """display_text must come from word_table.word, not stored on srs_cards."""
    word_id, word_text, language = srs_word
    headers, _ = await _register_and_get_user(client, db_pool, _email())

    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    cards = (await client.get(SRS_DUE, params={"language": language}, headers=headers)).json()
    assert cards[0]["display_text"] == word_text


async def test_due_excludes_known_items(client: AsyncClient, db_pool, srs_word):
    """Items with status='known' must not appear in due cards even if due_date <= NOW."""
    word_id, _, language = srs_word
    headers, _ = await _register_and_get_user(client, db_pool, _email())

    # Create the SRS card, then promote to known
    await client.put(f"/api/v1/words/word/{word_id}/status", json={"status": "learning"}, headers=headers)
    await client.put(f"/api/v1/words/word/{word_id}/status", json={"status": "known"},    headers=headers)

    resp = await client.get(SRS_DUE, params={"language": language}, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == []


async def test_due_requires_language_query_param(client: AsyncClient, db_pool):
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    resp = await client.get(SRS_DUE, headers=headers)
    assert resp.status_code == 422


async def test_due_limit_param_is_respected(client: AsyncClient, db_pool, make_word):
    # Two isolated, same-language words this test owns and reaps — no shared
    # `word_table` pick, so a concurrent worker can't perturb the due set.
    (w1, _), (w2, _) = await make_word("de"), await make_word("de")

    headers, _ = await _register_and_get_user(client, db_pool, _email())
    for wid in (w1, w2):
        await client.put(f"/api/v1/words/word/{wid}/status", json={"status": "learning"}, headers=headers)

    resp = await client.get(SRS_DUE, params={"language": "de", "limit": 1}, headers=headers)

    assert resp.status_code == 200
    assert len(resp.json()) == 1


# ---------------------------------------------------------------------------
# POST /srs/review/{card_id}
# ---------------------------------------------------------------------------

async def test_submit_correct_answer_returns_success_response(client: AsyncClient, db_pool, srs_word):
    word_id, _, language = srs_word
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    card_id = await _mark_learning_and_get_card_id(client, headers, language, word_id)

    resp = await client.post(f"/api/v1/srs/review/{card_id}", json={"correct": True}, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"card_id": card_id, "success": True}


async def test_correct_answer_advances_sm2_interval_and_repetitions(client: AsyncClient, db_pool, srs_word):
    """
    Correct answer on a 'create'-initialized card (interval=1.0, rep=0) should:
      new_interval = 1.0 * 2.5 = 2.5, repetitions = 1.
    """
    word_id, _, language = srs_word
    headers, uid = await _register_and_get_user(client, db_pool, _email())
    card_id = await _mark_learning_and_get_card_id(client, headers, language, word_id)

    await client.post(f"/api/v1/srs/review/{card_id}", json={"correct": True}, headers=headers)

    card = await _get_srs_card(db_pool, uid, word_id, "word", "passive")
    assert card is not None
    assert card["repetitions"] == 1
    assert card["interval_days"] > 1.0


async def test_incorrect_answer_resets_sm2_to_one_day(client: AsyncClient, db_pool, srs_word):
    """
    After one correct then one incorrect answer:
      interval = 1.0, repetitions = 0.
    """
    word_id, _, language = srs_word
    headers, uid = await _register_and_get_user(client, db_pool, _email())
    card_id = await _mark_learning_and_get_card_id(client, headers, language, word_id)

    # Advance first so there's something to reset
    await client.post(f"/api/v1/srs/review/{card_id}", json={"correct": True}, headers=headers)
    # Now penalise (submit_answer does not require due_date <= NOW)
    await client.post(f"/api/v1/srs/review/{card_id}", json={"correct": False}, headers=headers)

    card = await _get_srs_card(db_pool, uid, word_id, "word", "passive")
    assert card["interval_days"] == 1.0
    assert card["repetitions"] == 0


async def test_submit_answer_for_another_users_card_returns_404(client: AsyncClient, db_pool, srs_word):
    """Users must not be able to submit answers for cards they don't own."""
    word_id, _, language = srs_word
    headers_a, _ = await _register_and_get_user(client, db_pool, _email())
    headers_b, _ = await _register_and_get_user(client, db_pool, _email())

    card_id = await _mark_learning_and_get_card_id(client, headers_a, language, word_id)

    resp = await client.post(f"/api/v1/srs/review/{card_id}", json={"correct": True}, headers=headers_b)

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# W5 / Hole 14 — POST /srs/review/{card_id}/skip
# ---------------------------------------------------------------------------

SKIP_URL = "/api/v1/srs/review/{card_id}/skip"


async def test_skip_moves_due_date_into_the_future(client: AsyncClient, db_pool, srs_word):
    word_id, _, language = srs_word
    headers, uid = await _register_and_get_user(client, db_pool, _email())
    card_id = await _mark_learning_and_get_card_id(client, headers, language, word_id)

    before = await _get_srs_card(db_pool, uid, word_id, "word", "passive")
    resp = await client.post(SKIP_URL.format(card_id=card_id), headers=headers)
    assert resp.status_code == 200

    after = await _get_srs_card(db_pool, uid, word_id, "word", "passive")
    assert after["due_date"] > before["due_date"]
    # 1-day defer (review_service.SKIP_DEFER_DAYS). Use a wide window for clock drift.
    from datetime import timedelta
    delta = after["due_date"] - before["due_date"]
    assert timedelta(hours=23) <= delta <= timedelta(hours=25)


async def test_skip_does_not_change_repetitions(client: AsyncClient, db_pool, srs_word):
    word_id, _, language = srs_word
    headers, uid = await _register_and_get_user(client, db_pool, _email())
    card_id = await _mark_learning_and_get_card_id(client, headers, language, word_id)
    before = await _get_srs_card(db_pool, uid, word_id, "word", "passive")

    await client.post(SKIP_URL.format(card_id=card_id), headers=headers)
    after = await _get_srs_card(db_pool, uid, word_id, "word", "passive")

    assert after["repetitions"] == before["repetitions"]


async def test_skip_does_not_change_interval_days(client: AsyncClient, db_pool, srs_word):
    word_id, _, language = srs_word
    headers, uid = await _register_and_get_user(client, db_pool, _email())
    card_id = await _mark_learning_and_get_card_id(client, headers, language, word_id)
    before = await _get_srs_card(db_pool, uid, word_id, "word", "passive")

    await client.post(SKIP_URL.format(card_id=card_id), headers=headers)
    after = await _get_srs_card(db_pool, uid, word_id, "word", "passive")

    assert after["interval_days"] == before["interval_days"]


async def test_skip_does_not_change_ease_factor(client: AsyncClient, db_pool, srs_word):
    word_id, _, language = srs_word
    headers, uid = await _register_and_get_user(client, db_pool, _email())
    card_id = await _mark_learning_and_get_card_id(client, headers, language, word_id)
    before = await _get_srs_card(db_pool, uid, word_id, "word", "passive")

    await client.post(SKIP_URL.format(card_id=card_id), headers=headers)
    after = await _get_srs_card(db_pool, uid, word_id, "word", "passive")

    assert after["ease_factor"] == before["ease_factor"]


async def test_skip_does_not_change_levels_or_times_used_correctly(client: AsyncClient, db_pool, srs_word):
    """Skip is scheduling, not evidence — never touches user_word_knowledge fields."""
    word_id, _, language = srs_word
    headers, uid = await _register_and_get_user(client, db_pool, _email())
    card_id = await _mark_learning_and_get_card_id(client, headers, language, word_id)

    before = await db_pool.fetchrow(
        """
        SELECT passive_level, active_level, times_used_correctly, status
          FROM user_word_knowledge
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
        """,
        uid, word_id,
    )
    await client.post(SKIP_URL.format(card_id=card_id), headers=headers)
    after = await db_pool.fetchrow(
        """
        SELECT passive_level, active_level, times_used_correctly, status
          FROM user_word_knowledge
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
        """,
        uid, word_id,
    )
    assert after["passive_level"] == before["passive_level"]
    assert after["active_level"] == before["active_level"]
    assert after["times_used_correctly"] == before["times_used_correctly"]
    assert after["status"] == before["status"]


async def test_skipped_card_no_longer_appears_in_due_immediately(client: AsyncClient, db_pool, srs_word):
    word_id, _, language = srs_word
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    card_id = await _mark_learning_and_get_card_id(client, headers, language, word_id)

    # Before skip: card is due now.
    due_before = await client.get(
        f"/api/v1/srs/due?language={language}&limit=50",
        headers=headers,
    )
    assert any(c["card_id"] == card_id for c in due_before.json())

    await client.post(SKIP_URL.format(card_id=card_id), headers=headers)

    due_after = await client.get(
        f"/api/v1/srs/due?language={language}&limit=50",
        headers=headers,
    )
    assert not any(c["card_id"] == card_id for c in due_after.json()), (
        "skipped card must not surface in /srs/due immediately"
    )


async def test_skip_other_users_card_returns_404(client: AsyncClient, db_pool, srs_word):
    word_id, _, language = srs_word
    headers_a, _ = await _register_and_get_user(client, db_pool, _email())
    headers_b, _ = await _register_and_get_user(client, db_pool, _email())
    card_id = await _mark_learning_and_get_card_id(client, headers_a, language, word_id)

    resp = await client.post(SKIP_URL.format(card_id=card_id), headers=headers_b)
    assert resp.status_code == 404


async def test_skip_missing_card_returns_404(client: AsyncClient, db_pool):
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    resp = await client.post(SKIP_URL.format(card_id=999999), headers=headers)
    assert resp.status_code == 404


async def test_skip_does_not_emit_usage_event(client: AsyncClient, db_pool, srs_word):
    """Regression: skip must not write to word_usage_events (it's not evidence)."""
    word_id, _, language = srs_word
    headers, uid = await _register_and_get_user(client, db_pool, _email())
    card_id = await _mark_learning_and_get_card_id(client, headers, language, word_id)

    # Baseline count BEFORE skip (status_marked_learning has already fired one
    # status_change event when we marked the word learning).
    count_before = await db_pool.fetchval(
        "SELECT COUNT(*) FROM word_usage_events WHERE user_id = $1::uuid",
        uid,
    )
    await client.post(SKIP_URL.format(card_id=card_id), headers=headers)
    count_after = await db_pool.fetchval(
        "SELECT COUNT(*) FROM word_usage_events WHERE user_id = $1::uuid",
        uid,
    )
    assert count_after == count_before, "skip must not generate analytics events"
