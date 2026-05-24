"""
Hole 10 — orphan SRS card cleanup.

An `srs_cards` row is *orphaned* when the matching
`user_word_knowledge (user_id, item_id, item_type)` row is gone. Orphans can
arise if a content row is deleted out from under the polymorphic key (e.g.
manual catalog pruning) or, historically, from now-removed code paths that
wrote SRS rows ahead of the knowledge row.

This is operational hygiene, not correctness. Orphaned cards never surface in
`/srs/due` because `review_service.get_due_cards` LEFT JOINs against
`user_word_knowledge` and (now) filters on it. But they cost disk space and
confuse direct DB queries.

Design choices
--------------
- Audit and apply share one SQL pattern so they can never drift.
- Default dry-run; `apply=True` performs the delete.
- Idempotent: a second `apply=True` run returns `deleted == 0`.
- NEVER touches `user_word_knowledge`, `word_table`, `phrase_table`,
  `grammar_rule_table`, or non-orphan `srs_cards` rows.
"""
from __future__ import annotations

import asyncpg


# Single source of the audit predicate.
_ORPHAN_PREDICATE = """
NOT EXISTS (
    SELECT 1 FROM user_word_knowledge uwk
     WHERE uwk.user_id   = sc.user_id
       AND uwk.item_id   = sc.item_id
       AND uwk.item_type = sc.item_type
)
"""


async def find_orphan_srs_cards(
    pool: asyncpg.Pool, user_id: str | None = None
) -> list[dict]:
    """
    Return every orphan `srs_cards` row identifier. Ordered by
    `(user_id, item_id, direction)` for deterministic reporting.

    `user_id` (optional) scopes the audit to one user. **Additive** — `None` is
    the global scan the cleanup script runs, unchanged. Tests pass their own
    user_id so the global scan/delete can't race another xdist worker's rows.
    The value is bound via `$1`; only a constant clause is concatenated.
    """
    where = _ORPHAN_PREDICATE
    args: list = []
    if user_id is not None:
        where = where + "   AND sc.user_id = $1::uuid\n"
        args.append(user_id)
    rows = await pool.fetch(
        f"""
        SELECT sc.card_id, sc.user_id, sc.item_id, sc.item_type, sc.direction
          FROM srs_cards sc
         WHERE {where}
         ORDER BY sc.user_id, sc.item_id, sc.direction
        """,
        *args,
    )
    return [
        {
            "card_id":   r["card_id"],
            "user_id":   str(r["user_id"]),
            "item_id":   r["item_id"],
            "item_type": r["item_type"],
            "direction": r["direction"],
        }
        for r in rows
    ]


async def cleanup_orphan_srs_cards(
    pool: asyncpg.Pool, apply: bool = False, user_id: str | None = None
) -> dict:
    """
    Audit (and optionally delete) orphan `srs_cards` rows.

    Returns:
        {
            "found":   int,   # number of orphan rows seen at audit time
            "deleted": int,   # rows actually removed (0 when apply=False)
            "dry_run": bool,
        }

    With `apply=False` (default) no rows are written. With `apply=True`, runs
    a single DELETE over the same predicate inside a transaction. A second
    `apply=True` pass returns `deleted == 0`.

    `user_id` (optional) scopes the audit + delete to one user. **Additive** —
    `None` is the global pass the script runs, unchanged. Tests pass their own
    user_id so the global DELETE can't remove another xdist worker's orphan rows
    mid-test (which skewed the count assertions).
    """
    orphans = await find_orphan_srs_cards(pool, user_id)
    found = len(orphans)

    if not apply or found == 0:
        return {"found": found, "deleted": 0, "dry_run": not apply}

    where = _ORPHAN_PREDICATE
    args: list = []
    if user_id is not None:
        where = where + "   AND sc.user_id = $1::uuid\n"
        args.append(user_id)
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await conn.execute(
                f"""
                DELETE FROM srs_cards sc
                 WHERE {where}
                """,
                *args,
            )
    # asyncpg returns "DELETE N"
    deleted = int(result.rsplit(" ", 1)[-1]) if result.startswith("DELETE") else 0
    return {"found": found, "deleted": deleted, "dry_run": False}
