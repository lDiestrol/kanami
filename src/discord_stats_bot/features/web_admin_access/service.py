"""Application orchestration for managed Web Admin access changes."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from discord_stats_bot.features.audit_logging.service import AuditLoggingService
from discord_stats_bot.features.audit_logging.types import (
    AuditEventDraft,
    AuditEventRecord,
)
from discord_stats_bot.features.web_admin_access.types import (
    WebAdminAccessGrantRecord,
    normalize_utc_datetime,
)

WEB_ADMIN_ACCESS_GRANTED_EVENT_TYPE = "web_admin.access_granted"
WEB_ADMIN_ACCESS_REVOKED_EVENT_TYPE = "web_admin.access_revoked"
WEB_ADMIN_AUDIT_CATEGORY = "web_admin"


class WebAdminAccessMutationRepository(Protocol):
    """Mutation operations owned by the caller transaction."""

    async def grant(
        self,
        *,
        guild_id: int,
        user_id: int,
        actor_user_id: int,
        granted_at: datetime,
    ) -> WebAdminAccessGrantRecord | None: ...

    async def revoke(
        self,
        *,
        guild_id: int,
        user_id: int,
        actor_user_id: int,
        revoked_at: datetime,
    ) -> WebAdminAccessGrantRecord | None: ...


class WebAdminAccessAuditRepository(Protocol):
    """Audit persistence required for history-only access events."""

    async def create(
        self,
        draft: AuditEventDraft,
        *,
        expires_at: datetime | None,
    ) -> AuditEventRecord: ...

    async def create_many(
        self,
        events: Sequence[tuple[AuditEventDraft, datetime | None]],
    ) -> tuple[AuditEventRecord, ...]: ...

    async def mark_delivery_suppressed(
        self,
        event_ids: Sequence[int],
        suppressed_at: datetime,
    ) -> None: ...


class WebAdminAccessService:
    """Change managed access and append one history-only audit event."""

    def __init__(
        self,
        access_repository: WebAdminAccessMutationRepository,
        audit_repository: WebAdminAccessAuditRepository,
        *,
        transient_retention_days: int = 90,
    ) -> None:
        self._access_repository = access_repository
        self._audit_repository = audit_repository
        self._audit_service = AuditLoggingService(
            audit_repository,
            transient_retention_days=transient_retention_days,
        )

    async def grant(
        self,
        *,
        guild_id: int,
        user_id: int,
        actor_user_id: int,
        occurred_at: datetime,
    ) -> WebAdminAccessGrantRecord | None:
        occurred_at = normalize_utc_datetime(occurred_at, "occurred_at")
        record = await self._access_repository.grant(
            guild_id=guild_id,
            user_id=user_id,
            actor_user_id=actor_user_id,
            granted_at=occurred_at,
        )
        if record is None:
            return None
        await self._record_change(
            record=record,
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
            event_type=WEB_ADMIN_ACCESS_GRANTED_EVENT_TYPE,
            before_active=False,
            after_active=True,
        )
        return record

    async def revoke(
        self,
        *,
        guild_id: int,
        user_id: int,
        actor_user_id: int,
        occurred_at: datetime,
    ) -> WebAdminAccessGrantRecord | None:
        occurred_at = normalize_utc_datetime(occurred_at, "occurred_at")
        record = await self._access_repository.revoke(
            guild_id=guild_id,
            user_id=user_id,
            actor_user_id=actor_user_id,
            revoked_at=occurred_at,
        )
        if record is None:
            return None
        await self._record_change(
            record=record,
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
            event_type=WEB_ADMIN_ACCESS_REVOKED_EVENT_TYPE,
            before_active=True,
            after_active=False,
        )
        return record

    async def _record_change(
        self,
        *,
        record: WebAdminAccessGrantRecord,
        actor_user_id: int,
        occurred_at: datetime,
        event_type: str,
        before_active: bool,
        after_active: bool,
    ) -> None:
        audit_record = await self._audit_service.create(
            AuditEventDraft(
                guild_id=record.guild_id,
                category=WEB_ADMIN_AUDIT_CATEGORY,
                event_type=event_type,
                occurred_at=occurred_at,
                subject_type="user",
                subject_id=record.user_id,
                actor_user_id=actor_user_id,
                before_data={"managed_access": before_active},
                after_data={"managed_access": after_active},
                details_data={"grant_id": record.id},
            )
        )
        await self._audit_repository.mark_delivery_suppressed(
            (audit_record.id,),
            occurred_at,
        )
