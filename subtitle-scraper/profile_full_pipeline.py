"""
Profiler for the full subtitle scraper pipeline (with real DB I/O).

Stdout is the product: every print() emits a timing/throughput row that the
user reads to compare runs. Intentionally left as print(), not logging.

Instruments pipeline.py to measure actual performance including:
- Time per video (fetch, NLP, phrase extraction, DB writes)
- Time per populate() call
- Time per insert_phrases() call
- Overall throughput

Usage:
    # Process pending requests only (quick test):
    python profile_full_pipeline.py --requests-only

    # Process one active channel:
    python profile_full_pipeline.py --channel <CHANNEL_ID>

    # Full pipeline (all active channels):
    python profile_full_pipeline.py --full
"""

import argparse
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

_SCRAPER_DIR = str(Path(__file__).resolve().parent)
if _SCRAPER_DIR not in sys.path:
    sys.path.insert(0, _SCRAPER_DIR)

# E402: must import AFTER the sys.path.insert above — `pipeline` is a sibling
# module in subtitle-scraper/, not an installed package (TODO #3). Do not hoist.
import pipeline  # noqa: E402
from pipeline import (  # noqa: E402
    connect, load_channels, process_pending_requests, _process_channel_request, populate,
)

# Profiling state
profile_stats = {
    "videos_processed": 0,
    "videos_failed": 0,
    "total_populate_time": 0.0,
    "total_insert_phrases_time": 0.0,
    "total_transcript_fetch_time": 0.0,
    "total_nlp_time": 0.0,
    "total_db_time": 0.0,
    "video_times": [],  # (video_id, title, populate_time, phrase_time)
}

# Monkey-patch populate to instrument it
_original_populate = pipeline.populate

def instrumented_populate(*args, **kwargs):
    t0 = time.perf_counter()
    _original_populate(*args, **kwargs)
    elapsed = time.perf_counter() - t0
    profile_stats["total_populate_time"] += elapsed
    profile_stats["videos_processed"] += 1
    video_id = kwargs.get("video_id") or args[3]
    title = kwargs.get("title") or args[4]
    profile_stats["video_times"].append((video_id, title, elapsed, 0.0))

pipeline.populate = instrumented_populate

# Monkey-patch insert_phrases to instrument it
_original_insert_phrases = pipeline.insert_phrases

def instrumented_insert_phrases(cursor, sentence_ids, docs, language):
    t0 = time.perf_counter()
    _original_insert_phrases(cursor, sentence_ids, docs, language)
    elapsed = time.perf_counter() - t0
    profile_stats["total_insert_phrases_time"] += elapsed
    if profile_stats["video_times"]:
        # Attach phrase time to last video
        last = profile_stats["video_times"][-1]
        profile_stats["video_times"][-1] = (last[0], last[1], last[2], elapsed)

pipeline.insert_phrases = instrumented_insert_phrases


def report_stats():
    """Print profiling report."""
    total_time = (
        profile_stats["total_populate_time"] +
        profile_stats["total_insert_phrases_time"]
    )

    print("\n" + "="*70)
    print("FULL PIPELINE PROFILING REPORT")
    print("="*70)

    print("\n── Summary ────────────────────────────────────────────────────")
    print(f"  Videos processed:     {profile_stats['videos_processed']}")
    print(f"  Videos failed:        {profile_stats['videos_failed']}")
    print(f"  Total populate time:  {profile_stats['total_populate_time']:.3f}s")
    print(f"  Total phrase time:    {profile_stats['total_insert_phrases_time']:.3f}s")
    print(f"  Combined:             {total_time:.3f}s")

    if profile_stats["videos_processed"] > 0:
        avg_populate = profile_stats["total_populate_time"] / profile_stats["videos_processed"]
        avg_phrase = profile_stats["total_insert_phrases_time"] / profile_stats["videos_processed"]
        print(f"  Avg per video:        {avg_populate:.3f}s populate + {avg_phrase:.3f}s phrases")
        print(f"  Throughput:           {profile_stats['videos_processed'] / (total_time + 0.001):.1f} videos/sec")

    print("\n── Per-video breakdown (slowest first) ────────────────────────")
    # Sort by total time (populate + phrase)
    sorted_videos = sorted(
        profile_stats["video_times"],
        key=lambda x: x[2] + x[3],
        reverse=True
    )

    for video_id, title, pop_time, phrase_time in sorted_videos[:20]:
        total_vid = pop_time + phrase_time
        print(f"  {title[:40]:<40} | {pop_time:6.3f}s pop + {phrase_time:6.3f}s phrase = {total_vid:6.3f}s")

    if len(sorted_videos) > 20:
        print(f"  ... and {len(sorted_videos) - 20} more")

    print("="*70 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Profile the full subtitle scraper pipeline with real DB I/O"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--requests-only", action="store_true",
                      help="Process pending content requests only")
    mode.add_argument("--channel", metavar="CHANNEL_ID",
                      help="Process a single channel")
    mode.add_argument("--full", action="store_true",
                      help="Full pipeline: scan all active channels")

    args = parser.parse_args()

    connection = connect()
    cursor = connection.cursor()

    # Load lookup tables
    cursor.execute("SELECT video_id FROM video")
    processed_videos = {row[0] for row in cursor.fetchall()}

    cursor.execute("SELECT video_id FROM video_blacklist")
    blacklist = {row[0] for row in cursor.fetchall()}

    cursor.execute("SELECT language || '_' || rule, rule_id FROM grammar_rule")
    sentence_types = {row[0]: row[1] for row in cursor.fetchall()}

    cursor.execute("SELECT word, pos, lemma FROM word_table")
    db_words = {(r[0], r[1], r[2]) for r in cursor.fetchall()}

    nlp_cache = {}

    t_start = time.perf_counter()

    if args.requests_only:
        print("Processing pending content requests...")
        process_pending_requests(
            cursor, connection, nlp_cache, sentence_types, db_words,
            processed_videos, blacklist
        )

    elif args.channel:
        print(f"Processing single channel: {args.channel}")
        _process_channel_request(
            cursor, connection, args.channel, request_id=0,
            nlp_cache=nlp_cache, sentence_types=sentence_types,
            db_words=db_words, processed_videos=processed_videos,
            blacklist=blacklist
        )

    elif args.full:
        print("Running full pipeline...")
        from scrapetube import scrapetube

        channels = load_channels(cursor)
        print(f"Active channels: {len(channels)}")

        total = 0
        channel_iters = [(ch, iter(scrapetube.get_channel(ch["id"]))) for ch in channels]

        while channel_iters:
            next_round = []
            for channel, vid_iter in channel_iters:
                channel_id = channel["id"]
                channel_name = channel["name"] or channel_id
                language = channel["language"]

                from pipeline import upsert_channel
                internal_channel_id = upsert_channel(cursor, channel_id, channel_name, language)

                # Advance to next unprocessed video
                video = None
                while True:
                    try:
                        candidate = next(vid_iter)
                    except StopIteration:
                        break
                    vid_id = candidate["videoId"]
                    if vid_id in blacklist or vid_id in processed_videos:
                        continue
                    video = candidate
                    break

                if video is None:
                    continue

                video_id = video["videoId"]
                from pipeline import get_transcript
                transcript_obj, detected_lang = get_transcript(video_id, language)

                if transcript_obj is None:
                    profile_stats["videos_failed"] += 1
                    blacklist.add(video_id)
                    cursor.execute(
                        "INSERT INTO video_blacklist (video_id) VALUES (%s) ON CONFLICT DO NOTHING",
                        (video_id,),
                    )
                    connection.commit()
                    next_round.append((channel, vid_iter))
                    continue

                if detected_lang not in nlp_cache:
                    try:
                        import spacy
                        nlp_cache[detected_lang] = spacy.load(
                            pipeline.LANG_MODEL_MAP[detected_lang]
                        )
                        nlp_cache[detected_lang].select_pipes(
                            enable=["tok2vec", "tagger", "attribute_ruler", "lemmatizer"]
                        )
                    except Exception:
                        profile_stats["videos_failed"] += 1
                        next_round.append((channel, vid_iter))
                        continue

                title = video["title"]["runs"][0]["text"]
                thumbnail = video["thumbnail"]["thumbnails"][-1]["url"]

                fetched = None
                for attempt in range(3):
                    try:
                        fetched = transcript_obj.fetch()
                        break
                    except Exception as e:
                        print(f"    Fetch attempt {attempt + 1}/3 failed for {video_id}: {e}")
                        time.sleep(2 ** attempt)

                if fetched is None:
                    profile_stats["videos_failed"] += 1
                    next_round.append((channel, vid_iter))
                    continue

                from pipeline import fetch_category
                category = fetch_category(video_id)

                populate(
                    cursor=cursor, connection=connection, db_words=db_words,
                    video_id=video_id, title=title, thumbnail_url=thumbnail,
                    transcript=fetched, language=detected_lang,
                    dialect=transcript_obj.language_code,
                    nlp=nlp_cache[detected_lang], sentence_types=sentence_types,
                    category=category, channel_id=internal_channel_id,
                )

                processed_videos.add(video_id)
                total += 1
                print(f"  [{total}] {channel_name}: {title} ({detected_lang})")
                next_round.append((channel, vid_iter))

            channel_iters = next_round

    elapsed = time.perf_counter() - t_start
    print(f"\nElapsed: {elapsed:.2f}s")

    connection.close()

    report_stats()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[interrupted]")
        report_stats()
