from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.discord import (
    VoiceLeaderboardCommandHandler,
    build_voice_leaderboard_embed,
)
from discord_stats_bot.features.voice_statistics import (
    VoiceLeaderboard,
    VoiceLeaderboardEntry,
    VoiceStatisticsPeriod,
)
from tests.support.discord import (
    FakeGuild,
    FakeMember,
    make_interaction,
)
from tests.support.persistence import FakeSessionFactory

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def leaderboard(
    period: VoiceStatisticsPeriod = VoiceStatisticsPeriod.LAST_7_DAYS,
    *,
    entries: tuple[VoiceLeaderboardEntry, ...] | None = None,
) -> VoiceLeaderboard:
    return VoiceLeaderboard(
        AS_OF,
        period,
        entries
        if entries is not None
        else (
            VoiceLeaderboardEntry(1, 3600, 0),
            VoiceLeaderboardEntry(2, 3000, 60),
            VoiceLeaderboardEntry(3, 2400, 0),
            VoiceLeaderboardEntry(4, 1800, 0),
        ),
    )


def test_embed_uses_rank_order_medals_member_mentions_and_cache_fallback() -> None:
    guild = FakeGuild(
        members=(
            FakeMember(1, display_name="**Darkness**"),
            FakeMember(2, display_name="User_2"),
            FakeMember(3, display_name="User3"),
        )
    )

    embed = build_voice_leaderboard_embed(  # type: ignore[arg-type]
        leaderboard(), guild, checkpoint_interval_seconds=60
    )

    assert embed.title == "Голосовой рейтинг — 7 дней"
    lines = embed.description.splitlines()
    assert lines[0].startswith("🥇 **1.**")
    assert lines[1].startswith("🥈 **2.**")
    assert lines[2].startswith("🥉 **3.**")
    assert lines[3].startswith("**4.**")
    assert "<@1>" in lines[0]
    assert "<@2>" in lines[1]
    assert "<@3>" in lines[2]
    assert "Пользователь 4" in lines[3]
    assert "<@4>" not in embed.description
    assert "≈" not in lines[0]
    assert "≈" in lines[1]
    assert "восстановленные участки" in embed.fields[0].value


def test_empty_leaderboard_has_normal_public_presentation() -> None:
    embed = build_voice_leaderboard_embed(  # type: ignore[arg-type]
        leaderboard(entries=()), FakeGuild(), checkpoint_interval_seconds=60
    )

    assert embed.description == "За этот период голосовой активности пока нет."
    assert embed.fields == []


class RecordingRepository:
    def __init__(
        self,
        *,
        error: Exception | None = None,
    ) -> None:
        self.error = error
        self.calls: list[tuple[int, VoiceStatisticsPeriod, object]] = []

    async def get_leaderboard(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: object,
    ) -> VoiceLeaderboard:
        self.calls.append((guild_id, period, query))
        if self.error is not None:
            raise self.error
        return leaderboard(period)

    async def get_user_statistics(self, *args: object) -> object:
        raise AssertionError("leaderboard must not query invoking user's stats")


def make_handler(
    repository: RecordingRepository,
) -> tuple[VoiceLeaderboardCommandHandler, FakeSessionFactory]:
    session_factory = FakeSessionFactory()
    return (
        VoiceLeaderboardCommandHandler(
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
async def test_period_choices_default_and_success_are_public(
    value: str | None,
    expected: VoiceStatisticsPeriod,
) -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_interaction(user_id=12345)

    await handler.handle(interaction, value)  # type: ignore[arg-type]

    assert repository.calls[0][0:2] == (10, expected)
    assert all(call[0] != 12345 for call in repository.calls)
    assert interaction.response.deferred == [{"ephemeral": False, "thinking": True}]
    _, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is False
    assert kwargs["allowed_mentions"].everyone is False
    assert kwargs["allowed_mentions"].users is False
    assert kwargs["allowed_mentions"].roles is False
    assert session_factory.sessions[0].closed is True


@pytest.mark.parametrize(
    ("guild_id", "bot"),
    [(11, False), (None, False), (10, True)],
)
@pytest.mark.asyncio
async def test_leaderboard_rejects_other_guild_dm_and_bot(
    guild_id: int | None,
    bot: bool,
) -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_interaction(guild_id=guild_id, bot=bot)

    await handler.handle(interaction)  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_invalid_period_is_rejected_before_query() -> None:
    repository = RecordingRepository()
    handler, _ = make_handler(repository)
    interaction = make_interaction()

    await handler.handle(interaction, "invalid")  # type: ignore[arg-type]

    assert repository.calls == []
    assert interaction.response.messages[0][1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_query_failure_is_isolated_with_public_error() -> None:
    repository = RecordingRepository(error=RuntimeError("offline failure"))
    handler, session_factory = make_handler(repository)
    interaction = make_interaction(user_id=777)

    await handler.handle(interaction, "30d")  # type: ignore[arg-type]

    args, kwargs = interaction.followup.messages[0]
    assert args == ("Не удалось получить голосовой рейтинг. Попробуйте позже.",)
    assert kwargs["ephemeral"] is False
    assert repository.calls[0][1] is VoiceStatisticsPeriod.LAST_30_DAYS
    assert session_factory.sessions[0].closed is True
