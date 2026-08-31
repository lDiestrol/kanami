from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from discord_stats_bot.discord.operational_health import (
    OBSERVATION_RETENTION_DAYS,
    OperationalHealthObservationRunner,
    advance_observation_schedule,
)
from discord_stats_bot.persistence.models import OperationalHealthObservation

NOW = datetime(2026, 8, 25, 12, tzinfo=UTC)


def test_fixed_cadence_does_not_accumulate_short_execution_time() -> None:
    deadline, delay = advance_observation_schedule(0.0, 5.0, 60.0)
    assert deadline == 60.0
    assert delay == 55.0

    deadline, delay = advance_observation_schedule(deadline, 65.0, 60.0)
    assert deadline == 120.0
    assert delay == 55.0


def test_fixed_cadence_skips_missed_ticks_without_catch_up_storm() -> None:
    deadline, delay = advance_observation_schedule(60.0, 190.0, 60.0)

    assert deadline == 240.0
    assert delay == 50.0


class FakeResult:
    def __init__(self, row: object) -> None:
        self._row = row

    def one(self) -> object:
        return self._row


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSession:
    def __init__(self, results: list[object | Exception]) -> None:
        self.results = results
        self.added: list[object] = []
        self.statements: list[object] = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    def begin(self) -> FakeTransaction:
        return FakeTransaction()

    def add(self, value: object) -> None:
        self.added.append(value)

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        if not self.results:
            return FakeResult(SimpleNamespace())
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeResult(result)


class FakeSessionFactory:
    def __init__(self, sessions: list[FakeSession]) -> None:
        self.sessions = sessions
        self.calls = 0

    def __call__(self) -> FakeSession:
        session = self.sessions[self.calls]
        self.calls += 1
        return session


class FakeGateway:
    def __init__(self, ready: bool) -> None:
        self.ready = ready

    def is_ready(self) -> bool:
        return self.ready


def tracking_row(
    *,
    open_sessions: int = 0,
    confirmed_at: datetime | None = None,
    confirmed_sessions: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        open_sessions=open_sessions,
        confirmed_sessions=(
            open_sessions if confirmed_sessions is None else confirmed_sessions
        ),
        oldest_confirmed_at=confirmed_at,
    )


def runner(
    factory: FakeSessionFactory,
    *,
    gateway_ready: bool = True,
    game_enabled: bool = True,
    voice_interval: int = 60,
) -> OperationalHealthObservationRunner:
    result = OperationalHealthObservationRunner(
        factory,  # type: ignore[arg-type]
        guild_id=10,
        game_tracking_enabled=game_enabled,
        voice_checkpoint_interval_seconds=voice_interval,
        game_confirm_interval_seconds=60,
        clock=lambda: NOW,
    )
    result._gateway = FakeGateway(gateway_ready)  # noqa: SLF001
    return result


@pytest.mark.asyncio
async def test_runner_persists_healthy_observation_and_retention_delete() -> None:
    collect = FakeSession([tracking_row(), tracking_row()])
    persist = FakeSession([])
    factory = FakeSessionFactory([collect, persist])

    await runner(factory).observe_once()

    assert len(persist.added) == 1
    observation = persist.added[0]
    assert isinstance(observation, OperationalHealthObservation)
    assert observation.overall_status == "healthy"
    assert observation.discord_status == "healthy"
    assert observation.postgresql_status == "healthy"
    assert observation.voice_status == "healthy"
    assert observation.game_status == "healthy"
    delete_sql = str(persist.statements[0])
    assert "DELETE FROM operational_health_observations" in delete_sql
    assert OBSERVATION_RETENTION_DAYS == 8


@pytest.mark.asyncio
async def test_runner_classifies_stale_voice_as_degraded() -> None:
    collect = FakeSession(
        [
            tracking_row(
                open_sessions=1,
                confirmed_at=NOW - timedelta(seconds=181),
            )
        ]
    )
    persist = FakeSession([])

    await runner(
        FakeSessionFactory([collect, persist]), game_enabled=False
    ).observe_once()

    observation = persist.added[0]
    assert isinstance(observation, OperationalHealthObservation)
    assert observation.overall_status == "degraded"
    assert observation.voice_status == "degraded"
    assert observation.game_status == "neutral"
    assert observation.component == "Voice Tracking"
    assert observation.reason == "checkpoint stale for 3m"


@pytest.mark.asyncio
async def test_multiple_open_voice_sessions_use_oldest_checkpoint() -> None:
    collect = FakeSession(
        [
            tracking_row(
                open_sessions=2,
                confirmed_at=NOW - timedelta(seconds=181),
            )
        ]
    )
    persist = FakeSession([])

    await runner(
        FakeSessionFactory([collect, persist]), game_enabled=False
    ).observe_once()

    observation = persist.added[0]
    assert isinstance(observation, OperationalHealthObservation)
    assert observation.voice_status == "degraded"
    sql = str(collect.statements[0])
    assert "min(voice_sessions.confirmed_through_at)" in sql
    assert "count(voice_sessions.confirmed_through_at)" in sql


@pytest.mark.asyncio
async def test_open_voice_session_without_checkpoint_degrades_fresh_peer() -> None:
    collect = FakeSession(
        [
            tracking_row(
                open_sessions=2,
                confirmed_sessions=1,
                confirmed_at=NOW - timedelta(seconds=10),
            )
        ]
    )
    persist = FakeSession([])

    await runner(
        FakeSessionFactory([collect, persist]), game_enabled=False
    ).observe_once()

    observation = persist.added[0]
    assert isinstance(observation, OperationalHealthObservation)
    assert observation.voice_status == "degraded"
    assert observation.reason == "checkpoint time unavailable"


@pytest.mark.asyncio
async def test_custom_voice_interval_does_not_mark_normal_checkpoint_stale() -> None:
    collect = FakeSession(
        [
            tracking_row(
                open_sessions=1,
                confirmed_at=NOW - timedelta(seconds=300),
            )
        ]
    )
    persist = FakeSession([])

    await runner(
        FakeSessionFactory([collect, persist]),
        game_enabled=False,
        voice_interval=120,
    ).observe_once()

    observation = persist.added[0]
    assert isinstance(observation, OperationalHealthObservation)
    assert observation.voice_status == "healthy"


@pytest.mark.asyncio
async def test_runner_classifies_stale_enabled_game_as_degraded() -> None:
    collect = FakeSession(
        [
            tracking_row(),
            tracking_row(
                open_sessions=1,
                confirmed_at=NOW - timedelta(seconds=181),
            ),
        ]
    )
    persist = FakeSession([])

    await runner(FakeSessionFactory([collect, persist])).observe_once()

    observation = persist.added[0]
    assert isinstance(observation, OperationalHealthObservation)
    assert observation.overall_status == "degraded"
    assert observation.component == "Game Tracking"


@pytest.mark.asyncio
async def test_multiple_open_game_sessions_use_oldest_checkpoint() -> None:
    collect = FakeSession(
        [
            tracking_row(),
            tracking_row(
                open_sessions=2,
                confirmed_at=NOW - timedelta(seconds=181),
            ),
        ]
    )
    persist = FakeSession([])

    await runner(FakeSessionFactory([collect, persist])).observe_once()

    observation = persist.added[0]
    assert isinstance(observation, OperationalHealthObservation)
    assert observation.game_status == "degraded"
    sql = str(collect.statements[1])
    assert "min(game_sessions.confirmed_through_at)" in sql


@pytest.mark.asyncio
async def test_runner_classifies_gateway_not_ready_as_unavailable() -> None:
    collect = FakeSession([tracking_row()])
    persist = FakeSession([])

    await runner(
        FakeSessionFactory([collect, persist]),
        gateway_ready=False,
        game_enabled=False,
    ).observe_once()

    observation = persist.added[0]
    assert isinstance(observation, OperationalHealthObservation)
    assert observation.overall_status == "unavailable"
    assert observation.discord_status == "unavailable"
    assert observation.reason == "gateway not ready"


@pytest.mark.asyncio
async def test_diagnostic_failure_attempts_safe_postgresql_unavailable_record() -> None:
    collect = FakeSession([RuntimeError("postgresql://user:secret@example/db")])
    persist = FakeSession([])

    await runner(
        FakeSessionFactory([collect, persist]), game_enabled=False
    ).observe_once()

    observation = persist.added[0]
    assert isinstance(observation, OperationalHealthObservation)
    assert observation.overall_status == "unavailable"
    assert observation.postgresql_status == "unavailable"
    assert observation.reason == "health query failed"
    assert "secret" not in observation.reason
