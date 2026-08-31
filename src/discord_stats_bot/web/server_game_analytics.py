"""OWNER/ADMIN Web integration for Server Game Analytics."""

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse

from discord_stats_bot.common.formatting import format_voice_duration
from discord_stats_bot.features.game_tracking import (
    ServerGameStatistics,
    ServerGameStatisticsPeriod,
    ServerGameStatisticsService,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyServerGameStatisticsRepository,
)
from discord_stats_bot.web.auth import WebSession
from discord_stats_bot.web.authorization import WebAdminRole
from discord_stats_bot.web.presentation import render_admin_page

logger = logging.getLogger(__name__)
SERVER_GAME_ANALYTICS_TRANSACTION_SETUP_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)


class ServerGameAnalyticsDomainService(Protocol):
    async def get_report(
        self,
        guild_id: int,
        period: ServerGameStatisticsPeriod,
        as_of: datetime,
    ) -> ServerGameStatistics: ...


ServerGameRepositoryFactory = Callable[[AsyncSession], object]
ServerGameDomainServiceFactory = Callable[
    [object, ZoneInfo], ServerGameAnalyticsDomainService
]


def parse_server_game_period(values: Sequence[str]) -> ServerGameStatisticsPeriod:
    """Default every unsupported, missing, empty or duplicate shape to 30d."""

    if len(values) != 1:
        return ServerGameStatisticsPeriod.LAST_30_DAYS
    try:
        return ServerGameStatisticsPeriod(values[0])
    except ValueError:
        return ServerGameStatisticsPeriod.LAST_30_DAYS


def _default_domain_service_factory(
    repository: object,
    report_timezone: ZoneInfo,
) -> ServerGameAnalyticsDomainService:
    return ServerGameStatisticsService(
        repository,  # type: ignore[arg-type]
        report_timezone=report_timezone,
    )


class WebAdminServerGameAnalyticsService:
    """Load one consistent guild report with two bounded set-based reads."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        repository_factory: ServerGameRepositoryFactory = (
            SqlAlchemyServerGameStatisticsRepository
        ),
        domain_service_factory: ServerGameDomainServiceFactory = (
            _default_domain_service_factory
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
        self._repository_factory = repository_factory
        self._domain_service_factory = domain_service_factory
        self._clock = clock

    async def load(
        self,
        period: ServerGameStatisticsPeriod,
    ) -> ServerGameStatistics | None:
        as_of = normalize_observed_at(self._clock())
        try:
            async with self._session_factory() as session:
                await session.execute(text(SERVER_GAME_ANALYTICS_TRANSACTION_SETUP_SQL))
                return await self._domain_service_factory(
                    self._repository_factory(session),
                    self._report_timezone,
                ).get_report(self._guild_id, period, as_of)
        except Exception as error:
            logger.warning(
                "web_admin_server_game_analytics_load_failed error_type=%s",
                type(error).__name__,
            )
            return None


def _local_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _period_actions(period: ServerGameStatisticsPeriod) -> str:
    links = []
    for value, label in (
        (ServerGameStatisticsPeriod.LAST_7_DAYS, "7 дней"),
        (ServerGameStatisticsPeriod.LAST_30_DAYS, "30 дней"),
        (ServerGameStatisticsPeriod.LAST_90_DAYS, "90 дней"),
    ):
        current = ' aria-current="page"' if value is period else ""
        links.append(
            f'<a class="button secondary" href="/admin/games?period={value.value}"'
            f"{current}>{label}</a>"
        )
    return (
        '<nav class="analytics-period-selector" aria-label="Период игровой аналитики">'
        + "".join(links)
        + "</nav>"
    )


def _coverage(report: ServerGameStatistics) -> str:
    if report.earliest_recorded_on is None:
        earliest = (
            '<strong class="coverage-empty">Записанной активности пока нет</strong>'
        )
        badge = ""
    else:
        earliest = (
            '<span class="coverage-label">Самая ранняя записанная активность</span>'
            f'<time datetime="{report.earliest_recorded_on.isoformat()}">'
            f"{_local_date(report.earliest_recorded_on)}</time>"
        )
        badge = (
            '<span class="badge warning">История выбранного периода может быть неполной</span>'
            if report.period_may_be_partial
            else '<span class="badge neutral">Нет признака поздней первой записи в этом окне</span>'
        )
    return (
        '<section class="analytics-section" aria-labelledby="game-coverage-title">'
        '<div class="section-heading"><h2 id="game-coverage-title">Источник данных</h2>'
        "<p>Подтверждённые игровые сессии</p></div>"
        '<div class="coverage-grid server-game-coverage"><article class="coverage-source">'
        f'<h3>Game Tracking</h3><div class="coverage-earliest">{earliest}</div>'
        f'<div class="coverage-flags">{badge}</div></article></div>'
        '<p class="coverage-method">Указана только самая ранняя найденная записанная '
        "активность. Это не точная дата включения Game Tracking и не гарантия "
        "полноты истории.</p></section>"
    )


def _kpis(report: ServerGameStatistics) -> str:
    cards = (
        ("Игровое время", format_voice_duration(report.total_seconds), "time"),
        ("Игроков", str(report.active_gamers), "players"),
        ("Разных игр", str(report.unique_games), "games"),
        (
            "В среднем на игрока",
            format_voice_duration(report.average_seconds_per_gamer),
            "average",
        ),
    )
    return "".join(
        '<article class="analytics-kpi" '
        f'data-server-game-kpi="{key}"><p class="analytics-kpi-label">'
        f'{escape(label)}</p><strong class="analytics-kpi-value">{escape(value)}</strong></article>'
        for label, value, key in cards
    )


def _daily_chart(report: ServerGameStatistics) -> str:
    maximum = max((point.total_seconds for point in report.daily), default=0)
    bars = []
    for point in report.daily:
        height = point.total_seconds / maximum * 100 if maximum else 0.0
        duration = format_voice_duration(point.total_seconds)
        aria = (
            f"{_local_date(point.local_date)}: {duration}, "
            f"игроков {point.unique_gamers}"
        )
        bars.append(
            '<li class="daily-point daily-point-games" '
            f'aria-label="{escape(aria, quote=True)}">'
            f'<span class="daily-value">{escape(duration)}</span>'
            f'<small class="daily-estimated">Игроков: {point.unique_gamers}</small>'
            '<span class="daily-bar-track" aria-hidden="true">'
            f'<span class="daily-bar-fill" style="--bar-height:{height:.2f}%"></span></span>'
            f'<time datetime="{point.local_date.isoformat()}">{point.local_date:%d.%m}</time></li>'
        )
    zero_note = (
        '<p class="empty analytics-chart-empty">За выбранный период игровой активности не найдено.</p>'
        if maximum == 0
        else ""
    )
    return (
        f'{zero_note}<div class="analytics-chart-scroll"><ol class="daily-chart '
        f'daily-chart-games daily-chart-{len(report.daily)}" '
        f'aria-label="Игровая активность по дням">{"".join(bars)}</ol></div>'
    )


def _top_games(report: ServerGameStatistics) -> str:
    if not report.top_games:
        return '<p class="empty">За период нет игр для рейтинга.</p>'
    rows = "".join(
        '<li class="ranking-item server-game-ranking-item">'
        f'<span class="ranking-position" aria-label="Место {rank}">{rank}</span>'
        f'<div class="ranking-member"><strong>{escape(item.game_name)}</strong>'
        f"<small>Игроков: {item.unique_gamers}</small></div>"
        f"<strong>{escape(format_voice_duration(item.total_seconds))}</strong>"
        f"<small>Доля: {_game_share(item.total_seconds, report.total_seconds)}</small></li>"
        for rank, item in enumerate(report.top_games, 1)
    )
    return f'<ol class="ranking-list">{rows}</ol>'


def _game_share(item_seconds: int, total_seconds: int) -> str:
    if total_seconds <= 0:
        return "—"
    return f"{item_seconds / total_seconds * 100:.1f}%"


def _top_players(report: ServerGameStatistics) -> str:
    if not report.top_players:
        return '<p class="empty">За период нет игроков для рейтинга.</p>'
    rows = "".join(
        '<li class="ranking-item server-game-ranking-item">'
        f'<span class="ranking-position" aria-label="Место {rank}">{rank}</span>'
        f'<div class="ranking-member"><a href="/admin/members/{item.user_id}">'
        f"{escape(item.display_name)}</a><small>Игр: {item.unique_games} · "
        f"Игровых дней: {item.gaming_days}</small></div>"
        f"<strong>{escape(format_voice_duration(item.total_seconds))}</strong></li>"
        for rank, item in enumerate(report.top_players, 1)
    )
    return f'<ol class="ranking-list">{rows}</ol>'


def render_server_game_analytics_page(
    report: ServerGameStatistics | None,
    *,
    selected_period: ServerGameStatisticsPeriod,
    csrf_token: str,
    role: WebAdminRole,
) -> str:
    if report is None:
        body = (
            '<section class="notice failure"><strong>Игровая аналитика временно '
            "недоступна.</strong> Не удалось загрузить данные отчёта.</section>"
        )
    else:
        last_day = report.window.ended_before - timedelta(days=1)
        empty = (
            '<section class="notice server-games-empty">'
            "За выбранный период игровой активности не найдено.</section>"
            if not report.has_data
            else ""
        )
        body = f"""<section class="analytics-context" aria-label="Контекст игрового отчёта">
<div><span>Выбранный период</span><strong>{report.period.days} завершённых дней</strong></div>
<div><span>Диапазон</span><strong><time datetime="{report.window.started_on.isoformat()}">{_local_date(report.window.started_on)}</time> — <time datetime="{last_day.isoformat()}">{_local_date(last_day)}</time></strong></div>
<div><span>Часовой пояс</span><strong>{escape(report.window.timezone_name)}</strong></div>
<p>Сегодня не входит в отчёт: используются только завершённые локальные календарные дни.</p>
</section>
{_coverage(report)}
{empty}
<section class="analytics-section" aria-labelledby="game-kpi-title"><div class="section-heading"><h2 id="game-kpi-title">Основные показатели</h2><p>Подтверждённое игровое person-time</p></div><div class="analytics-kpi-grid server-game-kpi-grid">{_kpis(report)}</div></section>
<section class="analytics-panel analytics-section" aria-labelledby="game-chart-title"><div class="section-heading"><h2 id="game-chart-title">Игровая активность по дням</h2><p>Person-time и уникальные игроки · {escape(report.window.timezone_name)}</p></div>{_daily_chart(report)}</section>
<div class="analytics-rankings server-game-rankings"><section class="analytics-panel"><div class="section-heading"><h2>TOP игр</h2><p>По подтверждённому person-time</p></div>{_top_games(report)}</section><section class="analytics-panel"><div class="section-heading"><h2>TOP игроков</h2><p>Persisted identity, игры и игровые дни</p></div>{_top_players(report)}</section></div>
<section class="analytics-methodology notice info"><h2>Как считается игровая активность</h2><p>Игровое время — сумма подтверждённого времени участников: одновременная игра двух пользователей в течение часа даёт два часа person-time. Открытая сессия ограничена последним <code>confirmed_through_at</code>.</p><p>Названия объединяются той же canonicalization, что используется в member Game Analytics и <code>/games</code>. Приложения типа Playing не классифицируются и не фильтруются.</p></section>"""
    return render_admin_page(
        "Server Game Analytics",
        body,
        role=role,
        csrf_token=csrf_token,
        active_path="/admin/games",
        description="Игровая активность сервера за завершённые локальные дни",
        actions=_period_actions(selected_period),
        wide=True,
        kicker="Games",
    )


async def admin_games(request: Request) -> HTMLResponse:
    period = parse_server_game_period(request.query_params.getlist("period"))
    service: WebAdminServerGameAnalyticsService = (
        request.state.server_game_analytics_service
    )
    report = await service.load(period)
    session: WebSession = request.state.web_session
    return HTMLResponse(
        render_server_game_analytics_page(
            report,
            selected_period=period,
            csrf_token=session.csrf_token,
            role=session.role,
        ),
        status_code=200 if report is not None else 503,
    )
