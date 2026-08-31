"""Read-only Operations health evaluation and presentation for Web Admin."""

import asyncio
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from html import escape
from pathlib import Path
from typing import Protocol

from sqlalchemy import and_, func, literal_column, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.features.bot_profile import (
    BotProfileErrorCategory,
    BotProfileOperationError,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.persistence.models import (
    GameSession,
    OperationalHealthObservation,
    VoiceSession,
)
from discord_stats_bot.web.authorization import WebAdminRole
from discord_stats_bot.web.bot_control import BotProfileControl
from discord_stats_bot.web.presentation import render_admin_page

logger = logging.getLogger(__name__)
VOICE_STALE_THRESHOLD_SECONDS = 180
MINIMUM_GAME_STALE_THRESHOLD_SECONDS = 180
STALE_INTERVAL_MULTIPLIER = 3
HEALTH_OBSERVATION_INTERVAL_SECONDS = 60
OPERATIONAL_HISTORY_LOOKBACK_DAYS = 8


def tracking_stale_threshold(interval_seconds: int) -> int:
    return max(
        interval_seconds * STALE_INTERVAL_MULTIPLIER,
        VOICE_STALE_THRESHOLD_SECONDS,
    )


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    NEUTRAL = "neutral"


class OperationsBotStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class OperationsControlStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DiagnosticReason:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    status: HealthStatus
    reasons: tuple[DiagnosticReason, ...] = ()


@dataclass(frozen=True, slots=True)
class GitMetadata:
    commit: str | None = None
    branch: str | None = None


@dataclass(frozen=True, slots=True)
class TrackingState:
    open_sessions: int | None = None
    last_confirmed_at: datetime | None = None
    duplicate_open_session_groups: int | None = None
    temporal_violation_count: int | None = None
    oldest_confirmed_at: datetime | None = None
    missing_confirmation_count: int | None = None


@dataclass(frozen=True, slots=True)
class PostgreSQLState:
    available: bool
    latency_seconds: float | None = None
    database_size_bytes: int | None = None
    alembic_revision: str | None = None
    voice: TrackingState = TrackingState()
    game: TrackingState = TrackingState()


@dataclass(frozen=True, slots=True)
class OperationsBotState:
    status: OperationsBotStatus
    control_status: OperationsControlStatus
    health: ComponentHealth


@dataclass(frozen=True, slots=True)
class TrackingHealth:
    state: TrackingState
    health: ComponentHealth
    stale_threshold_seconds: int
    confirmation_age_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class IntegrityCheck:
    key: str
    status: HealthStatus
    message: str
    violation_count: int | None


@dataclass(frozen=True, slots=True)
class DataIntegrityHealth:
    status: HealthStatus
    checks: tuple[IntegrityCheck, ...]

    @property
    def passed_count(self) -> int:
        return sum(check.status is HealthStatus.HEALTHY for check in self.checks)


@dataclass(frozen=True, slots=True)
class HealthObservation:
    observed_at: datetime
    status: HealthStatus
    component: str
    reason: str


@dataclass(frozen=True, slots=True)
class AvailabilityWindow:
    label: str
    duration: timedelta
    observation_count: int
    expected_sample_count: int
    eligible_sample_count: int
    not_monitored_sample_count: int
    covered_sample_count: int
    missing_sample_count: int
    coverage_percent: float
    healthy_percent: float
    degraded_percent: float
    unavailable_percent: float
    missing_percent: float
    not_monitored_percent: float
    incident_count: int
    last_healthy_at: datetime | None
    history_available_since: datetime | None
    last_observed_at: datetime | None
    longest_gap_samples: int
    complete: bool


@dataclass(frozen=True, slots=True)
class OperationalIncident:
    started_at: datetime
    recovered_at: datetime | None
    status: HealthStatus
    component: str
    reason: str


@dataclass(frozen=True, slots=True)
class OperationalHistory:
    windows: tuple[AvailabilityWindow, ...] = ()
    incidents: tuple[OperationalIncident, ...] = ()
    available: bool = True


@dataclass(frozen=True, slots=True)
class WebAdminSystemStatus:
    generated_at: datetime
    uptime_seconds: float
    git: GitMetadata
    postgresql: PostgreSQLState
    postgresql_health: ComponentHealth
    bot: OperationsBotState
    voice: TrackingHealth
    game: TrackingHealth
    integrity: DataIntegrityHealth
    overall_status: HealthStatus
    game_tracking_enabled: bool
    game_confirm_interval_seconds: int
    history: OperationalHistory = OperationalHistory()

    @property
    def all_systems_operational(self) -> bool:
        return self.overall_status is HealthStatus.HEALTHY


class GitMetadataSource(Protocol):
    async def load(self) -> GitMetadata: ...


class OperationsRepository(Protocol):
    async def load(self) -> PostgreSQLState: ...

    async def load_history(self, since: datetime) -> tuple[HealthObservation, ...]: ...


class SubprocessGitMetadataSource:
    """Read deployment Git metadata with bounded, failure-safe subprocesses."""

    def __init__(self, repository_path: Path | None = None) -> None:
        self._repository_path = repository_path or Path.cwd()

    async def load(self) -> GitMetadata:
        commit = await self._run("rev-parse", "--short=12", "HEAD")
        branch = await self._run("symbolic-ref", "--quiet", "--short", "HEAD")
        return GitMetadata(commit=commit, branch=branch)

    async def _run(self, *arguments: str) -> str | None:
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *arguments,
                cwd=os.fspath(self._repository_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return None
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=2)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return None
        if process.returncode != 0:
            return None
        value = stdout.decode("utf-8", errors="replace").strip()
        return value or None


class SqlAlchemyOperationsRepository:
    """Load bounded operational facts and invariant counts using SELECT only."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._monotonic = monotonic

    async def load(self) -> PostgreSQLState:
        started_at = self._monotonic()
        try:
            async with self._session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as error:
            self._warn("probe", error)
            return PostgreSQLState(available=False)
        latency = max(0.0, self._monotonic() - started_at)

        size, revision = await self._load_database_metadata()
        voice = await self._load_tracking(VoiceSession)
        game = await self._load_tracking(GameSession)
        return PostgreSQLState(
            available=True,
            latency_seconds=latency,
            database_size_bytes=size,
            alembic_revision=revision,
            voice=voice,
            game=game,
        )

    async def load_history(self, since: datetime) -> tuple[HealthObservation, ...]:
        statement = (
            select(OperationalHealthObservation)
            .where(
                OperationalHealthObservation.guild_id == self._guild_id,
                OperationalHealthObservation.observed_at >= since,
            )
            .order_by(OperationalHealthObservation.observed_at)
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).scalars()
            return tuple(
                HealthObservation(
                    observed_at=row.observed_at,
                    status=HealthStatus(row.overall_status),
                    component=row.component,
                    reason=row.reason,
                )
                for row in rows
            )

    async def _load_database_metadata(self) -> tuple[int | None, str | None]:
        statement = select(
            func.pg_database_size(func.current_database()).label("database_size"),
            literal_column("(SELECT version_num FROM alembic_version LIMIT 1)").label(
                "alembic_revision"
            ),
        )
        try:
            async with self._session_factory() as session:
                row = (await session.execute(statement)).one()
        except Exception as error:
            self._warn("metadata", error)
            return None, None
        size = int(row.database_size) if row.database_size is not None else None
        revision = (
            str(row.alembic_revision) if row.alembic_revision is not None else None
        )
        return size, revision

    async def _load_tracking(
        self, model: type[VoiceSession] | type[GameSession]
    ) -> TrackingState:
        duplicate_groups = (
            select(model.guild_id, model.user_id)
            .where(model.guild_id == self._guild_id, model.ended_at.is_(None))
            .group_by(model.guild_id, model.user_id)
            .having(func.count(model.id) > 1)
            .subquery(f"{model.__tablename__}_duplicate_open_groups")
        )
        temporal_violation = or_(
            model.started_at > model.confirmed_through_at,
            and_(
                model.ended_at.is_not(None),
                or_(
                    model.confirmed_through_at > model.ended_at,
                    model.started_at > model.ended_at,
                ),
            ),
        )
        statement = select(
            select(func.count(model.id))
            .where(model.guild_id == self._guild_id, model.ended_at.is_(None))
            .scalar_subquery()
            .label("open_sessions"),
            select(func.max(model.confirmed_through_at))
            .where(model.guild_id == self._guild_id, model.ended_at.is_(None))
            .scalar_subquery()
            .label("last_confirmed_at"),
            select(func.min(model.confirmed_through_at))
            .where(model.guild_id == self._guild_id, model.ended_at.is_(None))
            .scalar_subquery()
            .label("oldest_confirmed_at"),
            select(func.count(model.id))
            .where(
                model.guild_id == self._guild_id,
                model.ended_at.is_(None),
                model.confirmed_through_at.is_(None),
            )
            .scalar_subquery()
            .label("missing_confirmation_count"),
            select(func.count())
            .select_from(duplicate_groups)
            .scalar_subquery()
            .label("duplicate_open_session_groups"),
            select(func.count(model.id))
            .where(model.guild_id == self._guild_id, temporal_violation)
            .scalar_subquery()
            .label("temporal_violation_count"),
        )
        try:
            async with self._session_factory() as session:
                row = (await session.execute(statement)).one()
        except Exception as error:
            self._warn(model.__tablename__, error)
            return TrackingState()
        return TrackingState(
            open_sessions=int(row.open_sessions),
            last_confirmed_at=row.last_confirmed_at,
            duplicate_open_session_groups=int(row.duplicate_open_session_groups),
            temporal_violation_count=int(row.temporal_violation_count),
            oldest_confirmed_at=row.oldest_confirmed_at,
            missing_confirmation_count=int(row.missing_confirmation_count),
        )

    @staticmethod
    def _warn(metric: str, error: Exception) -> None:
        logger.warning(
            "Web admin operations PostgreSQL metric failed metric=%s error_type=%s",
            metric,
            type(error).__name__,
        )


def _reason(code: str, message: str) -> DiagnosticReason:
    return DiagnosticReason(code, message)


def _confirmation_age_seconds(value: datetime | None, now: datetime) -> int | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return max(0, int((now - value.astimezone(UTC)).total_seconds()))


def evaluate_tracking_health(
    state: TrackingState,
    *,
    now: datetime,
    stale_threshold_seconds: int,
    enabled: bool = True,
    game: bool = False,
) -> TrackingHealth:
    """Interpret one tracking snapshot without relying on presentation strings."""

    if not enabled:
        return TrackingHealth(
            state,
            ComponentHealth(
                HealthStatus.NEUTRAL,
                (_reason("feature_disabled", "Game Tracking выключен"),),
            ),
            stale_threshold_seconds,
        )
    if state.open_sessions is None:
        return TrackingHealth(
            state,
            ComponentHealth(
                HealthStatus.UNAVAILABLE,
                (_reason("source_unavailable", "Диагностика PostgreSQL недоступна"),),
            ),
            stale_threshold_seconds,
        )

    reasons: list[DiagnosticReason] = []
    degraded = False
    age = _confirmation_age_seconds(state.oldest_confirmed_at, now)
    if state.open_sessions == 0:
        reasons.append(
            _reason(
                "no_active_sessions",
                "Нет активных игр" if game else "Нет активных сессий",
            )
        )
    else:
        reasons.append(
            _reason(
                "open_sessions",
                f"Открытых {'игр' if game else 'сессий'}: {state.open_sessions}",
            )
        )
        if state.missing_confirmation_count is None:
            degraded = True
            reasons.append(
                _reason(
                    "confirmation_check_unavailable",
                    "Проверка отсутствующих checkpoint недоступна",
                )
            )
        elif state.missing_confirmation_count > 0:
            degraded = True
            reasons.append(
                _reason(
                    "confirmation_missing",
                    "Открытых сессий без checkpoint: "
                    f"{state.missing_confirmation_count}",
                )
            )
        elif age is None:
            degraded = True
            reasons.append(
                _reason(
                    "confirmation_missing",
                    "Время последнего подтверждения недоступно",
                )
            )
        elif age > stale_threshold_seconds:
            degraded = True
            reasons.extend(
                (
                    _reason(
                        "checkpoint_stale",
                        f"Checkpoint не обновлялся {_format_elapsed(age)}",
                    ),
                    _reason(
                        "stale_threshold",
                        f"Допустимый порог: {_format_elapsed(stale_threshold_seconds)}",
                    ),
                )
            )
        else:
            reasons.append(
                _reason(
                    "checkpoint_fresh",
                    f"Последнее подтверждение: {_format_elapsed(age)} назад",
                )
            )

    if state.duplicate_open_session_groups is None:
        degraded = True
        reasons.append(
            _reason("duplicate_check_unavailable", "Проверка дублей недоступна")
        )
    elif state.duplicate_open_session_groups > 0:
        degraded = True
        reasons.append(
            _reason(
                "duplicate_open_sessions",
                "Пользователей с несколькими открытыми сессиями: "
                f"{state.duplicate_open_session_groups}",
            )
        )
    if state.temporal_violation_count is None:
        degraded = True
        reasons.append(
            _reason("temporal_check_unavailable", "Проверка timestamps недоступна")
        )
    elif state.temporal_violation_count > 0:
        degraded = True
        reasons.append(
            _reason(
                "temporal_violations",
                f"Неконсистентных временных строк: {state.temporal_violation_count}",
            )
        )

    return TrackingHealth(
        state,
        ComponentHealth(
            HealthStatus.DEGRADED if degraded else HealthStatus.HEALTHY,
            tuple(reasons),
        ),
        stale_threshold_seconds,
        age,
    )


def _integrity_check(
    key: str,
    label: str,
    violations: int | None,
) -> IntegrityCheck:
    if violations is None:
        return IntegrityCheck(
            key,
            HealthStatus.UNAVAILABLE,
            f"{label} — проверка недоступна",
            None,
        )
    if violations == 0:
        return IntegrityCheck(
            key,
            HealthStatus.HEALTHY,
            f"{label} — нарушений нет",
            0,
        )
    return IntegrityCheck(
        key,
        HealthStatus.DEGRADED,
        f"{label} — обнаружено нарушений: {violations}",
        violations,
    )


def evaluate_integrity(postgresql: PostgreSQLState) -> DataIntegrityHealth:
    checks = (
        _integrity_check(
            "voice_duplicate_open_sessions",
            "Voice: дубли открытых сессий",
            postgresql.voice.duplicate_open_session_groups,
        ),
        _integrity_check(
            "game_duplicate_open_sessions",
            "Games: дубли открытых сессий",
            postgresql.game.duplicate_open_session_groups,
        ),
        _integrity_check(
            "voice_temporal_consistency",
            "Voice timestamps",
            postgresql.voice.temporal_violation_count,
        ),
        _integrity_check(
            "game_temporal_consistency",
            "Game timestamps",
            postgresql.game.temporal_violation_count,
        ),
    )
    if any(check.status is HealthStatus.UNAVAILABLE for check in checks):
        status = HealthStatus.UNAVAILABLE
    elif any(check.status is HealthStatus.DEGRADED for check in checks):
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.HEALTHY
    return DataIntegrityHealth(status, checks)


def evaluate_overall_status(
    postgresql: ComponentHealth,
    bot: ComponentHealth,
    voice: ComponentHealth,
    game: ComponentHealth,
    integrity: DataIntegrityHealth,
) -> HealthStatus:
    """Aggregate critical availability first, then warnings; neutral is ignored."""

    if postgresql.status is HealthStatus.UNAVAILABLE:
        return HealthStatus.UNAVAILABLE
    if bot.status is HealthStatus.UNAVAILABLE:
        return HealthStatus.UNAVAILABLE
    if voice.status is HealthStatus.UNAVAILABLE:
        return HealthStatus.UNAVAILABLE
    if game.status is HealthStatus.UNAVAILABLE:
        return HealthStatus.UNAVAILABLE
    if any(
        component.status is HealthStatus.DEGRADED
        for component in (postgresql, bot, voice, game)
    ) or integrity.status in {HealthStatus.DEGRADED, HealthStatus.UNAVAILABLE}:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


def build_operational_history(
    observations: tuple[HealthObservation, ...],
    *,
    now: datetime,
) -> OperationalHistory:
    """Aggregate sampled checks honestly; missing time is never counted as uptime."""

    ordered = tuple(sorted(observations, key=lambda item: item.observed_at))
    incidents: list[OperationalIncident] = []
    active: OperationalIncident | None = None
    for observation in ordered:
        if observation.status is HealthStatus.HEALTHY:
            if active is not None:
                incidents.append(
                    OperationalIncident(
                        active.started_at,
                        observation.observed_at,
                        active.status,
                        active.component,
                        active.reason,
                    )
                )
                active = None
            continue
        if active is None:
            active = OperationalIncident(
                observation.observed_at,
                None,
                observation.status,
                observation.component,
                observation.reason,
            )
        elif (
            observation.status is HealthStatus.UNAVAILABLE
            and active.status is HealthStatus.DEGRADED
        ):
            active = OperationalIncident(
                active.started_at,
                None,
                observation.status,
                observation.component,
                observation.reason,
            )
    if active is not None:
        incidents.append(active)

    windows: list[AvailabilityWindow] = []
    for label, duration in (
        ("24 часа", timedelta(hours=24)),
        ("7 дней", timedelta(days=7)),
    ):
        started_at = now - duration
        selected = tuple(
            item for item in ordered if started_at <= item.observed_at <= now
        )
        count = len(selected)
        expected_samples = int(
            duration.total_seconds() // HEALTH_OBSERVATION_INTERVAL_SECONDS
        )
        status_priority = {
            HealthStatus.HEALTHY: 0,
            HealthStatus.DEGRADED: 1,
            HealthStatus.UNAVAILABLE: 2,
        }
        slots: dict[int, HealthStatus] = {}
        for item in selected:
            elapsed = (item.observed_at - started_at).total_seconds()
            slot = min(
                int(elapsed // HEALTH_OBSERVATION_INTERVAL_SECONDS),
                expected_samples - 1,
            )
            previous = slots.get(slot)
            if (
                previous is None
                or status_priority[item.status] > status_priority[previous]
            ):
                slots[slot] = item.status
        covered_samples = len(slots)
        prewindow_evidence = any(item.observed_at < started_at for item in ordered)
        if prewindow_evidence:
            eligible_start_slot = 0
        elif slots:
            eligible_start_slot = min(slots)
        else:
            eligible_start_slot = expected_samples
        eligible_samples = expected_samples - eligible_start_slot
        not_monitored_samples = eligible_start_slot
        missing_samples = eligible_samples - covered_samples

        def percentage(status: HealthStatus) -> float:
            if eligible_samples == 0:
                return 0.0
            matching = sum(slot_status is status for slot_status in slots.values())
            return round(matching * 100 / eligible_samples, 1)

        longest_gap = 0
        current_gap = 0
        for slot in range(eligible_start_slot, expected_samples):
            if slot in slots:
                current_gap = 0
            else:
                current_gap += 1
                longest_gap = max(longest_gap, current_gap)

        history_available_since = ordered[0].observed_at if ordered else None
        windows.append(
            AvailabilityWindow(
                label=label,
                duration=duration,
                observation_count=count,
                expected_sample_count=expected_samples,
                eligible_sample_count=eligible_samples,
                not_monitored_sample_count=not_monitored_samples,
                covered_sample_count=covered_samples,
                missing_sample_count=missing_samples,
                coverage_percent=(
                    round(covered_samples * 100 / eligible_samples, 1)
                    if eligible_samples
                    else 0.0
                ),
                healthy_percent=percentage(HealthStatus.HEALTHY),
                degraded_percent=percentage(HealthStatus.DEGRADED),
                unavailable_percent=percentage(HealthStatus.UNAVAILABLE),
                missing_percent=(
                    round(missing_samples * 100 / eligible_samples, 1)
                    if eligible_samples
                    else 0.0
                ),
                not_monitored_percent=round(
                    not_monitored_samples * 100 / expected_samples, 1
                ),
                incident_count=sum(
                    incident.started_at <= now
                    and (
                        incident.recovered_at is None
                        or incident.recovered_at >= started_at
                    )
                    for incident in incidents
                ),
                last_healthy_at=next(
                    (
                        item.observed_at
                        for item in reversed(selected)
                        if item.status is HealthStatus.HEALTHY
                    ),
                    None,
                ),
                history_available_since=history_available_since,
                last_observed_at=(selected[-1].observed_at if selected else None),
                longest_gap_samples=longest_gap,
                complete=(not_monitored_samples == 0 and missing_samples == 0),
            )
        )
    return OperationalHistory(tuple(windows), tuple(reversed(incidents[-10:])))


class WebAdminSystemStatusService:
    """Combine independent sources, then evaluate structured health results."""

    def __init__(
        self,
        repository: OperationsRepository,
        *,
        bot_control: BotProfileControl,
        git_metadata: GitMetadataSource,
        game_tracking_enabled: bool,
        voice_checkpoint_interval_seconds: int,
        game_confirm_interval_seconds: int,
        process_started_monotonic: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._repository = repository
        self._bot_control = bot_control
        self._git_metadata = git_metadata
        self._game_tracking_enabled = game_tracking_enabled
        self._voice_stale_threshold_seconds = tracking_stale_threshold(
            voice_checkpoint_interval_seconds
        )
        self._game_confirm_interval_seconds = game_confirm_interval_seconds
        self._process_started_monotonic = process_started_monotonic
        self._clock = clock
        self._monotonic = monotonic

    async def load(self) -> WebAdminSystemStatus:
        generated_at = normalize_observed_at(self._clock())
        postgresql, bot, git, history = await asyncio.gather(
            self._load_postgresql(),
            self._load_bot(),
            self._load_git(),
            self._load_history(generated_at),
        )
        postgresql_health = ComponentHealth(
            HealthStatus.HEALTHY if postgresql.available else HealthStatus.UNAVAILABLE,
            (
                _reason(
                    "postgresql_available"
                    if postgresql.available
                    else "postgresql_unavailable",
                    "Read-only запрос выполнен"
                    if postgresql.available
                    else "PostgreSQL недоступен",
                ),
            ),
        )
        voice = evaluate_tracking_health(
            postgresql.voice,
            now=generated_at,
            stale_threshold_seconds=self._voice_stale_threshold_seconds,
        )
        game_threshold = tracking_stale_threshold(
            self._game_confirm_interval_seconds,
        )
        game = evaluate_tracking_health(
            postgresql.game,
            now=generated_at,
            stale_threshold_seconds=game_threshold,
            enabled=self._game_tracking_enabled,
            game=True,
        )
        integrity = evaluate_integrity(postgresql)
        overall = evaluate_overall_status(
            postgresql_health,
            bot.health,
            voice.health,
            game.health,
            integrity,
        )
        return WebAdminSystemStatus(
            generated_at=generated_at,
            uptime_seconds=max(
                0.0, self._monotonic() - self._process_started_monotonic
            ),
            git=git,
            postgresql=postgresql,
            postgresql_health=postgresql_health,
            bot=bot,
            voice=voice,
            game=game,
            integrity=integrity,
            overall_status=overall,
            game_tracking_enabled=self._game_tracking_enabled,
            game_confirm_interval_seconds=self._game_confirm_interval_seconds,
            history=history,
        )

    async def _load_history(self, now: datetime) -> OperationalHistory:
        loader = getattr(self._repository, "load_history", None)
        if loader is None:
            return build_operational_history((), now=now)
        try:
            observations = await loader(
                now - timedelta(days=OPERATIONAL_HISTORY_LOOKBACK_DAYS)
            )
        except Exception as error:
            logger.warning(
                "Web admin Operations history failed error_type=%s",
                type(error).__name__,
            )
            return OperationalHistory(available=False)
        return build_operational_history(observations, now=now)

    async def _load_postgresql(self) -> PostgreSQLState:
        try:
            return await self._repository.load()
        except Exception as error:
            logger.warning(
                "Web admin Operations repository failed error_type=%s",
                type(error).__name__,
            )
            return PostgreSQLState(available=False)

    async def _load_git(self) -> GitMetadata:
        try:
            return await self._git_metadata.load()
        except Exception as error:
            logger.warning(
                "Web admin Git metadata failed error_type=%s", type(error).__name__
            )
            return GitMetadata()

    async def _load_bot(self) -> OperationsBotState:
        try:
            await self._bot_control.get_profile()
        except BotProfileOperationError as error:
            if error.category in {
                BotProfileErrorCategory.BOT_NOT_READY,
                BotProfileErrorCategory.GUILD_UNAVAILABLE,
            }:
                return OperationsBotState(
                    OperationsBotStatus.OFFLINE,
                    OperationsControlStatus.AVAILABLE,
                    ComponentHealth(
                        HealthStatus.UNAVAILABLE,
                        (_reason("bot_offline", "Бот не готов или offline"),),
                    ),
                )
            return self._unavailable_control()
        except Exception as error:
            logger.warning(
                "Web admin Operations Bot Control failed error_type=%s",
                type(error).__name__,
            )
            return self._unavailable_control()
        return OperationsBotState(
            OperationsBotStatus.ONLINE,
            OperationsControlStatus.AVAILABLE,
            ComponentHealth(
                HealthStatus.HEALTHY,
                (_reason("bot_online", "Бот доступен через Bot Control"),),
            ),
        )

    @staticmethod
    def _unavailable_control() -> OperationsBotState:
        return OperationsBotState(
            OperationsBotStatus.UNKNOWN,
            OperationsControlStatus.UNAVAILABLE,
            ComponentHealth(
                HealthStatus.DEGRADED,
                (_reason("bot_control_unavailable", "Bot Control недоступен"),),
            ),
        )


def _unknown(value: str | None) -> str:
    return value if value else "Unknown"


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "Недоступно"
    units = ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ")
    size = float(value)
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024 or candidate == units[-1]:
            break
        size /= 1024
    return f"{int(size)} {unit}" if unit == "Б" else f"{size:.1f} {unit}"


def _format_uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)
    prefix = f"{days} д " if days else ""
    return f"{prefix}{hours:02d}:{minutes:02d}:{seconds:02d}"


def _format_datetime(value: datetime | None) -> str:
    if value is None:
        return "Нет данных"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    minutes, remaining = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} мин" if remaining == 0 else f"{minutes} мин {remaining} сек"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч" if minutes == 0 else f"{hours} ч {minutes} мин"


def _format_freshness(tracking: TrackingHealth, *, game: bool = False) -> str:
    if tracking.health.status is HealthStatus.NEUTRAL:
        return "Неактивно"
    if tracking.state.open_sessions is None:
        return "Недоступно"
    if tracking.state.open_sessions == 0:
        return "Нет активных игр" if game else "Нет активных сессий"
    if tracking.confirmation_age_seconds is None:
        return "Нет данных"
    return f"{_format_elapsed(tracking.confirmation_age_seconds)} назад"


def _metric(label: str, value: str) -> str:
    return f'<div class="metric"><span>{escape(label)}</span><strong>{escape(value)}</strong></div>'


def _status_label(status: HealthStatus) -> str:
    return {
        HealthStatus.HEALTHY: "Работает",
        HealthStatus.DEGRADED: "Ограниченная работа",
        HealthStatus.UNAVAILABLE: "Недоступно",
        HealthStatus.NEUTRAL: "Неактивно",
    }[status]


def _reasons(health: ComponentHealth) -> str:
    if not health.reasons:
        return ""
    items = "".join(f"<li>{escape(reason.message)}</li>" for reason in health.reasons)
    return (
        '<div class="diagnostics"><span class="diagnostics-label">Диагностика</span>'
        f'<ul class="reasons">{items}</ul></div>'
    )


def _card_header(title: str, health: ComponentHealth) -> str:
    return (
        f"<header><h2>{escape(title)}</h2>"
        f'<span class="badge {health.status.value}">{_status_label(health.status)}</span></header>'
    )


def _integrity_content(integrity: DataIntegrityHealth) -> str:
    symbol = {
        HealthStatus.HEALTHY: "✓",
        HealthStatus.DEGRADED: "⚠",
        HealthStatus.UNAVAILABLE: "?",
        HealthStatus.NEUTRAL: "–",
    }
    checks = "".join(
        f'<li class="{check.status.value}"><strong>{symbol[check.status]}</strong> '
        f"{escape(check.message)}</li>"
        for check in integrity.checks
    )
    return (
        f'<ul class="integrity-list">{checks}</ul>'
        f'<p class="muted">{integrity.passed_count} / {len(integrity.checks)} '
        "проверок пройдено</p>"
    )


def _availability_content(history: OperationalHistory) -> str:
    if not history.available:
        return '<p class="muted">История временно недоступна.</p>'
    if not history.windows:
        return '<p class="muted">Наблюдения ещё не накоплены.</p>'
    cards = ""
    for window in history.windows:
        coverage = "Полное окно" if window.complete else "Частичное окно"
        partial_reasons = []
        if window.not_monitored_sample_count:
            partial_reasons.append("мониторинг начался позже выбранного окна")
        if window.missing_sample_count:
            partial_reasons.append("есть пропущенные наблюдения")
        completeness_reason = (
            "Полнота окна подтверждена: наблюдения покрывают весь выбранный период."
            if window.complete
            else f"Причина: {'; '.join(partial_reasons) or 'наблюдения покрывают окно не полностью'}."
        )
        note_class = " complete" if window.complete else ""
        cards += f"""<article class="availability-window"><header><h3>{escape(window.label)}</h3><span class="badge {"success" if window.complete else "warning"}">{escape(coverage)}</span></header>
<div class="availability-primary">{_metric("Healthy", f"{window.healthy_percent:.1f}%")}
{_metric("Ограниченная работа", f"{window.degraded_percent:.1f}%")}
{_metric("Недоступно", f"{window.unavailable_percent:.1f}%")}
{_metric("Missing", f"{window.missing_percent:.1f}%")}
{_metric("Coverage с начала мониторинга", f"{window.coverage_percent:.1f}%")}
{_metric("Not monitored до старта", f"{window.not_monitored_percent:.1f}%")}
{_metric("Инцидентов", str(window.incident_count))}</div>
<p class="availability-note{note_class}"><strong>{escape(coverage)}.</strong> {escape(completeness_reason)}</p>
<details class="availability-details"><summary>Подробности окна мониторинга</summary><dl class="detail-metadata">
<div><dt>Наблюдений</dt><dd>{window.observation_count}</dd></div>
<div><dt>Минут в выбранном окне</dt><dd>{window.expected_sample_count}</dd></div>
<div><dt>Минут под мониторингом</dt><dd>{window.eligible_sample_count}</dd></div>
<div><dt>Получено наблюдений</dt><dd>{window.covered_sample_count}</dd></div>
<div><dt>Пропущено наблюдений</dt><dd>{window.missing_sample_count}</dd></div>
<div><dt>Минут до начала мониторинга</dt><dd>{window.not_monitored_sample_count}</dd></div>
<div><dt>Максимальный разрыв</dt><dd>{window.longest_gap_samples} мин</dd></div>
<div><dt>Последнее успешное наблюдение</dt><dd>{_format_datetime(window.last_healthy_at)}</dd></div>
<div><dt>История доступна с</dt><dd>{_format_datetime(window.history_available_since)}</dd></div>
<div><dt>Последнее наблюдение</dt><dd>{_format_datetime(window.last_observed_at)}</dd></div>
</dl><p class="muted">Healthy, Degraded, Unavailable, Missing и Coverage рассчитаны для части окна, где мониторинг уже должен был работать. Not monitored показывает часть выбранного окна до начала мониторинга. Missing и Not monitored не считаются uptime.</p></details></article>"""
    return f'<div class="availability-grid">{cards}</div>'


def _incidents_content(history: OperationalHistory) -> str:
    if not history.available:
        return '<p class="muted">История инцидентов временно недоступна.</p>'
    if not history.incidents:
        return '<p class="calm-empty">Недавних инцидентов нет.</p>'
    rows = ""
    for incident in history.incidents:
        recovered = (
            _format_datetime(incident.recovered_at)
            if incident.recovered_at is not None
            else "Продолжается"
        )
        rows += (
            f'<li class="incident {incident.status.value}"><strong>'
            f"{escape(incident.component)} — {_status_label(incident.status)}</strong>"
            f"<span>{escape(incident.reason)}</span>"
            f"<small>{_format_datetime(incident.started_at)} → {recovered}</small></li>"
        )
    return f'<ul class="incident-list">{rows}</ul>'


def render_system_status_page(
    status: WebAdminSystemStatus,
    *,
    csrf_token: str,
    role: WebAdminRole,
) -> str:
    """Render already-evaluated health DTOs without business rules in HTML."""

    overall_text = {
        HealthStatus.HEALTHY: "Все системы работают",
        HealthStatus.DEGRADED: "Есть предупреждения",
        HealthStatus.UNAVAILABLE: "Обнаружены проблемы",
        HealthStatus.NEUTRAL: "Состояние неизвестно",
    }[status.overall_status]
    postgres = status.postgresql
    bot_text = {
        OperationsBotStatus.ONLINE: "Онлайн",
        OperationsBotStatus.OFFLINE: "Офлайн",
        OperationsBotStatus.UNKNOWN: "Неизвестно",
    }[status.bot.status]
    control_text = {
        OperationsControlStatus.AVAILABLE: "Доступен",
        OperationsControlStatus.UNAVAILABLE: "Недоступен",
    }[status.bot.control_status]
    latency = (
        f"{postgres.latency_seconds * 1000:.1f} мс"
        if postgres.latency_seconds is not None
        else "Недоступно"
    )
    generated = status.generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    voice_state = status.voice.state
    game_state = status.game.state
    game_feature = "Включено" if status.game_tracking_enabled else "Отключено"
    body = f"""<section class="overall operations-overall {status.overall_status.value}" aria-labelledby="overall-health-title"><div class="overall-copy"><strong id="overall-health-title">{overall_text}</strong><span class="muted">Последняя проверка: {generated}</span></div><span class="badge {status.overall_status.value}">{_status_label(status.overall_status)}</span></section>
<section aria-labelledby="components-title"><div class="section-heading"><h2 id="components-title">Основные компоненты</h2><p>Текущее состояние и ключевые сигналы</p></div><div class="component-grid">
<article class="card component-card {status.bot.health.status.value}">{_card_header("Discord / Bot", status.bot.health)}<div class="metrics">
{_metric("Bot", bot_text)}{_metric("Bot Control", control_text)}</div>{_reasons(status.bot.health)}</article>
<article class="card component-card {status.postgresql_health.status.value}">{_card_header("PostgreSQL", status.postgresql_health)}<div class="metrics">
{_metric("Статус", "Работает" if postgres.available else "Недоступно")}{_metric("Задержка", latency)}
{_metric("Размер БД", _format_bytes(postgres.database_size_bytes))}{_metric("Alembic revision", _unknown(postgres.alembic_revision))}
</div>{_reasons(status.postgresql_health)}</article>
<article class="card component-card {status.voice.health.status.value}">{_card_header("Voice tracking", status.voice.health)}<div class="metrics">
{_metric("Открытых сессий", str(voice_state.open_sessions) if voice_state.open_sessions is not None else "Недоступно")}
{_metric("Последнее подтверждение", _format_datetime(voice_state.last_confirmed_at))}{_metric("Давность данных", _format_freshness(status.voice))}
</div>{_reasons(status.voice.health)}</article>
<article class="card component-card {status.game.health.status.value}">{_card_header("Game tracking", status.game.health)}<div class="metrics">
{_metric("Статус", game_feature)}{_metric("Интервал подтверждения", f"{status.game_confirm_interval_seconds} сек")}
{_metric("Открытых сессий", str(game_state.open_sessions) if game_state.open_sessions is not None else "Недоступно")}
{_metric("Последнее подтверждение", _format_datetime(game_state.last_confirmed_at))}{_metric("Давность данных", _format_freshness(status.game, game=True))}
</div>{_reasons(status.game.health)}</article></div></section>
<section class="technical-strip" aria-labelledby="production-metadata-title"><h2 id="production-metadata-title">Production metadata</h2><dl class="metadata-list">
<div><dt>Git-коммит</dt><dd class="technical">{escape(_unknown(status.git.commit))}</dd></div><div><dt>Ветка</dt><dd class="technical">{escape(_unknown(status.git.branch))}</dd></div>
<div><dt>Alembic revision</dt><dd class="technical">{escape(_unknown(postgres.alembic_revision))}</dd></div><div><dt>Аптайм Web Admin</dt><dd>{escape(_format_uptime(status.uptime_seconds))}</dd></div>
</dl></section>
<section class="history-section"><div class="section-heading"><h2>Доступность 24h / 7d</h2><p>Healthy, degraded, unavailable, missing и not monitored учитываются отдельно</p></div>{_availability_content(status.history)}</section>
<section class="history-section"><div class="section-heading"><h2>Последние инциденты</h2><p>Операционная хронология</p></div>{_incidents_content(status.history)}</section>
<section class="integrity {status.integrity.status.value}">{_card_header("Целостность данных", ComponentHealth(status.integrity.status))}
{_integrity_content(status.integrity)}</section>"""
    return render_admin_page(
        "Состояние",
        body,
        role=role,
        csrf_token=csrf_token,
        active_path="/admin/system",
        description="Диагностика и доступность компонентов Kanami",
        wide=True,
        kicker="Operations",
    )
