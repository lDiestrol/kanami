"""Discord presentation adapter for the guild-only ``/stats`` command."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.common.formatting import format_voice_duration
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import (
    VoicePeriodStanding,
    VoiceStatisticsPeriod,
    VoiceStatisticsRepository,
    VoiceStatisticsService,
    VoiceUserProfile,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyVoiceStatisticsRepository,
)

logger = logging.getLogger(__name__)

VoiceStatisticsRepositoryFactory = Callable[[AsyncSession], VoiceStatisticsRepository]
STATS_TRANSACTION_ISOLATION_LEVEL = "REPEATABLE READ"
DEFAULT_STATS_PERIOD = VoiceStatisticsPeriod.LAST_7_DAYS
VOICE_PROFILE_PERIOD_LABELS = {
    VoiceStatisticsPeriod.TODAY: "Сегодня",
    VoiceStatisticsPeriod.LAST_7_DAYS: "7 дней",
    VoiceStatisticsPeriod.LAST_30_DAYS: "30 дней",
    VoiceStatisticsPeriod.ALL_TIME: "Всё время",
}


def build_voice_statistics_embed(
    profile: VoiceUserProfile,
    guild: discord.Guild,
    *,
    target_user: discord.Member,
    checkpoint_interval_seconds: int,
) -> discord.Embed:
    """Build one selected-period voice profile without Discord API reads."""

    core = profile.core
    description = f"Участник: <@{target_user.id}>"
    if core.durations.estimated_seconds > 0:
        description += "\n≈ Часть времени восстановлена после разрыва соединения."
    embed = discord.Embed(
        title=f"Голосовой профиль • {VOICE_PROFILE_PERIOD_LABELS[core.period]}",
        description=description,
        colour=0x7C5CFC,
    )
    avatar = getattr(getattr(target_user, "display_avatar", None), "url", None)
    if avatar:
        embed.set_thumbnail(url=str(avatar))

    duration = format_voice_duration(core.durations.total_seconds)
    if core.durations.estimated_seconds > 0:
        duration += " ≈"
    embed.add_field(name="В голосе", value=duration, inline=True)
    embed.add_field(
        name="Место",
        value=_format_voice_standing(core.standing),
        inline=True,
    )
    embed.add_field(name="Сессии", value=str(core.session_count), inline=True)
    embed.add_field(
        name="В среднем за сессию",
        value=format_voice_duration(profile.average_session_seconds),
        inline=True,
    )
    embed.add_field(
        name="Любимый канал",
        value=_favorite_channel_name(profile, guild),
        inline=True,
    )
    embed.add_field(
        name="Чаще всего вместе",
        value=_format_companions(profile, guild),
        inline=False,
    )
    if core.previous_durations is not None:
        embed.add_field(
            name="Динамика",
            value=_format_trend(
                core.durations.total_seconds,
                core.previous_durations.total_seconds,
            ),
            inline=False,
        )
    embed.set_footer(
        text=(
            "Текущая активность обновляется примерно раз в "
            f"{format_voice_duration(checkpoint_interval_seconds)}."
        )
    )
    return embed


def _format_voice_standing(standing: VoicePeriodStanding) -> str:
    if standing.rank is not None:
        return f"#{standing.rank} из {standing.participant_count}"
    if standing.participant_count == 0:
        return "Нет данных"
    return f"— из {standing.participant_count}"


def voice_channel_name(guild: discord.Guild, channel_id: int) -> str:
    """Resolve a current cache name for legacy channel-stat renderers."""

    channel = guild.get_channel(channel_id)
    if channel is None:
        return f"Канал {channel_id}"
    return discord.utils.escape_markdown(channel.name)


def _favorite_channel_name(profile: VoiceUserProfile, guild: discord.Guild) -> str:
    favorite = profile.core.favorite_channel
    if favorite is None:
        return "Нет данных"
    channel = guild.get_channel(favorite.channel_id)
    if channel is not None:
        mention = getattr(channel, "mention", None)
        if mention:
            return str(mention)
        return discord.utils.escape_markdown(channel.name)
    if favorite.channel_name:
        return discord.utils.escape_markdown(favorite.channel_name)
    return "Удалённый канал"


def _format_companions(profile: VoiceUserProfile, guild: discord.Guild) -> str:
    if not profile.companions:
        return "Нет данных"
    lines: list[str] = []
    for index, companion in enumerate(profile.companions, start=1):
        member = guild.get_member(companion.user_id)
        name = (
            f"<@{companion.user_id}>" if member is not None else "Участник недоступен"
        )
        duration = format_voice_duration(companion.total_seconds)
        estimated = " ≈" if companion.estimated_seconds > 0 else ""
        lines.append(f"{index}. {name} — {duration}{estimated}")
    return "\n".join(lines)


def _format_trend(current_seconds: int, previous_seconds: int) -> str:
    if previous_seconds == 0:
        if current_seconds == 0:
            return "→ Без изменений"
        return f"↑ Новый результат: {format_voice_duration(current_seconds)}"
    change_percent = round(
        (current_seconds - previous_seconds) * 100 / previous_seconds
    )
    marker = "↑" if change_percent > 0 else "↓" if change_percent < 0 else "→"
    sign = "+" if change_percent > 0 else ""
    return (
        f"{marker} {sign}{change_percent}% к предыдущему такому же периоду "
        f"({format_voice_duration(previous_seconds)})"
    )


class VoiceStatisticsCommandHandler:
    """Adapt a Discord interaction to one read-only profile query."""

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
        user: discord.Member | None = None,
        period_value: str | None = None,
    ) -> None:
        """Return one selected non-bot member's configured-guild profile."""

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
        try:
            period = VoiceStatisticsPeriod(period_value or DEFAULT_STATS_PERIOD)
        except ValueError:
            await interaction.response.send_message(
                "Неизвестный период голосовой статистики.", ephemeral=True
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
                profile = await service.get_user_profile(
                    self._guild_id, target_user.id, period, as_of
                )
        except Exception:
            logger.exception(
                "Voice statistics query failed guild_id=%s user_id=%s period=%s",
                self._guild_id,
                target_user.id,
                period.value,
            )
            await interaction.followup.send(
                "Не удалось получить голосовую статистику. Попробуйте позже.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=build_voice_statistics_embed(
                profile,
                interaction.guild,
                target_user=target_user,
                checkpoint_interval_seconds=self._checkpoint_interval_seconds,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
