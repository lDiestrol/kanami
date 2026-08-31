"""Discord-independent records returned by Rules v1."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RulesetStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


@dataclass(frozen=True, slots=True)
class RulesetRecord:
    id: int
    guild_id: int
    version: str
    title: str
    content: str
    status: RulesetStatus
    change_summary: str | None
    requires_reacceptance: bool
    created_by: int | None
    created_at: datetime
    published_at: datetime | None
    reacceptance_grace_days: int | None = None


@dataclass(frozen=True, slots=True)
class RuleAcceptanceResult:
    ruleset: RulesetRecord
    newly_accepted: bool


@dataclass(frozen=True, slots=True)
class RulesAcceptanceStatistics:
    ruleset: RulesetRecord
    accepted_count: int


@dataclass(frozen=True, slots=True)
class RulesetWithAcceptanceCount:
    ruleset: RulesetRecord
    accepted_count: int


@dataclass(frozen=True, slots=True)
class RuleAcceptanceRecord:
    user_id: int
    display_name: str | None
    accepted_at: datetime


class RulesComplianceAvailability(StrEnum):
    AVAILABLE = "available"
    NO_PUBLISHED_RULES = "no_published_rules"


class RulesComplianceStatus(StrEnum):
    COMPLIANT = "compliant"
    PENDING = "pending"
    OVERDUE = "overdue"


@dataclass(frozen=True, slots=True)
class RulesComplianceAcceptance:
    ruleset_id: int
    version: str
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class RulesComplianceResult:
    availability: RulesComplianceAvailability
    guild_id: int
    user_id: int
    current_ruleset_id: int | None = None
    current_version: str | None = None
    required_checkpoint_ruleset_id: int | None = None
    required_checkpoint_version: str | None = None
    checkpoint_requires_reacceptance: bool | None = None
    status: RulesComplianceStatus | None = None
    latest_qualifying_acceptance: RulesComplianceAcceptance | None = None
    deadline: datetime | None = None


@dataclass(frozen=True, slots=True)
class RulesComplianceSummary:
    availability: RulesComplianceAvailability
    guild_id: int
    total: int = 0
    compliant: int = 0
    pending: int = 0
    overdue: int = 0
    current_ruleset_id: int | None = None
    current_version: str | None = None
    required_checkpoint_ruleset_id: int | None = None
    required_checkpoint_version: str | None = None
    checkpoint_requires_reacceptance: bool | None = None
    deadline: datetime | None = None


class RulesPublicationSyncStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    NO_PUBLISHED_RULESET = "no_published_ruleset"
    CREATED = "created"
    UPDATED = "updated"
    RECREATED = "recreated"
    ALREADY_CURRENT = "already_current"
    CHANNEL_UNAVAILABLE = "channel_unavailable"
    UNSUPPORTED_CHANNEL = "unsupported_channel"
    FORBIDDEN = "forbidden"
    DISCORD_API_FAILURE = "discord_api_failure"


class RulesPublicationConfigurationStatus(StrEnum):
    CONFIGURED = "configured"
    CHANGED = "changed"
    DISABLED = "disabled"
    ALREADY_CONFIGURED = "already_configured"
    ALREADY_DISABLED = "already_disabled"
    INVALID_CHANNEL = "invalid_channel"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    CLEANUP_FORBIDDEN = "cleanup_forbidden"
    CLEANUP_DISCORD_API_FAILURE = "cleanup_discord_api_failure"


@dataclass(frozen=True, slots=True)
class RulesPublicationState:
    guild_id: int
    channel_id: int | None
    message_id: int | None
    ruleset_id: int | None


@dataclass(frozen=True, slots=True)
class RulesPublicationSyncResult:
    status: RulesPublicationSyncStatus
    guild_id: int
    channel_id: int | None = None
    message_id: int | None = None
    ruleset_id: int | None = None
    version: str | None = None

    @property
    def failed(self) -> bool:
        return self.status in {
            RulesPublicationSyncStatus.CHANNEL_UNAVAILABLE,
            RulesPublicationSyncStatus.UNSUPPORTED_CHANNEL,
            RulesPublicationSyncStatus.FORBIDDEN,
            RulesPublicationSyncStatus.DISCORD_API_FAILURE,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "ruleset_id": self.ruleset_id,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class RulesPublicationConfigurationResult:
    status: RulesPublicationConfigurationStatus
    guild_id: int
    previous_channel_id: int | None
    channel_id: int | None
    previous_message_id: int | None

    @property
    def failed(self) -> bool:
        return self.status in {
            RulesPublicationConfigurationStatus.INVALID_CHANNEL,
            RulesPublicationConfigurationStatus.RUNTIME_UNAVAILABLE,
            RulesPublicationConfigurationStatus.CLEANUP_FORBIDDEN,
            RulesPublicationConfigurationStatus.CLEANUP_DISCORD_API_FAILURE,
        }

    @property
    def changed(self) -> bool:
        return self.status in {
            RulesPublicationConfigurationStatus.CONFIGURED,
            RulesPublicationConfigurationStatus.CHANGED,
            RulesPublicationConfigurationStatus.DISABLED,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "guild_id": self.guild_id,
            "previous_channel_id": self.previous_channel_id,
            "channel_id": self.channel_id,
            "previous_message_id": self.previous_message_id,
            "changed": self.changed,
        }
