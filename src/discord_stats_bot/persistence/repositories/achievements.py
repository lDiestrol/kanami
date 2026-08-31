"""PostgreSQL repository for idempotent achievement unlocks."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from discord_stats_bot.features.achievements.types import (
    UnlockedAchievement,
    normalize_utc_datetime,
    validate_achievement_key,
)
from discord_stats_bot.persistence.models import UserAchievement


def _validate_member_ids(guild_id: int, user_id: int) -> None:
    if guild_id <= 0:
        raise ValueError("guild_id must be positive")
    if user_id <= 0:
        raise ValueError("user_id must be positive")


def unlock_achievements_statement(
    *,
    guild_id: int,
    user_id: int,
    achievement_keys: tuple[str, ...],
    unlocked_at: datetime,
) -> Executable:
    """Build one atomic insert returning only rows new to this call."""

    statement = insert(UserAchievement).values(
        [
            {
                "guild_id": guild_id,
                "user_id": user_id,
                "achievement_key": key,
                "unlocked_at": unlocked_at,
            }
            for key in achievement_keys
        ]
    )
    return statement.on_conflict_do_nothing(
        index_elements=(
            UserAchievement.guild_id,
            UserAchievement.user_id,
            UserAchievement.achievement_key,
        )
    ).returning(
        UserAchievement.guild_id,
        UserAchievement.user_id,
        UserAchievement.achievement_key,
        UserAchievement.unlocked_at,
    )


def list_unlocked_statement(*, guild_id: int, user_id: int) -> Executable:
    """Build deterministic listing by first unlock time and stable key."""

    return (
        select(
            UserAchievement.guild_id,
            UserAchievement.user_id,
            UserAchievement.achievement_key,
            UserAchievement.unlocked_at,
        )
        .where(
            UserAchievement.guild_id == guild_id,
            UserAchievement.user_id == user_id,
        )
        .order_by(
            UserAchievement.unlocked_at.asc(),
            UserAchievement.achievement_key.asc(),
        )
    )


class SqlAlchemyAchievementRepository:
    """Operate atomically within a caller-owned async session/transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def unlock_achievements(
        self,
        *,
        guild_id: int,
        user_id: int,
        achievement_keys: Sequence[str],
        unlocked_at: datetime,
    ) -> tuple[UnlockedAchievement, ...]:
        _validate_member_ids(guild_id, user_id)
        normalized_at = normalize_utc_datetime(unlocked_at, "unlocked_at")
        keys = tuple(dict.fromkeys(achievement_keys))
        for key in keys:
            validate_achievement_key(key)
        if not keys:
            return ()

        result = await self._session.execute(
            unlock_achievements_statement(
                guild_id=guild_id,
                user_id=user_id,
                achievement_keys=keys,
                unlocked_at=normalized_at,
            )
        )
        inserted = {row.achievement_key: self._to_record(row) for row in result.all()}
        return tuple(inserted[key] for key in keys if key in inserted)

    async def list_unlocked(
        self, *, guild_id: int, user_id: int
    ) -> tuple[UnlockedAchievement, ...]:
        _validate_member_ids(guild_id, user_id)
        result = await self._session.execute(
            list_unlocked_statement(guild_id=guild_id, user_id=user_id)
        )
        return tuple(self._to_record(row) for row in result.all())

    @staticmethod
    def _to_record(row: object) -> UnlockedAchievement:
        return UnlockedAchievement(
            guild_id=row.guild_id,  # type: ignore[attr-defined]
            user_id=row.user_id,  # type: ignore[attr-defined]
            achievement_key=row.achievement_key,  # type: ignore[attr-defined]
            unlocked_at=row.unlocked_at,  # type: ignore[attr-defined]
        )
