from datetime import UTC, datetime, timedelta, timezone

import pytest

from discord_stats_bot.features.audit_logging import AuditEventRecord
from discord_stats_bot.features.capabilities import (
    CAPABILITY_REGISTRY,
    VOICE_MOVE,
    AuthorizationReason,
    CapabilityAuthorizationService,
    CapabilityAuthorizationState,
    CapabilityGrant,
    CapabilityMutationService,
    CapabilitySubjectType,
    GuildCapabilityPolicy,
    UnknownCapabilityError,
    get_capability,
)

T0 = datetime(2026, 9, 7, 12, tzinfo=UTC)


class MemoryRepository:
    def __init__(self) -> None:
        self.policy: GuildCapabilityPolicy | None = None
        self.grants: dict[tuple[CapabilitySubjectType, int], CapabilityGrant] = {}
        self.locks = 0

    async def lock_guild(self, guild_id: int) -> None:
        self.locks += 1

    async def get_policy(self, guild_id: int, capability: str):
        return self.policy

    async def get_authorization_state(self, guild_id, capability, user_id, role_ids):
        return CapabilityAuthorizationState(
            None if self.policy is None else self.policy.enabled,
            (CapabilitySubjectType.USER, user_id) in self.grants,
            any(
                (CapabilitySubjectType.ROLE, role_id) in self.grants
                for role_id in role_ids
            ),
        )

    async def set_policy(self, **values: object) -> GuildCapabilityPolicy:
        self.policy = GuildCapabilityPolicy(**values)  # type: ignore[arg-type]
        return self.policy

    async def list_grants(self, guild_id: int, capability: str):
        return tuple(self.grants.values())

    async def has_user_grant(
        self, guild_id: int, capability: str, user_id: int
    ) -> bool:
        return (CapabilitySubjectType.USER, user_id) in self.grants

    async def has_any_role_grant(
        self, guild_id: int, capability: str, role_ids: object
    ) -> bool:
        return any(
            (CapabilitySubjectType.ROLE, role_id) in self.grants
            for role_id in role_ids  # type: ignore[union-attr]
        )

    async def add_grant(self, **values: object) -> CapabilityGrant | None:
        key = (values["subject_type"], values["subject_id"])
        if key in self.grants:
            return None
        grant = CapabilityGrant(**values)  # type: ignore[arg-type]
        self.grants[key] = grant  # type: ignore[index]
        return grant

    async def remove_grant(self, **values: object) -> bool:
        key = (values["subject_type"], values["subject_id"])
        return self.grants.pop(key, None) is not None  # type: ignore[arg-type]


class MemoryAuditRepository:
    def __init__(self, *, fail: bool = False) -> None:
        self.drafts = []
        self.suppressed = []
        self.fail = fail

    async def create(self, draft, *, expires_at):
        if self.fail:
            raise RuntimeError("audit failed")
        self.drafts.append(draft)
        return AuditEventRecord(
            id=len(self.drafts),
            guild_id=draft.guild_id,
            category=draft.category,
            event_type=draft.event_type,
            occurred_at=draft.occurred_at,
            created_at=draft.occurred_at,
            subject_type=draft.subject_type,
            subject_id=draft.subject_id,
            actor_user_id=draft.actor_user_id,
            channel_id=None,
            before_data=draft.before_data,
            after_data=draft.after_data,
            details_data=draft.details_data,
            discord_message_id=None,
            delivered_at=None,
            delivery_attempts=0,
            next_delivery_attempt_at=None,
            last_delivery_error=None,
            expires_at=expires_at,
        )

    async def create_many(self, events):
        return tuple()

    async def mark_delivery_suppressed(self, event_ids, suppressed_at):
        self.suppressed.append((tuple(event_ids), suppressed_at))


def services(repository=None, audit=None):
    repository = repository or MemoryRepository()
    audit = audit or MemoryAuditRepository()
    return (
        repository,
        audit,
        CapabilityAuthorizationService(repository),
        CapabilityMutationService(repository, audit),
    )


def test_registry_contains_only_voice_move_disabled_by_default() -> None:
    assert set(CAPABILITY_REGISTRY) == {VOICE_MOVE}
    assert get_capability("voice.move").default_enabled is False
    with pytest.raises(UnknownCapabilityError):
        get_capability("moderation.ban")


def test_types_validate_ids_subject_type_and_utc() -> None:
    with pytest.raises(ValueError, match="guild_id must be positive"):
        GuildCapabilityPolicy(0, VOICE_MOVE, False, T0, 1)
    with pytest.raises(ValueError, match="subject_id must be positive"):
        CapabilityGrant(1, VOICE_MOVE, CapabilitySubjectType.USER, 0, T0, 2)
    with pytest.raises(ValueError):
        CapabilitySubjectType("group")
    with pytest.raises(ValueError, match="timezone-aware"):
        GuildCapabilityPolicy(1, VOICE_MOVE, False, datetime(2026, 1, 1), 2)
    shifted = GuildCapabilityPolicy(
        1, VOICE_MOVE, False, T0.astimezone(timezone(timedelta(hours=3))), 2
    )
    assert shifted.updated_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_disabled_denies_even_guild_owner() -> None:
    _, _, authorization, _ = services()
    decision = await authorization.authorize(
        guild_id=1,
        capability=VOICE_MOVE,
        user_id=2,
        role_ids=(),
        is_guild_owner=True,
    )
    assert decision.allowed is False
    assert decision.reason is AuthorizationReason.DISABLED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner", "user_grant", "role_ids", "role_grant", "expected"),
    [
        (True, False, (), False, AuthorizationReason.GUILD_OWNER),
        (False, True, (), False, AuthorizationReason.USER_GRANT),
        (False, False, (8,), True, AuthorizationReason.ROLE_GRANT),
        (False, False, (7, 8, 9), True, AuthorizationReason.ROLE_GRANT),
        (False, False, (7, 9), True, AuthorizationReason.NOT_GRANTED),
    ],
)
async def test_enabled_authorization_order(
    owner, user_grant, role_ids, role_grant, expected
) -> None:
    repository = MemoryRepository()
    repository.policy = GuildCapabilityPolicy(1, VOICE_MOVE, True, T0, 99)
    if user_grant:
        repository.grants[(CapabilitySubjectType.USER, 2)] = CapabilityGrant(
            1, VOICE_MOVE, CapabilitySubjectType.USER, 2, T0, 99
        )
    if role_grant:
        repository.grants[(CapabilitySubjectType.ROLE, 8)] = CapabilityGrant(
            1, VOICE_MOVE, CapabilitySubjectType.ROLE, 8, T0, 99
        )
    authorization = CapabilityAuthorizationService(repository)

    decision = await authorization.authorize(
        guild_id=1,
        capability=VOICE_MOVE,
        user_id=2,
        role_ids=role_ids,
        is_guild_owner=owner,
    )

    assert decision.reason is expected
    assert decision.allowed is (expected is not AuthorizationReason.NOT_GRANTED)


@pytest.mark.asyncio
async def test_policy_mutations_are_idempotent_and_audited() -> None:
    repository, audit, _, mutation = services()

    enabled = await mutation.set_enabled(
        guild_id=1,
        capability=VOICE_MOVE,
        enabled=True,
        actor_user_id=99,
        occurred_at=T0,
    )
    duplicate = await mutation.set_enabled(
        guild_id=1,
        capability=VOICE_MOVE,
        enabled=True,
        actor_user_id=99,
        occurred_at=T0,
    )
    disabled = await mutation.set_enabled(
        guild_id=1,
        capability=VOICE_MOVE,
        enabled=False,
        actor_user_id=99,
        occurred_at=T0,
    )

    assert (enabled.changed, duplicate.changed, disabled.changed) == (True, False, True)
    assert [draft.event_type for draft in audit.drafts] == [
        "web_admin.capability_policy_changed",
        "web_admin.capability_policy_changed",
    ]
    assert audit.drafts[0].before_data == {"enabled": False}
    assert audit.drafts[0].after_data == {"enabled": True}
    assert repository.locks == 3


@pytest.mark.asyncio
async def test_grant_and_revoke_are_idempotent_and_audited() -> None:
    repository, audit, _, mutation = services()

    user = await mutation.grant_user(
        guild_id=1,
        capability=VOICE_MOVE,
        subject_id=2,
        actor_user_id=99,
        occurred_at=T0,
    )
    duplicate = await mutation.grant_user(
        guild_id=1,
        capability=VOICE_MOVE,
        subject_id=2,
        actor_user_id=99,
        occurred_at=T0,
    )
    role = await mutation.grant_role(
        guild_id=1,
        capability=VOICE_MOVE,
        subject_id=8,
        actor_user_id=99,
        occurred_at=T0,
    )
    revoked = await mutation.revoke(
        guild_id=1,
        capability=VOICE_MOVE,
        subject_type=CapabilitySubjectType.USER,
        subject_id=2,
        actor_user_id=99,
        occurred_at=T0,
    )
    repeated = await mutation.revoke(
        guild_id=1,
        capability=VOICE_MOVE,
        subject_type=CapabilitySubjectType.USER,
        subject_id=2,
        actor_user_id=99,
        occurred_at=T0,
    )

    assert [
        result.changed for result in (user, duplicate, role, revoked, repeated)
    ] == [
        True,
        False,
        True,
        True,
        False,
    ]
    assert [draft.event_type for draft in audit.drafts] == [
        "web_admin.capability_granted",
        "web_admin.capability_granted",
        "web_admin.capability_revoked",
    ]
    assert audit.drafts[1].details_data["subject_type"] == "role"


@pytest.mark.asyncio
async def test_user_grant_and_revoke_audits_keep_subject_and_actor_identity() -> None:
    _, audit, _, mutation = services()

    granted = await mutation.grant_user(
        guild_id=1,
        capability=VOICE_MOVE,
        subject_id=42,
        actor_user_id=99,
        occurred_at=T0,
    )
    revoked = await mutation.revoke(
        guild_id=1,
        capability=VOICE_MOVE,
        subject_type=CapabilitySubjectType.USER,
        subject_id=42,
        actor_user_id=99,
        occurred_at=T0,
    )

    assert (granted.changed, revoked.changed) == (True, True)
    assert [draft.event_type for draft in audit.drafts] == [
        "web_admin.capability_granted",
        "web_admin.capability_revoked",
    ]
    for draft in audit.drafts:
        assert (draft.subject_type, draft.subject_id, draft.actor_user_id) == (
            "discord_user",
            42,
            99,
        )
        assert draft.details_data["subject_type"] == "user"


@pytest.mark.asyncio
async def test_mutations_reject_unknown_capability() -> None:
    *_, mutation = services()
    with pytest.raises(UnknownCapabilityError):
        await mutation.set_enabled(
            guild_id=1,
            capability="voice.disconnect",
            enabled=True,
            actor_user_id=2,
            occurred_at=T0,
        )


@pytest.mark.asyncio
async def test_audit_failure_propagates_to_caller_transaction() -> None:
    repository = MemoryRepository()
    audit = MemoryAuditRepository(fail=True)
    mutation = CapabilityMutationService(repository, audit)

    with pytest.raises(RuntimeError, match="audit failed"):
        await mutation.set_enabled(
            guild_id=1,
            capability=VOICE_MOVE,
            enabled=True,
            actor_user_id=2,
            occurred_at=T0,
        )


@pytest.mark.asyncio
async def test_user_audit_failure_propagates_to_caller_transaction() -> None:
    mutation = CapabilityMutationService(
        MemoryRepository(), MemoryAuditRepository(fail=True)
    )

    with pytest.raises(RuntimeError, match="audit failed"):
        await mutation.grant_user(
            guild_id=1,
            capability=VOICE_MOVE,
            subject_id=42,
            actor_user_id=2,
            occurred_at=T0,
        )


def test_authorization_api_has_no_discord_permission_argument() -> None:
    import inspect

    parameters = inspect.signature(CapabilityAuthorizationService.authorize).parameters
    assert "administrator" not in parameters
    assert "permissions" not in parameters
