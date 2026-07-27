"""
Minimal end-to-end smoke test for the runtime pipeline's stage COMPOSITION.

Targets the RUNTIME `pipeline.GermanSubtitlePipeline`.

Why this file exists: Phase 2 deleted the old `src/app/` smoke suite (11 tests)
along with the refactor it bound to. The individual stages are each covered in
`tests/runtime/` — cleaning, ingestion, merging, segmentation, quality filter,
extraction, eligibility, exposure — but nothing verified they are *wired
together* correctly. A regression that only appears in composition (a stage
dropped from `run_fragments`, a config not threaded to a sub-component, the i+1
filter bypassed) would pass every stage-level test and still ship broken.

Deliberately small. This is not a re-creation of the deleted suite: four tests
covering construction, the happy path, the negative path, and the exposure
round-trip. Stage internals belong in the per-stage files, not here.

Uses `run_fragments`, not `run`, so there is **no file I/O, no network and no
external service** — fragments are built in memory. The only external
dependency is the spaCy model, skipped-if-missing exactly as in the other
runtime tests.

The i+1 scenario was probed against the real pipeline before being asserted:
"Ich gehe heute ins Kino." extracts units {gehen, heute, kino}. Seeding
`gehen` + `heute` leaves `kino` as the sole unknown, which is what makes the
utterance an i+1 match.
"""
import pytest
import spacy

from learning_units import LearningUnit, LearningUnitType
from pipeline import GermanSubtitlePipeline, I1Match
from subtitle_merger import SubtitleFragment
from user_knowledge import KnowledgeState

USER = "smoke-user"


def lemma(key: str) -> LearningUnit:
    return LearningUnit(LearningUnitType.LEMMA, key, key)


def fragment(text: str, start: float = 1.0, end: float = 3.2) -> SubtitleFragment:
    return SubtitleFragment(text=text, start_time=start, end_time=end, index=0)


# One sentence, one clearly-identifiable target. Extraction yields
# {gehen, heute, kino}; function words (Ich / ins) are excluded by default.
SENTENCE = "Ich gehe heute ins Kino."
SENTENCE_UNITS = ("gehen", "heute", "kino")
TARGET = "kino"


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
def pipeline(nlp) -> GermanSubtitlePipeline:
    return GermanSubtitlePipeline(nlp)


def test_construction_wires_every_stage(pipeline):
    """Constructing the pipeline must assemble all six stages plus the shared
    store and exposure service. If a stage stops being constructed, the run
    methods fail with AttributeError far from the cause."""
    assert pipeline._merger is not None
    assert pipeline._segmenter is not None
    assert pipeline._quality_filter is not None
    assert pipeline._extractor is not None
    assert pipeline.store is not None
    assert pipeline.exposure_service is not None
    # The exposure service must share the pipeline's store, not hold its own —
    # otherwise recorded exposures would never affect i+1 decisions.
    assert pipeline.exposure_service.store is pipeline.store


def test_sole_unknown_produces_an_i1_match(pipeline):
    """The happy path, all stages composed: fragments → merge → segment →
    quality → extract → i+1 filter. With two of three units seeded known,
    the third is the sole unknown and the utterance is a match."""
    pipeline.seed_known_vocabulary(USER, [lemma("gehen"), lemma("heute")])

    matches = pipeline.run_fragments([fragment(SENTENCE)], USER)

    assert len(matches) == 1
    match = matches[0]
    assert isinstance(match, I1Match)
    assert match.target_unit.key == TARGET
    assert SENTENCE.split()[-1].rstrip(".") in match.utterance.text
    # The extraction is carried through for UI highlighting — confirm the
    # composition passes it along rather than discarding it.
    assert {u.key for u in match.extraction.units} == set(SENTENCE_UNITS)


def test_multiple_unknowns_produce_no_match(pipeline):
    """The negative path. Without a seed all three units are unknown, so the
    utterance is i+3 and must be filtered out. If this returns a match, the
    i+1 filter has been bypassed somewhere in the composition."""
    matches = pipeline.run_fragments([fragment(SENTENCE)], USER)

    assert matches == []


def test_recorded_exposure_reaches_the_shared_store(pipeline):
    """The write path: pipeline → exposure service → store, keyed by the
    match's own utterance_id. Also covers dedup, since surfacing the same clip
    twice must not count twice."""
    pipeline.seed_known_vocabulary(USER, [lemma("gehen"), lemma("heute")])
    (match,) = pipeline.run_fragments([fragment(SENTENCE)], USER)

    event = pipeline.record_exposure(USER, match.target_unit, utterance_id=match.utterance_id)
    assert event is not None
    assert pipeline.store.get_state(USER, match.target_unit) == KnowledgeState.EXPOSED

    duplicate = pipeline.record_exposure(
        USER, match.target_unit, utterance_id=match.utterance_id
    )
    assert duplicate is None
