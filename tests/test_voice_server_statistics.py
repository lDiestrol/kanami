from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.voice_statistics import (
    VoiceChannelUsageEntry,
    VoiceLeaderboardEntry,
    VoiceServerStatistics,
    VoiceStatisticsPeriod,
    VoiceStatisticsQuery,
)
from discord_stats_bot.persistence.repositories.voice_statistics import (
    SqlAlchemyVoiceStatisticsRepository,
    voice_server_statistics_statement,
)

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Interval:
    session_id: int
    user_id: int
    channel_id: int
    started_at: datetime
    ended_at: datetime | None
    confirmed_through_at: datetime
    quality: str = "exact"
    is_afk: bool = False
    is_bot: bool = False


def query(*, threshold: int = 10) -> VoiceStatisticsQuery:
    return VoiceStatisticsQuery(
        as_of=AS_OF,
        today_started_at=AS_OF - timedelta(hours=12),
        last_7_days_started_at=AS_OF - timedelta(days=7),
        last_30_days_started_at=AS_OF - timedelta(days=30),
        min_exact_session_seconds=threshold,
    )


def interval(
    session_id: int,
    user_id: int,
    start_minutes: int,
    end_minutes: int | None,
    *,
    channel_id: int = 100,
    confirmed_minutes: int = 0,
    **kwargs: object,
) -> Interval:
    return Interval(
        session_id=session_id,
        user_id=user_id,
        channel_id=channel_id,
        started_at=AS_OF - timedelta(minutes=start_minutes),
        ended_at=(
            AS_OF - timedelta(minutes=end_minutes) if end_minutes is not None else None
        ),
        confirmed_through_at=AS_OF - timedelta(minutes=confirmed_minutes),
        **kwargs,  # type: ignore[arg-type]
    )


def aggregate_server(
    intervals: list[Interval],
    period: VoiceStatisticsPeriod,
    *,
    stats_query: VoiceStatisticsQuery | None = None,
) -> VoiceServerStatistics:
    """Semantic test double for the bounded production server SQL."""

    stats_query = stats_query or query()
    effective: list[tuple[Interval, datetime]] = []
    for item in intervals:
        if item.is_afk or item.is_bot:
            continue
        end = min(
            item.ended_at if item.ended_at is not None else item.confirmed_through_at,
            stats_query.as_of,
        )
        if end > item.started_at:
            effective.append((item, end))

    exact_by_session: defaultdict[int, int] = defaultdict(int)
    for item, end in effective:
        if item.quality == "exact":
            exact_by_session[item.session_id] += int(
                (end - item.started_at).total_seconds()
            )
    eligible_sessions = {
        session_id
        for session_id, seconds in exact_by_session.items()
        if seconds >= stats_query.min_exact_session_seconds
    }
    started_at = stats_query.started_at_for(period)
    user_totals: defaultdict[int, dict[str, int]] = defaultdict(
        lambda: {"exact": 0, "estimated": 0}
    )
    channel_totals: defaultdict[int, dict[str, int]] = defaultdict(
        lambda: {"exact": 0, "estimated": 0}
    )
    for item, end in effective:
        if item.session_id not in eligible_sessions:
            continue
        overlap_start = max(item.started_at, started_at or item.started_at)
        seconds = max(0, int((end - overlap_start).total_seconds()))
        if seconds == 0:
            continue
        user_totals[item.user_id][item.quality] += seconds
        channel_totals[item.channel_id][item.quality] += seconds

    users = sorted(
        (
            VoiceLeaderboardEntry(
                user_id=user_id,
                exact_seconds=durations["exact"],
                estimated_seconds=durations["estimated"],
            )
            for user_id, durations in user_totals.items()
        ),
        key=lambda entry: (-entry.total_seconds, -entry.exact_seconds, entry.user_id),
    )
    channels = sorted(
        (
            VoiceChannelUsageEntry(
                channel_id=channel_id,
                exact_seconds=durations["exact"],
                estimated_seconds=durations["estimated"],
            )
            for channel_id, durations in channel_totals.items()
        ),
        key=lambda entry: (
            -entry.total_seconds,
            -entry.exact_seconds,
            entry.channel_id,
        ),
    )
    return VoiceServerStatistics(
        as_of=stats_query.as_of,
        period=period,
        exact_seconds=sum(entry.exact_seconds for entry in users),
        estimated_seconds=sum(entry.estimated_seconds for entry in users),
        active_users=len(users),
        top_user=users[0] if users else None,
        top_channel=channels[0] if channels else None,
    )


def test_server_total_counts_member_hours_and_keeps_quality_separate() -> None:
    result = aggregate_server(
        [
            interval(1, 1, 60, 0),
            interval(2, 2, 60, 0),
            interval(3, 3, 60, 0, quality="estimated"),
            interval(3, 3, 70, 60, channel_id=999),
        ],
        VoiceStatisticsPeriod.ALL_TIME,
    )

    assert result.exact_seconds == 130 * 60
    assert result.estimated_seconds == 60 * 60
    assert result.total_seconds == 190 * 60
    assert result.active_users == 3
    assert result.average_seconds == result.total_seconds // 3


def test_server_semantics_exclude_bot_afk_short_and_cap_open_intervals() -> None:
    result = aggregate_server(
        [
            interval(1, 1, 60, None, confirmed_minutes=10),
            interval(2, 2, 40, None, confirmed_minutes=-10),
            interval(3, 3, 30, 0, is_bot=True),
            interval(4, 4, 30, 0, is_afk=True),
            interval(5, 5, 9, 0),
        ],
        VoiceStatisticsPeriod.ALL_TIME,
        stats_query=query(threshold=10 * 60),
    )

    assert result.total_seconds == 90 * 60
    assert result.active_users == 2
    assert result.top_user == VoiceLeaderboardEntry(1, 50 * 60, 0)


def test_reporting_window_clips_intervals_and_ignores_zero_overlap_users() -> None:
    stats_query = query()
    result = aggregate_server(
        [
            Interval(
                1,
                1,
                100,
                stats_query.last_7_days_started_at - timedelta(minutes=30),
                stats_query.last_7_days_started_at + timedelta(minutes=30),
                AS_OF,
            ),
            Interval(
                2,
                2,
                100,
                stats_query.last_7_days_started_at - timedelta(hours=2),
                stats_query.last_7_days_started_at,
                AS_OF,
            ),
        ],
        VoiceStatisticsPeriod.LAST_7_DAYS,
        stats_query=stats_query,
    )

    assert result.total_seconds == 30 * 60
    assert result.active_users == 1
    assert result.top_user == VoiceLeaderboardEntry(1, 30 * 60, 0)


def test_top_user_and_channel_use_existing_deterministic_orders() -> None:
    result = aggregate_server(
        [
            interval(1, 2, 50, 0, channel_id=200, quality="estimated"),
            interval(1, 2, 60, 50, channel_id=999),
            interval(2, 3, 60, 0, channel_id=100),
            interval(3, 1, 60, 0, channel_id=150),
        ],
        VoiceStatisticsPeriod.ALL_TIME,
    )

    assert result.top_user == VoiceLeaderboardEntry(1, 3600, 0)
    assert result.top_channel == VoiceChannelUsageEntry(100, 3600, 0)


def test_empty_server_report_has_no_top_entries_or_division_by_zero() -> None:
    result = aggregate_server([], VoiceStatisticsPeriod.LAST_7_DAYS)

    assert result.total_seconds == 0
    assert result.active_users == 0
    assert result.average_seconds == 0
    assert result.top_user is None
    assert result.top_channel is None


def test_server_statement_reuses_one_effective_eligible_aggregate() -> None:
    sql = str(
        voice_server_statistics_statement(
            10,
            VoiceStatisticsPeriod.LAST_7_DAYS,
            query(),
        ).compile(dialect=postgresql.dialect())
    ).lower()

    assert "server_effective_voice_intervals" in sql
    assert "server_eligible_voice_sessions" in sql
    assert sql.count("voice_intervals.is_afk is false") == 1
    assert sql.count("discord_users.is_bot is false") == 1
    assert "voice_server_user_totals" in sql
    assert "voice_server_channel_totals" in sql
    assert "ranked_voice_server_users" in sql
    assert "ranked_voice_server_channels" in sql
    assert "row_number() over" in sql
    assert "insert " not in sql
    assert "update " not in sql
    assert "delete " not in sql


@pytest.mark.asyncio
async def test_repository_maps_server_summary_and_top_entries() -> None:
    row = SimpleNamespace(
        server_exact_seconds=100,
        server_estimated_seconds=20,
        active_users=2,
        top_user_id=1,
        top_user_exact_seconds=70,
        top_user_estimated_seconds=10,
        top_channel_id=10,
        top_channel_exact_seconds=90,
        top_channel_estimated_seconds=5,
    )

    class Result:
        def one(self) -> object:
            return row

    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object) -> Result:
            self.statements.append(statement)
            return Result()

    session = Session()
    repository = SqlAlchemyVoiceStatisticsRepository(session)  # type: ignore[arg-type]

    result = await repository.get_server_statistics(
        10,
        VoiceStatisticsPeriod.LAST_7_DAYS,
        query(),
    )

    assert len(session.statements) == 1
    assert result.total_seconds == 120
    assert result.active_users == 2
    assert result.top_user == VoiceLeaderboardEntry(1, 70, 10)
    assert result.top_channel == VoiceChannelUsageEntry(10, 90, 5)


def test_server_dto_validates_values_and_average() -> None:
    report = VoiceServerStatistics(
        AS_OF,
        VoiceStatisticsPeriod.LAST_7_DAYS,
        100,
        1,
        3,
        VoiceLeaderboardEntry(1, 100, 1),
        VoiceChannelUsageEntry(10, 100, 1),
    )
    assert report.average_seconds == 33

    with pytest.raises(ValueError, match="must not be negative"):
        VoiceServerStatistics(
            AS_OF,
            VoiceStatisticsPeriod.ALL_TIME,
            -1,
            0,
            0,
            None,
            None,
        )
    with pytest.raises(ValueError, match="top user must be absent"):
        VoiceServerStatistics(
            AS_OF,
            VoiceStatisticsPeriod.ALL_TIME,
            0,
            0,
            0,
            VoiceLeaderboardEntry(1, 1, 0),
            None,
        )
