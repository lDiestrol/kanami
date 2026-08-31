from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import discord
import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.discord import AuditLogDeliveryRunner, build_member_return_embed
from discord_stats_bot.discord.member_returns import MemberReturnEventHandler
from discord_stats_bot.features.achievements import UnlockedAchievement
from discord_stats_bot.features.audit_logging import AuditEventRecord
from discord_stats_bot.features.member_returns import (
    MEMBER_RETURN_EVENT_TYPE,
    MemberReturnEvent,
    MemberReturnService,
    MemberReturnSnapshot,
)
from discord_stats_bot.features.server_settings import (
    GuildServerSettingsBaselines,
    resolve_guild_server_settings,
)
from discord_stats_bot.features.text_activity import TextUserMessageCount
from discord_stats_bot.features.voice_statistics import (
    VoicePeriodDurations,
    VoiceStatistics,
)
from discord_stats_bot.persistence.repositories.member_returns import (
    SqlAlchemyMemberReturnRepository,
)

RETURNED_AT = datetime(2026, 8, 20, 12, tzinfo=UTC)


class MemoryHistory:
    def __init__(self, lefts: tuple[datetime, ...] = ()) -> None:
        self.lefts = list(lefts)
        self.keys: set[tuple[int, int, datetime]] = set()
        self.events: list[MemberReturnEvent] = []

    async def latest_member_left_at(
        self, *, guild_id: int, user_id: int, before_or_at: datetime
    ) -> datetime | None:
        del guild_id, user_id
        candidates = [value for value in self.lefts if value <= before_or_at]
        return max(candidates, default=None)

    async def count_member_leaves(
        self, *, guild_id: int, user_id: int, before_or_at: datetime
    ) -> int:
        del guild_id, user_id
        return sum(value <= before_or_at for value in self.lefts)

    async def enqueue_member_return(self, event: MemberReturnEvent) -> bool:
        key = (event.guild_id, event.user_id, event.returned_at)
        if key in self.keys:
            return False
        self.keys.add(key)
        self.events.append(event)
        return True


class VoiceMetrics:
    def __init__(self, seconds: int = 1_123_200) -> None:
        self.seconds = seconds
        self.calls: list[datetime] = []

    async def get_user_statistics(
        self, guild_id: int, user_id: int, as_of: datetime
    ) -> VoiceStatistics:
        del guild_id, user_id
        self.calls.append(as_of)
        zero = VoicePeriodDurations()
        return VoiceStatistics(
            as_of=as_of,
            today=zero,
            last_7_days=zero,
            last_30_days=zero,
            all_time=VoicePeriodDurations(exact_seconds=self.seconds),
        )


class TextMetrics:
    def __init__(self, count: int = 8_412) -> None:
        self.count = count
        self.ended_on: date | None = None

    async def get_user_message_counts(
        self,
        guild_id: int,
        started_on: date | None,
        ended_on: date,
        *,
        user_ids: tuple[int, ...] | None = None,
        limit: int | None = None,
    ) -> tuple[TextUserMessageCount, ...]:
        del guild_id, started_on, limit
        self.ended_on = ended_on
        assert user_ids == (20,)
        return (TextUserMessageCount(20, self.count),) if self.count else ()


class AchievementMetrics:
    def __init__(self, count: int = 2) -> None:
        self.count = count

    async def list_unlocked(
        self, *, guild_id: int, user_id: int
    ) -> tuple[UnlockedAchievement, ...]:
        return tuple(
            UnlockedAchievement(guild_id, user_id, f"achievement_{item}", RETURNED_AT)
            for item in range(self.count)
        )


def make_service(
    history: MemoryHistory,
    *,
    min_absence_seconds: int = 86_400,
    timezone: ZoneInfo = ZoneInfo("UTC"),
    voice: VoiceMetrics | None = None,
    text: TextMetrics | None = None,
    achievements: AchievementMetrics | None = None,
) -> tuple[MemberReturnService, VoiceMetrics, TextMetrics]:
    voice = voice or VoiceMetrics()
    text = text or TextMetrics()
    return (
        MemberReturnService(
            history,
            voice,
            text,
            achievements or AchievementMetrics(),
            report_timezone=timezone,
            min_absence_seconds=min_absence_seconds,
        ),
        voice,
        text,
    )


@pytest.mark.asyncio
async def test_first_join_does_not_create_return_event() -> None:
    history = MemoryHistory()
    service, voice, _ = make_service(history)

    assert not await service.enqueue_if_returned(
        MemberReturnSnapshot(10, 20, RETURNED_AT)
    )
    assert history.events == []
    assert voice.calls == []


@pytest.mark.asyncio
async def test_join_after_left_snapshots_absence_and_all_lifetime_metrics() -> None:
    left_at = RETURNED_AT - timedelta(days=184, seconds=17)
    history = MemoryHistory((left_at,))
    service, voice, _ = make_service(history)

    assert await service.enqueue_if_returned(MemberReturnSnapshot(10, 20, RETURNED_AT))

    event = history.events[0]
    assert event.previous_left_at == left_at
    assert event.returned_at == RETURNED_AT
    assert event.absence_seconds == 184 * 86_400 + 17
    assert event.voice_seconds == 1_123_200
    assert event.message_count == 8_412
    assert event.achievement_count == 2
    assert event.return_number == 1
    assert voice.calls == [RETURNED_AT]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("absence", "expected"),
    [(timedelta(seconds=86_399), False), (timedelta(seconds=86_400), True)],
)
async def test_minimum_absence_threshold_boundary(
    absence: timedelta, expected: bool
) -> None:
    history = MemoryHistory((RETURNED_AT - absence,))
    service, _, _ = make_service(history)

    assert (
        await service.enqueue_if_returned(MemberReturnSnapshot(10, 20, RETURNED_AT))
        is expected
    )


@pytest.mark.asyncio
async def test_bot_does_not_create_return_event() -> None:
    history = MemoryHistory((RETURNED_AT - timedelta(days=2),))
    service, voice, _ = make_service(history)

    assert not await service.enqueue_if_returned(
        MemberReturnSnapshot(10, 20, RETURNED_AT, is_bot=True)
    )
    assert voice.calls == []


@pytest.mark.asyncio
async def test_multiple_leave_join_cycles_produce_correct_return_number() -> None:
    first_left = RETURNED_AT - timedelta(days=400)
    second_left = RETURNED_AT - timedelta(days=2)
    history = MemoryHistory((first_left,))
    service, _, _ = make_service(history)
    first_return = first_left + timedelta(days=2)

    assert await service.enqueue_if_returned(MemberReturnSnapshot(10, 20, first_return))
    history.lefts.append(second_left)
    assert await service.enqueue_if_returned(MemberReturnSnapshot(10, 20, RETURNED_AT))
    assert [event.return_number for event in history.events] == [1, 2]


@pytest.mark.asyncio
async def test_report_timezone_controls_message_snapshot_calendar_date() -> None:
    returned_at = datetime(2026, 8, 20, 20, 30, tzinfo=UTC)
    history = MemoryHistory((returned_at - timedelta(days=2),))
    service, _, text = make_service(history, timezone=ZoneInfo("Asia/Yekaterinburg"))

    assert await service.enqueue_if_returned(MemberReturnSnapshot(10, 20, returned_at))
    assert text.ended_on == date(2026, 8, 21)


@pytest.mark.asyncio
async def test_repeated_join_and_new_service_after_restart_do_not_duplicate() -> None:
    history = MemoryHistory((RETURNED_AT - timedelta(days=2),))
    first, _, _ = make_service(history)
    second, _, _ = make_service(history)
    member = MemberReturnSnapshot(10, 20, RETURNED_AT)

    assert await first.enqueue_if_returned(member)
    assert not await first.enqueue_if_returned(member)
    assert not await second.enqueue_if_returned(member)
    assert len(history.events) == 1


class NeverBeginSessionFactory:
    def begin(self) -> object:
        raise AssertionError("database must not be touched")


@pytest.mark.asyncio
async def test_handler_skips_joined_at_none_and_bots() -> None:
    handler = MemberReturnEventHandler(
        NeverBeginSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        min_absence_seconds=86_400,
        min_session_seconds=10,
    )
    guild = SimpleNamespace(id=10)

    assert not await handler.handle(  # type: ignore[arg-type]
        SimpleNamespace(id=20, guild=guild, bot=False, joined_at=None)
    )
    assert not await handler.handle(  # type: ignore[arg-type]
        SimpleNamespace(id=20, guild=guild, bot=True, joined_at=RETURNED_AT)
    )


@pytest.mark.asyncio
async def test_return_handler_reads_new_effective_channel_without_restart() -> None:
    class Provider:
        def __init__(self) -> None:
            self.channel_id: int | None = None

        async def get(self):  # type: ignore[no-untyped-def]
            return resolve_guild_server_settings(
                10,
                GuildServerSettingsBaselines(return_channel_id=self.channel_id),
                None,
            )

    class RecordingFactory:
        def __init__(self) -> None:
            self.begin_calls = 0

        def begin(self) -> object:
            self.begin_calls += 1
            raise RuntimeError("db path reached")

    provider = Provider()
    sessions = RecordingFactory()
    handler = MemberReturnEventHandler(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        min_absence_seconds=86_400,
        min_session_seconds=10,
        settings_provider=provider,
    )
    member = SimpleNamespace(
        id=20,
        guild=SimpleNamespace(id=10),
        bot=False,
        joined_at=RETURNED_AT,
    )

    assert await handler.handle(member) is False  # type: ignore[arg-type]
    provider.channel_id = 70
    with pytest.raises(RuntimeError, match="db path reached"):
        await handler.handle(member)  # type: ignore[arg-type]

    assert sessions.begin_calls == 1


class InsertResult:
    def scalar_one_or_none(self) -> int:
        return 1


class RecordingSession:
    def __init__(self) -> None:
        self.statement: object | None = None

    async def execute(self, statement: object) -> InsertResult:
        self.statement = statement
        return InsertResult()


@pytest.mark.asyncio
async def test_repository_conflict_key_and_snapshot_match_partial_index() -> None:
    session = RecordingSession()
    repository = SqlAlchemyMemberReturnRepository(session)  # type: ignore[arg-type]
    event = MemberReturnEvent(
        10,
        20,
        RETURNED_AT - timedelta(days=2),
        RETURNED_AT,
        172_800,
        3_600,
        42,
        3,
        2,
    )

    assert await repository.enqueue_member_return(event)
    compiled = session.statement.compile(  # type: ignore[union-attr]
        dialect=postgresql.dialect()
    )
    sql = str(compiled)
    normalized = " ".join(sql.split())
    assert "ON CONFLICT (guild_id, subject_id, occurred_at)" in normalized
    assert "WHERE event_type = 'member.returned' DO NOTHING" in normalized
    details = next(
        value
        for key, value in compiled.params.items()
        if key.startswith("details_data")
    )
    assert details == {
        "absence_seconds": 172_800,
        "previous_left_at": "2026-08-18T12:00:00+00:00",
        "returned_at": "2026-08-20T12:00:00+00:00",
        "voice_seconds": 3_600,
        "message_count": 42,
        "achievement_count": 3,
        "return_number": 2,
    }


def return_record(
    *, event_id: int = 1, user_id: int = 20, attempts: int = 0
) -> AuditEventRecord:
    return AuditEventRecord(
        id=event_id,
        guild_id=10,
        category="member",
        event_type=MEMBER_RETURN_EVENT_TYPE,
        occurred_at=RETURNED_AT,
        created_at=RETURNED_AT,
        subject_type="user",
        subject_id=user_id,
        actor_user_id=None,
        channel_id=None,
        before_data={},
        after_data={},
        details_data={
            "absence_seconds": 172_800,
            "voice_seconds": 1_123_200,
            "message_count": 8_412,
            "achievement_count": 14,
            "return_number": 2,
        },
        discord_message_id=None,
        delivered_at=None,
        delivery_attempts=attempts,
        next_delivery_attempt_at=None,
        last_delivery_error=None,
        expires_at=None,
    )


class DeliveryRepository:
    def __init__(self, records: tuple[AuditEventRecord, ...]) -> None:
        self.records = list(records)
        self.delivered: list[int] = []
        self.failed: list[int] = []
        self.event_types: tuple[str, ...] | None = None

    async def get_pending_delivery(self, **kwargs: object):  # type: ignore[no-untyped-def]
        self.event_types = kwargs.get("event_types")  # type: ignore[assignment]
        return tuple(
            record for record in self.records if record.id not in self.delivered
        )

    async def mark_delivered_many(
        self, event_ids: tuple[int, ...], message_id: int, delivered_at: datetime
    ) -> None:
        del message_id, delivered_at
        self.delivered.extend(event_ids)

    async def mark_delivery_failed_many(
        self, event_ids: tuple[int, ...], error: str, next_attempt_at: datetime
    ) -> None:
        del error, next_attempt_at
        self.failed.extend(event_ids)


class FakeTransaction:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *args: object) -> None:
        pass


class FakeSessionFactory:
    def __call__(self) -> FakeTransaction:
        return FakeTransaction()

    def begin(self) -> FakeTransaction:
        return FakeTransaction()


class ReturnChannel:
    def __init__(
        self, *, fail: bool = False, fail_user_ids: set[int] | None = None
    ) -> None:
        self.guild = SimpleNamespace(id=10)
        self.fail = fail
        self.fail_user_ids = fail_user_ids or set()
        self.calls: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        description = kwargs["embed"].description  # type: ignore[union-attr]
        if self.fail or any(
            f"<@{user_id}>" in description for user_id in self.fail_user_ids
        ):
            raise discord.HTTPException(AsyncMock(), "temporary")
        return SimpleNamespace(id=100)


class DeliveryClient:
    def __init__(self, channel: ReturnChannel) -> None:
        self.channel = channel
        self.requested_channels: list[int] = []

    def get_channel(self, channel_id: int) -> ReturnChannel | None:
        self.requested_channels.append(channel_id)
        return self.channel if channel_id == 60 else None


def delivery_runner(repository: DeliveryRepository) -> AuditLogDeliveryRunner:
    return AuditLogDeliveryRunner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
        event_channel_ids={MEMBER_RETURN_EVENT_TYPE: 60},
        event_types=(MEMBER_RETURN_EVENT_TYPE,),
        repository_factory=lambda session: repository,
        clock=lambda: RETURNED_AT,
    )


@pytest.mark.asyncio
async def test_delivery_failure_keeps_retry_possible_then_succeeds() -> None:
    repository = DeliveryRepository((return_record(),))
    runner = delivery_runner(repository)

    assert await runner.run_once(DeliveryClient(ReturnChannel(fail=True))) == 0  # type: ignore[arg-type]
    assert repository.failed == [1]
    assert repository.delivered == []

    channel = ReturnChannel()
    client = DeliveryClient(channel)
    assert await runner.run_once(client) == 1  # type: ignore[arg-type]
    assert repository.delivered == [1]
    assert client.requested_channels == [60]
    assert len(channel.calls) == 1
    assert channel.calls[0]["nonce"] == 1
    allowed = channel.calls[0]["allowed_mentions"]
    assert allowed.users != []  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_successful_delivery_is_not_repeated_after_runner_restart() -> None:
    repository = DeliveryRepository((return_record(),))
    channel = ReturnChannel()

    assert await delivery_runner(repository).run_once(DeliveryClient(channel)) == 1  # type: ignore[arg-type]
    assert await delivery_runner(repository).run_once(DeliveryClient(channel)) == 0  # type: ignore[arg-type]
    assert len(channel.calls) == 1
    assert repository.event_types == (MEMBER_RETURN_EVENT_TYPE,)


@pytest.mark.asyncio
async def test_one_member_delivery_error_does_not_stop_other_returns() -> None:
    repository = DeliveryRepository(
        (return_record(event_id=1, user_id=20), return_record(event_id=2, user_id=21))
    )
    channel = ReturnChannel(fail_user_ids={20})

    assert await delivery_runner(repository).run_once(DeliveryClient(channel)) == 1  # type: ignore[arg-type]
    assert repository.failed == [1]
    assert repository.delivered == [2]
    assert len(channel.calls) == 2


def test_embed_uses_id_mention_and_persisted_snapshot() -> None:
    embed = build_member_return_embed(
        user_id=20,
        absence_seconds=184 * 86_400,
        voice_seconds=312 * 3_600,
        message_count=8_412,
        achievement_count=14,
        return_number=2,
    )
    rendered = str(embed.to_dict())

    assert "<@20>" in rendered
    assert "184 дня" in rendered
    assert "312 ч" in rendered
    assert "8 412" in rendered
    assert "14" in rendered
    assert "2-е возвращение" in rendered
