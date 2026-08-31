"""Application orchestration for live voice transitions and reconciliation."""

from datetime import datetime
from typing import Protocol

from discord_stats_bot.features.voice.types import (
    ObservedVoiceState,
    OpenVoiceState,
    VoiceTransitionResult,
    normalize_observed_at,
)

EXACT_QUALITY = "exact"
ESTIMATED_QUALITY = "estimated"


class GuildMemberNotFoundError(LookupError):
    """Raised when voice tracking is requested before member provisioning."""


class VoiceTransitionRepository(Protocol):
    """Persistence operations required by voice tracking and reconciliation."""

    async def lock_member(self, guild_id: int, user_id: int) -> bool: ...

    async def get_open_state(
        self,
        guild_id: int,
        user_id: int,
    ) -> OpenVoiceState | None: ...

    async def get_latest_confirmed_through_at(
        self,
        guild_id: int,
        user_id: int,
    ) -> datetime | None: ...

    async def create_open_state(
        self,
        observed: ObservedVoiceState,
        *,
        quality: str,
    ) -> None: ...

    async def advance_confirmation(
        self,
        state: OpenVoiceState,
        observed_at: datetime,
    ) -> None: ...

    async def move_open_interval(
        self,
        state: OpenVoiceState,
        observed: ObservedVoiceState,
        *,
        quality: str,
    ) -> None: ...

    async def reconcile_same_snapshot(
        self,
        state: OpenVoiceState,
        observed: ObservedVoiceState,
        *,
        exact_quality: str,
        estimated_quality: str,
    ) -> None: ...

    async def close_open_state(
        self,
        state: OpenVoiceState,
        observed_at: datetime,
    ) -> None: ...


class VoiceTrackingService:
    """Apply serialized live observations inside the caller's transaction."""

    def __init__(self, repository: VoiceTransitionRepository) -> None:
        self._repository = repository

    async def observe_connected(
        self,
        observed: ObservedVoiceState,
    ) -> VoiceTransitionResult:
        """Apply a join, duplicate state, or channel snapshot transition."""

        await self._lock_existing_member(observed.guild_id, observed.user_id)
        current = await self._repository.get_open_state(
            observed.guild_id,
            observed.user_id,
        )

        if current is None:
            latest_confirmed = await self._repository.get_latest_confirmed_through_at(
                observed.guild_id,
                observed.user_id,
            )
            if latest_confirmed is not None and observed.observed_at < latest_confirmed:
                return VoiceTransitionResult.IGNORED_STALE
            await self._repository.create_open_state(
                observed,
                quality=EXACT_QUALITY,
            )
            return VoiceTransitionResult.JOINED

        if observed.observed_at < current.confirmed_through_at:
            return VoiceTransitionResult.IGNORED_STALE

        if current.has_same_channel_snapshot(observed):
            if observed.observed_at > current.confirmed_through_at:
                await self._repository.advance_confirmation(
                    current,
                    observed.observed_at,
                )
            return VoiceTransitionResult.UNCHANGED

        await self._repository.move_open_interval(
            current,
            observed,
            quality=EXACT_QUALITY,
        )
        return VoiceTransitionResult.MOVED

    async def observe_disconnected(
        self,
        guild_id: int,
        user_id: int,
        observed_at: datetime,
    ) -> VoiceTransitionResult:
        """Apply an idempotent leave/disconnect observation."""

        observed_at = normalize_observed_at(observed_at)
        await self._lock_existing_member(guild_id, user_id)
        current = await self._repository.get_open_state(guild_id, user_id)

        if current is None:
            latest_confirmed = await self._repository.get_latest_confirmed_through_at(
                guild_id,
                user_id,
            )
            if latest_confirmed is not None and observed_at < latest_confirmed:
                return VoiceTransitionResult.IGNORED_STALE
            return VoiceTransitionResult.UNCHANGED
        if observed_at < current.confirmed_through_at:
            return VoiceTransitionResult.IGNORED_STALE

        await self._repository.close_open_state(current, observed_at)
        return VoiceTransitionResult.LEFT

    async def reconcile_connected(
        self,
        observed: ObservedVoiceState,
    ) -> VoiceTransitionResult:
        """Reconcile one currently connected snapshot against durable state."""

        await self._lock_existing_member(observed.guild_id, observed.user_id)
        current = await self._repository.get_open_state(
            observed.guild_id,
            observed.user_id,
        )

        if current is None:
            latest_confirmed = await self._repository.get_latest_confirmed_through_at(
                observed.guild_id,
                observed.user_id,
            )
            if latest_confirmed is not None and observed.observed_at < latest_confirmed:
                return VoiceTransitionResult.IGNORED_STALE
            await self._repository.create_open_state(
                observed,
                quality=EXACT_QUALITY,
            )
            return VoiceTransitionResult.JOINED

        confirmed_through_at = current.confirmed_through_at
        if observed.observed_at < confirmed_through_at:
            return VoiceTransitionResult.IGNORED_STALE

        if current.has_same_channel_snapshot(observed):
            if observed.observed_at > confirmed_through_at:
                await self._repository.reconcile_same_snapshot(
                    current,
                    observed,
                    exact_quality=EXACT_QUALITY,
                    estimated_quality=ESTIMATED_QUALITY,
                )
            return VoiceTransitionResult.UNCHANGED

        await self._repository.close_open_state(current, confirmed_through_at)
        await self._repository.create_open_state(
            observed,
            quality=EXACT_QUALITY,
        )
        return VoiceTransitionResult.MOVED

    async def reconcile_disconnected(
        self,
        guild_id: int,
        user_id: int,
        reconciled_at: datetime,
    ) -> VoiceTransitionResult:
        """Reconcile a member absent from the current voice snapshot."""

        reconciled_at = normalize_observed_at(reconciled_at)
        await self._lock_existing_member(guild_id, user_id)
        current = await self._repository.get_open_state(guild_id, user_id)

        if current is None:
            latest_confirmed = await self._repository.get_latest_confirmed_through_at(
                guild_id,
                user_id,
            )
            if latest_confirmed is not None and reconciled_at < latest_confirmed:
                return VoiceTransitionResult.IGNORED_STALE
            return VoiceTransitionResult.UNCHANGED

        confirmed_through_at = current.confirmed_through_at
        if reconciled_at < confirmed_through_at:
            return VoiceTransitionResult.IGNORED_STALE

        await self._repository.close_open_state(current, confirmed_through_at)
        return VoiceTransitionResult.LEFT

    async def _lock_existing_member(self, guild_id: int, user_id: int) -> None:
        if not await self._repository.lock_member(guild_id, user_id):
            raise GuildMemberNotFoundError(
                f"guild member ({guild_id}, {user_id}) must be provisioned first"
            )
