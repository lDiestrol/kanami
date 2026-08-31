from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import discord_stats_bot.persistence.database as database_module
from discord_stats_bot.config import Settings
from discord_stats_bot.persistence.database import create_database_resources
from discord_stats_bot.persistence.models import Base

DATABASE_URL = (
    "postgresql+asyncpg://review-user:review-password@localhost:5432/review-db"
)
INITIAL_REVISION = "6f3d2a91b7c4"
AUDIT_REVISION = "91c4f28a6d3e"
TEXT_ACTIVITY_REVISION = "d7a4e2c91b56"
ACHIEVEMENTS_REVISION = "4b9c1e7a2d63"
ANNIVERSARY_REVISION = "2f6a8c4d1e90"
MEMBER_RETURN_REVISION = "7c2d9a4e6f10"
DISCORD_IDENTITY_REVISION = "a8d3e5f7b912"
WEB_ADMIN_ACCESS_REVISION = "8d44cacc791e"
SERVER_SETTINGS_REVISION = "3e7b9c2a6f41"
GAME_TRACKING_REVISION = "c5b7e1d9a024"
OPERATIONAL_HEALTH_REVISION = "f2a6c9d41b73"
RULES_REVISION = "b6e2c8f91a47"
RULES_PUBLICATION_REVISION = "e1a7c4d92b60"
RULES_COMPLIANCE_REVISION = "a4f6c8d21e73"
MEMBER_AVATAR_REVISION = "d4e8a1c7b962"


def make_settings() -> Settings:
    return Settings(
        _env_file=None,
        DISCORD_TOKEN="test-token",
        DISCORD_GUILD_ID=123456789,
        DATABASE_URL=DATABASE_URL,
    )


def test_persistence_import_does_not_create_engine() -> None:
    engines = [
        value
        for value in vars(database_module).values()
        if isinstance(value, AsyncEngine)
    ]

    assert engines == []


@pytest.mark.asyncio
async def test_database_resources_are_created_explicitly() -> None:
    resources = create_database_resources(make_settings())

    assert isinstance(resources.engine, AsyncEngine)
    assert resources.engine.url.drivername == "postgresql+asyncpg"
    assert isinstance(resources.session_factory, async_sessionmaker)

    await resources.dispose()


@pytest.mark.asyncio
async def test_session_factory_and_dispose_do_not_require_postgresql() -> None:
    resources = create_database_resources(make_settings())
    session = resources.session_factory()

    assert isinstance(session, AsyncSession)

    await session.close()
    await resources.dispose()


@pytest.mark.asyncio
async def test_database_resources_repr_hides_credentials() -> None:
    resources = create_database_resources(make_settings())
    rendered = repr(resources)

    assert DATABASE_URL not in rendered
    assert "review-user" not in rendered
    assert "review-password" not in rendered

    await resources.dispose()


def test_base_metadata_contains_business_tables() -> None:
    assert Base.metadata is not None
    assert set(Base.metadata.tables) == {
        "audit_events",
        "daily_text_activity",
        "discord_users",
        "guild_members",
        "guild_server_settings",
        "game_sessions",
        "operational_health_observations",
        "rule_acceptances",
        "rulesets",
        "guilds",
        "user_achievements",
        "web_admin_access_grants",
        "voice_channels",
        "voice_intervals",
        "voice_sessions",
    }


def test_alembic_configuration_has_no_stored_database_url() -> None:
    alembic_config = Config("alembic.ini")
    script_directory = ScriptDirectory.from_config(alembic_config)

    assert alembic_config.get_main_option("sqlalchemy.url") == ""
    assert Path(script_directory.dir).resolve() == Path("migrations").resolve()
    revisions = list(script_directory.walk_revisions())
    assert [revision.revision for revision in revisions] == [
        MEMBER_AVATAR_REVISION,
        RULES_COMPLIANCE_REVISION,
        RULES_PUBLICATION_REVISION,
        RULES_REVISION,
        OPERATIONAL_HEALTH_REVISION,
        GAME_TRACKING_REVISION,
        SERVER_SETTINGS_REVISION,
        WEB_ADMIN_ACCESS_REVISION,
        DISCORD_IDENTITY_REVISION,
        MEMBER_RETURN_REVISION,
        ANNIVERSARY_REVISION,
        ACHIEVEMENTS_REVISION,
        TEXT_ACTIVITY_REVISION,
        AUDIT_REVISION,
        INITIAL_REVISION,
    ]
    assert revisions[0].down_revision == RULES_COMPLIANCE_REVISION
    assert revisions[1].down_revision == RULES_PUBLICATION_REVISION
    assert revisions[2].down_revision == RULES_REVISION
    assert revisions[3].down_revision == OPERATIONAL_HEALTH_REVISION
    assert revisions[4].down_revision == GAME_TRACKING_REVISION
    assert revisions[5].down_revision == SERVER_SETTINGS_REVISION
    assert revisions[6].down_revision == WEB_ADMIN_ACCESS_REVISION
    assert revisions[7].down_revision == DISCORD_IDENTITY_REVISION
    assert revisions[8].down_revision == MEMBER_RETURN_REVISION
    assert revisions[9].down_revision == ANNIVERSARY_REVISION
    assert revisions[10].down_revision == ACHIEVEMENTS_REVISION
    assert revisions[11].down_revision == TEXT_ACTIVITY_REVISION
    assert revisions[12].down_revision == AUDIT_REVISION
    assert revisions[13].down_revision == INITIAL_REVISION
    assert revisions[14].down_revision is None


def test_alembic_env_uses_base_metadata() -> None:
    env_source = Path("migrations/env.py").read_text(encoding="utf-8")

    assert "from discord_stats_bot.persistence.models import Base" in env_source
    assert "target_metadata = Base.metadata" in env_source


def test_alembic_offline_configuration_does_not_expose_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    project_root = Path.cwd()
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_GUILD_ID", raising=False)
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.chdir(tmp_path)

    alembic_config = Config(project_root / "alembic.ini")
    alembic_config.set_main_option(
        "script_location",
        str(project_root / "migrations"),
    )

    command.upgrade(alembic_config, "head", sql=True)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert DATABASE_URL not in output
    assert "review-user" not in output
    assert "review-password" not in output
    for table_name in (
        "guilds",
        "discord_users",
        "guild_members",
        "voice_channels",
        "voice_sessions",
        "voice_intervals",
        "audit_events",
        "daily_text_activity",
        "user_achievements",
        "web_admin_access_grants",
        "guild_server_settings",
        "game_sessions",
        "operational_health_observations",
        "rulesets",
        "rule_acceptances",
    ):
        assert f"CREATE TABLE {table_name}" in output
    assert "BIGSERIAL" not in output
    assert " SERIAL" not in output
    assert output.count("GENERATED BY DEFAULT AS IDENTITY") == 8
    assert output.count("WHERE ended_at IS NULL") == 3
