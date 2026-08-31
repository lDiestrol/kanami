from datetime import UTC, datetime, timedelta

import pytest

from discord_stats_bot.features.rules import (
    RulesComplianceAcceptance,
    RulesComplianceAvailability,
    RulesComplianceService,
    RulesComplianceStatus,
    RulesetRecord,
    RulesetStatus,
)

T0 = datetime(2026, 1, 1, 12, tzinfo=UTC)


def published(
    ruleset_id: int,
    version: str,
    day: int,
    *,
    required: bool = False,
    grace_days: int | None = None,
    current: bool = False,
) -> RulesetRecord:
    observed_at = T0 + timedelta(days=day)
    return RulesetRecord(
        ruleset_id,
        10,
        version,
        "Правила",
        "Текст",
        RulesetStatus.PUBLISHED if current else RulesetStatus.ARCHIVED,
        None,
        required,
        None,
        observed_at,
        observed_at,
        grace_days,
    )


def draft(ruleset_id: int, version: str, *, required: bool = True) -> RulesetRecord:
    return RulesetRecord(
        ruleset_id,
        10,
        version,
        "Черновик",
        "Текст",
        RulesetStatus.DRAFT,
        None,
        required,
        None,
        T0,
        None,
        30 if required else None,
    )


class MemoryComplianceRepository:
    def __init__(
        self,
        history: tuple[RulesetRecord, ...],
        acceptances: dict[int, tuple[RulesComplianceAcceptance, ...]] | None = None,
        members: dict[int, tuple[bool, bool]] | None = None,
    ) -> None:
        self.history = history
        self.acceptances = acceptances or {}
        self.members = members or {}

    async def list_published_history(self, guild_id: int) -> tuple[RulesetRecord, ...]:
        assert guild_id == 10
        return self.history

    async def get_latest_qualifying_acceptance(
        self,
        *,
        guild_id: int,
        user_id: int,
        checkpoint: RulesetRecord,
        current: RulesetRecord,
    ) -> RulesComplianceAcceptance | None:
        assert guild_id == 10
        rulesets = {item.id: item for item in self.history}
        lower = self._key(checkpoint)
        upper = self._key(current)
        qualifying = tuple(
            acceptance
            for acceptance in self.acceptances.get(user_id, ())
            if acceptance.ruleset_id in rulesets
            and rulesets[acceptance.ruleset_id].status
            in {RulesetStatus.PUBLISHED, RulesetStatus.ARCHIVED}
            and lower <= self._key(rulesets[acceptance.ruleset_id]) <= upper
        )
        return max(qualifying, key=lambda item: item.accepted_at, default=None)

    async def count_current_members_and_qualifying_acceptances(
        self,
        *,
        guild_id: int,
        checkpoint: RulesetRecord,
        current: RulesetRecord,
    ) -> tuple[int, int]:
        in_scope = tuple(
            user_id
            for user_id, (is_current, is_bot) in self.members.items()
            if is_current and not is_bot
        )
        compliant = 0
        for user_id in in_scope:
            if (
                await self.get_latest_qualifying_acceptance(
                    guild_id=guild_id,
                    user_id=user_id,
                    checkpoint=checkpoint,
                    current=current,
                )
                is not None
            ):
                compliant += 1
        return len(in_scope), compliant

    @staticmethod
    def _key(ruleset: RulesetRecord) -> tuple[datetime, int]:
        assert ruleset.published_at is not None
        return ruleset.published_at, ruleset.id


def accepted(ruleset: RulesetRecord, minute: int = 0) -> RulesComplianceAcceptance:
    return RulesComplianceAcceptance(
        ruleset.id, ruleset.version, T0 + timedelta(days=20, minutes=minute)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("required_flags", "accepted_index", "expected", "checkpoint_index"),
    [
        ((False,), None, RulesComplianceStatus.PENDING, 0),
        ((False,), 0, RulesComplianceStatus.COMPLIANT, 0),
        ((False, False), 0, RulesComplianceStatus.COMPLIANT, 0),
        ((False, False), None, RulesComplianceStatus.PENDING, 0),
        ((False, False, True), 1, RulesComplianceStatus.PENDING, 2),
        ((False, False, True), 2, RulesComplianceStatus.COMPLIANT, 2),
        ((False, False, True, False), 2, RulesComplianceStatus.COMPLIANT, 2),
        ((False, False, True, False), 1, RulesComplianceStatus.PENDING, 2),
    ],
)
async def test_checkpoint_and_inherited_compliance_matrix(
    required_flags: tuple[bool, ...],
    accepted_index: int | None,
    expected: RulesComplianceStatus,
    checkpoint_index: int,
) -> None:
    versions = ("1.0", "1.10", "1.2", "release-z")
    history = tuple(
        published(
            index + 1,
            versions[index],
            index,
            required=required,
            current=index == len(required_flags) - 1,
        )
        for index, required in enumerate(required_flags)
    )
    user_acceptances = (
        () if accepted_index is None else (accepted(history[accepted_index]),)
    )
    service = RulesComplianceService(
        MemoryComplianceRepository(history, {20: user_acceptances})
    )

    result = await service.get_user_compliance(10, 20, now=T0 + timedelta(days=30))

    assert result.status is expected
    assert result.required_checkpoint_ruleset_id == history[checkpoint_index].id
    assert result.required_checkpoint_version == history[checkpoint_index].version


@pytest.mark.asyncio
async def test_grace_deadline_pending_overdue_and_acceptance_precedence() -> None:
    baseline = published(1, "1.0", 0)
    checkpoint = published(2, "1.1", 1, required=True, grace_days=7, current=True)
    deadline = checkpoint.published_at + timedelta(days=7)  # type: ignore[operator]
    pending_service = RulesComplianceService(
        MemoryComplianceRepository((baseline, checkpoint))
    )
    accepted_service = RulesComplianceService(
        MemoryComplianceRepository(
            (baseline, checkpoint), {20: (accepted(checkpoint),)}
        )
    )

    pending = await pending_service.get_user_compliance(
        10, 20, now=deadline - timedelta(seconds=1)
    )
    boundary = await pending_service.get_user_compliance(10, 20, now=deadline)
    overdue = await pending_service.get_user_compliance(
        10, 20, now=deadline + timedelta(seconds=1)
    )
    compliant = await accepted_service.get_user_compliance(
        10, 20, now=deadline + timedelta(days=100)
    )

    assert pending.status is RulesComplianceStatus.PENDING
    assert boundary.status is RulesComplianceStatus.PENDING
    assert overdue.status is RulesComplianceStatus.OVERDUE
    assert compliant.status is RulesComplianceStatus.COMPLIANT
    assert compliant.deadline == deadline


@pytest.mark.asyncio
async def test_draft_checkpoint_is_ignored_and_no_published_is_typed() -> None:
    current = published(1, "1.0", 0, current=True)
    service = RulesComplianceService(
        MemoryComplianceRepository((current, draft(99, "9.9")))
    )
    empty_service = RulesComplianceService(
        MemoryComplianceRepository((draft(99, "9.9"),))
    )

    result = await service.get_user_compliance(10, 20, now=T0)
    empty = await empty_service.get_user_compliance(10, 20, now=T0)
    empty_summary = await empty_service.summarize(10, now=T0)

    assert result.required_checkpoint_ruleset_id == current.id
    assert result.status is RulesComplianceStatus.PENDING
    assert empty.availability is RulesComplianceAvailability.NO_PUBLISHED_RULES
    assert empty.status is None
    assert empty_summary.availability is RulesComplianceAvailability.NO_PUBLISHED_RULES
    assert empty_summary.total == 0


@pytest.mark.asyncio
async def test_publication_id_breaks_timestamp_tie_without_version_comparison() -> None:
    first = published(10, "9.9", 0)
    checkpoint = published(20, "1.10", 0, required=True)
    current = published(30, "1.2", 0, current=True)
    service = RulesComplianceService(
        MemoryComplianceRepository(
            (current, first, checkpoint), {20: (accepted(first),)}
        )
    )

    result = await service.get_user_compliance(10, 20, now=T0 + timedelta(days=1))

    assert result.required_checkpoint_ruleset_id == checkpoint.id
    assert result.status is RulesComplianceStatus.PENDING


@pytest.mark.asyncio
async def test_latest_qualifying_acceptance_is_selected() -> None:
    first = published(1, "1.0", 0)
    checkpoint = published(2, "1.1", 1, required=True)
    current = published(3, "1.2", 2, current=True)
    earlier = accepted(checkpoint, 1)
    latest = accepted(current, 2)
    service = RulesComplianceService(
        MemoryComplianceRepository(
            (first, checkpoint, current), {20: (latest, earlier)}
        )
    )

    result = await service.get_user_compliance(10, 20, now=T0 + timedelta(days=30))

    assert result.latest_qualifying_acceptance == latest
    assert result.status is RulesComplianceStatus.COMPLIANT


@pytest.mark.asyncio
async def test_aggregate_excludes_departed_members_and_bots() -> None:
    checkpoint = published(1, "1.0", 0, required=True, grace_days=1, current=True)
    repository = MemoryComplianceRepository(
        (checkpoint,),
        {20: (accepted(checkpoint),), 22: (), 23: (), 24: ()},
        {
            20: (True, False),
            22: (True, False),
            23: (False, False),
            24: (True, True),
        },
    )

    summary = await RulesComplianceService(repository).summarize(
        10, now=T0 + timedelta(days=2)
    )

    assert summary.total == 2
    assert summary.compliant == 1
    assert summary.pending == 0
    assert summary.overdue == 1
