"""Business operations for versioned server-rule acceptance."""

from datetime import UTC, datetime
from typing import Protocol

from discord_stats_bot.features.rules.types import (
    RuleAcceptanceRecord,
    RuleAcceptanceResult,
    RulesAcceptanceStatistics,
    RulesetRecord,
    RulesetStatus,
    RulesetWithAcceptanceCount,
)

DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_FOOTER_LIMIT = 2048
DISCORD_EMBED_TOTAL_LIMIT = 6000
RULES_FOOTER_PREFIX = "Версия "


class RulesRepository(Protocol):
    async def lock_guild(self, guild_id: int) -> None: ...

    async def get_current_published(self, guild_id: int) -> RulesetRecord | None: ...

    async def has_accepted(
        self, guild_id: int, user_id: int, ruleset_id: int
    ) -> bool: ...

    async def create_acceptance(
        self,
        *,
        guild_id: int,
        user_id: int,
        ruleset_id: int,
        accepted_at: datetime,
    ) -> bool: ...

    async def count_acceptances(self, guild_id: int, ruleset_id: int) -> int: ...

    async def list_rulesets(
        self, guild_id: int
    ) -> tuple[RulesetWithAcceptanceCount, ...]: ...

    async def get_ruleset(
        self, guild_id: int, ruleset_id: int
    ) -> RulesetRecord | None: ...

    async def get_by_version(
        self, guild_id: int, version: str
    ) -> RulesetRecord | None: ...

    async def create_draft(self, **values: object) -> RulesetRecord: ...

    async def update_draft(
        self, ruleset_id: int, **values: object
    ) -> RulesetRecord | None: ...

    async def delete_draft(self, ruleset_id: int) -> bool: ...

    async def publish_draft(
        self, ruleset_id: int, *, published_at: datetime
    ) -> RulesetRecord: ...

    async def list_acceptances(
        self, guild_id: int, ruleset_id: int
    ) -> tuple[RuleAcceptanceRecord, ...]: ...


class NoPublishedRulesetError(LookupError):
    """Raised when a guild has no currently published ruleset."""


class RulesetNotFoundError(LookupError):
    pass


class ImmutableRulesetError(ValueError):
    pass


class DuplicateRulesetVersionError(ValueError):
    pass


class RulesetDiscordLimitError(ValueError):
    """Raised when a ruleset cannot be represented by the `/rules` embed."""

    def __init__(self, code: str, actual: int, limit: int) -> None:
        self.code = code
        self.actual = actual
        self.limit = limit
        super().__init__(
            f"Discord embed {code} is {actual} characters; limit is {limit}"
        )


def validate_ruleset_for_discord(*, version: str, title: str, content: str) -> None:
    """Validate the exact title/description/footer payload used by `/rules`."""

    footer = f"{RULES_FOOTER_PREFIX}{version}"
    parts = (
        ("title", len(title), DISCORD_EMBED_TITLE_LIMIT),
        ("description", len(content), DISCORD_EMBED_DESCRIPTION_LIMIT),
        ("footer", len(footer), DISCORD_EMBED_FOOTER_LIMIT),
    )
    for code, actual, limit in parts:
        if actual > limit:
            raise RulesetDiscordLimitError(code, actual, limit)
    total = sum(actual for _, actual, _ in parts)
    if total > DISCORD_EMBED_TOTAL_LIMIT:
        raise RulesetDiscordLimitError("total", total, DISCORD_EMBED_TOTAL_LIMIT)


class RulesService:
    """Coordinate Rules v1 reads and idempotent acceptance writes."""

    def __init__(self, repository: RulesRepository) -> None:
        self._repository = repository

    async def get_current_published(self, guild_id: int) -> RulesetRecord | None:
        self._validate_id(guild_id, "guild_id")
        return await self._repository.get_current_published(guild_id)

    async def has_accepted(self, guild_id: int, user_id: int, ruleset_id: int) -> bool:
        self._validate_id(guild_id, "guild_id")
        self._validate_id(user_id, "user_id")
        self._validate_id(ruleset_id, "ruleset_id")
        return await self._repository.has_accepted(guild_id, user_id, ruleset_id)

    async def has_accepted_current(self, guild_id: int, user_id: int) -> bool:
        current = await self.get_current_published(guild_id)
        if current is None:
            return False
        return await self.has_accepted(guild_id, user_id, current.id)

    async def accept_current(
        self,
        guild_id: int,
        user_id: int,
        *,
        accepted_at: datetime,
    ) -> RuleAcceptanceResult:
        self._validate_id(guild_id, "guild_id")
        self._validate_id(user_id, "user_id")
        if accepted_at.tzinfo is None or accepted_at.utcoffset() is None:
            raise ValueError("accepted_at must be timezone-aware")
        await self._repository.lock_guild(guild_id)
        current = await self._repository.get_current_published(guild_id)
        if current is None:
            raise NoPublishedRulesetError(f"guild {guild_id} has no published rules")
        created = await self._repository.create_acceptance(
            guild_id=guild_id,
            user_id=user_id,
            ruleset_id=current.id,
            accepted_at=accepted_at.astimezone(UTC),
        )
        return RuleAcceptanceResult(current, created)

    async def get_current_statistics(
        self, guild_id: int
    ) -> RulesAcceptanceStatistics | None:
        current = await self.get_current_published(guild_id)
        if current is None:
            return None
        count = await self._repository.count_acceptances(guild_id, current.id)
        return RulesAcceptanceStatistics(current, count)

    async def list_rulesets(
        self, guild_id: int
    ) -> tuple[RulesetWithAcceptanceCount, ...]:
        self._validate_id(guild_id, "guild_id")
        return await self._repository.list_rulesets(guild_id)

    async def get_ruleset(self, guild_id: int, ruleset_id: int) -> RulesetRecord:
        self._validate_id(guild_id, "guild_id")
        self._validate_id(ruleset_id, "ruleset_id")
        ruleset = await self._repository.get_ruleset(guild_id, ruleset_id)
        if ruleset is None:
            raise RulesetNotFoundError
        return ruleset

    async def create_draft(
        self, guild_id: int, *, version: str, actor_user_id: int, now: datetime
    ) -> RulesetRecord:
        self._validate_id(guild_id, "guild_id")
        self._validate_id(actor_user_id, "actor_user_id")
        version = self._required_text(version, "version")
        if await self._repository.get_by_version(guild_id, version) is not None:
            raise DuplicateRulesetVersionError
        current = await self._repository.get_current_published(guild_id)
        if current is None:
            raise NoPublishedRulesetError
        validate_ruleset_for_discord(
            version=version, title=current.title, content=current.content
        )
        return await self._repository.create_draft(
            guild_id=guild_id,
            version=version,
            title=current.title,
            content=current.content,
            change_summary=None,
            requires_reacceptance=False,
            reacceptance_grace_days=None,
            created_by=actor_user_id,
            created_at=self._utc(now),
        )

    async def update_draft(
        self,
        guild_id: int,
        ruleset_id: int,
        *,
        version: str,
        title: str,
        content: str,
        change_summary: str | None,
        requires_reacceptance: bool,
        reacceptance_grace_days: int | None = None,
    ) -> RulesetRecord:
        ruleset = await self.get_ruleset(guild_id, ruleset_id)
        if ruleset.status is not RulesetStatus.DRAFT:
            raise ImmutableRulesetError
        version = self._required_text(version, "version")
        conflict = await self._repository.get_by_version(guild_id, version)
        if conflict is not None and conflict.id != ruleset_id:
            raise DuplicateRulesetVersionError
        title = self._required_text(title, "title")
        content = self._required_text(content, "content")
        validate_ruleset_for_discord(version=version, title=title, content=content)
        if not requires_reacceptance:
            reacceptance_grace_days = None
        self._validate_reacceptance_grace(
            requires_reacceptance, reacceptance_grace_days
        )
        updated = await self._repository.update_draft(
            ruleset_id,
            version=version,
            title=title,
            content=content,
            change_summary=(change_summary.strip() or None) if change_summary else None,
            requires_reacceptance=requires_reacceptance,
            reacceptance_grace_days=reacceptance_grace_days,
        )
        if updated is None:
            raise ImmutableRulesetError
        return updated

    async def delete_draft(self, guild_id: int, ruleset_id: int) -> RulesetRecord:
        ruleset = await self.get_ruleset(guild_id, ruleset_id)
        if ruleset.status is not RulesetStatus.DRAFT:
            raise ImmutableRulesetError
        if not await self._repository.delete_draft(ruleset_id):
            raise ImmutableRulesetError
        return ruleset

    async def publish_draft(
        self, guild_id: int, ruleset_id: int, *, now: datetime
    ) -> tuple[RulesetRecord, RulesetRecord | None]:
        self._validate_id(guild_id, "guild_id")
        await self._repository.lock_guild(guild_id)
        draft = await self.get_ruleset(guild_id, ruleset_id)
        if draft.status is not RulesetStatus.DRAFT:
            raise ImmutableRulesetError
        validate_ruleset_for_discord(
            version=draft.version, title=draft.title, content=draft.content
        )
        previous = await self._repository.get_current_published(guild_id)
        published = await self._repository.publish_draft(
            ruleset_id, published_at=self._utc(now)
        )
        return published, previous

    async def list_acceptances(
        self, guild_id: int, ruleset_id: int
    ) -> tuple[RuleAcceptanceRecord, ...]:
        await self.get_ruleset(guild_id, ruleset_id)
        return await self._repository.list_acceptances(guild_id, ruleset_id)

    @staticmethod
    def _required_text(value: str, name: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError(f"{name} must not be blank")
        return value

    @staticmethod
    def _utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _validate_id(value: int, name: str) -> None:
        if value <= 0:
            raise ValueError(f"{name} must be positive")

    @staticmethod
    def _validate_reacceptance_grace(
        requires_reacceptance: bool, grace_days: int | None
    ) -> None:
        if grace_days is None:
            return
        if isinstance(grace_days, bool) or not 1 <= grace_days <= 365:
            raise ValueError("reacceptance_grace_days must be between 1 and 365")
