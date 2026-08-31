"""SQLAlchemy repository for durable audit history and delivery state."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.audit_logging import AuditEventDraft, AuditEventRecord
from discord_stats_bot.persistence.models import AuditEvent


class SqlAlchemyAuditEventRepository:
    """Operate on a caller-owned session without hidden transaction control."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        draft: AuditEventDraft,
        *,
        expires_at: datetime | None,
    ) -> AuditEventRecord:
        records = await self.create_many(((draft, expires_at),))
        return records[0]

    async def create_many(
        self,
        events: Sequence[tuple[AuditEventDraft, datetime | None]],
    ) -> tuple[AuditEventRecord, ...]:
        if not events:
            return ()
        models = [
            AuditEvent(
                guild_id=draft.guild_id,
                category=draft.category,
                event_type=draft.event_type,
                occurred_at=draft.occurred_at,
                subject_type=draft.subject_type,
                subject_id=draft.subject_id,
                actor_user_id=draft.actor_user_id,
                channel_id=draft.channel_id,
                before_data=dict(draft.before_data),
                after_data=dict(draft.after_data),
                details_data=dict(draft.details_data),
                expires_at=expires_at,
            )
            for draft, expires_at in events
        ]
        self._session.add_all(models)
        await self._session.flush()
        return tuple(self._to_record(model) for model in models)

    async def get_pending_delivery(
        self,
        *,
        guild_id: int,
        as_of: datetime,
        limit: int,
        event_types: Sequence[str] | None = None,
    ) -> tuple[AuditEventRecord, ...]:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if limit <= 0:
            raise ValueError("limit must be positive")
        conditions = [
            AuditEvent.guild_id == guild_id,
            AuditEvent.delivered_at.is_(None),
            or_(
                AuditEvent.next_delivery_attempt_at.is_(None),
                AuditEvent.next_delivery_attempt_at <= as_of,
            ),
        ]
        if event_types is not None:
            if not event_types:
                return ()
            conditions.append(AuditEvent.event_type.in_(event_types))
        statement = (
            select(AuditEvent)
            .where(*conditions)
            .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return tuple(self._to_record(model) for model in result.scalars())

    async def mark_delivered(
        self,
        event_id: int,
        discord_message_id: int,
        delivered_at: datetime,
    ) -> None:
        await self.mark_delivered_many((event_id,), discord_message_id, delivered_at)

    async def mark_delivery_suppressed(
        self, event_ids: Sequence[int], suppressed_at: datetime
    ) -> None:
        """Resolve history-only events without claiming a Discord message."""

        if not event_ids:
            return
        statement = (
            update(AuditEvent)
            .where(AuditEvent.id.in_(event_ids), AuditEvent.delivered_at.is_(None))
            .values(
                discord_message_id=None,
                delivered_at=suppressed_at,
                next_delivery_attempt_at=None,
                last_delivery_error=None,
            )
        )
        await self._session.execute(statement)

    async def mark_delivered_many(
        self,
        event_ids: Sequence[int],
        discord_message_id: int,
        delivered_at: datetime,
    ) -> None:
        if not event_ids:
            return
        statement = (
            update(AuditEvent)
            .where(AuditEvent.id.in_(event_ids), AuditEvent.delivered_at.is_(None))
            .values(
                discord_message_id=discord_message_id,
                delivered_at=delivered_at,
                next_delivery_attempt_at=None,
                last_delivery_error=None,
            )
        )
        await self._session.execute(statement)

    async def mark_delivery_failed(
        self,
        event_id: int,
        error: str,
        next_attempt_at: datetime,
    ) -> None:
        await self.mark_delivery_failed_many((event_id,), error, next_attempt_at)

    async def mark_delivery_failed_many(
        self,
        event_ids: Sequence[int],
        error: str,
        next_attempt_at: datetime,
    ) -> None:
        if not event_ids:
            return
        statement = (
            update(AuditEvent)
            .where(AuditEvent.id.in_(event_ids), AuditEvent.delivered_at.is_(None))
            .values(
                delivery_attempts=AuditEvent.delivery_attempts + 1,
                last_delivery_error=error[:1000],
                next_delivery_attempt_at=next_attempt_at,
            )
        )
        await self._session.execute(statement)

    async def delete_expired(self, *, as_of: datetime) -> int:
        statement = (
            delete(AuditEvent)
            .where(AuditEvent.expires_at.is_not(None), AuditEvent.expires_at <= as_of)
            .returning(AuditEvent.id)
        )
        result = await self._session.execute(statement)
        return len(result.scalars().all())

    @staticmethod
    def _to_record(model: AuditEvent) -> AuditEventRecord:
        return AuditEventRecord(
            id=model.id,
            guild_id=model.guild_id,
            category=model.category,
            event_type=model.event_type,
            occurred_at=model.occurred_at,
            created_at=model.created_at,
            subject_type=model.subject_type,
            subject_id=model.subject_id,
            actor_user_id=model.actor_user_id,
            channel_id=model.channel_id,
            before_data=dict(model.before_data),
            after_data=dict(model.after_data),
            details_data=dict(model.details_data),
            discord_message_id=model.discord_message_id,
            delivered_at=model.delivered_at,
            delivery_attempts=model.delivery_attempts,
            next_delivery_attempt_at=model.next_delivery_attempt_at,
            last_delivery_error=model.last_delivery_error,
            expires_at=model.expires_at,
        )
