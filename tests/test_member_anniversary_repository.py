from datetime import UTC, date, datetime

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.member_anniversaries import MemberAnniversary
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyMemberAnniversaryRepository,
)


class FakeScalars:
    def all(self) -> list[int]:
        return [101, 102]


class FakeResult:
    def scalars(self) -> FakeScalars:
        return FakeScalars()


class FakeSession:
    def __init__(self) -> None:
        self.statement: object | None = None

    async def execute(self, statement: object) -> FakeResult:
        self.statement = statement
        return FakeResult()


@pytest.mark.asyncio
async def test_repository_uses_partial_unique_conflict_key_and_returns_count() -> None:
    session = FakeSession()
    repository = SqlAlchemyMemberAnniversaryRepository(session)  # type: ignore[arg-type]
    anniversaries = (
        MemberAnniversary(1, "One", date(2026, 8, 20), 3, 0),
        MemberAnniversary(2, "Two", date(2026, 8, 20), 5, 0),
    )

    created = await repository.enqueue_anniversaries(
        guild_id=10,
        anniversaries=anniversaries,
        occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
    )
    compiled = str(session.statement.compile(dialect=postgresql.dialect()))  # type: ignore[union-attr]
    normalized = " ".join(compiled.split())

    assert created == 2
    assert "INSERT INTO audit_events" in normalized
    assert "ON CONFLICT (guild_id, subject_id, occurred_at)" in normalized
    assert "WHERE event_type = 'member.anniversary'" in normalized
    assert "DO NOTHING RETURNING audit_events.id" in normalized


@pytest.mark.asyncio
async def test_empty_enqueue_avoids_database_statement() -> None:
    session = FakeSession()
    repository = SqlAlchemyMemberAnniversaryRepository(session)  # type: ignore[arg-type]

    assert (
        await repository.enqueue_anniversaries(
            guild_id=10,
            anniversaries=(),
            occurred_at=datetime(2026, 8, 20, tzinfo=UTC),
        )
        == 0
    )
    assert session.statement is None
