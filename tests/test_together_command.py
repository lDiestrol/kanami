from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.discord import (
    VoiceTogetherCommandHandler,
    build_voice_pair_statistics_embed,
    format_voice_percentage,
)
from discord_stats_bot.features.voice_statistics import (
    VoiceChannelUsageEntry,
    VoicePairStatistics,
)
from tests.support.discord import (
    FakeChannel,
    FakeGuild,
    FakeMember,
    make_interaction,
)
from tests.support.persistence import FakeSessionFactory

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def pair_report(
    *,
    channels: tuple[VoiceChannelUsageEntry, ...] = (
        VoiceChannelUsageEntry(101, 3600, 60),
        VoiceChannelUsageEntry(102, 1800, 0),
        VoiceChannelUsageEntry(103, 600, 0),
    ),
    exact_seconds: int = 5400,
    estimated_seconds: int = 60,
    user1_total_seconds: int = 10920,
    user2_total_seconds: int = 5460,
) -> VoicePairStatistics:
    return VoicePairStatistics(
        as_of=AS_OF,
        user1_id=1,
        user2_id=2,
        exact_seconds=exact_seconds,
        estimated_seconds=estimated_seconds,
        user1_total_seconds=user1_total_seconds,
        user2_total_seconds=user2_total_seconds,
        channels=channels,
    )


@pytest.mark.parametrize(
    ("pair_seconds", "total_seconds", "formatted"),
    [
        (0, 0, "0%"),
        (20, 100, "20%"),
        (2, 3, "66.7%"),
        (1, 200, "0.5%"),
    ],
)
def test_percentage_format_is_compact_and_zero_safe(
    pair_seconds: int,
    total_seconds: int,
    formatted: str,
) -> None:
    assert format_voice_percentage(pair_seconds, total_seconds) == formatted


def test_pair_embed_has_mentions_shares_channels_fallback_and_estimated_marker() -> (
    None
):
    guild = FakeGuild(
        members=(FakeMember(1), FakeMember(2)),
        channels=(FakeChannel(101, "**Общий**"), FakeChannel(102, "Игровой")),
    )

    embed = build_voice_pair_statistics_embed(  # type: ignore[arg-type]
        pair_report(), guild, checkpoint_interval_seconds=60
    )

    assert embed.title == "Вместе в голосовых"
    assert embed.description == "<@1> × <@2>"
    assert embed.fields[0].name == "Совместное время — всё время"
    assert embed.fields[0].value == "1 ч 31 мин ≈"
    assert embed.fields[1].value == "<@1> — 50%\n<@2> — 100%"
    assert "1. \\*\\*Общий\\*\\* — 1 ч 01 мин ≈" in embed.fields[2].value
    assert "2. Игровой — 30 мин" in embed.fields[2].value
    assert "3. Канал 103 — 10 мин" in embed.fields[2].value
    assert "раз в 1 мин" in embed.footer.text


def test_empty_pair_embed_and_missing_members_have_clear_fallbacks() -> None:
    embed = build_voice_pair_statistics_embed(  # type: ignore[arg-type]
        pair_report(
            channels=(),
            exact_seconds=0,
            estimated_seconds=0,
            user1_total_seconds=0,
            user2_total_seconds=0,
        ),
        FakeGuild(),
        checkpoint_interval_seconds=60,
    )

    assert embed.description == "Пользователь 1 × Пользователь 2"
    assert embed.fields[0].value == "0 сек"
    assert embed.fields[1].value == "Пользователь 1 — 0%\nПользователь 2 — 0%"
    assert embed.fields[2].value == "Совместной голосовой активности пока нет."


class RecordingRepository:
    def __init__(
        self,
        *,
        result: VoicePairStatistics | None = None,
        error: Exception | None = None,
    ) -> None:
        self.result = result or pair_report()
        self.error = error
        self.events: list[object] = []
        self.calls: list[tuple[int, int, int, object]] = []

    async def get_pair_statistics(
        self,
        guild_id: int,
        user1_id: int,
        user2_id: int,
        query: object,
    ) -> VoicePairStatistics:
        self.events.append("pair")
        self.calls.append((guild_id, user1_id, user2_id, query))
        if self.error is not None:
            raise self.error
        return self.result


def make_together_interaction(
    *, guild_id: int | None = 10, invoking_bot: bool = False
) -> object:
    guild = (
        FakeGuild(members=(FakeMember(1), FakeMember(2)))
        if guild_id is not None
        else None
    )
    return make_interaction(
        guild_id=guild_id,
        guild=guild,
        user=FakeMember(99, bot=invoking_bot),
    )


def make_handler(
    repository: RecordingRepository,
) -> tuple[VoiceTogetherCommandHandler, FakeSessionFactory]:
    session_factory = FakeSessionFactory(repository.events)
    return (
        VoiceTogetherCommandHandler(
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


@pytest.mark.asyncio
async def test_together_uses_one_repeatable_read_snapshot_and_private_response() -> (
    None
):
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_together_interaction()

    await handler.handle(  # type: ignore[arg-type]
        interaction,
        FakeMember(1),
        FakeMember(2),
    )

    assert repository.calls[0][:3] == (10, 1, 2)
    assert repository.events == [
        (
            "connection",
            {"execution_options": {"isolation_level": "REPEATABLE READ"}},
        ),
        "pair",
        "rollback",
        "close",
    ]
    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    kwargs = interaction.followup.messages[0][1]
    assert kwargs["ephemeral"] is True
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["allowed_mentions"].users is False
    assert kwargs["allowed_mentions"].roles is False
    assert kwargs["embed"].description == "<@1> × <@2>"
    assert session_factory.sessions[0].closed is True


@pytest.mark.parametrize(
    ("guild_id", "invoking_bot", "user1", "user2", "message"),
    [
        (11, False, FakeMember(1), FakeMember(2), "настроенного сервера"),
        (None, False, FakeMember(1), FakeMember(2), "настроенного сервера"),
        (10, True, FakeMember(1), FakeMember(2), "настроенного сервера"),
        (10, False, FakeMember(1, bot=True), FakeMember(2), "ботами"),
        (10, False, FakeMember(1), FakeMember(2, bot=True), "ботами"),
        (10, False, FakeMember(1), FakeMember(1), "разных участников"),
    ],
)
@pytest.mark.asyncio
async def test_together_validation_rejects_without_database(
    guild_id: int | None,
    invoking_bot: bool,
    user1: FakeMember,
    user2: FakeMember,
    message: str,
) -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_together_interaction(
        guild_id=guild_id, invoking_bot=invoking_bot
    )

    await handler.handle(interaction, user1, user2)  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    args, kwargs = interaction.response.messages[0]
    assert message in args[0]
    assert kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_together_query_failure_is_private_and_has_no_partial_embed() -> None:
    repository = RecordingRepository(error=RuntimeError("offline"))
    handler, _ = make_handler(repository)
    interaction = make_together_interaction()

    await handler.handle(  # type: ignore[arg-type]
        interaction,
        FakeMember(1),
        FakeMember(2),
    )

    args, kwargs = interaction.followup.messages[0]
    assert args == ("Не удалось получить совместную статистику. Попробуйте позже.",)
    assert kwargs == {"ephemeral": True}
