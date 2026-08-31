import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.testclient import TestClient

import discord_stats_bot.persistence.database as database_module
import discord_stats_bot.web.main as web_main_module
from discord_stats_bot.persistence.database import create_database_resources
from discord_stats_bot.web import WebSettings, create_app
from discord_stats_bot.web.app import _member_monogram
from discord_stats_bot.web.auth import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from discord_stats_bot.web.authorization import WebAdminRole
from discord_stats_bot.web.avatars import discord_member_avatar_url
from discord_stats_bot.web.dashboard import (
    DashboardBotOverview,
    DashboardBotStatus,
    DashboardControlStatus,
    DashboardServerOverview,
    WebAdminDashboard,
)
from discord_stats_bot.web.service import (
    AdminCounts,
    AdminMember,
    AdminMemberAchievement,
    AdminMemberDetail,
    AdminMemberDetailResult,
    AdminMemberDetailStatus,
    AdminMemberLifecycleEvent,
    AdminMemberOrder,
    AdminMemberSort,
    AdminMembersPage,
    WebAdminMembershipRepository,
    WebAdminService,
    WebDatabaseHealth,
)

DATABASE_URL = "postgresql+asyncpg://test:test@localhost:5432/test"
OAUTH_SETTINGS = {
    "WEB_ADMIN_DISCORD_CLIENT_ID": 123456789012345678,
    "WEB_ADMIN_DISCORD_CLIENT_SECRET": "test-client-secret",
    "WEB_ADMIN_DISCORD_REDIRECT_URI": (
        "http://localhost:8000/admin/auth/discord/callback"
    ),
    "WEB_ADMIN_COOKIE_SECURE": False,
}
GLOBAL_AVATAR_HASH = "0123456789abcdef0123456789abcdef"
GUILD_AVATAR_HASH = "abcdef0123456789abcdef0123456789"
ANIMATED_AVATAR_HASH = "a_0123456789abcdef0123456789abcdef"


def make_settings(**overrides: object) -> WebSettings:
    values: dict[str, object] = {
        "DATABASE_URL": DATABASE_URL,
        "DISCORD_GUILD_ID": 1,
        **OAUTH_SETTINGS,
        **overrides,
    }
    return WebSettings(_env_file=None, **values)


@contextmanager
def authenticated_client(
    app: Starlette,
    *,
    raise_server_exceptions: bool = True,
    role: WebAdminRole = WebAdminRole.OWNER,
) -> Iterator[TestClient]:
    issued = app.state.web_session_store.create(42, role=role)
    with TestClient(
        app,
        raise_server_exceptions=raise_server_exceptions,
    ) as client:
        client.cookies.set(
            SESSION_COOKIE_NAME,
            issued.session_id,
            path=SESSION_COOKIE_PATH,
        )
        yield client


def test_web_settings_require_database_and_guild_but_not_discord_token() -> None:
    settings = make_settings()

    assert settings.web_admin_host == "127.0.0.1"
    assert settings.web_admin_port == 8000
    assert settings.voice_min_session_seconds == 10
    assert settings.discord_guild_id == 1
    assert settings.log_level == "INFO"
    assert not hasattr(settings, "discord_token")


def test_web_settings_require_configured_guild() -> None:
    with pytest.raises(ValidationError) as exc_info:
        WebSettings(_env_file=None, DATABASE_URL=DATABASE_URL, **OAUTH_SETTINGS)

    assert {error["loc"][0] for error in exc_info.value.errors()} == {
        "DISCORD_GUILD_ID"
    }


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "example.com", "not-an-ip"])
def test_web_settings_rejects_wildcard_hostname_and_malformed_bind(host: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(WEB_ADMIN_HOST=host)


def test_web_settings_private_bind_requires_opt_in_and_secure_public_oauth() -> None:
    with pytest.raises(ValidationError, match="ALLOW_PRIVATE_BIND"):
        make_settings(WEB_ADMIN_HOST="192.168.1.10")
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        make_settings(
            WEB_ADMIN_HOST="192.168.1.10",
            WEB_ADMIN_ALLOW_PRIVATE_BIND=True,
        )

    configured = make_settings(
        WEB_ADMIN_HOST="192.168.1.10",
        WEB_ADMIN_ALLOW_PRIVATE_BIND=True,
        WEB_ADMIN_DISCORD_REDIRECT_URI=(
            "https://admin.example.com/admin/auth/discord/callback"
        ),
        WEB_ADMIN_COOKIE_SECURE=True,
    )
    assert configured.web_admin_host == "192.168.1.10"


@pytest.mark.parametrize("host", ["8.8.8.8", "169.254.1.1", "fe80::1"])
def test_web_settings_rejects_public_and_link_local_bind(host: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(
            WEB_ADMIN_HOST=host,
            WEB_ADMIN_ALLOW_PRIVATE_BIND=True,
        )


def test_web_settings_accepts_ipv6_ula_with_explicit_opt_in() -> None:
    settings = make_settings(
        WEB_ADMIN_HOST="fd00::1234",
        WEB_ADMIN_ALLOW_PRIVATE_BIND=True,
        WEB_ADMIN_DISCORD_REDIRECT_URI=(
            "https://admin.example.com/admin/auth/discord/callback"
        ),
        WEB_ADMIN_COOKIE_SECURE=True,
    )
    assert settings.web_admin_host == "fd00::1234"


@pytest.mark.parametrize("port", [0, 65_536])
def test_web_settings_reject_invalid_ports(port: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(WEB_ADMIN_PORT=port)


class FakeAdminService:
    def __init__(
        self,
        health: WebDatabaseHealth,
        counts: AdminCounts | None = None,
        members: AdminMembersPage | None = None,
        member_detail: AdminMemberDetailResult | None = None,
    ) -> None:
        self.health = health
        self.counts = counts
        self.members = members
        self.member_detail = member_detail or AdminMemberDetailResult(
            AdminMemberDetailStatus.NOT_FOUND
        )
        self.probe_calls = 0
        self.count_calls = 0
        self.member_calls: list[tuple[int, str, AdminMemberSort, AdminMemberOrder]] = []
        self.member_detail_calls: list[int] = []

    async def probe_database(self) -> WebDatabaseHealth:
        self.probe_calls += 1
        return self.health

    async def load_counts(self) -> AdminCounts | None:
        self.count_calls += 1
        return self.counts

    async def load_members(
        self,
        *,
        page: int,
        query: str,
        sort: AdminMemberSort,
        order: AdminMemberOrder,
    ) -> AdminMembersPage | None:
        self.member_calls.append((page, query, sort, order))
        return self.members

    async def load_member_detail(self, user_id: int) -> AdminMemberDetailResult:
        self.member_detail_calls.append(user_id)
        return self.member_detail


class FakeResources:
    def __init__(self) -> None:
        self.session_factory = object()
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1


class FakeDashboardService:
    def __init__(self, dashboard: WebAdminDashboard) -> None:
        self.dashboard = dashboard
        self.calls = 0

    async def load(self) -> WebAdminDashboard:
        self.calls += 1
        return self.dashboard


def test_admin_page_and_health_render_healthy_database_and_counts() -> None:
    settings = make_settings()
    resources = FakeResources()
    service = FakeAdminService(
        WebDatabaseHealth(True, 0.0124),
        AdminCounts(guilds=1, tracked_users=42, audit_events=314),
    )
    resource_calls: list[tuple[object, bool]] = []
    dashboard_service = FakeDashboardService(
        WebAdminDashboard(
            database_health=WebDatabaseHealth(True, 0.0124),
            server=DashboardServerOverview(
                guild_name="Kanami Guild",
                member_count=42,
                in_voice_count=7,
                active_voice_sessions=4,
                voice_today_seconds=84 * 3600,
                voice_last_30_days_seconds=1_284 * 3600,
            ),
            bot=DashboardBotOverview(
                DashboardBotStatus.UNKNOWN,
                DashboardControlStatus.UNAVAILABLE,
            ),
        )
    )

    def resource_factory(config: object, *, read_only: bool) -> FakeResources:
        resource_calls.append((config, read_only))
        return resources

    app = create_app(
        settings,
        resource_factory=resource_factory,  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
        dashboard_service_factory=lambda *args: dashboard_service,
    )

    with authenticated_client(app) as client:
        page = client.get("/admin/")
        health = client.get("/admin/health")
        page_write = client.post("/admin/")
        health_write = client.post("/admin/health")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Kanami Admin" in page.text
    assert 'href="/admin/members"' in page.text
    assert "<span>PostgreSQL</span><strong>Работает</strong>" in page.text
    assert "Kanami Guild" in page.text
    assert ">42<" in page.text
    assert ">7<" in page.text
    assert "84 ч 00 мин" in page.text
    assert "1284 ч 00 мин" in page.text
    assert health.status_code == 200
    assert health.json() == {"status": "healthy"}
    assert resource_calls == [(settings, False)]
    assert resources.dispose_calls == 1
    assert service.probe_calls == 1
    assert service.count_calls == 0
    assert dashboard_service.calls == 1
    assert page_write.status_code == 405
    assert health_write.status_code == 405


@pytest.mark.parametrize("role", [WebAdminRole.OWNER, WebAdminRole.ADMIN])
def test_members_route_renders_member_directory_sorting_and_search(
    role: WebAdminRole,
) -> None:
    resources = FakeResources()
    service = FakeAdminService(
        WebDatabaseHealth(True),
        members=AdminMembersPage(
            entries=(
                AdminMember(
                    guild_id=1,
                    user_id=42,
                    display_name="<Kanami & Friends>",
                    joined_at=datetime(2025, 1, 2, 3, 4, tzinfo=UTC),
                    voice_seconds=3_660,
                    message_count=123,
                    achievement_count=4,
                    username="kanami<script>",
                    avatar_hash=GLOBAL_AVATAR_HASH,
                    guild_avatar_hash=GUILD_AVATAR_HASH,
                ),
            ),
            total=120,
            page=2,
            page_size=50,
            query="42",
            sort=AdminMemberSort.VOICE,
            order=AdminMemberOrder.DESC,
        ),
    )
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: resources,  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app, role=role) as client:
        response = client.get("/admin/members?page=2&q=42&sort=voice&order=desc")
        write_response = client.post("/admin/members")

    assert response.status_code == 200
    assert "Участники" in response.text
    assert 'class="member-directory-summary"' in response.text
    assert "120 участников" in response.text
    assert "Страница 2 из 3" in response.text
    assert 'class="member-toolbar"' in response.text
    assert '<option value="voice" selected>Voice lifetime</option>' in response.text
    assert '<option value="desc" selected>По убыванию</option>' in response.text
    assert 'class="member-directory-list"' in response.text
    assert 'class="member-row"' in response.text
    assert 'class="member-monogram"' in response.text
    assert ">KA</div>" in response.text
    assert 'class="member-avatar"' in response.text
    assert (
        'src="https://cdn.discordapp.com/guilds/1/users/42/avatars/'
        f'{GUILD_AVATAR_HASH}.png?size=64"' in response.text
    )
    assert 'width="46" height="46" loading="lazy" decoding="async"' in response.text
    assert "@kanami&lt;script&gt;" in response.text
    assert "Discord ID · 42" in response.text
    assert "&lt;Kanami &amp; Friends&gt;" in response.text
    assert "<Kanami & Friends>" not in response.text
    assert response.text.count('href="/admin/members/42"') == 2
    assert ">Профиль</a>" in response.text
    assert "Не хранится" not in response.text
    assert "02.01.2025 03:04 UTC" in response.text
    assert "1 ч 01 мин" in response.text
    assert ">123</dd>" in response.text
    assert ">4</dd>" in response.text
    assert (
        "/admin/members?page=1&amp;sort=voice&amp;order=desc&amp;q=42" in response.text
    )
    assert (
        "/admin/members?page=3&amp;sort=voice&amp;order=desc&amp;q=42" in response.text
    )
    assert "← Назад" in response.text
    assert "2 / 3" in response.text
    assert "Далее →" in response.text
    assert service.member_calls == [
        (2, "42", AdminMemberSort.VOICE, AdminMemberOrder.DESC)
    ]
    assert write_response.status_code == 405
    assert resources.dispose_calls == 1


def test_members_route_handles_empty_list_and_invalid_page() -> None:
    resources = FakeResources()
    service = FakeAdminService(
        WebDatabaseHealth(True),
        members=AdminMembersPage((), total=0, page=1, page_size=50, query=""),
    )
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: resources,  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app) as client:
        response = client.get("/admin/members?page=not-a-number")

    assert response.status_code == 200
    assert "0 участников" in response.text
    assert "Участники не найдены" in response.text
    assert "Traceback" not in response.text
    assert service.member_calls == [(1, "", AdminMemberSort.NAME, AdminMemberOrder.ASC)]


def test_members_route_invalid_sort_order_falls_back_and_query_is_escaped() -> None:
    service = FakeAdminService(
        WebDatabaseHealth(True),
        members=AdminMembersPage((), 0, 1, 50, '<query & "unsafe">'),
    )
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app) as client:
        response = client.get(
            "/admin/members?q=%3Cquery%20%26%20%22unsafe%22%3E"
            "&sort=not-valid&order=sideways"
        )

    assert response.status_code == 200
    assert 'value="&lt;query &amp; &quot;unsafe&quot;&gt;"' in response.text
    assert '<option value="name" selected>Имя</option>' in response.text
    assert '<option value="asc" selected>По возрастанию</option>' in response.text
    assert ">Сбросить</a>" in response.text
    assert service.member_calls == [
        (
            1,
            '<query & "unsafe">',
            AdminMemberSort.NAME,
            AdminMemberOrder.ASC,
        )
    ]


def test_member_monogram_handles_unicode_and_neutral_fallback() -> None:
    assert _member_monogram("Док Тор") == "ДТ"
    assert _member_monogram("東京") == "東京"
    assert _member_monogram("ß ß") == "SS"
    assert _member_monogram(" <> ") == "?"


def test_discord_member_avatar_url_precedence_animation_and_validation() -> None:
    assert discord_member_avatar_url(
        guild_id=1,
        user_id=42,
        guild_avatar_hash=GUILD_AVATAR_HASH,
        avatar_hash=GLOBAL_AVATAR_HASH,
        size=64,
    ) == (
        f"https://cdn.discordapp.com/guilds/1/users/42/avatars/"
        f"{GUILD_AVATAR_HASH}.png?size=64"
    )
    assert discord_member_avatar_url(
        guild_id=1,
        user_id=42,
        guild_avatar_hash=None,
        avatar_hash=ANIMATED_AVATAR_HASH,
        size=256,
    ) == (f"https://cdn.discordapp.com/avatars/42/{ANIMATED_AVATAR_HASH}.gif?size=256")
    assert (
        discord_member_avatar_url(
            guild_id=1,
            user_id=42,
            guild_avatar_hash="https://evil.example/avatar.png",
            avatar_hash="../../unsafe",
            size=64,
        )
        is None
    )


def test_member_directory_renders_global_avatar_and_monogram_fallback() -> None:
    service = FakeAdminService(
        WebDatabaseHealth(True),
        members=AdminMembersPage(
            entries=(
                AdminMember(
                    1,
                    42,
                    "Global avatar",
                    None,
                    0,
                    0,
                    0,
                    avatar_hash=GLOBAL_AVATAR_HASH,
                ),
                AdminMember(1, 43, "Fallback member", None, 0, 0, 0),
            ),
            total=2,
            page=1,
            page_size=50,
            query="",
        ),
    )
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app) as client:
        response = client.get("/admin/members")

    assert response.status_code == 200
    assert (
        f"https://cdn.discordapp.com/avatars/42/{GLOBAL_AVATAR_HASH}.png?size=64"
        in response.text
    )
    assert response.text.count('class="member-avatar-image"') == 1
    assert ">FM</div>" in response.text
    assert "evil.example" not in response.text
    assert (
        discord_member_avatar_url(
            guild_id=1,
            user_id=42,
            guild_avatar_hash=GUILD_AVATAR_HASH,
            avatar_hash=GLOBAL_AVATAR_HASH,
            size=63,
        )
        is None
    )


def test_members_route_handles_database_failure_without_details() -> None:
    resources = FakeResources()
    service = FakeAdminService(WebDatabaseHealth(False), members=None)
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: resources,  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app, raise_server_exceptions=False) as client:
        response = client.get("/admin/members")

    assert response.status_code == 503
    assert "PostgreSQL недоступна" in response.text
    assert "Traceback" not in response.text


def member_detail(*, departed: bool = False) -> AdminMemberDetail:
    joined_at = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)
    return AdminMemberDetail(
        guild_id=1,
        user_id=42,
        display_name="<Kana & Co>",
        username="user<script>",
        global_name="Global & Name",
        nickname="<Kana & Co>",
        joined_at=joined_at,
        left_at=(datetime(2026, 1, 2, 3, 4, tzinfo=UTC) if departed else None),
        voice_seconds=3_660,
        message_count=123,
        achievements=(
            AdminMemberAchievement(
                key="voice_10_hours",
                title="В эфире & сейчас",
                tier="bronze",
                unlocked_at=joined_at,
            ),
            AdminMemberAchievement(
                key="legacy_<key>",
                title=None,
                tier=None,
                unlocked_at=joined_at,
            ),
        ),
        lifecycle_events=(
            AdminMemberLifecycleEvent(
                "member.returned",
                datetime(2026, 2, 1, tzinfo=UTC),
                absence_seconds=172_800,
                return_number=2,
            ),
            AdminMemberLifecycleEvent(
                "member.left",
                datetime(2026, 1, 2, tzinfo=UTC),
            ),
            AdminMemberLifecycleEvent(
                "member.<unsafe>",
                datetime(2025, 1, 2, tzinfo=UTC),
            ),
        ),
        avatar_hash=GLOBAL_AVATAR_HASH,
        guild_avatar_hash=GUILD_AVATAR_HASH,
    )


def test_member_detail_route_renders_active_profile_and_escapes_data() -> None:
    resources = FakeResources()
    service = FakeAdminService(
        WebDatabaseHealth(True),
        member_detail=AdminMemberDetailResult(
            AdminMemberDetailStatus.FOUND,
            member_detail(),
        ),
    )
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: resources,  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app) as client:
        response = client.get("/admin/members/42")
        method_responses = [
            client.request(method, "/admin/members/42")
            for method in ("POST", "PUT", "PATCH", "DELETE")
        ]

    assert response.status_code == 200
    assert "На сервере" in response.text
    assert 'class="profile-hero"' in response.text
    assert 'class="profile-monogram"' in response.text
    assert 'class="profile-avatar"' in response.text
    assert (
        'src="https://cdn.discordapp.com/guilds/1/users/42/avatars/'
        f'{GUILD_AVATAR_HASH}.png?size=256"' in response.text
    )
    assert 'width="96" height="96" decoding="async"' in response.text
    assert 'class="profile-identity"' in response.text
    assert 'class="profile-stat-grid"' in response.text
    assert 'class="achievement-list"' in response.text
    assert 'class="achievement-card"' in response.text
    assert 'class="lifecycle-timeline"' in response.text
    assert "&lt;Kana &amp; Co&gt;" in response.text
    assert "user&lt;script&gt;" in response.text
    assert "Global &amp; Name" in response.text
    assert "<Kana & Co>" not in response.text
    assert "1 ч 01 мин" in response.text
    assert ">123<" in response.text
    assert "В эфире &amp; сейчас" in response.text
    assert "legacy_&lt;key&gt;" in response.text
    assert "Архивное достижение" in response.text
    assert "member.returned" in response.text
    assert "Вернулся на сервер" in response.text
    assert "Покинул сервер" in response.text
    assert "Событие участника" in response.text
    assert "member.&lt;unsafe&gt;" in response.text
    assert "Отсутствовал: 2 д" in response.text
    assert "Возвращение №2" in response.text
    assert "details_data" not in response.text
    assert "before_data" not in response.text
    assert "after_data" not in response.text
    assert service.member_detail_calls == [42]
    assert all(item.status_code == 405 for item in method_responses)
    assert "Usernameuser" not in response.text
    assert "<dt>Username</dt><dd>user&lt;script&gt;</dd>" in response.text
    assert "<script" not in response.text.casefold()
    assert "script-src 'none'" in response.headers["content-security-policy"]


def test_member_directory_and_profile_css_cover_narrow_structural_layouts() -> None:
    service = FakeAdminService(
        WebDatabaseHealth(True),
        member_detail=AdminMemberDetailResult(
            AdminMemberDetailStatus.FOUND,
            member_detail(),
        ),
    )
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app) as client:
        response = client.get("/admin/members/42")

    assert "html { min-width: 320px" in response.text
    assert ".member-toolbar {" in response.text
    assert ".member-row {" in response.text
    assert ".profile-hero {" in response.text
    assert ".profile-identity, .profile-membership" in response.text
    assert ".achievement-list" in response.text
    assert ".lifecycle-timeline" in response.text
    assert ".member-avatar-image" in response.text
    assert "object-fit: cover" in response.text
    assert "max-width: 100%" in response.text
    member_breakpoint = response.text.index("@media (max-width: 1100px)")
    navigation_breakpoint = response.text.index("@media (max-width: 900px)")
    wide_breakpoint = response.text.index("@media (min-width: 1200px)")
    member_compact_css = response.text[member_breakpoint:navigation_breakpoint]
    mobile_navigation_css = response.text[navigation_breakpoint:wide_breakpoint]
    assert member_breakpoint < navigation_breakpoint
    assert (
        ".member-toolbar { grid-template-columns: minmax(220px, 1fr) "
        "repeat(2, minmax(145px, 0.55fr)); }" in member_compact_css
    )
    assert ".member-toolbar-actions { grid-column: 1 / -1; }" in member_compact_css
    assert (
        ".member-row { grid-template-columns: 50px minmax(0, 1fr) auto; "
        "align-items: start; }" in member_compact_css
    )
    assert (
        ".member-stats { grid-column: 2 / -1; grid-template-columns: "
        "repeat(4, minmax(0, 1fr)); }" in member_compact_css
    )
    assert (
        ".member-profile-action { grid-column: 3; grid-row: 1; }" in member_compact_css
    )
    assert ".app-shell { display: block; }" in response.text[navigation_breakpoint:]
    assert (
        ".member-profile-action { grid-column: 1 / -1; grid-row: auto; "
        "width: 100%; }" in mobile_navigation_css
    )
    assert "@media (max-width: 640px)" in response.text
    assert (
        ".profile-identity, .profile-membership, .profile-stat-grid, "
        ".achievement-list { grid-template-columns: 1fr; }" in response.text
    )
    assert "@media (max-width: 430px)" in response.text
    assert ".member-stats { grid-template-columns: 1fr; }" in response.text
    assert "overflow-wrap: anywhere" in response.text
    assert "<script" not in response.text.casefold()
    assert "onerror=" not in response.text.casefold()


def test_member_detail_route_renders_departed_and_empty_states() -> None:
    detail = member_detail(departed=True)
    detail = AdminMemberDetail(
        guild_id=detail.guild_id,
        user_id=detail.user_id,
        display_name="42",
        username=None,
        global_name=None,
        nickname=None,
        joined_at=None,
        left_at=detail.left_at,
        voice_seconds=0,
        message_count=0,
        achievements=(),
        lifecycle_events=(),
        avatar_hash=detail.avatar_hash,
        guild_avatar_hash=detail.guild_avatar_hash,
    )
    service = FakeAdminService(
        WebDatabaseHealth(True),
        member_detail=AdminMemberDetailResult(
            AdminMemberDetailStatus.FOUND,
            detail,
        ),
    )
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app) as client:
        response = client.get("/admin/members/42")

    assert response.status_code == 200
    assert "Покинул сервер" in response.text
    assert "Нет открытых достижений" in response.text
    assert "История вступлений и выходов отсутствует" in response.text
    assert "0 сек" in response.text
    assert f"{GUILD_AVATAR_HASH}.png?size=256" in response.text


@pytest.mark.parametrize(
    "path", ["/admin/members/0", "/admin/members/9223372036854775808"]
)
def test_member_detail_invalid_bigint_returns_404_without_query(path: str) -> None:
    service = FakeAdminService(WebDatabaseHealth(True))
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app) as client:
        response = client.get(path)

    assert response.status_code == 404
    assert service.member_detail_calls == []


def test_member_detail_malformed_path_uses_starlette_404() -> None:
    service = FakeAdminService(WebDatabaseHealth(True))
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app) as client:
        response = client.get("/admin/members/not-a-number")

    assert response.status_code == 404
    assert service.member_detail_calls == []


@pytest.mark.parametrize(
    ("status", "expected_status"),
    [
        (AdminMemberDetailStatus.NOT_FOUND, 404),
        (AdminMemberDetailStatus.UNAVAILABLE, 503),
    ],
)
def test_member_detail_route_handles_not_found_and_database_failure_safely(
    status: AdminMemberDetailStatus,
    expected_status: int,
) -> None:
    service = FakeAdminService(
        WebDatabaseHealth(False),
        member_detail=AdminMemberDetailResult(status),
    )
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app, raise_server_exceptions=False) as client:
        response = client.get("/admin/members/42")

    assert response.status_code == expected_status
    assert "Traceback" not in response.text
    assert DATABASE_URL not in response.text
    assert "credentials" not in response.text


def test_database_failure_returns_503_json_and_safe_html_without_traceback() -> None:
    resources = FakeResources()
    service = FakeAdminService(WebDatabaseHealth(False))
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: resources,  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app, raise_server_exceptions=False) as client:
        health = client.get("/admin/health")
        page = client.get("/admin/")

    assert health.status_code == 503
    assert health.json() == {"status": "unhealthy"}
    assert page.status_code == 200
    assert "<span>PostgreSQL</span><strong>Недоступна</strong>" in page.text
    assert "Сводка сервера временно недоступна" in page.text
    assert "Traceback" not in page.text
    assert service.count_calls == 0
    assert resources.dispose_calls == 1


class SequenceMonotonic:
    def __init__(self, *values: float) -> None:
        self._values: Iterator[float] = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class FakeResult:
    def __init__(self, row: object | None = None) -> None:
        self._row = row

    def one(self) -> object:
        assert self._row is not None
        return self._row


class FakeSession:
    def __init__(
        self,
        statements: list[object],
        *,
        error: Exception | None = None,
        count_row: object | None = None,
    ) -> None:
        self._statements = statements
        self._error = error
        self._count_row = count_row
        self.closed = False

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def execute(self, statement: object) -> FakeResult:
        self._statements.append(statement)
        if self._error is not None:
            raise self._error
        return FakeResult(self._count_row)


class FakeSessionFactory:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        count_row: object | None = None,
    ) -> None:
        self.error = error
        self.count_row = count_row
        self.statements: list[object] = []
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        session = FakeSession(
            self.statements,
            error=self.error,
            count_row=self.count_row,
        )
        self.sessions.append(session)
        return session


@pytest.mark.asyncio
async def test_health_service_executes_real_select_one_and_measures_latency() -> None:
    sessions = FakeSessionFactory()
    service = WebAdminService(  # type: ignore[arg-type]
        sessions,
        guild_id=1,
        monotonic=SequenceMonotonic(10.0, 10.025),
    )

    health = await service.probe_database()

    assert health.available is True
    assert health.latency_seconds == pytest.approx(0.025)
    assert str(sessions.statements[0]) == "SELECT 1"
    assert sessions.sessions[0].closed is True


@pytest.mark.asyncio
async def test_health_service_hides_database_exception_and_closes_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions = FakeSessionFactory(error=RuntimeError("secret database detail"))
    service = WebAdminService(  # type: ignore[arg-type]
        sessions,
        guild_id=1,
        monotonic=SequenceMonotonic(10.0),
    )

    health = await service.probe_database()

    assert health == WebDatabaseHealth(False)
    assert sessions.sessions[0].closed is True
    assert "error_type=RuntimeError" in caplog.text
    assert "secret database detail" not in caplog.text


@pytest.mark.asyncio
async def test_count_query_is_one_bounded_select_over_existing_models() -> None:
    sessions = FakeSessionFactory(
        count_row=SimpleNamespace(guilds=1, tracked_users=42, audit_events=314)
    )
    service = WebAdminService(sessions, guild_id=1)  # type: ignore[arg-type]

    counts = await service.load_counts()
    sql = " ".join(str(sessions.statements[0]).split())

    assert counts == AdminCounts(1, 42, 314)
    assert sql.startswith("SELECT")
    assert "FROM guilds" in sql
    assert "FROM discord_users" in sql
    assert "FROM audit_events" in sql
    assert all(keyword not in sql.upper() for keyword in ("INSERT", "UPDATE", "DELETE"))
    assert sessions.sessions[0].closed is True


class FakeMembersResult:
    def __init__(self, rows: tuple[object, ...]) -> None:
        self._rows = rows

    def all(self) -> tuple[object, ...]:
        return self._rows


class FakeMembersSession:
    def __init__(
        self,
        statements: list[object],
        *,
        total: int = 1,
        rows: tuple[object, ...] = (),
        row_batches: tuple[tuple[object, ...], ...] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._statements = statements
        self._total = total
        self._rows = rows
        self._row_batches = iter(row_batches) if row_batches is not None else None
        self._error = error
        self.closed = False

    async def __aenter__(self) -> "FakeMembersSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True

    async def scalar(self, statement: object) -> int:
        self._statements.append(statement)
        if self._error is not None:
            raise self._error
        return self._total

    async def execute(self, statement: object) -> FakeMembersResult:
        self._statements.append(statement)
        if self._error is not None:
            raise self._error
        rows = next(self._row_batches) if self._row_batches is not None else self._rows
        return FakeMembersResult(rows)


class FakeMembersSessionFactory:
    def __init__(
        self,
        *,
        total: int = 1,
        rows: tuple[object, ...] = (),
        row_batches: tuple[tuple[object, ...], ...] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.statements: list[object] = []
        self.session = FakeMembersSession(
            self.statements,
            total=total,
            rows=rows,
            row_batches=row_batches,
            error=error,
        )

    def __call__(self) -> FakeMembersSession:
        return self.session


@pytest.mark.asyncio
async def test_members_service_uses_two_bounded_selects_and_shared_aggregates() -> None:
    joined_at = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)
    sessions = FakeMembersSessionFactory(
        total=51,
        rows=(
            SimpleNamespace(
                guild_id=1,
                user_id=42,
                display_name="Kanami",
                username="kanami",
                avatar_hash=GLOBAL_AVATAR_HASH,
                guild_avatar_hash=GUILD_AVATAR_HASH,
                joined_at=joined_at,
                voice_seconds=7_200,
                message_count=123,
                achievement_count=4,
            ),
        ),
    )
    service = WebAdminService(  # type: ignore[arg-type]
        sessions,
        guild_id=1,
        clock=lambda: datetime(2026, 8, 21, 12, tzinfo=UTC),
        min_session_seconds=10,
    )

    result = await service.load_members(page=2, query="42")

    assert result == AdminMembersPage(
        entries=(
            AdminMember(
                1,
                42,
                "Kanami",
                joined_at,
                7_200,
                123,
                4,
                "kanami",
                GLOBAL_AVATAR_HASH,
                GUILD_AVATAR_HASH,
            ),
        ),
        total=51,
        page=2,
        page_size=50,
        query="42",
    )
    assert len(sessions.statements) == 2
    count_sql, rows_sql = (
        " ".join(str(statement).split()) for statement in sessions.statements
    )
    assert count_sql.startswith("WITH filtered_admin_members AS")
    assert "SELECT count(*)" in count_sql
    assert "guild_members.left_at IS NULL" in count_sql
    assert "guild_members.guild_id =" in count_sql
    assert "discord_users.is_bot IS false" in count_sql
    assert "discord_users.id =" in count_sql
    assert "coalesce(nullif(guild_members.nickname" in count_sql.lower()
    assert "nullif(discord_users.global_name" in count_sql.lower()
    assert "nullif(discord_users.username" in count_sql.lower()
    assert "CAST(discord_users.id AS TEXT)" in count_sql
    assert "discord_users.avatar_hash" in count_sql
    assert "guild_members.guild_avatar_hash" in count_sql
    assert rows_sql.startswith("WITH filtered_admin_members AS")
    assert "LIMIT" in rows_sql and "OFFSET" in rows_sql
    assert "FROM voice_intervals JOIN voice_sessions" in rows_sql
    assert "JOIN filtered_admin_members ON" in rows_sql
    assert "voice_intervals.is_afk IS false" in rows_sql
    assert "voice_intervals.quality =" in rows_sql
    assert "voice_sessions.confirmed_through_at" in rows_sql
    assert "FROM daily_text_activity JOIN filtered_admin_members ON" in rows_sql
    assert "sum(daily_text_activity.message_count)" in rows_sql
    assert "FROM user_achievements JOIN filtered_admin_members ON" in rows_sql
    assert "count(*) AS achievement_count" in rows_sql
    assert rows_sql.count("JOIN filtered_admin_members ON") >= 3
    assert "enriched_admin_members AS" in rows_sql
    assert (
        "ORDER BY lower(enriched_admin_members.display_name) ASC, "
        "enriched_admin_members.user_id ASC" in rows_sql
    )
    assert rows_sql.index("enriched_admin_members AS") < rows_sql.rindex("LIMIT")
    assert all(
        keyword not in f"{count_sql} {rows_sql}".upper()
        for keyword in ("INSERT", "UPDATE", "DELETE")
    )
    assert sessions.session.closed is True


@pytest.mark.parametrize(
    ("sort", "order", "expected_order"),
    [
        (
            AdminMemberSort.NAME,
            AdminMemberOrder.DESC,
            "lower(enriched_admin_members.display_name) DESC",
        ),
        (
            AdminMemberSort.JOINED,
            AdminMemberOrder.ASC,
            "enriched_admin_members.joined_at ASC NULLS LAST",
        ),
        (
            AdminMemberSort.VOICE,
            AdminMemberOrder.DESC,
            "enriched_admin_members.voice_seconds DESC",
        ),
        (
            AdminMemberSort.MESSAGES,
            AdminMemberOrder.DESC,
            "enriched_admin_members.message_count DESC",
        ),
        (
            AdminMemberSort.ACHIEVEMENTS,
            AdminMemberOrder.DESC,
            "enriched_admin_members.achievement_count DESC",
        ),
    ],
)
@pytest.mark.asyncio
async def test_members_service_applies_allowlisted_global_sort_before_pagination(
    sort: AdminMemberSort,
    order: AdminMemberOrder,
    expected_order: str,
) -> None:
    sessions = FakeMembersSessionFactory(
        rows=(
            SimpleNamespace(
                guild_id=1,
                user_id=42,
                display_name="Kanami",
                username="kanami",
                avatar_hash=None,
                guild_avatar_hash=None,
                joined_at=datetime(2025, 1, 2, tzinfo=UTC),
                voice_seconds=7_200,
                message_count=123,
                achievement_count=4,
            ),
        )
    )
    service = WebAdminService(sessions, guild_id=1)  # type: ignore[arg-type]

    result = await service.load_members(
        page=1,
        query="",
        sort=sort,
        order=order,
    )

    assert result is not None
    assert result.sort is sort
    assert result.order is order
    rows_sql = " ".join(str(sessions.statements[1]).split())
    assert expected_order in rows_sql
    assert f"{expected_order}, enriched_admin_members.user_id ASC" in rows_sql
    assert rows_sql.index("enriched_admin_members AS") < rows_sql.rindex("LIMIT")


@pytest.mark.asyncio
async def test_members_service_name_search_is_case_insensitive_and_escaped() -> None:
    sessions = FakeMembersSessionFactory(total=0)
    service = WebAdminService(sessions, guild_id=1)  # type: ignore[arg-type]

    result = await service.load_members(page=1, query="Ka%_\\Name")

    assert result == AdminMembersPage((), 0, 1, 50, "Ka%_\\Name")
    sql = " ".join(str(sessions.statements[0]).split())
    assert "lower(guild_members.nickname) LIKE lower(" in sql
    assert "lower(discord_users.global_name) LIKE lower(" in sql
    assert "lower(discord_users.username) LIKE lower(" in sql
    assert "ESCAPE '\\'" in sql


def test_members_route_legacy_identity_falls_back_to_discord_id() -> None:
    resources = FakeResources()
    service = FakeAdminService(
        WebDatabaseHealth(True),
        members=AdminMembersPage(
            entries=(AdminMember(1, 42, "42", None, 0, 0, 0),),
            total=1,
            page=1,
            page_size=50,
            query="",
        ),
    )
    app = create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: resources,  # type: ignore[arg-type]
        service_factory=lambda session_factory: service,
    )

    with authenticated_client(app) as client:
        response = client.get("/admin/members")

    assert response.status_code == 200
    assert response.text.count("Discord ID · 42") == 1
    assert 'href="/admin/members/42">42</a>' in response.text
    assert "Не хранится" not in response.text


@pytest.mark.asyncio
async def test_members_service_empty_result_uses_only_count_select() -> None:
    sessions = FakeMembersSessionFactory(total=0)
    service = WebAdminService(sessions, guild_id=1)  # type: ignore[arg-type]

    result = await service.load_members(page=999, query="")

    assert result == AdminMembersPage((), 0, 1, 50, "")
    assert len(sessions.statements) == 1
    assert (
        str(sessions.statements[0])
        .lstrip()
        .startswith("WITH filtered_admin_members AS")
    )


@pytest.mark.asyncio
async def test_members_service_hides_database_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions = FakeMembersSessionFactory(error=RuntimeError("secret credentials"))
    service = WebAdminService(sessions, guild_id=1)  # type: ignore[arg-type]

    result = await service.load_members(page=1, query="")

    assert result is None
    assert "error_type=RuntimeError" in caplog.text
    assert "secret credentials" not in caplog.text
    assert sessions.session.closed is True


def profile_row(
    *,
    achievement_key: str | None = None,
    unlocked_at: datetime | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        guild_id=1,
        user_id=42,
        display_name="Kana",
        username="kanami",
        global_name="Kanami",
        nickname="Kana",
        avatar_hash=GLOBAL_AVATAR_HASH,
        guild_avatar_hash=GUILD_AVATAR_HASH,
        joined_at=datetime(2025, 1, 2, tzinfo=UTC),
        left_at=None,
        voice_seconds=7_200,
        message_count=123,
        achievement_key=achievement_key,
        unlocked_at=unlocked_at,
    )


@pytest.mark.asyncio
async def test_member_detail_service_uses_two_scoped_read_only_statements() -> None:
    unlocked_at = datetime(2026, 1, 2, tzinfo=UTC)
    sessions = FakeMembersSessionFactory(
        row_batches=(
            (
                profile_row(
                    achievement_key="voice_10_hours",
                    unlocked_at=unlocked_at,
                ),
                profile_row(
                    achievement_key="retired_achievement",
                    unlocked_at=unlocked_at,
                ),
            ),
            (
                SimpleNamespace(
                    id=4,
                    event_type="member.returned",
                    occurred_at=datetime(2026, 3, 1, tzinfo=UTC),
                    details_data={
                        "absence_seconds": 172_800,
                        "return_number": 2,
                        "unsafe": "must-not-render",
                    },
                ),
                SimpleNamespace(
                    id=3,
                    event_type="member.left",
                    occurred_at=datetime(2026, 2, 1, tzinfo=UTC),
                    details_data=None,
                ),
            ),
        )
    )
    service = WebAdminService(  # type: ignore[arg-type]
        sessions,
        guild_id=1,
        clock=lambda: datetime(2026, 8, 21, tzinfo=UTC),
        min_session_seconds=10,
    )

    result = await service.load_member_detail(42)

    assert result.status is AdminMemberDetailStatus.FOUND
    assert result.detail is not None
    assert result.detail.voice_seconds == 7_200
    assert result.detail.message_count == 123
    assert result.detail.achievement_count == 2
    assert result.detail.achievements[0].title == "В эфире"
    assert result.detail.achievements[0].tier == "bronze"
    assert result.detail.achievements[1].title is None
    assert result.detail.achievements[1].tier is None
    assert result.detail.lifecycle_events[0].absence_seconds == 172_800
    assert result.detail.lifecycle_events[0].return_number == 2
    assert result.detail.lifecycle_events[1].absence_seconds is None
    assert len(sessions.statements) == 2
    profile_sql, lifecycle_sql = (
        " ".join(str(statement).split()) for statement in sessions.statements
    )
    assert profile_sql.startswith("WITH admin_member_scope AS")
    assert "guild_members.guild_id =" in profile_sql
    assert "guild_members.user_id =" in profile_sql
    assert "discord_users.is_bot IS false" in profile_sql
    assert "guild_members.left_at IS NULL" not in profile_sql
    assert "discord_users.avatar_hash" in profile_sql
    assert "guild_members.guild_avatar_hash" in profile_sql
    assert "JOIN admin_member_scope ON" in profile_sql
    assert "voice_intervals.is_afk IS false" in profile_sql
    assert "voice_sessions.confirmed_through_at" in profile_sql
    assert "sum(daily_text_activity.message_count)" in profile_sql
    assert profile_sql.index("admin_member_profile_totals AS") < profile_sql.index(
        "LEFT OUTER JOIN user_achievements"
    )
    assert "audit_events.event_type IN" in lifecycle_sql
    assert "audit_events.occurred_at DESC, audit_events.id DESC" in lifecycle_sql
    assert "LIMIT" in lifecycle_sql
    assert all(
        keyword not in f"{profile_sql} {lifecycle_sql}".upper()
        for keyword in ("INSERT", "UPDATE", "DELETE")
    )


@pytest.mark.asyncio
async def test_member_detail_service_not_found_uses_one_statement() -> None:
    sessions = FakeMembersSessionFactory(row_batches=((),))
    service = WebAdminService(sessions, guild_id=1)  # type: ignore[arg-type]

    result = await service.load_member_detail(42)

    assert result == AdminMemberDetailResult(AdminMemberDetailStatus.NOT_FOUND)
    assert len(sessions.statements) == 1


@pytest.mark.asyncio
async def test_member_detail_service_handles_one_achievement_and_no_history() -> None:
    unlocked_at = datetime(2026, 1, 2, tzinfo=UTC)
    sessions = FakeMembersSessionFactory(
        row_batches=(
            (
                profile_row(
                    achievement_key="voice_10_hours",
                    unlocked_at=unlocked_at,
                ),
            ),
            (),
        )
    )
    service = WebAdminService(sessions, guild_id=1)  # type: ignore[arg-type]

    result = await service.load_member_detail(42)

    assert result.detail is not None
    assert result.detail.achievement_count == 1
    assert result.detail.lifecycle_events == ()


@pytest.mark.asyncio
async def test_member_detail_service_rejects_invalid_ids_without_sql() -> None:
    sessions = FakeMembersSessionFactory()
    service = WebAdminService(sessions, guild_id=1)  # type: ignore[arg-type]

    zero = await service.load_member_detail(0)
    oversized = await service.load_member_detail(9_223_372_036_854_775_808)

    assert zero.status is AdminMemberDetailStatus.NOT_FOUND
    assert oversized.status is AdminMemberDetailStatus.NOT_FOUND
    assert sessions.statements == []


@pytest.mark.asyncio
async def test_member_detail_service_ignores_malformed_return_details() -> None:
    sessions = FakeMembersSessionFactory(
        row_batches=(
            (profile_row(),),
            tuple(
                SimpleNamespace(
                    id=index,
                    event_type="member.returned",
                    occurred_at=datetime(2026, 1, index, tzinfo=UTC),
                    details_data=details,
                )
                for index, details in enumerate(
                    (
                        None,
                        {"absence_seconds": -1, "return_number": 0},
                        {"absence_seconds": True, "return_number": "2"},
                    ),
                    start=1,
                )
            ),
        )
    )
    service = WebAdminService(sessions, guild_id=1)  # type: ignore[arg-type]

    result = await service.load_member_detail(42)

    assert result.detail is not None
    assert all(
        event.absence_seconds is None and event.return_number is None
        for event in result.detail.lifecycle_events
    )


@pytest.mark.asyncio
async def test_member_detail_service_hides_database_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions = FakeMembersSessionFactory(error=RuntimeError("secret credentials"))
    service = WebAdminService(sessions, guild_id=1)  # type: ignore[arg-type]

    result = await service.load_member_detail(42)

    assert result.status is AdminMemberDetailStatus.UNAVAILABLE
    assert "error_type=RuntimeError" in caplog.text
    assert "secret credentials" not in caplog.text


def test_database_resources_enable_postgresql_read_only_mode_only_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class FakeEngine:
        async def dispose(self) -> None:
            pass

    def fake_create_engine(url: str, **kwargs: object) -> FakeEngine:
        calls.append((url, kwargs))
        return FakeEngine()

    monkeypatch.setattr(database_module, "create_async_engine", fake_create_engine)

    create_database_resources(make_settings())
    create_database_resources(make_settings(), read_only=True)

    assert "connect_args" not in calls[0][1]
    assert calls[1][1]["connect_args"] == {
        "server_settings": {"default_transaction_read_only": "on"}
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgresql_member_detail_query_and_guild_scope() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    resources = create_database_resources(
        make_settings(
            DATABASE_URL=database_url,
            DISCORD_GUILD_ID=10,
        )
    )
    try:
        async with resources.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                statements = (
                    "CREATE TEMP TABLE discord_users (id BIGINT PRIMARY KEY, "
                    "is_bot BOOLEAN NOT NULL, username TEXT, global_name TEXT, "
                    "avatar_hash TEXT)",
                    "CREATE TEMP TABLE guild_members (guild_id BIGINT NOT NULL, "
                    "user_id BIGINT NOT NULL, joined_at TIMESTAMPTZ, "
                    "left_at TIMESTAMPTZ, nickname TEXT, guild_avatar_hash TEXT, "
                    "PRIMARY KEY (guild_id, user_id))",
                    "CREATE TEMP TABLE voice_sessions (id BIGINT PRIMARY KEY, "
                    "guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                    "started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ, "
                    "confirmed_through_at TIMESTAMPTZ NOT NULL)",
                    "CREATE TEMP TABLE voice_intervals (id BIGINT PRIMARY KEY, "
                    "session_id BIGINT NOT NULL, guild_id BIGINT NOT NULL, "
                    "user_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, "
                    "started_at TIMESTAMPTZ NOT NULL, ended_at TIMESTAMPTZ, "
                    "quality TEXT NOT NULL, is_afk BOOLEAN NOT NULL)",
                    "CREATE TEMP TABLE daily_text_activity (guild_id BIGINT NOT NULL, "
                    "user_id BIGINT NOT NULL, channel_id BIGINT NOT NULL, "
                    "activity_date DATE NOT NULL, message_count BIGINT NOT NULL)",
                    "CREATE TEMP TABLE user_achievements (guild_id BIGINT NOT NULL, "
                    "user_id BIGINT NOT NULL, achievement_key TEXT NOT NULL, "
                    "unlocked_at TIMESTAMPTZ NOT NULL, "
                    "PRIMARY KEY (guild_id, user_id, achievement_key))",
                    "CREATE TEMP TABLE audit_events (id BIGINT PRIMARY KEY, "
                    "guild_id BIGINT NOT NULL, event_type TEXT NOT NULL, "
                    "occurred_at TIMESTAMPTZ NOT NULL, subject_type TEXT NOT NULL, "
                    "subject_id BIGINT, details_data JSONB NOT NULL)",
                )
                for statement in statements:
                    await connection.execute(text(statement))
                await connection.execute(
                    text(
                        "INSERT INTO discord_users VALUES "
                        "(19, false, 'no-activity', 'No Activity', NULL), "
                        "(20, false, 'active-user', 'Active Global', "
                        f"'{GLOBAL_AVATAR_HASH}'), "
                        "(21, false, NULL, NULL, "
                        f"'{GLOBAL_AVATAR_HASH}'), "
                        "(22, false, 'global-only', NULL, NULL), "
                        "(23, false, 'other-guild', NULL, NULL), "
                        "(24, true, 'bot', NULL, NULL), "
                        "(25, false, 'global-user', 'Global Fallback', NULL), "
                        "(26, false, 'Username Fallback', NULL, NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO guild_members VALUES "
                        "(10, 19, '2024-01-01T00:00:00+00', NULL, "
                        "'No Activity', NULL), "
                        "(10, 20, '2025-01-01T00:00:00+00', NULL, 'Active Nick', "
                        f"'{GUILD_AVATAR_HASH}'), "
                        "(10, 21, '2025-01-02T00:00:00+00', "
                        "'2026-01-02T00:00:00+00', NULL, "
                        f"'{GUILD_AVATAR_HASH}'), "
                        "(11, 23, '2025-01-03T00:00:00+00', NULL, 'Other', NULL), "
                        "(10, 24, '2025-01-04T00:00:00+00', NULL, 'Bot', NULL), "
                        "(10, 25, NULL, NULL, NULL, NULL), "
                        "(10, 26, NULL, NULL, NULL, NULL)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_sessions VALUES "
                        "(1, 10, 20, '2026-01-01T00:00:00+00', "
                        "'2026-01-01T00:02:30+00', '2026-01-01T00:02:30+00'), "
                        "(2, 10, 20, '2026-01-02T00:00:00+00', "
                        "'2026-01-02T00:00:05+00', '2026-01-02T00:00:05+00'), "
                        "(3, 10, 20, '2026-01-03T00:00:00+00', "
                        "'2026-01-03T00:10:00+00', '2026-01-03T00:10:00+00')"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO voice_intervals VALUES "
                        "(1, 1, 10, 20, 30, '2026-01-01T00:00:00+00', "
                        "'2026-01-01T00:02:00+00', 'exact', false), "
                        "(2, 1, 10, 20, 30, '2026-01-01T00:02:00+00', "
                        "'2026-01-01T00:02:30+00', 'estimated', false), "
                        "(3, 2, 10, 20, 30, '2026-01-02T00:00:00+00', "
                        "'2026-01-02T00:00:05+00', 'exact', false), "
                        "(4, 3, 10, 20, 99, '2026-01-03T00:00:00+00', "
                        "'2026-01-03T00:10:00+00', 'exact', true)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO daily_text_activity VALUES "
                        "(10, 20, 30, '2026-01-01', 4), "
                        "(10, 20, 31, '2026-01-02', 6)"
                    )
                )
                await connection.execute(
                    text(
                        "INSERT INTO user_achievements VALUES "
                        "(10, 20, 'voice_10_hours', '2026-01-10T00:00:00+00'), "
                        "(10, 20, 'retired_achievement', '2026-01-11T00:00:00+00')"
                    )
                )
                lifecycle_values = [
                    {
                        "id": event_id,
                        "event_type": (
                            "member.returned"
                            if event_id == 22
                            else "member.left"
                            if event_id == 21
                            else "member.joined"
                        ),
                        "occurred_at": datetime(
                            2026,
                            1,
                            21 if event_id >= 20 else event_id,
                            tzinfo=UTC,
                        ),
                        "details": (
                            '{"absence_seconds":172800,"return_number":2}'
                            if event_id == 22
                            else "{}"
                        ),
                    }
                    for event_id in range(1, 23)
                ]
                await connection.execute(
                    text(
                        "INSERT INTO audit_events "
                        "(id, guild_id, event_type, occurred_at, subject_type, "
                        "subject_id, details_data) VALUES "
                        "(:id, 10, :event_type, :occurred_at, 'user', 20, "
                        "CAST(:details AS JSONB))"
                    ),
                    lifecycle_values,
                )

                class BoundSessionFactory:
                    def __call__(self) -> AsyncSession:
                        return AsyncSession(bind=connection, expire_on_commit=False)

                service = WebAdminService(
                    BoundSessionFactory(),  # type: ignore[arg-type]
                    guild_id=10,
                    clock=lambda: datetime(2026, 8, 21, tzinfo=UTC),
                    min_session_seconds=10,
                )
                active = await service.load_member_detail(20)
                departed = await service.load_member_detail(21)
                global_only = await service.load_member_detail(22)
                other_guild = await service.load_member_detail(23)
                bot = await service.load_member_detail(24)
                global_fallback = await service.load_member_detail(25)
                username_fallback = await service.load_member_detail(26)
                voice_sorted = await service.load_members(
                    page=1,
                    query="",
                    sort=AdminMemberSort.VOICE,
                    order=AdminMemberOrder.DESC,
                    page_size=1,
                )
                membership_repository = WebAdminMembershipRepository(
                    BoundSessionFactory(),  # type: ignore[arg-type]
                    guild_id=10,
                )

                assert active.status is AdminMemberDetailStatus.FOUND
                assert active.detail is not None
                assert active.detail.display_name == "Active Nick"
                assert active.detail.voice_seconds == 150
                assert active.detail.message_count == 10
                assert active.detail.achievement_count == 2
                assert active.detail.avatar_hash == GLOBAL_AVATAR_HASH
                assert active.detail.guild_avatar_hash == GUILD_AVATAR_HASH
                assert len(active.detail.lifecycle_events) == 20
                assert active.detail.lifecycle_events[0].event_type == "member.returned"
                assert active.detail.lifecycle_events[1].event_type == "member.left"
                assert active.detail.lifecycle_events[2].event_type == "member.joined"
                assert departed.status is AdminMemberDetailStatus.FOUND
                assert departed.detail is not None
                assert departed.detail.display_name == "21"
                assert departed.detail.left_at is not None
                assert departed.detail.avatar_hash == GLOBAL_AVATAR_HASH
                assert departed.detail.guild_avatar_hash == GUILD_AVATAR_HASH
                assert global_only.status is AdminMemberDetailStatus.NOT_FOUND
                assert other_guild.status is AdminMemberDetailStatus.NOT_FOUND
                assert bot.status is AdminMemberDetailStatus.NOT_FOUND
                assert global_fallback.detail is not None
                assert global_fallback.detail.display_name == "Global Fallback"
                assert username_fallback.detail is not None
                assert username_fallback.detail.display_name == "Username Fallback"
                assert voice_sorted is not None
                assert voice_sorted.total == 4
                assert [entry.user_id for entry in voice_sorted.entries] == [20]
                assert voice_sorted.entries[0].voice_seconds == 150
                assert voice_sorted.entries[0].avatar_hash == GLOBAL_AVATAR_HASH
                assert voice_sorted.entries[0].guild_avatar_hash == GUILD_AVATAR_HASH
                assert await membership_repository.is_current_non_bot_member(20) is True
                assert (
                    await membership_repository.is_current_non_bot_member(21) is False
                )
                assert (
                    await membership_repository.is_current_non_bot_member(22) is False
                )
                assert (
                    await membership_repository.is_current_non_bot_member(23) is False
                )
                assert (
                    await membership_repository.is_current_non_bot_member(24) is False
                )
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgresql_web_resources_are_really_read_only() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    resources = create_database_resources(
        make_settings(DATABASE_URL=database_url),
        read_only=True,
    )
    try:
        health = await WebAdminService(
            resources.session_factory, guild_id=1
        ).probe_database()
        async with resources.session_factory() as session:
            transaction_mode = await session.scalar(text("SHOW transaction_read_only"))
    finally:
        await resources.dispose()

    assert health.available is True
    assert transaction_mode == "on"


def test_web_entrypoint_runs_uvicorn_on_configured_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        WEB_ADMIN_HOST="127.0.0.1",
        WEB_ADMIN_PORT=8123,
        LOG_LEVEL="WARNING",
    )
    calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(web_main_module, "WebSettings", lambda: settings)
    monkeypatch.setattr(
        web_main_module.uvicorn,
        "run",
        lambda app, **kwargs: calls.append((app, kwargs)),
    )

    assert web_main_module.main() == 0
    assert calls[0][1] == {
        "host": "127.0.0.1",
        "port": 8123,
        "log_level": "warning",
        "access_log": False,
        "proxy_headers": False,
    }


def test_web_entrypoint_warns_for_explicit_private_bind(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(
        WEB_ADMIN_HOST="192.168.50.10",
        WEB_ADMIN_ALLOW_PRIVATE_BIND=True,
        WEB_ADMIN_DISCORD_REDIRECT_URI=(
            "https://kanami.example.com/admin/auth/discord/callback"
        ),
        WEB_ADMIN_COOKIE_SECURE=True,
    )
    monkeypatch.setattr(web_main_module, "WebSettings", lambda: settings)
    monkeypatch.setattr(web_main_module, "create_app", lambda configured: object())
    monkeypatch.setattr(web_main_module.uvicorn, "run", lambda app, **kwargs: None)
    assert web_main_module.main() == 0
    output = capsys.readouterr().out
    assert "private non-loopback interface" in output
    assert "firewall and trusted reverse proxy" in output
