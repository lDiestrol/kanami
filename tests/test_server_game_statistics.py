from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.features.game_tracking import (
    GameSessionSlice,
    GameStatisticsPeriod,
    GameStatisticsService,
    ServerGameSessionSlice,
    ServerGameStatisticsPeriod,
    ServerGameStatisticsService,
    build_server_game_statistics_window,
)

AS_OF = datetime(2026, 8, 31, 12, tzinfo=UTC)


def server_session(
    session_id: int,
    user_id: int,
    display_name: str,
    game_name: str,
    started_at: datetime,
    effective_end: datetime,
    *,
    open_session: bool = False,
) -> ServerGameSessionSlice:
    return ServerGameSessionSlice(
        session_id,
        user_id,
        display_name,
        game_name,
        started_at,
        effective_end,
        None if open_session else effective_end,
    )


class RecordingRepository:
    def __init__(
        self,
        sessions: tuple[ServerGameSessionSlice, ...] = (),
        earliest: datetime | None = None,
    ) -> None:
        self.sessions = sessions
        self.earliest = earliest
        self.session_calls: list[tuple[int, datetime, datetime]] = []
        self.earliest_calls: list[int] = []

    async def list_server_sessions(
        self,
        guild_id: int,
        *,
        started_after: datetime,
        ended_before: datetime,
    ) -> tuple[ServerGameSessionSlice, ...]:
        self.session_calls.append((guild_id, started_after, ended_before))
        return self.sessions

    async def get_earliest_confirmed_activity(self, guild_id: int) -> datetime | None:
        self.earliest_calls.append(guild_id)
        return self.earliest


@pytest.mark.parametrize(
    ("period", "days"),
    [
        (ServerGameStatisticsPeriod.LAST_7_DAYS, 7),
        (ServerGameStatisticsPeriod.LAST_30_DAYS, 30),
        (ServerGameStatisticsPeriod.LAST_90_DAYS, 90),
    ],
)
@pytest.mark.asyncio
async def test_periods_use_completed_local_days_and_bounded_repository_reads(
    period: ServerGameStatisticsPeriod,
    days: int,
) -> None:
    repository = RecordingRepository()
    timezone = ZoneInfo("Asia/Yekaterinburg")

    report = await ServerGameStatisticsService(
        repository,
        report_timezone=timezone,
    ).get_report(10, period, AS_OF)

    assert report.window.ended_before.isoformat() == "2026-08-31"
    assert (
        report.window.started_on.isoformat()
        == (datetime(2026, 8, 31, tzinfo=timezone) - timedelta(days=days))
        .date()
        .isoformat()
    )
    assert report.window.ended_at == datetime(2026, 8, 30, 19, tzinfo=UTC)
    assert repository.session_calls == [
        (10, report.window.started_at, report.window.ended_at)
    ]
    assert repository.earliest_calls == [10]
    assert len(report.daily) == days


def test_completed_day_window_is_dst_safe() -> None:
    timezone = ZoneInfo("Europe/Stockholm")
    window = build_server_game_statistics_window(
        ServerGameStatisticsPeriod.LAST_7_DAYS,
        datetime(2026, 3, 30, 12, tzinfo=UTC),
        report_timezone=timezone,
    )

    assert window.started_on.isoformat() == "2026-03-23"
    assert window.ended_before.isoformat() == "2026-03-30"
    assert window.started_at == datetime(2026, 3, 22, 23, tzinfo=UTC)
    assert window.ended_at == datetime(2026, 3, 29, 22, tzinfo=UTC)
    assert window.ended_at - window.started_at == timedelta(hours=167)


@pytest.mark.asyncio
async def test_aggregation_clips_confirmed_person_time_and_builds_rankings() -> None:
    sessions = (
        server_session(
            1,
            10,
            "Alice",
            "Strinova",
            datetime(2026, 8, 23, 23, tzinfo=UTC),
            datetime(2026, 8, 24, 2, tzinfo=UTC),
        ),
        server_session(
            2,
            20,
            "Bob",
            "strinova with Medal",
            datetime(2026, 8, 24, 1, tzinfo=UTC),
            datetime(2026, 8, 24, 3, tzinfo=UTC),
        ),
        server_session(
            3,
            10,
            "Alice",
            "Minecraft",
            datetime(2026, 8, 25, 23, tzinfo=UTC),
            datetime(2026, 8, 26, 1, tzinfo=UTC),
        ),
        server_session(
            4,
            20,
            "Bob",
            "Krita",
            datetime(2026, 8, 27, 10, tzinfo=UTC),
            datetime(2026, 8, 27, 12, tzinfo=UTC),
            open_session=True,
        ),
        server_session(
            5,
            30,
            "Zero",
            "Ignored",
            datetime(2026, 8, 28, 10, tzinfo=UTC),
            datetime(2026, 8, 28, 10, tzinfo=UTC),
        ),
        server_session(
            6,
            40,
            "Outside",
            "Ignored",
            datetime(2026, 8, 20, 10, tzinfo=UTC),
            datetime(2026, 8, 20, 11, tzinfo=UTC),
        ),
    )
    report = await ServerGameStatisticsService(
        RecordingRepository(sessions, datetime(2026, 8, 23, tzinfo=UTC)),
        report_timezone=ZoneInfo("UTC"),
    ).get_report(10, ServerGameStatisticsPeriod.LAST_7_DAYS, AS_OF)

    assert report.total_seconds == 8 * 3600
    assert report.active_gamers == 2
    assert report.unique_games == 3
    assert report.average_seconds_per_gamer == 4 * 3600
    assert [
        (item.game_name, item.total_seconds, item.unique_gamers)
        for item in report.top_games
    ] == [
        ("Strinova", 4 * 3600, 2),
        ("Krita", 2 * 3600, 1),
        ("Minecraft", 2 * 3600, 1),
    ]
    assert [
        (
            item.display_name,
            item.total_seconds,
            item.unique_games,
            item.gaming_days,
        )
        for item in report.top_players
    ] == [
        ("Alice", 4 * 3600, 2, 3),
        ("Bob", 4 * 3600, 2, 2),
    ]
    assert report.daily[0].total_seconds == 4 * 3600
    assert report.daily[0].unique_gamers == 2
    assert report.daily[1].total_seconds == 3600
    assert report.daily[2].total_seconds == 3600
    assert report.daily[3].total_seconds == 2 * 3600
    assert all(point.total_seconds == 0 for point in report.daily[4:])
    assert report.period_may_be_partial is False


@pytest.mark.asyncio
async def test_empty_report_has_zero_buckets_average_and_honest_coverage() -> None:
    earliest = datetime(2026, 8, 29, 10, tzinfo=UTC)
    report = await ServerGameStatisticsService(
        RecordingRepository(earliest=earliest),
        report_timezone=ZoneInfo("UTC"),
    ).get_report(10, ServerGameStatisticsPeriod.LAST_30_DAYS, AS_OF)

    assert report.has_data is False
    assert report.total_seconds == 0
    assert report.average_seconds_per_gamer == 0
    assert len(report.daily) == 30
    assert all(
        (point.total_seconds, point.unique_gamers) == (0, 0) for point in report.daily
    )
    assert report.top_games == ()
    assert report.top_players == ()
    assert report.earliest_recorded_on.isoformat() == "2026-08-29"
    assert report.period_may_be_partial is True


@pytest.mark.parametrize(
    ("earliest_offset", "expected_partial"),
    [
        (timedelta(), False),
        (timedelta(hours=18), True),
        (timedelta(seconds=-1), False),
        (None, None),
    ],
)
@pytest.mark.asyncio
async def test_coverage_compares_exact_earliest_timestamp(
    earliest_offset: timedelta | None,
    expected_partial: bool | None,
) -> None:
    window = build_server_game_statistics_window(
        ServerGameStatisticsPeriod.LAST_7_DAYS,
        AS_OF,
        report_timezone=ZoneInfo("UTC"),
    )
    earliest = (
        window.started_at + earliest_offset if earliest_offset is not None else None
    )

    report = await ServerGameStatisticsService(
        RecordingRepository(earliest=earliest),
        report_timezone=ZoneInfo("UTC"),
    ).get_report(10, ServerGameStatisticsPeriod.LAST_7_DAYS, AS_OF)

    assert report.period_may_be_partial is expected_partial
    assert report.earliest_recorded_on == (
        earliest.astimezone(ZoneInfo("UTC")).date() if earliest is not None else None
    )


@pytest.mark.asyncio
async def test_positive_subsecond_overlap_preserves_zero_integer_total() -> None:
    started_at = datetime(2026, 8, 29, 10, tzinfo=UTC)
    report = await ServerGameStatisticsService(
        RecordingRepository(
            (
                server_session(
                    1,
                    10,
                    "Alice",
                    "Subsecond Game",
                    started_at,
                    started_at + timedelta(milliseconds=500),
                ),
            )
        ),
        report_timezone=ZoneInfo("UTC"),
    ).get_report(10, ServerGameStatisticsPeriod.LAST_7_DAYS, AS_OF)

    assert report.active_gamers == 1
    assert report.total_seconds == 0
    assert len(report.top_games) == 1
    assert report.top_games[0].total_seconds == 0


@pytest.mark.asyncio
async def test_server_and_member_statistics_share_canonicalization_semantics() -> None:
    start = datetime(2026, 8, 29, 10, tzinfo=UTC)
    end = datetime(2026, 8, 29, 12, tzinfo=UTC)
    server_sessions = (
        server_session(1, 10, "Alice", " Game WITH MEDAL ", start, end),
        server_session(2, 20, "Bob", "game", end, end + timedelta(hours=1)),
    )

    class MemberRepository:
        async def list_user_sessions(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return tuple(
                GameSessionSlice(
                    item.session_id,
                    f"raw:{item.session_id}",
                    item.game_name,
                    item.started_at,
                    item.confirmed_through_at,
                    item.ended_at,
                )
                for item in server_sessions
            )

    member = await GameStatisticsService(
        MemberRepository(),  # type: ignore[arg-type]
        report_timezone=ZoneInfo("UTC"),
    ).get_user_statistics(10, 10, GameStatisticsPeriod.LAST_30_DAYS, AS_OF)
    server = await ServerGameStatisticsService(
        RecordingRepository(server_sessions),
        report_timezone=ZoneInfo("UTC"),
    ).get_report(10, ServerGameStatisticsPeriod.LAST_30_DAYS, AS_OF)

    assert member.top_games[0].canonical_key == server.top_games[0].canonical_key
    assert member.top_games[0].game_name == server.top_games[0].game_name == "game"
