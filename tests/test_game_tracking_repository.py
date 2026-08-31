import ast
import inspect
import os
import textwrap
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.config import Settings
from discord_stats_bot.features.game_tracking import ObservedGame, TrackedGame
from discord_stats_bot.persistence.database import create_database_resources
from discord_stats_bot.persistence.models import GameSession
from discord_stats_bot.persistence.repositories.game_tracking import (
    SqlAlchemyGameTrackingRepository,
    close_game_session_statement,
    close_game_sessions_at_confirmation_statement,
    confirm_game_session_statement,
    confirm_game_sessions_statement,
    game_member_lock_statement,
    latest_game_confirmation_statement,
    open_game_session_statement,
    open_game_sessions_statement,
    user_game_sessions_statement,
)

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def compile_postgresql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def observed(name: str = "Minecraft", application_id: int | None = 123) -> ObservedGame:
    key = (
        f"application:{application_id}"
        if application_id is not None
        else f"name:{name.casefold()}"
    )
    return ObservedGame(10, 20, TrackedGame(key, name, application_id), NOW)


def test_game_queries_are_bounded_locked_and_set_based() -> None:
    statements = (
        game_member_lock_statement(10, 20),
        open_game_session_statement(10, 20),
        open_game_sessions_statement(10, for_update=True),
        latest_game_confirmation_statement(10, 20),
        confirm_game_session_statement(1, observed()),
        close_game_session_statement(1, NOW),
        close_game_sessions_at_confirmation_statement((1, 2)),
        confirm_game_sessions_statement((1, 2), NOW),
        user_game_sessions_statement(
            10,
            20,
            started_after=NOW - timedelta(days=30),
            ended_before=NOW,
        ),
    )
    sql = "\n".join(compile_postgresql(item) for item in statements)

    assert "guild_members" in sql
    assert "game_sessions" in sql
    assert "FOR UPDATE" in sql
    assert "ended_at IS NULL" in sql
    assert "max(game_sessions.confirmed_through_at)" in sql
    assert "game_sessions.id IN" in sql
    assert "CASE WHEN" in sql
    assert "game_sessions.started_at <" in sql


def test_game_model_has_history_and_partial_unique_open_indexes() -> None:
    table = GameSession.__table__
    indexes = {index.name: index for index in table.indexes}

    assert set(indexes) == {
        "ix_game_sessions_guild_started_at",
        "ix_game_sessions_guild_user_started_at",
        "uq_game_sessions_open_guild_user",
    }
    open_index = indexes["uq_game_sessions_open_guild_user"]
    assert open_index.unique
    assert tuple(column.name for column in open_index.columns) == (
        "guild_id",
        "user_id",
    )
    assert str(open_index.dialect_options["postgresql"]["where"]) == (
        "ended_at IS NULL"
    )


def test_game_repository_has_no_hidden_transaction_control() -> None:
    source = textwrap.dedent(inspect.getsource(SqlAlchemyGameTrackingRepository))
    tree = ast.parse(source)

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"begin", "commit", "rollback"}
    ]

    assert calls == []


class WriteResult:
    rowcount = 2


class RecordingSession:
    def __init__(self) -> None:
        self.executed: list[object] = []
        self.added: list[object] = []
        self.flush_count = 0

    async def execute(self, statement: object) -> WriteResult:
        self.executed.append(statement)
        return WriteResult()

    def add(self, value: object) -> None:
        self.added.append(value)

    def add_all(self, values: list[object]) -> None:
        self.added.extend(values)

    async def flush(self) -> None:
        self.flush_count += 1


@pytest.mark.asyncio
async def test_repository_start_and_batch_checkpoint_write_minimal_rows() -> None:
    session = RecordingSession()
    repository = SqlAlchemyGameTrackingRepository(session)  # type: ignore[arg-type]

    await repository.start_session(observed())
    confirmed = await repository.confirm_sessions((1, 2), NOW + timedelta(minutes=1))

    assert session.flush_count == 1
    assert len(session.added) == 1
    model = session.added[0]
    assert isinstance(model, GameSession)
    assert model.game_name == "Minecraft"
    assert model.application_id == 123
    assert model.started_at == NOW
    assert model.confirmed_through_at == NOW
    assert model.ended_at is None
    assert confirmed == 2
    assert len(session.executed) == 1
    assert "UPDATE game_sessions" in compile_postgresql(session.executed[0])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgresql_game_session_unique_switch_and_rollback() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="integration-test-placeholder",
        DISCORD_GUILD_ID=10,
        DATABASE_URL=database_url,
    )
    resources = create_database_resources(settings)
    try:
        async with resources.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE guild_members ("
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "PRIMARY KEY (guild_id, user_id))"
                    )
                )
                await connection.execute(
                    text("INSERT INTO guild_members VALUES (10, 20)")
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE game_sessions ("
                        "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "game_key TEXT NOT NULL, game_name TEXT NOT NULL, "
                        "application_id BIGINT, started_at TIMESTAMPTZ NOT NULL, "
                        "confirmed_through_at TIMESTAMPTZ NOT NULL, "
                        "ended_at TIMESTAMPTZ)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE UNIQUE INDEX uq_game_sessions_open_guild_user "
                        "ON game_sessions (guild_id, user_id) "
                        "WHERE ended_at IS NULL"
                    )
                )
                session = AsyncSession(bind=connection, expire_on_commit=False)
                repository = SqlAlchemyGameTrackingRepository(session)
                await repository.start_session(observed())
                current = await repository.get_open_session(10, 20)
                assert current is not None
                assert current.started_at == NOW
                await repository.close_session(current, NOW + timedelta(minutes=1))
                valorant = ObservedGame(
                    10,
                    20,
                    TrackedGame("name:valorant", "Valorant", None),
                    NOW + timedelta(minutes=1),
                )
                await repository.start_session(valorant)
                await session.flush()

                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        await repository.start_session(observed("Another", 999))

                current = await repository.get_open_session(10, 20)
                assert current is not None
                assert current.game_name == "Valorant"
                rows = (
                    await connection.execute(
                        text(
                            "SELECT game_name, started_at, confirmed_through_at, "
                            "ended_at FROM game_sessions ORDER BY id"
                        )
                    )
                ).all()
                assert len(rows) == 2
                assert rows[0].ended_at == NOW + timedelta(minutes=1)
                assert rows[1].ended_at is None
                await session.close()
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()
