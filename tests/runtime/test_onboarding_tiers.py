"""
Runtime-aligned port of the onboarding tier + non-demotion tests
(#17 salvage batch 2).

Targets the RUNTIME modules `onboarding.py`, `user_knowledge.py` and
`learning_units.py` — not the orphan refactor at `src/app/learning/`.

Two invariant families, both load-bearing for the onboarding UX:

1. **Tier structure.** Tiers must nest strictly (A1 ⊂ A2 ⊂ B1), be
   deterministic, be lowercase, and never contain a `_SKIP_LEMMAS` word. A user
   who picks A2 is promised everything A1 covers; if nesting breaks, they
   silently lose words. If a skip-lemma leaks into a tier, onboarding seeds a
   key the extractor can never produce, so the word is marked known and then
   never seen again.

2. **Non-demotion.** Seeding must never *lower* existing knowledge. Re-running
   onboarding after real progress would otherwise wipe it.

`test_b1_is_superset_of_a2` is deliberately NOT re-ported here — batch 1 already
covers it in `test_learning_invariants.py`. `test_a2_is_superset_of_a1` is the
sibling case and is included.

API parity was verified before porting: `VocabularyOnboarding` static helpers,
`UserKnowledgeStore(filter_policy=…, exposure_policy=…)`, `set_state`,
`get_state`, and `OnboardingResult.{seeded_count,skipped_count}` are identical
between runtime and the refactor. No production code was changed.

Ported nodeids (src/app suite):
  tests/learning/test_onboarding.py::TestGetTierLemmas::* (9 of 10; b1-superset already ported)
  tests/learning/test_onboarding.py::TestNonDemotion::* (5)
"""
import pytest

from learning_units import LearningUnit, LearningUnitType, _SKIP_LEMMAS
from onboarding import _TIER_ORDER, LevelTier, VocabularyOnboarding
from user_knowledge import (
    ExposurePolicy,
    KnowledgeFilterPolicy,
    KnowledgeState,
    UserKnowledgeStore,
)


def _lemma(key: str) -> LearningUnit:
    return LearningUnit(LearningUnitType.LEMMA, key, key)


@pytest.fixture
def store() -> UserKnowledgeStore:
    """Fresh in-memory store with the same policies the orphan test used."""
    return UserKnowledgeStore(
        filter_policy=KnowledgeFilterPolicy(min_known_state=KnowledgeState.KNOWN_PASSIVE),
        exposure_policy=ExposurePolicy(auto_advance=True, exposures_to_unlock=5),
    )


@pytest.fixture
def onboarding() -> VocabularyOnboarding:
    return VocabularyOnboarding()


class TestGetTierLemmas:
    """Tier sets must nest, be stable, and stay extraction-compatible."""

    def test_complete_beginner_returns_empty_set(self):
        assert VocabularyOnboarding.get_tier_lemmas(LevelTier.COMPLETE_BEGINNER) == frozenset()

    def test_a1_returns_nonempty_set(self):
        assert len(VocabularyOnboarding.get_tier_lemmas(LevelTier.A1)) > 0

    def test_a2_is_superset_of_a1(self):
        a1 = VocabularyOnboarding.get_tier_lemmas(LevelTier.A1)
        a2 = VocabularyOnboarding.get_tier_lemmas(LevelTier.A2)
        assert a1 < a2  # strict subset

    def test_tiers_are_strictly_growing(self):
        sizes = [len(VocabularyOnboarding.get_tier_lemmas(t)) for t in _TIER_ORDER]
        for prev, curr in zip(sizes, sizes[1:]):
            assert curr > prev

    def test_result_is_deterministic(self):
        first = VocabularyOnboarding.get_tier_lemmas(LevelTier.A2)
        second = VocabularyOnboarding.get_tier_lemmas(LevelTier.A2)
        assert first == second

    def test_all_lemmas_are_lowercase(self):
        for tier in _TIER_ORDER:
            for lemma in VocabularyOnboarding.get_tier_lemmas(tier):
                assert lemma == lemma.lower(), f"{lemma!r} is not lowercase"

    def test_no_skip_lemmas_in_any_tier(self):
        """A `_SKIP_LEMMAS` word can never be an extraction target, so seeding
        one marks a word known that the user will then never be shown."""
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


class TestMarkKnown:
    """`mark_known` is the user-facing "I already know these words" path, so it
    takes arbitrary user input — duplicates included — and must be safe."""

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
        assert result2.seeded_count == 1
        assert result2.skipped_count == 1

    def test_duplicate_keys_in_single_call_handled_safely(self, onboarding, store):
        """The same word passed three times must not raise or double-count."""
        result = onboarding.mark_known("u1", ["urlaub", "urlaub", "urlaub"], store)
        assert store.get_state("u1", _lemma("urlaub")) == KnowledgeState.KNOWN_PASSIVE
        assert result.seeded_count + result.skipped_count == 3

    def test_mark_known_does_not_demote_higher_state(self, onboarding, store):
        store.set_state("u1", _lemma("urlaub"), KnowledgeState.MASTERED)
        result = onboarding.mark_known("u1", ["urlaub"], store)
        assert store.get_state("u1", _lemma("urlaub")) == KnowledgeState.MASTERED
        assert result.skipped_count == 1


class TestNonDemotion:
    """Seeding must never lower existing state — re-running onboarding after
    real progress must not wipe it."""

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
        """Non-demotion is not "never change" — an UNLOCKED unit seeded at a
        higher state must still be upgraded."""
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
