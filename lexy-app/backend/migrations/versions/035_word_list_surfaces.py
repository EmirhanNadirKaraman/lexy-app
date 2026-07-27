"""Make word_lists / word_list_items able to hold an uploaded vocabulary list

Revision ID: 035
Revises: 034
Create Date: 2026-07-27

`word_lists` + `word_list_items` have existed since migration 001 but were
never given a router, service, or frontend — 0 rows and 0 code references in
the whole repo. The vocabulary-list upload feature is the first consumer, and
the 001 shape cannot express what it needs:

  - `word_lists` has no `language`, but resolution is language-scoped and the
    corpus is multi-language since migration 030 seeded Spanish.
  - `word_list_items.item_id` is NOT NULL and there is no text column, so a
    pasted surface form that does not resolve to a `word_table` row cannot be
    stored at all. Unresolved (and ambiguous) words must stay visible to the
    user rather than being silently dropped from the list.

What this does:
  - `word_lists.language TEXT NOT NULL DEFAULT 'de'`.
  - `word_list_items.surface TEXT NOT NULL` — the original uploaded string,
    stored for EVERY entry including resolved ones. This is what makes export
    round-trip the user's input and lets an entry be re-resolved later: a word
    with no catalog match today may match after the next scraper run.
  - `word_list_items.item_id` becomes nullable. NULL = not bound to a catalog
    row, which covers both `unresolved` (no match) and `ambiguous` (several
    matches, deliberately not auto-picked — see word_list_service).
  - Replaces UNIQUE (list_id, item_id, item_type) with a unique index on
    (list_id, lower(surface)).

Why the old unique constraint is DROPPED rather than kept alongside the new
one: NULLs are distinct in Postgres, so it would not dedupe unresolved or
ambiguous rows at all — and because resolution is case-insensitive, "laufen"
and "Laufen" resolve to the same word_id, so the second insert would fail
against it. `(list_id, lower(surface))` is the correct key for a word list.

Backfill: none required (0 rows). `surface` is added nullable and then set
NOT NULL, which fails loudly rather than silently inventing data if some
deployment somehow does hold rows.
"""
from alembic import op


revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE word_lists ADD COLUMN IF NOT EXISTS language TEXT NOT NULL DEFAULT 'de'"
    )

    op.execute("ALTER TABLE word_list_items ADD COLUMN IF NOT EXISTS surface TEXT")
    # Separate statement so a non-empty legacy table raises here instead of
    # having a value fabricated for it.
    op.execute("ALTER TABLE word_list_items ALTER COLUMN surface SET NOT NULL")

    # NULL item_id = unresolved or ambiguous. item_type stays NOT NULL and
    # keeps describing what the entry WOULD bind to ('word' today).
    op.execute("ALTER TABLE word_list_items ALTER COLUMN item_id DROP NOT NULL")

    op.execute(
        "ALTER TABLE word_list_items "
        "DROP CONSTRAINT IF EXISTS word_list_items_list_id_item_id_item_type_key"
    )
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_word_list_items_list_surface
            ON word_list_items (list_id, lower(surface))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_word_list_items_list_surface")
    # Rows with a NULL item_id cannot satisfy the restored constraint; they only
    # exist because of this migration, so drop them rather than block the
    # downgrade.
    op.execute("DELETE FROM word_list_items WHERE item_id IS NULL")
    op.execute("ALTER TABLE word_list_items ALTER COLUMN item_id SET NOT NULL")
    op.execute(
        "ALTER TABLE word_list_items "
        "ADD CONSTRAINT word_list_items_list_id_item_id_item_type_key "
        "UNIQUE (list_id, item_id, item_type)"
    )
    op.execute("ALTER TABLE word_list_items DROP COLUMN IF EXISTS surface")
    op.execute("ALTER TABLE word_lists DROP COLUMN IF EXISTS language")
