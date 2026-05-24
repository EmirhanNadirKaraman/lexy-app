"""Regression guards for the owned-word test fixtures (test isolation).

These lock in the contract the SRS/global-table isolation work depends on:
`make_word` (conftest fixture) and `insert_owned_word` (_word_helper) each
produce a fresh, uniquely-named row every call, so no test can collide with or
reap another's word. See docs/TESTS.md for the full rationale.
"""
from ._word_helper import insert_owned_word


async def test_make_word_is_unique_and_present(db_pool, make_word):
    w1, s1 = await make_word()
    w2, s2 = await make_word()

    assert w1 != w2, "make_word must return a distinct id each call"
    assert s1 != s2, "make_word surfaces must be unique"
    assert s1.startswith("_testword_") and s2.startswith("_testword_")

    present = await db_pool.fetchval(
        "SELECT count(*) FROM word_table WHERE word_id = ANY($1::int[])", [w1, w2]
    )
    assert present == 2, "both owned words should exist during the test"


async def test_insert_owned_word_is_unique(db_pool):
    w1, s1 = await insert_owned_word(db_pool)
    w2, s2 = await insert_owned_word(db_pool)

    assert w1 != w2 and s1 != s2
    assert s1.startswith("_testword_")


async def test_make_word_custom_surface_and_language(db_pool, make_word):
    wid, surface = await make_word("es", word="probaba_xyz")
    assert surface == "probaba_xyz"
    row = await db_pool.fetchrow(
        "SELECT language FROM word_table WHERE word_id = $1", wid
    )
    assert row["language"] == "es"
