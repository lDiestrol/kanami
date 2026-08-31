"""PostgreSQL aggregation for read-only voice statistics."""

from datetime import datetime
from typing import Protocol

from sqlalchemy import (
    BigInteger,
    Select,
    and_,
    case,
    cast,
    func,
    literal,
    select,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import FromClause

from discord_stats_bot.features.voice_statistics import (
    VoiceActivityInterval,
    VoiceChannelLeaderboard,
    VoiceChannelStatistics,
    VoiceChannelUsageEntry,
    VoiceCompanionEntry,
    VoiceFavoriteChannel,
    VoiceLeaderboard,
    VoiceLeaderboardEntry,
    VoicePairStatistics,
    VoicePeriodDurations,
    VoicePeriodStanding,
    VoiceProfileWindow,
    VoiceServerStatistics,
    VoiceStatistics,
    VoiceStatisticsPeriod,
    VoiceStatisticsQuery,
    VoiceUserProfileCore,
    VoiceUserStandings,
    VoiceUserTopChannels,
    VoiceUserTopCompanions,
)
from discord_stats_bot.persistence.models import (
    DiscordUser,
    VoiceChannel,
    VoiceInterval,
    VoiceSession,
)


def _duration_seconds(started_at: object, ended_at: object) -> object:
    return func.greatest(
        literal(0.0),
        func.extract("epoch", ended_at - started_at),
    )


class VoiceEligibilityQuery(Protocol):
    """Minimal bounds shared by voice statistics and server analytics."""

    as_of: datetime
    min_exact_session_seconds: int


def _eligible_voice_intervals(
    guild_id: int | None,
    query: VoiceEligibilityQuery,
    *,
    user_id: int | None = None,
    exclude_bots: bool = False,
    cte_prefix: str | None = None,
    member_scope: FromClause | None = None,
) -> tuple[object, object]:
    """Build shared effective-interval and whole-session eligibility CTEs."""

    effective_end = case(
        (
            VoiceInterval.ended_at.is_not(None),
            func.least(VoiceInterval.ended_at, query.as_of),
        ),
        else_=func.least(VoiceSession.confirmed_through_at, query.as_of),
    )
    statement = (
        select(
            VoiceInterval.session_id.label("session_id"),
            VoiceInterval.guild_id.label("guild_id"),
            VoiceInterval.user_id.label("user_id"),
            VoiceInterval.channel_id.label("channel_id"),
            VoiceInterval.quality.label("quality"),
            VoiceInterval.started_at.label("started_at"),
            effective_end.label("effective_end"),
        )
        .join(
            VoiceSession,
            and_(
                VoiceSession.id == VoiceInterval.session_id,
                VoiceSession.guild_id == VoiceInterval.guild_id,
                VoiceSession.user_id == VoiceInterval.user_id,
            ),
        )
        .where(
            VoiceInterval.is_afk.is_(False),
            VoiceInterval.started_at < query.as_of,
            effective_end > VoiceInterval.started_at,
        )
    )
    if guild_id is not None:
        statement = statement.where(VoiceInterval.guild_id == guild_id)
    if user_id is not None:
        statement = statement.where(VoiceInterval.user_id == user_id)
    if member_scope is not None:
        statement = statement.join(
            member_scope,
            and_(
                member_scope.c.guild_id == VoiceInterval.guild_id,
                member_scope.c.user_id == VoiceInterval.user_id,
            ),
        )
    if exclude_bots:
        statement = statement.join(
            DiscordUser,
            DiscordUser.id == VoiceInterval.user_id,
        ).where(DiscordUser.is_bot.is_(False))
    effective_intervals = statement.cte(
        f"{cte_prefix}_effective_voice_intervals"
        if cte_prefix
        else "effective_voice_intervals"
    )

    full_duration = _duration_seconds(
        effective_intervals.c.started_at,
        effective_intervals.c.effective_end,
    )
    eligible_sessions = (
        select(effective_intervals.c.session_id)
        .group_by(effective_intervals.c.session_id)
        .having(
            func.sum(
                case(
                    (effective_intervals.c.quality == "exact", full_duration),
                    else_=literal(0.0),
                )
            )
            >= query.min_exact_session_seconds
        )
        .cte(
            f"{cte_prefix}_eligible_voice_sessions"
            if cte_prefix
            else "eligible_voice_sessions"
        )
    )
    return effective_intervals, eligible_sessions


def eligible_voice_intervals(
    guild_id: int | None,
    query: VoiceEligibilityQuery,
    *,
    user_id: int | None = None,
    exclude_bots: bool = False,
    cte_prefix: str | None = None,
    member_scope: FromClause | None = None,
) -> tuple[object, object]:
    """Expose the single authoritative eligibility builder to bounded readers."""

    return _eligible_voice_intervals(
        guild_id,
        query,
        user_id=user_id,
        exclude_bots=exclude_bots,
        cte_prefix=cte_prefix,
        member_scope=member_scope,
    )


def _overlap_seconds(
    effective_intervals: object,
    started_at: datetime | None,
) -> tuple[object, object]:
    if started_at is None:
        return (
            _duration_seconds(
                effective_intervals.c.started_at,
                effective_intervals.c.effective_end,
            ),
            literal(True),
        )
    return (
        _duration_seconds(
            func.greatest(effective_intervals.c.started_at, started_at),
            effective_intervals.c.effective_end,
        ),
        effective_intervals.c.effective_end > started_at,
    )


def _bounded_overlap_seconds(
    effective_intervals: object,
    started_at: datetime,
    ended_at: datetime,
) -> tuple[object, object]:
    overlap_started_at = func.greatest(effective_intervals.c.started_at, started_at)
    overlap_ended_at = func.least(effective_intervals.c.effective_end, ended_at)
    return (
        _duration_seconds(overlap_started_at, overlap_ended_at),
        overlap_ended_at > overlap_started_at,
    )


def _quality_seconds(
    effective_intervals: object,
    quality: str,
    started_at: datetime | None,
    label: str,
) -> object:
    overlap, overlaps_window = _overlap_seconds(effective_intervals, started_at)
    summed = func.sum(
        case(
            (
                and_(
                    effective_intervals.c.quality == quality,
                    overlaps_window,
                ),
                overlap,
            ),
            else_=literal(0.0),
        )
    )
    return cast(func.floor(func.coalesce(summed, 0)), BigInteger).label(label)


def voice_member_all_time_totals_statement(
    query: VoiceStatisticsQuery,
    *,
    member_scope: FromClause,
) -> Select[tuple[int, ...]]:
    """Build scoped per-member all-time totals with command voice semantics."""

    effective_intervals, eligible_sessions = _eligible_voice_intervals(
        None,
        query,
        exclude_bots=True,
        cte_prefix="web_members",
        member_scope=member_scope,
    )
    eligible_intervals = effective_intervals.join(
        eligible_sessions,
        eligible_sessions.c.session_id == effective_intervals.c.session_id,
    )
    return (
        select(
            effective_intervals.c.guild_id,
            effective_intervals.c.user_id,
            _quality_seconds(
                effective_intervals,
                "exact",
                None,
                "exact_seconds",
            ),
            _quality_seconds(
                effective_intervals,
                "estimated",
                None,
                "estimated_seconds",
            ),
        )
        .select_from(eligible_intervals)
        .group_by(
            effective_intervals.c.guild_id,
            effective_intervals.c.user_id,
        )
    )


def _reporting_periods(
    query: VoiceStatisticsQuery,
) -> tuple[tuple[str, VoiceStatisticsPeriod, datetime | None], ...]:
    return (
        ("today", VoiceStatisticsPeriod.TODAY, query.today_started_at),
        (
            "last_7_days",
            VoiceStatisticsPeriod.LAST_7_DAYS,
            query.last_7_days_started_at,
        ),
        (
            "last_30_days",
            VoiceStatisticsPeriod.LAST_30_DAYS,
            query.last_30_days_started_at,
        ),
        ("all_time", VoiceStatisticsPeriod.ALL_TIME, None),
    )


def _voice_user_totals(
    guild_id: int,
    query: VoiceStatisticsQuery,
    periods: tuple[tuple[str, datetime | None], ...],
    *,
    cte_prefix: str | None = None,
) -> Select[tuple[int, ...]]:
    """Build shared non-bot per-user totals for one or more periods."""

    effective_intervals, eligible_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        exclude_bots=True,
        cte_prefix=cte_prefix,
    )
    eligible_intervals = effective_intervals.join(
        eligible_sessions,
        eligible_sessions.c.session_id == effective_intervals.c.session_id,
    )
    columns = [
        _quality_seconds(
            effective_intervals,
            quality,
            started_at,
            f"{period_name}_{quality}_seconds",
        )
        for period_name, started_at in periods
        for quality in ("exact", "estimated")
    ]
    return (
        select(effective_intervals.c.user_id, *columns)
        .select_from(eligible_intervals)
        .group_by(effective_intervals.c.user_id)
    )


def _ranking_order(
    user_id: object,
    exact_seconds: object,
    estimated_seconds: object,
) -> tuple[object, object, object]:
    total_seconds = exact_seconds + estimated_seconds
    return total_seconds.desc(), exact_seconds.desc(), user_id.asc()


def _channel_ranking_order(
    channel_id: object,
    exact_seconds: object,
    estimated_seconds: object,
) -> tuple[object, object, object]:
    total_seconds = exact_seconds + estimated_seconds
    return total_seconds.desc(), exact_seconds.desc(), channel_id.asc()


def _pair_overlap_expressions(
    first_intervals: object,
    second_intervals: object,
) -> tuple[object, object, object, object]:
    """Return the shared overlap boundaries, seconds and exact predicate."""

    overlap_started_at = func.greatest(
        first_intervals.c.started_at,
        second_intervals.c.started_at,
    )
    overlap_ended_at = func.least(
        first_intervals.c.effective_end,
        second_intervals.c.effective_end,
    )
    overlap_seconds = _duration_seconds(overlap_started_at, overlap_ended_at)
    is_exact = and_(
        first_intervals.c.quality == "exact",
        second_intervals.c.quality == "exact",
    )
    return overlap_started_at, overlap_ended_at, overlap_seconds, is_exact


def _pair_quality_seconds(
    overlap_seconds: object,
    is_exact: object,
    *,
    exact: bool,
    label: str,
) -> object:
    predicate = is_exact if exact else ~is_exact
    return cast(
        func.floor(
            func.coalesce(
                func.sum(case((predicate, overlap_seconds), else_=literal(0.0))),
                0,
            )
        ),
        BigInteger,
    ).label(label)


def _eligible_total_seconds(
    effective_intervals: object,
    eligible_sessions: object,
) -> object:
    eligible_intervals = effective_intervals.join(
        eligible_sessions,
        eligible_sessions.c.session_id == effective_intervals.c.session_id,
    )
    return (
        select(
            cast(
                func.floor(
                    func.coalesce(
                        func.sum(
                            _duration_seconds(
                                effective_intervals.c.started_at,
                                effective_intervals.c.effective_end,
                            )
                        ),
                        0,
                    )
                ),
                BigInteger,
            )
        )
        .select_from(eligible_intervals)
        .scalar_subquery()
    )


def _voice_channel_totals(
    guild_id: int,
    query: VoiceStatisticsQuery,
    started_at: datetime | None,
    *,
    user_id: int | None = None,
    cte_prefix: str | None = None,
) -> object:
    """Build shared eligible per-channel totals from persisted interval channels."""

    effective_intervals, eligible_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        user_id=user_id,
        exclude_bots=True,
        cte_prefix=cte_prefix,
    )
    eligible_intervals = effective_intervals.join(
        eligible_sessions,
        eligible_sessions.c.session_id == effective_intervals.c.session_id,
    )
    return (
        select(
            effective_intervals.c.channel_id,
            _quality_seconds(effective_intervals, "exact", started_at, "exact_seconds"),
            _quality_seconds(
                effective_intervals,
                "estimated",
                started_at,
                "estimated_seconds",
            ),
        )
        .select_from(eligible_intervals)
        .group_by(effective_intervals.c.channel_id)
        .cte(
            f"{cte_prefix}_voice_channel_totals"
            if cte_prefix
            else "voice_channel_totals"
        )
    )


def _ranked_channel_totals_statement(
    guild_id: int,
    query: VoiceStatisticsQuery,
    started_at: datetime | None,
    *,
    limit: int,
    user_id: int | None = None,
) -> Select[tuple[int, int, int]]:
    totals = _voice_channel_totals(guild_id, query, started_at, user_id=user_id)
    total_seconds = totals.c.exact_seconds + totals.c.estimated_seconds
    return (
        select(
            totals.c.channel_id,
            totals.c.exact_seconds,
            totals.c.estimated_seconds,
        )
        .where(total_seconds > 0)
        .order_by(
            *_channel_ranking_order(
                totals.c.channel_id,
                totals.c.exact_seconds,
                totals.c.estimated_seconds,
            )
        )
        .limit(limit)
    )


def voice_statistics_statement(
    guild_id: int,
    user_id: int,
    query: VoiceStatisticsQuery,
) -> Select[tuple[int, ...]]:
    """Build one bounded PostgreSQL aggregate for all reporting periods."""

    effective_intervals, eligible_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        user_id=user_id,
    )
    eligible_intervals = effective_intervals.join(
        eligible_sessions,
        eligible_sessions.c.session_id == effective_intervals.c.session_id,
    )

    columns = [
        _quality_seconds(
            effective_intervals,
            quality,
            started_at,
            f"{period_name}_{quality}",
        )
        for period_name, _, started_at in _reporting_periods(query)
        for quality in ("exact", "estimated")
    ]
    return select(*columns).select_from(eligible_intervals)


def voice_leaderboard_statement(
    guild_id: int,
    period: VoiceStatisticsPeriod,
    query: VoiceStatisticsQuery,
) -> Select[tuple[int, int, int]]:
    """Build one non-bot, deterministic, bounded TOP 10 aggregate."""

    totals = _voice_user_totals(
        guild_id,
        query,
        (("selected_period", query.started_at_for(period)),),
    ).cte("voice_leaderboard_totals")
    exact_seconds = totals.c.selected_period_exact_seconds
    estimated_seconds = totals.c.selected_period_estimated_seconds
    total_seconds = exact_seconds + estimated_seconds
    return (
        select(
            totals.c.user_id,
            exact_seconds.label("exact_seconds"),
            estimated_seconds.label("estimated_seconds"),
        )
        .where(total_seconds > 0)
        .order_by(*_ranking_order(totals.c.user_id, exact_seconds, estimated_seconds))
        .limit(10)
    )


def voice_user_standings_statement(
    guild_id: int,
    user_id: int,
    query: VoiceStatisticsQuery,
) -> Select[tuple[int | None, ...]]:
    """Build one deterministic full-ranking aggregate for all four periods."""

    reporting_periods = _reporting_periods(query)
    wide_totals = _voice_user_totals(
        guild_id,
        query,
        tuple((name, started_at) for name, _, started_at in reporting_periods),
    ).cte("voice_standing_wide_totals")
    period_totals = union_all(
        *(
            select(
                wide_totals.c.user_id,
                literal(period.value).label("period"),
                getattr(wide_totals.c, f"{name}_exact_seconds").label("exact_seconds"),
                getattr(wide_totals.c, f"{name}_estimated_seconds").label(
                    "estimated_seconds"
                ),
            )
            for name, period, _ in reporting_periods
        )
    ).cte("voice_standing_period_totals")
    total_seconds = period_totals.c.exact_seconds + period_totals.c.estimated_seconds
    active_totals = (
        select(period_totals)
        .where(total_seconds > 0)
        .cte("active_voice_standing_totals")
    )
    ranked = select(
        active_totals.c.period,
        active_totals.c.user_id,
        func.row_number()
        .over(
            partition_by=active_totals.c.period,
            order_by=_ranking_order(
                active_totals.c.user_id,
                active_totals.c.exact_seconds,
                active_totals.c.estimated_seconds,
            ),
        )
        .label("rank"),
    ).cte("ranked_voice_standings")

    columns: list[object] = []
    for name, period, _ in reporting_periods:
        in_period = ranked.c.period == period.value
        columns.extend(
            (
                func.max(
                    case(
                        (
                            and_(in_period, ranked.c.user_id == user_id),
                            ranked.c.rank,
                        )
                    )
                ).label(f"{name}_rank"),
                cast(
                    func.count(case((in_period, literal(1)))),
                    BigInteger,
                ).label(f"{name}_participant_count"),
            )
        )
    return select(*columns).select_from(ranked)


def voice_user_top_channels_statement(
    guild_id: int,
    user_id: int,
    query: VoiceStatisticsQuery,
) -> Select[tuple[int, int, int]]:
    """Build one read-only all-time TOP 3 channel aggregate for one user."""

    return _ranked_channel_totals_statement(
        guild_id,
        query,
        None,
        limit=3,
        user_id=user_id,
    )


def voice_user_top_companions_statement(
    guild_id: int,
    user_id: int,
    query: VoiceStatisticsQuery,
) -> Select[tuple[int, int, int]]:
    """Build all-time TOP 3 overlaps in the same eligible voice channel."""

    target_intervals, target_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        user_id=user_id,
        cte_prefix="target",
    )
    companion_intervals, companion_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        exclude_bots=True,
        cte_prefix="companion",
    )
    target = target_intervals.join(
        target_sessions,
        target_sessions.c.session_id == target_intervals.c.session_id,
    )
    companions = companion_intervals.join(
        companion_sessions,
        companion_sessions.c.session_id == companion_intervals.c.session_id,
    )
    overlap_started_at, overlap_ended_at, overlap_seconds, is_exact = (
        _pair_overlap_expressions(target_intervals, companion_intervals)
    )
    exact_seconds = _pair_quality_seconds(
        overlap_seconds, is_exact, exact=True, label="exact_seconds"
    )
    estimated_seconds = _pair_quality_seconds(
        overlap_seconds, is_exact, exact=False, label="estimated_seconds"
    )
    totals = (
        select(companion_intervals.c.user_id, exact_seconds, estimated_seconds)
        .select_from(
            target.join(
                companions,
                and_(
                    companion_intervals.c.channel_id == target_intervals.c.channel_id,
                    companion_intervals.c.user_id != user_id,
                    overlap_ended_at > overlap_started_at,
                ),
            )
        )
        .group_by(companion_intervals.c.user_id)
        .cte("voice_companion_totals")
    )
    total_seconds = totals.c.exact_seconds + totals.c.estimated_seconds
    return (
        select(totals.c.user_id, totals.c.exact_seconds, totals.c.estimated_seconds)
        .where(total_seconds > 0)
        .order_by(
            *_ranking_order(
                totals.c.user_id, totals.c.exact_seconds, totals.c.estimated_seconds
            )
        )
        .limit(3)
    )


def voice_user_profile_core_statement(
    guild_id: int,
    user_id: int,
    query: VoiceStatisticsQuery,
    window: VoiceProfileWindow,
) -> Select[tuple[object, ...]]:
    """Build the selected-period profile core as one read-only aggregate."""

    totals = _voice_user_totals(
        guild_id,
        query,
        (("selected_period", window.started_at),),
        cte_prefix="profile_ranking",
    ).cte("voice_profile_user_totals")
    exact_seconds = totals.c.selected_period_exact_seconds
    estimated_seconds = totals.c.selected_period_estimated_seconds
    active_totals = (
        select(totals)
        .where(exact_seconds + estimated_seconds > 0)
        .cte("active_voice_profile_totals")
    )
    ranked = select(
        active_totals,
        func.row_number()
        .over(
            order_by=_ranking_order(
                active_totals.c.user_id,
                active_totals.c.selected_period_exact_seconds,
                active_totals.c.selected_period_estimated_seconds,
            )
        )
        .label("rank"),
    ).cte("ranked_voice_profile_totals")

    target_intervals, target_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        user_id=user_id,
        cte_prefix="profile_target",
    )
    eligible_target = target_intervals.join(
        target_sessions,
        target_sessions.c.session_id == target_intervals.c.session_id,
    )
    current_overlap, current_overlaps = _overlap_seconds(
        target_intervals, window.started_at
    )

    def quality_sum(quality: str, overlap: object, predicate: object) -> object:
        return cast(
            func.floor(
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    target_intervals.c.quality == quality,
                                    predicate,
                                ),
                                overlap,
                            ),
                            else_=literal(0.0),
                        )
                    ),
                    0,
                )
            ),
            BigInteger,
        )

    if window.previous_started_at is None or window.previous_ended_at is None:
        previous_exact = literal(0, type_=BigInteger)
        previous_estimated = literal(0, type_=BigInteger)
    else:
        previous_overlap, previous_overlaps = _bounded_overlap_seconds(
            target_intervals,
            window.previous_started_at,
            window.previous_ended_at,
        )
        previous_exact = quality_sum("exact", previous_overlap, previous_overlaps)
        previous_estimated = quality_sum(
            "estimated", previous_overlap, previous_overlaps
        )

    target_summary = (
        select(
            quality_sum("exact", current_overlap, current_overlaps).label(
                "exact_seconds"
            ),
            quality_sum("estimated", current_overlap, current_overlaps).label(
                "estimated_seconds"
            ),
            cast(
                func.count(
                    func.distinct(
                        case(
                            (current_overlaps, target_intervals.c.session_id),
                        )
                    )
                ),
                BigInteger,
            ).label("session_count"),
            previous_exact.label("previous_exact_seconds"),
            previous_estimated.label("previous_estimated_seconds"),
        )
        .select_from(eligible_target)
        .cte("voice_profile_target_summary")
    )

    channel_totals = _voice_channel_totals(
        guild_id,
        query,
        window.started_at,
        user_id=user_id,
        cte_prefix="profile_favorite",
    )
    channel_total_seconds = (
        channel_totals.c.exact_seconds + channel_totals.c.estimated_seconds
    )
    favorite = (
        select(
            channel_totals.c.channel_id,
            VoiceChannel.name.label("channel_name"),
            channel_totals.c.exact_seconds,
            channel_totals.c.estimated_seconds,
        )
        .join(
            VoiceChannel,
            and_(
                VoiceChannel.guild_id == guild_id,
                VoiceChannel.id == channel_totals.c.channel_id,
            ),
            isouter=True,
        )
        .where(channel_total_seconds > 0)
        .order_by(
            *_channel_ranking_order(
                channel_totals.c.channel_id,
                channel_totals.c.exact_seconds,
                channel_totals.c.estimated_seconds,
            )
        )
        .limit(1)
        .cte("voice_profile_favorite_channel")
    )

    return select(
        target_summary.c.exact_seconds,
        target_summary.c.estimated_seconds,
        target_summary.c.session_count,
        target_summary.c.previous_exact_seconds,
        target_summary.c.previous_estimated_seconds,
        select(ranked.c.rank)
        .where(ranked.c.user_id == user_id)
        .scalar_subquery()
        .label("rank"),
        select(func.count())
        .select_from(ranked)
        .scalar_subquery()
        .label("participant_count"),
        select(favorite.c.channel_id).scalar_subquery().label("favorite_channel_id"),
        select(favorite.c.channel_name)
        .scalar_subquery()
        .label("favorite_channel_name"),
        select(favorite.c.exact_seconds)
        .scalar_subquery()
        .label("favorite_exact_seconds"),
        select(favorite.c.estimated_seconds)
        .scalar_subquery()
        .label("favorite_estimated_seconds"),
    ).select_from(target_summary)


def voice_user_profile_companions_statement(
    guild_id: int,
    user_id: int,
    query: VoiceStatisticsQuery,
    window: VoiceProfileWindow,
) -> Select[tuple[int, int, int]]:
    """Build selected-period TOP 3 co-presence using existing overlap semantics."""

    target_intervals, target_sessions = _eligible_voice_intervals(
        guild_id, query, user_id=user_id, cte_prefix="profile_companion_target"
    )
    companion_intervals, companion_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        exclude_bots=True,
        cte_prefix="profile_companion",
    )
    target = target_intervals.join(
        target_sessions,
        target_sessions.c.session_id == target_intervals.c.session_id,
    )
    companions = companion_intervals.join(
        companion_sessions,
        companion_sessions.c.session_id == companion_intervals.c.session_id,
    )
    overlap_started_at, overlap_ended_at, _, is_exact = _pair_overlap_expressions(
        target_intervals, companion_intervals
    )
    if window.started_at is not None:
        overlap_started_at = func.greatest(overlap_started_at, window.started_at)
    overlap_seconds = _duration_seconds(overlap_started_at, overlap_ended_at)
    exact = _pair_quality_seconds(
        overlap_seconds, is_exact, exact=True, label="exact_seconds"
    )
    estimated = _pair_quality_seconds(
        overlap_seconds, is_exact, exact=False, label="estimated_seconds"
    )
    companion_totals = (
        select(companion_intervals.c.user_id, exact, estimated)
        .select_from(
            target.join(
                companions,
                and_(
                    companion_intervals.c.channel_id == target_intervals.c.channel_id,
                    companion_intervals.c.user_id != user_id,
                    overlap_ended_at > overlap_started_at,
                ),
            )
        )
        .group_by(companion_intervals.c.user_id)
        .cte("voice_profile_companion_totals")
    )
    total = companion_totals.c.exact_seconds + companion_totals.c.estimated_seconds
    return (
        select(
            companion_totals.c.user_id,
            companion_totals.c.exact_seconds,
            companion_totals.c.estimated_seconds,
        )
        .where(total > 0)
        .order_by(
            *_ranking_order(
                companion_totals.c.user_id,
                companion_totals.c.exact_seconds,
                companion_totals.c.estimated_seconds,
            )
        )
        .limit(3)
    )


def voice_pair_statistics_statement(
    guild_id: int,
    user1_id: int,
    user2_id: int,
    query: VoiceStatisticsQuery,
) -> Select[tuple[int, ...]]:
    """Build pair totals, individual totals and TOP 3 common channels."""

    user1_intervals, user1_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        user_id=user1_id,
        exclude_bots=True,
        cte_prefix="pair_user1",
    )
    user2_intervals, user2_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        user_id=user2_id,
        exclude_bots=True,
        cte_prefix="pair_user2",
    )
    user1_eligible = user1_intervals.join(
        user1_sessions,
        user1_sessions.c.session_id == user1_intervals.c.session_id,
    )
    user2_eligible = user2_intervals.join(
        user2_sessions,
        user2_sessions.c.session_id == user2_intervals.c.session_id,
    )
    overlap_started_at, overlap_ended_at, overlap_seconds, is_exact = (
        _pair_overlap_expressions(user1_intervals, user2_intervals)
    )
    per_channel = (
        select(
            user1_intervals.c.channel_id,
            _pair_quality_seconds(
                overlap_seconds,
                is_exact,
                exact=True,
                label="exact_seconds",
            ),
            _pair_quality_seconds(
                overlap_seconds,
                is_exact,
                exact=False,
                label="estimated_seconds",
            ),
        )
        .select_from(
            user1_eligible.join(
                user2_eligible,
                and_(
                    user1_intervals.c.channel_id == user2_intervals.c.channel_id,
                    overlap_ended_at > overlap_started_at,
                ),
            )
        )
        .group_by(user1_intervals.c.channel_id)
        .cte("voice_pair_channel_totals")
    )
    channel_total = per_channel.c.exact_seconds + per_channel.c.estimated_seconds
    active_channels = (
        select(per_channel).where(channel_total > 0).cte("active_voice_pair_channels")
    )
    ranked_channels = select(
        active_channels,
        func.row_number()
        .over(
            order_by=_channel_ranking_order(
                active_channels.c.channel_id,
                active_channels.c.exact_seconds,
                active_channels.c.estimated_seconds,
            )
        )
        .label("rank"),
    ).cte("ranked_voice_pair_channels")
    summary = select(
        cast(
            func.coalesce(
                select(func.sum(active_channels.c.exact_seconds)).scalar_subquery(),
                0,
            ),
            BigInteger,
        ).label("pair_exact_seconds"),
        cast(
            func.coalesce(
                select(func.sum(active_channels.c.estimated_seconds)).scalar_subquery(),
                0,
            ),
            BigInteger,
        ).label("pair_estimated_seconds"),
        _eligible_total_seconds(user1_intervals, user1_sessions).label(
            "user1_total_seconds"
        ),
        _eligible_total_seconds(user2_intervals, user2_sessions).label(
            "user2_total_seconds"
        ),
    ).cte("voice_pair_summary")
    return (
        select(
            summary,
            ranked_channels.c.channel_id,
            ranked_channels.c.exact_seconds,
            ranked_channels.c.estimated_seconds,
        )
        .select_from(
            summary.outerjoin(
                ranked_channels,
                ranked_channels.c.rank <= 3,
            )
        )
        .order_by(ranked_channels.c.rank.asc())
    )


def voice_channel_leaderboard_statement(
    guild_id: int,
    period: VoiceStatisticsPeriod,
    query: VoiceStatisticsQuery,
) -> Select[tuple[int, int, int]]:
    """Build one read-only non-bot TOP 10 channel aggregate."""

    return _ranked_channel_totals_statement(
        guild_id,
        query,
        query.started_at_for(period),
        limit=10,
    )


def voice_channel_statistics_statement(
    guild_id: int,
    channel_id: int,
    period: VoiceStatisticsPeriod,
    query: VoiceStatisticsQuery,
) -> Select[tuple[int, ...]]:
    """Build one channel total and user TOP 10 in one aggregate SELECT."""

    effective_intervals, eligible_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        exclude_bots=True,
    )
    eligible_intervals = effective_intervals.join(
        eligible_sessions,
        eligible_sessions.c.session_id == effective_intervals.c.session_id,
    )
    started_at = query.started_at_for(period)
    per_user = (
        select(
            effective_intervals.c.user_id,
            _quality_seconds(effective_intervals, "exact", started_at, "exact_seconds"),
            _quality_seconds(
                effective_intervals,
                "estimated",
                started_at,
                "estimated_seconds",
            ),
        )
        .select_from(eligible_intervals)
        .where(effective_intervals.c.channel_id == channel_id)
        .group_by(effective_intervals.c.user_id)
        .cte("voice_channel_user_totals")
    )
    channel_totals = (
        select(
            _quality_seconds(
                effective_intervals,
                "exact",
                started_at,
                "channel_exact_seconds",
            ),
            _quality_seconds(
                effective_intervals,
                "estimated",
                started_at,
                "channel_estimated_seconds",
            ),
        )
        .select_from(eligible_intervals)
        .where(effective_intervals.c.channel_id == channel_id)
        .cte("selected_voice_channel_totals")
    )
    total_seconds = per_user.c.exact_seconds + per_user.c.estimated_seconds
    active_users = (
        select(per_user).where(total_seconds > 0).cte("active_voice_channel_users")
    )
    ranked = select(
        active_users.c.user_id,
        active_users.c.exact_seconds,
        active_users.c.estimated_seconds,
        func.row_number()
        .over(
            order_by=_ranking_order(
                active_users.c.user_id,
                active_users.c.exact_seconds,
                active_users.c.estimated_seconds,
            )
        )
        .label("rank"),
    ).cte("ranked_voice_channel_users")
    return (
        select(
            ranked,
            channel_totals.c.channel_exact_seconds,
            channel_totals.c.channel_estimated_seconds,
        )
        .select_from(
            channel_totals.outerjoin(
                ranked,
                ranked.c.rank <= 10,
            )
        )
        .order_by(ranked.c.rank.asc())
    )


def voice_server_statistics_statement(
    guild_id: int,
    period: VoiceStatisticsPeriod,
    query: VoiceStatisticsQuery,
) -> Select[tuple[int, ...]]:
    """Build server totals, active count and user/channel TOP 1."""

    effective_intervals, eligible_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        exclude_bots=True,
        cte_prefix="server",
    )
    eligible_intervals = effective_intervals.join(
        eligible_sessions,
        eligible_sessions.c.session_id == effective_intervals.c.session_id,
    )
    started_at = query.started_at_for(period)
    per_user = (
        select(
            effective_intervals.c.user_id,
            _quality_seconds(
                effective_intervals,
                "exact",
                started_at,
                "exact_seconds",
            ),
            _quality_seconds(
                effective_intervals,
                "estimated",
                started_at,
                "estimated_seconds",
            ),
        )
        .select_from(eligible_intervals)
        .group_by(effective_intervals.c.user_id)
        .cte("voice_server_user_totals")
    )
    user_total = per_user.c.exact_seconds + per_user.c.estimated_seconds
    active_users = (
        select(per_user).where(user_total > 0).cte("active_voice_server_users")
    )
    ranked_users = select(
        active_users,
        func.row_number()
        .over(
            order_by=_ranking_order(
                active_users.c.user_id,
                active_users.c.exact_seconds,
                active_users.c.estimated_seconds,
            )
        )
        .label("rank"),
    ).cte("ranked_voice_server_users")
    per_channel = (
        select(
            effective_intervals.c.channel_id,
            _quality_seconds(
                effective_intervals,
                "exact",
                started_at,
                "exact_seconds",
            ),
            _quality_seconds(
                effective_intervals,
                "estimated",
                started_at,
                "estimated_seconds",
            ),
        )
        .select_from(eligible_intervals)
        .group_by(effective_intervals.c.channel_id)
        .cte("voice_server_channel_totals")
    )
    channel_total = per_channel.c.exact_seconds + per_channel.c.estimated_seconds
    active_channels = (
        select(per_channel).where(channel_total > 0).cte("active_voice_server_channels")
    )
    ranked_channels = select(
        active_channels,
        func.row_number()
        .over(
            order_by=_channel_ranking_order(
                active_channels.c.channel_id,
                active_channels.c.exact_seconds,
                active_channels.c.estimated_seconds,
            )
        )
        .label("rank"),
    ).cte("ranked_voice_server_channels")
    summary = select(
        cast(
            func.coalesce(
                select(func.sum(active_users.c.exact_seconds)).scalar_subquery(),
                0,
            ),
            BigInteger,
        ).label("server_exact_seconds"),
        cast(
            func.coalesce(
                select(func.sum(active_users.c.estimated_seconds)).scalar_subquery(),
                0,
            ),
            BigInteger,
        ).label("server_estimated_seconds"),
        cast(
            select(func.count()).select_from(active_users).scalar_subquery(),
            BigInteger,
        ).label("active_users"),
    ).cte("voice_server_summary")
    return select(
        summary,
        ranked_users.c.user_id.label("top_user_id"),
        ranked_users.c.exact_seconds.label("top_user_exact_seconds"),
        ranked_users.c.estimated_seconds.label("top_user_estimated_seconds"),
        ranked_channels.c.channel_id.label("top_channel_id"),
        ranked_channels.c.exact_seconds.label("top_channel_exact_seconds"),
        ranked_channels.c.estimated_seconds.label("top_channel_estimated_seconds"),
    ).select_from(
        summary.outerjoin(ranked_users, ranked_users.c.rank == 1).outerjoin(
            ranked_channels,
            ranked_channels.c.rank == 1,
        )
    )


def voice_activity_intervals_statement(
    guild_id: int,
    started_at: datetime,
    query: VoiceStatisticsQuery,
) -> Select[tuple[datetime, datetime, str]]:
    """Fetch only eligible guild intervals overlapping one bounded activity window."""

    effective_intervals, eligible_sessions = _eligible_voice_intervals(
        guild_id,
        query,
        exclude_bots=True,
        cte_prefix="activity",
    )
    eligible_intervals = effective_intervals.join(
        eligible_sessions,
        eligible_sessions.c.session_id == effective_intervals.c.session_id,
    )
    clipped_start = func.greatest(effective_intervals.c.started_at, started_at).label(
        "started_at"
    )
    clipped_end = func.least(effective_intervals.c.effective_end, query.as_of).label(
        "ended_at"
    )
    return (
        select(
            clipped_start,
            clipped_end,
            effective_intervals.c.quality,
        )
        .select_from(eligible_intervals)
        .where(effective_intervals.c.effective_end > started_at)
        .order_by(
            clipped_start.asc(),
            effective_intervals.c.user_id.asc(),
            effective_intervals.c.session_id.asc(),
        )
    )


class SqlAlchemyVoiceStatisticsRepository:
    """Execute aggregate reads on a caller-owned async session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_user_statistics(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoiceStatistics:
        result = await self._session.execute(
            voice_statistics_statement(guild_id, user_id, query)
        )
        row = result.one()

        def period(name: str) -> VoicePeriodDurations:
            return VoicePeriodDurations(
                exact_seconds=int(getattr(row, f"{name}_exact")),
                estimated_seconds=int(getattr(row, f"{name}_estimated")),
            )

        return VoiceStatistics(
            as_of=query.as_of,
            today=period("today"),
            last_7_days=period("last_7_days"),
            last_30_days=period("last_30_days"),
            all_time=period("all_time"),
        )

    async def get_leaderboard(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> VoiceLeaderboard:
        result = await self._session.execute(
            voice_leaderboard_statement(guild_id, period, query)
        )
        entries = tuple(
            VoiceLeaderboardEntry(
                user_id=row.user_id,
                exact_seconds=int(row.exact_seconds),
                estimated_seconds=int(row.estimated_seconds),
            )
            for row in result
        )
        return VoiceLeaderboard(as_of=query.as_of, period=period, entries=entries)

    async def get_user_standings(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoiceUserStandings:
        result = await self._session.execute(
            voice_user_standings_statement(guild_id, user_id, query)
        )
        row = result.one()

        def standing(name: str) -> VoicePeriodStanding:
            rank = getattr(row, f"{name}_rank")
            return VoicePeriodStanding(
                rank=int(rank) if rank is not None else None,
                participant_count=int(getattr(row, f"{name}_participant_count")),
            )

        return VoiceUserStandings(
            as_of=query.as_of,
            today=standing("today"),
            last_7_days=standing("last_7_days"),
            last_30_days=standing("last_30_days"),
            all_time=standing("all_time"),
        )

    async def get_user_top_channels(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoiceUserTopChannels:
        result = await self._session.execute(
            voice_user_top_channels_statement(guild_id, user_id, query)
        )
        return VoiceUserTopChannels(
            as_of=query.as_of,
            entries=_channel_entries(result),
        )

    async def get_user_top_companions(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoiceUserTopCompanions:
        result = await self._session.execute(
            voice_user_top_companions_statement(guild_id, user_id, query)
        )
        return VoiceUserTopCompanions(
            as_of=query.as_of,
            entries=tuple(
                VoiceCompanionEntry(
                    user_id=row.user_id,
                    exact_seconds=int(row.exact_seconds),
                    estimated_seconds=int(row.estimated_seconds),
                )
                for row in result
            ),
        )

    async def get_user_profile_core(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
        window: VoiceProfileWindow,
    ) -> VoiceUserProfileCore:
        result = await self._session.execute(
            voice_user_profile_core_statement(guild_id, user_id, query, window)
        )
        row = result.one()
        favorite = None
        if row.favorite_channel_id is not None:
            favorite = VoiceFavoriteChannel(
                channel_id=int(row.favorite_channel_id),
                channel_name=row.favorite_channel_name,
                exact_seconds=int(row.favorite_exact_seconds),
                estimated_seconds=int(row.favorite_estimated_seconds),
            )
        previous = None
        if window.period is not VoiceStatisticsPeriod.ALL_TIME:
            previous = VoicePeriodDurations(
                int(row.previous_exact_seconds),
                int(row.previous_estimated_seconds),
            )
        return VoiceUserProfileCore(
            as_of=query.as_of,
            period=window.period,
            durations=VoicePeriodDurations(
                int(row.exact_seconds), int(row.estimated_seconds)
            ),
            standing=VoicePeriodStanding(
                rank=int(row.rank) if row.rank is not None else None,
                participant_count=int(row.participant_count),
            ),
            session_count=int(row.session_count),
            favorite_channel=favorite,
            previous_durations=previous,
        )

    async def get_user_profile_companions(
        self,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
        window: VoiceProfileWindow,
    ) -> tuple[VoiceCompanionEntry, ...]:
        result = await self._session.execute(
            voice_user_profile_companions_statement(guild_id, user_id, query, window)
        )
        return tuple(
            VoiceCompanionEntry(
                user_id=int(row.user_id),
                exact_seconds=int(row.exact_seconds),
                estimated_seconds=int(row.estimated_seconds),
            )
            for row in result
        )

    async def get_pair_statistics(
        self,
        guild_id: int,
        user1_id: int,
        user2_id: int,
        query: VoiceStatisticsQuery,
    ) -> VoicePairStatistics:
        result = await self._session.execute(
            voice_pair_statistics_statement(guild_id, user1_id, user2_id, query)
        )
        rows = result.all()
        row = rows[0]
        return VoicePairStatistics(
            as_of=query.as_of,
            user1_id=user1_id,
            user2_id=user2_id,
            exact_seconds=int(row.pair_exact_seconds),
            estimated_seconds=int(row.pair_estimated_seconds),
            user1_total_seconds=int(row.user1_total_seconds),
            user2_total_seconds=int(row.user2_total_seconds),
            channels=tuple(
                VoiceChannelUsageEntry(
                    channel_id=channel_row.channel_id,
                    exact_seconds=int(channel_row.exact_seconds),
                    estimated_seconds=int(channel_row.estimated_seconds),
                )
                for channel_row in rows
                if channel_row.channel_id is not None
            ),
        )

    async def get_channel_leaderboard(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> VoiceChannelLeaderboard:
        result = await self._session.execute(
            voice_channel_leaderboard_statement(guild_id, period, query)
        )
        return VoiceChannelLeaderboard(
            as_of=query.as_of,
            period=period,
            entries=_channel_entries(result),
        )

    async def get_channel_statistics(
        self,
        guild_id: int,
        channel_id: int,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> VoiceChannelStatistics:
        result = await self._session.execute(
            voice_channel_statistics_statement(guild_id, channel_id, period, query)
        )
        rows = result.all()
        return VoiceChannelStatistics(
            as_of=query.as_of,
            period=period,
            channel_id=channel_id,
            exact_seconds=int(rows[0].channel_exact_seconds) if rows else 0,
            estimated_seconds=(int(rows[0].channel_estimated_seconds) if rows else 0),
            entries=tuple(
                VoiceLeaderboardEntry(
                    user_id=row.user_id,
                    exact_seconds=int(row.exact_seconds),
                    estimated_seconds=int(row.estimated_seconds),
                )
                for row in rows
                if row.user_id is not None
            ),
        )

    async def get_server_statistics(
        self,
        guild_id: int,
        period: VoiceStatisticsPeriod,
        query: VoiceStatisticsQuery,
    ) -> VoiceServerStatistics:
        result = await self._session.execute(
            voice_server_statistics_statement(guild_id, period, query)
        )
        row = result.one()
        return VoiceServerStatistics(
            as_of=query.as_of,
            period=period,
            exact_seconds=int(row.server_exact_seconds),
            estimated_seconds=int(row.server_estimated_seconds),
            active_users=int(row.active_users),
            top_user=(
                VoiceLeaderboardEntry(
                    user_id=row.top_user_id,
                    exact_seconds=int(row.top_user_exact_seconds),
                    estimated_seconds=int(row.top_user_estimated_seconds),
                )
                if row.top_user_id is not None
                else None
            ),
            top_channel=(
                VoiceChannelUsageEntry(
                    channel_id=row.top_channel_id,
                    exact_seconds=int(row.top_channel_exact_seconds),
                    estimated_seconds=int(row.top_channel_estimated_seconds),
                )
                if row.top_channel_id is not None
                else None
            ),
        )

    async def get_activity_intervals(
        self,
        guild_id: int,
        started_at: datetime,
        query: VoiceStatisticsQuery,
    ) -> tuple[VoiceActivityInterval, ...]:
        result = await self._session.execute(
            voice_activity_intervals_statement(guild_id, started_at, query)
        )
        return tuple(
            VoiceActivityInterval(
                started_at=row.started_at,
                ended_at=row.ended_at,
                quality=row.quality,
            )
            for row in result
        )


def _channel_entries(result: object) -> tuple[VoiceChannelUsageEntry, ...]:
    return tuple(
        VoiceChannelUsageEntry(
            channel_id=row.channel_id,
            exact_seconds=int(row.exact_seconds),
            estimated_seconds=int(row.estimated_seconds),
        )
        for row in result
    )
