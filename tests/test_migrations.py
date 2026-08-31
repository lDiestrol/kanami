from io import StringIO
from types import ModuleType

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import Script, ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from discord_stats_bot.persistence.models import Base

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
TABLE_ORDER = (
    "guilds",
    "discord_users",
    "guild_members",
    "voice_channels",
    "voice_sessions",
    "voice_intervals",
)
REGULAR_INDEXES = {
    "ix_voice_sessions_guild_user_started_at",
    "ix_voice_intervals_session_started_at",
    "ix_voice_intervals_guild_user_started_at",
    "ix_voice_intervals_guild_channel_started_at",
    "ix_voice_intervals_guild_started_at",
}
PARTIAL_UNIQUE_INDEXES = {
    "uq_voice_sessions_open_guild_user",
    "uq_voice_intervals_open_guild_user",
}


def revision(revision_id: str = INITIAL_REVISION) -> Script:
    script_directory = ScriptDirectory.from_config(Config("alembic.ini"))
    selected = script_directory.get_revision(revision_id)
    assert selected is not None
    return selected


def render_revision_operation(
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
    revision_id: str = INITIAL_REVISION,
) -> str:
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    operations = Operations(context)
    revision_module: ModuleType = revision(revision_id).module
    monkeypatch.setattr(revision_module, "op", operations)

    getattr(revision_module, function_name)()
    return output.getvalue()


def normalize_sql(sql: str) -> str:
    return " ".join(sql.split())


def test_alembic_history_is_linear() -> None:
    script_directory = ScriptDirectory.from_config(Config("alembic.ini"))
    revisions = list(script_directory.walk_revisions())

    assert [item.revision for item in revisions] == [
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
    assert script_directory.get_heads() == [MEMBER_AVATAR_REVISION]
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
    assert all(callable(item.module.upgrade) for item in revisions)
    assert all(callable(item.module.downgrade) for item in revisions)


def test_upgrade_creates_tables_in_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = render_revision_operation(monkeypatch, "upgrade")
    positions = [sql.index(f"CREATE TABLE {table_name}") for table_name in TABLE_ORDER]

    assert positions == sorted(positions)


def test_upgrade_matches_metadata_constraints_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(render_revision_operation(monkeypatch, "upgrade"))

    for table in Base.metadata.sorted_tables:
        if table.name in {
            "audit_events",
            "daily_text_activity",
            "user_achievements",
            "web_admin_access_grants",
            "guild_server_settings",
            "game_sessions",
            "operational_health_observations",
            "rulesets",
            "rule_acceptances",
        }:
            continue
        for constraint in table.constraints:
            if constraint.name is not None:
                assert f"CONSTRAINT {constraint.name}" in sql

    for index_name in REGULAR_INDEXES:
        assert f"CREATE INDEX {index_name}" in sql

    for index_name in PARTIAL_UNIQUE_INDEXES:
        assert f"CREATE UNIQUE INDEX {index_name}" in sql
        index_start = sql.index(f"CREATE UNIQUE INDEX {index_name}")
        statement_end = sql.index(";", index_start)
        assert "WHERE ended_at IS NULL" in sql[index_start:statement_end]

    assert "BIGSERIAL" not in sql
    assert " SERIAL" not in sql
    for table_name in ("guilds", "discord_users", "voice_channels"):
        assert f"CREATE TABLE {table_name} ( id BIGINT NOT NULL," in sql
    for table_name in ("voice_sessions", "voice_intervals"):
        assert (
            f"CREATE TABLE {table_name} ( id BIGINT GENERATED BY DEFAULT AS IDENTITY,"
            in sql
        )
    assert sql.count("GENERATED BY DEFAULT AS IDENTITY") == 2
    assert "FOREIGN KEY(guild_id, user_id) REFERENCES guild_members" in sql
    assert "FOREIGN KEY(session_id, guild_id, user_id) REFERENCES voice_sessions" in sql
    assert "FOREIGN KEY(guild_id, channel_id) REFERENCES voice_channels" in sql


def test_audit_revision_matches_model_and_required_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", AUDIT_REVISION)
    )
    table = Base.metadata.tables["audit_events"]

    assert "CREATE TABLE audit_events" in sql
    for constraint in table.constraints:
        if constraint.name is not None:
            assert f"CONSTRAINT {constraint.name}" in sql
    for index in table.indexes:
        if index.name in {
            "uq_audit_events_member_anniversary",
            "uq_audit_events_member_returned",
        }:
            continue
        assert f"CREATE INDEX {index.name}" in sql
    assert "JSONB" in sql
    assert sql.count("JSONB") == 3
    assert "id BIGINT GENERATED BY DEFAULT AS IDENTITY" in sql
    assert (
        "CREATE INDEX ix_audit_events_pending_delivery ON audit_events "
        "(guild_id, next_delivery_attempt_at, occurred_at, id)"
    ) in sql
    assert "WHERE delivered_at IS NULL" in sql
    assert "WHERE expires_at IS NOT NULL" in sql


def test_audit_revision_downgrade_removes_indexes_then_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = render_revision_operation(monkeypatch, "downgrade", AUDIT_REVISION)
    table = Base.metadata.tables["audit_events"]

    for index in table.indexes:
        if index.name in {
            "uq_audit_events_member_anniversary",
            "uq_audit_events_member_returned",
        }:
            continue
        assert f"DROP INDEX {index.name}" in sql
        assert sql.index(f"DROP INDEX {index.name}") < sql.index(
            "DROP TABLE audit_events"
        )


def test_anniversary_revision_adds_partial_unique_outbox_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", ANNIVERSARY_REVISION)
    )
    model_index = next(
        index
        for index in Base.metadata.tables["audit_events"].indexes
        if index.name == "uq_audit_events_member_anniversary"
    )
    model_sql = normalize_sql(
        str(CreateIndex(model_index).compile(dialect=postgresql.dialect()))
    )

    assert model_index.unique is True
    assert tuple(column.name for column in model_index.columns) == (
        "guild_id",
        "subject_id",
        "occurred_at",
    )
    assert str(model_index.dialect_options["postgresql"]["where"]) == (
        "event_type = 'member.anniversary'"
    )
    assert sql.rstrip(";") == model_sql


def test_anniversary_revision_downgrade_removes_only_its_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "downgrade", ANNIVERSARY_REVISION)
    )

    assert sql == "DROP INDEX uq_audit_events_member_anniversary;"


def test_member_return_revision_matches_model_partial_unique_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", MEMBER_RETURN_REVISION)
    )
    model_index = next(
        index
        for index in Base.metadata.tables["audit_events"].indexes
        if index.name == "uq_audit_events_member_returned"
    )
    model_sql = normalize_sql(
        str(CreateIndex(model_index).compile(dialect=postgresql.dialect()))
    )

    assert model_index.unique is True
    assert tuple(column.name for column in model_index.columns) == (
        "guild_id",
        "subject_id",
        "occurred_at",
    )
    assert str(model_index.dialect_options["postgresql"]["where"]) == (
        "event_type = 'member.returned'"
    )
    assert sql.rstrip(";") == model_sql


def test_member_return_revision_downgrade_removes_only_its_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "downgrade", MEMBER_RETURN_REVISION)
    )

    assert sql == "DROP INDEX uq_audit_events_member_returned;"


def test_discord_identity_revision_adds_nullable_text_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", DISCORD_IDENTITY_REVISION)
    )

    assert sql == (
        "ALTER TABLE discord_users ADD COLUMN username TEXT; "
        "ALTER TABLE discord_users ADD COLUMN global_name TEXT; "
        "ALTER TABLE guild_members ADD COLUMN nickname TEXT;"
    )
    assert "UPDATE " not in sql


def test_discord_identity_revision_downgrade_drops_added_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "downgrade", DISCORD_IDENTITY_REVISION)
    )
    assert sql == (
        "ALTER TABLE guild_members DROP COLUMN nickname; "
        "ALTER TABLE discord_users DROP COLUMN global_name; "
        "ALTER TABLE discord_users DROP COLUMN username;"
    )


def test_member_avatar_revision_adds_nullable_text_columns_without_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", MEMBER_AVATAR_REVISION)
    )

    assert sql == (
        "ALTER TABLE discord_users ADD COLUMN avatar_hash TEXT; "
        "ALTER TABLE guild_members ADD COLUMN guild_avatar_hash TEXT;"
    )
    assert "NOT NULL" not in sql
    assert "UPDATE " not in sql


def test_member_avatar_revision_downgrade_drops_added_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "downgrade", MEMBER_AVATAR_REVISION)
    )

    assert sql == (
        "ALTER TABLE guild_members DROP COLUMN guild_avatar_hash; "
        "ALTER TABLE discord_users DROP COLUMN avatar_hash;"
    )


def test_text_activity_revision_matches_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", TEXT_ACTIVITY_REVISION)
    )
    table = Base.metadata.tables["daily_text_activity"]

    assert "CREATE TABLE daily_text_activity" in sql
    for constraint in table.constraints:
        if constraint.name is not None:
            assert f"CONSTRAINT {constraint.name}" in sql
    for index in table.indexes:
        assert f"CREATE INDEX {index.name}" in sql
    assert "PRIMARY KEY (guild_id, user_id, channel_id, activity_date)" in sql
    assert "FOREIGN KEY(guild_id, user_id) REFERENCES guild_members" in sql


def test_text_activity_downgrade_removes_index_then_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = render_revision_operation(
        monkeypatch,
        "downgrade",
        TEXT_ACTIVITY_REVISION,
    )

    assert sql.index("DROP INDEX ix_daily_text_activity_guild_date_user") < sql.index(
        "DROP TABLE daily_text_activity"
    )


def test_achievements_revision_matches_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", ACHIEVEMENTS_REVISION)
    )
    table = Base.metadata.tables["user_achievements"]

    assert "CREATE TABLE user_achievements" in sql
    for constraint in table.constraints:
        if constraint.name is not None:
            assert f"CONSTRAINT {constraint.name}" in sql
    assert "PRIMARY KEY (guild_id, user_id, achievement_key)" in sql
    assert (
        "FOREIGN KEY(guild_id, user_id) REFERENCES guild_members (guild_id, user_id)"
        in sql
    )
    assert "unlocked_at TIMESTAMP WITH TIME ZONE NOT NULL" in sql
    assert "CREATE INDEX" not in sql


def test_achievements_revision_downgrade_drops_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = render_revision_operation(monkeypatch, "downgrade", ACHIEVEMENTS_REVISION)

    assert "DROP TABLE user_achievements" in sql


def test_downgrade_drops_indexes_and_tables_in_safe_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = render_revision_operation(monkeypatch, "downgrade")

    for index_name in REGULAR_INDEXES | PARTIAL_UNIQUE_INDEXES:
        assert f"DROP INDEX {index_name}" in sql

    reverse_table_order = tuple(reversed(TABLE_ORDER))
    positions = [
        sql.index(f"DROP TABLE {table_name}") for table_name in reverse_table_order
    ]
    assert positions == sorted(positions)


def test_web_admin_access_revision_matches_model_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", WEB_ADMIN_ACCESS_REVISION)
    )
    table = Base.metadata.tables["web_admin_access_grants"]

    assert "CREATE TABLE web_admin_access_grants" in sql
    for constraint in table.constraints:
        if constraint.name is not None:
            assert f"CONSTRAINT {constraint.name}" in sql

    for index in table.indexes:
        model_sql = normalize_sql(
            str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        )
        assert model_sql in sql

    active_index = next(
        index
        for index in table.indexes
        if index.name == "uq_web_admin_access_grants_active"
    )
    assert active_index.unique is True
    assert tuple(column.name for column in active_index.columns) == (
        "guild_id",
        "user_id",
    )
    assert str(active_index.dialect_options["postgresql"]["where"]) == (
        "revoked_at IS NULL"
    )
    assert "id BIGINT GENERATED BY DEFAULT AS IDENTITY" in sql


def test_web_admin_access_revision_downgrade_removes_indexes_then_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sql = normalize_sql(
        render_revision_operation(monkeypatch, "downgrade", WEB_ADMIN_ACCESS_REVISION)
    )

    for index_name in (
        "ix_web_admin_access_grants_guild_granted_at",
        "uq_web_admin_access_grants_active",
    ):
        assert f"DROP INDEX {index_name}" in sql
        assert sql.index(f"DROP INDEX {index_name}") < sql.index(
            "DROP TABLE web_admin_access_grants"
        )


def test_server_settings_revision_matches_model_and_downgrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", SERVER_SETTINGS_REVISION)
    )
    table = Base.metadata.tables["guild_server_settings"]

    assert "CREATE TABLE guild_server_settings" in upgrade_sql
    for constraint in table.constraints:
        if constraint.name is not None and "rules_publication" not in constraint.name:
            assert f"CONSTRAINT {constraint.name}" in upgrade_sql
    assert "FOREIGN KEY(guild_id) REFERENCES guilds (id)" in upgrade_sql
    assert "DEFAULT 'env' NOT NULL" in upgrade_sql

    downgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "downgrade", SERVER_SETTINGS_REVISION)
    )
    assert "DROP TABLE guild_server_settings" in downgrade_sql


def test_game_tracking_revision_matches_model_and_downgrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", GAME_TRACKING_REVISION)
    )
    table = Base.metadata.tables["game_sessions"]

    assert "CREATE TABLE game_sessions" in upgrade_sql
    for constraint in table.constraints:
        if constraint.name is not None:
            assert f"CONSTRAINT {constraint.name}" in upgrade_sql
    for index in table.indexes:
        model_sql = normalize_sql(
            str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        )
        assert model_sql in upgrade_sql
    assert "FOREIGN KEY(guild_id, user_id) REFERENCES guild_members" in upgrade_sql
    assert "id BIGINT GENERATED BY DEFAULT AS IDENTITY" in upgrade_sql
    assert "WHERE ended_at IS NULL" in upgrade_sql

    downgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "downgrade", GAME_TRACKING_REVISION)
    )
    assert downgrade_sql.index(
        "DROP INDEX uq_game_sessions_open_guild_user"
    ) < downgrade_sql.index("DROP TABLE game_sessions")


def test_operational_health_revision_matches_model_and_downgrades(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", OPERATIONAL_HEALTH_REVISION)
    )
    table = Base.metadata.tables["operational_health_observations"]

    assert "CREATE TABLE operational_health_observations" in upgrade_sql
    for constraint in table.constraints:
        if constraint.name is not None:
            assert f"CONSTRAINT {constraint.name}" in upgrade_sql
    for index in table.indexes:
        model_sql = normalize_sql(
            str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        )
        assert model_sql in upgrade_sql
    assert "FOREIGN KEY(guild_id) REFERENCES guilds (id)" in upgrade_sql

    downgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "downgrade", OPERATIONAL_HEALTH_REVISION)
    )
    assert downgrade_sql.index(
        "DROP INDEX ix_operational_health_guild_observed_at"
    ) < downgrade_sql.index("DROP TABLE operational_health_observations")


def test_rules_revision_matches_models_constraints_and_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", RULES_REVISION)
    )

    for table_name in ("rulesets", "rule_acceptances"):
        table = Base.metadata.tables[table_name]
        assert f"CREATE TABLE {table_name}" in upgrade_sql
        for constraint in table.constraints:
            if constraint.name is not None:
                if constraint.name == "ck_rulesets_reacceptance_grace_days":
                    continue
                assert f"CONSTRAINT {constraint.name}" in upgrade_sql
        for index in table.indexes:
            model_sql = normalize_sql(
                str(CreateIndex(index).compile(dialect=postgresql.dialect()))
            )
            assert model_sql in upgrade_sql

    assert "WHERE status = 'published'" in upgrade_sql
    assert "UNIQUE (guild_id, user_id, ruleset_id)" in upgrade_sql
    assert "FOREIGN KEY(guild_id, ruleset_id) REFERENCES rulesets" in upgrade_sql
    assert upgrade_sql.index("CREATE TABLE rulesets") < upgrade_sql.index(
        "CREATE TABLE rule_acceptances"
    )

    downgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "downgrade", RULES_REVISION)
    )
    assert downgrade_sql.index("DROP TABLE rule_acceptances") < downgrade_sql.index(
        "DROP TABLE rulesets"
    )


def test_rules_publication_revision_extends_server_settings_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", RULES_PUBLICATION_REVISION)
    )
    assert upgrade_sql.count("ALTER TABLE guild_server_settings ADD COLUMN") == 3
    assert "rules_publication_channel_id BIGINT" in upgrade_sql
    assert "rules_publication_message_id BIGINT" in upgrade_sql
    assert "rules_publication_ruleset_id BIGINT" in upgrade_sql
    assert (
        "FOREIGN KEY(guild_id, rules_publication_ruleset_id) REFERENCES rulesets"
        in upgrade_sql
    )
    assert "UPDATE rulesets" not in upgrade_sql
    assert "UPDATE rule_acceptances" not in upgrade_sql

    downgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "downgrade", RULES_PUBLICATION_REVISION)
    )
    assert downgrade_sql.count("DROP COLUMN") == 3


def test_rules_compliance_revision_adds_nullable_bounded_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "upgrade", RULES_COMPLIANCE_REVISION)
    )
    downgrade_sql = normalize_sql(
        render_revision_operation(monkeypatch, "downgrade", RULES_COMPLIANCE_REVISION)
    )

    assert "ADD COLUMN reacceptance_grace_days SMALLINT" in upgrade_sql
    assert "NOT NULL" not in upgrade_sql
    assert "CONSTRAINT ck_rulesets_reacceptance_grace_days CHECK" in upgrade_sql
    assert "reacceptance_grace_days BETWEEN 1 AND 365" in upgrade_sql
    assert "requires_reacceptance" in upgrade_sql
    assert downgrade_sql.index("DROP CONSTRAINT") < downgrade_sql.index(
        "DROP COLUMN reacceptance_grace_days"
    )
