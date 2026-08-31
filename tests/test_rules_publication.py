from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from discord_stats_bot.discord.rules import (
    RULES_ACCEPT_BUTTON_CUSTOM_ID,
    RulesCommandHandler,
)
from discord_stats_bot.discord.rules_publication import RulesPublicationService
from discord_stats_bot.features.rules import (
    RulesetRecord,
    RulesetStatus,
    RulesPublicationConfigurationStatus,
    RulesPublicationState,
    RulesPublicationSyncStatus,
)

T0 = datetime(2026, 8, 26, 12, tzinfo=UTC)


class Context:
    async def __aenter__(self) -> object:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class FakeSession(Context):
    def begin(self) -> Context:
        return Context()


class FakeSessionFactory:
    def __call__(self) -> FakeSession:
        return FakeSession()


class FakeRulesRepository:
    def __init__(self, ruleset: RulesetRecord | None) -> None:
        self.ruleset = ruleset

    async def get_current_published(self, guild_id: int) -> RulesetRecord | None:
        return self.ruleset


class FakePublicationRepository:
    def __init__(self, state: RulesPublicationState) -> None:
        self.state = state
        self.saves: list[tuple[int, int]] = []
        self.save_error: Exception | None = None
        self.configuration_error: Exception | None = None

    async def get(self, guild_id: int) -> RulesPublicationState:
        return self.state

    async def save_delivery(
        self, *, guild_id: int, message_id: int, ruleset_id: int
    ) -> None:
        self.saves.append((message_id, ruleset_id))
        if self.save_error is not None:
            raise self.save_error
        self.state = RulesPublicationState(
            guild_id, self.state.channel_id, message_id, ruleset_id
        )

    async def save_configuration(
        self, *, guild_id: int, channel_id: int | None
    ) -> None:
        if self.configuration_error is not None:
            raise self.configuration_error
        self.state = RulesPublicationState(guild_id, channel_id, None, None)


class FakeMessage:
    def __init__(self, message_id: int, channel: "FakeChannel | None" = None) -> None:
        self.id = message_id
        self.channel = channel
        self.edits: list[dict[str, object]] = []
        self.edit_error: Exception | None = None
        self.delete_calls = 0
        self.delete_error: Exception | None = None

    async def edit(self, **payload: object) -> None:
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(payload)

    async def delete(self) -> None:
        self.delete_calls += 1
        if self.delete_error is not None:
            raise self.delete_error
        if self.channel is not None:
            self.channel.messages.pop(self.id, None)


class FakeChannel:
    def __init__(
        self,
        channel_id: int,
        guild_id: int = 10,
        *,
        channel_type: discord.ChannelType = discord.ChannelType.text,
        can_deliver: bool = True,
    ) -> None:
        self.id = channel_id
        self.guild = SimpleNamespace(id=guild_id)
        self.type = channel_type
        self.name = f"channel-{channel_id}"
        self.position = channel_id
        self.can_deliver = can_deliver
        self.messages: dict[int, FakeMessage] = {}
        self.sends: list[dict[str, object]] = []
        self.sent_messages: list[FakeMessage] = []
        self.next_message_id = 100
        self.send_error: Exception | None = None
        self.message_delete_error: Exception | None = None

    async def send(self, **payload: object) -> FakeMessage:
        if self.send_error is not None:
            raise self.send_error
        self.sends.append(payload)
        message = FakeMessage(self.next_message_id, self)
        message.delete_error = self.message_delete_error
        self.next_message_id += 1
        self.messages[message.id] = message
        self.sent_messages.append(message)
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        if message_id not in self.messages:
            raise discord.NotFound(_response(404), "deleted")
        return self.messages[message_id]

    def permissions_for(self, member: object) -> object:
        del member
        return SimpleNamespace(
            view_channel=self.can_deliver,
            send_messages=self.can_deliver,
            embed_links=self.can_deliver,
        )


class FakeGuild:
    def __init__(self, guild_id: int, channels: tuple[FakeChannel, ...]) -> None:
        self.id = guild_id
        self.channels = channels
        self.roles: tuple[object, ...] = ()
        self.me = SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=False))

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return next(
            (channel for channel in self.channels if channel.id == channel_id), None
        )


class FakeClient:
    def __init__(self, channel: FakeChannel | tuple[FakeChannel, ...] | None) -> None:
        self.channels = (
            channel
            if isinstance(channel, tuple)
            else (() if channel is None else (channel,))
        )
        self.guild = FakeGuild(10, self.channels)

    def is_ready(self) -> bool:
        return True

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        return self.guild if guild_id == self.guild.id else None

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.guild.get_channel(channel_id)

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        channel = self.get_channel(channel_id)
        if channel is None:
            raise discord.NotFound(_response(404), "missing channel")
        return channel


def _response(status: int) -> object:
    return SimpleNamespace(status=status, reason="test", headers={})


def _ruleset(ruleset_id: int = 1, version: str = "1.0") -> RulesetRecord:
    return RulesetRecord(
        ruleset_id,
        10,
        version,
        "Правила сервера",
        "Уважайте друг друга.",
        RulesetStatus.PUBLISHED,
        None,
        False,
        None,
        T0,
        T0,
    )


def _service(
    state: RulesPublicationState,
    rules: FakeRulesRepository,
    publication: FakePublicationRepository,
    channel: FakeChannel | tuple[FakeChannel, ...] | None,
) -> RulesPublicationService:
    sessions = FakeSessionFactory()
    handler = RulesCommandHandler(sessions, guild_id=10)  # type: ignore[arg-type]
    service = RulesPublicationService(
        sessions,  # type: ignore[arg-type]
        handler,
        guild_id=10,
        rules_repository_factory=lambda session: rules,  # type: ignore[arg-type]
        publication_repository_factory=lambda session: publication,
    )
    service.bind_client(FakeClient(channel))  # type: ignore[arg-type]
    return service


@pytest.mark.asyncio
async def test_disabled_publication_is_a_noop() -> None:
    state = RulesPublicationState(10, None, None, None)
    publication = FakePublicationRepository(state)
    result = await _service(
        state, FakeRulesRepository(_ruleset()), publication, None
    ).sync()

    assert result.status is RulesPublicationSyncStatus.NOT_CONFIGURED
    assert publication.saves == []


@pytest.mark.asyncio
async def test_configured_publication_without_current_rules_is_a_noop() -> None:
    state = RulesPublicationState(10, 20, None, None)
    publication = FakePublicationRepository(state)
    channel = FakeChannel(20)

    result = await _service(
        state, FakeRulesRepository(None), publication, channel
    ).sync()

    assert result.status is RulesPublicationSyncStatus.NO_PUBLISHED_RULESET
    assert channel.sends == []


@pytest.mark.asyncio
async def test_configure_first_channel_and_same_channel_preserves_cursor() -> None:
    initial = RulesPublicationState(10, None, None, None)
    publication = FakePublicationRepository(initial)
    channel = FakeChannel(20)
    service = _service(initial, FakeRulesRepository(_ruleset()), publication, channel)

    configured = await service.configure(20, actor_discord_user_id=41)
    publication.state = RulesPublicationState(10, 20, 90, 1)
    unchanged = await service.configure(20, actor_discord_user_id=41)

    assert configured.status is RulesPublicationConfigurationStatus.CONFIGURED
    assert publication.state == RulesPublicationState(10, 20, 90, 1)
    assert unchanged.status is RulesPublicationConfigurationStatus.ALREADY_CONFIGURED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "channel",
    [
        FakeChannel(20, guild_id=999),
        FakeChannel(20, channel_type=discord.ChannelType.voice),
        FakeChannel(20, can_deliver=False),
    ],
)
async def test_configure_rejects_other_guild_and_unsupported_channels(
    channel: FakeChannel,
) -> None:
    state = RulesPublicationState(10, None, None, None)
    publication = FakePublicationRepository(state)

    result = await _service(
        state, FakeRulesRepository(_ruleset()), publication, channel
    ).configure(20, actor_discord_user_id=41)

    assert result.status is RulesPublicationConfigurationStatus.INVALID_CHANNEL
    assert publication.state == state


@pytest.mark.asyncio
async def test_channel_change_deletes_old_message_and_clears_cursor() -> None:
    state = RulesPublicationState(10, 20, 90, 1)
    publication = FakePublicationRepository(state)
    old_channel = FakeChannel(20)
    new_channel = FakeChannel(21)
    old_message = FakeMessage(90, old_channel)
    old_channel.messages[90] = old_message

    result = await _service(
        state,
        FakeRulesRepository(_ruleset()),
        publication,
        (old_channel, new_channel),
    ).configure(21, actor_discord_user_id=41)

    assert result.status is RulesPublicationConfigurationStatus.CHANGED
    assert old_message.delete_calls == 1
    assert old_channel.messages == {}
    assert publication.state == RulesPublicationState(10, 21, None, None)


@pytest.mark.asyncio
async def test_channel_change_succeeds_when_old_message_is_already_deleted() -> None:
    state = RulesPublicationState(10, 20, 90, 1)
    publication = FakePublicationRepository(state)

    result = await _service(
        state,
        FakeRulesRepository(_ruleset()),
        publication,
        (FakeChannel(20), FakeChannel(21)),
    ).configure(21, actor_discord_user_id=41)

    assert result.status is RulesPublicationConfigurationStatus.CHANGED
    assert publication.state == RulesPublicationState(10, 21, None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            discord.Forbidden(_response(403), "forbidden"),
            RulesPublicationConfigurationStatus.CLEANUP_FORBIDDEN,
        ),
        (
            discord.HTTPException(_response(503), "temporary"),
            RulesPublicationConfigurationStatus.CLEANUP_DISCORD_API_FAILURE,
        ),
    ],
)
async def test_channel_change_cleanup_failure_preserves_configuration(
    error: Exception, expected: RulesPublicationConfigurationStatus
) -> None:
    state = RulesPublicationState(10, 20, 90, 1)
    publication = FakePublicationRepository(state)
    old_channel = FakeChannel(20)
    old_message = FakeMessage(90, old_channel)
    old_message.delete_error = error
    old_channel.messages[90] = old_message

    result = await _service(
        state,
        FakeRulesRepository(_ruleset()),
        publication,
        (old_channel, FakeChannel(21)),
    ).configure(21, actor_discord_user_id=41)

    assert result.status is expected
    assert publication.state == state


@pytest.mark.asyncio
async def test_disable_deletes_message_and_clears_all_state() -> None:
    state = RulesPublicationState(10, 20, 90, 1)
    publication = FakePublicationRepository(state)
    channel = FakeChannel(20)
    message = FakeMessage(90, channel)
    channel.messages[90] = message

    result = await _service(
        state, FakeRulesRepository(_ruleset()), publication, channel
    ).disable(actor_discord_user_id=41)

    assert result.status is RulesPublicationConfigurationStatus.DISABLED
    assert message.delete_calls == 1
    assert publication.state == RulesPublicationState(10, None, None, None)


@pytest.mark.asyncio
async def test_disable_cleanup_failure_preserves_old_state() -> None:
    state = RulesPublicationState(10, 20, 90, 1)
    publication = FakePublicationRepository(state)
    channel = FakeChannel(20)
    message = FakeMessage(90, channel)
    message.delete_error = discord.Forbidden(_response(403), "forbidden")
    channel.messages[90] = message

    result = await _service(
        state, FakeRulesRepository(_ruleset()), publication, channel
    ).disable(actor_discord_user_id=41)

    assert result.status is RulesPublicationConfigurationStatus.CLEANUP_FORBIDDEN
    assert publication.state == state


@pytest.mark.asyncio
async def test_configuration_db_failure_after_cleanup_is_recoverable() -> None:
    state = RulesPublicationState(10, 20, 90, 1)
    publication = FakePublicationRepository(state)
    database_error = RuntimeError("database unavailable")
    publication.configuration_error = database_error
    old_channel = FakeChannel(20)
    message = FakeMessage(90, old_channel)
    old_channel.messages[90] = message

    with pytest.raises(RuntimeError) as captured:
        await _service(
            state,
            FakeRulesRepository(_ruleset()),
            publication,
            (old_channel, FakeChannel(21)),
        ).configure(21, actor_discord_user_id=41)

    assert captured.value is database_error
    assert old_channel.messages == {}
    assert publication.state == state


@pytest.mark.asyncio
async def test_first_and_repeated_sync_create_once_and_reuse_accept_view() -> None:
    state = RulesPublicationState(10, 20, None, None)
    publication = FakePublicationRepository(state)
    channel = FakeChannel(20)
    service = _service(state, FakeRulesRepository(_ruleset()), publication, channel)

    created = await service.sync()
    current = await service.sync()

    assert created.status is RulesPublicationSyncStatus.CREATED
    assert current.status is RulesPublicationSyncStatus.ALREADY_CURRENT
    assert publication.saves == [(100, 1)]
    assert len(channel.sends) == 1
    view = channel.sends[0]["view"]
    assert isinstance(view, discord.ui.View)
    assert [child.custom_id for child in view.children] == [
        RULES_ACCEPT_BUTTON_CUSTOM_ID
    ]


@pytest.mark.asyncio
async def test_new_version_edits_existing_message_without_duplicate() -> None:
    state = RulesPublicationState(10, 20, 90, 1)
    publication = FakePublicationRepository(state)
    channel = FakeChannel(20)
    message = FakeMessage(90)
    channel.messages[90] = message
    rules = FakeRulesRepository(_ruleset(2, "2.0"))

    result = await _service(state, rules, publication, channel).sync()

    assert result.status is RulesPublicationSyncStatus.UPDATED
    assert len(message.edits) == 1
    assert channel.sends == []
    assert publication.saves == [(90, 2)]


@pytest.mark.asyncio
async def test_deleted_message_is_recreated_and_new_id_persisted() -> None:
    state = RulesPublicationState(10, 20, 90, 1)
    publication = FakePublicationRepository(state)
    channel = FakeChannel(20)

    result = await _service(
        state, FakeRulesRepository(_ruleset()), publication, channel
    ).sync()

    assert result.status is RulesPublicationSyncStatus.RECREATED
    assert result.message_id == 100
    assert publication.saves == [(100, 1)]


@pytest.mark.asyncio
async def test_message_deleted_between_fetch_and_edit_is_recreated() -> None:
    state = RulesPublicationState(10, 20, 90, 1)
    publication = FakePublicationRepository(state)
    channel = FakeChannel(20)
    message = FakeMessage(90)
    message.edit_error = discord.NotFound(_response(404), "deleted during edit")
    channel.messages[90] = message

    result = await _service(
        state, FakeRulesRepository(_ruleset(2, "2.0")), publication, channel
    ).sync()

    assert result.status is RulesPublicationSyncStatus.RECREATED
    assert result.message_id == 100
    assert publication.saves == [(100, 2)]


@pytest.mark.asyncio
async def test_create_persistence_failure_deletes_new_message() -> None:
    state = RulesPublicationState(10, 20, None, None)
    publication = FakePublicationRepository(state)
    persistence_error = RuntimeError("database unavailable")
    publication.save_error = persistence_error
    channel = FakeChannel(20)

    with pytest.raises(RuntimeError) as captured:
        await _service(
            state, FakeRulesRepository(_ruleset()), publication, channel
        ).sync()

    assert captured.value is persistence_error
    assert publication.saves == [(100, 1)]
    assert channel.sent_messages[0].delete_calls == 1
    assert channel.messages == {}


@pytest.mark.asyncio
async def test_recreate_persistence_failure_deletes_replacement() -> None:
    state = RulesPublicationState(10, 20, 90, 1)
    publication = FakePublicationRepository(state)
    persistence_error = RuntimeError("database unavailable")
    publication.save_error = persistence_error
    channel = FakeChannel(20)

    with pytest.raises(RuntimeError) as captured:
        await _service(
            state, FakeRulesRepository(_ruleset(2, "2.0")), publication, channel
        ).sync()

    assert captured.value is persistence_error
    assert channel.sent_messages[0].delete_calls == 1
    assert channel.messages == {}
    assert publication.state == state


@pytest.mark.asyncio
async def test_update_persistence_failure_does_not_delete_existing_message() -> None:
    state = RulesPublicationState(10, 20, 90, 1)
    publication = FakePublicationRepository(state)
    persistence_error = RuntimeError("database unavailable")
    publication.save_error = persistence_error
    channel = FakeChannel(20)
    message = FakeMessage(90, channel)
    channel.messages[90] = message

    with pytest.raises(RuntimeError) as captured:
        await _service(
            state, FakeRulesRepository(_ruleset(2, "2.0")), publication, channel
        ).sync()

    assert captured.value is persistence_error
    assert len(message.edits) == 1
    assert message.delete_calls == 0
    assert channel.messages == {90: message}


@pytest.mark.asyncio
async def test_cleanup_failure_logs_and_preserves_persistence_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    state = RulesPublicationState(10, 20, None, None)
    publication = FakePublicationRepository(state)
    persistence_error = RuntimeError("database unavailable")
    publication.save_error = persistence_error
    channel = FakeChannel(20)
    channel.message_delete_error = discord.Forbidden(_response(403), "cannot delete")

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError) as captured:
        await _service(
            state, FakeRulesRepository(_ruleset()), publication, channel
        ).sync()

    assert captured.value is persistence_error
    assert channel.sent_messages[0].delete_calls == 1
    assert 100 in channel.messages
    assert "rules_publication_persistence_compensation_failed" in caplog.text
    assert "guild_id=10 channel_id=20 message_id=100 ruleset_id=1" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status"),
    [
        (
            discord.Forbidden(_response(403), "forbidden"),
            RulesPublicationSyncStatus.FORBIDDEN,
        ),
        (
            discord.HTTPException(_response(503), "temporary"),
            RulesPublicationSyncStatus.DISCORD_API_FAILURE,
        ),
    ],
)
async def test_discord_send_failures_return_structured_result(
    error: Exception, status: RulesPublicationSyncStatus
) -> None:
    state = RulesPublicationState(10, 20, None, None)
    publication = FakePublicationRepository(state)
    channel = FakeChannel(20)
    channel.send_error = error

    result = await _service(
        state, FakeRulesRepository(_ruleset()), publication, channel
    ).sync()

    assert result.status is status
    assert result.failed is True
    assert publication.saves == []
