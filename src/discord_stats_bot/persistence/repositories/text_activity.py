"""PostgreSQL persistence for daily text activity aggregates."""

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.text_activity import TextUserMessageCount
from discord_stats_bot.persistence.models import DailyTextActivity, DiscordUser


class SqlAlchemyTextActivityRepository:
    """Record and read aggregates without owning the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_message(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        activity_date: date,
        attachment_count: int,
        is_reply: bool,
    ) -> None:
        """Atomically add one message using the aggregate natural key."""

        statement = insert(DailyTextActivity).values(
            guild_id=guild_id,
            user_id=user_id,
            channel_id=channel_id,
            activity_date=activity_date,
            message_count=1,
            attachment_count=attachment_count,
            reply_count=int(is_reply),
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    DailyTextActivity.guild_id,
                    DailyTextActivity.user_id,
                    DailyTextActivity.channel_id,
                    DailyTextActivity.activity_date,
                ],
                set_={
                    "message_count": DailyTextActivity.message_count + 1,
                    "attachment_count": (
                        DailyTextActivity.attachment_count
                        + statement.excluded.attachment_count
                    ),
                    "reply_count": (
                        DailyTextActivity.reply_count + statement.excluded.reply_count
                    ),
                },
            )
        )

    async def get_user_message_counts(
        self,
        guild_id: int,
        started_on: date | None,
        ended_on: date,
        *,
        user_ids: tuple[int, ...] | None = None,
        limit: int | None = None,
    ) -> tuple[TextUserMessageCount, ...]:
        """Return per-user totals for an inclusive reporting-date range."""

        if started_on is not None and ended_on < started_on:
            raise ValueError("ended_on must not be earlier than started_on")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")

        statement = (
            select(
                DailyTextActivity.user_id,
                func.sum(DailyTextActivity.message_count).label("message_count"),
            )
            .join(DiscordUser, DiscordUser.id == DailyTextActivity.user_id)
            .where(
                DailyTextActivity.guild_id == guild_id,
                DailyTextActivity.activity_date <= ended_on,
                DiscordUser.is_bot.is_(False),
            )
            .group_by(DailyTextActivity.user_id)
            .order_by(
                func.sum(DailyTextActivity.message_count).desc(),
                DailyTextActivity.user_id.asc(),
            )
        )
        if started_on is not None:
            statement = statement.where(DailyTextActivity.activity_date >= started_on)
        if user_ids is not None:
            if not user_ids:
                return ()
            statement = statement.where(DailyTextActivity.user_id.in_(user_ids))
        if limit is not None:
            statement = statement.limit(limit)

        rows = (await self._session.execute(statement)).all()
        return tuple(
            TextUserMessageCount(
                user_id=row.user_id,
                message_count=int(row.message_count),
            )
            for row in rows
        )
