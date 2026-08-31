"""Caller-owned SQLAlchemy repository for guild server setting overrides."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.server_settings import (
    GuildServerSettingKey,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
    GuildServerSettingsOverrides,
)
from discord_stats_bot.persistence.models import GuildServerSettings


class SqlAlchemyGuildServerSettingsRepository:
    """Read/write allowlisted columns without owning the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_guild(self, guild_id: int) -> None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        await self._session.execute(select(func.pg_advisory_xact_lock(guild_id)))

    async def get_overrides(
        self, guild_id: int, *, for_update: bool = False
    ) -> GuildServerSettingsOverrides | None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        statement = select(GuildServerSettings).where(
            GuildServerSettings.guild_id == guild_id
        )
        if for_update:
            statement = statement.with_for_update()
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return None if model is None else self._to_record(model)

    async def save_override(
        self,
        *,
        guild_id: int,
        key: GuildServerSettingKey,
        override: GuildServerSettingOverride,
        updated_at: datetime,
        updated_by_user_id: int,
    ) -> None:
        mode_field = f"{key.value}_mode"
        value_field = f"{key.value}_id"
        values: dict[str, object] = {
            "guild_id": guild_id,
            mode_field: override.mode.value,
            value_field: override.value,
            "updated_at": updated_at,
            "updated_by_user_id": updated_by_user_id,
        }
        statement = insert(GuildServerSettings).values(**values)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[GuildServerSettings.guild_id],
                set_={
                    mode_field: statement.excluded[mode_field],
                    value_field: statement.excluded[value_field],
                    "updated_at": statement.excluded.updated_at,
                    "updated_by_user_id": statement.excluded.updated_by_user_id,
                },
            )
        )

    @staticmethod
    def _to_record(model: GuildServerSettings) -> GuildServerSettingsOverrides:
        return GuildServerSettingsOverrides(
            guild_id=model.guild_id,
            autorole_role=GuildServerSettingOverride(
                GuildServerSettingOverrideMode(model.autorole_role_mode),
                model.autorole_role_id,
            ),
            audit_log_channel=GuildServerSettingOverride(
                GuildServerSettingOverrideMode(model.audit_log_channel_mode),
                model.audit_log_channel_id,
            ),
            anniversary_channel=GuildServerSettingOverride(
                GuildServerSettingOverrideMode(model.anniversary_channel_mode),
                model.anniversary_channel_id,
            ),
            return_channel=GuildServerSettingOverride(
                GuildServerSettingOverrideMode(model.return_channel_mode),
                model.return_channel_id,
            ),
            updated_at=model.updated_at,
            updated_by_user_id=model.updated_by_user_id,
        )
