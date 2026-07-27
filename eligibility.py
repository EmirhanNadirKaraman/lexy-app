"""
eligibility.py
--------------
Core i+1 eligibility logic for the German language-learning pipeline.

An utterance is eligible as a learning exposure for target unit T when all
three conditions hold:

  1. T appears in the utterance's extracted learning units.
  2. T is not yet known by the user (below the knowledge threshold).
  3. Every other unit in the utterance IS already known by the user.

Condition 3 is the i+1 constraint (Krashen): exactly one unknown per
utterance.  The known units act as scaffolding; the single unknown unit is
the acquisition target the utterance is designed to teach.

Relationship to existing modules
---------------------------------
  - Units come from UtteranceUnitExtractor (utterance_unit_extractor.py).
  - Knowledge state is queried via the KnowledgeSource protocol, satisfied
    by UserKnowledgeStore (user_knowledge.py).
  - EligibilityDecision can be combined with UtteranceExtractionResult and
    CandidateUtterance by callers that need the full picture (e.g. pipeline.py).

The evaluator itself is stateless and makes only read calls — no side effects.
Recording that an exposure occurred is the caller's responsibility.

Edge cases
----------
Repeated occurrences of the target:
    The evaluator operates on the *deduplicated* unit list from
    UtteranceUnitExtractor.  A word that appears three times is still a single
    LearningUnit entry.  Repetitions do not affect eligibility.

Target unit type mismatch:
    Two LearningUnits with the same key but different unit_types (e.g.
    LEMMA vs PHRASE) are treated as distinct units.  A target of type PHRASE
    will not match a LEMMA unit with the same surface key.

Empty unit list:
    An utterance with no extractable units (pure punctuation, numbers, etc.)
    is always ineligible.  The evaluator returns NO_LEARNABLE_UNITS before
    any knowledge lookups.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol, runtime_checkable

from learning_units import LearningUnit


# ---------------------------------------------------------------------------
# Knowledge source interface
# ---------------------------------------------------------------------------

@runtime_checkable
class KnowledgeSource(Protocol):
    """
    Minimal read interface required by UtteranceEligibilityEvaluator.

    Any object that implements is_known() satisfies this protocol —
    including UserKnowledgeStore, in-memory test doubles, and future
    database-backed implementations.

    The what-counts-as-known threshold is an internal concern of the
    implementation (e.g. KnowledgeFilterPolicy in UserKnowledgeStore).
    This protocol only exposes the boolean result.
    """

    def is_known(self, user_id: str, unit: LearningUnit) -> bool:
        """Return True if `unit` meets the known threshold for `user_id`."""
        ...


# ---------------------------------------------------------------------------
# Ineligibility reasons
# ---------------------------------------------------------------------------

class IneligibilityReason(str, Enum):
    """
    Enumerated reasons why an utterance is not eligible for a target unit.

    Checks are applied in priority order — the first failure is reported.
    Using (str, Enum) makes values JSON-serialisable and readable in logs
    without calling .value explicitly.

    Priority order:
        NO_LEARNABLE_UNITS > TARGET_NOT_IN_UTTERANCE >
        TARGET_ALREADY_KNOWN > OTHER_UNKNOWNS_PRESENT
    """

    NO_LEARNABLE_UNITS = "no_learnable_units"
    """The utterance has no extracted learning units at all."""

    TARGET_NOT_IN_UTTERANCE = "target_not_in_utterance"
    """The target unit does not appear in the utterance's unit list."""

    TARGET_ALREADY_KNOWN = "target_already_known"
    """The target is at or above the known threshold — it cannot be a new target."""

    OTHER_UNKNOWNS_PRESENT = "other_unknowns_present"
    """
    One or more units besides the target are also unknown, violating i+1.

    See EligibilityDecision.blocking_units for the specific offending units.
    """


# ---------------------------------------------------------------------------
# Decision dataclass
# ---------------------------------------------------------------------------

@dataclass
class EligibilityDecision:
    """
    The full result of evaluating whether an utterance is eligible for target T.

    Carries a complete partition of the utterance's units into known, unknown,
    and blocking categories so callers can display, log, and debug without
    re-querying the knowledge store.

    Attributes:
        eligible:
            True only when all three i+1 conditions hold:
              - target appears in the unit list
              - target is unknown to the user
              - every other unit in the list is known

        target_unit:
            The learning unit this decision was computed for.

        ineligibility_reason:
            The first failed condition (in priority order), or None when
            eligible is True.  Use failure_summary for a human-readable
            explanation.

        known_units:
            Units in the utterance that the user already knows, preserving
            their original order.  Does not include the target unit itself,
            even if the target is known (it is reported via target_unit +
            ineligibility_reason instead).

        unknown_units:
            All units in the utterance that the user does not know.
            Includes the target when it is unknown (the normal case for an
            eligible utterance).

        blocking_units:
            Subset of unknown_units that are NOT the target.
            Non-empty exactly when reason == OTHER_UNKNOWNS_PRESENT.
            These are the units that prevent this utterance from being i+1
            for the given target.  Resolving them (via other i+1 exposures)
            would eventually unlock this utterance.
    """
    eligible: bool
    target_unit: LearningUnit
    ineligibility_reason: Optional[IneligibilityReason] = None
    known_units: list[LearningUnit] = field(default_factory=list)
    unknown_units: list[LearningUnit] = field(default_factory=list)
    blocking_units: list[LearningUnit] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Derived properties
    # ------------------------------------------------------------------

    @property
    def failure_summary(self) -> str:
        """
        A human-readable one-line explanation of ineligibility.

        Returns an empty string when eligible is True.
        """
        if self.eligible:
            return ""
        r = self.ineligibility_reason
        if r == IneligibilityReason.NO_LEARNABLE_UNITS:
            return "No learnable units found in utterance."
        if r == IneligibilityReason.TARGET_NOT_IN_UTTERANCE:
            return f"Target '{self.target_unit.key}' is not in the utterance's unit list."
        if r == IneligibilityReason.TARGET_ALREADY_KNOWN:
            return (
                f"Target '{self.target_unit.key}' is already known — "
                "cannot be an acquisition target."
            )
        if r == IneligibilityReason.OTHER_UNKNOWNS_PRESENT:
            keys = [u.key for u in self.blocking_units]
            return (
                f"{len(self.blocking_units)} other unknown unit(s) besides "
                f"'{self.target_unit.key}': {keys}"
            )
        return "Ineligible (unrecognised reason)."

    def __repr__(self) -> str:
        status = "ELIGIBLE" if self.eligible else f"INELIGIBLE({self.ineligibility_reason})"
        return f"EligibilityDecision({status}, target={self.target_unit.key!r})"


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

class UtteranceEligibilityEvaluator:
    """
    Evaluates i+1 eligibility for a specific target unit given an utterance's
    learning units and a user's knowledge state.

    The evaluator is stateless beyond its reference to the knowledge source.
    All relevant state comes in via method arguments, making it straightforward
    to unit-test with simple mock knowledge sources.

    Args:
        knowledge_source:
            Any object satisfying the KnowledgeSource protocol.
            Typically UserKnowledgeStore; can be a test double.

    Usage::

        evaluator = UtteranceEligibilityEvaluator(knowledge_store)

        # Check a specific target
        decision = evaluator.evaluate(user_id, units, target)
        if decision.eligible:
            show_utterance_to_user(utterance, target)
            store.record_exposure(user_id, target)

        # Discover all eligible targets in one pass
        for decision in evaluator.find_eligible_targets(user_id, units):
            print(f"i+1 target: {decision.target_unit.key}")
    """

    def __init__(self, knowledge_source: KnowledgeSource) -> None:
        self.knowledge_source = knowledge_source

    # ------------------------------------------------------------------
    # Primary evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        user_id: str,
        units: list[LearningUnit],
        target: LearningUnit,
    ) -> EligibilityDecision:
        """
        Evaluate whether an utterance is eligible for target unit `target`.

        Checks are applied in priority order so the returned reason is always
        the most actionable one.

        Args:
            user_id: Identifier for the user whose knowledge to query.
            units:   Deduplicated learning units extracted from the utterance.
                     Typically UtteranceExtractionResult.units — do not pass
                     token_units (contains duplicates) here.
            target:  The candidate acquisition target.

        Returns:
            EligibilityDecision with eligible=True only when:
              - target is present in units
              - target is not yet known by the user
              - all other units are already known by the user
        """
        # Guard: empty unit list — no learning can happen
        if not units:
            return self._ineligible(
                target=target,
                reason=IneligibilityReason.NO_LEARNABLE_UNITS,
                known=[],
                unknown=[],
                blocking=[],
            )

        # Partition units into known / unknown in one pass over the store
        known, unknown = self._partition(user_id, units)

        # Check 1: target must be present
        if not self._unit_present(units, target):
            return self._ineligible(
                target=target,
                reason=IneligibilityReason.TARGET_NOT_IN_UTTERANCE,
                known=known,
                unknown=unknown,
                blocking=[],
            )

        # Check 2: target must be unknown
        if self.knowledge_source.is_known(user_id, target):
            return self._ineligible(
                target=target,
                reason=IneligibilityReason.TARGET_ALREADY_KNOWN,
                known=known,
                unknown=unknown,
                blocking=[],
            )

        # Check 3: no other unit may be unknown (i+1 constraint)
        blocking = self._blocking_units(unknown, target)
        if blocking:
            return self._ineligible(
                target=target,
                reason=IneligibilityReason.OTHER_UNKNOWNS_PRESENT,
                known=known,
                unknown=unknown,
                blocking=blocking,
            )

        # All checks passed
        return EligibilityDecision(
            eligible=True,
            target_unit=target,
            ineligibility_reason=None,
            known_units=known,
            unknown_units=unknown,   # contains only the target at this point
            blocking_units=[],
        )

    # ------------------------------------------------------------------
    # Batch / discovery helpers
    # ------------------------------------------------------------------

    def find_eligible_targets(
        self,
        user_id: str,
        units: list[LearningUnit],
    ) -> list[EligibilityDecision]:
        """
        Return EligibilityDecisions for every unit that is a valid i+1 target.

        Evaluates each unit in `units` as a candidate target and returns only
        the decisions where eligible=True.  Useful when you want to discover
        what the utterance can teach without pre-specifying a target.

        The result list has at most one entry in the well-formed case (the i+1
        constraint permits exactly one unknown), but the method collects all
        eligible decisions to expose anomalies in test data or edge cases.

        Args:
            user_id: The user to evaluate against.
            units:   Deduplicated learning units from the utterance.

        Returns:
            List of eligible EligibilityDecisions, one per eligible target.
            Empty list when the utterance has 0 or 2+ unknowns.
        """
        return [
            d
            for unit in units
            for d in [self.evaluate(user_id, units, unit)]
            if d.eligible
        ]

    def evaluate_all(
        self,
        user_id: str,
        units: list[LearningUnit],
    ) -> list[EligibilityDecision]:
        """
        Evaluate every unit in `units` as a potential target, returning all decisions.

        Unlike find_eligible_targets(), this returns both eligible and ineligible
        decisions.  Intended for debugging and diagnostics: shows why each unit
        is or is not a valid target for this utterance.

        Args:
            user_id: The user to evaluate against.
            units:   Deduplicated learning units from the utterance.

        Returns:
            One EligibilityDecision per unit, in the same order as `units`.
        """
        return [self.evaluate(user_id, units, unit) for unit in units]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _partition(
        self,
        user_id: str,
        units: list[LearningUnit],
    ) -> tuple[list[LearningUnit], list[LearningUnit]]:
        """
        Separate `units` into (known, unknown) with a single pass over the store.

        Preserves input order within each output list.  Batching the lookups
        here (rather than calling is_known per-check) reduces store round-trips
        from O(N * checks) to O(N).
        """
        known: list[LearningUnit] = []
        unknown: list[LearningUnit] = []
        for unit in units:
            (known if self.knowledge_source.is_known(user_id, unit) else unknown).append(unit)
        return known, unknown

    @staticmethod
    def _unit_present(units: list[LearningUnit], target: LearningUnit) -> bool:
        """
        Return True if `target` appears in `units` by (unit_type, key) identity.

        Compares on (unit_type, key) rather than object identity — two
        LearningUnit instances with the same type and key are the same unit.
        This matches the identity semantics of LearningUnit.__hash__.
        """
        target_id = (target.unit_type, target.key)
        return any((u.unit_type, u.key) == target_id for u in units)

    @staticmethod
    def _blocking_units(
        unknown: list[LearningUnit],
        target: LearningUnit,
    ) -> list[LearningUnit]:
        """
        Return unknown units that are NOT the target — the i+1 blockers.

        Each returned unit is an additional unknown that prevents this utterance
        from being i+1 for `target`.  Exposing the user to utterances where
        each of these units is the sole unknown will eventually unblock it.
        """
        target_id = (target.unit_type, target.key)
        return [u for u in unknown if (u.unit_type, u.key) != target_id]

    @staticmethod
    def _ineligible(
        target: LearningUnit,
        reason: IneligibilityReason,
        known: list[LearningUnit],
        unknown: list[LearningUnit],
        blocking: list[LearningUnit],
    ) -> EligibilityDecision:
        """Convenience factory for an ineligible EligibilityDecision."""
        return EligibilityDecision(
            eligible=False,
            target_unit=target,
            ineligibility_reason=reason,
            known_units=known,
            unknown_units=unknown,
            blocking_units=blocking,
        )


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _demo() -> None:
    """
    Demonstrates all ineligibility paths and the eligible path.

    Uses a simple dict-based knowledge stub so no spaCy model is required.
    """
    from learning_units import LearningUnit, LearningUnitType

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def lemma(key: str) -> LearningUnit:
        return LearningUnit(LearningUnitType.LEMMA, key, key)

    class DictKnowledgeSource:
        """Minimal test double: a set of (user_id, unit_key) pairs that are known."""
        def __init__(self, known_pairs: set[tuple[str, str]]) -> None:
            self._known = known_pairs

        def is_known(self, user_id: str, unit: LearningUnit) -> bool:
            return (user_id, unit.key) in self._known

    USER = "alice"

    # Alice knows: film, sein, wirklich — but not fantastisch
    known_set: set[tuple[str, str]] = {
        (USER, "film"),
        (USER, "sein"),
        (USER, "wirklich"),
        (USER, "kaufen"),
        (USER, "noch"),
        (USER, "danach"),
    }
    source = DictKnowledgeSource(known_set)
    evaluator = UtteranceEligibilityEvaluator(source)

    # Units for: "Der Film ist wirklich fantastisch."
    # (DET/PRON filtered out by extractor; content words only)
    units_s1 = [lemma("film"), lemma("sein"), lemma("wirklich"), lemma("fantastisch")]

    # Units for: "Danach kaufen wir noch Popcorn."
    units_s2 = [lemma("danach"), lemma("kaufen"), lemma("noch"), lemma("popcorn")]

    # Units for: "Popcorn schmeckt fantastisch mit Karamell."
    units_s3 = [lemma("popcorn"), lemma("schmecken"), lemma("fantastisch"), lemma("karamell")]

    # ------------------------------------------------------------------

    def print_decision(d: EligibilityDecision, label: str) -> None:
        print(f"\n  {label}")
        print(f"  {d!r}")
        if d.eligible:
            known_keys = [u.key for u in d.known_units]
            print(f"  known   : {known_keys}")
            print(f"  unknown : ['{d.target_unit.key}'] ← acquisition target")
        else:
            print(f"  reason  : {d.failure_summary}")

    # ------------------------------------------------------------------
    # 1. ELIGIBLE — one unknown, target present
    # ------------------------------------------------------------------
    print("─" * 62)
    print("  1. ELIGIBLE — exactly one unknown")
    print("─" * 62)

    d = evaluator.evaluate(USER, units_s1, lemma("fantastisch"))
    print_decision(d, "Target: 'fantastisch' in 'Der Film ist wirklich fantastisch.'")

    # ------------------------------------------------------------------
    # 2. INELIGIBLE — target not in utterance
    # ------------------------------------------------------------------
    print("\n" + "─" * 62)
    print("  2. INELIGIBLE — target not present")
    print("─" * 62)

    d = evaluator.evaluate(USER, units_s1, lemma("popcorn"))
    print_decision(d, "Target: 'popcorn' (not in sentence 1)")

    # ------------------------------------------------------------------
    # 3. INELIGIBLE — target already known
    # ------------------------------------------------------------------
    print("\n" + "─" * 62)
    print("  3. INELIGIBLE — target already known")
    print("─" * 62)

    d = evaluator.evaluate(USER, units_s1, lemma("film"))
    print_decision(d, "Target: 'film' (Alice already knows it)")

    # ------------------------------------------------------------------
    # 4. INELIGIBLE — multiple unknowns (i+1 violated)
    # ------------------------------------------------------------------
    print("\n" + "─" * 62)
    print("  4. INELIGIBLE — other unknowns present (i+2 or worse)")
    print("─" * 62)

    d = evaluator.evaluate(USER, units_s3, lemma("fantastisch"))
    print_decision(d, "Target: 'fantastisch' (but 'popcorn', 'schmecken', 'karamell' also unknown)")

    # ------------------------------------------------------------------
    # 5. INELIGIBLE — empty unit list
    # ------------------------------------------------------------------
    print("\n" + "─" * 62)
    print("  5. INELIGIBLE — no units")
    print("─" * 62)

    d = evaluator.evaluate(USER, [], lemma("fantastisch"))
    print_decision(d, "Target: 'fantastisch' in empty unit list")

    # ------------------------------------------------------------------
    # 6. find_eligible_targets() — auto-discover all i+1 targets
    # ------------------------------------------------------------------
    print("\n" + "─" * 62)
    print("  6. find_eligible_targets() — auto-discovery")
    print("─" * 62)

    for units, label in [
        (units_s1, "'Der Film ist wirklich fantastisch.'"),
        (units_s2, "'Danach kaufen wir noch Popcorn.'"),
        (units_s3, "'Popcorn schmeckt fantastisch mit Karamell.'"),
    ]:
        eligible = evaluator.find_eligible_targets(USER, units)
        keys = [d.target_unit.key for d in eligible]
        count = len(eligible)
        n_unknown = sum(1 for u in units if not source.is_known(USER, u))
        tag = "i+1" if count == 1 else (f"i+{n_unknown} — too many unknowns" if n_unknown > 1 else "0 unknowns")
        print(f"\n  {label}")
        print(f"  eligible targets: {keys}  ← {tag}")

    # ------------------------------------------------------------------
    # 7. evaluate_all() — full diagnostic for debugging
    # ------------------------------------------------------------------
    print("\n" + "─" * 62)
    print("  7. evaluate_all() — diagnostic view of each unit as target")
    print("─" * 62)

    print("\n  Sentence: 'Der Film ist wirklich fantastisch.'")
    print(f"  {'unit':<16} {'result':<12} {'detail'}")
    print(f"  {'─'*16} {'─'*12} {'─'*36}")
    for d in evaluator.evaluate_all(USER, units_s1):
        result = "ELIGIBLE" if d.eligible else "INELIGIBLE"
        detail = "" if d.eligible else d.failure_summary
        print(f"  {d.target_unit.key:<16} {result:<12} {detail}")


if __name__ == "__main__":
    _demo()
