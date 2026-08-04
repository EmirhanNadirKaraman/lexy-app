"""Migration 025's downgrade guard (audit finding tests_ci:db-02).

`book_blocks.tokens` holds the `token_id`s that `reading_selections.anchors`
points at, with no FK between them. `downgrade()` used to be a bare
`DROP COLUMN`: dropping and re-upgrading re-mints every token_id, so the
anchors silently become references to ids that exist nowhere. Nothing errors —
the selections just stop resolving.

What this file locks in:
  * `_tokenize` really is unstable across runs (the reason a downgrade is
    unrecoverable rather than merely inconvenient);
  * the guard's counting SQL is valid against the LIVE schema and counts the
    right rows — token_id anchors yes, pre-025 legacy anchors no;
  * `downgrade()` raises before executing any DDL when dependents exist, and
    proceeds when they don't.

**This suite never runs `alembic downgrade`.** The tests hit the shared dev
database (see conftest); a real downgrade would drop `book_blocks.tokens` out
from under every other suite. Instead the SQL constant is executed directly
through asyncpg, and `downgrade()` is driven with a fake `op` whose
`execute()` only records what it was handed.
"""
import importlib.util
import json
import uuid
from pathlib import Path

import pytest

from ._email_helper import make_test_email

# ---------------------------------------------------------------------------
# Load the migration module by path.
#
# `migrations/versions/` is not a package and the filename starts with a digit,
# so a normal import is impossible. Importing has no side effects: module level
# is revision identifiers, two compiled regexes and the SQL constant.
# ---------------------------------------------------------------------------

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations" / "versions" / "025_block_token_ids.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_025", _MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = _load_migration()


# ---------------------------------------------------------------------------
# Fakes for driving downgrade() without a database or alembic context
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    """Stands in for `op.get_bind()`; returns a canned count."""

    def __init__(self, count):
        self._count = count
        self.queries: list[str] = []

    def execute(self, statement, *args, **kwargs):
        self.queries.append(str(statement))
        return _FakeResult(self._count)


class _FakeOp:
    """Stands in for the alembic `op` module."""

    def __init__(self, conn):
        self._conn = conn
        self.executed: list[str] = []

    def get_bind(self):
        return self._conn

    def execute(self, sql):
        self.executed.append(str(sql))


@pytest.fixture
def fake_op(monkeypatch):
    """Patch the migration's `op` with a recorder; returns a factory."""
    def _install(count):
        conn = _FakeConn(count)
        op = _FakeOp(conn)
        monkeypatch.setattr(mig, "op", op)
        return op

    return _install


# ---------------------------------------------------------------------------
# Why a downgrade is unrecoverable: the backfill is not reproducible
# ---------------------------------------------------------------------------

def test_tokenize_mints_fresh_ids_every_run():
    """Re-running the backfill cannot reconstruct the previous token_ids.

    This is the whole reason the guard exists. If `_tokenize` were
    deterministic, a downgrade/re-upgrade cycle would restore the same ids and
    the anchors would survive.
    """
    first = mig._tokenize("Der Hund läuft")
    second = mig._tokenize("Der Hund läuft")

    assert [t["text"] for t in first] == [t["text"] for t in second], (
        "same input must produce the same token TEXTS"
    )
    assert [t["token_id"] for t in first] != [t["token_id"] for t in second], (
        "token_ids must differ across runs — if this ever passes deterministically, "
        "the downgrade guard's rationale needs revisiting"
    )


# ---------------------------------------------------------------------------
# downgrade() behaviour
# ---------------------------------------------------------------------------

def test_downgrade_refuses_when_dependent_anchors_exist(fake_op):
    op = fake_op(5)

    with pytest.raises(RuntimeError, match="Refusing to downgrade 025"):
        mig.downgrade()

    assert op.executed == [], (
        "the guard must raise BEFORE any DDL runs — a DROP COLUMN executed "
        "ahead of the check would destroy the data the check exists to protect"
    )


def test_downgrade_refusal_reports_the_row_count_and_a_way_forward(fake_op):
    fake_op(7)

    with pytest.raises(RuntimeError) as excinfo:
        mig.downgrade()

    message = str(excinfo.value)
    assert "7" in message, "operator needs to know how many rows are at stake"
    assert "reading_selections" in message, "name the table holding the dependents"
    assert "DELETE FROM reading_selections" in message, (
        "the refusal must hand the operator the explicit opt-out — deleting the "
        "dependent rows themselves — since there is deliberately no force flag"
    )


def test_downgrade_proceeds_when_no_dependents(fake_op):
    op = fake_op(0)

    mig.downgrade()

    assert any("DROP COLUMN tokens" in sql for sql in op.executed), (
        f"with zero dependents the column must still be dropped; got {op.executed}"
    )


def test_downgrade_treats_null_count_as_zero(fake_op):
    """`.scalar()` returning None must not blow up the guard."""
    op = fake_op(None)

    mig.downgrade()

    assert any("DROP COLUMN tokens" in sql for sql in op.executed)


def test_downgrade_has_no_force_flag():
    """No env-var escape hatch: the only way past the guard is deleting the rows.

    A flag would let an operator wave away the data loss without touching (or
    seeing) the data. If someone adds one, this test should be deleted
    deliberately, not silently.
    """
    source = _MIGRATION_PATH.read_text()
    assert "os.environ" not in source and "os.getenv" not in source, (
        "migration 025 must not read env vars — the guard is unconditional"
    )


# ---------------------------------------------------------------------------
# The guard's SQL, against the live schema
# ---------------------------------------------------------------------------

async def _create_user(pool) -> str:
    row = await pool.fetchrow(
        "INSERT INTO users (email, password_hash) VALUES ($1, 'x') RETURNING user_id",
        make_test_email(),
    )
    return str(row["user_id"])


async def _create_block(pool, uid: str) -> tuple[str, int, str]:
    """A real doc → page → block chain. Returns (doc_id, block_id, token_id).

    Everything cascades from `users`, which the autouse cleanup fixture deletes,
    so there is no manual teardown.
    """
    doc_id = str(await pool.fetchval(
        """
        INSERT INTO book_documents (user_id, title, filename, file_path, language,
                                    source_type, status)
        VALUES ($1::uuid, 'Migration 025 Test', 't.pdf', '/tmp/t.pdf', 'de', 'pdf', 'ready')
        RETURNING doc_id
        """,
        uid,
    ))
    page_id = await pool.fetchval(
        """
        INSERT INTO book_pages (doc_id, page_number) VALUES ($1::uuid, 1)
        RETURNING page_id
        """,
        doc_id,
    )
    token_id = str(uuid.uuid4())
    tokens = [{"token_id": token_id, "text": "Hund", "is_word": True}]
    block_id = await pool.fetchval(
        """
        INSERT INTO book_blocks (page_id, doc_id, block_index, clean_text, tokens)
        VALUES ($1, $2::uuid, 0, 'Hund', $3::jsonb)
        RETURNING block_id
        """,
        page_id, doc_id, json.dumps(tokens),
    )
    return doc_id, block_id, token_id


async def _save_selection(pool, uid: str, doc_id: str, anchors: list) -> None:
    await pool.execute(
        """
        INSERT INTO reading_selections
            (user_id, doc_id, canonical, surface_text, sentence_text, anchors)
        VALUES ($1::uuid, $2::uuid, 'hund', 'Hund', 'Der Hund läuft', $3::jsonb)
        """,
        uid, doc_id, json.dumps(anchors),
    )


async def test_dependent_anchors_sql_counts_only_token_id_anchors(db_pool):
    """The shipped predicate, run against the real schema, on known rows.

    Scoped to this test's user by APPENDING to the migration's own constant, so
    the predicate under test is the real string (not a copy that could drift)
    while the count stays deterministic under pytest-xdist — an unscoped count
    moves whenever another worker inserts.
    """
    uid = await _create_user(db_pool)
    doc_id, block_id, token_id = await _create_block(db_pool, uid)

    # Depends on the column: anchors a real token_id in a real block.
    await _save_selection(
        db_pool, uid, doc_id,
        [{"block_id": block_id, "token_id": token_id, "surface": "Hund"}],
    )
    # Pre-025 shape: no token_id key at all (routers/reading.py falls back to
    # "legacy" for these). Must NOT block a downgrade — it never depended on
    # the column.
    await _save_selection(
        db_pool, uid, doc_id,
        [{"block_id": block_id, "surface": "Hund"}],
    )
    # Explicit JSON null must be treated like a missing key.
    await _save_selection(
        db_pool, uid, doc_id,
        [{"block_id": block_id, "token_id": None, "surface": "Hund"}],
    )
    # Empty anchors — the shape test_reading_progression inserts.
    await _save_selection(db_pool, uid, doc_id, [])

    scoped = mig._DEPENDENT_ANCHORS_SQL + " AND rs.user_id = $1::uuid"
    count = await db_pool.fetchval(scoped, uid)

    assert count == 1, (
        "exactly the one selection anchoring a real token_id may block the "
        f"downgrade; legacy/null/empty anchors must not. Got {count}"
    )


async def test_dependent_anchors_sql_runs_against_the_live_schema(db_pool):
    """Execute the constant exactly as `downgrade()` does — no scoping.

    Catches the failure mode the scoped test can't: a guard whose SQL is a
    syntax error or names a dropped column is worse than no guard, because it
    turns a refusal into a crash mid-migration.
    """
    count = await db_pool.fetchval(mig._DEPENDENT_ANCHORS_SQL)

    assert isinstance(count, int) and count >= 0


async def test_multi_token_selection_counts_once(db_pool):
    """A selection spanning several tokens is one dependent row, not several.

    `reading_selections` is multi-token by design, so the predicate has to be
    EXISTS-shaped. Written as a join against `jsonb_array_elements` it would
    emit one row per anchor and the refusal message would quote an inflated
    number — telling the operator to expect more data loss than is real.
    """
    uid = await _create_user(db_pool)
    doc_id, block_id, token_id = await _create_block(db_pool, uid)

    await _save_selection(
        db_pool, uid, doc_id,
        [
            {"block_id": block_id, "token_id": token_id, "surface": "Der"},
            {"block_id": block_id, "token_id": str(uuid.uuid4()), "surface": "Hund"},
            {"block_id": block_id, "token_id": str(uuid.uuid4()), "surface": "läuft"},
        ],
    )

    scoped = mig._DEPENDENT_ANCHORS_SQL + " AND rs.user_id = $1::uuid"
    count = await db_pool.fetchval(scoped, uid)

    assert count == 1, (
        f"one selection with three anchors must count once, got {count}"
    )
