"""Controlled Discord adapter for the capability-authorized ``/move`` command."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.audit_logging import (
    AuditEventDraft,
    AuditLoggingService,
)
from discord_stats_bot.features.capabilities import CapabilityAuthorizationService
from discord_stats_bot.features.capabilities.registry import VOICE_MOVE
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyAuditEventRepository,
    SqlAlchemyCapabilityRepository,
)

logger = logging.getLogger(__name__)


class VoiceMoveCommandHandler:
    """Authorize, validate, move, then durably audit one Discord member move."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        wake_delivery: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        capability_repository_factory: Callable[[AsyncSession], object] = (
            SqlAlchemyCapabilityRepository
        ),
        audit_repository_factory: Callable[[AsyncSession], object] = (
            SqlAlchemyAuditEventRepository
        ),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._wake_delivery = wake_delivery
        self._clock = clock
        self._capability_repository_factory = capability_repository_factory
        self._audit_repository_factory = audit_repository_factory

    async def handle(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        destination: discord.VoiceChannel,
    ) -> None:
        guild = interaction.guild
        caller = interaction.user
        if (
            interaction.guild_id != self._guild_id
            or guild is None
            or getattr(guild, "id", None) != self._guild_id
        ):
            await self._respond(interaction, "Команда доступна только на этом сервере.")
            return
        if not isinstance(caller, discord.Member) or caller.bot:
            await self._respond(
                interaction, "Команда доступна только участнику сервера."
            )
            return
        if (
            not isinstance(member, discord.Member)
            or member.guild.id != self._guild_id
            or not isinstance(destination, discord.VoiceChannel)
            or destination.guild.id != self._guild_id
            or guild.afk_channel is not None
            and destination.id == guild.afk_channel.id
        ):
            await self._respond(
                interaction, "Указан недопустимый голосовой канал или участник."
            )
            return
        if member.id == caller.id:
            await self._respond(interaction, "Нельзя переместить самого себя.")
            return

        await interaction.response.defer(ephemeral=True)
        try:
            async with self._session_factory() as session:
                authorization = CapabilityAuthorizationService(
                    self._capability_repository_factory(session)  # type: ignore[arg-type]
                )
                decision = await authorization.authorize(
                    guild_id=self._guild_id,
                    capability=VOICE_MOVE,
                    user_id=caller.id,
                    role_ids=tuple(role.id for role in caller.roles),
                    is_guild_owner=guild.owner_id == caller.id,
                )
        except Exception:
            logger.exception(
                "Voice move authorization failed guild_id=%s actor_id=%s",
                self._guild_id,
                caller.id,
            )
            await self._followup(
                interaction, "Не удалось проверить право на перемещение."
            )
            return
        if not decision.allowed:
            await self._followup(interaction, "У вас нет права перемещать участников.")
            return

        source = member.voice.channel if member.voice is not None else None
        if not await self._validate_move(
            interaction, caller, member, source, destination
        ):
            return
        # Re-read immediately before the REST call: Discord voice state is mutable.
        current_source = member.voice.channel if member.voice is not None else None
        if current_source != source:
            source = current_source
            if not await self._validate_move(
                interaction, caller, member, source, destination
            ):
                return
        assert source is not None
        try:
            await member.move_to(
                destination,
                reason=f"Kanami /move requested by Discord user {caller.id}",
            )
        except discord.Forbidden:
            logger.warning(
                "Voice move forbidden guild_id=%s actor_id=%s target_id=%s",
                self._guild_id,
                caller.id,
                member.id,
            )
            await self._followup(
                interaction, "Discord отклонил перемещение: проверьте права бота."
            )
            return
        except discord.HTTPException:
            logger.exception(
                "Voice move HTTP failure guild_id=%s actor_id=%s target_id=%s",
                self._guild_id,
                caller.id,
                member.id,
            )
            await self._followup(
                interaction, "Не удалось переместить участника в Discord."
            )
            return

        try:
            await self._write_audit(caller, member, source, destination)
        except Exception:
            logger.exception(
                "Voice move audit persistence failed after move guild_id=%s actor_id=%s target_id=%s",
                self._guild_id,
                caller.id,
                member.id,
            )
            await self._followup(
                interaction, "Участник перемещён, но не удалось записать аудит."
            )
            return
        await self._followup(interaction, "Участник перемещён.")

    async def _validate_move(
        self,
        interaction: discord.Interaction,
        caller: discord.Member,
        member: discord.Member,
        source: object | None,
        destination: discord.VoiceChannel,
    ) -> bool:
        if source is None:
            await self._followup(
                interaction, "Участник сейчас не подключён к голосовому каналу."
            )
            return False
        if source == destination:
            await self._followup(interaction, "Участник уже находится в этом канале.")
            return False
        if not isinstance(source, (discord.VoiceChannel, discord.StageChannel)):
            await self._followup(
                interaction, "Текущий голосовой канал участника недоступен."
            )
            return False
        for channel in (source, destination):
            caller_permissions = channel.permissions_for(caller)
            if not caller_permissions.view_channel or not caller_permissions.connect:
                await self._followup(
                    interaction,
                    "У вас нет доступа к исходному или целевому голосовому каналу.",
                )
                return False
            bot_member = channel.guild.me
            if bot_member is None:
                await self._followup(interaction, "Бот недоступен на сервере.")
                return False
            bot_permissions = channel.permissions_for(bot_member)
            if not (
                bot_permissions.view_channel
                and bot_permissions.connect
                and bot_permissions.move_members
            ):
                await self._followup(
                    interaction,
                    "У бота недостаточно прав для перемещения в одном из каналов.",
                )
                return False
        return True

    async def _write_audit(
        self,
        caller: discord.Member,
        member: discord.Member,
        source: discord.VoiceChannel | discord.StageChannel,
        destination: discord.VoiceChannel,
    ) -> None:
        occurred_at = self._clock()
        draft = AuditEventDraft(
            guild_id=self._guild_id,
            category="moderation",
            event_type="moderation.voice_moved",
            occurred_at=occurred_at,
            subject_type="user",
            subject_id=member.id,
            actor_user_id=caller.id,
            before_data={"channel_id": source.id, "channel_name": source.name},
            after_data={"channel_id": destination.id, "channel_name": destination.name},
            details_data={
                "capability_key": str(VOICE_MOVE),
                "via": "slash_command",
                "source_channel_id": source.id,
                "source_channel_name": source.name,
                "destination_channel_id": destination.id,
                "destination_channel_name": destination.name,
            },
        )
        async with self._session_factory.begin() as session:
            await AuditLoggingService(
                self._audit_repository_factory(session)  # type: ignore[arg-type]
            ).create(draft)
        if self._wake_delivery is not None:
            self._wake_delivery()

    @staticmethod
    async def _respond(interaction: discord.Interaction, message: str) -> None:
        await interaction.response.send_message(
            message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @staticmethod
    async def _followup(interaction: discord.Interaction, message: str) -> None:
        await interaction.followup.send(
            message, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
