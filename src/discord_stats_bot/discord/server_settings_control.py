"""Validated transactional Bot Control adapter for server settings."""

from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.discord.server_settings import (
    RefreshableGuildServerSettingsProvider,
)
from discord_stats_bot.features.server_settings import (
    GuildServerSettingChange,
    GuildServerSettingKey,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
    GuildServerSettingsBaselines,
    GuildServerSettingsMutationService,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyAuditEventRepository,
    SqlAlchemyGuildServerSettingsRepository,
)


class ServerSettingControlErrorCategory(StrEnum):
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    INVALID_TARGET = "invalid_target"


class ServerSettingControlError(RuntimeError):
    def __init__(self, category: ServerSettingControlErrorCategory) -> None:
        self.category = category
        super().__init__(category.value)


class DiscordServerSettingsControlService:
    """Validate cached Discord targets, then own setting+audit transaction."""

    def __init__(
        self,
        client: discord.Client,
        session_factory: async_sessionmaker[AsyncSession],
        provider: RefreshableGuildServerSettingsProvider,
        *,
        guild_id: int,
        baselines: GuildServerSettingsBaselines,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        wake_runtime: Callable[[], None] | None = None,
    ) -> None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        self._client = client
        self._session_factory = session_factory
        self._provider = provider
        self._guild_id = guild_id
        self._baselines = baselines
        self._clock = clock
        self._wake_runtime = wake_runtime

    async def change_setting(
        self,
        key: GuildServerSettingKey,
        override: GuildServerSettingOverride,
        *,
        actor_discord_user_id: int,
    ) -> GuildServerSettingChange:
        guild = self._configured_guild()
        target_value = (
            self._baselines.value_for(key)
            if override.mode is GuildServerSettingOverrideMode.ENV
            else override.value
        )
        if target_value is not None:
            self._validate_target(guild, key, target_value)
        occurred_at = self._clock()
        async with self._session_factory.begin() as session:
            result = await GuildServerSettingsMutationService(
                SqlAlchemyGuildServerSettingsRepository(session),
                SqlAlchemyAuditEventRepository(session),
                baselines=self._baselines,
            ).change(
                guild_id=self._guild_id,
                key=key,
                override=override,
                actor_user_id=actor_discord_user_id,
                occurred_at=occurred_at,
            )
        if result.changed:
            await self._provider.invalidate()
            if self._wake_runtime is not None:
                self._wake_runtime()
        return result

    def _configured_guild(self) -> discord.Guild:
        if not self._client.is_ready():
            raise ServerSettingControlError(
                ServerSettingControlErrorCategory.RUNTIME_UNAVAILABLE
            )
        guild = self._client.get_guild(self._guild_id)
        if guild is None or guild.id != self._guild_id or guild.me is None:
            raise ServerSettingControlError(
                ServerSettingControlErrorCategory.RUNTIME_UNAVAILABLE
            )
        return guild

    def _validate_target(
        self,
        guild: discord.Guild,
        key: GuildServerSettingKey,
        target_id: int,
    ) -> None:
        if key is GuildServerSettingKey.AUTOROLE_ROLE:
            self._validate_role(guild, target_id)
            return
        self._validate_channel(guild, target_id)

    @staticmethod
    def _validate_role(guild: discord.Guild, role_id: int) -> None:
        role = guild.get_role(role_id)
        bot_member = guild.me
        if (
            role is None
            or role.guild.id != guild.id
            or role.id == guild.id
            or role.is_default()
            or role.managed
            or bot_member is None
            or not bot_member.guild_permissions.manage_roles
            or role >= bot_member.top_role
        ):
            raise ServerSettingControlError(
                ServerSettingControlErrorCategory.INVALID_TARGET
            )

    @staticmethod
    def _validate_channel(guild: discord.Guild, channel_id: int) -> None:
        channel = guild.get_channel(channel_id)
        bot_member = guild.me
        if (
            channel is None
            or getattr(getattr(channel, "guild", None), "id", None) != guild.id
            or getattr(channel, "type", None)
            not in {discord.ChannelType.text, discord.ChannelType.news}
            or bot_member is None
            or not callable(getattr(channel, "permissions_for", None))
        ):
            raise ServerSettingControlError(
                ServerSettingControlErrorCategory.INVALID_TARGET
            )
        permissions = channel.permissions_for(bot_member)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
        ):
            raise ServerSettingControlError(
                ServerSettingControlErrorCategory.INVALID_TARGET
            )
