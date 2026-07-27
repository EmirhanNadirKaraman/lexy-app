"""
Recommendation engine tests.

Unit tests (no DB) — test pure functions:
  score_sentence:
    - higher due_count increases score
    - priority_count adds to score
    - exact target_unknown beats farther unknown_count
    - zero signals produce zero score
  rank_sentences:
    - due items appear before non-due items
    - limit is respected
    - empty candidates → empty result
    - priority_ids empty → priority_count = 0 for all
    - unknown_word_ids absent → priority_count = 0 (no KeyError)
  score_video:
    - higher priority_score increases score
    - shorter duration tiebreaks equal priority_score
    - zero inputs → zero score
  rank_videos:
    - video with highest priority_score ranked first
    - limit is respected
    - empty coverage → empty result
    - covered_item_ids are sorted

Integration tests (real DB):
  - recommend_sentences returns valid shape for a new user
  - all returned sentences have unknown_count in [min_unknown, max_unknown]
  - recommend_sentences respects limit
  - recommend_videos returns no_target_items for a new user
  - recommend_videos returns valid shape when user has learning words
  - target_item_count equals len(prioritized items)

HTTP tests (FastAPI client):
  - GET /api/v1/recommendations/sentences returns 200
  - GET /api/v1/recommendations/sentences requires auth → 403
  - GET /api/v1/recommendations/sentences response shape is correct
  - GET /api/v1/recommendations/videos returns 200
  - GET /api/v1/recommendations/videos requires auth → 403
  - GET /api/v1/recommendations/videos response shape is correct
"""

import pytest

from backend.services.recommendation_service import (
    enrich_items,
    rank_sentences,
    rank_videos,
    recommend_items,
    recommend_sentences,
    recommend_videos,
    score_sentence,
    score_video,
)
from ._email_helper import cleanup_pattern, make_test_email


# ---------------------------------------------------------------------------
# Unit tests — pure functions, no DB
# ---------------------------------------------------------------------------

class TestScoreSentence:
    def test_higher_due_count_increases_score(self):
        base = score_sentence(unknown_count=2, due_count=0, priority_count=0, target_unknown=2)
        with_due = score_sentence(unknown_count=2, due_count=1, priority_count=0, target_unknown=2)
        assert with_due > base

    def test_priority_count_adds_to_score(self):
        base = score_sentence(unknown_count=2, due_count=0, priority_count=0, target_unknown=2)
        with_priority = score_sentence(unknown_count=2, due_count=0, priority_count=1, target_unknown=2)
        assert with_priority > base

    def test_exact_target_unknown_beats_farther(self):
        exact = score_sentence(unknown_count=2, due_count=0, priority_count=0, target_unknown=2)
        farther = score_sentence(unknown_count=4, due_count=0, priority_count=0, target_unknown=2)
        assert exact > farther

    def test_zero_signals_produce_zero_score(self):
        assert score_sentence(unknown_count=0, due_count=0, priority_count=0, target_unknown=0) == 0.0

    def test_due_weight_dominates_priority(self):
        # 1 due item (weight 30) > 3 priority items (weight 10 each = 30) only when due_count=1 vs priority_count=3
        # Actually 30 == 30, so let's use due_count=2 vs priority_count=3
        mostly_due = score_sentence(unknown_count=2, due_count=2, priority_count=0, target_unknown=2)
        mostly_priority = score_sentence(unknown_count=2, due_count=0, priority_count=3, target_unknown=2)
        assert mostly_due > mostly_priority  # 60 > 30


class TestRankSentences:
    def _make_candidate(self, sentence_id, unknown_count, due_count, unknown_word_ids=None):
        return {
            "sentence_id": sentence_id,
            "content": f"sentence {sentence_id}",
            "start_time": 0.0,
            "start_time_int": 0,
            "video_id": "vid_A",
            "video_title": "Video A",
            "thumbnail_url": "",
            "language": "de",
            "duration": 300.0,
            "unknown_count": unknown_count,
            "due_count": due_count,
            "unknown_word_ids": unknown_word_ids or [],
        }

    def test_due_items_ranked_first(self):
        candidates = [
            self._make_candidate(1, unknown_count=2, due_count=0),
            self._make_candidate(2, unknown_count=2, due_count=1),
        ]
        ranked = rank_sentences(candidates, target_unknown=2, priority_ids=set(), limit=10)
        assert ranked[0]["sentence_id"] == 2

    def test_limit_respected(self):
        candidates = [self._make_candidate(i, unknown_count=2, due_count=0) for i in range(20)]
        ranked = rank_sentences(candidates, target_unknown=2, priority_ids=set(), limit=5)
        assert len(ranked) == 5

    def test_empty_candidates_returns_empty(self):
        assert rank_sentences([], target_unknown=2, priority_ids=set(), limit=10) == []

    def test_empty_priority_ids_sets_priority_count_zero(self):
        candidates = [self._make_candidate(1, unknown_count=2, due_count=0, unknown_word_ids=[10, 20])]
        ranked = rank_sentences(candidates, target_unknown=2, priority_ids=set(), limit=10)
        assert ranked[0]["priority_count"] == 0

    def test_priority_ids_counted_correctly(self):
        candidates = [self._make_candidate(1, unknown_count=3, due_count=0, unknown_word_ids=[10, 20, 30])]
        ranked = rank_sentences(candidates, target_unknown=3, priority_ids={10, 30}, limit=10)
        assert ranked[0]["priority_count"] == 2

    def test_missing_unknown_word_ids_key_is_safe(self):
        candidate = {
            "sentence_id": 1,
            "content": "test",
            "start_time": 0.0,
            "start_time_int": 0,
            "video_id": "v",
            "video_title": "V",
            "thumbnail_url": "",
            "language": "de",
            "duration": 100.0,
            "unknown_count": 2,
            "due_count": 0,
            # unknown_word_ids intentionally absent
        }
        ranked = rank_sentences([candidate], target_unknown=2, priority_ids={1, 2}, limit=10)
        assert ranked[0]["priority_count"] == 0

    def test_score_field_present(self):
        candidates = [self._make_candidate(1, unknown_count=2, due_count=1)]
        ranked = rank_sentences(candidates, target_unknown=2, priority_ids=set(), limit=10)
        assert "score" in ranked[0]
        assert isinstance(ranked[0]["score"], (int, float))


class TestScoreVideo:
    def test_higher_priority_score_increases_score(self):
        high = score_video(priority_score=10.0, duration=300.0)
        low  = score_video(priority_score=2.0,  duration=300.0)
        assert high > low

    def test_shorter_duration_tiebreaks_equal_priority(self):
        short = score_video(priority_score=5.0, duration=100.0)
        long_ = score_video(priority_score=5.0, duration=1000.0)
        assert short > long_

    def test_zero_inputs_returns_zero(self):
        assert score_video(priority_score=0.0, duration=0.0) == 0.0


def _make_meta(video_id: str, duration: float = 300.0) -> dict:
    return {
        "title": video_id, "thumbnail_url": "", "language": "de",
        "duration": duration, "best_start_time": 0.0,
        "best_content": "", "best_sentence_id": 1,
    }


class TestRankVideos:
    def test_higher_priority_video_ranked_first(self):
        # vid_high covers items with scores 4.0 each; vid_low covers items 1.0 each
        coverage = {"vid_high": {1, 2}, "vid_low": {3, 4}}
        meta = {"vid_high": _make_meta("vid_high"), "vid_low": _make_meta("vid_low")}
        score_by_id = {1: 4.0, 2: 4.0, 3: 1.0, 4: 1.0}
        ranked = rank_videos(coverage, meta, score_by_id, limit=10)
        assert ranked[0]["video_id"] == "vid_high"

    def test_limit_respected(self):
        coverage = {f"vid_{i}": {i} for i in range(10)}
        meta = {f"vid_{i}": _make_meta(f"vid_{i}") for i in range(10)}
        score_by_id = {i: float(i + 1) for i in range(10)}
        ranked = rank_videos(coverage, meta, score_by_id, limit=3)
        assert len(ranked) == 3

    def test_empty_coverage_returns_empty(self):
        ranked = rank_videos({}, {}, score_by_id={1: 4.0, 2: 1.0}, limit=10)
        assert ranked == []

    def test_covered_item_ids_are_sorted(self):
        coverage = {"vid_A": {5, 1, 3}}
        meta = {"vid_A": _make_meta("vid_A")}
        ranked = rank_videos(coverage, meta, score_by_id={1: 4.0, 3: 4.0, 5: 4.0}, limit=10)
        assert ranked[0]["covered_item_ids"] == [1, 3, 5]

    def test_priority_score_field_present(self):
        coverage = {"vid_A": {1}}
        meta = {"vid_A": _make_meta("vid_A")}
        ranked = rank_videos(coverage, meta, score_by_id={1: 3.5}, limit=10)
        assert "priority_score" in ranked[0]
        assert ranked[0]["priority_score"] == pytest.approx(3.5)

    def test_score_field_present(self):
        coverage = {"vid_A": {1}}
        meta = {"vid_A": _make_meta("vid_A")}
        ranked = rank_videos(coverage, meta, score_by_id={1: 4.0}, limit=10)
        assert "score" in ranked[0]

    def test_item_not_in_score_by_id_contributes_zero(self):
        # vid_A covers item 1 (score 4.0) and item 99 (not in score_by_id)
        coverage = {"vid_A": {1, 99}}
        meta = {"vid_A": _make_meta("vid_A")}
        ranked = rank_videos(coverage, meta, score_by_id={1: 4.0}, limit=10)
        # covered_item_ids only includes items that are targets
        assert ranked[0]["covered_item_ids"] == [1]
        assert ranked[0]["covered_count"] == 1


# ---------------------------------------------------------------------------
# Integration tests — real DB
# ---------------------------------------------------------------------------

async def _any_language(pool) -> str:
    """Return any language present in the video table."""
    row = await pool.fetchrow("SELECT language FROM video LIMIT 1")
    if row is None:
        pytest.skip("No videos in DB — run the subtitle pipeline first")
    return row["language"]


async def _create_user(pool) -> str:
    """Insert a fresh test user and return its user_id string."""
    row = await pool.fetchrow(
        """
        INSERT INTO users (email, password_hash)
        VALUES ($1, 'x')
        RETURNING user_id
        """,
        make_test_email(),
    )
    return str(row["user_id"])


async def test_recommend_sentences_new_user_valid_shape(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    result = await recommend_sentences(
        db_pool, user_id=user_id, language=language,
        limit=5, target_unknown=2, min_unknown=0, max_unknown=10,
    )

    assert "sentences" in result
    assert "target_unknown" in result
    assert "total" in result
    assert isinstance(result["sentences"], list)
    assert result["target_unknown"] == 2
    assert result["total"] == len(result["sentences"])


async def test_recommend_sentences_unknown_count_within_range(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    result = await recommend_sentences(
        db_pool, user_id=user_id, language=language,
        limit=20, target_unknown=2, min_unknown=1, max_unknown=3,
    )

    for s in result["sentences"]:
        assert 1 <= s["unknown_count"] <= 3, (
            f"sentence {s['sentence_id']} has unknown_count={s['unknown_count']}, "
            f"expected between 1 and 3"
        )


async def test_recommend_sentences_respects_limit(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    result = await recommend_sentences(
        db_pool, user_id=user_id, language=language,
        limit=3, target_unknown=2, min_unknown=0, max_unknown=15,
    )

    assert len(result["sentences"]) <= 3


async def test_recommend_sentences_each_item_has_required_fields(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    result = await recommend_sentences(
        db_pool, user_id=user_id, language=language,
        limit=5, target_unknown=2, min_unknown=0, max_unknown=10,
    )

    for s in result["sentences"]:
        for field in ("sentence_id", "content", "video_id", "unknown_count",
                      "due_count", "priority_count", "score"):
            assert field in s, f"missing field {field!r} in sentence result"


async def test_recommend_videos_no_target_items_for_new_user(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    result = await recommend_videos(db_pool, user_id=user_id, language=language, limit=5)

    assert result["videos"] == []
    assert result["target_item_count"] == 0
    assert result["reason"] == "no_target_items"


async def test_recommend_videos_valid_shape_with_learning_words(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    # Give the user some learning words (word_ids that actually exist)
    word_rows = await db_pool.fetch(
        """
        SELECT DISTINCT wts.word_id
        FROM word_to_sentence wts
        JOIN sentence s ON s.sentence_id = wts.sentence_id
        JOIN video v    ON v.video_id = s.video_id
        WHERE v.language = $1
        LIMIT 5
        """,
        language,
    )
    if not word_rows:
        pytest.skip("No word_to_sentence data for this language")

    for row in word_rows:
        await db_pool.execute(
            """
            INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
            VALUES ($1::uuid, $2, 'word', 'learning')
            ON CONFLICT DO NOTHING
            """,
            user_id, row["word_id"],
        )

    result = await recommend_videos(db_pool, user_id=user_id, language=language, limit=5)

    assert isinstance(result["videos"], list)
    assert result["target_item_count"] >= 1
    for v in result["videos"]:
        assert v["covered_count"] >= 1
        assert "priority_score" in v
        assert "score" in v


async def test_recommend_videos_target_item_count_matches_db(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    word_rows = await db_pool.fetch(
        """
        SELECT DISTINCT wts.word_id
        FROM word_to_sentence wts
        JOIN sentence s ON s.sentence_id = wts.sentence_id
        JOIN video v    ON v.video_id = s.video_id
        WHERE v.language = $1
        LIMIT 4
        """,
        language,
    )
    if not word_rows:
        pytest.skip("No word_to_sentence data for this language")

    word_ids = [r["word_id"] for r in word_rows]
    for wid in word_ids:
        await db_pool.execute(
            """
            INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
            VALUES ($1::uuid, $2, 'word', 'learning')
            ON CONFLICT DO NOTHING
            """,
            user_id, wid,
        )

    result = await recommend_videos(db_pool, user_id=user_id, language=language, limit=10)
    assert result["target_item_count"] == len(word_ids)


# ---------------------------------------------------------------------------
# HTTP tests — FastAPI client
# ---------------------------------------------------------------------------

async def _auth_token(client) -> str:
    email = make_test_email()
    await client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    return resp.json()["access_token"]


async def _language_param(db_pool) -> str:
    row = await db_pool.fetchrow("SELECT language FROM video LIMIT 1")
    if row is None:
        pytest.skip("No videos in DB")
    return row["language"]


async def test_sentences_endpoint_returns_200(client, db_pool):
    token = await _auth_token(client)
    language = await _language_param(db_pool)

    resp = await client.get(
        "/api/v1/recommendations/sentences",
        params={"language": language},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


async def test_sentences_endpoint_requires_auth(client, db_pool):
    language = await _language_param(db_pool)
    resp = await client.get(
        "/api/v1/recommendations/sentences",
        params={"language": language},
    )
    assert resp.status_code == 403


async def test_sentences_response_shape(client, db_pool):
    token = await _auth_token(client)
    language = await _language_param(db_pool)

    resp = await client.get(
        "/api/v1/recommendations/sentences",
        params={"language": language, "limit": 5, "target_unknown": 2},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()

    assert "sentences" in body
    assert "target_unknown" in body
    assert "total" in body
    assert body["target_unknown"] == 2
    assert isinstance(body["sentences"], list)

    for s in body["sentences"]:
        for field in ("sentence_id", "content", "video_id", "video_title",
                      "start_time", "unknown_count", "due_count",
                      "priority_count", "score"):
            assert field in s, f"missing field {field!r}"


async def test_videos_endpoint_returns_200(client, db_pool):
    token = await _auth_token(client)
    language = await _language_param(db_pool)

    resp = await client.get(
        "/api/v1/recommendations/videos",
        params={"language": language},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


async def test_videos_endpoint_requires_auth(client, db_pool):
    language = await _language_param(db_pool)
    resp = await client.get(
        "/api/v1/recommendations/videos",
        params={"language": language},
    )
    assert resp.status_code == 403


async def test_videos_response_shape(client, db_pool):
    token = await _auth_token(client)
    language = await _language_param(db_pool)

    resp = await client.get(
        "/api/v1/recommendations/videos",
        params={"language": language, "limit": 3},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()

    assert "videos" in body
    assert "target_item_count" in body
    assert "reason" in body

    for v in body["videos"]:
        for field in ("video_id", "title", "thumbnail_url", "language",
                      "duration", "start_time", "priority_score",
                      "covered_item_ids", "covered_count", "score"):
            assert field in v, f"missing field {field!r}"


async def test_videos_new_user_returns_no_target_items(client, db_pool):
    token = await _auth_token(client)
    language = await _language_param(db_pool)

    resp = await client.get(
        "/api/v1/recommendations/videos",
        params={"language": language},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()

    assert resp.status_code == 200
    assert body["videos"] == []
    assert body["reason"] == "no_target_items"
    assert body["target_item_count"] == 0


# ---------------------------------------------------------------------------
# Item recommendation — integration tests
# ---------------------------------------------------------------------------

async def test_recommend_items_new_user_returns_empty(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    result = await recommend_items(
        db_pool, user_id=user_id, language=language, item_type="word", limit=10,
    )

    assert result["items"] == []
    assert result["item_type"] == "word"
    assert result["language"] == language
    assert result["total"] == 0


async def test_recommend_items_unsupported_type_returns_empty(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    for item_type in ("phrase", "grammar_rule"):
        result = await recommend_items(
            db_pool, user_id=user_id, language=language, item_type=item_type, limit=10,
        )
        assert result["items"] == [], f"expected empty for item_type={item_type!r}"
        assert result["total"] == 0


async def test_recommend_items_with_learning_words(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    word_rows = await db_pool.fetch(
        """
        SELECT DISTINCT wts.word_id
        FROM word_to_sentence wts
        JOIN sentence s ON s.sentence_id = wts.sentence_id
        JOIN video v    ON v.video_id = s.video_id
        WHERE v.language = $1
        LIMIT 3
        """,
        language,
    )
    if not word_rows:
        pytest.skip("No word_to_sentence data for this language")

    for row in word_rows:
        await db_pool.execute(
            """
            INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
            VALUES ($1::uuid, $2, 'word', 'learning')
            ON CONFLICT DO NOTHING
            """,
            user_id, row["word_id"],
        )

    result = await recommend_items(
        db_pool, user_id=user_id, language=language, item_type="word", limit=10,
    )

    assert isinstance(result["items"], list)
    assert result["total"] == len(result["items"])
    for item in result["items"]:
        assert "item_id" in item
        assert "display_text" in item
        assert "signals" in item
        assert "reasons" in item
        assert item["item_type"] == "word"
        assert item["score"] >= 0


async def test_recommend_items_each_item_has_required_fields(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    word_rows = await db_pool.fetch(
        """
        SELECT DISTINCT wts.word_id
        FROM word_to_sentence wts
        JOIN sentence s ON s.sentence_id = wts.sentence_id
        JOIN video v    ON v.video_id = s.video_id
        WHERE v.language = $1
        LIMIT 5
        """,
        language,
    )
    if not word_rows:
        pytest.skip("No word_to_sentence data for this language")

    for row in word_rows:
        await db_pool.execute(
            """
            INSERT INTO user_word_knowledge (user_id, item_id, item_type, status)
            VALUES ($1::uuid, $2, 'word', 'learning')
            ON CONFLICT DO NOTHING
            """,
            user_id, row["word_id"],
        )

    result = await recommend_items(
        db_pool, user_id=user_id, language=language, item_type="word", limit=10,
    )

    for item in result["items"]:
        for field in ("item_id", "item_type", "score", "display_text",
                      "current_status", "passive_level", "active_level",
                      "signals", "reasons"):
            assert field in item, f"missing field {field!r} in item result"
        for signal_key in ("is_due", "mistake_recency", "freq_rank", "is_learning"):
            assert signal_key in item["signals"], f"missing signal {signal_key!r}"


async def test_enrich_items_empty_ids_returns_empty(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    result = await enrich_items(db_pool, user_id=user_id, item_ids=[], language=language)
    assert result == {}


async def test_enrich_items_unknown_ids_returns_empty(db_pool):
    language = await _any_language(db_pool)
    user_id = await _create_user(db_pool)

    result = await enrich_items(
        db_pool, user_id=user_id, item_ids=[999_999_999], language=language,
    )
    assert result == {}


# ---------------------------------------------------------------------------
# Item recommendation — HTTP tests
# ---------------------------------------------------------------------------

async def test_items_endpoint_returns_200(client, db_pool):
    token = await _auth_token(client)
    language = await _language_param(db_pool)

    resp = await client.get(
        "/api/v1/recommendations/items",
        params={"language": language},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


async def test_items_endpoint_requires_auth(client, db_pool):
    language = await _language_param(db_pool)
    resp = await client.get(
        "/api/v1/recommendations/items",
        params={"language": language},
    )
    assert resp.status_code == 403


async def test_items_endpoint_rejects_invalid_item_type(client, db_pool):
    token = await _auth_token(client)
    language = await _language_param(db_pool)

    resp = await client.get(
        "/api/v1/recommendations/items",
        params={"language": language, "item_type": "nonsense"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_items_response_shape(client, db_pool):
    token = await _auth_token(client)
    language = await _language_param(db_pool)

    resp = await client.get(
        "/api/v1/recommendations/items",
        params={"language": language, "limit": 5},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()

    assert resp.status_code == 200
    assert "items" in body
    assert "item_type" in body
    assert "language" in body
    assert "total" in body
    assert body["item_type"] == "word"
    assert body["language"] == language
    assert isinstance(body["items"], list)
    assert body["total"] == len(body["items"])


async def test_items_new_user_returns_empty_list(client, db_pool):
    token = await _auth_token(client)
    language = await _language_param(db_pool)

    resp = await client.get(
        "/api/v1/recommendations/items",
        params={"language": language},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()

    assert resp.status_code == 200
    assert body["items"] == []
    assert body["total"] == 0


async def test_items_phrase_type_returns_empty(client, db_pool):
    token = await _auth_token(client)
    language = await _language_param(db_pool)

    resp = await client.get(
        "/api/v1/recommendations/items",
        params={"language": language, "item_type": "phrase"},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()

    assert resp.status_code == 200
    assert body["items"] == []
    assert body["item_type"] == "phrase"


# ---------------------------------------------------------------------------
# Unified enrichment dispatcher (#5c)
# ---------------------------------------------------------------------------

async def _make_user(db_pool) -> str:
    from backend.services.auth_service import register_user
    email = make_test_email()
    user = await register_user(db_pool, email, "password123")
    return str(user["user_id"])


async def test_enrich_by_type_buckets_words_and_phrases(db_pool, make_word):
    """Mixed enrichment fans out to the per-type enrichers and returns a
    composite-keyed dict."""
    from backend.services.recommendation_service import enrich_by_type

    # Phrase still comes from the shared catalog (phrase_table is out of scope for
    # the word_table isolation pass); the word is owned + reaped (conftest
    # make_word) and created in the phrase's language so the two match.
    phrase_row = await db_pool.fetchrow(
        "SELECT phrase_id, surface_form, language FROM phrase_table LIMIT 1"
    )
    if phrase_row is None:
        pytest.skip("phrase_table empty")

    language = phrase_row["language"]
    phrase_id = phrase_row["phrase_id"]
    word_id, word_surface = await make_word(language)

    uid = await _make_user(db_pool)

    enrichment = await enrich_by_type(
        db_pool, uid,
        [("word", word_id), ("phrase", phrase_id)],
        language,
    )
    assert ("word", word_id) in enrichment
    assert ("phrase", phrase_id) in enrichment
    assert enrichment[("word", word_id)]["display_text"]   == word_surface
    assert enrichment[("phrase", phrase_id)]["display_text"] == phrase_row["surface_form"]


async def test_enrich_by_type_omits_unknown_ids(db_pool):
    """IDs that don't exist in the relevant table are silently dropped."""
    from backend.services.recommendation_service import enrich_by_type

    uid = await _make_user(db_pool)
    enrichment = await enrich_by_type(
        db_pool, uid,
        [("word", 999_999_999), ("phrase", 999_999_999), ("grammar_rule", 999_999_999)],
        "de",
    )
    assert enrichment == {}


async def test_enrich_by_type_no_collision_on_shared_int_id(db_pool):
    """Composite key (item_type, item_id) keeps word_id=N and phrase_id=N
    enrichments distinct, even if both happen to be the same integer."""
    from backend.services.recommendation_service import enrich_by_type

    # Find a word_id and a phrase_id that share the same integer value.
    row = await db_pool.fetchrow(
        """
        SELECT wt.word_id AS shared_id, wt.word, pt.surface_form, wt.language
          FROM word_table wt
          JOIN phrase_table pt ON pt.phrase_id = wt.word_id
         WHERE wt.language = pt.language
         LIMIT 1
        """
    )
    if row is None:
        pytest.skip("no shared id between word_table and phrase_table — collision-free corpus")

    shared_id = row["shared_id"]
    language  = row["language"]
    uid = await _make_user(db_pool)

    enrichment = await enrich_by_type(
        db_pool, uid,
        [("word", shared_id), ("phrase", shared_id)],
        language,
    )

    assert ("word",   shared_id) in enrichment
    assert ("phrase", shared_id) in enrichment
    # Distinct display_text proves no collision.
    assert enrichment[("word",   shared_id)]["display_text"] == row["word"]
    assert enrichment[("phrase", shared_id)]["display_text"] == row["surface_form"]


async def test_recommend_items_returns_phrase_enrichment(db_pool, client):
    """recommend_items(item_type='phrase') returns enriched phrase rows when the
    user has a phrase in learning state (path that previously worked but had
    its dispatch duplicated)."""
    phrase_row = await db_pool.fetchrow(
        "SELECT phrase_id, surface_form, language FROM phrase_table LIMIT 1"
    )
    if phrase_row is None:
        pytest.skip("phrase_table empty")
    phrase_id, surface_form, language = phrase_row["phrase_id"], phrase_row["surface_form"], phrase_row["language"]

    token = await _auth_token(client)
    uid = await db_pool.fetchval(
        "SELECT user_id FROM users WHERE email LIKE $1 ORDER BY user_id DESC LIMIT 1", cleanup_pattern()
    )
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'phrase', 'learning')",
        uid, phrase_id,
    )

    resp = await client.get(
        "/api/v1/recommendations/items",
        params={"language": language, "item_type": "phrase"},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()

    assert resp.status_code == 200
    matching = [i for i in body["items"] if i["item_id"] == phrase_id]
    assert matching, "expected the learning phrase to appear"
    assert matching[0]["item_type"]    == "phrase"
    assert matching[0]["display_text"] == surface_form


async def test_recommend_items_returns_grammar_rule_enrichment(db_pool, client):
    """recommend_items(item_type='grammar_rule') used to return [] no matter
    what (no enricher). After #5c it returns enriched rule rows."""
    rule_row = await db_pool.fetchrow(
        "SELECT rule_id, title, rule_type, language FROM grammar_rule_table LIMIT 1"
    )
    if rule_row is None:
        pytest.skip("grammar_rule_table empty — seed step did not run")
    rule_id, title, rule_type, language = (
        rule_row["rule_id"], rule_row["title"], rule_row["rule_type"], rule_row["language"],
    )

    token = await _auth_token(client)
    uid = await db_pool.fetchval(
        "SELECT user_id FROM users WHERE email LIKE $1 ORDER BY user_id DESC LIMIT 1", cleanup_pattern()
    )
    await db_pool.execute(
        "INSERT INTO user_word_knowledge (user_id, item_id, item_type, status) "
        "VALUES ($1::uuid, $2, 'grammar_rule', 'learning')",
        uid, rule_id,
    )

    resp = await client.get(
        "/api/v1/recommendations/items",
        params={"language": language, "item_type": "grammar_rule"},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()

    assert resp.status_code == 200
    matching = [i for i in body["items"] if i["item_id"] == rule_id]
    assert matching, "expected the learning grammar rule to appear"
    assert matching[0]["item_type"]      == "grammar_rule"
    assert matching[0]["display_text"]   == title
    assert matching[0]["secondary_text"] == rule_type
