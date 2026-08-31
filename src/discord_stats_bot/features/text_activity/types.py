"""Discord-independent types for daily text activity."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum


def normalize_occurred_at(value: datetime) -> datetime:
    """Validate a message timestamp and normalize it to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class TextMessageActivity:
    """Metadata required to count one message without retaining its content.

    ``attachment_count`` is the number of attachments on the message, not a
    boolean count of messages that contain attachments.
    """

    guild_id: int
    user_id: int
    channel_id: int
    occurred_at: datetime
    attachment_count: int = 0
    is_reply: bool = False

    def __post_init__(self) -> None:
        for field_name in ("guild_id", "user_id", "channel_id"):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.attachment_count < 0:
            raise ValueError("attachment_count must not be negative")
        object.__setattr__(
            self,
            "occurred_at",
            normalize_occurred_at(self.occurred_at),
        )


@dataclass(frozen=True, slots=True)
class TextUserMessageCount:
    """Aggregated message count for one guild member."""

    user_id: int
    message_count: int

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        if self.message_count < 0:
            raise ValueError("message_count must not be negative")


class TextActivityPeriod(StrEnum):
    """Stable application values for text leaderboard periods."""

    TODAY = "today"
    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"
    ALL_TIME = "all"


@dataclass(frozen=True, slots=True)
class TextActivityDateRange:
    """Inclusive reporting dates used by one leaderboard query."""

    started_on: date | None
    ended_on: date

    def __post_init__(self) -> None:
        if self.started_on is not None and self.ended_on < self.started_on:
            raise ValueError("ended_on must not be earlier than started_on")


@dataclass(frozen=True, slots=True)
class TextActivityLeaderboard:
    """Ranked message totals for one reporting period."""

    as_of: datetime
    period: TextActivityPeriod
    entries: tuple[TextUserMessageCount, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_occurred_at(self.as_of))
