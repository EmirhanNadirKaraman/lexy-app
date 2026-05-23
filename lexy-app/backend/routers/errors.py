"""Client-side error reports from the frontend ErrorBoundary (W7).

POST /api/v1/errors/client accepts best-effort crash reports. Auth is optional:
crashes can happen pre-login or after token expiry, so a missing/invalid token
must NOT block the report.

Field length caps live in the Pydantic model (truncated server-side rather than
422'd, to maximise the chance a crash report lands). Storage is the
client_error_log table (migration 028).
"""
import logging

import jwt
from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator

from ..core.deps import rate_limit_client_errors
from ..core.security import decode_token
from ..database import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/errors", tags=["errors"])

# Field length caps. Tuned to keep a single row small but cover real React
# stack traces (which can run a few KB in dev builds).
MAX_MESSAGE         = 2_000
MAX_STACK           = 16_000
MAX_COMPONENT_STACK = 16_000
MAX_URL             = 2_000
MAX_USER_AGENT      = 1_000
MAX_RELEASE         = 200


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if len(value) <= limit:
        return value
    return value[:limit]


class ClientErrorReport(BaseModel):
    # Empty/whitespace-only message is rejected (422); everything else is
    # accepted and truncated to MAX_* before insert.
    message: str = Field(min_length=1)
    stack: str | None = None
    component_stack: str | None = None
    url: str | None = None
    user_agent: str | None = None
    release: str | None = None

    @field_validator("message")
    @classmethod
    def _strip_message(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("message must not be blank")
        return v


async def _try_resolve_user_id(authorization: str | None, pool) -> str | None:
    """Best-effort: return user_id if a valid bearer token is present, else None.

    NEVER raises — invalid / expired / missing tokens all collapse to None so
    the error report still lands.
    """
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token:
        return None
    try:
        user_id = decode_token(token)
    except jwt.PyJWTError:
        return None
    except Exception:  # pragma: no cover — defensive: never fail this path
        return None
    try:
        row = await pool.fetchrow(
            "SELECT user_id FROM users WHERE user_id = $1::uuid",
            user_id,
        )
    except Exception:  # pragma: no cover — DB lookup failure should not kill the report
        return None
    return str(row["user_id"]) if row else None


@router.post("/client", status_code=status.HTTP_204_NO_CONTENT)
async def report_client_error(
    body: ClientErrorReport,
    request: Request,
    authorization: str | None = Header(default=None),
    pool=Depends(get_pool),
) -> Response:
    """Record a frontend crash. Auth optional. Returns 204 on success.

    Throttled per client IP (S6) before any work — anonymous floods can't
    fill client_error_log. A 429 here is harmless to the frontend, whose
    ErrorBoundary reporter is fire-and-forget and ignores the response.
    """
    await rate_limit_client_errors(request)
    user_id = await _try_resolve_user_id(authorization, pool)

    # Server fills user_agent if the client didn't (the header is more reliable
    # than navigator.userAgent — and one fewer thing the boundary has to do).
    ua_header = request.headers.get("user-agent")

    message         = _truncate(body.message,                            MAX_MESSAGE)
    stack           = _truncate(body.stack,                              MAX_STACK)
    component_stack = _truncate(body.component_stack,                    MAX_COMPONENT_STACK)
    url             = _truncate(body.url,                                MAX_URL)
    user_agent      = _truncate(body.user_agent or ua_header,            MAX_USER_AGENT)
    release         = _truncate(body.release,                            MAX_RELEASE)

    await pool.execute(
        """
        INSERT INTO client_error_log
            (user_id, message, stack, component_stack, url, user_agent, release)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7)
        """,
        user_id,
        message,
        stack,
        component_stack,
        url,
        user_agent,
        release,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
