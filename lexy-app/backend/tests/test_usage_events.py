"""
Word usage events tests.

Integration tests (real DB via db_pool fixture):
  - record_event inserts a row
  - most_frequent_unknown_items returns items with status='unknown'
  - most_frequent_learning_items returns items with status='learning'
  - recently_failed_items returns items with outcome='incorrect', ordered by recency
  - most_interacted_items counts all events in window
  - aggregations return empty list when there are no events
  - since_days window excludes old events
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.services.usage_events_service import (
    most_frequent_unknown_items,
    most_frequent_learning_items,
    most_interacted_items,
    record_event,
    recently_failed_items,
)
from ._email_helper import make_test_email


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_created_word_ids: list[int] = []  # reaped after each test by the autouse fixture below


@pytest.fixture(autouse=True)
async def _reap_created_words(db_pool):
    """Reap words created by `_make_word` after each test — xdist-safe isolation.

    Was: `_make_word` returned a SHARED catalog row (cached `SELECT ... LIMIT 20`).
    Under `pytest -n auto` another worker could reap a cached row mid-test, so
    aggregations that join word_table (`most_frequent_unknown_items` et al.)
    dropped it and the test flaked. `_make_word` now inserts an owned word per
    call; this fixture deletes exactly those rows afterwards.
    """
    yield
    if _created_word_ids:
        await db_pool.execute(
            "DELETE FROM word_table WHERE word_id = ANY($1::int[])", _created_word_ids
        )
        _created_word_ids.clear()


async def _make_word(pool, offset: int = 0) -> int:
    """Create and return a fresh, uniquely-named word_id (owned + reaped).

    `offset` is retained for call-site compatibility but no longer changes the
    result — every call yields a distinct word, which is all the callers need.
    """
    surface = f"_testword_{uuid.uuid4().hex[:12]}"
    wid = await pool.fetchval(
        "INSERT INTO word_table (word, language, pos, tag, lemma) "
        "VALUES ($1, 'de', 'NOUN', 'NN', $1) RETURNING word_id",
        surface,
    )
    _created_word_ids.append(wid)
    return wid


async def _make_user(pool, email: str) -> str:
    """Register a user via auth_service and return user_id as str."""
    from backend.services.auth_service import register_user
    user = await register_user(pool, email, "password123")
    return str(user["user_id"])


async def _set_status(pool, user_id: str, item_id: int, item_type: str, status_val: str):
    await pool.execute(
        """
        INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
        VALUES ($1::uuid, $2, $3, $4)
        ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = EXCLUDED.status
        """,
        user_id, item_id, item_type, status_val,
    )


# ---------------------------------------------------------------------------
# Tests: record_event
# ---------------------------------------------------------------------------

async def test_record_event_inserts_row(db_pool):
    email = make_test_email()
    user_id = await _make_user(db_pool, email)
    word_id = await _make_word(db_pool, 0)

    await record_event(db_pool, user_id, word_id, "word", "status_change", "seen")

    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM word_usage_events WHERE user_id = $1::uuid AND item_id = $2",
        user_id, word_id,
    )
    assert count == 1


async def test_record_event_stores_metadata(db_pool):
    email = make_test_email()
    user_id = await _make_user(db_pool, email)
    word_id = await _make_word(db_pool, 0)

    await record_event(
        db_pool, user_id, word_id, "word", "guided_chat", "correct",
        metadata={"session_id": "abc123"},
    )

    row = await db_pool.fetchrow(
        "SELECT metadata FROM word_usage_events WHERE user_id = $1::uuid AND item_id = $2",
        user_id, word_id,
    )
    import json
    meta = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else dict(row["metadata"])
    assert meta["session_id"] == "abc123"


# ---------------------------------------------------------------------------
# Tests: most_frequent_unknown_items
# ---------------------------------------------------------------------------

async def test_most_frequent_unknown_returns_only_unknown(db_pool):
    email = make_test_email()
    user_id = await _make_user(db_pool, email)
    unknown_id = await _make_word(db_pool, 0)
    learning_id = await _make_word(db_pool, 1)

    await _set_status(db_pool, user_id, unknown_id, "word", "unknown")
    await _set_status(db_pool, user_id, learning_id, "word", "learning")

    for _ in range(3):
        await record_event(db_pool, user_id, unknown_id, "word", "free_chat", "seen")
    for _ in range(2):
        await record_event(db_pool, user_id, learning_id, "word", "free_chat", "seen")

    result = await most_frequent_unknown_items(db_pool, user_id)
    item_ids = [r["item_id"] for r in result]
    assert unknown_id in item_ids
    assert learning_id not in item_ids


async def test_most_frequent_unknown_ordered_by_count(db_pool):
    email = make_test_email()
    user_id = await _make_user(db_pool, email)
    w1 = await _make_word(db_pool, 0)
    w2 = await _make_word(db_pool, 1)

    for wid in (w1, w2):
        await _set_status(db_pool, user_id, wid, "word", "unknown")

    for _ in range(5):
        await record_event(db_pool, user_id, w1, "word", "free_chat", "seen")
    for _ in range(2):
        await record_event(db_pool, user_id, w2, "word", "free_chat", "seen")

    result = await most_frequent_unknown_items(db_pool, user_id)
    counts = {r["item_id"]: r["event_count"] for r in result}
    assert counts[w1] > counts[w2]


# ---------------------------------------------------------------------------
# Tests: most_frequent_learning_items
# ---------------------------------------------------------------------------

async def test_most_frequent_learning_returns_only_learning(db_pool):
    email = make_test_email()
    user_id = await _make_user(db_pool, email)
    learning_id = await _make_word(db_pool, 0)
    known_id = await _make_word(db_pool, 1)

    await _set_status(db_pool, user_id, learning_id, "word", "learning")
    await _set_status(db_pool, user_id, known_id, "word", "known")

    for _ in range(3):
        await record_event(db_pool, user_id, learning_id, "word", "guided_chat", "used")
    for _ in range(3):
        await record_event(db_pool, user_id, known_id, "word", "guided_chat", "correct")

    result = await most_frequent_learning_items(db_pool, user_id)
    item_ids = [r["item_id"] for r in result]
    assert learning_id in item_ids
    assert known_id not in item_ids


# ---------------------------------------------------------------------------
# Tests: recently_failed_items
# ---------------------------------------------------------------------------

async def test_recently_failed_returns_incorrect_outcomes(db_pool):
    email = make_test_email()
    user_id = await _make_user(db_pool, email)
    word_id = await _make_word(db_pool, 0)

    await record_event(db_pool, user_id, word_id, "word", "srs_review", "incorrect")
    await record_event(db_pool, user_id, word_id, "word", "srs_review", "incorrect")

    result = await recently_failed_items(db_pool, user_id)
    item = next((r for r in result if r["item_id"] == word_id), None)
    assert item is not None
    assert item["fail_count"] == 2
    assert item["last_failed"] is not None


async def test_recently_failed_excludes_correct_outcomes(db_pool):
    email = make_test_email()
    user_id = await _make_user(db_pool, email)
    word_id = await _make_word(db_pool, 0)

    await record_event(db_pool, user_id, word_id, "word", "srs_review", "correct")

    result = await recently_failed_items(db_pool, user_id)
    item_ids = [r["item_id"] for r in result]
    assert word_id not in item_ids


# ---------------------------------------------------------------------------
# Tests: most_interacted_items
# ---------------------------------------------------------------------------

async def test_most_interacted_counts_all_outcomes(db_pool):
    email = make_test_email()
    user_id = await _make_user(db_pool, email)
    word_id = await _make_word(db_pool, 0)

    for outcome in ("seen", "used", "correct", "incorrect"):
        await record_event(db_pool, user_id, word_id, "word", "guided_chat", outcome)

    result = await most_interacted_items(db_pool, user_id)
    item = next((r for r in result if r["item_id"] == word_id), None)
    assert item is not None
    assert item["total_interactions"] == 4


async def test_most_interacted_respects_since_days_window(db_pool):
    email = make_test_email()
    user_id = await _make_user(db_pool, email)
    word_id = await _make_word(db_pool, 0)

    # Insert an old event directly, bypassing record_event
    old_ts = datetime.now(timezone.utc) - timedelta(days=60)
    await db_pool.execute(
        """
        INSERT INTO word_usage_events (user_id, item_id, item_type, context, outcome, created_at)
        VALUES ($1::uuid, $2, 'word', 'free_chat', 'seen', $3)
        """,
        user_id, word_id, old_ts,
    )

    # since_days=30 should not include the 60-day-old event
    result = await most_interacted_items(db_pool, user_id, since_days=30)
    item_ids = [r["item_id"] for r in result]
    assert word_id not in item_ids


# ---------------------------------------------------------------------------
# Tests: empty / no events
# ---------------------------------------------------------------------------

async def test_all_aggregations_return_empty_for_new_user(db_pool):
    email = make_test_email()
    user_id = await _make_user(db_pool, email)

    assert await most_frequent_unknown_items(db_pool, user_id) == []
    assert await most_frequent_learning_items(db_pool, user_id) == []
    assert await recently_failed_items(db_pool, user_id) == []
    assert await most_interacted_items(db_pool, user_id) == []
