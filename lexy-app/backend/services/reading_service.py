"""
reading_service.py

Service layer for the interactive reading feature.

Responsibilities:
  - Bulk word-status lookup for a book page (for highlighting)
  - Save / list / update reading_selections (custom learning units)
"""
from __future__ import annotations

import json
import re

import asyncpg

from . import catalog_resolver


async def get_word_statuses_for_page(
    pool: asyncpg.Pool,
    user_id: str,
    doc_id: str,
    page_number: int,
    language: str,
) -> dict[str, str]:
    """
    Return {word_lowercase: status} for all user-tagged words visible on a page.

    Strategy:
      1. Fetch display text for every non-ignored block on the page.
      2. Extract unique alphabetic word tokens (lowercase).
      3. Batch-join against word_table + user_word_knowledge to get statuses.

    Words not in word_table or without a knowledge row are absent from the result
    (the frontend treats absence as unknown).
    """
    rows = await pool.fetch(
        """
        SELECT b.clean_text, b.corrected_text, b.user_text_override, b.correction_status
          FROM book_blocks b
          JOIN book_pages  p ON p.page_id = b.page_id
         WHERE p.doc_id       = $1::uuid
           AND p.page_number  = $2
           AND b.block_type  != 'ignored'
         ORDER BY b.block_index
        """,
        doc_id, page_number,
    )

    all_words: set[str] = set()
    for r in rows:
        if r["user_text_override"] is not None:
            text = r["user_text_override"]
        elif r["correction_status"] == "approved" and r["corrected_text"]:
            text = r["corrected_text"]
        else:
            text = r["clean_text"] or ""

        for w in re.findall(r"[^\W\d_]+", text, re.UNICODE):
            all_words.add(w.lower())

    if not all_words:
        return {}

    # Fold in Python, and key the result the way the frontend looks it up.
    #
    # Both halves were broken by the C collation. The filter compared
    # Python-lowered page tokens against SQL `LOWER(w.word)`, which folds ASCII
    # only, so an umlaut word never matched; and the returned key was that same
    # SQL-lowered value, so even a match would have been filed under `'Öl'`
    # while `BookReaderPage.tsx` looks it up as `tok.text.toLowerCase()` →
    # `'öl'`. Fixed 2026-07-28.
    #
    # The filter moves to Python rather than being widened: the join already
    # scopes rows to this user's tracked words (tens of rows), so SQL narrowing
    # bought nothing.
    #
    # Keys use plain `.lower()`, deliberately NOT `normalize_key`. The frontend
    # has no `.normalize()` call anywhere, so an NFC-normalised key would be
    # unlookupable for decomposed text. `.lower()` is exactly what JS
    # `toLowerCase()` produces. Adding NFC on both sides is a separate frontend
    # change; no book block currently carries combining diacritics.
    status_rows = await pool.fetch(
        """
        SELECT w.word, uwk.status
          FROM word_table w
          JOIN user_word_knowledge uwk
               ON uwk.item_id   = w.word_id
              AND uwk.item_type = 'word'
              AND uwk.user_id   = $1::uuid
         WHERE w.language = $2
        """,
        user_id, language,
    )

    return {
        key: r["status"]
        for r in status_rows
        if (key := r["word"].lower()) in all_words
    }


async def save_selection(
    pool: asyncpg.Pool,
    user_id: str,
    doc_id: str,
    canonical: str,
    surface_text: str,
    sentence_text: str,
    anchors: list[dict],
    note: str | None,
) -> dict:
    """
    Persist a custom learning unit and return the saved row dict.

    anchors format: [{"block_id": int, "token_index": int, "surface": str}, ...]
    """
    row = await pool.fetchrow(
        """
        INSERT INTO reading_selections
            (user_id, doc_id, canonical, surface_text, sentence_text, anchors, note)
        VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, $7)
        RETURNING selection_id, user_id, doc_id, canonical, surface_text,
                  sentence_text, anchors, note, status, created_at, updated_at
        """,
        user_id, doc_id, canonical, surface_text, sentence_text,
        json.dumps(anchors), note,
    )
    return dict(row)


async def list_selections_for_page(
    pool: asyncpg.Pool,
    user_id: str,
    doc_id: str,
    block_ids: list[int],
) -> list[dict]:
    """
    Return all saved selections that anchor to any of the given block_ids.
    Used to highlight already-saved tokens when rendering a page.
    """
    if not block_ids:
        return []

    rows = await pool.fetch(
        """
        SELECT selection_id, user_id, doc_id, canonical, surface_text,
               sentence_text, anchors, note, status, created_at, updated_at
          FROM reading_selections
         WHERE user_id = $1::uuid
           AND doc_id  = $2::uuid
           AND EXISTS (
               SELECT 1
                 FROM jsonb_array_elements(anchors) a
                WHERE (a->>'block_id')::int = ANY($3::int[])
           )
         ORDER BY created_at
        """,
        user_id, doc_id, block_ids,
    )
    return [dict(r) for r in rows]


async def list_all_selections(
    pool: asyncpg.Pool,
    user_id: str,
    doc_id: str,
) -> list[dict]:
    """Return all selections for a document (for the library/review view)."""
    rows = await pool.fetch(
        """
        SELECT selection_id, user_id, doc_id, canonical, surface_text,
               sentence_text, anchors, note, status, created_at, updated_at
          FROM reading_selections
         WHERE user_id = $1::uuid AND doc_id = $2::uuid
         ORDER BY created_at DESC
        """,
        user_id, doc_id,
    )
    return [dict(r) for r in rows]


async def update_selection(
    pool: asyncpg.Pool,
    selection_id: str,
    user_id: str,
    note: str | None = None,
    status: str | None = None,
) -> dict | None:
    """Partial update of note and/or status."""
    sets: list[str] = []
    params: list = [selection_id, user_id]

    if note is not None:
        params.append(note or None)
        sets.append(f"note = ${len(params)}")

    if status is not None:
        params.append(status)
        sets.append(f"status = ${len(params)}")

    if not sets:
        row = await pool.fetchrow(
            "SELECT * FROM reading_selections WHERE selection_id=$1::uuid AND user_id=$2::uuid",
            selection_id, user_id,
        )
        return dict(row) if row else None

    sets.append("updated_at = NOW()")
    query = f"""
        UPDATE reading_selections
           SET {', '.join(sets)}
         WHERE selection_id = $1::uuid AND user_id = $2::uuid
        RETURNING *
    """
    row = await pool.fetchrow(query, *params)
    return dict(row) if row else None


async def delete_selection(
    pool: asyncpg.Pool,
    selection_id: str,
    user_id: str,
) -> bool:
    """Delete a selection. Returns True if deleted, False if not found."""
    result = await pool.execute(
        "DELETE FROM reading_selections WHERE selection_id=$1::uuid AND user_id=$2::uuid",
        selection_id, user_id,
    )
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# Review scheduling
# ---------------------------------------------------------------------------

# Days to wait after each consecutive "got_it" response.
# Index = review_count before the event (0-based).
# Beyond index 5, the interval is capped at 30 days.
_REVIEW_INTERVALS_DAYS = [1, 2, 4, 7, 14, 30]


def _interval_days(review_count: int) -> int:
    if review_count < len(_REVIEW_INTERVALS_DAYS):
        return _REVIEW_INTERVALS_DAYS[review_count]
    return 30


async def record_review(
    pool: asyncpg.Pool,
    selection_id: str,
    user_id: str,
    outcome: str,  # 'got_it' | 'still_learning' | 'mastered'
) -> dict | None:
    """
    Update a selection after a review event.

    got_it:         increment review_count, set next_review_at per interval schedule
    still_learning: reset review_count to 0, set next_review_at = NOW() (due immediately)
    mastered:       set status = 'mastered', clear next_review_at (leaves rotation)
    """
    row = await pool.fetchrow(
        "SELECT review_count FROM reading_selections "
        "WHERE selection_id=$1::uuid AND user_id=$2::uuid",
        selection_id, user_id,
    )
    if not row:
        return None

    if outcome == "mastered":
        updated = await pool.fetchrow(
            """
            UPDATE reading_selections
               SET status        = 'mastered',
                   next_review_at = NULL,
                   updated_at    = NOW()
             WHERE selection_id = $1::uuid AND user_id = $2::uuid
            RETURNING *
            """,
            selection_id, user_id,
        )
    elif outcome == "got_it":
        new_count = row["review_count"] + 1
        days = _interval_days(row["review_count"])
        updated = await pool.fetchrow(
            """
            UPDATE reading_selections
               SET review_count   = $3,
                   next_review_at = NOW() + ($4 || ' days')::INTERVAL,
                   updated_at     = NOW()
             WHERE selection_id = $1::uuid AND user_id = $2::uuid
            RETURNING *
            """,
            selection_id, user_id, new_count, str(days),
        )
    else:  # still_learning — reset and bring back immediately
        updated = await pool.fetchrow(
            """
            UPDATE reading_selections
               SET review_count   = 0,
                   next_review_at = NOW(),
                   updated_at     = NOW()
             WHERE selection_id = $1::uuid AND user_id = $2::uuid
            RETURNING *
            """,
            selection_id, user_id,
        )

    return dict(updated) if updated else None


async def find_catalog_item(
    pool: asyncpg.Pool,
    doc_id: str,
    canonical: str,
) -> tuple[int, str] | None:
    """
    Look up a reading selection's canonical text in the word/phrase catalog.

    Returns (item_id, item_type) if the text binds to exactly one row for the
    document's language, or None if it binds to none — or to several.

    Used to wire reading saves/reviews into the main progression system.

    Delegates to `catalog_resolver`, which is the same rule vocabulary lists
    use. Three things changed when it did (2026-07-28):

    **Unicode.** The old predicates were `LOWER(word) = $1` fed
    `canonical.lower()`. Under this database's C locale Postgres folds ASCII
    only, so `LOWER('Öl')` is `'Öl'` and a selection of *öl* never bound —
    meaning it never propagated to the main SRS. Folding now happens in
    Python. See `services/text_norm.py`.

    **Phrases match `canonical`, not `surface_form`.** Vocabulary lists always
    matched `canonical`; reading matched `surface_form`, and 773 German rows
    differ, so the same phrase could bind to different rows depending on where
    the user entered it. `canonical` wins because it is the identity the rest
    of the app keys on (`matcher_service`, `phrase_table`'s unique constraint).
    Nothing is lost in practice: every differing row is a placeholder
    blueprint whose `surface_form` is a fragment like `(Dat) (Akk) geben`,
    which no book selection produces.

    **Ambiguity fails safe.** This used to be a bare `fetchrow`, silently
    binding to whichever row came back first — a coin flip between *die Bank*
    the bench and *die Bank* the institution, attaching mastery to the wrong
    sense invisibly. A surface with several catalog rows now returns None, so
    the selection is still saved and reviewable on its own schedule but does
    not advance a possibly-wrong catalog item. 410 German word surfaces have
    duplicate rows today, so this does reduce propagation coverage; that is
    the intended trade, and `word_list_service` has reported those same
    surfaces as `ambiguous` since it shipped.
    """
    doc = await pool.fetchrow(
        "SELECT language FROM book_documents WHERE doc_id = $1::uuid",
        doc_id,
    )
    if not doc:
        return None

    res = await catalog_resolver.resolve_one(pool, canonical, doc["language"])
    if res.item_id is None:
        return None
    return (res.item_id, res.item_type)


async def get_due_selections(
    pool: asyncpg.Pool,
    user_id: str,
    limit: int = 30,
) -> list[dict]:
    """
    Return selections due for review across all documents.

    Due = status='learning' AND (next_review_at IS NULL OR next_review_at <= NOW()).
    NULL next_review_at means newly saved (never reviewed) — returned first.
    Joins book_documents to include the document title for display.
    """
    rows = await pool.fetch(
        """
        SELECT rs.selection_id, rs.doc_id, rs.canonical, rs.surface_text,
               rs.sentence_text, rs.note, rs.status,
               rs.review_count, rs.next_review_at, rs.created_at,
               bd.title AS doc_title
          FROM reading_selections rs
          JOIN book_documents bd ON bd.doc_id = rs.doc_id
         WHERE rs.user_id = $1::uuid
           AND rs.status  = 'learning'
           AND (rs.next_review_at IS NULL OR rs.next_review_at <= NOW())
         ORDER BY rs.next_review_at ASC NULLS FIRST, rs.created_at ASC
         LIMIT $2
        """,
        user_id, limit,
    )
    return [dict(r) for r in rows]
