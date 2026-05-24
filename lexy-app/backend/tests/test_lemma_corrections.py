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


def _obs() -> str:
    """Per-test-unique observed_lemma. Accept (3B) writes a lemma_override keyed
    on (language, observed_lemma) — a fixed value would collide across workers,
    so each review test uses a unique one (reaped by the autouse cleanup)."""
    return f"obs-{worker_id()}-{uuid.uuid4().hex[:10]}"


def _accept_url(cid: int) -> str:
    return f"/api/v1/admin/lemma-corrections/{cid}/accept"


def _reject_url(cid: int) -> str:
    return f"/api/v1/admin/lemma-corrections/{cid}/reject"


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


async def _admin_headers(client: AsyncClient, db_pool) -> tuple[dict, str]:
    headers, uid = await _register(client, db_pool, make_test_email())
    await _make_admin(db_pool, uid)
    return headers, uid


async def _post_candidate(client: AsyncClient, headers: dict, *, obs: str,
                          suggested: str | None = None, surf: str | None = None) -> int:
    """Create a pending candidate via the public endpoint; return its id."""
    payload = {"language": "es", "surface_form": surf or _surf(), "observed_lemma": obs}
    if suggested is not None:
        payload["suggested_lemma"] = suggested
    r = await client.post(URL, json=payload, headers=headers)
    assert r.status_code == 201
    return r.json()["candidate_id"]


@pytest.fixture(autouse=True)
async def _cleanup_candidates(db_pool):
    yield
    await db_pool.execute(
        "DELETE FROM lemma_correction_candidate WHERE surface_form LIKE $1",
        f"lc-{worker_id()}-%",
    )
    # slice 3B: accept writes lemma_override rows — reap this worker's test ones.
    await db_pool.execute(
        "DELETE FROM lemma_override WHERE observed_lemma LIKE $1",
        f"obs-{worker_id()}-%",
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
    """A flag is a signal, not authority — it must never touch lemma_override.
    Scoped to a unique observed_lemma so a concurrent 3B accept (which DOES write
    lemma_override) can't skew a global COUNT under -n auto."""
    headers, _ = await _register(client, db_pool, make_test_email())
    obs = _obs()
    pre = await db_pool.fetchval(
        "SELECT COUNT(*) FROM lemma_override WHERE observed_lemma = $1", obs
    )
    r = await client.post(URL, json={
        "language": "es", "surface_form": _surf(),
        "observed_lemma": obs, "suggested_lemma": "duchar",
    }, headers=headers)
    assert r.status_code == 201
    post = await db_pool.fetchval(
        "SELECT COUNT(*) FROM lemma_override WHERE observed_lemma = $1", obs
    )
    assert pre == post == 0


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


# ---------------------------------------------------------------------------
# Admin review (#39 slice 3B) — accept promotes to lemma_override; reject doesn't
# ---------------------------------------------------------------------------

async def _override_for(db_pool, obs: str):
    return await db_pool.fetchrow(
        "SELECT * FROM lemma_override WHERE language = 'es' AND observed_lemma = $1 "
        "AND surface_form IS NULL AND pos IS NULL",
        obs,
    )


# --- accept ---

async def test_accept_with_suggested_creates_override(client: AsyncClient, db_pool):
    headers, _ = await _admin_headers(client, db_pool)
    obs = _obs()
    cid = await _post_candidate(client, headers, obs=obs, suggested="goodlemma")
    r = await client.post(_accept_url(cid), json={}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["candidate"]["status"] == "accepted"
    assert body["override"]["corrected_lemma"] == "goodlemma"
    assert body["override"]["source"] == "user_flag_reviewed"
    ov = await _override_for(db_pool, obs)
    assert ov is not None and ov["corrected_lemma"] == "goodlemma"


async def test_accept_with_corrected_lemma_overrides_suggestion(client: AsyncClient, db_pool):
    headers, _ = await _admin_headers(client, db_pool)
    obs = _obs()
    cid = await _post_candidate(client, headers, obs=obs, suggested="suggested_fix")
    r = await client.post(_accept_url(cid), json={"corrected_lemma": "admin_fix"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["override"]["corrected_lemma"] == "admin_fix"
    assert (await _override_for(db_pool, obs))["corrected_lemma"] == "admin_fix"


async def test_accept_updates_existing_override_no_duplicate(client: AsyncClient, db_pool):
    """The load-bearing upsert test: an existing context-free override is UPDATED
    in place (not duplicated) — proves the ON CONFLICT path fires."""
    headers, _ = await _admin_headers(client, db_pool)
    obs = _obs()
    await db_pool.execute(
        "INSERT INTO lemma_override (language, observed_lemma, corrected_lemma, source) "
        "VALUES ('es', $1, 'oldvalue', 'manual')",
        obs,
    )
    cid = await _post_candidate(client, headers, obs=obs, suggested="newvalue")
    r = await client.post(_accept_url(cid), json={}, headers=headers)
    assert r.status_code == 200
    rows = await db_pool.fetch(
        "SELECT corrected_lemma, source FROM lemma_override "
        "WHERE language='es' AND observed_lemma=$1",
        obs,
    )
    assert len(rows) == 1                                   # updated, not duplicated
    assert rows[0]["corrected_lemma"] == "newvalue"
    assert rows[0]["source"] == "user_flag_reviewed"


async def test_accept_sets_review_metadata(client: AsyncClient, db_pool):
    headers, uid = await _admin_headers(client, db_pool)
    obs = _obs()
    cid = await _post_candidate(client, headers, obs=obs, suggested="x")
    await client.post(_accept_url(cid), json={"review_note": "looks right"}, headers=headers)
    row = await db_pool.fetchrow(
        "SELECT status, reviewed_by, reviewed_at, review_note "
        "FROM lemma_correction_candidate WHERE candidate_id = $1",
        cid,
    )
    assert row["status"] == "accepted"
    assert str(row["reviewed_by"]) == uid
    assert row["reviewed_at"] is not None
    assert row["review_note"] == "looks right"


async def test_accept_is_atomic_both_applied(client: AsyncClient, db_pool):
    """Happy-path atomicity: after accept, BOTH the candidate is accepted AND the
    override exists."""
    headers, _ = await _admin_headers(client, db_pool)
    obs = _obs()
    cid = await _post_candidate(client, headers, obs=obs, suggested="x")
    await client.post(_accept_url(cid), json={}, headers=headers)
    cand = await db_pool.fetchrow(
        "SELECT status FROM lemma_correction_candidate WHERE candidate_id = $1", cid
    )
    assert cand["status"] == "accepted"
    assert await _override_for(db_pool, obs) is not None


# --- reject ---

async def test_reject_marks_rejected_no_override(client: AsyncClient, db_pool):
    headers, _ = await _admin_headers(client, db_pool)
    obs = _obs()
    cid = await _post_candidate(client, headers, obs=obs, suggested="x")
    r = await client.post(_reject_url(cid), json={"review_note": "not a real error"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["candidate"]["status"] == "rejected"
    assert r.json()["override"] is None
    assert await _override_for(db_pool, obs) is None


async def test_rejected_cannot_be_accepted(client: AsyncClient, db_pool):
    headers, _ = await _admin_headers(client, db_pool)
    cid = await _post_candidate(client, headers, obs=_obs(), suggested="x")
    await client.post(_reject_url(cid), json={}, headers=headers)
    r = await client.post(_accept_url(cid), json={}, headers=headers)
    assert r.status_code == 409


async def test_accepted_cannot_be_rejected(client: AsyncClient, db_pool):
    headers, _ = await _admin_headers(client, db_pool)
    cid = await _post_candidate(client, headers, obs=_obs(), suggested="x")
    await client.post(_accept_url(cid), json={}, headers=headers)
    r = await client.post(_reject_url(cid), json={}, headers=headers)
    assert r.status_code == 409


# --- auth ---

async def test_normal_user_cannot_accept_or_reject(client: AsyncClient, db_pool):
    admin_h, _ = await _admin_headers(client, db_pool)
    cid = await _post_candidate(client, admin_h, obs=_obs(), suggested="x")
    user_h, _ = await _register(client, db_pool, make_test_email())
    assert (await client.post(_accept_url(cid), json={}, headers=user_h)).status_code == 403
    assert (await client.post(_reject_url(cid), json={}, headers=user_h)).status_code == 403


async def test_unauthenticated_cannot_accept_or_reject(client: AsyncClient, db_pool):
    admin_h, _ = await _admin_headers(client, db_pool)
    cid = await _post_candidate(client, admin_h, obs=_obs(), suggested="x")
    assert (await client.post(_accept_url(cid), json={})).status_code in (401, 403)
    assert (await client.post(_reject_url(cid), json={})).status_code in (401, 403)


# --- validation / not-found ---

async def test_accept_nothing_to_promote_returns_400(client: AsyncClient, db_pool):
    headers, _ = await _admin_headers(client, db_pool)
    obs = _obs()
    cid = await _post_candidate(client, headers, obs=obs)        # no suggested_lemma → ''
    r = await client.post(_accept_url(cid), json={}, headers=headers)  # and no corrected_lemma
    assert r.status_code == 400
    assert await _override_for(db_pool, obs) is None             # nothing written


async def test_accept_unknown_candidate_returns_404(client: AsyncClient, db_pool):
    headers, _ = await _admin_headers(client, db_pool)
    r = await client.post(_accept_url(999_999_999), json={"corrected_lemma": "x"}, headers=headers)
    assert r.status_code == 404
