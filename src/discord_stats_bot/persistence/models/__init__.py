"""Declarative persistence models registered on the shared metadata."""

from discord_stats_bot.persistence.models.achievement import UserAchievement
from discord_stats_bot.persistence.models.audit import AuditEvent
from discord_stats_bot.persistence.models.base import Base
from discord_stats_bot.persistence.models.game import GameSession
from discord_stats_bot.persistence.models.guild import Guild
from discord_stats_bot.persistence.models.member import GuildMember
from discord_stats_bot.persistence.models.operational_health import (
    OperationalHealthObservation,
)
from discord_stats_bot.persistence.models.rules import RuleAcceptance, Ruleset
from discord_stats_bot.persistence.models.server_settings import GuildServerSettings
from discord_stats_bot.persistence.models.text_activity import DailyTextActivity
from discord_stats_bot.persistence.models.user import DiscordUser
from discord_stats_bot.persistence.models.voice import (
    VoiceChannel,
    VoiceInterval,
    VoiceSession,
)
from discord_stats_bot.persistence.models.web_admin_access import WebAdminAccessGrant

__all__ = [
    "Base",
    "AuditEvent",
    "DiscordUser",
    "DailyTextActivity",
    "Guild",
    "GameSession",
    "GuildMember",
    "OperationalHealthObservation",
    "RuleAcceptance",
    "Ruleset",
    "GuildServerSettings",
    "VoiceChannel",
    "VoiceInterval",
    "VoiceSession",
    "UserAchievement",
    "WebAdminAccessGrant",
]
