from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from discord_stats_bot.discord import AUTOROLE_REASON, AutoroleHandler
from discord_stats_bot.features.autorole import AutoroleService
from discord_stats_bot.features.server_settings import (
    GuildServerSettingsBaselines,
    resolve_guild_server_settings,
)


class FakeRole:
    def __init__(
        self,
        role_id: int,
        *,
        position: int = 1,
        managed: bool = False,
        default: bool = False,
    ) -> None:
        self.id = role_id
        self.position = position
        self.managed = managed
        self._default = default

    def is_default(self) -> bool:
        return self._default

    def __ge__(self, other: "FakeRole") -> bool:
        return (self.position, self.id) >= (other.position, other.id)


class FakeGuild:
    def __init__(
        self,
        *,
        guild_id: int = 10,
        role: FakeRole | None = None,
        manage_roles: bool = True,
        bot_top_role: FakeRole | None = None,
    ) -> None:
        self.id = guild_id
        self._role = role
        self.me = SimpleNamespace(
            guild_permissions=SimpleNamespace(manage_roles=manage_roles),
            top_role=bot_top_role or FakeRole(999, position=10),
        )

    def get_role(self, role_id: int) -> FakeRole | None:
        if self._role is not None and self._role.id == role_id:
            return self._role
        return None


class FakeMember:
    def __init__(
        self,
        guild: FakeGuild,
        *,
        bot: bool = False,
        roles: tuple[FakeRole, ...] = (),
        add_roles_error: Exception | None = None,
    ) -> None:
        self.id = 20
        self.guild = guild
        self.bot = bot
        self.roles = roles
        self.add_roles = AsyncMock(side_effect=add_roles_error)


def http_error(error_type: type[discord.HTTPException]) -> discord.HTTPException:
    response = SimpleNamespace(status=403, reason="Forbidden", headers={})
    return error_type(response, "role assignment failed")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_handler_reads_new_effective_autorole_without_restart() -> None:
    class Provider:
        def __init__(self) -> None:
            self.role_id: int | None = 30

        async def get(self):  # type: ignore[no-untyped-def]
            return resolve_guild_server_settings(
                10,
                GuildServerSettingsBaselines(autorole_role_id=self.role_id),
                None,
            )

    provider = Provider()
    first_role = FakeRole(30)
    guild = FakeGuild(role=first_role)
    handler = AutoroleHandler(guild_id=10, settings_provider=provider)
    first_member = FakeMember(guild)

    assert await handler.handle(first_member) is True  # type: ignore[arg-type]
    second_role = FakeRole(31)
    guild._role = second_role
    provider.role_id = 31
    second_member = FakeMember(guild)
    assert await handler.handle(second_member) is True  # type: ignore[arg-type]

    first_member.add_roles.assert_awaited_once_with(first_role, reason=AUTOROLE_REASON)
    second_member.add_roles.assert_awaited_once_with(
        second_role, reason=AUTOROLE_REASON
    )


def test_autorole_service_validates_configuration() -> None:
    with pytest.raises(ValueError, match="guild_id"):
        AutoroleService(guild_id=0, role_id=30)
    with pytest.raises(ValueError, match="role_id"):
        AutoroleService(guild_id=10, role_id=0)


@pytest.mark.asyncio
async def test_configured_role_is_assigned_once_with_reason() -> None:
    role = FakeRole(30)
    member = FakeMember(FakeGuild(role=role))
    handler = AutoroleHandler(guild_id=10, role_id=30)

    assert await handler.handle(member) is True  # type: ignore[arg-type]

    member.add_roles.assert_awaited_once_with(role, reason=AUTOROLE_REASON)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member",
    [
        FakeMember(FakeGuild(role=FakeRole(30)), bot=True),
        FakeMember(FakeGuild(guild_id=11, role=FakeRole(30))),
        FakeMember(FakeGuild(role=FakeRole(30)), roles=(FakeRole(30),)),
    ],
    ids=("bot", "other-guild", "already-has-role"),
)
async def test_ineligible_member_is_ignored_without_warning(
    member: FakeMember,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = AutoroleHandler(guild_id=10, role_id=30)

    assert await handler.handle(member) is False  # type: ignore[arg-type]

    member.add_roles.assert_not_awaited()
    assert "Autorole skipped" not in caplog.text


@pytest.mark.asyncio
async def test_missing_role_is_warned_and_not_assigned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    member = FakeMember(FakeGuild(role=None))
    handler = AutoroleHandler(guild_id=10, role_id=30)

    assert await handler.handle(member) is False  # type: ignore[arg-type]

    member.add_roles.assert_not_awaited()
    assert "guild_id=10 user_id=20 role_id=30" in caplog.text
    assert "role_not_found" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("guild", "reason"),
    [
        (FakeGuild(role=FakeRole(10, default=True)), "default_everyone_role"),
        (FakeGuild(role=FakeRole(30, managed=True)), "managed_role"),
        (
            FakeGuild(
                role=FakeRole(30, position=11),
                bot_top_role=FakeRole(999, position=10),
            ),
            "insufficient_role_hierarchy",
        ),
        (
            FakeGuild(role=FakeRole(30), manage_roles=False),
            "manage_roles_permission_missing",
        ),
    ],
    ids=("everyone", "managed", "hierarchy", "permission"),
)
async def test_unassignable_role_is_warned_and_not_added(
    guild: FakeGuild,
    reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    role_id = guild._role.id  # type: ignore[union-attr]
    member = FakeMember(guild)
    handler = AutoroleHandler(guild_id=10, role_id=role_id)

    assert await handler.handle(member) is False  # type: ignore[arg-type]

    member.add_roles.assert_not_awaited()
    assert reason in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [http_error(discord.Forbidden), http_error(discord.HTTPException)],
    ids=("forbidden", "http-exception"),
)
async def test_discord_assignment_error_is_isolated_without_retry(
    error: discord.HTTPException,
    caplog: pytest.LogCaptureFixture,
) -> None:
    role = FakeRole(30)
    member = FakeMember(FakeGuild(role=role), add_roles_error=error)
    handler = AutoroleHandler(guild_id=10, role_id=30)

    assert await handler.handle(member) is False  # type: ignore[arg-type]

    member.add_roles.assert_awaited_once()
    assert "Autorole skipped guild_id=10 user_id=20 role_id=30" in caplog.text
    assert "discord_" in caplog.text
