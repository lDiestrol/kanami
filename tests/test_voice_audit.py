from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.audit_logging import (
    VoiceAuditEnrichmentService,
    VoiceAuditTransitionTiming,
)
from discord_stats_bot.persistence.repositories.voice_audit import (
    voice_audit_transition_timing_statement,
)

OCCURRED_AT = datetime(2026, 8, 14, 0, 30, tzinfo=UTC)


class FakeVoiceAuditRepository:
    def __init__(
        self,
        timing: VoiceAuditTransitionTiming | None,
        *,
        today_total_seconds: int = 0,
    ) -> None:
        self.timing = timing
        self.today_total_seconds = today_total_seconds
        self.queries = []

    async def get_transition_timing(self, **kwargs):  # type: ignore[no-untyped-def]
        self.transition_kwargs = kwargs
        return self.timing

    async def get_today_total_seconds(self, **kwargs):  # type: ignore[no-untyped-def]
        self.queries.append(kwargs["query"])
        return self.today_total_seconds


def timing(*, counted_exact_seconds: int = 7200) -> VoiceAuditTransitionTiming:
    return VoiceAuditTransitionTiming(
        session_started_at=datetime(2026, 8, 13, 22, 24, tzinfo=UTC),
        previous_interval_seconds=48 * 60,
        current_session_seconds=2 * 3600 + 6 * 60,
        counted_exact_session_seconds=counted_exact_seconds,
        previous_interval_is_afk=False,
    )


@pytest.mark.asyncio
async def test_leave_snapshot_uses_report_timezone_and_statistics_threshold() -> None:
    repository = FakeVoiceAuditRepository(
        timing(), today_total_seconds=3 * 3600 + 12 * 60
    )
    service = VoiceAuditEnrichmentService(
        repository,  # type: ignore[arg-type]
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
        min_session_seconds=60,
    )

    details = await service.get_details(
        event_type="voice.left",
        guild_id=10,
        user_id=20,
        previous_channel_id=30,
        occurred_at=OCCURRED_AT,
    )

    assert details["previous_interval_seconds"] == 48 * 60
    assert details["today_total_seconds"] == 3 * 3600 + 12 * 60
    assert repository.queries[0].today_started_at == datetime(
        2026, 8, 13, 19, tzinfo=UTC
    )


@pytest.mark.asyncio
async def test_short_leave_omits_interval_but_keeps_prior_today_total() -> None:
    repository = FakeVoiceAuditRepository(
        timing(counted_exact_seconds=59), today_total_seconds=3600
    )
    service = VoiceAuditEnrichmentService(
        repository,  # type: ignore[arg-type]
        report_timezone=ZoneInfo("UTC"),
        min_session_seconds=60,
    )

    details = await service.get_details(
        event_type="voice.left",
        guild_id=10,
        user_id=20,
        previous_channel_id=30,
        occurred_at=OCCURRED_AT,
    )

    assert "previous_interval_seconds" not in details
    assert details["today_total_seconds"] == 3600


@pytest.mark.asyncio
async def test_move_snapshot_keeps_previous_interval_and_logical_session() -> None:
    repository = FakeVoiceAuditRepository(timing())
    service = VoiceAuditEnrichmentService(
        repository,  # type: ignore[arg-type]
        report_timezone=ZoneInfo("UTC"),
        min_session_seconds=60,
    )

    details = await service.get_details(
        event_type="voice.moved",
        guild_id=10,
        user_id=20,
        previous_channel_id=30,
        occurred_at=OCCURRED_AT,
    )

    assert details["previous_interval_seconds"] == 48 * 60
    assert details["current_session_seconds"] == 2 * 3600 + 6 * 60
    assert repository.queries == []


def test_timing_query_targets_tracker_interval_closed_at_event_boundary() -> None:
    statement = voice_audit_transition_timing_statement(
        guild_id=10,
        user_id=20,
        previous_channel_id=30,
        occurred_at=OCCURRED_AT,
    )
    sql = " ".join(
        str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        ).split()
    )

    assert "audit_previous_interval.channel_id = 30" in sql
    assert "audit_previous_interval.ended_at =" in sql
    assert "audit_session_segment.is_afk IS false" in sql
    assert "audit_session_segment.quality = 'exact'" in sql
