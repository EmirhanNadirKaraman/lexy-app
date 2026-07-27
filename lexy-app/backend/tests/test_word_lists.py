"""
Vocabulary list upload/download — /api/v1/word-lists.

Covers the five-state model (known / learning / unknown / unresolved /
ambiguous), the resolution rule, user scoping, export round-trip, and the
progression path used by mark-unknown-learning.

Words are created via `_word_helper.insert_owned_word` so each test owns its
surfaces — a shared-catalog `LIMIT 1` pick would be reaped by another xdist
worker mid-test (see docs/TESTS.md).
"""
import uuid

import pytest
from httpx import AsyncClient

from ._email_helper import make_test_email
from ._word_helper import insert_owned_word

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
LISTS = "/api/v1/word-lists"


async def _registered_headers(client: AsyncClient) -> dict:
    email = make_test_email()
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    resp = await client.post(LOGIN, json={"email": email, "password": "password123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _create(client, headers, words, *, name="My list", language="de"):
    return await client.post(
        LISTS,
        json={"name": name, "language": language, "words": words},
        headers=headers,
    )


def _by_surface(detail: dict) -> dict[str, dict]:
    return {e["surface"]: e for e in detail["entries"]}


# ---------------------------------------------------------------------------
# Schema (migration 035)
# ---------------------------------------------------------------------------


async def test_schema_supports_language_surface_and_nullable_item_id(db_pool):
    cols = {
        r["column_name"]: r
        for r in await db_pool.fetch(
            """
            SELECT table_name, column_name, is_nullable
            FROM information_schema.columns
            WHERE table_name IN ('word_lists', 'word_list_items')
            """
        )
    }
    assert cols["language"]["is_nullable"] == "NO"
    assert cols["surface"]["is_nullable"] == "NO"
    assert cols["item_id"]["is_nullable"] == "YES"

    indexes = [
        r["indexdef"]
        for r in await db_pool.fetch(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'word_list_items'"
        )
    ]
    assert any("uq_word_list_items_list_surface" in i for i in indexes)
    # The 001 constraint must be gone, not merely shadowed: NULL item_ids are
    # distinct in Postgres so it could not dedupe unresolved entries, and it
    # would reject two case-variants that resolve to the same word_id.
    assert not any("list_id_item_id_item_type_key" in i for i in indexes)


# ---------------------------------------------------------------------------
# Create + resolution
# ---------------------------------------------------------------------------


async def test_create_list_resolves_words(client: AsyncClient, db_pool):
    _wid, surface = await insert_owned_word(db_pool, language="de")
    headers = await _registered_headers(client)

    resp = await _create(client, headers, [surface])

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "My list"
    assert body["language"] == "de"
    assert body["total"] == 1
    entry = body["entries"][0]
    assert entry["surface"] == surface
    assert entry["status"] == "unknown"
    assert entry["item_id"] is not None


async def test_resolution_is_case_insensitive(client: AsyncClient, db_pool):
    wid, surface = await insert_owned_word(db_pool, language="de")
    headers = await _registered_headers(client)

    resp = await _create(client, headers, [surface.upper()])

    entry = resp.json()["entries"][0]
    assert entry["item_id"] == wid
    # The surface is stored as typed, not normalized to the catalog spelling.
    assert entry["surface"] == surface.upper()


async def test_resolution_is_language_scoped(client: AsyncClient, db_pool):
    _wid, surface = await insert_owned_word(db_pool, language="de")
    headers = await _registered_headers(client)

    # Same surface, list declared as Spanish → must not bind to the German row.
    resp = await _create(client, headers, [surface], language="es")

    entry = resp.json()["entries"][0]
    assert entry["status"] == "unresolved"
    assert entry["item_id"] is None


async def test_unresolved_words_are_stored_not_dropped(client: AsyncClient, db_pool):
    _wid, known_surface = await insert_owned_word(db_pool, language="de")
    missing = f"_nosuchword_{uuid.uuid4().hex[:12]}"
    headers = await _registered_headers(client)

    resp = await _create(client, headers, [known_surface, missing])

    body = resp.json()
    assert body["total"] == 2, "unresolved word must not be silently dropped"
    assert body["counts"]["unresolved"] == 1
    assert _by_surface(body)[missing]["status"] == "unresolved"

    # And it survives a re-read, i.e. it is persisted rather than response-only.
    again = await client.get(f"{LISTS}/{body['list_id']}", headers=headers)
    assert missing in _by_surface(again.json())


async def test_ambiguous_surface_is_reported_not_first_match_resolved(
    client: AsyncClient, db_pool,
):
    """Two catalog rows for one surface → 'ambiguous', never an arbitrary bind.

    Guessing a sense here would attach mastery progress to the wrong meaning
    invisibly (the W3 / Hole 2 concern), so the entry stays unbound.
    """
    surface = f"_ambigword_{uuid.uuid4().hex[:12]}"
    wid_noun, _ = await insert_owned_word(db_pool, language="de", word=surface, pos="NOUN")
    wid_verb, _ = await insert_owned_word(db_pool, language="de", word=surface, pos="VERB")
    headers = await _registered_headers(client)

    resp = await _create(client, headers, [surface])

    entry = resp.json()["entries"][0]
    assert entry["status"] == "ambiguous"
    assert entry["item_id"] is None
    assert entry["item_id"] not in (wid_noun, wid_verb)

    stored = await db_pool.fetchval(
        "SELECT item_id FROM word_list_items WHERE list_id = $1",
        resp.json()["list_id"],
    )
    assert stored is None, "ambiguous entry must not persist a guessed binding"


async def test_duplicate_surfaces_dedupe_case_insensitively(
    client: AsyncClient, db_pool,
):
    _wid, surface = await insert_owned_word(db_pool, language="de")
    headers = await _registered_headers(client)

    resp = await _create(client, headers, [surface, surface.upper(), f"  {surface}  "])

    body = resp.json()
    assert body["total"] == 1
    # First spelling wins, so export round-trips what the user actually pasted.
    assert body["entries"][0]["surface"] == surface


async def test_blank_and_whitespace_entries_are_skipped(client: AsyncClient, db_pool):
    _wid, surface = await insert_owned_word(db_pool, language="de")
    headers = await _registered_headers(client)

    resp = await _create(client, headers, [surface, "", "   ", "\t"])

    assert resp.json()["total"] == 1


async def test_counts_cover_every_state(client: AsyncClient, db_pool):
    _known_id, known_surface = await insert_owned_word(db_pool, language="de")
    _unk_id, unknown_surface = await insert_owned_word(db_pool, language="de")
    ambiguous = f"_ambigword_{uuid.uuid4().hex[:12]}"
    await insert_owned_word(db_pool, language="de", word=ambiguous, pos="NOUN")
    await insert_owned_word(db_pool, language="de", word=ambiguous, pos="VERB")
    missing = f"_nosuchword_{uuid.uuid4().hex[:12]}"
    headers = await _registered_headers(client)

    await client.put(
        f"/api/v1/words/word/{_known_id}/status",
        json={"status": "known"}, headers=headers,
    )

    resp = await _create(
        client, headers, [known_surface, unknown_surface, ambiguous, missing],
    )

    counts = resp.json()["counts"]
    assert counts["known"] == 1
    assert counts["unknown"] == 1
    assert counts["ambiguous"] == 1
    assert counts["unresolved"] == 1


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_oversized_list_returns_422(client: AsyncClient):
    headers = await _registered_headers(client)

    resp = await _create(client, headers, [f"w{i}" for i in range(501)])

    assert resp.status_code == 422


async def test_list_at_the_cap_is_accepted(client: AsyncClient):
    headers = await _registered_headers(client)

    resp = await _create(client, headers, [f"_capword_{i}" for i in range(500)])

    assert resp.status_code == 201


async def test_empty_list_returns_422(client: AsyncClient):
    headers = await _registered_headers(client)

    assert (await _create(client, headers, [])).status_code == 422
    assert (await _create(client, headers, ["", "   "])).status_code == 422


# ---------------------------------------------------------------------------
# Listing / export
# ---------------------------------------------------------------------------


async def test_list_index_returns_only_own_lists(client: AsyncClient, db_pool):
    _wid, surface = await insert_owned_word(db_pool, language="de")
    headers_a = await _registered_headers(client)
    headers_b = await _registered_headers(client)
    await _create(client, headers_a, [surface], name="A list")

    resp = await client.get(LISTS, headers=headers_b)

    assert resp.status_code == 200
    assert resp.json() == []


async def test_export_round_trips_all_surfaces_in_order(client: AsyncClient, db_pool):
    _wid, resolved = await insert_owned_word(db_pool, language="de")
    ambiguous = f"_ambigword_{uuid.uuid4().hex[:12]}"
    await insert_owned_word(db_pool, language="de", word=ambiguous, pos="NOUN")
    await insert_owned_word(db_pool, language="de", word=ambiguous, pos="VERB")
    missing = f"_nosuchword_{uuid.uuid4().hex[:12]}"
    uploaded = [resolved, missing, ambiguous]
    headers = await _registered_headers(client)
    list_id = (await _create(client, headers, uploaded)).json()["list_id"]

    resp = await client.get(f"{LISTS}/{list_id}/export", headers=headers)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    # Unresolved and ambiguous surfaces included, insertion order preserved.
    assert resp.text.split("\n") == uploaded


# ---------------------------------------------------------------------------
# User scoping — 404 (not 403) so ids can't be enumerated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("get", ""),
        ("get", "/export"),
        ("post", "/mark-unknown-learning"),
        ("delete", ""),
    ],
)
async def test_user_b_cannot_touch_user_a_list(
    client: AsyncClient, db_pool, method, suffix,
):
    _wid, surface = await insert_owned_word(db_pool, language="de")
    headers_a = await _registered_headers(client)
    headers_b = await _registered_headers(client)
    list_id = (await _create(client, headers_a, [surface])).json()["list_id"]

    resp = await getattr(client, method)(
        f"{LISTS}/{list_id}{suffix}", headers=headers_b,
    )

    assert resp.status_code == 404

    # The list itself must be untouched by the failed attempt.
    still_there = await client.get(f"{LISTS}/{list_id}", headers=headers_a)
    assert still_there.status_code == 200


async def test_user_b_mark_learning_does_not_progress_user_a_words(
    client: AsyncClient, db_pool,
):
    """The one cross-user route that would MUTATE another user's state."""
    wid, surface = await insert_owned_word(db_pool, language="de")
    headers_a = await _registered_headers(client)
    headers_b = await _registered_headers(client)
    list_id = (await _create(client, headers_a, [surface])).json()["list_id"]

    resp = await client.post(f"{LISTS}/{list_id}/mark-unknown-learning", headers=headers_b)

    assert resp.status_code == 404
    rows = await db_pool.fetch(
        "SELECT status FROM user_word_knowledge WHERE item_id = $1 AND item_type = 'word'",
        wid,
    )
    assert rows == [], "no knowledge row may be created by a cross-user call"


async def test_requires_authentication(client: AsyncClient):
    assert (await client.get(LISTS)).status_code in (401, 403)


async def test_get_missing_list_returns_404(client: AsyncClient):
    headers = await _registered_headers(client)
    assert (await client.get(f"{LISTS}/99999999", headers=headers)).status_code == 404


# ---------------------------------------------------------------------------
# mark-unknown-learning
# ---------------------------------------------------------------------------


async def test_mark_unknown_as_learning_uses_progression_path(
    client: AsyncClient, db_pool,
):
    """
    Same observable end state as PUT /words/word/{id}/status {"status":"learning"}:
    status flipped, passive_level advanced, and BOTH SRS cards created
    (status_marked_learning carries active_srs="create" since #0b).
    """
    wid, surface = await insert_owned_word(db_pool, language="de")
    headers = await _registered_headers(client)
    list_id = (await _create(client, headers, [surface])).json()["list_id"]

    resp = await client.post(f"{LISTS}/{list_id}/mark-unknown-learning", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["marked"] == 1
    assert resp.json()["marked_item_ids"] == [wid]

    user_id = await db_pool.fetchval(
        "SELECT user_id FROM user_word_knowledge WHERE item_id = $1 AND item_type = 'word'",
        wid,
    )
    knowledge = await db_pool.fetchrow(
        """
        SELECT status, passive_level FROM user_word_knowledge
        WHERE user_id = $1 AND item_id = $2 AND item_type = 'word'
        """,
        user_id, wid,
    )
    assert knowledge["status"] == "learning"
    assert knowledge["passive_level"] >= 1

    directions = {
        r["direction"]
        for r in await db_pool.fetch(
            "SELECT direction FROM srs_cards WHERE user_id = $1 AND item_id = $2",
            user_id, wid,
        )
    }
    assert directions == {"passive", "active"}

    # Reflected on the next read of the list.
    detail = await client.get(f"{LISTS}/{list_id}", headers=headers)
    assert detail.json()["entries"][0]["status"] == "learning"


async def test_mark_unknown_skips_unresolved_and_ambiguous(
    client: AsyncClient, db_pool,
):
    wid, resolved = await insert_owned_word(db_pool, language="de")
    ambiguous = f"_ambigword_{uuid.uuid4().hex[:12]}"
    amb_a, _ = await insert_owned_word(db_pool, language="de", word=ambiguous, pos="NOUN")
    amb_b, _ = await insert_owned_word(db_pool, language="de", word=ambiguous, pos="VERB")
    missing = f"_nosuchword_{uuid.uuid4().hex[:12]}"
    headers = await _registered_headers(client)
    list_id = (await _create(client, headers, [resolved, ambiguous, missing])).json()["list_id"]

    resp = await client.post(f"{LISTS}/{list_id}/mark-unknown-learning", headers=headers)

    body = resp.json()
    assert body["marked"] == 1
    assert body["marked_item_ids"] == [wid]
    assert body["skipped_unresolved"] == 1
    assert body["skipped_ambiguous"] == 1

    # Neither sense of the ambiguous surface may gain state.
    for amb_id in (amb_a, amb_b):
        rows = await db_pool.fetch(
            "SELECT 1 FROM user_word_knowledge WHERE item_id = $1 AND item_type = 'word'",
            amb_id,
        )
        assert rows == []

    detail = await client.get(f"{LISTS}/{list_id}", headers=headers)
    statuses = {e["surface"]: e["status"] for e in detail.json()["entries"]}
    assert statuses[ambiguous] == "ambiguous"
    assert statuses[missing] == "unresolved"


async def test_mark_unknown_leaves_known_entries_alone(client: AsyncClient, db_pool):
    wid, surface = await insert_owned_word(db_pool, language="de")
    headers = await _registered_headers(client)
    await client.put(
        f"/api/v1/words/word/{wid}/status", json={"status": "known"}, headers=headers,
    )
    list_id = (await _create(client, headers, [surface])).json()["list_id"]

    resp = await client.post(f"{LISTS}/{list_id}/mark-unknown-learning", headers=headers)

    assert resp.json()["marked"] == 0
    detail = await client.get(f"{LISTS}/{list_id}", headers=headers)
    assert detail.json()["entries"][0]["status"] == "known"


async def test_mark_unknown_is_idempotent(client: AsyncClient, db_pool):
    _wid, surface = await insert_owned_word(db_pool, language="de")
    headers = await _registered_headers(client)
    list_id = (await _create(client, headers, [surface])).json()["list_id"]

    first = await client.post(f"{LISTS}/{list_id}/mark-unknown-learning", headers=headers)
    second = await client.post(f"{LISTS}/{list_id}/mark-unknown-learning", headers=headers)

    assert first.json()["marked"] == 1
    assert second.json()["marked"] == 0, "already-learning entries must not re-fire"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def test_delete_removes_list_and_entries(client: AsyncClient, db_pool):
    _wid, surface = await insert_owned_word(db_pool, language="de")
    headers = await _registered_headers(client)
    list_id = (await _create(client, headers, [surface])).json()["list_id"]

    resp = await client.delete(f"{LISTS}/{list_id}", headers=headers)

    assert resp.status_code == 204
    assert (await client.get(f"{LISTS}/{list_id}", headers=headers)).status_code == 404
    remaining = await db_pool.fetchval(
        "SELECT COUNT(*) FROM word_list_items WHERE list_id = $1", list_id,
    )
    assert remaining == 0
