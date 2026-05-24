"""
backfill_video_channels.py

For every video with channel_id IS NULL, uses yt-dlp to fetch the channel_id
(and name) from YouTube, upserts the channel row if needed, then sets
video.channel_id.

Usage:
    python backfill_video_channels.py
    python backfill_video_channels.py --dry-run
    python backfill_video_channels.py --limit 50
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


def fetch_channel_info(video_id: str) -> dict | None:
    """Return {channel_id, channel_name} for a video, or None on failure."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    opts = {"skip_download": True, "quiet": True, "no_warnings": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            channel_id = (info.get("channel_id") or "").strip()
            channel_name = (info.get("channel") or info.get("uploader") or "").strip()
            if channel_id:
                return {"channel_id": channel_id, "channel_name": channel_name}
    except Exception:
        logger.warning("[yt-dlp] %s metadata fetch failed", video_id, exc_info=True)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill video.channel_id via yt-dlp.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, metavar="N")
    args = parser.parse_args()

    conn = connect()
    cursor = conn.cursor()

    query = "SELECT video_id FROM video WHERE channel_id IS NULL ORDER BY video_id"
    if args.limit:
        query += f" LIMIT {args.limit}"
    cursor.execute(query)
    rows = cursor.fetchall()
    total = len(rows)

    if total == 0:
        logger.info("No videos with NULL channel_id. Nothing to do.")
        conn.close()
        return

    logger.info("Found %d video(s) with NULL channel_id.", total)
    if args.dry_run:
        logger.info("DRY RUN — no changes will be written.")

    updated = 0
    failed = 0

    for i, (video_id,) in enumerate(rows, start=1):
        info = fetch_channel_info(video_id)

        if not info:
            logger.info("[%d/%d] %s -> (not found)", i, total, video_id)
            failed += 1
            time.sleep(0.5)
            continue

        channel_id = info["channel_id"]
        channel_name = info["channel_name"]
        logger.info("[%d/%d] %s -> %s (%s)", i, total, video_id, channel_name, channel_id)

        if not args.dry_run:
            cursor.execute(
                """
                INSERT INTO channel (youtube_channel_id, channel_name)
                VALUES (%s, %s)
                ON CONFLICT (youtube_channel_id) DO UPDATE
                    SET channel_name = CASE
                            WHEN channel.channel_name = '' THEN EXCLUDED.channel_name
                            ELSE channel.channel_name
                        END
                RETURNING id
                """,
                (channel_id, channel_name),
            )
            internal_id = cursor.fetchone()[0]
            cursor.execute(
                "UPDATE video SET channel_id = %s WHERE video_id = %s",
                (internal_id, video_id),
            )
            conn.commit()
            updated += 1

        time.sleep(0.5)

    conn.close()

    if not args.dry_run:
        logger.info("Done. Updated: %d, failed: %d", updated, failed)
    else:
        logger.info("Dry run complete.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
