"""
Notifications — read side of the `notification` table.

Owns every statement against the table on the API side. The write side lives in
`subtitle-scraper/pipeline.py` (`_notify_user` emits `channel_done`,
`video_done` and `request_failed`), which runs in its own process.

Delivery ordering is NOT this module's concern. `routers/notifications.py`
holds the SSE generator and the per-row mark-after-yield sequencing that makes
a mid-stream disconnect re-deliverable — the two functions here are the plain
fetch and the plain mark, so the router can interleave them with its yields.
Keeping the ordering in the router is deliberate: the correctness property is
about when the caller resumes, not about the SQL.
"""
from __future__ import annotations

import asyncpg


async def fetch_unseen(pool: asyncpg.Pool, user_id: str) -> list[asyncpg.Record]:
    """Return the user's unseen notifications, oldest first.

    Records (not dicts) — the caller reads `payload` as returned by asyncpg,
    which may be a dict or a JSON string depending on type codecs.
    """
    return await pool.fetch(
        """
        SELECT notification_id, type, payload
          FROM notification
         WHERE user_id = $1::uuid
           AND seen   = FALSE
         ORDER BY created_at
        """,
        user_id,
    )


async def mark_seen(pool: asyncpg.Pool, notification_id: int) -> None:
    """Mark a single notification delivered.

    Called per row, only after its SSE yield has resumed — see `_yield_unseen`
    in `routers/notifications.py` for why the granularity matters.
    """
    await pool.execute(
        "UPDATE notification SET seen = TRUE WHERE notification_id = $1",
        notification_id,
    )
