from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from discord_stats_bot.discord.game_tracking import (
    GamePresenceEventHandler,
    collect_game_activity_snapshots,
)
from discord_stats_bot.discord.runtime import (
    DiscordStatsClient,
    GuildReferenceProvisioningSummary,
    VoiceStartupReconciliationSummary,
    create_gateway_intents,
)
from discord_stats_bot.features.game_tracking import (
    GameCheckpointResult,
    GameReconciliationResult,
)

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


class UnusedSessionFactory:
    calls = 0

    def begin(self) -> None:
        self.calls += 1
        raise AssertionError("database transaction must not be opened")


class FailingSessionFactory:
    def begin(self) -> None:
        raise RuntimeError("database unavailable")


def member(
    *,
    guild_id: int = 10,
    user_id: int = 20,
    bot: bool = False,
    activities: tuple[object, ...] = (),
) -> SimpleNamespace:
    guild = SimpleNamespace(id=guild_id)
    return SimpleNamespace(
        guild=guild,
        id=user_id,
        bot=bot,
        activities=activities,
    )


def test_gateway_presence_intent_is_strictly_feature_gated() -> None:
    disabled = create_gateway_intents()
    enabled = create_gateway_intents(game_tracking_enabled=True)

    assert not disabled.presences
    assert enabled.presences
    assert disabled.members and enabled.members
    assert disabled.voice_states and enabled.voice_states


def test_discord_adapter_copies_only_minimal_activity_identity() -> None:
    rich_presence = SimpleNamespace(
        type=discord.ActivityType.playing,
        name="Minecraft",
        application_id=123,
        details="Survival",
        state="Secret server",
        secrets={"join": "do-not-store"},
    )

    snapshots = collect_game_activity_snapshots((rich_presence,))  # type: ignore[arg-type]

    assert len(snapshots) == 1
    assert snapshots[0].activity_type == "playing"
    assert snapshots[0].name == "Minecraft"
    assert snapshots[0].application_id == 123
    assert not hasattr(snapshots[0], "details")
    assert not hasattr(snapshots[0], "state")
    assert not hasattr(snapshots[0], "secrets")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    [member(guild_id=11), member(bot=True)],
)
async def test_presence_handler_ignores_wrong_guild_and_bots(
    target: SimpleNamespace,
) -> None:
    sessions = UnusedSessionFactory()
    handler = GamePresenceEventHandler(  # type: ignore[arg-type]
        sessions,
        guild_id=10,
    )

    result = await handler.handle(target, NOW)  # type: ignore[arg-type]

    assert result is None
    assert sessions.calls == 0


@pytest.mark.asyncio
async def test_presence_handler_isolates_database_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = GamePresenceEventHandler(  # type: ignore[arg-type]
        FailingSessionFactory(),
        guild_id=10,
    )

    result = await handler.handle(member(), NOW)  # type: ignore[arg-type]

    assert result is None
    assert "Game Presence transition failed guild_id=10 user_id=20" in caplog.text


class FakeProvisioner:
    def __init__(self) -> None:
        self.left: list[tuple[int, datetime]] = []

    async def mark_member_left(self, target: object, left_at: datetime) -> None:
        self.left.append((target.id, left_at))  # type: ignore[attr-defined]

    async def provision_guild(self, guild: object) -> GuildReferenceProvisioningSummary:
        return GuildReferenceProvisioningSummary(1, 1, 0)


class FakeGameHandler:
    def __init__(self) -> None:
        self.presences: list[tuple[int, datetime]] = []
        self.leaves: list[tuple[int, datetime]] = []

    async def handle(self, target: object, observed_at: datetime) -> None:
        self.presences.append((target.id, observed_at))  # type: ignore[attr-defined]

    async def close_member(self, target: object, observed_at: datetime) -> None:
        self.leaves.append((target.id, observed_at))  # type: ignore[attr-defined]


def make_client(*, game_handler: FakeGameHandler | None = None) -> DiscordStatsClient:
    return DiscordStatsClient(
        guild_id=10,
        reference_provisioner=FakeProvisioner(),  # type: ignore[arg-type]
        voice_reconciler=object(),  # type: ignore[arg-type]
        voice_event_handler=object(),  # type: ignore[arg-type]
        game_presence_event_handler=game_handler,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


def test_client_without_game_handler_does_not_request_presence_intent() -> None:
    client = make_client()

    assert not client.intents.presences


def test_client_with_game_handler_requests_presence_intent() -> None:
    client = make_client(game_handler=FakeGameHandler())

    assert client.intents.presences


@pytest.mark.asyncio
async def test_presence_event_runs_only_after_game_recovery_baseline() -> None:
    handler = FakeGameHandler()
    client = make_client(game_handler=handler)
    target = member()
    client._game_tracking_ready.set()
    client._game_startup_baseline_at = NOW - timedelta(seconds=1)

    await client.on_presence_update(target, target)  # type: ignore[arg-type]

    assert handler.presences == [(20, NOW)]


@pytest.mark.asyncio
async def test_member_leave_closes_open_game_when_tracking_ready() -> None:
    handler = FakeGameHandler()
    client = make_client(game_handler=handler)
    target = member()
    client._game_tracking_ready.set()
    client._game_startup_baseline_at = NOW - timedelta(seconds=1)

    await client.on_member_remove(target)  # type: ignore[arg-type]

    assert handler.leaves == [(20, NOW)]


class FakeVoiceReconciler:
    def __init__(self) -> None:
        self.calls = 0

    async def reconcile_guild(
        self, guild: object, reconciled_at: datetime
    ) -> VoiceStartupReconciliationSummary:
        self.calls += 1
        return VoiceStartupReconciliationSummary(reconciled_at, 0, 0, {}, 0)


class FakeGameReconciler:
    def __init__(self) -> None:
        self.calls = 0

    async def reconcile_guild(
        self, guild: object, reconciled_at: datetime
    ) -> GameReconciliationResult:
        self.calls += 1
        return GameReconciliationResult(reconciled_at, 1, 0, 1, 0)


class FakeGameCheckpointer:
    def __init__(self) -> None:
        self.calls: list[datetime] = []

    async def checkpoint_guild(
        self, guild: object, checkpointed_at: datetime
    ) -> GameCheckpointResult:
        self.calls.append(checkpointed_at)
        return GameCheckpointResult(checkpointed_at, 1, 1)


def lifecycle_client(
    *,
    game_reconciler: FakeGameReconciler | None = None,
    game_checkpointer: FakeGameCheckpointer | None = None,
) -> tuple[DiscordStatsClient, FakeVoiceReconciler, SimpleNamespace]:
    voice = FakeVoiceReconciler()
    guild = SimpleNamespace(id=10, members=[], voice_channels=[], stage_channels=[])
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=FakeProvisioner(),  # type: ignore[arg-type]
        voice_reconciler=voice,  # type: ignore[arg-type]
        voice_event_handler=object(),  # type: ignore[arg-type]
        game_presence_event_handler=(
            FakeGameHandler() if game_reconciler is not None else None
        ),  # type: ignore[arg-type]
        game_startup_reconciler=game_reconciler,  # type: ignore[arg-type]
        game_checkpointer=game_checkpointer,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    client.get_guild = lambda guild_id: guild if guild_id == 10 else None  # type: ignore[method-assign]
    return client, voice, guild


@pytest.mark.asyncio
async def test_repeated_ready_without_disconnect_does_not_split_game_session() -> None:
    game = FakeGameReconciler()
    client, voice, _ = lifecycle_client(game_reconciler=game)

    await client.on_ready()
    await client.on_ready()

    assert voice.calls == 2
    assert game.calls == 1
    assert client._game_tracking_ready.is_set()
    await client.close()


@pytest.mark.asyncio
async def test_disconnect_resume_runs_one_new_conservative_game_recovery() -> None:
    game = FakeGameReconciler()
    client, _, _ = lifecycle_client(game_reconciler=game)

    await client.on_ready()
    await client.on_disconnect()
    assert not client._game_tracking_ready.is_set()
    await client.on_resumed()

    assert game.calls == 2
    assert client._game_tracking_ready.is_set()
    await client.close()


@pytest.mark.asyncio
async def test_clean_shutdown_performs_final_game_checkpoint() -> None:
    checkpointer = FakeGameCheckpointer()
    client, _, _ = lifecycle_client(game_checkpointer=checkpointer)
    client._game_tracking_ready.set()

    await client.close()

    assert checkpointer.calls == [NOW]
