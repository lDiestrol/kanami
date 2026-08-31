from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from discord_stats_bot.features.server_analytics import (
    AnalyticsCoverage,
    AnalyticsDailyPoint,
    AnalyticsMessageTopMember,
    AnalyticsPercentState,
    AnalyticsVoiceMetric,
    AnalyticsVoiceTopMember,
    ServerAnalyticsPeriod,
    ServerAnalyticsReport,
    analytics_metric,
    build_analytics_window,
)
from discord_stats_bot.features.voice_statistics import (
    VoiceActivityInterval,
    VoiceActivityPeriod,
    aggregate_voice_activity_window,
)
from discord_stats_bot.web import WebSettings, create_app
from discord_stats_bot.web.analytics import (
    ANALYTICS_TRANSACTION_SETUP_SQL,
    AnalyticsKpiCoverage,
    SqlAlchemyAnalyticsDisplayNameRepository,
    WebAdminAnalytics,
    WebAdminAnalyticsService,
    WebAnalyticsTopMessageAuthor,
    WebAnalyticsTopVoiceMember,
)
from discord_stats_bot.web.auth import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from discord_stats_bot.web.authorization import WebAdminRole
from discord_stats_bot.web.presentation import ADMIN_STYLES
from discord_stats_bot.web.service import (
    AdminMemberDetailResult,
    AdminMemberDetailStatus,
    AdminMembersPage,
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
        REPORT_TIMEZONE="Asia/Yekaterinburg",
    )


def make_report(
    period: ServerAnalyticsPeriod = ServerAnalyticsPeriod.LAST_7_DAYS,
    *,
    voice_coverage: AnalyticsCoverage = AnalyticsCoverage(
        date(2026, 7, 1), False, False
    ),
    text_coverage: AnalyticsCoverage = AnalyticsCoverage(
        date(2026, 8, 10), False, True
    ),
) -> ServerAnalyticsReport:
    timezone = ZoneInfo("Asia/Yekaterinburg")
    window = build_analytics_window(period, AS_OF, report_timezone=timezone)
    voice_total = analytics_metric(7200, 3600)
    activity_period = (
        VoiceActivityPeriod.LAST_7_DAYS
        if period is ServerAnalyticsPeriod.LAST_7_DAYS
        else VoiceActivityPeriod.LAST_30_DAYS
    )
    activity = replace(
        aggregate_voice_activity_window(
            (),
            period=activity_period,
            started_at=window.current_started_at,
            as_of=window.current_ended_at,
            report_timezone=timezone,
        ),
        total_user_seconds=7200.0,
        hourly_activity=tuple(float(hour + 1) for hour in range(24)),
        weekday_activity=tuple(float(day + 1) for day in range(7)),
        heatmap_activity=tuple(
            tuple(float((row + weekday) % 5) for weekday in range(7))
            for row in range(8)
        ),
        top_hours=(23, 22, 21),
        active_weekday=6,
        quietest_period=(0, 0),
        has_estimated_time=True,
    )
    return ServerAnalyticsReport(
        window=window,
        active_members=analytics_metric(5, 0),
        voice_person_time=AnalyticsVoiceMetric(
            analytics_metric(6000, 3000),
            analytics_metric(1200, 600),
            voice_total,
        ),
        messages=analytics_metric(42, 21),
        unique_voice_users=analytics_metric(3, 2),
        unique_message_authors=analytics_metric(4, 2),
        daily=tuple(
            AnalyticsDailyPoint(
                window.current_started_on + timedelta(days=offset),
                voice_exact_seconds=(offset + 1) * 600,
                voice_estimated_seconds=(300 if offset == 0 else 0),
                messages=(offset + 1) * 10,
            )
            for offset in range(period.days)
        ),
        top_voice_members=(
            AnalyticsVoiceTopMember(101, 4000, 1000),
            AnalyticsVoiceTopMember(202, 3000, 0),
        ),
        top_message_authors=(
            AnalyticsMessageTopMember(101, 30),
            AnalyticsMessageTopMember(303, 12),
        ),
        voice_activity=activity,
        voice_coverage=voice_coverage,
        text_coverage=text_coverage,
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


class StaticAnalyticsService:
    def __init__(self, analytics: WebAdminAnalytics | None) -> None:
        self.analytics = analytics
        self.periods: list[ServerAnalyticsPeriod] = []

    async def load(self, period: ServerAnalyticsPeriod) -> WebAdminAnalytics | None:
        self.periods.append(period)
        return self.analytics


def page_model(
    period: ServerAnalyticsPeriod = ServerAnalyticsPeriod.LAST_7_DAYS,
) -> WebAdminAnalytics:
    report = make_report(period)
    return WebAdminAnalytics(
        report=report,
        period=period,
        report_timezone="Asia/Yekaterinburg",
        top_voice_members=(),
        top_message_authors=(),
        voice_hours_coverage=AnalyticsKpiCoverage(False, False),
        unique_voice_users_coverage=AnalyticsKpiCoverage(False, False),
        messages_coverage=AnalyticsKpiCoverage(False, True),
        unique_message_authors_coverage=AnalyticsKpiCoverage(False, True),
        active_members_coverage=AnalyticsKpiCoverage(False, True),
    )


def make_app(service: StaticAnalyticsService) -> Starlette:
    return create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: FakeAdminService(),
        analytics_service_factory=lambda *args: service,  # type: ignore[arg-type]
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


def test_analytics_requires_authentication_without_loading_report() -> None:
    service = StaticAnalyticsService(page_model())
    app = make_app(service)

    with TestClient(app) as client:
        response = client.get("/admin/analytics", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert service.periods == []


@pytest.mark.parametrize("role", [WebAdminRole.OWNER, WebAdminRole.ADMIN])
def test_owner_and_admin_can_render_final_analytics_page(role: WebAdminRole) -> None:
    analytics = page_model()
    analytics = replace(
        analytics,
        top_voice_members=(
            WebAnalyticsTopVoiceMember(
                user_id=101,
                display_name='<Voice & "name">',
                exact_seconds=4000,
                estimated_seconds=1000,
            ),
        ),
        top_message_authors=(
            WebAnalyticsTopMessageAuthor(
                user_id=303,
                display_name="<Text author>",
                message_count=12,
            ),
        ),
    )
    service = StaticAnalyticsService(analytics)
    app = make_app(service)

    with authenticated_client(app, role) as client:
        response = client.get("/admin/analytics")

    assert response.status_code == 200
    assert service.periods == [ServerAnalyticsPeriod.LAST_7_DAYS]
    assert "Server Analytics" in response.text
    assert "2026-08-13" in response.text
    assert "Asia/Yekaterinburg" in response.text
    assert "завершённые локальные календарные" in response.text
    for key in (
        "active-members",
        "voice",
        "messages",
        "voice-users",
        "message-authors",
    ):
        assert f'data-kpi="{key}"' in response.text
    assert "2 ч 00 мин" in response.text
    assert "Оценочно: 20 мин" in response.text
    assert "Нет базы для сравнения" in response.text
    assert "+100%" not in response.text
    assert "infinity" not in response.text.casefold()
    assert "&lt;Voice &amp; &quot;name&quot;&gt;" in response.text
    assert "<Voice &" not in response.text
    assert "&lt;Text author&gt;" in response.text
    assert "База сравнения может быть неполной" in response.text
    assert 'href="/admin/members/101"' in response.text
    assert 'href="/admin/members/303"' in response.text
    assert "Voice activity" in response.text
    assert "Паттерн Voice активности" in response.text
    assert "Как считается активность" in response.text
    assert "Игры в этот показатель не входят" in response.text
    assert response.text.count('aria-current="page" href="/admin/analytics"') == 2
    assert response.text.count('aria-current="page"') >= 3
    assert "<script" not in response.text.casefold()
    assert "script-src 'none'" in response.headers["content-security-policy"]


def test_kpi_delta_states_and_duration_delta_are_presented_without_synthetic_values() -> (
    None
):
    analytics = page_model()
    report = replace(
        analytics.report,
        messages=analytics_metric(0, 0),
        active_members=analytics_metric(5, 0),
    )
    service = StaticAnalyticsService(replace(analytics, report=report))
    app = make_app(service)

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/analytics")

    assert response.status_code == 200
    assert "+1 ч 00 мин" in response.text
    assert "+100.0%" in response.text  # A1 AVAILABLE percentage for Voice.
    assert "Без изменений" in response.text
    assert "Нет базы для сравнения" in response.text
    assert "infinity" not in response.text.casefold()


def test_available_zero_delta_uses_neutral_visual_tone() -> None:
    analytics = page_model()
    messages = analytics_metric(42, 42)
    assert messages.percent_state is AnalyticsPercentState.AVAILABLE
    report = replace(analytics.report, messages=messages)
    app = make_app(StaticAnalyticsService(replace(analytics, report=report)))

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/analytics")

    messages_card = response.text.split('data-kpi="messages"', 1)[1].split(
        "</article>", 1
    )[0]
    assert 'class="delta-state neutral"' in messages_card
    assert '<span class="delta-absolute">0</span>' in messages_card
    assert '<span class="delta-percent">+0.0%</span>' in messages_card


def test_coverage_warnings_are_source_aware_and_earliest_is_not_monitoring_start() -> (
    None
):
    analytics = page_model()
    report = replace(
        analytics.report,
        voice_coverage=AnalyticsCoverage(date(2026, 8, 14), True, True),
        text_coverage=AnalyticsCoverage(date(2026, 8, 10), True, True),
    )
    analytics = replace(
        analytics,
        report=report,
        voice_hours_coverage=AnalyticsKpiCoverage(True, True),
        unique_voice_users_coverage=AnalyticsKpiCoverage(True, True),
        messages_coverage=AnalyticsKpiCoverage(True, True),
        unique_message_authors_coverage=AnalyticsKpiCoverage(True, True),
        active_members_coverage=AnalyticsKpiCoverage(True, True),
    )
    app = make_app(StaticAnalyticsService(analytics))

    with authenticated_client(app, WebAdminRole.OWNER) as client:
        response = client.get("/admin/analytics")

    assert "14.08.2026" in response.text
    assert "10.08.2026" in response.text
    assert response.text.count("История периода может быть неполной") == 5
    assert response.text.count("База сравнения может быть неполной") == 5
    assert "не дата запуска сбора" in response.text
    assert "не гарантия полноты истории" in response.text
    assert "мониторинг начался" not in response.text.casefold()
    assert "полное покрытие" not in response.text.casefold()


@pytest.mark.parametrize(
    ("period", "expected_points"),
    [
        (ServerAnalyticsPeriod.LAST_7_DAYS, 7),
        (ServerAnalyticsPeriod.LAST_30_DAYS, 30),
    ],
)
def test_daily_voice_and_message_charts_render_accessible_points(
    period: ServerAnalyticsPeriod,
    expected_points: int,
) -> None:
    analytics = page_model(period)
    app = make_app(StaticAnalyticsService(analytics))

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get(f"/admin/analytics?period={period.value}")

    assert (
        response.text.count('class="daily-point daily-point-voice"') == expected_points
    )
    assert (
        response.text.count('class="daily-point daily-point-messages"')
        == expected_points
    )
    assert f"daily-chart-{expected_points}" in response.text
    first_date = analytics.report.window.current_started_on.strftime("%d.%m.%Y")
    assert f'aria-label="{first_date}: Voice 15 мин, оценочно: 5 мин"' in response.text
    assert "Оценочно: 5 мин" in response.text
    assert "сообщений" in response.text
    assert "--bar-height:" in response.text


def test_daily_charts_are_zero_safe_and_keep_all_values_accessible() -> None:
    analytics = page_model()
    report = replace(
        analytics.report,
        daily=tuple(
            AnalyticsDailyPoint(point.local_date) for point in analytics.report.daily
        ),
    )
    app = make_app(StaticAnalyticsService(replace(analytics, report=report)))

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/analytics")

    assert response.text.count("За выбранный период активности нет.") == 2
    assert response.text.count('class="daily-point daily-point-voice"') == 7
    assert response.text.count('class="daily-point daily-point-messages"') == 7
    assert "--bar-height:0.00%" in response.text
    assert "nan" not in response.text.casefold()
    assert "infinity" not in response.text.casefold()


def test_heatmap_uses_report_activity_and_has_semantic_labels_and_summary() -> None:
    app = make_app(StaticAnalyticsService(page_model()))

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/analytics")

    assert response.text.count('class="heatmap-cell level-') == 56
    for weekday in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"):
        assert f'<th scope="col">{weekday}</th>' in response.text
    for slot in (
        "00–03",
        "03–06",
        "06–09",
        "09–12",
        "12–15",
        "15–18",
        "18–21",
        "21–24",
    ):
        assert f'<th scope="row">{slot}</th>' in response.text
    assert "23:00–00:00" in response.text
    assert "воскресенье" in response.text
    assert "понедельник, 00:00–03:00" in response.text
    assert "Паттерн включает восстановленные оценочные участки Voice" in response.text
    assert "Нормализованная Voice activity" in response.text


def test_quietest_period_renderer_uses_domain_weekday_and_slot_start_contract() -> None:
    activity = aggregate_voice_activity_window(
        (
            VoiceActivityInterval(
                datetime(2026, 1, 10, 21, tzinfo=UTC),
                datetime(2026, 1, 10, 22, tzinfo=UTC),
            ),
            VoiceActivityInterval(
                datetime(2026, 1, 9, 22, tzinfo=UTC),
                datetime(2026, 1, 9, 22, 45, tzinfo=UTC),
            ),
            VoiceActivityInterval(
                datetime(2026, 1, 8, 20, tzinfo=UTC),
                datetime(2026, 1, 8, 20, 30, tzinfo=UTC),
            ),
        ),
        period=VoiceActivityPeriod.LAST_7_DAYS,
        started_at=datetime(2026, 1, 5, tzinfo=UTC),
        as_of=datetime(2026, 1, 12, tzinfo=UTC),
        report_timezone=ZoneInfo("UTC"),
    )
    assert activity.quietest_period == (0, 0)
    analytics = page_model()
    report = replace(analytics.report, voice_activity=activity)
    app = make_app(StaticAnalyticsService(replace(analytics, report=report)))

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/analytics")

    assert "понедельник, 00:00–03:00" in response.text


def test_empty_sources_rankings_and_activity_have_explanatory_states() -> None:
    analytics = page_model()
    empty_activity = replace(
        analytics.report.voice_activity,
        total_user_seconds=0.0,
        top_hours=(),
        active_weekday=None,
        quietest_period=None,
        has_estimated_time=False,
    )
    report = replace(
        analytics.report,
        voice_activity=empty_activity,
        voice_coverage=AnalyticsCoverage(None, None, None),
        text_coverage=AnalyticsCoverage(None, None, None),
    )
    analytics = replace(
        analytics,
        report=report,
        top_voice_members=(),
        top_message_authors=(),
        voice_hours_coverage=AnalyticsKpiCoverage(None, None),
        unique_voice_users_coverage=AnalyticsKpiCoverage(None, None),
        messages_coverage=AnalyticsKpiCoverage(None, None),
        unique_message_authors_coverage=AnalyticsKpiCoverage(None, None),
        active_members_coverage=AnalyticsKpiCoverage(None, None),
    )
    app = make_app(StaticAnalyticsService(analytics))

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/analytics")

    assert response.text.count("Записанной активности пока нет") == 2
    assert "Недостаточно Voice activity для выводов о паттерне" in response.text
    assert "За период нет Voice activity для рейтинга" in response.text
    assert "За период нет сообщений для рейтинга" in response.text
    assert "Источник за период пока пуст" in response.text


def test_analytics_css_has_local_overflow_and_mobile_layout_without_javascript() -> (
    None
):
    app = make_app(StaticAnalyticsService(page_model()))

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/analytics")

    assert ".analytics-chart-scroll" in response.text
    assert ".heatmap-scroll" in response.text
    assert "overflow-x: auto" in response.text
    assert ".daily-chart-30" in response.text
    assert "@media (max-width: 640px)" in response.text
    assert "@media (max-width: 430px)" in response.text
    assert ".analytics-kpi-grid { grid-template-columns: 1fr; }" in response.text
    assert (
        ".analytics-period-selector .button { flex: 1 1 0; min-height: 44px; }"
        in response.text
    )
    assert "<script" not in response.text.casefold()
    assert "javascript:" not in response.text.casefold()


def test_analytics_kpi_breakpoint_contract_excludes_wide_desktop_override() -> None:
    default_css, after_900 = ADMIN_STYLES.split("@media (max-width: 900px) {", 1)
    max_900_css, after_1200 = after_900.split("@media (min-width: 1200px) {", 1)
    min_1200_css, after_700 = after_1200.split("@media (max-width: 700px) {", 1)
    max_700_css, after_640 = after_700.split("@media (max-width: 640px) {", 1)
    _, after_430 = after_640.split("@media (max-width: 430px) {", 1)
    max_430_css, _ = after_430.split("@media (prefers-reduced-motion: reduce)", 1)

    assert (
        ".analytics-kpi-grid { display: grid; grid-template-columns: "
        "repeat(5, minmax(0, 1fr));" in default_css
    )
    assert (
        ".analytics-kpi-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }"
        in max_900_css
    )
    assert ".analytics-kpi-grid" not in min_1200_css
    assert (
        ".analytics-kpi-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }"
        in max_700_css
    )
    assert ".analytics-kpi-grid { grid-template-columns: 1fr; }" in max_430_css


@pytest.mark.parametrize(
    ("query", "expected", "status"),
    [
        ("", ServerAnalyticsPeriod.LAST_7_DAYS, 200),
        ("?period=7d", ServerAnalyticsPeriod.LAST_7_DAYS, 200),
        ("?period=30d", ServerAnalyticsPeriod.LAST_30_DAYS, 200),
    ],
)
def test_period_handling(
    query: str,
    expected: ServerAnalyticsPeriod,
    status: int,
) -> None:
    service = StaticAnalyticsService(page_model(expected))
    app = make_app(service)

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get(f"/admin/analytics{query}")

    assert response.status_code == status
    assert service.periods == [expected]


def test_invalid_period_is_controlled_and_does_not_load_report() -> None:
    service = StaticAnalyticsService(page_model())
    app = make_app(service)

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/analytics?period=90d")

    assert response.status_code == 400
    assert "Некорректный период" in response.text
    assert service.periods == []


def test_unavailable_report_is_controlled_without_exception_details() -> None:
    service = StaticAnalyticsService(None)
    app = make_app(service)

    with authenticated_client(app, WebAdminRole.OWNER) as client:
        response = client.get("/admin/analytics")

    assert response.status_code == 503
    assert "Analytics временно недоступна" in response.text
    assert "SELECT" not in response.text
    assert "Traceback" not in response.text


class FakeSession:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.statements: list[str] = []

    async def execute(self, statement: object) -> object:
        self.events.append("transaction_setup")
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
async def test_orchestration_uses_one_as_of_report_call_snapshot_and_name_batch() -> (
    None
):
    events: list[str] = []
    session = FakeSession(events)
    report = make_report()
    clock_calls = 0
    report_calls: list[tuple[int, ServerAnalyticsPeriod, datetime]] = []
    name_calls: list[tuple[int, frozenset[int]]] = []
    repository_sessions = []
    name_repository_sessions = []

    def clock() -> datetime:
        nonlocal clock_calls
        clock_calls += 1
        return AS_OF

    class DomainService:
        async def get_report(self, guild_id, period, as_of):  # type: ignore[no-untyped-def]
            events.append("report")
            report_calls.append((guild_id, period, as_of))
            return report

    class NameRepository:
        async def resolve(self, guild_id, user_ids):  # type: ignore[no-untyped-def]
            events.append("names")
            name_calls.append((guild_id, user_ids))
            return {101: "Persisted nickname", 303: "Historical user"}

    service = WebAdminAnalyticsService(
        lambda: FakeSessionContext(session),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
        min_session_seconds=10,
        analytics_repository_factory=lambda value: (
            repository_sessions.append(value) or object()
        ),
        display_name_repository_factory=lambda value: (
            name_repository_sessions.append(value) or NameRepository()
        ),
        domain_service_factory=lambda repository, timezone, threshold: DomainService(),
        clock=clock,
    )

    result = await service.load(ServerAnalyticsPeriod.LAST_7_DAYS)

    assert result is not None
    assert clock_calls == 1
    assert report_calls == [(10, ServerAnalyticsPeriod.LAST_7_DAYS, AS_OF)]
    assert name_calls == [(10, frozenset({101, 202, 303}))]
    assert repository_sessions == [session]
    assert name_repository_sessions == [session]
    assert events == ["transaction_setup", "report", "names"]
    assert session.statements == [ANALYTICS_TRANSACTION_SETUP_SQL]
    assert [item.display_name for item in result.top_voice_members] == [
        "Persisted nickname",
        "202",
    ]
    assert result.top_message_authors[0].display_name == "Persisted nickname"
    assert result.top_message_authors[1].display_name == "Historical user"


@pytest.mark.asyncio
async def test_source_coverage_maps_current_and_previous_independently() -> None:
    session = FakeSession()
    report = make_report(
        voice_coverage=AnalyticsCoverage(date(2026, 7, 1), False, False),
        text_coverage=AnalyticsCoverage(date(2026, 8, 10), False, True),
    )

    class DomainService:
        async def get_report(self, *args):  # type: ignore[no-untyped-def]
            return report

    class NameRepository:
        async def resolve(self, guild_id, user_ids):  # type: ignore[no-untyped-def]
            return {}

    service = WebAdminAnalyticsService(
        lambda: FakeSessionContext(session),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
        min_session_seconds=10,
        analytics_repository_factory=lambda value: object(),
        display_name_repository_factory=lambda value: NameRepository(),
        domain_service_factory=lambda *args: DomainService(),
        clock=lambda: AS_OF,
    )

    result = await service.load(ServerAnalyticsPeriod.LAST_7_DAYS)

    assert result is not None
    assert result.voice_hours_coverage.current_partial is False
    assert result.voice_hours_coverage.previous_partial is False
    assert result.messages_coverage.current_partial is False
    assert result.messages_coverage.previous_partial is True
    assert result.unique_message_authors_coverage.previous_partial is True
    assert result.active_members_coverage.current_partial is False
    assert result.active_members_coverage.previous_partial is True


@pytest.mark.asyncio
async def test_empty_source_none_flags_are_preserved_in_kpi_mapping() -> None:
    session = FakeSession()
    report = make_report(
        voice_coverage=AnalyticsCoverage(None, None, None),
        text_coverage=AnalyticsCoverage(date(2026, 7, 1), False, False),
    )

    class DomainService:
        async def get_report(self, *args):  # type: ignore[no-untyped-def]
            return report

    class NameRepository:
        async def resolve(self, guild_id, user_ids):  # type: ignore[no-untyped-def]
            return {}

    result = await WebAdminAnalyticsService(
        lambda: FakeSessionContext(session),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
        min_session_seconds=10,
        analytics_repository_factory=lambda value: object(),
        display_name_repository_factory=lambda value: NameRepository(),
        domain_service_factory=lambda *args: DomainService(),
        clock=lambda: AS_OF,
    ).load(ServerAnalyticsPeriod.LAST_7_DAYS)

    assert result is not None
    assert result.voice_hours_coverage.current_partial is None
    assert result.voice_hours_coverage.previous_partial is None
    assert result.active_members_coverage.current_partial is None
    assert result.active_members_coverage.previous_partial is None


@pytest.mark.asyncio
async def test_display_name_repository_uses_one_query_and_persisted_precedence() -> (
    None
):
    class Result:
        def all(self):
            return [SimpleNamespace(id=101, display_name="Historical nickname")]

    class Session:
        def __init__(self) -> None:
            self.statements = []

        async def execute(self, statement):  # type: ignore[no-untyped-def]
            self.statements.append(statement)
            return Result()

    session = Session()
    names = await SqlAlchemyAnalyticsDisplayNameRepository(  # type: ignore[arg-type]
        session
    ).resolve(10, frozenset({101, 202}))

    assert names == {101: "Historical nickname"}
    assert len(session.statements) == 1
    sql = str(session.statements[0])
    assert "guild_members.nickname" in sql
    assert "discord_users.global_name" in sql
    assert "discord_users.username" in sql
    assert "guild_members.left_at" not in sql


def test_navigation_exposes_analytics_to_owner_and_admin_on_desktop_and_mobile() -> (
    None
):
    from discord_stats_bot.web.presentation import render_navigation

    for role in (WebAdminRole.OWNER, WebAdminRole.ADMIN):
        navigation = render_navigation(
            role,
            "csrf",
            active_path="/admin/analytics",
        )
        assert navigation.count('href="/admin/analytics"') == 2
        assert navigation.count('aria-current="page" href="/admin/analytics"') == 2
        assert 'class="navigation desktop-navigation"' in navigation
        assert 'class="navigation mobile-navigation"' in navigation
