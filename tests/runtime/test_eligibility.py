"""
Runtime-aligned port of the unit-extraction and eligibility tests
(#17 salvage batch 5 — the final batch).

Targets the RUNTIME modules `eligibility.py` (593 lines) and
`utterance_unit_extractor.py` (751 lines), not the orphan refactor at
`src/app/learning/eligibility.py` / `src/app/extraction/extractor.py`.

Why this batch closed the campaign: `eligibility.py` is the **i+1 matching
core** — the rule that a sentence is only a teaching opportunity when exactly
one of its units is unknown. It is the single most load-bearing piece of the
learning model, and before this file it had **zero** coverage of the shipping
copy. `utterance_unit_extractor.py` had only `_has_garbage_symbols` covered.

Both classes live here rather than in separate files because they share the
`nlp` fixture, the A1 seed data, and the `lemma_unit` / `make_candidate`
helpers; splitting them would duplicate all of that.

Parity verified empirically before porting (`inspect.signature` + enum
comparison, NOT grep — grep produced a false parity answer three times during
this campaign):
  UtteranceEligibilityEvaluator.evaluate(self, user_id, units, target)   identical
  UtteranceEligibilityEvaluator.find_eligible_targets(self, user_id, units)  identical
  UtteranceUnitExtractor.extract(self, utterance)                        identical
  UserKnowledgeStore.seed_known_units(self, user_id, units, state=...)   identical
  IneligibilityReason members AND values                                 identical

Ported nodeids (src/app suite), both from tests/pipeline/test_pipeline.py:
  TestUnitExtraction::* (5 of 5)
  TestEligibility::*    (8 of 8)
"""
import pytest
import spacy

from eligibility import IneligibilityReason, UtteranceEligibilityEvaluator
from learning_units import LearningUnit, LearningUnitType
from subtitle_merger import MergedSubtitleWindow, SubtitleFragment
from subtitle_segmenter import CandidateUtterance
from user_knowledge import KnowledgeState, UserKnowledgeStore
from utterance_unit_extractor import UtteranceUnitExtractor

USER = "alice"

# A1-level German seeded as KNOWN_PASSIVE. Words ABSENT from this set are the
# unknowns the tests use as acquisition targets — deliberately absent:
# "schön", "wunderbar", "interessant".
A1_SEED: frozenset[str] = frozenset({
    # Auxiliaries and modals (AUX — content words by extractor default)
    "sein", "haben", "werden", "können", "müssen", "sollen", "wollen",
    # Common verbs
    "gehen", "kommen", "sehen", "machen", "arbeiten", "kaufen", "laufen",
    # Adjectives / adverbs
    "gut", "groß", "neu", "klein", "lang",
    "sehr", "wirklich", "heute", "hier", "jetzt", "morgen",
    # Nouns
    "kino", "film", "buch", "auto", "haus", "tag",
    "mann", "frau", "kind",
})


def make_candidate(text: str, start: float = 0.0, end: float = 3.0) -> CandidateUtterance:
    frag = SubtitleFragment(text=text, start_time=start, end_time=end, index=0)
    window = MergedSubtitleWindow(
        fragments=[frag], text=text, start_time=start, end_time=end
    )
    return CandidateUtterance(
        text=text,
        start_time=start,
        end_time=end,
        source_window=window,
        char_start=0,
        char_end=len(text),
    )


def lemma_unit(key: str) -> LearningUnit:
    return LearningUnit(LearningUnitType.LEMMA, key, key)


@pytest.fixture(scope="session")
def nlp():
    """Load de_core_news_md once per session — model load is expensive."""
    try:
        return spacy.load("de_core_news_md")
    except OSError:
        pytest.skip(
            "de_core_news_md not installed. "
            "Run: python -m spacy download de_core_news_md"
        )


@pytest.fixture
def extractor(nlp) -> UtteranceUnitExtractor:
    return UtteranceUnitExtractor(nlp)


@pytest.fixture
def store() -> UserKnowledgeStore:
    """Fresh store with A1_SEED pre-loaded as KNOWN_PASSIVE."""
    s = UserKnowledgeStore()
    s.seed_known_units(
        USER,
        [lemma_unit(k) for k in A1_SEED],
        state=KnowledgeState.KNOWN_PASSIVE,
    )
    return s


@pytest.fixture
def evaluator(store: UserKnowledgeStore) -> UtteranceEligibilityEvaluator:
    return UtteranceEligibilityEvaluator(store)


class TestUnitExtraction:
    """Candidate utterances → LearningUnit sets. What comes out of here is
    what eligibility then reasons over, so an extraction error silently
    changes every downstream i+1 decision."""

    def test_content_words_extracted(self, extractor):
        """Nouns, AUX verbs, and adjectives produce LEMMA units."""
        result = extractor.extract(make_candidate("Das Kino ist sehr schön."))
        keys = {u.key for u in result.units}

        assert "kino" in keys
        assert "sein" in keys     # AUX 'ist' → lemma 'sein'
        assert "schön" in keys

    def test_function_words_excluded_by_default(self, extractor):
        """Determiners, pronouns and prepositions are excluded when
        include_function_words=False (the default). Including them would make
        almost every sentence i+2 or worse."""
        result = extractor.extract(make_candidate("Ich gehe in das Kino."))
        keys = {u.key for u in result.units}

        assert "ich" not in keys    # PRON
        assert "das" not in keys    # DET
        assert "in" not in keys     # ADP

    def test_separable_verb_particle_combined_into_lemma(self, extractor):
        """The svp dependency identifies separable particles: 'macht … auf'
        must become 'aufmachen', not 'machen' + 'auf'. Splitting them would
        teach a verb the learner never actually met."""
        result = extractor.extract(make_candidate("Sie macht die Tür auf."))
        keys = {u.key for u in result.units}

        assert "aufmachen" in keys
        assert "machen" not in keys   # bare verb without particle must not appear

    def test_conjugated_verb_lemmatised_to_infinitive(self, extractor):
        """Past forms map to the same lemma as the present, so knowing 'gehen'
        covers 'ging' / 'gingen'."""
        result = extractor.extract(make_candidate("Wir gingen gestern ins Kino."))
        keys = {u.key for u in result.units}

        assert "gehen" in keys    # gingen → gehen
        assert "kino" in keys

    def test_repeated_lemma_in_utterance_deduplicated(self, extractor):
        """A lemma appearing twice is one unit for i+1 purposes. token_units
        keeps duplicates; units must not — otherwise a sentence repeating the
        target would look like i+2."""
        result = extractor.extract(
            make_candidate("Sie geht ins Kino, weil sie immer ins Kino geht.")
        )
        keys = [u.key for u in result.units]

        assert keys.count("gehen") == 1
        assert keys.count("kino") == 1


class TestEligibility:
    """The i+1 decision itself: a sentence teaches only when exactly one unit
    is unknown. Every test here uses the A1-seeded store, so anything outside
    A1_SEED is unknown."""

    def test_sole_unknown_is_eligible(self, evaluator):
        """Happy path — the target is the only unknown unit."""
        units = [
            lemma_unit("kino"),      # known
            lemma_unit("sein"),      # known
            lemma_unit("wirklich"),  # known
            lemma_unit("schön"),     # unknown — the target
        ]
        decision = evaluator.evaluate(USER, units, lemma_unit("schön"))

        assert decision.eligible
        assert decision.target_unit.key == "schön"
        assert not decision.blocking_units

    def test_two_unknowns_blocks_eligibility(self, evaluator):
        """i+2 → OTHER_UNKNOWNS_PRESENT, and blocking_units names the culprit
        so the caller can explain why the sentence was skipped."""
        units = [
            lemma_unit("kino"),         # known
            lemma_unit("sein"),         # known
            lemma_unit("wunderbar"),    # unknown — proposed target
            lemma_unit("interessant"),  # unknown — blocker
        ]
        decision = evaluator.evaluate(USER, units, lemma_unit("wunderbar"))

        assert not decision.eligible
        assert decision.ineligibility_reason == IneligibilityReason.OTHER_UNKNOWNS_PRESENT
        assert len(decision.blocking_units) == 1
        assert decision.blocking_units[0].key == "interessant"

    def test_target_already_known_is_ineligible(self, evaluator):
        """A known word cannot be an acquisition target."""
        units = [lemma_unit("kino"), lemma_unit("sein"), lemma_unit("schön")]
        decision = evaluator.evaluate(USER, units, lemma_unit("kino"))

        assert not decision.eligible
        assert decision.ineligibility_reason == IneligibilityReason.TARGET_ALREADY_KNOWN

    def test_target_absent_from_utterance_is_ineligible(self, evaluator):
        """The target must actually appear in the extracted unit list."""
        units = [lemma_unit("kino"), lemma_unit("sein"), lemma_unit("schön")]
        decision = evaluator.evaluate(USER, units, lemma_unit("pizza"))

        assert not decision.eligible
        assert decision.ineligibility_reason == IneligibilityReason.TARGET_NOT_IN_UTTERANCE

    def test_empty_unit_list_is_ineligible(self, evaluator):
        """An utterance with nothing extractable cannot be a learning exposure."""
        decision = evaluator.evaluate(USER, [], lemma_unit("schön"))

        assert not decision.eligible
        assert decision.ineligibility_reason == IneligibilityReason.NO_LEARNABLE_UNITS

    def test_eligible_decision_partitions_units_correctly(self, evaluator):
        """known_units must exclude the target; unknown_units must be exactly
        the target."""
        units = [
            lemma_unit("kino"),    # known
            lemma_unit("sein"),    # known
            lemma_unit("schön"),   # unknown
        ]
        decision = evaluator.evaluate(USER, units, lemma_unit("schön"))

        assert decision.eligible
        known_keys = {u.key for u in decision.known_units}
        unknown_keys = {u.key for u in decision.unknown_units}
        assert "schön" not in known_keys
        assert unknown_keys == {"schön"}

    def test_find_eligible_targets_returns_sole_unknown(self, evaluator):
        """find_eligible_targets auto-discovers the single valid i+1 target
        without the caller naming it."""
        units = [
            lemma_unit("kino"),      # known
            lemma_unit("sein"),      # known
            lemma_unit("wirklich"),  # known
            lemma_unit("schön"),     # unknown
        ]
        eligible = evaluator.find_eligible_targets(USER, units)

        assert len(eligible) == 1
        assert eligible[0].target_unit.key == "schön"

    def test_find_eligible_targets_empty_for_two_unknowns(self, evaluator):
        """No i+1 target exists when two units are unknown."""
        units = [
            lemma_unit("kino"),
            lemma_unit("wunderbar"),    # unknown
            lemma_unit("interessant"),  # unknown
        ]
        eligible = evaluator.find_eligible_targets(USER, units)

        assert eligible == []
