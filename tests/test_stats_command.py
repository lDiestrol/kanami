from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

import discord_stats_bot.discord.runtime as runtime_module
from discord_stats_bot.discord import (
    DiscordStatsClient,
    VoiceStatisticsCommandHandler,
    build_voice_statistics_embed,
    format_voice_duration,
)
from discord_stats_bot.features.voice_statistics import (
    VoiceChannelUsageEntry,
    VoiceCompanionEntry,
    VoiceFavoriteChannel,
    VoicePeriodDurations,
    VoicePeriodStanding,
    VoiceStatistics,
    VoiceStatisticsPeriod,
    VoiceUserProfile,
    VoiceUserProfileCore,
    VoiceUserStandings,
    VoiceUserTopChannels,
    VoiceUserTopCompanions,
)
from tests.support.discord import FakeChannel, FakeGuild, FakeMember, make_interaction
from tests.support.persistence import FakeSessionFactory
from tests.support.voice import make_voice_statistics

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def statistics(*, estimated: int = 0) -> VoiceStatistics:
    return make_voice_statistics(
        as_of=AS_OF,
        today_seconds=3600,
        last_7_days_seconds=7200,
        last_30_days_seconds=10800,
        all_time_seconds=14400,
        estimated_seconds=estimated,
    )


def standings(
    *,
    today: VoicePeriodStanding = VoicePeriodStanding(1, 10),
    last_7_days: VoicePeriodStanding = VoicePeriodStanding(2, 14),
    last_30_days: VoicePeriodStanding = VoicePeriodStanding(3, 14),
    all_time: VoicePeriodStanding = VoicePeriodStanding(4, 15),
) -> VoiceUserStandings:
    return VoiceUserStandings(
        AS_OF,
        today,
        last_7_days,
        last_30_days,
        all_time,
    )


def top_channels(
    entries: tuple[VoiceChannelUsageEntry, ...] = (
        VoiceChannelUsageEntry(101, 3600, 0),
        VoiceChannelUsageEntry(102, 1200, 60),
    ),
) -> VoiceUserTopChannels:
    return VoiceUserTopChannels(AS_OF, entries)


def top_companions(
    entries: tuple[VoiceCompanionEntry, ...] = (
        VoiceCompanionEntry(201, 3600, 0),
        VoiceCompanionEntry(202, 1200, 60),
        VoiceCompanionEntry(203, 600, 0),
    ),
) -> VoiceUserTopCompanions:
    return VoiceUserTopCompanions(AS_OF, entries)


def profile(
    *,
    period: VoiceStatisticsPeriod = VoiceStatisticsPeriod.LAST_7_DAYS,
    exact_seconds: int = 7200,
    estimated_seconds: int = 0,
    rank: int | None = 2,
    participant_count: int = 14,
    session_count: int = 3,
    favorite: VoiceFavoriteChannel | None = VoiceFavoriteChannel(
        101, "Сохранённый канал", 3600, 0
    ),
    companions: tuple[VoiceCompanionEntry, ...] = (
        VoiceCompanionEntry(201, 3600, 0),
        VoiceCompanionEntry(202, 1200, 60),
        VoiceCompanionEntry(203, 600, 0),
    ),
    previous_seconds: int | None = 3600,
) -> VoiceUserProfile:
    previous = (
        None
        if previous_seconds is None
        else VoicePeriodDurations(exact_seconds=previous_seconds)
    )
    return VoiceUserProfile(
        core=VoiceUserProfileCore(
            as_of=AS_OF,
            period=period,
            durations=VoicePeriodDurations(exact_seconds, estimated_seconds),
            standing=VoicePeriodStanding(rank, participant_count),
            session_count=session_count,
            favorite_channel=favorite,
            previous_durations=previous,
        ),
        companions=companions,
    )


@pytest.mark.parametrize(
    ("seconds", "formatted"),
    [
        (0, "0 сек"),
        (42, "42 сек"),
        (59, "59 сек"),
        (60, "1 мин"),
        (3599, "59 мин"),
        (3600, "1 ч 00 мин"),
        (3 * 3600 + 5 * 60, "3 ч 05 мин"),
        (81 * 3600 + 49 * 60, "3 д 9 ч 49 мин"),
    ],
)
def test_duration_formatter(seconds: int, formatted: str) -> None:
    assert format_voice_duration(seconds) == formatted


def test_duration_formatter_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        format_voice_duration(-1)


def test_embed_is_compact_and_mentions_recovered_time_only_when_present() -> None:
    guild = FakeGuild(
        channels=(FakeChannel(101, "**Общий**"),),
        members=(FakeMember(20), FakeMember(201), FakeMember(202)),
    )
    exact_embed = build_voice_statistics_embed(
        profile(),
        guild,
        target_user=FakeMember(20),  # type: ignore[arg-type]
        checkpoint_interval_seconds=60,
    )
    estimated_embed = build_voice_statistics_embed(
        profile(estimated_seconds=60),
        guild,
        target_user=FakeMember(20),  # type: ignore[arg-type]
        checkpoint_interval_seconds=60,
    )

    assert exact_embed.title == "Голосовой профиль • 7 дней"
    assert exact_embed.description == "Участник: <@20>"
    assert [field.name for field in exact_embed.fields] == [
        "В голосе",
        "Место",
        "Сессии",
        "В среднем за сессию",
        "Любимый канал",
        "Чаще всего вместе",
        "Динамика",
    ]
    assert exact_embed.fields[0].value == "2 ч 00 мин"
    assert exact_embed.fields[1].value == "#2 из 14"
    assert exact_embed.fields[2].value == "3"
    assert exact_embed.fields[3].value == "40 мин"
    assert "раз в 1 мин" in exact_embed.footer.text
    assert "восстановлена" in estimated_embed.description
    assert exact_embed.fields[4].value == "\\*\\*Общий\\*\\*"
    assert "1. <@201> — 1 ч 00 мин" in exact_embed.fields[5].value
    assert "2. <@202> — 21 мин ≈" in exact_embed.fields[5].value
    assert "3. Участник недоступен — 10 мин" in exact_embed.fields[5].value
    assert exact_embed.fields[6].value.startswith("↑ +100%")


def test_embed_formats_missing_rank_and_empty_ranking_without_none() -> None:
    embed = build_voice_statistics_embed(
        profile(
            rank=None,
            participant_count=12,
            session_count=0,
            favorite=None,
            companions=(),
            exact_seconds=0,
            previous_seconds=0,
        ),
        FakeGuild(),
        target_user=FakeMember(20),  # type: ignore[arg-type]
        checkpoint_interval_seconds=60,
    )

    assert embed.fields[1].value == "— из 12"
    assert "#None" not in " ".join(field.value for field in embed.fields)
    assert embed.fields[3].value == "0 сек"
    assert embed.fields[4].value == "Нет данных"
    assert embed.fields[5].value == "Нет данных"


def test_all_time_embed_omits_trend_and_uses_saved_deleted_channel_name() -> None:
    embed = build_voice_statistics_embed(
        profile(
            period=VoiceStatisticsPeriod.ALL_TIME,
            previous_seconds=None,
            favorite=VoiceFavoriteChannel(999, "Старый канал", 100, 0),
        ),
        FakeGuild(),
        target_user=FakeMember(20),  # type: ignore[arg-type]
        checkpoint_interval_seconds=60,
    )

    assert embed.title == "Голосовой профиль • Всё время"
    assert "Динамика" not in [field.name for field in embed.fields]
    assert embed.fields[4].value == "Старый канал"


class RecordingRepository:
    def __init__(
        self,
        profile_result: VoiceUserProfile | None = None,
        core_error: Exception | None = None,
        companions_error: Exception | None = None,
    ) -> None:
        self.profile_result = profile_result or profile()
        self.core_error = core_error
        self.companions_error = companions_error
        self.events: list[object] = []
        self.calls: list[tuple[int, int, object]] = []
        self.companion_calls: list[tuple[int, int, object, object]] = []

    async def get_user_profile_core(
        self, guild_id: int, user_id: int, query: object, window: object
    ) -> VoiceUserProfileCore:
        self.events.append("profile_core")
        self.calls.append((guild_id, user_id, query, window))
        if self.core_error is not None:
            raise self.core_error
        return self.profile_result.core

    async def get_user_profile_companions(
        self, guild_id: int, user_id: int, query: object, window: object
    ) -> tuple[VoiceCompanionEntry, ...]:
        self.events.append("profile_companions")
        self.companion_calls.append((guild_id, user_id, query, window))
        if self.companions_error is not None:
            raise self.companions_error
        return self.profile_result.companions


def make_stats_interaction(
    *, guild_id: int | None = 10, user_id: int = 20, bot: bool = False
) -> object:
    guild = (
        FakeGuild(channels=(FakeChannel(101, "Общий"),))
        if guild_id is not None
        else None
    )
    return make_interaction(
        guild_id=guild_id,
        user_id=user_id,
        bot=bot,
        guild=guild,
    )


def make_handler(
    repository: RecordingRepository,
    *,
    clock: object | None = None,
) -> tuple[VoiceStatisticsCommandHandler, FakeSessionFactory]:
    session_factory = FakeSessionFactory(repository.events)
    return (
        VoiceStatisticsCommandHandler(
            session_factory,  # type: ignore[arg-type]
            guild_id=10,
            report_timezone=ZoneInfo("UTC"),
            min_session_seconds=10,
            checkpoint_interval_seconds=60,
            repository_factory=lambda session: repository,
            clock=clock or (lambda: AS_OF),  # type: ignore[arg-type]
        ),
        session_factory,
    )


@pytest.mark.asyncio
async def test_stats_uses_invoking_user_and_sends_ephemeral_response() -> None:
    repository = RecordingRepository()
    clock_calls: list[datetime] = []

    def clock() -> datetime:
        clock_calls.append(AS_OF)
        return AS_OF

    handler, session_factory = make_handler(repository, clock=clock)
    interaction = make_stats_interaction(user_id=987)

    await handler.handle(interaction)  # type: ignore[arg-type]

    assert repository.calls[0][:2] == (10, 987)
    assert repository.companion_calls[0][:2] == (10, 987)
    assert repository.calls[0][2] is repository.companion_calls[0][2]
    assert repository.calls[0][3] is repository.companion_calls[0][3]
    assert repository.calls[0][3].period is VoiceStatisticsPeriod.LAST_7_DAYS
    assert clock_calls == [AS_OF]
    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    assert interaction.followup.messages[0][1]["ephemeral"] is True
    assert interaction.followup.messages[0][1]["allowed_mentions"].users is False
    assert interaction.followup.messages[0][1]["embed"].title == (
        "Голосовой профиль • 7 дней"
    )
    assert session_factory.sessions[0].closed is True
    assert session_factory.sessions[0].rolled_back is True


@pytest.mark.asyncio
async def test_stats_accepts_selected_period_and_rejects_unknown_value() -> None:
    repository = RecordingRepository(
        profile_result=profile(
            period=VoiceStatisticsPeriod.LAST_30_DAYS,
            previous_seconds=100,
        )
    )
    handler, _ = make_handler(repository)
    interaction = make_stats_interaction()

    await handler.handle(interaction, period_value="30d")  # type: ignore[arg-type]

    assert repository.calls[0][3].period is VoiceStatisticsPeriod.LAST_30_DAYS
    assert interaction.followup.messages[0][1]["embed"].title.endswith("30 дней")

    invalid = make_stats_interaction()
    await handler.handle(invalid, period_value="quarter")  # type: ignore[arg-type]

    assert invalid.response.messages == [
        (("Неизвестный период голосовой статистики.",), {"ephemeral": True})
    ]


@pytest.mark.asyncio
async def test_stats_uses_one_repeatable_read_snapshot_without_commit() -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)

    await handler.handle(make_stats_interaction())  # type: ignore[arg-type]

    assert repository.events == [
        (
            "connection",
            {"execution_options": {"isolation_level": "REPEATABLE READ"}},
        ),
        "profile_core",
        "profile_companions",
        "rollback",
        "close",
    ]
    assert "commit" not in repository.events
    assert session_factory.sessions[0].closed is True
    assert session_factory.sessions[0].rolled_back is True


@pytest.mark.parametrize(
    ("guild_id", "bot"),
    [(11, False), (None, False), (10, True)],
)
@pytest.mark.asyncio
async def test_stats_rejects_other_guild_dm_and_bot(
    guild_id: int | None, bot: bool
) -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_stats_interaction(guild_id=guild_id, bot=bot)

    await handler.handle(interaction)  # type: ignore[arg-type]

    assert repository.calls == []
    assert session_factory.sessions == []
    assert interaction.response.messages[0][1]["ephemeral"] is True
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_stats_uses_selected_member_and_rejects_bot_target() -> None:
    repository = RecordingRepository()
    handler, session_factory = make_handler(repository)
    interaction = make_stats_interaction(user_id=20)

    await handler.handle(interaction, FakeMember(77))  # type: ignore[arg-type]

    assert repository.calls[0][:2] == (10, 77)
    assert repository.companion_calls[0][:2] == (10, 77)
    assert interaction.followup.messages[0][1]["embed"].description.startswith(
        "Участник: <@77>"
    )
    assert session_factory.sessions[0].closed is True

    bot_interaction = make_stats_interaction(user_id=20)
    await handler.handle(  # type: ignore[arg-type]
        bot_interaction,
        FakeMember(88, bot=True),
    )

    assert bot_interaction.response.messages == [
        (("Статистика ботов недоступна.",), {"ephemeral": True})
    ]
    assert len(repository.calls) == 1


@pytest.mark.asyncio
async def test_stats_query_failure_is_isolated_and_user_friendly() -> None:
    repository = RecordingRepository(core_error=RuntimeError("offline failure"))
    handler, session_factory = make_handler(repository)
    interaction = make_stats_interaction()

    await handler.handle(interaction)  # type: ignore[arg-type]

    args, kwargs = interaction.followup.messages[0]
    assert "Не удалось получить" in args[0]
    assert kwargs["ephemeral"] is True
    assert repository.events == [
        (
            "connection",
            {"execution_options": {"isolation_level": "REPEATABLE READ"}},
        ),
        "profile_core",
        "rollback",
        "close",
    ]
    assert "commit" not in repository.events
    assert session_factory.sessions[0].closed is True
    assert session_factory.sessions[0].rolled_back is True


@pytest.mark.asyncio
async def test_stats_companions_failure_isolated_without_partial_embed() -> None:
    repository = RecordingRepository(
        companions_error=RuntimeError("offline companions failure")
    )
    handler, session_factory = make_handler(repository)
    interaction = make_stats_interaction(user_id=456)

    await handler.handle(interaction)  # type: ignore[arg-type]

    args, kwargs = interaction.followup.messages[0]
    assert args == ("Не удалось получить голосовую статистику. Попробуйте позже.",)
    assert "embed" not in kwargs
    assert kwargs["ephemeral"] is True
    assert repository.calls[0][:2] == (10, 456)
    assert repository.companion_calls[0][:2] == (10, 456)
    assert repository.events == [
        (
            "connection",
            {"execution_options": {"isolation_level": "REPEATABLE READ"}},
        ),
        "profile_core",
        "profile_companions",
        "rollback",
        "close",
    ]
    assert "commit" not in repository.events
    assert session_factory.sessions[0].closed is True
    assert session_factory.sessions[0].rolled_back is True


class NoOpProvisioner:
    async def provision_guild(self, guild: object) -> object:
        return runtime_module.GuildReferenceProvisioningSummary(0, 0, 0)


class NoOpReconciler:
    async def reconcile_guild(self, guild: object, reconciled_at: datetime) -> object:
        return runtime_module.VoiceStartupReconciliationSummary(
            reconciled_at, 0, 0, {}, 0
        )


class NoOpEventHandler:
    async def handle(self, *args: object) -> None:
        return None


class NoOpStatsHandler:
    async def handle(
        self,
        interaction: object,
        user: object | None = None,
        period: str | None = None,
    ) -> None:
        return None


class NoOpLeaderboardHandler:
    async def handle(self, interaction: object, period: str | None = None) -> None:
        return None


class NoOpChannelsHandler:
    async def handle(self, interaction: object, period: str | None = None) -> None:
        return None


class NoOpChannelStatsHandler:
    async def handle(
        self,
        interaction: object,
        channel: object,
        period: str | None = None,
    ) -> None:
        return None


class NoOpTogetherHandler:
    async def handle(
        self,
        interaction: object,
        user1: object,
        user2: object,
    ) -> None:
        return None


class NoOpServerStatsHandler:
    async def handle(self, interaction: object, period: str | None = None) -> None:
        return None


class NoOpActivityHandler:
    async def handle(self, interaction: object, period: str | None = None) -> None:
        return None


class NoOpGamesHandler:
    async def handle(
        self,
        interaction: object,
        user: object | None = None,
        period: str | None = None,
    ) -> None:
        return None


class NoOpTextLeaderboardHandler:
    async def handle(self, interaction: object, period: str | None = None) -> None:
        return None


class NoOpHealthHandler:
    async def handle(self, interaction: object, runtime: object) -> None:
        return None


class CommandLifecycleClient(DiscordStatsClient):
    def __init__(self) -> None:
        super().__init__(
            guild_id=10,
            reference_provisioner=NoOpProvisioner(),  # type: ignore[arg-type]
            voice_reconciler=NoOpReconciler(),  # type: ignore[arg-type]
            voice_event_handler=NoOpEventHandler(),  # type: ignore[arg-type]
            voice_stats_command_handler=NoOpStatsHandler(),  # type: ignore[arg-type]
            voice_leaderboard_command_handler=NoOpLeaderboardHandler(),  # type: ignore[arg-type]
            voice_channel_leaderboard_command_handler=NoOpChannelsHandler(),  # type: ignore[arg-type]
            voice_channel_statistics_command_handler=NoOpChannelStatsHandler(),  # type: ignore[arg-type]
            voice_together_command_handler=NoOpTogetherHandler(),  # type: ignore[arg-type]
            voice_server_statistics_command_handler=NoOpServerStatsHandler(),  # type: ignore[arg-type]
            voice_activity_command_handler=NoOpActivityHandler(),  # type: ignore[arg-type]
            game_statistics_command_handler=NoOpGamesHandler(),  # type: ignore[arg-type]
            text_leaderboard_command_handler=NoOpTextLeaderboardHandler(),  # type: ignore[arg-type]
            achievements_command_handler=NoOpStatsHandler(),  # type: ignore[arg-type]
            member_profile_command_handler=NoOpStatsHandler(),  # type: ignore[arg-type]
            health_command_handler=NoOpHealthHandler(),  # type: ignore[arg-type]
            clock=lambda: AS_OF,
        )
        self.recovery_triggers: list[str] = []

    async def _recover_voice_state(self, trigger: str) -> None:
        self.recovery_triggers.append(trigger)


@pytest.mark.asyncio
async def test_guild_command_syncs_once_in_setup_hook_not_on_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sync_calls: list[object] = []

    async def fake_sync(tree: object, *, guild: object) -> list[object]:
        sync_calls.append(guild)
        return [object() for _ in range(11)]

    monkeypatch.setattr(runtime_module.app_commands.CommandTree, "sync", fake_sync)
    client = CommandLifecycleClient()

    await client.setup_hook()
    await client.setup_hook()
    await client.on_ready()
    await client.on_resumed()

    assert len(sync_calls) == 1
    assert sync_calls[0].id == 10  # type: ignore[attr-defined]
    assert client.recovery_triggers == ["ready", "resumed"]
    stats_command = client.tree.get_command("stats", guild=client._command_guild)
    assert stats_command is not None
    period_parameter = next(
        parameter
        for parameter in stats_command.parameters
        if parameter.name == "period"
    )
    assert [choice.value for choice in period_parameter.choices] == [
        "today",
        "7d",
        "30d",
        "all",
    ]
    assert client.tree.get_command("top", guild=client._command_guild) is not None
    assert client.tree.get_command("leaderboard", guild=client._command_guild) is None
    assert client.tree.get_command("stats") is None
    assert client.tree.get_command("top") is None
    assert (
        client.tree.get_command("topmessages", guild=client._command_guild) is not None
    )
    assert client.tree.get_command("topmessages") is None
    assert client.tree.get_command("channels", guild=client._command_guild) is not None
    assert client.tree.get_command("channels") is None
    assert (
        client.tree.get_command("channelstats", guild=client._command_guild) is not None
    )
    assert client.tree.get_command("channelstats") is None
    assert client.tree.get_command("help", guild=client._command_guild) is not None
    assert client.tree.get_command("help") is None
    assert client.tree.get_command("together", guild=client._command_guild) is not None
    assert client.tree.get_command("together") is None
    assert (
        client.tree.get_command("serverstats", guild=client._command_guild) is not None
    )
    assert client.tree.get_command("serverstats") is None
    assert client.tree.get_command("activity", guild=client._command_guild) is not None
    assert client.tree.get_command("activity") is None
    games_command = client.tree.get_command("games", guild=client._command_guild)
    assert games_command is not None
    assert client.tree.get_command("games") is None
    assert [
        (choice.name, choice.value) for choice in games_command.parameters[1].choices
    ] == [  # type: ignore[union-attr]
        ("7 дней", "7d"),
        ("30 дней", "30d"),
        ("90 дней", "90d"),
        ("Всё время", "all"),
    ]
    assert (
        client.tree.get_command("achievements", guild=client._command_guild) is not None
    )
    assert client.tree.get_command("achievements") is None
    assert client.tree.get_command("profile", guild=client._command_guild) is not None
    assert client.tree.get_command("profile") is None
    assert client.tree.get_command("health", guild=client._command_guild) is not None
    assert client.tree.get_command("health") is None
    assert len(client.tree.get_commands(guild=client._command_guild)) == 13


@pytest.mark.asyncio
async def test_command_sync_failure_does_not_abort_client_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_sync(tree: object, *, guild: object) -> list[object]:
        raise RuntimeError("offline sync failure")

    monkeypatch.setattr(runtime_module.app_commands.CommandTree, "sync", failing_sync)
    client = CommandLifecycleClient()

    await client.setup_hook()

    assert client._commands_synced is False


def test_registered_top_has_four_russian_choices_and_optional_default() -> None:
    client = CommandLifecycleClient()
    command = client.tree.get_command("top", guild=client._command_guild)

    assert command is not None
    parameter = command.parameters[0]  # type: ignore[union-attr]
    assert parameter.required is False
    assert [(choice.name, choice.value) for choice in parameter.choices] == [
        ("Сегодня", "today"),
        ("7 дней", "7d"),
        ("30 дней", "30d"),
        ("Всё время", "all"),
    ]


def test_registered_commands_have_current_russian_descriptions() -> None:
    client = CommandLifecycleClient()

    descriptions = {
        command.name: command.description
        for command in client.tree.get_commands(guild=client._command_guild)
    }

    assert descriptions == {
        "stats": "Показать голосовую статистику участника",
        "top": "Показать рейтинг участников по времени в голосовых каналах",
        "topmessages": "Показать рейтинг участников по сообщениям",
        "channels": "Показать рейтинг голосовых каналов",
        "channelstats": "Показать статистику голосового канала",
        "together": "Показать совместную голосовую статистику двух участников",
        "serverstats": "Показать общую голосовую статистику сервера",
        "activity": "Показать, когда сервер наиболее активен",
        "games": "Показать игровую активность участника",
        "achievements": "Показать достижения участника",
        "profile": "Показать паспорт участника Kanami",
        "health": "Показать состояние Kanami",
        "help": "Показать справку по командам Kanami",
    }


def test_registered_stats_has_optional_member_parameter() -> None:
    client = CommandLifecycleClient()
    command = client.tree.get_command("stats", guild=client._command_guild)

    assert command is not None
    parameter = command.parameters[0]  # type: ignore[union-attr]
    assert parameter.name == "user"
    assert parameter.required is False
    assert parameter.type.name == "user"
    assert parameter.description == "Участник; если не указан — вы"


def test_registered_profile_has_optional_member_parameter() -> None:
    client = CommandLifecycleClient()
    command = client.tree.get_command("profile", guild=client._command_guild)

    assert command is not None
    parameter = command.parameters[0]  # type: ignore[union-attr]
    assert parameter.name == "user"
    assert parameter.required is False
    assert parameter.type.name == "user"
    assert parameter.description == "Участник; если не указан — вы"


def test_registered_together_has_two_required_member_parameters() -> None:
    client = CommandLifecycleClient()
    command = client.tree.get_command("together", guild=client._command_guild)

    assert command is not None
    user1, user2 = command.parameters  # type: ignore[union-attr]
    assert (user1.name, user1.required, user1.type.name, user1.description) == (
        "user1",
        True,
        "user",
        "Первый участник",
    )
    assert (user2.name, user2.required, user2.type.name, user2.description) == (
        "user2",
        True,
        "user",
        "Второй участник",
    )


def test_registered_serverstats_has_optional_russian_period_choices() -> None:
    client = CommandLifecycleClient()
    command = client.tree.get_command("serverstats", guild=client._command_guild)

    assert command is not None
    parameter = command.parameters[0]  # type: ignore[union-attr]
    assert parameter.required is False
    assert [(choice.name, choice.value) for choice in parameter.choices] == [
        ("Сегодня", "today"),
        ("7 дней", "7d"),
        ("30 дней", "30d"),
        ("Всё время", "all"),
    ]


def test_registered_activity_has_three_optional_period_choices() -> None:
    client = CommandLifecycleClient()
    command = client.tree.get_command("activity", guild=client._command_guild)

    assert command is not None
    parameter = command.parameters[0]  # type: ignore[union-attr]
    assert parameter.required is False
    assert parameter.description == "Период; по умолчанию — 30 дней"
    assert [(choice.name, choice.value) for choice in parameter.choices] == [
        ("7 дней", "7d"),
        ("30 дней", "30d"),
        ("90 дней", "90d"),
    ]


def test_registered_channels_has_four_russian_choices_and_optional_default() -> None:
    client = CommandLifecycleClient()
    command = client.tree.get_command("channels", guild=client._command_guild)

    assert command is not None
    parameter = command.parameters[0]  # type: ignore[union-attr]
    assert parameter.required is False
    assert [(choice.name, choice.value) for choice in parameter.choices] == [
        ("Сегодня", "today"),
        ("7 дней", "7d"),
        ("30 дней", "30d"),
        ("Всё время", "all"),
    ]


def test_registered_channelstats_has_required_vocal_channel_and_period_choices() -> (
    None
):
    client = CommandLifecycleClient()
    command = client.tree.get_command("channelstats", guild=client._command_guild)

    assert command is not None
    channel, period = command.parameters  # type: ignore[union-attr]
    assert channel.name == "channel"
    assert channel.required is True
    assert {channel_type.name for channel_type in channel.channel_types} == {
        "voice",
        "stage_voice",
    }
    assert period.required is False
    assert [(choice.name, choice.value) for choice in period.choices] == [
        ("Сегодня", "today"),
        ("7 дней", "7d"),
        ("30 дней", "30d"),
        ("Всё время", "all"),
    ]
