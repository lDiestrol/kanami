import ast
import inspect
import textwrap
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.persistence.repositories.web_admin_access import (
    SqlAlchemyWebAdminAccessRepository,
    grant_access_statement,
    revoke_access_statement,
)

T0 = datetime(2026, 8, 22, 12, tzinfo=UTC)


class FakeScalarResult:
    def __init__(self, rows: tuple[object, ...] = ()) -> None:
        self._rows = rows

    def one_or_none(self) -> object | None:
        if not self._rows:
            return None
        assert len(self._rows) == 1
        return self._rows[0]

    def all(self) -> tuple[object, ...]:
        return self._rows


class FakeSession:
    def __init__(
        self,
        *scalar_results: FakeScalarResult,
        scalar_values: tuple[object | None, ...] = (),
    ) -> None:
        self.scalar_results = list(scalar_results)
        self.scalar_values = list(scalar_values)
        self.statements: list[object] = []

    async def scalars(self, statement: object) -> FakeScalarResult:
        self.statements.append(statement)
        return self.scalar_results.pop(0) if self.scalar_results else FakeScalarResult()

    async def scalar(self, statement: object) -> object | None:
        self.statements.append(statement)
        return self.scalar_values.pop(0) if self.scalar_values else None


def sql(statement: object) -> str:
    return " ".join(
        str(statement.compile(dialect=postgresql.dialect())).split()  # type: ignore[attr-defined]
    )


def grant_model(
    *,
    grant_id: int = 1,
    revoked_by_user_id: int | None = None,
    revoked_at: datetime | None = None,
) -> object:
    return SimpleNamespace(
        id=grant_id,
        guild_id=10,
        user_id=20,
        granted_by_user_id=30,
        granted_at=T0,
        revoked_by_user_id=revoked_by_user_id,
        revoked_at=revoked_at,
    )


def test_grant_statement_uses_partial_unique_conflict_target() -> None:
    statement_sql = sql(
        grant_access_statement(
            guild_id=10,
            user_id=20,
            actor_user_id=30,
            granted_at=T0,
        )
    )

    assert "INSERT INTO web_admin_access_grants" in statement_sql
    assert "ON CONFLICT (guild_id, user_id)" in statement_sql
    assert "WHERE revoked_at IS NULL" in statement_sql
    assert "DO NOTHING" in statement_sql
    assert "RETURNING web_admin_access_grants.id" in statement_sql


def test_revoke_statement_targets_only_active_grant() -> None:
    statement_sql = sql(
        revoke_access_statement(
            guild_id=10,
            user_id=20,
            actor_user_id=30,
            revoked_at=T0,
        )
    )

    assert "UPDATE web_admin_access_grants SET" in statement_sql
    assert "revoked_by_user_id=" in statement_sql
    assert "revoked_at=" in statement_sql
    assert "web_admin_access_grants.guild_id =" in statement_sql
    assert "web_admin_access_grants.user_id =" in statement_sql
    assert "web_admin_access_grants.revoked_at IS NULL" in statement_sql
    assert "RETURNING web_admin_access_grants.id" in statement_sql


@pytest.mark.asyncio
async def test_grant_returns_inserted_record() -> None:
    session = FakeSession(FakeScalarResult((grant_model(),)))
    repository = SqlAlchemyWebAdminAccessRepository(session)  # type: ignore[arg-type]

    record = await repository.grant(
        guild_id=10,
        user_id=20,
        actor_user_id=30,
        granted_at=T0,
    )

    assert record is not None
    assert record.id == 1
    assert record.guild_id == 10
    assert record.user_id == 20
    assert record.granted_by_user_id == 30
    assert record.revoked_at is None


@pytest.mark.asyncio
async def test_duplicate_grant_returns_none() -> None:
    repository = SqlAlchemyWebAdminAccessRepository(FakeSession(FakeScalarResult()))  # type: ignore[arg-type]

    assert (
        await repository.grant(
            guild_id=10,
            user_id=20,
            actor_user_id=30,
            granted_at=T0,
        )
        is None
    )


@pytest.mark.asyncio
async def test_revoke_returns_closed_record() -> None:
    closed = grant_model(revoked_by_user_id=40, revoked_at=T0)
    repository = SqlAlchemyWebAdminAccessRepository(
        FakeSession(FakeScalarResult((closed,)))  # type: ignore[arg-type]
    )

    record = await repository.revoke(
        guild_id=10,
        user_id=20,
        actor_user_id=40,
        revoked_at=T0,
    )

    assert record is not None
    assert record.revoked_by_user_id == 40
    assert record.revoked_at == T0


@pytest.mark.asyncio
async def test_duplicate_revoke_returns_none() -> None:
    repository = SqlAlchemyWebAdminAccessRepository(FakeSession(FakeScalarResult()))  # type: ignore[arg-type]

    assert (
        await repository.revoke(
            guild_id=10,
            user_id=20,
            actor_user_id=40,
            revoked_at=T0,
        )
        is None
    )


@pytest.mark.asyncio
async def test_is_active_admin_uses_bounded_lookup() -> None:
    session = FakeSession(scalar_values=(123, None))
    repository = SqlAlchemyWebAdminAccessRepository(session)  # type: ignore[arg-type]

    assert await repository.is_active_admin(guild_id=10, user_id=20) is True
    assert await repository.is_active_admin(guild_id=10, user_id=21) is False
    assert "LIMIT" in sql(session.statements[0])
    assert "revoked_at IS NULL" in sql(session.statements[0])


@pytest.mark.asyncio
async def test_list_active_preserves_repository_order() -> None:
    first = grant_model(grant_id=1)
    second = SimpleNamespace(**{**first.__dict__, "id": 2, "user_id": 21})
    session = FakeSession(FakeScalarResult((first, second)))
    repository = SqlAlchemyWebAdminAccessRepository(session)  # type: ignore[arg-type]

    records = await repository.list_active(guild_id=10)
    statement_sql = sql(session.statements[0])

    assert tuple(record.id for record in records) == (1, 2)
    assert "revoked_at IS NULL" in statement_sql
    assert "ORDER BY web_admin_access_grants.granted_at ASC" in statement_sql


@pytest.mark.asyncio
async def test_repository_rejects_invalid_ids_and_naive_timestamps() -> None:
    repository = SqlAlchemyWebAdminAccessRepository(FakeSession())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="guild_id must be positive"):
        await repository.is_active_admin(guild_id=0, user_id=20)
    with pytest.raises(ValueError, match="granted_at must be timezone-aware"):
        await repository.grant(
            guild_id=10,
            user_id=20,
            actor_user_id=30,
            granted_at=datetime(2026, 8, 22, 12),
        )
    with pytest.raises(ValueError, match="actor_user_id must be positive"):
        await repository.revoke(
            guild_id=10,
            user_id=20,
            actor_user_id=0,
            revoked_at=T0,
        )


def test_repository_has_no_hidden_transaction_control() -> None:
    source = textwrap.dedent(inspect.getsource(SqlAlchemyWebAdminAccessRepository))
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"begin", "commit", "rollback"}
    ]

    assert calls == []
