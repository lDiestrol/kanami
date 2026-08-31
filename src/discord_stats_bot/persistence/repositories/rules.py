"""Caller-owned SQLAlchemy repository for Rules v1."""

from datetime import datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from discord_stats_bot.features.rules import (
    RuleAcceptanceRecord,
    RulesComplianceAcceptance,
    RulesetRecord,
    RulesetStatus,
    RulesetWithAcceptanceCount,
)
from discord_stats_bot.persistence.models import (
    DiscordUser,
    Guild,
    GuildMember,
    RuleAcceptance,
    Ruleset,
)


class SqlAlchemyRulesRepository:
    """Read current rules and create idempotent acceptance rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock_guild(self, guild_id: int) -> None:
        await self._session.execute(
            select(Guild.id).where(Guild.id == guild_id).with_for_update()
        )

    async def get_current_published(self, guild_id: int) -> RulesetRecord | None:
        model = (
            await self._session.execute(
                select(Ruleset).where(
                    Ruleset.guild_id == guild_id,
                    Ruleset.status == RulesetStatus.PUBLISHED.value,
                )
            )
        ).scalar_one_or_none()
        return None if model is None else self._to_record(model)

    async def list_published_history(self, guild_id: int) -> tuple[RulesetRecord, ...]:
        models = (
            (
                await self._session.execute(
                    select(Ruleset)
                    .where(
                        Ruleset.guild_id == guild_id,
                        Ruleset.status.in_(
                            (
                                RulesetStatus.PUBLISHED.value,
                                RulesetStatus.ARCHIVED.value,
                            )
                        ),
                    )
                    .order_by(Ruleset.published_at.asc(), Ruleset.id.asc())
                )
            )
            .scalars()
            .all()
        )
        return tuple(self._to_record(model) for model in models)

    async def get_latest_qualifying_acceptance(
        self,
        *,
        guild_id: int,
        user_id: int,
        checkpoint: RulesetRecord,
        current: RulesetRecord,
    ) -> RulesComplianceAcceptance | None:
        statement = (
            select(Ruleset.id, Ruleset.version, RuleAcceptance.accepted_at)
            .join(
                RuleAcceptance,
                (RuleAcceptance.guild_id == Ruleset.guild_id)
                & (RuleAcceptance.ruleset_id == Ruleset.id),
            )
            .where(
                Ruleset.guild_id == guild_id,
                RuleAcceptance.user_id == user_id,
                self._published_between(checkpoint, current),
            )
            .order_by(
                RuleAcceptance.accepted_at.desc(),
                Ruleset.published_at.desc(),
                Ruleset.id.desc(),
            )
            .limit(1)
        )
        row = (await self._session.execute(statement)).one_or_none()
        if row is None:
            return None
        return RulesComplianceAcceptance(int(row.id), str(row.version), row.accepted_at)

    async def count_current_members_and_qualifying_acceptances(
        self,
        *,
        guild_id: int,
        checkpoint: RulesetRecord,
        current: RulesetRecord,
    ) -> tuple[int, int]:
        current_members = (
            select(GuildMember.user_id)
            .join(DiscordUser, DiscordUser.id == GuildMember.user_id)
            .where(
                GuildMember.guild_id == guild_id,
                GuildMember.left_at.is_(None),
                DiscordUser.is_bot.is_(False),
            )
            .subquery("current_rules_members")
        )
        qualifying_users = (
            select(RuleAcceptance.user_id)
            .join(
                Ruleset,
                (Ruleset.guild_id == RuleAcceptance.guild_id)
                & (Ruleset.id == RuleAcceptance.ruleset_id),
            )
            .where(
                RuleAcceptance.guild_id == guild_id,
                self._published_between(checkpoint, current),
            )
            .distinct()
            .subquery("qualifying_rules_acceptances")
        )
        row = (
            await self._session.execute(
                select(
                    func.count(current_members.c.user_id).label("total"),
                    func.count(qualifying_users.c.user_id).label("compliant"),
                ).outerjoin(
                    qualifying_users,
                    qualifying_users.c.user_id == current_members.c.user_id,
                )
            )
        ).one()
        return int(row.total), int(row.compliant)

    async def has_accepted(self, guild_id: int, user_id: int, ruleset_id: int) -> bool:
        statement = select(
            select(RuleAcceptance.id)
            .where(
                RuleAcceptance.guild_id == guild_id,
                RuleAcceptance.user_id == user_id,
                RuleAcceptance.ruleset_id == ruleset_id,
            )
            .exists()
        )
        return bool((await self._session.execute(statement)).scalar_one())

    async def create_acceptance(
        self,
        *,
        guild_id: int,
        user_id: int,
        ruleset_id: int,
        accepted_at: datetime,
    ) -> bool:
        statement = (
            insert(RuleAcceptance)
            .values(
                guild_id=guild_id,
                user_id=user_id,
                ruleset_id=ruleset_id,
                accepted_at=accepted_at,
            )
            .on_conflict_do_nothing(constraint="uq_rule_acceptances_guild_user_ruleset")
            .returning(RuleAcceptance.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def count_acceptances(self, guild_id: int, ruleset_id: int) -> int:
        statement = select(func.count(RuleAcceptance.id)).where(
            RuleAcceptance.guild_id == guild_id,
            RuleAcceptance.ruleset_id == ruleset_id,
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def list_rulesets(
        self, guild_id: int
    ) -> tuple[RulesetWithAcceptanceCount, ...]:
        rows = (
            await self._session.execute(
                select(Ruleset, func.count(RuleAcceptance.id))
                .outerjoin(
                    RuleAcceptance,
                    (RuleAcceptance.guild_id == Ruleset.guild_id)
                    & (RuleAcceptance.ruleset_id == Ruleset.id),
                )
                .where(Ruleset.guild_id == guild_id)
                .group_by(Ruleset.id)
                .order_by(Ruleset.created_at.desc(), Ruleset.id.desc())
            )
        ).all()
        return tuple(
            RulesetWithAcceptanceCount(self._to_record(model), int(count))
            for model, count in rows
        )

    async def get_ruleset(self, guild_id: int, ruleset_id: int) -> RulesetRecord | None:
        model = (
            await self._session.execute(
                select(Ruleset).where(
                    Ruleset.guild_id == guild_id, Ruleset.id == ruleset_id
                )
            )
        ).scalar_one_or_none()
        return None if model is None else self._to_record(model)

    async def get_by_version(self, guild_id: int, version: str) -> RulesetRecord | None:
        model = (
            await self._session.execute(
                select(Ruleset).where(
                    Ruleset.guild_id == guild_id, Ruleset.version == version
                )
            )
        ).scalar_one_or_none()
        return None if model is None else self._to_record(model)

    async def create_draft(self, **values: object) -> RulesetRecord:
        model = Ruleset(status=RulesetStatus.DRAFT.value, published_at=None, **values)
        self._session.add(model)
        await self._session.flush()
        return self._to_record(model)

    async def update_draft(
        self, ruleset_id: int, **values: object
    ) -> RulesetRecord | None:
        model = (
            await self._session.execute(
                update(Ruleset)
                .where(
                    Ruleset.id == ruleset_id,
                    Ruleset.status == RulesetStatus.DRAFT.value,
                )
                .values(**values)
                .returning(Ruleset)
            )
        ).scalar_one_or_none()
        return None if model is None else self._to_record(model)

    async def delete_draft(self, ruleset_id: int) -> bool:
        deleted_id = (
            await self._session.execute(
                delete(Ruleset)
                .where(
                    Ruleset.id == ruleset_id,
                    Ruleset.status == RulesetStatus.DRAFT.value,
                )
                .returning(Ruleset.id)
            )
        ).scalar_one_or_none()
        return deleted_id is not None

    async def publish_draft(
        self, ruleset_id: int, *, published_at: datetime
    ) -> RulesetRecord:
        guild_id = (
            await self._session.execute(
                select(Ruleset.guild_id).where(Ruleset.id == ruleset_id)
            )
        ).scalar_one()
        await self._session.execute(
            update(Ruleset)
            .where(
                Ruleset.guild_id == guild_id,
                Ruleset.status == RulesetStatus.PUBLISHED.value,
            )
            .values(status=RulesetStatus.ARCHIVED.value)
        )
        model = (
            await self._session.execute(
                update(Ruleset)
                .where(
                    Ruleset.id == ruleset_id,
                    Ruleset.status == RulesetStatus.DRAFT.value,
                )
                .values(
                    status=RulesetStatus.PUBLISHED.value,
                    published_at=published_at,
                )
                .returning(Ruleset)
            )
        ).scalar_one()
        return self._to_record(model)

    async def list_acceptances(
        self, guild_id: int, ruleset_id: int
    ) -> tuple[RuleAcceptanceRecord, ...]:
        display_name = func.coalesce(
            func.nullif(GuildMember.nickname, ""),
            func.nullif(DiscordUser.global_name, ""),
            func.nullif(DiscordUser.username, ""),
        )
        rows = (
            await self._session.execute(
                select(
                    RuleAcceptance.user_id,
                    display_name.label("display_name"),
                    RuleAcceptance.accepted_at,
                )
                .join(DiscordUser, DiscordUser.id == RuleAcceptance.user_id)
                .outerjoin(
                    GuildMember,
                    (GuildMember.guild_id == RuleAcceptance.guild_id)
                    & (GuildMember.user_id == RuleAcceptance.user_id),
                )
                .where(
                    RuleAcceptance.guild_id == guild_id,
                    RuleAcceptance.ruleset_id == ruleset_id,
                )
                .order_by(RuleAcceptance.accepted_at.desc())
            )
        ).all()
        return tuple(
            RuleAcceptanceRecord(int(row.user_id), row.display_name, row.accepted_at)
            for row in rows
        )

    @staticmethod
    def _published_between(checkpoint: RulesetRecord, current: RulesetRecord) -> object:
        if checkpoint.published_at is None or current.published_at is None:
            raise ValueError("compliance bounds must be published rulesets")
        return and_(
            Ruleset.status.in_(
                (RulesetStatus.PUBLISHED.value, RulesetStatus.ARCHIVED.value)
            ),
            or_(
                Ruleset.published_at > checkpoint.published_at,
                and_(
                    Ruleset.published_at == checkpoint.published_at,
                    Ruleset.id >= checkpoint.id,
                ),
            ),
            or_(
                Ruleset.published_at < current.published_at,
                and_(
                    Ruleset.published_at == current.published_at,
                    Ruleset.id <= current.id,
                ),
            ),
        )

    @staticmethod
    def _to_record(model: Ruleset) -> RulesetRecord:
        return RulesetRecord(
            id=model.id,
            guild_id=model.guild_id,
            version=model.version,
            title=model.title,
            content=model.content,
            status=RulesetStatus(model.status),
            change_summary=model.change_summary,
            requires_reacceptance=model.requires_reacceptance,
            created_by=model.created_by,
            created_at=model.created_at,
            published_at=model.published_at,
            reacceptance_grace_days=model.reacceptance_grace_days,
        )
