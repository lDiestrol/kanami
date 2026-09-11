from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import discord
import pytest

from discord_stats_bot.discord.audit_logging import build_audit_embed
from discord_stats_bot.discord.voice_move import VoiceMoveCommandHandler
from discord_stats_bot.features.audit_logging import (
    AuditEventDraft,
    AuditEventRecord,
    AuditRetentionPolicy,
)
from discord_stats_bot.features.capabilities import CapabilityAuthorizationState
from tests.support.discord import make_interaction


class SessionFactory:
    def __init__(self, *, commit_fail=False):
        self.commit_fail = commit_fail
        self.events = []

    def __call__(self):
        return self

    async def __aenter__(self):
        return object()

    async def __aexit__(self, *_):
        return None

    def begin(self):
        return Transaction(self)


class Transaction:
    def __init__(self, factory):
        self.factory = factory

    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, *_):
        if exc_type is not None:
            self.factory.events.append("rollback")
            return None
        if self.factory.commit_fail:
            self.factory.events.append("commit_failed")
            raise RuntimeError("commit failed")
        self.factory.events.append("commit")
        return None


class CapabilityRepository:
    def __init__(self, enabled=True, user=False, role=False):
        self.state = CapabilityAuthorizationState(enabled, user, role)
        self.calls = []

    async def get_authorization_state(self, *args):
        self.calls.append(args)
        return self.state


class AuditRepository:
    def __init__(self, events, fail=False):
        self.drafts, self.fail = [], fail
        self.events = events

    async def create(self, draft, *, expires_at):
        if self.fail:
            raise RuntimeError("private audit failure")
        self.drafts.append((draft, expires_at))
        self.events.append("create")


def permissions(**overrides):
    return SimpleNamespace(
        view_channel=overrides.get("view_channel", True),
        connect=overrides.get("connect", True),
        move_members=overrides.get("move_members", True),
    )


def native_permissions_for(member_id, role_ids, overwrites):
    """Resolve a channel ACL through discord.py's native overwrite algorithm."""

    class Guild:
        id = 10
        owner_id = 1
        default_role = SimpleNamespace(permissions=discord.Permissions.none())

        def __init__(self):
            self.roles = {
                7: SimpleNamespace(
                    _permissions=discord.Permissions(
                        view_channel=True, connect=True
                    ).value
                ),
                8: SimpleNamespace(
                    _permissions=discord.Permissions(
                        view_channel=True, connect=True, move_members=True
                    ).value
                ),
            }

        def get_role(self, role_id):
            return self.roles.get(role_id)

    guild = Guild()
    member_value = object.__new__(discord.Member)
    member_value._user = SimpleNamespace(id=member_id)
    member_value._roles = discord.utils.SnowflakeList([guild.id, *role_ids])
    member_value.timed_out_until = None
    channel_value = SimpleNamespace(guild=guild, _overwrites=overwrites)
    return discord.abc.GuildChannel.permissions_for(channel_value, member_value)


def overwrite(overwrite_id, overwrite_type, **permissions_to_set):
    allow, deny = discord.PermissionOverwrite(**permissions_to_set).pair()
    return discord.abc._Overwrites(
        {
            "id": str(overwrite_id),
            "type": overwrite_type,
            "allow": str(allow.value),
            "deny": str(deny.value),
        }
    )


def member(member_id, *, bot=False, roles=()):
    value = MagicMock(spec=discord.Member)
    value.id, value.bot = member_id, bot
    value.roles = [SimpleNamespace(id=role_id) for role_id in roles]
    return value


def channel(channel_id, guild):
    value = MagicMock(spec=discord.VoiceChannel)
    value.id, value.name, value.guild = channel_id, f"voice-{channel_id}", guild
    value.permissions_for.return_value = permissions()
    return value


def environment(
    *,
    enabled=True,
    user=True,
    role=False,
    owner_id=99,
    caller_id=20,
    bot_member_id=99,
    caller_bot=False,
    roles=(),
    target_bot=False,
    audit_fail=False,
    commit_fail=False,
    wake_delivery=None,
):
    capability, audit, sessions = (
        CapabilityRepository(enabled, user, role),
        None,
        SessionFactory(commit_fail=commit_fail),
    )
    audit = AuditRepository(sessions.events, audit_fail)
    guild = MagicMock()
    guild.id, guild.owner_id, guild.afk_channel = 10, owner_id, None
    guild.me = member(bot_member_id, bot=True)
    caller, target = (
        member(caller_id, bot=caller_bot, roles=roles),
        member(30, bot=target_bot),
    )
    target.guild = guild
    source, destination = channel(40, guild), channel(50, guild)
    target.voice, target.move_to = SimpleNamespace(channel=source), AsyncMock()
    interaction = make_interaction(guild_id=10, guild=guild, user=caller)
    handler = VoiceMoveCommandHandler(
        sessions,
        guild_id=10,
        capability_repository_factory=lambda _: capability,
        audit_repository_factory=lambda _: audit,
        wake_delivery=wake_delivery,
    )  # type: ignore[arg-type]
    return SimpleNamespace(
        handler=handler,
        interaction=interaction,
        guild=guild,
        caller=caller,
        target=target,
        source=source,
        destination=destination,
        capability=capability,
        audit=audit,
        sessions=sessions,
    )


async def run(env):
    await env.handler.handle(env.interaction, env.target, env.destination)  # type: ignore[arg-type]


def reply(env):
    messages = env.interaction.followup.messages or env.interaction.response.messages
    return str(messages[0][0][0])


@pytest.mark.asyncio
async def test_successful_move_audits_exact_action_and_allows_bot_target():
    env = environment(target_bot=True)
    await run(env)
    env.target.move_to.assert_awaited_once_with(
        env.destination, reason="Kanami /move requested by Discord user 20"
    )
    draft, expires_at = env.audit.drafts[0]
    assert (
        draft.actor_user_id,
        draft.subject_id,
        draft.before_data,
        draft.after_data,
    ) == (
        20,
        30,
        {"channel_id": 40, "channel_name": "voice-40"},
        {"channel_id": 50, "channel_name": "voice-50"},
    )
    assert draft.details_data == {
        "capability_key": "voice.move",
        "via": "slash_command",
        "source_channel_id": 40,
        "source_channel_name": "voice-40",
        "destination_channel_id": 50,
        "destination_channel_name": "voice-50",
    }
    assert (
        draft.retention_policy is AuditRetentionPolicy.IMPORTANT and expires_at is None
    )


@pytest.mark.asyncio
async def test_context_rejects_wrong_guild_non_member_and_bot_callers():
    for env, mutate in (
        (environment(), lambda e: setattr(e.interaction, "guild_id", 11)),
        (
            environment(),
            lambda e: setattr(e.interaction, "user", MagicMock(spec=discord.User)),
        ),
        (environment(caller_bot=True), lambda e: None),
    ):
        mutate(env)
        await run(env)
        env.target.move_to.assert_not_awaited()
        assert (
            env.interaction.response.messages and not env.interaction.response.deferred
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "user", "role", "owner", "allowed"),
    [
        (False, True, False, 20, False),
        (True, False, False, 99, False),
        (True, False, False, 20, True),
        (True, True, False, 99, True),
        (True, False, True, 99, True),
    ],
    ids=["disabled-owner", "not-granted", "owner", "user-grant", "role-grant"],
)
async def test_capability_contract_controls_move_and_receives_current_roles(
    enabled, user, role, owner, allowed
):
    env = environment(
        enabled=enabled, user=user, role=role, owner_id=owner, roles=(7, 8)
    )
    await run(env)
    assert env.target.move_to.await_count == int(allowed)
    assert env.capability.calls[0][3] == (7, 8)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["self", "disconnected", "already", "afk"])
async def test_target_destination_rejections_do_not_move(case):
    env = environment()
    if case == "self":
        env.target.id = env.caller.id
    elif case == "disconnected":
        env.target.voice = None
    elif case == "already":
        env.target.voice = SimpleNamespace(channel=env.destination)
    else:
        env.guild.afk_channel = env.destination
    await run(env)
    env.target.move_to.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("which", "permission"),
    [
        ("source", "view_channel"),
        ("source", "connect"),
        ("destination", "view_channel"),
        ("destination", "connect"),
    ],
)
async def test_missing_caller_effective_acl_rejects_move(which, permission):
    env = environment()
    affected = getattr(env, which)
    affected.permissions_for.side_effect = lambda who: (
        permissions(**{permission: False}) if who is env.caller else permissions()
    )
    await run(env)
    env.target.move_to.assert_not_awaited()


@pytest.mark.asyncio
async def test_caller_native_move_members_is_not_required():
    env = environment()
    for item in (env.source, env.destination):
        item.permissions_for.side_effect = lambda who: (
            permissions(move_members=False) if who is env.caller else permissions()
        )
    await run(env)
    env.target.move_to.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["view_channel", "connect", "move_members"])
async def test_missing_bot_effective_acl_rejects_move(permission):
    env = environment()
    env.source.permissions_for.side_effect = lambda who: (
        permissions(**{permission: False}) if who is env.guild.me else permissions()
    )
    await run(env)
    env.target.move_to.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("permission", ["view_channel", "connect", "move_members"])
async def test_missing_destination_bot_effective_acl_rejects_move(permission):
    env = environment()
    env.destination.permissions_for.side_effect = lambda who: (
        permissions(**{permission: False}) if who is env.guild.me else permissions()
    )
    await run(env)
    env.target.move_to.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_discord_overwrites_control_caller_and_bot_acl_on_both_channels():
    env = environment(owner_id=1)
    caller_acl = [
        overwrite(10, 0, view_channel=False, connect=False),
        overwrite(7, 0, view_channel=True, connect=False),
        overwrite(20, 1, connect=True),
    ]
    bot_acl = [
        overwrite(10, 0, view_channel=False, connect=False, move_members=False),
        overwrite(8, 0, view_channel=True, connect=True, move_members=True),
    ]
    env.caller._roles = discord.utils.SnowflakeList([10, 7])
    env.guild.me._roles = discord.utils.SnowflakeList([10, 8])
    for item, caller_overwrites, bot_overwrites in (
        (env.source, caller_acl, bot_acl),
        (env.destination, caller_acl, bot_acl),
    ):
        item.permissions_for.side_effect = (
            lambda who, caller_overwrites=caller_overwrites, bot_overwrites=bot_overwrites: (
                native_permissions_for(
                    who.id,
                    [7] if who is env.caller else [8],
                    caller_overwrites if who is env.caller else bot_overwrites,
                )
            )
        )

    await run(env)

    env.target.move_to.assert_awaited_once()


@pytest.mark.asyncio
async def test_stage_and_afk_sources_are_allowed_with_effective_acl():
    for source in (
        MagicMock(spec=discord.StageChannel),
        None,
    ):
        env = environment()
        if source is None:
            source = env.guild.afk_channel = channel(41, env.guild)
        else:
            source.id, source.name, source.guild = 41, "stage-41", env.guild
            source.permissions_for.return_value = permissions()
        env.target.voice = SimpleNamespace(channel=source)
        await run(env)
        env.target.move_to.assert_awaited_once()


@pytest.mark.asyncio
async def test_stage_and_afk_destinations_are_rejected():
    env = environment()
    stage = MagicMock(spec=discord.StageChannel)
    stage.id, stage.guild = 51, env.guild
    await env.handler.handle(env.interaction, env.target, stage)  # type: ignore[arg-type]
    env.target.move_to.assert_not_awaited()

    env = environment()
    env.guild.afk_channel = env.destination
    await run(env)
    env.target.move_to.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_destination_is_left_to_discord_move_api():
    env = environment()
    env.destination.user_limit, env.destination.members = 1, [member(88)]
    await run(env)
    env.target.move_to.assert_awaited_once()


@pytest.mark.asyncio
async def test_source_change_revalidates_new_source_and_audits_it():
    env = environment()
    new_source = channel(41, env.guild)

    def mutate(who):
        if who is env.caller:
            env.target.voice = SimpleNamespace(channel=new_source)
        return permissions()

    env.destination.permissions_for.side_effect = mutate
    await run(env)
    env.target.move_to.assert_awaited_once()
    assert env.audit.drafts[0][0].before_data == {
        "channel_id": 41,
        "channel_name": "voice-41",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["unauthorized", "disconnected"])
async def test_source_change_to_unauthorized_or_disconnected_blocks_move(changed):
    env = environment()
    new_source = channel(41, env.guild)
    if changed == "unauthorized":
        new_source.permissions_for.side_effect = lambda who: (
            permissions(view_channel=False) if who is env.caller else permissions()
        )

    def mutate(who):
        if who is env.caller:
            env.target.voice = (
                None
                if changed == "disconnected"
                else SimpleNamespace(channel=new_source)
            )
        return permissions()

    env.destination.permissions_for.side_effect = mutate
    await run(env)
    env.target.move_to.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (discord.Forbidden(MagicMock(), "private details"), "Discord отклонил"),
        (discord.HTTPException(MagicMock(status=500), "private details"), "Не удалось"),
    ],
)
async def test_discord_errors_are_controlled_and_not_audited(error, expected):
    env = environment()
    env.target.move_to.side_effect = error
    await run(env)
    assert (
        not env.audit.drafts
        and expected in reply(env)
        and "private details" not in reply(env)
    )


@pytest.mark.asyncio
async def test_audit_failure_keeps_successful_move_and_never_rolls_back():
    env = environment(audit_fail=True)
    await run(env)
    env.target.move_to.assert_awaited_once()
    assert "перемещён" in reply(env) and "аудит" in reply(env)


@pytest.mark.asyncio
async def test_audit_commit_precedes_single_delivery_wake_after_move():
    events = []
    env = environment(wake_delivery=lambda: events.append("wake"))
    env.target.move_to.side_effect = lambda *args, **kwargs: events.append("move")
    env.audit.events = events
    env.sessions.events = events

    await run(env)

    assert events == ["move", "create", "commit", "wake"]


@pytest.mark.asyncio
async def test_audit_create_failure_does_not_commit_or_wake_or_rollback_move():
    events = []
    env = environment(audit_fail=True, wake_delivery=lambda: events.append("wake"))
    env.target.move_to.side_effect = lambda *args, **kwargs: events.append("move")
    env.audit.events = events
    env.sessions.events = events

    await run(env)

    assert events == ["move", "rollback"]
    assert "перемещён" in reply(env) and "аудит" in reply(env)


@pytest.mark.asyncio
async def test_audit_commit_failure_does_not_wake_or_rollback_discord_move():
    events = []
    env = environment(commit_fail=True, wake_delivery=lambda: events.append("wake"))
    env.target.move_to.side_effect = lambda *args, **kwargs: events.append("move")
    env.audit.events = events
    env.sessions.events = events

    await run(env)

    assert events == ["move", "create", "commit_failed"]
    assert "перемещён" in reply(env) and "аудит" in reply(env)


def test_moderation_move_audit_is_important_and_renders_as_a_move():
    draft = AuditEventDraft(
        guild_id=10,
        category="moderation",
        event_type="moderation.voice_moved",
        occurred_at=datetime(2026, 9, 8, tzinfo=UTC),
        subject_type="user",
        subject_id=30,
        actor_user_id=20,
        before_data={"channel_id": 40, "channel_name": "source"},
        after_data={"channel_id": 50, "channel_name": "destination"},
    )
    record = AuditEventRecord(
        id=1,
        guild_id=10,
        category="moderation",
        event_type="moderation.voice_moved",
        occurred_at=draft.occurred_at,
        created_at=draft.occurred_at,
        subject_type="user",
        subject_id=30,
        actor_user_id=20,
        channel_id=None,
        before_data=draft.before_data,
        after_data=draft.after_data,
        details_data={},
        discord_message_id=None,
        delivered_at=None,
        delivery_attempts=0,
        next_delivery_attempt_at=None,
        last_delivery_error=None,
        expires_at=None,
    )
    embed = build_audit_embed(record, report_timezone=ZoneInfo("UTC"))
    assert draft.retention_policy is AuditRetentionPolicy.IMPORTANT
    assert embed.title == "Участник перемещён Kanami" and "<#40> → <#50>" in (
        embed.description or ""
    )
