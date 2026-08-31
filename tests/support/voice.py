"""Factories for verbose voice-statistics value objects."""

from datetime import datetime

from discord_stats_bot.features.voice_statistics import (
    VoicePeriodDurations,
    VoiceStatistics,
)


def make_voice_statistics(
    *,
    as_of: datetime,
    today_seconds: int = 0,
    last_7_days_seconds: int = 0,
    last_30_days_seconds: int = 0,
    all_time_seconds: int = 0,
    estimated_seconds: int = 0,
) -> VoiceStatistics:
    """Create statistics values without reproducing aggregation logic."""

    return VoiceStatistics(
        as_of=as_of,
        today=VoicePeriodDurations(today_seconds, estimated_seconds),
        last_7_days=VoicePeriodDurations(last_7_days_seconds, estimated_seconds),
        last_30_days=VoicePeriodDurations(last_30_days_seconds, estimated_seconds),
        all_time=VoicePeriodDurations(all_time_seconds, estimated_seconds),
    )
