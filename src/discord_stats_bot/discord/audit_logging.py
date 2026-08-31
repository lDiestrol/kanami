"""Discord adapters for audit ingestion, presentation, delivery, and retention."""

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.common.formatting import format_voice_duration
from discord_stats_bot.discord.member_anniversaries import (
    build_member_anniversary_notification_embed,
)
from discord_stats_bot.discord.member_returns import build_member_return_embed
from discord_stats_bot.discord.server_settings import GuildServerSettingsProvider
from discord_stats_bot.features.audit_logging import (
    SUPPORTED_EVENT_TYPES,
    AuditEventDraft,
    AuditEventRecord,
    AuditLoggingService,
    VoiceAuditEnrichmentService,
)
from discord_stats_bot.features.voice import VoiceTransitionResult
from discord_stats_bot.persistence.repositories import SqlAlchemyAuditEventRepository
from discord_stats_bot.persistence.repositories.voice_audit import (
    SqlAlchemyVoiceAuditEnrichmentRepository,
)

logger = logging.getLogger(__name__)

AUDIT_DELIVERY_POLL_SECONDS = 5.0
AUDIT_DELIVERY_BATCH_SIZE = 25
AUDIT_CHANNEL_POSITION_BATCH_WINDOW_SECONDS = 1.5
AUDIT_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
AUDIT_RETRY_DELAYS_SECONDS = (5, 15, 30, 60, 120, 300)
KANAMI_EMBED_COLOR = 0x5865F2
SPECIALIZED_DELIVERY_EVENT_TYPES = frozenset({"member.anniversary", "member.returned"})
RETURN_HISTORY_EVENT_TYPES = frozenset({"member.joined", "member.left"})


class AuditRepository(Protocol):
    async def get_pending_delivery(
        self,
        *,
        guild_id: int,
        as_of: datetime,
        limit: int,
        event_types: Sequence[str] | None = None,
    ) -> tuple[AuditEventRecord, ...]: ...

    async def mark_delivered(
        self, event_id: int, discord_message_id: int, delivered_at: datetime
    ) -> None: ...

    async def mark_delivered_many(
        self,
        event_ids: Sequence[int],
        discord_message_id: int,
        delivered_at: datetime,
    ) -> None: ...

    async def mark_delivery_failed(
        self, event_id: int, error: str, next_attempt_at: datetime
    ) -> None: ...

    async def mark_delivery_failed_many(
        self,
        event_ids: Sequence[int],
        error: str,
        next_attempt_at: datetime,
    ) -> None: ...

    async def delete_expired(self, *, as_of: datetime) -> int: ...


AuditRepositoryFactory = Callable[[AsyncSession], AuditRepository]
VoiceAuditRepositoryFactory = Callable[[AsyncSession], Any]


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_text(value: object | None, *, fallback: str = "—", limit: int = 1000) -> str:
    if value is None or value == "":
        return fallback
    return discord.utils.escape_markdown(str(value), as_needed=True)[:limit]


def _change_text(value: object | None, *, limit: int = 220) -> str:
    return _safe_text(value, fallback="Не задано", limit=limit)


def _iso(value: object | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def _asset(asset: object | None) -> dict[str, str] | None:
    if asset is None:
        return None
    result = {"url": str(asset)}
    key = getattr(asset, "key", None)
    if key:
        result["key"] = str(key)
    return result


def _asset_url(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if not isinstance(value, Mapping):
        return None
    url = value.get("url")
    return str(url) if url else None


def _compact_lines(values: Sequence[str], *, empty: str = "—") -> str:
    if not values:
        return empty
    rendered = ""
    for value in values:
        if not rendered and len(value) > 1024:
            return f"{value[:1023]}…"
        candidate = value if not rendered else f"{rendered}\n{value}"
        if len(candidate) > 1024:
            return f"{rendered[:1019]}\n…"
        rendered = candidate
    return rendered


def _channel_snapshot(channel: object) -> dict[str, Any]:
    category = getattr(channel, "category", None)
    overwrites: list[dict[str, Any]] = []
    for target, overwrite in getattr(channel, "overwrites", {}).items():
        allow, deny = overwrite.pair()
        overwrites.append(
            {
                "target_id": int(target.id),
                "target_type": "role" if isinstance(target, discord.Role) else "member",
                "allow": allow.value,
                "deny": deny.value,
            }
        )
    overwrites.sort(key=lambda item: (item["target_type"], item["target_id"]))
    snapshot: dict[str, Any] = {
        "name": getattr(channel, "name", None),
        "channel_type": str(getattr(channel, "type", "unknown")),
        "category_id": getattr(category, "id", None),
        "category_name": getattr(category, "name", None),
        "position": getattr(channel, "position", None),
        "overwrites": overwrites,
    }
    optional_fields = {
        "topic": "topic",
        "slowmode_delay": "slowmode_delay",
        "nsfw": "nsfw",
        "bitrate": "bitrate",
        "user_limit": "user_limit",
    }
    for output_name, attribute in optional_fields.items():
        if hasattr(channel, attribute):
            snapshot[output_name] = getattr(channel, attribute)
    return snapshot


def _role_snapshot(role: discord.Role) -> dict[str, Any]:
    return {
        "name": role.name,
        "color": role.color.value,
        "permissions": role.permissions.value,
        "mentionable": role.mentionable,
        "hoist": role.hoist,
        "position": role.position,
    }


def _non_bot_channel_member_count(channel: object | None) -> int | None:
    members = getattr(channel, "members", None)
    if members is None:
        return None
    return sum(not getattr(member, "bot", False) for member in members)


def _changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[dict, dict]:
    keys = before.keys() | after.keys()
    changed_keys = sorted(key for key in keys if before.get(key) != after.get(key))
    return (
        {key: before.get(key) for key in changed_keys},
        {key: after.get(key) for key in changed_keys},
    )


class AuditEventIngestor:
    """Normalize Discord events and commit them before waking delivery."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        transient_retention_days: int = 90,
        wake_delivery: Callable[[], None] | None = None,
        repository_factory: Callable[[AsyncSession], Any] = (
            SqlAlchemyAuditEventRepository
        ),
        voice_audit_repository_factory: VoiceAuditRepositoryFactory = (
            SqlAlchemyVoiceAuditEnrichmentRepository
        ),
        report_timezone: ZoneInfo = ZoneInfo("UTC"),
        min_session_seconds: int = 60,
        enabled_event_types: Sequence[str] | None = None,
        suppress_delivery: bool = False,
        settings_provider: GuildServerSettingsProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._transient_retention_days = transient_retention_days
        self._wake_delivery = wake_delivery
        self._repository_factory = repository_factory
        self._voice_audit_repository_factory = voice_audit_repository_factory
        self._report_timezone = report_timezone
        self._min_session_seconds = min_session_seconds
        self._enabled_event_types = (
            None if enabled_event_types is None else frozenset(enabled_event_types)
        )
        self._suppress_delivery = suppress_delivery
        self._settings_provider = settings_provider
        if self._suppress_delivery and self._enabled_event_types is None:
            raise ValueError("suppress_delivery requires explicit enabled_event_types")
        if self._enabled_event_types is not None:
            unsupported = self._enabled_event_types - set(SUPPORTED_EVENT_TYPES)
            if unsupported:
                raise ValueError(
                    f"unsupported enabled event types: {sorted(unsupported)}"
                )

    async def _persist(self, drafts: Sequence[AuditEventDraft]) -> int:
        suppress_delivery = self._suppress_delivery
        if self._settings_provider is not None:
            settings = await self._settings_provider.get()
            if settings.audit_log_channel_id is not None:
                enabled_event_types: frozenset[str] | None = None
                suppress_delivery = False
            elif settings.return_channel_id is not None:
                enabled_event_types = RETURN_HISTORY_EVENT_TYPES
                suppress_delivery = True
            else:
                return 0
        else:
            enabled_event_types = self._enabled_event_types
        if enabled_event_types is not None:
            drafts = tuple(
                draft for draft in drafts if draft.event_type in enabled_event_types
            )
        if not drafts:
            return 0
        async with self._session_factory.begin() as session:
            repository = self._repository_factory(session)
            service = AuditLoggingService(
                repository,
                transient_retention_days=self._transient_retention_days,
            )
            records = await service.create_many(drafts)
            if suppress_delivery:
                await repository.mark_delivery_suppressed(
                    tuple(record.id for record in records),
                    max(draft.occurred_at for draft in drafts),
                )
        # The transaction context has committed before delivery is awakened.
        if not suppress_delivery and self._wake_delivery is not None:
            self._wake_delivery()
        return len(drafts)

    def _accept_guild(self, guild_id: int) -> bool:
        return guild_id == self._guild_id

    async def member_joined(self, member: discord.Member, occurred_at: datetime) -> int:
        if not self._accept_guild(member.guild.id):
            return 0
        return await self._persist(
            (
                AuditEventDraft(
                    guild_id=member.guild.id,
                    category="member",
                    event_type="member.joined",
                    occurred_at=occurred_at,
                    subject_type="user",
                    subject_id=member.id,
                    after_data={"joined_at": _iso(member.joined_at)},
                    details_data={"display_name": member.display_name},
                ),
            )
        )

    async def member_left(self, member: discord.Member, occurred_at: datetime) -> int:
        if not self._accept_guild(member.guild.id):
            return 0
        return await self._persist(
            (
                AuditEventDraft(
                    guild_id=member.guild.id,
                    category="member",
                    event_type="member.left",
                    occurred_at=occurred_at,
                    subject_type="user",
                    subject_id=member.id,
                    before_data={"joined_at": _iso(member.joined_at)},
                    details_data={"display_name": member.display_name},
                ),
            )
        )

    async def user_updated(
        self,
        guild: discord.Guild,
        member: discord.Member,
        before: discord.User,
        after: discord.User,
        occurred_at: datetime,
    ) -> int:
        if not self._accept_guild(guild.id) or member.id != after.id:
            return 0
        drafts: list[AuditEventDraft] = []
        common = {
            "guild_id": guild.id,
            "category": "member",
            "occurred_at": occurred_at,
            "subject_type": "user",
            "subject_id": after.id,
            "details_data": {"display_name": member.display_name},
        }
        if before.name != after.name:
            drafts.append(
                AuditEventDraft(
                    event_type="user.username_updated",
                    before_data={"username": before.name},
                    after_data={"username": after.name},
                    **common,
                )
            )
        if getattr(before, "avatar", None) != getattr(after, "avatar", None):
            drafts.append(
                AuditEventDraft(
                    event_type="user.avatar_updated",
                    before_data={"avatar": _asset(getattr(before, "avatar", None))},
                    after_data={"avatar": _asset(getattr(after, "avatar", None))},
                    **common,
                )
            )
        return await self._persist(drafts)

    async def member_updated(
        self,
        before: discord.Member,
        after: discord.Member,
        occurred_at: datetime,
    ) -> int:
        if not self._accept_guild(after.guild.id):
            return 0
        common = {
            "guild_id": after.guild.id,
            "category": "member",
            "occurred_at": occurred_at,
            "subject_type": "user",
            "subject_id": after.id,
            "details_data": {"display_name": after.display_name},
        }
        drafts: list[AuditEventDraft] = []
        if before.nick != after.nick:
            drafts.append(
                AuditEventDraft(
                    event_type="member.nickname_updated",
                    before_data={"nickname": before.nick},
                    after_data={"nickname": after.nick},
                    **common,
                )
            )
        if getattr(before, "guild_avatar", None) != getattr(
            after, "guild_avatar", None
        ):
            drafts.append(
                AuditEventDraft(
                    event_type="member.guild_avatar_updated",
                    before_data={
                        "guild_avatar": _asset(getattr(before, "guild_avatar", None))
                    },
                    after_data={
                        "guild_avatar": _asset(getattr(after, "guild_avatar", None))
                    },
                    **common,
                )
            )
        before_role_ids = sorted(role.id for role in before.roles)
        after_role_ids = sorted(role.id for role in after.roles)
        if before_role_ids != after_role_ids:
            before_set = set(before_role_ids)
            after_set = set(after_role_ids)
            role_details = {
                "display_name": after.display_name,
                "added_role_ids": sorted(after_set - before_set),
                "removed_role_ids": sorted(before_set - after_set),
            }
            drafts.append(
                AuditEventDraft(
                    event_type="member.roles_updated",
                    before_data={"role_ids": before_role_ids},
                    after_data={"role_ids": after_role_ids},
                    details_data=role_details,
                    **{
                        key: value
                        for key, value in common.items()
                        if key != "details_data"
                    },
                )
            )
        before_timeout = _iso(getattr(before, "timed_out_until", None))
        after_timeout = _iso(getattr(after, "timed_out_until", None))
        if before_timeout != after_timeout:
            drafts.append(
                AuditEventDraft(
                    event_type="member.timeout_updated",
                    before_data={"timed_out_until": before_timeout},
                    after_data={"timed_out_until": after_timeout},
                    **common,
                )
            )
        return await self._persist(drafts)

    async def voice_changed(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
        occurred_at: datetime,
        *,
        transition_result: VoiceTransitionResult | None = None,
    ) -> int:
        if not self._accept_guild(member.guild.id) or member.bot:
            return 0
        before_channel = before.channel
        after_channel = after.channel
        if before_channel == after_channel:
            return 0
        if before_channel is None:
            event_type = "voice.joined"
        elif after_channel is None:
            event_type = "voice.left"
        else:
            event_type = "voice.moved"
        before_data = (
            {}
            if before_channel is None
            else {"channel_id": before_channel.id, "channel_name": before_channel.name}
        )
        after_data = (
            {}
            if after_channel is None
            else {"channel_id": after_channel.id, "channel_name": after_channel.name}
        )
        details: dict[str, object] = {"display_name": member.display_name}
        if (
            event_type == "voice.joined"
            and transition_result is VoiceTransitionResult.JOINED
        ):
            member_count = _non_bot_channel_member_count(after_channel)
            if member_count is not None:
                details["channel_member_count"] = member_count
        expected_result = {
            "voice.left": VoiceTransitionResult.LEFT,
            "voice.moved": VoiceTransitionResult.MOVED,
        }.get(event_type)
        if transition_result is expected_result and before_channel is not None:
            try:
                async with self._session_factory() as session:
                    enrichment = VoiceAuditEnrichmentService(
                        self._voice_audit_repository_factory(session),
                        report_timezone=self._report_timezone,
                        min_session_seconds=self._min_session_seconds,
                    )
                    details.update(
                        await enrichment.get_details(
                            event_type=event_type,
                            guild_id=member.guild.id,
                            user_id=member.id,
                            previous_channel_id=before_channel.id,
                            occurred_at=occurred_at,
                        )
                    )
            except Exception:
                logger.exception(
                    "Voice audit enrichment failed guild_id=%s user_id=%s "
                    "event_type=%s",
                    member.guild.id,
                    member.id,
                    event_type,
                )
        return await self._persist(
            (
                AuditEventDraft(
                    guild_id=member.guild.id,
                    category="voice",
                    event_type=event_type,
                    occurred_at=occurred_at,
                    subject_type="user",
                    subject_id=member.id,
                    channel_id=(
                        after_channel.id
                        if after_channel is not None
                        else before_channel.id
                    ),
                    before_data=before_data,
                    after_data=after_data,
                    details_data=details,
                ),
            )
        )

    async def channel_created(self, channel: Any, occurred_at: datetime) -> int:
        if not self._accept_guild(channel.guild.id):
            return 0
        return await self._channel_event(
            "channel.created", channel, {}, _channel_snapshot(channel), occurred_at
        )

    async def channel_deleted(self, channel: Any, occurred_at: datetime) -> int:
        if not self._accept_guild(channel.guild.id):
            return 0
        return await self._channel_event(
            "channel.deleted", channel, _channel_snapshot(channel), {}, occurred_at
        )

    async def channel_updated(
        self, before: Any, after: Any, occurred_at: datetime
    ) -> int:
        if not self._accept_guild(after.guild.id):
            return 0
        before_diff, after_diff = _changed(
            _channel_snapshot(before), _channel_snapshot(after)
        )
        if not before_diff:
            return 0
        return await self._channel_event(
            "channel.updated", after, before_diff, after_diff, occurred_at
        )

    async def _channel_event(
        self,
        event_type: str,
        channel: Any,
        before_data: Mapping[str, Any],
        after_data: Mapping[str, Any],
        occurred_at: datetime,
    ) -> int:
        snapshot = _channel_snapshot(channel)
        return await self._persist(
            (
                AuditEventDraft(
                    guild_id=channel.guild.id,
                    category="server",
                    event_type=event_type,
                    occurred_at=occurred_at,
                    subject_type="channel",
                    subject_id=channel.id,
                    channel_id=channel.id,
                    before_data=before_data,
                    after_data=after_data,
                    details_data={
                        "channel_name": snapshot.get("name"),
                        "channel_type": snapshot.get("channel_type"),
                        "category_id": snapshot.get("category_id"),
                        "category_name": snapshot.get("category_name"),
                    },
                ),
            )
        )

    async def role_created(self, role: discord.Role, occurred_at: datetime) -> int:
        return await self._role_event(
            "role.created", role, {}, _role_snapshot(role), occurred_at
        )

    async def role_deleted(self, role: discord.Role, occurred_at: datetime) -> int:
        return await self._role_event(
            "role.deleted", role, _role_snapshot(role), {}, occurred_at
        )

    async def role_updated(
        self, before: discord.Role, after: discord.Role, occurred_at: datetime
    ) -> int:
        if not self._accept_guild(after.guild.id):
            return 0
        before_diff, after_diff = _changed(
            _role_snapshot(before), _role_snapshot(after)
        )
        if not before_diff:
            return 0
        details: dict[str, Any] = {}
        if "permissions" in before_diff:
            before_names = {name for name, enabled in before.permissions if enabled}
            after_names = {name for name, enabled in after.permissions if enabled}
            details = {
                "added_permissions": sorted(after_names - before_names),
                "removed_permissions": sorted(before_names - after_names),
            }
        return await self._role_event(
            "role.updated", after, before_diff, after_diff, occurred_at, details
        )

    async def _role_event(
        self,
        event_type: str,
        role: discord.Role,
        before_data: Mapping[str, Any],
        after_data: Mapping[str, Any],
        occurred_at: datetime,
        details_data: Mapping[str, Any] | None = None,
    ) -> int:
        if not self._accept_guild(role.guild.id):
            return 0
        return await self._persist(
            (
                AuditEventDraft(
                    guild_id=role.guild.id,
                    category="server",
                    event_type=event_type,
                    occurred_at=occurred_at,
                    subject_type="role",
                    subject_id=role.id,
                    before_data=before_data,
                    after_data=after_data,
                    details_data=details_data or {},
                ),
            )
        )

    async def moderation_changed(
        self,
        event_type: str,
        guild: discord.Guild,
        user: discord.User,
        occurred_at: datetime,
    ) -> int:
        if not self._accept_guild(guild.id):
            return 0
        return await self._persist(
            (
                AuditEventDraft(
                    guild_id=guild.id,
                    category="moderation",
                    event_type=event_type,
                    occurred_at=occurred_at,
                    subject_type="user",
                    subject_id=user.id,
                    details_data={"display_name": user.display_name},
                ),
            )
        )


_TITLES = {
    "rules.draft_created": "Создан черновик правил",
    "rules.draft_updated": "Изменён черновик правил",
    "rules.draft_deleted": "Удалён черновик правил",
    "rules.published": "Опубликованы правила",
    "member.anniversary": "Годовщина на сервере!",
    "member.returned": "С возвращением!",
    "member.joined": "Участник присоединился",
    "member.left": "Участник покинул сервер",
    "user.username_updated": "Имя пользователя изменено",
    "user.avatar_updated": "Аватар изменён",
    "member.nickname_updated": "Никнейм изменён",
    "member.guild_avatar_updated": "Аватар участника на сервере изменён",
    "member.roles_updated": "Роли участника изменены",
    "member.timeout_updated": "Тайм-аут участника изменён",
    "voice.joined": "Участник вошёл в голосовой канал",
    "voice.left": "Участник вышел из голосового канала",
    "voice.moved": "Участник сменил голосовой канал",
    "channel.created": "Канал создан",
    "channel.deleted": "Канал удалён",
    "channel.updated": "Канал изменён",
    "role.created": "Роль создана",
    "role.deleted": "Роль удалена",
    "role.updated": "Роль изменена",
    "moderation.banned": "Участник заблокирован",
    "moderation.unbanned": "Участник разблокирован",
}


def _add_change_fields(embed: discord.Embed, record: AuditEventRecord) -> None:
    labels = {
        "username": "Имя",
        "nickname": "Никнейм",
        "name": "Название",
        "topic": "Тема",
        "slowmode_delay": "Медленный режим",
        "nsfw": "NSFW",
        "bitrate": "Битрейт",
        "user_limit": "Лимит участников",
        "color": "Цвет",
        "mentionable": "Можно упоминать",
        "hoist": "Показывать отдельно",
    }
    changed_keys = record.before_data.keys() | record.after_data.keys()
    for key in sorted(changed_keys):
        if key not in labels:
            continue
        embed.add_field(
            name=labels[key],
            value=(
                f"Было: **{_change_text(record.before_data.get(key))}**\n"
                f"Стало: **{_change_text(record.after_data.get(key))}**"
            ),
            inline=False,
        )
    if "category_id" in changed_keys or "category_name" in changed_keys:
        before_category = _category_display(record.before_data)
        after_category = _category_display(record.after_data)
        embed.add_field(
            name="Категория",
            value=f"{before_category} → {after_category}",
            inline=False,
        )
    if "position" in changed_keys:
        before_position = record.before_data.get("position")
        after_position = record.after_data.get("position")
        if before_position is not None and after_position is not None:
            embed.add_field(
                name="Позиция",
                value=f"**{before_position} → {after_position}**",
                inline=False,
            )
    if "overwrites" in record.before_data or "overwrites" in record.after_data:
        before_count = len(record.before_data.get("overwrites") or [])
        after_count = len(record.after_data.get("overwrites") or [])
        embed.add_field(
            name="Права доступа",
            value=f"Изменены правила: **{before_count} → {after_count}**",
            inline=False,
        )


def _category_display(data: Mapping[str, Any]) -> str:
    category_id = data.get("category_id")
    if category_id:
        return f"<#{category_id}>"
    category_name = data.get("category_name")
    if category_name:
        return f"**{_safe_text(category_name)}**"
    return "Без категории"


def _channel_display(record: AuditEventRecord) -> str:
    channel_id = record.channel_id or record.subject_id
    if channel_id:
        return f"<#{channel_id}>"
    data = record.after_data or record.before_data or record.details_data
    name = data.get("name") or data.get("channel_name")
    return f"**{_safe_text(name, fallback='Канал')}**"


def _voice_channel_display(data: Mapping[str, Any]) -> str:
    channel_id = data.get("channel_id")
    if channel_id:
        return f"<#{channel_id}>"
    return f"**{_safe_text(data.get('channel_name'), fallback='Голосовой канал')}**"


def _positive_detail_seconds(record: AuditEventRecord, key: str) -> int | None:
    value = record.details_data.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _detail_datetime(record: AuditEventRecord, key: str) -> datetime | None:
    value = record.details_data.get(key)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _add_voice_fields(
    embed: discord.Embed,
    record: AuditEventRecord,
    report_timezone: ZoneInfo,
) -> None:
    user = f"<@{record.subject_id}>" if record.subject_id else "Участник"
    before_channel = _voice_channel_display(record.before_data)
    after_channel = _voice_channel_display(record.after_data)
    if record.event_type == "voice.joined":
        embed.description = f"{user} → {after_channel}"
        member_count = record.details_data.get("channel_member_count")
        if isinstance(member_count, int) and not isinstance(member_count, bool):
            embed.add_field(
                name="В канале сейчас",
                value=f"**{member_count} человек**",
                inline=False,
            )
        return
    if record.event_type == "voice.left":
        embed.description = f"{user} ← {before_channel}"
        previous_seconds = _positive_detail_seconds(record, "previous_interval_seconds")
        if previous_seconds is not None:
            embed.add_field(
                name="В канале",
                value=f"**{format_voice_duration(previous_seconds)}**",
                inline=True,
            )
        today_seconds = _positive_detail_seconds(record, "today_total_seconds")
        if today_seconds is not None:
            embed.add_field(
                name="Сегодня всего",
                value=f"**{format_voice_duration(today_seconds)}**",
                inline=True,
            )
    else:
        embed.description = f"{user}\n{before_channel} → {after_channel}"
        previous_seconds = _positive_detail_seconds(record, "previous_interval_seconds")
        if previous_seconds is not None:
            embed.add_field(
                name="В предыдущем канале",
                value=f"**{format_voice_duration(previous_seconds)}**",
                inline=True,
            )
        session_seconds = _positive_detail_seconds(record, "current_session_seconds")
        if session_seconds is not None:
            embed.add_field(
                name="Текущая сессия",
                value=f"**{format_voice_duration(session_seconds)}**",
                inline=True,
            )
    joined_at = _detail_datetime(record, "session_started_at")
    if joined_at is not None:
        embed.add_field(
            name="Вошёл",
            value=joined_at.astimezone(report_timezone).strftime("%H:%M"),
            inline=False,
        )


def _channel_update_keys(record: AuditEventRecord) -> set[str]:
    return set(record.before_data) | set(record.after_data)


def _is_pure_channel_position_update(record: AuditEventRecord) -> bool:
    return record.event_type == "channel.updated" and _channel_update_keys(record) == {
        "position"
    }


def _channel_update_title(record: AuditEventRecord) -> str:
    keys = _channel_update_keys(record)
    if keys == {"position"}:
        return "Канал перемещён"
    if keys == {"name"}:
        return "Канал переименован"
    if keys == {"topic"}:
        return "Изменена тема канала"
    return "Канал изменён"


def _add_channel_update_fields(embed: discord.Embed, record: AuditEventRecord) -> None:
    keys = _channel_update_keys(record)
    if keys == {"name"}:
        before_name = _safe_text(
            record.before_data.get("name"), fallback="Без названия"
        )
        after_name = _safe_text(record.after_data.get("name"), fallback="Без названия")
        embed.add_field(
            name="Название",
            value=f"**{before_name} → {after_name}**",
            inline=False,
        )
        return
    if keys == {"topic"}:
        topic = record.after_data.get("topic")
        embed.add_field(
            name="Тема",
            value=_safe_text(topic, fallback="Тема удалена"),
            inline=False,
        )
        return
    _add_change_fields(embed, record)


def build_audit_embed(
    record: AuditEventRecord,
    *,
    report_timezone: ZoneInfo = ZoneInfo("UTC"),
) -> discord.Embed:
    """Render a persisted event without requiring original Discord objects."""

    embed = discord.Embed(
        title=(
            _channel_update_title(record)
            if record.event_type == "channel.updated"
            else _TITLES.get(record.event_type, "Событие сервера")
        ),
        color=KANAMI_EMBED_COLOR,
        timestamp=record.occurred_at,
    )
    if record.event_type.startswith("rules."):
        version = record.details_data.get("version", "—")
        embed.description = f"Версия **{_safe_text(version)}**"
        previous = record.details_data.get("previous_published_version")
        if previous:
            embed.add_field(
                name="Предыдущая опубликованная версия",
                value=_safe_text(previous),
                inline=False,
            )
    user_mention = f"<@{record.subject_id}>" if record.subject_id else None
    if record.subject_type == "user" and user_mention:
        embed.description = user_mention

    if record.event_type.startswith(("user.", "member.")):
        _add_change_fields(embed, record)
    if record.event_type == "member.roles_updated":
        added = record.details_data.get("added_role_ids", [])
        removed = record.details_data.get("removed_role_ids", [])
        if added:
            embed.add_field(
                name="Добавлены",
                value=_compact_lines([f"<@&{item}>" for item in added]),
                inline=False,
            )
        if removed:
            embed.add_field(
                name="Удалены",
                value=_compact_lines([f"<@&{item}>" for item in removed]),
                inline=False,
            )
    if record.event_type == "member.timeout_updated":
        before = record.before_data.get("timed_out_until")
        after = record.after_data.get("timed_out_until")
        value = "Снят" if after is None else f"Установлен до: {_safe_text(after)}"
        if before and after:
            value = (
                f"Срок изменён\nБыло: {_safe_text(before)}\nСтало: {_safe_text(after)}"
            )
        embed.add_field(name="Тайм-аут", value=value, inline=False)
    if record.event_type.startswith("voice."):
        _add_voice_fields(embed, record, report_timezone)
    if record.event_type.startswith("channel."):
        embed.description = _channel_display(record)
        if record.event_type == "channel.updated":
            _add_channel_update_fields(embed, record)
        has_channel_type = (
            "channel_type" in record.after_data or "channel_type" in record.before_data
        )
        if record.event_type != "channel.updated" or has_channel_type:
            channel_type = record.after_data.get(
                "channel_type", record.before_data.get("channel_type")
            )
            embed.add_field(name="Тип", value=_safe_text(channel_type), inline=True)
    if record.event_type.startswith("role."):
        data = record.after_data or record.before_data
        embed.description = f"**{_safe_text(data.get('name'))}**"
        if record.event_type == "role.updated":
            _add_change_fields(embed, record)
            added = record.details_data.get("added_permissions", [])
            removed = record.details_data.get("removed_permissions", [])
            if added:
                embed.add_field(
                    name="Добавлены разрешения",
                    value=_compact_lines([", ".join(added)]),
                    inline=False,
                )
            if removed:
                embed.add_field(
                    name="Убраны разрешения",
                    value=_compact_lines([", ".join(removed)]),
                    inline=False,
                )
    thumbnail = _asset_url(record.after_data, "avatar") or _asset_url(
        record.after_data, "guild_avatar"
    )
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    embed.set_footer(text=f"Event ID: {record.id}")
    return embed


def build_channel_position_batch_embed(
    records: Sequence[AuditEventRecord],
) -> discord.Embed:
    """Render one Discord presentation for separately persisted reorder events."""

    if not records or not all(
        _is_pure_channel_position_update(item) for item in records
    ):
        raise ValueError("position batch requires pure channel position updates")
    lines = []
    for record in records:
        before = record.before_data.get("position")
        after = record.after_data.get("position")
        lines.append(f"{_channel_display(record)} — **{before} → {after}**")
    embed = discord.Embed(
        title="Изменён порядок каналов",
        description="\n".join(lines)[:4096],
        color=KANAMI_EMBED_COLOR,
        timestamp=records[-1].occurred_at,
    )
    ids = ", ".join(str(record.id) for record in records)
    embed.set_footer(text=f"Event IDs: {ids}"[:2048])
    return embed


@dataclass(frozen=True, slots=True)
class _DeliveryGroup:
    records: tuple[AuditEventRecord, ...]

    @property
    def event_ids(self) -> tuple[int, ...]:
        return tuple(record.id for record in self.records)

    @property
    def delivery_attempts(self) -> int:
        return max(record.delivery_attempts for record in self.records)

    @property
    def event_type(self) -> str:
        return self.records[0].event_type

    def build_embed(self, report_timezone: ZoneInfo) -> discord.Embed:
        if self.event_type == "member.anniversary":
            record = self.records[0]
            years = record.details_data.get("years")
            if record.subject_id is None or not isinstance(years, int) or years < 1:
                raise ValueError("invalid persisted member anniversary event")
            return build_member_anniversary_notification_embed(
                user_id=record.subject_id,
                years=years,
            )
        if self.event_type == "member.returned":
            record = self.records[0]
            values = {
                key: record.details_data.get(key)
                for key in (
                    "absence_seconds",
                    "voice_seconds",
                    "message_count",
                    "achievement_count",
                    "return_number",
                )
            }
            if (
                record.subject_id is None
                or any(
                    not isinstance(value, int) or value < 0 for value in values.values()
                )
                or values["return_number"] < 1
            ):
                raise ValueError("invalid persisted member return event")
            return build_member_return_embed(
                user_id=record.subject_id,
                **values,  # type: ignore[arg-type]
            )
        if len(self.records) > 1:
            return build_channel_position_batch_embed(self.records)
        return build_audit_embed(self.records[0], report_timezone=report_timezone)

    def allowed_mentions(self) -> discord.AllowedMentions:
        if self.event_type in {"member.anniversary", "member.returned"}:
            subject_id = self.records[0].subject_id
            if subject_id is not None:
                return discord.AllowedMentions(
                    everyone=False,
                    users=[discord.Object(id=subject_id)],
                    roles=False,
                    replied_user=False,
                )
        return discord.AllowedMentions.none()


def _position_batch_scope(record: AuditEventRecord) -> tuple[str, object]:
    if "category_id" in record.details_data:
        return ("category", record.details_data.get("category_id"))
    return ("channel", record.channel_id or record.subject_id)


def _prepare_delivery_groups(
    records: Sequence[AuditEventRecord],
    *,
    as_of: datetime,
    batch_window: timedelta,
) -> tuple[tuple[_DeliveryGroup, ...], datetime | None]:
    normal_groups = [
        _DeliveryGroup((record,))
        for record in records
        if not _is_pure_channel_position_update(record)
    ]
    by_scope: dict[tuple[str, object], list[AuditEventRecord]] = {}
    for record in records:
        if _is_pure_channel_position_update(record):
            by_scope.setdefault(_position_batch_scope(record), []).append(record)

    ready_position_groups: list[_DeliveryGroup] = []
    next_ready_at: datetime | None = None
    for scoped_records in by_scope.values():
        cluster: list[AuditEventRecord] = []
        for record in scoped_records:
            if cluster and record.occurred_at - cluster[-1].occurred_at > batch_window:
                ready_at = cluster[-1].occurred_at + batch_window
                if ready_at <= as_of:
                    ready_position_groups.append(_DeliveryGroup(tuple(cluster)))
                elif next_ready_at is None or ready_at < next_ready_at:
                    next_ready_at = ready_at
                cluster = []
            cluster.append(record)
        if cluster:
            ready_at = cluster[-1].occurred_at + batch_window
            if ready_at <= as_of:
                ready_position_groups.append(_DeliveryGroup(tuple(cluster)))
            elif next_ready_at is None or ready_at < next_ready_at:
                next_ready_at = ready_at

    groups = normal_groups + ready_position_groups
    groups.sort(key=lambda group: (group.records[0].occurred_at, group.records[0].id))
    return tuple(groups), next_ready_at


class AuditLogDeliveryRunner:
    """Deliver routed durable pending events with bounded retry backoff.

    Delivery is at-least-once: a process crash after Discord accepts a message but
    before ``mark_delivered`` commits can produce a duplicate, but not data loss.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        channel_id: int | None = None,
        event_channel_ids: Mapping[str, int] | None = None,
        event_types: Sequence[str] | None = None,
        repository_factory: AuditRepositoryFactory = SqlAlchemyAuditEventRepository,
        clock: Callable[[], datetime] = _now,
        poll_interval_seconds: float = AUDIT_DELIVERY_POLL_SECONDS,
        batch_size: int = AUDIT_DELIVERY_BATCH_SIZE,
        position_batch_window_seconds: float = (
            AUDIT_CHANNEL_POSITION_BATCH_WINDOW_SECONDS
        ),
        report_timezone: ZoneInfo = ZoneInfo("UTC"),
        settings_provider: GuildServerSettingsProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._channel_id = channel_id
        self._event_channel_ids = dict(event_channel_ids or {})
        self._settings_provider = settings_provider
        if (
            self._channel_id is None
            and not self._event_channel_ids
            and self._settings_provider is None
        ):
            raise ValueError("at least one delivery channel must be configured")
        if event_types is not None:
            self._pending_event_types = tuple(sorted(set(event_types)))
        elif self._channel_id is None:
            self._pending_event_types = tuple(sorted(self._event_channel_ids))
        else:
            self._pending_event_types = None
        self._repository_factory = repository_factory
        self._clock = clock
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
        if position_batch_window_seconds <= 0:
            raise ValueError("position_batch_window_seconds must be positive")
        self._position_batch_window = timedelta(seconds=position_batch_window_seconds)
        self._report_timezone = report_timezone
        self._next_batch_ready_at: datetime | None = None
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._client: discord.Client | None = None

    def wake(self) -> None:
        self._wakeup.set()

    def start(self, client: discord.Client) -> None:
        if self._task is not None and not self._task.done():
            return
        self._client = client
        self._task = asyncio.create_task(self._run(), name="audit-delivery-loop")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._client = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def run_once(self, client: discord.Client) -> int:
        as_of = self._clock()
        channel_id = self._channel_id
        event_channel_ids = self._event_channel_ids
        pending_event_types = self._pending_event_types
        if self._settings_provider is not None:
            settings = await self._settings_provider.get()
            channel_id = settings.audit_log_channel_id
            event_channel_ids = {
                event_type: configured_channel
                for event_type, configured_channel in (
                    ("member.anniversary", settings.anniversary_channel_id),
                    ("member.returned", settings.return_channel_id),
                )
                if configured_channel is not None
            }
            event_types = set(event_channel_ids)
            if channel_id is not None:
                event_types.update(
                    SUPPORTED_EVENT_TYPES - SPECIALIZED_DELIVERY_EVENT_TYPES
                )
            if not event_types:
                self._next_batch_ready_at = None
                return 0
            pending_event_types = tuple(sorted(event_types))
        async with self._session_factory() as session:
            repository = self._repository_factory(session)
            pending_kwargs: dict[str, object] = {
                "guild_id": self._guild_id,
                "as_of": as_of,
                "limit": self._batch_size,
            }
            if pending_event_types is not None:
                pending_kwargs["event_types"] = pending_event_types
            records = await repository.get_pending_delivery(
                **pending_kwargs,  # type: ignore[arg-type]
            )
        groups, self._next_batch_ready_at = _prepare_delivery_groups(
            records,
            as_of=as_of,
            batch_window=self._position_batch_window,
        )
        delivered = 0
        for group in groups:
            try:
                routed_channel_id = event_channel_ids.get(
                    group.event_type,
                    channel_id,
                )
                if routed_channel_id is None:
                    raise RuntimeError(
                        f"no delivery channel configured for {group.event_type}"
                    )
                channel = client.get_channel(routed_channel_id)
                channel_guild = getattr(channel, "guild", None)
                if (
                    channel is None
                    or not callable(getattr(channel, "send", None))
                    or getattr(channel_guild, "id", None) != self._guild_id
                ):
                    raise RuntimeError("configured delivery channel is unavailable")
                send_kwargs: dict[str, object] = {
                    "embed": group.build_embed(self._report_timezone),
                    "allowed_mentions": group.allowed_mentions(),
                }
                if group.event_type in {"member.anniversary", "member.returned"}:
                    send_kwargs["nonce"] = group.records[0].id
                message = await channel.send(
                    **send_kwargs,
                )
                delivered_at = self._clock()
                async with self._session_factory.begin() as session:
                    await self._repository_factory(session).mark_delivered_many(
                        group.event_ids, message.id, delivered_at
                    )
                delivered += len(group.records)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                delay = AUDIT_RETRY_DELAYS_SECONDS[
                    min(group.delivery_attempts, len(AUDIT_RETRY_DELAYS_SECONDS) - 1)
                ]
                next_attempt_at = self._clock() + timedelta(seconds=delay)
                error_summary = f"{type(error).__name__}: {error}"[:1000]
                try:
                    async with self._session_factory.begin() as session:
                        await self._repository_factory(
                            session
                        ).mark_delivery_failed_many(
                            group.event_ids, error_summary, next_attempt_at
                        )
                except Exception:
                    logger.exception(
                        "Durable delivery failure state could not be persisted "
                        "event_ids=%s guild_id=%s",
                        group.event_ids,
                        self._guild_id,
                    )
                logger.warning(
                    "Durable delivery failed event_ids=%s guild_id=%s "
                    "retry_in=%ss error=%s",
                    group.event_ids,
                    self._guild_id,
                    delay,
                    error_summary,
                )
        return delivered

    async def _run(self) -> None:
        while True:
            client = self._client
            if client is None:
                return
            try:
                await self.run_once(client)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Durable delivery cycle failed guild_id=%s", self._guild_id
                )
            self._wakeup.clear()
            timeout = self._poll_interval_seconds
            if self._next_batch_ready_at is not None:
                seconds_until_ready = (
                    self._next_batch_ready_at - self._clock()
                ).total_seconds()
                timeout = min(timeout, max(0.01, seconds_until_ready))
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=timeout)
            except TimeoutError:
                pass


class AuditRetentionRunner:
    """Delete expired transient events at startup and approximately daily."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        repository_factory: AuditRepositoryFactory = SqlAlchemyAuditEventRepository,
        clock: Callable[[], datetime] = _now,
        interval_seconds: float = AUDIT_CLEANUP_INTERVAL_SECONDS,
        settings_provider: GuildServerSettingsProvider | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._repository_factory = repository_factory
        self._clock = clock
        self._interval_seconds = interval_seconds
        self._settings_provider = settings_provider
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="audit-retention-loop")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def run_once(self) -> int:
        if (
            self._settings_provider is not None
            and (await self._settings_provider.get()).audit_log_channel_id is None
        ):
            return 0
        async with self._session_factory.begin() as session:
            deleted = await self._repository_factory(session).delete_expired(
                as_of=self._clock()
            )
        if deleted:
            logger.info("Expired transient audit events deleted count=%s", deleted)
        return deleted

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Audit retention cleanup failed")
            await asyncio.sleep(self._interval_seconds)
