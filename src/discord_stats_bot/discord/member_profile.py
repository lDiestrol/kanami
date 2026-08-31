"""Discord presentation adapter for the guild-only ``/profile`` command."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.member_profile import (
    KANAMI_MEMBER_ROLE_LABELS,
    AchievementReadRepository,
    MemberProfile,
    MemberProfileService,
    MemberProfileSubject,
    MemberRoleConfiguration,
    can_view_member_statistics,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import VoiceStatisticsRepository
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyAchievementRepository,
    SqlAlchemyVoiceStatisticsRepository,
)

logger = logging.getLogger(__name__)

VoiceRepositoryFactory = Callable[[AsyncSession], VoiceStatisticsRepository]
AchievementRepositoryFactory = Callable[[AsyncSession], AchievementReadRepository]
PROFILE_TRANSACTION_ISOLATION_LEVEL = "REPEATABLE READ"


def format_profile_voice_duration(seconds: int) -> str:
    """Format accumulated profile time without converting hours to days."""

    if seconds < 0:
        raise ValueError("seconds must not be negative")
    if seconds < 60:
        return f"{seconds} сек"

    total_minutes = seconds // 60
    total_hours, minutes = divmod(total_minutes, 60)
    if total_hours == 0:
        return f"{minutes} мин"
    return f"{total_hours} ч {minutes:02d} мин"


def build_member_profile_embed(profile: MemberProfile) -> discord.Embed:
    """Render one compact member passport from a Discord-independent result."""

    description = discord.utils.escape_markdown(profile.display_name)
    if profile.role is not None:
        description += f"\n{KANAMI_MEMBER_ROLE_LABELS[profile.role]}"
    embed = discord.Embed(
        title="Паспорт участника",
        description=description,
        colour=0x7C5CFC,
    )
    if profile.avatar_url:
        embed.set_thumbnail(url=profile.avatar_url)

    if profile.joined_on is None:
        membership = "Дата вступления недоступна"
    else:
        membership = f"С {profile.joined_on:%d.%m.%Y}"
        if profile.server_age_days is not None:
            membership += f"\n{profile.server_age_days} дн. на сервере"
    embed.add_field(name="На сервере", value=membership, inline=False)
    embed.add_field(
        name="Voice",
        value=(
            "Всего: "
            f"{format_profile_voice_duration(profile.voice_all_time_seconds)}\n"
            "За 30 дней: "
            f"{format_profile_voice_duration(profile.voice_last_30_days_seconds)}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Достижения",
        value=f"Получено: {profile.achievement_count}",
        inline=False,
    )
    if profile.has_estimated_voice_time:
        embed.set_footer(
            text="Часть подтверждённого времени восстановлена после разрыва связи."
        )
    return embed


def _member_role_ids(member: discord.Member) -> frozenset[int]:
    return frozenset(role.id for role in member.roles)


def _profile_subject(member: discord.Member) -> MemberProfileSubject:
    avatar = getattr(getattr(member, "display_avatar", None), "url", None)
    return MemberProfileSubject(
        user_id=member.id,
        display_name=member.display_name,
        joined_at=member.joined_at,
        role_ids=_member_role_ids(member),
        avatar_url=str(avatar) if avatar else None,
    )


class MemberProfileCommandHandler:
    """Authorize and adapt a Discord interaction to the Profile v1 service."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        min_session_seconds: int,
        role_configuration: MemberRoleConfiguration,
        voice_repository_factory: VoiceRepositoryFactory = (
            SqlAlchemyVoiceStatisticsRepository
        ),
        achievement_repository_factory: AchievementRepositoryFactory = (
            SqlAlchemyAchievementRepository
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
        self._min_session_seconds = min_session_seconds
        self._role_configuration = role_configuration
        self._voice_repository_factory = voice_repository_factory
        self._achievement_repository_factory = achievement_repository_factory
        self._clock = clock

    async def handle(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        """Return the authorized non-bot member's compact profile."""

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
                "Профили Kanami предназначены для участников сервера, а не ботов.",
                ephemeral=True,
            )
            return
        if not can_view_member_statistics(
            viewer_user_id=interaction.user.id,
            target_user_id=target_user.id,
            viewer_role_ids=_member_role_ids(interaction.user),
            role_configuration=self._role_configuration,
        ):
            await interaction.response.send_message(
                "Просмотр профилей других участников доступен только участникам "
                "с соответствующим уровнем доступа.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        as_of = normalize_observed_at(self._clock())
        try:
            async with self._session_factory() as session:
                await session.connection(
                    execution_options={
                        "isolation_level": PROFILE_TRANSACTION_ISOLATION_LEVEL
                    }
                )
                service = MemberProfileService(
                    self._voice_repository_factory(session),
                    self._achievement_repository_factory(session),
                    report_timezone=self._report_timezone,
                    min_session_seconds=self._min_session_seconds,
                    role_configuration=self._role_configuration,
                )
                profile = await service.get_profile(
                    guild_id=self._guild_id,
                    subject=_profile_subject(target_user),
                    as_of=as_of,
                )
        except Exception:
            logger.exception(
                "Member profile query failed guild_id=%s user_id=%s",
                self._guild_id,
                target_user.id,
            )
            await interaction.followup.send(
                "Не удалось получить профиль участника. Попробуйте позже.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=build_member_profile_embed(profile),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
