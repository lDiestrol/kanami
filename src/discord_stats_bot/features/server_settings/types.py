"""Discord-independent types for guild server setting overrides."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum


class GuildServerSettingKey(StrEnum):
    AUTOROLE_ROLE = "autorole_role"
    AUDIT_LOG_CHANNEL = "audit_log_channel"
    ANNIVERSARY_CHANNEL = "anniversary_channel"
    RETURN_CHANNEL = "return_channel"


class GuildServerSettingOverrideMode(StrEnum):
    ENV = "env"
    VALUE = "value"
    DISABLED = "disabled"


class GuildServerSettingSource(StrEnum):
    ENV = "env"
    DB = "db"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class GuildServerSettingOverride:
    mode: GuildServerSettingOverrideMode
    value: int | None = None

    def __post_init__(self) -> None:
        has_value = self.value is not None
        if (self.mode is GuildServerSettingOverrideMode.VALUE) != has_value:
            raise ValueError("value mode must have exactly one positive value")
        if self.value is not None and self.value <= 0:
            raise ValueError("setting value must be positive")


ENV_OVERRIDE = GuildServerSettingOverride(GuildServerSettingOverrideMode.ENV)
DISABLED_OVERRIDE = GuildServerSettingOverride(GuildServerSettingOverrideMode.DISABLED)


@dataclass(frozen=True, slots=True)
class GuildServerSettingsBaselines:
    autorole_role_id: int | None = None
    audit_log_channel_id: int | None = None
    anniversary_channel_id: int | None = None
    return_channel_id: int | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "autorole_role_id",
            "audit_log_channel_id",
            "anniversary_channel_id",
            "return_channel_id",
        ):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be positive when provided")

    def value_for(self, key: GuildServerSettingKey) -> int | None:
        return getattr(self, _ID_FIELDS[key])


@dataclass(frozen=True, slots=True)
class GuildServerSettingsOverrides:
    guild_id: int
    autorole_role: GuildServerSettingOverride = ENV_OVERRIDE
    audit_log_channel: GuildServerSettingOverride = ENV_OVERRIDE
    anniversary_channel: GuildServerSettingOverride = ENV_OVERRIDE
    return_channel: GuildServerSettingOverride = ENV_OVERRIDE
    updated_at: datetime | None = None
    updated_by_user_id: int | None = None

    def __post_init__(self) -> None:
        if self.guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if self.updated_by_user_id is not None and self.updated_by_user_id <= 0:
            raise ValueError("updated_by_user_id must be positive when provided")

    def override_for(self, key: GuildServerSettingKey) -> GuildServerSettingOverride:
        return getattr(self, key.value)

    def with_override(
        self,
        key: GuildServerSettingKey,
        override: GuildServerSettingOverride,
        *,
        updated_at: datetime,
        updated_by_user_id: int,
    ) -> "GuildServerSettingsOverrides":
        return replace(
            self,
            **{
                key.value: override,
                "updated_at": updated_at,
                "updated_by_user_id": updated_by_user_id,
            },
        )


@dataclass(frozen=True, slots=True)
class EffectiveGuildServerSettings:
    guild_id: int
    autorole_role_id: int | None
    audit_log_channel_id: int | None
    anniversary_channel_id: int | None
    return_channel_id: int | None
    autorole_role_source: GuildServerSettingSource
    audit_log_channel_source: GuildServerSettingSource
    anniversary_channel_source: GuildServerSettingSource
    return_channel_source: GuildServerSettingSource

    def value_for(self, key: GuildServerSettingKey) -> int | None:
        return getattr(self, _ID_FIELDS[key])

    def source_for(self, key: GuildServerSettingKey) -> GuildServerSettingSource:
        return getattr(self, _SOURCE_FIELDS[key])


@dataclass(frozen=True, slots=True)
class GuildServerSettingChange:
    changed: bool
    key: GuildServerSettingKey
    override: GuildServerSettingOverride
    effective: EffectiveGuildServerSettings


_ID_FIELDS = {
    GuildServerSettingKey.AUTOROLE_ROLE: "autorole_role_id",
    GuildServerSettingKey.AUDIT_LOG_CHANNEL: "audit_log_channel_id",
    GuildServerSettingKey.ANNIVERSARY_CHANNEL: "anniversary_channel_id",
    GuildServerSettingKey.RETURN_CHANNEL: "return_channel_id",
}
_SOURCE_FIELDS = {key: f"{key.value}_source" for key in GuildServerSettingKey}
