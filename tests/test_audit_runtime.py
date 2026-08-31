from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from discord_stats_bot.discord import (
    AuditLogDeliveryRunner,
    AuditRetentionRunner,
    DiscordStatsClient,
    create_gateway_intents,
)
from discord_stats_bot.features.voice import VoiceTransitionResult

T0 = datetime(2026, 8, 14, 12, tzinfo=UTC)


class NoOpDependency:
    pass


class RecordingReferenceProvisioner:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.provisioned: list[tuple[object, object | None]] = []
        self.departed: list[tuple[object, datetime]] = []

    async def provision_member(
        self,
        member: object,
        *,
        identity_user: object | None = None,
    ) -> None:
        self.provisioned.append((member, identity_user))
        if self.fail:
            raise RuntimeError("identity failure")

    async def mark_member_left(self, member: object, left_at: datetime) -> None:
        self.departed.append((member, left_at))
        if self.fail:
            raise RuntimeError("identity failure")


class FailingVoiceHandler:
    def __init__(self, error: RuntimeError | None = None) -> None:
        self.calls = 0
        self.error = error or RuntimeError("voice failure")

    async def handle(
        self, member: object, after: object, observed_at: datetime
    ) -> None:
        del member, after, observed_at
        self.calls += 1
        raise self.error


class RecordingAuditIngestor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.voice_calls: list[
            tuple[object, object, object, datetime, VoiceTransitionResult | None]
        ] = []

    async def voice_changed(
        self,
        member: object,
        before: object,
        after: object,
        occurred_at: datetime,
        *,
        transition_result: VoiceTransitionResult | None = None,
    ) -> None:
        self.voice_calls.append((member, before, after, occurred_at, transition_result))
        if self.fail:
            raise RuntimeError("audit failure")


def make_client(
    voice_handler: object,
    audit_ingestor: object | None,
    reference_provisioner: object | None = None,
) -> DiscordStatsClient:
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=(
            reference_provisioner or RecordingReferenceProvisioner()
        ),  # type: ignore[arg-type]
        voice_reconciler=NoOpDependency(),  # type: ignore[arg-type]
        voice_event_handler=voice_handler,  # type: ignore[arg-type]
        audit_event_ingestor=audit_ingestor,  # type: ignore[arg-type]
        clock=lambda: T0,
    )
    client._startup_baseline_at = T0 - timedelta(seconds=1)
    client._startup_complete.set()
    return client


@pytest.mark.asyncio
async def test_audit_voice_failure_does_not_replace_or_break_critical_tracking() -> (
    None
):
    voice_handler = SimpleNamespace(
        handle=pytest.importorskip("unittest.mock").AsyncMock(
            return_value=VoiceTransitionResult.JOINED
        )
    )
    audit = RecordingAuditIngestor(fail=True)
    client = make_client(voice_handler, audit)
    guild = SimpleNamespace(id=10)
    member = SimpleNamespace(id=20, guild=guild, bot=False)
    before = SimpleNamespace(channel=None)
    after = SimpleNamespace(channel=SimpleNamespace(id=30, name="Общий"))

    await client.on_voice_state_update(member, before, after)  # type: ignore[arg-type]

    voice_handler.handle.assert_awaited_once_with(member, after, T0)
    assert len(audit.voice_calls) == 1
    assert audit.voice_calls[0][4] is VoiceTransitionResult.JOINED


@pytest.mark.asyncio
async def test_unexpected_voice_exception_still_allows_audit_ingestion() -> None:
    critical_error = RuntimeError("voice failure")
    voice = FailingVoiceHandler(critical_error)
    audit = RecordingAuditIngestor()
    client = make_client(voice, audit)
    guild = SimpleNamespace(id=10)
    member = SimpleNamespace(id=20, guild=guild, bot=False)
    before = SimpleNamespace(channel=None)
    after = SimpleNamespace(channel=SimpleNamespace(id=30, name="Общий"))

    with pytest.raises(RuntimeError, match="voice failure") as exc_info:
        await client.on_voice_state_update(  # type: ignore[arg-type]
            member, before, after
        )

    assert voice.calls == 1
    assert exc_info.value is critical_error
    assert len(audit.voice_calls) == 1


@pytest.mark.asyncio
async def test_critical_voice_exception_wins_when_audit_also_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    critical_error = RuntimeError("voice failure")
    voice = FailingVoiceHandler(critical_error)
    audit = RecordingAuditIngestor(fail=True)
    client = make_client(voice, audit)
    member = SimpleNamespace(id=20, guild=SimpleNamespace(id=10), bot=False)
    before = SimpleNamespace(channel=None)
    after = SimpleNamespace(channel=SimpleNamespace(id=30, name="Общий"))

    with pytest.raises(RuntimeError, match="voice failure") as exc_info:
        await client.on_voice_state_update(  # type: ignore[arg-type]
            member, before, after
        )

    assert str(exc_info.value) == "voice failure"
    assert exc_info.value is critical_error
    assert len(audit.voice_calls) == 1
    assert "Audit voice ingestion failed guild_id=10 subject_id=20" in caplog.text
    assert "audit failure" in caplog.text


@pytest.mark.asyncio
async def test_disabled_audit_callbacks_do_not_touch_audit_dependencies() -> None:
    voice_handler = SimpleNamespace(
        handle=pytest.importorskip("unittest.mock").AsyncMock()
    )
    client = make_client(voice_handler, None)
    member = SimpleNamespace(id=20, guild=SimpleNamespace(id=10), bot=False)

    await client.on_member_join(member)  # type: ignore[arg-type]

    assert client._audit_delivery_runner is None
    assert client._audit_retention_runner is None


def full_member(*, guild_id: int = 10, nickname: str | None = "Kana") -> object:
    return SimpleNamespace(
        id=20,
        guild=SimpleNamespace(id=guild_id),
        bot=False,
        name="kanami",
        global_name="Kanami",
        nick=nickname,
        joined_at=T0 - timedelta(days=365),
    )


@pytest.mark.asyncio
async def test_identity_sync_events_work_without_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references = RecordingReferenceProvisioner()
    client = make_client(NoOpDependency(), None, references)
    before_member = full_member(nickname="Old")
    after_member = full_member(nickname=None)
    before_user = SimpleNamespace(id=20, name="old", global_name="Old")
    after_user = SimpleNamespace(id=20, name="new", global_name=None)
    guild = SimpleNamespace(id=10, get_member=lambda user_id: after_member)
    monkeypatch.setattr(client, "get_guild", lambda guild_id: guild)

    await client.on_member_join(after_member)  # type: ignore[arg-type]
    await client.on_member_update(  # type: ignore[arg-type]
        before_member, after_member
    )
    await client.on_user_update(before_user, after_user)  # type: ignore[arg-type]
    await client.on_member_remove(after_member)  # type: ignore[arg-type]

    assert references.provisioned == [
        (after_member, None),
        (after_member, None),
        (after_member, after_user),
    ]
    assert references.departed == [(after_member, T0)]


@pytest.mark.asyncio
async def test_user_update_only_syncs_member_of_configured_guild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    references = RecordingReferenceProvisioner()
    client = make_client(NoOpDependency(), None, references)
    before = SimpleNamespace(id=20, name="old", global_name=None)
    after = SimpleNamespace(id=20, name="new", global_name=None)
    monkeypatch.setattr(client, "get_guild", lambda guild_id: None)

    await client.on_user_update(before, after)  # type: ignore[arg-type]
    await client.on_member_join(full_member(guild_id=11))  # type: ignore[arg-type]

    assert references.provisioned == []
    assert references.departed == []


@pytest.mark.asyncio
async def test_identity_failure_does_not_block_join_neighbors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async_mock = pytest.importorskip("unittest.mock").AsyncMock
    references = RecordingReferenceProvisioner(fail=True)
    audit = SimpleNamespace(member_joined=async_mock())
    member_return = SimpleNamespace(handle=async_mock())
    autorole = SimpleNamespace(handle=async_mock(), role_id=30)
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=references,  # type: ignore[arg-type]
        voice_reconciler=NoOpDependency(),  # type: ignore[arg-type]
        voice_event_handler=NoOpDependency(),  # type: ignore[arg-type]
        audit_event_ingestor=audit,  # type: ignore[arg-type]
        member_return_event_handler=member_return,  # type: ignore[arg-type]
        autorole_handler=autorole,  # type: ignore[arg-type]
        clock=lambda: T0,
    )
    member = full_member()

    await client.on_member_join(member)  # type: ignore[arg-type]

    audit.member_joined.assert_awaited_once_with(member, T0)
    member_return.handle.assert_awaited_once_with(member)
    autorole.handle.assert_awaited_once_with(member)
    assert "Discord member reference sync failed guild_id=10 user_id=20" in caplog.text


def test_audit_intents_are_minimal_and_include_moderation() -> None:
    intents = create_gateway_intents()

    assert intents.guilds is True
    assert intents.members is True
    assert intents.voice_states is True
    assert intents.moderation is True
    assert intents.message_content is False
    assert intents.presences is False
    assert intents.typing is False
    assert intents.dm_messages is False
    assert intents.dm_reactions is False
    assert intents.dm_typing is False


class RecordingRunner:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self, *args: object) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


@pytest.mark.asyncio
async def test_ready_and_resume_start_calls_keep_single_audit_tasks() -> None:
    delivery = AuditLogDeliveryRunner(
        object(),  # type: ignore[arg-type]
        guild_id=10,
        channel_id=40,
    )
    retention = AuditRetentionRunner(object())  # type: ignore[arg-type]
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=NoOpDependency(),  # type: ignore[arg-type]
        voice_reconciler=NoOpDependency(),  # type: ignore[arg-type]
        voice_event_handler=NoOpDependency(),  # type: ignore[arg-type]
        audit_delivery_runner=delivery,
        audit_retention_runner=retention,
    )

    client._start_audit_runners()
    delivery_task = delivery._task
    retention_task = retention._task
    client._start_audit_runners()

    assert delivery_task is not None
    assert retention_task is not None
    assert delivery._task is delivery_task
    assert retention._task is retention_task

    await client.close()

    assert delivery._task is None
    assert retention._task is None


@pytest.mark.asyncio
async def test_client_close_awaits_both_audit_runners() -> None:
    delivery = RecordingRunner()
    retention = RecordingRunner()
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=NoOpDependency(),  # type: ignore[arg-type]
        voice_reconciler=NoOpDependency(),  # type: ignore[arg-type]
        voice_event_handler=NoOpDependency(),  # type: ignore[arg-type]
        audit_delivery_runner=delivery,  # type: ignore[arg-type]
        audit_retention_runner=retention,  # type: ignore[arg-type]
    )

    await client.close()

    assert delivery.stopped == 1
    assert retention.stopped == 1


def test_allowed_mentions_none_is_representable() -> None:
    allowed = discord.AllowedMentions.none()
    assert allowed.users is False and allowed.roles is False
