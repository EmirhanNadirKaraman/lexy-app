"""User vocabulary lists — upload a word list, see what you already know, export it.

Closes the gap between a pasted/uploaded vocabulary list and the rest of the
app: every resolved entry carries a `word_table.word_id`, which is exactly the
`item_ids` shape `playlist_service.generate_playlist` consumes.

Storage lives in `word_lists` / `word_list_items` (migration 001, extended by
035). Every entry stores its original `surface`; `item_id` is the binding to
`word_table` and is NULL when the surface did not bind.

Entry states surfaced to the user (five, not four):
  known / learning / unknown  — bound to a catalog row, from user_word_knowledge
  unresolved                  — no language-scoped match in word_table
  ambiguous                   — several matches; deliberately NOT auto-picked

Scope: entries are `item_type = 'word'` and resolve against `word_table` only.
Phrases are deliberately out of scope for this first version, so a multi-word
surface (*sich freuen auf*) reports `unresolved` even though `phrase_table`
may hold it. The frontend's `parseWordInput` does not split on spaces, so such
surfaces do reach here intact — wiring them to `phrase_table` is the natural
next step and needs no schema change (`item_type` already carries the column).

`ambiguous` exists because `word_service.lookup_word_by_text` refuses to guess
between several rows for one surface (*die Bank* = bench vs. bank — different
pos/lemma, different meaning). That refusal is the W3 / Hole 2 fix. A bulk
upload has no interactive picker, so rather than silently binding to a
first-match, an ambiguous surface is stored with `item_id = NULL` and reported
as such. A wrong binding would attach mastery progress to the wrong meaning
invisibly; an ambiguous label is recoverable.
"""
import asyncpg

from . import progression_service

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


def normalize_surfaces(words: list[str]) -> list[str]:
    """
    Strip, drop blanks, and dedupe case-insensitively while preserving the
    order the user supplied. The first spelling of a duplicate wins, so
    export round-trips what was actually pasted.

    Deduping here (not in SQL) keeps the unique index on
    (list_id, lower(surface)) a backstop rather than an error path.
    """
    seen: set[str] = set()
    out: list[str] = []
    for raw in words:
        surface = raw.strip()
        if not surface:
            continue
        key = surface.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(surface)
    return out


async def _resolve_surfaces(
    conn: asyncpg.Connection | asyncpg.Pool,
    surfaces: list[str],
    language: str,
) -> dict[str, list[int]]:
    """
    Bulk-resolve surfaces against word_table, language-scoped and
    case-insensitive. Returns lower(surface) → [word_id, ...] (ordered).

    One round-trip for the whole list rather than N lookups. The predicate is
    `lower(w.word) = ANY($1)` — not `ILIKE` — so it uses
    `ix_word_table_lang_lower_word (language, lower(word) text_pattern_ops)`
    from migration 032. Comparing Python's `lower()` output against
    Postgres's also fails safe: if the two ever disagree on some edge glyph
    the word reports as `unresolved` instead of binding to the wrong row.
    """
    if not surfaces:
        return {}

    keys = sorted({s.lower() for s in surfaces})
    rows = await conn.fetch(
        """
        SELECT w.word_id, lower(w.word) AS key
        FROM word_table w
        WHERE w.language = $2
          AND lower(w.word) = ANY($1::text[])
        ORDER BY lower(w.word), w.word_id
        """,
        keys,
        language,
    )

    resolved: dict[str, list[int]] = {}
    for r in rows:
        resolved.setdefault(r["key"], []).append(r["word_id"])
    return resolved


def _classify(word_ids: list[int]) -> tuple[int | None, str | None]:
    """
    (item_id, forced_status) for a surface's match list.

    0 matches → (None, 'unresolved'); 2+ → (None, 'ambiguous'); exactly 1 →
    (word_id, None), meaning "bound — read the real status from knowledge".
    """
    if not word_ids:
        return None, STATUS_UNRESOLVED
    if len(word_ids) > 1:
        return None, STATUS_AMBIGUOUS
    return word_ids[0], None


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
                item_id, _forced = _classify(resolved.get(surface.lower(), []))
                rows.append((list_id, item_id, "word", surface))

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
    # round-trip, only when some entry actually resolved late.
    late_ids = [ids[0] for ids in late.values() if len(ids) == 1]
    late_status: dict[int, str] = {}
    if late_ids:
        krows = await pool.fetch(
            """
            SELECT item_id, status FROM user_word_knowledge
            WHERE user_id = $2::uuid AND item_type = 'word' AND item_id = ANY($1::int[])
            """,
            late_ids, user_id,
        )
        late_status = {r["item_id"]: r["status"] for r in krows}

    entries = []
    for r in rows:
        item_id = r["item_id"]
        if item_id is not None:
            status = r["knowledge_status"] or STATUS_UNKNOWN
        else:
            item_id, forced = _classify(late.get(r["surface"].lower(), []))
            status = forced or late_status.get(item_id) or STATUS_UNKNOWN
        entries.append({
            "id": r["id"],
            "surface": r["surface"],
            "item_id": item_id,
            "item_type": r["item_type"],
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
    late_binds = [(e["item_id"], e["id"]) for e in targets]
    if late_binds:
        await pool.executemany(
            "UPDATE word_list_items SET item_id = $1 WHERE id = $2 AND item_id IS NULL",
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
