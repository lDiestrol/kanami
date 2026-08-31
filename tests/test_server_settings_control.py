from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

import discord_stats_bot.discord.server_settings_control as control_module
from discord_stats_bot.discord.server_settings_control import (
    DiscordServerSettingsControlService,
    ServerSettingControlError,
    ServerSettingControlErrorCategory,
)
from discord_stats_bot.features.server_settings import (
    DISABLED_OVERRIDE,
    ENV_OVERRIDE,
    GuildServerSettingKey,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
    GuildServerSettingsBaselines,
)

T0 = datetime(2026, 8, 23, 12, tzinfo=UTC)


class FakeRole:
    def __init__(
        self,
        guild: object,
        role_id: int,
        *,
        managed: bool = False,
        default: bool = False,
        position: int = 1,
    ) -> None:
        self.guild = guild
        self.id = role_id
        self.managed = managed
        self.default = default
        self.position = position

    def is_default(self) -> bool:
        return self.default

    def __ge__(self, other: "FakeRole") -> bool:
        return self.position >= other.position


class FakeGuild:
    def __init__(self, guild_id: int = 10) -> None:
        self.id = guild_id
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=True),
            top_role=None,
        )
        self.me.top_role = FakeRole(self, 999, position=10)
        self.role: FakeRole | None = FakeRole(self, 20)
        self.channel = SimpleNamespace(
            id=30,
            guild=self,
            type=discord.ChannelType.text,
            permissions_for=lambda member: SimpleNamespace(
                view_channel=True,
                send_messages=True,
                embed_links=True,
            ),
        )

    def get_role(self, role_id: int) -> FakeRole | None:
        return self.role if self.role is not None and self.role.id == role_id else None

    def get_channel(self, channel_id: int) -> object | None:
        return self.channel if self.channel.id == channel_id else None


class FakeClient:
    def __init__(self, guild: FakeGuild | None, *, ready: bool = True) -> None:
        self.guild = guild
        self.ready = ready

    def is_ready(self) -> bool:
        return self.ready

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        if self.guild is not None and self.guild.id == guild_id:
            return self.guild
        return None


class TransactionContext:
    def __init__(self) -> None:
        self.session = object()
        self.exit_error: type[BaseException] | None = None
        self.exited = False

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: object,
    ) -> None:
        self.exit_error = error_type
        self.exited = True


class FakeSessionFactory:
    def __init__(self) -> None:
        self.transaction = TransactionContext()

    def begin(self) -> TransactionContext:
        return self.transaction


class FakeProvider:
    def __init__(self, transaction: TransactionContext | None = None) -> None:
        self.invalidations = 0
        self.transaction = transaction

    async def invalidate(self) -> None:
        if self.transaction is not None:
            assert self.transaction.exited
            assert self.transaction.exit_error is None
        self.invalidations += 1


def make_service(
    client: FakeClient,
    session_factory: FakeSessionFactory | None = None,
    provider: FakeProvider | None = None,
) -> DiscordServerSettingsControlService:
    return DiscordServerSettingsControlService(
        client,  # type: ignore[arg-type]
        session_factory or FakeSessionFactory(),  # type: ignore[arg-type]
        provider or FakeProvider(),  # type: ignore[arg-type]
        guild_id=10,
        baselines=GuildServerSettingsBaselines(
            autorole_role_id=20,
            audit_log_channel_id=30,
            anniversary_channel_id=30,
            return_channel_id=30,
        ),
        clock=lambda: T0,
    )


@pytest.mark.parametrize(
    "client",
    [FakeClient(FakeGuild(), ready=False), FakeClient(None)],
)
@pytest.mark.asyncio
async def test_runtime_unavailable_fails_before_transaction(client: FakeClient) -> None:
    sessions = FakeSessionFactory()
    service = make_service(client, sessions)

    with pytest.raises(ServerSettingControlError) as caught:
        await service.change_setting(
            GuildServerSettingKey.AUDIT_LOG_CHANNEL,
            DISABLED_OVERRIDE,
            actor_discord_user_id=40,
        )

    assert (
        caught.value.category is ServerSettingControlErrorCategory.RUNTIME_UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_invalid_role_and_channel_fail_before_db_mutation() -> None:
    guild = FakeGuild()
    service = make_service(FakeClient(guild))
    guild.role = FakeRole(guild, 20, managed=True)
    with pytest.raises(ServerSettingControlError) as role_error:
        await service.change_setting(
            GuildServerSettingKey.AUTOROLE_ROLE,
            GuildServerSettingOverride(GuildServerSettingOverrideMode.VALUE, 20),
            actor_discord_user_id=40,
        )
    guild.channel.guild = SimpleNamespace(id=99)
    with pytest.raises(ServerSettingControlError) as channel_error:
        await service.change_setting(
            GuildServerSettingKey.RETURN_CHANNEL,
            GuildServerSettingOverride(GuildServerSettingOverrideMode.VALUE, 30),
            actor_discord_user_id=40,
        )

    assert role_error.value.category is ServerSettingControlErrorCategory.INVALID_TARGET
    assert (
        channel_error.value.category is ServerSettingControlErrorCategory.INVALID_TARGET
    )


@pytest.mark.asyncio
async def test_control_service_owns_atomic_transaction_and_invalidates_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = FakeSessionFactory()
    provider = FakeProvider(sessions.transaction)
    calls: list[dict[str, object]] = []

    class MutationService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def change(self, **kwargs: object) -> object:
            calls.append(dict(kwargs))
            return SimpleNamespace(changed=True)

    monkeypatch.setattr(
        control_module, "GuildServerSettingsMutationService", MutationService
    )
    monkeypatch.setattr(
        control_module,
        "SqlAlchemyGuildServerSettingsRepository",
        lambda session: object(),
    )
    monkeypatch.setattr(
        control_module, "SqlAlchemyAuditEventRepository", lambda session: object()
    )
    service = make_service(FakeClient(FakeGuild()), sessions, provider)

    result = await service.change_setting(
        GuildServerSettingKey.RETURN_CHANNEL,
        DISABLED_OVERRIDE,
        actor_discord_user_id=40,
    )

    assert result.changed is True  # type: ignore[attr-defined]
    assert calls[0]["guild_id"] == 10
    assert calls[0]["actor_user_id"] == 40
    assert provider.invalidations == 1
    assert sessions.transaction.exit_error is None


@pytest.mark.asyncio
async def test_noop_commits_without_invalidating_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = FakeSessionFactory()
    provider = FakeProvider(sessions.transaction)

    class MutationService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def change(self, **kwargs: object) -> object:
            return SimpleNamespace(changed=False)

    monkeypatch.setattr(
        control_module, "GuildServerSettingsMutationService", MutationService
    )
    monkeypatch.setattr(
        control_module,
        "SqlAlchemyGuildServerSettingsRepository",
        lambda session: object(),
    )
    monkeypatch.setattr(
        control_module, "SqlAlchemyAuditEventRepository", lambda session: object()
    )
    service = make_service(FakeClient(FakeGuild()), sessions, provider)

    result = await service.change_setting(
        GuildServerSettingKey.RETURN_CHANNEL,
        DISABLED_OVERRIDE,
        actor_discord_user_id=40,
    )

    assert result.changed is False  # type: ignore[attr-defined]
    assert sessions.transaction.exited
    assert sessions.transaction.exit_error is None
    assert provider.invalidations == 0


@pytest.mark.asyncio
async def test_switch_to_missing_env_baseline_target_fails_before_db_mutation() -> None:
    guild = FakeGuild()
    guild.role = None
    sessions = FakeSessionFactory()
    provider = FakeProvider()
    service = make_service(FakeClient(guild), sessions, provider)

    with pytest.raises(ServerSettingControlError) as caught:
        await service.change_setting(
            GuildServerSettingKey.AUTOROLE_ROLE,
            ENV_OVERRIDE,
            actor_discord_user_id=40,
        )

    assert caught.value.category is ServerSettingControlErrorCategory.INVALID_TARGET
    assert sessions.transaction.exited is False
    assert provider.invalidations == 0


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_and_does_not_invalidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = FakeSessionFactory()
    provider = FakeProvider()

    class MutationService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def change(self, **kwargs: object) -> object:
            raise RuntimeError("audit failed")

    monkeypatch.setattr(
        control_module, "GuildServerSettingsMutationService", MutationService
    )
    monkeypatch.setattr(
        control_module,
        "SqlAlchemyGuildServerSettingsRepository",
        lambda session: object(),
    )
    monkeypatch.setattr(
        control_module, "SqlAlchemyAuditEventRepository", lambda session: object()
    )
    service = make_service(FakeClient(FakeGuild()), sessions, provider)

    with pytest.raises(RuntimeError, match="audit failed"):
        await service.change_setting(
            GuildServerSettingKey.RETURN_CHANNEL,
            DISABLED_OVERRIDE,
            actor_discord_user_id=40,
        )

    assert sessions.transaction.exit_error is RuntimeError
    assert provider.invalidations == 0
