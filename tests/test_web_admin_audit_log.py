from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Iterator
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from discord_stats_bot.config import WebSettings
from discord_stats_bot.web.app import create_app
from discord_stats_bot.web.audit_log import (
    WEB_ADMIN_AUDIT_EVENT_TYPES,
    WEB_ADMIN_AUDIT_LIMIT,
    WebAdminAuditEntry,
    WebAdminAuditLogService,
    render_audit_log_page,
)
from discord_stats_bot.web.auth import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationCategory,
    WebAdminAuthorizationDecision,
    WebAdminRole,
)
from discord_stats_bot.web.service import (
    AdminCounts,
    AdminMemberDetailResult,
    AdminMemberDetailStatus,
    AdminMembersPage,
    WebDatabaseHealth,
)

DATABASE_URL = "postgresql+asyncpg://test:test@localhost:5432/test"


def make_settings() -> WebSettings:
    return WebSettings(
        _env_file=None,
        DATABASE_URL=DATABASE_URL,
        DISCORD_GUILD_ID=10,
        REPORT_TIMEZONE="Asia/Yekaterinburg",
        WEB_ADMIN_DISCORD_CLIENT_ID=123,
        WEB_ADMIN_DISCORD_CLIENT_SECRET="oauth-secret",
        WEB_ADMIN_DISCORD_REDIRECT_URI=(
            "http://localhost:8000/admin/auth/discord/callback"
        ),
        WEB_ADMIN_COOKIE_SECURE=False,
        WEB_ADMIN_ALLOWED_USER_IDS="41,42",
    )


class FakeResources:
    session_factory = object()

    async def dispose(self) -> None:
        pass


class FakeAdminService:
    async def probe_database(self) -> WebDatabaseHealth:
        return WebDatabaseHealth(True, 0.001)

    async def load_counts(self) -> AdminCounts:
        return AdminCounts(1, 2, 3)

    async def load_members(
        self, *, page: int, query: str, **kwargs: object
    ) -> AdminMembersPage:
        return AdminMembersPage((), 0, page, 50, query)

    async def load_member_detail(self, user_id: int) -> AdminMemberDetailResult:
        return AdminMemberDetailResult(AdminMemberDetailStatus.NOT_FOUND)


class FakeAuthorizer:
    def __init__(self) -> None:
        self.roles = {
            41: WebAdminRole.OWNER,
            42: WebAdminRole.OWNER,
            50: WebAdminRole.ADMIN,
        }
        self.calls: list[int] = []
        self.error: Exception | None = None

    async def authorize(self, user_id: int) -> WebAdminAuthorizationDecision:
        self.calls.append(user_id)
        if self.error is not None:
            raise self.error
        role = self.roles.get(user_id)
        if role is None:
            return WebAdminAuthorizationDecision(
                False,
                WebAdminAuthorizationCategory.NOT_ALLOWED,
            )
        return WebAdminAuthorizationDecision(True, role=role)


class FakeAuditLogService:
    report_timezone = ZoneInfo("Asia/Yekaterinburg")

    def __init__(self) -> None:
        self.entries: tuple[WebAdminAuditEntry, ...] | None = (
            WebAdminAuditEntry(
                3,
                "web_admin.server_setting_changed",
                datetime(2026, 8, 25, 10, 0, tzinfo=UTC),
                41,
                "Owner Local Name",
                None,
                None,
                {"source": "env", "value": 1234},
                {"source": "value", "value": 5678},
                {"setting_key": "autorole_role"},
            ),
            WebAdminAuditEntry(
                2,
                "web_admin.access_revoked",
                datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
                41,
                "Owner Local Name",
                50,
                "Managed Local Name",
            ),
            WebAdminAuditEntry(
                1,
                "web_admin.access_granted",
                datetime(2026, 8, 23, 10, 0, tzinfo=UTC),
                42,
                "42",
                60,
                "60",
            ),
        )
        self.calls = 0

    async def load_recent(self) -> tuple[WebAdminAuditEntry, ...] | None:
        self.calls += 1
        return self.entries


def make_app():
    authorizer = FakeAuthorizer()
    audit = FakeAuditLogService()
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),
        service_factory=lambda session_factory: FakeAdminService(),
        oauth_client_factory=lambda session, settings: SimpleNamespace(),
        bot_profile_control_factory=lambda session, settings: SimpleNamespace(),
        authorization_service_factory=lambda session_factory, settings: authorizer,
        administrator_service_factory=lambda session_factory, settings: (
            SimpleNamespace()
        ),
        audit_log_service_factory=lambda session_factory, settings: audit,
    )
    return app, authorizer, audit


@contextmanager
def authenticated_client(app, user_id: int, role: WebAdminRole) -> Iterator[TestClient]:
    issued = app.state.web_session_store.create(user_id, role=role)
    with TestClient(app) as client:
        client.cookies.set(
            SESSION_COOKIE_NAME,
            issued.session_id,
            path=SESSION_COOKIE_PATH,
        )
        yield client


@pytest.mark.parametrize("owner_id", [41, 42])
def test_both_owners_see_narrow_human_readable_audit_page(owner_id: int) -> None:
    app, authorizer, audit = make_app()
    with authenticated_client(app, owner_id, WebAdminRole.OWNER) as client:
        response = client.get("/admin/audit")

    assert response.status_code == 200
    assert authorizer.calls == [owner_id]
    assert audit.calls == 1
    assert 'class="table-wrap responsive-desktop-only"' in response.text
    assert "<table><thead>" in response.text
    assert 'class="mobile-record-list responsive-mobile-only"' in response.text
    assert response.text.count('class="mobile-record audit-record"') == 3
    assert '<dl class="record-fields">' in response.text
    assert "Выдан доступ ADMIN" in response.text
    assert "Отозван доступ ADMIN" in response.text
    assert "Изменена настройка" in response.text
    assert "Автоматическая роль" in response.text
    assert "ENV → Web Admin" in response.text
    assert "1234" not in response.text
    assert "5678" not in response.text
    assert "Owner Local Name" in response.text
    assert "Managed Local Name" in response.text
    assert "<code>41</code>" in response.text
    assert "<code>50</code>" in response.text
    assert "<code>42</code>" in response.text
    assert "<code>60</code>" in response.text
    assert "2026-08-24 15:00:00 +05" in response.text
    mobile_events = response.text.split('aria-label="События Web Admin">', 1)[1]
    assert mobile_events.index("Изменена настройка") < mobile_events.index(
        "Отозван доступ ADMIN"
    )
    assert mobile_events.index("Отозван доступ ADMIN") < mobile_events.index(
        "Выдан доступ ADMIN"
    )
    for forbidden in (
        "details_data",
        "last_delivery_error",
        "next_delivery_attempt_at",
        "discord_message_id",
    ):
        assert forbidden not in response.text


@pytest.mark.parametrize(
    ("setting_key", "label"),
    [
        ("autorole_role", "Автоматическая роль"),
        ("audit_log_channel", "Журнал аудита"),
        ("anniversary_channel", "Поздравления с годовщиной"),
        ("return_channel", "Возвращения участников"),
    ],
)
def test_all_server_setting_labels_and_modes_are_human_readable(
    setting_key: str,
    label: str,
) -> None:
    entry = WebAdminAuditEntry(
        1,
        "web_admin.server_setting_changed",
        datetime(2026, 8, 25, tzinfo=UTC),
        41,
        "Owner",
        None,
        None,
        {"source": "disabled", "value": None},
        {"source": "env", "value": 9012},
        {"setting_key": setting_key},
    )

    page = render_audit_log_page((entry,), timezone=ZoneInfo("UTC"))

    assert label in page
    assert "Отключено → ENV" in page
    assert "9012" not in page


@pytest.mark.parametrize(
    ("before_data", "after_data", "details_data"),
    [
        (None, None, None),
        ({"source": ["env"]}, {"source": True}, {"setting_key": []}),
        ("legacy", {"source": "disabled"}, {"setting_key": "unknown"}),
    ],
)
def test_malformed_server_setting_payload_has_safe_fallback(
    before_data: object,
    after_data: object,
    details_data: object,
) -> None:
    entry = WebAdminAuditEntry(
        1,
        "web_admin.server_setting_changed",
        datetime(2026, 8, 25, tzinfo=UTC),
        41,
        "Owner <unsafe>",
        None,
        None,
        before_data,
        after_data,
        details_data,
    )

    page = render_audit_log_page((entry,), timezone=ZoneInfo("UTC"))

    assert "Настройка сервера" in page
    assert "Неизвестно" in page
    assert "Owner &lt;unsafe&gt;" in page


def test_admin_and_unauthenticated_cannot_access_or_navigate_to_audit() -> None:
    app, _, audit = make_app()
    with TestClient(app) as client:
        unauthenticated = client.get("/admin/audit", follow_redirects=False)
    with authenticated_client(app, 50, WebAdminRole.ADMIN) as client:
        direct = client.get("/admin/audit")
        home = client.get("/admin/")

    assert unauthenticated.status_code == 303
    assert direct.status_code == 403
    assert 'href="/admin/audit"' not in home.text
    assert audit.calls == 0


def test_owner_navigation_is_shared_and_stale_owner_session_fails_fresh_gate() -> None:
    app, authorizer, audit = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as client:
        home = client.get("/admin/")
        authorizer.roles[41] = WebAdminRole.ADMIN
        denied = client.get("/admin/audit")

    assert 'href="/admin/administrators"' in home.text
    assert 'href="/admin/audit"' in home.text
    assert denied.status_code == 403
    assert authorizer.calls == [41]
    assert audit.calls == 0


def test_authorization_failure_and_database_failure_are_controlled() -> None:
    app, authorizer, audit = make_app()
    authorizer.error = RuntimeError("sensitive authorization detail")
    with authenticated_client(app, 41, WebAdminRole.OWNER) as client:
        authorization_denied = client.get("/admin/audit")

    app, _, audit = make_app()
    audit.entries = None
    with authenticated_client(app, 41, WebAdminRole.OWNER) as client:
        database_unavailable = client.get("/admin/audit")

    assert authorization_denied.status_code == 403
    assert "sensitive authorization detail" not in authorization_denied.text
    assert database_unavailable.status_code == 503
    assert "Журнал аудита временно недоступен" in database_unavailable.text


@pytest.mark.asyncio
async def test_audit_repository_is_scoped_bounded_ordered_and_select_only() -> None:
    class Result:
        def mappings(self):
            return self

        def all(self) -> list[dict[str, object]]:
            return [
                {
                    "id": 1,
                    "event_type": "web_admin.access_granted",
                    "occurred_at": datetime(2026, 8, 23, tzinfo=UTC),
                    "actor_user_id": 41,
                    "actor_display_name": "Owner",
                    "subject_id": 50,
                    "target_display_name": "50",
                    "before_data": None,
                    "after_data": None,
                    "details_data": {},
                },
                {
                    "id": 2,
                    "event_type": "web_admin.server_setting_changed",
                    "occurred_at": datetime(2026, 8, 24, tzinfo=UTC),
                    "actor_user_id": 41,
                    "actor_display_name": "Owner",
                    "subject_id": None,
                    "target_display_name": None,
                    "before_data": {"source": "env", "value": 30},
                    "after_data": {"source": "disabled", "value": None},
                    "details_data": {"setting_key": "audit_log_channel"},
                },
            ]

    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def execute(self, statement: object) -> Result:
            self.statements.append(statement)
            return Result()

    session = Session()
    service = WebAdminAuditLogService(
        lambda: session,  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
    )

    entries = await service.load_recent()

    assert entries is not None and entries[0].actor_display_name == "Owner"
    assert entries[1].target_user_id is None
    assert entries[1].details_data == {"setting_key": "audit_log_channel"}
    assert len(session.statements) == 1
    statement = session.statements[0]
    sql = " ".join(str(statement).split())
    params = tuple(statement.compile().params.values())  # type: ignore[union-attr]
    assert sql.startswith("SELECT")
    assert "audit_events.guild_id =" in sql
    assert "audit_events.category =" in sql
    assert "audit_events.event_type IN" in sql
    assert "audit_events.occurred_at DESC, audit_events.id DESC" in sql
    assert "LIMIT" in sql
    assert 10 in params
    assert "web_admin" in params
    assert any(
        tuple(value) == WEB_ADMIN_AUDIT_EVENT_TYPES
        for value in params
        if isinstance(value, (list, tuple))
    )
    assert WEB_ADMIN_AUDIT_LIMIT in params
    assert all(keyword not in sql.upper() for keyword in ("INSERT", "UPDATE", "DELETE"))
    for forbidden_column in ("delivery_attempts", "last_delivery_error"):
        assert forbidden_column not in sql
