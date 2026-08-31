import os
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.config import Settings
from discord_stats_bot.persistence.database import create_database_resources
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyTextActivityRepository,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_text_activity_postgresql_upsert_and_read_execution() -> None:
    """Execute real ON CONFLICT aggregation against a temporary table."""

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
                        "INSERT INTO discord_users (id, is_bot) VALUES "
                        "(20, false), (21, false)"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TEMP TABLE daily_text_activity ("
                        "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                        "channel_id BIGINT NOT NULL, activity_date DATE NOT NULL, "
                        "message_count BIGINT NOT NULL, "
                        "attachment_count BIGINT NOT NULL, reply_count BIGINT NOT NULL, "
                        "PRIMARY KEY (guild_id, user_id, channel_id, activity_date))"
                    )
                )
                session = AsyncSession(bind=connection, expire_on_commit=False)
                try:
                    repository = SqlAlchemyTextActivityRepository(session)
                    messages = (
                        (20, 30, date(2026, 8, 17), 2, True),
                        (20, 30, date(2026, 8, 17), 1, False),
                        (20, 31, date(2026, 8, 17), 0, False),
                        (21, 30, date(2026, 8, 17), 0, False),
                        (20, 30, date(2026, 8, 18), 0, False),
                    )
                    for (
                        user_id,
                        channel_id,
                        activity_date,
                        attachments,
                        reply,
                    ) in messages:
                        await repository.record_message(
                            guild_id=10,
                            user_id=user_id,
                            channel_id=channel_id,
                            activity_date=activity_date,
                            attachment_count=attachments,
                            is_reply=reply,
                        )

                    stored = (
                        await connection.execute(
                            text(
                                "SELECT user_id, channel_id, activity_date, "
                                "message_count, attachment_count, reply_count "
                                "FROM daily_text_activity "
                                "ORDER BY activity_date, user_id, channel_id"
                            )
                        )
                    ).all()
                    totals = await repository.get_user_message_counts(
                        10,
                        date(2026, 8, 17),
                        date(2026, 8, 18),
                    )
                finally:
                    await session.close()

                assert [tuple(row) for row in stored] == [
                    (20, 30, date(2026, 8, 17), 2, 3, 1),
                    (20, 31, date(2026, 8, 17), 1, 0, 0),
                    (21, 30, date(2026, 8, 17), 1, 0, 0),
                    (20, 30, date(2026, 8, 18), 1, 0, 0),
                ]
                assert [(item.user_id, item.message_count) for item in totals] == [
                    (20, 4),
                    (21, 1),
                ]
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()
