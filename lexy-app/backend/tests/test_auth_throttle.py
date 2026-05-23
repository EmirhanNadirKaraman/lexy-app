"""Auth throttling tests (S1).

Login is throttled per (IP, email) and per IP; register is throttled per IP.
The per-test autouse `cleanup` fixture (conftest.py) resets the in-memory
limiter between tests, and `monkeypatch` reverts the lowered limits, so these
tests don't leak state into the rest of the suite.

All requests from the ASGI test transport share one client IP, so the per-IP
buckets are exercised by varying the email; the per-(IP,email) bucket is
exercised by repeating one email.
"""
from httpx import AsyncClient

from backend.services import rate_limiter
from ._email_helper import make_test_email

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
PASSWORD = "password123"


def _email() -> str:
    return make_test_email()


# ---------------------------------------------------------------------------
# Happy path still works below the limit
# ---------------------------------------------------------------------------

async def test_login_succeeds_below_limit(client: AsyncClient):
    email = _email()
    await client.post(REGISTER, json={"email": email, "password": PASSWORD})

    resp = await client.post(LOGIN, json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


async def test_register_succeeds_below_limit(client: AsyncClient):
    resp = await client.post(REGISTER, json={"email": _email(), "password": PASSWORD})
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Login throttle
# ---------------------------------------------------------------------------

async def test_repeated_logins_hit_429_per_email(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(rate_limiter, "LOGIN_MAX_ATTEMPTS", 3)
    email = _email()
    await client.post(REGISTER, json={"email": email, "password": PASSWORD})

    # 3 wrong-password attempts: all reach auth and return 401.
    for _ in range(3):
        r = await client.post(LOGIN, json={"email": email, "password": "wrong"})
        assert r.status_code == 401

    # 4th attempt is throttled before auth runs.
    blocked = await client.post(LOGIN, json={"email": email, "password": "wrong"})
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == rate_limiter.AUTH_RATE_LIMIT_MESSAGE
    assert blocked.headers.get("retry-after") == str(rate_limiter.LOGIN_WINDOW_SECONDS)


async def test_login_throttle_does_not_leak_user_existence(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(rate_limiter, "LOGIN_MAX_ATTEMPTS", 2)

    # Unknown email: the wrong-credentials response is the generic 401, and once
    # throttled it's the generic 429 — never "no such user".
    unknown = _email()
    r1 = await client.post(LOGIN, json={"email": unknown, "password": PASSWORD})
    assert r1.status_code == 401
    r2 = await client.post(LOGIN, json={"email": unknown, "password": PASSWORD})
    assert r2.status_code == 401
    r3 = await client.post(LOGIN, json={"email": unknown, "password": PASSWORD})
    assert r3.status_code == 429
    assert r3.json()["detail"] == rate_limiter.AUTH_RATE_LIMIT_MESSAGE

    # A real account throttles with the SAME message — responses are
    # indistinguishable, so the throttle reveals nothing about existence.
    real = _email()
    await client.post(REGISTER, json={"email": real, "password": PASSWORD})
    for _ in range(2):
        await client.post(LOGIN, json={"email": real, "password": "wrong"})
    blocked_real = await client.post(LOGIN, json={"email": real, "password": "wrong"})
    assert blocked_real.status_code == 429
    assert blocked_real.json()["detail"] == r3.json()["detail"]


async def test_login_ip_spray_guard_trips_across_emails(client: AsyncClient, monkeypatch):
    # Keep the per-email limit high so only the per-IP guard can fire, then spray
    # distinct emails from the one test IP.
    monkeypatch.setattr(rate_limiter, "LOGIN_MAX_ATTEMPTS", 1000)
    monkeypatch.setattr(rate_limiter, "LOGIN_IP_MAX_ATTEMPTS", 3)

    for _ in range(3):
        r = await client.post(LOGIN, json={"email": _email(), "password": PASSWORD})
        assert r.status_code == 401  # unknown user, not yet throttled

    blocked = await client.post(LOGIN, json={"email": _email(), "password": PASSWORD})
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == rate_limiter.AUTH_RATE_LIMIT_MESSAGE


# ---------------------------------------------------------------------------
# Register throttle
# ---------------------------------------------------------------------------

async def test_register_hits_429_after_limit(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(rate_limiter, "REGISTER_MAX_ATTEMPTS", 2)

    for _ in range(2):
        r = await client.post(REGISTER, json={"email": _email(), "password": PASSWORD})
        assert r.status_code == 201

    blocked = await client.post(REGISTER, json={"email": _email(), "password": PASSWORD})
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == rate_limiter.AUTH_RATE_LIMIT_MESSAGE
    assert blocked.headers.get("retry-after") == str(rate_limiter.REGISTER_WINDOW_SECONDS)


# ---------------------------------------------------------------------------
# Reset + validation unaffected
# ---------------------------------------------------------------------------

async def test_reset_clears_throttle(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(rate_limiter, "LOGIN_MAX_ATTEMPTS", 1)
    email = _email()
    await client.post(REGISTER, json={"email": email, "password": PASSWORD})

    first = await client.post(LOGIN, json={"email": email, "password": PASSWORD})
    assert first.status_code == 200
    blocked = await client.post(LOGIN, json={"email": email, "password": PASSWORD})
    assert blocked.status_code == 429

    rate_limiter.reset_for_tests()  # mirrors the per-test autouse reset

    after = await client.post(LOGIN, json={"email": email, "password": PASSWORD})
    assert after.status_code == 200


async def test_validation_errors_are_not_throttled(client: AsyncClient, monkeypatch):
    # Pydantic validation runs before the handler body, so malformed requests
    # 422 and never consume the throttle budget.
    monkeypatch.setattr(rate_limiter, "REGISTER_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(rate_limiter, "LOGIN_MAX_ATTEMPTS", 1)

    for _ in range(3):
        bad = await client.post(REGISTER, json={"email": "not-an-email", "password": "x"})
        assert bad.status_code == 422

    # Budget intact: a valid register still succeeds.
    ok = await client.post(REGISTER, json={"email": _email(), "password": PASSWORD})
    assert ok.status_code == 201
