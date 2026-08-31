"""Server-rendered Web Admin bot-profile page and CSRF-protected actions."""

import logging
from html import escape
from urllib.parse import parse_qs, urlencode

from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.types import Message, Receive

from discord_stats_bot.features.bot_profile import (
    BOT_AVATAR_MAX_BYTES,
    BOT_NICKNAME_MAX_LENGTH,
    BotGuildProfile,
    BotProfileErrorCategory,
    BotProfileOperationError,
    normalize_bot_nickname,
    validate_bot_avatar,
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
from discord_stats_bot.web.bot_control import BotProfileControl
from discord_stats_bot.web.presentation import render_admin_page
from discord_stats_bot.web.security import WebWriteRateLimiter

logger = logging.getLogger(__name__)
FORM_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}
URLENCODED_FORM_MAX_BYTES = 4_096
MULTIPART_OVERHEAD_MAX_BYTES = 65_536
AVATAR_REQUEST_MAX_BYTES = BOT_AVATAR_MAX_BYTES + MULTIPART_OVERHEAD_MAX_BYTES
MULTIPART_FIELD_MAX_BYTES = 4_096


class AvatarRequestTooLarge(Exception):
    """Raised before an oversized ASGI body chunk reaches the multipart parser."""


def _bounded_receive(request: Request, *, limit: int) -> Receive:
    received_bytes = 0
    original_receive = request.receive

    async def receive() -> Message:
        nonlocal received_bytes
        message = await original_receive()
        if message["type"] == "http.request":
            received_bytes += len(message.get("body", b""))
            if received_bytes > limit:
                raise AvatarRequestTooLarge
        return message

    return receive


_RESULT_MESSAGES = {
    "nickname_updated": "Серверный никнейм обновлён.",
    "nickname_reset": "Серверный никнейм сброшен.",
    "avatar_updated": "Серверный аватар обновлён.",
    "avatar_reset": "Серверный аватар сброшен.",
}
_ERROR_MESSAGES = {
    BotProfileErrorCategory.CONTROL_UNAVAILABLE: (
        "Control service сейчас недоступен. Изменения не применены."
    ),
    BotProfileErrorCategory.CONTROL_UNAUTHORIZED: (
        "Control service отклонил запрос. Проверьте конфигурацию сервисов."
    ),
    BotProfileErrorCategory.BOT_NOT_READY: (
        "Discord-бот ещё не готов. Повторите попытку позже."
    ),
    BotProfileErrorCategory.GUILD_UNAVAILABLE: (
        "Настроенный Discord-сервер сейчас недоступен боту."
    ),
    BotProfileErrorCategory.INVALID_NICKNAME: "Некорректный серверный никнейм.",
    BotProfileErrorCategory.INVALID_AVATAR: (
        "Некорректный avatar. Разрешены PNG/JPEG установленного размера."
    ),
    BotProfileErrorCategory.DISCORD_FORBIDDEN: (
        "Discord не разрешил изменить профиль бота."
    ),
    BotProfileErrorCategory.DISCORD_API_FAILURE: (
        "Discord не применил изменение. Повторите попытку позже."
    ),
    BotProfileErrorCategory.TIMEOUT: "Control service не ответил вовремя.",
    BotProfileErrorCategory.MALFORMED_RESPONSE: (
        "Control service вернул некорректный ответ."
    ),
}


def render_bot_profile_page(
    profile: BotGuildProfile | None,
    *,
    csrf_token: str,
    role: WebAdminRole,
    result: str | None = None,
    error: BotProfileErrorCategory | None = None,
) -> str:
    result_message = _RESULT_MESSAGES.get(result or "")
    notices = ""
    if result_message:
        notices += f'<section class="notice success">{escape(result_message)}</section>'
    if error is not None:
        notices += (
            '<section class="notice failure">'
            f"{escape(_ERROR_MESSAGES.get(error, 'Операция не выполнена.'))}"
            "</section>"
        )

    if profile is None:
        profile_content = (
            '<section class="card"><p>Профиль бота сейчас недоступен.</p></section>'
        )
        controls = ""
    else:
        avatar_url = profile.display_avatar_url
        avatar = (
            f'<img class="avatar" src="{escape(avatar_url, quote=True)}" '
            'alt="Текущий avatar бота">'
            if avatar_url
            else '<div class="avatar placeholder">Нет avatar</div>'
        )
        guild_avatar_state = (
            "Установлен отдельный серверный avatar"
            if profile.guild_avatar_url
            else "Используется глобальный avatar"
        )
        profile_content = f"""
        <section class="profile-grid">
          <div>{avatar}</div>
          <div class="card">
            <dl>
              <dt>Отображаемое имя</dt><dd>{escape(profile.display_name)}</dd>
              <dt>Серверный никнейм</dt><dd>{escape(profile.nickname or "Не задан")}</dd>
              <dt>Discord user/application name</dt><dd>{escape(profile.application_name)}</dd>
              <dt>Avatar</dt><dd>{escape(guild_avatar_state)}</dd>
            </dl>
          </div>
        </section>"""
        escaped_csrf = escape(csrf_token, quote=True)
        controls = f"""
        <h2>Серверный никнейм</h2>
        <form method="post" action="/admin/settings/bot-profile/nickname" class="card form">
          <input type="hidden" name="csrf_token" value="{escaped_csrf}">
          <label for="nickname">Новый никнейм</label>
          <input id="nickname" name="nickname" required maxlength="{BOT_NICKNAME_MAX_LENGTH}">
          <button type="submit">Сохранить никнейм</button>
        </form>
        <form method="post" action="/admin/settings/bot-profile/nickname/reset" class="reset-form">
          <input type="hidden" name="csrf_token" value="{escaped_csrf}">
          <button class="danger" type="submit">Сбросить серверный никнейм</button>
        </form>

        <h2>Серверный avatar</h2>
        <form method="post" action="/admin/settings/bot-profile/avatar" enctype="multipart/form-data" class="card form">
          <input type="hidden" name="csrf_token" value="{escaped_csrf}">
          <label for="avatar">PNG или JPEG, не более {BOT_AVATAR_MAX_BYTES // (1024 * 1024)} MiB</label>
          <input id="avatar" type="file" name="avatar" accept="image/png,image/jpeg" required>
          <button type="submit">Загрузить avatar</button>
        </form>
        <form method="post" action="/admin/settings/bot-profile/avatar/reset" class="reset-form">
          <input type="hidden" name="csrf_token" value="{escaped_csrf}">
          <button class="danger" type="submit">Сбросить серверный avatar</button>
        </form>"""

    body = f"""{notices}
  {profile_content}
  {controls}"""
    return render_admin_page(
        "Профиль бота",
        body,
        role=role,
        csrf_token=csrf_token,
        active_path="/admin/settings/bot-profile",
        description="Изменения применяются только к профилю бота на настроенном Discord-сервере",
        kicker="Server identity",
    )


def _csrf_rejected() -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        "<title>Запрос отклонён — Kanami Admin</title></head><body>"
        "<h1>Запрос отклонён</h1></body></html>",
        status_code=403,
        headers=FORM_RESPONSE_HEADERS,
    )


def _neutral_write_denied(status_code: int = 403) -> HTMLResponse:
    return HTMLResponse(
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        "<title>Request denied — Kanami Admin</title></head><body>"
        "<h1>Request denied</h1></body></html>",
        status_code=status_code,
        headers=FORM_RESPONSE_HEADERS,
    )


async def _authorize_write(request: Request, session: WebSession) -> Response | None:
    """Re-check access only after session and CSRF validation, before control I/O."""

    try:
        decision: WebAdminAuthorizationDecision = (
            await request.state.admin_authorizer.authorize(session.discord_user_id)
        )
    except Exception as error:
        logger.warning(
            "web_admin_write_authorization_denied discord_user_id=%s "
            "category=unavailable exception_type=%s",
            session.discord_user_id,
            type(error).__name__,
        )
    else:
        if decision.allowed:
            limiter: WebWriteRateLimiter = request.state.web_write_limiter
            if limiter.allow(request.state.web_session_id):
                return None
            logger.warning(
                "web_admin_write_rate_limited discord_user_id=%s",
                session.discord_user_id,
            )
            return _neutral_write_denied(status_code=429)
        logger.warning(
            "web_admin_write_authorization_denied discord_user_id=%s category=%s",
            session.discord_user_id,
            decision.category or "denied",
        )

    sessions: WebSessionStore = request.state.web_sessions
    sessions.revoke(request.state.web_session_id)
    response = _neutral_write_denied()
    settings = request.state.web_settings
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        secure=settings.web_admin_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


async def _read_urlencoded_form(request: Request) -> dict[str, list[str]] | None:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
        "application/x-www-form-urlencoded"
    ):
        return None
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > URLENCODED_FORM_MAX_BYTES:
                return None
        except ValueError:
            return None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > URLENCODED_FORM_MAX_BYTES:
            return None
    try:
        return parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return None


def _valid_csrf(values: dict[str, list[str]] | None, session: WebSession) -> bool:
    if values is None:
        return False
    tokens = values.get("csrf_token", [])
    return len(tokens) == 1 and constant_time_token_equal(tokens[0], session.csrf_token)


def _redirect(
    *, result: str | None = None, error: BotProfileErrorCategory | None = None
) -> RedirectResponse:
    parameters = {}
    if result is not None:
        parameters["result"] = result
    if error is not None:
        parameters["error"] = error.value
    location = "/admin/settings/bot-profile"
    if parameters:
        location = f"{location}?{urlencode(parameters)}"
    return RedirectResponse(location, status_code=303, headers=FORM_RESPONSE_HEADERS)


def _log_outcome(
    *, actor: int, operation: str, error: BotProfileErrorCategory | None = None
) -> None:
    if error is None:
        logger.info(
            "web_admin_bot_profile_succeeded discord_user_id=%s operation=%s",
            actor,
            operation,
        )
    else:
        logger.warning(
            "web_admin_bot_profile_failed discord_user_id=%s operation=%s category=%s",
            actor,
            operation,
            error,
        )


async def admin_bot_profile(request: Request) -> HTMLResponse:
    control: BotProfileControl = request.state.bot_profile_control
    profile = None
    error = None
    try:
        profile = await control.get_profile()
    except BotProfileOperationError as operation_error:
        error = operation_error.category
    query_error = request.query_params.get("error")
    if query_error:
        try:
            error = BotProfileErrorCategory(query_error)
        except ValueError:
            pass
    session: WebSession = request.state.web_session
    return HTMLResponse(
        render_bot_profile_page(
            profile,
            csrf_token=session.csrf_token,
            role=session.role,
            result=request.query_params.get("result"),
            error=error,
        ),
        headers=FORM_RESPONSE_HEADERS,
    )


async def admin_bot_profile_nickname(request: Request) -> Response:
    session: WebSession = request.state.web_session
    values = await _read_urlencoded_form(request)
    if not _valid_csrf(values, session):
        return _csrf_rejected()
    nicknames = values.get("nickname", []) if values else []
    try:
        if len(nicknames) != 1:
            raise BotProfileOperationError(BotProfileErrorCategory.INVALID_NICKNAME)
        nickname = normalize_bot_nickname(nicknames[0])
        denied = await _authorize_write(request, session)
        if denied is not None:
            return denied
        control: BotProfileControl = request.state.bot_profile_control
        await control.update_nickname(
            nickname,
            actor_discord_user_id=session.discord_user_id,
        )
    except BotProfileOperationError as error:
        _log_outcome(
            actor=session.discord_user_id,
            operation="nickname_update",
            error=error.category,
        )
        return _redirect(error=error.category)
    _log_outcome(actor=session.discord_user_id, operation="nickname_update")
    return _redirect(result="nickname_updated")


async def _reset(
    request: Request,
    *,
    operation: str,
    result: str,
) -> Response:
    session: WebSession = request.state.web_session
    values = await _read_urlencoded_form(request)
    if not _valid_csrf(values, session):
        return _csrf_rejected()
    denied = await _authorize_write(request, session)
    if denied is not None:
        return denied
    control: BotProfileControl = request.state.bot_profile_control
    try:
        if operation == "nickname_reset":
            await control.reset_nickname(actor_discord_user_id=session.discord_user_id)
        else:
            await control.reset_avatar(actor_discord_user_id=session.discord_user_id)
    except BotProfileOperationError as error:
        _log_outcome(
            actor=session.discord_user_id,
            operation=operation,
            error=error.category,
        )
        return _redirect(error=error.category)
    _log_outcome(actor=session.discord_user_id, operation=operation)
    return _redirect(result=result)


async def admin_bot_profile_nickname_reset(request: Request) -> Response:
    return await _reset(
        request,
        operation="nickname_reset",
        result="nickname_reset",
    )


async def admin_bot_profile_avatar(request: Request) -> Response:
    session: WebSession = request.state.web_session
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
            if declared_length < 0 or declared_length > AVATAR_REQUEST_MAX_BYTES:
                return _redirect(error=BotProfileErrorCategory.INVALID_AVATAR)
        except ValueError:
            return _redirect(error=BotProfileErrorCategory.INVALID_AVATAR)
    try:
        bounded_request = Request(
            request.scope,
            receive=_bounded_receive(request, limit=AVATAR_REQUEST_MAX_BYTES),
        )
        async with bounded_request.form(
            max_files=1,
            max_fields=1,
            max_part_size=MULTIPART_FIELD_MAX_BYTES,
        ) as form:
            csrf_value = form.get("csrf_token")
            if not isinstance(csrf_value, str) or not constant_time_token_equal(
                csrf_value, session.csrf_token
            ):
                return _csrf_rejected()
            upload = form.get("avatar")
            if not isinstance(upload, UploadFile):
                raise BotProfileOperationError(BotProfileErrorCategory.INVALID_AVATAR)
            avatar = await upload.read(BOT_AVATAR_MAX_BYTES + 1)
            content_type = validate_bot_avatar(avatar, upload.content_type or "")
    except (AvatarRequestTooLarge, BotProfileOperationError, HTTPException) as error:
        category = (
            error.category
            if isinstance(error, BotProfileOperationError)
            else BotProfileErrorCategory.INVALID_AVATAR
        )
        _log_outcome(
            actor=session.discord_user_id,
            operation="avatar_update",
            error=category,
        )
        return _redirect(error=category)
    except Exception as error:
        logger.exception(
            "web_admin_bot_profile_failed discord_user_id=%s "
            "operation=avatar_update category=internal_error exception_type=%s",
            session.discord_user_id,
            type(error).__name__,
        )
        return _redirect(error=BotProfileErrorCategory.INVALID_AVATAR)
    denied = await _authorize_write(request, session)
    if denied is not None:
        return denied
    control: BotProfileControl = request.state.bot_profile_control
    try:
        await control.update_avatar(
            avatar,
            content_type,
            actor_discord_user_id=session.discord_user_id,
        )
    except BotProfileOperationError as error:
        _log_outcome(
            actor=session.discord_user_id,
            operation="avatar_update",
            error=error.category,
        )
        return _redirect(error=error.category)
    _log_outcome(actor=session.discord_user_id, operation="avatar_update")
    return _redirect(result="avatar_updated")


async def admin_bot_profile_avatar_reset(request: Request) -> Response:
    return await _reset(
        request,
        operation="avatar_reset",
        result="avatar_reset",
    )
