"""Managed Kanami capability feature exports."""

from discord_stats_bot.features.capabilities.presentation import (
    CapabilityPresentationSubjects,
)
from discord_stats_bot.features.capabilities.registry import (
    CAPABILITY_REGISTRY,
    VOICE_MOVE,
    UnknownCapabilityError,
    get_capability,
)
from discord_stats_bot.features.capabilities.service import (
    CAPABILITY_GRANTED_EVENT_TYPE,
    CAPABILITY_POLICY_CHANGED_EVENT_TYPE,
    CAPABILITY_REVOKED_EVENT_TYPE,
    CapabilityAuthorizationService,
    CapabilityMutationService,
    CapabilityRepository,
)
from discord_stats_bot.features.capabilities.types import (
    AuthorizationDecision,
    AuthorizationReason,
    CapabilityDefinition,
    CapabilityGrant,
    CapabilityKey,
    CapabilityMutationResult,
    CapabilitySubjectType,
    GuildCapabilityPolicy,
)

__all__ = [
    "CAPABILITY_GRANTED_EVENT_TYPE",
    "CAPABILITY_POLICY_CHANGED_EVENT_TYPE",
    "CAPABILITY_REGISTRY",
    "CAPABILITY_REVOKED_EVENT_TYPE",
    "VOICE_MOVE",
    "AuthorizationDecision",
    "AuthorizationReason",
    "CapabilityAuthorizationService",
    "CapabilityDefinition",
    "CapabilityGrant",
    "CapabilityKey",
    "CapabilityMutationResult",
    "CapabilityMutationService",
    "CapabilityPresentationSubjects",
    "CapabilityRepository",
    "CapabilitySubjectType",
    "GuildCapabilityPolicy",
    "UnknownCapabilityError",
    "get_capability",
]
