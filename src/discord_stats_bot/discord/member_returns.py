"""Discord adapter and presentation for durable member return notifications."""

import logging
from collections.abc import Callable
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.discord.server_settings import GuildServerSettingsProvider
from discord_stats_bot.features.member_returns import (
    MemberReturnService,
    MemberReturnSnapshot,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import VoiceStatisticsService
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyAchievementRepository,
    SqlAlchemyTextActivityRepository,
    SqlAlchemyVoiceStatisticsRepository,
)
from discord_stats_bot.persistence.repositories.member_returns import (
    SqlAlchemyMemberReturnRepository,
)

logger = logging.getLogger(__name__)


def _plural(value: int, forms: tuple[str, str, str]) -> str:
    if value % 10 == 1 and value % 100 != 11:
        return forms[0]
    if value % 10 in (2, 3, 4) and value % 100 not in (12, 13, 14):
        return forms[1]
    return forms[2]


def _number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def build_member_return_embed(
    *,
    user_id: int,
    absence_seconds: int,
    voice_seconds: int,
    message_count: int,
    achievement_count: int,
    return_number: int,
) -> discord.Embed:
    """Render only persisted snapshot values and mention by Discord user ID."""

    absence_days = absence_seconds // 86_400
    day_word = _plural(absence_days, ("день", "дня", "дней"))
    embed = discord.Embed(
        title="С возвращением!",
        description=(
            f"👋 С возвращением, <@{user_id}>!\n\n"
            f"Тебя не было **{_number(absence_days)} {day_word}**."
        ),
        colour=0x7C5CFC,
    )
    embed.add_field(
        name="До возвращения",
        value=(
            f"🎙 В голосовых: **{_number(voice_seconds // 3600)} ч**\n"
            f"💬 Сообщений: **{_number(message_count)}**\n"
            f"🏅 Достижений: **{_number(achievement_count)}**"
        ),
        inline=False,
    )
    embed.set_footer(text=f"Это твоё {return_number}-е возвращение на сервер.")
    return embed


class MemberReturnEventHandler:
    """Snapshot and enqueue a concrete Gateway member join when it is a return."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        min_absence_seconds: int,
        min_session_seconds: int,
        wake_delivery: Callable[[], None] | None = None,
        settings_provider: GuildServerSettingsProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
        self._min_absence_seconds = min_absence_seconds
        self._min_session_seconds = min_session_seconds
        self._wake_delivery = wake_delivery
        self._settings_provider = settings_provider

    async def handle(self, member: discord.Member) -> bool:
        if (
            self._settings_provider is not None
            and (await self._settings_provider.get()).return_channel_id is None
        ):
            return False
        joined_at = member.joined_at
        if member.guild.id != self._guild_id or member.bot or joined_at is None:
            return False
        joined_at = normalize_observed_at(joined_at)
        async with self._session_factory.begin() as session:
            service = MemberReturnService(
                SqlAlchemyMemberReturnRepository(session),
                VoiceStatisticsService(
                    SqlAlchemyVoiceStatisticsRepository(session),
                    report_timezone=self._report_timezone,
                    min_session_seconds=self._min_session_seconds,
                ),
                SqlAlchemyTextActivityRepository(session),
                SqlAlchemyAchievementRepository(session),
                report_timezone=self._report_timezone,
                min_absence_seconds=self._min_absence_seconds,
            )
            enqueued = await service.enqueue_if_returned(
                MemberReturnSnapshot(
                    guild_id=member.guild.id,
                    user_id=member.id,
                    joined_at=joined_at,
                    is_bot=member.bot,
                )
            )
        if enqueued and self._wake_delivery is not None:
            self._wake_delivery()
        if enqueued:
            logger.info(
                "Member return enqueued guild_id=%s user_id=%s returned_at=%s",
                member.guild.id,
                member.id,
                joined_at.isoformat(),
            )
        return enqueued
