import discord
import pytest

from discord_stats_bot.discord import DiscordStatsClient, build_kanami_help_embed
from tests.support.discord import make_interaction


class NoOpDependency:
    pass


def make_client() -> DiscordStatsClient:
    return DiscordStatsClient(
        guild_id=10,
        reference_provisioner=NoOpDependency(),  # type: ignore[arg-type]
        voice_reconciler=NoOpDependency(),  # type: ignore[arg-type]
        voice_event_handler=NoOpDependency(),  # type: ignore[arg-type]
    )


def test_help_embed_lists_only_current_commands() -> None:
    embed = build_kanami_help_embed()
    text = "\n".join(
        [embed.title or "", embed.description or ""]
        + [f"{field.name}\n{field.value}" for field in embed.fields]
    )

    for command in (
        "/stats",
        "/top",
        "/topmessages",
        "/channels",
        "/channelstats",
        "/together",
        "/serverstats",
        "/activity",
        "/achievements",
        "/anniversaries",
        "/rules",
        "/rules-status",
        "/help",
        "/health",
    ):
        assert command in text
    assert "/leaderboard" not in text
    assert "профиль своей или выбранной голосовой активности за период" in text
    assert "TOP-10 участников по количеству сообщений" in text
    assert "Управлять сервером" in text
    assert embed.footer.text is not None
    assert "Discord" in embed.footer.text


def test_help_is_registered_for_configured_guild_only() -> None:
    client = make_client()

    assert client.tree.get_command("help", guild=client._command_guild) is not None
    assert client.tree.get_command("help") is None


def test_client_has_static_online_help_game_presence() -> None:
    client = make_client()

    assert client.status is discord.Status.online
    assert isinstance(client.activity, discord.Game)
    assert client.activity.name == "/help • команды бота"


@pytest.mark.asyncio
async def test_help_responds_ephemeral_without_database_handler() -> None:
    client = make_client()
    interaction = make_interaction()

    await client._handle_help_command(interaction)  # type: ignore[arg-type]

    assert len(interaction.response.messages) == 1
    args, message = interaction.response.messages[0]
    assert args == ()
    assert message["ephemeral"] is True
    assert message["embed"].title == "Kanami — статистика сервера"
    allowed_mentions = message["allowed_mentions"]
    assert allowed_mentions.everyone is False
    assert allowed_mentions.users is False
    assert allowed_mentions.roles is False
