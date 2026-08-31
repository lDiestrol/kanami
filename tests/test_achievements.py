from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from discord_stats_bot.features.achievements import (
    DEFAULT_ACHIEVEMENT_CATALOG,
    AchievementCatalog,
    AchievementCategory,
    AchievementDefinition,
    AchievementEvaluator,
    AchievementMetric,
    AchievementMetricSnapshot,
    AchievementUnlockService,
    UnlockedAchievement,
)

T0 = datetime(2026, 8, 17, 12, tzinfo=UTC)


def keys_for(snapshot: AchievementMetricSnapshot) -> tuple[str, ...]:
    evaluator = AchievementEvaluator(DEFAULT_ACHIEVEMENT_CATALOG)
    return tuple(candidate.key for candidate in evaluator.evaluate(snapshot))


def test_catalog_is_small_stable_immutable_and_uniquely_keyed() -> None:
    definitions = DEFAULT_ACHIEVEMENT_CATALOG.definitions

    assert tuple(definition.key for definition in definitions) == (
        "voice_10_hours",
        "voice_50_hours",
        "voice_100_hours",
        "server_age_30_days",
        "server_age_365_days",
    )
    assert len({definition.key for definition in definitions}) == len(definitions)
    assert tuple(definition.threshold for definition in definitions) == (
        36_000,
        180_000,
        360_000,
        30,
        365,
    )
    assert all(
        definition.metric is not AchievementMetric.MESSAGE_COUNT
        for definition in definitions
    )
    with pytest.raises(FrozenInstanceError):
        definitions[0].title = "renamed"  # type: ignore[misc]


def test_catalog_lookup_returns_none_for_retired_or_unknown_key() -> None:
    assert DEFAULT_ACHIEVEMENT_CATALOG.get("voice_10_hours") is not None
    assert DEFAULT_ACHIEVEMENT_CATALOG.get("retired_achievement") is None


def test_evaluator_requires_threshold_and_accepts_exact_or_higher_values() -> None:
    assert keys_for(AchievementMetricSnapshot(voice_seconds=35_999)) == ()
    assert keys_for(AchievementMetricSnapshot(voice_seconds=36_000)) == (
        "voice_10_hours",
    )
    assert keys_for(AchievementMetricSnapshot(voice_seconds=36_001)) == (
        "voice_10_hours",
    )


def test_evaluator_returns_all_reached_tiers_in_catalog_order() -> None:
    assert keys_for(AchievementMetricSnapshot(voice_seconds=400_000)) == (
        "voice_10_hours",
        "voice_50_hours",
        "voice_100_hours",
    )


def test_unavailable_metric_is_not_zero_and_never_unlocks_its_definitions() -> None:
    assert keys_for(AchievementMetricSnapshot(server_age_days=365)) == (
        "server_age_30_days",
        "server_age_365_days",
    )
    assert keys_for(AchievementMetricSnapshot(voice_seconds=0)) == ()
    assert keys_for(AchievementMetricSnapshot()) == ()


@pytest.mark.parametrize(
    "field_name", ["voice_seconds", "server_age_days", "message_count"]
)
def test_metric_snapshot_rejects_negative_values(field_name: str) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        AchievementMetricSnapshot(**{field_name: -1})  # type: ignore[arg-type]


def test_metric_snapshot_requires_integer_semantics() -> None:
    with pytest.raises(TypeError, match="must be an integer"):
        AchievementMetricSnapshot(voice_seconds=True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must be an integer"):
        AchievementMetricSnapshot(server_age_days=1.5)  # type: ignore[arg-type]


def test_custom_message_count_definition_works_without_text_activity_imports() -> None:
    catalog = AchievementCatalog(
        definitions=(
            AchievementDefinition(
                key="message_count_test",
                title="Test",
                description="Future metric test.",
                category=AchievementCategory.TEXT,
                metric=AchievementMetric.MESSAGE_COUNT,
                threshold=5,
            ),
        )
    )

    candidates = AchievementEvaluator(catalog).evaluate(
        AchievementMetricSnapshot(message_count=5)
    )

    assert tuple(candidate.key for candidate in candidates) == ("message_count_test",)


def test_definition_rejects_non_positive_or_duplicate_identity() -> None:
    with pytest.raises(ValueError, match="threshold must be positive"):
        AchievementDefinition(
            key="invalid_threshold",
            title="Invalid",
            description="Invalid threshold.",
            category=AchievementCategory.VOICE,
            metric=AchievementMetric.VOICE_SECONDS,
            threshold=0,
        )
    definition = DEFAULT_ACHIEVEMENT_CATALOG.definitions[0]
    with pytest.raises(ValueError, match="keys must be unique"):
        AchievementCatalog(definitions=(definition, definition))


class InMemoryAchievementRepository:
    def __init__(self) -> None:
        self.records: dict[tuple[int, int, str], UnlockedAchievement] = {}

    async def unlock_achievements(
        self,
        *,
        guild_id: int,
        user_id: int,
        achievement_keys: tuple[str, ...],
        unlocked_at: datetime,
    ) -> tuple[UnlockedAchievement, ...]:
        new = []
        for key in achievement_keys:
            identity = (guild_id, user_id, key)
            if identity in self.records:
                continue
            record = UnlockedAchievement(guild_id, user_id, key, unlocked_at)
            self.records[identity] = record
            new.append(record)
        return tuple(new)

    async def list_unlocked(
        self, *, guild_id: int, user_id: int
    ) -> tuple[UnlockedAchievement, ...]:
        records = (
            record
            for identity, record in self.records.items()
            if identity[:2] == (guild_id, user_id)
        )
        return tuple(
            sorted(records, key=lambda item: (item.unlocked_at, item.achievement_key))
        )


@pytest.mark.asyncio
async def test_unlock_service_is_idempotent_and_preserves_first_timestamp() -> None:
    repository = InMemoryAchievementRepository()
    service = AchievementUnlockService(
        AchievementEvaluator(DEFAULT_ACHIEVEMENT_CATALOG), repository
    )
    snapshot = AchievementMetricSnapshot(voice_seconds=36_000)

    first = await service.evaluate_and_unlock(
        guild_id=10, user_id=20, snapshot=snapshot, unlocked_at=T0
    )
    second = await service.evaluate_and_unlock(
        guild_id=10,
        user_id=20,
        snapshot=snapshot,
        unlocked_at=T0 + timedelta(days=1),
    )

    assert tuple(item.achievement_key for item in first) == ("voice_10_hours",)
    assert second == ()
    assert len(repository.records) == 1
    assert next(iter(repository.records.values())).unlocked_at == T0


@pytest.mark.asyncio
async def test_unlock_service_separates_users_guilds_and_multiple_achievements() -> (
    None
):
    repository = InMemoryAchievementRepository()
    service = AchievementUnlockService(
        AchievementEvaluator(DEFAULT_ACHIEVEMENT_CATALOG), repository
    )
    snapshot = AchievementMetricSnapshot(voice_seconds=180_000)

    for guild_id, user_id in ((10, 20), (10, 21), (11, 20)):
        unlocked = await service.evaluate_and_unlock(
            guild_id=guild_id,
            user_id=user_id,
            snapshot=snapshot,
            unlocked_at=T0,
        )
        assert len(unlocked) == 2

    assert len(repository.records) == 6


@pytest.mark.asyncio
async def test_unlock_service_rejects_naive_timestamp() -> None:
    service = AchievementUnlockService(
        AchievementEvaluator(DEFAULT_ACHIEVEMENT_CATALOG),
        InMemoryAchievementRepository(),
    )

    with pytest.raises(ValueError, match="unlocked_at must be timezone-aware"):
        await service.evaluate_and_unlock(
            guild_id=10,
            user_id=20,
            snapshot=AchievementMetricSnapshot(voice_seconds=36_000),
            unlocked_at=datetime(2026, 8, 17, 12),
        )
