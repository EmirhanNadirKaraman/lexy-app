"""
LLM cache tests.

Pure unit tests (no DB):
  - make_cache_key determinism and sensitivity

Integration tests (real DB via db_pool fixture):
  - cache miss returns None
  - set then get returns the stored value
  - hit_count increments on repeated gets
  - ON CONFLICT DO NOTHING: second set_cached for the same key is silently ignored
  - expired entries are treated as misses

Thundering-herd guard (#24, get_or_compute):
  - cache hit: compute is never called
  - concurrent same-key callers: compute called exactly once, all get the
    same result
  - concurrent different-key callers: compute runs independently per key
  - compute raises → lock released; later call retries
  - double-check inside the lock returns the value written by an earlier
    waiter (no duplicate compute)
"""
import asyncio
import uuid

import pytest

from backend.services import llm_cache_service
from backend.services.llm_cache_service import (
    get_cached,
    get_or_compute,
    make_cache_key,
    set_cached,
)
from ._cache_helper import test_model

# ---------------------------------------------------------------------------
# Unit tests — no DB required
# ---------------------------------------------------------------------------

def test_make_cache_key_is_deterministic():
    k1 = make_cache_key("guided_open", "model-x", {"a": 1, "b": 2})
    k2 = make_cache_key("guided_open", "model-x", {"b": 2, "a": 1})  # different insertion order
    assert k1 == k2


def test_make_cache_key_differs_on_prompt_key():
    k1 = make_cache_key("guided_open",   "model-x", {"w": "Hund"})
    k2 = make_cache_key("guided_close",  "model-x", {"w": "Hund"})
    assert k1 != k2


def test_make_cache_key_differs_on_model():
    k1 = make_cache_key("guided_open", "model-a", {"w": "Hund"})
    k2 = make_cache_key("guided_open", "model-b", {"w": "Hund"})
    assert k1 != k2


def test_make_cache_key_differs_on_params():
    k1 = make_cache_key("guided_open", "model-x", {"target_word": "laufen",  "language": "de"})
    k2 = make_cache_key("guided_open", "model-x", {"target_word": "rennen",  "language": "de"})
    k3 = make_cache_key("guided_open", "model-x", {"target_word": "laufen",  "language": "fr"})
    assert k1 != k2
    assert k1 != k3
    assert k2 != k3


def test_make_cache_key_returns_hex_string():
    k = make_cache_key("p", "m", {})
    assert len(k) == 64
    assert all(c in "0123456789abcdef" for c in k)


# ---------------------------------------------------------------------------
# Integration tests — real DB
# ---------------------------------------------------------------------------

def _unique_key() -> str:
    """Return a cache key that is guaranteed not to exist yet."""
    return make_cache_key("test_prompt", test_model(), {"uuid": uuid.uuid4().hex})


async def test_cache_miss_returns_none(db_pool):
    result = await get_cached(db_pool, _unique_key())
    assert result is None


async def test_set_and_get_returns_stored_value(db_pool):
    key = _unique_key()
    payload = {"opening": "Hallo, wie geht es dir?", "extra": 42}

    await set_cached(db_pool, key, "test_prompt", test_model(), payload)

    result = await get_cached(db_pool, key)
    assert result == payload


async def test_hit_count_increments(db_pool):
    key = _unique_key()
    await set_cached(db_pool, key, "test_prompt", test_model(), {"v": 1})

    await get_cached(db_pool, key)
    await get_cached(db_pool, key)

    row = await db_pool.fetchrow(
        "SELECT hit_count FROM llm_cache WHERE cache_key = $1", key
    )
    assert row["hit_count"] == 2


async def test_last_hit_at_is_updated(db_pool):
    key = _unique_key()
    await set_cached(db_pool, key, "test_prompt", test_model(), {"v": 1})

    before = await db_pool.fetchrow(
        "SELECT last_hit_at FROM llm_cache WHERE cache_key = $1", key
    )
    assert before["last_hit_at"] is None  # not yet hit

    await get_cached(db_pool, key)

    after = await db_pool.fetchrow(
        "SELECT last_hit_at FROM llm_cache WHERE cache_key = $1", key
    )
    assert after["last_hit_at"] is not None


async def test_second_set_cached_is_noop(db_pool):
    """ON CONFLICT DO NOTHING — the first stored value wins."""
    key = _unique_key()
    await set_cached(db_pool, key, "test_prompt", test_model(), {"v": "first"})
    await set_cached(db_pool, key, "test_prompt", test_model(), {"v": "second"})

    result = await get_cached(db_pool, key)
    assert result["v"] == "first"


async def test_expired_entry_is_treated_as_miss(db_pool):
    key = _unique_key()
    # Store with a TTL that has already passed by back-dating expires_at directly
    await db_pool.execute(
        """
        INSERT INTO llm_cache (cache_key, prompt_key, model, response, expires_at)
        VALUES ($1, 'test', $2, '{"v":1}'::jsonb,
                NOW() - INTERVAL '1 second')
        """,
        key, test_model(),
    )

    result = await get_cached(db_pool, key)
    assert result is None


async def test_non_expiring_entry_is_returned(db_pool):
    key = _unique_key()
    await set_cached(db_pool, key, "test_prompt", test_model(), {"v": "permanent"})

    result = await get_cached(db_pool, key)
    assert result == {"v": "permanent"}


async def test_ttl_entry_is_returned_before_expiry(db_pool):
    key = _unique_key()
    await set_cached(db_pool, key, "test_prompt", test_model(), {"v": "ttl"}, ttl_seconds=3600)

    result = await get_cached(db_pool, key)
    assert result == {"v": "ttl"}


# ---------------------------------------------------------------------------
# #24 — get_or_compute thundering-herd guard
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_locks_between_tests():
    """Drop any locks left over from previous tests so each test sees a clean
    set. Keys themselves are uuid-unique already, but starting from an empty
    dict keeps assertions about lock identity (used below) trustworthy."""
    llm_cache_service.reset_for_tests()
    yield
    llm_cache_service.reset_for_tests()


async def test_get_or_compute_hit_does_not_call_compute(db_pool):
    """If the cache already has a value, compute() is never invoked."""
    key = _unique_key()
    await set_cached(db_pool, key, "test_prompt", test_model(), {"v": "cached"})

    calls = 0
    async def _compute():
        nonlocal calls
        calls += 1
        return {"v": "fresh"}

    result = await get_or_compute(db_pool, key, "test_prompt", test_model(), _compute)
    assert result == {"v": "cached"}
    assert calls == 0


async def test_get_or_compute_miss_writes_and_returns(db_pool):
    """On a cold miss, compute() runs once and its result is both returned
    AND persisted to the cache table."""
    key = _unique_key()
    calls = 0
    async def _compute():
        nonlocal calls
        calls += 1
        return {"v": "fresh"}

    result = await get_or_compute(db_pool, key, "test_prompt", test_model(), _compute)
    assert result == {"v": "fresh"}
    assert calls == 1

    # Persisted: a second call reads from cache, doesn't call compute.
    result2 = await get_or_compute(db_pool, key, "test_prompt", test_model(), _compute)
    assert result2 == {"v": "fresh"}
    assert calls == 1


async def test_concurrent_same_key_calls_compute_exactly_once(db_pool):
    """The load-bearing test: ten concurrent callers for the same cache_key
    must produce exactly one compute() invocation and all receive the same
    result."""
    key = _unique_key()
    calls = 0
    gate = asyncio.Event()

    async def _compute():
        nonlocal calls
        calls += 1
        # Hold inside compute so all waiters queue on the lock before we
        # actually finish — this is what surfaces the bug if locking is
        # missing.
        await gate.wait()
        return {"v": "fresh", "n": calls}

    async def _caller():
        return await get_or_compute(db_pool, key, "test_prompt", test_model(), _compute)

    # Fire 10 callers concurrently.
    tasks = [asyncio.create_task(_caller()) for _ in range(10)]
    # Let them all reach the lock before unblocking compute.
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks)

    assert calls == 1, f"expected exactly one compute() invocation, got {calls}"
    # All 10 callers received the SAME dict the single compute returned.
    for r in results:
        assert r == {"v": "fresh", "n": 1}


async def test_concurrent_different_keys_do_not_block(db_pool):
    """Two different cache_keys must compute in parallel — the per-key lock
    must not serialise unrelated work."""
    key_a, key_b = _unique_key(), _unique_key()

    # Each compute parks on its own event until both have started — proves
    # they're actually running concurrently and not waiting on each other.
    a_started = asyncio.Event()
    b_started = asyncio.Event()

    async def _compute_a():
        a_started.set()
        await b_started.wait()   # would deadlock if A blocked B
        return {"k": "a"}

    async def _compute_b():
        b_started.set()
        await a_started.wait()
        return {"k": "b"}

    task_a = asyncio.create_task(
        get_or_compute(db_pool, key_a, test_model("-p"), test_model(), _compute_a)
    )
    task_b = asyncio.create_task(
        get_or_compute(db_pool, key_b, test_model("-p"), test_model(), _compute_b)
    )

    # Tight timeout — if locking is wrong this hangs forever.
    results = await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=2.0)
    assert results[0] == {"k": "a"}
    assert results[1] == {"k": "b"}


async def test_compute_exception_releases_lock_and_allows_retry(db_pool):
    """If compute() raises, the lock must be released (the async-with handles
    this) and the cache must NOT be populated. A subsequent call with a
    working compute then succeeds."""
    key = _unique_key()
    attempts = 0

    async def _failing():
        nonlocal attempts
        attempts += 1
        raise RuntimeError("provider blew up")

    with pytest.raises(RuntimeError, match="provider blew up"):
        await get_or_compute(db_pool, key, "test_prompt", test_model(), _failing)

    assert attempts == 1
    # Cache was NOT populated.
    assert await get_cached(db_pool, key) is None

    # A second call with a working compute succeeds — the lock from the
    # previous failure didn't strand the key.
    async def _working():
        return {"v": "fresh"}

    result = await get_or_compute(db_pool, key, "test_prompt", test_model(), _working)
    assert result == {"v": "fresh"}


async def test_double_check_returns_first_writers_value(db_pool):
    """A waiter that enters the lock AFTER the first caller has written the
    cache must short-circuit on the double-check — its own compute() should
    never run.

    Differs from `test_concurrent_same_key_calls_compute_exactly_once` by
    proving the *waiter's* own compute is bypassed (not just that the first
    compute happens once)."""
    key = _unique_key()

    # First caller's compute is held until we explicitly release it.
    first_gate = asyncio.Event()
    second_compute_called = False

    async def _first_compute():
        await first_gate.wait()
        return {"v": "first-writer"}

    async def _second_compute():
        nonlocal second_compute_called
        second_compute_called = True
        return {"v": "second-writer"}

    first_task = asyncio.create_task(
        get_or_compute(db_pool, key, test_model("-p"), test_model(), _first_compute)
    )
    # Yield so the first task acquires the lock and enters compute().
    await asyncio.sleep(0.05)

    # Now fire the second caller. It'll cache-miss on the fast path, queue on
    # the lock, and only enter the critical section after the first finishes.
    second_task = asyncio.create_task(
        get_or_compute(db_pool, key, test_model("-p"), test_model(), _second_compute)
    )
    await asyncio.sleep(0.05)

    # Release the first compute — it writes the cache then exits the lock.
    first_gate.set()
    results = await asyncio.gather(first_task, second_task)

    assert results[0] == {"v": "first-writer"}
    assert results[1] == {"v": "first-writer"}, \
        "second caller must see the first writer's value, not its own"
    assert second_compute_called is False, \
        "double-check must short-circuit the waiter's compute()"


# ---------------------------------------------------------------------------
# Isolation — these tests must not leave residue in the shared table
#
# `llm_cache` is GLOBAL with no user FK, so the autouse user cleanup cannot
# reach it. Before the `_cache_helper` tag, a dev database had accumulated
# 2,760 rows of pure test residue, 335 of them sitting on real `item_gloss`
# keys with the production model id — i.e. a test run could hand `"stub gloss"`
# to a real SRS card, and a future curated gloss seed would have collided with
# them. See tests/_cache_helper.py.
# ---------------------------------------------------------------------------


async def test_cache_writes_are_tagged_with_a_non_production_model(db_pool):
    """Every row a test writes must be reapable AND unable to collide.

    The tag lives in `model` because that is the one field every write path
    controls — direct `set_cached` calls and the provider fakes, whose
    `model_id` feeds `make_cache_key`.
    """
    key = _unique_key()
    await set_cached(db_pool, key, "test_prompt", test_model(), {"v": 1})

    model = await db_pool.fetchval(
        "SELECT model FROM llm_cache WHERE cache_key = $1", key,
    )

    assert model.startswith("zztest-model"), model
    assert not model.startswith("claude"), "must never look like a production model"
    assert not model.startswith("curated"), "must never look like a seeded curated row"


async def test_cleanup_pattern_only_matches_this_workers_rows(db_pool):
    """Parallel safety: a finishing worker must not reap another's live rows."""
    from ._cache_helper import cleanup_pattern, worker_id

    pattern = cleanup_pattern()
    assert pattern.endswith("%")
    assert worker_id() in pattern

    mine = test_model()
    other = f"zztest-model-gw{worker_id()}-other"
    matches = await db_pool.fetchrow(
        "SELECT $1::text LIKE $3 AS mine_matches, $2::text LIKE $3 AS other_matches",
        mine, other, pattern,
    )
    assert matches["mine_matches"] is True
    assert matches["other_matches"] is False


async def test_cleanup_would_spare_a_seeded_curated_row(db_pool):
    """The fixture must not reach rows a future gloss seed writes.

    Pinned because the obvious cleanup predicate — `prompt_key='item_gloss'` —
    would wipe exactly those rows. Cleanup keys on the tagged model instead.
    """
    from ._cache_helper import cleanup_pattern

    spared = await db_pool.fetchrow(
        "SELECT $1::text LIKE $3 AS curated, $2::text LIKE $3 AS production",
        "curated:words_4000_old", "claude-haiku-4-5-20251001", cleanup_pattern(),
    )
    assert spared["curated"] is False
    assert spared["production"] is False


async def test_suite_leaves_no_tagged_rows_behind(db_pool):
    """End-to-end proof the autouse fixture actually reaps.

    Writes a tagged row, then asserts a *previous* test's tagged rows are
    already gone — i.e. exactly one row (this test's own) carries the tag at
    this point in the run.
    """
    from ._cache_helper import cleanup_pattern

    before = await db_pool.fetchval(
        "SELECT count(*) FROM llm_cache WHERE model LIKE $1", cleanup_pattern(),
    )
    assert before == 0, f"{before} tagged rows survived earlier tests"

    await set_cached(db_pool, _unique_key(), "test_prompt", test_model(), {"v": 1})

    assert await db_pool.fetchval(
        "SELECT count(*) FROM llm_cache WHERE model LIKE $1", cleanup_pattern(),
    ) == 1
