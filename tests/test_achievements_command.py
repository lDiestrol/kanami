from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from discord_stats_bot.discord import (
    AchievementsCommandHandler,
    DiscordStatsClient,
    build_achievements_embed,
)
from discord_stats_bot.discord.achievements import server_age_days
from discord_stats_bot.features.achievements import (
    DEFAULT_ACHIEVEMENT_CATALOG,
    AchievementMetricSnapshot,
    UnlockedAchievement,
)
from discord_stats_bot.features.voice_statistics import VoiceStatistics
from tests.support.discord import make_interaction, make_member
from tests.support.persistence import FakeSessionFactory
from tests.support.voice import make_voice_statistics

AS_OF = datetime(2026, 8, 18, 12, tzinfo=UTC)


class RecordingVoiceRepository:
    def __init__(self, voice_seconds: int) -> None:
        self.voice_seconds = voice_seconds
        self.calls: list[tuple[int, int, object]] = []

    async def get_user_statistics(
        self, guild_id: int, user_id: int, query: object
    ) -> VoiceStatistics:
        self.calls.append((guild_id, user_id, query))
        return make_voice_statistics(
            as_of=AS_OF,
            all_time_seconds=self.voice_seconds,
        )


class InMemoryAchievementRepository:
    def __init__(self, legacy: bool = False) -> None:
        self.records: dict[tuple[int, int, str], UnlockedAchievement] = {}
        self.unlock_calls: list[tuple[int, int, tuple[str, ...]]] = []
        if legacy:
            record = UnlockedAchievement(10, 20, "retired_achievement", AS_OF)
            self.records[(10, 20, record.achievement_key)] = record

    async def unlock_achievements(
        self,
        *,
        guild_id: int,
        user_id: int,
        achievement_keys: tuple[str, ...],
        unlocked_at: datetime,
    ) -> tuple[UnlockedAchievement, ...]:
        self.unlock_calls.append((guild_id, user_id, achievement_keys))
        added = []
        for key in achievement_keys:
            identity = (guild_id, user_id, key)
            if identity not in self.records:
                self.records[identity] = UnlockedAchievement(
                    guild_id, user_id, key, unlocked_at
                )
                added.append(self.records[identity])
        return tuple(added)

    async def list_unlocked(
        self, *, guild_id: int, user_id: int
    ) -> tuple[UnlockedAchievement, ...]:
        return tuple(
            record
            for identity, record in self.records.items()
            if identity[:2] == (guild_id, user_id)
        )


def make_handler(
    *, voice_seconds: int = 7 * 3600 + 12 * 60, legacy: bool = False
) -> tuple[
    AchievementsCommandHandler,
    FakeSessionFactory,
    RecordingVoiceRepository,
    InMemoryAchievementRepository,
]:
    sessions = FakeSessionFactory()
    voice_repository = RecordingVoiceRepository(voice_seconds)
    achievement_repository = InMemoryAchievementRepository(legacy=legacy)
    handler = AchievementsCommandHandler(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        min_session_seconds=10,
        voice_repository_factory=lambda session: voice_repository,  # type: ignore[arg-type]
        achievement_repository_factory=lambda session: achievement_repository,
        clock=lambda: AS_OF,
    )
    return handler, sessions, voice_repository, achievement_repository


@pytest.mark.asyncio
async def test_achievements_defaults_to_invoker_and_calculates_metrics() -> None:
    handler, sessions, voice_repository, achievement_repository = make_handler()
    interaction = make_interaction(
        user=make_member(20, joined_at=AS_OF - timedelta(days=40))
    )

    await handler.handle(interaction)  # type: ignore[arg-type]

    assert voice_repository.calls[0][:2] == (10, 20)
    assert achievement_repository.unlock_calls == [(10, 20, ("server_age_30_days",))]
    assert sessions.events == ["begin", "commit", "close"]
    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    kwargs = interaction.followup.messages[0][1]
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].description == "Участник: <@20>\nОткрыто **1 из 5**"
    text = "\n".join(field.value for field in kwargs["embed"].fields)
    assert "✅ Открыто" in text
    assert "7 ч 12 мин / 10 ч 00 мин" in text


@pytest.mark.asyncio
async def test_explicit_user_unlocks_thresholds_and_repeated_call_is_idempotent() -> (
    None
):
    handler, _, _, repository = make_handler(voice_seconds=50 * 3600)
    interaction = make_interaction()
    selected = make_member(21, joined_at=AS_OF - timedelta(days=365))

    await handler.handle(interaction, selected)  # type: ignore[arg-type]
    await handler.handle(make_interaction(), selected)  # type: ignore[arg-type]

    keys = {identity[2] for identity in repository.records}
    assert keys == {
        "voice_10_hours",
        "voice_50_hours",
        "server_age_30_days",
        "server_age_365_days",
    }
    assert len(repository.records) == 4
    assert all(call[:2] == (10, 21) for call in repository.unlock_calls)


def test_embed_handles_unknown_persisted_key_without_exposing_it() -> None:
    legacy = UnlockedAchievement(10, 20, "retired_achievement", AS_OF)
    embed = build_achievements_embed(
        target_user=make_member(20),  # type: ignore[arg-type]
        catalog=DEFAULT_ACHIEVEMENT_CATALOG,
        snapshot=AchievementMetricSnapshot(voice_seconds=0, server_age_days=0),
        unlocked_achievements=(legacy,),
    )

    assert embed.description == "Участник: <@20>\nОткрыто **0 из 5**"
    assert embed.footer.text == "Архивных достижений: 1"
    assert "retired_achievement" not in str(embed.to_dict())


@pytest.mark.asyncio
async def test_command_handles_unknown_persisted_key() -> None:
    handler, _, _, _ = make_handler(legacy=True)
    interaction = make_interaction(
        user=make_member(20, joined_at=AS_OF - timedelta(days=40))
    )

    await handler.handle(interaction)  # type: ignore[arg-type]

    embed = interaction.followup.messages[0][1]["embed"]
    assert embed.footer.text == "Архивных достижений: 1"
    assert "retired_achievement" not in str(embed.to_dict())


def test_server_age_uses_complete_days_and_allows_missing_join_date() -> None:
    member = make_member(20, joined_at=AS_OF - timedelta(days=24, hours=23))

    assert server_age_days(member, AS_OF) == 24  # type: ignore[arg-type]
    assert server_age_days(make_member(20), AS_OF) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_achievements_rejects_wrong_guild_without_database() -> None:
    handler, sessions, _, _ = make_handler()
    interaction = make_interaction(guild_id=None)

    await handler.handle(interaction)  # type: ignore[arg-type]

    assert sessions.calls == 0
    assert "настроенного сервера" in interaction.response.messages[0][0][0]
    assert interaction.response.messages[0][1]["ephemeral"] is True


def test_runtime_registers_achievements_for_configured_guild() -> None:
    handler, _, _, _ = make_handler()
    dependency = object()
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=dependency,  # type: ignore[arg-type]
        voice_reconciler=dependency,  # type: ignore[arg-type]
        voice_event_handler=dependency,  # type: ignore[arg-type]
        achievements_command_handler=handler,
    )

    command = client.tree.get_command("achievements", guild=client._command_guild)
    assert command is not None
    assert client.tree.get_command("achievements") is None
    assert [parameter.name for parameter in command.parameters] == ["user"]
