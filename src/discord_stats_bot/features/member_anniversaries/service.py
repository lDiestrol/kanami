"""Calendar calculation for upcoming guild member anniversaries."""

import calendar
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time
from typing import Protocol
from zoneinfo import ZoneInfo

from discord_stats_bot.features.member_anniversaries.types import (
    MemberAnniversary,
    MemberJoinSnapshot,
)


def _anniversary_date(joined_on: date, year: int) -> date:
    """Return the anniversary date, treating 28 February as the fallback."""

    if joined_on.month == 2 and joined_on.day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, joined_on.month, joined_on.day)


class MemberAnniversaryService:
    """Find upcoming anniversaries using the configured report timezone."""

    def __init__(self, report_timezone: ZoneInfo) -> None:
        self._report_timezone = report_timezone

    def upcoming(
        self,
        members: Iterable[MemberJoinSnapshot],
        *,
        as_of: datetime,
        days: int = 30,
    ) -> tuple[MemberAnniversary, ...]:
        """Return anniversaries from today through ``days`` days in the future."""

        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        if days < 0:
            raise ValueError("days must not be negative")

        today = as_of.astimezone(self._report_timezone).date()
        anniversaries: list[MemberAnniversary] = []
        for member in members:
            if member.is_bot or member.joined_at is None:
                continue
            if member.joined_at.tzinfo is None or member.joined_at.utcoffset() is None:
                continue

            joined_on = member.joined_at.astimezone(self._report_timezone).date()
            anniversary = _anniversary_date(joined_on, today.year)
            if anniversary < today:
                anniversary = _anniversary_date(joined_on, today.year + 1)

            years = anniversary.year - joined_on.year
            days_until = (anniversary - today).days
            if years < 1 or days_until > days:
                continue
            anniversaries.append(
                MemberAnniversary(
                    user_id=member.user_id,
                    display_name=member.display_name,
                    anniversary_date=anniversary,
                    years=years,
                    days_until=days_until,
                )
            )

        return tuple(
            sorted(
                anniversaries,
                key=lambda item: (
                    item.days_until,
                    item.display_name.casefold(),
                    item.user_id,
                ),
            )
        )


class MemberAnniversaryNotificationRepository(Protocol):
    """Caller-owned persistence contract for durable anniversary delivery."""

    async def enqueue_anniversaries(
        self,
        *,
        guild_id: int,
        anniversaries: Sequence[MemberAnniversary],
        occurred_at: datetime,
    ) -> int: ...


class MemberAnniversaryNotificationService:
    """Find today's anniversaries and enqueue each one idempotently."""

    def __init__(
        self,
        repository: MemberAnniversaryNotificationRepository,
        *,
        report_timezone: ZoneInfo,
    ) -> None:
        self._repository = repository
        self._report_timezone = report_timezone
        self._anniversaries = MemberAnniversaryService(report_timezone)

    async def enqueue_today(
        self,
        *,
        guild_id: int,
        members: Iterable[MemberJoinSnapshot],
        as_of: datetime,
    ) -> int:
        """Persist one durable notification per member anniversary today."""

        anniversaries = self._anniversaries.upcoming(
            members,
            as_of=as_of,
            days=0,
        )
        if not anniversaries:
            return 0
        local_date = as_of.astimezone(self._report_timezone).date()
        occurred_at = datetime.combine(
            local_date,
            time.min,
            tzinfo=self._report_timezone,
        ).astimezone(UTC)
        return await self._repository.enqueue_anniversaries(
            guild_id=guild_id,
            anniversaries=anniversaries,
            occurred_at=occurred_at,
        )
