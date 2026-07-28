
import asyncpg

from .text_norm import normalize_key

LOOKUP_CANDIDATE_CAP = 10


async def resolve_word_ids(
    conn: asyncpg.Connection | asyncpg.Pool,
    text: str,
    language: str,
) -> list[int]:
    """Every `word_table` row whose surface matches `text` case-insensitively.

    Returns sorted `word_id`s: `[]` = no match, one = unambiguous, several =
    ambiguous. Callers decide what ambiguity means; this never picks for them
    (the W3 / Hole 2 rule).

    Case-insensitivity is decided in **Python**, via `text_norm.normalize_key`.
    `ILIKE` and `lower()` cannot do it here: the database runs under the C
    locale, where both fold ASCII only, so `'Öl' ILIKE 'öl'` is false and
    `lower('Öl')` is `'Öl'`. Every German word with an uppercase non-ASCII
    letter was therefore invisible to a case-variant lookup. See
    `services/text_norm.py` for the collation evidence and the rejected
    alternatives.

    Fetching the language-scoped surfaces is the same trade
    `word_list_service._resolve_surfaces` makes, for the same reason — a
    bounded predicate cannot express this fold without an ICU collation.
    Measured ~20 ms for German, ~40 ms for the larger Spanish catalog.
    """
    key = normalize_key(text)
    if not key:
        return []
    rows = await conn.fetch(
        "SELECT w.word_id, w.word FROM word_table w WHERE w.language = $1",
        language,
    )
    return sorted(r["word_id"] for r in rows if normalize_key(r["word"]) == key)


async def lookup_word_by_text(
    pool: asyncpg.Pool,
    user_id: str,
    word: str,
    language: str,
) -> dict:
    """
    Resolve a surface form to its word_table entries for the given user.

    Returns a `WordLookupResponse`-shape dict:
      {
        status:     "not_found" | "single" | "ambiguous",
        item:       <WordLookupResult dict> | None,
        candidates: [<WordLookupResult dict>...]
      }

    W3 fix (Hole 2): the pre-W3 query ended with `LIMIT 1`, which silently
    picked an arbitrary row when the same surface form mapped to multiple
    entries (e.g. *die Bank* = bench vs. financial institution → different
    `pos`/`lemma`). Mastery progress could attach to the wrong meaning
    invisibly. We now fetch up to LOOKUP_CANDIDATE_CAP rows and return them
    all when there's more than one; the caller (interactive picker) decides.

    Sort order (deterministic):
      1. exact `word` match first (the typed spelling wins)
      2. then by `lemma` ascending
      3. then by `word_id` ascending (stability tie-breaker)

    Matching runs through `resolve_word_ids`, not `ILIKE`. The old predicate
    (`w.word ILIKE $1`) folded ASCII only under this database's C collation,
    so a user clicking *öl* got `not_found` while `Öl` sat in the table — and
    the picker then offered "Learn anyway", which forked the surface into a
    second row. Sort key 1 used to be a case-insensitive comparison; every
    candidate is now case-insensitively equal by construction, so it compares
    the exact spelling instead.
    """
    word_ids = await resolve_word_ids(pool, word, language)
    if not word_ids:
        return {"status": "not_found", "item": None, "candidates": []}

    rows = await pool.fetch(
        """
        SELECT
            w.word_id,
            w.word,
            w.lemma,
            w.pos,
            uwk.status                        AS current_status,
            COALESCE(uwk.passive_level, 0)    AS passive_level,
            COALESCE(uwk.active_level,  0)    AS active_level,
            sc_p.due_date                     AS passive_due,
            sc_a.due_date                     AS active_due
        FROM word_table w
        LEFT JOIN user_word_knowledge uwk
               ON uwk.item_id   = w.word_id
              AND uwk.item_type = 'word'
              AND uwk.user_id   = $2::uuid
        LEFT JOIN srs_cards sc_p
               ON sc_p.item_id   = w.word_id
              AND sc_p.item_type = 'word'
              AND sc_p.user_id   = $2::uuid
              AND sc_p.direction = 'passive'
        LEFT JOIN srs_cards sc_a
               ON sc_a.item_id   = w.word_id
              AND sc_a.item_type = 'word'
              AND sc_a.user_id   = $2::uuid
              AND sc_a.direction = 'active'
        WHERE w.word_id = ANY($1::int[])
        ORDER BY
            CASE WHEN w.word = $3 THEN 0 ELSE 1 END,
            w.lemma ASC,
            w.word_id ASC
        LIMIT $4
        """,
        word_ids,
        user_id,
        word.strip(),
        LOOKUP_CANDIDATE_CAP,
    )

    candidates = [
        {
            "word_id":        r["word_id"],
            "word":           r["word"],
            "lemma":          r["lemma"],
            "pos":            r["pos"] or "",
            "current_status": r["current_status"],
            "passive_level":  r["passive_level"],
            "active_level":   r["active_level"],
            "passive_due":    r["passive_due"],
            "active_due":     r["active_due"],
        }
        for r in rows
    ]

    if not candidates:
        return {"status": "not_found", "item": None, "candidates": []}
    if len(candidates) == 1:
        return {"status": "single", "item": candidates[0], "candidates": candidates}
    return {"status": "ambiguous", "item": None, "candidates": candidates}


async def _lookup_first_match(
    pool: asyncpg.Pool,
    user_id: str,
    word: str,
    language: str,
) -> dict | None:
    """
    Compatibility helper for non-interactive callers (e.g. `learn_word_anyway`'s
    post-create read) that just want the canonical entry. Picks `item` when
    available, falls back to the first candidate, else None. Internal — the
    public API surfaces the full response so the picker can disambiguate.
    """
    resp = await lookup_word_by_text(pool, user_id, word, language)
    if resp["item"] is not None:
        return resp["item"]
    if resp["candidates"]:
        return resp["candidates"][0]
    return None


async def learn_word_anyway(
    pool: asyncpg.Pool,
    user_id: str,
    text: str,
    language: str,
) -> dict:
    """
    Create (or reuse) a word_table row for `text` and mark it as 'learning'
    for the user atomically. Hole 1 / W2 fix: lets a user adopt a word that
    the scraper has never indexed, so the click-→-mark-→-review loop works
    end-to-end without waiting on a pipeline run.

    Behaviour:
      - text is trimmed; raises ValueError on empty input. Callers validate
        length again at the route layer (Pydantic).
      - An existing row is REUSED, never duplicated. `resolve_word_ids`
        decides that case-insensitively in Python; see the anti-fork note
        below for why the INSERT's own conflict clause cannot.
      - Only a genuinely new word gets a row, and it is sparse: pos='X'
        (spaCy's universal "other" tag), tag='', lemma=text. A future
        enrichment pass (e.g. when the scraper sees the word in context)
        may patch the row in place — we don't fabricate POS/lemma now.
      - Re-running with the same input is a no-op at the catalog level. We
        still run apply_progression so the user always ends up in the
        'learning' state regardless of prior status.

    Anti-fork (2026-07-28)
    ----------------------
    `word_table` is UNIQUE (word, language, pos) with `pos` *in the key*, so
    `ON CONFLICT (word, language, pos)` on a `pos='X'` insert does NOT conflict
    with the same word stored as `pos=''` (backfilled) or `pos='NOUN'`
    (scraper). It inserted a second row, and two rows for one surface is what
    `word_list_service` reports as `ambiguous` — permanently, for every user.
    Two such forked rows exist in the database from before this fix; cleaning
    them up is a separate data decision.

    Reaching that fork needed the lookup to miss while the row existed, which
    the C-collation `ILIKE` bug made easy: clicking *öl* when `Öl` was stored
    returned `not_found`, the picker offered "Learn anyway", and the user
    forked the word by accepting. Both halves are fixed here — resolve first,
    insert only when nothing matched.

    Several matches (an already-ambiguous surface) reuse the lowest `word_id`
    rather than inserting. That is the same row this function already returned
    in that case, since `_lookup_first_match` falls back to `candidates[0]`;
    the change is that it no longer adds a third row first. The response model
    is a single `WordLookupResult`, so surfacing ambiguity here would be an
    API change — tracked separately.
      - Progression: 'status_marked_learning' with status_override='learning'
        creates both passive AND active SRS cards (#0b) without inflating
        active_level / times_used_correctly (rule is exposure-only).

    Returns the lookup-shape dict (same keys as `lookup_word_by_text`) so
    the frontend can re-render the picker without a second round-trip.
    """
    from . import progression_service  # local import — avoids cycle at module load

    text = text.strip()
    if not text:
        raise ValueError("text must be non-empty")

    # Resolve BEFORE inserting. This is the anti-fork guard: the INSERT's own
    # ON CONFLICT cannot see a row stored under a different pos, and ILIKE
    # cannot see a Unicode case variant. Deliberately outside the transaction
    # below — it is a read, and it fetches the language-scoped catalog
    # (~9.3k de / ~29.6k es rows), which has no business being held open
    # inside a write transaction. An existing word needs no write at all.
    existing = await resolve_word_ids(pool, text, language)
    if existing:
        # Reuse. We don't UPDATE the row — if the scraper enriched POS/lemma
        # it should win over an 'X' placeholder.
        word_id = existing[0]
    else:
        async with pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    INSERT INTO word_table (word, language, pos, tag, lemma)
                    VALUES ($1, $2, 'X', '', $1)
                    ON CONFLICT (word, language, pos) DO NOTHING
                    RETURNING word_id
                    """,
                    text, language,
                )
                if row is None:
                    # Only reachable if a concurrent caller inserted the
                    # byte-identical surface with pos='X' between the resolve
                    # and here — that is exactly what the conflict target
                    # covers, so an exact indexed lookup finds it. Not a
                    # re-resolve: no need to re-scan the catalog for a row we
                    # know the precise key of.
                    row = await conn.fetchrow(
                        """
                        SELECT word_id FROM word_table
                         WHERE word = $1 AND language = $2 AND pos = 'X'
                        """,
                        text, language,
                    )
                    if row is None:
                        raise RuntimeError(
                            "learn_word_anyway: row vanished after conflict"
                        )
                word_id = row["word_id"]

    # progression_service.apply_progression opens its own transaction on a
    # pool connection. The catalog write above is already committed by this
    # point — same split the words.py status route uses.
    await progression_service.apply_progression(
        pool, user_id, word_id, "word",
        "status_marked_learning",
        status_override="learning",
    )

    # Read back the canonical row for the response. The caller (POST
    # /learn-anyway) expects a single WordLookupResult, so this uses the
    # internal first-match helper rather than the discriminated response.
    # Note the row read back may be a pre-existing scraper/backfill row we
    # reused, not the 'X' placeholder — so `pos` and `lemma` in the response
    # are whatever the catalog already knew, which is the point of the fix.
    result = await _lookup_first_match(pool, user_id, text, language)
    if result is None:
        raise RuntimeError("learn_word_anyway: lookup failed after insert")
    return result


async def get_user_knowledge(pool: asyncpg.Pool, user_id: str) -> list[dict]:
    """Return all word/phrase knowledge rows for a user, newest first."""
    rows = await pool.fetch(
        """
        SELECT item_id, item_type, status, passive_level, active_level, notes, last_seen
        FROM user_word_knowledge
        WHERE user_id = $1::uuid
        ORDER BY last_seen DESC NULLS LAST
        """,
        user_id,
    )
    return [dict(r) for r in rows]


