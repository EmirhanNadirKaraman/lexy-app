"""S17 — POST /phrases/seed is admin-only.

The reseed endpoint mutates the global shared `phrase_table`, so it's gated on
`require_admin` (was: any authenticated user). `is_admin` is planted
out-of-band in `users.settings` — here via direct SQL, deliberately NOT the
settings API, which filters writes to DEFAULTS and so cannot set it. A normal
authenticated user gets 403; an admin gets 201; no token gets 401/403.
"""
from httpx import AsyncClient

from ._email_helper import make_test_email

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
SEED = "/api/v1/phrases/seed"


async def _register(client: AsyncClient, db_pool, email: str):
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    r = await client.post(LOGIN, json={"email": email, "password": "password123"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    uid = await db_pool.fetchval("SELECT user_id FROM users WHERE email = $1", email)
    return headers, uid


async def _make_admin(db_pool, user_id) -> None:
    # Plant is_admin directly (the settings API can't — it's not in DEFAULTS).
    # Merge so any existing settings keys are preserved.
    await db_pool.execute(
        "UPDATE users SET settings = COALESCE(settings::jsonb, '{}'::jsonb) "
        "|| '{\"is_admin\": true}'::jsonb WHERE user_id = $1::uuid",
        user_id,
    )


async def test_seed_forbidden_for_non_admin(client: AsyncClient, db_pool):
    headers, _uid = await _register(client, db_pool, make_test_email())
    r = await client.post(SEED, headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"


async def test_seed_allowed_for_admin(client: AsyncClient, db_pool):
    headers, uid = await _register(client, db_pool, make_test_email())
    await _make_admin(db_pool, uid)
    r = await client.post(SEED, headers=headers)
    assert r.status_code == 201
    body = r.json()
    assert "inserted" in body and "total" in body


async def test_seed_requires_auth(client: AsyncClient):
    r = await client.post(SEED)
    # HTTPBearer → 403 with no header; 401 with a present-but-invalid one.
    assert r.status_code in (401, 403)
