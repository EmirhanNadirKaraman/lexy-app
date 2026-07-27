"""
#12 — LLM rate limiter tests.

Two layers:
  1. Unit tests for the in-memory limiter directly (no FastAPI).
  2. Endpoint tests that prove the dependency is wired into a representative
     protected route (POST /srs/review/{card_id}/produce) and that GET /srs/due
     stays exempt.
"""
import asyncio
import uuid
from time import monotonic

import pytest
from fastapi import HTTPException
from httpx import AsyncClient

from backend.services import llm_service, rate_limiter
from ._email_helper import make_test_email
from ._auth_helper import register_and_login

REGISTER       = "/api/v1/auth/register"
LOGIN          = "/api/v1/auth/login"
SRS_DUE        = "/api/v1/srs/due"
SRS_REVIEW     = "/api/v1/srs/review"


def _email() -> str:
    return make_test_email()


async def _register(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    """Register + login. Returns (auth_headers, user_id_str).

    Delegates to the shared helper; see tests/_auth_helper.py.
    """
    return await register_and_login(client, db_pool, email)


async def _active_card(client: AsyncClient, db_pool, headers, uid: str, word_id: int) -> int:
    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )
    return await db_pool.fetchval(
        "SELECT card_id FROM srs_cards "
        "WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word' AND direction = 'active'",
        uid, word_id,
    )


# ---------------------------------------------------------------------------
# Unit tests — limiter directly
# ---------------------------------------------------------------------------

async def test_first_request_is_allowed():
    rate_limiter.reset_for_tests()
    # No raise → allowed.
    await rate_limiter.check_and_record(str(uuid.uuid4()))


async def test_per_minute_breach_returns_429_with_retry_after_60():
    rate_limiter.reset_for_tests()
    uid = str(uuid.uuid4())
    # Use a tiny per_minute to keep the test fast.
    for _ in range(3):
        await rate_limiter.check_and_record(uid, per_minute=3, per_hour=1000)
    with pytest.raises(HTTPException) as ei:
        await rate_limiter.check_and_record(uid, per_minute=3, per_hour=1000)
    assert ei.value.status_code == 429
    assert ei.value.detail == "rate_limit_minute"
    assert ei.value.headers["Retry-After"] == "60"


async def test_per_hour_breach_returns_429_with_retry_after_3600():
    rate_limiter.reset_for_tests()
    uid = str(uuid.uuid4())
    # per_minute is high; per_hour is the tight bound.
    for _ in range(5):
        await rate_limiter.check_and_record(uid, per_minute=1000, per_hour=5)
    with pytest.raises(HTTPException) as ei:
        await rate_limiter.check_and_record(uid, per_minute=1000, per_hour=5)
    assert ei.value.status_code == 429
    assert ei.value.detail == "rate_limit_hour"
    assert ei.value.headers["Retry-After"] == "3600"


async def test_user_a_does_not_affect_user_b():
    rate_limiter.reset_for_tests()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    # Saturate user A.
    for _ in range(3):
        await rate_limiter.check_and_record(a, per_minute=3, per_hour=1000)
    with pytest.raises(HTTPException):
        await rate_limiter.check_and_record(a, per_minute=3, per_hour=1000)
    # User B still allowed.
    await rate_limiter.check_and_record(b, per_minute=3, per_hour=1000)


async def test_entries_outside_window_dont_count(monkeypatch):
    """Manually populate the deque with timestamps an hour old; they should
    be pruned and not count toward the hourly limit."""
    rate_limiter.reset_for_tests()
    uid = str(uuid.uuid4())

    # Inject 10 stale timestamps from over an hour ago into this user's deque.
    now = monotonic()
    rate_limiter._windows[uid].extend([now - 7200] * 10)

    # Should not raise, even with per_hour=5, because stale entries get pruned.
    await rate_limiter.check_and_record(uid, per_minute=1000, per_hour=5)
    # Confirm pruning happened — only the one we just recorded remains.
    assert len(rate_limiter._windows[uid]) == 1


async def test_concurrent_requests_at_limit_dont_all_squeak_through():
    """Two requests in flight when the user is at minute=N-1 — only one
    should succeed, the other should 429. asyncio.Lock guarantees this."""
    rate_limiter.reset_for_tests()
    uid = str(uuid.uuid4())
    # Pre-fill to N-1=2.
    for _ in range(2):
        await rate_limiter.check_and_record(uid, per_minute=3, per_hour=1000)
    # Race two concurrent requests.
    results = await asyncio.gather(
        rate_limiter.check_and_record(uid, per_minute=3, per_hour=1000),
        rate_limiter.check_and_record(uid, per_minute=3, per_hour=1000),
        return_exceptions=True,
    )
    raised = [r for r in results if isinstance(r, HTTPException)]
    succeeded = [r for r in results if r is None]
    assert len(succeeded) == 1
    assert len(raised) == 1
    assert raised[0].status_code == 429


# ---------------------------------------------------------------------------
# Endpoint tests — representative protected route + exempt route
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_llm(monkeypatch):
    """Force mock mode so /produce doesn't actually call Anthropic in tests."""
    monkeypatch.setattr(llm_service, "_MOCK", True)


async def test_protected_endpoint_returns_429_after_burst(client: AsyncClient, db_pool, monkeypatch, srs_word):
    """POST /srs/review/{card_id}/produce is rate-limited. We tighten the
    per_minute default to 3 via monkeypatch so the burst test is fast.

    Tightening is via monkeypatching the module constant rather than re-
    patching the dependency, so the route's existing Depends still runs."""
    monkeypatch.setattr(rate_limiter, "PER_MINUTE_DEFAULT", 3)

    word_id, word_text, _ = srs_word
    headers, uid = await _register(client, db_pool, _email())
    card_id = await _active_card(client, db_pool, headers, uid, word_id)

    url = f"{SRS_REVIEW}/{card_id}/produce"
    body = {"answer": word_text}

    # First 3 succeed.
    for i in range(3):
        resp = await client.post(url, json=body, headers=headers)
        assert resp.status_code == 200, f"call #{i+1} got {resp.status_code}"

    # 4th is 429.
    resp = await client.post(url, json=body, headers=headers)
    assert resp.status_code == 429
    assert resp.json()["detail"] == "rate_limit_minute"
    assert resp.headers["retry-after"] == "60"


async def test_protected_endpoint_429_isolated_per_user(client: AsyncClient, db_pool, monkeypatch, srs_word):
    """User A's saturation must not affect user B."""
    monkeypatch.setattr(rate_limiter, "PER_MINUTE_DEFAULT", 2)

    word_id, word_text, _ = srs_word
    headers_a, uid_a = await _register(client, db_pool, _email())
    headers_b, uid_b = await _register(client, db_pool, _email())
    card_a = await _active_card(client, db_pool, headers_a, uid_a, word_id)
    card_b = await _active_card(client, db_pool, headers_b, uid_b, word_id)

    # Saturate user A's bucket.
    for _ in range(2):
        await client.post(f"{SRS_REVIEW}/{card_a}/produce", json={"answer": word_text}, headers=headers_a)
    a_blocked = await client.post(
        f"{SRS_REVIEW}/{card_a}/produce", json={"answer": word_text}, headers=headers_a,
    )
    assert a_blocked.status_code == 429

    # User B unaffected.
    b_ok = await client.post(
        f"{SRS_REVIEW}/{card_b}/produce", json={"answer": word_text}, headers=headers_b,
    )
    assert b_ok.status_code == 200


async def test_protected_endpoint_429_fires_after_auth(client: AsyncClient, db_pool, monkeypatch, srs_word):
    """No Authorization header → 401/403, NOT 429. Rate limiter only runs
    after get_current_user succeeds, so anonymous callers never enter the
    bucket (and a 429 would actually be misleading)."""
    monkeypatch.setattr(rate_limiter, "PER_MINUTE_DEFAULT", 0)   # nothing should be allowed past auth

    word_id, _, _ = srs_word
    resp = await client.post(
        f"{SRS_REVIEW}/{word_id}/produce",
        json={"answer": "x"},
        # no headers
    )
    assert resp.status_code in (401, 403)


async def test_srs_due_is_NOT_rate_limited(client: AsyncClient, db_pool, monkeypatch, srs_word):
    """GET /srs/due may call translate_item_gloss but is cached. Intentionally
    exempt from the limiter so loading a review session never 429s."""
    monkeypatch.setattr(rate_limiter, "PER_MINUTE_DEFAULT", 1)

    word_id, _, language = srs_word
    headers, uid = await _register(client, db_pool, _email())
    await _active_card(client, db_pool, headers, uid, word_id)

    # Hit /srs/due many times — would 429 instantly if rate-limited.
    for _ in range(5):
        resp = await client.get(SRS_DUE, params={"language": language}, headers=headers)
        assert resp.status_code == 200, "GET /srs/due must remain exempt from rate limiting"
