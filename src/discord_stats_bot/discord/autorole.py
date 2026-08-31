"""Discord adapter for assigning one configured role to new members."""

import logging

import discord

from discord_stats_bot.discord.server_settings import GuildServerSettingsProvider
from discord_stats_bot.features.autorole import AutoroleService

logger = logging.getLogger(__name__)

AUTOROLE_REASON = "Kanami automatic role assignment"


class AutoroleHandler:
    """Validate cached Discord state and assign the configured role once."""

    def __init__(
        self,
        *,
        guild_id: int,
        role_id: int | None = None,
        settings_provider: GuildServerSettingsProvider | None = None,
    ) -> None:
        if role_id is None and settings_provider is None:
            raise ValueError("role_id or settings_provider is required")
        self._guild_id = guild_id
        self._role_id = role_id
        self._settings_provider = settings_provider

    @property
    def guild_id(self) -> int:
        return self._guild_id

    @property
    def role_id(self) -> int | None:
        return self._role_id

    async def handle(self, member: discord.Member) -> bool:
        """Assign autorole when safe; return whether Discord accepted the change."""

        guild = member.guild
        role_id = self._role_id
        if self._settings_provider is not None:
            role_id = (await self._settings_provider.get()).autorole_role_id
        if role_id is None:
            return False
        service = AutoroleService(guild_id=self._guild_id, role_id=role_id)
        if not service.should_consider(
            guild_id=guild.id,
            is_bot=member.bot,
            member_role_ids=(role.id for role in member.roles),
        ):
            return False

        role = guild.get_role(role_id)
        if role is None:
            self._warn(member, role_id, "role_not_found")
            return False
        if role.id == guild.id or role.is_default():
            self._warn(member, role_id, "default_everyone_role")
            return False
        if role.managed:
            self._warn(member, role_id, "managed_role")
            return False

        bot_member = guild.me
        if bot_member is None:
            self._warn(member, role_id, "bot_member_unavailable")
            return False
        if not bot_member.guild_permissions.manage_roles:
            self._warn(member, role_id, "manage_roles_permission_missing")
            return False
        if role >= bot_member.top_role:
            self._warn(member, role_id, "insufficient_role_hierarchy")
            return False

        try:
            await member.add_roles(role, reason=AUTOROLE_REASON)
        except discord.Forbidden as error:
            self._warn(member, role_id, f"discord_forbidden:{error.status}")
            return False
        except discord.HTTPException as error:
            self._warn(member, role_id, f"discord_http_error:{error.status}")
            return False

        logger.info(
            "Autorole assigned guild_id=%s user_id=%s role_id=%s",
            guild.id,
            member.id,
            role.id,
        )
        return True

    def _warn(self, member: discord.Member, role_id: int, reason: str) -> None:
        logger.warning(
            "Autorole skipped guild_id=%s user_id=%s role_id=%s reason=%s",
            member.guild.id,
            member.id,
            role_id,
            reason,
        )
