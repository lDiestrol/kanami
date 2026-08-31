"""Shared read-side canonical identity for tracked game names."""

from dataclasses import dataclass
from datetime import datetime

_MEDAL_GAME_NAME_SUFFIX = " with Medal"


@dataclass(frozen=True, slots=True)
class CanonicalGameName:
    key: str
    display_name: str
    has_medal_suffix: bool


@dataclass(frozen=True, slots=True)
class CanonicalGameNameSelection:
    """One deterministic display-name candidate for a canonical game."""

    display_name: str
    has_medal_suffix: bool
    observed_at: datetime
    session_id: int


def canonicalize_game_name(value: str) -> CanonicalGameName:
    """Normalize a persisted display name without consulting tracker identity."""

    display_name = value.strip()
    has_medal_suffix = display_name.casefold().endswith(
        _MEDAL_GAME_NAME_SUFFIX.casefold()
    )
    if has_medal_suffix:
        display_name = display_name[: -len(_MEDAL_GAME_NAME_SUFFIX)].rstrip()
    return CanonicalGameName(
        key=display_name.casefold(),
        display_name=display_name,
        has_medal_suffix=has_medal_suffix,
    )


def select_canonical_display_name(
    current: CanonicalGameNameSelection | None,
    candidate: CanonicalGameName,
    *,
    observed_at: datetime,
    session_id: int,
) -> CanonicalGameNameSelection:
    """Preserve the established non-Medal/latest deterministic display choice."""

    proposed = CanonicalGameNameSelection(
        candidate.display_name,
        candidate.has_medal_suffix,
        observed_at,
        session_id,
    )
    if current is None:
        return proposed
    if current.has_medal_suffix and not candidate.has_medal_suffix:
        return proposed
    if current.has_medal_suffix == candidate.has_medal_suffix and (
        observed_at,
        session_id,
    ) > (current.observed_at, current.session_id):
        return proposed
    return current
