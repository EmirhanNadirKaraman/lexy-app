"""
backfill_channel_names.py

Fetches channel name and language for channels in the `channel` table
that have an empty channel_name. Uses yt-dlp — no API key required.

For each nameless channel it picks one of its existing videos from the
`video` table and extracts channel metadata from that video. If no video
exists yet, it falls back to fetching the channel page directly.

Usage:
    python backfill_channel_names.py
    python backfill_channel_names.py --dry-run
    python backfill_channel_names.py --limit 50
"""

import argparse
import logging
import os
import time
from pathlib import Path

import psycopg2
import yt_dlp
from dotenv import load_dotenv

from db_ssl import connect_kwargs

logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).parent.parent / ".env")


def connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        **connect_kwargs(),  # S4: sslmode when DB_SSL_MODE enforces TLS
    )


def fetch_channel_info_via_video(video_id: str) -> dict | None:
    """Extract channel_name from a known video_id using yt-dlp."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {"skip_download": True, "quiet": True, "no_warnings": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            name = (info.get("channel") or info.get("uploader") or "").strip()
            if name:
                return {"channel_name": name}
    except Exception:
        logger.warning("[yt-dlp] video %s metadata fetch failed", video_id, exc_info=True)
    return None


def fetch_channel_info_via_channel_page(channel_id: str) -> dict | None:
    """Extract channel name directly from the channel page using yt-dlp."""
    url = f"https://www.youtube.com/channel/{channel_id}"
    opts = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": "1",  # only fetch enough to get channel metadata
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            name = (info.get("channel") or info.get("uploader") or info.get("title") or "").strip()
            if name:
                return {"channel_name": name}
    except Exception:
        logger.warning("[yt-dlp] channel page %s fetch failed", channel_id, exc_info=True)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill empty channel names via yt-dlp.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, metavar="N")
    args = parser.parse_args()

    conn = connect()
    cursor = conn.cursor()

    query = "SELECT youtube_channel_id FROM channel WHERE channel_name = '' ORDER BY youtube_channel_id"
    if args.limit:
        query += f" LIMIT {args.limit}"
    cursor.execute(query)
    rows = cursor.fetchall()
    total = len(rows)

    if total == 0:
        logger.info("No channels with empty names. Nothing to do.")
        conn.close()
        return

    logger.info("Found %d channel(s) with empty names.", total)
    if args.dry_run:
        logger.info("DRY RUN — no changes will be written.")

    updated = 0
    failed = 0

    for i, (channel_id,) in enumerate(rows, start=1):
        # Try via an existing video first — cheaper and more reliable.
        cursor.execute(
            """
            SELECT v.video_id
              FROM video v
              JOIN channel ch ON ch.id = v.channel_id
             WHERE ch.youtube_channel_id = %s
             LIMIT 1
            """,
            (channel_id,),
        )
        video_row = cursor.fetchone()

        info = None
        if video_row:
            info = fetch_channel_info_via_video(video_row[0])
        if not info:
            info = fetch_channel_info_via_channel_page(channel_id)

        if info:
            logger.info("[%d/%d] %s -> %s", i, total, channel_id, info["channel_name"])
            if not args.dry_run:
                cursor.execute(
                    "UPDATE channel SET channel_name = %s WHERE youtube_channel_id = %s",
                    (info["channel_name"], channel_id),
                )
                conn.commit()
                updated += 1
        else:
            logger.info("[%d/%d] %s -> (not found)", i, total, channel_id)
            failed += 1

        time.sleep(0.5)

    conn.close()

    if not args.dry_run:
        logger.info("Done. Updated: %d, failed: %d", updated, failed)
    else:
        logger.info("Dry run complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
