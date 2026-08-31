"""Discord presentation adapter for the guild-only ``/achievements`` command."""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.common.formatting import format_voice_duration
from discord_stats_bot.features.achievements import (
    DEFAULT_ACHIEVEMENT_CATALOG,
    AchievementCatalog,
    AchievementDefinition,
    AchievementEvaluator,
    AchievementMetric,
    AchievementMetricSnapshot,
    AchievementRepository,
    AchievementTier,
    AchievementUnlockService,
    UnlockedAchievement,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import (
    VoiceStatisticsRepository,
    VoiceStatisticsService,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyAchievementRepository,
    SqlAlchemyVoiceStatisticsRepository,
)

logger = logging.getLogger(__name__)

AchievementRepositoryFactory = Callable[[AsyncSession], AchievementRepository]
VoiceStatisticsRepositoryFactory = Callable[[AsyncSession], VoiceStatisticsRepository]

_TIER_MARKERS = {
    AchievementTier.BRONZE: "🥉",
    AchievementTier.SILVER: "🥈",
    AchievementTier.GOLD: "🥇",
    None: "🏅",
}
_CATEGORY_LABELS = {
    "voice": "Голос",
    "community": "Сообщество",
    "text": "Сообщения",
}
_FIELD_LIMIT = 1024
_MAX_DETAIL_FIELDS = 23
_MAX_DETAIL_TEXT = 5_500


def _format_metric_value(metric: AchievementMetric, value: int) -> str:
    if metric is AchievementMetric.VOICE_SECONDS:
        return format_voice_duration(value)
    if metric is AchievementMetric.SERVER_AGE_DAYS:
        return f"{value} дн."
    return str(value)


def _format_achievement(
    definition: AchievementDefinition,
    snapshot: AchievementMetricSnapshot,
    *,
    unlocked: bool,
) -> str:
    marker = _TIER_MARKERS[definition.tier]
    category = _CATEGORY_LABELS[definition.category.value]
    threshold = _format_metric_value(definition.metric, definition.threshold)
    heading = f"{marker} **{definition.title}** · {category} · {threshold}"
    if unlocked:
        progress = "✅ Открыто"
    else:
        current = snapshot.value_for(definition.metric)
        if current is None:
            progress = f"🔒 Прогресс недоступен / {threshold}"
        else:
            progress = (
                f"🔒 {_format_metric_value(definition.metric, current)} / {threshold}"
            )
    return f"{heading}\n{definition.description}\n{progress}"


def _chunk_entries(entries: Sequence[str]) -> tuple[str, ...]:
    chunks: list[str] = []
    current = ""
    for entry in entries:
        candidate = f"{current}\n\n{entry}" if current else entry
        if len(candidate) <= _FIELD_LIMIT:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = entry[:_FIELD_LIMIT]
    if current:
        chunks.append(current)
    return tuple(chunks)


def build_achievements_embed(
    *,
    target_user: discord.Member,
    catalog: AchievementCatalog,
    snapshot: AchievementMetricSnapshot,
    unlocked_achievements: Sequence[UnlockedAchievement],
) -> discord.Embed:
    """Build a bounded achievement overview without exposing persistence keys."""

    unlocked_keys = {item.achievement_key for item in unlocked_achievements}
    known_unlocked = tuple(
        definition
        for definition in catalog.definitions
        if definition.key in unlocked_keys
    )
    locked = tuple(
        definition
        for definition in catalog.definitions
        if definition.key not in unlocked_keys
    )
    legacy_count = sum(
        1 for item in unlocked_achievements if catalog.get(item.achievement_key) is None
    )
    embed = discord.Embed(
        title="Достижения",
        description=(
            f"Участник: <@{target_user.id}>\n"
            f"Открыто **{len(known_unlocked)} из {len(catalog.definitions)}**"
        ),
        colour=0x7C5CFC,
    )
    avatar = getattr(getattr(target_user, "display_avatar", None), "url", None)
    if avatar:
        embed.set_thumbnail(url=str(avatar))

    sections = (
        (
            "Открытые",
            tuple(
                _format_achievement(definition, snapshot, unlocked=True)
                for definition in known_unlocked
            ),
            "Пока нет открытых достижений.",
        ),
        (
            "В процессе",
            tuple(
                _format_achievement(definition, snapshot, unlocked=False)
                for definition in locked
            ),
            "Все известные достижения открыты!",
        ),
    )
    detail_fields = 0
    detail_text = len(embed.title or "") + len(embed.description or "")
    omitted_sections = 0
    for name, entries, empty_text in sections:
        chunks = _chunk_entries(entries) or (empty_text,)
        for index, chunk in enumerate(chunks, start=1):
            field_name = f"{name}{f' · {index}' if len(chunks) > 1 else ''}"
            if (
                detail_fields >= _MAX_DETAIL_FIELDS
                or detail_text + len(field_name) + len(chunk) > _MAX_DETAIL_TEXT
            ):
                omitted_sections += len(chunks) - index + 1
                break
            embed.add_field(name=field_name, value=chunk, inline=False)
            detail_fields += 1
            detail_text += len(field_name) + len(chunk)

    notes = []
    if legacy_count:
        notes.append(f"Архивных достижений: {legacy_count}")
    if omitted_sections:
        notes.append("Часть списка скрыта из-за ограничений Discord")
    if notes:
        embed.set_footer(text=" • ".join(notes))
    return embed


def server_age_days(member: discord.Member, as_of: datetime) -> int | None:
    """Return complete guild-membership days, or None when Discord has no date."""

    joined_at = member.joined_at
    if joined_at is None:
        return None
    joined_at = normalize_observed_at(joined_at)
    return max(0, int((as_of - joined_at).total_seconds() // 86_400))


class AchievementsCommandHandler:
    """Update and show one configured-guild member's achievements."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        min_session_seconds: int,
        catalog: AchievementCatalog = DEFAULT_ACHIEVEMENT_CATALOG,
        achievement_repository_factory: AchievementRepositoryFactory = (
            SqlAlchemyAchievementRepository
        ),
        voice_repository_factory: VoiceStatisticsRepositoryFactory = (
            SqlAlchemyVoiceStatisticsRepository
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
        self._min_session_seconds = min_session_seconds
        self._catalog = catalog
        self._achievement_repository_factory = achievement_repository_factory
        self._voice_repository_factory = voice_repository_factory
        self._clock = clock

    async def handle(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        """Re-evaluate and return the selected non-bot member's achievements."""

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
                "Достижения ботов недоступны.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        as_of = normalize_observed_at(self._clock())
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    voice_service = VoiceStatisticsService(
                        self._voice_repository_factory(session),
                        report_timezone=self._report_timezone,
                        min_session_seconds=self._min_session_seconds,
                    )
                    voice_statistics = await voice_service.get_user_statistics(
                        self._guild_id, target_user.id, as_of
                    )
                    snapshot = AchievementMetricSnapshot(
                        voice_seconds=voice_statistics.all_time.total_seconds,
                        server_age_days=server_age_days(target_user, as_of),
                    )
                    achievement_repository = self._achievement_repository_factory(
                        session
                    )
                    unlock_service = AchievementUnlockService(
                        AchievementEvaluator(self._catalog), achievement_repository
                    )
                    await unlock_service.evaluate_and_unlock(
                        guild_id=self._guild_id,
                        user_id=target_user.id,
                        snapshot=snapshot,
                        unlocked_at=as_of,
                    )
                    unlocked = await achievement_repository.list_unlocked(
                        guild_id=self._guild_id,
                        user_id=target_user.id,
                    )
        except Exception:
            logger.exception(
                "Achievement query failed guild_id=%s user_id=%s",
                self._guild_id,
                target_user.id,
            )
            await interaction.followup.send(
                "Не удалось получить достижения. Попробуйте позже.", ephemeral=True
            )
            return

        await interaction.followup.send(
            embed=build_achievements_embed(
                target_user=target_user,
                catalog=self._catalog,
                snapshot=snapshot,
                unlocked_achievements=unlocked,
            ),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
