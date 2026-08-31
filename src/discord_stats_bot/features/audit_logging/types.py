"""Discord-independent audit event types."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

AuditData = Mapping[str, Any]


class AuditRetentionPolicy(StrEnum):
    """Retention class applied when an event is persisted."""

    TRANSIENT = "transient"
    IMPORTANT = "important"


TRANSIENT_EVENT_TYPES = frozenset(
    {
        "voice.joined",
        "voice.left",
        "voice.moved",
        "user.avatar_updated",
        "user.username_updated",
        "member.nickname_updated",
        "member.guild_avatar_updated",
    }
)
IMPORTANT_EVENT_TYPES = frozenset(
    {
        "member.joined",
        "member.left",
        "member.returned",
        "member.roles_updated",
        "member.timeout_updated",
        "member.anniversary",
        "channel.created",
        "channel.deleted",
        "channel.updated",
        "role.created",
        "role.deleted",
        "role.updated",
        "moderation.banned",
        "moderation.unbanned",
        "web_admin.access_granted",
        "web_admin.access_revoked",
        "web_admin.server_setting_changed",
        "rules.draft_created",
        "rules.draft_updated",
        "rules.draft_deleted",
        "rules.published",
    }
)
SUPPORTED_EVENT_TYPES = TRANSIENT_EVENT_TYPES | IMPORTANT_EVENT_TYPES
SUPPORTED_CATEGORIES = frozenset(
    {"member", "voice", "server", "moderation", "web_admin"}
)


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_snowflake(value: int | None, field_name: str) -> None:
    if value is not None and value <= 0:
        raise ValueError(f"{field_name} must be positive when provided")


@dataclass(frozen=True, slots=True)
class AuditEventDraft:
    """Normalized event ready for retention calculation and persistence."""

    guild_id: int
    category: str
    event_type: str
    occurred_at: datetime
    subject_type: str
    subject_id: int | None = None
    actor_user_id: int | None = None
    channel_id: int | None = None
    before_data: AuditData = field(default_factory=dict)
    after_data: AuditData = field(default_factory=dict)
    details_data: AuditData = field(default_factory=dict)
    retention_policy: AuditRetentionPolicy | None = None

    def __post_init__(self) -> None:
        _validate_snowflake(self.guild_id, "guild_id")
        _validate_snowflake(self.subject_id, "subject_id")
        _validate_snowflake(self.actor_user_id, "actor_user_id")
        _validate_snowflake(self.channel_id, "channel_id")
        if self.category not in SUPPORTED_CATEGORIES:
            raise ValueError(f"unsupported audit category: {self.category}")
        if self.event_type not in SUPPORTED_EVENT_TYPES:
            raise ValueError(f"unsupported audit event_type: {self.event_type}")
        if not self.subject_type.strip():
            raise ValueError("subject_type must not be empty")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))
        expected_policy = (
            AuditRetentionPolicy.TRANSIENT
            if self.event_type in TRANSIENT_EVENT_TYPES
            else AuditRetentionPolicy.IMPORTANT
        )
        if self.retention_policy is None:
            object.__setattr__(self, "retention_policy", expected_policy)
        elif self.retention_policy is not expected_policy:
            raise ValueError(
                f"{self.event_type} requires {expected_policy.value} retention"
            )
        for field_name in ("before_data", "after_data", "details_data"):
            value = getattr(self, field_name)
            if not isinstance(value, Mapping):
                raise TypeError(f"{field_name} must be a mapping")
            object.__setattr__(self, field_name, dict(value))


@dataclass(frozen=True, slots=True)
class AuditEventRecord:
    """Persisted audit event used by delivery and future history queries."""

    id: int
    guild_id: int
    category: str
    event_type: str
    occurred_at: datetime
    created_at: datetime
    subject_type: str
    subject_id: int | None
    actor_user_id: int | None
    channel_id: int | None
    before_data: AuditData
    after_data: AuditData
    details_data: AuditData
    discord_message_id: int | None
    delivered_at: datetime | None
    delivery_attempts: int
    next_delivery_attempt_at: datetime | None
    last_delivery_error: str | None
    expires_at: datetime | None

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("id must be positive")
        _validate_snowflake(self.guild_id, "guild_id")
        object.__setattr__(self, "occurred_at", _utc(self.occurred_at, "occurred_at"))
        object.__setattr__(self, "created_at", _utc(self.created_at, "created_at"))
        if self.delivered_at is not None:
            object.__setattr__(
                self, "delivered_at", _utc(self.delivered_at, "delivered_at")
            )
        if self.next_delivery_attempt_at is not None:
            object.__setattr__(
                self,
                "next_delivery_attempt_at",
                _utc(self.next_delivery_attempt_at, "next_delivery_attempt_at"),
            )
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _utc(self.expires_at, "expires_at"))


@dataclass(frozen=True, slots=True)
class VoiceAuditTransitionTiming:
    """Voice-table boundaries for one just-applied leave or move."""

    session_started_at: datetime
    previous_interval_seconds: int
    current_session_seconds: int
    counted_exact_session_seconds: int
    previous_interval_is_afk: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "session_started_at",
            _utc(self.session_started_at, "session_started_at"),
        )
        for field_name in (
            "previous_interval_seconds",
            "current_session_seconds",
            "counted_exact_session_seconds",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must not be negative")
