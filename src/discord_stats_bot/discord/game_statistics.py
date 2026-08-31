"""Discord presentation adapter for the guild-only ``/games`` command."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.common.formatting import format_voice_duration
from discord_stats_bot.features.game_tracking import (
    GameStatistics,
    GameStatisticsPeriod,
    GameStatisticsRepository,
    GameStatisticsService,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.persistence.repositories import SqlAlchemyGameTrackingRepository

logger = logging.getLogger(__name__)

GameStatisticsRepositoryFactory = Callable[[AsyncSession], GameStatisticsRepository]
DEFAULT_GAME_STATISTICS_PERIOD = GameStatisticsPeriod.LAST_30_DAYS
GAME_PERIOD_LABELS = {
    GameStatisticsPeriod.LAST_7_DAYS: "7 дней",
    GameStatisticsPeriod.LAST_30_DAYS: "30 дней",
    GameStatisticsPeriod.LAST_90_DAYS: "90 дней",
    GameStatisticsPeriod.ALL_TIME: "Всё время",
}


def build_game_statistics_embed(
    statistics: GameStatistics,
    *,
    target_user: discord.Member,
    report_timezone: ZoneInfo,
    checkpoint_interval_seconds: int,
) -> discord.Embed:
    """Render one private game activity report."""

    display_name = discord.utils.escape_markdown(target_user.display_name)
    embed = discord.Embed(
        title=f"🎮 Игровая активность — {display_name}",
        description=f"За {GAME_PERIOD_LABELS[statistics.period].lower()}",
        colour=0x7C5CFC,
    )
    avatar = getattr(getattr(target_user, "display_avatar", None), "url", None)
    if avatar:
        embed.set_thumbnail(url=str(avatar))

    if not statistics.has_data:
        embed.add_field(
            name="Нет данных",
            value="За выбранный период подтверждённые игровые сессии не найдены.",
            inline=False,
        )
    else:
        embed.add_field(
            name="Общее время",
            value=format_voice_duration(statistics.total_seconds),
            inline=True,
        )
        embed.add_field(name="Игр", value=str(statistics.unique_games), inline=True)
        embed.add_field(
            name="Игровых дней", value=str(statistics.gaming_days), inline=True
        )
        embed.add_field(
            name="Топ игр",
            value="\n".join(
                f"{index}. {discord.utils.escape_markdown(item.game_name)} — "
                f"{format_voice_duration(item.total_seconds)}"
                for index, item in enumerate(statistics.top_games, start=1)
            ),
            inline=False,
        )
        assert statistics.latest_game is not None
        latest_local = statistics.latest_game.tracked_at.astimezone(report_timezone)
        today = statistics.as_of.astimezone(report_timezone).date()
        latest_date = (
            "сегодня"
            if latest_local.date() == today
            else latest_local.strftime("%d.%m.%Y")
        )
        embed.add_field(
            name="Последняя игра",
            value=(
                f"{discord.utils.escape_markdown(statistics.latest_game.game_name)}"
                f" — {latest_date}"
            ),
            inline=True,
        )
        assert statistics.longest_session is not None
        embed.add_field(
            name="Самая длинная сессия",
            value=(
                f"{discord.utils.escape_markdown(statistics.longest_session.game_name)}"
                f" — {format_voice_duration(statistics.longest_session.total_seconds)}"
            ),
            inline=True,
        )
    embed.set_footer(
        text=(
            "Текущая игра подтверждается примерно раз в "
            f"{format_voice_duration(checkpoint_interval_seconds)}."
        )
    )
    return embed


class GameStatisticsCommandHandler:
    """Adapt a Discord interaction to the game statistics read service."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        tracking_enabled: bool,
        report_timezone: ZoneInfo,
        checkpoint_interval_seconds: int,
        repository_factory: GameStatisticsRepositoryFactory = (
            SqlAlchemyGameTrackingRepository
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._tracking_enabled = tracking_enabled
        self._report_timezone = report_timezone
        self._checkpoint_interval_seconds = checkpoint_interval_seconds
        self._repository_factory = repository_factory
        self._clock = clock

    async def handle(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        period_value: str | None = None,
    ) -> None:
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
        target_user = user or interaction.user
        if target_user.bot:
            await interaction.response.send_message(
                "Статистика ботов недоступна.", ephemeral=True
            )
            return
        if not self._tracking_enabled:
            await interaction.response.send_message(
                "Игровая статистика недоступна: отслеживание игр не включено.",
                ephemeral=True,
            )
            return
        try:
            period = GameStatisticsPeriod(
                period_value or DEFAULT_GAME_STATISTICS_PERIOD
            )
        except ValueError:
            await interaction.response.send_message(
                "Неизвестный период игровой статистики.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        as_of = normalize_observed_at(self._clock())
        try:
            async with self._session_factory() as session:
                statistics = await GameStatisticsService(
                    self._repository_factory(session),
                    report_timezone=self._report_timezone,
                ).get_user_statistics(self._guild_id, target_user.id, period, as_of)
        except Exception:
            logger.exception(
                "Game statistics query failed guild_id=%s user_id=%s period=%s",
                self._guild_id,
                target_user.id,
                period.value,
            )
            await interaction.followup.send(
                "Не удалось получить игровую статистику. Попробуйте позже.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=build_game_statistics_embed(
                statistics,
                target_user=target_user,
                report_timezone=self._report_timezone,
                checkpoint_interval_seconds=self._checkpoint_interval_seconds,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
