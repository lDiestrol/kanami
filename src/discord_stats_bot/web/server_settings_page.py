"""OWNER/ADMIN Web Admin presentation for guild server settings."""

import logging
from html import escape
from urllib.parse import parse_qs, urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from discord_stats_bot.config import MAX_DISCORD_SNOWFLAKE
from discord_stats_bot.features.server_settings import (
    GuildServerSettingKey,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
    GuildServerSettingSource,
    ServerSettingsOptions,
)
from discord_stats_bot.web.auth import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    WebSession,
    WebSessionStore,
    constant_time_token_equal,
)
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationDecision,
    WebAdminRole,
)
from discord_stats_bot.web.bot_control import (
    ServerSettingsControl,
    ServerSettingsControlCategory,
    ServerSettingsControlError,
)
from discord_stats_bot.web.presentation import render_admin_page
from discord_stats_bot.web.security import WebWriteRateLimiter
from discord_stats_bot.web.server_settings import (
    WebAdminServerSettingsReadService,
    WebAdminServerSettingValue,
)

logger = logging.getLogger(__name__)
FORM_MAX_BYTES = 4_096
RESPONSE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
ALLOWED_ROLES = {WebAdminRole.OWNER, WebAdminRole.ADMIN}
SETTING_TITLES = {
    GuildServerSettingKey.AUTOROLE_ROLE: "Автоматическая роль",
    GuildServerSettingKey.AUDIT_LOG_CHANNEL: "Журнал аудита",
    GuildServerSettingKey.ANNIVERSARY_CHANNEL: "Поздравления с годовщиной",
    GuildServerSettingKey.RETURN_CHANNEL: "Возвращения участников",
}


def _denied(status_code: int = 403) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><h1>Request denied</h1>",
        status_code=status_code,
        headers=RESPONSE_HEADERS,
    )


def _revoke_session_and_deny(request: Request) -> HTMLResponse:
    sessions: WebSessionStore = request.state.web_sessions
    sessions.revoke(request.state.web_session_id)
    response = _denied()
    settings = request.state.web_settings
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        secure=settings.web_admin_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


async def _fresh_allowed(request: Request, session: WebSession) -> bool:
    try:
        decision: WebAdminAuthorizationDecision = (
            await request.state.admin_authorizer.authorize(session.discord_user_id)
        )
    except Exception as error:
        logger.warning(
            "web_admin_server_settings_authorization_failed actor=%s error_type=%s",
            session.discord_user_id,
            type(error).__name__,
        )
        return False
    return decision.allowed and decision.role in ALLOWED_ROLES


async def _read_form(request: Request) -> dict[str, list[str]] | None:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
        "application/x-www-form-urlencoded"
    ):
        return None
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > FORM_MAX_BYTES:
                return None
        except ValueError:
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


def _parse_change(
    values: dict[str, list[str]] | None,
    session: WebSession,
) -> tuple[GuildServerSettingKey, GuildServerSettingOverride] | None:
    if values is None:
        return None
    single = {key: item[0] for key, item in values.items() if len(item) == 1}
    if len(single) != len(values):
        return None
    try:
        key = GuildServerSettingKey(single["setting"])
        mode = GuildServerSettingOverrideMode(single["mode"])
    except (KeyError, ValueError):
        return None
    expected_fields = (
        {"csrf_token", "setting", "mode", "value"}
        if mode is GuildServerSettingOverrideMode.VALUE
        else {"csrf_token", "setting", "mode"}
    )
    if set(single) != expected_fields:
        return None
    value = None
    if mode is GuildServerSettingOverrideMode.VALUE:
        raw_value = single["value"]
        if not raw_value.isascii() or not raw_value.isdecimal():
            return None
        value = int(raw_value)
        if not 0 < value <= MAX_DISCORD_SNOWFLAKE:
            return None
    try:
        return key, GuildServerSettingOverride(mode, value)
    except ValueError:
        return None


def _redirect(*, result: str | None = None, error: str | None = None) -> Response:
    parameters = {
        key: value
        for key, value in (("result", result), ("error", error))
        if value is not None
    }
    location = "/admin/server-settings"
    if parameters:
        location += f"?{urlencode(parameters)}"
    return RedirectResponse(location, status_code=303, headers=RESPONSE_HEADERS)


def _option_map(
    key: GuildServerSettingKey,
    options: ServerSettingsOptions,
) -> dict[int, str]:
    items = (
        options.roles
        if key is GuildServerSettingKey.AUTOROLE_ROLE
        else options.channels
    )
    return {option.id: option.name for option in items}


def _current_text(
    setting: WebAdminServerSettingValue,
    options: ServerSettingsOptions,
) -> str:
    if setting.effective_value is None:
        return "Отключено"
    name = _option_map(setting.key, options).get(setting.effective_value)
    if name is None:
        return "Текущее значение недоступно (объект отсутствует или запрещён)"
    prefix = "@" if setting.key is GuildServerSettingKey.AUTOROLE_ROLE else "#"
    return f"{prefix}{name}"


def _source_text(source: GuildServerSettingSource) -> str:
    return {
        GuildServerSettingSource.ENV: "ENV",
        GuildServerSettingSource.DB: "Web Admin / DB",
        GuildServerSettingSource.DISABLED: "Отключено",
    }[source]


def _source_badge_class(source: GuildServerSettingSource) -> str:
    return {
        GuildServerSettingSource.ENV: "info",
        GuildServerSettingSource.DB: "accent",
        GuildServerSettingSource.DISABLED: "neutral",
    }[source]


def _setting_card(
    setting: WebAdminServerSettingValue,
    options: ServerSettingsOptions,
    csrf_token: str,
) -> str:
    escaped_csrf = escape(csrf_token, quote=True)
    key = escape(setting.key.value, quote=True)
    choices = _option_map(setting.key, options)
    selected_value = (
        setting.effective_value
        if setting.effective_value is not None and setting.effective_value in choices
        else None
    )
    placeholder = (
        "Выберите роль"
        if setting.key is GuildServerSettingKey.AUTOROLE_ROLE
        else "Выберите канал"
    )
    select_options = "".join(
        f'<option value="{option_id}"'
        f"{' selected' if option_id == selected_value else ''}>"
        f"{escape(name)}</option>"
        for option_id, name in choices.items()
    )
    select_options = (
        f'<option value="" disabled'
        f"{' selected' if selected_value is None else ''}>"
        f"{escape(placeholder)}</option>{select_options}"
    )
    value_form = (
        '<form method="post" action="/admin/server-settings">'
        f'<input type="hidden" name="csrf_token" value="{escaped_csrf}">'
        f'<input type="hidden" name="setting" value="{key}">'
        '<input type="hidden" name="mode" value="value">'
        f'<select name="value" required>{select_options}</select>'
        '<button class="secondary" type="submit">Выбрать значение</button></form>'
        if choices
        else '<p class="muted">Допустимые текущие объекты отсутствуют.</p>'
    )
    return f"""<section class="card">
<h2>{escape(SETTING_TITLES[setting.key])}</h2>
<p><strong>Текущее значение:</strong> {escape(_current_text(setting, options))}<br>
<strong>Источник:</strong> <span class="badge {_source_badge_class(setting.source)}">{escape(_source_text(setting.source))}</span></p>
<div class="actions">
<form method="post" action="/admin/server-settings">
<input type="hidden" name="csrf_token" value="{escaped_csrf}">
<input type="hidden" name="setting" value="{key}">
<input type="hidden" name="mode" value="env">
<button class="secondary" type="submit">Использовать ENV</button></form>
{value_form}
<form method="post" action="/admin/server-settings">
<input type="hidden" name="csrf_token" value="{escaped_csrf}">
<input type="hidden" name="setting" value="{key}">
<input type="hidden" name="mode" value="disabled">
<button class="danger" type="submit">Отключить</button></form>
</div></section>"""


def render_server_settings_page(
    settings: tuple[WebAdminServerSettingValue, ...] | None,
    options: ServerSettingsOptions | None,
    *,
    csrf_token: str,
    role: WebAdminRole,
    result: str | None = None,
    error: str | None = None,
) -> str:
    notices = {
        "saved": "Настройка сохранена.",
        "unchanged": "Настройка уже имела это значение.",
    }
    errors = {
        "invalid_target": "Выбранный объект больше недоступен.",
        "control_unavailable": "Control service временно недоступен.",
    }
    notice = ""
    if result in notices:
        notice = f'<p class="notice success">{escape(notices[result])}</p>'
    elif error in errors:
        notice = f'<p class="notice failure">{escape(errors[error])}</p>'
    unavailable = settings is None or options is None
    content = (
        '<p class="notice failure">Настройки временно недоступны.</p>'
        if unavailable
        else "".join(
            _setting_card(setting, options, csrf_token) for setting in settings
        )
    )
    return render_admin_page(
        "Настройки сервера",
        f"{notice}{content}",
        role=role,
        csrf_token=csrf_token,
        active_path="/admin/server-settings",
        description="Для каждой функции выберите ENV, текущее значение Discord или отключение",
        kicker="Server configuration",
    )


async def admin_server_settings(request: Request) -> Response:
    session: WebSession = request.state.web_session
    if not await _fresh_allowed(request, session):
        return _denied()
    read_service: WebAdminServerSettingsReadService = (
        request.state.server_settings_read_service
    )
    control: ServerSettingsControl = request.state.bot_profile_control
    settings = await read_service.load()
    try:
        options = await control.get_server_settings_options()
    except ServerSettingsControlError as error:
        logger.warning(
            "web_admin_server_settings_options_failed actor=%s category=%s",
            session.discord_user_id,
            error.category,
        )
        options = None
    return HTMLResponse(
        render_server_settings_page(
            settings,
            options,
            csrf_token=session.csrf_token,
            role=session.role,
            result=request.query_params.get("result"),
            error=request.query_params.get("error"),
        ),
        status_code=200 if settings is not None and options is not None else 503,
        headers=RESPONSE_HEADERS,
    )


async def admin_server_settings_update(request: Request) -> Response:
    session: WebSession = request.state.web_session
    values = await _read_form(request)
    csrf_values = values.get("csrf_token", []) if values is not None else []
    if len(csrf_values) != 1 or not constant_time_token_equal(
        csrf_values[0], session.csrf_token
    ):
        return _denied()
    change = _parse_change(values, session)
    if change is None:
        return _denied(status_code=400)
    if not await _fresh_allowed(request, session):
        return _revoke_session_and_deny(request)
    limiter: WebWriteRateLimiter = request.state.web_write_limiter
    if not limiter.allow(request.state.web_session_id):
        return _denied(status_code=429)
    key, override = change
    control: ServerSettingsControl = request.state.bot_profile_control
    if override.mode is GuildServerSettingOverrideMode.VALUE:
        try:
            options = await control.get_server_settings_options()
        except ServerSettingsControlError:
            return _redirect(error="control_unavailable")
        if override.value not in _option_map(key, options):
            return _redirect(error="invalid_target")
    try:
        changed = await control.change_server_setting(
            key,
            override,
            actor_discord_user_id=session.discord_user_id,
        )
    except ServerSettingsControlError as error:
        logger.warning(
            "web_admin_server_setting_change_failed actor=%s setting=%s category=%s",
            session.discord_user_id,
            key.value,
            error.category,
        )
        category = (
            "invalid_target"
            if error.category is ServerSettingsControlCategory.INVALID_TARGET
            else "control_unavailable"
        )
        return _redirect(error=category)
    logger.info(
        "web_admin_server_setting_change_succeeded actor=%s setting=%s changed=%s",
        session.discord_user_id,
        key.value,
        changed,
    )
    return _redirect(result="saved" if changed else "unchanged")
