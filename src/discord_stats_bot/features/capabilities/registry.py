"""Explicit registry of capabilities supported by this application version."""

from discord_stats_bot.features.capabilities.types import (
    CapabilityDefinition,
    CapabilityKey,
)

VOICE_MOVE = CapabilityKey("voice.move")

CAPABILITY_REGISTRY: dict[CapabilityKey, CapabilityDefinition] = {
    VOICE_MOVE: CapabilityDefinition(VOICE_MOVE, default_enabled=False),
}


class UnknownCapabilityError(ValueError):
    """Raised when code or persisted input names an unsupported capability."""


def get_capability(capability: CapabilityKey | str) -> CapabilityDefinition:
    key = CapabilityKey(capability)
    try:
        return CAPABILITY_REGISTRY[key]
    except KeyError as exc:
        raise UnknownCapabilityError(f"unsupported capability: {capability}") from exc
