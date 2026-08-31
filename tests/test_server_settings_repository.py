import ast
import inspect
import textwrap
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from discord_stats_bot.features.server_settings import (
    GuildServerSettingKey,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
    GuildServerSettingsBaselines,
    GuildServerSettingSource,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyGuildServerSettingsRepository,
)
from discord_stats_bot.web.server_settings import WebAdminServerSettingsReadService

T0 = datetime(2026, 8, 23, 12, tzinfo=UTC)


class FakeResult:
    def __init__(self, scalar: object | None = None) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> object | None:
        return self._scalar


class FakeSession:
    def __init__(self, results: list[FakeResult] | None = None) -> None:
        self.results = list(results or [])
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return self.results.pop(0) if self.results else FakeResult()


@pytest.mark.asyncio
async def test_repository_read_is_guild_scoped_and_optional_for_update() -> None:
    model = SimpleNamespace(
        guild_id=10,
        autorole_role_mode="value",
        autorole_role_id=20,
        audit_log_channel_mode="disabled",
        audit_log_channel_id=None,
        anniversary_channel_mode="env",
        anniversary_channel_id=None,
        return_channel_mode="value",
        return_channel_id=30,
        updated_at=T0,
        updated_by_user_id=40,
    )
    session = FakeSession([FakeResult(model)])
    repository = SqlAlchemyGuildServerSettingsRepository(session)  # type: ignore[arg-type]

    result = await repository.get_overrides(10, for_update=True)

    assert result is not None
    assert result.autorole_role.value == 20
    assert result.audit_log_channel.mode is GuildServerSettingOverrideMode.DISABLED
    assert result.return_channel.value == 30
    sql = " ".join(str(session.statements[0]).split())
    assert sql.startswith("SELECT")
    assert "guild_server_settings.guild_id =" in sql
    assert sql.endswith("FOR UPDATE")


@pytest.mark.asyncio
async def test_repository_write_is_allowlisted_upsert_without_transaction_control() -> (
    None
):
    session = FakeSession()
    repository = SqlAlchemyGuildServerSettingsRepository(session)  # type: ignore[arg-type]
    override = GuildServerSettingOverride(GuildServerSettingOverrideMode.VALUE, 55)

    await repository.save_override(
        guild_id=10,
        key=GuildServerSettingKey.ANNIVERSARY_CHANNEL,
        override=override,
        updated_at=T0,
        updated_by_user_id=40,
    )

    sql = " ".join(str(session.statements[0]).split())
    assert sql.startswith("INSERT INTO guild_server_settings")
    assert "ON CONFLICT (guild_id) DO UPDATE" in sql
    assert "anniversary_channel_mode" in sql
    assert "anniversary_channel_id" in sql
    source = textwrap.dedent(inspect.getsource(SqlAlchemyGuildServerSettingsRepository))
    tree = ast.parse(source)
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"begin", "commit", "rollback"}
    ]


@pytest.mark.asyncio
async def test_web_admin_read_model_uses_one_select_and_env_fallback() -> None:
    session = FakeSession([FakeResult(None)])

    class SessionContext:
        async def __aenter__(self) -> FakeSession:
            return session

        async def __aexit__(self, *args: object) -> None:
            return None

    service = WebAdminServerSettingsReadService(
        lambda: SessionContext(),  # type: ignore[arg-type]
        guild_id=10,
        baselines=GuildServerSettingsBaselines(
            autorole_role_id=11,
            audit_log_channel_id=12,
        ),
    )

    values = await service.load()

    assert values is not None and len(values) == 4
    by_key = {item.key: item for item in values}
    assert by_key[GuildServerSettingKey.AUTOROLE_ROLE].effective_value == 11
    assert (
        by_key[GuildServerSettingKey.AUTOROLE_ROLE].source
        is GuildServerSettingSource.ENV
    )
    assert len(session.statements) == 1
    sql = str(session.statements[0]).upper()
    assert sql.startswith("SELECT")
    assert all(
        f" {keyword} " not in f" {sql} " for keyword in ("INSERT", "UPDATE", "DELETE")
    )
