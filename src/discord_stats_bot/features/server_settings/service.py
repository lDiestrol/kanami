"""Resolution and mutation orchestration for guild server settings."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol

from discord_stats_bot.features.audit_logging import (
    AuditEventDraft,
    AuditEventRecord,
    AuditLoggingService,
)
from discord_stats_bot.features.server_settings.types import (
    EffectiveGuildServerSettings,
    GuildServerSettingChange,
    GuildServerSettingKey,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
    GuildServerSettingsBaselines,
    GuildServerSettingSource,
    GuildServerSettingsOverrides,
)

SERVER_SETTING_CHANGED_EVENT_TYPE = "web_admin.server_setting_changed"
SERVER_SETTINGS_AUDIT_CATEGORY = "web_admin"


class GuildServerSettingsRepository(Protocol):
    async def lock_guild(self, guild_id: int) -> None: ...

    async def get_overrides(
        self, guild_id: int, *, for_update: bool = False
    ) -> GuildServerSettingsOverrides | None: ...

    async def save_override(
        self,
        *,
        guild_id: int,
        key: GuildServerSettingKey,
        override: GuildServerSettingOverride,
        updated_at: datetime,
        updated_by_user_id: int,
    ) -> None: ...


class ServerSettingsAuditRepository(Protocol):
    async def create(
        self,
        draft: AuditEventDraft,
        *,
        expires_at: datetime | None,
    ) -> AuditEventRecord: ...

    async def create_many(
        self,
        events: Sequence[tuple[AuditEventDraft, datetime | None]],
    ) -> tuple[AuditEventRecord, ...]: ...

    async def mark_delivery_suppressed(
        self, event_ids: Sequence[int], suppressed_at: datetime
    ) -> None: ...


def empty_overrides(guild_id: int) -> GuildServerSettingsOverrides:
    return GuildServerSettingsOverrides(guild_id=guild_id)


def resolve_guild_server_settings(
    guild_id: int,
    baselines: GuildServerSettingsBaselines,
    overrides: GuildServerSettingsOverrides | None,
) -> EffectiveGuildServerSettings:
    configured = overrides or empty_overrides(guild_id)
    values: dict[str, object] = {"guild_id": guild_id}
    for key in GuildServerSettingKey:
        override = configured.override_for(key)
        if override.mode is GuildServerSettingOverrideMode.ENV:
            value = baselines.value_for(key)
            source = GuildServerSettingSource.ENV
        elif override.mode is GuildServerSettingOverrideMode.DISABLED:
            value = None
            source = GuildServerSettingSource.DISABLED
        else:
            value = override.value
            source = GuildServerSettingSource.DB
        values[f"{key.value}_id"] = value
        values[f"{key.value}_source"] = source
    return EffectiveGuildServerSettings(**values)  # type: ignore[arg-type]


class GuildServerSettingsMutationService:
    """Change one allowlisted override and append one history-only audit event."""

    def __init__(
        self,
        repository: GuildServerSettingsRepository,
        audit_repository: ServerSettingsAuditRepository,
        *,
        baselines: GuildServerSettingsBaselines,
    ) -> None:
        self._repository = repository
        self._audit_repository = audit_repository
        self._audit_service = AuditLoggingService(audit_repository)
        self._baselines = baselines

    async def change(
        self,
        *,
        guild_id: int,
        key: GuildServerSettingKey,
        override: GuildServerSettingOverride,
        actor_user_id: int,
        occurred_at: datetime,
    ) -> GuildServerSettingChange:
        if guild_id <= 0 or actor_user_id <= 0:
            raise ValueError("guild_id and actor_user_id must be positive")
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        occurred_at = occurred_at.astimezone(UTC)
        await self._repository.lock_guild(guild_id)
        current = await self._repository.get_overrides(guild_id, for_update=True)
        configured = current or empty_overrides(guild_id)
        previous_override = configured.override_for(key)
        if previous_override == override:
            return GuildServerSettingChange(
                False,
                key,
                override,
                resolve_guild_server_settings(guild_id, self._baselines, configured),
            )
        await self._repository.save_override(
            guild_id=guild_id,
            key=key,
            override=override,
            updated_at=occurred_at,
            updated_by_user_id=actor_user_id,
        )
        changed = configured.with_override(
            key,
            override,
            updated_at=occurred_at,
            updated_by_user_id=actor_user_id,
        )
        audit_record = await self._audit_service.create(
            AuditEventDraft(
                guild_id=guild_id,
                category=SERVER_SETTINGS_AUDIT_CATEGORY,
                event_type=SERVER_SETTING_CHANGED_EVENT_TYPE,
                occurred_at=occurred_at,
                subject_type="guild_setting",
                actor_user_id=actor_user_id,
                before_data=_audit_value(
                    previous_override, self._baselines.value_for(key)
                ),
                after_data=_audit_value(override, self._baselines.value_for(key)),
                details_data={"setting_key": key.value},
            )
        )
        await self._audit_repository.mark_delivery_suppressed(
            (audit_record.id,), occurred_at
        )
        return GuildServerSettingChange(
            True,
            key,
            override,
            resolve_guild_server_settings(guild_id, self._baselines, changed),
        )


def _audit_value(
    override: GuildServerSettingOverride, baseline_value: int | None
) -> dict[str, object]:
    value = (
        baseline_value
        if override.mode is GuildServerSettingOverrideMode.ENV
        else override.value
    )
    return {"source": override.mode.value, "value": value}
