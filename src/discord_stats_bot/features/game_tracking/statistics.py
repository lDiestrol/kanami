"""Discord-independent read model and aggregation for tracked games."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from discord_stats_bot.features.game_tracking.canonicalization import (
    CanonicalGameNameSelection,
    canonicalize_game_name,
    select_canonical_display_name,
)
from discord_stats_bot.features.voice.types import normalize_observed_at


class GameStatisticsPeriod(StrEnum):
    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"
    LAST_90_DAYS = "90d"
    ALL_TIME = "all"

    @property
    def days(self) -> int | None:
        return {
            self.LAST_7_DAYS: 7,
            self.LAST_30_DAYS: 30,
            self.LAST_90_DAYS: 90,
            self.ALL_TIME: None,
        }[self]


@dataclass(frozen=True, slots=True)
class GameSessionSlice:
    """Confirmed bounds needed by the statistics service."""

    session_id: int
    game_key: str
    game_name: str
    started_at: datetime
    confirmed_through_at: datetime
    ended_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", normalize_observed_at(self.started_at))
        object.__setattr__(
            self,
            "confirmed_through_at",
            normalize_observed_at(self.confirmed_through_at),
        )
        if self.ended_at is not None:
            object.__setattr__(self, "ended_at", normalize_observed_at(self.ended_at))

    @property
    def effective_end(self) -> datetime:
        return self.ended_at or self.confirmed_through_at


@dataclass(frozen=True, slots=True)
class GameUsageEntry:
    canonical_key: str
    game_name: str
    total_seconds: int


@dataclass(frozen=True, slots=True)
class LatestGame:
    game_name: str
    tracked_at: datetime


@dataclass(frozen=True, slots=True)
class LongestGameSession:
    game_name: str
    total_seconds: int


@dataclass(frozen=True, slots=True)
class GameStatistics:
    as_of: datetime
    period: GameStatisticsPeriod
    total_seconds: int
    unique_games: int
    gaming_days: int
    top_games: tuple[GameUsageEntry, ...]
    latest_game: LatestGame | None
    longest_session: LongestGameSession | None

    @property
    def has_data(self) -> bool:
        return self.unique_games > 0


class GameStatisticsRepository(Protocol):
    async def list_user_sessions(
        self,
        guild_id: int,
        user_id: int,
        *,
        started_after: datetime | None,
        ended_before: datetime,
    ) -> tuple[GameSessionSlice, ...]: ...


@dataclass(slots=True)
class _GameAccumulator:
    name_selection: CanonicalGameNameSelection
    duration: timedelta


class GameStatisticsService:
    """Aggregate confirmed game-session overlap for one user and period."""

    def __init__(
        self,
        repository: GameStatisticsRepository,
        *,
        report_timezone: ZoneInfo,
    ) -> None:
        self._repository = repository
        self._report_timezone = report_timezone

    async def get_user_statistics(
        self,
        guild_id: int,
        user_id: int,
        period: GameStatisticsPeriod,
        as_of: datetime,
    ) -> GameStatistics:
        as_of = normalize_observed_at(as_of)
        period_start = (
            None if period.days is None else as_of - timedelta(days=period.days)
        )
        sessions = await self._repository.list_user_sessions(
            guild_id,
            user_id,
            started_after=period_start,
            ended_before=as_of,
        )
        return self._aggregate(sessions, period, period_start, as_of)

    def _aggregate(
        self,
        sessions: Sequence[GameSessionSlice],
        period: GameStatisticsPeriod,
        period_start: datetime | None,
        as_of: datetime,
    ) -> GameStatistics:
        total = timedelta()
        games: dict[str, _GameAccumulator] = {}
        gaming_dates: set[date] = set()
        latest: tuple[datetime, datetime, int, str] | None = None
        longest: tuple[timedelta, str, int] | None = None

        for item in sessions:
            start = (
                max(item.started_at, period_start) if period_start else item.started_at
            )
            end = min(item.effective_end, as_of)
            if end <= start:
                continue
            duration = end - start
            total += duration

            canonical = canonicalize_game_name(item.game_name)
            accumulator = games.get(canonical.key)
            if accumulator is None:
                games[canonical.key] = _GameAccumulator(
                    select_canonical_display_name(
                        None,
                        canonical,
                        observed_at=end,
                        session_id=item.session_id,
                    ),
                    duration,
                )
            else:
                accumulator.duration += duration
                accumulator.name_selection = select_canonical_display_name(
                    accumulator.name_selection,
                    canonical,
                    observed_at=end,
                    session_id=item.session_id,
                )

            first_date = start.astimezone(self._report_timezone).date()
            last_date = (
                (end - timedelta(microseconds=1))
                .astimezone(self._report_timezone)
                .date()
            )
            cursor = first_date
            while cursor <= last_date:
                gaming_dates.add(cursor)
                cursor += timedelta(days=1)

            latest_candidate = (
                end,
                item.started_at,
                item.session_id,
                canonical.key,
            )
            if latest is None or latest_candidate[:3] > latest[:3]:
                latest = latest_candidate

            if longest is None or (
                duration,
                canonical.key,
                item.session_id,
            ) > (
                longest[0],
                longest[1],
                longest[2],
            ):
                longest = (duration, canonical.key, item.session_id)

        top_games = tuple(
            GameUsageEntry(
                key,
                value.name_selection.display_name,
                int(value.duration.total_seconds()),
            )
            for key, value in sorted(
                games.items(),
                key=lambda entry: (
                    -entry[1].duration.total_seconds(),
                    entry[1].name_selection.display_name.casefold(),
                    entry[0],
                ),
            )[:5]
        )
        return GameStatistics(
            as_of=as_of,
            period=period,
            total_seconds=int(total.total_seconds()),
            unique_games=len(games),
            gaming_days=len(gaming_dates),
            top_games=top_games,
            latest_game=(
                LatestGame(games[latest[3]].name_selection.display_name, latest[0])
                if latest
                else None
            ),
            longest_session=(
                LongestGameSession(
                    games[longest[1]].name_selection.display_name,
                    int(longest[0].total_seconds()),
                )
                if longest
                else None
            ),
        )
