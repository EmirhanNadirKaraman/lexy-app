"""
Built-in system list seeding — services/system_list_seed_service.py (TODO #43, phase 2).

Driven by `scripts/seed_system_lists.py`; the logic lives in the service so it
is testable without a subprocess (same split as `word_seed_service` /
`backfill_word_catalog.py` and `gloss_seed_service` / `seed_gloss_cache.py`).

**Behaviour tests call `seed_one_list` with a unique per-test name**, never
`seed_system_lists`. The latter uses the two fixed production names, and
seeding is append-only — so a test applying a fixture source through it would
silently add its surfaces to the real "Top German Words" list. The two-list
shape is asserted via a dry-run and against the module constants instead.

The property worth protecting here is that **ambiguous and unresolved surfaces
are kept, not dropped**. 227 of the word list's entries are ambiguous, and they
are the most common words in the language (`ein`, `zu`, `im`, `auf`, `ich`) —
dropping them would gut a "top words" list, and first-matching them would bind
mastery to a coin-flip sense.
"""
import uuid

import pytest
from httpx import AsyncClient

from backend.services import system_list_seed_service as slss
from backend.services.system_list_seed_service import (
    PHRASE_LIST_DESCRIPTION,
    PHRASE_LIST_NAME,
    WORD_LIST_DESCRIPTION,
    WORD_LIST_NAME,
    dedupe,
    read_columns,
)
from ._email_helper import make_test_email
from ._word_helper import insert_owned_word

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
LISTS = "/api/v1/word-lists"


def _letters(n: int = 10) -> str:
    return "".join(chr(ord("a") + int(c, 16)) for c in uuid.uuid4().hex[:n])


def _name() -> str:
    return f"Zz System {_letters()}"


async def _registered_headers(client: AsyncClient) -> dict:
    email = make_test_email()
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    resp = await client.post(LOGIN, json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
async def seeded_lists(db_pool):
    """Reap system lists a test creates, by name. Items cascade."""
    names: list[str] = []

    def _track(*list_names: str) -> None:
        names.extend(list_names)

    yield _track

    if names:
        await db_pool.execute(
            "DELETE FROM word_lists WHERE is_system AND name = ANY($1::text[])", names,
        )


@pytest.fixture
async def make_phrase(db_pool):
    """Local copy — the one in test_word_lists.py is module-scoped there."""
    created: list[int] = []

    async def _make(canonical: str, *, language: str = "de") -> int:
        pid = await db_pool.fetchval(
            "INSERT INTO phrase_table (canonical, surface_form, phrase_type, language) "
            "VALUES ($1, $1, 'verb_pattern', $2) RETURNING phrase_id",
            canonical, language,
        )
        created.append(pid)
        return pid

    yield _make

    if created:
        await db_pool.execute(
            "DELETE FROM user_word_knowledge WHERE item_type='phrase' AND item_id = ANY($1::int[])",
            created)
        await db_pool.execute(
            "DELETE FROM srs_cards WHERE item_type='phrase' AND item_id = ANY($1::int[])", created)
        await db_pool.execute("DELETE FROM phrase_table WHERE phrase_id = ANY($1::int[])", created)


def _source(tmp_path, *rows: tuple[str, str]):
    src = tmp_path / "final_result.txt"
    src.write_text("".join(f"{a}\t{b}\n" for a, b in rows), encoding="utf-8")
    return src


# ---------------------------------------------------------------------------
# Parser — pure
# ---------------------------------------------------------------------------


def test_read_columns_splits_the_two_columns(tmp_path):
    src = _source(tmp_path, ("werden", "werden"), ("haben", "etw. (Akk) haben"))

    col0, col1, skipped = read_columns(src)

    assert col0 == ["werden", "haben"]
    assert col1 == ["werden", "etw. (Akk) haben"]
    assert skipped == 0


def test_read_columns_skips_malformed_and_blank_rows(tmp_path):
    """A tab-less line is malformed and counted; a blank line just ends."""
    src = tmp_path / "final_result.txt"
    src.write_text("werden\twerden\n\nno-tab-here\n\t\nhaben\thaben\n", encoding="utf-8")

    col0, col1, skipped = read_columns(src)

    assert col0 == ["werden", "haben"]
    assert col1 == ["werden", "haben"]
    assert skipped == 1, "only the tab-less line; '\\t' is whitespace-only, i.e. blank"


def test_read_columns_keeps_a_row_with_only_one_cell_filled(tmp_path):
    src = _source(tmp_path, ("werden", ""), ("", "etw. geben"))

    col0, col1, skipped = read_columns(src)

    assert col0 == ["werden"]
    assert col1 == ["etw. geben"]
    assert skipped == 0


def test_dedupe_is_case_insensitive_first_spelling_wins():
    assert dedupe(["Haus", "haus", "HAUS", "Auto"]) == ["Haus", "Auto"]


def test_dedupe_folds_umlauts_stricter_than_the_db_index():
    """`normalize_key`, not SQL `lower()`.

    The `(list_id, lower(surface))` index folds ASCII only under the C locale,
    so it would NOT catch `Öl`/`öl`. Deduping here means the insert can never
    hit that constraint for a pair the database would have missed.
    """
    assert dedupe(["Öl", "öl", "ÖL"]) == ["Öl"]


def test_dedupe_preserves_file_order():
    assert dedupe(["zebra", "apfel", "zebra"]) == ["zebra", "apfel"]


def test_dedupe_drops_blank_surfaces():
    assert dedupe(["", "   ", "Haus"]) == ["Haus"]


# ---------------------------------------------------------------------------
# The two production lists — shape only, no writes
# ---------------------------------------------------------------------------


def test_list_names_and_descriptions_are_stable():
    """Names are the idempotency key, so a rename silently forks the list."""
    assert WORD_LIST_NAME == "Top German Words"
    assert PHRASE_LIST_NAME == "German Verb & Phrase Patterns"
    assert "column 0" in WORD_LIST_DESCRIPTION
    assert "column 1" in PHRASE_LIST_DESCRIPTION


async def test_seed_system_lists_dry_run_plans_two_lists(db_pool, tmp_path):
    src = _source(tmp_path, ("werden", "werden"), ("das Haus", "etw. (Akk) haben"))

    result = await slss.seed_system_lists(db_pool, apply=False, source=src)

    assert result["dry_run"] is True
    assert [lst["name"] for lst in result["lists"]] == [WORD_LIST_NAME, PHRASE_LIST_NAME]
    assert result["source_col0"] == 2 and result["source_col1"] == 2


# ---------------------------------------------------------------------------
# Seeding one list
# ---------------------------------------------------------------------------


async def test_dry_run_writes_nothing(db_pool, tmp_path, tracked_words, seeded_lists):
    _wid, surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    name = _name()
    seeded_lists(name)

    result = await slss.seed_one_list(db_pool, name, "desc", [surface], apply=False)

    assert result["dry_run"] is True
    assert result["items_inserted"] == 1
    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_lists WHERE name = $1", name,
    ) == 0


async def test_apply_creates_a_system_list_row(db_pool, tracked_words, seeded_lists):
    _wid, surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    name = _name()
    seeded_lists(name)

    result = await slss.seed_one_list(db_pool, name, "a description", [surface], apply=True)

    assert result["list_created"] is True
    row = await db_pool.fetchrow(
        "SELECT user_id, is_system, language, description FROM word_lists WHERE name = $1",
        name,
    )
    assert row["user_id"] is None
    assert row["is_system"] is True
    assert row["language"] == "de"
    assert row["description"] == "a description"


async def test_apply_is_idempotent(db_pool, tracked_words, seeded_lists):
    _wid, surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    name = _name()
    seeded_lists(name)

    first = await slss.seed_one_list(db_pool, name, "d", [surface], apply=True)
    second = await slss.seed_one_list(db_pool, name, "d", [surface], apply=True)

    assert first["list_created"] is True and first["items_inserted"] == 1
    assert second["list_created"] is False and second["list_existed"] is True
    assert second["items_inserted"] == 0
    assert second["items_existed"] == 1
    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_lists WHERE name = $1", name) == 1


async def test_existing_list_is_reused_and_new_surfaces_appended(
    db_pool, tracked_words, seeded_lists,
):
    _a, first_surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    _b, second_surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    name = _name()
    seeded_lists(name)

    await slss.seed_one_list(db_pool, name, "d", [first_surface], apply=True)
    result = await slss.seed_one_list(
        db_pool, name, "d", [first_surface, second_surface], apply=True)

    assert result["list_created"] is False
    assert result["items_inserted"] == 1, "only the new surface"
    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_list_items wli JOIN word_lists wl USING (list_id) "
        "WHERE wl.name = $1", name,
    ) == 2


async def test_inserted_count_reflects_actual_rows(db_pool, tracked_words, seeded_lists):
    """A pre-existing item must not be counted as inserted."""
    _a, existing = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    _b, fresh = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    name = _name()
    seeded_lists(name)
    await slss.seed_one_list(db_pool, name, "d", [existing], apply=True)

    result = await slss.seed_one_list(db_pool, name, "d", [existing, fresh], apply=True)

    assert result["items_inserted"] == 1
    assert result["items_existed"] == 1


async def test_duplicate_system_list_names_are_impossible(db_pool, seeded_lists):
    """Migration 037's partial unique index, exercised through the seeder."""
    import asyncpg as _asyncpg

    name = _name()
    seeded_lists(name)
    await slss.seed_one_list(db_pool, name, "d", ["Haus"], apply=True)

    with pytest.raises(_asyncpg.UniqueViolationError):
        await db_pool.execute(
            "INSERT INTO word_lists (user_id, name, language, is_system) "
            "VALUES (NULL, $1, 'de', true)", name,
        )


# ---------------------------------------------------------------------------
# Item content — surfaces, types, and the states that must be preserved
# ---------------------------------------------------------------------------


async def test_original_surfaces_are_stored(db_pool, tracked_words, seeded_lists):
    _wid, surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    name = _name()
    seeded_lists(name)

    await slss.seed_one_list(db_pool, name, "d", [surface], apply=True)

    assert await db_pool.fetchval(
        "SELECT wli.surface FROM word_list_items wli JOIN word_lists wl USING (list_id) "
        "WHERE wl.name = $1", name,
    ) == surface


async def test_words_and_phrases_both_bind(db_pool, tracked_words, seeded_lists, make_phrase):
    word_id, word_surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    canonical = f"jdm zzsys{_letters()} geben"
    phrase_id = await make_phrase(canonical)
    name = _name()
    seeded_lists(name)

    result = await slss.seed_one_list(
        db_pool, name, "d", [word_surface, canonical], apply=True)

    assert result["word"] == 1 and result["phrase"] == 1
    rows = {
        r["surface"]: (r["item_id"], r["item_type"])
        for r in await db_pool.fetch(
            "SELECT wli.surface, wli.item_id, wli.item_type FROM word_list_items wli "
            "JOIN word_lists wl USING (list_id) WHERE wl.name = $1", name)
    }
    assert rows[word_surface] == (word_id, "word")
    assert rows[canonical] == (phrase_id, "phrase")


async def test_ambiguous_surfaces_are_kept_with_null_item_id(
    db_pool, tracked_words, seeded_lists,
):
    """The highest-value property here.

    227 word-list entries are ambiguous and they are the most common words in
    the language. Dropping them would gut the list; first-matching would bind
    mastery to a coin-flip sense.
    """
    surface = f"Zzamb{_letters()}"
    await insert_owned_word(db_pool, word=surface, pos="NOUN")
    await insert_owned_word(db_pool, word=surface, pos="VERB")
    name = _name()
    seeded_lists(name)

    result = await slss.seed_one_list(db_pool, name, "d", [surface], apply=True)

    assert result["ambiguous"] == 1
    assert result["resolved"] == 0
    row = await db_pool.fetchrow(
        "SELECT wli.surface, wli.item_id FROM word_list_items wli "
        "JOIN word_lists wl USING (list_id) WHERE wl.name = $1", name)
    assert row["surface"] == surface, "kept, not dropped"
    assert row["item_id"] is None, "never first-matched"


async def test_unresolved_surfaces_are_kept_with_null_item_id(db_pool, seeded_lists):
    surface = f"Zzunresolved{_letters()}"
    name = _name()
    seeded_lists(name)

    result = await slss.seed_one_list(db_pool, name, "d", [surface], apply=True)

    assert result["unresolved"] == 1
    row = await db_pool.fetchrow(
        "SELECT wli.surface, wli.item_id FROM word_list_items wli "
        "JOIN word_lists wl USING (list_id) WHERE wl.name = $1", name)
    assert row["surface"] == surface
    assert row["item_id"] is None


async def test_counts_add_up(db_pool, tracked_words, seeded_lists):
    _wid, resolved = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    ambiguous = f"Zzamb{_letters()}"
    await insert_owned_word(db_pool, word=ambiguous, pos="NOUN")
    await insert_owned_word(db_pool, word=ambiguous, pos="VERB")
    unresolved = f"Zzunresolved{_letters()}"
    name = _name()
    seeded_lists(name)

    r = await slss.seed_one_list(
        db_pool, name, "d", [resolved, ambiguous, unresolved], apply=True)

    assert (r["resolved"], r["ambiguous"], r["unresolved"]) == (1, 1, 1)
    assert r["resolved"] + r["ambiguous"] + r["unresolved"] == r["unique_surfaces"] == 3
    assert r["items_inserted"] == 3, "every state is stored"


async def test_seeding_makes_no_llm_calls(db_pool, tracked_words, seeded_lists, monkeypatch):
    from backend.services import llm_service

    class _Exploding:
        model_id = "should-not-be-used"

        async def structured(self, *a, **kw):
            raise AssertionError("seeding called the LLM")

    monkeypatch.setattr(llm_service, "_MOCK", False)
    monkeypatch.setattr(llm_service, "_provider", _Exploding())
    _wid, surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    name = _name()
    seeded_lists(name)

    result = await slss.seed_one_list(db_pool, name, "d", [surface], apply=True)

    assert result["items_inserted"] == 1


# ---------------------------------------------------------------------------
# A seeded list through the public API — Phase 1 guarantees still hold
# ---------------------------------------------------------------------------


async def test_seeded_list_is_visible_to_multiple_users(
    client: AsyncClient, db_pool, tracked_words, seeded_lists,
):
    _wid, surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    name = _name()
    seeded_lists(name)
    await slss.seed_one_list(db_pool, name, "d", [surface], apply=True)
    list_id = await db_pool.fetchval("SELECT list_id FROM word_lists WHERE name = $1", name)

    for _ in range(2):
        headers = await _registered_headers(client)
        summaries = (await client.get(LISTS, headers=headers)).json()
        entry = next(r for r in summaries if r["list_id"] == list_id)
        assert entry["is_system"] is True
        assert entry["total"] == 1


async def test_normal_user_cannot_delete_a_seeded_list(
    client: AsyncClient, db_pool, tracked_words, seeded_lists,
):
    _wid, surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    name = _name()
    seeded_lists(name)
    await slss.seed_one_list(db_pool, name, "d", [surface], apply=True)
    list_id = await db_pool.fetchval("SELECT list_id FROM word_lists WHERE name = $1", name)
    headers = await _registered_headers(client)

    resp = await client.delete(f"{LISTS}/{list_id}", headers=headers)

    assert resp.status_code == 404
    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_lists WHERE list_id = $1", list_id) == 1


async def test_mark_learning_on_seeded_list_leaves_shared_rows_alone(
    client: AsyncClient, db_pool, tracked_words, seeded_lists,
):
    """Seeded rows already carry item_id, so late binding has nothing to do —
    but the guard must still hold for the ambiguous rows, which are NULL."""
    ambiguous = f"Zzamb{_letters()}"
    await insert_owned_word(db_pool, word=ambiguous, pos="NOUN")
    await insert_owned_word(db_pool, word=ambiguous, pos="VERB")
    _wid, resolved = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    name = _name()
    seeded_lists(name)
    await slss.seed_one_list(db_pool, name, "d", [resolved, ambiguous], apply=True)
    list_id = await db_pool.fetchval("SELECT list_id FROM word_lists WHERE name = $1", name)
    headers = await _registered_headers(client)

    before = await db_pool.fetch(
        "SELECT id, item_id, item_type FROM word_list_items WHERE list_id = $1 ORDER BY id",
        list_id)
    resp = await client.post(f"{LISTS}/{list_id}/mark-unknown-learning", headers=headers)
    after = await db_pool.fetch(
        "SELECT id, item_id, item_type FROM word_list_items WHERE list_id = $1 ORDER BY id",
        list_id)

    assert resp.status_code == 200
    assert resp.json()["marked"] == 1
    assert resp.json()["skipped_ambiguous"] == 1
    assert [dict(r) for r in before] == [dict(r) for r in after], "shared rows unchanged"


async def test_two_users_marking_a_seeded_list_stay_independent(
    client: AsyncClient, db_pool, tracked_words, seeded_lists,
):
    word_id, surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    name = _name()
    seeded_lists(name)
    await slss.seed_one_list(db_pool, name, "d", [surface], apply=True)
    list_id = await db_pool.fetchval("SELECT list_id FROM word_lists WHERE name = $1", name)
    headers_a = await _registered_headers(client)
    headers_b = await _registered_headers(client)

    await client.post(f"{LISTS}/{list_id}/mark-unknown-learning", headers=headers_a)

    counts_b = (await client.get(f"{LISTS}/{list_id}", headers=headers_b)).json()["counts"]
    assert counts_b["unknown"] == 1, "user B is unaffected by A's progress"
    assert await db_pool.fetchval(
        "SELECT count(*) FROM user_word_knowledge WHERE item_id = $1 AND item_type = 'word'",
        word_id,
    ) == 1


async def test_user_created_lists_are_untouched_by_seeding(
    client: AsyncClient, db_pool, tracked_words, seeded_lists,
):
    _wid, surface = await insert_owned_word(db_pool, word=f"Zzsys{_letters()}")
    headers = await _registered_headers(client)
    own = await client.post(
        LISTS, json={"name": "My own", "language": "de", "words": [surface]}, headers=headers)
    own_id = own.json()["list_id"]
    name = _name()
    seeded_lists(name)

    await slss.seed_one_list(db_pool, name, "d", [surface], apply=True)

    detail = (await client.get(f"{LISTS}/{own_id}", headers=headers)).json()
    assert detail["is_system"] is False
    assert detail["total"] == 1
    assert await db_pool.fetchval(
        "SELECT user_id IS NOT NULL FROM word_lists WHERE list_id = $1", own_id) is True
