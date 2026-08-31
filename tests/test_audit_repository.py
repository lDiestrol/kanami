from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.audit_logging import AuditEventDraft
from discord_stats_bot.persistence.models import AuditEvent
from discord_stats_bot.persistence.repositories import SqlAlchemyAuditEventRepository

T0 = datetime(2026, 8, 14, 12, tzinfo=UTC)


def make_draft(event_type: str = "voice.joined") -> AuditEventDraft:
    return AuditEventDraft(
        guild_id=10,
        category="voice" if event_type.startswith("voice.") else "member",
        event_type=event_type,
        occurred_at=T0,
        subject_type="user",
        subject_id=20,
        channel_id=30 if event_type.startswith("voice.") else None,
        before_data={},
        after_data={"channel_id": 30},
    )


class FakeScalars:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.values)

    def all(self) -> list[object]:
        return self.values


class FakeResult:
    def __init__(self, values: list[object] | None = None) -> None:
        self.values = values or []

    def scalars(self) -> FakeScalars:
        return FakeScalars(self.values)


class FakeSession:
    def __init__(self) -> None:
        self.models: list[AuditEvent] = []
        self.statements: list[object] = []
        self.execute_results: list[FakeResult] = []

    def add_all(self, models: list[AuditEvent]) -> None:
        self.models.extend(models)

    async def flush(self) -> None:
        for index, model in enumerate(self.models, start=1):
            model.id = index
            model.created_at = T0
            model.delivery_attempts = 0
            model.discord_message_id = None
            model.delivered_at = None
            model.next_delivery_attempt_at = None
            model.last_delivery_error = None

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        if self.execute_results:
            return self.execute_results.pop(0)
        return FakeResult()


def sql(statement: object) -> str:
    return " ".join(
        str(
            statement.compile(  # type: ignore[attr-defined]
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    )


@pytest.mark.asyncio
async def test_create_and_create_many_insert_normalized_json_and_expiry() -> None:
    session = FakeSession()
    repository = SqlAlchemyAuditEventRepository(session)  # type: ignore[arg-type]
    expires_at = T0 + timedelta(days=90)

    first = await repository.create(make_draft(), expires_at=expires_at)
    records = await repository.create_many(
        ((make_draft("member.joined"), None), (make_draft(), expires_at))
    )

    assert first.id == 1
    assert first.expires_at == expires_at
    assert [item.event_type for item in records] == ["member.joined", "voice.joined"]
    assert session.models[0].after_data == {"channel_id": 30}
    assert session.models[1].expires_at is None


def persisted_model(event_id: int, occurred_at: datetime) -> AuditEvent:
    return AuditEvent(
        id=event_id,
        guild_id=10,
        category="voice",
        event_type="voice.joined",
        occurred_at=occurred_at,
        created_at=T0,
        subject_type="user",
        subject_id=20,
        actor_user_id=None,
        channel_id=30,
        before_data={},
        after_data={},
        details_data={},
        discord_message_id=None,
        delivered_at=None,
        delivery_attempts=0,
        next_delivery_attempt_at=None,
        last_delivery_error=None,
        expires_at=T0 + timedelta(days=90),
    )


@pytest.mark.asyncio
async def test_pending_selection_has_retry_filter_oldest_order_and_limit() -> None:
    session = FakeSession()
    older = persisted_model(1, T0)
    newer = persisted_model(2, T0 + timedelta(seconds=1))
    session.execute_results.append(FakeResult([older, newer]))
    repository = SqlAlchemyAuditEventRepository(session)  # type: ignore[arg-type]

    records = await repository.get_pending_delivery(guild_id=10, as_of=T0, limit=25)
    statement_sql = sql(session.statements[0])

    assert [item.id for item in records] == [1, 2]
    assert "audit_events.guild_id = 10" in statement_sql
    assert "audit_events.delivered_at IS NULL" in statement_sql
    assert "next_delivery_attempt_at IS NULL" in statement_sql
    assert "next_delivery_attempt_at <=" in statement_sql
    assert "ORDER BY audit_events.occurred_at ASC, audit_events.id ASC" in statement_sql
    assert "LIMIT 25" in statement_sql


@pytest.mark.asyncio
async def test_pending_selection_rejects_non_positive_guild_id() -> None:
    repository = SqlAlchemyAuditEventRepository(FakeSession())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="guild_id must be positive"):
        await repository.get_pending_delivery(guild_id=0, as_of=T0, limit=25)


@pytest.mark.asyncio
async def test_pending_selection_can_filter_delivery_event_types() -> None:
    session = FakeSession()
    repository = SqlAlchemyAuditEventRepository(session)  # type: ignore[arg-type]

    await repository.get_pending_delivery(
        guild_id=10,
        as_of=T0,
        limit=25,
        event_types=("member.anniversary",),
    )
    statement_sql = sql(session.statements[0])

    assert "audit_events.event_type IN ('member.anniversary')" in statement_sql


@pytest.mark.asyncio
async def test_delivered_and_failed_updates_preserve_retry_semantics() -> None:
    session = FakeSession()
    repository = SqlAlchemyAuditEventRepository(session)  # type: ignore[arg-type]

    await repository.mark_delivered(1, 999, T0)
    await repository.mark_delivery_failed(2, "temporary", T0 + timedelta(seconds=5))
    delivered_sql, failed_sql = map(sql, session.statements)

    assert "discord_message_id=999" in delivered_sql
    assert "delivered_at=" in delivered_sql
    assert "next_delivery_attempt_at=NULL" in delivered_sql
    assert "last_delivery_error=NULL" in delivered_sql
    assert "delivery_attempts=(audit_events.delivery_attempts + 1)" in failed_sql
    assert "last_delivery_error='temporary'" in failed_sql
    assert "next_delivery_attempt_at=" in failed_sql


@pytest.mark.asyncio
async def test_suppressed_history_is_resolved_without_discord_message() -> None:
    session = FakeSession()
    repository = SqlAlchemyAuditEventRepository(session)  # type: ignore[arg-type]

    await repository.mark_delivery_suppressed((1, 2), T0)
    statement_sql = sql(session.statements[0])

    assert "audit_events.id IN (1, 2)" in statement_sql
    assert "discord_message_id=NULL" in statement_sql
    assert "delivered_at=" in statement_sql
    assert "next_delivery_attempt_at=NULL" in statement_sql
    assert "last_delivery_error=NULL" in statement_sql


@pytest.mark.asyncio
async def test_batch_delivery_state_updates_all_ids_in_one_statement() -> None:
    session = FakeSession()
    repository = SqlAlchemyAuditEventRepository(session)  # type: ignore[arg-type]

    await repository.mark_delivered_many((1, 2, 3), 999, T0)
    await repository.mark_delivery_failed_many(
        (4, 5), "temporary", T0 + timedelta(seconds=5)
    )
    delivered_sql, failed_sql = map(sql, session.statements)

    assert "audit_events.id IN (1, 2, 3)" in delivered_sql
    assert "discord_message_id=999" in delivered_sql
    assert "audit_events.id IN (4, 5)" in failed_sql
    assert "delivery_attempts=(audit_events.delivery_attempts + 1)" in failed_sql


@pytest.mark.asyncio
async def test_expiry_cleanup_only_targets_expired_non_null_rows() -> None:
    session = FakeSession()
    session.execute_results.append(FakeResult([1, 2]))
    repository = SqlAlchemyAuditEventRepository(session)  # type: ignore[arg-type]

    assert await repository.delete_expired(as_of=T0) == 2
    statement_sql = sql(session.statements[0])
    assert "audit_events.expires_at IS NOT NULL" in statement_sql
    assert "audit_events.expires_at <=" in statement_sql
    assert "RETURNING audit_events.id" in statement_sql
