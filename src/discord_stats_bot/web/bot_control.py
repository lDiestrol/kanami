"""Fixed-purpose Web Admin client for the bot process control interface."""

import asyncio
import json
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic import SecretStr as PydanticSecretStr

from discord_stats_bot.config import MAX_DISCORD_SNOWFLAKE
from discord_stats_bot.features.bot_profile import (
    BotGuildProfile,
    BotProfileErrorCategory,
    BotProfileOperationError,
    normalize_bot_nickname,
    validate_bot_avatar,
)
from discord_stats_bot.features.rules import (
    RulesPublicationConfigurationResult,
    RulesPublicationConfigurationStatus,
    RulesPublicationSyncResult,
    RulesPublicationSyncStatus,
)
from discord_stats_bot.features.server_settings import (
    GuildServerSettingKey,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
    ServerSettingsChannelOption,
    ServerSettingsChannelType,
    ServerSettingsOptions,
    ServerSettingsRoleOption,
)

CONTROL_RESPONSE_MAX_BYTES = 16_384
OPTIONS_RESPONSE_MAX_BYTES = 524_288
ACTOR_HEADER = "X-Kanami-Actor-Discord-User-Id"


class BotProfileControl(Protocol):
    async def get_profile(self) -> BotGuildProfile: ...

    async def update_nickname(
        self, nickname: str, *, actor_discord_user_id: int
    ) -> BotGuildProfile: ...

    async def reset_nickname(
        self, *, actor_discord_user_id: int
    ) -> BotGuildProfile: ...

    async def update_avatar(
        self,
        avatar: bytes,
        content_type: str,
        *,
        actor_discord_user_id: int,
    ) -> BotGuildProfile: ...

    async def reset_avatar(self, *, actor_discord_user_id: int) -> BotGuildProfile: ...


class WebAdminAccessControl(Protocol):
    async def grant_web_admin_access(
        self, user_id: int, *, actor_discord_user_id: int
    ) -> bool: ...

    async def revoke_web_admin_access(
        self, user_id: int, *, actor_discord_user_id: int
    ) -> bool: ...


class ServerSettingsControl(Protocol):
    async def get_server_settings_options(self) -> ServerSettingsOptions: ...

    async def change_server_setting(
        self,
        key: GuildServerSettingKey,
        override: GuildServerSettingOverride,
        *,
        actor_discord_user_id: int,
    ) -> bool: ...


class RulesPublicationControl(Protocol):
    async def sync_rules_publication(
        self, *, actor_discord_user_id: int
    ) -> RulesPublicationSyncResult: ...

    async def configure_rules_publication(
        self, channel_id: int, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult: ...

    async def disable_rules_publication(
        self, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult: ...


class ServerSettingsControlCategory(StrEnum):
    CONTROL_UNAVAILABLE = "control_unavailable"
    CONTROL_UNAUTHORIZED = "control_unauthorized"
    INVALID_TARGET = "invalid_target"
    SERVER_SETTINGS_UNAVAILABLE = "server_settings_unavailable"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"
    FAILURE = "failure"


class ServerSettingsControlError(RuntimeError):
    def __init__(self, category: ServerSettingsControlCategory) -> None:
        super().__init__(category)
        self.category = category


class RulesPublicationControlCategory(StrEnum):
    CONTROL_UNAVAILABLE = "control_unavailable"
    CONTROL_UNAUTHORIZED = "control_unauthorized"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"
    FAILURE = "failure"


class RulesPublicationControlError(RuntimeError):
    def __init__(self, category: RulesPublicationControlCategory) -> None:
        super().__init__(category)
        self.category = category


class WebAdminAccessControlCategory(StrEnum):
    CONTROL_UNAVAILABLE = "control_unavailable"
    CONTROL_UNAUTHORIZED = "control_unauthorized"
    ACCESS_FAILURE = "access_failure"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"


class WebAdminAccessControlError(RuntimeError):
    def __init__(self, category: WebAdminAccessControlCategory) -> None:
        super().__init__(category)
        self.category = category


class DisabledBotProfileControl:
    async def get_profile(self) -> BotGuildProfile:
        self._raise()

    async def update_nickname(
        self, nickname: str, *, actor_discord_user_id: int
    ) -> BotGuildProfile:
        del nickname, actor_discord_user_id
        self._raise()

    async def reset_nickname(self, *, actor_discord_user_id: int) -> BotGuildProfile:
        del actor_discord_user_id
        self._raise()

    async def update_avatar(
        self,
        avatar: bytes,
        content_type: str,
        *,
        actor_discord_user_id: int,
    ) -> BotGuildProfile:
        del avatar, content_type, actor_discord_user_id
        self._raise()

    async def reset_avatar(self, *, actor_discord_user_id: int) -> BotGuildProfile:
        del actor_discord_user_id
        self._raise()

    async def grant_web_admin_access(
        self, user_id: int, *, actor_discord_user_id: int
    ) -> bool:
        del user_id, actor_discord_user_id
        raise WebAdminAccessControlError(
            WebAdminAccessControlCategory.CONTROL_UNAVAILABLE
        )

    async def revoke_web_admin_access(
        self, user_id: int, *, actor_discord_user_id: int
    ) -> bool:
        del user_id, actor_discord_user_id
        raise WebAdminAccessControlError(
            WebAdminAccessControlCategory.CONTROL_UNAVAILABLE
        )

    async def get_server_settings_options(self) -> ServerSettingsOptions:
        raise ServerSettingsControlError(
            ServerSettingsControlCategory.CONTROL_UNAVAILABLE
        )

    async def change_server_setting(
        self,
        key: GuildServerSettingKey,
        override: GuildServerSettingOverride,
        *,
        actor_discord_user_id: int,
    ) -> bool:
        del key, override, actor_discord_user_id
        raise ServerSettingsControlError(
            ServerSettingsControlCategory.CONTROL_UNAVAILABLE
        )

    async def sync_rules_publication(
        self, *, actor_discord_user_id: int
    ) -> RulesPublicationSyncResult:
        del actor_discord_user_id
        raise RulesPublicationControlError(
            RulesPublicationControlCategory.CONTROL_UNAVAILABLE
        )

    async def configure_rules_publication(
        self, channel_id: int, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult:
        del channel_id, actor_discord_user_id
        raise RulesPublicationControlError(
            RulesPublicationControlCategory.CONTROL_UNAVAILABLE
        )

    async def disable_rules_publication(
        self, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult:
        del actor_discord_user_id
        raise RulesPublicationControlError(
            RulesPublicationControlCategory.CONTROL_UNAVAILABLE
        )

    @staticmethod
    def _raise() -> None:
        raise BotProfileOperationError(BotProfileErrorCategory.CONTROL_UNAVAILABLE)


class _ProfilePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    application_name: str
    display_name: str
    nickname: str | None
    guild_avatar_url: str | None
    display_avatar_url: str | None


class _SuccessPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: _ProfilePayload


class _ErrorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: BotProfileErrorCategory


class _AccessSuccessPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    changed: bool
    user_id: int


class _RoleOptionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int
    name: str


class _ChannelOptionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int
    name: str
    type: ServerSettingsChannelType


class _OptionsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    roles: list[_RoleOptionPayload]
    channels: list[_ChannelOptionPayload]


class _SettingChangePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    changed: bool
    setting: GuildServerSettingKey
    mode: GuildServerSettingOverrideMode
    value: int | None


class _RulesPublicationSyncPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: RulesPublicationSyncStatus
    guild_id: int
    channel_id: int | None
    message_id: int | None
    ruleset_id: int | None
    version: str | None


class _RulesPublicationConfigurationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: RulesPublicationConfigurationStatus
    guild_id: int
    previous_channel_id: int | None
    channel_id: int | None
    previous_message_id: int | None
    changed: bool


class AiohttpBotProfileControlClient:
    """Call only hard-coded bot-profile endpoints on one validated base URL."""

    def __init__(
        self,
        http_session: aiohttp.ClientSession,
        *,
        base_url: str,
        shared_secret: PydanticSecretStr,
    ) -> None:
        self._http_session = http_session
        self._base_url = base_url.rstrip("/")
        self._shared_secret = shared_secret

    async def get_profile(self) -> BotGuildProfile:
        return await self._request("GET", "/control/v1/bot-profile")

    async def update_nickname(
        self, nickname: str, *, actor_discord_user_id: int
    ) -> BotGuildProfile:
        normalized = normalize_bot_nickname(nickname)
        return await self._request(
            "POST",
            "/control/v1/bot-profile/nickname",
            actor_discord_user_id=actor_discord_user_id,
            json_payload={"nickname": normalized},
        )

    async def reset_nickname(self, *, actor_discord_user_id: int) -> BotGuildProfile:
        return await self._request(
            "POST",
            "/control/v1/bot-profile/nickname/reset",
            actor_discord_user_id=actor_discord_user_id,
        )

    async def update_avatar(
        self,
        avatar: bytes,
        content_type: str,
        *,
        actor_discord_user_id: int,
    ) -> BotGuildProfile:
        normalized_type = validate_bot_avatar(avatar, content_type)
        return await self._request(
            "POST",
            "/control/v1/bot-profile/avatar",
            actor_discord_user_id=actor_discord_user_id,
            body=avatar,
            content_type=normalized_type,
        )

    async def reset_avatar(self, *, actor_discord_user_id: int) -> BotGuildProfile:
        return await self._request(
            "POST",
            "/control/v1/bot-profile/avatar/reset",
            actor_discord_user_id=actor_discord_user_id,
        )

    async def grant_web_admin_access(
        self, user_id: int, *, actor_discord_user_id: int
    ) -> bool:
        return await self._mutate_web_admin_access(
            "grant",
            user_id,
            actor_discord_user_id=actor_discord_user_id,
        )

    async def revoke_web_admin_access(
        self, user_id: int, *, actor_discord_user_id: int
    ) -> bool:
        return await self._mutate_web_admin_access(
            "revoke",
            user_id,
            actor_discord_user_id=actor_discord_user_id,
        )

    async def get_server_settings_options(self) -> ServerSettingsOptions:
        headers = {"Authorization": f"Bearer {self._shared_secret.get_secret_value()}"}
        try:
            async with self._http_session.request(
                "GET",
                f"{self._base_url}/control/v1/server-settings/options",
                headers=headers,
                allow_redirects=False,
            ) as response:
                body = await response.content.read(OPTIONS_RESPONSE_MAX_BYTES + 1)
                if len(body) > OPTIONS_RESPONSE_MAX_BYTES:
                    raise ServerSettingsControlError(
                        ServerSettingsControlCategory.MALFORMED_RESPONSE
                    )
                if response.status == 401:
                    raise ServerSettingsControlError(
                        ServerSettingsControlCategory.CONTROL_UNAUTHORIZED
                    )
                if response.status != 200:
                    raise ServerSettingsControlError(
                        ServerSettingsControlCategory.SERVER_SETTINGS_UNAVAILABLE
                    )
                return self._parse_server_settings_options(body)
        except asyncio.TimeoutError as error:
            raise ServerSettingsControlError(
                ServerSettingsControlCategory.TIMEOUT
            ) from error
        except aiohttp.ClientError as error:
            raise ServerSettingsControlError(
                ServerSettingsControlCategory.CONTROL_UNAVAILABLE
            ) from error

    async def change_server_setting(
        self,
        key: GuildServerSettingKey,
        override: GuildServerSettingOverride,
        *,
        actor_discord_user_id: int,
    ) -> bool:
        if actor_discord_user_id <= 0:
            raise ValueError("Discord actor ID must be positive")
        payload: dict[str, object] = {
            "setting": key.value,
            "mode": override.mode.value,
        }
        if override.mode is GuildServerSettingOverrideMode.VALUE:
            payload["value"] = override.value
        headers = {
            "Authorization": f"Bearer {self._shared_secret.get_secret_value()}",
            ACTOR_HEADER: str(actor_discord_user_id),
        }
        try:
            async with self._http_session.request(
                "POST",
                f"{self._base_url}/control/v1/server-settings",
                headers=headers,
                json=payload,
                allow_redirects=False,
            ) as response:
                body = await response.content.read(CONTROL_RESPONSE_MAX_BYTES + 1)
                if len(body) > CONTROL_RESPONSE_MAX_BYTES:
                    raise ServerSettingsControlError(
                        ServerSettingsControlCategory.MALFORMED_RESPONSE
                    )
                return self._parse_setting_change_response(
                    response.status,
                    body,
                    key,
                    override,
                )
        except asyncio.TimeoutError as error:
            raise ServerSettingsControlError(
                ServerSettingsControlCategory.TIMEOUT
            ) from error
        except aiohttp.ClientError as error:
            raise ServerSettingsControlError(
                ServerSettingsControlCategory.CONTROL_UNAVAILABLE
            ) from error

    async def sync_rules_publication(
        self, *, actor_discord_user_id: int
    ) -> RulesPublicationSyncResult:
        status, body = await self._rules_publication_request(
            "/control/v1/rules/publication/sync",
            actor_discord_user_id=actor_discord_user_id,
        )
        if status == 401:
            raise RulesPublicationControlError(
                RulesPublicationControlCategory.CONTROL_UNAUTHORIZED
            )
        try:
            payload = _RulesPublicationSyncPayload.model_validate_json(body)
            self._validate_publication_ids(
                payload.guild_id,
                payload.channel_id,
                payload.message_id,
                payload.ruleset_id,
            )
        except (ValidationError, ValueError) as error:
            raise RulesPublicationControlError(
                RulesPublicationControlCategory.MALFORMED_RESPONSE
            ) from error
        return RulesPublicationSyncResult(
            payload.status,
            payload.guild_id,
            payload.channel_id,
            payload.message_id,
            payload.ruleset_id,
            payload.version,
        )

    async def configure_rules_publication(
        self, channel_id: int, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult:
        if not 0 < channel_id <= MAX_DISCORD_SNOWFLAKE:
            raise ValueError("Discord channel ID must be positive")
        return await self._change_rules_publication(
            "/control/v1/rules/publication/configure",
            actor_discord_user_id=actor_discord_user_id,
            json_payload={"channel_id": channel_id},
        )

    async def disable_rules_publication(
        self, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult:
        return await self._change_rules_publication(
            "/control/v1/rules/publication/disable",
            actor_discord_user_id=actor_discord_user_id,
        )

    async def _change_rules_publication(
        self,
        path: str,
        *,
        actor_discord_user_id: int,
        json_payload: dict[str, object] | None = None,
    ) -> RulesPublicationConfigurationResult:
        status, body = await self._rules_publication_request(
            path,
            actor_discord_user_id=actor_discord_user_id,
            json_payload=json_payload,
        )
        if status == 401:
            raise RulesPublicationControlError(
                RulesPublicationControlCategory.CONTROL_UNAUTHORIZED
            )
        try:
            payload = _RulesPublicationConfigurationPayload.model_validate_json(body)
            self._validate_publication_ids(
                payload.guild_id,
                payload.previous_channel_id,
                payload.channel_id,
                payload.previous_message_id,
            )
            result = RulesPublicationConfigurationResult(
                payload.status,
                payload.guild_id,
                payload.previous_channel_id,
                payload.channel_id,
                payload.previous_message_id,
            )
            if result.changed != payload.changed:
                raise ValueError("inconsistent publication change response")
        except (ValidationError, ValueError) as error:
            raise RulesPublicationControlError(
                RulesPublicationControlCategory.MALFORMED_RESPONSE
            ) from error
        return result

    async def _rules_publication_request(
        self,
        path: str,
        *,
        actor_discord_user_id: int,
        json_payload: dict[str, object] | None = None,
    ) -> tuple[int, bytes]:
        if actor_discord_user_id <= 0:
            raise ValueError("Discord actor ID must be positive")
        headers = {
            "Authorization": f"Bearer {self._shared_secret.get_secret_value()}",
            ACTOR_HEADER: str(actor_discord_user_id),
        }
        try:
            async with self._http_session.request(
                "POST",
                f"{self._base_url}{path}",
                headers=headers,
                json=json_payload,
                allow_redirects=False,
            ) as response:
                body = await response.content.read(CONTROL_RESPONSE_MAX_BYTES + 1)
                if len(body) > CONTROL_RESPONSE_MAX_BYTES:
                    raise RulesPublicationControlError(
                        RulesPublicationControlCategory.MALFORMED_RESPONSE
                    )
                return response.status, body
        except asyncio.TimeoutError as error:
            raise RulesPublicationControlError(
                RulesPublicationControlCategory.TIMEOUT
            ) from error
        except aiohttp.ClientError as error:
            raise RulesPublicationControlError(
                RulesPublicationControlCategory.CONTROL_UNAVAILABLE
            ) from error

    @staticmethod
    def _validate_publication_ids(guild_id: int, *values: int | None) -> None:
        if not 0 < guild_id <= MAX_DISCORD_SNOWFLAKE or any(
            value is not None and not 0 < value <= MAX_DISCORD_SNOWFLAKE
            for value in values
        ):
            raise ValueError("invalid publication identifiers")

    @staticmethod
    def _parse_server_settings_options(body: bytes) -> ServerSettingsOptions:
        try:
            payload = _OptionsPayload.model_validate_json(body)
            roles = tuple(
                ServerSettingsRoleOption(option.id, option.name)
                for option in payload.roles
                if 0 < option.id <= MAX_DISCORD_SNOWFLAKE
                and 0 < len(option.name) <= 100
            )
            channels = tuple(
                ServerSettingsChannelOption(option.id, option.name, option.type)
                for option in payload.channels
                if 0 < option.id <= MAX_DISCORD_SNOWFLAKE
                and 0 < len(option.name) <= 100
            )
            if (
                len(roles) != len(payload.roles)
                or len(channels) != len(payload.channels)
                or len(roles) > 250
                or len(channels) > 500
            ):
                raise ValueError("invalid options bounds")
        except (ValidationError, ValueError) as error:
            raise ServerSettingsControlError(
                ServerSettingsControlCategory.MALFORMED_RESPONSE
            ) from error
        return ServerSettingsOptions(roles, channels)

    @staticmethod
    def _parse_setting_change_response(
        status: int,
        body: bytes,
        key: GuildServerSettingKey,
        override: GuildServerSettingOverride,
    ) -> bool:
        if status == 401:
            raise ServerSettingsControlError(
                ServerSettingsControlCategory.CONTROL_UNAUTHORIZED
            )
        if status == 400:
            try:
                error = json.loads(body).get("error")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                error = None
            category = (
                ServerSettingsControlCategory.INVALID_TARGET
                if error == "invalid_target"
                else ServerSettingsControlCategory.FAILURE
            )
            raise ServerSettingsControlError(category)
        if status != 200:
            raise ServerSettingsControlError(
                ServerSettingsControlCategory.SERVER_SETTINGS_UNAVAILABLE
            )
        try:
            payload = _SettingChangePayload.model_validate_json(body)
            expected_value = (
                override.value
                if override.mode is GuildServerSettingOverrideMode.VALUE
                else None
            )
            if (
                payload.setting is not key
                or payload.mode is not override.mode
                or payload.value != expected_value
            ):
                raise ValueError("unexpected setting response")
        except (ValidationError, ValueError) as error:
            raise ServerSettingsControlError(
                ServerSettingsControlCategory.MALFORMED_RESPONSE
            ) from error
        return payload.changed

    async def _mutate_web_admin_access(
        self,
        operation: str,
        user_id: int,
        *,
        actor_discord_user_id: int,
    ) -> bool:
        if user_id <= 0 or actor_discord_user_id <= 0:
            raise ValueError("Discord user IDs must be positive")
        headers = {
            "Authorization": f"Bearer {self._shared_secret.get_secret_value()}",
            ACTOR_HEADER: str(actor_discord_user_id),
        }
        try:
            async with self._http_session.request(
                "POST",
                f"{self._base_url}/control/v1/web-admin/access/{operation}",
                headers=headers,
                json={"user_id": user_id},
                allow_redirects=False,
            ) as response:
                body = await response.content.read(CONTROL_RESPONSE_MAX_BYTES + 1)
                if len(body) > CONTROL_RESPONSE_MAX_BYTES:
                    raise WebAdminAccessControlError(
                        WebAdminAccessControlCategory.MALFORMED_RESPONSE
                    )
                return self._parse_access_response(response.status, body, user_id)
        except asyncio.TimeoutError as error:
            raise WebAdminAccessControlError(
                WebAdminAccessControlCategory.TIMEOUT
            ) from error
        except aiohttp.ClientError as error:
            raise WebAdminAccessControlError(
                WebAdminAccessControlCategory.CONTROL_UNAVAILABLE
            ) from error

    @staticmethod
    def _parse_access_response(status: int, body: bytes, user_id: int) -> bool:
        if status == 401:
            raise WebAdminAccessControlError(
                WebAdminAccessControlCategory.CONTROL_UNAUTHORIZED
            )
        if status != 200:
            raise WebAdminAccessControlError(
                WebAdminAccessControlCategory.ACCESS_FAILURE
            )
        try:
            payload = _AccessSuccessPayload.model_validate_json(body)
            if payload.user_id != user_id or payload.user_id <= 0:
                raise ValueError("unexpected user ID")
        except (ValidationError, ValueError) as error:
            raise WebAdminAccessControlError(
                WebAdminAccessControlCategory.MALFORMED_RESPONSE
            ) from error
        return payload.changed

    async def _request(
        self,
        method: str,
        path: str,
        *,
        actor_discord_user_id: int | None = None,
        json_payload: dict[str, object] | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> BotGuildProfile:
        headers = {
            "Authorization": f"Bearer {self._shared_secret.get_secret_value()}",
        }
        if actor_discord_user_id is not None:
            headers[ACTOR_HEADER] = str(actor_discord_user_id)
        if content_type is not None:
            headers["Content-Type"] = content_type
        try:
            async with self._http_session.request(
                method,
                f"{self._base_url}{path}",
                headers=headers,
                json=json_payload,
                data=body,
                allow_redirects=False,
            ) as response:
                response_body = await response.content.read(
                    CONTROL_RESPONSE_MAX_BYTES + 1
                )
                if len(response_body) > CONTROL_RESPONSE_MAX_BYTES:
                    raise BotProfileOperationError(
                        BotProfileErrorCategory.MALFORMED_RESPONSE
                    )
                return self._parse_response(response.status, response_body)
        except asyncio.TimeoutError as error:
            raise BotProfileOperationError(BotProfileErrorCategory.TIMEOUT) from error
        except aiohttp.ClientError as error:
            raise BotProfileOperationError(
                BotProfileErrorCategory.CONTROL_UNAVAILABLE
            ) from error

    @staticmethod
    def _parse_response(status: int, body: bytes) -> BotGuildProfile:
        try:
            payload = json.loads(body)
            if status == 200:
                profile = _SuccessPayload.model_validate(payload).profile
                if profile.user_id <= 0:
                    raise ValueError("invalid bot user ID")
                for asset_url in (
                    profile.guild_avatar_url,
                    profile.display_avatar_url,
                ):
                    if asset_url is None:
                        continue
                    parsed_url = urlsplit(asset_url)
                    if (
                        len(asset_url) > 2_048
                        or parsed_url.scheme != "https"
                        or parsed_url.hostname
                        not in {"cdn.discordapp.com", "media.discordapp.net"}
                        or parsed_url.username is not None
                        or parsed_url.password is not None
                    ):
                        raise ValueError("invalid Discord asset URL")
                return BotGuildProfile(**profile.model_dump())
            error = _ErrorPayload.model_validate(payload).error
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            RecursionError,
        ) as parse_error:
            raise BotProfileOperationError(
                BotProfileErrorCategory.MALFORMED_RESPONSE
            ) from parse_error
        if status == 401:
            raise BotProfileOperationError(BotProfileErrorCategory.CONTROL_UNAUTHORIZED)
        raise BotProfileOperationError(error)
