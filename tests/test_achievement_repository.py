import ast
import inspect
import textwrap
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.persistence.repositories.achievements import (
    SqlAlchemyAchievementRepository,
    list_unlocked_statement,
    unlock_achievements_statement,
)

T0 = datetime(2026, 8, 17, 12, tzinfo=UTC)


class AchievementRow(NamedTuple):
    guild_id: int
    user_id: int
    achievement_key: str
    unlocked_at: datetime


class FakeResult:
    def __init__(self, rows: tuple[AchievementRow, ...] = ()) -> None:
        self._rows = rows

    def all(self) -> tuple[AchievementRow, ...]:
        return self._rows


class FakeSession:
    def __init__(self, *results: FakeResult) -> None:
        self.results = list(results)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return self.results.pop(0) if self.results else FakeResult()


def sql(statement: object) -> str:
    return " ".join(
        str(statement.compile(dialect=postgresql.dialect())).split()  # type: ignore[attr-defined]
    )


def test_unlock_statement_uses_atomic_postgresql_conflict_handling() -> None:
    statement = unlock_achievements_statement(
        guild_id=10,
        user_id=20,
        achievement_keys=("voice_10_hours", "voice_50_hours"),
        unlocked_at=T0,
    )
    statement_sql = sql(statement)

    assert "INSERT INTO user_achievements" in statement_sql
    assert (
        "ON CONFLICT (guild_id, user_id, achievement_key) DO NOTHING" in statement_sql
    )
    assert "RETURNING user_achievements.guild_id" in statement_sql
    assert "user_achievements.unlocked_at" in statement_sql


@pytest.mark.asyncio
async def test_unlock_returns_only_new_rows_in_requested_order() -> None:
    session = FakeSession(
        FakeResult(
            (
                AchievementRow(10, 20, "voice_50_hours", T0),
                AchievementRow(10, 20, "voice_10_hours", T0),
            )
        )
    )
    repository = SqlAlchemyAchievementRepository(session)  # type: ignore[arg-type]

    records = await repository.unlock_achievements(
        guild_id=10,
        user_id=20,
        achievement_keys=(
            "voice_10_hours",
            "already_unlocked",
            "voice_50_hours",
            "voice_10_hours",
        ),
        unlocked_at=T0,
    )

    assert tuple(record.achievement_key for record in records) == (
        "voice_10_hours",
        "voice_50_hours",
    )


@pytest.mark.asyncio
async def test_empty_unlock_short_circuits_without_sql() -> None:
    session = FakeSession()
    repository = SqlAlchemyAchievementRepository(session)  # type: ignore[arg-type]

    assert (
        await repository.unlock_achievements(
            guild_id=10,
            user_id=20,
            achievement_keys=(),
            unlocked_at=T0,
        )
        == ()
    )
    assert session.statements == []


@pytest.mark.asyncio
async def test_list_unlocked_preserves_unknown_key_and_repository_order() -> None:
    rows = (
        AchievementRow(10, 20, "retired_achievement", T0),
        AchievementRow(10, 20, "voice_10_hours", T0 + timedelta(seconds=1)),
    )
    session = FakeSession(FakeResult(rows))
    repository = SqlAlchemyAchievementRepository(session)  # type: ignore[arg-type]

    records = await repository.list_unlocked(guild_id=10, user_id=20)
    statement_sql = sql(session.statements[0])

    assert tuple(record.achievement_key for record in records) == (
        "retired_achievement",
        "voice_10_hours",
    )
    assert (
        "ORDER BY user_achievements.unlocked_at ASC, "
        "user_achievements.achievement_key ASC"
    ) in statement_sql


def test_listing_statement_filters_one_guild_member() -> None:
    statement_sql = sql(list_unlocked_statement(guild_id=10, user_id=20))

    assert "user_achievements.guild_id" in statement_sql
    assert "user_achievements.user_id" in statement_sql
    assert "WHERE user_achievements.guild_id =" in statement_sql


@pytest.mark.asyncio
async def test_repository_rejects_naive_timestamp_and_invalid_identity() -> None:
    repository = SqlAlchemyAchievementRepository(FakeSession())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="unlocked_at must be timezone-aware"):
        await repository.unlock_achievements(
            guild_id=10,
            user_id=20,
            achievement_keys=("voice_10_hours",),
            unlocked_at=datetime(2026, 8, 17, 12),
        )
    with pytest.raises(ValueError, match="guild_id must be positive"):
        await repository.list_unlocked(guild_id=0, user_id=20)


def test_repository_has_no_hidden_transaction_control() -> None:
    source = textwrap.dedent(inspect.getsource(SqlAlchemyAchievementRepository))
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"begin", "commit", "rollback"}
    ]

    assert calls == []
