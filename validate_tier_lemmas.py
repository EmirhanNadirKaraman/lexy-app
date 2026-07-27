"""
validate_tier_lemmas.py
-----------------------
Diagnostic script: verify that onboarding tier lemma keys match what
LearningUnitExtractor actually produces from spaCy.

A mismatch means the onboarding seeds a key that the extractor never
generates — the unit silently remains UNSEEN and doesn't protect against
false unknowns in the i+1 filter.

Usage:
    python validate_tier_lemmas.py                   # all tiers
    python validate_tier_lemmas.py --tier a1         # one tier only
    python validate_tier_lemmas.py --verbose         # show all results

Requires:
    pip install spacy
    python -m spacy download de_core_news_md

How the check works
-------------------
For each lemma in a tier, we tokenize the lemma string itself through
spaCy and compare token.lemma_.lower() against the stored key.

This covers:
  - Simple lemmas: "gehen" → spaCy lemma "gehen" ✓
  - Adjectives:    "schön" → spaCy lemma "schön" ✓
  - Nouns:         "haus"  → spaCy lemma "Haus" → .lower() "haus" ✓

It does NOT reliably cover separable verbs, because the particle-joining
logic in LearningUnitExtractor requires a real sentence with syntactic
context (dep_ == "svp").  Separable verbs are flagged separately for
manual review.

Typical separable-verb prefixes (requires manual sentence testing):
    an, auf, aus, ab, bei, durch, ein, fort, her, hin, los, mit, nach,
    vor, weg, weiter, zu, zurück, zusammen
"""
from __future__ import annotations

import argparse
import sys


# ---------------------------------------------------------------------------
# Known separable-verb prefixes
# ---------------------------------------------------------------------------

_SEPARABLE_PREFIXES: frozenset[str] = frozenset({
    "an", "auf", "aus", "ab", "bei", "durch", "ein", "fort",
    "her", "hin", "los", "mit", "nach", "vor", "weg", "weiter",
    "zu", "zurück", "zusammen",
})


def _looks_like_separable(lemma_key: str) -> bool:
    """Heuristic: does the key start with a known separable prefix?"""
    for prefix in _SEPARABLE_PREFIXES:
        if lemma_key.startswith(prefix) and len(lemma_key) > len(prefix) + 2:
            return True
    return False


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------

def validate_tier(tier_name: str, lemmas: frozenset[str], nlp) -> dict:
    """
    Check each lemma in the tier against spaCy's lemmatiser output.

    Returns a dict with:
        ok:         list of (key, spacy_lemma) pairs that matched
        mismatch:   list of (key, spacy_lemma) pairs that did not match
        separable:  list of keys that look like separable verbs (need manual check)
    """
    ok: list[tuple[str, str]] = []
    mismatch: list[tuple[str, str]] = []
    separable: list[str] = []

    for key in sorted(lemmas):
        if _looks_like_separable(key):
            separable.append(key)
            continue

        doc = nlp(key)
        if not doc:
            mismatch.append((key, "<empty doc>"))
            continue

        # Take the first non-punct token's lemma
        token = next((t for t in doc if not t.is_punct and not t.is_space), None)
        if token is None:
            mismatch.append((key, "<no token>"))
            continue

        spacy_lemma = token.lemma_.lower()
        if spacy_lemma == key:
            ok.append((key, spacy_lemma))
        else:
            mismatch.append((key, spacy_lemma))

    return {"ok": ok, "mismatch": mismatch, "separable": separable}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Validate onboarding tier lemmas against spaCy.")
    parser.add_argument("--tier", choices=["a1", "a2", "b1", "all"], default="all")
    parser.add_argument("--verbose", action="store_true", help="Show passing lemmas too")
    args = parser.parse_args()

    try:
        import spacy
        nlp = spacy.load("de_core_news_md")
    except OSError:
        print("ERROR: de_core_news_md not installed.")
        print("  python -m spacy download de_core_news_md")
        sys.exit(1)

    from onboarding import LevelTier, _TIER_DELTAS

    tier_map = {
        "a1": (LevelTier.A1,  _TIER_DELTAS[LevelTier.A1]),
        "a2": (LevelTier.A2,  _TIER_DELTAS[LevelTier.A2]),
        "b1": (LevelTier.B1,  _TIER_DELTAS[LevelTier.B1]),
    }
    to_check = list(tier_map.items()) if args.tier == "all" else [(args.tier, tier_map[args.tier])]

    total_ok = total_mismatch = total_separable = 0

    for tier_name, (tier_enum, delta_lemmas) in to_check:
        print(f"\n{'─' * 60}")
        print(f"  Tier: {tier_name.upper()}  ({len(delta_lemmas)} delta lemmas)")
        print(f"{'─' * 60}")

        results = validate_tier(tier_name, delta_lemmas, nlp)

        # Mismatches — always show
        if results["mismatch"]:
            print(f"\n  MISMATCHES ({len(results['mismatch'])}) — seeded key ≠ spaCy output:")
            for key, spacy_lemma in results["mismatch"]:
                print(f"    {key:<30}  →  spaCy: {spacy_lemma!r}")
        else:
            print("\n  Mismatches: none")

        # Separable verbs — always show (need manual verification)
        if results["separable"]:
            print(f"\n  SEPARABLE VERBS ({len(results['separable'])}) — manual sentence test required:")
            for key in results["separable"]:
                print(f"    {key}")

        # Passing lemmas — only in verbose mode
        if args.verbose and results["ok"]:
            print(f"\n  OK ({len(results['ok'])}):")
            for key, _ in results["ok"]:
                print(f"    {key}")

        total_ok        += len(results["ok"])
        total_mismatch  += len(results["mismatch"])
        total_separable += len(results["separable"])

    # Summary
    print(f"\n{'═' * 60}")
    print(f"  TOTAL  ok={total_ok}  mismatch={total_mismatch}  separable(manual)={total_separable}")

    # Cross-tier duplicate assertion
    print("\n  Cross-tier duplicate check:")
    a1 = _TIER_DELTAS[LevelTier.A1]
    a2 = _TIER_DELTAS[LevelTier.A2]
    b1 = _TIER_DELTAS[LevelTier.B1]
    overlaps_a1_a2 = a1 & a2
    overlaps_a1_b1 = a1 & b1
    overlaps_a2_b1 = a2 & b1
    if overlaps_a1_a2 or overlaps_a1_b1 or overlaps_a2_b1:
        print(f"    A1∩A2: {sorted(overlaps_a1_a2) or 'none'}")
        print(f"    A1∩B1: {sorted(overlaps_a1_b1) or 'none'}")
        print(f"    A2∩B1: {sorted(overlaps_a2_b1) or 'none'}")
        print("    WARNING: cross-tier duplicates found (frozenset union hides them at runtime)")
    else:
        print("    No cross-tier duplicates.")

    if total_mismatch > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
