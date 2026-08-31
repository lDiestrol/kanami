from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from discord_stats_bot.discord import VoiceStateEventHandler
from discord_stats_bot.features.voice import (
    ObservedVoiceState,
    OpenVoiceState,
    VoiceTransitionResult,
)

T0 = datetime(2026, 8, 13, 10, 0, tzinfo=UTC)


class FakeTransaction:
    def __init__(self, factory: "FakeSessionFactory") -> None:
        self.factory = factory
        self.session = object()

    async def __aenter__(self) -> object:
        self.factory.entered += 1
        self.factory.sessions.append(self.session)
        return self.session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        self.factory.exited += 1
        if exc_type is not None:
            self.factory.failed += 1


class FakeSessionFactory:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0
        self.failed = 0
        self.sessions: list[object] = []

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)


class FakeReferenceRepository:
    def __init__(self) -> None:
        self.guilds: dict[int, object] = {}
        self.users: dict[int, object] = {}
        self.members: dict[tuple[int, int], object] = {}
        self.channels: dict[int, object] = {}
        self.factory_sessions: list[object] = []

    async def upsert_guild(self, guild: object) -> None:
        self.guilds[guild.id] = guild  # type: ignore[attr-defined]

    async def upsert_users(self, users: tuple[object, ...]) -> None:
        self.users.update((user.id, user) for user in users)  # type: ignore[attr-defined]

    async def upsert_members(self, members: tuple[object, ...]) -> None:
        self.members.update(
            ((member.guild_id, member.user_id), member)  # type: ignore[attr-defined]
            for member in members
        )

    async def upsert_voice_channels(self, channels: tuple[object, ...]) -> None:
        self.channels.update(
            (channel.id, channel)  # type: ignore[attr-defined]
            for channel in channels
        )


class FakeVoiceRepository:
    def __init__(self, references: FakeReferenceRepository) -> None:
        self.references = references
        self.current: dict[int, OpenVoiceState] = {}
        self.latest: dict[int, datetime] = {}
        self.calls: list[tuple[object, ...]] = []
        self.next_session_id = 100
        self.next_interval_id = 200
        self.factory_sessions: list[object] = []

    async def lock_member(self, guild_id: int, user_id: int) -> bool:
        self.calls.append(("lock", guild_id, user_id))
        return (guild_id, user_id) in self.references.members

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
        return self.latest.get(user_id)

    async def create_open_state(
        self,
        observed: ObservedVoiceState,
        *,
        quality: str,
    ) -> None:
        self.calls.append(("create", observed, quality))
        self.current[observed.user_id] = OpenVoiceState(
            self.next_session_id,
            self.next_interval_id,
            observed.observed_at,
            observed.channel_id,
            observed.channel_kind,
            observed.is_afk,
        )
        self.latest[observed.user_id] = observed.observed_at
        self.next_session_id += 1
        self.next_interval_id += 1

    async def advance_confirmation(
        self,
        state: OpenVoiceState,
        observed_at: datetime,
    ) -> None:
        self.calls.append(("advance", state.session_id, observed_at))
        user_id = self._user_id_for(state)
        self.current[user_id] = replace(state, confirmed_through_at=observed_at)
        self.latest[user_id] = observed_at

    async def move_open_interval(
        self,
        state: OpenVoiceState,
        observed: ObservedVoiceState,
        *,
        quality: str,
    ) -> None:
        self.calls.append(("move", state.session_id, observed, quality))
        self.current[observed.user_id] = OpenVoiceState(
            state.session_id,
            self.next_interval_id,
            observed.observed_at,
            observed.channel_id,
            observed.channel_kind,
            observed.is_afk,
        )
        self.latest[observed.user_id] = observed.observed_at
        self.next_interval_id += 1

    async def reconcile_same_snapshot(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("live adapter must use live service operations")

    async def close_open_state(
        self,
        state: OpenVoiceState,
        observed_at: datetime,
    ) -> None:
        user_id = self._user_id_for(state)
        self.calls.append(("close", user_id, state.session_id, observed_at))
        self.current.pop(user_id)
        self.latest[user_id] = observed_at

    def _user_id_for(self, state: OpenVoiceState) -> int:
        return next(
            user_id
            for user_id, current in self.current.items()
            if current.session_id == state.session_id
        )


class FakeChannel:
    def __init__(
        self,
        channel_id: int,
        *,
        channel_type: discord.ChannelType = discord.ChannelType.voice,
        name: str | None = None,
    ) -> None:
        self.id = channel_id
        self.type = channel_type
        self.name = name or f"channel-{channel_id}"


class FakeGuild:
    def __init__(self, guild_id: int = 10, *, afk_channel: FakeChannel | None = None):
        self.id = guild_id
        self.name = f"guild-{guild_id}"
        self.afk_channel = afk_channel


class FakeMember:
    def __init__(
        self,
        user_id: int,
        guild: FakeGuild,
        *,
        bot: bool = False,
    ) -> None:
        self.id = user_id
        self.guild = guild
        self.bot = bot
        self.joined_at = T0 - timedelta(days=1)


def voice_state(channel: FakeChannel | None, **flags: bool) -> object:
    return SimpleNamespace(channel=channel, **flags)


def make_handler() -> tuple[
    VoiceStateEventHandler,
    FakeSessionFactory,
    FakeReferenceRepository,
    FakeVoiceRepository,
]:
    sessions = FakeSessionFactory()
    references = FakeReferenceRepository()
    voices = FakeVoiceRepository(references)
    handler = VoiceStateEventHandler(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        voice_repository_factory=lambda session: (
            voices.factory_sessions.append(session) or voices
        ),
        reference_repository_factory=lambda session: (
            references.factory_sessions.append(session) or references
        ),
    )
    return handler, sessions, references, voices


@pytest.mark.asyncio
async def test_join_uses_existing_service_and_one_transaction() -> None:
    handler, sessions, references, voices = make_handler()
    guild = FakeGuild()
    member = FakeMember(20, guild)
    channel = FakeChannel(30)

    result = await handler.handle(  # type: ignore[arg-type]
        member, voice_state(channel), T0
    )

    assert result is VoiceTransitionResult.JOINED
    assert sessions.entered == sessions.exited == 1
    assert sessions.failed == 0
    assert references.factory_sessions == voices.factory_sessions == sessions.sessions
    assert set(references.members) == {(10, 20)}
    assert set(references.channels) == {30}
    create = next(call for call in voices.calls if call[0] == "create")
    observed = create[1]
    assert isinstance(observed, ObservedVoiceState)
    assert observed.observed_at == T0
    assert observed.channel_kind == "voice"


@pytest.mark.asyncio
async def test_move_between_voice_channels_keeps_logical_session() -> None:
    handler, sessions, _, voices = make_handler()
    member = FakeMember(20, FakeGuild())
    await handler.handle(  # type: ignore[arg-type]
        member, voice_state(FakeChannel(30)), T0
    )
    original_session_id = voices.current[20].session_id

    result = await handler.handle(  # type: ignore[arg-type]
        member,
        voice_state(FakeChannel(31)),
        T0 + timedelta(seconds=1),
    )

    assert result is VoiceTransitionResult.MOVED
    assert voices.current[20].session_id == original_session_id
    assert voices.current[20].channel_id == 31
    assert sessions.entered == sessions.exited == 2


@pytest.mark.asyncio
async def test_leave_closes_interval_and_session_through_existing_service() -> None:
    handler, _, _, voices = make_handler()
    member = FakeMember(20, FakeGuild())
    await handler.handle(  # type: ignore[arg-type]
        member, voice_state(FakeChannel(30)), T0
    )

    result = await handler.handle(  # type: ignore[arg-type]
        member, voice_state(None), T0 + timedelta(seconds=1)
    )

    assert result is VoiceTransitionResult.LEFT
    assert 20 not in voices.current
    assert any(call[0] == "close" for call in voices.calls)


@pytest.mark.parametrize(
    ("first_type", "second_type", "expected"),
    [
        (discord.ChannelType.voice, discord.ChannelType.stage_voice, "stage"),
        (discord.ChannelType.stage_voice, discord.ChannelType.voice, "voice"),
    ],
)
@pytest.mark.asyncio
async def test_voice_and_stage_transitions_are_classified(
    first_type: discord.ChannelType,
    second_type: discord.ChannelType,
    expected: str,
) -> None:
    handler, _, _, voices = make_handler()
    member = FakeMember(20, FakeGuild())
    await handler.handle(  # type: ignore[arg-type]
        member,
        voice_state(FakeChannel(30, channel_type=first_type)),
        T0,
    )

    result = await handler.handle(  # type: ignore[arg-type]
        member,
        voice_state(FakeChannel(31, channel_type=second_type)),
        T0 + timedelta(seconds=1),
    )

    assert result is VoiceTransitionResult.MOVED
    assert voices.current[20].channel_kind == expected


@pytest.mark.asyncio
async def test_afk_channel_metadata_is_snapshotted() -> None:
    afk = FakeChannel(30)
    handler, _, references, voices = make_handler()
    member = FakeMember(20, FakeGuild(afk_channel=afk))

    await handler.handle(member, voice_state(afk), T0)  # type: ignore[arg-type]

    assert voices.current[20].is_afk is True
    assert references.channels[30].is_afk is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "member",
    [FakeMember(20, FakeGuild(), bot=True), FakeMember(20, FakeGuild(11))],
)
@pytest.mark.asyncio
async def test_bot_and_other_guild_events_are_ignored(member: FakeMember) -> None:
    handler, sessions, references, voices = make_handler()

    result = await handler.handle(  # type: ignore[arg-type]
        member,
        voice_state(FakeChannel(30)),
        T0,
    )

    assert result is None
    assert sessions.entered == 0
    assert references.members == {}
    assert voices.calls == []


@pytest.mark.asyncio
async def test_non_channel_voice_state_change_is_idempotent() -> None:
    handler, _, _, voices = make_handler()
    member = FakeMember(20, FakeGuild())
    channel = FakeChannel(30)
    await handler.handle(  # type: ignore[arg-type]
        member, voice_state(channel, self_mute=False), T0
    )

    result = await handler.handle(  # type: ignore[arg-type]
        member,
        voice_state(channel, self_mute=True, self_video=True),
        T0 + timedelta(seconds=1),
    )

    assert result is VoiceTransitionResult.UNCHANGED
    assert sum(call[0] == "create" for call in voices.calls) == 1
    assert sum(call[0] == "move" for call in voices.calls) == 0
    assert sum(call[0] == "advance" for call in voices.calls) == 1


@pytest.mark.asyncio
async def test_multiple_users_have_independent_sessions() -> None:
    handler, sessions, _, voices = make_handler()
    guild = FakeGuild()

    first = await handler.handle(  # type: ignore[arg-type]
        FakeMember(20, guild), voice_state(FakeChannel(30)), T0
    )
    second = await handler.handle(  # type: ignore[arg-type]
        FakeMember(21, guild),
        voice_state(FakeChannel(31)),
        T0 + timedelta(seconds=1),
    )

    assert (first, second) == (
        VoiceTransitionResult.JOINED,
        VoiceTransitionResult.JOINED,
    )
    assert set(voices.current) == {20, 21}
    assert voices.current[20].session_id != voices.current[21].session_id
    assert sessions.entered == sessions.exited == 2


@pytest.mark.asyncio
async def test_transition_failure_is_logged_and_does_not_escape(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler, sessions, _, voices = make_handler()
    member = FakeMember(20, FakeGuild())

    original_lock = voices.lock_member

    async def fail_one_lock(guild_id: int, user_id: int) -> bool:
        if user_id == 20:
            raise RuntimeError("database unavailable")
        return await original_lock(guild_id, user_id)

    voices.lock_member = fail_one_lock  # type: ignore[method-assign]

    result = await handler.handle(  # type: ignore[arg-type]
        member,
        voice_state(FakeChannel(30)),
        T0,
    )

    assert result is None
    assert sessions.entered == sessions.exited == sessions.failed == 1
    assert "guild_id=10 user_id=20" in caplog.text
    assert "database unavailable" in caplog.text

    next_result = await handler.handle(  # type: ignore[arg-type]
        FakeMember(21, member.guild),
        voice_state(FakeChannel(31)),
        T0 + timedelta(seconds=1),
    )

    assert next_result is VoiceTransitionResult.JOINED
    assert set(voices.current) == {21}
    assert sessions.entered == sessions.exited == 2
