"""OWNER-only managed Web Admin access page and mutation routes."""

import logging
from html import escape
from typing import Protocol
from urllib.parse import parse_qs, urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from discord_stats_bot.web.auth import WebSession, constant_time_token_equal
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationDecision,
    WebAdminRole,
)
from discord_stats_bot.web.bot_control import (
    WebAdminAccessControl,
    WebAdminAccessControlError,
)
from discord_stats_bot.web.presentation import render_admin_page
from discord_stats_bot.web.security import WebWriteRateLimiter
from discord_stats_bot.web.service import (
    MAX_POSTGRESQL_BIGINT,
    WebAdminAdministrators,
    WebAdminManagedAccessRepository,
    WebAdminMembershipRepository,
)

logger = logging.getLogger(__name__)
FORM_MAX_BYTES = 4_096
RESPONSE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class AdministratorService(Protocol):
    async def load(self) -> WebAdminAdministrators | None: ...

    def is_owner(self, user_id: int) -> bool: ...

    async def is_active_admin(self, user_id: int) -> bool: ...

    async def is_current_non_bot_member(self, user_id: int) -> bool: ...


class WebAdminAdministratorService:
    """Compose SELECT-only access and membership repositories for the page."""

    def __init__(
        self,
        owner_user_ids: frozenset[int],
        access_repository: WebAdminManagedAccessRepository,
        membership_repository: WebAdminMembershipRepository,
    ) -> None:
        self._owner_user_ids = owner_user_ids
        self._access_repository = access_repository
        self._membership_repository = membership_repository

    async def load(self) -> WebAdminAdministrators | None:
        return await self._access_repository.list_administrators(self._owner_user_ids)

    def is_owner(self, user_id: int) -> bool:
        return user_id in self._owner_user_ids

    async def is_active_admin(self, user_id: int) -> bool:
        return await self._access_repository.is_active_admin(user_id)

    async def is_current_non_bot_member(self, user_id: int) -> bool:
        return await self._membership_repository.is_current_non_bot_member(user_id)


def _revoke_form(user_id: int, csrf_token: str) -> str:
    escaped_csrf = escape(csrf_token, quote=True)
    return (
        '<form method="post" action="/admin/administrators/revoke">'
        f'<input type="hidden" name="csrf_token" value="{escaped_csrf}">'
        f'<input type="hidden" name="user_id" value="{user_id}">'
        '<button class="danger" type="submit">Отозвать</button></form>'
    )


def render_administrators_page(
    administrators: WebAdminAdministrators | None,
    *,
    csrf_token: str,
    result: str | None,
    error: str | None,
) -> str:
    notices = ""
    result_messages = {
        "granted": "Доступ ADMIN выдан.",
        "already_admin": "Пользователь уже является active ADMIN.",
        "revoked": "Доступ ADMIN отозван.",
        "owner_protected": "OWNER защищён и не изменён.",
        "not_active": "Active ADMIN не найден.",
    }
    error_messages = {
        "invalid_request": "Некорректный запрос.",
        "invalid_target": "Нужен текущий non-bot участник настроенного сервера.",
        "control_unavailable": "Control service недоступен. Изменения не применены.",
        "read_unavailable": "Не удалось загрузить список администраторов.",
    }
    if result in result_messages:
        notices += (
            f'<section class="notice success">{result_messages[result]}</section>'
        )
    if error in error_messages:
        notices += f'<section class="notice failure">{error_messages[error]}</section>'

    if administrators is None:
        owners = (
            '<tr><td colspan="4" class="muted">Данные временно недоступны.</td></tr>'
        )
        admins = (
            '<tr><td colspan="6" class="muted">Данные временно недоступны.</td></tr>'
        )
        owner_records = admin_records = (
            '<p class="muted">Данные временно недоступны.</p>'
        )
    else:
        owners = (
            "".join(
                "<tr>"
                f"<td>{escape(item.display_name)}</td><td><code>{item.user_id}</code></td>"
                '<td><span class="badge owner">OWNER</span></td>'
                '<td><span class="protected">Постоянный доступ</span></td>'
                "</tr>"
                for item in administrators.owners
            )
            or '<tr><td colspan="4" class="muted">OWNER не настроены.</td></tr>'
        )
        owner_records = (
            "".join(
                '<article class="mobile-record administrator-record"><header class="record-header">'
                '<div class="record-heading">'
                f'<h3 class="record-title">{escape(item.display_name)}</h3>'
                f'<code class="record-identifier">{item.user_id}</code></div>'
                '<span class="badge owner">OWNER</span></header>'
                '<dl class="record-fields"><div><dt>Источник доступа</dt>'
                '<dd class="protected">Постоянный доступ</dd></div></dl></article>'
                for item in administrators.owners
            )
            or '<p class="muted">OWNER не настроены.</p>'
        )
        admins = (
            "".join(
                "<tr>"
                f"<td>{escape(item.display_name)}</td><td><code>{item.user_id}</code></td>"
                '<td><span class="badge admin">ADMIN</span></td>'
                f"<td>{escape(item.granted_at.isoformat() if item.granted_at else '—')}</td>"
                f"<td><code>{item.granted_by_user_id or '—'}</code></td>"
                f"<td>{_revoke_form(item.user_id, csrf_token)}</td>"
                "</tr>"
                for item in administrators.admins
            )
            or '<tr><td colspan="6" class="muted">Active ADMIN отсутствуют.</td></tr>'
        )
        admin_records = (
            "".join(
                '<article class="mobile-record administrator-record"><header class="record-header">'
                '<div class="record-heading">'
                f'<h3 class="record-title">{escape(item.display_name)}</h3>'
                f'<code class="record-identifier">{item.user_id}</code></div>'
                '<span class="badge admin">ADMIN</span></header>'
                '<dl class="record-fields">'
                "<div><dt>Источник доступа</dt><dd>Managed grant</dd></div>"
                f"<div><dt>Выдан</dt><dd>{escape(item.granted_at.isoformat() if item.granted_at else '—')}</dd></div>"
                f"<div><dt>Кем</dt><dd><code>{item.granted_by_user_id or '—'}</code></dd></div>"
                f'</dl><footer class="record-footer">{_revoke_form(item.user_id, csrf_token)}</footer></article>'
                for item in administrators.admins
            )
            or '<p class="muted">Active ADMIN отсутствуют.</p>'
        )

    body = f"""{notices}
<section class="card"><form class="form" method="post" action="/admin/administrators/grant">
<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}">
<label>Discord User ID<input name="user_id" inputmode="numeric" required></label><button type="submit">Добавить ADMIN</button>
</form></section>
<h2>OWNER</h2><div class="table-wrap responsive-desktop-only"><table><thead><tr><th>Имя</th><th>Discord ID</th><th>Роль</th><th>Статус</th></tr></thead><tbody>{owners}</tbody></table></div>
<section class="mobile-record-list responsive-mobile-only" aria-label="OWNER">{owner_records}</section>
<h2>Managed ADMIN</h2><div class="table-wrap responsive-desktop-only"><table><thead><tr><th>Имя</th><th>Discord ID</th><th>Роль</th><th>Выдан</th><th>Кем</th><th></th></tr></thead><tbody>{admins}</tbody></table></div>
<section class="mobile-record-list responsive-mobile-only" aria-label="Managed ADMIN">{admin_records}</section>"""
    return render_admin_page(
        "Администраторы",
        body,
        role=WebAdminRole.OWNER,
        csrf_token=csrf_token,
        active_path="/admin/administrators",
        description="OWNER задаются конфигурацией и защищены; managed ADMIN хранятся в PostgreSQL",
        wide=True,
        kicker="Access control",
    )


def _denied(status_code: int = 403) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><h1>Request denied</h1>",
        status_code=status_code,
        headers=RESPONSE_HEADERS,
    )


async def _fresh_owner(request: Request, session: WebSession) -> bool:
    try:
        decision: WebAdminAuthorizationDecision = (
            await request.state.admin_authorizer.authorize(session.discord_user_id)
        )
    except Exception as error:
        logger.warning(
            "web_admin_owner_authorization_failed actor=%s error_type=%s",
            session.discord_user_id,
            type(error).__name__,
        )
        return False
    return decision.allowed and decision.role is WebAdminRole.OWNER


async def _read_form(request: Request) -> dict[str, list[str]] | None:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "application/x-www-form-urlencoded"
    ):
        return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > FORM_MAX_BYTES:
            return None
    try:
        return parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return None


def _target(values: dict[str, list[str]] | None, session: WebSession) -> int | None:
    if values is None or set(values) != {"csrf_token", "user_id"}:
        return None
    csrf = values["csrf_token"]
    users = values["user_id"]
    if (
        len(csrf) != 1
        or not constant_time_token_equal(csrf[0], session.csrf_token)
        or len(users) != 1
    ):
        return None
    raw = users[0]
    if not raw.isascii() or not raw.isdecimal():
        return None
    user_id = int(raw)
    return user_id if 0 < user_id <= MAX_POSTGRESQL_BIGINT else None


def _redirect(
    *, result: str | None = None, error: str | None = None
) -> RedirectResponse:
    query = urlencode(
        {key: value for key, value in (("result", result), ("error", error)) if value}
    )
    location = "/admin/administrators" + (f"?{query}" if query else "")
    return RedirectResponse(location, status_code=303, headers=RESPONSE_HEADERS)


async def admin_administrators(request: Request) -> Response:
    session: WebSession = request.state.web_session
    if not await _fresh_owner(request, session):
        return _denied()
    service: AdministratorService = request.state.administrator_service
    administrators = await service.load()
    return HTMLResponse(
        render_administrators_page(
            administrators,
            csrf_token=session.csrf_token,
            result=request.query_params.get("result"),
            error=request.query_params.get("error"),
        ),
        status_code=200 if administrators is not None else 503,
        headers=RESPONSE_HEADERS,
    )


async def _mutate(request: Request, operation: str) -> Response:
    session: WebSession = request.state.web_session
    values = await _read_form(request)
    target = _target(values, session)
    if target is None:
        return _denied(status_code=400)
    if not await _fresh_owner(request, session):
        return _denied()
    limiter: WebWriteRateLimiter = request.state.web_write_limiter
    if not limiter.allow(request.state.web_session_id):
        return _denied(status_code=429)
    service: AdministratorService = request.state.administrator_service
    if service.is_owner(target):
        return _redirect(result="owner_protected")
    active = await service.is_active_admin(target)
    if operation == "grant":
        if active:
            return _redirect(result="already_admin")
        if not await service.is_current_non_bot_member(target):
            return _redirect(error="invalid_target")
    elif not active:
        return _redirect(result="not_active")
    control: WebAdminAccessControl = request.state.bot_profile_control
    try:
        changed = (
            await control.grant_web_admin_access(
                target, actor_discord_user_id=session.discord_user_id
            )
            if operation == "grant"
            else await control.revoke_web_admin_access(
                target, actor_discord_user_id=session.discord_user_id
            )
        )
    except WebAdminAccessControlError as error:
        logger.warning(
            "web_admin_access_mutation_failed actor=%s operation=%s category=%s",
            session.discord_user_id,
            operation,
            error.category,
        )
        return _redirect(error="control_unavailable")
    result = "granted" if operation == "grant" else "revoked"
    if not changed:
        result = "already_admin" if operation == "grant" else "not_active"
    return _redirect(result=result)


async def admin_administrators_grant(request: Request) -> Response:
    return await _mutate(request, "grant")


async def admin_administrators_revoke(request: Request) -> Response:
    return await _mutate(request, "revoke")
