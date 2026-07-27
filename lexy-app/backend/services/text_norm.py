"""
Unicode-safe case-insensitive matching for catalog lookups.

Why this exists
---------------
The database runs under the **C locale** (`datcollate=C`, `datctype=C`), so
Postgres's `lower()`, `upper()` and `ILIKE` fold **ASCII only**:

    lower('Öl')      -> 'Öl'      (unchanged)
    'Öl' ILIKE 'öl'  -> false
    lower('Ab')      -> 'ab'      (ASCII works)

Python's `str.lower()` folds the full Unicode range. So the common idiom "send
Python-lowered keys, compare against SQL `lower(column)`" silently fails for
every German word starting with Ä/Ö/Ü — the two sides compute different keys
and never meet. Found 2026-07-27: `Öl`, `Übung` and `Änderung` existed in
`word_table` yet vocabulary lists reported them `unresolved`. 50 German
`word_table` rows and 18 `phrase_table` canonicals were unreachable this way —
not only umlaut-*initial* ones: any uppercase non-ASCII letter anywhere in the
surface does it, which is why the multi-token `die Änderung` was affected.

Note the obvious-looking fix does **not** work. Making both sides SQL —
`lower(w.word) = lower($1)` — is self-consistent but still wrong, because
under C locale *neither* side folds: `lower('Öl')` is `'Öl'` and `lower('öl')`
is `'öl'`, which remain unequal. Verified against the live database.

The approach: fold in Python, filter in Python
----------------------------------------------
Callers fetch the language-scoped catalog rows and match them with
`normalize_key`, rather than asking SQL to do a fold it cannot do.

A bounded query that sends a handful of plausible spellings per surface as
exact-match values was tried first and rejected as **provably incomplete**:
whole-string case variants cannot cover a multi-token surface (a stored phrase
`die Änderung` is unreachable from a typed `Die Änderung`, since no
whole-string transform produces mixed per-token casing), and 153 `word_table`
rows were unreachable from their own uppercase form. Correctness wins here
because the cost is small and the path is cold — see `catalog_fetch_note`.

Rejected alternatives, for the record:
  * `lower(col COLLATE "und-x-icu")` is correct and bounded — it agreed with
    Python's `lower()` on all 12,138 German word and phrase rows, and
    correctly leaves `ß` alone. Not used because it makes an ICU-enabled
    Postgres a hard runtime requirement, and the query hard-fails where the
    collation is absent. It is the right optimisation if this ever gets hot.
  * `citext` / a column collation change would fix it at the schema level, but
    that is a migration against a shared catalog table and every index on it.
"""
from __future__ import annotations

import unicodedata

#: What a full language-scoped catalog fetch costs today. Worst case is
#: **Spanish**, not German: `word_table` holds 29,629 `es` rows vs 9,309 `de`
#: (plus 2,829 `de` phrases). Measured end-to-end at ~40 ms for `es` and
#: ~20 ms for `de`, flat in list size — a 500-surface Spanish list resolves in
#: ~35 ms. Both callers are cold paths — a user-initiated vocabulary-list upload and an
#: admin backfill script — not per-request work, so this is a deliberate trade
#: of bytes for provable correctness. If `word_table` grows by an order of
#: magnitude, switch to the ICU predicate described in the module docstring
#: rather than reintroducing a partial-coverage variant scheme.
catalog_fetch_note = __doc__


def normalize_key(text: str) -> str:
    """The canonical comparison key for a surface or catalog word.

    NFC-normalises (so a precomposed `ö` and a decomposed `o` + combining
    diaeresis compare equal), strips surrounding whitespace, and lowercases.

    Deliberately `lower()` and **not** `casefold()`. Casefold maps `ß` → `ss`,
    which is right for loose text search but wrong for a catalog key: this
    database holds `schließen` *and* `schliessen`, `heißt` *and* `heisst`,
    `Großteil` *and* `Grossteil` — 7 such pairs in German `word_table` as of
    2026-07-27. Casefolding would merge each pair into one key and report both
    spellings as `ambiguous`, breaking words that resolve cleanly today. They
    are distinct entries (Swiss vs. standard orthography), not duplicates.

    `lower()` still folds the full Unicode range — `Öl` → `öl` — which is the
    whole point here; only the `ß` expansion is unwanted. The trade-off is that
    an all-caps `STRASSE` does not match a stored `Straße`; that was already
    true before this change.
    """
    return unicodedata.normalize("NFC", text).strip().lower()


def index_by_key(rows, surface_field: str, id_field: str) -> dict[str, list[int]]:
    """Group catalog rows into `normalize_key(surface)` → sorted unique ids.

    Several ids under one key is a genuine duplicate in the catalog, which
    callers report as `ambiguous` rather than picking one.
    """
    out: dict[str, list[int]] = {}
    for r in rows:
        out.setdefault(normalize_key(r[surface_field]), []).append(r[id_field])
    return {k: sorted(set(v)) for k, v in out.items()}
