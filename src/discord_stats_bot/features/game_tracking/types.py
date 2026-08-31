"""Discord-independent types for tracked Playing activities."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from discord_stats_bot.features.voice.types import normalize_observed_at


@dataclass(frozen=True, slots=True)
class GameActivitySnapshot:
    """Minimal activity fields admitted from a Presence snapshot."""

    activity_type: str
    name: str | None
    application_id: int | None = None


@dataclass(frozen=True, slots=True)
class TrackedGame:
    """Validated game identity selected from one Presence snapshot."""

    key: str
    name: str
    application_id: int | None


@dataclass(frozen=True, slots=True)
class ObservedGame:
    """One selected game observed for a guild member at a UTC instant."""

    guild_id: int
    user_id: int
    game: TrackedGame
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.guild_id <= 0 or self.user_id <= 0:
            raise ValueError("guild_id and user_id must be positive")
        object.__setattr__(self, "observed_at", normalize_observed_at(self.observed_at))


@dataclass(frozen=True, slots=True)
class OpenGameSession:
    """Minimal durable state required for one game transition."""

    session_id: int
    guild_id: int
    user_id: int
    game_key: str
    game_name: str
    application_id: int | None
    started_at: datetime
    confirmed_through_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", normalize_observed_at(self.started_at))
        object.__setattr__(
            self,
            "confirmed_through_at",
            normalize_observed_at(self.confirmed_through_at),
        )


class GameTransitionResult(StrEnum):
    STARTED = "started"
    CONFIRMED = "confirmed"
    SWITCHED = "switched"
    CLOSED = "closed"
    UNCHANGED = "unchanged"
    IGNORED_STALE = "ignored_stale"


@dataclass(frozen=True, slots=True)
class GameReconciliationResult:
    reconciled_at: datetime
    observed_count: int
    closed_count: int
    started_count: int
    unchanged_count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reconciled_at",
            normalize_observed_at(self.reconciled_at),
        )


@dataclass(frozen=True, slots=True)
class GameCheckpointResult:
    checkpointed_at: datetime
    observed_count: int
    confirmed_count: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "checkpointed_at",
            normalize_observed_at(self.checkpointed_at),
        )
