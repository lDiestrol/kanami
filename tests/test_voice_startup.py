import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

import discord_stats_bot.discord.runtime as runtime_module
from discord_stats_bot.discord import (
    DiscordStatsClient,
    GuildReferenceProvisioner,
    VoiceStartupReconciler,
    VoiceStartupReconciliationSummary,
    create_gateway_intents,
)
from discord_stats_bot.features.voice import (
    ObservedVoiceState,
    OpenVoiceState,
    VoiceTransitionResult,
)

H = datetime(2026, 8, 12, 10, 0, tzinfo=UTC)
R = H + timedelta(minutes=5)


class FakeSession:
    async def __aenter__(self) -> object:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        return None


class FakeSessionFactory:
    def __call__(self) -> FakeSession:
        return FakeSession()

    def begin(self) -> FakeSession:
        return FakeSession()


class FakeMember:
    def __init__(
        self,
        user_id: int,
        guild: object,
        *,
        bot: bool = False,
        avatar_hash: str | None = "0123456789abcdef0123456789abcdef",
        guild_avatar_hash: str | None = "abcdef0123456789abcdef0123456789",
    ) -> None:
        self.id = user_id
        self.guild = guild
        self.bot = bot
        self.name = f"user-{user_id}"
        self.global_name = f"Global {user_id}"
        self.nick = f"Guild {user_id}"
        self.joined_at = H
        self.avatar = SimpleNamespace(key=avatar_hash) if avatar_hash else None
        self.guild_avatar = (
            SimpleNamespace(key=guild_avatar_hash) if guild_avatar_hash else None
        )


class FakeChannel:
    def __init__(self, channel_id: int, *user_ids: int) -> None:
        self.id = channel_id
        self.name = f"channel-{channel_id}"
        self.voice_states = dict.fromkeys(user_ids, object())


class FakeGuild:
    def __init__(
        self,
        *,
        guild_id: int = 10,
        voice_channels: tuple[FakeChannel, ...] = (),
        stage_channels: tuple[FakeChannel, ...] = (),
        afk_channel: FakeChannel | None = None,
        bots: frozenset[int] = frozenset(),
    ) -> None:
        self.id = guild_id
        self.name = f"guild-{guild_id}"
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

    @property
    def members(self) -> list[FakeMember]:
        return list(self._members.values())

    def get_member(self, user_id: int) -> FakeMember | None:
        return self._members.get(user_id)


class FakeVoiceRepository:
    def __init__(self, current: dict[int, OpenVoiceState] | None = None) -> None:
        self.current = dict(current or {})
        self.latest = {
            user_id: state.confirmed_through_at
            for user_id, state in self.current.items()
        }
        self.calls: list[tuple[object, ...]] = []
        self.next_session_id = 1000
        self.next_interval_id = 2000

    async def list_open_user_ids(self, guild_id: int) -> tuple[int, ...]:
        self.calls.append(("list_open", guild_id))
        return tuple(sorted(self.current))

    async def lock_member(self, guild_id: int, user_id: int) -> bool:
        self.calls.append(("lock", guild_id, user_id))
        return True

    async def get_open_state(
        self,
        guild_id: int,
        user_id: int,
    ) -> OpenVoiceState | None:
        self.calls.append(("get", guild_id, user_id))
        return self.current.get(user_id)

    async def get_latest_confirmed_through_at(
        self,
        guild_id: int,
        user_id: int,
    ) -> datetime | None:
        self.calls.append(("latest", guild_id, user_id))
        return self.latest.get(user_id)

    async def create_open_state(
        self,
        observed: ObservedVoiceState,
        *,
        quality: str,
    ) -> None:
        self.calls.append(("create", observed, quality))
        self.current[observed.user_id] = OpenVoiceState(
            session_id=self.next_session_id,
            interval_id=self.next_interval_id,
            confirmed_through_at=observed.observed_at,
            channel_id=observed.channel_id,
            channel_kind=observed.channel_kind,
            is_afk=observed.is_afk,
        )
        self.latest[observed.user_id] = observed.observed_at
        self.next_session_id += 1
        self.next_interval_id += 1

    async def advance_confirmation(
        self,
        state: OpenVoiceState,
        observed_at: datetime,
    ) -> None:
        raise AssertionError("startup reconciliation must not use live confirmation")

    async def move_open_interval(
        self,
        state: OpenVoiceState,
        observed: ObservedVoiceState,
        *,
        quality: str,
    ) -> None:
        raise AssertionError("startup reconciliation must not use live move")

    async def reconcile_same_snapshot(
        self,
        state: OpenVoiceState,
        observed: ObservedVoiceState,
        *,
        exact_quality: str,
        estimated_quality: str,
    ) -> None:
        self.calls.append(
            (
                "reconcile_same",
                observed,
                state.confirmed_through_at,
                estimated_quality,
                exact_quality,
            )
        )
        self.current[observed.user_id] = replace(
            state,
            interval_id=self.next_interval_id,
            confirmed_through_at=observed.observed_at,
        )
        self.latest[observed.user_id] = observed.observed_at
        self.next_interval_id += 2

    async def close_open_state(
        self,
        state: OpenVoiceState,
        observed_at: datetime,
    ) -> None:
        user_id = next(
            user_id
            for user_id, current in self.current.items()
            if current.session_id == state.session_id
        )
        self.calls.append(("close", user_id, state.session_id, observed_at))
        self.current.pop(user_id)
        self.latest[user_id] = observed_at


def open_state(
    user_id: int,
    *,
    channel_id: int = 30,
    channel_kind: str = "voice",
    is_afk: bool = False,
) -> OpenVoiceState:
    return OpenVoiceState(
        session_id=100 + user_id,
        interval_id=200 + user_id,
        confirmed_through_at=H,
        channel_id=channel_id,
        channel_kind=channel_kind,
        is_afk=is_afk,
    )


def make_reconciler(repository: FakeVoiceRepository) -> VoiceStartupReconciler:
    session_factory = FakeSessionFactory()
    return VoiceStartupReconciler(
        session_factory,  # type: ignore[arg-type]
        repository_factory=lambda session: repository,  # type: ignore[arg-type]
    )


class InMemoryReferenceRepository:
    def __init__(self) -> None:
        self.guilds: dict[int, object] = {}
        self.users: dict[int, object] = {}
        self.members: dict[tuple[int, int], object] = {}
        self.channels: dict[int, object] = {}
        self.left_at: dict[tuple[int, int], datetime] = {}

    async def upsert_guild(self, guild: object) -> None:
        self.guilds[guild.id] = guild  # type: ignore[attr-defined]

    async def upsert_users(self, users: tuple[object, ...]) -> None:
        self.users.update((user.id, user) for user in users)  # type: ignore[attr-defined]

    async def upsert_members(self, members: tuple[object, ...]) -> None:
        for member in members:
            key = (member.guild_id, member.user_id)  # type: ignore[attr-defined]
            previous = self.members.get(key)
            if (
                previous is not None and not member.has_complete_guild_identity  # type: ignore[attr-defined]
            ):
                member = replace(  # type: ignore[call-overload]
                    member,
                    nickname=previous.nickname,  # type: ignore[attr-defined]
                    guild_avatar_hash=previous.guild_avatar_hash,  # type: ignore[attr-defined]
                )
            self.members[key] = member
            if member.has_complete_guild_identity:  # type: ignore[attr-defined]
                self.left_at.pop(key, None)

    async def upsert_voice_channels(self, channels: tuple[object, ...]) -> None:
        self.channels.update(
            (channel.id, channel)
            for channel in channels  # type: ignore[attr-defined]
        )

    async def mark_member_left(
        self,
        *,
        guild_id: int,
        user_id: int,
        left_at: datetime,
    ) -> None:
        self.left_at[(guild_id, user_id)] = left_at


def make_provisioner(
    repository: InMemoryReferenceRepository,
) -> GuildReferenceProvisioner:
    return GuildReferenceProvisioner(
        FakeSessionFactory(),  # type: ignore[arg-type]
        repository_factory=lambda session: repository,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_guild_cache_provisioning_includes_members_voice_and_stage_channels() -> (
    None
):
    repository = InMemoryReferenceRepository()
    provisioner = make_provisioner(repository)
    guild = FakeGuild(
        voice_channels=(FakeChannel(30, 1, 2),),
        stage_channels=(FakeChannel(31), FakeChannel(32)),
        afk_channel=None,
        bots=frozenset({2}),
    )

    first = await provisioner.provision_guild(guild)  # type: ignore[arg-type]
    second = await provisioner.provision_guild(guild)  # type: ignore[arg-type]

    assert first == second == runtime_module.GuildReferenceProvisioningSummary(2, 2, 3)
    assert len(repository.guilds) == 1
    assert set(repository.users) == {1, 2}
    assert repository.users[2].is_bot is True  # type: ignore[attr-defined]
    assert repository.users[1].username == "user-1"  # type: ignore[attr-defined]
    assert repository.users[1].global_name == "Global 1"  # type: ignore[attr-defined]
    assert repository.users[1].avatar_hash == "0123456789abcdef0123456789abcdef"  # type: ignore[attr-defined]
    assert set(repository.members) == {(10, 1), (10, 2)}
    assert repository.members[(10, 1)].nickname == "Guild 1"  # type: ignore[attr-defined]
    assert (
        repository.members[(10, 1)].guild_avatar_hash
        == "abcdef0123456789abcdef0123456789"
    )  # type: ignore[attr-defined]
    assert set(repository.channels) == {30, 31, 32}
    assert repository.channels[30].channel_kind == "voice"  # type: ignore[attr-defined]
    assert repository.channels[31].channel_kind == "stage"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_full_member_without_guild_avatar_keeps_global_avatar_only() -> None:
    repository = InMemoryReferenceRepository()
    provisioner = make_provisioner(repository)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    member.guild_avatar = None

    await provisioner.provision_member(member)  # type: ignore[arg-type]

    assert repository.users[1].avatar_hash == ("0123456789abcdef0123456789abcdef")  # type: ignore[attr-defined]
    assert repository.members[(10, 1)].guild_avatar_hash is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_targeted_member_provisioning_updates_and_clears_identity() -> None:
    repository = InMemoryReferenceRepository()
    provisioner = make_provisioner(repository)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None

    await provisioner.provision_member(member)  # type: ignore[arg-type]
    member.name = "renamed"
    member.global_name = None
    member.nick = None
    member.avatar = None
    member.guild_avatar = None
    await provisioner.provision_member(member)  # type: ignore[arg-type]

    assert repository.users[1].username == "renamed"  # type: ignore[attr-defined]
    assert repository.users[1].global_name is None  # type: ignore[attr-defined]
    assert repository.users[1].avatar_hash is None  # type: ignore[attr-defined]
    assert repository.members[(10, 1)].nickname is None  # type: ignore[attr-defined]
    assert repository.members[(10, 1)].guild_avatar_hash is None  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_partial_user_snapshot_does_not_erase_member_nickname() -> None:
    repository = InMemoryReferenceRepository()
    provisioner = make_provisioner(repository)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    await provisioner.provision_member(member)  # type: ignore[arg-type]
    await provisioner.mark_member_left(member, R)  # type: ignore[arg-type]
    partial_user = type(
        "PartialUser",
        (),
        {
            "id": 1,
            "bot": False,
            "name": "partial-name",
            "global_name": None,
            "avatar": None,
        },
    )()

    snapshot = runtime_module._member_reference_snapshot(  # noqa: SLF001
        guild,  # type: ignore[arg-type]
        partial_user,  # type: ignore[arg-type]
    )
    await runtime_module.ReferenceDataProvisioningService(  # noqa: SLF001
        repository
    ).provision_guild(snapshot)

    assert repository.users[1].username == "partial-name"  # type: ignore[attr-defined]
    assert repository.members[(10, 1)].nickname == "Guild 1"  # type: ignore[attr-defined]
    assert (
        repository.members[(10, 1)].guild_avatar_hash
        == "abcdef0123456789abcdef0123456789"
    )  # type: ignore[attr-defined]
    assert repository.users[1].avatar_hash is None  # type: ignore[attr-defined]
    assert repository.left_at[(10, 1)] == R


@pytest.mark.asyncio
async def test_remove_then_full_provisioning_reactivates_membership() -> None:
    repository = InMemoryReferenceRepository()
    provisioner = make_provisioner(repository)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None

    await provisioner.provision_member(member)  # type: ignore[arg-type]
    await provisioner.mark_member_left(member, R)  # type: ignore[arg-type]
    assert repository.left_at[(10, 1)] == R

    await provisioner.provision_member(member)  # type: ignore[arg-type]
    assert (10, 1) not in repository.left_at


@pytest.mark.asyncio
async def test_member_departure_persists_latest_avatar_identity_before_left_at() -> (
    None
):
    repository = InMemoryReferenceRepository()
    provisioner = make_provisioner(repository)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    member.avatar = SimpleNamespace(key="11111111111111111111111111111111")
    member.guild_avatar = SimpleNamespace(key="22222222222222222222222222222222")

    await provisioner.mark_member_left(member, R)  # type: ignore[arg-type]

    assert repository.users[1].avatar_hash == ("11111111111111111111111111111111")  # type: ignore[attr-defined]
    assert repository.members[(10, 1)].guild_avatar_hash == (
        "22222222222222222222222222222222"
    )  # type: ignore[attr-defined]
    assert repository.left_at[(10, 1)] == R


@pytest.mark.asyncio
async def test_connected_same_snapshot_uses_existing_reconciliation_service() -> None:
    repository = FakeVoiceRepository({1: open_state(1)})
    reconciler = make_reconciler(repository)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))

    summary = await reconciler.reconcile_guild(guild, R)  # type: ignore[arg-type]

    same_call = next(call for call in repository.calls if call[0] == "reconcile_same")
    observed = same_call[1]
    assert isinstance(observed, ObservedVoiceState)
    assert observed.channel_id == 30
    assert observed.channel_kind == "voice"
    assert observed.is_afk is False
    assert observed.observed_at == R
    assert repository.current[1].session_id == 101
    assert repository.current[1].confirmed_through_at == R
    assert summary.outcomes == {VoiceTransitionResult.UNCHANGED: 1}


@pytest.mark.asyncio
async def test_connected_changed_snapshot_starts_new_exact_session() -> None:
    repository = FakeVoiceRepository({1: open_state(1)})
    reconciler = make_reconciler(repository)
    stage = FakeChannel(40, 1)
    guild = FakeGuild(stage_channels=(stage,), afk_channel=stage)

    summary = await reconciler.reconcile_guild(guild, R)  # type: ignore[arg-type]

    assert ("close", 1, 101, H) in repository.calls
    create_call = next(call for call in repository.calls if call[0] == "create")
    observed = create_call[1]
    assert isinstance(observed, ObservedVoiceState)
    assert observed.channel_id == 40
    assert observed.channel_kind == "stage"
    assert observed.is_afk is True
    assert observed.observed_at == R
    assert repository.current[1].session_id != 101
    assert summary.outcomes == {VoiceTransitionResult.MOVED: 1}


@pytest.mark.asyncio
async def test_persisted_open_user_absent_from_voice_is_reconciled_disconnected() -> (
    None
):
    repository = FakeVoiceRepository({1: open_state(1)})
    reconciler = make_reconciler(repository)

    summary = await reconciler.reconcile_guild(FakeGuild(), R)  # type: ignore[arg-type]

    assert ("close", 1, 101, H) in repository.calls
    assert repository.current == {}
    assert summary.connected_count == 0
    assert summary.disconnected_count == 1
    assert summary.outcomes == {VoiceTransitionResult.LEFT: 1}


@pytest.mark.asyncio
async def test_repeated_startup_with_same_r_does_not_duplicate_intervals() -> None:
    repository = FakeVoiceRepository({1: open_state(1)})
    reconciler = make_reconciler(repository)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))

    await reconciler.reconcile_guild(guild, R)  # type: ignore[arg-type]
    await reconciler.reconcile_guild(guild, R)  # type: ignore[arg-type]

    assert sum(call[0] == "reconcile_same" for call in repository.calls) == 1
    assert repository.current[1].confirmed_through_at == R


@pytest.mark.asyncio
async def test_no_open_sessions_and_no_connected_members_is_no_op() -> None:
    repository = FakeVoiceRepository()
    reconciler = make_reconciler(repository)

    summary = await reconciler.reconcile_guild(FakeGuild(), R)  # type: ignore[arg-type]

    assert repository.calls == [("list_open", 10)]
    assert summary.connected_count == 0
    assert summary.disconnected_count == 0
    assert summary.outcomes == {}
    assert summary.failed_count == 0


@pytest.mark.asyncio
async def test_connected_member_without_open_session_joins_at_r() -> None:
    repository = FakeVoiceRepository()
    reconciler = make_reconciler(repository)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))

    summary = await reconciler.reconcile_guild(guild, R)  # type: ignore[arg-type]

    create_call = next(call for call in repository.calls if call[0] == "create")
    observed = create_call[1]
    assert isinstance(observed, ObservedVoiceState)
    assert observed.observed_at == R
    assert summary.outcomes == {VoiceTransitionResult.JOINED: 1}


@pytest.mark.asyncio
async def test_multiple_users_share_one_r_in_single_reconciliation_operation() -> None:
    repository = FakeVoiceRepository(
        {
            1: open_state(1),
            2: open_state(2, channel_id=31),
            3: open_state(3, channel_id=32),
        }
    )
    reconciler = make_reconciler(repository)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1, 2, 4),))

    summary = await reconciler.reconcile_guild(guild, R)  # type: ignore[arg-type]

    assert summary.connected_count == 3
    assert summary.disconnected_count == 1
    assert summary.outcomes == {
        VoiceTransitionResult.UNCHANGED: 1,
        VoiceTransitionResult.MOVED: 1,
        VoiceTransitionResult.JOINED: 1,
        VoiceTransitionResult.LEFT: 1,
    }
    timestamps = {
        call[1].observed_at
        for call in repository.calls
        if call[0] in {"create", "reconcile_same"}
    }
    close_boundaries = {call[3] for call in repository.calls if call[0] == "close"}
    assert timestamps == {R}
    assert close_boundaries == {H}
    assert {state.confirmed_through_at for state in repository.current.values()} == {R}


@pytest.mark.asyncio
async def test_all_connected_and_disconnected_service_calls_receive_one_r(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeVoiceRepository({1: open_state(1), 2: open_state(2)})
    connected_inputs: list[ObservedVoiceState] = []
    disconnected_inputs: list[tuple[int, int, datetime]] = []

    class RecordingService:
        async def reconcile_connected(
            self,
            observed: ObservedVoiceState,
        ) -> VoiceTransitionResult:
            connected_inputs.append(observed)
            return VoiceTransitionResult.UNCHANGED

        async def reconcile_disconnected(
            self,
            guild_id: int,
            user_id: int,
            reconciled_at: datetime,
        ) -> VoiceTransitionResult:
            disconnected_inputs.append((guild_id, user_id, reconciled_at))
            return VoiceTransitionResult.LEFT

    service = RecordingService()
    monkeypatch.setattr(
        runtime_module,
        "VoiceTrackingService",
        lambda repository: service,
    )
    reconciler = make_reconciler(repository)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1, 3),))

    summary = await reconciler.reconcile_guild(guild, R)  # type: ignore[arg-type]

    assert {observed.observed_at for observed in connected_inputs} == {R}
    assert disconnected_inputs == [(10, 2, R)]
    assert summary.reconciled_at == R


def test_gateway_intents_include_voice_and_members_without_message_content() -> None:
    intents = create_gateway_intents()

    assert intents.guilds is True
    assert intents.members is True
    assert intents.voice_states is True
    assert intents.message_content is False
    assert intents.presences is False


class RecordingReconciler:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls: list[tuple[object, datetime]] = []
        self.events = events

    async def reconcile_guild(
        self,
        guild: object,
        reconciled_at: datetime,
    ) -> VoiceStartupReconciliationSummary:
        if self.events is not None:
            self.events.append("reconcile")
        self.calls.append((guild, reconciled_at))
        return VoiceStartupReconciliationSummary(
            reconciled_at=reconciled_at,
            connected_count=0,
            disconnected_count=0,
            outcomes={},
            failed_count=0,
        )


class RecordingProvisioner:
    def __init__(
        self,
        events: list[str] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[object] = []
        self.events = events
        self.error = error

    async def provision_guild(self, guild: object) -> object:
        self.calls.append(guild)
        if self.events is not None:
            self.events.append("provision")
        if self.error is not None:
            raise self.error
        return runtime_module.GuildReferenceProvisioningSummary(0, 0, 0)


class NoOpVoiceEventHandler:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, datetime]] = []

    async def handle(
        self,
        member: object,
        after: object,
        observed_at: datetime,
    ) -> None:
        self.calls.append((member, after, observed_at))


class RecordingOperationalHealthRunner:
    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events
        self.starts = 0
        self.running = False

    def start(self, gateway: object) -> None:
        del gateway
        if self.running:
            return
        self.running = True
        self.starts += 1
        if self.events is not None:
            self.events.append("observations")

    async def stop(self) -> None:
        self.running = False


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)
        self.calls: list[datetime] = []

    def __call__(self) -> datetime:
        value = next(self._values)
        self.calls.append(value)
        return value


class HarnessDiscordClient(DiscordStatsClient):
    def __init__(
        self,
        guild: FakeGuild,
        reconciler: RecordingReconciler,
        provisioner: object | None = None,
        event_handler: object | None = None,
        clock: Callable[[], datetime] = lambda: R,
        operational_health_runner: object | None = None,
        rules_publication_syncer: object | None = None,
    ) -> None:
        self._test_guild = guild
        super().__init__(
            guild_id=guild.id,
            reference_provisioner=provisioner or RecordingProvisioner(),  # type: ignore[arg-type]
            voice_reconciler=reconciler,  # type: ignore[arg-type]
            voice_event_handler=event_handler or NoOpVoiceEventHandler(),  # type: ignore[arg-type]
            operational_health_runner=operational_health_runner,  # type: ignore[arg-type]
            rules_publication_syncer=rules_publication_syncer,  # type: ignore[arg-type]
            clock=clock,
        )

    def get_guild(self, guild_id: int) -> discord.Guild | None:
        if guild_id == self._test_guild.id:
            return self._test_guild  # type: ignore[return-value]
        return None


@pytest.mark.asyncio
async def test_repeated_on_ready_runs_serialized_operations_with_one_r_each() -> None:
    guild = FakeGuild()
    reconciler = RecordingReconciler()
    client = HarnessDiscordClient(guild, reconciler)

    await client.on_ready()
    await client.on_ready()

    assert reconciler.calls == [(guild, R), (guild, R)]


class RecordingRulesPublicationSyncer:
    def __init__(self) -> None:
        self.calls = 0

    async def sync(self) -> None:
        self.calls += 1


@pytest.mark.asyncio
async def test_repeated_ready_reconciles_rules_publication_idempotently() -> None:
    syncer = RecordingRulesPublicationSyncer()
    client = HarnessDiscordClient(
        FakeGuild(), RecordingReconciler(), rules_publication_syncer=syncer
    )

    await client.on_ready()
    await client.on_ready()

    assert syncer.calls == 2


@pytest.mark.asyncio
async def test_on_ready_provisions_before_startup_reconciliation() -> None:
    events: list[str] = []
    guild = FakeGuild()
    reconciler = RecordingReconciler(events)
    provisioner = RecordingProvisioner(events)
    client = HarnessDiscordClient(guild, reconciler, provisioner)

    await client.on_ready()

    assert events == ["provision", "reconcile"]


@pytest.mark.asyncio
async def test_operational_observations_start_only_after_successful_reconciliation() -> (
    None
):
    events: list[str] = []
    runner = RecordingOperationalHealthRunner(events)
    client = HarnessDiscordClient(
        FakeGuild(),
        RecordingReconciler(events),
        RecordingProvisioner(events),
        operational_health_runner=runner,
    )

    await client.on_ready()
    await client.on_ready()

    assert events == [
        "provision",
        "reconcile",
        "observations",
        "provision",
        "reconcile",
    ]
    assert runner.starts == 1


@pytest.mark.asyncio
async def test_failed_reconciliation_does_not_start_operational_observations() -> None:
    class FailedReconciler(RecordingReconciler):
        async def reconcile_guild(
            self, guild: object, reconciled_at: datetime
        ) -> VoiceStartupReconciliationSummary:
            del guild, reconciled_at
            raise RuntimeError("reconciliation failed")

    runner = RecordingOperationalHealthRunner()
    client = HarnessDiscordClient(
        FakeGuild(),
        FailedReconciler(),
        operational_health_runner=runner,
    )

    await client.on_ready()

    assert runner.starts == 0


@pytest.mark.asyncio
async def test_on_ready_skips_reconciliation_when_provisioning_fails() -> None:
    guild = FakeGuild()
    reconciler = RecordingReconciler()
    provisioner = RecordingProvisioner(error=RuntimeError("provision failed"))
    client = HarnessDiscordClient(guild, reconciler, provisioner)

    await client.on_ready()

    assert provisioner.calls == [guild]
    assert reconciler.calls == []


@pytest.mark.asyncio
async def test_on_ready_reconciles_multiple_connected_users_after_provisioning() -> (
    None
):
    references = InMemoryReferenceRepository()

    class ProvisioningAwareVoiceRepository(FakeVoiceRepository):
        async def lock_member(self, guild_id: int, user_id: int) -> bool:
            self.calls.append(("lock", guild_id, user_id))
            return (guild_id, user_id) in references.members

    voice_repository = ProvisioningAwareVoiceRepository()
    guild = FakeGuild(
        voice_channels=(FakeChannel(30, 1),),
        stage_channels=(FakeChannel(31, 2),),
    )
    client = HarnessDiscordClient(
        guild,
        make_reconciler(voice_repository),  # type: ignore[arg-type]
        make_provisioner(references),
    )

    await client.on_ready()

    assert set(voice_repository.current) == {1, 2}
    assert sum(call[0] == "create" for call in voice_repository.calls) == 2
    assert set(references.members) == {(10, 1), (10, 2)}


@pytest.mark.asyncio
async def test_client_forwards_live_event_only_after_successful_startup() -> None:
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    after = object()
    event_handler = NoOpVoiceEventHandler()
    event_at = R + timedelta(seconds=1)
    client = HarnessDiscordClient(
        guild,
        RecordingReconciler(),
        event_handler=event_handler,
        clock=SequenceClock(R, event_at),
    )

    await client.on_ready()
    await client.on_voice_state_update(member, object(), after)  # type: ignore[arg-type]

    assert event_handler.calls == [(member, after, event_at)]


class BlockingReconciler(RecordingReconciler):
    def __init__(self, *, error: Exception | None = None) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.error = error

    async def reconcile_guild(
        self,
        guild: object,
        reconciled_at: datetime,
    ) -> VoiceStartupReconciliationSummary:
        self.calls.append((guild, reconciled_at))
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error
        return VoiceStartupReconciliationSummary(
            reconciled_at=reconciled_at,
            connected_count=0,
            disconnected_count=0,
            outcomes={},
            failed_count=0,
        )


@pytest.mark.parametrize("event_at", [R - timedelta(seconds=1), R])
@pytest.mark.asyncio
async def test_queued_event_covered_by_startup_baseline_is_discarded(
    event_at: datetime,
) -> None:
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    handler = NoOpVoiceEventHandler()
    clock = SequenceClock(event_at, R)
    client = HarnessDiscordClient(
        guild,
        RecordingReconciler(),
        event_handler=handler,
        clock=clock,
    )

    event_task = asyncio.create_task(
        client.on_voice_state_update(member, object(), object())  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    assert clock.calls == [event_at]
    assert event_task.done() is False

    await client.on_ready()
    await event_task

    assert handler.calls == []


@pytest.mark.asyncio
async def test_event_after_baseline_keeps_arrival_time_while_startup_finishes() -> None:
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    reconciler = BlockingReconciler()
    handler = NoOpVoiceEventHandler()
    event_at = R + timedelta(seconds=1)
    clock = SequenceClock(R, event_at)
    client = HarnessDiscordClient(
        guild,
        reconciler,
        event_handler=handler,
        clock=clock,
    )

    ready_task = asyncio.create_task(client.on_ready())
    await reconciler.started.wait()
    event_task = asyncio.create_task(
        client.on_voice_state_update(member, object(), object())  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)

    assert clock.calls == [R, event_at]
    assert handler.calls == []
    assert event_task.done() is False

    reconciler.release.set()
    await ready_task
    await event_task

    assert handler.calls[0][2] == event_at


@pytest.mark.asyncio
async def test_failed_startup_does_not_release_queued_live_event() -> None:
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    reconciler = BlockingReconciler(error=RuntimeError("reconciliation failed"))
    handler = NoOpVoiceEventHandler()
    client = HarnessDiscordClient(
        guild,
        reconciler,
        event_handler=handler,
        clock=SequenceClock(R, R + timedelta(seconds=1)),
    )

    ready_task = asyncio.create_task(client.on_ready())
    await reconciler.started.wait()
    event_task = asyncio.create_task(
        client.on_voice_state_update(member, object(), object())  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    reconciler.release.set()
    await ready_task
    await asyncio.sleep(0)

    assert event_task.done() is False
    assert handler.calls == []
    event_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await event_task


@pytest.mark.asyncio
async def test_repeated_on_ready_replaces_successful_startup_baseline() -> None:
    first_r = R
    second_r = R + timedelta(minutes=1)
    event_between_baselines = R + timedelta(seconds=30)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    reconciler = RecordingReconciler()
    handler = NoOpVoiceEventHandler()
    client = HarnessDiscordClient(
        guild,
        reconciler,
        event_handler=handler,
        clock=SequenceClock(first_r, second_r, event_between_baselines),
    )

    await client.on_ready()
    await client.on_ready()
    await client.on_voice_state_update(member, object(), object())  # type: ignore[arg-type]

    assert [call[1] for call in reconciler.calls] == [first_r, second_r]
    assert handler.calls == []


@pytest.mark.asyncio
async def test_on_disconnect_closes_live_gate() -> None:
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    handler = NoOpVoiceEventHandler()
    client = HarnessDiscordClient(
        guild,
        RecordingReconciler(),
        event_handler=handler,
        clock=SequenceClock(R, R + timedelta(seconds=1)),
    )
    await client.on_ready()

    await client.on_disconnect()
    event_task = asyncio.create_task(
        client.on_voice_state_update(member, object(), object())  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)

    assert event_task.done() is False
    assert handler.calls == []
    event_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await event_task


@pytest.mark.asyncio
async def test_successful_on_resumed_runs_recovery_and_reopens_live_gate() -> None:
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    provisioner = RecordingProvisioner()
    reconciler = RecordingReconciler()
    handler = NoOpVoiceEventHandler()
    event_at = R + timedelta(seconds=1)
    after = object()
    client = HarnessDiscordClient(
        guild,
        reconciler,
        provisioner,
        handler,
        SequenceClock(R, event_at),
    )

    await client.on_disconnect()
    await client.on_resumed()
    await client.on_voice_state_update(member, object(), after)  # type: ignore[arg-type]

    assert provisioner.calls == [guild]
    assert reconciler.calls == [(guild, R)]
    assert handler.calls == [(member, after, event_at)]


@pytest.mark.asyncio
async def test_on_resumed_publishes_a_new_baseline() -> None:
    first_r = R
    resumed_r = R + timedelta(minutes=1)
    event_between = R + timedelta(seconds=30)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    reconciler = RecordingReconciler()
    handler = NoOpVoiceEventHandler()
    client = HarnessDiscordClient(
        guild,
        reconciler,
        event_handler=handler,
        clock=SequenceClock(first_r, resumed_r, event_between),
    )

    await client.on_ready()
    await client.on_disconnect()
    await client.on_resumed()
    await client.on_voice_state_update(member, object(), object())  # type: ignore[arg-type]

    assert [call[1] for call in reconciler.calls] == [first_r, resumed_r]
    assert handler.calls == []


@pytest.mark.asyncio
async def test_failed_on_resumed_recovery_leaves_live_gate_closed() -> None:
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    reconciler = BlockingReconciler(error=RuntimeError("resume recovery failed"))
    reconciler.release.set()
    handler = NoOpVoiceEventHandler()
    client = HarnessDiscordClient(
        guild,
        reconciler,
        event_handler=handler,
        clock=SequenceClock(R, R + timedelta(seconds=1)),
    )

    await client.on_disconnect()
    await client.on_resumed()
    event_task = asyncio.create_task(
        client.on_voice_state_update(member, object(), object())  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)

    assert event_task.done() is False
    assert handler.calls == []
    event_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await event_task


@pytest.mark.asyncio
async def test_queued_event_after_disconnect_uses_new_resume_baseline() -> None:
    first_r = R
    event_at = R + timedelta(seconds=30)
    resumed_r = R + timedelta(minutes=1)
    guild = FakeGuild(voice_channels=(FakeChannel(30, 1),))
    member = guild.get_member(1)
    assert member is not None
    handler = NoOpVoiceEventHandler()
    client = HarnessDiscordClient(
        guild,
        RecordingReconciler(),
        event_handler=handler,
        clock=SequenceClock(first_r, event_at, resumed_r),
    )
    await client.on_ready()
    await client.on_disconnect()

    event_task = asyncio.create_task(
        client.on_voice_state_update(member, object(), object())  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    assert event_task.done() is False

    await client.on_resumed()
    await event_task

    assert handler.calls == []


@pytest.mark.asyncio
async def test_on_ready_and_on_resumed_serialize_shared_recovery() -> None:
    first_r = R
    resumed_r = R + timedelta(minutes=1)
    guild = FakeGuild()
    provisioner = RecordingProvisioner()
    reconciler = BlockingReconciler()
    client = HarnessDiscordClient(
        guild,
        reconciler,
        provisioner,
        clock=SequenceClock(first_r, resumed_r),
    )

    ready_task = asyncio.create_task(client.on_ready())
    await reconciler.started.wait()
    resumed_task = asyncio.create_task(client.on_resumed())
    await asyncio.sleep(0)

    assert len(provisioner.calls) == 1
    assert len(reconciler.calls) == 1

    reconciler.release.set()
    await ready_task
    await resumed_task

    assert provisioner.calls == [guild, guild]
    assert [call[1] for call in reconciler.calls] == [first_r, resumed_r]
