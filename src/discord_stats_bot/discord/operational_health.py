"""Bot-owned low-frequency operational health observation runner."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.persistence.models import (
    GameSession,
    OperationalHealthObservation,
    VoiceSession,
)

logger = logging.getLogger(__name__)
OBSERVATION_INTERVAL_SECONDS = 60
OBSERVATION_RETENTION_DAYS = 8
MINIMUM_TRACKING_STALE_THRESHOLD_SECONDS = 180
MINIMUM_GAME_STALE_THRESHOLD_SECONDS = 180
STALE_INTERVAL_MULTIPLIER = 3


class GatewayHealthSource(Protocol):
    def is_ready(self) -> bool: ...


def advance_observation_schedule(
    previous_deadline: float,
    now: float,
    interval_seconds: float,
) -> tuple[float, float]:
    """Return the next fixed cadence deadline without replaying missed ticks."""

    next_deadline = previous_deadline + interval_seconds
    if next_deadline < now:
        skipped = int((now - next_deadline) // interval_seconds) + 1
        next_deadline += skipped * interval_seconds
    return next_deadline, max(0.0, next_deadline - now)


class OperationalHealthObservationRunner:
    """Record one compact snapshot per minute and prune history beyond eight days."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        game_tracking_enabled: bool,
        voice_checkpoint_interval_seconds: int,
        game_confirm_interval_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        interval_seconds: int = OBSERVATION_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._game_tracking_enabled = game_tracking_enabled
        self._voice_stale_threshold = max(
            voice_checkpoint_interval_seconds * STALE_INTERVAL_MULTIPLIER,
            MINIMUM_TRACKING_STALE_THRESHOLD_SECONDS,
        )
        self._game_stale_threshold = max(
            game_confirm_interval_seconds * STALE_INTERVAL_MULTIPLIER,
            MINIMUM_GAME_STALE_THRESHOLD_SECONDS,
        )
        self._clock = clock
        self._interval_seconds = interval_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._gateway: GatewayHealthSource | None = None

    def start(self, gateway: GatewayHealthSource) -> None:
        self._gateway = gateway
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(), name="operational-health-observation-loop"
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        deadline = self._monotonic()
        while True:
            await self.observe_once()
            deadline, delay = advance_observation_schedule(
                deadline,
                self._monotonic(),
                self._interval_seconds,
            )
            await self._sleep(delay)

    async def observe_once(self) -> None:
        observed_at = normalize_observed_at(self._clock())
        try:
            observation = await self._collect(observed_at)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Operational health collection failed error_type=%s",
                type(error).__name__,
            )
            observation = self._postgresql_unavailable(observed_at)
        try:
            async with self._session_factory() as session, session.begin():
                session.add(observation)
                await session.execute(
                    delete(OperationalHealthObservation).where(
                        OperationalHealthObservation.guild_id == self._guild_id,
                        OperationalHealthObservation.observed_at
                        < observed_at - timedelta(days=OBSERVATION_RETENTION_DAYS),
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "Operational health observation could not be persisted error_type=%s",
                type(error).__name__,
            )

    async def _collect(self, observed_at: datetime) -> OperationalHealthObservation:
        async with self._session_factory() as session:
            voice = await self._tracking_status(
                session,
                VoiceSession,
                observed_at,
                self._voice_stale_threshold,
            )
            game = (
                await self._tracking_status(
                    session,
                    GameSession,
                    observed_at,
                    self._game_stale_threshold,
                )
                if self._game_tracking_enabled
                else ("neutral", "Game Tracking", "feature disabled")
            )

        gateway_ready = self._gateway is not None and self._gateway.is_ready()
        discord = (
            ("healthy", "Discord Gateway", "gateway ready")
            if gateway_ready
            else ("unavailable", "Discord Gateway", "gateway not ready")
        )
        statuses = (
            discord,
            ("healthy", "PostgreSQL", "health query passed"),
            voice,
            game,
        )
        overall, component, reason = self._overall(statuses)
        return OperationalHealthObservation(
            guild_id=self._guild_id,
            observed_at=observed_at,
            overall_status=overall,
            discord_status=discord[0],
            postgresql_status="healthy",
            voice_status=voice[0],
            game_status=game[0],
            component=component,
            reason=reason,
        )

    async def _tracking_status(
        self,
        session: AsyncSession,
        model: type[VoiceSession] | type[GameSession],
        observed_at: datetime,
        stale_threshold: int,
    ) -> tuple[str, str, str]:
        row = (
            await session.execute(
                select(
                    func.count(model.id).label("open_sessions"),
                    func.count(model.confirmed_through_at).label("confirmed_sessions"),
                    func.min(model.confirmed_through_at).label("oldest_confirmed_at"),
                ).where(
                    model.guild_id == self._guild_id,
                    model.ended_at.is_(None),
                )
            )
        ).one()
        component = "Game Tracking" if model is GameSession else "Voice Tracking"
        if int(row.open_sessions) == 0:
            return "healthy", component, "no active sessions"
        if int(row.confirmed_sessions) < int(row.open_sessions):
            return "degraded", component, "checkpoint time unavailable"
        confirmed_at = row.oldest_confirmed_at
        if confirmed_at is None or confirmed_at.tzinfo is None:
            return "degraded", component, "checkpoint time unavailable"
        age = max(
            0,
            int((observed_at - confirmed_at.astimezone(UTC)).total_seconds()),
        )
        if age > stale_threshold:
            return "degraded", component, f"checkpoint stale for {age // 60}m"
        return "healthy", component, "checkpoint fresh"

    @staticmethod
    def _overall(statuses: tuple[tuple[str, str, str], ...]) -> tuple[str, str, str]:
        active = tuple(item for item in statuses if item[0] != "neutral")
        for wanted in ("unavailable", "degraded"):
            for status, component, reason in active:
                if status == wanted:
                    return wanted, component, reason
        return "healthy", "System", "all observed components healthy"

    def _postgresql_unavailable(
        self, observed_at: datetime
    ) -> OperationalHealthObservation:
        gateway_ready = self._gateway is not None and self._gateway.is_ready()
        return OperationalHealthObservation(
            guild_id=self._guild_id,
            observed_at=observed_at,
            overall_status="unavailable",
            discord_status="healthy" if gateway_ready else "unavailable",
            postgresql_status="unavailable",
            voice_status="unavailable",
            game_status=("unavailable" if self._game_tracking_enabled else "neutral"),
            component="PostgreSQL",
            reason="health query failed",
        )
