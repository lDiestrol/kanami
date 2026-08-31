from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from discord_stats_bot.features.bot_profile import (
    BotGuildProfile,
    BotProfileErrorCategory,
    BotProfileOperationError,
)
from discord_stats_bot.web import WebSettings, create_app
from discord_stats_bot.web.auth import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationDecision,
    WebAdminRole,
)
from discord_stats_bot.web.dashboard import (
    DashboardBotOverview,
    DashboardBotStatus,
    DashboardControlStatus,
    WebAdminDashboard,
)
from discord_stats_bot.web.operations import (
    MINIMUM_GAME_STALE_THRESHOLD_SECONDS,
    VOICE_STALE_THRESHOLD_SECONDS,
    ComponentHealth,
    DataIntegrityHealth,
    DiagnosticReason,
    GitMetadata,
    HealthObservation,
    HealthStatus,
    OperationalHistory,
    OperationsBotState,
    OperationsBotStatus,
    OperationsControlStatus,
    PostgreSQLState,
    SqlAlchemyOperationsRepository,
    TrackingState,
    WebAdminSystemStatus,
    WebAdminSystemStatusService,
    build_operational_history,
    evaluate_integrity,
    evaluate_overall_status,
    evaluate_tracking_health,
)
from discord_stats_bot.web.service import WebDatabaseHealth

DATABASE_URL = "postgresql+asyncpg://test:test@localhost:5432/test"
NOW = datetime(2026, 8, 24, 20, 30, tzinfo=UTC)
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_settings(**overrides: object) -> WebSettings:
    values: dict[str, object] = {
        "DATABASE_URL": DATABASE_URL,
        "DISCORD_GUILD_ID": 10,
        "WEB_ADMIN_DISCORD_CLIENT_ID": 123,
        "WEB_ADMIN_DISCORD_CLIENT_SECRET": "oauth-secret",
        "WEB_ADMIN_DISCORD_REDIRECT_URI": (
            "http://localhost:8000/admin/auth/discord/callback"
        ),
        "WEB_ADMIN_COOKIE_SECURE": False,
        "GAME_TRACKING_ENABLED": True,
        "GAME_CONFIRM_INTERVAL_SECONDS": 60,
    }
    values.update(overrides)
    return WebSettings(_env_file=None, **values)


def tracking_state(
    *,
    open_sessions: int = 2,
    age_seconds: int | None = 12,
    duplicates: int = 0,
    temporal_violations: int = 0,
) -> TrackingState:
    confirmed = None if age_seconds is None else NOW - timedelta(seconds=age_seconds)
    return TrackingState(
        open_sessions,
        confirmed,
        duplicates,
        temporal_violations,
        confirmed,
        0,
    )


def postgresql_state(
    *,
    available: bool = True,
    voice: TrackingState | None = None,
    game: TrackingState | None = None,
) -> PostgreSQLState:
    if not available:
        return PostgreSQLState(False)
    return PostgreSQLState(
        True,
        latency_seconds=0.0124,
        database_size_bytes=1_572_864,
        alembic_revision="20260824_w12",
        voice=voice or tracking_state(),
        game=game or tracking_state(),
    )


def component(status: HealthStatus, code: str = "test") -> ComponentHealth:
    return ComponentHealth(status, (DiagnosticReason(code, code),))


def bot_state(
    status: OperationsBotStatus = OperationsBotStatus.ONLINE,
    control: OperationsControlStatus = OperationsControlStatus.AVAILABLE,
    health: HealthStatus = HealthStatus.HEALTHY,
) -> OperationsBotState:
    return OperationsBotState(status, control, component(health, "bot"))


def evaluated_status(
    *,
    database: PostgreSQLState | None = None,
    bot: OperationsBotState | None = None,
    game_enabled: bool = True,
    game_interval: int = 60,
    git: GitMetadata = GitMetadata("abc123def456", "main"),
    history: OperationalHistory = OperationalHistory(),
) -> WebAdminSystemStatus:
    database = database or postgresql_state()
    bot = bot or bot_state()
    db_health = component(
        HealthStatus.HEALTHY if database.available else HealthStatus.UNAVAILABLE,
        "database",
    )
    voice = evaluate_tracking_health(
        database.voice,
        now=NOW,
        stale_threshold_seconds=VOICE_STALE_THRESHOLD_SECONDS,
    )
    game = evaluate_tracking_health(
        database.game,
        now=NOW,
        stale_threshold_seconds=max(game_interval * 3, 180),
        enabled=game_enabled,
        game=True,
    )
    integrity = evaluate_integrity(database)
    overall = evaluate_overall_status(
        db_health, bot.health, voice.health, game.health, integrity
    )
    return WebAdminSystemStatus(
        generated_at=NOW,
        uptime_seconds=90_061,
        git=git,
        postgresql=database,
        postgresql_health=db_health,
        bot=bot,
        voice=voice,
        game=game,
        integrity=integrity,
        overall_status=overall,
        game_tracking_enabled=game_enabled,
        game_confirm_interval_seconds=game_interval,
        history=history,
    )


def observation(
    age: timedelta,
    status: HealthStatus,
    component_name: str = "System",
    reason: str = "test observation",
) -> HealthObservation:
    return HealthObservation(NOW - age, status, component_name, reason)


def sampled_observations(
    duration: timedelta,
    *,
    statuses: tuple[HealthStatus, ...] = (HealthStatus.HEALTHY,),
    missing_slots: frozenset[int] = frozenset(),
) -> tuple[HealthObservation, ...]:
    sample_count = int(duration.total_seconds() // 60)
    started_at = NOW - duration
    return tuple(
        HealthObservation(
            started_at + timedelta(seconds=slot * 60 + 30),
            statuses[slot % len(statuses)],
            "System",
            "sampled health",
        )
        for slot in range(sample_count)
        if slot not in missing_slots
    )


def test_history_aggregates_healthy_degraded_and_unavailable_observations() -> None:
    history = build_operational_history(
        sampled_observations(
            timedelta(hours=24),
            statuses=(
                HealthStatus.HEALTHY,
                HealthStatus.DEGRADED,
                HealthStatus.UNAVAILABLE,
            ),
        ),
        now=NOW,
    )

    day = history.windows[0]
    assert day.observation_count == 1440
    assert day.healthy_percent == 33.3
    assert day.degraded_percent == 33.3
    assert day.unavailable_percent == 33.3
    assert day.missing_percent == 0.0
    assert day.complete is True


def test_full_sequential_seven_day_window_has_complete_sampling_coverage() -> None:
    history = build_operational_history(
        sampled_observations(timedelta(days=7)),
        now=NOW,
    )

    day, week = history.windows
    assert day.complete is True
    assert week.complete is True
    assert day.covered_sample_count == day.expected_sample_count == 1440
    assert week.covered_sample_count == week.expected_sample_count == 10080
    assert day.healthy_percent == week.healthy_percent == 100.0
    assert day.missing_percent == week.missing_percent == 0.0


def test_partial_history_after_first_deployment_separates_pre_history() -> None:
    missing = frozenset(range(1380))
    history = build_operational_history(
        sampled_observations(timedelta(hours=24), missing_slots=missing),
        now=NOW,
    )

    day, week = history.windows
    assert day.covered_sample_count == 60
    assert day.expected_sample_count == 1440
    assert day.eligible_sample_count == 60
    assert day.not_monitored_sample_count == 1380
    assert day.missing_sample_count == 0
    assert day.coverage_percent == 100.0
    assert day.healthy_percent == 100.0
    assert day.missing_percent == 0.0
    assert day.complete is False
    assert week.complete is False


def test_first_observation_now_does_not_turn_previous_week_into_missing() -> None:
    history = build_operational_history(
        (observation(timedelta(), HealthStatus.HEALTHY),),
        now=NOW,
    )

    week = history.windows[1]
    assert week.expected_sample_count == 10080
    assert week.eligible_sample_count == 1
    assert week.covered_sample_count == 1
    assert week.missing_sample_count == 0
    assert week.not_monitored_sample_count == 10079
    assert week.healthy_percent == 100.0
    assert week.not_monitored_percent == 100.0
    assert week.history_available_since == NOW
    assert week.complete is False


def test_three_days_of_history_leave_seven_day_pre_history_not_missing() -> None:
    history = build_operational_history(
        sampled_observations(timedelta(days=3)),
        now=NOW,
    )

    week = history.windows[1]
    assert week.expected_sample_count == 10080
    assert week.eligible_sample_count == 4320
    assert week.covered_sample_count == 4320
    assert week.missing_sample_count == 0
    assert week.not_monitored_sample_count == 5760
    assert week.coverage_percent == 100.0
    assert week.complete is False


def test_internal_gap_is_visible_even_with_fresh_first_and_last_samples() -> None:
    missing = frozenset(range(600, 660))
    history = build_operational_history(
        sampled_observations(timedelta(hours=24), missing_slots=missing),
        now=NOW,
    )

    day = history.windows[0]
    assert day.history_available_since is not None
    assert day.last_observed_at is not None
    assert day.missing_sample_count == 60
    assert day.longest_gap_samples == 60
    assert day.coverage_percent == 95.8
    assert day.complete is False


def test_missing_observations_at_window_end_are_visible() -> None:
    missing = frozenset(range(1430, 1440))
    history = build_operational_history(
        sampled_observations(timedelta(hours=24), missing_slots=missing),
        now=NOW,
    )

    day = history.windows[0]
    assert day.missing_sample_count == 10
    assert day.longest_gap_samples == 10
    assert day.last_observed_at == NOW - timedelta(minutes=10, seconds=30)
    assert day.complete is False


def test_long_gap_between_healthy_samples_is_not_healthy_100_or_full() -> None:
    missing = frozenset(range(1, 1439))
    history = build_operational_history(
        sampled_observations(timedelta(hours=24), missing_slots=missing),
        now=NOW,
    )

    day = history.windows[0]
    assert day.observation_count == 2
    assert day.healthy_percent == 0.1
    assert day.missing_percent == 99.9
    assert day.longest_gap_samples == 1438
    assert day.complete is False


def test_prewindow_history_makes_leading_window_gap_missing() -> None:
    missing = frozenset(range(10))
    observations = (
        observation(timedelta(days=7, hours=12), HealthStatus.HEALTHY),
        *sampled_observations(timedelta(days=7), missing_slots=missing),
    )
    history = build_operational_history(observations, now=NOW)

    week = history.windows[1]
    assert week.eligible_sample_count == week.expected_sample_count == 10080
    assert week.not_monitored_sample_count == 0
    assert week.covered_sample_count == 10070
    assert week.missing_sample_count == 10
    assert week.longest_gap_samples == 10
    assert week.complete is False


def test_empty_history_has_zero_observations_and_no_incidents() -> None:
    history = build_operational_history((), now=NOW)

    assert all(window.observation_count == 0 for window in history.windows)
    assert all(window.covered_sample_count == 0 for window in history.windows)
    assert all(window.eligible_sample_count == 0 for window in history.windows)
    assert all(window.missing_sample_count == 0 for window in history.windows)
    assert all(window.missing_percent == 0.0 for window in history.windows)
    assert all(window.not_monitored_percent == 100.0 for window in history.windows)
    assert all(not window.complete for window in history.windows)
    assert history.incidents == ()


def test_normal_gap_free_history_preserves_observed_status_percentages() -> None:
    history = build_operational_history(
        sampled_observations(
            timedelta(hours=24),
            statuses=(HealthStatus.HEALTHY,) * 9 + (HealthStatus.DEGRADED,),
        ),
        now=NOW,
    )

    day = history.windows[0]
    assert day.coverage_percent == 100.0
    assert day.healthy_percent == 90.0
    assert day.degraded_percent == 10.0
    assert day.unavailable_percent == 0.0
    assert day.missing_percent == 0.0


def test_incident_start_recovery_and_open_incident_semantics() -> None:
    history = build_operational_history(
        (
            observation(timedelta(minutes=5), HealthStatus.HEALTHY),
            observation(
                timedelta(minutes=4),
                HealthStatus.DEGRADED,
                "Voice Tracking",
                "checkpoint stale for 6m",
            ),
            observation(
                timedelta(minutes=3),
                HealthStatus.DEGRADED,
                "Voice Tracking",
                "checkpoint stale for 7m",
            ),
            observation(timedelta(minutes=2), HealthStatus.HEALTHY),
            observation(
                timedelta(minutes=1),
                HealthStatus.UNAVAILABLE,
                "Discord Gateway",
                "gateway not ready",
            ),
        ),
        now=NOW,
    )

    newest, recovered = history.incidents
    assert newest.started_at == NOW - timedelta(minutes=1)
    assert newest.recovered_at is None
    assert recovered.started_at == NOW - timedelta(minutes=4)
    assert recovered.recovered_at == NOW - timedelta(minutes=2)
    assert recovered.reason == "checkpoint stale for 6m"


def test_incident_escalation_keeps_original_start_until_healthy_recovery() -> None:
    history = build_operational_history(
        (
            observation(
                timedelta(minutes=3),
                HealthStatus.DEGRADED,
                "Voice Tracking",
                "checkpoint stale for 6m",
            ),
            observation(
                timedelta(minutes=2),
                HealthStatus.UNAVAILABLE,
                "Discord Gateway",
                "gateway not ready",
            ),
            observation(
                timedelta(minutes=1),
                HealthStatus.DEGRADED,
                "Voice Tracking",
                "checkpoint stale for 8m",
            ),
            observation(timedelta(), HealthStatus.HEALTHY),
        ),
        now=NOW,
    )

    assert len(history.incidents) == 1
    incident = history.incidents[0]
    assert incident.started_at == NOW - timedelta(minutes=3)
    assert incident.recovered_at == NOW
    assert incident.status is HealthStatus.UNAVAILABLE
    assert incident.component == "Discord Gateway"


def test_system_page_renders_partial_history_and_operator_safe_incident() -> None:
    history = build_operational_history(
        (
            observation(timedelta(minutes=2), HealthStatus.HEALTHY),
            observation(
                timedelta(minutes=1),
                HealthStatus.DEGRADED,
                "Voice Tracking",
                "checkpoint stale for 6m",
            ),
        ),
        now=NOW,
    )
    app = make_app(StaticSystemStatusService(evaluated_status(history=history)))

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/system")

    assert response.status_code == 200
    assert "Доступность 24h / 7d" in response.text
    assert "Частичное окно" in response.text
    assert "мониторинг начался позже выбранного окна" in response.text
    assert "Пропущено наблюдений" in response.text
    assert "Минут в выбранном окне" in response.text
    assert "Not monitored до старта" in response.text
    assert "Минут до начала мониторинга" in response.text
    assert "Диагностика и доступность компонентов Kanami" in response.text
    assert "История доступна с" in response.text
    assert "Coverage с начала мониторинга" in response.text
    assert "Получено наблюдений" in response.text
    assert "Последнее успешное наблюдение" in response.text
    assert "checkpoint stale for 6m" in response.text
    assert "DATABASE_URL" not in response.text
    assert "oauth-secret" not in response.text


def test_web_settings_expose_shared_game_tracking_runtime_contract() -> None:
    settings = make_settings(
        GAME_CONFIRM_INTERVAL_SECONDS=75,
        VOICE_CHECKPOINT_INTERVAL_SECONDS=120,
    )

    assert settings.game_tracking_enabled is True
    assert settings.game_confirm_interval_seconds == 75
    assert settings.voice_checkpoint_interval_seconds == 120


def test_web_deployment_examples_separate_secrets_and_shared_runtime_context() -> None:
    env_example = (
        PROJECT_ROOT / "deploy/systemd/kanami-web-admin.env.example"
    ).read_text(encoding="utf-8")
    configured_keys = {
        line.split("=", 1)[0]
        for line in env_example.splitlines()
        if line and not line.startswith("#") and "=" in line
    }

    assert {
        "DATABASE_URL",
        "DISCORD_GUILD_ID",
        "REPORT_TIMEZONE",
        "VOICE_CHECKPOINT_INTERVAL_SECONDS",
        "GAME_TRACKING_ENABLED",
        "GAME_CONFIRM_INTERVAL_SECONDS",
    } <= configured_keys
    assert "DISCORD_TOKEN" not in configured_keys

    unit = (PROJECT_ROOT / "deploy/systemd/kanami-web-admin.service.example").read_text(
        encoding="utf-8"
    )
    assert "User=kanami-web" in unit
    assert "EnvironmentFile=/etc/kanami/kanami-web-admin.env" in unit
    assert "Environment=HOME=/var/lib/kanami-web" in unit


class FakeResources:
    session_factory = object()

    async def dispose(self) -> None:
        return None


class FakeAdminService:
    async def probe_database(self) -> WebDatabaseHealth:
        return WebDatabaseHealth(True)


class FakeDashboardService:
    async def load(self) -> WebAdminDashboard:
        return WebAdminDashboard(
            WebDatabaseHealth(True),
            None,
            DashboardBotOverview(
                DashboardBotStatus.UNKNOWN,
                DashboardControlStatus.UNAVAILABLE,
            ),
        )


class StaticSystemStatusService:
    def __init__(self, status: WebAdminSystemStatus) -> None:
        self.status = status
        self.calls = 0

    async def load(self) -> WebAdminSystemStatus:
        self.calls += 1
        return self.status


class FakeAuthorizer:
    async def authorize(self, discord_user_id: int) -> WebAdminAuthorizationDecision:
        del discord_user_id
        return WebAdminAuthorizationDecision(True, role=WebAdminRole.ADMIN)


def make_app(service: object) -> Starlette:
    return create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),  # type: ignore[arg-type]
        service_factory=lambda session_factory: FakeAdminService(),  # type: ignore[arg-type]
        dashboard_service_factory=lambda *args: FakeDashboardService(),
        system_status_service_factory=lambda *args: service,  # type: ignore[return-value]
        authorization_service_factory=lambda *args: FakeAuthorizer(),
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


def test_system_status_requires_authentication_without_loading_metrics() -> None:
    service = StaticSystemStatusService(evaluated_status())
    app = make_app(service)

    with TestClient(app) as client:
        response = client.get("/admin/system", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert service.calls == 0


@pytest.mark.parametrize("role", [WebAdminRole.OWNER, WebAdminRole.ADMIN])
def test_owner_and_managed_admin_can_view_evaluated_status(role: WebAdminRole) -> None:
    history = build_operational_history(
        (observation(timedelta(minutes=1), HealthStatus.HEALTHY),),
        now=NOW,
    )
    service = StaticSystemStatusService(evaluated_status(history=history))
    app = make_app(service)

    with authenticated_client(app, role) as client:
        response = client.get("/admin/system")

    assert response.status_code == 200
    assert "Все системы работают" in response.text
    assert "Discord / Bot" in response.text
    assert "PostgreSQL" in response.text
    assert "Voice tracking" in response.text
    assert "Game tracking" in response.text
    assert "Production metadata" in response.text
    assert "Доступность 24h / 7d" in response.text
    assert "Missing" in response.text
    assert "Not monitored" in response.text
    assert "Последние инциденты" in response.text
    assert "Целостность данных" in response.text
    assert "4 / 4 проверок пройдено" in response.text
    assert "abc123def456" in response.text
    assert "20260824_w12" in response.text
    assert "12.4 мс" in response.text
    assert "12 сек назад" in response.text
    assert 'href="/admin/system">Состояние</a>' in response.text
    assert service.calls == 1


def test_partial_window_explains_monitoring_start_and_missing_observations() -> None:
    history = build_operational_history(
        (
            observation(timedelta(minutes=4), HealthStatus.HEALTHY),
            observation(timedelta(minutes=1), HealthStatus.HEALTHY),
        ),
        now=NOW,
    )
    app = make_app(StaticSystemStatusService(evaluated_status(history=history)))

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/system")

    assert response.status_code == 200
    assert "мониторинг начался позже выбранного окна" in response.text
    assert "есть пропущенные наблюдения" in response.text
    assert "Missing" in response.text
    assert "Not monitored до старта" in response.text
    assert "Coverage с начала мониторинга" in response.text
    assert (
        "Healthy, Degraded, Unavailable, Missing и Coverage рассчитаны для части окна, "
        "где мониторинг уже должен был работать. Not monitored показывает часть "
        "выбранного окна до начала мониторинга. Missing и Not monitored не считаются "
        "uptime." in response.text
    )


def test_disabled_game_tracking_is_presented_as_neutral_inactive_state() -> None:
    status = evaluated_status(game_enabled=False)
    app = make_app(StaticSystemStatusService(status))

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/system")

    assert response.status_code == 200
    assert '<article class="card component-card neutral">' in response.text
    assert "Game tracking" in response.text
    assert "Неактивно" in response.text
    assert "Отключено" in response.text
    assert "Недавних инцидентов нет" in response.text


def test_managed_admin_still_cannot_open_owner_only_audit_route() -> None:
    app = make_app(StaticSystemStatusService(evaluated_status()))

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/audit")

    assert response.status_code == 403


def test_dashboard_navigation_contains_status_link_for_both_roles() -> None:
    app = make_app(StaticSystemStatusService(evaluated_status()))

    for role in (WebAdminRole.OWNER, WebAdminRole.ADMIN):
        with authenticated_client(app, role) as client:
            response = client.get("/admin/")
        assert 'href="/admin/system">Состояние</a>' in response.text


def test_overall_all_healthy_is_healthy() -> None:
    status = evaluate_overall_status(
        component(HealthStatus.HEALTHY),
        component(HealthStatus.HEALTHY),
        component(HealthStatus.HEALTHY),
        component(HealthStatus.HEALTHY),
        DataIntegrityHealth(HealthStatus.HEALTHY, ()),
    )

    assert status is HealthStatus.HEALTHY


def test_overall_one_degraded_component_is_degraded() -> None:
    status = evaluate_overall_status(
        component(HealthStatus.HEALTHY),
        component(HealthStatus.HEALTHY),
        component(HealthStatus.DEGRADED),
        component(HealthStatus.NEUTRAL),
        DataIntegrityHealth(HealthStatus.HEALTHY, ()),
    )

    assert status is HealthStatus.DEGRADED


def test_overall_postgresql_unavailable_is_unavailable() -> None:
    status = evaluate_overall_status(
        component(HealthStatus.UNAVAILABLE),
        component(HealthStatus.HEALTHY),
        component(HealthStatus.UNAVAILABLE),
        component(HealthStatus.UNAVAILABLE),
        DataIntegrityHealth(HealthStatus.UNAVAILABLE, ()),
    )

    assert status is HealthStatus.UNAVAILABLE


def test_neutral_disabled_game_does_not_degrade_overall() -> None:
    status = evaluated_status(game_enabled=False)

    assert status.game.health.status is HealthStatus.NEUTRAL
    assert status.overall_status is HealthStatus.HEALTHY


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (None, HealthStatus.HEALTHY),
        (12, HealthStatus.HEALTHY),
        (180, HealthStatus.HEALTHY),
        (181, HealthStatus.DEGRADED),
    ],
)
def test_voice_zero_fresh_boundary_and_stale_health(
    age: int | None,
    expected: HealthStatus,
) -> None:
    state = (
        tracking_state(open_sessions=0, age_seconds=None)
        if age is None
        else tracking_state(age_seconds=age)
    )

    result = evaluate_tracking_health(
        state,
        now=NOW,
        stale_threshold_seconds=VOICE_STALE_THRESHOLD_SECONDS,
    )

    assert result.health.status is expected
    if age is None:
        assert result.health.reasons[0].code == "no_active_sessions"


def test_multiple_open_voice_sessions_degrade_when_oldest_checkpoint_is_stale() -> None:
    result = evaluate_tracking_health(
        TrackingState(
            open_sessions=2,
            last_confirmed_at=NOW - timedelta(seconds=10),
            duplicate_open_session_groups=0,
            temporal_violation_count=0,
            oldest_confirmed_at=NOW - timedelta(seconds=181),
            missing_confirmation_count=0,
        ),
        now=NOW,
        stale_threshold_seconds=180,
    )

    assert result.health.status is HealthStatus.DEGRADED
    assert result.confirmation_age_seconds == 181


def test_multiple_open_game_sessions_degrade_when_oldest_checkpoint_is_stale() -> None:
    result = evaluate_tracking_health(
        TrackingState(
            open_sessions=2,
            last_confirmed_at=NOW - timedelta(seconds=10),
            duplicate_open_session_groups=0,
            temporal_violation_count=0,
            oldest_confirmed_at=NOW - timedelta(seconds=181),
            missing_confirmation_count=0,
        ),
        now=NOW,
        stale_threshold_seconds=180,
        game=True,
    )

    assert result.health.status is HealthStatus.DEGRADED


def test_open_session_with_null_checkpoint_degrades_fresh_peer() -> None:
    result = evaluate_tracking_health(
        TrackingState(
            open_sessions=2,
            last_confirmed_at=NOW - timedelta(seconds=10),
            duplicate_open_session_groups=0,
            temporal_violation_count=0,
            oldest_confirmed_at=NOW - timedelta(seconds=10),
            missing_confirmation_count=1,
        ),
        now=NOW,
        stale_threshold_seconds=180,
    )

    assert result.health.status is HealthStatus.DEGRADED
    assert "confirmation_missing" in {reason.code for reason in result.health.reasons}


@pytest.mark.parametrize(
    ("duplicates", "temporal", "expected_code"),
    [
        (2, 0, "duplicate_open_sessions"),
        (0, 3, "temporal_violations"),
    ],
)
def test_voice_integrity_violation_degrades_component(
    duplicates: int,
    temporal: int,
    expected_code: str,
) -> None:
    result = evaluate_tracking_health(
        tracking_state(duplicates=duplicates, temporal_violations=temporal),
        now=NOW,
        stale_threshold_seconds=180,
    )

    assert result.health.status is HealthStatus.DEGRADED
    assert expected_code in {reason.code for reason in result.health.reasons}


def test_game_disabled_is_neutral() -> None:
    result = evaluate_tracking_health(
        TrackingState(),
        now=NOW,
        stale_threshold_seconds=180,
        enabled=False,
        game=True,
    )

    assert result.health.status is HealthStatus.NEUTRAL
    assert result.health.reasons[0].code == "feature_disabled"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (tracking_state(open_sessions=0, age_seconds=None), HealthStatus.HEALTHY),
        (tracking_state(age_seconds=20), HealthStatus.HEALTHY),
        (tracking_state(age_seconds=181), HealthStatus.DEGRADED),
        (tracking_state(duplicates=1), HealthStatus.DEGRADED),
    ],
)
def test_enabled_game_health_rules(
    state: TrackingState,
    expected: HealthStatus,
) -> None:
    result = evaluate_tracking_health(
        state,
        now=NOW,
        stale_threshold_seconds=180,
        game=True,
    )

    assert result.health.status is expected


@pytest.mark.parametrize(
    ("age", "expected"),
    [(360, HealthStatus.HEALTHY), (361, HealthStatus.DEGRADED)],
)
def test_game_interval_120_uses_360_second_threshold(
    age: int,
    expected: HealthStatus,
) -> None:
    result = evaluate_tracking_health(
        tracking_state(age_seconds=age),
        now=NOW,
        stale_threshold_seconds=max(120 * 3, MINIMUM_GAME_STALE_THRESHOLD_SECONDS),
        game=True,
    )

    assert result.stale_threshold_seconds == 360
    assert result.health.status is expected


def test_integrity_all_zero_passes_all_checks() -> None:
    integrity = evaluate_integrity(postgresql_state())

    assert integrity.status is HealthStatus.HEALTHY
    assert integrity.passed_count == 4
    assert len(integrity.checks) == 4


def test_game_duplicate_and_temporal_violations_are_rendered() -> None:
    database = postgresql_state(
        game=tracking_state(duplicates=2, temporal_violations=3)
    )
    app = make_app(StaticSystemStatusService(evaluated_status(database=database)))

    with authenticated_client(app, WebAdminRole.OWNER) as client:
        response = client.get("/admin/system")

    assert response.status_code == 200
    assert "Games: дубли открытых сессий — обнаружено нарушений: 2" in response.text
    assert "Game timestamps — обнаружено нарушений: 3" in response.text
    assert "2 / 4 проверок пройдено" in response.text
    assert "Есть предупреждения" in response.text


def test_postgresql_failure_marks_integrity_unavailable_and_returns_200() -> None:
    app = make_app(
        StaticSystemStatusService(evaluated_status(database=PostgreSQLState(False)))
    )

    with authenticated_client(app, WebAdminRole.OWNER) as client:
        response = client.get("/admin/system")

    assert response.status_code == 200
    assert "Обнаружены проблемы" in response.text
    assert response.text.count("проверка недоступна") == 4
    assert "Traceback" not in response.text


class FakeRepository:
    def __init__(
        self,
        result: PostgreSQLState | None = None,
        error: Exception | None = None,
        history_error: Exception | None = None,
    ) -> None:
        self.result = result or postgresql_state()
        self.error = error
        self.history_error = history_error
        self.history_since: datetime | None = None

    async def load(self) -> PostgreSQLState:
        if self.error is not None:
            raise self.error
        return self.result

    async def load_history(self, since: datetime) -> tuple[HealthObservation, ...]:
        self.history_since = since
        if self.history_error is not None:
            raise self.history_error
        return ()


class FakeBotControl:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def get_profile(self) -> BotGuildProfile:
        if self.error is not None:
            raise self.error
        return BotGuildProfile(1, "Kanami", "Kanami", None, None, None)


class FakeGitSource:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def load(self) -> GitMetadata:
        if self.error is not None:
            raise self.error
        return GitMetadata("abc123", "main")


def make_status_service(
    *,
    repository: FakeRepository | None = None,
    bot_error: Exception | None = None,
    git_error: Exception | None = None,
    game_enabled: bool = True,
    voice_interval: int = 60,
    game_interval: int = 60,
    clock: object | None = None,
) -> WebAdminSystemStatusService:
    return WebAdminSystemStatusService(
        repository or FakeRepository(),
        bot_control=FakeBotControl(bot_error),  # type: ignore[arg-type]
        git_metadata=FakeGitSource(git_error),
        game_tracking_enabled=game_enabled,
        voice_checkpoint_interval_seconds=voice_interval,
        game_confirm_interval_seconds=game_interval,
        process_started_monotonic=100,
        clock=clock or (lambda: NOW),  # type: ignore[arg-type]
        monotonic=lambda: 160,
    )


@pytest.mark.asyncio
async def test_service_evaluates_healthy_postgresql_and_online_bot() -> None:
    status = await make_status_service().load()

    assert status.postgresql_health.status is HealthStatus.HEALTHY
    assert status.bot.health.status is HealthStatus.HEALTHY
    assert status.bot.status is OperationsBotStatus.ONLINE
    assert status.overall_status is HealthStatus.HEALTHY


@pytest.mark.asyncio
async def test_service_loads_eight_days_to_prove_prewindow_history() -> None:
    repository = FakeRepository()

    await make_status_service(repository=repository).load()

    assert repository.history_since == NOW - timedelta(days=8)


@pytest.mark.asyncio
async def test_service_evaluates_unavailable_postgresql() -> None:
    status = await make_status_service(
        repository=FakeRepository(PostgreSQLState(False))
    ).load()

    assert status.postgresql_health.status is HealthStatus.UNAVAILABLE
    assert status.voice.health.status is HealthStatus.UNAVAILABLE
    assert status.integrity.status is HealthStatus.UNAVAILABLE
    assert status.overall_status is HealthStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_bot_offline_is_unavailable() -> None:
    status = await make_status_service(
        bot_error=BotProfileOperationError(BotProfileErrorCategory.BOT_NOT_READY)
    ).load()

    assert status.bot.status is OperationsBotStatus.OFFLINE
    assert status.bot.health.status is HealthStatus.UNAVAILABLE
    assert status.overall_status is HealthStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_bot_control_unavailable_is_degraded() -> None:
    status = await make_status_service(
        bot_error=BotProfileOperationError(BotProfileErrorCategory.CONTROL_UNAVAILABLE)
    ).load()

    assert status.bot.status is OperationsBotStatus.UNKNOWN
    assert status.bot.control_status is OperationsControlStatus.UNAVAILABLE
    assert status.bot.health.status is HealthStatus.DEGRADED
    assert status.overall_status is HealthStatus.DEGRADED


@pytest.mark.asyncio
async def test_timezone_aware_clock_is_normalized_to_utc() -> None:
    local_now = NOW.astimezone(timezone(timedelta(hours=5)))
    status = await make_status_service(clock=lambda: local_now).load()

    assert status.generated_at == NOW
    assert status.generated_at.tzinfo is UTC
    assert status.voice.confirmation_age_seconds == 12


@pytest.mark.asyncio
async def test_service_uses_configured_voice_checkpoint_interval() -> None:
    database = postgresql_state(
        voice=tracking_state(age_seconds=300),
    )
    status = await make_status_service(
        repository=FakeRepository(database),
        voice_interval=120,
    ).load()

    assert status.voice.stale_threshold_seconds == 360
    assert status.voice.health.status is HealthStatus.HEALTHY


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_text"),
    [
        ("git", "Unknown"),
        ("bot", "Bot Control недоступен"),
        ("repository", "PostgreSQL недоступен"),
    ],
)
async def test_source_exception_isolated_from_http_500(
    failure: str,
    expected_text: str,
) -> None:
    service = make_status_service(
        repository=FakeRepository(
            error=RuntimeError("database secret") if failure == "repository" else None
        ),
        bot_error=RuntimeError("discord secret") if failure == "bot" else None,
        git_error=OSError("git failed") if failure == "git" else None,
    )
    app = make_app(service)

    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/system")

    assert response.status_code == 200
    assert expected_text in response.text
    assert "Traceback" not in response.text
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_history_read_failure_isolated_from_current_status() -> None:
    service = make_status_service(
        repository=FakeRepository(history_error=RuntimeError("database secret"))
    )
    status = await service.load()

    assert status.overall_status is HealthStatus.HEALTHY
    assert status.history.available is False

    app = make_app(StaticSystemStatusService(status))
    with authenticated_client(app, WebAdminRole.ADMIN) as client:
        response = client.get("/admin/system")
    assert response.status_code == 200
    assert "История временно недоступна" in response.text
    assert "secret" not in response.text


class FakeSqlResult:
    def __init__(self, row: object | None = None) -> None:
        self._row = row

    def one(self) -> object:
        assert self._row is not None
        return self._row


class FakeHistorySqlResult(FakeSqlResult):
    def __init__(self, rows: tuple[object, ...]) -> None:
        super().__init__()
        self._rows = rows

    def scalars(self) -> tuple[object, ...]:
        return self._rows


class FakeSqlSession:
    def __init__(self, result: FakeSqlResult | Exception) -> None:
        self.result = result
        self.statements: list[object] = []

    async def __aenter__(self) -> "FakeSqlSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, statement: object) -> FakeSqlResult:
        self.statements.append(statement)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeSqlSessionFactory:
    def __init__(self, sessions: list[FakeSqlSession]) -> None:
        self.sessions = sessions
        self.calls = 0

    def __call__(self) -> FakeSqlSession:
        session = self.sessions[self.calls]
        self.calls += 1
        return session


def tracking_sql_result(*, duplicates: int = 0, temporal: int = 0) -> FakeSqlResult:
    return FakeSqlResult(
        SimpleNamespace(
            open_sessions=2,
            last_confirmed_at=NOW,
            duplicate_open_session_groups=duplicates,
            temporal_violation_count=temporal,
            oldest_confirmed_at=NOW,
            missing_confirmation_count=0,
        )
    )


@pytest.mark.asyncio
async def test_repository_maps_set_based_integrity_metrics() -> None:
    sessions = [
        FakeSqlSession(FakeSqlResult()),
        FakeSqlSession(
            FakeSqlResult(
                SimpleNamespace(
                    database_size=1_572_864,
                    alembic_revision="revision-1",
                )
            )
        ),
        FakeSqlSession(tracking_sql_result(duplicates=1)),
        FakeSqlSession(tracking_sql_result(temporal=2)),
    ]
    session_factory = FakeSqlSessionFactory(sessions)
    monotonic_values = iter((100.0, 100.0124))
    repository = SqlAlchemyOperationsRepository(
        session_factory,  # type: ignore[arg-type]
        guild_id=10,
        monotonic=lambda: next(monotonic_values),
    )

    result = await repository.load()

    assert result.voice == TrackingState(2, NOW, 1, 0, NOW, 0)
    assert result.game == TrackingState(2, NOW, 0, 2, NOW, 0)
    assert result.latency_seconds == pytest.approx(0.0124)
    assert session_factory.calls == 4
    sql = "\n".join(str(session.statements[0]) for session in sessions)
    assert "SELECT 1" in sql
    assert "pg_database_size" in sql
    assert "alembic_version" in sql
    assert "voice_sessions" in sql
    assert "game_sessions" in sql
    assert "GROUP BY" in sql
    assert "HAVING count" in sql
    assert "started_at >" in sql
    assert "confirmed_through_at >" in sql
    assert "min(voice_sessions.confirmed_through_at)" in sql
    assert "voice_sessions.confirmed_through_at IS NULL" in sql


@pytest.mark.asyncio
async def test_repository_probe_failure_stops_optional_queries() -> None:
    session_factory = FakeSqlSessionFactory(
        [FakeSqlSession(RuntimeError("database unavailable"))]
    )
    repository = SqlAlchemyOperationsRepository(
        session_factory,  # type: ignore[arg-type]
        guild_id=10,
        monotonic=lambda: 100.0,
    )

    result = await repository.load()

    assert result == PostgreSQLState(available=False)
    assert session_factory.calls == 1


@pytest.mark.asyncio
async def test_repository_loads_bounded_ordered_history() -> None:
    row = SimpleNamespace(
        observed_at=NOW,
        overall_status="degraded",
        component="Voice Tracking",
        reason="checkpoint stale for 6m",
    )
    session = FakeSqlSession(FakeHistorySqlResult((row,)))
    repository = SqlAlchemyOperationsRepository(
        FakeSqlSessionFactory([session]),  # type: ignore[arg-type]
        guild_id=10,
    )

    result = await repository.load_history(NOW - timedelta(days=7))

    assert result == (
        HealthObservation(
            NOW,
            HealthStatus.DEGRADED,
            "Voice Tracking",
            "checkpoint stale for 6m",
        ),
    )
    sql = str(session.statements[0])
    assert "operational_health_observations.guild_id" in sql
    assert "operational_health_observations.observed_at >=" in sql
    assert "ORDER BY operational_health_observations.observed_at" in sql
