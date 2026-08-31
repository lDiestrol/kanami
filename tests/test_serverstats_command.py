from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.discord import (
    VoiceServerStatisticsCommandHandler,
    build_voice_server_statistics_embed,
)
from discord_stats_bot.features.voice_statistics import (
    VoiceChannelUsageEntry,
    VoiceLeaderboardEntry,
    VoiceServerStatistics,
    VoiceStatisticsPeriod,
)
from tests.support.discord import (
    FakeChannel,
    FakeGuild,
    FakeMember,
    make_interaction,
)
from tests.support.persistence import FakeSessionFactory

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def server_report(
    period: VoiceStatisticsPeriod = VoiceStatisticsPeriod.LAST_7_DAYS,
    *,
    exact_seconds: int = 10_200,
    estimated_seconds: int = 120,
    active_users: int = 2,
    top_user: VoiceLeaderboardEntry | None = VoiceLeaderboardEntry(1, 5400, 60),
    top_channel: VoiceChannelUsageEntry | None = VoiceChannelUsageEntry(101, 7200, 60),
) -> VoiceServerStatistics:
    return VoiceServerStatistics(
        as_of=AS_OF,
        period=period,
        exact_seconds=exact_seconds,
        estimated_seconds=estimated_seconds,
        active_users=active_users,
        top_user=top_user,
        top_channel=top_channel,
    )


@pytest.mark.parametrize(
    ("period", "label"),
    [
        (VoiceStatisticsPeriod.TODAY, "Сегодня"),
        (VoiceStatisticsPeriod.LAST_7_DAYS, "7 дней"),
        (VoiceStatisticsPeriod.LAST_30_DAYS, "30 дней"),
        (VoiceStatisticsPeriod.ALL_TIME, "Всё время"),
    ],
)
def test_server_embed_titles_match_existing_period_labels(
    period: VoiceStatisticsPeriod,
    label: str,
) -> None:
    embed = build_voice_server_statistics_embed(  # type: ignore[arg-type]
        server_report(period), FakeGuild(), checkpoint_interval_seconds=60
    )

    assert embed.title == f"Статистика сервера — {label}"


def test_server_embed_formats_total_average_top_entries_and_fallbacks() -> None:
    guild = FakeGuild(
        members=(FakeMember(1),),
        channels=(FakeChannel(101, "**Общий**"),),
    )

    embed = build_voice_server_statistics_embed(  # type: ignore[arg-type]
        server_report(), guild, checkpoint_interval_seconds=60
    )

    assert [field.name for field in embed.fields] == [
        "Суммарное время участников",
        "Активных участников",
        "В среднем на активного",
        "Самый активный участник",
        "Самый популярный канал",
    ]
    assert embed.fields[0].value == "2 ч 52 мин ≈"
    assert embed.fields[1].value == "2"
    assert embed.fields[2].value == "1 ч 26 мин"
    assert embed.fields[3].value == "<@1> — 1 ч 31 мин ≈"
    assert embed.fields[4].value == "\\*\\*Общий\\*\\* — 2 ч 01 мин ≈"
    assert "раз в 1 мин" in embed.footer.text

    fallback_embed = build_voice_server_statistics_embed(  # type: ignore[arg-type]
        server_report(), FakeGuild(), checkpoint_interval_seconds=60
    )
    assert fallback_embed.fields[3].value.startswith("Пользователь 1 —")
    assert fallback_embed.fields[4].value.startswith("Канал 101 —")


def test_empty_server_report_has_zero_average_and_clear_top_fallbacks() -> None:
    report = server_report(
        exact_seconds=0,
        estimated_seconds=0,
        active_users=0,
        top_user=None,
        top_channel=None,
    )

    embed = build_voice_server_statistics_embed(  # type: ignore[arg-type]
        report, FakeGuild(), checkpoint_interval_seconds=60
    )

    assert report.average_seconds == 0
    assert embed.fields[0].value == "0 сек"
    assert embed.fields[1].value == "0"
    assert embed.fields[2].value == "0 сек"
    assert embed.fields[3].value == "Нет данных за выбранный период."
    assert embed.fields[4].value == "Нет данных за выбранный период."


class RecordingRepository:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.events: list[object] = []
        self.calls: list[tuple[int, VoiceStatisticsPeriod, object]] = []

    async def get_server_statistics(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: object,
    ) -> VoiceServerStatistics:
        self.events.append("server")
        self.calls.append((guild_id, period, query))
        if self.error is not None:
            raise self.error
        return server_report(period)


def make_server_interaction(*, guild_id: int | None = 10, bot: bool = False) -> object:
    guild = (
        FakeGuild(members=(FakeMember(1),), channels=(FakeChannel(101, "Общий"),))
        if guild_id is not None
        else None
    )
    return make_interaction(
        guild_id=guild_id,
        guild=guild,
        user=FakeMember(99, bot=bot),
    )


def make_handler(
    repository: RecordingRepository,
) -> tuple[VoiceServerStatisticsCommandHandler, FakeSessionFactory]:
    session_factory = FakeSessionFactory(repository.events)
    return (
        VoiceServerStatisticsCommandHandler(
            session_factory,  # type: ignore[arg-type]
            guild_id=10,
            report_timezone=ZoneInfo("UTC"),
            min_session_seconds=10,
            checkpoint_interval_seconds=60,
            repository_factory=lambda session: repository,
            clock=lambda: AS_OF,
        ),
        session_factory,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, VoiceStatisticsPeriod.LAST_7_DAYS),
        ("today", VoiceStatisticsPeriod.TODAY),
        ("7d", VoiceStatisticsPeriod.LAST_7_DAYS),
        ("30d", VoiceStatisticsPeriod.LAST_30_DAYS),
        ("all", VoiceStatisticsPeriod.ALL_TIME),
    ],
)
@pytest.mark.asyncio
async def test_serverstats_periods_snapshot_and_private_safe_response(
    value: str | None,
    expected: VoiceStatisticsPeriod,
) -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_server_interaction()

    await handler.handle(interaction, value)  # type: ignore[arg-type]

    assert repository.calls[0][:2] == (10, expected)
    assert repository.events == [
        (
            "connection",
            {"execution_options": {"isolation_level": "REPEATABLE READ"}},
        ),
        "server",
        "rollback",
        "close",
    ]
    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    kwargs = interaction.followup.messages[0][1]
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["allowed_mentions"].users is False
    assert kwargs["allowed_mentions"].roles is False
    assert session_factory.sessions[0].closed is True


@pytest.mark.parametrize(("guild_id", "bot"), [(11, False), (None, False), (10, True)])
@pytest.mark.asyncio
async def test_serverstats_validation_rejects_without_database(
    guild_id: int | None,
    bot: bool,
) -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_server_interaction(guild_id=guild_id, bot=bot)

    await handler.handle(interaction)  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_serverstats_invalid_period_rejects_without_database() -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_server_interaction()

    await handler.handle(interaction, "invalid")  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_serverstats_query_failure_is_private_without_partial_embed() -> None:
    repository = RecordingRepository(error=RuntimeError("offline"))
    handler, _ = make_handler(repository)
    interaction = make_server_interaction()

    await handler.handle(interaction, "30d")  # type: ignore[arg-type]

    args, kwargs = interaction.followup.messages[0]
    assert args == ("Не удалось получить статистику сервера. Попробуйте позже.",)
    assert kwargs == {"ephemeral": True}
