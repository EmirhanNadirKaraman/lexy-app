"""
test_pipeline.py
----------------
End-to-end and unit-level regression tests for the German subtitle
language-learning pipeline.

Covered scenarios
-----------------
  - Fragment merging: broken subtitles, hyphen joins, gap-based separation
  - Quality filtering: short utterances, whitelist bypass, incomplete endings
  - Segmentation: single vs multi-sentence windows, timing interpolation
  - Unit extraction: content words, separable verbs, lemmatisation
  - Eligibility (i+1): all four failure paths and the happy path
  - Exposure counting: all four duplicate policies, stats, threshold queries
  - Integration: full-stack tests wiring every stage together

NLP tests require de_core_news_md:
    python -m spacy download de_core_news_md

All tests that require the model are collected under classes that take `nlp`
as a fixture; tests that need only the rule-based stages run without it.
"""
import pytest
from datetime import datetime, timezone

import spacy

from app.learning.eligibility import IneligibilityReason, UtteranceEligibilityEvaluator
from app.exposure.counter import QualifiedExposureCounter
from app.exposure.models import CountingPolicy, DuplicateRule
from app.exposure.service import ExposureService
from app.learning.units import LearningUnit, LearningUnitType
from app.subtitles.merging import SubtitleMerger
from app.subtitles.models import MergedSubtitleWindow, SubtitleFragment, CandidateUtterance
from app.subtitles.segmentation import SegmentationConfig, SubtitleSegmenter
from app.subtitles.quality import QualityFilterConfig, UtteranceQualityEvaluator
from app.extraction.extractor import UtteranceUnitExtractor
from app.learning.knowledge import KnowledgeState, UserKnowledgeStore


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

USER = "alice"

# A1-level German vocabulary seeded as KNOWN_PASSIVE in all tests that need a
# knowledge store.  Words *absent* from this set are treated as unknowns and
# used as acquisition targets.
#
# Intentionally absent (used as targets in tests):
#   "schön", "wunderbar", "interessant", "fantastisch"
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


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def make_fragment(
    text: str,
    start: float = 0.0,
    end: float = 3.0,
    index: int = 0,
) -> SubtitleFragment:
    return SubtitleFragment(text=text, start_time=start, end_time=end, index=index)


def make_window(
    text: str,
    start: float = 0.0,
    end: float = 3.0,
) -> MergedSubtitleWindow:
    frag = make_fragment(text, start, end)
    return MergedSubtitleWindow(
        fragments=[frag],
        text=text,
        start_time=start,
        end_time=end,
    )


def make_candidate(
    text: str,
    start: float = 0.0,
    end: float = 3.0,
) -> CandidateUtterance:
    window = make_window(text, start, end)
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


def seeded_store() -> UserKnowledgeStore:
    """Return a fresh UserKnowledgeStore with A1_SEED pre-loaded."""
    store = UserKnowledgeStore()
    store.seed_known_units(
        USER,
        [lemma_unit(k) for k in A1_SEED],
        state=KnowledgeState.KNOWN_PASSIVE,
    )
    return store


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def nlp():
    """Load de_core_news_md once per test session (model load is expensive)."""
    try:
        return spacy.load("de_core_news_md")
    except OSError:
        pytest.skip(
            "de_core_news_md not installed. "
            "Run: python -m spacy download de_core_news_md"
        )


@pytest.fixture
def merger():
    return SubtitleMerger()


@pytest.fixture
def quality_evaluator():
    return UtteranceQualityEvaluator()


@pytest.fixture
def extractor(nlp):
    return UtteranceUnitExtractor(nlp)


@pytest.fixture
def store():
    return seeded_store()


@pytest.fixture
def evaluator(store):
    return UtteranceEligibilityEvaluator(store)


@pytest.fixture
def counter():
    return QualifiedExposureCounter()   # default: DEDUPLICATE_UTTERANCE


# ---------------------------------------------------------------------------
# TestSubtitleMerging
# ---------------------------------------------------------------------------

class TestSubtitleMerging:
    """Merging stage: raw fragments → merged windows."""

    def test_tiny_gap_forces_unconditional_merge(self, merger):
        """
        Fragments 0.1 s apart (below tiny_gap_s = 0.15 s) merge regardless
        of punctuation — this is the typical mid-sentence subtitle break.
        """
        frags = [
            make_fragment("Das Kino ist wirklich", 0.0, 1.8, index=0),
            make_fragment("schön.", 1.9, 2.5, index=1),  # gap = 0.1 s
        ]
        windows = merger.merge_fragments(frags)

        assert len(windows) == 1
        assert windows[0].text == "Das Kino ist wirklich schön."

    def test_lowercase_start_merges_across_moderate_gap(self, merger):
        """
        A fragment starting with a lowercase letter is a syntactic continuation.
        The lowercase heuristic should trigger a merge even at a 0.3 s gap.
        """
        frags = [
            make_fragment("Das Buch ist wirklich", 0.0, 1.5, index=0),
            make_fragment("interessant.", 1.8, 2.5, index=1),  # gap = 0.3 s, lowercase
        ]
        windows = merger.merge_fragments(frags)

        assert len(windows) == 1
        assert "wirklich interessant" in windows[0].text

    def test_hyphen_word_break_merged_and_joined(self, merger):
        """
        A line ending with a hyphen signals a word broken across subtitle
        lines. The merger must join them and drop the hyphen.
        """
        frags = [
            make_fragment("Mein Lieblings-", 0.0, 1.5, index=0),
            make_fragment("buch.", 1.8, 2.2, index=1),  # gap = 0.3 s
        ]
        windows = merger.merge_fragments(frags)

        assert len(windows) == 1
        assert "Lieblingsbuch" in windows[0].text
        assert "-" not in windows[0].text

    def test_large_gap_keeps_fragments_separate(self, merger):
        """
        Fragments more than max_gap_s = 0.6 s apart must produce separate
        windows, even when heuristics might otherwise suggest merging.
        """
        frags = [
            make_fragment("Das ist gut.", 0.0, 2.0, index=0),
            make_fragment("Wirklich sehr gut.", 3.0, 5.0, index=1),  # gap = 1.0 s
        ]
        windows = merger.merge_fragments(frags)

        assert len(windows) == 2

    def test_strong_punctuation_plus_uppercase_prevents_merge(self, merger):
        """
        A full stop followed by an uppercase start and a gap above tiny_gap_s
        is an unambiguous sentence boundary — no merge should happen.
        """
        frags = [
            make_fragment("Das war gut.", 0.0, 2.0, index=0),
            make_fragment("Jetzt gehen wir.", 2.3, 4.0, index=1),  # gap = 0.3 s
        ]
        windows = merger.merge_fragments(frags)

        assert len(windows) == 2

    def test_continuation_preposition_forces_merge(self, merger):
        """
        A fragment ending with a preposition is syntactically open —
        it cannot be a complete sentence and must merge with the next fragment.
        """
        frags = [
            make_fragment("Ich gehe in", 0.0, 1.0, index=0),
            make_fragment("die Stadt.", 1.3, 2.0, index=1),  # gap = 0.3 s
        ]
        windows = merger.merge_fragments(frags)

        assert len(windows) == 1
        assert windows[0].text == "Ich gehe in die Stadt."

    def test_merged_window_preserves_outer_timing(self, merger):
        """Start and end times of a merged window must come from the outermost fragments."""
        frags = [
            make_fragment("Ich gehe", 1.0, 2.0, index=0),
            make_fragment("nach Hause.", 2.05, 3.5, index=1),
        ]
        windows = merger.merge_fragments(frags)

        assert len(windows) == 1
        assert windows[0].start_time == pytest.approx(1.0)
        assert windows[0].end_time == pytest.approx(3.5)


# ---------------------------------------------------------------------------
# TestQualityFilter
# ---------------------------------------------------------------------------

class TestQualityFilter:
    """Quality-filter stage: candidate utterances → accepted/rejected."""

    def test_normal_german_sentence_passes(self, quality_evaluator):
        """A clean, well-formed German sentence should pass all checks."""
        decision = quality_evaluator.evaluate(
            make_candidate("Das Kino ist wirklich sehr schön.")
        )
        assert decision.passed

    def test_single_word_utterance_rejected(self, quality_evaluator):
        """One-word fragments are below min_tokens and must be rejected."""
        decision = quality_evaluator.evaluate(make_candidate("Ja."))

        assert not decision.passed
        assert any("token" in r.lower() for r in decision.failure_reasons)

    def test_whitelisted_short_utterance_passes(self):
        """
        'Ja.' normally fails the token-count check, but a whitelist entry for
        the normalised form 'ja' bypasses all other checks entirely.
        This is the mechanism for preserving valid short discourse markers.
        """
        config = QualityFilterConfig(whitelist=frozenset({"ja", "nein", "okay"}))
        evaluator = UtteranceQualityEvaluator(config)

        decision = evaluator.evaluate(make_candidate("Ja."))

        assert decision.passed

    def test_utterance_ending_with_conjunction_rejected(self, quality_evaluator):
        """A trailing conjunction is a sure sign of a truncated fragment."""
        decision = quality_evaluator.evaluate(
            make_candidate("Ich gehe ins Kino und")
        )
        assert not decision.passed

    def test_utterance_ending_with_preposition_rejected(self, quality_evaluator):
        """A trailing preposition indicates the sentence was cut mid-phrase."""
        decision = quality_evaluator.evaluate(
            make_candidate("Ich bin auf der Suche nach")
        )
        assert not decision.passed

    def test_filter_returns_only_passing_candidates(self, quality_evaluator):
        """filter() should return only the subset of candidates that pass."""
        candidates = [
            make_candidate("Das Kino ist sehr schön."),   # passes
            make_candidate("Ja."),                        # rejected: 1 token
            make_candidate("Der Film war wunderbar."),    # passes
        ]
        accepted = quality_evaluator.filter(candidates)

        assert len(accepted) == 2
        assert all(c.text != "Ja." for c in accepted)


# ---------------------------------------------------------------------------
# TestSegmentation
# ---------------------------------------------------------------------------

class TestSegmentation:
    """Segmentation stage: merged windows → candidate utterances."""

    def test_single_sentence_window_produces_one_candidate(self, nlp):
        """A window with one sentence produces exactly one CandidateUtterance."""
        window = make_window("Das Kino ist sehr schön.", 0.0, 3.0)
        segmenter = SubtitleSegmenter(nlp)
        candidates = segmenter.segment_window(window)

        assert len(candidates) == 1
        assert "schön" in candidates[0].text

    def test_two_sentence_window_produces_two_candidates(self, nlp):
        """
        A merged window that spans two sentences must be split.
        This is the key regression guard for multi-utterance subtitle windows
        created when the merger joins fragments from different sentences.
        """
        window = make_window(
            "Ich bin sehr müde. Ich muss morgen früh arbeiten.",
            start=0.0,
            end=6.0,
        )
        segmenter = SubtitleSegmenter(nlp)
        candidates = segmenter.segment_window(window)

        assert len(candidates) == 2
        assert "müde" in candidates[0].text
        assert "arbeiten" in candidates[1].text

    def test_split_window_timing_is_interpolated(self, nlp):
        """
        When a window is split, per-sentence times are linearly interpolated
        by character position.  The first candidate starts at the window's
        start_time; the last ends at the window's end_time; they do not overlap.
        """
        window = make_window(
            "Ich bin sehr müde. Ich muss morgen früh arbeiten.",
            start=0.0,
            end=6.0,
        )
        segmenter = SubtitleSegmenter(nlp)
        first, second = segmenter.segment_window(window)

        assert first.start_time == pytest.approx(0.0)
        assert second.end_time == pytest.approx(6.0)
        assert first.end_time <= second.start_time

    def test_segment_below_min_chars_is_dropped(self, nlp):
        """
        Segments shorter than min_chars are discarded.  Here we raise
        min_chars to 5 so that 'Ok.' (3 chars) gets dropped.
        """
        config = SegmentationConfig(min_chars=5)
        segmenter = SubtitleSegmenter(nlp, config)
        window = make_window("Ok. Das Kino ist sehr schön.", 0.0, 4.0)
        candidates = segmenter.segment_window(window)

        assert not any(c.text.strip() == "Ok." for c in candidates)


# ---------------------------------------------------------------------------
# TestUnitExtraction
# ---------------------------------------------------------------------------

class TestUnitExtraction:
    """Unit-extraction stage: candidate utterances → LearningUnit sets."""

    def test_content_words_extracted(self, extractor):
        """Nouns, AUX verbs, and adjectives produce LEMMA units."""
        result = extractor.extract(make_candidate("Das Kino ist sehr schön."))
        keys = {u.key for u in result.units}

        assert "kino" in keys
        assert "sein" in keys     # AUX 'ist' → lemma 'sein'
        assert "schön" in keys

    def test_function_words_excluded_by_default(self, extractor):
        """
        Determiners, pronouns, and prepositions are excluded when
        include_function_words=False (the default).
        """
        result = extractor.extract(make_candidate("Ich gehe in das Kino."))
        keys = {u.key for u in result.units}

        assert "ich" not in keys    # PRON
        assert "das" not in keys    # DET
        assert "in" not in keys     # ADP

    def test_separable_verb_particle_combined_into_lemma(self, extractor):
        """
        The svp dependency relation identifies separable particles.
        'macht … auf' must become 'aufmachen', not 'machen' + 'auf'.
        """
        result = extractor.extract(make_candidate("Sie macht die Tür auf."))
        keys = {u.key for u in result.units}

        assert "aufmachen" in keys
        assert "machen" not in keys   # bare verb without particle must not appear

    def test_conjugated_verb_lemmatised_to_infinitive(self, extractor):
        """
        Past-tense forms must map to the same lemma as the present so that
        the user's knowledge of 'gehen' covers 'ging', 'gingen', etc.
        """
        result = extractor.extract(make_candidate("Wir gingen gestern ins Kino."))
        keys = {u.key for u in result.units}

        assert "gehen" in keys    # gingen → gehen
        assert "kino" in keys

    def test_repeated_lemma_in_utterance_deduplicated(self, extractor):
        """
        The same lemma appearing twice counts as one unit for i+1 purposes.
        token_units preserves duplicates; units must not.
        """
        result = extractor.extract(
            make_candidate("Sie geht ins Kino, weil sie immer ins Kino geht.")
        )
        keys = [u.key for u in result.units]

        assert keys.count("gehen") == 1
        assert keys.count("kino") == 1


# ---------------------------------------------------------------------------
# TestEligibility
# ---------------------------------------------------------------------------

class TestEligibility:
    """
    Eligibility stage: i+1 decision logic.

    All tests use a UserKnowledgeStore seeded with A1_SEED so that words
    not in the seed are unknown by default.
    """

    def test_sole_unknown_is_eligible(self, evaluator):
        """
        Happy path: target is the only unknown unit.
        'Das Kino ist wirklich schön.' — 'schön' is not in A1_SEED.
        """
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
        """
        i+2: two unknown units → OTHER_UNKNOWNS_PRESENT.
        The blocking_units list names the non-target unknown.
        """
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
        """
        'kino' is in A1_SEED — it cannot be an acquisition target.
        """
        units = [lemma_unit("kino"), lemma_unit("sein"), lemma_unit("schön")]
        decision = evaluator.evaluate(USER, units, lemma_unit("kino"))

        assert not decision.eligible
        assert decision.ineligibility_reason == IneligibilityReason.TARGET_ALREADY_KNOWN

    def test_target_absent_from_utterance_is_ineligible(self, evaluator):
        """The target must appear in the utterance's extracted unit list."""
        units = [lemma_unit("kino"), lemma_unit("sein"), lemma_unit("schön")]
        decision = evaluator.evaluate(USER, units, lemma_unit("pizza"))

        assert not decision.eligible
        assert decision.ineligibility_reason == IneligibilityReason.TARGET_NOT_IN_UTTERANCE

    def test_empty_unit_list_is_ineligible(self, evaluator):
        """An utterance with no extractable units cannot be a learning exposure."""
        decision = evaluator.evaluate(USER, [], lemma_unit("schön"))

        assert not decision.eligible
        assert decision.ineligibility_reason == IneligibilityReason.NO_LEARNABLE_UNITS

    def test_eligible_decision_partitions_units_correctly(self, evaluator):
        """
        In an eligible decision, known_units must not contain the target;
        unknown_units must contain exactly the target.
        """
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
        """
        find_eligible_targets() auto-discovers the single valid i+1 target
        without requiring the caller to specify it.
        """
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


# ---------------------------------------------------------------------------
# TestExposureCounter
# ---------------------------------------------------------------------------

class TestExposureCounter:
    """Exposure-counting stage: event recording and deduplication policies."""

    def test_first_record_accepted_and_returns_event(self, counter):
        """record() must return a populated ExposureEvent on first acceptance."""
        event = counter.record(USER, lemma_unit("schön"), "utt:kino-001")

        assert event is not None
        assert event.user_id == USER
        assert event.unit.key == "schön"
        assert event.utterance_id == "utt:kino-001"
        assert event.weight == pytest.approx(1.0)
        assert event.event_id  # non-empty UUID

    def test_deduplicate_utterance_rejects_second_record(self, counter):
        """
        Default policy (DEDUPLICATE_UTTERANCE): the same (user, unit,
        utterance_id) triple is accepted only once across all time.
        """
        counter.record(USER, lemma_unit("schön"), "utt:kino-001")
        duplicate = counter.record(USER, lemma_unit("schön"), "utt:kino-001")

        assert duplicate is None
        assert counter.get_raw_count(USER, lemma_unit("schön")) == 1

    def test_distinct_utterances_for_same_unit_all_accepted(self, counter):
        """Each distinct utterance_id must produce one independent exposure."""
        for i in range(4):
            result = counter.record(USER, lemma_unit("schön"), f"utt:{i:03d}")
            assert result is not None

        stats = counter.get_stats(USER, lemma_unit("schön"))
        assert stats.raw_count == 4
        assert stats.unique_utterances == 4

    def test_allow_all_accepts_same_utterance_repeatedly(self):
        """ALLOW_ALL never rejects — every record() call increments raw_count."""
        counter = QualifiedExposureCounter(CountingPolicy(DuplicateRule.ALLOW_ALL))
        for _ in range(5):
            assert counter.record(USER, lemma_unit("schön"), "utt:kino-001") is not None

        assert counter.get_raw_count(USER, lemma_unit("schön")) == 5

    def test_diminishing_returns_first_exposure_has_full_weight(self):
        """First exposure always carries weight 1.0."""
        counter = QualifiedExposureCounter(
            CountingPolicy(DuplicateRule.DIMINISHING_RETURNS, diminishing_decay=0.5)
        )
        event = counter.record(USER, lemma_unit("schön"), "utt:kino-001")

        assert event is not None
        assert event.weight == pytest.approx(1.0)

    def test_diminishing_returns_weights_decay_geometrically(self):
        """
        Three consecutive repeats of the same utterance must produce weights
        1.0, 0.5, 0.25 respectively.  weighted_count = 1.75.
        """
        counter = QualifiedExposureCounter(
            CountingPolicy(DuplicateRule.DIMINISHING_RETURNS, diminishing_decay=0.5)
        )
        events = [
            counter.record(USER, lemma_unit("schön"), "utt:kino-001")
            for _ in range(3)
        ]

        weights = [e.weight for e in events]
        assert weights == pytest.approx([1.0, 0.5, 0.25])
        assert counter.get_weighted_count(USER, lemma_unit("schön")) == pytest.approx(1.75)

    def test_diminishing_returns_fresh_utterance_resets_to_full_weight(self):
        """
        Diminishing returns are tracked per utterance_id.  A new utterance
        gets weight 1.0 regardless of how many times an earlier utterance
        has been repeated.
        """
        counter = QualifiedExposureCounter(
            CountingPolicy(DuplicateRule.DIMINISHING_RETURNS, diminishing_decay=0.5)
        )
        counter.record(USER, lemma_unit("schön"), "utt:kino-001")
        counter.record(USER, lemma_unit("schön"), "utt:kino-001")  # weight 0.5

        fresh = counter.record(USER, lemma_unit("schön"), "utt:kino-002")

        assert fresh is not None
        assert fresh.weight == pytest.approx(1.0)

    def test_session_dedup_rejects_replay_within_session(self):
        """Replaying the same clip inside one session is a duplicate."""
        counter = QualifiedExposureCounter(
            CountingPolicy(DuplicateRule.DEDUPLICATE_SESSION)
        )
        counter.record(USER, lemma_unit("schön"), "utt:kino-001", session_id="ep1")
        result = counter.record(USER, lemma_unit("schön"), "utt:kino-001", session_id="ep1")

        assert result is None
        assert counter.get_raw_count(USER, lemma_unit("schön")) == 1

    def test_session_dedup_allows_same_utterance_in_new_session(self):
        """
        Rewatching an episode a week later should count as a genuine new
        exposure.  Same utterance_id in a different session_id must be accepted.
        """
        counter = QualifiedExposureCounter(
            CountingPolicy(DuplicateRule.DEDUPLICATE_SESSION)
        )
        counter.record(
            USER, lemma_unit("schön"), "utt:kino-001",
            session_id="ep1-2026-03-01",
        )
        result = counter.record(
            USER, lemma_unit("schön"), "utt:kino-001",
            session_id="ep1-2026-03-15",
        )

        assert result is not None
        assert counter.get_raw_count(USER, lemma_unit("schön")) == 2

    def test_units_above_threshold_returns_correct_subset(self):
        """units_above_threshold filters by weighted_count >= threshold."""
        counter = QualifiedExposureCounter()
        for i in range(5):
            counter.record(USER, lemma_unit("schön"), f"utt:schoen-{i}")
        for i in range(2):
            counter.record(USER, lemma_unit("fantastisch"), f"utt:fantastisch-{i}")

        ready = counter.units_above_threshold(USER, threshold=5.0)
        keys = {u.key for u in ready}

        assert "schön" in keys
        assert "fantastisch" not in keys

    def test_get_stats_populates_all_fields(self, counter):
        """ExposureStats must reflect recorded events exactly."""
        t1 = datetime(2026, 3, 15, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 3, 16, 12, 0, 0, tzinfo=timezone.utc)

        counter.record(USER, lemma_unit("schön"), "utt:kino-001", occurred_at=t1)
        counter.record(USER, lemma_unit("schön"), "utt:kino-002", occurred_at=t2)

        stats = counter.get_stats(USER, lemma_unit("schön"))

        assert stats.raw_count == 2
        assert stats.weighted_count == pytest.approx(2.0)
        assert stats.unique_utterances == 2
        assert stats.first_exposure == t1
        assert stats.last_exposure == t2

    def test_reset_user_clears_all_state(self, counter):
        """reset_user() must remove every record for that user."""
        counter.record(USER, lemma_unit("schön"), "utt:kino-001")
        counter.reset_user(USER)

        assert counter.get_raw_count(USER, lemma_unit("schön")) == 0
        assert counter.get_events(USER, lemma_unit("schön")) == []


# ---------------------------------------------------------------------------
# TestExposureService
# ---------------------------------------------------------------------------

class TestExposureService:
    """
    Unit tests for ExposureService — the orchestrator that keeps
    QualifiedExposureCounter and UserKnowledgeStore in sync.

    Invariant under test: after every accepted record_qualified_exposure():
        counter.get_raw_count(user_id, unit)
            == store.get_knowledge(user_id, unit).exposure_count
    """

    def test_accepted_exposure_updates_both_counter_and_store(self, store, counter):
        """A single accepted exposure increments both stores to 1."""
        service = ExposureService(counter, store)
        target = lemma_unit("schön")

        event = service.record_qualified_exposure(USER, target, "utt:kino-001")

        assert event is not None
        assert counter.get_raw_count(USER, target) == 1
        assert store.get_knowledge(USER, target).exposure_count == 1

    def test_duplicate_exposure_rejected_leaves_store_unchanged(self, store, counter):
        """
        A duplicate utterance (same utterance_id) must be rejected by the
        counter and must NOT increment the store's exposure_count.
        """
        service = ExposureService(counter, store)
        target = lemma_unit("schön")

        first = service.record_qualified_exposure(USER, target, "utt:kino-001")
        second = service.record_qualified_exposure(USER, target, "utt:kino-001")

        assert first is not None
        assert second is None
        assert counter.get_raw_count(USER, target) == 1
        assert store.get_knowledge(USER, target).exposure_count == 1

    def test_knowledge_state_auto_advances_on_first_exposure(self, store, counter):
        """
        First accepted exposure should auto-advance the unit from UNSEEN to
        EXPOSED via the store's ExposurePolicy.
        """
        service = ExposureService(counter, store)
        target = lemma_unit("schön")

        assert store.get_state(USER, target) == KnowledgeState.UNSEEN

        service.record_qualified_exposure(USER, target, "utt:kino-001")

        assert store.get_state(USER, target) == KnowledgeState.EXPOSED

    def test_store_and_counter_counts_stay_in_sync_after_n_exposures(
        self, store, counter
    ):
        """
        After N exposures from N distinct utterances both stores show N.
        """
        service = ExposureService(counter, store)
        target = lemma_unit("schön")

        for i in range(5):
            service.record_qualified_exposure(USER, target, f"utt:schoen-{i}")

        assert counter.get_raw_count(USER, target) == 5
        assert store.get_knowledge(USER, target).exposure_count == 5

    def test_reset_user_clears_both_counter_and_store(self, store, counter):
        """reset_user() on the service must zero out both components."""
        service = ExposureService(counter, store)
        target = lemma_unit("schön")

        service.record_qualified_exposure(USER, target, "utt:kino-001")
        service.reset_user(USER)

        assert counter.get_raw_count(USER, target) == 0
        assert store.get_knowledge(USER, target).exposure_count == 0
        assert store.get_state(USER, target) == KnowledgeState.UNSEEN


# ---------------------------------------------------------------------------
# TestPipeline  (integration — requires de_core_news_md)
# ---------------------------------------------------------------------------

class TestPipeline:
    """
    Full-stack integration tests wiring every pipeline stage.

    Each test starts from raw subtitle fragments (or a complete window) and
    drives data through merging → segmentation → quality filtering →
    unit extraction → eligibility evaluation → exposure recording.
    """

    def test_broken_subtitle_merges_and_yields_eligible_utterance(
        self, merger, nlp, quality_evaluator, extractor, evaluator
    ):
        """
        'Das Kino ist wirklich schön.' is split across two subtitle fragments
        at an arbitrary cut point.  After merging the fragments form one
        complete sentence, which is then eligible as i+1 for 'schön'.
        """
        frags = [
            make_fragment("Das Kino ist wirklich", 0.0, 1.8, index=0),
            make_fragment("schön.", 1.9, 2.5, index=1),   # gap 0.1 s → unconditional merge
        ]

        windows = merger.merge_fragments(frags)
        assert len(windows) == 1
        assert windows[0].text == "Das Kino ist wirklich schön."

        segmenter = SubtitleSegmenter(nlp)
        candidates = segmenter.segment_windows(windows)
        candidates = quality_evaluator.filter(candidates)
        assert len(candidates) == 1

        result = extractor.extract(candidates[0])
        keys = {u.key for u in result.units}
        assert "schön" in keys, f"Expected 'schön' in extracted units, got: {keys}"

        decision = evaluator.evaluate(USER, result.units, lemma_unit("schön"))
        assert decision.eligible

    def test_utterance_with_two_unknowns_is_not_eligible(
        self, nlp, quality_evaluator, extractor, evaluator
    ):
        """
        'Das Kino ist wunderbar und interessant.' has two unknown adjectives.
        Neither can be an i+1 target — both evaluations must return
        OTHER_UNKNOWNS_PRESENT with the other unknown as the blocker.
        """
        candidates = quality_evaluator.filter(
            [make_candidate("Das Kino ist wunderbar und interessant.")]
        )
        assert len(candidates) == 1

        result = extractor.extract(candidates[0])
        keys = {u.key for u in result.units}
        assert "wunderbar" in keys, f"Extracted: {keys}"
        assert "interessant" in keys, f"Extracted: {keys}"

        d_wunderbar = evaluator.evaluate(USER, result.units, lemma_unit("wunderbar"))
        d_interessant = evaluator.evaluate(USER, result.units, lemma_unit("interessant"))

        assert not d_wunderbar.eligible
        assert d_wunderbar.ineligibility_reason == IneligibilityReason.OTHER_UNKNOWNS_PRESENT
        assert not d_interessant.eligible
        assert d_interessant.ineligibility_reason == IneligibilityReason.OTHER_UNKNOWNS_PRESENT

    def test_target_already_known_makes_utterance_ineligible(
        self, extractor, store, evaluator
    ):
        """
        Promoting 'schön' to KNOWN_PASSIVE must immediately make the utterance
        ineligible for it — the store and evaluator share the same instance.
        """
        store.set_state(USER, lemma_unit("schön"), KnowledgeState.KNOWN_PASSIVE)

        result = extractor.extract(make_candidate("Das Kino ist schön."))
        decision = evaluator.evaluate(USER, result.units, lemma_unit("schön"))

        assert not decision.eligible
        assert decision.ineligibility_reason == IneligibilityReason.TARGET_ALREADY_KNOWN

    def test_one_window_segmented_into_two_eligible_utterances(
        self, nlp, extractor, evaluator
    ):
        """
        A merged window that contains two sentences produces two candidates
        that are independently eligible for different unknown targets.
        'Das Kino ist schön.'    → i+1 for 'schön'
        'Der Film ist wunderbar.' → i+1 for 'wunderbar'
        """
        window = make_window(
            "Das Kino ist schön. Der Film ist wunderbar.",
            start=0.0,
            end=8.0,
        )
        segmenter = SubtitleSegmenter(nlp)
        candidates = segmenter.segment_window(window)
        assert len(candidates) == 2

        results = [extractor.extract(c) for c in candidates]

        d_schoen = evaluator.evaluate(USER, results[0].units, lemma_unit("schön"))
        d_wunderbar = evaluator.evaluate(USER, results[1].units, lemma_unit("wunderbar"))

        assert d_schoen.eligible, (
            f"Expected 'schön' eligible. Units: {[u.key for u in results[0].units]}"
        )
        assert d_wunderbar.eligible, (
            f"Expected 'wunderbar' eligible. Units: {[u.key for u in results[1].units]}"
        )

    def test_repeated_exposure_of_same_utterance_not_double_counted(
        self, extractor, store, counter
    ):
        """
        Seeking back and replaying the same subtitle clip must not increment
        the exposure count in either the counter or the knowledge store under
        the default DEDUPLICATE_UTTERANCE policy.
        """
        service = ExposureService(counter, store)
        utt_id = "utt:kino-sehr-schoen"
        target = lemma_unit("schön")

        first = service.record_qualified_exposure(USER, target, utt_id)
        second = service.record_qualified_exposure(USER, target, utt_id)

        assert first is not None
        assert second is None
        # Both stores agree: count = 1
        assert counter.get_raw_count(USER, target) == 1
        assert store.get_knowledge(USER, target).exposure_count == 1

    def test_exposure_then_promotion_makes_unit_known(
        self, extractor, store, evaluator, counter
    ):
        """
        Full lifecycle: expose via ExposureService → verify counter and store
        stay in sync → promote via store.set_state() → verify eligibility changes.

        1. Initially 'schön' is unknown → utterance is eligible.
        2. Record exposure via ExposureService → both counter and store advance.
        3. Promote 'schön' to KNOWN_PASSIVE via store.set_state() (SRS result).
        4. The same utterance is now ineligible for 'schön'.
        """
        service = ExposureService(counter, store)
        target = lemma_unit("schön")
        result = extractor.extract(make_candidate("Das Kino ist schön."))

        # Step 1: eligible before any exposure
        before = evaluator.evaluate(USER, result.units, target)
        assert before.eligible

        # Step 2: record exposure — store auto-advances UNSEEN → EXPOSED
        event = service.record_qualified_exposure(USER, target, "utt:kino-schoen")
        assert event is not None
        assert counter.get_raw_count(USER, target) == 1
        assert store.get_knowledge(USER, target).exposure_count == 1
        assert store.get_state(USER, target) == KnowledgeState.EXPOSED

        # Step 3: SRS promotes the unit to KNOWN_PASSIVE
        store.set_state(USER, target, KnowledgeState.KNOWN_PASSIVE)

        # Step 4: no longer eligible — target is now known
        after = evaluator.evaluate(USER, result.units, target)
        assert not after.eligible
