from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from discord_stats_bot.config import DatabaseSettings


@dataclass(slots=True)
class DatabaseResources:
    """Explicitly created async database resources without transaction policy."""

    engine: AsyncEngine = field(repr=False)
    session_factory: async_sessionmaker[AsyncSession] = field(repr=False)

    def __repr__(self) -> str:
        return "DatabaseResources(engine=<hidden>, session_factory=<hidden>)"

    async def dispose(self) -> None:
        """Release the engine pool without opening a new connection."""

        await self.engine.dispose()


def create_database_resources(
    settings: DatabaseSettings, *, read_only: bool = False
) -> DatabaseResources:
    """Create an async engine and session factory without connecting to PostgreSQL."""

    engine_options: dict[str, object] = {}
    if read_only:
        engine_options["connect_args"] = {
            "server_settings": {"default_transaction_read_only": "on"}
        }
    engine = create_async_engine(
        settings.database_url.get_secret_value(),
        **engine_options,
    )
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return DatabaseResources(engine=engine, session_factory=session_factory)
