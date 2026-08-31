"""Immutable code-defined catalog for the initial Kanami achievements."""

from dataclasses import dataclass

from discord_stats_bot.features.achievements.types import (
    AchievementCategory,
    AchievementDefinition,
    AchievementMetric,
    AchievementTier,
)


@dataclass(frozen=True, slots=True)
class AchievementCatalog:
    """Ordered, unique achievement definitions used by the evaluator."""

    definitions: tuple[AchievementDefinition, ...]

    def __post_init__(self) -> None:
        keys = tuple(definition.key for definition in self.definitions)
        if len(keys) != len(set(keys)):
            raise ValueError("achievement catalog keys must be unique")

    def get(self, key: str) -> AchievementDefinition | None:
        """Return a known definition without rejecting unknown persisted keys."""

        return next(
            (definition for definition in self.definitions if definition.key == key),
            None,
        )


DEFAULT_ACHIEVEMENT_CATALOG = AchievementCatalog(
    definitions=(
        AchievementDefinition(
            key="voice_10_hours",
            title="В эфире",
            description="Провести 10 часов в голосовых каналах.",
            category=AchievementCategory.VOICE,
            metric=AchievementMetric.VOICE_SECONDS,
            threshold=10 * 60 * 60,
            tier=AchievementTier.BRONZE,
        ),
        AchievementDefinition(
            key="voice_50_hours",
            title="Завсегдатай",
            description="Провести 50 часов в голосовых каналах.",
            category=AchievementCategory.VOICE,
            metric=AchievementMetric.VOICE_SECONDS,
            threshold=50 * 60 * 60,
            tier=AchievementTier.SILVER,
        ),
        AchievementDefinition(
            key="voice_100_hours",
            title="Голос сервера",
            description="Провести 100 часов в голосовых каналах.",
            category=AchievementCategory.VOICE,
            metric=AchievementMetric.VOICE_SECONDS,
            threshold=100 * 60 * 60,
            tier=AchievementTier.GOLD,
        ),
        AchievementDefinition(
            key="server_age_30_days",
            title="Освоился",
            description="Провести 30 дней на сервере.",
            category=AchievementCategory.COMMUNITY,
            metric=AchievementMetric.SERVER_AGE_DAYS,
            threshold=30,
            tier=AchievementTier.BRONZE,
        ),
        AchievementDefinition(
            key="server_age_365_days",
            title="Старожил",
            description="Провести 365 дней на сервере.",
            category=AchievementCategory.COMMUNITY,
            metric=AchievementMetric.SERVER_AGE_DAYS,
            threshold=365,
            tier=AchievementTier.GOLD,
        ),
    )
)
