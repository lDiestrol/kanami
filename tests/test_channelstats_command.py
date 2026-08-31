from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

import discord_stats_bot.discord.voice_channel_stats as channelstats_module
from discord_stats_bot.discord import (
    VoiceChannelStatisticsCommandHandler,
    build_voice_channel_statistics_embed,
)
from discord_stats_bot.features.voice_statistics import (
    VoiceChannelStatistics,
    VoiceLeaderboardEntry,
    VoiceStatisticsPeriod,
)
from tests.support.discord import (
    make_interaction,
    make_member,
)
from tests.support.persistence import FakeSessionFactory

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


class FakeMember:
    def __init__(self, user_id: int, display_name: str) -> None:
        self.id = user_id
        self.display_name = display_name


class FakeGuild:
    def __init__(self, guild_id: int = 10) -> None:
        self.id = guild_id
        self._members = {
            1: FakeMember(1, "**Darkness**"),
            2: FakeMember(2, "Darki_name"),
        }

    def get_member(self, user_id: int) -> FakeMember | None:
        return self._members.get(user_id)


class FakeVoiceChannel:
    def __init__(self, channel_id: int, name: str, guild: FakeGuild) -> None:
        self.id = channel_id
        self.name = name
        self.guild = guild


class FakeStageChannel(FakeVoiceChannel):
    pass


class FakeTextChannel:
    def __init__(self, channel_id: int, name: str, guild: FakeGuild) -> None:
        self.id = channel_id
        self.name = name
        self.guild = guild


def report(
    *,
    period: VoiceStatisticsPeriod = VoiceStatisticsPeriod.LAST_7_DAYS,
    exact_seconds: int = 3600,
    estimated_seconds: int = 60,
    entries: tuple[VoiceLeaderboardEntry, ...] = (
        VoiceLeaderboardEntry(1, 1800, 60),
        VoiceLeaderboardEntry(2, 1200, 0),
        VoiceLeaderboardEntry(3, 600, 0),
        VoiceLeaderboardEntry(4, 60, 0),
    ),
) -> VoiceChannelStatistics:
    return VoiceChannelStatistics(
        AS_OF,
        period,
        100,
        exact_seconds,
        estimated_seconds,
        entries,
    )


def test_embed_has_total_top_names_medals_markers_and_no_mentions() -> None:
    embed = build_voice_channel_statistics_embed(  # type: ignore[arg-type]
        report(),
        FakeGuild(),
        "**Хрюкаем**",
        checkpoint_interval_seconds=60,
    )

    assert embed.title == "Статистика канала — \\*\\*Хрюкаем\\*\\*"
    assert embed.description == "Период: 7 дней"
    assert embed.fields[0].name == "Общее время"
    assert embed.fields[0].value == "1 ч 01 мин ≈"
    lines = embed.fields[1].value.splitlines()
    assert lines[0].startswith("🥇 **1.** \\*\\*Darkness\\*\\*")
    assert lines[1].startswith("🥈 **2.** Darki\\_name")
    assert lines[2].startswith("🥉 **3.** Пользователь 3")
    assert lines[3].startswith("• **4.** Пользователь 4")
    assert "<@" not in embed.fields[1].value
    assert "восстановленные участки" in embed.fields[2].value
    assert "1 мин" in embed.footer.text


def test_empty_activity_does_not_render_empty_top() -> None:
    embed = build_voice_channel_statistics_embed(  # type: ignore[arg-type]
        report(exact_seconds=0, estimated_seconds=0, entries=()),
        FakeGuild(),
        "Пустой",
        checkpoint_interval_seconds=60,
    )

    assert embed.fields == []
    assert "пока нет" in embed.description


class RecordingRepository:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, int, VoiceStatisticsPeriod, object]] = []

    async def get_channel_statistics(
        self,
        guild_id: int,
        channel_id: int,
        period: VoiceStatisticsPeriod,
        query: object,
    ) -> VoiceChannelStatistics:
        self.calls.append((guild_id, channel_id, period, query))
        if self.error is not None:
            raise self.error
        return report(period=period)


def make_channelstats_interaction(
    *, guild_id: int | None = 10, user_id: int = 20, bot: bool = False
) -> object:
    guild = FakeGuild(guild_id) if guild_id is not None else None
    return make_interaction(
        guild_id=guild_id,
        guild=guild,
        user=make_member(user_id, bot=bot),
    )


def handler(
    repository: RecordingRepository,
) -> tuple[VoiceChannelStatisticsCommandHandler, FakeSessionFactory]:
    session_factory = FakeSessionFactory()
    return (
        VoiceChannelStatisticsCommandHandler(
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


@pytest.fixture(autouse=True)
def fake_discord_channel_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(channelstats_module.discord, "VoiceChannel", FakeVoiceChannel)
    monkeypatch.setattr(channelstats_module.discord, "StageChannel", FakeStageChannel)


@pytest.mark.parametrize("channel_type", [FakeVoiceChannel, FakeStageChannel])
@pytest.mark.asyncio
async def test_voice_and_stage_success_are_public_with_default_period(
    channel_type: type[FakeVoiceChannel],
) -> None:
    repository = RecordingRepository()
    command, session_factory = handler(repository)
    interaction = make_channelstats_interaction()
    channel = channel_type(100, "Актуальное имя", interaction.guild)

    await command.handle(interaction, channel)  # type: ignore[arg-type]

    assert repository.calls[0][0:3] == (
        10,
        100,
        VoiceStatisticsPeriod.LAST_7_DAYS,
    )
    assert interaction.response.deferred == [{"ephemeral": False, "thinking": True}]
    _, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is False
    assert kwargs["allowed_mentions"].users is False
    assert kwargs["embed"].title.endswith("Актуальное имя")
    assert session_factory.sessions[0].closed is True


@pytest.mark.parametrize(
    ("guild_id", "bot"),
    [(11, False), (None, False), (10, True)],
)
@pytest.mark.asyncio
async def test_wrong_guild_dm_and_bot_are_rejected_before_query(
    guild_id: int | None,
    bot: bool,
) -> None:
    repository = RecordingRepository()
    command, session_factory = handler(repository)
    interaction = make_channelstats_interaction(guild_id=guild_id, bot=bot)
    channel = FakeVoiceChannel(100, "Voice", FakeGuild(10))

    await command.handle(interaction, channel)  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.parametrize(
    "channel",
    [
        FakeTextChannel(100, "Text", FakeGuild()),
        FakeVoiceChannel(100, "Voice", FakeGuild(11)),
    ],
)
@pytest.mark.asyncio
async def test_invalid_channel_is_rejected_before_query(channel: object) -> None:
    repository = RecordingRepository()
    command, session_factory = handler(repository)
    interaction = make_channelstats_interaction()

    await command.handle(interaction, channel)  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_invalid_period_is_rejected_before_query() -> None:
    repository = RecordingRepository()
    command, session_factory = handler(repository)
    interaction = make_channelstats_interaction()
    channel = FakeVoiceChannel(100, "Voice", interaction.guild)

    await command.handle(interaction, channel, "invalid")  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_database_failure_is_public_and_friendly() -> None:
    repository = RecordingRepository(RuntimeError("offline"))
    command, session_factory = handler(repository)
    interaction = make_channelstats_interaction(user_id=777)
    channel = FakeVoiceChannel(100, "Voice", interaction.guild)

    await command.handle(interaction, channel, "30d")  # type: ignore[arg-type]

    args, kwargs = interaction.followup.messages[0]
    assert args == (
        "Не удалось получить статистику голосового канала. Попробуйте позже.",
    )
    assert kwargs["ephemeral"] is False
    assert session_factory.sessions[0].closed is True
