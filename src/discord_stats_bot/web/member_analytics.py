"""Web Admin integration for member-scoped activity analytics."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html import escape
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.common.formatting import format_voice_duration
from discord_stats_bot.features.member_analytics import (
    MemberAnalyticsReport,
    MemberAnalyticsService,
)
from discord_stats_bot.features.server_analytics import (
    AnalyticsCoverage,
    AnalyticsMetric,
    AnalyticsPercentState,
    ServerAnalyticsPeriod,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyMemberAnalyticsRepository,
)

logger = logging.getLogger(__name__)
MEMBER_ANALYTICS_TRANSACTION_SETUP_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)


class MemberAnalyticsDomainService(Protocol):
    async def get_report(
        self,
        guild_id: int,
        user_id: int,
        period: ServerAnalyticsPeriod,
        as_of: datetime,
    ) -> MemberAnalyticsReport: ...


MemberAnalyticsRepositoryFactory = Callable[[AsyncSession], object]
MemberAnalyticsDomainServiceFactory = Callable[
    [object, ZoneInfo, int], MemberAnalyticsDomainService
]


@dataclass(frozen=True, slots=True)
class MemberAnalyticsKpiCoverage:
    """Presentation caveats for one member KPI and its comparison."""

    current_partial: bool | None
    previous_partial: bool | None


@dataclass(frozen=True, slots=True)
class WebAdminMemberAnalytics:
    """Presentation-safe member activity page model."""

    report: MemberAnalyticsReport
    period: ServerAnalyticsPeriod
    report_timezone: str
    voice_coverage: MemberAnalyticsKpiCoverage
    messages_coverage: MemberAnalyticsKpiCoverage
    active_days_coverage: MemberAnalyticsKpiCoverage


def parse_member_analytics_period(values: Sequence[str]) -> ServerAnalyticsPeriod:
    """Accept one allowlisted period and default every other shape to 7d."""

    if len(values) != 1:
        return ServerAnalyticsPeriod.LAST_7_DAYS
    try:
        return ServerAnalyticsPeriod(values[0])
    except ValueError:
        return ServerAnalyticsPeriod.LAST_7_DAYS


def _default_domain_service_factory(
    repository: object,
    report_timezone: ZoneInfo,
    min_session_seconds: int,
) -> MemberAnalyticsDomainService:
    return MemberAnalyticsService(
        repository,  # type: ignore[arg-type]
        report_timezone=report_timezone,
        min_session_seconds=min_session_seconds,
    )


class WebAdminMemberAnalyticsService:
    """Load one B1 member report inside a repeatable-read read-only snapshot."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        min_session_seconds: int,
        analytics_repository_factory: MemberAnalyticsRepositoryFactory = (
            SqlAlchemyMemberAnalyticsRepository
        ),
        domain_service_factory: MemberAnalyticsDomainServiceFactory = (
            _default_domain_service_factory
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
        self._min_session_seconds = min_session_seconds
        self._analytics_repository_factory = analytics_repository_factory
        self._domain_service_factory = domain_service_factory
        self._clock = clock

    async def load(
        self,
        user_id: int,
        period: ServerAnalyticsPeriod,
    ) -> WebAdminMemberAnalytics | None:
        """Return a page model, or a controlled section-level unavailable state."""

        as_of = normalize_observed_at(self._clock())
        try:
            async with self._session_factory() as session:
                await session.execute(text(MEMBER_ANALYTICS_TRANSACTION_SETUP_SQL))
                report = await self._domain_service_factory(
                    self._analytics_repository_factory(session),
                    self._report_timezone,
                    self._min_session_seconds,
                ).get_report(self._guild_id, user_id, period, as_of)
        except Exception as error:
            logger.warning(
                "web_admin_member_analytics_load_failed error_type=%s",
                type(error).__name__,
            )
            return None

        return WebAdminMemberAnalytics(
            report=report,
            period=period,
            report_timezone=self._report_timezone.key,
            voice_coverage=_coverage(report.voice_coverage),
            messages_coverage=_coverage(report.text_coverage),
            active_days_coverage=_combined_coverage(
                report.voice_coverage,
                report.text_coverage,
            ),
        )


def _coverage(coverage: AnalyticsCoverage) -> MemberAnalyticsKpiCoverage:
    return MemberAnalyticsKpiCoverage(
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
) -> MemberAnalyticsKpiCoverage:
    return MemberAnalyticsKpiCoverage(
        _combined_flag(
            voice.current_window_begins_before_earliest_recorded,
            text.current_window_begins_before_earliest_recorded,
        ),
        _combined_flag(
            voice.previous_window_begins_before_earliest_recorded,
            text.previous_window_begins_before_earliest_recorded,
        ),
    )


def _format_count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


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
        return (
            '<span class="delta-state neutral">Нет активности в обоих периодах</span>'
        )
    assert metric.percent_delta is not None
    absolute = (
        format_voice_duration(abs(metric.absolute_delta))
        if duration
        else _format_count(abs(metric.absolute_delta))
    )
    tone = (
        "positive"
        if metric.absolute_delta > 0
        else "negative"
        if metric.absolute_delta < 0
        else "neutral"
    )
    return (
        f'<span class="delta-state {tone}">'
        f'<span class="delta-absolute">{escape(_signed(metric.absolute_delta, absolute))}</span>'
        f'<span class="delta-percent">{metric.percent_delta:+.1f}%</span></span>'
    )


def _current_caveat(coverage: MemberAnalyticsKpiCoverage) -> str:
    if coverage.current_partial is True:
        return (
            '<p class="kpi-caveat warning">Данные за начало выбранного периода '
            "могут быть неполными.</p>"
        )
    return ""


def _previous_caveat(coverage: MemberAnalyticsKpiCoverage) -> str:
    if coverage.previous_partial is True:
        return (
            '<p class="comparison-caveat warning">Данные предыдущего периода '
            "могут быть неполными.</p>"
        )
    if coverage.previous_partial is None:
        return (
            '<p class="comparison-caveat neutral">Полнота данных предыдущего '
            "периода неизвестна.</p>"
        )
    return ""


def _metric_card(
    label: str,
    value: str,
    metric: AnalyticsMetric,
    coverage: MemberAnalyticsKpiCoverage,
    *,
    key: str,
    duration: bool = False,
    secondary: str = "",
) -> str:
    return (
        f'<article class="analytics-kpi" data-member-kpi="{escape(key, quote=True)}">'
        f'<p class="analytics-kpi-label">{escape(label)}</p>'
        f'<strong class="analytics-kpi-value">{escape(value)}</strong>{secondary}'
        f"{_current_caveat(coverage)}"
        '<div class="analytics-comparison"><span>К предыдущему периоду</span>'
        f"{_delta(metric, duration=duration)}{_previous_caveat(coverage)}</div>"
        "</article>"
    )


def _period_selector(
    user_id: int,
    period: ServerAnalyticsPeriod,
    game_period: str,
) -> str:
    seven_current = (
        ' aria-current="page"' if period is ServerAnalyticsPeriod.LAST_7_DAYS else ""
    )
    thirty_current = (
        ' aria-current="page"' if period is ServerAnalyticsPeriod.LAST_30_DAYS else ""
    )
    return (
        '<nav class="analytics-period-selector" aria-label="Период активности">'
        f'<a class="button secondary" href="/admin/members/{user_id}?period=7d&amp;game_period={escape(game_period, quote=True)}"'
        f"{seven_current}>7 дней</a>"
        f'<a class="button secondary" href="/admin/members/{user_id}?period=30d&amp;game_period={escape(game_period, quote=True)}"'
        f"{thirty_current}>30 дней</a>"
        "</nav>"
    )


def _daily_history(report: MemberAnalyticsReport) -> str:
    rows = "".join(
        '<tr class="member-activity-day">'
        f'<td data-label="Дата"><time datetime="{point.local_date.isoformat()}">'
        f"{escape(_local_date(point.local_date))}</time></td>"
        f'<td data-label="Voice">{escape(format_voice_duration(point.voice_exact_seconds + point.voice_estimated_seconds))}</td>'
        f'<td data-label="Сообщения">{_format_count(point.messages)}</td></tr>'
        for point in report.daily
    )
    return (
        '<div class="member-activity-history">'
        '<table class="member-activity-table"><caption class="visually-hidden">'
        "Активность по завершённым локальным дням</caption>"
        '<thead><tr><th scope="col">Дата</th><th scope="col">Voice</th>'
        f'<th scope="col">Сообщения</th></tr></thead><tbody>{rows}</tbody></table></div>'
    )


def render_member_activity_section(
    analytics: WebAdminMemberAnalytics | None,
    *,
    user_id: int,
    selected_period: ServerAnalyticsPeriod,
    game_period: str = "30d",
) -> str:
    """Render an additive profile section without affecting lifetime data."""

    selector = _period_selector(user_id, selected_period, game_period)
    if analytics is None:
        content = (
            '<section class="notice failure member-activity-unavailable">'
            "Аналитика активности временно недоступна.</section>"
        )
    else:
        report = analytics.report
        estimated_note = (
            '<p class="kpi-secondary">Включает оценённое голосовое время.</p>'
            if report.voice_person_time.estimated_seconds.current > 0
            else ""
        )
        cards = "".join(
            (
                _metric_card(
                    "Voice",
                    format_voice_duration(
                        report.voice_person_time.total_seconds.current
                    ),
                    report.voice_person_time.total_seconds,
                    analytics.voice_coverage,
                    key="voice",
                    duration=True,
                    secondary=estimated_note,
                ),
                _metric_card(
                    "Messages",
                    _format_count(report.messages.current),
                    report.messages,
                    analytics.messages_coverage,
                    key="messages",
                ),
                _metric_card(
                    "Active days",
                    f"{report.active_days.current} из {analytics.period.days}",
                    report.active_days,
                    analytics.active_days_coverage,
                    key="active-days",
                ),
            )
        )
        content = (
            f'<div class="member-activity-kpis">{cards}</div>'
            '<div class="section-heading member-activity-history-heading">'
            "<h3>История по дням</h3><p>Текущий завершённый локальный период · "
            f"{escape(analytics.report_timezone)}</p></div>{_daily_history(report)}"
        )
    return (
        '<section class="member-profile-section member-activity-section" '
        'aria-labelledby="member-activity-title">'
        '<div class="member-activity-header"><div class="section-heading">'
        '<h2 id="member-activity-title">Активность</h2>'
        "<p>Завершённые локальные календарные дни</p></div>"
        f"{selector}</div>{content}</section>"
    )
