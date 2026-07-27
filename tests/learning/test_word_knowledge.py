"""
test_word_knowledge.py
-----------------------
pytest suite for the time-aware word knowledge evidence system.

Tests are organised around the five rules that define correctness:

  1. Content deduplication  — same content_id counts once per word
  2. Time spacing           — events within passive_min_gap / active_min_gap
                              are rejected
  3. Spaced evidence        — spread-out events accumulate correctly
  4. Explicit signals       — mark_known and mark_unknown override evidence
  5. State transitions      — thresholds are hit at exactly the right count

All tests use an EvidenceConfig with tightly controlled thresholds so that
assertions are deterministic and don't depend on wall-clock time.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.learning.word_knowledge import (
    ActiveEvidenceEvent,
    EvidenceConfig,
    KnowledgeStore,
    LearningState,
    PassiveEvidenceEvent,
    PassiveSource,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def ts(day: int, hour: int = 12) -> datetime:
    """Create a UTC datetime at the given day-of-month and hour in March 2024."""
    return datetime(2024, 3, day, hour, 0, tzinfo=timezone.utc)


def tight_config() -> EvidenceConfig:
    """
    Short gaps and low thresholds for test speed without compromising rule
    correctness.  Tests that check gap enforcement use explicit timedelta
    arithmetic rather than relying on real elapsed time.
    """
    return EvidenceConfig(
        passive_min_gap=timedelta(hours=12),
        active_min_gap=timedelta(hours=24),
        familiar_threshold=2,
        passive_threshold=5,
        active_threshold=3,
    )


@pytest.fixture
def store() -> KnowledgeStore:
    return KnowledgeStore(config=tight_config())


USER = "u1"
WORD = "anfangen"


# ---------------------------------------------------------------------------
# 1. Content deduplication
# ---------------------------------------------------------------------------

class TestContentDeduplication:
    def test_first_exposure_to_content_is_accepted(self, store):
        accepted = store.record_passive_evidence(USER, WORD, ts(1), content_id="ep01")
        assert accepted is True

    def test_second_exposure_same_content_is_rejected(self, store):
        store.record_passive_evidence(USER, WORD, ts(1), content_id="ep01")
        accepted = store.record_passive_evidence(USER, WORD, ts(3), content_id="ep01")
        assert accepted is False

    def test_second_exposure_same_content_does_not_increment_count(self, store):
        store.record_passive_evidence(USER, WORD, ts(1), content_id="ep01")
        store.record_passive_evidence(USER, WORD, ts(3), content_id="ep01")
        k = store.get_knowledge(USER, WORD)
        assert k.passive_evidence_count == 1

    def test_different_content_ids_both_count(self, store):
        store.record_passive_evidence(USER, WORD, ts(1), content_id="ep01")
        accepted = store.record_passive_evidence(USER, WORD, ts(2), content_id="ep02")
        assert accepted is True

    def test_different_content_ids_each_increment_count(self, store):
        store.record_passive_evidence(USER, WORD, ts(1), content_id="ep01")
        store.record_passive_evidence(USER, WORD, ts(2), content_id="ep02")
        k = store.get_knowledge(USER, WORD)
        assert k.passive_evidence_count == 2

    def test_no_content_id_uses_only_time_gap_rule(self, store):
        # Without a content_id, the same source can count again after the gap
        store.record_passive_evidence(USER, WORD, ts(1))
        accepted = store.record_passive_evidence(USER, WORD, ts(2))
        assert accepted is True

    def test_content_id_rejection_does_not_update_timestamp(self, store):
        # A rejected event must not move last_passive_event_at forward,
        # or it would incorrectly delay the next valid event.
        store.record_passive_evidence(USER, WORD, ts(1), content_id="ep01")
        k_before = store.get_knowledge(USER, WORD)
        last_before = k_before.last_passive_event_at

        store.record_passive_evidence(USER, WORD, ts(3), content_id="ep01")  # rejected
        k_after = store.get_knowledge(USER, WORD)
        assert k_after.last_passive_event_at == last_before

    def test_seen_content_ids_persists_across_resubmissions(self, store):
        store.record_passive_evidence(USER, WORD, ts(1), content_id="ep01")
        # Submit same content_id four more times at different timestamps
        for day in range(2, 6):
            store.record_passive_evidence(USER, WORD, ts(day), content_id="ep01")
        k = store.get_knowledge(USER, WORD)
        assert k.passive_evidence_count == 1


# ---------------------------------------------------------------------------
# 2. Time spacing — passive
# ---------------------------------------------------------------------------

class TestPassiveTimeSpacing:
    def test_event_within_gap_is_rejected(self, store):
        store.record_passive_evidence(USER, WORD, ts(1, 10))
        # 6 hours later — within 12 h gap
        t_soon = ts(1, 10) + timedelta(hours=6)
        accepted = store.record_passive_evidence(USER, WORD, t_soon, content_id="other")
        assert accepted is False

    def test_event_exactly_at_gap_is_accepted(self, store):
        store.record_passive_evidence(USER, WORD, ts(1, 0))
        t_exact = ts(1, 0) + timedelta(hours=12)
        accepted = store.record_passive_evidence(USER, WORD, t_exact, content_id="other")
        assert accepted is True

    def test_event_beyond_gap_is_accepted(self, store):
        store.record_passive_evidence(USER, WORD, ts(1))
        t_later = ts(1) + timedelta(hours=13)
        accepted = store.record_passive_evidence(USER, WORD, t_later, content_id="other")
        assert accepted is True

    def test_too_soon_event_does_not_update_timestamp(self, store):
        store.record_passive_evidence(USER, WORD, ts(1, 0))
        original_ts = store.get_knowledge(USER, WORD).last_passive_event_at

        t_soon = ts(1, 0) + timedelta(hours=1)
        store.record_passive_evidence(USER, WORD, t_soon, content_id="other")
        k = store.get_knowledge(USER, WORD)
        assert k.last_passive_event_at == original_ts

    def test_too_soon_event_does_not_increment_count(self, store):
        store.record_passive_evidence(USER, WORD, ts(1, 0))
        t_soon = ts(1, 0) + timedelta(hours=6)
        store.record_passive_evidence(USER, WORD, t_soon, content_id="ep99")
        k = store.get_knowledge(USER, WORD)
        assert k.passive_evidence_count == 1

    def test_content_dedup_is_checked_before_time_gap(self, store):
        # An already-seen content_id is rejected even if time gap is satisfied.
        store.record_passive_evidence(USER, WORD, ts(1), content_id="ep01")
        # 24 h later — time gap is fine, but content_id is already seen
        accepted = store.record_passive_evidence(USER, WORD, ts(2), content_id="ep01")
        assert accepted is False


# ---------------------------------------------------------------------------
# 3. Spaced exposures accumulate correctly
# ---------------------------------------------------------------------------

class TestSpacedPassiveAccumulation:
    def _seed_n_passive_events(self, store: KnowledgeStore, n: int) -> None:
        """Record n daily exposures with distinct content_ids."""
        for i in range(n):
            store.record_passive_evidence(
                USER, WORD,
                timestamp=ts(i + 1),
                source=PassiveSource.VIDEO,
                content_id=f"ep{i:02d}",
            )

    def test_count_matches_accepted_events(self, store):
        self._seed_n_passive_events(store, 4)
        k = store.get_knowledge(USER, WORD)
        assert k.passive_evidence_count == 4

    def test_passive_events_list_has_correct_length(self, store):
        self._seed_n_passive_events(store, 3)
        k = store.get_knowledge(USER, WORD)
        assert len(k.passive_events) == 3

    def test_passive_events_are_passiveevidenceevent_instances(self, store):
        self._seed_n_passive_events(store, 2)
        k = store.get_knowledge(USER, WORD)
        for evt in k.passive_events:
            assert isinstance(evt, PassiveEvidenceEvent)

    def test_source_is_stored_on_event(self, store):
        store.record_passive_evidence(USER, WORD, ts(1), source=PassiveSource.READING, content_id="a1")
        k = store.get_knowledge(USER, WORD)
        assert k.passive_events[0].source == PassiveSource.READING

    def test_last_passive_event_at_tracks_most_recent_accepted(self, store):
        store.record_passive_evidence(USER, WORD, ts(1), content_id="ep01")
        store.record_passive_evidence(USER, WORD, ts(2), content_id="ep02")
        k = store.get_knowledge(USER, WORD)
        assert k.last_passive_event_at == ts(2)


# ---------------------------------------------------------------------------
# 4. Time spacing — active
# ---------------------------------------------------------------------------

class TestActiveTimeSpacing:
    @pytest.fixture(autouse=True)
    def bring_to_passive(self, store):
        """Pre-condition: word must be PASSIVE for active events to count."""
        for i in range(5):
            store.record_passive_evidence(
                USER, WORD, ts(i + 1), content_id=f"c{i}"
            )
        assert store.get_state(USER, WORD) == LearningState.PASSIVE

    def test_first_active_success_is_accepted(self, store):
        accepted = store.record_active_success(USER, WORD, ts(10))
        assert accepted is True

    def test_second_success_within_gap_is_rejected(self, store):
        store.record_active_success(USER, WORD, ts(10, 10))
        t_soon = ts(10, 10) + timedelta(hours=6)  # within 24 h gap
        accepted = store.record_active_success(USER, WORD, t_soon)
        assert accepted is False

    def test_second_success_beyond_gap_is_accepted(self, store):
        store.record_active_success(USER, WORD, ts(10))
        t_next_day = ts(10) + timedelta(hours=25)
        accepted = store.record_active_success(USER, WORD, t_next_day)
        assert accepted is True

    def test_incorrect_attempt_is_rejected(self, store):
        accepted = store.record_active_success(USER, WORD, ts(10), correct=False)
        assert accepted is False

    def test_incorrect_attempt_does_not_increment_count(self, store):
        store.record_active_success(USER, WORD, ts(10), correct=False)
        k = store.get_knowledge(USER, WORD)
        assert k.active_success_count == 0

    def test_multiple_uses_same_session_count_as_one(self, store):
        # Simulate three correct uses in a one-hour chat session
        store.record_active_success(USER, WORD, ts(10, 10))
        store.record_active_success(USER, WORD, ts(10, 10) + timedelta(minutes=20))
        store.record_active_success(USER, WORD, ts(10, 10) + timedelta(minutes=40))
        k = store.get_knowledge(USER, WORD)
        assert k.active_success_count == 1

    def test_active_before_passive_is_rejected(self, store):
        # A word that never reached PASSIVE should not benefit from active recall
        fresh_store = KnowledgeStore(config=tight_config())
        accepted = fresh_store.record_active_success(USER, "neues_wort", ts(10))
        assert accepted is False

    def test_active_count_is_stored_in_events_list(self, store):
        store.record_active_success(USER, WORD, ts(10))
        k = store.get_knowledge(USER, WORD)
        assert len(k.active_events) == 1
        assert isinstance(k.active_events[0], ActiveEvidenceEvent)
        assert k.active_events[0].correct is True


# ---------------------------------------------------------------------------
# 5. Explicit signals: mark_known and mark_unknown
# ---------------------------------------------------------------------------

class TestMarkKnown:
    def test_mark_known_from_unknown_sets_passive(self, store):
        store.mark_known(USER, WORD, ts(1))
        assert store.get_state(USER, WORD) == LearningState.PASSIVE

    def test_mark_known_from_familiar_sets_passive(self, store):
        # Manually place at FAMILIAR
        store.record_passive_evidence(USER, WORD, ts(1), content_id="c1")
        store.record_passive_evidence(USER, WORD, ts(2), content_id="c2")
        assert store.get_state(USER, WORD) == LearningState.FAMILIAR
        store.mark_known(USER, WORD, ts(3))
        assert store.get_state(USER, WORD) == LearningState.PASSIVE

    def test_mark_known_does_not_demote_from_active(self, store):
        # Bring to ACTIVE first
        for i in range(5):
            store.record_passive_evidence(USER, WORD, ts(i + 1), content_id=f"c{i}")
        for i in range(3):
            store.record_active_success(USER, WORD, ts(10 + i * 2))
        assert store.get_state(USER, WORD) == LearningState.ACTIVE

        store.mark_known(USER, WORD, ts(20))
        assert store.get_state(USER, WORD) == LearningState.ACTIVE

    def test_mark_known_sets_count_to_passive_threshold(self, store):
        store.mark_known(USER, WORD, ts(1))
        k = store.get_knowledge(USER, WORD)
        assert k.passive_evidence_count >= store.config.passive_threshold

    def test_mark_known_updates_last_passive_event_at(self, store):
        store.mark_known(USER, WORD, ts(7))
        k = store.get_knowledge(USER, WORD)
        assert k.last_passive_event_at == ts(7)

    def test_mark_known_bypasses_content_dedup(self, store):
        # Calling mark_known twice should not raise; state stays PASSIVE or higher
        store.mark_known(USER, WORD, ts(1))
        store.mark_known(USER, WORD, ts(2))
        assert store.get_state(USER, WORD) == LearningState.PASSIVE

    def test_mark_known_allows_active_recall_immediately(self, store):
        # After mark_known, active success must be accepted (state >= PASSIVE)
        store.mark_known(USER, WORD, ts(1))
        accepted = store.record_active_success(USER, WORD, ts(5))
        assert accepted is True


class TestMarkUnknown:
    def test_mark_unknown_resets_to_unknown(self, store):
        for i in range(5):
            store.record_passive_evidence(USER, WORD, ts(i + 1), content_id=f"c{i}")
        store.mark_unknown(USER, WORD, ts(10))
        assert store.get_state(USER, WORD) == LearningState.UNKNOWN

    def test_mark_unknown_clears_passive_count(self, store):
        store.record_passive_evidence(USER, WORD, ts(1), content_id="c1")
        store.mark_unknown(USER, WORD, ts(5))
        k = store.get_knowledge(USER, WORD)
        assert k.passive_evidence_count == 0

    def test_mark_unknown_clears_active_count(self, store):
        for i in range(5):
            store.record_passive_evidence(USER, WORD, ts(i + 1), content_id=f"c{i}")
        store.record_active_success(USER, WORD, ts(10))
        store.mark_unknown(USER, WORD, ts(20))
        k = store.get_knowledge(USER, WORD)
        assert k.active_success_count == 0

    def test_mark_unknown_clears_seen_content_ids(self, store):
        store.record_passive_evidence(USER, WORD, ts(1), content_id="ep01")
        store.mark_unknown(USER, WORD, ts(5))
        k = store.get_knowledge(USER, WORD)
        assert len(k.seen_content_ids) == 0

    def test_mark_unknown_clears_event_history(self, store):
        store.record_passive_evidence(USER, WORD, ts(1), content_id="ep01")
        store.mark_unknown(USER, WORD, ts(5))
        k = store.get_knowledge(USER, WORD)
        assert k.passive_events == []
        assert k.active_events == []

    def test_evidence_after_mark_unknown_starts_fresh(self, store):
        # Accumulate up to FAMILIAR, reset, then one new event should not reach FAMILIAR
        store.record_passive_evidence(USER, WORD, ts(1), content_id="c1")
        store.record_passive_evidence(USER, WORD, ts(2), content_id="c2")
        assert store.get_state(USER, WORD) == LearningState.FAMILIAR

        store.mark_unknown(USER, WORD, ts(3))
        # One new event after reset
        store.record_passive_evidence(USER, WORD, ts(4), content_id="c3")
        assert store.get_state(USER, WORD) == LearningState.UNKNOWN

    def test_mark_unknown_from_active_resets_to_unknown(self, store):
        for i in range(5):
            store.record_passive_evidence(USER, WORD, ts(i + 1), content_id=f"c{i}")
        for i in range(3):
            store.record_active_success(USER, WORD, ts(10 + i * 2))
        assert store.get_state(USER, WORD) == LearningState.ACTIVE

        store.mark_unknown(USER, WORD, ts(30))
        assert store.get_state(USER, WORD) == LearningState.UNKNOWN


# ---------------------------------------------------------------------------
# 6. State transitions
# ---------------------------------------------------------------------------

class TestStateTransitions:
    def _passive_events(self, store: KnowledgeStore, n: int, start_day: int = 1) -> None:
        for i in range(n):
            store.record_passive_evidence(
                USER, WORD, ts(start_day + i), content_id=f"c{start_day + i}"
            )

    def _active_events(self, store: KnowledgeStore, n: int, start_day: int = 20) -> None:
        for i in range(n):
            store.record_active_success(USER, WORD, ts(start_day + i * 2))

    def test_zero_events_stays_unknown(self, store):
        assert store.get_state(USER, WORD) == LearningState.UNKNOWN

    def test_one_passive_event_stays_unknown(self, store):
        self._passive_events(store, 1)
        assert store.get_state(USER, WORD) == LearningState.UNKNOWN

    def test_familiar_threshold_reached(self, store):
        self._passive_events(store, store.config.familiar_threshold)
        assert store.get_state(USER, WORD) == LearningState.FAMILIAR

    def test_just_below_familiar_threshold_stays_unknown(self, store):
        self._passive_events(store, store.config.familiar_threshold - 1)
        assert store.get_state(USER, WORD) == LearningState.UNKNOWN

    def test_passive_threshold_reached(self, store):
        self._passive_events(store, store.config.passive_threshold)
        assert store.get_state(USER, WORD) == LearningState.PASSIVE

    def test_just_below_passive_threshold_stays_familiar(self, store):
        self._passive_events(store, store.config.passive_threshold - 1)
        assert store.get_state(USER, WORD) == LearningState.FAMILIAR

    def test_active_threshold_reached_from_passive(self, store):
        self._passive_events(store, store.config.passive_threshold)
        self._active_events(store, store.config.active_threshold)
        assert store.get_state(USER, WORD) == LearningState.ACTIVE

    def test_just_below_active_threshold_stays_passive(self, store):
        self._passive_events(store, store.config.passive_threshold)
        self._active_events(store, store.config.active_threshold - 1)
        assert store.get_state(USER, WORD) == LearningState.PASSIVE

    def test_active_count_does_not_advance_familiar(self, store):
        # A word at FAMILIAR must not jump to ACTIVE via active events
        self._passive_events(store, store.config.familiar_threshold)
        assert store.get_state(USER, WORD) == LearningState.FAMILIAR
        # Active recall should be rejected (state < PASSIVE), not advance state
        for i in range(store.config.active_threshold + 2):
            store.record_active_success(USER, WORD, ts(20 + i * 2))
        assert store.get_state(USER, WORD) == LearningState.FAMILIAR

    def test_transitions_are_strictly_monotone_via_passive(self, store):
        # Accumulate passive events one-by-one and verify the sequence
        states = []
        for i in range(store.config.passive_threshold):
            store.record_passive_evidence(USER, WORD, ts(i + 1), content_id=f"c{i}")
            states.append(store.get_state(USER, WORD))

        # State must never go backwards
        for a, b in zip(states, states[1:]):
            assert b >= a

    def test_state_does_not_advance_beyond_active(self, store):
        self._passive_events(store, store.config.passive_threshold)
        # Record many more active events than needed
        for i in range(store.config.active_threshold + 5):
            store.record_active_success(USER, WORD, ts(7 + i * 2))
        assert store.get_state(USER, WORD) == LearningState.ACTIVE


# ---------------------------------------------------------------------------
# 7. Full example flow (integration)
# ---------------------------------------------------------------------------

class TestFullFlow:
    def test_complete_journey_unknown_to_active(self):
        """
        Simulate a realistic learning journey:
        Day 1-5: five video exposures (different episodes) → PASSIVE
        Day 10, 12, 14: chat sessions → ACTIVE
        """
        store = KnowledgeStore(config=tight_config())

        for i in range(5):
            store.record_passive_evidence(
                USER, WORD, ts(i + 1), source=PassiveSource.VIDEO, content_id=f"ep{i:02d}"
            )

        assert store.get_state(USER, WORD) == LearningState.PASSIVE

        store.record_active_success(USER, WORD, ts(10))
        store.record_active_success(USER, WORD, ts(12))
        store.record_active_success(USER, WORD, ts(14))

        assert store.get_state(USER, WORD) == LearningState.ACTIVE

    def test_binge_watching_counts_as_one_event(self):
        """
        Watching 5 episodes in one afternoon must count as a single event,
        not 5 — both because of content-id dedup and time-gap enforcement.
        """
        store = KnowledgeStore(config=tight_config())
        base = ts(1, 18)
        for episode in range(5):
            store.record_passive_evidence(
                USER, WORD,
                timestamp=base + timedelta(minutes=50 * episode),
                source=PassiveSource.VIDEO,
                content_id=f"ep{episode:02d}",
            )

        k = store.get_knowledge(USER, WORD)
        # Only the first episode's event counts (time gap blocks the rest)
        assert k.passive_evidence_count == 1
        assert store.get_state(USER, WORD) == LearningState.UNKNOWN

    def test_mark_known_shortcut_enables_active_recall_immediately(self):
        store = KnowledgeStore(config=tight_config())
        store.mark_known(USER, WORD, ts(1))
        assert store.get_state(USER, WORD) == LearningState.PASSIVE

        # Active recall should immediately work
        accepted = store.record_active_success(USER, WORD, ts(3))
        assert accepted is True

    def test_get_state_for_unseen_word_returns_unknown(self):
        store = KnowledgeStore(config=tight_config())
        assert store.get_state(USER, "unseen_word") == LearningState.UNKNOWN

    def test_get_knowledge_for_unseen_word_returns_none(self):
        store = KnowledgeStore(config=tight_config())
        assert store.get_knowledge(USER, "unseen_word") is None

    def test_different_users_have_independent_state(self):
        store = KnowledgeStore(config=tight_config())

        # User A reaches FAMILIAR
        for i in range(2):
            store.record_passive_evidence("user_a", WORD, ts(i + 1), content_id=f"c{i}")

        # User B never sees the word
        assert store.get_state("user_a", WORD) == LearningState.FAMILIAR
        assert store.get_state("user_b", WORD) == LearningState.UNKNOWN

    def test_reset_user_removes_all_records(self):
        store = KnowledgeStore(config=tight_config())
        store.record_passive_evidence(USER, WORD, ts(1), content_id="c1")
        store.reset_user(USER)
        assert store.get_state(USER, WORD) == LearningState.UNKNOWN
        assert store.get_knowledge(USER, WORD) is None
