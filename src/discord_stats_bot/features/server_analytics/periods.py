"""Completed-local-calendar period construction for server analytics."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from discord_stats_bot.features.server_analytics.types import (
    AnalyticsWindow,
    ServerAnalyticsPeriod,
)
from discord_stats_bot.features.voice.types import normalize_observed_at


def build_analytics_window(
    period: ServerAnalyticsPeriod,
    as_of: datetime,
    *,
    report_timezone: ZoneInfo,
) -> AnalyticsWindow:
    """Build adjacent current/previous completed local-day windows."""

    if not isinstance(period, ServerAnalyticsPeriod):
        raise ValueError("period must be a ServerAnalyticsPeriod")
    as_of = normalize_observed_at(as_of)
    current_ended_before = as_of.astimezone(report_timezone).date()
    current_started_on = current_ended_before - timedelta(days=period.days)
    previous_ended_before = current_started_on
    previous_started_on = previous_ended_before - timedelta(days=period.days)

    def midnight(local_date: date) -> datetime:
        return datetime.combine(
            local_date, time.min, tzinfo=report_timezone
        ).astimezone(UTC)

    return AnalyticsWindow(
        as_of=as_of,
        timezone_name=report_timezone.key,
        current_started_at=midnight(current_started_on),
        current_ended_at=midnight(current_ended_before),
        previous_started_at=midnight(previous_started_on),
        previous_ended_at=midnight(previous_ended_before),
        current_started_on=current_started_on,
        current_ended_before=current_ended_before,
        previous_started_on=previous_started_on,
        previous_ended_before=previous_ended_before,
    )
