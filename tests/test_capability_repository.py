import ast
import inspect
import textwrap
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.capabilities import (
    VOICE_MOVE,
    CapabilitySubjectType,
)
from discord_stats_bot.persistence.repositories import SqlAlchemyCapabilityRepository

T0 = datetime(2026, 9, 7, 12, tzinfo=UTC)


class FakeScalarResult:
    def __init__(self, value=None, values=()):
        self.value = value
        self.values = values

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        assert self.value is not None
        return self.value

    def scalars(self):
        return self.values


class FakeSession:
    def __init__(self, *results):
        self.results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0) if self.results else FakeScalarResult()


def sql(statement) -> str:
    return " ".join(
        str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    )


def grant_model(subject_type="user", subject_id=2):
    return SimpleNamespace(
        guild_id=1,
        capability_key="voice.move",
        subject_type=subject_type,
        subject_id=subject_id,
        created_at=T0,
        created_by_user_id=99,
    )


@pytest.mark.asyncio
async def test_policy_round_trip_and_absent_policy() -> None:
    model = SimpleNamespace(
        guild_id=1,
        capability_key="voice.move",
        enabled=True,
        updated_at=T0,
        updated_by_user_id=99,
    )
    session = FakeSession(FakeScalarResult(), FakeScalarResult(model))
    repository = SqlAlchemyCapabilityRepository(session)  # type: ignore[arg-type]

    assert await repository.get_policy(1, VOICE_MOVE) is None
    saved = await repository.set_policy(
        guild_id=1,
        capability=VOICE_MOVE,
        enabled=True,
        updated_at=T0,
        updated_by_user_id=99,
    )

    assert saved.enabled is True
    assert "ON CONFLICT (guild_id, capability_key) DO UPDATE" in sql(
        session.statements[1]
    )


@pytest.mark.asyncio
async def test_grant_lookup_list_insert_and_delete_statements() -> None:
    models = (grant_model(), grant_model("role", 8))
    session = FakeSession(
        FakeScalarResult(2),
        FakeScalarResult(8),
        FakeScalarResult(values=models),
        FakeScalarResult(models[0]),
        FakeScalarResult(),
        FakeScalarResult(2),
        FakeScalarResult(),
    )
    repository = SqlAlchemyCapabilityRepository(session)  # type: ignore[arg-type]

    assert await repository.has_user_grant(1, VOICE_MOVE, 2)
    assert await repository.has_any_role_grant(1, VOICE_MOVE, (7, 8, 9))
    grants = await repository.list_grants(1, VOICE_MOVE)
    inserted = await repository.add_grant(
        guild_id=1,
        capability=VOICE_MOVE,
        subject_type=CapabilitySubjectType.USER,
        subject_id=2,
        created_at=T0,
        created_by_user_id=99,
    )
    duplicate = await repository.add_grant(
        guild_id=1,
        capability=VOICE_MOVE,
        subject_type=CapabilitySubjectType.USER,
        subject_id=2,
        created_at=T0,
        created_by_user_id=99,
    )
    removed = await repository.remove_grant(
        guild_id=1,
        capability=VOICE_MOVE,
        subject_type=CapabilitySubjectType.USER,
        subject_id=2,
    )
    repeated = await repository.remove_grant(
        guild_id=1,
        capability=VOICE_MOVE,
        subject_type=CapabilitySubjectType.USER,
        subject_id=2,
    )

    assert [grant.subject_id for grant in grants] == [2, 8]
    assert inserted is not None and duplicate is None
    assert (removed, repeated) == (True, False)
    assert "subject_type = 'user'" in sql(session.statements[0])
    assert "subject_id IN (7, 8, 9)" in sql(session.statements[1])
    assert "ON CONFLICT (guild_id, capability_key, subject_type, subject_id)" in sql(
        session.statements[3]
    )
    assert "DO NOTHING" in sql(session.statements[3])
    assert sql(session.statements[5]).startswith("DELETE FROM guild_capability_grants")


@pytest.mark.asyncio
async def test_empty_roles_short_circuits_and_invalid_ids_fail() -> None:
    session = FakeSession()
    repository = SqlAlchemyCapabilityRepository(session)  # type: ignore[arg-type]
    assert await repository.has_any_role_grant(1, VOICE_MOVE, ()) is False
    assert session.statements == []
    with pytest.raises(ValueError, match="user_id must be positive"):
        await repository.has_user_grant(1, VOICE_MOVE, 0)


def test_repository_has_no_hidden_transaction_control() -> None:
    source = textwrap.dedent(inspect.getsource(SqlAlchemyCapabilityRepository))
    tree = ast.parse(source)
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"begin", "commit", "rollback"}
    ]
