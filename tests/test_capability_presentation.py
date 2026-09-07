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
        self.members = {
            30: SimpleNamespace(id=30, display_name="Display member", guild=self)
        }

    def get_role(self, role_id: int):
        return self.role_map.get(role_id)

    def get_member(self, user_id: int):
        return self.members.get(user_id)


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
