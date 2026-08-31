from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.features.server_analytics import (
    AnalyticsEarliestRecorded,
    AnalyticsPercentState,
    AnalyticsTextRow,
    AnalyticsVoiceInterval,
    ServerAnalyticsPeriod,
    ServerAnalyticsService,
    analytics_metric,
    build_analytics_window,
)


def test_completed_local_day_windows_exclude_today_and_are_contiguous() -> None:
    window = build_analytics_window(
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2026, 8, 20, 18, 30, tzinfo=UTC),
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
    )

    assert window.current_started_on == date(2026, 8, 13)
    assert window.current_ended_before == date(2026, 8, 20)
    assert window.previous_started_on == date(2026, 8, 6)
    assert window.previous_ended_before == date(2026, 8, 13)
    assert window.current_started_at == datetime(2026, 8, 12, 19, tzinfo=UTC)
    assert window.current_ended_at == datetime(2026, 8, 19, 19, tzinfo=UTC)
    assert window.previous_ended_at == window.current_started_at


@pytest.mark.parametrize(
    ("period", "expected_days"),
    [
        (ServerAnalyticsPeriod.LAST_7_DAYS, 7),
        (ServerAnalyticsPeriod.LAST_30_DAYS, 30),
    ],
)
def test_period_has_exact_number_of_local_dates(
    period: ServerAnalyticsPeriod,
    expected_days: int,
) -> None:
    window = build_analytics_window(
        period,
        datetime(2026, 8, 29, 12, tzinfo=UTC),
        report_timezone=ZoneInfo("UTC"),
    )

    assert (
        window.current_ended_before - window.current_started_on
    ).days == expected_days
    assert (
        window.previous_ended_before - window.previous_started_on
    ).days == expected_days


def test_dst_forward_and_backward_use_local_midnights_not_rolling_hours() -> None:
    timezone = ZoneInfo("America/New_York")
    spring = build_analytics_window(
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2025, 3, 10, 16, tzinfo=UTC),
        report_timezone=timezone,
    )
    autumn = build_analytics_window(
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2025, 11, 4, 16, tzinfo=UTC),
        report_timezone=timezone,
    )

    assert spring.current_ended_at - spring.current_started_at == timedelta(hours=167)
    assert autumn.current_ended_at - autumn.current_started_at == timedelta(hours=169)


def test_metric_delta_states_are_explicit() -> None:
    positive = analytics_metric(15, 10)
    negative = analytics_metric(5, 10)
    zero = analytics_metric(0, 0)
    new = analytics_metric(5, 0)

    assert (positive.absolute_delta, positive.percent_delta) == (5, 50.0)
    assert (negative.absolute_delta, negative.percent_delta) == (-5, -50.0)
    assert zero.percent_state is AnalyticsPercentState.UNCHANGED_ZERO
    assert zero.percent_delta == 0.0
    assert new.percent_state is AnalyticsPercentState.NO_BASELINE
    assert new.percent_delta is None


class RecordingRepository:
    def __init__(self) -> None:
        self.queries = []

    async def list_voice_intervals(self, guild_id, query):  # type: ignore[no-untyped-def]
        self.queries.append(("voice", guild_id, query))
        return (
            AnalyticsVoiceInterval(
                1,
                datetime(2026, 8, 14, 10, tzinfo=UTC),
                datetime(2026, 8, 14, 11, tzinfo=UTC),
                "exact",
            ),
            AnalyticsVoiceInterval(
                2,
                datetime(2026, 8, 18, 10, tzinfo=UTC),
                datetime(2026, 8, 18, 10, 30, tzinfo=UTC),
                "estimated",
            ),
            AnalyticsVoiceInterval(
                3,
                datetime(2026, 8, 15, 23, 30, tzinfo=UTC),
                datetime(2026, 8, 16, 0, 30, tzinfo=UTC),
                "exact",
            ),
            AnalyticsVoiceInterval(
                1,
                datetime(2026, 8, 10, 10, tzinfo=UTC),
                datetime(2026, 8, 10, 12, tzinfo=UTC),
                "exact",
            ),
        )

    async def list_text_rows(self, guild_id, query):  # type: ignore[no-untyped-def]
        self.queries.append(("text", guild_id, query))
        return (
            AnalyticsTextRow(1, date(2026, 8, 14), 5),
            AnalyticsTextRow(4, date(2026, 8, 18), 5),
            AnalyticsTextRow(5, date(2026, 8, 10), 3),
        )

    async def get_earliest_recorded(self, guild_id):  # type: ignore[no-untyped-def]
        self.queries.append(("coverage", guild_id, None))
        return AnalyticsEarliestRecorded(
            datetime(2026, 8, 14, tzinfo=UTC),
            date(2026, 8, 1),
        )


@pytest.mark.asyncio
async def test_report_combines_activity_series_rankings_and_coverage() -> None:
    repository = RecordingRepository()
    service = ServerAnalyticsService(
        repository,  # type: ignore[arg-type]
        report_timezone=ZoneInfo("UTC"),
        min_session_seconds=10,
    )

    report = await service.get_report(
        10,
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2026, 8, 20, 12, tzinfo=UTC),
    )

    assert report.window.current_started_on == date(2026, 8, 13)
    assert report.window.current_ended_before == date(2026, 8, 20)
    assert report.active_members.current == 4  # voice {1,2,3} union text {1,4}
    assert report.active_members.previous == 2  # voice {1} union text {5}
    assert report.voice_person_time.exact_seconds.current == 7200
    assert report.voice_person_time.estimated_seconds.current == 1800
    assert report.voice_person_time.total_seconds.current == 9000
    assert report.voice_person_time.total_seconds.previous == 7200
    assert report.messages.current == 10
    assert report.messages.previous == 3
    assert report.unique_voice_users.current == 3
    assert report.unique_message_authors.current == 2
    assert len(report.daily) == 7
    assert report.daily[0].local_date == date(2026, 8, 13)
    assert report.daily[0].messages == 0
    assert report.daily[2].voice_exact_seconds == 1800
    assert report.daily[3].voice_exact_seconds == 1800
    assert tuple(item.user_id for item in report.top_voice_members) == (1, 3, 2)
    assert tuple(item.user_id for item in report.top_message_authors) == (1, 4)
    assert report.voice_activity.total_user_seconds == 9000
    assert report.voice_activity.has_estimated_time is True
    assert report.voice_coverage.earliest_recorded_on == date(2026, 8, 14)
    assert report.voice_coverage.current_window_begins_before_earliest_recorded is True
    assert report.voice_coverage.previous_window_begins_before_earliest_recorded is True
    assert report.text_coverage.current_window_begins_before_earliest_recorded is False
    assert report.text_coverage.previous_window_begins_before_earliest_recorded is False
    assert [item[0] for item in repository.queries] == ["voice", "text", "coverage"]
    assert repository.queries[0][2] is repository.queries[1][2]


class EmptyRepository:
    def __init__(self, earliest_recorded_on: date | None = None) -> None:
        self.earliest_recorded_on = earliest_recorded_on

    async def list_voice_intervals(self, guild_id, query):  # type: ignore[no-untyped-def]
        return ()

    async def list_text_rows(self, guild_id, query):  # type: ignore[no-untyped-def]
        return ()

    async def get_earliest_recorded(self, guild_id):  # type: ignore[no-untyped-def]
        return AnalyticsEarliestRecorded(
            (
                datetime.combine(self.earliest_recorded_on, datetime.min.time(), UTC)
                if self.earliest_recorded_on is not None
                else None
            ),
            self.earliest_recorded_on,
        )


@pytest.mark.asyncio
async def test_empty_sources_are_distinct_from_confirmed_zero_activity() -> None:
    report = await ServerAnalyticsService(
        EmptyRepository(),  # type: ignore[arg-type]
        report_timezone=ZoneInfo("UTC"),
        min_session_seconds=10,
    ).get_report(
        10,
        ServerAnalyticsPeriod.LAST_30_DAYS,
        datetime(2026, 8, 20, tzinfo=UTC),
    )

    assert report.active_members.current == 0
    assert report.active_members.percent_state is AnalyticsPercentState.UNCHANGED_ZERO
    assert all(point.messages == 0 for point in report.daily)
    assert report.voice_coverage.has_recorded_activity is False
    assert report.voice_coverage.current_window_begins_before_earliest_recorded is None
    assert report.voice_coverage.previous_window_begins_before_earliest_recorded is None
    assert report.text_coverage.has_recorded_activity is False


@pytest.mark.parametrize(
    ("earliest", "current_partial", "previous_partial"),
    [
        (date(2026, 8, 14), True, True),
        (date(2026, 8, 10), False, True),
        (date(2026, 8, 1), False, False),
        (None, None, None),
    ],
)
@pytest.mark.asyncio
async def test_coverage_compares_current_and_previous_independently(
    earliest: date | None,
    current_partial: bool | None,
    previous_partial: bool | None,
) -> None:
    report = await ServerAnalyticsService(
        EmptyRepository(earliest),  # type: ignore[arg-type]
        report_timezone=ZoneInfo("UTC"),
        min_session_seconds=10,
    ).get_report(
        10,
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2026, 8, 20, 12, tzinfo=UTC),
    )

    for coverage in (report.voice_coverage, report.text_coverage):
        assert coverage.earliest_recorded_on == earliest
        assert (
            coverage.current_window_begins_before_earliest_recorded is current_partial
        )
        assert (
            coverage.previous_window_begins_before_earliest_recorded is previous_partial
        )


@pytest.mark.asyncio
async def test_voice_ranking_matches_existing_total_then_exact_then_user_id() -> None:
    class RankingRepository(EmptyRepository):
        async def list_voice_intervals(  # type: ignore[no-untyped-def]
            self, guild_id, query
        ):
            started_at = datetime(2026, 8, 14, 10, tzinfo=UTC)

            def interval(user_id: int, seconds: int, quality: str):
                return AnalyticsVoiceInterval(
                    user_id,
                    started_at,
                    started_at + timedelta(seconds=seconds),
                    quality,
                )

            return (
                interval(1, 100, "exact"),
                interval(2, 90, "exact"),
                interval(2, 100, "estimated"),
                interval(4, 50, "exact"),
                interval(4, 10, "estimated"),
                interval(3, 50, "exact"),
                interval(3, 10, "estimated"),
            )

    report = await ServerAnalyticsService(
        RankingRepository(),  # type: ignore[arg-type]
        report_timezone=ZoneInfo("UTC"),
        min_session_seconds=10,
    ).get_report(
        10,
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2026, 8, 20, 12, tzinfo=UTC),
    )

    assert tuple(item.user_id for item in report.top_voice_members) == (2, 1, 3, 4)
