import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.config import Settings
from discord_stats_bot.features.member_returns import MemberReturnEvent
from discord_stats_bot.features.reference_data import (
    DiscordUserSnapshot,
    GuildMemberSnapshot,
    GuildSnapshot,
)
from discord_stats_bot.features.rules import (
    RulesComplianceService,
    RulesComplianceStatus,
)
from discord_stats_bot.features.server_settings import (
    GuildServerSettingKey,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
)
from discord_stats_bot.features.voice_statistics import (
    VoiceStatisticsPeriod,
    VoiceStatisticsQuery,
)
from discord_stats_bot.persistence.database import create_database_resources
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyAchievementRepository,
    SqlAlchemyGuildServerSettingsRepository,
    SqlAlchemyMemberReturnRepository,
    SqlAlchemyReferenceDataRepository,
    SqlAlchemyRulesRepository,
    SqlAlchemyVoiceStatisticsRepository,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rules_compliance_postgresql_history_and_member_scope() -> None:
    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="integration-test-placeholder",
        DISCORD_GUILD_ID=1,
        DATABASE_URL=test_database_url,
    )
    resources = create_database_resources(settings)
    published_at = datetime(2026, 8, 1, tzinfo=UTC)
    try:
        async with resources.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE discord_users ("
                        "id BIGINT PRIMARY KEY, is_bot BOOLEAN NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE guild_members ("
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "joined_at TIMESTAMPTZ, left_at TIMESTAMPTZ, nickname TEXT, "
                        "PRIMARY KEY (guild_id, user_id))"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE rulesets ("
                        "id BIGINT PRIMARY KEY, guild_id BIGINT NOT NULL, "
                        "version TEXT NOT NULL, title TEXT NOT NULL, "
                        "content TEXT NOT NULL, status TEXT NOT NULL, "
                        "change_summary TEXT, requires_reacceptance BOOLEAN NOT NULL, "
                        "reacceptance_grace_days SMALLINT, created_by BIGINT, "
                        "created_at TIMESTAMPTZ NOT NULL, published_at TIMESTAMPTZ)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE rule_acceptances ("
                        "id BIGINT PRIMARY KEY, guild_id BIGINT NOT NULL, "
                        "user_id BIGINT NOT NULL, ruleset_id BIGINT NOT NULL, "
                        "accepted_at TIMESTAMPTZ NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO discord_users (id, is_bot) VALUES "
                        "(20, false), (21, false), (22, false), (23, true)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO guild_members "
                        "(guild_id, user_id, joined_at, left_at) VALUES "
                        "(10, 20, :published_at, NULL), "
                        "(10, 21, :published_at, NULL), "
                        "(10, 22, :published_at, :left_at), "
                        "(10, 23, :published_at, NULL)"
                    ),
                    {
                        "published_at": published_at,
                        "left_at": published_at + timedelta(days=1),
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO rulesets "
                        "(id, guild_id, version, title, content, status, "
                        "requires_reacceptance, reacceptance_grace_days, "
                        "created_at, published_at) VALUES "
                        "(1, 10, '1.0', 'Rules', 'Text', 'archived', false, NULL, "
                        ":first_at, :first_at), "
                        "(2, 10, '1.1', 'Rules', 'Text', 'archived', true, 1, "
                        ":checkpoint_at, :checkpoint_at), "
                        "(3, 10, '1.2', 'Rules', 'Text', 'published', false, NULL, "
                        ":current_at, :current_at)"
                    ),
                    {
                        "first_at": published_at,
                        "checkpoint_at": published_at + timedelta(days=1),
                        "current_at": published_at + timedelta(days=2),
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO rule_acceptances "
                        "(id, guild_id, user_id, ruleset_id, accepted_at) VALUES "
                        "(1, 10, 20, 2, :accepted_at), "
                        "(2, 10, 22, 2, :accepted_at), "
                        "(3, 10, 23, 2, :accepted_at)"
                    ),
                    {"accepted_at": published_at + timedelta(days=2)},
                )
                async with AsyncSession(
                    bind=connection, expire_on_commit=False
                ) as session:
                    service = RulesComplianceService(SqlAlchemyRulesRepository(session))
                    compliant = await service.get_user_compliance(
                        10, 20, now=published_at + timedelta(days=3)
                    )
                    overdue = await service.get_user_compliance(
                        10, 21, now=published_at + timedelta(days=3)
                    )
                    summary = await service.summarize(
                        10, now=published_at + timedelta(days=3)
                    )

                assert compliant.status is RulesComplianceStatus.COMPLIANT
                assert overdue.status is RulesComplianceStatus.OVERDUE
                assert summary.total == 2
                assert summary.compliant == 1
                assert summary.pending == 0
                assert summary.overdue == 1
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_server_settings_postgresql_upsert_and_independent_overrides() -> None:
    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="integration-test-placeholder",
        DISCORD_GUILD_ID=1,
        DATABASE_URL=test_database_url,
    )
    resources = create_database_resources(settings)
    try:
        async with resources.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text("CREATE TEMP TABLE guilds (id BIGINT PRIMARY KEY, name TEXT)")
                )
                await connection.execute(text("INSERT INTO guilds VALUES (10, 'test')"))
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE guild_server_settings ("
                        "guild_id BIGINT PRIMARY KEY REFERENCES guilds(id), "
                        "autorole_role_mode TEXT NOT NULL DEFAULT 'env', "
                        "autorole_role_id BIGINT, "
                        "audit_log_channel_mode TEXT NOT NULL DEFAULT 'env', "
                        "audit_log_channel_id BIGINT, "
                        "anniversary_channel_mode TEXT NOT NULL DEFAULT 'env', "
                        "anniversary_channel_id BIGINT, "
                        "return_channel_mode TEXT NOT NULL DEFAULT 'env', "
                        "return_channel_id BIGINT, "
                        "rules_publication_channel_id BIGINT, "
                        "rules_publication_message_id BIGINT, "
                        "rules_publication_ruleset_id BIGINT, "
                        "updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                        "updated_by_user_id BIGINT)"
                    )
                )
                async with AsyncSession(
                    bind=connection, expire_on_commit=False
                ) as session:
                    repository = SqlAlchemyGuildServerSettingsRepository(session)
                    await repository.lock_guild(10)
                    await repository.save_override(
                        guild_id=10,
                        key=GuildServerSettingKey.AUTOROLE_ROLE,
                        override=GuildServerSettingOverride(
                            GuildServerSettingOverrideMode.VALUE, 20
                        ),
                        updated_at=datetime(2026, 8, 23, tzinfo=UTC),
                        updated_by_user_id=30,
                    )
                    await repository.save_override(
                        guild_id=10,
                        key=GuildServerSettingKey.AUDIT_LOG_CHANNEL,
                        override=GuildServerSettingOverride(
                            GuildServerSettingOverrideMode.DISABLED
                        ),
                        updated_at=datetime(2026, 8, 23, 1, tzinfo=UTC),
                        updated_by_user_id=30,
                    )
                    result = await repository.get_overrides(10)

                assert result is not None
                assert result.autorole_role.value == 20
                assert (
                    result.audit_log_channel.mode
                    is GuildServerSettingOverrideMode.DISABLED
                )
                assert (
                    result.anniversary_channel.mode
                    is GuildServerSettingOverrideMode.ENV
                )
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reference_identity_postgresql_upsert_null_and_membership_lifecycle() -> (
    None
):
    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="integration-test-placeholder",
        DISCORD_GUILD_ID=1,
        DATABASE_URL=test_database_url,
    )
    resources = create_database_resources(settings)
    joined_at = datetime(2025, 8, 21, 12, tzinfo=UTC)
    left_at = datetime(2026, 8, 21, 12, tzinfo=UTC)
    try:
        async with resources.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text("CREATE TEMP TABLE guilds (id BIGINT PRIMARY KEY, name TEXT)")
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE discord_users ("
                        "id BIGINT PRIMARY KEY, is_bot BOOLEAN NOT NULL, "
                        "username TEXT, global_name TEXT, avatar_hash TEXT)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE guild_members ("
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "joined_at TIMESTAMPTZ, left_at TIMESTAMPTZ, nickname TEXT, "
                        "guild_avatar_hash TEXT, "
                        "PRIMARY KEY (guild_id, user_id))"
                    )
                )
                async with AsyncSession(
                    bind=connection, expire_on_commit=False
                ) as session:
                    repository = SqlAlchemyReferenceDataRepository(session)
                    await repository.upsert_guild(GuildSnapshot(10, "First"))
                    await repository.upsert_guild(GuildSnapshot(11, "Second"))
                    await repository.upsert_users(
                        (
                            DiscordUserSnapshot(
                                20,
                                False,
                                "user",
                                "Global",
                                "0123456789abcdef0123456789abcdef",
                            ),
                        )
                    )
                    await repository.upsert_members(
                        (
                            GuildMemberSnapshot(
                                10,
                                20,
                                joined_at,
                                "First nick",
                                guild_avatar_hash=("abcdef0123456789abcdef0123456789"),
                            ),
                            GuildMemberSnapshot(11, 20, joined_at, "Second nick"),
                        )
                    )
                    await repository.mark_member_left(
                        guild_id=10,
                        user_id=20,
                        left_at=left_at,
                    )
                    departed = (
                        await session.execute(
                            text(
                                "SELECT username, global_name, avatar_hash, nickname, "
                                "guild_avatar_hash, left_at "
                                "FROM discord_users JOIN guild_members "
                                "ON discord_users.id = guild_members.user_id "
                                "WHERE guild_id = 10 AND user_id = 20"
                            )
                        )
                    ).one()
                    await repository.upsert_users(
                        (DiscordUserSnapshot(20, False, "renamed", None, None),)
                    )
                    await repository.upsert_members(
                        (GuildMemberSnapshot(10, 20, joined_at, None),)
                    )
                    rejoined = (
                        await session.execute(
                            text(
                                "SELECT username, global_name, avatar_hash, nickname, "
                                "guild_avatar_hash, left_at "
                                "FROM discord_users JOIN guild_members "
                                "ON discord_users.id = guild_members.user_id "
                                "WHERE guild_id = 10 AND user_id = 20"
                            )
                        )
                    ).one()
                    await repository.upsert_members(
                        (GuildMemberSnapshot(10, 20, joined_at, "Restored"),)
                    )
                    await repository.mark_member_left(
                        guild_id=10,
                        user_id=20,
                        left_at=left_at,
                    )
                    await repository.upsert_members(
                        (GuildMemberSnapshot(10, 20, joined_at, None, False),)
                    )
                    memberships = (
                        await session.execute(
                            text(
                                "SELECT guild_id, nickname, left_at "
                                "FROM guild_members "
                                "WHERE user_id = 20 ORDER BY guild_id"
                            )
                        )
                    ).all()

                assert departed == (
                    "user",
                    "Global",
                    "0123456789abcdef0123456789abcdef",
                    "First nick",
                    "abcdef0123456789abcdef0123456789",
                    left_at,
                )
                assert rejoined == ("renamed", None, None, None, None, None)
                assert memberships == [
                    (10, "Restored", left_at),
                    (11, "Second nick", None),
                ]
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_member_return_postgresql_partial_conflict_idempotency() -> None:
    """Exercise the exact model/migration/ON CONFLICT partial index contract."""

    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="integration-test-placeholder",
        DISCORD_GUILD_ID=1,
        DATABASE_URL=test_database_url,
    )
    resources = create_database_resources(settings)
    returned_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    event = MemberReturnEvent(
        10,
        20,
        returned_at - timedelta(days=2),
        returned_at,
        172_800,
        3_600,
        42,
        3,
        1,
    )
    try:
        async with resources.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE audit_events ("
                        "id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
                        "guild_id BIGINT NOT NULL, category TEXT NOT NULL, "
                        "event_type TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL, "
                        "subject_type TEXT NOT NULL, subject_id BIGINT NULL, "
                        "before_data JSONB NOT NULL, after_data JSONB NOT NULL, "
                        "details_data JSONB NOT NULL, delivery_attempts INTEGER NOT NULL, "
                        "expires_at TIMESTAMPTZ NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE UNIQUE INDEX uq_audit_events_member_returned "
                        "ON audit_events (guild_id, subject_id, occurred_at) "
                        "WHERE event_type = 'member.returned'"
                    )
                )
                async with AsyncSession(
                    bind=connection, expire_on_commit=False
                ) as session:
                    repository = SqlAlchemyMemberReturnRepository(session)
                    first = await repository.enqueue_member_return(event)
                    repeated = await repository.enqueue_member_return(event)
                    count = await session.scalar(
                        text("SELECT count(*) FROM audit_events")
                    )
                    details = await session.scalar(
                        text("SELECT details_data FROM audit_events")
                    )

                assert first is True
                assert repeated is False
                assert count == 1
                assert details["message_count"] == 42
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgresql_connection_smoke() -> None:
    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="integration-test-placeholder",
        DISCORD_GUILD_ID=1,
        DATABASE_URL=test_database_url,
    )
    resources = create_database_resources(settings)

    try:
        assert resources.engine.url.drivername == "postgresql+asyncpg"
        async with resources.engine.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await resources.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_achievement_unlock_postgresql_idempotency() -> None:
    """Exercise real PostgreSQL ON CONFLICT and first-unlock preservation."""

    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="integration-test-placeholder",
        DISCORD_GUILD_ID=1,
        DATABASE_URL=test_database_url,
    )
    resources = create_database_resources(settings)
    first_at = datetime(2026, 8, 17, 12, tzinfo=UTC)
    later_at = first_at + timedelta(days=1)
    try:
        async with resources.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE guild_members ("
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "PRIMARY KEY (guild_id, user_id))"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE user_achievements ("
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "achievement_key TEXT NOT NULL, unlocked_at TIMESTAMPTZ NOT NULL, "
                        "PRIMARY KEY (guild_id, user_id, achievement_key), "
                        "FOREIGN KEY (guild_id, user_id) "
                        "REFERENCES guild_members (guild_id, user_id))"
                    )
                )
                await connection.execute(
                    text("INSERT INTO guild_members VALUES (10, 20)")
                )
                async with AsyncSession(
                    bind=connection, expire_on_commit=False
                ) as session:
                    repository = SqlAlchemyAchievementRepository(session)
                    first = await repository.unlock_achievements(
                        guild_id=10,
                        user_id=20,
                        achievement_keys=("voice_10_hours", "voice_50_hours"),
                        unlocked_at=first_at,
                    )
                    repeated = await repository.unlock_achievements(
                        guild_id=10,
                        user_id=20,
                        achievement_keys=("voice_10_hours", "voice_50_hours"),
                        unlocked_at=later_at,
                    )
                    listed = await repository.list_unlocked(guild_id=10, user_id=20)
                    count = await session.scalar(
                        text("SELECT count(*) FROM user_achievements")
                    )

                assert tuple(item.achievement_key for item in first) == (
                    "voice_10_hours",
                    "voice_50_hours",
                )
                assert repeated == ()
                assert tuple(item.achievement_key for item in listed) == (
                    "voice_10_hours",
                    "voice_50_hours",
                )
                assert all(item.unlocked_at == first_at for item in listed)
                assert count == 2
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_voice_leaderboard_postgresql_aggregate_execution() -> None:
    """Execute the real aggregate against transaction-local temporary test data."""

    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="integration-test-placeholder",
        DISCORD_GUILD_ID=1,
        DATABASE_URL=test_database_url,
    )
    resources = create_database_resources(settings)
    as_of = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    try:
        async with resources.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE discord_users ("
                        "id BIGINT PRIMARY KEY, is_bot BOOLEAN NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE voice_sessions ("
                        "id BIGINT PRIMARY KEY, guild_id BIGINT NOT NULL, "
                        "user_id BIGINT NOT NULL, started_at TIMESTAMPTZ NOT NULL, "
                        "ended_at TIMESTAMPTZ NULL, "
                        "confirmed_through_at TIMESTAMPTZ NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE voice_intervals ("
                        "id BIGINT PRIMARY KEY, session_id BIGINT NOT NULL, "
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "channel_id BIGINT NOT NULL, "
                        "started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ NULL, "
                        "quality TEXT NOT NULL, is_afk BOOLEAN NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO discord_users (id, is_bot) VALUES "
                        "(1, false), (2, false), (3, true)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_sessions "
                        "(id, guild_id, user_id, started_at, ended_at, "
                        "confirmed_through_at) VALUES "
                        "(11, 10, 1, :u1_start, :as_of, :as_of), "
                        "(12, 10, 2, :u2_start, NULL, :u2_confirmed), "
                        "(13, 10, 3, :bot_start, :as_of, :as_of)"
                    ),
                    {
                        "u1_start": as_of - timedelta(seconds=120),
                        "u2_start": as_of - timedelta(seconds=200),
                        "u2_confirmed": as_of - timedelta(seconds=50),
                        "bot_start": as_of - timedelta(hours=1),
                        "as_of": as_of,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_intervals "
                        "(id, session_id, guild_id, user_id, channel_id, started_at, "
                        "ended_at, quality, is_afk) VALUES "
                        "(21, 11, 10, 1, 200, :u1_start, :u1_move, 'exact', false), "
                        "(24, 11, 10, 1, 100, :u1_move, :as_of, 'exact', false), "
                        "(22, 12, 10, 2, 100, :u2_start, NULL, 'exact', false), "
                        "(23, 13, 10, 3, 200, :bot_start, :as_of, 'exact', false)"
                    ),
                    {
                        "u1_start": as_of - timedelta(seconds=120),
                        "u1_move": as_of - timedelta(seconds=1),
                        "u2_start": as_of - timedelta(seconds=200),
                        "bot_start": as_of - timedelta(hours=1),
                        "as_of": as_of,
                    },
                )
                extra_users = tuple(range(4, 14))
                await connection.execute(
                    text(
                        "INSERT INTO discord_users (id, is_bot) "
                        "VALUES (:user_id, false)"
                    ),
                    [{"user_id": user_id} for user_id in extra_users],
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_sessions "
                        "(id, guild_id, user_id, started_at, ended_at, "
                        "confirmed_through_at) VALUES "
                        "(:session_id, 10, :user_id, :started_at, :as_of, :as_of)"
                    ),
                    [
                        {
                            "session_id": 100 + user_id,
                            "user_id": user_id,
                            "started_at": as_of - timedelta(seconds=10),
                            "as_of": as_of,
                        }
                        for user_id in extra_users
                    ],
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_intervals "
                        "(id, session_id, guild_id, user_id, channel_id, started_at, "
                        "ended_at, quality, is_afk) VALUES "
                        "(:interval_id, :session_id, 10, :user_id, 100, :started_at, "
                        ":as_of, 'exact', false)"
                    ),
                    [
                        {
                            "interval_id": 200 + user_id,
                            "session_id": 100 + user_id,
                            "user_id": user_id,
                            "started_at": as_of - timedelta(seconds=10),
                            "as_of": as_of,
                        }
                        for user_id in extra_users
                    ],
                )
                query = VoiceStatisticsQuery(
                    as_of,
                    as_of - timedelta(hours=12),
                    as_of - timedelta(days=7),
                    as_of - timedelta(days=30),
                    10,
                )
                session = AsyncSession(bind=connection, expire_on_commit=False)
                try:
                    repository = SqlAlchemyVoiceStatisticsRepository(session)
                    leaderboard = await repository.get_leaderboard(
                        10, VoiceStatisticsPeriod.ALL_TIME, query
                    )
                    user_channels = await repository.get_user_top_channels(10, 1, query)
                    channel_leaderboard = await repository.get_channel_leaderboard(
                        10, VoiceStatisticsPeriod.ALL_TIME, query
                    )
                    channel_statistics = await repository.get_channel_statistics(
                        10, 100, VoiceStatisticsPeriod.ALL_TIME, query
                    )
                finally:
                    await session.close()

                assert [entry.user_id for entry in leaderboard.entries[:2]] == [2, 1]
                assert [entry.total_seconds for entry in leaderboard.entries[:2]] == [
                    150,
                    120,
                ]
                assert [
                    (entry.channel_id, entry.total_seconds)
                    for entry in user_channels.entries
                ] == [(200, 119), (100, 1)]
                assert [
                    (entry.channel_id, entry.total_seconds)
                    for entry in channel_leaderboard.entries
                ] == [(100, 251), (200, 119)]
                assert channel_statistics.total_seconds == 251
                assert len(channel_statistics.entries) == 10
                assert channel_statistics.entries[0].user_id == 2
                assert 1 not in {entry.user_id for entry in channel_statistics.entries}
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_channelstats_postgresql_global_rounding_matches_channels() -> None:
    """Floor the selected channel aggregate after summing fractional durations."""

    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="integration-test-placeholder",
        DISCORD_GUILD_ID=1,
        DATABASE_URL=test_database_url,
    )
    resources = create_database_resources(settings)
    as_of = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    started_at = as_of - timedelta(seconds=100, microseconds=700_000)
    qualifying_started_at = as_of - timedelta(seconds=10, microseconds=700_000)
    fractional_channel_started_at = as_of - timedelta(microseconds=700_000)
    try:
        async with resources.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE discord_users ("
                        "id BIGINT PRIMARY KEY, is_bot BOOLEAN NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE voice_sessions ("
                        "id BIGINT PRIMARY KEY, guild_id BIGINT NOT NULL, "
                        "user_id BIGINT NOT NULL, started_at TIMESTAMPTZ NOT NULL, "
                        "ended_at TIMESTAMPTZ NULL, "
                        "confirmed_through_at TIMESTAMPTZ NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE voice_intervals ("
                        "id BIGINT PRIMARY KEY, session_id BIGINT NOT NULL, "
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "channel_id BIGINT NOT NULL, "
                        "started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ NULL, "
                        "quality TEXT NOT NULL, is_afk BOOLEAN NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO discord_users (id, is_bot) VALUES "
                        "(1, false), (2, false), (3, false), (4, false)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_sessions "
                        "(id, guild_id, user_id, started_at, ended_at, "
                        "confirmed_through_at) VALUES "
                        "(11, 10, 1, :started_at, :as_of, :as_of), "
                        "(12, 10, 2, :started_at, :as_of, :as_of), "
                        "(13, 10, 3, :qualifying_started_at, :as_of, :as_of), "
                        "(14, 10, 4, :qualifying_started_at, :as_of, :as_of)"
                    ),
                    {
                        "started_at": started_at,
                        "qualifying_started_at": qualifying_started_at,
                        "as_of": as_of,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_intervals "
                        "(id, session_id, guild_id, user_id, channel_id, started_at, "
                        "ended_at, quality, is_afk) VALUES "
                        "(21, 11, 10, 1, 100, :started_at, :as_of, 'exact', false), "
                        "(22, 12, 10, 2, 100, :started_at, :as_of, 'exact', false), "
                        "(23, 13, 10, 3, 300, :qualifying_started_at, "
                        ":fractional_started_at, 'exact', false), "
                        "(24, 13, 10, 3, 200, :fractional_started_at, "
                        ":as_of, 'exact', false), "
                        "(25, 14, 10, 4, 300, :qualifying_started_at, "
                        ":fractional_started_at, 'exact', false), "
                        "(26, 14, 10, 4, 200, :fractional_started_at, "
                        ":as_of, 'exact', false)"
                    ),
                    {
                        "started_at": started_at,
                        "qualifying_started_at": qualifying_started_at,
                        "fractional_started_at": fractional_channel_started_at,
                        "as_of": as_of,
                    },
                )
                query = VoiceStatisticsQuery(
                    as_of,
                    as_of - timedelta(hours=12),
                    as_of - timedelta(days=7),
                    as_of - timedelta(days=30),
                    10,
                )
                session = AsyncSession(bind=connection, expire_on_commit=False)
                try:
                    repository = SqlAlchemyVoiceStatisticsRepository(session)
                    channels = await repository.get_channel_leaderboard(
                        10, VoiceStatisticsPeriod.ALL_TIME, query
                    )
                    channelstats = await repository.get_channel_statistics(
                        10, 100, VoiceStatisticsPeriod.ALL_TIME, query
                    )
                    fractional_channelstats = await repository.get_channel_statistics(
                        10, 200, VoiceStatisticsPeriod.ALL_TIME, query
                    )
                finally:
                    await session.close()

                channel_totals = {
                    entry.channel_id: entry.exact_seconds for entry in channels.entries
                }
                assert channel_totals[100] == 201
                assert channel_totals[200] == 1
                assert channelstats.exact_seconds == 201
                assert [entry.exact_seconds for entry in channelstats.entries] == [
                    100,
                    100,
                ]
                assert channelstats.exact_seconds != sum(
                    entry.exact_seconds for entry in channelstats.entries
                )
                assert fractional_channelstats.exact_seconds == 1
                assert fractional_channelstats.entries == ()
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_voice_standings_postgresql_full_ranking_execution() -> None:
    """Execute full standings ranking, including positions outside TOP 10."""

    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="integration-test-placeholder",
        DISCORD_GUILD_ID=1,
        DATABASE_URL=test_database_url,
    )
    resources = create_database_resources(settings)
    as_of = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
    exact_durations = {
        1: 200,
        2: 190,
        3: 170,
        4: 180,
        5: 160,
        6: 160,
        7: 150,
        8: 140,
        9: 130,
        10: 120,
        11: 110,
        12: 100,
        13: 9,
        14: 1000,
        99: 5000,
    }
    try:
        async with resources.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE discord_users ("
                        "id BIGINT PRIMARY KEY, is_bot BOOLEAN NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE voice_sessions ("
                        "id BIGINT PRIMARY KEY, guild_id BIGINT NOT NULL, "
                        "user_id BIGINT NOT NULL, started_at TIMESTAMPTZ NOT NULL, "
                        "ended_at TIMESTAMPTZ NULL, "
                        "confirmed_through_at TIMESTAMPTZ NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE voice_intervals ("
                        "id BIGINT PRIMARY KEY, session_id BIGINT NOT NULL, "
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "channel_id BIGINT NOT NULL, "
                        "started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ NULL, "
                        "quality TEXT NOT NULL, is_afk BOOLEAN NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO discord_users (id, is_bot) VALUES (:id, :is_bot)"
                    ),
                    [
                        {"id": user_id, "is_bot": user_id == 99}
                        for user_id in (*range(1, 16), 99)
                    ],
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_sessions "
                        "(id, guild_id, user_id, started_at, ended_at, "
                        "confirmed_through_at) VALUES "
                        "(:id, 10, :user_id, :started_at, :as_of, :as_of)"
                    ),
                    [
                        {
                            "id": user_id,
                            "user_id": user_id,
                            "started_at": as_of
                            - timedelta(
                                seconds=(
                                    duration
                                    + (10 if user_id == 3 else 0)
                                    + (100 if user_id == 13 else 0)
                                )
                            ),
                            "as_of": as_of,
                        }
                        for user_id, duration in exact_durations.items()
                    ],
                )
                intervals = [
                    {
                        "id": user_id * 10,
                        "session_id": user_id,
                        "user_id": user_id,
                        "channel_id": 100,
                        "started_at": as_of - timedelta(seconds=duration),
                        "ended_at": as_of,
                        "quality": "exact",
                        "is_afk": user_id == 14,
                    }
                    for user_id, duration in exact_durations.items()
                ]
                intervals.extend(
                    (
                        {
                            "id": 31,
                            "session_id": 3,
                            "user_id": 3,
                            "channel_id": 100,
                            "started_at": as_of - timedelta(seconds=180),
                            "ended_at": as_of - timedelta(seconds=170),
                            "quality": "estimated",
                            "is_afk": False,
                        },
                        {
                            "id": 131,
                            "session_id": 13,
                            "user_id": 13,
                            "channel_id": 100,
                            "started_at": as_of - timedelta(seconds=109),
                            "ended_at": as_of - timedelta(seconds=9),
                            "quality": "estimated",
                            "is_afk": False,
                        },
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_intervals "
                        "(id, session_id, guild_id, user_id, channel_id, started_at, "
                        "ended_at, quality, is_afk) VALUES "
                        "(:id, :session_id, 10, :user_id, :channel_id, :started_at, "
                        ":ended_at, :quality, :is_afk)"
                    ),
                    intervals,
                )
                query = VoiceStatisticsQuery(
                    as_of,
                    as_of - timedelta(hours=12),
                    as_of - timedelta(days=7),
                    as_of - timedelta(days=30),
                    10,
                )
                session = AsyncSession(bind=connection, expire_on_commit=False)
                try:
                    repository = SqlAlchemyVoiceStatisticsRepository(session)
                    results = {
                        user_id: await repository.get_user_standings(10, user_id, query)
                        for user_id in (1, 3, 4, 5, 6, 11, 13, 15, 99)
                    }
                finally:
                    await session.close()

                assert results[1].all_time.rank == 1
                assert results[11].all_time.rank == 11
                assert results[4].all_time.rank < results[3].all_time.rank
                assert results[5].all_time.rank < results[6].all_time.rank
                assert results[13].all_time.rank is None
                assert results[15].all_time.rank is None
                assert results[99].all_time.rank is None
                assert {
                    result.all_time.participant_count for result in results.values()
                } == {12}
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()
