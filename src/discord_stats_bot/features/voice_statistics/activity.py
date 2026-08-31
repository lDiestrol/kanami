"""Timezone-aware, Discord-independent guild voice activity analytics."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from math import ceil
from zoneinfo import ZoneInfo

from discord_stats_bot.features.voice.types import normalize_observed_at

HEATMAP_LEVELS = ("·", "░", "▒", "▓", "█")
HEATMAP_SLOT_HOURS = 3
HEATMAP_ROWS = 24 // HEATMAP_SLOT_HOURS


class VoiceActivityPeriod(StrEnum):
    """Finite periods supported by the guild activity report."""

    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"
    LAST_90_DAYS = "90d"

    @property
    def days(self) -> int:
        return {
            VoiceActivityPeriod.LAST_7_DAYS: 7,
            VoiceActivityPeriod.LAST_30_DAYS: 30,
            VoiceActivityPeriod.LAST_90_DAYS: 90,
        }[self]


@dataclass(frozen=True, slots=True)
class VoiceActivityInterval:
    """One effective eligible interval returned by persistence."""

    started_at: datetime
    ended_at: datetime
    quality: str = "exact"

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", normalize_observed_at(self.started_at))
        object.__setattr__(self, "ended_at", normalize_observed_at(self.ended_at))
        if self.ended_at <= self.started_at:
            raise ValueError("voice activity interval must have positive duration")
        if self.quality not in {"exact", "estimated"}:
            raise ValueError("voice activity quality must be exact or estimated")


@dataclass(frozen=True, slots=True)
class VoiceActivityReport:
    """Normalized recurring-hour and weekday activity for one guild period."""

    as_of: datetime
    started_at: datetime
    period: VoiceActivityPeriod
    timezone_name: str
    total_user_seconds: float
    hourly_activity: tuple[float, ...]
    weekday_activity: tuple[float, ...]
    heatmap_activity: tuple[tuple[float, ...], ...]
    top_hours: tuple[int, ...]
    active_weekday: int | None
    quietest_period: tuple[int, int] | None
    has_estimated_time: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", normalize_observed_at(self.as_of))
        object.__setattr__(self, "started_at", normalize_observed_at(self.started_at))
        if self.started_at >= self.as_of:
            raise ValueError("activity report window must have positive duration")
        if self.total_user_seconds < 0:
            raise ValueError("total_user_seconds must not be negative")
        if len(self.hourly_activity) != 24 or len(self.weekday_activity) != 7:
            raise ValueError("activity report requires 24 hours and 7 weekdays")
        if len(self.heatmap_activity) != HEATMAP_ROWS or any(
            len(row) != 7 for row in self.heatmap_activity
        ):
            raise ValueError("activity heatmap requires 8 rows and 7 weekdays")

    @property
    def has_activity(self) -> bool:
        return self.total_user_seconds > 0


@dataclass(frozen=True, slots=True)
class _LocalHourSegment:
    local_date: date
    weekday: int
    hour: int
    seconds: float


def heatmap_intensity(value: float, maximum: float) -> str:
    """Map one non-negative activity value to a deterministic five-level glyph."""

    if value < 0 or maximum < 0:
        raise ValueError("heatmap activity must not be negative")
    if value == 0 or maximum == 0:
        return HEATMAP_LEVELS[0]
    ratio = min(value / maximum, 1.0)
    index = min(4, max(1, ceil(ratio * 4)))
    return HEATMAP_LEVELS[index]


def activity_heatmap_levels(
    activity: tuple[tuple[float, ...], ...],
) -> tuple[tuple[str, ...], ...]:
    """Convert an 8x7 numeric heatmap to stable compact intensity glyphs."""

    if len(activity) != HEATMAP_ROWS or any(len(row) != 7 for row in activity):
        raise ValueError("activity heatmap requires 8 rows and 7 weekdays")
    maximum = max((value for row in activity for value in row), default=0.0)
    return tuple(
        tuple(heatmap_intensity(value, maximum) for value in row) for row in activity
    )


def aggregate_voice_activity(
    intervals: tuple[VoiceActivityInterval, ...],
    *,
    period: VoiceActivityPeriod,
    as_of: datetime,
    report_timezone: ZoneInfo,
) -> VoiceActivityReport:
    """Clip and distribute user-time into normalized local recurring buckets."""

    if not isinstance(period, VoiceActivityPeriod):
        raise ValueError("period must be a VoiceActivityPeriod")
    as_of = normalize_observed_at(as_of)
    started_at = as_of - timedelta(days=period.days)
    return aggregate_voice_activity_window(
        intervals,
        period=period,
        started_at=started_at,
        as_of=as_of,
        report_timezone=report_timezone,
    )


def aggregate_voice_activity_window(
    intervals: tuple[VoiceActivityInterval, ...],
    *,
    period: VoiceActivityPeriod,
    started_at: datetime,
    as_of: datetime,
    report_timezone: ZoneInfo,
) -> VoiceActivityReport:
    """Aggregate an explicit window with the established activity semantics."""

    if not isinstance(period, VoiceActivityPeriod):
        raise ValueError("period must be a VoiceActivityPeriod")
    started_at = normalize_observed_at(started_at)
    as_of = normalize_observed_at(as_of)
    if started_at >= as_of:
        raise ValueError("activity report window must have positive duration")
    user_seconds = [[0.0 for _ in range(24)] for _ in range(7)]
    exposure_seconds = [[0.0 for _ in range(24)] for _ in range(7)]
    weekday_dates: list[set[date]] = [set() for _ in range(7)]

    for segment in _split_local_hours(started_at, as_of, report_timezone):
        exposure_seconds[segment.weekday][segment.hour] += segment.seconds
        weekday_dates[segment.weekday].add(segment.local_date)

    total_user_seconds = 0.0
    has_estimated_time = False
    for interval in intervals:
        clipped_start = max(interval.started_at, started_at)
        clipped_end = min(interval.ended_at, as_of)
        if clipped_end <= clipped_start:
            continue
        overlap_seconds = (clipped_end - clipped_start).total_seconds()
        total_user_seconds += overlap_seconds
        has_estimated_time = has_estimated_time or interval.quality == "estimated"
        for segment in _split_local_hours(clipped_start, clipped_end, report_timezone):
            user_seconds[segment.weekday][segment.hour] += segment.seconds

    hourly_activity = tuple(
        _normalized(
            sum(user_seconds[weekday][hour] for weekday in range(7)),
            sum(exposure_seconds[weekday][hour] for weekday in range(7)),
        )
        for hour in range(24)
    )
    weekday_activity = tuple(
        _normalized(sum(user_seconds[weekday]), len(weekday_dates[weekday]))
        for weekday in range(7)
    )
    heatmap_activity = tuple(
        tuple(
            _normalized(
                sum(
                    user_seconds[weekday][hour]
                    for hour in range(slot, slot + HEATMAP_SLOT_HOURS)
                ),
                sum(
                    exposure_seconds[weekday][hour]
                    for hour in range(slot, slot + HEATMAP_SLOT_HOURS)
                ),
            )
            for weekday in range(7)
        )
        for slot in range(0, 24, HEATMAP_SLOT_HOURS)
    )

    if total_user_seconds == 0:
        top_hours: tuple[int, ...] = ()
        active_weekday = None
        quietest_period = None
    else:
        top_hours = tuple(
            sorted(range(24), key=lambda hour: (-hourly_activity[hour], hour))[:3]
        )
        active_weekday = min(
            range(7), key=lambda weekday: (-weekday_activity[weekday], weekday)
        )
        quietest_period = min(
            (
                (weekday, slot * HEATMAP_SLOT_HOURS)
                for slot in range(HEATMAP_ROWS)
                for weekday in range(7)
            ),
            key=lambda item: (
                heatmap_activity[item[1] // HEATMAP_SLOT_HOURS][item[0]],
                item[0],
                item[1],
            ),
        )

    return VoiceActivityReport(
        as_of=as_of,
        started_at=started_at,
        period=period,
        timezone_name=report_timezone.key,
        total_user_seconds=total_user_seconds,
        hourly_activity=hourly_activity,
        weekday_activity=weekday_activity,
        heatmap_activity=heatmap_activity,
        top_hours=top_hours,
        active_weekday=active_weekday,
        quietest_period=quietest_period,
        has_estimated_time=has_estimated_time,
    )


def _normalized(value: float, denominator: float | int) -> float:
    return value / denominator if denominator else 0.0


def _split_local_hours(
    started_at: datetime,
    ended_at: datetime,
    timezone: ZoneInfo,
) -> tuple[_LocalHourSegment, ...]:
    cursor = normalize_observed_at(started_at)
    ended_at = normalize_observed_at(ended_at)
    segments: list[_LocalHourSegment] = []
    while cursor < ended_at:
        local_cursor = cursor.astimezone(timezone)
        boundary = min(_next_local_hour(cursor, timezone), ended_at)
        segments.append(
            _LocalHourSegment(
                local_date=local_cursor.date(),
                weekday=local_cursor.weekday(),
                hour=local_cursor.hour,
                seconds=(boundary - cursor).total_seconds(),
            )
        )
        cursor = boundary
    return tuple(segments)


def _next_local_hour(cursor: datetime, timezone: ZoneInfo) -> datetime:
    local_cursor = cursor.astimezone(timezone)
    candidate = local_cursor.replace(
        tzinfo=None, minute=0, second=0, microsecond=0
    ) + timedelta(hours=1)
    for _ in range(4):
        valid: list[datetime] = []
        for fold in (0, 1):
            aware = candidate.replace(tzinfo=timezone, fold=fold)
            utc_candidate = aware.astimezone(UTC)
            if (
                utc_candidate > cursor
                and utc_candidate.astimezone(timezone).replace(tzinfo=None) == candidate
            ):
                valid.append(utc_candidate)
        if valid:
            return min(valid)
        candidate += timedelta(hours=1)
    raise ValueError("could not resolve the next local hour boundary")
