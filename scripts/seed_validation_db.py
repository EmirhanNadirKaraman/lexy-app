"""Seed a VALIDATION database with the minimal synthetic corpus the backend
suite needs, deterministically and idempotently.

    python3 scripts/seed_validation_db.py           # seed (safe to re-run)
    python3 scripts/seed_validation_db.py --verify  # report only, write nothing

**Why this exists.** A validation database built from a schema dump has no
corpus, and ~55 backend tests skip themselves rather than fail when the corpus
is missing ("run the subtitle pipeline first"). A skipping test proves nothing,
so a task whose declared validation is the backend suite would be graded by a
suite that silently opted out of the parts touching real content. This creates
just enough content for those guards to pass.

**Everything here is INVENTED.** No row is copied from any real database. The
words and sentences are a handful of textbook German forms chosen to satisfy
specific guard conditions (a lemma with two surface forms, a non-ASCII surface,
a word id that collides with a phrase id). It is not a corpus sample and is not
meant to resemble one.

**Deterministic and idempotent.** Every row has an explicit, fixed id in a
reserved high range (`_BASE`), so a second run inserts nothing and the test
suite sees byte-identical content. Re-running is the supported way to repair a
database a test run has damaged. `--verify` re-checks each guard condition
without writing.

**It refuses to touch the application database.** The one production marker
this repository defines is `.env.example`'s `DB_NAME`; if the target matches it
the script exits non-zero having done nothing. That is the same exact-match
rule (and the same function) the autoloop validation-environment boundary uses
— see `autoloop/validation_env.py` and `docs/AUTOLOOP.md` §4g. It is one known
name, not a test-vs-production discriminator: pointing this at some OTHER real
database is not detectable here, and would insert junk into it.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_batch

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from autoloop.validation_env import repo_declared_db_name  # noqa: E402

#: Reserved id range. Far above anything a fresh database allocates, so these
#: rows never collide with ids the suite's own fixtures create, and a human
#: reading the table can tell instantly which rows are seed data.
_BASE = 900_000

LANGUAGE = "de"
DIALECT = "de-DE"

VIDEOS = [
    ("zzval-de-video-1", "Validation corpus video 1", 61.0),
    ("zzval-de-video-2", "Validation corpus video 2", 42.0),
]

#: (sentence_id, video_id, start, duration, content, tokens)
SENTENCES = [
    (_BASE + 1, "zzval-de-video-1", 0.0, 3.0, "Die Katze geht nach Hause.",
     ["Die", "Katze", "geht", "nach", "Hause"]),
    (_BASE + 2, "zzval-de-video-1", 3.0, 3.0, "Gestern ging sie durch die Baeume.",
     ["Gestern", "ging", "sie", "durch", "die", "Baeume"]),
    (_BASE + 3, "zzval-de-video-1", 6.0, 3.0, "Das Haus ist gross.",
     ["Das", "Haus", "ist", "gross"]),
    (_BASE + 4, "zzval-de-video-2", 0.0, 3.0, "Wir gehen zusammen.",
     ["Wir", "gehen", "zusammen"]),
    (_BASE + 5, "zzval-de-video-2", 3.0, 3.0, "Die Baeume sind hoch.",
     ["Die", "Baeume", "sind", "hoch"]),
]

#: The word-id collision case. `test_recommendations` needs one id present in
#: BOTH `word_table` and `phrase_table` for the same language, to prove the
#: enrichment dispatcher keys on (item_type, item_id) rather than the bare
#: SERIAL — a real collision, since the two tables have independent sequences.
#: Resolved at runtime to the lowest German phrase_id actually present.
COLLIDING_WORD = ("Katze", "Katze", "NOUN", "NN")

#: (word_id, word, lemma, pos, tag). Chosen for specific guards:
#:   * `gehen` / `ging` share a lemma  → "lemma with multiple surface forms"
#:   * `ging`  has lemma != surface    → the lemma-only match test
#:   * `Bäume` is non-ASCII            → the word_norm/normalize_key test
WORDS = [
    (_BASE + 101, "Haus", "Haus", "NOUN", "NN"),
    (_BASE + 102, "gehen", "gehen", "VERB", "VVINF"),
    (_BASE + 103, "ging", "gehen", "VERB", "VVFIN"),
    (_BASE + 104, "Bäume", "Baum", "NOUN", "NN"),
]

#: Links every seeded word into video 1's sentences, so the "populated video"
#: and playlist joins (word_to_sentence → sentence → video) resolve, and so the
#: two `gehen` surfaces land in the SAME video (that test groups per video).
LINKS = [
    (_BASE + 101, _BASE + 3),
    (_BASE + 102, _BASE + 1),
    (_BASE + 102, _BASE + 4),
    (_BASE + 103, _BASE + 2),
    (_BASE + 104, _BASE + 2),
    (_BASE + 104, _BASE + 5),
]

#: DELIBERATELY NOT SEEDED: `sich freuen auf`.
#:
#: `test_match_inflected_phrase_matches_canonical` skips unless that canonical
#: is in `phrase_table`, so seeding it looks like it would recover a test. It
#: does the opposite — the test then RUNS and FAILS, because the phrase
#: extractor never produces that canonical in the first place. Measured
#: directly: `match_sentence("ich freue mich auf die Reise", "de")` returns
#: `['ich', 'jdn. (Akk) freuen']`. The assertion is about the extractor's
#: dictionary, not about the database, so no amount of seeding can satisfy it
#: and its skip is the honest outcome. (That the extractor disagrees with the
#: test's premise is an app-level question — see docs/TESTS.md — not something
#: a validation database should paper over.)
PHRASE = None


def connect():
    missing = [k for k in ("DB_NAME", "DB_USER", "DB_HOST") if not os.getenv(k)]
    if missing:
        sys.exit(
            f"missing DB env var(s): {', '.join(missing)}. Point this at a "
            "validation database, e.g. by exporting the six names in your "
            "validation env file (see docs/AUTOLOOP.md §4g)."
        )
    name = os.environ["DB_NAME"]
    declared = repo_declared_db_name(REPO_ROOT)
    if declared and name.strip().lower() == declared.strip().lower():
        sys.exit(
            f"refusing to seed {name!r}: that is the database name this "
            "repository declares in .env.example as its application database. "
            "This script inserts synthetic rows into shared catalog tables — "
            "point it at a dedicated validation database instead."
        )
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.getenv("DB_PORT", "5432"),
        dbname=name,
        user=os.environ["DB_USER"],
        password=os.getenv("DB_PASSWORD"),
    )


def seed(conn) -> None:
    cur = conn.cursor()

    execute_batch(
        cur,
        "INSERT INTO video (video_id, title, thumbnail_url, duration, language, "
        "dialect, category) VALUES (%s, %s, '', %s, %s, %s, 'other') "
        "ON CONFLICT (video_id) DO NOTHING",
        [(vid, title, dur, LANGUAGE, DIALECT) for vid, title, dur in VIDEOS],
    )
    execute_batch(
        cur,
        "INSERT INTO sentence (sentence_id, video_id, start_time, duration, content, tokens) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (sentence_id) DO NOTHING",
        SENTENCES,
    )

    # The colliding word takes an id that already exists as a German phrase_id.
    cur.execute(
        "SELECT phrase_id FROM phrase_table WHERE language = %s ORDER BY phrase_id LIMIT 1",
        (LANGUAGE,),
    )
    row = cur.fetchone()
    words = list(WORDS)
    if row is not None:
        word, lemma, pos, tag = COLLIDING_WORD
        words.append((row[0], word, lemma, pos, tag))

    execute_batch(
        cur,
        "INSERT INTO word_table (word_id, word, language, pos, tag, lemma, frequency) "
        "VALUES (%s, %s, %s, %s, %s, %s, 1) ON CONFLICT DO NOTHING",
        [(wid, w, LANGUAGE, pos, tag, lemma) for wid, w, lemma, pos, tag in words],
    )
    execute_batch(
        cur,
        "INSERT INTO word_to_sentence (word_id, sentence_id) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING",
        LINKS,
    )
    if PHRASE is not None:  # see PHRASE's comment — currently, deliberately, None
        cur.execute(
            "INSERT INTO phrase_table (canonical, surface_form, phrase_type, language) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (canonical, language) DO NOTHING",
            PHRASE,
        )

    # Explicit ids do not advance a sequence, so a later fixture INSERT would
    # try to reuse one and fail on the primary key. Push each sequence past
    # what we just wrote.
    for seq, col, table in (
        ("word_table_word_id_seq", "word_id", "word_table"),
        ("sentence_id_seq", "sentence_id", "sentence"),
    ):
        cur.execute(
            f"SELECT setval('{seq}', GREATEST((SELECT COALESCE(MAX({col}), 1) FROM {table}), "
            f"(SELECT last_value FROM {seq})))"
        )
    conn.commit()


#: Each guard the backend suite actually applies, as (label, SQL returning a
#: row when satisfied). Kept in the same words the skip messages use, so a
#: `--verify` line maps to the test that would skip.
CHECKS = [
    ("videos exist (test_recommendations)",
     "SELECT 1 FROM video LIMIT 1"),
    ("2+ sentences (test_transcript_click)",
     "SELECT 1 FROM sentence HAVING count(*) >= 2"),
    ("word_to_sentence joins video (test_playlist)",
     "SELECT 1 FROM word_to_sentence wts JOIN sentence s ON s.sentence_id = wts.sentence_id "
     "JOIN video v ON v.video_id = s.video_id LIMIT 1"),
    ("populated video (test_reading_stats)",
     "SELECT 1 FROM video v JOIN sentence s ON s.video_id = v.video_id "
     "JOIN word_to_sentence wts ON wts.sentence_id = s.sentence_id "
     "JOIN word_table wt ON wt.word_id = wts.word_id LIMIT 1"),
    ("plain German word (test_free_chat_progression)",
     "SELECT 1 FROM word_table WHERE language = 'de' AND word !~ '[0-9_]' LIMIT 1"),
    ("word with lemma != surface (test_free_chat_progression)",
     "SELECT 1 FROM word_table WHERE lemma IS NOT NULL AND LOWER(lemma) != LOWER(word) "
     "AND word !~ '[0-9_]' AND lemma !~ '[0-9_]' LIMIT 1"),
    ("lemma with 2+ surface forms in one video (test_reading_stats)",
     "SELECT 1 FROM video v JOIN sentence s ON s.video_id = v.video_id "
     "JOIN word_to_sentence wts ON wts.sentence_id = s.sentence_id "
     "JOIN word_table wt ON wt.word_id = wts.word_id "
     "GROUP BY v.video_id, wt.lemma HAVING COUNT(DISTINCT wt.word_id) >= 2 LIMIT 1"),
    ("word_id/phrase_id collision (test_recommendations)",
     "SELECT 1 FROM word_table wt JOIN phrase_table pt ON pt.phrase_id = wt.word_id "
     "WHERE wt.language = pt.language LIMIT 1"),
    ("non-ASCII word (test_search_unicode)",
     r"SELECT 1 FROM word_table WHERE word ~ '[^\x01-\x7F]' LIMIT 1"),
]


def verify(conn) -> int:
    cur = conn.cursor()
    missing = 0
    for label, sql in CHECKS:
        cur.execute(sql)
        ok = cur.fetchone() is not None
        missing += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'MISS'}  {label}")
    return missing


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="seed_validation_db")
    ap.add_argument("--verify", action="store_true",
                    help="report which guards are satisfied; write nothing")
    args = ap.parse_args(argv)

    conn = connect()
    try:
        if not args.verify:
            seed(conn)
            print(f"seeded {os.environ['DB_NAME']} (idempotent; re-running changes nothing)")
        missing = verify(conn)
    finally:
        conn.close()
    if missing:
        print(f"{missing} guard(s) still unsatisfied")
        return 1
    print("all guards satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
