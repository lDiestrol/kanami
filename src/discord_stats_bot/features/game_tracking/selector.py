"""Deterministic selection of one privacy-minimal Playing activity."""

import unicodedata
from collections.abc import Iterable

from discord_stats_bot.features.game_tracking.types import (
    GameActivitySnapshot,
    TrackedGame,
)


def normalize_game_name(value: str) -> str:
    """Normalize a display name only for fallback identity comparison."""

    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def tracked_game_from_activity(
    activity: GameActivitySnapshot,
) -> TrackedGame | None:
    if activity.activity_type.casefold() != "playing":
        return None
    if activity.name is None:
        return None
    display_name = " ".join(activity.name.split())
    normalized_name = normalize_game_name(display_name)
    if not normalized_name:
        return None
    application_id = activity.application_id
    if application_id is not None and application_id <= 0:
        application_id = None
    key = (
        f"application:{application_id}"
        if application_id is not None
        else f"name:{normalized_name}"
    )
    return TrackedGame(key, display_name, application_id)


def select_tracked_game(
    activities: Iterable[GameActivitySnapshot],
    *,
    current_game_key: str | None = None,
) -> TrackedGame | None:
    """Select at most one game, preserving the current identity across reorders."""

    candidates: dict[str, TrackedGame] = {}
    for activity in activities:
        candidate = tracked_game_from_activity(activity)
        if candidate is not None:
            candidates.setdefault(candidate.key, candidate)
    if current_game_key is not None and current_game_key in candidates:
        return candidates[current_game_key]
    if not candidates:
        return None
    return min(candidates.values(), key=lambda item: (item.key, item.name.casefold()))
