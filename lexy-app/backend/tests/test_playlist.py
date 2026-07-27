"""
Playlist generation tests.

Unit tests (no DB) — test pure functions:
  - greedy_cover: picks video with most coverage first
  - greedy_cover: stops at max_videos
  - greedy_cover: stops when all targets covered
  - greedy_cover: stops when no remaining video covers anything new
  - greedy_cover: tiebreaks by shorter duration
  - greedy_cover: empty inputs
  - ilp_cover: beats greedy on a known greedy trap (the reason it exists)
  - ilp_cover: respects max_videos, covers all when the cap allows
  - ilp_cover: never selects a zero-contribution video
  - ilp_cover: duration tiebreak, best-value-first ordering, determinism
  - ilp_cover: same empty/zero-cap contract as greedy_cover
  - compute_coverage_stats: correct counts and percentage
  - compute_coverage_stats: full coverage
  - compute_coverage_stats: no coverage
  - compute_coverage_stats: zero targets

Integration tests (real DB):
  - generate_playlist returns videos when known word_ids given
  - generate_playlist returns empty playlist for word_ids with no videos
  - generate_playlist coverage stats match returned videos
  - generate_playlist respects max_videos
  - generate_playlist 422 for unsupported item_type

HTTP tests (FastAPI client):
  - POST /api/v1/playlists/generate returns 200 with correct shape
  - POST /api/v1/playlists/generate requires auth
  - POST /api/v1/playlists/generate with empty item_ids returns 422
  - POST /api/v1/playlists/generate accepts algorithm="ilp"
  - POST /api/v1/playlists/generate 422s an unknown algorithm
  - POST /api/v1/playlists/generate 503s when the solver is unavailable
  - POST /api/v1/playlists/generate defaults to greedy (never invokes the solver)
"""

import pytest

from backend.services.playlist_service import (
    compute_coverage_stats,
    generate_playlist,
    greedy_cover,
    ilp_cover,
)
from ._email_helper import make_test_email


# ---------------------------------------------------------------------------
# Unit tests — pure functions, no DB
# ---------------------------------------------------------------------------

class TestGreedyCover:
    def test_picks_video_with_most_coverage_first(self):
        coverage = {
            "vid_A": {1, 2, 3},
            "vid_B": {1, 4},
            "vid_C": {5},
        }
        result = greedy_cover(coverage, targets={1, 2, 3, 4, 5}, max_videos=10)
        assert result[0] == "vid_A"   # covers 3 items — best first pick
        assert set(result) == {"vid_A", "vid_B", "vid_C"}

    def test_stops_at_max_videos(self):
        coverage = {
            "vid_A": {1, 2},
            "vid_B": {3, 4},
            "vid_C": {5, 6},
        }
        result = greedy_cover(coverage, targets={1, 2, 3, 4, 5, 6}, max_videos=2)
        assert len(result) == 2

    def test_stops_when_all_targets_covered(self):
        coverage = {
            "vid_A": {1, 2, 3},
            "vid_B": {4, 5},
        }
        result = greedy_cover(coverage, targets={1, 2, 3}, max_videos=10)
        assert result == ["vid_A"]   # vid_B adds nothing new

    def test_stops_when_no_remaining_coverage(self):
        # Item 3 doesn't appear in any video
        coverage = {
            "vid_A": {1, 2},
        }
        result = greedy_cover(coverage, targets={1, 2, 3}, max_videos=10)
        assert result == ["vid_A"]   # selected, then stopped (nothing covers 3)

    def test_tiebreak_prefers_shorter_video(self):
        coverage = {
            "vid_long":  {1},
            "vid_short": {1},
        }
        durations = {"vid_long": 600.0, "vid_short": 120.0}
        result = greedy_cover(coverage, {1}, max_videos=1, video_durations=durations)
        assert result == ["vid_short"]

    def test_empty_targets_returns_empty(self):
        coverage = {"vid_A": {1, 2}}
        assert greedy_cover(coverage, targets=set(), max_videos=10) == []

    def test_empty_coverage_returns_empty(self):
        assert greedy_cover({}, targets={1, 2}, max_videos=10) == []

    def test_single_video_covering_all(self):
        coverage = {"vid_A": {1, 2, 3}}
        result = greedy_cover(coverage, {1, 2, 3}, max_videos=10)
        assert result == ["vid_A"]

    def test_order_reflects_greedy_priority(self):
        # vid_A covers 3 items; vid_B covers only 2 → vid_A is picked first
        coverage = {
            "vid_A": {1, 2, 3},
            "vid_B": {1, 4},   # 2 new items (1 already in vid_A)
        }
        result = greedy_cover(coverage, {1, 2, 3, 4}, max_videos=10)
        assert result[0] == "vid_A"   # 3 new items in round 1
        assert result[1] == "vid_B"   # only uncovered item left is 4

    def test_max_videos_zero_returns_empty(self):
        coverage = {"vid_A": {1}}
        result = greedy_cover(coverage, {1}, max_videos=0)
        assert result == []


class TestIlpCover:
    """`ilp_cover` must satisfy the same contract as `greedy_cover` (it is a
    drop-in `optimizer=`), and additionally be optimal under the max_videos cap."""

    def test_ilp_beats_greedy_on_a_known_greedy_trap(self):
        """The reason this optimizer exists.

        Three videos each cover two targets, so greedy's first pick is decided
        by its duration tiebreak — and vid_C is shortest. Taking vid_C first
        strands one target: whatever greedy picks second adds only one new item,
        so it covers 3 of 4. The optimal pair is A+B, which covers all 4.

        If this test ever fails because greedy also gets 4, the trap has stopped
        being a trap and the test is no longer proving anything — rebuild it
        rather than deleting it.
        """
        coverage = {
            "vid_A": {1, 2},
            "vid_B": {3, 4},
            "vid_C": {2, 3},
        }
        targets = {1, 2, 3, 4}
        durations = {"vid_A": 300.0, "vid_B": 300.0, "vid_C": 100.0}

        greedy = greedy_cover(coverage, targets, max_videos=2, video_durations=durations)
        optimal = ilp_cover(coverage, targets, max_videos=2, video_durations=durations)

        greedy_covered = set().union(*(coverage[v] for v in greedy))
        optimal_covered = set().union(*(coverage[v] for v in optimal))

        assert greedy_covered == {1, 2, 3}          # greedy strands target 4
        assert optimal_covered == targets           # ILP covers everything
        assert set(optimal) == {"vid_A", "vid_B"}

    def test_respects_max_videos(self):
        coverage = {"vid_A": {1}, "vid_B": {2}, "vid_C": {3}}
        result = ilp_cover(coverage, {1, 2, 3}, max_videos=2)
        assert len(result) == 2

    def test_covers_everything_when_cap_allows(self):
        coverage = {"vid_A": {1, 2}, "vid_B": {3}}
        result = ilp_cover(coverage, {1, 2, 3}, max_videos=10)
        assert set(result) == {"vid_A", "vid_B"}

    def test_never_selects_a_video_that_covers_nothing(self):
        """The ε duration penalty makes a zero-contribution pick strictly worse
        than not picking, so the cap is not filled with useless videos."""
        coverage = {"vid_A": {1, 2}, "vid_useless": {99}}
        result = ilp_cover(coverage, {1, 2}, max_videos=10)
        assert result == ["vid_A"]

    def test_prefers_shorter_video_when_coverage_ties(self):
        coverage = {"vid_long": {1, 2}, "vid_short": {1, 2}}
        durations = {"vid_long": 900.0, "vid_short": 60.0}
        result = ilp_cover(coverage, {1, 2}, max_videos=1, video_durations=durations)
        assert result == ["vid_short"]

    def test_result_is_ordered_best_value_first(self):
        """_build_result renders in list order, so the highest-coverage video
        must come first regardless of which optimal set CBC happened to return."""
        coverage = {"vid_small": {4}, "vid_big": {1, 2, 3}}
        result = ilp_cover(coverage, {1, 2, 3, 4}, max_videos=2)
        assert result == ["vid_big", "vid_small"]

    def test_deterministic_across_repeated_solves(self):
        """CBC may return any equally-optimal solution; the sort must make the
        output stable so the same request yields the same playlist."""
        coverage = {"vid_A": {1, 2}, "vid_B": {3, 4}, "vid_C": {2, 3}}
        targets = {1, 2, 3, 4}
        runs = {tuple(ilp_cover(coverage, targets, max_videos=2)) for _ in range(5)}
        assert len(runs) == 1

    # --- same edge cases greedy_cover is held to -----------------------------

    def test_empty_targets_returns_empty(self):
        assert ilp_cover({"vid_A": {1}}, set(), max_videos=10) == []

    def test_empty_coverage_returns_empty(self):
        assert ilp_cover({}, {1, 2}, max_videos=10) == []

    def test_max_videos_zero_returns_empty(self):
        assert ilp_cover({"vid_A": {1}}, {1}, max_videos=0) == []

    def test_no_video_covers_any_target_returns_empty(self):
        assert ilp_cover({"vid_A": {99}}, {1, 2}, max_videos=10) == []


class TestComputeCoverageStats:
    def test_partial_coverage(self):
        selected = ["vid_A", "vid_B"]
        coverage = {"vid_A": {1, 2}, "vid_B": {3}}
        targets = {1, 2, 3, 4}
        stats = compute_coverage_stats(selected, coverage, targets)

        assert stats["target_count"] == 4
        assert stats["covered_count"] == 3
        assert stats["coverage_pct"] == 75.0
        assert stats["uncovered_item_ids"] == [4]
        assert stats["video_count"] == 2

    def test_full_coverage(self):
        selected = ["vid_A"]
        coverage = {"vid_A": {1, 2, 3}}
        stats = compute_coverage_stats(selected, coverage, {1, 2, 3})

        assert stats["covered_count"] == 3
        assert stats["coverage_pct"] == 100.0
        assert stats["uncovered_item_ids"] == []

    def test_no_coverage(self):
        stats = compute_coverage_stats([], {}, {1, 2, 3})
        assert stats["covered_count"] == 0
        assert stats["coverage_pct"] == 0.0
        assert sorted(stats["uncovered_item_ids"]) == [1, 2, 3]

    def test_zero_targets(self):
        stats = compute_coverage_stats([], {}, set())
        assert stats["target_count"] == 0
        assert stats["coverage_pct"] == 0.0
        assert stats["video_count"] == 0

    def test_uncovered_ids_are_sorted(self):
        selected = ["vid_A"]
        coverage = {"vid_A": {5}}
        targets = {1, 2, 3, 4, 5}
        stats = compute_coverage_stats(selected, coverage, targets)
        assert stats["uncovered_item_ids"] == [1, 2, 3, 4]

    def test_selected_video_not_in_coverage_dict(self):
        """If a selected video_id has no coverage entry, it contributes 0 items."""
        stats = compute_coverage_stats(["ghost_vid"], {}, {1, 2})
        assert stats["covered_count"] == 0


# ---------------------------------------------------------------------------
# Integration tests — real DB
# ---------------------------------------------------------------------------

async def _word_ids_in_db(pool, n: int = 3) -> list[int]:
    """Return up to n word_ids that actually appear in word_to_sentence."""
    rows = await pool.fetch(
        """
        SELECT DISTINCT wts.word_id
        FROM word_to_sentence wts
        JOIN sentence s ON s.sentence_id = wts.sentence_id
        JOIN video v    ON v.video_id = s.video_id
        LIMIT $1
        """,
        n,
    )
    if not rows:
        pytest.skip("No word_to_sentence data — run the subtitle pipeline first")
    return [r["word_id"] for r in rows]


async def _language_for_word(pool, word_id: int) -> str:
    row = await pool.fetchrow(
        "SELECT language FROM word_table WHERE word_id = $1", word_id
    )
    return row["language"]


async def test_generate_playlist_returns_videos(db_pool):
    word_ids = await _word_ids_in_db(db_pool, 3)
    language = await _language_for_word(db_pool, word_ids[0])

    result = await generate_playlist(
        db_pool, item_ids=word_ids, item_type="word",
        language=language, max_videos=5,
    )

    assert isinstance(result["videos"], list)
    assert len(result["videos"]) >= 1
    # Every returned video must cover at least one target
    for video in result["videos"]:
        assert video["covered_count"] >= 1
        assert len(video["covered_item_ids"]) >= 1


async def test_generate_playlist_empty_for_unknown_word_ids(db_pool):
    result = await generate_playlist(
        db_pool,
        item_ids=[999_999_998, 999_999_999],
        item_type="word",
        language="de",
        max_videos=10,
    )
    assert result["videos"] == []
    assert result["coverage"]["covered_count"] == 0
    assert result["coverage"]["coverage_pct"] == 0.0


async def test_generate_playlist_coverage_stats_consistent(db_pool):
    word_ids = await _word_ids_in_db(db_pool, 5)
    language = await _language_for_word(db_pool, word_ids[0])

    result = await generate_playlist(
        db_pool, item_ids=word_ids, item_type="word",
        language=language, max_videos=10,
    )

    cov = result["coverage"]
    # covered_count == union of all covered_item_ids across videos
    covered_union: set[int] = set()
    for video in result["videos"]:
        covered_union |= set(video["covered_item_ids"])

    assert cov["covered_count"] == len(covered_union)
    assert cov["video_count"] == len(result["videos"])
    assert cov["target_count"] == len(word_ids)
    expected_pct = round(100.0 * len(covered_union) / len(word_ids), 1)
    assert cov["coverage_pct"] == expected_pct


async def test_generate_playlist_respects_max_videos(db_pool):
    word_ids = await _word_ids_in_db(db_pool, 10)
    language = await _language_for_word(db_pool, word_ids[0])

    result = await generate_playlist(
        db_pool, item_ids=word_ids, item_type="word",
        language=language, max_videos=2,
    )
    assert len(result["videos"]) <= 2


async def test_generate_playlist_unsupported_item_type(db_pool):
    with pytest.raises(ValueError, match="not yet supported"):
        await generate_playlist(
            db_pool, item_ids=[1], item_type="phrase",
            language="de", max_videos=5,
        )


# ---------------------------------------------------------------------------
# HTTP tests — FastAPI client
# ---------------------------------------------------------------------------

async def _auth_token(client) -> str:
    email = make_test_email()
    await client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    return resp.json()["access_token"]


async def test_endpoint_returns_200(client, db_pool):
    word_ids = await _word_ids_in_db(db_pool, 2)
    language = await _language_for_word(db_pool, word_ids[0])
    token = await _auth_token(client)

    resp = await client.post(
        "/api/v1/playlists/generate",
        json={"item_ids": word_ids, "language": language, "max_videos": 5},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "videos" in body
    assert "coverage" in body


async def test_endpoint_requires_auth(client):
    resp = await client.post(
        "/api/v1/playlists/generate",
        json={"item_ids": [1, 2], "language": "de"},
    )
    assert resp.status_code == 403


async def test_endpoint_rejects_empty_item_ids(client, db_pool):
    token = await _auth_token(client)
    resp = await client.post(
        "/api/v1/playlists/generate",
        json={"item_ids": [], "language": "de"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_endpoint_response_shape(client, db_pool):
    word_ids = await _word_ids_in_db(db_pool, 3)
    language = await _language_for_word(db_pool, word_ids[0])
    token = await _auth_token(client)

    resp = await client.post(
        "/api/v1/playlists/generate",
        json={"item_ids": word_ids, "language": language, "max_videos": 3},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.json()
    cov = body["coverage"]
    assert "target_count" in cov
    assert "covered_count" in cov
    assert "coverage_pct" in cov
    assert "uncovered_item_ids" in cov
    assert "video_count" in cov

    for video in body["videos"]:
        assert "video_id" in video
        assert "title" in video
        assert "start_time" in video
        assert "covered_item_ids" in video
        assert "covered_count" in video


async def test_endpoint_accepts_ilp_algorithm(client, db_pool):
    """The opt-in optimal mode is reachable over HTTP and returns the same shape."""
    word_ids = await _word_ids_in_db(db_pool, 2)
    language = await _language_for_word(db_pool, word_ids[0])
    token = await _auth_token(client)

    resp = await client.post(
        "/api/v1/playlists/generate",
        json={
            "item_ids": word_ids,
            "language": language,
            "max_videos": 5,
            "algorithm": "ilp",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "videos" in body
    assert "coverage" in body


async def test_endpoint_rejects_unknown_algorithm(client, db_pool):
    """Pydantic gates the enum, so a typo never reaches the optimizer lookup."""
    word_ids = await _word_ids_in_db(db_pool, 1)
    language = await _language_for_word(db_pool, word_ids[0])
    token = await _auth_token(client)

    resp = await client.post(
        "/api/v1/playlists/generate",
        json={
            "item_ids": word_ids,
            "language": language,
            "algorithm": "simulated_annealing",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 422


async def test_endpoint_returns_503_when_solver_unavailable(client, db_pool, monkeypatch):
    """A missing PuLP (or a non-optimal solve) must surface as 503, not 500.

    The request itself is valid — only the optimal backend is unavailable — and
    retrying with algorithm='greedy' will succeed, so this is a service-
    availability failure rather than a server error.
    """
    from backend.routers import playlists as playlists_router

    def _no_solver(*_args, **_kwargs):
        raise RuntimeError("The 'ilp' playlist algorithm requires PuLP.")

    monkeypatch.setitem(playlists_router._OPTIMIZERS, "ilp", _no_solver)

    word_ids = await _word_ids_in_db(db_pool, 2)
    language = await _language_for_word(db_pool, word_ids[0])
    token = await _auth_token(client)

    resp = await client.post(
        "/api/v1/playlists/generate",
        json={
            "item_ids": word_ids,
            "language": language,
            "algorithm": "ilp",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 503
    assert "PuLP" in resp.json()["detail"]


async def test_greedy_remains_the_default(client, db_pool, monkeypatch):
    """Omitting `algorithm` must not invoke the solver — greedy stays default."""
    from backend.routers import playlists as playlists_router

    def _should_not_run(*_args, **_kwargs):
        raise AssertionError("ilp_cover was called for a request with no algorithm")

    monkeypatch.setitem(playlists_router._OPTIMIZERS, "ilp", _should_not_run)

    word_ids = await _word_ids_in_db(db_pool, 2)
    language = await _language_for_word(db_pool, word_ids[0])
    token = await _auth_token(client)

    resp = await client.post(
        "/api/v1/playlists/generate",
        json={"item_ids": word_ids, "language": language},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
