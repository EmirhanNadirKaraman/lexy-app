"""
Seed the built-in system vocabulary lists (TODO #43 step 4, phase 2).

Creates two shared, read-only lists from `data/final_result.txt`:

    column 0 → "Top German Words"
    column 1 → "German Verb & Phrase Patterns"

Both are `is_system` lists with `user_id = NULL` (migration 037): visible to
every user, editable by none. Surfaces resolve through `catalog_resolver`, the
same rule a user's own list uses, so a built-in entry binds to exactly the row
a hand-pasted one would.

**Makes no LLM calls.**

Usage
-----
Dry-run (the default — audits and prints, no writes):

    python scripts/seed_system_lists.py

Apply (creates the lists and their items):

    python scripts/seed_system_lists.py --apply

Idempotent. Re-running with --apply inserts 0 and reuses the existing lists.

Reads DB credentials from environment variables (`DB_NAME`, `DB_USER`,
`DB_PASSWORD`, `DB_HOST`, `DB_PORT`), same as the backend. Source `.env`
before running:

    set -a && source .env && set +a && python scripts/seed_system_lists.py

What it will and will not do
----------------------------
- **Keeps ambiguous and unresolved surfaces**, with `item_id = NULL`, exactly
  as a user-uploaded list does. The word list's 242 ambiguous entries are the
  most common words in the language (`ein`, `zu`, `im`, `auf`, `ich`) —
  dropping them would gut a "top words" list, and first-matching them would
  bind mastery to a coin-flip sense.
- Append-only. New surfaces are added; existing rows are never rewritten and a
  surface removed from the source file is **not** pruned, since deleting an
  item someone has already learned from would be surprising. Re-seed from
  scratch by deleting the system list first.
- Never touches user-owned lists.
- Does not use `data/words_4000_old.txt` — that file is the curated gloss
  source (`scripts/seed_gloss_cache.py`), and its headwords are a subset of
  column 0's.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Make `backend.*` importable when running this script from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "lexy-app"))

import asyncpg  # noqa: E402

from backend.services.system_list_seed_service import seed_system_lists  # noqa: E402

logger = logging.getLogger("seed_system_lists")


async def _open_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        min_size=1,
        max_size=2,
    )


async def main(apply: bool, source: Path | None) -> int:
    pool = await _open_pool()
    try:
        result = await seed_system_lists(pool, apply=apply, source=source)

        logger.info("Source rows read              : %d", result["rows_read"])
        logger.info("  column 0 (headwords)        : %d", result["source_col0"])
        logger.info("  column 1 (blueprints)       : %d", result["source_col1"])
        logger.info("Skipped malformed rows        : %d", result["skipped_rows"])

        for lst in result["lists"]:
            logger.info("")
            logger.info("List: %s", lst["name"])
            logger.info("  unique surfaces           : %d", lst["unique_surfaces"])
            logger.info(
                "  list row                  : %s",
                "created" if lst["list_created"] else "already present",
            )
            logger.info("  items inserted            : %d", lst["items_inserted"])
            logger.info("  items already present     : %d", lst["items_existed"])
            # Ambiguous and unresolved are kept, not dropped — see the module
            # docstring for why that matters most for the highest-frequency
            # words.
            logger.info("  resolved                  : %d", lst["resolved"])
            logger.info("    of which word           : %d", lst["word"])
            logger.info("    of which phrase         : %d", lst["phrase"])
            logger.info("  ambiguous (kept, id NULL) : %d", lst["ambiguous"])
            logger.info("  unresolved (kept, id NULL): %d", lst["unresolved"])

        logger.info("")
        if apply:
            total = sum(lst["items_inserted"] for lst in result["lists"])
            logger.info("APPLY: inserted %d list item(s) across %d list(s)",
                        total, len(result["lists"]))
        else:
            logger.info("Dry-run — no writes. Re-run with --apply to commit.")
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually create the lists and items. Without this flag the script is read-only.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Override the source TSV (defaults to data/final_result.txt).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(asyncio.run(main(args.apply, args.source)))
