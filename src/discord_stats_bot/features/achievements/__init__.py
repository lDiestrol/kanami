"""Discord-independent achievements foundation."""

from discord_stats_bot.features.achievements.catalog import (
    DEFAULT_ACHIEVEMENT_CATALOG,
    AchievementCatalog,
)
from discord_stats_bot.features.achievements.evaluator import AchievementEvaluator
from discord_stats_bot.features.achievements.service import (
    AchievementRepository,
    AchievementUnlockService,
)
from discord_stats_bot.features.achievements.types import (
    AchievementCandidate,
    AchievementCategory,
    AchievementDefinition,
    AchievementMetric,
    AchievementMetricSnapshot,
    AchievementTier,
    UnlockedAchievement,
)

__all__ = [
    "DEFAULT_ACHIEVEMENT_CATALOG",
    "AchievementCandidate",
    "AchievementCatalog",
    "AchievementCategory",
    "AchievementDefinition",
    "AchievementEvaluator",
    "AchievementMetric",
    "AchievementMetricSnapshot",
    "AchievementRepository",
    "AchievementTier",
    "AchievementUnlockService",
    "UnlockedAchievement",
]
