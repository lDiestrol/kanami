"""Discord-independent member profile feature."""

from discord_stats_bot.features.member_profile.service import (
    AchievementReadRepository,
    MemberProfileService,
    can_view_member_statistics,
    resolve_kanami_member_role,
)
from discord_stats_bot.features.member_profile.types import (
    KANAMI_MEMBER_ROLE_LABELS,
    KanamiMemberRole,
    MemberProfile,
    MemberProfileSubject,
    MemberRoleConfiguration,
)

__all__ = [
    "KANAMI_MEMBER_ROLE_LABELS",
    "AchievementReadRepository",
    "KanamiMemberRole",
    "MemberProfile",
    "MemberProfileService",
    "MemberProfileSubject",
    "MemberRoleConfiguration",
    "can_view_member_statistics",
    "resolve_kanami_member_role",
]
