from __future__ import annotations

import warnings
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from spacy.language import Language
from spacy.tokens import Doc, Token


class LearningUnitType(str, Enum):
    """
    The kind of item a learning unit represents.

    LEMMA   — a single dictionary headword, the MVP unit type.
    PHRASE  — a fixed multi-word expression learned as one unit.
    """
    LEMMA = "lemma"
    PHRASE = "phrase"


@dataclass
class LearningUnit:
    """
    A single item a user can learn.

    Attributes:
        unit_type:    The kind of unit (LEMMA or PHRASE).
        key:          Canonical lowercase identifier used for all comparisons.
        display_form: Human-facing form, preserving German capitalisation.
        language:     BCP-47 language code.
    """
    unit_type: LearningUnitType
    key: str
    display_form: str
    language: str = "de"

    def __post_init__(self) -> None:
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

    Attributes:
        user_id:    The user this profile belongs to.
        known_keys: Set of (unit_type, key) pairs the user currently knows.
    """
    user_id: str
    known_keys: frozenset[tuple[LearningUnitType, str]]

    def is_known(self, unit: LearningUnit) -> bool:
        return (unit.unit_type, unit.key) in self.known_keys

    def unknown_units(self, units: list[LearningUnit]) -> list[LearningUnit]:
        return [u for u in units if not self.is_known(u)]

    def find_sole_unknown(self, units: list[LearningUnit]) -> Optional[LearningUnit]:
        """
        Return the one unknown unit if exactly one exists; otherwise None.
        This is the i+1 gate.
        """
        unknowns = self.unknown_units(units)
        return unknowns[0] if len(unknowns) == 1 else None


# Tokens to exclude categorically from learning units.
_SKIP_LEMMAS: frozenset[str] = frozenset({
    "ich", "du", "er", "sie", "es", "wir", "ihr",
    "ja", "nein", "halt", "mal", "doch", "schon", "noch",
})

# Minimum character length for a lemma to be included.
_MIN_LEMMA_LENGTH: int = 2


class LearningUnitExtractor:
    """
    Extracts a deduplicated list of LearningUnit objects from text or a
    spaCy Doc, applying German-specific normalisation rules.
    """

    def __init__(self, nlp: Language) -> None:
        self.nlp = nlp
        self._check_pipeline()

    def extract(self, text: str) -> list[LearningUnit]:
        """Parse `text` and return deduplicated learning units."""
        doc = self.nlp(text)
        return self.extract_from_doc(doc)

    def extract_from_doc(self, doc: Doc) -> list[LearningUnit]:
        """Extract learning units from an already-parsed spaCy Doc."""
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

    def _token_to_unit(self, token: Token) -> Optional[LearningUnit]:
        if self._should_skip(token):
            return None

        if token.dep_ == "svp":
            return None

        lemma_raw = token.lemma_ or token.text
        key = lemma_raw.lower()

        if not key or key in _SKIP_LEMMAS:
            return None

        svp_particles = [c for c in token.children if c.dep_ == "svp"]
        if svp_particles:
            particle = svp_particles[0].text.lower()
            key = particle + key
            lemma_raw = particle + lemma_raw[0].lower() + lemma_raw[1:]

        return LearningUnit(
            unit_type=LearningUnitType.LEMMA,
            key=key,
            display_form=lemma_raw,
        )

    @staticmethod
    def _should_skip(token: Token) -> bool:
        return (
            token.is_punct
            or token.is_space
            or token.is_digit
            or not any(c.isalpha() for c in token.text)
            or len(token.text.strip()) < _MIN_LEMMA_LENGTH
            or token.ent_type_ in {"PER", "LOC", "ORG", "MISC"}
        )

    def _check_pipeline(self) -> None:
        if not (self.nlp.has_pipe("tagger") or self.nlp.has_pipe("morphologizer")):
            warnings.warn(
                "LearningUnitExtractor: no 'tagger' or 'morphologizer' found in the "
                "spaCy pipeline. token.lemma_ will fall back to the surface form.",
                UserWarning,
                stacklevel=3,
            )
