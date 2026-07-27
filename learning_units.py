"""
learning_units.py
-----------------
Defines what a "learning unit" is in this system and how to extract,
normalise, and compare units against a user's knowledge profile.

Design philosophy
-----------------
A learning unit is the smallest meaningful item a user can learn from
an utterance. For MVP the only unit type is LEMMA — one canonical
dictionary headword. A second type, PHRASE, is scaffolded for multi-word
expressions but left empty until Phase 2.

This single-type MVP is deliberately narrow because:
  - Lemma-based matching is what spaCy gives us for free.
  - The i+1 filter ("exactly one unknown unit") works best with a stable,
    deduplicated unit set. Adding constructions or grammar concepts too early
    makes that count unstable.
  - The data model is open for extension: adding a new LearningUnitType and
    a corresponding extractor method does not break existing unit records.

i+1 filter contract
--------------------
An utterance qualifies as a learnable exposure for a user when:

    count_unknown(units_in_utterance, user.known_keys) == 1

The one unknown unit is the item the user is expected to acquire from
that exposure. UserKnowledgeProfile.find_sole_unknown() returns it directly.

Phase 2 extension points (do not implement yet)
------------------------------------------------
  LearningUnitType.CONSTRUCTION  — "warten auf + Akk", "es geht um"
  LearningUnitType.GRAMMAR       — dative case, passive voice rule
  Frequency tiers                — auto-mark very high-frequency lemmas as
                                   "seen" for new users so i+1 becomes useful
                                   sooner
  Confidence scores              — replace binary known/unknown with a float
                                   (maps to SRS ease factor)
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from spacy.language import Language
from spacy.tokens import Doc, Token


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class LearningUnitType(str, Enum):
    """
    The kind of item a learning unit represents.

    LEMMA   — a single dictionary headword, the MVP unit type.
              key examples: "gehen", "haus", "schön"

    PHRASE  — a fixed multi-word expression learned as one unit.
              key examples: "auf jeden fall", "es tut mir leid"
              Scaffolded here so phrase records can be stored with the same
              schema; the extractor does not populate these in MVP.
    """
    LEMMA = "lemma"
    PHRASE = "phrase"


# ---------------------------------------------------------------------------
# Core data models
# ---------------------------------------------------------------------------

@dataclass
class LearningUnit:
    """
    A single item a user can learn.

    Attributes:
        unit_type:    The kind of unit (LEMMA or PHRASE).
        key:          Canonical lowercase identifier used for all comparisons
                      and database lookups.  Always lowercase regardless of
                      German noun capitalisation conventions, so that "Haus"
                      and "haus" resolve to the same unit.
        display_form: Human-facing form, preserving German capitalisation
                      conventions (nouns capitalised, verbs lowercase).
                      Derived from spaCy's token.lemma_.
        language:     BCP-47 language code.  Prevents key collisions if the
                      system ever supports multiple languages.

    Equality and hashing are based on (unit_type, key, language) so that
    LearningUnit objects can be used in sets and as dict keys.
    """
    unit_type: LearningUnitType
    key: str
    display_form: str
    language: str = "de"

    def __post_init__(self) -> None:
        # Enforce invariant: key is always lowercase
        self.key = self.key.lower()

    def __hash__(self) -> int:
        return hash((self.unit_type, self.key, self.language))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LearningUnit):
            return NotImplemented
        return (
            self.unit_type == other.unit_type
            and self.key == other.key
            and self.language == other.language
        )

    def __repr__(self) -> str:
        return f"LearningUnit({self.unit_type.value}:{self.key!r})"


@dataclass
class UserKnowledgeProfile:
    """
    A lightweight, query-optimised snapshot of one user's full knowledge state.

    Build this once per request from UserKnowledgeStore.build_profile() and
    pass it to the i+1 filter.  It does not own the authoritative state —
    it is a read-only projection for fast set operations.

    Attributes:
        user_id:    The user this profile belongs to.
        known_keys: Set of (unit_type, key) pairs the user currently knows.
                    Units absent from this set are considered unknown.
    """
    user_id: str
    known_keys: frozenset[tuple[LearningUnitType, str]]

    def is_known(self, unit: LearningUnit) -> bool:
        return (unit.unit_type, unit.key) in self.known_keys

    def unknown_units(self, units: list[LearningUnit]) -> list[LearningUnit]:
        """Return only the units the user does not yet know."""
        return [u for u in units if not self.is_known(u)]

    def find_sole_unknown(
        self,
        units: list[LearningUnit],
    ) -> Optional[LearningUnit]:
        """
        Return the one unknown unit if exactly one exists; otherwise None.

        This is the i+1 gate: call this on the units extracted from a
        candidate utterance.  A non-None return means the utterance is
        learnable for this user right now.
        """
        unknowns = self.unknown_units(units)
        return unknowns[0] if len(unknowns) == 1 else None


# ---------------------------------------------------------------------------
# German-specific extraction constants
# ---------------------------------------------------------------------------

# Tokens to exclude categorically from learning units.
# These are either grammatically vacuous or so high-frequency that treating
# them as learning units would saturate the i+1 filter for new users.
#
# Tradeoff: a true beginner might not know "ich" or "und", so excluding them
# means the filter cannot help them until they have acquired enough vocabulary
# that the remaining unknowns in most sentences drop to one.  The recommended
# mitigation is an onboarding step that auto-marks A1 function words as SEEN.
_SKIP_LEMMAS: frozenset[str] = frozenset({
    # Personal pronouns — closed class, learned implicitly in first hours
    "ich", "du", "er", "sie", "es", "wir", "ihr",
    # High-frequency particles that almost always appear as noise
    "ja", "nein", "halt", "mal", "doch", "schon", "noch",
})

# Minimum character length for a lemma to be included.
# Single-character tokens in German are almost always punctuation remnants
# or foreign-language particles, not learnable vocabulary items.
_MIN_LEMMA_LENGTH: int = 2


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class LearningUnitExtractor:
    """
    Extracts a deduplicated list of LearningUnit objects from text or a
    spaCy Doc, applying German-specific normalisation rules.

    Normalisation pipeline for each token:
      1.  Skip punctuation, whitespace, numbers, and tokens shorter than
          _MIN_LEMMA_LENGTH.
      2.  Skip tokens in _SKIP_LEMMAS.
      3.  If the token is a separable verb particle (dep_ == "svp"), skip it —
          it will be incorporated into the head verb's lemma in step 4.
      4.  If the token is a verb with one or more separable-verb-particle
          (svp) children, prepend the particle to the lemma:
              "fange" (dep=ROOT) + "an" (dep=svp)  →  key="anfangen"
          This is the single most important German-specific rule: without it
          "anfangen" and "fangen" would appear as the same unit.
      5.  Lowercase the lemma to produce the canonical key.
      6.  Preserve token.lemma_ (spaCy's form) as display_form, which retains
          German noun capitalisation ("Haus", not "haus").
      7.  Deduplicate: if the same (unit_type, key) appears more than once in
          the utterance, include it only once.  For i+1 counting, a repeated
          unknown word is still one unknown.

    Separable verb coverage:
      spaCy de_core_news_md (and _lg) labels separable verb particles with
      dep_ == "svp" and attaches them to the finite verb as a child.  This
      works reliably for present tense and simple past in main clauses:
          "Ich mache die Tür auf."   →  aufmachen
          "Er fängt an zu arbeiten." →  anfangen
      It is less reliable for:
          - Zu-infinitives far from the particle
          - Very long sentences with multiple clause boundaries
          - Informal/fragmentary subtitle text
      Document failures rather than adding fragile heuristics to fix them.

    Reflexive verbs:
      "sich" is included as a learning unit (key="sich", type=LEMMA).
      "sich freuen" and "freuen" are therefore two separate unknown units in
      a sentence like "Er freut sich".  This is intentional at MVP level —
      both the verb lemma and the reflexive particle are worth knowing.
      Phase 2 can introduce LearningUnitType.CONSTRUCTION to represent
      "sich freuen" as a unified slot.
    """

    def __init__(self, nlp: Language) -> None:
        self.nlp = nlp
        self._check_pipeline()

    def extract(self, text: str) -> list[LearningUnit]:
        """Parse `text` and return deduplicated learning units."""
        doc = self.nlp(text)
        return self.extract_from_doc(doc)

    def extract_from_doc(self, doc: Doc) -> list[LearningUnit]:
        """
        Extract learning units from an already-parsed spaCy Doc.

        Prefer this over extract() when you already have a Doc (e.g. from the
        segmentation stage) to avoid double-parsing.
        """
        seen: set[tuple[LearningUnitType, str]] = set()
        units: list[LearningUnit] = []

        for token in doc:
            unit = self._token_to_unit(token)
            if unit is None:
                continue
            identity = (unit.unit_type, unit.key)
            if identity not in seen:
                seen.add(identity)
                units.append(unit)

        return units

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _token_to_unit(self, token: Token) -> Optional[LearningUnit]:
        """
        Map one spaCy token to a LearningUnit, or return None to skip it.
        """
        # --- Skip uninformative tokens ---
        if self._should_skip(token):
            return None

        # --- Skip separable particles; they will be attached to the head verb ---
        if token.dep_ == "svp":
            return None

        lemma_raw = token.lemma_ or token.text  # blank model fallback: use surface form
        key = lemma_raw.lower()

        if not key or key in _SKIP_LEMMAS:
            return None

        # --- Attach separable particle to verb lemma ---
        svp_particles = [c for c in token.children if c.dep_ == "svp"]
        if svp_particles:
            # Take the first particle (a verb can technically have one svp)
            particle = svp_particles[0].text.lower()
            key = particle + key
            # Display: lowercase the verb part so "anfangen" not "anFangen"
            lemma_raw = particle + lemma_raw[0].lower() + lemma_raw[1:]

        return LearningUnit(
            unit_type=LearningUnitType.LEMMA,
            key=key,
            display_form=lemma_raw,
        )

    @staticmethod
    def _should_skip(token: Token) -> bool:
        """
        Return True for tokens that carry no learnable lexical content.

        Named entities (persons, locations, organisations) are skipped because
        they are not vocabulary items in the language-learning sense — surfacing
        a character name or city name as an i+1 target is meaningless and
        contaminates the match list.  spaCy's de_core_news_md uses CoNLL-2003
        labels: PER, LOC, ORG, MISC.
        """
        return (
            token.is_punct
            or token.is_space
            or token.is_digit
            or not any(c.isalpha() for c in token.text)
            or len(token.text.strip()) < _MIN_LEMMA_LENGTH
            or token.ent_type_ in {"PER", "LOC", "ORG", "MISC"}
        )

    def _check_pipeline(self) -> None:
        """Warn if the pipeline lacks a tagger, which is needed for lemmas."""
        if not (self.nlp.has_pipe("tagger") or self.nlp.has_pipe("morphologizer")):
            warnings.warn(
                "LearningUnitExtractor: no 'tagger' or 'morphologizer' found in the "
                "spaCy pipeline. token.lemma_ will fall back to the surface form, "
                "which means inflected forms won't be normalised. "
                "Use spacy.load('de_core_news_md') or larger for correct lemmatisation.",
                UserWarning,
                stacklevel=3,
            )


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _demo() -> None:
    """
    Demonstrates lemma extraction, separable verb handling, and i+1 filtering.

    Uses de_core_news_md for real lemmatisation. Falls back gracefully to
    spacy.blank("de") with a warning if the model is not installed.
    """
    try:
        import spacy
        nlp = spacy.load("de_core_news_md")
    except OSError:
        import spacy
        nlp = spacy.blank("de")
        warnings.warn("de_core_news_md not found — lemmatisation will be inaccurate.", stacklevel=1)

    extractor = LearningUnitExtractor(nlp)

    sentences = [
        "Ich fange morgen mit dem Training an.",
        "Sie macht die Tür auf und schaut hinaus.",
        "Er freut sich über das Geschenk.",
        "Wir haben das Buch gelesen.",
        "Das Haus am See gehört meiner Familie.",
    ]

    print("─" * 60)
    print("  LEMMA EXTRACTION (with separable verb handling)")
    print("─" * 60)
    for sent in sentences:
        units = extractor.extract(sent)
        keys = [u.display_form for u in units]
        print(f"\n  {sent}")
        print(f"  → {keys}")

    # --- i+1 filter demo ---
    print("\n" + "─" * 60)
    print("  i+1 FILTER DEMO")
    print("─" * 60)

    # Simulate a user who knows common words but not "anfangen" or "Training"
    profile = UserKnowledgeProfile(
        user_id="u1",
        known_keys=frozenset(
            (LearningUnitType.LEMMA, k)
            for k in [
                "morgen", "mit", "dem", "mein",
                "machen", "tür", "schauen", "hinausschauen",
                "aufmachen", "freuen", "sich", "über",
                "haben", "buch", "lesen", "haus", "see", "gehören",
                "familie",
            ]
        ),
    )

    target_sentence = "Ich fange morgen mit dem Training an."
    units = extractor.extract(target_sentence)
    target = profile.find_sole_unknown(units)

    print(f"\n  Sentence: {target_sentence!r}")
    print(f"  All units: {[u.display_form for u in units]}")
    known = [u.display_form for u in units if profile.is_known(u)]
    unknown = [u.display_form for u in units if not profile.is_known(u)]
    print(f"  Known:   {known}")
    print(f"  Unknown: {unknown}")
    if target:
        print(f"  → i+1 target: {target!r}  ✓ show this sentence to the user")
    else:
        print(f"  → {len(profile.unknown_units(units))} unknowns — not i+1, skip")

    # Sentence with two unknowns — should be skipped
    target_sentence_2 = "Das Haus am See gehört meiner Familie."
    units_2 = extractor.extract(target_sentence_2)
    target_2 = profile.find_sole_unknown(units_2)
    unknown_2 = [u.display_form for u in units_2 if not profile.is_known(u)]
    print(f"\n  Sentence: {target_sentence_2!r}")
    print(f"  Unknown: {unknown_2}")
    if target_2:
        print(f"  → i+1 target: {target_2!r}")
    else:
        print(f"  → {len(unknown_2)} unknowns — not i+1, skip")


if __name__ == "__main__":
    _demo()
