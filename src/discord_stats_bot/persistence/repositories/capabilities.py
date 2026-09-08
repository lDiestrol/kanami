"""Caller-owned SQLAlchemy repository for managed capabilities."""

from collections.abc import Collection
from datetime import UTC, datetime

from sqlalchemy import delete, false, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.capabilities import (
    CapabilityAuthorizationState,
    CapabilityGrant,
    CapabilityKey,
    CapabilitySubjectType,
    GuildCapabilityPolicy,
)
from discord_stats_bot.persistence.models import (
    GuildCapabilityGrantModel,
    GuildCapabilityPolicyModel,
)


class SqlAlchemyCapabilityRepository:
    """Read and mutate generic capability state without transaction ownership."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_authorization_state(
        self,
        guild_id: int,
        capability: CapabilityKey,
        user_id: int,
        role_ids: Collection[int],
    ) -> CapabilityAuthorizationState:
        """One SELECT snapshot, including at READ COMMITTED; no guild lock."""
        self._validate_id(guild_id, "guild_id")
        self._validate_id(user_id, "user_id")
        self._validate_capability(capability)
        if any(role_id <= 0 for role_id in role_ids):
            raise ValueError("role_ids must contain only positive values")
        policy = select(GuildCapabilityPolicyModel.enabled).where(
            GuildCapabilityPolicyModel.guild_id == guild_id,
            GuildCapabilityPolicyModel.capability_key == capability,
        )
        grants = select(GuildCapabilityGrantModel.subject_id).where(
            GuildCapabilityGrantModel.guild_id == guild_id,
            GuildCapabilityGrantModel.capability_key == capability,
        )
        user_grant = grants.where(
            GuildCapabilityGrantModel.subject_type == CapabilitySubjectType.USER.value,
            GuildCapabilityGrantModel.subject_id == user_id,
        ).exists()
        role_grant = (
            grants.where(
                GuildCapabilityGrantModel.subject_type
                == CapabilitySubjectType.ROLE.value,
                GuildCapabilityGrantModel.subject_id.in_(role_ids),
            ).exists()
            if role_ids
            else false()
        )
        # Scalar columns avoid stale ORM identity-map values in caller sessions.
        statement = select(policy.scalar_subquery(), user_grant, role_grant)
        row = (await self._session.execute(statement)).one()
        return CapabilityAuthorizationState(*row)

    async def lock_guild(self, guild_id: int) -> None:
        self._validate_id(guild_id, "guild_id")
        await self._session.execute(select(func.pg_advisory_xact_lock(guild_id)))

    async def get_policy(
        self, guild_id: int, capability: CapabilityKey
    ) -> GuildCapabilityPolicy | None:
        self._validate_id(guild_id, "guild_id")
        statement = select(GuildCapabilityPolicyModel).where(
            GuildCapabilityPolicyModel.guild_id == guild_id,
            GuildCapabilityPolicyModel.capability_key == capability,
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return None if model is None else self._policy_record(model)

    async def set_policy(
        self,
        *,
        guild_id: int,
        capability: CapabilityKey,
        enabled: bool,
        updated_at: datetime,
        updated_by_user_id: int,
    ) -> GuildCapabilityPolicy:
        self._validate_id(guild_id, "guild_id")
        self._validate_id(updated_by_user_id, "updated_by_user_id")
        self._validate_capability(capability)
        updated_at = self._utc(updated_at, "updated_at")
        values = {
            "guild_id": guild_id,
            "capability_key": capability,
            "enabled": enabled,
            "updated_at": updated_at,
            "updated_by_user_id": updated_by_user_id,
        }
        statement = insert(GuildCapabilityPolicyModel).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[
                GuildCapabilityPolicyModel.guild_id,
                GuildCapabilityPolicyModel.capability_key,
            ],
            set_={
                "enabled": statement.excluded.enabled,
                "updated_at": statement.excluded.updated_at,
                "updated_by_user_id": statement.excluded.updated_by_user_id,
            },
        ).returning(GuildCapabilityPolicyModel)
        model = (await self._session.execute(statement)).scalar_one()
        return self._policy_record(model)

    async def list_grants(
        self, guild_id: int, capability: CapabilityKey
    ) -> tuple[CapabilityGrant, ...]:
        self._validate_id(guild_id, "guild_id")
        statement = (
            select(GuildCapabilityGrantModel)
            .where(
                GuildCapabilityGrantModel.guild_id == guild_id,
                GuildCapabilityGrantModel.capability_key == capability,
            )
            .order_by(
                GuildCapabilityGrantModel.subject_type,
                GuildCapabilityGrantModel.subject_id,
            )
        )
        models = (await self._session.execute(statement)).scalars()
        return tuple(self._grant_record(model) for model in models)

    async def has_user_grant(
        self, guild_id: int, capability: CapabilityKey, user_id: int
    ) -> bool:
        self._validate_id(guild_id, "guild_id")
        self._validate_id(user_id, "user_id")
        statement = (
            select(GuildCapabilityGrantModel.subject_id)
            .where(
                GuildCapabilityGrantModel.guild_id == guild_id,
                GuildCapabilityGrantModel.capability_key == capability,
                GuildCapabilityGrantModel.subject_type
                == CapabilitySubjectType.USER.value,
                GuildCapabilityGrantModel.subject_id == user_id,
            )
            .limit(1)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def has_any_role_grant(
        self,
        guild_id: int,
        capability: CapabilityKey,
        role_ids: Collection[int],
    ) -> bool:
        self._validate_id(guild_id, "guild_id")
        if any(role_id <= 0 for role_id in role_ids):
            raise ValueError("role_ids must contain only positive values")
        if not role_ids:
            return False
        statement = (
            select(GuildCapabilityGrantModel.subject_id)
            .where(
                GuildCapabilityGrantModel.guild_id == guild_id,
                GuildCapabilityGrantModel.capability_key == capability,
                GuildCapabilityGrantModel.subject_type
                == CapabilitySubjectType.ROLE.value,
                GuildCapabilityGrantModel.subject_id.in_(role_ids),
            )
            .limit(1)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def add_grant(
        self,
        *,
        guild_id: int,
        capability: CapabilityKey,
        subject_type: CapabilitySubjectType,
        subject_id: int,
        created_at: datetime,
        created_by_user_id: int,
    ) -> CapabilityGrant | None:
        self._validate_id(guild_id, "guild_id")
        self._validate_id(subject_id, "subject_id")
        self._validate_id(created_by_user_id, "created_by_user_id")
        self._validate_capability(capability)
        created_at = self._utc(created_at, "created_at")
        statement = (
            insert(GuildCapabilityGrantModel)
            .values(
                guild_id=guild_id,
                capability_key=capability,
                subject_type=subject_type.value,
                subject_id=subject_id,
                created_at=created_at,
                created_by_user_id=created_by_user_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    GuildCapabilityGrantModel.guild_id,
                    GuildCapabilityGrantModel.capability_key,
                    GuildCapabilityGrantModel.subject_type,
                    GuildCapabilityGrantModel.subject_id,
                ]
            )
            .returning(GuildCapabilityGrantModel)
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return None if model is None else self._grant_record(model)

    async def remove_grant(
        self,
        *,
        guild_id: int,
        capability: CapabilityKey,
        subject_type: CapabilitySubjectType,
        subject_id: int,
    ) -> bool:
        self._validate_id(guild_id, "guild_id")
        self._validate_id(subject_id, "subject_id")
        statement = (
            delete(GuildCapabilityGrantModel)
            .where(
                GuildCapabilityGrantModel.guild_id == guild_id,
                GuildCapabilityGrantModel.capability_key == capability,
                GuildCapabilityGrantModel.subject_type == subject_type.value,
                GuildCapabilityGrantModel.subject_id == subject_id,
            )
            .returning(GuildCapabilityGrantModel.subject_id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    @staticmethod
    def _validate_id(value: int, field_name: str) -> None:
        if value <= 0:
            raise ValueError(f"{field_name} must be positive")

    @staticmethod
    def _validate_capability(capability: CapabilityKey) -> None:
        if not capability or not capability.strip():
            raise ValueError("capability must not be empty")

    @staticmethod
    def _utc(value: datetime, field_name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field_name} must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _policy_record(model: GuildCapabilityPolicyModel) -> GuildCapabilityPolicy:
        return GuildCapabilityPolicy(
            guild_id=model.guild_id,
            capability=CapabilityKey(model.capability_key),
            enabled=model.enabled,
            updated_at=model.updated_at,
            updated_by_user_id=model.updated_by_user_id,
        )

    @staticmethod
    def _grant_record(model: GuildCapabilityGrantModel) -> CapabilityGrant:
        return CapabilityGrant(
            guild_id=model.guild_id,
            capability=CapabilityKey(model.capability_key),
            subject_type=CapabilitySubjectType(model.subject_type),
            subject_id=model.subject_id,
            created_at=model.created_at,
            created_by_user_id=model.created_by_user_id,
        )
