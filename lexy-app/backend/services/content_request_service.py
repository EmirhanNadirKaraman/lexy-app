"""
Content requests — the queue the subtitle scraper consumes.

Owns every statement against the `content_request` table. The router keeps the
HTTP concerns (validation of `content_id` at the API boundary, spawning the
scraper subprocess); this module is the only place the table's SQL lives.

Uniqueness is per-user (migration 029): a different user submitting the same
(request_type, content_id) gets their own row, so each user tracks their own
request and receives their own notifications. Idempotency for the *same* user
comes from the ON CONFLICT in `create_or_reset`.

Note the deliberate split of responsibility on submit: this module inserts and
returns the row, and the caller decides whether to spawn the pipeline based on
the returned `status`. Process management stays out of the service layer.
"""
from __future__ import annotations

import asyncpg


async def create_or_reset(
    pool: asyncpg.Pool,
    user_id: str,
    request_type: str,
    content_id: str,
) -> dict:
    """Insert a content request, or return the user's existing row.

    A row already in 'failed' is reset to 'pending' (and its error cleared);
    a row that is 'pending' or 'done' is returned unchanged.

    `content_id` is expected to be already validated + normalized to a
    canonical YouTube id by the caller — see the S7 note in
    `routers/content_requests.py`. The API is the only writer of this table,
    so the scraper inherits already-validated ids.
    """
    row = await pool.fetchrow(
        """
        INSERT INTO content_request (user_id, request_type, content_id)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id, request_type, content_id) DO UPDATE
            SET status     = CASE WHEN content_request.status = 'failed'
                                  THEN 'pending'
                                  ELSE content_request.status END,
                error      = CASE WHEN content_request.status = 'failed'
                                  THEN NULL
                                  ELSE content_request.error END,
                updated_at = NOW()
        RETURNING *
        """,
        user_id,
        request_type,
        content_id,
    )
    return dict(row)


async def list_for_user(pool: asyncpg.Pool, user_id: str) -> list[dict]:
    """Return the user's content requests, newest first."""
    rows = await pool.fetch(
        """
        SELECT * FROM content_request
        WHERE user_id = $1
        ORDER BY created_at DESC
        """,
        user_id,
    )
    return [dict(r) for r in rows]


async def count_pending(pool: asyncpg.Pool) -> int:
    """Count requests still waiting on the scraper, across all users.

    Used by the startup lifespan to decide whether to resume a pipeline run
    left over from a previous process.
    """
    count = await pool.fetchval(
        "SELECT COUNT(*) FROM content_request WHERE status = 'pending'"
    )
    return int(count or 0)
