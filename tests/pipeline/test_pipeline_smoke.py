"""
test_pipeline_smoke.py
-----------------------
End-to-end smoke tests for GermanSubtitlePipeline using real spaCy.

All tests in this file require de_core_news_md.  If the model is not
installed, the entire module is skipped via the session-scoped `nlp`
fixture.  Run separately from the unit-test suite:

    pytest tests/pipeline/test_pipeline_smoke.py -v

What these tests verify that unit tests cannot:
  - The real spaCy NER component suppresses proper nouns as targets.
  - The real lemmatiser produces keys that match the onboarding tier lists.
  - The real quality filter and i+1 filter interact correctly end-to-end.
  - run_with_diagnostics() returns a coherent, internally consistent snapshot
    on real subtitle content.
"""
from __future__ import annotations

import textwrap

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def nlp():
    """Load de_core_news_md once for all smoke tests; skip if not installed."""
    spacy = pytest.importorskip("spacy")
    try:
        return spacy.load("de_core_news_md")
    except OSError:
        pytest.skip("de_core_news_md not installed — run: python -m spacy download de_core_news_md")


@pytest.fixture(scope="module")
def srt_path(tmp_path_factory):
    """
    Write a realistic 15-sentence German SRT to a temp file.

    The content deliberately mixes:
      - Sentences with known A2 vocabulary (eligible after seeding)
      - Sentences with proper nouns (Berlin, Hans) — must NOT become i+1 targets
      - Sentences with above-A2 vocabulary (genuinely new words)
      - Short / exclamatory utterances (whitelist candidates)
    """
    srt = textwrap.dedent("""\
        1
        00:00:01,000 --> 00:00:03,500
        Ich fahre morgen nach Berlin.

        2
        00:00:04,000 --> 00:00:06,000
        Das ist wirklich sehr schön hier.

        3
        00:00:06,500 --> 00:00:09,000
        Hans kommt später zum Treffen.

        4
        00:00:09,500 --> 00:00:12,000
        Wir müssen das Problem schnell lösen.

        5
        00:00:12,500 --> 00:00:15,000
        Der Film war ziemlich langweilig.

        6
        00:00:15,500 --> 00:00:18,000
        Sie hat das Buch schon gelesen.

        7
        00:00:18,500 --> 00:00:21,000
        Die Verhandlungen dauern noch an.

        8
        00:00:21,500 --> 00:00:24,000
        Er arbeitet seit Jahren bei der Firma.

        9
        00:00:24,500 --> 00:00:27,000
        Das neue Gesetz tritt nächsten Monat in Kraft.

        10
        00:00:27,500 --> 00:00:30,000
        Ich verstehe das nicht ganz.

        11
        00:00:30,500 --> 00:00:33,000
        München ist eine wunderschöne Stadt.

        12
        00:00:33,500 --> 00:00:36,000
        Die Situation erfordert mehr Geduld.

        13
        00:00:36,500 --> 00:00:39,000
        Kannst du mir bitte helfen?

        14
        00:00:39,500 --> 00:00:42,000
        Die BMW-Fabrik liegt außerhalb der Stadt.

        15
        00:00:42,500 --> 00:00:45,000
        Er hat die Aufgabe erfolgreich abgeschlossen.
    """)

    path = tmp_path_factory.mktemp("smoke") / "test_episode.srt"
    path.write_text(srt, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def pipeline_and_result(nlp, srt_path):
    """Run run_with_diagnostics() once; share result across all smoke tests."""
    from app.learning.onboarding import LevelTier, VocabularyOnboarding
    from app.pipeline.runner import GermanSubtitlePipeline
    from app.learning.knowledge import UserKnowledgeStore

    store = UserKnowledgeStore()
    VocabularyOnboarding().seed_from_level("smoke_user", LevelTier.A2, store)

    pipeline = GermanSubtitlePipeline(nlp)
    pipeline.store = store

    matches, diag = pipeline.run_with_diagnostics(srt_path, "smoke_user")
    return pipeline, matches, diag


# ---------------------------------------------------------------------------
# Stage count sanity
# ---------------------------------------------------------------------------

class TestStageCounts:
    def test_fragments_ingested_equals_srt_blocks(self, pipeline_and_result):
        _, _, diag = pipeline_and_result
        assert diag.fragments_ingested == 15

    def test_candidates_accepted_plus_rejected_equals_segmented(self, pipeline_and_result):
        _, _, diag = pipeline_and_result
        assert diag.candidates_accepted + diag.candidates_rejected == diag.candidates_segmented

    def test_eligible_plus_ineligible_equals_accepted(self, pipeline_and_result):
        _, _, diag = pipeline_and_result
        accounted = (
            diag.eligible_utterances
            + diag.ineligible_all_known
            + diag.ineligible_too_many_unknowns
            + diag.ineligible_no_units
        )
        assert accounted == diag.candidates_accepted

    def test_i1_rate_in_valid_range(self, pipeline_and_result):
        _, _, diag = pipeline_and_result
        # After A2 seeding there must be SOME matches, but not 100%
        rate = diag.i1_rate
        assert rate is not None
        assert 0.0 < rate < 1.0, (
            f"i1_rate={rate:.2%} — expected some matches but not all. "
            f"eligible={diag.eligible_utterances}, accepted={diag.candidates_accepted}"
        )


# ---------------------------------------------------------------------------
# Named entity filtering (fix 1 validation)
# ---------------------------------------------------------------------------

class TestNamedEntityFiltering:
    def test_no_match_target_is_a_named_entity(self, nlp, pipeline_and_result):
        """
        After the NER fix, no i+1 match should have a proper noun as its target.

        Strategy: re-parse each utterance with spaCy, find the token whose
        lemma matches the target key, and assert its ent_type_ is not in the
        NER set.  A city name like 'berlin', a person name like 'hans', or an
        org like 'bmw' must not appear as acquisition targets.
        """
        _, matches, _ = pipeline_and_result
        ner_labels = {"PER", "LOC", "ORG", "MISC"}

        for match in matches:
            doc = nlp(match.utterance.text)
            for token in doc:
                if token.lemma_.lower() == match.target_unit.key:
                    assert token.ent_type_ not in ner_labels, (
                        f"Named entity {match.target_unit.key!r} (type={token.ent_type_!r}) "
                        f"appeared as i+1 target in: {match.utterance.text!r}"
                    )

    def test_berlin_is_not_a_target(self, pipeline_and_result):
        """Sentence 1 contains 'Berlin' — it must not be the i+1 target."""
        _, matches, _ = pipeline_and_result
        target_keys = {m.target_unit.key for m in matches}
        assert "berlin" not in target_keys

    def test_hans_is_not_a_target(self, pipeline_and_result):
        """Sentence 3 contains 'Hans' — person names must not be targets."""
        _, matches, _ = pipeline_and_result
        target_keys = {m.target_unit.key for m in matches}
        assert "hans" not in target_keys


# ---------------------------------------------------------------------------
# Onboarding / lemma key consistency
# ---------------------------------------------------------------------------

class TestLemmaKeyConsistency:
    def test_all_known_units_are_not_targets(self, pipeline_and_result):
        """
        No target unit should already be in the A2 seed.

        A seeded unit is KNOWN_PASSIVE (≥ threshold), so find_sole_unknown()
        must never return it.  If a seeded lemma key and an extracted lemma key
        diverge (e.g. seeded 'aufmachen' but spaCy extracted 'machen'), the
        seeded key doesn't protect and the mismatch shows up here as a target
        that was supposed to be known.
        """
        from app.learning.onboarding import LevelTier, VocabularyOnboarding
        a2_lemmas = VocabularyOnboarding.get_tier_lemmas(LevelTier.A2)

        _, matches, _ = pipeline_and_result
        for match in matches:
            assert match.target_unit.key not in a2_lemmas, (
                f"Target {match.target_unit.key!r} is in the A2 seed but appeared as unknown. "
                f"Possible onboarding/extraction key mismatch. Utterance: {match.utterance.text!r}"
            )


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------

class TestPersistenceRoundTrip:
    def test_save_and_load_preserves_state(self, pipeline_and_result, tmp_path):
        """
        UserKnowledgeStore.save() / load() must reproduce the exact same
        knowledge states so a pipeline restart doesn't lose user progress.
        """
        from app.learning.knowledge import UserKnowledgeStore

        pipeline, _, _ = pipeline_and_result
        store = pipeline.store
        save_path = tmp_path / "store.json"

        store.save(save_path)
        assert save_path.exists()

        loaded = UserKnowledgeStore.load(save_path)
        assert loaded._store.keys() == store._store.keys()

        for user_id in store._store:
            for key, original in store._store[user_id].items():
                restored = loaded._store[user_id][key]
                assert restored.state == original.state
                assert restored.exposure_count == original.exposure_count
                assert restored.unit.key == original.unit.key

    def test_load_raises_for_missing_file(self, tmp_path):
        from app.learning.knowledge import UserKnowledgeStore
        with pytest.raises(FileNotFoundError):
            UserKnowledgeStore.load(tmp_path / "nonexistent.json")

    def test_load_raises_for_wrong_version(self, tmp_path):
        import json
        from app.learning.knowledge import UserKnowledgeStore
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"version": 99, "users": {}}), encoding="utf-8")
        with pytest.raises(ValueError, match="version"):
            UserKnowledgeStore.load(bad)
