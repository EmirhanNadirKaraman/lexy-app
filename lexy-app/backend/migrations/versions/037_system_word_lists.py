"""Let word_lists hold shared, built-in system lists alongside private user lists

Revision ID: 037
Revises: 036
Create Date: 2026-07-28

`word_lists.user_id` has been `NOT NULL` with `ON DELETE CASCADE` since
migration 001, so every list belongs to exactly one user and there is no way to
express "a list everyone can see". Built-in vocabulary lists (TODO #43 step 4)
need that concept.

Shape
-----
  - `user_id` becomes nullable; a system list has no owner.
  - `is_system BOOLEAN NOT NULL DEFAULT false` — existing rows are user lists,
    which is what the default encodes.
  - a CHECK constraint tying the two together.

**The CHECK is the load-bearing part, not the flag.** The read filters in
`word_list_service` widen from `user_id = $2` to
`(user_id = $2 OR is_system)`, and that widening is exactly where a private
list could leak. The constraint makes the two dangerous states
*unrepresentable*:

    is_system AND user_id IS NOT NULL   -- a "system" list someone owns
    NOT is_system AND user_id IS NULL   -- an ownerless private list

so a widened filter cannot return a hybrid row no matter what the application
does. A boolean alone would leave both states writable.

Why nullable-owner rather than a synthetic system user: a row in `users` that
must never authenticate is a standing auth hazard, and `ON DELETE CASCADE`
would make deleting it silently destroy every built-in list.

`uq_word_lists_system_name` is partial — unique among system lists only — so
seeding is idempotent by name while users keep the existing freedom to name
their own lists anything, including a name a system list already uses.

This migration only opens the shape. **No system list is created here**;
seeding is a separate reviewed script (TODO #43 step 4, phase 2), for the same
reason the catalog backfill was: 9,000-odd rows inside a migration are
unreviewable and cannot be re-run.
"""
from alembic import op


revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE word_lists ALTER COLUMN user_id DROP NOT NULL")
    op.execute(
        "ALTER TABLE word_lists "
        "ADD COLUMN IF NOT EXISTS is_system BOOLEAN NOT NULL DEFAULT false"
    )
    # Named explicitly so the tests (and a future downgrade) can address it.
    op.execute(
        """
        ALTER TABLE word_lists
            ADD CONSTRAINT word_lists_owner_ck
            CHECK (
                (is_system AND user_id IS NULL)
                OR (NOT is_system AND user_id IS NOT NULL)
            )
        """
    )
    # Partial: system list names are unique, user list names are not
    # constrained at all — unchanged from today.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_word_lists_system_name
            ON word_lists (name)
            WHERE is_system
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_word_lists_system_name")
    op.execute("ALTER TABLE word_lists DROP CONSTRAINT IF EXISTS word_lists_owner_ck")
    # System lists have no owner, so they cannot satisfy the restored NOT NULL.
    # They only exist because of this migration, so drop them rather than block
    # the downgrade — same choice migration 035 made for NULL item_ids.
    op.execute("DELETE FROM word_lists WHERE is_system")
    op.execute("ALTER TABLE word_lists DROP COLUMN IF EXISTS is_system")
    op.execute("ALTER TABLE word_lists ALTER COLUMN user_id SET NOT NULL")
