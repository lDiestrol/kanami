"""Persistence for the single managed Rules publication per guild."""

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.rules import RulesPublicationState
from discord_stats_bot.persistence.models import GuildServerSettings


class SqlAlchemyRulesPublicationRepository:
    """Read publication configuration and persist its Discord delivery cursor."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, guild_id: int) -> RulesPublicationState:
        if guild_id <= 0:
            raise ValueError("guild_id must be positive")
        statement = select(
            GuildServerSettings.rules_publication_channel_id,
            GuildServerSettings.rules_publication_message_id,
            GuildServerSettings.rules_publication_ruleset_id,
        ).where(GuildServerSettings.guild_id == guild_id)
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return RulesPublicationState(guild_id, None, None, None)
        return RulesPublicationState(guild_id, row[0], row[1], row[2])

    async def save_delivery(
        self, *, guild_id: int, message_id: int, ruleset_id: int
    ) -> None:
        if min(guild_id, message_id, ruleset_id) <= 0:
            raise ValueError("publication identifiers must be positive")
        result = await self._session.execute(
            update(GuildServerSettings)
            .where(GuildServerSettings.guild_id == guild_id)
            .values(
                rules_publication_message_id=message_id,
                rules_publication_ruleset_id=ruleset_id,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("configured rules publication state disappeared")

    async def save_configuration(
        self, *, guild_id: int, channel_id: int | None
    ) -> None:
        if guild_id <= 0 or (channel_id is not None and channel_id <= 0):
            raise ValueError("publication configuration identifiers must be positive")
        values = {
            "guild_id": guild_id,
            "rules_publication_channel_id": channel_id,
            "rules_publication_message_id": None,
            "rules_publication_ruleset_id": None,
        }
        statement = insert(GuildServerSettings).values(**values)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[GuildServerSettings.guild_id],
                set_={key: value for key, value in values.items() if key != "guild_id"},
            )
        )
