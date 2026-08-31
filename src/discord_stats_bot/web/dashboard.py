"""Authenticated Web Admin Dashboard v1 read model and presentation."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from html import escape
from typing import Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.bot_profile import (
    BotGuildProfile,
    BotProfileErrorCategory,
    BotProfileOperationError,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.features.voice_statistics import (
    VoiceStatisticsPeriod,
    VoiceStatisticsRepository,
    VoiceStatisticsService,
)
from discord_stats_bot.persistence.models import (
    DiscordUser,
    Guild,
    GuildMember,
    VoiceInterval,
    VoiceSession,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyVoiceStatisticsRepository,
)
from discord_stats_bot.web.authorization import WebAdminRole
from discord_stats_bot.web.bot_control import BotProfileControl
from discord_stats_bot.web.presentation import render_admin_page
from discord_stats_bot.web.service import WebDatabaseHealth

logger = logging.getLogger(__name__)
DASHBOARD_TRANSACTION_ISOLATION_LEVEL = "REPEATABLE READ"


class DashboardDatabaseProbe(Protocol):
    async def probe_database(self) -> WebDatabaseHealth: ...


class DashboardOverviewRepository(Protocol):
    async def load(self, guild_id: int) -> "DashboardOverviewCounts": ...


DashboardOverviewRepositoryFactory = Callable[
    [AsyncSession], DashboardOverviewRepository
]
VoiceRepositoryFactory = Callable[[AsyncSession], VoiceStatisticsRepository]


class DashboardBotStatus(StrEnum):
    ONLINE = "online"
    NOT_READY = "not_ready"
    UNKNOWN = "unknown"


class DashboardControlStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DashboardOverviewCounts:
    guild_name: str | None
    member_count: int
    in_voice_count: int
    active_voice_sessions: int

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.member_count,
                self.in_voice_count,
                self.active_voice_sessions,
            )
        ):
            raise ValueError("dashboard counts must not be negative")


@dataclass(frozen=True, slots=True)
class DashboardServerOverview:
    guild_name: str | None
    member_count: int
    in_voice_count: int
    active_voice_sessions: int
    voice_today_seconds: int
    voice_last_30_days_seconds: int

    def __post_init__(self) -> None:
        if any(
            value < 0
            for value in (
                self.member_count,
                self.in_voice_count,
                self.active_voice_sessions,
                self.voice_today_seconds,
                self.voice_last_30_days_seconds,
            )
        ):
            raise ValueError("dashboard values must not be negative")


@dataclass(frozen=True, slots=True)
class DashboardBotOverview:
    status: DashboardBotStatus
    control_status: DashboardControlStatus
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class WebAdminDashboard:
    database_health: WebDatabaseHealth
    server: DashboardServerOverview | None
    bot: DashboardBotOverview


def dashboard_overview_statement(guild_id: int) -> object:
    """Build one configured-guild snapshot for current membership and Voice."""

    current_members = (
        select(GuildMember.user_id)
        .join(DiscordUser, DiscordUser.id == GuildMember.user_id)
        .where(
            GuildMember.guild_id == guild_id,
            GuildMember.left_at.is_(None),
            DiscordUser.is_bot.is_(False),
        )
        .cte("dashboard_current_members")
    )
    in_voice = (
        select(VoiceInterval.user_id)
        .join(
            current_members,
            current_members.c.user_id == VoiceInterval.user_id,
        )
        .where(
            VoiceInterval.guild_id == guild_id,
            VoiceInterval.ended_at.is_(None),
            VoiceInterval.is_afk.is_(False),
        )
        .cte("dashboard_in_voice")
    )
    open_sessions = (
        select(VoiceSession.id)
        .join(
            current_members,
            current_members.c.user_id == VoiceSession.user_id,
        )
        .where(
            VoiceSession.guild_id == guild_id,
            VoiceSession.ended_at.is_(None),
        )
        .cte("dashboard_open_voice_sessions")
    )
    return select(
        select(Guild.name)
        .where(Guild.id == guild_id)
        .scalar_subquery()
        .label("guild_name"),
        select(func.count())
        .select_from(current_members)
        .scalar_subquery()
        .label("member_count"),
        select(func.count(func.distinct(in_voice.c.user_id)))
        .select_from(in_voice)
        .scalar_subquery()
        .label("in_voice_count"),
        select(func.count())
        .select_from(open_sessions)
        .scalar_subquery()
        .label("active_voice_sessions"),
    )


class SqlAlchemyDashboardOverviewRepository:
    """Execute the bounded current-state Dashboard query."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(self, guild_id: int) -> DashboardOverviewCounts:
        row = (
            await self._session.execute(dashboard_overview_statement(guild_id))
        ).one()
        return DashboardOverviewCounts(
            guild_name=row.guild_name,
            member_count=int(row.member_count),
            in_voice_count=int(row.in_voice_count),
            active_voice_sessions=int(row.active_voice_sessions),
        )


class WebAdminDashboardService:
    """Combine independent PostgreSQL and Bot Control Dashboard sources."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        min_session_seconds: int,
        database_probe: DashboardDatabaseProbe,
        bot_control: BotProfileControl,
        overview_repository_factory: DashboardOverviewRepositoryFactory = (
            SqlAlchemyDashboardOverviewRepository
        ),
        voice_repository_factory: VoiceRepositoryFactory = (
            SqlAlchemyVoiceStatisticsRepository
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        if min_session_seconds <= 0:
            raise ValueError("min_session_seconds must be positive")
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
        self._min_session_seconds = min_session_seconds
        self._database_probe = database_probe
        self._bot_control = bot_control
        self._overview_repository_factory = overview_repository_factory
        self._voice_repository_factory = voice_repository_factory
        self._clock = clock

    async def load(self) -> WebAdminDashboard:
        """Load each source independently so Bot Control cannot hide DB data."""

        health = await self._database_probe.probe_database()
        server = await self._load_server() if health.available else None
        bot = await self._load_bot()
        return WebAdminDashboard(database_health=health, server=server, bot=bot)

    async def _load_server(self) -> DashboardServerOverview | None:
        as_of = normalize_observed_at(self._clock())
        try:
            async with self._session_factory() as session:
                await session.connection(
                    execution_options={
                        "isolation_level": DASHBOARD_TRANSACTION_ISOLATION_LEVEL
                    }
                )
                counts = await self._overview_repository_factory(session).load(
                    self._guild_id
                )
                voice_service = VoiceStatisticsService(
                    self._voice_repository_factory(session),
                    report_timezone=self._report_timezone,
                    min_session_seconds=self._min_session_seconds,
                )
                today = await voice_service.get_server_report(
                    self._guild_id,
                    VoiceStatisticsPeriod.TODAY,
                    as_of,
                )
                last_30_days = await voice_service.get_server_report(
                    self._guild_id,
                    VoiceStatisticsPeriod.LAST_30_DAYS,
                    as_of,
                )
        except Exception as error:
            logger.warning(
                "Web admin dashboard PostgreSQL lookup failed error_type=%s",
                type(error).__name__,
            )
            return None
        return DashboardServerOverview(
            guild_name=counts.guild_name,
            member_count=counts.member_count,
            in_voice_count=counts.in_voice_count,
            active_voice_sessions=counts.active_voice_sessions,
            voice_today_seconds=today.total_seconds,
            voice_last_30_days_seconds=last_30_days.total_seconds,
        )

    async def _load_bot(self) -> DashboardBotOverview:
        try:
            profile: BotGuildProfile = await self._bot_control.get_profile()
        except BotProfileOperationError as error:
            if error.category in {
                BotProfileErrorCategory.BOT_NOT_READY,
                BotProfileErrorCategory.GUILD_UNAVAILABLE,
            }:
                return DashboardBotOverview(
                    DashboardBotStatus.NOT_READY,
                    DashboardControlStatus.AVAILABLE,
                )
            return DashboardBotOverview(
                DashboardBotStatus.UNKNOWN,
                DashboardControlStatus.UNAVAILABLE,
            )
        except Exception as error:
            logger.warning(
                "Web admin dashboard Bot Control probe failed error_type=%s",
                type(error).__name__,
            )
            return DashboardBotOverview(
                DashboardBotStatus.UNKNOWN,
                DashboardControlStatus.UNAVAILABLE,
            )
        return DashboardBotOverview(
            DashboardBotStatus.ONLINE,
            DashboardControlStatus.AVAILABLE,
            display_name=profile.display_name,
        )


def format_dashboard_voice_duration(seconds: int) -> str:
    """Format server person-time as accumulated hours and minutes."""

    if seconds < 0:
        raise ValueError("seconds must not be negative")
    total_minutes = seconds // 60
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"{minutes} мин"
    return f"{hours} ч {minutes:02d} мин"


def _status_text(bot: DashboardBotOverview) -> tuple[str, str]:
    bot_text = {
        DashboardBotStatus.ONLINE: "Онлайн",
        DashboardBotStatus.NOT_READY: "Не готов",
        DashboardBotStatus.UNKNOWN: "Неизвестно",
    }[bot.status]
    control_text = {
        DashboardControlStatus.AVAILABLE: "Доступен",
        DashboardControlStatus.UNAVAILABLE: "Недоступен",
    }[bot.control_status]
    return bot_text, control_text


def _dashboard_bot_tone(status: DashboardBotStatus) -> str:
    return {
        DashboardBotStatus.ONLINE: "success",
        DashboardBotStatus.NOT_READY: "warning",
        DashboardBotStatus.UNKNOWN: "neutral",
    }[status]


def render_dashboard_page(
    dashboard: WebAdminDashboard,
    *,
    csrf_token: str,
    role: WebAdminRole,
) -> str:
    """Render a responsive, escaped Dashboard without synthetic health claims."""

    if dashboard.server is None:
        server_name = "Настроенный сервер"
        server_content = '<p class="notice failure">Сводка сервера временно недоступна. Показатели активности не подменяются оценочными значениями.</p>'
    else:
        overview = dashboard.server
        server_name = overview.guild_name or "Настроенный сервер"
        server_content = f"""<div class="metric-grid dashboard-kpis">
<article class="metric"><strong>{overview.member_count}</strong><span>Участников</span></article>
<article class="metric"><strong>{overview.in_voice_count}</strong><span>Сейчас в Voice</span></article>
<article class="metric"><strong>{overview.active_voice_sessions}</strong><span>Активных Voice-сессий</span></article>
<article class="metric usage-metric"><strong>{escape(format_dashboard_voice_duration(overview.voice_today_seconds))}</strong><span>Voice сегодня</span></article>
<article class="metric usage-metric"><strong>{escape(format_dashboard_voice_duration(overview.voice_last_30_days_seconds))}</strong><span>Voice за 30 дней</span></article>
</div><p class="muted">Voice отражает последнее подтверждённое состояние Kanami; окно «сегодня» рассчитано в REPORT_TIMEZONE.</p>"""

    database_text = "Работает" if dashboard.database_health.available else "Недоступна"
    database_class = "success" if dashboard.database_health.available else "danger"
    bot_text, control_text = _status_text(dashboard.bot)
    bot_class = _dashboard_bot_tone(dashboard.bot.status)
    control_class = (
        "success"
        if dashboard.bot.control_status is DashboardControlStatus.AVAILABLE
        else "danger"
    )
    bot_detail = (
        f'<span class="muted">{escape(dashboard.bot.display_name)}</span>'
        if dashboard.bot.display_name
        else ""
    )
    owner_links = (
        '<a class="quick-card" href="/admin/administrators"><strong>Администраторы</strong><span>OWNER и managed ADMIN</span></a>'
        '<a class="quick-card" href="/admin/audit"><strong>Журнал аудита</strong><span>История управления Web Admin</span></a>'
        if role is WebAdminRole.OWNER
        else ""
    )
    body = f"""<section class="dashboard-hero" aria-labelledby="server-summary-title"><div>
<p class="section-kicker">Discord-сервер</p><h2 id="server-summary-title">{escape(server_name)}</h2>
<span class="muted">Оперативная сводка Kanami</span></div><div class="hero-statuses">
<div class="health-signal {bot_class}"><span>Kanami</span><strong>{escape(bot_text)}</strong>{bot_detail}</div>
<div class="health-signal {database_class}"><span>PostgreSQL</span><strong>{escape(database_text)}</strong></div>
<div class="health-signal {control_class}"><span>Bot Control</span><strong>{escape(control_text)}</strong></div>
</div></section>
<section class="dashboard-section" aria-labelledby="dashboard-kpi-title"><header><h2 id="dashboard-kpi-title">Активность сервера</h2><p>Ключевые показатели</p></header>{server_content}</section>
<section class="dashboard-section" aria-labelledby="quick-actions-title"><header><h2 id="quick-actions-title">Быстрый доступ</h2><p>Управление и настройки</p></header><div class="quick-grid secondary-actions">
<a class="quick-card" href="/admin/settings/bot-profile"><strong>Бот</strong><span>Профиль и управление</span></a>
<a class="quick-card" href="/admin/server-settings"><strong>Настройки сервера</strong><span>Runtime-конфигурация</span></a>
<a class="quick-card" href="/admin/rules"><strong>Правила</strong><span>Версии и публикация</span></a>
{owner_links}</div></section>"""
    return render_admin_page(
        "Dashboard",
        body,
        role=role,
        csrf_token=csrf_token,
        active_path="/admin/",
        description="Состояние и активность настроенного Discord-сервера",
    )
