"""Add tokens JSONB column to book_blocks and backfill with stable UUIDs

Revision ID: 025
Revises: 024
Create Date: 2026-03-29

`book_blocks.tokens` holds [{token_id, text, is_word}] per block. The
`token_id`s are the stable identity of a token across text edits — that is the
whole point of the column, and `book_service._retokenize_preserving_ids`
(services/book_service.py:329-367) goes out of its way to carry an existing
`token_id` onto the matching token of the repaired text.

**`reading_selections.anchors` points at these ids with no foreign key.**
Each anchor is `{block_id, token_id, surface}` (see migration 009 and
`routers/reading.py:_anchors_from_row`), so every saved reading selection is a
dangling reference to a `token_id` that only exists inside this JSONB column.
Postgres cannot enforce it, which is why `downgrade()` enforces it in Python —
see the guard below.

Append-only note (CLAUDE.md §12): the 2026-08-05 edit that added the downgrade
guard touched **only** `downgrade()`, the module-level SQL constants it reads,
and this prose. `upgrade()`, `_tokenize`, and the revision identifiers are
byte-for-byte what was applied, so no deployment that already ran 025 can
diverge from one that runs it now.
"""
from alembic import op
import sqlalchemy as sa
import json
import uuid
import regex as _re

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None

_SPLIT = _re.compile(r'(\p{L}[\p{L}\p{M}\'-]*)')
_WORD  = _re.compile(r'^\p{L}')

# Row-level predicate (alias `rs`): does this reading_selections row anchor to
# at least one token_id, i.e. is it a row this migration's downgrade would
# orphan? Rows saved BEFORE 025 have no `token_id` key in their anchors at all
# (hence the `"legacy"` fallback in routers/reading.py:64) and are correctly NOT
# counted — they never depended on the column, so they must not block a
# downgrade.
#
# `a->>'token_id' IS NOT NULL` excludes both the missing key and a JSON null.
# Deliberately written without the `?` jsonb operator: SQLAlchemy's `text()`
# treats `?` as a parameter marker under some drivers, and this exact string is
# also executed through asyncpg by tests/test_migration_025_downgrade.py.
#
# **The CASE is load-bearing.** `jsonb_array_elements` ERRORS on anything that
# isn't a JSON array ("cannot extract elements from a scalar/object"), and
# `anchors` is plain `JSONB NOT NULL DEFAULT '[]'` (migration 009:32) — nothing
# constrains it to an array. A single `'null'::jsonb` or object-shaped row would
# turn this guard from a refusal into a crash mid-downgrade, which is strictly
# worse than no guard. Non-array anchors are therefore expanded as `[]`, i.e.
# counted as zero dependents: `routers/reading.py:59-60` already coerces any
# non-list to `[]`, so such a row resolves to no anchors in the app today and
# dropping the column destroys nothing it had not already lost. The inner
# `jsonb_typeof(a) = 'object'` guards the same way one level down — `->>` is
# lenient on scalar elements in current Postgres, but not provably so across
# every version this migration may be replayed on.
_ANCHOR_DEPENDS_PREDICATE = """EXISTS (
       SELECT 1
         FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(rs.anchors) = 'array'
                     THEN rs.anchors
                     ELSE '[]'::jsonb
                END
              ) AS a
        WHERE jsonb_typeof(a) = 'object'
          AND a->>'token_id' IS NOT NULL
     )"""

# Ends with the EXISTS closing paren and no semicolon, so callers can append
# further conditions (the tests scope it with ` AND rs.user_id = $1::uuid`).
_DEPENDENT_ANCHORS_SQL = f"""
    SELECT COUNT(*) FROM reading_selections rs
     WHERE {_ANCHOR_DEPENDS_PREDICATE}"""

# The operator's opt-out, quoted verbatim in the refusal below. Derived from the
# same predicate on purpose: a hand-written copy would drift, and the first thing
# it would drift on is the CASE — handing the operator a DELETE that crashes on
# exactly the rows the CASE was added for.
_DELETE_DEPENDENTS_SQL = "DELETE FROM reading_selections rs WHERE " + " ".join(
    _ANCHOR_DEPENDS_PREDICATE.split()
)


def _tokenize(text: str) -> list[dict]:
    """Tokenize text, producing [{token_id: UUID, text: str, is_word: bool}].

    Every call mints fresh `uuid.uuid4()` values. Re-running the backfill
    therefore produces a completely different id set — there is no way to
    reconstruct the previous ids, which is what makes a downgrade/re-upgrade
    cycle unrecoverable for anything holding the old ones.
    """
    return [
        {"token_id": str(uuid.uuid4()), "text": p, "is_word": bool(_WORD.match(p))}
        for p in _SPLIT.split(text or "")
        if p
    ]


def upgrade() -> None:
    # Step 1: Add the column, nullable first
    op.execute("ALTER TABLE book_blocks ADD COLUMN tokens JSONB")

    # Step 2: Backfill using Python tokenizer
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("""
            SELECT block_id,
                   COALESCE(user_text_override,
                     CASE WHEN correction_status = 'approved' AND corrected_text IS NOT NULL
                          THEN corrected_text ELSE clean_text END,
                     ocr_text, '') AS display_text
              FROM book_blocks
        """)
    ).fetchall()

    for block_id, display_text in rows:
        tokens = _tokenize(display_text or "")
        conn.execute(
            sa.text(
                "UPDATE book_blocks SET tokens = :tokens WHERE block_id = :block_id"
            ),
            {"tokens": json.dumps(tokens), "block_id": block_id},
        )

    # Step 3: Set NOT NULL and default
    op.execute("""
        ALTER TABLE book_blocks
        ALTER COLUMN tokens SET NOT NULL,
        ALTER COLUMN tokens SET DEFAULT '[]'::jsonb
    """)


def downgrade() -> None:
    """Drop book_blocks.tokens — refused while any reading selection anchors to it.

    Dropping the column and re-upgrading is not reversible: the re-run mints
    new `uuid4()` ids for every token (see `_tokenize`), so each
    `reading_selections.anchors` entry silently becomes a reference to an id
    that no longer exists anywhere. Nothing errors — the selections just stop
    resolving to their place in the text, and the user's saved reading units
    lose their highlight positions permanently.

    Postgres cannot catch this (the anchors are JSONB with no FK), so the check
    is here. There is no force flag on purpose: an operator who genuinely wants
    the downgrade deletes the dependent `reading_selections` rows first, which
    makes the data loss an explicit, auditable act rather than a flag.

    The count is deliberately tolerant of malformed `anchors` — see the CASE
    note on `_ANCHOR_DEPENDS_PREDICATE`. A guard that raised on a stray JSON
    null would abort the downgrade mid-flight instead of refusing it cleanly.
    """
    conn = op.get_bind()
    dependent = conn.execute(sa.text(_DEPENDENT_ANCHORS_SQL)).scalar() or 0

    if dependent:
        raise RuntimeError(
            f"Refusing to downgrade 025: {dependent} reading_selections row(s) "
            "anchor to token_ids stored in book_blocks.tokens. Dropping the "
            "column and re-upgrading mints new UUIDs for every token, silently "
            "orphaning those anchors — the selections survive but no longer "
            "resolve to any text. To proceed anyway, delete the dependent rows "
            "first (they are the data being destroyed):\n"
            f"    {_DELETE_DEPENDENTS_SQL};\n"
            "Restore them from a backup afterwards if they matter."
        )

    op.execute("ALTER TABLE book_blocks DROP COLUMN tokens")
