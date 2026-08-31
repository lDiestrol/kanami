from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from discord_stats_bot.features.game_tracking import (
    ServerGameDailyPoint,
    ServerGameStatistics,
    ServerGameStatisticsPeriod,
    ServerGameTopGame,
    ServerGameTopPlayer,
    build_server_game_statistics_window,
)
from discord_stats_bot.web import WebSettings, create_app
from discord_stats_bot.web.auth import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from discord_stats_bot.web.authorization import WebAdminRole
from discord_stats_bot.web.server_game_analytics import (
    SERVER_GAME_ANALYTICS_TRANSACTION_SETUP_SQL,
    WebAdminServerGameAnalyticsService,
)
from discord_stats_bot.web.service import (
    AdminMemberDetailResult,
    AdminMemberDetailStatus,
    AdminMembersPage,
    WebDatabaseHealth,
)

DATABASE_URL = "postgresql+asyncpg://test:test@localhost:5432/test"
AS_OF = datetime(2026, 8, 31, 12, tzinfo=UTC)


def make_settings() -> WebSettings:
    return WebSettings(
        _env_file=None,
        DATABASE_URL=DATABASE_URL,
        DISCORD_GUILD_ID=10,
        WEB_ADMIN_DISCORD_CLIENT_ID=123456789012345678,
        WEB_ADMIN_DISCORD_CLIENT_SECRET="test-client-secret",
        WEB_ADMIN_DISCORD_REDIRECT_URI=(
            "http://localhost:8000/admin/auth/discord/callback"
        ),
        WEB_ADMIN_COOKIE_SECURE=False,
        REPORT_TIMEZONE="Asia/Yekaterinburg",
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
        return AdminMemberDetailResult(AdminMemberDetailStatus.NOT_FOUND)


class StaticServerGameService:
    def __init__(self, report: ServerGameStatistics | None) -> None:
        self.report = report
        self.periods: list[ServerGameStatisticsPeriod] = []

    async def load(
        self, period: ServerGameStatisticsPeriod
    ) -> ServerGameStatistics | None:
        self.periods.append(period)
        return self.report


def make_report(
    period: ServerGameStatisticsPeriod = ServerGameStatisticsPeriod.LAST_30_DAYS,
    *,
    populated: bool = True,
) -> ServerGameStatistics:
    timezone = ZoneInfo("Asia/Yekaterinburg")
    window = build_server_game_statistics_window(
        period,
        AS_OF,
        report_timezone=timezone,
    )
    daily = tuple(
        ServerGameDailyPoint(
            window.started_on + timedelta(days=offset),
            (offset + 1) * 600 if populated else 0,
            2 if populated else 0,
        )
        for offset in range(period.days)
    )
    return ServerGameStatistics(
        period=period,
        window=window,
        total_seconds=10_800 if populated else 0,
        active_gamers=2 if populated else 0,
        unique_games=2 if populated else 0,
        average_seconds_per_gamer=5400 if populated else 0,
        daily=daily,
        top_games=(
            (
                ServerGameTopGame("unsafe", '<Game & "unsafe">', 7200, 2),
                ServerGameTopGame("minecraft", "Minecraft", 3600, 1),
            )
            if populated
            else ()
        ),
        top_players=(
            (
                ServerGameTopPlayer(101, '<Player & "unsafe">', 7200, 2, 3),
                ServerGameTopPlayer(202, "Player Two", 3600, 1, 1),
            )
            if populated
            else ()
        ),
        earliest_recorded_on=datetime(2026, 8, 20).date(),
        period_may_be_partial=True,
    )


def make_app(service: StaticServerGameService) -> Starlette:
    return create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: FakeAdminService(),
        server_game_analytics_service_factory=lambda *args: service,  # type: ignore[arg-type]
    )


@contextmanager
def authenticated_client(
    app: Starlette,
    role: WebAdminRole,
) -> Iterator[TestClient]:
    issued = app.state.web_session_store.create(42, role=role)
    with TestClient(app) as client:
        client.cookies.set(
            SESSION_COOKIE_NAME,
            issued.session_id,
            path=SESSION_COOKIE_PATH,
        )
        yield client


def test_server_games_requires_authentication_without_loading_report() -> None:
    service = StaticServerGameService(make_report())
    with TestClient(make_app(service)) as client:
        response = client.get("/admin/games", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert service.periods == []


@pytest.mark.parametrize("role", [WebAdminRole.OWNER, WebAdminRole.ADMIN])
def test_server_games_populated_page_is_authorized_escaped_and_server_rendered(
    role: WebAdminRole,
) -> None:
    service = StaticServerGameService(make_report())
    with authenticated_client(make_app(service), role) as client:
        response = client.get("/admin/games")

    assert response.status_code == 200
    assert service.periods == [ServerGameStatisticsPeriod.LAST_30_DAYS]
    assert "Server Game Analytics" in response.text
    assert 'aria-current="page" href="/admin/games"' in response.text
    assert 'data-server-game-kpi="time"' in response.text
    assert "3 ч" in response.text
    assert 'data-server-game-kpi="players"' in response.text
    assert "Игровая активность по дням" in response.text
    assert response.text.count('class="daily-point daily-point-games"') == 30
    assert "История выбранного периода может быть неполной" in response.text
    assert "&lt;Game &amp; &quot;unsafe&quot;&gt;" in response.text
    assert "&lt;Player &amp; &quot;unsafe&quot;&gt;" in response.text
    assert 'href="/admin/members/101"' in response.text
    assert "Доля: 66.7%" in response.text
    assert "<script" not in response.text.casefold()
    assert "javascript:" not in response.text.casefold()
    assert "script-src 'none'" in response.headers["content-security-policy"]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", ServerGameStatisticsPeriod.LAST_30_DAYS),
        ("?period=7d", ServerGameStatisticsPeriod.LAST_7_DAYS),
        ("?period=30d", ServerGameStatisticsPeriod.LAST_30_DAYS),
        ("?period=90d", ServerGameStatisticsPeriod.LAST_90_DAYS),
        ("?period=", ServerGameStatisticsPeriod.LAST_30_DAYS),
        ("?period=invalid", ServerGameStatisticsPeriod.LAST_30_DAYS),
        (
            "?period=7d&period=90d",
            ServerGameStatisticsPeriod.LAST_30_DAYS,
        ),
    ],
)
def test_server_game_period_shapes_default_safely(
    query: str,
    expected: ServerGameStatisticsPeriod,
) -> None:
    service = StaticServerGameService(make_report())
    with authenticated_client(make_app(service), WebAdminRole.ADMIN) as client:
        response = client.get(f"/admin/games{query}")

    assert response.status_code == 200
    assert service.periods == [expected]
    assert (
        f'href="/admin/games?period={expected.value}" aria-current="page"'
        in response.text
    )


def test_server_games_90d_selector_chart_and_responsive_css() -> None:
    report = make_report(ServerGameStatisticsPeriod.LAST_90_DAYS)
    service = StaticServerGameService(report)
    with authenticated_client(make_app(service), WebAdminRole.OWNER) as client:
        response = client.get("/admin/games?period=90d")

    assert response.text.count('class="daily-point daily-point-games"') == 90
    assert "daily-chart-90" in response.text
    assert ".daily-chart-90 { min-width: 2700px" in response.text
    assert ".server-game-kpi-grid { grid-template-columns: 1fr; }" in response.text
    assert (
        ".analytics-chart-scroll { max-width: 100%; overflow-x: auto;" in response.text
    )


def test_server_games_empty_and_unavailable_states_are_controlled() -> None:
    empty = StaticServerGameService(make_report(populated=False))
    with authenticated_client(make_app(empty), WebAdminRole.ADMIN) as client:
        empty_response = client.get("/admin/games")
    assert empty_response.status_code == 200
    assert "За выбранный период игровой активности не найдено." in empty_response.text

    unavailable = StaticServerGameService(None)
    with authenticated_client(make_app(unavailable), WebAdminRole.ADMIN) as client:
        unavailable_response = client.get("/admin/games")
    assert unavailable_response.status_code == 503
    assert "Игровая аналитика временно недоступна" in unavailable_response.text
    assert "Traceback" not in unavailable_response.text
    assert "SELECT" not in unavailable_response.text


def test_zero_total_with_top_games_renders_neutral_share_without_error() -> None:
    report = replace(
        make_report(),
        total_seconds=0,
        average_seconds_per_gamer=0,
        top_games=(ServerGameTopGame("subsecond", "Subsecond Game", 0, 1),),
    )
    service = StaticServerGameService(report)

    with authenticated_client(make_app(service), WebAdminRole.ADMIN) as client:
        response = client.get("/admin/games")

    assert response.status_code == 200
    assert "Subsecond Game" in response.text
    assert "Доля: —" in response.text
    assert "ZeroDivisionError" not in response.text


class FakeSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement: object) -> object:
        self.statements.append(str(statement))
        return object()


class FakeSessionContext:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> FakeSession:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_web_service_uses_one_read_only_snapshot_and_configured_guild() -> None:
    session = FakeSession()
    report = make_report()
    calls: list[tuple[int, ServerGameStatisticsPeriod, datetime]] = []
    repository_sessions: list[FakeSession] = []

    class DomainService:
        async def get_report(  # type: ignore[no-untyped-def]
            self, guild_id, period, as_of
        ):
            calls.append((guild_id, period, as_of))
            return report

    result = await WebAdminServerGameAnalyticsService(
        lambda: FakeSessionContext(session),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
        repository_factory=lambda value: repository_sessions.append(value) or object(),
        domain_service_factory=lambda *args: DomainService(),
        clock=lambda: AS_OF,
    ).load(ServerGameStatisticsPeriod.LAST_90_DAYS)

    assert result is report
    assert session.statements == [SERVER_GAME_ANALYTICS_TRANSACTION_SETUP_SQL]
    assert repository_sessions == [session]
    assert calls == [(10, ServerGameStatisticsPeriod.LAST_90_DAYS, AS_OF)]


@pytest.mark.asyncio
async def test_web_service_hides_internal_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingDomainService:
        async def get_report(self, *args):  # type: ignore[no-untyped-def]
            raise RuntimeError("secret database details")

    service = WebAdminServerGameAnalyticsService(
        lambda: FakeSessionContext(FakeSession()),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        repository_factory=lambda session: object(),
        domain_service_factory=lambda *args: FailingDomainService(),
        clock=lambda: AS_OF,
    )

    with caplog.at_level("WARNING"):
        result = await service.load(ServerGameStatisticsPeriod.LAST_30_DAYS)

    assert result is None
    assert "error_type=RuntimeError" in caplog.text
    assert "secret database details" not in caplog.text
