"""
TODO #38 / route B — frequency-ranked, language-aware autocomplete.

search_service.suggest now does a case-insensitive PREFIX match over
word_table scoped to the active language, ranked by the precomputed
`frequency` column (migration 032). Replaces the old phrase_blueprint-only,
German-only, language-ignoring behaviour that returned [] for Spanish.

xdist isolation (round 2): each fixture builds its rows via the OWNED-word
machinery (`make_word`, reaped by id through `tracked_words`) under a
per-test-unique `zzqx<uuid>` prefix, and yields the surfaces. There is NO
shared `zzqx%` namespace and NO prefix `DELETE` teardown — so a concurrent
worker's teardown can't reap this test's rows mid-run (the original flake: a
sibling fixture's `DELETE ... WHERE word LIKE 'zzqx%'` deleted another worker's
lowercase rows while leaving the capital casing variant, producing the observed
`['zzqxbajo', 'Zzqxalto']` result in test_suggest_respects_limit).
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from backend.services import search_service


@pytest.fixture
async def seeded_words(make_word):
    """Owned, frequency-ranked es + en words sharing a per-test-unique prefix.

    Yields a namespace of the generated surfaces so assertions never hardcode
    `zzqxalto` etc. Rows are reaped by id via `make_word`/`tracked_words`.
    """
    p = f"zzqx{uuid.uuid4().hex[:8]}"
    alto = f"{p}alto"
    alta = f"{p}alta"
    alto_cap = alto.capitalize()   # casing variant of `alto`; lower() collapses it
    bajo = f"{p}bajo"
    await make_word("es", word=alto,     frequency=100)
    await make_word("es", word=alta,     frequency=40)
    await make_word("es", word=alto_cap, frequency=5)    # collapses into `alto`
    await make_word("es", word=bajo,     frequency=70)   # different prefix branch
    await make_word("en", word=alto,     frequency=999)  # same surface, other language
    return SimpleNamespace(prefix=p, alto=alto, alta=alta, alto_cap=alto_cap, bajo=bajo)


async def test_suggest_returns_frequency_ranked_words(db_pool, seeded_words):
    n = seeded_words
    out = await search_service.suggest(db_pool, f"{n.prefix}a", "es")
    words = [r["word"] for r in out]
    # alto (100) before alta (40); bajo excluded (prefix '<p>a').
    assert words[:2] == [n.alto, n.alta], words
    assert all(r["type"] == "word" for r in out)
    assert out[0]["score"] == 100.0


async def test_suggest_collapses_casing_to_highest_frequency(db_pool, seeded_words):
    n = seeded_words
    out = await search_service.suggest(db_pool, n.alto, "es")
    # Only one 'alto' entry, the freq-100 lowercase casing — not the freq-5 cap.
    matches = [r for r in out if r["word"].lower() == n.alto.lower()]
    assert len(matches) == 1
    assert matches[0]["word"] == n.alto
    assert matches[0]["score"] == 100.0


async def test_suggest_is_language_scoped(db_pool, seeded_words):
    """The en row (freq 999) must NOT appear in es suggestions even though it
    has the highest frequency overall."""
    n = seeded_words
    es = await search_service.suggest(db_pool, n.alto, "es")
    assert all(r["score"] != 999.0 for r in es), "English row leaked into es"

    en = await search_service.suggest(db_pool, n.alto, "en")
    assert en and en[0]["score"] == 999.0


async def test_suggest_empty_query_returns_empty(db_pool):
    assert await search_service.suggest(db_pool, "", "es") == []
    assert await search_service.suggest(db_pool, "   ", "es") == []


async def test_suggest_no_language_returns_empty(db_pool, seeded_words):
    """Without a language we don't scan the whole multi-language table."""
    assert await search_service.suggest(db_pool, seeded_words.prefix, None) == []


async def test_suggest_respects_limit(db_pool, seeded_words):
    n = seeded_words
    out = await search_service.suggest(db_pool, n.prefix, "es", limit=2)
    assert len(out) == 2
    # Highest-frequency first: alto (100), bajo (70).
    assert [r["word"] for r in out] == [n.alto, n.bajo]


# ---------------------------------------------------------------------------
# kind = words | phrases | both (user-selected via SearchBar)
# ---------------------------------------------------------------------------

@pytest.fixture
async def seeded_de(make_word, db_pool):
    """Owned German word + a uniquely-keyed phrase_blueprint row.

    The word is reaped by id via `make_word`; the blueprint row is deleted by
    its exact unique `lookup_key` (no shared-prefix DELETE).
    """
    p = f"zzqx{uuid.uuid4().hex[:8]}"
    verb = f"{p}geben"
    blueprint = f"{verb} jdm. etw."
    await make_word("de", word=verb, pos="VERB", tag="VERB", frequency=80)
    await db_pool.execute(
        "INSERT INTO phrase_blueprint (lookup_key, blueprint) VALUES ($1, $2)",
        verb, blueprint,
    )
    try:
        yield SimpleNamespace(prefix=p, verb=verb, blueprint=blueprint)
    finally:
        await db_pool.execute("DELETE FROM phrase_blueprint WHERE lookup_key = $1", verb)


async def test_suggest_kind_words_excludes_phrases(db_pool, seeded_de):
    n = seeded_de
    out = await search_service.suggest(db_pool, f"{n.prefix}geb", "de", kind="words")
    assert out and all(r["type"] == "word" for r in out)
    assert any(r["word"] == n.verb for r in out)


async def test_suggest_kind_phrases_returns_only_phrases(db_pool, seeded_de):
    # phrase_blueprint matching is whole-word (\m…\M), not prefix, so query
    # the full word — this is the pre-existing German phrase-suggest behaviour.
    n = seeded_de
    out = await search_service.suggest(db_pool, n.verb, "de", kind="phrases")
    assert out and all(r["type"] == "phrase" for r in out)
    assert any("jdm. etw." in r["word"] for r in out)


async def test_suggest_kind_both_includes_each_type(db_pool, seeded_de):
    n = seeded_de
    out = await search_service.suggest(db_pool, n.verb, "de", kind="both")
    types = {r["type"] for r in out}
    assert "word" in types and "phrase" in types
    # phrases lead in 'both'
    assert out[0]["type"] == "phrase"


async def test_suggest_phrases_empty_for_non_phrase_language(db_pool, seeded_words):
    """Spanish has no phrase source — kind='phrases' yields nothing,
    kind='both' degrades to words only."""
    n = seeded_words
    assert await search_service.suggest(db_pool, n.prefix, "es", kind="phrases") == []
    both = await search_service.suggest(db_pool, f"{n.prefix}a", "es", kind="both")
    assert both and all(r["type"] == "word" for r in both)


async def test_suggest_invalid_kind_falls_back_to_words(db_pool, seeded_words):
    n = seeded_words
    out = await search_service.suggest(db_pool, f"{n.prefix}a", "es", kind="bogus")
    assert out and all(r["type"] == "word" for r in out)
