"""Application orchestration for one member analytics snapshot."""

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from discord_stats_bot.features.member_analytics.types import MemberAnalyticsReport
from discord_stats_bot.features.server_analytics import (
    AnalyticsCoverage,
    AnalyticsDailyPoint,
    AnalyticsEarliestRecorded,
    AnalyticsQuery,
    AnalyticsTextRow,
    AnalyticsVoiceInterval,
    AnalyticsVoiceMetric,
    AnalyticsWindow,
    ServerAnalyticsPeriod,
    analytics_metric,
    build_analytics_window,
)


class MemberAnalyticsRepository(Protocol):
    """Member-scoped persistence reads required for one analytics report."""

    async def list_voice_intervals(
        self,
        guild_id: int,
        user_id: int,
        query: AnalyticsQuery,
    ) -> tuple[AnalyticsVoiceInterval, ...]: ...

    async def list_text_rows(
        self,
        guild_id: int,
        user_id: int,
        query: AnalyticsQuery,
    ) -> tuple[AnalyticsTextRow, ...]: ...

    async def get_earliest_recorded(
        self,
        guild_id: int,
        user_id: int,
    ) -> AnalyticsEarliestRecorded: ...


class MemberAnalyticsService:
    """Combine bounded member Voice/Text reads into one immutable report."""

    def __init__(
        self,
        repository: MemberAnalyticsRepository,
        *,
        report_timezone: ZoneInfo,
        min_session_seconds: int,
    ) -> None:
        if min_session_seconds <= 0:
            raise ValueError("min_session_seconds must be positive")
        self._repository = repository
        self._report_timezone = report_timezone
        self._min_session_seconds = min_session_seconds

    async def get_report(
        self,
        guild_id: int,
        user_id: int,
        period: ServerAnalyticsPeriod,
        as_of: datetime,
    ) -> MemberAnalyticsReport:
        """Return member analytics for adjacent completed-local-day windows."""

        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if user_id <= 0:
            raise ValueError("user_id must be positive")
        window = build_analytics_window(
            period,
            as_of,
            report_timezone=self._report_timezone,
        )
        query = AnalyticsQuery(window, self._min_session_seconds)
        voice_intervals = await self._repository.list_voice_intervals(
            guild_id, user_id, query
        )
        text_rows = await self._repository.list_text_rows(guild_id, user_id, query)
        earliest = await self._repository.get_earliest_recorded(guild_id, user_id)
        return self._build_report(
            user_id,
            period,
            window,
            voice_intervals,
            text_rows,
            earliest,
        )

    def _build_report(
        self,
        user_id: int,
        period: ServerAnalyticsPeriod,
        window: AnalyticsWindow,
        voice_intervals: tuple[AnalyticsVoiceInterval, ...],
        text_rows: tuple[AnalyticsTextRow, ...],
        earliest: AnalyticsEarliestRecorded,
    ) -> MemberAnalyticsReport:
        voice_seconds = {
            "current": defaultdict(float),
            "previous": defaultdict(float),
        }
        voice_active_dates: dict[str, set[date]] = {
            "current": set(),
            "previous": set(),
        }
        daily_voice: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])

        for interval in voice_intervals:
            for name, started_at, ended_at in (
                (
                    "previous",
                    window.previous_started_at,
                    window.previous_ended_at,
                ),
                ("current", window.current_started_at, window.current_ended_at),
            ):
                bounds = _overlap_bounds(interval, started_at, ended_at)
                if bounds is None:
                    continue
                overlap_start, overlap_end = bounds
                voice_seconds[name][interval.quality] += (
                    overlap_end - overlap_start
                ).total_seconds()
                for local_date, seconds in _split_local_dates(
                    overlap_start,
                    overlap_end,
                    self._report_timezone,
                ):
                    if seconds <= 0:
                        continue
                    voice_active_dates[name].add(local_date)
                    if name == "current":
                        quality_index = 0 if interval.quality == "exact" else 1
                        daily_voice[local_date][quality_index] += seconds

        text_totals = {"current": 0, "previous": 0}
        text_active_dates: dict[str, set[date]] = {
            "current": set(),
            "previous": set(),
        }
        daily_messages: defaultdict[date, int] = defaultdict(int)
        for row in text_rows:
            if (
                window.previous_started_on
                <= row.activity_date
                < window.previous_ended_before
            ):
                name = "previous"
            elif (
                window.current_started_on
                <= row.activity_date
                < window.current_ended_before
            ):
                name = "current"
            else:
                continue
            text_totals[name] += row.message_count
            if row.message_count > 0:
                text_active_dates[name].add(row.activity_date)
            if name == "current":
                daily_messages[row.activity_date] += row.message_count

        exact = analytics_metric(
            int(voice_seconds["current"]["exact"]),
            int(voice_seconds["previous"]["exact"]),
        )
        estimated = analytics_metric(
            int(voice_seconds["current"]["estimated"]),
            int(voice_seconds["previous"]["estimated"]),
        )
        total = analytics_metric(
            exact.current + estimated.current,
            exact.previous + estimated.previous,
        )
        daily = tuple(
            AnalyticsDailyPoint(
                local_date=local_date,
                voice_exact_seconds=int(daily_voice[local_date][0]),
                voice_estimated_seconds=int(daily_voice[local_date][1]),
                messages=daily_messages[local_date],
            )
            for local_date in _dates(
                window.current_started_on,
                window.current_ended_before,
            )
        )
        return MemberAnalyticsReport(
            user_id=user_id,
            period=period,
            window=window,
            voice_person_time=AnalyticsVoiceMetric(exact, estimated, total),
            messages=analytics_metric(text_totals["current"], text_totals["previous"]),
            active_days=analytics_metric(
                len(voice_active_dates["current"] | text_active_dates["current"]),
                len(voice_active_dates["previous"] | text_active_dates["previous"]),
            ),
            daily=daily,
            voice_coverage=_coverage(
                (
                    earliest.voice_started_at.astimezone(self._report_timezone).date()
                    if earliest.voice_started_at is not None
                    else None
                ),
                window,
            ),
            text_coverage=_coverage(earliest.text_activity_date, window),
        )


def _overlap_bounds(
    interval: AnalyticsVoiceInterval,
    started_at: datetime,
    ended_at: datetime,
) -> tuple[datetime, datetime] | None:
    start = max(interval.started_at, started_at)
    end = min(interval.ended_at, ended_at)
    return (start, end) if end > start else None


def _split_local_dates(
    started_at: datetime,
    ended_at: datetime,
    timezone: ZoneInfo,
) -> tuple[tuple[date, float], ...]:
    cursor = started_at
    parts: list[tuple[date, float]] = []
    while cursor < ended_at:
        local_date = cursor.astimezone(timezone).date()
        next_midnight = datetime.combine(
            local_date + timedelta(days=1),
            time.min,
            tzinfo=timezone,
        ).astimezone(UTC)
        boundary = min(next_midnight, ended_at)
        if boundary <= cursor:  # pragma: no cover - defensive for exotic zone changes
            raise ValueError("could not resolve the next local date boundary")
        parts.append((local_date, (boundary - cursor).total_seconds()))
        cursor = boundary
    return tuple(parts)


def _dates(started_on: date, ended_before: date) -> tuple[date, ...]:
    return tuple(
        started_on + timedelta(days=offset)
        for offset in range((ended_before - started_on).days)
    )


def _coverage(earliest: date | None, window: AnalyticsWindow) -> AnalyticsCoverage:
    if earliest is None:
        return AnalyticsCoverage(None, None, None)
    return AnalyticsCoverage(
        earliest,
        window.current_started_on < earliest,
        window.previous_started_on < earliest,
    )
