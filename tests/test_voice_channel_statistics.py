from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.voice_statistics import (
    VoiceChannelStatistics,
    VoiceLeaderboardEntry,
    VoiceStatisticsPeriod,
    VoiceStatisticsQuery,
    VoiceStatisticsService,
)
from discord_stats_bot.persistence.repositories.voice_statistics import (
    SqlAlchemyVoiceStatisticsRepository,
    voice_channel_statistics_statement,
)

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Interval:
    user_id: int
    session_id: int
    channel_id: int
    started_at: datetime
    ended_at: datetime | None
    confirmed_through_at: datetime
    quality: str = "exact"
    is_afk: bool = False
    is_bot: bool = False
    channel_kind: str = "voice"


class InMemoryRepository:
    def __init__(self, intervals: list[Interval]) -> None:
        self.intervals = intervals
        self.calls: list[
            tuple[int, int, VoiceStatisticsPeriod, VoiceStatisticsQuery]
        ] = []

    async def get_channel_statistics(
        self,
        guild_id: int,
        channel_id: int,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> VoiceChannelStatistics:
        self.calls.append((guild_id, channel_id, period, query))
        effective: list[tuple[Interval, datetime]] = []
        for interval in self.intervals:
            if interval.is_afk or interval.is_bot:
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
        started_at = query.started_at_for(period)
        totals: defaultdict[int, dict[str, int]] = defaultdict(
            lambda: {"exact": 0, "estimated": 0}
        )
        for interval, end in effective:
            if interval.session_id not in eligible or interval.channel_id != channel_id:
                continue
            overlap_start = max(interval.started_at, started_at or interval.started_at)
            totals[interval.user_id][interval.quality] += max(
                0, int((end - overlap_start).total_seconds())
            )
        entries = [
            VoiceLeaderboardEntry(
                user_id,
                values["exact"],
                values["estimated"],
            )
            for user_id, values in totals.items()
            if values["exact"] + values["estimated"] > 0
        ]
        entries.sort(
            key=lambda entry: (
                -entry.total_seconds,
                -entry.exact_seconds,
                entry.user_id,
            )
        )
        return VoiceChannelStatistics(
            query.as_of,
            period,
            channel_id,
            sum(entry.exact_seconds for entry in entries),
            sum(entry.estimated_seconds for entry in entries),
            tuple(entries[:10]),
        )


def interval(
    user_id: int,
    channel_id: int,
    seconds: int,
    *,
    session_id: int | None = None,
    quality: str = "exact",
    **kwargs: object,
) -> Interval:
    return Interval(
        user_id,
        session_id or user_id,
        channel_id,
        AS_OF - timedelta(seconds=seconds),
        AS_OF,
        AS_OF,
        quality=quality,
        **kwargs,
    )


def service(
    intervals: list[Interval],
    *,
    threshold: int = 10,
    timezone: str = "UTC",
) -> tuple[VoiceStatisticsService, InMemoryRepository]:
    repository = InMemoryRepository(intervals)
    return (
        VoiceStatisticsService(
            repository,  # type: ignore[arg-type]
            report_timezone=ZoneInfo(timezone),
            min_session_seconds=threshold,
        ),
        repository,
    )


@pytest.mark.asyncio
async def test_channel_users_intervals_and_moves_are_aggregated() -> None:
    stats, _ = service(
        [
            interval(1, 10, 30, session_id=11),
            interval(1, 10, 20, session_id=12),
            interval(2, 10, 40),
            interval(3, 20, 100),
        ]
    )

    report = await stats.get_channel_statistics(
        10, 10, VoiceStatisticsPeriod.ALL_TIME, AS_OF
    )

    assert report.total_seconds == 90
    assert [(entry.user_id, entry.total_seconds) for entry in report.entries] == [
        (1, 50),
        (2, 40),
    ]


@pytest.mark.asyncio
async def test_one_second_moved_interval_keeps_whole_session_eligibility() -> None:
    stats, _ = service(
        [
            Interval(
                1,
                11,
                10,
                AS_OF - timedelta(seconds=1201),
                AS_OF - timedelta(seconds=1),
                AS_OF,
            ),
            interval(1, 20, 1, session_id=11),
        ],
        threshold=1200,
    )

    report = await stats.get_channel_statistics(
        10, 20, VoiceStatisticsPeriod.ALL_TIME, AS_OF
    )

    assert report.entries == (VoiceLeaderboardEntry(1, 1, 0),)


@pytest.mark.asyncio
async def test_threshold_estimated_afk_bot_stage_and_open_cap() -> None:
    stats, _ = service(
        [
            interval(1, 10, 9),
            interval(1, 10, 100, quality="estimated"),
            interval(2, 10, 10, channel_kind="stage"),
            interval(2, 10, 5, session_id=2, quality="estimated"),
            interval(3, 10, 100, is_afk=True),
            interval(4, 10, 100, is_bot=True),
            Interval(
                5,
                5,
                10,
                AS_OF - timedelta(seconds=100),
                None,
                AS_OF - timedelta(seconds=40),
            ),
        ]
    )

    report = await stats.get_channel_statistics(
        10, 10, VoiceStatisticsPeriod.ALL_TIME, AS_OF
    )

    assert report.exact_seconds == 70
    assert report.estimated_seconds == 5
    assert [entry.user_id for entry in report.entries] == [5, 2]


@pytest.mark.parametrize(
    ("period", "expected_start"),
    [
        (VoiceStatisticsPeriod.TODAY, datetime(2026, 8, 13, 19, tzinfo=UTC)),
        (VoiceStatisticsPeriod.LAST_7_DAYS, AS_OF - timedelta(days=7)),
        (VoiceStatisticsPeriod.LAST_30_DAYS, AS_OF - timedelta(days=30)),
        (VoiceStatisticsPeriod.ALL_TIME, None),
    ],
)
@pytest.mark.asyncio
async def test_channel_statistics_reuses_shared_period_boundaries(
    period: VoiceStatisticsPeriod,
    expected_start: datetime | None,
) -> None:
    stats, repository = service([], timezone="Asia/Yekaterinburg")

    await stats.get_channel_statistics(10, 20, period, AS_OF)

    assert repository.calls[0][3].started_at_for(period) == expected_start


@pytest.mark.parametrize(
    ("period", "expected_seconds"),
    [
        (VoiceStatisticsPeriod.TODAY, 12 * 60 * 60),
        (VoiceStatisticsPeriod.LAST_7_DAYS, 7 * 24 * 60 * 60),
        (VoiceStatisticsPeriod.LAST_30_DAYS, 8 * 24 * 60 * 60),
        (VoiceStatisticsPeriod.ALL_TIME, 8 * 24 * 60 * 60),
    ],
)
@pytest.mark.asyncio
async def test_channel_statistics_intersects_selected_period_lower_bound(
    period: VoiceStatisticsPeriod,
    expected_seconds: int,
) -> None:
    stats, _ = service([interval(1, 10, 8 * 24 * 60 * 60)])

    report = await stats.get_channel_statistics(10, 10, period, AS_OF)

    assert report.total_seconds == expected_seconds


@pytest.mark.asyncio
async def test_ranking_top_10_ties_and_total_include_users_outside_top() -> None:
    intervals = [interval(user_id, 10, 1000 - user_id) for user_id in range(1, 13)]
    intervals.extend(
        (
            interval(20, 10, 100),
            interval(21, 10, 90),
            interval(21, 10, 10, quality="estimated"),
            Interval(30, 30, 10, AS_OF, AS_OF, AS_OF),
        )
    )
    stats, _ = service(intervals)

    report = await stats.get_channel_statistics(
        10, 10, VoiceStatisticsPeriod.ALL_TIME, AS_OF
    )

    assert len(report.entries) == 10
    assert report.total_seconds == sum(1000 - user_id for user_id in range(1, 13)) + 200
    assert [entry.user_id for entry in report.entries[:2]] == [1, 2]
    assert 11 not in {entry.user_id for entry in report.entries}

    tied_stats, _ = service(
        [
            interval(30, 10, 100),
            interval(10, 10, 100),
            interval(20, 10, 90),
            interval(20, 10, 10, quality="estimated"),
        ]
    )
    tied = await tied_stats.get_channel_statistics(
        10, 10, VoiceStatisticsPeriod.ALL_TIME, AS_OF
    )
    assert [entry.user_id for entry in tied.entries] == [10, 30, 20]


def query() -> VoiceStatisticsQuery:
    return VoiceStatisticsQuery(
        AS_OF,
        AS_OF - timedelta(hours=12),
        AS_OF - timedelta(days=7),
        AS_OF - timedelta(days=30),
        10,
    )


def test_channel_statistics_sql_is_one_read_only_aggregate() -> None:
    statement = voice_channel_statistics_statement(
        10, 20, VoiceStatisticsPeriod.LAST_7_DAYS, query()
    )
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    assert "eligible_voice_sessions" in sql
    assert "voice_channel_user_totals" in sql
    assert "active_voice_channel_users" in sql
    assert "ranked_voice_channel_users" in sql
    assert "selected_voice_channel_totals" in sql
    assert "voice_intervals.is_afk is false" in sql
    assert "discord_users.is_bot is false" in sql
    assert "effective_voice_intervals.channel_id = 20" in sql
    assert "row_number() over" in sql
    assert "left outer join ranked_voice_channel_users" in sql
    assert "on ranked_voice_channel_users.rank <= 10" in sql
    assert "sum(active_voice_channel_users.exact_seconds) over" not in sql
    selected_total_sql = sql.split("selected_voice_channel_totals as", 1)[1].split(
        "), \nvoice_channel_user_totals as", 1
    )[0]
    assert "floor(coalesce(sum(" in selected_total_sql
    assert "group by effective_voice_intervals.user_id" not in selected_total_sql
    assert "active_voice_channel_users" not in selected_total_sql
    assert sql.index("eligible_voice_sessions as") < sql.index(
        "where effective_voice_intervals.channel_id = 20"
    )
    assert "where ranked_voice_channel_users.rank <= 10" not in sql
    assert all(word not in sql for word in ("insert ", "update ", "delete "))


@pytest.mark.asyncio
async def test_repository_executes_once_and_preserves_total_outside_top() -> None:
    rows = [
        SimpleNamespace(
            user_id=1,
            exact_seconds=100,
            estimated_seconds=2,
            channel_exact_seconds=500,
            channel_estimated_seconds=20,
        )
    ]

    class Result:
        def all(self) -> list[object]:
            return rows

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, statement: object) -> Result:
            self.calls += 1
            return Result()

    session = Session()
    repository = SqlAlchemyVoiceStatisticsRepository(session)  # type: ignore[arg-type]

    report = await repository.get_channel_statistics(
        10, 20, VoiceStatisticsPeriod.ALL_TIME, query()
    )

    assert session.calls == 1
    assert (report.exact_seconds, report.estimated_seconds) == (500, 20)
    assert report.entries == (VoiceLeaderboardEntry(1, 100, 2),)


@pytest.mark.asyncio
async def test_repository_empty_result_returns_zero_total() -> None:
    class Result:
        def all(self) -> list[object]:
            return []

    class Session:
        async def execute(self, statement: object) -> Result:
            return Result()

    repository = SqlAlchemyVoiceStatisticsRepository(Session())  # type: ignore[arg-type]

    report = await repository.get_channel_statistics(
        10, 20, VoiceStatisticsPeriod.ALL_TIME, query()
    )

    assert report.total_seconds == 0
    assert report.entries == ()


@pytest.mark.asyncio
async def test_repository_keeps_total_without_nullable_ranked_user() -> None:
    rows = [
        SimpleNamespace(
            user_id=None,
            exact_seconds=None,
            estimated_seconds=None,
            channel_exact_seconds=1,
            channel_estimated_seconds=0,
        )
    ]

    class Result:
        def all(self) -> list[object]:
            return rows

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, statement: object) -> Result:
            self.calls += 1
            return Result()

    session = Session()
    repository = SqlAlchemyVoiceStatisticsRepository(session)  # type: ignore[arg-type]

    report = await repository.get_channel_statistics(
        10, 20, VoiceStatisticsPeriod.ALL_TIME, query()
    )

    assert session.calls == 1
    assert report.exact_seconds == 1
    assert report.entries == ()


@pytest.mark.parametrize(
    "dto",
    [
        lambda: VoiceChannelStatistics(
            AS_OF, VoiceStatisticsPeriod.ALL_TIME, 0, 1, 0, ()
        ),
        lambda: VoiceChannelStatistics(
            AS_OF, VoiceStatisticsPeriod.ALL_TIME, 1, -1, 0, ()
        ),
        lambda: VoiceChannelStatistics(
            AS_OF,
            VoiceStatisticsPeriod.ALL_TIME,
            1,
            1,
            0,
            tuple(VoiceLeaderboardEntry(index, 1, 0) for index in range(1, 12)),
        ),
    ],
)
def test_channel_statistics_dto_validation(dto: object) -> None:
    with pytest.raises(ValueError):
        dto()  # type: ignore[operator]
