from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.voice_statistics import (
    VoiceLeaderboard,
    VoiceLeaderboardEntry,
    VoicePeriodStanding,
    VoiceStatisticsPeriod,
    VoiceStatisticsQuery,
    VoiceStatisticsService,
    VoiceUserStandings,
)
from discord_stats_bot.persistence.repositories.voice_statistics import (
    SqlAlchemyVoiceStatisticsRepository,
    voice_leaderboard_statement,
    voice_user_standings_statement,
)

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Interval:
    user_id: int
    session_id: int
    started_at: datetime
    ended_at: datetime | None
    confirmed_through_at: datetime
    quality: str = "exact"
    is_afk: bool = False
    channel_kind: str = "voice"
    is_bot: bool = False


class InMemoryLeaderboardRepository:
    """Semantic double mirroring the shared PostgreSQL query rules."""

    def __init__(self, intervals: list[Interval]) -> None:
        self.intervals = intervals
        self.calls: list[tuple[int, VoiceStatisticsPeriod, VoiceStatisticsQuery]] = []
        self.standing_calls: list[tuple[int, int, VoiceStatisticsQuery]] = []

    async def get_leaderboard(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> VoiceLeaderboard:
        self.calls.append((guild_id, period, query))
        return VoiceLeaderboard(
            query.as_of,
            period,
            tuple(self._ranked_entries(period, query)[:10]),
        )

    def _ranked_entries(
        self,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> list[VoiceLeaderboardEntry]:
        effective: list[tuple[Interval, datetime]] = []
        for interval in self.intervals:
            if interval.is_bot or interval.is_afk:
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
        window_start = query.started_at_for(period)
        totals: defaultdict[int, dict[str, int]] = defaultdict(
            lambda: {"exact": 0, "estimated": 0}
        )
        for interval, end in effective:
            if interval.session_id not in eligible:
                continue
            start = max(interval.started_at, window_start or interval.started_at)
            totals[interval.user_id][interval.quality] += max(
                0,
                int((end - start).total_seconds()),
            )
        entries = [
            VoiceLeaderboardEntry(user_id, values["exact"], values["estimated"])
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
        return entries

    async def get_user_standings(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoiceUserStandings:
        self.standing_calls.append((guild_id, user_id, query))

        def standing(period: VoiceStatisticsPeriod) -> VoicePeriodStanding:
            entries = self._ranked_entries(period, query)
            rank = next(
                (
                    index
                    for index, entry in enumerate(entries, start=1)
                    if entry.user_id == user_id
                ),
                None,
            )
            return VoicePeriodStanding(rank, len(entries))

        return VoiceUserStandings(
            query.as_of,
            standing(VoiceStatisticsPeriod.TODAY),
            standing(VoiceStatisticsPeriod.LAST_7_DAYS),
            standing(VoiceStatisticsPeriod.LAST_30_DAYS),
            standing(VoiceStatisticsPeriod.ALL_TIME),
        )

    async def get_user_statistics(self, *args: object) -> object:
        raise AssertionError("leaderboard must use one grouped query")


def make_service(
    intervals: list[Interval],
    *,
    timezone: str = "UTC",
    threshold: int = 1,
) -> tuple[VoiceStatisticsService, InMemoryLeaderboardRepository]:
    repository = InMemoryLeaderboardRepository(intervals)
    return (
        VoiceStatisticsService(
            repository,
            report_timezone=ZoneInfo(timezone),
            min_session_seconds=threshold,
        ),
        repository,
    )


def closed(
    user_id: int,
    seconds: int,
    *,
    session_id: int | None = None,
    quality: str = "exact",
    **kwargs: object,
) -> Interval:
    return Interval(
        user_id=user_id,
        session_id=session_id or user_id,
        started_at=AS_OF - timedelta(seconds=seconds),
        ended_at=AS_OF,
        confirmed_through_at=AS_OF,
        quality=quality,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_exact_and_estimated_are_ranked_by_total() -> None:
    service, _ = make_service(
        [
            closed(1, 100),
            closed(2, 80),
            closed(2, 30, session_id=2, quality="estimated"),
        ]
    )

    leaderboard = await service.get_leaderboard(
        10, VoiceStatisticsPeriod.ALL_TIME, AS_OF
    )

    assert [entry.user_id for entry in leaderboard.entries] == [2, 1]
    assert leaderboard.entries[0].exact_seconds == 80
    assert leaderboard.entries[0].estimated_seconds == 30


@pytest.mark.asyncio
async def test_ties_use_exact_desc_then_user_id_asc() -> None:
    service, _ = make_service(
        [
            closed(3, 100),
            closed(1, 100),
            closed(2, 90),
            closed(2, 10, quality="estimated"),
        ]
    )

    leaderboard = await service.get_leaderboard(
        10, VoiceStatisticsPeriod.ALL_TIME, AS_OF
    )

    assert [entry.user_id for entry in leaderboard.entries] == [1, 3, 2]


@pytest.mark.asyncio
async def test_top_10_excludes_zero_duration_and_bots() -> None:
    intervals = [closed(user_id, 1000 - user_id) for user_id in range(1, 13)]
    intervals.append(closed(99, 5000, is_bot=True))
    intervals.append(Interval(50, 50, AS_OF, AS_OF, AS_OF))
    service, _ = make_service(intervals)

    leaderboard = await service.get_leaderboard(
        10, VoiceStatisticsPeriod.ALL_TIME, AS_OF
    )

    assert len(leaderboard.entries) == 10
    assert [entry.user_id for entry in leaderboard.entries] == list(range(1, 11))
    assert 99 not in {entry.user_id for entry in leaderboard.entries}


@pytest.mark.asyncio
async def test_open_intervals_use_confirmation_capped_by_as_of() -> None:
    service, _ = make_service(
        [
            Interval(
                1,
                1,
                AS_OF - timedelta(minutes=20),
                None,
                AS_OF - timedelta(minutes=5),
            ),
            Interval(
                2,
                2,
                AS_OF - timedelta(minutes=20),
                None,
                AS_OF + timedelta(minutes=5),
            ),
        ]
    )

    leaderboard = await service.get_leaderboard(
        10, VoiceStatisticsPeriod.ALL_TIME, AS_OF
    )

    assert [entry.exact_seconds for entry in leaderboard.entries] == [1200, 900]


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
async def test_all_periods_share_stats_window_semantics(
    period: VoiceStatisticsPeriod,
    expected_start: datetime | None,
) -> None:
    service, repository = make_service([], timezone="Asia/Yekaterinburg")

    leaderboard = await service.get_leaderboard(10, period, AS_OF)

    query = repository.calls[0][2]
    assert leaderboard.as_of == AS_OF
    assert query.started_at_for(period) == expected_start


@pytest.mark.asyncio
async def test_interval_crossing_window_start_counts_only_overlap() -> None:
    service, _ = make_service(
        [
            Interval(
                1,
                1,
                AS_OF - timedelta(days=7, minutes=20),
                AS_OF - timedelta(days=7) + timedelta(minutes=10),
                AS_OF,
            )
        ]
    )

    leaderboard = await service.get_leaderboard(
        10, VoiceStatisticsPeriod.LAST_7_DAYS, AS_OF
    )

    assert leaderboard.entries[0].exact_seconds == 10 * 60


@pytest.mark.asyncio
async def test_afk_is_excluded_and_stage_is_included() -> None:
    service, _ = make_service(
        [
            closed(1, 100, is_afk=True),
            closed(2, 50, channel_kind="stage"),
        ]
    )

    leaderboard = await service.get_leaderboard(
        10, VoiceStatisticsPeriod.ALL_TIME, AS_OF
    )

    assert [entry.user_id for entry in leaderboard.entries] == [2]


@pytest.mark.asyncio
async def test_session_threshold_uses_exact_only_and_includes_estimated_after_pass() -> (
    None
):
    service, _ = make_service(
        [
            closed(1, 9),
            closed(1, 100, quality="estimated"),
            closed(2, 10),
            closed(2, 100, quality="estimated"),
        ],
        threshold=10,
    )

    leaderboard = await service.get_leaderboard(
        10, VoiceStatisticsPeriod.ALL_TIME, AS_OF
    )

    assert len(leaderboard.entries) == 1
    assert leaderboard.entries[0] == VoiceLeaderboardEntry(2, 10, 100)


@pytest.mark.asyncio
async def test_user_standings_include_first_second_and_rank_beyond_top_10() -> None:
    service, repository = make_service(
        [closed(user_id, 1000 - user_id) for user_id in range(1, 13)]
    )

    first = await service.get_user_standings(10, 1, AS_OF)
    second = await service.get_user_standings(10, 2, AS_OF)
    eleventh = await service.get_user_standings(10, 11, AS_OF)

    assert first.all_time == VoicePeriodStanding(1, 12)
    assert second.all_time == VoicePeriodStanding(2, 12)
    assert eleventh.all_time == VoicePeriodStanding(11, 12)
    assert [call[:2] for call in repository.standing_calls] == [
        (10, 1),
        (10, 2),
        (10, 11),
    ]


@pytest.mark.asyncio
async def test_user_standing_ties_use_exact_then_user_id() -> None:
    service, _ = make_service(
        [
            closed(3, 100),
            closed(1, 100),
            closed(2, 90),
            closed(2, 10, quality="estimated"),
        ]
    )

    standings = {
        user_id: await service.get_user_standings(10, user_id, AS_OF)
        for user_id in (1, 2, 3)
    }

    assert standings[1].all_time.rank == 1
    assert standings[3].all_time.rank == 2
    assert standings[2].all_time.rank == 3
    assert {item.all_time.participant_count for item in standings.values()} == {3}


@pytest.mark.asyncio
async def test_user_standings_exclude_ineligible_bot_afk_and_zero_duration() -> None:
    service, _ = make_service(
        [
            closed(1, 9),
            closed(1, 100, quality="estimated"),
            closed(2, 10, channel_kind="stage"),
            closed(2, 100, quality="estimated"),
            closed(3, 1000, is_bot=True),
            closed(4, 1000, is_afk=True),
            Interval(5, 5, AS_OF, AS_OF, AS_OF),
        ],
        threshold=10,
    )

    qualified = await service.get_user_standings(10, 2, AS_OF)
    ineligible = await service.get_user_standings(10, 1, AS_OF)
    bot = await service.get_user_standings(10, 3, AS_OF)

    assert qualified.all_time == VoicePeriodStanding(1, 1)
    assert ineligible.all_time == VoicePeriodStanding(None, 1)
    assert bot.all_time == VoicePeriodStanding(None, 1)


@pytest.mark.asyncio
async def test_user_standings_have_no_rank_when_the_guild_has_no_activity() -> None:
    service, _ = make_service([])

    standings = await service.get_user_standings(10, 99, AS_OF)

    assert standings.today == VoicePeriodStanding(None, 0)
    assert standings.last_7_days == VoicePeriodStanding(None, 0)
    assert standings.last_30_days == VoicePeriodStanding(None, 0)
    assert standings.all_time == VoicePeriodStanding(None, 0)


@pytest.mark.asyncio
async def test_all_user_standing_periods_use_one_query_snapshot() -> None:
    service, repository = make_service([], timezone="Asia/Yekaterinburg")

    standings = await service.get_user_standings(10, 20, AS_OF)

    assert len(repository.standing_calls) == 1
    query = repository.standing_calls[0][2]
    assert standings.as_of == AS_OF
    assert query.today_started_at == datetime(2026, 8, 13, 19, tzinfo=UTC)
    assert query.last_7_days_started_at == AS_OF - timedelta(days=7)
    assert query.last_30_days_started_at == AS_OF - timedelta(days=30)
    assert query.started_at_for(VoiceStatisticsPeriod.ALL_TIME) is None


def test_leaderboard_statement_is_one_read_only_bounded_aggregate() -> None:
    query = VoiceStatisticsQuery(
        AS_OF,
        AS_OF - timedelta(hours=12),
        AS_OF - timedelta(days=7),
        AS_OF - timedelta(days=30),
        10,
    )
    sql = str(
        voice_leaderboard_statement(
            10, VoiceStatisticsPeriod.LAST_7_DAYS, query
        ).compile(dialect=postgresql.dialect())
    ).lower()

    assert "least(voice_sessions.confirmed_through_at" in sql
    assert "least(voice_intervals.ended_at" in sql
    assert "discord_users.is_bot is false" in sql
    assert "voice_intervals.is_afk is false" in sql
    assert "eligible_voice_sessions" in sql
    assert "group by effective_voice_intervals.user_id" in sql
    assert "order by" in sql and "desc" in sql and "asc" in sql
    assert "limit" in sql
    assert all(word not in sql for word in ("insert ", "update ", "delete "))


def test_standings_statement_is_one_read_only_unbounded_ranking_query() -> None:
    query = VoiceStatisticsQuery(
        AS_OF,
        AS_OF - timedelta(hours=12),
        AS_OF - timedelta(days=7),
        AS_OF - timedelta(days=30),
        10,
    )

    sql = str(
        voice_user_standings_statement(10, 20, query).compile(
            dialect=postgresql.dialect()
        )
    ).lower()

    assert "row_number() over" in sql
    assert "partition by" in sql
    assert (
        "order by active_voice_standing_totals.exact_seconds + "
        "active_voice_standing_totals.estimated_seconds desc, "
        "active_voice_standing_totals.exact_seconds desc, "
        "active_voice_standing_totals.user_id asc"
    ) in sql
    assert "count(case when" in sql
    assert "discord_users.is_bot is false" in sql
    assert "voice_intervals.is_afk is false" in sql
    assert "eligible_voice_sessions" in sql
    assert "least(voice_sessions.confirmed_through_at" in sql
    assert "union all" in sql
    assert "limit" not in sql
    assert all(word not in sql for word in ("insert ", "update ", "delete "))


@pytest.mark.asyncio
async def test_repository_executes_one_query_and_maps_entries() -> None:
    rows = [
        SimpleNamespace(user_id=1, exact_seconds=20, estimated_seconds=2),
        SimpleNamespace(user_id=2, exact_seconds=10, estimated_seconds=0),
    ]

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, statement: object) -> list[object]:
            self.calls += 1
            return rows

    query = VoiceStatisticsQuery(
        AS_OF,
        AS_OF - timedelta(hours=12),
        AS_OF - timedelta(days=7),
        AS_OF - timedelta(days=30),
        10,
    )
    session = Session()
    repository = SqlAlchemyVoiceStatisticsRepository(session)  # type: ignore[arg-type]

    leaderboard = await repository.get_leaderboard(
        10, VoiceStatisticsPeriod.LAST_7_DAYS, query
    )

    assert session.calls == 1
    assert leaderboard.entries == (
        VoiceLeaderboardEntry(1, 20, 2),
        VoiceLeaderboardEntry(2, 10, 0),
    )


@pytest.mark.asyncio
async def test_standings_repository_executes_one_query_and_maps_all_periods() -> None:
    row = SimpleNamespace(
        today_rank=1,
        today_participant_count=2,
        last_7_days_rank=11,
        last_7_days_participant_count=14,
        last_30_days_rank=None,
        last_30_days_participant_count=14,
        all_time_rank=None,
        all_time_participant_count=0,
    )

    class Result:
        def one(self) -> object:
            return row

    class Session:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, statement: object) -> Result:
            self.calls += 1
            return Result()

    query = VoiceStatisticsQuery(
        AS_OF,
        AS_OF - timedelta(hours=12),
        AS_OF - timedelta(days=7),
        AS_OF - timedelta(days=30),
        10,
    )
    session = Session()

    standings = await SqlAlchemyVoiceStatisticsRepository(  # type: ignore[arg-type]
        session
    ).get_user_standings(10, 20, query)

    assert session.calls == 1
    assert standings.today == VoicePeriodStanding(1, 2)
    assert standings.last_7_days == VoicePeriodStanding(11, 14)
    assert standings.last_30_days == VoicePeriodStanding(None, 14)
    assert standings.all_time == VoicePeriodStanding(None, 0)


@pytest.mark.parametrize(
    ("rank", "participant_count"),
    [(0, 1), (2, 1), (1, 0), (None, -1)],
)
def test_standing_dto_rejects_invalid_values(
    rank: int | None,
    participant_count: int,
) -> None:
    with pytest.raises(ValueError):
        VoicePeriodStanding(rank, participant_count)


@pytest.mark.asyncio
async def test_service_rejects_unvalidated_period() -> None:
    service, repository = make_service([])

    with pytest.raises(ValueError, match="VoiceStatisticsPeriod"):
        await service.get_leaderboard(10, "week", AS_OF)  # type: ignore[arg-type]

    assert repository.calls == []
