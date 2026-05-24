"""Lemma correction candidates (#39 slice 3A).

User/community flags for bad spaCy lemmas. **SIGNAL ONLY** — this service never
writes `lemma_override`; promotion candidate → override is admin/LLM review
(slice 3B). See docs/LEMMA_OVERRIDE_WORKFLOW.md.
"""
from __future__ import annotations

import asyncpg


async def create_candidate(
    pool: asyncpg.Pool,
    *,
    user_id: str,
    language: str,
    surface_form: str,
    observed_lemma: str,
    suggested_lemma: str,
    context_text: str,
    item_type: str | None,
    item_id: int | None,
    sentence_id: int | None,
) -> dict:
    """Create a pending candidate, or bump `report_count` on an existing pending
    duplicate (cross-user dedup on language/surface/observed/suggested/context —
    `user_id` is intentionally not in the key). Returns the candidate row.

    Never touches `lemma_override`. `suggested_lemma`/`context_text` are already
    normalized to '' (the "absent" sentinel) by the request schema.
    """
    row = await pool.fetchrow(
        """
        INSERT INTO lemma_correction_candidate
            (user_id, language, surface_form, observed_lemma, suggested_lemma,
             context_text, item_type, item_id, sentence_id, source, status)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, 'user_flag', 'pending')
        ON CONFLICT (language, surface_form, observed_lemma, suggested_lemma, context_text)
            WHERE status = 'pending'
            DO UPDATE SET report_count = lemma_correction_candidate.report_count + 1,
                          updated_at   = NOW()
        RETURNING *
        """,
        user_id, language, surface_form, observed_lemma, suggested_lemma,
        context_text, item_type, item_id, sentence_id,
    )
    return dict(row)


async def list_candidates(
    pool: asyncpg.Pool, *, status: str = "pending", limit: int = 100
) -> list[dict]:
    """Candidates with the given status, newest first, capped at 100 (the admin
    review queue). 3A surfaces 'pending'; the status arg is forward-compat for
    3B's accepted/rejected views."""
    limit = max(1, min(limit, 100))
    rows = await pool.fetch(
        """
        SELECT * FROM lemma_correction_candidate
         WHERE status = $1
         ORDER BY created_at DESC, candidate_id DESC
         LIMIT $2
        """,
        status, limit,
    )
    return [dict(r) for r in rows]
