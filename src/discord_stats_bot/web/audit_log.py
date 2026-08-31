"""OWNER-only read-only Web Admin access audit page."""

import logging
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import Text, and_, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased
from starlette.requests import Request
from starlette.responses import HTMLResponse, Response

from discord_stats_bot.persistence.models import AuditEvent, DiscordUser, GuildMember
from discord_stats_bot.web.auth import WebSession
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationDecision,
    WebAdminRole,
)
from discord_stats_bot.web.presentation import render_admin_page

logger = logging.getLogger(__name__)
WEB_ADMIN_AUDIT_CATEGORY = "web_admin"
WEB_ADMIN_AUDIT_EVENT_TYPES = (
    "web_admin.access_granted",
    "web_admin.access_revoked",
    "web_admin.server_setting_changed",
    "rules.draft_created",
    "rules.draft_updated",
    "rules.draft_deleted",
    "rules.published",
)
WEB_ADMIN_AUDIT_LIMIT = 100
RESPONSE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


@dataclass(frozen=True, slots=True)
class WebAdminAuditEntry:
    event_id: int
    event_type: str
    occurred_at: datetime
    actor_user_id: int
    actor_display_name: str
    target_user_id: int | None
    target_display_name: str | None
    before_data: object = None
    after_data: object = None
    details_data: object = None


class AuditLogService(Protocol):
    report_timezone: ZoneInfo

    async def load_recent(self) -> tuple[WebAdminAuditEntry, ...] | None: ...


class WebAdminAuditLogService:
    """Load bounded Web Admin history through SELECT statements."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
    ) -> None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        self._session_factory = session_factory
        self._guild_id = guild_id
        self.report_timezone = report_timezone

    async def load_recent(self) -> tuple[WebAdminAuditEntry, ...] | None:
        actor_user = aliased(DiscordUser, name="actor_user")
        actor_member = aliased(GuildMember, name="actor_member")
        target_user = aliased(DiscordUser, name="target_user")
        target_member = aliased(GuildMember, name="target_member")
        actor_name = func.coalesce(
            func.nullif(actor_member.nickname, ""),
            func.nullif(actor_user.global_name, ""),
            func.nullif(actor_user.username, ""),
            cast(AuditEvent.actor_user_id, Text),
        ).label("actor_display_name")
        target_name = func.coalesce(
            func.nullif(target_member.nickname, ""),
            func.nullif(target_user.global_name, ""),
            func.nullif(target_user.username, ""),
            cast(AuditEvent.subject_id, Text),
        ).label("target_display_name")
        statement = (
            select(
                AuditEvent.id,
                AuditEvent.event_type,
                AuditEvent.occurred_at,
                AuditEvent.actor_user_id,
                actor_name,
                AuditEvent.subject_id,
                target_name,
                AuditEvent.before_data,
                AuditEvent.after_data,
                AuditEvent.details_data,
            )
            .outerjoin(actor_user, actor_user.id == AuditEvent.actor_user_id)
            .outerjoin(
                actor_member,
                and_(
                    actor_member.guild_id == self._guild_id,
                    actor_member.user_id == AuditEvent.actor_user_id,
                ),
            )
            .outerjoin(target_user, target_user.id == AuditEvent.subject_id)
            .outerjoin(
                target_member,
                and_(
                    target_member.guild_id == self._guild_id,
                    target_member.user_id == AuditEvent.subject_id,
                ),
            )
            .where(
                AuditEvent.guild_id == self._guild_id,
                AuditEvent.category == WEB_ADMIN_AUDIT_CATEGORY,
                AuditEvent.event_type.in_(WEB_ADMIN_AUDIT_EVENT_TYPES),
                AuditEvent.actor_user_id.is_not(None),
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .limit(WEB_ADMIN_AUDIT_LIMIT)
        )
        try:
            async with self._session_factory() as session:
                rows = (await session.execute(statement)).mappings().all()
        except Exception as error:
            logger.warning(
                "Web admin audit lookup failed error_type=%s",
                type(error).__name__,
            )
            return None
        return tuple(
            WebAdminAuditEntry(
                event_id=int(row["id"]),
                event_type=str(row["event_type"]),
                occurred_at=row["occurred_at"],
                actor_user_id=int(row["actor_user_id"]),
                actor_display_name=str(row["actor_display_name"]),
                target_user_id=(
                    int(row["subject_id"]) if row["subject_id"] is not None else None
                ),
                target_display_name=(
                    str(row["target_display_name"])
                    if row["target_display_name"] is not None
                    else None
                ),
                before_data=row["before_data"],
                after_data=row["after_data"],
                details_data=row["details_data"],
            )
            for row in rows
        )


def _action(event_type: str) -> str:
    return (
        "Выдан доступ ADMIN"
        if event_type == "web_admin.access_granted"
        else "Отозван доступ ADMIN"
    )


_SETTING_LABELS = {
    "autorole_role": "Автоматическая роль",
    "audit_log_channel": "Журнал аудита",
    "anniversary_channel": "Поздравления с годовщиной",
    "return_channel": "Возвращения участников",
}
_SETTING_MODE_LABELS = {
    "env": "ENV",
    "value": "Web Admin",
    "disabled": "Отключено",
}


def _setting_key(details_data: object) -> str | None:
    if not isinstance(details_data, dict):
        return None
    value = details_data.get("setting_key")
    return value if isinstance(value, str) else None


def _setting_mode(snapshot: object) -> str:
    if not isinstance(snapshot, dict):
        return "Неизвестно"
    source = snapshot.get("source")
    return (
        _SETTING_MODE_LABELS.get(source, "Неизвестно")
        if isinstance(source, str)
        else "Неизвестно"
    )


def _setting_change(entry: WebAdminAuditEntry) -> tuple[str, str, str]:
    key = _setting_key(entry.details_data)
    setting = _SETTING_LABELS.get(key, "Настройка сервера")
    transition = (
        f"{_setting_mode(entry.before_data)} → {_setting_mode(entry.after_data)}"
    )
    return "Изменена настройка", setting, transition


def _entry_cells(entry: WebAdminAuditEntry) -> tuple[str, str, str]:
    if entry.event_type == "web_admin.server_setting_changed":
        return _setting_change(entry)
    if entry.event_type.startswith("rules."):
        action = {
            "rules.draft_created": "Создан черновик правил",
            "rules.draft_updated": "Изменён черновик правил",
            "rules.draft_deleted": "Удалён черновик правил",
            "rules.published": "Опубликованы правила",
        }[entry.event_type]
        details = entry.details_data if isinstance(entry.details_data, dict) else {}
        version = details.get("version", "—")
        return action, f"Версия {escape(str(version))}", "Успешно"
    target = "Пользователь"
    if entry.target_display_name is not None and entry.target_user_id is not None:
        target = (
            f"{escape(entry.target_display_name)}<br>"
            f"<code>{entry.target_user_id}</code>"
        )
    return _action(entry.event_type), target, "ADMIN"


def render_audit_log_page(
    entries: tuple[WebAdminAuditEntry, ...] | None,
    *,
    timezone: ZoneInfo,
    csrf_token: str = "",
) -> str:
    if entries is None:
        content = (
            '<section class="notice failure">'
            "Журнал аудита временно недоступен."
            "</section>"
        )
    elif not entries:
        content = '<p class="muted">События Web Admin отсутствуют.</p>'
    else:
        rendered_rows = []
        rendered_records = []
        for entry in entries:
            action, target, result = _entry_cells(entry)
            occurred_at = escape(
                entry.occurred_at.astimezone(timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
            )
            rendered_rows.append(
                "<tr>"
                f"<td>{occurred_at}</td>"
                f"<td>{escape(action)}</td>"
                f"<td>{escape(entry.actor_display_name)}<br><code>{entry.actor_user_id}</code></td>"
                f"<td>{target}</td>"
                f'<td><span class="badge">{escape(result)}</span></td>'
                "</tr>"
            )
            rendered_records.append(
                '<article class="mobile-record audit-record"><header class="record-header">'
                f'<div class="record-heading"><time datetime="{escape(entry.occurred_at.isoformat(), quote=True)}">{occurred_at}</time>'
                f'<h2 class="record-title">{escape(action)}</h2></div>'
                f'<span class="badge">{escape(result)}</span></header>'
                '<dl class="record-fields">'
                f"<div><dt>Инициатор</dt><dd>{escape(entry.actor_display_name)}<br><code>{entry.actor_user_id}</code></dd></div>"
                f"<div><dt>Объект</dt><dd>{target}</dd></div>"
                "</dl></article>"
            )
        rows = "".join(rendered_rows)
        records = "".join(rendered_records)
        content = (
            '<div class="table-wrap responsive-desktop-only"><table><thead><tr><th>Дата и время</th>'
            "<th>Действие</th><th>Инициатор</th><th>Объект</th><th>Результат</th>"
            f"</tr></thead><tbody>{rows}</tbody></table></div>"
            f'<section class="mobile-record-list responsive-mobile-only" aria-label="События Web Admin">{records}</section>'
        )
    return render_admin_page(
        "Журнал аудита",
        content,
        role=WebAdminRole.OWNER,
        csrf_token=csrf_token,
        active_path="/admin/audit",
        description="Последние события управления Web Admin",
        wide=True,
        kicker="Administration",
    )


def _denied() -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><h1>Request denied</h1>",
        status_code=403,
        headers=RESPONSE_HEADERS,
    )


async def admin_audit_log(request: Request) -> Response:
    session: WebSession = request.state.web_session
    try:
        decision: WebAdminAuthorizationDecision = (
            await request.state.admin_authorizer.authorize(session.discord_user_id)
        )
    except Exception as error:
        logger.warning(
            "web_admin_audit_authorization_failed actor=%s error_type=%s",
            session.discord_user_id,
            type(error).__name__,
        )
        return _denied()
    if not decision.allowed or decision.role is not WebAdminRole.OWNER:
        return _denied()
    service: AuditLogService = request.state.audit_log_service
    entries = await service.load_recent()
    return HTMLResponse(
        render_audit_log_page(
            entries,
            timezone=service.report_timezone,
            csrf_token=session.csrf_token,
        ),
        status_code=200 if entries is not None else 503,
        headers=RESPONSE_HEADERS,
    )
