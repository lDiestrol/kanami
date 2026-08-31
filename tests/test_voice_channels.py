from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.voice_statistics import (
    VoiceChannelLeaderboard,
    VoiceChannelUsageEntry,
    VoiceStatisticsPeriod,
    VoiceStatisticsQuery,
    VoiceStatisticsService,
    VoiceUserTopChannels,
)
from discord_stats_bot.persistence.repositories.voice_statistics import (
    SqlAlchemyVoiceStatisticsRepository,
    voice_channel_leaderboard_statement,
    voice_user_top_channels_statement,
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


class InMemoryChannelRepository:
    def __init__(self, intervals: list[Interval]) -> None:
        self.intervals = intervals
        self.user_calls: list[tuple[int, int, VoiceStatisticsQuery]] = []
        self.server_calls: list[
            tuple[int, VoiceStatisticsPeriod, VoiceStatisticsQuery]
        ] = []

    def _entries(
        self,
        query: VoiceStatisticsQuery,
        window_start: datetime | None,
        user_id: int | None,
    ) -> list[VoiceChannelUsageEntry]:
        effective: list[tuple[Interval, datetime]] = []
        for interval in self.intervals:
            if interval.is_bot or interval.is_afk:
                continue
            if user_id is not None and interval.user_id != user_id:
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
        totals: defaultdict[int, dict[str, int]] = defaultdict(
            lambda: {"exact": 0, "estimated": 0}
        )
        for interval, end in effective:
            if interval.session_id not in eligible:
                continue
            start = max(interval.started_at, window_start or interval.started_at)
            totals[interval.channel_id][interval.quality] += max(
                0, int((end - start).total_seconds())
            )
        entries = [
            VoiceChannelUsageEntry(
                channel_id,
                values["exact"],
                values["estimated"],
            )
            for channel_id, values in totals.items()
            if values["exact"] + values["estimated"] > 0
        ]
        entries.sort(
            key=lambda entry: (
                -entry.total_seconds,
                -entry.exact_seconds,
                entry.channel_id,
            )
        )
        return entries

    async def get_user_top_channels(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoiceUserTopChannels:
        self.user_calls.append((guild_id, user_id, query))
        return VoiceUserTopChannels(
            query.as_of, tuple(self._entries(query, None, user_id)[:3])
        )

    async def get_channel_leaderboard(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> VoiceChannelLeaderboard:
        self.server_calls.append((guild_id, period, query))
        return VoiceChannelLeaderboard(
            query.as_of,
            period,
            tuple(self._entries(query, query.started_at_for(period), None)[:10]),
        )


def service(
    intervals: list[Interval],
    *,
    threshold: int = 1,
    timezone: str = "UTC",
) -> tuple[VoiceStatisticsService, InMemoryChannelRepository]:
    repository = InMemoryChannelRepository(intervals)
    return (
        VoiceStatisticsService(
            repository,  # type: ignore[arg-type]
            report_timezone=ZoneInfo(timezone),
            min_session_seconds=threshold,
        ),
        repository,
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


@pytest.mark.asyncio
async def test_user_top_channels_move_ranking_ties_and_top_3() -> None:
    stats, repository = service(
        [
            Interval(
                1,
                10,
                100,
                AS_OF - timedelta(minutes=60),
                AS_OF - timedelta(minutes=40),
                AS_OF,
            ),
            Interval(1, 10, 200, AS_OF - timedelta(minutes=40), AS_OF, AS_OF),
            interval(1, 300, 600, session_id=20),
            interval(1, 400, 500, session_id=30),
            interval(1, 400, 100, session_id=30, quality="estimated"),
            interval(1, 301, 600, session_id=21),
        ]
    )
    result = await stats.get_user_top_channels(10, 1, AS_OF)

    assert result.entries == (
        VoiceChannelUsageEntry(200, 2400, 0),
        VoiceChannelUsageEntry(100, 1200, 0),
        VoiceChannelUsageEntry(300, 600, 0),
    )
    assert len(repository.user_calls) == 1


@pytest.mark.asyncio
async def test_user_channel_ties_use_exact_then_channel_id() -> None:
    stats, repository = service(
        [
            interval(1, 30, 100),
            interval(1, 10, 100, session_id=2),
            interval(1, 20, 90, session_id=3),
            interval(1, 20, 10, session_id=3, quality="estimated"),
        ]
    )

    result = await repository.get_user_top_channels(10, 1, stats._build_query(AS_OF))

    assert [entry.channel_id for entry in result.entries] == [10, 30, 20]


@pytest.mark.asyncio
async def test_channel_threshold_afk_stage_bot_and_estimated_semantics() -> None:
    stats, repository = service(
        [
            interval(1, 10, 9),
            interval(1, 10, 100, quality="estimated"),
            interval(2, 20, 10, channel_kind="stage"),
            interval(2, 20, 100, quality="estimated"),
            interval(3, 30, 1000, is_afk=True),
            interval(4, 40, 1000, is_bot=True),
        ],
        threshold=10,
    )

    result = await repository.get_channel_leaderboard(
        10, VoiceStatisticsPeriod.ALL_TIME, stats._build_query(AS_OF)
    )

    assert result.entries == (VoiceChannelUsageEntry(20, 10, 100),)


@pytest.mark.asyncio
async def test_server_channels_sum_users_intervals_and_split_moves() -> None:
    stats, repository = service(
        [
            interval(1, 10, 100),
            interval(2, 10, 200),
            Interval(
                3,
                30,
                20,
                AS_OF - timedelta(seconds=300),
                AS_OF - timedelta(seconds=200),
                AS_OF,
            ),
            Interval(3, 30, 10, AS_OF - timedelta(seconds=200), AS_OF, AS_OF),
        ]
    )

    result = await repository.get_channel_leaderboard(
        10, VoiceStatisticsPeriod.ALL_TIME, stats._build_query(AS_OF)
    )

    assert result.entries == (
        VoiceChannelUsageEntry(10, 500, 0),
        VoiceChannelUsageEntry(20, 100, 0),
    )


@pytest.mark.asyncio
async def test_server_channel_ties_top_10_and_zero_duration_exclusion() -> None:
    intervals = [interval(user_id, user_id, 1000 - user_id) for user_id in range(1, 13)]
    intervals.extend(
        (
            interval(20, 30, 100),
            interval(21, 20, 90),
            interval(21, 20, 10, quality="estimated"),
            Interval(40, 40, 40, AS_OF, AS_OF, AS_OF),
        )
    )
    stats, repository = service(intervals)

    result = await repository.get_channel_leaderboard(
        10, VoiceStatisticsPeriod.ALL_TIME, stats._build_query(AS_OF)
    )

    assert len(result.entries) == 10
    assert result.entries[0].channel_id == 1
    assert 40 not in {entry.channel_id for entry in result.entries}

    tie_stats, tie_repository = service(
        [
            interval(1, 30, 100),
            interval(2, 10, 100),
            interval(3, 20, 90),
            interval(3, 20, 10, quality="estimated"),
        ]
    )
    tied = await tie_repository.get_channel_leaderboard(
        10, VoiceStatisticsPeriod.ALL_TIME, tie_stats._build_query(AS_OF)
    )
    assert [entry.channel_id for entry in tied.entries] == [10, 30, 20]


@pytest.mark.asyncio
async def test_open_cap_and_window_intersection_for_channel_ranking() -> None:
    stats, repository = service(
        [
            Interval(
                1,
                1,
                10,
                AS_OF - timedelta(minutes=20),
                None,
                AS_OF - timedelta(minutes=5),
            ),
            Interval(
                2,
                2,
                20,
                AS_OF - timedelta(days=7, minutes=20),
                AS_OF - timedelta(days=7) + timedelta(minutes=10),
                AS_OF,
            ),
        ]
    )
    query = stats._build_query(AS_OF)

    all_time = await repository.get_channel_leaderboard(
        10, VoiceStatisticsPeriod.ALL_TIME, query
    )
    week = await repository.get_channel_leaderboard(
        10, VoiceStatisticsPeriod.LAST_7_DAYS, query
    )

    assert {entry.channel_id: entry.exact_seconds for entry in all_time.entries} == {
        10: 900,
        20: 1800,
    }
    assert week.entries == (
        VoiceChannelUsageEntry(10, 900, 0),
        VoiceChannelUsageEntry(20, 600, 0),
    )


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
async def test_channel_periods_reuse_shared_boundaries(
    period: VoiceStatisticsPeriod,
    expected_start: datetime | None,
) -> None:
    stats, repository = service([], timezone="Asia/Yekaterinburg")

    await stats.get_channel_leaderboard(10, period, AS_OF)

    assert repository.server_calls[0][2].started_at_for(period) == expected_start


def query() -> VoiceStatisticsQuery:
    return VoiceStatisticsQuery(
        AS_OF,
        AS_OF - timedelta(hours=12),
        AS_OF - timedelta(days=7),
        AS_OF - timedelta(days=30),
        10,
    )


@pytest.mark.parametrize(
    ("statement", "expected_limit"),
    [
        (voice_user_top_channels_statement(10, 20, query()), "3"),
        (
            voice_channel_leaderboard_statement(
                10, VoiceStatisticsPeriod.LAST_7_DAYS, query()
            ),
            "10",
        ),
    ],
)
def test_channel_sql_is_one_read_only_interval_channel_aggregate(
    statement: object,
    expected_limit: str,
) -> None:
    sql = str(
        statement.compile(  # type: ignore[union-attr]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()

    assert "effective_voice_intervals.channel_id" in sql
    assert "group by effective_voice_intervals.channel_id" in sql
    assert "eligible_voice_sessions" in sql
    assert "voice_intervals.is_afk is false" in sql
    assert "discord_users.is_bot is false" in sql
    assert "least(voice_sessions.confirmed_through_at" in sql
    assert "order by" in sql and "desc" in sql and "asc" in sql
    assert f"limit {expected_limit}" in sql
    assert all(word not in sql for word in ("insert ", "update ", "delete "))


@pytest.mark.asyncio
async def test_channel_repository_methods_execute_one_query_each() -> None:
    rows = [SimpleNamespace(channel_id=10, exact_seconds=20, estimated_seconds=2)]

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, statement: object) -> list[object]:
            self.calls += 1
            return rows

    session = Session()
    repository = SqlAlchemyVoiceStatisticsRepository(session)  # type: ignore[arg-type]

    favorites = await repository.get_user_top_channels(10, 20, query())
    channels = await repository.get_channel_leaderboard(
        10, VoiceStatisticsPeriod.LAST_7_DAYS, query()
    )

    assert session.calls == 2
    assert favorites.entries == (VoiceChannelUsageEntry(10, 20, 2),)
    assert channels.entries == (VoiceChannelUsageEntry(10, 20, 2),)


@pytest.mark.parametrize(
    "dto",
    [
        lambda: VoiceChannelUsageEntry(0, 1, 0),
        lambda: VoiceChannelUsageEntry(1, -1, 0),
        lambda: VoiceUserTopChannels(
            AS_OF, tuple(VoiceChannelUsageEntry(index, 1, 0) for index in range(1, 5))
        ),
        lambda: VoiceChannelLeaderboard(
            AS_OF,
            VoiceStatisticsPeriod.ALL_TIME,
            tuple(VoiceChannelUsageEntry(index, 1, 0) for index in range(1, 12)),
        ),
    ],
)
def test_channel_dtos_validate_ids_durations_and_limits(dto: object) -> None:
    with pytest.raises(ValueError):
        dto()  # type: ignore[operator]
