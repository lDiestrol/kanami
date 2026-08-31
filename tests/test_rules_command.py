from datetime import UTC, datetime

import discord
import pytest

import discord_stats_bot.discord.rules as rules_module
from discord_stats_bot.discord import (
    RULES_ACCEPT_BUTTON_CUSTOM_ID,
    DiscordStatsClient,
    RulesCommandHandler,
)
from tests.support.discord import FakeMember, make_guild, make_interaction
from tests.support.persistence import FakeSessionFactory
from tests.test_rules_service import InMemoryRulesRepository, make_ruleset


class NoOpDependency:
    pass


@pytest.fixture(autouse=True)
def accept_shared_fake_members(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rules_module.discord, "Member", FakeMember)


def make_handler(
    repository: InMemoryRulesRepository,
    *,
    role_id: int | None = None,
    session_events: list[object] | None = None,
) -> RulesCommandHandler:
    return RulesCommandHandler(
        FakeSessionFactory(session_events),  # type: ignore[arg-type]
        guild_id=10,
        accepted_role_id=role_id,
        repository_factory=lambda session: repository,
        clock=lambda: datetime(2026, 8, 25, 12, tzinfo=UTC),
    )


def test_rules_commands_are_registered_with_expected_permissions() -> None:
    handler = make_handler(InMemoryRulesRepository(make_ruleset()))
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=NoOpDependency(),  # type: ignore[arg-type]
        voice_reconciler=NoOpDependency(),  # type: ignore[arg-type]
        voice_event_handler=NoOpDependency(),  # type: ignore[arg-type]
        rules_command_handler=handler,
    )

    rules = client.tree.get_command("rules", guild=client._command_guild)
    status = client.tree.get_command("rules-status", guild=client._command_guild)

    assert rules is not None and rules.guild_only is True
    assert rules.default_permissions is None
    assert status is not None and status.guild_only is True
    assert status.default_permissions == discord.Permissions(manage_guild=True)


@pytest.mark.asyncio
async def test_persistent_view_uses_stable_custom_id_and_registers_on_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = make_handler(InMemoryRulesRepository(make_ruleset()))
    client = DiscordStatsClient(
        guild_id=10,
        reference_provisioner=NoOpDependency(),  # type: ignore[arg-type]
        voice_reconciler=NoOpDependency(),  # type: ignore[arg-type]
        voice_event_handler=NoOpDependency(),  # type: ignore[arg-type]
        rules_command_handler=handler,
    )
    registered: list[discord.ui.View] = []
    monkeypatch.setattr(client, "add_view", registered.append)

    async def sync(*, guild: object) -> list[object]:
        del guild
        return []

    monkeypatch.setattr(client.tree, "sync", sync)

    await client.setup_hook()
    await client.setup_hook()

    assert len(registered) == 1
    view = registered[0]
    assert view.is_persistent()
    assert [item.custom_id for item in view.children] == [RULES_ACCEPT_BUTTON_CUSTOM_ID]


@pytest.mark.asyncio
async def test_rules_display_and_acceptance_messages_are_safe_and_idempotent() -> None:
    repository = InMemoryRulesRepository(make_ruleset())
    handler = make_handler(repository)
    show_interaction = make_interaction()

    await handler.show(show_interaction)  # type: ignore[arg-type]

    _, shown = show_interaction.response.messages[0]
    assert shown["embed"].title == "Правила сервера"
    assert shown["embed"].footer.text == "Версия 1.0"
    assert shown["view"].is_persistent()

    first = make_interaction()
    second = make_interaction()
    await handler.accept_current(first)  # type: ignore[arg-type]
    await handler.accept_current(second)  # type: ignore[arg-type]

    assert first.response.messages[0][1]["ephemeral"] is True
    assert "Правила версии 1.0 приняты" in first.response.messages[0][0][0]
    assert "Добро пожаловать" in first.response.messages[0][0][0]
    assert "уже принял правила версии 1.0" in second.response.messages[0][0][0]
    assert len(repository.acceptances) == 1


@pytest.mark.asyncio
async def test_no_published_rules_returns_harmless_ephemeral_message() -> None:
    handler = make_handler(InMemoryRulesRepository(None))
    interaction = make_interaction()

    await handler.show(interaction)  # type: ignore[arg-type]

    args, kwargs = interaction.response.messages[0]
    assert "нет опубликованных правил" in args[0]
    assert kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_rules_status_requires_manage_guild_and_shows_count() -> None:
    repository = InMemoryRulesRepository(make_ruleset())
    repository.acceptances[(10, 20, 1)] = datetime(2026, 8, 25, tzinfo=UTC)
    handler = make_handler(repository)
    denied = make_interaction()
    allowed = make_interaction(manage_guild=True)

    await handler.show_status(denied)  # type: ignore[arg-type]
    await handler.show_status(allowed)  # type: ignore[arg-type]

    assert "Управлять сервером" in denied.response.messages[0][0][0]
    _, message = allowed.response.messages[0]
    assert message["ephemeral"] is True
    fields = {field.name: field.value for field in message["embed"].fields}
    assert fields["Текущая версия"] == "1.0"
    assert fields["Приняли"] == "1"


@pytest.mark.asyncio
async def test_missing_optional_role_does_not_rollback_acceptance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = InMemoryRulesRepository(make_ruleset())
    handler = make_handler(repository, role_id=999)
    member = FakeMember(20)
    guild = make_guild(members=(member,))
    guild.get_role = lambda role_id: None  # type: ignore[attr-defined]
    member.guild = guild  # type: ignore[attr-defined]
    interaction = make_interaction(user=member, guild=guild)

    await handler.accept_current(interaction)  # type: ignore[arg-type]

    assert len(repository.acceptances) == 1
    assert "Rules accepted role is unavailable" in caplog.text
    message = interaction.response.messages[0][0][0]
    assert "Правила версии 1.0 успешно приняты" in message
    assert "не смогла выдать роль доступа" in message
    assert "Обратись к администрации" in message
    assert "Добро пожаловать" not in message


@pytest.mark.asyncio
async def test_configured_role_is_granted_after_durable_acceptance() -> None:
    repository = InMemoryRulesRepository(make_ruleset())
    session_events: list[object] = []
    handler = make_handler(repository, role_id=777, session_events=session_events)
    member = FakeMember(20)
    role = object()
    granted: list[tuple[object, str | None]] = []
    guild = make_guild(members=(member,))
    guild.get_role = lambda role_id: role if role_id == 777 else None  # type: ignore[attr-defined]

    async def add_roles(selected: object, *, reason: str | None = None) -> None:
        assert repository.acceptances
        assert "commit" in session_events
        granted.append((selected, reason))

    member.guild = guild  # type: ignore[attr-defined]
    member.add_roles = add_roles  # type: ignore[attr-defined]
    interaction = make_interaction(user=member, guild=guild)

    await handler.accept_current(interaction)  # type: ignore[arg-type]

    assert granted == [(role, "Принятие текущей версии правил сервера")]
    assert "Добро пожаловать" in interaction.response.messages[0][0][0]


@pytest.mark.asyncio
async def test_existing_configured_role_is_normal_success_without_assignment() -> None:
    repository = InMemoryRulesRepository(make_ruleset())
    handler = make_handler(repository, role_id=777)
    member = FakeMember(20)
    role = object()
    member.roles = [role]
    guild = make_guild(members=(member,))
    guild.get_role = lambda role_id: role if role_id == 777 else None  # type: ignore[attr-defined]
    member.guild = guild  # type: ignore[attr-defined]

    async def unexpected_add_roles(*args: object, **kwargs: object) -> None:
        raise AssertionError("add_roles must not be called for an existing role")

    member.add_roles = unexpected_add_roles  # type: ignore[attr-defined]
    interaction = make_interaction(user=member, guild=guild)

    await handler.accept_current(interaction)  # type: ignore[arg-type]

    assert len(repository.acceptances) == 1
    assert "Добро пожаловать" in interaction.response.messages[0][0][0]


@pytest.mark.asyncio
async def test_role_assignment_failure_warns_without_rolling_back_acceptance(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = InMemoryRulesRepository(make_ruleset())
    session_events: list[object] = []
    handler = make_handler(repository, role_id=777, session_events=session_events)
    member = FakeMember(20)
    role = object()
    guild = make_guild(members=(member,))
    guild.get_role = lambda role_id: role if role_id == 777 else None  # type: ignore[attr-defined]

    async def failing_add_roles(*args: object, **kwargs: object) -> None:
        assert repository.acceptances
        assert "commit" in session_events
        raise RuntimeError("Discord role assignment failed")

    member.guild = guild  # type: ignore[attr-defined]
    member.add_roles = failing_add_roles  # type: ignore[attr-defined]
    interaction = make_interaction(user=member, guild=guild)

    await handler.accept_current(interaction)  # type: ignore[arg-type]

    assert len(repository.acceptances) == 1
    assert "Rules accepted role grant failed" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    message = interaction.response.messages[0][0][0]
    assert "Правила версии 1.0 успешно приняты" in message
    assert "не смогла выдать роль доступа" in message
    assert "Добро пожаловать" not in message
