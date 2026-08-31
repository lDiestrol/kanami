"""Discord presentation adapter for the guild-only ``/serverstats`` command."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.discord.voice_leaderboard import VOICE_PERIOD_LABELS
from discord_stats_bot.discord.voice_stats import (
    STATS_TRANSACTION_ISOLATION_LEVEL,
    format_voice_duration,
    voice_channel_name,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import (
    VoiceServerStatistics,
    VoiceStatisticsPeriod,
    VoiceStatisticsRepository,
    VoiceStatisticsService,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyVoiceStatisticsRepository,
)

logger = logging.getLogger(__name__)

VoiceStatisticsRepositoryFactory = Callable[[AsyncSession], VoiceStatisticsRepository]


def _member_name(guild: discord.Guild, user_id: int) -> str:
    if guild.get_member(user_id) is None:
        return f"Пользователь {user_id}"
    return f"<@{user_id}>"


def build_voice_server_statistics_embed(
    report: VoiceServerStatistics,
    guild: discord.Guild,
    *,
    checkpoint_interval_seconds: int,
) -> discord.Embed:
    """Build the compact server overview embed."""

    embed = discord.Embed(
        title=f"Статистика сервера — {VOICE_PERIOD_LABELS[report.period]}",
        colour=0x7C5CFC,
    )
    embed.add_field(
        name="Суммарное время участников",
        value=(
            f"{format_voice_duration(report.total_seconds)}"
            f"{' ≈' if report.estimated_seconds > 0 else ''}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Активных участников",
        value=str(report.active_users),
        inline=False,
    )
    embed.add_field(
        name="В среднем на активного",
        value=format_voice_duration(report.average_seconds),
        inline=False,
    )
    if report.top_user is None:
        top_user = "Нет данных за выбранный период."
    else:
        top_user = (
            f"{_member_name(guild, report.top_user.user_id)} — "
            f"{format_voice_duration(report.top_user.total_seconds)}"
            f"{' ≈' if report.top_user.estimated_seconds > 0 else ''}"
        )
    embed.add_field(
        name="Самый активный участник",
        value=top_user,
        inline=False,
    )
    if report.top_channel is None:
        top_channel = "Нет данных за выбранный период."
    else:
        top_channel = (
            f"{voice_channel_name(guild, report.top_channel.channel_id)} — "
            f"{format_voice_duration(report.top_channel.total_seconds)}"
            f"{' ≈' if report.top_channel.estimated_seconds > 0 else ''}"
        )
    embed.add_field(
        name="Самый популярный канал",
        value=top_channel,
        inline=False,
    )
    embed.set_footer(
        text=(
            "Текущая активность обновляется примерно раз в "
            f"{format_voice_duration(checkpoint_interval_seconds)}."
        )
    )
    return embed


class VoiceServerStatisticsCommandHandler:
    """Adapt one private server overview interaction to the service."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        min_session_seconds: int,
        checkpoint_interval_seconds: int,
        repository_factory: VoiceStatisticsRepositoryFactory = (
            SqlAlchemyVoiceStatisticsRepository
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
        self._min_session_seconds = min_session_seconds
        self._checkpoint_interval_seconds = checkpoint_interval_seconds
        self._repository_factory = repository_factory
        self._clock = clock

    async def handle(
        self,
        interaction: discord.Interaction,
        period_value: str | None = None,
    ) -> None:
        """Return the configured guild overview as a private response."""

        if (
            interaction.guild_id != self._guild_id
            or interaction.guild is None
            or interaction.user.bot
        ):
            await interaction.response.send_message(
                "Эта команда доступна только участникам настроенного сервера.",
                ephemeral=True,
            )
            return
        try:
            period = VoiceStatisticsPeriod(
                period_value or VoiceStatisticsPeriod.LAST_7_DAYS
            )
        except ValueError:
            await interaction.response.send_message(
                "Неизвестный период статистики сервера.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        as_of = normalize_observed_at(self._clock())
        try:
            async with self._session_factory() as session:
                await session.connection(
                    execution_options={
                        "isolation_level": STATS_TRANSACTION_ISOLATION_LEVEL
                    }
                )
                service = VoiceStatisticsService(
                    self._repository_factory(session),
                    report_timezone=self._report_timezone,
                    min_session_seconds=self._min_session_seconds,
                )
                report = await service.get_server_report(
                    self._guild_id,
                    period,
                    as_of,
                )
        except Exception:
            logger.exception(
                "Voice server statistics query failed guild_id=%s user_id=%s period=%s",
                self._guild_id,
                interaction.user.id,
                period.value,
            )
            await interaction.followup.send(
                "Не удалось получить статистику сервера. Попробуйте позже.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=build_voice_server_statistics_embed(
                report,
                interaction.guild,
                checkpoint_interval_seconds=self._checkpoint_interval_seconds,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
