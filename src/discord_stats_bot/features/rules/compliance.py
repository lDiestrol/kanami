"""Computed Rules compliance over immutable publication and acceptance history."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from discord_stats_bot.features.rules.types import (
    RulesComplianceAcceptance,
    RulesComplianceAvailability,
    RulesComplianceResult,
    RulesComplianceStatus,
    RulesComplianceSummary,
    RulesetRecord,
    RulesetStatus,
)


class RulesComplianceRepository(Protocol):
    async def list_published_history(
        self, guild_id: int
    ) -> tuple[RulesetRecord, ...]: ...

    async def get_latest_qualifying_acceptance(
        self,
        *,
        guild_id: int,
        user_id: int,
        checkpoint: RulesetRecord,
        current: RulesetRecord,
    ) -> RulesComplianceAcceptance | None: ...

    async def count_current_members_and_qualifying_acceptances(
        self,
        *,
        guild_id: int,
        checkpoint: RulesetRecord,
        current: RulesetRecord,
    ) -> tuple[int, int]: ...


@dataclass(frozen=True, slots=True)
class _ComplianceContext:
    current: RulesetRecord
    checkpoint: RulesetRecord
    deadline: datetime | None


class RulesComplianceService:
    """Evaluate compliance without modifying factual acceptance history."""

    def __init__(self, repository: RulesComplianceRepository) -> None:
        self._repository = repository

    async def get_user_compliance(
        self, guild_id: int, user_id: int, *, now: datetime
    ) -> RulesComplianceResult:
        self._validate_id(guild_id, "guild_id")
        self._validate_id(user_id, "user_id")
        now = self._utc(now)
        context = await self._context(guild_id)
        if context is None:
            return RulesComplianceResult(
                RulesComplianceAvailability.NO_PUBLISHED_RULES,
                guild_id,
                user_id,
            )
        acceptance = await self._repository.get_latest_qualifying_acceptance(
            guild_id=guild_id,
            user_id=user_id,
            checkpoint=context.checkpoint,
            current=context.current,
        )
        status = self._status(acceptance is not None, context.deadline, now)
        return RulesComplianceResult(
            RulesComplianceAvailability.AVAILABLE,
            guild_id,
            user_id,
            context.current.id,
            context.current.version,
            context.checkpoint.id,
            context.checkpoint.version,
            context.checkpoint.requires_reacceptance,
            status,
            acceptance,
            context.deadline,
        )

    async def summarize(
        self, guild_id: int, *, now: datetime
    ) -> RulesComplianceSummary:
        self._validate_id(guild_id, "guild_id")
        now = self._utc(now)
        context = await self._context(guild_id)
        if context is None:
            return RulesComplianceSummary(
                RulesComplianceAvailability.NO_PUBLISHED_RULES, guild_id
            )
        (
            total,
            compliant,
        ) = await self._repository.count_current_members_and_qualifying_acceptances(
            guild_id=guild_id,
            checkpoint=context.checkpoint,
            current=context.current,
        )
        if not 0 <= compliant <= total:
            raise ValueError("compliant member count must be within total")
        outstanding = total - compliant
        overdue = (
            outstanding
            if context.deadline is not None and now > context.deadline
            else 0
        )
        return RulesComplianceSummary(
            RulesComplianceAvailability.AVAILABLE,
            guild_id,
            total,
            compliant,
            outstanding - overdue,
            overdue,
            context.current.id,
            context.current.version,
            context.checkpoint.id,
            context.checkpoint.version,
            context.checkpoint.requires_reacceptance,
            context.deadline,
        )

    async def _context(self, guild_id: int) -> _ComplianceContext | None:
        history = tuple(
            ruleset
            for ruleset in await self._repository.list_published_history(guild_id)
            if ruleset.status in {RulesetStatus.PUBLISHED, RulesetStatus.ARCHIVED}
            and ruleset.published_at is not None
        )
        if not history:
            return None
        ordered = tuple(sorted(history, key=self._publication_key))
        current = next(
            (
                item
                for item in reversed(ordered)
                if item.status is RulesetStatus.PUBLISHED
            ),
            None,
        )
        if current is None:
            return None
        through_current = tuple(
            item
            for item in ordered
            if self._publication_key(item) <= self._publication_key(current)
        )
        checkpoint = next(
            (item for item in reversed(through_current) if item.requires_reacceptance),
            through_current[0],
        )
        deadline = None
        if checkpoint.reacceptance_grace_days is not None:
            if not checkpoint.requires_reacceptance:
                raise ValueError("grace period requires a reacceptance checkpoint")
            if not 1 <= checkpoint.reacceptance_grace_days <= 365:
                raise ValueError("reacceptance grace days must be between 1 and 365")
            deadline = self._utc(checkpoint.published_at) + timedelta(
                days=checkpoint.reacceptance_grace_days
            )
        return _ComplianceContext(current, checkpoint, deadline)

    @classmethod
    def _publication_key(cls, ruleset: RulesetRecord) -> tuple[datetime, int]:
        if ruleset.published_at is None:
            raise ValueError("published ruleset must have published_at")
        return cls._utc(ruleset.published_at), ruleset.id

    @staticmethod
    def _status(
        accepted: bool, deadline: datetime | None, now: datetime
    ) -> RulesComplianceStatus:
        if accepted:
            return RulesComplianceStatus.COMPLIANT
        if deadline is not None and now > deadline:
            return RulesComplianceStatus.OVERDUE
        return RulesComplianceStatus.PENDING

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _validate_id(value: int, name: str) -> None:
        if value <= 0:
            raise ValueError(f"{name} must be positive")
