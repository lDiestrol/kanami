from dataclasses import replace
from datetime import UTC, datetime

import pytest

from discord_stats_bot.features.rules import (
    DuplicateRulesetVersionError,
    ImmutableRulesetError,
    NoPublishedRulesetError,
    RulesetDiscordLimitError,
    RulesetRecord,
    RulesetStatus,
    RulesService,
    validate_ruleset_for_discord,
)


def make_ruleset(ruleset_id: int = 1, version: str = "1.0") -> RulesetRecord:
    now = datetime(2026, 8, 25, tzinfo=UTC)
    return RulesetRecord(
        id=ruleset_id,
        guild_id=10,
        version=version,
        title="Правила сервера",
        content="Уважайте друг друга.",
        status=RulesetStatus.PUBLISHED,
        change_summary="Первоначальная редакция",
        requires_reacceptance=False,
        created_by=None,
        created_at=now,
        published_at=now,
    )


class InMemoryRulesRepository:
    def __init__(self, current: RulesetRecord | None) -> None:
        self.current = current
        self.acceptances: dict[tuple[int, int, int], datetime] = {}

    async def lock_guild(self, guild_id: int) -> None:
        assert guild_id > 0

    async def get_current_published(self, guild_id: int) -> RulesetRecord | None:
        if self.current is None or self.current.guild_id != guild_id:
            return None
        return self.current

    async def has_accepted(self, guild_id: int, user_id: int, ruleset_id: int) -> bool:
        return (guild_id, user_id, ruleset_id) in self.acceptances

    async def create_acceptance(
        self,
        *,
        guild_id: int,
        user_id: int,
        ruleset_id: int,
        accepted_at: datetime,
    ) -> bool:
        key = (guild_id, user_id, ruleset_id)
        if key in self.acceptances:
            return False
        self.acceptances[key] = accepted_at
        return True

    async def count_acceptances(self, guild_id: int, ruleset_id: int) -> int:
        return sum(
            stored_guild == guild_id and stored_ruleset == ruleset_id
            for stored_guild, _, stored_ruleset in self.acceptances
        )


@pytest.mark.asyncio
async def test_obtains_current_published_rules() -> None:
    ruleset = make_ruleset()
    service = RulesService(InMemoryRulesRepository(ruleset))

    assert await service.get_current_published(10) == ruleset


@pytest.mark.asyncio
async def test_no_published_rules_is_explicit() -> None:
    service = RulesService(InMemoryRulesRepository(None))

    assert await service.get_current_published(10) is None
    assert await service.get_current_statistics(10) is None
    with pytest.raises(NoPublishedRulesetError):
        await service.accept_current(
            10, 20, accepted_at=datetime(2026, 8, 25, tzinfo=UTC)
        )


@pytest.mark.asyncio
async def test_successful_acceptance_is_idempotent_and_counted() -> None:
    repository = InMemoryRulesRepository(make_ruleset())
    service = RulesService(repository)
    accepted_at = datetime(2026, 8, 25, 12, tzinfo=UTC)

    first = await service.accept_current(10, 20, accepted_at=accepted_at)
    second = await service.accept_current(10, 20, accepted_at=accepted_at)
    statistics = await service.get_current_statistics(10)

    assert first.newly_accepted is True
    assert second.newly_accepted is False
    assert await service.has_accepted_current(10, 20) is True
    assert statistics is not None
    assert statistics.accepted_count == 1
    assert repository.acceptances == {(10, 20, 1): accepted_at}


@pytest.mark.asyncio
async def test_acceptance_is_tied_to_exact_ruleset_version() -> None:
    repository = InMemoryRulesRepository(make_ruleset(1, "1.0"))
    service = RulesService(repository)
    accepted_at = datetime(2026, 8, 25, tzinfo=UTC)
    await service.accept_current(10, 20, accepted_at=accepted_at)

    repository.current = make_ruleset(2, "2.0")

    assert await service.has_accepted_current(10, 20) is False
    second_version = await service.accept_current(10, 20, accepted_at=accepted_at)
    assert second_version.newly_accepted is True
    assert set(repository.acceptances) == {(10, 20, 1), (10, 20, 2)}


@pytest.mark.asyncio
async def test_acceptance_timestamp_must_be_timezone_aware() -> None:
    service = RulesService(InMemoryRulesRepository(make_ruleset()))

    with pytest.raises(ValueError, match="timezone-aware"):
        await service.accept_current(10, 20, accepted_at=datetime(2026, 8, 25))


class ManagementRulesRepository(InMemoryRulesRepository):
    def __init__(self) -> None:
        super().__init__(make_ruleset())
        assert self.current is not None
        self.rules = {
            1: self.current,
            2: replace(make_ruleset(2, "0.9"), status=RulesetStatus.ARCHIVED),
        }
        self.locked = False
        self.update_succeeds = True
        self.delete_succeeds = True

    async def get_ruleset(self, guild_id: int, ruleset_id: int):
        return self.rules.get(ruleset_id)

    async def get_by_version(self, guild_id: int, version: str):
        return next(
            (item for item in self.rules.values() if item.version == version), None
        )

    async def create_draft(self, **values: object):
        item = replace(
            self.current,
            id=3,
            version=str(values["version"]),
            status=RulesetStatus.DRAFT,
            change_summary=None,
            created_by=int(values["created_by"]),
            created_at=values["created_at"],
            published_at=None,
        )
        self.rules[item.id] = item
        return item

    async def update_draft(self, ruleset_id: int, **values: object):
        if not self.update_succeeds:
            return None
        item = replace(self.rules[ruleset_id], **values)
        self.rules[ruleset_id] = item
        return item

    async def delete_draft(self, ruleset_id: int) -> bool:
        if not self.delete_succeeds:
            return False
        del self.rules[ruleset_id]
        return True

    async def publish_draft(self, ruleset_id: int, *, published_at: datetime):
        assert self.locked
        assert self.current is not None
        self.rules[self.current.id] = replace(
            self.current, status=RulesetStatus.ARCHIVED
        )
        published = replace(
            self.rules[ruleset_id],
            status=RulesetStatus.PUBLISHED,
            published_at=published_at,
        )
        self.rules[ruleset_id] = published
        self.current = published
        return published

    async def lock_guild(self, guild_id: int) -> None:
        self.locked = True


@pytest.mark.asyncio
async def test_management_create_edit_publish_and_immutability_invariants() -> None:
    repository = ManagementRulesRepository()
    service = RulesService(repository)
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)

    draft = await service.create_draft(10, version="1.1", actor_user_id=20, now=now)
    assert draft.title == repository.rules[1].title
    assert draft.content == repository.rules[1].content
    assert draft.status is RulesetStatus.DRAFT

    edited = await service.update_draft(
        10,
        draft.id,
        version="1.2",
        title="Новые правила",
        content="Новый текст",
        change_summary="Описание",
        requires_reacceptance=True,
        reacceptance_grace_days=14,
    )
    assert edited.requires_reacceptance is True
    assert edited.reacceptance_grace_days == 14

    published, previous = await service.publish_draft(10, draft.id, now=now)
    assert repository.locked is True
    assert previous is not None and previous.version == "1.0"
    assert published.status is RulesetStatus.PUBLISHED
    assert repository.rules[1].status is RulesetStatus.ARCHIVED
    assert (
        sum(
            item.status is RulesetStatus.PUBLISHED for item in repository.rules.values()
        )
        == 1
    )

    with pytest.raises(ImmutableRulesetError):
        await service.delete_draft(10, published.id)
    with pytest.raises(ImmutableRulesetError):
        await service.delete_draft(10, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requires_reacceptance", "grace_days"),
    [(True, 0), (True, 366)],
)
async def test_reacceptance_grace_validation(
    requires_reacceptance: bool, grace_days: int
) -> None:
    repository = ManagementRulesRepository()
    service = RulesService(repository)
    now = datetime(2026, 8, 25, tzinfo=UTC)
    draft = await service.create_draft(10, version="1.1", actor_user_id=20, now=now)

    with pytest.raises(ValueError):
        await service.update_draft(
            10,
            draft.id,
            version="1.1",
            title="Правила",
            content="Текст",
            change_summary=None,
            requires_reacceptance=requires_reacceptance,
            reacceptance_grace_days=grace_days,
        )


@pytest.mark.asyncio
async def test_disabling_reacceptance_normalizes_stale_grace_to_none() -> None:
    repository = ManagementRulesRepository()
    service = RulesService(repository)
    now = datetime(2026, 8, 25, tzinfo=UTC)
    draft = await service.create_draft(10, version="1.1", actor_user_id=20, now=now)

    updated = await service.update_draft(
        10,
        draft.id,
        version="1.1",
        title="Правила",
        content="Текст",
        change_summary=None,
        requires_reacceptance=False,
        reacceptance_grace_days=14,
    )

    assert updated.requires_reacceptance is False
    assert updated.reacceptance_grace_days is None


@pytest.mark.asyncio
async def test_management_duplicate_version_is_rejected_before_write() -> None:
    service = RulesService(ManagementRulesRepository())

    with pytest.raises(DuplicateRulesetVersionError):
        await service.create_draft(
            10,
            version="1.0",
            actor_user_id=20,
            now=datetime(2026, 8, 25, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    ("version", "title", "content"),
    [
        ("v", "T" * 256, "C"),
        ("v", "T", "C" * 4096),
        ("v" * 2041, "T", "C"),
        ("v" * 1641, "T" * 256, "C" * 4096),
    ],
)
def test_discord_ruleset_embed_accepts_exact_boundaries(
    version: str, title: str, content: str
) -> None:
    validate_ruleset_for_discord(version=version, title=title, content=content)


@pytest.mark.parametrize(
    ("version", "title", "content", "code"),
    [
        ("v", "T" * 257, "C", "title"),
        ("v", "T", "C" * 4097, "description"),
        ("v" * 2042, "T", "C", "footer"),
        ("v" * 1642, "T" * 256, "C" * 4096, "total"),
    ],
)
def test_discord_ruleset_embed_rejects_over_limit_values(
    version: str, title: str, content: str, code: str
) -> None:
    with pytest.raises(RulesetDiscordLimitError) as captured:
        validate_ruleset_for_discord(version=version, title=title, content=content)

    assert captured.value.code == code


@pytest.mark.asyncio
async def test_concurrent_publish_zero_row_update_becomes_immutable_conflict() -> None:
    repository = ManagementRulesRepository()
    service = RulesService(repository)
    draft = await service.create_draft(
        10,
        version="1.1",
        actor_user_id=20,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    repository.update_succeeds = False

    with pytest.raises(ImmutableRulesetError):
        await service.update_draft(
            10,
            draft.id,
            version="1.1",
            title="Правила",
            content="Текст",
            change_summary=None,
            requires_reacceptance=False,
        )


@pytest.mark.asyncio
async def test_concurrent_publish_zero_row_delete_becomes_immutable_conflict() -> None:
    repository = ManagementRulesRepository()
    service = RulesService(repository)
    draft = await service.create_draft(
        10,
        version="1.1",
        actor_user_id=20,
        now=datetime(2026, 8, 25, tzinfo=UTC),
    )
    repository.delete_succeeds = False

    with pytest.raises(ImmutableRulesetError):
        await service.delete_draft(10, draft.id)

    assert draft.id in repository.rules
