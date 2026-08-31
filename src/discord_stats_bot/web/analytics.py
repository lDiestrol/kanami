"""OWNER/ADMIN Web integration for the Server Analytics read foundation."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import Text, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import HTMLResponse

from discord_stats_bot.common.formatting import format_voice_duration
from discord_stats_bot.features.server_analytics import (
    AnalyticsCoverage,
    AnalyticsMetric,
    AnalyticsPercentState,
    ServerAnalyticsPeriod,
    ServerAnalyticsReport,
    ServerAnalyticsService,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import activity_heatmap_levels
from discord_stats_bot.persistence.models import DiscordUser, GuildMember
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyServerAnalyticsRepository,
)
from discord_stats_bot.web.auth import WebSession
from discord_stats_bot.web.authorization import WebAdminRole
from discord_stats_bot.web.presentation import render_admin_page

logger = logging.getLogger(__name__)
ANALYTICS_TRANSACTION_SETUP_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)


class AnalyticsDisplayNameRepository(Protocol):
    async def resolve(
        self, guild_id: int, user_ids: frozenset[int]
    ) -> Mapping[int, str]: ...


class AnalyticsDomainService(Protocol):
    async def get_report(
        self,
        guild_id: int,
        period: ServerAnalyticsPeriod,
        as_of: datetime,
    ) -> ServerAnalyticsReport: ...


AnalyticsRepositoryFactory = Callable[[AsyncSession], object]
AnalyticsDisplayNameRepositoryFactory = Callable[
    [AsyncSession], AnalyticsDisplayNameRepository
]
AnalyticsDomainServiceFactory = Callable[
    [object, ZoneInfo, int], AnalyticsDomainService
]


@dataclass(frozen=True, slots=True)
class AnalyticsKpiCoverage:
    """Presentation caveats for one KPI's current and comparison windows."""

    current_partial: bool | None
    previous_partial: bool | None


@dataclass(frozen=True, slots=True)
class WebAnalyticsTopVoiceMember:
    user_id: int
    display_name: str
    exact_seconds: int
    estimated_seconds: int

    @property
    def total_seconds(self) -> int:
        return self.exact_seconds + self.estimated_seconds


@dataclass(frozen=True, slots=True)
class WebAnalyticsTopMessageAuthor:
    user_id: int
    display_name: str
    message_count: int


@dataclass(frozen=True, slots=True)
class WebAdminAnalytics:
    """Immutable page model built from one A1 report and one name lookup."""

    report: ServerAnalyticsReport
    period: ServerAnalyticsPeriod
    report_timezone: str
    top_voice_members: tuple[WebAnalyticsTopVoiceMember, ...]
    top_message_authors: tuple[WebAnalyticsTopMessageAuthor, ...]
    voice_hours_coverage: AnalyticsKpiCoverage
    unique_voice_users_coverage: AnalyticsKpiCoverage
    messages_coverage: AnalyticsKpiCoverage
    unique_message_authors_coverage: AnalyticsKpiCoverage
    active_members_coverage: AnalyticsKpiCoverage


def _coverage(coverage: AnalyticsCoverage) -> AnalyticsKpiCoverage:
    return AnalyticsKpiCoverage(
        coverage.current_window_begins_before_earliest_recorded,
        coverage.previous_window_begins_before_earliest_recorded,
    )


def _combined_flag(*values: bool | None) -> bool | None:
    if any(value is True for value in values):
        return True
    if any(value is None for value in values):
        return None
    return False


def _combined_coverage(
    voice: AnalyticsCoverage,
    text: AnalyticsCoverage,
) -> AnalyticsKpiCoverage:
    return AnalyticsKpiCoverage(
        _combined_flag(
            voice.current_window_begins_before_earliest_recorded,
            text.current_window_begins_before_earliest_recorded,
        ),
        _combined_flag(
            voice.previous_window_begins_before_earliest_recorded,
            text.previous_window_begins_before_earliest_recorded,
        ),
    )


class SqlAlchemyAnalyticsDisplayNameRepository:
    """Resolve persisted guild display names in one set-based query."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(
        self,
        guild_id: int,
        user_ids: frozenset[int],
    ) -> Mapping[int, str]:
        if not user_ids:
            return {}
        display_name = func.coalesce(
            func.nullif(GuildMember.nickname, ""),
            func.nullif(DiscordUser.global_name, ""),
            func.nullif(DiscordUser.username, ""),
            cast(DiscordUser.id, Text),
        ).label("display_name")
        rows = (
            await self._session.execute(
                select(DiscordUser.id, display_name)
                .join(
                    GuildMember,
                    (GuildMember.user_id == DiscordUser.id)
                    & (GuildMember.guild_id == guild_id),
                )
                .where(DiscordUser.id.in_(user_ids))
                .order_by(DiscordUser.id.asc())
            )
        ).all()
        return {int(row.id): str(row.display_name) for row in rows}


def _default_domain_service_factory(
    repository: object,
    report_timezone: ZoneInfo,
    min_session_seconds: int,
) -> AnalyticsDomainService:
    return ServerAnalyticsService(
        repository,  # type: ignore[arg-type]
        report_timezone=report_timezone,
        min_session_seconds=min_session_seconds,
    )


class WebAdminAnalyticsService:
    """Load one A1 report and its names inside one repeatable-read snapshot."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        min_session_seconds: int,
        analytics_repository_factory: AnalyticsRepositoryFactory = (
            SqlAlchemyServerAnalyticsRepository
        ),
        display_name_repository_factory: AnalyticsDisplayNameRepositoryFactory = (
            SqlAlchemyAnalyticsDisplayNameRepository
        ),
        domain_service_factory: AnalyticsDomainServiceFactory = (
            _default_domain_service_factory
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
        self._min_session_seconds = min_session_seconds
        self._analytics_repository_factory = analytics_repository_factory
        self._display_name_repository_factory = display_name_repository_factory
        self._domain_service_factory = domain_service_factory
        self._clock = clock

    async def load(self, period: ServerAnalyticsPeriod) -> WebAdminAnalytics | None:
        """Return a page model, or a controlled unavailable state on DB failure."""

        as_of = normalize_observed_at(self._clock())
        try:
            async with self._session_factory() as session:
                await session.execute(text(ANALYTICS_TRANSACTION_SETUP_SQL))
                report = await self._domain_service_factory(
                    self._analytics_repository_factory(session),
                    self._report_timezone,
                    self._min_session_seconds,
                ).get_report(self._guild_id, period, as_of)
                user_ids = frozenset(
                    item.user_id
                    for item in (
                        *report.top_voice_members,
                        *report.top_message_authors,
                    )
                )
                names = await self._display_name_repository_factory(session).resolve(
                    self._guild_id,
                    user_ids,
                )
        except Exception as error:
            logger.warning(
                "web_admin_analytics_load_failed error_type=%s",
                type(error).__name__,
            )
            return None

        def name(user_id: int) -> str:
            return names.get(user_id, str(user_id))

        voice_coverage = _coverage(report.voice_coverage)
        text_coverage = _coverage(report.text_coverage)
        return WebAdminAnalytics(
            report=report,
            period=period,
            report_timezone=self._report_timezone.key,
            top_voice_members=tuple(
                WebAnalyticsTopVoiceMember(
                    item.user_id,
                    name(item.user_id),
                    item.exact_seconds,
                    item.estimated_seconds,
                )
                for item in report.top_voice_members
            ),
            top_message_authors=tuple(
                WebAnalyticsTopMessageAuthor(
                    item.user_id,
                    name(item.user_id),
                    item.message_count,
                )
                for item in report.top_message_authors
            ),
            voice_hours_coverage=voice_coverage,
            unique_voice_users_coverage=voice_coverage,
            messages_coverage=text_coverage,
            unique_message_authors_coverage=text_coverage,
            active_members_coverage=_combined_coverage(
                report.voice_coverage,
                report.text_coverage,
            ),
        )


WEEKDAY_SHORT = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
WEEKDAY_NAMES = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)


def _local_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _signed(value: int, formatted: str) -> str:
    if value > 0:
        return f"+{formatted}"
    if value < 0:
        return f"−{formatted}"
    return formatted


def _delta(metric: AnalyticsMetric, *, duration: bool = False) -> str:
    if metric.percent_state is AnalyticsPercentState.NO_BASELINE:
        return '<span class="delta-state neutral">Нет базы для сравнения</span>'
    if metric.percent_state is AnalyticsPercentState.UNCHANGED_ZERO:
        return '<span class="delta-state neutral">Без изменений</span>'
    assert metric.percent_delta is not None
    absolute = (
        format_voice_duration(abs(metric.absolute_delta))
        if duration
        else f"{abs(metric.absolute_delta):,}".replace(",", " ")
    )
    if metric.absolute_delta > 0:
        tone = "positive"
    elif metric.absolute_delta < 0:
        tone = "negative"
    else:
        tone = "neutral"
    return (
        f'<span class="delta-state {tone}">'
        f'<span class="delta-absolute">{_signed(metric.absolute_delta, absolute)}</span>'
        f'<span class="delta-percent">{metric.percent_delta:+.1f}%</span></span>'
    )


def _current_caveat(coverage: AnalyticsKpiCoverage) -> str:
    if coverage.current_partial is True:
        return '<p class="kpi-caveat warning">История периода может быть неполной</p>'
    if coverage.current_partial is None:
        return '<p class="kpi-caveat neutral">Источник за период пока пуст</p>'
    return ""


def _previous_caveat(coverage: AnalyticsKpiCoverage) -> str:
    if coverage.previous_partial is True:
        return '<p class="comparison-caveat warning">База сравнения может быть неполной</p>'
    if coverage.previous_partial is None:
        return (
            '<p class="comparison-caveat neutral">Источник для базы сравнения пуст</p>'
        )
    return ""


def _metric_card(
    label: str,
    value: str,
    metric: AnalyticsMetric,
    coverage: AnalyticsKpiCoverage,
    *,
    key: str,
    duration: bool = False,
    secondary: str = "",
) -> str:
    return (
        f'<article class="analytics-kpi" data-kpi="{escape(key, quote=True)}">'
        f'<p class="analytics-kpi-label">{escape(label)}</p>'
        f'<strong class="analytics-kpi-value">{escape(value)}</strong>{secondary}'
        f"{_current_caveat(coverage)}"
        f'<div class="analytics-comparison"><span>К предыдущему периоду</span>'
        f"{_delta(metric, duration=duration)}{_previous_caveat(coverage)}</div></article>"
    )


def _coverage_card(label: str, coverage: AnalyticsCoverage) -> str:
    if coverage.earliest_recorded_on is None:
        earliest = (
            '<strong class="coverage-empty">Записанной активности пока нет</strong>'
        )
    else:
        earliest = (
            '<span class="coverage-label">Самая ранняя записанная активность</span>'
            f'<time datetime="{coverage.earliest_recorded_on.isoformat()}">'
            f"{_local_date(coverage.earliest_recorded_on)}</time>"
        )
    warnings = []
    if coverage.current_window_begins_before_earliest_recorded is True:
        warnings.append(
            '<span class="badge warning">Текущий период может быть частичным</span>'
        )
    if coverage.previous_window_begins_before_earliest_recorded is True:
        warnings.append(
            '<span class="badge warning">База сравнения может быть частичной</span>'
        )
    if coverage.earliest_recorded_on is not None and not warnings:
        warnings.append(
            '<span class="badge neutral">Нет признака поздней первой записи в этих окнах</span>'
        )
    return (
        '<article class="coverage-source">'
        f'<h3>{escape(label)}</h3><div class="coverage-earliest">{earliest}</div>'
        f'<div class="coverage-flags">{"".join(warnings)}</div></article>'
    )


def _daily_chart(analytics: WebAdminAnalytics, *, voice: bool) -> str:
    points = analytics.report.daily
    kind = "voice" if voice else "messages"
    values = tuple(
        point.voice_exact_seconds + point.voice_estimated_seconds
        if voice
        else point.messages
        for point in points
    )
    maximum = max(values, default=0)
    bars = []
    for point, value in zip(points, values, strict=True):
        height = value / maximum * 100 if maximum else 0.0
        if voice:
            value_label = format_voice_duration(value)
            estimated_label = (
                f"Оценочно: {format_voice_duration(point.voice_estimated_seconds)}"
                if point.voice_estimated_seconds > 0
                else ""
            )
            aria = f"{_local_date(point.local_date)}: Voice {value_label}"
            if estimated_label:
                aria = f"{aria}, {estimated_label.lower()}"
        else:
            value_label = f"{value:,}".replace(",", " ")
            estimated_label = ""
            aria = f"{_local_date(point.local_date)}: {value_label} сообщений"
        estimated = (
            f'<small class="daily-estimated">{escape(estimated_label)}</small>'
            if estimated_label
            else ""
        )
        bars.append(
            f'<li class="daily-point daily-point-{kind}" aria-label="{escape(aria, quote=True)}">'
            f'<span class="daily-value">{escape(value_label)}</span>{estimated}'
            '<span class="daily-bar-track" aria-hidden="true">'
            f'<span class="daily-bar-fill" style="--bar-height:{height:.2f}%"></span></span>'
            f'<time datetime="{point.local_date.isoformat()}">{point.local_date:%d.%m}</time></li>'
        )
    zero_note = (
        '<p class="empty analytics-chart-empty">За выбранный период активности нет.</p>'
        if maximum == 0
        else ""
    )
    return (
        f'{zero_note}<div class="analytics-chart-scroll"><ol class="daily-chart daily-chart-{kind} '
        f'daily-chart-{len(points)}" aria-label="Ежедневная динамика">'
        f"{''.join(bars)}</ol></div>"
    )


def _activity_pattern(analytics: WebAdminAnalytics) -> str:
    activity = analytics.report.voice_activity
    if not activity.has_activity:
        summary = (
            '<p class="empty">Недостаточно Voice activity для выводов о паттерне.</p>'
        )
    else:
        top_hours = "".join(
            f'<span class="badge accent">{hour:02d}:00–{(hour + 1) % 24:02d}:00</span>'
            for hour in activity.top_hours
        )
        active_weekday = (
            WEEKDAY_NAMES[activity.active_weekday]
            if activity.active_weekday is not None
            else "—"
        )
        if activity.quietest_period is None:
            quiet = "—"
        else:
            quiet_weekday, quiet_hour = activity.quietest_period
            quiet = (
                f"{WEEKDAY_NAMES[quiet_weekday]}, "
                f"{quiet_hour:02d}:00–{(quiet_hour + 3) % 24:02d}:00"
            )
        summary = (
            '<div class="activity-summary">'
            f'<div><span>Самые активные часы</span><strong class="activity-hours">{top_hours}</strong></div>'
            f"<div><span>Самый активный день</span><strong>{escape(active_weekday)}</strong></div>"
            f"<div><span>Самое тихое окно</span><strong>{escape(quiet)}</strong></div></div>"
        )

    levels = activity_heatmap_levels(activity.heatmap_activity)
    rows = []
    for row_index, (values, row_levels) in enumerate(
        zip(activity.heatmap_activity, levels, strict=True)
    ):
        started_hour = row_index * 3
        slot = f"{started_hour:02d}–{started_hour + 3:02d}"
        cells = []
        for weekday, (value, level) in enumerate(zip(values, row_levels, strict=True)):
            level_index = ("·", "░", "▒", "▓", "█").index(level)
            label = f"{WEEKDAY_SHORT[weekday]}, {slot}: {value:.2f}"
            cells.append(
                f'<td class="heatmap-cell level-{level_index}">'
                f'<span class="visually-hidden">{escape(label)}</span>'
                f'<span aria-hidden="true">{level}</span></td>'
            )
        rows.append(f'<tr><th scope="row">{slot}</th>{"".join(cells)}</tr>')
    estimated_note = (
        '<p class="activity-estimated-note notice info">Паттерн включает восстановленные оценочные участки Voice.</p>'
        if activity.has_estimated_time
        else ""
    )
    headings = "".join(f'<th scope="col">{day}</th>' for day in WEEKDAY_SHORT)
    heatmap = (
        '<div class="heatmap-scroll"><table class="activity-heatmap">'
        '<caption class="visually-hidden">Нормализованная Voice activity по трёхчасовым окнам и дням недели</caption>'
        f'<thead><tr><th scope="col">Время</th>{headings}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )
    return f"{summary}{estimated_note}{heatmap}"


def _rankings(analytics: WebAdminAnalytics) -> str:
    if analytics.top_voice_members:
        voice_items = "".join(
            '<li class="ranking-item">'
            f'<span class="ranking-position" aria-label="Место {rank}">{rank}</span>'
            f'<div class="ranking-member"><a href="/admin/members/{item.user_id}">'
            f"{escape(item.display_name)}</a><code>{item.user_id}</code></div>"
            f"<strong>{escape(format_voice_duration(item.total_seconds))}</strong>"
            + (
                f"<small>Оценочно: {escape(format_voice_duration(item.estimated_seconds))}</small>"
                if item.estimated_seconds > 0
                else ""
            )
            + "</li>"
            for rank, item in enumerate(analytics.top_voice_members, 1)
        )
        voice = f'<ol class="ranking-list">{voice_items}</ol>'
    else:
        voice = '<p class="empty">За период нет Voice activity для рейтинга.</p>'
    if analytics.top_message_authors:
        message_items = "".join(
            '<li class="ranking-item">'
            f'<span class="ranking-position" aria-label="Место {rank}">{rank}</span>'
            f'<div class="ranking-member"><a href="/admin/members/{item.user_id}">'
            f"{escape(item.display_name)}</a><code>{item.user_id}</code></div>"
            f"<strong>{f'{item.message_count:,}'.replace(',', ' ')}</strong></li>"
            for rank, item in enumerate(analytics.top_message_authors, 1)
        )
        messages = f'<ol class="ranking-list">{message_items}</ol>'
    else:
        messages = '<p class="empty">За период нет сообщений для рейтинга.</p>'
    return (
        '<div class="analytics-rankings">'
        f'<section class="analytics-panel"><div class="section-heading"><h2>Top Voice</h2>'
        f"<p>Рейтинг по общему времени</p></div>{voice}</section>"
        f'<section class="analytics-panel"><div class="section-heading"><h2>Top Messages</h2>'
        f"<p>Рейтинг по числу сообщений</p></div>{messages}</section></div>"
    )


def render_analytics_page(
    analytics: WebAdminAnalytics | None,
    *,
    selected_period: ServerAnalyticsPeriod,
    csrf_token: str,
    role: WebAdminRole,
    invalid_period: bool = False,
) -> str:
    """Render the complete responsive A2.2 presentation from one A1 report."""

    period_actions = (
        '<nav class="analytics-period-selector" aria-label="Период Analytics">'
        f'<a class="button secondary" href="/admin/analytics?period=7d"'
        f"{' aria-current="page"' if selected_period is ServerAnalyticsPeriod.LAST_7_DAYS else ''}>7 дней</a>"
        f'<a class="button secondary" href="/admin/analytics?period=30d"'
        f"{' aria-current="page"' if selected_period is ServerAnalyticsPeriod.LAST_30_DAYS else ''}>30 дней</a></nav>"
    )
    if invalid_period:
        body = (
            '<section class="notice warning"><strong>Некорректный период.</strong> '
            "Поддерживаются только 7d и 30d.</section>"
        )
    elif analytics is None:
        body = (
            '<section class="notice failure"><strong>Analytics временно недоступна.</strong> '
            "Не удалось загрузить данные отчёта.</section>"
        )
    else:
        report = analytics.report
        estimated_seconds = report.voice_person_time.estimated_seconds.current
        voice_secondary = (
            f'<p class="kpi-secondary">Оценочно: {escape(format_voice_duration(estimated_seconds))}</p>'
            if estimated_seconds > 0
            else ""
        )
        metrics = "".join(
            (
                _metric_card(
                    "Активные участники",
                    f"{report.active_members.current:,}".replace(",", " "),
                    report.active_members,
                    analytics.active_members_coverage,
                    key="active-members",
                ),
                _metric_card(
                    "Voice",
                    format_voice_duration(
                        report.voice_person_time.total_seconds.current
                    ),
                    report.voice_person_time.total_seconds,
                    analytics.voice_hours_coverage,
                    key="voice",
                    duration=True,
                    secondary=voice_secondary,
                ),
                _metric_card(
                    "Сообщения",
                    f"{report.messages.current:,}".replace(",", " "),
                    report.messages,
                    analytics.messages_coverage,
                    key="messages",
                ),
                _metric_card(
                    "Участники Voice",
                    f"{report.unique_voice_users.current:,}".replace(",", " "),
                    report.unique_voice_users,
                    analytics.unique_voice_users_coverage,
                    key="voice-users",
                ),
                _metric_card(
                    "Авторы сообщений",
                    f"{report.unique_message_authors.current:,}".replace(",", " "),
                    report.unique_message_authors,
                    analytics.unique_message_authors_coverage,
                    key="message-authors",
                ),
            )
        )
        current_last_day = report.window.current_ended_before - timedelta(days=1)
        body = f"""<section class="analytics-context" aria-label="Контекст отчёта">
<div><span>Выбранный период</span><strong>{analytics.period.days} завершённых дней</strong></div>
<div><span>Диапазон</span><strong><time datetime="{report.window.current_started_on.isoformat()}">{_local_date(report.window.current_started_on)}</time> — <time datetime="{current_last_day.isoformat()}">{_local_date(current_last_day)}</time></strong></div>
<div><span>Часовой пояс</span><strong>{escape(analytics.report_timezone)}</strong></div>
<p>Сегодня не входит в отчёт: используются только завершённые локальные календарные дни.</p>
</section>
<section class="analytics-section" aria-labelledby="coverage-title"><div class="section-heading"><h2 id="coverage-title">Источники данных</h2><p>Voice и сообщения оцениваются независимо</p></div>
<div class="coverage-grid">{_coverage_card("Voice", report.voice_coverage)}{_coverage_card("Messages", report.text_coverage)}</div>
<p class="coverage-method">Указана только самая ранняя найденная записанная активность. Это не дата запуска сбора и не гарантия полноты истории.</p></section>
<section class="analytics-section" aria-labelledby="kpi-title"><div class="section-heading"><h2 id="kpi-title">Основные показатели</h2><p>Сравнение с предыдущим равным периодом</p></div>
<div class="analytics-kpi-grid">{metrics}</div></section>
<section class="analytics-panel analytics-section" aria-labelledby="voice-chart-title"><div class="section-heading"><h2 id="voice-chart-title">Voice activity</h2><p>Общее eligible Voice время по завершённым дням</p></div>{_daily_chart(analytics, voice=True)}</section>
<section class="analytics-panel analytics-section" aria-labelledby="messages-chart-title"><div class="section-heading"><h2 id="messages-chart-title">Messages</h2><p>Persisted сообщения по завершённым дням</p></div>{_daily_chart(analytics, voice=False)}</section>
<section class="analytics-panel analytics-section" aria-labelledby="activity-pattern-title"><div class="section-heading"><h2 id="activity-pattern-title">Паттерн Voice активности</h2><p>Нормализованные трёхчасовые окна · {escape(analytics.report_timezone)}</p></div>{_activity_pattern(analytics)}</section>
{_rankings(analytics)}
<section class="analytics-methodology notice info"><h2>Как считается активность</h2><p>Активный участник — не бот, у которого за выбранный период есть хотя бы одно сохранённое сообщение или учитываемая Voice activity вне AFK. Игры в этот показатель не входят.</p><p>Voice включает точное и, когда оно есть, восстановленное оценочное время. Проценты не скрываются при потенциально неполной истории, а сопровождаются предупреждением.</p></section>"""
    return render_admin_page(
        "Server Analytics",
        body,
        role=role,
        csrf_token=csrf_token,
        active_path="/admin/analytics",
        description="Voice и текстовая активность сервера за завершённые локальные календарные дни",
        actions=period_actions,
        wide=True,
        kicker="Analytics",
    )


async def admin_analytics(request: Request) -> HTMLResponse:
    raw_period = request.query_params.get("period", ServerAnalyticsPeriod.LAST_7_DAYS)
    try:
        period = ServerAnalyticsPeriod(raw_period)
    except ValueError:
        period = ServerAnalyticsPeriod.LAST_7_DAYS
        invalid = True
    else:
        invalid = False
    session: WebSession = request.state.web_session
    if invalid:
        return HTMLResponse(
            render_analytics_page(
                None,
                selected_period=period,
                csrf_token=session.csrf_token,
                role=session.role,
                invalid_period=True,
            ),
            status_code=400,
        )
    service: WebAdminAnalyticsService = request.state.analytics_service
    analytics = await service.load(period)
    return HTMLResponse(
        render_analytics_page(
            analytics,
            selected_period=period,
            csrf_token=session.csrf_token,
            role=session.role,
        ),
        status_code=200 if analytics is not None else 503,
    )
