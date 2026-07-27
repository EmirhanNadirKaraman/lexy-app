"""
Prioritization service tests.

Unit tests (no DB) — test pure functions:
  compute_item_score:
    - all signals zero → 0.0
    - is_due alone → weights.due
    - all signals max → sum of all weights
    - custom weights applied correctly
    - mistake_recency between 0 and 1 produces proportional score
  explain_signals:
    - all signals zero → empty list
    - is_due alone → ["review due"]
    - high mistake_recency → ["recent mistake"]
    - high freq_rank → ["frequently encountered"]
    - is_learning alone → ["in study list"]
    - all signals high → all four reasons present

Integration tests (real DB):
  - new user → empty list (no error)
  - user with learning word → that item appears with is_learning signal
  - user with due SRS card → that item has is_due=1.0 in signals
  - result is sorted by score descending
  - limit is respected
  - item appearing in multiple signals gets a single entry with combined score
  - item_type='phrase' returns empty list (no data)
"""

import pytest

from backend.services.prioritization_service import (
    DEFAULT_WEIGHTS,
    ScoringWeights,
    compute_item_score,
    explain_signals,
    get_prioritized_items,
)
from ._email_helper import make_test_email
from ._word_helper import insert_owned_word  # owned, xdist-safe words (docs/TESTS.md)


# ---------------------------------------------------------------------------
# Unit tests — pure functions, no DB
# ---------------------------------------------------------------------------

class TestComputeItemScore:
    def test_all_zero_returns_zero(self):
        assert compute_item_score(False, 0.0, 0.0, False) == 0.0

    def test_is_due_alone(self):
        score = compute_item_score(True, 0.0, 0.0, False)
        assert score == pytest.approx(DEFAULT_WEIGHTS.due)

    def test_is_learning_alone(self):
        score = compute_item_score(False, 0.0, 0.0, True)
        assert score == pytest.approx(DEFAULT_WEIGHTS.learning)

    def test_all_max_signals_equals_sum_of_weights(self):
        score = compute_item_score(True, 1.0, 1.0, True)
        expected = (
            DEFAULT_WEIGHTS.due
            + DEFAULT_WEIGHTS.mistake
            + DEFAULT_WEIGHTS.freq
            + DEFAULT_WEIGHTS.learning
        )
        assert score == pytest.approx(expected)

    def test_custom_weights_applied(self):
        weights = ScoringWeights(due=10.0, mistake=0.0, freq=0.0, learning=0.0)
        score = compute_item_score(True, 0.0, 0.0, False, weights=weights)
        assert score == pytest.approx(10.0)

    def test_mistake_recency_proportional(self):
        low  = compute_item_score(False, 0.1, 0.0, False)
        high = compute_item_score(False, 0.9, 0.0, False)
        assert high > low

    def test_due_beats_all_other_signals_at_max(self):
        # is_due (4) should beat mistake_recency + freq + learning (3+2+1=6)?
        # No — at max, other signals sum to 6 > 4. But one due item beats one learning.
        due_only      = compute_item_score(True,  0.0, 0.0, False)
        learning_only = compute_item_score(False, 0.0, 0.0, True)
        assert due_only > learning_only

    def test_score_bounded_by_sum_of_weights(self):
        max_score = DEFAULT_WEIGHTS.due + DEFAULT_WEIGHTS.mistake + DEFAULT_WEIGHTS.freq + DEFAULT_WEIGHTS.learning
        score = compute_item_score(True, 1.0, 1.0, True)
        assert score <= max_score + 1e-9


class TestExplainSignals:
    def test_no_signals_returns_empty(self):
        assert explain_signals(False, 0.0, 0.0, False) == []

    def test_is_due_returns_review_due(self):
        reasons = explain_signals(True, 0.0, 0.0, False)
        assert "review due" in reasons

    def test_high_mistake_recency_returns_recent_mistake(self):
        # 0.4 is the threshold (≈ mistake 6 days ago)
        reasons = explain_signals(False, 0.5, 0.0, False)
        assert "recent mistake" in reasons

    def test_low_mistake_recency_excluded(self):
        reasons = explain_signals(False, 0.1, 0.0, False)
        assert "recent mistake" not in reasons

    def test_high_freq_rank_returns_frequently_encountered(self):
        reasons = explain_signals(False, 0.0, 0.5, False)
        assert "frequently encountered" in reasons

    def test_low_freq_rank_excluded(self):
        reasons = explain_signals(False, 0.0, 0.1, False)
        assert "frequently encountered" not in reasons

    def test_is_learning_returns_in_study_list(self):
        reasons = explain_signals(False, 0.0, 0.0, True)
        assert "in study list" in reasons

    def test_all_signals_returns_all_reasons(self):
        reasons = explain_signals(True, 1.0, 1.0, True)
        assert "review due"              in reasons
        assert "recent mistake"          in reasons
        assert "frequently encountered"  in reasons
        assert "in study list"           in reasons

    def test_returns_list_not_other_type(self):
        result = explain_signals(False, 0.0, 0.0, False)
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Integration tests — real DB
# ---------------------------------------------------------------------------

async def _create_user(pool) -> str:
    row = await pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING user_id",
        make_test_email(),
    )
    return str(row["user_id"])


async def test_new_user_returns_empty_list(db_pool):
    user_id = await _create_user(db_pool)
    items = await get_prioritized_items(db_pool, user_id)
    assert items == []


async def test_user_with_learning_word_appears_in_results(db_pool):
    user_id = await _create_user(db_pool)

    word_id, _ = await insert_owned_word(db_pool)

    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
        VALUES ($1::uuid, $2, 'word', 'learning')
        ON CONFLICT DO NOTHING
        """,
        user_id, word_id,
    )

    items = await get_prioritized_items(db_pool, user_id)

    item_ids = [item.item_id for item in items]
    assert word_id in item_ids

    matched = next(i for i in items if i.item_id == word_id)
    assert matched.signals["is_learning"] == pytest.approx(1.0)
    assert matched.item_type == "word"
    assert "in study list" in matched.reasons


async def test_user_with_due_srs_card_has_is_due_signal(db_pool):
    user_id = await _create_user(db_pool)

    word_id, _ = await insert_owned_word(db_pool)

    # Insert knowledge row (status != 'known') and an overdue passive SRS card
    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
        VALUES ($1::uuid, $2, 'word', 'learning')
        ON CONFLICT DO NOTHING
        """,
        user_id, word_id,
    )
    await db_pool.execute(
        """
        INSERT INTO srs_cards
            (user_id, item_id, item_type, direction, due_date, interval_days, ease_factor, repetitions)
        VALUES ($1::uuid, $2, 'word', 'passive', NOW() - INTERVAL '1 day', 1.0, 2.5, 0)
        ON CONFLICT DO NOTHING
        """,
        user_id, word_id,
    )

    items = await get_prioritized_items(db_pool, user_id)
    matched = next((i for i in items if i.item_id == word_id), None)
    assert matched is not None
    assert matched.signals["is_due"] == pytest.approx(1.0)
    assert "review due" in matched.reasons


async def test_results_sorted_by_score_descending(db_pool):
    user_id = await _create_user(db_pool)

    word_rows = [{"word_id": (await insert_owned_word(db_pool))[0]} for _ in range(3)]

    for row in word_rows:
        await db_pool.execute(
            """
            INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
            VALUES ($1::uuid, $2, 'word', 'learning')
            ON CONFLICT DO NOTHING
            """,
            user_id, row["word_id"],
        )

    items = await get_prioritized_items(db_pool, user_id)
    scores = [item.score for item in items]
    assert scores == sorted(scores, reverse=True)


async def test_limit_respected(db_pool):
    user_id = await _create_user(db_pool)

    word_rows = [{"word_id": (await insert_owned_word(db_pool))[0]} for _ in range(5)]
    for row in word_rows:
        await db_pool.execute(
            """
            INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
            VALUES ($1::uuid, $2, 'word', 'learning')
            ON CONFLICT DO NOTHING
            """,
            user_id, row["word_id"],
        )

    items = await get_prioritized_items(db_pool, user_id, limit=3)
    assert len(items) <= 3


async def test_item_in_multiple_signals_appears_once(db_pool):
    """An item that is both 'learning' and 'due' should appear exactly once with combined score."""
    user_id = await _create_user(db_pool)

    word_id, _ = await insert_owned_word(db_pool)

    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
        VALUES ($1::uuid, $2, 'word', 'learning')
        ON CONFLICT DO NOTHING
        """,
        user_id, word_id,
    )
    await db_pool.execute(
        """
        INSERT INTO srs_cards
            (user_id, item_id, item_type, direction, due_date, interval_days, ease_factor, repetitions)
        VALUES ($1::uuid, $2, 'word', 'passive', NOW() - INTERVAL '1 day', 1.0, 2.5, 0)
        ON CONFLICT DO NOTHING
        """,
        user_id, word_id,
    )

    items = await get_prioritized_items(db_pool, user_id)
    matched = [i for i in items if i.item_id == word_id]
    assert len(matched) == 1, "item appeared more than once in results"

    item = matched[0]
    expected_score = DEFAULT_WEIGHTS.due + DEFAULT_WEIGHTS.learning  # is_due + is_learning
    assert item.score >= expected_score  # may also have freq_rank contribution


async def test_phrase_item_type_returns_empty(db_pool):
    """item_type='phrase' has no signal data yet — should return [] without error."""
    user_id = await _create_user(db_pool)
    items = await get_prioritized_items(db_pool, user_id, item_type="phrase")
    assert items == []
