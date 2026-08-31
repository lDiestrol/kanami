import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.config import Settings
from discord_stats_bot.features.server_analytics import (
    AnalyticsQuery,
    ServerAnalyticsPeriod,
    build_analytics_window,
)
from discord_stats_bot.persistence.database import create_database_resources
from discord_stats_bot.persistence.repositories.member_analytics import (
    SqlAlchemyMemberAnalyticsRepository,
    member_analytics_earliest_recorded_statement,
    member_analytics_text_rows_statement,
    member_analytics_voice_intervals_statement,
)


def query() -> AnalyticsQuery:
    return AnalyticsQuery(
        build_analytics_window(
            ServerAnalyticsPeriod.LAST_7_DAYS,
            datetime(2026, 8, 20, 12, tzinfo=UTC),
            report_timezone=ZoneInfo("UTC"),
        ),
        min_exact_session_seconds=10,
    )


def sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_voice_statement_is_member_scoped_and_reuses_eligibility() -> None:
    compiled = sql(member_analytics_voice_intervals_statement(10, 20, query()))

    assert "member_analytics_eligible_voice_sessions" in compiled
    assert "voice_intervals.guild_id = 10" in compiled
    assert "voice_intervals.user_id = 20" in compiled
    assert "voice_intervals.is_afk IS false" in compiled
    assert "discord_users.is_bot IS false" in compiled
    assert "voice_sessions.confirmed_through_at" in compiled
    assert "voice_intervals.quality = 'exact'" in compiled
    assert "2026-08-06 00:00:00+00:00" in compiled
    assert "2026-08-20 00:00:00+00:00" in compiled
    assert "guild_members" not in compiled


def test_text_statement_is_member_scoped_and_groups_combined_date_range() -> None:
    compiled = sql(member_analytics_text_rows_statement(10, 20, query()))

    assert "daily_text_activity.guild_id = 10" in compiled
    assert "daily_text_activity.user_id = 20" in compiled
    assert "sum(daily_text_activity.message_count)" in compiled
    assert (
        "GROUP BY daily_text_activity.user_id, daily_text_activity.activity_date"
        in compiled
    )
    assert "discord_users.is_bot IS false" in compiled
    assert "2026-08-06" in compiled
    assert "2026-08-20" in compiled
    assert "guild_members" not in compiled


def test_coverage_statement_is_member_specific_for_both_sources() -> None:
    compiled = sql(member_analytics_earliest_recorded_statement(10, 20))

    assert "min(voice_intervals.started_at)" in compiled
    assert "min(daily_text_activity.activity_date)" in compiled
    assert "voice_intervals.guild_id = 10" in compiled
    assert "voice_intervals.user_id = 20" in compiled
    assert "daily_text_activity.guild_id = 10" in compiled
    assert "daily_text_activity.user_id = 20" in compiled
    assert "voice_intervals.is_afk IS false" in compiled
    assert compiled.count("discord_users.is_bot IS false") == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgresql_reader_is_member_scoped_and_preserves_source_semantics() -> (
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
                        "ended_at TIMESTAMPTZ, "
                        "confirmed_through_at TIMESTAMPTZ NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE voice_intervals ("
                        "id BIGINT PRIMARY KEY, session_id BIGINT NOT NULL, "
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "channel_id BIGINT NOT NULL, started_at TIMESTAMPTZ NOT NULL, "
                        "ended_at TIMESTAMPTZ, quality TEXT NOT NULL, "
                        "channel_kind TEXT NOT NULL, is_afk BOOLEAN NOT NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE daily_text_activity ("
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "channel_id BIGINT NOT NULL, activity_date DATE NOT NULL, "
                        "message_count BIGINT NOT NULL, attachment_count BIGINT NOT NULL, "
                        "reply_count BIGINT NOT NULL, "
                        "PRIMARY KEY (guild_id, user_id, channel_id, activity_date))"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO discord_users (id, is_bot) VALUES "
                        "(20, false), (21, false), (22, true)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_sessions "
                        "(id, guild_id, user_id, started_at, ended_at, confirmed_through_at) "
                        "VALUES "
                        "(1, 10, 20, '2026-08-14 10:00+00', '2026-08-14 11:00+00', '2026-08-14 11:00+00'), "
                        "(2, 10, 21, '2026-08-14 10:00+00', '2026-08-14 11:00+00', '2026-08-14 11:00+00'), "
                        "(3, 11, 20, '2026-08-14 10:00+00', '2026-08-14 11:00+00', '2026-08-14 11:00+00'), "
                        "(4, 10, 22, '2026-08-14 10:00+00', '2026-08-14 11:00+00', '2026-08-14 11:00+00'), "
                        "(5, 10, 20, '2026-08-16 10:00+00', '2026-08-16 10:00:05+00', '2026-08-16 10:00:05+00'), "
                        "(6, 10, 20, '2026-08-15 10:00+00', '2026-08-15 10:40+00', '2026-08-15 10:40+00'), "
                        "(7, 10, 20, '2026-07-01 10:00+00', '2026-07-01 11:00+00', '2026-07-01 11:00+00'), "
                        "(8, 10, 20, '2026-08-10 10:00+00', '2026-08-10 10:45+00', '2026-08-10 10:45+00'), "
                        "(9, 10, 20, '2026-08-18 10:00+00', NULL, '2026-08-18 10:20+00'), "
                        "(10, 10, 20, '2026-08-05 23:30+00', '2026-08-06 00:30+00', '2026-08-06 00:30+00'), "
                        "(11, 10, 20, '2026-08-19 23:30+00', '2026-08-20 00:30+00', '2026-08-20 00:30+00'), "
                        "(12, 10, 20, '2026-08-17 10:00+00', '2026-08-17 11:00+00', '2026-08-17 11:00+00')"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_intervals "
                        "(id, session_id, guild_id, user_id, channel_id, started_at, "
                        "ended_at, quality, channel_kind, is_afk) VALUES "
                        "(1, 1, 10, 20, 100, '2026-08-14 10:00+00', '2026-08-14 11:00+00', 'exact', 'voice', false), "
                        "(2, 2, 10, 21, 100, '2026-08-14 10:00+00', '2026-08-14 11:00+00', 'exact', 'voice', false), "
                        "(3, 3, 11, 20, 100, '2026-08-14 10:00+00', '2026-08-14 11:00+00', 'exact', 'voice', false), "
                        "(4, 4, 10, 22, 100, '2026-08-14 10:00+00', '2026-08-14 11:00+00', 'exact', 'voice', false), "
                        "(5, 5, 10, 20, 100, '2026-08-16 10:00+00', '2026-08-16 10:00:05+00', 'exact', 'voice', false), "
                        "(6, 6, 10, 20, 100, '2026-08-15 10:00+00', '2026-08-15 10:30+00', 'exact', 'voice', false), "
                        "(7, 6, 10, 20, 101, '2026-08-15 10:30+00', '2026-08-15 10:40+00', 'estimated', 'stage', false), "
                        "(8, 7, 10, 20, 100, '2026-07-01 10:00+00', '2026-07-01 11:00+00', 'exact', 'voice', true), "
                        "(9, 8, 10, 20, 100, '2026-08-10 10:00+00', '2026-08-10 10:45+00', 'exact', 'voice', false), "
                        "(10, 9, 10, 20, 100, '2026-08-18 10:00+00', NULL, 'exact', 'voice', false), "
                        "(11, 10, 10, 20, 100, '2026-08-05 23:30+00', '2026-08-06 00:30+00', 'exact', 'voice', false), "
                        "(12, 11, 10, 20, 100, '2026-08-19 23:30+00', '2026-08-20 00:30+00', 'exact', 'voice', false), "
                        "(13, 12, 10, 20, 100, '2026-08-17 10:00+00', '2026-08-17 11:00+00', 'estimated', 'voice', false), "
                        "(14, 7, 10, 20, 100, '2026-08-01 10:00+00', '2026-08-01 11:00+00', 'exact', 'voice', false)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO daily_text_activity "
                        "(guild_id, user_id, channel_id, activity_date, message_count, "
                        "attachment_count, reply_count) VALUES "
                        "(10, 20, 200, '2026-08-14', 2, 0, 0), "
                        "(10, 20, 201, '2026-08-14', 3, 0, 0), "
                        "(10, 20, 200, '2026-08-10', 4, 0, 0), "
                        "(10, 20, 200, '2026-08-02', 1, 0, 0), "
                        "(10, 21, 200, '2026-08-01', 100, 0, 0), "
                        "(11, 20, 200, '2026-08-01', 100, 0, 0), "
                        "(10, 22, 200, '2026-08-01', 100, 0, 0)"
                    )
                )

                async with AsyncSession(
                    bind=connection, expire_on_commit=False
                ) as session:
                    repository = SqlAlchemyMemberAnalyticsRepository(session)
                    voice = await repository.list_voice_intervals(10, 20, query())
                    text_rows = await repository.list_text_rows(10, 20, query())
                    earliest = await repository.get_earliest_recorded(10, 20)

                assert {item.user_id for item in voice} == {20}
                assert [(item.user_id, item.message_count) for item in text_rows] == [
                    (20, 4),
                    (20, 5),
                ]
                assert (
                    sum(
                        int((item.ended_at - item.started_at).total_seconds())
                        for item in voice
                        if item.quality == "exact"
                    )
                    == 215 * 60
                )
                assert (
                    sum(
                        int((item.ended_at - item.started_at).total_seconds())
                        for item in voice
                        if item.quality == "estimated"
                    )
                    == 10 * 60
                )
                assert earliest.voice_started_at == datetime(2026, 8, 1, 10, tzinfo=UTC)
                assert earliest.text_activity_date.isoformat() == "2026-08-02"

                empty_voice = await repository.list_voice_intervals(10, 999, query())
                empty_text = await repository.list_text_rows(10, 999, query())
                empty_earliest = await repository.get_earliest_recorded(10, 999)
                assert empty_voice == ()
                assert empty_text == ()
                assert empty_earliest.voice_started_at is None
                assert empty_earliest.text_activity_date is None

                bot_voice = await repository.list_voice_intervals(10, 22, query())
                bot_text = await repository.list_text_rows(10, 22, query())
                bot_earliest = await repository.get_earliest_recorded(10, 22)
                assert bot_voice == ()
                assert bot_text == ()
                assert bot_earliest.voice_started_at is None
                assert bot_earliest.text_activity_date is None
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()
