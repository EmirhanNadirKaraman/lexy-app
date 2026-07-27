"""
Transcript reading-stats tests.

Endpoint: GET /api/v1/videos/{video_id}/reading-stats

Tests hit the real development DB via the `client` fixture. Tests that need
actual video/word data will be skipped if those tables are empty or missing.
"""

import asyncpg
import pytest
from httpx import AsyncClient
from ._email_helper import make_test_email

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
STATS    = "/api/v1/videos/{video_id}/reading-stats"
WORD_PUT = "/api/v1/words/word/{word_id}/status"


# ---------------------------------------------------------------------------
# Local fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def conn(db_pool):
    """Dedicated DB connection for direct queries in this module.

    Using an explicit acquire() means each test gets a clean connection.
    If a helper raises a PostgresError (e.g. table missing), the connection
    is cleanly returned to the pool rather than left in a broken state.
    """
    async with db_pool.acquire() as connection:
        yield connection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_email() -> str:
    return make_test_email()


async def _token(client: AsyncClient) -> str:
    email = make_email()
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    resp = await client.post(LOGIN, json={"email": email, "password": "password123"})
    return resp.json()["access_token"]


async def _video_with_words(conn: asyncpg.Connection) -> str:
    """Return a video_id that has sentences + word rows, or skip the test."""
    try:
        row = await conn.fetchrow("""
            SELECT DISTINCT s.video_id
            FROM sentence s
            JOIN word_to_sentence wts ON wts.sentence_id = s.sentence_id
            JOIN word_table wt        ON wt.word_id = wts.word_id
            LIMIT 1
        """)
    except asyncpg.PostgresError:
        pytest.skip("sentence/word tables not found — run the subtitle pipeline first")
    if row is None:
        pytest.skip("No populated videos in DB — run the subtitle pipeline first")
    return row["video_id"]


async def _word_in_video(conn: asyncpg.Connection, video_id: str) -> int:
    """Return any word_id that appears in the given video."""
    return await conn.fetchval("""
        SELECT wts.word_id
        FROM sentence s
        JOIN word_to_sentence wts ON wts.sentence_id = s.sentence_id
        WHERE s.video_id = $1
        LIMIT 1
    """, video_id)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def test_stats_requires_auth(client: AsyncClient):
    # Auth check happens before any DB access — no real video_id needed
    resp = await client.get("/api/v1/videos/any-video-id/reading-stats")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------

async def test_stats_response_has_required_fields(client: AsyncClient, conn):
    video_id = await _video_with_words(conn)
    token = await _token(client)

    resp = await client.get(
        STATS.format(video_id=video_id),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    data = resp.json()
    for field in ("video_id", "total_lemmas", "known", "learning", "unknown",
                  "known_pct", "learning_pct", "unknown_pct"):
        assert field in data, f"missing field: {field}"
    assert data["video_id"] == video_id


# ---------------------------------------------------------------------------
# Fresh user — no knowledge yet → everything unknown
# ---------------------------------------------------------------------------

async def test_stats_fresh_user_all_unknown(client: AsyncClient, conn):
    video_id = await _video_with_words(conn)
    token = await _token(client)

    data = (await client.get(
        STATS.format(video_id=video_id),
        headers={"Authorization": f"Bearer {token}"},
    )).json()

    assert data["known"] == 0
    assert data["learning"] == 0
    assert data["unknown"] == data["total_lemmas"]
    assert data["known_pct"] == 0.0
    assert data["learning_pct"] == 0.0


# ---------------------------------------------------------------------------
# Marking words updates stats
# ---------------------------------------------------------------------------

async def test_marking_word_known_increments_known(client: AsyncClient, conn):
    video_id = await _video_with_words(conn)
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    word_id = await _word_in_video(conn, video_id)

    before = (await client.get(STATS.format(video_id=video_id), headers=headers)).json()

    await client.put(
        WORD_PUT.format(word_id=word_id),
        json={"status": "known"},
        headers=headers,
    )

    after = (await client.get(STATS.format(video_id=video_id), headers=headers)).json()

    assert after["known"] >= before["known"] + 1
    assert after["unknown"] <= before["unknown"]
    assert after["total_lemmas"] == before["total_lemmas"]


async def test_marking_word_learning_increments_learning(client: AsyncClient, conn):
    video_id = await _video_with_words(conn)
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    word_id = await _word_in_video(conn, video_id)

    await client.put(
        WORD_PUT.format(word_id=word_id),
        json={"status": "learning"},
        headers=headers,
    )

    data = (await client.get(STATS.format(video_id=video_id), headers=headers)).json()

    assert data["learning"] >= 1
    assert data["known"] == 0


async def test_upgrading_learning_to_known_moves_between_buckets(client: AsyncClient, conn):
    video_id = await _video_with_words(conn)
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    word_id = await _word_in_video(conn, video_id)

    await client.put(WORD_PUT.format(word_id=word_id), json={"status": "learning"}, headers=headers)
    mid = (await client.get(STATS.format(video_id=video_id), headers=headers)).json()
    assert mid["learning"] >= 1

    await client.put(WORD_PUT.format(word_id=word_id), json={"status": "known"}, headers=headers)
    after = (await client.get(STATS.format(video_id=video_id), headers=headers)).json()

    assert after["known"] >= 1
    assert after["total_lemmas"] == mid["total_lemmas"]


# ---------------------------------------------------------------------------
# Percentages
# ---------------------------------------------------------------------------

async def test_percentages_sum_to_100(client: AsyncClient, conn):
    video_id = await _video_with_words(conn)
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}
    word_id = await _word_in_video(conn, video_id)

    await client.put(WORD_PUT.format(word_id=word_id), json={"status": "known"}, headers=headers)

    data = (await client.get(STATS.format(video_id=video_id), headers=headers)).json()
    total = data["known_pct"] + data["learning_pct"] + data["unknown_pct"]

    assert abs(total - 100.0) < 0.5  # allow for ROUND(..., 1) rounding


# ---------------------------------------------------------------------------
# Lemma deduplication
# ---------------------------------------------------------------------------

async def test_lemma_dedup_multiple_forms_count_once(client: AsyncClient, conn):
    """Marking every surface form of a multi-form lemma must not inflate total_lemmas."""
    try:
        row = await conn.fetchrow("""
            SELECT wt.lemma, array_agg(DISTINCT wt.word_id) AS word_ids
            FROM word_table wt
            GROUP BY wt.lemma
            HAVING COUNT(DISTINCT wt.word_id) >= 2
            LIMIT 1
        """)
    except asyncpg.PostgresError:
        pytest.skip("word_table not found")

    if row is None:
        pytest.skip("No lemma with multiple surface forms in DB")

    video_id = await _video_with_words(conn)
    token = await _token(client)
    headers = {"Authorization": f"Bearer {token}"}

    before = (await client.get(STATS.format(video_id=video_id), headers=headers)).json()

    for word_id in row["word_ids"]:
        await client.put(WORD_PUT.format(word_id=word_id), json={"status": "known"}, headers=headers)

    after = (await client.get(STATS.format(video_id=video_id), headers=headers)).json()

    assert after["total_lemmas"] == before["total_lemmas"]


# ---------------------------------------------------------------------------
# Non-existent video returns zeros, not 404
# ---------------------------------------------------------------------------

async def test_unknown_video_returns_zeros(client: AsyncClient):
    token = await _token(client)

    data = (await client.get(
        "/api/v1/videos/nonexistent-video-id-xyz/reading-stats",
        headers={"Authorization": f"Bearer {token}"},
    )).json()

    assert data["total_lemmas"] == 0
    assert data["known"] == 0
    assert data["unknown"] == 0
    assert data["known_pct"] == 0.0
