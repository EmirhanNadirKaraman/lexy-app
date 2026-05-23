"""
Tests for POST /api/v1/sentences/match.

These tests do not touch the database. A local fixture creates a client
that stubs out the DB pool lifecycle so the tests remain self-contained.
"""
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

MATCH = "/api/v1/sentences/match"


@pytest.fixture
async def matcher_client():
    """App client with DB pool creation stubbed out."""
    from backend.main import app

    with (
        patch("backend.database.create_pool", new_callable=AsyncMock),
        patch("backend.database.close_pool", new_callable=AsyncMock),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------


async def test_response_contains_sentence_and_phrases(matcher_client: AsyncClient):
    resp = await matcher_client.post(MATCH, json={"sentence": "Ich lerne Deutsch."})

    assert resp.status_code == 200
    data = resp.json()
    assert data["sentence"] == "Ich lerne Deutsch."
    assert isinstance(data["phrases"], list)
    assert len(data["phrases"]) > 0


async def test_each_phrase_has_required_fields(matcher_client: AsyncClient):
    resp = await matcher_client.post(MATCH, json={"sentence": "Ich lerne Deutsch."})

    for phrase in resp.json()["phrases"]:
        assert "dictionary_entry" in phrase
        assert "sentence_phrase" in phrase
        assert "logic" in phrase
        assert "match_type" in phrase
        assert "indices" in phrase
        assert isinstance(phrase["sentence_phrase"], list)
        assert isinstance(phrase["indices"], list)


# ---------------------------------------------------------------------------
# Basic matching behaviour
# ---------------------------------------------------------------------------


async def test_verb_phrase_detected(matcher_client: AsyncClient):
    """'lerne' should produce a verb-type phrase with indices."""
    resp = await matcher_client.post(
        MATCH, json={"sentence": "Ich lade meine Freunde ein."}
    )
    phrases = resp.json()["phrases"]

    # At least one phrase should come from a VERB token
    verb_phrases = [p for p in phrases if "VERB" in p["logic"] or "->" in p["logic"]]
    assert len(verb_phrases) > 0


async def test_separable_verb_blueprint(matcher_client: AsyncClient):
    """'einladen' is a separable verb — the blueprint should include 'ein'."""
    resp = await matcher_client.post(
        MATCH, json={"sentence": "Ich lade meine Freunde zum Essen ein."}
    )
    phrases = resp.json()["phrases"]

    verb_entries = [p["dictionary_entry"] for p in phrases if "->" in p["logic"]]
    # At least one verb entry should reference einladen / ein
    assert any("ein" in e.lower() for e in verb_entries)


async def test_empty_sentence_returns_empty_phrases(matcher_client: AsyncClient):
    resp = await matcher_client.post(MATCH, json={"sentence": "   "})

    assert resp.status_code == 200
    assert resp.json()["phrases"] == []


async def test_missing_sentence_field_returns_422(matcher_client: AsyncClient):
    resp = await matcher_client.post(MATCH, json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Stage 1 — language-gated extractor
# ---------------------------------------------------------------------------

async def test_language_query_de_keeps_german_extraction(matcher_client: AsyncClient):
    """Explicitly passing ?language=de must not change German behaviour
    (back-compat with the no-param call asserted above)."""
    resp = await matcher_client.post(
        f"{MATCH}?language=de", json={"sentence": "Ich lerne Deutsch."}
    )
    assert resp.status_code == 200
    assert len(resp.json()["phrases"]) > 0


async def test_language_query_es_returns_no_phrases(matcher_client: AsyncClient):
    """A GERMAN sentence under ?language=es yields no phrases. Since #36 Spanish
    HAS an extractor and (since #39 slice 2) es text is parsed by the Spanish
    model — but German text contains no Spanish reflexive/verb-prep patterns, so
    the result is still []. Real Spanish extraction is covered by the unit tests
    below + tests/test_spanish_phrase_extractor.py."""
    resp = await matcher_client.post(
        f"{MATCH}?language=es", json={"sentence": "Ich lerne Deutsch."}
    )
    assert resp.status_code == 200
    assert resp.json()["phrases"] == []


async def test_language_query_unknown_returns_no_phrases(matcher_client: AsyncClient):
    resp = await matcher_client.post(
        f"{MATCH}?language=xx", json={"sentence": "Ich lerne Deutsch."}
    )
    assert resp.status_code == 200
    assert resp.json()["phrases"] == []


async def test_matcher_service_match_sentence_with_language_param():
    """Unit-level: match_sentence(german_text, language='es') returns [] and
    never invokes extract_german_logic — the language arg routes to the Spanish
    path, and German text has no Spanish patterns. (Asserts language threads
    through rather than being ignored.)"""
    from unittest.mock import patch
    from backend.services import matcher_service

    with patch.object(matcher_service._pf, "extract_german_logic") as ge_mock:
        result = await matcher_service.match_sentence("Ich lerne Deutsch.", "es")
        assert result == []
        # The German extractor must NOT be called when language='es'.
        ge_mock.assert_not_called()


async def test_matcher_service_match_sentence_default_language_is_de():
    """Back-compat: omitting `language` must still run the German extractor."""
    from backend.services import matcher_service

    result = await matcher_service.match_sentence("Ich lerne Deutsch.")
    assert isinstance(result, list)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# #39 slice 2 — per-language model selection (the matcher used to parse every
# language with the German model)
# ---------------------------------------------------------------------------

import importlib.util as _ilu  # noqa: E402

_ES_INSTALLED = _ilu.find_spec("es_core_news_sm") is not None
requires_es = pytest.mark.skipif(not _ES_INSTALLED, reason="es_core_news_sm not installed")


@requires_es
async def test_spanish_match_loads_spanish_model_not_german():
    """The core fix: a Spanish match parses with the Spanish model (via
    nlp_service), not phrase_finder's German model. Spy proves the selection
    unambiguously — a behavioural test alone could pass on German-parsed junk."""
    from backend.services import matcher_service, nlp_service

    with patch.object(
        nlp_service.NLPService, "get_model",
        wraps=nlp_service.NLPService.get_model,
    ) as gm:
        await matcher_service.match_sentence("Me llamo Ana.", "es")
    gm.assert_any_call("es")


@requires_es
async def test_spanish_reflexive_matched_via_spanish_model():
    """Behavioural: a Spanish reflexive yields its canonical — only possible
    when the sentence is parsed by the Spanish model."""
    from backend.services import matcher_service

    out = await matcher_service.match_sentence("Me llamo Ana.", "es")
    assert any(p["dictionary_entry"] == "llamarse" for p in out)


@requires_es
async def test_spanish_verbprep_matched_via_spanish_model():
    from backend.services import matcher_service

    out = await matcher_service.match_sentence("Dependo de mis padres.", "es")
    assert any(p["dictionary_entry"] == "depender de" for p in out)


async def test_german_match_unchanged_unit():
    """German still parses with the resident German model and extracts."""
    from backend.services import matcher_service

    out = await matcher_service.match_sentence("Ich lade meine Freunde ein.")
    assert len(out) > 0


async def test_unknown_language_returns_no_phrases_unit():
    """No registered extractor → no parsing, no phrases (no model load)."""
    from backend.services import matcher_service

    assert await matcher_service.match_sentence("cualquier cosa", "xx") == []
