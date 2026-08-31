import re
from ipaddress import IPv4Network, IPv6Network, ip_address
from typing import Annotated, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

PositiveInt = Annotated[int, Field(gt=0)]
NonEmptySecret = Annotated[SecretStr, Field(min_length=1)]
WebAdminHost = Annotated[str, Field(min_length=1)]
TcpPort = Annotated[int, Field(ge=1, le=65_535)]
MAX_DISCORD_SNOWFLAKE = 18_446_744_073_709_551_615
DiscordClientId = Annotated[int, Field(gt=0, le=MAX_DISCORD_SNOWFLAKE)]
WebSessionLifetime = Annotated[int, Field(ge=300, le=86_400)]
WebAdminAllowedUserIds = Annotated[frozenset[DiscordClientId], NoDecode]
ControlSharedSecret = Annotated[SecretStr, Field(min_length=32)]


class DatabaseSettings(BaseSettings):
    """Validated database settings shared by the application and migrations."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
        validate_default=True,
    )

    database_url: NonEmptySecret = Field(validation_alias="DATABASE_URL")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        raw_url = value.get_secret_value()
        if not raw_url.strip():
            raise ValueError("must not be empty or whitespace")

        try:
            parsed_url = make_url(raw_url)
            _ = parsed_url.port
        except (ArgumentError, TypeError, ValueError) as error:
            raise ValueError("must be a valid SQLAlchemy database URL") from error

        if parsed_url.drivername != "postgresql+asyncpg":
            raise ValueError("drivername must be postgresql+asyncpg")
        return value


class RuntimeSettings(DatabaseSettings):
    """Settings shared by standalone application processes."""

    discord_guild_id: PositiveInt = Field(validation_alias="DISCORD_GUILD_ID")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    voice_min_session_seconds: PositiveInt = Field(
        default=10,
        validation_alias="VOICE_MIN_SESSION_SECONDS",
    )
    voice_checkpoint_interval_seconds: PositiveInt = Field(
        default=60,
        validation_alias="VOICE_CHECKPOINT_INTERVAL_SECONDS",
    )
    report_timezone: Annotated[str, Field(min_length=1)] = Field(
        default="UTC",
        validation_alias="REPORT_TIMEZONE",
    )
    game_tracking_enabled: bool = Field(
        default=False,
        validation_alias="GAME_TRACKING_ENABLED",
    )
    game_confirm_interval_seconds: PositiveInt = Field(
        default=60,
        validation_alias="GAME_CONFIRM_INTERVAL_SECONDS",
    )
    discord_audit_log_channel_id: PositiveInt | None = Field(
        default=None,
        validation_alias="DISCORD_AUDIT_LOG_CHANNEL_ID",
    )
    discord_autorole_id: PositiveInt | None = Field(
        default=None,
        validation_alias="DISCORD_AUTOROLE_ID",
    )
    discord_anniversary_channel_id: PositiveInt | None = Field(
        default=None,
        validation_alias="DISCORD_ANNIVERSARY_CHANNEL_ID",
    )
    discord_return_channel_id: PositiveInt | None = Field(
        default=None,
        validation_alias="DISCORD_RETURN_CHANNEL_ID",
    )
    discord_guest_role_id: PositiveInt | None = Field(
        default=None,
        validation_alias="DISCORD_GUEST_ROLE_ID",
    )
    discord_initiated_role_id: PositiveInt | None = Field(
        default=None,
        validation_alias="DISCORD_INITIATED_ROLE_ID",
    )
    discord_guardian_role_id: PositiveInt | None = Field(
        default=None,
        validation_alias="DISCORD_GUARDIAN_ROLE_ID",
    )
    discord_purple_role_id: PositiveInt | None = Field(
        default=None,
        validation_alias="DISCORD_PURPLE_ROLE_ID",
    )
    discord_gold_role_id: PositiveInt | None = Field(
        default=None,
        validation_alias="DISCORD_GOLD_ROLE_ID",
    )
    rules_accepted_role_id: PositiveInt | None = Field(
        default=None,
        validation_alias="RULES_ACCEPTED_ROLE_ID",
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        allowed_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        if normalized not in allowed_levels:
            msg = f"must be one of: {', '.join(sorted(allowed_levels))}"
            raise ValueError(msg)
        return normalized

    @field_validator("report_timezone")
    @classmethod
    def validate_report_timezone(cls, value: str) -> str:
        timezone_name = value.strip()
        try:
            ZoneInfo(timezone_name)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("must be a valid IANA timezone") from error
        return timezone_name


class WebSettings(RuntimeSettings):
    """Database, OAuth and safe-bind HTTP settings for the standalone web process."""

    web_admin_host: WebAdminHost = Field(
        default="127.0.0.1",
        validation_alias="WEB_ADMIN_HOST",
    )
    web_admin_allow_private_bind: bool = Field(
        default=False,
        validation_alias="WEB_ADMIN_ALLOW_PRIVATE_BIND",
    )
    web_admin_port: TcpPort = Field(
        default=8000,
        validation_alias="WEB_ADMIN_PORT",
    )
    web_admin_discord_client_id: DiscordClientId = Field(
        validation_alias="WEB_ADMIN_DISCORD_CLIENT_ID",
    )
    web_admin_discord_client_secret: NonEmptySecret = Field(
        validation_alias="WEB_ADMIN_DISCORD_CLIENT_SECRET",
    )
    web_admin_discord_redirect_uri: Annotated[str, Field(min_length=1)] = Field(
        validation_alias="WEB_ADMIN_DISCORD_REDIRECT_URI",
    )
    web_admin_cookie_secure: bool = Field(
        default=True,
        validation_alias="WEB_ADMIN_COOKIE_SECURE",
    )
    web_admin_session_lifetime_seconds: WebSessionLifetime = Field(
        default=28_800,
        validation_alias="WEB_ADMIN_SESSION_LIFETIME_SECONDS",
    )
    web_admin_allowed_user_ids: WebAdminAllowedUserIds = Field(
        default_factory=frozenset,
        validation_alias="WEB_ADMIN_ALLOWED_USER_IDS",
    )
    web_admin_bot_control_url: str | None = Field(
        default=None,
        validation_alias="WEB_ADMIN_BOT_CONTROL_URL",
    )
    web_admin_bot_control_shared_secret: ControlSharedSecret | None = Field(
        default=None,
        validation_alias="WEB_ADMIN_BOT_CONTROL_SHARED_SECRET",
    )

    @field_validator("web_admin_allowed_user_ids", mode="before")
    @classmethod
    def parse_web_admin_allowed_user_ids(cls, value: object) -> object:
        if value is None:
            return frozenset()
        if not isinstance(value, str):
            return value

        allowed_user_ids: set[int] = set()
        for raw_item in value.split(","):
            item = raw_item.strip()
            if not item:
                continue
            if re.fullmatch(r"[0-9]+", item) is None:
                raise ValueError(
                    "must contain only comma-separated positive decimal Discord IDs"
                )
            user_id = int(item)
            if user_id <= 0 or user_id > 18_446_744_073_709_551_615:
                raise ValueError(
                    "each Discord ID must be between 1 and 18446744073709551615"
                )
            allowed_user_ids.add(user_id)
        return frozenset(allowed_user_ids)

    @field_validator("web_admin_bot_control_url")
    @classmethod
    def validate_web_bot_control_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        control_url = value.strip()
        try:
            parsed = urlsplit(control_url)
            port = parsed.port
        except ValueError as error:
            raise ValueError("must be a valid loopback HTTP URL") from error
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or not 1 <= port <= 65_535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "must be an http://127.0.0.1:<port> URL without credentials, "
                "query or fragment"
            )
        return f"http://127.0.0.1:{port}"

    @field_validator("web_admin_bot_control_shared_secret")
    @classmethod
    def validate_web_bot_control_secret(
        cls, value: SecretStr | None
    ) -> SecretStr | None:
        return cls._validate_control_secret(value)

    @field_validator("web_admin_host")
    @classmethod
    def validate_web_admin_host(cls, value: str) -> str:
        host = value.strip()
        if not host or "%" in host:
            raise ValueError("must be a literal IP address")
        try:
            address = ip_address(host)
        except ValueError as error:
            raise ValueError("must be a literal IP address") from error
        if address.is_unspecified:
            raise ValueError("must not be a wildcard address")
        if address.is_loopback:
            return address.compressed

        private_ipv4 = IPv4Network("10.0.0.0/8")
        private_ipv4_172 = IPv4Network("172.16.0.0/12")
        private_ipv4_192 = IPv4Network("192.168.0.0/16")
        private_ipv6 = IPv6Network("fc00::/7")
        if address.version == 4:
            is_private_network = any(
                address in network
                for network in (private_ipv4, private_ipv4_172, private_ipv4_192)
            )
        else:
            is_private_network = address in private_ipv6
        if not is_private_network:
            raise ValueError("must be a loopback, RFC1918 or IPv6 ULA address")
        return address.compressed

    @field_validator("web_admin_discord_client_secret")
    @classmethod
    def validate_web_client_secret(cls, value: SecretStr) -> SecretStr:
        raw_secret = value.get_secret_value()
        if not raw_secret.strip():
            raise ValueError("must not be empty or whitespace")
        if raw_secret != raw_secret.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in raw_secret
        ):
            raise ValueError("must not contain surrounding whitespace or controls")
        return value

    @field_validator("web_admin_discord_redirect_uri")
    @classmethod
    def validate_web_redirect_uri(cls, value: str) -> str:
        redirect_uri = value.strip()
        try:
            parsed = urlsplit(redirect_uri)
            port = parsed.port
        except ValueError as error:
            raise ValueError("must be a valid HTTP(S) URL") from error

        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("must not contain query or fragment")
        if parsed.path != "/admin/auth/discord/callback":
            raise ValueError("path must be exactly /admin/auth/discord/callback")
        if port is not None and not 1 <= port <= 65_535:
            raise ValueError("port must be between 1 and 65535")
        return redirect_uri

    @model_validator(mode="after")
    def validate_cookie_transport(self) -> "WebSettings":
        address = ip_address(self.web_admin_host)
        if not address.is_loopback and not self.web_admin_allow_private_bind:
            raise ValueError(
                "WEB_ADMIN_ALLOW_PRIVATE_BIND=true is required for a private "
                "non-loopback WEB_ADMIN_HOST"
            )
        parsed = urlsplit(self.web_admin_discord_redirect_uri)
        if self.web_admin_cookie_secure:
            if parsed.scheme != "https":
                raise ValueError(
                    "WEB_ADMIN_COOKIE_SECURE=true requires an HTTPS redirect URI"
                )
            return self._validate_bot_control_configuration()

        if parsed.scheme != "http":
            raise ValueError(
                "WEB_ADMIN_COOKIE_SECURE=false is only allowed for loopback HTTP"
            )
        if (parsed.hostname or "").lower() not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError(
                "WEB_ADMIN_COOKIE_SECURE=false requires a loopback redirect host"
            )
        if not address.is_loopback:
            raise ValueError(
                "WEB_ADMIN_COOKIE_SECURE=false requires a loopback WEB_ADMIN_HOST"
            )
        return self._validate_bot_control_configuration()

    def _validate_bot_control_configuration(self) -> "WebSettings":
        if (self.web_admin_bot_control_url is None) != (
            self.web_admin_bot_control_shared_secret is None
        ):
            raise ValueError(
                "WEB_ADMIN_BOT_CONTROL_URL and "
                "WEB_ADMIN_BOT_CONTROL_SHARED_SECRET must be configured together"
            )
        return self

    @staticmethod
    def _validate_control_secret(value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        raw_secret = value.get_secret_value()
        if raw_secret != raw_secret.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in raw_secret
        ):
            raise ValueError("must not contain surrounding whitespace or controls")
        return value


class Settings(RuntimeSettings):
    """Validated Discord-bot settings loaded from the environment."""

    model_config = SettingsConfigDict(extra="forbid")

    discord_token: NonEmptySecret = Field(validation_alias="DISCORD_TOKEN")
    discord_bot_control_enabled: bool = Field(
        default=False,
        validation_alias="DISCORD_BOT_CONTROL_ENABLED",
    )
    discord_bot_control_host: Literal["127.0.0.1"] = Field(
        default="127.0.0.1",
        validation_alias="DISCORD_BOT_CONTROL_HOST",
    )
    discord_bot_control_port: TcpPort = Field(
        default=8765,
        validation_alias="DISCORD_BOT_CONTROL_PORT",
    )
    discord_bot_control_shared_secret: ControlSharedSecret | None = Field(
        default=None,
        validation_alias="DISCORD_BOT_CONTROL_SHARED_SECRET",
    )
    member_return_min_absence_seconds: PositiveInt = Field(
        default=86_400,
        validation_alias="MEMBER_RETURN_MIN_ABSENCE_SECONDS",
    )
    raw_message_retention_days: PositiveInt = Field(
        default=90,
        validation_alias="RAW_MESSAGE_RETENTION_DAYS",
    )
    server_event_retention_days: PositiveInt = Field(
        default=365,
        validation_alias="SERVER_EVENT_RETENTION_DAYS",
    )
    audit_transient_retention_days: PositiveInt = Field(
        default=90,
        validation_alias="AUDIT_TRANSIENT_RETENTION_DAYS",
    )

    @field_validator("discord_token")
    @classmethod
    def validate_discord_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty or whitespace")
        return value

    @field_validator("discord_bot_control_shared_secret")
    @classmethod
    def validate_bot_control_secret(cls, value: SecretStr | None) -> SecretStr | None:
        return WebSettings._validate_control_secret(value)

    @model_validator(mode="after")
    def validate_bot_control_configuration(self) -> "Settings":
        if (
            self.discord_bot_control_enabled
            and self.discord_bot_control_shared_secret is None
        ):
            raise ValueError(
                "DISCORD_BOT_CONTROL_SHARED_SECRET is required when "
                "DISCORD_BOT_CONTROL_ENABLED=true"
            )
        return self
