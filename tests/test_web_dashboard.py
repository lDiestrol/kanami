import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.applications import Starlette
from starlette.testclient import TestClient

from discord_stats_bot.features.bot_profile import (
    BotGuildProfile,
    BotProfileErrorCategory,
    BotProfileOperationError,
)
from discord_stats_bot.features.voice_statistics import (
    VoiceChannelUsageEntry,
    VoiceLeaderboardEntry,
    VoiceServerStatistics,
    VoiceStatisticsPeriod,
)
from discord_stats_bot.persistence.database import create_database_resources
from discord_stats_bot.web import WebSettings, create_app
from discord_stats_bot.web.auth import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from discord_stats_bot.web.authorization import WebAdminRole
from discord_stats_bot.web.dashboard import (
    DashboardBotOverview,
    DashboardBotStatus,
    DashboardControlStatus,
    DashboardOverviewCounts,
    DashboardServerOverview,
    SqlAlchemyDashboardOverviewRepository,
    WebAdminDashboard,
    WebAdminDashboardService,
    format_dashboard_voice_duration,
)
from discord_stats_bot.web.presentation import ADMIN_STYLES, render_navigation
from discord_stats_bot.web.service import (
    AdminMemberDetailResult,
    AdminMemberDetailStatus,
    AdminMembersPage,
    WebDatabaseHealth,
)

DATABASE_URL = "postgresql+asyncpg://test:test@localhost:5432/test"
AS_OF = datetime(2026, 8, 24, 20, 30, tzinfo=UTC)


def make_settings(**overrides: object) -> WebSettings:
    values: dict[str, object] = {
        "DATABASE_URL": DATABASE_URL,
        "DISCORD_GUILD_ID": 10,
        "WEB_ADMIN_DISCORD_CLIENT_ID": 123456789012345678,
        "WEB_ADMIN_DISCORD_CLIENT_SECRET": "test-client-secret",
        "WEB_ADMIN_DISCORD_REDIRECT_URI": (
            "http://localhost:8000/admin/auth/discord/callback"
        ),
        "WEB_ADMIN_COOKIE_SECURE": False,
        "REPORT_TIMEZONE": "Asia/Yekaterinburg",
    }
    values.update(overrides)
    return WebSettings(
        _env_file=None,
        **values,
    )


class FakeResources:
    session_factory = object()

    async def dispose(self) -> None:
        return None


class FakeAdminService:
    async def probe_database(self) -> WebDatabaseHealth:
        return WebDatabaseHealth(True)

    async def load_counts(self) -> None:
        return None

    async def load_members(
        self, *, page: int, query: str, **kwargs: object
    ) -> AdminMembersPage:
        return AdminMembersPage((), 0, page, 50, query)

    async def load_member_detail(self, user_id: int) -> AdminMemberDetailResult:
        del user_id
        return AdminMemberDetailResult(AdminMemberDetailStatus.NOT_FOUND)


class StaticDashboardService:
    def __init__(self, dashboard: WebAdminDashboard) -> None:
        self.dashboard = dashboard
        self.calls = 0

    async def load(self) -> WebAdminDashboard:
        self.calls += 1
        return self.dashboard


def dashboard_fixture() -> WebAdminDashboard:
    return WebAdminDashboard(
        database_health=WebDatabaseHealth(True),
        server=DashboardServerOverview(
            guild_name="<Kanami & Friends>",
            member_count=53,
            in_voice_count=7,
            active_voice_sessions=4,
            voice_today_seconds=84 * 3600 + 5 * 60,
            voice_last_30_days_seconds=1_284 * 3600 + 9 * 60,
        ),
        bot=DashboardBotOverview(
            DashboardBotStatus.ONLINE,
            DashboardControlStatus.AVAILABLE,
            display_name="<Kanami Bot>",
        ),
    )


def make_app(dashboard: StaticDashboardService) -> Starlette:
    return create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: FakeAdminService(),
        dashboard_service_factory=lambda *args: dashboard,
    )


@contextmanager
def authenticated_client(app: Starlette, role: WebAdminRole) -> Iterator[TestClient]:
    issued = app.state.web_session_store.create(42, role=role)
    with TestClient(app) as client:
        client.cookies.set(
            SESSION_COOKIE_NAME,
            issued.session_id,
            path=SESSION_COOKIE_PATH,
        )
        yield client


def test_dashboard_requires_authentication_without_loading_data() -> None:
    dashboard = StaticDashboardService(dashboard_fixture())
    app = make_app(dashboard)

    with TestClient(app) as client:
        response = client.get("/admin/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert dashboard.calls == 0


def test_root_redirects_to_admin_without_loading_dashboard() -> None:
    dashboard = StaticDashboardService(dashboard_fixture())
    app = make_app(dashboard)

    with TestClient(app) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/"
    assert dashboard.calls == 0


def test_root_redirect_does_not_bypass_admin_authentication() -> None:
    dashboard = StaticDashboardService(dashboard_fixture())
    app = make_app(dashboard)

    with TestClient(app) as client:
        root = client.get("/", follow_redirects=False)
        admin = client.get(root.headers["location"], follow_redirects=False)

    assert admin.status_code == 303
    assert admin.headers["location"] == "/admin/login"
    assert dashboard.calls == 0


@pytest.mark.parametrize("role", [WebAdminRole.OWNER, WebAdminRole.ADMIN])
def test_owner_and_managed_admin_can_view_server_dashboard(role: WebAdminRole) -> None:
    dashboard = StaticDashboardService(dashboard_fixture())
    app = make_app(dashboard)

    with authenticated_client(app, role) as client:
        response = client.get("/admin/")

    assert response.status_code == 200
    assert "Dashboard" in response.text
    assert "&lt;Kanami &amp; Friends&gt;" in response.text
    assert "<Kanami & Friends>" not in response.text
    assert "&lt;Kanami Bot&gt;" in response.text
    assert ">53<" in response.text
    assert ">7<" in response.text
    assert ">4<" in response.text
    assert "84 ч 05 мин" in response.text
    assert "1284 ч 09 мин" in response.text
    assert (
        '<div class="health-signal success"><span>Kanami</span><strong>Онлайн</strong>'
        in response.text
    )
    assert (
        '<div class="health-signal success"><span>PostgreSQL</span><strong>Работает</strong>'
        in response.text
    )
    assert (
        '<div class="health-signal success"><span>Bot Control</span><strong>Доступен</strong>'
        in response.text
    )
    assert 'class="metric usage-metric"' in response.text
    assert response.text.count('class="metric usage-metric"') == 2
    assert 'href="/admin/members"' in response.text
    assert 'href="/admin/settings/bot-profile"' in response.text
    assert 'href="/admin/server-settings"' in response.text
    assert ('href="/admin/administrators"' in response.text) == (
        role is WebAdminRole.OWNER
    )
    assert ('href="/admin/audit"' in response.text) == (role is WebAdminRole.OWNER)
    assert 'class="app-shell"' in response.text
    assert 'class="sidebar"' in response.text
    assert 'class="desktop-navigation-shell"' in response.text
    assert 'class="navigation desktop-navigation"' in response.text
    assert '<details class="mobile-menu">' in response.text
    assert '<summary class="mobile-menu-summary">' in response.text
    assert 'aria-label="Мобильная навигация"' in response.text
    assert '<span class="mobile-menu-label">Меню</span>' in response.text
    assert 'aria-current="page" href="/admin/"' in response.text
    assert "--bg-root" in response.text
    assert "--accent" in response.text
    assert "prefers-reduced-motion" in response.text


def test_shared_navigation_marks_one_nested_destination_and_escapes_csrf() -> None:
    owner_navigation = render_navigation(
        WebAdminRole.OWNER,
        'unsafe"><token',
        active_path="/admin/rules/12",
    )
    admin_navigation = render_navigation(
        WebAdminRole.ADMIN,
        "safe",
        active_path="/admin/rules/12",
    )

    assert owner_navigation.count('aria-current="page"') == 2
    assert owner_navigation.count('aria-current="page" href="/admin/rules"') == 2
    assert owner_navigation.count('value="unsafe&quot;&gt;&lt;token"') == 2
    assert (
        owner_navigation.count(
            '<form method="post" action="/admin/logout" class="logout">'
        )
        == 2
    )
    assert '<details class="mobile-menu">' in owner_navigation
    assert '<summary class="mobile-menu-summary">' in owner_navigation
    assert 'class="navigation desktop-navigation"' in owner_navigation
    assert 'class="navigation mobile-navigation"' in owner_navigation
    assert '<span class="role-badge">OWNER</span>' in owner_navigation
    assert '<span class="role-badge">ADMIN</span>' in admin_navigation
    assert 'href="/admin/administrators"' in owner_navigation
    assert 'href="/admin/administrators"' not in admin_navigation


def test_navigation_responsive_breakpoint_contract() -> None:
    default_css, after_900 = ADMIN_STYLES.split("@media (max-width: 900px) {", 1)
    max_900_css, after_1200 = after_900.split("@media (min-width: 1200px) {", 1)
    _, after_700 = after_1200.split("@media (max-width: 700px) {", 1)
    _, after_640 = after_700.split("@media (max-width: 640px) {", 1)
    max_640_css, after_430 = after_640.split("@media (max-width: 430px) {", 1)
    max_430_css, _ = after_430.split("@media (prefers-reduced-motion: reduce)", 1)

    assert ".desktop-navigation-shell { display: none; }" not in default_css
    assert ".mobile-menu { display: block; width: 100%; }" not in default_css
    assert ".desktop-navigation-shell { display: none; }" in max_900_css
    assert ".mobile-menu { display: block; width: 100%; }" in max_900_css
    assert ".mobile-menu-summary {" in max_900_css
    assert ".mobile-menu-panel {" in max_900_css
    assert (
        ".navigation { grid-template-columns: repeat(3, minmax(0, 1fr));" in max_900_css
    )
    assert (
        ".member-profile-action { grid-column: 1 / -1; grid-row: auto; "
        "width: 100%; }" in max_900_css
    )
    assert "body { font-size: 14px; }" not in max_900_css
    assert ".navigation { grid-template-columns: 1fr 1fr; }" in max_640_css
    assert "body { font-size: 14px; }" in max_640_css
    assert ".navigation { grid-template-columns: 1fr; }" in max_430_css


class FakeProbe:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.calls = 0

    async def probe_database(self) -> WebDatabaseHealth:
        self.calls += 1
        return WebDatabaseHealth(self.available)


class FakeSession:
    def __init__(self) -> None:
        self.connection_options: list[dict[str, object]] = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def connection(self, **kwargs: object) -> object:
        self.connection_options.append(kwargs)
        return object()


class FakeSessionFactory:
    def __init__(self) -> None:
        self.calls = 0
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        self.calls += 1
        session = FakeSession()
        self.sessions.append(session)
        return session


class FakeOverviewRepository:
    def __init__(self, result: DashboardOverviewCounts) -> None:
        self.result = result
        self.calls: list[int] = []

    async def load(self, guild_id: int) -> DashboardOverviewCounts:
        self.calls.append(guild_id)
        return self.result


class RecordingVoiceRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[int, VoiceStatisticsPeriod, object]] = []

    async def get_server_statistics(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: object,
    ) -> VoiceServerStatistics:
        self.calls.append((guild_id, period, query))
        seconds = 3_600 if period is VoiceStatisticsPeriod.TODAY else 30 * 3_600
        return VoiceServerStatistics(
            as_of=AS_OF,
            period=period,
            exact_seconds=seconds,
            estimated_seconds=60,
            active_users=1,
            top_user=VoiceLeaderboardEntry(20, seconds, 60),
            top_channel=VoiceChannelUsageEntry(30, seconds, 60),
        )


class FakeBotControl:
    def __init__(self, error: BotProfileOperationError | None = None) -> None:
        self.error = error
        self.calls = 0

    async def get_profile(self) -> BotGuildProfile:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return BotGuildProfile(1, "Kanami", "Kanami", None, None, None)


def make_dashboard_service(
    *,
    probe: FakeProbe | None = None,
    bot: FakeBotControl | None = None,
    counts: DashboardOverviewCounts | None = None,
) -> tuple[
    WebAdminDashboardService,
    FakeSessionFactory,
    FakeOverviewRepository,
    RecordingVoiceRepository,
    FakeBotControl,
]:
    sessions = FakeSessionFactory()
    overview = FakeOverviewRepository(
        counts or DashboardOverviewCounts("Kanami", 53, 7, 4)
    )
    voice = RecordingVoiceRepository()
    bot = bot or FakeBotControl()
    service = WebAdminDashboardService(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
        min_session_seconds=10,
        database_probe=probe or FakeProbe(),
        bot_control=bot,  # type: ignore[arg-type]
        overview_repository_factory=lambda session: overview,
        voice_repository_factory=lambda session: voice,  # type: ignore[arg-type]
        clock=lambda: AS_OF,
    )
    return service, sessions, overview, voice, bot


@pytest.mark.asyncio
async def test_dashboard_service_uses_existing_voice_semantics_and_boundaries() -> None:
    service, sessions, overview, voice, bot = make_dashboard_service()

    dashboard = await service.load()

    assert dashboard.server == DashboardServerOverview(
        "Kanami", 53, 7, 4, 3_660, 108_060
    )
    assert dashboard.bot.status is DashboardBotStatus.ONLINE
    assert dashboard.bot.control_status is DashboardControlStatus.AVAILABLE
    assert sessions.calls == 1
    assert overview.calls == [10]
    assert bot.calls == 1
    assert [call[:2] for call in voice.calls] == [
        (10, VoiceStatisticsPeriod.TODAY),
        (10, VoiceStatisticsPeriod.LAST_30_DAYS),
    ]
    today_query = voice.calls[0][2]
    assert today_query.today_started_at == datetime(2026, 8, 24, 19, tzinfo=UTC)  # type: ignore[attr-defined]
    assert today_query.last_30_days_started_at == AS_OF - timedelta(days=30)  # type: ignore[attr-defined]
    assert voice.calls[1][2] is not today_query
    assert voice.calls[1][2] == today_query


@pytest.mark.asyncio
async def test_empty_database_values_render_as_zero_without_failure() -> None:
    service, _, _, _, _ = make_dashboard_service(
        counts=DashboardOverviewCounts(None, 0, 0, 0)
    )

    dashboard = await service.load()

    assert dashboard.server is not None
    assert dashboard.server.member_count == 0
    assert format_dashboard_voice_duration(0) == "0 мин"


@pytest.mark.asyncio
async def test_unavailable_bot_control_does_not_hide_postgresql_overview() -> None:
    bot = FakeBotControl(
        BotProfileOperationError(BotProfileErrorCategory.CONTROL_UNAVAILABLE)
    )
    service, _, _, _, _ = make_dashboard_service(bot=bot)

    dashboard = await service.load()

    assert dashboard.server is not None
    assert dashboard.database_health.available
    assert dashboard.bot.status is DashboardBotStatus.UNKNOWN
    assert dashboard.bot.control_status is DashboardControlStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_unavailable_database_does_not_skip_bot_status_probe() -> None:
    probe = FakeProbe(available=False)
    service, sessions, _, _, bot = make_dashboard_service(probe=probe)

    dashboard = await service.load()

    assert dashboard.server is None
    assert not dashboard.database_health.available
    assert dashboard.bot.status is DashboardBotStatus.ONLINE
    assert sessions.calls == 0
    assert bot.calls == 1


def test_dashboard_renders_independent_failure_states_without_synthetic_health() -> (
    None
):
    dashboard = StaticDashboardService(
        WebAdminDashboard(
            database_health=WebDatabaseHealth(False),
            server=None,
            bot=DashboardBotOverview(
                DashboardBotStatus.ONLINE,
                DashboardControlStatus.UNAVAILABLE,
            ),
        )
    )
    app = make_app(dashboard)

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/")

    assert response.status_code == 200
    assert "<span>Kanami</span><strong>Онлайн</strong>" in response.text
    assert "<span>PostgreSQL</span><strong>Недоступна</strong>" in response.text
    assert "<span>Bot Control</span><strong>Недоступен</strong>" in response.text
    assert "Сводка сервера временно недоступна" in response.text
    assert "не подменяются оценочными значениями" in response.text


class FakeResult:
    def one(self) -> object:
        return SimpleNamespace(
            guild_name="Guild",
            member_count=53,
            in_voice_count=7,
            active_voice_sessions=4,
        )


class RecordingSqlSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement: object) -> FakeResult:
        self.statements.append(statement)
        return FakeResult()


@pytest.mark.asyncio
async def test_dashboard_repository_maps_one_set_based_current_state_query() -> None:
    session = RecordingSqlSession()
    repository = SqlAlchemyDashboardOverviewRepository(session)  # type: ignore[arg-type]

    result = await repository.load(10)

    assert result == DashboardOverviewCounts("Guild", 53, 7, 4)
    assert len(session.statements) == 1
    sql = str(session.statements[0])
    assert "guild_members" in sql
    assert "voice_intervals" in sql
    assert "voice_sessions" in sql
    assert "is_afk IS false" in sql


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgresql_dashboard_overview_aggregates_configured_guild() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is not configured")

    resources = create_database_resources(make_settings(DATABASE_URL=database_url))
    try:
        async with resources.engine.connect() as connection:
            transaction = await connection.begin()
            try:
                statements = (
                    "CREATE TEMP TABLE guilds (id BIGINT PRIMARY KEY, name TEXT)",
                    "CREATE TEMP TABLE discord_users "
                    "(id BIGINT PRIMARY KEY, is_bot BOOLEAN NOT NULL)",
                    "CREATE TEMP TABLE guild_members "
                    "(guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                    "left_at TIMESTAMPTZ)",
                    "CREATE TEMP TABLE voice_sessions "
                    "(id BIGINT PRIMARY KEY, guild_id BIGINT NOT NULL, "
                    "user_id BIGINT NOT NULL, ended_at TIMESTAMPTZ)",
                    "CREATE TEMP TABLE voice_intervals "
                    "(guild_id BIGINT NOT NULL, user_id BIGINT NOT NULL, "
                    "ended_at TIMESTAMPTZ, is_afk BOOLEAN NOT NULL)",
                    "INSERT INTO guilds VALUES (10, 'Kanami'), (11, 'Other')",
                    "INSERT INTO discord_users VALUES "
                    "(20, false), (21, false), (22, false), (23, true), "
                    "(24, false)",
                    "INSERT INTO guild_members VALUES "
                    "(10, 20, NULL), (10, 21, NULL), "
                    "(10, 22, '2026-01-01T00:00:00+00'), "
                    "(10, 23, NULL), (11, 24, NULL)",
                    "INSERT INTO voice_sessions VALUES "
                    "(1, 10, 20, NULL), (2, 10, 21, NULL), "
                    "(3, 10, 22, NULL), (4, 10, 23, NULL), "
                    "(5, 11, 24, NULL)",
                    "INSERT INTO voice_intervals VALUES "
                    "(10, 20, NULL, false), (10, 21, NULL, true), "
                    "(10, 22, NULL, false), (10, 23, NULL, false), "
                    "(11, 24, NULL, false)",
                )
                for statement in statements:
                    await connection.execute(text(statement))

                async with AsyncSession(bind=connection) as session:
                    result = await SqlAlchemyDashboardOverviewRepository(session).load(
                        10
                    )

                assert result == DashboardOverviewCounts("Kanami", 2, 1, 2)
            finally:
                await transaction.rollback()
    finally:
        await resources.dispose()
