from datetime import UTC, datetime, timedelta

import pytest

from discord_stats_bot.features.game_tracking import (
    GameActivitySnapshot,
    GameCheckpointService,
    GameReconciliationService,
    GameTrackingService,
    GameTransitionResult,
    ObservedGame,
    OpenGameSession,
    select_tracked_game,
)

GUILD_ID = 10
USER_ID = 20
NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def activity(
    name: str | None = "Minecraft",
    *,
    activity_type: str = "playing",
    application_id: int | None = None,
) -> GameActivitySnapshot:
    return GameActivitySnapshot(activity_type, name, application_id)


@pytest.mark.parametrize(
    "activity_type",
    ["listening", "streaming", "watching", "custom", "competing", "unknown"],
)
def test_selector_ignores_every_non_playing_activity(activity_type: str) -> None:
    assert select_tracked_game((activity(activity_type=activity_type),)) is None


@pytest.mark.parametrize("name", [None, "", "   \t"])
def test_selector_ignores_missing_or_blank_playing_name(name: str | None) -> None:
    assert select_tracked_game((activity(name),)) is None


def test_selector_returns_playing_with_application_identity() -> None:
    selected = select_tracked_game((activity(application_id=123),))

    assert selected is not None
    assert selected.key == "application:123"
    assert selected.name == "Minecraft"
    assert selected.application_id == 123


def test_selector_falls_back_to_normalized_name_identity() -> None:
    selected = select_tracked_game((activity("  MineCraft\tJava  "),))

    assert selected is not None
    assert selected.key == "name:minecraft java"
    assert selected.name == "MineCraft Java"
    assert selected.application_id is None


def test_selector_is_deterministic_across_reorder() -> None:
    minecraft = activity("Minecraft")
    valorant = activity("Valorant")

    first = select_tracked_game((valorant, minecraft))
    second = select_tracked_game((minecraft, valorant))

    assert first == second


def test_selector_keeps_current_game_when_multiple_playing_activities_reorder() -> None:
    minecraft = activity("Minecraft")
    valorant = activity("Valorant")

    selected = select_tracked_game(
        (minecraft, valorant), current_game_key="name:valorant"
    )
    reordered = select_tracked_game(
        (valorant, minecraft), current_game_key="name:valorant"
    )

    assert selected is not None
    assert selected.key == "name:valorant"
    assert reordered == selected


class MemoryRepository:
    def __init__(self, current: OpenGameSession | None = None) -> None:
        self.current = current
        self.latest = current.confirmed_through_at if current else None
        self.operations: list[tuple[str, object]] = []
        self.next_id = 100

    async def lock_member(self, guild_id: int, user_id: int) -> bool:
        self.operations.append(("lock", (guild_id, user_id)))
        return True

    async def get_open_session(
        self, guild_id: int, user_id: int
    ) -> OpenGameSession | None:
        return self.current

    async def get_latest_confirmed_through_at(
        self, guild_id: int, user_id: int
    ) -> datetime | None:
        return self.latest

    async def start_session(self, observed: ObservedGame) -> None:
        self.operations.append(("start", observed))
        self.current = OpenGameSession(
            self.next_id,
            observed.guild_id,
            observed.user_id,
            observed.game.key,
            observed.game.name,
            observed.game.application_id,
            observed.observed_at,
            observed.observed_at,
        )
        self.latest = observed.observed_at
        self.next_id += 1

    async def confirm_session(
        self, session: OpenGameSession, observed: ObservedGame
    ) -> None:
        self.operations.append(("confirm", observed))
        self.current = OpenGameSession(
            session.session_id,
            session.guild_id,
            session.user_id,
            session.game_key,
            observed.game.name,
            observed.game.application_id,
            session.started_at,
            observed.observed_at,
        )
        self.latest = observed.observed_at

    async def close_session(self, session: OpenGameSession, ended_at: datetime) -> None:
        self.operations.append(("close", ended_at))
        self.current = None
        self.latest = ended_at


def open_minecraft(
    *,
    confirmed_at: datetime = NOW,
    game_name: str = "Minecraft",
    application_id: int | None = None,
) -> OpenGameSession:
    key = (
        f"application:{application_id}"
        if application_id is not None
        else "name:minecraft"
    )
    return OpenGameSession(
        50,
        GUILD_ID,
        USER_ID,
        key,
        game_name,
        application_id,
        NOW - timedelta(hours=1),
        confirmed_at,
    )


@pytest.mark.asyncio
async def test_nothing_to_minecraft_starts_session() -> None:
    repository = MemoryRepository()

    result = await GameTrackingService(repository).observe(
        GUILD_ID, USER_ID, (activity(),), NOW
    )

    assert result is GameTransitionResult.STARTED
    assert repository.current is not None
    assert repository.current.game_key == "name:minecraft"


@pytest.mark.asyncio
async def test_same_game_confirms_without_new_session() -> None:
    repository = MemoryRepository(open_minecraft())

    result = await GameTrackingService(repository).observe(
        GUILD_ID, USER_ID, (activity(),), NOW + timedelta(minutes=1)
    )

    assert result is GameTransitionResult.CONFIRMED
    assert repository.current is not None
    assert repository.current.session_id == 50
    assert [name for name, _ in repository.operations].count("start") == 0


@pytest.mark.asyncio
async def test_rich_presence_detail_change_is_same_identity() -> None:
    repository = MemoryRepository(open_minecraft())
    service = GameTrackingService(repository)

    first = await service.observe(
        GUILD_ID, USER_ID, (activity(),), NOW + timedelta(seconds=1)
    )
    second = await service.observe(
        GUILD_ID, USER_ID, (activity(),), NOW + timedelta(seconds=2)
    )

    assert first is GameTransitionResult.CONFIRMED
    assert second is GameTransitionResult.CONFIRMED
    assert repository.current is not None
    assert repository.current.session_id == 50


@pytest.mark.asyncio
async def test_same_application_id_updates_cosmetic_name_without_switch() -> None:
    repository = MemoryRepository(
        open_minecraft(application_id=123, game_name="Minecraft Launcher")
    )

    result = await GameTrackingService(repository).observe(
        GUILD_ID,
        USER_ID,
        (activity("Minecraft: Java Edition", application_id=123),),
        NOW + timedelta(minutes=1),
    )

    assert result is GameTransitionResult.CONFIRMED
    assert repository.current is not None
    assert repository.current.session_id == 50
    assert repository.current.game_name == "Minecraft: Java Edition"


@pytest.mark.asyncio
async def test_minecraft_to_none_closes_session_and_duplicate_is_unchanged() -> None:
    repository = MemoryRepository(open_minecraft())
    service = GameTrackingService(repository)

    closed = await service.observe(GUILD_ID, USER_ID, (), NOW + timedelta(minutes=1))
    duplicate = await service.observe(GUILD_ID, USER_ID, (), NOW + timedelta(minutes=2))

    assert closed is GameTransitionResult.CLOSED
    assert duplicate is GameTransitionResult.UNCHANGED
    assert repository.current is None


@pytest.mark.asyncio
async def test_minecraft_to_valorant_closes_then_starts_atomically_ordered() -> None:
    repository = MemoryRepository(open_minecraft())

    result = await GameTrackingService(repository).observe(
        GUILD_ID,
        USER_ID,
        (activity("Valorant", application_id=456),),
        NOW + timedelta(minutes=1),
    )

    assert result is GameTransitionResult.SWITCHED
    assert [name for name, _ in repository.operations][-2:] == ["close", "start"]
    assert repository.current is not None
    assert repository.current.game_key == "application:456"


@pytest.mark.asyncio
async def test_duplicate_presence_event_does_not_write() -> None:
    repository = MemoryRepository(open_minecraft())

    result = await GameTrackingService(repository).observe(
        GUILD_ID, USER_ID, (activity(),), NOW
    )

    assert result is GameTransitionResult.UNCHANGED
    assert [name for name, _ in repository.operations] == ["lock"]


@pytest.mark.asyncio
async def test_fast_switches_leave_exactly_one_current_game() -> None:
    repository = MemoryRepository()
    service = GameTrackingService(repository)

    for offset, name in enumerate(("Minecraft", "Valorant", "Minecraft")):
        await service.observe(
            GUILD_ID,
            USER_ID,
            (activity(name),),
            NOW + timedelta(seconds=offset),
        )

    assert repository.current is not None
    assert repository.current.game_key == "name:minecraft"
    assert [name for name, _ in repository.operations].count("start") == 3
    assert [name for name, _ in repository.operations].count("close") == 2


class BatchRepository:
    def __init__(self, sessions: tuple[OpenGameSession, ...]) -> None:
        self.sessions = list(sessions)
        self.closed_ids: list[int] = []
        self.started: list[ObservedGame] = []
        self.confirmed_ids: list[int] = []
        self.for_update_values: list[bool] = []

    async def list_open_sessions(
        self, guild_id: int, *, for_update: bool = False
    ) -> tuple[OpenGameSession, ...]:
        self.for_update_values.append(for_update)
        return tuple(self.sessions)

    async def close_sessions_at_confirmation(self, session_ids: list[int]) -> int:
        self.closed_ids.extend(session_ids)
        self.sessions = [
            item for item in self.sessions if item.session_id not in session_ids
        ]
        return len(session_ids)

    async def start_sessions(self, observations: tuple[ObservedGame, ...]) -> int:
        self.started.extend(observations)
        return len(observations)

    async def confirm_sessions(
        self, session_ids: tuple[int, ...], confirmed_through_at: datetime
    ) -> int:
        self.confirmed_ids.extend(session_ids)
        return len(session_ids)


@pytest.mark.asyncio
async def test_crash_reconciliation_closes_stale_session_at_confirmation() -> None:
    repository = BatchRepository((open_minecraft(),))

    result = await GameReconciliationService(repository).reconcile(  # type: ignore[arg-type]
        GUILD_ID, {}, NOW + timedelta(hours=1)
    )

    assert result.closed_count == 1
    assert repository.closed_ids == [50]
    assert repository.started == []
    assert repository.for_update_values == [True]


@pytest.mark.asyncio
async def test_crash_same_game_closes_old_and_opens_fresh_from_startup() -> None:
    startup = NOW + timedelta(hours=1)
    repository = BatchRepository((open_minecraft(),))

    result = await GameReconciliationService(repository).reconcile(  # type: ignore[arg-type]
        GUILD_ID, {USER_ID: (activity(),)}, startup
    )

    assert result.closed_count == 1
    assert result.started_count == 1
    assert repository.started[0].observed_at == startup
    assert repository.started[0].game.key == "name:minecraft"


@pytest.mark.asyncio
async def test_crash_new_game_closes_old_and_opens_selected_game() -> None:
    repository = BatchRepository((open_minecraft(),))

    await GameReconciliationService(repository).reconcile(  # type: ignore[arg-type]
        GUILD_ID,
        {USER_ID: (activity("Valorant"),)},
        NOW + timedelta(hours=1),
    )

    assert repository.closed_ids == [50]
    assert repository.started[0].game.key == "name:valorant"


@pytest.mark.asyncio
async def test_repeated_same_timestamp_reconciliation_keeps_fresh_session() -> None:
    repository = BatchRepository((open_minecraft(confirmed_at=NOW),))

    result = await GameReconciliationService(repository).reconcile(  # type: ignore[arg-type]
        GUILD_ID, {USER_ID: (activity(),)}, NOW
    )

    assert result.unchanged_count == 1
    assert repository.closed_ids == []
    assert repository.started == []


@pytest.mark.asyncio
async def test_checkpoint_confirms_only_matching_open_game_in_one_batch() -> None:
    second = OpenGameSession(
        51,
        GUILD_ID,
        21,
        "name:valorant",
        "Valorant",
        None,
        NOW - timedelta(hours=1),
        NOW,
    )
    repository = BatchRepository((open_minecraft(), second))

    result = await GameCheckpointService(repository).checkpoint(  # type: ignore[arg-type]
        GUILD_ID,
        {USER_ID: (activity(),), 21: (activity("Another Game"),)},
        NOW + timedelta(minutes=1),
    )

    assert result.confirmed_count == 1
    assert repository.confirmed_ids == [50]
    assert repository.for_update_values == [False]
