"""
Reading → progression tests.

Source of truth: lexy-app/features/reading-progression/tests.md

Covers:
  Unit (no DB):
    - _interval_days for all six schedule slots and the beyond-cap case
  Unit (DB):
    - find_catalog_item: resolves word, returns None for unknown canonical
    - record_review: got_it increments count + sets next_review_at,
                     still_learning resets count + sets next_review_at=NOW,
                     mastered sets status='mastered' + clears next_review_at
  Integration (real DB, HTTP):
    - save_selection with catalog-matched canonical → passive SRS card created
    - save_selection with no catalog match → selection saved, no SRS card
    - review got_it → SM-2 passive card advances (repetitions + 1)
    - review mastered → status='mastered', SRS card unchanged
    - get_due_selections returns newly-saved (NULL next_review_at)
    - get_due_selections excludes mastered
"""
import uuid

import pytest
from httpx import AsyncClient

from backend.services.reading_service import _interval_days, find_catalog_item
from ._email_helper import make_test_email
from ._auth_helper import register_and_login

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _email() -> str:
    return make_test_email()


async def _register_and_login(client: AsyncClient, db_pool, email: str) -> tuple[dict, str]:
    """Register + login. Returns (auth_headers, user_id_str).

    Delegates to the shared helper; see tests/_auth_helper.py.
    """
    return await register_and_login(client, db_pool, email)


async def _get_word(db_pool) -> tuple[int, str, str]:
    # find_catalog_item() must resolve this surface back through the matcher /
    # lemmatizer, so the word has to be a REAL corpus word — a synthetic
    # `_testword_<uuid>` wouldn't round-trip. We use the same race-safe pattern
    # as test_words.py: exclude synthetic surfaces (digits/underscores, which
    # real words never carry) and pick deterministically by word_id, so no
    # reapable row can ever be selected. (See docs/TESTS.md.)
    row = await db_pool.fetchrow(
        "SELECT word_id, word, language FROM word_table "
        "WHERE word !~ '[0-9_]' ORDER BY word_id LIMIT 1"
    )
    if row is None:
        pytest.skip("word_table has no plain word — run the subtitle pipeline first")
    return row["word_id"], row["word"], row["language"]


async def _create_doc(db_pool, uid: str, language: str) -> str:
    """Insert a minimal book_documents row owned by the test user.

    Deleted automatically when the user row is CASCADE-deleted by the cleanup fixture.
    """
    doc_id = str(await db_pool.fetchval(
        """
        INSERT INTO book_documents
            (user_id, title, filename, file_path, language, source_type, status)
        VALUES ($1::uuid, 'Test Book', 'test.pdf', '/tmp/test.pdf', $2, 'pdf', 'ready')
        RETURNING doc_id
        """,
        uid, language,
    ))
    return doc_id


async def _save_selection_direct(db_pool, uid: str, doc_id: str, canonical: str) -> str:
    """Insert a reading_selection row directly (bypasses HTTP + progression)."""
    sel_id = str(await db_pool.fetchval(
        """
        INSERT INTO reading_selections
            (user_id, doc_id, canonical, surface_text, sentence_text, anchors)
        VALUES ($1::uuid, $2::uuid, $3, $3, $3, '[]'::jsonb)
        RETURNING selection_id
        """,
        uid, doc_id, canonical,
    ))
    return sel_id


async def _get_passive_srs_card(db_pool, uid: str, item_id: int, item_type: str) -> dict | None:
    row = await db_pool.fetchrow(
        """
        SELECT interval_days, ease_factor, repetitions
          FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2
           AND item_type = $3 AND direction = 'passive'
        """,
        uid, item_id, item_type,
    )
    return dict(row) if row else None


def _sel_body(word: str) -> dict:
    return {
        "canonical": word,
        "surface_text": word,
        "sentence_text": f"Test sentence with {word}.",
        "anchors": [],
        "note": None,
    }


# ---------------------------------------------------------------------------
# Unit tests — _interval_days (pure function, no DB)
# ---------------------------------------------------------------------------

def test_interval_days_first_review():
    assert _interval_days(0) == 1

def test_interval_days_second_review():
    assert _interval_days(1) == 2

def test_interval_days_third_review():
    assert _interval_days(2) == 4

def test_interval_days_fourth_review():
    assert _interval_days(3) == 7

def test_interval_days_fifth_review():
    assert _interval_days(4) == 14

def test_interval_days_sixth_review():
    assert _interval_days(5) == 30

def test_interval_days_beyond_sixth_is_capped_at_30():
    assert _interval_days(6) == 30
    assert _interval_days(100) == 30


# ---------------------------------------------------------------------------
# Unit tests — find_catalog_item (DB)
# ---------------------------------------------------------------------------

async def test_find_catalog_item_resolves_word(client: AsyncClient, db_pool):
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    result = await find_catalog_item(db_pool, doc_id, word)

    assert result is not None
    item_id, item_type = result
    assert item_id == word_id
    assert item_type == "word"


async def test_find_catalog_item_returns_none_for_unknown_canonical(
    client: AsyncClient, db_pool
):
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    result = await find_catalog_item(db_pool, doc_id, "xyzzy_nonexistent_12345")

    assert result is None


# ---------------------------------------------------------------------------
# Unit tests — record_review (DB, service layer)
# ---------------------------------------------------------------------------

async def test_record_review_got_it_increments_review_count(client: AsyncClient, db_pool):
    from backend.services.reading_service import record_review
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)
    sel_id = await _save_selection_direct(db_pool, uid, doc_id, word)

    result = await record_review(db_pool, sel_id, uid, "got_it")

    assert result is not None
    assert result["review_count"] == 1
    assert result["next_review_at"] is not None


async def test_record_review_still_learning_resets_count(client: AsyncClient, db_pool):
    from backend.services.reading_service import record_review
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)
    sel_id = await _save_selection_direct(db_pool, uid, doc_id, word)

    await record_review(db_pool, sel_id, uid, "got_it")
    result = await record_review(db_pool, sel_id, uid, "still_learning")

    assert result["review_count"] == 0
    assert result["next_review_at"] is not None  # due immediately


async def test_record_review_mastered_sets_status_and_clears_date(
    client: AsyncClient, db_pool
):
    from backend.services.reading_service import record_review
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)
    sel_id = await _save_selection_direct(db_pool, uid, doc_id, word)

    result = await record_review(db_pool, sel_id, uid, "mastered")

    assert result["status"] == "mastered"
    assert result["next_review_at"] is None


# ---------------------------------------------------------------------------
# Integration tests — HTTP
# ---------------------------------------------------------------------------

async def test_save_selection_with_word_match_creates_passive_srs_card(
    client: AsyncClient, db_pool
):
    """Catalog match on save fires status_marked_learning → passive SRS card created."""
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(word),
        headers=headers,
    )
    assert resp.status_code == 201

    card = await _get_passive_srs_card(db_pool, uid, word_id, "word")
    assert card is not None
    assert card["interval_days"] == 1.0
    assert card["ease_factor"] == 2.5
    assert card["repetitions"] == 0


async def test_save_selection_no_catalog_match_creates_no_srs_card(
    client: AsyncClient, db_pool
):
    """Uncatalogued canonical: selection is saved but no SRS card is created."""
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body("xyzzy_not_in_word_table_phrase_abc"),
        headers=headers,
    )
    assert resp.status_code == 201

    count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM srs_cards WHERE user_id = $1::uuid",
        uid,
    )
    assert count == 0


# ---------------------------------------------------------------------------
# Save-selection → status='learning' atomic (#5 follow-up)
#
# Before: save fired status_marked_learning WITHOUT status_override, so
# user_word_knowledge.status stayed 'unknown' until passive evidence later
# auto-promoted it. Product-wise, saving from reading means "I want to learn
# this" — the status should flip atomically.
#
# After: save passes status_override='learning'. The existing #2 atomicity
# fix folds the status flip into the same transaction as the level/SRS
# deltas, so a successful save means status='learning' is persisted.
# ---------------------------------------------------------------------------

async def test_save_selection_with_word_match_marks_learning(
    client: AsyncClient, db_pool
):
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(word),
        headers=headers,
    )
    assert resp.status_code == 201

    uwk = await db_pool.fetchrow(
        "SELECT status, passive_level, active_level, times_used_correctly "
        "FROM user_word_knowledge "
        "WHERE user_id=$1::uuid AND item_id=$2 AND item_type='word'",
        uid, word_id,
    )
    assert uwk is not None
    assert uwk["status"] == "learning"
    # status_marked_learning passive_delta=1; active stays 0 per #0b.
    assert uwk["passive_level"] == 1
    assert uwk["active_level"] == 0
    assert uwk["times_used_correctly"] == 0


async def test_save_selection_creates_both_srs_cards_for_word_match(
    client: AsyncClient, db_pool
):
    """status_marked_learning (with override) creates BOTH passive and active
    SRS cards per #0b. Saving a matched reading selection is the same shape."""
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(word),
        headers=headers,
    )
    assert resp.status_code == 201

    passive = await db_pool.fetchrow(
        "SELECT card_id, repetitions FROM srs_cards "
        "WHERE user_id=$1::uuid AND item_id=$2 AND item_type='word' AND direction='passive'",
        uid, word_id,
    )
    active = await db_pool.fetchrow(
        "SELECT card_id, repetitions FROM srs_cards "
        "WHERE user_id=$1::uuid AND item_id=$2 AND item_type='word' AND direction='active'",
        uid, word_id,
    )
    assert passive is not None
    assert active is not None
    assert passive["repetitions"] == 0
    assert active["repetitions"] == 0


async def test_save_selection_with_phrase_match_marks_learning(
    client: AsyncClient, db_pool
):
    """Same contract for phrase_table matches."""
    phrase_row = await db_pool.fetchrow(
        "SELECT phrase_id, surface_form, language FROM phrase_table LIMIT 1"
    )
    if phrase_row is None:
        pytest.skip("phrase_table is empty")

    phrase_id     = phrase_row["phrase_id"]
    surface_form  = phrase_row["surface_form"]
    language      = phrase_row["language"]

    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(surface_form),
        headers=headers,
    )
    assert resp.status_code == 201

    uwk = await db_pool.fetchrow(
        "SELECT status, passive_level, active_level "
        "FROM user_word_knowledge "
        "WHERE user_id=$1::uuid AND item_id=$2 AND item_type='phrase'",
        uid, phrase_id,
    )
    assert uwk is not None
    assert uwk["status"] == "learning"
    assert uwk["passive_level"] == 1
    assert uwk["active_level"] == 0


async def test_save_selection_unmatched_creates_no_uwk_row(
    client: AsyncClient, db_pool
):
    """No catalog match → save still succeeds, but no user_word_knowledge row
    is created (catalog progression is skipped entirely)."""
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    nonsense = f"zzz_not_in_catalog_{uuid.uuid4().hex[:8]}"
    resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(nonsense),
        headers=headers,
    )
    assert resp.status_code == 201

    uwk_count = await db_pool.fetchval(
        "SELECT COUNT(*) FROM user_word_knowledge WHERE user_id=$1::uuid",
        uid,
    )
    assert uwk_count == 0


async def test_review_got_it_advances_passive_srs_card(client: AsyncClient, db_pool):
    """got_it fires passive_review_correct → SM-2 repetitions increments."""
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    # Save via HTTP to get both the selection and the SRS card
    save_resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(word),
        headers=headers,
    )
    assert save_resp.status_code == 201
    sel_id = save_resp.json()["selection_id"]

    card_before = await _get_passive_srs_card(db_pool, uid, word_id, "word")
    assert card_before is not None

    resp = await client.post(
        f"/api/v1/reading/selections/{sel_id}/review",
        json={"outcome": "got_it"},
        headers=headers,
    )
    assert resp.status_code == 200

    card_after = await _get_passive_srs_card(db_pool, uid, word_id, "word")
    assert card_after["repetitions"] == card_before["repetitions"] + 1
    assert card_after["interval_days"] > card_before["interval_days"]


async def test_review_mastered_marks_catalog_item_known(client: AsyncClient, db_pool):
    """Reading 'Mastered' on a catalog-matched selection (#5 / Hole 24 fix):
    fires status_marked_known on the linked word/phrase via status_override.
    The user_word_knowledge row's status flips to 'known' atomically inside the
    standard progression transaction.
    """
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    save_resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(word),
        headers=headers,
    )
    sel_id = save_resp.json()["selection_id"]

    # The save already runs apply_progression('status_marked_learning'), giving
    # passive_level=1, status='unknown' (no auto-promotion until threshold).
    uwk_before = await db_pool.fetchrow(
        "SELECT status, passive_level, active_level FROM user_word_knowledge "
        "WHERE user_id=$1::uuid AND item_id=$2 AND item_type='word'",
        uid, word_id,
    )
    assert uwk_before is not None
    assert uwk_before["status"] != "known"

    resp = await client.post(
        f"/api/v1/reading/selections/{sel_id}/review",
        json={"outcome": "mastered"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "mastered"

    uwk_after = await db_pool.fetchrow(
        "SELECT status, passive_level, active_level, times_used_correctly "
        "FROM user_word_knowledge WHERE user_id=$1::uuid AND item_id=$2 AND item_type='word'",
        uid, word_id,
    )
    assert uwk_after["status"] == "known"


async def test_review_mastered_does_not_inflate_active(client: AsyncClient, db_pool):
    """Critical policy: reading 'Mastered' is manual known confidence, NOT
    active production evidence. active_level must stay 0; times_used_correctly
    must not bump; an active SRS card that already exists (saving a selection
    fires status_marked_learning per #0b which creates one) must NOT be
    advanced as if the user had produced the word."""
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    save_resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(word),
        headers=headers,
    )
    sel_id = save_resp.json()["selection_id"]

    # Capture the active card baseline (may or may not exist; if it does it
    # was created at reps=0 by status_marked_learning per #0b).
    active_before = await db_pool.fetchrow(
        "SELECT card_id, repetitions, interval_days, ease_factor FROM srs_cards "
        "WHERE user_id=$1::uuid AND item_id=$2 AND item_type='word' AND direction='active'",
        uid, word_id,
    )

    resp = await client.post(
        f"/api/v1/reading/selections/{sel_id}/review",
        json={"outcome": "mastered"},
        headers=headers,
    )
    assert resp.status_code == 200

    uwk = await db_pool.fetchrow(
        "SELECT active_level, times_used_correctly FROM user_word_knowledge "
        "WHERE user_id=$1::uuid AND item_id=$2 AND item_type='word'",
        uid, word_id,
    )
    # status_marked_known has active_delta=0 + times_used_correctly_delta=0.
    assert uwk["active_level"] == 0
    assert uwk["times_used_correctly"] == 0

    # status_marked_known has active_srs=None — the active card is neither
    # created (if missing) nor advanced (if present).
    active_after = await db_pool.fetchrow(
        "SELECT card_id, repetitions, interval_days, ease_factor FROM srs_cards "
        "WHERE user_id=$1::uuid AND item_id=$2 AND item_type='word' AND direction='active'",
        uid, word_id,
    )
    if active_before is None:
        assert active_after is None, "mastered must not fabricate an active card"
    else:
        assert active_after is not None
        assert active_after["repetitions"]   == active_before["repetitions"]
        assert active_after["interval_days"] == active_before["interval_days"]
        assert active_after["ease_factor"]   == active_before["ease_factor"]


async def test_review_mastered_advances_passive_card_via_status_marked_known(
    client: AsyncClient, db_pool,
):
    """status_marked_known has passive_srs='correct' — the passive SRS card
    advances via SM-2 (consistent with the manual Known click in the vocab UI).
    This is the desired behaviour: the user demonstrated recognition; reward
    the schedule."""
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    save_resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(word),
        headers=headers,
    )
    sel_id = save_resp.json()["selection_id"]

    card_before = await _get_passive_srs_card(db_pool, uid, word_id, "word")
    assert card_before is not None

    resp = await client.post(
        f"/api/v1/reading/selections/{sel_id}/review",
        json={"outcome": "mastered"},
        headers=headers,
    )
    assert resp.status_code == 200

    card_after = await _get_passive_srs_card(db_pool, uid, word_id, "word")
    assert card_after is not None
    assert card_after["repetitions"]   == card_before["repetitions"] + 1
    assert card_after["interval_days"] > card_before["interval_days"]


async def test_review_mastered_unmatched_succeeds(client: AsyncClient, db_pool):
    """A selection whose canonical has no catalog match (e.g. a multi-word
    expression not in phrase_table) must still mark mastered successfully —
    catalog lookup miss is not an error."""
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    # canonical that won't match word_table or phrase_table
    nonsense = f"zzz_not_in_catalog_{uuid.uuid4().hex[:8]}"
    save_resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json={
            "canonical":     nonsense,
            "surface_text":  nonsense,
            "sentence_text": f"a sentence with {nonsense} in it.",
            "anchors":       [],
            "note":          None,
        },
        headers=headers,
    )
    assert save_resp.status_code == 201
    sel_id = save_resp.json()["selection_id"]

    resp = await client.post(
        f"/api/v1/reading/selections/{sel_id}/review",
        json={"outcome": "mastered"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "mastered"


async def test_due_selections_includes_newly_saved(client: AsyncClient, db_pool):
    """Newly saved selection (next_review_at IS NULL) appears in the due list."""
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(word),
        headers=headers,
    )

    resp = await client.get("/api/v1/reading/selections/due", headers=headers)
    assert resp.status_code == 200
    assert any(s["canonical"] == word for s in resp.json())


async def test_due_selections_excludes_mastered(client: AsyncClient, db_pool):
    """Mastered selections must not appear in the due list."""
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    save_resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(word),
        headers=headers,
    )
    sel_id = save_resp.json()["selection_id"]

    await client.post(
        f"/api/v1/reading/selections/{sel_id}/review",
        json={"outcome": "mastered"},
        headers=headers,
    )

    resp = await client.get("/api/v1/reading/selections/due", headers=headers)
    assert resp.status_code == 200
    assert not any(s["canonical"] == word for s in resp.json())
