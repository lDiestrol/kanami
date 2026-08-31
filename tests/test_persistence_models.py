from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.schema import (
    CreateIndex,
    CreateTable,
    ForeignKeyConstraint,
    Index,
    Table,
)

import discord_stats_bot.persistence.models as models_module
from discord_stats_bot.persistence.models import (
    AuditEvent,
    Base,
    DailyTextActivity,
    DiscordUser,
    GameSession,
    Guild,
    GuildMember,
    GuildServerSettings,
    OperationalHealthObservation,
    RuleAcceptance,
    Ruleset,
    UserAchievement,
    VoiceChannel,
    VoiceInterval,
    VoiceSession,
)

EXPECTED_TABLES = {
    "audit_events",
    "guilds",
    "discord_users",
    "daily_text_activity",
    "guild_members",
    "guild_server_settings",
    "game_sessions",
    "voice_channels",
    "voice_sessions",
    "voice_intervals",
    "user_achievements",
    "web_admin_access_grants",
    "operational_health_observations",
    "rule_acceptances",
    "rulesets",
}


def index_by_name(table: Table, name: str) -> Index:
    return next(index for index in table.indexes if index.name == name)


def test_all_business_models_register_on_shared_metadata() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert AuditEvent.__table__ is Base.metadata.tables["audit_events"]
    assert Guild.__table__ is Base.metadata.tables["guilds"]
    assert DiscordUser.__table__ is Base.metadata.tables["discord_users"]
    assert DailyTextActivity.__table__ is Base.metadata.tables["daily_text_activity"]
    assert GameSession.__table__ is Base.metadata.tables["game_sessions"]
    assert GuildMember.__table__ is Base.metadata.tables["guild_members"]
    assert (
        OperationalHealthObservation.__table__
        is Base.metadata.tables["operational_health_observations"]
    )
    assert RuleAcceptance.__table__ is Base.metadata.tables["rule_acceptances"]
    assert Ruleset.__table__ is Base.metadata.tables["rulesets"]
    assert (
        GuildServerSettings.__table__ is Base.metadata.tables["guild_server_settings"]
    )
    assert VoiceChannel.__table__ is Base.metadata.tables["voice_channels"]
    assert VoiceSession.__table__ is Base.metadata.tables["voice_sessions"]
    assert VoiceInterval.__table__ is Base.metadata.tables["voice_intervals"]
    assert UserAchievement.__table__ is Base.metadata.tables["user_achievements"]


def test_model_import_does_not_create_async_engine() -> None:
    engines = [
        value
        for value in vars(models_module).values()
        if isinstance(value, AsyncEngine)
    ]

    assert engines == []


def test_table_columns_match_approved_schema() -> None:
    expected_columns = {
        "guilds": {"id", "name"},
        "discord_users": {
            "id",
            "is_bot",
            "username",
            "global_name",
            "avatar_hash",
        },
        "daily_text_activity": {
            "guild_id",
            "user_id",
            "channel_id",
            "activity_date",
            "message_count",
            "attachment_count",
            "reply_count",
        },
        "guild_members": {
            "guild_id",
            "user_id",
            "joined_at",
            "left_at",
            "nickname",
            "guild_avatar_hash",
        },
        "guild_server_settings": {
            "guild_id",
            "autorole_role_mode",
            "autorole_role_id",
            "audit_log_channel_mode",
            "audit_log_channel_id",
            "anniversary_channel_mode",
            "anniversary_channel_id",
            "return_channel_mode",
            "return_channel_id",
            "rules_publication_channel_id",
            "rules_publication_message_id",
            "rules_publication_ruleset_id",
            "updated_at",
            "updated_by_user_id",
        },
        "game_sessions": {
            "id",
            "guild_id",
            "user_id",
            "game_key",
            "game_name",
            "application_id",
            "started_at",
            "confirmed_through_at",
            "ended_at",
        },
        "operational_health_observations": {
            "id",
            "guild_id",
            "observed_at",
            "overall_status",
            "discord_status",
            "postgresql_status",
            "voice_status",
            "game_status",
            "component",
            "reason",
        },
        "rulesets": {
            "id",
            "guild_id",
            "version",
            "title",
            "content",
            "status",
            "change_summary",
            "requires_reacceptance",
            "reacceptance_grace_days",
            "created_by",
            "created_at",
            "published_at",
        },
        "rule_acceptances": {
            "id",
            "guild_id",
            "user_id",
            "ruleset_id",
            "accepted_at",
        },
        "voice_channels": {
            "id",
            "guild_id",
            "name",
            "channel_kind",
            "is_afk",
        },
        "voice_sessions": {
            "id",
            "guild_id",
            "user_id",
            "started_at",
            "ended_at",
            "confirmed_through_at",
        },
        "voice_intervals": {
            "id",
            "session_id",
            "guild_id",
            "user_id",
            "channel_id",
            "started_at",
            "ended_at",
            "quality",
            "channel_kind",
            "is_afk",
        },
        "user_achievements": {
            "guild_id",
            "user_id",
            "achievement_key",
            "unlocked_at",
        },
    }

    for table_name, column_names in expected_columns.items():
        assert set(Base.metadata.tables[table_name].columns.keys()) == column_names


def test_primary_keys_match_approved_schema() -> None:
    expected_primary_keys = {
        "guilds": ("id",),
        "discord_users": ("id",),
        "daily_text_activity": (
            "guild_id",
            "user_id",
            "channel_id",
            "activity_date",
        ),
        "guild_members": ("guild_id", "user_id"),
        "guild_server_settings": ("guild_id",),
        "game_sessions": ("id",),
        "operational_health_observations": ("id",),
        "rulesets": ("id",),
        "rule_acceptances": ("id",),
        "voice_channels": ("id",),
        "voice_sessions": ("id",),
        "voice_intervals": ("id",),
        "user_achievements": ("guild_id", "user_id", "achievement_key"),
    }

    for table_name, column_names in expected_primary_keys.items():
        table = Base.metadata.tables[table_name]
        assert (
            tuple(column.name for column in table.primary_key.columns) == column_names
        )


def test_column_types_and_nullability_match_approved_schema() -> None:
    nullable_columns = {
        "guilds": {"name"},
        "discord_users": {"username", "global_name", "avatar_hash"},
        "daily_text_activity": set(),
        "guild_members": {
            "joined_at",
            "left_at",
            "nickname",
            "guild_avatar_hash",
        },
        "game_sessions": {"application_id", "ended_at"},
        "operational_health_observations": set(),
        "rulesets": {
            "change_summary",
            "created_by",
            "published_at",
            "reacceptance_grace_days",
        },
        "rule_acceptances": set(),
        "voice_channels": {"name"},
        "voice_sessions": {"ended_at"},
        "voice_intervals": {"ended_at"},
        "user_achievements": set(),
    }

    for table_name, expected_nullable in nullable_columns.items():
        table = Base.metadata.tables[table_name]
        actual_nullable = {column.name for column in table.columns if column.nullable}
        assert actual_nullable == expected_nullable

    bigint_columns = {
        "guilds": {"id"},
        "discord_users": {"id"},
        "daily_text_activity": {
            "guild_id",
            "user_id",
            "channel_id",
            "message_count",
            "attachment_count",
            "reply_count",
        },
        "guild_members": {"guild_id", "user_id"},
        "game_sessions": {"id", "guild_id", "user_id", "application_id"},
        "operational_health_observations": {"id", "guild_id"},
        "rulesets": {"id", "guild_id", "created_by"},
        "rule_acceptances": {"id", "guild_id", "user_id", "ruleset_id"},
        "voice_channels": {"id", "guild_id"},
        "voice_sessions": {"id", "guild_id", "user_id"},
        "voice_intervals": {
            "id",
            "session_id",
            "guild_id",
            "user_id",
            "channel_id",
        },
        "user_achievements": {"guild_id", "user_id"},
    }
    for table_name, column_names in bigint_columns.items():
        table = Base.metadata.tables[table_name]
        assert all(
            isinstance(table.c[column_name].type, BigInteger)
            for column_name in column_names
        )

    assert isinstance(Guild.__table__.c.name.type, Text)
    assert isinstance(DiscordUser.__table__.c.is_bot.type, Boolean)
    assert isinstance(DiscordUser.__table__.c.username.type, Text)
    assert isinstance(DiscordUser.__table__.c.global_name.type, Text)
    assert isinstance(DiscordUser.__table__.c.avatar_hash.type, Text)
    assert DiscordUser.__table__.c.avatar_hash.nullable is True
    assert isinstance(GuildMember.__table__.c.nickname.type, Text)
    assert isinstance(GuildMember.__table__.c.guild_avatar_hash.type, Text)
    assert GuildMember.__table__.c.guild_avatar_hash.nullable is True
    assert isinstance(GameSession.__table__.c.game_key.type, Text)
    assert isinstance(GameSession.__table__.c.game_name.type, Text)
    assert isinstance(OperationalHealthObservation.__table__.c.reason.type, Text)
    assert isinstance(VoiceChannel.__table__.c.name.type, Text)
    assert isinstance(VoiceChannel.__table__.c.channel_kind.type, Text)
    assert isinstance(VoiceChannel.__table__.c.is_afk.type, Boolean)
    assert isinstance(VoiceInterval.__table__.c.quality.type, Text)
    assert isinstance(VoiceInterval.__table__.c.channel_kind.type, Text)
    assert isinstance(VoiceInterval.__table__.c.is_afk.type, Boolean)
    assert isinstance(DailyTextActivity.__table__.c.activity_date.type, Date)
    assert isinstance(UserAchievement.__table__.c.achievement_key.type, Text)


def test_timestamp_columns_are_timezone_aware() -> None:
    timestamp_columns = {
        "guild_members": {"joined_at", "left_at"},
        "game_sessions": {"started_at", "confirmed_through_at", "ended_at"},
        "operational_health_observations": {"observed_at"},
        "rulesets": {"created_at", "published_at"},
        "rule_acceptances": {"accepted_at"},
        "voice_sessions": {"started_at", "ended_at", "confirmed_through_at"},
        "voice_intervals": {"started_at", "ended_at"},
        "user_achievements": {"unlocked_at"},
    }

    for table_name, column_names in timestamp_columns.items():
        table = Base.metadata.tables[table_name]
        for column_name in column_names:
            column_type = table.c[column_name].type
            assert isinstance(column_type, DateTime)
            assert column_type.timezone is True


def test_internal_session_ids_use_identity() -> None:
    for table in (
        VoiceSession.__table__,
        VoiceInterval.__table__,
        GameSession.__table__,
        OperationalHealthObservation.__table__,
        Ruleset.__table__,
        RuleAcceptance.__table__,
    ):
        identity = table.c.id.identity
        assert identity is not None
        assert identity.always is False

        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        id_definition = next(
            line.strip() for line in ddl.splitlines() if line.strip().startswith("id ")
        )
        assert "BIGINT GENERATED BY DEFAULT AS IDENTITY" in id_definition


def test_external_discord_ids_do_not_autoincrement() -> None:
    for table in (Guild.__table__, DiscordUser.__table__, VoiceChannel.__table__):
        id_column = table.c.id
        assert id_column.autoincrement is False
        assert id_column.identity is None

        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        id_definition = next(
            line.strip() for line in ddl.splitlines() if line.strip().startswith("id ")
        )
        assert id_definition.startswith("id BIGINT NOT NULL")
        assert "SERIAL" not in id_definition
        assert "IDENTITY" not in id_definition


def test_foreign_keys_match_approved_schema() -> None:
    expected_foreign_keys = {
        "guild_members": {
            "fk_guild_members_guild_id_guilds": (
                ("guild_id",),
                ("guilds.id",),
            ),
            "fk_guild_members_user_id_discord_users": (
                ("user_id",),
                ("discord_users.id",),
            ),
        },
        "daily_text_activity": {
            "fk_daily_text_activity_guild_member": (
                ("guild_id", "user_id"),
                ("guild_members.guild_id", "guild_members.user_id"),
            ),
        },
        "voice_channels": {
            "fk_voice_channels_guild_id_guilds": (
                ("guild_id",),
                ("guilds.id",),
            ),
        },
        "voice_sessions": {
            "fk_voice_sessions_guild_member": (
                ("guild_id", "user_id"),
                ("guild_members.guild_id", "guild_members.user_id"),
            ),
        },
        "game_sessions": {
            "fk_game_sessions_guild_member": (
                ("guild_id", "user_id"),
                ("guild_members.guild_id", "guild_members.user_id"),
            ),
        },
        "operational_health_observations": {
            "fk_operational_health_guild": (("guild_id",), ("guilds.id",)),
        },
        "voice_intervals": {
            "fk_voice_intervals_session_guild_user": (
                ("session_id", "guild_id", "user_id"),
                (
                    "voice_sessions.id",
                    "voice_sessions.guild_id",
                    "voice_sessions.user_id",
                ),
            ),
            "fk_voice_intervals_guild_channel": (
                ("guild_id", "channel_id"),
                ("voice_channels.guild_id", "voice_channels.id"),
            ),
        },
        "user_achievements": {
            "fk_user_achievements_guild_member": (
                ("guild_id", "user_id"),
                ("guild_members.guild_id", "guild_members.user_id"),
            ),
        },
    }

    for table_name, expected in expected_foreign_keys.items():
        table = Base.metadata.tables[table_name]
        actual = {}
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            actual[constraint.name] = (
                tuple(element.parent.name for element in constraint.elements),
                tuple(element.target_fullname for element in constraint.elements),
            )
        assert actual == expected


def test_unique_constraints_match_approved_schema() -> None:
    voice_channel_uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in VoiceChannel.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    voice_session_uniques = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in VoiceSession.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }

    assert voice_channel_uniques == {
        "uq_voice_channels_guild_id_id": ("guild_id", "id")
    }
    assert voice_session_uniques == {
        "uq_voice_sessions_id_guild_id_user_id": ("id", "guild_id", "user_id")
    }


def test_check_constraints_match_approved_schema() -> None:
    expected_checks = {
        "guilds": {"ck_guilds_id_positive": "id > 0"},
        "discord_users": {"ck_discord_users_id_positive": "id > 0"},
        "daily_text_activity": {
            "ck_daily_text_activity_channel_id_positive": "channel_id > 0",
            "ck_daily_text_activity_message_count_nonnegative": ("message_count >= 0"),
            "ck_daily_text_activity_attachment_count_nonnegative": (
                "attachment_count >= 0"
            ),
            "ck_daily_text_activity_reply_count_nonnegative": "reply_count >= 0",
        },
        "guild_members": {
            "ck_guild_members_membership_time_order": (
                "left_at IS NULL OR joined_at IS NULL OR left_at >= joined_at"
            )
        },
        "voice_channels": {
            "ck_voice_channels_id_positive": "id > 0",
            "ck_voice_channels_channel_kind": ("channel_kind IN ('voice', 'stage')"),
        },
        "voice_sessions": {
            "ck_voice_sessions_started_before_confirmed": (
                "started_at <= confirmed_through_at"
            ),
            "ck_voice_sessions_end_after_start": (
                "ended_at IS NULL OR ended_at >= started_at"
            ),
            "ck_voice_sessions_confirmed_before_end": (
                "ended_at IS NULL OR confirmed_through_at <= ended_at"
            ),
        },
        "game_sessions": {
            "ck_game_sessions_game_name_not_blank": "btrim(game_name) <> ''",
            "ck_game_sessions_game_key_not_blank": "btrim(game_key) <> ''",
            "ck_game_sessions_application_id_positive": (
                "application_id IS NULL OR application_id > 0"
            ),
            "ck_game_sessions_started_before_confirmed": (
                "started_at <= confirmed_through_at"
            ),
            "ck_game_sessions_end_after_start": (
                "ended_at IS NULL OR ended_at >= started_at"
            ),
            "ck_game_sessions_confirmed_before_end": (
                "ended_at IS NULL OR confirmed_through_at <= ended_at"
            ),
        },
        "operational_health_observations": {
            "ck_operational_health_overall_status": (
                "overall_status IN ('healthy', 'degraded', 'unavailable')"
            ),
            "ck_operational_health_discord_status": (
                "discord_status IN ('healthy', 'degraded', 'unavailable')"
            ),
            "ck_operational_health_postgresql_status": (
                "postgresql_status IN ('healthy', 'degraded', 'unavailable')"
            ),
            "ck_operational_health_voice_status": (
                "voice_status IN ('healthy', 'degraded', 'unavailable')"
            ),
            "ck_operational_health_game_status": (
                "game_status IN ('healthy', 'degraded', 'unavailable', 'neutral')"
            ),
            "ck_operational_health_component_not_blank": "btrim(component) <> ''",
            "ck_operational_health_reason_not_blank": "btrim(reason) <> ''",
        },
        "voice_intervals": {
            "ck_voice_intervals_end_after_start": (
                "ended_at IS NULL OR ended_at >= started_at"
            ),
            "ck_voice_intervals_quality": ("quality IN ('exact', 'estimated')"),
            "ck_voice_intervals_channel_kind": ("channel_kind IN ('voice', 'stage')"),
            "ck_voice_intervals_open_must_be_exact": (
                "ended_at IS NOT NULL OR quality = 'exact'"
            ),
        },
        "user_achievements": {
            "ck_user_achievements_guild_id_positive": "guild_id > 0",
            "ck_user_achievements_user_id_positive": "user_id > 0",
            "ck_user_achievements_key_length": (
                "char_length(achievement_key) BETWEEN 1 AND 128"
            ),
        },
    }

    for table_name, expected in expected_checks.items():
        table = Base.metadata.tables[table_name]
        actual = {
            constraint.name: str(constraint.sqltext)
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert actual == expected


def test_regular_indexes_match_approved_schema() -> None:
    assert UserAchievement.__table__.indexes == set()
    expected_indexes = {
        "daily_text_activity": {
            "ix_daily_text_activity_guild_date_user": (
                "guild_id",
                "activity_date",
                "user_id",
            ),
        },
        "voice_sessions": {
            "ix_voice_sessions_guild_user_started_at": (
                "guild_id",
                "user_id",
                "started_at",
            ),
        },
        "game_sessions": {
            "ix_game_sessions_guild_user_started_at": (
                "guild_id",
                "user_id",
                "started_at",
            ),
            "ix_game_sessions_guild_started_at": (
                "guild_id",
                "started_at",
            ),
        },
        "operational_health_observations": {
            "ix_operational_health_guild_observed_at": (
                "guild_id",
                "observed_at",
            ),
        },
        "voice_intervals": {
            "ix_voice_intervals_session_started_at": (
                "session_id",
                "started_at",
            ),
            "ix_voice_intervals_guild_user_started_at": (
                "guild_id",
                "user_id",
                "started_at",
            ),
            "ix_voice_intervals_guild_channel_started_at": (
                "guild_id",
                "channel_id",
                "started_at",
            ),
            "ix_voice_intervals_guild_started_at": (
                "guild_id",
                "started_at",
            ),
        },
    }

    for table_name, expected in expected_indexes.items():
        table = Base.metadata.tables[table_name]
        actual = {
            index.name: tuple(column.name for column in index.columns)
            for index in table.indexes
            if not index.unique
        }
        assert actual == expected


def test_partial_unique_indexes_match_approved_schema() -> None:
    expected_partial_indexes = {
        "voice_sessions": "uq_voice_sessions_open_guild_user",
        "voice_intervals": "uq_voice_intervals_open_guild_user",
        "game_sessions": "uq_game_sessions_open_guild_user",
    }

    for table_name, index_name in expected_partial_indexes.items():
        table = Base.metadata.tables[table_name]
        index = index_by_name(table, index_name)

        assert index.unique is True
        assert tuple(column.name for column in index.columns) == (
            "guild_id",
            "user_id",
        )
        where_clause = index.dialect_options["postgresql"]["where"]
        assert str(where_clause) == "ended_at IS NULL"

        ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        assert "UNIQUE INDEX" in ddl
        assert "WHERE ended_at IS NULL" in ddl
