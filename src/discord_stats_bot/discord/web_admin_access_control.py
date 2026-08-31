"""Transactional Bot Control adapter for managed Web Admin access."""

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.web_admin_access import WebAdminAccessService
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyAuditEventRepository,
    SqlAlchemyWebAdminAccessRepository,
)


class WebAdminAccessControlService:
    """Own the transaction for one managed access mutation plus its audit."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._clock = clock

    async def grant_access(
        self,
        user_id: int,
        *,
        actor_discord_user_id: int,
    ) -> bool:
        return await self._change(
            "grant",
            user_id=user_id,
            actor_discord_user_id=actor_discord_user_id,
        )

    async def revoke_access(
        self,
        user_id: int,
        *,
        actor_discord_user_id: int,
    ) -> bool:
        return await self._change(
            "revoke",
            user_id=user_id,
            actor_discord_user_id=actor_discord_user_id,
        )

    async def _change(
        self,
        operation: str,
        *,
        user_id: int,
        actor_discord_user_id: int,
    ) -> bool:
        occurred_at = self._clock()
        async with self._session_factory.begin() as session:
            service = WebAdminAccessService(
                SqlAlchemyWebAdminAccessRepository(session),
                SqlAlchemyAuditEventRepository(session),
            )
            if operation == "grant":
                record = await service.grant(
                    guild_id=self._guild_id,
                    user_id=user_id,
                    actor_user_id=actor_discord_user_id,
                    occurred_at=occurred_at,
                )
            elif operation == "revoke":
                record = await service.revoke(
                    guild_id=self._guild_id,
                    user_id=user_id,
                    actor_user_id=actor_discord_user_id,
                    occurred_at=occurred_at,
                )
            else:
                raise ValueError(f"unsupported access operation: {operation}")
        return record is not None
