"""Immutable, presentation-independent read models for member analytics."""

from dataclasses import dataclass
from datetime import timedelta

from discord_stats_bot.features.server_analytics import (
    AnalyticsCoverage,
    AnalyticsDailyPoint,
    AnalyticsMetric,
    AnalyticsVoiceMetric,
    AnalyticsWindow,
    ServerAnalyticsPeriod,
)


@dataclass(frozen=True, slots=True)
class MemberAnalyticsReport:
    """One member's current/previous completed-local-day analytics snapshot."""

    user_id: int
    period: ServerAnalyticsPeriod
    window: AnalyticsWindow
    voice_person_time: AnalyticsVoiceMetric
    messages: AnalyticsMetric
    active_days: AnalyticsMetric
    daily: tuple[AnalyticsDailyPoint, ...]
    voice_coverage: AnalyticsCoverage
    text_coverage: AnalyticsCoverage

    def __post_init__(self) -> None:
        if self.user_id <= 0:
            raise ValueError("user_id must be positive")
        if not isinstance(self.period, ServerAnalyticsPeriod):
            raise ValueError("period must be a ServerAnalyticsPeriod")

        current_days = (
            self.window.current_ended_before - self.window.current_started_on
        ).days
        previous_days = (
            self.window.previous_ended_before - self.window.previous_started_on
        ).days
        if current_days != self.period.days or previous_days != self.period.days:
            raise ValueError("analytics window must match the requested period")
        expected_dates = tuple(
            self.window.current_started_on + timedelta(days=offset)
            for offset in range(current_days)
        )
        if tuple(point.local_date for point in self.daily) != expected_dates:
            raise ValueError(
                "daily series must contain every current-window date exactly once"
            )
        if self.active_days.current > current_days:
            raise ValueError("current active days cannot exceed the current window")
        if self.active_days.previous > previous_days:
            raise ValueError("previous active days cannot exceed the previous window")
