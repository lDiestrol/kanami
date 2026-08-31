"""Application service for normalized audit event persistence."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from discord_stats_bot.features.audit_logging.retention import calculate_expires_at
from discord_stats_bot.features.audit_logging.types import (
    AuditEventDraft,
    AuditEventRecord,
    VoiceAuditTransitionTiming,
)
from discord_stats_bot.features.voice_statistics import VoiceStatisticsQuery
from discord_stats_bot.features.voice_statistics.service import (
    build_voice_statistics_query,
)


class AuditEventRepository(Protocol):
    """Caller-owned transaction contract used by audit application services."""

    async def create(
        self, draft: AuditEventDraft, *, expires_at: datetime | None
    ) -> AuditEventRecord: ...

    async def create_many(
        self,
        events: Sequence[tuple[AuditEventDraft, datetime | None]],
    ) -> tuple[AuditEventRecord, ...]: ...


class AuditLoggingService:
    """Calculate retention and persist events without transaction ownership."""

    def __init__(
        self,
        repository: AuditEventRepository,
        *,
        transient_retention_days: int = 90,
    ) -> None:
        if transient_retention_days <= 0:
            raise ValueError("transient_retention_days must be positive")
        self._repository = repository
        self._transient_retention_days = transient_retention_days

    async def create(self, draft: AuditEventDraft) -> AuditEventRecord:
        expires_at = self._expires_at(draft)
        return await self._repository.create(draft, expires_at=expires_at)

    async def create_many(
        self, drafts: Sequence[AuditEventDraft]
    ) -> tuple[AuditEventRecord, ...]:
        events = tuple((draft, self._expires_at(draft)) for draft in drafts)
        if not events:
            return ()
        return await self._repository.create_many(events)

    def _expires_at(self, draft: AuditEventDraft) -> datetime | None:
        assert draft.retention_policy is not None
        return calculate_expires_at(
            draft.occurred_at,
            draft.retention_policy,
            transient_retention_days=self._transient_retention_days,
        )


class VoiceAuditEnrichmentRepository(Protocol):
    """Read voice source-of-truth values for an immutable audit snapshot."""

    async def get_transition_timing(
        self,
        *,
        guild_id: int,
        user_id: int,
        previous_channel_id: int,
        occurred_at: datetime,
    ) -> VoiceAuditTransitionTiming | None: ...

    async def get_today_total_seconds(
        self,
        *,
        guild_id: int,
        user_id: int,
        query: VoiceStatisticsQuery,
    ) -> int: ...


class VoiceAuditEnrichmentService:
    """Snapshot tracker-derived voice values before durable audit delivery."""

    def __init__(
        self,
        repository: VoiceAuditEnrichmentRepository,
        *,
        report_timezone: ZoneInfo,
        min_session_seconds: int,
    ) -> None:
        if min_session_seconds <= 0:
            raise ValueError("min_session_seconds must be positive")
        self._repository = repository
        self._report_timezone = report_timezone
        self._min_session_seconds = min_session_seconds

    async def get_details(
        self,
        *,
        event_type: str,
        guild_id: int,
        user_id: int,
        previous_channel_id: int,
        occurred_at: datetime,
    ) -> dict[str, object]:
        if event_type not in {"voice.left", "voice.moved"}:
            return {}
        timing = await self._repository.get_transition_timing(
            guild_id=guild_id,
            user_id=user_id,
            previous_channel_id=previous_channel_id,
            occurred_at=occurred_at,
        )
        if timing is None:
            return {}

        details: dict[str, object] = {
            "session_started_at": timing.session_started_at.isoformat()
        }
        eligible = timing.counted_exact_session_seconds >= self._min_session_seconds
        if eligible and not timing.previous_interval_is_afk:
            if timing.previous_interval_seconds > 0:
                details["previous_interval_seconds"] = timing.previous_interval_seconds
            if event_type == "voice.moved" and timing.current_session_seconds > 0:
                details["current_session_seconds"] = timing.current_session_seconds

        if event_type == "voice.left":
            query = build_voice_statistics_query(
                occurred_at,
                report_timezone=self._report_timezone,
                min_session_seconds=self._min_session_seconds,
            )
            today_total = await self._repository.get_today_total_seconds(
                guild_id=guild_id,
                user_id=user_id,
                query=query,
            )
            if today_total > 0:
                details["today_total_seconds"] = today_total
        return details
