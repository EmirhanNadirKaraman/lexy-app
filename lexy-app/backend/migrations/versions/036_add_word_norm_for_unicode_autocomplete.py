"""Unicode-correct normalized columns for autocomplete (word_table, phrase_blueprint)

Revision ID: 036
Revises: 035
Create Date: 2026-07-28

This database runs under the **C locale** (`datcollate=C`, `datctype=C`), so
Postgres's `lower()`, `upper()` and `ILIKE` fold **ASCII only**:

    lower('Öl')      -> 'Öl'      (unchanged)
    'Öl' ILIKE 'öl'  -> false
    lower('Ab')      -> 'ab'      (ASCII works — which is what hid this)

Every other case-insensitive lookup in the app was fixed by folding in Python
(`services/text_norm.normalize_key`) and letting SQL match on ids or equality.
Autocomplete is the one path where that does not work: `/api/suggest` fires on
every keystroke and needs an **indexed prefix** match, so it cannot fetch the
catalog and filter in Python (38,942 word rows, ~12 ms — per character).

So the normalization is persisted instead, as a generated column whose
expression reproduces `normalize_key` exactly:

    normalize_key(s) == unicodedata.normalize("NFC", s).strip().lower()
    word_norm        == lower(btrim(normalize(word, NFC)) COLLATE "und-x-icu")

Verified before writing this migration: the expression's output matched
Python's `normalize_key` on **all 38,942 `word_table` rows and all 2,829
`phrase_table` rows — zero mismatches**. All three functions are IMMUTABLE
(`provolatile = 'i'`), which `GENERATED ... STORED` requires.

**ß is deliberately preserved.** ICU `lower()` maps `Straße` → `straße`, NOT
`strasse`. That matches `normalize_key`'s use of `lower()` rather than
`casefold()`: German `word_table` holds `schließen` *and* `schliessen`,
`heißt` *and* `heisst`, `Großteil` *and* `Grossteil` as distinct rows, and
folding ß→ss would merge each pair.

**Requires an ICU-enabled Postgres.** `"und-x-icu"` is a built-in ICU
collation; this server has 784 of them. On a build without ICU the `ALTER
TABLE` fails outright — deliberately, since a silent fallback to C-locale
`lower()` would reintroduce exactly the bug this migration exists to fix.

Why a generated column rather than an application-maintained one: three code
paths insert into `word_table` (`word_seed_service`, `word_service`, and
`subtitle-scraper/pipeline.py`, the last using psycopg in a separate process).
A column any of them could forget to populate would silently make words
unsearchable. `GENERATED` needs no code change in any of them, and populates
every existing row during the ADD COLUMN rewrite — so there is no backfill
statement here on purpose.

`phrase_blueprint.lookup_key_norm` gets the same treatment for
`_suggest_phrases`, which had the same ASCII-only fold (`~*`) plus a raw-regex
sink — see docs/SECURITY.md S19/S20.

Cost: `word_table` is 20 MB / 38,942 rows and `phrase_blueprint` 6.9 MB /
43,549 rows. Both ADD COLUMNs rewrite the table under ACCESS EXCLUSIVE; brief
at this size, but not free on a much larger corpus.
"""
from alembic import op


revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Generated + STORED: populates existing rows during the rewrite and every
    # future insert, with no application code involved. See the module
    # docstring for why the expression is exactly this.
    op.execute(
        """
        ALTER TABLE word_table
            ADD COLUMN IF NOT EXISTS word_norm TEXT
            GENERATED ALWAYS AS (
                lower(btrim(normalize(word, NFC)) COLLATE "und-x-icu")
            ) STORED
        """
    )
    # text_pattern_ops makes `word_norm LIKE 'x%'` index-usable regardless of
    # the database collation — the same reason migration 032 used it for
    # `lower(word)`. `language` leads because every autocomplete query is
    # language-scoped.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_word_table_lang_word_norm
            ON word_table (language, word_norm text_pattern_ops)
        """
    )

    # Phrase autocomplete matched `lookup_key` with `~*`, which folds ASCII
    # only here. `lookup_key` is the blueprint verbatim (the scraper inserts
    # `(bp, bp)`), so it carries original casing and needs the same column.
    op.execute(
        """
        ALTER TABLE phrase_blueprint
            ADD COLUMN IF NOT EXISTS lookup_key_norm TEXT
            GENERATED ALWAYS AS (
                lower(btrim(normalize(lookup_key, NFC)) COLLATE "und-x-icu")
            ) STORED
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE phrase_blueprint DROP COLUMN IF EXISTS lookup_key_norm")
    op.execute("DROP INDEX IF EXISTS ix_word_table_lang_word_norm")
    op.execute("ALTER TABLE word_table DROP COLUMN IF EXISTS word_norm")
