"""Discord-independent types for read-only voice statistics."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from discord_stats_bot.features.voice.types import normalize_observed_at


@dataclass(frozen=True, slots=True)
class VoicePeriodDurations:
    """Exact and recovered estimated seconds for one reporting period."""

    exact_seconds: int = 0
    estimated_seconds: int = 0

    def __post_init__(self) -> None:
        if self.exact_seconds < 0 or self.estimated_seconds < 0:
            raise ValueError("voice durations must not be negative")

    @property
    def total_seconds(self) -> int:
        return self.exact_seconds + self.estimated_seconds


@dataclass(frozen=True, slots=True)
class VoiceStatistics:
    """Voice durations for the four user-facing reporting periods."""

    as_of: datetime
    today: VoicePeriodDurations
    last_7_days: VoicePeriodDurations
    last_30_days: VoicePeriodDurations
    all_time: VoicePeriodDurations

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))

    @property
    def has_estimated_time(self) -> bool:
        return any(
            period.estimated_seconds > 0
            for period in (
                self.today,
                self.last_7_days,
                self.last_30_days,
                self.all_time,
            )
        )


@dataclass(frozen=True, slots=True)
class VoiceStatisticsQuery:
    """UTC boundaries required by the persistence aggregation."""

    as_of: datetime
    today_started_at: datetime
    last_7_days_started_at: datetime
    last_30_days_started_at: datetime
    min_exact_session_seconds: int

    def __post_init__(self) -> None:
        for field_name in (
            "as_of",
            "today_started_at",
            "last_7_days_started_at",
            "last_30_days_started_at",
        ):
            object.__setattr__(
                self,
                field_name,
                normalize_observed_at(getattr(self, field_name)),
            )
        if self.min_exact_session_seconds <= 0:
            raise ValueError("min_exact_session_seconds must be positive")

    def started_at_for(self, period: "VoiceStatisticsPeriod") -> datetime | None:
        """Return the selected reporting lower boundary."""

        return {
            VoiceStatisticsPeriod.TODAY: self.today_started_at,
            VoiceStatisticsPeriod.LAST_7_DAYS: self.last_7_days_started_at,
            VoiceStatisticsPeriod.LAST_30_DAYS: self.last_30_days_started_at,
            VoiceStatisticsPeriod.ALL_TIME: None,
        }[period]


class VoiceStatisticsPeriod(StrEnum):
    """Stable application values for supported reporting periods."""

    TODAY = "today"
    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"
    ALL_TIME = "all"


@dataclass(frozen=True, slots=True)
class VoiceProfileWindow:
    """Selected/current and optional equal-length comparison boundaries."""

    period: VoiceStatisticsPeriod
    started_at: datetime | None
    previous_started_at: datetime | None
    previous_ended_at: datetime | None

    def __post_init__(self) -> None:
        for field_name in ("started_at", "previous_started_at", "previous_ended_at"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, normalize_observed_at(value))
        if self.period is VoiceStatisticsPeriod.ALL_TIME:
            if any(
                value is not None
                for value in (
                    self.started_at,
                    self.previous_started_at,
                    self.previous_ended_at,
                )
            ):
                raise ValueError("all-time profile window must be unbounded")
        elif None in (
            self.started_at,
            self.previous_started_at,
            self.previous_ended_at,
        ):
            raise ValueError("finite profile window must include comparison boundaries")


@dataclass(frozen=True, slots=True)
class VoicePeriodStanding:
    """One user's position in a complete guild ranking for one period."""

    rank: int | None
    participant_count: int

    def __post_init__(self) -> None:
        if self.participant_count < 0:
            raise ValueError("participant_count must not be negative")
        if self.rank is not None and self.rank < 1:
            raise ValueError("rank must be positive when present")
        if self.participant_count == 0 and self.rank is not None:
            raise ValueError("rank must be absent when participant_count is zero")
        if self.rank is not None and self.rank > self.participant_count:
            raise ValueError("rank must not exceed participant_count")


@dataclass(frozen=True, slots=True)
class VoiceUserStandings:
    """One user's guild ranking positions for all reporting periods."""

    as_of: datetime
    today: VoicePeriodStanding
    last_7_days: VoicePeriodStanding
    last_30_days: VoicePeriodStanding
    all_time: VoicePeriodStanding

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))


@dataclass(frozen=True, slots=True)
class VoiceLeaderboardEntry:
    """One persistence-ranked guild member independent of Discord cache."""

    user_id: int
    exact_seconds: int
    estimated_seconds: int

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        if self.exact_seconds < 0 or self.estimated_seconds < 0:
            raise ValueError("voice durations must not be negative")

    @property
    def total_seconds(self) -> int:
        return self.exact_seconds + self.estimated_seconds


@dataclass(frozen=True, slots=True)
class VoiceLeaderboard:
    """Top voice users for one reporting period and one UTC snapshot."""

    as_of: datetime
    period: VoiceStatisticsPeriod
    entries: tuple[VoiceLeaderboardEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))
        if len(self.entries) > 10:
            raise ValueError("voice leaderboard must contain at most 10 entries")


@dataclass(frozen=True, slots=True)
class VoiceChannelUsageEntry:
    """Aggregated usage of one persisted voice interval channel."""

    channel_id: int
    exact_seconds: int
    estimated_seconds: int

    def __post_init__(self) -> None:
        if self.channel_id <= 0:
            raise ValueError("channel_id must be positive")
        if self.exact_seconds < 0 or self.estimated_seconds < 0:
            raise ValueError("voice durations must not be negative")

    @property
    def total_seconds(self) -> int:
        return self.exact_seconds + self.estimated_seconds


@dataclass(frozen=True, slots=True)
class VoiceUserTopChannels:
    """One user's all-time favorite voice channels."""

    as_of: datetime
    entries: tuple[VoiceChannelUsageEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))
        if len(self.entries) > 3:
            raise ValueError("user top channels must contain at most 3 entries")


@dataclass(frozen=True, slots=True)
class VoiceCompanionEntry:
    """All-time overlap with one eligible non-bot guild member."""

    user_id: int
    exact_seconds: int
    estimated_seconds: int

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        if self.exact_seconds < 0 or self.estimated_seconds < 0:
            raise ValueError("voice durations must not be negative")

    @property
    def total_seconds(self) -> int:
        return self.exact_seconds + self.estimated_seconds


@dataclass(frozen=True, slots=True)
class VoiceFavoriteChannel:
    """Favorite persisted channel for the selected profile period."""

    channel_id: int
    channel_name: str | None
    exact_seconds: int
    estimated_seconds: int

    def __post_init__(self) -> None:
        if self.channel_id <= 0:
            raise ValueError("channel_id must be positive")
        if self.exact_seconds < 0 or self.estimated_seconds < 0:
            raise ValueError("voice durations must not be negative")

    @property
    def total_seconds(self) -> int:
        return self.exact_seconds + self.estimated_seconds


@dataclass(frozen=True, slots=True)
class VoiceUserProfileCore:
    """Persistence aggregate for one selected-period voice profile."""

    as_of: datetime
    period: VoiceStatisticsPeriod
    durations: VoicePeriodDurations
    standing: VoicePeriodStanding
    session_count: int
    favorite_channel: VoiceFavoriteChannel | None
    previous_durations: VoicePeriodDurations | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))
        if self.session_count < 0:
            raise ValueError("session_count must not be negative")
        if self.period is VoiceStatisticsPeriod.ALL_TIME:
            if self.previous_durations is not None:
                raise ValueError("all-time profile must not include a comparison")
        elif self.previous_durations is None:
            raise ValueError("finite profile must include a comparison")


@dataclass(frozen=True, slots=True)
class VoiceUserProfile:
    """Complete selected-period profile returned to the presentation layer."""

    core: VoiceUserProfileCore
    companions: tuple[VoiceCompanionEntry, ...]

    def __post_init__(self) -> None:
        if len(self.companions) > 3:
            raise ValueError("voice profile must contain at most 3 companions")

    @property
    def average_session_seconds(self) -> int:
        if self.core.session_count == 0:
            return 0
        return self.core.durations.total_seconds // self.core.session_count


@dataclass(frozen=True, slots=True)
class VoiceUserTopCompanions:
    """One user's all-time TOP 3 voice companions."""

    as_of: datetime
    entries: tuple[VoiceCompanionEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))
        if len(self.entries) > 3:
            raise ValueError("user top companions must contain at most 3 entries")


@dataclass(frozen=True, slots=True)
class VoicePairStatistics:
    """All-time voice overlap and shares for two guild members."""

    as_of: datetime
    user1_id: int
    user2_id: int
    exact_seconds: int
    estimated_seconds: int
    user1_total_seconds: int
    user2_total_seconds: int
    channels: tuple[VoiceChannelUsageEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))
        if self.user1_id <= 0 or self.user2_id <= 0:
            raise ValueError("user IDs must be positive")
        if self.user1_id == self.user2_id:
            raise ValueError("pair users must be different")
        if any(
            seconds < 0
            for seconds in (
                self.exact_seconds,
                self.estimated_seconds,
                self.user1_total_seconds,
                self.user2_total_seconds,
            )
        ):
            raise ValueError("voice durations must not be negative")
        if len(self.channels) > 3:
            raise ValueError("pair statistics must contain at most 3 channels")

    @property
    def total_seconds(self) -> int:
        return self.exact_seconds + self.estimated_seconds


@dataclass(frozen=True, slots=True)
class VoiceChannelLeaderboard:
    """Top guild voice channels for one reporting period."""

    as_of: datetime
    period: VoiceStatisticsPeriod
    entries: tuple[VoiceChannelUsageEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))
        if len(self.entries) > 10:
            raise ValueError("channel leaderboard must contain at most 10 entries")


@dataclass(frozen=True, slots=True)
class VoiceChannelStatistics:
    """One channel's total and persistence-ranked TOP 10 users."""

    as_of: datetime
    period: VoiceStatisticsPeriod
    channel_id: int
    exact_seconds: int
    estimated_seconds: int
    entries: tuple[VoiceLeaderboardEntry, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))
        if self.channel_id <= 0:
            raise ValueError("channel_id must be positive")
        if self.exact_seconds < 0 or self.estimated_seconds < 0:
            raise ValueError("voice durations must not be negative")
        if len(self.entries) > 10:
            raise ValueError("channel statistics must contain at most 10 entries")

    @property
    def total_seconds(self) -> int:
        return self.exact_seconds + self.estimated_seconds


@dataclass(frozen=True, slots=True)
class VoiceServerStatistics:
    """Compact server-wide voice overview for one reporting period."""

    as_of: datetime
    period: VoiceStatisticsPeriod
    exact_seconds: int
    estimated_seconds: int
    active_users: int
    top_user: VoiceLeaderboardEntry | None
    top_channel: VoiceChannelUsageEntry | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))
        if self.exact_seconds < 0 or self.estimated_seconds < 0:
            raise ValueError("voice durations must not be negative")
        if self.active_users < 0:
            raise ValueError("active_users must not be negative")
        if self.active_users == 0 and self.top_user is not None:
            raise ValueError("top user must be absent when there are no active users")

    @property
    def total_seconds(self) -> int:
        return self.exact_seconds + self.estimated_seconds

    @property
    def average_seconds(self) -> int:
        if self.active_users == 0:
            return 0
        return self.total_seconds // self.active_users
