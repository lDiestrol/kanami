"""OWNER/ADMIN Web Admin management for versioned guild rules."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from html import escape
from typing import Protocol
from urllib.parse import parse_qs, urlencode

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from discord_stats_bot.config import MAX_DISCORD_SNOWFLAKE
from discord_stats_bot.features.audit_logging import (
    AuditEventDraft,
    AuditLoggingService,
)
from discord_stats_bot.features.rules import (
    DuplicateRulesetVersionError,
    ImmutableRulesetError,
    NoPublishedRulesetError,
    RuleAcceptanceRecord,
    RulesComplianceAvailability,
    RulesComplianceService,
    RulesComplianceSummary,
    RulesetDiscordLimitError,
    RulesetNotFoundError,
    RulesetRecord,
    RulesetStatus,
    RulesetWithAcceptanceCount,
    RulesPublicationConfigurationStatus,
    RulesPublicationState,
    RulesService,
    validate_ruleset_for_discord,
)
from discord_stats_bot.features.server_settings import ServerSettingsChannelOption
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyAuditEventRepository,
    SqlAlchemyRulesPublicationRepository,
    SqlAlchemyRulesRepository,
)
from discord_stats_bot.web.auth import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    WebSession,
    WebSessionStore,
    constant_time_token_equal,
)
from discord_stats_bot.web.authorization import WebAdminRole
from discord_stats_bot.web.bot_control import RulesPublicationControlError
from discord_stats_bot.web.presentation import render_admin_page
from discord_stats_bot.web.security import WebWriteRateLimiter

logger = logging.getLogger(__name__)
RESPONSE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
FORM_MAX_BYTES = 256 * 1024
ALLOWED_ROLES = {WebAdminRole.OWNER, WebAdminRole.ADMIN}
DUPLICATE_VERSION_CONSTRAINT = "uq_rulesets_guild_version"


class RulesManagement(Protocol):
    async def load(self) -> tuple[RulesetWithAcceptanceCount, ...]: ...
    async def get(self, ruleset_id: int) -> RulesetRecord: ...
    async def acceptances(
        self, ruleset_id: int
    ) -> tuple[RuleAcceptanceRecord, ...]: ...
    async def create_draft(self, version: str, actor_user_id: int) -> RulesetRecord: ...
    async def update_draft(
        self, ruleset_id: int, actor_user_id: int, **values: object
    ) -> RulesetRecord: ...
    async def delete_draft(self, ruleset_id: int, actor_user_id: int) -> None: ...
    async def publish(self, ruleset_id: int, actor_user_id: int) -> RulesetRecord: ...
    async def load_publication_state(self) -> RulesPublicationState: ...
    async def load_compliance_summary(self) -> RulesComplianceSummary: ...


class WebAdminRulesService:
    """Own transactions around the shared Rules repository and audit outbox."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._clock = clock

    async def load(self) -> tuple[RulesetWithAcceptanceCount, ...]:
        async with self._session_factory() as session:
            return await RulesService(SqlAlchemyRulesRepository(session)).list_rulesets(
                self._guild_id
            )

    async def load_publication_state(self) -> RulesPublicationState:
        async with self._session_factory() as session:
            return await SqlAlchemyRulesPublicationRepository(session).get(
                self._guild_id
            )

    async def load_compliance_summary(self) -> RulesComplianceSummary:
        async with self._session_factory() as session:
            return await RulesComplianceService(
                SqlAlchemyRulesRepository(session)
            ).summarize(self._guild_id, now=self._clock())

    async def get(self, ruleset_id: int) -> RulesetRecord:
        async with self._session_factory() as session:
            return await RulesService(SqlAlchemyRulesRepository(session)).get_ruleset(
                self._guild_id, ruleset_id
            )

    async def acceptances(self, ruleset_id: int) -> tuple[RuleAcceptanceRecord, ...]:
        async with self._session_factory() as session:
            return await RulesService(
                SqlAlchemyRulesRepository(session)
            ).list_acceptances(self._guild_id, ruleset_id)

    async def create_draft(self, version: str, actor_user_id: int) -> RulesetRecord:
        async with self._session_factory.begin() as session:
            service = RulesService(SqlAlchemyRulesRepository(session))
            try:
                ruleset = await service.create_draft(
                    self._guild_id,
                    version=version,
                    actor_user_id=actor_user_id,
                    now=self._clock(),
                )
                await self._audit(
                    session, "rules.draft_created", ruleset, actor_user_id
                )
                return ruleset
            except IntegrityError as error:
                if _integrity_constraint_name(error) == DUPLICATE_VERSION_CONSTRAINT:
                    raise DuplicateRulesetVersionError from error
                raise

    async def update_draft(
        self, ruleset_id: int, actor_user_id: int, **values: object
    ) -> RulesetRecord:
        async with self._session_factory.begin() as session:
            service = RulesService(SqlAlchemyRulesRepository(session))
            before = await service.get_ruleset(self._guild_id, ruleset_id)
            try:
                ruleset = await service.update_draft(
                    self._guild_id, ruleset_id, **values
                )  # type: ignore[arg-type]
                await self._audit(
                    session,
                    "rules.draft_updated",
                    ruleset,
                    actor_user_id,
                    before={"version": before.version},
                )
                return ruleset
            except IntegrityError as error:
                if _integrity_constraint_name(error) == DUPLICATE_VERSION_CONSTRAINT:
                    raise DuplicateRulesetVersionError from error
                raise

    async def delete_draft(self, ruleset_id: int, actor_user_id: int) -> None:
        async with self._session_factory.begin() as session:
            service = RulesService(SqlAlchemyRulesRepository(session))
            ruleset = await service.delete_draft(self._guild_id, ruleset_id)
            await self._audit(session, "rules.draft_deleted", ruleset, actor_user_id)

    async def publish(self, ruleset_id: int, actor_user_id: int) -> RulesetRecord:
        async with self._session_factory.begin() as session:
            published, previous = await RulesService(
                SqlAlchemyRulesRepository(session)
            ).publish_draft(self._guild_id, ruleset_id, now=self._clock())
            await self._audit(
                session,
                "rules.published",
                published,
                actor_user_id,
                details={
                    "previous_published_version": previous.version if previous else None
                },
            )
            return published

    async def _audit(
        self,
        session: AsyncSession,
        event_type: str,
        ruleset: RulesetRecord,
        actor_user_id: int,
        *,
        before: dict[str, object] | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        await AuditLoggingService(SqlAlchemyAuditEventRepository(session)).create(
            AuditEventDraft(
                guild_id=self._guild_id,
                category="web_admin",
                event_type=event_type,
                occurred_at=self._clock(),
                subject_type="ruleset",
                subject_id=ruleset.id,
                actor_user_id=actor_user_id,
                before_data=before or {},
                after_data={"version": ruleset.version, "status": ruleset.status.value},
                details_data={
                    "guild_id": self._guild_id,
                    "version": ruleset.version,
                    **(details or {}),
                },
            )
        )


def _integrity_constraint_name(error: IntegrityError) -> str | None:
    """Extract the PostgreSQL constraint without classifying unrelated failures."""

    candidate: object | None = error.orig
    seen: set[int] = set()
    while candidate is not None and id(candidate) not in seen:
        seen.add(id(candidate))
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
        diag = getattr(candidate, "diag", None)
        name = getattr(diag, "constraint_name", None)
        if isinstance(name, str):
            return name
        candidate = getattr(candidate, "__cause__", None) or getattr(
            candidate, "__context__", None
        )
    return None


def _denied(status_code: int = 403) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><h1>Request denied</h1>",
        status_code=status_code,
        headers=RESPONSE_HEADERS,
    )


def _revoke_and_deny(request: Request) -> HTMLResponse:
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


async def _allowed(request: Request) -> bool:
    session: WebSession = request.state.web_session
    try:
        decision = await request.state.admin_authorizer.authorize(
            session.discord_user_id
        )
    except Exception as error:
        logger.warning(
            "web_admin_rules_authorization_failed actor=%s error_type=%s",
            session.discord_user_id,
            type(error).__name__,
        )
        return False
    return decision.allowed and decision.role in ALLOWED_ROLES


async def _read_form(request: Request) -> dict[str, str] | None:
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
        parsed = parse_qs(body.decode(), keep_blank_values=True)
    except UnicodeDecodeError:
        return None
    if any(len(values) != 1 for values in parsed.values()):
        return None
    return {key: values[0] for key, values in parsed.items()}


async def _write_form(
    request: Request, expected: set[str]
) -> dict[str, str] | Response:
    values = await _read_form(request)
    session: WebSession = request.state.web_session
    if (
        values is None
        or set(values) != expected
        or not constant_time_token_equal(
            values.get("csrf_token", ""), session.csrf_token
        )
    ):
        return _denied(400)
    if not await _allowed(request):
        return _revoke_and_deny(request)
    limiter: WebWriteRateLimiter = request.state.web_write_limiter
    if not limiter.allow(request.state.web_session_id):
        return _denied(429)
    return values


def _redirect(path: str = "/admin/rules", **parameters: str) -> RedirectResponse:
    if parameters:
        path += "?" + urlencode(parameters)
    return RedirectResponse(path, status_code=303, headers=RESPONSE_HEADERS)


def _date(value: datetime | None) -> str:
    return (
        "—" if value is None else value.astimezone(UTC).strftime("%d.%m.%Y %H:%M UTC")
    )


def _status(value: RulesetStatus) -> str:
    return {
        RulesetStatus.DRAFT: "Черновик",
        RulesetStatus.PUBLISHED: "Опубликовано",
        RulesetStatus.ARCHIVED: "Архив",
    }[value]


def _status_badge(value: RulesetStatus) -> str:
    return f'<span class="badge {value.value}">{_status(value)}</span>'


class WebRulesPublicationStatus(StrEnum):
    DISABLED = "disabled"
    UNPUBLISHED = "unpublished"
    CURRENT = "current"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class WebRulesPublicationView:
    state: RulesPublicationState
    status: WebRulesPublicationStatus
    current_version: str | None
    reflected_version: str | None
    channel_name: str | None
    channel_options: tuple[ServerSettingsChannelOption, ...]
    options_available: bool


def build_rules_publication_view(
    items: tuple[RulesetWithAcceptanceCount, ...],
    state: RulesPublicationState,
    channel_options: tuple[ServerSettingsChannelOption, ...],
    *,
    options_available: bool,
) -> WebRulesPublicationView:
    current = next(
        (
            item.ruleset
            for item in items
            if item.ruleset.status is RulesetStatus.PUBLISHED
        ),
        None,
    )
    by_id = {item.ruleset.id: item.ruleset for item in items}
    reflected = by_id.get(state.ruleset_id) if state.ruleset_id is not None else None
    channel_name = next(
        (option.name for option in channel_options if option.id == state.channel_id),
        None,
    )
    if state.channel_id is None:
        status = WebRulesPublicationStatus.DISABLED
    elif state.message_id is None:
        status = WebRulesPublicationStatus.UNPUBLISHED
    elif current is not None and state.ruleset_id == current.id:
        status = WebRulesPublicationStatus.CURRENT
    else:
        status = WebRulesPublicationStatus.STALE
    return WebRulesPublicationView(
        state,
        status,
        current.version if current is not None else None,
        reflected.version if reflected is not None else None,
        channel_name,
        channel_options,
        options_available,
    )


def _layout(title: str, body: str, session: WebSession) -> str:
    description = (
        "Версии, подтверждения и управляемая публикация правил"
        if title == "Правила"
        else "Управление версией правил и сохранёнными подтверждениями"
    )
    return render_admin_page(
        title,
        body,
        role=session.role,
        csrf_token=session.csrf_token,
        active_path="/admin/rules",
        description=description,
        wide=True,
        kicker="Rules",
    )


def _render_publication(
    publication: WebRulesPublicationView, session: WebSession
) -> str:
    labels = {
        WebRulesPublicationStatus.DISABLED: "Отключена",
        WebRulesPublicationStatus.UNPUBLISHED: "Не опубликовано",
        WebRulesPublicationStatus.CURRENT: "Актуально",
        WebRulesPublicationStatus.STALE: "Требуется синхронизация",
    }
    badge_classes = {
        WebRulesPublicationStatus.DISABLED: "neutral",
        WebRulesPublicationStatus.UNPUBLISHED: "warning",
        WebRulesPublicationStatus.CURRENT: "success",
        WebRulesPublicationStatus.STALE: "danger",
    }
    state = publication.state
    channel = (
        f"#{publication.channel_name}"
        if publication.channel_name is not None
        else (f"ID {state.channel_id}" if state.channel_id is not None else "—")
    )
    reflected = publication.reflected_version or (
        f"ruleset ID {state.ruleset_id}" if state.ruleset_id is not None else "—"
    )
    selected_ids = {option.id for option in publication.channel_options}
    options = ['<option value="" disabled>Выберите канал</option>']
    if state.channel_id is not None and state.channel_id not in selected_ids:
        options.append(
            f'<option value="{state.channel_id}" selected>ID {state.channel_id}</option>'
        )
    options.extend(
        f'<option value="{option.id}"{" selected" if option.id == state.channel_id else ""}>#{escape(option.name)}</option>'
        for option in publication.channel_options
    )
    disabled = "" if publication.options_available else " disabled"
    availability = (
        ""
        if publication.options_available
        else '<p class="notice failure">Bot Control временно недоступен. Сохранённая конфигурация показана из PostgreSQL.</p>'
    )
    csrf = escape(session.csrf_token, quote=True)
    publication_label = labels[publication.status]
    if publication.current_version:
        publication_label += f" — версия {publication.current_version}"
    return f'''<section class="card"><h2>Публикация в Discord</h2>
<p><strong>Статус:</strong> <span class="badge {badge_classes[publication.status]}">{escape(publication_label)}</span></p>
<p><strong>Канал:</strong> {escape(channel)}<br><strong>Managed message:</strong> {state.message_id or "—"}<br><strong>Отражает:</strong> {escape(reflected)}</p>
{availability}<div class="actions"><form method="post" action="/admin/rules/publication/channel"><input type="hidden" name="csrf_token" value="{csrf}"><label>Канал<select name="channel_id" required{disabled}>{"".join(options)}</select></label><button type="submit"{disabled}>Сохранить</button></form>
<form method="post" action="/admin/rules/publication/sync"><input type="hidden" name="csrf_token" value="{csrf}"><button class="secondary" type="submit"{disabled}>Синхронизировать</button></form>
<form method="post" action="/admin/rules/publication/disable"><input type="hidden" name="csrf_token" value="{csrf}"><button class="danger" type="submit"{disabled}>Отключить</button></form></div></section>'''


def _render_compliance(
    summary: RulesComplianceSummary | None, *, unavailable: bool
) -> str:
    if unavailable:
        return """<section class="card"><h2>Подтверждение правил</h2><p class="notice failure">Сводку подтверждений временно не удалось загрузить. Остальные данные правил доступны.</p></section>"""
    if (
        summary is None
        or summary.availability is RulesComplianceAvailability.NO_PUBLISHED_RULES
    ):
        return """<section class="card"><h2>Подтверждение правил</h2><p>Нет опубликованных правил.</p></section>"""
    checkpoint_kind = (
        "требует повторного принятия"
        if summary.checkpoint_requires_reacceptance
        else "первая опубликованная версия"
    )
    return f"""<section class="card card-accent"><h2>Подтверждение правил</h2>
<p><strong>Текущая версия:</strong> {escape(summary.current_version or "—")}<br><strong>Обязательная точка принятия:</strong> {escape(summary.required_checkpoint_version or "—")} ({checkpoint_kind})<br><strong>Срок подтверждения:</strong> {_date(summary.deadline)}</p>
<p class="muted">Учитывается принятие обязательной точки или любой более новой опубликованной версии; это не обязательно принятие текущей версии.</p>
<div class="metric-grid"><div class="metric"><strong>{summary.compliant}</strong><span>Подтвердили</span></div><div class="metric"><strong>{summary.pending}</strong><span>Ожидают</span></div><div class="metric"><strong>{summary.overdue}</strong><span>Просрочили</span></div><div class="metric"><strong>{summary.total}</strong><span>Всего</span></div></div></section>"""


def render_rules_page(
    items: tuple[RulesetWithAcceptanceCount, ...],
    publication: WebRulesPublicationView,
    compliance: RulesComplianceSummary | None,
    session: WebSession,
    *,
    result: str | None = None,
    error: str | None = None,
    warning: str | None = None,
    compliance_unavailable: bool = False,
) -> str:
    current = next(
        (item for item in items if item.ruleset.status is RulesetStatus.PUBLISHED), None
    )
    errors = {
        "duplicate": "Версия уже существует.",
        "no_published": "Сначала должна существовать опубликованная версия.",
        "invalid": "Проверьте заполнение обязательных полей.",
        "invalid_channel": "Выбранный канал недоступен для публикации правил.",
        "cleanup_forbidden": "Не удалось удалить прежнюю публикацию: недостаточно прав Discord.",
        "cleanup_discord_api_failure": "Discord временно не позволил удалить прежнюю публикацию.",
        "control_unavailable": "Bot Control временно недоступен.",
        "sync_channel_unavailable": "Настроенный Discord-канал недоступен.",
        "sync_unsupported_channel": "Настроенный канал не поддерживает публикацию правил.",
        "sync_forbidden": "Недостаточно прав Discord для синхронизации сообщения.",
        "sync_discord_api_failure": "Discord API временно недоступен.",
    }
    results = {
        "published": "Версия опубликована.",
        "configured": "Канал публикации настроен.",
        "changed": "Канал публикации изменён.",
        "disabled": "Публикация отключена.",
        "unchanged": "Настройка уже актуальна.",
        "sync_created": "Сообщение с правилами создано.",
        "sync_updated": "Сообщение с правилами обновлено.",
        "sync_recreated": "Удалённое сообщение с правилами восстановлено.",
        "sync_already_current": "Сообщение с правилами уже актуально.",
        "sync_not_configured": "Публикация отключена.",
        "sync_no_published_ruleset": "Опубликованная версия правил отсутствует.",
    }
    notice = (
        f'<p class="notice success">{escape(results[result])}</p>'
        if result in results
        else ('<p class="notice success">Операция выполнена.</p>' if result else "")
    )
    if error in errors:
        notice = f'<p class="notice failure">{escape(errors[error])}</p>'
    if warning == "sync_failed":
        notice += '<p class="notice failure">Версия опубликована, но сообщение Discord не удалось синхронизировать. Используйте ручную синхронизацию позже.</p>'
    if current:
        rule = current.ruleset
        published = f"""<section class="card"><h2>{escape(rule.title)} · {escape(rule.version)}</h2><p>{_status_badge(rule.status)} · {_date(rule.published_at)} · принято: {current.accepted_count}</p><p>{escape(rule.change_summary or "Описание изменений не задано")}</p><p>Повторное принятие: {"требуется" if rule.requires_reacceptance else "не требуется"}</p><pre>{escape(rule.content)}</pre></section>"""
    else:
        published = '<p class="notice failure">Опубликованная версия отсутствует.</p>'
    rows = "".join(
        f'<tr><td><a href="/admin/rules/{item.ruleset.id}">{escape(item.ruleset.version)}</a></td><td>{_status_badge(item.ruleset.status)}</td><td>{_date(item.ruleset.created_at)}</td><td>{_date(item.ruleset.published_at)}</td><td>{escape(item.ruleset.change_summary or "—")}</td><td><a href="/admin/rules/{item.ruleset.id}/acceptances">{item.accepted_count}</a></td></tr>'
        for item in items
    )
    form = f'<form class="card" method="post" action="/admin/rules/drafts"><h2>Создать новую версию</h2><input type="hidden" name="csrf_token" value="{escape(session.csrf_token, quote=True)}"><label>Версия<input name="version" required placeholder="1.1"></label><button type="submit">Создать новую версию</button></form>'
    return _layout(
        "Правила",
        f'{notice}{published}{_render_publication(publication, session)}{_render_compliance(compliance, unavailable=compliance_unavailable)}{form}<section class="card"><h2>История версий</h2><div class="table-wrap"><table><thead><tr><th>Версия</th><th>Статус</th><th>Создано</th><th>Опубликовано</th><th>Изменения</th><th>Принято</th></tr></thead><tbody>{rows}</tbody></table></div></section>',
        session,
    )


def render_ruleset_page(
    rule: RulesetRecord,
    session: WebSession,
    *,
    preview: dict[str, str] | None = None,
    error: str | None = None,
) -> str:
    values = preview or {
        "version": rule.version,
        "title": rule.title,
        "content": rule.content,
        "change_summary": rule.change_summary or "",
        "requires_reacceptance": "on" if rule.requires_reacceptance else "",
        "reacceptance_grace_days": (
            str(rule.reacceptance_grace_days)
            if rule.reacceptance_grace_days is not None
            else ""
        ),
    }
    preview_html = (
        f'<section class="card"><h2>Предпросмотр Discord</h2><strong>{escape(values["title"])}</strong><p class="muted">Версия {escape(values["version"])}</p><pre>{escape(values["content"])}</pre></section>'
        if preview
        else ""
    )
    if rule.status is RulesetStatus.DRAFT:
        checked = " checked" if values.get("requires_reacceptance") == "on" else ""
        fields = f'<input type="hidden" name="csrf_token" value="{escape(session.csrf_token, quote=True)}"><label>Версия<input name="version" value="{escape(values["version"], quote=True)}" required></label><label>Заголовок<input name="title" value="{escape(values["title"], quote=True)}" required></label><label>Текст правил<textarea name="content" required>{escape(values["content"])}</textarea></label><label>Описание изменений<textarea name="change_summary">{escape(values["change_summary"])}</textarea></label><label class="checkbox-label"><input type="checkbox" name="requires_reacceptance" value="on"{checked}> Требовать повторное принятие (без enforcement)</label><label>Grace period, дней (1–365)<input type="number" name="reacceptance_grace_days" min="1" max="365" value="{escape(values.get("reacceptance_grace_days", ""), quote=True)}"></label>'
        editor = f'<section class="card"><form method="post"><h2>Черновик</h2>{fields}<div class="actions"><button formaction="/admin/rules/{rule.id}/save">Сохранить черновик</button><button class="secondary" formaction="/admin/rules/{rule.id}/preview">Предпросмотр</button></div></form><div class="actions"><form method="post" action="/admin/rules/{rule.id}/publish"><input type="hidden" name="csrf_token" value="{escape(session.csrf_token, quote=True)}"><button>Опубликовать</button></form><form method="post" action="/admin/rules/{rule.id}/delete"><input type="hidden" name="csrf_token" value="{escape(session.csrf_token, quote=True)}"><button class="danger">Удалить черновик</button></form></div></section>'
    else:
        editor = f'<section class="card"><p>{_status_badge(rule.status)} · эта версия неизменяема.</p><h2>{escape(rule.title)} · {escape(rule.version)}</h2><pre>{escape(rule.content)}</pre></section>'
    messages = {
        "duplicate": "Версия уже существует.",
        "invalid": "Проверьте заполнение обязательных полей.",
        "discord_title": "Заголовок превышает лимит Discord embed: 256 символов.",
        "discord_description": "Текст правил превышает лимит Discord embed: 4096 символов.",
        "discord_footer": "Версия слишком длинная: footer Discord embed может содержать не более 2048 символов.",
        "discord_total": "Общий размер title, description и footer превышает лимит Discord embed: 6000 символов.",
    }
    message = (
        f'<p class="notice failure">{escape(messages[error])}</p>'
        if error in messages
        else ""
    )
    return _layout(
        f"Правила {rule.version}",
        f'<a class="back-link" href="/admin/rules">← Все версии</a>{message}{editor}{preview_html}',
        session,
    )


async def admin_rules(request: Request) -> Response:
    if not await _allowed(request):
        return _denied()
    session = request.state.web_session
    items = await request.state.rules_service.load()
    publication_state = await request.state.rules_service.load_publication_state()
    options: tuple[ServerSettingsChannelOption, ...] = ()
    options_available = True
    try:
        settings_options = (
            await request.state.bot_profile_control.get_server_settings_options()
        )
        options = settings_options.channels
    except Exception as error:
        options_available = False
        logger.warning(
            "web_admin_rules_publication_options_unavailable actor=%s error_type=%s",
            session.discord_user_id,
            type(error).__name__,
        )
    publication = build_rules_publication_view(
        items,
        publication_state,
        options,
        options_available=options_available,
    )
    compliance = None
    compliance_unavailable = False
    try:
        compliance = await request.state.rules_service.load_compliance_summary()
    except Exception as error:
        compliance_unavailable = True
        logger.warning(
            "web_admin_rules_compliance_unavailable actor=%s error_type=%s",
            session.discord_user_id,
            type(error).__name__,
        )
    return HTMLResponse(
        render_rules_page(
            items,
            publication,
            compliance,
            session,
            result=request.query_params.get("result"),
            error=request.query_params.get("error"),
            warning=request.query_params.get("warning"),
            compliance_unavailable=compliance_unavailable,
        ),
        headers=RESPONSE_HEADERS,
    )


def _publication_configuration_redirect(
    status: RulesPublicationConfigurationStatus,
) -> RedirectResponse:
    if status is RulesPublicationConfigurationStatus.CONFIGURED:
        return _redirect(result="configured")
    if status is RulesPublicationConfigurationStatus.CHANGED:
        return _redirect(result="changed")
    if status is RulesPublicationConfigurationStatus.DISABLED:
        return _redirect(result="disabled")
    if status in {
        RulesPublicationConfigurationStatus.ALREADY_CONFIGURED,
        RulesPublicationConfigurationStatus.ALREADY_DISABLED,
    }:
        return _redirect(result="unchanged")
    if status is RulesPublicationConfigurationStatus.INVALID_CHANNEL:
        return _redirect(error="invalid_channel")
    if status is RulesPublicationConfigurationStatus.CLEANUP_FORBIDDEN:
        return _redirect(error="cleanup_forbidden")
    if status is RulesPublicationConfigurationStatus.CLEANUP_DISCORD_API_FAILURE:
        return _redirect(error="cleanup_discord_api_failure")
    return _redirect(error="control_unavailable")


async def admin_rules_publication_channel(request: Request) -> Response:
    values = await _write_form(request, {"csrf_token", "channel_id"})
    if isinstance(values, Response):
        return values
    raw_channel_id = values["channel_id"]
    if not raw_channel_id.isascii() or not raw_channel_id.isdecimal():
        return _denied(400)
    channel_id = int(raw_channel_id)
    if not 0 < channel_id <= MAX_DISCORD_SNOWFLAKE:
        return _denied(400)
    try:
        result = await request.state.bot_profile_control.configure_rules_publication(
            channel_id,
            actor_discord_user_id=(request.state.web_session.discord_user_id),
        )
    except RulesPublicationControlError:
        return _redirect(error="control_unavailable")
    return _publication_configuration_redirect(result.status)


async def admin_rules_publication_disable(request: Request) -> Response:
    values = await _write_form(request, {"csrf_token"})
    if isinstance(values, Response):
        return values
    try:
        result = await request.state.bot_profile_control.disable_rules_publication(
            actor_discord_user_id=request.state.web_session.discord_user_id
        )
    except RulesPublicationControlError:
        return _redirect(error="control_unavailable")
    return _publication_configuration_redirect(result.status)


async def admin_rules_publication_sync(request: Request) -> Response:
    values = await _write_form(request, {"csrf_token"})
    if isinstance(values, Response):
        return values
    actor = request.state.web_session.discord_user_id
    try:
        result = await request.state.bot_profile_control.sync_rules_publication(
            actor_discord_user_id=actor
        )
    except RulesPublicationControlError:
        return _redirect(error="control_unavailable")
    logger.info(
        "web_admin_rules_publication_manual_sync actor=%s guild_id=%s status=%s",
        actor,
        result.guild_id,
        result.status.value,
    )
    if result.failed:
        return _redirect(error=f"sync_{result.status.value}")
    return _redirect(result=f"sync_{result.status.value}")


async def admin_rules_create(request: Request) -> Response:
    values = await _write_form(request, {"csrf_token", "version"})
    if isinstance(values, Response):
        return values
    try:
        rule = await request.state.rules_service.create_draft(
            values["version"], request.state.web_session.discord_user_id
        )
    except DuplicateRulesetVersionError:
        return _redirect(error="duplicate")
    except NoPublishedRulesetError:
        return _redirect(error="no_published")
    except ValueError:
        return _redirect(error="invalid")
    return _redirect(f"/admin/rules/{rule.id}", result="created")


async def admin_rules_detail(request: Request) -> Response:
    if not await _allowed(request):
        return _denied()
    try:
        rule = await request.state.rules_service.get(request.path_params["ruleset_id"])
    except RulesetNotFoundError:
        return _denied(404)
    return HTMLResponse(
        render_ruleset_page(
            rule,
            request.state.web_session,
            error=request.query_params.get("error"),
        ),
        headers=RESPONSE_HEADERS,
    )


def _draft_values(values: dict[str, str]) -> dict[str, object]:
    requires_reacceptance = values.get("requires_reacceptance") == "on"
    raw_grace = values.get("reacceptance_grace_days", "").strip()
    grace_days = None
    if requires_reacceptance and raw_grace:
        if not raw_grace.isascii() or not raw_grace.isdecimal():
            raise ValueError("invalid reacceptance grace days")
        grace_days = int(raw_grace)
        if not 1 <= grace_days <= 365:
            raise ValueError("invalid reacceptance grace configuration")
    return {
        "version": values["version"],
        "title": values["title"],
        "content": values["content"],
        "change_summary": values["change_summary"],
        "requires_reacceptance": requires_reacceptance,
        "reacceptance_grace_days": grace_days,
    }


async def _draft_form(
    request: Request, *, rate_limit: bool = False
) -> dict[str, str] | Response:
    values = await _read_form(request)
    if values is None:
        return _denied(400)
    expected = {"csrf_token", "version", "title", "content", "change_summary"}
    if values.get("requires_reacceptance") == "on":
        expected.add("requires_reacceptance")
    if "reacceptance_grace_days" in values:
        expected.add("reacceptance_grace_days")
    session = request.state.web_session
    if set(values) != expected or not constant_time_token_equal(
        values.get("csrf_token", ""), session.csrf_token
    ):
        return _denied(400)
    if not await _allowed(request):
        return _revoke_and_deny(request) if rate_limit else _denied()
    if rate_limit:
        limiter: WebWriteRateLimiter = request.state.web_write_limiter
        if not limiter.allow(request.state.web_session_id):
            return _denied(429)
    return values


async def admin_rules_save(request: Request) -> Response:
    values = await _draft_form(request, rate_limit=True)
    if isinstance(values, Response):
        return values
    ruleset_id = request.path_params["ruleset_id"]
    try:
        await request.state.rules_service.update_draft(
            ruleset_id,
            request.state.web_session.discord_user_id,
            **_draft_values(values),
        )
    except DuplicateRulesetVersionError:
        return _redirect(f"/admin/rules/{ruleset_id}", error="duplicate")
    except (ImmutableRulesetError, RulesetNotFoundError):
        return _denied(409)
    except RulesetDiscordLimitError as error:
        return _redirect(f"/admin/rules/{ruleset_id}", error=f"discord_{error.code}")
    except ValueError:
        return _redirect(f"/admin/rules/{ruleset_id}", error="invalid")
    return _redirect(f"/admin/rules/{ruleset_id}", result="saved")


async def admin_rules_preview(request: Request) -> Response:
    values = await _draft_form(request)
    if isinstance(values, Response):
        return values
    try:
        rule = await request.state.rules_service.get(request.path_params["ruleset_id"])
    except RulesetNotFoundError:
        return _denied(404)
    if rule.status is not RulesetStatus.DRAFT:
        return _denied(409)
    error_code = None
    try:
        _draft_values(values)
        validate_ruleset_for_discord(
            version=values["version"].strip(),
            title=values["title"].strip(),
            content=values["content"].strip(),
        )
    except RulesetDiscordLimitError as error:
        error_code = f"discord_{error.code}"
    except ValueError:
        error_code = "invalid"
    return HTMLResponse(
        render_ruleset_page(
            rule, request.state.web_session, preview=values, error=error_code
        ),
        status_code=422 if error_code else 200,
        headers=RESPONSE_HEADERS,
    )


async def _simple_action(request: Request, action: str) -> Response:
    values = await _write_form(request, {"csrf_token"})
    if isinstance(values, Response):
        return values
    ruleset_id = request.path_params["ruleset_id"]
    try:
        if action == "publish":
            await request.state.rules_service.publish(
                ruleset_id, request.state.web_session.discord_user_id
            )
        else:
            await request.state.rules_service.delete_draft(
                ruleset_id, request.state.web_session.discord_user_id
            )
    except (ImmutableRulesetError, RulesetNotFoundError):
        return _denied(409)
    except RulesetDiscordLimitError as error:
        return _redirect(f"/admin/rules/{ruleset_id}", error=f"discord_{error.code}")
    if action == "publish":
        actor = request.state.web_session.discord_user_id
        try:
            sync_result = (
                await request.state.bot_profile_control.sync_rules_publication(
                    actor_discord_user_id=actor
                )
            )
        except RulesPublicationControlError as error:
            logger.warning(
                "web_admin_rules_publication_auto_sync_failed actor=%s "
                "ruleset_id=%s error_type=%s category=%s",
                actor,
                ruleset_id,
                type(error).__name__,
                error.category.value,
            )
            return _redirect(result="published", warning="sync_failed")
        if sync_result.failed:
            logger.warning(
                "web_admin_rules_publication_auto_sync_failed actor=%s "
                "ruleset_id=%s guild_id=%s status=%s",
                actor,
                ruleset_id,
                sync_result.guild_id,
                sync_result.status.value,
            )
            return _redirect(result="published", warning="sync_failed")
    return _redirect(result="published" if action == "publish" else "deleted")


async def admin_rules_publish(request: Request) -> Response:
    return await _simple_action(request, "publish")


async def admin_rules_delete(request: Request) -> Response:
    return await _simple_action(request, "delete")


async def admin_rules_acceptances(request: Request) -> Response:
    if not await _allowed(request):
        return _denied()
    try:
        rule = await request.state.rules_service.get(request.path_params["ruleset_id"])
        entries = await request.state.rules_service.acceptances(rule.id)
    except RulesetNotFoundError:
        return _denied(404)
    rows = "".join(
        f"<tr><td><code>{item.user_id}</code></td><td>{escape(item.display_name or '—')}</td><td>{_date(item.accepted_at)}</td></tr>"
        for item in entries
    )
    body = f'<a class="back-link" href="/admin/rules/{rule.id}">← Версия {escape(rule.version)}</a><section class="card"><div class="table-wrap"><table><thead><tr><th>Discord user ID</th><th>Имя</th><th>Принято</th></tr></thead><tbody>{rows}</tbody></table></div></section>'
    return HTMLResponse(
        _layout(f"Принятия версии {rule.version}", body, request.state.web_session),
        headers=RESPONSE_HEADERS,
    )
