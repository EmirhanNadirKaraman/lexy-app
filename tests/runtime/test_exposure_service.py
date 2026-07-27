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

Ported nodeids (src/app suite):
  tests/exposure/test_exposure_integration.py::TestDeduplication::* (4 of 6)
  tests/exposure/test_exposure_integration.py::TestSingleExposure::* (3 of 5)
  tests/exposure/test_exposure_integration.py::TestCounterStoreInvariant::* (3 of 5)
"""
import pytest

from exposure_counter import CountingPolicy, DuplicateRule, QualifiedExposureCounter
from exposure_service import ExposureService
from learning_units import LearningUnit, LearningUnitType
from user_knowledge import KnowledgeState, UserKnowledgeStore

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
