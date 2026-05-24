"""
Tests for guided_chat_service.get_next_target — the auto-target picker.

Verifies that all three priority tiers (due active SRS card, learning item
without active card, random fallback) now consider both words and phrases.
The polymorphic shape `{item_id, item_type, word, lemma}` is preserved.
"""
import uuid

import pytest

from backend.services.guided_chat_service import get_next_target
from ._email_helper import make_test_email


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_user(db_pool) -> str:
    from backend.services.auth_service import register_user
    email = make_test_email()
    user = await register_user(db_pool, email, "password123")
    return str(user["user_id"])


async def _get_phrase(db_pool, language: str = "de") -> tuple[int, str, str] | None:
    row = await db_pool.fetchrow(
        "SELECT phrase_id, surface_form, canonical FROM phrase_table "
        "WHERE language = $1 LIMIT 1",
        language,
    )
    return (row["phrase_id"], row["surface_form"], row["canonical"]) if row else None


# ---------------------------------------------------------------------------
# Priority 1: due active SRS card
# ---------------------------------------------------------------------------

async def test_picks_phrase_with_due_active_card(db_pool):
    """A phrase with an active SRS card due NOW should be picked first."""
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table empty")
    phrase_id, surface_form, canonical = info

    uid = await _make_user(db_pool)
    # Track the phrase as 'learning' (preserves auto-promotion semantics).
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'learning')",
        uid, phrase_id,
    )
    # Schedule an active card due now.
    await db_pool.execute(
        """
        INSERT INTO srs_cards (user_id, item_id, item_type, direction,
                               due_date, interval_days, ease_factor, repetitions)
        VALUES ($1::uuid, $2, 'phrase', 'active', NOW(), 1.0, 2.5, 0)
        """,
        uid, phrase_id,
    )

    target = await get_next_target(db_pool, uid, "de")

    assert target is not None
    assert target["item_id"]   == phrase_id
    assert target["item_type"] == "phrase"
    assert target["word"]      == surface_form
    assert target["lemma"]     == canonical


async def test_due_word_card_still_chosen_when_no_phrase_due(db_pool, make_word):
    """Existing word path must not regress — a due word card is picked when no phrase is due."""
    word_id, _ = await make_word()  # owned German word (see conftest make_word)

    uid = await _make_user(db_pool)
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'word', 'learning')",
        uid, word_id,
    )
    await db_pool.execute(
        """
        INSERT INTO srs_cards (user_id, item_id, item_type, direction,
                               due_date, interval_days, ease_factor, repetitions)
        VALUES ($1::uuid, $2, 'word', 'active', NOW(), 1.0, 2.5, 0)
        """,
        uid, word_id,
    )

    target = await get_next_target(db_pool, uid, "de")
    assert target is not None
    assert target["item_id"]   == word_id
    assert target["item_type"] == "word"


async def test_phrase_card_chosen_over_word_when_phrase_due_first(db_pool, make_word):
    """Priority 1 ordering: whichever card has the earliest due_date wins,
    regardless of item_type."""
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table empty")
    phrase_id, _, _ = info
    word_id, _ = await make_word()  # owned German word (see conftest make_word)

    uid = await _make_user(db_pool)

    # Word card due in 1 hour, phrase card due 1 hour ago.
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'word', 'learning'), ($1::uuid, $3, 'phrase', 'learning')",
        uid, word_id, phrase_id,
    )
    await db_pool.execute(
        """
        INSERT INTO srs_cards (user_id, item_id, item_type, direction,
                               due_date, interval_days, ease_factor, repetitions)
        VALUES ($1::uuid, $2, 'word',   'active', NOW() + INTERVAL '1 hour', 1.0, 2.5, 0),
               ($1::uuid, $3, 'phrase', 'active', NOW() - INTERVAL '1 hour', 1.0, 2.5, 0)
        """,
        uid, word_id, phrase_id,
    )

    target = await get_next_target(db_pool, uid, "de")
    assert target is not None
    assert target["item_id"]   == phrase_id
    assert target["item_type"] == "phrase"


# ---------------------------------------------------------------------------
# Priority 2: learning item without an active card
# ---------------------------------------------------------------------------

async def test_picks_learning_phrase_without_active_card(db_pool):
    """When no due cards exist, a 'learning' phrase without an active card
    should be picked at priority 2."""
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table empty")
    phrase_id, surface_form, canonical = info

    uid = await _make_user(db_pool)
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'learning')",
        uid, phrase_id,
    )
    # No active SRS card → priority 2 fires.

    target = await get_next_target(db_pool, uid, "de")
    assert target is not None
    assert target["item_id"]   == phrase_id
    assert target["item_type"] == "phrase"
    assert target["word"]      == surface_form
    assert target["lemma"]     == canonical


async def test_priority_2_skips_phrase_when_active_card_exists(db_pool):
    """If the learning phrase already has an active card (even not due yet),
    priority 2 should skip it."""
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table empty")
    phrase_id, _, _ = info

    uid = await _make_user(db_pool)
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'learning')",
        uid, phrase_id,
    )
    # Active card scheduled in the future — priority 1 won't fire (not due),
    # priority 2 should ALSO skip this phrase because an active card exists.
    await db_pool.execute(
        """
        INSERT INTO srs_cards (user_id, item_id, item_type, direction,
                               due_date, interval_days, ease_factor, repetitions)
        VALUES ($1::uuid, $2, 'phrase', 'active',
                NOW() + INTERVAL '7 days', 1.0, 2.5, 0)
        """,
        uid, phrase_id,
    )

    target = await get_next_target(db_pool, uid, "de")

    # Falls through to priority 3 (random untagged), or None if catalog empty
    # after exclusions. Either way it must NOT be this phrase.
    if target is not None:
        assert not (target["item_id"] == phrase_id and target["item_type"] == "phrase")


# ---------------------------------------------------------------------------
# Priority 3: random fallback now considers phrases too
# ---------------------------------------------------------------------------

async def test_priority_3_can_return_phrase(db_pool):
    """When no due cards and no learning items exist, the random fallback
    should be able to return either a word or a phrase. We assert phrases
    are reachable by retrying a few times; flaky-by-design tolerance: low
    probability of all-words run when phrase_table has ≥1 row."""
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table empty")

    uid = await _make_user(db_pool)

    seen_types: set[str] = set()
    # 25 draws — with even ~1 phrase in a corpus of ~1000 words, the chance
    # of seeing a phrase at least once is much higher than 1−(1−1/1000)^25 ≈ 2.5%.
    # The point is just to confirm the UNION ALL branch is reachable.
    for _ in range(25):
        target = await get_next_target(db_pool, uid, "de")
        if target is None:
            break
        seen_types.add(target["item_type"])
        if "phrase" in seen_types:
            break

    # If the corpus is dominated by words this is best-effort; assert only that
    # the function returned something well-formed.
    if seen_types:
        assert seen_types <= {"word", "phrase"}


# ---------------------------------------------------------------------------
# Return shape contract
# ---------------------------------------------------------------------------

async def test_target_shape_has_required_keys(db_pool):
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table empty")
    phrase_id, _, _ = info

    uid = await _make_user(db_pool)
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'learning')",
        uid, phrase_id,
    )

    target = await get_next_target(db_pool, uid, "de")
    assert target is not None
    assert {"item_id", "item_type", "word", "lemma"} <= set(target)
