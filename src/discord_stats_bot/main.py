import asyncio
import logging
import time
from collections.abc import Callable
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.config import Settings
from discord_stats_bot.discord import (
    AchievementsCommandHandler,
    AuditEventIngestor,
    AuditLogDeliveryRunner,
    AuditRetentionRunner,
    AutoroleHandler,
    BotControlServer,
    DiscordBotProfileService,
    DiscordServerSettingsOptionsService,
    DiscordStatsClient,
    GameCheckpointRunner,
    GamePresenceEventHandler,
    GameStartupReconciler,
    GameStatisticsCommandHandler,
    GuildReferenceProvisioner,
    HealthCommandHandler,
    MemberAnniversariesCommandHandler,
    MemberAnniversaryCheckRunner,
    MemberProfileCommandHandler,
    MemberReturnEventHandler,
    OperationalHealthObservationRunner,
    RulesCommandHandler,
    RulesPublicationService,
    TextActivityEventHandler,
    TextLeaderboardCommandHandler,
    VoiceActivityCommandHandler,
    VoiceChannelLeaderboardCommandHandler,
    VoiceChannelStatisticsCommandHandler,
    VoiceCheckpointRunner,
    VoiceLeaderboardCommandHandler,
    VoiceServerStatisticsCommandHandler,
    VoiceStartupReconciler,
    VoiceStateEventHandler,
    VoiceStatisticsCommandHandler,
    VoiceTogetherCommandHandler,
    WebAdminAccessControlService,
    create_bot_control_app,
)
from discord_stats_bot.discord.server_settings import (
    RefreshableGuildServerSettingsProvider,
)
from discord_stats_bot.discord.server_settings_control import (
    DiscordServerSettingsControlService,
)
from discord_stats_bot.features.member_profile import MemberRoleConfiguration
from discord_stats_bot.features.server_settings import (
    EffectiveGuildServerSettings,
    GuildServerSettingsBaselines,
)
from discord_stats_bot.logging import configure_logging
from discord_stats_bot.persistence import create_database_resources

logger = logging.getLogger(__name__)


def _log_effective_server_settings(
    effective: EffectiveGuildServerSettings,
) -> None:
    def state(value: int | None) -> str:
        return "enabled" if value is not None else "disabled"

    logger.info(
        "Effective guild server settings: autorole=%s audit_log=%s "
        "anniversaries=%s member_returns=%s",
        state(effective.autorole_role_id),
        state(effective.audit_log_channel_id),
        state(effective.anniversary_channel_id),
        state(effective.return_channel_id),
    )


def _create_bot_control_server(
    settings: Settings,
    client: DiscordStatsClient,
    session_factory: async_sessionmaker[AsyncSession],
    server_settings_provider: RefreshableGuildServerSettingsProvider | None = None,
    server_settings_baselines: GuildServerSettingsBaselines | None = None,
    wake_delivery: Callable[[], None] | None = None,
    rules_publication_service: RulesPublicationService | None = None,
) -> BotControlServer | None:
    if not settings.discord_bot_control_enabled:
        logger.info("Bot control interface disabled")
        return None
    assert settings.discord_bot_control_shared_secret is not None
    if server_settings_baselines is None:
        server_settings_baselines = GuildServerSettingsBaselines(
            autorole_role_id=settings.discord_autorole_id,
            audit_log_channel_id=settings.discord_audit_log_channel_id,
            anniversary_channel_id=settings.discord_anniversary_channel_id,
            return_channel_id=settings.discord_return_channel_id,
        )
    if server_settings_provider is None:
        server_settings_provider = RefreshableGuildServerSettingsProvider(
            session_factory,
            guild_id=settings.discord_guild_id,
            baselines=server_settings_baselines,
        )
    control_app = create_bot_control_app(
        DiscordBotProfileService(client, guild_id=settings.discord_guild_id),
        shared_secret=settings.discord_bot_control_shared_secret,
        web_admin_access_operator=WebAdminAccessControlService(
            session_factory,
            guild_id=settings.discord_guild_id,
        ),
        server_settings_operator=DiscordServerSettingsControlService(
            client,
            session_factory,
            server_settings_provider,
            guild_id=settings.discord_guild_id,
            baselines=server_settings_baselines,
            wake_runtime=wake_delivery,
        ),
        server_settings_options_operator=DiscordServerSettingsOptionsService(
            client,
            guild_id=settings.discord_guild_id,
        ),
        rules_publication_operator=rules_publication_service,
    )
    server = BotControlServer(
        control_app,
        host=settings.discord_bot_control_host,
        port=settings.discord_bot_control_port,
    )
    logger.info(
        "Bot control interface enabled host=%s port=%s",
        settings.discord_bot_control_host,
        settings.discord_bot_control_port,
    )
    return server


async def run_application(settings: Settings) -> None:
    """Run the Discord Gateway client and own its persistence lifecycle."""

    process_started_at = time.monotonic()
    resources = create_database_resources(settings)
    provisioner = GuildReferenceProvisioner(resources.session_factory)
    reconciler = VoiceStartupReconciler(resources.session_factory)
    voice_event_handler = VoiceStateEventHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
    )
    voice_checkpointer = VoiceCheckpointRunner(resources.session_factory)
    game_presence_event_handler = (
        GamePresenceEventHandler(
            resources.session_factory,
            guild_id=settings.discord_guild_id,
        )
        if settings.game_tracking_enabled
        else None
    )
    game_startup_reconciler = (
        GameStartupReconciler(resources.session_factory)
        if settings.game_tracking_enabled
        else None
    )
    game_checkpointer = (
        GameCheckpointRunner(resources.session_factory)
        if settings.game_tracking_enabled
        else None
    )
    operational_health_runner = OperationalHealthObservationRunner(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        game_tracking_enabled=settings.game_tracking_enabled,
        voice_checkpoint_interval_seconds=settings.voice_checkpoint_interval_seconds,
        game_confirm_interval_seconds=settings.game_confirm_interval_seconds,
    )
    voice_stats_command_handler = VoiceStatisticsCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        min_session_seconds=settings.voice_min_session_seconds,
        checkpoint_interval_seconds=settings.voice_checkpoint_interval_seconds,
    )
    voice_leaderboard_command_handler = VoiceLeaderboardCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        min_session_seconds=settings.voice_min_session_seconds,
        checkpoint_interval_seconds=settings.voice_checkpoint_interval_seconds,
    )
    voice_channel_leaderboard_command_handler = VoiceChannelLeaderboardCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        min_session_seconds=settings.voice_min_session_seconds,
        checkpoint_interval_seconds=settings.voice_checkpoint_interval_seconds,
    )
    voice_channel_statistics_command_handler = VoiceChannelStatisticsCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        min_session_seconds=settings.voice_min_session_seconds,
        checkpoint_interval_seconds=settings.voice_checkpoint_interval_seconds,
    )
    voice_together_command_handler = VoiceTogetherCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        min_session_seconds=settings.voice_min_session_seconds,
        checkpoint_interval_seconds=settings.voice_checkpoint_interval_seconds,
    )
    voice_server_statistics_command_handler = VoiceServerStatisticsCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        min_session_seconds=settings.voice_min_session_seconds,
        checkpoint_interval_seconds=settings.voice_checkpoint_interval_seconds,
    )
    voice_activity_command_handler = VoiceActivityCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        min_session_seconds=settings.voice_min_session_seconds,
    )
    game_statistics_command_handler = GameStatisticsCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        tracking_enabled=settings.game_tracking_enabled,
        report_timezone=ZoneInfo(settings.report_timezone),
        checkpoint_interval_seconds=settings.game_confirm_interval_seconds,
    )
    achievements_command_handler = AchievementsCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        min_session_seconds=settings.voice_min_session_seconds,
    )
    member_profile_command_handler = MemberProfileCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        min_session_seconds=settings.voice_min_session_seconds,
        role_configuration=MemberRoleConfiguration(
            guest_role_id=settings.discord_guest_role_id,
            initiated_role_id=settings.discord_initiated_role_id,
            guardian_role_id=settings.discord_guardian_role_id,
            purple_role_id=settings.discord_purple_role_id,
            gold_role_id=settings.discord_gold_role_id,
        ),
    )
    member_anniversaries_command_handler = MemberAnniversariesCommandHandler(
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
    )
    health_command_handler = HealthCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        process_started_at=process_started_at,
    )
    rules_command_handler = RulesCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        accepted_role_id=settings.rules_accepted_role_id,
    )
    rules_publication_service = RulesPublicationService(
        resources.session_factory,
        rules_command_handler,
        guild_id=settings.discord_guild_id,
    )
    text_activity_event_handler = TextActivityEventHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
    )
    text_leaderboard_command_handler = TextLeaderboardCommandHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
    )
    server_settings_baselines = GuildServerSettingsBaselines(
        autorole_role_id=settings.discord_autorole_id,
        audit_log_channel_id=settings.discord_audit_log_channel_id,
        anniversary_channel_id=settings.discord_anniversary_channel_id,
        return_channel_id=settings.discord_return_channel_id,
    )
    server_settings_provider = RefreshableGuildServerSettingsProvider(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        baselines=server_settings_baselines,
    )
    initial_server_settings = await server_settings_provider.get()
    _log_effective_server_settings(initial_server_settings)
    audit_delivery_runner = AuditLogDeliveryRunner(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        settings_provider=server_settings_provider,
    )
    audit_retention_runner = AuditRetentionRunner(
        resources.session_factory,
        settings_provider=server_settings_provider,
    )
    audit_event_ingestor = AuditEventIngestor(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        transient_retention_days=settings.audit_transient_retention_days,
        wake_delivery=audit_delivery_runner.wake,
        report_timezone=ZoneInfo(settings.report_timezone),
        min_session_seconds=settings.voice_min_session_seconds,
        settings_provider=server_settings_provider,
    )
    member_anniversary_check_runner = MemberAnniversaryCheckRunner(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        wake_delivery=audit_delivery_runner.wake,
        settings_provider=server_settings_provider,
    )
    member_return_event_handler = MemberReturnEventHandler(
        resources.session_factory,
        guild_id=settings.discord_guild_id,
        report_timezone=ZoneInfo(settings.report_timezone),
        min_absence_seconds=settings.member_return_min_absence_seconds,
        min_session_seconds=settings.voice_min_session_seconds,
        wake_delivery=audit_delivery_runner.wake,
        settings_provider=server_settings_provider,
    )
    autorole_handler = AutoroleHandler(
        guild_id=settings.discord_guild_id,
        settings_provider=server_settings_provider,
    )
    client = DiscordStatsClient(
        guild_id=settings.discord_guild_id,
        reference_provisioner=provisioner,
        voice_reconciler=reconciler,
        voice_event_handler=voice_event_handler,
        voice_checkpointer=voice_checkpointer,
        voice_stats_command_handler=voice_stats_command_handler,
        voice_leaderboard_command_handler=voice_leaderboard_command_handler,
        voice_channel_leaderboard_command_handler=(
            voice_channel_leaderboard_command_handler
        ),
        voice_channel_statistics_command_handler=(
            voice_channel_statistics_command_handler
        ),
        voice_together_command_handler=voice_together_command_handler,
        voice_server_statistics_command_handler=(
            voice_server_statistics_command_handler
        ),
        voice_activity_command_handler=voice_activity_command_handler,
        game_statistics_command_handler=game_statistics_command_handler,
        achievements_command_handler=achievements_command_handler,
        member_profile_command_handler=member_profile_command_handler,
        member_anniversaries_command_handler=member_anniversaries_command_handler,
        member_anniversary_check_runner=member_anniversary_check_runner,
        member_return_event_handler=member_return_event_handler,
        health_command_handler=health_command_handler,
        rules_command_handler=rules_command_handler,
        rules_publication_syncer=rules_publication_service,
        text_activity_event_handler=text_activity_event_handler,
        text_leaderboard_command_handler=text_leaderboard_command_handler,
        audit_event_ingestor=audit_event_ingestor,
        audit_delivery_runner=audit_delivery_runner,
        audit_retention_runner=audit_retention_runner,
        autorole_handler=autorole_handler,
        game_presence_event_handler=game_presence_event_handler,
        game_startup_reconciler=game_startup_reconciler,
        game_checkpointer=game_checkpointer,
        operational_health_runner=operational_health_runner,
        voice_checkpoint_interval_seconds=(settings.voice_checkpoint_interval_seconds),
        game_confirm_interval_seconds=settings.game_confirm_interval_seconds,
    )
    rules_publication_service.bind_client(client)
    control_server = _create_bot_control_server(
        settings,
        client,
        resources.session_factory,
        server_settings_provider=server_settings_provider,
        server_settings_baselines=server_settings_baselines,
        wake_delivery=audit_delivery_runner.wake,
        rules_publication_service=rules_publication_service,
    )
    try:
        async with client:
            if control_server is not None:
                await control_server.start()
            try:
                await client.start(settings.discord_token.get_secret_value())
            finally:
                if control_server is not None:
                    await control_server.stop()
    finally:
        await resources.dispose()


def main() -> int:
    """Validate configuration, configure logging, and run the application."""

    settings = Settings()
    configure_logging(settings.log_level)
    logger.info("Application configuration validated; starting Discord Gateway")
    try:
        asyncio.run(run_application(settings))
    except KeyboardInterrupt:
        logger.info("Shutdown requested; application stopped normally")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
