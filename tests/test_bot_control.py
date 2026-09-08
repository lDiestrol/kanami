import asyncio
import json
from types import SimpleNamespace

import aiohttp
import discord
import pytest
from pydantic import SecretStr, ValidationError
from starlette.testclient import TestClient

import discord_stats_bot.main as main_module
from discord_stats_bot.config import MAX_DISCORD_SNOWFLAKE, Settings, WebSettings
from discord_stats_bot.discord.bot_control import (
    ACTOR_HEADER,
    DiscordBotProfileService,
    create_bot_control_app,
)
from discord_stats_bot.discord.capability_presentation import (
    CapabilityPresentationRuntimeUnavailableError,
)
from discord_stats_bot.features.bot_profile import (
    PNG_CONTENT_TYPE,
    PNG_SIGNATURE,
    BotGuildProfile,
    BotProfileErrorCategory,
    BotProfileOperationError,
)
from discord_stats_bot.features.capabilities import CapabilityPresentationSubjects
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
from discord_stats_bot.web.bot_control import (
    OPTIONS_RESPONSE_MAX_BYTES,
    AiohttpBotProfileControlClient,
    CapabilityPresentationControlError,
    ServerSettingsControlCategory,
    ServerSettingsControlError,
)

DATABASE_URL = "postgresql+asyncpg://test:test@localhost:5432/test"
SHARED_SECRET = "control-secret-value-that-is-at-least-32-characters"


def make_bot_settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL=DATABASE_URL,
        DISCORD_GUILD_ID=10,
        DISCORD_TOKEN="bot-token-placeholder",
        **overrides,
    )


def make_web_settings(**overrides: object) -> WebSettings:
    return WebSettings(
        _env_file=None,
        DATABASE_URL=DATABASE_URL,
        DISCORD_GUILD_ID=10,
        WEB_ADMIN_DISCORD_CLIENT_ID=123,
        WEB_ADMIN_DISCORD_CLIENT_SECRET="oauth-secret",
        WEB_ADMIN_DISCORD_REDIRECT_URI=(
            "http://localhost:8000/admin/auth/discord/callback"
        ),
        WEB_ADMIN_COOKIE_SECURE=False,
        **overrides,
    )


def test_bot_control_config_is_disabled_and_loopback_by_default() -> None:
    settings = make_bot_settings()

    assert settings.discord_bot_control_enabled is False
    assert settings.discord_bot_control_host == "127.0.0.1"
    assert settings.discord_bot_control_port == 8765
    assert settings.discord_bot_control_shared_secret is None


def test_bot_control_config_rejects_non_loopback_missing_secret_and_bad_port() -> None:
    with pytest.raises(ValidationError):
        make_bot_settings(DISCORD_BOT_CONTROL_HOST="0.0.0.0")
    with pytest.raises(ValidationError, match="SHARED_SECRET"):
        make_bot_settings(DISCORD_BOT_CONTROL_ENABLED=True)
    with pytest.raises(ValidationError):
        make_bot_settings(DISCORD_BOT_CONTROL_PORT=0)


def test_web_control_config_requires_exact_loopback_url_and_secret_pair() -> None:
    configured = make_web_settings(
        WEB_ADMIN_BOT_CONTROL_URL="http://127.0.0.1:8765/",
        WEB_ADMIN_BOT_CONTROL_SHARED_SECRET=SHARED_SECRET,
    )
    assert configured.web_admin_bot_control_url == "http://127.0.0.1:8765"

    for invalid_url in (
        "http://0.0.0.0:8765",
        "https://127.0.0.1:8765",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:99999",
        "http://user:pass@127.0.0.1:8765",
        "http://127.0.0.1:8765/control",
    ):
        with pytest.raises(ValidationError):
            make_web_settings(
                WEB_ADMIN_BOT_CONTROL_URL=invalid_url,
                WEB_ADMIN_BOT_CONTROL_SHARED_SECRET=SHARED_SECRET,
            )
    with pytest.raises(ValidationError, match="configured together"):
        make_web_settings(WEB_ADMIN_BOT_CONTROL_URL="http://127.0.0.1:8765")


def test_control_secrets_are_masked_in_settings_repr_and_errors() -> None:
    bot = make_bot_settings(
        DISCORD_BOT_CONTROL_ENABLED=True,
        DISCORD_BOT_CONTROL_SHARED_SECRET=SHARED_SECRET,
    )
    web = make_web_settings(
        WEB_ADMIN_BOT_CONTROL_URL="http://127.0.0.1:8765",
        WEB_ADMIN_BOT_CONTROL_SHARED_SECRET=SHARED_SECRET,
    )

    assert SHARED_SECRET not in repr(bot)
    assert SHARED_SECRET not in repr(web)
    assert "**********" in repr(bot.discord_bot_control_shared_secret)


class FakeOperator:
    def __init__(self) -> None:
        self.profile = BotGuildProfile(
            user_id=99,
            application_name="Kanami",
            display_name="Kanami Server",
            nickname="Kanami Server",
            guild_avatar_url="https://cdn.discordapp.com/guild-avatar.png",
            display_avatar_url="https://cdn.discordapp.com/guild-avatar.png",
        )
        self.calls: list[tuple[object, ...]] = []
        self.error: BotProfileOperationError | None = None

    async def get_profile(self) -> BotGuildProfile:
        if self.error:
            raise self.error
        self.calls.append(("get",))
        return self.profile

    async def update_nickname(
        self, nickname: str, *, actor_discord_user_id: int
    ) -> BotGuildProfile:
        self.calls.append(("nickname", nickname, actor_discord_user_id))
        return self.profile

    async def reset_nickname(self, *, actor_discord_user_id: int) -> BotGuildProfile:
        self.calls.append(("nickname_reset", actor_discord_user_id))
        return self.profile

    async def update_avatar(
        self,
        avatar: bytes,
        content_type: str,
        *,
        actor_discord_user_id: int,
    ) -> BotGuildProfile:
        self.calls.append(("avatar", avatar, content_type, actor_discord_user_id))
        return self.profile

    async def reset_avatar(self, *, actor_discord_user_id: int) -> BotGuildProfile:
        self.calls.append(("avatar_reset", actor_discord_user_id))
        return self.profile


def control_headers(secret: str = SHARED_SECRET) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {secret}",
        ACTOR_HEADER: "42",
    }


def test_control_api_denies_missing_wrong_auth_and_has_no_generic_proxy() -> None:
    operator = FakeOperator()
    app = create_bot_control_app(operator, shared_secret=SecretStr(SHARED_SECRET))
    with TestClient(app) as client:
        assert client.get("/control/v1/bot-profile").status_code == 401
        assert (
            client.get(
                "/control/v1/bot-profile",
                headers={"Authorization": "Bearer wrong"},
            ).status_code
            == 401
        )
        assert (
            client.get(
                "/control/v1/discord-proxy/guilds/10",
                headers=control_headers(),
            ).status_code
            == 404
        )
        profile = client.get(
            "/control/v1/bot-profile",
            headers=control_headers(),
        )

    assert profile.status_code == 200
    assert profile.json()["profile"]["user_id"] == 99


class FakeRulesPublicationOperator:
    def __init__(self) -> None:
        self.calls = 0
        self.configuration_calls: list[tuple[object, ...]] = []

    async def sync(self) -> RulesPublicationSyncResult:
        self.calls += 1
        return RulesPublicationSyncResult(
            RulesPublicationSyncStatus.CREATED,
            guild_id=10,
            channel_id=20,
            message_id=30,
            ruleset_id=40,
            version="1.0",
        )

    async def configure(
        self, channel_id: int, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult:
        self.configuration_calls.append(
            ("configure", channel_id, actor_discord_user_id)
        )
        return RulesPublicationConfigurationResult(
            RulesPublicationConfigurationStatus.CONFIGURED,
            10,
            None,
            channel_id,
            None,
        )

    async def disable(
        self, *, actor_discord_user_id: int
    ) -> RulesPublicationConfigurationResult:
        self.configuration_calls.append(("disable", actor_discord_user_id))
        return RulesPublicationConfigurationResult(
            RulesPublicationConfigurationStatus.DISABLED,
            10,
            20,
            None,
            30,
        )


def test_rules_publication_sync_endpoint_is_authenticated_and_structured() -> None:
    publication = FakeRulesPublicationOperator()
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        rules_publication_operator=publication,
    )
    with TestClient(app) as client:
        denied = client.post("/control/v1/rules/publication/sync")
        response = client.post(
            "/control/v1/rules/publication/sync", headers=control_headers()
        )

    assert denied.status_code == 401
    assert response.status_code == 200
    assert response.json() == {
        "status": "created",
        "guild_id": 10,
        "channel_id": 20,
        "message_id": 30,
        "ruleset_id": 40,
        "version": "1.0",
    }
    assert publication.calls == 1


def test_rules_publication_configuration_endpoints_are_fixed_and_authenticated() -> (
    None
):
    publication = FakeRulesPublicationOperator()
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        rules_publication_operator=publication,
    )
    with TestClient(app) as client:
        denied = client.post(
            "/control/v1/rules/publication/configure", json={"channel_id": 20}
        )
        configured = client.post(
            "/control/v1/rules/publication/configure",
            headers=control_headers(),
            json={"channel_id": 20},
        )
        disabled = client.post(
            "/control/v1/rules/publication/disable", headers=control_headers()
        )

    assert denied.status_code == 401
    assert configured.status_code == 200
    assert configured.json()["status"] == "configured"
    assert configured.json()["changed"] is True
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert publication.configuration_calls == [
        ("configure", 20, 42),
        ("disable", 42),
    ]


def test_control_api_allows_only_fixed_profile_operations_and_ignores_browser_target() -> (
    None
):
    operator = FakeOperator()
    app = create_bot_control_app(operator, shared_secret=SecretStr(SHARED_SECRET))
    png = PNG_SIGNATURE + b"safe-image"
    with TestClient(app) as client:
        nickname = client.post(
            "/control/v1/bot-profile/nickname",
            headers=control_headers(),
            json={"nickname": "  New Name  "},
        )
        supplied_guild = client.post(
            "/control/v1/bot-profile/nickname",
            headers=control_headers(),
            json={"nickname": "Wrong", "guild_id": 999},
        )
        nickname_reset = client.post(
            "/control/v1/bot-profile/nickname/reset",
            headers=control_headers(),
        )
        avatar = client.post(
            "/control/v1/bot-profile/avatar",
            headers={**control_headers(), "Content-Type": PNG_CONTENT_TYPE},
            content=png,
        )
        avatar_reset = client.post(
            "/control/v1/bot-profile/avatar/reset",
            headers=control_headers(),
        )

    assert nickname.status_code == 200
    assert supplied_guild.status_code == 400
    assert nickname_reset.status_code == 200
    assert avatar.status_code == 200
    assert avatar_reset.status_code == 200
    assert operator.calls == [
        ("nickname", "New Name", 42),
        ("nickname_reset", 42),
        ("avatar", png, PNG_CONTENT_TYPE, 42),
        ("avatar_reset", 42),
    ]


def test_control_api_returns_controlled_bot_not_ready_error() -> None:
    operator = FakeOperator()
    operator.error = BotProfileOperationError(BotProfileErrorCategory.BOT_NOT_READY)
    app = create_bot_control_app(operator, shared_secret=SecretStr(SHARED_SECRET))
    with TestClient(app) as client:
        response = client.get(
            "/control/v1/bot-profile",
            headers=control_headers(),
        )

    assert response.status_code == 503
    assert response.json() == {"error": "bot_not_ready"}


@pytest.mark.parametrize(
    "actor",
    ["", "abc", "-1", "0", str(MAX_DISCORD_SNOWFLAKE + 1)],
)
def test_control_api_rejects_invalid_actor(actor: str) -> None:
    operator = FakeOperator()
    app = create_bot_control_app(operator, shared_secret=SecretStr(SHARED_SECRET))
    with TestClient(app) as client:
        invalid_actor = client.post(
            "/control/v1/bot-profile/nickname/reset",
            headers={
                "Authorization": f"Bearer {SHARED_SECRET}",
                ACTOR_HEADER: actor,
            },
        )

    assert invalid_actor.status_code == 401
    assert invalid_actor.json() == {"error": "control_unauthorized"}
    assert operator.calls == []


def test_control_api_rejects_fake_avatar() -> None:
    operator = FakeOperator()
    app = create_bot_control_app(operator, shared_secret=SecretStr(SHARED_SECRET))
    with TestClient(app) as client:
        fake_avatar = client.post(
            "/control/v1/bot-profile/avatar",
            headers={**control_headers(), "Content-Type": PNG_CONTENT_TYPE},
            content=b"fake-png",
        )

    assert fake_avatar.status_code == 400
    assert operator.calls == []


class FakeMember:
    def __init__(self, error: Exception | None = None) -> None:
        self.id = 99
        self.nick = "Old"
        self.display_name = "Old"
        self.guild_avatar = None
        self.display_avatar = SimpleNamespace(url="https://cdn.discordapp.com/a.png")
        self.error = error
        self.edits: list[dict[str, object]] = []

    async def edit(self, **kwargs: object) -> "FakeMember":
        self.edits.append(kwargs)
        if self.error is not None:
            raise self.error
        if "nick" in kwargs:
            self.nick = kwargs["nick"]  # type: ignore[assignment]
            self.display_name = self.nick or "Kanami"
        if isinstance(kwargs.get("avatar"), bytes):
            self.guild_avatar = SimpleNamespace(
                url="https://cdn.discordapp.com/new.png"
            )
            self.display_avatar = self.guild_avatar
        if kwargs.get("avatar", object()) is None:
            self.guild_avatar = None
        return self


class FakeDiscordClient:
    def __init__(
        self,
        member: FakeMember,
        *,
        ready: bool = True,
        guild_available: bool = True,
    ) -> None:
        self.user = SimpleNamespace(id=99, name="Kanami")
        self.guild = SimpleNamespace(
            me=member,
            get_member=lambda user_id: member if user_id == 99 else None,
        )
        self.ready = ready
        self.guild_available = guild_available

    def is_ready(self) -> bool:
        return self.ready

    def get_guild(self, guild_id: int) -> object | None:
        return self.guild if self.guild_available and guild_id == 10 else None


@pytest.mark.asyncio
async def test_discord_profile_service_edits_own_configured_guild_member() -> None:
    member = FakeMember()
    service = DiscordBotProfileService(
        FakeDiscordClient(member),  # type: ignore[arg-type]
        guild_id=10,
    )
    png = PNG_SIGNATURE + b"image"

    await service.update_nickname(" New ", actor_discord_user_id=42)
    await service.reset_nickname(actor_discord_user_id=42)
    await service.update_avatar(png, PNG_CONTENT_TYPE, actor_discord_user_id=42)
    await service.reset_avatar(actor_discord_user_id=42)

    assert member.edits[0]["nick"] == "New"
    assert member.edits[1]["nick"] is None
    assert member.edits[2]["avatar"] == png
    assert member.edits[3]["avatar"] is None
    assert all("Discord user 42" in str(edit["reason"]) for edit in member.edits)


@pytest.mark.asyncio
async def test_discord_profile_service_maps_not_ready_and_discord_api_failure() -> None:
    not_ready = DiscordBotProfileService(
        FakeDiscordClient(FakeMember(), ready=False),  # type: ignore[arg-type]
        guild_id=10,
    )
    response = SimpleNamespace(status=500, reason="Failure", headers={})
    failing = DiscordBotProfileService(
        FakeDiscordClient(  # type: ignore[arg-type]
            FakeMember(discord.HTTPException(response, "sensitive response"))
        ),
        guild_id=10,
    )

    with pytest.raises(BotProfileOperationError) as not_ready_error:
        await not_ready.get_profile()
    with pytest.raises(BotProfileOperationError) as api_error:
        await failing.update_nickname("Name", actor_discord_user_id=42)

    assert not_ready_error.value.category is BotProfileErrorCategory.BOT_NOT_READY
    assert api_error.value.category is BotProfileErrorCategory.DISCORD_API_FAILURE
    assert "sensitive response" not in str(api_error.value)


@pytest.mark.asyncio
async def test_discord_profile_service_maps_permission_failure() -> None:
    response = SimpleNamespace(status=403, reason="Forbidden", headers={})
    service = DiscordBotProfileService(
        FakeDiscordClient(  # type: ignore[arg-type]
            FakeMember(discord.Forbidden(response, "sensitive permission response"))
        ),
        guild_id=10,
    )

    with pytest.raises(BotProfileOperationError) as error:
        await service.reset_avatar(actor_discord_user_id=42)

    assert error.value.category is BotProfileErrorCategory.DISCORD_FORBIDDEN
    assert "sensitive permission response" not in str(error.value)


class FakeResponseContent:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def read(self, limit: int) -> bytes:
        chunk, self.body = self.body[:limit], self.body[limit:]
        return chunk


class FakeControlResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = FakeResponseContent(body)

    async def __aenter__(self) -> "FakeControlResponse":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class ChunkedResponseContent:
    """A consuming stream that can return fewer bytes than requested."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.read_limits = []
        self.bytes_read = 0

    async def read(self, limit):
        self.read_limits.append(limit)
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        if len(chunk) > limit:
            self.chunks.insert(0, chunk[limit:])
            chunk = chunk[:limit]
        self.bytes_read += len(chunk)
        return chunk


class FakeControlHttpSession:
    def __init__(self, response: FakeControlResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeControlResponse:
        self.calls.append((method, url, kwargs))
        return self.response


class CapabilitySubjectsHttpSession:
    def __init__(self, *, malformed_batch: int | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.malformed_batch = malformed_batch

    def request(self, method: str, url: str, **kwargs: object) -> FakeControlResponse:
        self.calls.append((method, url, kwargs))
        if self.malformed_batch == len(self.calls):
            response = FakeControlResponse(200, b"")
            response.content = ChunkedResponseContent(
                [b'{"roles":[],"members":[]}', b"!"]
            )
            return response
        params = kwargs["params"]
        roles = [int(value) for key, value in params if key == "role_id"]
        users = [int(value) for key, value in params if key == "user_id"]
        payload = {
            "roles": [{"id": value, "name": f"Role {value}"} for value in roles],
            "members": [{"id": value, "name": f"Member {value}"} for value in users],
        }
        body = json.dumps(payload).encode()
        response = FakeControlResponse(200, b"")
        response.content = ChunkedResponseContent([body[:11], body[11:]])
        return response


async def call_capability_client(client, endpoint):
    if endpoint == "subjects":
        return await client.get_capability_presentation_subjects((20,), (30,))
    if endpoint == "roles":
        return await client.get_capability_role_options()
    return await client.get_capability_member_options()


def chunked_capability_client(chunks):
    response = FakeControlResponse(200, b"")
    response.content = ChunkedResponseContent(chunks)
    http_session = FakeControlHttpSession(response)
    client = AiohttpBotProfileControlClient(
        http_session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:8765",
        shared_secret=SecretStr(SHARED_SECRET),
    )
    return client, response.content, http_session


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["subjects", "roles", "members"])
@pytest.mark.parametrize(
    "size", [100, OPTIONS_RESPONSE_MAX_BYTES - 1, OPTIONS_RESPONSE_MAX_BYTES]
)
async def test_capability_client_reads_full_chunked_body_within_limit(endpoint, size):
    body = b'{"roles":[{"id":20,"name":"Team"}],"members":[{"id":30,"name":"Alice"}]}'
    body += b" " * (size - len(body))
    client, stream, http_session = chunked_capability_client(
        [body[:9], body[9:41], body[41:]]
    )

    result = await call_capability_client(client, endpoint)

    expected = {
        "subjects": CapabilityPresentationSubjects(((20, "Team"),), ((30, "Alice"),)),
        "roles": ((20, "Team"),),
        "members": ((30, "Alice"),),
    }
    assert result == expected[endpoint]
    assert stream.bytes_read == size
    assert not stream.chunks
    assert len(stream.read_limits) >= 4  # Includes the final EOF read.
    assert http_session.calls[0][2]["allow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["subjects", "roles", "members"])
async def test_capability_client_rejects_cumulative_over_limit_immediately(endpoint):
    body = b'{"roles":[],"members":[]}'
    body += b" " * (OPTIONS_RESPONSE_MAX_BYTES - len(body))
    client, stream, _ = chunked_capability_client(
        [body[:10], body[10:], b" ", AssertionError("must stop at limit + 1")]
    )

    with pytest.raises(CapabilityPresentationControlError, match="control_unavailable"):
        await call_capability_client(client, endpoint)

    assert stream.bytes_read == OPTIONS_RESPONSE_MAX_BYTES + 1
    assert stream.read_limits[-1] == 1
    assert len(stream.chunks) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["subjects", "roles", "members"])
@pytest.mark.parametrize(
    "later_chunk,category",
    [
        (b"!", "malformed_response"),
        (asyncio.TimeoutError(), "timeout"),
        (aiohttp.ClientPayloadError("incomplete body"), "control_unavailable"),
    ],
)
async def test_capability_client_later_chunk_failure_never_returns_prefix(
    endpoint, later_chunk, category
):
    client, stream, _ = chunked_capability_client(
        [b'{"roles":[],"members":[]}', later_chunk]
    )
    with pytest.raises(CapabilityPresentationControlError, match=category):
        await call_capability_client(client, endpoint)
    assert len(stream.read_limits) >= 2


@pytest.mark.asyncio
async def test_web_control_client_uses_fixed_endpoint_and_separate_secret() -> None:
    body = (
        b'{"profile":{"user_id":99,"application_name":"Kanami",'
        b'"display_name":"Server Kanami","nickname":"Server Kanami",'
        b'"guild_avatar_url":null,'
        b'"display_avatar_url":"https://cdn.discordapp.com/avatar.png"}}'
    )
    http_session = FakeControlHttpSession(FakeControlResponse(200, body))
    client = AiohttpBotProfileControlClient(
        http_session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:8765",
        shared_secret=SecretStr(SHARED_SECRET),
    )

    profile = await client.update_nickname(" New ", actor_discord_user_id=42)

    assert profile.user_id == 99
    method, url, kwargs = http_session.calls[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:8765/control/v1/bot-profile/nickname"
    assert kwargs["headers"] == {
        "Authorization": f"Bearer {SHARED_SECRET}",
        "X-Kanami-Actor-Discord-User-Id": "42",
    }
    assert kwargs["json"] == {"nickname": "New"}


@pytest.mark.asyncio
async def test_web_control_client_uses_fixed_rules_publication_endpoints() -> None:
    http_session = FakeControlHttpSession(
        FakeControlResponse(
            200,
            b'{"status":"created","guild_id":10,"channel_id":20,'
            b'"message_id":30,"ruleset_id":40,"version":"1.0"}',
        )
    )
    client = AiohttpBotProfileControlClient(
        http_session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:8765",
        shared_secret=SecretStr(SHARED_SECRET),
    )

    synced = await client.sync_rules_publication(actor_discord_user_id=42)
    http_session.response = FakeControlResponse(
        200,
        b'{"status":"configured","guild_id":10,'
        b'"previous_channel_id":null,"channel_id":20,'
        b'"previous_message_id":null,"changed":true}',
    )
    configured = await client.configure_rules_publication(20, actor_discord_user_id=42)

    assert synced.status is RulesPublicationSyncStatus.CREATED
    assert configured.status is RulesPublicationConfigurationStatus.CONFIGURED
    assert [call[1] for call in http_session.calls] == [
        "http://127.0.0.1:8765/control/v1/rules/publication/sync",
        "http://127.0.0.1:8765/control/v1/rules/publication/configure",
    ]
    assert http_session.calls[1][2]["json"] == {"channel_id": 20}
    assert all(
        call[2]["headers"]["X-Kanami-Actor-Discord-User-Id"] == "42"
        for call in http_session.calls
    )


@pytest.mark.asyncio
async def test_web_control_client_uses_fixed_access_urls_actor_and_user_only() -> None:
    http_session = FakeControlHttpSession(
        FakeControlResponse(200, b'{"changed":true,"user_id":123}')
    )
    client = AiohttpBotProfileControlClient(
        http_session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:8765",
        shared_secret=SecretStr(SHARED_SECRET),
    )

    granted = await client.grant_web_admin_access(
        123,
        actor_discord_user_id=42,
    )
    http_session.response = FakeControlResponse(200, b'{"changed":false,"user_id":123}')
    revoked = await client.revoke_web_admin_access(
        123,
        actor_discord_user_id=42,
    )

    assert granted is True
    assert revoked is False
    assert [call[:2] for call in http_session.calls] == [
        ("POST", "http://127.0.0.1:8765/control/v1/web-admin/access/grant"),
        ("POST", "http://127.0.0.1:8765/control/v1/web-admin/access/revoke"),
    ]
    for _, _, kwargs in http_session.calls:
        assert kwargs["headers"] == {
            "Authorization": f"Bearer {SHARED_SECRET}",
            "X-Kanami-Actor-Discord-User-Id": "42",
        }
        assert kwargs["json"] == {"user_id": 123}
        assert kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_web_control_client_reads_options_without_actor_header() -> None:
    http_session = FakeControlHttpSession(
        FakeControlResponse(
            200,
            b'{"roles":[{"id":20,"name":"Member"}],'
            b'"channels":[{"id":30,"name":"audit-log","type":"text"}]}',
        )
    )
    client = AiohttpBotProfileControlClient(
        http_session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:8765",
        shared_secret=SecretStr(SHARED_SECRET),
    )

    options = await client.get_server_settings_options()

    assert options.roles == (ServerSettingsRoleOption(20, "Member"),)
    assert options.channels[0].id == 30
    method, url, kwargs = http_session.calls[0]
    assert method == "GET"
    assert url == "http://127.0.0.1:8765/control/v1/server-settings/options"
    assert kwargs["headers"] == {"Authorization": f"Bearer {SHARED_SECRET}"}
    assert kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_capability_role_options_client_validates_response_contract() -> None:
    session = FakeControlHttpSession(
        FakeControlResponse(
            200, b'{"roles":[{"id":20,"name":"Moderators"}],"members":[]}'
        )
    )
    client = AiohttpBotProfileControlClient(
        session,
        base_url="http://127.0.0.1:8765",
        shared_secret=SecretStr(SHARED_SECRET),
    )  # type: ignore[arg-type]
    assert await client.get_capability_role_options() == ((20, "Moderators"),)
    session.response = FakeControlResponse(
        200, b'{"roles":[{"id":20,"name":""}],"members":[]}'
    )
    with pytest.raises(CapabilityPresentationControlError):
        await client.get_capability_role_options()
    session.response = FakeControlResponse(
        200,
        b'{"roles":[{"id":20,"name":"Moderators"},{"id":20,"name":"Duplicate"}],"members":[]}',
    )
    with pytest.raises(CapabilityPresentationControlError):
        await client.get_capability_role_options()
    session.response = FakeControlResponse(503, b'{"error":"unavailable"}')
    with pytest.raises(CapabilityPresentationControlError):
        await client.get_capability_role_options()
    too_many = (
        b'{"roles":['
        + b",".join(b'{"id":1,"name":"Role"}' for _ in range(251))
        + b'],"members":[]}'
    )
    session.response = FakeControlResponse(200, too_many)
    with pytest.raises(CapabilityPresentationControlError):
        await client.get_capability_role_options()


@pytest.mark.asyncio
async def test_capability_member_options_client_validates_response_contract() -> None:
    session = FakeControlHttpSession(
        FakeControlResponse(200, b'{"roles":[],"members":[{"id":30,"name":"Alice"}]}')
    )
    client = AiohttpBotProfileControlClient(
        session,
        base_url="http://127.0.0.1:8765",
        shared_secret=SecretStr(SHARED_SECRET),
    )  # type: ignore[arg-type]

    assert await client.get_capability_member_options() == ((30, "Alice"),)
    method, url, kwargs = session.calls[-1]
    assert method == "GET"
    assert url == "http://127.0.0.1:8765/control/v1/capabilities/members"
    assert kwargs == {
        "headers": {"Authorization": f"Bearer {SHARED_SECRET}"},
        "allow_redirects": False,
    }

    invalid_payloads = (
        b"not json",
        b'{"roles":[]}',
        b'{"roles":[],"members":[{"id":30,"name":"Alice"},{"id":30,"name":"Again"}]}',
        b'{"roles":[],"members":[{"id":0,"name":"Alice"}]}',
        (
            b'{"roles":[],"members":[{"id":'
            + str(MAX_DISCORD_SNOWFLAKE + 1).encode()
            + b',"name":"Alice"}]}'
        ),
        b'{"roles":[],"members":[{"id":30,"name":""}]}',
        b'{"roles":[],"members":[{"id":30,"name":"' + b"A" * 101 + b'"}]}',
        json.dumps(
            {
                "roles": [],
                "members": [{"id": value, "name": "Member"} for value in range(1, 252)],
            }
        ).encode(),
    )
    for payload in invalid_payloads:
        session.response = FakeControlResponse(200, payload)
        with pytest.raises(CapabilityPresentationControlError):
            await client.get_capability_member_options()

    session.response = FakeControlResponse(503, b'{"error":"unavailable"}')
    with pytest.raises(CapabilityPresentationControlError):
        await client.get_capability_member_options()


@pytest.mark.asyncio
async def test_capability_subject_client_batches_deduplicates_and_aggregates() -> None:
    http_session = CapabilitySubjectsHttpSession()
    client = AiohttpBotProfileControlClient(
        http_session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:8765",
        shared_secret=SecretStr(SHARED_SECRET),
    )
    roles = tuple(range(1, 252)) + (1, 2)
    users = tuple(range(501, 752)) + (501,)

    subjects = await client.get_capability_presentation_subjects(roles, users)

    assert subjects.roles == tuple((value, f"Role {value}") for value in range(1, 252))
    assert subjects.members == tuple(
        (value, f"Member {value}") for value in range(501, 752)
    )
    assert len(http_session.calls) == 3
    for method, url, kwargs in http_session.calls:
        assert method == "GET"
        assert url == "http://127.0.0.1:8765/control/v1/capabilities/subjects"
        assert kwargs["headers"] == {"Authorization": f"Bearer {SHARED_SECRET}"}
        assert kwargs["allow_redirects"] is False
        assert sum(1 for key, _ in kwargs["params"] if key == "role_id") <= 250
        assert sum(1 for key, _ in kwargs["params"] if key == "user_id") <= 250
        assert len(kwargs["params"]) <= 250


@pytest.mark.asyncio
async def test_capability_subject_client_rejects_malformed_batch_without_partial_result() -> (
    None
):
    http_session = CapabilitySubjectsHttpSession(malformed_batch=2)
    client = AiohttpBotProfileControlClient(
        http_session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:8765",
        shared_secret=SecretStr(SHARED_SECRET),
    )

    with pytest.raises(CapabilityPresentationControlError):
        await client.get_capability_presentation_subjects(
            tuple(range(1, 252)), tuple(range(501, 752))
        )

    assert len(http_session.calls) == 2


@pytest.mark.asyncio
async def test_web_control_client_writes_exact_setting_payload_and_session_actor() -> (
    None
):
    http_session = FakeControlHttpSession(
        FakeControlResponse(
            200,
            b'{"changed":false,"setting":"audit_log_channel",'
            b'"mode":"env","value":null}',
        )
    )
    client = AiohttpBotProfileControlClient(
        http_session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:8765",
        shared_secret=SecretStr(SHARED_SECRET),
    )

    changed = await client.change_server_setting(
        GuildServerSettingKey.AUDIT_LOG_CHANNEL,
        GuildServerSettingOverride(GuildServerSettingOverrideMode.ENV),
        actor_discord_user_id=42,
    )

    assert changed is False
    method, url, kwargs = http_session.calls[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:8765/control/v1/server-settings"
    assert kwargs["headers"] == {
        "Authorization": f"Bearer {SHARED_SECRET}",
        "X-Kanami-Actor-Discord-User-Id": "42",
    }
    assert kwargs["json"] == {"setting": "audit_log_channel", "mode": "env"}


@pytest.mark.asyncio
async def test_web_control_client_maps_invalid_target_without_leaking_body() -> None:
    http_session = FakeControlHttpSession(
        FakeControlResponse(400, b'{"error":"invalid_target"}')
    )
    client = AiohttpBotProfileControlClient(
        http_session,  # type: ignore[arg-type]
        base_url="http://127.0.0.1:8765",
        shared_secret=SecretStr(SHARED_SECRET),
    )

    with pytest.raises(ServerSettingsControlError) as caught:
        await client.change_server_setting(
            GuildServerSettingKey.AUTOROLE_ROLE,
            GuildServerSettingOverride(GuildServerSettingOverrideMode.DISABLED),
            actor_discord_user_id=42,
        )

    assert caught.value.category is ServerSettingsControlCategory.INVALID_TARGET


def test_main_creates_control_server_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = object()
    assert (
        main_module._create_bot_control_server(  # type: ignore[arg-type]
            make_bot_settings(),
            object(),
            session_factory,
        )
        is None
    )

    configured = make_bot_settings(
        DISCORD_BOT_CONTROL_ENABLED=True,
        DISCORD_BOT_CONTROL_PORT=8877,
        DISCORD_BOT_CONTROL_SHARED_SECRET=SHARED_SECRET,
    )
    service = object()
    access_service = object()
    app = object()
    server = object()
    service_calls: list[tuple[object, int]] = []
    access_service_calls: list[tuple[object, int]] = []
    server_settings_service = object()
    server_settings_options_service = object()
    app_calls: list[tuple[object, object, object, object, object, object, object]] = []
    server_calls: list[tuple[object, str, int]] = []
    monkeypatch.setattr(
        main_module,
        "DiscordBotProfileService",
        lambda client, guild_id: service_calls.append((client, guild_id)) or service,
    )
    monkeypatch.setattr(
        main_module,
        "WebAdminAccessControlService",
        lambda factory, guild_id: (
            access_service_calls.append((factory, guild_id)) or access_service
        ),
    )
    monkeypatch.setattr(
        main_module,
        "DiscordServerSettingsControlService",
        lambda *args, **kwargs: server_settings_service,
    )
    monkeypatch.setattr(
        main_module,
        "DiscordServerSettingsOptionsService",
        lambda *args, **kwargs: server_settings_options_service,
    )
    capability_presentation_service = object()
    monkeypatch.setattr(
        main_module,
        "DiscordCapabilityPresentationService",
        lambda *args, **kwargs: capability_presentation_service,
    )
    monkeypatch.setattr(
        main_module,
        "create_bot_control_app",
        lambda operator, shared_secret, web_admin_access_operator, server_settings_operator, server_settings_options_operator, capability_presentation_subjects_operator, rules_publication_operator: (
            app_calls.append(
                (
                    operator,
                    shared_secret,
                    web_admin_access_operator,
                    server_settings_operator,
                    server_settings_options_operator,
                    capability_presentation_subjects_operator,
                    rules_publication_operator,
                )
            )
            or app
        ),
    )
    monkeypatch.setattr(
        main_module,
        "BotControlServer",
        lambda control_app, host, port: (
            server_calls.append((control_app, host, port)) or server
        ),
    )
    client = object()

    result = main_module._create_bot_control_server(  # type: ignore[arg-type]
        configured,
        client,
        session_factory,
    )

    assert result is server
    assert service_calls == [(client, 10)]
    assert access_service_calls == [(session_factory, 10)]
    assert app_calls == [
        (
            service,
            configured.discord_bot_control_shared_secret,
            access_service,
            server_settings_service,
            server_settings_options_service,
            capability_presentation_service,
            None,
        )
    ]
    assert server_calls == [(app, "127.0.0.1", 8877)]


class FakeWebAdminAccessOperator:
    def __init__(self, *, changed: bool = True, error: Exception | None = None) -> None:
        self.changed = changed
        self.error = error
        self.calls: list[tuple[object, ...]] = []

    async def grant_access(self, user_id: int, *, actor_discord_user_id: int) -> bool:
        self.calls.append(("grant", user_id, actor_discord_user_id))
        if self.error is not None:
            raise self.error
        return self.changed

    async def revoke_access(self, user_id: int, *, actor_discord_user_id: int) -> bool:
        self.calls.append(("revoke", user_id, actor_discord_user_id))
        if self.error is not None:
            raise self.error
        return self.changed


class FakeServerSettingsOperator:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def change_setting(
        self, key: object, override: object, *, actor_discord_user_id: int
    ) -> object:
        self.calls.append((key, override, actor_discord_user_id))
        return SimpleNamespace(changed=True)


class FakeServerSettingsOptionsOperator:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    async def get_options(self) -> ServerSettingsOptions:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ServerSettingsOptions(
            roles=(ServerSettingsRoleOption(20, "Member"),),
            channels=(
                ServerSettingsChannelOption(
                    30,
                    "audit-log",
                    ServerSettingsChannelType.TEXT,
                ),
            ),
        )


class FakeCapabilitySubjectsOperator:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[tuple[int, ...], tuple[int, ...]]] = []

    async def get_subjects(
        self, role_ids: tuple[int, ...], user_ids: tuple[int, ...]
    ) -> CapabilityPresentationSubjects:
        self.calls.append((role_ids, user_ids))
        if self.error is not None:
            raise self.error
        return CapabilityPresentationSubjects(
            tuple((role_id, f"Role {role_id}") for role_id in role_ids),
            tuple((user_id, f"Member {user_id}") for user_id in user_ids),
        )

    async def get_role_options(self) -> tuple[tuple[int, str], ...]:
        if self.error is not None:
            raise self.error
        return ((20, "Moderators"), (21, "Voice team"))

    async def get_member_options(self) -> tuple[tuple[int, str], ...]:
        if self.error is not None:
            raise self.error
        return ((30, "Alice"), (31, "Bob"))


def test_capability_roles_endpoint_is_authenticated_and_returns_options() -> None:
    roles = FakeCapabilitySubjectsOperator()
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        capability_presentation_subjects_operator=roles,
    )
    with TestClient(app) as client:
        missing = client.get("/control/v1/capabilities/roles")
        wrong = client.get(
            "/control/v1/capabilities/roles", headers={"Authorization": "Bearer wrong"}
        )
        response = client.get(
            "/control/v1/capabilities/roles",
            headers={"Authorization": f"Bearer {SHARED_SECRET}"},
        )
    assert missing.status_code == wrong.status_code == 401
    assert response.json() == {
        "roles": [{"id": 20, "name": "Moderators"}, {"id": 21, "name": "Voice team"}],
        "members": [],
    }


def test_capability_roles_endpoint_maps_unavailable_runtime_and_missing_operator() -> (
    None
):
    unavailable = create_bot_control_app(
        FakeOperator(), shared_secret=SecretStr(SHARED_SECRET)
    )
    runtime = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        capability_presentation_subjects_operator=FakeCapabilitySubjectsOperator(
            CapabilityPresentationRuntimeUnavailableError()
        ),
    )
    headers = {"Authorization": f"Bearer {SHARED_SECRET}"}
    with TestClient(unavailable) as client:
        assert (
            client.get("/control/v1/capabilities/roles", headers=headers).status_code
            == 503
        )
    with TestClient(runtime) as client:
        assert client.get("/control/v1/capabilities/roles", headers=headers).json() == {
            "error": "runtime_unavailable"
        }


def test_capability_members_endpoint_is_authenticated_and_returns_options() -> None:
    members = FakeCapabilitySubjectsOperator()
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        capability_presentation_subjects_operator=members,
    )
    with TestClient(app) as client:
        missing = client.get("/control/v1/capabilities/members")
        wrong = client.get(
            "/control/v1/capabilities/members",
            headers={"Authorization": "Bearer wrong"},
        )
        response = client.get(
            "/control/v1/capabilities/members",
            headers={"Authorization": f"Bearer {SHARED_SECRET}"},
        )

    assert missing.status_code == wrong.status_code == 401
    assert response.json() == {
        "roles": [],
        "members": [{"id": 30, "name": "Alice"}, {"id": 31, "name": "Bob"}],
    }


@pytest.mark.parametrize(
    "error,expected_error,logged",
    [
        (RuntimeError("unexpected"), "capability_presentation_failure", True),
        (CapabilityPresentationRuntimeUnavailableError(), "runtime_unavailable", False),
    ],
)
def test_capability_member_options_logs_only_unexpected_failure(
    caplog, error, expected_error, logged
):
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        capability_presentation_subjects_operator=FakeCapabilitySubjectsOperator(error),
    )
    with TestClient(app) as client:
        response = client.get(
            "/control/v1/capabilities/members",
            headers={"Authorization": f"Bearer {SHARED_SECRET}"},
        )
    assert response.status_code == 503
    assert response.json() == {"error": expected_error}
    records = [
        record
        for record in caplog.records
        if "capability_member_options_failed" in record.message
    ]
    assert len(records) == int(logged)
    if logged:
        assert records[0].exc_info is not None


def test_capability_members_endpoint_maps_unavailable_runtime_and_missing_operator() -> (
    None
):
    unavailable = create_bot_control_app(
        FakeOperator(), shared_secret=SecretStr(SHARED_SECRET)
    )
    runtime = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        capability_presentation_subjects_operator=FakeCapabilitySubjectsOperator(
            CapabilityPresentationRuntimeUnavailableError()
        ),
    )
    headers = {"Authorization": f"Bearer {SHARED_SECRET}"}
    with TestClient(unavailable) as client:
        assert (
            client.get("/control/v1/capabilities/members", headers=headers).status_code
            == 503
        )
    with TestClient(runtime) as client:
        response = client.get("/control/v1/capabilities/members", headers=headers)

    assert response.status_code == 503
    assert response.json() == {"error": "runtime_unavailable"}


def test_capability_subjects_endpoint_is_authenticated_bounded_and_typed() -> None:
    subjects = FakeCapabilitySubjectsOperator()
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        capability_presentation_subjects_operator=subjects,
    )
    too_many = "&".join(f"role_id={index}" for index in range(1, 252))
    with TestClient(app) as client:
        unauthorized = client.get("/control/v1/capabilities/subjects")
        invalid = client.get(
            "/control/v1/capabilities/subjects?role_id=0",
            headers={"Authorization": f"Bearer {SHARED_SECRET}"},
        )
        oversized = client.get(
            f"/control/v1/capabilities/subjects?{too_many}",
            headers={"Authorization": f"Bearer {SHARED_SECRET}"},
        )
        combined = client.get(
            "/control/v1/capabilities/subjects?"
            + "&".join(
                [
                    *(f"role_id={index}" for index in range(1, 126)),
                    *(f"user_id={index}" for index in range(126, 252)),
                ]
            ),
            headers={"Authorization": f"Bearer {SHARED_SECRET}"},
        )
        response = client.get(
            "/control/v1/capabilities/subjects?role_id=20&user_id=30",
            headers={"Authorization": f"Bearer {SHARED_SECRET}"},
        )

    assert unauthorized.status_code == 401
    assert invalid.status_code == 400
    assert oversized.status_code == 400
    assert combined.status_code == 400
    assert response.status_code == 200
    assert response.json() == {
        "roles": [{"id": 20, "name": "Role 20"}],
        "members": [{"id": 30, "name": "Member 30"}],
    }
    assert subjects.calls == [((20,), (30,))]


def test_capability_subjects_endpoint_maps_runtime_unavailable_to_503() -> None:
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        capability_presentation_subjects_operator=FakeCapabilitySubjectsOperator(
            CapabilityPresentationRuntimeUnavailableError()
        ),
    )
    with TestClient(app) as client:
        response = client.get(
            "/control/v1/capabilities/subjects",
            headers={"Authorization": f"Bearer {SHARED_SECRET}"},
        )

    assert response.status_code == 503
    assert response.json() == {"error": "runtime_unavailable"}


def test_server_settings_options_requires_bearer_and_has_bounded_contract() -> None:
    options = FakeServerSettingsOptionsOperator()
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        server_settings_options_operator=options,
    )
    with TestClient(app) as client:
        unauthorized = client.get("/control/v1/server-settings/options")
        response = client.get(
            "/control/v1/server-settings/options?guild_id=999",
            headers={"Authorization": f"Bearer {SHARED_SECRET}"},
        )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    assert response.json() == {
        "roles": [{"id": 20, "name": "Member"}],
        "channels": [{"id": 30, "name": "audit-log", "type": "text"}],
    }
    assert options.calls == 1


def test_server_settings_options_returns_503_without_runtime_operator() -> None:
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
    )
    with TestClient(app) as client:
        response = client.get(
            "/control/v1/server-settings/options",
            headers={"Authorization": f"Bearer {SHARED_SECRET}"},
        )

    assert response.status_code == 503
    assert response.json() == {"error": "server_settings_unavailable"}


def test_server_settings_control_accepts_only_bounded_allowlisted_payload() -> None:
    operator = FakeServerSettingsOperator()
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        server_settings_operator=operator,
    )
    with TestClient(app) as client:
        changed = client.post(
            "/control/v1/server-settings",
            headers=control_headers(),
            json={"setting": "audit_log_channel", "mode": "value", "value": 123},
        )
        disabled = client.post(
            "/control/v1/server-settings",
            headers=control_headers(),
            json={"setting": "return_channel", "mode": "disabled"},
        )

    assert changed.status_code == 200
    assert changed.json() == {
        "changed": True,
        "setting": "audit_log_channel",
        "mode": "value",
        "value": 123,
    }
    assert disabled.status_code == 200
    assert operator.calls[0][0] is GuildServerSettingKey.AUDIT_LOG_CHANNEL
    assert operator.calls[0][1].mode is GuildServerSettingOverrideMode.VALUE
    assert operator.calls[0][1].value == 123
    assert operator.calls[0][2] == 42


@pytest.mark.parametrize(
    "payload",
    [
        {"setting": "unknown", "mode": "disabled"},
        {"setting": "autorole_role", "mode": "value"},
        {"setting": "autorole_role", "mode": "disabled", "value": 123},
        {"setting": "autorole_role", "mode": "value", "value": True},
        {
            "setting": "autorole_role",
            "mode": "value",
            "value": 123,
            "guild_id": 10,
        },
    ],
)
def test_server_settings_control_rejects_unknown_extra_and_invalid_fields(
    payload: dict[str, object],
) -> None:
    operator = FakeServerSettingsOperator()
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        server_settings_operator=operator,
    )
    with TestClient(app) as client:
        response = client.post(
            "/control/v1/server-settings",
            headers=control_headers(),
            json=payload,
        )

    assert response.status_code == 400
    assert operator.calls == []


def test_web_admin_access_control_is_unavailable_without_operator() -> None:
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
    )
    with TestClient(app) as client:
        response = client.post(
            "/control/v1/web-admin/access/grant",
            headers=control_headers(),
            json={"user_id": 123},
        )

    assert response.status_code == 503
    assert response.json() == {"error": "access_control_unavailable"}


def test_web_admin_access_control_rejects_bad_auth_and_actor() -> None:
    access = FakeWebAdminAccessOperator()
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        web_admin_access_operator=access,
    )
    with TestClient(app) as client:
        bad_secret = client.post(
            "/control/v1/web-admin/access/grant",
            headers={"Authorization": "Bearer wrong", ACTOR_HEADER: "42"},
            json={"user_id": 123},
        )
        bad_actor = client.post(
            "/control/v1/web-admin/access/grant",
            headers={
                "Authorization": f"Bearer {SHARED_SECRET}",
                ACTOR_HEADER: "0",
            },
            json={"user_id": 123},
        )

    assert bad_secret.status_code == 401
    assert bad_actor.status_code == 401
    assert access.calls == []


def test_web_admin_access_control_rejects_untrusted_payload_fields() -> None:
    access = FakeWebAdminAccessOperator()
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        web_admin_access_operator=access,
    )
    with TestClient(app) as client:
        extra_guild = client.post(
            "/control/v1/web-admin/access/grant",
            headers=control_headers(),
            json={"user_id": 123, "guild_id": 999},
        )
        invalid_user = client.post(
            "/control/v1/web-admin/access/grant",
            headers=control_headers(),
            json={"user_id": 0},
        )
        boolean_user = client.post(
            "/control/v1/web-admin/access/grant",
            headers=control_headers(),
            json={"user_id": True},
        )

    assert extra_guild.status_code == 400
    assert invalid_user.status_code == 400
    assert boolean_user.status_code == 400
    assert access.calls == []


def test_web_admin_access_control_grant_and_revoke_fixed_operations() -> None:
    access = FakeWebAdminAccessOperator(changed=True)
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        web_admin_access_operator=access,
    )
    with TestClient(app) as client:
        grant = client.post(
            "/control/v1/web-admin/access/grant",
            headers=control_headers(),
            json={"user_id": 123},
        )
        revoke = client.post(
            "/control/v1/web-admin/access/revoke",
            headers=control_headers(),
            json={"user_id": 123},
        )

    assert grant.status_code == 200
    assert grant.json() == {"changed": True, "user_id": 123}
    assert revoke.status_code == 200
    assert revoke.json() == {"changed": True, "user_id": 123}
    assert access.calls == [
        ("grant", 123, 42),
        ("revoke", 123, 42),
    ]


def test_web_admin_access_control_preserves_idempotent_no_change() -> None:
    access = FakeWebAdminAccessOperator(changed=False)
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        web_admin_access_operator=access,
    )
    with TestClient(app) as client:
        response = client.post(
            "/control/v1/web-admin/access/grant",
            headers=control_headers(),
            json={"user_id": 123},
        )

    assert response.status_code == 200
    assert response.json() == {"changed": False, "user_id": 123}


def test_web_admin_access_control_converts_operator_failure_to_503() -> None:
    access = FakeWebAdminAccessOperator(error=RuntimeError("database failed"))
    app = create_bot_control_app(
        FakeOperator(),
        shared_secret=SecretStr(SHARED_SECRET),
        web_admin_access_operator=access,
    )
    with TestClient(app) as client:
        response = client.post(
            "/control/v1/web-admin/access/grant",
            headers=control_headers(),
            json={"user_id": 123},
        )

    assert response.status_code == 503
    assert response.json() == {"error": "access_control_failure"}
