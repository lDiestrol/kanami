"""Read-only Web Admin presentation for registered Kanami capabilities."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from urllib.parse import parse_qs, urlencode

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from discord_stats_bot.features.capabilities import (
    CAPABILITY_REGISTRY,
    CapabilityDefinition,
    CapabilityMutationService,
    CapabilitySubjectType,
    UnknownCapabilityError,
    get_capability,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyAuditEventRepository,
    SqlAlchemyCapabilityRepository,
)
from discord_stats_bot.web.auth import WebSession, constant_time_token_equal
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationDecision,
    WebAdminRole,
)
from discord_stats_bot.web.bot_control import CapabilityPresentationControl
from discord_stats_bot.web.presentation import render_admin_page
from discord_stats_bot.web.security import WebWriteRateLimiter

logger = logging.getLogger(__name__)
RESPONSE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
ALLOWED_ROLES = {WebAdminRole.OWNER, WebAdminRole.ADMIN}
FORM_MAX_BYTES = 4_096


@dataclass(frozen=True, slots=True)
class WebAdminCapabilitySubject:
    label: str
    subject_id: int
    missing: bool


@dataclass(frozen=True, slots=True)
class WebAdminCapabilityGrant:
    subject: WebAdminCapabilitySubject


@dataclass(frozen=True, slots=True)
class WebAdminCapabilityView:
    definition: CapabilityDefinition
    enabled: bool
    role_grants: tuple[WebAdminCapabilityGrant, ...]
    user_grants: tuple[WebAdminCapabilityGrant, ...]
    subject_resolution_available: bool = True


class WebAdminCapabilitiesReadService:
    """Load only registry-backed capability state and cached subject labels."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        subject_control: CapabilityPresentationControl,
        *,
        guild_id: int,
    ) -> None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        self._session_factory = session_factory
        self._subject_control = subject_control
        self._guild_id = guild_id

    async def load(self) -> tuple[WebAdminCapabilityView, ...] | None:
        try:
            async with self._session_factory() as session:
                repository = SqlAlchemyCapabilityRepository(session)
                records = []
                for definition in CAPABILITY_REGISTRY.values():
                    records.append(
                        (
                            definition,
                            await repository.get_policy(self._guild_id, definition.key),
                            await repository.list_grants(
                                self._guild_id, definition.key
                            ),
                        )
                    )
        except Exception as error:
            logger.warning(
                "web_admin_capabilities_lookup_failed error_type=%s",
                type(error).__name__,
            )
            return None
        records_tuple = tuple(records)
        role_ids = tuple(
            grant.subject_id
            for _, _, grants in records_tuple
            for grant in grants
            if grant.subject_type is CapabilitySubjectType.ROLE
        )
        user_ids = tuple(
            grant.subject_id
            for _, _, grants in records_tuple
            for grant in grants
            if grant.subject_type is CapabilitySubjectType.USER
        )
        try:
            subjects = await self._subject_control.get_capability_presentation_subjects(
                role_ids, user_ids
            )
            role_names = dict(subjects.roles)
            user_names = dict(subjects.members)
            subject_resolution_available = True
        except Exception as error:
            logger.warning(
                "web_admin_capability_subjects_unavailable error_type=%s",
                type(error).__name__,
            )
            role_names = {}
            user_names = {}
            subject_resolution_available = False
        return tuple(
            WebAdminCapabilityView(
                definition=definition,
                enabled=(
                    definition.default_enabled if policy is None else policy.enabled
                ),
                role_grants=tuple(
                    WebAdminCapabilityGrant(
                        WebAdminCapabilitySubject(
                            role_names.get(
                                grant.subject_id,
                                "Unknown role"
                                if subject_resolution_available
                                else "Role name unavailable",
                            ),
                            grant.subject_id,
                            subject_resolution_available
                            and grant.subject_id not in role_names,
                        )
                    )
                    for grant in grants
                    if grant.subject_type is CapabilitySubjectType.ROLE
                ),
                user_grants=tuple(
                    WebAdminCapabilityGrant(
                        WebAdminCapabilitySubject(
                            user_names.get(
                                grant.subject_id,
                                "Unknown member"
                                if subject_resolution_available
                                else "Member name unavailable",
                            ),
                            grant.subject_id,
                            subject_resolution_available
                            and grant.subject_id not in user_names,
                        )
                    )
                    for grant in grants
                    if grant.subject_type is CapabilitySubjectType.USER
                ),
                subject_resolution_available=subject_resolution_available,
            )
            for definition, policy, grants in records_tuple
        )


class WebAdminCapabilityPolicyMutationService:
    """Own the short Web Admin transaction around the generic A1 mutation."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id

    async def set_enabled(
        self, capability: str, enabled: bool, actor_user_id: int
    ) -> bool:
        async with self._session_factory.begin() as session:
            result = await CapabilityMutationService(
                SqlAlchemyCapabilityRepository(session),
                SqlAlchemyAuditEventRepository(session),
            ).set_enabled(
                guild_id=self._guild_id,
                capability=capability,
                enabled=enabled,
                actor_user_id=actor_user_id,
                occurred_at=datetime.now(UTC),
            )
        return result.changed


async def _allowed(request: Request, session: WebSession) -> bool:
    try:
        decision: WebAdminAuthorizationDecision = (
            await request.state.admin_authorizer.authorize(session.discord_user_id)
        )
    except Exception as error:
        logger.warning(
            "web_admin_capabilities_authorization_failed actor=%s error_type=%s",
            session.discord_user_id,
            type(error).__name__,
        )
        return False
    return decision.allowed and decision.role in ALLOWED_ROLES


def _grant_list(grants: tuple[WebAdminCapabilityGrant, ...], *, empty: str) -> str:
    if not grants:
        return f'<p class="empty">{escape(empty)}</p>'
    return (
        '<ul class="capability-grants">'
        + "".join(
            "<li><strong>"
            f"{escape(grant.subject.label)}</strong>"
            f"<small>Discord ID · {grant.subject.subject_id}</small></li>"
            for grant in grants
        )
        + "</ul>"
    )


def _policy_action(item: WebAdminCapabilityView, csrf_token: str) -> str:
    enabled = not item.enabled
    label = "Enable" if enabled else "Disable"
    return (
        '<form method="post" action="/admin/capabilities">'
        f'<input type="hidden" name="csrf_token" value="{escape(csrf_token, quote=True)}">'
        f'<input type="hidden" name="capability" value="{escape(item.definition.key, quote=True)}">'
        f'<input type="hidden" name="enabled" value="{str(enabled).lower()}">'
        f'<button class="{"secondary" if enabled else "danger"}" type="submit">'
        f"{label}</button></form>"
    )


def render_capabilities_page(
    capabilities: tuple[WebAdminCapabilityView, ...] | None,
    *,
    csrf_token: str,
    role: WebAdminRole,
    result: str | None = None,
) -> str:
    if capabilities is None:
        body = '<p class="notice failure">Capabilities временно недоступны.</p>'
    else:
        messages = {
            "enabled": "Capability enabled.",
            "disabled": "Capability disabled.",
            "already_enabled": "Capability is already enabled.",
            "already_disabled": "Capability is already disabled.",
            "invalid": "Invalid capability request.",
            "unavailable": "Capability write is temporarily unavailable.",
        }
        notice = (
            f'<p class="notice {"failure" if result in {"invalid", "unavailable"} else "success"}">'
            f"{escape(messages[result])}</p>"
            if result in messages
            else ""
        )
        warning = (
            '<p class="notice failure">'
            "Discord role/member names temporarily unavailable."
            "</p>"
            if any(not item.subject_resolution_available for item in capabilities)
            else ""
        )
        body = (
            notice
            + warning
            + "".join(
                '<section class="card capability-card">'
                f'<p class="page-kicker">{escape(item.definition.group)}</p>'
                f"<h2>{escape(item.definition.title)}</h2>"
                f"<p>{escape(item.definition.description)}</p>"
                "<p><strong>Состояние:</strong> "
                f'<span class="badge {"accent" if item.enabled else "neutral"}">'
                f"{'Enabled' if item.enabled else 'Disabled'}</span></p>"
                '<div class="capability-grant-columns">'
                "<section><h3>Allowed roles</h3>"
                f"{_grant_list(item.role_grants, empty='Нет разрешённых ролей.')}"
                "</section><section><h3>Allowed users</h3>"
                f"{_grant_list(item.user_grants, empty='Нет разрешённых пользователей.')}"
                "</section></div>"
                f'<div class="actions">{_policy_action(item, csrf_token)}</div>'
                f'<code class="capability-key">{escape(item.definition.key)}</code>'
                "</section>"
                for item in capabilities
            )
        )
    return render_admin_page(
        "Capabilities",
        body,
        role=role,
        csrf_token=csrf_token,
        active_path="/admin/capabilities",
        description="Зарегистрированные права Kanami и текущие разрешения",
        kicker="Kanami access",
    )


async def admin_capabilities(request: Request) -> HTMLResponse:
    session: WebSession = request.state.web_session
    if not await _allowed(request, session):
        return HTMLResponse(
            "<!doctype html><h1>Request denied</h1>",
            status_code=403,
            headers=RESPONSE_HEADERS,
        )
    service: WebAdminCapabilitiesReadService = request.state.capabilities_read_service
    capabilities = await service.load()
    return HTMLResponse(
        render_capabilities_page(
            capabilities,
            csrf_token=session.csrf_token,
            role=session.role,
            result=request.query_params.get("result"),
        ),
        status_code=200 if capabilities is not None else 503,
        headers=RESPONSE_HEADERS,
    )


async def _read_form(request: Request) -> dict[str, str] | None:
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
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
    except UnicodeDecodeError:
        return None
    if any(len(values) != 1 for values in parsed.values()):
        return None
    return {key: values[0] for key, values in parsed.items()}


def _redirect(**parameters: str) -> RedirectResponse:
    location = "/admin/capabilities"
    if parameters:
        location += "?" + urlencode(parameters)
    return RedirectResponse(location, status_code=303, headers=RESPONSE_HEADERS)


async def admin_capabilities_update(request: Request) -> Response:
    session: WebSession = request.state.web_session
    values = await _read_form(request)
    if (
        values is None
        or set(values) != {"csrf_token", "capability", "enabled"}
        or not constant_time_token_equal(values["csrf_token"], session.csrf_token)
        or values["enabled"] not in {"true", "false"}
    ):
        return HTMLResponse("<!doctype html><h1>Request denied</h1>", status_code=400)
    if not await _allowed(request, session):
        return HTMLResponse("<!doctype html><h1>Request denied</h1>", status_code=403)
    try:
        definition = get_capability(values["capability"])
    except UnknownCapabilityError:
        return _redirect(result="invalid")
    limiter: WebWriteRateLimiter = request.state.web_write_limiter
    if not limiter.allow(request.state.web_session_id):
        return HTMLResponse("<!doctype html><h1>Request denied</h1>", status_code=429)
    enabled = values["enabled"] == "true"
    try:
        changed = await request.state.capabilities_mutation_service.set_enabled(
            definition.key, enabled, session.discord_user_id
        )
    except Exception as error:
        logger.warning(
            "web_admin_capability_mutation_failed actor=%s error_type=%s",
            session.discord_user_id,
            type(error).__name__,
        )
        return _redirect(result="unavailable")
    if changed:
        return _redirect(result="enabled" if enabled else "disabled")
    return _redirect(result="already_enabled" if enabled else "already_disabled")
