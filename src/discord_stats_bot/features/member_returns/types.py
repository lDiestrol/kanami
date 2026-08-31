"""Discord-independent types for durable member return notifications."""

from dataclasses import dataclass
from datetime import datetime

from discord_stats_bot.features.voice.types import normalize_observed_at

MEMBER_RETURN_EVENT_TYPE = "member.returned"


@dataclass(frozen=True, slots=True)
class MemberReturnSnapshot:
    """Gateway values that identify one concrete guild rejoin."""

    guild_id: int
    user_id: int
    joined_at: datetime
    is_bot: bool = False

    def __post_init__(self) -> None:
        if self.guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        object.__setattr__(self, "joined_at", normalize_observed_at(self.joined_at))


@dataclass(frozen=True, slots=True)
class MemberReturnEvent:
    """Immutable statistics snapshot stored with ``member.returned``."""

    guild_id: int
    user_id: int
    previous_left_at: datetime
    returned_at: datetime
    absence_seconds: int
    voice_seconds: int
    message_count: int
    achievement_count: int
    return_number: int

    def __post_init__(self) -> None:
        for field_name in ("guild_id", "user_id", "return_number"):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        for field_name in (
            "absence_seconds",
            "voice_seconds",
            "message_count",
            "achievement_count",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must not be negative")
        object.__setattr__(
            self,
            "previous_left_at",
            normalize_observed_at(self.previous_left_at),
        )
        object.__setattr__(
            self,
            "returned_at",
            normalize_observed_at(self.returned_at),
        )
        if self.returned_at < self.previous_left_at:
            raise ValueError("returned_at must not precede previous_left_at")
