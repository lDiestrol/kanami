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
from discord_stats_bot.persistence.repositories.server_analytics import (
    SqlAlchemyServerAnalyticsRepository,
    server_analytics_earliest_recorded_statement,
    server_analytics_text_rows_statement,
    server_analytics_voice_intervals_statement,
)


def query() -> AnalyticsQuery:
    window = build_analytics_window(
        ServerAnalyticsPeriod.LAST_7_DAYS,
        datetime(2026, 8, 20, 12, tzinfo=UTC),
        report_timezone=ZoneInfo("UTC"),
    )
    return AnalyticsQuery(window, min_exact_session_seconds=10)


def sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def test_voice_statement_reuses_eligibility_and_one_combined_range() -> None:
    compiled = sql(server_analytics_voice_intervals_statement(10, query()))

    assert "analytics_eligible_voice_sessions" in compiled
    assert "voice_intervals.is_afk IS false" in compiled
    assert "discord_users.is_bot IS false" in compiled
    assert "voice_sessions.confirmed_through_at" in compiled
    assert "voice_intervals.quality = 'exact'" in compiled
    assert "2026-08-06 00:00:00+00:00" in compiled
    assert "2026-08-20 00:00:00+00:00" in compiled
    assert "guild_members" not in compiled


def test_text_statement_groups_once_per_user_and_date_and_excludes_bots() -> None:
    compiled = sql(server_analytics_text_rows_statement(10, query()))

    assert "sum(daily_text_activity.message_count)" in compiled
    assert (
        "GROUP BY daily_text_activity.user_id, daily_text_activity.activity_date"
        in compiled
    )
    assert "discord_users.is_bot IS false" in compiled
    assert "2026-08-06" in compiled
    assert "2026-08-20" in compiled
    assert "guild_members" not in compiled


def test_coverage_statement_uses_earliest_recorded_rows_not_full_claim() -> None:
    compiled = sql(server_analytics_earliest_recorded_statement(10))

    assert "min(voice_intervals.started_at)" in compiled
    assert "min(daily_text_activity.activity_date)" in compiled
    assert "voice_intervals.is_afk IS false" in compiled
    assert compiled.count("discord_users.is_bot IS false") == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgresql_reader_preserves_voice_and_text_semantics() -> None:
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
                        "(20, false), (21, true), (22, false)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_sessions "
                        "(id, guild_id, user_id, started_at, ended_at, confirmed_through_at) "
                        "VALUES "
                        "(1, 10, 20, '2026-08-14 10:00+00', '2026-08-14 11:00+00', '2026-08-14 11:00+00'), "
                        "(2, 10, 21, '2026-08-14 10:00+00', '2026-08-14 11:00+00', '2026-08-14 11:00+00'), "
                        "(3, 10, 22, '2026-08-15 10:00+00', '2026-08-15 11:10+00', '2026-08-15 11:10+00'), "
                        "(4, 10, 20, '2026-08-16 10:00+00', '2026-08-16 10:00:05+00', '2026-08-16 10:00:05+00'), "
                        "(5, 10, 20, '2026-08-18 10:00+00', NULL, '2026-08-18 10:20+00'), "
                        "(6, 10, 20, '2026-08-19 10:00+00', '2026-08-19 11:00+00', '2026-08-19 11:00+00')"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_intervals "
                        "(id, session_id, guild_id, user_id, channel_id, started_at, "
                        "ended_at, quality, channel_kind, is_afk) VALUES "
                        "(1, 1, 10, 20, 100, '2026-08-14 10:00+00', '2026-08-14 11:00+00', 'exact', 'voice', false), "
                        "(2, 2, 10, 21, 100, '2026-08-14 10:00+00', '2026-08-14 11:00+00', 'exact', 'voice', false), "
                        "(3, 3, 10, 22, 100, '2026-08-15 10:00+00', '2026-08-15 10:30+00', 'exact', 'voice', false), "
                        "(4, 3, 10, 22, 101, '2026-08-15 10:30+00', '2026-08-15 11:00+00', 'exact', 'stage', false), "
                        "(5, 3, 10, 22, 101, '2026-08-15 11:00+00', '2026-08-15 11:10+00', 'estimated', 'stage', false), "
                        "(6, 4, 10, 20, 100, '2026-08-16 10:00+00', '2026-08-16 10:00:05+00', 'exact', 'voice', false), "
                        "(7, 5, 10, 20, 100, '2026-08-18 10:00+00', NULL, 'exact', 'voice', false), "
                        "(8, 6, 10, 20, 100, '2026-08-19 10:00+00', '2026-08-19 11:00+00', 'exact', 'voice', true)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO daily_text_activity "
                        "(guild_id, user_id, channel_id, activity_date, message_count, "
                        "attachment_count, reply_count) VALUES "
                        "(10, 20, 200, '2026-08-14', 2, 0, 0), "
                        "(10, 20, 201, '2026-08-14', 3, 0, 0), "
                        "(10, 21, 200, '2026-08-14', 100, 0, 0), "
                        "(10, 22, 200, '2026-08-15', 4, 0, 0)"
                    )
                )

                async with AsyncSession(
                    bind=connection, expire_on_commit=False
                ) as session:
                    repository = SqlAlchemyServerAnalyticsRepository(session)
                    voice = await repository.list_voice_intervals(10, query())
                    text_rows = await repository.list_text_rows(10, query())
                    earliest = await repository.get_earliest_recorded(10)

                assert {(item.user_id, item.quality) for item in voice} == {
                    (20, "exact"),
                    (22, "exact"),
                    (22, "estimated"),
                }
                assert (
                    sum(
                        int((item.ended_at - item.started_at).total_seconds())
                        for item in voice
                        if item.user_id == 20
                    )
                    == 80 * 60
                )
                assert (
                    sum(
                        int((item.ended_at - item.started_at).total_seconds())
                        for item in voice
                        if item.user_id == 22 and item.quality == "exact"
                    )
                    == 60 * 60
                )
                assert [(item.user_id, item.message_count) for item in text_rows] == [
                    (20, 5),
                    (22, 4),
                ]
                assert earliest.voice_started_at == datetime(
                    2026, 8, 14, 10, tzinfo=UTC
                )
                assert earliest.text_activity_date.isoformat() == "2026-08-14"
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()
