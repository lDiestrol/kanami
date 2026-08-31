"""Discord presentation adapter for the guild-only ``/top`` command."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.discord.voice_stats import format_voice_duration
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import (
    VoiceLeaderboard,
    VoiceStatisticsPeriod,
    VoiceStatisticsRepository,
    VoiceStatisticsService,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyVoiceStatisticsRepository,
)

logger = logging.getLogger(__name__)

VoiceStatisticsRepositoryFactory = Callable[[AsyncSession], VoiceStatisticsRepository]

VOICE_PERIOD_LABELS = {
    VoiceStatisticsPeriod.TODAY: "Сегодня",
    VoiceStatisticsPeriod.LAST_7_DAYS: "7 дней",
    VoiceStatisticsPeriod.LAST_30_DAYS: "30 дней",
    VoiceStatisticsPeriod.ALL_TIME: "Всё время",
}


def _leaderboard_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member is None:
        return f"Пользователь {user_id}"
    return f"<@{user_id}>"


def build_voice_leaderboard_embed(
    leaderboard: VoiceLeaderboard,
    guild: discord.Guild,
    *,
    checkpoint_interval_seconds: int,
) -> discord.Embed:
    """Render persistence ranking with non-notifying cached-member mentions."""

    embed = discord.Embed(
        title=f"Голосовой рейтинг — {VOICE_PERIOD_LABELS[leaderboard.period]}",
        colour=0x7C5CFC,
    )
    if not leaderboard.entries:
        embed.description = "За этот период голосовой активности пока нет."
    else:
        medals = ("🥇", "🥈", "🥉")
        lines: list[str] = []
        has_estimated = False
        for index, entry in enumerate(leaderboard.entries, start=1):
            prefix = f"{medals[index - 1]} " if index <= len(medals) else ""
            duration = format_voice_duration(entry.total_seconds)
            estimated_marker = " ≈" if entry.estimated_seconds > 0 else ""
            has_estimated = has_estimated or entry.estimated_seconds > 0
            lines.append(
                f"{prefix}**{index}.** {_leaderboard_name(guild, entry.user_id)} — "
                f"**{duration}**{estimated_marker}"
            )
        embed.description = "\n".join(lines)
        if has_estimated:
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


class VoiceLeaderboardCommandHandler:
    """Adapt one public leaderboard interaction to the shared stats service."""

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
        """Return the configured guild TOP 10 as a public response."""

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
        try:
            period = VoiceStatisticsPeriod(
                period_value or VoiceStatisticsPeriod.LAST_7_DAYS
            )
        except ValueError:
            await interaction.response.send_message(
                "Неизвестный период голосового рейтинга.",
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
                leaderboard = await service.get_leaderboard(
                    self._guild_id,
                    period,
                    as_of,
                )
        except Exception:
            logger.exception(
                "Voice leaderboard query failed guild_id=%s user_id=%s period=%s",
                self._guild_id,
                invoking_user_id,
                period.value,
            )
            await interaction.followup.send(
                "Не удалось получить голосовой рейтинг. Попробуйте позже.",
                ephemeral=False,
            )
            return

        await interaction.followup.send(
            embed=build_voice_leaderboard_embed(
                leaderboard,
                interaction.guild,
                checkpoint_interval_seconds=self._checkpoint_interval_seconds,
            ),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
