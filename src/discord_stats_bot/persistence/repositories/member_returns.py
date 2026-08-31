"""PostgreSQL history and idempotent outbox persistence for member returns."""

from datetime import datetime

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.member_returns import (
    MEMBER_RETURN_EVENT_TYPE,
    MemberReturnEvent,
)
from discord_stats_bot.persistence.models import AuditEvent


class SqlAlchemyMemberReturnRepository:
    """Use important audit history and the shared caller-owned transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest_member_left_at(
        self, *, guild_id: int, user_id: int, before_or_at: datetime
    ) -> datetime | None:
        statement = select(func.max(AuditEvent.occurred_at)).where(
            AuditEvent.guild_id == guild_id,
            AuditEvent.subject_id == user_id,
            AuditEvent.event_type == "member.left",
            AuditEvent.occurred_at <= before_or_at,
        )
        return (await self._session.execute(statement)).scalar_one()

    async def count_member_leaves(
        self, *, guild_id: int, user_id: int, before_or_at: datetime
    ) -> int:
        statement = select(func.count(AuditEvent.id)).where(
            AuditEvent.guild_id == guild_id,
            AuditEvent.subject_id == user_id,
            AuditEvent.event_type == "member.left",
            AuditEvent.occurred_at <= before_or_at,
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def enqueue_member_return(self, event: MemberReturnEvent) -> bool:
        statement = (
            insert(AuditEvent)
            .values(
                guild_id=event.guild_id,
                category="member",
                event_type=MEMBER_RETURN_EVENT_TYPE,
                occurred_at=event.returned_at,
                subject_type="user",
                subject_id=event.user_id,
                before_data={"left_at": event.previous_left_at.isoformat()},
                after_data={"joined_at": event.returned_at.isoformat()},
                details_data={
                    "absence_seconds": event.absence_seconds,
                    "previous_left_at": event.previous_left_at.isoformat(),
                    "returned_at": event.returned_at.isoformat(),
                    "voice_seconds": event.voice_seconds,
                    "message_count": event.message_count,
                    "achievement_count": event.achievement_count,
                    "return_number": event.return_number,
                },
                expires_at=None,
            )
            .on_conflict_do_nothing(
                index_elements=(
                    AuditEvent.guild_id,
                    AuditEvent.subject_id,
                    AuditEvent.occurred_at,
                ),
                index_where=text("event_type = 'member.returned'"),
            )
            .returning(AuditEvent.id)
        )
        result = await self._session.execute(statement)
        return result.scalar_one_or_none() is not None
