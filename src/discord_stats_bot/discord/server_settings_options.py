"""Bounded current Discord role/channel options for server settings."""

import discord

from discord_stats_bot.discord.server_settings_control import (
    ServerSettingControlError,
    ServerSettingControlErrorCategory,
)
from discord_stats_bot.features.server_settings import (
    ServerSettingsChannelOption,
    ServerSettingsChannelType,
    ServerSettingsOptions,
    ServerSettingsRoleOption,
)

MAX_ROLE_OPTIONS = 250
MAX_CHANNEL_OPTIONS = 500
MAX_OPTION_NAME_LENGTH = 100


class DiscordServerSettingsOptionsService:
    """Read only safe current targets from the configured guild cache."""

    def __init__(self, client: discord.Client, *, guild_id: int) -> None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        self._client = client
        self._guild_id = guild_id

    async def get_options(self) -> ServerSettingsOptions:
        guild = self._configured_guild()
        bot_member = guild.me
        assert bot_member is not None
        roles = ()
        if bot_member.guild_permissions.manage_roles:
            roles = tuple(
                ServerSettingsRoleOption(role.id, role.name[:MAX_OPTION_NAME_LENGTH])
                for role in sorted(
                    (
                        role
                        for role in guild.roles
                        if role.guild.id == guild.id
                        and role.id != guild.id
                        and not role.is_default()
                        and not role.managed
                        and role < bot_member.top_role
                    ),
                    key=lambda role: (-role.position, role.name.casefold(), role.id),
                )[:MAX_ROLE_OPTIONS]
            )
        channels = tuple(
            ServerSettingsChannelOption(
                channel.id,
                channel.name[:MAX_OPTION_NAME_LENGTH],
                (
                    ServerSettingsChannelType.NEWS
                    if channel.type is discord.ChannelType.news
                    else ServerSettingsChannelType.TEXT
                ),
            )
            for channel in sorted(
                (
                    channel
                    for channel in guild.channels
                    if getattr(getattr(channel, "guild", None), "id", None) == guild.id
                    and getattr(channel, "type", None)
                    in {discord.ChannelType.text, discord.ChannelType.news}
                    and self._can_deliver(channel, bot_member)
                ),
                key=lambda channel: (
                    channel.position,
                    channel.name.casefold(),
                    channel.id,
                ),
            )[:MAX_CHANNEL_OPTIONS]
        )
        return ServerSettingsOptions(roles, channels)

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

    @staticmethod
    def _can_deliver(channel: object, bot_member: discord.Member) -> bool:
        permissions_for = getattr(channel, "permissions_for", None)
        if not callable(permissions_for):
            return False
        permissions = permissions_for(bot_member)
        return bool(
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
        )
