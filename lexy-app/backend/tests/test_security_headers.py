"""HTTP security-headers tests (S5).

Covers the middleware (headers on success, error, and 404 responses; HSTS
gating) and the policy builder unit.
"""
from httpx import AsyncClient

from backend.core.security_headers import build_security_headers
from ._email_helper import make_test_email

KNOWLEDGE = "/api/v1/words/knowledge"
REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"

BASELINE_HEADERS = [
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
    "cross-origin-opener-policy",
    "content-security-policy",
]


def _assert_baseline(headers) -> None:
    for name in BASELINE_HEADERS:
        assert name in headers, f"missing security header: {name}"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"


# ---------------------------------------------------------------------------
# Middleware behaviour over HTTP
# ---------------------------------------------------------------------------

async def test_headers_present_on_success(client: AsyncClient):
    email = make_test_email()
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    login = await client.post(LOGIN, json={"email": email, "password": "password123"})
    token = login.json()["access_token"]

    resp = await client.get(KNOWLEDGE, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    _assert_baseline(resp.headers)


async def test_headers_present_on_auth_error(client: AsyncClient):
    # No token → 403, but the headers must still be stamped.
    resp = await client.get(KNOWLEDGE)
    assert resp.status_code == 403
    _assert_baseline(resp.headers)


async def test_headers_present_on_404(client: AsyncClient):
    resp = await client.get("/api/v1/this-route-does-not-exist")
    assert resp.status_code == 404
    _assert_baseline(resp.headers)


async def test_headers_present_on_validation_error(client: AsyncClient):
    resp = await client.post(REGISTER, json={"email": "bad", "password": "x"})
    assert resp.status_code == 422
    _assert_baseline(resp.headers)


# ---------------------------------------------------------------------------
# HSTS gating
# ---------------------------------------------------------------------------

async def test_hsts_absent_by_default(client: AsyncClient, monkeypatch):
    monkeypatch.delenv("ENABLE_HSTS", raising=False)
    resp = await client.get(KNOWLEDGE)
    assert "strict-transport-security" not in resp.headers


async def test_hsts_present_when_enabled(client: AsyncClient, monkeypatch):
    monkeypatch.setenv("ENABLE_HSTS", "true")
    resp = await client.get(KNOWLEDGE)
    assert resp.headers.get("strict-transport-security") == "max-age=31536000; includeSubDomains"


# ---------------------------------------------------------------------------
# Policy builder unit
# ---------------------------------------------------------------------------

def test_build_headers_hsts_toggle():
    assert "Strict-Transport-Security" not in build_security_headers(hsts_enabled=False)
    assert "Strict-Transport-Security" in build_security_headers(hsts_enabled=True)


def test_csp_allows_app_needs():
    csp = build_security_headers(hsts_enabled=False)["Content-Security-Policy"]
    # React inline styles need 'unsafe-inline' on style-src.
    assert "style-src 'self' 'unsafe-inline'" in csp
    # YoutubeEmbed loads the IFrame API script at runtime → its origins must be
    # script-src sources (regression guard: a bare `script-src 'self'` breaks
    # the video player in production).
    assert "script-src 'self' https://www.youtube.com https://s.ytimg.com" in csp
    assert "frame-src https://www.youtube.com" in csp
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
