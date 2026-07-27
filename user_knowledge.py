"""
user_knowledge.py
-----------------
MVP user knowledge model for a German language-learning pipeline.

Tracks what each user knows and drives the i+1 exposure filter:
an utterance is surfaced to a user only when it contains exactly one
unit they do not yet know.

Knowledge lifecycle
-------------------
UNSEEN → EXPOSED → UNLOCKED → KNOWN_PASSIVE → KNOWN_ACTIVE → MASTERED

  UNSEEN        Unit has never appeared in an i+1 utterance for this user.
  EXPOSED       Unit appeared as the i+1 target at least once; user saw it
                in context but has not actively studied it.
  UNLOCKED      User has been exposed enough times that the unit is queued
                for active study (e.g. added to SRS deck).
  KNOWN_PASSIVE User recognises the unit reliably in reading/listening.
  KNOWN_ACTIVE  User can produce the unit reliably in writing/speaking.
  MASTERED      Strong, durable recall over a long time interval.

The boundary between "unknown" and "known" for the i+1 filter is
configurable via KnowledgeFilterPolicy.  The default threshold is
KNOWN_PASSIVE — a unit is considered known once the user can recognise it.

Interaction with exposure counting
-----------------------------------
The pipeline flow is:

  1.  Extract units from all utterances (UtteranceUnitExtractor).
  2.  For each user, call store.find_sole_unknown(user_id, utterance_units)
      to identify i+1 utterances and the target unit.
  3.  When an i+1 utterance is surfaced: call store.record_exposure(user_id, unit).
  4.  State advances automatically per ExposurePolicy (UNSEEN → EXPOSED,
      and EXPOSED → UNLOCKED after N exposures).
  5.  Higher state transitions (UNLOCKED → KNOWN_PASSIVE etc.) are driven by
      SRS review results, handled by a separate review module that calls
      store.set_state() directly.

This module is intentionally in-memory.  For production, replace or wrap
UserKnowledgeStore with a class that reads/writes the database; the method
signatures remain the same.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Optional

from learning_units import LearningUnit, LearningUnitType, UserKnowledgeProfile


# ---------------------------------------------------------------------------
# Knowledge state enum
# ---------------------------------------------------------------------------

class KnowledgeState(IntEnum):
    """
    Ordered mastery levels for a single learning unit.

    Using IntEnum gives free comparison operators:
        KnowledgeState.KNOWN_PASSIVE >= KnowledgeState.EXPOSED  # True
    which lets KnowledgeFilterPolicy express its threshold as a single value.
    """
    UNSEEN = 0
    EXPOSED = 1
    UNLOCKED = 2
    KNOWN_PASSIVE = 3
    KNOWN_ACTIVE = 4
    MASTERED = 5

    def label(self) -> str:
        return {
            0: "Unseen",
            1: "Exposed",
            2: "Unlocked",
            3: "Known (passive)",
            4: "Known (active)",
            5: "Mastered",
        }[self.value]


# ---------------------------------------------------------------------------
# Policy dataclasses
# ---------------------------------------------------------------------------

@dataclass
class KnowledgeFilterPolicy:
    """
    Defines what counts as "known" when building the i+1 filter.

    Attributes:
        min_known_state:
            Units at or above this state are treated as known.
            The default (KNOWN_PASSIVE) means the user must be able to
            recognise the unit before it stops being an "unknown slot" in
            the i+1 filter.

            Raise to KNOWN_ACTIVE for a stricter production-use threshold.
            Lower to UNLOCKED to make the filter fire sooner while the user
            is still learning the unit actively (useful for A0/A1 users who
            need many i+1 exposures to get started).

    Note on EXPOSED and UNLOCKED:
        EXPOSED and UNLOCKED are below the default threshold, so a unit the
        user has merely seen — but not yet internalised — still counts as
        unknown.  This is intentional: repeated i+1 exposure to the same
        target accelerates acquisition.
    """
    min_known_state: KnowledgeState = KnowledgeState.KNOWN_PASSIVE

    def is_known(self, state: KnowledgeState) -> bool:
        """Return True if `state` meets the known threshold."""
        return state >= self.min_known_state

    def is_unknown(self, state: KnowledgeState) -> bool:
        return not self.is_known(state)


@dataclass
class ExposurePolicy:
    """
    Controls automatic state transitions triggered by record_exposure().

    Attributes:
        auto_advance:
            When True, record_exposure() may advance the unit's state.
            Set False to disable all automatic transitions (e.g. for
            offline batch-import scenarios where you set states explicitly).

        exposures_to_unlock:
            Number of i+1 exposures in the EXPOSED state before the unit
            is automatically advanced to UNLOCKED (queued for active study).
            Default 5 reflects roughly one week of daily single exposures.
    """
    auto_advance: bool = True
    exposures_to_unlock: int = 5


# ---------------------------------------------------------------------------
# Per-unit knowledge record
# ---------------------------------------------------------------------------

@dataclass
class UserUnitKnowledge:
    """
    One user's knowledge record for one learning unit.

    This is the row-level representation.  One record per (user_id, unit) pair.

    Attributes:
        user_id:              Opaque user identifier.
        unit:                 The learning unit this record describes.
        state:                Current knowledge state.
        exposure_count:       Times this unit was the sole unknown in an i+1
                              utterance surfaced to the user.
        correct_recall_count: SRS correct-answer count.  Populated by the
                              SRS review module; stored here so this record
                              is the single source of truth per unit.
        state_changed_at:     Timestamp of the last state transition.
        last_exposed_at:      Timestamp of the last record_exposure() call.
        created_at:           Timestamp when this record was first created.
    """
    user_id: str
    unit: LearningUnit
    state: KnowledgeState = KnowledgeState.UNSEEN
    exposure_count: int = 0
    correct_recall_count: int = 0
    state_changed_at: Optional[datetime] = None
    last_exposed_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return (
            f"UserUnitKnowledge("
            f"user={self.user_id!r}, "
            f"unit={self.unit.key!r}, "
            f"state={self.state.name}, "
            f"exposures={self.exposure_count})"
        )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class UserKnowledgeStore:
    """
    In-memory store for UserUnitKnowledge records.

    Provides all read and write operations needed by the pipeline:
      - state lookup and update
      - exposure recording with auto-advance
      - i+1 filtering helpers
      - UserKnowledgeProfile snapshot for the filter stage

    Storage structure:
        _store[user_id][(unit_type, unit_key)] = UserUnitKnowledge

    The outer dict is keyed by user_id so per-user queries are O(1) to reach
    the right partition, then O(1) for individual unit lookups.

    Replace this class with a DB-backed implementation for production.  The
    method signatures are the public contract; internals are replaceable.
    """

    def __init__(
        self,
        filter_policy: Optional[KnowledgeFilterPolicy] = None,
        exposure_policy: Optional[ExposurePolicy] = None,
    ) -> None:
        self.filter_policy = filter_policy or KnowledgeFilterPolicy()
        self.exposure_policy = exposure_policy or ExposurePolicy()
        # _store[user_id][(unit_type, key)] → UserUnitKnowledge
        self._store: dict[str, dict[tuple[LearningUnitType, str], UserUnitKnowledge]] = {}

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_knowledge(self, user_id: str, unit: LearningUnit) -> UserUnitKnowledge:
        """
        Return the knowledge record for (user_id, unit).

        If no record exists, returns a default UNSEEN record without
        persisting it — the store is only written when state changes.
        """
        return self._user_store(user_id).get(
            self._key(unit),
            UserUnitKnowledge(user_id=user_id, unit=unit),
        )

    def get_state(self, user_id: str, unit: LearningUnit) -> KnowledgeState:
        """Return the current KnowledgeState for (user_id, unit)."""
        record = self._user_store(user_id).get(self._key(unit))
        return record.state if record else KnowledgeState.UNSEEN

    def is_known(self, user_id: str, unit: LearningUnit) -> bool:
        """Return True if the unit meets the filter policy's known threshold."""
        return self.filter_policy.is_known(self.get_state(user_id, unit))

    def unknown_units(
        self,
        user_id: str,
        units: list[LearningUnit],
    ) -> list[LearningUnit]:
        """Return the subset of `units` that the user does not yet know."""
        return [u for u in units if not self.is_known(user_id, u)]

    def find_sole_unknown(
        self,
        user_id: str,
        units: list[LearningUnit],
    ) -> Optional[LearningUnit]:
        """
        Return the one unit in `units` the user does not know, or None.

        This is the i+1 gate.  Call this on the deduplicated units of a
        candidate utterance:
          - returns a LearningUnit  → the utterance is i+1 for this user
          - returns None            → 0 or 2+ unknowns; skip this utterance

        The returned unit is the acquisition target for that exposure.
        """
        unknowns = self.unknown_units(user_id, units)
        return unknowns[0] if len(unknowns) == 1 else None

    def get_summary(self, user_id: str) -> dict[KnowledgeState, int]:
        """
        Return a count of units per KnowledgeState for the given user.

        Useful for dashboard displays and debugging.  UNSEEN units are not
        counted here since they have no explicit records.
        """
        counts: dict[KnowledgeState, int] = {s: 0 for s in KnowledgeState}
        for record in self._user_store(user_id).values():
            counts[record.state] += 1
        return counts

    def build_profile(self, user_id: str) -> UserKnowledgeProfile:
        """
        Build a UserKnowledgeProfile snapshot for the i+1 filter stage.

        The profile is a frozen, lightweight view — it does not update if
        the store changes after this call.  Build a fresh profile each time
        you run the filter for a user.
        """
        known_keys = frozenset(
            (rec.unit.unit_type, rec.unit.key)
            for rec in self._user_store(user_id).values()
            if self.filter_policy.is_known(rec.state)
        )
        return UserKnowledgeProfile(user_id=user_id, known_keys=known_keys)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def set_state(
        self,
        user_id: str,
        unit: LearningUnit,
        state: KnowledgeState,
    ) -> UserUnitKnowledge:
        """
        Directly set the knowledge state for (user_id, unit).

        Use this for:
          - SRS review outcomes (UNLOCKED → KNOWN_PASSIVE)
          - Manual overrides
          - Bulk onboarding seeding via seed_known_units()
          - Test setup

        Does not enforce a forward-only transition: you can demote a unit
        (e.g. KNOWN_PASSIVE → UNLOCKED after a long inactivity streak).
        Whether to enforce monotonic progression is a product decision.
        """
        record = self._get_or_create(user_id, unit)
        record.state = state
        record.state_changed_at = datetime.now(timezone.utc)
        return record

    def record_exposure(
        self,
        user_id: str,
        unit: LearningUnit,
    ) -> UserUnitKnowledge:
        """
        Record that (user_id, unit) was the i+1 target of a surfaced utterance.

        Always increments exposure_count and updates last_exposed_at.

        Auto-advance behaviour (when exposure_policy.auto_advance is True):
          UNSEEN   → EXPOSED  on first call
          EXPOSED  → UNLOCKED once exposure_count reaches exposures_to_unlock

        States above UNLOCKED are not touched by this method — those transitions
        belong to the SRS review module which calls set_state() directly.
        """
        record = self._get_or_create(user_id, unit)
        record.exposure_count += 1
        record.last_exposed_at = datetime.now(timezone.utc)

        if self.exposure_policy.auto_advance:
            if record.state == KnowledgeState.UNSEEN:
                self._advance(record, KnowledgeState.EXPOSED)
            elif (
                record.state == KnowledgeState.EXPOSED
                and record.exposure_count >= self.exposure_policy.exposures_to_unlock
            ):
                self._advance(record, KnowledgeState.UNLOCKED)

        return record

    def seed_known_units(
        self,
        user_id: str,
        units: list[LearningUnit],
        state: KnowledgeState = KnowledgeState.KNOWN_PASSIVE,
    ) -> None:
        """
        Bulk-mark a list of units as known for a new user.

        Call this during onboarding to pre-populate the knowledge store with
        high-frequency vocabulary the user is assumed to already know.  Without
        a seed, a new user has zero known units and the i+1 filter cannot fire
        for any sentence (every sentence has multiple unknowns).

        A practical seed set for German A1: the ~200 most frequent lemmas
        (determiners, pronouns, common verbs).  Seed at KNOWN_PASSIVE so
        these units don't appear as i+1 targets and clutter the queue.
        """
        for unit in units:
            self.set_state(user_id, unit, state)

    def reset_user(self, user_id: str) -> None:
        """
        Remove all knowledge records for a user.

        Useful for testing and account resets.  Irreversible in this
        in-memory implementation.
        """
        self._store.pop(user_id, None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _user_store(self, user_id: str) -> dict[tuple[LearningUnitType, str], UserUnitKnowledge]:
        """Return the inner dict for a user, read-only (does not create)."""
        return self._store.get(user_id, {})

    def _get_or_create(self, user_id: str, unit: LearningUnit) -> UserUnitKnowledge:
        """Return or create the mutable knowledge record for (user_id, unit)."""
        if user_id not in self._store:
            self._store[user_id] = {}
        inner = self._store[user_id]
        k = self._key(unit)
        if k not in inner:
            inner[k] = UserUnitKnowledge(user_id=user_id, unit=unit)
        return inner[k]

    @staticmethod
    def _key(unit: LearningUnit) -> tuple[LearningUnitType, str]:
        return (unit.unit_type, unit.key)

    @staticmethod
    def _advance(record: UserUnitKnowledge, new_state: KnowledgeState) -> None:
        record.state = new_state
        record.state_changed_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path | str) -> None:
        """
        Serialise the entire store to a UTF-8 JSON file.

        All users and their unit records are written atomically via a
        write-then-rename pattern so a crash during save never leaves a
        partial file.

        Args:
            path: Destination file path.  Parent directory must exist.
        """
        path = Path(path)
        users: dict[str, dict] = {}
        for user_id, records in self._store.items():
            user_records: dict[str, dict] = {}
            for record in records.values():
                rec_key = f"{record.unit.unit_type.value}:{record.unit.key}"
                user_records[rec_key] = {
                    "unit_type":           record.unit.unit_type.value,
                    "key":                 record.unit.key,
                    "display_form":        record.unit.display_form,
                    "language":            record.unit.language,
                    "state":               record.state.value,
                    "exposure_count":      record.exposure_count,
                    "correct_recall_count": record.correct_recall_count,
                    "state_changed_at":    record.state_changed_at.isoformat() if record.state_changed_at else None,
                    "last_exposed_at":     record.last_exposed_at.isoformat() if record.last_exposed_at else None,
                    "created_at":          record.created_at.isoformat(),
                }
            users[user_id] = user_records

        payload = json.dumps({"version": 1, "users": users}, ensure_ascii=False, indent=2)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(
        cls,
        path: Path | str,
        filter_policy: Optional[KnowledgeFilterPolicy] = None,
        exposure_policy: Optional[ExposurePolicy] = None,
    ) -> "UserKnowledgeStore":
        """
        Deserialise a store from a JSON file written by save().

        Args:
            path:            Path to the JSON file.
            filter_policy:   KnowledgeFilterPolicy for the loaded store.
                             Defaults to KnowledgeFilterPolicy().
            exposure_policy: ExposurePolicy for the loaded store.
                             Defaults to ExposurePolicy().

        Returns:
            A new UserKnowledgeStore populated with the saved records.

        Raises:
            FileNotFoundError: if *path* does not exist.
            ValueError:        if the file format version is unrecognised.
        """
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        version = raw.get("version")
        if version != 1:
            raise ValueError(f"Unsupported store file version: {version!r}")

        store = cls(filter_policy=filter_policy, exposure_policy=exposure_policy)
        for user_id, records in raw.get("users", {}).items():
            store._store[user_id] = {}
            for rec_key, rec in records.items():
                unit = LearningUnit(
                    unit_type=LearningUnitType(rec["unit_type"]),
                    key=rec["key"],
                    display_form=rec["display_form"],
                    language=rec.get("language", "de"),
                )
                record = UserUnitKnowledge(
                    user_id=user_id,
                    unit=unit,
                    state=KnowledgeState(rec["state"]),
                    exposure_count=rec["exposure_count"],
                    correct_recall_count=rec["correct_recall_count"],
                    state_changed_at=datetime.fromisoformat(rec["state_changed_at"]) if rec["state_changed_at"] else None,
                    last_exposed_at=datetime.fromisoformat(rec["last_exposed_at"]) if rec["last_exposed_at"] else None,
                    created_at=datetime.fromisoformat(rec["created_at"]),
                )
                store._store[user_id][cls._key(unit)] = record
        return store


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _demo() -> None:
    from learning_units import LearningUnit, LearningUnitType

    def lemma(key: str) -> LearningUnit:
        return LearningUnit(LearningUnitType.LEMMA, key, key)

    store = UserKnowledgeStore(
        filter_policy=KnowledgeFilterPolicy(min_known_state=KnowledgeState.KNOWN_PASSIVE),
        exposure_policy=ExposurePolicy(auto_advance=True, exposures_to_unlock=3),
    )
    USER = "alice"

    # ------------------------------------------------------------------
    # 1. Seeding known vocabulary at onboarding
    # ------------------------------------------------------------------
    print("─" * 60)
    print("  1. ONBOARDING SEED")
    print("─" * 60)

    seed_words = [lemma(k) for k in [
        "gehen", "sein", "haben", "und", "in", "mit", "der", "die", "das",
        "ich", "du", "er", "sie", "wir", "auch", "nicht",
    ]]
    store.seed_known_units(USER, seed_words, state=KnowledgeState.KNOWN_PASSIVE)
    summary = store.get_summary(USER)
    print(f"\n  Seeded {len(seed_words)} words as KNOWN_PASSIVE.")
    print(f"  Summary: { {s.name: n for s, n in summary.items() if n > 0} }")

    # ------------------------------------------------------------------
    # 2. i+1 filter in action
    # ------------------------------------------------------------------
    print("\n" + "─" * 60)
    print("  2. i+1 FILTER")
    print("─" * 60)

    anfangen = lemma("anfangen")
    training = lemma("training")

    # Sentence 1: one unknown (anfangen) → i+1
    utterance_units_1 = [lemma("ich"), anfangen, lemma("mit"), lemma("dem"), training]
    target = store.find_sole_unknown(USER, utterance_units_1)
    known_1 = [u.key for u in utterance_units_1 if store.is_known(USER, u)]
    unknown_1 = [u.key for u in utterance_units_1 if not store.is_known(USER, u)]
    print("\n  Utterance: 'Ich fange mit dem Training an.'")
    print(f"  Known   : {known_1}")
    print(f"  Unknown : {unknown_1}")
    print(f"  i+1 target: {target.key if target else 'None — not i+1'}")

    # Sentence 2: two unknowns → skip
    utterance_units_2 = [anfangen, training, lemma("und"), lemma("durchhalten")]
    target2 = store.find_sole_unknown(USER, utterance_units_2)
    unknown_2 = [u.key for u in utterance_units_2 if not store.is_known(USER, u)]
    print("\n  Utterance: 'Anfangen, Training und Durchhalten.'")
    print(f"  Unknown : {unknown_2}")
    print(f"  i+1 target: {target2.key if target2 else 'None — 2+ unknowns, skip'}")

    # ------------------------------------------------------------------
    # 3. Exposure recording and auto-advance
    # ------------------------------------------------------------------
    print("\n" + "─" * 60)
    print("  3. EXPOSURE RECORDING  (auto_advance=True, threshold=3)")
    print("─" * 60)

    print(f"\n  State before any exposure: {store.get_state(USER, anfangen).name}")
    for i in range(1, 6):
        rec = store.record_exposure(USER, anfangen)
        print(f"  Exposure {i:>2}: state={rec.state.name:<15} exposure_count={rec.exposure_count}")

    # ------------------------------------------------------------------
    # 4. SRS-driven promotion
    # ------------------------------------------------------------------
    print("\n" + "─" * 60)
    print("  4. SRS PROMOTION  (set_state direct)")
    print("─" * 60)

    store.set_state(USER, anfangen, KnowledgeState.KNOWN_PASSIVE)
    rec = store.get_knowledge(USER, anfangen)
    print(f"\n  After SRS review: state={rec.state.name}")
    print(f"  is_known now: {store.is_known(USER, anfangen)}")

    # Sentence 1 revisited — anfangen is now known
    target_after = store.find_sole_unknown(USER, utterance_units_1)
    unknown_after = [u.key for u in utterance_units_1 if not store.is_known(USER, u)]
    print(f"\n  Sentence 1 unknowns after promotion: {unknown_after}")
    print(f"  i+1 target: {target_after.key if target_after else 'None — not i+1 (0 or 2+ unknowns)'}")

    # ------------------------------------------------------------------
    # 5. Filter policy comparison
    # ------------------------------------------------------------------
    print("\n" + "─" * 60)
    print("  5. FILTER POLICY COMPARISON")
    print("─" * 60)

    unlocked_unit = lemma("durchhalten")
    store.set_state(USER, unlocked_unit, KnowledgeState.UNLOCKED)

    for threshold in [KnowledgeState.UNLOCKED, KnowledgeState.KNOWN_PASSIVE]:
        policy = KnowledgeFilterPolicy(min_known_state=threshold)
        known = policy.is_known(KnowledgeState.UNLOCKED)
        print(
            f"\n  min_known_state={threshold.name:<15} "
            f"→ UNLOCKED counts as known: {known}"
        )

    # ------------------------------------------------------------------
    # 6. User summary
    # ------------------------------------------------------------------
    print("\n" + "─" * 60)
    print("  6. USER SUMMARY")
    print("─" * 60)

    summary_final = store.get_summary(USER)
    print()
    for state, count in summary_final.items():
        if count > 0:
            bar = "█" * count
            print(f"  {state.label():<20} {bar}  ({count})")

    # ------------------------------------------------------------------
    # 7. Build profile for filter stage
    # ------------------------------------------------------------------
    print("\n" + "─" * 60)
    print("  7. UserKnowledgeProfile snapshot")
    print("─" * 60)

    profile = store.build_profile(USER)
    print(f"\n  known_keys count : {len(profile.known_keys)}")
    sample = list(profile.known_keys)[:5]
    print(f"  sample keys      : {[k for _, k in sample]}")
    print(f"  'anfangen' known : {profile.is_known(anfangen)}")
    print(f"  'training' known : {profile.is_known(training)}")


if __name__ == "__main__":
    _demo()
