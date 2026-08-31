import ast
import inspect
import textwrap
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from discord_stats_bot.features.web_admin_access import (
    WEB_ADMIN_ACCESS_GRANTED_EVENT_TYPE,
    WEB_ADMIN_ACCESS_REVOKED_EVENT_TYPE,
    WEB_ADMIN_AUDIT_CATEGORY,
    WebAdminAccessGrantRecord,
    WebAdminAccessService,
)

T0 = datetime(2026, 8, 22, 12, tzinfo=UTC)


def access_record(
    *,
    revoked_by_user_id: int | None = None,
    revoked_at: datetime | None = None,
) -> WebAdminAccessGrantRecord:
    return WebAdminAccessGrantRecord(
        id=7,
        guild_id=10,
        user_id=20,
        granted_by_user_id=30,
        granted_at=T0,
        revoked_by_user_id=revoked_by_user_id,
        revoked_at=revoked_at,
    )


class FakeAccessRepository:
    def __init__(
        self,
        *,
        grant_result: WebAdminAccessGrantRecord | None = None,
        revoke_result: WebAdminAccessGrantRecord | None = None,
    ) -> None:
        self.grant_result = grant_result
        self.revoke_result = revoke_result
        self.grant_calls: list[dict[str, object]] = []
        self.revoke_calls: list[dict[str, object]] = []

    async def grant(self, **kwargs: object) -> WebAdminAccessGrantRecord | None:
        self.grant_calls.append(dict(kwargs))
        return self.grant_result

    async def revoke(self, **kwargs: object) -> WebAdminAccessGrantRecord | None:
        self.revoke_calls.append(dict(kwargs))
        return self.revoke_result


class FakeAuditRepository:
    def __init__(self, *, create_error: Exception | None = None) -> None:
        self.create_error = create_error
        self.created: list[tuple[object, datetime | None]] = []
        self.suppressed: list[tuple[tuple[int, ...], datetime]] = []

    async def create(
        self,
        draft: object,
        *,
        expires_at: datetime | None,
    ) -> object:
        if self.create_error is not None:
            raise self.create_error
        self.created.append((draft, expires_at))
        return SimpleNamespace(id=99)

    async def create_many(self, events: object) -> tuple[object, ...]:
        raise AssertionError("create_many must not be used")

    async def mark_delivery_suppressed(
        self,
        event_ids: tuple[int, ...],
        suppressed_at: datetime,
    ) -> None:
        self.suppressed.append((event_ids, suppressed_at))


@pytest.mark.asyncio
async def test_grant_creates_one_history_only_audit_event() -> None:
    record = access_record()
    access = FakeAccessRepository(grant_result=record)
    audit = FakeAuditRepository()
    service = WebAdminAccessService(access, audit)  # type: ignore[arg-type]

    result = await service.grant(
        guild_id=10,
        user_id=20,
        actor_user_id=30,
        occurred_at=T0,
    )

    assert result is record
    assert access.grant_calls == [
        {
            "guild_id": 10,
            "user_id": 20,
            "actor_user_id": 30,
            "granted_at": T0,
        }
    ]
    assert len(audit.created) == 1
    draft, expires_at = audit.created[0]
    assert expires_at is None
    assert draft.category == WEB_ADMIN_AUDIT_CATEGORY  # type: ignore[attr-defined]
    assert draft.event_type == WEB_ADMIN_ACCESS_GRANTED_EVENT_TYPE  # type: ignore[attr-defined]
    assert draft.subject_type == "user"  # type: ignore[attr-defined]
    assert draft.subject_id == 20  # type: ignore[attr-defined]
    assert draft.actor_user_id == 30  # type: ignore[attr-defined]
    assert draft.before_data == {"managed_access": False}  # type: ignore[attr-defined]
    assert draft.after_data == {"managed_access": True}  # type: ignore[attr-defined]
    assert draft.details_data == {"grant_id": 7}  # type: ignore[attr-defined]
    assert audit.suppressed == [((99,), T0)]


@pytest.mark.asyncio
async def test_duplicate_grant_does_not_create_audit_event() -> None:
    access = FakeAccessRepository(grant_result=None)
    audit = FakeAuditRepository()
    service = WebAdminAccessService(access, audit)  # type: ignore[arg-type]

    result = await service.grant(
        guild_id=10,
        user_id=20,
        actor_user_id=30,
        occurred_at=T0,
    )

    assert result is None
    assert audit.created == []
    assert audit.suppressed == []


@pytest.mark.asyncio
async def test_revoke_creates_one_history_only_audit_event() -> None:
    record = access_record(revoked_by_user_id=30, revoked_at=T0)
    access = FakeAccessRepository(revoke_result=record)
    audit = FakeAuditRepository()
    service = WebAdminAccessService(access, audit)  # type: ignore[arg-type]

    result = await service.revoke(
        guild_id=10,
        user_id=20,
        actor_user_id=30,
        occurred_at=T0,
    )

    assert result is record
    assert len(audit.created) == 1
    draft, expires_at = audit.created[0]
    assert expires_at is None
    assert draft.event_type == WEB_ADMIN_ACCESS_REVOKED_EVENT_TYPE  # type: ignore[attr-defined]
    assert draft.before_data == {"managed_access": True}  # type: ignore[attr-defined]
    assert draft.after_data == {"managed_access": False}  # type: ignore[attr-defined]
    assert draft.details_data == {"grant_id": 7}  # type: ignore[attr-defined]
    assert audit.suppressed == [((99,), T0)]


@pytest.mark.asyncio
async def test_duplicate_revoke_does_not_create_audit_event() -> None:
    access = FakeAccessRepository(revoke_result=None)
    audit = FakeAuditRepository()
    service = WebAdminAccessService(access, audit)  # type: ignore[arg-type]

    result = await service.revoke(
        guild_id=10,
        user_id=20,
        actor_user_id=30,
        occurred_at=T0,
    )

    assert result is None
    assert audit.created == []
    assert audit.suppressed == []


@pytest.mark.asyncio
async def test_audit_failure_is_not_swallowed() -> None:
    access = FakeAccessRepository(grant_result=access_record())
    audit = FakeAuditRepository(create_error=RuntimeError("audit failed"))
    service = WebAdminAccessService(access, audit)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="audit failed"):
        await service.grant(
            guild_id=10,
            user_id=20,
            actor_user_id=30,
            occurred_at=T0,
        )

    assert len(access.grant_calls) == 1
    assert audit.suppressed == []


@pytest.mark.asyncio
async def test_naive_timestamp_is_rejected_before_mutation() -> None:
    access = FakeAccessRepository(grant_result=access_record())
    audit = FakeAuditRepository()
    service = WebAdminAccessService(access, audit)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="occurred_at must be timezone-aware"):
        await service.grant(
            guild_id=10,
            user_id=20,
            actor_user_id=30,
            occurred_at=datetime(2026, 8, 22, 12),
        )

    assert access.grant_calls == []
    assert audit.created == []


def test_service_has_no_hidden_transaction_control() -> None:
    source = textwrap.dedent(inspect.getsource(WebAdminAccessService))
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"begin", "commit", "rollback"}
    ]

    assert calls == []
