"""Read-only PostgreSQL queries for the web-admin foundation."""

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import ceil
from zoneinfo import ZoneInfo

from sqlalchemy import BigInteger, Text, and_, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.achievements import DEFAULT_ACHIEVEMENT_CATALOG
from discord_stats_bot.features.voice_statistics.service import (
    build_voice_statistics_query,
)
from discord_stats_bot.persistence.models import (
    AuditEvent,
    DailyTextActivity,
    DiscordUser,
    Guild,
    GuildMember,
    UserAchievement,
    WebAdminAccessGrant,
)
from discord_stats_bot.persistence.repositories.voice_statistics import (
    voice_member_all_time_totals_statement,
)
from discord_stats_bot.web.authorization import WebAdminRole

logger = logging.getLogger(__name__)
MEMBERS_PAGE_SIZE = 50
MEMBER_LIFECYCLE_LIMIT = 20
MAX_POSTGRESQL_BIGINT = 9_223_372_036_854_775_807
MEMBER_LIFECYCLE_EVENT_TYPES = (
    "member.joined",
    "member.left",
    "member.returned",
)


class AdminMemberSort(StrEnum):
    """Allowlisted global ordering keys for the member directory."""

    NAME = "name"
    JOINED = "joined"
    VOICE = "voice"
    MESSAGES = "messages"
    ACHIEVEMENTS = "achievements"


class AdminMemberOrder(StrEnum):
    """Allowlisted direction for the member directory primary ordering."""

    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class WebDatabaseHealth:
    """Result of one real PostgreSQL round trip."""

    available: bool
    latency_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class WebAdminAdministrator:
    """One OWNER or active managed ADMIN for safe page presentation."""

    user_id: int
    role: WebAdminRole
    display_name: str
    granted_at: datetime | None = None
    granted_by_user_id: int | None = None


@dataclass(frozen=True, slots=True)
class WebAdminAdministrators:
    """Current administrator groups shown by the OWNER-only page."""

    owners: tuple[WebAdminAdministrator, ...]
    admins: tuple[WebAdminAdministrator, ...]


@dataclass(frozen=True, slots=True)
class AdminCounts:
    """Small schema-level counters safe for the foundation dashboard."""

    guilds: int
    tracked_users: int
    audit_events: int


@dataclass(frozen=True, slots=True)
class AdminMember:
    """One current persisted guild membership and its lifetime aggregates."""

    guild_id: int
    user_id: int
    display_name: str
    joined_at: datetime | None
    voice_seconds: int
    message_count: int
    achievement_count: int
    username: str | None = None
    avatar_hash: str | None = None
    guild_avatar_hash: str | None = None


@dataclass(frozen=True, slots=True)
class AdminMembersPage:
    """One deterministic page of current persisted members."""

    entries: tuple[AdminMember, ...]
    total: int
    page: int
    page_size: int
    query: str
    sort: AdminMemberSort = AdminMemberSort.NAME
    order: AdminMemberOrder = AdminMemberOrder.ASC

    @property
    def total_pages(self) -> int:
        return max(1, ceil(self.total / self.page_size))


@dataclass(frozen=True, slots=True)
class AdminMemberAchievement:
    """One persisted unlock enriched from the immutable code catalog."""

    key: str
    title: str | None
    tier: str | None
    unlocked_at: datetime


@dataclass(frozen=True, slots=True)
class AdminMemberLifecycleEvent:
    """One allowlisted member lifecycle event for safe HTML presentation."""

    event_type: str
    occurred_at: datetime
    absence_seconds: int | None = None
    return_number: int | None = None


@dataclass(frozen=True, slots=True)
class AdminMemberDetail:
    """One configured-guild membership with lifetime read-only data."""

    guild_id: int
    user_id: int
    display_name: str
    username: str | None
    global_name: str | None
    nickname: str | None
    joined_at: datetime | None
    left_at: datetime | None
    voice_seconds: int
    message_count: int
    achievements: tuple[AdminMemberAchievement, ...]
    lifecycle_events: tuple[AdminMemberLifecycleEvent, ...]
    avatar_hash: str | None = None
    guild_avatar_hash: str | None = None

    @property
    def achievement_count(self) -> int:
        return len(self.achievements)


class AdminMemberDetailStatus(StrEnum):
    """Explicitly distinguish 404 from a safely hidden database failure."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class AdminMemberDetailResult:
    """Outcome of a bounded detail lookup."""

    status: AdminMemberDetailStatus
    detail: AdminMemberDetail | None = None


def _safe_nonnegative_int(details: object, key: str) -> int | None:
    if not isinstance(details, Mapping):
        return None
    value = details.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


class WebAdminMembershipRepository:
    """Bounded SELECT-only lookup for the configured guild access policy."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
    ) -> None:
        if guild_id <= 0 or guild_id > MAX_POSTGRESQL_BIGINT:
            raise ValueError("guild_id must fit a positive PostgreSQL BIGINT")
        self._session_factory = session_factory
        self._guild_id = guild_id

    async def is_current_non_bot_member(self, discord_user_id: int) -> bool:
        if discord_user_id <= 0 or discord_user_id > MAX_POSTGRESQL_BIGINT:
            return False
        statement = (
            select(GuildMember.user_id)
            .join(DiscordUser, DiscordUser.id == GuildMember.user_id)
            .where(
                GuildMember.guild_id == self._guild_id,
                GuildMember.user_id == discord_user_id,
                GuildMember.left_at.is_(None),
                DiscordUser.is_bot.is_(False),
            )
            .limit(1)
        )
        try:
            async with self._session_factory() as session:
                return (await session.scalar(statement)) is not None
        except Exception as error:
            logger.warning(
                "Web admin membership lookup failed error_type=%s",
                type(error).__name__,
            )
            return False


class WebAdminManagedAccessRepository:
    """Bounded SELECT-only managed access lookup for the configured guild."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
    ) -> None:
        if guild_id <= 0 or guild_id > MAX_POSTGRESQL_BIGINT:
            raise ValueError("guild_id must fit a positive PostgreSQL BIGINT")
        self._session_factory = session_factory
        self._guild_id = guild_id

    async def is_active_admin(self, discord_user_id: int) -> bool:
        if discord_user_id <= 0 or discord_user_id > MAX_POSTGRESQL_BIGINT:
            return False
        statement = (
            select(WebAdminAccessGrant.id)
            .where(
                WebAdminAccessGrant.guild_id == self._guild_id,
                WebAdminAccessGrant.user_id == discord_user_id,
                WebAdminAccessGrant.revoked_at.is_(None),
            )
            .limit(1)
        )
        try:
            async with self._session_factory() as session:
                return (await session.scalar(statement)) is not None
        except Exception as error:
            logger.warning(
                "Web admin managed access lookup failed error_type=%s",
                type(error).__name__,
            )
            return False

    async def list_administrators(
        self,
        owner_user_ids: frozenset[int],
    ) -> WebAdminAdministrators | None:
        """Return configured OWNERs and active non-OWNER grants using SELECT only."""

        identity_columns = (
            DiscordUser.username,
            DiscordUser.global_name,
            GuildMember.nickname,
        )
        owner_statement = (
            select(DiscordUser.id, *identity_columns)
            .outerjoin(
                GuildMember,
                and_(
                    GuildMember.guild_id == self._guild_id,
                    GuildMember.user_id == DiscordUser.id,
                ),
            )
            .where(DiscordUser.id.in_(owner_user_ids))
        )
        admin_statement = (
            select(
                WebAdminAccessGrant.user_id,
                WebAdminAccessGrant.granted_at,
                WebAdminAccessGrant.granted_by_user_id,
                *identity_columns,
            )
            .outerjoin(
                DiscordUser,
                DiscordUser.id == WebAdminAccessGrant.user_id,
            )
            .outerjoin(
                GuildMember,
                and_(
                    GuildMember.guild_id == self._guild_id,
                    GuildMember.user_id == WebAdminAccessGrant.user_id,
                ),
            )
            .where(
                WebAdminAccessGrant.guild_id == self._guild_id,
                WebAdminAccessGrant.revoked_at.is_(None),
            )
            .order_by(
                WebAdminAccessGrant.granted_at.asc(),
                WebAdminAccessGrant.id.asc(),
            )
        )
        if owner_user_ids:
            admin_statement = admin_statement.where(
                WebAdminAccessGrant.user_id.not_in(owner_user_ids)
            )
        try:
            async with self._session_factory() as session:
                owner_rows = (await session.execute(owner_statement)).mappings().all()
                admin_rows = (await session.execute(admin_statement)).mappings().all()
        except Exception as error:
            logger.warning(
                "Web admin administrator list failed error_type=%s",
                type(error).__name__,
            )
            return None

        owner_identity = {int(row["id"]): row for row in owner_rows}
        owners = tuple(
            WebAdminAdministrator(
                user_id=user_id,
                role=WebAdminRole.OWNER,
                display_name=self._display_name(owner_identity.get(user_id), user_id),
            )
            for user_id in sorted(owner_user_ids)
        )
        admins = tuple(
            WebAdminAdministrator(
                user_id=int(row["user_id"]),
                role=WebAdminRole.ADMIN,
                display_name=self._display_name(row, int(row["user_id"])),
                granted_at=row["granted_at"],
                granted_by_user_id=int(row["granted_by_user_id"]),
            )
            for row in admin_rows
        )
        return WebAdminAdministrators(owners=owners, admins=admins)

    @staticmethod
    def _display_name(row: Mapping[str, object] | None, user_id: int) -> str:
        if row is not None:
            for field in ("nickname", "global_name", "username"):
                value = row.get(field)
                if isinstance(value, str) and value:
                    return value
        return str(user_id)


class WebAdminService:
    """Run bounded SELECT-only probes using short-lived async sessions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        min_session_seconds: int = 10,
    ) -> None:
        if guild_id <= 0 or guild_id > MAX_POSTGRESQL_BIGINT:
            raise ValueError("guild_id must fit a positive PostgreSQL BIGINT")
        if min_session_seconds <= 0:
            raise ValueError("min_session_seconds must be positive")
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._monotonic = monotonic
        self._clock = clock
        self._min_session_seconds = min_session_seconds

    async def probe_database(self) -> WebDatabaseHealth:
        started_at = self._monotonic()
        try:
            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as error:
            logger.warning(
                "Web admin PostgreSQL health probe failed error_type=%s",
                type(error).__name__,
            )
            return WebDatabaseHealth(available=False)
        return WebDatabaseHealth(
            available=True,
            latency_seconds=max(0.0, self._monotonic() - started_at),
        )

    async def load_counts(self) -> AdminCounts | None:
        statement = select(
            select(func.count()).select_from(Guild).scalar_subquery().label("guilds"),
            select(func.count())
            .select_from(DiscordUser)
            .scalar_subquery()
            .label("tracked_users"),
            select(func.count())
            .select_from(AuditEvent)
            .scalar_subquery()
            .label("audit_events"),
        )
        try:
            async with self._session_factory() as session:
                row = (await session.execute(statement)).one()
        except Exception as error:
            logger.warning(
                "Web admin counter query failed error_type=%s",
                type(error).__name__,
            )
            return None
        return AdminCounts(
            guilds=int(row.guilds),
            tracked_users=int(row.tracked_users),
            audit_events=int(row.audit_events),
        )

    async def load_members(
        self,
        *,
        page: int,
        query: str,
        sort: AdminMemberSort = AdminMemberSort.NAME,
        order: AdminMemberOrder = AdminMemberOrder.ASC,
        page_size: int = MEMBERS_PAGE_SIZE,
    ) -> AdminMembersPage | None:
        """Load current memberships with two set-based SELECT statements."""

        if page <= 0:
            raise ValueError("page must be positive")
        if page_size <= 0:
            raise ValueError("page_size must be positive")
        if not isinstance(sort, AdminMemberSort):
            raise ValueError("sort must be an AdminMemberSort")
        if not isinstance(order, AdminMemberOrder):
            raise ValueError("order must be an AdminMemberOrder")
        normalized_query = query.strip()
        display_name = func.coalesce(
            func.nullif(GuildMember.nickname, ""),
            func.nullif(DiscordUser.global_name, ""),
            func.nullif(DiscordUser.username, ""),
            cast(DiscordUser.id, Text),
        ).label("display_name")

        members = (
            select(
                GuildMember.guild_id.label("guild_id"),
                GuildMember.user_id.label("user_id"),
                display_name,
                DiscordUser.username.label("username"),
                DiscordUser.avatar_hash.label("avatar_hash"),
                GuildMember.guild_avatar_hash.label("guild_avatar_hash"),
                GuildMember.joined_at.label("joined_at"),
            )
            .join(DiscordUser, DiscordUser.id == GuildMember.user_id)
            .where(
                GuildMember.guild_id == self._guild_id,
                GuildMember.left_at.is_(None),
                DiscordUser.is_bot.is_(False),
            )
        )
        if normalized_query:
            if normalized_query.isdecimal():
                members = members.where(DiscordUser.id == int(normalized_query))
            else:
                escaped_query = (
                    normalized_query.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                pattern = f"%{escaped_query}%"
                members = members.where(
                    or_(
                        GuildMember.nickname.ilike(pattern, escape="\\"),
                        DiscordUser.global_name.ilike(pattern, escape="\\"),
                        DiscordUser.username.ilike(pattern, escape="\\"),
                    )
                )

        filtered_members = members.cte("filtered_admin_members")
        count_statement = select(func.count()).select_from(filtered_members)

        try:
            async with self._session_factory() as session:
                total = int(await session.scalar(count_statement) or 0)
                total_pages = max(1, ceil(total / page_size))
                current_page = min(page, total_pages)
                if total == 0:
                    return AdminMembersPage(
                        entries=(),
                        total=0,
                        page=1,
                        page_size=page_size,
                        query=normalized_query,
                        sort=sort,
                        order=order,
                    )

                statistics_query = build_voice_statistics_query(
                    self._clock(),
                    report_timezone=ZoneInfo("UTC"),
                    min_session_seconds=self._min_session_seconds,
                )
                voice_totals = voice_member_all_time_totals_statement(
                    statistics_query,
                    member_scope=filtered_members,
                ).subquery("admin_member_voice_totals")
                text_totals = (
                    select(
                        DailyTextActivity.guild_id.label("guild_id"),
                        DailyTextActivity.user_id.label("user_id"),
                        func.sum(DailyTextActivity.message_count).label(
                            "message_count"
                        ),
                    )
                    .join(
                        filtered_members,
                        and_(
                            filtered_members.c.guild_id == DailyTextActivity.guild_id,
                            filtered_members.c.user_id == DailyTextActivity.user_id,
                        ),
                    )
                    .group_by(
                        DailyTextActivity.guild_id,
                        DailyTextActivity.user_id,
                    )
                    .subquery("admin_member_text_totals")
                )
                achievement_totals = (
                    select(
                        UserAchievement.guild_id.label("guild_id"),
                        UserAchievement.user_id.label("user_id"),
                        func.count().label("achievement_count"),
                    )
                    .join(
                        filtered_members,
                        and_(
                            filtered_members.c.guild_id == UserAchievement.guild_id,
                            filtered_members.c.user_id == UserAchievement.user_id,
                        ),
                    )
                    .group_by(
                        UserAchievement.guild_id,
                        UserAchievement.user_id,
                    )
                    .subquery("admin_member_achievement_totals")
                )
                enriched_members = (
                    select(
                        filtered_members.c.guild_id,
                        filtered_members.c.user_id,
                        filtered_members.c.display_name,
                        filtered_members.c.username,
                        filtered_members.c.avatar_hash,
                        filtered_members.c.guild_avatar_hash,
                        filtered_members.c.joined_at,
                        cast(
                            func.coalesce(voice_totals.c.exact_seconds, 0)
                            + func.coalesce(voice_totals.c.estimated_seconds, 0),
                            BigInteger,
                        ).label("voice_seconds"),
                        cast(
                            func.coalesce(text_totals.c.message_count, 0),
                            BigInteger,
                        ).label("message_count"),
                        cast(
                            func.coalesce(achievement_totals.c.achievement_count, 0),
                            BigInteger,
                        ).label("achievement_count"),
                    )
                    .select_from(
                        filtered_members.outerjoin(
                            voice_totals,
                            and_(
                                voice_totals.c.guild_id == filtered_members.c.guild_id,
                                voice_totals.c.user_id == filtered_members.c.user_id,
                            ),
                        )
                        .outerjoin(
                            text_totals,
                            and_(
                                text_totals.c.guild_id == filtered_members.c.guild_id,
                                text_totals.c.user_id == filtered_members.c.user_id,
                            ),
                        )
                        .outerjoin(
                            achievement_totals,
                            and_(
                                achievement_totals.c.guild_id
                                == filtered_members.c.guild_id,
                                achievement_totals.c.user_id
                                == filtered_members.c.user_id,
                            ),
                        )
                    )
                    .cte("enriched_admin_members")
                )
                sort_columns = {
                    AdminMemberSort.NAME: func.lower(enriched_members.c.display_name),
                    AdminMemberSort.JOINED: enriched_members.c.joined_at,
                    AdminMemberSort.VOICE: enriched_members.c.voice_seconds,
                    AdminMemberSort.MESSAGES: enriched_members.c.message_count,
                    AdminMemberSort.ACHIEVEMENTS: (
                        enriched_members.c.achievement_count
                    ),
                }
                primary_order = (
                    sort_columns[sort].asc()
                    if order is AdminMemberOrder.ASC
                    else sort_columns[sort].desc()
                )
                if sort is AdminMemberSort.JOINED:
                    primary_order = primary_order.nulls_last()
                ordering = (primary_order, enriched_members.c.user_id.asc())
                rows_statement = (
                    select(enriched_members)
                    .order_by(*ordering)
                    .limit(page_size)
                    .offset((current_page - 1) * page_size)
                )
                rows = (await session.execute(rows_statement)).all()
        except Exception as error:
            logger.warning(
                "Web admin member query failed error_type=%s",
                type(error).__name__,
            )
            return None

        return AdminMembersPage(
            entries=tuple(
                AdminMember(
                    guild_id=int(row.guild_id),
                    user_id=int(row.user_id),
                    display_name=str(row.display_name),
                    joined_at=row.joined_at,
                    voice_seconds=int(row.voice_seconds),
                    message_count=int(row.message_count),
                    achievement_count=int(row.achievement_count),
                    username=row.username,
                    avatar_hash=row.avatar_hash,
                    guild_avatar_hash=row.guild_avatar_hash,
                )
                for row in rows
            ),
            total=total,
            page=current_page,
            page_size=page_size,
            query=normalized_query,
            sort=sort,
            order=order,
        )

    async def load_member_detail(self, user_id: int) -> AdminMemberDetailResult:
        """Load one member profile and bounded lifecycle history in two SELECTs."""

        if user_id <= 0 or user_id > MAX_POSTGRESQL_BIGINT:
            return AdminMemberDetailResult(AdminMemberDetailStatus.NOT_FOUND)

        display_name = func.coalesce(
            func.nullif(GuildMember.nickname, ""),
            func.nullif(DiscordUser.global_name, ""),
            func.nullif(DiscordUser.username, ""),
            cast(DiscordUser.id, Text),
        ).label("display_name")
        member_scope = (
            select(
                GuildMember.guild_id.label("guild_id"),
                GuildMember.user_id.label("user_id"),
                display_name,
                DiscordUser.username.label("username"),
                DiscordUser.global_name.label("global_name"),
                DiscordUser.avatar_hash.label("avatar_hash"),
                GuildMember.nickname.label("nickname"),
                GuildMember.guild_avatar_hash.label("guild_avatar_hash"),
                GuildMember.joined_at.label("joined_at"),
                GuildMember.left_at.label("left_at"),
            )
            .join(DiscordUser, DiscordUser.id == GuildMember.user_id)
            .where(
                GuildMember.guild_id == self._guild_id,
                GuildMember.user_id == user_id,
                DiscordUser.is_bot.is_(False),
            )
            .cte("admin_member_scope")
        )
        statistics_query = build_voice_statistics_query(
            self._clock(),
            report_timezone=ZoneInfo("UTC"),
            min_session_seconds=self._min_session_seconds,
        )
        voice_totals = voice_member_all_time_totals_statement(
            statistics_query,
            member_scope=member_scope,
        ).subquery("admin_member_detail_voice_totals")
        text_totals = (
            select(
                DailyTextActivity.guild_id.label("guild_id"),
                DailyTextActivity.user_id.label("user_id"),
                func.sum(DailyTextActivity.message_count).label("message_count"),
            )
            .join(
                member_scope,
                and_(
                    member_scope.c.guild_id == DailyTextActivity.guild_id,
                    member_scope.c.user_id == DailyTextActivity.user_id,
                ),
            )
            .group_by(
                DailyTextActivity.guild_id,
                DailyTextActivity.user_id,
            )
            .subquery("admin_member_detail_text_totals")
        )
        profile_totals = (
            select(
                member_scope,
                cast(
                    func.coalesce(voice_totals.c.exact_seconds, 0)
                    + func.coalesce(voice_totals.c.estimated_seconds, 0),
                    BigInteger,
                ).label("voice_seconds"),
                cast(
                    func.coalesce(text_totals.c.message_count, 0),
                    BigInteger,
                ).label("message_count"),
            )
            .select_from(
                member_scope.outerjoin(
                    voice_totals,
                    and_(
                        voice_totals.c.guild_id == member_scope.c.guild_id,
                        voice_totals.c.user_id == member_scope.c.user_id,
                    ),
                ).outerjoin(
                    text_totals,
                    and_(
                        text_totals.c.guild_id == member_scope.c.guild_id,
                        text_totals.c.user_id == member_scope.c.user_id,
                    ),
                )
            )
            .cte("admin_member_profile_totals")
        )
        profile_statement = (
            select(
                profile_totals,
                UserAchievement.achievement_key,
                UserAchievement.unlocked_at,
            )
            .select_from(
                profile_totals.outerjoin(
                    UserAchievement,
                    and_(
                        UserAchievement.guild_id == profile_totals.c.guild_id,
                        UserAchievement.user_id == profile_totals.c.user_id,
                    ),
                )
            )
            .order_by(
                UserAchievement.unlocked_at.asc().nulls_last(),
                UserAchievement.achievement_key.asc().nulls_last(),
            )
        )
        lifecycle_statement = (
            select(
                AuditEvent.id,
                AuditEvent.event_type,
                AuditEvent.occurred_at,
                AuditEvent.details_data,
            )
            .where(
                AuditEvent.guild_id == self._guild_id,
                AuditEvent.subject_id == user_id,
                AuditEvent.subject_type == "user",
                AuditEvent.event_type.in_(MEMBER_LIFECYCLE_EVENT_TYPES),
            )
            .order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
            .limit(MEMBER_LIFECYCLE_LIMIT)
        )

        try:
            async with self._session_factory() as session:
                profile_rows = (await session.execute(profile_statement)).all()
                if not profile_rows:
                    return AdminMemberDetailResult(AdminMemberDetailStatus.NOT_FOUND)
                lifecycle_rows = (await session.execute(lifecycle_statement)).all()
        except Exception as error:
            logger.warning(
                "Web admin member detail query failed error_type=%s",
                type(error).__name__,
            )
            return AdminMemberDetailResult(AdminMemberDetailStatus.UNAVAILABLE)

        first = profile_rows[0]
        achievements = tuple(
            AdminMemberAchievement(
                key=str(row.achievement_key),
                title=(
                    definition.title
                    if (
                        definition := DEFAULT_ACHIEVEMENT_CATALOG.get(
                            str(row.achievement_key)
                        )
                    )
                    is not None
                    else None
                ),
                tier=(
                    definition.tier.value
                    if definition is not None and definition.tier is not None
                    else None
                ),
                unlocked_at=row.unlocked_at,
            )
            for row in profile_rows
            if row.achievement_key is not None and row.unlocked_at is not None
        )
        lifecycle_events = tuple(
            AdminMemberLifecycleEvent(
                event_type=str(row.event_type),
                occurred_at=row.occurred_at,
                absence_seconds=(
                    _safe_nonnegative_int(row.details_data, "absence_seconds")
                    if row.event_type == "member.returned"
                    else None
                ),
                return_number=(
                    value
                    if row.event_type == "member.returned"
                    and (
                        value := _safe_nonnegative_int(
                            row.details_data, "return_number"
                        )
                    )
                    is not None
                    and value >= 1
                    else None
                ),
            )
            for row in lifecycle_rows
        )
        detail = AdminMemberDetail(
            guild_id=int(first.guild_id),
            user_id=int(first.user_id),
            display_name=str(first.display_name),
            username=first.username,
            global_name=first.global_name,
            nickname=first.nickname,
            joined_at=first.joined_at,
            left_at=first.left_at,
            voice_seconds=int(first.voice_seconds),
            message_count=int(first.message_count),
            achievements=achievements,
            lifecycle_events=lifecycle_events,
            avatar_hash=first.avatar_hash,
            guild_avatar_hash=first.guild_avatar_hash,
        )
        return AdminMemberDetailResult(AdminMemberDetailStatus.FOUND, detail)
