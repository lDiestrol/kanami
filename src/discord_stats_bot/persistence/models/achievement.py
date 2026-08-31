"""Persistence model for durable per-guild achievement unlocks."""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKeyConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from discord_stats_bot.persistence.models.base import Base


class UserAchievement(Base):
    """One stable achievement key unlocked once for one guild member."""

    __tablename__ = "user_achievements"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "user_id"],
            ["guild_members.guild_id", "guild_members.user_id"],
            name="fk_user_achievements_guild_member",
        ),
        CheckConstraint(
            "guild_id > 0",
            name="ck_user_achievements_guild_id_positive",
        ),
        CheckConstraint(
            "user_id > 0",
            name="ck_user_achievements_user_id_positive",
        ),
        CheckConstraint(
            "char_length(achievement_key) BETWEEN 1 AND 128",
            name="ck_user_achievements_key_length",
        ),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    achievement_key: Mapped[str] = mapped_column(Text, primary_key=True)
    unlocked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
