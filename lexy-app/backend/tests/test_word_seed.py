"""
Catalog backfill from data/final_result.txt — services/word_seed_service.py.

Driven by `scripts/backfill_word_catalog.py`; the logic lives in the service
so it is testable without a subprocess (same split as
`srs_cleanup_service` / `cleanup_orphan_srs_cards.py`).

Fixture words use a `Zzseed…` prefix rather than the `_testword_` style used
elsewhere: a leading underscore is deliberately rejected by
`is_clean_candidate`, so an underscore-prefixed fixture would never become a
candidate and every DB test would silently pass with zero rows.

The load-bearing test here is
`test_existing_word_is_not_reinserted_case_insensitively`. `word_table` has
`UNIQUE (word, language, pos)` with `pos` in the key and every existing German
row carrying `pos = ''`, so an insert that differs only in case or POS does
NOT conflict — it creates a second row, and two rows for one surface is what
`word_list_service` reports as `ambiguous`. A careless backfill would turn
thousands of currently-resolvable words ambiguous.

Pure-function tests need no DB. The DB tests write to the shared `word_table`,
so every fixture word carries a uuid suffix and is reaped afterwards.
"""
import uuid

import pytest

from backend.services import word_seed_service as wss
from backend.services.word_seed_service import (
    SKIP_ARTEFACT,
    SKIP_MULTIWORD,
    build_candidates,
    is_clean_candidate,
    strip_article,
)


# ---------------------------------------------------------------------------
# Article stripping — pure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headword,expected",
    [
        ("das Haus", "Haus"),
        ("der Mann", "Mann"),
        ("die Frau", "Frau"),
        ("Das Haus", "Haus"),      # article casing is ignored
        ("DIE Frau", "Frau"),
        ("  der Mann  ", "Mann"),  # surrounding whitespace
    ],
)
def test_strip_article(headword, expected):
    assert strip_article(headword) == expected


@pytest.mark.parametrize("headword", ["gehen", "Haus", "derartig", "dasselbe", "dieser"])
def test_strip_article_leaves_non_article_surfaces_alone(headword):
    """`derartig` starts with 'der' but is not an article + noun."""
    assert strip_article(headword) == headword


# ---------------------------------------------------------------------------
# Candidate cleanliness — pure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("surface", ["Haus", "gehen", "größer", "Fußball", "T-Shirt"])
def test_clean_candidates_accepted(surface):
    assert is_clean_candidate(surface) is True


@pytest.mark.parametrize(
    "surface",
    [
        "Abgeordnete, die Abgeordnete",  # multi-entry cell
        "der, die, das",
        "all, alle",
        "sich setzen",                   # real vocabulary, but multi-word
        "ein paar",
        "",
    ],
)
def test_unclean_candidates_rejected(surface):
    assert is_clean_candidate(surface) is False


def test_skip_reasons_are_distinguished():
    """Multi-word entries are real vocabulary; comma cells are a data defect.

    Reporting them as one "dirty" bucket would misdescribe 37 working
    reflexive verbs that belong to `phrase_table`.
    """
    _cands, skipped = build_candidates(
        ["sich setzen", "ein paar", "der, die, das", "all, alle"],
    )
    assert skipped[SKIP_MULTIWORD] == ["sich setzen", "ein paar"]
    assert skipped[SKIP_ARTEFACT] == ["der, die, das", "all, alle"]


# ---------------------------------------------------------------------------
# Candidate building — pure
# ---------------------------------------------------------------------------


def test_build_candidates_strips_articles_and_drops_unclean():
    candidates, skipped = build_candidates(
        ["das Haus", "der Mann", "gehen", "sich setzen", "der, die, das"],
    )
    assert candidates == ["Haus", "Mann", "gehen"]
    assert len(skipped[SKIP_MULTIWORD]) == 1
    assert len(skipped[SKIP_ARTEFACT]) == 1


def test_build_candidates_dedupes_case_insensitively_first_spelling_wins():
    """Case-insensitive to match how `word_list_service` resolves.

    A case-sensitive dedup would emit both `Haus` and `haus`, and inserting
    both would fork the surface into two rows — i.e. make it ambiguous.
    """
    candidates, _ = build_candidates(["Haus", "haus", "das Haus", "HAUS"])
    assert candidates == ["Haus"]


def test_build_candidates_preserves_file_order():
    candidates, _ = build_candidates(["zebra", "das Haus", "apfel"])
    assert candidates == ["zebra", "Haus", "apfel"]


def test_read_headwords_ignores_column_one(tmp_path):
    """Column 1 holds phrase blueprints — they must never reach word_table."""
    src = tmp_path / "src.txt"
    src.write_text(
        "sagen\tjdm. (Dat) etw. (Akk) sagen\n"
        "das Haus\tdas Haus\n"
        "geben\tjdm. (Dat) etw. (Akk) geben\n",
        encoding="utf-8",
    )

    heads = wss.read_headwords(src)

    assert heads == ["sagen", "das Haus", "geben"]
    assert not any("(Dat)" in h for h in heads)


def test_blueprints_never_become_candidates(tmp_path):
    src = tmp_path / "src.txt"
    src.write_text("sagen\tjdm. (Dat) etw. (Akk) sagen\n", encoding="utf-8")

    candidates, _ = build_candidates(wss.read_headwords(src))

    assert candidates == ["sagen"]


# ---------------------------------------------------------------------------
# DB-backed: seed_word_catalog
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_words(db_pool):
    """Reap any word_table rows a test creates via the backfill."""
    created: list[str] = []

    def _track(*words: str) -> None:
        created.extend(words)

    yield _track

    if created:
        await db_pool.execute(
            "DELETE FROM word_table WHERE language='de' AND word = ANY($1::text[])",
            created,
        )


def _source(tmp_path, *headwords: str):
    """A minimal 2-column source file; column 1 is filler."""
    src = tmp_path / "final_result.txt"
    src.write_text(
        "".join(f"{h}\t{h}\n" for h in headwords), encoding="utf-8",
    )
    return src


async def test_dry_run_writes_nothing(db_pool, tmp_path, seeded_words):
    word = f"Zzseedword{uuid.uuid4().hex[:10]}"
    seeded_words(word)
    src = _source(tmp_path, word)

    result = await wss.seed_word_catalog(db_pool, apply=False, source=src)

    assert result["dry_run"] is True
    assert result["missing"] == 1
    assert result["inserted"] == 0
    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_table WHERE language='de' AND word=$1", word,
    ) == 0


async def test_apply_inserts_missing_rows(db_pool, tmp_path, seeded_words):
    word = f"Zzseedword{uuid.uuid4().hex[:10]}"
    seeded_words(word)
    src = _source(tmp_path, word)

    result = await wss.seed_word_catalog(db_pool, apply=True, source=src)

    assert result["dry_run"] is False
    assert result["inserted"] == 1
    row = await db_pool.fetchrow(
        "SELECT word, language, pos, tag, lemma, frequency FROM word_table "
        "WHERE language='de' AND word=$1", word,
    )
    assert row is not None
    assert row["pos"] == "", "a non-empty pos would fork existing rows"
    assert row["tag"] == ""
    assert row["lemma"] == word
    assert row["frequency"] == 0


async def test_apply_is_idempotent(db_pool, tmp_path, seeded_words):
    word = f"Zzseedword{uuid.uuid4().hex[:10]}"
    seeded_words(word)
    src = _source(tmp_path, word)

    first = await wss.seed_word_catalog(db_pool, apply=True, source=src)
    second = await wss.seed_word_catalog(db_pool, apply=True, source=src)

    assert first["inserted"] == 1
    assert second["missing"] == 0
    assert second["inserted"] == 0
    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_table WHERE language='de' AND word=$1", word,
    ) == 1


async def test_existing_word_is_not_reinserted_case_insensitively(
    db_pool, tmp_path, seeded_words,
):
    """The anti-fork guard — the whole reason this backfill is insert-only.

    `word_table`'s unique key includes `pos`, so a differently-cased insert
    would not conflict; it would create a second row and make the surface
    `ambiguous` in vocabulary lists.
    """
    existing = f"Zzseedword{uuid.uuid4().hex[:10]}"
    seeded_words(existing)
    await db_pool.execute(
        "INSERT INTO word_table (word, language, pos, tag, lemma) "
        "VALUES ($1, 'de', '', '', $1)", existing,
    )
    src = _source(tmp_path, existing.upper())

    result = await wss.seed_word_catalog(db_pool, apply=True, source=src)

    assert result["already_present"] == 1
    assert result["missing"] == 0
    assert result["inserted"] == 0
    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_table WHERE language='de' AND lower(word)=lower($1)",
        existing,
    ) == 1, "case variant must not fork the surface into two rows"


async def test_article_nouns_are_seeded_bare(db_pool, tmp_path, seeded_words):
    noun = f"Zzseednoun{uuid.uuid4().hex[:10]}"
    seeded_words(noun)
    src = _source(tmp_path, f"das {noun}")

    await wss.seed_word_catalog(db_pool, apply=True, source=src)

    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_table WHERE language='de' AND word=$1", noun,
    ) == 1
    assert await db_pool.fetchval(
        "SELECT count(*) FROM word_table WHERE language='de' AND word=$1", f"das {noun}",
    ) == 0, "the article form belongs to phrase_table, not word_table"


async def test_unclean_entries_are_never_inserted(db_pool, tmp_path):
    src = _source(tmp_path, "der, die, das", "sich setzen")

    result = await wss.seed_word_catalog(db_pool, apply=True, source=src)

    assert result["candidates"] == 0
    assert result["inserted"] == 0
    assert result["skipped_artefact"] == 1
    assert result["skipped_multiword"] == 1


async def test_language_scoped(db_pool, tmp_path, seeded_words):
    """An existing Spanish row must not satisfy a German candidate."""
    word = f"Zzseedword{uuid.uuid4().hex[:10]}"
    await db_pool.execute(
        "INSERT INTO word_table (word, language, pos, tag, lemma) "
        "VALUES ($1, 'es', '', '', $1)", word,
    )
    seeded_words(word)
    src = _source(tmp_path, word)

    try:
        result = await wss.seed_word_catalog(db_pool, apply=True, source=src)
        assert result["missing"] == 1
        assert result["inserted"] == 1
    finally:
        await db_pool.execute(
            "DELETE FROM word_table WHERE language='es' AND word=$1", word,
        )


# ---------------------------------------------------------------------------
# Regression: the point of the whole backfill
# ---------------------------------------------------------------------------


async def test_seeded_word_resolves_unknown_in_a_vocabulary_list(
    client, db_pool, tmp_path, seeded_words,
):
    """Before seeding the surface is `unresolved`; after, it is `unknown`.

    This is what the backfill is for — and it also proves the seeded row is a
    single clean row, since two would report `ambiguous` instead.
    """
    from ._email_helper import make_test_email

    word = f"Zzseedword{uuid.uuid4().hex[:10]}"
    seeded_words(word)

    email = make_test_email()
    await client.post("/api/v1/auth/register", json={"email": email, "password": "password123"})
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    before = await client.post(
        "/api/v1/word-lists",
        json={"name": "before", "language": "de", "words": [word]},
        headers=headers,
    )
    assert before.json()["entries"][0]["status"] == "unresolved"

    await wss.seed_word_catalog(db_pool, apply=True, source=_source(tmp_path, word))

    after = await client.post(
        "/api/v1/word-lists",
        json={"name": "after", "language": "de", "words": [word]},
        headers=headers,
    )
    entry = after.json()["entries"][0]
    assert entry["status"] == "unknown", f"expected unknown, got {entry['status']}"
    assert entry["item_type"] == "word"
    assert entry["item_id"] is not None
