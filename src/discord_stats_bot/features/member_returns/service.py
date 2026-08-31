"""Application orchestration for durable member return notifications."""

from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from discord_stats_bot.features.achievements import UnlockedAchievement
from discord_stats_bot.features.member_returns.types import (
    MemberReturnEvent,
    MemberReturnSnapshot,
)
from discord_stats_bot.features.text_activity import TextUserMessageCount
from discord_stats_bot.features.voice_statistics import VoiceStatistics


class MemberReturnHistoryRepository(Protocol):
    async def latest_member_left_at(
        self, *, guild_id: int, user_id: int, before_or_at: datetime
    ) -> datetime | None: ...

    async def count_member_leaves(
        self, *, guild_id: int, user_id: int, before_or_at: datetime
    ) -> int: ...

    async def enqueue_member_return(self, event: MemberReturnEvent) -> bool: ...


class MemberReturnVoiceStatistics(Protocol):
    async def get_user_statistics(
        self, guild_id: int, user_id: int, as_of: datetime
    ) -> VoiceStatistics: ...


class MemberReturnTextStatistics(Protocol):
    async def get_user_message_counts(
        self,
        guild_id: int,
        started_on: date | None,
        ended_on: date,
        *,
        user_ids: tuple[int, ...] | None = None,
        limit: int | None = None,
    ) -> tuple[TextUserMessageCount, ...]: ...


class MemberReturnAchievements(Protocol):
    async def list_unlocked(
        self, *, guild_id: int, user_id: int
    ) -> tuple[UnlockedAchievement, ...]: ...


class MemberReturnService:
    """Detect a rejoin, snapshot lifetime metrics, and enqueue it once."""

    def __init__(
        self,
        history_repository: MemberReturnHistoryRepository,
        voice_statistics: MemberReturnVoiceStatistics,
        text_statistics: MemberReturnTextStatistics,
        achievements: MemberReturnAchievements,
        *,
        report_timezone: ZoneInfo,
        min_absence_seconds: int,
    ) -> None:
        if min_absence_seconds <= 0:
            raise ValueError("min_absence_seconds must be positive")
        self._history_repository = history_repository
        self._voice_statistics = voice_statistics
        self._text_statistics = text_statistics
        self._achievements = achievements
        self._report_timezone = report_timezone
        self._min_absence_seconds = min_absence_seconds

    async def enqueue_if_returned(self, member: MemberReturnSnapshot) -> bool:
        """Return whether one new durable event was inserted."""

        if member.is_bot:
            return False
        left_at = await self._history_repository.latest_member_left_at(
            guild_id=member.guild_id,
            user_id=member.user_id,
            before_or_at=member.joined_at,
        )
        if left_at is None:
            return False
        absence_seconds = int((member.joined_at - left_at).total_seconds())
        if absence_seconds < self._min_absence_seconds:
            return False

        voice = await self._voice_statistics.get_user_statistics(
            member.guild_id,
            member.user_id,
            member.joined_at,
        )
        local_date = member.joined_at.astimezone(self._report_timezone).date()
        messages = await self._text_statistics.get_user_message_counts(
            member.guild_id,
            None,
            local_date,
            user_ids=(member.user_id,),
        )
        unlocked = await self._achievements.list_unlocked(
            guild_id=member.guild_id,
            user_id=member.user_id,
        )
        return_number = await self._history_repository.count_member_leaves(
            guild_id=member.guild_id,
            user_id=member.user_id,
            before_or_at=left_at,
        )
        event = MemberReturnEvent(
            guild_id=member.guild_id,
            user_id=member.user_id,
            previous_left_at=left_at,
            returned_at=member.joined_at,
            absence_seconds=absence_seconds,
            voice_seconds=voice.all_time.total_seconds,
            message_count=messages[0].message_count if messages else 0,
            achievement_count=len(unlocked),
            return_number=return_number,
        )
        return await self._history_repository.enqueue_member_return(event)
