"""
Grammar rules as SRS items tests.

Source of truth: lexy-app/features/grammar-rules-srs/tests.md

Covers:
  Integration (real DB):
    Happy path:
      - PUT grammar_rule/{id}/status 'learning' → 200 with item_type='grammar_rule',
        status='learning'
      - After PUT, passive SRS card exists with default SM-2 values
      - No active SRS card is created (grammar rules are passive-only)
      - GET /srs/due?language=de returns a card for the grammar rule
      - Returned card has display_text = grammar_rule_table.title (not slug or ID)
      - Returned card has item_type='grammar_rule' and direction='passive'
    Edge cases:
      - PUT with 'known' → rule excluded from /srs/due
      - PUT with 'unknown' → no SRS card created
      - Double PUT with 'learning' → idempotent (exactly one card)
      - German rule NOT returned for /srs/due?language=fr
"""
import uuid

import pytest
from httpx import AsyncClient
from ._email_helper import make_test_email

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
SRS_DUE  = "/api/v1/srs/due"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _email() -> str:
    return make_test_email()


async def _register_and_login(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    r = await client.post(LOGIN, json={"email": email, "password": "password123"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    uid = str(await db_pool.fetchval("SELECT user_id FROM users WHERE email = $1", email))
    return headers, uid


async def _get_german_rule(db_pool) -> tuple[int, str, str]:
    """Return (rule_id, slug, title) for a seeded German grammar rule.

    ORDER BY rule_id: pick the lowest-id (seeded-at-startup) rule, never a
    transient high-id `_testrule_` row that another worker's catalog-survival
    test inserts then deletes. An unscoped `LIMIT 1` here flaked under
    `pytest -n auto` — the round-1 shared-row-pick pattern on grammar_rule_table,
    surfaced by the round-2/3 inserts (grammar_rule_table.language DEFAULTs to
    'de', so those `_testrule_` rows land in this WHERE clause).
    """
    row = await db_pool.fetchrow(
        "SELECT rule_id, slug, title FROM grammar_rule_table WHERE language = 'de' "
        "ORDER BY rule_id LIMIT 1"
    )
    if row is None:
        pytest.skip("No grammar rules seeded for language='de'")
    return row["rule_id"], row["slug"], row["title"]


async def _get_passive_card(db_pool, uid: str, rule_id: int) -> dict | None:
    row = await db_pool.fetchrow(
        """
        SELECT interval_days, ease_factor, repetitions
          FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2
           AND item_type = 'grammar_rule' AND direction = 'passive'
        """,
        uid, rule_id,
    )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_put_grammar_rule_learning_returns_200(client: AsyncClient, db_pool):
    rule_id, slug, title = await _get_german_rule(db_pool)
    headers, _ = await _register_and_login(client, db_pool, _email())

    resp = await client.put(
        f"/api/v1/words/grammar_rule/{rule_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["item_type"] == "grammar_rule"
    assert data["status"] == "learning"
    assert data["item_id"] == rule_id


async def test_put_grammar_rule_learning_creates_passive_srs_card(
    client: AsyncClient, db_pool
):
    rule_id, slug, title = await _get_german_rule(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    await client.put(
        f"/api/v1/words/grammar_rule/{rule_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    card = await _get_passive_card(db_pool, uid, rule_id)
    assert card is not None
    assert card["interval_days"] == 1.0
    assert card["ease_factor"] == 2.5
    assert card["repetitions"] == 0


async def test_put_grammar_rule_learning_no_active_card_created(
    client: AsyncClient, db_pool
):
    """Grammar rules are passive-only — no active SRS card must be created."""
    rule_id, slug, title = await _get_german_rule(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    await client.put(
        f"/api/v1/words/grammar_rule/{rule_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    active_card = await db_pool.fetchrow(
        """
        SELECT card_id FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2
           AND item_type = 'grammar_rule' AND direction = 'active'
        """,
        uid, rule_id,
    )
    assert active_card is None


async def test_grammar_rule_appears_in_srs_due_with_title_as_display_text(
    client: AsyncClient, db_pool
):
    """GET /srs/due returns the grammar rule card; display_text must equal the title."""
    rule_id, slug, title = await _get_german_rule(db_pool)
    headers, _ = await _register_and_login(client, db_pool, _email())

    await client.put(
        f"/api/v1/words/grammar_rule/{rule_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    resp = await client.get(SRS_DUE, params={"language": "de"}, headers=headers)
    assert resp.status_code == 200

    rule_cards = [
        c for c in resp.json()
        if c["item_type"] == "grammar_rule" and c["item_id"] == rule_id
    ]
    assert len(rule_cards) >= 1
    assert rule_cards[0]["display_text"] == title
    assert rule_cards[0]["direction"] == "passive"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

async def test_put_grammar_rule_known_excludes_from_srs_due(client: AsyncClient, db_pool):
    """Promoting to 'known' must remove the rule from /srs/due."""
    rule_id, slug, title = await _get_german_rule(db_pool)
    headers, _ = await _register_and_login(client, db_pool, _email())

    await client.put(
        f"/api/v1/words/grammar_rule/{rule_id}/status",
        json={"status": "learning"},
        headers=headers,
    )
    await client.put(
        f"/api/v1/words/grammar_rule/{rule_id}/status",
        json={"status": "known"},
        headers=headers,
    )

    resp = await client.get(SRS_DUE, params={"language": "de"}, headers=headers)
    assert resp.status_code == 200
    rule_cards = [
        c for c in resp.json()
        if c["item_type"] == "grammar_rule" and c["item_id"] == rule_id
    ]
    assert rule_cards == []


async def test_put_grammar_rule_unknown_creates_no_srs_card(client: AsyncClient, db_pool):
    """status_marked_unknown has no SRS action — no card must be created."""
    rule_id, slug, title = await _get_german_rule(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    await client.put(
        f"/api/v1/words/grammar_rule/{rule_id}/status",
        json={"status": "unknown"},
        headers=headers,
    )

    card = await db_pool.fetchrow(
        "SELECT card_id FROM srs_cards WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'grammar_rule'",
        uid, rule_id,
    )
    assert card is None


async def test_double_put_grammar_rule_learning_is_idempotent(
    client: AsyncClient, db_pool
):
    """Pressing 'Add to study' twice must not create a duplicate SRS card."""
    rule_id, slug, title = await _get_german_rule(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    for _ in range(2):
        await client.put(
            f"/api/v1/words/grammar_rule/{rule_id}/status",
            json={"status": "learning"},
            headers=headers,
        )

    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM srs_cards WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'grammar_rule'",
        uid, rule_id,
    )
    assert count == 1


# ---------------------------------------------------------------------------
# enrich_grammar_rules (#5c) — feeds recommendation_service.enrich_by_type
# ---------------------------------------------------------------------------

async def test_enrich_grammar_rules_returns_metadata(db_pool):
    from backend.services.grammar_service import enrich_grammar_rules

    rule_id, _, title = await _get_german_rule(db_pool)
    uid = await db_pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING user_id",
        make_test_email(),
    )
    uid = str(uid)
    # Track the rule so passive_level / status are non-default.
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status, passive_level) "
        "VALUES ($1::uuid, $2, 'grammar_rule', 'learning', 2)",
        uid, rule_id,
    )

    enrichment = await enrich_grammar_rules(db_pool, uid, [rule_id], "de")

    assert rule_id in enrichment
    meta = enrichment[rule_id]
    assert meta["display_text"]   == title
    assert meta["secondary_text"] is not None   # rule_type
    assert meta["current_status"] == "learning"
    assert meta["passive_level"]  == 2
    assert meta["active_level"]   == 0


async def test_enrich_grammar_rules_omits_unknown_ids(db_pool):
    from backend.services.grammar_service import enrich_grammar_rules

    uid = await db_pool.fetchval(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING user_id",
        make_test_email(),
    )
    uid = str(uid)

    enrichment = await enrich_grammar_rules(db_pool, uid, [999_999_999], "de")
    assert enrichment == {}


async def test_enrich_grammar_rules_empty_input_is_empty_output(db_pool):
    from backend.services.grammar_service import enrich_grammar_rules
    assert await enrich_grammar_rules(db_pool, str(uuid.uuid4()), [], "de") == {}


async def test_german_grammar_rule_not_in_due_for_french(client: AsyncClient, db_pool):
    """A German grammar rule must NOT appear when querying /srs/due?language=fr."""
    rule_id, slug, title = await _get_german_rule(db_pool)
    headers, _ = await _register_and_login(client, db_pool, _email())

    await client.put(
        f"/api/v1/words/grammar_rule/{rule_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    resp = await client.get(SRS_DUE, params={"language": "fr"}, headers=headers)
    assert resp.status_code == 200
    rule_cards = [
        c for c in resp.json()
        if c["item_type"] == "grammar_rule" and c["item_id"] == rule_id
    ]
    assert rule_cards == []
