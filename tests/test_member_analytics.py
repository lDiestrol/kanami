from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.features.member_analytics import MemberAnalyticsService
from discord_stats_bot.features.server_analytics import (
    AnalyticsEarliestRecorded,
    AnalyticsPercentState,
    AnalyticsTextRow,
    AnalyticsVoiceInterval,
    ServerAnalyticsPeriod,
    analytics_metric,
)


class RecordingRepository:
    def __init__(
        self,
        *,
        voice: tuple[AnalyticsVoiceInterval, ...] = (),
        text: tuple[AnalyticsTextRow, ...] = (),
        earliest: AnalyticsEarliestRecorded | None = None,
    ) -> None:
        self.voice = voice
        self.text = text
        self.earliest = earliest or AnalyticsEarliestRecorded(None, None)
        self.reads: list[tuple[str, int, int, object | None]] = []

    async def list_voice_intervals(  # type: ignore[no-untyped-def]
        self, guild_id, user_id, query
    ):
        self.reads.append(("voice", guild_id, user_id, query))
        return self.voice

    async def list_text_rows(  # type: ignore[no-untyped-def]
        self, guild_id, user_id, query
    ):
        self.reads.append(("text", guild_id, user_id, query))
        return self.text

    async def get_earliest_recorded(  # type: ignore[no-untyped-def]
        self, guild_id, user_id
    ):
        self.reads.append(("coverage", guild_id, user_id, None))
        return self.earliest


def service(
    repository: RecordingRepository,
    timezone: str = "UTC",
) -> MemberAnalyticsService:
    return MemberAnalyticsService(
        repository,  # type: ignore[arg-type]
        report_timezone=ZoneInfo(timezone),
        min_session_seconds=10,
    )


@pytest.mark.parametrize(
    ("period", "expected_days"),
    [
        (ServerAnalyticsPeriod.LAST_7_DAYS, 7),
        (ServerAnalyticsPeriod.LAST_30_DAYS, 30),
    ],
)
@pytest.mark.asyncio
async def test_report_uses_completed_local_day_window_and_exact_daily_length(
    period: ServerAnalyticsPeriod,
    expected_days: int,
) -> None:
    repository = RecordingRepository()

    report = await service(repository, "Asia/Yekaterinburg").get_report(
        10,
        20,
        period,
        datetime(2026, 8, 20, 18, 30, tzinfo=UTC),
    )

    assert report.window.current_ended_before == date(2026, 8, 20)
    assert report.window.current_ended_at == datetime(2026, 8, 19, 19, tzinfo=UTC)
    assert len(report.daily) == expected_days
    assert report.daily[0].local_date == date(2026, 8, 20) - timedelta(
        days=expected_days
    )
    assert report.daily[-1].local_date == date(2026, 8, 19)
    assert [name for name, *_ in repository.reads] == ["voice", "text", "coverage"]
    assert {(guild_id, user_id) for _, guild_id, user_id, _ in repository.reads} == {
        (10, 20)
    }
    assert repository.reads[0][3] is repository.reads[1][3]


@pytest.mark.asyncio
async def test_dst_window_and_daily_voice_use_local_midnight_boundaries() -> None:
    repository = RecordingRepository(
        voice=(
            AnalyticsVoiceInterval(
                20,
                datetime(2025, 3, 9, 5, tzinfo=UTC),
                datetime(2025, 3, 10, 4, tzinfo=UTC),
                "exact",
            ),
        )
    )

    report = await service(repository, "America/New_York").get_report(
        10,
        20,
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2025, 3, 10, 16, tzinfo=UTC),
    )

    assert report.window.current_ended_at - report.window.current_started_at == (
        timedelta(hours=167)
    )
    march_ninth = next(
        point for point in report.daily if point.local_date == date(2025, 3, 9)
    )
    assert march_ninth.voice_exact_seconds == 23 * 60 * 60
    assert report.active_days.current == 1


@pytest.mark.asyncio
async def test_report_clips_windows_preserves_quality_and_unions_active_dates() -> None:
    repository = RecordingRepository(
        voice=(
            AnalyticsVoiceInterval(
                20,
                datetime(2026, 8, 10, 10, tzinfo=UTC),
                datetime(2026, 8, 10, 12, tzinfo=UTC),
                "exact",
            ),
            AnalyticsVoiceInterval(
                20,
                datetime(2026, 8, 12, 23, 30, tzinfo=UTC),
                datetime(2026, 8, 13, 0, 30, tzinfo=UTC),
                "exact",
            ),
            AnalyticsVoiceInterval(
                20,
                datetime(2026, 8, 14, 10, tzinfo=UTC),
                datetime(2026, 8, 14, 11, tzinfo=UTC),
                "estimated",
            ),
        ),
        text=(
            AnalyticsTextRow(20, date(2026, 8, 10), 3),
            AnalyticsTextRow(20, date(2026, 8, 11), 4),
            AnalyticsTextRow(20, date(2026, 8, 14), 5),
            AnalyticsTextRow(20, date(2026, 8, 15), 2),
        ),
        earliest=AnalyticsEarliestRecorded(
            datetime(2026, 8, 10, tzinfo=UTC),
            date(2026, 8, 1),
        ),
    )

    report = await service(repository).get_report(
        10,
        20,
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2026, 8, 20, 12, tzinfo=UTC),
    )

    assert report.voice_person_time.exact_seconds.current == 30 * 60
    assert report.voice_person_time.exact_seconds.previous == 150 * 60
    assert report.voice_person_time.estimated_seconds.current == 60 * 60
    assert report.voice_person_time.estimated_seconds.previous == 0
    assert report.voice_person_time.total_seconds.current == 90 * 60
    assert report.voice_person_time.total_seconds.previous == 150 * 60
    assert report.messages.current == report.messages.previous == 7
    assert report.active_days.current == 3
    assert report.active_days.previous == 3
    assert report.messages.percent_state is AnalyticsPercentState.AVAILABLE
    assert (
        report.voice_person_time.estimated_seconds.percent_state
        is AnalyticsPercentState.NO_BASELINE
    )
    assert report.daily[0].voice_exact_seconds == 30 * 60
    assert report.daily[1].voice_estimated_seconds == 60 * 60
    assert report.daily[1].messages == 5
    assert report.daily[2].messages == 2
    assert [point.local_date for point in report.daily] == sorted(
        point.local_date for point in report.daily
    )


@pytest.mark.asyncio
async def test_empty_member_has_zero_filled_report_and_empty_coverage() -> None:
    report = await service(RecordingRepository()).get_report(
        10,
        20,
        ServerAnalyticsPeriod.LAST_30_DAYS,
        datetime(2026, 8, 20, 12, tzinfo=UTC),
    )

    assert report.user_id == 20
    assert report.period is ServerAnalyticsPeriod.LAST_30_DAYS
    assert len(report.daily) == 30
    assert all(
        (point.voice_exact_seconds, point.voice_estimated_seconds, point.messages)
        == (0, 0, 0)
        for point in report.daily
    )
    for metric in (
        report.voice_person_time.exact_seconds,
        report.voice_person_time.estimated_seconds,
        report.voice_person_time.total_seconds,
        report.messages,
        report.active_days,
    ):
        assert metric.percent_state is AnalyticsPercentState.UNCHANGED_ZERO
    assert report.voice_coverage.earliest_recorded_on is None
    assert report.voice_coverage.current_window_begins_before_earliest_recorded is None
    assert report.voice_coverage.previous_window_begins_before_earliest_recorded is None
    assert report.text_coverage.earliest_recorded_on is None


@pytest.mark.asyncio
async def test_member_specific_voice_and_text_coverage_are_independent() -> None:
    report = await service(
        RecordingRepository(
            earliest=AnalyticsEarliestRecorded(
                datetime(2026, 8, 14, 23, 30, tzinfo=UTC),
                date(2026, 8, 10),
            )
        ),
        "Asia/Yekaterinburg",
    ).get_report(
        10,
        20,
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2026, 8, 20, 12, tzinfo=UTC),
    )

    assert report.voice_coverage.earliest_recorded_on == date(2026, 8, 15)
    assert report.voice_coverage.current_window_begins_before_earliest_recorded is True
    assert report.voice_coverage.previous_window_begins_before_earliest_recorded is True
    assert report.text_coverage.earliest_recorded_on == date(2026, 8, 10)
    assert report.text_coverage.current_window_begins_before_earliest_recorded is False
    assert report.text_coverage.previous_window_begins_before_earliest_recorded is True


@pytest.mark.asyncio
async def test_positive_identifiers_and_constructor_configuration_are_required() -> (
    None
):
    analytics = service(RecordingRepository())
    args = (
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2026, 8, 20, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="guild_id must be positive"):
        await analytics.get_report(0, 20, *args)
    with pytest.raises(ValueError, match="user_id must be positive"):
        await analytics.get_report(10, 0, *args)
    with pytest.raises(ValueError, match="min_session_seconds must be positive"):
        MemberAnalyticsService(
            RecordingRepository(),  # type: ignore[arg-type]
            report_timezone=ZoneInfo("UTC"),
            min_session_seconds=0,
        )


@pytest.mark.asyncio
async def test_report_invariants_reject_missing_dates_and_excess_active_days() -> None:
    report = await service(RecordingRepository()).get_report(
        10,
        20,
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2026, 8, 20, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="every current-window date exactly once"):
        replace(report, daily=report.daily[:-1])
    with pytest.raises(ValueError, match="current active days cannot exceed"):
        replace(report, active_days=analytics_metric(8, 0))
