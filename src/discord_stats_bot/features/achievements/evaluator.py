"""Pure achievement evaluation without persistence or runtime dependencies."""

from discord_stats_bot.features.achievements.catalog import AchievementCatalog
from discord_stats_bot.features.achievements.types import (
    AchievementCandidate,
    AchievementMetricSnapshot,
)


class AchievementEvaluator:
    """Evaluate one metric snapshot in deterministic catalog order."""

    def __init__(self, catalog: AchievementCatalog) -> None:
        self._catalog = catalog

    def evaluate(
        self, snapshot: AchievementMetricSnapshot
    ) -> tuple[AchievementCandidate, ...]:
        candidates = []
        for definition in self._catalog.definitions:
            metric_value = snapshot.value_for(definition.metric)
            if metric_value is not None and metric_value >= definition.threshold:
                candidates.append(
                    AchievementCandidate(
                        definition=definition,
                        metric_value=metric_value,
                    )
                )
        return tuple(candidates)
