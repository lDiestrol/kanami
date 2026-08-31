"""Discord presentation adapter for the administrative ``/health`` command."""

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

import discord
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.common.formatting import format_voice_duration

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HealthRuntimeSnapshot:
    """Cheap current-process state owned by the Discord runtime."""

    gateway_ready: bool
    gateway_latency_seconds: float
    registered_guild_command_count: int
    commands_synced: bool
    voice_startup_ready: bool


@dataclass(frozen=True, slots=True)
class DatabaseHealth:
    """Result of one isolated PostgreSQL connectivity probe."""

    available: bool
    latency_seconds: float | None = None


def _format_latency(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "н/д"
    return f"{round(seconds * 1000)} мс"


def build_health_embed(
    *,
    guild: discord.Guild,
    runtime: HealthRuntimeSnapshot,
    database: DatabaseHealth,
    uptime_seconds: int,
    bot_avatar_url: str | None = None,
) -> discord.Embed:
    """Build a compact Russian health overview without sensitive values."""

    healthy = (
        runtime.gateway_ready and database.available and runtime.voice_startup_ready
    )
    embed = discord.Embed(
        title="Kanami — состояние",
        description=f"Сервер: **{guild.name}**",
        colour=0x57F287 if healthy else 0xFEE75C,
    )
    gateway_status = "🟢 OK" if runtime.gateway_ready else "🟡 degraded"
    embed.add_field(
        name="Discord",
        value=(
            f"Gateway: {gateway_status}\n"
            f"Задержка: {_format_latency(runtime.gateway_latency_seconds)}"
        ),
        inline=False,
    )
    database_status = "🟢 OK" if database.available else "🔴 недоступен"
    embed.add_field(
        name="PostgreSQL",
        value=(
            f"Соединение: {database_status}\n"
            f"Задержка: {_format_latency(database.latency_seconds)}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Uptime",
        value=format_voice_duration(uptime_seconds),
        inline=False,
    )
    member_count = guild.member_count
    formatted_member_count = "н/д" if member_count is None else str(member_count)
    sync_status = "OK" if runtime.commands_synced else "не подтверждён"
    embed.add_field(
        name="Runtime",
        value=(
            "Локально зарегистрировано команд: "
            f"{runtime.registered_guild_command_count}\n"
            f"Command sync: {sync_status}\n"
            f"Участников: {formatted_member_count}\n"
            f"Голосовых каналов: {len(guild.voice_channels)}\n"
            f"Stage-каналов: {len(guild.stage_channels)}"
        ),
        inline=False,
    )
    voice_status = "🟢 готов" if runtime.voice_startup_ready else "🟡 восстанавливается"
    embed.add_field(
        name="Voice tracking",
        value=voice_status,
        inline=False,
    )
    if bot_avatar_url:
        embed.set_thumbnail(url=bot_avatar_url)
    return embed


class HealthCommandHandler:
    """Return private read-only diagnostics to guild managers."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        process_started_at: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._process_started_at = process_started_at
        self._monotonic = monotonic

    async def _probe_database(self) -> DatabaseHealth:
        started_at = self._monotonic()
        async with self._session_factory() as session:
            await session.execute(text("SELECT 1"))
        return DatabaseHealth(
            available=True,
            latency_seconds=max(0.0, self._monotonic() - started_at),
        )

    async def handle(
        self,
        interaction: discord.Interaction,
        runtime: HealthRuntimeSnapshot,
    ) -> None:
        """Validate access, probe PostgreSQL, and always return available state."""

        guild = interaction.guild
        user = interaction.user
        if (
            guild is None
            or guild.id != self._guild_id
            or not isinstance(user, discord.Member)
            or user.bot
            or not user.guild_permissions.manage_guild
        ):
            await interaction.response.send_message(
                "Эта команда доступна только участникам с правом «Управлять сервером».",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            database = await self._probe_database()
        except Exception as exc:
            logger.warning(
                "PostgreSQL health probe failed guild_id=%s user_id=%s error_type=%s",
                self._guild_id,
                user.id,
                type(exc).__name__,
            )
            database = DatabaseHealth(available=False)

        uptime_seconds = max(0, int(self._monotonic() - self._process_started_at))
        client_user = getattr(getattr(interaction, "client", None), "user", None)
        display_avatar = getattr(client_user, "display_avatar", None)
        avatar_url = getattr(display_avatar, "url", None)
        await interaction.followup.send(
            embed=build_health_embed(
                guild=guild,
                runtime=runtime,
                database=database,
                uptime_seconds=uptime_seconds,
                bot_avatar_url=str(avatar_url) if avatar_url else None,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
