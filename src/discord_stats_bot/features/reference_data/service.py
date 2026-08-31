"""Application service for idempotent Discord reference-data provisioning."""

from datetime import datetime
from typing import Protocol

from discord_stats_bot.features.reference_data.types import (
    DiscordUserSnapshot,
    GuildMemberSnapshot,
    GuildReferenceSnapshot,
    GuildSnapshot,
    VoiceChannelSnapshot,
)


class ReferenceDataProvisioningRepository(Protocol):
    """Caller-owned persistence operations for current Discord snapshots."""

    async def upsert_guild(self, guild: GuildSnapshot) -> None: ...

    async def upsert_users(self, users: tuple[DiscordUserSnapshot, ...]) -> None: ...

    async def upsert_members(
        self,
        members: tuple[GuildMemberSnapshot, ...],
    ) -> None: ...

    async def upsert_voice_channels(
        self,
        channels: tuple[VoiceChannelSnapshot, ...],
    ) -> None: ...

    async def mark_member_left(
        self,
        *,
        guild_id: int,
        user_id: int,
        left_at: datetime,
    ) -> None: ...


class ReferenceDataProvisioningService:
    """Provision one cache snapshot inside the caller's transaction."""

    def __init__(self, repository: ReferenceDataProvisioningRepository) -> None:
        self._repository = repository

    async def provision_guild(self, snapshot: GuildReferenceSnapshot) -> None:
        """Upsert references in foreign-key order without deleting absent rows."""

        await self._repository.upsert_guild(snapshot.guild)
        await self._repository.upsert_users(snapshot.users)
        await self._repository.upsert_members(snapshot.members)
        await self._repository.upsert_voice_channels(snapshot.voice_channels)

    async def mark_member_left(
        self,
        *,
        guild_id: int,
        user_id: int,
        left_at: datetime,
    ) -> None:
        """Mark one already provisioned membership as departed."""

        await self._repository.mark_member_left(
            guild_id=guild_id,
            user_id=user_id,
            left_at=left_at,
        )
