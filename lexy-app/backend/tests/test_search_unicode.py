"""
Unicode search — services/search_service.search (C-collation bug, 2026-07-28).

The corpus search path folded case in SQL: `w.word ILIKE $1 OR w.lemma ILIKE $1`
for words, and an `ILIKE`/word-boundary-regex pair against `phrase_blueprint`.
This database is `datcollate=C`, so Postgres folds ASCII only — `'Öl' ILIKE 'öl'`
is false — and searching *öl* returned nothing even though `Öl` was indexed
against real sentences. Both paths now resolve to ids in Python via
`normalize_key` and the SQL matches on `= ANY(...)`.

Autocomplete (`_suggest_words` / `_suggest_phrases`) was deferred when this
file was written and fixed on 2026-07-28 by migration 036, which adds generated
`word_table.word_norm` and `phrase_blueprint.lookup_key_norm` columns. It could
not use the fold-in-Python approach the rest of the app uses: `/api/suggest`
fires on every keystroke and needs an *indexed prefix*, so the normalization is
persisted instead. The deferral tests at the end of this file were inverted
rather than deleted.

Every fixture builds its own video/sentence/word rows and reaps them, because
`video`, `sentence` and `word_table` are global (docs/TESTS.md).
"""
import uuid

import pytest
from httpx import AsyncClient

from backend.services import search_service
from backend.services.text_norm import normalize_key
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

    async def _make(word: str, *, lemma: str | None = None, language: str = "de",
                    frequency: int = 0) -> int:
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
            "INSERT INTO word_table (word, language, pos, tag, lemma, frequency) "
            "VALUES ($1, $2, '', '', $3, $4) RETURNING word_id",
            word, language, lemma or word, frequency,
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


async def test_suggest_words_finds_umlaut_word_from_lowercase_prefix(db_pool, corpus):
    """Was `test_suggest_words_is_still_the_unfixed_sql_version` — now inverted.

    That test asserted autocomplete returned nothing for a lowercase umlaut
    prefix, and instructed whoever fixed it to flip the assertion rather than
    delete the coverage. This is that flip: migration 036's `word_norm` column
    makes the prefix match Unicode-correct.
    """
    word = f"Özzs{_letters()}"
    await corpus(word)

    hits = await search_service._suggest_words(db_pool, word.lower()[:6], "de", 10)

    assert any(h["word"] == word for h in hits), "lowercase umlaut prefix found nothing"


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


# ---------------------------------------------------------------------------
# Autocomplete — migration 036 (`word_norm` / `lookup_key_norm`)
#
# `/api/suggest` is the one lookup that could not be fixed by folding in Python
# and filtering: it fires per keystroke and needs an indexed prefix. So the
# normalization is persisted as a generated column whose SQL expression
# reproduces `normalize_key`, and the query matches a folded prefix against it.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("prefix_of", ["lower", "exact", "upper"])
async def test_suggest_words_finds_umlaut_word_from_any_casing(db_pool, corpus, prefix_of):
    word = f"Özzs{_letters()}"
    await corpus(word)
    typed = {"lower": word.lower(), "exact": word, "upper": word.upper()}[prefix_of]

    hits = await search_service._suggest_words(db_pool, typed[:6], "de", 10)

    assert any(h["word"] == word for h in hits), f"{typed[:6]!r} found nothing"


@pytest.mark.parametrize("initial", ["Ö", "Ü", "Ä"])
async def test_suggest_words_umlaut_initial_prefix(db_pool, corpus, initial):
    """`üb` → `Übung`, `änd` → `Änderung` — the prefix itself carries the umlaut."""
    word = f"{initial}zzs{_letters()}"
    await corpus(word)

    hits = await search_service._suggest_words(db_pool, word.lower()[:4], "de", 10)

    assert any(h["word"] == word for h in hits)


async def test_suggest_words_is_language_scoped(db_pool, corpus):
    word = f"Özzs{_letters()}"
    await corpus(word, language="es")

    assert await search_service._suggest_words(db_pool, word.lower()[:6], "de", 10) == []
    assert await search_service._suggest_words(db_pool, word.lower()[:6], "es", 10)


async def test_suggest_words_honours_limit(db_pool, corpus):
    stem = f"Özzlim{_letters(6)}"
    for i in range(5):
        await corpus(f"{stem}{'abcde'[i]}")

    assert len(await search_service._suggest_words(db_pool, stem.lower(), "de", 3)) == 3
    assert len(await search_service._suggest_words(db_pool, stem.lower(), "de", 10)) == 5


async def test_suggest_words_ranks_by_frequency_desc(db_pool, corpus):
    stem = f"Özzrank{_letters(6)}"
    await corpus(f"{stem}a", frequency=5)
    await corpus(f"{stem}b", frequency=99)
    await corpus(f"{stem}c", frequency=50)

    hits = await search_service._suggest_words(db_pool, stem.lower(), "de", 10)

    assert [h["word"] for h in hits] == [f"{stem}b", f"{stem}c", f"{stem}a"]
    assert hits[0]["score"] == 99.0


async def test_suggest_words_collapses_case_variants_highest_frequency_wins(db_pool, corpus):
    """417 German keys have several rows; each is now ONE suggestion.

    The old `DISTINCT ON (lower(word))` could not collapse `Folgen`/`folgen`
    because SQL `lower()` left the umlaut/casing pair distinct under the C
    locale. This is a visible change to the dropdown, and intended.
    """
    word = f"Özzdup{_letters(6)}"
    await corpus(word, frequency=3)
    await corpus(word.lower(), frequency=42)

    hits = await search_service._suggest_words(db_pool, word.lower(), "de", 10)

    matching = [h for h in hits if h["word"].lower() == word.lower()]
    assert len(matching) == 1, "case variants must collapse to one suggestion"
    assert matching[0]["word"] == word.lower(), "highest-frequency spelling wins"
    assert matching[0]["score"] == 42.0


@pytest.mark.parametrize("sharp_s,double_s", [("straße", "strasse"), ("schließen", "schliessen")])
async def test_suggest_words_does_not_merge_sharp_s_with_double_s(
    db_pool, corpus, sharp_s, double_s,
):
    """ICU `lower()` leaves ß alone; `casefold()` would map it to ss and merge.

    Both spellings must remain separate suggestions, and each prefix must find
    only its own word.
    """
    uniq = _letters()
    sharp, double = f"{sharp_s}{uniq}", f"{double_s}{uniq}"
    await corpus(sharp)
    await corpus(double)

    sharp_hits = [h["word"] for h in await search_service._suggest_words(db_pool, sharp, "de", 10)]
    double_hits = [h["word"] for h in await search_service._suggest_words(db_pool, double, "de", 10)]

    assert sharp_hits == [sharp]
    assert double_hits == [double]


@pytest.mark.parametrize("wildcard", ["%", "_"])
async def test_suggest_words_treats_like_wildcards_literally(db_pool, corpus, wildcard):
    """A typed `%` used to match the entire catalog; `_` any single character.

    Asserted as "does not match an unrelated word", not "returns nothing": the
    real corpus tokenizes punctuation, so `%` is itself a `word_table` row
    (frequency 5) and legitimately matches *itself*. Asserting emptiness would
    be testing the corpus, not the escaping.
    """
    unrelated = f"Özzwild{_letters()}"
    await corpus(unrelated, frequency=999)

    hits = await search_service._suggest_words(db_pool, wildcard, "de", 10)

    assert all(h["word"] != unrelated for h in hits), \
        f"{wildcard!r} expanded as a wildcard instead of matching literally"
    assert all(h["word"].lower().startswith(wildcard) for h in hits), \
        "every hit must literally start with the typed character"


async def test_suggest_words_finds_a_word_containing_a_literal_wildcard(db_pool, corpus):
    """Escaping must not break matching a surface that really contains `_`."""
    word = f"Özz_wild{_letters(6)}"
    await corpus(word)

    hits = await search_service._suggest_words(db_pool, word.lower(), "de", 10)

    assert any(h["word"] == word for h in hits)


async def test_suggest_words_blank_query_returns_nothing(db_pool):
    assert await search_service._suggest_words(db_pool, "   ", "de", 10) == []


# --- the generated column itself -------------------------------------------


async def test_migration_populated_word_norm_for_existing_rows(db_pool):
    """036 backfills via the ADD COLUMN rewrite — no manual UPDATE."""
    total, filled = await db_pool.fetchrow(
        "SELECT count(*) AS total, count(word_norm) AS filled FROM word_table",
    )
    assert total > 0
    assert filled == total, "every existing row must have word_norm"


async def test_word_norm_matches_normalize_key_on_real_rows(db_pool):
    """The whole design rests on the SQL expression reproducing normalize_key."""
    rows = await db_pool.fetch(
        "SELECT word, word_norm FROM word_table WHERE word ~ '[^\\x01-\\x7F]' LIMIT 500",
    )
    if not rows:
        pytest.skip("no non-ASCII rows in word_table")
    mismatched = [(r["word"], r["word_norm"]) for r in rows
                  if r["word_norm"] != normalize_key(r["word"])]
    assert mismatched == []


async def test_new_word_gets_word_norm_without_application_code(db_pool, tracked_words):
    """GENERATED means no insert path can forget it — the reason it beats a
    column the application maintains (three code paths insert into word_table,
    one of them the scraper in a separate process)."""
    word = f"Özzgen{_letters()}"
    wid = await db_pool.fetchval(
        "INSERT INTO word_table (word, language, pos, tag, lemma) "
        "VALUES ($1, 'de', '', '', $1) RETURNING word_id",
        word,
    )
    tracked_words.append(wid)

    stored = await db_pool.fetchval("SELECT word_norm FROM word_table WHERE word_id = $1", wid)

    assert stored == normalize_key(word) == word.lower()


async def test_word_norm_is_generated_and_cannot_be_written(db_pool, tracked_words):
    import asyncpg as _asyncpg
    with pytest.raises(_asyncpg.PostgresError):
        await db_pool.execute(
            "INSERT INTO word_table (word, language, pos, tag, lemma, word_norm) "
            "VALUES ($1, 'de', '', '', $1, 'nope')",
            f"Özzro{_letters()}",
        )


# --- _suggest_phrases -------------------------------------------------------


@pytest.mark.parametrize(
    "metachar", [".*", ".+", "(", "[a-z]+", "\\", "Ö.*geben"],
)
async def test_suggest_phrases_treats_regex_metacharacters_literally(
    db_pool, blueprint_corpus, metachar,
):
    """SECURITY.md S20 — the query is data, not a pattern.

    The old predicate concatenated the raw term into a Postgres ARE, so `.*`
    matched all 43,549 blueprints and a backtracking pattern would run against
    every one of them inside a request.
    """
    token = f"özzp{_letters()}"
    await blueprint_corpus(f"jdm {token} geben")

    hits = await search_service._suggest_phrases(db_pool, metachar, 10)

    assert not any(token in h["word"] for h in hits), \
        f"{metachar!r} was evaluated as a pattern"


async def test_suggest_phrases_finds_umlaut_blueprint_from_lowercase(
    db_pool, blueprint_corpus,
):
    """The blueprint carries original casing; the query is lowercased."""
    token = f"Özzp{_letters()}"
    await blueprint_corpus(f"jdm {token} geben")

    hits = await search_service._suggest_phrases(db_pool, token.lower(), 10)

    assert any(token in h["word"] for h in hits)


async def test_suggest_phrases_literal_token_still_matches(db_pool, blueprint_corpus):
    """Control: escaping must not simply break matching."""
    token = f"özzp{_letters()}"
    await blueprint_corpus(f"jdm {token} geben")

    assert any(token in h["word"] for h in await search_service._suggest_phrases(db_pool, token, 10))


async def test_suggest_phrases_blank_query_returns_nothing(db_pool):
    assert await search_service._suggest_phrases(db_pool, "  ", 10) == []
