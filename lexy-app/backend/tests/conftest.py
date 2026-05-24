"""
Test fixtures.

Run tests from lexy-app/:
    cd lexy-app
    pytest                 # serial
    pytest -n auto         # parallel via pytest-xdist

The tests hit the real development database. Each test uses emails in the
pattern  test+{worker_id}_{random}@example.com  (built by `make_test_email()`
in `_email_helper.py`). The autouse `cleanup` fixture deletes only rows
matching THIS worker's pattern, so parallel workers can't trample each other's
in-flight users.
"""
import os
import uuid
from pathlib import Path

import asyncpg
import pytest
from dotenv import load_dotenv
from httpx import ASGITransport, AsyncClient

from backend.database import _resolve_ssl
from . import _word_helper
from ._email_helper import cleanup_pattern

# .env is four levels up from this file:
# tests/ → backend/ → lexy-app/ → sentence-to-phrase-matcher/
load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")


@pytest.fixture(scope="session")
async def db_pool():
    pool = await asyncpg.create_pool(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        ssl=_resolve_ssl(os.getenv("DB_SSL_MODE")),  # S4: honour DB_SSL_MODE (default plaintext)
    )
    yield pool
    await pool.close()


@pytest.fixture
async def client(db_pool):
    """
    HTTP client wired to the FastAPI app with the test pool injected.

    The app lifespan still runs (creating its own pool), but all routes
    use our test pool via dependency_overrides.
    """
    from backend.database import get_pool
    from backend.main import app

    app.dependency_overrides[get_pool] = lambda: db_pool

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
async def cleanup(db_pool):
    """Delete all test users created during a test, and clear any in-memory
    rate-limiter state so per-user counters from one test don't carry into
    the next."""
    yield
    # Per-worker scoped delete — under pytest-xdist each worker only touches
    # the rows whose email carries its own worker tag. Serial runs collapse to
    # the 'main' tag.
    await db_pool.execute(
        "DELETE FROM users WHERE email LIKE $1",
        cleanup_pattern(),
    )
    # Reset in-process LLM rate limiter (#12). Importing here keeps the
    # fixture cheap when the limiter module isn't loaded.
    from backend.services import rate_limiter
    rate_limiter.reset_for_tests()


async def _reap_word_ids(pool, word_ids: list[int]) -> None:
    """Delete synthetic word_table rows a test created.

    word_table is GLOBAL (not user-scoped), so the autouse user cleanup above
    can't reach these rows — without this they accumulate forever and break
    LIMIT-1 picks in other suites. Safe + order-independent because:
      * word_table's FK children (word_to_sentence, word_strength,
        most_frequent_words) are ON DELETE CASCADE;
      * user_word_knowledge / srs_cards have NO FK to word_table (they use the
        polymorphic (item_id, item_type) key) and are removed via the user
        CASCADE, so they never block this delete.
    Idempotent: deleting an already-gone id is a no-op.
    """
    if not word_ids:
        return
    await pool.execute("DELETE FROM word_table WHERE word_id = ANY($1::int[])", word_ids)


@pytest.fixture
async def tracked_words(db_pool):
    """Registry for synthetic word_table rows a test inserts. Append each
    created word_id (helpers like test_words._create_ambiguous_word do this for
    you); the teardown reaps them via _reap_word_ids so word_table stays clean.
    """
    created: list[int] = []
    yield created
    await _reap_word_ids(db_pool, created)


@pytest.fixture
async def make_word(db_pool, tracked_words):
    """Factory for test-OWNED, xdist-safe `word_table` rows.

    Use this instead of `SELECT ... FROM word_table LIMIT N`. An unfiltered pick
    returns a SHARED catalog row that another xdist worker can reap mid-test
    (its `_reap_word_ids` teardown), after which `review_service.get_due_cards`'
    `WHERE wt.word_id IS NOT NULL` drops the card and the test fails randomly —
    the root cause of the SRS flake cluster (see docs/TESTS.md). Each call here
    inserts a row with a globally-unique surface (`_testword_<uuid>`) and
    registers it with `tracked_words`, so the existing reap deletes exactly what
    this test created. The unique surface means no unfiltered pick anywhere can
    return it and no other worker can delete it.

        word_id, surface = await make_word()                  # German NOUN
        word_id, surface = await make_word("es", pos="VERB")  # custom

    Returns an async callable `(language='de', *, word, lemma, pos, tag,
    frequency) -> (word_id, surface)`.
    """
    async def _make(
        language: str = "de",
        *,
        word: str | None = None,
        lemma: str | None = None,
        pos: str = "NOUN",
        tag: str = "NN",
        frequency: int | None = None,
    ) -> tuple[int, str]:
        surface = word or f"_testword_{uuid.uuid4().hex[:12]}"
        if frequency is None:
            wid = await db_pool.fetchval(
                "INSERT INTO word_table (word, language, pos, tag, lemma) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING word_id",
                surface, language, pos, tag, lemma or surface,
            )
        else:
            wid = await db_pool.fetchval(
                "INSERT INTO word_table (word, language, pos, tag, lemma, frequency) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING word_id",
                surface, language, pos, tag, lemma or surface, frequency,
            )
        tracked_words.append(wid)
        return wid, surface

    return _make


@pytest.fixture
async def srs_word(make_word) -> tuple[int, str, str]:
    """Convenience: one owned German word as (word_id, surface, 'de')."""
    wid, surface = await make_word("de")
    return wid, surface, "de"


@pytest.fixture(autouse=True)
async def _reap_owned_words(db_pool):
    """Reap words created via `_word_helper.insert_owned_word` after each test.

    Files with an existing local word-helper + many call sites rewrite that
    helper to call `insert_owned_word` (which registers the id); this autouse
    fixture deletes exactly those rows afterwards, so the shared `word_table`
    stays clean and no row a test created can leak into another worker's pick.
    Cheap no-op for tests that create no owned words. (The fixture-style
    `make_word` path reaps via `tracked_words` instead.)
    """
    yield
    ids = _word_helper._drain()
    if ids:
        await db_pool.execute(
            "DELETE FROM word_table WHERE word_id = ANY($1::int[])", ids
        )
