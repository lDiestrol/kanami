"""SELECT-only read model for the Stage 6B.2 settings UI."""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.server_settings import (
    GuildServerSettingKey,
    GuildServerSettingsBaselines,
    GuildServerSettingSource,
    resolve_guild_server_settings,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyGuildServerSettingsRepository,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebAdminServerSettingValue:
    key: GuildServerSettingKey
    effective_value: int | None
    source: GuildServerSettingSource


class WebAdminServerSettingsReadService:
    """Resolve all four settings through one bounded SELECT-only lookup."""

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

    async def load(self) -> tuple[WebAdminServerSettingValue, ...] | None:
        try:
            async with self._session_factory() as session:
                overrides = await SqlAlchemyGuildServerSettingsRepository(
                    session
                ).get_overrides(self._guild_id)
        except Exception as error:
            logger.warning(
                "Web admin server settings lookup failed error_type=%s",
                type(error).__name__,
            )
            return None
        effective = resolve_guild_server_settings(
            self._guild_id,
            self._baselines,
            overrides,
        )
        return tuple(
            WebAdminServerSettingValue(
                key,
                effective.value_for(key),
                effective.source_for(key),
            )
            for key in GuildServerSettingKey
        )
