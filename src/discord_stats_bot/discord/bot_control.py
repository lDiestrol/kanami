"""Narrow authenticated loopback control API owned by the Discord process."""

import asyncio
import json
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Protocol

import discord
import uvicorn
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from discord_stats_bot.config import MAX_DISCORD_SNOWFLAKE
from discord_stats_bot.discord.server_settings_control import (
    ServerSettingControlError,
    ServerSettingControlErrorCategory,
)
from discord_stats_bot.features.bot_profile import (
    BOT_AVATAR_MAX_BYTES,
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
)
from discord_stats_bot.features.server_settings import (
    GuildServerSettingChange,
    GuildServerSettingKey,
    GuildServerSettingOverride,
    GuildServerSettingOverrideMode,
    ServerSettingsOptions,
)

logger = logging.getLogger(__name__)
CONTROL_BODY_MAX_BYTES = BOT_AVATAR_MAX_BYTES + 1
CONTROL_JSON_MAX_BYTES = 4_096
ACTOR_HEADER = "x-kanami-actor-discord-user-id"


class BotProfileOperator(Protocol):
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


class WebAdminAccessOperator(Protocol):
    async def grant_access(
        self, user_id: int, *, actor_discord_user_id: int
    ) -> bool: ...

    async def revoke_access(
        self, user_id: int, *, actor_discord_user_id: int
    ) -> bool: ...


class ServerSettingsOperator(Protocol):
    async def change_setting(
        self,
        key: GuildServerSettingKey,
        override: GuildServerSettingOverride,
        *,
        actor_discord_user_id: int,
    ) -> GuildServerSettingChange: ...


class RulesPublicationOperator(Protocol):
    async def sync(self) -> RulesPublicationSyncResult: ...

    async def configure(
        self, channel_id: int, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult: ...

    async def disable(
        self, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult: ...


class ServerSettingsOptionsOperator(Protocol):
    async def get_options(self) -> ServerSettingsOptions: ...


class DiscordBotProfileService:
    """Edit only the configured guild's own bot member through discord.py."""

    def __init__(self, client: discord.Client, *, guild_id: int) -> None:
        self._client = client
        self._guild_id = guild_id
        self._lock = asyncio.Lock()

    async def get_profile(self) -> BotGuildProfile:
        member = self._current_member()
        return self._profile(member)

    async def update_nickname(
        self, nickname: str, *, actor_discord_user_id: int
    ) -> BotGuildProfile:
        normalized = normalize_bot_nickname(nickname)
        return await self._edit(
            "nickname_update",
            actor_discord_user_id,
            nick=normalized,
        )

    async def reset_nickname(self, *, actor_discord_user_id: int) -> BotGuildProfile:
        return await self._edit(
            "nickname_reset",
            actor_discord_user_id,
            nick=None,
        )

    async def update_avatar(
        self,
        avatar: bytes,
        content_type: str,
        *,
        actor_discord_user_id: int,
    ) -> BotGuildProfile:
        validate_bot_avatar(avatar, content_type)
        return await self._edit(
            "avatar_update",
            actor_discord_user_id,
            avatar=avatar,
        )

    async def reset_avatar(self, *, actor_discord_user_id: int) -> BotGuildProfile:
        return await self._edit(
            "avatar_reset",
            actor_discord_user_id,
            avatar=None,
        )

    def _current_member(self) -> discord.Member:
        if not self._client.is_ready() or self._client.user is None:
            raise BotProfileOperationError(BotProfileErrorCategory.BOT_NOT_READY)
        guild = self._client.get_guild(self._guild_id)
        if guild is None:
            raise BotProfileOperationError(BotProfileErrorCategory.GUILD_UNAVAILABLE)
        member = guild.me or guild.get_member(self._client.user.id)
        if member is None or member.id != self._client.user.id:
            raise BotProfileOperationError(BotProfileErrorCategory.GUILD_UNAVAILABLE)
        return member

    async def _edit(
        self,
        operation: str,
        actor_discord_user_id: int,
        **changes: object,
    ) -> BotGuildProfile:
        async with self._lock:
            member = self._current_member()
            try:
                updated = await member.edit(
                    reason=(
                        "Kanami Web Admin bot profile change by Discord user "
                        f"{actor_discord_user_id}"
                    ),
                    **changes,
                )
            except discord.Forbidden as error:
                raise BotProfileOperationError(
                    BotProfileErrorCategory.DISCORD_FORBIDDEN
                ) from error
            except discord.HTTPException as error:
                raise BotProfileOperationError(
                    BotProfileErrorCategory.DISCORD_API_FAILURE
                ) from error
            except ValueError as error:
                raise BotProfileOperationError(
                    BotProfileErrorCategory.INVALID_AVATAR
                ) from error
            profile = self._profile(updated or member)
            logger.info(
                "bot_profile_control_succeeded actor_discord_user_id=%s operation=%s",
                actor_discord_user_id,
                operation,
            )
            return profile

    def _profile(self, member: discord.Member) -> BotGuildProfile:
        user = self._client.user
        if user is None:
            raise BotProfileOperationError(BotProfileErrorCategory.BOT_NOT_READY)
        guild_avatar = getattr(member, "guild_avatar", None)
        display_avatar = getattr(member, "display_avatar", None)
        return BotGuildProfile(
            user_id=user.id,
            application_name=user.name,
            display_name=member.display_name,
            nickname=member.nick,
            guild_avatar_url=str(guild_avatar.url)
            if guild_avatar is not None
            else None,
            display_avatar_url=(
                str(display_avatar.url) if display_avatar is not None else None
            ),
        )


def _authorized(request: Request, shared_secret: SecretStr) -> bool:
    authorization = request.headers.get("authorization", "")
    scheme, separator, received = authorization.partition(" ")
    expected = shared_secret.get_secret_value()
    return (
        separator == " "
        and scheme.casefold() == "bearer"
        and secrets.compare_digest(received, expected)
    )


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > limit:
                raise BotProfileOperationError(BotProfileErrorCategory.INVALID_AVATAR)
        except ValueError as error:
            raise BotProfileOperationError(
                BotProfileErrorCategory.INVALID_AVATAR
            ) from error
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise BotProfileOperationError(BotProfileErrorCategory.INVALID_AVATAR)
    return bytes(body)


def _actor(request: Request) -> int:
    raw_actor = request.headers.get(ACTOR_HEADER, "")
    if not raw_actor.isascii() or not raw_actor.isdecimal():
        raise BotProfileOperationError(BotProfileErrorCategory.CONTROL_UNAUTHORIZED)
    actor = int(raw_actor)
    if not 0 < actor <= MAX_DISCORD_SNOWFLAKE:
        raise BotProfileOperationError(BotProfileErrorCategory.CONTROL_UNAUTHORIZED)
    return actor


def create_bot_control_app(
    operator: BotProfileOperator,
    *,
    shared_secret: SecretStr,
    web_admin_access_operator: WebAdminAccessOperator | None = None,
    server_settings_operator: ServerSettingsOperator | None = None,
    server_settings_options_operator: ServerSettingsOptionsOperator | None = None,
    rules_publication_operator: RulesPublicationOperator | None = None,
) -> Starlette:
    """Create an API exposing only fixed bot-profile operations."""

    async def execute(
        request: Request,
        action: Callable[[], Awaitable[BotGuildProfile]],
        *,
        operation: str,
        actor: int | None,
    ) -> Response:
        if not _authorized(request, shared_secret):
            logger.warning("bot_profile_control_denied category=unauthorized")
            return JSONResponse(
                {"error": BotProfileErrorCategory.CONTROL_UNAUTHORIZED.value},
                status_code=401,
            )
        try:
            profile = await action()
        except BotProfileOperationError as error:
            logger.warning(
                "bot_profile_control_failed actor_discord_user_id=%s "
                "operation=%s category=%s",
                actor,
                operation,
                error.category,
            )
            status = (
                400
                if error.category
                in {
                    BotProfileErrorCategory.INVALID_NICKNAME,
                    BotProfileErrorCategory.INVALID_AVATAR,
                }
                else 503
            )
            return JSONResponse({"error": error.category.value}, status_code=status)
        return JSONResponse({"profile": profile.to_dict()})

    async def mutate_web_admin_access(request: Request, operation: str) -> Response:
        if not _authorized(request, shared_secret):
            return JSONResponse({"error": "control_unauthorized"}, status_code=401)
        try:
            actor = _actor(request)
        except BotProfileOperationError:
            return JSONResponse({"error": "control_unauthorized"}, status_code=401)
        if web_admin_access_operator is None:
            return JSONResponse(
                {"error": "access_control_unavailable"}, status_code=503
            )
        try:
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise ValueError
            body = await _read_bounded_body(request, CONTROL_JSON_MAX_BYTES)
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {"user_id"}:
                raise ValueError
            user_id = payload["user_id"]
            if isinstance(user_id, bool) or not isinstance(user_id, int):
                raise ValueError
            if not 0 < user_id <= MAX_DISCORD_SNOWFLAKE:
                raise ValueError
        except (BotProfileOperationError, json.JSONDecodeError, TypeError, ValueError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            if operation == "grant":
                changed = await web_admin_access_operator.grant_access(
                    user_id, actor_discord_user_id=actor
                )
            elif operation == "revoke":
                changed = await web_admin_access_operator.revoke_access(
                    user_id, actor_discord_user_id=actor
                )
            else:
                raise ValueError("unsupported access operation")
        except Exception as error:
            logger.exception(
                "web_admin_access_control_failed actor_discord_user_id=%s operation=%s error_type=%s",
                actor,
                operation,
                type(error).__name__,
            )
            return JSONResponse({"error": "access_control_failure"}, status_code=503)
        logger.info(
            "web_admin_access_control_succeeded actor_discord_user_id=%s operation=%s user_id=%s changed=%s",
            actor,
            operation,
            user_id,
            changed,
        )
        return JSONResponse({"changed": changed, "user_id": user_id})

    async def grant_web_admin_access(request: Request) -> Response:
        return await mutate_web_admin_access(request, "grant")

    async def revoke_web_admin_access(request: Request) -> Response:
        return await mutate_web_admin_access(request, "revoke")

    async def change_server_setting(request: Request) -> Response:
        if not _authorized(request, shared_secret):
            return JSONResponse({"error": "control_unauthorized"}, status_code=401)
        try:
            actor = _actor(request)
        except BotProfileOperationError:
            return JSONResponse({"error": "control_unauthorized"}, status_code=401)
        if server_settings_operator is None:
            return JSONResponse(
                {"error": "server_settings_unavailable"}, status_code=503
            )
        try:
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise ValueError
            body = await _read_bounded_body(request, CONTROL_JSON_MAX_BYTES)
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError
            mode = GuildServerSettingOverrideMode(payload.get("mode"))
            expected_fields = (
                {"setting", "mode", "value"}
                if mode is GuildServerSettingOverrideMode.VALUE
                else {"setting", "mode"}
            )
            if set(payload) != expected_fields:
                raise ValueError
            key = GuildServerSettingKey(payload["setting"])
            value = payload.get("value")
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 < value <= MAX_DISCORD_SNOWFLAKE
            ):
                raise ValueError
            override = GuildServerSettingOverride(mode, value)
        except (
            BotProfileOperationError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        try:
            result = await server_settings_operator.change_setting(
                key,
                override,
                actor_discord_user_id=actor,
            )
        except ServerSettingControlError as error:
            status = (
                400
                if error.category is ServerSettingControlErrorCategory.INVALID_TARGET
                else 503
            )
            return JSONResponse({"error": error.category.value}, status_code=status)
        except Exception as error:
            logger.exception(
                "server_settings_control_failed actor_discord_user_id=%s "
                "setting=%s error_type=%s",
                actor,
                key.value,
                type(error).__name__,
            )
            return JSONResponse({"error": "server_settings_failure"}, status_code=503)
        logger.info(
            "server_settings_control_succeeded actor_discord_user_id=%s "
            "setting=%s mode=%s changed=%s",
            actor,
            key.value,
            override.mode.value,
            result.changed,
        )
        return JSONResponse(
            {
                "changed": result.changed,
                "setting": key.value,
                "mode": override.mode.value,
                "value": override.value,
            }
        )

    async def get_server_settings_options(request: Request) -> Response:
        if not _authorized(request, shared_secret):
            return JSONResponse({"error": "control_unauthorized"}, status_code=401)
        if server_settings_options_operator is None:
            return JSONResponse(
                {"error": "server_settings_unavailable"}, status_code=503
            )
        try:
            options = await server_settings_options_operator.get_options()
        except ServerSettingControlError:
            return JSONResponse({"error": "runtime_unavailable"}, status_code=503)
        except Exception as error:
            logger.exception(
                "server_settings_options_failed error_type=%s",
                type(error).__name__,
            )
            return JSONResponse({"error": "server_settings_failure"}, status_code=503)
        return JSONResponse(
            {
                "roles": [
                    {"id": option.id, "name": option.name} for option in options.roles
                ],
                "channels": [
                    {
                        "id": option.id,
                        "name": option.name,
                        "type": option.type.value,
                    }
                    for option in options.channels
                ],
            }
        )

    async def get_profile(request: Request) -> Response:
        return await execute(
            request,
            operator.get_profile,
            operation="profile_read",
            actor=None,
        )

    async def sync_rules_publication(request: Request) -> Response:
        if not _authorized(request, shared_secret):
            return JSONResponse({"error": "control_unauthorized"}, status_code=401)
        try:
            actor = _actor(request)
        except BotProfileOperationError:
            return JSONResponse({"error": "control_unauthorized"}, status_code=401)
        if rules_publication_operator is None:
            return JSONResponse(
                {"error": "rules_publication_unavailable"}, status_code=503
            )
        try:
            result = await rules_publication_operator.sync()
        except Exception as error:
            logger.exception(
                "rules_publication_control_failed error_type=%s",
                type(error).__name__,
            )
            return JSONResponse({"error": "rules_publication_failure"}, status_code=503)
        logger.info(
            "rules_publication_control_sync actor_discord_user_id=%s status=%s",
            actor,
            result.status.value,
        )
        return JSONResponse(result.to_dict(), status_code=503 if result.failed else 200)

    async def mutate_rules_publication(request: Request, operation: str) -> Response:
        if not _authorized(request, shared_secret):
            return JSONResponse({"error": "control_unauthorized"}, status_code=401)
        try:
            actor = _actor(request)
        except BotProfileOperationError:
            return JSONResponse({"error": "control_unauthorized"}, status_code=401)
        if rules_publication_operator is None:
            return JSONResponse(
                {"error": "rules_publication_unavailable"}, status_code=503
            )
        try:
            if operation == "configure":
                if (
                    request.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                    != "application/json"
                ):
                    raise ValueError
                body = await _read_bounded_body(request, CONTROL_JSON_MAX_BYTES)
                payload = json.loads(body)
                if not isinstance(payload, dict) or set(payload) != {"channel_id"}:
                    raise ValueError
                channel_id = payload["channel_id"]
                if (
                    isinstance(channel_id, bool)
                    or not isinstance(channel_id, int)
                    or not 0 < channel_id <= MAX_DISCORD_SNOWFLAKE
                ):
                    raise ValueError
                result = await rules_publication_operator.configure(
                    channel_id, actor_discord_user_id=actor
                )
            elif operation == "disable":
                result = await rules_publication_operator.disable(
                    actor_discord_user_id=actor
                )
            else:
                raise ValueError
        except (BotProfileOperationError, json.JSONDecodeError, ValueError):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        except Exception as error:
            logger.exception(
                "rules_publication_configuration_control_failed "
                "actor_discord_user_id=%s operation=%s error_type=%s",
                actor,
                operation,
                type(error).__name__,
            )
            return JSONResponse({"error": "rules_publication_failure"}, status_code=503)
        logger.info(
            "rules_publication_configuration_control actor_discord_user_id=%s "
            "operation=%s guild_id=%s previous_channel_id=%s channel_id=%s "
            "previous_message_id=%s status=%s",
            actor,
            operation,
            result.guild_id,
            result.previous_channel_id,
            result.channel_id,
            result.previous_message_id,
            result.status.value,
        )
        status_code = 200
        if result.status is RulesPublicationConfigurationStatus.INVALID_CHANNEL:
            status_code = 400
        elif result.failed:
            status_code = 503
        return JSONResponse(result.to_dict(), status_code=status_code)

    async def configure_rules_publication(request: Request) -> Response:
        return await mutate_rules_publication(request, "configure")

    async def disable_rules_publication(request: Request) -> Response:
        return await mutate_rules_publication(request, "disable")

    async def update_nickname(request: Request) -> Response:
        if not _authorized(request, shared_secret):
            return await execute(
                request,
                operator.get_profile,
                operation="nickname_update",
                actor=None,
            )
        try:
            actor = _actor(request)
        except BotProfileOperationError as error:
            return JSONResponse({"error": error.category.value}, status_code=401)
        try:
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                raise ValueError
            body = await _read_bounded_body(request, CONTROL_JSON_MAX_BYTES)
            payload = json.loads(body)
            if not isinstance(payload, dict) or set(payload) != {"nickname"}:
                raise ValueError
            nickname = normalize_bot_nickname(payload["nickname"])
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            BotProfileOperationError,
        ):
            return JSONResponse(
                {"error": BotProfileErrorCategory.INVALID_NICKNAME.value},
                status_code=400,
            )
        return await execute(
            request,
            lambda: operator.update_nickname(
                nickname,
                actor_discord_user_id=actor,
            ),
            operation="nickname_update",
            actor=actor,
        )

    async def reset_nickname(request: Request) -> Response:
        if not _authorized(request, shared_secret):
            return await execute(
                request, operator.get_profile, operation="nickname_reset", actor=None
            )
        try:
            actor = _actor(request)
        except BotProfileOperationError as error:
            return JSONResponse({"error": error.category.value}, status_code=401)
        return await execute(
            request,
            lambda: operator.reset_nickname(actor_discord_user_id=actor),
            operation="nickname_reset",
            actor=actor,
        )

    async def update_avatar(request: Request) -> Response:
        if not _authorized(request, shared_secret):
            return await execute(
                request, operator.get_profile, operation="avatar_update", actor=None
            )
        try:
            actor = _actor(request)
        except BotProfileOperationError as error:
            return JSONResponse({"error": error.category.value}, status_code=401)
        try:
            avatar = await _read_bounded_body(request, CONTROL_BODY_MAX_BYTES)
            content_type = validate_bot_avatar(
                avatar,
                request.headers.get("content-type", ""),
            )
        except BotProfileOperationError as error:
            return JSONResponse({"error": error.category.value}, status_code=400)
        return await execute(
            request,
            lambda: operator.update_avatar(
                avatar,
                content_type,
                actor_discord_user_id=actor,
            ),
            operation="avatar_update",
            actor=actor,
        )

    async def reset_avatar(request: Request) -> Response:
        if not _authorized(request, shared_secret):
            return await execute(
                request, operator.get_profile, operation="avatar_reset", actor=None
            )
        try:
            actor = _actor(request)
        except BotProfileOperationError as error:
            return JSONResponse({"error": error.category.value}, status_code=401)
        return await execute(
            request,
            lambda: operator.reset_avatar(actor_discord_user_id=actor),
            operation="avatar_reset",
            actor=actor,
        )

    return Starlette(
        debug=False,
        routes=[
            Route("/control/v1/bot-profile", get_profile, methods=["GET"]),
            Route(
                "/control/v1/web-admin/access/grant",
                grant_web_admin_access,
                methods=["POST"],
            ),
            Route(
                "/control/v1/web-admin/access/revoke",
                revoke_web_admin_access,
                methods=["POST"],
            ),
            Route(
                "/control/v1/server-settings",
                change_server_setting,
                methods=["POST"],
            ),
            Route(
                "/control/v1/server-settings/options",
                get_server_settings_options,
                methods=["GET"],
            ),
            Route(
                "/control/v1/rules/publication/sync",
                sync_rules_publication,
                methods=["POST"],
            ),
            Route(
                "/control/v1/rules/publication/configure",
                configure_rules_publication,
                methods=["POST"],
            ),
            Route(
                "/control/v1/rules/publication/disable",
                disable_rules_publication,
                methods=["POST"],
            ),
            Route(
                "/control/v1/bot-profile/nickname",
                update_nickname,
                methods=["POST"],
            ),
            Route(
                "/control/v1/bot-profile/nickname/reset",
                reset_nickname,
                methods=["POST"],
            ),
            Route(
                "/control/v1/bot-profile/avatar",
                update_avatar,
                methods=["POST"],
            ),
            Route(
                "/control/v1/bot-profile/avatar/reset",
                reset_avatar,
                methods=["POST"],
            ),
        ],
    )


class BotControlServer:
    """Own one loopback-only Uvicorn task inside the Discord process."""

    def __init__(self, app: Starlette, *, host: str, port: int) -> None:
        if host != "127.0.0.1":
            raise ValueError("bot control server must bind to 127.0.0.1")
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._server.serve(),
            name="bot-control-server",
        )
        try:
            await asyncio.wait_for(self._wait_until_started(), timeout=5)
        except BaseException:
            task = self._task
            self._task = None
            self._server.should_exit = True
            if task is not None:
                await task
            raise

    async def _wait_until_started(self) -> None:
        assert self._task is not None
        while not self._server.started:
            if self._task.done():
                await self._task
                raise RuntimeError("bot control server stopped during startup")
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        self._server.should_exit = True
        await task
