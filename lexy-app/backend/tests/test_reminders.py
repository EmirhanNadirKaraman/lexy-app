"""
Reminder summary endpoint.

Covers GET /api/v1/reminders/summary — the small aggregation used by the
frontend reminder banner to decide whether to nag the user.
"""
import uuid

import pytest
from httpx import AsyncClient
from ._email_helper import make_test_email

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
URL      = "/api/v1/reminders/summary"


def _email() -> str:
    return make_test_email()


async def _register(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    r = await client.post(LOGIN, json={"email": email, "password": "password123"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    uid = str(await db_pool.fetchval("SELECT user_id FROM users WHERE email = $1", email))
    return headers, uid


async def test_summary_returns_expected_keys(client: AsyncClient, db_pool):
    headers, _ = await _register(client, db_pool, _email())

    resp = await client.get(URL, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert {"srs_due_count", "reading_due_count", "learning_item_count",
            "total_due", "has_anything_due"} <= set(body)


async def test_summary_is_zero_for_new_user(client: AsyncClient, db_pool):
    headers, _ = await _register(client, db_pool, _email())

    body = (await client.get(URL, headers=headers)).json()

    assert body["srs_due_count"]      == 0
    assert body["reading_due_count"]  == 0
    assert body["learning_item_count"] == 0
    assert body["total_due"]          == 0
    assert body["has_anything_due"]   is False


async def test_summary_counts_learning_word(client: AsyncClient, db_pool, srs_word):
    word_id, _, _ = srs_word
    headers, _ = await _register(client, db_pool, _email())

    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    body = (await client.get(URL, headers=headers)).json()

    assert body["learning_item_count"] >= 1
    # status_marked_learning also creates a due passive card (and now active too)
    assert body["srs_due_count"] >= 1
    assert body["has_anything_due"] is True


async def test_summary_does_not_count_known(client: AsyncClient, db_pool, srs_word):
    word_id, _, _ = srs_word
    headers, _ = await _register(client, db_pool, _email())

    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "known"},
        headers=headers,
    )

    body = (await client.get(URL, headers=headers)).json()

    assert body["srs_due_count"] == 0          # known cards excluded
    assert body["learning_item_count"] == 0    # only learning counted


async def test_summary_requires_auth(client: AsyncClient):
    resp = await client.get(URL)
    assert resp.status_code in (401, 403)
