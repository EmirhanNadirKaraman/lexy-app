"""#39 slice 3A — lemma correction candidate endpoints.

POST /api/v1/lemma-corrections (auth) + GET /api/v1/admin/lemma-corrections
(admin). Signal only: a flag NEVER writes `lemma_override`.

xdist isolation: the pending-dedup index is GLOBAL and cross-user
(language/surface/observed/suggested/context), so every test uses a
per-worker-unique `surface_form` (`_surf()`) — otherwise two workers flagging
the same surface would dedup into one row and skew `report_count` assertions.
The autouse fixture reaps only this worker's tagged candidates (user-cleanup
can't: `user_id` is `ON DELETE SET NULL`, so the candidate survives).
"""
import uuid

import pytest
from httpx import AsyncClient

from ._email_helper import make_test_email, worker_id
from backend.services import rate_limiter

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
URL      = "/api/v1/lemma-corrections"
ADMIN_URL = "/api/v1/admin/lemma-corrections"


def _surf() -> str:
    """Per-test-unique, worker-scoped surface_form (part of the global dedup key)."""
    return f"lc-{worker_id()}-{uuid.uuid4().hex[:10]}"


async def _register(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    r = await client.post(LOGIN, json={"email": email, "password": "password123"})
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    uid = str(await db_pool.fetchval("SELECT user_id FROM users WHERE email = $1", email))
    return headers, uid


async def _make_admin(db_pool, user_id) -> None:
    # Plant is_admin out-of-band (the settings API can't set it).
    await db_pool.execute(
        "UPDATE users SET settings = COALESCE(settings::jsonb, '{}'::jsonb) "
        "|| '{\"is_admin\": true}'::jsonb WHERE user_id = $1::uuid",
        user_id,
    )


@pytest.fixture(autouse=True)
async def _cleanup_candidates(db_pool):
    yield
    await db_pool.execute(
        "DELETE FROM lemma_correction_candidate WHERE surface_form LIKE $1",
        f"lc-{worker_id()}-%",
    )


# --- auth gate --------------------------------------------------------------

async def test_unauthenticated_post_rejected(client: AsyncClient):
    r = await client.post(URL, json={"language": "es", "surface_form": _surf(), "observed_lemma": "badlemma"})
    assert r.status_code in (401, 403)


# --- create -----------------------------------------------------------------

async def test_authenticated_post_creates_pending(client: AsyncClient, db_pool):
    headers, _ = await _register(client, db_pool, make_test_email())
    surf = _surf()
    r = await client.post(URL, json={
        "language": "es", "surface_form": surf,
        "observed_lemma": "badlemma", "suggested_lemma": "goodlemma",
    }, headers=headers)
    assert r.status_code == 201
    body = r.json()
    assert body["surface_form"] == surf
    assert body["status"] == "pending"
    assert body["source"] == "user_flag"
    assert body["report_count"] == 1
    assert body["suggested_lemma"] == "goodlemma"


async def test_optional_fields_normalize_to_empty_string(client: AsyncClient, db_pool):
    """Missing suggested_lemma/context_text become '' (the table sentinel)."""
    headers, _ = await _register(client, db_pool, make_test_email())
    r = await client.post(URL, json={
        "language": "es", "surface_form": _surf(), "observed_lemma": "badlemma",
    }, headers=headers)
    assert r.status_code == 201
    assert r.json()["suggested_lemma"] == ""
    assert r.json()["context_text"] == ""


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"language": "",   "surface_form": "x", "observed_lemma": "y"},       # blank language
    {"language": "   ", "surface_form": "x", "observed_lemma": "y"},      # whitespace language
    {"language": "es", "surface_form": "",  "observed_lemma": "y"},       # blank surface
    {"language": "es", "surface_form": "x", "observed_lemma": "   "},     # whitespace observed
    {"language": "es", "surface_form": "x"},                              # missing observed
    {"language": "es", "surface_form": "x", "observed_lemma": "y", "item_type": "grammar_rule"},  # bad item_type
])
async def test_invalid_payload_rejected(client: AsyncClient, db_pool, payload):
    headers, _ = await _register(client, db_pool, make_test_email())
    r = await client.post(URL, json=payload, headers=headers)
    assert r.status_code == 422


async def test_overlong_fields_rejected(client: AsyncClient, db_pool):
    headers, _ = await _register(client, db_pool, make_test_email())
    r = await client.post(URL, json={
        "language": "es", "surface_form": "a" * 201, "observed_lemma": "y",
    }, headers=headers)
    assert r.status_code == 422


# --- dedup ------------------------------------------------------------------

async def test_duplicate_post_increments_report_count(client: AsyncClient, db_pool):
    headers, _ = await _register(client, db_pool, make_test_email())
    surf = _surf()
    payload = {"language": "es", "surface_form": surf, "observed_lemma": "badlemma"}
    r1 = await client.post(URL, json=payload, headers=headers)
    r2 = await client.post(URL, json=payload, headers=headers)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["candidate_id"] == r2.json()["candidate_id"]   # same row, not a new one
    assert r2.json()["report_count"] == 2
    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM lemma_correction_candidate WHERE surface_form = $1", surf
    )
    assert count == 1


async def test_duplicate_across_users_dedups(client: AsyncClient, db_pool):
    """Two different users flagging the same thing → one candidate, count 2."""
    surf = _surf()
    payload = {"language": "es", "surface_form": surf, "observed_lemma": "badlemma"}
    ha, _ = await _register(client, db_pool, make_test_email())
    hb, _ = await _register(client, db_pool, make_test_email())
    r1 = await client.post(URL, json=payload, headers=ha)
    r2 = await client.post(URL, json=payload, headers=hb)
    assert r1.json()["candidate_id"] == r2.json()["candidate_id"]
    assert r2.json()["report_count"] == 2


async def test_different_suggested_lemma_separate_candidate(client: AsyncClient, db_pool):
    headers, _ = await _register(client, db_pool, make_test_email())
    surf = _surf()
    base = {"language": "es", "surface_form": surf, "observed_lemma": "badlemma"}
    r1 = await client.post(URL, json={**base, "suggested_lemma": "fix1"}, headers=headers)
    r2 = await client.post(URL, json={**base, "suggested_lemma": "fix2"}, headers=headers)
    assert r1.json()["candidate_id"] != r2.json()["candidate_id"]
    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM lemma_correction_candidate WHERE surface_form = $1", surf
    )
    assert count == 2


# --- the load-bearing safety guarantee --------------------------------------

async def test_post_never_mutates_lemma_override(client: AsyncClient, db_pool):
    """A flag is a signal, not authority — it must never touch lemma_override."""
    headers, _ = await _register(client, db_pool, make_test_email())
    pre = await db_pool.fetchval("SELECT COUNT(*) FROM lemma_override")
    r = await client.post(URL, json={
        "language": "es", "surface_form": _surf(),
        "observed_lemma": "duchaber", "suggested_lemma": "duchar",
    }, headers=headers)
    assert r.status_code == 201
    post = await db_pool.fetchval("SELECT COUNT(*) FROM lemma_override")
    assert pre == post


# --- admin list -------------------------------------------------------------

async def test_normal_user_cannot_list(client: AsyncClient, db_pool):
    headers, _ = await _register(client, db_pool, make_test_email())
    r = await client.get(ADMIN_URL, headers=headers)
    assert r.status_code == 403
    assert r.json()["detail"] == "admin_required"


async def test_admin_can_list_pending_candidate(client: AsyncClient, db_pool):
    headers, uid = await _register(client, db_pool, make_test_email())
    await _make_admin(db_pool, uid)
    surf = _surf()
    await client.post(URL, json={
        "language": "es", "surface_form": surf, "observed_lemma": "badlemma",
    }, headers=headers)
    r = await client.get(ADMIN_URL, headers=headers)
    assert r.status_code == 200
    surfaces = [c["surface_form"] for c in r.json()]
    assert surf in surfaces  # membership — the global list contains this candidate


# --- throttle ---------------------------------------------------------------

async def test_per_user_throttle(client: AsyncClient, db_pool, monkeypatch):
    monkeypatch.setattr(rate_limiter, "LEMMA_CORRECTION_MAX_REPORTS", 2)
    headers, _ = await _register(client, db_pool, make_test_email())
    for _ in range(2):
        ok = await client.post(URL, json={
            "language": "es", "surface_form": _surf(), "observed_lemma": "x",
        }, headers=headers)
        assert ok.status_code == 201
    blocked = await client.post(URL, json={
        "language": "es", "surface_form": _surf(), "observed_lemma": "x",
    }, headers=headers)
    assert blocked.status_code == 429
