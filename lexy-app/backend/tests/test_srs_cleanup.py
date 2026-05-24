"""
Hole 10 — `srs_cleanup_service` orphan-cleanup tests.

An orphan `srs_cards` row has no matching `user_word_knowledge` row for
`(user_id, item_id, item_type)`. The service must:
  - detect them
  - delete only them on --apply
  - never touch valid rows
  - be idempotent
"""
from __future__ import annotations

import pytest

from backend.services.srs_cleanup_service import (
    cleanup_orphan_srs_cards,
    find_orphan_srs_cards,
)
from ._email_helper import make_test_email


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


async def _insert_srs(pool, user_id: str, item_id: int, item_type: str, direction: str) -> int:
    row = await pool.fetchrow(
        """
        INSERT INTO srs_cards
            (user_id, item_id, item_type, direction,
             due_date, interval_days, ease_factor, repetitions)
        VALUES ($1::uuid, $2, $3, $4, NOW(), 1.0, 2.5, 0)
        RETURNING card_id
        """,
        user_id, item_id, item_type, direction,
    )
    return row["card_id"]


async def _insert_uwk(pool, user_id: str, item_id: int, item_type: str, status: str = "learning") -> None:
    await pool.execute(
        """
        INSERT INTO user_word_knowledge
            (user_id, item_id, item_type, status,
             passive_level, active_level, times_seen, times_used_correctly)
        VALUES ($1::uuid, $2, $3, $4, 0, 0, 0, 0)
        """,
        user_id, item_id, item_type, status,
    )


def _has(rows, card_id: int) -> bool:
    return any(r["card_id"] == card_id for r in rows)


# ---------------------------------------------------------------------------
# Audit query
# ---------------------------------------------------------------------------

async def test_audit_detects_orphan_card(db_pool):
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    orphan_id = await _insert_srs(db_pool, uid, wid, "word", "passive")

    rows = await find_orphan_srs_cards(db_pool)
    assert _has(rows, orphan_id)


async def test_audit_ignores_card_with_matching_uwk_row(db_pool):
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _insert_uwk(db_pool, uid, wid, "word")
    valid_id = await _insert_srs(db_pool, uid, wid, "word", "passive")

    rows = await find_orphan_srs_cards(db_pool)
    assert not _has(rows, valid_id)


# ---------------------------------------------------------------------------
# Dry-run vs. apply
# ---------------------------------------------------------------------------

async def test_dry_run_does_not_delete(db_pool):
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    orphan_id = await _insert_srs(db_pool, uid, wid, "word", "passive")

    result = await cleanup_orphan_srs_cards(db_pool, apply=False)
    assert result["dry_run"] is True
    assert result["deleted"] == 0
    assert result["found"] >= 1

    row = await db_pool.fetchrow("SELECT card_id FROM srs_cards WHERE card_id = $1", orphan_id)
    assert row is not None


async def test_apply_deletes_only_orphans(db_pool):
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)

    # One orphan + one valid card (sharing user + item to make the cleanup
    # tighter; the valid one has a uwk row, the orphan doesn't).
    orphan_id = await _insert_srs(db_pool, uid, wid, "word", "passive")
    await _insert_uwk(db_pool, uid, wid, "word")
    # The valid card must differ by direction since the uniq key is
    # (user, item_id, item_type, direction).
    valid_id  = await _insert_srs(db_pool, uid, wid, "word", "active")

    # The orphan above shares (user, item, item_type) with the valid row's
    # uwk, so it ISN'T actually an orphan once we plant the uwk. Fix by
    # planting the orphan on a *different* item_id that has no uwk anywhere.
    other_wid = await _get_word_id(db_pool)  # a second distinct owned word
    real_orphan_id = await _insert_srs(db_pool, uid, other_wid, "word", "passive")

    # `orphan_id` is no longer orphan after we planted the uwk row above.
    # Clean it up so the assertions stay focused.
    await db_pool.execute("DELETE FROM srs_cards WHERE card_id = $1", orphan_id)

    result = await cleanup_orphan_srs_cards(db_pool, apply=True)
    assert result["dry_run"] is False
    assert result["deleted"] >= 1

    # Real orphan gone
    assert await db_pool.fetchrow("SELECT 1 FROM srs_cards WHERE card_id = $1", real_orphan_id) is None
    # Valid card untouched
    assert await db_pool.fetchrow("SELECT 1 FROM srs_cards WHERE card_id = $1", valid_id) is not None


async def test_apply_does_not_touch_user_word_knowledge(db_pool):
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _insert_uwk(db_pool, uid, wid, "word")
    other_wid = await _get_word_id(db_pool)  # a second distinct owned word
    await _insert_srs(db_pool, uid, other_wid, "word", "passive")  # orphan

    pre = await db_pool.fetchval(
        "SELECT COUNT(*) FROM user_word_knowledge WHERE user_id = $1::uuid", uid,
    )
    await cleanup_orphan_srs_cards(db_pool, apply=True)
    post = await db_pool.fetchval(
        "SELECT COUNT(*) FROM user_word_knowledge WHERE user_id = $1::uuid", uid,
    )
    assert pre == post == 1


async def test_idempotent_second_apply_deletes_zero(db_pool):
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _insert_srs(db_pool, uid, wid, "word", "passive")  # orphan

    first  = await cleanup_orphan_srs_cards(db_pool, apply=True)
    second = await cleanup_orphan_srs_cards(db_pool, apply=True)

    assert first["deleted"] >= 1
    assert second["deleted"] == 0


# ---------------------------------------------------------------------------
# Safety: shared catalog untouched
# ---------------------------------------------------------------------------

async def test_apply_does_not_touch_word_or_phrase_tables(db_pool):
    uid = await _create_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _insert_srs(db_pool, uid, wid, "word", "passive")  # orphan

    pre_words   = await db_pool.fetchval("SELECT COUNT(*) FROM word_table")
    pre_phrases = await db_pool.fetchval("SELECT COUNT(*) FROM phrase_table")
    pre_grammar = await db_pool.fetchval("SELECT COUNT(*) FROM grammar_rule_table")

    await cleanup_orphan_srs_cards(db_pool, apply=True)

    assert await db_pool.fetchval("SELECT COUNT(*) FROM word_table")         == pre_words
    assert await db_pool.fetchval("SELECT COUNT(*) FROM phrase_table")       == pre_phrases
    assert await db_pool.fetchval("SELECT COUNT(*) FROM grammar_rule_table") == pre_grammar
