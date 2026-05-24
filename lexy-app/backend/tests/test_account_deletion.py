"""
Account self-deletion endpoint.

Covers DELETE /api/v1/account — the user-initiated permanent account delete
that lands all cascade fan-out via Postgres FK declarations (no
service-layer mirror needed). Verifies:
  - the user's own row is gone
  - private cascade tables drain
  - shared catalog tables (word_table, phrase_table, …) are untouched
  - other users are unaffected
  - auth is required
  - a second delete with the same token returns 401 (user no longer exists)
"""
import pytest
from httpx import AsyncClient

from ._email_helper import make_test_email

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
URL      = "/api/v1/account"


async def _register(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    r = await client.post(LOGIN, json={"email": email, "password": "password123"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    uid = str(await db_pool.fetchval("SELECT user_id FROM users WHERE email = $1", email))
    return headers, uid


async def _seed_private_rows(db_pool, user_id: str) -> None:
    """Plant a row in each cascade table so we can confirm the delete fans out."""
    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge (user_id, item_id, item_type, status,
            passive_level, active_level, times_seen, times_used_correctly)
        VALUES ($1::uuid, 999999, 'word', 'learning', 0, 0, 0, 0)
        """,
        user_id,
    )
    await db_pool.execute(
        """
        INSERT INTO srs_cards (user_id, item_id, item_type, direction,
            due_date, interval_days, ease_factor, repetitions)
        VALUES ($1::uuid, 999999, 'word', 'passive', NOW(), 1.0, 2.5, 0)
        """,
        user_id,
    )
    await db_pool.execute(
        """
        INSERT INTO word_usage_events (user_id, item_id, item_type, context, outcome)
        VALUES ($1::uuid, 999999, 'word', 'transcript', 'seen')
        """,
        user_id,
    )


async def test_authenticated_user_can_delete_account(client, db_pool):
    headers, uid = await _register(client, db_pool, make_test_email())
    await _seed_private_rows(db_pool, uid)

    r = await client.request("DELETE", URL, headers=headers, json={"password": "password123"})

    assert r.status_code == 204
    assert r.content == b""

    # Users row gone
    row = await db_pool.fetchrow("SELECT user_id FROM users WHERE user_id = $1::uuid", uid)
    assert row is None


async def test_cascade_clears_private_user_data(client, db_pool):
    headers, uid = await _register(client, db_pool, make_test_email())
    await _seed_private_rows(db_pool, uid)

    # Pre: rows exist
    pre_uwk  = await db_pool.fetchval("SELECT COUNT(*) FROM user_word_knowledge WHERE user_id = $1::uuid", uid)
    pre_srs  = await db_pool.fetchval("SELECT COUNT(*) FROM srs_cards            WHERE user_id = $1::uuid", uid)
    pre_evt  = await db_pool.fetchval("SELECT COUNT(*) FROM word_usage_events    WHERE user_id = $1::uuid", uid)
    assert pre_uwk == 1 and pre_srs == 1 and pre_evt == 1

    await client.request("DELETE", URL, headers=headers, json={"password": "password123"})

    # Post: all gone via ON DELETE CASCADE
    post_uwk = await db_pool.fetchval("SELECT COUNT(*) FROM user_word_knowledge WHERE user_id = $1::uuid", uid)
    post_srs = await db_pool.fetchval("SELECT COUNT(*) FROM srs_cards            WHERE user_id = $1::uuid", uid)
    post_evt = await db_pool.fetchval("SELECT COUNT(*) FROM word_usage_events    WHERE user_id = $1::uuid", uid)
    assert post_uwk == 0 and post_srs == 0 and post_evt == 0


async def test_shared_catalog_data_preserved(client, db_pool):
    headers, _uid = await _register(client, db_pool, make_test_email())

    pre_words    = await db_pool.fetchval("SELECT COUNT(*) FROM word_table")
    pre_phrases  = await db_pool.fetchval("SELECT COUNT(*) FROM phrase_table")
    pre_grammar  = await db_pool.fetchval("SELECT COUNT(*) FROM grammar_rule_table")
    pre_channels = await db_pool.fetchval("SELECT COUNT(*) FROM channel")
    pre_videos   = await db_pool.fetchval("SELECT COUNT(*) FROM video")

    r = await client.request("DELETE", URL, headers=headers, json={"password": "password123"})
    assert r.status_code == 204

    assert await db_pool.fetchval("SELECT COUNT(*) FROM word_table")         == pre_words
    assert await db_pool.fetchval("SELECT COUNT(*) FROM phrase_table")       == pre_phrases
    assert await db_pool.fetchval("SELECT COUNT(*) FROM grammar_rule_table") == pre_grammar
    assert await db_pool.fetchval("SELECT COUNT(*) FROM channel")            == pre_channels
    assert await db_pool.fetchval("SELECT COUNT(*) FROM video")              == pre_videos


async def test_unauthenticated_request_rejected(client):
    r = await client.delete(URL)
    # FastAPI's HTTPBearer raises 403 when the Authorization header is missing
    # entirely; 401 once a header is present but invalid. Either form is a
    # rejection — we just need to ensure the route is auth-gated.
    assert r.status_code in (401, 403)


async def test_invalid_token_rejected(client):
    r = await client.delete(URL, headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


async def test_delete_does_not_affect_other_user(client, db_pool):
    headers_a, uid_a = await _register(client, db_pool, make_test_email())
    headers_b, uid_b = await _register(client, db_pool, make_test_email())
    await _seed_private_rows(db_pool, uid_b)

    r = await client.request("DELETE", URL, headers=headers_a, json={"password": "password123"})
    assert r.status_code == 204

    # B is untouched
    assert await db_pool.fetchval("SELECT user_id FROM users WHERE user_id = $1::uuid", uid_b) is not None
    assert await db_pool.fetchval("SELECT COUNT(*) FROM user_word_knowledge WHERE user_id = $1::uuid", uid_b) == 1
    assert await db_pool.fetchval("SELECT COUNT(*) FROM srs_cards WHERE user_id = $1::uuid", uid_b)            == 1
    # And B can still authenticate
    me = await client.get("/api/v1/words/knowledge", headers=headers_b)
    assert me.status_code == 200


async def test_second_delete_with_same_token_returns_401(client, db_pool):
    headers, _uid = await _register(client, db_pool, make_test_email())

    r1 = await client.request("DELETE", URL, headers=headers, json={"password": "password123"})
    assert r1.status_code == 204

    r2 = await client.request("DELETE", URL, headers=headers, json={"password": "password123"})
    # get_current_user raises 401 "User not found" once the row is gone.
    assert r2.status_code == 401


# ---------------------------------------------------------------------------
# Password re-authentication (S3)
# ---------------------------------------------------------------------------

async def test_delete_with_no_body_is_rejected_and_keeps_account(client, db_pool):
    """The stolen-token case: a bare DELETE (valid token, no body/password) must
    NOT delete the account — it now requires password re-auth."""
    headers, uid = await _register(client, db_pool, make_test_email())

    r = await client.delete(URL, headers=headers)  # no JSON body at all
    assert r.status_code == 403
    assert await db_pool.fetchval("SELECT user_id FROM users WHERE user_id = $1::uuid", uid) is not None


async def test_delete_with_empty_password_is_rejected_and_keeps_account(client, db_pool):
    headers, uid = await _register(client, db_pool, make_test_email())

    r = await client.request("DELETE", URL, headers=headers, json={})
    assert r.status_code == 403
    assert await db_pool.fetchval("SELECT user_id FROM users WHERE user_id = $1::uuid", uid) is not None


async def test_delete_with_wrong_password_is_rejected_and_keeps_account(client, db_pool):
    headers, uid = await _register(client, db_pool, make_test_email())
    await _seed_private_rows(db_pool, uid)

    r = await client.request("DELETE", URL, headers=headers, json={"password": "not-the-password"})
    assert r.status_code == 403
    # Account and its cascade data are untouched.
    assert await db_pool.fetchval("SELECT user_id FROM users WHERE user_id = $1::uuid", uid) is not None
    assert await db_pool.fetchval(
        "SELECT COUNT(*) FROM srs_cards WHERE user_id = $1::uuid", uid
    ) == 1


async def test_content_request_user_id_set_null_on_delete(client, db_pool):
    """content_request uses ON DELETE SET NULL so failed-request audit survives."""
    headers, uid = await _register(client, db_pool, make_test_email())

    req_id = await db_pool.fetchval(
        """
        INSERT INTO content_request (user_id, request_type, content_id, status)
        VALUES ($1::uuid, 'channel', 'UC_test_delete_' || substr(md5(random()::text), 1, 8), 'pending')
        RETURNING request_id
        """,
        uid,
    )

    r = await client.request("DELETE", URL, headers=headers, json={"password": "password123"})
    assert r.status_code == 204

    # Row survives, user_id anonymised
    row = await db_pool.fetchrow(
        "SELECT user_id, status FROM content_request WHERE request_id = $1", req_id,
    )
    assert row is not None
    assert row["user_id"] is None

    # Cleanup
    await db_pool.execute("DELETE FROM content_request WHERE request_id = $1", req_id)


async def test_client_error_log_user_id_set_null_on_delete(client, db_pool):
    """client_error_log uses ON DELETE SET NULL so crash signal survives anonymised."""
    headers, uid = await _register(client, db_pool, make_test_email())

    err_id = await db_pool.fetchval(
        """
        INSERT INTO client_error_log (user_id, message)
        VALUES ($1::uuid, 'pre-delete crash')
        RETURNING error_id
        """,
        uid,
    )

    r = await client.request("DELETE", URL, headers=headers, json={"password": "password123"})
    assert r.status_code == 204

    row = await db_pool.fetchrow(
        "SELECT user_id, message FROM client_error_log WHERE error_id = $1", err_id,
    )
    assert row is not None
    assert row["user_id"] is None
    assert row["message"] == "pre-delete crash"

    await db_pool.execute("DELETE FROM client_error_log WHERE error_id = $1", err_id)
