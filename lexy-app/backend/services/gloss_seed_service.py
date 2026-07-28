"""
Pre-seed the permanent item-gloss cache from curated translations (TODO #43 step 2).

`review_service.get_due_cards` needs an English gloss for every non-grammar due
card, and gets it from `llm_service.translate_item_gloss` — one LLM call per
item that has never been glossed, on the review hot path. `data/words_4000_old.txt`
already holds 4,095 human translations for the same headwords the catalog was
seeded from, so most of those calls are avoidable.

Provenance: the sentinel model
------------------------------
Curated rows are stored under the model `curated:words_4000_old`, never under a
real model id. Two reasons, both load-bearing:

- **Honest provenance.** A human wrote these. Filing them under
  `claude-haiku-4-5-…` would claim a model produced text it never saw.
- **Model-switch survival.** `make_cache_key` includes the model, so rows
  written under one model id miss entirely after `LLM_MODEL` changes — which is
  the whole point of the provider seam. `translate_item_gloss` checks the
  sentinel key *before* the model-specific one, so curated glosses survive a
  switch while genuine LLM output stays correctly model-scoped.

The sentinel also cannot collide with the `zztest-model-*` tag tests use, so
the autouse cleanup in `tests/conftest.py` can never reap a curated row.

Key compatibility
-----------------
The key must match what `translate_item_gloss` computes, so the text component
uses **`.lower()`**, not `text_norm.normalize_key`. That is a deliberate
mirror of the consumer, not an oversight: the two differ on exactly 1 of 9,313
German rows (a stray `·`), and diverging here would silently produce keys
nothing ever reads. `_CACHE_VERSION` is untouched.

What is seeded, and what is not
-------------------------------
Only single **words**, and only those whose surface actually appears as a
review `display_text` (`word_table.word` for `item_type='word'`). The cache key
is the *text*, not `item_id`, so a surface with several catalog rows is still
seedable — ambiguity is a catalog concern, not a gloss one.

**Phrases are deliberately not seeded.** Only 29 curated headwords match a
`phrase_table.surface_form` against 2,829 German phrases, and the file's
translations are for the bare headword rather than the phrase. Inventing phrase
glosses from that would be fabrication; it needs its own source.

Rows are skipped when the translation does not fit the gloss shape the prompt
asks for ("1-4 words for single words"): multi-sense cells (`1) …; 2) …`) and
long translations are left to the LLM rather than guessed at.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import asyncpg

from . import llm_cache_service

#: Repo-root `data/words_4000_old.txt`, resolved from this file so the script
#: works from any cwd (the `os.chdir` trap in docs/COMMON_ERRORS.md).
DEFAULT_SOURCE = (
    Path(__file__).resolve().parent.parent.parent.parent / "data" / "words_4000_old.txt"
)

LANGUAGE = "de"
ITEM_TYPE = "word"
PROMPT_KEY = "item_gloss"

#: Sentinel model for curated rows — see the module docstring. Never a real
#: model id, never the `zztest-model-*` prefix tests use.
CURATED_MODEL = "curated:words_4000_old"

#: Definite articles stripped from noun headwords, matching `word_seed_service`
#: — `word_table` stores bare surfaces, so the gloss key must too.
_ARTICLES = ("der ", "die ", "das ")

#: A seedable headword is one clean token, same rule as the catalog seeder.
_CLEAN_TOKEN = re.compile(r"^[^\W\d_][\w\-']*$", re.UNICODE)

#: Longest translation still treated as a gloss. The prompt asks for "1-4 words
#: for single words"; 4 is the stated ceiling and 92% of the file already fits.
MAX_GLOSS_WORDS = 4

SKIP_ARTEFACT = "artefact"        # multi-entry cell, e.g. `der, die, das`
SKIP_MULTISENSE = "multisense"    # `1) a; 2) one (of)` — not a single gloss
SKIP_LONG = "long"                # a definition, not a gloss
SKIP_EMPTY = "empty"              # missing headword or translation

_MULTISENSE = re.compile(r"^\s*1\s*\)")


def strip_article(headword: str) -> str:
    """`das Haus` → `Haus`. Leaves non-article surfaces untouched."""
    s = headword.strip()
    low = s.lower()
    for article in _ARTICLES:
        if low.startswith(article):
            return s[len(article):].strip()
    return s


def classify_translation(translation: str) -> str | None:
    """Why a translation is not seedable, or None when it is."""
    t = translation.strip()
    if not t:
        return SKIP_EMPTY
    if _MULTISENSE.match(t):
        return SKIP_MULTISENSE
    if len(t.split()) > MAX_GLOSS_WORDS:
        return SKIP_LONG
    return None


def read_rows(source: Path | None = None) -> list[tuple[str, str]]:
    """`(headword, translation)` from columns 1 and 3 of every non-empty line.

    The file is an 11-column TSV; the other columns (example, conjugations,
    POS, frequency rank) are enrichment this seeder does not use.
    """
    path = Path(source) if source is not None else DEFAULT_SOURCE
    out: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        cols = line.split("\t")
        if len(cols) < 3:
            continue
        out.append((cols[0].strip(), cols[2].strip()))
    return out


def build_candidates(
    rows: list[tuple[str, str]],
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Turn raw rows into `{cache_key_text: gloss}` plus skips by reason.

    The returned mapping is keyed by `surface.lower()` — exactly the text
    component `translate_item_gloss` hashes — so the first spelling of a
    case-duplicate wins and later ones are ignored rather than overwriting.
    """
    candidates: dict[str, str] = {}
    skipped: dict[str, list[str]] = {
        SKIP_ARTEFACT: [], SKIP_MULTISENSE: [], SKIP_LONG: [], SKIP_EMPTY: [],
    }

    for raw_head, translation in rows:
        surface = strip_article(raw_head)
        if not surface:
            skipped[SKIP_EMPTY].append(raw_head)
            continue
        if not _CLEAN_TOKEN.match(surface):
            # `der, die, das`, `all, alle` — several entries in one cell.
            skipped[SKIP_ARTEFACT].append(surface)
            continue
        reason = classify_translation(translation)
        if reason is not None:
            skipped[reason].append(surface)
            continue
        candidates.setdefault(surface.lower(), translation.strip())

    return candidates, skipped


def curated_cache_key(text: str, item_type: str = ITEM_TYPE, language: str = LANGUAGE) -> str:
    """The sentinel-model key `translate_item_gloss` checks first.

    Mirrors that function's params exactly, including `.lower()` on the text.
    """
    return llm_cache_service.make_cache_key(
        PROMPT_KEY, CURATED_MODEL,
        {"text": text.lower(), "item_type": item_type, "language": language},
    )


async def catalog_surfaces(pool: asyncpg.Pool, language: str = LANGUAGE) -> set[str]:
    """Every `word_table.word` for `language`, lowercased into key space.

    A seeded gloss is only reachable if some review card's `display_text`
    hashes to its key, and for `item_type='word'` that display text is
    `word_table.word`. Several rows sharing a lowercased surface collapse to
    one key, which is correct — the gloss is per text, not per row.
    """
    rows = await pool.fetch(
        "SELECT word FROM word_table WHERE language = $1", language,
    )
    return {r["word"].lower() for r in rows}


async def seed_glosses(
    pool: asyncpg.Pool,
    apply: bool = False,
    source: Path | None = None,
    language: str = LANGUAGE,
) -> dict:
    """
    Audit (and optionally write) curated gloss cache rows.

    Returns:
        {
            "rows_read":        int,   # non-empty lines in the source
            "candidates":       int,   # clean headword + gloss-shaped translation
            "in_catalog":       int,   # candidates whose text exists in word_table
            "not_in_catalog":   int,   # skipped — nothing would ever read the key
            "already_cached":   int,   # curated key already present
            "missing":          int,   # would be written
            "inserted":         int,   # rows actually written (0 when apply=False)
            "skipped_artefact"/"skipped_multisense"/"skipped_long"/"skipped_empty": int,
            "skipped_samples":  dict[str, list[str]],
            "sample":           list[tuple[str, str]],
            "dry_run":          bool,
        }

    Never calls the LLM. With `apply=False` nothing is written. Idempotent: a
    second `apply=True` pass returns `inserted == 0`, because the missing set is
    recomputed against the cache each time and the INSERT additionally carries
    `ON CONFLICT DO NOTHING`.
    """
    rows = read_rows(source)
    candidates, skipped = build_candidates(rows)

    surfaces = await catalog_surfaces(pool, language)
    in_catalog = {t: g for t, g in candidates.items() if t in surfaces}
    not_in_catalog = len(candidates) - len(in_catalog)

    keyed = {curated_cache_key(t, ITEM_TYPE, language): (t, g) for t, g in in_catalog.items()}
    present: set[str] = set()
    if keyed:
        found = await pool.fetch(
            "SELECT cache_key FROM llm_cache WHERE cache_key = ANY($1::text[])",
            list(keyed),
        )
        present = {r["cache_key"] for r in found}
    missing = {k: v for k, v in keyed.items() if k not in present}

    result = {
        "rows_read": len(rows),
        "candidates": len(candidates),
        "in_catalog": len(in_catalog),
        "not_in_catalog": not_in_catalog,
        "already_cached": len(present),
        "missing": len(missing),
        "inserted": 0,
        "skipped_artefact": len(skipped[SKIP_ARTEFACT]),
        "skipped_multisense": len(skipped[SKIP_MULTISENSE]),
        "skipped_long": len(skipped[SKIP_LONG]),
        "skipped_empty": len(skipped[SKIP_EMPTY]),
        "skipped_samples": {k: v[:3] for k, v in skipped.items()},
        "sample": [(t, g) for t, g in list(missing.values())[:5]],
        "dry_run": not apply,
    }

    if not apply or not missing:
        return result

    # One statement with RETURNING, so `inserted` counts rows this call wrote.
    # Counting the keys afterwards would also count rows that were already
    # there — the over-reporting bug fixed in the catalog backfill (0dd7075).
    # `expires_at` stays NULL: curated glosses are permanent, like the LLM ones.
    keys = list(missing)
    glosses = [json.dumps({"gloss": v[1]}, ensure_ascii=False) for v in missing.values()]
    async with pool.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetch(
                """
                INSERT INTO llm_cache (cache_key, prompt_key, model, response)
                SELECT k, $2, $3, r::jsonb
                  FROM unnest($1::text[], $4::text[]) AS t(k, r)
                ON CONFLICT (cache_key) DO NOTHING
                RETURNING cache_key
                """,
                keys, PROMPT_KEY, CURATED_MODEL, glosses,
            )
            result["inserted"] = len(inserted)
    result["dry_run"] = False
    return result

