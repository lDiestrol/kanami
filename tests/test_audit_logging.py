from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import discord
import pytest
from sqlalchemy import BigInteger, DateTime, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB

from discord_stats_bot.discord.audit_logging import (
    AUDIT_CHANNEL_POSITION_BATCH_WINDOW_SECONDS,
    AUDIT_RETRY_DELAYS_SECONDS,
    AuditEventIngestor,
    AuditLogDeliveryRunner,
    AuditRetentionRunner,
    build_audit_embed,
)
from discord_stats_bot.features.audit_logging import (
    SUPPORTED_EVENT_TYPES,
    AuditEventDraft,
    AuditEventRecord,
    AuditLoggingService,
    AuditRetentionPolicy,
    VoiceAuditTransitionTiming,
    calculate_expires_at,
)
from discord_stats_bot.features.server_settings import (
    GuildServerSettingsBaselines,
    resolve_guild_server_settings,
)
from discord_stats_bot.features.voice import VoiceTransitionResult
from discord_stats_bot.persistence.models import AuditEvent

T0 = datetime(2026, 8, 14, 12, tzinfo=UTC)


def draft(event_type: str = "voice.joined", **overrides: object) -> AuditEventDraft:
    values = {
        "guild_id": 10,
        "category": "voice" if event_type.startswith("voice.") else "member",
        "event_type": event_type,
        "occurred_at": T0,
        "subject_type": "user",
        "subject_id": 20,
    }
    values.update(overrides)
    return AuditEventDraft(**values)  # type: ignore[arg-type]


def record(**overrides: object) -> AuditEventRecord:
    values = {
        "id": 1,
        "guild_id": 10,
        "category": "voice",
        "event_type": "voice.joined",
        "occurred_at": T0,
        "created_at": T0,
        "subject_type": "user",
        "subject_id": 20,
        "actor_user_id": None,
        "channel_id": 30,
        "before_data": {},
        "after_data": {"channel_id": 30, "channel_name": "Общий"},
        "details_data": {},
        "discord_message_id": None,
        "delivered_at": None,
        "delivery_attempts": 0,
        "next_delivery_attempt_at": None,
        "last_delivery_error": None,
        "expires_at": T0 + timedelta(days=90),
    }
    values.update(overrides)
    return AuditEventRecord(**values)  # type: ignore[arg-type]


def test_audit_draft_validates_and_normalizes_retention() -> None:
    transient = draft()
    important = draft(
        "member.joined", category="member", after_data={"joined_at": T0.isoformat()}
    )

    assert transient.retention_policy is AuditRetentionPolicy.TRANSIENT
    assert important.retention_policy is AuditRetentionPolicy.IMPORTANT
    assert transient.occurred_at.tzinfo is UTC


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"guild_id": 0}, "guild_id"),
        ({"occurred_at": datetime(2026, 1, 1)}, "timezone-aware"),
        ({"category": "messages"}, "category"),
        ({"event_type": "message.created"}, "event_type"),
    ],
)
def test_audit_draft_rejects_invalid_values(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        draft(**overrides)


def test_retention_semantics() -> None:
    assert calculate_expires_at(
        T0, AuditRetentionPolicy.TRANSIENT, transient_retention_days=90
    ) == T0 + timedelta(days=90)
    assert (
        calculate_expires_at(
            T0, AuditRetentionPolicy.IMPORTANT, transient_retention_days=90
        )
        is None
    )


class RecordingCreateRepository:
    def __init__(self) -> None:
        self.events: tuple[tuple[AuditEventDraft, datetime | None], ...] = ()

    async def create_many(self, events):  # type: ignore[no-untyped-def]
        self.events = tuple(events)
        return ()


@pytest.mark.asyncio
async def test_service_applies_retention_to_create_many() -> None:
    repository = RecordingCreateRepository()
    service = AuditLoggingService(repository, transient_retention_days=90)  # type: ignore[arg-type]

    await service.create_many(
        (
            draft(),
            draft("member.joined", category="member"),
        )
    )

    assert repository.events[0][1] == T0 + timedelta(days=90)
    assert repository.events[1][1] is None


def test_audit_model_has_approved_shape_and_indexes() -> None:
    table = AuditEvent.__table__
    assert set(table.columns.keys()) == {
        "id",
        "guild_id",
        "category",
        "event_type",
        "occurred_at",
        "created_at",
        "subject_type",
        "subject_id",
        "actor_user_id",
        "channel_id",
        "before_data",
        "after_data",
        "details_data",
        "discord_message_id",
        "delivered_at",
        "delivery_attempts",
        "next_delivery_attempt_at",
        "last_delivery_error",
        "expires_at",
    }
    assert isinstance(table.c.id.type, BigInteger)
    assert isinstance(table.c.guild_id.type, BigInteger)
    assert isinstance(table.c.category.type, Text)
    assert isinstance(table.c.delivery_attempts.type, Integer)
    assert isinstance(table.c.occurred_at.type, DateTime)
    assert table.c.occurred_at.type.timezone is True
    assert all(
        isinstance(table.c[name].type, JSONB)
        for name in ("before_data", "after_data", "details_data")
    )
    assert {index.name for index in table.indexes} == {
        "ix_audit_events_guild_occurred_at",
        "ix_audit_events_guild_event_type_occurred_at",
        "ix_audit_events_pending_delivery",
        "ix_audit_events_expires_at",
        "uq_audit_events_member_anniversary",
        "uq_audit_events_member_returned",
    }
    pending_index = next(
        index
        for index in table.indexes
        if index.name == "ix_audit_events_pending_delivery"
    )
    assert tuple(column.name for column in pending_index.columns) == (
        "guild_id",
        "next_delivery_attempt_at",
        "occurred_at",
        "id",
    )
    anniversary_index = next(
        index
        for index in table.indexes
        if index.name == "uq_audit_events_member_anniversary"
    )
    assert anniversary_index.unique is True
    assert tuple(column.name for column in anniversary_index.columns) == (
        "guild_id",
        "subject_id",
        "occurred_at",
    )
    assert str(anniversary_index.dialect_options["postgresql"]["where"]) == (
        "event_type = 'member.anniversary'"
    )
    return_index = next(
        index
        for index in table.indexes
        if index.name == "uq_audit_events_member_returned"
    )
    assert return_index.unique is True
    assert tuple(column.name for column in return_index.columns) == (
        "guild_id",
        "subject_id",
        "occurred_at",
    )
    assert str(return_index.dialect_options["postgresql"]["where"]) == (
        "event_type = 'member.returned'"
    )
    assert not table.foreign_keys


class FakeTransaction:
    def __init__(self, session: object, calls: list[str]) -> None:
        self.session = session
        self.calls = calls

    async def __aenter__(self) -> object:
        self.calls.append("begin")
        return self.session

    async def __aexit__(self, *args: object) -> None:
        self.calls.append("commit")


class FakeSessionFactory:
    def __init__(self) -> None:
        self.session = object()
        self.calls: list[str] = []

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self.session, self.calls)

    def __call__(self) -> FakeTransaction:
        return FakeTransaction(self.session, self.calls)


class RecordingAuditRepository(RecordingCreateRepository):
    def __init__(self, pending: tuple[AuditEventRecord, ...] = ()) -> None:
        super().__init__()
        self.pending = pending
        self.delivered: list[tuple[int, int, datetime]] = []
        self.failed: list[tuple[int, str, datetime]] = []
        self.deleted_as_of: list[datetime] = []
        self.requested_guild_ids: list[int] = []
        self.suppressed: list[tuple[tuple[int, ...], datetime]] = []
        self.requested_event_types: list[tuple[str, ...] | None] = []

    async def get_pending_delivery(
        self,
        *,
        guild_id: int,
        as_of: datetime,
        limit: int,
        event_types: tuple[str, ...] | None = None,
    ):
        del as_of
        self.requested_guild_ids.append(guild_id)
        self.requested_event_types.append(event_types)
        return tuple(
            item
            for item in self.pending
            if item.guild_id == guild_id
            and (event_types is None or item.event_type in event_types)
        )[:limit]

    async def mark_delivered(
        self, event_id: int, message_id: int, delivered_at: datetime
    ) -> None:
        self.delivered.append((event_id, message_id, delivered_at))

    async def mark_delivery_suppressed(
        self, event_ids: tuple[int, ...], suppressed_at: datetime
    ) -> None:
        self.suppressed.append((event_ids, suppressed_at))

    async def mark_delivered_many(
        self, event_ids: tuple[int, ...], message_id: int, delivered_at: datetime
    ) -> None:
        self.delivered.extend(
            (event_id, message_id, delivered_at) for event_id in event_ids
        )

    async def mark_delivery_failed(
        self, event_id: int, error: str, next_attempt_at: datetime
    ) -> None:
        self.failed.append((event_id, error, next_attempt_at))

    async def mark_delivery_failed_many(
        self, event_ids: tuple[int, ...], error: str, next_attempt_at: datetime
    ) -> None:
        self.failed.extend((event_id, error, next_attempt_at) for event_id in event_ids)

    async def delete_expired(self, *, as_of: datetime) -> int:
        self.deleted_as_of.append(as_of)
        return 2


@pytest.mark.asyncio
async def test_ingestion_commits_before_delivery_wakeup() -> None:
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository()
    calls = sessions.calls
    ingestor = AuditEventIngestor(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        wake_delivery=lambda: calls.append("wake"),
        repository_factory=lambda session: repository,
    )
    guild = SimpleNamespace(id=10)
    member = SimpleNamespace(
        id=20,
        guild=guild,
        joined_at=T0,
        display_name="Kanami",
    )

    assert await ingestor.member_joined(member, T0) == 1  # type: ignore[arg-type]

    assert calls == ["begin", "commit", "wake"]
    saved, expires_at = repository.events[0]
    assert saved.event_type == "member.joined"
    assert expires_at is None


class FakeChannel:
    def __init__(self, *, fail: Exception | None = None) -> None:
        self.guild = SimpleNamespace(id=10)
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.fail:
            raise self.fail
        return SimpleNamespace(id=999)


class FakeClient:
    def __init__(self, channel: FakeChannel | None) -> None:
        self.channel = channel

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        assert channel_id == 40
        return self.channel


class MutableSettingsProvider:
    def __init__(self, **baselines: int | None) -> None:
        self.set(**baselines)

    def set(self, **baselines: int | None) -> None:
        self.settings = resolve_guild_server_settings(
            10,
            GuildServerSettingsBaselines(**baselines),
            None,
        )

    async def get(self):  # type: ignore[no-untyped-def]
        return self.settings


@pytest.mark.asyncio
async def test_audit_ingestion_reads_effective_setting_without_restart() -> None:
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository()
    provider = MutableSettingsProvider()
    ingestor = AuditEventIngestor(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        repository_factory=lambda session: repository,
        settings_provider=provider,
    )
    member = SimpleNamespace(
        id=20,
        guild=SimpleNamespace(id=10),
        joined_at=T0,
        display_name="Member",
    )

    assert await ingestor.member_joined(member, T0) == 0  # type: ignore[arg-type]
    provider.set(audit_log_channel_id=40)
    assert await ingestor.member_joined(member, T0) == 1  # type: ignore[arg-type]

    assert repository.events[0][0].event_type == "member.joined"


@pytest.mark.asyncio
async def test_audit_delivery_reads_new_effective_channel_without_restart() -> None:
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository((record(),))
    provider = MutableSettingsProvider(audit_log_channel_id=40)
    first = FakeChannel()
    second = FakeChannel()

    class MultiChannelClient:
        def get_channel(self, channel_id: int) -> FakeChannel | None:
            return {40: first, 41: second}.get(channel_id)

    runner = AuditLogDeliveryRunner(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        repository_factory=lambda session: repository,
        clock=lambda: T0,
        settings_provider=provider,
    )

    assert await runner.run_once(MultiChannelClient()) == 1  # type: ignore[arg-type]
    provider.set(audit_log_channel_id=41)
    assert await runner.run_once(MultiChannelClient()) == 1  # type: ignore[arg-type]

    assert len(first.calls) == 1
    assert len(second.calls) == 1


@pytest.mark.asyncio
async def test_delivery_success_marks_record_and_disables_mentions() -> None:
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository((record(),))
    channel = FakeChannel()
    runner = AuditLogDeliveryRunner(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
        repository_factory=lambda session: repository,
        clock=lambda: T0,
    )

    assert await runner.run_once(FakeClient(channel)) == 1  # type: ignore[arg-type]

    assert repository.delivered == [(1, 999, T0)]
    allowed = channel.calls[0]["allowed_mentions"]
    assert isinstance(allowed, discord.AllowedMentions)
    assert allowed.everyone is False
    assert allowed.users is False
    assert allowed.roles is False


@pytest.mark.asyncio
async def test_delivery_preserves_repository_oldest_first_order_and_batch_limit() -> (
    None
):
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository(
        (
            record(id=1, occurred_at=T0),
            record(id=2, occurred_at=T0 + timedelta(seconds=1)),
            record(id=3, occurred_at=T0 + timedelta(seconds=2)),
        )
    )
    channel = FakeChannel()
    runner = AuditLogDeliveryRunner(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
        repository_factory=lambda session: repository,
        clock=lambda: T0,
        batch_size=2,
    )

    assert await runner.run_once(FakeClient(channel)) == 2  # type: ignore[arg-type]

    footers = [call["embed"].footer.text for call in channel.calls]
    assert "Event ID: 1" in footers[0]
    assert "Event ID: 2" in footers[1]
    assert all("Event ID: 3" not in footer for footer in footers)


@pytest.mark.asyncio
async def test_delivery_never_sends_pending_event_from_another_guild() -> None:
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository(
        (
            record(id=1, guild_id=10),
            record(id=2, guild_id=20),
        )
    )
    channel = FakeChannel()
    runner = AuditLogDeliveryRunner(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
        repository_factory=lambda session: repository,
        clock=lambda: T0,
    )

    assert await runner.run_once(FakeClient(channel)) == 1  # type: ignore[arg-type]

    assert repository.requested_guild_ids == [10]
    assert [item[0] for item in repository.delivered] == [1]
    assert len(channel.calls) == 1
    assert "Event ID: 1" in channel.calls[0]["embed"].footer.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel", [None, FakeChannel(fail=discord.Forbidden(AsyncMock(), "no"))]
)
async def test_delivery_failure_keeps_pending_and_schedules_retry(
    channel: FakeChannel | None,
) -> None:
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository((record(delivery_attempts=1),))
    runner = AuditLogDeliveryRunner(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
        repository_factory=lambda session: repository,
        clock=lambda: T0,
    )

    assert await runner.run_once(FakeClient(channel)) == 0  # type: ignore[arg-type]

    assert repository.delivered == []
    assert repository.failed[0][0] == 1
    assert repository.failed[0][2] == T0 + timedelta(
        seconds=AUDIT_RETRY_DELAYS_SECONDS[1]
    )


@pytest.mark.asyncio
async def test_retry_can_deliver_on_later_runner_cycle() -> None:
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository((record(delivery_attempts=1),))
    runner = AuditLogDeliveryRunner(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
        repository_factory=lambda session: repository,
        clock=lambda: T0,
    )
    await runner.run_once(FakeClient(None))  # type: ignore[arg-type]
    repository.pending = (record(delivery_attempts=2),)

    assert await runner.run_once(FakeClient(FakeChannel())) == 1  # type: ignore[arg-type]
    assert repository.failed and repository.delivered


@pytest.mark.asyncio
async def test_voice_delivery_retry_keeps_saved_enrichment_unchanged() -> None:
    event = record(
        event_type="voice.left",
        before_data={"channel_id": 30, "channel_name": "Общий"},
        after_data={},
        details_data={
            "previous_interval_seconds": 48 * 60,
            "today_total_seconds": 3 * 3600,
        },
    )
    repository = RecordingAuditRepository((event,))
    runner = AuditLogDeliveryRunner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
        repository_factory=lambda session: repository,
        clock=lambda: T0,
    )
    failed_channel = FakeChannel(fail=RuntimeError("temporary"))

    assert await runner.run_once(FakeClient(failed_channel)) == 0  # type: ignore[arg-type]
    assert repository.delivered == []
    assert repository.failed[0][0] == event.id

    repository.pending = (event,)
    success_channel = FakeChannel()
    assert await runner.run_once(FakeClient(success_channel)) == 1  # type: ignore[arg-type]
    assert (
        failed_channel.calls[0]["embed"].to_dict()
        == success_channel.calls[0]["embed"].to_dict()
    )
    assert repository.delivered[0][0] == event.id


@pytest.mark.asyncio
async def test_retention_runner_deletes_only_through_repository() -> None:
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository()
    runner = AuditRetentionRunner(
        sessions,  # type: ignore[arg-type]
        repository_factory=lambda session: repository,
        clock=lambda: T0,
    )

    assert await runner.run_once() == 2
    assert repository.deleted_as_of == [T0]


def test_persisted_event_renders_without_discord_cache() -> None:
    embed = build_audit_embed(
        record(
            event_type="channel.deleted",
            category="server",
            subject_type="channel",
            subject_id=30,
            before_data={"name": "Удалённый", "channel_type": "voice"},
            after_data={},
        )
    )

    assert embed.title == "Канал удалён"
    assert embed.description == "<#30>"
    assert embed.footer.text == "Event ID: 1"
    assert {field.name: field.value for field in embed.fields}["Тип"] == "voice"


def test_voice_join_embed_uses_mentions_saved_count_and_discord_timestamp() -> None:
    embed = build_audit_embed(
        record(details_data={"channel_member_count": 3}),
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
    )

    assert embed.title == "Участник вошёл в голосовой канал"
    assert embed.description == "<@20> → <#30>"
    assert {field.name: field.value for field in embed.fields} == {
        "В канале сейчас": "**3 человек**"
    }
    visible = str(embed.to_dict())
    assert "Guild ID" not in visible
    assert "Channel ID" not in visible
    assert embed.footer.text == "Event ID: 1"
    assert embed.timestamp == T0
    assert "Сегодня, в" not in (embed.footer.text or "")


def test_voice_leave_embed_uses_immutable_tracker_snapshot() -> None:
    details = {
        "previous_interval_seconds": 1 * 3600 + 37 * 60,
        "today_total_seconds": 3 * 3600 + 12 * 60,
        "session_started_at": "2026-08-14T10:46:00+00:00",
    }
    event = record(
        event_type="voice.left",
        before_data={"channel_id": 30, "channel_name": "Общий"},
        after_data={},
        details_data=details,
    )

    first = build_audit_embed(event, report_timezone=ZoneInfo("UTC"))
    second = build_audit_embed(event, report_timezone=ZoneInfo("UTC"))

    assert first.description == "<@20> ← <#30>"
    assert {field.name: field.value for field in first.fields} == {
        "В канале": "**1 ч 37 мин**",
        "Сегодня всего": "**3 ч 12 мин**",
        "Вошёл": "10:46",
    }
    assert first.to_dict() == second.to_dict()


def test_voice_move_is_one_semantic_embed_with_logical_session_duration() -> None:
    embed = build_audit_embed(
        record(
            event_type="voice.moved",
            before_data={"channel_id": 30, "channel_name": "Общий"},
            after_data={"channel_id": 31, "channel_name": "Игры"},
            details_data={
                "previous_interval_seconds": 48 * 60,
                "current_session_seconds": 2 * 3600 + 6 * 60,
                "session_started_at": "2026-08-14T09:54:00+00:00",
            },
        )
    )

    assert embed.title == "Участник сменил голосовой канал"
    assert embed.description == "<@20>\n<#30> → <#31>"
    assert {field.name: field.value for field in embed.fields} == {
        "В предыдущем канале": "**48 мин**",
        "Текущая сессия": "**2 ч 06 мин**",
        "Вошёл": "09:54",
    }


def test_old_voice_event_without_enrichment_has_safe_basic_fallback() -> None:
    embed = build_audit_embed(
        record(
            event_type="voice.left",
            before_data={"channel_id": 30, "channel_name": "Общий"},
            after_data={},
            details_data={},
        )
    )

    assert embed.description == "<@20> ← <#30>"
    assert embed.fields == []


@pytest.mark.parametrize("event_type", sorted(SUPPORTED_EVENT_TYPES))
def test_every_single_audit_embed_uses_only_discord_timestamp(
    event_type: str,
) -> None:
    embed = build_audit_embed(record(event_type=event_type))

    assert embed.timestamp == T0
    assert embed.footer.text == "Event ID: 1"
    assert "Сегодня, в" not in (embed.footer.text or "")


def test_channel_created_keeps_channel_type() -> None:
    embed = build_audit_embed(
        record(
            event_type="channel.created",
            category="server",
            subject_type="channel",
            subject_id=30,
            before_data={},
            after_data={"name": "Новый", "channel_type": "text"},
        )
    )

    assert {field.name: field.value for field in embed.fields}["Тип"] == "text"


def test_channel_update_name_only_omits_unchanged_channel_type() -> None:
    embed = build_audit_embed(
        record(
            event_type="channel.updated",
            category="server",
            subject_type="channel",
            subject_id=30,
            before_data={"name": "Старое"},
            after_data={"name": "Новое"},
        )
    )

    fields = {field.name: field.value for field in embed.fields}
    assert embed.title == "Канал переименован"
    assert embed.description == "<#30>"
    assert "Название" in fields
    assert "Старое" in fields["Название"]
    assert "Новое" in fields["Название"]
    assert "Тип" not in fields


def test_channel_update_keeps_type_when_present_in_persisted_diff() -> None:
    embed = build_audit_embed(
        record(
            event_type="channel.updated",
            category="server",
            subject_type="channel",
            subject_id=30,
            before_data={"channel_type": "text"},
            after_data={"channel_type": "voice"},
        )
    )

    fields = {field.name: field.value for field in embed.fields}
    assert fields["Тип"] == "voice"


def channel_position_record(
    event_id: int,
    *,
    occurred_at: datetime = T0,
    channel_id: int | None = None,
    before: int = 17,
    after: int = 7,
    category_id: int | None = 70,
    delivery_attempts: int = 0,
) -> AuditEventRecord:
    actual_channel_id = channel_id or 30 + event_id
    return record(
        id=event_id,
        category="server",
        event_type="channel.updated",
        occurred_at=occurred_at,
        subject_type="channel",
        subject_id=actual_channel_id,
        channel_id=actual_channel_id,
        before_data={"position": before},
        after_data={"position": after},
        details_data={
            "channel_name": f"channel-{event_id}",
            "category_id": category_id,
        },
        delivery_attempts=delivery_attempts,
        expires_at=None,
    )


def test_single_channel_position_update_is_compact_and_hides_internal_ids() -> None:
    embed = build_audit_embed(channel_position_record(1))

    assert embed.title == "Канал перемещён"
    assert embed.description == "<#31>"
    fields = {field.name: field.value for field in embed.fields}
    assert fields == {"Позиция": "**17 → 7**"}
    visible = " ".join(
        [embed.title or "", embed.description or ""]
        + [f"{field.name} {field.value}" for field in embed.fields]
    )
    assert "Guild ID" not in visible
    assert "Channel ID" not in visible
    assert "—" not in visible
    assert embed.footer.text == "Event ID: 1"


def test_channel_topic_update_shows_only_new_topic_without_empty_fields() -> None:
    embed = build_audit_embed(
        record(
            event_type="channel.updated",
            category="server",
            subject_type="channel",
            subject_id=30,
            before_data={"topic": None},
            after_data={"topic": "Новая тема"},
        )
    )

    assert embed.title == "Изменена тема канала"
    assert [(field.name, field.value) for field in embed.fields] == [
        ("Тема", "Новая тема")
    ]
    assert "—" not in str(embed.to_dict())


def test_channel_parent_update_uses_human_readable_mentions() -> None:
    embed = build_audit_embed(
        record(
            event_type="channel.updated",
            category="server",
            subject_type="channel",
            subject_id=30,
            before_data={"category_id": 70, "category_name": "Старая"},
            after_data={"category_id": 80, "category_name": "Новая"},
        )
    )

    assert {field.name: field.value for field in embed.fields} == {
        "Категория": "<#70> → <#80>"
    }
    assert "ID категории" not in str(embed.to_dict())


@pytest.mark.asyncio
async def test_single_position_delivery_waits_only_for_debounce_window() -> None:
    current = [T0 + timedelta(seconds=1)]
    repository = RecordingAuditRepository((channel_position_record(1),))
    channel = FakeChannel()
    runner = AuditLogDeliveryRunner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
        repository_factory=lambda session: repository,
        clock=lambda: current[0],
    )

    assert await runner.run_once(FakeClient(channel)) == 0  # type: ignore[arg-type]
    assert channel.calls == []
    assert repository.delivered == []

    current[0] = T0 + timedelta(seconds=AUDIT_CHANNEL_POSITION_BATCH_WINDOW_SECONDS)
    assert await runner.run_once(FakeClient(channel)) == 1  # type: ignore[arg-type]
    assert len(channel.calls) == 1
    assert channel.calls[0]["embed"].title == "Канал перемещён"


@pytest.mark.asyncio
async def test_position_updates_are_delivered_as_one_message_and_all_marked() -> None:
    now = T0 + timedelta(seconds=AUDIT_CHANNEL_POSITION_BATCH_WINDOW_SECONDS + 0.1)
    records = (
        channel_position_record(1, before=17, after=7),
        channel_position_record(2, before=7, after=8),
        channel_position_record(3, before=8, after=9),
    )
    repository = RecordingAuditRepository(records)
    channel = FakeChannel()
    runner = AuditLogDeliveryRunner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
        repository_factory=lambda session: repository,
        clock=lambda: now,
    )

    assert await runner.run_once(FakeClient(channel)) == 3  # type: ignore[arg-type]

    assert len(channel.calls) == 1
    embed = channel.calls[0]["embed"]
    assert embed.title == "Изменён порядок каналов"
    assert "<#31> — **17 → 7**" in embed.description
    assert "<#32> — **7 → 8**" in embed.description
    assert embed.timestamp == records[-1].occurred_at
    assert embed.footer.text == "Event IDs: 1, 2, 3"
    assert "Сегодня, в" not in (embed.footer.text or "")
    assert [item[0] for item in repository.delivered] == [1, 2, 3]
    assert {item[1] for item in repository.delivered} == {999}


@pytest.mark.asyncio
async def test_unrelated_position_updates_are_not_combined() -> None:
    now = T0 + timedelta(seconds=6)
    records = (
        channel_position_record(1),
        channel_position_record(
            2,
            occurred_at=T0 + timedelta(seconds=3),
            before=8,
            after=9,
        ),
    )
    repository = RecordingAuditRepository(records)
    channel = FakeChannel()
    runner = AuditLogDeliveryRunner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
        repository_factory=lambda session: repository,
        clock=lambda: now,
    )

    assert await runner.run_once(FakeClient(channel)) == 2  # type: ignore[arg-type]
    assert len(channel.calls) == 2
    assert all(call["embed"].title == "Канал перемещён" for call in channel.calls)


@pytest.mark.asyncio
async def test_position_batch_failure_marks_none_delivered_and_retries_together() -> (
    None
):
    now = T0 + timedelta(seconds=2)
    records = (
        channel_position_record(1),
        channel_position_record(2, before=7, after=8),
    )
    repository = RecordingAuditRepository(records)
    runner = AuditLogDeliveryRunner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
        repository_factory=lambda session: repository,
        clock=lambda: now,
    )

    failing_channel = FakeChannel(fail=RuntimeError("temporary"))
    assert await runner.run_once(FakeClient(failing_channel)) == 0  # type: ignore[arg-type]
    assert repository.delivered == []
    assert [item[0] for item in repository.failed] == [1, 2]
    assert len(failing_channel.calls) == 1

    repository.pending = tuple(
        channel_position_record(
            item.id,
            before=int(item.before_data["position"]),
            after=int(item.after_data["position"]),
            delivery_attempts=1,
        )
        for item in records
    )
    retry_channel = FakeChannel()
    assert await runner.run_once(FakeClient(retry_channel)) == 2  # type: ignore[arg-type]
    assert len(retry_channel.calls) == 1
    assert [item[0] for item in repository.delivered] == [1, 2]


def make_ingestor() -> tuple[AuditEventIngestor, RecordingAuditRepository]:
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository()
    ingestor = AuditEventIngestor(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        repository_factory=lambda session: repository,
    )
    return ingestor, repository


class FakeAsset:
    def __init__(self, key: str) -> None:
        self.key = key

    def __str__(self) -> str:
        return f"https://cdn.example/{self.key}.png"


class FakeMember:
    def __init__(
        self,
        *,
        guild_id: int = 10,
        nickname: str | None = None,
        role_ids: tuple[int, ...] = (1,),
        timeout: datetime | None = None,
        guild_avatar: FakeAsset | None = None,
    ) -> None:
        self.id = 20
        self.guild = SimpleNamespace(id=guild_id)
        self.bot = False
        self.joined_at = T0
        self.name = "kanami"
        self.display_name = nickname or self.name
        self.nick = nickname
        self.roles = tuple(SimpleNamespace(id=role_id) for role_id in role_ids)
        self.timed_out_until = timeout
        self.guild_avatar = guild_avatar


@pytest.mark.asyncio
async def test_member_join_and_left_are_normalized() -> None:
    ingestor, repository = make_ingestor()
    member = FakeMember()

    await ingestor.member_joined(member, T0)  # type: ignore[arg-type]
    joined = repository.events[0][0]
    await ingestor.member_left(member, T0 + timedelta(seconds=1))  # type: ignore[arg-type]
    left = repository.events[0][0]

    assert joined.event_type == "member.joined"
    assert joined.retention_policy is AuditRetentionPolicy.IMPORTANT
    assert left.event_type == "member.left"


@pytest.mark.asyncio
async def test_member_history_only_ingestor_filters_unrouted_audit_events() -> None:
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository()
    ingestor = AuditEventIngestor(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        repository_factory=lambda session: repository,
        enabled_event_types=("member.joined", "member.left"),
        suppress_delivery=True,
    )
    member = FakeMember()
    updated = FakeMember(nickname="new")

    assert await ingestor.member_joined(member, T0) == 1  # type: ignore[arg-type]
    assert await ingestor.member_updated(member, updated, T0) == 0  # type: ignore[arg-type]
    assert [event.event_type for event, _ in repository.events] == ["member.joined"]
    assert repository.suppressed == [((), T0)]


@pytest.mark.asyncio
async def test_user_update_splits_username_and_avatar_events() -> None:
    ingestor, repository = make_ingestor()
    guild = SimpleNamespace(id=10)
    member = FakeMember()
    before = SimpleNamespace(id=20, name="old", avatar=FakeAsset("old"))
    after = SimpleNamespace(id=20, name="new", avatar=FakeAsset("new"))

    assert await ingestor.user_updated(guild, member, before, after, T0) == 2  # type: ignore[arg-type]

    events = [item[0] for item in repository.events]
    assert [item.event_type for item in events] == [
        "user.username_updated",
        "user.avatar_updated",
    ]
    assert events[0].before_data == {"username": "old"}
    assert events[1].after_data["avatar"]["key"] == "new"


@pytest.mark.asyncio
async def test_member_update_splits_all_meaningful_changes() -> None:
    ingestor, repository = make_ingestor()
    before = FakeMember(guild_avatar=FakeAsset("old"))
    after = FakeMember(
        nickname="Канами",
        role_ids=(1, 2),
        timeout=T0 + timedelta(hours=1),
        guild_avatar=FakeAsset("new"),
    )

    assert await ingestor.member_updated(before, after, T0) == 4  # type: ignore[arg-type]

    events = {item[0].event_type: item[0] for item in repository.events}
    assert set(events) == {
        "member.nickname_updated",
        "member.guild_avatar_updated",
        "member.roles_updated",
        "member.timeout_updated",
    }
    assert events["member.roles_updated"].details_data["added_role_ids"] == [2]


@pytest.mark.asyncio
async def test_voice_events_only_track_channel_transitions() -> None:
    ingestor, repository = make_ingestor()
    member = FakeMember()
    first = SimpleNamespace(id=30, name="Общий")
    second = SimpleNamespace(id=31, name="Игры")
    none_state = SimpleNamespace(channel=None, self_mute=False, self_video=False)
    first_state = SimpleNamespace(channel=first, self_mute=False, self_video=False)
    muted_state = SimpleNamespace(channel=first, self_mute=True, self_video=True)
    second_state = SimpleNamespace(channel=second, self_mute=False, self_video=False)

    await ingestor.voice_changed(member, none_state, first_state, T0)  # type: ignore[arg-type]
    assert repository.events[0][0].event_type == "voice.joined"
    await ingestor.voice_changed(member, first_state, second_state, T0)  # type: ignore[arg-type]
    assert repository.events[0][0].event_type == "voice.moved"
    await ingestor.voice_changed(member, second_state, none_state, T0)  # type: ignore[arg-type]
    assert repository.events[0][0].event_type == "voice.left"
    previous = repository.events
    assert await ingestor.voice_changed(member, first_state, muted_state, T0) == 0  # type: ignore[arg-type]
    assert repository.events is previous


class FakeVoiceAuditRepository:
    async def get_transition_timing(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        return VoiceAuditTransitionTiming(
            session_started_at=T0 - timedelta(hours=2, minutes=6),
            previous_interval_seconds=48 * 60,
            current_session_seconds=2 * 3600 + 6 * 60,
            counted_exact_session_seconds=2 * 3600 + 6 * 60,
            previous_interval_is_afk=False,
        )

    async def get_today_total_seconds(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        return 3 * 3600 + 12 * 60


@pytest.mark.asyncio
async def test_voice_ingestion_saves_join_count_and_move_tracker_snapshot() -> None:
    sessions = FakeSessionFactory()
    repository = RecordingAuditRepository()
    ingestor = AuditEventIngestor(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        repository_factory=lambda session: repository,
        voice_audit_repository_factory=lambda session: FakeVoiceAuditRepository(),
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
        min_session_seconds=60,
    )
    member = FakeMember()
    first = SimpleNamespace(
        id=30,
        name="Общий",
        members=(member, SimpleNamespace(bot=False), SimpleNamespace(bot=True)),
    )
    second = SimpleNamespace(id=31, name="Игры", members=(member,))
    none_state = SimpleNamespace(channel=None)
    first_state = SimpleNamespace(channel=first)
    second_state = SimpleNamespace(channel=second)

    await ingestor.voice_changed(
        member,
        none_state,
        first_state,
        T0,
        transition_result=VoiceTransitionResult.JOINED,
    )  # type: ignore[arg-type]
    joined = repository.events[0][0]
    assert joined.details_data["channel_member_count"] == 2

    await ingestor.voice_changed(
        member,
        first_state,
        second_state,
        T0,
        transition_result=VoiceTransitionResult.MOVED,
    )  # type: ignore[arg-type]
    moved = repository.events[0][0]
    assert moved.event_type == "voice.moved"
    assert moved.details_data["previous_interval_seconds"] == 48 * 60
    assert moved.details_data["current_session_seconds"] == 2 * 3600 + 6 * 60


class FakeGuildChannel:
    def __init__(self, *, guild_id: int = 10, name: str = "Общий") -> None:
        self.id = 30
        self.guild = SimpleNamespace(id=guild_id)
        self.name = name
        self.type = discord.ChannelType.voice
        self.category = None
        self.position = 1
        self.overwrites = {}
        self.bitrate = 64000
        self.user_limit = 0


@pytest.mark.asyncio
async def test_channel_create_delete_update_and_noop() -> None:
    ingestor, repository = make_ingestor()
    before = FakeGuildChannel(name="Старое")
    after = FakeGuildChannel(name="Новое")

    await ingestor.channel_created(before, T0)
    assert repository.events[0][0].event_type == "channel.created"
    await ingestor.channel_deleted(before, T0)
    assert repository.events[0][0].before_data["name"] == "Старое"
    await ingestor.channel_updated(before, after, T0)
    assert repository.events[0][0].before_data == {"name": "Старое"}
    previous = repository.events
    assert await ingestor.channel_updated(after, after, T0) == 0
    assert repository.events is previous


class FakeRole:
    def __init__(
        self, *, guild_id: int = 10, name: str = "Role", permissions: int = 0
    ) -> None:
        self.id = 50
        self.guild = SimpleNamespace(id=guild_id)
        self.name = name
        self.color = SimpleNamespace(value=0x123456)
        self.permissions = discord.Permissions(permissions)
        self.mentionable = False
        self.hoist = False
        self.position = 1


@pytest.mark.asyncio
async def test_role_create_delete_and_update_are_normalized() -> None:
    ingestor, repository = make_ingestor()
    before = FakeRole(name="Старая")
    after = FakeRole(name="Новая", permissions=discord.Permissions.kick_members.flag)

    await ingestor.role_created(before, T0)  # type: ignore[arg-type]
    assert repository.events[0][0].event_type == "role.created"
    await ingestor.role_deleted(before, T0)  # type: ignore[arg-type]
    assert repository.events[0][0].event_type == "role.deleted"
    await ingestor.role_updated(before, after, T0)  # type: ignore[arg-type]
    updated = repository.events[0][0]
    assert updated.event_type == "role.updated"
    assert "kick_members" in updated.details_data["added_permissions"]


@pytest.mark.asyncio
async def test_ban_unban_and_other_guild_filter() -> None:
    ingestor, repository = make_ingestor()
    guild = SimpleNamespace(id=10)
    user = SimpleNamespace(id=20, display_name="Kanami")

    await ingestor.moderation_changed("moderation.banned", guild, user, T0)  # type: ignore[arg-type]
    assert repository.events[0][0].event_type == "moderation.banned"
    await ingestor.moderation_changed("moderation.unbanned", guild, user, T0)  # type: ignore[arg-type]
    assert repository.events[0][0].event_type == "moderation.unbanned"
    previous = repository.events
    assert (
        await ingestor.member_joined(FakeMember(guild_id=11), T0)  # type: ignore[arg-type]
        == 0
    )
    assert repository.events is previous
