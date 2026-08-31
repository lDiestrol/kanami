from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.voice_statistics import (
    VoicePeriodDurations,
    VoiceStatistics,
    VoiceStatisticsQuery,
    VoiceStatisticsService,
)
from discord_stats_bot.persistence.repositories.voice_statistics import (
    SqlAlchemyVoiceStatisticsRepository,
    voice_statistics_statement,
)

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Interval:
    session_id: int
    started_at: datetime
    ended_at: datetime | None
    confirmed_through_at: datetime
    quality: str = "exact"
    is_afk: bool = False


class InMemoryVoiceStatisticsRepository:
    """Semantic test double; production performs the same work in SQL."""

    def __init__(self, intervals: list[Interval]) -> None:
        self.intervals = intervals
        self.calls: list[tuple[int, int, VoiceStatisticsQuery]] = []

    async def get_user_statistics(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoiceStatistics:
        self.calls.append((guild_id, user_id, query))
        effective: list[tuple[Interval, datetime]] = []
        for interval in self.intervals:
            if interval.is_afk:
                continue
            end = min(
                interval.ended_at
                if interval.ended_at is not None
                else interval.confirmed_through_at,
                query.as_of,
            )
            if interval.started_at < end:
                effective.append((interval, end))

        exact_by_session: defaultdict[int, int] = defaultdict(int)
        for interval, end in effective:
            if interval.quality == "exact":
                exact_by_session[interval.session_id] += int(
                    (end - interval.started_at).total_seconds()
                )
        eligible = {
            session_id
            for session_id, seconds in exact_by_session.items()
            if seconds >= query.min_exact_session_seconds
        }

        def aggregate(window_start: datetime | None) -> VoicePeriodDurations:
            totals = {"exact": 0, "estimated": 0}
            for interval, end in effective:
                if interval.session_id not in eligible:
                    continue
                start = max(interval.started_at, window_start or interval.started_at)
                totals[interval.quality] += max(
                    0,
                    int((end - start).total_seconds()),
                )
            return VoicePeriodDurations(
                exact_seconds=totals["exact"],
                estimated_seconds=totals["estimated"],
            )

        return VoiceStatistics(
            as_of=query.as_of,
            today=aggregate(query.today_started_at),
            last_7_days=aggregate(query.last_7_days_started_at),
            last_30_days=aggregate(query.last_30_days_started_at),
            all_time=aggregate(None),
        )


def make_service(
    intervals: list[Interval],
    *,
    timezone: str = "UTC",
    threshold: int = 1,
) -> tuple[VoiceStatisticsService, InMemoryVoiceStatisticsRepository]:
    repository = InMemoryVoiceStatisticsRepository(intervals)
    return (
        VoiceStatisticsService(
            repository,
            report_timezone=ZoneInfo(timezone),
            min_session_seconds=threshold,
        ),
        repository,
    )


@pytest.mark.asyncio
async def test_closed_exact_interval_is_aggregated() -> None:
    service, _ = make_service(
        [Interval(1, AS_OF - timedelta(hours=2), AS_OF - timedelta(hours=1), AS_OF)]
    )

    statistics = await service.get_user_statistics(10, 20, AS_OF)

    assert statistics.today == VoicePeriodDurations(exact_seconds=3600)
    assert statistics.all_time.total_seconds == 3600


@pytest.mark.asyncio
async def test_estimated_and_exact_durations_remain_separate_and_totalled() -> None:
    service, _ = make_service(
        [
            Interval(1, AS_OF - timedelta(minutes=30), AS_OF, AS_OF),
            Interval(
                1,
                AS_OF - timedelta(minutes=40),
                AS_OF - timedelta(minutes=30),
                AS_OF,
                quality="estimated",
            ),
        ]
    )

    statistics = await service.get_user_statistics(10, 20, AS_OF)

    assert statistics.today.exact_seconds == 1800
    assert statistics.today.estimated_seconds == 600
    assert statistics.today.total_seconds == 2400
    assert statistics.has_estimated_time is True


@pytest.mark.asyncio
async def test_open_interval_stops_at_confirmation_not_as_of() -> None:
    confirmed = AS_OF - timedelta(minutes=5)
    service, _ = make_service(
        [Interval(1, AS_OF - timedelta(minutes=20), None, confirmed)]
    )

    statistics = await service.get_user_statistics(10, 20, AS_OF)

    assert statistics.today.exact_seconds == 15 * 60


@pytest.mark.asyncio
async def test_open_confirmation_after_as_of_is_capped_at_as_of() -> None:
    service, _ = make_service(
        [
            Interval(
                1,
                AS_OF - timedelta(minutes=20),
                None,
                AS_OF + timedelta(minutes=5),
            )
        ]
    )

    statistics = await service.get_user_statistics(10, 20, AS_OF)

    assert statistics.today.exact_seconds == 20 * 60


@pytest.mark.asyncio
async def test_interval_intersection_handles_start_end_and_outside_window() -> None:
    service, _ = make_service(
        [
            Interval(
                1,
                AS_OF - timedelta(days=7, minutes=20),
                AS_OF - timedelta(days=7) + timedelta(minutes=10),
                AS_OF,
            ),
            Interval(
                2,
                AS_OF - timedelta(minutes=10),
                AS_OF + timedelta(minutes=10),
                AS_OF,
            ),
            Interval(
                3,
                AS_OF - timedelta(days=8),
                AS_OF - timedelta(days=8) + timedelta(hours=1),
                AS_OF,
            ),
        ]
    )

    statistics = await service.get_user_statistics(10, 20, AS_OF)

    assert statistics.last_7_days.exact_seconds == 20 * 60
    assert statistics.all_time.exact_seconds == 100 * 60


@pytest.mark.asyncio
async def test_today_uses_report_timezone_local_midnight() -> None:
    service, repository = make_service([], timezone="Asia/Yekaterinburg")

    await service.get_user_statistics(10, 20, AS_OF)

    query = repository.calls[0][2]
    assert query.today_started_at == datetime(2026, 8, 13, 19, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_today_timezone_uses_zoneinfo_dst_rules_not_fixed_offset() -> None:
    service, repository = make_service([], timezone="Europe/Stockholm")

    await service.get_user_statistics(10, 20, AS_OF)

    query = repository.calls[0][2]
    assert query.today_started_at == datetime(2026, 8, 13, 22, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_rolling_windows_and_all_time_use_one_as_of() -> None:
    service, repository = make_service([])

    statistics = await service.get_user_statistics(10, 20, AS_OF)

    query = repository.calls[0][2]
    assert statistics.as_of == AS_OF
    assert query.last_7_days_started_at == AS_OF - timedelta(days=7)
    assert query.last_30_days_started_at == AS_OF - timedelta(days=30)
    assert statistics.today.total_seconds == 0
    assert statistics.last_7_days.total_seconds == 0
    assert statistics.last_30_days.total_seconds == 0
    assert statistics.all_time.total_seconds == 0


@pytest.mark.asyncio
async def test_multiple_intervals_and_logical_sessions_are_summed() -> None:
    service, _ = make_service(
        [
            Interval(1, AS_OF - timedelta(hours=3), AS_OF - timedelta(hours=2), AS_OF),
            Interval(1, AS_OF - timedelta(hours=2), AS_OF - timedelta(hours=1), AS_OF),
            Interval(2, AS_OF - timedelta(minutes=30), AS_OF, AS_OF),
        ]
    )

    statistics = await service.get_user_statistics(10, 20, AS_OF)

    assert statistics.all_time.exact_seconds == 150 * 60


@pytest.mark.asyncio
async def test_minimum_applies_to_whole_session_exact_time_only() -> None:
    service, _ = make_service(
        [
            Interval(1, AS_OF - timedelta(seconds=9), AS_OF, AS_OF),
            Interval(
                1,
                AS_OF - timedelta(minutes=5, seconds=9),
                AS_OF - timedelta(seconds=9),
                AS_OF,
                quality="estimated",
            ),
            Interval(2, AS_OF - timedelta(seconds=10), AS_OF, AS_OF),
            Interval(
                2,
                AS_OF - timedelta(minutes=2, seconds=10),
                AS_OF - timedelta(seconds=10),
                AS_OF,
                quality="estimated",
            ),
        ],
        threshold=10,
    )

    statistics = await service.get_user_statistics(10, 20, AS_OF)

    assert statistics.all_time.exact_seconds == 10
    assert statistics.all_time.estimated_seconds == 120


def test_query_statement_aggregates_in_sql_with_open_interval_cap() -> None:
    query = VoiceStatisticsQuery(
        as_of=AS_OF,
        today_started_at=AS_OF - timedelta(hours=12),
        last_7_days_started_at=AS_OF - timedelta(days=7),
        last_30_days_started_at=AS_OF - timedelta(days=30),
        min_exact_session_seconds=10,
    )

    sql = str(
        voice_statistics_statement(10, 20, query).compile(dialect=postgresql.dialect())
    ).lower()

    assert "least(voice_sessions.confirmed_through_at" in sql
    assert "least(voice_intervals.ended_at" in sql
    assert "greatest" in sql
    assert "extract(epoch" in sql
    assert "eligible_voice_sessions" in sql
    assert "having sum(case when" in sql
    assert "voice_intervals.is_afk is false" in sql
    assert "insert " not in sql
    assert "update " not in sql
    assert "delete " not in sql


@pytest.mark.asyncio
async def test_sql_repository_maps_one_aggregate_row_without_transaction_control() -> (
    None
):
    row = SimpleNamespace(
        today_exact=1,
        today_estimated=2,
        last_7_days_exact=3,
        last_7_days_estimated=4,
        last_30_days_exact=5,
        last_30_days_estimated=6,
        all_time_exact=7,
        all_time_estimated=8,
    )

    class Result:
        def one(self) -> object:
            return row

    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object) -> Result:
            self.statements.append(statement)
            return Result()

    query = VoiceStatisticsQuery(
        as_of=AS_OF,
        today_started_at=AS_OF - timedelta(hours=12),
        last_7_days_started_at=AS_OF - timedelta(days=7),
        last_30_days_started_at=AS_OF - timedelta(days=30),
        min_exact_session_seconds=10,
    )
    session = Session()
    repository = SqlAlchemyVoiceStatisticsRepository(session)  # type: ignore[arg-type]

    result = await repository.get_user_statistics(10, 20, query)

    assert len(session.statements) == 1
    assert result.today == VoicePeriodDurations(1, 2)
    assert result.all_time == VoicePeriodDurations(7, 8)


def test_duration_dtos_reject_negative_values() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        VoicePeriodDurations(exact_seconds=-1)
