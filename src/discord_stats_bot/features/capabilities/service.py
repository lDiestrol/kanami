"""Authorization and mutation orchestration for managed capabilities."""

from collections.abc import Collection, Sequence
from datetime import UTC, datetime
from typing import Protocol

from discord_stats_bot.features.audit_logging import (
    AuditEventDraft,
    AuditEventRecord,
    AuditLoggingService,
)
from discord_stats_bot.features.capabilities.registry import get_capability
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

CAPABILITY_POLICY_CHANGED_EVENT_TYPE = "web_admin.capability_policy_changed"
CAPABILITY_GRANTED_EVENT_TYPE = "web_admin.capability_granted"
CAPABILITY_REVOKED_EVENT_TYPE = "web_admin.capability_revoked"
CAPABILITY_AUDIT_CATEGORY = "web_admin"


class CapabilityRepository(Protocol):
    async def lock_guild(self, guild_id: int) -> None: ...

    async def get_policy(
        self, guild_id: int, capability: CapabilityKey
    ) -> GuildCapabilityPolicy | None: ...

    async def set_policy(
        self,
        *,
        guild_id: int,
        capability: CapabilityKey,
        enabled: bool,
        updated_at: datetime,
        updated_by_user_id: int,
    ) -> GuildCapabilityPolicy: ...

    async def list_grants(
        self, guild_id: int, capability: CapabilityKey
    ) -> tuple[CapabilityGrant, ...]: ...

    async def has_user_grant(
        self, guild_id: int, capability: CapabilityKey, user_id: int
    ) -> bool: ...

    async def has_any_role_grant(
        self, guild_id: int, capability: CapabilityKey, role_ids: Collection[int]
    ) -> bool: ...

    async def add_grant(
        self,
        *,
        guild_id: int,
        capability: CapabilityKey,
        subject_type: CapabilitySubjectType,
        subject_id: int,
        created_at: datetime,
        created_by_user_id: int,
    ) -> CapabilityGrant | None: ...

    async def remove_grant(
        self,
        *,
        guild_id: int,
        capability: CapabilityKey,
        subject_type: CapabilitySubjectType,
        subject_id: int,
    ) -> bool: ...


class CapabilityAuditRepository(Protocol):
    async def create(
        self, draft: AuditEventDraft, *, expires_at: datetime | None
    ) -> AuditEventRecord: ...

    async def create_many(
        self, events: Sequence[tuple[AuditEventDraft, datetime | None]]
    ) -> tuple[AuditEventRecord, ...]: ...

    async def mark_delivery_suppressed(
        self, event_ids: Sequence[int], suppressed_at: datetime
    ) -> None: ...


def _positive(value: int, field_name: str) -> None:
    if value <= 0:
        raise ValueError(f"{field_name} must be positive")


def _occurred_at(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must be timezone-aware")
    return value.astimezone(UTC)


class CapabilityAuthorizationService:
    """Evaluate only Kanami access grants, never Discord permission bits."""

    def __init__(self, repository: CapabilityRepository) -> None:
        self._repository = repository

    async def authorize(
        self,
        *,
        guild_id: int,
        capability: CapabilityKey | str,
        user_id: int,
        role_ids: Collection[int],
        is_guild_owner: bool,
    ) -> AuthorizationDecision:
        _positive(guild_id, "guild_id")
        _positive(user_id, "user_id")
        if any(role_id <= 0 for role_id in role_ids):
            raise ValueError("role_ids must contain only positive values")
        definition = get_capability(capability)
        policy = await self._repository.get_policy(guild_id, definition.key)
        enabled = definition.default_enabled if policy is None else policy.enabled
        if not enabled:
            return AuthorizationDecision(False, AuthorizationReason.DISABLED)
        if is_guild_owner:
            return AuthorizationDecision(True, AuthorizationReason.GUILD_OWNER)
        if await self._repository.has_user_grant(guild_id, definition.key, user_id):
            return AuthorizationDecision(True, AuthorizationReason.USER_GRANT)
        if role_ids and await self._repository.has_any_role_grant(
            guild_id, definition.key, role_ids
        ):
            return AuthorizationDecision(True, AuthorizationReason.ROLE_GRANT)
        return AuthorizationDecision(False, AuthorizationReason.NOT_GRANTED)


class CapabilityMutationService:
    """Mutate capability state and append audit in the caller transaction."""

    def __init__(
        self,
        repository: CapabilityRepository,
        audit_repository: CapabilityAuditRepository,
    ) -> None:
        self._repository = repository
        self._audit_repository = audit_repository
        self._audit_service = AuditLoggingService(audit_repository)

    async def set_enabled(
        self,
        *,
        guild_id: int,
        capability: CapabilityKey | str,
        enabled: bool,
        actor_user_id: int,
        occurred_at: datetime,
    ) -> CapabilityMutationResult:
        definition, occurred_at = self._validate_mutation(
            guild_id, capability, actor_user_id, occurred_at
        )
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool")
        await self._repository.lock_guild(guild_id)
        current = await self._repository.get_policy(guild_id, definition.key)
        before = definition.default_enabled if current is None else current.enabled
        if before is enabled:
            return CapabilityMutationResult(False)
        await self._repository.set_policy(
            guild_id=guild_id,
            capability=definition.key,
            enabled=enabled,
            updated_at=occurred_at,
            updated_by_user_id=actor_user_id,
        )
        await self._write_audit(
            AuditEventDraft(
                guild_id=guild_id,
                category=CAPABILITY_AUDIT_CATEGORY,
                event_type=CAPABILITY_POLICY_CHANGED_EVENT_TYPE,
                occurred_at=occurred_at,
                subject_type="guild_capability",
                actor_user_id=actor_user_id,
                before_data={"enabled": before},
                after_data={"enabled": enabled},
                details_data={"capability_key": definition.key},
            ),
            occurred_at,
        )
        return CapabilityMutationResult(True)

    async def grant_user(
        self,
        *,
        guild_id: int,
        capability: CapabilityKey | str,
        subject_id: int,
        actor_user_id: int,
        occurred_at: datetime,
    ) -> CapabilityMutationResult:
        return await self._grant(
            CapabilitySubjectType.USER,
            guild_id=guild_id,
            capability=capability,
            subject_id=subject_id,
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
        )

    async def grant_role(
        self,
        *,
        guild_id: int,
        capability: CapabilityKey | str,
        subject_id: int,
        actor_user_id: int,
        occurred_at: datetime,
    ) -> CapabilityMutationResult:
        return await self._grant(
            CapabilitySubjectType.ROLE,
            guild_id=guild_id,
            capability=capability,
            subject_id=subject_id,
            actor_user_id=actor_user_id,
            occurred_at=occurred_at,
        )

    async def _grant(
        self,
        subject_type: CapabilitySubjectType,
        *,
        guild_id: int,
        capability: CapabilityKey | str,
        subject_id: int,
        actor_user_id: int,
        occurred_at: datetime,
    ) -> CapabilityMutationResult:
        definition, occurred_at = self._validate_mutation(
            guild_id, capability, actor_user_id, occurred_at
        )
        if not isinstance(subject_type, CapabilitySubjectType):
            raise ValueError("subject_type must be user or role")
        _positive(subject_id, "subject_id")
        await self._repository.lock_guild(guild_id)
        grant = await self._repository.add_grant(
            guild_id=guild_id,
            capability=definition.key,
            subject_type=subject_type,
            subject_id=subject_id,
            created_at=occurred_at,
            created_by_user_id=actor_user_id,
        )
        if grant is None:
            return CapabilityMutationResult(False)
        await self._write_audit(
            self._grant_audit_draft(
                CAPABILITY_GRANTED_EVENT_TYPE,
                "grant",
                guild_id,
                definition.key,
                subject_type,
                subject_id,
                actor_user_id,
                occurred_at,
            ),
            occurred_at,
        )
        return CapabilityMutationResult(True)

    async def revoke(
        self,
        *,
        guild_id: int,
        capability: CapabilityKey | str,
        subject_type: CapabilitySubjectType,
        subject_id: int,
        actor_user_id: int,
        occurred_at: datetime,
    ) -> CapabilityMutationResult:
        definition, occurred_at = self._validate_mutation(
            guild_id, capability, actor_user_id, occurred_at
        )
        if not isinstance(subject_type, CapabilitySubjectType):
            raise ValueError("subject_type must be user or role")
        _positive(subject_id, "subject_id")
        await self._repository.lock_guild(guild_id)
        changed = await self._repository.remove_grant(
            guild_id=guild_id,
            capability=definition.key,
            subject_type=subject_type,
            subject_id=subject_id,
        )
        if not changed:
            return CapabilityMutationResult(False)
        await self._write_audit(
            self._grant_audit_draft(
                CAPABILITY_REVOKED_EVENT_TYPE,
                "revoke",
                guild_id,
                definition.key,
                subject_type,
                subject_id,
                actor_user_id,
                occurred_at,
            ),
            occurred_at,
        )
        return CapabilityMutationResult(True)

    @staticmethod
    def _validate_mutation(
        guild_id: int,
        capability: CapabilityKey | str,
        actor_user_id: int,
        occurred_at: datetime,
    ) -> tuple[CapabilityDefinition, datetime]:
        _positive(guild_id, "guild_id")
        _positive(actor_user_id, "actor_user_id")
        return get_capability(capability), _occurred_at(occurred_at)

    @staticmethod
    def _grant_audit_draft(
        event_type: str,
        action: str,
        guild_id: int,
        capability: CapabilityKey,
        subject_type: CapabilitySubjectType,
        subject_id: int,
        actor_user_id: int,
        occurred_at: datetime,
    ) -> AuditEventDraft:
        return AuditEventDraft(
            guild_id=guild_id,
            category=CAPABILITY_AUDIT_CATEGORY,
            event_type=event_type,
            occurred_at=occurred_at,
            subject_type=f"discord_{subject_type.value}",
            subject_id=subject_id,
            actor_user_id=actor_user_id,
            details_data={
                "capability_key": capability,
                "action": action,
                "subject_type": subject_type.value,
                "subject_id": subject_id,
            },
        )

    async def _write_audit(self, draft: AuditEventDraft, occurred_at: datetime) -> None:
        record = await self._audit_service.create(draft)
        await self._audit_repository.mark_delivery_suppressed((record.id,), occurred_at)
