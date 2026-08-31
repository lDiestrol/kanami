import math
from collections.abc import Iterator

import discord
import pytest

import discord_stats_bot.discord.health as health_module
from discord_stats_bot.discord import (
    DiscordStatsClient,
    HealthCommandHandler,
    HealthRuntimeSnapshot,
)
from tests.support.discord import FakeMember, make_channel, make_guild, make_interaction
from tests.support.persistence import FakeSessionFactory


class NoOpDependency:
    pass


class SequenceMonotonic:
    def __init__(self, *values: float) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class RecordingHealthHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[object, HealthRuntimeSnapshot]] = []

    async def handle(
        self,
        interaction: object,
        runtime: HealthRuntimeSnapshot,
    ) -> None:
        self.calls.append((interaction, runtime))


def make_runtime_snapshot(**overrides: object) -> HealthRuntimeSnapshot:
    values = {
        "gateway_ready": True,
        "gateway_latency_seconds": 0.042,
        "registered_guild_command_count": 10,
        "commands_synced": True,
        "voice_startup_ready": True,
    }
    values.update(overrides)
    return HealthRuntimeSnapshot(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def accept_shared_fake_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health_module.discord, "Member", FakeMember)


def make_handler(
    session_factory: FakeSessionFactory,
    *,
    monotonic: SequenceMonotonic | None = None,
) -> HealthCommandHandler:
    return HealthCommandHandler(
        session_factory,  # type: ignore[arg-type]
        guild_id=10,
        process_started_at=100.0,
        monotonic=monotonic or SequenceMonotonic(200.0, 200.004, 200.004),
    )


def embed_text(embed: discord.Embed) -> str:
    return "\n".join(
        [embed.title or "", embed.description or ""]
        + [f"{field.name}\n{field.value}" for field in embed.fields]
    )


def test_health_is_registered_for_configured_guild_with_admin_metadata() -> None:
    handler = RecordingHealthHandler()
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=NoOpDependency(),  # type: ignore[arg-type]
        voice_reconciler=NoOpDependency(),  # type: ignore[arg-type]
        voice_event_handler=NoOpDependency(),  # type: ignore[arg-type]
        health_command_handler=handler,  # type: ignore[arg-type]
    )

    command = client.tree.get_command("health", guild=client._command_guild)

    assert command is not None
    assert client.tree.get_command("health") is None
    assert command.guild_only is True
    assert command.default_permissions == discord.Permissions(manage_guild=True)
    assert command.parameters == []


@pytest.mark.asyncio
async def test_runtime_passes_current_health_snapshot() -> None:
    handler = RecordingHealthHandler()
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=NoOpDependency(),  # type: ignore[arg-type]
        voice_reconciler=NoOpDependency(),  # type: ignore[arg-type]
        voice_event_handler=NoOpDependency(),  # type: ignore[arg-type]
        health_command_handler=handler,  # type: ignore[arg-type]
    )
    client._commands_synced = True
    client._startup_complete.set()
    interaction = make_interaction()

    await client._handle_health_command(interaction)  # type: ignore[arg-type]

    runtime = handler.calls[0][1]
    assert runtime.registered_guild_command_count == 2
    assert runtime.commands_synced is True
    assert runtime.voice_startup_ready is True
    assert runtime.gateway_ready is False


@pytest.mark.asyncio
async def test_manage_guild_member_gets_ephemeral_healthy_response() -> None:
    events: list[object] = []
    guild = make_guild(
        name="test-host",
        member_count=101,
        voice_channels=(make_channel(1, "Voice"), make_channel(2, "Gaming")),
        stage_channels=(make_channel(3, "Stage"),),
    )
    interaction = make_interaction(
        guild=guild,
        manage_guild=True,
    )
    handler = make_handler(FakeSessionFactory(events))

    await handler.handle(interaction, make_runtime_snapshot())  # type: ignore[arg-type]

    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    assert len(interaction.followup.messages) == 1
    _, message = interaction.followup.messages[0]
    assert message["ephemeral"] is True
    allowed_mentions = message["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False
    text = embed_text(message["embed"])
    assert "Kanami — состояние" in text
    assert "test-host" in text
    assert "Gateway: 🟢 OK" in text
    assert "Задержка: 42 мс" in text
    assert "Соединение: 🟢 OK" in text
    assert "Задержка: 4 мс" in text
    assert "1 мин" in text
    assert "Локально зарегистрировано команд: 10" in text
    assert "Command sync: OK" in text
    assert "Участников: 101" in text
    assert "Голосовых каналов: 2" in text
    assert "Stage-каналов: 1" in text
    assert "Voice tracking\n🟢 готов" in text
    assert ("execute", "SELECT 1") in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interaction_kwargs", "user"),
    [
        ({}, None),
        ({"bot": True, "manage_guild": True}, None),
        ({"guild_id": None, "guild": None, "manage_guild": True}, None),
        (
            {"guild_id": 11, "guild": make_guild(guild_id=11), "manage_guild": True},
            None,
        ),
        ({"manage_guild": True}, object()),
    ],
    ids=["member", "bot", "dm", "wrong-guild", "not-member"],
)
async def test_denied_context_is_ephemeral_and_skips_database(
    interaction_kwargs: dict[str, object],
    user: object | None,
) -> None:
    factory = FakeSessionFactory()
    if user is not None:
        interaction_kwargs["user"] = user
    interaction = make_interaction(**interaction_kwargs)  # type: ignore[arg-type]
    handler = make_handler(factory)

    await handler.handle(interaction, make_runtime_snapshot())  # type: ignore[arg-type]

    assert factory.calls == 0
    assert interaction.response.deferred == []
    assert len(interaction.response.messages) == 1
    args, message = interaction.response.messages[0]
    assert message["ephemeral"] is True
    assert "Управлять сервером" in args[0]
    assert interaction.followup.messages == []


@pytest.mark.asyncio
async def test_database_failure_is_logged_and_other_health_is_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    error = RuntimeError(
        "connection failed postgresql+asyncpg://secret:password@production/db"
    )
    interaction = make_interaction(manage_guild=True)
    handler = make_handler(FakeSessionFactory(execute_error=error))

    await handler.handle(interaction, make_runtime_snapshot())  # type: ignore[arg-type]

    assert "PostgreSQL health probe failed guild_id=10 user_id=20" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "secret" not in caplog.text
    assert "password" not in caplog.text
    assert "production" not in caplog.text
    assert "postgresql+asyncpg" not in caplog.text
    _, message = interaction.followup.messages[0]
    text = embed_text(message["embed"])
    assert "Gateway: 🟢 OK" in text
    assert "Соединение: 🔴 недоступен" in text
    assert "Задержка: н/д" in text
    assert "secret" not in text
    assert "password" not in text
    assert "production" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("latency", [math.nan, math.inf, -0.001])
async def test_invalid_gateway_latency_is_unavailable(latency: float) -> None:
    interaction = make_interaction(manage_guild=True)
    handler = make_handler(FakeSessionFactory())

    await handler.handle(  # type: ignore[arg-type]
        interaction,
        make_runtime_snapshot(gateway_latency_seconds=latency),
    )

    _, message = interaction.followup.messages[0]
    discord_field = message["embed"].fields[0]
    assert "Задержка: н/д" in discord_field.value


@pytest.mark.asyncio
async def test_degraded_runtime_states_and_unknown_member_count() -> None:
    guild = make_guild(member_count=None)
    guild.member_count = None
    interaction = make_interaction(guild=guild, manage_guild=True)
    handler = make_handler(FakeSessionFactory())

    await handler.handle(  # type: ignore[arg-type]
        interaction,
        make_runtime_snapshot(
            gateway_ready=False,
            commands_synced=False,
            voice_startup_ready=False,
        ),
    )

    _, message = interaction.followup.messages[0]
    embed = message["embed"]
    text = embed_text(embed)
    assert "Gateway: 🟡 degraded" in text
    assert "Command sync: не подтверждён" in text
    assert "Участников: н/д" in text
    assert "Voice tracking\n🟡 восстанавливается" in text
    assert embed.colour.value == 0xFEE75C


@pytest.mark.asyncio
async def test_bot_avatar_is_optional_and_embed_stays_within_limits() -> None:
    interaction = make_interaction(manage_guild=True, client=None)
    handler = make_handler(FakeSessionFactory())

    await handler.handle(interaction, make_runtime_snapshot())  # type: ignore[arg-type]

    _, message = interaction.followup.messages[0]
    embed = message["embed"]
    assert embed.thumbnail.url is None
    assert len(embed) <= 6000
    assert len(embed.fields) <= 25
    assert all(len(field.name) <= 256 for field in embed.fields)
    assert all(len(field.value) <= 1024 for field in embed.fields)
