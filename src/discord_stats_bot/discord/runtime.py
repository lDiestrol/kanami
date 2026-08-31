"""Discord Gateway adapters for reference provisioning and voice tracking."""

import asyncio
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.discord.achievements import AchievementsCommandHandler
from discord_stats_bot.discord.audit_logging import (
    AuditEventIngestor,
    AuditLogDeliveryRunner,
    AuditRetentionRunner,
)
from discord_stats_bot.discord.autorole import AutoroleHandler
from discord_stats_bot.discord.game_statistics import GameStatisticsCommandHandler
from discord_stats_bot.discord.game_tracking import (
    GameCheckpointRunner,
    GamePresenceEventHandler,
    GameStartupReconciler,
)
from discord_stats_bot.discord.health import HealthCommandHandler, HealthRuntimeSnapshot
from discord_stats_bot.discord.kanami_help import build_kanami_help_embed
from discord_stats_bot.discord.member_anniversaries import (
    MemberAnniversariesCommandHandler,
    MemberAnniversaryCheckRunner,
)
from discord_stats_bot.discord.member_profile import MemberProfileCommandHandler
from discord_stats_bot.discord.member_returns import MemberReturnEventHandler
from discord_stats_bot.discord.operational_health import (
    OperationalHealthObservationRunner,
)
from discord_stats_bot.discord.rules import RulesCommandHandler
from discord_stats_bot.discord.text_leaderboard import TextLeaderboardCommandHandler
from discord_stats_bot.discord.voice_activity import VoiceActivityCommandHandler
from discord_stats_bot.discord.voice_channel_stats import (
    VoiceChannelStatisticsCommandHandler,
)
from discord_stats_bot.discord.voice_channels import (
    VoiceChannelLeaderboardCommandHandler,
)
from discord_stats_bot.discord.voice_leaderboard import VoiceLeaderboardCommandHandler
from discord_stats_bot.discord.voice_server_stats import (
    VoiceServerStatisticsCommandHandler,
)
from discord_stats_bot.discord.voice_stats import VoiceStatisticsCommandHandler
from discord_stats_bot.discord.voice_together import VoiceTogetherCommandHandler
from discord_stats_bot.features.reference_data import (
    DiscordUserSnapshot,
    GuildMemberSnapshot,
    GuildReferenceSnapshot,
    GuildSnapshot,
    ReferenceDataProvisioningRepository,
    ReferenceDataProvisioningService,
    VoiceChannelSnapshot,
)
from discord_stats_bot.features.text_activity import (
    TextActivityRepository,
    TextActivityService,
    TextMessageActivity,
)
from discord_stats_bot.features.voice import (
    ObservedVoiceState,
    VoiceTrackingService,
    VoiceTransitionRepository,
    VoiceTransitionResult,
)
from discord_stats_bot.features.voice.types import normalize_observed_at
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyReferenceDataRepository,
    SqlAlchemyTextActivityRepository,
    SqlAlchemyVoiceTransitionRepository,
)

logger = logging.getLogger(__name__)


class VoiceStartupRepository(VoiceTransitionRepository, Protocol):
    """Repository contract additionally needed by startup reconciliation."""

    async def list_open_user_ids(self, guild_id: int) -> tuple[int, ...]: ...


VoiceRepositoryFactory = Callable[[AsyncSession], VoiceStartupRepository]


class RulesPublicationSyncer(Protocol):
    async def sync(self) -> object: ...


LiveVoiceRepositoryFactory = Callable[[AsyncSession], VoiceTransitionRepository]
ReferenceRepositoryFactory = Callable[
    [AsyncSession], ReferenceDataProvisioningRepository
]
TextActivityRepositoryFactory = Callable[[AsyncSession], TextActivityRepository]


@dataclass(frozen=True, slots=True)
class GuildReferenceProvisioningSummary:
    """Counts from one successfully committed guild cache snapshot."""

    user_count: int
    member_count: int
    voice_channel_count: int


class GuildReferenceProvisioner:
    """Adapt a Discord guild cache to the reference provisioning service."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository_factory: ReferenceRepositoryFactory = (
            SqlAlchemyReferenceDataRepository
        ),
    ) -> None:
        self._session_factory = session_factory
        self._repository_factory = repository_factory

    async def provision_guild(
        self,
        guild: discord.Guild,
    ) -> GuildReferenceProvisioningSummary:
        """Upsert one complete available cache snapshot in one transaction."""

        snapshot = self._collect_snapshot(guild)
        async with self._session_factory.begin() as session:
            service = ReferenceDataProvisioningService(
                self._repository_factory(session)
            )
            await service.provision_guild(snapshot)
        return GuildReferenceProvisioningSummary(
            user_count=len(snapshot.users),
            member_count=len(snapshot.members),
            voice_channel_count=len(snapshot.voice_channels),
        )

    async def provision_member(
        self,
        member: discord.Member,
        *,
        identity_user: discord.User | discord.Member | None = None,
    ) -> None:
        """Upsert one complete current member snapshot."""

        async with self._session_factory.begin() as session:
            service = ReferenceDataProvisioningService(
                self._repository_factory(session)
            )
            await service.provision_guild(
                _member_reference_snapshot(
                    member.guild,
                    member,
                    identity_user=identity_user,
                )
            )

    async def mark_member_left(
        self,
        member: discord.Member,
        left_at: datetime,
    ) -> None:
        """Persist final identity and mark one membership departed atomically."""

        left_at = normalize_observed_at(left_at)
        async with self._session_factory.begin() as session:
            service = ReferenceDataProvisioningService(
                self._repository_factory(session)
            )
            await service.provision_guild(
                _member_reference_snapshot(member.guild, member)
            )
            await service.mark_member_left(
                guild_id=member.guild.id,
                user_id=member.id,
                left_at=left_at,
            )

    @staticmethod
    def _collect_snapshot(guild: discord.Guild) -> GuildReferenceSnapshot:
        users = tuple(
            _discord_user_snapshot(member)
            for member in sorted(guild.members, key=lambda member: member.id)
        )
        members = tuple(
            _guild_member_snapshot(guild, member)
            for member in sorted(guild.members, key=lambda member: member.id)
        )
        afk_channel_id = guild.afk_channel.id if guild.afk_channel is not None else None
        channels = tuple(
            sorted(
                (
                    *(
                        VoiceChannelSnapshot(
                            id=channel.id,
                            guild_id=guild.id,
                            name=channel.name,
                            channel_kind="voice",
                            is_afk=channel.id == afk_channel_id,
                        )
                        for channel in guild.voice_channels
                    ),
                    *(
                        VoiceChannelSnapshot(
                            id=channel.id,
                            guild_id=guild.id,
                            name=channel.name,
                            channel_kind="stage",
                            is_afk=channel.id == afk_channel_id,
                        )
                        for channel in guild.stage_channels
                    ),
                ),
                key=lambda channel: channel.id,
            )
        )
        return GuildReferenceSnapshot(
            guild=GuildSnapshot(id=guild.id, name=guild.name),
            users=users,
            members=members,
            voice_channels=channels,
        )


def _discord_user_snapshot(
    user: discord.User | discord.Member,
) -> DiscordUserSnapshot:
    return DiscordUserSnapshot(
        id=user.id,
        is_bot=user.bot,
        username=getattr(user, "name", None),
        global_name=getattr(user, "global_name", None),
        avatar_hash=_discord_asset_key(getattr(user, "avatar", None)),
    )


def _guild_member_snapshot(
    guild: discord.Guild,
    user: discord.User | discord.Member,
) -> GuildMemberSnapshot:
    has_complete_guild_identity = hasattr(user, "nick")
    return GuildMemberSnapshot(
        guild_id=guild.id,
        user_id=user.id,
        joined_at=getattr(user, "joined_at", None),
        nickname=(getattr(user, "nick", None) if has_complete_guild_identity else None),
        has_complete_guild_identity=has_complete_guild_identity,
        guild_avatar_hash=(
            _discord_asset_key(getattr(user, "guild_avatar", None))
            if has_complete_guild_identity
            else None
        ),
    )


def _discord_asset_key(asset: object | None) -> str | None:
    key = getattr(asset, "key", None)
    return key if isinstance(key, str) and key else None


def _member_reference_snapshot(
    guild: discord.Guild,
    user: discord.User | discord.Member,
    *,
    identity_user: discord.User | discord.Member | None = None,
    voice_channels: tuple[VoiceChannelSnapshot, ...] = (),
) -> GuildReferenceSnapshot:
    """Build the shared targeted guild/user/member reference snapshot."""

    return GuildReferenceSnapshot(
        guild=GuildSnapshot(id=guild.id, name=guild.name),
        users=(
            _discord_user_snapshot(
                identity_user if identity_user is not None else user
            ),
        ),
        members=(_guild_member_snapshot(guild, user),),
        voice_channels=voice_channels,
    )


class TextActivityEventHandler:
    """Filter one MESSAGE_CREATE and persist privacy-safe daily counters."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        report_timezone: ZoneInfo,
        text_repository_factory: TextActivityRepositoryFactory = (
            SqlAlchemyTextActivityRepository
        ),
        reference_repository_factory: ReferenceRepositoryFactory = (
            SqlAlchemyReferenceDataRepository
        ),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._report_timezone = report_timezone
        self._text_repository_factory = text_repository_factory
        self._reference_repository_factory = reference_repository_factory

    async def handle(self, message: discord.Message) -> bool:
        """Count a relevant user message; return whether it was persisted."""

        guild = message.guild
        if guild is None or guild.id != self._guild_id:
            return False
        author = message.author
        if author.bot or message.webhook_id is not None:
            return False
        if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
            return False

        channel_id = message.channel.id
        try:
            async with self._session_factory.begin() as session:
                references = ReferenceDataProvisioningService(
                    self._reference_repository_factory(session)
                )
                await references.provision_guild(
                    _member_reference_snapshot(guild, author)
                )
                activity = TextActivityService(
                    self._text_repository_factory(session),
                    report_timezone=self._report_timezone,
                )
                await activity.record_message(
                    TextMessageActivity(
                        guild_id=guild.id,
                        user_id=author.id,
                        channel_id=channel_id,
                        occurred_at=message.created_at,
                        attachment_count=0,
                        is_reply=message.type is discord.MessageType.reply,
                    )
                )
        except Exception:
            logger.exception(
                "Text activity persistence failed guild_id=%s user_id=%s channel_id=%s",
                guild.id,
                author.id,
                channel_id,
            )
            return False
        return True


class VoiceStateEventHandler:
    """Adapt one live Discord voice-state event to application services."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        guild_id: int,
        voice_repository_factory: LiveVoiceRepositoryFactory = (
            SqlAlchemyVoiceTransitionRepository
        ),
        reference_repository_factory: ReferenceRepositoryFactory = (
            SqlAlchemyReferenceDataRepository
        ),
    ) -> None:
        self._session_factory = session_factory
        self._guild_id = guild_id
        self._voice_repository_factory = voice_repository_factory
        self._reference_repository_factory = reference_repository_factory

    async def handle(
        self,
        member: discord.Member,
        after: discord.VoiceState,
        observed_at: datetime,
    ) -> VoiceTransitionResult | None:
        """Apply one relevant event and isolate its persistence failures."""

        guild = member.guild
        if guild.id != self._guild_id or member.bot:
            return None

        observed_at = normalize_observed_at(observed_at)
        channel = after.channel
        channel_id = channel.id if channel is not None else None
        try:
            reference_snapshot = self._collect_reference_snapshot(member, channel)
            async with self._session_factory.begin() as session:
                reference_service = ReferenceDataProvisioningService(
                    self._reference_repository_factory(session)
                )
                await reference_service.provision_guild(reference_snapshot)

                voice_service = VoiceTrackingService(
                    self._voice_repository_factory(session)
                )
                if channel is None:
                    result = await voice_service.observe_disconnected(
                        guild.id,
                        member.id,
                        observed_at,
                    )
                else:
                    channel_kind = self._channel_kind(channel)
                    result = await voice_service.observe_connected(
                        ObservedVoiceState(
                            guild_id=guild.id,
                            user_id=member.id,
                            channel_id=channel.id,
                            channel_kind=channel_kind,
                            is_afk=(
                                guild.afk_channel is not None
                                and channel.id == guild.afk_channel.id
                            ),
                            observed_at=observed_at,
                        )
                    )
        except Exception:
            logger.exception(
                "Live voice-state transition failed guild_id=%s user_id=%s "
                "channel_id=%s connected=%s",
                guild.id,
                member.id,
                channel_id,
                channel is not None,
            )
            return None

        logger.debug(
            "Live voice-state transition completed guild_id=%s user_id=%s "
            "channel_id=%s outcome=%s",
            guild.id,
            member.id,
            channel_id,
            result.value,
        )
        return result

    @classmethod
    def _collect_reference_snapshot(
        cls,
        member: discord.Member,
        channel: discord.VoiceChannel | discord.StageChannel | None,
    ) -> GuildReferenceSnapshot:
        guild = member.guild
        channels = (
            ()
            if channel is None
            else (
                VoiceChannelSnapshot(
                    id=channel.id,
                    guild_id=guild.id,
                    name=channel.name,
                    channel_kind=cls._channel_kind(channel),
                    is_afk=(
                        guild.afk_channel is not None
                        and channel.id == guild.afk_channel.id
                    ),
                ),
            )
        )
        return _member_reference_snapshot(
            guild,
            member,
            voice_channels=channels,
        )

    @staticmethod
    def _channel_kind(
        channel: discord.VoiceChannel | discord.StageChannel,
    ) -> str:
        if channel.type == discord.ChannelType.voice:
            return "voice"
        if channel.type == discord.ChannelType.stage_voice:
            return "stage"
        raise ValueError(f"unsupported voice channel type: {channel.type!s}")


@dataclass(frozen=True, slots=True)
class VoiceStartupReconciliationSummary:
    """Compact result of reconciling one guild at one timestamp."""

    reconciled_at: datetime
    connected_count: int
    disconnected_count: int
    outcomes: dict[VoiceTransitionResult, int]
    failed_count: int


@dataclass(frozen=True, slots=True)
class VoiceCheckpointSummary:
    """Compact result of one connected-only voice checkpoint cycle."""

    checkpointed_at: datetime
    connected_count: int
    outcomes: dict[VoiceTransitionResult, int]
    failed_count: int


class VoiceCheckpointRunner:
    """Confirm the current connected guild cache in per-user transactions."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository_factory: LiveVoiceRepositoryFactory = (
            SqlAlchemyVoiceTransitionRepository
        ),
    ) -> None:
        self._session_factory = session_factory
        self._repository_factory = repository_factory

    async def checkpoint_guild(
        self,
        guild: discord.Guild,
        checkpointed_at: datetime,
    ) -> VoiceCheckpointSummary:
        """Confirm cached connected non-bots without inferring disconnections."""

        checkpointed_at = normalize_observed_at(checkpointed_at)
        connected = self._collect_connected(guild, checkpointed_at)
        user_ids = tuple(connected)
        results = await asyncio.gather(
            *(self._checkpoint_connected(connected[user_id]) for user_id in user_ids),
            return_exceptions=True,
        )

        outcomes: Counter[VoiceTransitionResult] = Counter()
        failed_count = 0
        for user_id, result in zip(user_ids, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                failed_count += 1
                logger.error(
                    "Voice checkpoint failed guild_id=%s user_id=%s",
                    guild.id,
                    user_id,
                    exc_info=(type(result), result, result.__traceback__),
                )
            else:
                outcomes[result] += 1

        return VoiceCheckpointSummary(
            checkpointed_at=checkpointed_at,
            connected_count=len(connected),
            outcomes=dict(outcomes),
            failed_count=failed_count,
        )

    @staticmethod
    def _collect_connected(
        guild: discord.Guild,
        checkpointed_at: datetime,
    ) -> dict[int, ObservedVoiceState]:
        connected: dict[int, ObservedVoiceState] = {}
        afk_channel_id = guild.afk_channel.id if guild.afk_channel is not None else None
        for channels, channel_kind in (
            (guild.voice_channels, "voice"),
            (guild.stage_channels, "stage"),
        ):
            for channel in channels:
                for user_id in channel.voice_states:
                    member = guild.get_member(user_id)
                    if member is None:
                        logger.warning(
                            "Skipping uncached voice member during checkpoint "
                            "guild_id=%s user_id=%s",
                            guild.id,
                            user_id,
                        )
                        continue
                    if member.bot:
                        continue
                    connected[user_id] = ObservedVoiceState(
                        guild_id=guild.id,
                        user_id=user_id,
                        channel_id=channel.id,
                        channel_kind=channel_kind,
                        is_afk=channel.id == afk_channel_id,
                        observed_at=checkpointed_at,
                    )
        return connected

    async def _checkpoint_connected(
        self,
        observed: ObservedVoiceState,
    ) -> VoiceTransitionResult:
        async with self._session_factory.begin() as session:
            service = VoiceTrackingService(self._repository_factory(session))
            return await service.observe_connected(observed)


class VoiceStartupReconciler:
    """Adapt cached Discord voice states to transactional application service calls."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        repository_factory: VoiceRepositoryFactory = SqlAlchemyVoiceTransitionRepository,
    ) -> None:
        self._session_factory = session_factory
        self._repository_factory = repository_factory

    async def reconcile_guild(
        self,
        guild: discord.Guild,
        reconciled_at: datetime,
    ) -> VoiceStartupReconciliationSummary:
        """Reconcile all relevant guild members using one normalized UTC ``R``."""

        reconciled_at = normalize_observed_at(reconciled_at)
        connected, connected_user_ids = self._collect_connected(
            guild,
            reconciled_at,
        )
        open_user_ids = await self._list_open_user_ids(guild.id)
        disconnected_user_ids = sorted(open_user_ids - connected_user_ids)

        operations = [
            self._reconcile_connected(observed) for observed in connected.values()
        ]
        operations.extend(
            self._reconcile_disconnected(guild.id, user_id, reconciled_at)
            for user_id in disconnected_user_ids
        )
        results = await asyncio.gather(*operations, return_exceptions=True)

        outcomes: Counter[VoiceTransitionResult] = Counter()
        failed_count = 0
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                failed_count += 1
                logger.error(
                    "Voice startup reconciliation failed for one guild member",
                    exc_info=(type(result), result, result.__traceback__),
                )
            else:
                outcomes[result] += 1

        return VoiceStartupReconciliationSummary(
            reconciled_at=reconciled_at,
            connected_count=len(connected),
            disconnected_count=len(disconnected_user_ids),
            outcomes=dict(outcomes),
            failed_count=failed_count,
        )

    def _collect_connected(
        self,
        guild: discord.Guild,
        reconciled_at: datetime,
    ) -> tuple[dict[int, ObservedVoiceState], set[int]]:
        connected: dict[int, ObservedVoiceState] = {}
        connected_user_ids: set[int] = set()
        afk_channel_id = guild.afk_channel.id if guild.afk_channel is not None else None

        channel_groups = (
            (guild.voice_channels, "voice"),
            (guild.stage_channels, "stage"),
        )
        for channels, channel_kind in channel_groups:
            for channel in channels:
                for user_id in channel.voice_states:
                    connected_user_ids.add(user_id)
                    member = guild.get_member(user_id)
                    if member is None:
                        logger.warning(
                            "Skipping uncached voice member during startup reconciliation "
                            "guild_id=%s user_id=%s",
                            guild.id,
                            user_id,
                        )
                        continue
                    if member.bot:
                        continue
                    connected[user_id] = ObservedVoiceState(
                        guild_id=guild.id,
                        user_id=user_id,
                        channel_id=channel.id,
                        channel_kind=channel_kind,
                        is_afk=channel.id == afk_channel_id,
                        observed_at=reconciled_at,
                    )

        return connected, connected_user_ids

    async def _list_open_user_ids(self, guild_id: int) -> set[int]:
        async with self._session_factory() as session:
            repository = self._repository_factory(session)
            return set(await repository.list_open_user_ids(guild_id))

    async def _reconcile_connected(
        self,
        observed: ObservedVoiceState,
    ) -> VoiceTransitionResult:
        async with self._session_factory.begin() as session:
            service = VoiceTrackingService(self._repository_factory(session))
            return await service.reconcile_connected(observed)

    async def _reconcile_disconnected(
        self,
        guild_id: int,
        user_id: int,
        reconciled_at: datetime,
    ) -> VoiceTransitionResult:
        async with self._session_factory.begin() as session:
            service = VoiceTrackingService(self._repository_factory(session))
            return await service.reconcile_disconnected(
                guild_id,
                user_id,
                reconciled_at,
            )


def create_gateway_intents(*, game_tracking_enabled: bool = False) -> discord.Intents:
    """Create only the intents currently required by the approved MVP wiring."""

    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    intents.moderation = True
    intents.voice_states = True
    intents.guild_messages = True
    intents.presences = game_tracking_enabled
    return intents


class DiscordStatsClient(discord.Client):
    """Discord client wiring startup recovery and live voice-state events."""

    def __init__(
        self,
        *,
        guild_id: int,
        reference_provisioner: GuildReferenceProvisioner,
        voice_reconciler: VoiceStartupReconciler,
        voice_event_handler: VoiceStateEventHandler,
        voice_checkpointer: VoiceCheckpointRunner | None = None,
        voice_stats_command_handler: VoiceStatisticsCommandHandler | None = None,
        voice_leaderboard_command_handler: VoiceLeaderboardCommandHandler | None = None,
        voice_channel_leaderboard_command_handler: (
            VoiceChannelLeaderboardCommandHandler | None
        ) = None,
        voice_channel_statistics_command_handler: (
            VoiceChannelStatisticsCommandHandler | None
        ) = None,
        voice_together_command_handler: VoiceTogetherCommandHandler | None = None,
        voice_server_statistics_command_handler: (
            VoiceServerStatisticsCommandHandler | None
        ) = None,
        voice_activity_command_handler: VoiceActivityCommandHandler | None = None,
        game_statistics_command_handler: GameStatisticsCommandHandler | None = None,
        achievements_command_handler: AchievementsCommandHandler | None = None,
        member_profile_command_handler: MemberProfileCommandHandler | None = None,
        member_anniversaries_command_handler: (
            MemberAnniversariesCommandHandler | None
        ) = None,
        member_anniversary_check_runner: MemberAnniversaryCheckRunner | None = None,
        member_return_event_handler: MemberReturnEventHandler | None = None,
        health_command_handler: HealthCommandHandler | None = None,
        rules_command_handler: RulesCommandHandler | None = None,
        rules_publication_syncer: RulesPublicationSyncer | None = None,
        text_activity_event_handler: TextActivityEventHandler | None = None,
        text_leaderboard_command_handler: TextLeaderboardCommandHandler | None = None,
        audit_event_ingestor: AuditEventIngestor | None = None,
        audit_delivery_runner: AuditLogDeliveryRunner | None = None,
        audit_retention_runner: AuditRetentionRunner | None = None,
        autorole_handler: AutoroleHandler | None = None,
        game_presence_event_handler: GamePresenceEventHandler | None = None,
        game_startup_reconciler: GameStartupReconciler | None = None,
        game_checkpointer: GameCheckpointRunner | None = None,
        operational_health_runner: OperationalHealthObservationRunner | None = None,
        voice_checkpoint_interval_seconds: int = 60,
        game_confirm_interval_seconds: int = 60,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        intents: discord.Intents | None = None,
    ) -> None:
        super().__init__(
            intents=intents
            or create_gateway_intents(
                game_tracking_enabled=game_presence_event_handler is not None
            ),
            status=discord.Status.online,
            activity=discord.Game("/help • команды бота"),
        )
        self._guild_id = guild_id
        self._reference_provisioner = reference_provisioner
        self._voice_reconciler = voice_reconciler
        self._voice_event_handler = voice_event_handler
        self._voice_checkpointer = voice_checkpointer
        self._voice_stats_command_handler = voice_stats_command_handler
        self._voice_leaderboard_command_handler = voice_leaderboard_command_handler
        self._voice_channel_leaderboard_command_handler = (
            voice_channel_leaderboard_command_handler
        )
        self._voice_channel_statistics_command_handler = (
            voice_channel_statistics_command_handler
        )
        self._voice_together_command_handler = voice_together_command_handler
        self._voice_server_statistics_command_handler = (
            voice_server_statistics_command_handler
        )
        self._voice_activity_command_handler = voice_activity_command_handler
        self._game_statistics_command_handler = game_statistics_command_handler
        self._achievements_command_handler = achievements_command_handler
        self._member_profile_command_handler = member_profile_command_handler
        self._member_anniversaries_command_handler = (
            member_anniversaries_command_handler
        )
        self._member_anniversary_check_runner = member_anniversary_check_runner
        self._member_return_event_handler = member_return_event_handler
        self._health_command_handler = health_command_handler
        self._rules_command_handler = rules_command_handler
        self._rules_publication_syncer = rules_publication_syncer
        self._text_activity_event_handler = text_activity_event_handler
        self._text_leaderboard_command_handler = text_leaderboard_command_handler
        self._audit_event_ingestor = audit_event_ingestor
        self._audit_delivery_runner = audit_delivery_runner
        self._audit_retention_runner = audit_retention_runner
        self._autorole_handler = autorole_handler
        self._game_presence_event_handler = game_presence_event_handler
        self._game_startup_reconciler = game_startup_reconciler
        self._game_checkpointer = game_checkpointer
        self._operational_health_runner = operational_health_runner
        self._voice_checkpoint_interval_seconds = voice_checkpoint_interval_seconds
        self._game_confirm_interval_seconds = game_confirm_interval_seconds
        self._clock = clock
        self._ready_lock = asyncio.Lock()
        self._startup_complete = asyncio.Event()
        self._startup_baseline_at: datetime | None = None
        self._recovery_generation = 0
        self._voice_checkpoint_task: asyncio.Task[None] | None = None
        self._game_tracking_ready = asyncio.Event()
        self._game_startup_baseline_at: datetime | None = None
        self._game_reconciled_generation: int | None = None
        self._game_checkpoint_task: asyncio.Task[None] | None = None
        self.tree = app_commands.CommandTree(self)
        self._command_guild = discord.Object(id=guild_id)
        self._commands_synced = False
        self._command_sync_lock = asyncio.Lock()
        self._rules_view_registered = False
        if voice_stats_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="stats",
                    description="Показать голосовую статистику участника",
                    callback=self._handle_stats_command,
                ),
                guild=self._command_guild,
            )
        if voice_leaderboard_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="top",
                    description=(
                        "Показать рейтинг участников по времени в голосовых каналах"
                    ),
                    callback=self._handle_leaderboard_command,
                ),
                guild=self._command_guild,
            )
        self.tree.add_command(
            app_commands.Command(
                name="help",
                description="Показать справку по командам Kanami",
                callback=self._handle_help_command,
            ),
            guild=self._command_guild,
        )
        if voice_channel_leaderboard_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="channels",
                    description="Показать рейтинг голосовых каналов",
                    callback=self._handle_channels_command,
                ),
                guild=self._command_guild,
            )
        if voice_channel_statistics_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="channelstats",
                    description="Показать статистику голосового канала",
                    callback=self._handle_channelstats_command,
                ),
                guild=self._command_guild,
            )
        if voice_together_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="together",
                    description=(
                        "Показать совместную голосовую статистику двух участников"
                    ),
                    callback=self._handle_together_command,
                ),
                guild=self._command_guild,
            )
        if voice_server_statistics_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="serverstats",
                    description="Показать общую голосовую статистику сервера",
                    callback=self._handle_serverstats_command,
                ),
                guild=self._command_guild,
            )
        if voice_activity_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="activity",
                    description="Показать, когда сервер наиболее активен",
                    callback=self._handle_activity_command,
                ),
                guild=self._command_guild,
            )
        if game_statistics_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="games",
                    description="Показать игровую активность участника",
                    callback=self._handle_games_command,
                ),
                guild=self._command_guild,
            )
        if text_leaderboard_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="topmessages",
                    description="Показать рейтинг участников по сообщениям",
                    callback=self._handle_text_leaderboard_command,
                ),
                guild=self._command_guild,
            )
        if achievements_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="achievements",
                    description="Показать достижения участника",
                    callback=self._handle_achievements_command,
                ),
                guild=self._command_guild,
            )
        if member_profile_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="profile",
                    description="Показать паспорт участника Kanami",
                    callback=self._handle_profile_command,
                ),
                guild=self._command_guild,
            )
        if member_anniversaries_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="anniversaries",
                    description="Показать ближайшие годовщины участников",
                    callback=self._handle_anniversaries_command,
                ),
                guild=self._command_guild,
            )
        if health_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="health",
                    description="Показать состояние Kanami",
                    callback=self._handle_health_command,
                ),
                guild=self._command_guild,
            )
        if rules_command_handler is not None:
            self.tree.add_command(
                app_commands.Command(
                    name="rules",
                    description="Показать текущие правила сервера",
                    callback=self._handle_rules_command,
                ),
                guild=self._command_guild,
            )
            self.tree.add_command(
                app_commands.Command(
                    name="rules-status",
                    description="Показать состояние принятия правил",
                    callback=self._handle_rules_status_command,
                ),
                guild=self._command_guild,
            )

    async def setup_hook(self) -> None:
        """Synchronize configured-guild commands once before Gateway events."""

        if self._rules_command_handler is not None and not self._rules_view_registered:
            self.add_view(self._rules_command_handler.create_persistent_view())
            self._rules_view_registered = True

        async with self._command_sync_lock:
            if self._commands_synced:
                return
            try:
                commands = await self.tree.sync(guild=self._command_guild)
            except Exception:
                logger.exception(
                    "Guild application command synchronization failed; "
                    "Gateway startup will continue guild_id=%s",
                    self._guild_id,
                )
                return
            self._commands_synced = True
            logger.info(
                "Guild application commands synchronized guild_id=%s commands=%s",
                self._guild_id,
                len(commands),
            )

    @app_commands.describe(
        user="Участник; если не указан — вы",
        period="Период; по умолчанию — 7 дней",
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="Сегодня", value="today"),
            app_commands.Choice(name="7 дней", value="7d"),
            app_commands.Choice(name="30 дней", value="30d"),
            app_commands.Choice(name="Всё время", value="all"),
        ]
    )
    async def _handle_stats_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        if self._voice_stats_command_handler is None:
            return
        await self._voice_stats_command_handler.handle(
            interaction,
            user,
            period.value if period is not None else None,
        )

    async def _handle_help_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=build_kanami_help_embed(),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.guild_only()
    async def _handle_rules_command(self, interaction: discord.Interaction) -> None:
        if self._rules_command_handler is None:
            return
        await self._rules_command_handler.show(interaction)

    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def _handle_rules_status_command(
        self, interaction: discord.Interaction
    ) -> None:
        if self._rules_command_handler is None:
            return
        await self._rules_command_handler.show_status(interaction)

    @app_commands.describe(user="Участник; если не указан — вы")
    @app_commands.guild_only()
    async def _handle_profile_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        if self._member_profile_command_handler is None:
            return
        await self._member_profile_command_handler.handle(interaction, user)

    async def _handle_anniversaries_command(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if self._member_anniversaries_command_handler is None:
            return
        await self._member_anniversaries_command_handler.handle(interaction)

    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def _handle_health_command(
        self,
        interaction: discord.Interaction,
    ) -> None:
        if self._health_command_handler is None:
            return
        await self._health_command_handler.handle(
            interaction,
            HealthRuntimeSnapshot(
                gateway_ready=self.is_ready(),
                gateway_latency_seconds=self.latency,
                registered_guild_command_count=len(
                    self.tree.get_commands(guild=self._command_guild)
                ),
                commands_synced=self._commands_synced,
                voice_startup_ready=self._startup_complete.is_set(),
            ),
        )

    @app_commands.describe(user="Участник; если не указан — вы")
    async def _handle_achievements_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ) -> None:
        if self._achievements_command_handler is None:
            return
        await self._achievements_command_handler.handle(interaction, user)

    @app_commands.describe(
        user1="Первый участник",
        user2="Второй участник",
    )
    async def _handle_together_command(
        self,
        interaction: discord.Interaction,
        user1: discord.Member,
        user2: discord.Member,
    ) -> None:
        if self._voice_together_command_handler is None:
            return
        await self._voice_together_command_handler.handle(interaction, user1, user2)

    @app_commands.choices(
        period=[
            app_commands.Choice(name="Сегодня", value="today"),
            app_commands.Choice(name="7 дней", value="7d"),
            app_commands.Choice(name="30 дней", value="30d"),
            app_commands.Choice(name="Всё время", value="all"),
        ]
    )
    async def _handle_serverstats_command(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        if self._voice_server_statistics_command_handler is None:
            return
        await self._voice_server_statistics_command_handler.handle(
            interaction,
            period.value if period is not None else None,
        )

    @app_commands.describe(period="Период; по умолчанию — 30 дней")
    @app_commands.choices(
        period=[
            app_commands.Choice(name="7 дней", value="7d"),
            app_commands.Choice(name="30 дней", value="30d"),
            app_commands.Choice(name="90 дней", value="90d"),
        ]
    )
    async def _handle_activity_command(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        if self._voice_activity_command_handler is None:
            return
        await self._voice_activity_command_handler.handle(
            interaction,
            period.value if period is not None else None,
        )

    @app_commands.describe(
        user="Участник; если не указан — вы",
        period="Период; по умолчанию — 30 дней",
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="7 дней", value="7d"),
            app_commands.Choice(name="30 дней", value="30d"),
            app_commands.Choice(name="90 дней", value="90d"),
            app_commands.Choice(name="Всё время", value="all"),
        ]
    )
    @app_commands.guild_only()
    async def _handle_games_command(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        if self._game_statistics_command_handler is None:
            return
        await self._game_statistics_command_handler.handle(
            interaction,
            user,
            period.value if period is not None else None,
        )

    @app_commands.choices(
        period=[
            app_commands.Choice(name="Сегодня", value="today"),
            app_commands.Choice(name="7 дней", value="7d"),
            app_commands.Choice(name="30 дней", value="30d"),
            app_commands.Choice(name="Всё время", value="all"),
        ]
    )
    async def _handle_leaderboard_command(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        if self._voice_leaderboard_command_handler is None:
            return
        await self._voice_leaderboard_command_handler.handle(
            interaction,
            period.value if period is not None else None,
        )

    @app_commands.choices(
        period=[
            app_commands.Choice(name="Сегодня", value="today"),
            app_commands.Choice(name="7 дней", value="7d"),
            app_commands.Choice(name="30 дней", value="30d"),
            app_commands.Choice(name="Всё время", value="all"),
        ]
    )
    async def _handle_text_leaderboard_command(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        if self._text_leaderboard_command_handler is None:
            return
        await self._text_leaderboard_command_handler.handle(
            interaction,
            period.value if period is not None else None,
        )

    @app_commands.choices(
        period=[
            app_commands.Choice(name="Сегодня", value="today"),
            app_commands.Choice(name="7 дней", value="7d"),
            app_commands.Choice(name="30 дней", value="30d"),
            app_commands.Choice(name="Всё время", value="all"),
        ]
    )
    async def _handle_channels_command(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        if self._voice_channel_leaderboard_command_handler is None:
            return
        await self._voice_channel_leaderboard_command_handler.handle(
            interaction,
            period.value if period is not None else None,
        )

    @app_commands.describe(channel="Голосовой или Stage-канал")
    @app_commands.choices(
        period=[
            app_commands.Choice(name="Сегодня", value="today"),
            app_commands.Choice(name="7 дней", value="7d"),
            app_commands.Choice(name="30 дней", value="30d"),
            app_commands.Choice(name="Всё время", value="all"),
        ]
    )
    async def _handle_channelstats_command(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | discord.StageChannel,
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        if self._voice_channel_statistics_command_handler is None:
            return
        await self._voice_channel_statistics_command_handler.handle(
            interaction,
            channel,
            period.value if period is not None else None,
        )

    async def on_ready(self) -> None:
        """Recover voice persistence after a full Gateway READY."""

        self._start_audit_runners()
        await self._recover_voice_state("ready")
        await self._sync_rules_publication("ready")
        self._start_operational_health_runner()

    async def on_resumed(self) -> None:
        """Recover voice persistence after a successful Gateway RESUME."""

        self._start_audit_runners()
        await self._recover_voice_state("resumed")
        await self._sync_rules_publication("resumed")
        self._start_operational_health_runner()

    async def _sync_rules_publication(self, trigger: str) -> None:
        if self._rules_publication_syncer is None:
            return
        try:
            await self._rules_publication_syncer.sync()
        except Exception:
            logger.exception(
                "Rules publication reconciliation failed; Gateway runtime will "
                "continue guild_id=%s trigger=%s",
                self._guild_id,
                trigger,
            )

    def _start_operational_health_runner(self) -> None:
        if self._operational_health_runner is None:
            return
        if not self._startup_complete.is_set():
            return
        if (
            self._game_startup_reconciler is not None
            and not self._game_tracking_ready.is_set()
        ):
            return
        self._operational_health_runner.start(self)

    def _start_audit_runners(self) -> None:
        if self._audit_delivery_runner is not None:
            self._audit_delivery_runner.start(self)
        if self._audit_retention_runner is not None:
            self._audit_retention_runner.start()
        if self._member_anniversary_check_runner is not None:
            self._member_anniversary_check_runner.start(self)

    async def _recover_voice_state(self, trigger: str) -> None:
        """Run the serialized provisioning and reconciliation sequence."""

        async with self._ready_lock:
            self._startup_complete.clear()
            self._startup_baseline_at = None
            recovery_generation = self._recovery_generation
            await self._stop_voice_checkpoint_loop()
            game_recovery_needed = (
                self._game_startup_reconciler is not None
                and self._game_reconciled_generation != recovery_generation
            )
            if game_recovery_needed:
                self._game_tracking_ready.clear()
                self._game_startup_baseline_at = None
                await self._stop_game_checkpoint_loop()
            guild = self.get_guild(self._guild_id)
            if guild is None:
                logger.error(
                    "Configured guild is unavailable during Gateway recovery "
                    "guild_id=%s trigger=%s",
                    self._guild_id,
                    trigger,
                )
                return

            try:
                provisioning_summary = (
                    await self._reference_provisioner.provision_guild(guild)
                )
            except Exception:
                logger.exception(
                    "Discord reference provisioning failed; skipping voice startup "
                    "reconciliation guild_id=%s trigger=%s",
                    guild.id,
                    trigger,
                )
                return

            logger.info(
                "Discord reference provisioning completed guild_id=%s users=%s "
                "members=%s voice_channels=%s trigger=%s",
                guild.id,
                provisioning_summary.user_count,
                provisioning_summary.member_count,
                provisioning_summary.voice_channel_count,
                trigger,
            )
            reconciled_at = normalize_observed_at(self._clock())
            try:
                summary = await self._voice_reconciler.reconcile_guild(
                    guild,
                    reconciled_at,
                )
            except Exception:
                logger.exception(
                    "Voice startup reconciliation failed; live tracking remains "
                    "paused guild_id=%s trigger=%s",
                    guild.id,
                    trigger,
                )
                summary = None
            if summary is not None:
                logger.info(
                    "Voice startup reconciliation completed guild_id=%s connected=%s "
                    "disconnected=%s joined=%s moved=%s left=%s unchanged=%s stale=%s "
                    "failed=%s trigger=%s",
                    guild.id,
                    summary.connected_count,
                    summary.disconnected_count,
                    summary.outcomes.get(VoiceTransitionResult.JOINED, 0),
                    summary.outcomes.get(VoiceTransitionResult.MOVED, 0),
                    summary.outcomes.get(VoiceTransitionResult.LEFT, 0),
                    summary.outcomes.get(VoiceTransitionResult.UNCHANGED, 0),
                    summary.outcomes.get(VoiceTransitionResult.IGNORED_STALE, 0),
                    summary.failed_count,
                    trigger,
                )
                if summary.failed_count:
                    logger.error(
                        "Voice startup reconciliation incomplete; live tracking "
                        "remains paused guild_id=%s failed=%s trigger=%s",
                        guild.id,
                        summary.failed_count,
                        trigger,
                    )
            if recovery_generation != self._recovery_generation:
                logger.warning(
                    "Discarding Gateway recovery completed after a newer disconnect "
                    "guild_id=%s trigger=%s",
                    guild.id,
                    trigger,
                )
                return
            if summary is not None and not summary.failed_count:
                self._startup_baseline_at = normalize_observed_at(summary.reconciled_at)
                self._startup_complete.set()
                self._start_voice_checkpoint_loop()
            if game_recovery_needed:
                game_reconciled_at = normalize_observed_at(self._clock())
                try:
                    game_summary = await self._game_startup_reconciler.reconcile_guild(
                        guild, game_reconciled_at
                    )
                except Exception:
                    logger.exception(
                        "Game startup reconciliation failed; live tracking remains "
                        "paused guild_id=%s trigger=%s",
                        guild.id,
                        trigger,
                    )
                    return
                if recovery_generation != self._recovery_generation:
                    logger.warning(
                        "Discarding game recovery completed after a newer disconnect "
                        "guild_id=%s trigger=%s",
                        guild.id,
                        trigger,
                    )
                    return
                self._game_reconciled_generation = recovery_generation
                self._game_startup_baseline_at = game_summary.reconciled_at
                self._game_tracking_ready.set()
                self._start_game_checkpoint_loop()
                logger.info(
                    "Game startup reconciliation completed guild_id=%s observed=%s "
                    "closed=%s started=%s unchanged=%s trigger=%s",
                    guild.id,
                    game_summary.observed_count,
                    game_summary.closed_count,
                    game_summary.started_count,
                    game_summary.unchanged_count,
                    trigger,
                )

    async def on_disconnect(self) -> None:
        """Pause live persistence until the next startup reconciliation completes."""

        self._startup_complete.clear()
        self._startup_baseline_at = None
        self._recovery_generation += 1
        await self._stop_voice_checkpoint_loop()
        self._game_tracking_ready.clear()
        self._game_startup_baseline_at = None
        await self._stop_game_checkpoint_loop()

    def _start_voice_checkpoint_loop(self) -> None:
        if self._voice_checkpointer is None:
            return
        if (
            self._voice_checkpoint_task is not None
            and not self._voice_checkpoint_task.done()
        ):
            return
        self._voice_checkpoint_task = asyncio.create_task(
            self._voice_checkpoint_loop(),
            name="voice-checkpoint-loop",
        )

    async def _stop_voice_checkpoint_loop(self) -> None:
        task = self._voice_checkpoint_task
        self._voice_checkpoint_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _voice_checkpoint_loop(self) -> None:
        while True:
            await asyncio.sleep(self._voice_checkpoint_interval_seconds)
            if not self._startup_complete.is_set():
                continue
            generation = self._recovery_generation
            guild = self.get_guild(self._guild_id)
            if guild is None:
                logger.warning(
                    "Skipping voice checkpoint because configured guild is unavailable "
                    "guild_id=%s",
                    self._guild_id,
                )
                continue
            checkpointed_at = normalize_observed_at(self._clock())
            if (
                generation != self._recovery_generation
                or not self._startup_complete.is_set()
            ):
                continue
            try:
                summary = await self._voice_checkpointer.checkpoint_guild(
                    guild,
                    checkpointed_at,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Voice checkpoint cycle failed guild_id=%s",
                    guild.id,
                )
                continue
            logger.debug(
                "Voice checkpoint completed guild_id=%s connected=%s unchanged=%s "
                "joined=%s moved=%s stale=%s failed=%s",
                guild.id,
                summary.connected_count,
                summary.outcomes.get(VoiceTransitionResult.UNCHANGED, 0),
                summary.outcomes.get(VoiceTransitionResult.JOINED, 0),
                summary.outcomes.get(VoiceTransitionResult.MOVED, 0),
                summary.outcomes.get(VoiceTransitionResult.IGNORED_STALE, 0),
                summary.failed_count,
            )

    def _start_game_checkpoint_loop(self) -> None:
        if self._game_checkpointer is None:
            return
        if (
            self._game_checkpoint_task is not None
            and not self._game_checkpoint_task.done()
        ):
            return
        self._game_checkpoint_task = asyncio.create_task(
            self._game_checkpoint_loop(),
            name="game-checkpoint-loop",
        )

    async def _stop_game_checkpoint_loop(self) -> None:
        task = self._game_checkpoint_task
        self._game_checkpoint_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _game_checkpoint_loop(self) -> None:
        while True:
            await asyncio.sleep(self._game_confirm_interval_seconds)
            if not self._game_tracking_ready.is_set():
                continue
            await self._checkpoint_games("periodic")

    async def _checkpoint_games(self, trigger: str) -> None:
        if self._game_checkpointer is None:
            return
        guild = self.get_guild(self._guild_id)
        if guild is None:
            logger.warning(
                "Skipping game checkpoint because configured guild is unavailable "
                "guild_id=%s trigger=%s",
                self._guild_id,
                trigger,
            )
            return
        try:
            summary = await self._game_checkpointer.checkpoint_guild(
                guild, normalize_observed_at(self._clock())
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Game checkpoint cycle failed guild_id=%s trigger=%s",
                guild.id,
                trigger,
            )
            return
        logger.debug(
            "Game checkpoint completed guild_id=%s observed=%s confirmed=%s trigger=%s",
            guild.id,
            summary.observed_count,
            summary.confirmed_count,
            trigger,
        )

    async def close(self) -> None:
        """Cancel all owned background tasks before closing the Gateway client."""

        self._startup_complete.clear()
        self._startup_baseline_at = None
        await self._stop_voice_checkpoint_loop()
        game_was_ready = self._game_tracking_ready.is_set()
        await self._stop_game_checkpoint_loop()
        if game_was_ready:
            await self._checkpoint_games("shutdown")
        self._game_tracking_ready.clear()
        self._game_startup_baseline_at = None
        if self._audit_delivery_runner is not None:
            await self._audit_delivery_runner.stop()
        if self._audit_retention_runner is not None:
            await self._audit_retention_runner.stop()
        if self._member_anniversary_check_runner is not None:
            await self._member_anniversary_check_runner.stop()
        if self._operational_health_runner is not None:
            await self._operational_health_runner.stop()
        await super().close()

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Forward one Gateway voice event after startup recovery is complete."""

        if member.guild.id != self._guild_id or member.bot:
            return
        observed_at = normalize_observed_at(self._clock())
        await self._startup_complete.wait()
        baseline_at = self._startup_baseline_at
        if baseline_at is None:
            logger.warning(
                "Skipping live voice-state event without startup baseline "
                "guild_id=%s user_id=%s",
                member.guild.id,
                member.id,
            )
            return
        if observed_at <= baseline_at:
            logger.debug(
                "Skipping live voice-state event covered by startup baseline "
                "guild_id=%s user_id=%s",
                member.guild.id,
                member.id,
            )
            return
        transition_result: VoiceTransitionResult | None = None
        try:
            transition_result = await self._voice_event_handler.handle(
                member, after, observed_at
            )
        finally:
            if self._audit_event_ingestor is not None:
                try:
                    await self._audit_event_ingestor.voice_changed(
                        member,
                        before,
                        after,
                        observed_at,
                        transition_result=transition_result,
                    )
                except Exception:
                    logger.exception(
                        "Audit voice ingestion failed guild_id=%s subject_id=%s",
                        member.guild.id,
                        member.id,
                    )

    async def on_message(self, message: discord.Message) -> None:
        """Forward MESSAGE_CREATE metadata to isolated text persistence."""

        if self._text_activity_event_handler is None:
            return
        await self._text_activity_event_handler.handle(message)

    async def on_presence_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        """Forward one Presence snapshot after Game Tracking recovery."""

        del before
        if self._game_presence_event_handler is None:
            return
        if after.guild.id != self._guild_id or after.bot:
            return
        observed_at = normalize_observed_at(self._clock())
        await self._game_tracking_ready.wait()
        baseline_at = self._game_startup_baseline_at
        if baseline_at is None or observed_at <= baseline_at:
            return
        await self._game_presence_event_handler.handle(after, observed_at)

    async def _sync_member_reference(
        self,
        member: discord.Member,
        *,
        identity_user: discord.User | discord.Member | None = None,
        left_at: datetime | None = None,
    ) -> None:
        if member.guild.id != self._guild_id:
            return
        try:
            if left_at is None:
                await self._reference_provisioner.provision_member(
                    member,
                    identity_user=identity_user,
                )
            else:
                await self._reference_provisioner.mark_member_left(member, left_at)
        except Exception:
            logger.exception(
                "Discord member reference sync failed guild_id=%s user_id=%s "
                "departed=%s",
                member.guild.id,
                member.id,
                left_at is not None,
            )

    async def on_member_join(self, member: discord.Member) -> None:
        await self._sync_member_reference(member)
        await self._run_audit(
            "member.joined",
            member.guild.id,
            member.id,
            lambda: self._audit_event_ingestor.member_joined(member, self._clock()),
        )
        if self._member_return_event_handler is not None:
            try:
                await self._member_return_event_handler.handle(member)
            except Exception:
                logger.exception(
                    "Member return handling failed guild_id=%s user_id=%s",
                    member.guild.id,
                    member.id,
                )
        if self._autorole_handler is not None:
            try:
                await self._autorole_handler.handle(member)
            except Exception:
                logger.exception(
                    "Autorole handler failed guild_id=%s user_id=%s role_id=%s",
                    member.guild.id,
                    member.id,
                    getattr(self._autorole_handler, "role_id", None),
                )

    async def on_member_remove(self, member: discord.Member) -> None:
        occurred_at = normalize_observed_at(self._clock())
        await self._sync_member_reference(member, left_at=occurred_at)
        await self._run_audit(
            "member.left",
            member.guild.id,
            member.id,
            lambda: self._audit_event_ingestor.member_left(member, occurred_at),
        )
        if (
            self._game_presence_event_handler is not None
            and self._game_tracking_ready.is_set()
        ):
            await self._game_presence_event_handler.close_member(member, occurred_at)

    async def on_member_update(
        self, before: discord.Member, after: discord.Member
    ) -> None:
        await self._sync_member_reference(after)
        await self._run_audit(
            "member.updated",
            after.guild.id,
            after.id,
            lambda: self._audit_event_ingestor.member_updated(
                before, after, self._clock()
            ),
        )

    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        guild = self.get_guild(self._guild_id)
        member = guild.get_member(after.id) if guild is not None else None
        if guild is None or member is None:
            return
        await self._sync_member_reference(member, identity_user=after)
        await self._run_audit(
            "user.updated",
            guild.id,
            after.id,
            lambda: self._audit_event_ingestor.user_updated(
                guild, member, before, after, self._clock()
            ),
        )

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        await self._run_audit(
            "channel.created",
            channel.guild.id,
            channel.id,
            lambda: self._audit_event_ingestor.channel_created(channel, self._clock()),
        )

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        await self._run_audit(
            "channel.deleted",
            channel.guild.id,
            channel.id,
            lambda: self._audit_event_ingestor.channel_deleted(channel, self._clock()),
        )

    async def on_guild_channel_update(
        self,
        before: discord.abc.GuildChannel,
        after: discord.abc.GuildChannel,
    ) -> None:
        await self._run_audit(
            "channel.updated",
            after.guild.id,
            after.id,
            lambda: self._audit_event_ingestor.channel_updated(
                before, after, self._clock()
            ),
        )

    async def on_guild_role_create(self, role: discord.Role) -> None:
        await self._run_audit(
            "role.created",
            role.guild.id,
            role.id,
            lambda: self._audit_event_ingestor.role_created(role, self._clock()),
        )

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        await self._run_audit(
            "role.deleted",
            role.guild.id,
            role.id,
            lambda: self._audit_event_ingestor.role_deleted(role, self._clock()),
        )

    async def on_guild_role_update(
        self, before: discord.Role, after: discord.Role
    ) -> None:
        await self._run_audit(
            "role.updated",
            after.guild.id,
            after.id,
            lambda: self._audit_event_ingestor.role_updated(
                before, after, self._clock()
            ),
        )

    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        await self._run_audit(
            "moderation.banned",
            guild.id,
            user.id,
            lambda: self._audit_event_ingestor.moderation_changed(
                "moderation.banned", guild, user, self._clock()
            ),
        )

    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        await self._run_audit(
            "moderation.unbanned",
            guild.id,
            user.id,
            lambda: self._audit_event_ingestor.moderation_changed(
                "moderation.unbanned", guild, user, self._clock()
            ),
        )

    async def _run_audit(
        self,
        event_type: str,
        guild_id: int,
        subject_id: int | None,
        operation: Callable[[], object],
    ) -> None:
        if self._audit_event_ingestor is None or guild_id != self._guild_id:
            return
        try:
            await operation()  # type: ignore[misc]
        except Exception:
            logger.exception(
                "Audit ingestion failed event_type=%s guild_id=%s subject_id=%s",
                event_type,
                guild_id,
                subject_id,
            )
