"""Discord presentation adapter for the guild-only ``/activity`` command."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import (
    VoiceActivityPeriod,
    VoiceActivityReport,
    VoiceStatisticsRepository,
    VoiceStatisticsService,
    activity_heatmap_levels,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyVoiceStatisticsRepository,
)

logger = logging.getLogger(__name__)

VoiceStatisticsRepositoryFactory = Callable[[AsyncSession], VoiceStatisticsRepository]

ACTIVITY_PERIOD_LABELS = {
    VoiceActivityPeriod.LAST_7_DAYS: "последние 7 дней",
    VoiceActivityPeriod.LAST_30_DAYS: "последние 30 дней",
    VoiceActivityPeriod.LAST_90_DAYS: "последние 90 дней",
}
RUSSIAN_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
HEATMAP_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


def _hour_range(hour: int, *, hours: int = 1) -> str:
    return f"{hour:02d}:00 — {(hour + hours) % 24:02d}:00"


def _activity_heatmap(report: VoiceActivityReport) -> str:
    levels = activity_heatmap_levels(report.heatmap_activity)
    lines = [f"      {' '.join(HEATMAP_WEEKDAYS)}"]
    lines.extend(
        f"{row * 3:02d}:00 {' '.join(values)}" for row, values in enumerate(levels)
    )
    return f"```text\n{'\n'.join(lines)}\n```"


def build_voice_activity_embed(report: VoiceActivityReport) -> discord.Embed:
    """Render compact recurring activity insights without exposing raw internals."""

    embed = discord.Embed(
        title="🕘 Когда сервер оживает",
        description=(f"Голосовая активность • {ACTIVITY_PERIOD_LABELS[report.period]}"),
        colour=0x7C5CFC,
    )
    if not report.has_activity:
        embed.add_field(
            name="Недостаточно данных",
            value=(
                f"За {ACTIVITY_PERIOD_LABELS[report.period]} недостаточно "
                "голосовой активности для построения статистики."
            ),
            inline=False,
        )
    else:
        medals = ("🥇", "🥈", "🥉")
        embed.add_field(
            name="Самое активное время сервера",
            value="\n".join(
                f"{medal} {_hour_range(hour)}"
                for medal, hour in zip(medals, report.top_hours, strict=True)
            ),
            inline=False,
        )
        assert report.active_weekday is not None
        embed.add_field(
            name="Самый активный день",
            value=RUSSIAN_WEEKDAYS[report.active_weekday],
            inline=True,
        )
        assert report.quietest_period is not None
        quiet_weekday, quiet_hour = report.quietest_period
        embed.add_field(
            name="Самое тихое время",
            value=(
                f"{RUSSIAN_WEEKDAYS[quiet_weekday]}, {_hour_range(quiet_hour, hours=3)}"
            ),
            inline=True,
        )
        embed.add_field(
            name="Активность",
            value=_activity_heatmap(report),
            inline=False,
        )
        if report.has_estimated_time:
            embed.add_field(
                name="Примечание",
                value=(
                    "Результат включает восстановленные участки после разрыва "
                    "соединения."
                ),
                inline=False,
            )
    embed.set_footer(text=f"Часовой пояс: {report.timezone_name}")
    return embed


class VoiceActivityCommandHandler:
    """Adapt one public activity interaction to the shared statistics service."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        min_session_seconds: int,
        repository_factory: VoiceStatisticsRepositoryFactory = (
            SqlAlchemyVoiceStatisticsRepository
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
        self._min_session_seconds = min_session_seconds
        self._repository_factory = repository_factory
        self._clock = clock

    async def handle(
        self,
        interaction: discord.Interaction,
        period_value: str | None = None,
    ) -> None:
        """Return recurring guild activity as a public response."""

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
            period = VoiceActivityPeriod(
                period_value or VoiceActivityPeriod.LAST_30_DAYS
            )
        except ValueError:
            await interaction.response.send_message(
                "Неизвестный период активности сервера.",
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
                report = await service.get_activity_report(
                    self._guild_id, period, as_of
                )
        except Exception:
            logger.exception(
                "Voice activity query failed guild_id=%s user_id=%s period=%s",
                self._guild_id,
                interaction.user.id,
                period.value,
            )
            await interaction.followup.send(
                "Не удалось получить активность сервера. Попробуйте позже.",
                ephemeral=False,
            )
            return

        await interaction.followup.send(
            embed=build_voice_activity_embed(report),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
