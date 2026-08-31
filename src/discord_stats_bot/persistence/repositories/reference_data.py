"""SQLAlchemy upserts for Discord reference-data provisioning."""

from datetime import datetime

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.reference_data import (
    DiscordUserSnapshot,
    GuildMemberSnapshot,
    GuildSnapshot,
    VoiceChannelSnapshot,
)
from discord_stats_bot.persistence.models import (
    DiscordUser,
    Guild,
    GuildMember,
    VoiceChannel,
)


class SqlAlchemyReferenceDataRepository:
    """Upsert cache snapshots without owning or committing the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_guild(self, guild: GuildSnapshot) -> None:
        statement = insert(Guild).values(id=guild.id, name=guild.name)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[Guild.id],
                set_={"name": statement.excluded.name},
            )
        )

    async def upsert_users(self, users: tuple[DiscordUserSnapshot, ...]) -> None:
        if not users:
            return
        statement = insert(DiscordUser).values(
            [
                {
                    "id": user.id,
                    "is_bot": user.is_bot,
                    "username": user.username,
                    "global_name": user.global_name,
                    "avatar_hash": user.avatar_hash,
                }
                for user in users
            ]
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[DiscordUser.id],
                set_={
                    "is_bot": statement.excluded.is_bot,
                    "username": statement.excluded.username,
                    "global_name": statement.excluded.global_name,
                    "avatar_hash": statement.excluded.avatar_hash,
                },
            )
        )

    async def upsert_members(
        self,
        members: tuple[GuildMemberSnapshot, ...],
    ) -> None:
        if not members:
            return
        complete = tuple(
            member for member in members if member.has_complete_guild_identity
        )
        partial = tuple(
            member for member in members if not member.has_complete_guild_identity
        )
        if complete:
            await self._upsert_members(complete, update_guild_identity=True)
        if partial:
            await self._upsert_members(partial, update_guild_identity=False)

    async def _upsert_members(
        self,
        members: tuple[GuildMemberSnapshot, ...],
        *,
        update_guild_identity: bool,
    ) -> None:
        statement = insert(GuildMember).values(
            [
                {
                    "guild_id": member.guild_id,
                    "user_id": member.user_id,
                    "joined_at": member.joined_at,
                    "left_at": None,
                    "nickname": member.nickname,
                    "guild_avatar_hash": member.guild_avatar_hash,
                }
                for member in members
            ]
        )
        updates: dict[str, object] = {
            "joined_at": func.coalesce(
                statement.excluded.joined_at,
                GuildMember.joined_at,
            ),
        }
        if update_guild_identity:
            updates["left_at"] = None
            updates["nickname"] = statement.excluded.nickname
            updates["guild_avatar_hash"] = statement.excluded.guild_avatar_hash
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[GuildMember.guild_id, GuildMember.user_id],
                set_=updates,
            )
        )

    async def mark_member_left(
        self,
        *,
        guild_id: int,
        user_id: int,
        left_at: datetime,
    ) -> None:
        await self._session.execute(
            update(GuildMember)
            .where(
                GuildMember.guild_id == guild_id,
                GuildMember.user_id == user_id,
            )
            .values(left_at=left_at)
        )

    async def upsert_voice_channels(
        self,
        channels: tuple[VoiceChannelSnapshot, ...],
    ) -> None:
        if not channels:
            return
        statement = insert(VoiceChannel).values(
            [
                {
                    "id": channel.id,
                    "guild_id": channel.guild_id,
                    "name": channel.name,
                    "channel_kind": channel.channel_kind,
                    "is_afk": channel.is_afk,
                }
                for channel in channels
            ]
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[VoiceChannel.id],
                set_={
                    "name": statement.excluded.name,
                    "channel_kind": statement.excluded.channel_kind,
                    "is_afk": statement.excluded.is_afk,
                },
            )
        )
