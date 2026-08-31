from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Iterator

import pytest
from starlette.testclient import TestClient

from discord_stats_bot.config import WebSettings
from discord_stats_bot.web.administrators import render_administrators_page
from discord_stats_bot.web.app import create_app
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
    WebAdminAdministrator,
    WebAdminAdministrators,
    WebDatabaseHealth,
)

DATABASE_URL = "postgresql+asyncpg://test:test@localhost:5432/test"


def make_settings() -> WebSettings:
    return WebSettings(
        _env_file=None,
        DATABASE_URL=DATABASE_URL,
        DISCORD_GUILD_ID=10,
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

    async def authorize(self, user_id: int) -> WebAdminAuthorizationDecision:
        self.calls.append(user_id)
        role = self.roles.get(user_id)
        if role is None:
            return WebAdminAuthorizationDecision(
                False, WebAdminAuthorizationCategory.NOT_ALLOWED
            )
        return WebAdminAuthorizationDecision(True, role=role)


class FakeAdministratorService:
    def __init__(self) -> None:
        self.active = {50}
        self.current = {60}
        self.load_result = WebAdminAdministrators(
            owners=(
                WebAdminAdministrator(41, WebAdminRole.OWNER, "Owner One"),
                WebAdminAdministrator(42, WebAdminRole.OWNER, "42"),
            ),
            admins=(
                WebAdminAdministrator(
                    50,
                    WebAdminRole.ADMIN,
                    "Managed Admin",
                    datetime(2026, 8, 23, tzinfo=UTC),
                    41,
                ),
            ),
        )

    async def load(self) -> WebAdminAdministrators | None:
        return self.load_result

    def is_owner(self, user_id: int) -> bool:
        return user_id in {41, 42}

    async def is_active_admin(self, user_id: int) -> bool:
        return user_id in self.active

    async def is_current_non_bot_member(self, user_id: int) -> bool:
        return user_id in self.current


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []
        self.changed = True

    async def grant_web_admin_access(
        self, user_id: int, *, actor_discord_user_id: int
    ) -> bool:
        self.calls.append(("grant", user_id, actor_discord_user_id))
        return self.changed

    async def revoke_web_admin_access(
        self, user_id: int, *, actor_discord_user_id: int
    ) -> bool:
        self.calls.append(("revoke", user_id, actor_discord_user_id))
        return self.changed


def make_app():
    authorizer = FakeAuthorizer()
    administrators = FakeAdministratorService()
    control = FakeControl()
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),
        service_factory=lambda session_factory: FakeAdminService(),
        oauth_client_factory=lambda session, settings: SimpleNamespace(),
        bot_profile_control_factory=lambda session, settings: control,
        authorization_service_factory=lambda session_factory, settings: authorizer,
        administrator_service_factory=lambda session_factory, settings: administrators,
    )
    return app, authorizer, administrators, control


@contextmanager
def authenticated_client(
    app, user_id: int, role: WebAdminRole
) -> Iterator[tuple[TestClient, str]]:
    issued = app.state.web_session_store.create(user_id, role=role)
    with TestClient(app) as client:
        client.cookies.set(
            SESSION_COOKIE_NAME, issued.session_id, path=SESSION_COOKIE_PATH
        )
        yield client, issued.session.csrf_token


@pytest.mark.parametrize("owner_id", [41, 42])
def test_both_owners_see_page_with_protected_owners_and_active_admin(
    owner_id: int,
) -> None:
    app, _, _, _ = make_app()
    with authenticated_client(app, owner_id, WebAdminRole.OWNER) as (client, _):
        response = client.get("/admin/administrators")

    assert response.status_code == 200
    assert "Owner One" in response.text
    assert "<code>42</code>" in response.text
    assert "Managed Admin" in response.text
    assert response.text.count("Постоянный доступ") == 4
    assert response.text.count('class="table-wrap responsive-desktop-only"') == 2
    assert response.text.count("<table><thead>") == 2
    assert response.text.count('class="mobile-record-list responsive-mobile-only"') == 2
    assert 'class="mobile-record administrator-record"' in response.text
    assert (
        response.text.count(
            '<form method="post" action="/admin/administrators/revoke">'
        )
        == 2
    )
    owner_section = response.text.split("<h2>Managed ADMIN</h2>", 1)[0]
    assert "/administrators/revoke" not in owner_section


def test_mobile_revoke_uses_same_post_route_target_and_escaped_csrf() -> None:
    administrators = FakeAdministratorService().load_result

    page = render_administrators_page(
        administrators,
        csrf_token='unsafe"><token',
        result=None,
        error=None,
    )

    assert page.count('<form method="post" action="/admin/administrators/revoke">') == 2
    assert page.count('name="user_id" value="50"') == 2
    assert page.count('value="unsafe&quot;&gt;&lt;token"') == 5
    assert "Managed grant" in page
    owner_section = page.split("<h2>Managed ADMIN</h2>", 1)[0]
    assert "/admin/administrators/revoke" not in owner_section


def test_admin_and_unauthenticated_cannot_get_management_page() -> None:
    app, _, _, _ = make_app()
    with TestClient(app) as anonymous:
        unauthenticated = anonymous.get("/admin/administrators", follow_redirects=False)
    with authenticated_client(app, 50, WebAdminRole.ADMIN) as (client, _):
        admin = client.get("/admin/administrators", follow_redirects=False)

    assert unauthenticated.status_code == 303
    assert admin.status_code == 403


def test_management_navigation_is_owner_only() -> None:
    app, _, _, _ = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, _):
        owner_home = client.get("/admin/")
    with authenticated_client(app, 50, WebAdminRole.ADMIN) as (client, _):
        admin_home = client.get("/admin/")

    assert 'href="/admin/administrators"' in owner_home.text
    assert 'href="/admin/administrators"' not in admin_home.text


def test_owner_grants_current_member_with_session_actor_only() -> None:
    app, authorizer, _, control = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/administrators/grant",
            data={"csrf_token": csrf, "user_id": "60"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith("result=granted")
    assert authorizer.calls == [41]
    assert control.calls == [("grant", 60, 41)]


@pytest.mark.parametrize(
    "data",
    [
        {"user_id": "60"},
        {"csrf_token": "wrong", "user_id": "60"},
        {"csrf_token": "TOKEN", "user_id": "0"},
        {"csrf_token": "TOKEN", "user_id": "abc"},
        {"csrf_token": "TOKEN", "user_id": "60", "guild_id": "999"},
        {"csrf_token": "TOKEN", "user_id": "60", "actor_user_id": "999"},
    ],
)
def test_grant_rejects_bad_csrf_invalid_id_and_untrusted_fields(
    data: dict[str, str],
) -> None:
    app, _, _, control = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        payload = {
            key: (csrf if value == "TOKEN" else value) for key, value in data.items()
        }
        response = client.post("/admin/administrators/grant", data=payload)

    assert response.status_code == 400
    assert control.calls == []


def test_admin_post_and_ineligible_target_do_not_call_control() -> None:
    app, _, _, control = make_app()
    with authenticated_client(app, 50, WebAdminRole.ADMIN) as (client, csrf):
        admin_response = client.post(
            "/admin/administrators/grant", data={"csrf_token": csrf, "user_id": "60"}
        )
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        stale_response = client.post(
            "/admin/administrators/grant",
            data={"csrf_token": csrf, "user_id": "61"},
            follow_redirects=False,
        )

    assert admin_response.status_code == 403
    assert "error=invalid_target" in stale_response.headers["location"]
    assert control.calls == []


def test_owner_and_duplicate_admin_are_idempotently_protected() -> None:
    app, _, _, control = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        owner = client.post(
            "/admin/administrators/grant",
            data={"csrf_token": csrf, "user_id": "42"},
            follow_redirects=False,
        )
        duplicate = client.post(
            "/admin/administrators/grant",
            data={"csrf_token": csrf, "user_id": "50"},
            follow_redirects=False,
        )

    assert "result=owner_protected" in owner.headers["location"]
    assert "result=already_admin" in duplicate.headers["location"]
    assert control.calls == []


def test_owner_revokes_only_active_admin_with_current_owner_actor() -> None:
    app, _, _, control = make_app()
    with authenticated_client(app, 42, WebAdminRole.OWNER) as (client, csrf):
        revoked = client.post(
            "/admin/administrators/revoke",
            data={"csrf_token": csrf, "user_id": "50"},
            follow_redirects=False,
        )
        owner = client.post(
            "/admin/administrators/revoke",
            data={"csrf_token": csrf, "user_id": "41"},
            follow_redirects=False,
        )
        inactive = client.post(
            "/admin/administrators/revoke",
            data={"csrf_token": csrf, "user_id": "60"},
            follow_redirects=False,
        )

    assert control.calls == [("revoke", 50, 42)]
    assert "result=owner_protected" in owner.headers["location"]
    assert "result=not_active" in inactive.headers["location"]
    assert "result=revoked" in revoked.headers["location"]


def test_admin_cannot_post_revoke_and_csrf_is_required() -> None:
    app, _, _, control = make_app()
    with authenticated_client(app, 50, WebAdminRole.ADMIN) as (client, csrf):
        denied = client.post(
            "/admin/administrators/revoke", data={"csrf_token": csrf, "user_id": "50"}
        )
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, _):
        csrf_denied = client.post(
            "/admin/administrators/revoke", data={"user_id": "50"}
        )

    assert denied.status_code == 403
    assert csrf_denied.status_code == 400
    assert control.calls == []
