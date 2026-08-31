"""Starlette application factory for the standalone web admin."""

import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from html import escape
from typing import Protocol
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo

import aiohttp
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from discord_stats_bot.common.formatting import format_voice_duration
from discord_stats_bot.config import WebSettings
from discord_stats_bot.features.server_settings import GuildServerSettingsBaselines
from discord_stats_bot.persistence import DatabaseResources, create_database_resources
from discord_stats_bot.web.administrators import (
    AdministratorService,
    WebAdminAdministratorService,
    admin_administrators,
    admin_administrators_grant,
    admin_administrators_revoke,
)
from discord_stats_bot.web.analytics import WebAdminAnalyticsService, admin_analytics
from discord_stats_bot.web.audit_log import (
    AuditLogService,
    WebAdminAuditLogService,
    admin_audit_log,
)
from discord_stats_bot.web.auth import (
    OAUTH_COOKIE_NAME,
    OAUTH_COOKIE_PATH,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    AdminAuthenticationMiddleware,
    AiohttpOAuthHttpClient,
    DiscordOAuthClient,
    DiscordOAuthError,
    OAuthIdentityProvider,
    OAuthTransactionStore,
    StoreCapacityError,
    WebSession,
    WebSessionStore,
    constant_time_token_equal,
)
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationDecision,
    WebAdminAuthorizationService,
    WebAdminRole,
)
from discord_stats_bot.web.avatars import discord_member_avatar_url
from discord_stats_bot.web.bot_control import (
    AiohttpBotProfileControlClient,
    BotProfileControl,
    DisabledBotProfileControl,
)
from discord_stats_bot.web.bot_profile import (
    admin_bot_profile,
    admin_bot_profile_avatar,
    admin_bot_profile_avatar_reset,
    admin_bot_profile_nickname,
    admin_bot_profile_nickname_reset,
)
from discord_stats_bot.web.dashboard import (
    WebAdminDashboard,
    WebAdminDashboardService,
    render_dashboard_page,
)
from discord_stats_bot.web.game_analytics import (
    WebAdminGameAnalyticsService,
    parse_game_analytics_period,
    render_member_games_section,
)
from discord_stats_bot.web.member_analytics import (
    WebAdminMemberAnalyticsService,
    parse_member_analytics_period,
    render_member_activity_section,
)
from discord_stats_bot.web.operations import (
    SqlAlchemyOperationsRepository,
    SubprocessGitMetadataSource,
    WebAdminSystemStatus,
    WebAdminSystemStatusService,
    render_system_status_page,
)
from discord_stats_bot.web.presentation import render_admin_page
from discord_stats_bot.web.rules import (
    RulesManagement,
    WebAdminRulesService,
    admin_rules,
    admin_rules_acceptances,
    admin_rules_create,
    admin_rules_delete,
    admin_rules_detail,
    admin_rules_preview,
    admin_rules_publication_channel,
    admin_rules_publication_disable,
    admin_rules_publication_sync,
    admin_rules_publish,
    admin_rules_save,
)
from discord_stats_bot.web.security import (
    AdminSecurityHeadersMiddleware,
    WebWriteRateLimiter,
)
from discord_stats_bot.web.server_game_analytics import (
    WebAdminServerGameAnalyticsService,
    admin_games,
)
from discord_stats_bot.web.server_settings import WebAdminServerSettingsReadService
from discord_stats_bot.web.server_settings_page import (
    admin_server_settings,
    admin_server_settings_update,
)
from discord_stats_bot.web.service import (
    MAX_POSTGRESQL_BIGINT,
    AdminCounts,
    AdminMemberDetail,
    AdminMemberDetailResult,
    AdminMemberDetailStatus,
    AdminMemberOrder,
    AdminMemberSort,
    AdminMembersPage,
    WebAdminManagedAccessRepository,
    WebAdminMembershipRepository,
    WebAdminService,
    WebDatabaseHealth,
)


class AdminService(Protocol):
    async def probe_database(self) -> WebDatabaseHealth: ...

    async def load_counts(self) -> AdminCounts | None: ...

    async def load_members(
        self,
        *,
        page: int,
        query: str,
        sort: AdminMemberSort,
        order: AdminMemberOrder,
    ) -> AdminMembersPage | None: ...

    async def load_member_detail(self, user_id: int) -> AdminMemberDetailResult: ...


class AdminAuthorizer(Protocol):
    async def authorize(
        self, discord_user_id: int
    ) -> WebAdminAuthorizationDecision: ...


class DashboardService(Protocol):
    async def load(self) -> WebAdminDashboard: ...


class SystemStatusService(Protocol):
    async def load(self) -> WebAdminSystemStatus: ...


ResourceFactory = Callable[..., DatabaseResources]
ServiceFactory = Callable[[object], AdminService]
AuthorizationServiceFactory = Callable[[object, WebSettings], AdminAuthorizer]
AdministratorServiceFactory = Callable[[object, WebSettings], AdministratorService]
AuditLogServiceFactory = Callable[[object, WebSettings], AuditLogService]
ServerSettingsReadServiceFactory = Callable[
    [object, WebSettings], WebAdminServerSettingsReadService
]
OAuthClientFactory = Callable[
    [aiohttp.ClientSession, WebSettings], OAuthIdentityProvider
]
BotProfileControlFactory = Callable[
    [aiohttp.ClientSession, WebSettings], BotProfileControl
]
DashboardServiceFactory = Callable[
    [object, WebSettings, AdminService, BotProfileControl], DashboardService
]
SystemStatusServiceFactory = Callable[
    [object, WebSettings, BotProfileControl, float], SystemStatusService
]
RulesServiceFactory = Callable[[object, WebSettings], RulesManagement]
AnalyticsServiceFactory = Callable[[object, WebSettings], WebAdminAnalyticsService]
MemberAnalyticsServiceFactory = Callable[
    [object, WebSettings], WebAdminMemberAnalyticsService
]
GameAnalyticsServiceFactory = Callable[
    [object, WebSettings], WebAdminGameAnalyticsService
]
ServerGameAnalyticsServiceFactory = Callable[
    [object, WebSettings], WebAdminServerGameAnalyticsService
]

logger = logging.getLogger(__name__)
AUTH_RESPONSE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}
MAX_LOGOUT_BODY_BYTES = 4_096


def _default_oauth_client_factory(
    http_session: aiohttp.ClientSession,
    settings: WebSettings,
) -> OAuthIdentityProvider:
    return DiscordOAuthClient(
        AiohttpOAuthHttpClient(http_session),
        client_id=settings.web_admin_discord_client_id,
        client_secret=settings.web_admin_discord_client_secret,
        redirect_uri=settings.web_admin_discord_redirect_uri,
    )


def _default_bot_profile_control_factory(
    http_session: aiohttp.ClientSession,
    settings: WebSettings,
) -> BotProfileControl:
    if (
        settings.web_admin_bot_control_url is None
        or settings.web_admin_bot_control_shared_secret is None
    ):
        return DisabledBotProfileControl()
    return AiohttpBotProfileControlClient(
        http_session,
        base_url=settings.web_admin_bot_control_url,
        shared_secret=settings.web_admin_bot_control_shared_secret,
    )


def _web_session(request: Request) -> WebSession:
    return request.state.web_session


def _parse_page(value: str | None) -> int:
    try:
        return max(1, int(value or "1"))
    except ValueError:
        return 1


def _format_joined_at(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")


def _parse_member_sort(value: str | None) -> AdminMemberSort:
    try:
        return AdminMemberSort(value or AdminMemberSort.NAME)
    except ValueError:
        return AdminMemberSort.NAME


def _parse_member_order(value: str | None) -> AdminMemberOrder:
    try:
        return AdminMemberOrder(value or AdminMemberOrder.ASC)
    except ValueError:
        return AdminMemberOrder.ASC


def _members_url(
    page: int,
    query: str,
    sort: AdminMemberSort,
    order: AdminMemberOrder,
) -> str:
    parameters: dict[str, str | int] = {
        "page": page,
        "sort": sort.value,
        "order": order.value,
    }
    if query:
        parameters["q"] = query
    return f"/admin/members?{urlencode(parameters)}"


def _member_url(user_id: int) -> str:
    if user_id <= 0 or user_id > MAX_POSTGRESQL_BIGINT:
        raise ValueError("user_id must fit a positive PostgreSQL BIGINT")
    return f"/admin/members/{user_id}"


def _format_count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _member_count_label(value: int) -> str:
    remainder_100 = value % 100
    remainder_10 = value % 10
    if 11 <= remainder_100 <= 14:
        word = "участников"
    elif remainder_10 == 1:
        word = "участник"
    elif 2 <= remainder_10 <= 4:
        word = "участника"
    else:
        word = "участников"
    return f"{_format_count(value)} {word}"


def _member_monogram(display_name: str) -> str:
    words = tuple(part for part in re.split(r"\s+", display_name.strip()) if part)
    initials = [
        character
        for word in words[:2]
        if (character := next((item for item in word if item.isalnum()), ""))
    ]
    if len(initials) == 1:
        initials = [item for item in words[0] if item.isalnum()][:2]
    return "".join(initials).upper()[:2] or "?"


def _member_username(username: str | None) -> str:
    if not username:
        return ""
    return f'<span class="member-username">@{escape(username)}</span>'


def _member_avatar(
    *,
    guild_id: int,
    user_id: int,
    display_name: str,
    avatar_hash: str | None,
    guild_avatar_hash: str | None,
    profile: bool = False,
) -> str:
    monogram = escape(_member_monogram(display_name))
    monogram_class = "profile-monogram" if profile else "member-monogram"
    url = discord_member_avatar_url(
        guild_id=guild_id,
        user_id=user_id,
        guild_avatar_hash=guild_avatar_hash,
        avatar_hash=avatar_hash,
        size=256 if profile else 64,
    )
    if url is None:
        return f'<div class="{monogram_class}" aria-hidden="true">{monogram}</div>'
    frame_class = "profile-avatar" if profile else "member-avatar"
    dimensions = 'width="96" height="96"' if profile else 'width="46" height="46"'
    loading = "" if profile else ' loading="lazy"'
    return (
        f'<div class="{frame_class}">'
        f'<div class="{monogram_class}" aria-hidden="true">{monogram}</div>'
        f'<img class="member-avatar-image" src="{escape(url, quote=True)}" alt="" '
        f'{dimensions}{loading} decoding="async"></div>'
    )


def _member_sort_options(selected: AdminMemberSort) -> str:
    labels = {
        AdminMemberSort.NAME: "Имя",
        AdminMemberSort.JOINED: "Дата вступления",
        AdminMemberSort.VOICE: "Voice lifetime",
        AdminMemberSort.MESSAGES: "Сообщения",
        AdminMemberSort.ACHIEVEMENTS: "Достижения",
    }
    return "".join(
        f'<option value="{value.value}"'
        f"{' selected' if value is selected else ''}>{label}</option>"
        for value, label in labels.items()
    )


def _member_order_options(selected: AdminMemberOrder) -> str:
    labels = {
        AdminMemberOrder.ASC: "По возрастанию",
        AdminMemberOrder.DESC: "По убыванию",
    }
    return "".join(
        f'<option value="{value.value}"'
        f"{' selected' if value is selected else ''}>{label}</option>"
        for value, label in labels.items()
    )


def _render_members_page(
    result: AdminMembersPage | None,
    *,
    csrf_token: str,
    role: WebAdminRole,
) -> str:
    if result is None:
        content = (
            '<section class="status unhealthy">'
            "<strong>PostgreSQL недоступна</strong><br>"
            '<span class="muted">Не удалось загрузить участников.</span>'
            "</section>"
        )
        summary = ""
        pagination = ""
        query = ""
        sort = AdminMemberSort.NAME
        order = AdminMemberOrder.ASC
    else:
        query = result.query
        sort = result.sort
        order = result.order
        summary = (
            '<div class="member-directory-summary">'
            f"<strong>{_member_count_label(result.total)}</strong>"
            f"<span>Страница {result.page} из {result.total_pages}</span></div>"
        )
        if not result.entries:
            content = (
                '<section class="empty member-directory-empty">'
                "<strong>Участники не найдены</strong>"
                "<span>Попробуйте изменить поисковый запрос.</span></section>"
            )
        else:
            records = "".join(
                '<article class="member-row">'
                f"{_member_avatar(guild_id=member.guild_id, user_id=member.user_id, display_name=member.display_name, avatar_hash=member.avatar_hash, guild_avatar_hash=member.guild_avatar_hash)}"
                '<div class="member-primary">'
                f'<h2><a href="{_member_url(member.user_id)}">{escape(member.display_name)}</a></h2>'
                f"{_member_username(member.username)}"
                f'<code class="member-id">Discord ID · {member.user_id}</code></div>'
                '<dl class="member-stats">'
                f"<div><dt>Вступление</dt><dd>{escape(_format_joined_at(member.joined_at))}</dd></div>"
                f"<div><dt>Voice</dt><dd>{escape(format_voice_duration(member.voice_seconds))}</dd></div>"
                f"<div><dt>Сообщения</dt><dd>{_format_count(member.message_count)}</dd></div>"
                f"<div><dt>Достижения</dt><dd>{_format_count(member.achievement_count)}</dd></div>"
                "</dl>"
                f'<a class="button secondary member-profile-action" href="{_member_url(member.user_id)}">Профиль</a>'
                "</article>"
                for member in result.entries
            )
            content = f'<section class="member-directory-list" aria-label="Участники">{records}</section>'
        if result.total:
            previous = (
                f'<a class="button secondary" href="{escape(_members_url(result.page - 1, query, sort, order))}">← Назад</a>'
                if result.page > 1
                else '<span class="button secondary disabled" aria-disabled="true">← Назад</span>'
            )
            following = (
                f'<a class="button secondary" href="{escape(_members_url(result.page + 1, query, sort, order))}">Далее →</a>'
                if result.page < result.total_pages
                else '<span class="button secondary disabled" aria-disabled="true">Далее →</span>'
            )
            pagination = (
                '<nav class="pagination member-pagination" aria-label="Страницы участников">'
                f'{previous}<span class="pagination-position">{result.page} / {result.total_pages}</span>'
                f"{following}</nav>"
            )
        else:
            pagination = ""

    reset = (
        '<a class="button ghost member-search-reset" href="/admin/members">Сбросить</a>'
        if query
        else ""
    )
    body = f"""{summary}
    <form class="member-toolbar" method="get" action="/admin/members">
      <label class="member-search-field"><span>Поиск</span>
        <input name="q" value="{escape(query, quote=True)}" placeholder="Имя, username или Discord ID">
      </label>
      <label><span>Сортировка</span><select name="sort">{_member_sort_options(sort)}</select></label>
      <label><span>Порядок</span><select name="order">{_member_order_options(order)}</select></label>
      <div class="member-toolbar-actions"><button type="submit">Найти</button>{reset}</div>
    </form>
    {content}
    {pagination}"""
    return render_admin_page(
        "Участники",
        body,
        role=role,
        csrf_token=csrf_token,
        active_path="/admin/members",
        description="Текущие участники и сохранённая lifetime-статистика",
        wide=True,
        kicker="Members",
    )


def _optional_text(value: str | None) -> str:
    return escape(value) if value else "—"


def _achievement_tier(value: str | None) -> str:
    if value is None:
        return "Без уровня"
    return {
        "bronze": "Бронза",
        "silver": "Серебро",
        "gold": "Золото",
    }.get(value.casefold(), value)


def _lifecycle_label(event_type: str) -> str:
    return {
        "member.joined": "Вступил на сервер",
        "member.left": "Покинул сервер",
        "member.returned": "Вернулся на сервер",
    }.get(event_type, "Событие участника")


def _render_member_detail_page(
    detail: AdminMemberDetail,
    *,
    activity_section: str,
    games_section: str,
    csrf_token: str,
    role: WebAdminRole,
) -> str:
    status = "На сервере" if detail.left_at is None else "Покинул сервер"
    status_class = "active" if detail.left_at is None else "departed"
    if detail.achievements:
        achievement_rows = "".join(
            '<li class="achievement-card"><span class="achievement-mark" aria-hidden="true"></span>'
            '<div class="achievement-copy">'
            f"<h3>{escape(item.title or 'Архивное достижение')}</h3>"
            f'<span class="badge accent">{escape(_achievement_tier(item.tier))}</span>'
            f'<p>Получено <time datetime="{item.unlocked_at.isoformat()}">{escape(_format_joined_at(item.unlocked_at))}</time></p>'
            f'<code class="achievement-key">{escape(item.key)}</code></div></li>'
            for item in detail.achievements
        )
        achievements = f'<ul class="achievement-list">{achievement_rows}</ul>'
    else:
        achievements = '<p class="empty">Нет открытых достижений.</p>'

    if detail.lifecycle_events:
        lifecycle_rows = "".join(
            '<li class="lifecycle-event"><span class="timeline-marker" aria-hidden="true"></span>'
            '<div class="lifecycle-event-copy">'
            f'<time datetime="{event.occurred_at.isoformat()}">{escape(_format_joined_at(event.occurred_at))}</time>'
            f"<strong>{escape(_lifecycle_label(event.event_type))}</strong>"
            + (
                '<div class="lifecycle-details">'
                + "".join(
                    f"<span>{escape(part)}</span>"
                    for part in (
                        (
                            f"Отсутствовал: {format_voice_duration(event.absence_seconds)}"
                            if event.absence_seconds is not None
                            else ""
                        ),
                        (
                            f"Возвращение №{event.return_number}"
                            if event.return_number is not None
                            else ""
                        ),
                    )
                    if part
                )
                + "</div>"
                if event.absence_seconds is not None or event.return_number is not None
                else ""
            )
            + f'<code class="lifecycle-type">{escape(event.event_type)}</code></div></li>'
            for event in detail.lifecycle_events
        )
        lifecycle = f'<ol class="lifecycle-timeline">{lifecycle_rows}</ol>'
    else:
        lifecycle = '<p class="empty">История вступлений и выходов отсутствует.</p>'

    body = f"""<a class="back-link" href="/admin/members">← Участники</a>
    <section class="profile-hero">
      {_member_avatar(guild_id=detail.guild_id, user_id=detail.user_id, display_name=detail.display_name, avatar_hash=detail.avatar_hash, guild_avatar_hash=detail.guild_avatar_hash, profile=True)}
      <div class="profile-primary"><h2>{escape(detail.display_name)}</h2>
        {_member_username(detail.username)}
        <code>Discord ID · {detail.user_id}</code></div>
      <span class="badge {status_class} profile-status">{status}</span>
    </section>
    <section class="member-profile-section" aria-labelledby="identity-title">
      <div class="section-heading"><h2 id="identity-title">Identity</h2><p>Сохранённые Discord identity fields</p></div>
      <dl class="profile-identity">
        <div><dt>Username</dt><dd>{_optional_text(detail.username)}</dd></div>
        <div><dt>Global name</dt><dd>{_optional_text(detail.global_name)}</dd></div>
        <div><dt>Nickname</dt><dd>{_optional_text(detail.nickname)}</dd></div>
        <div><dt>Discord ID</dt><dd><code>{detail.user_id}</code></dd></div>
      </dl>
    </section>
    <section class="member-profile-section" aria-labelledby="membership-title">
      <div class="section-heading"><h2 id="membership-title">Membership</h2><p>Текущее persisted membership state</p></div>
      <dl class="profile-membership">
        <div><dt>Вступление</dt><dd>{escape(_format_joined_at(detail.joined_at))}</dd></div>
        <div><dt>Выход</dt><dd>{escape(_format_joined_at(detail.left_at))}</dd></div>
      </dl>
    </section>
    {activity_section}
    {games_section}
    <section class="member-profile-section" aria-labelledby="lifetime-title">
      <div class="section-heading"><h2 id="lifetime-title">Lifetime statistics</h2><p>Сохранённая активность Kanami</p></div>
      <div class="profile-stat-grid">
        <article><span>Voice lifetime</span><strong>{escape(format_voice_duration(detail.voice_seconds))}</strong></article>
        <article><span>Сообщения</span><strong>{_format_count(detail.message_count)}</strong></article>
        <article><span>Достижения</span><strong>{_format_count(detail.achievement_count)}</strong></article>
      </div>
    </section>
    <section class="member-profile-section" aria-labelledby="achievements-title">
      <div class="section-heading"><h2 id="achievements-title">Достижения</h2><p>Полученные награды</p></div>{achievements}
    </section>
    <section class="member-profile-section" aria-labelledby="lifecycle-title">
      <div class="section-heading"><h2 id="lifecycle-title">История участия</h2><p>Последние 20 lifecycle events</p></div>{lifecycle}
    </section>"""
    return render_admin_page(
        detail.display_name,
        body,
        role=role,
        csrf_token=csrf_token,
        active_path="/admin/members",
        description="Профиль участника и lifetime-данные Kanami",
        kicker="Member profile",
    )


def _render_member_detail_error(
    *, not_found: bool, csrf_token: str, role: WebAdminRole
) -> str:
    title = "Участник не найден" if not_found else "PostgreSQL недоступна"
    message = (
        "В настроенном сервере нет такого участника."
        if not_found
        else "Не удалось загрузить профиль участника."
    )
    return render_admin_page(
        title,
        f'<a class="back-link" href="/admin/members">← Участники</a>'
        f'<section class="notice {"warning" if not_found else "failure"}">{message}</section>',
        role=role,
        csrf_token=csrf_token,
        active_path="/admin/members",
        description="Профиль участника недоступен",
        kicker="Members",
    )


async def admin_home(request: Request) -> HTMLResponse:
    service: DashboardService = request.state.dashboard_service
    dashboard = await service.load()
    session = _web_session(request)
    return HTMLResponse(
        render_dashboard_page(
            dashboard,
            csrf_token=session.csrf_token,
            role=session.role,
        )
    )


async def admin_system(request: Request) -> HTMLResponse:
    service: SystemStatusService = request.state.system_status_service
    status = await service.load()
    session = _web_session(request)
    return HTMLResponse(
        render_system_status_page(
            status,
            csrf_token=session.csrf_token,
            role=session.role,
        )
    )


async def web_root(request: Request) -> RedirectResponse:
    del request
    return RedirectResponse("/admin/", status_code=303)


async def admin_health(request: Request) -> JSONResponse:
    service: AdminService = request.state.admin_service
    health = await service.probe_database()
    payload: dict[str, object] = {
        "status": "healthy" if health.available else "unhealthy"
    }
    return JSONResponse(payload, status_code=200 if health.available else 503)


async def admin_members(request: Request) -> HTMLResponse:
    service: AdminService = request.state.admin_service
    page = _parse_page(request.query_params.get("page"))
    query = request.query_params.get("q", "").strip()[:100]
    sort = _parse_member_sort(request.query_params.get("sort"))
    order = _parse_member_order(request.query_params.get("order"))
    result = await service.load_members(
        page=page,
        query=query,
        sort=sort,
        order=order,
    )
    session = _web_session(request)
    return HTMLResponse(
        _render_members_page(
            result,
            csrf_token=session.csrf_token,
            role=session.role,
        ),
        status_code=200 if result is not None else 503,
    )


async def admin_member_detail(request: Request) -> HTMLResponse:
    service: AdminService = request.state.admin_service
    user_id = int(request.path_params["discord_id"])
    session = _web_session(request)
    csrf_token = session.csrf_token
    if user_id <= 0 or user_id > MAX_POSTGRESQL_BIGINT:
        return HTMLResponse(
            _render_member_detail_error(
                not_found=True,
                csrf_token=csrf_token,
                role=session.role,
            ),
            status_code=404,
        )
    result = await service.load_member_detail(user_id)
    if result.status is AdminMemberDetailStatus.UNAVAILABLE:
        return HTMLResponse(
            _render_member_detail_error(
                not_found=False,
                csrf_token=csrf_token,
                role=session.role,
            ),
            status_code=503,
        )
    if result.status is AdminMemberDetailStatus.NOT_FOUND or result.detail is None:
        return HTMLResponse(
            _render_member_detail_error(
                not_found=True,
                csrf_token=csrf_token,
                role=session.role,
            ),
            status_code=404,
        )
    period = parse_member_analytics_period(request.query_params.getlist("period"))
    game_period = parse_game_analytics_period(
        request.query_params.getlist("game_period")
    )
    member_analytics_service: WebAdminMemberAnalyticsService = (
        request.state.member_analytics_service
    )
    member_analytics = await member_analytics_service.load(user_id, period)
    game_analytics_service: WebAdminGameAnalyticsService = (
        request.state.game_analytics_service
    )
    game_analytics = await game_analytics_service.load(user_id, game_period)
    return HTMLResponse(
        _render_member_detail_page(
            result.detail,
            activity_section=render_member_activity_section(
                member_analytics,
                user_id=user_id,
                selected_period=period,
                game_period=game_period.value,
            ),
            games_section=render_member_games_section(
                game_analytics,
                user_id=user_id,
                selected_period=game_period,
                activity_period=period.value,
            ),
            csrf_token=csrf_token,
            role=session.role,
        )
    )


def _delete_oauth_cookie(response: Response, settings: WebSettings) -> None:
    response.delete_cookie(
        OAUTH_COOKIE_NAME,
        path=OAUTH_COOKIE_PATH,
        secure=settings.web_admin_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _oauth_error_response(
    settings: WebSettings,
    *,
    status_code: int = 400,
) -> HTMLResponse:
    response = HTMLResponse(
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        "<title>Ошибка входа — Kanami Admin</title></head><body><main>"
        "<h1>Не удалось выполнить вход</h1>"
        '<p>Начните вход заново.</p><a href="/admin/login">Войти</a>'
        "</main></body></html>",
        status_code=status_code,
        headers=AUTH_RESPONSE_HEADERS,
    )
    _delete_oauth_cookie(response, settings)
    return response


def _authorization_denied_response(settings: WebSettings) -> HTMLResponse:
    response = HTMLResponse(
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        "<title>Доступ запрещён — Kanami Admin</title></head><body><main>"
        "<h1>Доступ к Kanami Admin запрещён</h1>"
        "</main></body></html>",
        status_code=403,
        headers=AUTH_RESPONSE_HEADERS,
    )
    _delete_oauth_cookie(response, settings)
    return response


async def admin_login(request: Request) -> RedirectResponse | HTMLResponse:
    settings: WebSettings = request.state.web_settings
    transactions: OAuthTransactionStore = request.state.oauth_transactions
    previous_state = request.cookies.get(OAUTH_COOKIE_NAME)
    try:
        transaction = transactions.begin(previous_state=previous_state)
    except StoreCapacityError:
        logger.warning("web_admin_oauth_failed category=transaction_store_full")
        return _oauth_error_response(settings, status_code=503)

    oauth_client: OAuthIdentityProvider = request.state.oauth_client
    authorize_url = oauth_client.build_authorize_url(
        state=transaction.state,
        code_challenge=transaction.code_challenge,
    )
    response = RedirectResponse(
        authorize_url,
        status_code=303,
        headers=AUTH_RESPONSE_HEADERS,
    )
    response.set_cookie(
        OAUTH_COOKIE_NAME,
        transaction.state,
        max_age=300,
        expires=transaction.expires_at,
        path=OAUTH_COOKIE_PATH,
        secure=settings.web_admin_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


async def admin_oauth_callback(request: Request) -> Response:
    settings: WebSettings = request.state.web_settings
    transactions: OAuthTransactionStore = request.state.oauth_transactions
    states = request.query_params.getlist("state")
    cookie_state = request.cookies.get(OAUTH_COOKIE_NAME)
    if len(states) != 1 or not states[0] or not cookie_state:
        logger.warning("web_admin_oauth_failed category=state_missing")
        return _oauth_error_response(settings)

    query_state = states[0]
    if not constant_time_token_equal(query_state, cookie_state):
        transactions.discard(cookie_state)
        logger.warning("web_admin_oauth_failed category=state_mismatch")
        return _oauth_error_response(settings)

    transaction = transactions.consume(query_state)
    if transaction is None:
        logger.warning("web_admin_oauth_failed category=state_expired_or_replayed")
        return _oauth_error_response(settings)

    if request.query_params.get("error") is not None:
        logger.warning("web_admin_oauth_failed category=discord_denied")
        return _oauth_error_response(settings)

    codes = request.query_params.getlist("code")
    if len(codes) != 1 or not codes[0] or len(codes[0]) > 2_048:
        logger.warning("web_admin_oauth_failed category=code_missing_or_invalid")
        return _oauth_error_response(settings)

    oauth_client: OAuthIdentityProvider = request.state.oauth_client
    try:
        identity = await oauth_client.authenticate(
            code=codes[0],
            code_verifier=transaction.code_verifier,
        )
        authorizer: AdminAuthorizer = request.state.admin_authorizer
        authorization = await authorizer.authorize(identity.user_id)
        if not authorization.allowed:
            logger.warning(
                "web_admin_authorization_denied discord_user_id=%s category=%s",
                identity.user_id,
                authorization.category,
            )
            return _authorization_denied_response(settings)
        assert authorization.role is not None
        logger.info(
            "web_admin_authorization_succeeded discord_user_id=%s",
            identity.user_id,
        )
        sessions: WebSessionStore = request.state.web_sessions
        issued = sessions.create(
            identity.user_id,
            role=authorization.role,
            previous_session_id=request.cookies.get(SESSION_COOKIE_NAME),
        )
    except DiscordOAuthError as error:
        logger.warning(
            "web_admin_oauth_failed category=%s upstream_status=%s",
            error.category,
            error.upstream_status,
        )
        return _oauth_error_response(settings, status_code=502)
    except StoreCapacityError:
        logger.warning("web_admin_oauth_failed category=session_store_full")
        return _oauth_error_response(settings, status_code=503)

    response = RedirectResponse(
        "/admin/",
        status_code=303,
        headers=AUTH_RESPONSE_HEADERS,
    )
    _delete_oauth_cookie(response, settings)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        issued.session_id,
        max_age=settings.web_admin_session_lifetime_seconds,
        expires=issued.session.expires_at,
        path=SESSION_COOKIE_PATH,
        secure=settings.web_admin_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    logger.info(
        "web_admin_oauth_succeeded discord_user_id=%s",
        identity.user_id,
    )
    return response


async def _read_logout_csrf_token(request: Request) -> str | None:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != (
        "application/x-www-form-urlencoded"
    ):
        return None
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_LOGOUT_BODY_BYTES:
                return None
        except ValueError:
            return None

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_LOGOUT_BODY_BYTES:
            return None
    try:
        values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return None
    csrf_values = values.get("csrf_token", [])
    return csrf_values[0] if len(csrf_values) == 1 else None


async def admin_logout(request: Request) -> Response:
    session = _web_session(request)
    csrf_token = await _read_logout_csrf_token(request)
    if csrf_token is None or not constant_time_token_equal(
        csrf_token, session.csrf_token
    ):
        return HTMLResponse(
            '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            "<title>Запрос отклонён — Kanami Admin</title></head><body>"
            "<h1>Запрос отклонён</h1></body></html>",
            status_code=403,
            headers=AUTH_RESPONSE_HEADERS,
        )

    sessions: WebSessionStore = request.state.web_sessions
    sessions.revoke(request.state.web_session_id)
    settings: WebSettings = request.state.web_settings
    response = RedirectResponse(
        "/admin/login",
        status_code=303,
        headers=AUTH_RESPONSE_HEADERS,
    )
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_PATH,
        secure=settings.web_admin_cookie_secure,
        httponly=True,
        samesite="lax",
    )
    logger.info(
        "web_admin_logout_succeeded discord_user_id=%s", session.discord_user_id
    )
    return response


def create_app(
    settings: WebSettings | None = None,
    *,
    resource_factory: ResourceFactory = create_database_resources,
    service_factory: ServiceFactory | None = None,
    oauth_client_factory: OAuthClientFactory = _default_oauth_client_factory,
    bot_profile_control_factory: BotProfileControlFactory = (
        _default_bot_profile_control_factory
    ),
    authorization_service_factory: AuthorizationServiceFactory | None = None,
    administrator_service_factory: AdministratorServiceFactory | None = None,
    audit_log_service_factory: AuditLogServiceFactory | None = None,
    server_settings_read_service_factory: (
        ServerSettingsReadServiceFactory | None
    ) = None,
    dashboard_service_factory: DashboardServiceFactory | None = None,
    system_status_service_factory: SystemStatusServiceFactory | None = None,
    rules_service_factory: RulesServiceFactory | None = None,
    analytics_service_factory: AnalyticsServiceFactory | None = None,
    member_analytics_service_factory: MemberAnalyticsServiceFactory | None = None,
    game_analytics_service_factory: GameAnalyticsServiceFactory | None = None,
    server_game_analytics_service_factory: (
        ServerGameAnalyticsServiceFactory | None
    ) = None,
    oauth_transaction_store: OAuthTransactionStore | None = None,
    web_session_store: WebSessionStore | None = None,
) -> Starlette:
    """Build an isolated ASGI app without importing or starting Discord runtime."""

    web_settings = settings or WebSettings()
    transactions = (
        oauth_transaction_store
        if oauth_transaction_store is not None
        else OAuthTransactionStore()
    )
    sessions = (
        web_session_store
        if web_session_store is not None
        else WebSessionStore(
            lifetime_seconds=web_settings.web_admin_session_lifetime_seconds
        )
    )
    write_limiter = WebWriteRateLimiter()
    process_started_monotonic = time.monotonic()

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[dict[str, object]]:
        del app
        resources = resource_factory(web_settings, read_only=False)
        try:
            managed_access_repository = WebAdminManagedAccessRepository(
                resources.session_factory,
                guild_id=web_settings.discord_guild_id,
            )
            membership_repository = WebAdminMembershipRepository(
                resources.session_factory,
                guild_id=web_settings.discord_guild_id,
            )
            service = (
                service_factory(resources.session_factory)
                if service_factory is not None
                else WebAdminService(
                    resources.session_factory,
                    guild_id=web_settings.discord_guild_id,
                    min_session_seconds=web_settings.voice_min_session_seconds,
                )
            )
            authorizer = (
                authorization_service_factory(
                    resources.session_factory,
                    web_settings,
                )
                if authorization_service_factory is not None
                else WebAdminAuthorizationService(
                    web_settings.web_admin_allowed_user_ids,
                    managed_access_repository,
                    membership_repository,
                )
            )
            administrator_service = (
                administrator_service_factory(
                    resources.session_factory,
                    web_settings,
                )
                if administrator_service_factory is not None
                else WebAdminAdministratorService(
                    web_settings.web_admin_allowed_user_ids,
                    managed_access_repository,
                    membership_repository,
                )
            )
            audit_log_service = (
                audit_log_service_factory(
                    resources.session_factory,
                    web_settings,
                )
                if audit_log_service_factory is not None
                else WebAdminAuditLogService(
                    resources.session_factory,
                    guild_id=web_settings.discord_guild_id,
                    report_timezone=ZoneInfo(
                        getattr(web_settings, "report_timezone", "UTC")
                    ),
                )
            )
            server_settings_read_service = (
                server_settings_read_service_factory(
                    resources.session_factory,
                    web_settings,
                )
                if server_settings_read_service_factory is not None
                else WebAdminServerSettingsReadService(
                    resources.session_factory,
                    guild_id=web_settings.discord_guild_id,
                    baselines=GuildServerSettingsBaselines(
                        autorole_role_id=web_settings.discord_autorole_id,
                        audit_log_channel_id=(
                            web_settings.discord_audit_log_channel_id
                        ),
                        anniversary_channel_id=(
                            web_settings.discord_anniversary_channel_id
                        ),
                        return_channel_id=web_settings.discord_return_channel_id,
                    ),
                )
            )
            timeout = aiohttp.ClientTimeout(total=8, connect=3, sock_read=5)
            async with aiohttp.ClientSession(timeout=timeout) as http_session:
                oauth_client = oauth_client_factory(http_session, web_settings)
                bot_profile_control = bot_profile_control_factory(
                    http_session,
                    web_settings,
                )
                dashboard_service = (
                    dashboard_service_factory(
                        resources.session_factory,
                        web_settings,
                        service,
                        bot_profile_control,
                    )
                    if dashboard_service_factory is not None
                    else WebAdminDashboardService(
                        resources.session_factory,
                        guild_id=web_settings.discord_guild_id,
                        report_timezone=ZoneInfo(web_settings.report_timezone),
                        min_session_seconds=(web_settings.voice_min_session_seconds),
                        database_probe=service,
                        bot_control=bot_profile_control,
                    )
                )
                system_status_service = (
                    system_status_service_factory(
                        resources.session_factory,
                        web_settings,
                        bot_profile_control,
                        process_started_monotonic,
                    )
                    if system_status_service_factory is not None
                    else WebAdminSystemStatusService(
                        SqlAlchemyOperationsRepository(
                            resources.session_factory,
                            guild_id=web_settings.discord_guild_id,
                        ),
                        bot_control=bot_profile_control,
                        git_metadata=SubprocessGitMetadataSource(),
                        game_tracking_enabled=web_settings.game_tracking_enabled,
                        voice_checkpoint_interval_seconds=(
                            web_settings.voice_checkpoint_interval_seconds
                        ),
                        game_confirm_interval_seconds=(
                            web_settings.game_confirm_interval_seconds
                        ),
                        process_started_monotonic=process_started_monotonic,
                    )
                )
                rules_service = (
                    rules_service_factory(resources.session_factory, web_settings)
                    if rules_service_factory is not None
                    else WebAdminRulesService(
                        resources.session_factory,
                        guild_id=web_settings.discord_guild_id,
                    )
                )
                analytics_service = (
                    analytics_service_factory(resources.session_factory, web_settings)
                    if analytics_service_factory is not None
                    else WebAdminAnalyticsService(
                        resources.session_factory,
                        guild_id=web_settings.discord_guild_id,
                        report_timezone=ZoneInfo(web_settings.report_timezone),
                        min_session_seconds=web_settings.voice_min_session_seconds,
                    )
                )
                member_analytics_service = (
                    member_analytics_service_factory(
                        resources.session_factory, web_settings
                    )
                    if member_analytics_service_factory is not None
                    else WebAdminMemberAnalyticsService(
                        resources.session_factory,
                        guild_id=web_settings.discord_guild_id,
                        report_timezone=ZoneInfo(web_settings.report_timezone),
                        min_session_seconds=web_settings.voice_min_session_seconds,
                    )
                )
                game_analytics_service = (
                    game_analytics_service_factory(
                        resources.session_factory, web_settings
                    )
                    if game_analytics_service_factory is not None
                    else WebAdminGameAnalyticsService(
                        resources.session_factory,
                        guild_id=web_settings.discord_guild_id,
                        report_timezone=ZoneInfo(web_settings.report_timezone),
                    )
                )
                server_game_analytics_service = (
                    server_game_analytics_service_factory(
                        resources.session_factory, web_settings
                    )
                    if server_game_analytics_service_factory is not None
                    else WebAdminServerGameAnalyticsService(
                        resources.session_factory,
                        guild_id=web_settings.discord_guild_id,
                        report_timezone=ZoneInfo(web_settings.report_timezone),
                    )
                )
                yield {
                    "admin_service": service,
                    "dashboard_service": dashboard_service,
                    "system_status_service": system_status_service,
                    "rules_service": rules_service,
                    "analytics_service": analytics_service,
                    "member_analytics_service": member_analytics_service,
                    "game_analytics_service": game_analytics_service,
                    "server_game_analytics_service": server_game_analytics_service,
                    "admin_authorizer": authorizer,
                    "administrator_service": administrator_service,
                    "audit_log_service": audit_log_service,
                    "server_settings_read_service": server_settings_read_service,
                    "oauth_client": oauth_client,
                    "bot_profile_control": bot_profile_control,
                    "oauth_transactions": transactions,
                    "web_sessions": sessions,
                    "web_write_limiter": write_limiter,
                    "web_settings": web_settings,
                }
        finally:
            await resources.dispose()

    app = Starlette(
        debug=False,
        routes=[
            Route("/", web_root, methods=["GET"], name="web-root"),
            Route("/admin/login", admin_login, methods=["GET"], name="admin-login"),
            Route(
                "/admin/auth/discord/callback",
                admin_oauth_callback,
                methods=["GET"],
                name="admin-oauth-callback",
            ),
            Route(
                "/admin/logout",
                admin_logout,
                methods=["POST"],
                name="admin-logout",
            ),
            Route("/admin/", admin_home, methods=["GET"], name="admin-home"),
            Route(
                "/admin/analytics",
                admin_analytics,
                methods=["GET"],
                name="admin-analytics",
            ),
            Route(
                "/admin/games",
                admin_games,
                methods=["GET"],
                name="admin-games",
            ),
            Route(
                "/admin/system",
                admin_system,
                methods=["GET"],
                name="admin-system",
            ),
            Route(
                "/admin/members",
                admin_members,
                methods=["GET"],
                name="admin-members",
            ),
            Route(
                "/admin/members/{discord_id:int}",
                admin_member_detail,
                methods=["GET"],
                name="admin-member-detail",
            ),
            Route(
                "/admin/administrators",
                admin_administrators,
                methods=["GET"],
                name="admin-administrators",
            ),
            Route(
                "/admin/administrators/grant",
                admin_administrators_grant,
                methods=["POST"],
                name="admin-administrators-grant",
            ),
            Route(
                "/admin/administrators/revoke",
                admin_administrators_revoke,
                methods=["POST"],
                name="admin-administrators-revoke",
            ),
            Route(
                "/admin/audit",
                admin_audit_log,
                methods=["GET"],
                name="admin-audit-log",
            ),
            Route("/admin/rules", admin_rules, methods=["GET"], name="admin-rules"),
            Route(
                "/admin/rules/drafts",
                admin_rules_create,
                methods=["POST"],
                name="admin-rules-create",
            ),
            Route(
                "/admin/rules/{ruleset_id:int}",
                admin_rules_detail,
                methods=["GET"],
                name="admin-rules-detail",
            ),
            Route(
                "/admin/rules/{ruleset_id:int}/save",
                admin_rules_save,
                methods=["POST"],
                name="admin-rules-save",
            ),
            Route(
                "/admin/rules/{ruleset_id:int}/preview",
                admin_rules_preview,
                methods=["POST"],
                name="admin-rules-preview",
            ),
            Route(
                "/admin/rules/{ruleset_id:int}/publish",
                admin_rules_publish,
                methods=["POST"],
                name="admin-rules-publish",
            ),
            Route(
                "/admin/rules/{ruleset_id:int}/delete",
                admin_rules_delete,
                methods=["POST"],
                name="admin-rules-delete",
            ),
            Route(
                "/admin/rules/{ruleset_id:int}/acceptances",
                admin_rules_acceptances,
                methods=["GET"],
                name="admin-rules-acceptances",
            ),
            Route(
                "/admin/rules/publication/channel",
                admin_rules_publication_channel,
                methods=["POST"],
                name="admin-rules-publication-channel",
            ),
            Route(
                "/admin/rules/publication/disable",
                admin_rules_publication_disable,
                methods=["POST"],
                name="admin-rules-publication-disable",
            ),
            Route(
                "/admin/rules/publication/sync",
                admin_rules_publication_sync,
                methods=["POST"],
                name="admin-rules-publication-sync",
            ),
            Route(
                "/admin/server-settings",
                admin_server_settings,
                methods=["GET"],
                name="admin-server-settings",
            ),
            Route(
                "/admin/server-settings",
                admin_server_settings_update,
                methods=["POST"],
                name="admin-server-settings-update",
            ),
            Route(
                "/admin/settings/bot-profile",
                admin_bot_profile,
                methods=["GET"],
                name="admin-bot-profile",
            ),
            Route(
                "/admin/settings/bot-profile/nickname",
                admin_bot_profile_nickname,
                methods=["POST"],
                name="admin-bot-profile-nickname",
            ),
            Route(
                "/admin/settings/bot-profile/nickname/reset",
                admin_bot_profile_nickname_reset,
                methods=["POST"],
                name="admin-bot-profile-nickname-reset",
            ),
            Route(
                "/admin/settings/bot-profile/avatar",
                admin_bot_profile_avatar,
                methods=["POST"],
                name="admin-bot-profile-avatar",
            ),
            Route(
                "/admin/settings/bot-profile/avatar/reset",
                admin_bot_profile_avatar_reset,
                methods=["POST"],
                name="admin-bot-profile-avatar-reset",
            ),
            Route(
                "/admin/health",
                admin_health,
                methods=["GET"],
                name="admin-health",
            ),
        ],
        lifespan=lifespan,
    )
    app.state.oauth_transaction_store = transactions
    app.state.web_session_store = sessions
    app.add_middleware(
        AdminAuthenticationMiddleware,
        session_store=sessions,
        cookie_secure=web_settings.web_admin_cookie_secure,
    )
    app.add_middleware(AdminSecurityHeadersMiddleware)
    return app
