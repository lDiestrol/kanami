"""Server-wide analytics read foundation."""

from discord_stats_bot.features.server_analytics.periods import build_analytics_window
from discord_stats_bot.features.server_analytics.service import (
    ServerAnalyticsRepository,
    ServerAnalyticsService,
    analytics_metric,
)
from discord_stats_bot.features.server_analytics.types import (
    AnalyticsCoverage,
    AnalyticsDailyPoint,
    AnalyticsEarliestRecorded,
    AnalyticsMessageTopMember,
    AnalyticsMetric,
    AnalyticsPercentState,
    AnalyticsQuery,
    AnalyticsTextRow,
    AnalyticsVoiceInterval,
    AnalyticsVoiceMetric,
    AnalyticsVoiceTopMember,
    AnalyticsWindow,
    ServerAnalyticsPeriod,
    ServerAnalyticsReport,
)

__all__ = [
    "AnalyticsCoverage",
    "AnalyticsDailyPoint",
    "AnalyticsEarliestRecorded",
    "AnalyticsMessageTopMember",
    "AnalyticsMetric",
    "AnalyticsPercentState",
    "AnalyticsQuery",
    "AnalyticsTextRow",
    "AnalyticsVoiceInterval",
    "AnalyticsVoiceMetric",
    "AnalyticsVoiceTopMember",
    "AnalyticsWindow",
    "ServerAnalyticsPeriod",
    "ServerAnalyticsReport",
    "ServerAnalyticsRepository",
    "ServerAnalyticsService",
    "analytics_metric",
    "build_analytics_window",
]
