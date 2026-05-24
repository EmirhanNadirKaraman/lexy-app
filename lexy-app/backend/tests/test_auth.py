"""
Auth tests — register, login, protected route access.

Happy paths and the two main failure modes: duplicate email and wrong password.
"""
import uuid

from httpx import AsyncClient
from ._email_helper import make_test_email


def make_email() -> str:
    """Unique email that the cleanup fixture will delete after the test."""
    return make_test_email()


REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
KNOWLEDGE = "/api/v1/words/knowledge"


# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------


async def test_register_returns_user_id_and_email(client: AsyncClient):
    email = make_email()
    resp = await client.post(REGISTER, json={"email": email, "password": "password123"})

    assert resp.status_code == 201
    data = resp.json()
    assert data["email"] == email
    assert "user_id" in data
    assert len(data["user_id"]) == 36  # UUID format


async def test_register_duplicate_email_returns_400(client: AsyncClient):
    email = make_email()
    payload = {"email": email, "password": "password123"}

    first = await client.post(REGISTER, json=payload)
    assert first.status_code == 201

    second = await client.post(REGISTER, json=payload)
    assert second.status_code == 400
    # S9: the failure is generic and must NOT reveal the email already exists.
    detail = second.json()["detail"]
    assert "already registered" not in detail.lower()
    assert "could not be completed" in detail.lower()


async def test_register_normalises_email_to_lowercase(client: AsyncClient):
    base = make_email()
    upper = base.upper()

    resp = await client.post(REGISTER, json={"email": upper, "password": "password123"})
    assert resp.status_code == 201
    assert resp.json()["email"] == base  # stored lowercase


async def test_register_short_password_returns_422(client: AsyncClient):
    resp = await client.post(REGISTER, json={"email": make_email(), "password": "short"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Registration hardening — non-enumeration (S9) + invite code (S2)
# ---------------------------------------------------------------------------

async def test_register_duplicate_uses_generic_message(client: AsyncClient):
    from backend.services.auth_service import GENERIC_REGISTER_ERROR
    email = make_email()
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    r = await client.post(REGISTER, json={"email": email, "password": "password123"})
    assert r.status_code == 400
    assert r.json()["detail"] == GENERIC_REGISTER_ERROR


async def test_register_code_ignored_when_unset(client: AsyncClient, monkeypatch):
    """Open registration: a supplied code is harmless, and none is required."""
    monkeypatch.delenv("REGISTRATION_CODE", raising=False)
    r1 = await client.post(
        REGISTER, json={"email": make_email(), "password": "password123", "registration_code": "whatever"}
    )
    assert r1.status_code == 201
    r2 = await client.post(REGISTER, json={"email": make_email(), "password": "password123"})
    assert r2.status_code == 201


async def test_register_requires_code_when_configured(client: AsyncClient, monkeypatch):
    from backend.services.auth_service import GENERIC_REGISTER_ERROR
    monkeypatch.setenv("REGISTRATION_CODE", "let-me-in")

    missing = await client.post(REGISTER, json={"email": make_email(), "password": "password123"})
    assert missing.status_code == 400
    assert missing.json()["detail"] == GENERIC_REGISTER_ERROR

    wrong = await client.post(
        REGISTER, json={"email": make_email(), "password": "password123", "registration_code": "nope"}
    )
    assert wrong.status_code == 400
    assert wrong.json()["detail"] == GENERIC_REGISTER_ERROR

    ok = await client.post(
        REGISTER, json={"email": make_email(), "password": "password123", "registration_code": "let-me-in"}
    )
    assert ok.status_code == 201


async def test_register_wrong_code_and_duplicate_email_are_indistinguishable(client: AsyncClient, monkeypatch):
    """With a code configured, a duplicate email (correct code) and a wrong code
    return the identical response — the cause can't be inferred."""
    from backend.services.auth_service import GENERIC_REGISTER_ERROR
    monkeypatch.setenv("REGISTRATION_CODE", "let-me-in")

    email = make_email()
    created = await client.post(
        REGISTER, json={"email": email, "password": "password123", "registration_code": "let-me-in"}
    )
    assert created.status_code == 201

    dup = await client.post(
        REGISTER, json={"email": email, "password": "password123", "registration_code": "let-me-in"}
    )
    wrong_code = await client.post(
        REGISTER, json={"email": make_email(), "password": "password123", "registration_code": "bad"}
    )
    assert dup.status_code == wrong_code.status_code == 400
    assert dup.json()["detail"] == wrong_code.json()["detail"] == GENERIC_REGISTER_ERROR


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


async def test_login_returns_access_token(client: AsyncClient):
    email = make_email()
    await client.post(REGISTER, json={"email": email, "password": "password123"})

    resp = await client.post(LOGIN, json={"email": email, "password": "password123"})
    assert resp.status_code == 200

    data = resp.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"


async def test_login_wrong_password_returns_401(client: AsyncClient):
    email = make_email()
    await client.post(REGISTER, json={"email": email, "password": "password123"})

    resp = await client.post(LOGIN, json={"email": email, "password": "wrong-password"})
    assert resp.status_code == 401


async def test_login_unknown_email_returns_401(client: AsyncClient):
    resp = await client.post(LOGIN, json={"email": make_email(), "password": "password123"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Protected route access
# ---------------------------------------------------------------------------


async def test_protected_route_without_token_returns_403(client: AsyncClient):
    # FastAPI's HTTPBearer returns 403 (Forbidden) when no Authorization header
    # is present, because the credentials are missing entirely.
    resp = await client.get(KNOWLEDGE)
    assert resp.status_code == 403


async def test_protected_route_with_invalid_token_returns_401(client: AsyncClient):
    resp = await client.get(KNOWLEDGE, headers={"Authorization": "Bearer not.a.real.token"})
    assert resp.status_code == 401


async def test_protected_route_with_valid_token_returns_200(client: AsyncClient):
    email = make_email()
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    login = await client.post(LOGIN, json={"email": email, "password": "password123"})
    token = login.json()["access_token"]

    resp = await client.get(KNOWLEDGE, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
