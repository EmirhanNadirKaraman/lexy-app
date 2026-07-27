"""
Time-aware evidence system for tracking per-user word knowledge.

The central idea is that learning state advances based on *qualified,
distinct* evidence events spread across time — not raw repetition counts.
Two exposures of the same video, or two exposures ten minutes apart, are
one learning event.  Five exposures over five days are five events.

Evidence types
--------------
  Passive  — reading or video exposure: each new content source and each
             new calendar day contribute at most one qualified event.
  Active   — successful spaced recall (chat / review): each day in which
             the user correctly produces the word counts as one event.

State machine (MVP)
-------------------
  UNKNOWN  ──2 passive──►  FAMILIAR  ──5 passive──►  PASSIVE  ──3 active──►  ACTIVE
  UNKNOWN  ─── mark_known ─────────────────────────► PASSIVE
  ANY      ─── mark_unknown ─► UNKNOWN  (resets all evidence)

All thresholds and spacing rules are configurable via EvidenceConfig.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, IntEnum
from typing import Optional


class LearningState(IntEnum):
    """Ordered mastery levels. IntEnum allows >= comparisons."""
    UNKNOWN  = 0
    FAMILIAR = 1
    PASSIVE  = 2
    ACTIVE   = 3

    def label(self) -> str:
        return self.name.capitalize()


class PassiveSource(str, Enum):
    """Origin of a passive evidence event."""
    VIDEO   = "video"
    READING = "reading"


@dataclass(frozen=True)
class PassiveEvidenceEvent:
    """One qualified passive evidence event."""
    timestamp:  datetime
    source:     PassiveSource
    content_id: Optional[str] = None


@dataclass(frozen=True)
class ActiveEvidenceEvent:
    """One qualified active recall event."""
    timestamp: datetime
    correct:   bool = True


@dataclass
class WordKnowledge:
    """Everything known about one (user_id, word) pair."""
    user_id:               str
    word:                  str
    state:                 LearningState = LearningState.UNKNOWN
    passive_evidence_count: int = 0
    active_success_count:  int = 0
    last_passive_event_at: Optional[datetime] = None
    last_active_event_at:  Optional[datetime] = None
    seen_content_ids:      set[str] = field(default_factory=set)
    passive_events:        list[PassiveEvidenceEvent] = field(default_factory=list)
    active_events:         list[ActiveEvidenceEvent]  = field(default_factory=list)


@dataclass
class EvidenceConfig:
    """Spacing rules and state-transition thresholds."""
    passive_min_gap:    timedelta = field(default_factory=lambda: timedelta(hours=12))
    active_min_gap:     timedelta = field(default_factory=lambda: timedelta(hours=24))
    familiar_threshold: int = 2
    passive_threshold:  int = 5
    active_threshold:   int = 3


class KnowledgeStore:
    """
    In-memory store for WordKnowledge records, keyed by (user_id, word).

    Enforces all evidence rules: content deduplication, time spacing, and
    state transitions.
    """

    def __init__(self, config: Optional[EvidenceConfig] = None) -> None:
        self.config = config or EvidenceConfig()
        self._store: dict[tuple[str, str], WordKnowledge] = {}

    def record_passive_evidence(
        self,
        user_id:    str,
        word:       str,
        timestamp:  datetime,
        source:     PassiveSource = PassiveSource.VIDEO,
        content_id: Optional[str] = None,
    ) -> bool:
        """
        Record a passive exposure event (video or reading).
        Returns True if accepted; False if rejected as duplicate or too soon.
        """
        record = self._get_or_create(user_id, word)

        if content_id is not None and content_id in record.seen_content_ids:
            return False

        if record.last_passive_event_at is not None:
            elapsed = timestamp - record.last_passive_event_at
            if elapsed < self.config.passive_min_gap:
                return False

        if content_id is not None:
            record.seen_content_ids.add(content_id)
        record.passive_evidence_count += 1
        record.last_passive_event_at = timestamp
        record.passive_events.append(PassiveEvidenceEvent(timestamp, source, content_id))

        self._apply_passive_transitions(record)
        return True

    def mark_known(self, user_id: str, word: str, timestamp: datetime) -> None:
        """Explicitly mark a word as at least PASSIVE."""
        record = self._get_or_create(user_id, word)

        if record.state < LearningState.PASSIVE:
            record.state = LearningState.PASSIVE

        if record.passive_evidence_count < self.config.passive_threshold:
            record.passive_evidence_count = self.config.passive_threshold

        record.last_passive_event_at = timestamp

    def mark_unknown(self, user_id: str, word: str, timestamp: datetime) -> None:
        """Reset a word to UNKNOWN, clearing all evidence."""
        record = self._get_or_create(user_id, word)
        record.state                  = LearningState.UNKNOWN
        record.passive_evidence_count = 0
        record.active_success_count   = 0
        record.last_passive_event_at  = None
        record.last_active_event_at   = None
        record.seen_content_ids       = set()
        record.passive_events.clear()
        record.active_events.clear()

    def record_active_success(
        self,
        user_id:   str,
        word:      str,
        timestamp: datetime,
        correct:   bool = True,
    ) -> bool:
        """
        Record an active recall attempt.
        Returns True if accepted; False if rejected.
        """
        if not correct:
            return False

        record = self._get_or_create(user_id, word)

        if record.state < LearningState.PASSIVE:
            return False

        if record.last_active_event_at is not None:
            elapsed = timestamp - record.last_active_event_at
            if elapsed < self.config.active_min_gap:
                return False

        record.active_success_count += 1
        record.last_active_event_at = timestamp
        record.active_events.append(ActiveEvidenceEvent(timestamp, correct=True))

        self._apply_active_transitions(record)
        return True

    def get_state(self, user_id: str, word: str) -> LearningState:
        """Return current state; UNKNOWN for words never seen."""
        record = self._store.get((user_id, word))
        return record.state if record else LearningState.UNKNOWN

    def get_knowledge(self, user_id: str, word: str) -> Optional[WordKnowledge]:
        """Return the full WordKnowledge record, or None if not in the store."""
        return self._store.get((user_id, word))

    def reset_user(self, user_id: str) -> None:
        """Remove all records for a user."""
        to_remove = [k for k in self._store if k[0] == user_id]
        for k in to_remove:
            del self._store[k]

    def _get_or_create(self, user_id: str, word: str) -> WordKnowledge:
        key = (user_id, word)
        if key not in self._store:
            self._store[key] = WordKnowledge(user_id=user_id, word=word)
        return self._store[key]

    def _apply_passive_transitions(self, record: WordKnowledge) -> None:
        count = record.passive_evidence_count
        if record.state < LearningState.PASSIVE and count >= self.config.passive_threshold:
            record.state = LearningState.PASSIVE
        elif record.state < LearningState.FAMILIAR and count >= self.config.familiar_threshold:
            record.state = LearningState.FAMILIAR

    def _apply_active_transitions(self, record: WordKnowledge) -> None:
        if (record.state == LearningState.PASSIVE
                and record.active_success_count >= self.config.active_threshold):
            record.state = LearningState.ACTIVE
