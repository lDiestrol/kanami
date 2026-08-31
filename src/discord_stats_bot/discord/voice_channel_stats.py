"""Discord adapter for the guild-only ``/channelstats`` command."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.discord.voice_leaderboard import VOICE_PERIOD_LABELS
from discord_stats_bot.discord.voice_stats import format_voice_duration
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import (
    VoiceChannelStatistics,
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
    member = guild.get_member(user_id)
    if member is None:
        return f"Пользователь {user_id}"
    return discord.utils.escape_markdown(member.display_name)


def build_voice_channel_statistics_embed(
    statistics: VoiceChannelStatistics,
    guild: discord.Guild,
    channel_name: str,
    *,
    checkpoint_interval_seconds: int,
) -> discord.Embed:
    """Render one selected channel without mentions or persisted cache names."""

    embed = discord.Embed(
        title=(f"Статистика канала — {discord.utils.escape_markdown(channel_name)}"),
        description=f"Период: {VOICE_PERIOD_LABELS[statistics.period]}",
        colour=0x7C5CFC,
    )
    if statistics.total_seconds == 0:
        embed.description = (
            f"{embed.description}\n\n"
            "За этот период подтверждённой голосовой активности пока нет."
        )
    else:
        total_marker = " ≈" if statistics.estimated_seconds > 0 else ""
        embed.add_field(
            name="Общее время",
            value=(f"{format_voice_duration(statistics.total_seconds)}{total_marker}"),
            inline=False,
        )
        medals = ("🥇", "🥈", "🥉")
        lines = []
        for index, entry in enumerate(statistics.entries, start=1):
            prefix = medals[index - 1] if index <= len(medals) else "•"
            estimated_marker = " ≈" if entry.estimated_seconds > 0 else ""
            lines.append(
                f"{prefix} **{index}.** {_member_name(guild, entry.user_id)} — "
                f"**{format_voice_duration(entry.total_seconds)}**"
                f"{estimated_marker}"
            )
        if lines:
            embed.add_field(
                name="TOP пользователей",
                value="\n".join(lines),
                inline=False,
            )
        if statistics.estimated_seconds > 0 or any(
            entry.estimated_seconds > 0 for entry in statistics.entries
        ):
            embed.add_field(
                name="Примечание",
                value="≈ включает восстановленные участки после разрыва соединения",
                inline=False,
            )
    embed.set_footer(
        text=(
            "Текущая активность обновляется примерно раз в "
            f"{format_voice_duration(checkpoint_interval_seconds)}."
        )
    )
    return embed


class VoiceChannelStatisticsCommandHandler:
    """Adapt one selected voice/stage channel to the shared stats service."""

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
        channel: discord.abc.GuildChannel,
        period_value: str | None = None,
    ) -> None:
        """Return one configured-guild voice/stage channel report publicly."""

        invoking_user_id = interaction.user.id
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
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await interaction.response.send_message(
                "Выберите голосовой или Stage-канал этого сервера.",
                ephemeral=True,
            )
            return
        if channel.guild.id != self._guild_id:
            await interaction.response.send_message(
                "Выберите голосовой или Stage-канал этого сервера.",
                ephemeral=True,
            )
            return
        try:
            period = VoiceStatisticsPeriod(
                period_value or VoiceStatisticsPeriod.LAST_7_DAYS
            )
        except ValueError:
            await interaction.response.send_message(
                "Неизвестный период статистики голосового канала.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        as_of = normalize_observed_at(self._clock())
        try:
            async with self._session_factory() as session:
                service = VoiceStatisticsService(
                    self._repository_factory(session),
                    report_timezone=self._report_timezone,
                    min_session_seconds=self._min_session_seconds,
                )
                statistics = await service.get_channel_statistics(
                    self._guild_id,
                    channel.id,
                    period,
                    as_of,
                )
        except Exception:
            logger.exception(
                "Voice channel report query failed guild_id=%s user_id=%s "
                "channel_id=%s period=%s",
                self._guild_id,
                invoking_user_id,
                channel.id,
                period.value,
            )
            await interaction.followup.send(
                "Не удалось получить статистику голосового канала. Попробуйте позже.",
                ephemeral=False,
            )
            return

        await interaction.followup.send(
            embed=build_voice_channel_statistics_embed(
                statistics,
                interaction.guild,
                channel.name,
                checkpoint_interval_seconds=self._checkpoint_interval_seconds,
            ),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
