"""SQLAlchemy persistence operations for voice transitions and reconciliation."""

from datetime import datetime

from sqlalchemy import Select, Update, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.voice.types import ObservedVoiceState, OpenVoiceState
from discord_stats_bot.persistence.models import (
    GuildMember,
    VoiceInterval,
    VoiceSession,
)


class VoicePersistenceInvariantError(RuntimeError):
    """Raised when persisted open voice state violates service invariants."""


def member_lock_statement(guild_id: int, user_id: int) -> Select[tuple[int]]:
    """Build the PostgreSQL row-lock statement used to serialize one member."""

    return (
        select(GuildMember.user_id)
        .where(
            GuildMember.guild_id == guild_id,
            GuildMember.user_id == user_id,
        )
        .with_for_update()
    )


def open_session_statement(
    guild_id: int,
    user_id: int,
) -> Select[tuple[int, datetime]]:
    return select(
        VoiceSession.id.label("session_id"),
        VoiceSession.confirmed_through_at,
    ).where(
        VoiceSession.guild_id == guild_id,
        VoiceSession.user_id == user_id,
        VoiceSession.ended_at.is_(None),
    )


def open_interval_statement(
    session_id: int,
) -> Select[tuple[int, int, str, bool]]:
    return select(
        VoiceInterval.id.label("interval_id"),
        VoiceInterval.channel_id,
        VoiceInterval.channel_kind,
        VoiceInterval.is_afk,
    ).where(
        VoiceInterval.session_id == session_id,
        VoiceInterval.ended_at.is_(None),
    )


def latest_confirmation_statement(
    guild_id: int,
    user_id: int,
) -> Select[tuple[datetime | None]]:
    """Build the last trustworthy voice boundary query for closed history."""

    return select(func.max(VoiceSession.confirmed_through_at)).where(
        VoiceSession.guild_id == guild_id,
        VoiceSession.user_id == user_id,
    )


def open_user_ids_statement(guild_id: int) -> Select[tuple[int]]:
    """Build the query used to find users requiring startup reconciliation."""

    return (
        select(VoiceSession.user_id)
        .where(
            VoiceSession.guild_id == guild_id,
            VoiceSession.ended_at.is_(None),
        )
        .order_by(VoiceSession.user_id)
    )


def advance_confirmation_statement(
    session_id: int,
    observed_at: datetime,
) -> Update:
    return (
        update(VoiceSession)
        .where(
            VoiceSession.id == session_id,
            VoiceSession.ended_at.is_(None),
        )
        .values(confirmed_through_at=observed_at)
    )


def close_interval_statement(interval_id: int, observed_at: datetime) -> Update:
    return (
        update(VoiceInterval)
        .where(
            VoiceInterval.id == interval_id,
            VoiceInterval.ended_at.is_(None),
        )
        .values(ended_at=observed_at)
    )


def close_session_statement(session_id: int, observed_at: datetime) -> Update:
    return (
        update(VoiceSession)
        .where(
            VoiceSession.id == session_id,
            VoiceSession.ended_at.is_(None),
        )
        .values(
            ended_at=observed_at,
            confirmed_through_at=observed_at,
        )
    )


class SqlAlchemyVoiceTransitionRepository:
    """Persist transitions without owning the session or committing its transaction.

    The caller must provision the referenced guild member and voice channel, then run
    the service call inside one transaction on the supplied ``AsyncSession``.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_member(self, guild_id: int, user_id: int) -> bool:
        result = await self._session.execute(member_lock_statement(guild_id, user_id))
        return result.scalar_one_or_none() is not None

    async def get_open_state(
        self,
        guild_id: int,
        user_id: int,
    ) -> OpenVoiceState | None:
        session_result = await self._session.execute(
            open_session_statement(guild_id, user_id)
        )
        session_row = session_result.one_or_none()
        if session_row is None:
            return None

        interval_result = await self._session.execute(
            open_interval_statement(session_row.session_id)
        )
        interval_row = interval_result.one_or_none()
        if interval_row is None:
            raise VoicePersistenceInvariantError(
                f"open voice session {session_row.session_id} has no open interval"
            )

        return OpenVoiceState(
            session_id=session_row.session_id,
            interval_id=interval_row.interval_id,
            confirmed_through_at=session_row.confirmed_through_at,
            channel_id=interval_row.channel_id,
            channel_kind=interval_row.channel_kind,
            is_afk=interval_row.is_afk,
        )

    async def get_latest_confirmed_through_at(
        self,
        guild_id: int,
        user_id: int,
    ) -> datetime | None:
        result = await self._session.execute(
            latest_confirmation_statement(guild_id, user_id)
        )
        return result.scalar_one()

    async def list_open_user_ids(self, guild_id: int) -> tuple[int, ...]:
        result = await self._session.execute(open_user_ids_statement(guild_id))
        return tuple(result.scalars())

    async def create_open_state(
        self,
        observed: ObservedVoiceState,
        *,
        quality: str,
    ) -> None:
        voice_session = VoiceSession(
            guild_id=observed.guild_id,
            user_id=observed.user_id,
            started_at=observed.observed_at,
            ended_at=None,
            confirmed_through_at=observed.observed_at,
        )
        self._session.add(voice_session)
        await self._session.flush()
        if voice_session.id is None:
            raise VoicePersistenceInvariantError(
                "database did not assign the voice session identity"
            )

        self._session.add(
            VoiceInterval(
                session_id=voice_session.id,
                guild_id=observed.guild_id,
                user_id=observed.user_id,
                channel_id=observed.channel_id,
                started_at=observed.observed_at,
                ended_at=None,
                quality=quality,
                channel_kind=observed.channel_kind,
                is_afk=observed.is_afk,
            )
        )
        await self._session.flush()

    async def advance_confirmation(
        self,
        state: OpenVoiceState,
        observed_at: datetime,
    ) -> None:
        await self._session.execute(
            advance_confirmation_statement(state.session_id, observed_at)
        )

    async def move_open_interval(
        self,
        state: OpenVoiceState,
        observed: ObservedVoiceState,
        *,
        quality: str,
    ) -> None:
        await self._session.execute(
            close_interval_statement(state.interval_id, observed.observed_at)
        )
        await self._session.execute(
            advance_confirmation_statement(state.session_id, observed.observed_at)
        )
        self._session.add(
            VoiceInterval(
                session_id=state.session_id,
                guild_id=observed.guild_id,
                user_id=observed.user_id,
                channel_id=observed.channel_id,
                started_at=observed.observed_at,
                ended_at=None,
                quality=quality,
                channel_kind=observed.channel_kind,
                is_afk=observed.is_afk,
            )
        )
        await self._session.flush()

    async def reconcile_same_snapshot(
        self,
        state: OpenVoiceState,
        observed: ObservedVoiceState,
        *,
        exact_quality: str,
        estimated_quality: str,
    ) -> None:
        """Replace an unconfirmed tail with estimated downtime and a new exact tail."""

        await self._session.execute(
            close_interval_statement(state.interval_id, state.confirmed_through_at)
        )
        self._session.add(
            VoiceInterval(
                session_id=state.session_id,
                guild_id=observed.guild_id,
                user_id=observed.user_id,
                channel_id=state.channel_id,
                started_at=state.confirmed_through_at,
                ended_at=observed.observed_at,
                quality=estimated_quality,
                channel_kind=state.channel_kind,
                is_afk=state.is_afk,
            )
        )
        self._session.add(
            VoiceInterval(
                session_id=state.session_id,
                guild_id=observed.guild_id,
                user_id=observed.user_id,
                channel_id=observed.channel_id,
                started_at=observed.observed_at,
                ended_at=None,
                quality=exact_quality,
                channel_kind=observed.channel_kind,
                is_afk=observed.is_afk,
            )
        )
        await self._session.execute(
            advance_confirmation_statement(state.session_id, observed.observed_at)
        )
        await self._session.flush()

    async def close_open_state(
        self,
        state: OpenVoiceState,
        observed_at: datetime,
    ) -> None:
        await self._session.execute(
            close_interval_statement(state.interval_id, observed_at)
        )
        await self._session.execute(
            close_session_statement(state.session_id, observed_at)
        )
