"""Durable confirmed history of Discord Playing activities."""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class GameSession(Base):
    """One continuous observed game identity for one guild member."""

    __tablename__ = "game_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["guild_members.guild_id", "guild_members.user_id"],
            name="fk_game_sessions_guild_member",
        ),
        CheckConstraint(
            "btrim(game_name) <> ''",
            name="ck_game_sessions_game_name_not_blank",
        ),
        CheckConstraint(
            "btrim(game_key) <> ''",
            name="ck_game_sessions_game_key_not_blank",
        ),
        CheckConstraint(
            "application_id IS NULL OR application_id > 0",
            name="ck_game_sessions_application_id_positive",
        ),
        CheckConstraint(
            "started_at <= confirmed_through_at",
            name="ck_game_sessions_started_before_confirmed",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="ck_game_sessions_end_after_start",
        ),
        CheckConstraint(
            "ended_at IS NULL OR confirmed_through_at <= ended_at",
            name="ck_game_sessions_confirmed_before_end",
        ),
        Index(
            "uq_game_sessions_open_guild_user",
            "guild_id",
            "user_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "ix_game_sessions_guild_user_started_at",
            "guild_id",
            "user_id",
            "started_at",
        ),
        Index(
            "ix_game_sessions_guild_started_at",
            "guild_id",
            "started_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    game_key: Mapped[str] = mapped_column(Text, nullable=False)
    game_name: Mapped[str] = mapped_column(Text, nullable=False)
    application_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    confirmed_through_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
