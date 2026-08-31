import ast
import inspect
import textwrap
from datetime import UTC, datetime

import pytest
from sqlalchemy.dialects import postgresql

from discord_stats_bot.features.reference_data import (
    DiscordUserSnapshot,
    GuildMemberSnapshot,
    GuildReferenceSnapshot,
    GuildSnapshot,
    ReferenceDataProvisioningService,
    VoiceChannelSnapshot,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyReferenceDataRepository,
)

JOINED_AT = datetime(2026, 8, 1, tzinfo=UTC)


class InMemoryReferenceRepository:
    def __init__(self) -> None:
        self.guilds: dict[int, GuildSnapshot] = {}
        self.users: dict[int, DiscordUserSnapshot] = {}
        self.members: dict[tuple[int, int], GuildMemberSnapshot] = {}
        self.channels: dict[int, VoiceChannelSnapshot] = {}
        self.left_at: dict[tuple[int, int], datetime] = {}

    async def upsert_guild(self, guild: GuildSnapshot) -> None:
        self.guilds[guild.id] = guild

    async def upsert_users(self, users: tuple[DiscordUserSnapshot, ...]) -> None:
        self.users.update((user.id, user) for user in users)

    async def upsert_members(
        self,
        members: tuple[GuildMemberSnapshot, ...],
    ) -> None:
        self.members.update(
            ((member.guild_id, member.user_id), member) for member in members
        )

    async def upsert_voice_channels(
        self,
        channels: tuple[VoiceChannelSnapshot, ...],
    ) -> None:
        self.channels.update((channel.id, channel) for channel in channels)

    async def mark_member_left(
        self,
        *,
        guild_id: int,
        user_id: int,
        left_at: datetime,
    ) -> None:
        self.left_at[(guild_id, user_id)] = left_at


def reference_snapshot(
    *,
    guild_name: str = "Kanami",
    first_user_is_bot: bool = False,
    first_joined_at: datetime = JOINED_AT,
    voice_name: str = "General",
    voice_is_afk: bool = False,
    username: str | None = "kanami",
    global_name: str | None = "Kanami",
    nickname: str | None = "Kana",
    avatar_hash: str | None = "0123456789abcdef0123456789abcdef",
    guild_avatar_hash: str | None = "abcdef0123456789abcdef0123456789",
) -> GuildReferenceSnapshot:
    return GuildReferenceSnapshot(
        guild=GuildSnapshot(id=10, name=guild_name),
        users=(
            DiscordUserSnapshot(
                id=20,
                is_bot=first_user_is_bot,
                username=username,
                global_name=global_name,
                avatar_hash=avatar_hash,
            ),
            DiscordUserSnapshot(21, False, "second", None),
        ),
        members=(
            GuildMemberSnapshot(
                10,
                20,
                first_joined_at,
                nickname,
                guild_avatar_hash=guild_avatar_hash,
            ),
            GuildMemberSnapshot(10, 21, JOINED_AT, None),
        ),
        voice_channels=(
            VoiceChannelSnapshot(30, 10, voice_name, "voice", voice_is_afk),
            VoiceChannelSnapshot(31, 10, "Town Hall", "stage", False),
        ),
    )


@pytest.mark.asyncio
async def test_provisioning_empty_store_creates_complete_reference_snapshot() -> None:
    repository = InMemoryReferenceRepository()

    await ReferenceDataProvisioningService(repository).provision_guild(
        reference_snapshot()
    )

    assert repository.guilds == {10: GuildSnapshot(10, "Kanami")}
    assert set(repository.users) == {20, 21}
    assert set(repository.members) == {(10, 20), (10, 21)}
    assert set(repository.channels) == {30, 31}
    assert repository.channels[30].channel_kind == "voice"
    assert repository.channels[31].channel_kind == "stage"


@pytest.mark.asyncio
async def test_repeated_provisioning_is_idempotent() -> None:
    repository = InMemoryReferenceRepository()
    service = ReferenceDataProvisioningService(repository)
    snapshot = reference_snapshot()

    await service.provision_guild(snapshot)
    await service.provision_guild(snapshot)

    assert len(repository.guilds) == 1
    assert len(repository.users) == 2
    assert len(repository.members) == 2
    assert len(repository.channels) == 2


@pytest.mark.asyncio
async def test_provisioning_updates_mutable_snapshot_fields() -> None:
    repository = InMemoryReferenceRepository()
    service = ReferenceDataProvisioningService(repository)
    await service.provision_guild(reference_snapshot())
    changed_joined_at = JOINED_AT.replace(day=2)

    await service.provision_guild(
        reference_snapshot(
            guild_name="Kanami renamed",
            first_user_is_bot=True,
            first_joined_at=changed_joined_at,
            voice_name="AFK renamed",
            voice_is_afk=True,
            username="renamed",
            global_name=None,
            nickname=None,
            avatar_hash=None,
            guild_avatar_hash=None,
        )
    )

    assert repository.guilds[10].name == "Kanami renamed"
    assert repository.users[20].is_bot is True
    assert repository.users[20].username == "renamed"
    assert repository.users[20].global_name is None
    assert repository.users[20].avatar_hash is None
    assert repository.members[(10, 20)].joined_at == changed_joined_at
    assert repository.members[(10, 20)].nickname is None
    assert repository.members[(10, 20)].guild_avatar_hash is None
    assert repository.channels[30].name == "AFK renamed"
    assert repository.channels[30].is_afk is True


class RecordingSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement: object) -> None:
        self.statements.append(statement)


def compile_postgresql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


@pytest.mark.asyncio
async def test_sqlalchemy_repository_uses_conflict_updates_for_all_entities() -> None:
    session = RecordingSession()
    repository = SqlAlchemyReferenceDataRepository(session)  # type: ignore[arg-type]
    snapshot = reference_snapshot()

    await ReferenceDataProvisioningService(repository).provision_guild(snapshot)

    sql = "\n".join(compile_postgresql(statement) for statement in session.statements)
    assert len(session.statements) == 4
    assert sql.count("ON CONFLICT") == 4
    assert "INSERT INTO guilds" in sql
    assert "INSERT INTO discord_users" in sql
    assert "INSERT INTO guild_members" in sql
    assert "INSERT INTO voice_channels" in sql
    assert "left_at = %(param_" in sql
    assert "username = excluded.username" in sql
    assert "global_name = excluded.global_name" in sql
    assert "avatar_hash = excluded.avatar_hash" in sql
    assert "nickname = excluded.nickname" in sql
    assert "guild_avatar_hash = excluded.guild_avatar_hash" in sql
    assert "DELETE" not in sql


@pytest.mark.asyncio
async def test_partial_member_upsert_does_not_update_existing_guild_identity() -> None:
    session = RecordingSession()
    repository = SqlAlchemyReferenceDataRepository(session)  # type: ignore[arg-type]

    await repository.upsert_members(
        (GuildMemberSnapshot(10, 20, JOINED_AT, None, False),)
    )

    sql = compile_postgresql(session.statements[0])
    update_clause = sql.split("DO UPDATE SET", maxsplit=1)[1]
    assert "nickname" not in update_clause
    assert "guild_avatar_hash" not in update_clause
    assert "left_at" not in update_clause


@pytest.mark.asyncio
async def test_member_departure_and_rejoin_sql_update_left_at_forward_only() -> None:
    session = RecordingSession()
    repository = SqlAlchemyReferenceDataRepository(session)  # type: ignore[arg-type]

    await repository.mark_member_left(guild_id=10, user_id=20, left_at=JOINED_AT)
    await repository.upsert_members((GuildMemberSnapshot(10, 20, JOINED_AT, "Kana"),))

    departure_sql = compile_postgresql(session.statements[0])
    rejoin_sql = compile_postgresql(session.statements[1])
    assert departure_sql.startswith("UPDATE guild_members SET left_at=")
    assert "guild_members.guild_id =" in departure_sql
    assert "guild_members.user_id =" in departure_sql
    update_clause = rejoin_sql.split("DO UPDATE SET", maxsplit=1)[1]
    assert "left_at =" in update_clause
    assert "nickname = excluded.nickname" in update_clause


@pytest.mark.asyncio
async def test_same_user_keeps_distinct_nicknames_in_multiple_guilds() -> None:
    repository = InMemoryReferenceRepository()
    service = ReferenceDataProvisioningService(repository)

    await service.provision_guild(
        GuildReferenceSnapshot(
            GuildSnapshot(10, "First"),
            (DiscordUserSnapshot(20, False, "user", "Global"),),
            (GuildMemberSnapshot(10, 20, JOINED_AT, "First nick"),),
            (),
        )
    )
    await service.provision_guild(
        GuildReferenceSnapshot(
            GuildSnapshot(11, "Second"),
            (DiscordUserSnapshot(20, False, "user", "Global"),),
            (GuildMemberSnapshot(11, 20, JOINED_AT, "Second nick"),),
            (),
        )
    )

    assert repository.members[(10, 20)].nickname == "First nick"
    assert repository.members[(11, 20)].nickname == "Second nick"


def test_reference_repository_has_no_hidden_transaction_control() -> None:
    source = textwrap.dedent(inspect.getsource(SqlAlchemyReferenceDataRepository))
    tree = ast.parse(source)
    transaction_control_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"begin", "commit", "rollback"}
    ]

    assert transaction_control_calls == []
