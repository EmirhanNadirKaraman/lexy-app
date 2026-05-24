"""
backfill_categories.py

One-off script to populate the `category` column for videos already in the
database that still have the default value 'other'.

Uses yt-dlp to fetch category metadata — no YouTube Data API key required.

Usage:
    python backfill_categories.py
    python backfill_categories.py --dry-run          # print what would change, no writes
    python backfill_categories.py --limit 100        # process at most N videos
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import psycopg2
import yt_dlp
from dotenv import load_dotenv

from db_ssl import connect_kwargs

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB connection (same as pipeline.py)
# ---------------------------------------------------------------------------

def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        **connect_kwargs(),  # S4: sslmode when DB_SSL_MODE enforces TLS
    )


# ---------------------------------------------------------------------------
# Category fetching
# ---------------------------------------------------------------------------

def fetch_category(video_id: str) -> str:
    """
    Use yt-dlp to extract the category of a YouTube video.
    Returns the first entry in `categories`, falling back to `genre`,
    then to 'other' if nothing is found or an error occurs.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            categories = info.get("categories") or []
            if categories:
                return categories[0]
            genre = (info.get("genre") or "").strip()
            if genre:
                return genre
    except Exception:
        logger.warning("[yt-dlp] Error fetching category for %s", video_id, exc_info=True)
    return "other"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill video categories using yt-dlp.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print categories without writing to the database.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process at most N videos.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    conn = connect()
    cursor = conn.cursor()

    query = "SELECT video_id FROM video WHERE category = 'other' ORDER BY video_id"
    if args.limit:
        query += f" LIMIT {args.limit}"

    cursor.execute(query)
    rows = cursor.fetchall()
    total = len(rows)

    if total == 0:
        logger.info("No videos with category='other' found. Nothing to do.")
        conn.close()
        return

    logger.info("Found %d video(s) with category='other'.", total)
    if args.dry_run:
        logger.info("DRY RUN — no changes will be written.")

    updated = 0
    skipped = 0

    for i, (video_id,) in enumerate(rows, start=1):
        category = fetch_category(video_id)
        logger.info("[%d/%d] %s -> %s", i, total, video_id, category)

        if args.dry_run:
            continue

        if category != "other":
            cursor.execute(
                "UPDATE video SET category = %s WHERE video_id = %s",
                (category, video_id),
            )
            conn.commit()
            updated += 1
        else:
            skipped += 1

        # Be polite to YouTube's servers
        time.sleep(0.5)

    conn.close()

    if not args.dry_run:
        logger.info("Done. Updated: %d, still 'other': %d", updated, skipped)
    else:
        logger.info("Dry run complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
