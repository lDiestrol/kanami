import ast
import asyncio
import inspect
import textwrap
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import discord_stats_bot.discord.server_settings as provider_module
from discord_stats_bot.discord.server_settings import (
    RefreshableGuildServerSettingsProvider,
)
from discord_stats_bot.features.server_settings import (
    DISABLED_OVERRIDE,
    ENV_OVERRIDE,
    SERVER_SETTING_CHANGED_EVENT_TYPE,
    GuildServerSettingKey,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
    GuildServerSettingsBaselines,
    GuildServerSettingsMutationService,
    GuildServerSettingSource,
    GuildServerSettingsOverrides,
    resolve_guild_server_settings,
)

T0 = datetime(2026, 8, 23, 12, tzinfo=UTC)
BASELINES = GuildServerSettingsBaselines(
    autorole_role_id=11,
    audit_log_channel_id=12,
    anniversary_channel_id=13,
    return_channel_id=14,
)


class FakeRepository:
    def __init__(self, current: GuildServerSettingsOverrides | None = None) -> None:
        self.current = current
        self.locked: list[int] = []
        self.saved: list[dict[str, object]] = []

    async def lock_guild(self, guild_id: int) -> None:
        self.locked.append(guild_id)

    async def get_overrides(
        self, guild_id: int, *, for_update: bool = False
    ) -> GuildServerSettingsOverrides | None:
        assert guild_id == 10
        assert for_update is True
        return self.current

    async def save_override(self, **kwargs: object) -> None:
        self.saved.append(dict(kwargs))


class FakeAuditRepository:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.created: list[tuple[object, datetime | None]] = []
        self.suppressed: list[tuple[tuple[int, ...], datetime]] = []

    async def create(self, draft: object, *, expires_at: datetime | None) -> object:
        if self.error is not None:
            raise self.error
        self.created.append((draft, expires_at))
        return SimpleNamespace(id=99)

    async def create_many(self, events: object) -> tuple[object, ...]:
        raise AssertionError("create_many must not be used")

    async def mark_delivery_suppressed(
        self, event_ids: tuple[int, ...], suppressed_at: datetime
    ) -> None:
        self.suppressed.append((event_ids, suppressed_at))


def test_empty_db_uses_all_env_baselines() -> None:
    effective = resolve_guild_server_settings(10, BASELINES, None)

    assert effective.autorole_role_id == 11
    assert effective.audit_log_channel_id == 12
    assert effective.anniversary_channel_id == 13
    assert effective.return_channel_id == 14
    assert all(
        effective.source_for(key) is GuildServerSettingSource.ENV
        for key in GuildServerSettingKey
    )


def test_value_disabled_and_env_resolve_independently() -> None:
    overrides = GuildServerSettingsOverrides(
        guild_id=10,
        autorole_role=GuildServerSettingOverride(
            GuildServerSettingOverrideMode.VALUE, 21
        ),
        audit_log_channel=DISABLED_OVERRIDE,
        anniversary_channel=ENV_OVERRIDE,
        return_channel=GuildServerSettingOverride(
            GuildServerSettingOverrideMode.VALUE, 24
        ),
    )

    effective = resolve_guild_server_settings(10, BASELINES, overrides)

    assert effective.autorole_role_id == 21
    assert effective.autorole_role_source is GuildServerSettingSource.DB
    assert effective.audit_log_channel_id is None
    assert effective.audit_log_channel_source is GuildServerSettingSource.DISABLED
    assert effective.anniversary_channel_id == 13
    assert effective.anniversary_channel_source is GuildServerSettingSource.ENV
    assert effective.return_channel_id == 24


@pytest.mark.asyncio
async def test_exact_configured_noop_has_no_write_or_audit() -> None:
    repository = FakeRepository(GuildServerSettingsOverrides(guild_id=10))
    audit = FakeAuditRepository()
    service = GuildServerSettingsMutationService(
        repository,
        audit,
        baselines=BASELINES,  # type: ignore[arg-type]
    )

    result = await service.change(
        guild_id=10,
        key=GuildServerSettingKey.AUTOROLE_ROLE,
        override=ENV_OVERRIDE,
        actor_user_id=30,
        occurred_at=T0,
    )

    assert result.changed is False
    assert repository.locked == [10]
    assert repository.saved == []
    assert audit.created == []


@pytest.mark.asyncio
async def test_real_change_writes_bounded_history_only_audit() -> None:
    repository = FakeRepository()
    audit = FakeAuditRepository()
    service = GuildServerSettingsMutationService(
        repository,
        audit,
        baselines=BASELINES,  # type: ignore[arg-type]
    )
    override = GuildServerSettingOverride(GuildServerSettingOverrideMode.VALUE, 55)

    result = await service.change(
        guild_id=10,
        key=GuildServerSettingKey.AUDIT_LOG_CHANNEL,
        override=override,
        actor_user_id=30,
        occurred_at=T0,
    )

    assert result.changed is True
    assert result.effective.audit_log_channel_id == 55
    assert repository.saved == [
        {
            "guild_id": 10,
            "key": GuildServerSettingKey.AUDIT_LOG_CHANNEL,
            "override": override,
            "updated_at": T0,
            "updated_by_user_id": 30,
        }
    ]
    assert len(audit.created) == 1
    draft, expires_at = audit.created[0]
    assert expires_at is None
    assert draft.event_type == SERVER_SETTING_CHANGED_EVENT_TYPE  # type: ignore[attr-defined]
    assert draft.category == "web_admin"  # type: ignore[attr-defined]
    assert draft.subject_type == "guild_setting"  # type: ignore[attr-defined]
    assert draft.subject_id is None  # type: ignore[attr-defined]
    assert draft.actor_user_id == 30  # type: ignore[attr-defined]
    assert draft.before_data == {"source": "env", "value": 12}  # type: ignore[attr-defined]
    assert draft.after_data == {"source": "value", "value": 55}  # type: ignore[attr-defined]
    assert draft.details_data == {"setting_key": "audit_log_channel"}  # type: ignore[attr-defined]
    assert audit.suppressed == [((99,), T0)]


@pytest.mark.asyncio
async def test_audit_failure_propagates_for_transaction_rollback() -> None:
    repository = FakeRepository()
    audit = FakeAuditRepository(RuntimeError("audit failed"))
    service = GuildServerSettingsMutationService(
        repository,
        audit,
        baselines=BASELINES,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="audit failed"):
        await service.change(
            guild_id=10,
            key=GuildServerSettingKey.RETURN_CHANNEL,
            override=DISABLED_OVERRIDE,
            actor_user_id=30,
            occurred_at=T0,
        )

    assert len(repository.saved) == 1
    assert audit.suppressed == []


def test_unknown_keys_and_invalid_override_shapes_are_rejected() -> None:
    with pytest.raises(ValueError):
        GuildServerSettingKey("arbitrary_env_name")
    with pytest.raises(ValueError):
        GuildServerSettingOverride(GuildServerSettingOverrideMode.VALUE)
    with pytest.raises(ValueError):
        GuildServerSettingOverride(GuildServerSettingOverrideMode.DISABLED, 10)


def test_domain_service_has_no_hidden_transaction_control() -> None:
    source = textwrap.dedent(inspect.getsource(GuildServerSettingsMutationService))
    tree = ast.parse(source)
    transaction_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"begin", "commit", "rollback"}
    ]

    assert transaction_calls == []


@pytest.mark.asyncio
async def test_inflight_old_read_cannot_repopulate_cache_after_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_started = asyncio.Event()
    release_old_read = asyncio.Event()
    invalidation_started = asyncio.Event()
    old_overrides = GuildServerSettingsOverrides(guild_id=10)
    new_overrides = GuildServerSettingsOverrides(
        guild_id=10,
        autorole_role=GuildServerSettingOverride(
            GuildServerSettingOverrideMode.VALUE, 99
        ),
    )
    repository_state = SimpleNamespace(
        current=old_overrides,
        reads=0,
    )

    class SessionContext:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *args: object) -> None:
            pass

    class BlockingRepository:
        def __init__(self, session: object) -> None:
            del session

        async def get_overrides(
            self, guild_id: int
        ) -> GuildServerSettingsOverrides | None:
            assert guild_id == 10
            repository_state.reads += 1
            if repository_state.reads == 1:
                snapshot = repository_state.current
                read_started.set()
                await release_old_read.wait()
                return snapshot
            return repository_state.current

    monkeypatch.setattr(
        provider_module,
        "SqlAlchemyGuildServerSettingsRepository",
        BlockingRepository,
    )
    provider = RefreshableGuildServerSettingsProvider(
        lambda: SessionContext(),  # type: ignore[arg-type]
        guild_id=10,
        baselines=BASELINES,
    )

    old_get = asyncio.create_task(provider.get())
    await read_started.wait()
    repository_state.current = new_overrides

    async def invalidate_after_mutation() -> None:
        invalidation_started.set()
        await provider.invalidate()

    invalidation = asyncio.create_task(invalidate_after_mutation())
    await invalidation_started.wait()
    release_old_read.set()

    old_effective, _ = await asyncio.gather(old_get, invalidation)
    fresh_effective = await provider.get()

    assert old_effective.autorole_role_id == 11
    assert fresh_effective.autorole_role_id == 99
    assert repository_state.reads == 2
