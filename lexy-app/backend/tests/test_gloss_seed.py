"""
Curated gloss-cache seeding — services/gloss_seed_service.py (TODO #43 step 2).

Driven by `scripts/seed_gloss_cache.py`; the logic lives in the service so it
is testable without a subprocess (same split as `word_seed_service` /
`backfill_word_catalog.py`).

The load-bearing property is **key compatibility**: a seeded row is only worth
anything if `llm_service.translate_item_gloss` computes the same cache key. The
seeder therefore mirrors that function's params exactly, including `.lower()`
on the text rather than `text_norm.normalize_key`. Several tests below assert
the two agree rather than asserting the seeder's key in isolation — a key that
is self-consistently wrong would pass the weaker check.

`llm_cache` is reaped per test by the autouse fixture only for rows carrying
the `zztest-model-*` tag (see `_cache_helper`). Curated rows use the
`curated:words_4000_old` sentinel, which that pattern deliberately does NOT
match, so these tests clean up their own writes explicitly.
"""
import uuid

import pytest

from backend.services import gloss_seed_service as gss
from backend.services import llm_cache_service, llm_service
from backend.services.gloss_seed_service import (
    CURATED_MODEL,
    SKIP_ARTEFACT,
    SKIP_LONG,
    SKIP_MULTISENSE,
    build_candidates,
    classify_translation,
    curated_cache_key,
    read_rows,
    strip_article,
)


def _letters(n: int = 10) -> str:
    return "".join(chr(ord("a") + int(c, 16)) for c in uuid.uuid4().hex[:n])


@pytest.fixture
async def curated_rows(db_pool):
    """Reap ONLY the curated rows this test created.

    The autouse `llm_cache` cleanup cannot help here: it matches
    `zztest-model-%`, and the curated sentinel is deliberately excluded from
    that pattern so a real seeded row is never reaped.

    Registration is explicit — `curated_rows(word)` — rather than a blanket
    `DELETE WHERE model = CURATED_MODEL`. That blanket form was the first
    version of this fixture and it **wiped all 3,622 production curated rows**
    on the first full-suite run after seeding: every test using it deleted the
    entire seed, not just its own writes. Same shape as `tracked_words`, and
    parallel-safe for the same reason — a worker can only ever delete keys it
    computed itself.
    """
    keys: list[str] = []

    def _track(*texts: str) -> None:
        keys.extend(curated_cache_key(t) for t in texts)

    yield _track

    if keys:
        await db_pool.execute(
            "DELETE FROM llm_cache WHERE cache_key = ANY($1::text[])", keys,
        )


def _source(tmp_path, *rows: tuple[str, str]):
    """A minimal 11-column TSV; only columns 1 and 3 carry meaning here."""
    src = tmp_path / "words.txt"
    src.write_text(
        "".join(f"{h}\t\t{t}\t" + "\t" * 7 + "\n" for h, t in rows),
        encoding="utf-8",
    )
    return src


# ---------------------------------------------------------------------------
# Parser — pure
# ---------------------------------------------------------------------------


def test_read_rows_takes_headword_and_translation_columns(tmp_path):
    """Column 1 and column 3. The other 9 are enrichment this seeder ignores."""
    src = _source(tmp_path, ("werden", "to become, get"), ("ich", "I"))

    assert read_rows(src) == [("werden", "to become, get"), ("ich", "I")]


def test_read_rows_skips_blank_and_malformed_lines(tmp_path):
    src = tmp_path / "words.txt"
    src.write_text("ein\t\ta\t\n\nzu\t\tto\t\nbroken\n", encoding="utf-8")

    assert read_rows(src) == [("ein", "a"), ("zu", "to")]


@pytest.mark.parametrize(
    "headword,expected",
    [("das Haus", "Haus"), ("der Mann", "Mann"), ("die Frau", "Frau"),
     ("Das Haus", "Haus"), ("  der Mann  ", "Mann"), ("gehen", "gehen"),
     ("derartig", "derartig"), ("dieser", "dieser")],
)
def test_strip_article(headword, expected):
    assert strip_article(headword) == expected


# ---------------------------------------------------------------------------
# Gloss shape — pure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("t", ["I", "in the", "to become, get", "on, at, in"])
def test_short_translations_are_seedable(t):
    assert classify_translation(t) is None


def test_multisense_translation_is_skipped():
    """`1) a; 2) one (of)` is two glosses in one cell — not a gloss.

    Left to the LLM rather than guessed at; picking sense 1 would silently
    drop meaning.
    """
    assert classify_translation("1) a; 2) one (of)") == SKIP_MULTISENSE


def test_long_translation_is_skipped():
    assert classify_translation("a very long definition that reads like prose") == SKIP_LONG


def test_empty_translation_is_skipped():
    assert classify_translation("   ") is not None


def test_boundary_is_four_words():
    """The prompt asks for 1-4 words; 4 is in, 5 is out."""
    assert classify_translation("one two three four") is None
    assert classify_translation("one two three four five") == SKIP_LONG


# ---------------------------------------------------------------------------
# Candidate building — pure
# ---------------------------------------------------------------------------


def test_artefact_cells_are_skipped():
    """`der, die, das` is several entries crammed into one cell."""
    candidates, skipped = build_candidates(
        [("der, die, das", "the"), ("all, alle", "all"), ("Haus", "house")],
    )

    assert list(candidates) == ["haus"]
    assert skipped[SKIP_ARTEFACT] == ["der, die, das", "all, alle"]


def test_candidates_are_keyed_by_lowercased_surface():
    """The key text must be `.lower()` — what translate_item_gloss hashes."""
    candidates, _ = build_candidates([("das Haus", "house")])

    assert candidates == {"haus": "house"}


def test_first_spelling_wins_for_case_duplicates():
    candidates, _ = build_candidates([("Haus", "house"), ("haus", "building")])

    assert candidates == {"haus": "house"}


def test_article_nouns_are_keyed_bare():
    """`das Öl` glosses the word `Öl` — `word_table` stores the bare surface."""
    candidates, _ = build_candidates([("das Öl", "oil")])

    assert candidates == {"öl": "oil"}


# ---------------------------------------------------------------------------
# Cache-key compatibility — the whole design rests on this
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["Öl", "öl", "ÖL", "Übung", "Änderung", "Haus", "straße"])
def test_curated_key_matches_what_translate_item_gloss_computes(text):
    """Asserted against the consumer, not in isolation.

    A key that is self-consistently wrong would pass a weaker check; this pins
    that the seeder and the reader agree, including on `.lower()` vs
    `normalize_key` for umlauts.
    """
    expected = llm_cache_service.make_cache_key(
        "item_gloss", CURATED_MODEL,
        {"text": text.lower(), "item_type": "word", "language": "de"},
    )

    assert curated_cache_key(text) == expected


def test_umlaut_case_variants_share_one_curated_key():
    assert curated_cache_key("Öl") == curated_cache_key("öl") == curated_cache_key("ÖL")


def test_curated_model_is_not_a_real_model_and_not_reapable_by_tests():
    """The sentinel must survive the autouse `llm_cache` cleanup."""
    from ._cache_helper import cleanup_pattern

    assert CURATED_MODEL == "curated:words_4000_old"
    assert not CURATED_MODEL.startswith("claude")
    assert not CURATED_MODEL.startswith("zztest-model")
    # The literal check the cleanup fixture performs.
    assert not CURATED_MODEL.startswith(cleanup_pattern().rstrip("%"))


# ---------------------------------------------------------------------------
# DB-backed seeding
# ---------------------------------------------------------------------------


async def test_dry_run_writes_nothing(db_pool, tmp_path, tracked_words, curated_rows):
    word = f"Zzgloss{_letters()}"
    await _owned_word(db_pool, tracked_words, word)
    curated_rows(word)
    src = _source(tmp_path, (word, "a test gloss"))

    result = await gss.seed_glosses(db_pool, apply=False, source=src)

    assert result["dry_run"] is True
    assert result["missing"] == 1
    assert result["inserted"] == 0
    assert await db_pool.fetchval(
        "SELECT count(*) FROM llm_cache WHERE cache_key = $1", curated_cache_key(word),
    ) == 0


async def test_apply_inserts_curated_rows(db_pool, tmp_path, tracked_words, curated_rows):
    word = f"Zzgloss{_letters()}"
    await _owned_word(db_pool, tracked_words, word)
    curated_rows(word)
    src = _source(tmp_path, (word, "a test gloss"))

    result = await gss.seed_glosses(db_pool, apply=True, source=src)

    assert result["inserted"] == 1
    row = await db_pool.fetchrow(
        "SELECT prompt_key, model, response, expires_at FROM llm_cache WHERE cache_key = $1",
        curated_cache_key(word),
    )
    assert row["prompt_key"] == "item_gloss"
    assert row["model"] == CURATED_MODEL
    assert row["response"] == '{"gloss": "a test gloss"}'
    assert row["expires_at"] is None, "curated glosses are permanent"


async def test_apply_is_idempotent(db_pool, tmp_path, tracked_words, curated_rows):
    word = f"Zzgloss{_letters()}"
    await _owned_word(db_pool, tracked_words, word)
    curated_rows(word)
    src = _source(tmp_path, (word, "a test gloss"))

    first = await gss.seed_glosses(db_pool, apply=True, source=src)
    second = await gss.seed_glosses(db_pool, apply=True, source=src)

    assert first["inserted"] == 1
    assert second["missing"] == 0
    assert second["inserted"] == 0
    assert second["already_cached"] == 1


async def test_inserted_count_reflects_only_new_rows(
    db_pool, tmp_path, tracked_words, curated_rows,
):
    """A pre-existing row must not be counted as inserted.

    The over-reporting bug the catalog backfill shipped (0dd7075): counting
    matching keys after the INSERT includes rows that were already there.
    """
    existing = f"Zzgloss{_letters()}"
    fresh = f"Zzgloss{_letters()}"
    await _owned_word(db_pool, tracked_words, existing)
    await _owned_word(db_pool, tracked_words, fresh)
    curated_rows(existing, fresh)
    await gss.seed_glosses(db_pool, apply=True, source=_source(tmp_path, (existing, "one")))

    result = await gss.seed_glosses(
        db_pool, apply=True,
        source=_source(tmp_path, (existing, "one"), (fresh, "two")),
    )

    assert result["already_cached"] == 1
    assert result["inserted"] == 1, "pre-existing row must not count as inserted"


async def test_surfaces_absent_from_the_catalog_are_not_seeded(
    db_pool, tmp_path, curated_rows,
):
    """A key nothing ever looks up is dead weight."""
    word = f"Zzabsent{_letters()}"
    curated_rows(word)
    src = _source(tmp_path, (word, "a gloss"))

    result = await gss.seed_glosses(db_pool, apply=True, source=src)

    assert result["not_in_catalog"] == 1
    assert result["inserted"] == 0


async def test_ambiguous_catalog_surface_is_still_seedable(
    db_pool, tmp_path, tracked_words, curated_rows,
):
    """Ambiguity is a catalog concern; the gloss key is the TEXT, not item_id.

    Two `word_table` rows sharing a surface produce one cache key, so the
    surface gets exactly one gloss — no skip, no duplicate.
    """
    word = f"Zzamb{_letters()}"
    await _owned_word(db_pool, tracked_words, word, pos="NOUN")
    await _owned_word(db_pool, tracked_words, word, pos="VERB")
    curated_rows(word)
    src = _source(tmp_path, (word, "ambiguous gloss"))

    result = await gss.seed_glosses(db_pool, apply=True, source=src)

    assert result["inserted"] == 1
    # Scoped to THIS surface's key, not a global curated count — the real seed
    # has 3,622 rows, so a global assertion only passes on an empty table.
    assert await db_pool.fetchval(
        "SELECT count(*) FROM llm_cache WHERE cache_key = $1", curated_cache_key(word),
    ) == 1


async def test_umlaut_surface_is_seeded_under_the_lowercased_key(
    db_pool, tmp_path, tracked_words, curated_rows,
):
    word = f"Özzgloss{_letters()}"
    await _owned_word(db_pool, tracked_words, word)
    curated_rows(word)
    src = _source(tmp_path, (word, "oil"))

    await gss.seed_glosses(db_pool, apply=True, source=src)

    assert await db_pool.fetchval(
        "SELECT count(*) FROM llm_cache WHERE cache_key = $1", curated_cache_key(word.lower()),
    ) == 1


# ---------------------------------------------------------------------------
# Curated-first lookup in translate_item_gloss
# ---------------------------------------------------------------------------


class _ExplodingProvider:
    """Any provider call is a test failure — the curated row must be enough."""

    model_id = "zztest-model-should-not-be-used"

    async def structured(self, *a, **kw):
        raise AssertionError("provider called despite a curated gloss being present")


async def test_curated_gloss_is_served_without_calling_the_provider(
    db_pool, tmp_path, tracked_words, curated_rows, monkeypatch,
):
    word = f"Zzgloss{_letters()}"
    await _owned_word(db_pool, tracked_words, word)
    curated_rows(word)
    await gss.seed_glosses(db_pool, apply=True, source=_source(tmp_path, (word, "curated!")))

    monkeypatch.setattr(llm_service, "_MOCK", False)
    monkeypatch.setattr(llm_service, "_provider", _ExplodingProvider())

    gloss = await llm_service.translate_item_gloss(word, "word", "de", pool=db_pool)

    assert gloss == "curated!"


async def test_curated_gloss_survives_a_model_switch(
    db_pool, tmp_path, tracked_words, curated_rows, monkeypatch,
):
    """The reason the sentinel exists.

    `make_cache_key` includes the model, so a gloss cached under one model id
    misses after `LLM_MODEL` changes. The curated key is model-independent, so
    it still hits — while genuine LLM output stays correctly model-scoped.
    """
    word = f"Zzgloss{_letters()}"
    await _owned_word(db_pool, tracked_words, word)
    curated_rows(word)
    await gss.seed_glosses(db_pool, apply=True, source=_source(tmp_path, (word, "stable")))

    monkeypatch.setattr(llm_service, "_MOCK", False)
    for model in ("zztest-model-a", "zztest-model-b"):
        class _P:
            model_id = model

            async def structured(self, *a, **kw):
                raise AssertionError(f"provider called under {model}")

        monkeypatch.setattr(llm_service, "_provider", _P())
        assert await llm_service.translate_item_gloss(
            word, "word", "de", pool=db_pool,
        ) == "stable"


async def test_uncurated_word_still_falls_back_to_the_model_cache(
    db_pool, tracked_words, curated_rows, monkeypatch,
):
    """No curated row → unchanged behaviour: compute once, cache, reuse."""
    from ._cache_helper import test_model

    word = f"Zzuncurated{_letters()}"
    curated_rows(word)
    calls = {"n": 0}

    class _Counting:
        model_id = test_model()

        async def structured(self, *a, **kw):
            calls["n"] += 1
            return {"gloss": "computed"}

    monkeypatch.setattr(llm_service, "_MOCK", False)
    monkeypatch.setattr(llm_service, "_provider", _Counting())

    first = await llm_service.translate_item_gloss(word, "word", "de", pool=db_pool)
    second = await llm_service.translate_item_gloss(word, "word", "de", pool=db_pool)

    assert first == second == "computed"
    assert calls["n"] == 1, "second call must hit the model-specific cache"


async def test_mock_llm_behaviour_is_unchanged(db_pool, monkeypatch):
    """MOCK short-circuits BEFORE the curated lookup, exactly as before.

    Preserved deliberately: hundreds of tests rely on the `[gloss:…]` shape,
    and moving the curated read ahead of it would change what they see.
    """
    monkeypatch.setattr(llm_service, "_MOCK", True)

    assert await llm_service.translate_item_gloss(
        "Haus", "word", "de", pool=db_pool,
    ) == "[gloss:Haus]"


async def test_seeding_never_calls_the_provider(
    db_pool, tmp_path, tracked_words, curated_rows, monkeypatch,
):
    word = f"Zzgloss{_letters()}"
    await _owned_word(db_pool, tracked_words, word)
    curated_rows(word)
    monkeypatch.setattr(llm_service, "_MOCK", False)
    monkeypatch.setattr(llm_service, "_provider", _ExplodingProvider())

    result = await gss.seed_glosses(
        db_pool, apply=True, source=_source(tmp_path, (word, "no llm here")),
    )

    assert result["inserted"] == 1


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _owned_word(db_pool, tracked: list[int], word: str, *,
                      pos: str = "", language: str = "de") -> int:
    wid = await db_pool.fetchval(
        "INSERT INTO word_table (word, language, pos, tag, lemma) "
        "VALUES ($1, $2, $3, '', $1) RETURNING word_id",
        word, language, pos,
    )
    tracked.append(wid)
    return wid
