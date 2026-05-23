"""S16 — DoS hardening for the public POST /api/v1/sentences/match.

Covers the per-IP throttle and the input-length cap. Kept in a separate file
from `test_matcher.py` (the #39 matcher suite) so this S16 change stays a
self-contained changeset.

The route is intentionally left public (the frontend never calls it); the cap +
throttle close the unauthenticated CPU-exhaustion vector without an auth change.
"""
from httpx import AsyncClient

from backend.services import rate_limiter

MATCH = "/api/v1/sentences/match"


async def test_normal_match_succeeds(client: AsyncClient):
    resp = await client.post(MATCH, json={"sentence": "Ich gehe nach Hause."})
    assert resp.status_code == 200
    assert "phrases" in resp.json()


async def test_oversize_sentence_rejected_before_matcher(client: AsyncClient):
    # 1001 chars > the 1000-char cap → 422 from schema validation, so the
    # spaCy parser never runs.
    resp = await client.post(MATCH, json={"sentence": "x" * 1001})
    assert resp.status_code == 422


async def test_match_throttled_per_ip(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(rate_limiter, "SENTENCE_MATCH_MAX_REQUESTS", 3)

    for _ in range(3):
        ok = await client.post(MATCH, json={"sentence": "Hallo Welt."})
        assert ok.status_code == 200

    blocked = await client.post(MATCH, json={"sentence": "Hallo Welt."})
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == rate_limiter.PUBLIC_RATE_LIMIT_MESSAGE


async def test_unknown_language_returns_empty_not_crash(client: AsyncClient):
    # A language with no extractor must degrade to an empty phrase list, not 500.
    resp = await client.post(MATCH + "?language=xx", json={"sentence": "hola mundo"})
    assert resp.status_code == 200
    assert resp.json()["phrases"] == []
