"""Application service for read-only voice statistics."""

from datetime import UTC, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics.activity import (
    VoiceActivityInterval,
    VoiceActivityPeriod,
    VoiceActivityReport,
    aggregate_voice_activity,
)
from discord_stats_bot.features.voice_statistics.types import (
    VoiceChannelLeaderboard,
    VoiceChannelStatistics,
    VoiceCompanionEntry,
    VoiceLeaderboard,
    VoicePairStatistics,
    VoiceProfileWindow,
    VoiceServerStatistics,
    VoiceStatistics,
    VoiceStatisticsPeriod,
    VoiceStatisticsQuery,
    VoiceUserProfile,
    VoiceUserProfileCore,
    VoiceUserStandings,
    VoiceUserTopChannels,
    VoiceUserTopCompanions,
)


class VoiceStatisticsRepository(Protocol):
    """Read persistence contract for one user's aggregate voice statistics."""

    async def get_user_statistics(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoiceStatistics: ...

    async def get_leaderboard(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> VoiceLeaderboard: ...

    async def get_user_standings(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoiceUserStandings: ...

    async def get_user_top_channels(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoiceUserTopChannels: ...

    async def get_user_top_companions(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoiceUserTopCompanions: ...

    async def get_user_profile_core(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
        window: VoiceProfileWindow,
    ) -> VoiceUserProfileCore: ...

    async def get_user_profile_companions(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
        window: VoiceProfileWindow,
    ) -> tuple[VoiceCompanionEntry, ...]: ...

    async def get_channel_leaderboard(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> VoiceChannelLeaderboard: ...

    async def get_channel_statistics(
        self,
        guild_id: int,
        channel_id: int,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> VoiceChannelStatistics: ...

    async def get_pair_statistics(
        self,
        guild_id: int,
        user1_id: int,
        user2_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoicePairStatistics: ...

    async def get_server_statistics(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> VoiceServerStatistics: ...

    async def get_activity_intervals(
        self,
        guild_id: int,
        started_at: datetime,
        query: VoiceStatisticsQuery,
    ) -> tuple[VoiceActivityInterval, ...]: ...


class VoiceStatisticsService:
    """Calculate reporting windows and delegate aggregation to persistence."""

    def __init__(
        self,
        repository: VoiceStatisticsRepository,
        *,
        report_timezone: ZoneInfo,
        min_session_seconds: int,
    ) -> None:
        if min_session_seconds <= 0:
            raise ValueError("min_session_seconds must be positive")
        self._repository = repository
        self._report_timezone = report_timezone
        self._min_session_seconds = min_session_seconds

    async def get_user_statistics(
        self,
        guild_id: int,
        user_id: int,
        as_of: datetime,
    ) -> VoiceStatistics:
        """Return one consistent snapshot for today, 7d, 30d and all time."""

        query = self._build_query(as_of)
        return await self._repository.get_user_statistics(guild_id, user_id, query)

    async def get_user_profile(
        self,
        guild_id: int,
        user_id: int,
        period: VoiceStatisticsPeriod,
        as_of: datetime,
    ) -> VoiceUserProfile:
        """Return a complete profile from two reads sharing one window snapshot."""

        if not isinstance(period, VoiceStatisticsPeriod):
            raise ValueError("period must be a VoiceStatisticsPeriod")
        query = self._build_query(as_of)
        window = build_voice_profile_window(
            period,
            query,
            report_timezone=self._report_timezone,
        )
        core = await self._repository.get_user_profile_core(
            guild_id, user_id, query, window
        )
        companions = await self._repository.get_user_profile_companions(
            guild_id, user_id, query, window
        )
        return VoiceUserProfile(core=core, companions=companions)

    async def get_leaderboard(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        as_of: datetime,
    ) -> VoiceLeaderboard:
        """Return the persistence-ranked guild TOP 10 for one period."""

        if not isinstance(period, VoiceStatisticsPeriod):
            raise ValueError("period must be a VoiceStatisticsPeriod")
        query = self._build_query(as_of)
        return await self._repository.get_leaderboard(guild_id, period, query)

    async def get_user_statistics_with_standings(
        self,
        guild_id: int,
        user_id: int,
        as_of: datetime,
    ) -> tuple[VoiceStatistics, VoiceUserStandings]:
        """Return durations and full-guild positions from one window snapshot."""

        query = self._build_query(as_of)
        statistics = await self._repository.get_user_statistics(
            guild_id,
            user_id,
            query,
        )
        standings = await self._repository.get_user_standings(
            guild_id,
            user_id,
            query,
        )
        return statistics, standings

    async def get_user_report(
        self,
        guild_id: int,
        user_id: int,
        as_of: datetime,
    ) -> tuple[
        VoiceStatistics,
        VoiceUserStandings,
        VoiceUserTopChannels,
        VoiceUserTopCompanions,
    ]:
        """Return all personal statistics from one shared query definition."""

        query = self._build_query(as_of)
        statistics = await self._repository.get_user_statistics(
            guild_id, user_id, query
        )
        standings = await self._repository.get_user_standings(guild_id, user_id, query)
        top_channels = await self._repository.get_user_top_channels(
            guild_id, user_id, query
        )
        top_companions = await self._repository.get_user_top_companions(
            guild_id, user_id, query
        )
        return statistics, standings, top_channels, top_companions

    async def get_user_standings(
        self,
        guild_id: int,
        user_id: int,
        as_of: datetime,
    ) -> VoiceUserStandings:
        """Return full-guild positions for all four reporting periods."""

        query = self._build_query(as_of)
        return await self._repository.get_user_standings(guild_id, user_id, query)

    async def get_channel_leaderboard(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        as_of: datetime,
    ) -> VoiceChannelLeaderboard:
        """Return the persistence-ranked guild channel TOP 10."""

        if not isinstance(period, VoiceStatisticsPeriod):
            raise ValueError("period must be a VoiceStatisticsPeriod")
        query = self._build_query(as_of)
        return await self._repository.get_channel_leaderboard(guild_id, period, query)

    async def get_channel_statistics(
        self,
        guild_id: int,
        channel_id: int,
        period: VoiceStatisticsPeriod,
        as_of: datetime,
    ) -> VoiceChannelStatistics:
        """Return one channel's total and persistence-ranked TOP 10 users."""

        if channel_id <= 0:
            raise ValueError("channel_id must be positive")
        if not isinstance(period, VoiceStatisticsPeriod):
            raise ValueError("period must be a VoiceStatisticsPeriod")
        query = self._build_query(as_of)
        return await self._repository.get_channel_statistics(
            guild_id, channel_id, period, query
        )

    async def get_user_top_channels(
        self,
        guild_id: int,
        user_id: int,
        as_of: datetime,
    ) -> VoiceUserTopChannels:
        """Return one user's all-time TOP 3 channels."""

        query = self._build_query(as_of)
        return await self._repository.get_user_top_channels(guild_id, user_id, query)

    async def get_user_top_companions(
        self,
        guild_id: int,
        user_id: int,
        as_of: datetime,
    ) -> VoiceUserTopCompanions:
        """Return one user's all-time TOP 3 voice companions."""

        query = self._build_query(as_of)
        return await self._repository.get_user_top_companions(guild_id, user_id, query)

    async def get_pair_report(
        self,
        guild_id: int,
        user1_id: int,
        user2_id: int,
        as_of: datetime,
    ) -> VoicePairStatistics:
        """Return one all-time pair report from a shared query definition."""

        if user1_id <= 0 or user2_id <= 0:
            raise ValueError("user IDs must be positive")
        if user1_id == user2_id:
            raise ValueError("pair users must be different")
        query = self._build_query(as_of)
        return await self._repository.get_pair_statistics(
            guild_id,
            user1_id,
            user2_id,
            query,
        )

    async def get_server_report(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        as_of: datetime,
    ) -> VoiceServerStatistics:
        """Return one consistent compact server overview."""

        if not isinstance(period, VoiceStatisticsPeriod):
            raise ValueError("period must be a VoiceStatisticsPeriod")
        query = self._build_query(as_of)
        return await self._repository.get_server_statistics(guild_id, period, query)

    async def get_activity_report(
        self,
        guild_id: int,
        period: VoiceActivityPeriod,
        as_of: datetime,
    ) -> VoiceActivityReport:
        """Return timezone-aware recurring guild activity for one finite period."""

        if not isinstance(period, VoiceActivityPeriod):
            raise ValueError("period must be a VoiceActivityPeriod")
        query = self._build_query(as_of)
        started_at = query.as_of - timedelta(days=period.days)
        intervals = await self._repository.get_activity_intervals(
            guild_id, started_at, query
        )
        return aggregate_voice_activity(
            intervals,
            period=period,
            as_of=query.as_of,
            report_timezone=self._report_timezone,
        )

    def _build_query(self, as_of: datetime) -> VoiceStatisticsQuery:
        return build_voice_statistics_query(
            as_of,
            report_timezone=self._report_timezone,
            min_session_seconds=self._min_session_seconds,
        )


def build_voice_statistics_query(
    as_of: datetime,
    *,
    report_timezone: ZoneInfo,
    min_session_seconds: int,
) -> VoiceStatisticsQuery:
    """Build the shared UTC/report-timezone window definition."""

    if min_session_seconds <= 0:
        raise ValueError("min_session_seconds must be positive")
    as_of = normalize_observed_at(as_of)
    local_as_of = as_of.astimezone(report_timezone)
    local_midnight = local_as_of.replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return VoiceStatisticsQuery(
        as_of=as_of,
        today_started_at=local_midnight,
        last_7_days_started_at=as_of - timedelta(days=7),
        last_30_days_started_at=as_of - timedelta(days=30),
        min_exact_session_seconds=min_session_seconds,
    )


def build_voice_profile_window(
    period: VoiceStatisticsPeriod,
    query: VoiceStatisticsQuery,
    *,
    report_timezone: ZoneInfo,
) -> VoiceProfileWindow:
    """Build current and equal local/rolling comparison windows."""

    if period is VoiceStatisticsPeriod.ALL_TIME:
        return VoiceProfileWindow(period, None, None, None)
    started_at = query.started_at_for(period)
    if started_at is None:  # pragma: no cover - guarded by the enum branch above
        raise AssertionError("finite period requires a lower boundary")
    if period is VoiceStatisticsPeriod.TODAY:
        local_as_of = query.as_of.astimezone(report_timezone)
        previous_date = local_as_of.date() - timedelta(days=1)
        previous_start = datetime.combine(
            previous_date,
            time.min,
            tzinfo=report_timezone,
        ).astimezone(UTC)
        previous_end = datetime.combine(
            previous_date,
            time(
                local_as_of.hour,
                local_as_of.minute,
                local_as_of.second,
                local_as_of.microsecond,
            ),
            tzinfo=report_timezone,
        ).astimezone(UTC)
    else:
        window_length = query.as_of - started_at
        previous_end = started_at
        previous_start = previous_end - window_length
    return VoiceProfileWindow(period, started_at, previous_start, previous_end)
