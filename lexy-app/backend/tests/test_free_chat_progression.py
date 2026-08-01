"""
Free chat → progression tests.

Source of truth: lexy-app/features/free-chat-progression/tests.md

Covers:
  Unit (no DB):
    - _compute_sentence_quality for five natural-language cases
  Unit (DB):
    - match_learning_words: surface match, lemma match, known excluded,
      empty text, numbers only, deduplication
  Integration (real DB, mocked LLM):
    - German message (de)            → free_chat_used_correctly (both tracks advance)
    - Mixed message                  → free_chat_mixed_lang     (passive only)
    - English label + target word    → free_chat_mixed_lang     (passive only)
                                       [mixed-language crediting fix, 2026-05-23 —
                                        a target-language word in an English-classified
                                        message still earns passive credit]
    - English label + no target word → no progression
    - free_chat_matched is NOT fired by the chat router (confirmed by event mapping)
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from backend.routers.chat import _compute_sentence_quality
from backend.services.chat_service import match_learning_words
from ._email_helper import make_test_email
from ._auth_helper import register_and_login

REGISTER = "/api/v1/auth/register"
LOGIN    = "/api/v1/auth/login"
SESSIONS = "/api/v1/chat/sessions"


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


async def _get_word(db_pool, language: str | None = None) -> tuple[int, str, str]:
    # Exclude test-fixture pollution: other suites (e.g. test_words.py's
    # "learn anyway" path) insert synthetic surfaces like 'ζtest_<hex>' /
    # 'Bnk_<hex>' into the un-user-scoped word_table and the autouse cleanup
    # (which only deletes test users) can't reap them. Those surfaces contain
    # digits/underscores, which real corpus words never do — and they can't
    # round-trip the message tokenizer, so a LIMIT-1 grab of one silently
    # breaks every match-based test. Pick a plain word and stay deterministic.
    #
    # `language` pins the pick to the language the CALLER matches against.
    # Without it this returned "the first plain word in the table" regardless
    # of language, which is only accidentally right: on the dev database
    # word_id 1 is German, so a caller matching against 'de' passed. On any
    # other database — a dedicated validation database whose only rows were
    # Spanish fixtures — it returned 'hola' and the match could never
    # succeed. Same failure class as the unordered catalog picks in
    # docs/TESTS.md; see docs/COMMON_ERRORS.md.
    if language is None:
        row = await db_pool.fetchrow(
            "SELECT word_id, word, language FROM word_table "
            "WHERE word !~ '[0-9_]' ORDER BY word_id LIMIT 1"
        )
    else:
        row = await db_pool.fetchrow(
            "SELECT word_id, word, language FROM word_table "
            "WHERE word !~ '[0-9_]' AND language = $1 ORDER BY word_id LIMIT 1",
            language,
        )
    if row is None:
        pytest.skip(
            "word_table has no plain word"
            + (f" in {language!r}" if language else "")
            + " — run the subtitle pipeline first"
        )
    return row["word_id"], row["word"], row["language"]


async def _mark_learning(db_pool, uid: str, word_id: int) -> None:
    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge
            (user_id, item_id, item_type, status, passive_level, active_level)
        VALUES ($1::uuid, $2, 'word', 'learning', 1, 0)
        ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'learning'
        """,
        uid, word_id,
    )


async def _get_knowledge(db_pool, uid: str, word_id: int) -> dict | None:
    row = await db_pool.fetchrow(
        """
        SELECT passive_level, active_level, times_used_correctly
          FROM user_word_knowledge
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
        """,
        uid, word_id,
    )
    return dict(row) if row else None


async def _create_free_session(client: AsyncClient, headers: dict) -> str:
    r = await client.post(SESSIONS, json={"session_type": "free"}, headers=headers)
    assert r.status_code == 201
    return r.json()["session_id"]


def _llm_result(language_detected: str) -> dict:
    return {
        "reply": "Sehr gut!",
        "language_detected": language_detected,
        "corrections": [],
        "word_matches": [],
    }


# ---------------------------------------------------------------------------
# Unit tests — _compute_sentence_quality
# ---------------------------------------------------------------------------

def test_quality_empty_list_is_needs_work():
    assert _compute_sentence_quality([]) == "needs_work"


def test_quality_all_high_is_excellent():
    assert _compute_sentence_quality(["high", "high", "high", "high", "high"]) == "excellent"


def test_quality_exactly_60_pct_high_is_excellent():
    # 3 out of 5 = 60 % → boundary: >= 0.6 → excellent
    assert _compute_sentence_quality(["high", "high", "high", "medium", "medium"]) == "excellent"


def test_quality_majority_low_is_needs_work():
    # 2 out of 3 lows = 67 % >= 50 %
    assert _compute_sentence_quality(["low", "low", "high"]) == "needs_work"


def test_quality_mixed_is_good():
    # 1 high (33 % < 60 %), 1 low (33 % < 50 %)
    assert _compute_sentence_quality(["high", "medium", "low"]) == "good"


# ---------------------------------------------------------------------------
# Unit tests — match_learning_words (real DB)
# ---------------------------------------------------------------------------

async def test_match_returns_learning_word_by_surface(client: AsyncClient, db_pool):
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    matches = await match_learning_words(db_pool, uid, word, language)

    assert any(m["item_id"] == word_id for m in matches)
    assert all(m["item_type"] == "word" for m in matches)


async def test_match_finds_word_by_lemma(client: AsyncClient, db_pool):
    """Tokens matching a word's lemma (not surface form) must also be returned."""
    row = await db_pool.fetchrow(
        """
        SELECT word_id, word, lemma, language FROM word_table
         WHERE lemma IS NOT NULL AND LOWER(lemma) != LOWER(word)
           AND word !~ '[0-9_]' AND lemma !~ '[0-9_]'
         LIMIT 1
        """
    )
    if row is None:
        pytest.skip("No word with lemma != surface form in word_table")

    word_id, _word, lemma, language = row["word_id"], row["word"], row["lemma"], row["language"]
    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    # Search using only the lemma form — surface form absent
    matches = await match_learning_words(db_pool, uid, lemma, language)

    assert any(m["item_id"] == word_id for m in matches)


async def test_match_excludes_known_words(client: AsyncClient, db_pool):
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())

    await db_pool.execute(
        """
        INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
        VALUES ($1::uuid, $2, 'word', 'known')
        ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'known'
        """,
        uid, word_id,
    )

    matches = await match_learning_words(db_pool, uid, word, language)

    assert not any(m["item_id"] == word_id for m in matches)


async def test_match_returns_empty_for_empty_text(client: AsyncClient, db_pool):
    headers, uid = await _register_and_login(client, db_pool, _email())
    assert await match_learning_words(db_pool, uid, "", "de") == []


async def test_match_returns_empty_for_numbers_only(client: AsyncClient, db_pool):
    headers, uid = await _register_and_login(client, db_pool, _email())
    assert await match_learning_words(db_pool, uid, "123 456 789", "de") == []


async def test_match_deduplicates_repeated_word(client: AsyncClient, db_pool):
    """Same word appearing multiple times in a message yields exactly one match entry."""
    word_id, word, language = await _get_word(db_pool)
    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    matches = await match_learning_words(db_pool, uid, f"{word} {word} {word}", language)

    word_matches = [m for m in matches if m["item_id"] == word_id]
    assert len(word_matches) == 1


# ---------------------------------------------------------------------------
# Phrase matching — match_learning_words delegates to matcher_service (#5b)
# ---------------------------------------------------------------------------

async def _get_phrase(db_pool, language: str = "de") -> tuple[int, str, str, str] | None:
    """Return (phrase_id, canonical, surface_form, language) for any seeded phrase.

    ORDER BY + a fixture exclusion, for the same reason `_get_word` above pins
    its language. `phrase_table` is not user-scoped, so the autouse cleanup
    (which reaps by test user) cannot remove `_testphrase_<hex>` rows other
    tests leave behind — and on a freshly built database those leaked rows are
    the FIRST ones physically, so an unordered LIMIT 1 picks a synthetic
    surface the spaCy matcher can never match.
    """
    row = await db_pool.fetchrow(
        "SELECT phrase_id, canonical, surface_form, language FROM phrase_table "
        "WHERE language = $1 AND canonical !~ '^_testphrase' "
        "ORDER BY phrase_id LIMIT 1",
        language,
    )
    return (row["phrase_id"], row["canonical"], row["surface_form"], row["language"]) if row else None


async def test_match_returns_learning_phrase_by_surface(client: AsyncClient, db_pool):
    """A phrase in 'learning' status whose surface form appears in the message
    must be returned with item_type='phrase'."""
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table is empty — seed the phrase pipeline first")
    phrase_id, canonical, surface_form, language = info

    headers, uid = await _register_and_login(client, db_pool, _email())
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'learning') "
        "ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'learning'",
        uid, phrase_id,
    )

    matches = await match_learning_words(db_pool, uid, surface_form, language)
    assert any(m["item_id"] == phrase_id and m["item_type"] == "phrase" for m in matches)


async def test_inflection_fixture_is_seeded(db_pool):
    """The data precondition for the strict-xfail test below, asserted on its
    own so a missing fixture reports as ITS OWN failure rather than being
    absorbed into that test's expected failure. Without this split, deleting
    the phrase would keep the suite green (a missing row makes the xfail test
    fail, which is what it is marked to do) and the extractor defect would
    stop being what is actually observed."""
    row = await db_pool.fetchrow(
        "SELECT phrase_id FROM phrase_table WHERE canonical = $1 AND language = 'de'",
        "sich freuen auf",
    )
    assert row is not None, (
        "phrase 'sich freuen auf' is not seeded — run scripts/seed_validation_db.py"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN EXTRACTOR DEFECT (app-level, tracked as rt-05): the phrase "
        "extractor never yields the canonical 'sich freuen auf'. Measured: "
        "match_sentence('ich freue mich auf die Reise', 'de') returns "
        "['ich', 'jdn. (Akk) freuen'], so the reflexive+preposition pattern "
        "is reduced to a bare accusative verb frame and the canonical this "
        "test names is unreachable. strict=True on purpose: the day the "
        "extractor is fixed this test PASSES and pytest reports XPASS as a "
        "failure, forcing the mark to be removed rather than quietly "
        "outliving the bug."
    ),
)
async def test_match_inflected_phrase_matches_canonical(client: AsyncClient, db_pool):
    """Inflected production (e.g. 'ich freue mich auf die Reise') must match the
    canonical 'sich freuen auf' via the spaCy-based phrase_finder."""
    row = await db_pool.fetchrow(
        "SELECT phrase_id FROM phrase_table WHERE canonical = $1 AND language = 'de'",
        "sich freuen auf",
    )
    # NOT a skip. A `pytest.skip` inside an `xfail(strict=True)` test reports
    # as skipped, so the expected failure would never be observed and the mark
    # would silently mean nothing. The fixture is seeded by
    # `scripts/seed_validation_db.py`; its absence is a setup error worth
    # failing on, and `test_inflection_fixture_is_seeded` below reports that
    # cause on its own, outside the xfail.
    assert row is not None, (
        "phrase 'sich freuen auf' is not seeded — run scripts/seed_validation_db.py"
    )
    phrase_id = row["phrase_id"]

    headers, uid = await _register_and_login(client, db_pool, _email())
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'learning') "
        "ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'learning'",
        uid, phrase_id,
    )

    matches = await match_learning_words(db_pool, uid, "Ich freue mich auf die Reise.", "de")
    assert any(m["item_id"] == phrase_id and m["item_type"] == "phrase" for m in matches), (
        "expected inflected 'freue mich auf' to map to canonical 'sich freuen auf'"
    )


async def test_match_excludes_known_phrases(client: AsyncClient, db_pool):
    """A phrase with status='known' must NOT appear in matches."""
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table empty")
    phrase_id, _, surface_form, language = info

    headers, uid = await _register_and_login(client, db_pool, _email())
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'known') "
        "ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'known'",
        uid, phrase_id,
    )

    matches = await match_learning_words(db_pool, uid, surface_form, language)
    assert not any(m["item_id"] == phrase_id and m["item_type"] == "phrase" for m in matches)


async def test_match_word_and_phrase_returned_together(client: AsyncClient, db_pool):
    """A message containing both a tracked word and a tracked phrase must return
    both kinds of match in one call."""
    info = await _get_phrase(db_pool)
    if info is None:
        pytest.skip("phrase_table empty")
    phrase_id, _, surface_form, language = info

    if language != "de":
        pytest.skip("test assumes de word + de phrase")
    # The word must be in the SAME language the match runs in — `language`
    # here comes from the phrase, and passing it is the whole fix.
    word_id, word, _ = await _get_word(db_pool, language)

    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'learning') "
        "ON CONFLICT (user_id, item_id, item_type) DO UPDATE SET status = 'learning'",
        uid, phrase_id,
    )

    matches = await match_learning_words(db_pool, uid, f"{word} {surface_form}", language)
    item_types = {m["item_type"] for m in matches}
    assert "word" in item_types
    assert "phrase" in item_types


# ---------------------------------------------------------------------------
# Integration tests — free chat HTTP endpoint (LLM mocked)
# ---------------------------------------------------------------------------

async def test_german_message_advances_both_tracks(client: AsyncClient, db_pool):
    """language_detected='de' → free_chat_used_correctly: passive_level AND active_level grow."""
    word_id, word, language = await _get_word(db_pool)
    if language != "de":
        pytest.skip("Need a German word for this test")

    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)
    session_id = await _create_free_session(client, headers)

    before = await _get_knowledge(db_pool, uid, word_id)
    before_passive = before["passive_level"] if before else 0
    before_active  = before["active_level"]  if before else 0

    with patch(
        "backend.routers.chat.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_llm_result("de"),
    ):
        resp = await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": word},
            headers=headers,
        )
    assert resp.status_code == 201

    after = await _get_knowledge(db_pool, uid, word_id)
    assert after["passive_level"] > before_passive, "passive_level should increase for German"
    assert after["active_level"]  > before_active,  "active_level should increase for German"


async def test_mixed_message_advances_passive_only(client: AsyncClient, db_pool):
    """language_detected='mixed' → free_chat_mixed_lang: passive grows, active unchanged."""
    word_id, word, language = await _get_word(db_pool)
    if language != "de":
        pytest.skip("Need a German word for this test")

    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)
    session_id = await _create_free_session(client, headers)

    before = await _get_knowledge(db_pool, uid, word_id)
    before_passive = before["passive_level"] if before else 0
    before_active  = before["active_level"]  if before else 0

    with patch(
        "backend.routers.chat.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_llm_result("mixed"),
    ):
        resp = await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": word},
            headers=headers,
        )
    assert resp.status_code == 201

    after = await _get_knowledge(db_pool, uid, word_id)
    assert after["passive_level"] > before_passive,         "passive_level should increase for mixed"
    assert after["active_level"]  == before_active, "active_level must NOT change for mixed"


async def test_english_label_with_target_word_advances_passive_only(client: AsyncClient, db_pool):
    """Mixed-language crediting fix (2026-05-23): a target-language learning
    word that appears in a message the LLM labels 'en' still earns PASSIVE
    credit (free_chat_mixed_lang) — passive_level grows, active does NOT, and
    no active SRS card is fabricated. Before the fix the 'en' label skipped
    matching entirely and this word got nothing."""
    word_id, word, language = await _get_word(db_pool)
    if language != "de":
        pytest.skip("Need a German word for this test")

    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)
    session_id = await _create_free_session(client, headers)

    before = await _get_knowledge(db_pool, uid, word_id)
    before_passive = before["passive_level"] if before else 0
    before_active  = before["active_level"]  if before else 0

    with patch(
        "backend.routers.chat.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_llm_result("en"),
    ):
        resp = await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": word},
            headers=headers,
        )
    assert resp.status_code == 201

    after = await _get_knowledge(db_pool, uid, word_id)
    assert after["passive_level"] > before_passive, (
        "passive_level should grow for an EN-labelled message containing a target word"
    )
    assert after["active_level"] == before_active, (
        "active_level must NOT change for an EN-labelled message"
    )

    # "active SRS does not advance": _mark_learning inserts uwk directly (no
    # apply_progression), so no active card exists, and free_chat_mixed_lang
    # has no active_srs action — confirm none was fabricated.
    active_card = await db_pool.fetchval(
        """
        SELECT 1 FROM srs_cards
         WHERE user_id = $1::uuid AND item_id = $2 AND item_type = 'word'
           AND direction = 'active'
        """,
        uid, word_id,
    )
    assert active_card is None, "no active SRS card should be created for a mixed-credit turn"


async def test_english_message_without_target_word_triggers_no_progression(client: AsyncClient, db_pool):
    """language_detected='en' AND the message contains no target-language
    learning word → no progression at all. Complement of the case above: the
    always-on matcher finds nothing, so no event fires."""
    word_id, word, language = await _get_word(db_pool)
    if language != "de":
        pytest.skip("Need a German word for this test")

    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)
    session_id = await _create_free_session(client, headers)

    before = await _get_knowledge(db_pool, uid, word_id)

    # Gibberish content — deterministically contains none of the user's
    # learning words regardless of what _get_word returned.
    with patch(
        "backend.routers.chat.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_llm_result("en"),
    ):
        resp = await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": "qwerty asdfgh zxcvbn"},
            headers=headers,
        )
    assert resp.status_code == 201

    after = await _get_knowledge(db_pool, uid, word_id)
    if after and before:
        assert after["passive_level"] == before["passive_level"], "No progression when no target word present"
        assert after["active_level"]  == before["active_level"],  "No progression when no target word present"


# ---------------------------------------------------------------------------
# Unicode matching (C-collation bug, fixed 2026-07-28)
#
# `match_learning_words` pushed case-folding into SQL — `LOWER(wt.word) =
# ANY($2)` fed Python-lowered tokens. This database is `datcollate=C`, so
# Postgres folds ASCII only: `LOWER('Öl')` is `'Öl'`, never `'öl'`. Producing
# an umlaut word in free chat therefore matched nothing and earned **no
# progression or SRS credit** — silently, with no error anywhere.
#
# Fixture surfaces are letters-only on purpose: the message tokenizer is
# `[^\W\d_]+`, so a digit or underscore in the surface makes it untokenizable
# and every assertion below would pass vacuously against a word that can never
# match. That is the same trap documented on `_get_word` above.
# ---------------------------------------------------------------------------


def _letters(n: int = 12) -> str:
    """A unique letters-only suffix (uuid hex would smuggle in digits)."""
    return "".join(chr(ord("a") + int(c, 16)) for c in uuid.uuid4().hex[:n])


async def _owned_word(db_pool, tracked: list[int], surface: str, *,
                      lemma: str | None = None, language: str = "de") -> int:
    wid = await db_pool.fetchval(
        "INSERT INTO word_table (word, language, pos, tag, lemma) "
        "VALUES ($1, $2, '', '', $3) RETURNING word_id",
        surface, language, lemma or surface,
    )
    tracked.append(wid)
    return wid


@pytest.mark.parametrize("initial", ["Ö", "Ü", "Ä"])
async def test_match_finds_tracked_umlaut_word_from_lowercase_message(
    client: AsyncClient, db_pool, tracked_words, initial,
):
    """Tracking `Öl`, writing `öl` — the case that earned no credit."""
    surface = f"{initial}zzc{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    matches = await match_learning_words(db_pool, uid, f"ich mag {surface.lower()}", "de")

    assert any(m["item_id"] == word_id for m in matches), \
        f"tracked {surface!r} did not match produced {surface.lower()!r}"


async def test_match_finds_tracked_umlaut_word_from_exact_spelling(
    client: AsyncClient, db_pool, tracked_words,
):
    surface = f"Özzc{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    matches = await match_learning_words(db_pool, uid, f"das {surface} hier", "de")

    assert any(m["item_id"] == word_id for m in matches)


async def test_match_finds_tracked_umlaut_word_from_uppercase_message(
    client: AsyncClient, db_pool, tracked_words,
):
    surface = f"Üzzc{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    matches = await match_learning_words(db_pool, uid, surface.upper(), "de")

    assert any(m["item_id"] == word_id for m in matches)


async def test_match_finds_umlaut_word_by_lemma_case_variant(
    client: AsyncClient, db_pool, tracked_words,
):
    """The lemma half of the predicate needs the same fold as the surface."""
    lemma = f"Äzzc{_letters()}"
    surface = f"Bzzc{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface, lemma=lemma)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    matches = await match_learning_words(db_pool, uid, lemma.lower(), "de")

    assert any(m["item_id"] == word_id for m in matches)


async def test_match_ascii_word_still_works(client: AsyncClient, db_pool, tracked_words):
    """Regression guard — the ASCII path was never broken and must stay."""
    surface = f"Zzc{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    matches = await match_learning_words(db_pool, uid, surface.lower(), "de")

    assert any(m["item_id"] == word_id for m in matches)


@pytest.mark.parametrize("sharp_s,double_s", [("straße", "strasse"), ("schließen", "schliessen")])
async def test_match_does_not_merge_sharp_s_with_double_s(
    client: AsyncClient, db_pool, tracked_words, sharp_s, double_s,
):
    """`casefold()` maps ß to ss and would grant credit for the wrong word.

    These are distinct German entries, so `normalize_key` uses `lower()`.
    Tracking the ß spelling and writing the ss spelling must NOT match.
    """
    uniq = _letters()
    sharp_id = await _owned_word(db_pool, tracked_words, f"{sharp_s}{uniq}")
    _headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, sharp_id)

    matches = await match_learning_words(db_pool, uid, f"{double_s}{uniq}", "de")

    assert not any(m["item_id"] == sharp_id for m in matches), \
        "ss spelling must not earn credit for the ß word"


async def test_match_excludes_known_umlaut_words(client: AsyncClient, db_pool, tracked_words):
    """Mastered items stay excluded — the fix must not widen the status gate."""
    surface = f"Özzc{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'word', 'known')",
        uid, word_id,
    )

    matches = await match_learning_words(db_pool, uid, surface.lower(), "de")

    assert not any(m["item_id"] == word_id for m in matches)


async def test_match_umlaut_word_is_language_scoped(
    client: AsyncClient, db_pool, tracked_words,
):
    surface = f"Üzzc{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface, language="es")
    _headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    matches = await match_learning_words(db_pool, uid, surface.lower(), "de")

    assert not any(m["item_id"] == word_id for m in matches)


async def test_match_does_not_return_untracked_umlaut_words(
    client: AsyncClient, db_pool, tracked_words,
):
    """Only items the user tracks — the inverted join must not widen scope."""
    surface = f"Äzzc{_letters()}"
    await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())

    matches = await match_learning_words(db_pool, uid, surface.lower(), "de")

    assert matches == []


async def test_match_collapses_mixed_case_spellings_of_one_umlaut_word(
    client: AsyncClient, db_pool, tracked_words,
):
    """Three *different spellings* of one word yield one match, not three.

    Plain dedup of a repeated token is already covered by
    `test_match_deduplicates_repeated_word` (ASCII, unaffected by the fold).
    What this adds is that case variants collapse to the same item — the fold
    happens before dedup, so `öl öl Öl` cannot be credited twice.
    """
    surface = f"Özzc{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    _headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)

    low = surface.lower()
    matches = await match_learning_words(db_pool, uid, f"{low} {low} {surface}", "de")

    assert len([m for m in matches if m["item_id"] == word_id]) == 1


async def test_umlaut_word_in_german_message_advances_both_tracks(
    client: AsyncClient, db_pool, tracked_words,
):
    """End-to-end: the credit that was silently lost is now granted.

    Mirrors `test_german_message_advances_both_tracks` with an umlaut surface
    written in lower case — before the fix neither level moved and nothing
    surfaced the failure.
    """
    surface = f"Özzc{_letters()}"
    word_id = await _owned_word(db_pool, tracked_words, surface)
    headers, uid = await _register_and_login(client, db_pool, _email())
    await _mark_learning(db_pool, uid, word_id)
    session_id = await _create_free_session(client, headers)

    before = await _get_knowledge(db_pool, uid, word_id)

    with patch(
        "backend.routers.chat.llm_service.evaluate_and_reply",
        new_callable=AsyncMock,
        return_value=_llm_result("de"),
    ):
        resp = await client.post(
            f"{SESSIONS}/{session_id}/messages",
            json={"content": surface.lower()},
            headers=headers,
        )
    assert resp.status_code == 201

    after = await _get_knowledge(db_pool, uid, word_id)
    assert after["passive_level"] > before["passive_level"], "passive credit lost"
    assert after["active_level"] > before["active_level"], "active credit lost"
