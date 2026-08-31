"""Discord presentation adapter for the guild-only ``/together`` command."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.discord.voice_stats import (
    STATS_TRANSACTION_ISOLATION_LEVEL,
    format_voice_duration,
    voice_channel_name,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import (
    VoicePairStatistics,
    VoiceStatisticsRepository,
    VoiceStatisticsService,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyVoiceStatisticsRepository,
)

logger = logging.getLogger(__name__)

VoiceStatisticsRepositoryFactory = Callable[[AsyncSession], VoiceStatisticsRepository]


def format_voice_percentage(pair_seconds: int, total_seconds: int) -> str:
    """Format a compact percentage without hiding a zero denominator."""

    if pair_seconds < 0 or total_seconds < 0:
        raise ValueError("voice durations must not be negative")
    if total_seconds == 0:
        return "0%"
    percentage = round(pair_seconds * 100 / total_seconds, 1)
    if percentage.is_integer():
        return f"{int(percentage)}%"
    return f"{percentage:.1f}%"


def _member_name(guild: discord.Guild, user_id: int) -> str:
    if guild.get_member(user_id) is None:
        return f"Пользователь {user_id}"
    return f"<@{user_id}>"


def build_voice_pair_statistics_embed(
    report: VoicePairStatistics,
    guild: discord.Guild,
    *,
    checkpoint_interval_seconds: int,
) -> discord.Embed:
    """Build the compact all-time pair report."""

    user1_name = _member_name(guild, report.user1_id)
    user2_name = _member_name(guild, report.user2_id)
    embed = discord.Embed(
        title="Вместе в голосовых",
        description=f"{user1_name} × {user2_name}",
        colour=0x7C5CFC,
    )
    embed.add_field(
        name="Совместное время — всё время",
        value=(
            f"{format_voice_duration(report.total_seconds)}"
            f"{' ≈' if report.estimated_seconds > 0 else ''}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Доля голосового времени",
        value=(
            f"{user1_name} — "
            f"{format_voice_percentage(report.total_seconds, report.user1_total_seconds)}\n"
            f"{user2_name} — "
            f"{format_voice_percentage(report.total_seconds, report.user2_total_seconds)}"
        ),
        inline=False,
    )
    if report.channels:
        channels = "\n".join(
            (
                f"{index}. {voice_channel_name(guild, entry.channel_id)} — "
                f"{format_voice_duration(entry.total_seconds)}"
                f"{' ≈' if entry.estimated_seconds > 0 else ''}"
            )
            for index, entry in enumerate(report.channels, start=1)
        )
    else:
        channels = "Совместной голосовой активности пока нет."
    embed.add_field(
        name="Чаще всего вместе в каналах",
        value=channels,
        inline=False,
    )
    embed.set_footer(
        text=(
            "Текущая активность обновляется примерно раз в "
            f"{format_voice_duration(checkpoint_interval_seconds)}."
        )
    )
    return embed


class VoiceTogetherCommandHandler:
    """Adapt one private pair-statistics interaction to the service."""

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
        user1: discord.Member,
        user2: discord.Member,
    ) -> None:
        """Return one configured-guild non-bot pair report."""

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
        if user1.bot or user2.bot:
            await interaction.response.send_message(
                "Сравнение с ботами недоступно.",
                ephemeral=True,
            )
            return
        if user1.id == user2.id:
            await interaction.response.send_message(
                "Выберите двух разных участников.",
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
                report = await service.get_pair_report(
                    self._guild_id,
                    user1.id,
                    user2.id,
                    as_of,
                )
        except Exception:
            logger.exception(
                "Voice pair statistics query failed guild_id=%s user1_id=%s "
                "user2_id=%s",
                self._guild_id,
                user1.id,
                user2.id,
            )
            await interaction.followup.send(
                "Не удалось получить совместную статистику. Попробуйте позже.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=build_voice_pair_statistics_embed(
                report,
                interaction.guild,
                checkpoint_interval_seconds=self._checkpoint_interval_seconds,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
