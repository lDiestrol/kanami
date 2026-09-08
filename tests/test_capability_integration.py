"""Opt-in PostgreSQL guarantees using isolated tables shared by real sessions."""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from discord_stats_bot.config import Settings
from discord_stats_bot.features.capabilities import (
    VOICE_MOVE,
    AuthorizationReason,
    CapabilityAuthorizationService,
    CapabilityMutationService,
    CapabilitySubjectType,
)
from discord_stats_bot.persistence.database import create_database_resources
from discord_stats_bot.persistence.models import (
    AuditEvent,
    Base,
    Guild,
    GuildCapabilityGrantModel,
    GuildCapabilityPolicyModel,
)
from discord_stats_bot.persistence.repositories import (
    SqlAlchemyAuditEventRepository,
    SqlAlchemyCapabilityRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
T0 = datetime(2026, 9, 8, 12, tzinfo=UTC)


@pytest_asyncio.fixture
async def capability_db():
    test_database_url = os.getenv("TEST_DATABASE_URL")
    if not test_database_url:
        pytest.skip("TEST_DATABASE_URL is not set")
    settings = Settings(
        _env_file=None,
        DISCORD_TOKEN="integration-test-placeholder",
        DISCORD_GUILD_ID=1,
        DATABASE_URL=test_database_url,
    )
    resources = create_database_resources(settings)
    schema = f"test_capability_{uuid4().hex}"
    guild_id = uuid4().int % (2**62) + 1
    engine = resources.engine.execution_options(
        isolation_level="READ COMMITTED", schema_translate_map={None: schema}
    )
    created = False
    try:
        async with engine.begin() as connection:
            await connection.execute(CreateSchema(schema))
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
                        Guild.__table__,
                        GuildCapabilityPolicyModel.__table__,
                        GuildCapabilityGrantModel.__table__,
                        AuditEvent.__table__,
                    ],
                )
            )
            await connection.execute(Guild.__table__.insert().values(id=guild_id))
        created = True
        yield async_sessionmaker(engine, expire_on_commit=False), guild_id
    finally:
        try:
            if created:
                async with engine.begin() as connection:
                    await connection.execute(DropSchema(schema, cascade=True))
        finally:
            await resources.dispose()


async def mutate(session, guild_id, operation, subject_type=CapabilitySubjectType.USER):
    service = CapabilityMutationService(
        SqlAlchemyCapabilityRepository(session), SqlAlchemyAuditEventRepository(session)
    )
    return await apply_mutation(service, guild_id, operation, subject_type)


async def apply_mutation(service, guild_id, operation, subject_type):
    values = dict(
        guild_id=guild_id, capability=VOICE_MOVE, actor_user_id=99, occurred_at=T0
    )
    if operation in {"enable", "disable"}:
        return await service.set_enabled(**values, enabled=operation == "enable")
    if operation == "revoke":
        return await service.revoke(**values, subject_type=subject_type, subject_id=20)
    grant = (
        service.grant_user
        if subject_type is CapabilitySubjectType.USER
        else service.grant_role
    )
    return await grant(**values, subject_id=20)


async def persisted_state(factory, guild_id):
    async with factory() as session:
        repository = SqlAlchemyCapabilityRepository(session)
        return (
            await repository.get_policy(guild_id, VOICE_MOVE),
            await repository.list_grants(guild_id, VOICE_MOVE),
            tuple(
                (
                    await session.execute(
                        select(AuditEvent.event_type).order_by(AuditEvent.id)
                    )
                )
                .scalars()
                .all()
            ),
        )


@pytest.mark.parametrize("subject_type", list(CapabilitySubjectType))
async def test_authorization_cannot_mix_enabled_policy_with_later_grant(
    capability_db, subject_type
):
    factory, guild_id = capability_db
    async with factory.begin() as session:
        await mutate(session, guild_id, "enable")

    read_complete = asyncio.Event()
    writes_committed = asyncio.Event()

    class PausedReadSession(AsyncSession):
        reads = 0

        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if statement.is_select and "guild_capability_" in str(statement):
                self.reads += 1
                if self.reads == 1:
                    # Pause after the real SELECT has captured its snapshot. The
                    # old implementation pauses here after reading only policy,
                    # then incorrectly sees the later committed USER/ROLE grant.
                    read_complete.set()
                    await writes_committed.wait()
            return result

    async def authorize():
        async with PausedReadSession(bind=factory.kw["bind"]) as session:
            async with session.begin():
                assert (
                    await session.execute(text("SHOW transaction_isolation"))
                ).scalar_one() == "read committed"
                decision = await CapabilityAuthorizationService(
                    SqlAlchemyCapabilityRepository(session)
                ).authorize(
                    guild_id=guild_id,
                    capability=VOICE_MOVE,
                    user_id=20,
                    role_ids=(20,)
                    if subject_type is CapabilitySubjectType.ROLE
                    else (),
                    is_guild_owner=False,
                )
                assert not decision.allowed
                assert decision.reason is AuthorizationReason.NOT_GRANTED
                assert session.reads == 1
                assert session.in_transaction()

    async def change_state():
        await read_complete.wait()
        async with factory.begin() as session:
            await mutate(session, guild_id, "disable")
        async with factory.begin() as session:
            await mutate(session, guild_id, "grant", subject_type)
        writes_committed.set()

    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        group.create_task(authorize())
        group.create_task(change_state())
    policy, grants, _ = await persisted_state(factory, guild_id)
    assert policy.enabled is False
    assert len(grants) == 1


async def overlapping_mutations(
    factory, guild_id, first, second, subject_type, *, second_policy_reads=None
):
    """Prove the second backend waits on the first before letting it commit."""
    first_finished = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    second_pid = None

    async def first_writer():
        async with factory.begin() as session:
            result = await mutate(session, guild_id, first, subject_type)
            first_finished.set()
            await release_first.wait()
        return result.changed

    async def second_writer():
        nonlocal second_pid
        await first_finished.wait()
        async with factory.begin() as session:
            second_pid = (
                await session.execute(text("SELECT pg_backend_pid()"))
            ).scalar_one()
            second_started.set()
            if second_policy_reads is None:
                result = await mutate(session, guild_id, second, subject_type)
            else:

                class ObservingCapabilityRepository(SqlAlchemyCapabilityRepository):
                    async def get_policy(self, guild_id, capability):
                        policy = await super().get_policy(guild_id, capability)
                        second_policy_reads.append(
                            None if policy is None else policy.enabled
                        )
                        return policy

                result = await apply_mutation(
                    CapabilityMutationService(
                        ObservingCapabilityRepository(session),
                        SqlAlchemyAuditEventRepository(session),
                    ),
                    guild_id,
                    second,
                    subject_type,
                )
        return result.changed

    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        task_one = group.create_task(first_writer())
        task_two = group.create_task(second_writer())
        await second_started.wait()
        async with factory() as observer:
            while not (
                await observer.execute(
                    text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                    {"pid": second_pid},
                )
            ).scalar_one():
                await asyncio.sleep(0.01)
        release_first.set()
    return task_one.result(), task_two.result()


async def policy_change_audits(factory, guild_id):
    async with factory() as session:
        return tuple(
            (
                await session.execute(
                    select(AuditEvent.before_data, AuditEvent.after_data)
                    .where(
                        AuditEvent.guild_id == guild_id,
                        AuditEvent.event_type == "web_admin.capability_policy_changed",
                    )
                    .order_by(AuditEvent.id)
                )
            ).all()
        )


@pytest.mark.parametrize(
    ("first", "second", "initial_enabled"),
    [("enable", "disable", False), ("disable", "enable", True)],
)
async def test_concurrent_policy_mutations_follow_committed_state_and_audit_order(
    capability_db, first, second, initial_enabled
):
    factory, guild_id = capability_db
    if initial_enabled:
        async with factory.begin() as session:
            assert (await mutate(session, guild_id, "enable")).changed is True
    before_audits = await policy_change_audits(factory, guild_id)
    second_policy_reads = []

    assert await overlapping_mutations(
        factory,
        guild_id,
        first,
        second,
        CapabilitySubjectType.USER,
        second_policy_reads=second_policy_reads,
    ) == (True, True)

    policy, _, _ = await persisted_state(factory, guild_id)
    assert policy.enabled is (second == "enable")
    assert second_policy_reads == [first == "enable"]
    new_audits = (await policy_change_audits(factory, guild_id))[len(before_audits) :]
    assert new_audits == (
        ({"enabled": first == "disable"}, {"enabled": first == "enable"}),
        ({"enabled": second == "disable"}, {"enabled": second == "enable"}),
    )


@pytest.mark.parametrize("subject_type", list(CapabilitySubjectType))
@pytest.mark.parametrize("operation", ["grant", "revoke"])
async def test_identical_concurrent_mutations_have_one_change_and_one_audit(
    capability_db, subject_type, operation
):
    factory, guild_id = capability_db
    if operation == "revoke":
        async with factory.begin() as session:
            await mutate(session, guild_id, "grant", subject_type)
    assert await overlapping_mutations(
        factory, guild_id, operation, operation, subject_type
    ) == (True, False)
    _, grants, audits = await persisted_state(factory, guild_id)
    assert len(grants) == (1 if operation == "grant" else 0)
    event = f"web_admin.capability_{'granted' if operation == 'grant' else 'revoked'}"
    assert audits.count(event) == 1
    assert len(audits) == (1 if operation == "grant" else 2)


@pytest.mark.parametrize("subject_type", list(CapabilitySubjectType))
@pytest.mark.parametrize("first,second", [("grant", "revoke"), ("revoke", "grant")])
async def test_concurrent_grant_revoke_follow_transaction_order(
    capability_db, subject_type, first, second
):
    factory, guild_id = capability_db
    if first == "revoke":
        async with factory.begin() as session:
            await mutate(session, guild_id, "grant", subject_type)
    _, _, before = await persisted_state(factory, guild_id)
    assert await overlapping_mutations(
        factory, guild_id, first, second, subject_type
    ) == (True, True)
    _, grants, audits = await persisted_state(factory, guild_id)
    assert len(grants) == (1 if second == "grant" else 0)
    assert audits[len(before) :] == tuple(
        f"web_admin.capability_{'granted' if operation == 'grant' else 'revoked'}"
        for operation in (first, second)
    )


@pytest.mark.parametrize("failure_point", ["create", "suppress"])
@pytest.mark.parametrize(
    "operation,subject_type",
    [
        ("enable", CapabilitySubjectType.USER),
        ("disable", CapabilitySubjectType.USER),
        ("grant", CapabilitySubjectType.USER),
        ("grant", CapabilitySubjectType.ROLE),
        ("revoke", CapabilitySubjectType.USER),
        ("revoke", CapabilitySubjectType.ROLE),
    ],
)
async def test_audit_failure_rolls_back_real_mutation_and_audit(
    capability_db, failure_point, operation, subject_type
):
    factory, guild_id = capability_db
    if operation in {"revoke", "disable"}:
        async with factory.begin() as session:
            await mutate(
                session,
                guild_id,
                "grant" if operation == "revoke" else "enable",
                subject_type,
            )
    before = await persisted_state(factory, guild_id)

    class FailingAuditRepository(SqlAlchemyAuditEventRepository):
        async def create(self, *args, **kwargs):
            record = await super().create(*args, **kwargs)
            if failure_point == "create":
                raise RuntimeError("audit failed after insert")
            return record

        async def mark_delivery_suppressed(self, *args, **kwargs):
            await super().mark_delivery_suppressed(*args, **kwargs)
            raise RuntimeError("audit failed after suppression")

    with pytest.raises(RuntimeError, match="audit failed"):
        async with factory.begin() as session:
            service = CapabilityMutationService(
                SqlAlchemyCapabilityRepository(session), FailingAuditRepository(session)
            )
            await apply_mutation(service, guild_id, operation, subject_type)
    assert await persisted_state(factory, guild_id) == before
