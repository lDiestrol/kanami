from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.text_activity import (
    TextActivityPeriod,
    TextActivityService,
    TextMessageActivity,
    TextUserMessageCount,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyTextActivityRepository,
)

ActivityKey = tuple[int, int, int, date]
ActivityCounters = tuple[int, int, int]


class InMemoryTextActivityRepository:
    def __init__(self) -> None:
        self.rows: dict[ActivityKey, ActivityCounters] = {}

    async def record_message(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        activity_date: date,
        attachment_count: int,
        is_reply: bool,
    ) -> None:
        key = (guild_id, user_id, channel_id, activity_date)
        messages, attachments, replies = self.rows.get(key, (0, 0, 0))
        self.rows[key] = (
            messages + 1,
            attachments + attachment_count,
            replies + int(is_reply),
        )

    async def get_user_message_counts(
        self,
        guild_id: int,
        started_on: date | None,
        ended_on: date,
        *,
        user_ids: tuple[int, ...] | None = None,
        limit: int | None = None,
    ) -> tuple[TextUserMessageCount, ...]:
        del guild_id, started_on, ended_on, user_ids, limit
        return ()


def make_service(
    *,
    timezone: str = "UTC",
) -> tuple[TextActivityService, InMemoryTextActivityRepository]:
    repository = InMemoryTextActivityRepository()
    return (
        TextActivityService(repository, report_timezone=ZoneInfo(timezone)),
        repository,
    )


@pytest.mark.asyncio
async def test_first_message_creates_daily_statistics() -> None:
    service, repository = make_service()

    await service.record_message(
        TextMessageActivity(10, 20, 30, datetime(2026, 8, 17, 12, tzinfo=UTC))
    )

    assert repository.rows == {(10, 20, 30, date(2026, 8, 17)): (1, 0, 0)}


@pytest.mark.asyncio
async def test_same_daily_key_increments_all_selected_counters() -> None:
    service, repository = make_service()
    occurred_at = datetime(2026, 8, 17, 12, tzinfo=UTC)

    await service.record_message(
        TextMessageActivity(
            10,
            20,
            30,
            occurred_at,
            attachment_count=2,
            is_reply=True,
        )
    )
    await service.record_message(
        TextMessageActivity(
            10,
            20,
            30,
            occurred_at,
            attachment_count=1,
        )
    )

    assert repository.rows[(10, 20, 30, date(2026, 8, 17))] == (2, 3, 1)


@pytest.mark.asyncio
async def test_channel_user_and_date_dimensions_are_independent() -> None:
    service, repository = make_service()
    observations = (
        TextMessageActivity(10, 20, 30, datetime(2026, 8, 17, tzinfo=UTC)),
        TextMessageActivity(10, 20, 31, datetime(2026, 8, 17, tzinfo=UTC)),
        TextMessageActivity(10, 21, 30, datetime(2026, 8, 17, tzinfo=UTC)),
        TextMessageActivity(10, 20, 30, datetime(2026, 8, 18, tzinfo=UTC)),
    )

    for observation in observations:
        await service.record_message(observation)

    assert repository.rows == {
        (10, 20, 30, date(2026, 8, 17)): (1, 0, 0),
        (10, 20, 31, date(2026, 8, 17)): (1, 0, 0),
        (10, 21, 30, date(2026, 8, 17)): (1, 0, 0),
        (10, 20, 30, date(2026, 8, 18)): (1, 0, 0),
    }


@pytest.mark.asyncio
async def test_report_timezone_controls_calendar_date_at_boundary() -> None:
    service, repository = make_service(timezone="Asia/Yekaterinburg")

    await service.record_message(
        TextMessageActivity(
            10,
            20,
            30,
            datetime(2026, 8, 17, 19, 0, tzinfo=UTC),
        )
    )
    await service.record_message(
        TextMessageActivity(
            10,
            20,
            30,
            datetime(2026, 8, 17, 18, 59, 59, tzinfo=UTC),
        )
    )

    assert set(key[3] for key in repository.rows) == {
        date(2026, 8, 17),
        date(2026, 8, 18),
    }


def test_domain_input_is_plain_typed_data_and_normalizes_to_utc() -> None:
    activity = TextMessageActivity(
        guild_id=10,
        user_id=20,
        channel_id=30,
        occurred_at=datetime(
            2026,
            8,
            17,
            23,
            tzinfo=ZoneInfo("Asia/Yekaterinburg"),
        ),
    )

    assert activity.occurred_at == datetime(2026, 8, 17, 18, tzinfo=UTC)
    assert (activity.guild_id, activity.user_id, activity.channel_id) == (10, 20, 30)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"channel_id": 0}, "channel_id must be positive"),
        ({"attachment_count": -1}, "attachment_count must not be negative"),
        ({"occurred_at": datetime(2026, 8, 17)}, "timezone-aware"),
    ],
)
def test_domain_input_rejects_invalid_metadata(
    kwargs: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "guild_id": 10,
        "user_id": 20,
        "channel_id": 30,
        "occurred_at": datetime(2026, 8, 17, tzinfo=UTC),
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        TextMessageActivity(**values)  # type: ignore[arg-type]


class RecordingSession:
    def __init__(self, rows: tuple[object, ...] = ()) -> None:
        self.statements: list[object] = []
        self._rows = rows

    async def execute(self, statement: object) -> object:
        self.statements.append(statement)
        return SimpleNamespace(all=lambda: list(self._rows))


@pytest.mark.asyncio
async def test_repository_uses_single_atomic_postgresql_upsert() -> None:
    session = RecordingSession()
    repository = SqlAlchemyTextActivityRepository(session)  # type: ignore[arg-type]

    await repository.record_message(
        guild_id=10,
        user_id=20,
        channel_id=30,
        activity_date=date(2026, 8, 17),
        attachment_count=2,
        is_reply=True,
    )

    assert len(session.statements) == 1
    sql = " ".join(
        str(
            session.statements[0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    )
    assert "ON CONFLICT (guild_id, user_id, channel_id, activity_date) DO UPDATE" in sql
    assert "message_count = (daily_text_activity.message_count +" in sql
    assert "attachment_count = (daily_text_activity.attachment_count +" in sql
    assert "reply_count = (daily_text_activity.reply_count +" in sql


@pytest.mark.asyncio
async def test_repository_reads_ranked_user_totals_for_inclusive_dates() -> None:
    session = RecordingSession(
        (
            SimpleNamespace(user_id=20, message_count=9),
            SimpleNamespace(user_id=21, message_count=3),
        )
    )
    repository = SqlAlchemyTextActivityRepository(session)  # type: ignore[arg-type]

    result = await repository.get_user_message_counts(
        10,
        date(2026, 8, 1),
        date(2026, 8, 17),
        user_ids=(20, 21),
        limit=10,
    )

    assert [(item.user_id, item.message_count) for item in result] == [(20, 9), (21, 3)]
    sql = " ".join(
        str(
            session.statements[0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    )
    assert "activity_date >= '2026-08-01'" in sql
    assert "activity_date <= '2026-08-17'" in sql
    assert "GROUP BY daily_text_activity.user_id" in sql
    assert "ORDER BY sum(daily_text_activity.message_count) DESC" in sql
    assert "daily_text_activity.user_id ASC" in sql
    assert "discord_users.is_bot IS false" in sql
    assert "LIMIT 10" in sql


@pytest.mark.asyncio
async def test_repository_empty_user_filter_avoids_query() -> None:
    session = RecordingSession()
    repository = SqlAlchemyTextActivityRepository(session)  # type: ignore[arg-type]

    result = await repository.get_user_message_counts(
        10,
        date(2026, 8, 1),
        date(2026, 8, 17),
        user_ids=(),
    )

    assert result == ()
    assert session.statements == []


class RecordingLeaderboardRepository(InMemoryTextActivityRepository):
    def __init__(self) -> None:
        super().__init__()
        self.read_calls: list[
            tuple[int, date | None, date, tuple[int, ...] | None, int | None]
        ] = []

    async def get_user_message_counts(
        self,
        guild_id: int,
        started_on: date | None,
        ended_on: date,
        *,
        user_ids: tuple[int, ...] | None = None,
        limit: int | None = None,
    ) -> tuple[TextUserMessageCount, ...]:
        self.read_calls.append((guild_id, started_on, ended_on, user_ids, limit))
        return (TextUserMessageCount(20, 7),)


@pytest.mark.parametrize(
    ("period", "expected_started_on"),
    [
        (TextActivityPeriod.TODAY, date(2026, 8, 18)),
        (TextActivityPeriod.LAST_7_DAYS, date(2026, 8, 12)),
        (TextActivityPeriod.LAST_30_DAYS, date(2026, 7, 20)),
        (TextActivityPeriod.ALL_TIME, None),
    ],
)
@pytest.mark.asyncio
async def test_leaderboard_uses_inclusive_report_timezone_calendar_periods(
    period: TextActivityPeriod,
    expected_started_on: date | None,
) -> None:
    repository = RecordingLeaderboardRepository()
    service = TextActivityService(
        repository,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
    )

    result = await service.get_leaderboard(
        10,
        period,
        datetime(2026, 8, 17, 19, 0, tzinfo=UTC),
        limit=10,
    )

    assert repository.read_calls == [
        (10, expected_started_on, date(2026, 8, 18), None, 10)
    ]
    assert result.period is period
    assert result.entries == (TextUserMessageCount(20, 7),)
