"""
Profiler for the subtitle parsing pipeline.

Stdout is the product: every print() emits a timing/coverage row that the user
reads to compare runs. Intentionally left as print(), not logging.

Usage:
    # Profile using a real transcript fetched from YouTube (reads/writes real DB):
    python profile_pipeline.py --video-id <VIDEO_ID> [--lang de]

    # Profile from a saved transcript JSON (skips YouTube fetch):
    python profile_pipeline.py --transcript transcript.json [--lang de]

    # Dry-run: profile NLP + phrase work only, skip all DB writes:
    python profile_pipeline.py --video-id <VIDEO_ID> --dry-run

    # Save transcript to JSON for repeated profiling without network calls:
    python profile_pipeline.py --video-id <VIDEO_ID> --save-transcript transcript.json

    # Show full cProfile output sorted by cumulative time (default: tottime):
    python profile_pipeline.py --video-id <VIDEO_ID> --sort cumulative --top 30
"""

import argparse
import cProfile
import json
import os
import pstats
import sys
import time
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

_SCRAPER_DIR = str(Path(__file__).resolve().parent)
if _SCRAPER_DIR not in sys.path:
    sys.path.insert(0, _SCRAPER_DIR)
from pipeline import (
    LANG_MODEL_MAP,
    NO_MORPH_LANGS,
    connect,
    get_transcript,
    populate,
    insert_phrases,
    clean_sentence,
)

import spacy


# ---------------------------------------------------------------------------
# Timing helpers
# ---------------------------------------------------------------------------

class Timer:
    """Accumulating wall-clock timer with lap support."""

    def __init__(self):
        self.laps: list[tuple[str, float]] = []
        self._start: float | None = None
        self._label: str = ""

    @contextmanager
    def lap(self, label: str):
        t0 = time.perf_counter()
        yield
        self.laps.append((label, time.perf_counter() - t0))

    def report(self):
        total = sum(t for _, t in self.laps)
        print("\n── Timing breakdown ──────────────────────────────────────")
        for label, t in self.laps:
            pct = 100 * t / total if total else 0
            bar = "█" * int(pct / 2)
            print(f"  {label:<35} {t:7.3f}s  {pct:5.1f}%  {bar}")
        print(f"  {'TOTAL':<35} {total:7.3f}s")
        print("──────────────────────────────────────────────────────────\n")


# ---------------------------------------------------------------------------
# Transcript helpers
# ---------------------------------------------------------------------------

def load_transcript_from_file(path: str):
    """Load a transcript JSON (list of {text, start, duration})."""
    with open(path) as f:
        raw = json.load(f)
    return [SimpleNamespace(**s) for s in raw]


def save_transcript_to_file(transcript, path: str):
    raw = [{"text": s.text, "start": s.start, "duration": s.duration} for s in transcript]
    with open(path, "w") as f:
        json.dump(raw, f, indent=2, ensure_ascii=False)
    print(f"Transcript saved to {path}")


# ---------------------------------------------------------------------------
# Profiled populate (inline, so we can time individual phases)
# ---------------------------------------------------------------------------

def profiled_populate(*, transcript, language, nlp, dry_run: bool, timer: Timer):
    """
    Run the same logic as pipeline.populate() with per-phase timing.
    In dry_run mode no DB is touched.
    """
    texts = [s.text for s in transcript]

    with timer.lap("nlp.pipe (tokenise/POS/lemma)"):
        docs = list(nlp.pipe(texts))

    sentence_rows = []
    all_tokens = []

    with timer.lap("build sentence_rows + token lists"):
        for index, doc in enumerate(docs):
            tokens = [
                (t.text, t.pos_, t.lemma_, t.tag_)
                for t in doc
                if t.pos_ != "PUNCT" and t.text.strip()
            ]
            sentence_rows.append((
                "PROFILE_VIDEO",
                transcript[index].start,
                transcript[index].duration,
                clean_sentence(transcript[index].text),
                [t[0] for t in tokens],
            ))
            all_tokens.append(tokens)

    POS_LIST = {"VERB", "ADJ", "NOUN", "ADV", "PRON"}
    # NO_MORPH_LANGS now imported from pipeline (← language_config, #18).

    with timer.lap("grammar rule extraction (morph)"):
        if language not in NO_MORPH_LANGS:
            gram_types = []
            seen_rules: dict[str, int] = {}
            for idx, doc in enumerate(docs):
                for token in doc:
                    if token.pos_ in POS_LIST:
                        for prop, val in token.morph.to_dict().items():
                            rule = token.pos_ + prop + str(val)
                            key = language + "_" + rule
                            if key not in seen_rules:
                                seen_rules[key] = len(seen_rules)
                            gram_types.append((idx, seen_rules[key]))

    with timer.lap("build word set"):
        video_word_set = {token for tokens in all_tokens for token in tokens}

    with timer.lap("build word_to_sentence links"):
        word_id_map = {(w[0], w[1]): i for i, w in enumerate(video_word_set)}
        w2s = [
            (word_id_map[(t[0], t[1])], idx)
            for idx, tokens in enumerate(all_tokens)
            for t in tokens
            if (t[0], t[1]) in word_id_map
        ]

    if language == "de":
        with timer.lap("insert_phrases / phrase extraction"):
            # Extract phrases but don't write to DB
            from phrase_finder import extract_german_logic
            for doc in docs:
                extract_german_logic(doc)

    return {
        "sentences": len(sentence_rows),
        "tokens_total": sum(len(t) for t in all_tokens),
        "grammar_types": len(gram_types) if language not in NO_MORPH_LANGS else 0,
        "word_types": len(video_word_set),
        "w2s_links": len(w2s),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Profile the subtitle parsing pipeline")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--video-id", help="YouTube video ID to fetch and profile")
    src.add_argument("--transcript", help="Path to a saved transcript JSON file")

    parser.add_argument("--lang", default=None,
                        help="Language code (e.g. de, en). Auto-detected if omitted.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip all DB writes; profile NLP + phrase work only")
    parser.add_argument("--save-transcript", metavar="PATH",
                        help="Save fetched transcript to JSON for reuse")
    parser.add_argument("--cprofile", action="store_true",
                        help="Also run cProfile and print the top hotspots")
    parser.add_argument("--sort", default="tottime",
                        choices=["tottime", "cumulative", "ncalls", "filename"],
                        help="cProfile sort key (default: tottime)")
    parser.add_argument("--top", type=int, default=20,
                        help="Number of cProfile rows to show (default: 20)")
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # 1. Load / fetch transcript
    # ------------------------------------------------------------------
    if args.transcript:
        transcript = load_transcript_from_file(args.transcript)
        language = args.lang or "de"
        print(f"Loaded {len(transcript)} segments from {args.transcript} (lang={language})")
    else:
        print(f"Fetching transcript for {args.video_id}…")
        raw, detected_lang, _ = get_transcript(args.video_id, args.lang)
        if raw is None:
            print("No transcript found for this video.")
            sys.exit(1)
        transcript = [SimpleNamespace(**s) for s in raw]
        language = args.lang or detected_lang
        print(f"Detected language: {language}")
        print(f"Fetched {len(transcript)} segments")

        if args.save_transcript:
            save_transcript_to_file(transcript, args.save_transcript)

    # ------------------------------------------------------------------
    # 2. Load spacy model
    # ------------------------------------------------------------------
    model_name = LANG_MODEL_MAP.get(language)
    if not model_name:
        print(f"No spacy model for language '{language}'")
        sys.exit(1)

    print(f"Loading spacy model {model_name}…")
    t0 = time.perf_counter()
    nlp = spacy.load(model_name)
    nlp.select_pipes(enable=["tok2vec", "tagger", "attribute_ruler", "lemmatizer"])
    print(f"Model loaded in {time.perf_counter() - t0:.2f}s")

    print(f"\nPipeline: {len(transcript)} sentences  |  language={language}  |  dry_run={args.dry_run}")

    # ------------------------------------------------------------------
    # 3. Run profiled_populate (with manual timing)
    # ------------------------------------------------------------------
    timer = Timer()

    if args.cprofile:
        pr = cProfile.Profile()
        pr.enable()

    stats = profiled_populate(
        transcript=transcript,
        language=language,
        nlp=nlp,
        dry_run=args.dry_run,
        timer=timer,
    )

    if args.cprofile:
        pr.disable()

    # ------------------------------------------------------------------
    # 4. Report
    # ------------------------------------------------------------------
    print(f"\n── Data sizes ────────────────────────────────────────────")
    print(f"  Sentences processed:   {stats['sentences']}")
    print(f"  Total tokens:          {stats['tokens_total']}")
    print(f"  Unique word types:     {stats['word_types']}")
    print(f"  Grammar rule hits:     {stats['grammar_types']}")
    print(f"  word_to_sentence rows: {stats['w2s_links']}")
    print(f"──────────────────────────────────────────────────────────")

    timer.report()

    if args.cprofile:
        s = StringIO()
        ps = pstats.Stats(pr, stream=s).sort_stats(args.sort)
        ps.print_stats(args.top)
        print("── cProfile output ───────────────────────────────────────")
        print(s.getvalue())

    # ------------------------------------------------------------------
    # 5. Optional: full pipeline on real DB (not dry-run)
    # ------------------------------------------------------------------
    if not args.dry_run and args.video_id:
        print("Running full populate() against real DB for end-to-end timing…")
        connection = connect()
        cursor = connection.cursor()
        cursor.execute("SELECT word, pos, lemma FROM word_table")
        db_words = {(r[0], r[1], r[2]) for r in cursor.fetchall()}
        cursor.execute("SELECT language || '_' || rule, rule_id FROM grammar_rule")
        sentence_types = {row[0]: row[1] for row in cursor.fetchall()}

        # Use a fake video id so populate() skips the duplicate check
        # and immediately exits — override to actually measure insert time.
        # We delete the test row after.
        test_vid = f"__PROFILE_{args.video_id}__"
        cursor.execute("SELECT 1 FROM video WHERE video_id = %s", (args.video_id,))
        already_exists = cursor.fetchone() is not None

        if already_exists:
            print("  Video already in DB — skipping full DB populate (would be a no-op).")
        else:
            t_db = time.perf_counter()
            populate(
                cursor=cursor, connection=connection, db_words=db_words,
                video_id=args.video_id, title="[profile run]", thumbnail_url="",
                transcript=transcript, language=language,
                dialect=language, nlp=nlp, sentence_types=sentence_types,
                category="other", channel_id=None,
            )
            print(f"  Full DB populate: {time.perf_counter() - t_db:.3f}s")
            # Roll back so we don't pollute the DB with test data
            connection.rollback()
            print("  DB transaction rolled back (no data written).")

        connection.close()


if __name__ == "__main__":
    main()
