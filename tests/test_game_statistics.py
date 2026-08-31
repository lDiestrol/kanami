from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.discord.game_statistics import GameStatisticsCommandHandler
from discord_stats_bot.features.game_tracking import (
    GameSessionSlice,
    GameStatisticsPeriod,
    GameStatisticsService,
)
from tests.support.discord import FakeGuild, FakeMember, make_interaction
from tests.support.persistence import FakeSessionFactory

AS_OF = datetime(2026, 8, 27, 12, tzinfo=UTC)


def game_session(
    session_id: int,
    name: str,
    start: datetime,
    end: datetime,
    *,
    key: str | None = None,
    open_session: bool = False,
) -> GameSessionSlice:
    return GameSessionSlice(
        session_id,
        key or f"name:{name.casefold()}",
        name,
        start,
        end,
        None if open_session else end,
    )


class RecordingRepository:
    def __init__(
        self,
        sessions: tuple[GameSessionSlice, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.sessions = sessions
        self.error = error
        self.calls: list[tuple[int, int, datetime | None, datetime]] = []

    async def list_user_sessions(
        self,
        guild_id: int,
        user_id: int,
        *,
        started_after: datetime | None,
        ended_before: datetime,
    ) -> tuple[GameSessionSlice, ...]:
        self.calls.append((guild_id, user_id, started_after, ended_before))
        if self.error is not None:
            raise self.error
        return self.sessions


@pytest.mark.parametrize(
    ("period", "days"),
    [
        (GameStatisticsPeriod.LAST_7_DAYS, 7),
        (GameStatisticsPeriod.LAST_30_DAYS, 30),
        (GameStatisticsPeriod.LAST_90_DAYS, 90),
        (GameStatisticsPeriod.ALL_TIME, None),
    ],
)
@pytest.mark.asyncio
async def test_statistics_periods_use_canonical_utc_rolling_window(
    period: GameStatisticsPeriod,
    days: int | None,
) -> None:
    repository = RecordingRepository()

    report = await GameStatisticsService(
        repository, report_timezone=ZoneInfo("Asia/Yekaterinburg")
    ).get_user_statistics(10, 20, period, AS_OF)

    assert report.period is period
    assert repository.calls == [
        (10, 20, None if days is None else AS_OF - timedelta(days=days), AS_OF)
    ]


@pytest.mark.asyncio
async def test_statistics_aggregate_confirmed_time_games_days_top_latest_and_longest() -> (
    None
):
    sessions = (
        game_session(
            1,
            "Strinova",
            datetime(2026, 8, 24, 8, tzinfo=UTC),
            datetime(2026, 8, 24, 12, tzinfo=UTC),
            key="application:1",
        ),
        game_session(
            2,
            "Minecraft",
            datetime(2026, 8, 25, 20, tzinfo=UTC),
            datetime(2026, 8, 26, 2, tzinfo=UTC),
            key="application:2",
        ),
        game_session(
            3,
            "Strinova",
            datetime(2026, 8, 26, 10, tzinfo=UTC),
            datetime(2026, 8, 26, 15, tzinfo=UTC),
            key="application:1",
        ),
        game_session(
            4,
            "Strinova",
            datetime(2026, 8, 27, 8, tzinfo=UTC),
            datetime(2026, 8, 27, 10, tzinfo=UTC),
            key="application:1",
            open_session=True,
        ),
    )

    report = await GameStatisticsService(
        RecordingRepository(sessions), report_timezone=ZoneInfo("UTC")
    ).get_user_statistics(10, 20, GameStatisticsPeriod.LAST_30_DAYS, AS_OF)

    assert report.total_seconds == 17 * 3600
    assert report.unique_games == 2
    assert report.gaming_days == 4
    assert [(item.game_name, item.total_seconds) for item in report.top_games] == [
        ("Strinova", 11 * 3600),
        ("Minecraft", 6 * 3600),
    ]
    assert report.latest_game is not None
    assert report.latest_game.game_name == "Strinova"
    assert report.latest_game.tracked_at == datetime(2026, 8, 27, 10, tzinfo=UTC)
    assert report.longest_session is not None
    assert report.longest_session.game_name == "Minecraft"
    assert report.longest_session.total_seconds == 6 * 3600


@pytest.mark.asyncio
async def test_statistics_merge_same_name_across_application_game_keys() -> None:
    sessions = (
        game_session(
            1,
            "Fortnite",
            AS_OF - timedelta(hours=4),
            AS_OF - timedelta(hours=2),
            key="application:1402418703554842694",
        ),
        game_session(
            2,
            "Fortnite",
            AS_OF - timedelta(hours=1),
            AS_OF,
            key="application:432980957394370572",
        ),
    )

    report = await GameStatisticsService(
        RecordingRepository(sessions), report_timezone=ZoneInfo("UTC")
    ).get_user_statistics(10, 20, GameStatisticsPeriod.ALL_TIME, AS_OF)

    assert report.total_seconds == 3 * 3600
    assert report.unique_games == 1
    assert [
        (entry.canonical_key, entry.game_name, entry.total_seconds)
        for entry in report.top_games
    ] == [("fortnite", "Fortnite", 3 * 3600)]


@pytest.mark.asyncio
async def test_statistics_merge_case_and_outer_whitespace_name_variants() -> None:
    sessions = tuple(
        game_session(
            index,
            name,
            AS_OF - timedelta(hours=4 - index),
            AS_OF - timedelta(hours=3 - index),
            key=f"application:{index}",
        )
        for index, name in enumerate(("Strinova", " strinova ", "STRINOVA"), start=1)
    )

    report = await GameStatisticsService(
        RecordingRepository(sessions), report_timezone=ZoneInfo("UTC")
    ).get_user_statistics(10, 20, GameStatisticsPeriod.ALL_TIME, AS_OF)

    assert report.unique_games == 1
    assert report.top_games[0].canonical_key == "strinova"
    assert report.top_games[0].total_seconds == 3 * 3600


@pytest.mark.asyncio
async def test_statistics_merge_exact_case_insensitive_medal_suffix() -> None:
    sessions = (
        game_session(
            1,
            "Strinova",
            AS_OF - timedelta(hours=3),
            AS_OF - timedelta(hours=2),
        ),
        game_session(
            2,
            "Strinova WITH MEDAL",
            AS_OF - timedelta(hours=2),
            AS_OF,
            key="application:307998818547531777",
        ),
    )

    report = await GameStatisticsService(
        RecordingRepository(sessions), report_timezone=ZoneInfo("UTC")
    ).get_user_statistics(10, 20, GameStatisticsPeriod.ALL_TIME, AS_OF)

    assert report.unique_games == 1
    assert report.top_games[0].canonical_key == "strinova"
    assert report.top_games[0].game_name == "Strinova"
    assert report.top_games[0].total_seconds == 3 * 3600


@pytest.mark.asyncio
async def test_statistics_do_not_merge_different_medal_games_with_same_raw_key() -> (
    None
):
    medal_application_key = "application:307998818547531777"
    sessions = (
        game_session(
            1,
            "Minecraft with Medal",
            AS_OF - timedelta(hours=2),
            AS_OF - timedelta(hours=1),
            key=medal_application_key,
        ),
        game_session(
            2,
            "Strinova with Medal",
            AS_OF - timedelta(hours=1),
            AS_OF,
            key=medal_application_key,
        ),
    )

    report = await GameStatisticsService(
        RecordingRepository(sessions), report_timezone=ZoneInfo("UTC")
    ).get_user_statistics(10, 20, GameStatisticsPeriod.ALL_TIME, AS_OF)

    assert report.unique_games == 2
    assert {entry.canonical_key for entry in report.top_games} == {
        "minecraft",
        "strinova",
    }
    assert {entry.game_name for entry in report.top_games} == {
        "Minecraft",
        "Strinova",
    }


@pytest.mark.parametrize("game_name", ["VALORANT", "R.E.P.O.", "osu!"])
@pytest.mark.asyncio
async def test_statistics_preserve_readable_display_name(game_name: str) -> None:
    session = game_session(
        1,
        game_name,
        AS_OF - timedelta(hours=1),
        AS_OF,
        key="application:1",
    )

    report = await GameStatisticsService(
        RecordingRepository((session,)), report_timezone=ZoneInfo("UTC")
    ).get_user_statistics(10, 20, GameStatisticsPeriod.ALL_TIME, AS_OF)

    assert report.top_games[0].game_name == game_name


@pytest.mark.asyncio
async def test_latest_and_longest_use_preferred_canonical_display_name() -> None:
    sessions = (
        game_session(
            1,
            "Strinova",
            AS_OF - timedelta(hours=5),
            AS_OF - timedelta(hours=4),
        ),
        game_session(
            2,
            "Strinova with Medal",
            AS_OF - timedelta(hours=3),
            AS_OF,
            key="application:307998818547531777",
        ),
    )

    report = await GameStatisticsService(
        RecordingRepository(sessions), report_timezone=ZoneInfo("UTC")
    ).get_user_statistics(10, 20, GameStatisticsPeriod.ALL_TIME, AS_OF)

    assert report.latest_game is not None
    assert report.latest_game.game_name == "Strinova"
    assert report.longest_session is not None
    assert report.longest_session.game_name == "Strinova"
    assert report.longest_session.total_seconds == 3 * 3600


@pytest.mark.asyncio
async def test_statistics_clip_session_to_period_and_limit_top_to_five() -> None:
    sessions = tuple(
        game_session(
            index,
            f"Game {index}",
            AS_OF - timedelta(days=7, hours=index),
            AS_OF - timedelta(days=7) + timedelta(hours=8 - index),
        )
        for index in range(1, 7)
    )

    report = await GameStatisticsService(
        RecordingRepository(sessions), report_timezone=ZoneInfo("UTC")
    ).get_user_statistics(10, 20, GameStatisticsPeriod.LAST_7_DAYS, AS_OF)

    assert report.total_seconds == sum((8 - index) * 3600 for index in range(1, 7))
    assert report.unique_games == 6
    assert [item.game_name for item in report.top_games] == [
        "Game 1",
        "Game 2",
        "Game 3",
        "Game 4",
        "Game 5",
    ]


@pytest.mark.asyncio
async def test_statistics_no_data_is_explicit() -> None:
    report = await GameStatisticsService(
        RecordingRepository(), report_timezone=ZoneInfo("UTC")
    ).get_user_statistics(10, 20, GameStatisticsPeriod.ALL_TIME, AS_OF)

    assert report.has_data is False
    assert report.total_seconds == 0
    assert report.unique_games == 0
    assert report.gaming_days == 0
    assert report.top_games == ()
    assert report.latest_game is None
    assert report.longest_session is None


@pytest.mark.parametrize(
    ("ended_at", "expected_gaming_days"),
    [
        (datetime(2026, 8, 27, 19, 30, tzinfo=UTC), 2),
        (datetime(2026, 8, 27, 19, 0, tzinfo=UTC), 1),
    ],
)
@pytest.mark.asyncio
async def test_gaming_days_use_report_timezone_and_exclude_midnight_end_boundary(
    ended_at: datetime,
    expected_gaming_days: int,
) -> None:
    session = game_session(
        1,
        "Minecraft",
        datetime(2026, 8, 27, 18, 30, tzinfo=UTC),
        ended_at,
    )

    report = await GameStatisticsService(
        RecordingRepository((session,)),
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
    ).get_user_statistics(
        10,
        20,
        GameStatisticsPeriod.LAST_7_DAYS,
        datetime(2026, 8, 28, 12, tzinfo=UTC),
    )

    assert report.gaming_days == expected_gaming_days


def make_handler(
    repository: RecordingRepository,
    *,
    tracking_enabled: bool = True,
) -> tuple[GameStatisticsCommandHandler, FakeSessionFactory]:
    session_factory = FakeSessionFactory()
    return (
        GameStatisticsCommandHandler(
            session_factory,  # type: ignore[arg-type]
            guild_id=10,
            tracking_enabled=tracking_enabled,
            report_timezone=ZoneInfo("UTC"),
            checkpoint_interval_seconds=60,
            repository_factory=lambda session: repository,  # type: ignore[arg-type]
            clock=lambda: AS_OF,
        ),
        session_factory,
    )


@pytest.mark.asyncio
async def test_games_command_defaults_to_invoker_and_30d_private_embed() -> None:
    repository = RecordingRepository(
        (
            game_session(
                1,
                "Minecraft",
                AS_OF - timedelta(hours=2),
                AS_OF - timedelta(hours=1),
            ),
        )
    )
    handler, session_factory = make_handler(repository)
    interaction = make_interaction(
        user=FakeMember(20, display_name="Player", avatar_url="https://avatar")
    )

    await handler.handle(interaction)  # type: ignore[arg-type]

    assert repository.calls == [(10, 20, AS_OF - timedelta(days=30), AS_OF)]
    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    kwargs = interaction.followup.messages[0][1]
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].users is False
    embed = kwargs["embed"]
    assert embed.title == "🎮 Игровая активность — Player"
    assert embed.description == "За 30 дней"
    assert [field.name for field in embed.fields] == [
        "Общее время",
        "Игр",
        "Игровых дней",
        "Топ игр",
        "Последняя игра",
        "Самая длинная сессия",
    ]
    assert "Minecraft — сегодня" == embed.fields[4].value
    assert session_factory.sessions[0].closed is True


@pytest.mark.asyncio
async def test_games_command_selected_member_and_period_are_forwarded() -> None:
    repository = RecordingRepository()
    handler, _ = make_handler(repository)
    target = FakeMember(30)
    interaction = make_interaction(guild=FakeGuild(members=(target,)))

    await handler.handle(interaction, target, "90d")  # type: ignore[arg-type]

    assert repository.calls == [(10, 30, AS_OF - timedelta(days=90), AS_OF)]
    embed = interaction.followup.messages[0][1]["embed"]
    assert embed.fields[0].name == "Нет данных"


@pytest.mark.asyncio
async def test_games_command_opt_in_disabled_does_not_open_database() -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository, tracking_enabled=False)
    interaction = make_interaction()

    await handler.handle(interaction)  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    args, kwargs = interaction.response.messages[0]
    assert "не включено" in args[0]
    assert kwargs == {"ephemeral": True}


@pytest.mark.parametrize(
    ("guild_id", "invoker_bot", "target_bot"),
    [(11, False, False), (None, False, False), (10, True, False), (10, False, True)],
)
@pytest.mark.asyncio
async def test_games_command_rejects_invalid_guild_and_bots_without_database(
    guild_id: int | None,
    invoker_bot: bool,
    target_bot: bool,
) -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_interaction(guild_id=guild_id, bot=invoker_bot)
    target = FakeMember(30, bot=True) if target_bot else None

    await handler.handle(interaction, target)  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_games_command_invalid_period_and_query_failure_are_safe() -> None:
    repository = RecordingRepository(error=RuntimeError("offline"))
    handler, session_factory = make_handler(repository)

    invalid = make_interaction()
    await handler.handle(invalid, period_value="today")  # type: ignore[arg-type]
    assert invalid.response.messages[0][1]["ephemeral"] is True
    assert session_factory.sessions == []

    failed = make_interaction()
    await handler.handle(failed, period_value="7d")  # type: ignore[arg-type]
    args, kwargs = failed.followup.messages[0]
    assert args == ("Не удалось получить игровую статистику. Попробуйте позже.",)
    assert kwargs == {"ephemeral": True}
