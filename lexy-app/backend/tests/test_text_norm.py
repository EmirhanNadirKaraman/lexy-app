"""
Unicode comparison key for catalog lookups — services/text_norm.py.

Pure functions, no DB. The DB-backed consequences are in `test_word_lists.py`
(vocabulary resolution) and `test_word_seed.py` (backfill presence checks).

Background: the database runs under the C locale, where Postgres folds ASCII
only. `normalize_key` exists so the fold happens in Python instead. See the
module docstring for why `lower(col) = lower($1)` does not fix it.
"""
import pytest

from backend.services.text_norm import index_by_key, normalize_key


@pytest.mark.parametrize(
    "a,b",
    [
        ("Öl", "öl"),
        ("Öl", "ÖL"),
        ("Übung", "übung"),
        ("Änderung", "änderung"),
        ("die Änderung", "Die Änderung"),
        ("das Öl", "DAS ÖL"),
        ("Haus", "haus"),          # ASCII still works
        ("  Öl  ", "öl"),          # surrounding whitespace stripped
    ],
)
def test_case_variants_share_a_key(a, b):
    assert normalize_key(a) == normalize_key(b)


def test_decomposed_and_precomposed_umlauts_agree():
    """NFC, so `o` + U+0308 and precomposed `\u00f6` are the same key.

    A pasted list from an unnormalised source (macOS filenames, some PDFs)
    would otherwise never match a precomposed catalog row.
    """
    decomposed = "o\u0308l"
    precomposed = "\u00f6l"
    assert decomposed != precomposed, "the two literals must really differ"
    assert normalize_key(decomposed) == normalize_key(precomposed)


# ---------------------------------------------------------------------------
# The ß rule — the reason this uses lower(), not casefold()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sharp_s,double_s",
    [("schließen", "schliessen"), ("heißt", "heisst"), ("Großteil", "Grossteil")],
)
def test_sharp_s_and_double_s_are_different_keys(sharp_s, double_s):
    """`casefold()` would merge these; `lower()` must not.

    German `word_table` holds both spellings of 7 such pairs as separate rows
    (Swiss vs. standard orthography). Merging their keys would report both as
    `ambiguous` and break words that resolve cleanly today, so switching this
    helper to `casefold()` is a regression, not an improvement.
    """
    assert normalize_key(sharp_s) != normalize_key(double_s)


def test_sharp_s_is_still_case_insensitive_with_itself():
    assert normalize_key("Schließen") == normalize_key("schließen")


# ---------------------------------------------------------------------------
# index_by_key
# ---------------------------------------------------------------------------


def test_index_by_key_groups_case_variants_and_sorts_ids():
    rows = [
        {"s": "Öl", "id": 7},
        {"s": "öl", "id": 3},
        {"s": "Haus", "id": 5},
    ]
    assert index_by_key(rows, "s", "id") == {"öl": [3, 7], "haus": [5]}


def test_index_by_key_dedupes_repeated_ids():
    rows = [{"s": "Öl", "id": 7}, {"s": "ÖL", "id": 7}]
    assert index_by_key(rows, "s", "id") == {"öl": [7]}
