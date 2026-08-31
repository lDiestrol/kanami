"""Application orchestration for pure evaluation and idempotent persistence."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from discord_stats_bot.features.achievements.evaluator import AchievementEvaluator
from discord_stats_bot.features.achievements.types import (
    AchievementMetricSnapshot,
    UnlockedAchievement,
    normalize_utc_datetime,
)


class AchievementRepository(Protocol):
    """Caller-owned transaction contract for achievement unlock persistence."""

    async def unlock_achievements(
        self,
        *,
        guild_id: int,
        user_id: int,
        achievement_keys: Sequence[str],
        unlocked_at: datetime,
    ) -> tuple[UnlockedAchievement, ...]: ...

    async def list_unlocked(
        self, *, guild_id: int, user_id: int
    ) -> tuple[UnlockedAchievement, ...]: ...


class AchievementUnlockService:
    """Persist newly satisfied achievements without owning the transaction."""

    def __init__(
        self,
        evaluator: AchievementEvaluator,
        repository: AchievementRepository,
    ) -> None:
        self._evaluator = evaluator
        self._repository = repository

    async def evaluate_and_unlock(
        self,
        *,
        guild_id: int,
        user_id: int,
        snapshot: AchievementMetricSnapshot,
        unlocked_at: datetime,
    ) -> tuple[UnlockedAchievement, ...]:
        normalized_at = normalize_utc_datetime(unlocked_at, "unlocked_at")
        candidates = self._evaluator.evaluate(snapshot)
        return await self._repository.unlock_achievements(
            guild_id=guild_id,
            user_id=user_id,
            achievement_keys=tuple(candidate.key for candidate in candidates),
            unlocked_at=normalized_at,
        )
