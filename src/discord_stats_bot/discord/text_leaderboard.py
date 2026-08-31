"""Discord presentation adapter for the guild-only ``/topmessages`` command."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.text_activity import (
    TextActivityLeaderboard,
    TextActivityPeriod,
    TextActivityRepository,
    TextActivityService,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyTextActivityRepository,
)

logger = logging.getLogger(__name__)

TextActivityRepositoryFactory = Callable[[AsyncSession], TextActivityRepository]

TEXT_PERIOD_LABELS = {
    TextActivityPeriod.TODAY: "Сегодня",
    TextActivityPeriod.LAST_7_DAYS: "7 дней",
    TextActivityPeriod.LAST_30_DAYS: "30 дней",
    TextActivityPeriod.ALL_TIME: "Всё время",
}


def _leaderboard_name(guild: discord.Guild, user_id: int) -> str:
    if guild.get_member(user_id) is None:
        return f"Пользователь {user_id}"
    return f"<@{user_id}>"


def build_text_leaderboard_embed(
    leaderboard: TextActivityLeaderboard,
    guild: discord.Guild,
) -> discord.Embed:
    """Render deterministic message totals with non-notifying mentions."""

    embed = discord.Embed(
        title=f"Текстовый рейтинг — {TEXT_PERIOD_LABELS[leaderboard.period]}",
        colour=0x7C5CFC,
    )
    if not leaderboard.entries:
        embed.description = "За этот период сообщений пока нет."
        return embed

    medals = ("🥇", "🥈", "🥉")
    lines: list[str] = []
    for index, entry in enumerate(leaderboard.entries, start=1):
        prefix = f"{medals[index - 1]} " if index <= len(medals) else ""
        lines.append(
            f"{prefix}**{index}.** {_leaderboard_name(guild, entry.user_id)} — "
            f"**{entry.message_count}**"
        )
    embed.description = "\n".join(lines)
    embed.set_footer(text="Учитываются обычные сообщения и ответы без их содержимого.")
    return embed


class TextLeaderboardCommandHandler:
    """Adapt one public text leaderboard interaction to the feature service."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        repository_factory: TextActivityRepositoryFactory = (
            SqlAlchemyTextActivityRepository
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
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
            period = TextActivityPeriod(period_value or TextActivityPeriod.LAST_7_DAYS)
        except ValueError:
            await interaction.response.send_message(
                "Неизвестный период текстового рейтинга.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            async with self._session_factory() as session:
                service = TextActivityService(
                    self._repository_factory(session),
                    report_timezone=self._report_timezone,
                )
                leaderboard = await service.get_leaderboard(
                    self._guild_id,
                    period,
                    self._clock(),
                    limit=10,
                )
        except Exception:
            logger.exception(
                "Text leaderboard query failed guild_id=%s user_id=%s period=%s",
                self._guild_id,
                invoking_user_id,
                period.value,
            )
            await interaction.followup.send(
                "Не удалось получить текстовый рейтинг. Попробуйте позже.",
                ephemeral=False,
            )
            return

        await interaction.followup.send(
            embed=build_text_leaderboard_embed(leaderboard, interaction.guild),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
