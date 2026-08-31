from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from discord_stats_bot.config import WebSettings
from discord_stats_bot.features.bot_profile import (
    BOT_AVATAR_MAX_BYTES,
    JPEG_CONTENT_TYPE,
    PNG_CONTENT_TYPE,
    PNG_SIGNATURE,
    BotGuildProfile,
    BotProfileErrorCategory,
    BotProfileOperationError,
)
from discord_stats_bot.web.app import create_app
from discord_stats_bot.web.auth import SESSION_COOKIE_NAME, SESSION_COOKIE_PATH
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationCategory,
    WebAdminAuthorizationDecision,
    WebAdminRole,
)
from discord_stats_bot.web.bot_profile import (
    AVATAR_REQUEST_MAX_BYTES,
    admin_bot_profile_avatar,
)
from discord_stats_bot.web.service import (
    AdminCounts,
    AdminMemberDetailResult,
    AdminMemberDetailStatus,
    AdminMembersPage,
    WebDatabaseHealth,
)

DATABASE_URL = "postgresql+asyncpg://test:test@localhost:5432/test"


def make_settings(**overrides: object) -> WebSettings:
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
        WEB_ADMIN_ALLOWED_USER_IDS="42",
        **overrides,
    )


class FakeResources:
    session_factory = object()

    async def dispose(self) -> None:
        pass


class FakeAdminService:
    async def probe_database(self) -> WebDatabaseHealth:
        return WebDatabaseHealth(True, 0.001)

    async def load_counts(self) -> AdminCounts:
        return AdminCounts(1, 2, 3)

    async def load_members(
        self, *, page: int, query: str, **kwargs: object
    ) -> AdminMembersPage:
        return AdminMembersPage((), 0, page, 50, query)

    async def load_member_detail(self, user_id: int) -> AdminMemberDetailResult:
        return AdminMemberDetailResult(AdminMemberDetailStatus.NOT_FOUND)


class FakeAuthorizer:
    def __init__(self) -> None:
        self.allowed = True
        self.category: WebAdminAuthorizationCategory | None = None
        self.error: Exception | None = None
        self.calls: list[int] = []

    async def authorize(self, discord_user_id: int) -> WebAdminAuthorizationDecision:
        self.calls.append(discord_user_id)
        if self.error is not None:
            raise self.error
        return WebAdminAuthorizationDecision(
            self.allowed,
            self.category,
            WebAdminRole.OWNER if self.allowed else None,
        )


class FakeControl:
    def __init__(self) -> None:
        self.profile = BotGuildProfile(
            user_id=99,
            application_name="Kanami",
            display_name="Server Kanami",
            nickname="Server Kanami",
            guild_avatar_url="https://cdn.discordapp.com/guild.png",
            display_avatar_url="https://cdn.discordapp.com/guild.png",
        )
        self.calls: list[tuple[object, ...]] = []
        self.error: BotProfileOperationError | None = None

    def _result(self) -> BotGuildProfile:
        if self.error is not None:
            raise self.error
        return self.profile

    async def get_profile(self) -> BotGuildProfile:
        self.calls.append(("get",))
        return self._result()

    async def update_nickname(
        self, nickname: str, *, actor_discord_user_id: int
    ) -> BotGuildProfile:
        self.calls.append(("nickname", nickname, actor_discord_user_id))
        return self._result()

    async def reset_nickname(self, *, actor_discord_user_id: int) -> BotGuildProfile:
        self.calls.append(("nickname_reset", actor_discord_user_id))
        return self._result()

    async def update_avatar(
        self,
        avatar: bytes,
        content_type: str,
        *,
        actor_discord_user_id: int,
    ) -> BotGuildProfile:
        self.calls.append(("avatar", avatar, content_type, actor_discord_user_id))
        return self._result()

    async def reset_avatar(self, *, actor_discord_user_id: int) -> BotGuildProfile:
        self.calls.append(("avatar_reset", actor_discord_user_id))
        return self._result()


def make_app(
    control: FakeControl, *, authorizer: FakeAuthorizer | None = None
) -> Starlette:
    current_authorizer = authorizer or FakeAuthorizer()
    return create_app(
        make_settings(),
        resource_factory=lambda settings, read_only: FakeResources(),
        service_factory=lambda session_factory: FakeAdminService(),
        oauth_client_factory=lambda session, settings: SimpleNamespace(),
        authorization_service_factory=lambda session_factory, settings: (
            current_authorizer
        ),
        bot_profile_control_factory=lambda session, settings: control,
    )


@contextmanager
def authenticated_client(
    app: Starlette,
) -> Iterator[tuple[TestClient, str]]:
    issued = app.state.web_session_store.create(42, role=WebAdminRole.OWNER)
    with TestClient(app) as client:
        client.cookies.set(
            SESSION_COOKIE_NAME,
            issued.session_id,
            path=SESSION_COOKIE_PATH,
        )
        yield client, issued.session.csrf_token


def test_bot_profile_page_requires_existing_authenticated_session() -> None:
    app = make_app(FakeControl())
    with TestClient(app) as client:
        response = client.get(
            "/admin/settings/bot-profile",
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_authorized_session_can_open_bot_profile_page_and_home_links_it() -> None:
    control = FakeControl()
    app = make_app(control)
    with authenticated_client(app) as (client, _csrf):
        page = client.get("/admin/settings/bot-profile")
        home = client.get("/admin/")

    assert page.status_code == 200
    assert "Server Kanami" in page.text
    assert "Kanami" in page.text
    assert "guild.png" in page.text
    assert "/admin/settings/bot-profile" in home.text


@pytest.mark.parametrize("data", [{}, {"csrf_token": "wrong", "nickname": "New"}])
def test_bot_profile_posts_require_valid_session_csrf(data: dict[str, str]) -> None:
    control = FakeControl()
    app = make_app(control)
    with authenticated_client(app) as (client, _csrf):
        response = client.post(
            "/admin/settings/bot-profile/nickname",
            data=data,
            follow_redirects=False,
        )

    assert response.status_code == 403
    assert not any(call[0] == "nickname" for call in control.calls)


def test_bot_profile_avatar_post_rejects_wrong_csrf() -> None:
    control = FakeControl()
    app = make_app(control)
    with authenticated_client(app) as (client, _csrf):
        response = client.post(
            "/admin/settings/bot-profile/avatar",
            data={"csrf_token": "wrong"},
            files={
                "avatar": (
                    "avatar.png",
                    PNG_SIGNATURE + b"image",
                    PNG_CONTENT_TYPE,
                )
            },
            follow_redirects=False,
        )

    assert response.status_code == 403
    assert not any(call[0] == "avatar" for call in control.calls)


@pytest.mark.parametrize("nickname", ["", " ", "x" * 33, "bad\nname"])
def test_bot_profile_rejects_invalid_nickname(nickname: str) -> None:
    control = FakeControl()
    app = make_app(control)
    with authenticated_client(app) as (client, csrf):
        response = client.post(
            "/admin/settings/bot-profile/nickname",
            data={"csrf_token": csrf, "nickname": nickname},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "error=invalid_nickname" in response.headers["location"]
    assert not any(call[0] == "nickname" for call in control.calls)


def test_bot_profile_nickname_update_and_reset_use_authenticated_actor() -> None:
    control = FakeControl()
    app = make_app(control)
    with authenticated_client(app) as (client, csrf):
        update = client.post(
            "/admin/settings/bot-profile/nickname",
            data={"csrf_token": csrf, "nickname": "  New Name  "},
            follow_redirects=False,
        )
        reset = client.post(
            "/admin/settings/bot-profile/nickname/reset",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )

    assert update.headers["location"].endswith("result=nickname_updated")
    assert reset.headers["location"].endswith("result=nickname_reset")
    assert ("nickname", "New Name", 42) in control.calls
    assert ("nickname_reset", 42) in control.calls


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/admin/settings/bot-profile/nickname", {"nickname": "New"}),
        ("/admin/settings/bot-profile/nickname/reset", {}),
        ("/admin/settings/bot-profile/avatar/reset", {}),
    ],
)
def test_write_actions_fail_closed_when_fresh_authorization_is_lost(
    path: str,
    data: dict[str, str],
) -> None:
    control = FakeControl()
    authorizer = FakeAuthorizer()
    authorizer.allowed = False
    authorizer.category = WebAdminAuthorizationCategory.NOT_CURRENT_MEMBER
    app = make_app(control, authorizer=authorizer)

    with authenticated_client(app) as (client, csrf):
        response = client.post(
            path,
            data={**data, "csrf_token": csrf},
            follow_redirects=False,
        )
        expired = client.get("/admin/", follow_redirects=False)

    assert response.status_code == 403
    assert expired.status_code == 303
    assert authorizer.calls == [42]
    assert control.calls == []


def test_write_action_database_failure_fails_closed_without_control_call() -> None:
    control = FakeControl()
    authorizer = FakeAuthorizer()
    authorizer.error = RuntimeError("sensitive database detail")
    app = make_app(control, authorizer=authorizer)

    with authenticated_client(app) as (client, csrf):
        response = client.post(
            "/admin/settings/bot-profile/nickname",
            data={"nickname": "New", "csrf_token": csrf},
        )

    assert response.status_code == 403
    assert "sensitive database detail" not in response.text
    assert control.calls == []


def test_csrf_is_checked_before_fresh_authorization() -> None:
    control = FakeControl()
    authorizer = FakeAuthorizer()
    authorizer.allowed = False
    app = make_app(control, authorizer=authorizer)

    with authenticated_client(app) as (client, csrf):
        response = client.post(
            "/admin/settings/bot-profile/nickname",
            data={"nickname": "New", "csrf_token": f"wrong-{csrf}"},
        )

    assert response.status_code == 403
    assert authorizer.calls == []
    assert control.calls == []


def test_avatar_update_uses_fresh_authorization_before_control() -> None:
    control = FakeControl()
    authorizer = FakeAuthorizer()
    authorizer.allowed = False
    authorizer.category = WebAdminAuthorizationCategory.NOT_ALLOWED
    app = make_app(control, authorizer=authorizer)

    with authenticated_client(app) as (client, csrf):
        response = client.post(
            "/admin/settings/bot-profile/avatar",
            data={"csrf_token": csrf},
            files={
                "avatar": ("avatar.png", PNG_SIGNATURE + b"image", PNG_CONTENT_TYPE)
            },
        )

    assert response.status_code == 403
    assert authorizer.calls == [42]
    assert control.calls == []


def test_write_action_rate_limiter_is_bounded_and_blocks_fast_spam() -> None:
    control = FakeControl()
    authorizer = FakeAuthorizer()
    app = make_app(control, authorizer=authorizer)

    with authenticated_client(app) as (client, csrf):
        responses = [
            client.post(
                "/admin/settings/bot-profile/nickname/reset",
                data={"csrf_token": csrf},
                follow_redirects=False,
            )
            for _ in range(11)
        ]

    assert [response.status_code for response in responses] == [303] * 10 + [429]
    assert len(control.calls) == 10


def test_bot_profile_success_redirect_displays_confirmation() -> None:
    app = make_app(FakeControl())
    with authenticated_client(app) as (client, csrf):
        response = client.post(
            "/admin/settings/bot-profile/nickname",
            data={"csrf_token": csrf, "nickname": "New Name"},
            follow_redirects=True,
        )

    assert response.status_code == 200
    assert "Серверный никнейм обновлён." in response.text


@pytest.mark.parametrize(
    ("filename", "content_type", "body"),
    [
        ("avatar.png", PNG_CONTENT_TYPE, PNG_SIGNATURE + b"image"),
        ("avatar.jpg", JPEG_CONTENT_TYPE, b"\xff\xd8\xff\xe0image\xff\xd9"),
    ],
)
def test_bot_profile_accepts_png_and_jpeg_uploads(
    filename: str,
    content_type: str,
    body: bytes,
) -> None:
    control = FakeControl()
    app = make_app(control)
    with authenticated_client(app) as (client, csrf):
        response = client.post(
            "/admin/settings/bot-profile/avatar",
            data={"csrf_token": csrf},
            files={"avatar": (filename, body, content_type)},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].endswith("result=avatar_updated")
    assert ("avatar", body, content_type, 42) in control.calls


def test_bot_profile_rejects_fake_png_and_oversized_upload() -> None:
    control = FakeControl()
    app = make_app(control)
    with authenticated_client(app) as (client, csrf):
        fake = client.post(
            "/admin/settings/bot-profile/avatar",
            data={"csrf_token": csrf},
            files={"avatar": ("avatar.png", b"not-png", PNG_CONTENT_TYPE)},
            follow_redirects=False,
        )
        oversized = client.post(
            "/admin/settings/bot-profile/avatar",
            data={"csrf_token": csrf},
            files={
                "avatar": (
                    "large.png",
                    PNG_SIGNATURE + b"x" * BOT_AVATAR_MAX_BYTES,
                    PNG_CONTENT_TYPE,
                )
            },
            follow_redirects=False,
        )

    assert "error=invalid_avatar" in fake.headers["location"]
    assert "error=invalid_avatar" in oversized.headers["location"]
    assert not any(call[0] == "avatar" for call in control.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", [None, b"1"])
async def test_avatar_request_body_is_bounded_without_reliable_content_length(
    declared_length: bytes | None,
) -> None:
    control = FakeControl()
    app = make_app(control)
    issued = app.state.web_session_store.create(42, role=WebAdminRole.OWNER)
    boundary = b"kanami-boundary"
    prefix = (
        b"--"
        + boundary
        + b'\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n'
        + issued.session.csrf_token.encode("ascii")
        + b"\r\n--"
        + boundary
        + b'\r\nContent-Disposition: form-data; name="avatar"; filename="avatar.png"\r\n'
        + b"Content-Type: image/png\r\n\r\n"
    )
    chunk_size = 1024 * 1024
    chunks = [prefix, *([b"x" * chunk_size] * 20)]
    received_bytes = 0
    receive_calls = 0

    async def receive() -> dict[str, object]:
        nonlocal receive_calls, received_bytes
        body = chunks[receive_calls]
        receive_calls += 1
        received_bytes += len(body)
        return {"type": "http.request", "body": body, "more_body": True}

    headers = [(b"content-type", b"multipart/form-data; boundary=" + boundary)]
    if declared_length is not None:
        headers.append((b"content-length", declared_length))
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/admin/settings/bot-profile/avatar",
            "raw_path": b"/admin/settings/bot-profile/avatar",
            "query_string": b"",
            "headers": headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8000),
            "state": {
                "web_session": issued.session,
                "bot_profile_control": control,
            },
        },
        receive,
    )

    response = await admin_bot_profile_avatar(request)

    assert response.status_code == 303
    assert b"error=invalid_avatar" in response.headers["location"].encode()
    assert receive_calls < len(chunks)
    assert received_bytes <= AVATAR_REQUEST_MAX_BYTES + chunk_size
    assert not any(call[0] == "avatar" for call in control.calls)


def test_unexpected_avatar_parser_error_is_logged_safely(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    control = FakeControl()
    app = make_app(control)

    def fail_form(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("parser failure")

    monkeypatch.setattr(Request, "form", fail_form)
    caplog.set_level("ERROR", logger="discord_stats_bot.web.bot_profile")
    with authenticated_client(app) as (client, csrf):
        response = client.post(
            "/admin/settings/bot-profile/avatar",
            data={"csrf_token": csrf},
            files={
                "avatar": ("avatar.png", PNG_SIGNATURE + b"private", PNG_CONTENT_TYPE)
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "error=invalid_avatar" in response.headers["location"]
    assert "category=internal_error" in caplog.text
    assert csrf not in caplog.text
    assert "private" not in caplog.text
    assert not any(call[0] == "avatar" for call in control.calls)


def test_bot_profile_avatar_reset_and_control_unavailable_are_friendly() -> None:
    control = FakeControl()
    app = make_app(control)
    with authenticated_client(app) as (client, csrf):
        reset = client.post(
            "/admin/settings/bot-profile/avatar/reset",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        control.error = BotProfileOperationError(
            BotProfileErrorCategory.CONTROL_UNAVAILABLE
        )
        unavailable = client.get("/admin/settings/bot-profile")

    assert reset.headers["location"].endswith("result=avatar_reset")
    assert ("avatar_reset", 42) in control.calls
    assert unavailable.status_code == 200
    assert "Control service сейчас недоступен" in unavailable.text
    assert "Профиль бота сейчас недоступен" in unavailable.text


def test_web_settings_for_profile_control_do_not_require_discord_token() -> None:
    settings = make_settings(
        WEB_ADMIN_BOT_CONTROL_URL="http://127.0.0.1:8765",
        WEB_ADMIN_BOT_CONTROL_SHARED_SECRET=(
            "control-secret-value-that-is-at-least-32-characters"
        ),
    )

    assert not hasattr(settings, "discord_token")
