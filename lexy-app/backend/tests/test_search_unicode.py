"""
Unicode search — services/search_service.search (C-collation bug, 2026-07-28).

The corpus search path folded case in SQL: `w.word ILIKE $1 OR w.lemma ILIKE $1`
for words, and an `ILIKE`/word-boundary-regex pair against `phrase_blueprint`.
This database is `datcollate=C`, so Postgres folds ASCII only — `'Öl' ILIKE 'öl'`
is false — and searching *öl* returned nothing even though `Öl` was indexed
against real sentences. Both paths now resolve to ids in Python via
`normalize_key` and the SQL matches on `= ANY(...)`.

Scope note: `_suggest_words` (autocomplete) is deliberately NOT covered or
changed here. A Unicode-correct *prefix* match needs a normalized generated
column plus an index — a migration, tracked as its own task. The last test in
this file pins that it is still the unfixed version, so nobody assumes
otherwise.

Every fixture builds its own video/sentence/word rows and reaps them, because
`video`, `sentence` and `word_table` are global (docs/TESTS.md).
"""
import uuid

import pytest
from httpx import AsyncClient

from backend.services import search_service
from ._auth_helper import register_and_login
from ._email_helper import make_test_email

def _letters(n: int = 12) -> str:
    """Letters-only unique suffix — digits would not round-trip a text search."""
    return "".join(chr(ord("a") + int(c, 16)) for c in uuid.uuid4().hex[:n])


@pytest.fixture
async def corpus(db_pool):
    """Build video + sentence + word_table rows wired through word_to_sentence.

    Returns a factory: `await make(word, lemma=..., language=...)` → word_id.
    Everything created is reaped afterwards.
    """
    videos: list[str] = []
    words: list[int] = []

    async def _make(word: str, *, lemma: str | None = None, language: str = "de") -> int:
        vid = f"zzsrch{_letters(10)}"
        await db_pool.execute(
            "INSERT INTO video (video_id, title, thumbnail_url, duration, language, "
            "dialect, category, transcript_source) "
            "VALUES ($1, 'Zz Search Fixture', '', 60, $2, '', 'other', 'test')",
            vid, language,
        )
        videos.append(vid)
        sentence_id = await db_pool.fetchval(
            "INSERT INTO sentence (video_id, start_time, duration, content, tokens) "
            "VALUES ($1, 1.0, 2.0, $2, '{}') RETURNING sentence_id",
            vid, f"Ein Satz mit {word} darin.",
        )
        word_id = await db_pool.fetchval(
            "INSERT INTO word_table (word, language, pos, tag, lemma) "
            "VALUES ($1, $2, '', '', $3) RETURNING word_id",
            word, language, lemma or word,
        )
        words.append(word_id)
        await db_pool.execute(
            "INSERT INTO word_to_sentence (word_id, sentence_id) VALUES ($1, $2)",
            word_id, sentence_id,
        )
        return word_id

    yield _make

    if words:
        await db_pool.execute(
            "DELETE FROM word_to_sentence WHERE word_id = ANY($1::int[])", words)
        await db_pool.execute(
            "DELETE FROM word_table WHERE word_id = ANY($1::int[])", words)
    if videos:
        await db_pool.execute(
            "DELETE FROM sentence WHERE video_id = ANY($1::text[])", videos)
        await db_pool.execute(
            "DELETE FROM video WHERE video_id = ANY($1::text[])", videos)


async def _search(db_pool, query: str, language: str | None = "de") -> list[dict]:
    results, _total = await search_service.search(db_pool, query, language, 50, 0)
    return results


def _contents(results: list[dict]) -> str:
    return " || ".join(r["content"] for r in results)


# ---------------------------------------------------------------------------
# Word search — the ILIKE path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("initial", ["Ö", "Ü", "Ä"])
async def test_search_finds_umlaut_word_from_lowercase_query(db_pool, corpus, initial):
    """`öl` must find the sentence indexed under `Öl`."""
    word = f"{initial}zzs{_letters()}"
    await corpus(word)

    results = await _search(db_pool, word.lower())

    assert results, f"searching {word.lower()!r} found nothing"
    assert word in _contents(results)


async def test_search_finds_umlaut_word_from_exact_query(db_pool, corpus):
    word = f"Özzs{_letters()}"
    await corpus(word)

    results = await _search(db_pool, word)

    assert results
    assert word in _contents(results)


async def test_search_finds_umlaut_word_from_uppercase_query(db_pool, corpus):
    word = f"Üzzs{_letters()}"
    await corpus(word)

    results = await _search(db_pool, word.upper())

    assert results


async def test_search_ascii_still_works(db_pool, corpus):
    """Regression guard — the ASCII path was never broken."""
    word = f"Zzs{_letters()}"
    await corpus(word)

    results = await _search(db_pool, word.lower())

    assert results
    assert word in _contents(results)


async def test_search_matches_on_lemma_and_reports_match_type(db_pool, corpus):
    """`match_type` still distinguishes a surface hit from a lemma-only hit."""
    surface = f"Bzzs{_letters()}"
    lemma = f"Özzs{_letters()}"
    await corpus(surface, lemma=lemma)

    by_surface = await _search(db_pool, surface.lower())
    by_lemma = await _search(db_pool, lemma.lower())

    assert by_surface and by_surface[0]["match_type"] == "word"
    assert by_lemma, "lemma half of the predicate must fold too"
    assert by_lemma[0]["match_type"] == "lemma"


@pytest.mark.parametrize("sharp_s,double_s", [("straße", "strasse"), ("schließen", "schliessen")])
async def test_search_does_not_merge_sharp_s_with_double_s(
    db_pool, corpus, sharp_s, double_s,
):
    """`casefold()` maps ß to ss and would return the wrong sentence.

    `normalize_key` uses `lower()`, so these stay distinct.
    """
    uniq = _letters()
    sharp, double = f"{sharp_s}{uniq}", f"{double_s}{uniq}"
    await corpus(sharp)
    await corpus(double)

    sharp_hits = _contents(await _search(db_pool, sharp))
    double_hits = _contents(await _search(db_pool, double))

    assert sharp in sharp_hits and double not in sharp_hits
    assert double in double_hits and sharp not in double_hits


async def test_search_is_language_scoped(db_pool, corpus):
    """Scoping is on the VIDEO's language, exactly as before."""
    word = f"Äzzs{_letters()}"
    await corpus(word, language="es")

    assert await _search(db_pool, word.lower(), "de") == []
    assert await _search(db_pool, word.lower(), "es")


async def test_search_without_language_spans_languages(db_pool, corpus):
    word = f"Özzs{_letters()}"
    await corpus(word, language="es")

    assert await _search(db_pool, word.lower(), None)


async def test_search_returns_every_matching_video_not_just_one(db_pool, corpus):
    """Several rows sharing a normalized surface all surface, as before.

    The old query returned one row per video for every ILIKE hit; resolving to
    ids first must not collapse them. `DISTINCT ON (v.video_id)` still applies,
    and each fixture word gets its own video.
    """
    word = f"Özzs{_letters()}"
    await corpus(word)
    await corpus(word.lower())

    results = await _search(db_pool, word.upper())

    assert len({r["video_id"] for r in results}) == 2


async def test_search_unknown_word_returns_nothing(db_pool):
    assert await _search(db_pool, f"zzznotindexed{_letters()}") == []


# ---------------------------------------------------------------------------
# Blueprint search — the regex / substring path
# ---------------------------------------------------------------------------


@pytest.fixture
async def blueprint_corpus(db_pool):
    """A video + sentence linked to a phrase_blueprint row."""
    videos: list[str] = []
    blueprints: list[int] = []

    async def _make(blueprint: str, *, language: str = "de") -> int:
        vid = f"zzsrchbp{_letters(8)}"
        await db_pool.execute(
            "INSERT INTO video (video_id, title, thumbnail_url, duration, language, "
            "dialect, category, transcript_source) "
            "VALUES ($1, 'Zz Blueprint Fixture', '', 60, $2, '', 'other', 'test')",
            vid, language,
        )
        videos.append(vid)
        sentence_id = await db_pool.fetchval(
            "INSERT INTO sentence (video_id, start_time, duration, content, tokens) "
            "VALUES ($1, 1.0, 2.0, $2, '{}') RETURNING sentence_id",
            vid, f"Satz fuer {blueprint}.",
        )
        bp_id = await db_pool.fetchval(
            "INSERT INTO phrase_blueprint (lookup_key, blueprint) VALUES ($1, $2) "
            "RETURNING blueprint_id",
            blueprint.lower(), blueprint,
        )
        blueprints.append(bp_id)
        await db_pool.execute(
            "INSERT INTO sentence_to_phrase "
            "(sentence_id, blueprint_id, surface_form, logic, match_type, indices) "
            "VALUES ($1, $2, $3, 'test', 'exact', '{}')",
            sentence_id, bp_id, blueprint,
        )
        return bp_id

    yield _make

    if blueprints:
        await db_pool.execute(
            "DELETE FROM sentence_to_phrase WHERE blueprint_id = ANY($1::int[])", blueprints)
    if videos:
        await db_pool.execute("DELETE FROM sentence WHERE video_id = ANY($1::text[])", videos)
        await db_pool.execute("DELETE FROM video WHERE video_id = ANY($1::text[])", videos)
    if blueprints:
        await db_pool.execute(
            "DELETE FROM phrase_blueprint WHERE blueprint_id = ANY($1::int[])", blueprints)


async def test_search_finds_umlaut_blueprint_from_lowercase_query(db_pool, blueprint_corpus):
    """6,921 of 43,549 blueprints carry non-ASCII text."""
    token = f"Özzb{_letters()}"
    await blueprint_corpus(f"jdm {token} geben")

    results = await _search(db_pool, token.lower())

    assert results, "umlaut blueprint token did not match"


async def test_blueprint_single_word_search_still_requires_a_whole_word(
    db_pool, blueprint_corpus,
):
    """`ist` must not match inside `Tadschikistan` — the old `\\m…\\M` contract."""
    inner = _letters(6)
    await blueprint_corpus(f"zzprefix{inner}zzsuffix geben")

    assert await _search(db_pool, inner) == []


async def test_blueprint_multiword_search_matches_a_substring(db_pool, blueprint_corpus):
    """Multi-word queries kept the looser substring behaviour."""
    token = f"özzb{_letters()}"
    await blueprint_corpus(f"jdm {token} etw geben")

    results = await _search(db_pool, f"{token} etw")

    assert results


async def test_blueprint_search_escapes_regex_metacharacters(db_pool, blueprint_corpus):
    """A query is data, not a pattern.

    The old single-word blueprint predicate concatenated raw input into a
    regex (`pb.blueprint ~* ('\\m' || $1 || '\\M')`), so `.*` matched anything
    and a crafted query was evaluated against 43k rows. `re.escape` in
    `_resolve_blueprint_ids` makes the term literal.
    """
    token = f"Özzb{_letters()}"
    bp_id = await blueprint_corpus(f"jdm {token} geben")

    # Asserted against the resolver, not the whole search: the real corpus
    # contains punctuation tokens in `word_table`, so a query like "(" has
    # legitimate word hits that say nothing about regex handling.
    for metachar_query in (".*", ".+", "(", "[a-z]+", "Ö.*geben"):
        assert bp_id not in await search_service._resolve_blueprint_ids(
            db_pool, metachar_query, whole_word=True,
        ), f"{metachar_query!r} was evaluated as a pattern, not a literal"

    # Control: the literal token still matches, so the escaping did not simply
    # break matching.
    assert bp_id in await search_service._resolve_blueprint_ids(
        db_pool, token.lower(), whole_word=True,
    )


# ---------------------------------------------------------------------------
# Autocomplete is deliberately NOT fixed here
# ---------------------------------------------------------------------------


async def test_suggest_words_is_still_the_unfixed_sql_version(db_pool, corpus):
    """Pins the deferral so it is a decision, not an oversight.

    `_suggest_words` still prefix-matches with SQL `lower(word) LIKE lower($2)`,
    which folds ASCII only here — so a lowercase umlaut prefix finds nothing. A
    Unicode-correct prefix match needs a normalized generated column plus an
    index (a migration), tracked separately in docs/TODO.md.

    When that lands, this test SHOULD fail. Flip it to assert the suggestion is
    returned rather than deleting it.
    """
    word = f"Özzs{_letters()}"
    await corpus(word)

    hits = await search_service._suggest_words(db_pool, word.lower()[:6], "de", 10)

    assert hits == [], (
        "autocomplete now folds Unicode — invert this assertion and close the "
        "deferral in docs/TODO.md"
    )


async def test_suggest_words_still_works_for_ascii(db_pool, corpus):
    word = f"Zzs{_letters()}"
    await corpus(word)

    hits = await search_service._suggest_words(db_pool, word.lower()[:8], "de", 10)

    assert any(h["word"] == word for h in hits)


# ---------------------------------------------------------------------------
# Route-level smoke: the response shape is unchanged
# ---------------------------------------------------------------------------


async def test_search_route_shape_unchanged(client: AsyncClient, db_pool, corpus):
    word = f"Özzs{_letters()}"
    await corpus(word)
    headers, _uid = await register_and_login(client, db_pool, make_test_email())

    resp = await client.get(
        "/api/search", params={"q": word.lower(), "language": "de"}, headers=headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"query", "results", "total"}
    assert body["total"] >= 1
    assert set(body["results"][0]) >= {
        "video_id", "title", "thumbnail_url", "language",
        "start_time", "start_time_int", "content", "surface_form", "match_type",
    }
