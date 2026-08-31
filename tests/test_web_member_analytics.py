from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from discord_stats_bot.features.game_tracking import (
    GameStatistics,
    GameStatisticsPeriod,
    GameUsageEntry,
    LatestGame,
    LongestGameSession,
)
from discord_stats_bot.features.member_analytics import MemberAnalyticsReport
from discord_stats_bot.features.server_analytics import (
    AnalyticsCoverage,
    AnalyticsDailyPoint,
    AnalyticsVoiceMetric,
    ServerAnalyticsPeriod,
    analytics_metric,
    build_analytics_window,
)
from discord_stats_bot.web import WebSettings, create_app
from discord_stats_bot.web.auth import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from discord_stats_bot.web.authorization import WebAdminRole
from discord_stats_bot.web.game_analytics import (
    GAME_ANALYTICS_TRANSACTION_SETUP_SQL,
    WebAdminGameAnalytics,
    WebAdminGameAnalyticsService,
)
from discord_stats_bot.web.member_analytics import (
    MEMBER_ANALYTICS_TRANSACTION_SETUP_SQL,
    MemberAnalyticsKpiCoverage,
    WebAdminMemberAnalytics,
    WebAdminMemberAnalyticsService,
)
from discord_stats_bot.web.service import (
    AdminMemberDetail,
    AdminMemberDetailResult,
    AdminMemberDetailStatus,
    WebDatabaseHealth,
)

DATABASE_URL = "postgresql+asyncpg://test:test@localhost:5432/test"
AS_OF = datetime(2026, 8, 20, 12, tzinfo=UTC)


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
        REPORT_TIMEZONE="UTC",
    )


class FakeResources:
    session_factory = object()

    async def dispose(self) -> None:
        return None


def member_detail(*, departed: bool = False) -> AdminMemberDetail:
    return AdminMemberDetail(
        guild_id=10,
        user_id=42,
        display_name="<Member & Name>",
        username="unsafe<script>",
        global_name="Global",
        nickname="Member",
        joined_at=datetime(2025, 1, 2, tzinfo=UTC),
        left_at=datetime(2026, 1, 2, tzinfo=UTC) if departed else None,
        voice_seconds=3660,
        message_count=123,
        achievements=(),
        lifecycle_events=(),
        avatar_hash=None,
        guild_avatar_hash=None,
    )


class FakeAdminService:
    def __init__(self, result: AdminMemberDetailResult) -> None:
        self.result = result
        self.detail_calls: list[int] = []

    async def probe_database(self) -> WebDatabaseHealth:
        return WebDatabaseHealth(True)

    async def load_counts(self):  # type: ignore[no-untyped-def]
        return None

    async def load_members(self, **kwargs):  # type: ignore[no-untyped-def]
        return None

    async def load_member_detail(self, user_id: int) -> AdminMemberDetailResult:
        self.detail_calls.append(user_id)
        return self.result


class FakeMemberAnalyticsService:
    def __init__(self, result: WebAdminMemberAnalytics | None = None) -> None:
        self.result = result
        self.calls: list[tuple[int, ServerAnalyticsPeriod]] = []

    async def load(
        self, user_id: int, period: ServerAnalyticsPeriod
    ) -> WebAdminMemberAnalytics | None:
        self.calls.append((user_id, period))
        return self.result


class FakeGameAnalyticsService:
    def __init__(self, result: WebAdminGameAnalytics | None = None) -> None:
        self.result = result
        self.calls: list[tuple[int, GameStatisticsPeriod]] = []

    async def load(
        self, user_id: int, period: GameStatisticsPeriod
    ) -> WebAdminGameAnalytics | None:
        self.calls.append((user_id, period))
        return self.result


def make_report(
    period: ServerAnalyticsPeriod = ServerAnalyticsPeriod.LAST_7_DAYS,
    *,
    voice_exact: tuple[int, int] = (6000, 3000),
    voice_estimated: tuple[int, int] = (1200, 600),
    messages: tuple[int, int] = (10, 20),
    active_days: tuple[int, int] = (3, 3),
    voice_coverage: AnalyticsCoverage = AnalyticsCoverage(
        date(2026, 7, 1), False, False
    ),
    text_coverage: AnalyticsCoverage = AnalyticsCoverage(
        date(2026, 7, 1), False, False
    ),
) -> MemberAnalyticsReport:
    window = build_analytics_window(
        period,
        AS_OF,
        report_timezone=ZoneInfo("UTC"),
    )
    daily = tuple(
        AnalyticsDailyPoint(
            local_date=window.current_started_on + timedelta(days=offset),
            voice_exact_seconds=60 if offset == 1 else 0,
            voice_estimated_seconds=30 if offset == 1 else 0,
            messages=2 if offset == 1 else 0,
        )
        for offset in range(period.days)
    )
    exact = analytics_metric(*voice_exact)
    estimated = analytics_metric(*voice_estimated)
    total = analytics_metric(
        voice_exact[0] + voice_estimated[0],
        voice_exact[1] + voice_estimated[1],
    )
    return MemberAnalyticsReport(
        user_id=42,
        period=period,
        window=window,
        voice_person_time=AnalyticsVoiceMetric(exact, estimated, total),
        messages=analytics_metric(*messages),
        active_days=analytics_metric(*active_days),
        daily=daily,
        voice_coverage=voice_coverage,
        text_coverage=text_coverage,
    )


def page_model(
    report: MemberAnalyticsReport | None = None,
    *,
    voice_coverage: MemberAnalyticsKpiCoverage = MemberAnalyticsKpiCoverage(
        False, False
    ),
    messages_coverage: MemberAnalyticsKpiCoverage = MemberAnalyticsKpiCoverage(
        False, False
    ),
    active_days_coverage: MemberAnalyticsKpiCoverage = MemberAnalyticsKpiCoverage(
        False, False
    ),
) -> WebAdminMemberAnalytics:
    report = report or make_report()
    return WebAdminMemberAnalytics(
        report=report,
        period=report.period,
        report_timezone="UTC",
        voice_coverage=voice_coverage,
        messages_coverage=messages_coverage,
        active_days_coverage=active_days_coverage,
    )


def make_app(
    admin: FakeAdminService,
    analytics: FakeMemberAnalyticsService,
    games: FakeGameAnalyticsService | None = None,
) -> Starlette:
    games = games or FakeGameAnalyticsService()
    return create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: admin,
        member_analytics_service_factory=lambda *args: analytics,  # type: ignore[arg-type]
        game_analytics_service_factory=lambda *args: games,  # type: ignore[arg-type]
    )


@contextmanager
def authenticated_client(app: Starlette) -> Iterator[TestClient]:
    issued = app.state.web_session_store.create(42, role=WebAdminRole.OWNER)
    with TestClient(app) as client:
        client.cookies.set(
            SESSION_COOKIE_NAME,
            issued.session_id,
            path=SESSION_COOKIE_PATH,
        )
        yield client


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", ServerAnalyticsPeriod.LAST_7_DAYS),
        ("?period=7d", ServerAnalyticsPeriod.LAST_7_DAYS),
        ("?period=30d", ServerAnalyticsPeriod.LAST_30_DAYS),
        ("?period=", ServerAnalyticsPeriod.LAST_7_DAYS),
        ("?period=90d", ServerAnalyticsPeriod.LAST_7_DAYS),
        ("?period=30d&period=30d", ServerAnalyticsPeriod.LAST_7_DAYS),
    ],
)
def test_profile_period_parsing_is_allowlisted_and_defaults_safely(
    query: str,
    expected: ServerAnalyticsPeriod,
) -> None:
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )
    analytics = FakeMemberAnalyticsService()

    with authenticated_client(make_app(admin, analytics)) as client:
        response = client.get(f"/admin/members/42{query}")

    assert response.status_code == 200
    assert analytics.calls == [(42, expected)]


def test_successful_activity_is_additive_ordered_and_escaped() -> None:
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )
    analytics = FakeMemberAnalyticsService(page_model())

    with authenticated_client(make_app(admin, analytics)) as client:
        response = client.get("/admin/members/42")

    assert response.status_code == 200
    assert '<h2 id="member-activity-title">Активность</h2>' in response.text
    assert "Lifetime statistics" in response.text
    assert response.text.index("member-activity-title") < response.text.index(
        "lifetime-title"
    )
    assert 'data-member-kpi="voice"' in response.text
    assert "2 ч" in response.text
    assert 'data-member-kpi="messages"' in response.text
    assert ">10<" in response.text
    assert 'data-member-kpi="active-days"' in response.text
    assert "3 из 7" in response.text
    assert "Включает оценённое голосовое время" in response.text
    assert 'class="delta-state positive"' in response.text
    assert 'class="delta-state negative"' in response.text
    assert 'class="delta-state neutral"' in response.text
    assert response.text.count('class="member-activity-day"') == 7
    assert ">0 сек<" in response.text
    assert "&lt;Member &amp; Name&gt;" in response.text
    assert "unsafe&lt;script&gt;" in response.text
    assert "<script" not in response.text.casefold()
    assert "script-src 'none'" in response.headers["content-security-policy"]


def test_no_baseline_and_unchanged_zero_never_render_synthetic_percentages() -> None:
    report = make_report(
        voice_exact=(0, 0),
        voice_estimated=(0, 0),
        messages=(0, 0),
        active_days=(5, 0),
    )
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )

    with authenticated_client(
        make_app(admin, FakeMemberAnalyticsService(page_model(report)))
    ) as client:
        response = client.get("/admin/members/42")

    assert "Нет активности в обоих периодах" in response.text
    assert "Нет базы для сравнения" in response.text
    assert "∞" not in response.text
    assert "+100" not in response.text
    assert '<span class="delta-percent">' not in response.text


def test_coverage_is_source_aware_without_calling_unknown_history_partial() -> None:
    report = make_report(
        voice_coverage=AnalyticsCoverage(date(2026, 8, 15), True, True),
        text_coverage=AnalyticsCoverage(None, None, None),
    )
    model = page_model(
        report,
        voice_coverage=MemberAnalyticsKpiCoverage(True, True),
        messages_coverage=MemberAnalyticsKpiCoverage(None, None),
        active_days_coverage=MemberAnalyticsKpiCoverage(True, True),
    )
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )

    with authenticated_client(
        make_app(admin, FakeMemberAnalyticsService(model))
    ) as client:
        response = client.get("/admin/members/42")

    assert "Данные за начало выбранного периода могут быть неполными" in response.text
    assert "Данные предыдущего периода могут быть неполными" in response.text
    assert "Полнота данных предыдущего периода неизвестна" in response.text
    messages_card = response.text.split('data-member-kpi="messages"', 1)[1].split(
        "</article>", 1
    )[0]
    assert "могут быть неполными" not in messages_card


def test_combined_previous_unknown_does_not_claim_missing_or_partial_history() -> None:
    report = make_report(
        voice_coverage=AnalyticsCoverage(date(2026, 7, 1), False, False),
        text_coverage=AnalyticsCoverage(None, None, None),
    )
    model = page_model(
        report,
        voice_coverage=MemberAnalyticsKpiCoverage(False, False),
        messages_coverage=MemberAnalyticsKpiCoverage(None, None),
        active_days_coverage=MemberAnalyticsKpiCoverage(None, None),
    )
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )

    with authenticated_client(
        make_app(admin, FakeMemberAnalyticsService(model))
    ) as client:
        response = client.get("/admin/members/42")

    active_days_card = response.text.split('data-member-kpi="active-days"', 1)[1].split(
        "</article>", 1
    )[0]
    assert "Полнота данных предыдущего периода неизвестна" in active_days_card
    assert "Нет записанной базы сравнения" not in active_days_card
    assert "могут быть неполными" not in active_days_card


def test_30_day_history_and_selected_member_links_are_preserved() -> None:
    report = make_report(ServerAnalyticsPeriod.LAST_30_DAYS, active_days=(3, 2))
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )

    with authenticated_client(
        make_app(admin, FakeMemberAnalyticsService(page_model(report)))
    ) as client:
        response = client.get("/admin/members/42?period=30d")

    assert response.status_code == 200
    assert response.text.count('class="member-activity-day"') == 30
    assert "3 из 30" in response.text
    assert 'href="/admin/members/42?period=7d&amp;game_period=30d"' in response.text
    assert (
        'href="/admin/members/42?period=30d&amp;game_period=30d" '
        'aria-current="page"' in response.text
    )


def test_analytics_failure_is_section_level_and_keeps_profile_http_200() -> None:
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )

    with authenticated_client(
        make_app(admin, FakeMemberAnalyticsService(None))
    ) as client:
        response = client.get("/admin/members/42?period=30d")

    assert response.status_code == 200
    assert "Аналитика активности временно недоступна" in response.text
    assert "Lifetime statistics" in response.text
    assert "1 ч 01 мин" in response.text
    assert (
        'href="/admin/members/42?period=30d&amp;game_period=30d" '
        'aria-current="page"' in response.text
    )
    assert "Traceback" not in response.text
    assert "SELECT" not in response.text


@pytest.mark.parametrize(
    ("status", "expected_status"),
    [
        (AdminMemberDetailStatus.NOT_FOUND, 404),
        (AdminMemberDetailStatus.UNAVAILABLE, 503),
    ],
)
def test_missing_or_unavailable_detail_preserves_status_and_skips_analytics(
    status: AdminMemberDetailStatus,
    expected_status: int,
) -> None:
    admin = FakeAdminService(AdminMemberDetailResult(status))
    analytics = FakeMemberAnalyticsService(page_model())
    games = FakeGameAnalyticsService(game_page_model())

    with authenticated_client(make_app(admin, analytics, games)) as client:
        response = client.get("/admin/members/42")

    assert response.status_code == expected_status
    assert analytics.calls == []
    assert games.calls == []


def test_departed_member_can_render_historical_activity() -> None:
    admin = FakeAdminService(
        AdminMemberDetailResult(
            AdminMemberDetailStatus.FOUND,
            member_detail(departed=True),
        )
    )

    with authenticated_client(
        make_app(admin, FakeMemberAnalyticsService(page_model()))
    ) as client:
        response = client.get("/admin/members/42")

    assert response.status_code == 200
    assert "Покинул сервер" in response.text
    assert 'data-member-kpi="voice"' in response.text


def test_authentication_precedes_member_and_analytics_reads() -> None:
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )
    analytics = FakeMemberAnalyticsService(page_model())
    games = FakeGameAnalyticsService(game_page_model())
    app = make_app(admin, analytics, games)

    with TestClient(app) as client:
        response = client.get("/admin/members/42", follow_redirects=False)

    assert response.status_code == 303
    assert admin.detail_calls == []
    assert analytics.calls == []
    assert games.calls == []


def test_member_activity_css_is_scoped_responsive_and_script_free() -> None:
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )

    with authenticated_client(
        make_app(admin, FakeMemberAnalyticsService(page_model()))
    ) as client:
        response = client.get("/admin/members/42")

    assert ".member-activity-kpis {" in response.text
    assert ".member-activity-history {" in response.text
    assert ".member-activity-table thead { display: none; }" in response.text
    assert (
        ".member-activity-table tr { display: grid; grid-template-columns: "
        "repeat(2, minmax(0, 1fr));" in response.text
    )
    assert "overflow: hidden" in response.text
    assert "@media (max-width: 640px)" in response.text
    assert "@media (max-width: 430px)" in response.text
    assert "<script" not in response.text.casefold()
    assert "javascript:" not in response.text.casefold()


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
async def test_web_service_uses_one_read_only_snapshot_and_configured_scope() -> None:
    session = FakeSession()
    report = make_report()
    calls: list[tuple[int, int, ServerAnalyticsPeriod, datetime]] = []
    repository_sessions: list[FakeSession] = []
    factory_arguments: list[tuple[object, ZoneInfo, int]] = []

    class DomainService:
        async def get_report(  # type: ignore[no-untyped-def]
            self, guild_id, user_id, period, as_of
        ):
            calls.append((guild_id, user_id, period, as_of))
            return report

    def domain_factory(repository, timezone, threshold):  # type: ignore[no-untyped-def]
        factory_arguments.append((repository, timezone, threshold))
        return DomainService()

    result = await WebAdminMemberAnalyticsService(
        lambda: FakeSessionContext(session),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        min_session_seconds=10,
        analytics_repository_factory=lambda value: (
            repository_sessions.append(value) or object()
        ),
        domain_service_factory=domain_factory,
        clock=lambda: AS_OF,
    ).load(42, ServerAnalyticsPeriod.LAST_7_DAYS)

    assert result is not None
    assert session.statements == [MEMBER_ANALYTICS_TRANSACTION_SETUP_SQL]
    assert repository_sessions == [session]
    assert len(factory_arguments) == 1
    assert factory_arguments[0][1:] == (ZoneInfo("UTC"), 10)
    assert calls == [(10, 42, ServerAnalyticsPeriod.LAST_7_DAYS, AS_OF)]


@pytest.mark.asyncio
async def test_web_service_maps_combined_coverage_and_hides_database_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = FakeSession()
    report = make_report(
        voice_coverage=AnalyticsCoverage(None, None, None),
        text_coverage=AnalyticsCoverage(date(2026, 8, 15), True, False),
    )

    class DomainService:
        async def get_report(self, *args):  # type: ignore[no-untyped-def]
            return report

    service = WebAdminMemberAnalyticsService(
        lambda: FakeSessionContext(session),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        min_session_seconds=10,
        analytics_repository_factory=lambda value: object(),
        domain_service_factory=lambda *args: DomainService(),
        clock=lambda: AS_OF,
    )
    result = await service.load(42, ServerAnalyticsPeriod.LAST_7_DAYS)

    assert result is not None
    assert result.voice_coverage.current_partial is None
    assert result.messages_coverage.current_partial is True
    assert result.active_days_coverage.current_partial is True
    assert result.active_days_coverage.previous_partial is None

    class FailingDomainService:
        async def get_report(self, *args):  # type: ignore[no-untyped-def]
            raise RuntimeError("secret database details")

    failing = WebAdminMemberAnalyticsService(
        lambda: FakeSessionContext(FakeSession()),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        min_session_seconds=10,
        analytics_repository_factory=lambda value: object(),
        domain_service_factory=lambda *args: FailingDomainService(),
        clock=lambda: AS_OF,
    )

    with caplog.at_level("WARNING"):
        unavailable = await failing.load(42, ServerAnalyticsPeriod.LAST_7_DAYS)

    assert unavailable is None
    assert "error_type=RuntimeError" in caplog.text
    assert "secret database details" not in caplog.text


def game_page_model(*, has_data: bool = True) -> WebAdminGameAnalytics:
    statistics = GameStatistics(
        as_of=AS_OF,
        period=GameStatisticsPeriod.LAST_30_DAYS,
        total_seconds=18_300 if has_data else 0,
        unique_games=6 if has_data else 0,
        gaming_days=4 if has_data else 0,
        top_games=(
            tuple(
                GameUsageEntry(
                    f"game-{index}",
                    "Game <script> &" if index == 1 else f"Game {index}",
                    (7 - index) * 3600,
                )
                for index in range(1, 6)
            )
            if has_data
            else ()
        ),
        latest_game=(
            LatestGame("Latest <unsafe>", datetime(2026, 8, 20, 10, tzinfo=UTC))
            if has_data
            else None
        ),
        longest_session=(
            LongestGameSession("Longest & Game", 7200) if has_data else None
        ),
    )
    return WebAdminGameAnalytics(statistics, ZoneInfo("Asia/Yekaterinburg"))


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", GameStatisticsPeriod.LAST_30_DAYS),
        ("?game_period=7d", GameStatisticsPeriod.LAST_7_DAYS),
        ("?game_period=30d", GameStatisticsPeriod.LAST_30_DAYS),
        ("?game_period=90d", GameStatisticsPeriod.LAST_90_DAYS),
        ("?game_period=", GameStatisticsPeriod.LAST_30_DAYS),
        ("?game_period=all", GameStatisticsPeriod.LAST_30_DAYS),
        ("?game_period=invalid", GameStatisticsPeriod.LAST_30_DAYS),
        (
            "?game_period=7d&game_period=90d",
            GameStatisticsPeriod.LAST_30_DAYS,
        ),
    ],
)
def test_game_period_parsing_is_allowlisted_and_defaults_safely(
    query: str,
    expected: GameStatisticsPeriod,
) -> None:
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )
    games = FakeGameAnalyticsService()

    with authenticated_client(
        make_app(admin, FakeMemberAnalyticsService(), games)
    ) as client:
        response = client.get(f"/admin/members/42{query}")

    assert response.status_code == 200
    assert games.calls == [(42, expected)]


def test_game_analytics_is_populated_ordered_escaped_and_preserves_periods() -> None:
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )
    activity = FakeMemberAnalyticsService(page_model())
    games = FakeGameAnalyticsService(game_page_model())

    with authenticated_client(make_app(admin, activity, games)) as client:
        response = client.get("/admin/members/42?period=30d&game_period=90d")

    assert response.status_code == 200
    assert activity.calls == [(42, ServerAnalyticsPeriod.LAST_30_DAYS)]
    assert games.calls == [(42, GameStatisticsPeriod.LAST_90_DAYS)]
    assert (
        response.text.index("member-activity-title")
        < response.text.index("member-games-title")
        < response.text.index("lifetime-title")
    )
    assert 'data-game-kpi="time"' in response.text
    assert "5 ч 05 мин" in response.text
    assert 'data-game-kpi="games"' in response.text
    assert ">6<" in response.text
    assert 'data-game-kpi="days"' in response.text
    assert ">4<" in response.text
    assert response.text.count("<li><span>Game") == 5
    assert "Game &lt;script&gt; &amp;" in response.text
    assert "Latest &lt;unsafe&gt;" in response.text
    assert "20.08.2026 15:00" in response.text
    assert "Longest &amp; Game" in response.text
    assert "<script> &" not in response.text
    assert 'href="/admin/members/42?period=30d&amp;game_period=7d"' in response.text
    assert 'href="/admin/members/42?period=7d&amp;game_period=90d"' in response.text
    assert (
        'href="/admin/members/42?period=30d&amp;game_period=90d" '
        'aria-current="page"' in response.text
    )


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (
            game_page_model(has_data=False),
            "Игровых данных за выбранный период пока нет.",
        ),
        (None, "Игровая аналитика временно недоступна."),
    ],
)
def test_game_analytics_empty_and_failure_are_section_level(
    result: WebAdminGameAnalytics | None,
    message: str,
) -> None:
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )

    with authenticated_client(
        make_app(
            admin,
            FakeMemberAnalyticsService(page_model()),
            FakeGameAnalyticsService(result),
        )
    ) as client:
        response = client.get("/admin/members/42")

    assert response.status_code == 200
    assert message in response.text
    assert "Lifetime statistics" in response.text
    assert 'data-member-kpi="voice"' in response.text


def test_game_analytics_css_is_responsive_and_server_rendered() -> None:
    admin = FakeAdminService(
        AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, member_detail())
    )
    with authenticated_client(
        make_app(
            admin,
            FakeMemberAnalyticsService(),
            FakeGameAnalyticsService(game_page_model()),
        )
    ) as client:
        response = client.get("/admin/members/42")

    assert (
        ".member-games-kpis, .member-games-details { grid-template-columns: 1fr; }"
        in response.text
    )
    assert (
        ".member-games-header { align-items: stretch; flex-direction: column; }"
        in response.text
    )
    assert "@media (max-width: 640px)" in response.text
    assert "<script" not in response.text.casefold()
    assert "javascript:" not in response.text.casefold()


@pytest.mark.asyncio
async def test_game_web_service_uses_read_only_configured_guild_snapshot() -> None:
    session = FakeSession()
    statistics = game_page_model().statistics
    calls: list[tuple[int, int, GameStatisticsPeriod, datetime]] = []
    repository_sessions: list[FakeSession] = []

    class DomainService:
        async def get_user_statistics(  # type: ignore[no-untyped-def]
            self, guild_id, user_id, period, as_of
        ):
            calls.append((guild_id, user_id, period, as_of))
            return statistics

    result = await WebAdminGameAnalyticsService(
        lambda: FakeSessionContext(session),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
        repository_factory=lambda value: repository_sessions.append(value) or object(),
        domain_service_factory=lambda *args: DomainService(),
        clock=lambda: AS_OF,
    ).load(42, GameStatisticsPeriod.LAST_90_DAYS)

    assert result is not None
    assert session.statements == [GAME_ANALYTICS_TRANSACTION_SETUP_SQL]
    assert repository_sessions == [session]
    assert calls == [(10, 42, GameStatisticsPeriod.LAST_90_DAYS, AS_OF)]


@pytest.mark.asyncio
async def test_game_web_service_hides_database_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingDomainService:
        async def get_user_statistics(self, *args):  # type: ignore[no-untyped-def]
            raise RuntimeError("secret database details")

    service = WebAdminGameAnalyticsService(
        lambda: FakeSessionContext(FakeSession()),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        repository_factory=lambda value: object(),
        domain_service_factory=lambda *args: FailingDomainService(),
        clock=lambda: AS_OF,
    )

    with caplog.at_level("WARNING"):
        result = await service.load(42, GameStatisticsPeriod.LAST_30_DAYS)

    assert result is None
    assert "error_type=RuntimeError" in caplog.text
    assert "secret database details" not in caplog.text
