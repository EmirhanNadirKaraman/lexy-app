"""
Thin async wrapper around phrase_finder's language-gated dispatcher.

phrase_finder.py lives in subtitle-scraper/ and loads the German spaCy model +
verb dictionary at module-import time. `extract_phrases(doc, language)` routes
German → German-specific logic, Spanish → the first-slice Spanish extractor
(#36); any other language returns [].

**Per-language parsing (#39 slice 2).** `_extract` parses each sentence with the
model that matches its language — German via phrase_finder's resident model,
others via `nlp_service`'s per-language cache. Before this fix the matcher
parsed *every* language with the German model, so Spanish chat matching was
unreliable. Languages without an extractor skip parsing entirely.

**Lemma overrides (#39).** `match_sentence_with_ids` loads `lemma_override`
rows (it has the DB pool) and threads the {observed→corrected} dict into the
sync extractor, so canonicals like `ducharse` come out right. The override-less
`/sentences/match` route (`match_sentence` with no pool) is a deliberate
exception — it's a stateless matcher utility, not the learning path.

`_pf` is a normal module reference; resources stay resident for the process
lifetime. Lazy-loading German is still deferred (trigram-index bootstrap is
intertwined with verb_blueprint_map).
"""
import asyncio
import logging
import sys
import threading
from pathlib import Path

import asyncpg

_SCRAPER_PATH = str(Path(__file__).resolve().parents[3] / "subtitle-scraper")

if _SCRAPER_PATH not in sys.path:
    sys.path.insert(0, _SCRAPER_PATH)

import phrase_finder as _pf

from . import nlp_service

logger = logging.getLogger(__name__)

# Serialises the first per-language model load. `_extract` runs in a thread
# pool (run_in_executor); without this, two concurrent first-time Spanish
# matches could both call spacy.load("es_core_news_sm") and pay the ~30s load
# twice. Cache hits are a fast dict lookup, so holding it is cheap.
_model_lock = threading.Lock()


def _model_for(language: str):
    """spaCy model for `language`, or None if unavailable.

    German reuses phrase_finder's already-resident model (avoids loading a
    second de_core_news_sm); other languages come from nlp_service's per-
    language cache. Fails OPEN — any load error (no mapping, model not
    installed, corrupt model) returns None so the matcher degrades to "no
    phrase extraction" rather than crashing chat.
    """
    if language == "de":
        return _pf.nlp
    try:
        with _model_lock:
            return nlp_service.NLPService.get_model(language)
    except Exception:  # noqa: BLE001 — fail open; loading must never crash matching
        logger.warning(
            "No spaCy model for language %r; skipping phrase extraction", language
        )
        return None


def _extract(sentence: str, language: str, overrides: dict | None = None) -> list[dict]:
    """nlp(sentence) → Doc → phrase extraction. Sync — run via executor.

    Parses with the LANGUAGE-appropriate model (#39 slice 2 fix): pre-fix this
    always used the German model regardless of `language`, so Spanish text was
    parsed by de_core_news_sm and matched unreliably. Languages without a
    registered extractor skip parsing entirely. `overrides` (#39) patches spaCy
    lemma errors (e.g. `duchaber`→`duchar`) before canonicals are built.
    """
    if language not in _pf._LANGUAGE_EXTRACTORS:
        return []  # no extractor for this language — skip the nlp() cost
    nlp = _model_for(language)
    if nlp is None:
        return []
    doc = nlp(sentence)
    return _pf.extract_phrases(doc, language, overrides)


async def match_sentence(
    sentence: str, language: str = "de", overrides: dict | None = None
) -> list[dict]:
    """Run nlp() + extract_phrases in a thread so the sync spaCy call
    doesn't block the event loop.

    `language` defaults to "de" for backward compatibility with the
    single-arg callers (POST /api/v1/sentences/match, existing tests).
    For non-German content pass the matching language; it is parsed with
    that language's model (#39 slice 2).

    `overrides` (#39) is an optional {observed_lemma: corrected_lemma} map. It's
    loaded async before this call (callers with a DB pool use
    `match_sentence_with_ids`, which loads it); a plain dict is then handed into
    the executor, so the sync path never touches the DB. `None` trusts spaCy —
    the path the override-less `/sentences/match` route takes by design.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _extract, sentence, language, overrides)


def get_blueprint_map() -> dict[str, str]:
    """Expose the verb blueprint dict loaded at module import time.

    Used by phrase_service.seed_from_blueprint_map() at application startup
    to populate phrase_table without re-reading the file from disk.

    German-only by design — there is no Spanish/French equivalent yet.
    """
    return _pf.verb_blueprint_map


async def _load_overrides_for(pool: asyncpg.Pool, language: str) -> dict:
    """Active context-free lemma overrides for `language` (#39) →
    {observed_lemma: corrected_lemma}.

    Loaded fresh per call (one tiny indexed query); slice 3's promotion flow
    will need to invalidate any future cache here. Fails OPEN: a missing table
    (un-migrated env) or a DB error returns {} so matching still runs (just
    without lemma correction). Not gated on language — German simply has no
    seeds today, so it's a no-op there.
    """
    try:
        rows = await pool.fetch(
            "SELECT observed_lemma, corrected_lemma FROM lemma_override "
            "WHERE language = $1 AND status = 'active' "
            "AND surface_form IS NULL AND pos IS NULL",
            language,
        )
    except asyncpg.UndefinedTableError:
        return {}  # migration 033 not applied yet — expected on un-migrated envs
    except asyncpg.PostgresError:
        logger.warning(
            "lemma_override lookup failed for %r; proceeding without overrides",
            language, exc_info=True,
        )
        return {}
    return {r["observed_lemma"]: r["corrected_lemma"] for r in rows}


async def match_sentence_with_ids(
    pool: asyncpg.Pool,
    sentence: str,
    language: str = "de",
) -> list[dict]:
    """Match a sentence and attach a phrase_id to each result where available.

    phrase_id is None when the canonical blueprint is not in phrase_table —
    for example, single nouns or verbs that matched via trigram fuzzy fallback
    to an unseeded entry.

    A single batch query looks up all canonical forms so there is at most one
    round-trip to the DB regardless of sentence length.

    `language` threads through to `match_sentence`, which now parses with that
    language's model (#39 slice 2). Lemma overrides (#39) are loaded here (we
    have the pool) and passed into the sync extractor via `match_sentence`.
    """
    overrides = await _load_overrides_for(pool, language)
    phrases = await match_sentence(sentence, language, overrides)
    if not phrases:
        return phrases

    unique_canonicals = list({p["dictionary_entry"] for p in phrases})
    rows = await pool.fetch(
        """
        SELECT phrase_id, canonical
        FROM phrase_table
        WHERE canonical = ANY($1::text[]) AND language = $2
        """,
        unique_canonicals, language,
    )
    canonical_to_id: dict[str, int] = {r["canonical"]: r["phrase_id"] for r in rows}

    return [
        {**p, "phrase_id": canonical_to_id.get(p["dictionary_entry"])}
        for p in phrases
    ]
