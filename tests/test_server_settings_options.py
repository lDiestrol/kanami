from types import SimpleNamespace

import discord
import pytest

from discord_stats_bot.discord.server_settings_control import (
    ServerSettingControlError,
    ServerSettingControlErrorCategory,
)
from discord_stats_bot.discord.server_settings_options import (
    DiscordServerSettingsOptionsService,
)


class FakeRole:
    def __init__(
        self,
        guild: object,
        role_id: int,
        name: str,
        position: int,
        *,
        managed: bool = False,
        default: bool = False,
    ) -> None:
        self.guild = guild
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = managed
        self.default = default

    def is_default(self) -> bool:
        return self.default

    def __lt__(self, other: "FakeRole") -> bool:
        return self.position < other.position


class FakeChannel:
    def __init__(
        self,
        guild: object,
        channel_id: int,
        name: str,
        position: int,
        channel_type: discord.ChannelType,
        *,
        allowed: bool = True,
    ) -> None:
        self.guild = guild
        self.id = channel_id
        self.name = name
        self.position = position
        self.type = channel_type
        self.allowed = allowed

    def permissions_for(self, member: object) -> object:
        del member
        return SimpleNamespace(
            view_channel=self.allowed,
            send_messages=self.allowed,
            embed_links=self.allowed,
        )


class FakeGuild:
    def __init__(self, guild_id: int = 10, *, manage_roles: bool = True) -> None:
        self.id = guild_id
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=manage_roles),
            top_role=None,
        )
        self.me.top_role = FakeRole(self, 900, "Bot", 10)
        other_guild = SimpleNamespace(id=99)
        self.roles = [
            FakeRole(self, guild_id, "everyone", 0, default=True),
            FakeRole(self, 21, "Zulu", 5),
            FakeRole(self, 20, "Alpha", 5),
            FakeRole(self, 22, "Managed", 4, managed=True),
            FakeRole(self, 23, "High", 10),
            FakeRole(other_guild, 24, "Other", 2),
        ]
        self.channels = [
            FakeChannel(self, 32, "zeta", 2, discord.ChannelType.news),
            FakeChannel(self, 30, "beta", 1, discord.ChannelType.text),
            FakeChannel(self, 31, "alpha", 1, discord.ChannelType.text),
            FakeChannel(self, 33, "voice", 0, discord.ChannelType.voice),
            FakeChannel(
                self, 34, "forbidden", 0, discord.ChannelType.text, allowed=False
            ),
            FakeChannel(other_guild, 35, "other", 0, discord.ChannelType.text),
        ]


class FakeClient:
    def __init__(self, guild: FakeGuild | None, *, ready: bool = True) -> None:
        self.guild = guild
        self.ready = ready

    def is_ready(self) -> bool:
        return self.ready

    def get_guild(self, guild_id: int) -> FakeGuild | None:
        if self.guild is not None and self.guild.id == guild_id:
            return self.guild
        return None


@pytest.mark.asyncio
async def test_options_filter_and_sort_current_roles_and_channels() -> None:
    service = DiscordServerSettingsOptionsService(  # type: ignore[arg-type]
        FakeClient(FakeGuild()),
        guild_id=10,
    )

    options = await service.get_options()

    assert [(option.id, option.name) for option in options.roles] == [
        (20, "Alpha"),
        (21, "Zulu"),
    ]
    assert [
        (option.id, option.name, option.type.value) for option in options.channels
    ] == [
        (31, "alpha", "text"),
        (30, "beta", "text"),
        (32, "zeta", "news"),
    ]


@pytest.mark.asyncio
async def test_roles_are_empty_without_manage_roles() -> None:
    service = DiscordServerSettingsOptionsService(  # type: ignore[arg-type]
        FakeClient(FakeGuild(manage_roles=False)),
        guild_id=10,
    )

    options = await service.get_options()

    assert options.roles == ()
    assert len(options.channels) == 3


@pytest.mark.parametrize(
    "client",
    [FakeClient(FakeGuild(), ready=False), FakeClient(None)],
)
@pytest.mark.asyncio
async def test_options_fail_closed_without_ready_configured_guild(
    client: FakeClient,
) -> None:
    service = DiscordServerSettingsOptionsService(  # type: ignore[arg-type]
        client,
        guild_id=10,
    )

    with pytest.raises(ServerSettingControlError) as caught:
        await service.get_options()

    assert (
        caught.value.category is ServerSettingControlErrorCategory.RUNTIME_UNAVAILABLE
    )
