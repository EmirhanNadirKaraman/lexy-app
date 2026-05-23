"""
Client error reporting endpoint (W7).

Covers POST /api/v1/errors/client — the best-effort frontend crash sink used
by the ErrorBoundary. Auth is intentionally optional (crashes can occur before
login or after token expiry), so absence/invalidity of a bearer token never
returns 401.
"""
import pytest
from httpx import AsyncClient

from ._email_helper import make_test_email
from backend.routers import errors as errors_router
from backend.services import rate_limiter

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
URL      = "/api/v1/errors/client"


async def _register(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    r = await client.post(LOGIN, json={"email": email, "password": "password123"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    uid = str(await db_pool.fetchval("SELECT user_id FROM users WHERE email = $1", email))
    return headers, uid


def _minimal_payload(message: str = "boom") -> dict:
    return {"message": message}


def _full_payload(message: str = "TypeError: x is undefined") -> dict:
    return {
        "message": message,
        "stack": "Error: bad\n  at Foo (foo.js:1:1)",
        "component_stack": "in Foo\n  in App",
        "url": "https://example.com/page",
        "user_agent": "Mozilla/5.0",
        "release": "1.2.3",
    }


@pytest.fixture(autouse=True)
async def _cleanup_error_rows(db_pool):
    """Wipe the small log table after each test so assertions are tight.

    The autouse user cleanup in conftest handles user rows; this is the local
    counterpart so client_error_log doesn't leak between tests.
    """
    yield
    await db_pool.execute("DELETE FROM client_error_log")


async def test_unauthenticated_post_returns_204_and_inserts_row(client: AsyncClient, db_pool):
    resp = await client.post(URL, json=_full_payload())

    assert resp.status_code == 204
    row = await db_pool.fetchrow("SELECT * FROM client_error_log ORDER BY error_id DESC LIMIT 1")
    assert row is not None
    assert row["user_id"] is None
    assert row["message"]         == "TypeError: x is undefined"
    assert row["stack"]           == "Error: bad\n  at Foo (foo.js:1:1)"
    assert row["component_stack"] == "in Foo\n  in App"
    assert row["url"]             == "https://example.com/page"
    assert row["user_agent"]      == "Mozilla/5.0"
    assert row["release"]         == "1.2.3"


async def test_authenticated_post_associates_user_id(client: AsyncClient, db_pool):
    headers, uid = await _register(client, db_pool, make_test_email())

    resp = await client.post(URL, json=_full_payload("auth crash"), headers=headers)

    assert resp.status_code == 204
    row = await db_pool.fetchrow(
        "SELECT user_id, message FROM client_error_log ORDER BY error_id DESC LIMIT 1"
    )
    assert row is not None
    assert str(row["user_id"]) == uid
    assert row["message"] == "auth crash"


async def test_invalid_token_still_accepts_report_with_null_user(client: AsyncClient, db_pool):
    headers = {"Authorization": "Bearer not-a-real-token"}

    resp = await client.post(URL, json=_minimal_payload("garbage token"), headers=headers)

    assert resp.status_code == 204
    row = await db_pool.fetchrow("SELECT user_id, message FROM client_error_log ORDER BY error_id DESC LIMIT 1")
    assert row is not None
    assert row["user_id"] is None
    assert row["message"] == "garbage token"


async def test_optional_fields_missing_handled(client: AsyncClient, db_pool):
    resp = await client.post(URL, json={"message": "just a message"})

    assert resp.status_code == 204
    row = await db_pool.fetchrow(
        "SELECT message, stack, component_stack, url, release FROM client_error_log "
        "ORDER BY error_id DESC LIMIT 1"
    )
    assert row["message"]         == "just a message"
    assert row["stack"]           is None
    assert row["component_stack"] is None
    assert row["url"]             is None
    assert row["release"]         is None


async def test_oversized_fields_are_truncated(client: AsyncClient, db_pool):
    huge_stack   = "x" * (errors_router.MAX_STACK + 5_000)
    huge_message = "m" * (errors_router.MAX_MESSAGE + 1_000)

    resp = await client.post(URL, json={"message": huge_message, "stack": huge_stack})

    assert resp.status_code == 204
    row = await db_pool.fetchrow(
        "SELECT message, stack FROM client_error_log ORDER BY error_id DESC LIMIT 1"
    )
    assert len(row["message"]) == errors_router.MAX_MESSAGE
    assert len(row["stack"])   == errors_router.MAX_STACK


async def test_malformed_payload_returns_422(client: AsyncClient):
    # No `message` field at all → schema rejects.
    resp = await client.post(URL, json={"stack": "lonely stack"})
    assert resp.status_code == 422

    # Blank-only message also rejected (strip → empty).
    resp = await client.post(URL, json={"message": "   "})
    assert resp.status_code == 422


async def test_endpoint_does_not_require_auth(client: AsyncClient):
    # No Authorization header at all — must succeed (not 401/403).
    resp = await client.post(URL, json={"message": "anon"})
    assert resp.status_code == 204


async def test_user_agent_falls_back_to_header(client: AsyncClient, db_pool):
    # Client omits `user_agent` field; server fills from the User-Agent header.
    resp = await client.post(
        URL,
        json={"message": "header fallback"},
        headers={"User-Agent": "TestRunner/9.9"},
    )
    assert resp.status_code == 204
    row = await db_pool.fetchrow(
        "SELECT user_agent FROM client_error_log ORDER BY error_id DESC LIMIT 1"
    )
    assert row["user_agent"] == "TestRunner/9.9"


# ---------------------------------------------------------------------------
# S6 — per-IP throttle (the endpoint stays public; floods are capped)
# ---------------------------------------------------------------------------

async def test_reports_throttled_per_ip_after_limit(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(rate_limiter, "CLIENT_ERROR_MAX_REPORTS", 3)

    for _ in range(3):
        ok = await client.post(URL, json=_minimal_payload())
        assert ok.status_code == 204

    blocked = await client.post(URL, json=_minimal_payload())
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == rate_limiter.PUBLIC_RATE_LIMIT_MESSAGE


async def test_authenticated_report_below_limit_still_succeeds(client: AsyncClient, db_pool, monkeypatch):
    monkeypatch.setattr(rate_limiter, "CLIENT_ERROR_MAX_REPORTS", 5)
    headers, _ = await _register(client, db_pool, make_test_email())

    resp = await client.post(URL, json=_minimal_payload("auth ok"), headers=headers)
    assert resp.status_code == 204
