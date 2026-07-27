"""
SSE notification stream tests (#4a).

Covers two correctness fixes:
  1. Per-row mark-after-yield ordering: a row is marked seen only AFTER its
     yield resumes. Consumer disconnect mid-stream leaves un-yielded rows
     unseen for re-delivery.
  2. request_failed notifications: when subtitle-scraper marks a content
     request failed, a notification row is written.

The bulk of these tests drive the extracted `_yield_unseen` async generator
directly so we can step it without going through SSE/httpx machinery.
"""
import json
import uuid


from backend.routers.notifications import _yield_unseen
from ._email_helper import make_test_email


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_user(db_pool) -> str:
    from backend.services.auth_service import register_user
    email = make_test_email()
    user = await register_user(db_pool, email, "password123")
    return str(user["user_id"])


async def _insert_notification(db_pool, user_id: str, notif_type: str, payload: dict) -> int:
    return await db_pool.fetchval(
        """
        INSERT INTO notification (user_id, type, payload)
        VALUES ($1::uuid, $2, $3::jsonb)
        RETURNING notification_id
        """,
        user_id, notif_type, json.dumps(payload),
    )


async def _is_seen(db_pool, notification_id: int) -> bool:
    return await db_pool.fetchval(
        "SELECT seen FROM notification WHERE notification_id = $1",
        notification_id,
    )


# ---------------------------------------------------------------------------
# Bug 1 — mark-after-yield ordering
# ---------------------------------------------------------------------------

async def test_yield_unseen_does_not_mark_before_yield(db_pool):
    """The row must still be unseen when its yield is first received."""
    user_id = await _make_user(db_pool)
    nid = await _insert_notification(db_pool, user_id, "channel_done", {"channel_id": "x"})

    gen = _yield_unseen(db_pool, user_id)
    event = await gen.__anext__()
    try:
        # The row was yielded but the mark hasn't run yet (control returned
        # to the caller before the post-yield UPDATE statement).
        assert "channel_done" in event
        assert await _is_seen(db_pool, nid) is False
    finally:
        await gen.aclose()


async def test_yield_unseen_marks_after_yield_resumes(db_pool):
    """Asking for the next event runs the mark for the previous row."""
    user_id = await _make_user(db_pool)
    n1 = await _insert_notification(db_pool, user_id, "channel_done", {"channel_id": "a"})
    n2 = await _insert_notification(db_pool, user_id, "video_done",   {"video_id":   "b"})

    gen = _yield_unseen(db_pool, user_id)
    first  = await gen.__anext__()
    second = await gen.__anext__()

    try:
        # By the time `second` is yielded, the post-yield UPDATE for the first
        # row has already executed.
        assert "channel_done" in first
        assert "video_done"   in second
        assert await _is_seen(db_pool, n1) is True
        # n2 was just yielded but its mark hasn't run yet.
        assert await _is_seen(db_pool, n2) is False
    finally:
        await gen.aclose()


async def test_disconnect_before_advancing_leaves_remaining_unseen(db_pool):
    """Simulates a consumer that reads one event then disconnects (`aclose`
    on the generator). Rows after the yielded one stay unseen — they will
    re-deliver on the next subscription."""
    user_id = await _make_user(db_pool)
    n1 = await _insert_notification(db_pool, user_id, "channel_done", {"channel_id": "x"})
    n2 = await _insert_notification(db_pool, user_id, "video_done",   {"video_id":   "y"})

    gen = _yield_unseen(db_pool, user_id)
    _ = await gen.__anext__()       # Consume only the first event …
    await gen.aclose()               # … then disconnect.

    # The first row never got its post-yield UPDATE (consumer never advanced
    # past the yield), so it stays unseen. The second row was never yielded.
    # Both must be re-deliverable.
    assert await _is_seen(db_pool, n1) is False
    assert await _is_seen(db_pool, n2) is False


async def test_yield_unseen_empty_user_yields_heartbeat(db_pool):
    """With no unseen rows the helper yields a single heartbeat comment line."""
    user_id = await _make_user(db_pool)

    gen = _yield_unseen(db_pool, user_id)
    event = await gen.__anext__()
    try:
        assert event.startswith(": heartbeat")
    finally:
        await gen.aclose()


async def test_yield_unseen_payload_parses_string_jsonb(db_pool):
    """asyncpg may return JSONB as a string depending on type codecs — the
    helper must handle both shapes."""
    user_id = await _make_user(db_pool)
    payload = {"video_id": "abc123", "title": "Hello"}
    await _insert_notification(db_pool, user_id, "video_done", payload)

    gen = _yield_unseen(db_pool, user_id)
    event = await gen.__anext__()
    try:
        # Strip "data: " prefix and trailing "\n\n"
        body = event[len("data: "):].rstrip("\n")
        parsed = json.loads(body)
        assert parsed["type"] == "video_done"
        assert parsed["payload"] == payload
    finally:
        await gen.aclose()


# ---------------------------------------------------------------------------
# Bug 2 — failed-request notifications
# ---------------------------------------------------------------------------

async def test_failed_mark_request_writes_request_failed_notification(db_pool):
    """The scraper's _mark_request now emits a 'request_failed' notification
    whenever it sets status='failed'. We can't import the function directly
    (lives in subtitle-scraper, separate process / sys.path), so we exercise
    the exact SQL it executes.

    Both statements MUST run together: UPDATE content_request, then
    INSERT INTO notification. Verify the user gets a notification with the
    reason embedded.
    """
    user_id = await _make_user(db_pool)

    # 1) Create a content request, exactly as the user-side POST would.
    request_id = await db_pool.fetchval(
        """
        INSERT INTO content_request (user_id, request_type, content_id)
        VALUES ($1::uuid, 'channel', $2)
        RETURNING request_id
        """,
        user_id, "UC" + uuid.uuid4().hex[:22],
    )

    # 2) Simulate _mark_request(... 'failed', reason). The production code in
    #    subtitle-scraper/pipeline.py runs these two statements as part of the
    #    same transaction; we run them sequentially against the same pool here.
    reason = "transcript fetch error (transient)"
    await db_pool.execute(
        "UPDATE content_request SET status = 'failed', error = $1, updated_at = NOW() WHERE request_id = $2",
        reason, request_id,
    )
    await db_pool.execute(
        """
        INSERT INTO notification (user_id, type, payload)
        SELECT user_id, 'request_failed', $1::jsonb
          FROM content_request
         WHERE request_id = $2 AND user_id IS NOT NULL
        """,
        json.dumps({"reason": reason}), request_id,
    )

    # 3) The notification was written for the right user with the reason.
    row = await db_pool.fetchrow(
        "SELECT type, payload, seen FROM notification "
        "WHERE user_id = $1::uuid AND type = 'request_failed' ORDER BY notification_id DESC LIMIT 1",
        user_id,
    )
    assert row is not None
    assert row["type"] == "request_failed"
    payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])
    assert payload["reason"] == reason
    assert row["seen"] is False


async def test_request_failed_notification_delivered_via_yield_unseen(db_pool):
    """End-to-end: a request_failed notification flows through the SSE helper."""
    user_id = await _make_user(db_pool)
    await _insert_notification(
        db_pool, user_id, "request_failed", {"reason": "unsupported language: xx"},
    )

    gen = _yield_unseen(db_pool, user_id)
    event = await gen.__anext__()
    try:
        body = event[len("data: "):].rstrip("\n")
        parsed = json.loads(body)
        assert parsed["type"] == "request_failed"
        assert parsed["payload"]["reason"] == "unsupported language: xx"
    finally:
        await gen.aclose()
