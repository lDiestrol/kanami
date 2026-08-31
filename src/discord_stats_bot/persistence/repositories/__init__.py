"""SQLAlchemy repositories operating on caller-owned sessions."""

from discord_stats_bot.persistence.repositories.achievements import (
    SqlAlchemyAchievementRepository,
)
from discord_stats_bot.persistence.repositories.audit_events import (
    SqlAlchemyAuditEventRepository,
)
from discord_stats_bot.persistence.repositories.game_tracking import (
    SqlAlchemyGameTrackingRepository,
)
from discord_stats_bot.persistence.repositories.member_analytics import (
    SqlAlchemyMemberAnalyticsRepository,
)
from discord_stats_bot.persistence.repositories.member_anniversaries import (
    SqlAlchemyMemberAnniversaryRepository,
)
from discord_stats_bot.persistence.repositories.member_returns import (
    SqlAlchemyMemberReturnRepository,
)
from discord_stats_bot.persistence.repositories.reference_data import (
    SqlAlchemyReferenceDataRepository,
)
from discord_stats_bot.persistence.repositories.rules import SqlAlchemyRulesRepository
from discord_stats_bot.persistence.repositories.rules_publication import (
    SqlAlchemyRulesPublicationRepository,
)
from discord_stats_bot.persistence.repositories.server_analytics import (
    SqlAlchemyServerAnalyticsRepository,
)
from discord_stats_bot.persistence.repositories.server_game_statistics import (
    SqlAlchemyServerGameStatisticsRepository,
)
from discord_stats_bot.persistence.repositories.server_settings import (
    SqlAlchemyGuildServerSettingsRepository,
)
from discord_stats_bot.persistence.repositories.text_activity import (
    SqlAlchemyTextActivityRepository,
)
from discord_stats_bot.persistence.repositories.voice import (
    SqlAlchemyVoiceTransitionRepository,
    VoicePersistenceInvariantError,
)
from discord_stats_bot.persistence.repositories.voice_audit import (
    SqlAlchemyVoiceAuditEnrichmentRepository,
)
from discord_stats_bot.persistence.repositories.voice_statistics import (
    SqlAlchemyVoiceStatisticsRepository,
)
from discord_stats_bot.persistence.repositories.web_admin_access import (
    SqlAlchemyWebAdminAccessRepository,
)

__all__ = [
    "SqlAlchemyAuditEventRepository",
    "SqlAlchemyGameTrackingRepository",
    "SqlAlchemyServerGameStatisticsRepository",
    "SqlAlchemyAchievementRepository",
    "SqlAlchemyMemberAnalyticsRepository",
    "SqlAlchemyMemberAnniversaryRepository",
    "SqlAlchemyMemberReturnRepository",
    "SqlAlchemyGuildServerSettingsRepository",
    "SqlAlchemyServerAnalyticsRepository",
    "SqlAlchemyReferenceDataRepository",
    "SqlAlchemyRulesRepository",
    "SqlAlchemyRulesPublicationRepository",
    "SqlAlchemyTextActivityRepository",
    "SqlAlchemyVoiceTransitionRepository",
    "SqlAlchemyVoiceStatisticsRepository",
    "SqlAlchemyVoiceAuditEnrichmentRepository",
    "SqlAlchemyWebAdminAccessRepository",
    "VoicePersistenceInvariantError",
]
