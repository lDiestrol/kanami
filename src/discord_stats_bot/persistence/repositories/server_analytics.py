"""Bounded PostgreSQL reads for the server analytics foundation."""

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.server_analytics import (
    AnalyticsEarliestRecorded,
    AnalyticsQuery,
    AnalyticsTextRow,
    AnalyticsVoiceInterval,
)
from discord_stats_bot.persistence.models import (
    DailyTextActivity,
    DiscordUser,
    VoiceInterval,
)
from discord_stats_bot.persistence.repositories.voice_statistics import (
    eligible_voice_intervals,
)


def server_analytics_voice_intervals_statement(
    guild_id: int,
    query: AnalyticsQuery,
) -> Select[tuple[int, object, object, str]]:
    """Fetch eligible non-bot intervals once for the combined current/previous range."""

    effective, eligible_sessions = eligible_voice_intervals(
        guild_id,
        query,
        exclude_bots=True,
        cte_prefix="analytics",
    )
    eligible = effective.join(
        eligible_sessions,
        eligible_sessions.c.session_id == effective.c.session_id,
    )
    clipped_start = func.greatest(
        effective.c.started_at,
        query.window.previous_started_at,
    ).label("started_at")
    clipped_end = func.least(
        effective.c.effective_end,
        query.window.current_ended_at,
    ).label("ended_at")
    return (
        select(
            effective.c.user_id,
            clipped_start,
            clipped_end,
            effective.c.quality,
        )
        .select_from(eligible)
        .where(
            effective.c.effective_end > query.window.previous_started_at,
            effective.c.started_at < query.window.current_ended_at,
        )
        .order_by(
            clipped_start.asc(),
            effective.c.user_id.asc(),
            effective.c.session_id.asc(),
        )
    )


def server_analytics_text_rows_statement(
    guild_id: int,
    query: AnalyticsQuery,
) -> Select[tuple[int, object, int]]:
    """Aggregate text once per user/date across the combined date range."""

    return (
        select(
            DailyTextActivity.user_id,
            DailyTextActivity.activity_date,
            func.sum(DailyTextActivity.message_count).label("message_count"),
        )
        .join(DiscordUser, DiscordUser.id == DailyTextActivity.user_id)
        .where(
            DailyTextActivity.guild_id == guild_id,
            DailyTextActivity.activity_date >= query.window.previous_started_on,
            DailyTextActivity.activity_date < query.window.current_ended_before,
            DiscordUser.is_bot.is_(False),
        )
        .group_by(DailyTextActivity.user_id, DailyTextActivity.activity_date)
        .order_by(
            DailyTextActivity.activity_date.asc(),
            DailyTextActivity.user_id.asc(),
        )
    )


def server_analytics_earliest_recorded_statement(
    guild_id: int,
) -> Select[tuple[object, object]]:
    """Return earliest non-bot source rows without claiming monitoring coverage."""

    earliest_voice = (
        select(func.min(VoiceInterval.started_at))
        .join(DiscordUser, DiscordUser.id == VoiceInterval.user_id)
        .where(
            VoiceInterval.guild_id == guild_id,
            VoiceInterval.is_afk.is_(False),
            DiscordUser.is_bot.is_(False),
        )
        .scalar_subquery()
    )
    earliest_text = (
        select(func.min(DailyTextActivity.activity_date))
        .join(DiscordUser, DiscordUser.id == DailyTextActivity.user_id)
        .where(
            DailyTextActivity.guild_id == guild_id,
            DiscordUser.is_bot.is_(False),
        )
        .scalar_subquery()
    )
    return select(
        earliest_voice.label("voice_started_at"),
        earliest_text.label("text_activity_date"),
    )


class SqlAlchemyServerAnalyticsRepository:
    """Execute three reads on a caller-owned snapshot transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_voice_intervals(
        self,
        guild_id: int,
        query: AnalyticsQuery,
    ) -> tuple[AnalyticsVoiceInterval, ...]:
        rows = (
            await self._session.execute(
                server_analytics_voice_intervals_statement(guild_id, query)
            )
        ).all()
        return tuple(
            AnalyticsVoiceInterval(
                row.user_id,
                row.started_at,
                row.ended_at,
                row.quality,
            )
            for row in rows
        )

    async def list_text_rows(
        self,
        guild_id: int,
        query: AnalyticsQuery,
    ) -> tuple[AnalyticsTextRow, ...]:
        rows = (
            await self._session.execute(
                server_analytics_text_rows_statement(guild_id, query)
            )
        ).all()
        return tuple(
            AnalyticsTextRow(
                row.user_id,
                row.activity_date,
                int(row.message_count),
            )
            for row in rows
        )

    async def get_earliest_recorded(
        self,
        guild_id: int,
    ) -> AnalyticsEarliestRecorded:
        row = (
            await self._session.execute(
                server_analytics_earliest_recorded_statement(guild_id)
            )
        ).one()
        return AnalyticsEarliestRecorded(row.voice_started_at, row.text_activity_date)
