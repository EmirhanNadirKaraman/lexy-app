import json
import re

import asyncpg

from .text_norm import normalize_key


async def create_session(
    pool: asyncpg.Pool,
    user_id: str,
    session_type: str = "free",
    *,
    language: str | None = None,
    target_item_id: int | None = None,
    target_item_type: str | None = None,
) -> dict:
    """Insert a chat_sessions row.

    Stage 3 (second-language plan): `language` is the target-language
    code for this session and is persisted on the row. Free chat needs
    this because it has no target-item to derive a language from at
    message time; guided chat could re-derive via word/phrase_table but
    storing it avoids the per-message JOIN and keeps the LLM prompt +
    free_chat_* progression aligned with the user's actual target.

    NULL is accepted for back-compat with pre-Stage-3 callers; readers
    fall back to DEFAULT_LANGUAGE ("de") so legacy German sessions in
    the wild keep working.
    """
    row = await pool.fetchrow(
        """
        INSERT INTO chat_sessions
            (user_id, session_type, language, target_item_id, target_item_type)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING *
        """,
        user_id,
        session_type,
        language,
        target_item_id,
        target_item_type,
    )
    return _session_dict(row)


async def get_session(pool: asyncpg.Pool, session_id: str) -> dict | None:
    row = await pool.fetchrow(
        "SELECT * FROM chat_sessions WHERE session_id = $1",
        session_id,
    )
    return _session_dict(row) if row else None


async def list_sessions(pool: asyncpg.Pool, user_id: str) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM chat_sessions WHERE user_id = $1 ORDER BY started_at DESC",
        user_id,
    )
    return [_session_dict(r) for r in rows]


async def save_message(
    pool: asyncpg.Pool,
    session_id: str,
    role: str,
    content: str,
    *,
    language_detected: str | None = None,
    corrections: list | None = None,
    word_matches: list | None = None,
    evaluation: dict | None = None,
) -> dict:
    row = await pool.fetchrow(
        """
        INSERT INTO chat_messages
            (session_id, role, content, language_detected,
             corrections, word_matches, evaluation)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb)
        RETURNING *
        """,
        session_id,
        role,
        content,
        language_detected,
        json.dumps(corrections)  if corrections  is not None else None,
        json.dumps(word_matches) if word_matches is not None else None,
        json.dumps(evaluation)   if evaluation   is not None else None,
    )
    return _message_dict(row)


async def get_messages(pool: asyncpg.Pool, session_id: str) -> list[dict]:
    rows = await pool.fetch(
        "SELECT * FROM chat_messages WHERE session_id = $1 ORDER BY created_at ASC",
        session_id,
    )
    return [_message_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Free-chat vocabulary matching
# ---------------------------------------------------------------------------

async def match_learning_words(
    pool: asyncpg.Pool,
    user_id: str,
    text: str,
    language: str,
) -> list[dict]:
    """
    Find non-mastered vocabulary items the user is tracking that appear in `text`.

    Two paths combined:
      WORDS   — tokenises the message and matches the tokens against the words
                the user is tracking, on surface OR lemma.
      PHRASES — delegates to matcher_service.match_sentence_with_ids, which runs
                the spaCy-based phrase extractor (verb patterns, separable verbs,
                reflexive verbs) and returns canonical phrase IDs. This catches
                inflected production like "ich freue mich auf X" matching the
                canonical "sich freuen auf".

    Returns a deduplicated list of dicts shaped:
        {item_id, item_type ∈ {'word','phrase'}, word}

    The polymorphic shape lets routers/chat.py:send_message iterate the matches
    and call apply_progression(..., item_type, ...) for each — words and phrases
    advance through the same free_chat_* events.

    Case-insensitivity is decided in **Python**, via `text_norm.normalize_key`.
    The word query used to push it into SQL — `LOWER(wt.word) = ANY($2)` fed
    Python-lowered tokens — but this database runs under the C locale, where
    Postgres folds ASCII only: `LOWER('Öl')` is `'Öl'`, never `'öl'`. Producing
    an umlaut word in free chat therefore matched nothing and earned **no
    progression or SRS credit**, silently. Fixed 2026-07-28; see
    `services/text_norm.py` for the collation evidence.

    The join is inverted rather than widened: fetch the words this user is
    tracking and filter them in Python. That is bounded by the user's own
    vocabulary (tens of rows), so it is *cheaper* than the old query as well as
    correct — unlike `word_list_service`, this needs no whole-catalog fetch.

    The phrase path is unaffected: it matches on `phrase_id`s the spaCy
    extractor returns, and never asks Postgres to fold case.
    """
    tokens = {normalize_key(w) for w in re.findall(r"[^\W\d_]+", text, re.UNICODE)}
    tokens.discard("")
    if not tokens:
        return []

    # Every non-mastered word this user tracks in this language. ORDER BY makes
    # the result order deterministic; the old query had none.
    tracked = await pool.fetch(
        """
        SELECT wt.word_id AS item_id, wt.word, wt.lemma
          FROM word_table wt
          JOIN user_word_knowledge uwk
               ON uwk.item_id   = wt.word_id
              AND uwk.item_type = 'word'
              AND uwk.user_id   = $1::uuid
         WHERE wt.language   = $2
           AND uwk.status   != 'known'
         ORDER BY wt.word_id
        """,
        user_id, language,
    )

    results: list[dict] = []
    seen: set[int] = set()
    for r in tracked:
        if r["item_id"] in seen:
            continue
        # Surface OR lemma, matching the old predicate exactly.
        if (normalize_key(r["word"]) in tokens
                or normalize_key(r["lemma"] or "") in tokens):
            seen.add(r["item_id"])
            results.append(
                {"item_id": r["item_id"], "item_type": "word", "word": r["word"]}
            )

    # Phrase matching — defer the import to avoid the matcher_service module-level
    # spaCy/phrase_finder bootstrap during chat_service import (and to make tests
    # that don't touch chat insensitive to matcher init failures).
    from . import matcher_service

    try:
        phrase_hits = await matcher_service.match_sentence_with_ids(pool, text, language)
    except Exception:
        # Free chat must not crash if phrase extraction errors — analytics-grade only.
        phrase_hits = []

    phrase_ids = list({p["phrase_id"] for p in phrase_hits if p.get("phrase_id") is not None})
    if phrase_ids:
        phrase_rows = await pool.fetch(
            """
            SELECT DISTINCT pt.phrase_id AS item_id,
                   'phrase'::text       AS item_type,
                   pt.surface_form      AS word
              FROM phrase_table pt
              JOIN user_word_knowledge uwk
                   ON uwk.item_id   = pt.phrase_id
                  AND uwk.item_type = 'phrase'
                  AND uwk.user_id   = $1::uuid
             WHERE pt.phrase_id  = ANY($2::int[])
               AND pt.language   = $3
               AND uwk.status   != 'known'
            """,
            user_id, phrase_ids, language,
        )
        results.extend(dict(r) for r in phrase_rows)

    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_dict(row) -> dict:
    return {
        "session_id": str(row["session_id"]),
        "user_id": str(row["user_id"]),          # used for ownership checks, stripped by response_model
        "session_type": row["session_type"],
        # `language` is None on legacy rows (pre-Stage-3 / migration 031).
        # Routers fall back to DEFAULT_LANGUAGE when they read this.
        "language": row["language"] if "language" in row else None,
        "target_item_id": row["target_item_id"],
        "target_item_type": row["target_item_type"],
        "started_at": row["started_at"],
    }


def _message_dict(row) -> dict:
    return {
        "message_id":       row["message_id"],
        "session_id":       str(row["session_id"]),
        "role":             row["role"],
        "content":          row["content"],
        "language_detected": row["language_detected"],
        "corrections":  json.loads(row["corrections"])  if row["corrections"]  is not None else None,
        "word_matches": json.loads(row["word_matches"]) if row["word_matches"] is not None else None,
        "evaluation":   json.loads(row["evaluation"])   if row["evaluation"]   is not None else None,
        "created_at":       row["created_at"],
    }
