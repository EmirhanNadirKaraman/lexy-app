import math
import re

import asyncpg

from .text_norm import normalize_key

# Exact case-insensitive word/lemma match, by pre-resolved word_id.
#
# `$1` is every matching word_id, `$3` the subset that matched on the SURFACE
# (the rest matched only on `lemma`), which is what `match_type` reports.
# Resolution happens in Python — see `_resolve_word_ids` for why a SQL-side
# case-insensitive predicate cannot do it here.
WORD_QUERY = """
    SELECT DISTINCT ON (v.video_id)
        v.video_id, v.title, v.thumbnail_url, v.language,
        s.sentence_id, s.start_time, s.content,
        NULL::text AS surface_form,
        CASE WHEN w.word_id = ANY($3::int[]) THEN 'word' ELSE 'lemma' END AS match_type
    FROM word_table w
    JOIN word_to_sentence wts ON wts.word_id = w.word_id
    JOIN sentence s           ON s.sentence_id = wts.sentence_id
    JOIN video v              ON v.video_id = s.video_id
    WHERE w.word_id = ANY($1::int[])
      AND ($2::text IS NULL OR v.language = $2)
    ORDER BY v.video_id, s.start_time
"""

# Blueprint hits, by pre-resolved blueprint_id.
#
# Replaces two near-identical queries: a substring match for multi-word
# searches and a word-boundary regex for single-word ones (so "ist" does not
# match inside "Tadschikistan"). Both folded case in SQL and so missed umlauts;
# the distinction now lives in `_resolve_blueprint_ids(whole_word=...)`, which
# also escapes the term — the old word-boundary form interpolated raw user
# input into a regex.
PHRASE_QUERY = """
    SELECT DISTINCT ON (v.video_id)
        v.video_id, v.title, v.thumbnail_url, v.language,
        s.sentence_id, s.start_time, s.content,
        stp.surface_form,
        stp.match_type
    FROM phrase_blueprint pb
    JOIN sentence_to_phrase stp ON stp.blueprint_id = pb.blueprint_id
    JOIN sentence s             ON s.sentence_id = stp.sentence_id
    JOIN video v                ON v.video_id = s.video_id
    WHERE pb.blueprint_id = ANY($1::int[])
      AND ($2::text IS NULL OR v.language = $2)
    ORDER BY v.video_id, s.start_time
"""


async def _resolve_word_ids(
    pool: asyncpg.Pool, term: str,
) -> tuple[list[int], list[int]]:
    """`(all matching word_ids, the subset that matched on the surface)`.

    Replaces a SQL-side case-insensitive match on `word` OR `lemma`. That
    predicate folded case in SQL, and this database runs under the C locale
    where Postgres folds ASCII only — `'Öl'` and `'öl'` never compared equal.
    Searching *öl* returned nothing even though `Öl` was indexed against real
    sentences. Making both sides SQL would not help: neither side folds. The
    fold happens in Python via `text_norm.normalize_key`; see
    `services/text_norm.py`.

    `word_table` is fetched unscoped because the original predicate was too —
    `WORD_QUERY` scopes on the *video's* language, not the word's. ~39k short
    rows, measured at ~12 ms, on an explicit user action.

    Surface matches are reported separately so `match_type` keeps meaning what
    it did: `'word'` when the typed text equals the surface, `'lemma'` when it
    only equals the lemma.
    """
    key = normalize_key(term)
    if not key:
        return [], []

    rows = await pool.fetch("SELECT word_id, word, lemma FROM word_table")
    surface: list[int] = []
    lemma_only: list[int] = []
    for r in rows:
        if normalize_key(r["word"]) == key:
            surface.append(r["word_id"])
        elif normalize_key(r["lemma"] or "") == key:
            lemma_only.append(r["word_id"])
    return sorted(surface + lemma_only), sorted(surface)


async def _resolve_blueprint_ids(
    pool: asyncpg.Pool, query: str, *, whole_word: bool,
) -> list[int]:
    """Blueprint ids whose text contains `query`, case-insensitively.

    `whole_word=True` reproduces the old word-boundary regex used for
    single-word searches, so *ist* does not match inside *Tadschikistan*;
    `False` reproduces the old substring behaviour for multi-word searches.

    Both used to fold in SQL and so missed every umlaut-bearing blueprint —
    6,921 of 43,549 rows carry non-ASCII text. Folding is now Python-side.

    Escaping the term is a second fix: the old word-boundary predicate
    concatenated raw user input into a regex, so a crafted query was evaluated
    as a pattern against 43k rows. `re.escape` makes that impossible.
    """
    key = normalize_key(query)
    if not key:
        return []

    rows = await pool.fetch("SELECT blueprint_id, blueprint FROM phrase_blueprint")
    if whole_word:
        pattern = re.compile(rf"(?<!\w){re.escape(key)}(?!\w)")
        return sorted(
            r["blueprint_id"] for r in rows
            if pattern.search(normalize_key(r["blueprint"] or ""))
        )
    return sorted(
        r["blueprint_id"] for r in rows
        if key in normalize_key(r["blueprint"] or "")
    )


SIMILARITY_THRESHOLD = 0.3


def _build_multi_word_sentence_query(terms: list[str], language: str | None) -> tuple[str, list]:
    """Find sentences containing ALL search terms via trigram matching."""
    n = len(terms)
    params: list = list(terms)

    union_parts = [
        f"SELECT {i + 1} AS term_idx, word_id FROM word_table "
        f"WHERE similarity(word, ${i + 1}) > {SIMILARITY_THRESHOLD} "
        f"OR similarity(lemma, ${i + 1}) > {SIMILARITY_THRESHOLD}"
        for i in range(n)
    ]

    n_p = n + 1
    lang_p = n + 2
    params.extend([n, language])

    query = f"""
        WITH matched_words AS (
            {" UNION ALL ".join(union_parts)}
        ),
        sentence_matches AS (
            SELECT wts.sentence_id
            FROM word_to_sentence wts
            JOIN matched_words mw ON mw.word_id = wts.word_id
            GROUP BY wts.sentence_id
            HAVING COUNT(DISTINCT mw.term_idx) = ${n_p}
        )
        SELECT DISTINCT ON (v.video_id)
               v.video_id, v.title, v.thumbnail_url, v.language,
               s.sentence_id, s.start_time, s.content,
               NULL::text AS surface_form,
               'multi_word'::text AS match_type
        FROM sentence_matches sm
        JOIN sentence s ON s.sentence_id = sm.sentence_id
        JOIN video v    ON v.video_id = s.video_id
        WHERE (${lang_p}::text IS NULL OR v.language = ${lang_p})
        ORDER BY v.video_id, s.start_time
    """
    return query, params


def _build_multi_word_video_query(terms: list[str], language: str | None) -> tuple[str, list]:
    """Fallback: find videos containing ALL terms; return the first sentence per video."""
    n = len(terms)
    params: list = list(terms)

    union_parts = [
        f"SELECT {i + 1} AS term_idx, word_id FROM word_table "
        f"WHERE similarity(word, ${i + 1}) > {SIMILARITY_THRESHOLD} "
        f"OR similarity(lemma, ${i + 1}) > {SIMILARITY_THRESHOLD}"
        for i in range(n)
    ]

    n_p = n + 1
    lang_p = n + 2
    params.extend([n, language])

    query = f"""
        WITH matched_words AS (
            {" UNION ALL ".join(union_parts)}
        ),
        video_matches AS (
            SELECT s.video_id
            FROM word_to_sentence wts
            JOIN matched_words mw ON mw.word_id = wts.word_id
            JOIN sentence s       ON s.sentence_id = wts.sentence_id
            GROUP BY s.video_id
            HAVING COUNT(DISTINCT mw.term_idx) = ${n_p}
        ),
        first_sentences AS (
            SELECT DISTINCT ON (s.video_id)
                s.sentence_id, s.video_id, s.start_time, s.content
            FROM video_matches vm
            JOIN sentence s ON s.video_id = vm.video_id
            ORDER BY s.video_id, s.start_time
        )
        SELECT v.video_id, v.title, v.thumbnail_url, v.language,
               fs.sentence_id, fs.start_time, fs.content,
               NULL::text AS surface_form,
               'video_match'::text AS match_type
        FROM first_sentences fs
        JOIN video v ON v.video_id = fs.video_id
        WHERE (${lang_p}::text IS NULL OR v.language = ${lang_p})
        ORDER BY v.duration ASC
    """
    return query, params


def _to_dict(r) -> dict:
    return {
        "video_id": r["video_id"],
        "title": r["title"],
        "thumbnail_url": r["thumbnail_url"],
        "language": r["language"],
        "start_time": r["start_time"],
        "start_time_int": math.floor(r["start_time"]),
        "content": r["content"],
        "surface_form": r["surface_form"],
        "match_type": r["match_type"],
    }


async def search(
    pool: asyncpg.Pool,
    query: str,
    language: str | None,
    limit: int,
    offset: int,
) -> tuple[list[dict], int]:
    terms = query.strip().split()

    if len(terms) > 1:
        # A selected phrase chip (e.g. "geben jdm. etw.") has spaces but should
        # go through phrase search, not multi-word. Try phrase first.
        blueprint_ids = await _resolve_blueprint_ids(pool, query, whole_word=False)
        phrase_rows = (
            await pool.fetch(PHRASE_QUERY, blueprint_ids, language)
            if blueprint_ids else []
        )
        if phrase_rows:
            results = [_to_dict(r) for r in phrase_rows]
        else:
            sent_query, sent_params = _build_multi_word_sentence_query(terms, language)
            rows = await pool.fetch(sent_query, *sent_params)

            if not rows:
                vid_query, vid_params = _build_multi_word_video_query(terms, language)
                rows = await pool.fetch(vid_query, *vid_params)

            if rows:
                results = [_to_dict(r) for r in rows]
            else:
                # No single video has all terms — greedy set cover across videos
                results = await greedy_set_cover_search(pool, terms, language)
    else:
        # Single word: exact match + phrase blueprint, merged.
        # Use word-boundary phrase query so "ist" doesn't match "Tadschikistan".
        word_ids, surface_ids = await _resolve_word_ids(pool, query)
        word_rows = (
            await pool.fetch(WORD_QUERY, word_ids, language, surface_ids)
            if word_ids else []
        )
        blueprint_ids = await _resolve_blueprint_ids(pool, query, whole_word=True)
        phrase_rows = (
            await pool.fetch(PHRASE_QUERY, blueprint_ids, language)
            if blueprint_ids else []
        )

        seen = {r["sentence_id"] for r in phrase_rows}
        extra = [r for r in word_rows if r["sentence_id"] not in seen]

        results = [_to_dict(r) for r in phrase_rows] + [_to_dict(r) for r in extra]
        results.sort(key=lambda r: r["start_time"])

    total = len(results)
    return results[offset: offset + limit], total


def _build_video_coverage_query(terms: list[str], language: str | None) -> tuple[str, list]:
    """Return one row per (video, term_idx) with the first matching sentence."""
    n = len(terms)
    params: list = list(terms)

    union_parts = [
        f"SELECT {i + 1} AS term_idx, word_id FROM word_table "
        f"WHERE similarity(word, ${i + 1}) > {SIMILARITY_THRESHOLD} "
        f"OR similarity(lemma, ${i + 1}) > {SIMILARITY_THRESHOLD}"
        for i in range(n)
    ]

    lang_p = n + 1
    params.append(language)

    query = f"""
        WITH matched_words AS (
            {" UNION ALL ".join(union_parts)}
        )
        SELECT DISTINCT ON (s.video_id, mw.term_idx)
            s.video_id, mw.term_idx,
            s.sentence_id, s.start_time, s.content,
            v.title, v.thumbnail_url, v.language, v.duration
        FROM word_to_sentence wts
        JOIN matched_words mw ON mw.word_id = wts.word_id
        JOIN sentence s ON s.sentence_id = wts.sentence_id
        JOIN video v    ON v.video_id = s.video_id
        WHERE (${lang_p}::text IS NULL OR v.language = ${lang_p})
        ORDER BY s.video_id, mw.term_idx, s.start_time
    """
    return query, params


async def greedy_set_cover_search(
    pool: asyncpg.Pool,
    terms: list[str],
    language: str | None,
) -> list[dict]:
    """Greedy set cover: find minimum videos that collectively cover all terms."""
    query, params = _build_video_coverage_query(terms, language)
    rows = await pool.fetch(query, *params)

    # coverage[video_id][term_idx] = first matching row for that term in that video
    coverage: dict[str, dict[int, object]] = {}
    for row in rows:
        vid = row["video_id"]
        if vid not in coverage:
            coverage[vid] = {}
        coverage[vid][row["term_idx"]] = row

    uncovered = set(range(1, len(terms) + 1))  # term_idx is 1-based
    results = []

    while uncovered and coverage:
        # Pick video covering the most uncovered terms; tiebreak by shortest duration
        best_vid = max(
            coverage,
            key=lambda vid: (
                len(set(coverage[vid]) & uncovered),
                -coverage[vid][next(iter(coverage[vid]))]["duration"],
            ),
        )
        new_terms = set(coverage[best_vid]) & uncovered
        if not new_terms:
            break

        # Representative sentence: earliest sentence for the lowest term index
        rep = coverage[best_vid][min(new_terms)]
        results.append({
            "video_id": rep["video_id"],
            "title": rep["title"],
            "thumbnail_url": rep["thumbnail_url"],
            "language": rep["language"],
            "start_time": rep["start_time"],
            "start_time_int": math.floor(rep["start_time"]),
            "content": rep["content"],
            "surface_form": None,
            "match_type": "set_cover",
        })

        uncovered -= new_terms
        del coverage[best_vid]

    return results


async def get_video_sentences(
    pool: asyncpg.Pool,
    video_id: str,
) -> list[dict]:
    """All sentences in a video, ordered by time."""
    rows = await pool.fetch(
        "SELECT sentence_id, start_time, content FROM sentence WHERE video_id = $1 ORDER BY start_time",
        video_id,
    )
    return [
        {
            "sentence_id": r["sentence_id"],
            "start_time": r["start_time"],
            "start_time_int": math.floor(r["start_time"]),
            "content": r["content"],
        }
        for r in rows
    ]


SUGGEST_KINDS = ("words", "phrases", "both")

# Languages that have a phrase-suggestion source. phrase_blueprint is the
# German verb-blueprint table (no language column), so today only German has
# phrases. Extend this when a phrase extractor for another language lands
# (TODO #36) and that language's blueprints get a language scope.
PHRASE_LANGUAGES = ("de",)


async def _suggest_words(pool, query: str, language: str, limit: int) -> list[dict]:
    """Frequency-ranked word prefix match, language-scoped (TODO #38 route B).

    Case-insensitive prefix over word_table; DISTINCT ON (lower(word))
    collapses casing variants to the highest-frequency casing; ranked by the
    precomputed `frequency` column (migration 032). Index-assisted via
    ix_word_table_lang_lower_word.
    """
    rows = await pool.fetch(
        """
        SELECT word, frequency
          FROM (
              SELECT DISTINCT ON (lower(word)) word, frequency
                FROM word_table
               WHERE language = $1
                 AND lower(word) LIKE lower($2) || '%'
               ORDER BY lower(word), frequency DESC
          ) t
         ORDER BY frequency DESC, word ASC
         LIMIT $3
        """,
        language, query, limit,
    )
    return [{"word": r["word"], "score": float(r["frequency"]), "type": "word"} for r in rows]


async def _suggest_phrases(pool, query: str, limit: int) -> list[dict]:
    """German verb-blueprint phrase suggestions (the pre-route-B behaviour).

    phrase_blueprint has no language column — it's German-only — so callers
    gate this on PHRASE_LANGUAGES. Ranked by trigram similarity to the
    typed query.
    """
    rows = await pool.fetch(
        r"""
        SELECT blueprint AS word, strict_word_similarity($1, lookup_key) AS score
          FROM phrase_blueprint
         WHERE strict_word_similarity($1, lookup_key) > 0.3
           AND lookup_key ~* ('\m' || $1 || '\M')
         ORDER BY score DESC
         LIMIT $2
        """,
        query, limit,
    )
    return [{"word": r["word"], "score": float(r["score"]), "type": "phrase"} for r in rows]


async def suggest(
    pool: asyncpg.Pool,
    query: str,
    language: str | None,
    limit: int = 10,
    kind: str = "words",
) -> list[dict]:
    """Autocomplete suggestions for `query` in `language`.

    `kind` (chosen by the user via the SearchBar control) selects the source:
      'words'   — frequency-ranked word prefix matches (route B). Default.
      'phrases' — German verb-blueprint phrase matches (only meaningful for
                  PHRASE_LANGUAGES; empty otherwise).
      'both'    — phrases first (capped at ~half the limit so words still
                  show), then frequency-ranked words fill the rest.

    Words and phrases rank on different scales (frequency count vs trigram
    similarity), so 'both' keeps them in separate sections rather than
    interleaving by score. The frontend differentiates by `type`.

    Returns [{word, score, type ∈ {'word','phrase'}}].
    """
    q = query.strip()
    if not q or not language:
        return []
    if kind not in SUGGEST_KINDS:
        kind = "words"

    want_words = kind in ("words", "both")
    want_phrases = kind in ("phrases", "both") and language in PHRASE_LANGUAGES

    words = await _suggest_words(pool, q, language, limit) if want_words else []
    phrases = await _suggest_phrases(pool, q, limit) if want_phrases else []

    if kind == "words":
        return words[:limit]
    if kind == "phrases":
        return phrases[:limit]

    # 'both' — reserve up to half the slots for phrases so words always show,
    # then fill the remainder with words.
    if not phrases:
        return words[:limit]
    if not words:
        return phrases[:limit]
    phrase_slots = min(len(phrases), max(1, limit // 2))
    return phrases[:phrase_slots] + words[: limit - phrase_slots]


async def get_word_forms(pool: asyncpg.Pool, terms: list[str]) -> list[str]:
    """Return all surface forms that share a lemma with any of the given terms."""
    rows = await pool.fetch("""
        SELECT DISTINCT w2.word
        FROM word_table w1
        JOIN word_table w2 ON w2.lemma = w1.lemma
        WHERE w1.word = ANY($1) OR w1.lemma = ANY($1)
    """, terms)
    return [r["word"] for r in rows]


async def get_languages(pool: asyncpg.Pool) -> list[str]:
    rows = await pool.fetch("SELECT DISTINCT language FROM video ORDER BY language")
    return [r["language"] for r in rows]


async def get_categories(pool: asyncpg.Pool) -> list[str]:
    rows = await pool.fetch(
        "SELECT category FROM video_category ORDER BY category"
    )
    return [r["category"] for r in rows]
