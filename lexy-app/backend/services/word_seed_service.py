"""
Seed missing German catalog words from `data/final_result.txt` (N5a step 1).

`word_table` is scraper-derived: it holds whatever appeared in scraped YouTube
subtitles, which is not the same set as a curated learner vocabulary. Ordinary
words — *Abbildung*, *Abenteuer*, *Abfall*, *Adresse* — are simply absent, so
roughly half of `final_result.txt`'s headwords report `unresolved` in a
vocabulary list. This backfill closes that gap.

`final_result.txt` already seeds `phrase_table` at startup
(`main.py` → `matcher_service.get_blueprint_map` →
`phrase_service.seed_from_blueprint_map`). **Nothing here touches that path.**
This reads the same file for a different column and a different table.

Column model
------------
The file is a 2-column TSV. Column 0 is the headword (nouns carry their
article); column 1 is the phrase blueprint. **Only column 0 is read here** —
blueprints belong to `phrase_table` and must never become `word_table` rows.

Why insert-only, and why `pos=''`
---------------------------------
`word_table` has `UNIQUE (word, language, pos)` with `pos` NOT NULL and *part
of the key*, and every existing German row carries `pos = ''`. Inserting a
word that already exists with a different `pos` therefore does NOT conflict —
it creates a second row, and two rows for one surface is exactly what
`word_list_service` reports as `ambiguous`. Seeding POS onto existing words
would silently break lists that resolve cleanly today.

So: insert only surfaces absent by `lower(word)`, and insert with `pos=''` to
match the convention already in the table. Existing rows are never updated.
POS/gloss/example enrichment from `words_4000_old.txt` is deliberately out of
scope — it needs a schema decision, not just a column.

Frequency is left at the table default. `word_table.frequency` means "number
of sentences the word appears in" (migration 032) — app-corpus frequency. A
seeded word has appeared in zero scraped sentences, and writing a rank from
some other source into that column would corrupt its meaning.
"""
from __future__ import annotations

import re
from pathlib import Path

import asyncpg

#: Repo-root `data/final_result.txt`, resolved from this file so the script
#: works from any cwd (the `os.chdir` trap in docs/COMMON_ERRORS.md).
DEFAULT_SOURCE = (
    Path(__file__).resolve().parent.parent.parent.parent / "data" / "final_result.txt"
)

LANGUAGE = "de"

#: Definite articles stripped from noun headwords. `final_result.txt` stores
#: nouns as "das Haus"; `word_table` stores bare surfaces, so the article is
#: removed for this table only. `phrase_table` keeps the full "das Haus" row —
#: that is what makes the two independently trackable, and it is not changed.
_ARTICLES = ("der ", "die ", "das ")

#: A candidate must be a single clean token. Everything else in column 0 is
#: excluded from `word_table`, for two quite different reasons — see
#: `classify_skip`.
_CLEAN_TOKEN = re.compile(r"^[^\W\d_][\w\-']*$", re.UNICODE)

#: Why a column-0 value was not imported as a word. Both are correct
#: exclusions, but only one is a data-quality problem, so they are reported
#: separately rather than lumped together as "dirty".
SKIP_MULTIWORD = "multiword"   # a real lexical item, but not a single word
SKIP_ARTEFACT = "artefact"     # a multi-entry cell, not a headword at all


def classify_skip(surface: str) -> str:
    """Why `surface` is not a `word_table` candidate.

    `sich setzen` and `ein paar` are genuine vocabulary — they are simply
    multi-word, so they belong to `phrase_table` (which already carries 133
    `reflexive_verb` rows seeded from this same file). Calling them "dirty"
    would misreport working data.

    `der, die, das` and `all, alle` are a different thing: several entries
    crammed into one cell. Those are a source-data defect.
    """
    return SKIP_ARTEFACT if "," in surface else SKIP_MULTIWORD


def strip_article(headword: str) -> str:
    """`das Haus` → `Haus`. Leaves non-article surfaces untouched."""
    s = headword.strip()
    low = s.lower()
    for article in _ARTICLES:
        if low.startswith(article):
            return s[len(article):].strip()
    return s


def is_clean_candidate(surface: str) -> bool:
    """True when a surface is a single word fit for `word_table`.

    Rejects anything containing whitespace, a comma, or other punctuation
    that marks it as a multi-entry artefact rather than a headword.
    """
    return bool(surface) and bool(_CLEAN_TOKEN.match(surface))


def read_headwords(source: Path | None = None) -> list[str]:
    """Column 0 of every non-empty row, in file order. Column 1 is ignored."""
    path = Path(source) if source is not None else DEFAULT_SOURCE
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        out.append(line.split("\t", 1)[0].strip())
    return out


def build_candidates(headwords: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Turn raw column-0 values into (candidates, skipped_by_reason).

    Candidates are article-stripped, clean single tokens, deduplicated
    **case-insensitively** with the first spelling winning, in file order.
    Case-insensitive dedup matches how `word_list_service` resolves, so the
    candidate set and the lookup agree.

    `skipped_by_reason` maps `SKIP_MULTIWORD` / `SKIP_ARTEFACT` to the
    article-stripped forms that failed the clean check.
    """
    candidates: list[str] = []
    skipped: dict[str, list[str]] = {SKIP_MULTIWORD: [], SKIP_ARTEFACT: []}
    seen: set[str] = set()
    seen_skips: set[str] = set()

    for raw in headwords:
        surface = strip_article(raw)
        if not surface:
            continue
        if not is_clean_candidate(surface):
            if surface not in seen_skips:
                seen_skips.add(surface)
                skipped[classify_skip(surface)].append(surface)
            continue
        key = surface.lower()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(surface)

    return candidates, skipped


async def find_missing_words(
    pool: asyncpg.Pool, candidates: list[str], language: str = LANGUAGE,
) -> list[str]:
    """Candidates with no `word_table` row for the same `lower(word)`.

    Case-insensitive on purpose: a seeded `Haus` must not fork an existing
    `haus`. One round-trip regardless of candidate count.
    """
    if not candidates:
        return []
    rows = await pool.fetch(
        """
        SELECT lower(w.word) AS key
        FROM word_table w
        WHERE w.language = $2
          AND lower(w.word) = ANY($1::text[])
        """,
        sorted({c.lower() for c in candidates}), language,
    )
    present = {r["key"] for r in rows}
    return [c for c in candidates if c.lower() not in present]


async def seed_word_catalog(
    pool: asyncpg.Pool,
    apply: bool = False,
    source: Path | None = None,
    language: str = LANGUAGE,
) -> dict:
    """
    Audit (and optionally insert) missing catalog words.

    Returns:
        {
            "rows_read":      int,   # non-empty lines in the source file
            "candidates":     int,   # clean, deduplicated, article-stripped
            "already_present": int,  # candidates already in word_table
            "missing":        int,   # candidates that would be inserted
            "inserted":       int,   # rows actually written (0 when apply=False)
            "skipped_multiword": int,  # real vocabulary, but multi-word
            "skipped_artefact":  int,  # multi-entry cells — a source defect
            "skipped_samples":   dict[str, list[str]],
            "inserted_samples": list[str],
            "dry_run":        bool,
        }

    With `apply=False` (the default) nothing is written. Idempotent: a second
    `apply=True` pass returns `inserted == 0`, because the missing-set is
    recomputed against the table each time and the INSERT additionally carries
    `ON CONFLICT DO NOTHING` as a backstop against a concurrent scraper write.
    """
    headwords = read_headwords(source)
    candidates, skipped = build_candidates(headwords)
    missing = await find_missing_words(pool, candidates, language)

    result = {
        "rows_read": len(headwords),
        "candidates": len(candidates),
        "already_present": len(candidates) - len(missing),
        "missing": len(missing),
        "inserted": 0,
        "skipped_multiword": len(skipped[SKIP_MULTIWORD]),
        "skipped_artefact": len(skipped[SKIP_ARTEFACT]),
        "skipped_samples": {k: v[:3] for k, v in skipped.items()},
        "inserted_samples": missing[:5],
        "dry_run": not apply,
    }

    if not apply or not missing:
        return result

    # pos/tag empty and lemma == word: matches every existing German row. See
    # the module docstring for why a real POS would fork rows instead of
    # enriching them. `frequency` is left to the column default (0).
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO word_table (word, language, pos, tag, lemma)
                VALUES ($1, $2, '', '', $1)
                ON CONFLICT (word, language, pos) DO NOTHING
                """,
                [(w, language) for w in missing],
            )
            # Count what actually landed rather than trusting executemany —
            # a concurrent scraper insert could have taken some of them.
            result["inserted"] = await conn.fetchval(
                """
                SELECT count(*) FROM word_table
                 WHERE language = $2 AND pos = '' AND word = ANY($1::text[])
                """,
                missing, language,
            )
    result["dry_run"] = False
    return result
