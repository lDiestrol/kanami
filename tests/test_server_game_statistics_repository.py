from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.persistence.repositories.server_game_statistics import (
    SqlAlchemyServerGameStatisticsRepository,
    server_game_earliest_confirmed_statement,
    server_game_sessions_statement,
)


def sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_server_game_sessions_query_is_guild_window_bounded_and_set_based() -> None:
    compiled = sql(
        server_game_sessions_statement(
            10,
            started_after=datetime(2026, 8, 1, tzinfo=UTC),
            ended_before=datetime(2026, 8, 31, tzinfo=UTC),
        )
    )

    assert "game_sessions.guild_id = 10" in compiled
    assert "game_sessions.started_at < '2026-08-31 00:00:00+00:00'" in compiled
    assert "CASE WHEN (game_sessions.ended_at IS NULL)" in compiled
    assert "game_sessions.confirmed_through_at" in compiled
    assert "2026-08-01 00:00:00+00:00" in compiled
    assert "discord_users.is_bot IS false" in compiled
    assert "guild_members.nickname" in compiled
    assert "discord_users.global_name" in compiled
    assert "discord_users.username" in compiled
    assert "GROUP BY" not in compiled


def test_server_game_coverage_query_is_one_guild_aggregate() -> None:
    compiled = sql(server_game_earliest_confirmed_statement(10))

    assert "min(game_sessions.started_at)" in compiled
    assert "game_sessions.guild_id = 10" in compiled
    assert "discord_users.is_bot IS false" in compiled
    assert "CASE WHEN (game_sessions.ended_at IS NULL)" in compiled
    assert "game_sessions.confirmed_through_at" in compiled


class FakeResult:
    def __init__(self, *, rows: tuple[object, ...] = (), scalar: object = None) -> None:
        self.rows = rows
        self.scalar = scalar

    def all(self) -> tuple[object, ...]:
        return self.rows

    def scalar_one(self) -> object:
        return self.scalar


class RecordingSession:
    def __init__(self, results: list[FakeResult]) -> None:
        self.results = results
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_repository_maps_sessions_and_earliest_in_exactly_two_reads() -> None:
    started_at = datetime(2026, 8, 20, 10, tzinfo=UTC)
    confirmed_at = datetime(2026, 8, 20, 11, tzinfo=UTC)
    session = RecordingSession(
        [
            FakeResult(
                rows=(
                    SimpleNamespace(
                        session_id=1,
                        user_id=20,
                        display_name="Persisted name",
                        game_name="Minecraft",
                        started_at=started_at,
                        confirmed_through_at=confirmed_at,
                        ended_at=None,
                    ),
                )
            ),
            FakeResult(scalar=started_at),
        ]
    )
    repository = SqlAlchemyServerGameStatisticsRepository(  # type: ignore[arg-type]
        session
    )

    rows = await repository.list_server_sessions(
        10,
        started_after=datetime(2026, 8, 1, tzinfo=UTC),
        ended_before=datetime(2026, 8, 31, tzinfo=UTC),
    )
    earliest = await repository.get_earliest_confirmed_activity(10)

    assert len(session.statements) == 2
    assert len(rows) == 1
    assert rows[0].display_name == "Persisted name"
    assert rows[0].effective_end == confirmed_at
    assert earliest == started_at
