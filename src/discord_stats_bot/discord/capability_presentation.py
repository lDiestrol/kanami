"""Read current capability-grant subjects from the configured Discord cache."""

import discord

from discord_stats_bot.features.capabilities import (
    CapabilityPresentationSubjects,
)

MAX_CAPABILITY_SUBJECTS = 250
MAX_CAPABILITY_SUBJECT_NAME_LENGTH = 100
MAX_CAPABILITY_ROLE_OPTIONS = 250


class CapabilityPresentationRuntimeUnavailableError(RuntimeError):
    """The configured Discord guild cache is not currently available."""


class DiscordCapabilityPresentationService:
    """Resolve only requested role/member names from the configured guild cache."""

    def __init__(self, client: discord.Client, *, guild_id: int) -> None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        self._client = client
        self._guild_id = guild_id

    async def get_subjects(
        self, role_ids: tuple[int, ...], user_ids: tuple[int, ...]
    ) -> CapabilityPresentationSubjects:
        guild = self._configured_guild()
        roles = tuple(
            (role_id, role.name[:MAX_CAPABILITY_SUBJECT_NAME_LENGTH])
            for role_id in role_ids[:MAX_CAPABILITY_SUBJECTS]
            if (role := guild.get_role(role_id)) is not None
            and role.guild.id == guild.id
        )
        members = tuple(
            (user_id, member.display_name[:MAX_CAPABILITY_SUBJECT_NAME_LENGTH])
            for user_id in user_ids[:MAX_CAPABILITY_SUBJECTS]
            if (member := guild.get_member(user_id)) is not None
            and member.guild.id == guild.id
        )
        return CapabilityPresentationSubjects(roles, members)

    async def get_role_options(self) -> tuple[tuple[int, str], ...]:
        """Return current configured-guild roles suitable for capability grants."""
        guild = self._configured_guild()
        return tuple(
            (role.id, role.name[:MAX_CAPABILITY_SUBJECT_NAME_LENGTH])
            for role in sorted(
                (
                    role
                    for role in guild.roles
                    if role.guild.id == guild.id and not role.is_default()
                ),
                key=lambda role: (-role.position, role.name.casefold(), role.id),
            )[:MAX_CAPABILITY_ROLE_OPTIONS]
        )

    def _configured_guild(self) -> discord.Guild:
        if not self._client.is_ready():
            raise CapabilityPresentationRuntimeUnavailableError()
        guild = self._client.get_guild(self._guild_id)
        if guild is None or guild.id != self._guild_id or guild.me is None:
            raise CapabilityPresentationRuntimeUnavailableError()
        return guild
