"""Lemma correction candidate table — community flags for bad lemmas (#39 slice 3A)

Revision ID: 034
Revises: 033
Create Date: 2026-05-24

A signal inbox for wrong spaCy lemmas. Authenticated users flag a bad
lemma/canonical; each flag creates (or bumps the report_count of) a PENDING
candidate. This table is SIGNAL ONLY — it is never read by the extractor/matcher
and never mutates `lemma_override` (033). Promotion candidate → override is
admin/LLM review (slice 3B), the only path from a user signal to extraction
truth. See docs/LEMMA_OVERRIDE_WORKFLOW.md.

`suggested_lemma` / `context_text` use '' (empty string) for "absent" rather
than NULL, so the pending-dedup unique index is on plain columns (clean
`INSERT … ON CONFLICT` inference) instead of COALESCE expressions. The Pydantic
schema normalizes missing/blank → '' so callers don't see the convention.
"""
from alembic import op


revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS lemma_correction_candidate (
            candidate_id    BIGSERIAL PRIMARY KEY,
            -- first reporter, for audit; nullable + SET NULL so the community
            -- signal outlives the reporter deleting their account.
            user_id         UUID REFERENCES users(user_id) ON DELETE SET NULL,
            language        TEXT NOT NULL CHECK (length(language) > 0),
            surface_form    TEXT NOT NULL CHECK (length(surface_form) > 0),
            observed_lemma  TEXT NOT NULL CHECK (length(observed_lemma) > 0),
            suggested_lemma TEXT NOT NULL DEFAULT '',   -- '' = no suggested correction
            context_text    TEXT NOT NULL DEFAULT '',   -- '' = no context sentence
            item_type       TEXT CHECK (item_type IN ('word','phrase')),  -- NULL allowed
            item_id         BIGINT,
            sentence_id     BIGINT,
            source          TEXT NOT NULL DEFAULT 'user_flag'
                            CHECK (source IN ('user_flag','llm','admin','import')),
            status          TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','accepted','rejected','merged')),
            report_count    INT NOT NULL DEFAULT 1,
            reviewed_by     UUID REFERENCES users(user_id) ON DELETE SET NULL,  -- slice 3B
            reviewed_at     TIMESTAMPTZ,
            review_note     TEXT,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    # One PENDING candidate per (language, surface, observed, suggested, context).
    # Cross-user BY DESIGN — two people flagging the same thing bump report_count
    # on the one row (the strength signal a reviewer ranks by); user_id is
    # deliberately NOT in the key. Partial on status='pending' so a re-flag after
    # a reviewed (accepted/rejected) candidate starts a fresh pending row.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_lemma_candidate_pending
            ON lemma_correction_candidate
               (language, surface_form, observed_lemma, suggested_lemma, context_text)
            WHERE status = 'pending'
        """
    )
    # Admin list: newest pending first.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_lemma_candidate_status_created
            ON lemma_correction_candidate (status, created_at DESC)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS lemma_correction_candidate")
