"""
Word knowledge tests — upsert behaviour on first and subsequent calls.

These tests require at least one row in word_table (populated by the
subtitle ingestion pipeline). They will be skipped if the table is empty.
"""
import uuid

import pytest
from httpx import AsyncClient
from ._email_helper import cleanup_pattern, make_test_email

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
KNOWLEDGE = "/api/v1/words/knowledge"
BY_TEXT = "/api/v1/words/by-text"


def make_email() -> str:
    return make_test_email()


async def _registered_token(client: AsyncClient) -> str:
    """Register a fresh user and return its JWT."""
    email = make_email()
    await client.post(REGISTER, json={"email": email, "password": "password123"})
    resp = await client.post(LOGIN, json={"email": email, "password": "password123"})
    return resp.json()["access_token"]


async def _get_word_id(db_pool) -> int:
    """Return a real word_id from word_table, or skip the test if none exists.
    Excludes synthetic test surfaces (which carry digits/underscores) so a
    LIMIT-1 grab can't return another suite's leftover fixture row."""
    word_id = await db_pool.fetchval(
        "SELECT word_id FROM word_table WHERE word !~ '[0-9_]' ORDER BY word_id LIMIT 1"
    )
    if word_id is None:
        pytest.skip("word_table has no plain word — run the subtitle pipeline first")
    return word_id


# ---------------------------------------------------------------------------
# GET /knowledge
# ---------------------------------------------------------------------------


async def test_knowledge_is_empty_for_new_user(client: AsyncClient):
    token = await _registered_token(client)
    resp = await client.get(KNOWLEDGE, headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# PUT /{item_type}/{item_id}/status — insert on first call
# ---------------------------------------------------------------------------


async def test_first_status_update_creates_row(client: AsyncClient, db_pool):
    word_id = await _get_word_id(db_pool)
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["item_id"] == word_id
    assert data["item_type"] == "word"
    assert data["status"] == "learning"
    # status update + progression now run in a single transaction, so the
    # response reflects the post-progression state. status_marked_learning has
    # passive_delta=1.
    assert data["passive_level"] == 1
    assert data["active_level"] == 0


async def test_first_status_update_appears_in_knowledge_list(client: AsyncClient, db_pool):
    word_id = await _get_word_id(db_pool)
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    knowledge = await client.get(KNOWLEDGE, headers=headers)
    items = knowledge.json()
    assert len(items) == 1
    assert items[0]["item_id"] == word_id
    assert items[0]["status"] == "learning"


# ---------------------------------------------------------------------------
# PUT /{item_type}/{item_id}/status — update on second call
# ---------------------------------------------------------------------------


async def test_second_status_update_changes_status(client: AsyncClient, db_pool):
    word_id = await _get_word_id(db_pool)
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/words/word/{word_id}/status"

    await client.put(url, json={"status": "learning"}, headers=headers)
    resp = await client.put(url, json={"status": "known"}, headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "known"


async def test_second_status_update_does_not_create_duplicate(client: AsyncClient, db_pool):
    word_id = await _get_word_id(db_pool)
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/words/word/{word_id}/status"

    await client.put(url, json={"status": "learning"}, headers=headers)
    await client.put(url, json={"status": "known"}, headers=headers)

    knowledge = await client.get(KNOWLEDGE, headers=headers)
    matching = [i for i in knowledge.json() if i["item_id"] == word_id]
    assert len(matching) == 1  # still exactly one row


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_invalid_status_returns_422(client: AsyncClient, db_pool):
    word_id = await _get_word_id(db_pool)
    token = await _registered_token(client)

    resp = await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "mastered"},  # not a valid status
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_invalid_item_type_returns_422(client: AsyncClient, db_pool):
    word_id = await _get_word_id(db_pool)
    token = await _registered_token(client)

    resp = await client.put(
        f"/api/v1/words/bogus_type/{word_id}/status",
        json={"status": "learning"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /by-text — progress fields
# ---------------------------------------------------------------------------


async def _get_word_text_and_language(db_pool) -> tuple[str, str]:
    """Return (word, language) for a real word, skipping if none. Excludes
    synthetic test surfaces (digits/underscores) so leftover fixture rows from
    other suites can't be picked by a LIMIT-1 grab."""
    row = await db_pool.fetchrow(
        "SELECT word, language FROM word_table WHERE word !~ '[0-9_]' ORDER BY word_id LIMIT 1"
    )
    if row is None:
        pytest.skip("word_table has no plain word — run the subtitle pipeline first")
    return row["word"], row["language"]


async def test_by_text_returns_progress_fields_for_unknown_word(client: AsyncClient, db_pool):
    """A word with no knowledge row returns zero levels and null due dates."""
    word, language = await _get_word_text_and_language(db_pool)
    token = await _registered_token(client)

    resp = await client.get(
        BY_TEXT,
        params={"word": word, "language": language},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    # W3: response is a discriminated shape. Real word_table data has
    # genuinely ambiguous surface forms; either single or ambiguous is fine
    # — pick the first available candidate to inspect its progress fields.
    assert data["status"] in {"single", "ambiguous"}
    item = data["item"] or (data["candidates"][0] if data["candidates"] else None)
    assert item is not None
    assert item["passive_level"] == 0
    assert item["active_level"] == 0
    assert item["passive_due"] is None
    assert item["active_due"] is None


async def test_by_text_returns_nonzero_levels_after_status_update(client: AsyncClient, db_pool):
    """After marking a word 'learning', passive_level should be > 0."""
    word, language = await _get_word_text_and_language(db_pool)
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    # First get the word_id
    lookup = await client.get(BY_TEXT, params={"word": word, "language": language}, headers=headers)
    assert lookup.status_code == 200
    body = lookup.json()
    # Tolerate either single or ambiguous — pick the first match either way.
    item = body["item"] or (body["candidates"][0] if body["candidates"] else None)
    assert item is not None
    word_id = item["word_id"]

    # Mark it as learning (fires status_marked_learning → passive_delta=1)
    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )

    # Re-fetch and check levels advanced
    resp = await client.get(BY_TEXT, params={"word": word, "language": language}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    item = body["item"] or (body["candidates"][0] if body["candidates"] else None)
    assert item is not None
    assert item["passive_level"] > 0
    assert item["current_status"] == "learning"


async def test_by_text_returns_not_found_for_unknown_word_not_in_db(client: AsyncClient):
    """A word that doesn't exist in word_table returns status='not_found'."""
    token = await _registered_token(client)

    resp = await client.get(
        BY_TEXT,
        params={"word": "xyzzy_no_such_word_123", "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "not_found"
    assert body["item"] is None
    assert body["candidates"] == []


# ---------------------------------------------------------------------------
# PUT status=known — contract: confidence, not fabricated active mastery.
# Mirrors the actual frontend call. Pinned to lock the behaviour.
# ---------------------------------------------------------------------------

async def _get_word_for_lookup(db_pool) -> tuple[int, str, str]:
    """Return (word_id, word_text, language) for a real row that supports
    by-text lookup. Excludes synthetic test surfaces (digits/underscores)."""
    row = await db_pool.fetchrow(
        "SELECT word_id, word, language FROM word_table WHERE word !~ '[0-9_]' "
        "ORDER BY word_id LIMIT 1"
    )
    if row is None:
        pytest.skip("word_table has no plain word")
    return row["word_id"], row["word"], row["language"]


async def test_put_known_writes_status_known(client: AsyncClient, db_pool):
    """Status field is written inside apply_progression's transaction via
    status_override — a single atomic write covering status + SRS + levels."""
    word_id, word, language = await _get_word_for_lookup(db_pool)
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "known"},
        headers=headers,
    )

    assert resp.status_code == 200
    assert resp.json()["status"] == "known"

    # Round-trip via /by-text to confirm persisted state.
    lookup = await client.get(BY_TEXT, params={"word": word, "language": language}, headers=headers)
    body = lookup.json()
    item = body["item"] or (body["candidates"][0] if body["candidates"] else None)
    assert item is not None
    assert item["current_status"] == "known"


async def test_put_known_does_not_create_active_srs_card(client: AsyncClient, db_pool):
    """Manual known must NOT schedule active production review."""
    word_id, _, _ = await _get_word_for_lookup(db_pool)
    token = await _registered_token(client)
    uid = await db_pool.fetchval(
        "SELECT user_id FROM users WHERE email LIKE $1 ORDER BY user_id DESC LIMIT 1", cleanup_pattern()
    )

    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "known"},
        headers={"Authorization": f"Bearer {token}"},
    )

    active = await db_pool.fetchrow(
        "SELECT card_id FROM srs_cards WHERE user_id=$1::uuid AND item_id=$2 "
        "AND item_type='word' AND direction='active'",
        uid, word_id,
    )
    assert active is None


async def test_put_known_does_not_increment_active_level(client: AsyncClient, db_pool):
    """Manual known must not inflate active_level — that's reserved for production evidence."""
    word_id, _, _ = await _get_word_for_lookup(db_pool)
    token = await _registered_token(client)
    uid = await db_pool.fetchval(
        "SELECT user_id FROM users WHERE email LIKE $1 ORDER BY user_id DESC LIMIT 1", cleanup_pattern()
    )

    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "known"},
        headers={"Authorization": f"Bearer {token}"},
    )

    row = await db_pool.fetchrow(
        "SELECT active_level, times_used_correctly FROM user_word_knowledge "
        "WHERE user_id=$1::uuid AND item_id=$2 AND item_type='word'",
        uid, word_id,
    )
    # Row is created by apply_progression (via status_override) — assert level fields stay zero.
    if row is not None:
        assert row["active_level"] == 0
        assert row["times_used_correctly"] == 0


async def test_put_known_after_learning_does_not_advance_active_card(client: AsyncClient, db_pool):
    """If a learning word already has an active SRS card (from #0b), marking
    it known must NOT advance it as if the user answered correctly."""
    word_id, _, _ = await _get_word_for_lookup(db_pool)
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    uid = await db_pool.fetchval(
        "SELECT user_id FROM users WHERE email LIKE $1 ORDER BY user_id DESC LIMIT 1", cleanup_pattern()
    )

    # First mark learning → creates active card at interval=1.0, reps=0.
    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "learning"},
        headers=headers,
    )
    before = await db_pool.fetchrow(
        "SELECT interval_days, repetitions, ease_factor, due_date FROM srs_cards "
        "WHERE user_id=$1::uuid AND item_id=$2 AND item_type='word' AND direction='active'",
        uid, word_id,
    )
    assert before is not None

    # Now mark known. Must NOT advance the active card.
    await client.put(
        f"/api/v1/words/word/{word_id}/status",
        json={"status": "known"},
        headers=headers,
    )

    after = await db_pool.fetchrow(
        "SELECT interval_days, repetitions, ease_factor, due_date FROM srs_cards "
        "WHERE user_id=$1::uuid AND item_id=$2 AND item_type='word' AND direction='active'",
        uid, word_id,
    )
    assert after is not None
    assert after["repetitions"]   == before["repetitions"]
    assert after["interval_days"] == before["interval_days"]
    assert after["ease_factor"]   == before["ease_factor"]
    assert after["due_date"]      == before["due_date"]


# ---------------------------------------------------------------------------
# W2 / Hole 1 — POST /words/learn-anyway
# ---------------------------------------------------------------------------

LEARN_ANYWAY = "/api/v1/words/learn-anyway"


def _unique_text() -> str:
    """A surface form guaranteed not to clash with any existing word_table row."""
    return f"ζtest_{uuid.uuid4().hex[:8]}"


async def _user_id_for(db_pool, token: str) -> str:
    """Resolve the user_id from an auth token (looks up by the most recent
    cleanup-patterned email — works because each test makes a fresh user)."""
    row = await db_pool.fetchrow(
        """
        SELECT user_id FROM users
         WHERE email LIKE $1
         ORDER BY created_at DESC LIMIT 1
        """,
        cleanup_pattern(),
    )
    assert row is not None, "no user found via cleanup pattern"
    return str(row["user_id"])


async def test_learn_anyway_creates_word_table_row(client: AsyncClient, db_pool, tracked_words):
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    text = _unique_text()

    resp = await client.post(LEARN_ANYWAY, json={"text": text, "language": "de"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    tracked_words.append(body["word_id"])
    assert body["word"].lower() == text.lower()
    assert body["current_status"] == "learning"

    # Row exists in word_table with pos='X' sparse placeholder.
    row = await db_pool.fetchrow(
        "SELECT word, language, pos, lemma FROM word_table WHERE word = $1 AND language = $2",
        text, "de",
    )
    assert row is not None
    assert row["pos"] == "X"
    assert row["lemma"] == text


async def test_learn_anyway_does_not_duplicate_existing_word(client: AsyncClient, db_pool, tracked_words):
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    text = _unique_text()

    r1 = await client.post(LEARN_ANYWAY, json={"text": text, "language": "de"}, headers=headers)
    r2 = await client.post(LEARN_ANYWAY, json={"text": text, "language": "de"}, headers=headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["word_id"] == r2.json()["word_id"], "idempotent: same word_id on re-call"
    tracked_words.append(r1.json()["word_id"])

    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM word_table WHERE word = $1 AND language = $2",
        text, "de",
    )
    assert count == 1


async def test_learn_anyway_creates_user_word_knowledge_learning(client: AsyncClient, db_pool, tracked_words):
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    text = _unique_text()

    resp = await client.post(LEARN_ANYWAY, json={"text": text, "language": "de"}, headers=headers)
    assert resp.status_code == 200
    word_id = resp.json()["word_id"]
    tracked_words.append(word_id)
    uid = await _user_id_for(db_pool, token)

    row = await db_pool.fetchrow(
        """
        SELECT status, passive_level, active_level, times_used_correctly
          FROM user_word_knowledge
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
        """,
        uid, word_id,
    )
    assert row is not None
    assert row["status"] == "learning"
    # exposure-only: active_level and times_used_correctly stay at 0
    assert row["active_level"] == 0
    assert row["times_used_correctly"] == 0


async def test_learn_anyway_creates_both_srs_cards(client: AsyncClient, db_pool, tracked_words):
    """status_marked_learning creates passive AND active cards (#0b)."""
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    text = _unique_text()

    resp = await client.post(LEARN_ANYWAY, json={"text": text, "language": "de"}, headers=headers)
    word_id = resp.json()["word_id"]
    tracked_words.append(word_id)
    uid = await _user_id_for(db_pool, token)

    directions = await db_pool.fetch(
        """
        SELECT direction FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
         ORDER BY direction
        """,
        uid, word_id,
    )
    assert {d["direction"] for d in directions} == {"passive", "active"}


async def test_learn_anyway_requires_auth(client: AsyncClient):
    resp = await client.post(LEARN_ANYWAY, json={"text": "auto", "language": "de"})
    assert resp.status_code == 403


async def test_learn_anyway_empty_text_returns_422(client: AsyncClient):
    token = await _registered_token(client)
    resp = await client.post(LEARN_ANYWAY,
                             json={"text": "", "language": "de"},
                             headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 422


async def test_learn_anyway_whitespace_only_text_returns_422(client: AsyncClient):
    """Pydantic min_length=1 catches '' but not '   '; service-level strip catches the rest."""
    token = await _registered_token(client)
    # whitespace-only after strip; Pydantic min_length=1 still passes "   "
    resp = await client.post(LEARN_ANYWAY,
                             json={"text": "   ", "language": "de"},
                             headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 422


async def test_learn_anyway_over_max_length_returns_422(client: AsyncClient):
    token = await _registered_token(client)
    resp = await client.post(LEARN_ANYWAY,
                             json={"text": "x" * 200, "language": "de"},
                             headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# W3 / Hole 2 — disambiguation of ambiguous surface forms
# ---------------------------------------------------------------------------

async def _create_ambiguous_word(db_pool, tracked: list[int], surface: str = None) -> tuple[str, list[int]]:
    """Create 2 word_table rows sharing (word, language) but with different
    POS — simulates the "die Bank" (bench / financial institution) case.
    Returns (surface, [word_id_noun, word_id_verb]). UNIQUE(word, language, pos)
    allows the two rows. Every created word_id is appended to `tracked` (the
    tracked_words fixture) so the rows are reaped after the test — word_table is
    global, so unreaped synthetic rows pollute every other suite's LIMIT-1
    picks. The random `Bnk_<hex>` surface keeps inserts unique per call, so
    parallel xdist workers never share (and so never trample) a row."""
    if surface is None:
        surface = f"Bnk_{uuid.uuid4().hex[:8]}"
    rows = []
    for pos, tag, lemma in [("NOUN", "NN", surface), ("VERB", "VVFIN", f"{surface.lower()}_v")]:
        row = await db_pool.fetchrow(
            """
            INSERT INTO word_table (word, language, pos, tag, lemma)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (word, language, pos) DO NOTHING
            RETURNING word_id
            """,
            surface, "de", pos, tag, lemma,
        )
        if row is None:
            row = await db_pool.fetchrow(
                "SELECT word_id FROM word_table WHERE word = $1 AND language = 'de' AND pos = $2",
                surface, pos,
            )
        rows.append(row["word_id"])
    tracked.extend(rows)
    return surface, rows


async def test_by_text_single_match_returns_single_status(client: AsyncClient, db_pool):
    """Existing single-match behaviour preserved (back-compat for unambiguous words)."""
    word, language = await _get_word_text_and_language(db_pool)
    token = await _registered_token(client)
    resp = await client.get(BY_TEXT, params={"word": word, "language": language},
                            headers={"Authorization": f"Bearer {token}"})
    body = resp.json()
    # Most existing word_table rows are non-ambiguous; either single or
    # ambiguous is acceptable here, but item must be set when single.
    assert body["status"] in {"single", "ambiguous"}
    if body["status"] == "single":
        assert body["item"] is not None
        assert len(body["candidates"]) == 1


async def test_by_text_ambiguous_returns_all_candidates_not_arbitrary_pick(client: AsyncClient, db_pool, tracked_words):
    """Hole 2 regression guard: multi-match must surface ALL candidates."""
    surface, word_ids = await _create_ambiguous_word(db_pool, tracked_words)
    token = await _registered_token(client)
    resp = await client.get(BY_TEXT, params={"word": surface, "language": "de"},
                            headers={"Authorization": f"Bearer {token}"})
    body = resp.json()
    assert body["status"] == "ambiguous"
    assert body["item"] is None, "must NOT pick one arbitrary winner"
    candidate_ids = {c["word_id"] for c in body["candidates"]}
    assert candidate_ids == set(word_ids)


async def test_by_text_candidates_include_user_progress_fields(client: AsyncClient, db_pool, tracked_words):
    """Candidates carry per-user current_status / passive_level (UI needs to show them)."""
    surface, word_ids = await _create_ambiguous_word(db_pool, tracked_words)
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Mark the first row as learning so its current_status differs.
    await client.put(f"/api/v1/words/word/{word_ids[0]}/status",
                     json={"status": "learning"}, headers=headers)

    resp = await client.get(BY_TEXT, params={"word": surface, "language": "de"}, headers=headers)
    body = resp.json()
    by_id = {c["word_id"]: c for c in body["candidates"]}
    assert by_id[word_ids[0]]["current_status"] == "learning"
    assert by_id[word_ids[1]]["current_status"] is None


async def test_by_text_candidates_sorted_deterministically(client: AsyncClient, db_pool, tracked_words):
    """Sort: exact case-insensitive word match first, then lemma asc, then word_id."""
    surface, word_ids = await _create_ambiguous_word(db_pool, tracked_words)
    token = await _registered_token(client)
    resp = await client.get(BY_TEXT, params={"word": surface, "language": "de"},
                            headers={"Authorization": f"Bearer {token}"})
    body = resp.json()
    candidates = body["candidates"]
    # Both rows have word == surface, so the case-insensitive tier is tied;
    # within tie, sorted by lemma asc. NOUN lemma == surface, VERB lemma
    # == surface.lower() + '_v' — _v sorts after the bare surface for ASCII,
    # so NOUN comes first. Stable, predictable.
    assert candidates[0]["pos"] == "NOUN"
    assert candidates[1]["pos"] == "VERB"


async def test_by_text_candidates_include_pos_field(client: AsyncClient, db_pool, tracked_words):
    """W3 added `pos` to the WordLookupResult shape."""
    surface, _ = await _create_ambiguous_word(db_pool, tracked_words)
    token = await _registered_token(client)
    resp = await client.get(BY_TEXT, params={"word": surface, "language": "de"},
                            headers={"Authorization": f"Bearer {token}"})
    candidates = resp.json()["candidates"]
    poss = {c["pos"] for c in candidates}
    assert poss == {"NOUN", "VERB"}


async def test_learn_anyway_still_works_after_lookup_refactor(client: AsyncClient, db_pool, tracked_words):
    """W2 path still works: learn-anyway creates a row + flips status to learning."""
    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    text = _unique_text()
    resp = await client.post(LEARN_ANYWAY, json={"text": text, "language": "de"}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    tracked_words.append(body["word_id"])
    assert body["current_status"] == "learning"
    assert body["pos"] == "X"  # sparse placeholder retained


# ---------------------------------------------------------------------------
# word_table pollution cleanup — proof of the tracked_words reap mechanism
# ---------------------------------------------------------------------------

async def test_reap_word_ids_removes_synthetic_rows(db_pool):
    """Proof that the tracked_words teardown actually deletes what it tracks.

    word_table is global (not user-scoped), so the autouse user cleanup can't
    reap synthetic rows — this `_reap_word_ids` call (run by the tracked_words
    fixture after every test) is what keeps the table from accumulating
    ζtest_/Bnk_ pollution. Insert two synthetic rows, reap by id, confirm gone,
    and confirm a second reap is a harmless no-op."""
    from .conftest import _reap_word_ids

    ids = []
    for surface in (f"ζtest_{uuid.uuid4().hex[:8]}", f"Bnk_{uuid.uuid4().hex[:8]}"):
        wid = await db_pool.fetchval(
            "INSERT INTO word_table (word, language, pos, tag, lemma) "
            "VALUES ($1, 'de', 'X', 'X', $1) RETURNING word_id",
            surface,
        )
        ids.append(wid)

    present = await db_pool.fetchval(
        "SELECT count(*) FROM word_table WHERE word_id = ANY($1::int[])", ids
    )
    assert present == 2

    await _reap_word_ids(db_pool, ids)
    after = await db_pool.fetchval(
        "SELECT count(*) FROM word_table WHERE word_id = ANY($1::int[])", ids
    )
    assert after == 0, "reap must delete all tracked synthetic rows"

    # Idempotent: reaping already-gone ids (and an empty list) must not error.
    await _reap_word_ids(db_pool, ids)
    await _reap_word_ids(db_pool, [])


# ---------------------------------------------------------------------------
# Unicode lookup + learn-anyway de-duplication (C-collation bug, 2026-07-28)
#
# The database folds ASCII only (`'Öl' ILIKE 'öl'` is false), so the old
# `WHERE w.word ILIKE $1` lookup missed every catalog word carrying an
# uppercase non-ASCII letter. That miss was not just a display bug: the picker
# then offered "Learn anyway", and learn-anyway's
# `ON CONFLICT (word, language, pos)` cannot conflict with a row stored under a
# different `pos`, so accepting forked the surface into a second row — which
# `word_list_service` reports as `ambiguous` for every user, permanently.
#
# Both halves are covered here. Fixtures carry real umlauts on purpose: an
# ASCII fixture passes against the broken code.
# ---------------------------------------------------------------------------


async def _insert_word(db_pool, tracked: list[int], word: str, *, pos: str = "",
                       language: str = "de") -> int:
    """Insert one catalog row and register it for reaping."""
    wid = await db_pool.fetchval(
        "INSERT INTO word_table (word, language, pos, tag, lemma) "
        "VALUES ($1, $2, $3, '', $1) RETURNING word_id",
        word, language, pos,
    )
    tracked.append(wid)
    return wid


def _umlaut_surface(initial: str) -> str:
    return f"{initial}zzw{uuid.uuid4().hex[:10]}"


@pytest.mark.parametrize("initial", ["Ö", "Ü", "Ä"])
async def test_by_text_finds_umlaut_word_with_stored_spelling(
    client: AsyncClient, db_pool, tracked_words, initial,
):
    """Baseline guard: the stored spelling must keep resolving.

    Unlike vocabulary lists (which lowered in Python before querying), this
    endpoint passed the raw input to `ILIKE`, so a byte-exact spelling already
    worked. This pins that it still does after the switch to `resolve_word_ids`
    — it is a regression guard, not a bug reproduction. The case-variant tests
    below are the ones that failed before the fix.
    """
    surface = _umlaut_surface(initial)
    wid = await _insert_word(db_pool, tracked_words, surface)

    token = await _registered_token(client)
    resp = await client.get(
        BY_TEXT, params={"word": surface, "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "single", f"expected single, got {body['status']}"
    assert body["item"]["word_id"] == wid


@pytest.mark.parametrize("initial", ["Ö", "Ü", "Ä"])
async def test_by_text_finds_umlaut_word_from_lowercased_input(
    client: AsyncClient, db_pool, tracked_words, initial,
):
    surface = _umlaut_surface(initial)
    wid = await _insert_word(db_pool, tracked_words, surface)

    token = await _registered_token(client)
    resp = await client.get(
        BY_TEXT, params={"word": surface.lower(), "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.json()["item"]["word_id"] == wid, "öl must find the stored Öl"


async def test_by_text_finds_umlaut_word_from_uppercased_input(
    client: AsyncClient, db_pool, tracked_words,
):
    surface = _umlaut_surface("Ö")
    wid = await _insert_word(db_pool, tracked_words, surface)

    token = await _registered_token(client)
    resp = await client.get(
        BY_TEXT, params={"word": surface.upper(), "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.json()["item"]["word_id"] == wid


async def test_by_text_returns_the_stored_spelling_not_the_typed_one(
    client: AsyncClient, db_pool, tracked_words,
):
    """A case-variant hit must report the canonical stored surface."""
    surface = _umlaut_surface("Ö")
    await _insert_word(db_pool, tracked_words, surface)

    token = await _registered_token(client)
    resp = await client.get(
        BY_TEXT, params={"word": surface.lower(), "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.json()["item"]["word"] == surface


@pytest.mark.parametrize(
    "sharp_s,double_s",
    [("straße", "strasse"), ("schließen", "schliessen")],
)
async def test_sharp_s_and_double_s_are_not_merged(
    client: AsyncClient, db_pool, tracked_words, sharp_s, double_s,
):
    """`casefold()` maps ß to ss and would merge these into one ambiguous key.

    They are distinct German entries (Swiss vs. standard orthography), so
    `normalize_key` uses `lower()`. Each must resolve to its own row, `single`
    rather than `ambiguous`.
    """
    uniq = uuid.uuid4().hex[:10]
    sharp_id = await _insert_word(db_pool, tracked_words, f"{sharp_s}{uniq}")
    double_id = await _insert_word(db_pool, tracked_words, f"{double_s}{uniq}")

    token = await _registered_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    r_sharp = await client.get(
        BY_TEXT, params={"word": f"{sharp_s}{uniq}", "language": "de"}, headers=headers)
    r_double = await client.get(
        BY_TEXT, params={"word": f"{double_s}{uniq}", "language": "de"}, headers=headers)

    assert r_sharp.json()["status"] == "single"
    assert r_double.json()["status"] == "single"
    assert r_sharp.json()["item"]["word_id"] == sharp_id
    assert r_double.json()["item"]["word_id"] == double_id


async def test_by_text_umlaut_duplicates_still_report_ambiguous(
    client: AsyncClient, db_pool, tracked_words,
):
    """Genuine duplicates are still never first-matched (W3 / Hole 2)."""
    surface = _umlaut_surface("Ö")
    await _insert_word(db_pool, tracked_words, surface, pos="NOUN")
    await _insert_word(db_pool, tracked_words, surface.lower(), pos="VERB")

    token = await _registered_token(client)
    resp = await client.get(
        BY_TEXT, params={"word": surface, "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    body = resp.json()
    assert body["status"] == "ambiguous"
    assert body["item"] is None
    assert len(body["candidates"]) == 2


async def test_by_text_is_still_language_scoped(
    client: AsyncClient, db_pool, tracked_words,
):
    surface = _umlaut_surface("Ü")
    await _insert_word(db_pool, tracked_words, surface, language="es")

    token = await _registered_token(client)
    resp = await client.get(
        BY_TEXT, params={"word": surface, "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.json()["status"] == "not_found"


# --- learn-anyway must reuse, never fork ------------------------------------


async def test_learn_anyway_reuses_existing_pos_empty_row(
    client: AsyncClient, db_pool, tracked_words,
):
    """The core anti-fork case: backfilled rows carry pos=''.

    `ON CONFLICT (word, language, pos)` on a pos='X' insert does NOT conflict
    with a pos='' row, so the old code added a second row and made the surface
    permanently ambiguous in vocabulary lists.
    """
    surface = f"Zzlearn{uuid.uuid4().hex[:10]}"
    existing_id = await _insert_word(db_pool, tracked_words, surface, pos="")

    token = await _registered_token(client)
    resp = await client.post(
        LEARN_ANYWAY, json={"text": surface, "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    assert resp.json()["word_id"] == existing_id, "must reuse, not create"
    rows = await db_pool.fetch(
        "SELECT pos FROM word_table WHERE word = $1 AND language = 'de'", surface)
    assert [r["pos"] for r in rows] == [""], "a pos='X' row would fork the surface"


async def test_learn_anyway_reuses_existing_scraper_pos_row(
    client: AsyncClient, db_pool, tracked_words,
):
    """Scraper rows carry a real POS — same fork risk as pos=''."""
    surface = f"Zzlearn{uuid.uuid4().hex[:10]}"
    existing_id = await _insert_word(db_pool, tracked_words, surface, pos="NOUN")

    token = await _registered_token(client)
    resp = await client.post(
        LEARN_ANYWAY, json={"text": surface, "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.json()["word_id"] == existing_id
    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_table WHERE word = $1 AND language = 'de'", surface,
    ) == 1


async def test_learn_anyway_reuses_existing_row_for_unicode_case_variant(
    client: AsyncClient, db_pool, tracked_words,
):
    """Existing `Öl`, user adopts `öl` — the exact production fork path.

    The lookup used to miss (ILIKE folds ASCII only), the picker offered
    "Learn anyway", and accepting created a second row.
    """
    surface = _umlaut_surface("Ö")
    existing_id = await _insert_word(db_pool, tracked_words, surface, pos="")

    token = await _registered_token(client)
    resp = await client.post(
        LEARN_ANYWAY, json={"text": surface.lower(), "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.json()["word_id"] == existing_id
    rows = await db_pool.fetch(
        "SELECT word FROM word_table WHERE language = 'de' AND word = ANY($1::text[])",
        [surface, surface.lower()],
    )
    assert [r["word"] for r in rows] == [surface], "case variant forked the surface"


async def test_learn_anyway_still_creates_a_row_for_a_genuinely_new_word(
    client: AsyncClient, db_pool, tracked_words,
):
    """The fix must not swing the other way and refuse to adopt new words."""
    surface = _umlaut_surface("Ä")

    token = await _registered_token(client)
    resp = await client.post(
        LEARN_ANYWAY, json={"text": surface, "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    body = resp.json()
    tracked_words.append(body["word_id"])
    assert body["current_status"] == "learning"
    row = await db_pool.fetchrow(
        "SELECT pos, lemma FROM word_table WHERE word = $1 AND language = 'de'", surface)
    assert row is not None
    assert row["pos"] == "X", "genuinely new words keep the sparse placeholder"
    assert row["lemma"] == surface


async def test_learn_anyway_on_ambiguous_surface_does_not_add_a_third_row(
    client: AsyncClient, db_pool, tracked_words,
):
    """An already-forked surface must not be forked further.

    The response model is a single WordLookupResult, so this reuses the lowest
    word_id — the same row the old code returned via `_lookup_first_match`,
    minus the extra insert.
    """
    surface, ids = await _create_ambiguous_word(db_pool, tracked_words)

    token = await _registered_token(client)
    resp = await client.post(
        LEARN_ANYWAY, json={"text": surface, "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    assert resp.json()["word_id"] == min(ids)
    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_table WHERE word = $1 AND language = 'de'", surface,
    ) == 2, "no third row"


async def test_learn_anyway_is_language_scoped(
    client: AsyncClient, db_pool, tracked_words,
):
    """A Spanish row must not satisfy a German adopt."""
    surface = f"Zzlearn{uuid.uuid4().hex[:10]}"
    await _insert_word(db_pool, tracked_words, surface, language="es")

    token = await _registered_token(client)
    resp = await client.post(
        LEARN_ANYWAY, json={"text": surface, "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )

    body = resp.json()
    tracked_words.append(body["word_id"])
    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_table WHERE word = $1 AND language = 'de'", surface,
    ) == 1
