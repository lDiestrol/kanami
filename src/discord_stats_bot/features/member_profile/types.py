"""Discord-independent member profile values and role configuration."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from discord_stats_bot.features.voice.types import normalize_observed_at


class KanamiMemberRole(StrEnum):
    """Stable role keys ordered separately from Discord's display names."""

    GUEST = "guest"
    INITIATED = "initiated"
    GUARDIAN = "guardian"
    PURPLE = "purple"
    GOLD = "gold"


KANAMI_MEMBER_ROLE_LABELS = {
    KanamiMemberRole.GUEST: "Гость",
    KanamiMemberRole.INITIATED: "Посвящённый",
    KanamiMemberRole.GUARDIAN: "Страж",
    KanamiMemberRole.PURPLE: "Фиолетовый",
    KanamiMemberRole.GOLD: "Золотой",
}


@dataclass(frozen=True, slots=True)
class MemberRoleConfiguration:
    """Optional stable Discord role IDs used by member-statistics policies."""

    guest_role_id: int | None = None
    initiated_role_id: int | None = None
    guardian_role_id: int | None = None
    purple_role_id: int | None = None
    gold_role_id: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "guest_role_id",
            "initiated_role_id",
            "guardian_role_id",
            "purple_role_id",
            "gold_role_id",
        ):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be positive when configured")


@dataclass(frozen=True, slots=True)
class MemberProfileSubject:
    """Current member identity supplied by an adapter such as Discord."""

    user_id: int
    display_name: str
    joined_at: datetime | None
    role_ids: frozenset[int] = frozenset()
    avatar_url: str | None = None

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        if not self.display_name:
            raise ValueError("display_name must not be empty")
        if self.joined_at is not None:
            object.__setattr__(self, "joined_at", normalize_observed_at(self.joined_at))


@dataclass(frozen=True, slots=True)
class MemberProfile:
    """Reusable Profile v1 result independent of Discord presentation."""

    as_of: datetime
    user_id: int
    display_name: str
    avatar_url: str | None
    role: KanamiMemberRole | None
    joined_on: date | None
    server_age_days: int | None
    voice_all_time_seconds: int
    voice_last_30_days_seconds: int
    achievement_count: int
    has_estimated_voice_time: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))
        for field_name in (
            "voice_all_time_seconds",
            "voice_last_30_days_seconds",
            "achievement_count",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must not be negative")
