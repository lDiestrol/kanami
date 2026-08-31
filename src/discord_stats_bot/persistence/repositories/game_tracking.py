"""SQLAlchemy adapter for Game Tracking transitions and batch recovery."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, Update, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.game_tracking import (
    GameSessionSlice,
    ObservedGame,
    OpenGameSession,
)
from discord_stats_bot.persistence.models import GameSession, GuildMember


def game_member_lock_statement(guild_id: int, user_id: int) -> Select[tuple[int]]:
    return (
        select(GuildMember.user_id)
        .where(
            GuildMember.guild_id == guild_id,
            GuildMember.user_id == user_id,
        )
        .with_for_update()
    )


def open_game_session_statement(
    guild_id: int,
    user_id: int,
) -> Select[tuple[GameSession]]:
    return select(GameSession).where(
        GameSession.guild_id == guild_id,
        GameSession.user_id == user_id,
        GameSession.ended_at.is_(None),
    )


def open_game_sessions_statement(
    guild_id: int, *, for_update: bool = False
) -> Select[tuple[GameSession]]:
    statement = (
        select(GameSession)
        .where(
            GameSession.guild_id == guild_id,
            GameSession.ended_at.is_(None),
        )
        .order_by(GameSession.user_id)
    )
    return statement.with_for_update() if for_update else statement


def latest_game_confirmation_statement(
    guild_id: int, user_id: int
) -> Select[tuple[datetime | None]]:
    return select(func.max(GameSession.confirmed_through_at)).where(
        GameSession.guild_id == guild_id,
        GameSession.user_id == user_id,
    )


def user_game_sessions_statement(
    guild_id: int,
    user_id: int,
    *,
    started_after: datetime | None,
    ended_before: datetime,
) -> Select[tuple[GameSession]]:
    effective_end = case(
        (GameSession.ended_at.is_(None), GameSession.confirmed_through_at),
        else_=GameSession.ended_at,
    )
    statement = select(GameSession).where(
        GameSession.guild_id == guild_id,
        GameSession.user_id == user_id,
        GameSession.started_at < ended_before,
        effective_end > GameSession.started_at,
    )
    if started_after is not None:
        statement = statement.where(effective_end > started_after)
    return statement.order_by(
        effective_end.desc(), GameSession.started_at.desc(), GameSession.id.desc()
    )


def confirm_game_session_statement(
    session_id: int,
    observed: ObservedGame,
) -> Update:
    return (
        update(GameSession)
        .where(GameSession.id == session_id, GameSession.ended_at.is_(None))
        .values(
            game_name=observed.game.name,
            application_id=observed.game.application_id,
            confirmed_through_at=observed.observed_at,
        )
    )


def close_game_session_statement(session_id: int, ended_at: datetime) -> Update:
    return (
        update(GameSession)
        .where(GameSession.id == session_id, GameSession.ended_at.is_(None))
        .values(ended_at=ended_at, confirmed_through_at=ended_at)
    )


def close_game_sessions_at_confirmation_statement(
    session_ids: Sequence[int],
) -> Update:
    return (
        update(GameSession)
        .where(GameSession.id.in_(session_ids), GameSession.ended_at.is_(None))
        .values(ended_at=GameSession.confirmed_through_at)
    )


def confirm_game_sessions_statement(
    session_ids: Sequence[int], confirmed_through_at: datetime
) -> Update:
    return (
        update(GameSession)
        .where(
            GameSession.id.in_(session_ids),
            GameSession.ended_at.is_(None),
            GameSession.confirmed_through_at < confirmed_through_at,
        )
        .values(confirmed_through_at=confirmed_through_at)
    )


def _open_state(model: GameSession) -> OpenGameSession:
    return OpenGameSession(
        model.id,
        model.guild_id,
        model.user_id,
        model.game_key,
        model.game_name,
        model.application_id,
        model.started_at,
        model.confirmed_through_at,
    )


class SqlAlchemyGameTrackingRepository:
    """Persist game intent operations without owning transaction boundaries."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_member(self, guild_id: int, user_id: int) -> bool:
        result = await self._session.execute(
            game_member_lock_statement(guild_id, user_id)
        )
        return result.scalar_one_or_none() is not None

    async def get_open_session(
        self, guild_id: int, user_id: int
    ) -> OpenGameSession | None:
        result = await self._session.execute(
            open_game_session_statement(guild_id, user_id)
        )
        model = result.scalar_one_or_none()
        return _open_state(model) if model is not None else None

    async def get_latest_confirmed_through_at(
        self, guild_id: int, user_id: int
    ) -> datetime | None:
        result = await self._session.execute(
            latest_game_confirmation_statement(guild_id, user_id)
        )
        return result.scalar_one()

    async def list_user_sessions(
        self,
        guild_id: int,
        user_id: int,
        *,
        started_after: datetime | None,
        ended_before: datetime,
    ) -> tuple[GameSessionSlice, ...]:
        result = await self._session.execute(
            user_game_sessions_statement(
                guild_id,
                user_id,
                started_after=started_after,
                ended_before=ended_before,
            )
        )
        return tuple(
            GameSessionSlice(
                model.id,
                model.game_key,
                model.game_name,
                model.started_at,
                model.confirmed_through_at,
                model.ended_at,
            )
            for model in result.scalars()
        )

    async def start_session(self, observed: ObservedGame) -> None:
        self._session.add(self._new_model(observed))
        await self._session.flush()

    async def confirm_session(
        self, session: OpenGameSession, observed: ObservedGame
    ) -> None:
        await self._session.execute(
            confirm_game_session_statement(session.session_id, observed)
        )

    async def close_session(self, session: OpenGameSession, ended_at: datetime) -> None:
        await self._session.execute(
            close_game_session_statement(session.session_id, ended_at)
        )

    async def list_open_sessions(
        self, guild_id: int, *, for_update: bool = False
    ) -> tuple[OpenGameSession, ...]:
        result = await self._session.execute(
            open_game_sessions_statement(guild_id, for_update=for_update)
        )
        return tuple(_open_state(model) for model in result.scalars())

    async def close_sessions_at_confirmation(self, session_ids: Sequence[int]) -> int:
        if not session_ids:
            return 0
        result = await self._session.execute(
            close_game_sessions_at_confirmation_statement(session_ids)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def start_sessions(self, observations: Sequence[ObservedGame]) -> int:
        if not observations:
            return 0
        self._session.add_all([self._new_model(item) for item in observations])
        await self._session.flush()
        return len(observations)

    async def confirm_sessions(
        self, session_ids: Sequence[int], confirmed_through_at: datetime
    ) -> int:
        if not session_ids:
            return 0
        result = await self._session.execute(
            confirm_game_sessions_statement(session_ids, confirmed_through_at)
        )
        return int(result.rowcount)  # type: ignore[attr-defined]

    @staticmethod
    def _new_model(observed: ObservedGame) -> GameSession:
        return GameSession(
            guild_id=observed.guild_id,
            user_id=observed.user_id,
            game_key=observed.game.key,
            game_name=observed.game.name,
            application_id=observed.game.application_id,
            started_at=observed.observed_at,
            confirmed_through_at=observed.observed_at,
            ended_at=None,
        )
