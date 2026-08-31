"""Server-wide historical game analytics over completed local days."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo

from discord_stats_bot.features.game_tracking.canonicalization import (
    CanonicalGameNameSelection,
    canonicalize_game_name,
    select_canonical_display_name,
)
from discord_stats_bot.features.voice.types import normalize_observed_at


class ServerGameStatisticsPeriod(StrEnum):
    LAST_7_DAYS = "7d"
    LAST_30_DAYS = "30d"
    LAST_90_DAYS = "90d"

    @property
    def days(self) -> int:
        return {
            self.LAST_7_DAYS: 7,
            self.LAST_30_DAYS: 30,
            self.LAST_90_DAYS: 90,
        }[self]


@dataclass(frozen=True, slots=True)
class ServerGameStatisticsWindow:
    as_of: datetime
    timezone_name: str
    started_at: datetime
    ended_at: datetime
    started_on: date
    ended_before: date

    def __post_init__(self) -> None:
        for field_name in ("as_of", "started_at", "ended_at"):
            object.__setattr__(
                self, field_name, normalize_observed_at(getattr(self, field_name))
            )
        if not self.timezone_name:
            raise ValueError("timezone_name must not be empty")
        if not self.started_at < self.ended_at <= self.as_of:
            raise ValueError("server game UTC window must be ordered")
        if not self.started_on < self.ended_before:
            raise ValueError("server game local-date window must be ordered")


@dataclass(frozen=True, slots=True)
class ServerGameSessionSlice:
    session_id: int
    user_id: int
    display_name: str
    game_name: str
    started_at: datetime
    confirmed_through_at: datetime
    ended_at: datetime | None

    def __post_init__(self) -> None:
        if self.session_id <= 0 or self.user_id <= 0:
            raise ValueError("session_id and user_id must be positive")
        if not self.display_name:
            raise ValueError("display_name must not be empty")
        for field_name in ("started_at", "confirmed_through_at"):
            object.__setattr__(
                self, field_name, normalize_observed_at(getattr(self, field_name))
            )
        if self.ended_at is not None:
            object.__setattr__(self, "ended_at", normalize_observed_at(self.ended_at))

    @property
    def effective_end(self) -> datetime:
        return self.ended_at or self.confirmed_through_at


@dataclass(frozen=True, slots=True)
class ServerGameDailyPoint:
    local_date: date
    total_seconds: int
    unique_gamers: int


@dataclass(frozen=True, slots=True)
class ServerGameTopGame:
    canonical_key: str
    game_name: str
    total_seconds: int
    unique_gamers: int


@dataclass(frozen=True, slots=True)
class ServerGameTopPlayer:
    user_id: int
    display_name: str
    total_seconds: int
    unique_games: int
    gaming_days: int


@dataclass(frozen=True, slots=True)
class ServerGameStatistics:
    period: ServerGameStatisticsPeriod
    window: ServerGameStatisticsWindow
    total_seconds: int
    active_gamers: int
    unique_games: int
    average_seconds_per_gamer: int
    daily: tuple[ServerGameDailyPoint, ...]
    top_games: tuple[ServerGameTopGame, ...]
    top_players: tuple[ServerGameTopPlayer, ...]
    earliest_recorded_on: date | None
    period_may_be_partial: bool | None

    @property
    def has_data(self) -> bool:
        return self.active_gamers > 0


class ServerGameStatisticsRepository(Protocol):
    async def list_server_sessions(
        self,
        guild_id: int,
        *,
        started_after: datetime,
        ended_before: datetime,
    ) -> tuple[ServerGameSessionSlice, ...]: ...

    async def get_earliest_confirmed_activity(
        self,
        guild_id: int,
    ) -> datetime | None: ...


@dataclass(slots=True)
class _GameAccumulator:
    name_selection: CanonicalGameNameSelection
    seconds: float
    users: set[int]


@dataclass(slots=True)
class _PlayerAccumulator:
    display_name: str
    seconds: float
    games: set[str]
    gaming_days: set[date]


def build_server_game_statistics_window(
    period: ServerGameStatisticsPeriod,
    as_of: datetime,
    *,
    report_timezone: ZoneInfo,
) -> ServerGameStatisticsWindow:
    """Build a DST-safe window containing only completed local calendar days."""

    if not isinstance(period, ServerGameStatisticsPeriod):
        raise ValueError("period must be a ServerGameStatisticsPeriod")
    as_of = normalize_observed_at(as_of)
    ended_before = as_of.astimezone(report_timezone).date()
    started_on = ended_before - timedelta(days=period.days)

    def midnight(local_date: date) -> datetime:
        return datetime.combine(
            local_date, time.min, tzinfo=report_timezone
        ).astimezone(UTC)

    return ServerGameStatisticsWindow(
        as_of,
        report_timezone.key,
        midnight(started_on),
        midnight(ended_before),
        started_on,
        ended_before,
    )


class ServerGameStatisticsService:
    """Aggregate one guild's confirmed game person-time without N+1 reads."""

    def __init__(
        self,
        repository: ServerGameStatisticsRepository,
        *,
        report_timezone: ZoneInfo,
    ) -> None:
        self._repository = repository
        self._report_timezone = report_timezone

    async def get_report(
        self,
        guild_id: int,
        period: ServerGameStatisticsPeriod,
        as_of: datetime,
    ) -> ServerGameStatistics:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        window = build_server_game_statistics_window(
            period,
            as_of,
            report_timezone=self._report_timezone,
        )
        sessions = await self._repository.list_server_sessions(
            guild_id,
            started_after=window.started_at,
            ended_before=window.ended_at,
        )
        earliest = await self._repository.get_earliest_confirmed_activity(guild_id)
        return self._aggregate(period, window, sessions, earliest)

    def _aggregate(
        self,
        period: ServerGameStatisticsPeriod,
        window: ServerGameStatisticsWindow,
        sessions: tuple[ServerGameSessionSlice, ...],
        earliest: datetime | None,
    ) -> ServerGameStatistics:
        total_seconds = 0.0
        games: dict[str, _GameAccumulator] = {}
        players: dict[int, _PlayerAccumulator] = {}
        daily_seconds: defaultdict[date, float] = defaultdict(float)
        daily_users: defaultdict[date, set[int]] = defaultdict(set)

        for item in sessions:
            started_at = max(item.started_at, window.started_at)
            ended_at = min(item.effective_end, window.ended_at)
            if ended_at <= started_at:
                continue
            duration = (ended_at - started_at).total_seconds()
            total_seconds += duration
            canonical = canonicalize_game_name(item.game_name)

            game = games.get(canonical.key)
            selection = select_canonical_display_name(
                game.name_selection if game else None,
                canonical,
                observed_at=ended_at,
                session_id=item.session_id,
            )
            if game is None:
                games[canonical.key] = _GameAccumulator(
                    selection,
                    duration,
                    {item.user_id},
                )
            else:
                game.name_selection = selection
                game.seconds += duration
                game.users.add(item.user_id)

            player = players.get(item.user_id)
            if player is None:
                player = _PlayerAccumulator(item.display_name, 0.0, set(), set())
                players[item.user_id] = player
            player.seconds += duration
            player.games.add(canonical.key)

            for local_date, seconds in _split_local_dates(
                started_at,
                ended_at,
                self._report_timezone,
            ):
                daily_seconds[local_date] += seconds
                daily_users[local_date].add(item.user_id)
                player.gaming_days.add(local_date)

        daily = tuple(
            ServerGameDailyPoint(
                local_date,
                int(daily_seconds[local_date]),
                len(daily_users[local_date]),
            )
            for local_date in _dates(window.started_on, window.ended_before)
        )
        top_games = tuple(
            ServerGameTopGame(
                key,
                value.name_selection.display_name,
                int(value.seconds),
                len(value.users),
            )
            for key, value in sorted(
                games.items(),
                key=lambda entry: (
                    -entry[1].seconds,
                    entry[1].name_selection.display_name.casefold(),
                    entry[0],
                ),
            )[:10]
        )
        top_players = tuple(
            ServerGameTopPlayer(
                user_id,
                value.display_name,
                int(value.seconds),
                len(value.games),
                len(value.gaming_days),
            )
            for user_id, value in sorted(
                players.items(),
                key=lambda entry: (
                    -entry[1].seconds,
                    entry[1].display_name.casefold(),
                    entry[0],
                ),
            )[:10]
        )
        earliest_timestamp = (
            normalize_observed_at(earliest) if earliest is not None else None
        )
        earliest_on = (
            earliest_timestamp.astimezone(self._report_timezone).date()
            if earliest_timestamp is not None
            else None
        )
        active_gamers = len(players)
        total = int(total_seconds)
        return ServerGameStatistics(
            period,
            window,
            total,
            active_gamers,
            len(games),
            total // active_gamers if active_gamers else 0,
            daily,
            top_games,
            top_players,
            earliest_on,
            window.started_at < earliest_timestamp
            if earliest_timestamp is not None
            else None,
        )


def _split_local_dates(
    started_at: datetime,
    ended_at: datetime,
    timezone: ZoneInfo,
) -> tuple[tuple[date, float], ...]:
    cursor = started_at
    parts: list[tuple[date, float]] = []
    while cursor < ended_at:
        local_date = cursor.astimezone(timezone).date()
        next_midnight = datetime.combine(
            local_date + timedelta(days=1),
            time.min,
            tzinfo=timezone,
        ).astimezone(UTC)
        boundary = min(next_midnight, ended_at)
        if boundary <= cursor:  # pragma: no cover - exotic timezone defense
            raise ValueError("could not resolve the next local date boundary")
        parts.append((local_date, (boundary - cursor).total_seconds()))
        cursor = boundary
    return tuple(parts)


def _dates(started_on: date, ended_before: date) -> tuple[date, ...]:
    return tuple(
        started_on + timedelta(days=offset)
        for offset in range((ended_before - started_on).days)
    )
