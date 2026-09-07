from types import SimpleNamespace

import pytest

from discord_stats_bot.discord.capability_presentation import (
    CapabilityPresentationRuntimeUnavailableError,
    DiscordCapabilityPresentationService,
)


class Guild:
    id = 10
    me = object()

    def __init__(self) -> None:
        self.role_map = {
            10: SimpleNamespace(
                id=10, name="@everyone", guild=self, position=0, is_default=lambda: True
            ),
            20: SimpleNamespace(
                id=20,
                name="Moderators",
                guild=self,
                position=2,
                is_default=lambda: False,
            ),
            21: SimpleNamespace(
                id=21,
                name="Voice team",
                guild=self,
                position=1,
                is_default=lambda: False,
            ),
        }
        self.roles = list(self.role_map.values())
        self.member_map = {
            30: SimpleNamespace(id=30, display_name="Display member", guild=self)
        }
        self.members = list(self.member_map.values())

    def get_role(self, role_id: int):
        return self.role_map.get(role_id)

    def get_member(self, user_id: int):
        return self.member_map.get(user_id)


class Client:
    def __init__(self, *, ready: bool = True, guild: Guild | None = None) -> None:
        self.ready = ready
        self.guild = guild

    def is_ready(self) -> bool:
        return self.ready

    def get_guild(self, guild_id: int):
        return self.guild if self.guild is not None and guild_id == 10 else None


@pytest.mark.asyncio
async def test_cached_capability_subjects_return_role_and_member_names() -> None:
    subjects = await DiscordCapabilityPresentationService(
        Client(guild=Guild()),
        guild_id=10,  # type: ignore[arg-type]
    ).get_subjects((20,), (30,))

    assert subjects.roles == ((20, "Moderators"),)
    assert subjects.members == ((30, "Display member"),)


@pytest.mark.asyncio
async def test_missing_cached_capability_subjects_are_not_materialized() -> None:
    subjects = await DiscordCapabilityPresentationService(
        Client(guild=Guild()),
        guild_id=10,  # type: ignore[arg-type]
    ).get_subjects((99,), (98,))

    assert subjects.roles == ()
    assert subjects.members == ()


@pytest.mark.asyncio
async def test_role_options_are_configured_guild_only_default_free_and_deterministic() -> (
    None
):
    options = await DiscordCapabilityPresentationService(
        Client(guild=Guild()),
        guild_id=10,  # type: ignore[arg-type]
    ).get_role_options()
    assert options == ((20, "Moderators"), (21, "Voice team"))


@pytest.mark.asyncio
async def test_role_options_respect_maximum() -> None:
    guild = Guild()
    guild.roles = [
        SimpleNamespace(
            id=value,
            name=f"Role {value}",
            guild=guild,
            position=value,
            is_default=lambda: False,
        )
        for value in range(1, 400)
    ]
    options = await DiscordCapabilityPresentationService(
        Client(guild=guild),
        guild_id=10,  # type: ignore[arg-type]
    ).get_role_options()
    assert len(options) == 250


@pytest.mark.asyncio
async def test_member_options_are_human_only_deterministic_and_bounded() -> None:
    guild = Guild()
    guild.members = [
        SimpleNamespace(id=32, display_name="alice", guild=guild, bot=False),
        SimpleNamespace(id=31, display_name="Alice", guild=guild, bot=False),
        SimpleNamespace(id=33, display_name="Build bot", guild=guild, bot=True),
        *(
            SimpleNamespace(
                id=value,
                display_name=f"Member {value}",
                guild=guild,
                bot=False,
            )
            for value in range(100, 400)
        ),
    ]

    options = await DiscordCapabilityPresentationService(
        Client(guild=guild),
        guild_id=10,  # type: ignore[arg-type]
    ).get_member_options()

    assert len(options) == 250
    assert options[:2] == ((31, "Alice"), (32, "alice"))
    assert all(user_id != 33 for user_id, _ in options)
    assert len({user_id for user_id, _ in options}) == len(options)


@pytest.mark.asyncio
async def test_member_options_bound_display_names() -> None:
    guild = Guild()
    guild.members = [
        SimpleNamespace(id=30, display_name="A" * 101, guild=guild, bot=False)
    ]

    options = await DiscordCapabilityPresentationService(
        Client(guild=guild),
        guild_id=10,  # type: ignore[arg-type]
    ).get_member_options()

    assert options == ((30, "A" * 100),)


@pytest.mark.asyncio
@pytest.mark.parametrize("ready,guild", [(False, Guild()), (True, None)])
async def test_unavailable_capability_cache_has_capability_specific_error(
    ready: bool, guild: Guild | None
) -> None:
    service = DiscordCapabilityPresentationService(
        Client(ready=ready, guild=guild),
        guild_id=10,  # type: ignore[arg-type]
    )

    with pytest.raises(CapabilityPresentationRuntimeUnavailableError):
        await service.get_subjects((), ())
    with pytest.raises(CapabilityPresentationRuntimeUnavailableError):
        await service.get_member_options()
