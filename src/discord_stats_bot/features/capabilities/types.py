"""Discord-independent domain types for managed Kanami capabilities."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import NewType

CapabilityKey = NewType("CapabilityKey", str)


def _positive(value: int, field_name: str) -> None:
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _capability_key(value: CapabilityKey) -> None:
    if not value or not value.strip():
        raise ValueError("capability key must not be empty")


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    key: CapabilityKey
    default_enabled: bool = False

    def __post_init__(self) -> None:
        _capability_key(self.key)


class CapabilitySubjectType(StrEnum):
    USER = "user"
    ROLE = "role"


@dataclass(frozen=True, slots=True)
class GuildCapabilityPolicy:
    guild_id: int
    capability: CapabilityKey
    enabled: bool
    updated_at: datetime
    updated_by_user_id: int

    def __post_init__(self) -> None:
        _positive(self.guild_id, "guild_id")
        _capability_key(self.capability)
        _positive(self.updated_by_user_id, "updated_by_user_id")
        object.__setattr__(self, "updated_at", _utc(self.updated_at, "updated_at"))


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    guild_id: int
    capability: CapabilityKey
    subject_type: CapabilitySubjectType
    subject_id: int
    created_at: datetime
    created_by_user_id: int

    def __post_init__(self) -> None:
        _positive(self.guild_id, "guild_id")
        _capability_key(self.capability)
        if not isinstance(self.subject_type, CapabilitySubjectType):
            raise ValueError("subject_type must be user or role")
        _positive(self.subject_id, "subject_id")
        _positive(self.created_by_user_id, "created_by_user_id")
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))


class AuthorizationReason(StrEnum):
    DISABLED = "disabled"
    GUILD_OWNER = "guild_owner"
    USER_GRANT = "user_grant"
    ROLE_GRANT = "role_grant"
    NOT_GRANTED = "not_granted"


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    allowed: bool
    reason: AuthorizationReason


@dataclass(frozen=True, slots=True)
class CapabilityMutationResult:
    changed: bool
