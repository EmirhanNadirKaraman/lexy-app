"""
Pinned audit holes — one test per known bug from docs/WORKFLOW_AUDIT.md.

Each test is marked xfail today. When a fix lands, flip strict=True (or remove
the mark) and the test enforces the fix going forward. This is the cheapest way
to make sure we don't accidentally re-introduce a hole after closing it.

The hole numbers refer to docs/WORKFLOW_AUDIT.md.
"""
import uuid

import pytest
from httpx import AsyncClient

from backend.services.progression_service import _RULES, compute_delta
from ._email_helper import make_test_email
from ._auth_helper import register_and_login

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"


def _email() -> str:
    return make_test_email()


async def _register(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    """Register + login. Returns (auth_headers, user_id_str).

    Delegates to the shared helper; see tests/_auth_helper.py.
    """
    return await register_and_login(client, db_pool, email)


# ---------------------------------------------------------------------------
# Hole 7 — status_marked_unknown is a no-op
# ---------------------------------------------------------------------------

# Hole 7 — RESOLVED 2026-05-18. status_marked_unknown now resets both passive
# and active SRS cards via the SM-2 incorrect branch. action='incorrect' is a
# no-op when the card doesn't exist, so this never fabricates a new card.
# Test kept as a regression guard.
def test_status_marked_unknown_should_reset_srs():
    d = compute_delta("status_marked_unknown")
    assert d.passive_srs == "incorrect"
    assert d.active_srs == "incorrect"
    # Levels intentionally not touched.
    assert d.passive_delta == 0
    assert d.active_delta == 0


# ---------------------------------------------------------------------------
# Hole 8 — status_marked_known gives only active_delta=1, not threshold
# ---------------------------------------------------------------------------

# Hole 8 was originally "status_marked_known gives active_delta=1, below
# ACTIVE_MASTERY_THRESHOLD". The design decision changed: manual known is
# user confidence, NOT production evidence. The new rule has active_delta=0
# by design — no fabrication of active mastery. The active path now only
# advances through real production events (guided_counted,
# free_chat_used_correctly, active_review_correct). See progression_service.py
# for the documented reasoning. Test removed.


# ---------------------------------------------------------------------------
# Hole 5d (now also: passive_review_correct doesn't bump passive_level)
# ---------------------------------------------------------------------------

# Hole 15 — RESOLVED 2026-05-18. passive_review_correct now has passive_delta=1.
# A controlled review is at least as strong as a subtitle click, which gives
# passive_delta=1 — they're now consistent. Kept as a regression guard.
def test_passive_review_correct_bumps_passive_level():
    d = compute_delta("passive_review_correct")
    assert d.passive_delta >= 1


# ---------------------------------------------------------------------------
# Hole 5 — transcript context excluded from frequent-unknowns
# (mirror of test_insights.test_frequent_unknowns_includes_transcript_clicks)
# kept here so all audit holes are searchable in one place
# ---------------------------------------------------------------------------

# Hole 5 — RESOLVED 2026-05-18. most_frequent_unknown_items now includes
# 'transcript' in its context IN clause. Subtitle clicks surface in the
# "Keeps coming up" insight card. Kept as a regression guard.
async def test_transcript_context_in_frequent_unknowns_aggregation(db_pool, make_word):
    """Direct test of the aggregation, not via HTTP."""
    from backend.services.usage_events_service import most_frequent_unknown_items

    word_id, _ = await make_word()
    uid = uuid.uuid4()

    try:
        await db_pool.execute(
            "INSERT INTO users (user_id, email, password_hash) VALUES ($1, $2, 'x')",
            uid, make_test_email(),
        )
        await db_pool.execute(
            "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
            "VALUES ($1, $2, 'word', 'unknown')",
            uid, word_id,
        )
        for _ in range(3):
            await db_pool.execute(
                "INSERT INTO word_usage_events (user_id, item_id, item_type, context, outcome) "
                "VALUES ($1, $2, 'word', 'transcript', 'seen')",
                uid, word_id,
            )

        result = await most_frequent_unknown_items(db_pool, str(uid))
        assert any(r["item_id"] == word_id for r in result)
    finally:
        await db_pool.execute("DELETE FROM users WHERE user_id = $1", uid)


# ---------------------------------------------------------------------------
# Hole 0a — SRS review UI shows German word as prompt for both directions
# Can't test UI from here, but we can test that the API delivers an English
# prompt for active cards. Will become test-able when the schema gains a
# prompt_text/gloss field.
# ---------------------------------------------------------------------------

# Hole 0a — PARTIALLY RESOLVED 2026-05-19. Backend payload now carries
# prompt_text + answer_text (#0a-1). The frontend UI rewrite is #0a-2 — still
# pending. This regression guard locks in the backend half of the contract.
async def test_active_srs_card_has_english_prompt(client: AsyncClient, db_pool, srs_word):
    word_id, _, language = srs_word
    headers, _ = await _register(client, db_pool, _email())

    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )
    resp = await client.get("/api/v1/srs/due", params={"language": language}, headers=headers)
    active = next((c for c in resp.json() if c["direction"] == "active"), None)
    assert active is not None
    assert "prompt_text" in active and active["prompt_text"], (
        "active card should carry an English gloss to test production, not the German display_text"
    )


# ---------------------------------------------------------------------------
# Phrase support in free chat — Hole 18 / TODO #5b
# ---------------------------------------------------------------------------

# Hole 18 — RESOLVED 2026-05-19. match_learning_words now delegates phrase
# detection to matcher_service.match_sentence_with_ids and joins the result to
# user_word_knowledge. Regression guard.
async def test_match_learning_words_matches_phrases(db_pool):
    from backend.services.chat_service import match_learning_words

    row = await db_pool.fetchrow(
        "SELECT phrase_id, surface_form, language FROM phrase_table LIMIT 1"
    )
    if row is None:
        pytest.skip("phrase_table is empty")
    phrase_id, surface_form, language = row["phrase_id"], row["surface_form"], row["language"]

    uid = uuid.uuid4()
    try:
        await db_pool.execute(
            "INSERT INTO users (user_id, email, password_hash) VALUES ($1, $2, 'x')",
            uid, make_test_email(),
        )
        await db_pool.execute(
            "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
            "VALUES ($1, $2, 'phrase', 'learning')",
            uid, phrase_id,
        )

        result = await match_learning_words(db_pool, str(uid), surface_form, language)
        assert any(r["item_id"] == phrase_id and r["item_type"] == "phrase" for r in result)
    finally:
        await db_pool.execute("DELETE FROM users WHERE user_id = $1", uid)


# ---------------------------------------------------------------------------
# Sanity: every audit-hole xfail above references an actual rule in _RULES
# (catches typos when the rule table is refactored)
# ---------------------------------------------------------------------------

def test_rule_table_contains_expected_events():
    expected = {
        "transcript_clicked",
        "status_marked_learning",
        "status_marked_known",
        "status_marked_unknown",
        "passive_review_correct",
        "passive_review_incorrect",
        "active_review_correct",
        "active_review_incorrect",
        "guided_counted",
        "guided_used",
        "guided_not_used",
        "free_chat_matched",
        "free_chat_used_correctly",
        "free_chat_mixed_lang",
    }
    assert expected <= set(_RULES.keys()), (
        f"Missing rules: {expected - set(_RULES.keys())}"
    )
