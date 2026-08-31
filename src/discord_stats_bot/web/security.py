"""Central response hardening and bounded write-action abuse protection."""

from collections import OrderedDict, deque
from collections.abc import Callable
from time import monotonic

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; "
        "img-src 'self' https://cdn.discordapp.com "
        "https://media.discordapp.net data:; "
        "style-src 'unsafe-inline'; "
        "script-src 'none'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "Permissions-Policy": (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class AdminSecurityHeadersMiddleware:
    """Apply one conservative policy to every Web Admin HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/admin"):
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self._app(scope, receive, send_with_headers)


class WebWriteRateLimiter:
    """Small process-local sliding-window limiter with bounded key storage."""

    def __init__(
        self,
        *,
        limit: int = 10,
        window_seconds: float = 60.0,
        capacity: int = 1_024,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._capacity = capacity
        self._clock = clock
        self._events: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, key: str) -> bool:
        now = self._clock()
        events = self._events.pop(key, deque())
        cutoff = now - self._window_seconds
        while events and events[0] <= cutoff:
            events.popleft()
        allowed = len(events) < self._limit
        if allowed:
            events.append(now)
        self._events[key] = events
        while len(self._events) > self._capacity:
            self._events.popitem(last=False)
        return allowed
