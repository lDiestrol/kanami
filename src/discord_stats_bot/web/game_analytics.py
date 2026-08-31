"""Web Admin integration for member-scoped game analytics."""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import Protocol
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.common.formatting import format_voice_duration
from discord_stats_bot.features.game_tracking import (
    GameStatistics,
    GameStatisticsPeriod,
    GameStatisticsService,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.persistence.repositories import SqlAlchemyGameTrackingRepository

logger = logging.getLogger(__name__)
GAME_ANALYTICS_TRANSACTION_SETUP_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)


class GameAnalyticsDomainService(Protocol):
    async def get_user_statistics(
        self,
        guild_id: int,
        user_id: int,
        period: GameStatisticsPeriod,
        as_of: datetime,
    ) -> GameStatistics: ...


GameAnalyticsRepositoryFactory = Callable[[AsyncSession], object]
GameAnalyticsDomainServiceFactory = Callable[
    [object, ZoneInfo], GameAnalyticsDomainService
]


@dataclass(frozen=True, slots=True)
class WebAdminGameAnalytics:
    """Presentation-safe member game analytics model."""

    statistics: GameStatistics
    report_timezone: ZoneInfo


def parse_game_analytics_period(values: Sequence[str]) -> GameStatisticsPeriod:
    """Accept one web-supported period and default every other shape to 30d."""

    if len(values) != 1:
        return GameStatisticsPeriod.LAST_30_DAYS
    try:
        period = GameStatisticsPeriod(values[0])
    except ValueError:
        return GameStatisticsPeriod.LAST_30_DAYS
    if period is GameStatisticsPeriod.ALL_TIME:
        return GameStatisticsPeriod.LAST_30_DAYS
    return period


def _default_domain_service_factory(
    repository: object,
    report_timezone: ZoneInfo,
) -> GameAnalyticsDomainService:
    return GameStatisticsService(
        repository,  # type: ignore[arg-type]
        report_timezone=report_timezone,
    )


class WebAdminGameAnalyticsService:
    """Load one member's game report in an isolated read-only snapshot."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        repository_factory: GameAnalyticsRepositoryFactory = (
            SqlAlchemyGameTrackingRepository
        ),
        domain_service_factory: GameAnalyticsDomainServiceFactory = (
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
        user_id: int,
        period: GameStatisticsPeriod,
    ) -> WebAdminGameAnalytics | None:
        """Return game analytics or a controlled section-level failure state."""

        as_of = normalize_observed_at(self._clock())
        try:
            async with self._session_factory() as session:
                await session.execute(text(GAME_ANALYTICS_TRANSACTION_SETUP_SQL))
                statistics = await self._domain_service_factory(
                    self._repository_factory(session),
                    self._report_timezone,
                ).get_user_statistics(self._guild_id, user_id, period, as_of)
        except Exception as error:
            logger.warning(
                "web_admin_game_analytics_load_failed error_type=%s",
                type(error).__name__,
            )
            return None
        return WebAdminGameAnalytics(statistics, self._report_timezone)


def _period_selector(
    user_id: int,
    game_period: GameStatisticsPeriod,
    activity_period: str,
) -> str:
    links = []
    for period, label in (
        (GameStatisticsPeriod.LAST_7_DAYS, "7 дней"),
        (GameStatisticsPeriod.LAST_30_DAYS, "30 дней"),
        (GameStatisticsPeriod.LAST_90_DAYS, "90 дней"),
    ):
        current = ' aria-current="page"' if period is game_period else ""
        query = urlencode({"period": activity_period, "game_period": period.value})
        links.append(
            f'<a class="button secondary" href="/admin/members/{user_id}?{escape(query, quote=True)}"'
            f"{current}>{label}</a>"
        )
    return (
        '<nav class="analytics-period-selector" aria-label="Период игровой активности">'
        + "".join(links)
        + "</nav>"
    )


def _local_datetime(value: datetime, timezone: ZoneInfo) -> str:
    return value.astimezone(timezone).strftime("%d.%m.%Y %H:%M")


def render_member_games_section(
    analytics: WebAdminGameAnalytics | None,
    *,
    user_id: int,
    selected_period: GameStatisticsPeriod,
    activity_period: str,
) -> str:
    """Render game analytics without accessing persistence or live Presence."""

    selector = _period_selector(user_id, selected_period, activity_period)
    if analytics is None:
        content = (
            '<section class="notice failure member-games-unavailable">'
            "Игровая аналитика временно недоступна.</section>"
        )
    elif not analytics.statistics.has_data:
        content = (
            '<section class="notice member-games-empty">'
            "Игровых данных за выбранный период пока нет.</section>"
        )
    else:
        statistics = analytics.statistics
        kpis = "".join(
            (
                '<article class="analytics-kpi" data-game-kpi="time">'
                '<p class="analytics-kpi-label">Игровое время</p>'
                f'<strong class="analytics-kpi-value">{escape(format_voice_duration(statistics.total_seconds))}</strong></article>',
                '<article class="analytics-kpi" data-game-kpi="games">'
                '<p class="analytics-kpi-label">Разных игр</p>'
                f'<strong class="analytics-kpi-value">{statistics.unique_games}</strong></article>',
                '<article class="analytics-kpi" data-game-kpi="days">'
                '<p class="analytics-kpi-label">Игровых дней</p>'
                f'<strong class="analytics-kpi-value">{statistics.gaming_days}</strong></article>',
            )
        )
        top_games = "".join(
            "<li><span>"
            f"{escape(item.game_name)}</span><strong>{escape(format_voice_duration(item.total_seconds))}</strong></li>"
            for item in statistics.top_games
        )
        latest = statistics.latest_game
        longest = statistics.longest_session
        details = (
            '<div class="member-games-details">'
            '<article><h3>Топ игр</h3><ol class="member-games-top">'
            f"{top_games}</ol></article>"
            "<article><h3>Последняя игра</h3>"
            f"<strong>{escape(latest.game_name)}</strong>"
            f'<p><time datetime="{latest.tracked_at.isoformat()}">{escape(_local_datetime(latest.tracked_at, analytics.report_timezone))}</time> · {escape(analytics.report_timezone.key)}</p></article>'
            "<article><h3>Самая длинная сессия</h3>"
            f"<strong>{escape(longest.game_name)}</strong>"
            f"<p>{escape(format_voice_duration(longest.total_seconds))}</p></article>"
            "</div>"
        )
        content = f'<div class="member-games-kpis">{kpis}</div>{details}'
    return (
        '<section class="member-profile-section member-games-section" '
        'aria-labelledby="member-games-title">'
        '<div class="member-games-header"><div class="section-heading">'
        '<h2 id="member-games-title">Игры</h2>'
        "<p>Подтверждённая игровая активность</p></div>"
        f"{selector}</div>{content}</section>"
    )
