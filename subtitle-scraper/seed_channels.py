"""
seed_channels.py

Populate the `channel` table from the bundled seed file:
  - seed_data/channels.json — list of {id, name, language} entries.

The DB is the runtime source of truth (`pipeline.py:load_channels` reads it
directly). This script is the bootstrap path: run once on a fresh deployment,
or re-run idempotently to upsert newly-added seed entries.

    python seed_channels.py
    python seed_channels.py --dry-run
"""

import argparse
import json
import logging
import os
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

from db_ssl import connect_kwargs

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).parent.parent / ".env")

SEED_PATH = Path(__file__).parent / "seed_data" / "channels.json"


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        **connect_kwargs(),  # S4: sslmode when DB_SSL_MODE enforces TLS
    )


def load_seed_channels(path: Path = SEED_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    # Legacy dict-keyed-by-language shape — flatten for safety.
    result = []
    for entries in data.values():
        result.extend(entries)
    return result


def seed(dry_run: bool = False) -> None:
    channels = load_seed_channels()
    logger.info("seed_data/channels.json: %d channels", len(channels))

    if dry_run:
        for ch in channels:
            logger.info(
                "[dry] upsert channel %r (%r, %s)",
                ch.get("id"), ch.get("name"), ch.get("language"),
            )
        logger.info("Dry run complete — nothing written.")
        return

    conn = connect()
    cursor = conn.cursor()

    inserted = 0
    skipped = 0
    for ch in channels:
        channel_id = (ch.get("id") or "").strip()
        if not channel_id:
            continue
        channel_name = (ch.get("name") or "").strip()
        language = ch.get("language") or None
        cursor.execute(
            """
            INSERT INTO channel (youtube_channel_id, channel_name, language)
            VALUES (%s, %s, %s)
            ON CONFLICT (youtube_channel_id) DO UPDATE
                SET channel_name = CASE
                        WHEN channel.channel_name = '' THEN EXCLUDED.channel_name
                        ELSE channel.channel_name
                    END,
                    language = COALESCE(channel.language, EXCLUDED.language)
            """,
            (channel_id, channel_name, language),
        )
        if cursor.rowcount:
            inserted += 1
        else:
            skipped += 1

    conn.commit()
    logger.info("Done. %d upserted, %d already present", inserted, skipped)

    cursor.close()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed channel table from bundled seed file.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    seed(dry_run=args.dry_run)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
