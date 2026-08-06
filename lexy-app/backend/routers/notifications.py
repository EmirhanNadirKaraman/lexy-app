import asyncio
import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..core.deps import get_current_user
from ..database import get_pool
from ..services import notification_service

router = APIRouter(prefix="/notifications", tags=["notifications"])


# Polling interval between consecutive unseen-row fetches. SSE delivery is still
# polling-based; LISTEN/NOTIFY is a planned follow-on (TODO #4b).
_POLL_INTERVAL_SECONDS = 3


async def _yield_unseen(pool, user_id):
    """Yield SSE events for each unseen notification for *user_id*, then mark
    each row seen ONLY AFTER its yield has resumed.

    Why per-row, mark-after-yield:
      The previous implementation batch-fetched, batch-marked-seen, then yielded.
      If the SSE connection dropped between the UPDATE and the yields, the rows
      were lost permanently — marked seen on the server but never received by
      the client. With this pattern, if the consumer disconnects (the yield
      raises CancelledError / GeneratorExit), the corresponding `UPDATE` never
      runs, so the row stays unseen and is re-delivered on the next subscription.

    Yields one ": heartbeat\\n\\n" line when there are no unseen rows, so the
    SSE connection stays alive across idle polling cycles.

    The SQL lives in `notification_service`; the ordering below is the part
    that matters here and is deliberately kept in the router.
    """
    rows = await notification_service.fetch_unseen(pool, user_id)
    if not rows:
        yield ": heartbeat\n\n"
        return

    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        data = json.dumps({
            "type":    row["type"],
            "payload": dict(payload) if payload else {},
        })
        yield f"data: {data}\n\n"
        # Mark seen AFTER the yield has resumed. If the consumer disconnected
        # mid-yield this never runs and the row stays in the queue.
        await notification_service.mark_seen(pool, row["notification_id"])


@router.get("/stream")
async def notification_stream(
    request: Request,
    pool=Depends(get_pool),
    current_user: dict = Depends(get_current_user),
):
    """SSE stream — delivers unseen notifications as they arrive.

    Per-row mark-after-yield ordering means a disconnect during delivery leaves
    the un-yielded rows unseen, so they re-deliver on reconnect. See
    _yield_unseen for the rationale.
    """
    user_id = str(current_user["user_id"])

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    break
                async for event in _yield_unseen(pool, user_id):
                    yield event
                await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )
