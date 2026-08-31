import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.persistence.repositories import (
    SqlAlchemyRulesPublicationRepository,
)


class FakeResult:
    def __init__(self, row: object = None, *, rowcount: int = 1) -> None:
        self.row = row
        self.rowcount = rowcount

    def one_or_none(self) -> object:
        return self.row


class FakeSession:
    def __init__(self, *results: FakeResult) -> None:
        self.results = list(results)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return self.results.pop(0)


def sql(statement: object) -> str:
    return " ".join(
        str(statement.compile(dialect=postgresql.dialect())).split()  # type: ignore[attr-defined]
    )


@pytest.mark.asyncio
async def test_missing_settings_row_means_publication_disabled() -> None:
    session = FakeSession(FakeResult())
    repository = SqlAlchemyRulesPublicationRepository(session)  # type: ignore[arg-type]

    state = await repository.get(10)

    assert state.channel_id is None
    assert state.message_id is None
    assert state.ruleset_id is None
    assert "WHERE guild_server_settings.guild_id =" in sql(session.statements[0])


@pytest.mark.asyncio
async def test_repository_maps_configured_delivery_cursor() -> None:
    session = FakeSession(FakeResult((20, 30, 40)))
    repository = SqlAlchemyRulesPublicationRepository(session)  # type: ignore[arg-type]

    state = await repository.get(10)

    assert state.guild_id == 10
    assert state.channel_id == 20
    assert state.message_id == 30
    assert state.ruleset_id == 40


@pytest.mark.asyncio
async def test_delivery_cursor_update_is_guild_scoped() -> None:
    session = FakeSession(FakeResult(rowcount=1))
    repository = SqlAlchemyRulesPublicationRepository(session)  # type: ignore[arg-type]

    await repository.save_delivery(guild_id=10, message_id=20, ruleset_id=30)

    statement = sql(session.statements[0])
    assert statement.startswith("UPDATE guild_server_settings SET")
    assert "rules_publication_message_id=" in statement
    assert "rules_publication_ruleset_id=" in statement
    assert "WHERE guild_server_settings.guild_id =" in statement


@pytest.mark.asyncio
async def test_configuration_upsert_clears_delivery_cursor() -> None:
    session = FakeSession(FakeResult())
    repository = SqlAlchemyRulesPublicationRepository(session)  # type: ignore[arg-type]

    await repository.save_configuration(guild_id=10, channel_id=20)

    statement = sql(session.statements[0])
    assert statement.startswith("INSERT INTO guild_server_settings")
    assert "ON CONFLICT (guild_id) DO UPDATE SET" in statement
    assert "rules_publication_channel_id" in statement
    assert "rules_publication_message_id" in statement
    assert "rules_publication_ruleset_id" in statement
