"""Member Profile v1 orchestration and reusable access policy."""

from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from discord_stats_bot.features.achievements import UnlockedAchievement
from discord_stats_bot.features.member_profile.types import (
    KanamiMemberRole,
    MemberProfile,
    MemberProfileSubject,
    MemberRoleConfiguration,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import (
    VoiceStatisticsRepository,
    VoiceStatisticsService,
)

_ROLE_RESOLUTION_ORDER = (
    (KanamiMemberRole.GOLD, "gold_role_id"),
    (KanamiMemberRole.PURPLE, "purple_role_id"),
    (KanamiMemberRole.GUARDIAN, "guardian_role_id"),
    (KanamiMemberRole.INITIATED, "initiated_role_id"),
    (KanamiMemberRole.GUEST, "guest_role_id"),
)


class AchievementReadRepository(Protocol):
    """Narrow read contract required by Profile v1."""

    async def list_unlocked(
        self, *, guild_id: int, user_id: int
    ) -> tuple[UnlockedAchievement, ...]: ...


def can_view_member_statistics(
    *,
    viewer_user_id: int,
    target_user_id: int,
    viewer_role_ids: frozenset[int],
    role_configuration: MemberRoleConfiguration,
) -> bool:
    """Allow self-view, or other-member view to configured Purple/Gold roles."""

    if viewer_user_id == target_user_id:
        return True
    privileged_ids = {
        role_id
        for role_id in (
            role_configuration.purple_role_id,
            role_configuration.gold_role_id,
        )
        if role_id is not None
    }
    return bool(privileged_ids.intersection(viewer_role_ids))


def resolve_kanami_member_role(
    role_ids: frozenset[int],
    role_configuration: MemberRoleConfiguration,
) -> KanamiMemberRole | None:
    """Resolve the highest configured Kanami role without using role names."""

    for role, field_name in _ROLE_RESOLUTION_ORDER:
        configured_id = getattr(role_configuration, field_name)
        if configured_id is not None and configured_id in role_ids:
            return role
    return None


class MemberProfileService:
    """Combine existing voice and achievement reads into one profile result."""

    def __init__(
        self,
        voice_repository: VoiceStatisticsRepository,
        achievement_repository: AchievementReadRepository,
        *,
        report_timezone: ZoneInfo,
        min_session_seconds: int,
        role_configuration: MemberRoleConfiguration,
    ) -> None:
        self._voice_service = VoiceStatisticsService(
            voice_repository,
            report_timezone=report_timezone,
            min_session_seconds=min_session_seconds,
        )
        self._achievement_repository = achievement_repository
        self._report_timezone = report_timezone
        self._role_configuration = role_configuration

    async def get_profile(
        self,
        *,
        guild_id: int,
        subject: MemberProfileSubject,
        as_of: datetime,
    ) -> MemberProfile:
        """Return one bounded profile snapshot from aggregate/read-model queries."""

        as_of = normalize_observed_at(as_of)
        voice = await self._voice_service.get_user_statistics(
            guild_id, subject.user_id, as_of
        )
        achievements = await self._achievement_repository.list_unlocked(
            guild_id=guild_id,
            user_id=subject.user_id,
        )
        joined_on = (
            subject.joined_at.astimezone(self._report_timezone).date()
            if subject.joined_at is not None
            else None
        )
        server_age_days = (
            max(0, int((as_of - subject.joined_at).total_seconds() // 86_400))
            if subject.joined_at is not None
            else None
        )
        return MemberProfile(
            as_of=as_of,
            user_id=subject.user_id,
            display_name=subject.display_name,
            avatar_url=subject.avatar_url,
            role=resolve_kanami_member_role(subject.role_ids, self._role_configuration),
            joined_on=joined_on,
            server_age_days=server_age_days,
            voice_all_time_seconds=voice.all_time.total_seconds,
            voice_last_30_days_seconds=voice.last_30_days.total_seconds,
            achievement_count=len(achievements),
            has_estimated_voice_time=voice.has_estimated_time,
        )
