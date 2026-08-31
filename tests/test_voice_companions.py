from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.voice_statistics import (
    VoiceChannelUsageEntry,
    VoiceCompanionEntry,
    VoicePairStatistics,
    VoiceStatisticsQuery,
    VoiceUserTopCompanions,
)
from discord_stats_bot.persistence.repositories.voice_statistics import (
    SqlAlchemyVoiceStatisticsRepository,
    voice_pair_statistics_statement,
    voice_user_top_companions_statement,
)

AS_OF = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)
TARGET_USER_ID = 1


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
    guild_id: int = 10


def query(*, threshold: int = 10) -> VoiceStatisticsQuery:
    return VoiceStatisticsQuery(
        as_of=AS_OF,
        today_started_at=AS_OF - timedelta(hours=12),
        last_7_days_started_at=AS_OF - timedelta(days=7),
        last_30_days_started_at=AS_OF - timedelta(days=30),
        min_exact_session_seconds=threshold,
    )


def aggregate_companions(
    intervals: list[Interval],
    *,
    stats_query: VoiceStatisticsQuery | None = None,
) -> VoiceUserTopCompanions:
    """Semantic test double for the bounded production SQL aggregate."""

    stats_query = stats_query or query()
    effective: list[tuple[Interval, datetime]] = []
    for interval in intervals:
        if interval.guild_id != 10 or interval.is_afk:
            continue
        end = min(
            interval.ended_at
            if interval.ended_at is not None
            else interval.confirmed_through_at,
            stats_query.as_of,
        )
        if end > interval.started_at:
            effective.append((interval, end))

    exact_by_session: defaultdict[int, int] = defaultdict(int)
    for interval, end in effective:
        if interval.quality == "exact":
            exact_by_session[interval.session_id] += int(
                (end - interval.started_at).total_seconds()
            )
    eligible_sessions = {
        session_id
        for session_id, seconds in exact_by_session.items()
        if seconds >= stats_query.min_exact_session_seconds
    }

    totals: defaultdict[int, dict[str, int]] = defaultdict(
        lambda: {"exact": 0, "estimated": 0}
    )
    targets = [
        item
        for item in effective
        if item[0].user_id == TARGET_USER_ID and item[0].session_id in eligible_sessions
    ]
    companions = [
        item
        for item in effective
        if item[0].user_id != TARGET_USER_ID
        and not item[0].is_bot
        and item[0].session_id in eligible_sessions
    ]
    for target, target_end in targets:
        for companion, companion_end in companions:
            if target.channel_id != companion.channel_id:
                continue
            started_at = max(target.started_at, companion.started_at)
            ended_at = min(target_end, companion_end)
            seconds = max(0, int((ended_at - started_at).total_seconds()))
            if seconds == 0:
                continue
            quality = (
                "exact"
                if target.quality == companion.quality == "exact"
                else "estimated"
            )
            totals[companion.user_id][quality] += seconds

    entries = sorted(
        (
            VoiceCompanionEntry(
                user_id=user_id,
                exact_seconds=durations["exact"],
                estimated_seconds=durations["estimated"],
            )
            for user_id, durations in totals.items()
        ),
        key=lambda entry: (-entry.total_seconds, -entry.exact_seconds, entry.user_id),
    )[:3]
    return VoiceUserTopCompanions(AS_OF, tuple(entries))


def aggregate_pair(
    intervals: list[Interval],
    *,
    user1_id: int = 1,
    user2_id: int = 2,
    stats_query: VoiceStatisticsQuery | None = None,
) -> VoicePairStatistics:
    """Semantic test double for the production pair SQL statement."""

    stats_query = stats_query or query()
    effective: list[tuple[Interval, datetime]] = []
    for item in intervals:
        if item.guild_id != 10 or item.is_afk or item.is_bot:
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
    eligible = [item for item in effective if item[0].session_id in eligible_sessions]

    def user_total(user_id: int) -> int:
        return sum(
            int((end - item.started_at).total_seconds())
            for item, end in eligible
            if item.user_id == user_id
        )

    channel_totals: defaultdict[int, dict[str, int]] = defaultdict(
        lambda: {"exact": 0, "estimated": 0}
    )
    user1_intervals = [item for item in eligible if item[0].user_id == user1_id]
    user2_intervals = [item for item in eligible if item[0].user_id == user2_id]
    for user1_interval, user1_end in user1_intervals:
        for user2_interval, user2_end in user2_intervals:
            if user1_interval.channel_id != user2_interval.channel_id:
                continue
            started_at = max(user1_interval.started_at, user2_interval.started_at)
            ended_at = min(user1_end, user2_end)
            seconds = max(0, int((ended_at - started_at).total_seconds()))
            if seconds == 0:
                continue
            quality = (
                "exact"
                if user1_interval.quality == user2_interval.quality == "exact"
                else "estimated"
            )
            channel_totals[user1_interval.channel_id][quality] += seconds

    all_channels = tuple(
        VoiceChannelUsageEntry(
            channel_id=channel_id,
            exact_seconds=durations["exact"],
            estimated_seconds=durations["estimated"],
        )
        for channel_id, durations in channel_totals.items()
    )
    ranked_channels = tuple(
        sorted(
            all_channels,
            key=lambda entry: (
                -entry.total_seconds,
                -entry.exact_seconds,
                entry.channel_id,
            ),
        )[:3]
    )
    return VoicePairStatistics(
        as_of=stats_query.as_of,
        user1_id=user1_id,
        user2_id=user2_id,
        exact_seconds=sum(entry.exact_seconds for entry in all_channels),
        estimated_seconds=sum(entry.estimated_seconds for entry in all_channels),
        user1_total_seconds=user_total(user1_id),
        user2_total_seconds=user_total(user2_id),
        channels=ranked_channels,
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


def test_same_channel_overlap_counts_but_disjoint_and_other_channel_do_not() -> None:
    result = aggregate_companions(
        [
            interval(1, 1, 180, 0),
            interval(2, 2, 105, 30),
            interval(3, 3, 240, 190),
            interval(4, 4, 120, 60, channel_id=200),
        ]
    )

    assert result.entries == (VoiceCompanionEntry(2, 75 * 60, 0),)
    assert all(entry.user_id != TARGET_USER_ID for entry in result.entries)


@pytest.mark.parametrize(
    ("target_quality", "companion_quality", "exact", "estimated"),
    [
        ("exact", "exact", 600, 0),
        ("exact", "estimated", 0, 600),
        ("estimated", "estimated", 0, 600),
    ],
)
def test_pair_quality_uses_exact_only_when_both_intervals_are_exact(
    target_quality: str,
    companion_quality: str,
    exact: int,
    estimated: int,
) -> None:
    # Each logical session also has enough exact time outside the overlap so
    # estimated pair variants remain eligible under the existing threshold.
    result = aggregate_companions(
        [
            interval(1, 1, 30, 20, quality="exact", channel_id=999),
            interval(1, 1, 20, 10, quality=target_quality),
            interval(2, 2, 30, 20, quality="exact", channel_id=998),
            interval(2, 2, 20, 10, quality=companion_quality),
        ]
    )

    assert result.entries == (VoiceCompanionEntry(2, exact, estimated),)


def test_afk_bot_short_session_and_open_caps_follow_existing_semantics() -> None:
    result = aggregate_companions(
        [
            interval(1, 1, 60, None, confirmed_minutes=10),
            interval(2, 2, 40, None, confirmed_minutes=20),
            interval(3, 3, 40, 20, is_afk=True),
            interval(4, 4, 40, 20, is_bot=True),
            interval(5, 5, 9, 0),
            interval(6, 6, 30, None, confirmed_minutes=-10),
        ],
        stats_query=query(threshold=10 * 60),
    )

    assert result.entries == (
        VoiceCompanionEntry(2, 20 * 60, 0),
        VoiceCompanionEntry(6, 20 * 60, 0),
    )


def test_multiple_overlaps_aggregate_and_top_three_order_is_deterministic() -> None:
    result = aggregate_companions(
        [
            interval(1, 1, 60, 30),
            interval(2, 1, 20, 0),
            interval(3, 2, 60, 30),
            interval(4, 2, 20, 10),
            interval(5, 3, 60, 20),
            interval(6, 4, 60, 20, quality="estimated"),
            interval(6, 4, 70, 60, channel_id=999),
            interval(7, 5, 60, 30),
        ]
    )

    assert result.entries == (
        VoiceCompanionEntry(2, 40 * 60, 0),
        VoiceCompanionEntry(3, 30 * 60, 0),
        VoiceCompanionEntry(5, 30 * 60, 0),
    )


def test_group_conversation_counts_each_pair_independently() -> None:
    result = aggregate_companions(
        [
            interval(1, 1, 60, 0),
            interval(2, 2, 60, 0),
            interval(3, 3, 60, 0),
        ]
    )

    assert result.entries == (
        VoiceCompanionEntry(2, 3600, 0),
        VoiceCompanionEntry(3, 3600, 0),
    )
    assert sum(entry.total_seconds for entry in result.entries) == 7200


def test_companion_statement_reuses_effective_and_eligibility_sql() -> None:
    sql = str(
        voice_user_top_companions_statement(10, 1, query()).compile(
            dialect=postgresql.dialect()
        )
    ).lower()

    assert "target_eligible_voice_sessions" in sql
    assert "companion_eligible_voice_sessions" in sql
    assert sql.count("voice_intervals.is_afk is false") == 2
    assert "discord_users.is_bot is false" in sql
    assert "least(voice_sessions.confirmed_through_at" in sql
    assert "companion_effective_voice_intervals.channel_id = " in sql
    assert "target_effective_voice_intervals.channel_id" in sql
    assert "greatest(" in sql
    assert "least(" in sql
    assert "order by" in sql
    assert "limit" in sql
    assert "insert " not in sql
    assert "update " not in sql
    assert "delete " not in sql


@pytest.mark.asyncio
async def test_repository_maps_companion_rows_without_transaction_control() -> None:
    rows = [
        SimpleNamespace(user_id=2, exact_seconds=90, estimated_seconds=10),
        SimpleNamespace(user_id=3, exact_seconds=80, estimated_seconds=0),
    ]

    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object) -> list[object]:
            self.statements.append(statement)
            return rows

    session = Session()
    repository = SqlAlchemyVoiceStatisticsRepository(session)  # type: ignore[arg-type]

    result = await repository.get_user_top_companions(10, 1, query())

    assert len(session.statements) == 1
    assert result.entries == (
        VoiceCompanionEntry(2, 90, 10),
        VoiceCompanionEntry(3, 80, 0),
    )


def test_companion_dtos_validate_durations_and_top_three_limit() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        VoiceCompanionEntry(1, -1, 0)
    with pytest.raises(ValueError, match="at most 3"):
        VoiceUserTopCompanions(
            AS_OF,
            tuple(VoiceCompanionEntry(user_id, 1, 0) for user_id in range(1, 5)),
        )


def test_pair_counts_only_positive_overlap_in_the_same_channel() -> None:
    result = aggregate_pair(
        [
            interval(1, 1, 180, 0),
            interval(2, 2, 105, 30),
            interval(3, 2, 240, 190),
            interval(4, 2, 120, 60, channel_id=200),
        ]
    )

    assert result.exact_seconds == 75 * 60
    assert result.estimated_seconds == 0
    assert result.channels == (VoiceChannelUsageEntry(100, 75 * 60, 0),)
    assert result.user1_total_seconds == 180 * 60
    assert result.user2_total_seconds == 185 * 60


@pytest.mark.parametrize(
    ("user1_quality", "user2_quality", "exact", "estimated"),
    [
        ("exact", "exact", 600, 0),
        ("exact", "estimated", 0, 600),
        ("estimated", "exact", 0, 600),
        ("estimated", "estimated", 0, 600),
    ],
)
def test_pair_quality_semantics_are_symmetric(
    user1_quality: str,
    user2_quality: str,
    exact: int,
    estimated: int,
) -> None:
    intervals = [
        interval(1, 1, 30, 20, quality="exact", channel_id=999),
        interval(1, 1, 20, 10, quality=user1_quality),
        interval(2, 2, 30, 20, quality="exact", channel_id=998),
        interval(2, 2, 20, 10, quality=user2_quality),
    ]

    forward = aggregate_pair(intervals)
    reverse = aggregate_pair(intervals, user1_id=2, user2_id=1)

    assert (forward.exact_seconds, forward.estimated_seconds) == (exact, estimated)
    assert (reverse.exact_seconds, reverse.estimated_seconds) == (exact, estimated)
    assert reverse.channels == forward.channels
    assert reverse.user1_total_seconds == forward.user2_total_seconds
    assert reverse.user2_total_seconds == forward.user1_total_seconds


def test_pair_applies_afk_bot_eligibility_and_open_interval_caps() -> None:
    result = aggregate_pair(
        [
            interval(1, 1, 60, None, confirmed_minutes=10),
            interval(2, 2, 40, None, confirmed_minutes=-10),
            interval(3, 1, 30, 20, is_afk=True),
            interval(4, 2, 30, 20, is_afk=True),
            interval(5, 1, 9, 0, channel_id=300),
            interval(6, 2, 9, 0, channel_id=300),
            interval(7, 1, 30, 0, channel_id=400, is_bot=True),
            interval(8, 2, 30, 0, channel_id=400),
        ],
        stats_query=query(threshold=10 * 60),
    )

    assert result.channels == (VoiceChannelUsageEntry(100, 30 * 60, 0),)
    assert result.user1_total_seconds == 50 * 60
    assert result.user2_total_seconds == 40 * 60 + 30 * 60


def test_pair_channel_totals_rank_top_three_without_losing_pair_total() -> None:
    result = aggregate_pair(
        [
            interval(1, 1, 60, 0, channel_id=100),
            interval(2, 2, 60, 30, channel_id=100),
            interval(3, 1, 60, 0, channel_id=200),
            interval(4, 2, 60, 30, channel_id=200, quality="estimated"),
            interval(4, 2, 70, 60, channel_id=999),
            interval(5, 1, 60, 0, channel_id=150),
            interval(6, 2, 60, 30, channel_id=150),
            interval(7, 1, 20, 0, channel_id=50),
            interval(8, 2, 20, 0, channel_id=50),
        ]
    )

    assert result.total_seconds == 110 * 60
    assert [entry.channel_id for entry in result.channels] == [100, 150, 200]
    assert result.channels[0].exact_seconds == 30 * 60
    assert result.channels[2].estimated_seconds == 30 * 60


def test_pair_aggregates_multiple_overlaps_into_one_channel_total() -> None:
    result = aggregate_pair(
        [
            interval(1, 1, 60, 40),
            interval(2, 2, 60, 40),
            interval(3, 1, 30, 10),
            interval(4, 2, 30, 10),
        ]
    )

    assert result.channels == (VoiceChannelUsageEntry(100, 40 * 60, 0),)
    assert result.total_seconds == 40 * 60


def test_pair_statement_is_one_bounded_read_only_aggregate() -> None:
    sql = str(
        voice_pair_statistics_statement(10, 1, 2, query()).compile(
            dialect=postgresql.dialect()
        )
    ).lower()

    assert "pair_user1_eligible_voice_sessions" in sql
    assert "pair_user2_eligible_voice_sessions" in sql
    assert sql.count("discord_users.is_bot is false") == 2
    assert sql.count("voice_intervals.is_afk is false") == 2
    assert "pair_user1_effective_voice_intervals.channel_id = " in sql
    assert "pair_user2_effective_voice_intervals.channel_id" in sql
    assert "greatest(" in sql
    assert "least(" in sql
    assert "ranked_voice_pair_channels" in sql
    assert "row_number() over" in sql
    assert "insert " not in sql
    assert "update " not in sql
    assert "delete " not in sql


@pytest.mark.asyncio
async def test_repository_maps_pair_summary_and_ranked_channels() -> None:
    rows = [
        SimpleNamespace(
            pair_exact_seconds=100,
            pair_estimated_seconds=20,
            user1_total_seconds=240,
            user2_total_seconds=120,
            channel_id=10,
            exact_seconds=80,
            estimated_seconds=0,
        ),
        SimpleNamespace(
            pair_exact_seconds=100,
            pair_estimated_seconds=20,
            user1_total_seconds=240,
            user2_total_seconds=120,
            channel_id=20,
            exact_seconds=20,
            estimated_seconds=20,
        ),
    ]

    class Result:
        def all(self) -> list[object]:
            return rows

    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def execute(self, statement: object) -> Result:
            self.statements.append(statement)
            return Result()

    session = Session()
    repository = SqlAlchemyVoiceStatisticsRepository(session)  # type: ignore[arg-type]

    result = await repository.get_pair_statistics(10, 1, 2, query())

    assert len(session.statements) == 1
    assert result.total_seconds == 120
    assert result.user1_total_seconds == 240
    assert result.user2_total_seconds == 120
    assert result.channels == (
        VoiceChannelUsageEntry(10, 80, 0),
        VoiceChannelUsageEntry(20, 20, 20),
    )


def test_pair_dto_rejects_same_user_negative_values_and_more_than_three_channels() -> (
    None
):
    with pytest.raises(ValueError, match="different"):
        VoicePairStatistics(AS_OF, 1, 1, 0, 0, 0, 0, ())
    with pytest.raises(ValueError, match="must not be negative"):
        VoicePairStatistics(AS_OF, 1, 2, -1, 0, 0, 0, ())
    with pytest.raises(ValueError, match="at most 3"):
        VoicePairStatistics(
            AS_OF,
            1,
            2,
            4,
            0,
            4,
            4,
            tuple(
                VoiceChannelUsageEntry(channel_id, 1, 0) for channel_id in range(1, 5)
            ),
        )
