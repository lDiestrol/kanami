from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.discord import (
    DiscordStatsClient,
    TextLeaderboardCommandHandler,
    build_text_leaderboard_embed,
)
from discord_stats_bot.features.text_activity import (
    TextActivityLeaderboard,
    TextActivityPeriod,
    TextUserMessageCount,
)
from tests.support.discord import (
    FakeGuild,
    make_guild,
    make_interaction,
    make_member,
)
from tests.support.persistence import FakeSessionFactory

AS_OF = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def text_guild(member_ids: tuple[int, ...] = ()) -> FakeGuild:
    return make_guild(members=tuple(make_member(user_id) for user_id in member_ids))


def leaderboard(
    *,
    entries: tuple[TextUserMessageCount, ...] | None = None,
) -> TextActivityLeaderboard:
    return TextActivityLeaderboard(
        AS_OF,
        TextActivityPeriod.LAST_7_DAYS,
        entries
        if entries is not None
        else (
            TextUserMessageCount(1, 20),
            TextUserMessageCount(2, 15),
            TextUserMessageCount(99, 7),
        ),
    )


def test_embed_uses_ranking_mentions_and_missing_member_fallback() -> None:
    embed = build_text_leaderboard_embed(  # type: ignore[arg-type]
        leaderboard(),
        text_guild((1, 2)),
    )

    assert embed.title == "Текстовый рейтинг — 7 дней"
    lines = embed.description.splitlines()
    assert lines[0].startswith("🥇 **1.** <@1> — **20**")
    assert lines[1].startswith("🥈 **2.** <@2> — **15**")
    assert lines[2].startswith("🥉 **3.** Пользователь 99 — **7**")
    assert "<@99>" not in embed.description


def test_empty_leaderboard_has_normal_public_presentation() -> None:
    embed = build_text_leaderboard_embed(  # type: ignore[arg-type]
        leaderboard(entries=()),
        text_guild(),
    )

    assert embed.description == "За этот период сообщений пока нет."
    assert embed.fields == []


class RecordingRepository:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[int, date | None, date, int | None]] = []

    async def get_user_message_counts(
        self,
        guild_id: int,
        started_on: date | None,
        ended_on: date,
        *,
        user_ids: tuple[int, ...] | None = None,
        limit: int | None = None,
    ) -> tuple[TextUserMessageCount, ...]:
        del user_ids
        self.calls.append((guild_id, started_on, ended_on, limit))
        if self.error is not None:
            raise self.error
        return leaderboard().entries


def make_handler(
    repository: RecordingRepository,
) -> tuple[TextLeaderboardCommandHandler, FakeSessionFactory]:
    sessions = FakeSessionFactory()
    handler = TextLeaderboardCommandHandler(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        repository_factory=lambda session: repository,  # type: ignore[arg-type]
        clock=lambda: AS_OF,
    )
    return handler, sessions


@pytest.mark.asyncio
async def test_handler_queries_top_10_and_sends_public_safe_mentions() -> None:
    repository = RecordingRepository()
    handler, sessions = make_handler(repository)
    interaction = make_interaction(guild=text_guild((1, 2)), user_id=50)

    await handler.handle(interaction, "7d")  # type: ignore[arg-type]

    assert repository.calls == [(10, date(2026, 8, 11), date(2026, 8, 17), 10)]
    assert interaction.response.deferred == [{"ephemeral": False, "thinking": True}]
    _, kwargs = interaction.followup.messages[0]
    assert kwargs["ephemeral"] is False
    assert kwargs["allowed_mentions"].users is False
    assert sessions.sessions[0].closed is True


@pytest.mark.asyncio
async def test_handler_isolates_query_failure() -> None:
    repository = RecordingRepository(error=RuntimeError("offline"))
    handler, _ = make_handler(repository)
    interaction = make_interaction(guild=text_guild((1, 2)), user_id=50)

    await handler.handle(interaction, "all")  # type: ignore[arg-type]

    args, kwargs = interaction.followup.messages[0]
    assert args == ("Не удалось получить текстовый рейтинг. Попробуйте позже.",)
    assert kwargs["ephemeral"] is False


class NoOpLeaderboardHandler:
    async def handle(self, interaction: object, period: str | None = None) -> None:
        del interaction, period


def make_client() -> DiscordStatsClient:
    return DiscordStatsClient(
        guild_id=10,
        reference_provisioner=object(),  # type: ignore[arg-type]
        voice_reconciler=object(),  # type: ignore[arg-type]
        voice_event_handler=object(),  # type: ignore[arg-type]
        text_leaderboard_command_handler=NoOpLeaderboardHandler(),  # type: ignore[arg-type]
    )


def test_topmessages_is_registered_with_four_optional_period_choices() -> None:
    client = make_client()
    command = client.tree.get_command("topmessages", guild=client._command_guild)

    assert command is not None
    assert client.tree.get_command("topmessages") is None
    parameter = command.parameters[0]  # type: ignore[union-attr]
    assert parameter.required is False
    assert [(choice.name, choice.value) for choice in parameter.choices] == [
        ("Сегодня", "today"),
        ("7 дней", "7d"),
        ("30 дней", "30d"),
        ("Всё время", "all"),
    ]
