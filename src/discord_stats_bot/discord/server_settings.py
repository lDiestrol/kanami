"""Refreshable bot-process access to effective guild server settings."""

import asyncio
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.server_settings import (
    EffectiveGuildServerSettings,
    GuildServerSettingsBaselines,
    resolve_guild_server_settings,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyGuildServerSettingsRepository,
)


class GuildServerSettingsProvider(Protocol):
    async def get(self) -> EffectiveGuildServerSettings: ...


class RefreshableGuildServerSettingsProvider:
    """Cache one DB resolution and invalidate it after Bot Control writes."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        baselines: GuildServerSettingsBaselines,
    ) -> None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._baselines = baselines
        self._cached: EffectiveGuildServerSettings | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> EffectiveGuildServerSettings:
        cached = self._cached
        if cached is not None:
            return cached
        async with self._lock:
            if self._cached is None:
                async with self._session_factory() as session:
                    overrides = await SqlAlchemyGuildServerSettingsRepository(
                        session
                    ).get_overrides(self._guild_id)
                self._cached = resolve_guild_server_settings(
                    self._guild_id,
                    self._baselines,
                    overrides,
                )
            return self._cached

    async def invalidate(self) -> None:
        async with self._lock:
            self._cached = None
