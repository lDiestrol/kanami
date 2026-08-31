from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from discord_stats_bot.features.voice import (
    GuildMemberNotFoundError,
    ObservedVoiceState,
    OpenVoiceState,
    VoiceTrackingService,
    VoiceTransitionResult,
)

OBSERVED_AT = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


class FakeVoiceTransitionRepository:
    def __init__(
        self,
        current: OpenVoiceState | None = None,
        *,
        member_exists: bool = True,
        latest_confirmed_through_at: datetime | None = None,
    ) -> None:
        self.current = current
        self.member_exists = member_exists
        self.latest_confirmed_through_at = (
            latest_confirmed_through_at
            if latest_confirmed_through_at is not None
            else current.confirmed_through_at
            if current is not None
            else None
        )
        self.next_session_id = current.session_id + 1 if current is not None else 101
        self.next_interval_id = current.interval_id + 1 if current is not None else 201
        self.calls: list[tuple[object, ...]] = []
        self.qualities: list[str] = []
        self.reconciled_intervals: list[tuple[datetime, datetime, str]] = []

    async def lock_member(self, guild_id: int, user_id: int) -> bool:
        self.calls.append(("lock", guild_id, user_id))
        return self.member_exists

    async def get_open_state(
        self,
        guild_id: int,
        user_id: int,
    ) -> OpenVoiceState | None:
        self.calls.append(("get", guild_id, user_id))
        return self.current

    async def get_latest_confirmed_through_at(
        self,
        guild_id: int,
        user_id: int,
    ) -> datetime | None:
        self.calls.append(("get_latest", guild_id, user_id))
        return self.latest_confirmed_through_at

    async def create_open_state(
        self,
        observed: ObservedVoiceState,
        *,
        quality: str,
    ) -> None:
        self.calls.append(("create", observed))
        self.qualities.append(quality)
        self.current = OpenVoiceState(
            session_id=self.next_session_id,
            interval_id=self.next_interval_id,
            confirmed_through_at=observed.observed_at,
            channel_id=observed.channel_id,
            channel_kind=observed.channel_kind,
            is_afk=observed.is_afk,
        )
        self.latest_confirmed_through_at = observed.observed_at
        self.next_session_id += 1
        self.next_interval_id += 1

    async def advance_confirmation(
        self,
        state: OpenVoiceState,
        observed_at: datetime,
    ) -> None:
        self.calls.append(("advance", state.session_id, observed_at))
        self.current = replace(state, confirmed_through_at=observed_at)
        self.latest_confirmed_through_at = observed_at

    async def move_open_interval(
        self,
        state: OpenVoiceState,
        observed: ObservedVoiceState,
        *,
        quality: str,
    ) -> None:
        self.calls.append(("move", state.session_id, observed))
        self.qualities.append(quality)
        self.current = OpenVoiceState(
            session_id=state.session_id,
            interval_id=self.next_interval_id,
            confirmed_through_at=observed.observed_at,
            channel_id=observed.channel_id,
            channel_kind=observed.channel_kind,
            is_afk=observed.is_afk,
        )
        self.latest_confirmed_through_at = observed.observed_at
        self.next_interval_id += 1

    async def reconcile_same_snapshot(
        self,
        state: OpenVoiceState,
        observed: ObservedVoiceState,
        *,
        exact_quality: str,
        estimated_quality: str,
    ) -> None:
        self.calls.append(("reconcile_same", state.session_id, observed))
        self.qualities.extend((estimated_quality, exact_quality))
        self.reconciled_intervals.append(
            (state.confirmed_through_at, observed.observed_at, estimated_quality)
        )
        self.current = OpenVoiceState(
            session_id=state.session_id,
            interval_id=self.next_interval_id,
            confirmed_through_at=observed.observed_at,
            channel_id=observed.channel_id,
            channel_kind=observed.channel_kind,
            is_afk=observed.is_afk,
        )
        self.latest_confirmed_through_at = observed.observed_at
        self.next_interval_id += 2

    async def close_open_state(
        self,
        state: OpenVoiceState,
        observed_at: datetime,
    ) -> None:
        self.calls.append(("close", state.session_id, state.interval_id, observed_at))
        self.latest_confirmed_through_at = observed_at
        self.current = None


def observed_state(
    *,
    channel_id: int = 30,
    channel_kind: str = "voice",
    is_afk: bool = False,
    observed_at: datetime = OBSERVED_AT,
) -> ObservedVoiceState:
    return ObservedVoiceState(
        guild_id=10,
        user_id=20,
        channel_id=channel_id,
        channel_kind=channel_kind,
        is_afk=is_afk,
        observed_at=observed_at,
    )


def open_state(
    *,
    confirmed_through_at: datetime = OBSERVED_AT,
    channel_id: int = 30,
    channel_kind: str = "voice",
    is_afk: bool = False,
) -> OpenVoiceState:
    return OpenVoiceState(
        session_id=101,
        interval_id=201,
        confirmed_through_at=confirmed_through_at,
        channel_id=channel_id,
        channel_kind=channel_kind,
        is_afk=is_afk,
    )


@pytest.mark.asyncio
async def test_join_creates_one_exact_open_state() -> None:
    repository = FakeVoiceTransitionRepository()
    service = VoiceTrackingService(repository)
    observed = observed_state()

    result = await service.observe_connected(observed)

    assert result is VoiceTransitionResult.JOINED
    assert repository.calls == [
        ("lock", 10, 20),
        ("get", 10, 20),
        ("get_latest", 10, 20),
        ("create", observed),
    ]
    assert repository.qualities == ["exact"]
    assert repository.current is not None
    assert repository.current.confirmed_through_at == OBSERVED_AT


@pytest.mark.asyncio
async def test_duplicate_state_is_idempotent_and_advances_confirmation() -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)
    later = OBSERVED_AT + timedelta(seconds=15)

    result = await service.observe_connected(observed_state(observed_at=later))

    assert result is VoiceTransitionResult.UNCHANGED
    assert repository.calls[-1] == ("advance", 101, later)
    assert not any(call[0] in {"create", "move"} for call in repository.calls)


@pytest.mark.asyncio
async def test_duplicate_state_at_same_timestamp_is_no_op() -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)

    result = await service.observe_connected(observed_state())

    assert result is VoiceTransitionResult.UNCHANGED
    assert repository.calls == [("lock", 10, 20), ("get", 10, 20)]


@pytest.mark.parametrize(
    ("changes", "expected_channel_id", "expected_kind", "expected_is_afk"),
    [
        ({"channel_id": 31}, 31, "voice", False),
        ({"channel_kind": "stage"}, 30, "stage", False),
        ({"is_afk": True}, 30, "voice", True),
    ],
)
@pytest.mark.asyncio
async def test_channel_snapshot_change_moves_within_same_exact_session(
    changes: dict[str, object],
    expected_channel_id: int,
    expected_kind: str,
    expected_is_afk: bool,
) -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)
    later = OBSERVED_AT + timedelta(seconds=15)
    observed = observed_state(observed_at=later, **changes)

    result = await service.observe_connected(observed)

    assert result is VoiceTransitionResult.MOVED
    assert repository.calls[-1] == ("move", 101, observed)
    assert repository.qualities == ["exact"]
    assert repository.current is not None
    assert repository.current.session_id == 101
    assert repository.current.channel_id == expected_channel_id
    assert repository.current.channel_kind == expected_kind
    assert repository.current.is_afk is expected_is_afk


@pytest.mark.asyncio
async def test_move_at_confirmed_timestamp_is_allowed() -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)

    result = await service.observe_connected(observed_state(channel_id=31))

    assert result is VoiceTransitionResult.MOVED
    assert repository.current is not None
    assert repository.current.session_id == 101


@pytest.mark.asyncio
async def test_leave_closes_interval_and_session_at_same_timestamp() -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)
    later = OBSERVED_AT + timedelta(seconds=15)

    result = await service.observe_disconnected(10, 20, later)

    assert result is VoiceTransitionResult.LEFT
    assert repository.calls[-1] == ("close", 101, 201, later)
    assert repository.current is None


@pytest.mark.asyncio
async def test_duplicate_leave_is_idempotent() -> None:
    repository = FakeVoiceTransitionRepository()
    service = VoiceTrackingService(repository)

    result = await service.observe_disconnected(10, 20, OBSERVED_AT)

    assert result is VoiceTransitionResult.UNCHANGED
    assert repository.calls == [
        ("lock", 10, 20),
        ("get", 10, 20),
        ("get_latest", 10, 20),
    ]


@pytest.mark.parametrize("operation", ["connected", "disconnected"])
@pytest.mark.asyncio
async def test_stale_event_is_explicitly_ignored(operation: str) -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)
    stale = OBSERVED_AT - timedelta(microseconds=1)

    if operation == "connected":
        result = await service.observe_connected(
            observed_state(channel_id=31, observed_at=stale)
        )
    else:
        result = await service.observe_disconnected(10, 20, stale)

    assert result is VoiceTransitionResult.IGNORED_STALE
    assert repository.calls == [("lock", 10, 20), ("get", 10, 20)]
    assert repository.current == open_state()


@pytest.mark.asyncio
async def test_existing_member_is_required_and_not_provisioned_by_service() -> None:
    repository = FakeVoiceTransitionRepository(member_exists=False)
    service = VoiceTrackingService(repository)

    with pytest.raises(GuildMemberNotFoundError):
        await service.observe_connected(observed_state())

    assert repository.calls == [("lock", 10, 20)]


@pytest.mark.asyncio
async def test_delayed_join_after_closed_session_is_ignored_as_stale() -> None:
    repository = FakeVoiceTransitionRepository()
    service = VoiceTrackingService(repository)
    joined_at = OBSERVED_AT
    left_at = OBSERVED_AT + timedelta(minutes=30)

    assert (
        await service.observe_connected(observed_state(observed_at=joined_at))
        is VoiceTransitionResult.JOINED
    )
    assert (
        await service.observe_disconnected(10, 20, left_at)
        is VoiceTransitionResult.LEFT
    )
    create_count = sum(call[0] == "create" for call in repository.calls)

    result = await service.observe_connected(
        observed_state(observed_at=OBSERVED_AT + timedelta(minutes=20))
    )

    assert result is VoiceTransitionResult.IGNORED_STALE
    assert sum(call[0] == "create" for call in repository.calls) == create_count
    assert repository.current is None


@pytest.mark.asyncio
async def test_later_join_after_closed_session_creates_new_session() -> None:
    repository = FakeVoiceTransitionRepository()
    service = VoiceTrackingService(repository)

    assert (
        await service.observe_connected(observed_state())
        is VoiceTransitionResult.JOINED
    )
    assert repository.current is not None
    first_session_id = repository.current.session_id
    assert (
        await service.observe_disconnected(
            10,
            20,
            OBSERVED_AT + timedelta(minutes=30),
        )
        is VoiceTransitionResult.LEFT
    )

    result = await service.observe_connected(
        observed_state(observed_at=OBSERVED_AT + timedelta(minutes=40))
    )

    assert result is VoiceTransitionResult.JOINED
    assert repository.current is not None
    assert repository.current.session_id != first_session_id


@pytest.mark.asyncio
async def test_join_at_latest_closed_confirmation_is_not_stale() -> None:
    repository = FakeVoiceTransitionRepository(
        latest_confirmed_through_at=OBSERVED_AT,
    )
    service = VoiceTrackingService(repository)

    result = await service.observe_connected(observed_state())

    assert result is VoiceTransitionResult.JOINED
    assert repository.current is not None


@pytest.mark.asyncio
async def test_stale_disconnect_after_closed_session_is_ignored() -> None:
    repository = FakeVoiceTransitionRepository(
        latest_confirmed_through_at=OBSERVED_AT,
    )
    service = VoiceTrackingService(repository)

    result = await service.observe_disconnected(
        10,
        20,
        OBSERVED_AT - timedelta(microseconds=1),
    )

    assert result is VoiceTransitionResult.IGNORED_STALE


@pytest.mark.parametrize("offset", [timedelta(), timedelta(minutes=10)])
@pytest.mark.asyncio
async def test_non_stale_disconnect_without_open_session_is_unchanged(
    offset: timedelta,
) -> None:
    repository = FakeVoiceTransitionRepository(
        latest_confirmed_through_at=OBSERVED_AT,
    )
    service = VoiceTrackingService(repository)

    result = await service.observe_disconnected(10, 20, OBSERVED_AT + offset)

    assert result is VoiceTransitionResult.UNCHANGED


def test_observed_timestamp_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        observed_state(observed_at=datetime(2026, 8, 11, 12, 0))


def test_observed_timestamp_is_normalized_to_utc() -> None:
    local_time = datetime(
        2026,
        8,
        11,
        17,
        0,
        tzinfo=timezone(timedelta(hours=5)),
    )

    observed = observed_state(observed_at=local_time)

    assert observed.observed_at == OBSERVED_AT
    assert observed.observed_at.tzinfo is UTC


@pytest.mark.asyncio
async def test_reconcile_same_snapshot_splits_exact_estimated_exact() -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)
    reconciled_at = OBSERVED_AT + timedelta(minutes=5)

    result = await service.reconcile_connected(
        observed_state(observed_at=reconciled_at)
    )

    assert result is VoiceTransitionResult.UNCHANGED
    assert repository.calls[-1][0] == "reconcile_same"
    assert repository.reconciled_intervals == [
        (OBSERVED_AT, reconciled_at, "estimated")
    ]
    assert repository.qualities == ["estimated", "exact"]
    assert repository.current is not None
    assert repository.current.session_id == 101
    assert repository.current.confirmed_through_at == reconciled_at


@pytest.mark.asyncio
async def test_reconcile_same_snapshot_at_confirmation_is_no_op() -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)

    result = await service.reconcile_connected(observed_state())

    assert result is VoiceTransitionResult.UNCHANGED
    assert repository.calls == [("lock", 10, 20), ("get", 10, 20)]
    assert repository.reconciled_intervals == []


@pytest.mark.asyncio
async def test_reconcile_same_snapshot_is_idempotent_at_same_r() -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)
    reconciled_at = OBSERVED_AT + timedelta(minutes=5)
    observed = observed_state(observed_at=reconciled_at)

    assert (
        await service.reconcile_connected(observed) is VoiceTransitionResult.UNCHANGED
    )
    assert (
        await service.reconcile_connected(observed) is VoiceTransitionResult.UNCHANGED
    )

    assert len(repository.reconciled_intervals) == 1


@pytest.mark.parametrize("channel_id", [30, 31])
@pytest.mark.asyncio
async def test_stale_connected_reconciliation_does_not_mutate_state(
    channel_id: int,
) -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)

    result = await service.reconcile_connected(
        observed_state(
            channel_id=channel_id,
            observed_at=OBSERVED_AT - timedelta(microseconds=1),
        )
    )

    assert result is VoiceTransitionResult.IGNORED_STALE
    assert repository.calls == [("lock", 10, 20), ("get", 10, 20)]
    assert repository.current == open_state()


@pytest.mark.parametrize(
    "changes",
    [
        {"channel_id": 31},
        {"channel_kind": "stage"},
        {"is_afk": True},
    ],
)
@pytest.mark.asyncio
async def test_reconcile_changed_snapshot_closes_at_h_and_starts_new_session_at_r(
    changes: dict[str, object],
) -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)
    reconciled_at = OBSERVED_AT + timedelta(minutes=5)
    observed = observed_state(observed_at=reconciled_at, **changes)

    result = await service.reconcile_connected(observed)

    assert result is VoiceTransitionResult.MOVED
    assert repository.calls[-2:] == [
        ("close", 101, 201, OBSERVED_AT),
        ("create", observed),
    ]
    assert repository.reconciled_intervals == []
    assert repository.current is not None
    assert repository.current.session_id != 101
    assert repository.current.confirmed_through_at == reconciled_at


@pytest.mark.asyncio
async def test_reconcile_changed_snapshot_at_h_starts_new_exact_session() -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)

    result = await service.reconcile_connected(observed_state(channel_id=31))

    assert result is VoiceTransitionResult.MOVED
    assert repository.calls[-2][0] == "close"
    assert repository.calls[-1][0] == "create"
    assert repository.reconciled_intervals == []


@pytest.mark.asyncio
async def test_reconcile_absent_member_closes_at_h_without_estimated_gap() -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)
    reconciled_at = OBSERVED_AT + timedelta(minutes=5)

    result = await service.reconcile_disconnected(10, 20, reconciled_at)

    assert result is VoiceTransitionResult.LEFT
    assert repository.calls[-1] == ("close", 101, 201, OBSERVED_AT)
    assert repository.reconciled_intervals == []
    assert repository.latest_confirmed_through_at == OBSERVED_AT
    assert repository.current is None


@pytest.mark.asyncio
async def test_stale_disconnected_reconciliation_does_not_mutate_state() -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)

    result = await service.reconcile_disconnected(
        10,
        20,
        OBSERVED_AT - timedelta(microseconds=1),
    )

    assert result is VoiceTransitionResult.IGNORED_STALE
    assert repository.calls == [("lock", 10, 20), ("get", 10, 20)]
    assert repository.current == open_state()


@pytest.mark.asyncio
async def test_reconcile_current_connected_without_open_state_joins_at_r() -> None:
    repository = FakeVoiceTransitionRepository()
    service = VoiceTrackingService(repository)
    reconciled_at = OBSERVED_AT + timedelta(minutes=5)

    result = await service.reconcile_connected(
        observed_state(observed_at=reconciled_at)
    )

    assert result is VoiceTransitionResult.JOINED
    assert repository.current is not None
    assert repository.current.confirmed_through_at == reconciled_at
    assert repository.qualities == ["exact"]


@pytest.mark.asyncio
async def test_reconcile_absent_without_open_state_is_idempotent() -> None:
    repository = FakeVoiceTransitionRepository(latest_confirmed_through_at=OBSERVED_AT)
    service = VoiceTrackingService(repository)

    result = await service.reconcile_disconnected(
        10,
        20,
        OBSERVED_AT + timedelta(minutes=5),
    )

    assert result is VoiceTransitionResult.UNCHANGED
    assert repository.current is None


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.asyncio
async def test_reconciliation_older_than_closed_history_is_stale(
    connected: bool,
) -> None:
    repository = FakeVoiceTransitionRepository(latest_confirmed_through_at=OBSERVED_AT)
    service = VoiceTrackingService(repository)
    stale = OBSERVED_AT - timedelta(microseconds=1)

    if connected:
        result = await service.reconcile_connected(observed_state(observed_at=stale))
    else:
        result = await service.reconcile_disconnected(10, 20, stale)

    assert result is VoiceTransitionResult.IGNORED_STALE
    assert repository.current is None


@pytest.mark.asyncio
async def test_reconciliation_timestamp_must_be_timezone_aware() -> None:
    repository = FakeVoiceTransitionRepository(open_state())
    service = VoiceTrackingService(repository)

    with pytest.raises(ValueError, match="timezone-aware"):
        await service.reconcile_disconnected(
            10,
            20,
            datetime(2026, 8, 11, 12, 0),
        )
