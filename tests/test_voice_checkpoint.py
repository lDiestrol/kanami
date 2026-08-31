import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import discord
import pytest

from discord_stats_bot.discord import (
    DiscordStatsClient,
    GuildReferenceProvisioningSummary,
    VoiceCheckpointRunner,
    VoiceStartupReconciliationSummary,
)
from discord_stats_bot.features.voice import (
    ObservedVoiceState,
    OpenVoiceState,
    VoiceTransitionResult,
)

H = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)
C = H + timedelta(minutes=1)


class FakeSession:
    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSessionFactory:
    def __init__(self) -> None:
        self.begin_count = 0

    def begin(self) -> FakeSession:
        self.begin_count += 1
        return FakeSession()


class FakeMember:
    def __init__(self, user_id: int, guild: object, *, bot: bool = False) -> None:
        self.id = user_id
        self.guild = guild
        self.bot = bot


class FakeChannel:
    def __init__(self, channel_id: int, *user_ids: int) -> None:
        self.id = channel_id
        self.voice_states = dict.fromkeys(user_ids, object())


class FakeGuild:
    def __init__(
        self,
        *,
        voice_channels: tuple[FakeChannel, ...] = (),
        stage_channels: tuple[FakeChannel, ...] = (),
        afk_channel: FakeChannel | None = None,
        bots: frozenset[int] = frozenset(),
    ) -> None:
        self.id = 10
        self.voice_channels = voice_channels
        self.stage_channels = stage_channels
        self.afk_channel = afk_channel
        user_ids = {
            user_id
            for channel in (*voice_channels, *stage_channels)
            for user_id in channel.voice_states
        }
        self._members = {
            user_id: FakeMember(user_id, self, bot=user_id in bots)
            for user_id in user_ids
        }

    def get_member(self, user_id: int) -> FakeMember | None:
        return self._members.get(user_id)


def open_state(
    user_id: int,
    *,
    channel_id: int,
    channel_kind: str = "voice",
    is_afk: bool = False,
    confirmed_at: datetime = H,
) -> OpenVoiceState:
    return OpenVoiceState(
        session_id=100 + user_id,
        interval_id=200 + user_id,
        confirmed_through_at=confirmed_at,
        channel_id=channel_id,
        channel_kind=channel_kind,
        is_afk=is_afk,
    )


class FakeVoiceRepository:
    def __init__(
        self,
        states: dict[int, OpenVoiceState],
        *,
        failing_user_id: int | None = None,
    ) -> None:
        self.states = dict(states)
        self.failing_user_id = failing_user_id
        self.calls: list[tuple[object, ...]] = []

    async def lock_member(self, guild_id: int, user_id: int) -> bool:
        if user_id == self.failing_user_id:
            raise RuntimeError("isolated test failure")
        self.calls.append(("lock", guild_id, user_id))
        return True

    async def get_open_state(
        self, guild_id: int, user_id: int
    ) -> OpenVoiceState | None:
        self.calls.append(("get", guild_id, user_id))
        return self.states.get(user_id)

    async def get_latest_confirmed_through_at(
        self, guild_id: int, user_id: int
    ) -> datetime | None:
        self.calls.append(("latest", guild_id, user_id))
        return None

    async def create_open_state(
        self, observed: ObservedVoiceState, *, quality: str
    ) -> None:
        self.calls.append(("create", observed, quality))

    async def advance_confirmation(
        self, state: OpenVoiceState, observed_at: datetime
    ) -> None:
        user_id = state.session_id - 100
        self.calls.append(("advance", user_id, observed_at))
        self.states[user_id] = replace(state, confirmed_through_at=observed_at)

    async def move_open_interval(
        self,
        state: OpenVoiceState,
        observed: ObservedVoiceState,
        *,
        quality: str,
    ) -> None:
        self.calls.append(("move", state.session_id, observed, quality))
        self.states[observed.user_id] = replace(
            state,
            confirmed_through_at=observed.observed_at,
            channel_id=observed.channel_id,
            channel_kind=observed.channel_kind,
            is_afk=observed.is_afk,
        )

    async def reconcile_same_snapshot(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("checkpoint must use live observation semantics")

    async def close_open_state(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("checkpoint must not infer disconnections")


def make_runner(
    repository: FakeVoiceRepository,
    session_factory: FakeSessionFactory | None = None,
) -> VoiceCheckpointRunner:
    return VoiceCheckpointRunner(
        session_factory or FakeSessionFactory(),  # type: ignore[arg-type]
        repository_factory=lambda session: repository,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_checkpoint_confirms_multiple_users_without_new_state() -> None:
    repository = FakeVoiceRepository(
        {1: open_state(1, channel_id=30), 2: open_state(2, channel_id=30)}
    )
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1, 2),))
    session_factory = FakeSessionFactory()

    summary = await make_runner(repository, session_factory).checkpoint_guild(  # type: ignore[arg-type]
        guild, C
    )

    assert summary.connected_count == 2
    assert summary.outcomes == {VoiceTransitionResult.UNCHANGED: 2}
    assert {state.session_id for state in repository.states.values()} == {101, 102}
    assert {state.interval_id for state in repository.states.values()} == {201, 202}
    assert {state.confirmed_through_at for state in repository.states.values()} == {C}
    assert session_factory.begin_count == 2
    assert not any(call[0] in {"create", "move"} for call in repository.calls)


@pytest.mark.asyncio
async def test_checkpoint_supports_voice_stage_afk_bots_and_one_timestamp() -> None:
    voice = FakeChannel(30, 1, 4)
    stage = FakeChannel(31, 2)
    afk = FakeChannel(32, 3)
    repository = FakeVoiceRepository(
        {
            1: open_state(1, channel_id=30),
            2: open_state(2, channel_id=31, channel_kind="stage"),
            3: open_state(3, channel_id=32, is_afk=True),
            4: open_state(4, channel_id=30),
        }
    )
    guild = FakeGuild(
        voice_channels=(voice, afk),
        stage_channels=(stage,),
        afk_channel=afk,
        bots=frozenset({4}),
    )

    summary = await make_runner(repository).checkpoint_guild(guild, C)  # type: ignore[arg-type]

    assert summary.connected_count == 3
    assert {call[1] for call in repository.calls if call[0] == "advance"} == {
        1,
        2,
        3,
    }
    assert {call[2] for call in repository.calls if call[0] == "advance"} == {C}
    assert repository.states[4].confirmed_through_at == H


@pytest.mark.asyncio
async def test_stale_checkpoint_cannot_undo_newer_live_observation() -> None:
    newer = C + timedelta(seconds=1)
    repository = FakeVoiceRepository(
        {1: open_state(1, channel_id=31, confirmed_at=newer)}
    )
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))

    summary = await make_runner(repository).checkpoint_guild(guild, C)  # type: ignore[arg-type]

    assert summary.outcomes == {VoiceTransitionResult.IGNORED_STALE: 1}
    assert repository.states[1].channel_id == 31
    assert repository.states[1].confirmed_through_at == newer
    assert not any(call[0] in {"advance", "move"} for call in repository.calls)


@pytest.mark.asyncio
async def test_checkpoint_does_not_infer_disconnect_for_absent_cached_user() -> None:
    repository = FakeVoiceRepository({1: open_state(1, channel_id=30)})

    summary = await make_runner(repository).checkpoint_guild(FakeGuild(), C)  # type: ignore[arg-type]

    assert summary.connected_count == 0
    assert summary.outcomes == {}
    assert repository.states[1].confirmed_through_at == H
    assert repository.calls == []


@pytest.mark.asyncio
async def test_checkpoint_cache_divergence_uses_live_move_semantics() -> None:
    repository = FakeVoiceRepository({1: open_state(1, channel_id=30)})
    guild = FakeGuild(stage_channels=(FakeChannel(31, 1),))

    summary = await make_runner(repository).checkpoint_guild(guild, C)  # type: ignore[arg-type]

    assert summary.outcomes == {VoiceTransitionResult.MOVED: 1}
    assert repository.states[1].session_id == 101
    assert repository.states[1].channel_id == 31
    assert repository.states[1].channel_kind == "stage"
    assert sum(call[0] == "move" for call in repository.calls) == 1


@pytest.mark.asyncio
async def test_checkpoint_isolates_one_user_failure() -> None:
    repository = FakeVoiceRepository(
        {1: open_state(1, channel_id=30), 2: open_state(2, channel_id=30)},
        failing_user_id=1,
    )
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1, 2),))

    summary = await make_runner(repository).checkpoint_guild(guild, C)  # type: ignore[arg-type]

    assert summary.failed_count == 1
    assert summary.outcomes == {VoiceTransitionResult.UNCHANGED: 1}
    assert repository.states[1].confirmed_through_at == H
    assert repository.states[2].confirmed_through_at == C


class NoOpProvisioner:
    async def provision_guild(self, guild: object) -> GuildReferenceProvisioningSummary:
        return GuildReferenceProvisioningSummary(0, 0, 0)


class NoOpReconciler:
    async def reconcile_guild(
        self, guild: object, reconciled_at: datetime
    ) -> VoiceStartupReconciliationSummary:
        return VoiceStartupReconciliationSummary(reconciled_at, 0, 0, {}, 0)


class NoOpEventHandler:
    async def handle(self, *args: object) -> None:
        return None


class RecordingCheckpointer:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def checkpoint_guild(self, guild: object, checkpointed_at: datetime) -> None:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class HarnessClient(DiscordStatsClient):
    def __init__(self, guild: FakeGuild, checkpointer: RecordingCheckpointer) -> None:
        self._guild = guild
        super().__init__(
            guild_id=guild.id,
            reference_provisioner=NoOpProvisioner(),  # type: ignore[arg-type]
            voice_reconciler=NoOpReconciler(),  # type: ignore[arg-type]
            voice_event_handler=NoOpEventHandler(),  # type: ignore[arg-type]
            voice_checkpointer=checkpointer,  # type: ignore[arg-type]
            voice_checkpoint_interval_seconds=0.001,  # type: ignore[arg-type]
            clock=lambda: C,
        )

    def get_guild(self, guild_id: int) -> discord.Guild | None:
        return self._guild if guild_id == self._guild.id else None  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_checkpoint_lifecycle_waits_for_recovery_and_cancels_on_disconnect() -> (
    None
):
    checkpointer = RecordingCheckpointer()
    client = HarnessClient(FakeGuild(), checkpointer)

    await asyncio.sleep(0)
    assert checkpointer.calls == 0
    assert client._voice_checkpoint_task is None

    await client.on_ready()
    first_task = client._voice_checkpoint_task
    await asyncio.wait_for(checkpointer.started.wait(), timeout=1)
    assert first_task is not None

    await client.on_disconnect()
    assert first_task.done()
    assert checkpointer.cancelled.is_set()
    assert client._voice_checkpoint_task is None


@pytest.mark.asyncio
async def test_resume_replaces_loop_without_duplicates_and_close_cancels_it() -> None:
    first = RecordingCheckpointer()
    client = HarnessClient(FakeGuild(), first)
    await client.on_ready()
    await asyncio.wait_for(first.started.wait(), timeout=1)
    first_task = client._voice_checkpoint_task

    second = RecordingCheckpointer()
    client._voice_checkpointer = second
    await client.on_resumed()
    await asyncio.wait_for(second.started.wait(), timeout=1)
    second_task = client._voice_checkpoint_task

    assert first_task is not None and first_task.done()
    assert second_task is not None and second_task is not first_task
    await client.close()
    assert second_task.done()
    assert second.cancelled.is_set()
    assert client._voice_checkpoint_task is None
