import logging
from dataclasses import fields
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import discord
import pytest

from discord_stats_bot.discord.runtime import (
    DiscordStatsClient,
    TextActivityEventHandler,
    create_gateway_intents,
)
from discord_stats_bot.features.text_activity import TextMessageActivity

OCCURRED_AT = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


class RecordingReferenceRepository:
    def __init__(self) -> None:
        self.guilds: list[object] = []
        self.users: list[tuple[object, ...]] = []
        self.members: list[tuple[object, ...]] = []
        self.voice_channels: list[tuple[object, ...]] = []

    async def upsert_guild(self, guild: object) -> None:
        self.guilds.append(guild)

    async def upsert_users(self, users: tuple[object, ...]) -> None:
        self.users.append(users)

    async def upsert_members(self, members: tuple[object, ...]) -> None:
        self.members.append(members)

    async def upsert_voice_channels(self, channels: tuple[object, ...]) -> None:
        self.voice_channels.append(channels)


class RecordingTextRepository:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def record_message(self, **kwargs: object) -> None:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error


class TransactionContext:
    def __init__(self) -> None:
        self.session = object()
        self.exited = False

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        self.exited = True


class FakeSessionFactory:
    def __init__(self) -> None:
        self.transactions: list[TransactionContext] = []

    def begin(self) -> TransactionContext:
        transaction = TransactionContext()
        self.transactions.append(transaction)
        return transaction


class PrivacySafeMessage:
    def __init__(
        self,
        *,
        guild_id: int | None = 10,
        user_id: int = 20,
        bot: bool = False,
        webhook_id: int | None = None,
        message_type: discord.MessageType = discord.MessageType.default,
        channel_id: int = 30,
    ) -> None:
        self.guild = (
            None
            if guild_id is None
            else SimpleNamespace(id=guild_id, name="Kanami guild")
        )
        self.author = SimpleNamespace(
            id=user_id,
            bot=bot,
            joined_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.webhook_id = webhook_id
        self.type = message_type
        self.channel = SimpleNamespace(id=channel_id)
        self.created_at = OCCURRED_AT

    @property
    def content(self) -> str:
        raise AssertionError("runtime must not read message content")

    @property
    def attachments(self) -> tuple[object, ...]:
        raise AssertionError("runtime must not read message attachments")

    @property
    def embeds(self) -> tuple[object, ...]:
        raise AssertionError("runtime must not read message embeds")


def make_handler(
    *,
    text_error: Exception | None = None,
) -> tuple[
    TextActivityEventHandler,
    FakeSessionFactory,
    RecordingReferenceRepository,
    RecordingTextRepository,
]:
    sessions = FakeSessionFactory()
    references = RecordingReferenceRepository()
    text = RecordingTextRepository(error=text_error)
    handler = TextActivityEventHandler(
        sessions,  # type: ignore[arg-type]
        guild_id=10,
        report_timezone=ZoneInfo("UTC"),
        text_repository_factory=lambda session: text,  # type: ignore[arg-type]
        reference_repository_factory=lambda session: references,  # type: ignore[arg-type]
    )
    return handler, sessions, references, text


@pytest.mark.asyncio
async def test_normal_message_records_zero_attachments_without_reading_payload() -> (
    None
):
    handler, sessions, references, text = make_handler()
    message = PrivacySafeMessage()

    result = await handler.handle(message)  # type: ignore[arg-type]

    assert result is True
    assert len(sessions.transactions) == 1
    assert sessions.transactions[0].exited is True
    assert references.guilds[0].id == 10  # type: ignore[attr-defined]
    assert references.users[0][0].id == 20  # type: ignore[attr-defined]
    assert references.members[0][0].user_id == 20  # type: ignore[attr-defined]
    assert references.voice_channels == [()]
    assert text.calls == [
        {
            "guild_id": 10,
            "user_id": 20,
            "channel_id": 30,
            "activity_date": OCCURRED_AT.date(),
            "attachment_count": 0,
            "is_reply": False,
        }
    ]


@pytest.mark.asyncio
async def test_reply_is_identified_by_message_type() -> None:
    handler, _, _, text = make_handler()

    await handler.handle(  # type: ignore[arg-type]
        PrivacySafeMessage(message_type=discord.MessageType.reply)
    )

    assert text.calls[0]["is_reply"] is True


@pytest.mark.asyncio
async def test_thread_uses_actual_thread_id_as_channel_id() -> None:
    handler, _, _, text = make_handler()

    await handler.handle(PrivacySafeMessage(channel_id=9876))  # type: ignore[arg-type]

    assert text.calls[0]["channel_id"] == 9876


@pytest.mark.parametrize(
    "message",
    [
        PrivacySafeMessage(bot=True),
        PrivacySafeMessage(webhook_id=99),
        PrivacySafeMessage(guild_id=None),
        PrivacySafeMessage(guild_id=11),
        PrivacySafeMessage(message_type=discord.MessageType.pins_add),
    ],
)
@pytest.mark.asyncio
async def test_irrelevant_messages_are_ignored(message: PrivacySafeMessage) -> None:
    handler, sessions, references, text = make_handler()

    result = await handler.handle(message)  # type: ignore[arg-type]

    assert result is False
    assert sessions.transactions == []
    assert references.guilds == []
    assert text.calls == []


@pytest.mark.asyncio
async def test_persistence_failure_is_logged_without_escaping_or_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler, _, _, _ = make_handler(text_error=RuntimeError("database offline"))
    message = PrivacySafeMessage()

    with caplog.at_level(logging.ERROR):
        result = await handler.handle(message)  # type: ignore[arg-type]

    assert result is False
    assert "guild_id=10 user_id=20 channel_id=30" in caplog.text
    assert "database offline" in caplog.text
    assert "message content" not in caplog.text


def test_domain_and_persistence_input_have_no_message_content_field() -> None:
    assert {field.name for field in fields(TextMessageActivity)} == {
        "guild_id",
        "user_id",
        "channel_id",
        "occurred_at",
        "attachment_count",
        "is_reply",
    }


def test_gateway_intents_enable_guild_messages_without_message_content() -> None:
    intents = create_gateway_intents()

    assert intents.guild_messages is True
    assert intents.message_content is False
    assert intents.dm_messages is False


class RecordingEventHandler:
    def __init__(self) -> None:
        self.messages: list[object] = []

    async def handle(self, message: object) -> None:
        self.messages.append(message)


@pytest.mark.asyncio
async def test_client_on_message_delegates_to_configured_handler() -> None:
    event_handler = RecordingEventHandler()
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=object(),  # type: ignore[arg-type]
        voice_reconciler=object(),  # type: ignore[arg-type]
        voice_event_handler=object(),  # type: ignore[arg-type]
        text_activity_event_handler=event_handler,  # type: ignore[arg-type]
    )
    message = PrivacySafeMessage()

    await client.on_message(message)  # type: ignore[arg-type]

    assert event_handler.messages == [message]
