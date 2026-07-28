"""
Seed the built-in system vocabulary lists (TODO #43 step 4, phase 2).

Phase 1 (migration 037) opened the shape: `word_lists.user_id` is nullable and
`is_system` marks a shared list every user can read but none can edit. This
creates the actual content.

Two lists, both from `data/final_result.txt` — the file that already seeds
`phrase_table` at startup, so its surfaces are the ones the rest of the app
knows:

  column 0 → "Top German Words"              (headwords)
  column 1 → "German Verb & Phrase Patterns" (blueprints)

`data/words_4000_old.txt` is deliberately NOT used here. Its headwords are a
subset of column 0's, and it already has a job — it is the curated gloss source
(`gloss_seed_service`). Using it for lists too would mean two files claiming to
define the same list.

Resolution goes through `catalog_resolver`, the same rule vocabulary lists and
interactive reading use, so a system list binds a surface to exactly the row a
user's own list would. That matters for the two states this seeder deliberately
preserves rather than dropping:

  * **ambiguous** — several catalog rows share the surface. 242 of the word
    list's entries are in this state, and they are the most common words in the
    language (`ein`, `zu`, `im`, `auf`, `ich`). Dropping them would quietly
    remove the top of a "top words" list; first-matching them would bind
    mastery to a coin-flip sense (the W3 / Hole 2 rule).
  * **unresolved** — no catalog row yet. Stored so the list still round-trips
    and can bind later.

Both are stored with `item_id = NULL`, exactly as a user-uploaded list stores
them.

Idempotency has two levels, both leaning on constraints rather than on
read-then-write races:
  * lists — `ON CONFLICT (name) WHERE is_system DO NOTHING`, inferring the
    partial unique index migration 037 added;
  * items — `ON CONFLICT (list_id, lower(surface)) DO NOTHING`, inferring
    `uq_word_list_items_list_surface` from migration 035.

Re-running is therefore append-only: new surfaces in the source file are added,
existing rows are left alone. A source file that *removes* a surface will not
prune it — that is a deliberate choice, since deleting a list item a user has
already learned from would be surprising. Re-seeding from scratch means
deleting the system list first.

Makes no LLM calls.
"""
from __future__ import annotations

from pathlib import Path

import asyncpg

from . import catalog_resolver
from .text_norm import normalize_key

#: Repo-root `data/final_result.txt`, resolved from this file so the script
#: works from any cwd (the `os.chdir` trap in docs/COMMON_ERRORS.md).
DEFAULT_SOURCE = (
    Path(__file__).resolve().parent.parent.parent.parent / "data" / "final_result.txt"
)

LANGUAGE = "de"

WORD_LIST_NAME = "Top German Words"
WORD_LIST_DESCRIPTION = (
    "The most common German headwords, from data/final_result.txt (column 0), "
    "stored exactly as the source writes them. Nouns keep their article "
    "(\u201cdas Haus\u201d), so those entries bind to phrase_table collocations "
    "rather than to a bare word \u2014 the article is part of the learning unit, "
    "and \u201cdas Haus\u201d and \u201cHaus\u201d stay independently trackable. "
    "Entries matching several catalog senses are kept and shown as ambiguous "
    "rather than bound to one of them."
)

PHRASE_LIST_NAME = "German Verb & Phrase Patterns"
PHRASE_LIST_DESCRIPTION = (
    "German verb and phrase blueprints, from data/final_result.txt (column 1) — "
    "the same source that seeds phrase_table, so entries bind to the phrases "
    "chat and SRS already use."
)


def read_columns(source: Path | None = None) -> tuple[list[str], list[str], int]:
    """`(column 0 values, column 1 values, skipped_rows)`.

    `final_result.txt` is a 2-column TSV. A line with no tab is malformed and
    counted in `skipped`. A whitespace-only line is not malformed, just empty,
    so it is skipped silently. An individual empty *cell* simply contributes
    nothing to its column — that is normal, not an error.
    """
    path = Path(source) if source is not None else DEFAULT_SOURCE
    col0: list[str] = []
    col1: list[str] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            skipped += 1
            continue
        head, blueprint = parts[0].strip(), parts[1].strip()
        if head:
            col0.append(head)
        if blueprint:
            col1.append(blueprint)
    return col0, col1, skipped


def dedupe(surfaces: list[str]) -> list[str]:
    """Case-insensitive dedupe preserving file order, first spelling winning.

    Uses `normalize_key`, which is *stricter* than the DB's
    `(list_id, lower(surface))` index — that index folds under the C locale and
    would not treat `Öl`/`öl` as duplicates. Deduping here means the insert can
    never hit that constraint for a case pair the database would have missed.
    """
    seen: set[str] = set()
    out: list[str] = []
    for s in surfaces:
        key = normalize_key(s)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


async def _upsert_list(
    conn: asyncpg.Connection, name: str, description: str, language: str,
) -> tuple[int, bool]:
    """`(list_id, created)`. Reuses an existing system list of the same name."""
    row = await conn.fetchrow(
        """
        INSERT INTO word_lists (user_id, name, language, description, is_system)
        VALUES (NULL, $1, $2, $3, true)
        ON CONFLICT (name) WHERE is_system DO NOTHING
        RETURNING list_id
        """,
        name, language, description,
    )
    if row is not None:
        return row["list_id"], True
    existing = await conn.fetchval(
        "SELECT list_id FROM word_lists WHERE name = $1 AND is_system", name,
    )
    return existing, False


async def seed_one_list(
    pool: asyncpg.Pool,
    name: str,
    description: str,
    surfaces: list[str],
    apply: bool = False,
    language: str = LANGUAGE,
) -> dict:
    """Audit (and optionally create) one system list and its items."""
    unique = dedupe(surfaces)
    resolved = await catalog_resolver.resolve_surfaces(pool, unique, language)

    counts = {"resolved": 0, "ambiguous": 0, "unresolved": 0, "word": 0, "phrase": 0}
    planned: list[tuple[int | None, str, str]] = []
    for surface in unique:
        res = resolved[normalize_key(surface)]
        if res.item_id is not None:
            counts["resolved"] += 1
            counts[res.item_type] += 1
        elif res.status == catalog_resolver.STATUS_AMBIGUOUS:
            counts["ambiguous"] += 1
        else:
            counts["unresolved"] += 1
        planned.append((res.item_id, res.item_type, surface))

    result = {
        "name": name,
        "unique_surfaces": len(unique),
        "list_created": False,
        "list_existed": False,
        "items_inserted": 0,
        "items_existed": 0,
        **counts,
        "dry_run": not apply,
    }

    if not apply:
        existing = await pool.fetchval(
            "SELECT list_id FROM word_lists WHERE name = $1 AND is_system", name,
        )
        result["list_existed"] = existing is not None
        result["list_created"] = existing is None
        if existing is not None:
            have = await pool.fetchval(
                "SELECT count(*) FROM word_list_items WHERE list_id = $1", existing,
            )
            result["items_existed"] = have
            result["items_inserted"] = max(0, len(unique) - have)
        else:
            result["items_inserted"] = len(unique)
        return result

    async with pool.acquire() as conn:
        async with conn.transaction():
            list_id, created = await _upsert_list(conn, name, description, language)
            result["list_id"] = list_id
            result["list_created"] = created
            result["list_existed"] = not created

            # RETURNING so the count is rows this call actually wrote. Counting
            # the list's items afterwards would include rows a previous run
            # inserted — the over-reporting bug the catalog backfill shipped.
            inserted = await conn.fetch(
                """
                INSERT INTO word_list_items (list_id, item_id, item_type, surface)
                SELECT $1, i, t, s
                  FROM unnest($2::int[], $3::text[], $4::text[]) AS u(i, t, s)
                ON CONFLICT (list_id, lower(surface)) DO NOTHING
                RETURNING id
                """,
                list_id,
                [p[0] for p in planned],
                [p[1] for p in planned],
                [p[2] for p in planned],
            )
            result["items_inserted"] = len(inserted)
            result["items_existed"] = len(unique) - len(inserted)

    result["dry_run"] = False
    return result


async def seed_system_lists(
    pool: asyncpg.Pool,
    apply: bool = False,
    source: Path | None = None,
    language: str = LANGUAGE,
) -> dict:
    """Seed both built-in lists. Returns per-list results plus source counts."""
    col0, col1, skipped = read_columns(source)

    words = await seed_one_list(
        pool, WORD_LIST_NAME, WORD_LIST_DESCRIPTION, col0, apply, language,
    )
    phrases = await seed_one_list(
        pool, PHRASE_LIST_NAME, PHRASE_LIST_DESCRIPTION, col1, apply, language,
    )

    return {
        "rows_read": len(col0) + len(col1),
        "source_col0": len(col0),
        "source_col1": len(col1),
        "skipped_rows": skipped,
        "lists": [words, phrases],
        "dry_run": not apply,
    }
