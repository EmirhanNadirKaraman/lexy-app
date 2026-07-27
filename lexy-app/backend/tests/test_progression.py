"""
Passive/active progression tests.

Unit tests (no DB) — test compute_delta:
  - correct delta values for every implemented event
  - unknown event raises ValueError

Integration tests (real DB) — test apply_progression:
  - guided_counted updates passive_level + active_level + times_used_correctly
  - guided_counted creates active SRS card
  - guided_counted creates passive SRS card
  - guided_counted repeated → auto-promotion to 'known' at ACTIVE_MASTERY_THRESHOLD
  - guided_used updates passive_level only, does NOT create active SRS card
  - guided_not_used penalises active SRS card
  - guided_not_used does NOT touch levels
  - status_marked_learning creates passive SRS card
  - status_marked_known boosts both levels and creates both SRS cards
  - passive auto-promotion: passive_level >= PASSIVE_PROMOTION_THRESHOLD → 'learning'
"""

import pytest

from backend.services.progression_service import (
    ACTIVE_MASTERY_THRESHOLD,
    PASSIVE_PROMOTION_THRESHOLD,
    _is_demotion,
    apply_progression,
    compute_delta,
)
from ._email_helper import make_test_email


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_word_id(pool, offset: int = 0) -> int:
    # Owned, uniquely-named word (xdist-safe — see conftest / docs/TESTS.md).
    # `offset` retained for call-site compatibility; each call now yields a
    # distinct word, reaped after the test by conftest's _reap_owned_words.
    from ._word_helper import insert_owned_word
    wid, _ = await insert_owned_word(pool)
    return wid


async def _make_user(pool) -> str:
    from backend.services.auth_service import register_user
    email = make_test_email()
    user = await register_user(pool, email, "password123")
    return str(user["user_id"])


async def _get_uwk(pool, user_id: str, item_id: int) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT passive_level, active_level, times_seen, times_used_correctly, status
          FROM user_word_knowledge
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
        """,
        user_id, item_id,
    )
    return dict(row) if row else None


async def _get_srs(pool, user_id: str, item_id: int, direction: str) -> dict | None:
    row = await pool.fetchrow(
        """
        SELECT interval_days, ease_factor, repetitions, due_date
          FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word' AND direction = $3
        """,
        user_id, item_id, direction,
    )
    return dict(row) if row else None


async def _apply(pool, user_id, item_id, event):
    await apply_progression(pool, user_id, item_id, "word", event)


# ---------------------------------------------------------------------------
# Unit tests — pure function, no DB
# ---------------------------------------------------------------------------

def test_compute_delta_guided_counted():
    d = compute_delta("guided_counted")
    assert d.passive_delta == 1
    assert d.active_delta == 1
    assert d.times_used_correctly_delta == 1
    assert d.times_seen_delta == 0
    assert d.passive_srs == "correct"
    assert d.active_srs == "correct"


def test_compute_delta_guided_used():
    d = compute_delta("guided_used")
    assert d.passive_delta == 1
    assert d.active_delta == 0
    assert d.times_used_correctly_delta == 0
    assert d.passive_srs == "correct"
    assert d.active_srs is None


def test_compute_delta_guided_not_used():
    d = compute_delta("guided_not_used")
    assert d.passive_delta == 0
    assert d.active_delta == 0
    assert d.passive_srs is None
    assert d.active_srs == "incorrect"


def test_compute_delta_status_marked_learning():
    d = compute_delta("status_marked_learning")
    assert d.passive_delta == 1
    assert d.active_delta == 0
    assert d.times_seen_delta == 1
    assert d.passive_srs == "create"
    assert d.active_srs == "create"


def test_compute_delta_status_marked_known():
    """Manual 'Known' is user confidence — must not fabricate active progress.

    Status flip to 'known' is written inside apply_progression's transaction
    (via status_override). This rule only advances the passive SRS card;
    active is untouched.
    """
    d = compute_delta("status_marked_known")
    assert d.passive_delta == 0
    assert d.active_delta == 0
    assert d.times_used_correctly_delta == 0
    assert d.passive_srs == "correct"
    assert d.active_srs is None


def test_compute_delta_status_marked_unknown():
    """Manual 'Unknown' resets existing SRS cards but leaves levels alone."""
    d = compute_delta("status_marked_unknown")
    assert d.passive_delta == 0
    assert d.active_delta == 0
    assert d.times_seen_delta == 0
    assert d.times_used_correctly_delta == 0
    assert d.passive_srs == "incorrect"
    assert d.active_srs == "incorrect"


def test_compute_delta_unknown_event_raises():
    with pytest.raises(ValueError, match="Unknown progression event"):
        compute_delta("definitely_not_a_real_event")


def test_compute_delta_returns_frozen_dataclass():
    d = compute_delta("guided_counted")
    with pytest.raises(Exception):
        d.passive_delta = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Integration tests — real DB
# ---------------------------------------------------------------------------

async def test_guided_counted_updates_levels(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "guided_counted")

    row = await _get_uwk(db_pool, uid, wid)
    assert row is not None
    assert row["passive_level"] == 1
    assert row["active_level"] == 1
    assert row["times_used_correctly"] == 1


async def test_guided_counted_creates_active_srs_card(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "guided_counted")

    card = await _get_srs(db_pool, uid, wid, "active")
    assert card is not None
    assert card["repetitions"] == 1


async def test_guided_counted_creates_passive_srs_card(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "guided_counted")

    card = await _get_srs(db_pool, uid, wid, "passive")
    assert card is not None
    assert card["repetitions"] == 1


async def test_guided_counted_promotes_to_known_at_threshold(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    for _ in range(ACTIVE_MASTERY_THRESHOLD):
        await _apply(db_pool, uid, wid, "guided_counted")

    row = await _get_uwk(db_pool, uid, wid)
    assert row["status"] == "known"
    assert row["active_level"] == ACTIVE_MASTERY_THRESHOLD


async def test_guided_counted_srs_advances_on_repeat(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "guided_counted")
    card_after_1 = await _get_srs(db_pool, uid, wid, "active")

    await _apply(db_pool, uid, wid, "guided_counted")
    card_after_2 = await _get_srs(db_pool, uid, wid, "active")

    assert card_after_2["interval_days"] > card_after_1["interval_days"]
    assert card_after_2["repetitions"] == 2


async def test_guided_used_updates_passive_only(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "guided_used")

    row = await _get_uwk(db_pool, uid, wid)
    assert row["passive_level"] == 1
    assert row["active_level"] == 0
    assert row["times_used_correctly"] == 0


async def test_guided_used_does_not_create_active_srs_card(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "guided_used")

    card = await _get_srs(db_pool, uid, wid, "active")
    assert card is None


async def test_guided_not_used_does_not_touch_levels(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "guided_not_used")

    row = await _get_uwk(db_pool, uid, wid)
    # Row may not even exist if there were no prior events
    if row is not None:
        assert row["passive_level"] == 0
        assert row["active_level"] == 0


async def test_guided_not_used_penalises_existing_active_card(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Create a card with ease > 1.3 so we can detect penalisation
    await _apply(db_pool, uid, wid, "guided_counted")
    card_before = await _get_srs(db_pool, uid, wid, "active")
    assert card_before is not None

    await _apply(db_pool, uid, wid, "guided_not_used")
    card_after = await _get_srs(db_pool, uid, wid, "active")

    assert card_after["interval_days"] == 1.0    # reset to 1 on incorrect
    assert card_after["repetitions"] == 0
    assert card_after["ease_factor"] < card_before["ease_factor"]


async def test_guided_not_used_noop_when_no_card(db_pool):
    """guided_not_used with no existing card should not raise or create anything."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "guided_not_used")  # should not raise

    card = await _get_srs(db_pool, uid, wid, "active")
    assert card is None


async def test_status_marked_learning_creates_passive_card(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "status_marked_learning")

    card = await _get_srs(db_pool, uid, wid, "passive")
    assert card is not None
    assert card["repetitions"] == 0    # 'create' action — not advanced yet


async def test_status_marked_learning_creates_active_card(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "status_marked_learning")

    card = await _get_srs(db_pool, uid, wid, "active")
    assert card is not None
    assert card["repetitions"] == 0    # 'create' action — not advanced yet


async def test_status_marked_learning_does_not_increment_active_level(db_pool):
    """#0b regression: scheduling an active card on Learning must NOT count as
    active production evidence. Active level only grows from real production."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "status_marked_learning")

    row = await _get_uwk(db_pool, uid, wid)
    assert row is not None
    assert row["active_level"] == 0


async def test_status_marked_learning_does_not_increment_times_used_correctly(db_pool):
    """#0b regression: marking Learning is exposure, not a correct production."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "status_marked_learning")

    row = await _get_uwk(db_pool, uid, wid)
    assert row is not None
    assert row["times_used_correctly"] == 0


async def test_status_marked_learning_does_not_duplicate_cards(db_pool):
    """#0b regression: re-marking Learning must be idempotent — both SRS card
    inserts use ON CONFLICT DO NOTHING, so the second click should leave the
    existing cards' SM-2 state untouched."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "status_marked_learning")
    passive_before = await _get_srs(db_pool, uid, wid, "passive")
    active_before  = await _get_srs(db_pool, uid, wid, "active")
    assert passive_before is not None
    assert active_before  is not None

    # Advance the cards a bit so we can detect any clobber.
    await _apply(db_pool, uid, wid, "passive_review_correct")
    passive_mid = await _get_srs(db_pool, uid, wid, "passive")
    assert passive_mid["repetitions"] == passive_before["repetitions"] + 1

    # Second status_marked_learning — must not reset either card.
    await _apply(db_pool, uid, wid, "status_marked_learning")

    passive_after = await _get_srs(db_pool, uid, wid, "passive")
    active_after  = await _get_srs(db_pool, uid, wid, "active")

    # No duplicate rows (only one passive + one active card per user/item/direction
    # — enforced by the unique constraint, but assert via raw count too).
    count = await db_pool.fetchval(
        """
        SELECT COUNT(*)
          FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
        """,
        uid, wid,
    )
    assert count == 2

    # Existing cards untouched by the second 'create' action.
    assert passive_after["repetitions"]   == passive_mid["repetitions"]
    assert passive_after["interval_days"] == passive_mid["interval_days"]
    assert active_after["repetitions"]    == active_before["repetitions"]
    assert active_after["interval_days"]  == active_before["interval_days"]


async def test_status_marked_known_creates_passive_card_only(db_pool):
    """Manual known advances passive but must NOT create active SRS card."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "status_marked_known")

    assert await _get_srs(db_pool, uid, wid, "passive") is not None
    assert await _get_srs(db_pool, uid, wid, "active") is None


async def test_status_marked_known_does_not_change_levels(db_pool):
    """Manual known is self-classification, not evidence — leaves levels untouched."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # status_marked_known has no level deltas; called without status_override,
    # no row is created. The router-side caller (test_words.py) covers the
    # status_override=... path that *does* write a row.
    await _apply(db_pool, uid, wid, "status_marked_known")

    row = await _get_uwk(db_pool, uid, wid)
    if row is not None:
        assert row["passive_level"] == 0
        assert row["active_level"] == 0
        assert row["times_used_correctly"] == 0


async def test_status_marked_known_does_not_advance_existing_active_card(db_pool):
    """If the user already had an active card from learning, manual known must not
    advance it as if they answered correctly. The card's SM-2 state stays put.
    """
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Seed an active card by marking learning (creates both passive + active at
    # interval=1.0, reps=0 thanks to #0b).
    await _apply(db_pool, uid, wid, "status_marked_learning")
    before = await _get_srs(db_pool, uid, wid, "active")
    assert before is not None
    assert before["repetitions"] == 0

    # Manual known — must NOT touch the active card.
    await _apply(db_pool, uid, wid, "status_marked_known")

    after = await _get_srs(db_pool, uid, wid, "active")
    assert after is not None
    assert after["repetitions"] == before["repetitions"]
    assert after["interval_days"] == before["interval_days"]
    assert after["ease_factor"] == before["ease_factor"]
    assert after["due_date"] == before["due_date"]


async def test_status_marked_known_does_not_increment_times_used_correctly(db_pool):
    """`times_used_correctly` tracks actual production — confidence clicks must not bump it."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Pre-seed a row so we can read times_used_correctly afterwards.
    await _apply(db_pool, uid, wid, "status_marked_learning")
    before = await _get_uwk(db_pool, uid, wid)
    before_count = before["times_used_correctly"] if before else 0

    await _apply(db_pool, uid, wid, "status_marked_known")

    after = await _get_uwk(db_pool, uid, wid)
    after_count = after["times_used_correctly"] if after else 0
    assert after_count == before_count


# Sanity: existing production events must still bump active_level — the rule
# change must not regress these. (Tests for guided_counted exist above; the
# additions below cover the events that weren't explicitly asserted before.)

async def test_active_review_correct_still_increments_active_level(db_pool):
    """active_review_correct must still bump active_level after the status_marked_known fix."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Need an existing active card so SM-2 advance applies.
    await _apply(db_pool, uid, wid, "status_marked_learning")
    before = await _get_uwk(db_pool, uid, wid)
    before_active = before["active_level"] if before else 0

    await _apply(db_pool, uid, wid, "active_review_correct")

    after = await _get_uwk(db_pool, uid, wid)
    assert after["active_level"] == before_active + 1
    assert after["times_used_correctly"] >= 1


async def test_free_chat_used_correctly_still_increments_active_level(db_pool):
    """free_chat_used_correctly must still bump active_level (German production)."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "free_chat_used_correctly")

    row = await _get_uwk(db_pool, uid, wid)
    assert row["active_level"] >= 1
    assert row["times_used_correctly"] >= 1


# ---------------------------------------------------------------------------
# status_marked_unknown — resets existing cards, never creates new ones
# ---------------------------------------------------------------------------

async def test_status_marked_unknown_resets_existing_passive_card(db_pool):
    """An advanced passive card should be knocked back to 1 day, reps=0."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Build up a passive card with multiple correct reviews.
    await _apply(db_pool, uid, wid, "status_marked_learning")
    for _ in range(3):
        await _apply(db_pool, uid, wid, "passive_review_correct")

    before = await _get_srs(db_pool, uid, wid, "passive")
    assert before["repetitions"] >= 3
    assert before["interval_days"] > 1.0

    await _apply(db_pool, uid, wid, "status_marked_unknown")

    after = await _get_srs(db_pool, uid, wid, "passive")
    assert after is not None
    assert after["interval_days"] == 1.0
    assert after["repetitions"] == 0
    # ease drops by 0.15, floored at 1.3
    assert after["ease_factor"] < before["ease_factor"]


async def test_status_marked_unknown_resets_existing_active_card(db_pool):
    """If active card already exists (from learning), it should also reset."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Learning creates both cards (#0b); a guided_counted then advances active.
    await _apply(db_pool, uid, wid, "status_marked_learning")
    await _apply(db_pool, uid, wid, "guided_counted")

    before = await _get_srs(db_pool, uid, wid, "active")
    assert before is not None
    assert before["repetitions"] >= 1

    await _apply(db_pool, uid, wid, "status_marked_unknown")

    after = await _get_srs(db_pool, uid, wid, "active")
    assert after is not None
    assert after["interval_days"] == 1.0
    assert after["repetitions"] == 0


async def test_status_marked_unknown_does_not_create_active_card_when_missing(db_pool):
    """Critical: clicking Unknown must NEVER fabricate an active card.

    Verifies the documented contract that action='incorrect' is a no-op when
    the card doesn't exist (progression_service.py line ~333).
    """
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Confirm precondition: no active card.
    assert await _get_srs(db_pool, uid, wid, "active") is None

    await _apply(db_pool, uid, wid, "status_marked_unknown")

    assert await _get_srs(db_pool, uid, wid, "active") is None


async def test_status_marked_unknown_does_not_create_passive_card_when_missing(db_pool):
    """Same defensive check for the passive side."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    assert await _get_srs(db_pool, uid, wid, "passive") is None

    await _apply(db_pool, uid, wid, "status_marked_unknown")

    assert await _get_srs(db_pool, uid, wid, "passive") is None


async def test_status_marked_unknown_does_not_change_levels(db_pool):
    """Levels are not touched by the rule — no fabricated regression evidence."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Build up levels via real activity.
    for _ in range(3):
        await _apply(db_pool, uid, wid, "guided_counted")
    before = await _get_uwk(db_pool, uid, wid)
    assert before["passive_level"] == 3
    assert before["active_level"]  == 3

    await _apply(db_pool, uid, wid, "status_marked_unknown")

    after = await _get_uwk(db_pool, uid, wid)
    assert after["passive_level"] == before["passive_level"]
    assert after["active_level"]  == before["active_level"]
    assert after["times_used_correctly"] == before["times_used_correctly"]


# ---------------------------------------------------------------------------
# passive_review_correct now bumps passive_level
# ---------------------------------------------------------------------------

async def test_passive_review_correct_increments_passive_level(db_pool):
    """A correct passive review should advance the 'Understood' metric."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Card needs to exist first (review flow always operates on a real card).
    await _apply(db_pool, uid, wid, "status_marked_learning")
    before = await _get_uwk(db_pool, uid, wid)
    before_passive = before["passive_level"]

    await _apply(db_pool, uid, wid, "passive_review_correct")

    after = await _get_uwk(db_pool, uid, wid)
    assert after["passive_level"] == before_passive + 1


async def test_passive_review_correct_still_advances_card(db_pool):
    """Adding passive_delta=1 to the rule must not break SM-2 card advancement."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await _apply(db_pool, uid, wid, "status_marked_learning")
    before = await _get_srs(db_pool, uid, wid, "passive")

    await _apply(db_pool, uid, wid, "passive_review_correct")

    after = await _get_srs(db_pool, uid, wid, "passive")
    assert after["repetitions"] == before["repetitions"] + 1
    assert after["interval_days"] > before["interval_days"]
    assert after["ease_factor"] >= before["ease_factor"]


# ---------------------------------------------------------------------------
# status_override — atomic status + progression in one transaction (#2)
# ---------------------------------------------------------------------------

async def test_status_override_learning_writes_status_and_creates_cards(db_pool):
    """One call should atomically: set status='learning', bump passive_level,
    and create both passive and active SRS cards."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    result = await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )

    assert result is not None
    assert result["status"] == "learning"
    assert result["passive_level"] == 1
    assert result["times_seen"] == 1

    # Persisted state matches.
    row = await _get_uwk(db_pool, uid, wid)
    assert row["status"] == "learning"

    # SRS cards created per #0b.
    assert await _get_srs(db_pool, uid, wid, "passive") is not None
    assert await _get_srs(db_pool, uid, wid, "active") is not None


async def test_status_override_known_writes_status_without_fabricating_active(db_pool):
    """status_override='known' must persist the status flip but the rule's
    no-active-credit semantics must hold."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    result = await apply_progression(
        db_pool, uid, wid, "word", "status_marked_known", status_override="known",
    )

    assert result is not None
    assert result["status"] == "known"
    assert result["active_level"] == 0
    assert result["times_used_correctly"] == 0

    # No active card was created (rule has active_srs=None).
    assert await _get_srs(db_pool, uid, wid, "active") is None
    # Passive card exists (rule has passive_srs='correct').
    assert await _get_srs(db_pool, uid, wid, "passive") is not None


async def test_status_override_unknown_writes_status_and_resets_existing_cards(db_pool):
    """status_override='unknown' must persist the status flip AND knock down
    existing SRS schedules via the rule's incorrect actions."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Build up cards via the normal learning loop.
    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    await _apply(db_pool, uid, wid, "guided_counted")  # advances active
    for _ in range(2):
        await _apply(db_pool, uid, wid, "passive_review_correct")
    before_p = await _get_srs(db_pool, uid, wid, "passive")
    before_a = await _get_srs(db_pool, uid, wid, "active")
    assert before_p["interval_days"] > 1.0
    assert before_a["repetitions"] >= 1

    # Now click Unknown.
    result = await apply_progression(
        db_pool, uid, wid, "word", "status_marked_unknown", status_override="unknown",
    )

    assert result is not None
    assert result["status"] == "unknown"

    after_p = await _get_srs(db_pool, uid, wid, "passive")
    after_a = await _get_srs(db_pool, uid, wid, "active")
    assert after_p["interval_days"] == 1.0
    assert after_p["repetitions"] == 0
    assert after_a["interval_days"] == 1.0
    assert after_a["repetitions"] == 0


async def test_status_override_creates_row_even_without_level_deltas(db_pool):
    """status_marked_known has no level deltas. Pre-#2 the row would be created
    by upsert_word_status (now deleted). After #2, apply_progression itself
    must create the row via status_override on a fresh user."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # No prior row.
    assert await _get_uwk(db_pool, uid, wid) is None

    result = await apply_progression(
        db_pool, uid, wid, "word", "status_marked_known", status_override="known",
    )

    assert result is not None
    assert result["status"] == "known"
    row = await _get_uwk(db_pool, uid, wid)
    assert row is not None
    assert row["status"] == "known"


async def test_status_override_overwrites_existing_status(db_pool):
    """When the row already exists with a different status, status_override must
    update it (ON CONFLICT DO UPDATE SET status = ...)."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    assert (await _get_uwk(db_pool, uid, wid))["status"] == "learning"

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_known", status_override="known",
    )
    assert (await _get_uwk(db_pool, uid, wid))["status"] == "known"


async def test_apply_progression_without_status_override_does_not_touch_status(db_pool):
    """Sanity: other event paths (SRS reviews, chat) must not silently change
    the status field when they don't pass status_override."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Establish status='learning'.
    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )

    # A passive review correct without status_override — status must remain 'learning'.
    await _apply(db_pool, uid, wid, "passive_review_correct")
    assert (await _get_uwk(db_pool, uid, wid))["status"] == "learning"


async def test_apply_progression_returns_none_when_no_write_happens(db_pool):
    """Events with no level deltas, no SRS-touching-uwk write, and no status_override
    (e.g. guided_not_used when no card exists) should return None — confirms the
    return contract for callers that ignore it (SRS reviews, etc.)."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # status_marked_unknown without status_override: rule has only SRS actions
    # (which no-op when cards are missing) and zero level deltas. No write.
    result = await apply_progression(
        db_pool, uid, wid, "word", "status_marked_unknown",
    )
    assert result is None


async def test_passive_promotion_to_learning(db_pool):
    """Accumulating passive exposures auto-promotes from 'unknown' to 'learning'."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Each guided_used gives passive_delta=1; hit PASSIVE_PROMOTION_THRESHOLD
    for _ in range(PASSIVE_PROMOTION_THRESHOLD):
        await _apply(db_pool, uid, wid, "guided_used")

    row = await _get_uwk(db_pool, uid, wid)
    assert row["passive_level"] == PASSIVE_PROMOTION_THRESHOLD
    assert row["status"] == "learning"


async def test_active_mastery_overrides_passive_promotion(db_pool):
    """If active_level hits mastery while status is 'unknown', word goes straight to 'known'."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    for _ in range(ACTIVE_MASTERY_THRESHOLD):
        await _apply(db_pool, uid, wid, "guided_counted")

    row = await _get_uwk(db_pool, uid, wid)
    assert row["status"] == "known"   # not just 'learning'


# ---------------------------------------------------------------------------
# Hole 9 — passive mastery promotes 'learning' → 'known'
#
# Before: 'learning' was a one-way trap. passive_review_correct raised
# passive_level but no auto-promotion took the user out of 'learning'. Only
# active-track events (guided_counted / free_chat_used_correctly /
# active_review_correct) could reach 'known'.
#
# After: when passive_level reaches the per-user passive_reps_for_known
# threshold (same setting that already gates 'unknown' → 'learning'), a
# learning item auto-promotes to 'known'. Active level + active SRS are
# untouched — passive mastery is recognition mastery, not production.
# ---------------------------------------------------------------------------

async def test_passive_mastery_promotes_learning_to_known(db_pool):
    """Status 'learning' + passive_level reaches threshold → 'known'."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Start at status='learning' (override sets it explicitly + bumps passive to 1)
    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    row = await _get_uwk(db_pool, uid, wid)
    assert row["status"] == "learning"
    assert row["passive_level"] == 1

    # Drive passive_level up via passive reviews until it crosses the threshold.
    # passive_review_correct gives passive_delta=1, so PASSIVE_PROMOTION_THRESHOLD-1
    # more events get us to the threshold value.
    for _ in range(PASSIVE_PROMOTION_THRESHOLD - 1):
        await _apply(db_pool, uid, wid, "passive_review_correct")

    row = await _get_uwk(db_pool, uid, wid)
    assert row["passive_level"] >= PASSIVE_PROMOTION_THRESHOLD
    assert row["status"] == "known"


async def test_passive_below_threshold_keeps_learning(db_pool):
    """At passive_level = threshold - 1, status stays 'learning'."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    # Already at passive_level=1 from the override event. Bring it to
    # threshold-1 (so still below threshold).
    for _ in range(PASSIVE_PROMOTION_THRESHOLD - 2):
        await _apply(db_pool, uid, wid, "passive_review_correct")

    row = await _get_uwk(db_pool, uid, wid)
    assert row["passive_level"] == PASSIVE_PROMOTION_THRESHOLD - 1
    assert row["status"] == "learning"


async def test_passive_promotion_does_not_touch_active_level(db_pool):
    """Hole 9: auto-promotion to 'known' via passive evidence must NOT
    fabricate active mastery. active_level + times_used_correctly stay zero."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    for _ in range(PASSIVE_PROMOTION_THRESHOLD - 1):
        await _apply(db_pool, uid, wid, "passive_review_correct")

    row = await _get_uwk(db_pool, uid, wid)
    assert row["status"] == "known"
    assert row["active_level"] == 0
    assert row["times_used_correctly"] == 0


async def test_passive_promotion_does_not_advance_active_srs(db_pool):
    """Hole 9: auto-promotion to 'known' via passive evidence must NOT advance
    the active SRS card (created by status_marked_learning at reps=0, interval=1)."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    active_before = await _get_srs(db_pool, uid, wid, "active")
    assert active_before is not None
    assert active_before["repetitions"] == 0

    for _ in range(PASSIVE_PROMOTION_THRESHOLD - 1):
        await _apply(db_pool, uid, wid, "passive_review_correct")

    row = await _get_uwk(db_pool, uid, wid)
    assert row["status"] == "known"

    active_after = await _get_srs(db_pool, uid, wid, "active")
    assert active_after is not None
    assert active_after["repetitions"]   == active_before["repetitions"]
    assert active_after["interval_days"] == active_before["interval_days"]
    assert active_after["ease_factor"]   == active_before["ease_factor"]


async def test_passive_promotion_known_status_is_sticky(db_pool):
    """Once 'known', further passive reviews must not flip status back."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    for _ in range(PASSIVE_PROMOTION_THRESHOLD - 1):
        await _apply(db_pool, uid, wid, "passive_review_correct")
    assert (await _get_uwk(db_pool, uid, wid))["status"] == "known"

    # Another passive review on the (already known) item.
    await _apply(db_pool, uid, wid, "passive_review_correct")
    assert (await _get_uwk(db_pool, uid, wid))["status"] == "known"


# ---------------------------------------------------------------------------
# Hole 26 — manual demotion resets levels + reschedules cards
#
# Before: known item demoted to learning/unknown kept mastered levels + long
# SRS intervals. State was internally contradictory.
#
# After: a dedicated demotion branch inside apply_progression resets levels
# and reschedules cards. Triggered only when status_override demotes (lower
# rank in unknown < learning < known). Additive rule deltas are bypassed.
#
# Policy:
#   known → learning:  passive=1, active=0, both SRS 'reset' (due+1d, ease kept)
#   known → unknown:   passive=0, active=0, both SRS 'incorrect' (no card created)
#   learning → unknown: passive=0, active=0, both SRS 'incorrect'
# ---------------------------------------------------------------------------


# --- unit tests for the transition detector --------------------------------

def test_is_demotion_known_to_learning():
    assert _is_demotion("known", "learning") is True


def test_is_demotion_known_to_unknown():
    assert _is_demotion("known", "unknown") is True


def test_is_demotion_learning_to_unknown():
    assert _is_demotion("learning", "unknown") is True


def test_is_demotion_unknown_to_learning_is_not_demotion():
    assert _is_demotion("unknown", "learning") is False


def test_is_demotion_learning_to_known_is_not_demotion():
    assert _is_demotion("learning", "known") is False


def test_is_demotion_unknown_to_known_is_not_demotion():
    assert _is_demotion("unknown", "known") is False


def test_is_demotion_same_status_is_not_demotion():
    for s in ("unknown", "learning", "known"):
        assert _is_demotion(s, s) is False


def test_is_demotion_none_prior_is_not_demotion():
    """First-time status set (no prior row) is never a demotion."""
    assert _is_demotion(None, "unknown") is False
    assert _is_demotion(None, "learning") is False
    assert _is_demotion(None, "known") is False


# --- known → learning ------------------------------------------------------

async def _drive_to_known(pool, uid, wid):
    """Build status=known with high passive_level + advanced active card."""
    # status=learning + active card via learning click
    await apply_progression(
        pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    # active mastery via guided_counted × ACTIVE_MASTERY_THRESHOLD → auto-known
    for _ in range(ACTIVE_MASTERY_THRESHOLD):
        await _apply(pool, uid, wid, "guided_counted")


async def test_demote_known_to_learning_resets_passive_level(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _drive_to_known(db_pool, uid, wid)
    before = await _get_uwk(db_pool, uid, wid)
    assert before["status"] == "known"
    assert before["passive_level"] > 1

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )

    row = await _get_uwk(db_pool, uid, wid)
    assert row["status"] == "learning"
    assert row["passive_level"] == 1


async def test_demote_known_to_learning_resets_active_level_to_zero(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _drive_to_known(db_pool, uid, wid)
    before = await _get_uwk(db_pool, uid, wid)
    assert before["active_level"] >= ACTIVE_MASTERY_THRESHOLD

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )

    row = await _get_uwk(db_pool, uid, wid)
    assert row["active_level"] == 0


async def test_demote_known_to_learning_reschedules_passive_card(db_pool):
    """Advanced passive card should drop to interval=1, reps=0, ease preserved."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _drive_to_known(db_pool, uid, wid)
    # Advance the passive card a couple of times so interval > 1.
    for _ in range(2):
        await _apply(db_pool, uid, wid, "passive_review_correct")
    before_p = await _get_srs(db_pool, uid, wid, "passive")
    assert before_p["interval_days"] > 1.0
    before_ease = before_p["ease_factor"]

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )

    after_p = await _get_srs(db_pool, uid, wid, "passive")
    assert after_p["interval_days"] == 1.0
    assert after_p["repetitions"] == 0
    # 'reset' preserves ease — distinguishes it from 'incorrect' which subtracts 0.15.
    assert after_p["ease_factor"] == before_ease


async def test_demote_known_to_learning_reschedules_active_card(db_pool):
    """Advanced active card should drop to interval=1, reps=0, ease preserved."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _drive_to_known(db_pool, uid, wid)
    before_a = await _get_srs(db_pool, uid, wid, "active")
    assert before_a is not None
    assert before_a["repetitions"] >= ACTIVE_MASTERY_THRESHOLD
    before_ease = before_a["ease_factor"]

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )

    after_a = await _get_srs(db_pool, uid, wid, "active")
    assert after_a is not None
    assert after_a["interval_days"] == 1.0
    assert after_a["repetitions"] == 0
    assert after_a["ease_factor"] == before_ease


async def test_demote_known_to_learning_creates_missing_active_card(db_pool):
    """If the user reached 'known' via the manual confidence click only
    (status_marked_known has active_srs=None — no active card), demoting to
    learning must create the active card so production practice is scheduled."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Become 'known' via manual click only (no production events).
    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_known", status_override="known",
    )
    assert (await _get_uwk(db_pool, uid, wid))["status"] == "known"
    assert await _get_srs(db_pool, uid, wid, "active") is None  # precondition

    # Demote to learning.
    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )

    active = await _get_srs(db_pool, uid, wid, "active")
    assert active is not None
    assert active["repetitions"] == 0
    assert active["interval_days"] == 1.0


async def test_demote_known_to_learning_preserves_times_used_correctly(db_pool):
    """Counters record history — demotion must not rewrite them."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _drive_to_known(db_pool, uid, wid)
    before = await _get_uwk(db_pool, uid, wid)
    assert before["times_used_correctly"] >= ACTIVE_MASTERY_THRESHOLD
    before_count = before["times_used_correctly"]

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )

    row = await _get_uwk(db_pool, uid, wid)
    assert row["times_used_correctly"] == before_count


# --- known → unknown -------------------------------------------------------

async def test_demote_known_to_unknown_resets_both_levels(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _drive_to_known(db_pool, uid, wid)

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_unknown", status_override="unknown",
    )

    row = await _get_uwk(db_pool, uid, wid)
    assert row["status"] == "unknown"
    assert row["passive_level"] == 0
    assert row["active_level"] == 0


async def test_demote_known_to_unknown_resets_existing_cards(db_pool):
    """known → unknown uses 'incorrect' action: existing cards reset, ease drops 0.15."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _drive_to_known(db_pool, uid, wid)
    before_p = await _get_srs(db_pool, uid, wid, "passive")
    before_a = await _get_srs(db_pool, uid, wid, "active")
    assert before_p is not None
    assert before_a is not None

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_unknown", status_override="unknown",
    )

    after_p = await _get_srs(db_pool, uid, wid, "passive")
    after_a = await _get_srs(db_pool, uid, wid, "active")
    assert after_p["interval_days"] == 1.0
    assert after_p["repetitions"] == 0
    assert after_a["interval_days"] == 1.0
    assert after_a["repetitions"] == 0
    # 'incorrect' lowers ease by 0.15 (floored at 1.3).
    assert after_p["ease_factor"] < before_p["ease_factor"]
    assert after_a["ease_factor"] < before_a["ease_factor"]


async def test_demote_known_to_unknown_does_not_create_missing_active_card(db_pool):
    """A 'known via confidence click' item has no active card. Demoting to
    unknown must NOT fabricate one — the 'incorrect' action is a no-op when
    the card is missing."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_known", status_override="known",
    )
    assert await _get_srs(db_pool, uid, wid, "active") is None

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_unknown", status_override="unknown",
    )

    assert await _get_srs(db_pool, uid, wid, "active") is None


async def test_demote_known_to_unknown_preserves_times_counters(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _drive_to_known(db_pool, uid, wid)
    before = await _get_uwk(db_pool, uid, wid)
    before_seen = before["times_seen"]
    before_used = before["times_used_correctly"]

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_unknown", status_override="unknown",
    )

    row = await _get_uwk(db_pool, uid, wid)
    assert row["times_seen"] == before_seen
    assert row["times_used_correctly"] == before_used


# --- learning → unknown ----------------------------------------------------

async def test_demote_learning_to_unknown_resets_levels(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Build status=learning with non-trivial passive_level via passive reviews.
    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    await _apply(db_pool, uid, wid, "passive_review_correct")
    await _apply(db_pool, uid, wid, "passive_review_correct")
    before = await _get_uwk(db_pool, uid, wid)
    assert before["status"] == "learning"
    assert before["passive_level"] >= 2

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_unknown", status_override="unknown",
    )

    row = await _get_uwk(db_pool, uid, wid)
    assert row["status"] == "unknown"
    assert row["passive_level"] == 0
    assert row["active_level"] == 0


async def test_demote_learning_to_unknown_resets_existing_cards(db_pool):
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    for _ in range(2):
        await _apply(db_pool, uid, wid, "passive_review_correct")
    before_p = await _get_srs(db_pool, uid, wid, "passive")
    assert before_p["interval_days"] > 1.0

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_unknown", status_override="unknown",
    )

    after_p = await _get_srs(db_pool, uid, wid, "passive")
    assert after_p["interval_days"] == 1.0
    assert after_p["repetitions"] == 0


# --- regression: upgrade paths unaffected ----------------------------------

async def test_upgrade_unknown_to_learning_uses_additive_path(db_pool):
    """unknown → learning is NOT a demotion. The additive status_marked_learning
    rule applies: passive_delta=1, active_delta=0, both cards 'create'."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    # Ensure a row exists with status='unknown' (via a non-status event).
    await _apply(db_pool, uid, wid, "transcript_clicked")
    assert (await _get_uwk(db_pool, uid, wid))["status"] == "unknown"

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )

    row = await _get_uwk(db_pool, uid, wid)
    assert row["status"] == "learning"
    # 1 from transcript_clicked + 1 from status_marked_learning rule = 2.
    assert row["passive_level"] == 2
    assert row["active_level"] == 0
    assert await _get_srs(db_pool, uid, wid, "passive") is not None
    assert await _get_srs(db_pool, uid, wid, "active")  is not None


async def test_upgrade_learning_to_known_does_not_reset(db_pool):
    """learning → known is NOT a demotion. Levels are preserved (the rule has
    no level deltas) — only the status field flips via status_override."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    # Bump passive a bit so we can detect a reset (there shouldn't be one).
    await _apply(db_pool, uid, wid, "passive_review_correct")
    before = await _get_uwk(db_pool, uid, wid)
    assert before["passive_level"] >= 2

    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_known", status_override="known",
    )

    row = await _get_uwk(db_pool, uid, wid)
    assert row["status"] == "known"
    assert row["passive_level"] == before["passive_level"]


# --- production events still work ------------------------------------------

async def test_production_events_still_bump_active_level_post_demotion(db_pool):
    """After a known → learning demotion, the user should be able to climb
    back to known via real production events (guided_counted, etc.)."""
    uid = await _make_user(db_pool)
    wid = await _get_word_id(db_pool)
    await _drive_to_known(db_pool, uid, wid)

    # Demote.
    await apply_progression(
        db_pool, uid, wid, "word", "status_marked_learning", status_override="learning",
    )
    assert (await _get_uwk(db_pool, uid, wid))["active_level"] == 0

    # Climb back via guided_counted.
    for _ in range(ACTIVE_MASTERY_THRESHOLD):
        await _apply(db_pool, uid, wid, "guided_counted")

    row = await _get_uwk(db_pool, uid, wid)
    assert row["active_level"] == ACTIVE_MASTERY_THRESHOLD
    assert row["status"] == "known"


# --- grammar_rule guard ----------------------------------------------------

async def test_demote_grammar_rule_to_learning_does_not_create_active_card(db_pool):
    """Grammar rules are passive-only — _update_srs short-circuits active.
    Demotion 'reset' action must respect that guard."""
    rule_row = await db_pool.fetchrow("SELECT rule_id FROM grammar_rule_table LIMIT 1")
    if rule_row is None:
        pytest.skip("grammar_rule_table is empty")
    rid = rule_row["rule_id"]
    uid = await _make_user(db_pool)

    # Mark known then demote to learning.
    await apply_progression(
        db_pool, uid, rid, "grammar_rule", "status_marked_known", status_override="known",
    )
    await apply_progression(
        db_pool, uid, rid, "grammar_rule", "status_marked_learning", status_override="learning",
    )

    passive = await db_pool.fetchrow(
        "SELECT * FROM srs_cards WHERE user_id=$1::uuid AND item_id=$2 AND item_type='grammar_rule' AND direction='passive'",
        uid, rid,
    )
    active = await db_pool.fetchrow(
        "SELECT * FROM srs_cards WHERE user_id=$1::uuid AND item_id=$2 AND item_type='grammar_rule' AND direction='active'",
        uid, rid,
    )
    assert passive is not None
    assert active is None
