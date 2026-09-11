"""Persistence models for generic guild capability policies and grants."""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class GuildCapabilityPolicyModel(Base):
    __tablename__ = "guild_capability_policies"
    __table_args__ = (
        CheckConstraint("guild_id > 0", name="ck_capability_policies_guild_positive"),
        CheckConstraint(
            "length(btrim(capability_key)) > 0",
            name="ck_capability_policies_key_not_empty",
        ),
        CheckConstraint(
            "updated_by_user_id > 0",
            name="ck_capability_policies_updated_by_positive",
        ),
    )

    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guilds.id", name="fk_capability_policies_guild_id_guilds"),
        primary_key=True,
        autoincrement=False,
    )
    capability_key: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)


class GuildCapabilityGrantModel(Base):
    __tablename__ = "guild_capability_grants"
    __table_args__ = (
        CheckConstraint("guild_id > 0", name="ck_capability_grants_guild_positive"),
        CheckConstraint(
            "length(btrim(capability_key)) > 0",
            name="ck_capability_grants_key_not_empty",
        ),
        CheckConstraint(
            "subject_type IN ('user', 'role')",
            name="ck_capability_grants_subject_type",
        ),
        CheckConstraint(
            "subject_id > 0", name="ck_capability_grants_subject_id_positive"
        ),
        CheckConstraint(
            "created_by_user_id > 0",
            name="ck_capability_grants_created_by_positive",
        ),
    )

    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guilds.id", name="fk_capability_grants_guild_id_guilds"),
        primary_key=True,
        autoincrement=False,
    )
    capability_key: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_type: Mapped[str] = mapped_column(Text, primary_key=True)
    subject_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_by_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
