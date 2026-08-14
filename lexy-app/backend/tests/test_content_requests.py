"""
Content requests (channel/video index requests).

Covers:
  - POST /api/v1/content-requests creates a row with status='pending'
  - Duplicate request returns the existing row unchanged
  - A 'failed' duplicate gets reset to 'pending'
  - GET /api/v1/content-requests lists user's requests, newest first
  - Auth required

We don't actually spawn the subtitle-scraper subprocess in tests — the router
calls asyncio.ensure_future on _spawn_pipeline but that schedules a coroutine
that opens a subprocess we don't want running in CI. We patch _spawn_pipeline
to be a no-op for these tests.
"""
import os
import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from ._email_helper import make_test_email
from ._auth_helper import register_and_login

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
URL      = "/api/v1/content-requests"


def _email() -> str:
    return make_test_email()


# xdist worker tag (sanitised to the id charset) so test content_ids are
# per-worker. The autouse cleanup then deletes only THIS worker's rows and can't
# race-delete another worker's in-flight rows under -n auto (the global content_id
# DELETE was a pre-existing cross-worker flake). Tags stay inside the S7-valid
# id formats (UC+22 / 11 chars).
_WORKER = "".join(c for c in os.environ.get("PYTEST_XDIST_WORKER", "main") if c.isalnum()) or "main"


def _channel_id() -> str:
    # Valid 'UC'+22 channel id, worker-tagged + unique per test.
    return ("UC" + _WORKER + uuid.uuid4().hex)[:24]


def _video_id() -> str:
    # Valid 11-char video id, worker-tagged + unique per test.
    return ("v" + _WORKER + uuid.uuid4().hex)[:11]


async def _register_and_get_user(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    """Register + login. Returns (auth_headers, user_id_str).

    Delegates to the shared helper; see tests/_auth_helper.py.
    """
    return await register_and_login(client, db_pool, email)


@pytest.fixture(autouse=True)
def _patch_spawn(monkeypatch):
    """Prevent the real subprocess from being spawned during tests."""
    from backend.routers import content_requests
    monkeypatch.setattr(content_requests, "_spawn_pipeline", AsyncMock())


@pytest.fixture(autouse=True)
async def _cleanup_requests(db_pool):
    yield
    # Per-worker scoped: delete only ids this worker created (UC<worker>… /
    # v<worker>…), so a concurrent worker's in-flight rows are never touched.
    await db_pool.execute(
        "DELETE FROM content_request WHERE content_id LIKE $1 OR content_id LIKE $2",
        f"UC{_WORKER}%", f"v{_WORKER}%",
    )


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------

async def test_post_channel_request_creates_pending_row(client: AsyncClient, db_pool):
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    content_id = _channel_id()

    resp = await client.post(
        URL,
        json={"request_type": "channel", "content_id": content_id},
        headers=headers,
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["request_type"] == "channel"
    assert body["content_id"] == content_id
    assert body["status"] == "pending"
    assert body["error"] is None
    assert "request_id" in body and "created_at" in body and "updated_at" in body


async def test_post_video_request_creates_pending_row(client: AsyncClient, db_pool):
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    content_id = _video_id()

    resp = await client.post(
        URL,
        json={"request_type": "video", "content_id": content_id},
        headers=headers,
    )

    assert resp.status_code == 201
    assert resp.json()["request_type"] == "video"
    assert resp.json()["content_id"] == content_id


async def test_duplicate_pending_request_returns_existing(client: AsyncClient, db_pool):
    """Posting the same (type, content_id) twice yields the same request_id."""
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    content_id = _channel_id()

    r1 = await client.post(URL, json={"request_type": "channel", "content_id": content_id}, headers=headers)
    r2 = await client.post(URL, json={"request_type": "channel", "content_id": content_id}, headers=headers)

    assert r1.json()["request_id"] == r2.json()["request_id"]
    assert r2.json()["status"] == "pending"


async def test_failed_request_reset_to_pending_on_resubmit(client: AsyncClient, db_pool):
    """If a previous attempt failed, re-submitting flips status back to pending."""
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    content_id = _channel_id()

    # First submit
    r1 = await client.post(URL, json={"request_type": "channel", "content_id": content_id}, headers=headers)
    request_id = r1.json()["request_id"]

    # Simulate scraper marking it failed
    await db_pool.execute(
        "UPDATE content_request SET status='failed', error='test reason' WHERE request_id = $1",
        request_id,
    )

    # Resubmit
    r2 = await client.post(URL, json={"request_type": "channel", "content_id": content_id}, headers=headers)
    body = r2.json()
    assert body["status"] == "pending"
    assert body["error"] is None
    assert body["request_id"] == request_id


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

async def test_list_returns_users_requests_newest_first(client: AsyncClient, db_pool):
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    ids = [_channel_id() for _ in range(3)]
    for cid in ids:
        await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers)

    resp = await client.get(URL, headers=headers)

    assert resp.status_code == 200
    rows = resp.json()
    # Newest first → reverse of insertion order
    returned_ids = [r["content_id"] for r in rows][:3]
    assert returned_ids == list(reversed(ids))


async def test_list_isolates_users(client: AsyncClient, db_pool):
    """User A's requests must not appear in user B's list."""
    headers_a, _ = await _register_and_get_user(client, db_pool, _email())
    headers_b, _ = await _register_and_get_user(client, db_pool, _email())

    cid = _channel_id()
    await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers_a)

    resp_b = await client.get(URL, headers=headers_b)
    assert all(r["content_id"] != cid for r in resp_b.json())


# ---------------------------------------------------------------------------
# Per-user uniqueness (migration 029)
# ---------------------------------------------------------------------------

async def test_two_users_same_content_create_separate_rows(client: AsyncClient, db_pool):
    """User A and User B submitting the same content get distinct request rows.

    Pre-migration-029 behaviour was that User B's POST returned User A's row
    (preserved via ON CONFLICT on the global UNIQUE). After 029 each user has
    their own row scoped by (user_id, request_type, content_id).
    """
    headers_a, uid_a = await _register_and_get_user(client, db_pool, _email())
    headers_b, uid_b = await _register_and_get_user(client, db_pool, _email())

    cid = _channel_id()
    r_a = await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers_a)
    r_b = await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers_b)

    assert r_a.status_code == 201
    assert r_b.status_code == 201
    assert r_a.json()["request_id"] != r_b.json()["request_id"]

    # DB-side check: two rows exist, one per user.
    rows = await db_pool.fetch(
        "SELECT user_id::text AS user_id, request_id, status FROM content_request "
        "WHERE request_type = 'channel' AND content_id = $1 ORDER BY request_id",
        cid,
    )
    assert len(rows) == 2
    user_ids = {r["user_id"] for r in rows}
    assert user_ids == {uid_a, uid_b}


async def test_user_b_sees_own_request_after_user_a_submitted_same_content(
    client: AsyncClient, db_pool,
):
    headers_a, _ = await _register_and_get_user(client, db_pool, _email())
    headers_b, _ = await _register_and_get_user(client, db_pool, _email())

    cid = _channel_id()
    await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers_a)
    r_b = await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers_b)

    list_b = await client.get(URL, headers=headers_b)
    assert list_b.status_code == 200
    body_b = list_b.json()
    own = [r for r in body_b if r["content_id"] == cid]
    assert len(own) == 1
    assert own[0]["request_id"] == r_b.json()["request_id"]
    assert own[0]["status"] == "pending"


async def test_user_a_still_sees_own_request_after_user_b_submitted_same_content(
    client: AsyncClient, db_pool,
):
    """User A's list still shows User A's request; User B's submit must not
    rebind A's row to B or alter A's status/error."""
    headers_a, uid_a = await _register_and_get_user(client, db_pool, _email())
    headers_b, _ = await _register_and_get_user(client, db_pool, _email())

    cid = _channel_id()
    r_a = await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers_a)
    a_id = r_a.json()["request_id"]
    await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers_b)

    list_a = await client.get(URL, headers=headers_a)
    own = [r for r in list_a.json() if r["content_id"] == cid]
    assert len(own) == 1
    assert own[0]["request_id"] == a_id

    # DB row for A still owned by A and untouched.
    row_a = await db_pool.fetchrow(
        "SELECT user_id::text AS user_id, status, error FROM content_request WHERE request_id = $1",
        a_id,
    )
    assert row_a["user_id"] == uid_a
    assert row_a["status"] == "pending"
    assert row_a["error"] is None


async def test_user_b_resubmit_of_failed_does_not_reset_user_a_row(
    client: AsyncClient, db_pool,
):
    """When A's row is in 'failed' status, B's submit must reset only B's row.

    Pre-029 this was the worst symptom: B's resubmit flipped A's failed
    row to pending and the scraper later notified A (not B). After 029,
    A's failed row stays failed, B gets their own pending row.
    """
    headers_a, _ = await _register_and_get_user(client, db_pool, _email())
    headers_b, _ = await _register_and_get_user(client, db_pool, _email())

    cid = _channel_id()
    r_a = await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers_a)
    a_id = r_a.json()["request_id"]
    await db_pool.execute(
        "UPDATE content_request SET status='failed', error='scraper exploded' WHERE request_id = $1",
        a_id,
    )

    r_b = await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers_b)
    assert r_b.status_code == 201
    assert r_b.json()["status"] == "pending"
    assert r_b.json()["request_id"] != a_id

    # A's row untouched.
    row_a = await db_pool.fetchrow(
        "SELECT status, error FROM content_request WHERE request_id = $1", a_id,
    )
    assert row_a["status"] == "failed"
    assert row_a["error"] == "scraper exploded"


async def test_notification_routes_to_submitting_user(client: AsyncClient, db_pool):
    """The scraper notifies via _notify_user(request_id, ...). With per-user
    rows, the user_id pulled from content_request is the submitting user's
    — so the notification lands in *their* notification row, not the
    earlier submitter's.

    This test simulates the scraper's notification insert directly (we
    don't spawn the subprocess in tests) to verify the routing contract.
    """
    headers_a, uid_a = await _register_and_get_user(client, db_pool, _email())
    headers_b, uid_b = await _register_and_get_user(client, db_pool, _email())

    cid = _channel_id()
    r_a = await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers_a)
    r_b = await client.post(URL, json={"request_type": "channel", "content_id": cid}, headers=headers_b)
    _a_id = r_a.json()["request_id"]  # unused below, but asserts the key exists
    b_id = r_b.json()["request_id"]

    # Mirror what _notify_user in subtitle-scraper/pipeline.py does:
    #   INSERT INTO notification (user_id, type, payload)
    #   SELECT user_id, $1, $2 FROM content_request WHERE request_id = $3 AND user_id IS NOT NULL
    import json as _json
    await db_pool.execute(
        """
        INSERT INTO notification (user_id, type, payload)
        SELECT user_id, $1, $2::jsonb
          FROM content_request
         WHERE request_id = $3 AND user_id IS NOT NULL
        """,
        "channel_done", _json.dumps({"channel_id": cid}), b_id,
    )

    rows = await db_pool.fetch(
        "SELECT user_id::text AS user_id FROM notification "
        "WHERE type = 'channel_done' AND payload->>'channel_id' = $1",
        cid,
    )
    assert len(rows) == 1
    assert rows[0]["user_id"] == uid_b
    assert rows[0]["user_id"] != uid_a

    # Cleanup: per-test notification rows so other suites stay clean.
    await db_pool.execute(
        "DELETE FROM notification WHERE type = 'channel_done' AND payload->>'channel_id' = $1",
        cid,
    )


# ---------------------------------------------------------------------------
# content_id validation + normalization (S7)
# ---------------------------------------------------------------------------

async def test_video_url_is_normalized_to_id(client: AsyncClient, db_pool):
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    vid = _video_id()
    resp = await client.post(
        URL,
        json={"request_type": "video", "content_id": f"https://www.youtube.com/watch?v={vid}"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["content_id"] == vid  # stored as the bare id, not the URL


async def test_youtu_be_short_url_normalized(client: AsyncClient, db_pool):
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    vid = _video_id()
    resp = await client.post(
        URL,
        json={"request_type": "video", "content_id": f"https://youtu.be/{vid}"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["content_id"] == vid


async def test_channel_url_is_normalized_to_id(client: AsyncClient, db_pool):
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    cid = _channel_id()
    resp = await client.post(
        URL,
        json={"request_type": "channel", "content_id": f"https://www.youtube.com/channel/{cid}"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["content_id"] == cid


@pytest.mark.parametrize("bad", [
    "https://evil.com/watch?v=dQw4w9WgXcQ",   # non-YouTube host
    "https://youtube.com.evil.com/watch?v=dQw4w9WgXcQ",  # look-alike host
    "; rm -rf / #",                            # shell metacharacters
    "$(whoami)",                               # command substitution
    "../../etc/passwd",                        # path-traversal-ish
    "x" * 300,                                 # overlong
    "",                                        # empty
    "   ",                                     # whitespace only
    "dQw4w9WgX",                               # too short (9 chars)
    "dQw4w9WgXcQextra",                        # too long (16 chars)
])
async def test_invalid_video_content_id_rejected(client: AsyncClient, db_pool, bad):
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    resp = await client.post(
        URL, json={"request_type": "video", "content_id": bad}, headers=headers,
    )
    assert resp.status_code == 422


async def test_channel_id_rejected_as_video(client: AsyncClient, db_pool):
    """A UC… channel id is the wrong length for a video request."""
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    resp = await client.post(
        URL, json={"request_type": "video", "content_id": _channel_id()}, headers=headers,
    )
    assert resp.status_code == 422


async def test_video_id_rejected_as_channel(client: AsyncClient, db_pool):
    headers, _ = await _register_and_get_user(client, db_pool, _email())
    resp = await client.post(
        URL, json={"request_type": "channel", "content_id": _video_id()}, headers=headers,
    )
    assert resp.status_code == 422


async def test_invalid_request_not_inserted_and_not_spawned(client: AsyncClient, db_pool):
    """A rejected content_id must neither create a row nor spawn the scraper."""
    from backend.routers import content_requests
    headers, _ = await _register_and_get_user(client, db_pool, _email())

    resp = await client.post(
        URL, json={"request_type": "video", "content_id": "not a valid id"}, headers=headers,
    )
    assert resp.status_code == 422

    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM content_request WHERE content_id = 'not a valid id'"
    )
    assert count == 0
    content_requests._spawn_pipeline.assert_not_called()


# ---------------------------------------------------------------------------
# content_request_service — the extracted SQL, driven directly (arch-05)
#
# The router owns HTTP concerns (content_id validation, spawning the scraper);
# the service owns every statement against content_request. These tests call
# the service without going through the routes, so a future router refactor
# can't silently take the query behaviour with it.
#
# user_id is passed as a **str**, exactly as the router does — get_current_user
# normalizes the UUID to a str (core/deps.py:47) and the service's SQL casts
# nothing, so the type has to match the real call path.
# ---------------------------------------------------------------------------

async def test_service_create_or_reset_inserts_pending_row(client: AsyncClient, db_pool):
    from backend.services import content_request_service
    _, uid = await _register_and_get_user(client, db_pool, _email())
    cid = _channel_id()

    row = await content_request_service.create_or_reset(db_pool, uid, "channel", cid)

    assert row["status"] == "pending"
    assert row["error"] is None
    assert row["content_id"] == cid
    assert row["request_type"] == "channel"
    # RETURNING * — the service hands back the whole row (including user_id,
    # which the route's response_model filters out). Asserted so a switch to an
    # explicit column list shows up as a test failure, not a silent narrowing.
    assert str(row["user_id"]) == uid


async def test_service_create_or_reset_is_idempotent_for_same_user(client: AsyncClient, db_pool):
    from backend.services import content_request_service
    _, uid = await _register_and_get_user(client, db_pool, _email())
    cid = _channel_id()

    first  = await content_request_service.create_or_reset(db_pool, uid, "channel", cid)
    second = await content_request_service.create_or_reset(db_pool, uid, "channel", cid)

    assert first["request_id"] == second["request_id"]
    assert second["status"] == "pending"


async def test_service_create_or_reset_resets_failed_row(client: AsyncClient, db_pool):
    from backend.services import content_request_service
    _, uid = await _register_and_get_user(client, db_pool, _email())
    cid = _channel_id()

    created = await content_request_service.create_or_reset(db_pool, uid, "channel", cid)
    await db_pool.execute(
        "UPDATE content_request SET status='failed', error='boom' WHERE request_id = $1",
        created["request_id"],
    )

    again = await content_request_service.create_or_reset(db_pool, uid, "channel", cid)

    assert again["request_id"] == created["request_id"]
    assert again["status"] == "pending"
    assert again["error"] is None


async def test_service_create_or_reset_leaves_done_row_done(client: AsyncClient, db_pool):
    """Only 'failed' is reset. A completed request stays completed — otherwise a
    resubmit would re-run the scraper over content already indexed."""
    from backend.services import content_request_service
    _, uid = await _register_and_get_user(client, db_pool, _email())
    cid = _channel_id()

    created = await content_request_service.create_or_reset(db_pool, uid, "channel", cid)
    await db_pool.execute(
        "UPDATE content_request SET status='done' WHERE request_id = $1",
        created["request_id"],
    )

    again = await content_request_service.create_or_reset(db_pool, uid, "channel", cid)

    assert again["request_id"] == created["request_id"]
    assert again["status"] == "done"


async def test_service_list_for_user_is_newest_first_and_scoped(client: AsyncClient, db_pool):
    from backend.services import content_request_service
    _, uid_a = await _register_and_get_user(client, db_pool, _email())
    _, uid_b = await _register_and_get_user(client, db_pool, _email())

    ids = [_channel_id() for _ in range(3)]
    for cid in ids:
        await content_request_service.create_or_reset(db_pool, uid_a, "channel", cid)
    b_only = _channel_id()
    await content_request_service.create_or_reset(db_pool, uid_b, "channel", b_only)

    rows_a = await content_request_service.list_for_user(db_pool, uid_a)

    assert [r["content_id"] for r in rows_a][:3] == list(reversed(ids))
    assert all(r["content_id"] != b_only for r in rows_a)


async def test_service_count_pending_sees_a_pending_row(client: AsyncClient, db_pool):
    """Feeds the startup resume in main.py's lifespan.

    Asserted as a lower bound, not an exact count: the query is global across
    users and the suite runs under -n auto, so a concurrent worker's rows are
    legitimately in scope. '>= 1 while my own row is pending' holds regardless.
    """
    from backend.services import content_request_service
    _, uid = await _register_and_get_user(client, db_pool, _email())
    created = await content_request_service.create_or_reset(db_pool, uid, "channel", _channel_id())
    assert created["status"] == "pending"

    assert await content_request_service.count_pending(db_pool) >= 1


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def test_post_requires_auth(client: AsyncClient):
    resp = await client.post(URL, json={"request_type": "channel", "content_id": _channel_id()})
    assert resp.status_code in (401, 403)


async def test_get_requires_auth(client: AsyncClient):
    resp = await client.get(URL)
    assert resp.status_code in (401, 403)
