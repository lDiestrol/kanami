"""Discord Gateway adapters and runtime."""

from discord_stats_bot.discord.achievements import (
    AchievementsCommandHandler,
    build_achievements_embed,
)
from discord_stats_bot.discord.audit_logging import (
    AuditEventIngestor,
    AuditLogDeliveryRunner,
    AuditRetentionRunner,
    build_audit_embed,
)
from discord_stats_bot.discord.autorole import AUTOROLE_REASON, AutoroleHandler
from discord_stats_bot.discord.bot_control import (
    BotControlServer,
    DiscordBotProfileService,
    create_bot_control_app,
)
from discord_stats_bot.discord.game_statistics import (
    GAME_PERIOD_LABELS,
    GameStatisticsCommandHandler,
    build_game_statistics_embed,
)
from discord_stats_bot.discord.game_tracking import (
    GameCheckpointRunner,
    GamePresenceEventHandler,
    GameStartupReconciler,
)
from discord_stats_bot.discord.health import (
    DatabaseHealth,
    HealthCommandHandler,
    HealthRuntimeSnapshot,
    build_health_embed,
)
from discord_stats_bot.discord.kanami_help import build_kanami_help_embed
from discord_stats_bot.discord.member_anniversaries import (
    ANNIVERSARY_DAILY_CHECK_TIME,
    ANNIVERSARY_WINDOW_DAYS,
    MemberAnniversariesCommandHandler,
    MemberAnniversaryCheckRunner,
    build_member_anniversaries_embed,
    build_member_anniversary_notification_embed,
)
from discord_stats_bot.discord.member_profile import (
    MemberProfileCommandHandler,
    build_member_profile_embed,
)
from discord_stats_bot.discord.member_returns import (
    MemberReturnEventHandler,
    build_member_return_embed,
)
from discord_stats_bot.discord.operational_health import (
    OperationalHealthObservationRunner,
)
from discord_stats_bot.discord.rules import (
    RULES_ACCEPT_BUTTON_CUSTOM_ID,
    RulesAcceptanceView,
    RulesCommandHandler,
    build_rules_embed,
    build_rules_status_embed,
)
from discord_stats_bot.discord.rules_publication import RulesPublicationService
from discord_stats_bot.discord.runtime import (
    DiscordStatsClient,
    GuildReferenceProvisioner,
    GuildReferenceProvisioningSummary,
    TextActivityEventHandler,
    VoiceCheckpointRunner,
    VoiceCheckpointSummary,
    VoiceStartupReconciler,
    VoiceStartupReconciliationSummary,
    VoiceStateEventHandler,
    create_gateway_intents,
)
from discord_stats_bot.discord.server_settings_options import (
    DiscordServerSettingsOptionsService,
)
from discord_stats_bot.discord.text_leaderboard import (
    TEXT_PERIOD_LABELS,
    TextLeaderboardCommandHandler,
    build_text_leaderboard_embed,
)
from discord_stats_bot.discord.voice_activity import (
    ACTIVITY_PERIOD_LABELS,
    VoiceActivityCommandHandler,
    build_voice_activity_embed,
)
from discord_stats_bot.discord.voice_channel_stats import (
    VoiceChannelStatisticsCommandHandler,
    build_voice_channel_statistics_embed,
)
from discord_stats_bot.discord.voice_channels import (
    VoiceChannelLeaderboardCommandHandler,
    build_voice_channel_leaderboard_embed,
)
from discord_stats_bot.discord.voice_leaderboard import (
    VOICE_PERIOD_LABELS,
    VoiceLeaderboardCommandHandler,
    build_voice_leaderboard_embed,
)
from discord_stats_bot.discord.voice_server_stats import (
    VoiceServerStatisticsCommandHandler,
    build_voice_server_statistics_embed,
)
from discord_stats_bot.discord.voice_stats import (
    VoiceStatisticsCommandHandler,
    build_voice_statistics_embed,
    format_voice_duration,
)
from discord_stats_bot.discord.voice_together import (
    VoiceTogetherCommandHandler,
    build_voice_pair_statistics_embed,
    format_voice_percentage,
)
from discord_stats_bot.discord.web_admin_access_control import (
    WebAdminAccessControlService,
)

__all__ = [
    "AchievementsCommandHandler",
    "AuditEventIngestor",
    "AuditLogDeliveryRunner",
    "AuditRetentionRunner",
    "AUTOROLE_REASON",
    "AutoroleHandler",
    "BotControlServer",
    "DiscordStatsClient",
    "DiscordBotProfileService",
    "DiscordServerSettingsOptionsService",
    "DatabaseHealth",
    "GuildReferenceProvisioner",
    "GuildReferenceProvisioningSummary",
    "GameCheckpointRunner",
    "GamePresenceEventHandler",
    "GameStartupReconciler",
    "GameStatisticsCommandHandler",
    "GAME_PERIOD_LABELS",
    "HealthCommandHandler",
    "HealthRuntimeSnapshot",
    "MemberAnniversariesCommandHandler",
    "MemberAnniversaryCheckRunner",
    "MemberReturnEventHandler",
    "MemberProfileCommandHandler",
    "OperationalHealthObservationRunner",
    "RULES_ACCEPT_BUTTON_CUSTOM_ID",
    "RulesAcceptanceView",
    "RulesCommandHandler",
    "RulesPublicationService",
    "TextActivityEventHandler",
    "TextLeaderboardCommandHandler",
    "TEXT_PERIOD_LABELS",
    "WebAdminAccessControlService",
    "VoiceCheckpointRunner",
    "VoiceCheckpointSummary",
    "VoiceChannelLeaderboardCommandHandler",
    "VoiceChannelStatisticsCommandHandler",
    "VoiceStateEventHandler",
    "VOICE_PERIOD_LABELS",
    "VoiceLeaderboardCommandHandler",
    "VoiceServerStatisticsCommandHandler",
    "VoiceStatisticsCommandHandler",
    "VoiceTogetherCommandHandler",
    "VoiceActivityCommandHandler",
    "ACTIVITY_PERIOD_LABELS",
    "build_voice_statistics_embed",
    "build_voice_activity_embed",
    "build_achievements_embed",
    "build_audit_embed",
    "build_voice_channel_leaderboard_embed",
    "build_kanami_help_embed",
    "build_member_anniversaries_embed",
    "build_member_anniversary_notification_embed",
    "build_member_return_embed",
    "build_member_profile_embed",
    "build_health_embed",
    "build_game_statistics_embed",
    "build_rules_embed",
    "build_rules_status_embed",
    "build_text_leaderboard_embed",
    "build_voice_channel_statistics_embed",
    "build_voice_leaderboard_embed",
    "build_voice_pair_statistics_embed",
    "build_voice_server_statistics_embed",
    "VoiceStartupReconciler",
    "VoiceStartupReconciliationSummary",
    "create_gateway_intents",
    "create_bot_control_app",
    "format_voice_duration",
    "format_voice_percentage",
    "ANNIVERSARY_WINDOW_DAYS",
    "ANNIVERSARY_DAILY_CHECK_TIME",
]
