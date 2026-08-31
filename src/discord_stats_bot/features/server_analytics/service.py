"""Application orchestration for one consistent server analytics snapshot."""

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from discord_stats_bot.features.server_analytics.periods import build_analytics_window
from discord_stats_bot.features.server_analytics.types import (
    AnalyticsCoverage,
    AnalyticsDailyPoint,
    AnalyticsEarliestRecorded,
    AnalyticsMessageTopMember,
    AnalyticsMetric,
    AnalyticsPercentState,
    AnalyticsQuery,
    AnalyticsTextRow,
    AnalyticsVoiceInterval,
    AnalyticsVoiceMetric,
    AnalyticsVoiceTopMember,
    AnalyticsWindow,
    ServerAnalyticsPeriod,
    ServerAnalyticsReport,
)
from discord_stats_bot.features.voice_statistics import (
    VoiceActivityInterval,
    VoiceActivityPeriod,
    aggregate_voice_activity_window,
)


class ServerAnalyticsRepository(Protocol):
    """Persistence reads required to build a complete report."""

    async def list_voice_intervals(
        self, guild_id: int, query: AnalyticsQuery
    ) -> tuple[AnalyticsVoiceInterval, ...]: ...

    async def list_text_rows(
        self, guild_id: int, query: AnalyticsQuery
    ) -> tuple[AnalyticsTextRow, ...]: ...

    async def get_earliest_recorded(
        self, guild_id: int
    ) -> AnalyticsEarliestRecorded: ...


def analytics_metric(current: int, previous: int) -> AnalyticsMetric:
    """Build an integer comparison without magic infinity values."""

    if current < 0 or previous < 0:
        raise ValueError("analytics metric values must not be negative")
    absolute_delta = current - previous
    if previous > 0:
        state = AnalyticsPercentState.AVAILABLE
        percent_delta: float | None = absolute_delta / previous * 100
    elif current == 0:
        state = AnalyticsPercentState.UNCHANGED_ZERO
        percent_delta = 0.0
    else:
        state = AnalyticsPercentState.NO_BASELINE
        percent_delta = None
    return AnalyticsMetric(
        current=current,
        previous=previous,
        absolute_delta=absolute_delta,
        percent_delta=percent_delta,
        percent_state=state,
    )


class ServerAnalyticsService:
    """Combine voice/text reads into one immutable analytics report."""

    def __init__(
        self,
        repository: ServerAnalyticsRepository,
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
        period: ServerAnalyticsPeriod,
        as_of: datetime,
    ) -> ServerAnalyticsReport:
        """Return current/previous data from one caller-provided snapshot time."""

        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        window = build_analytics_window(
            period,
            as_of,
            report_timezone=self._report_timezone,
        )
        query = AnalyticsQuery(window, self._min_session_seconds)
        voice_intervals = await self._repository.list_voice_intervals(guild_id, query)
        text_rows = await self._repository.list_text_rows(guild_id, query)
        earliest = await self._repository.get_earliest_recorded(guild_id)
        return self._build_report(period, window, voice_intervals, text_rows, earliest)

    def _build_report(
        self,
        period: ServerAnalyticsPeriod,
        window: AnalyticsWindow,
        voice_intervals: tuple[AnalyticsVoiceInterval, ...],
        text_rows: tuple[AnalyticsTextRow, ...],
        earliest: AnalyticsEarliestRecorded,
    ) -> ServerAnalyticsReport:
        voice_current = _VoiceAggregation()
        voice_previous = _VoiceAggregation()
        daily_voice: dict[date, list[float]] = defaultdict(lambda: [0.0, 0.0])
        activity_intervals: list[VoiceActivityInterval] = []

        for interval in voice_intervals:
            _add_voice_overlap(
                voice_previous,
                interval,
                window.previous_started_at,
                window.previous_ended_at,
            )
            current_bounds = _overlap_bounds(
                interval,
                window.current_started_at,
                window.current_ended_at,
            )
            if current_bounds is None:
                continue
            current_start, current_end = current_bounds
            voice_current.add(
                interval.user_id,
                interval.quality,
                (current_end - current_start).total_seconds(),
            )
            quality_index = 0 if interval.quality == "exact" else 1
            for local_date, seconds in _split_local_dates(
                current_start,
                current_end,
                self._report_timezone,
            ):
                daily_voice[local_date][quality_index] += seconds
            activity_intervals.append(
                VoiceActivityInterval(current_start, current_end, interval.quality)
            )

        text_current = _TextAggregation()
        text_previous = _TextAggregation()
        daily_messages: defaultdict[date, int] = defaultdict(int)
        for row in text_rows:
            if (
                window.previous_started_on
                <= row.activity_date
                < window.previous_ended_before
            ):
                text_previous.add(row.user_id, row.message_count)
            elif (
                window.current_started_on
                <= row.activity_date
                < window.current_ended_before
            ):
                text_current.add(row.user_id, row.message_count)
                daily_messages[row.activity_date] += row.message_count

        current_active = voice_current.users | text_current.users
        previous_active = voice_previous.users | text_previous.users
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
        activity_period = {
            ServerAnalyticsPeriod.LAST_7_DAYS: VoiceActivityPeriod.LAST_7_DAYS,
            ServerAnalyticsPeriod.LAST_30_DAYS: VoiceActivityPeriod.LAST_30_DAYS,
        }[period]
        voice_activity = aggregate_voice_activity_window(
            tuple(activity_intervals),
            period=activity_period,
            started_at=window.current_started_at,
            as_of=window.current_ended_at,
            report_timezone=self._report_timezone,
        )

        exact = analytics_metric(
            int(voice_current.exact_seconds),
            int(voice_previous.exact_seconds),
        )
        estimated = analytics_metric(
            int(voice_current.estimated_seconds),
            int(voice_previous.estimated_seconds),
        )
        total = analytics_metric(
            exact.current + estimated.current,
            exact.previous + estimated.previous,
        )
        return ServerAnalyticsReport(
            window=window,
            active_members=analytics_metric(len(current_active), len(previous_active)),
            voice_person_time=AnalyticsVoiceMetric(exact, estimated, total),
            messages=analytics_metric(text_current.total, text_previous.total),
            unique_voice_users=analytics_metric(
                len(voice_current.users), len(voice_previous.users)
            ),
            unique_message_authors=analytics_metric(
                len(text_current.users), len(text_previous.users)
            ),
            daily=daily,
            top_voice_members=voice_current.top_five(),
            top_message_authors=text_current.top_five(),
            voice_activity=voice_activity,
            voice_coverage=_coverage(
                (
                    earliest.voice_started_at.astimezone(self._report_timezone).date()
                    if earliest.voice_started_at is not None
                    else None
                ),
                window.current_started_on,
                window.previous_started_on,
            ),
            text_coverage=_coverage(
                earliest.text_activity_date,
                window.current_started_on,
                window.previous_started_on,
            ),
        )


class _VoiceAggregation:
    def __init__(self) -> None:
        self.exact_seconds = 0.0
        self.estimated_seconds = 0.0
        self.by_user: defaultdict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])

    @property
    def users(self) -> set[int]:
        return set(self.by_user)

    def add(self, user_id: int, quality: str, seconds: float) -> None:
        if seconds <= 0:
            return
        index = 0 if quality == "exact" else 1
        self.by_user[user_id][index] += seconds
        if quality == "exact":
            self.exact_seconds += seconds
        else:
            self.estimated_seconds += seconds

    def top_five(self) -> tuple[AnalyticsVoiceTopMember, ...]:
        """Match Voice statistics: total, exact, then stable user ID."""

        entries = (
            AnalyticsVoiceTopMember(user_id, int(seconds[0]), int(seconds[1]))
            for user_id, seconds in self.by_user.items()
        )
        return tuple(
            sorted(
                entries,
                key=lambda item: (
                    -item.total_seconds,
                    -item.exact_seconds,
                    item.user_id,
                ),
            )[:5]
        )


class _TextAggregation:
    def __init__(self) -> None:
        self.total = 0
        self.by_user: defaultdict[int, int] = defaultdict(int)

    @property
    def users(self) -> set[int]:
        return set(self.by_user)

    def add(self, user_id: int, count: int) -> None:
        if count <= 0:
            return
        self.total += count
        self.by_user[user_id] += count

    def top_five(self) -> tuple[AnalyticsMessageTopMember, ...]:
        entries = (
            AnalyticsMessageTopMember(user_id, count)
            for user_id, count in self.by_user.items()
        )
        return tuple(
            sorted(entries, key=lambda item: (-item.message_count, item.user_id))[:5]
        )


def _add_voice_overlap(
    aggregation: _VoiceAggregation,
    interval: AnalyticsVoiceInterval,
    started_at: datetime,
    ended_at: datetime,
) -> None:
    bounds = _overlap_bounds(interval, started_at, ended_at)
    if bounds is not None:
        start, end = bounds
        aggregation.add(
            interval.user_id, interval.quality, (end - start).total_seconds()
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


def _coverage(
    earliest: date | None,
    current_window_start: date,
    previous_window_start: date,
) -> AnalyticsCoverage:
    if earliest is None:
        return AnalyticsCoverage(None, None, None)
    return AnalyticsCoverage(
        earliest,
        current_window_start < earliest,
        previous_window_start < earliest,
    )
