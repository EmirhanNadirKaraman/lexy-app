"""Shared helper for test-OWNED `word_table` rows (xdist-safe isolation).

See docs/TESTS.md. Tests must NOT pick a SHARED catalog row via
`SELECT ... FROM word_table LIMIT N` (no ORDER BY): under `pytest -n auto`
another worker's word-reap can delete that row mid-test, and queries that join
word_table (e.g. `review_service.get_due_cards`' `WHERE wt.word_id IS NOT NULL`)
then silently drop the row — the root cause of the SRS/global-table flake
cluster. Create an OWNED row instead; it has a globally-unique surface so no
unfiltered pick can return it and no other worker can reap it.

Two consumers:
  * the `make_word` / `srs_word` fixtures in conftest.py (fixture-style), and
  * files with an existing local word-helper + many call sites, which rewrite
    that helper's body to call `insert_owned_word` (call sites unchanged).

Rows created here are reaped after every test by the autouse `_reap_owned_words`
fixture in conftest.py, which drains `_drain()`.
"""
import uuid

# word_ids created during the current test; drained + reaped by the conftest
# autouse fixture. One list per worker process (xdist workers don't share it).
_created: list[int] = []


async def insert_owned_word(
    pool,
    *,
    language: str = "de",
    word: str | None = None,
    lemma: str | None = None,
    pos: str = "NOUN",
    tag: str = "NN",
    frequency: int | None = None,
) -> tuple[int, str]:
    """Insert a uniquely-named word and register it for teardown reap.

    Returns (word_id, surface). `word` overrides the generated surface; the
    default `_testword_<uuid>` guarantees global uniqueness.
    """
    surface = word or f"_testword_{uuid.uuid4().hex[:12]}"
    lemma = lemma or surface
    if frequency is None:
        wid = await pool.fetchval(
            "INSERT INTO word_table (word, language, pos, tag, lemma) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING word_id",
            surface, language, pos, tag, lemma,
        )
    else:
        wid = await pool.fetchval(
            "INSERT INTO word_table (word, language, pos, tag, lemma, frequency) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING word_id",
            surface, language, pos, tag, lemma, frequency,
        )
    _created.append(wid)
    return wid, surface


def _drain() -> list[int]:
    """Return + clear the ids created since the last drain (called per-test)."""
    ids = list(_created)
    _created.clear()
    return ids
