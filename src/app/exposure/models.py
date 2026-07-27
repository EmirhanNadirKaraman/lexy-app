from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from app.learning.units import LearningUnit


class DuplicateRule(str, Enum):
    """Controls how repeated occurrences of the same utterance are counted."""
    ALLOW_ALL = "allow_all"
    DEDUPLICATE_UTTERANCE = "deduplicate_utterance"
    DEDUPLICATE_SESSION = "deduplicate_session"
    DIMINISHING_RETURNS = "diminishing_returns"


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


@dataclass(frozen=True)
class ExposureEvent:
    """An immutable record of one qualified exposure."""
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


@dataclass(frozen=True)
class ExposureStats:
    """A read-only summary of all qualified exposures for one (user, unit) pair."""
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
