"""
W6 / Hole 11+16 — `srs_backfill_service` audit + backfill tests.

The service exists to repair pre-#0b data: items marked `status='learning'`
without a matching active SRS card. New writes since #0b create both
directions, so the audit count should reach zero after this backfill
runs once.

Each test plants its OWN learning row + manipulates `srs_cards` directly
to simulate the pre-#0b shape — we can't rely on real word_table state
for assertions of "exactly 1 missing row" because production data may
have its own quirks.
"""
from __future__ import annotations

import pytest

from backend.services.srs_backfill_service import (
    backfill_missing_active_cards,
    find_missing_active_cards,
)
from ._email_helper import cleanup_pattern, make_test_email


async def _create_user(pool) -> str:
    row = await pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING user_id",
        make_test_email(),
    )
    return str(row["user_id"])


async def _get_word_id(pool) -> int:
    # Owned, uniquely-named word (xdist-safe — see conftest / docs/TESTS.md).
    from ._word_helper import insert_owned_word
    wid, _ = await insert_owned_word(pool)
    return wid


async def _get_phrase_id(pool) -> int | None:
    row = await pool.fetchrow("SELECT phrase_id FROM phrase_table LIMIT 1")
    return row["phrase_id"] if row else None


async def _get_grammar_rule_id(pool) -> int | None:
    row = await pool.fetchrow("SELECT rule_id FROM grammar_rule_table LIMIT 1")
    return row["rule_id"] if row else None


async def _plant_pre_0b_learning(pool, user_id: str, item_id: int, item_type: str) -> None:
    """Insert a `learning` row + ONLY a passive SRS card (the pre-#0b shape)."""
    await pool.execute(
        """
        INSERT INTO user_word_knowledge
            (user_id, item_id, item_type, status, passive_level, active_level,
             times_seen, times_used_correctly)
        VALUES ($1::uuid, $2, $3, 'learning', 1, 0, 1, 0)
        """,
        user_id, item_id, item_type,
    )
    await pool.execute(
        """
        INSERT INTO srs_cards
            (user_id, item_id, item_type, direction,
             due_date, interval_days, ease_factor, repetitions)
        VALUES ($1::uuid, $2, $3, 'passive', NOW(), 1.0, 2.5, 0)
        """,
        user_id, item_id, item_type,
    )


def _matches(rows: list[dict], user_id: str, item_id: int, item_type: str) -> bool:
    return any(
        r["user_id"] == user_id and r["item_id"] == item_id and r["item_type"] == item_type
        for r in rows
    )


# ---------------------------------------------------------------------------
# Audit query — finds the right rows and ignores the wrong ones
# ---------------------------------------------------------------------------

async def test_audit_finds_learning_word_missing_active_srs(db_pool):
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _plant_pre_0b_learning(db_pool, uid, wid, "word")

    rows = await find_missing_active_cards(db_pool)
    assert _matches(rows, uid, wid, "word")


async def test_audit_finds_learning_phrase_missing_active_srs(db_pool):
    pid = await _get_phrase_id(db_pool)
    if pid is None:
        pytest.skip("phrase_table empty")
    uid = await _create_user(db_pool)
    await _plant_pre_0b_learning(db_pool, uid, pid, "phrase")

    rows = await find_missing_active_cards(db_pool)
    assert _matches(rows, uid, pid, "phrase")


async def test_audit_ignores_grammar_rule_rows(db_pool):
    """grammar_rule is passive-only — never gets an active card."""
    rid = await _get_grammar_rule_id(db_pool)
    if rid is None:
        pytest.skip("grammar_rule_table empty")
    uid = await _create_user(db_pool)
    await _plant_pre_0b_learning(db_pool, uid, rid, "grammar_rule")

    rows = await find_missing_active_cards(db_pool)
    assert not _matches(rows, uid, rid, "grammar_rule")


async def test_audit_ignores_known_and_unknown_status_rows(db_pool):
    """Only `learning` rows belong in the active queue."""
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    # Manually set status='known', no active card.
    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge
            (user_id, item_id, item_type, status, passive_level, active_level,
             times_seen, times_used_correctly)
        VALUES ($1::uuid, $2, 'word', 'known', 5, 3, 5, 3)
        """,
        uid, wid,
    )
    rows = await find_missing_active_cards(db_pool)
    assert not _matches(rows, uid, wid, "word")


async def test_audit_ignores_rows_that_already_have_active_srs_card(db_pool):
    """The whole point of the audit — rows already in good shape stay quiet."""
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _plant_pre_0b_learning(db_pool, uid, wid, "word")
    # Add the missing active card so this row is no longer in the audit.
    await db_pool.execute(
        """
        INSERT INTO srs_cards
            (user_id, item_id, item_type, direction,
             due_date, interval_days, ease_factor, repetitions)
        VALUES ($1::uuid, $2, 'word', 'active', NOW(), 1.0, 2.5, 0)
        """,
        uid, wid,
    )
    rows = await find_missing_active_cards(db_pool)
    assert not _matches(rows, uid, wid, "word")


# ---------------------------------------------------------------------------
# Backfill — inserts the missing card with the right defaults
# ---------------------------------------------------------------------------

async def test_backfill_inserts_active_card_with_expected_defaults(db_pool):
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _plant_pre_0b_learning(db_pool, uid, wid, "word")

    result = await backfill_missing_active_cards(db_pool, apply=True)
    assert result["inserted"] >= 1

    card = await db_pool.fetchrow(
        """
        SELECT direction, interval_days, ease_factor, repetitions
          FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
           AND direction = 'active'
        """,
        uid, wid,
    )
    assert card is not None, "backfill must insert the missing active card"
    assert card["direction"]     == "active"
    assert card["interval_days"] == 1.0
    assert card["ease_factor"]   == 2.5
    assert card["repetitions"]   == 0


async def test_backfill_does_not_mutate_user_word_knowledge(db_pool):
    """Skip is scheduling-only — never touches levels/status/times-correct."""
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _plant_pre_0b_learning(db_pool, uid, wid, "word")

    before = await db_pool.fetchrow(
        """
        SELECT status, passive_level, active_level, times_seen, times_used_correctly
          FROM user_word_knowledge
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
        """,
        uid, wid,
    )
    await backfill_missing_active_cards(db_pool, apply=True)
    after = await db_pool.fetchrow(
        """
        SELECT status, passive_level, active_level, times_seen, times_used_correctly
          FROM user_word_knowledge
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
        """,
        uid, wid,
    )
    assert after == before


async def test_backfill_is_idempotent(db_pool):
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _plant_pre_0b_learning(db_pool, uid, wid, "word")

    first = await backfill_missing_active_cards(db_pool, apply=True)
    second = await backfill_missing_active_cards(db_pool, apply=True)

    assert first["inserted"] >= 1
    assert second["inserted"] == 0, "second apply must be a no-op"

    # Exactly one active row exists for this (user, word).
    count = await db_pool.fetchval(
        """
        SELECT COUNT(*) FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2
           AND item_type = 'word' AND direction = 'active'
        """,
        uid, wid,
    )
    assert count == 1


async def test_backfill_dry_run_does_not_insert(db_pool):
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _plant_pre_0b_learning(db_pool, uid, wid, "word")

    result = await backfill_missing_active_cards(db_pool, apply=False)
    assert result["dry_run"] is True
    assert result["inserted"] == 0
    assert result["found"] >= 1

    # Confirm no active card was created.
    card = await db_pool.fetchrow(
        """
        SELECT card_id FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2
           AND item_type = 'word' AND direction = 'active'
        """,
        uid, wid,
    )
    assert card is None, "dry-run must not insert"


async def test_backfill_does_not_touch_passive_srs_card(db_pool):
    """Regression: the existing passive card shouldn't shift."""
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _plant_pre_0b_learning(db_pool, uid, wid, "word")

    before = await db_pool.fetchrow(
        """
        SELECT due_date, interval_days, ease_factor, repetitions
          FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
           AND direction = 'passive'
        """,
        uid, wid,
    )
    await backfill_missing_active_cards(db_pool, apply=True)
    after = await db_pool.fetchrow(
        """
        SELECT due_date, interval_days, ease_factor, repetitions
          FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
           AND direction = 'passive'
        """,
        uid, wid,
    )
    assert after == before


# Silence: ensure these tests' planted rows don't pollute neighbours.
# The autouse cleanup fixture matches by email LIKE cleanup_pattern() so
# the user (and CASCADE'd rows) are pruned per worker — no manual teardown.
_ = cleanup_pattern  # re-export silence; the fixture handles cleanup
