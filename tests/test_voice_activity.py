from collections import Counter
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.voice_statistics import (
    VoiceActivityInterval,
    VoiceActivityPeriod,
    VoiceStatisticsQuery,
    activity_heatmap_levels,
    aggregate_voice_activity,
    aggregate_voice_activity_window,
    heatmap_intensity,
)
from discord_stats_bot.persistence.repositories.voice_statistics import (
    SqlAlchemyVoiceStatisticsRepository,
    voice_activity_intervals_statement,
)

AS_OF = datetime(2026, 1, 12, tzinfo=UTC)
UTC_ZONE = ZoneInfo("UTC")


def activity_report(
    *intervals: VoiceActivityInterval,
    period: VoiceActivityPeriod = VoiceActivityPeriod.LAST_7_DAYS,
    as_of: datetime = AS_OF,
    timezone: ZoneInfo = UTC_ZONE,
):
    return aggregate_voice_activity(
        intervals,
        period=period,
        as_of=as_of,
        report_timezone=timezone,
    )


def test_interval_splits_20_30_to_22_15_across_hour_buckets() -> None:
    report = activity_report(
        VoiceActivityInterval(
            datetime(2026, 1, 10, 20, 30, tzinfo=UTC),
            datetime(2026, 1, 10, 22, 15, tzinfo=UTC),
        )
    )

    exposure = 7 * 3600
    assert report.hourly_activity[20] * exposure == pytest.approx(30 * 60)
    assert report.hourly_activity[21] * exposure == pytest.approx(60 * 60)
    assert report.hourly_activity[22] * exposure == pytest.approx(15 * 60)
    assert report.total_user_seconds == 105 * 60


def test_midnight_and_weekday_boundary_use_local_calendar_days() -> None:
    report = activity_report(
        VoiceActivityInterval(
            datetime(2026, 1, 11, 23, 30, tzinfo=UTC),
            datetime(2026, 1, 12, 0, 30, tzinfo=UTC),
        ),
        as_of=datetime(2026, 1, 13, tzinfo=UTC),
    )

    assert report.weekday_activity[6] == pytest.approx(30 * 60)
    assert report.weekday_activity[0] == pytest.approx(30 * 60)
    assert report.hourly_activity[23] > 0
    assert report.hourly_activity[0] > 0


def test_timezone_conversion_happens_before_hour_and_weekday_selection() -> None:
    report = activity_report(
        VoiceActivityInterval(
            datetime(2026, 1, 10, 18, 30, tzinfo=UTC),
            datetime(2026, 1, 10, 19, 30, tzinfo=UTC),
        ),
        timezone=ZoneInfo("Asia/Yekaterinburg"),
    )

    assert report.hourly_activity[23] > 0
    assert report.hourly_activity[0] > 0
    assert report.weekday_activity[5] == pytest.approx(30 * 60)
    assert report.weekday_activity[6] == pytest.approx(30 * 60)


def test_period_clipping_and_multiple_users_sum_as_user_time() -> None:
    started_at = AS_OF - timedelta(days=7)
    report = activity_report(
        VoiceActivityInterval(
            started_at - timedelta(hours=1), started_at + timedelta(minutes=30)
        ),
        VoiceActivityInterval(
            started_at, started_at + timedelta(minutes=30), quality="estimated"
        ),
        VoiceActivityInterval(AS_OF, AS_OF + timedelta(hours=1)),
    )

    assert report.total_user_seconds == 60 * 60
    assert report.has_estimated_time is True


def test_explicit_completed_day_window_handles_dst_forward() -> None:
    timezone = ZoneInfo("America/New_York")
    started_at = datetime(2025, 3, 3, 5, tzinfo=UTC)
    ended_at = datetime(2025, 3, 10, 4, tzinfo=UTC)

    report = aggregate_voice_activity_window(
        (
            VoiceActivityInterval(
                datetime(2025, 3, 9, 5, tzinfo=UTC),
                datetime(2025, 3, 10, 4, tzinfo=UTC),
            ),
        ),
        period=VoiceActivityPeriod.LAST_7_DAYS,
        started_at=started_at,
        as_of=ended_at,
        report_timezone=timezone,
    )

    assert report.started_at == started_at
    assert report.as_of == ended_at
    assert report.total_user_seconds == 23 * 60 * 60


def test_explicit_completed_day_window_handles_repeated_dst_hour() -> None:
    timezone = ZoneInfo("America/New_York")
    report = aggregate_voice_activity_window(
        (
            VoiceActivityInterval(
                datetime(2025, 11, 2, 5, 30, tzinfo=UTC),
                datetime(2025, 11, 2, 6, 30, tzinfo=UTC),
            ),
        ),
        period=VoiceActivityPeriod.LAST_7_DAYS,
        started_at=datetime(2025, 10, 28, 4, tzinfo=UTC),
        as_of=datetime(2025, 11, 4, 5, tzinfo=UTC),
        report_timezone=timezone,
    )

    assert report.total_user_seconds == 60 * 60
    assert report.hourly_activity[1] > 0


def test_top_hours_and_quietest_bucket_are_deterministic() -> None:
    report = activity_report(
        VoiceActivityInterval(
            datetime(2026, 1, 10, 21, tzinfo=UTC),
            datetime(2026, 1, 10, 22, tzinfo=UTC),
        ),
        VoiceActivityInterval(
            datetime(2026, 1, 9, 22, tzinfo=UTC),
            datetime(2026, 1, 9, 22, 45, tzinfo=UTC),
        ),
        VoiceActivityInterval(
            datetime(2026, 1, 8, 20, tzinfo=UTC),
            datetime(2026, 1, 8, 20, 30, tzinfo=UTC),
        ),
    )

    assert report.top_hours == (21, 22, 20)
    assert report.quietest_period == (0, 0)


def test_active_weekday_is_normalized_by_actual_occurrence_count() -> None:
    period = VoiceActivityPeriod.LAST_30_DAYS
    started_at = AS_OF - timedelta(days=period.days)
    dates = [
        (started_at + timedelta(days=offset)).date() for offset in range(period.days)
    ]
    occurrences = Counter(day.weekday() for day in dates)
    frequent_weekday = min(
        weekday for weekday, count in occurrences.items() if count == 5
    )
    less_frequent_weekday = min(
        weekday for weekday, count in occurrences.items() if count == 4
    )
    frequent_intervals = tuple(
        VoiceActivityInterval(
            datetime.combine(day, time.min, tzinfo=UTC),
            datetime.combine(day, time.min, tzinfo=UTC) + timedelta(seconds=60),
        )
        for day in dates
        if day.weekday() == frequent_weekday
    )
    less_frequent_intervals = tuple(
        VoiceActivityInterval(
            datetime.combine(day, time.min, tzinfo=UTC),
            datetime.combine(day, time.min, tzinfo=UTC) + timedelta(seconds=70),
        )
        for day in dates
        if day.weekday() == less_frequent_weekday
    )
    assert len(frequent_intervals) == 5
    assert len(less_frequent_intervals) == 4

    report = activity_report(
        *frequent_intervals,
        *less_frequent_intervals,
        period=period,
    )

    frequent_total = sum(
        item.ended_at.timestamp() - item.started_at.timestamp()
        for item in frequent_intervals
    )
    less_frequent_total = sum(
        item.ended_at.timestamp() - item.started_at.timestamp()
        for item in less_frequent_intervals
    )
    assert frequent_total > less_frequent_total
    assert report.weekday_activity[frequent_weekday] == pytest.approx(60)
    assert report.weekday_activity[less_frequent_weekday] == pytest.approx(70)
    assert report.active_weekday == less_frequent_weekday


def test_heatmap_has_56_ordered_cells_and_stable_intensity_levels() -> None:
    report = activity_report()

    assert len(report.heatmap_activity) == 8
    assert all(len(row) == 7 for row in report.heatmap_activity)
    assert report.heatmap_activity == tuple((0.0,) * 7 for _ in range(8))
    levels = activity_heatmap_levels(
        (
            (0.0, 1.0, 2.0, 3.0, 4.0, 0.0, 0.0),
            *((0.0,) * 7 for _ in range(7)),
        )
    )
    assert levels[0] == ("·", "░", "▒", "▓", "█", "·", "·")
    assert heatmap_intensity(0, 4) == "·"
    assert heatmap_intensity(4, 4) == "█"


def test_empty_activity_has_no_meaningless_rankings() -> None:
    report = activity_report()

    assert report.has_activity is False
    assert report.top_hours == ()
    assert report.active_weekday is None
    assert report.quietest_period is None


def test_activity_statement_is_guild_bounded_read_only_and_reuses_voice_rules() -> None:
    query = VoiceStatisticsQuery(
        as_of=AS_OF,
        today_started_at=AS_OF - timedelta(days=1),
        last_7_days_started_at=AS_OF - timedelta(days=7),
        last_30_days_started_at=AS_OF - timedelta(days=30),
        min_exact_session_seconds=10,
    )
    sql = str(
        voice_activity_intervals_statement(
            10, AS_OF - timedelta(days=90), query
        ).compile(dialect=postgresql.dialect())
    ).lower()

    assert "activity_effective_voice_intervals" in sql
    assert "activity_eligible_voice_sessions" in sql
    assert "voice_intervals.guild_id =" in sql
    assert "voice_intervals.is_afk is false" in sql
    assert "discord_users.is_bot is false" in sql
    assert "effective_end >" in sql
    assert "insert " not in sql
    assert "update " not in sql
    assert "delete " not in sql


@pytest.mark.asyncio
async def test_repository_maps_activity_intervals() -> None:
    rows = [
        SimpleNamespace(
            started_at=AS_OF - timedelta(hours=2),
            ended_at=AS_OF - timedelta(hours=1),
            quality="exact",
        )
    ]

    class Session:
        async def execute(self, statement: object) -> list[object]:
            return rows

    repository = SqlAlchemyVoiceStatisticsRepository(Session())  # type: ignore[arg-type]
    query = VoiceStatisticsQuery(
        AS_OF,
        AS_OF - timedelta(days=1),
        AS_OF - timedelta(days=7),
        AS_OF - timedelta(days=30),
        10,
    )

    result = await repository.get_activity_intervals(
        10, AS_OF - timedelta(days=7), query
    )

    assert result == (
        VoiceActivityInterval(AS_OF - timedelta(hours=2), AS_OF - timedelta(hours=1)),
    )
