import logging
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from discord_stats_bot.config import Settings, WebSettings
from discord_stats_bot.web.app import create_app
from discord_stats_bot.web.auth import (
    DISCORD_AUTHORIZE_URL,
    DISCORD_CURRENT_USER_URL,
    DISCORD_TOKEN_URL,
    OAUTH_COOKIE_NAME,
    OAUTH_COOKIE_PATH,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_PATH,
    DiscordOAuthClient,
    DiscordOAuthError,
    OAuthHttpResponse,
    OAuthTransactionStore,
    StoreCapacityError,
    WebSessionStore,
    pkce_s256_challenge,
)
from discord_stats_bot.web.authorization import (
    WebAdminAuthorizationCategory,
    WebAdminAuthorizationDecision,
    WebAdminAuthorizationService,
    WebAdminRole,
)
from discord_stats_bot.web.service import (
    AdminCounts,
    AdminMemberDetailResult,
    AdminMemberDetailStatus,
    AdminMembersPage,
    WebAdminManagedAccessRepository,
    WebAdminMembershipRepository,
    WebDatabaseHealth,
)

DATABASE_URL = "postgresql+asyncpg://test:test@localhost:5432/test"
CLIENT_ID = 123456789012345678
CLIENT_SECRET = "discord-client-secret-value"
ACCESS_TOKEN = "discord-access-token-value"
REFRESH_TOKEN = "discord-refresh-token-value"
REDIRECT_URI = "http://localhost:8000/admin/auth/discord/callback"


def make_settings(**overrides: object) -> WebSettings:
    values: dict[str, object] = {
        "DATABASE_URL": DATABASE_URL,
        "DISCORD_GUILD_ID": 1,
        "WEB_ADMIN_DISCORD_CLIENT_ID": CLIENT_ID,
        "WEB_ADMIN_DISCORD_CLIENT_SECRET": CLIENT_SECRET,
        "WEB_ADMIN_DISCORD_REDIRECT_URI": REDIRECT_URI,
        "WEB_ADMIN_COOKIE_SECURE": False,
        "WEB_ADMIN_ALLOWED_USER_IDS": "42",
        **overrides,
    }
    return WebSettings(_env_file=None, **values)


def test_web_settings_exposes_non_secret_server_setting_baselines() -> None:
    settings = make_settings(
        DISCORD_AUTOROLE_ID=11,
        DISCORD_AUDIT_LOG_CHANNEL_ID=12,
        DISCORD_ANNIVERSARY_CHANNEL_ID=13,
        DISCORD_RETURN_CHANNEL_ID=14,
    )

    assert settings.discord_autorole_id == 11
    assert settings.discord_audit_log_channel_id == 12
    assert settings.discord_anniversary_channel_id == 13
    assert settings.discord_return_channel_id == 14


def load_web_settings_from_environment(
    monkeypatch: pytest.MonkeyPatch,
    allowed_user_ids: str | None,
) -> WebSettings:
    values = {
        "DATABASE_URL": DATABASE_URL,
        "DISCORD_GUILD_ID": "1",
        "WEB_ADMIN_DISCORD_CLIENT_ID": str(CLIENT_ID),
        "WEB_ADMIN_DISCORD_CLIENT_SECRET": CLIENT_SECRET,
        "WEB_ADMIN_DISCORD_REDIRECT_URI": REDIRECT_URI,
        "WEB_ADMIN_COOKIE_SECURE": "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    if allowed_user_ids is None:
        monkeypatch.delenv("WEB_ADMIN_ALLOWED_USER_IDS", raising=False)
    else:
        monkeypatch.setenv("WEB_ADMIN_ALLOWED_USER_IDS", allowed_user_ids)
    return WebSettings(_env_file=None)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class SequenceTokens:
    def __init__(self, *tokens: str) -> None:
        self.tokens = iter(tokens)

    def __call__(self) -> str:
        return next(self.tokens)


class FakeHttpClient:
    def __init__(self) -> None:
        self.token_response = OAuthHttpResponse(
            200,
            (
                '{"access_token":"%s","refresh_token":"%s",'
                '"token_type":"Bearer"}' % (ACCESS_TOKEN, REFRESH_TOKEN)
            ).encode(),
        )
        self.identity_response = OAuthHttpResponse(200, b'{"id":"42"}')
        self.token_error: DiscordOAuthError | None = None
        self.identity_error: DiscordOAuthError | None = None
        self.post_calls: list[tuple[str, dict[str, str], str, str]] = []
        self.get_calls: list[tuple[str, str]] = []

    async def post_form(
        self,
        url: str,
        *,
        data: dict[str, str],
        client_id: str,
        client_secret: str,
    ) -> OAuthHttpResponse:
        self.post_calls.append((url, dict(data), client_id, client_secret))
        if self.token_error is not None:
            raise self.token_error
        return self.token_response

    async def get_bearer(
        self,
        url: str,
        *,
        access_token: str,
    ) -> OAuthHttpResponse:
        self.get_calls.append((url, access_token))
        if self.identity_error is not None:
            raise self.identity_error
        return self.identity_response


class FakeResources:
    def __init__(self) -> None:
        self.session_factory = object()
        self.dispose_calls = 0

    async def dispose(self) -> None:
        self.dispose_calls += 1


class FakeAdminService:
    async def probe_database(self) -> WebDatabaseHealth:
        return WebDatabaseHealth(True, 0.001)

    async def load_counts(self) -> AdminCounts:
        return AdminCounts(guilds=1, tracked_users=2, audit_events=3)

    async def load_members(
        self, *, page: int, query: str, **kwargs: object
    ) -> AdminMembersPage:
        return AdminMembersPage((), total=0, page=page, page_size=50, query=query)

    async def load_member_detail(self, user_id: int) -> AdminMemberDetailResult:
        return AdminMemberDetailResult(AdminMemberDetailStatus.NOT_FOUND)


class FakeAuthorizer:
    def __init__(
        self,
        *,
        allowed: bool = True,
        category: WebAdminAuthorizationCategory = (
            WebAdminAuthorizationCategory.NOT_CURRENT_MEMBER
        ),
    ) -> None:
        self.allowed = allowed
        self.category = category
        self.calls: list[int] = []

    async def authorize(self, discord_user_id: int) -> WebAdminAuthorizationDecision:
        self.calls.append(discord_user_id)
        return WebAdminAuthorizationDecision(
            self.allowed,
            None if self.allowed else self.category,
            WebAdminRole.OWNER if self.allowed else None,
        )


def make_app(
    *,
    settings: WebSettings | None = None,
    http_client: FakeHttpClient | None = None,
    transactions: OAuthTransactionStore | None = None,
    sessions: WebSessionStore | None = None,
    authorizer: FakeAuthorizer | None = None,
):
    settings = settings or make_settings()
    http_client = http_client or FakeHttpClient()
    resources = FakeResources()
    authorizer = authorizer or FakeAuthorizer()

    def oauth_factory(
        _session: object, web_settings: WebSettings
    ) -> DiscordOAuthClient:
        return DiscordOAuthClient(
            http_client,
            client_id=web_settings.web_admin_discord_client_id,
            client_secret=web_settings.web_admin_discord_client_secret,
            redirect_uri=web_settings.web_admin_discord_redirect_uri,
        )

    app = create_app(
        settings,
        resource_factory=lambda config, read_only: resources,  # type: ignore[arg-type]
        service_factory=lambda session_factory: FakeAdminService(),
        oauth_client_factory=oauth_factory,  # type: ignore[arg-type]
        authorization_service_factory=lambda session_factory, config: authorizer,
        oauth_transaction_store=transactions,
        web_session_store=sessions,
    )
    return app, http_client, resources


def start_login(client: TestClient) -> tuple[str, object]:
    response = client.get("/admin/login", follow_redirects=False)
    assert response.status_code == 303
    query = parse_qs(urlsplit(response.headers["location"]).query)
    return query["state"][0], response


def complete_login(client: TestClient) -> tuple[str, object]:
    state, _ = start_login(client)
    response = client.get(
        "/admin/auth/discord/callback",
        params={"code": "authorization-code-value", "state": state},
        follow_redirects=False,
    )
    return state, response


def csrf_from_html(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def test_web_settings_require_oauth_fields_and_keep_them_out_of_bot_settings() -> None:
    with pytest.raises(ValidationError) as exc_info:
        WebSettings(
            _env_file=None,
            DATABASE_URL=DATABASE_URL,
            DISCORD_GUILD_ID=1,
        )

    assert {item["loc"][0] for item in exc_info.value.errors()} == {
        "WEB_ADMIN_DISCORD_CLIENT_ID",
        "WEB_ADMIN_DISCORD_CLIENT_SECRET",
        "WEB_ADMIN_DISCORD_REDIRECT_URI",
    }

    bot_settings = Settings(
        _env_file=None,
        DATABASE_URL=DATABASE_URL,
        DISCORD_GUILD_ID=1,
        DISCORD_TOKEN="bot-token-placeholder",
    )
    assert not hasattr(bot_settings, "web_admin_discord_client_secret")
    assert CLIENT_SECRET not in repr(make_settings())
    assert "**********" in repr(make_settings().web_admin_discord_client_secret)


def test_web_admin_allowlist_is_normalized_and_deny_by_default() -> None:
    normalized = make_settings(
        WEB_ADMIN_ALLOWED_USER_IDS=" 42, 7, ,42,7, 99 ",
    )
    assert normalized.web_admin_allowed_user_ids == frozenset({7, 42, 99})

    missing = make_settings(WEB_ADMIN_ALLOWED_USER_IDS=None)
    empty = make_settings(WEB_ADMIN_ALLOWED_USER_IDS=" ,  ,")
    assert missing.web_admin_allowed_user_ids == frozenset()
    assert empty.web_admin_allowed_user_ids == frozenset()


@pytest.mark.parametrize(
    "value",
    ["abc", "1.5", "-1", "+1", "0", "1, nope", "18446744073709551616"],
)
def test_web_admin_allowlist_rejects_malformed_ids(value: str) -> None:
    with pytest.raises(ValidationError, match="Discord ID"):
        make_settings(WEB_ADMIN_ALLOWED_USER_IDS=value)


@pytest.mark.parametrize(
    ("raw_value", "expected"),
    [
        ("111111111111111111", frozenset({111111111111111111})),
        (
            "111111111111111111,123456789012345678",
            frozenset({111111111111111111, 123456789012345678}),
        ),
        (
            "111111111111111111, 123456789012345678",
            frozenset({111111111111111111, 123456789012345678}),
        ),
        (
            "111111111111111111, ,111111111111111111,123456789012345678,",
            frozenset({111111111111111111, 123456789012345678}),
        ),
        ("", frozenset()),
        (" , , ", frozenset()),
        (None, frozenset()),
    ],
)
def test_web_admin_allowlist_loads_documented_raw_environment_format(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str | None,
    expected: frozenset[int],
) -> None:
    settings = load_web_settings_from_environment(monkeypatch, raw_value)

    assert settings.web_admin_allowed_user_ids == expected


@pytest.mark.parametrize("raw_value", ["abc", "0", "-1", "12.5"])
def test_web_admin_allowlist_rejects_malformed_environment_values(
    monkeypatch: pytest.MonkeyPatch,
    raw_value: str,
) -> None:
    with pytest.raises(ValidationError, match="Discord ID"):
        load_web_settings_from_environment(monkeypatch, raw_value)


@pytest.mark.asyncio
async def test_authorization_service_resolves_owner_and_admin_then_membership() -> None:
    class Membership:
        def __init__(self, current_ids: set[int]) -> None:
            self.current_ids = current_ids
            self.calls: list[int] = []

        async def is_current_non_bot_member(self, discord_user_id: int) -> bool:
            self.calls.append(discord_user_id)
            return discord_user_id in self.current_ids

    class ManagedAccess:
        def __init__(self, active_ids: set[int]) -> None:
            self.active_ids = active_ids
            self.calls: list[int] = []

        async def is_active_admin(self, discord_user_id: int) -> bool:
            self.calls.append(discord_user_id)
            return discord_user_id in self.active_ids

    membership = Membership({41, 42, 43})
    managed = ManagedAccess({42, 43})
    service = WebAdminAuthorizationService(
        frozenset({41, 42}),
        managed,
        membership,
    )

    first_owner = await service.authorize(41)
    second_owner_with_grant = await service.authorize(42)
    admin = await service.authorize(43)
    denied = await service.authorize(44)

    assert first_owner == WebAdminAuthorizationDecision(
        True,
        role=WebAdminRole.OWNER,
    )
    assert second_owner_with_grant.role is WebAdminRole.OWNER
    assert admin.role is WebAdminRole.ADMIN
    assert denied.category is WebAdminAuthorizationCategory.NOT_ALLOWED
    assert managed.calls == [43, 44]
    assert membership.calls == [41, 42, 43]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "owner_ids", "active_admin_ids"),
    [
        (41, frozenset({41}), set()),
        (42, frozenset(), {42}),
    ],
)
async def test_authorization_service_denies_owner_or_admin_without_membership(
    user_id: int,
    owner_ids: frozenset[int],
    active_admin_ids: set[int],
) -> None:
    class Membership:
        async def is_current_non_bot_member(self, discord_user_id: int) -> bool:
            return False

    class ManagedAccess:
        async def is_active_admin(self, discord_user_id: int) -> bool:
            return discord_user_id in active_admin_ids

    decision = await WebAdminAuthorizationService(
        owner_ids,
        ManagedAccess(),
        Membership(),
    ).authorize(user_id)
    assert decision.category is WebAdminAuthorizationCategory.NOT_CURRENT_MEMBER
    assert decision.role is None


@pytest.mark.parametrize(
    "redirect_uri",
    [
        "ftp://localhost/admin/auth/discord/callback",
        "https://user:password@example.com/admin/auth/discord/callback",
        "https://example.com/admin/auth/discord/callback?next=x",
        "https://example.com/admin/auth/discord/callback#fragment",
        "https://example.com/wrong/callback",
    ],
)
def test_web_settings_reject_invalid_redirect_uris(redirect_uri: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(
            WEB_ADMIN_DISCORD_REDIRECT_URI=redirect_uri,
            WEB_ADMIN_COOKIE_SECURE=True,
        )


def test_web_settings_enforce_explicit_secure_transport_combinations() -> None:
    secure = make_settings(
        WEB_ADMIN_DISCORD_REDIRECT_URI=(
            "https://admin.example.com/admin/auth/discord/callback"
        ),
        WEB_ADMIN_COOKIE_SECURE=True,
    )
    assert secure.web_admin_cookie_secure is True

    with pytest.raises(ValidationError, match="requires an HTTPS"):
        make_settings(WEB_ADMIN_COOKIE_SECURE=True)
    with pytest.raises(ValidationError, match="only allowed for loopback HTTP"):
        make_settings(
            WEB_ADMIN_DISCORD_REDIRECT_URI=(
                "https://admin.example.com/admin/auth/discord/callback"
            ),
            WEB_ADMIN_COOKIE_SECURE=False,
        )
    with pytest.raises(ValidationError, match="loopback redirect host"):
        make_settings(
            WEB_ADMIN_DISCORD_REDIRECT_URI=(
                "http://admin.example.com/admin/auth/discord/callback"
            ),
            WEB_ADMIN_COOKIE_SECURE=False,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("WEB_ADMIN_DISCORD_CLIENT_ID", 0),
        ("WEB_ADMIN_DISCORD_CLIENT_ID", "not-decimal"),
        ("WEB_ADMIN_SESSION_LIFETIME_SECONDS", 299),
        ("WEB_ADMIN_SESSION_LIFETIME_SECONDS", 86_401),
    ],
)
def test_web_settings_reject_invalid_ids_and_session_lifetimes(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_oauth_transaction_store_is_random_bounded_expiring_and_one_shot() -> None:
    clock = MutableClock()
    store = OAuthTransactionStore(capacity=2, clock=clock)
    first = store.begin()
    second = store.begin()

    assert first.state != second.state
    assert first.code_verifier != second.code_verifier
    assert len(first.state) >= 43
    assert len(first.code_verifier) >= 43
    assert first.code_challenge == pkce_s256_challenge(first.code_verifier)
    assert store.consume(first.state) is not None
    assert store.consume(first.state) is None

    third = store.begin(previous_state=second.state)
    assert store.consume(second.state) is None
    clock.advance(301)
    assert store.consume(third.state) is None
    assert len(store) == 0

    store.begin()
    store.begin()
    with pytest.raises(StoreCapacityError):
        store.begin()


def test_web_session_store_has_absolute_expiry_rotation_capacity_and_restart() -> None:
    clock = MutableClock()
    store = WebSessionStore(lifetime_seconds=300, capacity=2, clock=clock)
    first = store.create(42, role=WebAdminRole.OWNER)
    assert len(first.session_id) >= 43
    assert first.session.expires_at - first.session.created_at == timedelta(seconds=300)
    assert store.get(first.session_id) == first.session

    clock.advance(200)
    assert store.get(first.session_id) == first.session
    assert first.session.expires_at - first.session.created_at == timedelta(seconds=300)

    rotated = store.create(
        42,
        role=WebAdminRole.OWNER,
        previous_session_id=first.session_id,
    )
    assert rotated.session_id != first.session_id
    assert store.get(first.session_id) is None
    assert WebSessionStore(lifetime_seconds=300).get(rotated.session_id) is None

    store.create(43, role=WebAdminRole.ADMIN)
    with pytest.raises(StoreCapacityError):
        store.create(44, role=WebAdminRole.ADMIN)
    clock.advance(301)
    assert store.get(rotated.session_id) is None


@pytest.mark.asyncio
async def test_discord_oauth_adapter_uses_exact_code_flow_and_returns_only_identity() -> (
    None
):
    http_client = FakeHttpClient()
    settings = make_settings()
    client = DiscordOAuthClient(
        http_client,
        client_id=settings.web_admin_discord_client_id,
        client_secret=settings.web_admin_discord_client_secret,
        redirect_uri=settings.web_admin_discord_redirect_uri,
    )

    identity = await client.authenticate(code="code-value", code_verifier="verifier")

    assert identity.user_id == 42
    assert identity.__slots__ == ("user_id",)
    assert http_client.post_calls == [
        (
            DISCORD_TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": "code-value",
                "redirect_uri": REDIRECT_URI,
                "code_verifier": "verifier",
            },
            str(CLIENT_ID),
            CLIENT_SECRET,
        )
    ]
    assert http_client.get_calls == [(DISCORD_CURRENT_USER_URL, ACCESS_TOKEN)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "category"),
    [
        (b"not-json", "token_malformed_response"),
        (b"{}", "token_malformed_response"),
        (b'{"access_token":"   ","token_type":"Bearer"}', "token_malformed_response"),
        (b'{"access_token":"x","token_type":"MAC"}', "token_invalid_type"),
    ],
)
async def test_discord_oauth_adapter_rejects_malformed_token_responses(
    body: bytes,
    category: str,
) -> None:
    http_client = FakeHttpClient()
    http_client.token_response = OAuthHttpResponse(200, body)
    settings = make_settings()
    client = DiscordOAuthClient(
        http_client,
        client_id=CLIENT_ID,
        client_secret=settings.web_admin_discord_client_secret,
        redirect_uri=REDIRECT_URI,
    )

    with pytest.raises(DiscordOAuthError) as exc_info:
        await client.authenticate(code="code", code_verifier="verifier")
    assert exc_info.value.category == category
    assert http_client.get_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 429, 500])
async def test_discord_oauth_adapter_rejects_token_status_without_body(
    status: int,
) -> None:
    http_client = FakeHttpClient()
    http_client.token_response = OAuthHttpResponse(status, b"sensitive-upstream-body")
    settings = make_settings()
    client = DiscordOAuthClient(
        http_client,
        client_id=CLIENT_ID,
        client_secret=settings.web_admin_discord_client_secret,
        redirect_uri=REDIRECT_URI,
    )

    with pytest.raises(DiscordOAuthError) as exc_info:
        await client.authenticate(code="code", code_verifier="verifier")
    assert exc_info.value.category == "token_bad_status"
    assert exc_info.value.upstream_status == status
    assert "sensitive-upstream-body" not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity_body",
    [b"not-json", b"{}", b'{"id":"0"}', b'{"id":"-1"}', b'{"id":"abc"}'],
)
async def test_discord_oauth_adapter_rejects_invalid_identity(
    identity_body: bytes,
) -> None:
    http_client = FakeHttpClient()
    http_client.identity_response = OAuthHttpResponse(200, identity_body)
    settings = make_settings()
    client = DiscordOAuthClient(
        http_client,
        client_id=CLIENT_ID,
        client_secret=settings.web_admin_discord_client_secret,
        redirect_uri=REDIRECT_URI,
    )

    with pytest.raises(DiscordOAuthError) as exc_info:
        await client.authenticate(code="code", code_verifier="verifier")
    assert exc_info.value.category == "identity_malformed_response"


def test_auth_middleware_is_deny_by_default_with_exact_public_paths() -> None:
    app, _, _ = make_app()
    with TestClient(app) as client:
        for path in ("/admin/", "/admin/members", "/admin/members/42"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/admin/login"

        assert client.get("/admin/health").status_code == 200
        assert client.get("/admin/login", follow_redirects=False).status_code == 303
        assert (
            client.get(
                "/admin/auth/discord/callback",
                follow_redirects=False,
            ).status_code
            == 400
        )
        assert (
            client.get(
                "/admin/health/details",
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert client.get("/admin/future", follow_redirects=False).status_code == 303
        assert client.post("/admin/future").status_code == 401

        issued = app.state.web_session_store.create(42, role=WebAdminRole.OWNER)
        client.cookies.set(
            SESSION_COOKIE_NAME,
            issued.session_id,
            path=SESSION_COOKIE_PATH,
        )
        assert client.get("/admin/").status_code == 200
        assert client.get("/admin/future").status_code == 404


def test_central_security_headers_cover_html_redirect_health_and_denial() -> None:
    app, _, _ = make_app()
    expected = {
        "cache-control": "no-store",
        "referrer-policy": "no-referrer",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "permissions-policy": (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        ),
    }
    with TestClient(app) as client:
        redirect = client.get("/admin/", follow_redirects=False)
        login = client.get("/admin/login", follow_redirects=False)
        health = client.get("/admin/health")
        denial = client.post("/admin/future")

    for response in (redirect, login, health, denial):
        for name, value in expected.items():
            assert response.headers[name] == value
        csp = response.headers["content-security-policy"]
        assert "script-src 'none'" in csp
        assert "https://cdn.discordapp.com" in csp
        assert "https://media.discordapp.net" in csp
        assert "frame-ancestors 'none'" in csp


def test_forwarded_headers_do_not_change_cookie_security_decisions() -> None:
    app, _, _ = make_app()
    with TestClient(app) as client:
        response = client.get(
            "/admin/login",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "203.0.113.10",
                "Forwarded": "for=203.0.113.10;proto=https",
            },
            follow_redirects=False,
        )

    assert "Secure" not in response.headers["set-cookie"]


def test_login_builds_exact_identify_pkce_request_and_secure_cookie() -> None:
    app, _, _ = make_app()
    with TestClient(app) as client:
        state, response = start_login(client)

    location = urlsplit(response.headers["location"])
    query = parse_qs(location.query)
    assert (
        f"{location.scheme}://{location.netloc}{location.path}" == DISCORD_AUTHORIZE_URL
    )
    assert query == {
        "response_type": ["code"],
        "client_id": [str(CLIENT_ID)],
        "redirect_uri": [REDIRECT_URI],
        "scope": ["identify"],
        "state": [state],
        "code_challenge": [query["code_challenge"][0]],
        "code_challenge_method": ["S256"],
    }
    assert len(query["code_challenge"][0]) == 43
    cookie = response.headers["set-cookie"]
    assert f"{OAUTH_COOKIE_NAME}={state}" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert f"Path={OAUTH_COOKIE_PATH}" in cookie
    assert "Max-Age=300" in cookie
    assert "Secure" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert state not in response.text


def test_https_mode_sets_secure_oauth_and_session_cookies() -> None:
    settings = make_settings(
        WEB_ADMIN_DISCORD_REDIRECT_URI=(
            "https://admin.example.com/admin/auth/discord/callback"
        ),
        WEB_ADMIN_COOKIE_SECURE=True,
    )
    app, _, _ = make_app(settings=settings)
    with TestClient(app, base_url="https://admin.example.com") as client:
        state, login = start_login(client)
        callback = client.get(
            "/admin/auth/discord/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )

    assert "Secure" in login.headers["set-cookie"]
    session_cookies = [
        item
        for item in callback.headers.get_list("set-cookie")
        if item.startswith(SESSION_COOKIE_NAME)
    ]
    assert len(session_cookies) == 1
    assert "Secure" in session_cookies[0]


def test_successful_callback_creates_session_rotates_old_and_never_opens_redirect() -> (
    None
):
    sessions = WebSessionStore(lifetime_seconds=28_800)
    old = sessions.create(7, role=WebAdminRole.ADMIN)
    app, http_client, _ = make_app(sessions=sessions)
    with TestClient(app) as client:
        client.cookies.set(
            SESSION_COOKIE_NAME,
            old.session_id,
            domain="testserver.local",
            path=SESSION_COOKIE_PATH,
        )
        state, _ = start_login(client)
        response = client.get(
            "/admin/auth/discord/callback",
            params={
                "code": "authorization-code-value",
                "state": state,
                "next": "https://evil.example/",
            },
            follow_redirects=False,
        )
        session_cookie = client.cookies.get(SESSION_COOKIE_NAME)
        page = client.get("/admin/")

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/"
    assert "?" not in response.headers["location"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert session_cookie and session_cookie != old.session_id
    assert sessions.get(old.session_id) is None
    assert sessions.get(session_cookie).discord_user_id == 42  # type: ignore[union-attr]
    assert page.status_code == 200
    assert "authorization-code-value" not in page.text
    assert session_cookie not in page.text
    assert CLIENT_SECRET not in page.text
    assert ACCESS_TOKEN not in page.text
    assert REFRESH_TOKEN not in page.text
    assert http_client.post_calls[0][1]["code_verifier"] not in page.text

    cookies = response.headers.get_list("set-cookie")
    session_headers = [item for item in cookies if item.startswith(SESSION_COOKIE_NAME)]
    assert len(session_headers) == 1
    session_header = session_headers[0]
    assert "HttpOnly" in session_header
    assert "SameSite=lax" in session_header
    assert f"Path={SESSION_COOKIE_PATH}" in session_header
    assert "Max-Age=28800" in session_header
    assert "Secure" not in session_header


@pytest.mark.parametrize(
    "category",
    [
        WebAdminAuthorizationCategory.NOT_ALLOWED,
        WebAdminAuthorizationCategory.NOT_CURRENT_MEMBER,
    ],
)
def test_callback_denial_returns_403_without_session_or_session_cookie(
    category: WebAdminAuthorizationCategory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    sessions = WebSessionStore(lifetime_seconds=28_800)
    authorizer = FakeAuthorizer(allowed=False, category=category)
    settings = make_settings(WEB_ADMIN_ALLOWED_USER_IDS="42,777777777777777777")
    app, _, _ = make_app(
        settings=settings,
        sessions=sessions,
        authorizer=authorizer,
    )
    caplog.set_level(logging.INFO)

    with TestClient(app) as client:
        state, _ = start_login(client)
        response = client.get(
            "/admin/auth/discord/callback",
            params={"code": "authorization-code-value", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 403
    assert "Доступ к Kanami Admin запрещён" in response.text
    assert "location" not in response.headers
    assert len(sessions) == 0
    assert authorizer.calls == [42]
    cookies = response.headers.get_list("set-cookie")
    assert any(item.startswith(OAUTH_COOKIE_NAME) for item in cookies)
    assert not any(item.startswith(SESSION_COOKIE_NAME) for item in cookies)
    assert f"category={category}" in caplog.text
    application_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("discord_stats_bot")
    )
    combined = response.text + application_logs
    for secret in (
        state,
        "authorization-code-value",
        CLIENT_SECRET,
        ACCESS_TOKEN,
        REFRESH_TOKEN,
        "777777777777777777",
    ):
        assert secret not in combined


def test_callback_authorizes_before_creating_session() -> None:
    events: list[str] = []

    class OrderedAuthorizer(FakeAuthorizer):
        async def authorize(
            self, discord_user_id: int
        ) -> WebAdminAuthorizationDecision:
            events.append("authorize")
            return await super().authorize(discord_user_id)

    class OrderedSessions(WebSessionStore):
        def create(
            self,
            discord_user_id: int,
            *,
            role: WebAdminRole,
            previous_session_id: str | None = None,
        ):
            events.append("create_session")
            return super().create(
                discord_user_id,
                role=role,
                previous_session_id=previous_session_id,
            )

    app, _, _ = make_app(
        sessions=OrderedSessions(lifetime_seconds=28_800),
        authorizer=OrderedAuthorizer(),
    )
    with TestClient(app) as client:
        _, response = complete_login(client)

    assert response.status_code == 303
    assert events == ["authorize", "create_session"]


@pytest.mark.asyncio
async def test_membership_repository_uses_one_bounded_select_only_statement() -> None:
    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def scalar(self, statement: object) -> int:
            self.statements.append(statement)
            return 42

    session = Session()
    repository = WebAdminMembershipRepository(
        lambda: session,  # type: ignore[arg-type]
        guild_id=1,
    )

    assert await repository.is_current_non_bot_member(42) is True
    assert len(session.statements) == 1
    sql = " ".join(str(session.statements[0]).split())
    assert sql.startswith("SELECT")
    assert "guild_members.guild_id =" in sql
    assert "guild_members.user_id =" in sql
    assert "guild_members.left_at IS NULL" in sql
    assert "discord_users.is_bot IS false" in sql
    assert "LIMIT" in sql
    assert all(keyword not in sql.upper() for keyword in ("INSERT", "UPDATE", "DELETE"))


@pytest.mark.asyncio
async def test_managed_access_repository_uses_configured_guild_select_only() -> None:
    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def scalar(self, statement: object) -> int:
            self.statements.append(statement)
            return 1

    session = Session()
    repository = WebAdminManagedAccessRepository(
        lambda: session,  # type: ignore[arg-type]
        guild_id=123,
    )

    assert await repository.is_active_admin(42) is True
    assert len(session.statements) == 1
    statement = session.statements[0]
    sql = " ".join(str(statement).split())
    assert sql.startswith("SELECT")
    assert "web_admin_access_grants.guild_id =" in sql
    assert "web_admin_access_grants.user_id =" in sql
    assert "web_admin_access_grants.revoked_at IS NULL" in sql
    assert 123 in statement.compile().params.values()  # type: ignore[union-attr]
    assert 42 in statement.compile().params.values()  # type: ignore[union-attr]
    assert "LIMIT" in sql
    assert all(keyword not in sql.upper() for keyword in ("INSERT", "UPDATE", "DELETE"))


@pytest.mark.asyncio
async def test_managed_access_repository_denies_revoked_or_missing_grant() -> None:
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def scalar(self, statement: object) -> None:
            return None

    repository = WebAdminManagedAccessRepository(
        lambda: Session(),  # type: ignore[arg-type]
        guild_id=1,
    )

    assert await repository.is_active_admin(42) is False


@pytest.mark.asyncio
async def test_administrator_list_is_select_only_active_and_owner_protected() -> None:
    class Result:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self.rows = rows

        def mappings(self):
            return self

        def all(self) -> list[dict[str, object]]:
            return self.rows

    class Session:
        def __init__(self) -> None:
            self.statements: list[object] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def execute(self, statement: object) -> Result:
            self.statements.append(statement)
            if len(self.statements) == 1:
                return Result(
                    [
                        {
                            "id": 41,
                            "username": "owner-user",
                            "global_name": "Owner Global",
                            "nickname": "Owner Nick",
                        }
                    ]
                )
            return Result(
                [
                    {
                        "user_id": 50,
                        "granted_at": datetime(2026, 8, 23, tzinfo=UTC),
                        "granted_by_user_id": 41,
                        "username": "admin-user",
                        "global_name": None,
                        "nickname": "Admin Nick",
                    }
                ]
            )

    session = Session()
    repository = WebAdminManagedAccessRepository(
        lambda: session,  # type: ignore[arg-type]
        guild_id=10,
    )

    result = await repository.list_administrators(frozenset({41, 42}))

    assert result is not None
    assert [(item.user_id, item.display_name) for item in result.owners] == [
        (41, "Owner Nick"),
        (42, "42"),
    ]
    assert [(item.user_id, item.display_name) for item in result.admins] == [
        (50, "Admin Nick")
    ]
    sql = [" ".join(str(statement).split()) for statement in session.statements]
    assert all(statement.startswith("SELECT") for statement in sql)
    assert "web_admin_access_grants.revoked_at IS NULL" in sql[1]
    assert "web_admin_access_grants.user_id NOT IN" in sql[1]
    assert all(
        keyword not in statement.upper()
        for statement in sql
        for keyword in ("INSERT", "UPDATE", "DELETE")
    )


@pytest.mark.parametrize(
    ("query", "cookie", "expected_category"),
    [
        ({"code": "code"}, None, "state_missing"),
        ({"code": "code", "state": "query-state"}, None, "state_missing"),
        (
            {"code": "code", "state": "query-state"},
            "cookie-state",
            "state_mismatch",
        ),
    ],
)
def test_callback_rejects_missing_or_mismatched_state(
    query: dict[str, str],
    cookie: str | None,
    expected_category: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _, _ = make_app()
    caplog.set_level(logging.WARNING)
    with TestClient(app) as client:
        if cookie:
            client.cookies.set(OAUTH_COOKIE_NAME, cookie, path=OAUTH_COOKIE_PATH)
        response = client.get(
            "/admin/auth/discord/callback",
            params=query,
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert expected_category in caplog.text
    assert "query-state" not in caplog.text
    assert "cookie-state" not in caplog.text


def test_callback_rejects_unicode_state_without_logging_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unicode_state = "💥"
    app, _, _ = make_app()
    caplog.set_level(logging.WARNING)
    with TestClient(app) as client:
        start_login(client)
        response = client.get(
            "/admin/auth/discord/callback",
            params={"code": "code", "state": unicode_state},
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert "state_mismatch" in caplog.text
    assert unicode_state not in caplog.text


def test_callback_rejects_expired_transaction_and_replay() -> None:
    clock = MutableClock()
    transactions = OAuthTransactionStore(clock=clock)
    app, _, _ = make_app(transactions=transactions)
    with TestClient(app) as client:
        state, _ = start_login(client)
        clock.advance(301)
        expired = client.get(
            "/admin/auth/discord/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        assert expired.status_code == 400

        state, _ = start_login(client)
        first = client.get(
            "/admin/auth/discord/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )
        client.cookies.set(OAUTH_COOKIE_NAME, state, path=OAUTH_COOKIE_PATH)
        replay = client.get(
            "/admin/auth/discord/callback",
            params={"code": "code", "state": state},
            follow_redirects=False,
        )

    assert first.status_code == 303
    assert replay.status_code == 400


def test_callback_rejects_discord_denial_and_missing_code() -> None:
    app, http_client, _ = make_app()
    with TestClient(app) as client:
        state, _ = start_login(client)
        denied = client.get(
            "/admin/auth/discord/callback",
            params={
                "error": "access_denied",
                "error_description": "sensitive-description",
                "state": state,
            },
            follow_redirects=False,
        )
        assert denied.status_code == 400
        assert "sensitive-description" not in denied.text

        state, _ = start_login(client)
        missing = client.get(
            "/admin/auth/discord/callback",
            params={"state": state},
            follow_redirects=False,
        )

    assert missing.status_code == 400
    assert http_client.post_calls == []


@pytest.mark.parametrize(
    ("failure_target", "category", "status"),
    [
        ("token", "token_network_error", None),
        ("token", "token_bad_status", 429),
        ("identity", "identity_network_error", None),
        ("identity", "identity_bad_status", 500),
    ],
)
def test_callback_handles_discord_failures_without_leaks(
    failure_target: str,
    category: str,
    status: int | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    http_client = FakeHttpClient()
    error = DiscordOAuthError(category, status)
    if failure_target == "token":
        http_client.token_error = error
    else:
        http_client.identity_error = error
    app, _, _ = make_app(http_client=http_client)
    caplog.set_level(logging.WARNING)

    with TestClient(app) as client:
        state, _ = start_login(client)
        response = client.get(
            "/admin/auth/discord/callback",
            params={"code": "authorization-code-value", "state": state},
            follow_redirects=False,
        )

    assert response.status_code == 502
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    combined = response.text + caplog.text
    assert category in caplog.text
    assert "authorization-code-value" not in combined
    assert CLIENT_SECRET not in combined
    assert ACCESS_TOKEN not in combined
    assert REFRESH_TOKEN not in combined
    assert state not in combined


def test_session_expiry_unknown_tampering_and_new_store_fail_closed() -> None:
    clock = MutableClock()
    sessions = WebSessionStore(lifetime_seconds=300, clock=clock)
    issued = sessions.create(42, role=WebAdminRole.OWNER)
    app, _, _ = make_app(sessions=sessions)
    with TestClient(app) as client:
        client.cookies.set(
            SESSION_COOKIE_NAME, issued.session_id, path=SESSION_COOKIE_PATH
        )
        assert client.get("/admin/").status_code == 200
        clock.advance(301)
        assert client.get("/admin/", follow_redirects=False).status_code == 303

        client.cookies.set(SESSION_COOKIE_NAME, "tampered", path=SESSION_COOKIE_PATH)
        assert client.get("/admin/", follow_redirects=False).status_code == 303

    restarted_app, _, _ = make_app(sessions=WebSessionStore(lifetime_seconds=300))
    with TestClient(restarted_app) as client:
        client.cookies.set(
            SESSION_COOKIE_NAME, issued.session_id, path=SESSION_COOKIE_PATH
        )
        assert client.get("/admin/", follow_redirects=False).status_code == 303


def test_logout_requires_post_and_csrf_then_revokes_copied_cookie() -> None:
    app, _, _ = make_app()
    with TestClient(app) as client:
        _, callback = complete_login(client)
        assert callback.status_code == 303
        page = client.get("/admin/")
        csrf_token = csrf_from_html(page.text)
        copied_session = client.cookies.get(SESSION_COOKIE_NAME)

        assert client.get("/admin/logout").status_code == 405
        assert client.post("/admin/logout", data={}).status_code == 403
        assert (
            client.post(
                "/admin/logout",
                data={"csrf_token": "wrong-token"},
            ).status_code
            == 403
        )

        logout = client.post(
            "/admin/logout",
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert logout.status_code == 303
        assert logout.headers["location"] == "/admin/login"
        assert SESSION_COOKIE_NAME in logout.headers["set-cookie"]

        client.cookies.set(
            SESSION_COOKIE_NAME,
            copied_session,
            path=SESSION_COOKIE_PATH,
        )
        assert client.get("/admin/", follow_redirects=False).status_code == 303


def test_logout_rejects_unicode_csrf_without_revoking_session_or_logging_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    unicode_token = "💥"
    app, _, _ = make_app()
    caplog.set_level(logging.INFO)
    with TestClient(app) as client:
        _, callback = complete_login(client)
        assert callback.status_code == 303

        response = client.post(
            "/admin/logout",
            data={"csrf_token": unicode_token},
            follow_redirects=False,
        )
        authenticated_page = client.get("/admin/", follow_redirects=False)

    assert response.status_code == 403
    assert authenticated_page.status_code == 200
    application_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("discord_stats_bot")
    )
    assert unicode_token not in application_logs


def test_auth_responses_do_not_reflect_protocol_secrets_or_open_redirect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, http_client, _ = make_app()
    caplog.set_level(logging.INFO)
    with TestClient(app) as client:
        state, login = start_login(client)
        callback = client.get(
            "/admin/auth/discord/callback",
            params={
                "code": "authorization-code-value",
                "state": state,
                "return_to": "https://evil.example/",
            },
            follow_redirects=False,
        )

    verifier = http_client.post_calls[0][1]["code_verifier"]
    intended_login_headers = login.headers["location"] + login.headers["set-cookie"]
    assert state in intended_login_headers
    assert callback.headers["location"] == "/admin/"

    application_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("discord_stats_bot")
    )
    forbidden_output = callback.text + callback.headers["location"] + application_logs
    for secret in (
        state,
        "authorization-code-value",
        verifier,
        CLIENT_SECRET,
        ACCESS_TOKEN,
        REFRESH_TOKEN,
    ):
        assert secret not in forbidden_output
    assert "evil.example" not in callback.headers["location"]
