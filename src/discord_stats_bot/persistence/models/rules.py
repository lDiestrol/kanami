from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class Ruleset(Base):
    """Immutable published server-rules version."""

    __tablename__ = "rulesets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id"],
            ["guilds.id"],
            name="fk_rulesets_guild_id_guilds",
        ),
        ForeignKeyConstraint(
            ["created_by"],
            ["discord_users.id"],
            name="fk_rulesets_created_by_discord_users",
        ),
        CheckConstraint("id > 0", name="ck_rulesets_id_positive"),
        CheckConstraint("btrim(version) <> ''", name="ck_rulesets_version_not_blank"),
        CheckConstraint("btrim(title) <> ''", name="ck_rulesets_title_not_blank"),
        CheckConstraint("btrim(content) <> ''", name="ck_rulesets_content_not_blank"),
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="ck_rulesets_status",
        ),
        CheckConstraint(
            "(status = 'draft' AND published_at IS NULL) OR "
            "(status IN ('published', 'archived') AND published_at IS NOT NULL)",
            name="ck_rulesets_publication_state",
        ),
        CheckConstraint(
            "created_by IS NULL OR created_by > 0",
            name="ck_rulesets_created_by_positive",
        ),
        CheckConstraint(
            "reacceptance_grace_days IS NULL OR "
            "(requires_reacceptance AND reacceptance_grace_days BETWEEN 1 AND 365)",
            name="ck_rulesets_reacceptance_grace_days",
        ),
        UniqueConstraint("guild_id", "version", name="uq_rulesets_guild_version"),
        UniqueConstraint("guild_id", "id", name="uq_rulesets_guild_id_id"),
        Index(
            "uq_rulesets_current_published_guild",
            "guild_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    requires_reacceptance: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reacceptance_grace_days: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RuleAcceptance(Base):
    """Durable proof that one member accepted one exact ruleset."""

    __tablename__ = "rule_acceptances"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["guild_members.guild_id", "guild_members.user_id"],
            name="fk_rule_acceptances_guild_user_guild_members",
        ),
        ForeignKeyConstraint(
            ["guild_id", "ruleset_id"],
            ["rulesets.guild_id", "rulesets.id"],
            name="fk_rule_acceptances_guild_ruleset_rulesets",
        ),
        CheckConstraint("id > 0", name="ck_rule_acceptances_id_positive"),
        UniqueConstraint(
            "guild_id",
            "user_id",
            "ruleset_id",
            name="uq_rule_acceptances_guild_user_ruleset",
        ),
        Index(
            "ix_rule_acceptances_guild_ruleset_accepted_at",
            "guild_id",
            "ruleset_id",
            "accepted_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ruleset_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
