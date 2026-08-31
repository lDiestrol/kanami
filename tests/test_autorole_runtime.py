from types import SimpleNamespace

import pytest

from discord_stats_bot.discord import DiscordStatsClient


class NoOpDependency:
    pass


class RecordingAudit:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.joined: list[object] = []

    async def member_joined(self, member: object, occurred_at: object) -> None:
        del occurred_at
        self.joined.append(member)
        if self.fail:
            raise RuntimeError("audit join failure")


class RecordingAutorole:
    role_id = 30

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.members: list[object] = []

    async def handle(self, member: object) -> None:
        self.members.append(member)
        if self.fail:
            raise RuntimeError("autorole failure")


def make_client(
    *,
    audit: RecordingAudit | None,
    autorole: RecordingAutorole | None,
) -> DiscordStatsClient:
    return DiscordStatsClient(
        guild_id=10,
        reference_provisioner=NoOpDependency(),  # type: ignore[arg-type]
        voice_reconciler=NoOpDependency(),  # type: ignore[arg-type]
        voice_event_handler=NoOpDependency(),  # type: ignore[arg-type]
        audit_event_ingestor=audit,  # type: ignore[arg-type]
        autorole_handler=autorole,  # type: ignore[arg-type]
    )


def member() -> object:
    return SimpleNamespace(id=20, guild=SimpleNamespace(id=10), bot=False)


@pytest.mark.asyncio
async def test_disabled_autorole_does_not_run_on_member_join() -> None:
    audit = RecordingAudit()
    client = make_client(audit=audit, autorole=None)
    joined_member = member()

    await client.on_member_join(joined_member)  # type: ignore[arg-type]

    assert audit.joined == [joined_member]
    assert client._autorole_handler is None


@pytest.mark.asyncio
async def test_audit_failure_does_not_prevent_autorole(
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit = RecordingAudit(fail=True)
    autorole = RecordingAutorole()
    client = make_client(audit=audit, autorole=autorole)
    joined_member = member()

    await client.on_member_join(joined_member)  # type: ignore[arg-type]

    assert audit.joined == [joined_member]
    assert autorole.members == [joined_member]
    assert "Audit ingestion failed event_type=member.joined" in caplog.text


@pytest.mark.asyncio
async def test_autorole_failure_does_not_prevent_or_escape_audit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    audit = RecordingAudit()
    autorole = RecordingAutorole(fail=True)
    client = make_client(audit=audit, autorole=autorole)
    joined_member = member()

    await client.on_member_join(joined_member)  # type: ignore[arg-type]

    assert audit.joined == [joined_member]
    assert autorole.members == [joined_member]
    assert "Autorole handler failed guild_id=10 user_id=20 role_id=30" in caplog.text


@pytest.mark.asyncio
async def test_successful_autorole_does_not_create_manual_roles_audit_event() -> None:
    audit = RecordingAudit()
    autorole = RecordingAutorole()
    client = make_client(audit=audit, autorole=autorole)
    joined_member = member()

    await client.on_member_join(joined_member)  # type: ignore[arg-type]

    assert audit.joined == [joined_member]
    assert autorole.members == [joined_member]
    assert not hasattr(audit, "roles_updated")
