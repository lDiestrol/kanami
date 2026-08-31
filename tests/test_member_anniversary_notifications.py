import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import discord
import pytest

from discord_stats_bot.discord import (
    AuditLogDeliveryRunner,
    DiscordStatsClient,
    MemberAnniversaryCheckRunner,
)
from discord_stats_bot.features.audit_logging import AuditEventRecord
from discord_stats_bot.features.member_anniversaries import (
    MEMBER_ANNIVERSARY_EVENT_TYPE,
    MemberAnniversary,
    MemberAnniversaryNotificationService,
    MemberJoinSnapshot,
)
from discord_stats_bot.features.server_settings import (
    GuildServerSettingsBaselines,
    resolve_guild_server_settings,
)
from tests.support.discord import make_guild, make_member

NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


class RecordingAnniversaryRepository:
    def __init__(self) -> None:
        self.keys: set[tuple[int, int, datetime]] = set()
        self.saved: list[MemberAnniversary] = []
        self.occurred_at: list[datetime] = []

    async def enqueue_anniversaries(
        self,
        *,
        guild_id: int,
        anniversaries: tuple[MemberAnniversary, ...],
        occurred_at: datetime,
    ) -> int:
        created = 0
        for anniversary in anniversaries:
            key = (guild_id, anniversary.user_id, occurred_at)
            if key in self.keys:
                continue
            self.keys.add(key)
            self.saved.append(anniversary)
            self.occurred_at.append(occurred_at)
            created += 1
        return created


def snapshot(
    user_id: int,
    joined_at: datetime | None,
    *,
    bot: bool = False,
) -> MemberJoinSnapshot:
    return MemberJoinSnapshot(user_id, f"User {user_id}", joined_at, bot)


@pytest.mark.asyncio
async def test_today_multiple_members_and_filters_are_enqueued() -> None:
    repository = RecordingAnniversaryRepository()
    service = MemberAnniversaryNotificationService(
        repository,
        report_timezone=ZoneInfo("UTC"),
    )
    members = (
        snapshot(1, datetime(2020, 8, 20, tzinfo=UTC)),
        snapshot(2, datetime(2022, 8, 20, tzinfo=UTC)),
        snapshot(3, datetime(2020, 8, 20, tzinfo=UTC), bot=True),
        snapshot(4, None),
        snapshot(5, datetime(2020, 9, 1, tzinfo=UTC)),
        snapshot(6, datetime(2026, 8, 20, tzinfo=UTC)),
    )

    created = await service.enqueue_today(guild_id=10, members=members, as_of=NOW)

    assert created == 2
    assert [(item.user_id, item.years) for item in repository.saved] == [
        (1, 6),
        (2, 4),
    ]


@pytest.mark.asyncio
async def test_no_anniversaries_does_not_touch_repository() -> None:
    repository = RecordingAnniversaryRepository()
    service = MemberAnniversaryNotificationService(
        repository,
        report_timezone=ZoneInfo("UTC"),
    )

    created = await service.enqueue_today(
        guild_id=10,
        members=(snapshot(1, datetime(2020, 9, 1, tzinfo=UTC)),),
        as_of=NOW,
    )

    assert created == 0
    assert repository.saved == []


@pytest.mark.asyncio
async def test_february_29_and_report_timezone_match_command_rules() -> None:
    repository = RecordingAnniversaryRepository()
    service = MemberAnniversaryNotificationService(
        repository,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
    )

    created = await service.enqueue_today(
        guild_id=10,
        members=(snapshot(1, datetime(2020, 2, 29, 12, tzinfo=UTC)),),
        as_of=datetime(2025, 2, 27, 20, tzinfo=UTC),
    )

    assert created == 1
    assert repository.saved[0].anniversary_date.isoformat() == "2025-02-28"
    assert repository.occurred_at == [datetime(2025, 2, 27, 19, tzinfo=UTC)]


@pytest.mark.asyncio
async def test_repeated_processing_and_restart_do_not_enqueue_twice() -> None:
    repository = RecordingAnniversaryRepository()
    members = (snapshot(1, datetime(2020, 8, 20, tzinfo=UTC)),)

    first_service = MemberAnniversaryNotificationService(
        repository,
        report_timezone=ZoneInfo("UTC"),
    )
    second_service = MemberAnniversaryNotificationService(
        repository,
        report_timezone=ZoneInfo("UTC"),
    )

    assert (
        await first_service.enqueue_today(guild_id=10, members=members, as_of=NOW) == 1
    )
    assert (
        await first_service.enqueue_today(guild_id=10, members=members, as_of=NOW) == 0
    )
    assert (
        await second_service.enqueue_today(guild_id=10, members=members, as_of=NOW) == 0
    )
    assert len(repository.saved) == 1


class FakeTransaction:
    def __init__(self, session: object) -> None:
        self.session = session

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        pass


class FakeSessionFactory:
    def __init__(self) -> None:
        self.session = object()

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self.session)

    def __call__(self) -> FakeTransaction:
        return FakeTransaction(self.session)


class FakeAnniversaryClient:
    def __init__(self, guild: object) -> None:
        self.guild = guild

    def get_guild(self, guild_id: int) -> object | None:
        return self.guild if guild_id == 10 else None


@pytest.mark.asyncio
async def test_check_runner_enqueues_cache_and_wakes_delivery_after_commit() -> None:
    repository = RecordingAnniversaryRepository()
    wake_calls: list[str] = []
    runner = MemberAnniversaryCheckRunner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        repository_factory=lambda session: repository,
        wake_delivery=lambda: wake_calls.append("wake"),
        clock=lambda: NOW,
    )
    guild = make_guild(
        members=(make_member(1, joined_at=datetime(2020, 8, 20, tzinfo=UTC)),)
    )

    assert await runner.run_once(FakeAnniversaryClient(guild)) == 1  # type: ignore[arg-type]
    assert wake_calls == ["wake"]


@pytest.mark.asyncio
async def test_check_runner_reads_new_effective_channel_without_restart() -> None:
    class Provider:
        def __init__(self) -> None:
            self.channel_id: int | None = None

        async def get(self):  # type: ignore[no-untyped-def]
            return resolve_guild_server_settings(
                10,
                GuildServerSettingsBaselines(anniversary_channel_id=self.channel_id),
                None,
            )

    provider = Provider()
    repository = RecordingAnniversaryRepository()
    runner = MemberAnniversaryCheckRunner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        repository_factory=lambda session: repository,
        clock=lambda: NOW,
        settings_provider=provider,
    )
    guild = make_guild(
        members=(make_member(1, joined_at=datetime(2020, 8, 20, tzinfo=UTC)),)
    )
    client = FakeAnniversaryClient(guild)

    assert await runner.run_once(client) == 0  # type: ignore[arg-type]
    provider.channel_id = 60
    assert await runner.run_once(client) == 1  # type: ignore[arg-type]


def delivery_record(user_id: int, *, event_id: int) -> AuditEventRecord:
    return AuditEventRecord(
        id=event_id,
        guild_id=10,
        category="member",
        event_type=MEMBER_ANNIVERSARY_EVENT_TYPE,
        occurred_at=NOW,
        created_at=NOW,
        subject_type="user",
        subject_id=user_id,
        actor_user_id=None,
        channel_id=None,
        before_data={},
        after_data={},
        details_data={"years": 3, "anniversary_date": "2026-08-20"},
        discord_message_id=None,
        delivered_at=None,
        delivery_attempts=0,
        next_delivery_attempt_at=None,
        last_delivery_error=None,
        expires_at=None,
    )


class DeliveryRepository:
    def __init__(self, records: tuple[AuditEventRecord, ...]) -> None:
        self.records = list(records)
        self.delivered: list[int] = []
        self.failed: list[int] = []
        self.requested_event_types: tuple[str, ...] | None = None

    async def get_pending_delivery(self, **kwargs: object):  # type: ignore[no-untyped-def]
        self.requested_event_types = kwargs.get("event_types")  # type: ignore[assignment]
        return tuple(item for item in self.records if item.id not in self.delivered)

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


class SelectiveChannel:
    def __init__(self, *, fail_user_ids: set[int] | None = None) -> None:
        self.guild = SimpleNamespace(id=10)
        self.fail_user_ids = fail_user_ids or set()
        self.calls: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        description = kwargs["embed"].description  # type: ignore[union-attr]
        for user_id in self.fail_user_ids:
            if f"<@{user_id}>" in description:
                raise discord.HTTPException(AsyncMock(), "temporary")
        return SimpleNamespace(id=1000 + len(self.calls))


class DeliveryClient:
    def __init__(self, channel: SelectiveChannel) -> None:
        self.channel = channel

    def get_channel(self, channel_id: int) -> SelectiveChannel | None:
        return self.channel if channel_id == 50 else None


def delivery_runner(repository: DeliveryRepository) -> AuditLogDeliveryRunner:
    return AuditLogDeliveryRunner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        event_channel_ids={MEMBER_ANNIVERSARY_EVENT_TYPE: 50},
        repository_factory=lambda session: repository,
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_delivery_failure_is_retried_and_other_member_continues() -> None:
    repository = DeliveryRepository(
        (delivery_record(1, event_id=1), delivery_record(2, event_id=2))
    )
    runner = delivery_runner(repository)
    first_channel = SelectiveChannel(fail_user_ids={1})

    assert await runner.run_once(DeliveryClient(first_channel)) == 1  # type: ignore[arg-type]
    assert repository.failed == [1]
    assert repository.delivered == [2]

    retry_channel = SelectiveChannel()
    assert await runner.run_once(DeliveryClient(retry_channel)) == 1  # type: ignore[arg-type]
    assert repository.delivered == [2, 1]
    assert "<@1>" in retry_channel.calls[0]["embed"].description  # type: ignore[union-attr]
    assert retry_channel.calls[0]["nonce"] == 1
    allowed = retry_channel.calls[0]["allowed_mentions"]
    assert allowed.everyone is False  # type: ignore[union-attr]
    assert allowed.roles is False  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_successful_delivery_stays_absent_after_runner_restart() -> None:
    repository = DeliveryRepository((delivery_record(1, event_id=1),))
    channel = SelectiveChannel()

    assert await delivery_runner(repository).run_once(DeliveryClient(channel)) == 1  # type: ignore[arg-type]
    assert await delivery_runner(repository).run_once(DeliveryClient(channel)) == 0  # type: ignore[arg-type]
    assert len(channel.calls) == 1


class NoOpDependency:
    pass


@pytest.mark.asyncio
async def test_repeated_ready_keeps_one_worker_and_close_cancels_it() -> None:
    runner = MemberAnniversaryCheckRunner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        clock=lambda: NOW,
    )
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=NoOpDependency(),  # type: ignore[arg-type]
        voice_reconciler=NoOpDependency(),  # type: ignore[arg-type]
        voice_event_handler=NoOpDependency(),  # type: ignore[arg-type]
        member_anniversary_check_runner=runner,
    )
    client._recover_voice_state = AsyncMock()  # type: ignore[method-assign]

    await client.on_ready()
    first_task = runner._task
    await client.on_ready()

    assert first_task is not None
    assert runner._task is first_task
    await asyncio.sleep(0)
    await client.close()
    assert runner._task is None
    assert first_task.cancelled()


def test_next_check_uses_local_wall_clock_instead_of_fixed_day_sleep() -> None:
    runner = MemberAnniversaryCheckRunner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("Asia/Yekaterinburg"),
        clock=lambda: datetime(2026, 8, 20, 18, 0, tzinfo=UTC),
    )

    assert runner._seconds_until_next_check() == 65 * 60
