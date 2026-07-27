"""
exposure_counter.py
-------------------
MVP qualified exposure counting system for the German language-learning pipeline.

A "qualified exposure" is a single event in which an utterance was surfaced
to a user and that utterance satisfied the i+1 condition for a specific target
unit — i.e. the target was the *only* unknown unit in the utterance.
(Eligibility is determined upstream by UtteranceEligibilityEvaluator.)

This module records those events, counts them, and supports optional rules for
handling repeated occurrences of the same utterance.

Counting semantics
------------------
Three modes are supported via CountingPolicy.duplicate_rule:

  ALLOW_ALL
      Every surfacing is recorded unconditionally.  Replaying the same
      subtitle clip ten times contributes ten exposures.  Appropriate when
      the caller already controls surfacing frequency (e.g. a spaced-
      repetition scheduler that will never surface the same clip twice in
      quick succession).

  DEDUPLICATE_UTTERANCE
      A (user, unit, utterance_id) triple is counted at most once across
      all time.  Recommended default.  Prevents replay abuse and produces
      counts that are proportional to the breadth of input the user has
      encountered rather than the number of button presses.

  DEDUPLICATE_SESSION
      Within a session (same session_id) a (unit, utterance_id) pair counts
      once.  Across different sessions it can count again.  Good for apps
      where a "session" has a clear start/end (e.g. one episode of a show)
      and you want to reward returning to the same source material later
      without penalising users who encounter the same phrase in different
      contexts.
      Falls back to DEDUPLICATE_UTTERANCE when session_id is None.

  DIMINISHING_RETURNS
      Every surfacing is recorded but with a decreasing weight:
          weight = max(decay^(n-1), min_weight)
      where n is the number of times this utterance has previously been
      counted for this (user, unit) pair.  1st = 1.0, 2nd = decay,
      3rd = decay², and so on.  The weighted_count (sum of weights) is
      what you compare against advancement thresholds.

      Rationale: the first time a user sees "Das Buch liegt auf dem Tisch"
      and notices the word *liegt*, that exposure is maximally informative.
      The fifth time they see the exact same sentence, they are likely just
      reading along without active acquisition.  Diminishing returns rewards
      exposure to a variety of example sentences without completely ignoring
      repetition.

Tradeoffs summary
-----------------
┌─────────────────────────┬────────────────────────────────────────────────┐
│ Rule                    │ Best suited for                                │
├─────────────────────────┼────────────────────────────────────────────────┤
│ ALLOW_ALL               │ Scheduler-controlled exposure delivery         │
│ DEDUPLICATE_UTTERANCE   │ Passive intake pipelines (subtitle streaming)  │
│ DEDUPLICATE_SESSION     │ Episode-based viewing apps                     │
│ DIMINISHING_RETURNS     │ Mixed intake + active replay scenarios         │
└─────────────────────────┴────────────────────────────────────────────────┘

Persistence path
----------------
QualifiedExposureCounter stores events in plain Python dicts.  For production,
replace or subclass with a DB-backed implementation that writes ExposureEvent
rows to a table and queries aggregate counts.  The public method signatures are
the stable contract; only the storage internals change.

Utterance IDs
-------------
The counter is agnostic about how utterance_id is derived.  Recommended
approaches:
  - Hash of (utterance_text, start_time, source_id) — stable across reruns
  - Database primary key of the utterance row
  - str(uuid4()) for ephemeral sessions where dedup is not needed
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from learning_units import LearningUnit, LearningUnitType


# ---------------------------------------------------------------------------
# Duplicate-counting rules
# ---------------------------------------------------------------------------

class DuplicateRule(str, Enum):
    """
    Controls how repeated occurrences of the same utterance are counted.

    See module docstring for a detailed comparison of each rule.
    """
    ALLOW_ALL = "allow_all"
    DEDUPLICATE_UTTERANCE = "deduplicate_utterance"
    DEDUPLICATE_SESSION = "deduplicate_session"
    DIMINISHING_RETURNS = "diminishing_returns"


# ---------------------------------------------------------------------------
# Counting policy
# ---------------------------------------------------------------------------

@dataclass
class CountingPolicy:
    """
    Aggregate configuration for QualifiedExposureCounter.

    Attributes:
        duplicate_rule:
            How to handle repeated exposures to the same utterance.
            Default: DEDUPLICATE_UTTERANCE.

        diminishing_decay:
            Base of the geometric decay for DIMINISHING_RETURNS.
            weight(n) = diminishing_decay ^ (n - 1)  [n = occurrence index, 1-based]
            Must be in (0, 1).  Default 0.5 halves the weight with each repeat.

        min_weight:
            Floor applied to the diminishing weight so that even highly
            repeated utterances still register a small contribution.
            Set to 0.0 to zero-out beyond a certain repetition count.
            Ignored when duplicate_rule != DIMINISHING_RETURNS.
    """
    duplicate_rule: DuplicateRule = DuplicateRule.DEDUPLICATE_UTTERANCE
    diminishing_decay: float = 0.5
    min_weight: float = 0.0

    def __post_init__(self) -> None:
        if not 0.0 < self.diminishing_decay < 1.0:
            raise ValueError(
                f"diminishing_decay must be in (0, 1), got {self.diminishing_decay}"
            )
        if self.min_weight < 0.0:
            raise ValueError(f"min_weight must be >= 0, got {self.min_weight}")


# ---------------------------------------------------------------------------
# Exposure event
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExposureEvent:
    """
    An immutable record of one qualified exposure.

    A qualified exposure is the moment a user was shown an utterance for which
    a specific unit was the sole unknown — i.e. the i+1 condition was met.

    Attributes:
        event_id:
            Unique identifier for this event.  Auto-generated as a UUID4
            string.  Useful as a primary key when persisting to a database.

        user_id:
            Opaque user identifier.

        unit:
            The learning unit that was the sole unknown in the utterance.
            This is the acquisition target of the exposure.

        utterance_id:
            Stable identifier for the source utterance.  See module docstring
            for recommended derivation strategies.

        occurred_at:
            Wall-clock time of the exposure.  Always timezone-aware; defaults
            to UTC.

        weight:
            Contribution of this event to the weighted exposure count.
            1.0 for a full exposure, < 1.0 for diminishing-returns repeats.

        session_id:
            Optional session scope for DEDUPLICATE_SESSION policy.
            None means the event belongs to no named session.

        source_id:
            Optional identifier for the source material (e.g. video ID,
            book chapter).  Useful for analytics and per-source dedup.
    """
    event_id: str
    user_id: str
    unit: LearningUnit
    utterance_id: str
    occurred_at: datetime
    weight: float = 1.0
    session_id: Optional[str] = None
    source_id: Optional[str] = None

    def __repr__(self) -> str:
        ts = self.occurred_at.strftime("%H:%M:%S")
        return (
            f"ExposureEvent("
            f"user={self.user_id!r}, "
            f"unit={self.unit.key!r}, "
            f"utterance={self.utterance_id!r}, "
            f"weight={self.weight:.2f}, "
            f"at={ts})"
        )


# ---------------------------------------------------------------------------
# Exposure statistics snapshot
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExposureStats:
    """
    A read-only summary of all qualified exposures for one (user, unit) pair.

    Attributes:
        unit:
            The learning unit these statistics describe.

        raw_count:
            Total number of ExposureEvents recorded, regardless of weight.
            Counts every accepted record() call.

        weighted_count:
            Sum of weights across all events.  With ALLOW_ALL or
            DEDUPLICATE_* policies every weight is 1.0, so weighted_count
            equals raw_count.  With DIMINISHING_RETURNS, weighted_count <
            raw_count when repeats have occurred.
            Compare this against advancement thresholds.

        unique_utterances:
            Number of distinct utterance_ids that contributed at least one
            exposure.  A proxy for how many different contexts the user has
            encountered the target in.

        first_exposure:
            Timestamp of the earliest recorded event.  None if no events exist.

        last_exposure:
            Timestamp of the most recent recorded event.  None if no events exist.
    """
    unit: LearningUnit
    raw_count: int
    weighted_count: float
    unique_utterances: int
    first_exposure: Optional[datetime]
    last_exposure: Optional[datetime]

    def __repr__(self) -> str:
        return (
            f"ExposureStats("
            f"unit={self.unit.key!r}, "
            f"raw={self.raw_count}, "
            f"weighted={self.weighted_count:.2f}, "
            f"unique_utterances={self.unique_utterances})"
        )


# ---------------------------------------------------------------------------
# Counter
# ---------------------------------------------------------------------------

class QualifiedExposureCounter:
    """
    Records and queries qualified exposure events for user/unit pairs.

    All state is held in Python dicts (MVP in-memory storage).  For
    production, replace the private _write_* and _read_* internals with
    database calls while keeping the public API unchanged.

    Args:
        policy: CountingPolicy that governs deduplication and weighting.
                Defaults to DEDUPLICATE_UTTERANCE with weight 1.0.

    Thread safety:
        Not thread-safe.  For concurrent access wrap with a lock or migrate
        to a database-backed implementation.
    """

    def __init__(self, policy: Optional[CountingPolicy] = None) -> None:
        self.policy = policy or CountingPolicy()

        # Primary event log: _events[user_id][unit_key] -> list[ExposureEvent]
        self._events: dict[str, dict[_UnitKey, list[ExposureEvent]]] = {}

        # Per-(user, unit, utterance_id) occurrence count — used for both
        # deduplication checks (count >= 1) and diminishing-return weights.
        self._utterance_counts: dict[str, dict[_UnitKey, dict[str, int]]] = {}

        # DEDUPLICATE_SESSION: seen (session_id, utterance_id) pairs per (user, unit).
        self._session_seen: dict[str, dict[_UnitKey, set[tuple[str, str]]]] = {}

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def record(
        self,
        user_id: str,
        unit: LearningUnit,
        utterance_id: str,
        occurred_at: Optional[datetime] = None,
        session_id: Optional[str] = None,
        source_id: Optional[str] = None,
    ) -> Optional[ExposureEvent]:
        """
        Attempt to record a qualified exposure.

        Returns the ExposureEvent if the policy accepted the event, or None
        if the event was rejected as a duplicate.  Callers should advance
        the user's knowledge state (e.g. via UserKnowledgeStore.record_exposure)
        only when the return value is not None.

        Args:
            user_id:      The user who was exposed.
            unit:         The sole-unknown acquisition target unit.
            utterance_id: Stable ID of the source utterance.
            occurred_at:  Exposure timestamp.  Defaults to now (UTC).
            session_id:   Optional session scope for DEDUPLICATE_SESSION.
            source_id:    Optional source material identifier (e.g. video ID).

        Returns:
            ExposureEvent if accepted, None if rejected by the duplicate rule.
        """
        if occurred_at is None:
            occurred_at = datetime.now(timezone.utc)

        unit_key = _unit_key(unit)

        # Check deduplication policy
        if self._is_duplicate(user_id, unit_key, utterance_id, session_id):
            return None

        # Compute weight (relevant only for DIMINISHING_RETURNS)
        weight = self._compute_weight(user_id, unit_key, utterance_id)

        event = ExposureEvent(
            event_id=str(uuid.uuid4()),
            user_id=user_id,
            unit=unit,
            utterance_id=utterance_id,
            occurred_at=occurred_at,
            weight=weight,
            session_id=session_id,
            source_id=source_id,
        )

        # Persist
        self._append_event(user_id, unit_key, event)
        self._increment_utterance_count(user_id, unit_key, utterance_id)
        if session_id is not None:
            self._mark_session_seen(user_id, unit_key, session_id, utterance_id)

        return event

    def reset_user(self, user_id: str) -> None:
        """
        Remove all exposure records for a user.

        Useful for account resets and test teardown.  Irreversible in this
        in-memory implementation.
        """
        self._events.pop(user_id, None)
        self._utterance_counts.pop(user_id, None)
        self._session_seen.pop(user_id, None)

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

    def get_raw_count(self, user_id: str, unit: LearningUnit) -> int:
        """
        Return the total number of accepted exposure events for (user, unit).

        With deduplication policies this equals the number of distinct
        qualifying utterances (or session+utterance pairs).  With ALLOW_ALL
        it equals the number of times record() was called and accepted.
        """
        return len(self._get_events(user_id, _unit_key(unit)))

    def get_weighted_count(self, user_id: str, unit: LearningUnit) -> float:
        """
        Return the sum of weights across all accepted exposure events.

        With ALLOW_ALL, DEDUPLICATE_UTTERANCE, or DEDUPLICATE_SESSION every
        weight is 1.0, so this equals get_raw_count().  With
        DIMINISHING_RETURNS it is less than or equal to get_raw_count().

        Use this value when comparing against advancement thresholds:
            if counter.get_weighted_count(user_id, unit) >= threshold:
                store.advance(user_id, unit)
        """
        return sum(e.weight for e in self._get_events(user_id, _unit_key(unit)))

    def get_stats(self, user_id: str, unit: LearningUnit) -> ExposureStats:
        """
        Return a full statistics snapshot for (user, unit).

        Builds ExposureStats from the raw event list.  O(N) where N is the
        number of recorded events for this pair.
        """
        events = self._get_events(user_id, _unit_key(unit))
        if not events:
            return ExposureStats(
                unit=unit,
                raw_count=0,
                weighted_count=0.0,
                unique_utterances=0,
                first_exposure=None,
                last_exposure=None,
            )
        return ExposureStats(
            unit=unit,
            raw_count=len(events),
            weighted_count=sum(e.weight for e in events),
            unique_utterances=len({e.utterance_id for e in events}),
            first_exposure=min(e.occurred_at for e in events),
            last_exposure=max(e.occurred_at for e in events),
        )

    def get_all_stats(self, user_id: str) -> dict[LearningUnit, ExposureStats]:
        """
        Return an ExposureStats snapshot for every unit that has exposures.

        Returns an empty dict if the user has no records.
        """
        user_events = self._events.get(user_id, {})
        result: dict[LearningUnit, ExposureStats] = {}
        for unit_key, events in user_events.items():
            if events:
                unit = events[0].unit  # canonical unit from the first event
                result[unit] = self.get_stats(user_id, unit)
        return result

    def get_events(
        self,
        user_id: str,
        unit: LearningUnit,
    ) -> list[ExposureEvent]:
        """
        Return all accepted exposure events for (user, unit) in recorded order.

        Returns an empty list if no events exist.  The returned list is a
        copy — mutations do not affect internal state.
        """
        return list(self._get_events(user_id, _unit_key(unit)))

    def units_above_threshold(
        self,
        user_id: str,
        threshold: float,
    ) -> list[LearningUnit]:
        """
        Return all units whose weighted exposure count meets or exceeds threshold.

        Useful for batch-advancing units after processing a video:
            ready = counter.units_above_threshold(user_id, 5.0)
            for unit in ready:
                store.set_state(user_id, unit, KnowledgeState.UNLOCKED)

        Args:
            user_id:   The user to query.
            threshold: Minimum weighted_count to include a unit.

        Returns:
            List of LearningUnits, unordered.
        """
        all_stats = self.get_all_stats(user_id)
        return [
            unit
            for unit, stats in all_stats.items()
            if stats.weighted_count >= threshold
        ]

    # ------------------------------------------------------------------
    # Private: deduplication logic
    # ------------------------------------------------------------------

    def _is_duplicate(
        self,
        user_id: str,
        unit_key: _UnitKey,
        utterance_id: str,
        session_id: Optional[str],
    ) -> bool:
        """
        Return True if this (user, unit, utterance) should be rejected
        under the current duplicate_rule.
        """
        rule = self.policy.duplicate_rule

        if rule == DuplicateRule.ALLOW_ALL:
            return False

        if rule == DuplicateRule.DIMINISHING_RETURNS:
            # Always accept; weight is reduced, but never outright rejected.
            return False

        if rule == DuplicateRule.DEDUPLICATE_UTTERANCE:
            count = self._get_utterance_count(user_id, unit_key, utterance_id)
            return count > 0

        if rule == DuplicateRule.DEDUPLICATE_SESSION:
            if session_id is None:
                # No session → fall back to lifetime deduplication
                count = self._get_utterance_count(user_id, unit_key, utterance_id)
                return count > 0
            seen = self._session_seen.get(user_id, {}).get(unit_key, set())
            return (session_id, utterance_id) in seen

        return False  # unreachable; satisfies type checker

    # ------------------------------------------------------------------
    # Private: weight computation
    # ------------------------------------------------------------------

    def _compute_weight(
        self,
        user_id: str,
        unit_key: _UnitKey,
        utterance_id: str,
    ) -> float:
        """
        Compute the weight for this event.

        For DIMINISHING_RETURNS: weight = decay^n where n is the number of
        times this utterance has already been counted for (user, unit).
        First occurrence: n=0 → weight=1.0.  Second: n=1 → weight=decay.

        For all other rules: weight=1.0 (deduplication makes repeat weight moot).
        """
        if self.policy.duplicate_rule != DuplicateRule.DIMINISHING_RETURNS:
            return 1.0

        n = self._get_utterance_count(user_id, unit_key, utterance_id)
        raw_weight = self.policy.diminishing_decay ** n
        return max(raw_weight, self.policy.min_weight)

    # ------------------------------------------------------------------
    # Private: low-level storage accessors
    # ------------------------------------------------------------------

    def _get_events(self, user_id: str, unit_key: _UnitKey) -> list[ExposureEvent]:
        return self._events.get(user_id, {}).get(unit_key, [])

    def _append_event(
        self, user_id: str, unit_key: _UnitKey, event: ExposureEvent
    ) -> None:
        if user_id not in self._events:
            self._events[user_id] = {}
        self._events[user_id].setdefault(unit_key, []).append(event)

    def _get_utterance_count(
        self, user_id: str, unit_key: _UnitKey, utterance_id: str
    ) -> int:
        return (
            self._utterance_counts
            .get(user_id, {})
            .get(unit_key, {})
            .get(utterance_id, 0)
        )

    def _increment_utterance_count(
        self, user_id: str, unit_key: _UnitKey, utterance_id: str
    ) -> None:
        if user_id not in self._utterance_counts:
            self._utterance_counts[user_id] = {}
        counts = self._utterance_counts[user_id]
        if unit_key not in counts:
            counts[unit_key] = {}
        counts[unit_key][utterance_id] = counts[unit_key].get(utterance_id, 0) + 1

    def _mark_session_seen(
        self,
        user_id: str,
        unit_key: _UnitKey,
        session_id: str,
        utterance_id: str,
    ) -> None:
        if user_id not in self._session_seen:
            self._session_seen[user_id] = {}
        if unit_key not in self._session_seen[user_id]:
            self._session_seen[user_id][unit_key] = set()
        self._session_seen[user_id][unit_key].add((session_id, utterance_id))


# ---------------------------------------------------------------------------
# Internal type alias
# ---------------------------------------------------------------------------

_UnitKey = tuple[LearningUnitType, str]


def _unit_key(unit: LearningUnit) -> _UnitKey:
    return (unit.unit_type, unit.key)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _demo() -> None:
    """
    Demonstrates all four counting policies and the main query methods.
    """
    from learning_units import LearningUnit, LearningUnitType

    def lemma(k: str) -> LearningUnit:
        return LearningUnit(LearningUnitType.LEMMA, k, k)

    USER = "alice"
    fantastisch = lemma("fantastisch")
    popcorn = lemma("popcorn")
    lohnen = lemma("lohnen")

    # Utterance IDs (in production: hash of text+timing or DB row ID)
    UTT_1 = "utt:film-ist-fantastisch"
    UTT_2 = "utt:popcorn-danach"
    UTT_3 = "utt:popcorn-kostet"
    UTT_4 = "utt:film-ist-fantastisch"   # intentional repeat of UTT_1

    def _header(title: str) -> None:
        print(f"\n{'─' * 62}")
        print(f"  {title}")
        print(f"{'─' * 62}")

    # ------------------------------------------------------------------
    # 1. ALLOW_ALL — every call is counted
    # ------------------------------------------------------------------
    _header("1. ALLOW_ALL — every record() accepted")

    counter = QualifiedExposureCounter(CountingPolicy(DuplicateRule.ALLOW_ALL))
    for _ in range(3):
        counter.record(USER, fantastisch, UTT_1)
    stats = counter.get_stats(USER, fantastisch)
    print("\n  3× same utterance for 'fantastisch'")
    print(f"  raw_count       : {stats.raw_count}")
    print(f"  weighted_count  : {stats.weighted_count:.1f}")
    print(f"  unique_utterances: {stats.unique_utterances}")

    # ------------------------------------------------------------------
    # 2. DEDUPLICATE_UTTERANCE — same utterance_id accepted only once
    # ------------------------------------------------------------------
    _header("2. DEDUPLICATE_UTTERANCE — lifetime dedup per utterance")

    counter = QualifiedExposureCounter()   # default policy
    events: list = []
    for uid in [UTT_1, UTT_2, UTT_3, UTT_4]:  # UTT_4 is a repeat of UTT_1
        result = counter.record(USER, fantastisch, uid)
        accepted = result is not None
        events.append((uid, accepted))

    print()
    for uid, accepted in events:
        tag = "ACCEPTED" if accepted else "REJECTED (duplicate)"
        print(f"  record({uid!r:<34}) → {tag}")

    stats = counter.get_stats(USER, fantastisch)
    print(f"\n  raw_count       : {stats.raw_count}  (UTT_4 rejected as duplicate of UTT_1)")
    print(f"  unique_utterances: {stats.unique_utterances}")

    # ------------------------------------------------------------------
    # 3. DEDUPLICATE_SESSION — per-session dedup, counts again next session
    # ------------------------------------------------------------------
    _header("3. DEDUPLICATE_SESSION — same utterance allowed in new session")

    counter = QualifiedExposureCounter(CountingPolicy(DuplicateRule.DEDUPLICATE_SESSION))

    SESSION_A = "ep1-2026-03-22"
    SESSION_B = "ep1-2026-03-29"   # a week later, same episode

    results = [
        counter.record(USER, fantastisch, UTT_1, session_id=SESSION_A),
        counter.record(USER, fantastisch, UTT_1, session_id=SESSION_A),  # dup in same session
        counter.record(USER, fantastisch, UTT_1, session_id=SESSION_B),  # allowed: new session
    ]

    print()
    labels = [
        "Session A, UTT_1 (first)",
        "Session A, UTT_1 (replay in same session)",
        "Session B, UTT_1 (rewatching a week later)",
    ]
    for label, r in zip(labels, results):
        print(f"  {label:<45} → {'ACCEPTED' if r else 'REJECTED'}")

    stats = counter.get_stats(USER, fantastisch)
    print(f"\n  raw_count: {stats.raw_count}  (2 distinct sessions)")

    # ------------------------------------------------------------------
    # 4. DIMINISHING_RETURNS — weight decays with repetition
    # ------------------------------------------------------------------
    _header("4. DIMINISHING_RETURNS — weight = 0.5^(n-1)")

    policy = CountingPolicy(
        duplicate_rule=DuplicateRule.DIMINISHING_RETURNS,
        diminishing_decay=0.5,
        min_weight=0.0,
    )
    counter = QualifiedExposureCounter(policy)

    print(f"\n  {'occurrence':<12} {'utterance_id':<36} {'weight'}")
    print(f"  {'─'*12} {'─'*36} {'─'*6}")
    for i, uid in enumerate([UTT_1, UTT_1, UTT_1, UTT_2, UTT_1], start=1):
        event = counter.record(USER, fantastisch, uid)
        w = event.weight if event else 0.0
        print(f"  {i:<12} {uid:<36} {w:.4f}")

    stats = counter.get_stats(USER, fantastisch)
    print(f"\n  raw_count       : {stats.raw_count}")
    print(f"  weighted_count  : {stats.weighted_count:.4f}")
    print(f"  breakdown       : 1.0 + 0.5 + 0.25 + 1.0 + 0.125 = {1+0.5+0.25+1+0.125:.4f}")

    # ------------------------------------------------------------------
    # 5. Multiple units + units_above_threshold
    # ------------------------------------------------------------------
    _header("5. units_above_threshold — batch advancement check")

    counter = QualifiedExposureCounter()   # DEDUPLICATE_UTTERANCE

    # Simulate a user watching two episodes worth of clips
    exposures = [
        (fantastisch, f"utt:fantastisch-{i}") for i in range(6)
    ] + [
        (popcorn, f"utt:popcorn-{i}") for i in range(3)
    ] + [
        (lohnen, f"utt:lohnen-{i}") for i in range(1)
    ]
    for unit, uid in exposures:
        counter.record(USER, unit, uid)

    print("\n  Threshold = 5 exposures (e.g. to advance to UNLOCKED)")
    ready = counter.units_above_threshold(USER, threshold=5.0)
    print(f"  Units ready for advancement: {[u.key for u in ready]}")

    print("\n  Full summary:")
    all_stats = counter.get_all_stats(USER)
    for stats in sorted(all_stats.values(), key=lambda s: -s.raw_count):
        bar = "█" * stats.raw_count
        print(f"  {stats.unit.key:<16} {bar}  ({stats.raw_count})")

    # ------------------------------------------------------------------
    # 6. get_events() — audit trail
    # ------------------------------------------------------------------
    _header("6. get_events() — raw event log")

    counter = QualifiedExposureCounter()
    for uid in [UTT_1, UTT_2]:
        counter.record(USER, fantastisch, uid)

    print()
    for e in counter.get_events(USER, fantastisch):
        print(f"  {e}")


if __name__ == "__main__":
    _demo()
