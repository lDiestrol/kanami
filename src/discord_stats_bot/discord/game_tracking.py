"""Discord Presence adapter and lifecycle runners for Game Tracking."""

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.game_tracking import (
    GameActivitySnapshot,
    GameBatchRepository,
    GameCheckpointResult,
    GameCheckpointService,
    GameReconciliationResult,
    GameReconciliationService,
    GameTrackingRepository,
    GameTrackingService,
    GameTransitionResult,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyGameTrackingRepository,
)

logger = logging.getLogger(__name__)


class GamePersistenceRepository(GameTrackingRepository, GameBatchRepository, Protocol):
    pass


GameRepositoryFactory = Callable[[AsyncSession], GamePersistenceRepository]


def _activity_type_name(activity: discord.BaseActivity) -> str:
    activity_type = getattr(activity, "type", None)
    enum_name = getattr(activity_type, "name", None)
    return enum_name if isinstance(enum_name, str) else str(activity_type)


def _application_id(activity: discord.BaseActivity) -> int | None:
    raw_value = getattr(activity, "application_id", None)
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def collect_game_activity_snapshots(
    activities: Sequence[discord.BaseActivity],
) -> tuple[GameActivitySnapshot, ...]:
    """Copy only identity fields; no status, details, state or secrets."""

    return tuple(
        GameActivitySnapshot(
            activity_type=_activity_type_name(activity),
            name=getattr(activity, "name", None),
            application_id=_application_id(activity),
        )
        for activity in activities
    )


def collect_guild_game_activities(
    guild: discord.Guild,
) -> dict[int, tuple[GameActivitySnapshot, ...]]:
    return {
        member.id: collect_game_activity_snapshots(member.activities)
        for member in sorted(guild.members, key=lambda item: item.id)
        if not member.bot
    }


class GamePresenceEventHandler:
    """Persist one relevant Presence update without leaking Discord payloads."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        repository_factory: GameRepositoryFactory = SqlAlchemyGameTrackingRepository,
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._repository_factory = repository_factory

    async def handle(
        self, member: discord.Member, observed_at: datetime
    ) -> GameTransitionResult | None:
        if member.guild.id != self._guild_id or member.bot:
            return None
        observed_at = normalize_observed_at(observed_at)
        activities = collect_game_activity_snapshots(member.activities)
        try:
            async with self._session_factory.begin() as session:
                result = await GameTrackingService(
                    self._repository_factory(session)
                ).observe(
                    member.guild.id,
                    member.id,
                    activities,
                    observed_at,
                )
        except Exception:
            logger.exception(
                "Game Presence transition failed guild_id=%s user_id=%s",
                member.guild.id,
                member.id,
            )
            return None
        logger.debug(
            "Game Presence transition completed guild_id=%s user_id=%s outcome=%s",
            member.guild.id,
            member.id,
            result.value,
        )
        return result

    async def close_member(
        self, member: discord.Member, observed_at: datetime
    ) -> GameTransitionResult | None:
        if member.guild.id != self._guild_id or member.bot:
            return None
        observed_at = normalize_observed_at(observed_at)
        try:
            async with self._session_factory.begin() as session:
                return await GameTrackingService(
                    self._repository_factory(session)
                ).observe(member.guild.id, member.id, (), observed_at)
        except Exception:
            logger.exception(
                "Game session close on member leave failed guild_id=%s user_id=%s",
                member.guild.id,
                member.id,
            )
            return None


class GameStartupReconciler:
    """Reconcile cached Presence with crash-safe durable sessions in one transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository_factory: GameRepositoryFactory = SqlAlchemyGameTrackingRepository,
    ) -> None:
        self._session_factory = session_factory
        self._repository_factory = repository_factory

    async def reconcile_guild(
        self, guild: discord.Guild, reconciled_at: datetime
    ) -> GameReconciliationResult:
        activities = collect_guild_game_activities(guild)
        async with self._session_factory.begin() as session:
            return await GameReconciliationService(
                self._repository_factory(session)
            ).reconcile(guild.id, activities, reconciled_at)


class GameCheckpointRunner:
    """Confirm all matching open games with one PostgreSQL UPDATE."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository_factory: GameRepositoryFactory = SqlAlchemyGameTrackingRepository,
    ) -> None:
        self._session_factory = session_factory
        self._repository_factory = repository_factory

    async def checkpoint_guild(
        self, guild: discord.Guild, checkpointed_at: datetime
    ) -> GameCheckpointResult:
        activities = collect_guild_game_activities(guild)
        async with self._session_factory.begin() as session:
            return await GameCheckpointService(
                self._repository_factory(session)
            ).checkpoint(guild.id, activities, checkpointed_at)
