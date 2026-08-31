"""Narrow async-session lifecycle fakes for command tests."""


class FakeTransaction:
    def __init__(self, session: "FakeSession") -> None:
        self._session = session

    async def __aenter__(self) -> None:
        self._session.transaction_active = True
        self._session.events.append("begin")

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        self._session.events.append("rollback" if exc_type else "commit")
        self._session.rolled_back = exc_type is not None
        self._session.transaction_active = False


class FakeSession:
    def __init__(
        self,
        events: list[object],
        *,
        execute_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.closed = False
        self.transaction_active = False
        self.rolled_back = False
        self.execute_error = execute_error

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.transaction_active:
            self.events.append("rollback")
            self.transaction_active = False
            self.rolled_back = True
        self.events.append("close")
        self.closed = True

    async def connection(self, **kwargs: object) -> object:
        self.events.append(("connection", kwargs))
        self.transaction_active = True
        return object()

    async def execute(self, statement: object) -> object:
        self.events.append(("execute", str(statement)))
        if self.execute_error is not None:
            raise self.execute_error
        return object()

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)


class FakeSessionFactory:
    def __init__(
        self,
        events: list[object] | None = None,
        *,
        execute_error: Exception | None = None,
    ) -> None:
        self.events = events if events is not None else []
        self.sessions: list[FakeSession] = []
        self.calls = 0
        self.execute_error = execute_error

    def __call__(self) -> FakeSession:
        self.calls += 1
        session = FakeSession(self.events, execute_error=self.execute_error)
        self.sessions.append(session)
        return session
