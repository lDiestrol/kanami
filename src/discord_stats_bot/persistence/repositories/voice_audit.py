"""Tracker-derived read model for durable voice audit presentation."""

from datetime import datetime

from sqlalchemy import BigInteger, and_, case, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from discord_stats_bot.features.audit_logging import VoiceAuditTransitionTiming
from discord_stats_bot.features.voice_statistics import VoiceStatisticsQuery
from discord_stats_bot.persistence.models import VoiceInterval, VoiceSession
from discord_stats_bot.persistence.repositories.voice_statistics import (
    SqlAlchemyVoiceStatisticsRepository,
)


def _seconds(started_at: object, ended_at: object) -> object:
    return cast(
        func.floor(
            func.greatest(literal(0.0), func.extract("epoch", ended_at - started_at))
        ),
        BigInteger,
    )


def voice_audit_transition_timing_statement(
    *,
    guild_id: int,
    user_id: int,
    previous_channel_id: int,
    occurred_at: datetime,
) -> object:
    """Build one query for the interval closed exactly by this transition."""

    previous = aliased(VoiceInterval, name="audit_previous_interval")
    segment = aliased(VoiceInterval, name="audit_session_segment")
    effective_end = case(
        (
            segment.ended_at.is_not(None),
            func.least(segment.ended_at, occurred_at),
        ),
        else_=func.least(VoiceSession.confirmed_through_at, occurred_at),
    )
    counted_exact_seconds = cast(
        func.floor(
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                segment.quality == "exact",
                                segment.is_afk.is_(False),
                                segment.started_at < occurred_at,
                                effective_end > segment.started_at,
                            ),
                            func.greatest(
                                literal(0.0),
                                func.extract(
                                    "epoch", effective_end - segment.started_at
                                ),
                            ),
                        ),
                        else_=literal(0.0),
                    )
                ),
                literal(0.0),
            )
        ),
        BigInteger,
    ).label("counted_exact_session_seconds")
    return (
        select(
            VoiceSession.started_at.label("session_started_at"),
            _seconds(previous.started_at, occurred_at).label(
                "previous_interval_seconds"
            ),
            _seconds(VoiceSession.started_at, occurred_at).label(
                "current_session_seconds"
            ),
            counted_exact_seconds,
            previous.is_afk.label("previous_interval_is_afk"),
        )
        .join(previous, previous.session_id == VoiceSession.id)
        .join(segment, segment.session_id == VoiceSession.id)
        .where(
            previous.guild_id == guild_id,
            previous.user_id == user_id,
            previous.channel_id == previous_channel_id,
            previous.ended_at == occurred_at,
        )
        .group_by(
            VoiceSession.id,
            VoiceSession.started_at,
            previous.id,
            previous.started_at,
            previous.is_afk,
        )
        .order_by(previous.id.desc())
        .limit(1)
    )


class SqlAlchemyVoiceAuditEnrichmentRepository:
    """Read immutable voice audit values on a caller-owned session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_transition_timing(
        self,
        *,
        guild_id: int,
        user_id: int,
        previous_channel_id: int,
        occurred_at: datetime,
    ) -> VoiceAuditTransitionTiming | None:
        result = await self._session.execute(
            voice_audit_transition_timing_statement(
                guild_id=guild_id,
                user_id=user_id,
                previous_channel_id=previous_channel_id,
                occurred_at=occurred_at,
            )
        )
        row = result.one_or_none()
        if row is None:
            return None
        return VoiceAuditTransitionTiming(
            session_started_at=row.session_started_at,
            previous_interval_seconds=int(row.previous_interval_seconds),
            current_session_seconds=int(row.current_session_seconds),
            counted_exact_session_seconds=int(row.counted_exact_session_seconds),
            previous_interval_is_afk=row.previous_interval_is_afk,
        )

    async def get_today_total_seconds(
        self,
        *,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> int:
        statistics = await SqlAlchemyVoiceStatisticsRepository(
            self._session
        ).get_user_statistics(guild_id, user_id, query)
        return statistics.today.total_seconds
