"""
Pre-seed curated item glosses into the permanent LLM cache (N5a / TODO #43 step 2).

`review_service.get_due_cards` asks `llm_service.translate_item_gloss` for an
English gloss on every non-grammar due card, which is an LLM call per item that
has never been glossed — on the review hot path. `data/words_4000_old.txt`
already holds human translations for the same headwords the catalog was seeded
from, so this writes them into the cache instead.

**Makes no LLM calls.**

Usage
-----
Dry-run (the default — audits and prints, no writes):

    python scripts/seed_gloss_cache.py

Apply (writes the cache rows):

    python scripts/seed_gloss_cache.py --apply

Idempotent. Re-running with --apply yields `inserted == 0`.

Reads DB credentials from environment variables (`DB_NAME`, `DB_USER`,
`DB_PASSWORD`, `DB_HOST`, `DB_PORT`), same as the backend. Source `.env`
before running:

    set -a && source .env && set +a && python scripts/seed_gloss_cache.py

What it will and will not do
----------------------------
- Writes rows under the sentinel model `curated:words_4000_old`, never a real
  model id — honest provenance, and `translate_item_gloss` checks that key
  before the model-specific one so the rows survive an `LLM_MODEL` switch.
- Seeds **words only**, and only surfaces that exist in `word_table`, since a
  key nothing looks up is dead weight.
- Skips multi-sense (`1) …; 2) …`) and long translations — those are
  definitions, not the 1-4 word gloss the prompt asks for, and are left to the
  LLM.
- Never seeds phrases: only 29 curated headwords match a phrase surface, and
  the translations are for the bare headword. That needs its own source.
- Never overwrites an existing cache row (`ON CONFLICT DO NOTHING`).
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

from backend.services.gloss_seed_service import seed_glosses  # noqa: E402

logger = logging.getLogger("seed_gloss_cache")


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
        result = await seed_glosses(pool, apply=apply, source=source)

        logger.info("Source rows read              : %d", result["rows_read"])
        logger.info("Gloss-shaped candidates       : %d", result["candidates"])
        logger.info("  present in word_table       : %d", result["in_catalog"])
        logger.info("  not in word_table (skipped) : %d", result["not_in_catalog"])
        logger.info("  already cached              : %d", result["already_cached"])
        logger.info("  missing                     : %d", result["missing"])
        # Skips are reported separately because they mean different things: an
        # artefact is a source-data defect, while multi-sense/long rows are
        # perfectly good translations that simply are not glosses.
        logger.info(
            "Skipped, multi-entry cell     : %d  (source-data defect)",
            result["skipped_artefact"],
        )
        logger.info(
            "Skipped, multi-sense          : %d  (left to the LLM)",
            result["skipped_multisense"],
        )
        logger.info(
            "Skipped, too long             : %d  (left to the LLM)",
            result["skipped_long"],
        )
        logger.info("Skipped, empty                : %d", result["skipped_empty"])
        for text, gloss in result["sample"]:
            logger.info("    e.g. %r -> %r", text, gloss)

        if apply:
            logger.info("APPLY: inserted %d curated gloss row(s)", result["inserted"])
        else:
            logger.info("Dry-run — no writes. Re-run with --apply to commit.")
        return result["missing"]
    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the cache rows. Without this flag the script is read-only.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help="Override the source TSV (defaults to data/words_4000_old.txt).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    sys.exit(0 if asyncio.run(main(args.apply, args.source)) >= 0 else 1)
