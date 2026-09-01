import ast
import inspect
import textwrap
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.rules import (
    RulesComplianceAcceptance,
    RulesetRecord,
    RulesetStatus,
)
from discord_stats_bot.persistence.repositories import SqlAlchemyRulesRepository

T0 = datetime(2026, 8, 25, 12, tzinfo=UTC)


class FakeResult:
    def __init__(
        self,
        scalar: object | None = None,
        *,
        rows: tuple[object, ...] = (),
        one: object | None = None,
    ) -> None:
        self.scalar = scalar
        self.rows = rows
        self.one_value = one

    def scalar_one_or_none(self) -> object | None:
        return self.scalar

    def scalar_one(self) -> object:
        return self.scalar

    def scalars(self) -> "FakeResult":
        return self

    def all(self) -> tuple[object, ...]:
        return self.rows

    def one_or_none(self) -> object | None:
        return self.one_value

    def one(self) -> object:
        assert self.one_value is not None
        return self.one_value


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


def ruleset(
    ruleset_id: int,
    version: str,
    status: RulesetStatus,
    published_at: datetime,
) -> RulesetRecord:
    return RulesetRecord(
        ruleset_id,
        10,
        version,
        "Правила",
        "Текст",
        status,
        None,
        False,
        None,
        published_at,
        published_at,
    )


@pytest.mark.asyncio
async def test_current_read_is_guild_scoped_and_maps_published_record() -> None:
    model = SimpleNamespace(
        id=1,
        guild_id=10,
        version="1.0",
        title="Правила сервера",
        content="Уважайте друг друга.",
        status="published",
        change_summary=None,
        requires_reacceptance=False,
        created_by=None,
        created_at=T0,
        published_at=T0,
        reacceptance_grace_days=None,
    )
    session = FakeSession(FakeResult(model))
    repository = SqlAlchemyRulesRepository(session)  # type: ignore[arg-type]

    record = await repository.get_current_published(10)

    assert record is not None
    assert record.status is RulesetStatus.PUBLISHED
    statement_sql = sql(session.statements[0])
    assert "WHERE rulesets.guild_id =" in statement_sql
    assert "rulesets.status =" in statement_sql


@pytest.mark.asyncio
async def test_acceptance_insert_uses_named_conflict_constraint_and_returning() -> None:
    session = FakeSession(FakeResult(99), FakeResult(None))
    repository = SqlAlchemyRulesRepository(session)  # type: ignore[arg-type]

    first = await repository.create_acceptance(
        guild_id=10, user_id=20, ruleset_id=1, accepted_at=T0
    )
    duplicate = await repository.create_acceptance(
        guild_id=10, user_id=20, ruleset_id=1, accepted_at=T0
    )

    statement_sql = sql(session.statements[0])
    assert first is True
    assert duplicate is False
    assert "INSERT INTO rule_acceptances" in statement_sql
    assert (
        "ON CONFLICT ON CONSTRAINT uq_rule_acceptances_guild_user_ruleset DO NOTHING"
        in statement_sql
    )
    assert "RETURNING rule_acceptances.id" in statement_sql


@pytest.mark.asyncio
async def test_rules_lock_uses_transaction_advisory_guild_contract() -> None:
    session = FakeSession()
    repository = SqlAlchemyRulesRepository(session)  # type: ignore[arg-type]

    await repository.lock_guild(10)

    statement_sql = sql(session.statements[0])
    statement_params = (
        session.statements[0]
        .compile(
            dialect=postgresql.dialect()  # type: ignore[attr-defined]
        )
        .params
    )
    assert "pg_advisory_xact_lock" in statement_sql
    assert 10 in statement_params.values()
    assert "FROM guilds" not in statement_sql
    assert "FOR UPDATE" not in statement_sql


@pytest.mark.asyncio
async def test_publish_archives_current_before_promoting_draft() -> None:
    published_model = SimpleNamespace(
        id=2,
        guild_id=10,
        version="1.1",
        title="Новые правила",
        content="Новый текст",
        status="published",
        change_summary="Изменения",
        requires_reacceptance=True,
        created_by=20,
        created_at=T0,
        published_at=T0,
        reacceptance_grace_days=14,
    )
    session = FakeSession(
        FakeResult(10),
        FakeResult(),
        FakeResult(published_model),
    )
    repository = SqlAlchemyRulesRepository(session)  # type: ignore[arg-type]

    result = await repository.publish_draft(2, published_at=T0)

    archive_sql = sql(session.statements[1])
    publish_sql = sql(session.statements[2])
    assert "UPDATE rulesets" in archive_sql
    assert "rulesets.guild_id =" in archive_sql
    assert "rulesets.status =" in archive_sql
    assert "UPDATE rulesets" in publish_sql
    assert "rulesets.id =" in publish_sql
    assert "rulesets.status =" in publish_sql
    assert "RETURNING" in publish_sql
    assert result.status is RulesetStatus.PUBLISHED


@pytest.mark.asyncio
async def test_update_draft_returns_none_when_concurrent_state_matches_no_row() -> None:
    session = FakeSession(FakeResult(None))
    repository = SqlAlchemyRulesRepository(session)  # type: ignore[arg-type]

    result = await repository.update_draft(2, title="Новые правила")

    statement_sql = sql(session.statements[0])
    assert result is None
    assert "rulesets.status =" in statement_sql
    assert "RETURNING" in statement_sql


@pytest.mark.asyncio
async def test_delete_draft_reports_whether_concurrent_mutation_deleted_a_row() -> None:
    session = FakeSession(FakeResult(2), FakeResult(None))
    repository = SqlAlchemyRulesRepository(session)  # type: ignore[arg-type]

    deleted = await repository.delete_draft(2)
    concurrent_publish = await repository.delete_draft(2)

    statement_sql = sql(session.statements[0])
    assert deleted is True
    assert concurrent_publish is False
    assert "rulesets.status =" in statement_sql
    assert "RETURNING rulesets.id" in statement_sql


@pytest.mark.asyncio
async def test_update_draft_persists_and_clears_reacceptance_grace() -> None:
    def draft_model(*, required: bool, grace_days: int | None) -> SimpleNamespace:
        return SimpleNamespace(
            id=2,
            guild_id=10,
            version="1.1",
            title="Правила",
            content="Текст",
            status="draft",
            change_summary=None,
            requires_reacceptance=required,
            created_by=20,
            created_at=T0,
            published_at=None,
            reacceptance_grace_days=grace_days,
        )

    session = FakeSession(
        FakeResult(draft_model(required=True, grace_days=14)),
        FakeResult(draft_model(required=False, grace_days=None)),
    )
    repository = SqlAlchemyRulesRepository(session)  # type: ignore[arg-type]

    configured = await repository.update_draft(
        2,
        requires_reacceptance=True,
        reacceptance_grace_days=14,
    )
    cleared = await repository.update_draft(
        2,
        requires_reacceptance=False,
        reacceptance_grace_days=None,
    )

    configured_params = (
        session.statements[0]
        .compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect()
        )
        .params
    )
    cleared_params = (
        session.statements[1]
        .compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect()
        )
        .params
    )
    assert configured is not None
    assert configured.requires_reacceptance is True
    assert configured.reacceptance_grace_days == 14
    assert configured_params["requires_reacceptance"] is True
    assert configured_params["reacceptance_grace_days"] == 14
    assert cleared is not None
    assert cleared.requires_reacceptance is False
    assert cleared.reacceptance_grace_days is None
    assert cleared_params["requires_reacceptance"] is False
    assert cleared_params["reacceptance_grace_days"] is None


@pytest.mark.asyncio
async def test_compliance_reads_use_publication_order_and_current_human_members() -> (
    None
):
    checkpoint = ruleset(2, "1.10", RulesetStatus.ARCHIVED, T0)
    current = ruleset(3, "1.2", RulesetStatus.PUBLISHED, T0)
    acceptance_row = SimpleNamespace(id=3, version="1.2", accepted_at=T0)
    count_row = SimpleNamespace(total=5, compliant=3)
    session = FakeSession(
        FakeResult(one=acceptance_row),
        FakeResult(one=count_row),
    )
    repository = SqlAlchemyRulesRepository(session)  # type: ignore[arg-type]

    acceptance = await repository.get_latest_qualifying_acceptance(
        guild_id=10,
        user_id=20,
        checkpoint=checkpoint,
        current=current,
    )
    counts = await repository.count_current_members_and_qualifying_acceptances(
        guild_id=10,
        checkpoint=checkpoint,
        current=current,
    )

    acceptance_sql = sql(session.statements[0])
    summary_sql = sql(session.statements[1])
    assert acceptance == RulesComplianceAcceptance(3, "1.2", T0)
    assert counts == (5, 3)
    assert "rulesets.published_at" in acceptance_sql
    assert "rulesets.id" in acceptance_sql
    assert "rule_acceptances.accepted_at DESC" in acceptance_sql
    assert "guild_members.left_at IS NULL" in summary_sql
    assert "discord_users.is_bot IS false" in summary_sql
    assert "SELECT DISTINCT rule_acceptances.user_id" in summary_sql
    assert "rulesets.status IN" in summary_sql
    assert "1.10" not in summary_sql
    assert "1.2" not in summary_sql


@pytest.mark.asyncio
async def test_published_history_excludes_drafts_and_has_deterministic_order() -> None:
    models = (
        SimpleNamespace(
            id=1,
            guild_id=10,
            version="1.0",
            title="Правила",
            content="Текст",
            status="archived",
            change_summary=None,
            requires_reacceptance=False,
            created_by=None,
            created_at=T0,
            published_at=T0,
            reacceptance_grace_days=None,
        ),
    )
    session = FakeSession(FakeResult(rows=models))
    repository = SqlAlchemyRulesRepository(session)  # type: ignore[arg-type]

    history = await repository.list_published_history(10)

    statement_sql = sql(session.statements[0])
    assert tuple(item.id for item in history) == (1,)
    assert "rulesets.status IN" in statement_sql
    assert "ORDER BY rulesets.published_at ASC, rulesets.id ASC" in statement_sql


def test_repository_has_no_hidden_transaction_control() -> None:
    source = textwrap.dedent(inspect.getsource(SqlAlchemyRulesRepository))
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"begin", "commit", "rollback"}
    ]

    assert calls == []
