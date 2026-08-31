from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator

import pytest
from starlette.testclient import TestClient

from discord_stats_bot.config import WebSettings
from discord_stats_bot.features.server_settings import (
    GuildServerSettingKey,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
    GuildServerSettingSource,
    ServerSettingsChannelOption,
    ServerSettingsChannelType,
    ServerSettingsOptions,
    ServerSettingsRoleOption,
)
from discord_stats_bot.web.app import create_app
from discord_stats_bot.web.auth import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationCategory,
    WebAdminAuthorizationDecision,
    WebAdminRole,
)
from discord_stats_bot.web.bot_control import (
    ServerSettingsControlCategory,
    ServerSettingsControlError,
)
from discord_stats_bot.web.server_settings import WebAdminServerSettingValue
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
        DISCORD_AUTOROLE_ID=20,
        DISCORD_AUDIT_LOG_CHANNEL_ID=30,
        WEB_ADMIN_DISCORD_CLIENT_ID=123,
        WEB_ADMIN_DISCORD_CLIENT_SECRET="oauth-secret",
        WEB_ADMIN_DISCORD_REDIRECT_URI=(
            "http://localhost:8000/admin/auth/discord/callback"
        ),
        WEB_ADMIN_COOKIE_SECURE=False,
        WEB_ADMIN_ALLOWED_USER_IDS="41",
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
        self.roles = {41: WebAdminRole.OWNER, 50: WebAdminRole.ADMIN}
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


class FakeReadService:
    def __init__(self) -> None:
        self.result = (
            WebAdminServerSettingValue(
                GuildServerSettingKey.AUTOROLE_ROLE,
                20,
                GuildServerSettingSource.ENV,
            ),
            WebAdminServerSettingValue(
                GuildServerSettingKey.AUDIT_LOG_CHANNEL,
                30,
                GuildServerSettingSource.DB,
            ),
            WebAdminServerSettingValue(
                GuildServerSettingKey.ANNIVERSARY_CHANNEL,
                None,
                GuildServerSettingSource.DISABLED,
            ),
            WebAdminServerSettingValue(
                GuildServerSettingKey.RETURN_CHANNEL,
                999,
                GuildServerSettingSource.DB,
            ),
        )

    async def load(self) -> tuple[WebAdminServerSettingValue, ...] | None:
        return self.result


class FakeControl:
    def __init__(self) -> None:
        self.options = ServerSettingsOptions(
            roles=(
                ServerSettingsRoleOption(20, "Member"),
                ServerSettingsRoleOption(21, "Other role"),
            ),
            channels=(
                ServerSettingsChannelOption(
                    30,
                    "audit-log",
                    ServerSettingsChannelType.TEXT,
                ),
                ServerSettingsChannelOption(
                    31,
                    "other-channel",
                    ServerSettingsChannelType.TEXT,
                ),
            ),
        )
        self.options_error: ServerSettingsControlError | None = None
        self.change_error: ServerSettingsControlError | None = None
        self.changed = True
        self.calls: list[tuple[object, object, int]] = []
        self.options_calls = 0

    async def get_server_settings_options(self) -> ServerSettingsOptions:
        self.options_calls += 1
        if self.options_error is not None:
            raise self.options_error
        return self.options

    async def change_server_setting(
        self,
        key: GuildServerSettingKey,
        override: GuildServerSettingOverride,
        *,
        actor_discord_user_id: int,
    ) -> bool:
        self.calls.append((key, override, actor_discord_user_id))
        if self.change_error is not None:
            raise self.change_error
        return self.changed


def make_app():
    authorizer = FakeAuthorizer()
    read_service = FakeReadService()
    control = FakeControl()
    read_only_calls: list[bool] = []

    def resources(settings: object, *, read_only: bool) -> FakeResources:
        del settings
        read_only_calls.append(read_only)
        return FakeResources()

    app = create_app(
        make_settings(),
        resource_factory=resources,
        service_factory=lambda session_factory: FakeAdminService(),
        oauth_client_factory=lambda session, settings: SimpleNamespace(),
        bot_profile_control_factory=lambda session, settings: control,
        authorization_service_factory=lambda session_factory, settings: authorizer,
        server_settings_read_service_factory=(
            lambda session_factory, settings: read_service  # type: ignore[arg-type]
        ),
    )
    return app, authorizer, read_service, control, read_only_calls


@contextmanager
def authenticated_client(
    app: object,
    user_id: int,
    role: WebAdminRole,
) -> Iterator[tuple[TestClient, str]]:
    issued = app.state.web_session_store.create(user_id, role=role)  # type: ignore[attr-defined]
    with TestClient(app) as client:  # type: ignore[arg-type]
        client.cookies.set(
            SESSION_COOKIE_NAME,
            issued.session_id,
            path=SESSION_COOKIE_PATH,
        )
        yield client, issued.session.csrf_token


@pytest.mark.parametrize(
    ("user_id", "role"),
    [(41, WebAdminRole.OWNER), (50, WebAdminRole.ADMIN)],
)
def test_owner_and_admin_get_effective_settings_page(
    user_id: int,
    role: WebAdminRole,
) -> None:
    app, _, _, _, read_only_calls = make_app()

    with authenticated_client(app, user_id, role) as (client, _):
        response = client.get("/admin/server-settings")

    assert response.status_code == 200
    assert "@Member" in response.text
    assert "#audit-log" in response.text
    assert 'class="badge info">ENV</span>' in response.text
    assert 'class="badge accent">Web Admin / DB</span>' in response.text
    assert 'class="badge neutral">Отключено</span>' in response.text
    assert "Текущее значение недоступно" in response.text
    assert "999" not in response.text
    assert read_only_calls == [False]


def test_effective_env_role_and_db_channel_are_selected() -> None:
    app, _, _, _, _ = make_app()

    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, _):
        response = client.get("/admin/server-settings")

    autorole_card = response.text.split("Автоматическая роль", 1)[1].split(
        "Журнал аудита", 1
    )[0]
    audit_card = response.text.split("Журнал аудита", 1)[1].split(
        "Поздравления с годовщиной", 1
    )[0]
    assert '<option value="20" selected>Member</option>' in autorole_card
    assert '<option value="21">Other role</option>' in autorole_card
    assert '<option value="30" selected>audit-log</option>' in audit_card
    assert '<option value="31">other-channel</option>' in audit_card


def test_db_role_value_is_selected() -> None:
    app, _, read_service, _, _ = make_app()
    read_service.result = (
        WebAdminServerSettingValue(
            GuildServerSettingKey.AUTOROLE_ROLE,
            21,
            GuildServerSettingSource.DB,
        ),
    )

    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, _):
        response = client.get("/admin/server-settings")

    assert '<option value="21" selected>Other role</option>' in response.text
    assert '<option value="20">Member</option>' in response.text


def test_disabled_and_missing_values_keep_neutral_placeholder_selected() -> None:
    app, _, _, _, _ = make_app()

    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, _):
        response = client.get("/admin/server-settings")

    disabled_card = response.text.split("Поздравления с годовщиной", 1)[1].split(
        "Возвращения участников", 1
    )[0]
    missing_card = response.text.split("Возвращения участников", 1)[1]
    placeholder = '<option value="" disabled selected>Выберите канал</option>'
    for card in (disabled_card, missing_card):
        assert placeholder in card
        assert '<option value="30" selected>' not in card
        assert '<option value="31" selected>' not in card
    assert "999" not in response.text


def test_anonymous_and_revoked_admin_get_are_denied_without_session_revoke() -> None:
    app, authorizer, _, control, _ = make_app()
    with TestClient(app) as client:
        anonymous = client.get("/admin/server-settings", follow_redirects=False)
    authorizer.roles.pop(50)
    with authenticated_client(app, 50, WebAdminRole.ADMIN) as (client, _):
        revoked = client.get("/admin/server-settings")
        assert len(app.state.web_session_store) == 1

    assert anonymous.status_code == 303
    assert revoked.status_code == 403
    assert control.options_calls == 0
    assert control.calls == []


def test_navigation_is_visible_to_owner_and_admin_but_owner_links_stay_private() -> (
    None
):
    app, _, _, _, _ = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, _):
        owner = client.get("/admin/")
    with authenticated_client(app, 50, WebAdminRole.ADMIN) as (client, _):
        admin = client.get("/admin/")

    assert 'href="/admin/server-settings"' in owner.text
    assert 'href="/admin/server-settings"' in admin.text
    assert 'href="/admin/administrators"' in owner.text
    assert 'href="/admin/audit"' in owner.text
    assert 'href="/admin/administrators"' not in admin.text
    assert 'href="/admin/audit"' not in admin.text


def test_options_unavailable_is_controlled_503() -> None:
    app, _, _, control, _ = make_app()
    control.options_error = ServerSettingsControlError(
        ServerSettingsControlCategory.CONTROL_UNAVAILABLE
    )

    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, _):
        response = client.get("/admin/server-settings")

    assert response.status_code == 503
    assert "Настройки временно недоступны" in response.text
    assert "Traceback" not in response.text


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"csrf_token": "wrong", "setting": "autorole_role", "mode": "env"},
    ],
)
def test_post_requires_csrf(data: dict[str, str]) -> None:
    app, authorizer, _, control, _ = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, _):
        response = client.post("/admin/server-settings", data=data)
        assert len(app.state.web_session_store) == 1

    assert response.status_code == 403
    assert authorizer.calls == []
    assert control.options_calls == 0
    assert control.calls == []


@pytest.mark.parametrize(
    "user_id,role", [(41, WebAdminRole.OWNER), (50, WebAdminRole.ADMIN)]
)
def test_fresh_owner_and_admin_post_use_session_actor(
    user_id: int,
    role: WebAdminRole,
) -> None:
    app, authorizer, _, control, _ = make_app()
    with authenticated_client(app, user_id, role) as (client, csrf):
        response = client.post(
            "/admin/server-settings",
            data={
                "csrf_token": csrf,
                "setting": "autorole_role",
                "mode": "value",
                "value": "20",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith("result=saved")
    assert authorizer.calls[-1] == user_id
    assert control.calls[0][0] is GuildServerSettingKey.AUTOROLE_ROLE
    assert control.calls[0][1] == GuildServerSettingOverride(
        GuildServerSettingOverrideMode.VALUE,
        20,
    )
    assert control.calls[0][2] == user_id


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("env", GuildServerSettingOverride(GuildServerSettingOverrideMode.ENV)),
        (
            "disabled",
            GuildServerSettingOverride(GuildServerSettingOverrideMode.DISABLED),
        ),
    ],
)
def test_env_and_disabled_map_to_exact_control_payload(
    mode: str,
    expected: GuildServerSettingOverride,
) -> None:
    app, _, _, control, _ = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/server-settings",
            data={
                "csrf_token": csrf,
                "setting": "audit_log_channel",
                "mode": mode,
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert control.calls == [(GuildServerSettingKey.AUDIT_LOG_CHANNEL, expected, 41)]


@pytest.mark.parametrize(
    "extra",
    [
        {"guild_id": "10"},
        {"actor": "50"},
        {"value": "20"},
        {"unexpected": "field"},
    ],
)
def test_env_rejects_browser_scope_and_extra_fields(extra: dict[str, str]) -> None:
    app, _, _, control, _ = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/server-settings",
            data={
                "csrf_token": csrf,
                "setting": "autorole_role",
                "mode": "env",
                **extra,
            },
        )

    assert response.status_code == 400
    assert control.calls == []


@pytest.mark.parametrize(
    "data",
    [
        {"setting": "unknown", "mode": "env"},
        {"setting": "autorole_role", "mode": "unknown"},
        {"setting": "autorole_role", "mode": "value"},
        {"setting": "autorole_role", "mode": "value", "value": "True"},
    ],
)
def test_post_rejects_unknown_mode_setting_and_invalid_value(
    data: dict[str, str],
) -> None:
    app, _, _, control, _ = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/server-settings",
            data={"csrf_token": csrf, **data},
        )

    assert response.status_code == 400
    assert control.calls == []


def test_value_must_still_exist_in_current_options() -> None:
    app, _, _, control, _ = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/server-settings",
            data={
                "csrf_token": csrf,
                "setting": "autorole_role",
                "mode": "value",
                "value": "99",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "error=invalid_target" in response.headers["location"]
    assert control.calls == []


@pytest.mark.parametrize(
    ("changed", "result"),
    [(True, "saved"), (False, "unchanged")],
)
def test_changed_and_noop_are_successful_prg(changed: bool, result: str) -> None:
    app, _, _, control, _ = make_app()
    control.changed = changed
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/server-settings",
            data={
                "csrf_token": csrf,
                "setting": "return_channel",
                "mode": "disabled",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert f"result={result}" in response.headers["location"]


@pytest.mark.parametrize(
    ("category", "query"),
    [
        (ServerSettingsControlCategory.INVALID_TARGET, "error=invalid_target"),
        (
            ServerSettingsControlCategory.CONTROL_UNAVAILABLE,
            "error=control_unavailable",
        ),
    ],
)
def test_control_errors_are_safe_redirects(
    category: ServerSettingsControlCategory,
    query: str,
) -> None:
    app, _, _, control, _ = make_app()
    control.change_error = ServerSettingsControlError(category)
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/server-settings",
            data={
                "csrf_token": csrf,
                "setting": "return_channel",
                "mode": "disabled",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert query in response.headers["location"]


def test_revoked_admin_post_is_fresh_denied_before_control() -> None:
    app, authorizer, _, control, _ = make_app()
    with authenticated_client(app, 50, WebAdminRole.ADMIN) as (client, csrf):
        authorizer.roles.pop(50)
        response = client.post(
            "/admin/server-settings",
            data={
                "csrf_token": csrf,
                "setting": "autorole_role",
                "mode": "env",
            },
        )
        assert len(app.state.web_session_store) == 0

    assert response.status_code == 403
    assert f"{SESSION_COOKIE_NAME}=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert control.options_calls == 0
    assert control.calls == []


def test_authorization_backend_failure_revokes_session_without_control_io() -> None:
    app, authorizer, _, control, _ = make_app()
    authorizer.error = RuntimeError("authorization unavailable")
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        response = client.post(
            "/admin/server-settings",
            data={
                "csrf_token": csrf,
                "setting": "autorole_role",
                "mode": "value",
                "value": "20",
            },
        )
        assert len(app.state.web_session_store) == 0

    assert response.status_code == 403
    assert f"{SESSION_COOKIE_NAME}=" in response.headers["set-cookie"]
    assert "Max-Age=0" in response.headers["set-cookie"]
    assert control.options_calls == 0
    assert control.calls == []


def test_write_rate_limit_uses_existing_bounded_limiter() -> None:
    app, _, _, _, _ = make_app()
    with authenticated_client(app, 41, WebAdminRole.OWNER) as (client, csrf):
        responses = [
            client.post(
                "/admin/server-settings",
                data={
                    "csrf_token": csrf,
                    "setting": "autorole_role",
                    "mode": "env",
                },
                follow_redirects=False,
            )
            for _ in range(11)
        ]
        assert len(app.state.web_session_store) == 1

    assert [response.status_code for response in responses[:10]] == [303] * 10
    assert responses[10].status_code == 429
