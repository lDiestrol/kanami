"""Immutable, presentation-independent read models for server analytics."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import VoiceActivityReport


class ServerAnalyticsPeriod(StrEnum):
    """Supported counts of completed local calendar days."""

    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"

    @property
    def days(self) -> int:
        return {
            ServerAnalyticsPeriod.LAST_7_DAYS: 7,
            ServerAnalyticsPeriod.LAST_30_DAYS: 30,
        }[self]


@dataclass(frozen=True, slots=True)
class AnalyticsWindow:
    """Current and preceding completed-local-day windows in UTC and local dates."""

    as_of: datetime
    timezone_name: str
    current_started_at: datetime
    current_ended_at: datetime
    previous_started_at: datetime
    previous_ended_at: datetime
    current_started_on: date
    current_ended_before: date
    previous_started_on: date
    previous_ended_before: date

    def __post_init__(self) -> None:
        for field_name in (
            "as_of",
            "current_started_at",
            "current_ended_at",
            "previous_started_at",
            "previous_ended_at",
        ):
            object.__setattr__(
                self, field_name, normalize_observed_at(getattr(self, field_name))
            )
        if not self.timezone_name:
            raise ValueError("timezone_name must not be empty")
        if not (
            self.previous_started_at
            < self.previous_ended_at
            == self.current_started_at
            < self.current_ended_at
            <= self.as_of
        ):
            raise ValueError("analytics UTC windows must be contiguous and ordered")
        if not (
            self.previous_started_on
            < self.previous_ended_before
            == self.current_started_on
            < self.current_ended_before
        ):
            raise ValueError(
                "analytics local-date windows must be contiguous and ordered"
            )


class AnalyticsPercentState(StrEnum):
    """Typed interpretation of a current/previous percentage comparison."""

    AVAILABLE = "available"
    UNCHANGED_ZERO = "unchanged_zero"
    NO_BASELINE = "no_baseline"


@dataclass(frozen=True, slots=True)
class AnalyticsMetric:
    """One non-negative KPI with an explicit comparison state."""

    current: int
    previous: int
    absolute_delta: int
    percent_delta: float | None
    percent_state: AnalyticsPercentState

    def __post_init__(self) -> None:
        if self.current < 0 or self.previous < 0:
            raise ValueError("analytics metric values must not be negative")
        if self.absolute_delta != self.current - self.previous:
            raise ValueError("absolute_delta must equal current minus previous")
        if self.percent_state is AnalyticsPercentState.NO_BASELINE:
            if (
                self.previous != 0
                or self.current == 0
                or self.percent_delta is not None
            ):
                raise ValueError(
                    "no-baseline metric requires positive current and zero previous"
                )
        elif self.percent_state is AnalyticsPercentState.UNCHANGED_ZERO:
            if (self.current, self.previous, self.percent_delta) != (0, 0, 0.0):
                raise ValueError("unchanged-zero metric must represent two zero values")
        elif self.previous <= 0 or self.percent_delta is None:
            raise ValueError("available percentage requires a positive previous value")


@dataclass(frozen=True, slots=True)
class AnalyticsVoiceMetric:
    """Exact, estimated and combined voice person-time comparisons."""

    exact_seconds: AnalyticsMetric
    estimated_seconds: AnalyticsMetric
    total_seconds: AnalyticsMetric

    def __post_init__(self) -> None:
        for side in ("current", "previous"):
            if getattr(self.total_seconds, side) != (
                getattr(self.exact_seconds, side)
                + getattr(self.estimated_seconds, side)
            ):
                raise ValueError("voice total must equal exact plus estimated seconds")


@dataclass(frozen=True, slots=True)
class AnalyticsDailyPoint:
    """One zero-filled current-window local calendar day."""

    local_date: date
    voice_exact_seconds: int = 0
    voice_estimated_seconds: int = 0
    messages: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.voice_exact_seconds,
                self.voice_estimated_seconds,
                self.messages,
            )
            < 0
        ):
            raise ValueError("daily analytics values must not be negative")


@dataclass(frozen=True, slots=True)
class AnalyticsVoiceTopMember:
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
class AnalyticsMessageTopMember:
    user_id: int
    message_count: int

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        if self.message_count < 0:
            raise ValueError("message_count must not be negative")


@dataclass(frozen=True, slots=True)
class AnalyticsCoverage:
    """Earliest observed date without claiming an authoritative collector start."""

    earliest_recorded_on: date | None
    current_window_begins_before_earliest_recorded: bool | None
    previous_window_begins_before_earliest_recorded: bool | None

    def __post_init__(self) -> None:
        if self.earliest_recorded_on is None:
            if any(
                value is not None
                for value in (
                    self.current_window_begins_before_earliest_recorded,
                    self.previous_window_begins_before_earliest_recorded,
                )
            ):
                raise ValueError("an empty source cannot have coverage comparisons")
        elif any(
            value is None
            for value in (
                self.current_window_begins_before_earliest_recorded,
                self.previous_window_begins_before_earliest_recorded,
            )
        ):
            raise ValueError("a recorded source requires both coverage comparisons")

    @property
    def has_recorded_activity(self) -> bool:
        return self.earliest_recorded_on is not None


@dataclass(frozen=True, slots=True)
class ServerAnalyticsReport:
    """Complete backend snapshot consumed by the future Web Admin page."""

    window: AnalyticsWindow
    active_members: AnalyticsMetric
    voice_person_time: AnalyticsVoiceMetric
    messages: AnalyticsMetric
    unique_voice_users: AnalyticsMetric
    unique_message_authors: AnalyticsMetric
    daily: tuple[AnalyticsDailyPoint, ...]
    top_voice_members: tuple[AnalyticsVoiceTopMember, ...]
    top_message_authors: tuple[AnalyticsMessageTopMember, ...]
    voice_activity: VoiceActivityReport
    voice_coverage: AnalyticsCoverage
    text_coverage: AnalyticsCoverage

    def __post_init__(self) -> None:
        expected_days = (
            self.window.current_ended_before - self.window.current_started_on
        ).days
        if len(self.daily) != expected_days:
            raise ValueError("daily series must contain every current-window date")
        if len(self.top_voice_members) > 5 or len(self.top_message_authors) > 5:
            raise ValueError(
                "server analytics rankings must contain at most five entries"
            )


@dataclass(frozen=True, slots=True)
class AnalyticsQuery:
    """Persistence bounds and voice threshold for one report snapshot."""

    window: AnalyticsWindow
    min_exact_session_seconds: int

    def __post_init__(self) -> None:
        if self.min_exact_session_seconds <= 0:
            raise ValueError("min_exact_session_seconds must be positive")

    @property
    def as_of(self) -> datetime:
        """Structural compatibility with the shared voice eligibility query."""

        return self.window.current_ended_at


@dataclass(frozen=True, slots=True)
class AnalyticsVoiceInterval:
    user_id: int
    started_at: datetime
    ended_at: datetime
    quality: str

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        object.__setattr__(self, "started_at", normalize_observed_at(self.started_at))
        object.__setattr__(self, "ended_at", normalize_observed_at(self.ended_at))
        if self.ended_at <= self.started_at:
            raise ValueError("analytics voice interval must have positive duration")
        if self.quality not in {"exact", "estimated"}:
            raise ValueError("analytics voice quality must be exact or estimated")


@dataclass(frozen=True, slots=True)
class AnalyticsTextRow:
    user_id: int
    activity_date: date
    message_count: int

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        if self.message_count < 0:
            raise ValueError("message_count must not be negative")


@dataclass(frozen=True, slots=True)
class AnalyticsEarliestRecorded:
    voice_started_at: datetime | None
    text_activity_date: date | None

    def __post_init__(self) -> None:
        if self.voice_started_at is not None:
            object.__setattr__(
                self,
                "voice_started_at",
                normalize_observed_at(self.voice_started_at),
            )
