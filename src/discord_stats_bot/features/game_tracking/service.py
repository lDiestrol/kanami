"""State transitions, crash-safe reconciliation and checkpoint selection."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from discord_stats_bot.features.game_tracking.selector import select_tracked_game
from discord_stats_bot.features.game_tracking.types import (
    GameActivitySnapshot,
    GameCheckpointResult,
    GameReconciliationResult,
    GameTransitionResult,
    ObservedGame,
    OpenGameSession,
)
from discord_stats_bot.features.voice.types import normalize_observed_at


class GameTrackingRepository(Protocol):
    async def lock_member(self, guild_id: int, user_id: int) -> bool: ...

    async def get_open_session(
        self, guild_id: int, user_id: int
    ) -> OpenGameSession | None: ...

    async def get_latest_confirmed_through_at(
        self, guild_id: int, user_id: int
    ) -> datetime | None: ...

    async def start_session(self, observed: ObservedGame) -> None: ...

    async def confirm_session(
        self,
        session: OpenGameSession,
        observed: ObservedGame,
    ) -> None: ...

    async def close_session(
        self, session: OpenGameSession, ended_at: datetime
    ) -> None: ...


class GameBatchRepository(Protocol):
    async def list_open_sessions(
        self, guild_id: int, *, for_update: bool = False
    ) -> tuple[OpenGameSession, ...]: ...

    async def close_sessions_at_confirmation(
        self, session_ids: Sequence[int]
    ) -> int: ...

    async def start_sessions(self, observations: Sequence[ObservedGame]) -> int: ...

    async def confirm_sessions(
        self, session_ids: Sequence[int], confirmed_through_at: datetime
    ) -> int: ...


class GuildMemberNotFoundError(LookupError):
    """Raised if tracking runs before reference provisioning."""


class GameTrackingService:
    """Apply one serialized Presence observation in the caller transaction."""

    def __init__(self, repository: GameTrackingRepository) -> None:
        self._repository = repository

    async def observe(
        self,
        guild_id: int,
        user_id: int,
        activities: Sequence[GameActivitySnapshot],
        observed_at: datetime,
    ) -> GameTransitionResult:
        observed_at = normalize_observed_at(observed_at)
        if not await self._repository.lock_member(guild_id, user_id):
            raise GuildMemberNotFoundError(
                f"guild member ({guild_id}, {user_id}) must be provisioned first"
            )
        current = await self._repository.get_open_session(guild_id, user_id)
        selected = select_tracked_game(
            activities,
            current_game_key=current.game_key if current is not None else None,
        )

        if current is None:
            latest = await self._repository.get_latest_confirmed_through_at(
                guild_id, user_id
            )
            if latest is not None and observed_at < latest:
                return GameTransitionResult.IGNORED_STALE
            if selected is None:
                return GameTransitionResult.UNCHANGED
            await self._repository.start_session(
                ObservedGame(guild_id, user_id, selected, observed_at)
            )
            return GameTransitionResult.STARTED

        if observed_at < current.confirmed_through_at:
            return GameTransitionResult.IGNORED_STALE
        if selected is None:
            await self._repository.close_session(current, observed_at)
            return GameTransitionResult.CLOSED

        observed = ObservedGame(guild_id, user_id, selected, observed_at)
        if selected.key == current.game_key:
            if (
                observed_at == current.confirmed_through_at
                and selected.name == current.game_name
            ):
                return GameTransitionResult.UNCHANGED
            await self._repository.confirm_session(current, observed)
            return GameTransitionResult.CONFIRMED

        await self._repository.close_session(current, observed_at)
        await self._repository.start_session(observed)
        return GameTransitionResult.SWITCHED


class GameReconciliationService:
    """Cut unconfirmed downtime and establish one fresh startup snapshot."""

    def __init__(self, repository: GameBatchRepository) -> None:
        self._repository = repository

    async def reconcile(
        self,
        guild_id: int,
        activities_by_user: Mapping[int, Sequence[GameActivitySnapshot]],
        reconciled_at: datetime,
    ) -> GameReconciliationResult:
        reconciled_at = normalize_observed_at(reconciled_at)
        open_sessions = await self._repository.list_open_sessions(
            guild_id, for_update=True
        )
        open_by_user = {session.user_id: session for session in open_sessions}
        selected_by_user = {
            user_id: selected
            for user_id, activities in activities_by_user.items()
            if (
                selected := select_tracked_game(
                    activities,
                    current_game_key=(
                        open_by_user[user_id].game_key
                        if user_id in open_by_user
                        else None
                    ),
                )
            )
            is not None
        }

        kept_user_ids: set[int] = set()
        close_ids: list[int] = []
        for session in open_sessions:
            selected = selected_by_user.get(session.user_id)
            if reconciled_at < session.confirmed_through_at:
                kept_user_ids.add(session.user_id)
            elif (
                reconciled_at == session.confirmed_through_at
                and selected is not None
                and selected.key == session.game_key
            ):
                kept_user_ids.add(session.user_id)
            else:
                close_ids.append(session.session_id)

        closed_count = await self._repository.close_sessions_at_confirmation(close_ids)
        observations = tuple(
            ObservedGame(guild_id, user_id, game, reconciled_at)
            for user_id, game in sorted(selected_by_user.items())
            if user_id not in kept_user_ids
        )
        started_count = await self._repository.start_sessions(observations)
        return GameReconciliationResult(
            reconciled_at,
            len(selected_by_user),
            closed_count,
            started_count,
            len(kept_user_ids),
        )


class GameCheckpointService:
    """Confirm matching open games with one set-based persistence update."""

    def __init__(self, repository: GameBatchRepository) -> None:
        self._repository = repository

    async def checkpoint(
        self,
        guild_id: int,
        activities_by_user: Mapping[int, Sequence[GameActivitySnapshot]],
        checkpointed_at: datetime,
    ) -> GameCheckpointResult:
        checkpointed_at = normalize_observed_at(checkpointed_at)
        open_sessions = await self._repository.list_open_sessions(guild_id)
        matching_ids = tuple(
            session.session_id
            for session in open_sessions
            if (
                selected := select_tracked_game(
                    activities_by_user.get(session.user_id, ()),
                    current_game_key=session.game_key,
                )
            )
            is not None
            and selected.key == session.game_key
        )
        confirmed_count = await self._repository.confirm_sessions(
            matching_ids, checkpointed_at
        )
        return GameCheckpointResult(
            checkpointed_at,
            len(activities_by_user),
            confirmed_count,
        )
