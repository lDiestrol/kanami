"""Discord-independent types for live voice state transitions."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


def normalize_observed_at(value: datetime) -> datetime:
    """Validate an observed timestamp and normalize it to UTC."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    return value.astimezone(UTC)


class VoiceTransitionResult(StrEnum):
    """Outcome of applying one live voice state observation."""

    JOINED = "joined"
    MOVED = "moved"
    LEFT = "left"
    UNCHANGED = "unchanged"
    IGNORED_STALE = "ignored_stale"


@dataclass(frozen=True, slots=True)
class ObservedVoiceState:
    """Current connected state observed by an external adapter."""

    guild_id: int
    user_id: int
    channel_id: int
    channel_kind: str
    is_afk: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.channel_kind not in {"voice", "stage"}:
            raise ValueError("channel_kind must be 'voice' or 'stage'")
        object.__setattr__(
            self,
            "observed_at",
            normalize_observed_at(self.observed_at),
        )


@dataclass(frozen=True, slots=True)
class OpenVoiceState:
    """Minimal persisted state needed to decide the next transition."""

    session_id: int
    interval_id: int
    confirmed_through_at: datetime
    channel_id: int
    channel_kind: str
    is_afk: bool

    def has_same_channel_snapshot(self, observed: ObservedVoiceState) -> bool:
        return (
            self.channel_id == observed.channel_id
            and self.channel_kind == observed.channel_kind
            and self.is_afk == observed.is_afk
        )
