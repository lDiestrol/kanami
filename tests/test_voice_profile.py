from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.voice_statistics import (
    VoiceCompanionEntry,
    VoiceFavoriteChannel,
    VoicePeriodDurations,
    VoiceStatisticsPeriod,
    VoiceStatisticsService,
)
from discord_stats_bot.features.voice_statistics.service import (
    build_voice_profile_window,
    build_voice_statistics_query,
)
from discord_stats_bot.persistence.repositories.voice_statistics import (
    SqlAlchemyVoiceStatisticsRepository,
    voice_user_profile_companions_statement,
    voice_user_profile_core_statement,
)

AS_OF = datetime(2026, 8, 14, 12, 34, 56, tzinfo=UTC)


def query(*, timezone: str = "UTC") -> object:
    return build_voice_statistics_query(
        AS_OF,
        report_timezone=ZoneInfo(timezone),
        min_session_seconds=10,
    )


@pytest.mark.parametrize(
    ("period", "current_delta", "previous_delta"),
    [
        (VoiceStatisticsPeriod.LAST_7_DAYS, timedelta(days=7), timedelta(days=14)),
        (VoiceStatisticsPeriod.LAST_30_DAYS, timedelta(days=30), timedelta(days=60)),
    ],
)
def test_rolling_profile_windows_are_equal_and_adjacent(
    period: VoiceStatisticsPeriod,
    current_delta: timedelta,
    previous_delta: timedelta,
) -> None:
    stats_query = query()

    window = build_voice_profile_window(
        period, stats_query, report_timezone=ZoneInfo("UTC")
    )

    assert window.started_at == AS_OF - current_delta
    assert window.previous_ended_at == window.started_at
    assert window.previous_started_at == AS_OF - previous_delta


def test_today_compares_previous_local_day_to_same_local_time() -> None:
    stats_query = query(timezone="Asia/Yekaterinburg")

    window = build_voice_profile_window(
        VoiceStatisticsPeriod.TODAY,
        stats_query,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
    )

    assert window.started_at == datetime(2026, 8, 13, 19, 0, tzinfo=UTC)
    assert window.previous_started_at == datetime(2026, 8, 12, 19, 0, tzinfo=UTC)
    assert window.previous_ended_at == datetime(2026, 8, 13, 12, 34, 56, tzinfo=UTC)


def test_all_time_profile_has_no_comparison_window() -> None:
    stats_query = query()

    window = build_voice_profile_window(
        VoiceStatisticsPeriod.ALL_TIME,
        stats_query,
        report_timezone=ZoneInfo("UTC"),
    )

    assert window.started_at is None
    assert window.previous_started_at is None
    assert window.previous_ended_at is None


def test_profile_core_sql_reuses_eligibility_and_counts_logical_sessions() -> None:
    stats_query = query()
    window = build_voice_profile_window(
        VoiceStatisticsPeriod.LAST_7_DAYS,
        stats_query,
        report_timezone=ZoneInfo("UTC"),
    )

    sql = str(
        voice_user_profile_core_statement(10, 20, stats_query, window).compile(
            dialect=postgresql.dialect()
        )
    ).lower()

    assert "count(distinct(case when" in sql
    assert "profile_target_eligible_voice_sessions" in sql
    assert "having sum(case when" in sql
    assert "voice_intervals.is_afk is false" in sql
    assert "least(voice_sessions.confirmed_through_at" in sql
    assert "row_number() over" in sql
    assert (
        "selected_period_exact_seconds + "
        "active_voice_profile_totals.selected_period_estimated_seconds desc"
    ) in sql
    assert "selected_period_exact_seconds desc" in sql
    assert "active_voice_profile_totals.user_id asc" in sql
    assert "profile_favorite_voice_channel_totals.exact_seconds desc" in sql
    assert "profile_favorite_voice_channel_totals.channel_id asc" in sql
    assert "voice_channels.name" in sql
    assert "limit" in sql


def test_profile_companions_are_clipped_to_period_and_exclude_self() -> None:
    stats_query = query()
    window = build_voice_profile_window(
        VoiceStatisticsPeriod.LAST_7_DAYS,
        stats_query,
        report_timezone=ZoneInfo("UTC"),
    )

    sql = str(
        voice_user_profile_companions_statement(10, 20, stats_query, window).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).lower()

    assert "2026-08-07 12:34:56+00:00" in sql
    assert "profile_companion_eligible_voice_sessions" in sql
    assert "discord_users.is_bot is false" in sql
    assert "profile_companion_effective_voice_intervals.user_id != 20" in sql
    assert "greatest(" in sql
    assert "limit 3" in sql


@pytest.mark.asyncio
async def test_profile_repository_maps_core_and_favorite_channel() -> None:
    row = SimpleNamespace(
        exact_seconds=7200,
        estimated_seconds=60,
        session_count=2,
        previous_exact_seconds=3600,
        previous_estimated_seconds=0,
        rank=3,
        participant_count=12,
        favorite_channel_id=101,
        favorite_channel_name="Общий",
        favorite_exact_seconds=4000,
        favorite_estimated_seconds=60,
    )

    class Result:
        def one(self) -> object:
            return row

    class Session:
        async def execute(self, statement: object) -> Result:
            return Result()

    stats_query = query()
    window = build_voice_profile_window(
        VoiceStatisticsPeriod.LAST_7_DAYS,
        stats_query,
        report_timezone=ZoneInfo("UTC"),
    )
    repository = SqlAlchemyVoiceStatisticsRepository(Session())  # type: ignore[arg-type]

    core = await repository.get_user_profile_core(10, 20, stats_query, window)

    assert core.durations == VoicePeriodDurations(7200, 60)
    assert core.previous_durations == VoicePeriodDurations(3600, 0)
    assert core.standing.rank == 3
    assert core.session_count == 2
    assert core.favorite_channel == VoiceFavoriteChannel(101, "Общий", 4000, 60)


@pytest.mark.asyncio
async def test_profile_service_uses_two_queries_with_same_window() -> None:
    class Repository:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object, object]] = []

        async def get_user_profile_core(
            self, guild_id: int, user_id: int, stats_query: object, window: object
        ) -> object:
            self.calls.append(("core", stats_query, window))
            return SimpleNamespace()

        async def get_user_profile_companions(
            self, guild_id: int, user_id: int, stats_query: object, window: object
        ) -> tuple[VoiceCompanionEntry, ...]:
            self.calls.append(("companions", stats_query, window))
            return ()

    repository = Repository()
    service = VoiceStatisticsService(
        repository,  # type: ignore[arg-type]
        report_timezone=ZoneInfo("UTC"),
        min_session_seconds=10,
    )

    result = await service.get_user_profile(
        10, 20, VoiceStatisticsPeriod.LAST_7_DAYS, AS_OF
    )

    assert result.companions == ()
    assert [call[0] for call in repository.calls] == ["core", "companions"]
    assert repository.calls[0][1] is repository.calls[1][1]
    assert repository.calls[0][2] is repository.calls[1][2]
