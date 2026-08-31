"""Idempotent Discord publication of the current Rules v1 ruleset."""

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

import discord
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from discord_stats_bot.discord.rules import RulesCommandHandler, build_rules_embed
from discord_stats_bot.discord.server_settings_control import ServerSettingControlError
from discord_stats_bot.discord.server_settings_options import (
    DiscordServerSettingsOptionsService,
)
from discord_stats_bot.features.rules import (
    RulesPublicationConfigurationResult,
    RulesPublicationConfigurationStatus,
    RulesPublicationState,
    RulesPublicationSyncResult,
    RulesPublicationSyncStatus,
    RulesRepository,
    RulesService,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyRulesPublicationRepository,
    SqlAlchemyRulesRepository,
)

logger = logging.getLogger(__name__)


class RulesPublicationRepository(Protocol):
    async def get(self, guild_id: int) -> RulesPublicationState: ...

    async def save_delivery(
        self, *, guild_id: int, message_id: int, ruleset_id: int
    ) -> None: ...

    async def save_configuration(
        self, *, guild_id: int, channel_id: int | None
    ) -> None: ...


RulesRepositoryFactory = Callable[[AsyncSession], RulesRepository]
PublicationRepositoryFactory = Callable[[AsyncSession], RulesPublicationRepository]


class RulesPublicationService:
    """Reconcile one durable publication cursor with one Discord message."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        rules_handler: RulesCommandHandler,
        *,
        guild_id: int,
        rules_repository_factory: RulesRepositoryFactory = SqlAlchemyRulesRepository,
        publication_repository_factory: PublicationRepositoryFactory = (
            SqlAlchemyRulesPublicationRepository
        ),
    ) -> None:
        self._session_factory = session_factory
        self._rules_handler = rules_handler
        self._guild_id = guild_id
        self._rules_repository_factory = rules_repository_factory
        self._publication_repository_factory = publication_repository_factory
        self._client: discord.Client | None = None
        self._lock = asyncio.Lock()

    def bind_client(self, client: discord.Client) -> None:
        if self._client is not None and self._client is not client:
            raise RuntimeError("rules publication service is already bound")
        self._client = client

    async def sync(self) -> RulesPublicationSyncResult:
        async with self._lock:
            return await self._sync_locked()

    async def configure(
        self, channel_id: int, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult:
        if channel_id <= 0 or actor_discord_user_id <= 0:
            raise ValueError("channel and actor IDs must be positive")
        async with self._lock:
            state = await self._load_state()
            client = self._client
            if client is None:
                return self._configuration_result(
                    RulesPublicationConfigurationStatus.RUNTIME_UNAVAILABLE,
                    state,
                    state.channel_id,
                )
            try:
                options = await DiscordServerSettingsOptionsService(
                    client, guild_id=self._guild_id
                ).get_options()
            except ServerSettingControlError:
                return self._configuration_result(
                    RulesPublicationConfigurationStatus.RUNTIME_UNAVAILABLE,
                    state,
                    state.channel_id,
                )
            if channel_id not in {option.id for option in options.channels}:
                return self._configuration_result(
                    RulesPublicationConfigurationStatus.INVALID_CHANNEL,
                    state,
                    state.channel_id,
                )
            if state.channel_id == channel_id:
                return self._configuration_result(
                    RulesPublicationConfigurationStatus.ALREADY_CONFIGURED,
                    state,
                    channel_id,
                )
            cleanup_failure = await self._cleanup_previous_delivery(state)
            if cleanup_failure is not None:
                return self._configuration_result(
                    cleanup_failure, state, state.channel_id
                )
            await self._save_configuration(channel_id)
            status = (
                RulesPublicationConfigurationStatus.CONFIGURED
                if state.channel_id is None
                else RulesPublicationConfigurationStatus.CHANGED
            )
            result = self._configuration_result(status, state, channel_id)
            self._log_configuration(result, actor_discord_user_id)
            return result

    async def disable(
        self, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult:
        if actor_discord_user_id <= 0:
            raise ValueError("actor ID must be positive")
        async with self._lock:
            state = await self._load_state()
            if state.channel_id is None:
                return self._configuration_result(
                    RulesPublicationConfigurationStatus.ALREADY_DISABLED,
                    state,
                    None,
                )
            cleanup_failure = await self._cleanup_previous_delivery(state)
            if cleanup_failure is not None:
                return self._configuration_result(
                    cleanup_failure, state, state.channel_id
                )
            await self._save_configuration(None)
            result = self._configuration_result(
                RulesPublicationConfigurationStatus.DISABLED, state, None
            )
            self._log_configuration(result, actor_discord_user_id)
            return result

    async def _load_state(self) -> RulesPublicationState:
        async with self._session_factory() as session:
            return await self._publication_repository_factory(session).get(
                self._guild_id
            )

    async def _save_configuration(self, channel_id: int | None) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await self._publication_repository_factory(session).save_configuration(
                    guild_id=self._guild_id,
                    channel_id=channel_id,
                )

    async def _cleanup_previous_delivery(
        self, state: RulesPublicationState
    ) -> RulesPublicationConfigurationStatus | None:
        if state.channel_id is None or state.message_id is None:
            return None
        client = self._client
        if client is None:
            return RulesPublicationConfigurationStatus.RUNTIME_UNAVAILABLE
        try:
            channel = client.get_channel(state.channel_id)
            if channel is None:
                channel = await client.fetch_channel(state.channel_id)
            fetch_message = getattr(channel, "fetch_message", None)
            if not callable(fetch_message):
                return RulesPublicationConfigurationStatus.CLEANUP_DISCORD_API_FAILURE
            message = await fetch_message(state.message_id)
            await message.delete()
        except discord.NotFound:
            return None
        except discord.Forbidden:
            return RulesPublicationConfigurationStatus.CLEANUP_FORBIDDEN
        except discord.HTTPException:
            return RulesPublicationConfigurationStatus.CLEANUP_DISCORD_API_FAILURE
        return None

    async def _sync_locked(self) -> RulesPublicationSyncResult:
        async with self._session_factory() as session:
            state = await self._publication_repository_factory(session).get(
                self._guild_id
            )
            ruleset = await RulesService(
                self._rules_repository_factory(session)
            ).get_current_published(self._guild_id)

        if state.channel_id is None:
            return self._result(RulesPublicationSyncStatus.NOT_CONFIGURED, state)
        if ruleset is None:
            return self._result(RulesPublicationSyncStatus.NO_PUBLISHED_RULESET, state)
        client = self._client
        if client is None:
            return self._failure(
                RulesPublicationSyncStatus.CHANNEL_UNAVAILABLE, state, ruleset.version
            )
        try:
            channel = client.get_channel(state.channel_id)
            if channel is None:
                channel = await client.fetch_channel(state.channel_id)
        except discord.Forbidden:
            return self._failure(
                RulesPublicationSyncStatus.FORBIDDEN, state, ruleset.version
            )
        except discord.NotFound:
            return self._failure(
                RulesPublicationSyncStatus.CHANNEL_UNAVAILABLE, state, ruleset.version
            )
        except discord.HTTPException:
            return self._failure(
                RulesPublicationSyncStatus.DISCORD_API_FAILURE, state, ruleset.version
            )

        channel_guild = getattr(channel, "guild", None)
        if (
            getattr(channel_guild, "id", None) != self._guild_id
            or not callable(getattr(channel, "send", None))
            or not callable(getattr(channel, "fetch_message", None))
        ):
            return self._failure(
                RulesPublicationSyncStatus.UNSUPPORTED_CHANNEL, state, ruleset.version
            )

        embed = build_rules_embed(ruleset)
        view = self._rules_handler.create_persistent_view()
        message = None
        recreate = False
        if state.message_id is not None:
            try:
                message = await channel.fetch_message(state.message_id)
            except discord.NotFound:
                recreate = True
            except discord.Forbidden:
                return self._failure(
                    RulesPublicationSyncStatus.FORBIDDEN, state, ruleset.version
                )
            except discord.HTTPException:
                return self._failure(
                    RulesPublicationSyncStatus.DISCORD_API_FAILURE,
                    state,
                    ruleset.version,
                )

        if message is not None and state.ruleset_id == ruleset.id:
            result = self._result(
                RulesPublicationSyncStatus.ALREADY_CURRENT,
                state,
                version=ruleset.version,
            )
            self._log_result(result)
            return result

        try:
            if message is None:
                message = await channel.send(
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                status = (
                    RulesPublicationSyncStatus.RECREATED
                    if recreate
                    else RulesPublicationSyncStatus.CREATED
                )
            else:
                try:
                    await message.edit(
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    status = RulesPublicationSyncStatus.UPDATED
                except discord.NotFound:
                    message = await channel.send(
                        embed=embed,
                        view=view,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    status = RulesPublicationSyncStatus.RECREATED
        except discord.Forbidden:
            return self._failure(
                RulesPublicationSyncStatus.FORBIDDEN, state, ruleset.version
            )
        except discord.NotFound:
            return self._failure(
                RulesPublicationSyncStatus.CHANNEL_UNAVAILABLE, state, ruleset.version
            )
        except discord.HTTPException:
            return self._failure(
                RulesPublicationSyncStatus.DISCORD_API_FAILURE, state, ruleset.version
            )

        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await self._publication_repository_factory(session).save_delivery(
                        guild_id=self._guild_id,
                        message_id=message.id,
                        ruleset_id=ruleset.id,
                    )
        except Exception:
            if status in {
                RulesPublicationSyncStatus.CREATED,
                RulesPublicationSyncStatus.RECREATED,
            }:
                await self._cleanup_unpersisted_message(
                    message,
                    channel_id=state.channel_id,
                    ruleset_id=ruleset.id,
                )
            raise
        result = RulesPublicationSyncResult(
            status,
            self._guild_id,
            state.channel_id,
            message.id,
            ruleset.id,
            ruleset.version,
        )
        self._log_result(result)
        return result

    async def _cleanup_unpersisted_message(
        self,
        message: discord.Message,
        *,
        channel_id: int,
        ruleset_id: int,
    ) -> None:
        try:
            await message.delete()
        except discord.NotFound:
            logger.info(
                "rules_publication_persistence_compensation_already_absent "
                "guild_id=%s channel_id=%s message_id=%s ruleset_id=%s",
                self._guild_id,
                channel_id,
                message.id,
                ruleset_id,
            )
        except Exception as cleanup_error:
            logger.error(
                "rules_publication_persistence_compensation_failed guild_id=%s "
                "channel_id=%s message_id=%s ruleset_id=%s error_type=%s",
                self._guild_id,
                channel_id,
                message.id,
                ruleset_id,
                type(cleanup_error).__name__,
                exc_info=True,
            )
        else:
            logger.info(
                "rules_publication_persistence_compensation_succeeded guild_id=%s "
                "channel_id=%s message_id=%s ruleset_id=%s",
                self._guild_id,
                channel_id,
                message.id,
                ruleset_id,
            )

    def _failure(
        self,
        status: RulesPublicationSyncStatus,
        state: RulesPublicationState,
        version: str,
    ) -> RulesPublicationSyncResult:
        result = self._result(status, state, version=version)
        self._log_result(result, failure=True)
        return result

    def _configuration_result(
        self,
        status: RulesPublicationConfigurationStatus,
        previous: RulesPublicationState,
        channel_id: int | None,
    ) -> RulesPublicationConfigurationResult:
        return RulesPublicationConfigurationResult(
            status=status,
            guild_id=self._guild_id,
            previous_channel_id=previous.channel_id,
            channel_id=channel_id,
            previous_message_id=previous.message_id,
        )

    @staticmethod
    def _log_configuration(
        result: RulesPublicationConfigurationResult, actor_discord_user_id: int
    ) -> None:
        logger.info(
            "rules_publication_configuration actor_discord_user_id=%s guild_id=%s "
            "previous_channel_id=%s channel_id=%s previous_message_id=%s status=%s",
            actor_discord_user_id,
            result.guild_id,
            result.previous_channel_id,
            result.channel_id,
            result.previous_message_id,
            result.status.value,
        )

    def _result(
        self,
        status: RulesPublicationSyncStatus,
        state: RulesPublicationState,
        *,
        version: str | None = None,
    ) -> RulesPublicationSyncResult:
        return RulesPublicationSyncResult(
            status,
            self._guild_id,
            state.channel_id,
            state.message_id,
            state.ruleset_id,
            version,
        )

    @staticmethod
    def _log_result(
        result: RulesPublicationSyncResult, *, failure: bool = False
    ) -> None:
        log = logger.warning if failure else logger.info
        log(
            "rules_publication_sync guild_id=%s channel_id=%s message_id=%s "
            "result=%s ruleset_id=%s version=%s",
            result.guild_id,
            result.channel_id,
            result.message_id,
            result.status.value,
            result.ruleset_id,
            result.version,
        )
