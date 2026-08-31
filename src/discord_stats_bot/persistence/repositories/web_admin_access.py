"""PostgreSQL repository for managed Web Admin access grants."""

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from discord_stats_bot.features.web_admin_access import (
    WebAdminAccessGrantRecord,
    normalize_utc_datetime,
)
from discord_stats_bot.persistence.models import WebAdminAccessGrant


def _validate_ids(
    guild_id: int, user_id: int, actor_user_id: int | None = None
) -> None:
    if guild_id <= 0:
        raise ValueError("guild_id must be positive")
    if user_id <= 0:
        raise ValueError("user_id must be positive")
    if actor_user_id is not None and actor_user_id <= 0:
        raise ValueError("actor_user_id must be positive")


def grant_access_statement(
    *,
    guild_id: int,
    user_id: int,
    actor_user_id: int,
    granted_at: datetime,
) -> Executable:
    """Insert one active grant unless one already exists."""
    statement = insert(WebAdminAccessGrant).values(
        guild_id=guild_id,
        user_id=user_id,
        granted_by_user_id=actor_user_id,
        granted_at=granted_at,
    )
    return statement.on_conflict_do_nothing(
        index_elements=(
            WebAdminAccessGrant.guild_id,
            WebAdminAccessGrant.user_id,
        ),
        index_where=WebAdminAccessGrant.revoked_at.is_(None),
    ).returning(WebAdminAccessGrant)


def revoke_access_statement(
    *,
    guild_id: int,
    user_id: int,
    actor_user_id: int,
    revoked_at: datetime,
) -> Executable:
    """Close the current active grant, if one exists."""
    return (
        update(WebAdminAccessGrant)
        .where(
            WebAdminAccessGrant.guild_id == guild_id,
            WebAdminAccessGrant.user_id == user_id,
            WebAdminAccessGrant.revoked_at.is_(None),
        )
        .values(
            revoked_by_user_id=actor_user_id,
            revoked_at=revoked_at,
        )
        .returning(WebAdminAccessGrant)
    )


class SqlAlchemyWebAdminAccessRepository:
    """Operate on caller-owned sessions without hidden transaction control."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def is_active_admin(self, *, guild_id: int, user_id: int) -> bool:
        _validate_ids(guild_id, user_id)
        statement = (
            select(WebAdminAccessGrant.id)
            .where(
                WebAdminAccessGrant.guild_id == guild_id,
                WebAdminAccessGrant.user_id == user_id,
                WebAdminAccessGrant.revoked_at.is_(None),
            )
            .limit(1)
        )
        return (await self._session.scalar(statement)) is not None

    async def list_active(
        self, *, guild_id: int
    ) -> tuple[WebAdminAccessGrantRecord, ...]:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        statement = (
            select(WebAdminAccessGrant)
            .where(
                WebAdminAccessGrant.guild_id == guild_id,
                WebAdminAccessGrant.revoked_at.is_(None),
            )
            .order_by(
                WebAdminAccessGrant.granted_at.asc(),
                WebAdminAccessGrant.id.asc(),
            )
        )
        result = await self._session.scalars(statement)
        return tuple(self._to_record(model) for model in result.all())

    async def grant(
        self,
        *,
        guild_id: int,
        user_id: int,
        actor_user_id: int,
        granted_at: datetime,
    ) -> WebAdminAccessGrantRecord | None:
        _validate_ids(guild_id, user_id, actor_user_id)
        normalized_at = normalize_utc_datetime(granted_at, "granted_at")
        result = await self._session.scalars(
            grant_access_statement(
                guild_id=guild_id,
                user_id=user_id,
                actor_user_id=actor_user_id,
                granted_at=normalized_at,
            )
        )
        model = result.one_or_none()
        return None if model is None else self._to_record(model)

    async def revoke(
        self,
        *,
        guild_id: int,
        user_id: int,
        actor_user_id: int,
        revoked_at: datetime,
    ) -> WebAdminAccessGrantRecord | None:
        _validate_ids(guild_id, user_id, actor_user_id)
        normalized_at = normalize_utc_datetime(revoked_at, "revoked_at")
        result = await self._session.scalars(
            revoke_access_statement(
                guild_id=guild_id,
                user_id=user_id,
                actor_user_id=actor_user_id,
                revoked_at=normalized_at,
            )
        )
        model = result.one_or_none()
        return None if model is None else self._to_record(model)

    @staticmethod
    def _to_record(model: WebAdminAccessGrant) -> WebAdminAccessGrantRecord:
        return WebAdminAccessGrantRecord(
            id=model.id,
            guild_id=model.guild_id,
            user_id=model.user_id,
            granted_by_user_id=model.granted_by_user_id,
            granted_at=model.granted_at,
            revoked_by_user_id=model.revoked_by_user_id,
            revoked_at=model.revoked_at,
        )
