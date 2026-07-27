"""User vocabulary lists — upload a word list, see what you already know, export it.

Closes the gap between a pasted/uploaded vocabulary list and the rest of the
app: a resolved word entry carries a `word_table.word_id`, which is exactly the
`item_ids` shape `playlist_service.generate_playlist` consumes.

Storage lives in `word_lists` / `word_list_items` (migration 001, extended by
035). Every entry stores its original `surface`; `item_id` is the binding to
`word_table` or `phrase_table` (per `item_type`) and is NULL when the surface
did not bind.

Entry states surfaced to the user (five, not four):
  known / learning / unknown  — bound to a catalog row, from user_word_knowledge
  unresolved                  — no language-scoped match in either table
  ambiguous                   — several matches; deliberately NOT auto-picked

Scope: entries resolve against **both** `word_table` and `phrase_table`, and
carry the matching `item_type` (`word` | `phrase`). `phrase_table` is seeded
from `data/final_result.txt` at startup (`main.py` →
`matcher_service.get_blueprint_map` → `phrase_service.seed_from_blueprint_map`),
so a pasted blueprint like *jdm. (Dat) etw. (Akk) sagen* binds to the same
phrase row the chat matcher and SRS already use. Nothing about that seeding
changes here.

Precedence is decided by the surface's own shape, not by which table answers
first:

  multi-token surface  → phrase first, word as fallback
  single-token surface → word first, phrase as fallback

That keeps *das Haus* (a `collocation` row in `phrase_table`) and *Haus* (a
`word_table` row) as two distinct, individually-trackable entries rather than
silently collapsing them — article/noun normalisation is deliberately NOT
attempted here. Within the preferred type, several matches still mean
`ambiguous`; the fallback type is only consulted when the preferred one has
none.

`ambiguous` exists because `word_service.lookup_word_by_text` refuses to guess
between several rows for one surface (*die Bank* = bench vs. bank — different
pos/lemma, different meaning). That refusal is the W3 / Hole 2 fix. A bulk
upload has no interactive picker, so rather than silently binding to a
first-match, an ambiguous surface is stored with `item_id = NULL` and reported
as such. A wrong binding would attach mastery progress to the wrong meaning
invisibly; an ambiguous label is recoverable.
"""
from typing import NamedTuple

import asyncpg

from . import progression_service
from .text_norm import index_by_key, normalize_key

# Matches the Pydantic cap in WordListCreate. Duplicated as a service-level
# guard so a non-HTTP caller can't blow past it.
MAX_LIST_WORDS = 500

STATUS_KNOWN = "known"
STATUS_LEARNING = "learning"
STATUS_UNKNOWN = "unknown"
STATUS_UNRESOLVED = "unresolved"
STATUS_AMBIGUOUS = "ambiguous"

ALL_STATUSES = (
    STATUS_KNOWN,
    STATUS_LEARNING,
    STATUS_UNKNOWN,
    STATUS_UNRESOLVED,
    STATUS_AMBIGUOUS,
)

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


def normalize_surfaces(words: list[str]) -> list[str]:
    """
    Strip, drop blanks, and dedupe case-insensitively while preserving the
    order the user supplied. The first spelling of a duplicate wins, so
    export round-trips what was actually pasted.

    Deduping here (not in SQL) keeps the unique index on
    (list_id, lower(surface)) a backstop rather than an error path.

    Uses `normalize_key`, so `Öl` and `öl` collapse to one entry the same way
    `Haus` and `haus` do — Python's Unicode-aware `lower()`, not Postgres's
    ASCII-only one. See `services/text_norm.py`.

    Note the index is a *weaker* backstop for non-ASCII surfaces than for
    ASCII ones: its `lower()` runs under the C locale and would not consider
    `Öl` and `öl` duplicates. That is fine only because this function runs
    first and no duplicate pair ever reaches the insert. Don't drop it and
    lean on the constraint.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in words:
        surface = raw.strip()
        if not surface:
            continue
        key = normalize_key(surface)
        if key in seen:
            continue
        seen.add(key)
        out.append(surface)
    return out


async def _resolve_surfaces(
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
    without an ICU collation, and both callers here are cold paths (list
    upload, late binding) measured at ~20 ms for German and ~40 ms for the
    larger Spanish catalog. `services/text_norm.py` documents the sizes, the
    ICU alternative, and why the obvious bounded scheme was rejected as
    incomplete.

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


async def create_list(
    pool: asyncpg.Pool,
    user_id: str,
    name: str,
    language: str,
    words: list[str],
    description: str | None = None,
) -> dict:
    """
    Create a list and resolve every surface. Raises ValueError when the
    normalized word list is empty or over MAX_LIST_WORDS.
    """
    surfaces = normalize_surfaces(words)
    if not surfaces:
        raise ValueError("word list is empty")
    if len(surfaces) > MAX_LIST_WORDS:
        raise ValueError(f"word list exceeds {MAX_LIST_WORDS} words")

    async with pool.acquire() as conn:
        async with conn.transaction():
            list_id = await conn.fetchval(
                """
                INSERT INTO word_lists (user_id, name, language, description)
                VALUES ($1::uuid, $2, $3, $4)
                RETURNING list_id
                """,
                user_id, name, language, description,
            )

            resolved = await _resolve_surfaces(conn, surfaces, language)
            rows = []
            for surface in surfaces:
                # An unbound surface still records the type it was looked up
                # as, so the row says what it was trying to be rather than
                # defaulting everything to 'word'.
                res = resolved[normalize_key(surface)]
                rows.append((list_id, res.item_id, res.item_type, surface))

            await conn.executemany(
                """
                INSERT INTO word_list_items (list_id, item_id, item_type, surface)
                VALUES ($1, $2, $3, $4)
                """,
                rows,
            )

    return await get_list(pool, user_id, list_id)


async def _load_list_row(pool: asyncpg.Pool, user_id: str, list_id: int) -> dict | None:
    """Ownership filter lives here — every public entry point goes through it."""
    row = await pool.fetchrow(
        """
        SELECT list_id, name, language, description, created_at
        FROM word_lists
        WHERE list_id = $1 AND user_id = $2::uuid
        """,
        list_id, user_id,
    )
    return dict(row) if row else None


async def _load_entries(pool: asyncpg.Pool, user_id: str, list_id: int, language: str) -> list[dict]:
    """
    Entries in insertion order with their current five-state status.

    Entries that already carry an `item_id` use it directly — the stored
    binding is authoritative. Entries with a NULL `item_id` are re-resolved
    from their surface on every read, so a word that had no catalog match at
    upload time reports correctly once a later scraper run adds it. Reads
    never write: the binding is persisted by mark_unknown_as_learning, which
    is the point where the user acts on it.
    """
    rows = await pool.fetch(
        """
        SELECT
            wli.id,
            wli.item_id,
            wli.item_type,
            wli.surface,
            uwk.status AS knowledge_status
        FROM word_list_items wli
        LEFT JOIN user_word_knowledge uwk
               ON uwk.item_id   = wli.item_id
              AND uwk.item_type = wli.item_type
              AND uwk.user_id   = $2::uuid
        WHERE wli.list_id = $1
        ORDER BY wli.id
        """,
        list_id, user_id,
    )

    unbound = [r["surface"] for r in rows if r["item_id"] is None]
    late = await _resolve_surfaces(pool, unbound, language)

    # Freshly-resolved surfaces need their knowledge status too — one extra
    # round-trip, only when some entry actually resolved late. Keyed by
    # (item_id, item_type): a word and a phrase can share an id, since the two
    # catalogs have independent SERIAL sequences.
    late_keys = [(r.item_id, r.item_type) for r in late.values() if r.item_id is not None]
    late_status: dict[tuple[int, str], str] = {}
    if late_keys:
        krows = await pool.fetch(
            """
            SELECT item_id, item_type, status FROM user_word_knowledge
            WHERE user_id = $3::uuid
              AND item_id   = ANY($1::int[])
              AND item_type = ANY($2::text[])
            """,
            [k[0] for k in late_keys], sorted({k[1] for k in late_keys}), user_id,
        )
        late_status = {(r["item_id"], r["item_type"]): r["status"] for r in krows}

    entries = []
    for r in rows:
        item_id = r["item_id"]
        item_type = r["item_type"]
        if item_id is not None:
            status = r["knowledge_status"] or STATUS_UNKNOWN
        else:
            res = late.get(normalize_key(r["surface"]))
            if res is None:
                item_id, item_type, status = None, item_type, STATUS_UNRESOLVED
            else:
                item_id, item_type = res.item_id, res.item_type
                status = res.status or late_status.get(
                    (item_id, item_type)
                ) or STATUS_UNKNOWN
        entries.append({
            "id": r["id"],
            "surface": r["surface"],
            "item_id": item_id,
            "item_type": item_type,
            "status": status,
        })
    return entries


def _counts(entries: list[dict]) -> dict[str, int]:
    counts = dict.fromkeys(ALL_STATUSES, 0)
    for e in entries:
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    return counts


async def get_list(pool: asyncpg.Pool, user_id: str, list_id: int) -> dict | None:
    """Full list with per-entry status. None when absent or owned by someone else."""
    row = await _load_list_row(pool, user_id, list_id)
    if row is None:
        return None
    entries = await _load_entries(pool, user_id, list_id, row["language"])
    return {
        **row,
        "entries": entries,
        "total": len(entries),
        "counts": _counts(entries),
    }


async def list_lists(pool: asyncpg.Pool, user_id: str) -> list[dict]:
    """Summaries (no entries) for the current user, newest first."""
    rows = await pool.fetch(
        """
        SELECT
            wl.list_id, wl.name, wl.language, wl.description, wl.created_at,
            COUNT(wli.id) AS total
        FROM word_lists wl
        LEFT JOIN word_list_items wli ON wli.list_id = wl.list_id
        WHERE wl.user_id = $1::uuid
        GROUP BY wl.list_id
        ORDER BY wl.created_at DESC, wl.list_id DESC
        """,
        user_id,
    )
    return [dict(r) for r in rows]


async def export_list(pool: asyncpg.Pool, user_id: str, list_id: int) -> str | None:
    """
    The list as plain text, one surface per line, in insertion order.

    Includes unresolved and ambiguous surfaces — the export is the user's
    list, not the subset the catalog happened to recognise, so upload →
    download round-trips.
    """
    row = await _load_list_row(pool, user_id, list_id)
    if row is None:
        return None
    surfaces = await pool.fetch(
        "SELECT surface FROM word_list_items WHERE list_id = $1 ORDER BY id",
        list_id,
    )
    return "\n".join(r["surface"] for r in surfaces)


async def mark_unknown_as_learning(
    pool: asyncpg.Pool, user_id: str, list_id: int,
) -> dict | None:
    """
    Flip every resolved-but-unknown entry to 'learning'.

    Only entries bound to exactly one catalog row are touched. `unresolved`
    and `ambiguous` entries are skipped — there is no item to progress, and
    guessing which sense of an ambiguous surface the user meant is exactly
    what this feature refuses to do. Already-learning and already-known
    entries are left alone so the call is idempotent.

    State goes through `progression_service.apply_progression` with the same
    event the words.py status path uses, so levels, auto-promotion and both
    SRS cards behave identically to marking the word learning by hand. This
    service never writes user_word_knowledge or srs_cards itself.

    NOT atomic across entries, by design. Each `apply_progression` call opens
    its own transaction (it takes a pool, not a connection — the same
    constraint `routers/reading.py:160` documents for save-selection). If one
    call fails mid-loop, earlier entries stay marked and later ones do not,
    and the request surfaces the error. That is recoverable rather than
    corrupting: the call is idempotent, so a retry picks up exactly the
    entries that were missed. The binding UPDATE below has the same property —
    a committed binding with no progression still reads as `unknown`, so the
    retry re-targets it.
    """
    row = await _load_list_row(pool, user_id, list_id)
    if row is None:
        return None

    entries = await _load_entries(pool, user_id, list_id, row["language"])
    targets = [e for e in entries if e["status"] == STATUS_UNKNOWN and e["item_id"] is not None]

    # Persist bindings discovered by late re-resolution, so the stored row
    # matches the progression we are about to apply.
    # Persist `item_type` alongside `item_id`. A late resolution can land on
    # the *fallback* catalog — a multi-token surface that only later gains a
    # word_table row resolves as 'word' though it was stored as 'phrase' — so
    # writing the id without the type would leave a row whose join points at
    # the wrong catalog.
    late_binds = [(e["item_id"], e["item_type"], e["id"]) for e in targets]
    if late_binds:
        await pool.executemany(
            "UPDATE word_list_items SET item_id = $1, item_type = $2 "
            "WHERE id = $3 AND item_id IS NULL",
            late_binds,
        )

    for entry in targets:
        await progression_service.apply_progression(
            pool, user_id, entry["item_id"], entry["item_type"],
            "status_marked_learning",
            status_override=STATUS_LEARNING,
        )

    return {
        "list_id": list_id,
        "marked": len(targets),
        "marked_item_ids": [e["item_id"] for e in targets],
        "skipped_unresolved": sum(1 for e in entries if e["status"] == STATUS_UNRESOLVED),
        "skipped_ambiguous": sum(1 for e in entries if e["status"] == STATUS_AMBIGUOUS),
    }


async def delete_list(pool: asyncpg.Pool, user_id: str, list_id: int) -> bool:
    """True when a row was deleted. Entries go with it via ON DELETE CASCADE."""
    result = await pool.execute(
        "DELETE FROM word_lists WHERE list_id = $1 AND user_id = $2::uuid",
        list_id, user_id,
    )
    return result.split()[-1] != "0"
