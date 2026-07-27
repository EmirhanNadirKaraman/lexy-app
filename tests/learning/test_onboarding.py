"""
test_onboarding.py
------------------
Tests for onboarding.py: vocabulary seeding, user-marked known items,
duplicate handling, non-demotion safety, and UserKnowledgeStore integration.

Coverage
--------
  TestGetTierLemmas        — cumulative tier contents are correct and stable
  TestSeedFromLevel        — level-based seeding writes correct state
  TestSeedFromLemmas       — custom-list seeding and filtering
  TestMarkKnown            — user-flagged words stored correctly
  TestNonDemotion          — units already at or above seed state are untouched
  TestOnboardingResult     — result fields are accurate and deterministic
  TestKnowledgeStoreInteg  — seeded state feeds the i+1 filter correctly
"""
import pytest

from app.learning.units import LearningUnit, LearningUnitType, _SKIP_LEMMAS
from app.learning.onboarding import (
    LevelTier,
    VocabularyOnboarding,
    _TIER_ORDER,
)
from app.learning.knowledge import (
    ExposurePolicy,
    KnowledgeFilterPolicy,
    KnowledgeState,
    UserKnowledgeStore,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _store() -> UserKnowledgeStore:
    """Return a fresh in-memory store with default policies."""
    return UserKnowledgeStore(
        filter_policy=KnowledgeFilterPolicy(min_known_state=KnowledgeState.KNOWN_PASSIVE),
        exposure_policy=ExposurePolicy(auto_advance=True, exposures_to_unlock=5),
    )


def _lemma(key: str) -> LearningUnit:
    return LearningUnit(LearningUnitType.LEMMA, key, key)


@pytest.fixture
def store() -> UserKnowledgeStore:
    return _store()


@pytest.fixture
def onboarding() -> VocabularyOnboarding:
    return VocabularyOnboarding()


# ---------------------------------------------------------------------------
# TestGetTierLemmas
# ---------------------------------------------------------------------------

class TestGetTierLemmas:

    def test_complete_beginner_returns_empty_set(self):
        assert VocabularyOnboarding.get_tier_lemmas(LevelTier.COMPLETE_BEGINNER) == frozenset()

    def test_a1_returns_nonempty_set(self):
        assert len(VocabularyOnboarding.get_tier_lemmas(LevelTier.A1)) > 0

    def test_a2_is_superset_of_a1(self):
        a1 = VocabularyOnboarding.get_tier_lemmas(LevelTier.A1)
        a2 = VocabularyOnboarding.get_tier_lemmas(LevelTier.A2)
        assert a1 < a2  # strict subset

    def test_b1_is_superset_of_a2(self):
        a2 = VocabularyOnboarding.get_tier_lemmas(LevelTier.A2)
        b1 = VocabularyOnboarding.get_tier_lemmas(LevelTier.B1)
        assert a2 < b1  # strict subset

    def test_tiers_are_strictly_growing(self):
        sizes = [
            len(VocabularyOnboarding.get_tier_lemmas(t))
            for t in _TIER_ORDER
        ]
        # Each tier must be strictly larger than the previous
        for prev, curr in zip(sizes, sizes[1:]):
            assert curr > prev

    def test_result_is_deterministic(self):
        """Same call always returns the same frozenset."""
        first  = VocabularyOnboarding.get_tier_lemmas(LevelTier.A2)
        second = VocabularyOnboarding.get_tier_lemmas(LevelTier.A2)
        assert first == second

    def test_all_lemmas_are_lowercase(self):
        for tier in _TIER_ORDER:
            for lemma in VocabularyOnboarding.get_tier_lemmas(tier):
                assert lemma == lemma.lower(), f"{lemma!r} is not lowercase"

    def test_no_skip_lemmas_in_any_tier(self):
        """_SKIP_LEMMAS words can never be extraction targets; they must not appear."""
        for tier in _TIER_ORDER:
            overlap = VocabularyOnboarding.get_tier_lemmas(tier) & _SKIP_LEMMAS
            assert overlap == frozenset(), f"Tier {tier} contains {overlap}"

    def test_known_a1_words_present(self):
        a1 = VocabularyOnboarding.get_tier_lemmas(LevelTier.A1)
        for word in ["sein", "haben", "gehen", "nicht", "und", "mit"]:
            assert word in a1

    def test_tier_size_helper_matches_set_length(self):
        for tier in _TIER_ORDER:
            assert VocabularyOnboarding.tier_size(tier) == len(
                VocabularyOnboarding.get_tier_lemmas(tier)
            )


# ---------------------------------------------------------------------------
# TestSeedFromLevel
# ---------------------------------------------------------------------------

class TestSeedFromLevel:

    def test_a1_seed_stores_units_as_known_passive(self, onboarding, store):
        onboarding.seed_from_level("u1", LevelTier.A1, store)
        for lemma in VocabularyOnboarding.get_tier_lemmas(LevelTier.A1):
            state = store.get_state("u1", _lemma(lemma))
            assert state == KnowledgeState.KNOWN_PASSIVE, f"{lemma!r} has state {state.name}"

    def test_a2_seed_includes_all_a1_words(self, onboarding, store):
        onboarding.seed_from_level("u1", LevelTier.A2, store)
        for lemma in VocabularyOnboarding.get_tier_lemmas(LevelTier.A1):
            assert store.get_state("u1", _lemma(lemma)) == KnowledgeState.KNOWN_PASSIVE

    def test_seeded_count_matches_tier_size(self, onboarding, store):
        result = onboarding.seed_from_level("u1", LevelTier.A1, store)
        assert result.seeded_count == VocabularyOnboarding.tier_size(LevelTier.A1)

    def test_complete_beginner_seeds_nothing(self, onboarding, store):
        result = onboarding.seed_from_level("u1", LevelTier.COMPLETE_BEGINNER, store)
        assert result.seeded_count == 0
        assert result.skipped_count == 0
        assert store.get_summary("u1") == {s: 0 for s in KnowledgeState}

    def test_result_tier_field_set_correctly(self, onboarding, store):
        result = onboarding.seed_from_level("u1", LevelTier.A2, store)
        assert result.tier == LevelTier.A2

    def test_result_state_field_reflects_custom_state(self, onboarding, store):
        result = onboarding.seed_from_level(
            "u1", LevelTier.A1, store, state=KnowledgeState.KNOWN_ACTIVE
        )
        assert result.state == KnowledgeState.KNOWN_ACTIVE
        # Spot-check one unit
        assert store.get_state("u1", _lemma("sein")) == KnowledgeState.KNOWN_ACTIVE

    def test_b1_seed_is_larger_than_a2_seed(self, onboarding):
        store_a2 = _store()
        store_b1 = _store()
        r_a2 = onboarding.seed_from_level("u", LevelTier.A2, store_a2)
        r_b1 = onboarding.seed_from_level("u", LevelTier.B1, store_b1)
        assert r_b1.seeded_count > r_a2.seeded_count

    def test_different_users_get_independent_stores(self, onboarding, store):
        onboarding.seed_from_level("alice", LevelTier.A1, store)
        onboarding.seed_from_level("bob",   LevelTier.COMPLETE_BEGINNER, store)
        # alice has A1 seed; bob has nothing
        assert store.get_state("alice", _lemma("gehen")) == KnowledgeState.KNOWN_PASSIVE
        assert store.get_state("bob",   _lemma("gehen")) == KnowledgeState.UNSEEN


# ---------------------------------------------------------------------------
# TestSeedFromLemmas
# ---------------------------------------------------------------------------

class TestSeedFromLemmas:

    def test_empty_iterable_seeds_nothing(self, onboarding, store):
        result = onboarding.seed_from_lemmas("u1", [], store)
        assert result.seeded_count == 0
        assert result.skipped_count == 0

    def test_custom_list_stored_correctly(self, onboarding, store):
        words = ["urlaub", "buchung", "reise"]
        onboarding.seed_from_lemmas("u1", words, store)
        for w in words:
            assert store.get_state("u1", _lemma(w)) == KnowledgeState.KNOWN_PASSIVE

    def test_skip_lemmas_filtered_silently(self, onboarding, store):
        # Words in _SKIP_LEMMAS must not appear in the store.
        skip_words = list(_SKIP_LEMMAS)[:5]
        result = onboarding.seed_from_lemmas("u1", skip_words, store)
        assert result.seeded_count == 0
        for w in skip_words:
            assert store.get_state("u1", _lemma(w)) == KnowledgeState.UNSEEN

    def test_mixed_valid_and_skip_lemmas(self, onboarding, store):
        words = ["urlaub"] + list(_SKIP_LEMMAS)[:3] + ["reise"]
        result = onboarding.seed_from_lemmas("u1", words, store)
        assert result.seeded_count == 2  # only "urlaub" and "reise"

    def test_keys_are_lowercased_before_lookup(self, onboarding, store):
        # Input with mixed case must normalise to the same key.
        onboarding.seed_from_lemmas("u1", ["GEHEN", "Haus"], store)
        assert store.get_state("u1", _lemma("gehen")) == KnowledgeState.KNOWN_PASSIVE
        assert store.get_state("u1", _lemma("haus"))  == KnowledgeState.KNOWN_PASSIVE

    def test_tier_field_is_none_for_custom_seed(self, onboarding, store):
        result = onboarding.seed_from_lemmas("u1", ["urlaub"], store)
        assert result.tier is None

    def test_custom_state_applied(self, onboarding, store):
        onboarding.seed_from_lemmas("u1", ["urlaub"], store, state=KnowledgeState.UNLOCKED)
        assert store.get_state("u1", _lemma("urlaub")) == KnowledgeState.UNLOCKED


# ---------------------------------------------------------------------------
# TestMarkKnown
# ---------------------------------------------------------------------------

class TestMarkKnown:

    def test_marked_words_stored_as_known_passive(self, onboarding, store):
        onboarding.mark_known("u1", ["urlaub", "strand", "sommer"], store)
        for w in ["urlaub", "strand", "sommer"]:
            assert store.get_state("u1", _lemma(w)) == KnowledgeState.KNOWN_PASSIVE

    def test_result_seeded_count_correct(self, onboarding, store):
        result = onboarding.mark_known("u1", ["urlaub", "strand"], store)
        assert result.seeded_count == 2

    def test_second_mark_known_call_skips_already_known(self, onboarding, store):
        onboarding.mark_known("u1", ["urlaub"], store)
        result2 = onboarding.mark_known("u1", ["urlaub", "strand"], store)
        # "urlaub" already KNOWN_PASSIVE — skipped; "strand" is new
        assert result2.seeded_count == 1
        assert result2.skipped_count == 1

    def test_duplicate_keys_in_single_call_handled_safely(self, onboarding, store):
        # Passing the same word twice must not raise and must not double-count.
        result = onboarding.mark_known("u1", ["urlaub", "urlaub", "urlaub"], store)
        # After deduplication-by-state-check, only one write occurs.
        assert store.get_state("u1", _lemma("urlaub")) == KnowledgeState.KNOWN_PASSIVE
        # The second and third occurrences are skipped
        assert result.seeded_count + result.skipped_count == 3

    def test_mark_known_does_not_demote_higher_state(self, onboarding, store):
        store.set_state("u1", _lemma("urlaub"), KnowledgeState.MASTERED)
        result = onboarding.mark_known("u1", ["urlaub"], store)
        assert store.get_state("u1", _lemma("urlaub")) == KnowledgeState.MASTERED
        assert result.skipped_count == 1


# ---------------------------------------------------------------------------
# TestNonDemotion
# ---------------------------------------------------------------------------

class TestNonDemotion:
    """seed_from_level() and seed_from_lemmas() must never lower existing state."""

    def test_mastered_unit_not_demoted_by_level_seed(self, onboarding, store):
        store.set_state("u1", _lemma("sein"), KnowledgeState.MASTERED)
        onboarding.seed_from_level("u1", LevelTier.A1, store)
        assert store.get_state("u1", _lemma("sein")) == KnowledgeState.MASTERED

    def test_known_active_unit_not_demoted(self, onboarding, store):
        store.set_state("u1", _lemma("gehen"), KnowledgeState.KNOWN_ACTIVE)
        onboarding.seed_from_level("u1", LevelTier.A1, store)
        assert store.get_state("u1", _lemma("gehen")) == KnowledgeState.KNOWN_ACTIVE

    def test_same_state_unit_skipped_not_rewritten(self, onboarding, store):
        store.set_state("u1", _lemma("sein"), KnowledgeState.KNOWN_PASSIVE)
        result = onboarding.seed_from_lemmas(
            "u1", ["sein"], store, state=KnowledgeState.KNOWN_PASSIVE
        )
        assert result.skipped_count == 1
        assert result.seeded_count == 0

    def test_unlocked_unit_can_be_advanced_by_higher_seed_state(self, onboarding, store):
        # If we seed at KNOWN_ACTIVE, an UNLOCKED unit should be upgraded.
        store.set_state("u1", _lemma("urlaub"), KnowledgeState.UNLOCKED)
        result = onboarding.seed_from_lemmas(
            "u1", ["urlaub"], store, state=KnowledgeState.KNOWN_ACTIVE
        )
        assert store.get_state("u1", _lemma("urlaub")) == KnowledgeState.KNOWN_ACTIVE
        assert result.seeded_count == 1

    def test_reseed_after_full_a2_skips_all(self, onboarding, store):
        onboarding.seed_from_level("u1", LevelTier.A2, store)
        result2 = onboarding.seed_from_level("u1", LevelTier.A2, store)
        assert result2.seeded_count == 0
        assert result2.skipped_count == VocabularyOnboarding.tier_size(LevelTier.A2)


# ---------------------------------------------------------------------------
# TestOnboardingResult
# ---------------------------------------------------------------------------

class TestOnboardingResult:

    def test_seeded_plus_skipped_equals_input_count(self, onboarding, store):
        words = ["gehen", "kommen", "sein"]
        # Pre-seed one of them
        store.set_state("u1", _lemma("sein"), KnowledgeState.KNOWN_PASSIVE)
        result = onboarding.seed_from_lemmas("u1", words, store)
        assert result.seeded_count + result.skipped_count == 3

    def test_user_id_in_result(self, onboarding, store):
        result = onboarding.seed_from_level("alice", LevelTier.A1, store)
        assert result.user_id == "alice"

    def test_result_is_frozen(self, onboarding, store):
        result = onboarding.seed_from_level("u1", LevelTier.A1, store)
        with pytest.raises((AttributeError, TypeError)):
            result.seeded_count = 0  # type: ignore[misc]

    def test_complete_beginner_result_all_zeros(self, onboarding, store):
        result = onboarding.seed_from_level("u1", LevelTier.COMPLETE_BEGINNER, store)
        assert result.seeded_count == 0
        assert result.skipped_count == 0


# ---------------------------------------------------------------------------
# TestKnowledgeStoreIntegration
# ---------------------------------------------------------------------------

class TestKnowledgeStoreIntegration:
    """After seeding, the store must support the full i+1 pipeline contract."""

    def test_seeded_units_reported_as_known(self, onboarding, store):
        onboarding.seed_from_level("u1", LevelTier.A1, store)
        for lemma_key in ["sein", "haben", "gehen", "nicht"]:
            assert store.is_known("u1", _lemma(lemma_key))

    def test_unseeded_units_remain_unknown(self, onboarding, store):
        onboarding.seed_from_level("u1", LevelTier.A1, store)
        # "urlaub" is not in any tier
        assert not store.is_known("u1", _lemma("urlaub"))

    def test_build_profile_contains_seeded_keys(self, onboarding, store):
        onboarding.seed_from_level("u1", LevelTier.A1, store)
        profile = store.build_profile("u1")
        assert (LearningUnitType.LEMMA, "sein") in profile.known_keys
        assert (LearningUnitType.LEMMA, "urlaub") not in profile.known_keys

    def test_i1_filter_fires_after_a1_seed(self, onboarding, store):
        # Seed A1. Use canonical lemma keys (not inflected forms) to match
        # exactly what LearningUnitExtractor would produce. One unknown → i+1.
        onboarding.seed_from_level("u1", LevelTier.A1, store)
        units = [_lemma(k) for k in ["sein", "machen", "gut", "urlaub"]]
        sole = store.find_sole_unknown("u1", units)
        assert sole is not None
        assert sole.key == "urlaub"

    def test_i1_filter_does_not_fire_with_two_unknowns(self, onboarding, store):
        onboarding.seed_from_level("u1", LevelTier.A1, store)
        # Both "urlaub" and "buchung" unknown → not i+1
        units = [_lemma(k) for k in ["sein", "urlaub", "buchung"]]
        assert store.find_sole_unknown("u1", units) is None

    def test_i1_filter_does_not_fire_when_all_known(self, onboarding, store):
        onboarding.seed_from_level("u1", LevelTier.A1, store)
        units = [_lemma(k) for k in ["sein", "haben", "gehen"]]
        assert store.find_sole_unknown("u1", units) is None

    def test_mark_known_makes_unit_eligible_as_i1_context(self, onboarding, store):
        # Seed nothing, then user marks "reise" as known.
        # Sentence with only "urlaub" unknown → i+1 match.
        onboarding.mark_known("u1", ["reise", "machen", "gern"], store)
        units = [_lemma(k) for k in ["reise", "machen", "urlaub", "gern"]]
        sole = store.find_sole_unknown("u1", units)
        assert sole is not None
        assert sole.key == "urlaub"

    def test_get_summary_reflects_seeded_state_counts(self, onboarding, store):
        result = onboarding.seed_from_level("u1", LevelTier.A1, store)
        summary = store.get_summary("u1")
        assert summary[KnowledgeState.KNOWN_PASSIVE] == result.seeded_count

    def test_sample_tier_lemmas_returns_sorted_subset(self):
        sample = VocabularyOnboarding.sample_tier_lemmas(LevelTier.A2, n=5, above_tier=LevelTier.A1)
        assert len(sample) == 5
        assert sample == sorted(sample)  # deterministic sorted order
