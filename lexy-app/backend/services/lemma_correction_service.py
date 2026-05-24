"""Lemma correction candidates (#39 slice 3A).

User/community flags for bad spaCy lemmas. **SIGNAL ONLY** — this service never
writes `lemma_override`; promotion candidate → override is admin/LLM review
(slice 3B). See docs/LEMMA_OVERRIDE_WORKFLOW.md.
"""
from __future__ import annotations

import asyncpg
from pydantic import ValidationError

from ..models.schemas import LemmaCorrectionAdjudication

# Source tag for an override promoted from a reviewed user flag. Single source of
# truth (must match the CHECK on lemma_override.source — migration 033).
REVIEWED_OVERRIDE_SOURCE = "user_flag_reviewed"


class CandidateNotFound(Exception):
    """No candidate with that id (router → 404)."""


class CandidateNotPending(Exception):
    """Candidate already reviewed — accepted/rejected/merged (router → 409)."""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status


class NothingToPromote(Exception):
    """Accept with no corrected_lemma and a blank suggested_lemma (router → 400)."""


class AdjudicatorError(Exception):
    """The injected adjudicator returned malformed output (router → 502)."""


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


async def accept_candidate(
    pool: asyncpg.Pool,
    *,
    candidate_id: int,
    admin_id: str,
    corrected_lemma: str | None,
    review_note: str | None,
) -> dict:
    """Promote a pending candidate to a `lemma_override` and mark it accepted —
    the human-gated signal→authority step (slice 3B). Atomic: the override write
    and the candidate flip are one transaction, so an override failure leaves the
    candidate pending. Raises `CandidateNotFound` / `CandidateNotPending` /
    `NothingToPromote`. Returns `{"candidate": ..., "override": ...}`.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            cand = await conn.fetchrow(
                "SELECT * FROM lemma_correction_candidate WHERE candidate_id = $1 FOR UPDATE",
                candidate_id,
            )
            if cand is None:
                raise CandidateNotFound()
            if cand["status"] != "pending":
                raise CandidateNotPending(cand["status"])
            chosen = (corrected_lemma or "").strip() or (cand["suggested_lemma"] or "").strip()
            if not chosen:
                raise NothingToPromote()

            # Context-free override (surface_form/pos left NULL → matches the
            # partial unique index uq_lemma_override_lang_observed). Upsert:
            # update an existing context-free override for the same
            # (language, observed_lemma), else insert a new one.
            override = await conn.fetchrow(
                """
                INSERT INTO lemma_override
                    (language, observed_lemma, corrected_lemma, source, status)
                VALUES ($1, $2, $3, $4, 'active')
                ON CONFLICT (language, observed_lemma)
                    WHERE surface_form IS NULL AND pos IS NULL
                    DO UPDATE SET corrected_lemma = EXCLUDED.corrected_lemma,
                                  source          = EXCLUDED.source,
                                  status          = 'active',
                                  updated_at      = NOW()
                RETURNING *
                """,
                cand["language"], cand["observed_lemma"], chosen, REVIEWED_OVERRIDE_SOURCE,
            )
            updated = await conn.fetchrow(
                """
                UPDATE lemma_correction_candidate
                   SET status = 'accepted', reviewed_by = $2::uuid,
                       reviewed_at = NOW(), review_note = $3, updated_at = NOW()
                 WHERE candidate_id = $1
                RETURNING *
                """,
                candidate_id, admin_id, review_note,
            )
    return {"candidate": dict(updated), "override": dict(override)}


async def reject_candidate(
    pool: asyncpg.Pool,
    *,
    candidate_id: int,
    admin_id: str,
    review_note: str | None,
) -> dict:
    """Mark a pending candidate rejected. NEVER writes `lemma_override`. Raises
    `CandidateNotFound` / `CandidateNotPending`. Returns `{"candidate": ...,
    "override": None}`."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            cand = await conn.fetchrow(
                "SELECT status FROM lemma_correction_candidate WHERE candidate_id = $1 FOR UPDATE",
                candidate_id,
            )
            if cand is None:
                raise CandidateNotFound()
            if cand["status"] != "pending":
                raise CandidateNotPending(cand["status"])
            updated = await conn.fetchrow(
                """
                UPDATE lemma_correction_candidate
                   SET status = 'rejected', reviewed_by = $2::uuid,
                       reviewed_at = NOW(), review_note = $3, updated_at = NOW()
                 WHERE candidate_id = $1
                RETURNING *
                """,
                candidate_id, admin_id, review_note,
            )
    return {"candidate": dict(updated), "override": None}


# --- dry-run LLM adjudication (#39 slice 3C) -------------------------------
# Advisory only: build a structured input + (via an INJECTED adjudicator) return
# a proposal. NEVER writes lemma_override / candidate status / report_count. No
# live LLM call lives here — the adjudicator is injected (None in production →
# the endpoint 503s; tests pass a fake), so this stays fully testable.

_ADJUDICATION_FIELDS = (
    "language", "surface_form", "observed_lemma",
    "suggested_lemma", "context_text", "report_count",
)

ADJUDICATION_INSTRUCTION = (
    "You are a linguistics reviewer judging a flagged lemma. Decide whether "
    "`observed_lemma` is the WRONG lemma for `surface_form` (in `context_text`) "
    "and, if so, what the correct lemma is. Do NOT accept the user's "
    "`suggested_lemma` blindly — verify it independently; a user flag is a "
    "signal, not the answer. Respond as JSON: "
    '{"decision": "accept"|"reject"|"needs_review", '
    '"proposed_corrected_lemma": string|null, "confidence": 0.0-1.0, '
    '"reason": string}.'
)


def build_adjudication_input(candidate: dict) -> dict:
    """The structured candidate fields handed to an adjudicator (and the prompt)."""
    return {k: candidate[k] for k in _ADJUDICATION_FIELDS}


def format_adjudication_prompt(adj_input: dict) -> str:
    """Deterministic prompt for a (future) real LLM adjudicator — the
    instruction/warning + the candidate fields. No model is called here; this is
    the prompt-shape deliverable, unit-tested for the don't-trust-blindly warning."""
    body = "\n".join(f"{k}: {adj_input.get(k)}" for k in _ADJUDICATION_FIELDS)
    return f"{ADJUDICATION_INSTRUCTION}\n\n{body}"


async def adjudicate_candidate_dry_run(pool, *, candidate_id: int, adjudicator) -> dict:
    """Return an adjudication PROPOSAL for a pending candidate — dry-run.

    `adjudicator` is an async callable `(adj_input: dict) -> dict`. Read-only:
    never writes `lemma_override`, never changes the candidate's status /
    report_count. Raises `CandidateNotFound` / `CandidateNotPending` /
    `AdjudicatorError` (malformed adjudicator output).
    """
    cand = await pool.fetchrow(
        "SELECT * FROM lemma_correction_candidate WHERE candidate_id = $1", candidate_id,
    )
    if cand is None:
        raise CandidateNotFound()
    if cand["status"] != "pending":
        raise CandidateNotPending(cand["status"])

    adj_input = build_adjudication_input(dict(cand))
    raw = await adjudicator(adj_input)
    # The adjudicator is external/untrusted output — validate against the schema.
    merged = {**(raw if isinstance(raw, dict) else {}), "candidate_id": candidate_id}
    try:
        proposal = LemmaCorrectionAdjudication(**merged)
    except ValidationError as exc:
        raise AdjudicatorError(str(exc)) from exc
    return proposal.model_dump()
