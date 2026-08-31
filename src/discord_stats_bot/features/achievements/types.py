"""Discord-independent domain types for achievement evaluation and unlocks."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

_ACHIEVEMENT_KEY_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}")


class AchievementCategory(StrEnum):
    """Stable catalog categories independent of presentation."""

    VOICE = "voice"
    COMMUNITY = "community"
    TEXT = "text"


class AchievementMetric(StrEnum):
    """Metrics understood by the pure evaluator."""

    VOICE_SECONDS = "voice_seconds"
    SERVER_AGE_DAYS = "server_age_days"
    MESSAGE_COUNT = "message_count"


class AchievementTier(StrEnum):
    """Optional presentation tier which is not achievement identity."""

    BRONZE = "bronze"
    SILVER = "silver"
    GOLD = "gold"


def validate_achievement_key(value: str) -> str:
    """Validate a stable persistence-safe achievement key."""

    if not isinstance(value, str) or _ACHIEVEMENT_KEY_PATTERN.fullmatch(value) is None:
        raise ValueError("achievement_key must match [a-z][a-z0-9_]{0,127}")
    return value


def normalize_utc_datetime(value: datetime, field_name: str) -> datetime:
    """Reject naive values and normalize an aware timestamp to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_metric_value(value: int | None, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer or None")
    if value < 0:
        raise ValueError(f"{field_name} must not be negative")


@dataclass(frozen=True, slots=True)
class AchievementDefinition:
    """Immutable code-defined achievement definition."""

    key: str
    title: str
    description: str
    category: AchievementCategory
    metric: AchievementMetric
    threshold: int
    tier: AchievementTier | None = None

    def __post_init__(self) -> None:
        validate_achievement_key(self.key)
        if not self.title.strip():
            raise ValueError("achievement title must not be empty")
        if not self.description.strip():
            raise ValueError("achievement description must not be empty")
        if not isinstance(self.threshold, int) or isinstance(self.threshold, bool):
            raise TypeError("achievement threshold must be an integer")
        if self.threshold <= 0:
            raise ValueError("achievement threshold must be positive")


@dataclass(frozen=True, slots=True)
class AchievementMetricSnapshot:
    """Available metric values for one user; None means unavailable."""

    voice_seconds: int | None = None
    server_age_days: int | None = None
    message_count: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("voice_seconds", "server_age_days", "message_count"):
            _validate_metric_value(getattr(self, field_name), field_name)

    def value_for(self, metric: AchievementMetric) -> int | None:
        """Return the snapshot value corresponding to a catalog metric."""

        return {
            AchievementMetric.VOICE_SECONDS: self.voice_seconds,
            AchievementMetric.SERVER_AGE_DAYS: self.server_age_days,
            AchievementMetric.MESSAGE_COUNT: self.message_count,
        }[metric]


@dataclass(frozen=True, slots=True)
class AchievementCandidate:
    """One catalog definition satisfied by a metric snapshot."""

    definition: AchievementDefinition
    metric_value: int

    def __post_init__(self) -> None:
        _validate_metric_value(self.metric_value, "metric_value")
        if self.metric_value < self.definition.threshold:
            raise ValueError("candidate metric_value must satisfy its threshold")

    @property
    def key(self) -> str:
        return self.definition.key


@dataclass(frozen=True, slots=True)
class UnlockedAchievement:
    """Persisted achievement identity and first-unlock timestamp."""

    guild_id: int
    user_id: int
    achievement_key: str
    unlocked_at: datetime

    def __post_init__(self) -> None:
        if self.guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        validate_achievement_key(self.achievement_key)
        object.__setattr__(
            self,
            "unlocked_at",
            normalize_utc_datetime(self.unlocked_at, "unlocked_at"),
        )
