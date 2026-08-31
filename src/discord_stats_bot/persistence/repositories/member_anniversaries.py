"""Persistence adapter for idempotent member anniversary enqueueing."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.member_anniversaries import (
    MEMBER_ANNIVERSARY_EVENT_TYPE,
    MemberAnniversary,
)
from discord_stats_bot.persistence.models import AuditEvent


class SqlAlchemyMemberAnniversaryRepository:
    """Enqueue anniversary events in the shared durable delivery outbox."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue_anniversaries(
        self,
        *,
        guild_id: int,
        anniversaries: Sequence[MemberAnniversary],
        occurred_at: datetime,
    ) -> int:
        if not anniversaries:
            return 0
        statement = (
            insert(AuditEvent)
            .values(
                [
                    {
                        "guild_id": guild_id,
                        "category": "member",
                        "event_type": MEMBER_ANNIVERSARY_EVENT_TYPE,
                        "occurred_at": occurred_at,
                        "subject_type": "user",
                        "subject_id": anniversary.user_id,
                        "before_data": {},
                        "after_data": {},
                        "details_data": {
                            "display_name": anniversary.display_name,
                            "years": anniversary.years,
                            "anniversary_date": (
                                anniversary.anniversary_date.isoformat()
                            ),
                        },
                        "expires_at": None,
                    }
                    for anniversary in anniversaries
                ]
            )
            .on_conflict_do_nothing(
                index_elements=(
                    AuditEvent.guild_id,
                    AuditEvent.subject_id,
                    AuditEvent.occurred_at,
                ),
                index_where=text("event_type = 'member.anniversary'"),
            )
            .returning(AuditEvent.id)
        )
        result = await self._session.execute(statement)
        return len(result.scalars().all())
