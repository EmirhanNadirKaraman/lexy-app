import json
import os

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..core.security import decode_token
from ..database import get_pool
from ..services import rate_limiter

_bearer = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    pool=Depends(get_pool),
) -> dict:
    """FastAPI dependency. Returns {user_id, email, is_admin} or raises 401.

    Distinguishes token expiry from generic token errors so the frontend can
    react differently (silent re-login vs. surfacing a real auth bug). The
    `WWW-Authenticate` header on the expired branch follows RFC 6750.
    """
    try:
        user_id = decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token_expired",
            headers={
                "WWW-Authenticate":
                    'Bearer error="invalid_token", error_description="token expired"',
            },
        )
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = await pool.fetchrow(
        "SELECT user_id, email, settings FROM users WHERE user_id = $1::uuid",
        user_id,
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    row = dict(user)
    row["user_id"] = str(row["user_id"])  # asyncpg returns UUID objects; normalize to str
    raw_settings = row.pop("settings") or "{}"
    settings = json.loads(raw_settings) if isinstance(raw_settings, str) else (raw_settings or {})
    row["is_admin"] = bool(settings.get("is_admin", False))
    return row


async def rate_limit_llm(current_user: dict = Depends(get_current_user)) -> None:
    """FastAPI dependency that gates LLM-backed routes (#12).

    Calls `rate_limiter.check_and_record(user_id)` which raises 429 when the
    user has exceeded the per-minute or per-hour budget. Depends on
    `get_current_user`, so any auth failure (401 / 403) fires *before* the
    rate-limit check — anonymous callers never reach the limiter.
    """
    await rate_limiter.check_and_record(str(current_user["user_id"]))


# ── Auth-route throttling (S1) ──────────────────────────────────────────────
#
# /auth/login and /auth/register are unauthenticated, so there's no user_id to
# key on — we key on client IP (and IP+email for login). These are plain async
# helpers (not FastAPI dependencies) called at the top of the route handler,
# because login needs the parsed request body (email) which a dependency can't
# see. Call them BEFORE any password hashing / DB lookup so throttled requests
# pay no expensive work.

def _client_ip(request: Request) -> str:
    """Best-effort client IP for rate-limit keying.

    By default we use the socket peer (`request.client.host`). X-Forwarded-For
    is honoured ONLY when `TRUST_PROXY_HEADERS` is set, because a client can
    spoof that header to dodge the throttle — trust it only when a known proxy
    in front of the app rewrites it. Returns 'unknown' if the peer is absent
    (e.g. some ASGI test transports), which simply collapses all such callers
    into one bucket.
    """
    if os.getenv("TRUST_PROXY_HEADERS", "").lower() in ("1", "true", "yes"):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    client = request.client
    return client.host if client else "unknown"


async def rate_limit_login(request: Request, email: str) -> None:
    """Throttle login attempts. Two tiers (see rate_limiter constants):
    per (IP, email) to stop targeted brute-force, then per IP to stop spraying.
    Raises HTTPException(429) with a generic message that never reveals whether
    the account exists.
    """
    ip = _client_ip(request)
    normalized = (email or "").lower().strip()
    await rate_limiter.check_window(
        f"login:{ip}:{normalized}",
        limit=rate_limiter.LOGIN_MAX_ATTEMPTS,
        window_seconds=rate_limiter.LOGIN_WINDOW_SECONDS,
        detail=rate_limiter.AUTH_RATE_LIMIT_MESSAGE,
    )
    await rate_limiter.check_window(
        f"login_ip:{ip}",
        limit=rate_limiter.LOGIN_IP_MAX_ATTEMPTS,
        window_seconds=rate_limiter.LOGIN_IP_WINDOW_SECONDS,
        detail=rate_limiter.AUTH_RATE_LIMIT_MESSAGE,
    )


async def rate_limit_register(request: Request) -> None:
    """Throttle account creation per client IP. Raises HTTPException(429)."""
    ip = _client_ip(request)
    await rate_limiter.check_window(
        f"register:{ip}",
        limit=rate_limiter.REGISTER_MAX_ATTEMPTS,
        window_seconds=rate_limiter.REGISTER_WINDOW_SECONDS,
        detail=rate_limiter.AUTH_RATE_LIMIT_MESSAGE,
    )


# ── Public-endpoint throttling (S6, S16) ────────────────────────────────────
#
# Same pattern as the auth helpers, for the two unauthenticated routes that do
# real work. Keyed by client IP; called at the top of the handler so throttled
# requests skip the DB write / spaCy parse entirely.

async def rate_limit_client_errors(request: Request) -> None:
    """Throttle anonymous crash reports per client IP (S6). Raises 429."""
    ip = _client_ip(request)
    await rate_limiter.check_window(
        f"client_errors:{ip}",
        limit=rate_limiter.CLIENT_ERROR_MAX_REPORTS,
        window_seconds=rate_limiter.CLIENT_ERROR_WINDOW_SECONDS,
        detail=rate_limiter.PUBLIC_RATE_LIMIT_MESSAGE,
    )


async def rate_limit_sentence_match(request: Request) -> None:
    """Throttle sentence-match (spaCy) calls per client IP (S16). Raises 429."""
    ip = _client_ip(request)
    await rate_limiter.check_window(
        f"sentence_match:{ip}",
        limit=rate_limiter.SENTENCE_MATCH_MAX_REQUESTS,
        window_seconds=rate_limiter.SENTENCE_MATCH_WINDOW_SECONDS,
        detail=rate_limiter.PUBLIC_RATE_LIMIT_MESSAGE,
    )
