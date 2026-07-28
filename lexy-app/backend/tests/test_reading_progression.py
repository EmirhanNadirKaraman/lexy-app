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

from backend.services import catalog_resolver, reading_service
from backend.services.reading_service import _interval_days, find_catalog_item
from backend.services.text_norm import normalize_key
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
    #
    # It must ALSO be unambiguous. Since 2026-07-28 `find_catalog_item` fails
    # safe on a surface with several catalog rows instead of silently binding
    # to whichever one Postgres returned first, so an ambiguous pick makes
    # every binding assertion below fail. The old lowest-word_id pick was
    # `das`, which has two German rows — these tests were passing only because
    # the resolver used to guess. Uniqueness is checked with `normalize_key`,
    # not SQL `lower()`, so it agrees with the resolver under the C collation.
    rows = await db_pool.fetch(
        "SELECT word_id, word, language FROM word_table "
        "WHERE word !~ '[0-9_]' ORDER BY word_id LIMIT 2000"
    )
    counts: dict[tuple[str, str], int] = {}
    for r in rows:
        counts[(normalize_key(r["word"]), r["language"])] = (
            counts.get((normalize_key(r["word"]), r["language"]), 0) + 1
        )
    for r in rows:
        if counts[(normalize_key(r["word"]), r["language"])] == 1:
            return r["word_id"], r["word"], r["language"]
    pytest.skip("word_table has no unambiguous plain word — run the subtitle pipeline first")


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
    """Same contract for phrase_table matches.

    Selects on `canonical`, which is what `find_catalog_item` has matched
    since 2026-07-28 (it used to match `surface_form`, disagreeing with
    vocabulary lists on the 773 German rows where the two columns differ).
    `ORDER BY phrase_id` makes the pick deterministic — a bare `LIMIT 1` was
    an xdist flake waiting to happen (docs/TESTS.md).
    """
    phrase_row = await db_pool.fetchrow(
        "SELECT phrase_id, canonical, language FROM phrase_table "
        "ORDER BY phrase_id LIMIT 1"
    )
    if phrase_row is None:
        pytest.skip("phrase_table is empty")

    phrase_id = phrase_row["phrase_id"]
    canonical = phrase_row["canonical"]
    language  = phrase_row["language"]

    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, language)

    resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(canonical),
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


# ---------------------------------------------------------------------------
# Unicode + shared-resolver binding (2026-07-28)
#
# `find_catalog_item` had three problems, all fixed by delegating to
# `catalog_resolver`:
#   1. `LOWER(word) = $1` fed `canonical.lower()`. Postgres folds ASCII only
#      here, so a selection of `öl` never bound and never reached the main SRS.
#   2. Phrases matched `phrase_table.surface_form` while vocabulary lists
#      matched `canonical`; 773 German rows differ, so the same phrase bound to
#      different rows depending on entry point.
#   3. A bare `fetchrow` silently first-matched an ambiguous surface, attaching
#      mastery to a coin-flip sense.
# ---------------------------------------------------------------------------

def _letters(n: int = 12) -> str:
    return "".join(chr(ord("a") + int(c, 16)) for c in uuid.uuid4().hex[:n])


async def _owned_word(db_pool, tracked: list[int], word: str, *,
                      pos: str = "", language: str = "de") -> int:
    wid = await db_pool.fetchval(
        "INSERT INTO word_table (word, language, pos, tag, lemma) "
        "VALUES ($1, $2, $3, '', $1) RETURNING word_id",
        word, language, pos,
    )
    tracked.append(wid)
    return wid


@pytest.mark.parametrize("initial", ["Ö", "Ü", "Ä"])
async def test_find_catalog_item_resolves_umlaut_word_from_lowercase(
    client: AsyncClient, db_pool, tracked_words, initial,
):
    """The selection text arrives lowercased from the frontend."""
    surface = f"{initial}zzr{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    result = await find_catalog_item(db_pool, doc_id, surface.lower())

    assert result == (word_id, "word")


async def test_find_catalog_item_resolves_umlaut_word_from_stored_spelling(
    client: AsyncClient, db_pool, tracked_words,
):
    surface = f"Özzr{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    assert await find_catalog_item(db_pool, doc_id, surface) == (word_id, "word")


@pytest.mark.parametrize("sharp_s,double_s", [("straße", "strasse"), ("schließen", "schliessen")])
async def test_find_catalog_item_does_not_merge_sharp_s_with_double_s(
    client: AsyncClient, db_pool, tracked_words, sharp_s, double_s,
):
    """`casefold()` would merge these and bind the wrong catalog row."""
    uniq = _letters()
    sharp_id = await _owned_word(db_pool, tracked_words, f"{sharp_s}{uniq}")
    double_id = await _owned_word(db_pool, tracked_words, f"{double_s}{uniq}")
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    assert await find_catalog_item(db_pool, doc_id, f"{sharp_s}{uniq}") == (sharp_id, "word")
    assert await find_catalog_item(db_pool, doc_id, f"{double_s}{uniq}") == (double_id, "word")


async def test_find_catalog_item_fails_safe_on_ambiguous_surface(
    client: AsyncClient, db_pool, tracked_words,
):
    """Two rows for one surface must bind to NEITHER.

    This used to be a bare `fetchrow`, so it silently took whichever row came
    back first — a coin flip between two senses, attaching mastery to the wrong
    one invisibly. Reading has no ambiguity response shape, so it fails safe:
    the selection is still saved and reviewable on its own schedule, it just
    does not advance a possibly-wrong catalog item.
    """
    surface = f"Zzr{_letters()}"
    await _owned_word(db_pool, tracked_words, surface, pos="NOUN")
    await _owned_word(db_pool, tracked_words, surface, pos="VERB")
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    assert await find_catalog_item(db_pool, doc_id, surface) is None


async def test_find_catalog_item_umlaut_case_variants_are_ambiguous_together(
    client: AsyncClient, db_pool, tracked_words,
):
    """`Öl` + `öl` are one key now, so two such rows are a genuine duplicate."""
    surface = f"Özzr{_letters()}"
    await _owned_word(db_pool, tracked_words, surface, pos="NOUN")
    await _owned_word(db_pool, tracked_words, surface.lower(), pos="VERB")
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    assert await find_catalog_item(db_pool, doc_id, surface.lower()) is None


async def test_find_catalog_item_is_language_scoped(
    client: AsyncClient, db_pool, tracked_words,
):
    surface = f"Üzzr{_letters()}"
    await _owned_word(db_pool, tracked_words, surface, language="es")
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    assert await find_catalog_item(db_pool, doc_id, surface.lower()) is None


# --- canonical, not surface_form -------------------------------------------


@pytest.fixture
async def owned_phrase(db_pool):
    """A phrase whose `canonical` differs from its `surface_form`."""
    created: list[int] = []

    async def _make(canonical: str, surface_form: str, *, language: str = "de") -> int:
        pid = await db_pool.fetchval(
            "INSERT INTO phrase_table (canonical, surface_form, phrase_type, language) "
            "VALUES ($1, $2, 'verb_pattern', $3) RETURNING phrase_id",
            canonical, surface_form, language,
        )
        created.append(pid)
        return pid

    yield _make

    if created:
        await db_pool.execute(
            "DELETE FROM user_word_knowledge WHERE item_type='phrase' AND item_id = ANY($1::int[])",
            created,
        )
        await db_pool.execute(
            "DELETE FROM srs_cards WHERE item_type='phrase' AND item_id = ANY($1::int[])", created,
        )
        await db_pool.execute("DELETE FROM phrase_table WHERE phrase_id = ANY($1::int[])", created)


async def test_find_catalog_item_matches_phrase_canonical_not_surface_form(
    client: AsyncClient, db_pool, owned_phrase,
):
    """773 German rows have canonical != surface_form. Canonical is the identity."""
    uniq = _letters()
    canonical = f"jdm zzr{uniq} geben"
    surface_form = f"zzr{uniq} geben"
    phrase_id = await owned_phrase(canonical, surface_form)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    assert await find_catalog_item(db_pool, doc_id, canonical) == (phrase_id, "phrase")
    assert await find_catalog_item(db_pool, doc_id, surface_form) is None, \
        "surface_form must no longer bind — it disagreed with vocabulary lists"


async def test_find_catalog_item_matches_umlaut_phrase_canonical(
    client: AsyncClient, db_pool, owned_phrase,
):
    uniq = _letters()
    canonical = f"die Änderung zzr{uniq}"
    phrase_id = await owned_phrase(canonical, canonical)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    assert await find_catalog_item(db_pool, doc_id, canonical.lower()) == (phrase_id, "phrase")


async def test_reading_and_vocabulary_list_agree_on_the_phrase_row(
    client: AsyncClient, db_pool, owned_phrase,
):
    """The point of the shared resolver: one surface, one binding, either path."""
    uniq = _letters()
    canonical = f"sich Öffnen zzr{uniq} auf"
    phrase_id = await owned_phrase(canonical, f"Öffnen zzr{uniq} auf")
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    reading = await find_catalog_item(db_pool, doc_id, canonical.lower())
    listed = await catalog_resolver.resolve_one(db_pool, canonical.lower(), "de")

    assert reading == (phrase_id, "phrase")
    assert (listed.item_id, listed.item_type) == reading


async def test_umlaut_selection_propagates_to_main_srs(
    client: AsyncClient, db_pool, tracked_words,
):
    """End-to-end: the SRS credit that used to be silently lost.

    Saving a selection routes through `status_marked_learning`, which creates
    both SRS cards. Before the fix the umlaut canonical bound to nothing, so
    no card and no knowledge row appeared and nothing reported the failure.
    """
    surface = f"Özzr{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    resp = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(surface.lower()),
        headers=headers,
    )
    assert resp.status_code == 201

    uwk = await db_pool.fetchrow(
        "SELECT status FROM user_word_knowledge "
        " WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'",
        uid, word_id,
    )
    assert uwk is not None, "umlaut selection did not reach the catalog"
    assert uwk["status"] == "learning"
    assert await db_pool.fetchval(
        "SELECT count(*) FROM srs_cards "
        " WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'",
        uid, word_id,
    ) == 2


# ---------------------------------------------------------------------------
# get_word_statuses_for_page — status colours for umlaut words (2026-07-28)
#
# Two independent breakages, both from the C collation:
#   - the filter compared Python-lowered page tokens to SQL `LOWER(w.word)`,
#     which folds ASCII only, so an umlaut word never matched at all;
#   - the returned KEY was that same SQL-lowered value, so even a match would
#     have been filed under `'Öl'` while BookReaderPage.tsx looks it up as
#     `tok.text.toLowerCase()` → `'öl'`.
#
# Keys are plain `.lower()`, deliberately not `normalize_key`: the frontend has
# no `.normalize()` call, so an NFC key would be unlookupable for decomposed
# text. These tests pin the frontend contract, not just the match.
# ---------------------------------------------------------------------------


async def _page_with_text(db_pool, doc_id: str, text: str, page_number: int = 1) -> None:
    page_id = await db_pool.fetchval(
        "INSERT INTO book_pages (doc_id, page_number) VALUES ($1::uuid, $2) RETURNING page_id",
        doc_id, page_number,
    )
    await db_pool.execute(
        "INSERT INTO book_blocks (page_id, doc_id, block_index, block_type, clean_text) "
        "VALUES ($1, $2::uuid, 0, 'text', $3)",
        page_id, doc_id, text,
    )


async def _mark(db_pool, uid: str, word_id: int, status: str) -> None:
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'word', $3)",
        uid, word_id, status,
    )


@pytest.mark.parametrize("initial", ["Ö", "Ü", "Ä"])
async def test_page_word_statuses_include_umlaut_words(
    client: AsyncClient, db_pool, tracked_words, initial,
):
    surface = f"{initial}zzp{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")
    await _page_with_text(db_pool, doc_id, f"Hier steht {surface} im Text.")
    await _mark(db_pool, uid, word_id, "learning")

    statuses = await reading_service.get_word_statuses_for_page(
        db_pool, uid, doc_id, 1, "de",
    )

    assert statuses.get(surface.lower()) == "learning"


async def test_page_word_statuses_key_matches_js_tolowercase(
    client: AsyncClient, db_pool, tracked_words,
):
    """Frontend contract: `BookReaderPage.tsx` does `tok.text.toLowerCase()`.

    Python `.lower()` and JS `toLowerCase()` agree for `Ö` → `ö`. Keying with
    the stored spelling (or an NFC-normalised form) would be unlookupable.
    """
    surface = f"Özzp{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")
    await _page_with_text(db_pool, doc_id, surface)
    await _mark(db_pool, uid, word_id, "known")

    statuses = await reading_service.get_word_statuses_for_page(
        db_pool, uid, doc_id, 1, "de",
    )

    assert surface.lower() in statuses, "key must be the JS-lowercased token"
    assert surface not in statuses, "the stored spelling is not a usable key"


async def test_page_word_statuses_still_work_for_ascii(
    client: AsyncClient, db_pool, tracked_words,
):
    """Regression guard — the ASCII path was never broken."""
    surface = f"Zzp{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")
    await _page_with_text(db_pool, doc_id, f"ein {surface} hier")
    await _mark(db_pool, uid, word_id, "known")

    statuses = await reading_service.get_word_statuses_for_page(
        db_pool, uid, doc_id, 1, "de",
    )

    assert statuses.get(surface.lower()) == "known"


async def test_page_word_statuses_exclude_words_not_on_the_page(
    client: AsyncClient, db_pool, tracked_words,
):
    """The Python-side filter must not widen scope to the whole vocabulary."""
    on_page = f"Özzp{_letters()}"
    off_page = f"Üzzp{_letters()}"
    on_id = await _owned_word(db_pool, tracked_words, on_page)
    off_id = await _owned_word(db_pool, tracked_words, off_page)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")
    await _page_with_text(db_pool, doc_id, on_page)
    await _mark(db_pool, uid, on_id, "learning")
    await _mark(db_pool, uid, off_id, "learning")

    statuses = await reading_service.get_word_statuses_for_page(
        db_pool, uid, doc_id, 1, "de",
    )

    assert on_page.lower() in statuses
    assert off_page.lower() not in statuses


async def test_page_word_statuses_are_language_scoped(
    client: AsyncClient, db_pool, tracked_words,
):
    surface = f"Äzzp{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface, language="es")
    _headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")
    await _page_with_text(db_pool, doc_id, surface)
    await _mark(db_pool, uid, word_id, "learning")

    statuses = await reading_service.get_word_statuses_for_page(
        db_pool, uid, doc_id, 1, "de",
    )

    assert surface.lower() not in statuses


async def test_mastered_on_ambiguous_surface_leaves_the_catalog_untouched(
    client: AsyncClient, db_pool, tracked_words,
):
    """The sharpest edge of the ambiguity fail-safe — pinned deliberately.

    `mastered` maps to `status_marked_known` with `status_override="known"`
    (CLAUDE.md §8b), so this is a user *explicitly declaring mastery*. On a
    surface with several catalog rows, `find_catalog_item` now returns None and
    that declaration has no catalog effect at all — where before it advanced a
    coin-flip row to `known`, which is unrecoverable (auto-promotion is
    one-way). 410 German word surfaces are in this state today.

    The reading row itself still records `mastered`, so nothing the user did is
    lost on the reading schedule.
    """
    surface = f"Zzm{_letters()}"
    ids = [
        await _owned_word(db_pool, tracked_words, surface, pos="NOUN"),
        await _owned_word(db_pool, tracked_words, surface, pos="VERB"),
    ]
    headers, uid = await _register_and_login(client, db_pool, _email())
    doc_id = await _create_doc(db_pool, uid, "de")

    save = await client.post(
        f"/api/v1/books/{doc_id}/selections",
        json=_sel_body(surface), headers=headers,
    )
    assert save.status_code == 201
    selection_id = save.json()["selection_id"]

    review = await client.post(
        f"/api/v1/reading/selections/{selection_id}/review",
        json={"outcome": "mastered"}, headers=headers,
    )
    assert review.status_code == 200

    assert await db_pool.fetchval(
        "SELECT status FROM reading_selections WHERE selection_id = $1::uuid", selection_id,
    ) == "mastered", "the reading row must still record the user's action"

    assert await db_pool.fetchval(
        "SELECT count(*) FROM user_word_knowledge "
        " WHERE user_id = $1::uuid AND item_type = 'word' AND item_id = ANY($2::int[])",
        uid, ids,
    ) == 0, "an ambiguous surface must not promote either candidate to known"
