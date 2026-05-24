"""
Transcript click → progression tests.

Source of truth: lexy-app/features/transcript-click-progression/tests.md

Covers:
  Unit (no DB):
    - compute_delta("transcript_clicked") shape: passive_delta=1, times_seen_delta=1,
      passive_srs="create", active_delta=0, active_srs=None
  Integration (real DB):
    - POST returns 204
    - passive_level incremented by 1
    - times_seen incremented by 1
    - First click creates passive SRS card with default SM-2 values
    - Second click increments passive_level again but does NOT advance the SRS card
    - word_id < 1 (path value 0) returns 422
    - Unauthenticated request returns 401
"""
import uuid

import pytest
from httpx import AsyncClient

from backend.services.progression_service import compute_delta
from ._email_helper import make_test_email

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"


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


async def _get_word(db_pool) -> int:
    # Owned, uniquely-named word (xdist-safe — see conftest / docs/TESTS.md).
    from ._word_helper import insert_owned_word
    wid, _ = await insert_owned_word(db_pool)
    return wid


async def _get_knowledge(db_pool, uid: str, word_id: int) -> dict | None:
    row = await db_pool.fetchrow(
        """
        SELECT passive_level, times_seen
          FROM user_word_knowledge
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
        """,
        uid, word_id,
    )
    return dict(row) if row else None


async def _get_passive_srs_card(db_pool, uid: str, word_id: int) -> dict | None:
    row = await db_pool.fetchrow(
        """
        SELECT interval_days, ease_factor, repetitions, due_date
          FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2
           AND item_type = 'word' AND direction = 'passive'
        """,
        uid, word_id,
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Unit test — compute_delta
# ---------------------------------------------------------------------------

def test_transcript_clicked_delta():
    d = compute_delta("transcript_clicked")
    assert d.passive_delta == 1
    assert d.times_seen_delta == 1
    assert d.passive_srs == "create"
    assert d.active_delta == 0
    assert d.active_srs is None


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

async def test_transcript_click_returns_204(client: AsyncClient, db_pool):
    word_id = await _get_word(db_pool)
    headers, _ = await _register_and_login(client, db_pool, _email())

    resp = await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers)

    assert resp.status_code == 204


async def test_transcript_click_increments_passive_level(client: AsyncClient, db_pool):
    word_id = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers)

    row = await _get_knowledge(db_pool, uid, word_id)
    assert row is not None
    assert row["passive_level"] == 1


async def test_transcript_click_increments_times_seen(client: AsyncClient, db_pool):
    word_id = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers)

    row = await _get_knowledge(db_pool, uid, word_id)
    assert row is not None
    assert row["times_seen"] == 1


async def test_first_click_creates_passive_srs_card_with_defaults(client: AsyncClient, db_pool):
    """The 'create' SRS action inserts with interval=1.0, ease=2.5, reps=0."""
    word_id = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers)

    card = await _get_passive_srs_card(db_pool, uid, word_id)
    assert card is not None
    assert card["interval_days"] == 1.0
    assert card["ease_factor"] == 2.5
    assert card["repetitions"] == 0


async def test_second_click_does_not_advance_srs_card(client: AsyncClient, db_pool):
    """
    'create' is a no-op when a card already exists. The second click should
    NOT change interval_days, ease_factor, or repetitions.
    """
    word_id = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    # First click — creates card
    await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers)
    card_after_first = await _get_passive_srs_card(db_pool, uid, word_id)

    # Second click — card must stay identical
    await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers)
    card_after_second = await _get_passive_srs_card(db_pool, uid, word_id)

    assert card_after_second["interval_days"] == card_after_first["interval_days"]
    assert card_after_second["ease_factor"]   == card_after_first["ease_factor"]
    assert card_after_second["repetitions"]   == card_after_first["repetitions"]

    # passive_level must have incremented a second time
    row = await _get_knowledge(db_pool, uid, word_id)
    assert row["passive_level"] == 2


async def test_transcript_click_word_id_zero_returns_422(client: AsyncClient, db_pool):
    """FastAPI Path(ge=1) rejects word_id=0 before the handler runs."""
    headers, _ = await _register_and_login(client, db_pool, _email())

    resp = await client.post("/api/v1/words/word/0/transcript-click", headers=headers)

    assert resp.status_code == 422


async def test_transcript_click_requires_auth(client: AsyncClient, db_pool):
    word_id = await _get_word(db_pool)

    resp = await client.post(f"/api/v1/words/word/{word_id}/transcript-click")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Per-sentence dedup (T1.1) — sentence_id present in the body
# ---------------------------------------------------------------------------

async def _get_two_sentences(db_pool) -> tuple[int, int]:
    rows = await db_pool.fetch("SELECT sentence_id FROM sentence ORDER BY sentence_id LIMIT 2")
    if len(rows) < 2:
        pytest.skip("sentence table has fewer than 2 rows — run the subtitle pipeline first")
    return rows[0]["sentence_id"], rows[1]["sentence_id"]


async def _get_two_words(db_pool) -> tuple[int, int]:
    # Two distinct owned words (xdist-safe — see conftest / docs/TESTS.md).
    from ._word_helper import insert_owned_word
    w1, _ = await insert_owned_word(db_pool)
    w2, _ = await insert_owned_word(db_pool)
    return w1, w2


async def test_first_transcript_click_with_sentence_applies_progression(client, db_pool):
    """First click with sentence_id increments passive_level + times_seen."""
    word_id = await _get_word(db_pool)
    sid, _ = await _get_two_sentences(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    resp = await client.post(
        f"/api/v1/words/word/{word_id}/transcript-click",
        headers=headers,
        json={"sentence_id": sid},
    )

    assert resp.status_code == 204
    row = await _get_knowledge(db_pool, uid, word_id)
    assert row["passive_level"] == 1
    assert row["times_seen"] == 1


async def test_duplicate_transcript_click_same_sentence_is_noop(client, db_pool):
    """
    Second click on same (word, sentence, user, day) must NOT inflate
    passive_level or times_seen. This is the core T1.1 guarantee.
    """
    word_id = await _get_word(db_pool)
    sid, _ = await _get_two_sentences(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    body = {"sentence_id": sid}
    r1 = await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers, json=body)
    r2 = await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers, json=body)

    assert r1.status_code == 204
    # Duplicate returns 204 too — caller can't tell first from repeat.
    assert r2.status_code == 204
    row = await _get_knowledge(db_pool, uid, word_id)
    assert row["passive_level"] == 1, "duplicate click must not inflate passive_level"
    assert row["times_seen"] == 1, "duplicate click must not inflate times_seen"


async def test_different_sentence_same_word_still_counts(client, db_pool):
    """Same word in a different sentence — dedup scope does not apply."""
    word_id = await _get_word(db_pool)
    sid1, sid2 = await _get_two_sentences(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    await client.post(f"/api/v1/words/word/{word_id}/transcript-click",
                     headers=headers, json={"sentence_id": sid1})
    await client.post(f"/api/v1/words/word/{word_id}/transcript-click",
                     headers=headers, json={"sentence_id": sid2})

    row = await _get_knowledge(db_pool, uid, word_id)
    assert row["passive_level"] == 2


async def test_different_user_same_word_and_sentence_still_counts(client, db_pool):
    """Dedup is scoped per-user. User B's first click should fire progression."""
    word_id = await _get_word(db_pool)
    sid, _ = await _get_two_sentences(db_pool)
    headers_a, uid_a = await _register_and_login(client, db_pool, _email())
    headers_b, uid_b = await _register_and_login(client, db_pool, _email())

    body = {"sentence_id": sid}
    await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers_a, json=body)
    await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers_b, json=body)

    row_a = await _get_knowledge(db_pool, uid_a, word_id)
    row_b = await _get_knowledge(db_pool, uid_b, word_id)
    assert row_a["passive_level"] == 1
    assert row_b["passive_level"] == 1


async def test_different_word_same_sentence_still_counts(client, db_pool):
    """Dedup is per (user, item). A different word in the same sentence still counts."""
    word_a, word_b = await _get_two_words(db_pool)
    sid, _ = await _get_two_sentences(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    body = {"sentence_id": sid}
    await client.post(f"/api/v1/words/word/{word_a}/transcript-click", headers=headers, json=body)
    await client.post(f"/api/v1/words/word/{word_b}/transcript-click", headers=headers, json=body)

    row_a = await _get_knowledge(db_pool, uid, word_a)
    row_b = await _get_knowledge(db_pool, uid, word_b)
    assert row_a["passive_level"] == 1
    assert row_b["passive_level"] == 1


async def test_duplicate_click_does_not_inflate_srs_card(client, db_pool):
    """
    A duplicate click must not advance the SRS card either — second click is
    a no-op end-to-end, not just on level counters.
    """
    word_id = await _get_word(db_pool)
    sid, _ = await _get_two_sentences(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    body = {"sentence_id": sid}
    await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers, json=body)
    card_after_first = await _get_passive_srs_card(db_pool, uid, word_id)

    await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers, json=body)
    card_after_second = await _get_passive_srs_card(db_pool, uid, word_id)

    assert card_after_second == card_after_first


async def test_duplicate_click_does_not_double_record_usage_event(client, db_pool):
    """The word_usage_events row is what the unique index enforces."""
    word_id = await _get_word(db_pool)
    sid, _ = await _get_two_sentences(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    body = {"sentence_id": sid}
    await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers, json=body)
    await client.post(f"/api/v1/words/word/{word_id}/transcript-click", headers=headers, json=body)

    count = await db_pool.fetchval(
        """
        SELECT COUNT(*) FROM word_usage_events
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
           AND context = 'transcript' AND sentence_id = $3
        """,
        uid, word_id, sid,
    )
    assert count == 1
