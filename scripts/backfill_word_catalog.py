"""
Seed missing German catalog words from `data/final_result.txt` (N5a step 1).

`word_table` is scraper-derived and misses much ordinary vocabulary, so about
half of `final_result.txt`'s headwords report `unresolved` in a vocabulary
list. This inserts the missing ones.

Reads **column 0 only** (the headword). Column 1 holds phrase blueprints,
which belong to `phrase_table` — that seeding runs at backend startup and is
NOT touched here.

Usage
-----
Dry-run (the default — audits and prints, no writes):

    python scripts/backfill_word_catalog.py

Apply (inserts the missing rows):

    python scripts/backfill_word_catalog.py --apply

Idempotent. Re-running with --apply yields `inserted == 0`.

Reads DB credentials from environment variables (`DB_NAME`, `DB_USER`,
`DB_PASSWORD`, `DB_HOST`, `DB_PORT`), same as the backend. Source `.env`
before running:

    set -a && source .env && set +a && python scripts/backfill_word_catalog.py

What it will and will not do
----------------------------
- Inserts only surfaces absent from `word_table` under
  `services/text_norm.normalize_key` (Unicode casefold done in Python — this
  database's C-locale `lower()` folds ASCII only, so `Öl` and `öl` would
  otherwise look like different words and fork the surface).
- Writes `pos=''`, `tag=''`, `lemma == word` — matching every existing German
  row. A real POS would NOT conflict with an existing `pos=''` row and would
  create a duplicate, which vocabulary lists then report as `ambiguous`.
- Never updates an existing row.
- Never writes `phrase_table`, and never imports column 1.
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

from backend.services.word_seed_service import seed_word_catalog  # noqa: E402

logger = logging.getLogger("backfill_word_catalog")


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
        result = await seed_word_catalog(pool, apply=apply, source=source)

        logger.info("Source rows read            : %d", result["rows_read"])
        logger.info("Clean candidates            : %d", result["candidates"])
        logger.info("  already in word_table     : %d", result["already_present"])
        logger.info("  missing                   : %d", result["missing"])
        # Two different reasons, reported separately: multi-word entries are
        # real vocabulary that belongs to phrase_table, artefacts are a
        # source-data defect. Only the second is a problem.
        logger.info(
            "Skipped, multi-word         : %d  (belong to phrase_table)",
            result["skipped_multiword"],
        )
        for sample in result["skipped_samples"].get("multiword", []):
            logger.info("    e.g. %r", sample)
        logger.info(
            "Skipped, multi-entry cell   : %d  (source-data defect)",
            result["skipped_artefact"],
        )
        for sample in result["skipped_samples"].get("artefact", []):
            logger.info("    e.g. %r", sample)
        if result["inserted_samples"]:
            for s in result["inserted_samples"]:
                logger.info("    would insert e.g. %r", s)

        if apply:
            logger.info("APPLY: inserted %d word_table row(s)", result["inserted"])
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
        help="Actually insert the missing rows. Without this flag the script is read-only.",
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
    sys.exit(0 if asyncio.run(main(args.apply, args.source)) >= 0 else 1)
