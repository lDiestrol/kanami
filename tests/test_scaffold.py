import asyncio
import importlib
from collections.abc import Coroutine
from pathlib import Path

import pytest
from pydantic import ValidationError

import discord_stats_bot.main as main_module
from discord_stats_bot.config import Settings, WebSettings
from discord_stats_bot.features.server_settings import (
    DISABLED_OVERRIDE,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
    GuildServerSettingsBaselines,
    GuildServerSettingsOverrides,
    resolve_guild_server_settings,
)

CONFIG_ENV_NAMES = (
    "DISCORD_TOKEN",
    "DISCORD_GUILD_ID",
    "DISCORD_AUDIT_LOG_CHANNEL_ID",
    "DISCORD_AUTOROLE_ID",
    "DISCORD_ANNIVERSARY_CHANNEL_ID",
    "DISCORD_RETURN_CHANNEL_ID",
    "DISCORD_GUEST_ROLE_ID",
    "DISCORD_INITIATED_ROLE_ID",
    "DISCORD_GUARDIAN_ROLE_ID",
    "DISCORD_PURPLE_ROLE_ID",
    "DISCORD_GOLD_ROLE_ID",
    "MEMBER_RETURN_MIN_ABSENCE_SECONDS",
    "WEB_ADMIN_HOST",
    "WEB_ADMIN_PORT",
    "DATABASE_URL",
    "REPORT_TIMEZONE",
    "RAW_MESSAGE_RETENTION_DAYS",
    "SERVER_EVENT_RETENTION_DAYS",
    "AUDIT_TRANSIENT_RETENTION_DAYS",
    "VOICE_MIN_SESSION_SECONDS",
    "VOICE_CHECKPOINT_INTERVAL_SECONDS",
    "GAME_TRACKING_ENABLED",
    "GAME_CONFIRM_INTERVAL_SECONDS",
    "LOG_LEVEL",
)

REQUIRED_SETTINGS = {
    "DISCORD_TOKEN": "test-token",
    "DISCORD_GUILD_ID": 123456789,
    "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
}


@pytest.fixture(autouse=True)
def isolate_scaffold_tests_from_local_dotenv(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Keep ignored developer/production .env files out of settings unit tests."""

    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def clear_config_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CONFIG_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def use_in_memory_server_settings_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Provider:
        def __init__(
            self,
            session_factory: object,
            *,
            guild_id: int,
            baselines: GuildServerSettingsBaselines,
        ) -> None:
            del session_factory
            self.effective = resolve_guild_server_settings(
                guild_id,
                baselines,
                None,
            )

        async def get(self) -> object:
            return self.effective

        async def invalidate(self) -> None:
            pass

    monkeypatch.setattr(main_module, "RefreshableGuildServerSettingsProvider", Provider)


def make_settings(**overrides: object) -> Settings:
    values = REQUIRED_SETTINGS | overrides
    return Settings(_env_file=None, **values)


def test_package_imports() -> None:
    package = importlib.import_module("discord_stats_bot")
    assert package.__name__ == "discord_stats_bot"


def test_settings_defaults() -> None:
    settings = make_settings()

    assert settings.report_timezone == "UTC"
    assert settings.raw_message_retention_days == 90
    assert settings.server_event_retention_days == 365
    assert settings.discord_audit_log_channel_id is None
    assert settings.discord_autorole_id is None
    assert settings.discord_anniversary_channel_id is None
    assert settings.discord_return_channel_id is None
    assert settings.discord_guest_role_id is None
    assert settings.discord_initiated_role_id is None
    assert settings.discord_guardian_role_id is None
    assert settings.discord_purple_role_id is None
    assert settings.discord_gold_role_id is None
    assert settings.rules_accepted_role_id is None
    assert settings.member_return_min_absence_seconds == 86_400
    assert not hasattr(settings, "web_admin_host")
    assert not hasattr(settings, "web_admin_port")
    assert settings.audit_transient_retention_days == 90
    assert settings.voice_min_session_seconds == 10
    assert settings.voice_checkpoint_interval_seconds == 60
    assert not settings.game_tracking_enabled
    assert settings.game_confirm_interval_seconds == 60
    assert settings.log_level == "INFO"


def test_invalid_web_host_does_not_affect_discord_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEB_ADMIN_HOST", "0.0.0.0")

    settings = make_settings()

    assert not hasattr(settings, "web_admin_host")
    assert not hasattr(settings, "web_admin_port")
    with pytest.raises(ValidationError, match="wildcard"):
        WebSettings(
            _env_file=None,
            DATABASE_URL=REQUIRED_SETTINGS["DATABASE_URL"],
            DISCORD_GUILD_ID=REQUIRED_SETTINGS["DISCORD_GUILD_ID"],
        )


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REQUIRED_SETTINGS.items():
        monkeypatch.setenv(name, str(value))

    settings = Settings(_env_file=None)

    assert settings.discord_guild_id == REQUIRED_SETTINGS["DISCORD_GUILD_ID"]


def test_missing_required_settings_raise_validation_error() -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None)

    missing_fields = {error["loc"][0] for error in exc_info.value.errors()}
    assert missing_fields == {"DISCORD_TOKEN", "DISCORD_GUILD_ID", "DATABASE_URL"}


@pytest.mark.parametrize("field", ["DISCORD_TOKEN", "DATABASE_URL"])
def test_empty_required_secret_is_rejected(field: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: ""})


@pytest.mark.parametrize("field", ["DISCORD_TOKEN", "DATABASE_URL"])
@pytest.mark.parametrize("value", [" ", "\t", " \t "])
def test_whitespace_only_required_secret_is_rejected(
    field: str,
    value: str,
) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


@pytest.mark.parametrize("guild_id", [0, -1])
def test_non_positive_guild_id_is_rejected(guild_id: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(DISCORD_GUILD_ID=guild_id)


@pytest.mark.parametrize(
    "field",
    [
        "RAW_MESSAGE_RETENTION_DAYS",
        "SERVER_EVENT_RETENTION_DAYS",
        "AUDIT_TRANSIENT_RETENTION_DAYS",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_retention_is_rejected(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_voice_minimum_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(VOICE_MIN_SESSION_SECONDS=value)


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_audit_channel_id_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(DISCORD_AUDIT_LOG_CHANNEL_ID=value)


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_autorole_id_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(DISCORD_AUTOROLE_ID=value)


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_anniversary_channel_id_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(DISCORD_ANNIVERSARY_CHANNEL_ID=value)


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_return_channel_id_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(DISCORD_RETURN_CHANNEL_ID=value)


@pytest.mark.parametrize(
    "field",
    [
        "DISCORD_GUEST_ROLE_ID",
        "DISCORD_INITIATED_ROLE_ID",
        "DISCORD_GUARDIAN_ROLE_ID",
        "DISCORD_PURPLE_ROLE_ID",
        "DISCORD_GOLD_ROLE_ID",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_member_profile_role_id_is_rejected(
    field: str, value: int
) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_return_minimum_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(MEMBER_RETURN_MIN_ABSENCE_SECONDS=value)


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_voice_checkpoint_interval_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(VOICE_CHECKPOINT_INTERVAL_SECONDS=value)


@pytest.mark.parametrize("value", [0, -1])
def test_non_positive_game_confirmation_interval_is_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(GAME_CONFIRM_INTERVAL_SECONDS=value)


def test_postgresql_asyncpg_database_url_is_valid() -> None:
    settings = make_settings()

    assert settings.database_url.get_secret_value().startswith("postgresql+asyncpg://")


def test_malformed_database_url_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(
            DATABASE_URL="postgresql+asyncpg://user:password@localhost:not-a-port/db"
        )


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://user:password@localhost/db",
        "sqlite+aiosqlite:///local.db",
    ],
)
def test_wrong_database_driver_is_rejected(database_url: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(DATABASE_URL=database_url)


@pytest.mark.parametrize("timezone_name", ["UTC", "Europe/Stockholm"])
def test_valid_report_timezone_is_accepted(timezone_name: str) -> None:
    settings = make_settings(REPORT_TIMEZONE=timezone_name)

    assert settings.report_timezone == timezone_name


def test_unknown_report_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(REPORT_TIMEZONE="Not/AZone")


def test_log_level_is_normalized_to_uppercase() -> None:
    settings = make_settings(LOG_LEVEL="debug")

    assert settings.log_level == "DEBUG"


def test_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(LOG_LEVEL="verbose")


def test_secret_values_are_masked() -> None:
    settings = make_settings()
    rendered_settings = repr(settings)

    assert REQUIRED_SETTINGS["DISCORD_TOKEN"] not in rendered_settings
    assert REQUIRED_SETTINGS["DATABASE_URL"] not in rendered_settings
    assert "test:test" not in rendered_settings
    assert str(settings.discord_token) == "**********"
    assert str(settings.database_url) == "**********"


def test_database_validation_error_does_not_expose_credentials() -> None:
    password = "review-only-password"
    invalid_url = f"postgresql://user:{password}@localhost/db"

    with pytest.raises(ValidationError) as exc_info:
        make_settings(DATABASE_URL=invalid_url)

    assert password not in str(exc_info.value)
    assert invalid_url not in str(exc_info.value)


def test_main_output_does_not_expose_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called_with: list[Settings] = []

    async def fake_run_application(settings: Settings) -> None:
        called_with.append(settings)

    monkeypatch.setattr(main_module, "run_application", fake_run_application)
    for name, value in REQUIRED_SETTINGS.items():
        monkeypatch.setenv(name, str(value))

    assert main_module.main() == 0

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "starting Discord Gateway" in output
    assert REQUIRED_SETTINGS["DISCORD_TOKEN"] not in output
    assert REQUIRED_SETTINGS["DATABASE_URL"] not in output
    assert "test:test" not in output
    assert len(called_with) == 1


def test_main_treats_keyboard_interrupt_as_normal_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_asyncio_run(coroutine: Coroutine[object, object, object]) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(main_module.asyncio, "run", fake_asyncio_run)
    for name, value in REQUIRED_SETTINGS.items():
        monkeypatch.setenv(name, str(value))

    assert main_module.main() == 0

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "Shutdown requested; application stopped normally" in output
    assert "Traceback" not in output


def test_main_does_not_hide_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_asyncio_run(coroutine: Coroutine[object, object, object]) -> None:
        coroutine.close()
        raise RuntimeError("unexpected application failure")

    monkeypatch.setattr(main_module.asyncio, "run", fake_asyncio_run)
    for name, value in REQUIRED_SETTINGS.items():
        monkeypatch.setenv(name, str(value))

    with pytest.raises(RuntimeError, match="unexpected application failure"):
        main_module.main()


@pytest.mark.asyncio
async def test_application_runtime_owns_client_and_database_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = make_settings()
    calls: list[object] = []

    class FakeResources:
        session_factory = object()

        async def dispose(self) -> None:
            calls.append("dispose")

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            calls.append("client_enter")
            return self

        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_value: BaseException | None,
            traceback: object,
        ) -> None:
            calls.append("client_exit")

        async def start(self, token: str) -> None:
            calls.append(("start", token))

    resources = FakeResources()
    reconciler = object()
    provisioner = object()
    voice_event_handler = object()
    voice_checkpointer = object()
    voice_stats_command_handler = object()
    voice_leaderboard_command_handler = object()
    voice_channel_leaderboard_command_handler = object()
    voice_channel_statistics_command_handler = object()
    voice_together_command_handler = object()
    voice_server_statistics_command_handler = object()
    text_activity_event_handler = object()
    text_leaderboard_command_handler = object()
    health_command_handler = object()
    health_handler_kwargs: list[dict[str, object]] = []
    stats_handler_kwargs: list[dict[str, object]] = []
    client = FakeClient()
    client_kwargs: list[dict[str, object]] = []
    monkeypatch.setattr(
        main_module,
        "create_database_resources",
        lambda runtime_settings: resources,
    )
    monkeypatch.setattr(
        main_module,
        "GuildReferenceProvisioner",
        lambda session_factory: provisioner,
    )
    monkeypatch.setattr(
        main_module,
        "VoiceStartupReconciler",
        lambda session_factory: reconciler,
    )
    monkeypatch.setattr(
        main_module,
        "VoiceStateEventHandler",
        lambda session_factory, guild_id: voice_event_handler,
    )
    monkeypatch.setattr(
        main_module,
        "VoiceCheckpointRunner",
        lambda session_factory: voice_checkpointer,
    )

    def make_stats_handler(session_factory: object, **kwargs: object) -> object:
        assert session_factory is resources.session_factory
        stats_handler_kwargs.append(kwargs)
        return voice_stats_command_handler

    monkeypatch.setattr(
        main_module,
        "VoiceStatisticsCommandHandler",
        make_stats_handler,
    )
    monkeypatch.setattr(
        main_module,
        "VoiceLeaderboardCommandHandler",
        lambda session_factory, **kwargs: voice_leaderboard_command_handler,
    )
    monkeypatch.setattr(
        main_module,
        "VoiceChannelLeaderboardCommandHandler",
        lambda session_factory, **kwargs: voice_channel_leaderboard_command_handler,
    )
    monkeypatch.setattr(
        main_module,
        "VoiceChannelStatisticsCommandHandler",
        lambda session_factory, **kwargs: voice_channel_statistics_command_handler,
    )
    monkeypatch.setattr(
        main_module,
        "VoiceTogetherCommandHandler",
        lambda session_factory, **kwargs: voice_together_command_handler,
    )
    monkeypatch.setattr(
        main_module,
        "VoiceServerStatisticsCommandHandler",
        lambda session_factory, **kwargs: voice_server_statistics_command_handler,
    )
    monkeypatch.setattr(
        main_module,
        "TextActivityEventHandler",
        lambda session_factory, **kwargs: text_activity_event_handler,
    )
    monkeypatch.setattr(
        main_module,
        "TextLeaderboardCommandHandler",
        lambda session_factory, **kwargs: text_leaderboard_command_handler,
    )

    def make_health_handler(session_factory: object, **kwargs: object) -> object:
        assert session_factory is resources.session_factory
        health_handler_kwargs.append(kwargs)
        return health_command_handler

    monkeypatch.setattr(main_module, "HealthCommandHandler", make_health_handler)
    monkeypatch.setattr(main_module.time, "monotonic", lambda: 123.0)

    def make_client(**kwargs: object) -> FakeClient:
        client_kwargs.append(kwargs)
        return client

    monkeypatch.setattr(
        main_module,
        "DiscordStatsClient",
        make_client,
    )

    await main_module.run_application(settings)

    assert calls == [
        "client_enter",
        ("start", REQUIRED_SETTINGS["DISCORD_TOKEN"]),
        "client_exit",
        "dispose",
    ]
    assert client_kwargs[0]["voice_checkpointer"] is voice_checkpointer
    assert client_kwargs[0]["voice_checkpoint_interval_seconds"] == 60
    assert client_kwargs[0]["game_presence_event_handler"] is None
    assert client_kwargs[0]["game_startup_reconciler"] is None
    assert client_kwargs[0]["game_checkpointer"] is None
    assert client_kwargs[0]["game_confirm_interval_seconds"] == 60
    assert client_kwargs[0]["voice_stats_command_handler"] is (
        voice_stats_command_handler
    )
    assert client_kwargs[0]["voice_leaderboard_command_handler"] is (
        voice_leaderboard_command_handler
    )
    assert client_kwargs[0]["voice_channel_leaderboard_command_handler"] is (
        voice_channel_leaderboard_command_handler
    )
    assert client_kwargs[0]["voice_channel_statistics_command_handler"] is (
        voice_channel_statistics_command_handler
    )
    assert client_kwargs[0]["voice_together_command_handler"] is (
        voice_together_command_handler
    )
    assert client_kwargs[0]["voice_server_statistics_command_handler"] is (
        voice_server_statistics_command_handler
    )
    assert client_kwargs[0]["text_activity_event_handler"] is (
        text_activity_event_handler
    )
    assert client_kwargs[0]["text_leaderboard_command_handler"] is (
        text_leaderboard_command_handler
    )
    assert client_kwargs[0]["health_command_handler"] is health_command_handler
    assert health_handler_kwargs == [
        {"guild_id": 123456789, "process_started_at": 123.0}
    ]
    assert client_kwargs[0]["audit_event_ingestor"] is not None
    assert client_kwargs[0]["audit_delivery_runner"] is not None
    assert client_kwargs[0]["audit_retention_runner"] is not None
    assert client_kwargs[0]["member_anniversary_check_runner"] is not None
    assert client_kwargs[0]["member_return_event_handler"] is not None
    assert client_kwargs[0]["autorole_handler"] is not None
    assert str(stats_handler_kwargs[0]["report_timezone"]) == "UTC"
    assert stats_handler_kwargs[0]["min_session_seconds"] == 10
    assert (
        "Effective guild server settings: autorole=disabled audit_log=disabled "
        "anniversaries=disabled member_returns=disabled"
    ) in caplog.text


def test_startup_status_uses_db_value_when_env_baseline_is_absent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="discord_stats_bot.main")
    effective = resolve_guild_server_settings(
        10,
        GuildServerSettingsBaselines(),
        GuildServerSettingsOverrides(
            guild_id=10,
            autorole_role=GuildServerSettingOverride(
                GuildServerSettingOverrideMode.VALUE,
                20,
            ),
        ),
    )

    main_module._log_effective_server_settings(effective)

    assert "autorole=enabled" in caplog.text


def test_startup_status_uses_db_disabled_when_env_baseline_is_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO", logger="discord_stats_bot.main")
    effective = resolve_guild_server_settings(
        10,
        GuildServerSettingsBaselines(
            autorole_role_id=20,
            audit_log_channel_id=30,
            anniversary_channel_id=40,
            return_channel_id=50,
        ),
        GuildServerSettingsOverrides(
            guild_id=10,
            autorole_role=DISABLED_OVERRIDE,
            audit_log_channel=DISABLED_OVERRIDE,
            anniversary_channel=DISABLED_OVERRIDE,
            return_channel=DISABLED_OVERRIDE,
        ),
    )

    main_module._log_effective_server_settings(effective)

    assert (
        "autorole=disabled audit_log=disabled anniversaries=disabled "
        "member_returns=disabled"
    ) in caplog.text


@pytest.mark.asyncio
async def test_application_cancellation_closes_client_and_database_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    calls: list[str] = []

    class FakeResources:
        session_factory = object()

        async def dispose(self) -> None:
            calls.append("dispose")

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            await self.close()

        async def start(self, token: str) -> None:
            del token
            calls.append("start")
            raise asyncio.CancelledError

        async def close(self) -> None:
            calls.append("close")

    resources = FakeResources()
    dependencies = (
        "GuildReferenceProvisioner",
        "VoiceStartupReconciler",
        "VoiceStateEventHandler",
        "VoiceCheckpointRunner",
        "VoiceStatisticsCommandHandler",
        "VoiceLeaderboardCommandHandler",
        "VoiceChannelLeaderboardCommandHandler",
        "VoiceTogetherCommandHandler",
        "VoiceServerStatisticsCommandHandler",
        "TextActivityEventHandler",
        "TextLeaderboardCommandHandler",
        "HealthCommandHandler",
    )
    monkeypatch.setattr(
        main_module,
        "create_database_resources",
        lambda runtime_settings: resources,
    )
    for dependency in dependencies:
        monkeypatch.setattr(
            main_module,
            dependency,
            lambda *args, **kwargs: object(),
        )
    monkeypatch.setattr(
        main_module, "DiscordStatsClient", lambda **kwargs: FakeClient()
    )

    with pytest.raises(asyncio.CancelledError):
        await main_module.run_application(settings)

    assert calls == ["start", "close", "dispose"]


@pytest.mark.asyncio
async def test_application_wires_optional_features_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(
        DISCORD_AUDIT_LOG_CHANNEL_ID=40,
        DISCORD_AUTOROLE_ID=50,
        DISCORD_ANNIVERSARY_CHANNEL_ID=60,
        DISCORD_RETURN_CHANNEL_ID=70,
        MEMBER_RETURN_MIN_ABSENCE_SECONDS=43_200,
        AUDIT_TRANSIENT_RETENTION_DAYS=45,
        GAME_TRACKING_ENABLED=True,
        GAME_CONFIRM_INTERVAL_SECONDS=75,
    )
    calls: list[object] = []

    class FakeResources:
        session_factory = object()

        async def dispose(self) -> None:
            calls.append("dispose")

    class FakeDelivery:
        def wake(self) -> None:
            pass

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def start(self, token: str) -> None:
            del token

    resources = FakeResources()
    delivery = FakeDelivery()
    retention = object()
    ingestor = object()
    autorole = object()
    anniversary_runner = object()
    return_handler = object()
    game_handler = object()
    game_reconciler = object()
    game_checkpointer = object()
    monkeypatch.setattr(
        main_module, "create_database_resources", lambda runtime_settings: resources
    )
    for dependency in (
        "GuildReferenceProvisioner",
        "VoiceStartupReconciler",
        "VoiceStateEventHandler",
        "VoiceCheckpointRunner",
        "VoiceStatisticsCommandHandler",
        "VoiceLeaderboardCommandHandler",
        "VoiceChannelLeaderboardCommandHandler",
        "VoiceChannelStatisticsCommandHandler",
        "VoiceTogetherCommandHandler",
        "VoiceServerStatisticsCommandHandler",
        "TextActivityEventHandler",
        "TextLeaderboardCommandHandler",
    ):
        monkeypatch.setattr(main_module, dependency, lambda *args, **kwargs: object())

    def make_delivery(session_factory: object, **kwargs: object) -> object:
        assert session_factory is resources.session_factory
        assert kwargs["guild_id"] == 123456789
        assert kwargs["settings_provider"] is not None
        assert str(kwargs["report_timezone"]) == "UTC"
        return delivery

    def make_ingestor(session_factory: object, **kwargs: object) -> object:
        assert session_factory is resources.session_factory
        assert kwargs["guild_id"] == 123456789
        assert kwargs["transient_retention_days"] == 45
        assert kwargs["wake_delivery"] == delivery.wake
        assert str(kwargs["report_timezone"]) == "UTC"
        assert kwargs["min_session_seconds"] == settings.voice_min_session_seconds
        assert kwargs["settings_provider"] is not None
        return ingestor

    monkeypatch.setattr(main_module, "AuditLogDeliveryRunner", make_delivery)
    monkeypatch.setattr(
        main_module,
        "AuditRetentionRunner",
        lambda session_factory, **kwargs: retention,
    )
    monkeypatch.setattr(main_module, "AuditEventIngestor", make_ingestor)

    def make_anniversary_runner(session_factory: object, **kwargs: object) -> object:
        assert session_factory is resources.session_factory
        assert kwargs["guild_id"] == 123456789
        assert str(kwargs["report_timezone"]) == "UTC"
        assert kwargs["wake_delivery"] == delivery.wake
        assert kwargs["settings_provider"] is not None
        return anniversary_runner

    monkeypatch.setattr(
        main_module,
        "MemberAnniversaryCheckRunner",
        make_anniversary_runner,
    )

    def make_return_handler(session_factory: object, **kwargs: object) -> object:
        assert session_factory is resources.session_factory
        assert kwargs["guild_id"] == 123456789
        assert str(kwargs["report_timezone"]) == "UTC"
        assert kwargs["min_absence_seconds"] == 43_200
        assert kwargs["min_session_seconds"] == settings.voice_min_session_seconds
        assert kwargs["wake_delivery"] == delivery.wake
        assert kwargs["settings_provider"] is not None
        return return_handler

    monkeypatch.setattr(main_module, "MemberReturnEventHandler", make_return_handler)

    def make_autorole(**kwargs: object) -> object:
        assert kwargs["guild_id"] == 123456789
        assert kwargs["settings_provider"] is not None
        return autorole

    monkeypatch.setattr(main_module, "AutoroleHandler", make_autorole)
    monkeypatch.setattr(
        main_module,
        "GamePresenceEventHandler",
        lambda session_factory, **kwargs: game_handler,
    )
    monkeypatch.setattr(
        main_module,
        "GameStartupReconciler",
        lambda session_factory: game_reconciler,
    )
    monkeypatch.setattr(
        main_module,
        "GameCheckpointRunner",
        lambda session_factory: game_checkpointer,
    )

    def make_client(**kwargs: object) -> FakeClient:
        assert kwargs["audit_event_ingestor"] is ingestor
        assert kwargs["audit_delivery_runner"] is delivery
        assert kwargs["audit_retention_runner"] is retention
        assert kwargs["autorole_handler"] is autorole
        assert kwargs["member_anniversary_check_runner"] is anniversary_runner
        assert kwargs["member_return_event_handler"] is return_handler
        assert kwargs["game_presence_event_handler"] is game_handler
        assert kwargs["game_startup_reconciler"] is game_reconciler
        assert kwargs["game_checkpointer"] is game_checkpointer
        assert kwargs["game_confirm_interval_seconds"] == 75
        return FakeClient()

    monkeypatch.setattr(main_module, "DiscordStatsClient", make_client)

    await main_module.run_application(settings)

    assert calls == ["dispose"]


@pytest.mark.asyncio
async def test_return_channel_alone_enables_only_member_history_and_return_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings(DISCORD_RETURN_CHANNEL_ID=70)
    captured: dict[str, object] = {}

    class FakeResources:
        session_factory = object()

        async def dispose(self) -> None:
            pass

    class FakeDelivery:
        def wake(self) -> None:
            pass

    class FakeClient:
        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def start(self, token: str) -> None:
            del token

    resources = FakeResources()
    delivery = FakeDelivery()
    ingestor = object()
    return_handler = object()
    retention = object()
    monkeypatch.setattr(main_module, "create_database_resources", lambda _: resources)

    def make_delivery(session_factory: object, **kwargs: object) -> object:
        assert session_factory is resources.session_factory
        assert kwargs["settings_provider"] is not None
        return delivery

    def make_ingestor(session_factory: object, **kwargs: object) -> object:
        assert session_factory is resources.session_factory
        assert kwargs["settings_provider"] is not None
        assert kwargs["wake_delivery"] == delivery.wake
        assert kwargs["settings_provider"] is not None
        return ingestor

    def make_return_handler(session_factory: object, **kwargs: object) -> object:
        assert session_factory is resources.session_factory
        assert kwargs["wake_delivery"] == delivery.wake
        return return_handler

    monkeypatch.setattr(main_module, "AuditLogDeliveryRunner", make_delivery)
    monkeypatch.setattr(main_module, "AuditEventIngestor", make_ingestor)
    monkeypatch.setattr(main_module, "MemberReturnEventHandler", make_return_handler)
    monkeypatch.setattr(
        main_module,
        "AuditRetentionRunner",
        lambda *args, **kwargs: retention,
    )

    def make_client(**kwargs: object) -> FakeClient:
        captured.update(kwargs)
        return FakeClient()

    monkeypatch.setattr(main_module, "DiscordStatsClient", make_client)

    await main_module.run_application(settings)

    assert captured["audit_event_ingestor"] is ingestor
    assert captured["audit_delivery_runner"] is delivery
    assert captured["audit_retention_runner"] is retention
    assert captured["member_return_event_handler"] is return_handler
