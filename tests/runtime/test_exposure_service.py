"""
Runtime-aligned port of the exposure counter/store integration tests
(#17 salvage batch 3).

Targets the RUNTIME modules `exposure_counter.py` (761 lines),
`exposure_service.py` and `user_knowledge.py` — not the orphan refactor at
`src/app/exposure/`.

The invariant these tests exist to protect:

    counter.get_raw_count(user_id, unit)
        == store.get_knowledge(user_id, unit).exposure_count

`ExposureService.record_qualified_exposure()` is the single correct write point.
Two failure modes it guards, both of which are silent:

1. **Double-counting on replay.** A caller that skips the counter's duplicate
   check could replay the same subtitle clip and advance knowledge state on
   every replay — a user who seeks back five times in one sitting would unlock
   a word they genuinely saw once.
2. **Silent count divergence.** A caller that updates one component but not the
   other leaves the two numbers disagreeing with no error, making both
   untrustworthy for any later comparison.

Parity verified before porting: `record_qualified_exposure` has a byte-identical
signature in both trees (checked with `inspect.signature`), and `CountingPolicy`
exposes the same three fields (`duplicate_rule`, `diminishing_decay`,
`min_weight`). The only difference is structural — runtime keeps
`CountingPolicy` / `DuplicateRule` inside `exposure_counter.py` where the
refactor split them into `models.py`. That is an import-line change, not a
behaviour change. No production code was changed.

Batch 4 additionally ports the state-progression group, which needed the
`fast_store` / `fast_service` threshold fixtures (unlock at 3 instead of the
default 5) — a fixture adaptation only; no behaviour is stubbed. `ExposurePolicy`
carries the same two fields (`auto_advance`, `exposures_to_unlock`) and
`KnowledgeState` the same six members with the same values in both trees,
confirmed with `dataclasses.fields()` / enum comparison rather than by reading.

Ported nodeids (src/app suite), all from
tests/exposure/test_exposure_integration.py:
  batch 3: TestDeduplication::* (4 of 6)
           TestSingleExposure::* (3 of 5)
           TestCounterStoreInvariant::* (3 of 5)
  batch 4: TestStateProgression::* (5 of 5)
           TestAlreadyKnownTarget::* (4 of 4)
           TestPostUnlockBehavior::* (4 of 4)
"""
import pytest

from exposure_counter import CountingPolicy, DuplicateRule, QualifiedExposureCounter
from exposure_service import ExposureService
from learning_units import LearningUnit, LearningUnitType
from user_knowledge import ExposurePolicy, KnowledgeState, UserKnowledgeStore

USER = "alice"
OTHER_USER = "bob"


def lemma(key: str) -> LearningUnit:
    return LearningUnit(LearningUnitType.LEMMA, key, key)


def utt(n: int) -> str:
    """A distinct utterance ID per exposure in a sequence."""
    return f"utt:sentence-{n}"


def _assert_invariant(
    service: ExposureService,
    user_id: str,
    unit: LearningUnit,
    expected_count: int,
) -> None:
    """Counter and store must show the same count."""
    raw = service.counter.get_raw_count(user_id, unit)
    stored = service.store.get_knowledge(user_id, unit).exposure_count
    assert raw == expected_count, f"counter raw_count: expected {expected_count}, got {raw}"
    assert stored == expected_count, f"store exposure_count: expected {expected_count}, got {stored}"


@pytest.fixture
def target() -> LearningUnit:
    return lemma("schön")


@pytest.fixture
def store() -> UserKnowledgeStore:
    """Bare store with default policy (exposures_to_unlock=5)."""
    return UserKnowledgeStore()


@pytest.fixture
def counter() -> QualifiedExposureCounter:
    """Default counter: DEDUPLICATE_UTTERANCE."""
    return QualifiedExposureCounter()


@pytest.fixture
def service(counter: QualifiedExposureCounter, store: UserKnowledgeStore) -> ExposureService:
    return ExposureService(counter, store)


@pytest.fixture
def fast_store() -> UserKnowledgeStore:
    """Lower unlock threshold (3 instead of the default 5) so progression tests
    stay short. Only the threshold differs — no behaviour is stubbed."""
    return UserKnowledgeStore(exposure_policy=ExposurePolicy(exposures_to_unlock=3))


@pytest.fixture
def fast_service(fast_store: UserKnowledgeStore) -> ExposureService:
    return ExposureService(QualifiedExposureCounter(), fast_store)


class TestSingleExposure:
    def test_state_advances_from_unseen_to_exposed(self, service, target):
        service.record_qualified_exposure(USER, target, utt(1))
        assert service.store.get_state(USER, target) == KnowledgeState.EXPOSED

    def test_counter_and_store_both_show_count_one(self, service, target):
        service.record_qualified_exposure(USER, target, utt(1))
        _assert_invariant(service, USER, target, expected_count=1)

    def test_unrelated_unit_is_unaffected(self, service, target):
        """Recording one unit must not leak into another's counters."""
        service.record_qualified_exposure(USER, target, utt(1))
        other = lemma("hässlich")
        assert service.counter.get_raw_count(USER, other) == 0
        assert service.store.get_knowledge(USER, other).exposure_count == 0


class TestDeduplication:
    """The counter's duplicate policy gates whether the store is updated."""

    def test_same_utterance_id_rejected_on_second_call(self, service, target):
        first = service.record_qualified_exposure(USER, target, utt(1))
        second = service.record_qualified_exposure(USER, target, utt(1))

        assert first is not None
        assert second is None

    def test_store_not_updated_when_counter_rejects_duplicate(self, service, target):
        """The replay-protection case: both components must stay at 1, not 2."""
        service.record_qualified_exposure(USER, target, utt(1))
        service.record_qualified_exposure(USER, target, utt(1))  # rejected

        _assert_invariant(service, USER, target, expected_count=1)

    def test_two_distinct_utterance_ids_both_accepted(self, service, target):
        e1 = service.record_qualified_exposure(USER, target, utt(1))
        e2 = service.record_qualified_exposure(USER, target, utt(2))

        assert e1 is not None
        assert e2 is not None
        _assert_invariant(service, USER, target, expected_count=2)

    def test_allow_all_policy_accepts_same_utterance_multiple_times(self, store, target):
        """ALLOW_ALL opts out of dedup — the invariant must still hold."""
        svc = ExposureService(
            QualifiedExposureCounter(CountingPolicy(duplicate_rule=DuplicateRule.ALLOW_ALL)),
            store,
        )
        for _ in range(3):
            svc.record_qualified_exposure(USER, target, utt(1))

        _assert_invariant(svc, USER, target, expected_count=3)


class TestCounterStoreInvariant:
    def test_invariant_holds_after_mix_of_accepted_and_rejected(self, service, target):
        service.record_qualified_exposure(USER, target, utt(1))
        service.record_qualified_exposure(USER, target, utt(1))  # dup, rejected
        service.record_qualified_exposure(USER, target, utt(2))
        service.record_qualified_exposure(USER, target, utt(2))  # dup, rejected
        service.record_qualified_exposure(USER, target, utt(3))

        _assert_invariant(service, USER, target, expected_count=3)

    def test_invariant_holds_independently_per_user(self, service, target):
        """Two users' counts must not bleed into each other."""
        service.record_qualified_exposure(USER, target, utt(1))
        service.record_qualified_exposure(OTHER_USER, target, utt(1))
        service.record_qualified_exposure(OTHER_USER, target, utt(2))

        _assert_invariant(service, USER, target, expected_count=1)
        _assert_invariant(service, OTHER_USER, target, expected_count=2)

    def test_invariant_holds_across_multiple_units(self, service):
        a, b = lemma("schön"), lemma("laufen")
        service.record_qualified_exposure(USER, a, utt(1))
        service.record_qualified_exposure(USER, b, utt(1))
        service.record_qualified_exposure(USER, b, utt(2))

        _assert_invariant(service, USER, a, expected_count=1)
        _assert_invariant(service, USER, b, expected_count=2)


class TestStateProgression:
    """Exposure count drives UNSEEN → EXPOSED → UNLOCKED via ExposurePolicy.
    Uses `fast_service` (exposures_to_unlock=3) to keep the sequences short."""

    def test_state_stays_exposed_below_threshold(self, fast_service, target):
        for i in range(2):  # 2 < threshold of 3
            fast_service.record_qualified_exposure(USER, target, utt(i))

        assert fast_service.store.get_state(USER, target) == KnowledgeState.EXPOSED

    def test_state_advances_to_unlocked_at_threshold(self, fast_service, target):
        for i in range(3):  # exactly at threshold
            fast_service.record_qualified_exposure(USER, target, utt(i))

        assert fast_service.store.get_state(USER, target) == KnowledgeState.UNLOCKED

    def test_exposures_beyond_threshold_do_not_advance_state_further(self, fast_service, target):
        """Exposure only owns UNSEEN→EXPOSED→UNLOCKED. Everything past UNLOCKED
        belongs to SRS review, so piling on exposures must not promote further."""
        for i in range(6):
            fast_service.record_qualified_exposure(USER, target, utt(i))

        assert fast_service.store.get_state(USER, target) == KnowledgeState.UNLOCKED

    def test_counter_and_store_counts_agree_at_each_step(self, fast_service, target):
        for i in range(5):
            fast_service.record_qualified_exposure(USER, target, utt(i))
            _assert_invariant(fast_service, USER, target, expected_count=i + 1)

    def test_custom_threshold_two_exposures_to_unlock(self, target):
        """The threshold is policy, not a constant — a store configured with 2
        must unlock on the second exposure."""
        quick_store = UserKnowledgeStore(exposure_policy=ExposurePolicy(exposures_to_unlock=2))
        svc = ExposureService(QualifiedExposureCounter(), quick_store)

        svc.record_qualified_exposure(USER, target, utt(1))
        assert quick_store.get_state(USER, target) == KnowledgeState.EXPOSED

        svc.record_qualified_exposure(USER, target, utt(2))
        assert quick_store.get_state(USER, target) == KnowledgeState.UNLOCKED


class TestAlreadyKnownTarget:
    """States above UNLOCKED belong to SRS review, not ExposurePolicy. An
    exposure must never regress or re-advance them."""

    def test_exposure_on_known_passive_unit_increments_count_but_leaves_state(
        self, service, target
    ):
        service.store.set_state(USER, target, KnowledgeState.KNOWN_PASSIVE)

        service.record_qualified_exposure(USER, target, utt(1))

        assert service.store.get_state(USER, target) == KnowledgeState.KNOWN_PASSIVE
        # The count still increments — the service records, it doesn't suppress.
        _assert_invariant(service, USER, target, expected_count=1)

    def test_exposure_on_mastered_unit_leaves_state_unchanged(self, service, target):
        service.store.set_state(USER, target, KnowledgeState.MASTERED)

        service.record_qualified_exposure(USER, target, utt(1))

        assert service.store.get_state(USER, target) == KnowledgeState.MASTERED

    def test_auto_advance_disabled_records_exposure_without_state_change(self, target):
        """With auto_advance off, exposures are still counted but never promote."""
        no_advance_store = UserKnowledgeStore(exposure_policy=ExposurePolicy(auto_advance=False))
        svc = ExposureService(QualifiedExposureCounter(), no_advance_store)

        svc.record_qualified_exposure(USER, target, utt(1))

        assert no_advance_store.get_state(USER, target) == KnowledgeState.UNSEEN
        assert no_advance_store.get_knowledge(USER, target).exposure_count == 1

    def test_onboarding_seed_state_not_overwritten_by_exposure(self, service, target):
        """Words seeded KNOWN_PASSIVE during onboarding are ones the user already
        knows. An incidental exposure must not demote them."""
        service.store.set_state(USER, target, KnowledgeState.KNOWN_PASSIVE)
        state_before = service.store.get_state(USER, target)

        service.record_qualified_exposure(USER, target, utt(1))

        assert service.store.get_state(USER, target) == state_before


class TestPostUnlockBehavior:
    """After UNLOCKED, the service must keep counting without interfering with
    SRS-managed state — and the counter/store invariant must still hold."""

    def test_exposures_continue_accumulating_while_unit_is_unlocked(self, fast_service, target):
        for i in range(3):
            fast_service.record_qualified_exposure(USER, target, utt(i))
        assert fast_service.store.get_state(USER, target) == KnowledgeState.UNLOCKED

        fast_service.record_qualified_exposure(USER, target, utt(3))
        fast_service.record_qualified_exposure(USER, target, utt(4))

        _assert_invariant(fast_service, USER, target, expected_count=5)
        assert fast_service.store.get_state(USER, target) == KnowledgeState.UNLOCKED

    def test_srs_promotion_to_known_passive_then_exposure_leaves_state(
        self, fast_service, target
    ):
        for i in range(3):
            fast_service.record_qualified_exposure(USER, target, utt(i))
        fast_service.store.set_state(USER, target, KnowledgeState.KNOWN_PASSIVE)

        # A later exposure must not demote below KNOWN_PASSIVE.
        fast_service.record_qualified_exposure(USER, target, utt(3))

        assert fast_service.store.get_state(USER, target) == KnowledgeState.KNOWN_PASSIVE

    def test_srs_demotion_then_further_exposures_do_not_re_advance_past_unlocked(
        self, fast_service, target
    ):
        """The inactivity path: SRS demotes KNOWN_PASSIVE back to UNLOCKED, and
        further exposures count but cannot auto-promote past UNLOCKED again."""
        fast_service.store.set_state(USER, target, KnowledgeState.KNOWN_PASSIVE)
        fast_service.store.set_state(USER, target, KnowledgeState.UNLOCKED)

        for i in range(4):
            fast_service.record_qualified_exposure(USER, target, utt(i))

        assert fast_service.store.get_state(USER, target) == KnowledgeState.UNLOCKED
        _assert_invariant(fast_service, USER, target, expected_count=4)

    def test_unit_seen_in_two_contexts_before_becoming_known(self, fast_service):
        """Two words learned from different sentences each follow the full
        UNSEEN→EXPOSED→UNLOCKED arc independently."""
        word_a = lemma("schön")
        word_b = lemma("wunderbar")

        for i in range(3):
            fast_service.record_qualified_exposure(USER, word_a, f"utt:schoen-{i}")
        for i in range(2):
            fast_service.record_qualified_exposure(USER, word_b, f"utt:wunderbar-{i}")

        assert fast_service.store.get_state(USER, word_a) == KnowledgeState.UNLOCKED
        assert fast_service.store.get_state(USER, word_b) == KnowledgeState.EXPOSED
