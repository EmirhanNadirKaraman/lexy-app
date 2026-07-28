"""
Shared catalog resolution — surface text → (`word_table` | `phrase_table`) row.

One rule, two entry points. Vocabulary lists (`word_list_service`) and
interactive reading (`reading_service`) both need "what does this text bind
to?", and they used to answer it differently: lists matched
`phrase_table.canonical`, reading matched `surface_form`. **773 German rows
have different values in those two columns**, so the same phrase could bind to
different rows — or bind on one path and not the other — depending on where
the user entered it. This module is the single answer (extracted 2026-07-28).

Case-insensitivity is decided in **Python**, via `text_norm.normalize_key`.
The database runs under the C locale, where Postgres's `lower()` and `ILIKE`
fold ASCII only — `lower('Öl')` is `'Öl'` — so any SQL-side predicate silently
misses every surface carrying an uppercase non-ASCII letter. Making both sides
SQL does not help: neither side folds. See `services/text_norm.py`.

Never first-match. Two candidates of the same type yield `ambiguous` with
`item_id = None`, because a wrong binding attaches mastery progress to the
wrong sense invisibly and no bulk/automatic path has an interactive picker.
That is the W3 / Hole 2 rule, and it now covers reading too — `find_catalog_item`
previously used a bare `fetchrow`, which silently took whichever row Postgres
returned first.
"""
from typing import NamedTuple

import asyncpg

from .text_norm import index_by_key, normalize_key

STATUS_UNRESOLVED = "unresolved"
STATUS_AMBIGUOUS = "ambiguous"

ITEM_WORD = "word"
ITEM_PHRASE = "phrase"


class Resolution(NamedTuple):
    """What a surface resolved to.

    `status` is a *forced* status — `unresolved` or `ambiguous` — and is None
    when the surface bound to exactly one row, in which case the real status
    comes from `user_word_knowledge`. `item_type` is meaningful even when
    `item_id` is None: it records which table we looked in first, so an
    unbound row still says what it was trying to be.
    """

    item_id: int | None
    item_type: str
    status: str | None


def preferred_type(surface: str) -> str:
    """Which catalog a surface should be looked up in first.

    Shape-based on purpose. A pasted blueprint (*jdm. (Dat) etw. (Akk) sagen*)
    or an article+noun (*das Haus*) is multi-token and belongs to
    `phrase_table`; a bare headword (*sagen*, *Haus*) belongs to `word_table`.
    Deciding by shape rather than by which query happens to return a row keeps
    *das Haus* and *Haus* independently trackable.
    """
    return ITEM_PHRASE if " " in surface.strip() else ITEM_WORD


async def resolve_surfaces(
    conn: asyncpg.Connection | asyncpg.Pool,
    surfaces: list[str],
    language: str,
) -> dict[str, Resolution]:
    """
    Bulk-resolve surfaces against both catalogs, language-scoped and
    case-insensitive. Returns normalize_key(surface) → Resolution.

    Two round-trips for the whole list (one per table) rather than N lookups.

    Case-insensitivity is decided in **Python**, not SQL. This database runs
    under the C locale, where Postgres's `lower()` and `ILIKE` fold ASCII only
    — `lower('Öl')` is `'Öl'`, so the old `lower(col) = ANY(python_lowered)`
    predicate never matched a surface with an uppercase non-ASCII letter: 50
    German `word_table` rows and 18 `phrase_table` canonicals were unreachable
    from vocabulary lists (found 2026-07-27). Making both sides SQL would not
    have helped: neither side folds.

    So each query fetches the whole language-scoped catalog and `normalize_key`
    does the matching. That is more rows than a targeted lookup, and it is a
    deliberate trade — a bounded query cannot express this fold correctly
    without an ICU collation. Measured ~20 ms for German and ~40 ms for the
    larger Spanish catalog (29,629 rows), flat in the number of surfaces.

    **The two callers pay for it differently.** A vocabulary list amortises one
    fetch across up to MAX_LIST_WORDS surfaces. `resolve_one` does not: reading
    pays a full fetch per selection save and per review click. That is accepted
    for now — both are user-initiated actions, not per-request work — but it is
    the first place to look if reading feels slow, and the ICU predicate in
    `services/text_norm.py` is the bounded alternative.

    Phrases match on `canonical`, not `surface_form`. `canonical` is the
    stable identity the rest of the app already keys on — `matcher_service`
    looks phrases up by it, and it is what `phrase_table`'s unique constraint
    covers. `surface_form` is a display convenience derived by stripping
    `jdm./jdn./etw.` placeholders, so matching it too would let one pasted
    line hit two rows and turn resolvable entries ambiguous for no gain.
    """
    if not surfaces:
        return {}

    word_rows = await conn.fetch(
        "SELECT w.word_id AS item_id, w.word AS surface "
        "FROM word_table w WHERE w.language = $1",
        language,
    )
    phrase_rows = await conn.fetch(
        "SELECT p.phrase_id AS item_id, p.canonical AS surface "
        "FROM phrase_table p WHERE p.language = $1",
        language,
    )

    by_type: dict[str, dict[str, list[int]]] = {
        ITEM_WORD: index_by_key(word_rows, "surface", "item_id"),
        ITEM_PHRASE: index_by_key(phrase_rows, "surface", "item_id"),
    }

    out: dict[str, Resolution] = {}
    for surface in surfaces:
        key = normalize_key(surface)
        first = preferred_type(surface)
        second = ITEM_WORD if first == ITEM_PHRASE else ITEM_PHRASE
        out[key] = _classify(
            by_type[first].get(key, []), first,
            by_type[second].get(key, []), second,
        )
    return out


def _classify(
    preferred_ids: list[int],
    preferred_type_: str,
    fallback_ids: list[int],
    fallback_type: str,
) -> Resolution:
    """
    Apply the precedence rule to one surface's candidates.

    The preferred type is decided first and decisively: several matches there
    mean `ambiguous`, and the fallback is NOT consulted — a surface that is
    genuinely ambiguous as a word should not quietly become a phrase. The
    fallback is only reached when the preferred type produced nothing at all.

    Never first-match: two candidates of the same type always yield
    `ambiguous` with `item_id = None`, because a bulk upload has no
    interactive picker and a wrong binding would attach mastery progress to
    the wrong sense invisibly (the W3 / Hole 2 concern).
    """
    if len(preferred_ids) == 1:
        return Resolution(preferred_ids[0], preferred_type_, None)
    if len(preferred_ids) > 1:
        return Resolution(None, preferred_type_, STATUS_AMBIGUOUS)

    if len(fallback_ids) == 1:
        return Resolution(fallback_ids[0], fallback_type, None)
    if len(fallback_ids) > 1:
        return Resolution(None, fallback_type, STATUS_AMBIGUOUS)

    return Resolution(None, preferred_type_, STATUS_UNRESOLVED)


async def resolve_one(
    conn: asyncpg.Connection | asyncpg.Pool,
    surface: str,
    language: str,
) -> Resolution:
    """Single-surface convenience wrapper over `resolve_surfaces`.

    Same rule, same precedence, same refusal to first-match — callers that
    resolve one string at a time (reading selections) get exactly what a
    vocabulary list would get for the same text.
    """
    resolved = await resolve_surfaces(conn, [surface], language)
    return resolved.get(
        normalize_key(surface), Resolution(None, preferred_type(surface), STATUS_UNRESOLVED),
    )
