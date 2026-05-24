"""#18 — centralised scraper language config (`subtitle-scraper/language_config.py`).

Locks the values + ORDER migrated out of pipeline.py's hardcoded maps, the
helper semantics, the unknown-language behaviour (must match the old
`LANG_MODEL_MAP.get` / `in LANG_TRANSCRIPT_CODES` / `not in NO_MORPH_LANGS`),
and that pipeline.py no longer hardcodes the maps. Plain-Python config (no YAML
dependency). Loaded by sys.path insert like the other scraper tests.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRAPER_DIR = Path(__file__).resolve().parents[1] / "subtitle-scraper"


def _load_language_config():
    """Load language_config.py by file path — NO sys.path mutation. A
    module-level `sys.path.insert` collides with test_scraper_channels' fixture
    (which removes the scraper dir in teardown) and breaks it under -n auto
    (round-3 lesson). language_config has no sibling imports, so a standalone
    spec-load is clean."""
    spec = importlib.util.spec_from_file_location(
        "scraper_language_config_under_test", SCRAPER_DIR / "language_config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lc = _load_language_config()


_EXPECTED_ORDER = ["en", "de", "fr", "es", "it", "pt", "ru", "ja", "ko"]


def test_config_loads_and_has_de_es():
    assert "de" in lc.LANGUAGES and "es" in lc.LANGUAGES


def test_model_names_match_old_map():
    # de uses the MEDIUM model; the rest _sm — locked against "normalization".
    assert lc.get_spacy_model_name("de") == "de_core_news_md"
    assert lc.get_spacy_model_name("es") == "es_core_news_sm"
    assert lc.get_spacy_model_name("en") == "en_core_web_sm"


def test_transcript_codes_match_old_map():
    assert lc.get_transcript_codes("de") == ["de", "de-DE", "de-AT"]
    assert lc.get_transcript_codes("es") == ["es", "es-ES", "es-MX", "es-419"]
    assert lc.get_transcript_codes("en") == ["en-GB", "en", "en-US"]


def test_order_is_preserved():
    # LOAD-BEARING: pipeline.get_transcript iterates TRANSCRIPT_CODES.items() in
    # order for subtitle auto-detect. A sorted()/set-based rebuild would break it.
    assert list(lc.MODEL_MAP.keys()) == _EXPECTED_ORDER
    assert list(lc.TRANSCRIPT_CODES.keys()) == _EXPECTED_ORDER
    assert lc.get_supported_languages() == _EXPECTED_ORDER


def test_no_morph_langs():
    assert lc.NO_MORPH_LANGS == {"ja", "ko"}
    assert lc.has_morphology("ja") is False
    assert lc.has_morphology("de") is True


def test_unknown_language_behaviour_matches_old():
    # old: LANG_MODEL_MAP.get(x) -> None; x in LANG_TRANSCRIPT_CODES -> False;
    # x not in NO_MORPH_LANGS -> True (an unknown language was treated as having
    # morphology). The helpers preserve all three.
    assert lc.get_spacy_model_name("xx") is None
    assert lc.get_transcript_codes("xx") == []
    assert lc.has_morphology("xx") is True
    assert "xx" not in lc.NO_MORPH_LANGS


def test_transcript_codes_returns_a_copy():
    # mutating the returned list must not corrupt the config.
    codes = lc.get_transcript_codes("de")
    codes.append("de-XX")
    assert lc.get_transcript_codes("de") == ["de", "de-DE", "de-AT"]


def test_validate_rejects_malformed():
    with pytest.raises(ValueError, match="spacy_model"):
        lc._validate({"xx": {"spacy_model": "", "transcript_codes": ["xx"], "has_morphology": True}})
    with pytest.raises(ValueError, match="transcript_codes"):
        lc._validate({"xx": {"spacy_model": "m", "transcript_codes": [], "has_morphology": True}})
    with pytest.raises(ValueError, match="has_morphology"):
        lc._validate({"xx": {"spacy_model": "m", "transcript_codes": ["xx"], "has_morphology": "yes"}})


def test_pipeline_no_longer_hardcodes_the_maps():
    """pipeline.py must source the maps from language_config, not redefine them —
    the whole point of #18 (add a language by editing language_config only).
    Source-level check: avoids the heavy + collision-prone `import pipeline`."""
    src = (SCRAPER_DIR / "pipeline.py").read_text(encoding="utf-8")
    assert "from language_config import" in src
    assert "LANG_MODEL_MAP = {" not in src
    assert "LANG_TRANSCRIPT_CODES = {" not in src
    assert 'NO_MORPH_LANGS = {"ja"' not in src
