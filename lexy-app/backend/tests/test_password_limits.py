"""S10 — password length limits at the validation boundary.

bcrypt silently ignores anything past 72 BYTES, so a long password's effective
entropy can be smaller than the user believes. `RegisterRequest` now rejects
> 72-byte passwords up front, byte-accurately (so a multibyte password under 72
*characters* but over 72 *bytes* is still caught). Login / account-delete carry
only a generous body-size cap — NOT the 72-byte rule — so an account whose
password predates the register cap is never locked out.
"""
from httpx import AsyncClient

from ._email_helper import make_test_email

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"


def make_email() -> str:
    return make_test_email()


# ---------------------------------------------------------------------------
# Register — 72-byte upper bound, byte-accurate
# ---------------------------------------------------------------------------

async def test_register_password_at_72_bytes_succeeds(client: AsyncClient):
    pw = "a" * 72  # exactly 72 ASCII bytes
    resp = await client.post(REGISTER, json={"email": make_email(), "password": pw})
    assert resp.status_code == 201


async def test_register_password_over_72_bytes_rejected(client: AsyncClient):
    pw = "a" * 73  # 73 ASCII bytes
    resp = await client.post(REGISTER, json={"email": make_email(), "password": pw})
    assert resp.status_code == 422
    assert "72 bytes" in resp.text  # clear, user-facing reason


async def test_register_rejects_multibyte_password_over_72_bytes(client: AsyncClient):
    # The killer case: 37 euro signs = 37 CHARACTERS but 111 BYTES (3 each in
    # UTF-8). A char-based max_length(72) would WRONGLY accept this — the
    # byte-accurate validator rejects it.
    pw = "€" * 37  # "€" * 37
    assert len(pw) == 37 and len(pw.encode("utf-8")) == 111
    resp = await client.post(REGISTER, json={"email": make_email(), "password": pw})
    assert resp.status_code == 422
    assert "72 bytes" in resp.text


async def test_register_multibyte_password_at_byte_limit_succeeds(client: AsyncClient):
    # 24 euro signs = 72 bytes exactly → allowed (boundary is inclusive).
    pw = "€" * 24
    assert len(pw.encode("utf-8")) == 72
    resp = await client.post(REGISTER, json={"email": make_email(), "password": pw})
    assert resp.status_code == 201


async def test_register_below_min_length_still_rejected(client: AsyncClient):
    # The new upper bound must not weaken the existing min_length=8 lower bound.
    resp = await client.post(REGISTER, json={"email": make_email(), "password": "short"})
    assert resp.status_code == 422


async def test_register_then_login_at_72_byte_boundary(client: AsyncClient):
    # A password exactly at the cap round-trips: register then authenticate.
    email = make_email()
    pw = "a" * 72
    reg = await client.post(REGISTER, json={"email": email, "password": pw})
    assert reg.status_code == 201
    login = await client.post(LOGIN, json={"email": email, "password": pw})
    assert login.status_code == 200
    assert "access_token" in login.json()


# ---------------------------------------------------------------------------
# Login / delete — body-size guard, deliberately NOT the 72-byte rule
# ---------------------------------------------------------------------------

async def test_login_over_72_bytes_is_not_a_validation_error(client: AsyncClient):
    # Must NOT 422 on a 72-byte rule (that would lock out a pre-cap account).
    # It reaches verify_password and fails as ordinary wrong creds (401).
    resp = await client.post(LOGIN, json={"email": make_email(), "password": "a" * 200})
    assert resp.status_code == 401  # wrong credentials, NOT 422


async def test_login_over_body_cap_rejected(client: AsyncClient):
    # The generous 1024-char body guard still bounds a multi-KB/MB password blob.
    resp = await client.post(LOGIN, json={"email": make_email(), "password": "a" * 2000})
    assert resp.status_code == 422
