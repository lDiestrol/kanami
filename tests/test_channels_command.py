from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.discord import (
    VoiceChannelLeaderboardCommandHandler,
    build_voice_channel_leaderboard_embed,
)
from discord_stats_bot.features.voice_statistics import (
    VoiceChannelLeaderboard,
    VoiceChannelUsageEntry,
    VoiceStatisticsPeriod,
)
from tests.support.discord import FakeChannel, FakeGuild, make_interaction
from tests.support.persistence import FakeSessionFactory

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def leaderboard(
    period: VoiceStatisticsPeriod = VoiceStatisticsPeriod.LAST_7_DAYS,
    entries: tuple[VoiceChannelUsageEntry, ...] | None = None,
) -> VoiceChannelLeaderboard:
    return VoiceChannelLeaderboard(
        AS_OF,
        period,
        entries
        if entries is not None
        else (
            VoiceChannelUsageEntry(1, 3600, 0),
            VoiceChannelUsageEntry(2, 3000, 60),
            VoiceChannelUsageEntry(3, 2400, 0),
            VoiceChannelUsageEntry(4, 1800, 0),
        ),
    )


def test_channel_embed_uses_medals_safe_cache_names_and_deleted_fallback() -> None:
    guild = FakeGuild(
        channels=(FakeChannel(1, "**Общий**"), FakeChannel(2, "Игровая_комната"))
    )

    embed = build_voice_channel_leaderboard_embed(  # type: ignore[arg-type]
        leaderboard(), guild, checkpoint_interval_seconds=60
    )

    assert embed.title == "Голосовые каналы — 7 дней"
    lines = embed.description.splitlines()
    assert lines[0].startswith("🥇 **1.**")
    assert lines[1].startswith("🥈 **2.**")
    assert lines[2].startswith("🥉 **3.**")
    assert lines[3].startswith("• **4.**")
    assert "\\*\\*Общий\\*\\*" in lines[0]
    assert "Игровая\\_комната" in lines[1]
    assert "Канал 3" in lines[2]
    assert "<#" not in embed.description
    assert "≈" in lines[1]
    assert "восстановленные участки" in embed.fields[0].value


def test_empty_channel_leaderboard_has_normal_presentation() -> None:
    embed = build_voice_channel_leaderboard_embed(  # type: ignore[arg-type]
        leaderboard(entries=()), FakeGuild(), checkpoint_interval_seconds=60
    )

    assert embed.description == (
        "За этот период голосовой активности в каналах пока нет."
    )
    assert embed.fields == []


class RecordingRepository:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, VoiceStatisticsPeriod, object]] = []

    async def get_channel_leaderboard(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: object,
    ) -> VoiceChannelLeaderboard:
        self.calls.append((guild_id, period, query))
        if self.error is not None:
            raise self.error
        return leaderboard(period)


def handler(
    repository: RecordingRepository,
) -> tuple[VoiceChannelLeaderboardCommandHandler, FakeSessionFactory]:
    session_factory = FakeSessionFactory()
    return (
        VoiceChannelLeaderboardCommandHandler(
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
async def test_channel_periods_default_and_success_are_public(
    value: str | None,
    expected: VoiceStatisticsPeriod,
) -> None:
    repository = RecordingRepository()
    command, session_factory = handler(repository)
    interaction = make_interaction(user_id=987)

    await command.handle(interaction, value)  # type: ignore[arg-type]

    assert repository.calls[0][0:2] == (10, expected)
    assert interaction.response.deferred == [{"ephemeral": False, "thinking": True}]
    _, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is False
    assert kwargs["allowed_mentions"].users is False
    assert session_factory.sessions[0].closed is True


@pytest.mark.parametrize(
    ("guild_id", "bot"),
    [(11, False), (None, False), (10, True)],
)
@pytest.mark.asyncio
async def test_channels_rejects_wrong_guild_dm_and_bot(
    guild_id: int | None, bot: bool
) -> None:
    repository = RecordingRepository()
    command, session_factory = handler(repository)
    interaction = make_interaction(guild_id=guild_id, bot=bot)

    await command.handle(interaction)  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_channels_rejects_invalid_period_before_query() -> None:
    repository = RecordingRepository()
    command, session_factory = handler(repository)
    interaction = make_interaction()

    await command.handle(interaction, "invalid")  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_channels_query_failure_is_public_and_friendly() -> None:
    repository = RecordingRepository(RuntimeError("offline"))
    command, session_factory = handler(repository)
    interaction = make_interaction(user_id=777)

    await command.handle(interaction, "30d")  # type: ignore[arg-type]

    args, kwargs = interaction.followup.messages[0]
    assert args == (
        "Не удалось получить статистику голосовых каналов. Попробуйте позже.",
    )
    assert kwargs["ephemeral"] is False
    assert session_factory.sessions[0].closed is True
