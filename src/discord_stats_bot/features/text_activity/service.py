"""Application service for daily text activity."""

from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from discord_stats_bot.features.text_activity.types import (
    TextActivityDateRange,
    TextActivityLeaderboard,
    TextActivityPeriod,
    TextMessageActivity,
    TextUserMessageCount,
    normalize_occurred_at,
)


class TextActivityRepository(Protocol):
    """Caller-owned persistence operation required by text activity."""

    async def record_message(
        self,
        *,
        guild_id: int,
        user_id: int,
        channel_id: int,
        activity_date: date,
        attachment_count: int,
        is_reply: bool,
    ) -> None: ...

    async def get_user_message_counts(
        self,
        guild_id: int,
        started_on: date | None,
        ended_on: date,
        *,
        user_ids: tuple[int, ...] | None = None,
        limit: int | None = None,
    ) -> tuple[TextUserMessageCount, ...]: ...


class TextActivityService:
    """Map UTC message observations to reporting dates and persist counters."""

    def __init__(
        self,
        repository: TextActivityRepository,
        *,
        report_timezone: ZoneInfo,
    ) -> None:
        self._repository = repository
        self._report_timezone = report_timezone

    async def record_message(self, activity: TextMessageActivity) -> None:
        """Count one message on its ``REPORT_TIMEZONE`` calendar date."""

        activity_date = activity.occurred_at.astimezone(self._report_timezone).date()
        await self._repository.record_message(
            guild_id=activity.guild_id,
            user_id=activity.user_id,
            channel_id=activity.channel_id,
            activity_date=activity_date,
            attachment_count=activity.attachment_count,
            is_reply=activity.is_reply,
        )

    async def get_leaderboard(
        self,
        guild_id: int,
        period: TextActivityPeriod,
        as_of: datetime,
        *,
        limit: int = 10,
    ) -> TextActivityLeaderboard:
        """Return deterministic message totals for calendar reporting dates."""

        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if limit <= 0:
            raise ValueError("limit must be positive")
        as_of = normalize_occurred_at(as_of)
        date_range = text_activity_date_range(
            period,
            as_of,
            report_timezone=self._report_timezone,
        )
        entries = await self._repository.get_user_message_counts(
            guild_id,
            date_range.started_on,
            date_range.ended_on,
            limit=limit,
        )
        return TextActivityLeaderboard(as_of, period, entries)


def text_activity_date_range(
    period: TextActivityPeriod,
    as_of: datetime,
    *,
    report_timezone: ZoneInfo,
) -> TextActivityDateRange:
    """Build inclusive local-calendar boundaries for a text period."""

    local_date = normalize_occurred_at(as_of).astimezone(report_timezone).date()
    started_on = {
        TextActivityPeriod.TODAY: local_date,
        TextActivityPeriod.LAST_7_DAYS: local_date - timedelta(days=6),
        TextActivityPeriod.LAST_30_DAYS: local_date - timedelta(days=29),
        TextActivityPeriod.ALL_TIME: None,
    }[period]
    return TextActivityDateRange(started_on, local_date)
