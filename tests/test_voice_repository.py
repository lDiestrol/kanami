import ast
import inspect
import textwrap
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.voice import ObservedVoiceState, OpenVoiceState
from discord_stats_bot.persistence.models import VoiceInterval, VoiceSession
from discord_stats_bot.persistence.repositories.voice import (
    SqlAlchemyVoiceTransitionRepository,
    advance_confirmation_statement,
    close_interval_statement,
    close_session_statement,
    latest_confirmation_statement,
    member_lock_statement,
    open_interval_statement,
    open_session_statement,
    open_user_ids_statement,
)

OBSERVED_AT = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


class RecordingSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.flush_count = 0
        self.executed: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flush_count += 1
        for value in self.added:
            if isinstance(value, VoiceSession) and value.id is None:
                value.id = 501

    async def execute(self, statement: object) -> None:
        self.executed.append(statement)


def compile_postgresql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_member_lock_query_compiles_with_for_update() -> None:
    sql = compile_postgresql(member_lock_statement(10, 20))

    assert "FROM guild_members" in sql
    assert "guild_members.guild_id" in sql
    assert "guild_members.user_id" in sql
    assert sql.rstrip().endswith("FOR UPDATE")


def test_voice_read_and_update_queries_compile_for_postgresql() -> None:
    statements = (
        open_session_statement(10, 20),
        open_interval_statement(501),
        advance_confirmation_statement(501, OBSERVED_AT),
        close_interval_statement(601, OBSERVED_AT),
        close_session_statement(501, OBSERVED_AT),
        latest_confirmation_statement(10, 20),
        open_user_ids_statement(10),
    )

    sql = "\n".join(compile_postgresql(statement) for statement in statements)

    assert "FROM voice_sessions" in sql
    assert "FROM voice_intervals" in sql
    assert "UPDATE voice_sessions" in sql
    assert "UPDATE voice_intervals" in sql
    assert "ended_at IS NULL" in sql
    assert "max(voice_sessions.confirmed_through_at)" in sql


def test_open_user_ids_query_selects_only_open_sessions_for_guild() -> None:
    sql = compile_postgresql(open_user_ids_statement(10))

    assert "SELECT voice_sessions.user_id" in sql
    assert "voice_sessions.guild_id" in sql
    assert "voice_sessions.ended_at IS NULL" in sql
    assert "ORDER BY voice_sessions.user_id" in sql


@pytest.mark.asyncio
async def test_repository_lists_open_user_ids_as_immutable_sequence() -> None:
    class ScalarResult:
        def scalars(self) -> tuple[int, ...]:
            return (20, 30)

    class ListingSession:
        async def execute(self, statement: object) -> ScalarResult:
            assert "voice_sessions.ended_at IS NULL" in compile_postgresql(statement)
            return ScalarResult()

    repository = SqlAlchemyVoiceTransitionRepository(  # type: ignore[arg-type]
        ListingSession()
    )

    assert await repository.list_open_user_ids(10) == (20, 30)


def test_repository_implementation_has_no_hidden_transaction_control() -> None:
    source = textwrap.dedent(inspect.getsource(SqlAlchemyVoiceTransitionRepository))
    tree = ast.parse(source)
    transaction_control_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"begin", "commit", "rollback"}
    ]

    assert transaction_control_calls == []


@pytest.mark.asyncio
async def test_create_uses_database_session_identity_flush_and_exact_quality() -> None:
    session = RecordingSession()
    repository = SqlAlchemyVoiceTransitionRepository(session)  # type: ignore[arg-type]
    observed = ObservedVoiceState(
        guild_id=10,
        user_id=20,
        channel_id=30,
        channel_kind="voice",
        is_afk=False,
        observed_at=OBSERVED_AT,
    )

    await repository.create_open_state(observed, quality="exact")

    assert session.flush_count == 2
    assert len(session.added) == 2
    voice_session, interval = session.added
    assert isinstance(voice_session, VoiceSession)
    assert voice_session.id == 501
    assert isinstance(interval, VoiceInterval)
    assert interval.id is None
    assert interval.session_id == voice_session.id
    assert interval.quality == "exact"
    assert interval.started_at == OBSERVED_AT
    assert interval.ended_at is None


@pytest.mark.asyncio
async def test_same_snapshot_reconciliation_persists_estimated_and_open_exact() -> None:
    session = RecordingSession()
    repository = SqlAlchemyVoiceTransitionRepository(session)  # type: ignore[arg-type]
    reconciled_at = datetime(2026, 8, 11, 12, 5, tzinfo=UTC)
    state = OpenVoiceState(
        session_id=501,
        interval_id=601,
        confirmed_through_at=OBSERVED_AT,
        channel_id=30,
        channel_kind="voice",
        is_afk=False,
    )
    observed = ObservedVoiceState(
        guild_id=10,
        user_id=20,
        channel_id=30,
        channel_kind="voice",
        is_afk=False,
        observed_at=reconciled_at,
    )

    await repository.reconcile_same_snapshot(
        state,
        observed,
        exact_quality="exact",
        estimated_quality="estimated",
    )

    assert session.flush_count == 1
    assert len(session.executed) == 2
    assert "UPDATE voice_intervals" in compile_postgresql(session.executed[0])
    assert "UPDATE voice_sessions" in compile_postgresql(session.executed[1])
    assert len(session.added) == 2
    estimated, exact = session.added
    assert isinstance(estimated, VoiceInterval)
    assert estimated.session_id == 501
    assert estimated.started_at == OBSERVED_AT
    assert estimated.ended_at == reconciled_at
    assert estimated.quality == "estimated"
    assert isinstance(exact, VoiceInterval)
    assert exact.session_id == 501
    assert exact.started_at == reconciled_at
    assert exact.ended_at is None
    assert exact.quality == "exact"
