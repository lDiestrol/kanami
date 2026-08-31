"""Discord OAuth2 adapter and bounded in-memory Web Admin authentication stores."""

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Protocol
from urllib.parse import urlencode

import aiohttp
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
)
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from discord_stats_bot.web.authorization import WebAdminRole

DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_CURRENT_USER_URL = "https://discord.com/api/v10/users/@me"
DISCORD_ID_MAX = 18_446_744_073_709_551_615
OAUTH_TRANSACTION_TTL_SECONDS = 300
DEFAULT_STORE_CAPACITY = 1_024
MAX_UPSTREAM_RESPONSE_BYTES = 65_536

OAUTH_COOKIE_NAME = "kanami_admin_oauth"
OAUTH_COOKIE_PATH = "/admin/auth/discord/callback"
SESSION_COOKIE_NAME = "kanami_admin_session"
SESSION_COOKIE_PATH = "/admin"
PUBLIC_ADMIN_PATHS = frozenset(
    {
        "/admin/login",
        "/admin/auth/discord/callback",
        "/admin/health",
    }
)

Clock = Callable[[], datetime]
TokenFactory = Callable[[], str]


def utc_now() -> datetime:
    return datetime.now(UTC)


def random_token() -> str:
    """Return a 256-bit URL-safe token without padding."""

    return secrets.token_urlsafe(32)


def pkce_s256_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def constant_time_token_equal(left: str, right: str) -> bool:
    """Compare arbitrary text tokens through fixed-size byte digests."""

    return hmac.compare_digest(_token_digest(left), _token_digest(right))


class StoreCapacityError(RuntimeError):
    """A bounded authentication store cannot accept another entry."""


@dataclass(frozen=True, slots=True)
class OAuthTransaction:
    state: str
    code_verifier: str
    code_challenge: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _StoredOAuthTransaction:
    code_verifier: str
    expires_at: datetime


class OAuthTransactionStore:
    """Bounded, one-shot OAuth transactions local to one Web Admin process."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_STORE_CAPACITY,
        ttl_seconds: int = OAUTH_TRANSACTION_TTL_SECONDS,
        clock: Clock = utc_now,
        token_factory: TokenFactory = random_token,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._capacity = capacity
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock
        self._token_factory = token_factory
        self._entries: dict[bytes, _StoredOAuthTransaction] = {}
        self._lock = Lock()

    def begin(self, *, previous_state: str | None = None) -> OAuthTransaction:
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            if previous_state:
                self._entries.pop(_token_digest(previous_state), None)
            if len(self._entries) >= self._capacity:
                raise StoreCapacityError("oauth_transaction_store_full")

            for _ in range(4):
                state = self._token_factory()
                key = _token_digest(state)
                if key not in self._entries:
                    break
            else:
                raise StoreCapacityError("oauth_transaction_token_collision")

            verifier = self._token_factory()
            expires_at = now + self._ttl
            self._entries[key] = _StoredOAuthTransaction(verifier, expires_at)
        return OAuthTransaction(
            state=state,
            code_verifier=verifier,
            code_challenge=pkce_s256_challenge(verifier),
            expires_at=expires_at,
        )

    def consume(self, state: str) -> _StoredOAuthTransaction | None:
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            return self._entries.pop(_token_digest(state), None)

    def discard(self, state: str) -> None:
        with self._lock:
            self._entries.pop(_token_digest(state), None)

    def __len__(self) -> int:
        with self._lock:
            self._cleanup(self._clock())
            return len(self._entries)

    def _cleanup(self, now: datetime) -> None:
        expired = [key for key, item in self._entries.items() if item.expires_at <= now]
        for key in expired:
            del self._entries[key]


@dataclass(frozen=True, slots=True)
class WebSession:
    discord_user_id: int
    role: WebAdminRole
    created_at: datetime
    expires_at: datetime
    csrf_token: str


@dataclass(frozen=True, slots=True)
class IssuedWebSession:
    session_id: str
    session: WebSession


class WebSessionStore:
    """Bounded absolute-lifetime sessions local to one Web Admin process."""

    def __init__(
        self,
        *,
        lifetime_seconds: int,
        capacity: int = DEFAULT_STORE_CAPACITY,
        clock: Clock = utc_now,
        token_factory: TokenFactory = random_token,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if lifetime_seconds <= 0:
            raise ValueError("lifetime_seconds must be positive")
        self._capacity = capacity
        self._lifetime = timedelta(seconds=lifetime_seconds)
        self._clock = clock
        self._token_factory = token_factory
        self._entries: dict[bytes, WebSession] = {}
        self._lock = Lock()

    def create(
        self,
        discord_user_id: int,
        *,
        role: WebAdminRole,
        previous_session_id: str | None = None,
    ) -> IssuedWebSession:
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            if previous_session_id:
                self._entries.pop(_token_digest(previous_session_id), None)
            if len(self._entries) >= self._capacity:
                raise StoreCapacityError("web_session_store_full")

            for _ in range(4):
                session_id = self._token_factory()
                key = _token_digest(session_id)
                if key not in self._entries:
                    break
            else:
                raise StoreCapacityError("web_session_token_collision")

            session = WebSession(
                discord_user_id=discord_user_id,
                role=role,
                created_at=now,
                expires_at=now + self._lifetime,
                csrf_token=self._token_factory(),
            )
            self._entries[key] = session
        return IssuedWebSession(session_id, session)

    def get(self, session_id: str) -> WebSession | None:
        now = self._clock()
        with self._lock:
            self._cleanup(now)
            return self._entries.get(_token_digest(session_id))

    def revoke(self, session_id: str) -> bool:
        with self._lock:
            return self._entries.pop(_token_digest(session_id), None) is not None

    def __len__(self) -> int:
        with self._lock:
            self._cleanup(self._clock())
            return len(self._entries)

    def _cleanup(self, now: datetime) -> None:
        expired = [key for key, item in self._entries.items() if item.expires_at <= now]
        for key in expired:
            del self._entries[key]


@dataclass(frozen=True, slots=True)
class DiscordIdentity:
    user_id: int


class OAuthIdentityProvider(Protocol):
    def build_authorize_url(self, *, state: str, code_challenge: str) -> str: ...

    async def authenticate(
        self,
        *,
        code: str,
        code_verifier: str,
    ) -> DiscordIdentity: ...


@dataclass(frozen=True, slots=True)
class OAuthHttpResponse:
    status: int
    body: bytes


class OAuthHttpClient(Protocol):
    async def post_form(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        client_id: str,
        client_secret: str,
    ) -> OAuthHttpResponse: ...

    async def get_bearer(self, url: str, *, access_token: str) -> OAuthHttpResponse: ...


class AiohttpOAuthHttpClient:
    """Small safe aiohttp transport used only by the Discord OAuth adapter."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def post_form(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        client_id: str,
        client_secret: str,
    ) -> OAuthHttpResponse:
        try:
            async with self._session.post(
                url,
                data=data,
                auth=aiohttp.BasicAuth(client_id, client_secret),
                allow_redirects=False,
            ) as response:
                return await self._read_response(response)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise DiscordOAuthError("token_network_error") from error

    async def get_bearer(self, url: str, *, access_token: str) -> OAuthHttpResponse:
        try:
            async with self._session.get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
                allow_redirects=False,
            ) as response:
                return await self._read_response(response)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise DiscordOAuthError("identity_network_error") from error

    @staticmethod
    async def _read_response(response: aiohttp.ClientResponse) -> OAuthHttpResponse:
        if response.status != 200:
            return OAuthHttpResponse(response.status, b"")
        body = await response.content.read(MAX_UPSTREAM_RESPONSE_BYTES + 1)
        if len(body) > MAX_UPSTREAM_RESPONSE_BYTES:
            raise DiscordOAuthError("upstream_response_too_large", response.status)
        return OAuthHttpResponse(response.status, body)


class DiscordOAuthError(RuntimeError):
    """A deliberately content-free OAuth failure safe for logs."""

    def __init__(self, category: str, upstream_status: int | None = None) -> None:
        super().__init__(category)
        self.category = category
        self.upstream_status = upstream_status


class _TokenResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: SecretStr = Field(min_length=1)
    token_type: str = Field(min_length=1)
    refresh_token: SecretStr | None = None

    @field_validator("access_token")
    @classmethod
    def validate_access_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("access token must not be blank")
        return value


class _IdentityResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)


class DiscordOAuthClient:
    """Authorization Code + PKCE client returning only validated identity."""

    def __init__(
        self,
        http_client: OAuthHttpClient,
        *,
        client_id: int,
        client_secret: SecretStr,
        redirect_uri: str,
    ) -> None:
        self._http_client = http_client
        self._client_id = str(client_id)
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri

    def build_authorize_url(self, *, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "scope": "identify",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{DISCORD_AUTHORIZE_URL}?{query}"

    async def authenticate(self, *, code: str, code_verifier: str) -> DiscordIdentity:
        token_response = await self._http_client.post_form(
            DISCORD_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
                "code_verifier": code_verifier,
            },
            client_id=self._client_id,
            client_secret=self._client_secret.get_secret_value(),
        )
        if token_response.status != 200:
            raise DiscordOAuthError("token_bad_status", token_response.status)
        token = self._parse_token_response(token_response.body)

        identity_response = await self._http_client.get_bearer(
            DISCORD_CURRENT_USER_URL,
            access_token=token.access_token.get_secret_value(),
        )
        if identity_response.status != 200:
            raise DiscordOAuthError("identity_bad_status", identity_response.status)
        return self._parse_identity_response(identity_response.body)

    @staticmethod
    def _parse_token_response(body: bytes) -> _TokenResponse:
        try:
            payload = json.loads(body)
            token = _TokenResponse.model_validate(payload)
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            RecursionError,
        ) as error:
            raise DiscordOAuthError("token_malformed_response") from error
        if token.token_type.casefold() != "bearer":
            raise DiscordOAuthError("token_invalid_type")
        return token

    @staticmethod
    def _parse_identity_response(body: bytes) -> DiscordIdentity:
        try:
            payload = json.loads(body)
            identity = _IdentityResponse.model_validate(payload)
            if not identity.id.isascii() or not identity.id.isdecimal():
                raise ValueError("invalid Discord ID")
            user_id = int(identity.id)
            if not 0 < user_id <= DISCORD_ID_MAX:
                raise ValueError("invalid Discord ID")
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            RecursionError,
        ) as error:
            raise DiscordOAuthError("identity_malformed_response") from error
        return DiscordIdentity(user_id)


class AdminAuthenticationMiddleware:
    """Require a valid local session for every non-public ``/admin`` path."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        session_store: WebSessionStore,
        cookie_secure: bool,
    ) -> None:
        self._app = app
        self._session_store = session_store
        self._cookie_secure = cookie_secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._is_protected(scope["path"]):
            await self._app(scope, receive, send)
            return

        request = Request(scope)
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        session = self._session_store.get(session_id) if session_id else None
        if session is not None:
            state = scope.setdefault("state", {})
            state["web_session"] = session
            state["web_session_id"] = session_id
            await self._app(scope, receive, send)
            return

        if scope["method"] in {"GET", "HEAD"}:
            response = RedirectResponse("/admin/login", status_code=303)
        else:
            response = PlainTextResponse("Authentication required", status_code=401)
        if session_id:
            response.delete_cookie(
                SESSION_COOKIE_NAME,
                path=SESSION_COOKIE_PATH,
                secure=self._cookie_secure,
                httponly=True,
                samesite="lax",
            )
        await response(scope, receive, send)

    @staticmethod
    def _is_protected(path: str) -> bool:
        is_admin = path == "/admin" or path.startswith("/admin/")
        return is_admin and path not in PUBLIC_ADMIN_PATHS
