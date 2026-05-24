"""
End-to-end progression loop test (#29).

One golden-path integration test through the main learning loop, exercised
via real HTTP endpoints + real DB. Locks the contract after the recent
progression/SRS patches:
  - #0a-2  active SRS review uses typed production via /produce
  - #0b    status_marked_learning creates BOTH passive + active SRS cards
  - Hole 9 passive_level ≥ threshold while status='learning' → 'known'
  - Hole 26 manual demotion resets levels and reschedules cards

Flow:
  1. Register a user.
  2. Mark a German word as 'learning' → both SRS cards exist, active_level=0.
  3. GET /srs/due → passive + active cards present for the word.
  4. POST /srs/review/{passive} correct=true → passive_level grows, passive
     SRS advances (interval > 1), active card untouched.
  5. POST /srs/review/{active}/produce {answer: <exact word>} → exact-match
     fast path (no LLM); active_level grows, times_used_correctly bumps,
     active card advances.
  6. Repeat #5 until active_level reaches ACTIVE_MASTERY_THRESHOLD → status='known'.
  7. GET /srs/due → the word is filtered out (uwk.status='known').
  8. PUT status='learning' (manual demote known → learning, Hole 26 'reset'):
     passive_level=1, active_level=0, both cards rescheduled (interval=1, reps=0,
     ease preserved).
  9. PUT status='unknown' (manual demote learning → unknown):
     passive_level=0, active_level=0, both cards reset via 'incorrect'
     (interval=1, reps=0, ease drops 0.15).

The LLM is mocked via `llm_service._MOCK = True` so the test never hits Anthropic.
The active-direction fast path takes care of evaluation without needing the
mock evaluator: when the typed answer normalises to the target text, the
endpoint short-circuits to correct=True before any LLM call.
"""
import uuid

import pytest
from httpx import AsyncClient

from backend.services import llm_service
from backend.services.progression_service import ACTIVE_MASTERY_THRESHOLD
from ._email_helper import make_test_email


REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
SRS_DUE  = "/api/v1/srs/due"


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _force_mock_llm(monkeypatch):
    """Golden-path test must not depend on Anthropic. Mock returns a stable
    string for any item gloss; the active-produce fast path skips LLM entirely
    when the answer matches the target exactly."""
    monkeypatch.setattr(llm_service, "_MOCK", True)


def _email() -> str:
    return make_test_email()


async def _register(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    """Register + login. Returns (auth_headers, user_id_str)."""
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    r = await client.post(LOGIN, json={"email": email, "password": "password123"})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    uid = str(await db_pool.fetchval("SELECT user_id FROM users WHERE email = $1", email))
    return headers, uid


async def _get_uwk(db_pool, uid: str, word_id: int) -> dict | None:
    row = await db_pool.fetchrow(
        """
        SELECT status, passive_level, active_level,
               times_seen, times_used_correctly
          FROM user_word_knowledge
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
        """,
        uid, word_id,
    )
    return dict(row) if row else None


async def _get_card(db_pool, uid: str, word_id: int, direction: str) -> dict | None:
    row = await db_pool.fetchrow(
        """
        SELECT card_id, interval_days, ease_factor, repetitions, due_date
          FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2
           AND item_type = 'word' AND direction = $3
        """,
        uid, word_id, direction,
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# The golden-path test
# ---------------------------------------------------------------------------

async def test_e2e_learning_loop_golden_path(client: AsyncClient, db_pool, make_word):
    """Full learning-loop traversal through real HTTP + real DB."""
    headers, uid = await _register(client, db_pool, _email())
    word_id, word_surface = await make_word()  # owned German word (conftest make_word)

    # ----- 1. Mark Learning -----
    r = await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    uwk = await _get_uwk(db_pool, uid, word_id)
    assert uwk is not None
    assert uwk["status"] == "learning"
    assert uwk["passive_level"] == 1   # status_marked_learning passive_delta=1
    assert uwk["active_level"]  == 0   # #0b: active card created but no active credit
    assert uwk["times_used_correctly"] == 0

    passive_card = await _get_card(db_pool, uid, word_id, "passive")
    active_card  = await _get_card(db_pool, uid, word_id, "active")
    assert passive_card is not None, "#0b: passive card must exist after marking learning"
    assert active_card  is not None, "#0b: active card must exist after marking learning"
    assert passive_card["repetitions"] == 0
    assert active_card["repetitions"]  == 0

    # ----- 2. /srs/due returns both directions for the word -----
    r = await client.get(SRS_DUE, params={"language": "de"}, headers=headers)
    assert r.status_code == 200, r.text
    due = r.json()
    word_cards = [c for c in due if c["item_id"] == word_id]
    assert {c["direction"] for c in word_cards} == {"passive", "active"}, \
        "both directions must be due immediately after marking learning"
    passive_card_id = next(c["card_id"] for c in word_cards if c["direction"] == "passive")
    active_card_id  = next(c["card_id"] for c in word_cards if c["direction"] == "active")

    # ----- 3. Passive correct review -----
    r = await client.post(
        f"/api/v1/srs/review/{passive_card_id}",
        json={"correct": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    uwk = await _get_uwk(db_pool, uid, word_id)
    # 1 (learning) + 1 (passive_review_correct) = 2.
    assert uwk["passive_level"] == 2
    assert uwk["active_level"]  == 0   # passive review must not touch active side
    assert uwk["times_used_correctly"] == 0

    passive_after = await _get_card(db_pool, uid, word_id, "passive")
    assert passive_after["repetitions"] == 1
    assert passive_after["interval_days"] > 1.0   # SM-2 advanced past the day-1 default

    active_after = await _get_card(db_pool, uid, word_id, "active")
    assert active_after["repetitions"]   == 0
    assert active_after["interval_days"] == 1.0   # untouched by passive review

    # ----- 4. Active production via exact-match fast path -----
    r = await client.post(
        f"/api/v1/srs/review/{active_card_id}/produce",
        json={"answer": word_surface},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    resp = r.json()
    assert resp["correct"] is True
    assert resp["expected"] == word_surface
    assert "Exact match" in resp["feedback"]   # confirms we hit the fast path (no LLM)

    uwk = await _get_uwk(db_pool, uid, word_id)
    # passive_delta=1 + active_delta=1 from active_review_correct.
    # passive was 2 → 3, active was 0 → 1.
    assert uwk["passive_level"] == 3
    assert uwk["active_level"]  == 1
    assert uwk["times_used_correctly"] == 1
    assert uwk["status"] == "learning"

    active_after = await _get_card(db_pool, uid, word_id, "active")
    assert active_after["repetitions"]   == 1
    assert active_after["interval_days"] > 1.0   # SM-2 advanced

    # ----- 5. Continue producing until active_level hits the mastery threshold -----
    # active_review_correct gives active_delta=1. We're at active_level=1. Need
    # ACTIVE_MASTERY_THRESHOLD - 1 more correct productions.
    for _ in range(ACTIVE_MASTERY_THRESHOLD - 1):
        r = await client.post(
            f"/api/v1/srs/review/{active_card_id}/produce",
            json={"answer": word_surface},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["correct"] is True

    uwk = await _get_uwk(db_pool, uid, word_id)
    assert uwk["status"] == "known", \
        f"active_level should have crossed the mastery threshold; got {uwk}"
    assert uwk["active_level"] == ACTIVE_MASTERY_THRESHOLD
    assert uwk["times_used_correctly"] == ACTIVE_MASTERY_THRESHOLD

    # ----- 6. /srs/due filters out 'known' items -----
    r = await client.get(SRS_DUE, params={"language": "de"}, headers=headers)
    word_cards_after_known = [c for c in r.json() if c["item_id"] == word_id]
    assert word_cards_after_known == [], \
        "review_service.get_due_cards must exclude items with status='known'"

    # ----- 7. Manual demote known → learning (Hole 26 'reset') -----
    passive_before_demote = await _get_card(db_pool, uid, word_id, "passive")
    active_before_demote  = await _get_card(db_pool, uid, word_id, "active")
    passive_ease_before = passive_before_demote["ease_factor"]
    active_ease_before  = active_before_demote["ease_factor"]
    # Sanity: ease climbed during the correct reviews above (+0.05 each).
    assert passive_ease_before > 2.5
    assert active_ease_before  > 2.5

    r = await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    uwk = await _get_uwk(db_pool, uid, word_id)
    assert uwk["status"] == "learning"
    assert uwk["passive_level"] == 1   # Hole 26 policy: reset to 1
    assert uwk["active_level"]  == 0   # Hole 26 policy: reset to 0
    # times_used_correctly preserved (records history).
    assert uwk["times_used_correctly"] == ACTIVE_MASTERY_THRESHOLD

    passive_after_demote = await _get_card(db_pool, uid, word_id, "passive")
    active_after_demote  = await _get_card(db_pool, uid, word_id, "active")
    # 'reset' action: interval=1, reps=0, ease preserved.
    assert passive_after_demote["interval_days"] == 1.0
    assert passive_after_demote["repetitions"]   == 0
    assert passive_after_demote["ease_factor"]   == passive_ease_before
    assert active_after_demote["interval_days"]  == 1.0
    assert active_after_demote["repetitions"]    == 0
    assert active_after_demote["ease_factor"]    == active_ease_before

    # ----- 8. Manual demote learning → unknown -----
    passive_ease_pre_unknown = passive_after_demote["ease_factor"]
    active_ease_pre_unknown  = active_after_demote["ease_factor"]

    r = await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "unknown"},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    uwk = await _get_uwk(db_pool, uid, word_id)
    assert uwk["status"] == "unknown"
    assert uwk["passive_level"] == 0
    assert uwk["active_level"]  == 0
    # Counters still preserved.
    assert uwk["times_used_correctly"] == ACTIVE_MASTERY_THRESHOLD

    passive_unknown = await _get_card(db_pool, uid, word_id, "passive")
    active_unknown  = await _get_card(db_pool, uid, word_id, "active")
    # 'incorrect' action: reset existing cards, ease drops 0.15 (floored 1.3).
    assert passive_unknown["interval_days"] == 1.0
    assert passive_unknown["repetitions"]   == 0
    assert passive_unknown["ease_factor"]    < passive_ease_pre_unknown
    assert active_unknown["interval_days"]  == 1.0
    assert active_unknown["repetitions"]    == 0
    assert active_unknown["ease_factor"]     < active_ease_pre_unknown


async def test_e2e_demote_unknown_does_not_fabricate_missing_active_card(
    client: AsyncClient, db_pool, make_word,
):
    """Companion to the golden path: a user who reached 'known' via the manual
    confidence click (no production events, so no active card was ever created)
    then demotes to 'unknown'. The demotion must NOT fabricate an active card —
    the 'incorrect' action is documented as a no-op when the card is missing.
    """
    headers, uid = await _register(client, db_pool, _email())
    word_id, _ = await make_word()  # owned German word (conftest make_word)

    # Reach 'known' via manual click only. No active card is created by
    # status_marked_known (active_srs=None on that rule).
    r = await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "known"},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    assert (await _get_uwk(db_pool, uid, word_id))["status"] == "known"
    assert await _get_card(db_pool, uid, word_id, "active") is None

    # Demote to unknown.
    r = await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "unknown"},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    uwk = await _get_uwk(db_pool, uid, word_id)
    assert uwk["status"] == "unknown"
    assert uwk["passive_level"] == 0
    assert uwk["active_level"]  == 0

    # The critical assertion: no active card was fabricated by the demotion.
    assert await _get_card(db_pool, uid, word_id, "active") is None
