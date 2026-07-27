"""
Insight cards + analytics aggregations.

Covers:
  - GET /api/v1/insights/cards returns two cards (frequent_unknowns, recent_mistakes)
  - Card payload shape (card_type, title, explanation, items list)
  - Cards reflect user's actual usage events
  - Audit Hole 5: 'transcript' context is excluded from most_frequent_unknown_items.
    Locked in as a failing-to-fix-it scenario via xfail; flip strict=True when the
    fix lands so the test enforces the new behaviour.
"""

import pytest
from httpx import AsyncClient
from ._email_helper import make_test_email
from ._auth_helper import register_and_login

REGISTER     = "/api/v1/auth/register"
LOGIN        = "/api/v1/auth/login"
INSIGHT_URL  = "/api/v1/insights/cards"


def _email() -> str:
    return make_test_email()


async def _register_and_get_user(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    """Register + login. Returns (auth_headers, user_id_str).

    Delegates to the shared helper; see tests/_auth_helper.py.
    """
    return await register_and_login(client, db_pool, email)


async def _get_word(db_pool) -> tuple[int, str]:
    # Owned, uniquely-named word (xdist-safe — see conftest / docs/TESTS.md).
    from ._word_helper import insert_owned_word
    wid, _ = await insert_owned_word(db_pool, language="de")
    return wid, "de"


async def _seed_event(db_pool, uid: str, word_id: int, context: str, outcome: str):
    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
        VALUES ($1::uuid, $2, 'word', 'unknown')
        ON CONFLICT DO NOTHING
        """,
        uid, word_id,
    )
    await db_pool.execute(
        """
        INSERT INTO word_usage_events (user_id, item_id, item_type, context, outcome)
        VALUES ($1::uuid, $2, 'word', $3, $4)
        """,
        uid, word_id, context, outcome,
    )


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------

async def test_insight_cards_returns_two_card_types(client: AsyncClient, db_pool):
    """GET /insights/cards always returns frequent_unknowns + recent_mistakes."""
    _, language = await _get_word(db_pool)
    headers, _ = await _register_and_get_user(client, db_pool, _email())

    resp = await client.get(INSIGHT_URL, params={"language": language}, headers=headers)

    assert resp.status_code == 200
    body = resp.json()
    assert "cards" in body
    assert body["language"] == language
    types = {c["card_type"] for c in body["cards"]}
    assert types == {"frequent_unknowns", "recent_mistakes"}


async def test_insight_card_shape(client: AsyncClient, db_pool):
    """Each card has title, explanation, items list."""
    _, language = await _get_word(db_pool)
    headers, _ = await _register_and_get_user(client, db_pool, _email())

    resp = await client.get(INSIGHT_URL, params={"language": language}, headers=headers)

    for card in resp.json()["cards"]:
        assert isinstance(card["title"], str) and card["title"]
        assert isinstance(card["explanation"], str) and card["explanation"]
        assert isinstance(card["items"], list)


async def test_insight_cards_empty_for_new_user(client: AsyncClient, db_pool):
    """No events → both cards empty."""
    _, language = await _get_word(db_pool)
    headers, _ = await _register_and_get_user(client, db_pool, _email())

    resp = await client.get(INSIGHT_URL, params={"language": language}, headers=headers)

    for card in resp.json()["cards"]:
        assert card["items"] == []


# ---------------------------------------------------------------------------
# frequent_unknowns
# ---------------------------------------------------------------------------

async def test_frequent_unknowns_surfaces_unknown_word_with_chat_events(client: AsyncClient, db_pool):
    """A word with status=unknown + free_chat events appears in frequent_unknowns."""
    word_id, language = await _get_word(db_pool)
    headers, uid = await _register_and_get_user(client, db_pool, _email())

    # 3 free_chat events: should appear
    for _ in range(3):
        await _seed_event(db_pool, uid, word_id, context="free_chat", outcome="seen")

    resp = await client.get(INSIGHT_URL, params={"language": language}, headers=headers)
    cards = {c["card_type"]: c for c in resp.json()["cards"]}
    items = cards["frequent_unknowns"]["items"]

    assert any(it["item_id"] == word_id for it in items)


async def test_frequent_unknowns_includes_transcript_clicks(client: AsyncClient, db_pool):
    """RESOLVED 2026-05-18 (Hole 5).

    Clicking a word in a subtitle is the dominant exposure channel for users
    who learn primarily by watching video. The 'transcript' context is now
    included in most_frequent_unknown_items so these words surface in
    'Keeps coming up'. Regression guard.
    """
    word_id, language = await _get_word(db_pool)
    headers, uid = await _register_and_get_user(client, db_pool, _email())

    for _ in range(5):
        await _seed_event(db_pool, uid, word_id, context="transcript", outcome="seen")

    resp = await client.get(INSIGHT_URL, params={"language": language}, headers=headers)
    cards = {c["card_type"]: c for c in resp.json()["cards"]}
    items = cards["frequent_unknowns"]["items"]

    assert any(it["item_id"] == word_id for it in items), (
        "transcript-clicked words should appear in frequent unknowns"
    )


# ---------------------------------------------------------------------------
# recent_mistakes
# ---------------------------------------------------------------------------

async def test_recent_mistakes_surfaces_failed_words(client: AsyncClient, db_pool):
    """A word with outcome='incorrect' appears in recent_mistakes."""
    word_id, language = await _get_word(db_pool)
    headers, uid = await _register_and_get_user(client, db_pool, _email())

    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
        VALUES ($1::uuid, $2, 'word', 'learning')
        ON CONFLICT DO NOTHING
        """,
        uid, word_id,
    )
    await db_pool.execute(
        """
        INSERT INTO word_usage_events (user_id, item_id, item_type, context, outcome)
        VALUES ($1::uuid, $2, 'word', 'srs_review', 'incorrect')
        """,
        uid, word_id,
    )

    resp = await client.get(INSIGHT_URL, params={"language": language}, headers=headers)
    cards = {c["card_type"]: c for c in resp.json()["cards"]}
    items = cards["recent_mistakes"]["items"]

    assert any(it["item_id"] == word_id for it in items)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def test_insight_cards_requires_auth(client: AsyncClient, db_pool):
    """No token → 401 or 403 (FastAPI HTTPBearer returns 403 when header missing)."""
    _, language = await _get_word(db_pool)
    resp = await client.get(INSIGHT_URL, params={"language": language})
    assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Unified enrichment via enrich_by_type (#5c)
# ---------------------------------------------------------------------------

async def test_insight_card_includes_grammar_rule_when_prioritized(client: AsyncClient, db_pool):
    """Before #5c, a grammar rule surfaced by prioritization fell out of the
    insight card because _build_card only enriched words and phrases. After
    #5c the unified dispatcher includes it."""
    rule_row = await db_pool.fetchrow(
        "SELECT rule_id, title, language FROM grammar_rule_table LIMIT 1"
    )
    if rule_row is None:
        pytest.skip("grammar_rule_table empty")
    rule_id, title, language = rule_row["rule_id"], rule_row["title"], rule_row["language"]

    headers, uid = await _register_and_get_user(client, db_pool, _email())
    # Track the rule with 'unknown' status + many recent events to surface it
    # in frequent_unknowns.
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'grammar_rule', 'unknown')",
        uid, rule_id,
    )
    for _ in range(5):
        await db_pool.execute(
            "INSERT INTO word_usage_events (user_id, item_id, item_type, context, outcome) "
            "VALUES ($1::uuid, $2, 'grammar_rule', 'guided_chat', 'seen')",
            uid, rule_id,
        )

    resp = await client.get(INSIGHT_URL, params={"language": language}, headers=headers)
    assert resp.status_code == 200
    cards = {c["card_type"]: c for c in resp.json()["cards"]}
    items = cards["frequent_unknowns"]["items"]

    rule_items = [it for it in items if it["item_id"] == rule_id and it["item_type"] == "grammar_rule"]
    assert rule_items, "expected the grammar rule to appear in frequent_unknowns"
    assert rule_items[0]["display_text"] == title
