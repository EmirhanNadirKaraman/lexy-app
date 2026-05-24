"""Centralised scraper language configuration (#18).

Single source of truth for the per-language settings that used to be hardcoded
dicts in pipeline.py (`LANG_MODEL_MAP` / `LANG_TRANSCRIPT_CODES` /
`NO_MORPH_LANGS`). Adding a scraper language now means editing only this file
(+ installing the spaCy model). See docs/MAINTENANCE.md
"How to add a scraper language".

Plain-Python config (not YAML): PyYAML is only *transitively* available here,
not a declared dependency, and the goal is centralisation, not a config-file
format — a module keeps this zero-dependency and trivially testable.

NB: the `LANGUAGES` insertion order is LOAD-BEARING — `pipeline.get_transcript`
iterates `TRANSCRIPT_CODES.items()` in order for subtitle auto-detection. Keep
the order stable; the derived maps below preserve it.
"""
from __future__ import annotations

# language code -> config. ORDER MATTERS (see module docstring): keep en, de,
# fr, es, it, pt, ru, ja, ko.
LANGUAGES: dict[str, dict] = {
    "en": {"spacy_model": "en_core_web_sm", "transcript_codes": ["en-GB", "en", "en-US"],          "has_morphology": True,  "phrase_extractor": None},
    # `de` uses the MEDIUM model (better German morphology); every other language
    # uses _sm. Do NOT "normalize" this to de_core_news_sm.
    "de": {"spacy_model": "de_core_news_md", "transcript_codes": ["de", "de-DE", "de-AT"],          "has_morphology": True,  "phrase_extractor": "de"},
    "fr": {"spacy_model": "fr_core_news_sm", "transcript_codes": ["fr", "fr-FR", "fr-CA"],          "has_morphology": True,  "phrase_extractor": None},
    "es": {"spacy_model": "es_core_news_sm", "transcript_codes": ["es", "es-ES", "es-MX", "es-419"], "has_morphology": True,  "phrase_extractor": "es"},
    "it": {"spacy_model": "it_core_news_sm", "transcript_codes": ["it", "it-IT"],                   "has_morphology": True,  "phrase_extractor": None},
    "pt": {"spacy_model": "pt_core_news_sm", "transcript_codes": ["pt", "pt-PT", "pt-BR"],          "has_morphology": True,  "phrase_extractor": None},
    "ru": {"spacy_model": "ru_core_news_sm", "transcript_codes": ["ru", "ru-RU", "ru-UA"],          "has_morphology": True,  "phrase_extractor": None},
    "ja": {"spacy_model": "ja_core_news_sm", "transcript_codes": ["ja", "ja-JP"],                   "has_morphology": False, "phrase_extractor": None},
    "ko": {"spacy_model": "ko_core_news_sm", "transcript_codes": ["ko", "ko-KR"],                   "has_morphology": False, "phrase_extractor": None},
}
# `phrase_extractor` is INFORMATIONAL (which languages have a phrase extractor) —
# not consumed by pipeline today; the runtime source of truth is
# phrase_finder._LANGUAGE_EXTRACTORS.


def _validate(languages: dict) -> None:
    """Fail fast on a malformed entry (e.g. a future typo'd key). Takes the dict
    explicitly so it's unit-testable with a bad config."""
    for code, cfg in languages.items():
        model = cfg.get("spacy_model")
        if not isinstance(model, str) or not model:
            raise ValueError(f"language {code!r}: spacy_model must be a non-empty string")
        codes = cfg.get("transcript_codes")
        if not isinstance(codes, list) or not codes or not all(isinstance(c, str) and c for c in codes):
            raise ValueError(f"language {code!r}: transcript_codes must be a non-empty list of strings")
        if not isinstance(cfg.get("has_morphology"), bool):
            raise ValueError(f"language {code!r}: has_morphology must be a bool")


_validate(LANGUAGES)

# Derived maps — identical shape AND ORDER to the old pipeline.py constants, so
# existing call sites + the profile scripts work unchanged (pipeline re-exports
# these as LANG_MODEL_MAP / LANG_TRANSCRIPT_CODES / NO_MORPH_LANGS).
MODEL_MAP: dict[str, str] = {code: cfg["spacy_model"] for code, cfg in LANGUAGES.items()}
TRANSCRIPT_CODES: dict[str, list[str]] = {
    code: list(cfg["transcript_codes"]) for code, cfg in LANGUAGES.items()
}
NO_MORPH_LANGS: set[str] = {code for code, cfg in LANGUAGES.items() if not cfg["has_morphology"]}


def get_supported_languages() -> list[str]:
    """Configured language codes, in priority order."""
    return list(LANGUAGES.keys())


def get_spacy_model_name(language: str) -> str | None:
    """spaCy model for `language`, or None if unsupported (matches the old
    `LANG_MODEL_MAP.get(language)`)."""
    cfg = LANGUAGES.get(language)
    return cfg["spacy_model"] if cfg else None


def get_transcript_codes(language: str) -> list[str]:
    """Transcript codes to try for `language`, or [] if unsupported."""
    cfg = LANGUAGES.get(language)
    return list(cfg["transcript_codes"]) if cfg else []


def has_morphology(language: str) -> bool:
    """Whether `language` has morphology features (False for ja/ko). Unknown
    languages default to True — matches the old `language not in NO_MORPH_LANGS`
    test, which treated an unknown language as having morphology."""
    cfg = LANGUAGES.get(language)
    return cfg["has_morphology"] if cfg else True
