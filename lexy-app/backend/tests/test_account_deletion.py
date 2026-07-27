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
import uuid

from httpx import AsyncClient

from ._email_helper import make_test_email
from ._auth_helper import register_and_login

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
URL      = "/api/v1/account"


async def _register(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    """Register + login. Returns (auth_headers, user_id_str).

    Delegates to the shared helper; see tests/_auth_helper.py.
    """
    return await register_and_login(client, db_pool, email)


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


async def test_shared_catalog_data_preserved(client, db_pool, make_word):
    """Account deletion must not cascade into the shared catalog tables.

    Asserts that OWNED rows (unique business keys) in every catalog table
    survive the delete — not global COUNT(*), which drifts under `pytest -n
    auto` because other workers insert/reap owned catalog rows concurrently
    (the original flake).
    """
    headers, uid = await _register(client, db_pool, make_test_email())

    uniq = uuid.uuid4().hex[:12]
    word_id, _surface = await make_word("de")  # reaped by tracked_words
    phrase_canon = f"_testphrase_{uniq}"
    grammar_slug = f"_testrule_{uniq}"
    channel_yt   = f"UCtest{uniq}"
    video_title  = f"_testvideo_{uniq}"

    await db_pool.execute(
        "INSERT INTO phrase_table (canonical, surface_form) VALUES ($1, $1)", phrase_canon,
    )
    await db_pool.execute(
        "INSERT INTO grammar_rule_table (slug, title, rule_type, short_explanation) "
        "VALUES ($1, $1, 'test', 'x')", grammar_slug,
    )
    await db_pool.execute("INSERT INTO channel (youtube_channel_id) VALUES ($1)", channel_yt)
    await db_pool.execute(
        "INSERT INTO video (title, thumbnail_url, duration, language, dialect) "
        "VALUES ($1, 'x', 1.0, 'de', 'standard')", video_title,
    )
    # Give the delete a real user→catalog edge to traverse.
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status, "
        "passive_level, active_level, times_seen, times_used_correctly) "
        "VALUES ($1::uuid, $2, 'word', 'learning', 0, 0, 0, 0)",
        uid, word_id,
    )

    try:
        r = await client.request("DELETE", URL, headers=headers, json={"password": "password123"})
        assert r.status_code == 204

        # Each specific owned catalog row still exists — no cascade to catalog.
        assert await db_pool.fetchval("SELECT 1 FROM word_table         WHERE word_id = $1",            word_id)      == 1
        assert await db_pool.fetchval("SELECT 1 FROM phrase_table       WHERE canonical = $1",          phrase_canon) == 1
        assert await db_pool.fetchval("SELECT 1 FROM grammar_rule_table WHERE slug = $1",               grammar_slug) == 1
        assert await db_pool.fetchval("SELECT 1 FROM channel            WHERE youtube_channel_id = $1", channel_yt)   == 1
        assert await db_pool.fetchval("SELECT 1 FROM video             WHERE title = $1",               video_title)  == 1
    finally:
        # Exact-key cleanup (word_id reaped via make_word/tracked_words).
        await db_pool.execute("DELETE FROM phrase_table       WHERE canonical = $1",          phrase_canon)
        await db_pool.execute("DELETE FROM grammar_rule_table WHERE slug = $1",               grammar_slug)
        await db_pool.execute("DELETE FROM channel            WHERE youtube_channel_id = $1", channel_yt)
        await db_pool.execute("DELETE FROM video             WHERE title = $1",               video_title)


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
