"""Minimal Discord-shaped records for command adapter tests."""

from collections.abc import Sequence
from datetime import datetime
from types import SimpleNamespace

_AUTOMATIC_GUILD = object()


class FakeAvatar:
    def __init__(self, url: str | None = None) -> None:
        self.url = url


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakeChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name


def make_channel(channel_id: int, name: str) -> FakeChannel:
    return FakeChannel(channel_id, name)


class FakeMember:
    def __init__(
        self,
        user_id: int,
        *,
        bot: bool = False,
        display_name: str | None = None,
        joined_at: datetime | None = None,
        avatar_url: str | None = None,
        manage_guild: bool = False,
        role_ids: Sequence[int] = (),
    ) -> None:
        self.id = user_id
        self.bot = bot
        self.display_name = (
            display_name if display_name is not None else f"User {user_id}"
        )
        self.joined_at = joined_at
        self.display_avatar = FakeAvatar(avatar_url)
        self.guild_permissions = SimpleNamespace(manage_guild=manage_guild)
        self.roles = [FakeRole(role_id) for role_id in role_ids]


def make_member(
    user_id: int,
    *,
    bot: bool = False,
    display_name: str | None = None,
    joined_at: datetime | None = None,
    avatar_url: str | None = None,
    manage_guild: bool = False,
    role_ids: Sequence[int] = (),
) -> FakeMember:
    return FakeMember(
        user_id,
        bot=bot,
        display_name=display_name,
        joined_at=joined_at,
        avatar_url=avatar_url,
        manage_guild=manage_guild,
        role_ids=role_ids,
    )


class FakeGuild:
    def __init__(
        self,
        *,
        guild_id: int = 10,
        members: Sequence[object] = (),
        channels: Sequence[object] = (),
        name: str = "Test Guild",
        member_count: int | None = None,
        voice_channels: Sequence[object] = (),
        stage_channels: Sequence[object] = (),
    ) -> None:
        self.id = guild_id
        self.name = name
        self.member_count = len(members) if member_count is None else member_count
        self.voice_channels = list(voice_channels)
        self.stage_channels = list(stage_channels)
        self.members = list(members)
        self._members = {member.id: member for member in members}  # type: ignore[attr-defined]
        self._channels = {channel.id: channel for channel in channels}  # type: ignore[attr-defined]

    def get_member(self, user_id: int) -> object | None:
        return self._members.get(user_id)

    def get_channel(self, channel_id: int) -> object | None:
        return self._channels.get(channel_id)


def make_guild(
    *,
    guild_id: int = 10,
    members: Sequence[object] = (),
    channels: Sequence[object] = (),
    name: str = "Test Guild",
    member_count: int | None = None,
    voice_channels: Sequence[object] = (),
    stage_channels: Sequence[object] = (),
) -> FakeGuild:
    return FakeGuild(
        guild_id=guild_id,
        members=members,
        channels=channels,
        name=name,
        member_count=member_count,
        voice_channels=voice_channels,
        stage_channels=stage_channels,
    )


class RecordingResponse:
    def __init__(self) -> None:
        self.deferred: list[dict[str, object]] = []
        self.messages: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def defer(self, **kwargs: object) -> None:
        self.deferred.append(kwargs)

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.messages.append((args, kwargs))


class RecordingFollowup:
    def __init__(self) -> None:
        self.messages: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def send(self, *args: object, **kwargs: object) -> None:
        self.messages.append((args, kwargs))


class FakeInteraction:
    def __init__(
        self,
        *,
        guild_id: int | None = 10,
        user: object | None = None,
        guild: object = _AUTOMATIC_GUILD,
        client: object | None = None,
    ) -> None:
        self.guild_id = guild_id
        if guild is _AUTOMATIC_GUILD:
            self.guild = make_guild(guild_id=guild_id) if guild_id is not None else None
        else:
            self.guild = guild
        self.user = user if user is not None else make_member(20)
        self.client = client
        self.response = RecordingResponse()
        self.followup = RecordingFollowup()


def make_interaction(
    *,
    guild_id: int | None = 10,
    user: object | None = None,
    user_id: int = 20,
    bot: bool = False,
    guild: object = _AUTOMATIC_GUILD,
    client: object | None = None,
    manage_guild: bool = False,
) -> FakeInteraction:
    return FakeInteraction(
        guild_id=guild_id,
        user=(
            user
            if user is not None
            else make_member(user_id, bot=bot, manage_guild=manage_guild)
        ),
        guild=guild,
        client=client,
    )
