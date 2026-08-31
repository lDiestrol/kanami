"""Bounded set-based PostgreSQL reads for Server Game Analytics."""

from datetime import datetime

from sqlalchemy import Select, Text, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.game_tracking import ServerGameSessionSlice
from discord_stats_bot.persistence.models import DiscordUser, GameSession, GuildMember


def server_game_sessions_statement(
    guild_id: int,
    *,
    started_after: datetime,
    ended_before: datetime,
) -> Select:
    """Fetch only confirmed non-bot sessions intersecting one report window."""

    effective_end = case(
        (GameSession.ended_at.is_(None), GameSession.confirmed_through_at),
        else_=GameSession.ended_at,
    )
    display_name = func.coalesce(
        func.nullif(GuildMember.nickname, ""),
        func.nullif(DiscordUser.global_name, ""),
        func.nullif(DiscordUser.username, ""),
        cast(DiscordUser.id, Text),
    ).label("display_name")
    return (
        select(
            GameSession.id.label("session_id"),
            GameSession.user_id,
            display_name,
            GameSession.game_name,
            GameSession.started_at,
            GameSession.confirmed_through_at,
            GameSession.ended_at,
        )
        .join(
            GuildMember,
            (GuildMember.guild_id == GameSession.guild_id)
            & (GuildMember.user_id == GameSession.user_id),
        )
        .join(DiscordUser, DiscordUser.id == GameSession.user_id)
        .where(
            GameSession.guild_id == guild_id,
            DiscordUser.is_bot.is_(False),
            GameSession.started_at < ended_before,
            effective_end > started_after,
            effective_end > GameSession.started_at,
        )
        .order_by(
            effective_end.asc(),
            GameSession.started_at.asc(),
            GameSession.id.asc(),
        )
    )


def server_game_earliest_confirmed_statement(guild_id: int) -> Select:
    """Find the earliest positive confirmed activity without claiming coverage."""

    effective_end = case(
        (GameSession.ended_at.is_(None), GameSession.confirmed_through_at),
        else_=GameSession.ended_at,
    )
    return (
        select(func.min(GameSession.started_at).label("started_at"))
        .join(DiscordUser, DiscordUser.id == GameSession.user_id)
        .where(
            GameSession.guild_id == guild_id,
            DiscordUser.is_bot.is_(False),
            effective_end > GameSession.started_at,
        )
    )


class SqlAlchemyServerGameStatisticsRepository:
    """Execute two set-based reads on a caller-owned snapshot transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_server_sessions(
        self,
        guild_id: int,
        *,
        started_after: datetime,
        ended_before: datetime,
    ) -> tuple[ServerGameSessionSlice, ...]:
        rows = (
            await self._session.execute(
                server_game_sessions_statement(
                    guild_id,
                    started_after=started_after,
                    ended_before=ended_before,
                )
            )
        ).all()
        return tuple(
            ServerGameSessionSlice(
                row.session_id,
                row.user_id,
                str(row.display_name),
                row.game_name,
                row.started_at,
                row.confirmed_through_at,
                row.ended_at,
            )
            for row in rows
        )

    async def get_earliest_confirmed_activity(
        self,
        guild_id: int,
    ) -> datetime | None:
        result = await self._session.execute(
            server_game_earliest_confirmed_statement(guild_id)
        )
        return result.scalar_one()
