"""
Client error reporting endpoint (W7).

Covers POST /api/v1/errors/client — the best-effort frontend crash sink used
by the ErrorBoundary. Auth is intentionally optional (crashes can occur before
login or after token expiry), so absence/invalidity of a bearer token never
returns 401.

xdist isolation (round 3): `client_error_log` is a shared table. Every test
tags its crash `message` with a per-worker-unique prefix (`_tag()`), queries
its row back by that tag (never `ORDER BY error_id DESC LIMIT 1`, which under
`pytest -n auto` returns another worker's row or a row a concurrent cleanup
just deleted → `None`), and the autouse cleanup deletes only this worker's
tagged rows (never a global `DELETE FROM client_error_log`).
"""
import uuid

import pytest
from httpx import AsyncClient

from ._email_helper import make_test_email, worker_id
from backend.routers import errors as errors_router
from backend.services import rate_limiter
from ._auth_helper import register_and_login

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
URL      = "/api/v1/errors/client"


def _tag() -> str:
    """Per-test-unique, worker-scoped message prefix. Goes at the FRONT of every
    crash message so it survives MAX_MESSAGE truncation and so the worker-scoped
    cleanup (LIKE 'cetest-<worker>-%') reaps exactly this worker's rows."""
    return f"cetest-{worker_id()}-{uuid.uuid4().hex[:10]}"


async def _register(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    """Register + login. Returns (auth_headers, user_id_str).

    Delegates to the shared helper; see tests/_auth_helper.py.
    """
    return await register_and_login(client, db_pool, email)


def _minimal_payload(message: str) -> dict:
    return {"message": message}


def _full_payload(message: str) -> dict:
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
    """Delete only THIS worker's tagged rows after each test. A global
    `DELETE FROM client_error_log` would race other xdist workers mid-test."""
    yield
    await db_pool.execute(
        "DELETE FROM client_error_log WHERE message LIKE $1", f"cetest-{worker_id()}-%"
    )


async def test_unauthenticated_post_returns_204_and_inserts_row(client: AsyncClient, db_pool):
    msg = f"{_tag()} TypeError: x is undefined"
    resp = await client.post(URL, json=_full_payload(msg))

    assert resp.status_code == 204
    row = await db_pool.fetchrow("SELECT * FROM client_error_log WHERE message = $1", msg)
    assert row is not None
    assert row["user_id"] is None
    assert row["message"]         == msg
    assert row["stack"]           == "Error: bad\n  at Foo (foo.js:1:1)"
    assert row["component_stack"] == "in Foo\n  in App"
    assert row["url"]             == "https://example.com/page"
    assert row["user_agent"]      == "Mozilla/5.0"
    assert row["release"]         == "1.2.3"


async def test_authenticated_post_associates_user_id(client: AsyncClient, db_pool):
    headers, uid = await _register(client, db_pool, make_test_email())
    msg = f"{_tag()} auth crash"

    resp = await client.post(URL, json=_full_payload(msg), headers=headers)

    assert resp.status_code == 204
    row = await db_pool.fetchrow(
        "SELECT user_id, message FROM client_error_log WHERE message = $1", msg
    )
    assert row is not None
    assert str(row["user_id"]) == uid
    assert row["message"] == msg


async def test_invalid_token_still_accepts_report_with_null_user(client: AsyncClient, db_pool):
    headers = {"Authorization": "Bearer not-a-real-token"}
    msg = f"{_tag()} garbage token"

    resp = await client.post(URL, json=_minimal_payload(msg), headers=headers)

    assert resp.status_code == 204
    row = await db_pool.fetchrow(
        "SELECT user_id, message FROM client_error_log WHERE message = $1", msg
    )
    assert row is not None
    assert row["user_id"] is None
    assert row["message"] == msg


async def test_optional_fields_missing_handled(client: AsyncClient, db_pool):
    msg = f"{_tag()} just a message"
    resp = await client.post(URL, json={"message": msg})

    assert resp.status_code == 204
    row = await db_pool.fetchrow(
        "SELECT message, stack, component_stack, url, release FROM client_error_log "
        "WHERE message = $1",
        msg,
    )
    assert row["message"]         == msg
    assert row["stack"]           is None
    assert row["component_stack"] is None
    assert row["url"]             is None
    assert row["release"]         is None


async def test_oversized_fields_are_truncated(client: AsyncClient, db_pool):
    tag = _tag()
    huge_stack   = "x" * (errors_router.MAX_STACK + 5_000)
    # tag rides at the FRONT so it survives truncation to MAX_MESSAGE.
    huge_message = tag + ("m" * (errors_router.MAX_MESSAGE + 1_000))

    resp = await client.post(URL, json={"message": huge_message, "stack": huge_stack})

    assert resp.status_code == 204
    row = await db_pool.fetchrow(
        "SELECT message, stack FROM client_error_log WHERE message LIKE $1", tag + "%"
    )
    assert row is not None
    assert len(row["message"]) == errors_router.MAX_MESSAGE
    assert len(row["stack"])   == errors_router.MAX_STACK


async def test_malformed_payload_returns_422(client: AsyncClient):
    # No `message` field at all → schema rejects. (Inserts nothing.)
    resp = await client.post(URL, json={"stack": "lonely stack"})
    assert resp.status_code == 422

    # Blank-only message also rejected (strip → empty).
    resp = await client.post(URL, json={"message": "   "})
    assert resp.status_code == 422


async def test_endpoint_does_not_require_auth(client: AsyncClient):
    # No Authorization header at all — must succeed (not 401/403). Tagged so the
    # inserted row is reaped by the worker-scoped cleanup.
    resp = await client.post(URL, json={"message": f"{_tag()} anon"})
    assert resp.status_code == 204


async def test_user_agent_falls_back_to_header(client: AsyncClient, db_pool):
    # Client omits `user_agent` field; server fills from the User-Agent header.
    msg = f"{_tag()} header fallback"
    resp = await client.post(
        URL,
        json={"message": msg},
        headers={"User-Agent": "TestRunner/9.9"},
    )
    assert resp.status_code == 204
    row = await db_pool.fetchrow(
        "SELECT user_agent FROM client_error_log WHERE message = $1", msg
    )
    assert row["user_agent"] == "TestRunner/9.9"


# ---------------------------------------------------------------------------
# S6 — per-IP throttle (the endpoint stays public; floods are capped)
# ---------------------------------------------------------------------------

async def test_reports_throttled_per_ip_after_limit(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(rate_limiter, "CLIENT_ERROR_MAX_REPORTS", 3)
    msg = _tag()  # tagged so the 3 accepted rows are reaped by cleanup

    for _ in range(3):
        ok = await client.post(URL, json=_minimal_payload(msg))
        assert ok.status_code == 204

    blocked = await client.post(URL, json=_minimal_payload(msg))
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == rate_limiter.PUBLIC_RATE_LIMIT_MESSAGE


async def test_authenticated_report_below_limit_still_succeeds(client: AsyncClient, db_pool, monkeypatch):
    monkeypatch.setattr(rate_limiter, "CLIENT_ERROR_MAX_REPORTS", 5)
    headers, _ = await _register(client, db_pool, make_test_email())

    resp = await client.post(URL, json=_minimal_payload(f"{_tag()} auth ok"), headers=headers)
    assert resp.status_code == 204
